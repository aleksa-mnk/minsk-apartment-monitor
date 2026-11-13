"""Safe, bounded fallback download when Telegram cannot fetch a listing photo by URL.

Normally Telegram fetches the image itself. This module is only a fallback for
photos hosted on the actual marketplace's HTTPS domains. It never follows
redirects to arbitrary hosts or downloads unlimited responses.
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urljoin, urlsplit

import httpx

MAX_PHOTO_BYTES = 9_000_000  # Telegram sendPhoto is limited to 10 MB.
HOSTS = {
    'Kufar': ('kufar.by',),
    'Realt': ('realt.by',),
    'Onliner': ('onliner.by',),
}


def is_https_photo_url(url: object) -> bool:
    if not isinstance(url, str) or len(url) > 4096:
        return False
    try:
        p = urlsplit(url)
        host = (p.hostname or '').lower().rstrip('.')
        if p.scheme != 'https' or not host or p.username or p.password or p.port not in (None, 443):
            return False
        if host in ('localhost',) or host.endswith(('.localhost', '.local', '.internal', '.test')):
            return False
        try:
            ipaddress.ip_address(host)
            return False
        except ValueError:
            pass
        return '.' in host
    except ValueError:
        return False


def is_marketplace_photo_url(url: object, source: str) -> bool:
    if not is_https_photo_url(url):
        return False
    host = (urlsplit(url).hostname or '').lower().rstrip('.')
    return any(host == domain or host.endswith('.' + domain) for domain in HOSTS.get(source, ()))


async def _download(client: httpx.AsyncClient, url: str, referer: str, source: str) -> tuple[bytes, str] | None:
    current = url
    for _ in range(3):
        if not is_marketplace_photo_url(current, source):
            return None
        async with client.stream('GET', current, follow_redirects=False, headers={
            'Accept': 'image/jpeg,image/png,image/*;q=0.8',
            'Referer': referer,
            'User-Agent': 'Mozilla/5.0 (compatible; MinskWalkRentBot/2.1)',
        }) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                target = response.headers.get('location')
                if not target:
                    return None
                current = urljoin(current, target)
                continue
            response.raise_for_status()
            length = response.headers.get('content-length')
            if length and length.isdigit() and int(length) > MAX_PHOTO_BYTES:
                return None
            data = bytearray()
            async for part in response.aiter_bytes():
                if len(data) + len(part) > MAX_PHOTO_BYTES:
                    return None
                data.extend(part)
            # Don't send login/CAPTCHA HTML masquerading as an image.
            if data.startswith(b'\xff\xd8\xff'):
                return bytes(data), 'flat.jpg'
            if data.startswith(b'\x89PNG\r\n\x1a\n'):
                return bytes(data), 'flat.png'
            return None
    return None


async def download_photo(url: str, referer: str, source: str) -> tuple[bytes, str] | None:
    """Returns image bytes + filename for Telegram upload, or None if unsafe/unavailable."""
    if not is_marketplace_photo_url(url, source):
        return None
    async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
        return await _download(client, url, referer, source)
