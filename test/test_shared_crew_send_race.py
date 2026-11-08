"""First submit wins: two collaborators sending against one session state.

* Of two sends against the same revision, exactly one is accepted. The other is
  refused before any queue, steer or turn with a ``send_race`` conflict naming
  the winner's server-derived principal and message id.
* A retry of the winner under its idempotency key answers the recorded send,
  including after the winner's response was lost, and never accepts twice.
* Send now is a new submission at the current revision: it enters fair
  admission, may queue behind the running turn, and never steers.

Races are driven with explicit events and ``asyncio.gather``; nothing sleeps.
"""

from __future__ import annotations

import asyncio
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.sections_shared_crew import effective_limits, shared_crew_config_from
from kiro_crew.shared_crew import admission_entry as entry
from kiro_crew.shared_crew import admission_gate as ag
from kiro_crew.shared_crew import send_race, work_store
from kiro_crew.shared_crew.admission import Liveness, OperationState, ReasonCode
from kiro_crew.shared_crew.collab import FRAME_SEND_STATE, SERVER_FRAMES
from kiro_crew.shared_crew.containment import ContainmentReport, SliceManager
from kiro_crew.shared_crew.contracts import (
    ACTOR_REQUEST_KEY,
    ActorContext,
    DisplaySnapshot,
    EnforcementLevel,
    Principal,
    PrincipalKind,
    Provenance,
    Transport,
    principal_id_for,
)
from kiro_crew.shared_crew.crew_mode import LOCAL_BOOTSTRAP_SUBJECTS
from kiro_crew.shared_crew.identity import local_operator_principal
from kiro_crew.shared_crew.send_race import Conflict, Granted, Replay, SendRaceRegistry
from kiro_crew.shared_crew.typing_lease import same_person
from kiro_crew.shared_crew.work_store import WorkStore
from kiro_crew.taskq.store import TaskStore
from kiro_crew.testing.clock import ManualClock

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


# -- the registry ------------------------------------------------------------------


def _registry() -> SendRaceRegistry:
    clock = ManualClock(start=1_000.0)
    return SendRaceRegistry(clock=clock.time, settle_wait_seconds=BOUND)


@pytest.mark.asyncio
async def test_two_sends_on_one_revision_accept_exactly_one():
    reg = _registry()
    first = await reg.claim("s", principal=ALICE, base_revision=0, idempotency_key="a1")
    assert isinstance(first, Granted)
    # Bob's claim waits for Alice's to settle; it cannot decide on a pending one.
    second = asyncio.ensure_future(reg.claim("s", principal=BOB, base_revision=0))
    await asyncio.sleep(0)
    assert not second.done()
    won = reg.commit(first.claim, message_id="m-alice")
    lost = await asyncio.wait_for(second, BOUND)
    assert isinstance(lost, Conflict)
    assert lost.winner == won
    assert lost.winner.principal.id == ALICE.id and lost.winner.message_id == "m-alice"
    assert lost.current_revision == 1 == reg.state("s")["revision"]


@pytest.mark.asyncio
async def test_a_send_refused_for_another_reason_does_not_win_the_race():
    reg = _registry()
    first = await reg.claim("s", principal=ALICE, base_revision=0)
    second = asyncio.ensure_future(reg.claim("s", principal=BOB, base_revision=0))
    await asyncio.sleep(0)
    assert isinstance(first, Granted)
    reg.release(first.claim)
    granted = await asyncio.wait_for(second, BOUND)
    assert isinstance(granted, Granted)
    assert reg.state("s")["revision"] == 0


