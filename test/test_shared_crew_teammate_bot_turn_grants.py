"""A team Crew teammate's turn on a Bot's thread takes none of the owner's approvals.

Who started a turn is carried with it (``attribution.teammate_started``): the
teammate's own request publishes it, a turn or drain that request starts inherits
it, and a queued entry carries it (``TEAMMATE_ORIGIN_KEY``) to the drain that runs
it later. The turn's open records it on the slot (``_turn_teammate_sent``), and
every rung that approves without asking reads that per request
(``approval_posture.teammate_bot_turn``), so a grant the owner adds mid-turn is
withheld too. The owner's own turns keep every grant.

The approval harness is the one ``test_card_turn_untrusted.py`` uses. It cannot
admit a real Bot turn (that needs the Bot's config and memory store), so the slot
becomes a Bot's thread once the turn is open, before the tool call; the rungs read
it per request, which is the property under test. The route tests publish the actor
the way ``identity.publish_actor`` does; ``/api/chat`` is pinned through the real
relay ingress in ``test_members_roster_relay_teammate.py``.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_runner_coverage_helpers import (
    _async_iter,
    _complete,
    _drive,
    _permission,
    _runner_state,
    _settle,
    _slot,
)
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_regenerate import (
    api_chat_slot_edit_resend,
    api_chat_slot_regenerate,
)
from kiro_crew.dashboard.slot_queue_repository import (
    TEAMMATE_ORIGIN_KEY,
    durable_queue_entries,
    sanitize_restored_queue,
)
from kiro_crew.shared_crew import attribution
from kiro_crew.shared_crew.contracts import (
    ActorContext,
    DisplaySnapshot,
    Principal,
    PrincipalKind,
    Provenance,
    Transport,
    principal_id_for,
)
from kiro_crew.shared_crew.identity import KIRO_ISSUER, PROVIDER_KIRO
from kiro_crew.testing.wait import async_wait_until

_LS = json.dumps({"command": "ls -la"})

# The sub-agent tests spawn through SubagentManager: pin the host-memory reading.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")

#: A teammate as ``identity.relay_principal`` builds one.
MATE = Principal(
    id=principal_id_for(PROVIDER_KIRO, KIRO_ISSUER, "kiro-user-ana"),
    kind=PrincipalKind.HUMAN,
    provider=PROVIDER_KIRO,
    issuer=KIRO_ISSUER,
    subject="kiro-user-ana",
    provenance=Provenance.KIRO_RELAY,
    display=DisplaySnapshot(name="Ana"),
)
#: A person admitted on the direct listener, who has the owner's access.
OWNER = Principal(
    id=principal_id_for("tailscale", "tailnet", "owner@example.test"),
    kind=PrincipalKind.HUMAN,
    provider="tailscale",
    issuer="tailnet",
    subject="owner@example.test",
    provenance=Provenance.TAILSCALE_SERVE,
    display=DisplaySnapshot(name="owner", login="owner@example.test"),
)


@pytest.fixture(autouse=True)
def _vouch_for_the_program():
    """The name-grant check resolves the real PATH; these tests measure the rung."""
    with (
        patch.object(chat_runner, "_name_grant_refusal_off_loop", new=AsyncMock(return_value=None)),
        patch.object(chat_runner, "_name_grant_refusal_for", new=AsyncMock(return_value=None)),
    ):
        yield


# --- the approval rungs ------------------------------------------------------


def _bot_slot(grant: str):
    slot = _slot("member-code-reviewer")
    slot.mode = "member"
    if grant == "trust":
        slot._trust = True
    elif grant == "trust_reads":
        slot._trust_reads = True
    elif grant == "trusted_pattern":
        slot._trusted_patterns = {"ls*"}
    elif grant == "scope":
        slot._trust_scope = "scope-1"
    return slot


def _script(client, slot, *, then=None) -> None:
    """One turn that opens as a chat, becomes the Bot's thread and asks for one tool."""
    calls = {"n": 0}

    async def _events():
        slot.mode = "member"
        if then is not None:
            then()
        yield _permission(tool_input=_LS)
        yield _complete()

    def _stream(*_args, **_kwargs):
        calls["n"] += 1
        return _events() if calls["n"] == 1 else _async_iter([_complete()])

    client.stream = MagicMock(side_effect=_stream)
    client.stream_command = MagicMock(side_effect=_stream)


