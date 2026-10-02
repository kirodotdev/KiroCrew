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


_KEY = "dashboard:chat-1"


def test_another_sessions_link_write_leaves_this_sessions_epoch_alone(tmp_path):
    """The fence is per session: a Slack or mirror link landing on an unrelated
    session must not refuse a steering write the person already approved for
    this one."""
    sessions = _map(tmp_path)
    before = sessions.link_epoch(_KEY)
    sessions.set_slack_link("dashboard:chat-2", "1700000000.000200", "C999")
    sessions.set_mirror_link(
        "dashboard:chat-3",
        ChannelLink(channel_type="discord", channel_id="9", thread_id="10"),
    )
    assert sessions.link_epoch(_KEY) == before
    assert sessions.link_epoch("dashboard:chat-2") != 0
    assert sessions.link_epoch("dashboard:chat-3") != 0


def test_a_slack_link_moves_the_epoch(tmp_path):
    sessions = _map(tmp_path)
    before = sessions.link_epoch(_KEY)
    sessions.set_slack_link(_KEY, "1700000000.000100", "C123")
    assert sessions.link_epoch(_KEY) != before


def test_a_mirror_link_moves_the_epoch(tmp_path):
    sessions = _map(tmp_path)
    before = sessions.link_epoch(_KEY)
    sessions.set_mirror_link(
        _KEY,
        ChannelLink(channel_type="discord", channel_id="123", thread_id="456"),
    )
    assert sessions.link_epoch(_KEY) != before


def test_re_registering_an_identical_slack_binding_leaves_the_epoch_alone(tmp_path):
    """Slack re-registers a thread's binding on every turn; an unchanged binding
    is not a new link, so it must not refuse a concurrent steering write."""
    sessions = _map(tmp_path)
    sessions.set_slack_link(_KEY, "1700000000.000100", "C123")
    before = sessions.link_epoch(_KEY)
    sessions.set_slack_link(_KEY, "1700000000.000100", "C123")
    assert sessions.link_epoch(_KEY) == before


_DISCORD = ChannelLink(channel_type="discord", channel_id="123", thread_id="456")


def test_re_registering_an_identical_mirror_binding_leaves_the_epoch_alone(tmp_path):
    """The dispatcher rebinds a channel-born session's own conversation on every
    inbound turn; the same binding again is not a new link."""
    sessions = _map(tmp_path)
    sessions.set_mirror_link(_KEY, _DISCORD, accepts_inbound=True)
    before = sessions.link_epoch(_KEY)
    sessions.set_mirror_link(_KEY, _DISCORD, accepts_inbound=True)
    assert sessions.link_epoch(_KEY) == before


def test_re_registering_a_slack_binding_through_the_mirror_path_leaves_the_epoch_alone(
    tmp_path,
):
    slack = ChannelLink(channel_type="slack", channel_id="C123", thread_id="1700000000.000100")
    sessions = _map(tmp_path)
    sessions.set_mirror_link(_KEY, slack)
    before = sessions.link_epoch(_KEY)
    sessions.set_mirror_link(_KEY, slack)
    assert sessions.link_epoch(_KEY) == before


@pytest.mark.parametrize("change", ["target", "inbound"])
def test_a_changed_mirror_binding_moves_the_epoch(tmp_path, change):
    sessions = _map(tmp_path)
    sessions.set_mirror_link(_KEY, _DISCORD)
    before = sessions.link_epoch(_KEY)
    if change == "target":
        sessions.set_mirror_link(
            _KEY,
            ChannelLink(channel_type="discord", channel_id="123", thread_id="789"),
        )
    else:
        sessions.set_mirror_link(_KEY, _DISCORD, accepts_inbound=True)
    assert sessions.link_epoch(_KEY) != before


def test_a_plain_session_write_leaves_the_epoch_alone(tmp_path):
    """Only link writes move it, so unrelated session traffic never refuses a
    steering write."""
    sessions = _map(tmp_path)
    before = sessions.link_epoch(_KEY)
    sessions.set(_KEY, "sid-1")
    assert sessions.link_epoch(_KEY) == before


def test_the_dashboard_session_manager_forwards_the_epoch():
    """``DashboardState.sessions`` is a ``SessionManager``, not a ``SessionMap``:
    the folder write's probe calls ``link_epoch`` on it, so the manager must
    forward it (a missing method fails closed and refuses every tool write)."""
    from types import SimpleNamespace

    from kiro_crew.session import SessionManager

    manager = SessionManager.__new__(SessionManager)
    manager._session_map = SimpleNamespace(link_epoch=lambda key: 7 if key == _KEY else 0)
    assert manager.link_epoch(_KEY) == 7


def test_a_deleted_session_drops_its_epoch_entry(tmp_path):
    """The epoch map is bounded by the sessions in the map: a link/delete cycle
    must not leave a retained entry behind for every key it ever saw."""
    sessions = _map(tmp_path)
    for n in range(5):
        key = f"dashboard:chat-cycle-{n}"
        sessions.set_slack_link(key, f"1700000000.00{n}100", "C123")
        sessions.delete(key)
    assert sessions._link_epochs == {}


def test_a_recreated_session_never_reads_an_epoch_recorded_before_its_delete(tmp_path):
    """Epoch values are map-wide monotonic, so a key deleted and linked again
    cannot land back on the value a stale probe recorded (no ABA)."""
    sessions = _map(tmp_path)
    sessions.set_slack_link(_KEY, "1700000000.000100", "C123")
    recorded = sessions.link_epoch(_KEY)
    sessions.delete(_KEY)
    sessions.set_slack_link("dashboard:chat-other", "1700000000.000300", "C777")
    sessions.set_slack_link(_KEY, "1700000000.000400", "C123")
    assert sessions.link_epoch(_KEY) != recorded
