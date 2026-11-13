"""Every system notification channel has a "when does this fire?" tip.

``CHANNEL_TIP_KEY`` in the Sources pane maps each ``system.*`` channel id to a
catalog key. These tests pin it to the backend ``SYSTEM_CHANNELS`` registry, so
adding a channel without a tip, or keeping a tip for a removed channel, fails
here instead of shipping a row with no explanation (or a stale one).
"""

import json
import re
from pathlib import Path

from kiro_crew.notifications.bus import SYSTEM_CHANNELS

ROOT = Path(__file__).resolve().parent.parent
PANEL = ROOT / "website" / "src" / "pages" / "settings" / "NotificationsPanel.tsx"
EN_MANUAL = ROOT / "website" / "src" / "i18n" / "locales" / "en.manual.json"

_TABLE_RE = re.compile(r"const CHANNEL_TIP_KEY: Record<string, string> = \{(?P<body>.*?)\n\}", re.S)
_ENTRY_RE = re.compile(r"^\s*'(?P<channel>[^']+)':\s*'(?P<key>[^']+)',?\s*$")


def _tip_table() -> dict[str, str]:
    match = _TABLE_RE.search(PANEL.read_text(encoding="utf-8"))
    assert match, "CHANNEL_TIP_KEY not found in NotificationsPanel.tsx"
    table: dict[str, str] = {}
    for line in match.group("body").splitlines():
        if not line.strip():
            continue
        entry = _ENTRY_RE.match(line)
        assert entry, f"unparseable CHANNEL_TIP_KEY line: {line!r}"
        assert entry["channel"] not in table, f"duplicate entry for {entry['channel']}"
        table[entry["channel"]] = entry["key"]
    return table


def _resolve(catalog: dict, key: str) -> object:
    node: object = catalog
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def test_tip_table_covers_exactly_the_system_channels():
    assert set(_tip_table()) == set(SYSTEM_CHANNELS)


def test_every_tip_key_resolves_to_english_copy():
    catalog = json.loads(EN_MANUAL.read_text(encoding="utf-8"))
    for channel, key in _tip_table().items():
        value = _resolve(catalog, key)
        assert isinstance(value, str) and value.strip(), f"{channel}: {key} has no English copy"