async def _turn(state, client, slot, *, teammate: bool, grant: str) -> None:
    slot.mode = "chat"
    _script(client, slot)
    if grant == "yolo":
        state.is_yolo_active = MagicMock(return_value=True)
    with (
        patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0),
        patch.object(chat_runner, "safety_override") as override,
        attribution.teammate_origin(teammate),
    ):
        override.return_value.is_scope_active.return_value = grant == "scope"
        override.return_value.is_cli_launch = False
        await _drive(state, slot)


_GRANTS = ["trust", "trust_reads", "trusted_pattern", "scope", "yolo"]


@pytest.mark.asyncio
@pytest.mark.parametrize("grant", _GRANTS)
async def test_the_owners_turn_on_a_bot_still_uses_the_owners_grant(tmp_path, grant):
    """The control: without it the teammate tests below would pass for any reason."""
    state, client = _runner_state(tmp_path)
    slot = _bot_slot(grant)

    await _turn(state, client, slot, teammate=False, grant=grant)

    client.approve_tool.assert_awaited_once_with("req-cov-1")


@pytest.mark.asyncio
@pytest.mark.parametrize("grant", _GRANTS)
async def test_a_teammates_turn_on_a_bot_takes_none_of_the_owners_grants(tmp_path, grant):
    state, client = _runner_state(tmp_path)
    slot = _bot_slot(grant)

    await _turn(state, client, slot, teammate=True, grant=grant)

    client.approve_tool.assert_not_awaited()
    # The mark described the turn, and is gone once it has ended.
    assert slot._turn_teammate_sent is False
    # It reached the approval card, where a person decides.
    assert len([m for m in slot.messages if m.get("role") == "permission"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("teammate", [True, False])
async def test_trust_switched_on_mid_turn_is_withheld_from_a_teammates_turn(tmp_path, teammate):
    # The turn opens on normal approvals; the owner turns on Trust before the tool
    # call. Read per request, so the teammate's turn still asks and the owner's does not.
    state, client = _runner_state(tmp_path)
    slot = _bot_slot("")
    slot.mode = "chat"

    def _owner_turns_on_trust() -> None:
        slot._trust = True

    _script(client, slot, then=_owner_turns_on_trust)
    with (
        patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0),
        attribution.teammate_origin(teammate),
    ):
        await _drive(state, slot)

    if teammate:
        client.approve_tool.assert_not_awaited()
    else:
        client.approve_tool.assert_awaited_once_with("req-cov-1")


@pytest.mark.parametrize("teammate", [True, False])
def test_trust_switched_on_leaves_a_teammates_open_card_for_a_person(tmp_path, teammate):
    # Through the facade: the owner module runs on its globals.
    from kiro_crew.dashboard.chat_handlers import _sweep_pending_approvals

    async def _check() -> bool:
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _bot_slot("")
        slot._turn_teammate_sent = teammate
        future = asyncio.get_running_loop().create_future()
        slot._approval_futures["req-1"] = future
        with patch("kiro_crew.dashboard.chat_handlers.sel"):
            _sweep_pending_approvals(state, slot, "trust", lambda label: label)
        return future.done()

    assert asyncio.run(_check()) is (not teammate)


@pytest.mark.parametrize("teammate", [True, False])
def test_a_teammates_turn_stores_no_auto_policy_for_its_subagents(teammate):
    from kiro_crew.dashboard.chat_turn.tool_approval import _persistable_session_policy

    slot = _bot_slot("trust")
    slot._turn_teammate_sent = teammate

    assert _persistable_session_policy(slot, False) == ("" if teammate else "auto")


def test_a_teammates_turn_on_an_ordinary_session_is_not_narrowed():
    # Only a Bot's thread: a session's approvals are the person's own there.
    from kiro_crew.dashboard.approval_posture import teammate_bot_turn

    slot = _slot()
    slot._turn_teammate_sent = True

    assert teammate_bot_turn(slot) is False


# --- who started the turn, per route -----------------------------------------


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.push_slots_update = MagicMock()
    st.sessions.discard_conversation = AsyncMock(return_value=True)
    st.sessions.aflush = AsyncMock()
    st.sessions._session_map.get = MagicMock(return_value="")
    st.subagents = None
    return st


def _client(state, actor: Principal) -> TestClient:
    from kiro_crew.dashboard.chat_handlers import api_chat_slot_continue

    @web.middleware
    async def as_actor(request: web.Request, handler):
        attribution.bind_request_author(
            ActorContext(principal=actor, request_id="r", transport=Transport.HTTP)
        )
        return await handler(request)

    app = web.Application(middlewares=[as_actor])
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/edit-resend", api_chat_slot_edit_resend)
    app.router.add_post("/api/chat/slots/{slot}/regenerate", api_chat_slot_regenerate)
    app.router.add_post("/api/chat/slots/{slot}/continue", api_chat_slot_continue)
    return TestClient(TestServer(app))


def _recorder() -> tuple[list[bool], AsyncMock]:
    seen: list[bool] = []

    async def _turn(*_args, **_kwargs) -> None:
        seen.append(attribution.teammate_started())

    return seen, AsyncMock(side_effect=_turn)


def _conversation(state):
    slot = state.get_or_create_slot("s1")
    slot.mode = "member"
    slot.append("user", "owner asks", meta={"author": OWNER.to_public_dict()})
    slot.append("assistant", "answer")
    slot.drain()
    return slot


@pytest.mark.asyncio
@pytest.mark.parametrize(("actor", "expected"), [(MATE, True), (OWNER, False)])
async def test_regenerate_runs_as_whoever_pressed_it(state, actor, expected) -> None:
    _conversation(state)
    seen, run = _recorder()
    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=run):
        async with _client(state, actor) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 200, await resp.text()
            await async_wait_until(lambda: seen)
    assert seen == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize(("actor", "expected"), [(MATE, True), (OWNER, False)])
