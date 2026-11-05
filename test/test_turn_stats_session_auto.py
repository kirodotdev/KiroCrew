"""An Auto turn names ``auto`` in its footer when the provider chain reports nothing.

``read_turn_model`` reads the provider chain. A harness whose session handle keeps
neither a request nor a resolved id after an Auto pick (KAS) leaves it blank, and
the footer then shows no model at all. ``_session_selected_auto`` falls back to the
live session's allocation selection, ``slot._session_requested_model``, and names
``auto`` only when that selection is exactly ``auto``. Both footer writers use it:
the completed turn (``_on_complete``) and the abnormal end
(``_attach_abnormal_turn_stats``).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew.acp.types import TurnUsage
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_turn.turn_stats import _session_selected_auto
from kiro_crew.dashboard.state import _ChatSlot, row_mid


@pytest.mark.parametrize(
    ("selected", "expected"),
    [
        ("auto", "auto"),
        (" Auto ", "auto"),
        ("", ""),
        (None, ""),
        ("claude-sonnet-4.6", ""),
        (7, ""),
    ],
)
def test_only_an_auto_selection_names_auto(selected, expected) -> None:
    slot = SimpleNamespace(_session_requested_model=selected)
    assert _session_selected_auto(slot) == expected


def test_a_slot_without_the_attribute_names_nothing() -> None:
    assert _session_selected_auto(SimpleNamespace()) == ""


def _turn_on(slot: _ChatSlot) -> SimpleNamespace:
    return SimpleNamespace(
        slot=slot,
        state=MagicMock(),
        client=object(),
        _turn_msg_boundary=0,
        _turn_start_mid=None,
        _turn_footer_attached=False,
    )


@pytest.mark.parametrize(("selected", "expected"), [("auto", "auto"), ("", None)])
def test_the_abnormal_end_footer_uses_the_same_fallback(monkeypatch, selected, expected) -> None:
    monkeypatch.setattr(chat_runner, "read_turn_model", lambda client: "")
    slot = _ChatSlot("auto-footer")
    reply = slot.append("assistant", "partial reply", "msg msg-a", broadcast=False)
    # The runner records its reply rows; the footer lands only on one of them.
    slot._turn_reply_mids.append(row_mid(reply))
    slot._session_requested_model = selected

    assert chat_runner._attach_abnormal_turn_stats(_turn_on(slot), TurnUsage(credits=0.26), 3900)

    assert slot.messages[-1]["meta"]["turn_stats"].get("model") == expected


def test_a_reported_model_wins_over_the_session_selection(monkeypatch) -> None:
    monkeypatch.setattr(chat_runner, "read_turn_model", lambda client: "claude-sonnet-4.6")
    slot = _ChatSlot("pinned-footer")
    reply = slot.append("assistant", "partial reply", "msg msg-a", broadcast=False)
    # The runner records its reply rows; the footer lands only on one of them.
    slot._turn_reply_mids.append(row_mid(reply))
    slot._session_requested_model = "auto"

    assert chat_runner._attach_abnormal_turn_stats(_turn_on(slot), TurnUsage(credits=0.26), 3900)

    assert slot.messages[-1]["meta"]["turn_stats"]["model"] == "claude-sonnet-4.6"
