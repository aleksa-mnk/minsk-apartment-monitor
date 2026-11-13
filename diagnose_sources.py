"""Print source/coordinate coverage without a Telegram token (local diagnostic)."""
from __future__ import annotations

import asyncio
import os

import httpx
from addresses import address_key
from sources import ListingSources


async def main() -> None:
    headers = {'User-Agent': 'MinskWalkRentBot/3.0 (personal rental alert)',
               'Accept-Language': 'ru-RU,ru;q=0.9,en;q=0.8'}
    async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers=headers) as client:
        sources = ListingSources(client, pages=max(1, min(8, int(os.getenv('PAGES_PER_SOURCE', '8')))))
        for result in await sources.fetch_all(force=True):
            if result.error:
                print(f'{result.source}: ERROR {result.error}')
                continue
            data = result.ads
            native = sum(ad.lat is not None and ad.lon is not None for ad in data)
            addresses = sum(address_key(ad.address) is not None for ad in data)
            byn = sum(ad.price is not None and ad.currency == 'BYN' for ad in data)
            usd = sum(ad.price is not None and ad.currency == 'USD' for ad in data)
            print(f'{result.source}: {len(data)} объявлений | координаты: {native} | '
                  f'точный адрес: {addresses} | цена USD: {usd} | цена BYN: {byn}')
            if data and addresses == 0 and native == 0:
                print('  Примеры адресов без дома:',
                      ' ; '.join(repr(ad.address[:60]) for ad in data[:3]))


if __name__ == '__main__':
    asyncio.run(main())