async def test_edit_and_resend_runs_as_whoever_sent_the_edit(state, actor, expected) -> None:
    _conversation(state)
    seen, run = _recorder()
    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=run):
        async with _client(state, actor) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 200, await resp.text()
            await async_wait_until(lambda: seen)
    assert seen == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize(("actor", "expected"), [(MATE, True), (OWNER, False)])
async def test_continue_queues_its_turn_as_whoever_pressed_it(state, actor, expected) -> None:
    slot = _conversation(state)
    started = AsyncMock(return_value=True)
    with (
        patch("kiro_crew.dashboard.chat_handlers._start_next_queued_turn", started),
        patch("kiro_crew.dashboard.chat_handlers.sel"),
    ):
        async with _client(state, actor) as client:
            resp = await client.post("/api/chat/slots/s1/continue")
            assert resp.status == 200, await resp.text()
    started.assert_awaited_once()
    assert (slot._queue[0].get(TEAMMATE_ORIGIN_KEY) is True) is expected


async def _drain(state, slot) -> list[bool]:
    seen: list[bool] = []

    def _spawn(_state, _slot, coro):
        seen.append(attribution.teammate_started())
        coro.close()
        return MagicMock()

    with patch.object(chat_runner, "spawn_guarded_turn", side_effect=_spawn):
        assert await chat_runner._start_next_queued_turn(state, slot) is True
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("queued_by_teammate", [True, False])
async def test_the_drain_runs_an_entry_as_whoever_queued_it(state, queued_by_teammate) -> None:
    # Queued in one context and drained in the OTHER: the entry decides, never the
    # context that happened to trigger the drain.
    from kiro_crew.dashboard.chat_delivery import queue_for_next_turn

    slot = state.get_or_create_slot("busy-bot")
    slot.mode = "member"
    with attribution.teammate_origin(queued_by_teammate):
        queue_for_next_turn(state, slot, "typed while the Bot was working")
    with attribution.teammate_origin(not queued_by_teammate):
        seen = await _drain(state, slot)

    assert seen == [queued_by_teammate]


