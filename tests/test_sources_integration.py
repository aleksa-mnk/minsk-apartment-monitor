"""Integration of all three fetchers against deterministic offline HTTP responses."""
import asyncio
import json
import httpx
from sources import ListingSources


def test_fetch_three_sources_mock_http():
    kufar = {'ads': [{'ad_id': 701, 'subject': 'Квартира Мележа 5',
                     'ad_link': 'https://re.kufar.by/vi/701',
                     'price_usd': 45000, 'ad_parameters': [
                         {'p': 'address', 'vl': 'ул. Мележа, 5'},
                     ]}]}
    realt = {'props': {'pageProps': {'objects': [{
        'code': 702, 'title': 'Однокомнатная', 'priceUsd': 500,
        'address': 'Мележа, 7', 'townName': 'Минск'
    }]}}}
    onliner = {'apartments': [{
        'id': 703, 'rent_type': '1_room',
        'location': {'latitude': 53.941, 'longitude': 27.598},
        'price': {'converted': {'USD': {'amount': 400}}},
        'url': 'https://r.onliner.by/ak/apartments/703'
    }], 'page': {'last': 1}}

    async def run():
        def responder(req):
            if req.url.host == 'api.kufar.by':
                return httpx.Response(200, json=kufar)
            if req.url.host == 'realt.by':
                return httpx.Response(200, text='<script id="__NEXT_DATA__">' + json.dumps(realt) + '</script>')
            if req.url.host == 'r.onliner.by':
                return httpx.Response(200, json=onliner)
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
            batch = await ListingSources(client, pages=1).fetch_all(force=True)
            assert len(batch) == 3
            assert all(result.ok for result in batch)
            assert set(ad.key for result in batch for ad in result.ads) == {
                'kufar:701', 'realt:702', 'onliner:703'
            }
    asyncio.run(run())


def test_provider_failure_does_not_stop_other_sources():
    async def run():
        def responder(req):
            if req.url.host == 'r.onliner.by':
                return httpx.Response(200, json={'apartments': [{
                    'id': 111, 'rent_type': '1_room', 'url': 'https://r.onliner.by/ak/apartments/111',
                }], 'page': {'last': 1}})
            return httpx.Response(403, text='access forbidden')

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
            results = await ListingSources(client, pages=1).fetch_all(force=True)
            assert len(results) == 3
            assert results[2].ok and len(results[2].ads) == 1
            assert not results[0].ok and not results[1].ok
    asyncio.run(run())
