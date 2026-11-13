"""Register unobtrusive Telegram slash commands and the built-in Menu button.

Unlike a ReplyKeyboardMarkup, this does not take space below the text field.
"""
from __future__ import annotations

COMMANDS: tuple[tuple[str, str], ...] = (
    ('menu', 'Показать настройки'),
    ('walk', 'Время пешком: /walk 18'),
    ('budget', 'Бюджет: /budget 600 или 0'),
    ('rooms', 'Комнаты: /rooms 0, 1, 2, 3, 4'),
    ('owner', 'Собственник: /owner on или off'),
    ('watch', 'Включить мониторинг'),
    ('stop', 'Остановить мониторинг'),
    ('search', 'Проверить объявления'),
    ('status', 'Состояние источников'),
    ('hide', 'Скрыть большие кнопки'),
    ('help', 'Подсказка по командам'),
    ('start', 'Открыть настройки'),
)


async def register_menu(bot) -> None:
    """Install / hints and the 'Menu' button for private chats.

    A one-off setup is sufficient; repeated calls from scheduled checks are
    harmless and keep the list synchronized with code updates.
    """
    from aiogram.types import BotCommand, MenuButtonCommands

    await bot.set_my_commands(
        [BotCommand(command=command, description=description)
         for command, description in COMMANDS]
    )
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
