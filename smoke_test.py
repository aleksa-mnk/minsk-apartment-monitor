"""Live smoke test to run ON THE HOST that has Internet access.

python smoke_test.py
python smoke_test.py --full  # also refresh OSM house-address index
"""
from __future__ import annotations

import argparse
import asyncio
import sys

import httpx

from addresses import AddressResolver
from engine import Engine
from models import Listing, WORK_LAT, WORK_LON
from sources import ListingSources
from store import Store
from walking import PedestrianRouter, point_key


async def run_live_checks(full: bool) -> int:
    headers = {'User-Agent': 'MinskWalkRentBot/2.0 (personal use)', 'Accept-Language': 'ru-RU,ru;q=0.9'}
    db = Store(':memory:')
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=25) as client:
        sources = ListingSources(client, pages=1)
        results = await sources.fetch_all(force=True)
        errors = 0
        for row in results:
            sign = 'OK ' if row.ok else 'ERR'
            with_photos = sum(bool(ad.image) for ad in row.ads)
            print(f'{sign} {row.source:7} : {len(row.ads)} объявлений; с фото: {with_photos}'
                  + (f'  {row.error}' if row.error else ''))
            if row.ok and row.ads and with_photos == 0:
                print(f'WARN {row.source}: нет фото в выдаче. Проверь извлечение фотографий.')
            if not row.ok:
                errors += 1
        router = PedestrianRouter(client, db)
        # This small route should take a few minutes, not zero or car time.
        test_ad = Listing('test:1', 'Test', 'https://example.org', 'Проверка', 'Минск',
                          lat=WORK_LAT + 0.002, lon=WORK_LON + 0.001)
        values = await router.measure([test_ad])
        route = values.get(point_key(test_ad.lat, test_ad.lon))
        if route is None:
            print('ERR Foot routing: unavailable ' + str(router.last_error))
            errors += 1
        else:
            print(f'OK  Foot routing: {round(route[0] / 60, 1)} мин., {route[1]} м по пешему профилю OSM')
        if full:
            address = AddressResolver(client, db)
            success = await address.ensure_index()
            print(('OK ' if success else 'ERR') + f' OSM index: {db.address_count()} адресов; '
                  f'error={address.error or "нет"}')
            if not success:
                errors += 1
    db.close()
    print('\nРЕЗУЛЬТАТ:', 'ВСЕ ПРОВЕРКИ ПРОШЛИ' if not errors else f'ОШИБКИ: {errors}')
    return 1 if errors else 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Live test sources and pedestrian routing')
    parser.add_argument('--full', action='store_true', help='also test one Overpass address query')
    args = parser.parse_args()
    sys.exit(asyncio.run(run_live_checks(args.full)))