@pytest.mark.asyncio
async def test_stale_and_reordered_submissions():
    reg = _registry()
    a = await reg.claim("s", principal=ALICE, base_revision=0)
    assert isinstance(a, Granted)
    reg.commit(a.claim, message_id="a")
    # Bob's tab still shows revision 0: stale, so it loses to Alice's send.
    stale = await reg.claim("s", principal=BOB, base_revision=0)
    assert isinstance(stale, Conflict) and stale.winner.message_id == "a"
    # Bob at revision 1 has seen it and is accepted.
    current = await reg.claim("s", principal=BOB, base_revision=1)
    assert isinstance(current, Granted)
    reg.commit(current.claim, message_id="b")
    # Alice sends from a tab that missed Bob's send: the earliest unseen send by
    # someone else is named, never her own.
    reordered = await reg.claim("s", principal=ALICE, base_revision=0)
    assert isinstance(reordered, Conflict) and reordered.winner.message_id == "b"
    # One person's own tabs never race each other.
    own = await reg.claim("s", principal=BOB, base_revision=1)
    assert isinstance(own, Granted)


@pytest.mark.asyncio
async def test_one_operator_s_local_subjects_never_race_each_other():
    """The race and the typing lease agree on who counts as one person.

    Two machine-local bootstrap subjects are the one operator of this machine,
    so the lease holds one lease across them and the race accepts a send under
    either against a send the other made.
    """
    app, startup = (local_operator_principal(s) for s in LOCAL_BOOTSTRAP_SUBJECTS[:2])
    assert app.id != startup.id and same_person(app, startup)
    reg = _registry()
    first = await reg.claim("s", principal=app, base_revision=0)
    assert isinstance(first, Granted)
    reg.commit(first.claim, message_id="from-app")
    # The operator's other local token sends from a tab that still shows 0.
    stale = await reg.claim("s", principal=startup, base_revision=0)
    assert isinstance(stale, Granted)


@pytest.mark.asyncio
async def test_a_retry_of_the_winner_replays_and_accepts_nothing():
    reg = _registry()
    a = await reg.claim("s", principal=ALICE, base_revision=0, idempotency_key="k-1")
    assert isinstance(a, Granted)
    won = reg.commit(a.claim, message_id="a")
    # The response was lost; the tab retries with the same key and stale base.
    again = await reg.claim("s", principal=ALICE, base_revision=0, idempotency_key="k-1")
    assert isinstance(again, Replay) and again.accepted == won
    # A retry that raced the first send waits, then replays it.
    b = await reg.claim("s", principal=BOB, base_revision=1, idempotency_key="k-2")
    assert isinstance(b, Granted)
    racing_retry = asyncio.ensure_future(
        reg.claim("s", principal=BOB, base_revision=1, idempotency_key="k-2")
    )
    await asyncio.sleep(0)
    reg.commit(b.claim, message_id="b")
    replay = await asyncio.wait_for(racing_retry, BOUND)
    assert isinstance(replay, Replay) and replay.accepted.message_id == "b"
    assert reg.state("s")["revision"] == 2
    # The key is scoped to its principal: someone else's key never replays.
    other = await reg.claim("s", principal=ALICE, base_revision=2, idempotency_key="k-2")
    assert isinstance(other, Granted)


@pytest.mark.asyncio
async def test_a_send_without_a_base_is_not_compared_but_still_counts():
    reg = _registry()
    app = await reg.claim("s", principal=BOB, base_revision=None)
    assert isinstance(app, Granted)
    reg.commit(app.claim)
    lost = await reg.claim("s", principal=ALICE, base_revision=0)
    assert isinstance(lost, Conflict) and lost.winner.principal.id == BOB.id
    assert lost.winner.message_id.startswith("send_")


@pytest.mark.asyncio
async def test_after_a_restart_any_base_is_accepted_and_stale_tabs_still_lose():
    reg = _registry()
    a = await reg.claim("s", principal=ALICE, base_revision=7)
    assert isinstance(a, Granted)
    assert reg.commit(a.claim).revision == 8
    b = await reg.claim("s", principal=BOB, base_revision=7)
    assert isinstance(b, Conflict) and b.current_revision == 8
    future = await reg.claim("s", principal=BOB, base_revision=20)
    assert isinstance(future, Conflict)


