"""Independent adapters for long-term Minsk rentals: Kufar, Realt and Onliner.

The providers' search endpoints are not official supported public APIs. A site
may block requests or alter its schema; the bot reports source errors and keeps
working with the sources that still respond. Never bypass CAPTCHA/protection.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from models import Listing, valid_minsk_coordinate
from photo_delivery import is_https_photo_url

log = logging.getLogger(__name__)
KUFAR_API = 'https://api.kufar.by/search-api/v2/search/rendered-paginated'
KUFAR_HTML = 'https://re.kufar.by/l/minsk/snyat/kvartiru'
REALT_BASE = 'https://realt.by'
REALT_URLS = ('https://realt.by/rent-flat-for-long/', 'https://realt.by/rent/flat-for-long/')
ONLINER_APIS = ('https://r.onliner.by/sdk/search/apartments', 'https://ak.api.onliner.by/apartments')
SOURCE_NAMES = ('Kufar', 'Realt', 'Onliner')


class SourceUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceResult:
    source: str
    ads: list[Listing]
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def number(value: Any) -> float | None:
    try:
        if isinstance(value, bool) or value in (None, ''):
            return None
        out = float(str(value).replace(' ', '').replace(',', '.'))
        return out if 0 <= out <= 1_000_000_000 else None
    except (ValueError, TypeError):
        return None


def parse_pair(a: Any, b: Any) -> tuple[float, float] | None:
    lat, lon = number(a), number(b)
    if lat is None or lon is None:
        return None
    if valid_minsk_coordinate(lat, lon):
        return lat, lon
    if valid_minsk_coordinate(lon, lat):
        return lon, lat
    return None


def coordinates(obj: dict[str, Any]) -> tuple[float, float] | None:
    """Find coordinates from documented or common vendor shape.

    Only read location-like fields. Do not scan arbitrary numeric fields: house
    numbers and prices must not become fake geographical coordinates.
    """
    variants: list[Any] = [obj]
    for key in ('location', 'coordinates', 'coordinate', 'geo', 'geoLocation', 'map', 'point', 'position', 'geopoint'):
        val = obj.get(key)
        if val is not None:
            variants.append(val)
            if isinstance(val, dict):
                for k2 in ('coords', 'coordinates', 'point', 'center', 'location'):
                    if k2 in val:
                        variants.append(val[k2])
    for v in variants:
        if isinstance(v, dict):
            for la, lo in (('latitude', 'longitude'), ('lat', 'lon'), ('lat', 'lng'), ('lat', 'long'), ('y', 'x')):
                hit = parse_pair(v.get(la), v.get(lo))
                if hit:
                    return hit
        elif isinstance(v, (list, tuple)) and len(v) >= 2:
            hit = parse_pair(v[0], v[1])
            if hit:
                return hit
        elif isinstance(v, str):
            m = re.fullmatch(r'\s*([\d.,]+)\s*[,; ]\s*([\d.,]+)\s*', v)
            if m:
                hit = parse_pair(m.group(1), m.group(2))
                if hit:
                    return hit
    return None


def permitted_url(raw: Any, source: str) -> str | None:
    text = str(raw or '').strip()
    if text.startswith('/'):
        base = {'Kufar': 'https://re.kufar.by', 'Realt': REALT_BASE, 'Onliner': 'https://r.onliner.by'}[source]
        text = urljoin(base, text)
    parsed = urlsplit(text)
    if parsed.scheme != 'https':
        return None
    host = (parsed.hostname or '').lower()
    allowed = {'Kufar': ('kufar.by',), 'Realt': ('realt.by',), 'Onliner': ('onliner.by',)}[source]
    if not any(host == name or host.endswith('.' + name) for name in allowed):
        return None
    return text.split('#', 1)[0]


def first_photo(raw: Any, source: str, depth: int = 0) -> str | None:
    """Extract a public HTTPS photo from common marketplace fields.

    The three sites use slightly different photo formats, including nested
    dictionaries and lists. Do not mistake an object for a Telegram URL.
    """
    if depth > 4:
        return None
    if isinstance(raw, str):
        candidate = raw.strip()
        if candidate.startswith('//'):
            candidate = 'https:' + candidate
        elif candidate.startswith('/'):
            host = {'Kufar': 'https://re.kufar.by', 'Realt': REALT_BASE,
                    'Onliner': 'https://r.onliner.by'}[source]
            candidate = urljoin(host, candidate)
        return candidate if is_https_photo_url(candidate) else None
    if isinstance(raw, dict):
        for key in ('url', 'photoUrl', 'photo_url', 'original', 'large', 'big',
                    'src', 'image', 'photo', 'medium', 'preview', 'thumbnail',
                    'small', 'photos', 'images'):
            if key in raw:
                found = first_photo(raw[key], source, depth + 1)
                if found:
                    return found
    if isinstance(raw, (list, tuple)):
        for item in raw[:10]:
            found = first_photo(item, source, depth + 1)
            if found:
                return found
    return None


def room_number(raw: Any) -> int | None:
    if isinstance(raw, int) and 1 <= raw <= 10:
        return raw
    text = str(raw or '').lower()
    m = re.search(r'(?<!\d)([1-9])\s*(?:[_ -]?(?:room|rooms|комн))', text)
    if not m:
        m = re.search(r'^\s*([1-9])\s*$', text)
    if m:
        return int(m.group(1))
    if 'студ' in text or 'studio' in text:
        return 1
    return None


def _param(params: Any, *names: str) -> str:
    if isinstance(params, dict):
        for n in names:
            if params.get(n):
                return str(params[n])
    if not isinstance(params, list):
        return ''
    for name in names:
        for item in params:
            if isinstance(item, dict) and item.get('p') == name:
                val = item.get('vl') or item.get('v')
                if val not in (None, '', []):
                    return str(val)
    return ''


def _value_as_address(raw: Any) -> str:
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, dict):
        for key in ('full', 'formatted', 'text', 'value', 'address', 'name'):
            if isinstance(raw.get(key), str) and raw[key].strip():
                return raw[key].strip()
    return ''


def _json_arrays(root: Any, keys: set[str], marker_fields: set[str]) -> list[dict]:
    """Select the largest plausible listing array from Next.js JSON."""
    candidates: list[list[dict]] = []

    def walk(obj: Any, depth: int = 0) -> None:
        if depth > 16:
            return
        if isinstance(obj, dict):
            for key, val in obj.items():
                if key in keys and isinstance(val, list):
                    entries = [x for x in val if isinstance(x, dict)]
                    if entries and any(any(field in x for field in marker_fields) for x in entries[:4]):
                        candidates.append(entries)
                if isinstance(val, (dict, list)):
                    walk(val, depth + 1)
        elif isinstance(obj, list):
            # Do not descend through giant arrays of non-data primitives.
            for item in obj[:800]:
                if isinstance(item, (dict, list)):
                    walk(item, depth + 1)

    walk(root)
    return max(candidates, key=len) if candidates else []


def _next_data(html: str) -> Any:
    soup = BeautifulSoup(html, 'html.parser')
    script = soup.find('script', id='__NEXT_DATA__')
    if not script:
        return None
    try:
        return json.loads(script.string or script.get_text())
    except (json.JSONDecodeError, TypeError):
        return None


def parse_kufar(payload: Any) -> list[Listing]:
    if not isinstance(payload, dict):
        raise SourceUnavailable('Kufar: неверный JSON')
    ads = payload.get('ads')
    if not isinstance(ads, list):
        ads = _json_arrays(payload, {'ads', 'items', 'results'}, {'ad_id', 'ad_link', 'subject', 'price_usd'})
    if not isinstance(ads, list):
        raise SourceUnavailable('Kufar: изменился формат ответа')
    found: list[Listing] = []
    for obj in ads:
        if not isinstance(obj, dict):
            continue
        ad_id = obj.get('ad_id') or obj.get('id')
        if not ad_id:
            continue
        url = permitted_url(obj.get('ad_link') or obj.get('url') or f'https://re.kufar.by/vi/{ad_id}', 'Kufar')
        if not url:
            continue
        params = obj.get('ad_parameters') or obj.get('parameters') or []
        street = _param(params, 'address', 're_address', 'street', 're_street', 'rent_address')
        house = _param(params, 'house_number', 'house', 're_house')
        loc = _value_as_address(obj.get('address')) or _value_as_address(obj.get('location'))
        if not loc:
            loc = (street + (f', {house}' if house and house not in street else '')).strip(', ')
        if not loc:
            loc = _param(params, 'district', 'region', 'area') or 'Адрес не указан'
        usd_cents = number(obj.get('price_usd') or obj.get('priceUsd'))
        byn_cents = number(obj.get('price_byn') or obj.get('priceByn'))
        price = round(usd_cents / 100) if usd_cents else round(byn_cents / 100) if byn_cents else None
        curr = 'USD' if usd_cents else 'BYN'
        owner_label = _param(params, 'seller_type', 'owner_type', 'seller').lower()
        owner = (False if obj.get('company_ad') is True or 'агент' in owner_label
                 else True if 'собственник' in owner_label or 'владелец' in owner_label
                 else None)
        room = room_number(_param(params, 'rooms', 'rms', 'rooms_count')) or room_number(obj.get('subject'))
        title = str(obj.get('subject') or obj.get('title') or 'Квартира в Минске')[:130]
        image = first_photo(obj.get('images') or obj.get('photo') or obj.get('image'), 'Kufar')
        if image is None:
            for pic in (obj.get('images') or [])[:5]:
                if isinstance(pic, dict) and pic.get('path'):
                    image = first_photo('https://rms.kufar.by/v1/list_thumbs_2x/'
                                        + str(pic['path']).lstrip('/'), 'Kufar')
                    if image:
                        break
        pt = coordinates(obj)
        found.append(Listing(
            key=f'kufar:{ad_id}', source='Kufar', url=url,
            title=title, address=loc, price=price, currency=curr, rooms=room,
            lat=pt[0] if pt else None, lon=pt[1] if pt else None, owner=owner,
            image=image, published_at=str(obj.get('list_time') or '') or None,
        ))
    return found


def parse_onliner(payload: Any) -> list[Listing]:
    if not isinstance(payload, dict) or not isinstance(payload.get('apartments'), list):
        raise SourceUnavailable('Onliner: изменился JSON API')
    found: list[Listing] = []
    for obj in payload['apartments']:
        if not isinstance(obj, dict) or not obj.get('id'):
            continue
        rent_type = str(obj.get('rent_type') or '')
        if rent_type in ('room', 'room_only', 'part') or 'house' in rent_type:
            continue
        url = permitted_url(obj.get('url') or f'https://r.onliner.by/ak/apartments/{obj["id"]}', 'Onliner')
        if not url:
            continue
        location = obj.get('location') or {}
        if not isinstance(location, dict):
            location = {}
        addr = (location.get('address') or location.get('user_address') or obj.get('address') or '')
        if isinstance(addr, str) and addr and 'минск' not in addr.lower() and 'беларус' not in addr.lower():
            addr = 'Минск, ' + addr
        pt = coordinates(location) or coordinates(obj)
        price_obj = obj.get('price') or {}
        usd = None
        if isinstance(price_obj, dict):
            converted = price_obj.get('converted') or {}
            if isinstance(converted, dict):
                usd = number((converted.get('USD') or {}).get('amount'))
            if usd is None and price_obj.get('currency') == 'USD':
                usd = number(price_obj.get('amount'))
        rooms = room_number(rent_type) or room_number(obj.get('title'))
        found.append(Listing(
            key=f'onliner:{obj["id"]}', source='Onliner', url=url,
            title=f'{rooms}-комнатная квартира' if rooms else 'Квартира в Минске',
            address=str(addr) or 'Адрес не указан',
            price=int(round(usd)) if usd else None, currency='USD', rooms=rooms,
            lat=pt[0] if pt else None, lon=pt[1] if pt else None,
            owner=((obj.get('contact') or {}).get('owner') if isinstance(obj.get('contact'), dict) else None),
            image=first_photo(obj.get('photo') or obj.get('photos') or obj.get('images')
                              or obj.get('image'), 'Onliner'),
            published_at=obj.get('created_at'),
        ))
    return found


def _realt_price(obj: dict) -> tuple[int | None, str]:
    for key in ('priceUsd', 'price_usd'):
        if number(obj.get(key)):
            return round(number(obj[key])), 'USD'
    price = number(obj.get('price'))
    if price:
        raw_cur = obj.get('priceCurrency') or obj.get('currency')
        mapping = {'840': 'USD', '933': 'BYN', '978': 'EUR', 'USD': 'USD', 'BYN': 'BYN', 'EUR': 'EUR'}
        return round(price), mapping.get(str(raw_cur).upper(), 'BYN')
    for key in ('priceByn', 'price_byn'):
        if number(obj.get(key)):
            return round(number(obj[key])), 'BYN'
    return None, 'BYN'


def parse_realt(payload: Any) -> list[Listing]:
    ads = _json_arrays(payload, {'objects', 'items', 'results', 'data'},
                        {'code', 'uuid', 'priceCurrency', 'priceUsd'})
    if not ads:
        raise SourceUnavailable('Realt: не найдены объявления в JSON страницы')
    result = []
    for obj in ads:
        if not isinstance(obj, dict):
            continue
        ad_id = obj.get('code') or obj.get('uuid') or obj.get('id')
        if not ad_id:
            continue
        link = permitted_url(obj.get('url') or obj.get('link') or f'/rent-flat-for-long/object/{ad_id}/', 'Realt')
        if not link:
            continue
        city = obj.get('townName') or obj.get('town') or obj.get('city') or ''
        if isinstance(city, str) and city and 'минск' not in city.lower():
            continue
        raw_address = obj.get('address') or obj.get('fullAddress') or obj.get('street') or ''
        address = _value_as_address(raw_address)
        if not address:
            street = str(obj.get('streetName') or obj.get('street') or '').strip()
            house = str(obj.get('houseNumber') or obj.get('house') or '').strip()
            address = street + ((', ' + house) if house else '')
        if address and 'минск' not in address.lower():
            address = 'Минск, ' + address
        pt = coordinates(obj)
        price, currency = _realt_price(obj)
        rooms = room_number(obj.get('rooms') or obj.get('roomsCount'))
        title = str(obj.get('title') or obj.get('seoTitle') or (
            f'{rooms}-комнатная квартира' if rooms else 'Квартира в Минске'))
        image = first_photo(obj.get('mainPhoto') or obj.get('photo') or obj.get('image')
                            or obj.get('photos') or obj.get('images') or obj.get('photoUrl'), 'Realt')
        is_owner = obj.get('isOwner')
        owner = is_owner if isinstance(is_owner, bool) else None
        result.append(Listing(
            key=f'realt:{ad_id}', source='Realt', url=link, title=title[:130],
            address=address or 'Адрес не указан', price=price, currency=currency,
            rooms=rooms, lat=pt[0] if pt else None, lon=pt[1] if pt else None,
            owner=owner, image=image, published_at=str(obj.get('updatedAt') or obj.get('createdAt') or '') or None,
        ))
    return result


def _parse_realt_cards(html: str) -> list[Listing]:
    """Limited fail-safe for legacy SSR pages (may return no address and be skipped)."""
    soup = BeautifulSoup(html, 'html.parser')
    found: dict[str, Listing] = {}
    for link in soup.select('a[href*="rent-flat-for-long/object/"]'):
        href = permitted_url(link.get('href'), 'Realt')
        if not href:
            continue
        m = re.search(r'/object/(\d+)/?', href)
        if not m:
            continue
        block = link.find_parent('article') or link.find_parent('li') or link.parent
        text = block.get_text(' ', strip=True)[:500] if block else ''
        # Never invent a street address or walking time from unstructured text.
        address_match = re.search(r'(?:ул\.|улица|проспект|пр\-т)\s*[а-яё\- ]+[, ]+\d+[а-яёк/\d]*', text, re.I)
        if not address_match:
            continue
        amount = re.search(r'([\d\s]+)\s*\$', text)
        result = Listing(
            key=f'realt:{m.group(1)}', source='Realt', url=href,
            title=text[:120] or 'Квартира в Минске', address='Минск, ' + address_match.group(0),
            price=int(amount.group(1).replace(' ', '')) if amount else None,
            currency='USD', rooms=room_number(text),
        )
        found[result.key] = result
    return list(found.values())


class ListingSources:
    def __init__(self, client: httpx.AsyncClient, *, pages: int = 3):
        self.client, self.pages = client, min(5, max(1, pages))
        self.cache: tuple[float, list[SourceResult]] | None = None
        self.lock = asyncio.Lock()

    async def _get(self, url: str, *, params: dict | None = None, accept_json: bool = False) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(2):
            try:
                resp = await self.client.get(url, params=params, headers={
                    'Accept': 'application/json' if accept_json else 'text/html,application/xhtml+xml',
                }, timeout=24)
                if resp.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                    await asyncio.sleep(2)
                    continue
                resp.raise_for_status()
                return resp
            except httpx.HTTPError as exc:
                last = exc
                if attempt == 0:
                    await asyncio.sleep(1)
        raise SourceUnavailable(f'{urlsplit(url).hostname}: {str(last)[:130]}')

    async def _kufar(self) -> list[Listing]:
        all_ads: dict[str, Listing] = {}
        params = {
            'lang': 'ru', 'size': '80', 'sort': 'lst.d', 'cat': '1010',
            'typ': 'let', 'rnt': '1', 'rgn': '6', 'ar': '18', 'cur': 'USD',
        }
        api_error = None
        cursor = None
        try:
            for _ in range(self.pages):
                query = dict(params)
                if cursor:
                    query['cursor'] = cursor
                data = (await self._get(KUFAR_API, params=query, accept_json=True)).json()
                batch = parse_kufar(data)
                for item in batch:
                    all_ads[item.key] = item
                pages = (data.get('pagination') or {}).get('pages') or []
                next_pages = [p for p in pages if isinstance(p, dict) and p.get('label') == 'next']
                nxt = next_pages[0].get('token') if next_pages else None
                if not nxt or nxt == cursor:
                    break
                cursor = nxt
                await asyncio.sleep(1.2)
            if all_ads:
                return list(all_ads.values())
            api_error = 'API Kufar вернул 0 объявлений (проверить фильтры/схему)'
        except (SourceUnavailable, ValueError, TypeError) as exc:
            api_error = str(exc)
        # Independent path if private search API is blocked or changes.
        try:
            html = (await self._get(KUFAR_HTML)).text
            data = _next_data(html)
            if data is None:
                raise SourceUnavailable('Kufar HTML: отсутствует __NEXT_DATA__')
            ads = parse_kufar(data)
            if not ads:
                raise SourceUnavailable('Kufar HTML: объявления не обнаружены')
            return ads
        except SourceUnavailable as exc:
            raise SourceUnavailable(f'API: {api_error}; HTML: {exc}') from exc

    async def _onliner(self) -> list[Listing]:
        reasons = []
        for url in ONLINER_APIS:
            all_ads: dict[str, Listing] = {}
            try:
                for pg in range(1, self.pages + 1):
                    data = (await self._get(url, params={'page': str(pg)}, accept_json=True)).json()
                    ads = parse_onliner(data)
                    for ad in ads:
                        all_ads[ad.key] = ad
                    pageinfo = data.get('page') or {}
                    if not ads or (isinstance(pageinfo, dict) and number(pageinfo.get('last')) is not None
                                   and pg >= int(pageinfo['last'])):
                        break
                    await asyncio.sleep(1.2)
                if all_ads:
                    return list(all_ads.values())
                reasons.append(f'{url}: нет объявлений')
            except (SourceUnavailable, ValueError, TypeError) as exc:
                reasons.append(str(exc))
        raise SourceUnavailable('Onliner: ' + '; '.join(reasons)[:220])

    async def _realt(self) -> list[Listing]:
        reasons = []
        for base in REALT_URLS:
            results: dict[str, Listing] = {}
            try:
                for pg in range(1, self.pages + 1):
                    url = base if pg == 1 else base + '?page=' + str(pg)
                    html = (await self._get(url)).text
                    data = _next_data(html)
                    if data is not None:
                        try:
                            ads = parse_realt(data)
                        except SourceUnavailable:
                            ads = _parse_realt_cards(html)
                    else:
                        ads = _parse_realt_cards(html)
                    if not ads:
                        if pg == 1:
                            raise SourceUnavailable('Realt: нет читаемых карточек; изменилась разметка или блокировка')
                        break
                    for ad in ads:
                        results[ad.key] = ad
                    await asyncio.sleep(1.2)
                if results:
                    return list(results.values())
            except SourceUnavailable as exc:
                reasons.append(str(exc))
        raise SourceUnavailable('Realt: ' + ' | '.join(reasons)[:230])

    async def fetch_all(self, *, force: bool = False) -> list[SourceResult]:
        if not force and self.cache and time.monotonic() - self.cache[0] < 120:
            return self.cache[1]
        async with self.lock:
            if not force and self.cache and time.monotonic() - self.cache[0] < 120:
                return self.cache[1]

            async def once(name: str, method) -> SourceResult:
                try:
                    return SourceResult(source=name, ads=await method())
                except Exception as exc:
                    # Isolate source failures, retaining diagnostics for /status.
                    log.exception('%s fetch error', name)
                    return SourceResult(source=name, ads=[], error=f'{type(exc).__name__}: {str(exc)[:240]}')

            results = await asyncio.gather(
                once('Kufar', self._kufar),
                once('Realt', self._realt),
                once('Onliner', self._onliner),
            )
            self.cache = (time.monotonic(), results)
            return results
