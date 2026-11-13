"""Fan out sources, resolve verifiable locations, route on foot and filter.

Source-by-source counters make silent omissions visible in GitHub Actions logs.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from addresses import AddressResolver
from models import Filters, Listing, near_work, matches
from rates import usd_byn_rate
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
    byn_per_usd: float | None = None
    source_counts: dict[str, dict[str, int]] = field(default_factory=dict)

    def filtered(self, flt: Filters) -> list[Listing]:
        return [ad for ad in self.ads if matches(ad, flt, byn_per_usd=self.byn_per_usd)]

    def diagnostics(self, flt: Filters) -> dict[str, dict[str, int]]:
        """Counts for each source in the actual filtering pipeline."""
        result = {item.source: dict(self.source_counts.get(item.source, {})) for item in self.sources}
        for name in result:
            result[name].update(within_walk=0, price_ok=0, matched=0)
        for ad in self.ads:
            stats = result.setdefault(ad.source, {})
            if ad.walk_seconds is None or ad.walk_seconds > flt.walk_minutes * 60:
                continue
            stats['within_walk'] = stats.get('within_walk', 0) + 1
            # Compare in one currency only; this is budget-only (not rooms/owner).
            price_filter = Filters(walk_minutes=flt.walk_minutes, max_price_usd=flt.max_price_usd)
            if not matches(ad, price_filter, byn_per_usd=self.byn_per_usd):
                continue
            stats['price_ok'] = stats.get('price_ok', 0) + 1
            if matches(ad, flt, byn_per_usd=self.byn_per_usd):
                stats['matched'] = stats.get('matched', 0) + 1
        return result


class Engine:
    def __init__(self, store, sources: ListingSources, addresses: AddressResolver, foot: PedestrianRouter):
        self.store = store
        self.sources = sources
        self.addresses = addresses
        self.foot = foot
        self._lock = asyncio.Lock()
        self._last_scan: Scan | None = None
        self._last_time = 0.0

    async def scan(self, *, fresh: bool = False, max_walk_minutes: int = 20) -> Scan:
        """Only one simultaneous scan, so repeated searches don't hammer sites."""
        async with self._lock:
            # The old fixed 2.9 km geocoding index excluded houses when
            # walking limits exceeded 20 min. Keep a margin for nearby streets.
            radius_m = max(2900, max_walk_minutes * 110 + 700)
            page_results, _ = await asyncio.gather(
                self.sources.fetch_all(force=fresh),
                self.addresses.ensure_index(radius_m=radius_m),
            )
            combined: dict[str, Listing] = {}
            stats: dict[str, dict[str, int]] = {}
            for item in page_results:
                self.store.set_source_status(item.source, item.ok, len(item.ads), item.error)
                stats[item.source] = {'fetched': len(item.ads), 'geo_resolved': 0,
                                      'geo_missing': 0, 'near_straight': 0,
                                      'routed': 0}
                for ad in item.ads:
                    combined[ad.key] = ad

            prefiltered: list[Listing] = []
            missing_geo = 0
            for ad in combined.values():
                if ad.lat is None or ad.lon is None:
                    pt = self.addresses.resolve(ad.address)
                    if pt:
                        ad = ad.with_geo(*pt)
                        stats[ad.source]['geo_resolved'] += 1
                if ad.lat is None or ad.lon is None:
                    missing_geo += 1
                    stats[ad.source]['geo_missing'] += 1
                    continue
                if near_work(ad, max_walk_minutes=max_walk_minutes):
                    prefiltered.append(ad)
                    stats[ad.source]['near_straight'] += 1

            # Fetch only an official USD/BYN rate. If unavailable, any BYN
            # listing remains excluded under a USD budget rather than guessed.
            conversion = None
            if any(ad.price is not None and ad.currency == 'BYN' for ad in combined.values()):
                client = getattr(self.sources, 'client', None)
                if client is not None:
                    conversion = await usd_byn_rate(client, self.store)

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
                stats[ad.source]['routed'] += 1
            done.sort(key=lambda x: (x.walk_seconds if x.walk_seconds is not None else 1e10,
                                      x.price if x.price is not None else 1e10))
            report = Scan(
                ads=done, sources=page_results, fetched=len(combined),
                geo_missing=missing_geo, candidates=len(prefiltered),
                unrouted=unrouted, foot_error=self.foot.last_error if unrouted else None,
                byn_per_usd=conversion, source_counts=stats,
            )
            self.store.set_meta('last_scan', __import__('models').now_utc())
            self._last_scan = report
            return report
