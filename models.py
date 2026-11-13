"""Typed models and geographical helpers for Minsk walk-to-work rentals."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from math import asin, cos, radians, sin, sqrt
from typing import Optional

WORK_ADDRESS = 'Минск, ул. Мележа, 5, корп. 1'
WORK_LAT = 53.940579
WORK_LON = 27.597341
DEFAULT_WALK_MINUTES = 20
MIN_WALK_MINUTES = 5
MAX_WALK_MINUTES = 45
MAX_WALK_SECONDS = DEFAULT_WALK_MINUTES * 60
# Conservative straight-line prefilter; the final decision ALWAYS uses a walk route.
PREFILTER_METERS = 2700


@dataclass(frozen=True)
class Listing:
    key: str
    source: str
    url: str
    title: str
    address: str
    price: int | None = None
    currency: str = 'USD'
    rooms: int | None = None
    lat: float | None = None
    lon: float | None = None
    owner: bool | None = None
    image: str | None = None
    published_at: str | None = None
    walk_seconds: float | None = None
    walk_meters: float | None = None

    def with_geo(self, lat: float, lon: float) -> 'Listing':
        return replace(self, lat=lat, lon=lon)

    def with_walk(self, seconds: float, meters: float | None) -> 'Listing':
        return replace(self, walk_seconds=seconds, walk_meters=meters)


@dataclass(frozen=True)
class Filters:
    max_price_usd: int = 0
    rooms: int = 0  # 0 = any, 4 = 4+
    owner_only: bool = False
    watching: bool = False
    walk_minutes: int = DEFAULT_WALK_MINUTES


def valid_minsk_coordinate(lat: float, lon: float) -> bool:
    # Enough to reject obviously invalid or transposed pairs before exact routing.
    return 53.60 < lat < 54.20 and 27.10 < lon < 28.10


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * r * asin(min(1.0, sqrt(a)))


def near_work(ad: Listing, max_walk_minutes: int = DEFAULT_WALK_MINUTES) -> bool:
    # Increase the prefilter for larger user-selected limits. The extra margin
    # avoids discarding a plausible walking route prematurely.
    prefilter_m = max(PREFILTER_METERS, max_walk_minutes * 110 + 500)
    return ad.lat is not None and ad.lon is not None and (
        valid_minsk_coordinate(ad.lat, ad.lon)
        and haversine_m(WORK_LAT, WORK_LON, ad.lat, ad.lon) <= prefilter_m
    )


def matches(ad: Listing, flt: Filters) -> bool:
    if ad.walk_seconds is None or ad.walk_seconds > flt.walk_minutes * 60:
        return False
    if flt.max_price_usd:
        if ad.currency != 'USD' or ad.price is None or ad.price > flt.max_price_usd:
            return False
    if flt.rooms:
        if ad.rooms is None:
            return False
        if flt.rooms == 4 and ad.rooms < 4:
            return False
        if flt.rooms != 4 and ad.rooms != flt.rooms:
            return False
    if flt.owner_only and ad.owner is not True:
        return False
    return True


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')
