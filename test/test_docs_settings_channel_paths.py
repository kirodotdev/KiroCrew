"""Docs name a channel's settings by the path the dashboard really has.

Every chat channel's settings live under one Settings tab, "Messaging Channels",
which lists the channels from ``CHANNELS`` in
``website/src/pages/settings/ChannelsPanel.tsx``. There is no ``Settings → Slack``
or ``Settings → Discord`` page, so a doc that writes one sends the reader to a
tab that does not exist. This test reads the channel names from that list and
fails on any ``Settings → <channel>`` in the docs that skips the tab.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
CHANNELS_PANEL = ROOT / "website" / "src" / "pages" / "settings" / "ChannelsPanel.tsx"
DOC_ROOTS = (ROOT / "src" / "kiro_crew" / "docs", ROOT / "docs")

# `{ key: 'discord', name: 'Discord', ...` -> "Discord"
_CHANNEL_NAME_RE = re.compile(r"\{\s*key:\s*'[a-z]+',\s*name:\s*'([^']+)'")


def _channel_names() -> list[str]:
    names = _CHANNEL_NAME_RE.findall(CHANNELS_PANEL.read_text(encoding="utf-8"))
    assert len(names) >= 5, f"could not read the channel list from {CHANNELS_PANEL}"
    return names


def _skipped_tab_hits() -> list[str]:
    names = "|".join(re.escape(n) for n in _channel_names())
    # `Settings`, an arrow (→, -> or >), then a channel name. `\s` spans a line
    # wrap, and bold markers around either part are ignored.
    pattern = re.compile(rf"\bSettings\**\s*(?:→|->|>)\s*\**\s*(?:{names})\b")
    hits = []
    for doc_root in DOC_ROOTS:
        for path in sorted(doc_root.rglob("*.md")):
            text = path.read_text(encoding="utf-8")
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                hits.append(f"{path.relative_to(ROOT)}:{line}: {match.group(0)!r}")
    return hits


def test_channel_settings_paths_go_through_messaging_channels() -> None:
    hits = _skipped_tab_hits()
    assert not hits, (
        "Write 'Settings → Messaging Channels → <channel>'; there is no "
        "per-channel Settings tab:\n" + "\n".join(hits)
    )
