"""Ending a typing is never held back by the presence rate limit.

A sender's tab reports ``typing`` on a keystroke and ``viewing`` the moment its
draft is sent, usually well inside the per-connection spacing. That step down
must reach every collaborator at once; any other early report stays refused.
"""

from __future__ import annotations

from kiro_crew.shared_crew.collab import ACTIVITY_MIN_INTERVAL_SECONDS, FRAME_PRESENCE, Activity
from kiro_crew.shared_crew.contracts import (
    DisplaySnapshot,
    Principal,
    PrincipalKind,
    Provenance,
    principal_id_for,
)
from kiro_crew.shared_crew.presence import REFUSED_RATE, PresenceRegistry, handle_activity_frame


def _person(login: str) -> Principal:
    return Principal(
        id=principal_id_for("tailscale", "tailnet", login),
        kind=PrincipalKind.HUMAN,
        provider="tailscale",
        issuer="tailnet",
        subject=login,
        provenance=Provenance.TAILSCALE_SERVE,
        display=DisplaySnapshot(name=login, login=login),
    )


ALICE = _person("alice@example.test")
BOB = _person("bob@example.test")


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _State:
    def __init__(self, clock: _Clock) -> None:
        self._slots = {"s1": object(), "s2": object()}
        self._shared_crew_presence = PresenceRegistry(clock=clock)
        self.frames: list[tuple[str, dict]] = []

    def broadcast_ws(self, msg_type: str, data: dict) -> None:
        self.frames.append((msg_type, data))


def _frame(activity: str, session_key: str = "s1") -> dict:
    return {"session_key": session_key, "activity": activity}


def test_a_send_right_after_a_keystroke_clears_typing_for_collaborators():
    clock = _Clock()
    state = _State(clock)
    handle_activity_frame(state, "ws:bob", BOB, _frame("viewing"), dashboard_user=True)
    assert handle_activity_frame(
        state, "ws:a", ALICE, _frame("typing"), dashboard_user=True
    ).accepted
    state.frames.clear()

    clock.now += 0.1
    result = handle_activity_frame(state, "ws:a", ALICE, _frame("viewing"), dashboard_user=True)

    assert result.accepted
    msg_type, frame = state.frames[-1]
    assert msg_type == FRAME_PRESENCE
    alice = [e for e in frame["entries"] if e["principal"]["id"] == ALICE.id]
    assert [e["activity"] for e in alice] == ["viewing"]


def test_only_the_step_down_from_typing_skips_the_spacing():
    clock = _Clock()
    registry = PresenceRegistry(clock=clock)
    assert registry.report("tab", ALICE, "s1", Activity.TYPING).accepted
    clock.now += 0.1
    assert registry.report("tab", ALICE, "s1", Activity.VIEWING).accepted
    # Typing again inside the spacing is refused: two frames per interval at most.
    clock.now += 0.1
    again = registry.report("tab", ALICE, "s1", Activity.TYPING)
    assert not again.accepted and again.reason == REFUSED_RATE
    assert registry.entries("s1")[0].activity is Activity.VIEWING
    # A second step down with no typing in between is an ordinary early report.
    assert registry.report("tab", ALICE, "s1", Activity.IDLE).reason == REFUSED_RATE
    clock.now += ACTIVITY_MIN_INTERVAL_SECONDS
    assert registry.report("tab", ALICE, "s1", Activity.TYPING).accepted


def test_typing_then_another_session_inside_the_spacing_stays_refused():
    clock = _Clock()
    registry = PresenceRegistry(clock=clock)
    assert registry.report("tab", ALICE, "s1", Activity.TYPING).accepted
    clock.now += 0.1
    moved = registry.report("tab", ALICE, "s2", Activity.VIEWING)
    assert not moved.accepted and moved.reason == REFUSED_RATE
