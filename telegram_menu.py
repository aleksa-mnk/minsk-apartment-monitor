"""Register a real Telegram command menu, visible next to the message field.

The menu is managed by Telegram, so it remains visible when GitHub Actions
is not currently running. Choosing a command inserts it into the input field;
commands that need an argument (/budget, /rooms, /owner) still require one.
"""
from __future__ import annotations

# Telegram commands must be lowercase and may not contain '/' or whitespace.
# Use actionable short descriptions: Telegram shows these after typing '/'.
COMMANDS: tuple[tuple[str, str], ...] = (
    ('menu', '⚙️ Мои фильтры'),
    ('walk', '🚶 Минут до работы: /walk 18'),
    ('budget', '💵 Цена: /budget 600 или 0'),
    ('rooms', '🚪 Комнаты: /rooms 0, 1, 2, 3, 4'),
    ('owner', '👤 Собственник: /owner on или off'),
    ('watch', '🔔 Включить уведомления'),
    ('stop', '🔕 Остановить уведомления'),
    ('search', '🔎 Найти квартиры'),
    ('status', '🩺 Проверить источники'),
    ('help', 'Как пользоваться'),
    ('start', 'Открыть меню'),
)


def quick_reply_keyboard():
    """Persistent reply buttons work in GitHub Actions without callback polling.

    Tap => ordinary Telegram message. Actions reads it on the next schedule.
    """
    from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

    rows = [
        ['⚙️ Настройки', '🔎 Поиск'],
        ['🚶 15 мин', '🚶 20 мин', '🚶 25 мин', '🚶 30 мин'],
        ['💵 500 $', '💵 600 $', '💵 700 $', '💵 Без лимита'],
        ['👤 Собственник', '🔔 Мониторинг'],
        ['🩺 Статус'],
    ]
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=label) for label in row] for row in rows],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder='Фильтры или /walk 18',
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
