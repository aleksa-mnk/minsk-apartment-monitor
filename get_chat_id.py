"""Show Telegram private chat IDs after a fresh /start to your bot.

Stop a locally running bot.py first to avoid getUpdates polling conflicts.
"""
import asyncio
import os

from aiogram import Bot
from dotenv import load_dotenv


async def main():
    load_dotenv()
    token = os.getenv('BOT_TOKEN', '').strip()
    if not token or ':' not in token:
        raise SystemExit('BOT_TOKEN is missing in .env')
    async with Bot(token=token) as bot:
        updates = await bot.get_updates(timeout=0, limit=30)
    private_ids = {
        m.chat.id for u in updates
        if (m := u.message) and m.chat.type == 'private'
    }
    if not private_ids:
        print('No messages found. Stop local bot.py, send /start to your Telegram bot, retry.')
    else:
        for chat_id in sorted(private_ids):
            print(f'Private Telegram chat ID: {chat_id}')
        print('Use only YOUR ID in the private GitHub repository Actions secret TELEGRAM_CHAT_ID.')


if __name__ == '__main__':
    asyncio.run(main())