def test_a_teammates_queued_entry_keeps_its_mark_across_a_restart() -> None:
    entry = {"id": "q1", "content": "typed while the Bot was working", TEAMMATE_ORIGIN_KEY: True}
    owners = {"id": "q2", "content": "the owner's"}

    restored = sanitize_restored_queue(durable_queue_entries([entry, owners]))

    assert [item.get(TEAMMATE_ORIGIN_KEY) for item in restored] == [True, None]


# --- sub-agents: a teammate's Bot turn starts none ---------------------------

_BOT = "dashboard:member-code-reviewer"


def _mark(teammate: bool):
    """A Bot thread's running turn on ``_BOT``, a teammate's or the owner's."""
    from kiro_crew.dashboard.approval_posture import bot_turn

    slot = _bot_slot("")
    slot._turn_teammate_sent = teammate
    return bot_turn(_BOT, slot)


@pytest.mark.asyncio
@pytest.mark.parametrize("teammate", [True, False])
async def test_a_running_turn_is_seen_by_its_session_key(tmp_path, teammate):
    # The spawn gate holds a session key, not a slot.
    from kiro_crew.dashboard.approval_posture import teammate_turn_running

    state, client = _runner_state(tmp_path)
    slot = _bot_slot("")
    slot.mode = "chat"
    seen: list[bool] = []

    def _record() -> None:
        seen.append(teammate_turn_running(chat_runner.effective_session_key(slot)))

    _script(client, slot, then=_record)
    with (
        patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0),
        attribution.teammate_origin(teammate),
    ):
        await _drive(state, slot)

    assert seen == [teammate]
    assert teammate_turn_running(chat_runner.effective_session_key(slot)) is False


@pytest.mark.parametrize("teammate", [True, False])
def test_a_bot_turn_is_seen_by_session_key_only_while_it_runs(teammate):
    from kiro_crew.dashboard.approval_posture import teammate_turn_running

    with _mark(teammate):
        running = teammate_turn_running(_BOT)

    assert running is teammate
    assert teammate_turn_running(_BOT) is False


def _manager():
    from subagent_helpers import _mock_ctx_builder, _mock_sessions

    from kiro_crew.subagent import SubagentManager

    return SubagentManager(
        sessions=_mock_sessions(),
        ctx_builder=_mock_ctx_builder(),
        on_spawn_approval=AsyncMock(return_value=True),
        on_done=AsyncMock(),
        # The owner's YOLO is on: an ordinary spawn starts without asking.
        is_yolo=lambda: True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("teammate", [True, False])
async def test_a_teammates_bot_turn_starts_no_sub_agent(teammate):
    # Refused like a card turn's spawn, so none of the paths a child takes its
    # approvals from (YOLO, parent trust, the sub-agent approval callbacks) is reached.
    manager = _manager()
    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel") as audit,
        _mark(teammate),
    ):
        info = manager.spawn("do the thing", parent_session_key=_BOT)
    task = manager._tasks.get(info.id)
    if task is not None:
        await asyncio.gather(task, return_exceptions=True)

    outcomes = [
        c.kwargs.get("outcome") for c in audit.return_value.log_tool_invocation.call_args_list
    ]
    if teammate:
        assert info.done is True and "teammate" in info.error
        assert info.id not in manager._tasks
        assert "rejected_teammate_bot_turn" in outcomes
    else:
        assert info.error == ""
        assert "rejected_teammate_bot_turn" not in outcomes


