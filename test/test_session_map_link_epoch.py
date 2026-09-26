"""``SessionMap.link_epoch``: the lock-free fence a coroutine re-checks after probing
channel links off the event loop (the folder steering write's commit-time guard).
"""

from __future__ import annotations

import pytest

from kiro_crew.messaging.link import ChannelLink
from kiro_crew.session_map import SessionMap


@pytest.fixture()
def tmp_path(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.session_map._KIRO_SESSIONS_DIR", tmp_path / "kiro")
    return tmp_path


def _map(tmp_path) -> SessionMap:
    return SessionMap()


def test_a_slack_link_moves_the_epoch(tmp_path):
    sessions = _map(tmp_path)
    before = sessions.link_epoch()
    sessions.set_slack_link("dashboard:chat-1", "1700000000.000100", "C123")
    assert sessions.link_epoch() != before


def test_a_mirror_link_moves_the_epoch(tmp_path):
    sessions = _map(tmp_path)
    before = sessions.link_epoch()
    sessions.set_mirror_link(
        "dashboard:chat-1",
        ChannelLink(channel_type="discord", channel_id="123", thread_id="456"),
    )
    assert sessions.link_epoch() != before


def test_re_registering_an_identical_slack_binding_leaves_the_epoch_alone(tmp_path):
    """Slack re-registers a thread's binding on every turn; an unchanged binding
    is not a new link, so it must not refuse a concurrent steering write."""
    sessions = _map(tmp_path)
    sessions.set_slack_link("dashboard:chat-1", "1700000000.000100", "C123")
    before = sessions.link_epoch()
    sessions.set_slack_link("dashboard:chat-1", "1700000000.000100", "C123")
    assert sessions.link_epoch() == before


def test_a_plain_session_write_leaves_the_epoch_alone(tmp_path):
    """Only link writes move it, so unrelated session traffic never refuses a
    steering write."""
    sessions = _map(tmp_path)
    before = sessions.link_epoch()
    sessions.set("dashboard:chat-1", "sid-1")
    assert sessions.link_epoch() == before


def test_the_dashboard_session_manager_forwards_the_epoch():
    """``DashboardState.sessions`` is a ``SessionManager``, not a ``SessionMap``:
    the folder write's probe calls ``link_epoch`` on it, so the manager must
    forward it (a missing method fails closed and refuses every tool write)."""
    from types import SimpleNamespace

    from kiro_crew.session import SessionManager

    manager = SessionManager.__new__(SessionManager)
    manager._session_map = SimpleNamespace(link_epoch=lambda: 7)
    assert manager.link_epoch() == 7
