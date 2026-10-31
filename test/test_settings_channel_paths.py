"""Messages must name the Settings page a chat channel actually lives on."""

from __future__ import annotations

import re
from pathlib import Path

import kiro_crew

# website/src/pages/settings/ChannelsPanel.tsx: every channel is under one tab.
_CHANNELS = (
    "Slack|Discord|Telegram|Webex|WeCom|Feishu|Microsoft Teams|Teams|WeChat|iMessage|WhatsApp"
)
_WRONG_PATH = re.compile(rf"Settings → (?:{_CHANNELS})\b")


def test_no_python_source_names_a_settings_tab_per_channel() -> None:
    root = Path(kiro_crew.__file__).parent
    hits = [
        f"{path.relative_to(root)}:{line_no}"
        for path in root.rglob("*.py")
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _WRONG_PATH.search(line)
    ]

    assert not hits, f"use Settings → Messaging Channels → <channel>: {hits}"