@pytest.mark.parametrize("teammate", [True, False])
def test_a_spawn_request_keeps_the_teammate_turn_it_arrived_from(teammate):
    # A Stop while the spawn handler awaits ends the turn; the arrival mark the card
    # refusals read (CARD_TURN_AT_ARRIVAL) holds the teammate's turn too, so every
    # card-turn reader refuses it: the spawn gate, the delegation routes, the cron
    # tools and the lost-wave report.
    from kiro_crew.dashboard import card_send

    class _Request(dict):
        headers = {"X-Session-Key": _BOT}

    req = _Request()

    async def _handler(_request):
        return None

    async def _arrive() -> None:
        with _mark(teammate):
            await card_send.card_turn_arrival_middleware(req, _handler)

    asyncio.run(_arrive())

    assert card_send.card_turn_seen(req, _BOT) is teammate
    # Only for the session the request came from.
    assert card_send.card_turn_seen(req, "dashboard:other") is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("teammate", "path", "status"),
    [
        (True, "/api/spawn/run-1/steer", 403),
        (True, "/api/spawn/run-1/continue", 403),
        (True, "/api/session-control/send", 403),
        # A route that only stops work stays open.
        (True, "/api/session-control/stop", 200),
        (False, "/api/spawn/run-1/steer", 200),
    ],
)
async def test_a_teammates_bot_turn_hands_no_work_on(teammate, path, status):
    # The card turn's delegation routes: a steer or continue of a running sub-agent,
    # a send into another session. Each would run on the owner's approvals.
    from kiro_crew.dashboard import card_send

    @web.middleware
    async def _internal(request, handler):
        request["internal_auth"] = True
        return await handler(request)

    async def _ok(_request):
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[_internal, card_send.card_turn_delegation_middleware])
    for route in ("/api/spawn/{id}/steer", "/api/spawn/{id}/continue"):
        app.router.add_post(route, _ok)
    app.router.add_post("/api/session-control/send", _ok)
    app.router.add_post("/api/session-control/stop", _ok)
    with patch("kiro_crew.dashboard.chat_handlers.sel"), _mark(teammate):
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(path, headers={"X-Session-Key": _BOT})
            body = await resp.json()
    assert resp.status == status, body
    if status == 403:
        assert body["code"] == "card_turn_delegation_refused"


@pytest.mark.parametrize(
    ("teammate", "tool", "refused"),
    [
        (True, "cron_add", True),
        (True, "cron_trigger", True),
        # Reads and stops pass, as for a card turn.
        (True, "cron_list", False),
        (False, "cron_add", False),
    ],
)
def test_a_teammates_bot_turn_schedules_no_cron_through_the_cron_tools(teammate, tool, refused):
    from kiro_crew.dashboard import card_send

    class _Request(dict):
        headers = {"X-Session-Key": _BOT}

    with patch("kiro_crew.dashboard.chat_handlers.sel"), _mark(teammate):
        refusal = card_send.card_turn_cron_tool_refusal(_Request(), _BOT, tool)

    assert (refusal is not None) is refused
    if refused:
        assert refusal.status == 403
        assert json.loads(refusal.body)["code"] == "card_turn_delegation_refused"


@pytest.mark.asyncio
@pytest.mark.parametrize("teammate", [True, False])
async def test_a_teammates_lost_wave_report_is_ignored(teammate):
    # A pre-gate failure in a teammate's spawn_run wave reports the members lost;
    # recording them would start a synthetic continuation nobody marked as theirs.
    manager = _manager()
    with patch("kiro_crew.subagent.sel"), _mark(teammate):
        manager.record_lost_submission("w1", 2, "backend unavailable", parent_session_key=_BOT)

    assert ("w1" in manager._batch_submitted) is (not teammate)


