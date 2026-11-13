"""Offline tests for the GitHub Actions runner (aiogram itself not installed here)."""
import asyncio
import importlib
import json
import sys
import types
from pathlib import Path

import pytest

from models import Filters, Listing, matches
from store import Store


@pytest.fixture
def app(monkeypatch):
    a = types.ModuleType('aiogram')
    a.Bot = type('Bot', (), {})
    e = types.ModuleType('aiogram.exceptions')
    e.TelegramAPIError = type('TelegramAPIError', (Exception,), {})
    e.TelegramBadRequest = type('TelegramBadRequest', (Exception,), {})
    e.TelegramForbiddenError = type('TelegramForbiddenError', (Exception,), {})
    e.TelegramRetryAfter = type('TelegramRetryAfter', (Exception,), {})
    t = types.ModuleType('aiogram.types')
    class KeyboardButton:
        def __init__(self, text):
            self.text = text
    class ReplyKeyboardMarkup:
        def __init__(self, **kwargs):
            self.keyboard = kwargs['keyboard']
            self.is_persistent = kwargs.get('is_persistent')
    t.KeyboardButton = KeyboardButton
    t.ReplyKeyboardMarkup = ReplyKeyboardMarkup
    class ReplyKeyboardRemove:
        pass
    t.ReplyKeyboardRemove = ReplyKeyboardRemove
    monkeypatch.setitem(sys.modules, 'aiogram.types', t)
    ui = types.ModuleType('bot')
    ui.menu_text = lambda flt: '<b>Menu</b>'
    ui.status_text = lambda: 'Status'

    async def fake_send(bot, chat_id, ad):
        bot.adverts.append(ad.key)

    ui.send_listing = fake_send
    monkeypatch.setitem(sys.modules, 'aiogram', a)
    monkeypatch.setitem(sys.modules, 'aiogram.exceptions', e)
    monkeypatch.setitem(sys.modules, 'bot', ui)
    sys.modules.pop('scheduled_check', None)
    m = importlib.import_module('scheduled_check')
    async def no_sleep(_):
        return None
    monkeypatch.setattr(m.asyncio, 'sleep', no_sleep)
    yield m
    sys.modules.pop('scheduled_check', None)


class DummyScan:
    def __init__(self, ads):
        self.ads = ads

    def filtered(self, flt):
        return [ad for ad in self.ads if matches(ad, flt)]


class DummyBot:
    def __init__(self):
        self.adverts = []
        self.messages = []
        self.updates = []
        self.markups = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.messages.append((chat_id, text))
        self.markups.append(reply_markup)

    async def get_updates(self, offset=0, timeout=0, limit=100):
        return [u for u in self.updates if u.update_id >= offset][:limit]


def ad(n, owner=True):
    return Listing(
        key=f'k{n}', source='Kufar', url=f'https://www.kufar.by/item/{n}',
        title='Test apartment', address='Test 1', price=350, rooms=1,
        owner=owner, walk_seconds=11 * 60,
    )


def test_settings_and_state_survive_multiple_runs(app, tmp_path):
    p = tmp_path / 'state.json'
    state = app.read_state(p)
    assert state['watching'] is True
    assert app.update_filter_from_command(state, '/owner on') == '/owner'
    assert state['owner_only'] is True
    assert app.update_filter_from_command(state, '/budget 650') == '/budget'
    assert app.update_filter_from_command(state, '/rooms 1') == '/rooms'
    assert app.update_filter_from_command(state, '/rooms 9') == 'invalid'
    assert app.update_filter_from_command(state, '/menu') == '/menu'
    assert state['rooms'] == 1
    state['seen'] = ['k3', 'k1', 'k1']
    app.write_state(state, p)
    again = app.read_state(p)
    assert again['seen'] == ['k1', 'k3']
    assert again['max_price_usd'] == 650
    assert again['owner_only'] is True
    assert again['rooms'] == 1
    assert json.loads(p.read_text())['watching'] is True


