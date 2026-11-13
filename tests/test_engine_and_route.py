import asyncio

import httpx

from addresses import AddressResolver
from engine import Engine
from models import Filters, Listing, MAX_WALK_SECONDS, WORK_LAT, WORK_LON, matches, near_work
from sources import SourceResult
from store import Store
from walking import PedestrianRouter, point_key


def test_strict_walking_filter():
    ad = Listing('A', 'Onliner', 'https://ak.onliner.by/apartments/1', 'Квартира', 'Мележа',
                 lat=WORK_LAT, lon=WORK_LON, price=400, currency='USD')
    assert not matches(ad, Filters())  # Without pedestrian route, never publish.
    assert matches(ad.with_walk(20 * 60, 1500), Filters(max_price_usd=400))
    assert not matches(ad.with_walk(20 * 60 + 1, 1501), Filters())
    assert not matches(ad.with_walk(10 * 60, 800), Filters(owner_only=True))
    assert near_work(ad)
    assert not near_work(ad.with_geo(53.97, 27.64))


def test_router_osrm_foot_table():
    calls = []

    def responder(request):
        calls.append(request)
        if '/routed-foot/table/v1/driving/' in str(request.url):
            return httpx.Response(200, json={'code': 'Ok', 'durations': [[600.0]], 'distances': [[820.0]]})
        return httpx.Response(400)

    async def run():
        store = Store(':memory:')
        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
            router = PedestrianRouter(client, store)
            ad = Listing('A', 'Onliner', 'https://ak.onliner.by/apartments/1', 'Квартира',
                         'Мележа', lat=53.941, lon=27.598)
            one = await router.measure([ad])
            assert one[point_key(ad.lat, ad.lon)] == (600, 820)
            two = await router.measure([ad])
            assert one == two
            assert len(calls) == 1  # cached
            assert '/routed-foot/' in str(calls[0].url)
        store.close()

    asyncio.run(run())


def test_engine_partial_provider_outage_and_geo_index():
    class FakeSources:
        async def fetch_all(self, force=False):
            return [
                SourceResult('Kufar', [Listing('k:1', 'Kufar', 'https://re.kufar.by/vi/1',
                                               'Квартира', 'Минск, ул. Мележа, 5к1')]),
                SourceResult('Realt', [], error='HTTP 403'),
                SourceResult('Onliner', [Listing('o:1', 'Onliner', 'https://ak.onliner.by/apartments/1',
                                                'Квартира', 'Адрес не указан')]),
            ]

    class FakeAddress:
        error = None
        async def ensure_index(self, *, radius_m=2900):
            return True
        def resolve(self, text):
            return (WORK_LAT, WORK_LON) if 'Мележа' in text else None

    class FakeFoot:
        last_error = None
        async def measure(self, ads):
            return {point_key(ad.lat, ad.lon): (0.0, 0.0) for ad in ads}

    async def run():
        repo = Store(':memory:')
        engine = Engine(repo, FakeSources(), FakeAddress(), FakeFoot())
        scan = await engine.scan()
        assert len(scan.ads) == 1
        assert scan.fetched == 2
        assert scan.geo_missing == 1
        assert len(scan.filtered(Filters())) == 1
        statuses = repo.statuses()
        assert any(s['name'] == 'Realt' and not s['ok'] for s in statuses)
        repo.close()

    asyncio.run(run())


def test_store_dedupe():
    repo = Store(':memory:')
    repo.update_user(5, watching=True)
    assert repo.watchers() == [5]
    repo.mark_seen(5, ['ad:1', 'ad:1'])
    assert repo.has_seen(5, 'ad:1')
    assert not repo.has_seen(6, 'ad:1')
    repo.update_user(5, watching=False)
    assert not repo.watchers()
    repo.close()


def test_engine_uses_extended_walk_prefilter():
    far = Listing('x1', 'Onliner', 'https://ak.onliner.by/apartments/1',
                  'Квартира', 'Мележа', lat=WORK_LAT + 0.035, lon=WORK_LON, price=550)

    class OneSource:
        async def fetch_all(self, force=False):
            return [SourceResult('Onliner', [far])]

    class ReadyAddresses:
        error = None
        async def ensure_index(self, *, radius_m=2900):
            return True
        def resolve(self, address):
            return None

    class Foot:
        last_error = None
        async def measure(self, ads):
            return {point_key(ad.lat, ad.lon): (35 * 60, 3900) for ad in ads}

    async def scenario():
        store = Store(':memory:')
        try:
            engine = Engine(store, OneSource(), ReadyAddresses(), Foot())
            old = await engine.scan(max_walk_minutes=20)
            new = await engine.scan(max_walk_minutes=45)
            assert len(old.ads) == 0
            assert len(new.ads) == 1
            assert len(new.filtered(Filters(walk_minutes=20))) == 0
            assert len(new.filtered(Filters(walk_minutes=45))) == 1
        finally:
            store.close()

    asyncio.run(scenario())
