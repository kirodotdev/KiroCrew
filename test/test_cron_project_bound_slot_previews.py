"""A project-bound cron run's text stays out of non-owner slot rows.

Notification and chat frames carrying a project-bound run's output are withheld
from non-owner sockets, but every slot-list row quotes its newest turn in
``last_message`` / ``prompt_preview``, and that list reaches every dashboard
user, SSE reader and app token. Only the owner boundary
(``include_check_status``) gets the quoted text for a ``cron-`` slot whose
newest turn is not explicitly unbound.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.state import DashboardState

SECRET = "project output nobody else should read"


@pytest.fixture(autouse=True)
def _loop():
    import asyncio

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield
    loop.close()
    asyncio.set_event_loop(None)


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = DashboardState(
        sessions=MagicMock(count=0),
        crons=MagicMock(),
        lessons=MagicMock(),
        start_time=0.0,
    )
    monkeypatch.setattr(st, "is_yolo_active", lambda: False)
    monkeypatch.setattr(st, "_spawn_ws_send", lambda client, message: None)
    return st


def _cron_slot(state, key: str, *, project_bound: object, options: bool = False):
    slot = state.get_or_create_slot(key)
    meta = {} if project_bound is None else {"project_bound": project_bound}
    text = f"# Cron Job Result: job\n\n{SECRET}"
    if options:
        text += " [OPTIONS: ship it | hold]"
    slot.append("assistant", text, "msg msg-a", meta=meta)
    return slot


def _row(rows, key):
    return next(r for r in rows if r["key"] == key)


def _quotes(row) -> bool:
    return SECRET in row["last_message"] or SECRET in row["prompt_preview"]


def test_a_project_bound_row_is_blank_outside_the_owner_boundary(state):
    _cron_slot(state, "cron-a", project_bound=True, options=True)

    for rows in (
        state.serialize_slots(),
        state.serialize_slots(dashboard_user=True),
    ):
        row = _row(rows, "cron-a")
        assert not _quotes(row)
        assert row["options"] == [] and row["has_options"] is False

    owner = _row(state.serialize_slots(include_check_status=True), "cron-a")
    assert SECRET in owner["last_message"]
    assert owner["has_options"] is True and owner["options"]


def test_the_broadcast_views_split_the_same_way(state):
    _cron_slot(state, "cron-a", project_bound=True)
    bare, ws, owner = state.serialize_slot_views(owner=True)

    assert not _quotes(_row(bare, "cron-a"))
    assert not _quotes(_row(ws, "cron-a"))
    assert SECRET in _row(owner, "cron-a")["last_message"]
    # The derived owner view equals a full owner pass, field for field.
    assert (
        _row(owner, "cron-a")["last_message"]
        == _row(state.serialize_slots(include_check_status=True), "cron-a")["last_message"]
    )


def test_an_unstamped_cron_row_is_withheld(state):
    """Unknown provenance is not evidence of an unbound run."""
    _cron_slot(state, "cron-b", project_bound=None)
    assert not _quotes(_row(state.serialize_slots(dashboard_user=True), "cron-b"))


def test_an_explicitly_unbound_cron_row_is_unchanged(state):
    _cron_slot(state, "cron-c", project_bound=False)
    assert SECRET in _row(state.serialize_slots(dashboard_user=True), "cron-c")["last_message"]


def test_a_follow_up_in_a_project_bound_slot_is_withheld(state):
    slot = _cron_slot(state, "cron-d", project_bound=False)
    slot.append("user", f"why did it say {SECRET}?", "msg msg-u")
    assert not _quotes(_row(state.serialize_slots(dashboard_user=True), "cron-d"))


def test_an_ordinary_chat_slot_is_unchanged(state):
    slot = state.get_or_create_slot("chat-x")
    slot.append("assistant", SECRET, "msg")
    assert SECRET in _row(state.serialize_slots(), "chat-x")["last_message"]