def test_preview_uses_seen_state_and_new_ads_only(app):
    state = app.read_state(Path('/unlikely-path-for-state.json'))
    tg = DummyBot()
    scan = DummyScan([ad(1), ad(2), ad(3)])
    async def scenario():
        initial = await app.send_adverts(tg, 5, state, scan, Filters(), force_search=False)
        repeated = await app.send_adverts(tg, 5, state, scan, Filters(), force_search=False)
        new = await app.send_adverts(tg, 5, state, DummyScan([ad(1), ad(2), ad(3), ad(4)]),
                                     Filters(), force_search=False)
        return initial, repeated, new
    assert asyncio.run(scenario()) == (3, 0, 1)
    assert tg.adverts == ['k1', 'k2', 'k3', 'k4']
    assert state['seen'] == ['k1', 'k2', 'k3', 'k4']


def test_failed_preview_is_retried_not_silently_marked_seen(app, monkeypatch):
    state = app.read_state(Path('/unlikely-path-for-state.json'))
    tg = DummyBot()
    async def flaky_send(_tg, _chat, listing):
        if listing.key == 'k2':
            raise app.TelegramBadRequest('Bad picture')
        tg.adverts.append(listing.key)
    monkeypatch.setattr(app.telegram_ui, 'send_listing', flaky_send)
    async def scenario():
        first = await app.send_adverts(tg, 5, state, DummyScan([ad(1), ad(2), ad(3)]),
                                       Filters(), force_search=False)
        second = await app.send_adverts(tg, 5, state, DummyScan([ad(1), ad(2), ad(3)]),
                                        Filters(), force_search=False)
        return first, second
    assert asyncio.run(scenario()) == (1, 0)
    assert state['seen'] == ['k1']
    assert state['initialized'] is True


def test_owner_filter_maintained_on_github_runner(app):
    state = app.read_state(Path('/unlikely-path-for-state.json'))
    state['owner_only'] = True
    tg = DummyBot()
    scan = DummyScan([ad(1, owner=True), ad(2, owner=False)])
    asyncio.run(app.send_adverts(tg, 5, state, scan,
                                 Filters(owner_only=True), force_search=False))
    assert tg.adverts == ['k1']
    assert state['seen'] == ['k1']


def test_commands_from_strangers_are_ignored(app):
    tg = DummyBot()
    tg.updates = [
        types.SimpleNamespace(update_id=1, message=types.SimpleNamespace(
            text='/stop', chat=types.SimpleNamespace(id=9, type='private'))),
        types.SimpleNamespace(update_id=2, message=types.SimpleNamespace(
            text='/owner on', chat=types.SimpleNamespace(id=5, type='private'))),
    ]
    state = app.read_state(Path('/unlikely-path-for-state.json'))
    store = Store(':memory:')
    try:
        result = asyncio.run(app.handle_commands(tg, 5, state, store, None))
        assert result is False
        assert state['watching'] is True
        assert state['owner_only'] is True
        assert state['update_offset'] == 3
        assert len(tg.messages) == 1
        assert tg.messages[0][0] == 5
    finally:
        store.close()


def test_workflow_schedule_and_secrets_present():
    import yaml
    base = Path(__file__).resolve().parents[1]
    doc = yaml.load((base / '.github/workflows/monitor.yml').read_text(), Loader=yaml.BaseLoader)
    assert doc['on']['schedule'][0]['cron'] == '17,47 * * * *'
    assert 'workflow_dispatch' in doc['on']
    assert doc['permissions']['contents'] == 'write'
    steps = doc['jobs']['monitor']['steps']
    names = [step['name'] for step in steps]
    assert 'Persist seen listings and filters in private repository' in names
    run = next(step for step in steps if step['name'] == 'Check listings and notify on Telegram')
    assert 'secrets.BOT_TOKEN' in run['env']['BOT_TOKEN']
    assert 'secrets.TELEGRAM_CHAT_ID' in run['env']['TELEGRAM_CHAT_ID']