@pytest.mark.asyncio
async def test_after_a_teammates_turn_the_owners_trust_reaches_the_threads_sub_agents(tmp_path):
    # The teammate's turn ends and the thread is idle: a Trust the owner presses now
    # stores the "auto" policy its sub-agents read, as on any thread of the owner's.
    from kiro_crew.dashboard.approval_posture import trust_reaches_subagents

    state, client = _runner_state(tmp_path)
    slot = _bot_slot("")
    slot.mode = "chat"
    _script(client, slot)
    with (
        patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0),
        attribution.teammate_origin(True),
    ):
        await _drive(state, slot)

    slot._trust = True
    assert trust_reaches_subagents(slot) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("teammate", "path", "status"),
    [
        (True, "/api/member/self/notes", 403),
        (True, "/api/member/self/update", 403),
        (True, "/api/member/self/goals/report", 403),
        (True, "/api/member/self/goals/key-result", 403),
        # A read of its own goals stays open.
        (True, "/api/member/self/goals/read", 200),
        # The owner's turn keeps every pre-allowed write.
        (False, "/api/member/self/notes", 200),
        (False, "/api/member/self/update", 200),
    ],
)
async def test_a_teammates_bot_turn_cannot_rewrite_the_bots_own_record(teammate, path, status):
    # These kirocrew-mate tools run without a prompt (spec_grants._MATE_AUTO_GRANTS),
    # so the gateway route is where a teammate's turn is held.
    from kiro_crew.dashboard import card_send

    @web.middleware
    async def _internal(request, handler):
        request["internal_auth"] = True
        return await handler(request)

    async def _ok(_request):
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[_internal, card_send.card_turn_delegation_middleware])
    for route in (
        "/api/member/self/notes",
        "/api/member/self/update",
        "/api/member/self/goals/report",
        "/api/member/self/goals/key-result",
        "/api/member/self/goals/read",
    ):
        app.router.add_post(route, _ok)
    with patch("kiro_crew.dashboard.chat_handlers.sel"), _mark(teammate):
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(path, json={}, headers={"X-Session-Key": _BOT})
            body = await resp.json()
    assert resp.status == status, body
    if status == 403:
        assert body["code"] == "restricted_turn_self_write_refused"


@pytest.mark.asyncio
@pytest.mark.parametrize("grant", ["trust", "yolo"])
@pytest.mark.parametrize("teammate", [True, False])
async def test_a_teammates_declined_auto_approval_is_audited(tmp_path, grant, teammate):
    # The owner's Trust or YOLO is on; the teammate's call asks, and the decline is
    # written. The owner's own call auto-approves and writes no decline.
    state, client = _runner_state(tmp_path)
    slot = _bot_slot("trust" if grant == "trust" else "")
    slot.mode = "chat"
    _script(client, slot)
    if grant == "yolo":
        state.is_yolo_active = MagicMock(return_value=True)
    audit = MagicMock()
    with (
        patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0),
        patch.object(chat_runner, "sel", return_value=audit),
        patch.object(chat_runner, "safety_override") as override,
        attribution.teammate_origin(teammate),
    ):
        override.return_value.is_cli_launch = False
        slot._empty_response_retries = 2
        await chat_runner._run_chat(state, slot, "hello")
        await _settle(slot)

    declines = [
        c.kwargs
        for c in audit.log_tool_invocation.call_args_list
        if c.kwargs.get("outcome") == "auto_approve_declined"
    ]
    if teammate:
        assert [d["metadata"]["reason"] for d in declines] == ["teammate_turn"]
    else:
        assert declines == []
        client.approve_tool.assert_awaited_once_with("req-cov-1")


@pytest.mark.parametrize("teammate", [True, False])
def test_a_self_write_keeps_the_teammate_turn_it_arrived_from(teammate):
    # The request waits in auth while the teammate's turn is stopped and an owner's
    # turn starts on the same session: the mark recorded at arrival still refuses it.
    from kiro_crew.dashboard import card_send

    class _Request(dict):
        headers = {"X-Session-Key": _BOT}
        method = "POST"
        path = "/api/member/self/notes"

    req = _Request()
    req["internal_auth"] = True
    reached: list[bool] = []

    async def _record(_request):
        reached.append(True)
        return None

    async def _arrive_then_owner_turn() -> object:
        with _mark(teammate):
            await card_send.card_turn_arrival_middleware(req, lambda r: asyncio.sleep(0))
        with _mark(False), patch("kiro_crew.dashboard.chat_handlers.sel"):
            return await card_send.card_turn_delegation_middleware(req, _record)

    response = asyncio.run(_arrive_then_owner_turn())

    if teammate:
        assert response.status == 403
        assert reached == []
    else:
        assert reached == [True]


