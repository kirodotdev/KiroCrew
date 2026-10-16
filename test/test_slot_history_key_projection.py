"""The slot payload carries the transcript stem reopen-closed-session resumes by.

``to_dict()["history_key"]`` is ``slot_history_key`` folded to a filename stem,
the same key History rows carry. The dashboard's Cmd/Ctrl+Shift+T reopen
resumes by it; the client cannot rebuild it for an unbound channel-born slot,
which carries no ``linked_session_key`` and no origin link.
"""

from __future__ import annotations

from kiro_crew.dashboard.state import _ChatSlot


def test_dashboard_slot_history_key_is_its_dashboard_transcript_stem():
    assert _ChatSlot("chat-1").to_dict()["history_key"] == "dashboard_chat-1"


def test_linked_slot_history_key_is_its_linked_transcript_stem():
    slot = _ChatSlot("chat-2")
    slot.linked_session_key = "slack:C0ABC:1790411186.442029"
    assert slot.to_dict()["history_key"] == "slack_C0ABC_1790411186.442029"


def test_unbound_channel_slot_history_key_is_the_channel_transcript_stem():
    # The case the client cannot derive: channel-born, no binding, no link.
    slot = _ChatSlot("slack_1785370133.085469")
    slot.channel_origin = True
    assert slot.linked_session_key == ""
    assert slot.to_dict()["history_key"] == "slack_1785370133.085469"
