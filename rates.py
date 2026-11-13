"""Official USD/BYN reference rate for filtering BYN-priced listings.

Never guess a rate. If the official service is unreachable, a recent (<=72 h)
previously verified rate from the local SQLite cache may be used.
"""
from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)
USD_RATE_URL = 'https://api.nbrb.by/exrates/rates/USD?parammode=2'


def parse_usd_rate(data: object) -> float:
    if not isinstance(data, dict) or data.get('Cur_Abbreviation') != 'USD':
        raise ValueError('NBRB: USD rate was not returned')
    scale = float(data['Cur_Scale'])
    rate = float(data['Cur_OfficialRate']) / scale
    if not (1.0 < rate < 20.0):
        raise ValueError('NBRB: invalid USD/BYN rate')
    return rate


async def usd_byn_rate(client: httpx.AsyncClient, store) -> float | None:
    cached = store.get_meta_float('usd_byn_rate')
    updated = store.get_meta_float('usd_byn_updated')
    age = time.time() - updated if updated else float('inf')
    if 0 < cached < 20 and 0 <= age <= 22 * 3600:
        return cached
    try:
        res = await client.get(USD_RATE_URL, timeout=12)
        res.raise_for_status()
        rate = parse_usd_rate(res.json())
        store.set_meta('usd_byn_rate', str(rate))
        store.set_meta('usd_byn_updated', str(time.time()))
        return rate
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        log.warning('Cannot verify USD/BYN exchange rate: %s', exc)
        if 1.0 < cached < 20 and 0 <= age < 72 * 3600:
            log.warning('Using last verified NBRB rate from cache (%.1f h old)', age / 3600)
            return cached
        return None
