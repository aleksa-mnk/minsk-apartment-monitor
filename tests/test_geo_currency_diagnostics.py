"""Regression tests for real-world omissions that hid Kufar and Realt results."""
import asyncio
import httpx
from sources import parse_kufar, parse_realt, coordinates
from models import Listing, Filters, WORK_LAT, WORK_LON, matches
from rates import parse_usd_rate, usd_byn_rate
from engine import Engine
from sources import SourceResult
from walking import point_key
from store import Store


def test_kufar_city_does_not_override_full_address_and_geo_param():
    ad = parse_kufar({'ads': [{
        'ad_id': '123', 'ad_link': 'https://re.kufar.by/vi/123',
        'subject': 'Квартира в центре', 'price_usd': '55000',
        'location': 'Минск', 'ad_parameters': [
            {'p': 're_address', 'vl': 'ул. Мележа, 5к1'},
            {'p': 'coordinates', 'vl': '27.597341,53.940579'},
        ],
    }]})[0]
    assert ad.address == 'ул. Мележа, 5к1'
    assert (ad.lat, ad.lon) == (53.940579, 27.597341)
    assert ad.price == 550


def test_kufar_city_and_street_house_fields_are_combined():
    ad = parse_kufar({'ads': [{
        'ad_id': 123, 'ad_link': 'https://re.kufar.by/vi/123',
        'location': {'name': 'Минск'},
        'ad_parameters': [
            {'p': 're_street', 'v': 'ул. Мележа'},
            {'p': 'house_number', 'vl': '5к1'},
        ],
    }]})[0]
    assert ad.address == 'ул. Мележа, 5к1'


def test_realt_city_label_does_not_override_street_and_house():
    ad = parse_realt({'data': {'items': [{
        'code': 999, 'townName': 'Минск', 'address': 'Минск',
        'streetName': 'ул. Мележа', 'houseNumber': '5к1',
        'price': 1400, 'priceCurrency': 933,
        'map': {'geometry': {'coordinates': [27.597341, 53.940579]}},
    }]}})[0]
    assert ad.address == 'Минск, ул. Мележа, 5к1'
    assert (ad.lat, ad.lon) == (53.940579, 27.597341)
    assert ad.currency == 'BYN'
    assert ad.price == 1400


def test_geojson_nested_point_is_supported_but_price_is_not_geo():
    assert coordinates({'geo': {'geometry': {'coordinates': [27.597, 53.940]}}}) == (53.940, 27.597)
    assert coordinates({'price': 53.940, 'area': 27.597}) is None


def test_byn_price_under_usd_budget_not_discarded():
    ad = Listing('real:1', 'Realt', 'https://realt.by/1', 'flat', 'ул. Мележа, 5',
                 price=1400, currency='BYN', walk_seconds=600)
    assert not matches(ad, Filters(max_price_usd=550, walk_minutes=25))
    assert matches(ad, Filters(max_price_usd=550, walk_minutes=25), byn_per_usd=3.0026)
    assert not matches(ad, Filters(max_price_usd=400, walk_minutes=25), byn_per_usd=3.0026)


def test_nbrb_rate_scales_correctly_and_rejects_invalid():
    assert parse_usd_rate({'Cur_Abbreviation': 'USD', 'Cur_Scale': 100,
                           'Cur_OfficialRate': 300.26}) == __import__('pytest').approx(3.0026)
    try:
        parse_usd_rate({'Cur_Abbreviation': 'EUR', 'Cur_Scale': 1, 'Cur_OfficialRate': 3})
    except ValueError:
        pass
    else:
        assert False, 'Wrong currency must not be accepted'


def test_official_rate_during_scan_exposes_realt_as_a_match():
    class Sources:
        async def fetch_all(self, force=False):
            return [
                SourceResult('Kufar', [Listing('k1', 'Kufar', 'https://re.kufar.by/vi/1',
                                                'One', 'Мележа 5к1', price=450)]),
                SourceResult('Realt', [Listing('r1', 'Realt', 'https://realt.by/a/1',
                                                'Two', 'Мележа 5к1', price=1400, currency='BYN')]),
                SourceResult('Onliner', []),
            ]

        def __init__(self, client):
            self.client = client

    class Addresses:
        async def ensure_index(self, *, radius_m):
            assert radius_m >= 2900
            return True

        def resolve(self, address):
            assert 'Мележа' in address
            return (WORK_LAT, WORK_LON)

    class Foot:
        last_error = None
        async def measure(self, ads):
            return {point_key(x.lat, x.lon): (600, 800) for x in ads}

    async def scenario():
        def reply(req):
            if req.url.host == 'api.nbrb.by':
                return httpx.Response(200, json={'Cur_Abbreviation': 'USD',
                                                 'Cur_OfficialRate': 3.0026, 'Cur_Scale': 1})
            return httpx.Response(404)
        store = Store(':memory:')
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            scan = await Engine(store, Sources(client), Addresses(), Foot()).scan(max_walk_minutes=25)
            f = Filters(max_price_usd=550, walk_minutes=25)
            assert [ad.source for ad in scan.filtered(f)] == ['Kufar', 'Realt']
            assert scan.diagnostics(f)['Realt']['matched'] == 1
            assert scan.diagnostics(f)['Kufar']['geo_resolved'] == 1
        store.close()
    asyncio.run(scenario())


def test_osm_index_reloads_for_longer_walk_limit():
    import time
    from addresses import AddressResolver

    calls = []

    def reply(req):
        from urllib.parse import parse_qs
        calls.append(parse_qs(req.content.decode())['data'][0])
        nodes = [
            {'type': 'node', 'lat': WORK_LAT, 'lon': WORK_LON,
             'tags': {'addr:street': 'Мележа', 'addr:housenumber': str(n)}}
            for n in range(1, 34)
        ]
        return httpx.Response(200, json={'elements': nodes})

    async def scenario():
        store = Store(':memory:')
        store.set_meta('osm_index_updated', str(time.time()))
        store.set_meta('osm_index_radius_m', '2900')
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            resolver = AddressResolver(client, store)
            await resolver.ensure_index(radius_m=2900)
            assert not calls
            await resolver.ensure_index(radius_m=3800)
            assert len(calls) == 1
            assert 'around:3800' in calls[0]
            assert store.get_meta_float('osm_index_radius_m') == 3800
        store.close()
    asyncio.run(scenario())


def test_kufar_separate_lat_and_lon_parameters():
    from sources import coordinates_from_parameters
    pt = coordinates_from_parameters([
        {'p': 'latitude', 'vl': '53.940579'},
        {'p': 'longitude', 'vl': '27.597341'},
    ])
    assert pt == (53.940579, 27.597341)