@pytest.mark.asyncio
async def test_a_pending_claim_that_never_settles_bounds_the_wait():
    clock = ManualClock(start=0.0)
    reg = SendRaceRegistry(clock=clock.time, settle_wait_seconds=0.0)
    hold = asyncio.Event()
    claimed = asyncio.Event()

    async def alice_send():
        granted = await reg.claim("s", principal=ALICE, base_revision=0)
        assert isinstance(granted, Granted)
        claimed.set()
        await hold.wait()

    alice = asyncio.ensure_future(alice_send())
    await asyncio.wait_for(claimed.wait(), BOUND)
    lost = await reg.claim("s", principal=BOB, base_revision=0)
    assert isinstance(lost, Conflict) and lost.winner.principal.id == ALICE.id
    hold.set()
    await asyncio.wait_for(alice, BOUND)


@pytest.mark.asyncio
async def test_a_claim_whose_handler_ends_unrecorded_is_given_back():
    reg = _registry()

    async def refused_send():
        granted = await reg.claim("s", principal=ALICE, base_revision=0)
        assert isinstance(granted, Granted)
        return granted.claim

    claim = await asyncio.wait_for(asyncio.ensure_future(refused_send()), BOUND)
    await asyncio.sleep(0)  # the done callback runs on the next loop pass
    assert claim.settled.is_set() and reg._sessions["s"].pending is None
    after = await reg.claim("s", principal=BOB, base_revision=0)
    assert isinstance(after, Granted)
    # One task's second send finds its own earlier claim still pending and
    # takes it back rather than waiting on itself.
    again = await reg.claim("s", principal=BOB, base_revision=0)
    assert isinstance(again, Granted) and again.claim is not after.claim


def test_idle_sessions_are_bounded(monkeypatch):
    monkeypatch.setattr(send_race, "MAX_SESSIONS", 3)
    reg = _registry()
    for n in range(5):
        reg._session(f"s{n}")
    assert list(reg._sessions) == ["s2", "s3", "s4"]


def test_the_send_state_frame_is_a_server_frame():
    assert FRAME_SEND_STATE in SERVER_FRAMES
    assert send_race.base_revision_of(None) is None
    assert send_race.base_revision_of(3) == 3
    for bad in (True, -1, "3", 1.5):
        with pytest.raises(send_race.BaseRevisionError):
            send_race.base_revision_of(bad)


# -- the chat send handler ---------------------------------------------------------


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


def _report() -> ContainmentReport:
    return ContainmentReport(
        platform="linux",
        available=True,
        reason="",
        cpu=True,
        memory=True,
        pids=True,
        level=EnforcementLevel.BEST_EFFORT,
        hard_ready=True,
        slices_enabled=True,
    )


@pytest.fixture
def installed(tmp_path, monkeypatch):
    clock = ManualClock(start=70_000.0)
    (tmp_path / "svc").mkdir()
    raw = {"admission_enabled": True, "principal_concurrency": 1, "crew_concurrency": 2}
    store = WorkStore(
        TaskStore(tmp_path / "svc" / "t.db", clock=clock.time).open(), clock=clock.time
    )
    svc = ag.AdmissionService(
        store,
        limits=effective_limits(shared_crew_config_from(raw), auto_crew_concurrency=8),
        clock=clock.time,
        containment_probe=_report,
        slices=SliceManager(
            runner=lambda cmd: subprocess.CompletedProcess(cmd, 0, "", ""),
            systemctl="/usr/bin/systemctl",
        ),
        liveness=lambda ref: Liveness.STOPPED,
    )

    async def _current():
        return svc

    monkeypatch.setattr(ag, "current_service", _current)
    monkeypatch.setattr(ag, "_SERVICE", svc)
    yield svc
    entry._PENDING_SLOTS.clear()
    work_store.set_change_listener(None)


def _client(state) -> TestClient:
    from kiro_crew.dashboard.chat_handlers import api_chat
    from kiro_crew.shared_crew.routes_collab import api_shared_crew_send_state

    app = web.Application(middlewares=[_actor_middleware()])
    app["state"] = state
    app.router.add_post("/api/chat", api_chat)
    app.router.add_get("/api/shared-crew/send-state", api_shared_crew_send_state)
    return TestClient(TestServer(app))


