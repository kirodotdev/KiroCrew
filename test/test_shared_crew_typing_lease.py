"""The composer typing lease: one collaborator types, everyone else is read-only.

Driven by a fake clock. The lease is bound to the verified connection principal,
renewed by typing reports, and released on send, on a cleared draft, after the
idle window, and when the typing tab disconnects. The host refuses a
non-holder's send with ``409 composer_locked``; a single operator never locks.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.sections_shared_crew import SHARED_CREW_BOUNDS, shared_crew_config_from
from kiro_crew.shared_crew import typing_lease
from kiro_crew.shared_crew.admission import REASON_HTTP_STATUS, ReasonCode
from kiro_crew.shared_crew.collab import FRAME_TYPING_LEASE, SERVER_FRAMES
from kiro_crew.shared_crew.contracts import (
    DisplaySnapshot,
    Principal,
    PrincipalKind,
    Provenance,
    principal_id_for,
)
from kiro_crew.shared_crew.identity import (
    ACTOR_REQUEST_KEY,
    ActorContext,
    Transport,
    local_operator_principal,
)
from kiro_crew.shared_crew.presence import (
    PresenceRegistry,
    handle_activity_frame,
    release_connection,
)
from kiro_crew.shared_crew.typing_lease import (
    END_CLEARED,
    END_DISCONNECTED,
    END_IDLE,
    END_SENT,
    TypingLeaseRegistry,
)
from kiro_crew.testing.wait import await_until

BOUND = 30.0


def _person(login: str) -> Principal:
    return Principal(
        id=principal_id_for("tailscale", "tailnet", login),
        kind=PrincipalKind.HUMAN,
        provider="tailscale",
        issuer="tailnet",
        subject=login,
        provenance=Provenance.TAILSCALE_SERVE,
        display=DisplaySnapshot(name=login.split("@")[0], login=login),
    )


ALICE = _person("alice@example.test")
BOB = _person("bob@example.test")
PEOPLE = {"alice": ALICE, "bob": BOB}


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _registry(clock: _Clock, idle: float = 10) -> TypingLeaseRegistry:
    return TypingLeaseRegistry(idle_seconds=idle, clock=clock, wall_clock=clock)


class _State:
    """A gateway stand-in: open slots, a fake-clock presence and lease registry."""

    def __init__(self, clock: _Clock) -> None:
        self._slots = {"s1": object(), "s2": object()}
        self._shared_crew_presence = PresenceRegistry(clock=clock, wall_clock=clock)
        self._shared_crew_typing_lease = _registry(clock)
        self.frames: list[tuple[str, dict]] = []

    def broadcast_ws(self, msg_type: str, data: dict) -> None:
        self.frames.append((msg_type, data))

    def lease_frames(self) -> list[dict]:
        return [f for t, f in self.frames if t == FRAME_TYPING_LEASE]


def _frame(activity: str, session_key: str = "s1", **extra) -> dict:
    return {"session_key": session_key, "activity": activity, **extra}


# -- the registry -----------------------------------------------------------------


def test_the_first_typer_gets_the_lease_and_a_second_is_locked():
    clock = _Clock()
    reg = _registry(clock)
    change = reg.typed("s1", "ws:a", ALICE)
    assert change.sessions == {"s1": ""}
    assert reg.holder("s1") == ALICE
    # Bob types too: he never takes a held lease, and his send is held.
    assert reg.typed("s1", "ws:b", BOB).sessions == {}
    assert reg.holder("s1") == ALICE
    assert reg.blocks("s1", BOB) == ALICE
    assert reg.blocks("s1", ALICE) is None
    # Another session is its own lease.
    assert reg.blocks("s2", BOB) is None


def test_renewal_extends_and_the_idle_window_releases():
    clock = _Clock()
    reg = _registry(clock)
    reg.typed("s1", "ws:a", ALICE)
    clock.now += 9
    reg.typed("s1", "ws:a", ALICE)
    clock.now += 9
    assert reg.holder("s1") == ALICE, "a renewal 9 s ago keeps the lease"
    assert reg.state("s1")["expires_at"] == pytest.approx(clock.now + 1)
    clock.now += 1
    change = reg.sweep()
    assert change.sessions == {"s1": END_IDLE}
    assert reg.holder("s1") is None and reg.blocks("s1", BOB) is None
    # Free again: Bob may take it now.
    reg.typed("s1", "ws:b", BOB)
    assert reg.holder("s1") == BOB


def test_a_read_after_the_idle_window_leaves_the_end_for_the_sweep():
    clock = _Clock()
    reg = _registry(clock)
    reg.typed("s1", "ws:a", ALICE)
    clock.now += 10
    assert reg.holder("s1") is None and reg.state("s1")["holder"] is None
    # The sweep still sees the end, so the idle timer broadcasts it.
    assert reg.sweep().sessions == {"s1": END_IDLE}


def test_release_on_send_on_clear_and_on_disconnect():
    clock = _Clock()
    reg = _registry(clock)
    reg.typed("s1", "ws:a", ALICE)
    assert reg.sent("s1", BOB).sessions == {}, "someone else's send never releases it"
    assert reg.sent("s1", ALICE).sessions == {"s1": END_SENT}
    assert reg.holder("s1") is None

    reg.typed("s1", "ws:a", ALICE)
    assert reg.cleared("s1", "ws:b").sessions == {}
    assert reg.cleared("s1", "ws:a").sessions == {"s1": END_CLEARED}

    reg.typed("s1", "ws:a", ALICE)
    assert reg.disconnected("ws:a").sessions == {"s1": END_DISCONNECTED}
    assert reg.holder("s1") is None


def test_one_person_with_many_tabs_is_one_holder():
    clock = _Clock()
    reg = _registry(clock)
    reg.typed("s1", "ws:a1", ALICE)
    reg.typed("s1", "ws:a2", ALICE)
    # Her other tab is never locked out.
    assert reg.blocks("s1", ALICE) is None
    # Closing one typing tab keeps the lease while another still drafts.
    assert reg.disconnected("ws:a1").sessions == {}
    assert reg.holder("s1") == ALICE
    # A send from any of her tabs is the holder's send.
    assert reg.sent("s1", ALICE).sessions == {"s1": END_SENT}


def test_a_personal_crew_never_locks():
    clock = _Clock()
    reg = TypingLeaseRegistry(idle_seconds=10, clock=clock, wall_clock=clock, owner_id="")
    app = local_operator_principal("local-app")
    startup = local_operator_principal("local-startup")
    assert app.id != startup.id
    reg.typed("s1", "ws:1", app)
    # The operator's other local subject is the same person: nothing is held.
    assert reg.blocks("s1", startup) is None
    assert reg.blocks("s1", app) is None
    assert set(reg.state("s1")["holder_ids"]) >= {app.id, startup.id}
    assert reg.sent("s1", startup).sessions == {"s1": END_SENT}


def test_unknown_and_system_principals_never_hold():
    clock = _Clock()
    reg = _registry(clock)
    assert reg.typed("s1", "ws:x", Principal.unknown()).sessions == {}
    assert reg.holder("s1") is None


def test_the_state_shape_carries_no_text_and_no_issuer():
    clock = _Clock()
    reg = _registry(clock)
    empty = reg.state("s1")
    assert empty["holder"] is None and empty["holder_ids"] == []
    assert empty["idle_seconds"] == 10 and empty["expires_at"] is None
    reg.typed("s1", "ws:a", ALICE)
    held = reg.state("s1")
    assert set(held) == {
        "schema_version",
        "session_key",
        "holder",
        "holder_ids",
        "expires_at",
        "idle_seconds",
        "ended",
    }
    assert held["holder"] == ALICE.to_public_dict()
    assert "issuer" not in held["holder"] and "subject" not in held["holder"]
    assert ALICE.id in held["holder_ids"]


def test_the_idle_window_is_a_bounded_config_value():
    assert shared_crew_config_from({}).typing_idle_seconds == 10
    assert SHARED_CREW_BOUNDS["typing_idle_seconds"] == (3, 60)
    assert shared_crew_config_from({"typing_idle_seconds": 1}).typing_idle_seconds == 3
    assert shared_crew_config_from({"typing_idle_seconds": 600}).typing_idle_seconds == 60
    assert shared_crew_config_from({"typing_idle_seconds": 25}).typing_idle_seconds == 25
    assert typing_lease.clamp_idle_seconds("x") == 10
    assert typing_lease.clamp_idle_seconds(True) == 10
    assert typing_lease.clamp_idle_seconds(99) == 60


def test_the_frame_and_the_refusal_code_are_registered():
    assert FRAME_TYPING_LEASE in SERVER_FRAMES
    assert ReasonCode.COMPOSER_LOCKED.value == typing_lease.CODE_COMPOSER_LOCKED
    assert REASON_HTTP_STATUS[ReasonCode.COMPOSER_LOCKED] == 409


# -- the socket side ----------------------------------------------------------------


def test_activity_frames_drive_the_lease_and_broadcast_it():
    clock = _Clock()
    state = _State(clock)
    handle_activity_frame(state, "ws:b", BOB, _frame("viewing"), dashboard_user=True)
    handle_activity_frame(state, "ws:a", ALICE, _frame("typing"), dashboard_user=True)
    frames = state.lease_frames()
    assert frames[-1]["holder"]["id"] == ALICE.id
    assert frames[-1]["session_key"] == "s1"
    # Her typing hold runs out (a plain step down): the draft is still there.
    clock.now += 5
    handle_activity_frame(state, "ws:a", ALICE, _frame("viewing"), dashboard_user=True)
    assert state._shared_crew_typing_lease.holder("s1") == ALICE
    # She clears the draft: released at once, even inside the spacing.
    clock.now += 0.1
    handle_activity_frame(
        state, "ws:a", ALICE, _frame("viewing", draft="empty"), dashboard_user=True
    )
    assert state.lease_frames()[-1]["holder"] is None
    assert state.lease_frames()[-1]["ended"] == END_CLEARED


def test_a_typing_report_with_an_empty_draft_never_acquires():
    clock = _Clock()
    state = _State(clock)
    handle_activity_frame(
        state, "ws:a", ALICE, _frame("typing", draft="empty"), dashboard_user=True
    )
    assert state._shared_crew_typing_lease.holder("s1") is None


def test_a_forged_holder_field_is_ignored():
    clock = _Clock()
    state = _State(clock)
    forged = _frame(
        "typing", holder=BOB.to_public_dict(), principal=BOB.to_public_dict(), text="secret"
    )
    handle_activity_frame(state, "ws:a", ALICE, forged, dashboard_user=True)
    lease = state._shared_crew_typing_lease
    assert lease.holder("s1") == ALICE
    assert "secret" not in repr(state.frames)
    # An app socket (not a dashboard user) never reaches the lease.
    handle_activity_frame(state, "ws:app", BOB, _frame("typing", "s2"), dashboard_user=False)
    assert lease.holder("s2") is None
    # A session the gateway has not opened never grows the registry.
    handle_activity_frame(state, "ws:a", ALICE, _frame("typing", "nope"), dashboard_user=True)
    assert lease.holder("nope") is None


def test_a_disconnect_releases_the_lease_for_everyone():
    clock = _Clock()
    state = _State(clock)
    handle_activity_frame(state, "ws:a", ALICE, _frame("typing"), dashboard_user=True)
    release_connection(state, "ws:a")
    last = state.lease_frames()[-1]
    assert last["holder"] is None and last["ended"] == END_DISCONNECTED


def test_a_rate_limited_typing_report_does_not_renew():
    clock = _Clock()
    state = _State(clock)
    handle_activity_frame(state, "ws:a", ALICE, _frame("typing"), dashboard_user=True)
    clock.now += 0.5
    # Inside the presence spacing: refused, so the lease keeps its old time.
    assert not handle_activity_frame(
        state, "ws:a", ALICE, _frame("typing"), dashboard_user=True
    ).accepted
    clock.now += 9.5
    assert state._shared_crew_typing_lease.sweep().sessions == {"s1": END_IDLE}


@pytest.mark.asyncio
async def test_the_idle_timer_unlocks_without_another_report():
    state = _State(_Clock())
    real = TypingLeaseRegistry(idle_seconds=0.05)
    state._shared_crew_typing_lease = real
    idle_ended = asyncio.Event()

    def broadcast(msg_type: str, data: dict) -> None:
        state.frames.append((msg_type, data))
        if msg_type == FRAME_TYPING_LEASE and data.get("ended") == END_IDLE:
            idle_ended.set()

    state.broadcast_ws = broadcast  # type: ignore[method-assign]
    real.typed("s1", "ws:a", ALICE)
    typing_lease.arm_idle_timer(state, real)
    await await_until(idle_ended, what="the idle timer ending the lease")
    assert real.holder("s1") is None
    assert state.lease_frames()[-1]["ended"] == END_IDLE


# -- the host -------------------------------------------------------------------------


def _actor_middleware():
    @web.middleware
    async def mw(request: web.Request, handler):
        who = PEOPLE[request.headers.get("X-Test-Actor", "alice")]
        request[ACTOR_REQUEST_KEY] = ActorContext(
            principal=who, request_id="r", transport=Transport.HTTP
        )
        request["is_dashboard_user"] = True
        return await handler(request)

    return mw


@pytest.fixture
def dash_state(tmp_path, monkeypatch):
    from chat_test_helpers import _make_state

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.broadcast_ws_owners = MagicMock()
    st.push_slots_update = MagicMock()
    st.owner_id = ""
    return st


def _client(state) -> TestClient:
    from kiro_crew.dashboard.chat_handlers import api_chat
    from kiro_crew.shared_crew.routes_collab import api_shared_crew_typing_lease

    app = web.Application(middlewares=[_actor_middleware()])
    app["state"] = state
    app.router.add_post("/api/chat", api_chat)
    app.router.add_get("/api/shared-crew/typing-lease", api_shared_crew_typing_lease)
    return TestClient(TestServer(app))


async def _post(client, who: str, body: dict):
    resp = await client.post("/api/chat?ws=1", json=body, headers={"X-Test-Actor": who})
    return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_a_non_holder_send_is_refused_and_the_holder_send_releases(dash_state):
    slot = dash_state.get_or_create_slot("s1")
    lease = typing_lease.typing_lease_for(dash_state)
    lease.typed("s1", "ws:a", ALICE)
    run_chat = AsyncMock()
    with (
        patch("kiro_crew.dashboard.chat_handlers._run_chat", new=run_chat),
        patch("kiro_crew.dashboard.chat_handlers._maybe_auto_title", new=AsyncMock()),
    ):
        async with _client(dash_state) as client:
            status, body = await _post(client, "bob", {"slot": "s1", "message": "bob's draft"})
            assert status == 409
            assert body["code"] == "composer_locked"
            assert body["holder"]["id"] == ALICE.id
            assert body["holder"]["display"]["name"] == "alice"
            assert "issuer" not in body["holder"]
            assert body["lease"]["holder"]["id"] == ALICE.id
            # Nothing of Bob's send reached the session.
            assert [m for m in slot.messages if m.get("role") == "user"] == []
            # A reconnecting tab reads the holder.
            got = await client.get("/api/shared-crew/typing-lease?session_key=s1")
            assert (await got.json())["holder"]["id"] == ALICE.id
            # Alice sends: accepted, and the lease ends for everyone.
            status, _ = await _post(client, "alice", {"slot": "s1", "message": "alice's"})
            assert status == 200
            await asyncio.wait_for(asyncio.shield(slot.task), BOUND)
            assert lease.holder("s1") is None
            ended = [
                c.args[1]
                for c in dash_state.broadcast_ws.call_args_list
                if c.args[0] == FRAME_TYPING_LEASE
            ]
            assert ended[-1]["holder"] is None and ended[-1]["ended"] == END_SENT
            # Free now: Bob's send goes through.
            status, _ = await _post(client, "bob", {"slot": "s1", "message": "bob's draft"})
            assert status == 200
    assert run_chat.await_count == 2


@pytest.mark.asyncio
async def test_a_steer_into_a_running_turn_is_never_held(dash_state):
    slot = dash_state.get_or_create_slot("s1")
    typing_lease.typing_lease_for(dash_state).typed("s1", "ws:a", ALICE)
    busy = MagicMock()
    busy.done.return_value = False
    slot.task = busy
    steer = AsyncMock(return_value=web.json_response({"ok": True, "steered": True}))
    with patch("kiro_crew.dashboard.chat_handlers.steer_into_running_turn", new=steer):
        async with _client(dash_state) as client:
            status, body = await _post(
                client, "bob", {"slot": "s1", "message": "stop that", "steer": True}
            )
    assert body.get("code") != "composer_locked"
    assert steer.await_count == 1, (status, body)


@pytest.mark.asyncio
async def test_a_steer_flag_with_no_running_turn_is_still_held(dash_state):
    dash_state.get_or_create_slot("s1")
    typing_lease.typing_lease_for(dash_state).typed("s1", "ws:a", ALICE)
    async with _client(dash_state) as client:
        status, body = await _post(client, "bob", {"slot": "s1", "message": "x", "steer": True})
    assert status == 409 and body["code"] == "composer_locked"


@pytest.mark.asyncio
async def test_after_the_idle_window_the_send_race_is_the_backstop(dash_state):
    clock = _Clock()
    dash_state.get_or_create_slot("s1")
    lease = TypingLeaseRegistry(idle_seconds=10, clock=clock, wall_clock=clock)
    dash_state._shared_crew_typing_lease = lease
    lease.typed("s1", "ws:a", ALICE)
    clock.now += 11
    from kiro_crew.shared_crew import send_race

    reg = send_race.send_race_for(dash_state)
    granted = await reg.claim("s1", principal=ALICE, base_revision=0)
    reg.commit(granted.claim, message_id="alice-first")
    async with _client(dash_state) as client:
        status, body = await _post(
            client, "bob", {"slot": "s1", "message": "x", "base_revision": 0}
        )
    assert status == 409 and body["code"] == "send_race"
