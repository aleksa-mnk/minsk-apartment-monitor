"""Run one monitoring cycle on GitHub Actions (no continuous Telegram polling).

Persistent, small settings/seen state lives in data/state.json (committed by CI).
The larger OSM/route SQLite cache is restored via GitHub Actions cache, best effort.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import httpx
from aiogram import Bot
from aiogram.types import ReplyKeyboardRemove
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from dotenv import load_dotenv

import bot as telegram_ui
from addresses import AddressResolver
from engine import Engine
from models import Filters, MIN_WALK_MINUTES, MAX_WALK_MINUTES, now_utc
from panel_config import apply_panel_config
from sources import ListingSources
from store import Store
from walking import PedestrianRouter
from telegram_menu import register_menu
from ui_actions import canonical_command

log = logging.getLogger('minsk-github-monitor')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

STATE_PATH = Path(os.getenv('STATE_PATH', 'data/state.json'))
AUTOMATIC_BATCH = 8
MANUAL_BATCH = 12
DEFAULT_STATE = {
    'version': 1,
    'initialized': False,
    'update_offset': 0,
    'watching': True,
    'owner_only': False,
    'max_price_usd': 0,
    'rooms': 0,
    'walk_minutes': 20,
    'ui_version': 0,
    'panel_revision': '',
    'last_scan_at': '',
    'last_sent': 0,
    'last_matches': 0,
    'last_fetched': 0,
    # Repeated /search goes through the results in pages, not the same first 3.
    'manual_offset': 0,
    'seen': [],
}


def read_state(path: Path = STATE_PATH) -> dict:
    state = DEFAULT_STATE.copy()
    state['seen'] = []
    if path.exists():
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            raise ValueError('State must be a JSON object')
        for field in DEFAULT_STATE:
            if field in data:
                state[field] = data[field]
    if not isinstance(state['seen'], list) or not all(isinstance(k, str) for k in state['seen']):
        raise ValueError('State.seen must be a list of strings')
    for field in ('update_offset', 'max_price_usd', 'rooms', 'walk_minutes', 'ui_version',
                  'last_sent', 'last_matches', 'last_fetched', 'manual_offset'):
        if not isinstance(state[field], int) or state[field] < 0:
            raise ValueError(f'State.{field} must be a nonnegative integer')
    if (state['rooms'] > 4 or state['max_price_usd'] > 100000
            or not MIN_WALK_MINUTES <= state['walk_minutes'] <= MAX_WALK_MINUTES):
        raise ValueError('Saved filters are invalid')
    for field in ('initialized', 'watching', 'owner_only'):
        if not isinstance(state[field], bool):
            raise ValueError(f'State.{field} must be a boolean')
    for field in ('panel_revision', 'last_scan_at'):
        if not isinstance(state[field], str):
            raise ValueError(f'State.{field} must be a string')
    state['seen'] = sorted(set(state['seen']))
    return state


def write_state(state: dict, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**state, 'seen': sorted(set(state['seen']))}
    updated = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + '\n'
    if path.exists() and path.read_text(encoding='utf-8') == updated:
        return
    temp_path = path.with_suffix('.tmp')
    temp_path.write_text(updated, encoding='utf-8')
    temp_path.replace(path)


def sync_filters_to_store(store: Store, chat_id: int, state: dict) -> Filters:
    flt = store.update_user(
        chat_id,
        max_price_usd=state['max_price_usd'],
        rooms=state['rooms'],
        owner_only=state['owner_only'],
        watching=state['watching'],
        walk_minutes=state['walk_minutes'],
    )
    store.mark_seen(chat_id, state['seen'])
    return flt


def update_filter_from_command(state: dict, text: str) -> str | None:
    """Update persistent GitHub filters from slash commands or reply buttons."""
    parts = canonical_command(text).strip().split()
    if not parts:
        return None
    command = parts[0].split('@')[0].lower()
    if command == '/watch':
        state['watching'] = True
    elif command == '/stop':
        state['watching'] = False
    elif command == '/toggle':
        state['watching'] = not state['watching']
    elif command == '/owner':
        if len(parts) == 1:
            state['owner_only'] = not state['owner_only']
        elif len(parts) == 2 and parts[1].lower() in ('on', 'yes', '1'):
            state['owner_only'] = True
        elif len(parts) == 2 and parts[1].lower() in ('off', 'no', '0'):
            state['owner_only'] = False
        else:
            return 'invalid'
    elif command == '/budget':
        if len(parts) == 1:
            return 'prompt_budget'
        if len(parts) != 2 or not parts[1].isdigit() or int(parts[1]) > 100000:
            return 'invalid'
        state['max_price_usd'] = int(parts[1])
    elif command == '/walk':
        if len(parts) == 1:
            return 'prompt_walk'
        if len(parts) != 2 or not parts[1].isdigit() or not MIN_WALK_MINUTES <= int(parts[1]) <= MAX_WALK_MINUTES:
            return 'invalid'
        state['walk_minutes'] = int(parts[1])
    elif command == '/rooms':
        if len(parts) == 1:
            return 'prompt_rooms'
        if len(parts) != 2 or parts[1] not in ('0', '1', '2', '3', '4'):
            return 'invalid'
        state['rooms'] = int(parts[1])
    elif command in ('/start', '/help', '/menu', '/status', '/search', '/hide'):
        pass
    else:
        return None
    return command


def format_action_reply(last_action: str, flt: Filters) -> str:
    if last_action == '/hide':
        return 'Клавиатура скрыта. Настройки: /menu'
    if last_action == 'prompt_walk':
        return '🚶 Напиши /walk 18 (от 5 до 45 мин)'
    if last_action == 'prompt_budget':
        return '💵 Напиши /budget 650 (или /budget 0)'
    if last_action == 'prompt_rooms':
        return '🚪 Напиши /rooms 1 (или /rooms 0)'
    if last_action == 'invalid':
        return '⚠️ Проверь значение: /walk 18 · /budget 600 · /rooms 1'
    return telegram_ui.menu_text(flt)


async def handle_commands(tg: Bot, chat_id: int, state: dict, store: Store, engine: Engine) -> bool:
    """Apply all queued messages; send ONE compact reply instead of spamming."""
    force_search = False
    last_action: str | None = None
    for _ in range(3):
        updates = await tg.get_updates(offset=state['update_offset'], timeout=0, limit=100)
        if not updates:
            break
        for upd in updates:
            message = upd.message
            if message and message.chat.id == chat_id and message.chat.type == 'private':
                action = update_filter_from_command(state, message.text or '')
                if action is not None:
                    last_action = action
                    if action == '/search':
                        force_search = True
            # An old inline menu may still be visible in Telegram. Support its
            # direct actions, but new GitHub menus use reply buttons without
            # the annoying 30-minute callback spinner.
            callback = getattr(upd, 'callback_query', None)
            if callback and callback.from_user.id == chat_id:
                data = callback.data or ''
                if data in ('owner', 'watch', 'scan', 'status') or data.startswith(('walk:', 'budget:', 'rooms:')):
                    command = ('/owner' if data == 'owner' else '/toggle' if data == 'watch' else
                               '/search' if data == 'scan' else '/status' if data == 'status' else
                               '/' + data.replace(':', ' ', 1))
                    action = update_filter_from_command(state, command)
                    if action is not None:
                        last_action = action
                        if action == '/search':
                            force_search = True
                # Callback answer may have expired by the time Actions runs.
                try:
                    await tg.answer_callback_query(callback.id)
                except TelegramAPIError:
                    pass
            state['update_offset'] = max(state['update_offset'], upd.update_id + 1)
        if len(updates) < 100:
            break
    if last_action is not None:
        flt = sync_filters_to_store(store, chat_id, state)
        response = telegram_ui.status_text() if last_action == '/status' else format_action_reply(last_action, flt)
        # Remove the old, oversized keyboard once. Later replies remain plain
        # messages, so Telegram's compact slash-command Menu stays available.
        remove_keyboard = state['ui_version'] < 3 or last_action == '/hide'
        await tg.send_message(
            chat_id, response, parse_mode='HTML',
            reply_markup=ReplyKeyboardRemove() if remove_keyboard else None,
        )
        state['ui_version'] = 3
    return force_search


async def send_adverts(tg: Bot, chat_id: int, state: dict, scan, flt: Filters, *, force_search: bool) -> int:
    seen = set(state['seen'])
    matched = scan.filtered(flt)
    if force_search:
        # Explicit search: page through all current matches, including those
        # silently marked 'seen' by the older, lossy initial preview logic.
        offset = state.get('manual_offset', 0)
        if offset >= len(matched):
            offset = 0
        chosen = matched[offset:offset + MANUAL_BATCH]
        next_offset = offset + len(chosen)
        state['manual_offset'] = next_offset if next_offset < len(matched) else 0
    else:
        # Every matching ad gets its turn. Do not mark unsent listings as seen.
        chosen = [ad for ad in matched if ad.key not in seen][:AUTOMATIC_BATCH]
    sent = 0
    blocked = False
    bad_requests = 0
    for ad in chosen:
        try:
            await telegram_ui.send_listing(tg, chat_id, ad)
            seen.add(ad.key)
            sent += 1
            await asyncio.sleep(1.05)
        except TelegramForbiddenError:
            state['watching'] = False
            blocked = True
            log.error('Bot was blocked by the user; monitoring stopped')
            break
        except TelegramRetryAfter as exc:
            log.warning('Telegram rate-limited listing %s: %s', ad.key, exc)
            break
        except TelegramBadRequest as exc:
            # One broken ad must not prevent the rest from reaching the user.
            log.warning('Telegram rejected listing %s: %s', ad.key, exc)
            bad_requests += 1
            if bad_requests >= 4:
                break
    if not blocked:
        state['initialized'] = True
    state['seen'] = sorted(seen)
    return sent


async def scheduled_cycle(tg: Bot, chat_id: int, state: dict, store: Store, engine: Engine) -> None:
    telegram_ui.repo = store
    telegram_ui.engine = engine
    sync_filters_to_store(store, chat_id, state)
    # Telegram webhook on Netlify handles all messages and callbacks immediately.
    # getUpdates cannot be used while a webhook is configured.
    webhook = os.getenv('TELEGRAM_WEBHOOK_MODE', '1') == '1'
    force_search = os.getenv('FORCE_SEARCH', '0') == '1'
    if not webhook:
        force_search = await handle_commands(tg, chat_id, state, store, engine) or force_search
    if not webhook and state['ui_version'] < 3:
        # Migration: Telegram keeps old reply keyboards until the bot explicitly
        # removes them. Do this once even when no commands are waiting.
        await tg.send_message(
            chat_id, 'Большие кнопки скрыты. Настройки: /menu',
            reply_markup=ReplyKeyboardRemove(),
        )
        state['ui_version'] = 3
    if not state['watching'] and not force_search:
        log.info('Monitoring is paused; commands were processed')
        return
    scan = await engine.scan(fresh=True, max_walk_minutes=state['walk_minutes'])
    for sr in scan.sources:
        log.info('%s: ok=%s fetched=%s error=%s', sr.source, sr.ok, len(sr.ads), sr.error)
    current_filters = store.get_user(chat_id)
    log.info('Scanned %d ads, nearby=%d, unlocated=%d, unrouted=%d, matches=%d',
             scan.fetched, scan.candidates, scan.geo_missing, scan.unrouted,
             len(scan.filtered(current_filters)))
    log.info('USD/BYN NBRB reference rate: %s',
             f'{scan.byn_per_usd:.4f}' if scan.byn_per_usd else 'unavailable (BYN prices cannot be checked)')
    for source, stat in scan.diagnostics(current_filters).items():
        log.info('SOURCE %s: fetched=%d, no_geo=%d, osm_resolved=%d, near=%d, '
                 'routed=%d, walk_ok=%d, price_ok=%d, matched=%d',
                 source, stat.get('fetched', 0), stat.get('geo_missing', 0),
                 stat.get('geo_resolved', 0), stat.get('near_straight', 0),
                 stat.get('routed', 0), stat.get('within_walk', 0),
                 stat.get('price_ok', 0), stat.get('matched', 0))
    if not any(source.ok for source in scan.sources):
        raise RuntimeError('All three listing sources failed. Retry on the next schedule.')
    state['last_scan_at'] = now_utc()
    if scan.candidates and not scan.ads and scan.unrouted == scan.candidates:
        log.warning('All nearby routes failed; keeping uninitialized state for a later retry')
        return
    current = store.get_user(chat_id)
    matched = scan.filtered(current)
    state['last_matches'] = len(matched)
    state['last_fetched'] = scan.fetched
    delivered = await send_adverts(tg, chat_id, state, scan, current, force_search=force_search)
    state['last_sent'] = delivered
    log.info('Delivered %s Telegram messages', delivered)
    if force_search:
        # Explain the real number of matches instead of leaving the impression
        # that the bot intentionally shows only three listings.
        by_site = scan.diagnostics(current)
        source_counts = ', '.join(f'{name} {st.get("matched", 0)}' for name, st in by_site.items())
        summary = f'Подходит: {len(matched)}. {source_counts}.'
        if len(matched) > MANUAL_BATCH:
            if state['manual_offset']:
                summary += ' Остальные: нажми /search ещё раз.'
            else:
                summary += ' Это последняя часть выдачи.'
        if delivered < len(matched) and not state['manual_offset'] and len(matched) <= MANUAL_BATCH:
            summary += f' Отправлено: {delivered}.'
        await tg.send_message(chat_id, summary)


async def main() -> None:
    load_dotenv()
    token = os.getenv('BOT_TOKEN', '').strip()
    chat_str = os.getenv('TELEGRAM_CHAT_ID', '').strip()
    if not token or ':' not in token:
        raise SystemExit('Set BOT_TOKEN in GitHub Actions Secrets')
    if not chat_str.isdigit() or int(chat_str) <= 0:
        raise SystemExit('Set numeric TELEGRAM_CHAT_ID in GitHub Actions Secrets')
    chat_id = int(chat_str)
    path = Path(os.getenv('DB_PATH', 'data/apartments.sqlite3'))
    path.parent.mkdir(parents=True, exist_ok=True)
    state = read_state()
    try:
        if apply_panel_config(state):
            log.info('New dashboard preferences applied from data/panel_settings.json')
    except (ValueError, OSError) as exc:
        # Corrupt panel config must not silently reset user filters or prevent alerts.
        log.error('Invalid dashboard configuration, retaining previous filters: %s', exc)
    store = Store(str(path))
    headers = {
        'User-Agent': 'MinskWalkRentBot/3.0 (personal rental alert)',
        'Accept-Language': 'ru-RU,ru;q=0.9,en;q=0.8',
    }
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers=headers) as client:
            src = ListingSources(client, pages=max(1, min(8, int(os.getenv('PAGES_PER_SOURCE', '8')))))
            resolver = AddressResolver(client, store)
            pedestrian = PedestrianRouter(client, store)
            engine = Engine(store, src, resolver, pedestrian)
            async with Bot(token=token) as tg:
                try:
                    await register_menu(tg)
                except TelegramAPIError as exc:
                    log.warning('Could not update Telegram command menu: %s', exc)
                await scheduled_cycle(tg, chat_id, state, store, engine)
    finally:
        # GitHub Actions commits this file even if a scan later fails.
        # Already delivered adverts and processed commands will not be forgotten.
        write_state(state)
        store.close()


if __name__ == '__main__':
    asyncio.run(main())