def _user_rows(slot) -> list[dict]:
    return [m for m in slot.messages if m.get("role") == "user"]


async def _post(client, who: str, body: dict, key: str = ""):
    headers = {"X-Test-Actor": who}
    if key:
        headers["Idempotency-Key"] = key
    resp = await client.post("/api/chat?ws=1", json=body, headers=headers)
    return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_two_collaborators_racing_at_the_host_accept_exactly_one(dash_state):
    slot = dash_state.get_or_create_slot("s1")
    run_chat = AsyncMock()
    with (
        patch("kiro_crew.dashboard.chat_handlers._run_chat", new=run_chat),
        patch("kiro_crew.dashboard.chat_handlers._maybe_auto_title", new=AsyncMock()),
    ):
        async with _client(dash_state) as client:
            results = await asyncio.wait_for(
                asyncio.gather(
                    _post(
                        client, "alice", {"slot": "s1", "message": "from alice", "base_revision": 0}
                    ),
                    _post(client, "bob", {"slot": "s1", "message": "from bob", "base_revision": 0}),
                ),
                BOUND,
            )
            state_resp = await client.get("/api/shared-crew/send-state?session_key=s1")
            send_state = await state_resp.json()
    statuses = sorted(status for status, _ in results)
    assert statuses == [200, 409]
    (won_status, won), (lost_status, lost) = sorted(results, key=lambda r: r[0])
    rows = _user_rows(slot)
    assert len(rows) == 1
    winner_text = rows[0]["content"]
    winner = ALICE if winner_text == "from alice" else BOB
    assert lost["code"] == ReasonCode.SEND_RACE.value
    assert lost["winner"]["principal"]["id"] == winner.id
    assert lost["winner"]["principal"]["display"]["name"] == winner.display.name
    assert lost["winner"]["message_id"] == rows[0]["meta"]["mid"]
    assert lost["current_revision"] == 1
    assert "issuer" not in lost["winner"]["principal"]
    assert run_chat.await_count == 1
    assert send_state["revision"] == 1
    assert send_state["last"]["principal"]["id"] == winner.id
    frames = [
        c.args for c in dash_state.broadcast_ws.call_args_list if c.args[0] == FRAME_SEND_STATE
    ]
    assert len(frames) == 1 and frames[0][1]["revision"] == 1


@pytest.mark.asyncio
async def test_lost_winner_response_then_retry_runs_once(dash_state):
    slot = dash_state.get_or_create_slot("s1")
    run_chat = AsyncMock()
    with (
        patch("kiro_crew.dashboard.chat_handlers._run_chat", new=run_chat),
        patch("kiro_crew.dashboard.chat_handlers._maybe_auto_title", new=AsyncMock()),
    ):
        async with _client(dash_state) as client:
            body = {"slot": "s1", "message": "hello", "base_revision": 0}
            st1, first = await asyncio.wait_for(
                _post(client, "alice", body, "lost-key-0001"), BOUND
            )
            await asyncio.wait_for(asyncio.shield(slot.task), BOUND)
            # The tab never saw that answer and retries the same send.
            st2, again = await asyncio.wait_for(
                _post(client, "alice", body, "lost-key-0001"), BOUND
            )
            st3, dup = await asyncio.wait_for(_post(client, "alice", body, "lost-key-0001"), BOUND)
    assert st1 == 200 and first.get("replayed") is not True
    assert st2 == 200 and again["replayed"] is True and again["send_revision"] == 1
    assert st3 == 200 and dup["replayed"] is True
    assert len(_user_rows(slot)) == 1
    assert run_chat.await_count == 1


