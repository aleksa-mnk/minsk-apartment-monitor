"""Strict pedestrian routing via FOSSGIS' foot-profile OSRM backend.

IMPORTANT: /routed-foot/ is required, even though OSRM calls the path /driving/.
No straight-line minutes are ever passed off as walking travel times.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Iterable

import httpx

from models import Listing, WORK_LAT, WORK_LON

log = logging.getLogger(__name__)
OSRM_BASE = 'https://routing.openstreetmap.de/routed-foot'


class RouteUnavailable(RuntimeError):
    pass


def point_key(lat: float, lon: float) -> str:
    return f'{lat:.5f},{lon:.5f}'


class PedestrianRouter:
    def __init__(self, client: httpx.AsyncClient, store, base_url: str = OSRM_BASE):
        self.client, self.store = client, store
        self.base_url = base_url.rstrip('/')
        self.lock = asyncio.Lock()
        self.last_request = 0.0
        self.last_error: str | None = None

    async def _get(self, path: str, params: dict) -> dict:
        # Operator's published limit is at most one request per second.
        async with self.lock:
            delay = 1.1 - (time.monotonic() - self.last_request)
            if delay > 0:
                await asyncio.sleep(delay)
            self.last_request = time.monotonic()
            try:
                resp = await self.client.get(f'{self.base_url}/{path}', params=params, timeout=28)
                resp.raise_for_status()
                data = resp.json()
                if data.get('code') != 'Ok':
                    raise RouteUnavailable(str(data.get('message') or data.get('code')))
                self.last_error = None
                return data
            except (httpx.HTTPError, ValueError, RouteUnavailable) as exc:
                self.last_error = str(exc)[:200]
                raise RouteUnavailable(f'Пешеходный маршрутизатор: {self.last_error}') from exc

    async def _single(self, ad: Listing) -> tuple[float, float | None] | None:
        assert ad.lat is not None and ad.lon is not None
        coords = f'{WORK_LON},{WORK_LAT};{ad.lon},{ad.lat}'
        try:
            obj = await self._get(f'route/v1/driving/{coords}', {'overview': 'false', 'steps': 'false'})
            routes = obj.get('routes') or []
            if not routes:
                return None
            return float(routes[0]['duration']), float(routes[0]['distance'])
        except (KeyError, ValueError, TypeError) as exc:
            raise RouteUnavailable('Неизвестный формат ответа пешеходного маршрута') from exc

    async def measure(self, ads: list[Listing]) -> dict[str, tuple[float, float | None]]:
        """Use cached values; batch-lookup missing points using OSRM Table.

        If the table is unsupported, fall back to direct foot routes with the same
        rate limit. Failure is surfaced, NEVER interpreted as zero-minute trip.
        """
        result: dict[str, tuple[float, float | None]] = {}
        missing_by_point: dict[str, Listing] = {}
        for ad in ads:
            if ad.lat is None or ad.lon is None:
                continue
            point = point_key(ad.lat, ad.lon)
            cached = self.store.get_route(point)
            if cached is not None:
                result[point] = cached
            else:
                missing_by_point[point] = ad
        missing = list(missing_by_point.items())
        for start in range(0, len(missing), 24):
            group = missing[start:start + 24]
            coord_str = f'{WORK_LON},{WORK_LAT};' + ';'.join(f'{ad.lon},{ad.lat}' for _, ad in group)
            try:
                raw = await self._get(f'table/v1/driving/{coord_str}', {
                    'sources': '0', 'destinations': ';'.join(str(i) for i in range(1, len(group) + 1)),
                    'annotations': 'duration',
                })
                durations = raw['durations'][0]
                distances = (raw.get('distances') or [[]])[0]
                if len(durations) != len(group):
                    raise RouteUnavailable('OSRM table returned wrong number of points')
                for i, (point, _) in enumerate(group):
                    if durations[i] is None:
                        continue
                    sec = float(durations[i])
                    meters = float(distances[i]) if i < len(distances) and distances[i] is not None else None
                    if not math.isfinite(sec) or sec < 0:
                        continue
                    result[point] = (sec, meters)
                    self.store.save_route(point, sec, meters)
            except (RouteUnavailable, ValueError, KeyError, TypeError) as exc:
                log.warning('Foot table unavailable, trying direct route(s): %s', exc)
                for point, ad in group:
                    try:
                        value = await self._single(ad)
                        if value is not None:
                            result[point] = value
                            self.store.save_route(point, *value)
                    except RouteUnavailable as route_exc:
                        # Avoid hammering a broken/blocked service for every location.
                        log.warning('Foot route unavailable: %s', route_exc)
                        break
        return result
