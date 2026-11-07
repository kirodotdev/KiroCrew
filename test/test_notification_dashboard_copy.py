"""A note the gateway writes names its dashboard copy; nothing else can name one.

The bell renders that copy in the reader's language (``noteText`` in
``website/src/components/notifications/notifMeta.tsx``) and keeps the note's
English ``title`` / ``body`` for every reader with no catalog, so the two English
texts must say the same thing.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.state import DashboardState
from kiro_crew.messaging.turn_ceiling import ConversationTurnCeiling, TurnCeilingExceeded

_EN_CATALOG = Path(__file__).parent.parent / "website/src/i18n/locales/en.manual.json"


def _make_state(monkeypatch, tmp_path) -> DashboardState:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.notifications.settings.config_dir", lambda: tmp_path)
    return DashboardState(
        sessions=MagicMock(count=0),
        crons=MagicMock(),
        lessons=MagicMock(),
        start_time=0.0,
    )


def test_a_gateway_note_names_its_copy_and_meta_cannot(monkeypatch, tmp_path) -> None:
    state = _make_state(monkeypatch, tmp_path)
    ceiling = ConversationTurnCeiling(max_turns=1, window_secs=60.0)
    ceiling.check("discord:C1:T1")
    with pytest.raises(TurnCeilingExceeded):
        ceiling.check("discord:C1:T1")

    note = state._notification_log[-1]
    assert note["i18n"] == {"id": "turn_limit", "params": {"surface": "discord"}}
    copy = json.loads(_EN_CATALOG.read_text(encoding="utf-8"))["components"]["notifications"]
    assert note["title"] == copy["systemNote"]["turn_limit_title"]
    assert note["body"] == copy["systemNote"]["turn_limit_body"].replace("{{surface}}", "discord")

    # An app push and an agent's send_notification both forward caller meta.
    state.notify("agent", "Build finished", "42 rows", meta={"i18n": {"id": "turn_limit"}})
    assert state._notification_log[-1]["title"] == "Build finished"
    assert "i18n" not in state._notification_log[-1]
