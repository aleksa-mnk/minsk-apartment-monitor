"""Check the actual command list and registration API without network access."""
import asyncio
import sys
import types
from dataclasses import dataclass

import telegram_menu


def test_commands_have_examples_and_are_unique():
    commands = dict(telegram_menu.COMMANDS)
    assert len(commands) == len(telegram_menu.COMMANDS)
    assert {'menu', 'walk', 'budget', 'rooms', 'owner', 'watch', 'stop', 'search', 'status', 'help'} <= set(commands)
    assert '600' in commands['budget']
    assert '18' in commands['walk']
    assert '0' in commands['rooms']
    assert 'on' in commands['owner']
    for command, description in telegram_menu.COMMANDS:
        assert 1 <= len(command) <= 32
        assert command.islower() and command.isascii() and command.replace('_', '').isalnum()
        assert 1 <= len(description) <= 256


def test_telegram_menu_is_registered_and_activated(monkeypatch):
    @dataclass
    class BotCommand:
        command: str
        description: str

    class MenuButtonCommands:
        pass

    fake = types.ModuleType('aiogram.types')
    fake.BotCommand = BotCommand
    fake.MenuButtonCommands = MenuButtonCommands
    monkeypatch.setitem(sys.modules, 'aiogram.types', fake)

    class Bot:
        def __init__(self):
            self.commands = None
            self.button = None

        async def set_my_commands(self, commands):
            self.commands = commands

        async def set_chat_menu_button(self, *, menu_button):
            self.button = menu_button

    bot = Bot()
    asyncio.run(telegram_menu.register_menu(bot))
    assert [command.command for command in bot.commands] == [command for command, _ in telegram_menu.COMMANDS]
    assert isinstance(bot.button, MenuButtonCommands)


def test_menu_does_not_create_persistent_reply_keyboard():
    assert not hasattr(telegram_menu, 'quick_reply_keyboard')
    assert 'hide' in dict(telegram_menu.COMMANDS)
