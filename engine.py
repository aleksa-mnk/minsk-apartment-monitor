"""Fan out sources, resolve exact house locations, verify foot routes, then filter."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from addresses import AddressResolver
from models import Filters, Listing, near_work, matches
from sources import ListingSources, SourceResult
from walking import PedestrianRouter, point_key

log = logging.getLogger(__name__)


@dataclass
class Scan:
    ads: list[Listing]
    sources: list[SourceResult]
    fetched: int
    geo_missing: int
    candidates: int
    unrouted: int
    foot_error: str | None

    def filtered(self, flt: Filters) -> list[Listing]:
        return [ad for ad in self.ads if matches(ad, flt)]


class Engine:
    def __init__(self, store, sources: ListingSources, addresses: AddressResolver, foot: PedestrianRouter):
        self.store = store
        self.sources = sources
        self.addresses = addresses
        self.foot = foot
        self._lock = asyncio.Lock()
        self._last_scan: Scan | None = None
        self._last_time = 0.0

    async def scan(self, *, fresh: bool = False) -> Scan:
        """Only one simultaneous external scan, so repeated /search cannot hammer portals."""
        async with self._lock:
            # Try the address index in parallel with site downloads. An old index
            # remains available even if Overpass is blocked.
            page_results, _ = await asyncio.gather(
                self.sources.fetch_all(force=fresh), self.addresses.ensure_index(),
            )
            combined: dict[str, Listing] = {}
            for item in page_results:
                self.store.set_source_status(item.source, item.ok, len(item.ads), item.error)
                for ad in item.ads:
                    combined[ad.key] = ad
            prefiltered: list[Listing] = []
            missing_geo = 0
            for ad in combined.values():
                if ad.lat is None or ad.lon is None:
                    pt = self.addresses.resolve(ad.address)
                    if pt:
                        ad = ad.with_geo(*pt)
                if ad.lat is None or ad.lon is None:
                    missing_geo += 1
                    continue
                if near_work(ad):
                    prefiltered.append(ad)
            routes = await self.foot.measure(prefiltered)
            done: list[Listing] = []
            unrouted = 0
            for ad in prefiltered:
                assert ad.lat is not None and ad.lon is not None
                route = routes.get(point_key(ad.lat, ad.lon))
                if route is None:
                    unrouted += 1
                    continue
                done.append(ad.with_walk(*route))
            done.sort(key=lambda x: (x.walk_seconds if x.walk_seconds is not None else 1e10,
                                      x.price if x.price is not None else 1e10))
            report = Scan(
                ads=done, sources=page_results, fetched=len(combined),
                geo_missing=missing_geo, candidates=len(prefiltered),
                unrouted=unrouted, foot_error=self.foot.last_error if unrouted else None,
            )
            self.store.set_meta('last_scan', __import__('models').now_utc())
            self._last_scan = report
            return report
