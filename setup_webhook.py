"""Register the secure Netlify webhook for instant Telegram commands.

Use:  .\\.venv\\Scripts\\python.exe setup_webhook.py https://your-site.netlify.app
No new secrets: the webhook verification header is derived from BOT_TOKEN.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from dotenv import load_dotenv
from telegram_menu import COMMANDS


def secret_from_token(token: str) -> str:
    return hashlib.sha256(f'minsk-telegram-webhook-v1:{token}'.encode('utf-8')).hexdigest()


def telegram_api(token: str, method: str, data: dict) -> dict:
    body = json.dumps(data, ensure_ascii=False).encode('utf-8')
    request = Request(
        f'https://api.telegram.org/bot{token}/{method}',
        data=body,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    with urlopen(request, timeout=15) as response:
        payload = json.load(response)
    if not payload.get('ok'):
        raise RuntimeError(f'Telegram rejected {method}')
    return payload['result']


def main() -> None:
    load_dotenv()
    token = os.getenv('BOT_TOKEN', '').strip()
    if not token or ':' not in token:
        raise SystemExit('BOT_TOKEN not found. Check local .env file.')
    if len(sys.argv) == 2 and sys.argv[1] == '--check':
        info = telegram_api(token, 'getWebhookInfo', {})
        print('Webhook:', info.get('url') or 'not configured')
        print('Pending:', info.get('pending_update_count'))
        if info.get('last_error_message'):
            print('Last delivery error:', info['last_error_message'])
        return
    if len(sys.argv) != 2:
        raise SystemExit('Usage: python setup_webhook.py https://your-site.netlify.app')
    site = sys.argv[1].rstrip('/')
    parsed = urlparse(site)
    if parsed.scheme != 'https' or not parsed.netloc.endswith('.netlify.app') or parsed.path:
        raise SystemExit('Use https://your-site.netlify.app without a path.')
    url = site + '/.netlify/functions/telegram'
    try:
        with urlopen(url, timeout=15) as result:
            health = json.load(result)
        if health.get('service') != 'telegram-webhook':
            raise RuntimeError('Netlify returned an unexpected page')
        if not health.get('configured'):
            raise RuntimeError('Add BOT_TOKEN and TELEGRAM_CHAT_ID to Netlify Functions environment')
    except (URLError, ValueError, RuntimeError) as exc:
        raise SystemExit(f'Webhook function is not deployed yet: {exc}')
    try:
        result = telegram_api(token, 'setWebhook', {
            'url': url,
            'secret_token': secret_from_token(token),
            'allowed_updates': ['message', 'callback_query'],
            'drop_pending_updates': True,
            'max_connections': 1,
        })
        info = telegram_api(token, 'getWebhookInfo', {})
        telegram_api(token, 'setMyCommands', {'commands': [
            {'command': name, 'description': description} for name, description in COMMANDS
        ]})
        telegram_api(token, 'setChatMenuButton', {'menu_button': {'type': 'commands'}})
    except (HTTPError, URLError, RuntimeError) as exc:
        raise SystemExit(f'Telegram setup failed: {type(exc).__name__}. Check Netlify BOT_TOKEN.')
    print('OK: Telegram webhook registered.')
    print('URL:', info['url'])
    print('Pending updates:', info.get('pending_update_count', 'unknown'))
    print('Now send /start to your bot. Do not run local bot.py concurrently.')


if __name__ == '__main__':
    main()