@pytest.mark.asyncio
async def test_a_refused_send_gives_its_claim_back(dash_state):
    dash_state.get_or_create_slot("s1")
    refusal = web.json_response({"error": "archived", "code": "relay_archive"}, status=409)
    with (
        patch("kiro_crew.dashboard.chat_handlers.relay_archive_refusal", return_value=refusal),
        patch("kiro_crew.dashboard.chat_handlers._maybe_auto_title", new=AsyncMock()),
    ):
        async with _client(dash_state) as client:
            st1, first = await _post(
                client, "alice", {"slot": "s1", "message": "a", "base_revision": 0}
            )
            assert st1 == 409 and first["code"] == "relay_archive"
            assert send_race.send_race_for(dash_state).state("s1")["revision"] == 0
            assert send_race.send_race_for(dash_state)._sessions["s1"].pending is None


@pytest.mark.asyncio
async def test_send_now_enters_admission_and_queues_without_steering(dash_state, installed):
    slot = dash_state.get_or_create_slot("s1")
    reg = send_race.send_race_for(dash_state)
    granted = await reg.claim("s1", principal=ALICE, base_revision=0)
    assert isinstance(granted, Granted)
    reg.commit(granted.claim, message_id="alice-first")
    busy = MagicMock()
    busy.done.return_value = False
    slot.task = busy
    steer = AsyncMock()
    with (
        patch("kiro_crew.dashboard.chat_handlers.steer_into_running_turn", new=steer),
        patch("kiro_crew.dashboard.chat_handlers._maybe_auto_title", new=AsyncMock()),
    ):
        async with _client(dash_state) as client:
            draft = {"slot": "s1", "message": "bob's draft", "steer": True}
            st1, lost = await _post(client, "bob", {**draft, "base_revision": 0}, "bob-key-00001")
            assert st1 == 409 and lost["winner"]["message_id"] == "alice-first"
            # Nothing of the losing draft reached the session.
            assert slot._queue == [] or len(slot._queue) == 0
            st2, now = await _post(
                client,
                "bob",
                {**draft, "base_revision": lost["current_revision"], "send_now": True},
                "bob-key-00002",
            )
    assert steer.await_count == 0
    assert st2 == 200 and now["queued"] is True and now.get("steered") is not True
    assert len(slot._queue) == 1
    work_ids = entry.queued_work_ids(slot._queue)
    assert len(work_ids) == 1
    assert installed._store.get_sync(work_ids[0]).state is OperationState.QUEUED
    assert reg.state("s1")["revision"] == 2


@pytest.mark.asyncio
async def test_a_losing_send_leaves_the_session_persona_alone(dash_state):
    slot = dash_state.get_or_create_slot("s1")
    reg = send_race.send_race_for(dash_state)
    granted = await reg.claim("s1", principal=ALICE, base_revision=0)
    assert isinstance(granted, Granted)
    reg.commit(granted.claim)
    async with _client(dash_state) as client:
        status, body = await _post(
            client,
            "bob",
            {"slot": "s1", "message": "x", "base_revision": 0, "color_theme": "custom-bob"},
        )
    assert status == 409 and body["code"] == "send_race"
    assert slot.color_theme != "custom-bob"


@pytest.mark.asyncio
async def test_a_losing_steer_never_steers(dash_state):
    slot = dash_state.get_or_create_slot("s1")
    reg = send_race.send_race_for(dash_state)
    granted = await reg.claim("s1", principal=ALICE, base_revision=0)
    assert isinstance(granted, Granted)
    reg.commit(granted.claim)
    busy = MagicMock()
    busy.done.return_value = False
    slot.task = busy
    steer = AsyncMock()
    with patch("kiro_crew.dashboard.chat_handlers.steer_into_running_turn", new=steer):
        async with _client(dash_state) as client:
            status, body = await _post(
                client, "bob", {"slot": "s1", "message": "x", "steer": True, "base_revision": 0}
            )
    assert status == 409 and body["code"] == "send_race"
    assert steer.await_count == 0 and len(slot._queue) == 0


@pytest.mark.asyncio
async def test_a_malformed_base_revision_is_refused(dash_state):
    dash_state.get_or_create_slot("s1")
    async with _client(dash_state) as client:
        status, body = await _post(
            client, "bob", {"slot": "s1", "message": "x", "base_revision": "1"}
        )
    assert status == 400 and body["code"] == "invalid_base_revision"
