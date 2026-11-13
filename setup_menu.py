"""One-time Telegram menu setup from Windows, without starting the bot.

Usage from the project directory:
    .\\.venv\\Scripts\\python.exe setup_menu.py
Requires BOT_TOKEN in .env (never commit the .env file).
"""
from __future__ import annotations

import asyncio
import os

from aiogram import Bot
from dotenv import load_dotenv

from telegram_menu import register_menu


async def main() -> None:
    load_dotenv()
    token = os.getenv('BOT_TOKEN', '').strip()
    if not token or ':' not in token:
        raise SystemExit('В файле .env нужен настоящий BOT_TOKEN от BotFather.')
    async with Bot(token=token) as bot:
        await register_menu(bot)
    print('Готово! Открой чат с ботом в Telegram: появилась кнопка «Меню».')
    print('Можно также набрать / и выбрать команду из списка.')


if __name__ == '__main__':
    asyncio.run(main())
