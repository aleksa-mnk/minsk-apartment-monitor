"""Map compact Telegram keyboard labels to the same slash commands as typed input."""
from __future__ import annotations

import re


BUTTONS = {
    '⚙️ настройки': '/menu',
    '⚙️ фильтры': '/menu',
    '🔎 поиск': '/search',
    '🔎 найти': '/search',
    '🩺 статус': '/status',
    '👤 собственник': '/owner',
    '🔔 мониторинг': '/toggle',
    '💵 без лимита': '/budget 0',
    '💵 другой бюджет': '/budget',
}


def canonical_command(text: str) -> str:
    """Pure translation; unknown text stays unchanged for caller to ignore."""
    value = ' '.join((text or '').strip().split())
    normalized = value.casefold()
    if normalized in BUTTONS:
        return BUTTONS[normalized]
    walk = re.fullmatch(r'🚶\s*(\d{1,2})\s*мин(?:ут)?', normalized)
    if walk:
        return f'/walk {int(walk.group(1))}'
    price = re.fullmatch(r'💵\s*(\d{1,6})\s*\$', normalized)
    if price:
        return f'/budget {int(price.group(1))}'
    return value
