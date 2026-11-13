"""Panel settings coexist with Telegram commands and scan state."""
import json

import pytest

from panel_config import apply_panel_config, parse_panel


def sample_config(revision='r1', **overrides):
    return {
        'revision': revision,
        'settings': {
            'walk_minutes': 25,
            'max_price_usd': 650,
            'rooms': 1,
            'owner_only': True,
            'watching': True,
            **overrides,
        },
    }


def test_dashboard_applies_new_revision_only_once(tmp_path):
    path = tmp_path / 'panel_settings.json'
    state = {'walk_minutes': 20, 'owner_only': False, 'max_price_usd': 0, 'watching': True, 'rooms': 0}
    assert apply_panel_config(state, path) is False
    path.write_text(json.dumps(sample_config()), encoding='utf8')
    assert apply_panel_config(state, path) is True
    assert state['walk_minutes'] == 25
    assert state['owner_only'] is True
    assert state['max_price_usd'] == 650
    assert state['panel_revision'] == 'r1'
    # Telegram command after the same panel revision should remain effective.
    state['max_price_usd'] = 700
    assert apply_panel_config(state, path) is False
    assert state['max_price_usd'] == 700
    path.write_text(json.dumps(sample_config('r2', walk_minutes=18)), encoding='utf8')
    assert apply_panel_config(state, path) is True
    assert state['walk_minutes'] == 18
    assert state['max_price_usd'] == 650


@pytest.mark.parametrize('bad', [
    sample_config(walk_minutes=46),
    sample_config(walk_minutes=True),
    sample_config(rooms=9),
    sample_config(owner_only='yes'),
    sample_config(max_price_usd=-1),
    {'revision': 'r2', 'settings': {'walk_minutes': 20}},
    {'revision': '', 'settings': sample_config()['settings']},
])
def test_rejects_invalid_panel_config(bad):
    with pytest.raises(ValueError):
        parse_panel(bad)


def test_panel_does_not_reset_seen_history(tmp_path):
    state = {'seen': ['a', 'b'], 'initialized': True, 'update_offset': 91,
             'manual_offset': 12}
    path = tmp_path / 'panel_settings.json'
    path.write_text(json.dumps(sample_config()), encoding='utf8')
    assert apply_panel_config(state, path)
    assert state['seen'] == ['a', 'b']
    assert state['update_offset'] == 91
    assert state['initialized'] is True
    assert state['manual_offset'] == 0  # changed filters reset manual browsing