def test_github_walk_settings_are_persistent(app, tmp_path):
    state = app.read_state(tmp_path / 'missing.json')
    assert state['walk_minutes'] == 20
    assert app.update_filter_from_command(state, '🚶 30 мин') == '/walk'
    assert app.update_filter_from_command(state, '/walk 45') == '/walk'
    assert app.update_filter_from_command(state, '/walk 46') == 'invalid'
    assert app.update_filter_from_command(state, '/budget 650') == '/budget'
    assert app.update_filter_from_command(state, '👤 Собственник') == '/owner'
    assert app.update_filter_from_command(state, '🔔 Мониторинг') == '/toggle'
    p = tmp_path / 'state.json'
    app.write_state(state, p)
    again = app.read_state(p)
    assert again['walk_minutes'] == 45
    assert again['max_price_usd'] == 650
    assert again['owner_only'] is True
    assert again['watching'] is False


def test_github_old_state_json_is_migrated(app, tmp_path):
    path = tmp_path / 'state.json'
    path.write_text(json.dumps({
        'initialized': True, 'max_price_usd': 625, 'owner_only': True,
        'watching': True, 'seen': ['k1'], 'update_offset': 50,
    }), encoding='utf-8')
    state = app.read_state(path)
    assert state['walk_minutes'] == 20
    assert state['ui_version'] == 0
    assert state['max_price_usd'] == 625
    assert state['seen'] == ['k1']
    app.write_state(state, path)
    assert json.loads(path.read_text())['walk_minutes'] == 20


def test_queued_commands_produce_one_short_reply(app, tmp_path):
    state = app.read_state(tmp_path / 'missing.json')
    tg = DummyBot()
    tg.updates = [
        types.SimpleNamespace(update_id=1, message=types.SimpleNamespace(
            text='🚶 30 мин', chat=types.SimpleNamespace(id=5, type='private'))),
        types.SimpleNamespace(update_id=2, message=types.SimpleNamespace(
            text='💵 600 $', chat=types.SimpleNamespace(id=5, type='private'))),
        types.SimpleNamespace(update_id=3, message=types.SimpleNamespace(
            text='👤 Собственник', chat=types.SimpleNamespace(id=5, type='private'))),
    ]
    store = Store(':memory:')
    try:
        asyncio.run(app.handle_commands(tg, 5, state, store, None))
        assert len(tg.messages) == 1
        assert state['walk_minutes'] == 30
        assert state['max_price_usd'] == 600
        assert state['owner_only'] is True
        assert store.get_user(5).walk_minutes == 30
        assert state['ui_version'] == 3
    finally:
        store.close()


def test_old_persistent_keyboard_removed_once_even_without_commands(app):
    state = app.read_state(Path('/unlikely-path-for-state.json'))
    state['watching'] = False
    state['ui_version'] = 2  # stored by previous bot version
    tg = DummyBot()
    store = Store(':memory:')
    try:
        asyncio.run(app.scheduled_cycle(tg, 5, state, store, None))
        assert len(tg.messages) == 1
        assert 'скрыты' in tg.messages[0][1]
        assert isinstance(tg.markups[0], sys.modules['aiogram.types'].ReplyKeyboardRemove)
        assert state['ui_version'] == 3
        asyncio.run(app.scheduled_cycle(tg, 5, state, store, None))
        assert len(tg.messages) == 1  # no repeated setup message
    finally:
        store.close()


def test_hide_command_also_removes_keyboard_when_already_migrated(app):
    state = app.read_state(Path('/unlikely-path-for-state.json'))
    state['ui_version'] = 3
    tg = DummyBot()
    tg.updates = [types.SimpleNamespace(
        update_id=1,
        message=types.SimpleNamespace(text='/hide', chat=types.SimpleNamespace(id=5, type='private')),
    )]
    store = Store(':memory:')
    try:
        asyncio.run(app.handle_commands(tg, 5, state, store, None))
        assert len(tg.messages) == 1
        assert isinstance(tg.markups[0], sys.modules['aiogram.types'].ReplyKeyboardRemove)
        assert state['ui_version'] == 3
    finally:
        store.close()
