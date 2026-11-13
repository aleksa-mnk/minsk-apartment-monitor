"""Apply the Netlify dashboard preferences once per revision.

The panel file and the scan state are intentionally separate Git files. Actions
commits its state and seen history; Netlify only commits panel_settings.json.
Telegram commands still work: once a panel revision has been consumed, Actions
keeps subsequent Telegram changes until the panel is saved again.
"""
from __future__ import annotations

import json
from pathlib import Path

from models import MAX_WALK_MINUTES, MIN_WALK_MINUTES

CONFIG_FIELDS = ('walk_minutes', 'max_price_usd', 'rooms', 'owner_only', 'watching')
DEFAULT_PATH = Path('data/panel_settings.json')


def parse_panel(data: object) -> tuple[str, dict]:
    if not isinstance(data, dict) or set(data) != {'revision', 'settings'}:
        raise ValueError('Panel config must contain revision and settings')
    revision = data['revision']
    settings = data['settings']
    if not isinstance(revision, str) or not 1 <= len(revision) <= 100:
        raise ValueError('Panel revision missing or invalid')
    if not isinstance(settings, dict) or set(settings) != set(CONFIG_FIELDS):
        raise ValueError('Panel settings have missing or extra fields')
    walk = settings['walk_minutes']
    price = settings['max_price_usd']
    rooms = settings['rooms']
    if (type(walk) is not int or not MIN_WALK_MINUTES <= walk <= MAX_WALK_MINUTES
            or type(price) is not int or not 0 <= price <= 100000
            or type(rooms) is not int or not 0 <= rooms <= 4
            or type(settings['owner_only']) is not bool
            or type(settings['watching']) is not bool):
        raise ValueError('Panel filters are invalid')
    return revision, settings


def apply_panel_config(state: dict, path: Path = DEFAULT_PATH) -> bool:
    """Apply new panel config; do not reapply it on later scheduled scans."""
    if not path.exists():
        return False
    revision, settings = parse_panel(json.loads(path.read_text(encoding='utf-8')))
    if state.get('panel_revision', '') == revision:
        return False
    state.update(settings)
    # A new filter set should start browsing matches from the beginning.
    state['manual_offset'] = 0
    state['panel_revision'] = revision
    return True
