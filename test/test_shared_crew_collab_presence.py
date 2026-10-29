"""Presence and typing: bound to the verified actor, folded per person, transient.

Drives :class:`PresenceRegistry` with an explicit clock, and the socket-facing
helpers with a recording state double, so every expiry and rate-limit edge is
deterministic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.shared_crew import presence as presence_mod
from kiro_crew.shared_crew.collab import (
    ACTIVITY_MIN_INTERVAL_SECONDS,
    FRAME_PRESENCE,
    PRESENCE_TTL_SECONDS,
    Activity,
)
from kiro_crew.shared_crew.contracts import (
    DisplaySnapshot,
    Principal,
    PrincipalKind,
    Provenance,
    principal_id_for,
)
from kiro_crew.shared_crew.presence import (
    REFUSED_FULL,
    REFUSED_MALFORMED,
    REFUSED_RATE,
    PresenceRegistry,
    handle_activity_frame,
    parse_activity_frame,
    release_connection,
    sweep_and_broadcast,
)


def _person(login: str, name: str = "") -> Principal:
    return Principal(
        id=principal_id_for("tailscale", "tailnet", login),
        kind=PrincipalKind.HUMAN,
        provider="tailscale",
        issuer="tailnet",
        subject=login,
        provenance=Provenance.TAILSCALE_SERVE,
        display=DisplaySnapshot(name=name or login, login=login),
    )


ALICE = _person("alice@example.test", "Alice")
BOB = _person("bob@example.test", "Bob")


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def registry(clock: _Clock) -> PresenceRegistry:
    return PresenceRegistry(clock=clock, wall_clock=lambda: clock.now + 5_000_000)


def test_many_tabs_of_one_person_fold_into_one_entry(registry, clock):
    registry.report("tab-1", ALICE, "s1", Activity.VIEWING)
    registry.report("tab-2", ALICE, "s1", Activity.TYPING)
    registry.report("tab-3", BOB, "s1", Activity.VIEWING)

    entries = registry.entries("s1")

    assert [e.principal.id for e in entries] == [ALICE.id, BOB.id]
    alice = entries[0]
    assert alice.connections == 2
    # Typing on any tab is what the person is doing.
    assert alice.activity is Activity.TYPING
    assert entries[1].connections == 1


def test_entry_expires_without_refresh(registry, clock):
    registry.report("tab-1", ALICE, "s1", Activity.VIEWING)
    clock.now += PRESENCE_TTL_SECONDS - 0.5
    assert len(registry.entries("s1")) == 1

    clock.now += 1
    assert registry.entries("s1") == []
    assert len(registry._conns) == 0


def test_sweep_reports_the_sessions_it_emptied(registry, clock):
    registry.report("tab-1", ALICE, "s1", Activity.VIEWING)
    registry.report("tab-2", BOB, "s2", Activity.VIEWING)
    clock.now += PRESENCE_TTL_SECONDS
    assert registry.sweep() == ("s1", "s2")
    assert registry.sweep() == ()


def test_rate_limit_is_per_connection(registry, clock):
    assert registry.report("tab-1", ALICE, "s1", Activity.VIEWING).accepted
    second = registry.report("tab-1", ALICE, "s1", Activity.TYPING)
    assert not second.accepted
    assert second.reason == REFUSED_RATE
    # The refused frame changed nothing.
    assert registry.entries("s1")[0].activity is Activity.VIEWING
    # Another tab of the same person has its own budget.
    assert registry.report("tab-2", ALICE, "s1", Activity.TYPING).accepted

    clock.now += ACTIVITY_MIN_INTERVAL_SECONDS
    assert registry.report("tab-1", ALICE, "s1", Activity.IDLE).accepted


def test_disconnect_removes_only_that_tab(registry, clock):
    registry.report("tab-1", ALICE, "s1", Activity.VIEWING)
    registry.report("tab-2", ALICE, "s1", Activity.VIEWING)

    assert registry.disconnect("tab-1") == ("s1",)
    entries = registry.entries("s1")
    assert len(entries) == 1 and entries[0].connections == 1

    registry.disconnect("tab-2")
    assert registry.entries("s1") == []
    assert registry.disconnect("tab-2") == ()


def test_moving_to_another_session_leaves_the_first(registry, clock):
    registry.report("tab-1", ALICE, "s1", Activity.VIEWING)
    clock.now += ACTIVITY_MIN_INTERVAL_SECONDS
    result = registry.report("tab-1", ALICE, "s2", Activity.VIEWING)
    assert set(result.changed) == {"s1", "s2"}
    assert registry.entries("s1") == []
    assert [e.principal.id for e in registry.entries("s2")] == [ALICE.id]


def test_refresh_with_same_activity_needs_no_frame(registry, clock):
    registry.report("tab-1", ALICE, "s1", Activity.VIEWING)
    clock.now += ACTIVITY_MIN_INTERVAL_SECONDS
    result = registry.report("tab-1", ALICE, "s1", Activity.VIEWING)
    assert result.accepted and result.changed == ()


def test_registry_is_bounded_and_never_evicts_a_live_entry(clock):
    registry = PresenceRegistry(clock=clock, max_connections=2)
    registry.report("tab-1", ALICE, "s1", Activity.VIEWING)
    registry.report("tab-2", BOB, "s1", Activity.VIEWING)
    refused = registry.report("tab-3", BOB, "s1", Activity.VIEWING)
    assert refused.reason == REFUSED_FULL
    assert len(registry._conns) == 2


def test_frame_parser_reads_only_session_and_activity():
    frame = {
        "type": "shared_crew.activity",
        "session_key": "s1",
        "activity": "typing",
        "principal": BOB.to_public_dict(),
        "draft": "secret plan",
        "text": "keystrokes",
    }
    assert parse_activity_frame(frame) == ("s1", Activity.TYPING)
    assert parse_activity_frame({"session_key": "s1", "activity": "shouting"}) is None
    assert parse_activity_frame({"session_key": "", "activity": "typing"}) is None
    assert parse_activity_frame({"session_key": "x" * 300, "activity": "typing"}) is None
    assert parse_activity_frame(["s1", "typing"]) is None


class _State:
    """The gateway surface the socket helpers touch: open slots and a broadcast."""

    def __init__(self, slots=("s1", "s2")) -> None:
        self._slots = {k: object() for k in slots}
        self.frames: list[tuple[str, dict]] = []

    def broadcast_ws(self, msg_type: str, data: dict) -> None:
        self.frames.append((msg_type, data))


def test_socket_frame_is_bound_to_the_connection_actor_not_the_frame():
    state = _State()
    forged = {"session_key": "s1", "activity": "viewing", "principal": BOB.to_public_dict()}

    result = handle_activity_frame(state, "ws:1", ALICE, forged, dashboard_user=True)

    assert result.accepted
    msg_type, frame = state.frames[-1]
    assert msg_type == FRAME_PRESENCE
    assert [e["principal"]["id"] for e in frame["entries"]] == [ALICE.id]
    assert frame["entries"][0]["principal"]["display"]["name"] == "Alice"


def test_socket_frame_refused_for_app_tokens_and_unknown_sessions():
    state = _State()
    frame = {"session_key": "s1", "activity": "viewing"}
    assert handle_activity_frame(state, "ws:1", ALICE, frame, dashboard_user=False).reason == (
        REFUSED_MALFORMED
    )
    unknown = {"session_key": "not-open", "activity": "viewing"}
    assert not handle_activity_frame(state, "ws:1", ALICE, unknown, dashboard_user=True).accepted
    assert state.frames == []


def test_release_and_sweep_push_the_emptied_session(monkeypatch):
    clock = _Clock()
    state = _State()
    state._shared_crew_presence = PresenceRegistry(clock=clock)
    handle_activity_frame(
        state, "ws:1", ALICE, {"session_key": "s1", "activity": "viewing"}, dashboard_user=True
    )
    handle_activity_frame(
        state, "ws:2", BOB, {"session_key": "s2", "activity": "viewing"}, dashboard_user=True
    )
    state.frames.clear()

    release_connection(state, "ws:1")
    assert [(t, f["session_key"], f["entries"]) for t, f in state.frames] == [
        (FRAME_PRESENCE, "s1", [])
    ]

    state.frames.clear()
    clock.now += PRESENCE_TTL_SECONDS
    sweep_and_broadcast(state)
    assert [(t, f["session_key"], f["entries"]) for t, f in state.frames] == [
        (FRAME_PRESENCE, "s2", [])
    ]


def test_presence_is_never_written_to_disk(tmp_path: Path, monkeypatch):
    """No draft or keystroke can reach storage: the registry holds no text field.

    The whole lifecycle runs with KIROCREW_HOME at an empty temp dir and leaves
    it empty, and a frame's text fields do not appear in anything the registry
    exposes.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _State()
    frame = {"session_key": "s1", "activity": "typing", "draft": "TOP-SECRET-DRAFT"}
    handle_activity_frame(state, "ws:1", ALICE, frame, dashboard_user=True)
    release_connection(state, "ws:1")

    assert list(tmp_path.iterdir()) == []
    assert "TOP-SECRET-DRAFT" not in json.dumps(state.frames)
    registry = presence_mod.presence_for(state)
    assert "TOP-SECRET-DRAFT" not in repr(registry.__dict__)
