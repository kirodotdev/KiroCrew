"""``ScriptContext.list_sessions()`` rows, projected from the slot list's frame."""

from __future__ import annotations

import pytest

from kiro_crew.cron_script_session_state import SESSION_STATUSES, session_state_rows

_EPOCH = "2026-11-20T00:00:00+00:00"


def _slot(**fields: object) -> dict:
    row: dict = {
        "key": "chat-1",
        "title": "Fix the build",
        "folder_id": "f1",
        "memory_mode": "persistent",
        "running": False,
        "pending_approval": False,
        "needs_input": False,
        "has_options": False,
        "interrupted": False,
        "last_activity_ts": "2026-11-26T10:00:00+00:00",
        "last_agent_ts": "2026-11-26T09:59:00+00:00",
        "unread_ts": "2026-11-26T09:59:00+00:00",
        "last_read_ts": "2026-11-26T09:59:00+00:00",
        "read_epoch": _EPOCH,
    }
    row.update(fields)
    return row


def test_a_row_carries_the_documented_fields():
    (row,) = session_state_rows([_slot()])

    assert row == {
        "slot": "chat-1",
        "title": "Fix the build",
        "folder_id": "f1",
        "status": "idle",
        "unread": False,
        "last_activity": "2026-11-26T10:00:00+00:00",
        "last_reply": "2026-11-26T09:59:00+00:00",
    }


@pytest.mark.parametrize(
    "fields,status",
    [
        ({"running": True, "pending_approval": True}, "awaiting_approval"),
        ({"running": True, "needs_input": True}, "awaiting_answer"),
        ({"running": True}, "working"),
        ({"has_options": True}, "awaiting_choice"),
        ({"interrupted": True}, "interrupted"),
        ({}, "idle"),
    ],
)
def test_the_most_urgent_state_wins(fields, status):
    (row,) = session_state_rows([_slot(**fields)])

    assert row["status"] == status
    assert status in SESSION_STATUSES


@pytest.mark.parametrize(
    "fields,unread",
    [
        ({"unread_ts": "2026-11-26T11:00:00+00:00"}, True),
        ({"unread_ts": "2026-11-26T11:00:00Z"}, True),
        ({"unread_ts": "2026-11-26T11:00:00+00:00", "last_read_ts": None}, True),
        ({"unread_ts": "2026-11-19T00:00:00+00:00", "last_read_ts": None}, False),
        ({"unread_ts": "2026-11-26T11:00:00+00:00", "read_epoch": None}, False),
        ({"unread_ts": None}, False),
        ({"unread_ts": "not a time"}, False),
        ({"unread_ts": "2026-11-26T11:00:00+00:00", "muted": True}, False),
    ],
    ids=[
        "newer-reply",
        "z-suffix",
        "never-read",
        "before-epoch",
        "no-epoch",
        "no-reply",
        "bad",
        "muted",
    ],
)
def test_unread_follows_the_sidebar_badge_rule(fields, unread):
    (row,) = session_state_rows([_slot(**fields)])

    assert row["unread"] is unread


@pytest.mark.parametrize(
    "fields",
    [
        {"memory_mode": "incognito"},
        {"memory_mode": "temporary"},
        {"slack_origin_channel": "C123"},
        {"key": ""},
    ],
    ids=["incognito", "temporary", "channel-born", "no-key"],
)
def test_private_and_channel_chats_are_left_out(fields):
    assert session_state_rows([_slot(**fields)]) == []


def test_a_malformed_entry_is_skipped_and_order_is_kept():
    rows = session_state_rows([_slot(key="a"), "junk", _slot(key="b")])

    assert [r["slot"] for r in rows] == ["a", "b"]