_SELF_WRITE_ROUTES = (
    "/api/member/self/notes",
    "/api/member/self/update",
    "/api/member/self/goals/report",
    "/api/member/self/goals/key-result",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _SELF_WRITE_ROUTES)
async def test_an_owner_card_turn_keeps_the_bots_own_record_writes(path):
    # The self-write hold is the teammate's alone: a Dashboard card's turn on the
    # owner's Bot keeps main's pre-allowed writes (rfc-dashboard-card-send.md).
    from kiro_crew.dashboard import card_send

    @web.middleware
    async def _internal(request, handler):
        request["internal_auth"] = True
        return await handler(request)

    async def _ok(_request):
        return web.json_response({"ok": True})

    app = web.Application(
        middlewares=[
            card_send.card_turn_arrival_middleware,
            _internal,
            card_send.card_turn_delegation_middleware,
        ]
    )
    for route in _SELF_WRITE_ROUTES:
        app.router.add_post(route, _ok)
    app.router.add_post("/api/session-control/send", _ok)
    with patch("kiro_crew.dashboard.chat_handlers.sel"), card_send.card_turn(_BOT):
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(path, json={}, headers={"X-Session-Key": _BOT})
            body = await resp.json()
            # Control: the same card turn is still held off a delegation route.
            held = await client.post("/api/session-control/send", headers={"X-Session-Key": _BOT})
            held_body = await held.json()
    assert resp.status == 200, body
    assert held.status == 403, held_body
    assert held_body["code"] == "card_turn_delegation_refused"


def test_an_owner_card_turn_self_write_in_flight_after_stop_is_allowed():
    # The card turn stops while its write waits in auth: the card mark recorded at
    # arrival is not a teammate's, so the write reaches its handler.
    from kiro_crew.dashboard import card_send

    class _Request(dict):
        headers = {"X-Session-Key": _BOT}
        method = "POST"
        path = "/api/member/self/notes"

    req = _Request()
    req["internal_auth"] = True
    reached: list[bool] = []

    async def _record(_request):
        reached.append(True)
        return None

    async def _arrive_then_stop() -> object:
        with card_send.card_turn(_BOT):
            await card_send.card_turn_arrival_middleware(req, lambda r: asyncio.sleep(0))
        with patch("kiro_crew.dashboard.chat_handlers.sel"):
            return await card_send.card_turn_delegation_middleware(req, _record)

    response = asyncio.run(_arrive_then_stop())

    assert req[card_send.CARD_TURN_AT_ARRIVAL] is True
    assert response is None
    assert reached == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("teammate", [True, False])
async def test_a_teammates_steer_takes_the_running_bot_turn_off_the_owners_yolo(
    tmp_path, monkeypatch, teammate
):
    # The owner's turn runs on a normal Bot under global YOLO; the teammate's
    # /api/chat passes the door check and is steered in. A turn that carries the
    # teammate's text is one YOLO does not reach.
    from steer_requeue_helpers import _running_slot

    from kiro_crew.dashboard.chat_delivery import STEER_STEERED, steer_into_running_turn

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    # A plain key: a member-thread key is created only by its endpoint. The mode is
    # what makes it a Bot's thread for ``teammate_bot_turn``.
    slot = _running_slot(state)
    slot.mode = "member"
    slot._turn_teammate_sent = False
    acp = MagicMock()
    acp.supports_steer = True
    acp.steer = AsyncMock(return_value=True)
    slot._acp_client = acp

    with (
        patch("kiro_crew.dashboard.chat_handlers.sel"),
        patch("kiro_crew.dashboard.state.safety_override") as override,
        attribution.teammate_origin(teammate),
    ):
        override.return_value.is_cli_launch = False
        outcome = await steer_into_running_turn(
            state, slot, "from ana", user_origin=True, admission={}
        )
        reaches = state.yolo_reaches(slot)

    assert outcome == STEER_STEERED
    assert slot._turn_teammate_sent is teammate
    assert reaches is (not teammate)
