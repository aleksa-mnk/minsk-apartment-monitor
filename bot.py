"""Personal Telegram monitor for rentals within 20 minutes on foot of work.

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
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

from addresses import AddressResolver
from engine import Engine, Scan
from models import Filters, Listing, WORK_ADDRESS, WORK_LAT, WORK_LON, matches
from photo_delivery import download_photo, is_https_photo_url
from sources import ListingSources, SOURCE_NAMES
from store import Store
from walking import PedestrianRouter

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
log = logging.getLogger('minsk-walk-bot')
router = Router()
repo: Store
engine: Engine
allowed_id: int | None
interval: int


def menu_text(f: Filters) -> str:
    price = f'до ${f.max_price_usd}' if f.max_price_usd else 'любой'
    rooms = 'любое' if f.rooms == 0 else '4+' if f.rooms == 4 else str(f.rooms)
    return (
        '<b>🏡 Квартиры в 20 минутах пешком от работы</b>\n\n'
        '📍 Минск, Мележа 5к1\n'
        '🚶 Маршрут пешком ≤ 20 минут, не радиус по прямой\n'
        '🏢 Источники: Kufar, Realt, Onliner\n'
        '🗓 Только долгосрочная аренда\n\n'
        f'💵 Бюджет в USD: <b>{price}</b>\n'
        f'🚪 Комнат: <b>{rooms}</b>\n'
        f'👤 Только собственник: <b>{"да" if f.owner_only else "нет"}</b>\n'
        f'🔔 Мониторинг: <b>{"включён" if f.watching else "выключен"}</b>\n\n'
        'Изменить бюджет: /budget 600 (или /budget 0)\n'
        'Количество комнат: /rooms 1 (или /rooms 0)\n'
        'Проверить доступность сайтов: /status'
    )


def main_keyboard(f: Filters) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='🔎 Найти сейчас', callback_data='scan')],
        [InlineKeyboardButton(text='🔕 Остановить' if f.watching else '🔔 Начать мониторинг', callback_data='watch')],
        [InlineKeyboardButton(text=('✅ ' if f.owner_only else '⬜ ') + 'Только собственник', callback_data='owner')],
        [InlineKeyboardButton(text='🚪 Любые', callback_data='rooms:0'),
         InlineKeyboardButton(text='1', callback_data='rooms:1'),
         InlineKeyboardButton(text='2', callback_data='rooms:2'),
         InlineKeyboardButton(text='3', callback_data='rooms:3'),
         InlineKeyboardButton(text='4+', callback_data='rooms:4')],
        [InlineKeyboardButton(text='🩺 Статус источников', callback_data='status')],
    ])


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
    price = f'{ad.price:,}'.replace(',', ' ') + f' {ad.currency}/мес.' if ad.price else 'не указана'
    return (
        f'<b>🏠 {html.escape(ad.title)}</b>\n'
        f'📍 {html.escape(ad.address)}\n'
        f'🚶 <b>~{minutes} мин. пешком до Мележа, 5к1</b> '
        '(маршрут OpenStreetMap)\n'
        f'💵 {html.escape(price)}\n'
        f'🚪 Комнат: {ad.rooms if ad.rooms else "не указано"}\n'
        f'👤 {"Собственник" if ad.owner is True else "Агентство" if ad.owner is False else "Неизвестно"}\n'
        f'🌐 {html.escape(ad.source)}'
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
    await bot.send_message(chat_id, '🔍 Проверяю Kufar, Realt, Onliner и пешие маршруты…')
    try:
        scan = await engine.scan()
    except Exception:
        log.exception('Scan failed')
        await bot.send_message(chat_id, '❌ Ошибка проверки. Подробнее в /status. Мониторинг продолжит попытки.')
        return
    found = scan.filtered(repo.get_user(user_id))
    await bot.send_message(chat_id, (
        f'Найдено подходящих квартир: <b>{len(found)}</b>\n'
        + html.escape(summary_text(scan))
        + ('\n⚠️ Список может быть неполным: есть ошибки источников или маршрутов.'
           if any(not s.ok for s in scan.sources) or scan.unrouted else '')
    ), parse_mode='HTML')
    delivered: list[str] = []
    for ad in found[:8]:
        try:
            await send_listing(bot, chat_id, ad)
            delivered.append(ad.key)
        except TelegramRetryAfter as exc:
            log.warning('Telegram rate limited: %s', exc)
            break
        except TelegramBadRequest as exc:
            log.warning('Telegram rejected listing %s: %s', ad.key, exc)
    if seed:
        # Existing matches are not 'new'; do not re-notify them after /watch.
        # If sending failed, that advert remains unmarked to retry later.
        undelivered = {ad.key for ad in found[:8]} - set(delivered)
        repo.mark_seen(user_id, [ad.key for ad in found if ad.key not in undelivered])
        await bot.send_message(chat_id, '🔔 Мониторинг включён. Буду проверять новые объявления '
                                        f'примерно каждые {interval // 60} мин. Для остановки: /stop')


@router.message(CommandStart())
@router.message(Command('help'))
async def start(message: Message) -> None:
    if await _accept_message(message):
        await reply_menu(message, message.from_user.id)


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
        await message.answer('🔕 Мониторинг выключен. Настройки сохранились.')


@router.message(Command('budget'))
async def budget_cmd(message: Message) -> None:
    if not await _accept_message(message):
        return
    parts = (message.text or '').split()
    if len(parts) != 2 or not parts[1].isdigit() or int(parts[1]) > 100_000:
        await message.answer('Формат: /budget 600 (максимум $600 в месяц), /budget 0 (любой бюджет).')
        return
    repo.update_user(message.from_user.id, max_price_usd=int(parts[1]))
    await reply_menu(message, message.from_user.id)


@router.message(Command('rooms'))
async def rooms_cmd(message: Message) -> None:
    if not await _accept_message(message):
        return
    parts = (message.text or '').split()
    if len(parts) != 2 or parts[1] not in ('0', '1', '2', '3', '4'):
        await message.answer('Формат: /rooms 0 (любые), /rooms 1, /rooms 2, /rooms 3, /rooms 4 (4+).')
        return
    repo.update_user(message.from_user.id, rooms=int(parts[1]))
    await reply_menu(message, message.from_user.id)


@router.message(Command('status'))
async def status_cmd(message: Message) -> None:
    if await _accept_message(message):
        await message.answer(status_text(), parse_mode='HTML')


def status_text() -> str:
    rows = repo.statuses()
    statuses = '\n'.join(
        f'{"✅" if r["ok"] else "❌"} <b>{html.escape(r["name"])}</b>: '
        f'{r["total"]} объявл.; {html.escape(r["checked_at"])}'
        + (f'\n   ↳ {html.escape(r["error"][:150])}' if r['error'] else '')
        for r in rows
    ) if rows else 'Ещё не было сканирования. Нажми /search.'
    last_scan = repo.db.execute("SELECT value FROM meta WHERE name='last_scan'").fetchone()
    return (
        '<b>🩺 Состояние бота</b>\n' + statuses + '\n\n'
        f'📍 OSM адресов в локальном индексе: {repo.address_count()}\n'
        f'🚶 Ошибка пешего маршрутизатора: {html.escape(engine.foot.last_error or "нет")}\n'
        f'🗺 Ошибка адресного индекса: {html.escape(engine.addresses.error or "нет")}\n'
        f'🕐 Последнее сканирование: {html.escape(last_scan[0] if last_scan else "ещё не было")}\n'
        'Если источник ❌, это не значит, что свободных квартир нет.'
    )


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
    if await _accept_message(message):
        await message.answer('Пиши /start, /search, /watch, /stop, /budget 600 или /status.')


async def notify_loop(bot: Bot) -> None:
    failure_cycles = {name: 0 for name in SOURCE_NAMES}
    last_error_message = 0.0
    while True:
        await asyncio.sleep(interval)
        subscribers = repo.watchers()
        if not subscribers:
            continue
        try:
            scan = await engine.scan(fresh=True)
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
