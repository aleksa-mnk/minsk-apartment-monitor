"""Photo extraction and bounded upload fallback, with mocked (not live) servers."""
import asyncio
import ast
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

import photo_delivery
from models import Listing
from photo_delivery import _download, is_https_photo_url, is_marketplace_photo_url
from sources import first_photo, parse_kufar, parse_onliner, parse_realt


JPEG = b'\xff\xd8\xff' + b'0' * 200
PNG = b'\x89PNG\r\n\x1a\n' + b'0' * 200


def test_secure_photo_url():
    assert is_https_photo_url('https://content.onliner.by/apartment/img.jpg')
    assert is_marketplace_photo_url('https://rms.kufar.by/images/photo.jpg', 'Kufar')
    assert not is_marketplace_photo_url('https://example.org/img.jpg', 'Kufar')
    for url in ('http://realt.by/img.jpg', 'https://127.0.0.1/img.jpg',
                'https://localhost/img.jpg', 'https://foo.local/img.jpg',
                'https://u:pass@onliner.by/img.jpg', 'file:///foo'):
        assert not is_https_photo_url(url)


def test_photo_extraction_for_each_provider():
    kufar = parse_kufar({'ads': [{
        'ad_id': 1, 'ad_link': 'https://re.kufar.by/vi/1',
        'subject': 'Квартира', 'images': [{'path': 'abc.jpg'}],
    }]})
    assert kufar[0].image == 'https://rms.kufar.by/v1/list_thumbs_2x/abc.jpg'
    onliner = parse_onliner({'apartments': [{
        'id': 1, 'url': 'https://r.onliner.by/ak/apartments/1',
        'photo': {'url': 'https://content.onliner.by/photo.jpg'},
    }]})
    assert onliner[0].image == 'https://content.onliner.by/photo.jpg'
    realt = parse_realt({'props': {'pageProps': {'objects': [{
        'code': 1, 'townName': 'Минск', 'photos': [{'large': '//img.realt.by/large.png'}],
    }]}}})
    assert realt[0].image == 'https://img.realt.by/large.png'
    assert first_photo({'images': [{'thumbnail': 'https://img.realt.by/p.jpg'}]}, 'Realt')


def test_bounded_download_and_redirect_protection():
    async def test():
        def good(req):
            return httpx.Response(200, content=JPEG, headers={'Content-Type': 'image/jpeg'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(good)) as client:
            result = await _download(client, 'https://img.realt.by/a.jpg', 'https://realt.by/x', 'Realt')
            assert result == (JPEG, 'flat.jpg')
        def oversize(req):
            return httpx.Response(200, content=JPEG, headers={'content-length': '10000000'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(oversize)) as client:
            assert await _download(client, 'https://img.realt.by/a.jpg', '', 'Realt') is None
        def evil_redirect(req):
            return httpx.Response(302, headers={'location': 'https://evil.example/photo.jpg'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(evil_redirect)) as client:
            assert await _download(client, 'https://img.realt.by/a.jpg', '', 'Realt') is None
    asyncio.run(test())


def _isolated_send_listing():
    # Execute just the function with fake Telegram API; the test environment
    # needn't have aiogram, which is installed by requirements.txt on Windows.
    root = ast.parse(Path(__file__).resolve().parents[1].joinpath('bot.py').read_text())
    code = next(x for x in root.body if isinstance(x, ast.AsyncFunctionDef) and x.name == 'send_listing')
    code.decorator_list = []
    fake = {}

    class TelegramBadRequest(Exception):
        pass

    class BufferedInputFile:
        def __init__(self, data, filename):
            self.data, self.filename = data, filename

    fake.update({'Bot': object, 'Listing': Listing,
                 'listing_text': lambda ad: 'До работы: 17 мин. | 500 USD | Kufar',
                 'ad_keyboard': lambda ad: 'two buttons',
                 'TelegramBadRequest': TelegramBadRequest,
                 'BufferedInputFile': BufferedInputFile,
                 'is_https_photo_url': is_https_photo_url,
                 'download_photo': AsyncMock(return_value=(JPEG, 'flat.jpg')),
                 'httpx': httpx, 'log': logging.getLogger('photo-test')})
    exec(compile(ast.fix_missing_locations(ast.Module(body=[code], type_ignores=[])), 'bot.py', 'exec'), fake)
    return fake['send_listing'], fake, TelegramBadRequest


def test_send_listing_photo_and_fallback():
    async def run():
        send_listing, env, BadRequest = _isolated_send_listing()
        ad = Listing('kufar:1', 'Kufar', 'https://re.kufar.by/vi/1', 'Квартира', 'Мележа',
                     image='https://rms.kufar.by/p.jpg', walk_seconds=15*60)
        bot = SimpleNamespace(send_photo=AsyncMock(), send_message=AsyncMock())
        await send_listing(bot, 123, ad)
        assert bot.send_photo.await_count == 1 and bot.send_message.await_count == 0
        assert bot.send_photo.await_args.kwargs['caption'].startswith('До работы:')

        bot.send_photo.reset_mock()
        bot.send_photo.side_effect = [BadRequest('URL not accessible'), None]
        await send_listing(bot, 123, ad)
        assert bot.send_photo.await_count == 2
        assert bot.send_photo.await_args.kwargs['photo'].filename == 'flat.jpg'
        assert bot.send_message.await_count == 0

        bot.send_photo.reset_mock(side_effect=True)
        bot.send_photo.side_effect = BadRequest('No photo')
        await send_listing(bot, 123, ad)
        assert bot.send_message.await_count == 1

        bot.send_message.reset_mock()
        no_image = Listing('realt:1', 'Realt', 'https://realt.by/x', 'Квартира', 'Мележа',
                           walk_seconds=15*60)
        await send_listing(bot, 123, no_image)
        assert bot.send_message.await_count == 1
    asyncio.run(run())
