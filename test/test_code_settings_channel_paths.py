"""Messages the app prints name a channel's settings by the real dashboard path.

Every chat channel's settings live under one Settings tab, "Messaging Channels"
(``settings.tabs.channels.label``), which lists the channels from ``CHANNELS`` in
``website/src/pages/settings/ChannelsPanel.tsx``. There is no ``Settings → Slack``
or ``Settings → Discord`` page, so a doctor line or dashboard note that names one
sends the reader to a tab that does not exist. This test reads the channel names
from that list and fails on any ``Settings → <channel>`` inside a string literal
under ``src/kiro_crew`` that skips the tab. Docstrings are developer notes, not
messages, so they are not checked.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
CHANNELS_PANEL = ROOT / "website" / "src" / "pages" / "settings" / "ChannelsPanel.tsx"
SRC_ROOT = ROOT / "src" / "kiro_crew"

# `{ key: 'discord', name: 'Discord', ...` -> "Discord"
_CHANNEL_NAME_RE = re.compile(r"\{\s*key:\s*'[a-z]+',\s*name:\s*'([^']+)'")


def _channel_names() -> list[str]:
    names = _CHANNEL_NAME_RE.findall(CHANNELS_PANEL.read_text(encoding="utf-8"))
    assert len(names) >= 5, f"could not read the channel list from {CHANNELS_PANEL}"
    return names


def _skipped_tab_pattern() -> re.Pattern[str]:
    names = "|".join(re.escape(n) for n in _channel_names())
    # `Settings`, an arrow (→, -> or >), then a channel name.
    return re.compile(rf"\bSettings\s*(?:→|->|>)\s*(?:{names})\b")


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _message_hits(path: Path, text: str, pattern: re.Pattern[str]) -> list[str]:
    tree = ast.parse(text, filename=str(path))
    docstrings = _docstring_ids(tree)
    hits = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and (match := pattern.search(node.value))
        ):
            hits.append(f"{path.relative_to(ROOT)}:{node.lineno}: {match.group(0)!r}")
    return hits


def _skipped_tab_hits() -> list[str]:
    pattern = _skipped_tab_pattern()
    hits = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        # Cheap text filter first: only parse the few files that could match.
        if "Settings" in text and pattern.search(text):
            hits.extend(_message_hits(path, text, pattern))
    return hits


def test_pattern_catches_a_skipped_tab_and_passes_the_full_path() -> None:
    pattern = _skipped_tab_pattern()
    assert pattern.search("update it in Settings → Slack and reinstall")
    assert pattern.search("Settings -> Discord")
    assert not pattern.search("Settings → Messaging Channels → Slack")
    # Discord's own client menu is not our dashboard path.
    assert not pattern.search("Discord → Settings → Advanced")


def test_channel_settings_messages_go_through_messaging_channels() -> None:
    hits = _skipped_tab_hits()
    assert not hits, (
        "Write 'Settings → Messaging Channels → <channel>'; there is no "
        "per-channel Settings tab:\n" + "\n".join(hits)
    )
