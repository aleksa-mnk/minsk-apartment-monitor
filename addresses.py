"""Normalize Belarusian street/house addresses and use a cached local OSM address index.

The index is collected in a SINGLE Overpass query at most once per week.
We do not bulk-query the public Nominatim geocoding endpoint.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata

import httpx

from models import WORK_LAT, WORK_LON, valid_minsk_coordinate

log = logging.getLogger(__name__)
OVERPASS_SERVERS = (
    'https://overpass.kumi.systems/api/interpreter',
    'https://overpass-api.de/api/interpreter',
)


def _clean(value: str) -> str:
    value = unicodedata.normalize('NFKC', str(value or '')).lower()
    value = value.replace('ё', 'е').replace('ў', 'у').replace('і', 'и')
    return re.sub(r'\s+', ' ', value).strip()


def street_key(value: str) -> str:
    value = _clean(value)
    value = re.sub(r'(?<!\w)(?:город|г\.|минск|беларусь)(?!\w)', ' ', value)
    # Convert a prefix/suffix street type to a common form. Work for both RU/BY.
    value = re.sub(
        r'(?<!\w)(?:улица|ул\.?|вул\.?|проспект|пр\-?т|просп\.?|переулок|пер\.?|проезд|пр\-?д|бульвар|б\-?р|площадь|пл\.)(?!\w)',
        ' ', value,
    )
    value = re.sub(r'[^a-zа-я0-9 ]+', ' ', value)
    return re.sub(r'\s+', ' ', value).strip()


def house_key(value: str) -> str:
    value = _clean(value).replace(' ', '')
    value = value.replace('корпус', 'к').replace('корп.', 'к').replace('корп', 'к')
    value = re.sub(r'(?<=\d)/(?=\d)', 'к', value)
    value = re.sub(r'[^\da-zа-я]', '', value)
    return value


# A number following a street may include corpus, e.g. 5к1, 5/1, 5 корп. 1.
HOUSE_RE = re.compile(r'(?<!\w)(\d+[а-яa-z]?(?:\s*,?\s*(?:[/к]|корп(?:ус)?\.?)\s*\d+[а-яa-z]?)?)(?!\w)', re.I)


def address_key(text: str) -> tuple[str, str] | None:
    text = _clean(text)
    text = re.sub(r'\b(?:республика беларусь|беларусь|город минск|г\.\s*минск|минск)\b\s*,?', ' ', text)
    # Most portals show a street first (before the house number).
    m = HOUSE_RE.search(text)
    if not m:
        return None
    street = street_key(text[:m.start()])
    street = re.sub(r'\b(?:мкр|район|сельсовет|д|дом|строение|ст)\b', ' ', street).strip()
    # Avoid treating a district, streetless building, or bare 'Минск' as an address.
    if not street or len(street) < 3 or len(street) > 64:
        return None
    number = house_key(m.group(1))
    return (street, number) if number else None


def build_osm_index(payload: dict) -> dict[str, tuple[float, float]]:
    result: dict[str, tuple[float, float]] = {}
    for el in payload.get('elements', []):
        if not isinstance(el, dict):
            continue
        tags = el.get('tags') or {}
        street = street_key(tags.get('addr:street', ''))
        house = house_key(tags.get('addr:housenumber', ''))
        point = el.get('center') or el
        try:
            lat, lon = float(point.get('lat')), float(point.get('lon'))
        except (ValueError, TypeError, AttributeError):
            continue
        if street and house and valid_minsk_coordinate(lat, lon):
            result[f'{street}|{house}'] = (lat, lon)
    return result


class AddressResolver:
    def __init__(self, client: httpx.AsyncClient, store, *, refresh_days: int = 7):
        self.client = client
        self.store = store
        self.refresh_days = refresh_days
        self.error: str | None = None
        self._lock = asyncio.Lock()
        self._next_attempt = 0.0

    async def ensure_index(self, *, radius_m: int = 2900) -> bool:
        # A wider user-selected walking limit needs a wider address index.
        # The previous fixed 2.9 km OSM index silently lost flats at 30-45 min.
        radius_m = max(2900, min(6000, int(radius_m)))
        updated = self.store.get_meta_float('osm_index_updated')
        cached_radius = self.store.get_meta_float('osm_index_radius_m')
        if updated and time.time() - updated < self.refresh_days * 86400 and cached_radius >= radius_m:
            return True
        if time.monotonic() < self._next_attempt:
            return self.store.address_count() > 0
        async with self._lock:
            updated = self.store.get_meta_float('osm_index_updated')
            cached_radius = self.store.get_meta_float('osm_index_radius_m')
            if updated and time.time() - updated < self.refresh_days * 86400 and cached_radius >= radius_m:
                return True
            if time.monotonic() < self._next_attempt:
                return self.store.address_count() > 0
            # Limit queries to the ~20-min walking area, plus a safety margin.
            query = (
                '[out:json][timeout:35];'
                '(nwr["addr:street"]["addr:housenumber"]'
                f'(around:{radius_m},{WORK_LAT},{WORK_LON}););out center tags;'
            )
            errors = []
            for url in OVERPASS_SERVERS:
                try:
                    resp = await self.client.post(url, data={'data': query}, timeout=55)
                    resp.raise_for_status()
                    data = resp.json()
                    index = build_osm_index(data)
                    if len(index) < 30:
                        raise ValueError(f'слишком мало адресов OSM: {len(index)}')
                    self.store.replace_addresses(index)
                    self.store.set_meta('osm_index_updated', str(time.time()))
                    self.store.set_meta('osm_index_radius_m', str(radius_m))
                    self.error = None
                    log.info('OSM address index refreshed: %s exact addresses', len(index))
                    return True
                except (httpx.HTTPError, ValueError) as exc:
                    errors.append(f'{url}: {str(exc)[:100]}')
            self.error = ' | '.join(errors)
            self._next_attempt = time.monotonic() + 6 * 3600
            log.warning('OSM address index unavailable; keeping old cache: %s', self.error)
            return self.store.address_count() > 0

    def resolve(self, text: str) -> tuple[float, float] | None:
        key = address_key(text)
        if not key:
            return None
        return self.store.lookup_address(f'{key[0]}|{key[1]}')
