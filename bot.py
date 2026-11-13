"""Personal Telegram monitor for rentals within a configurable walk from work.

python bot.py
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import time
from contextlib import suppress
from math import ceil
from urllib.parse import urlencode

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyKeyboardRemove
from dotenv import load_dotenv

from addresses import AddressResolver
from engine import Engine, Scan
from models import Filters, Listing, WORK_ADDRESS, WORK_LAT, WORK_LON, MIN_WALK_MINUTES, MAX_WALK_MINUTES, matches
from photo_delivery import download_photo, is_https_photo_url
from sources import ListingSources, SOURCE_NAMES
from store import Store
from walking import PedestrianRouter
from telegram_menu import register_menu
from ui_actions import canonical_command

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
log = logging.getLogger('minsk-walk-bot')
router = Router()
repo: Store
engine: Engine
allowed_id: int | None
interval: int


def menu_text(f: Filters) -> str:
    price = f'до {f.max_price_usd} $' if f.max_price_usd else 'любой'
    rooms = 'любые' if f.rooms == 0 else '4+' if f.rooms == 4 else str(f.rooms)
    return (
        '<b>Настройки</b>\n'
        f'Пешком: <b>{f.walk_minutes} мин</b> · бюджет: <b>{price}</b>\n'
        f'Комнаты: {rooms} · собственник: {"только" if f.owner_only else "любой"}\n'
        f'Мониторинг: {"включён" if f.watching else "выключен"}\n'
        'Изменить: /walk 18 · /budget 600 · /owner'
    )


def main_keyboard(f: Filters) -> InlineKeyboardMarkup:
    budget_label = f'{f.max_price_usd} $' if f.max_price_usd else 'Любая цена'
    rooms_label = 'Любые' if f.rooms == 0 else '4+' if f.rooms == 4 else str(f.rooms)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f'🚶 {f.walk_minutes} мин', callback_data='choose:walk'),
         InlineKeyboardButton(text=f'💵 {budget_label}', callback_data='choose:budget')],
        [InlineKeyboardButton(text=f'🚪 {rooms_label}', callback_data='choose:rooms'),
         InlineKeyboardButton(text='✅ Собственник' if f.owner_only else '👤 Все', callback_data='owner')],
        [InlineKeyboardButton(text='🔎 Найти', callback_data='scan'),
         InlineKeyboardButton(text='🔕 Стоп' if f.watching else '🔔 Включить', callback_data='watch')],
        [InlineKeyboardButton(text='🩺 Статус', callback_data='status')],
    ])


def options_keyboard(kind: str) -> InlineKeyboardMarkup:
    if kind == 'walk':
        items = [(f'{value} мин', f'walk:{value}') for value in (10, 15, 20, 25, 30, 40)]
        rows = [items[:3], items[3:]]
    elif kind == 'budget':
        items = [(f'{value} $', f'budget:{value}') for value in (400, 500, 600, 700, 800)]
        rows = [items[:3], items[3:] + [('Без лимита', 'budget:0')]]
    else:
        items = [('Любые', 'rooms:0')] + [(('4+' if value == 4 else str(value)), f'rooms:{value}') for value in (1, 2, 3, 4)]
        rows = [items[:3], items[3:]]
    markup = [[InlineKeyboardButton(text=label, callback_data=data) for label, data in row] for row in rows]
    markup.append([InlineKeyboardButton(text='⬅️ Назад', callback_data='choose:menu')])
    return InlineKeyboardMarkup(inline_keyboard=markup)


def options_text(kind: str) -> str:
    if kind == 'walk':
        return '<b>🚶 Сколько минут пешком?</b>\nДругое число: /walk 18 (от 5 до 45)'
    if kind == 'budget':
        return '<b>💵 Бюджет в месяц</b>\nДругая сумма: /budget 650'
    return '<b>🚪 Сколько комнат?</b>'


def map_url(ad: Listing) -> str:
    assert ad.lat is not None and ad.lon is not None
    return 'https://www.google.com/maps/dir/?' + urlencode({
        'api': 1, 'origin': f'{ad.lat},{ad.lon}',
        'destination': f'{WORK_LAT},{WORK_LON}', 'travelmode': 'walking',
    })


def ad_keyboard(ad: Listing) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f'Открыть на {ad.source}', url=ad.url)],
        [InlineKeyboardButton(text='🚶 Посмотреть пеший маршрут', url=map_url(ad))],
    ])


def listing_text(ad: Listing) -> str:
    assert ad.walk_seconds is not None
    minutes = ceil(ad.walk_seconds / 60)
    price = (f'{ad.price:,}'.replace(',', ' ') + f' {ad.currency}/мес.') if ad.price is not None else 'цена не указана'
    owner = 'Собственник' if ad.owner is True else 'Агентство' if ad.owner is False else 'Не указано'
    return (
        f'<b>🏠 {html.escape(ad.title)}</b>\n'
        f'🚶 <b>{minutes} мин до работы</b> · 💵 <b>{html.escape(price)}</b>\n'
        f'👤 {owner} · 🌐 {html.escape(ad.source)}\n'
        f'📍 {html.escape(ad.address)}'
    )


async def send_listing(bot: Bot, user_id: int, ad: Listing) -> None:
    """Send the main photo with the full listing caption, or a text fallback.

    Keep source/owner filters untouched. A failed photo must never hide an ad.
    """
    text = listing_text(ad)
    keyboard = ad_keyboard(ad)
    # Telegram limits photo captions to 1024 characters. Unusually long
    # addresses still get delivered as text rather than silently failing.
    if is_https_photo_url(ad.image) and len(text) <= 1024:
        try:
            await bot.send_photo(
                user_id, photo=ad.image, caption=text,
                parse_mode='HTML', reply_markup=keyboard,
            )
            return
        except TelegramBadRequest as exc:
            # Some image CDNs reject Telegram's fetchers. Download from the
            # original marketplace and upload the bytes as a second attempt.
            log.info('Photo URL rejected for %s: %s', ad.key, exc)
            try:
                downloaded = await download_photo(ad.image, ad.url, ad.source)
            except (httpx.HTTPError, ValueError) as fetch_exc:
                log.warning('Photo download failed for %s: %s', ad.key, fetch_exc)
                downloaded = None
            if downloaded is not None:
                image_bytes, filename = downloaded
                try:
                    await bot.send_photo(
                        user_id, photo=BufferedInputFile(image_bytes, filename=filename),
                        caption=text, parse_mode='HTML', reply_markup=keyboard,
                    )
                    return
                except TelegramBadRequest as upload_exc:
                    log.warning('Photo upload rejected for %s: %s', ad.key, upload_exc)
    # No photo, broken link, or unsupported format: always send the listing.
    await bot.send_message(
        user_id, text, parse_mode='HTML',
        reply_markup=keyboard, disable_web_page_preview=True,
    )


def _auth_id(user_id: int) -> bool:
    if allowed_id is not None:
        return user_id == allowed_id
    owner = repo.get_meta_float('claimed_user_id')
    if owner == 0:
        # For a personal bot the first /start claims it. Set ALLOWED_USER_ID in
        # .env to avoid anybody claiming it before you.
        repo.set_meta('claimed_user_id', str(user_id))
        return True
    return int(owner) == user_id


async def _accept_message(message: Message) -> bool:
    if message.chat.type != 'private' or not message.from_user:
        await message.answer('Этот бот работает только в личных сообщениях.')
        return False
    if not _auth_id(message.from_user.id):
        await message.answer('Это личный бот, доступ к нему закрыт.')
        return False
    return True


async def _accept_callback(cq: CallbackQuery) -> bool:
    if not cq.message or not _auth_id(cq.from_user.id):
        await cq.answer('Нет доступа к этому боту.', show_alert=True)
        return False
    return True


async def reply_menu(message: Message, user_id: int, *, edit: bool = False) -> None:
    flt = repo.get_user(user_id)
    if edit:
        try:
            await message.edit_text(menu_text(flt), parse_mode='HTML', reply_markup=main_keyboard(flt))
        except TelegramBadRequest:
            pass
    else:
        await message.answer(menu_text(flt), parse_mode='HTML', reply_markup=main_keyboard(flt))


def summary_text(scan: Scan) -> str:
    source_bits = [f'{"✅" if s.ok else "❌"} {s.source}: {len(s.ads) if s.ok else "ошибка"}'
                   for s in scan.sources]
    return (
        '\n'.join(source_bits) + '\n'
        f'Проверено объявлений: {scan.fetched}; рядом по прямой: {scan.candidates}; '
        f'без точных координат: {scan.geo_missing}; '
        f'без пешего маршрута: {scan.unrouted}.'
    )


async def do_search(bot: Bot, chat_id: int, user_id: int, *, seed: bool = False) -> None:
    flt = repo.get_user(user_id)
    progress = await bot.send_message(chat_id, '🔎 Ищу квартиры…')
    try:
        scan = await engine.scan(max_walk_minutes=flt.walk_minutes)
    except Exception:
        log.exception('Scan failed')
        await progress.edit_text('⚠️ Не удалось проверить объявления. Повторю позже.')
        return
    found = scan.filtered(flt)
    warning = ' · ⚠️ часть сайтов недоступна' if any(not s.ok for s in scan.sources) else ''
    prefix = '🔔 Мониторинг включён\n' if seed else ''
    await progress.edit_text(prefix + f'🏠 Подходящих: <b>{len(found)}</b>{warning}', parse_mode='HTML')
    delivered: list[str] = []
    for ad in found[:6]:
        try:
            await send_listing(bot, chat_id, ad)
            delivered.append(ad.key)
        except TelegramRetryAfter as exc:
            log.warning('Telegram rate limited: %s', exc)
            break
        except TelegramBadRequest as exc:
            log.warning('Telegram rejected listing %s: %s', ad.key, exc)
    if seed:
        undelivered = {ad.key for ad in found[:6]} - set(delivered)
        repo.mark_seen(user_id, [ad.key for ad in found if ad.key not in undelivered])


@router.message(CommandStart())
@router.message(Command('help'))
@router.message(Command('menu'))
async def start(message: Message) -> None:
    if await _accept_message(message):
        await reply_menu(message, message.from_user.id)


@router.message(Command('hide'))
async def hide_cmd(message: Message) -> None:
    if await _accept_message(message):
        await message.answer('Клавиатура скрыта. Настройки: /menu',
                             reply_markup=ReplyKeyboardRemove())


@router.message(Command('search'))
async def search_cmd(message: Message) -> None:
    if await _accept_message(message):
        await do_search(message.bot, message.chat.id, message.from_user.id)


@router.message(Command('watch'))
async def watch_cmd(message: Message) -> None:
    if not await _accept_message(message):
        return
    repo.update_user(message.from_user.id, watching=True)
    await do_search(message.bot, message.chat.id, message.from_user.id, seed=True)


@router.message(Command('stop'))
async def stop_cmd(message: Message) -> None:
    if await _accept_message(message):
        repo.update_user(message.from_user.id, watching=False)
        await message.answer('🔕 Мониторинг выключен.')


@router.message(Command('owner'))
async def owner_cmd(message: Message) -> None:
    if not await _accept_message(message):
        return
    parts = (message.text or '').split()
    current = repo.get_user(message.from_user.id)
    if len(parts) == 1:
        value = not current.owner_only
    elif len(parts) == 2 and parts[1].lower() in ('on', 'yes', '1'):
        value = True
    elif len(parts) == 2 and parts[1].lower() in ('off', 'no', '0'):
        value = False
    else:
        await message.answer('👤 Используй /owner on или /owner off')
        return
    repo.update_user(message.from_user.id, owner_only=value)
    await reply_menu(message, message.from_user.id)


@router.message(Command('budget'))
async def budget_cmd(message: Message) -> None:
    if not await _accept_message(message):
        return
    parts = (message.text or '').split()
    if len(parts) != 2 or not parts[1].isdigit() or int(parts[1]) > 100_000:
        await message.answer('💵 Например: /budget 600 или /budget 0')
        return
    repo.update_user(message.from_user.id, max_price_usd=int(parts[1]))
    await reply_menu(message, message.from_user.id)


@router.message(Command('rooms'))
async def rooms_cmd(message: Message) -> None:
    if not await _accept_message(message):
        return
    parts = (message.text or '').split()
    if len(parts) != 2 or parts[1] not in ('0', '1', '2', '3', '4'):
        await message.answer('🚪 Например: /rooms 1 или /rooms 0')
        return
    repo.update_user(message.from_user.id, rooms=int(parts[1]))
    await reply_menu(message, message.from_user.id)


@router.message(Command('walk'))
async def walk_cmd(message: Message) -> None:
    if not await _accept_message(message):
        return
    parts = (message.text or '').split()
    if len(parts) != 2 or not parts[1].isdigit() or not MIN_WALK_MINUTES <= int(parts[1]) <= MAX_WALK_MINUTES:
        await message.answer('🚶 Например: /walk 15 (от 5 до 45 мин)')
        return
    repo.update_user(message.from_user.id, walk_minutes=int(parts[1]))
    await reply_menu(message, message.from_user.id)


@router.message(Command('toggle'))
async def toggle_cmd(message: Message) -> None:
    if not await _accept_message(message):
        return
    f = repo.get_user(message.from_user.id)
    repo.update_user(message.from_user.id, watching=not f.watching)
    await reply_menu(message, message.from_user.id)


@router.message(Command('status'))
async def status_cmd(message: Message) -> None:
    if await _accept_message(message):
        await message.answer(status_text(), parse_mode='HTML')


def status_text() -> str:
    rows = repo.statuses()
    if not rows:
        return '🩺 Проверок пока не было.'
    lines = [f'{"✅" if r["ok"] else "⚠️"} {html.escape(r["name"])}: {r["total"] if r["ok"] else "ошибка"}' for r in rows]
    failed = [r for r in rows if not r['ok']]
    if failed:
        lines.append('⚠️ Часть объявлений может отсутствовать.')
    last_scan = repo.db.execute("SELECT value FROM meta WHERE name='last_scan'").fetchone()
    if last_scan:
        lines.append('🕒 ' + html.escape(last_scan[0]))
    return '🩺 <b>Источники</b>\n' + '\n'.join(lines)


@router.callback_query(F.data == 'scan')
async def search_btn(cq: CallbackQuery) -> None:
    if await _accept_callback(cq):
        await cq.answer()
        await do_search(cq.bot, cq.message.chat.id, cq.from_user.id)


@router.callback_query(F.data == 'watch')
async def watch_btn(cq: CallbackQuery) -> None:
    if not await _accept_callback(cq):
        return
    await cq.answer()
    flt = repo.get_user(cq.from_user.id)
    if flt.watching:
        repo.update_user(cq.from_user.id, watching=False)
        await cq.message.answer('🔕 Мониторинг выключен.')
    else:
        repo.update_user(cq.from_user.id, watching=True)
        await do_search(cq.bot, cq.message.chat.id, cq.from_user.id, seed=True)
    await reply_menu(cq.message, cq.from_user.id, edit=True)


@router.callback_query(F.data.startswith('choose:'))
async def choose_btn(cq: CallbackQuery) -> None:
    if not await _accept_callback(cq):
        return
    kind = (cq.data or '').partition(':')[2]
    await cq.answer()
    if kind == 'menu':
        await reply_menu(cq.message, cq.from_user.id, edit=True)
        return
    if kind not in ('walk', 'budget', 'rooms'):
        return
    try:
        await cq.message.edit_text(options_text(kind), parse_mode='HTML', reply_markup=options_keyboard(kind))
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith('walk:'))
async def walk_btn(cq: CallbackQuery) -> None:
    if not await _accept_callback(cq):
        return
    try:
        value = int((cq.data or '').partition(':')[2])
    except ValueError:
        await cq.answer('Некорректное значение')
        return
    if not MIN_WALK_MINUTES <= value <= MAX_WALK_MINUTES:
        await cq.answer('Некорректное значение')
        return
    repo.update_user(cq.from_user.id, walk_minutes=value)
    await cq.answer('Сохранено')
    await reply_menu(cq.message, cq.from_user.id, edit=True)


@router.callback_query(F.data.startswith('budget:'))
async def budget_btn(cq: CallbackQuery) -> None:
    if not await _accept_callback(cq):
        return
    try:
        value = int((cq.data or '').partition(':')[2])
    except ValueError:
        await cq.answer('Некорректное значение')
        return
    if not 0 <= value <= 100000:
        await cq.answer('Некорректное значение')
        return
    repo.update_user(cq.from_user.id, max_price_usd=value)
    await cq.answer('Сохранено')
    await reply_menu(cq.message, cq.from_user.id, edit=True)


@router.callback_query(F.data == 'owner')
async def owner_btn(cq: CallbackQuery) -> None:
    if await _accept_callback(cq):
        flt = repo.get_user(cq.from_user.id)
        repo.update_user(cq.from_user.id, owner_only=not flt.owner_only)
        await cq.answer()
        await reply_menu(cq.message, cq.from_user.id, edit=True)


@router.callback_query(F.data.startswith('rooms:'))
async def rooms_btn(cq: CallbackQuery) -> None:
    if await _accept_callback(cq):
        try:
            value = int(cq.data.partition(':')[2])
        except (TypeError, ValueError):
            await cq.answer('Некорректное значение')
            return
        if value not in range(5):
            await cq.answer('Некорректное значение')
            return
        repo.update_user(cq.from_user.id, rooms=value)
        await cq.answer()
        await reply_menu(cq.message, cq.from_user.id, edit=True)


@router.callback_query(F.data == 'status')
async def status_btn(cq: CallbackQuery) -> None:
    if await _accept_callback(cq):
        await cq.answer()
        await cq.message.answer(status_text(), parse_mode='HTML')


@router.message()
async def other(message: Message) -> None:
    if not await _accept_message(message):
        return
    action = canonical_command(message.text or '')
    if not action.startswith('/'):
        await message.answer('⚙️ Нажми /menu для настроек.')
        return
    cmd = action.split()[0].lower()
    # Reply-keyboard labels need to work when running this bot on a PC, too.
    if cmd == '/menu':
        await reply_menu(message, message.from_user.id)
    elif cmd == '/search':
        await do_search(message.bot, message.chat.id, message.from_user.id)
    elif cmd == '/status':
        await message.answer(status_text(), parse_mode='HTML')
    elif cmd == '/toggle':
        f = repo.get_user(message.from_user.id)
        repo.update_user(message.from_user.id, watching=not f.watching)
        await reply_menu(message, message.from_user.id)
    elif cmd == '/owner':
        f = repo.get_user(message.from_user.id)
        repo.update_user(message.from_user.id, owner_only=not f.owner_only)
        await reply_menu(message, message.from_user.id)
    elif cmd in ('/walk', '/budget') and len(action.split()) == 2:
        value_str = action.split()[1]
        if not value_str.isdigit():
            await message.answer('⚠️ Нужна цифра.')
            return
        value = int(value_str)
        if cmd == '/walk' and MIN_WALK_MINUTES <= value <= MAX_WALK_MINUTES:
            repo.update_user(message.from_user.id, walk_minutes=value)
        elif cmd == '/budget' and 0 <= value <= 100000:
            repo.update_user(message.from_user.id, max_price_usd=value)
        else:
            await message.answer('⚠️ Некорректное значение.')
            return
        await reply_menu(message, message.from_user.id)
    elif cmd == '/budget':
        await message.answer('💵 Напиши /budget 600 или /budget 0')
    else:
        await message.answer('⚙️ Нажми /menu для настроек.')


async def notify_loop(bot: Bot) -> None:
    failure_cycles = {name: 0 for name in SOURCE_NAMES}
    last_error_message = 0.0
    while True:
        await asyncio.sleep(interval)
        subscribers = repo.watchers()
        if not subscribers:
            continue
        try:
            scan = await engine.scan(fresh=True, max_walk_minutes=max(repo.get_user(uid).walk_minutes for uid in subscribers))
            for row in scan.sources:
                failure_cycles[row.source] = 0 if row.ok else failure_cycles[row.source] + 1
            failing = [name for name, fails in failure_cycles.items() if fails >= 3]
            if (failing or scan.unrouted) and time.monotonic() - last_error_message > 12 * 3600:
                msg = '⚠️ Проблема с мониторингом: ' + (', '.join(failing) if failing else 'пешие маршруты')
                msg += '. Часть квартир может не находиться. /status'
                for uid in subscribers:
                    with suppress(TelegramForbiddenError, TelegramBadRequest):
                        await bot.send_message(uid, msg)
                last_error_message = time.monotonic()
            for uid in subscribers:
                flt = repo.get_user(uid)
                matched = [ad for ad in scan.ads if matches(ad, flt) and not repo.has_seen(uid, ad.key)]
                # Limit per check; do NOT mark omitted ads as seen. Retry next scan.
                for ad in matched[:6]:
                    try:
                        await send_listing(bot, uid, ad)
                        repo.mark_seen(uid, [ad.key])
                        await asyncio.sleep(0.1)
                    except TelegramForbiddenError:
                        repo.update_user(uid, watching=False)
                        break
                    except TelegramRetryAfter as exc:
                        log.warning('Telegram rate limit: retry after %ss', exc.retry_after)
                        break
                    except TelegramBadRequest as exc:
                        log.error('Telegram rejected %s: %s', ad.key, exc)
                        # Avoid infinite reattempts of an invalid listing.
                        repo.mark_seen(uid, [ad.key])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Background monitor cycle failed; next cycle will retry')


async def main() -> None:
    global repo, engine, allowed_id, interval
    load_dotenv()
    token = os.getenv('BOT_TOKEN', '').strip()
    if not token or ':' not in token:
        raise SystemExit('Нужен BOT_TOKEN. Получи его у @BotFather и запиши в .env')
    allowed = os.getenv('ALLOWED_USER_ID', '').strip()
    allowed_id = int(allowed) if allowed.isdigit() else None
    interval = max(300, int(os.getenv('CHECK_INTERVAL_SECONDS', '600')))
    pages = max(1, min(5, int(os.getenv('PAGES_PER_SOURCE', '3'))))
    repo = Store(os.getenv('DB_PATH', 'data/apartments.sqlite3'))
    headers = {
        'User-Agent': 'MinskWalkRentBot/2.0 (personal rental alert; support via Telegram)',
        'Accept-Language': 'ru-RU,ru;q=0.9,en;q=0.8',
    }
    async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers=headers) as client:
        src = ListingSources(client, pages=pages)
        adr = AddressResolver(client, repo)
        foot = PedestrianRouter(client, repo)
        engine = Engine(repo, src, adr, foot)
        dp = Dispatcher()
        dp.include_router(router)
        async with Bot(token=token) as bot:
            try:
                await register_menu(bot)
            except TelegramAPIError as exc:
                log.warning('Could not update Telegram command menu: %s', exc)
            watcher = asyncio.create_task(notify_loop(bot))
            try:
                log.info('Started. Work: %s, interval: %s seconds', WORK_ADDRESS, interval)
                await dp.start_polling(bot)
            finally:
                watcher.cancel()
                with suppress(asyncio.CancelledError):
                    await watcher
    repo.close()


if __name__ == '__main__':
    asyncio.run(main())
