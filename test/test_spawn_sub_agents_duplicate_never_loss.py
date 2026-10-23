"""A result ``spawn_sub_agents`` returns reaches its parent at least once, for every parent.

The blocking tool returns its members' results inline, and each member's completion
event is the other way the result reaches the parent. Every completion route -- a
dashboard tab, a channel or session parent, a cron parent -- skips an announce whose
every carried member is marked ``_collected_inline``, and the tool sends that mark only
after the reply frame carrying the member was written to the parent's pipe. So a reply
the parent never received -- torn, crashed before the write, cancelled, cut past the
response cap, or answered into the broker by a pooled backend -- marks nothing, and its
members are delivered by their completions: a duplicate at worst, never a loss.

The first half drives the real tool through the real stdio loop and pins what is
marked and when; the second drives the real completion consumer (``on_done``) and pins
which announces are skipped; a seeded property test composes the two halves' real
decision functions over random waves.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import random
import sys
import threading
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp_shared_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _framing_undetected,
    _LoopHarness,
    _tools_call,
)
from test_gateway_subagent_wiring import _Done, harness, make_info

import kiro_crew.mcp_shared as mcp_shared
from kiro_crew import install_liveness, mcp_core
from kiro_crew.context_management import COMPLETION_KEEP_DEFAULT_CHARS
from kiro_crew.mcp_tools import spawn as spawn_tools
from kiro_crew.subagent import SubagentInfo
from kiro_crew.testing.wait import async_wait_until, wait_until
from kiro_crew.validation import build_tool_response

#: One parent of each kind the completion routes tell apart.
PARENTS = ["dashboard:tab1", "cron:job1:run1", "slack:C1:1700000000.1"]

MARK = "/api/spawn/mark-collected"

#: The real writer, taken before any test runs: the loop harness patches
#: ``mcp_shared.respond``, and the facade mirrors that patch into the owner.
_REAL_RESPOND = mcp_shared.respond


# ── the mark: real tool, real stdio loop ─────────────────────────────────────


class _Gateway:
    """The gateway routes the tool calls, recording every mark in one event log."""

    def __init__(self, ids: list[str], events: list[Any], result_of=None) -> None:
        self._spawned = iter(ids)
        self._result_of = result_of or (lambda aid: f"{aid} finished")
        self._events = events
        self._lock = threading.Lock()

    def post(self, path: str, body: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        if path == "/api/spawn":
            return {"id": next(self._spawned)}
        if path == MARK:
            with self._lock:
                self._events.append(("mark", list(body["ids"]), body["parent_session"]))
        return {}

    def get(self, path: str, **_kw: Any) -> dict[str, Any]:
        aid = path.rsplit("/", 1)[1]
        return {"done": True, "agent": aid, "result": self._result_of(aid)}

    def marks(self) -> list[Any]:
        with self._lock:
            return [e for e in self._events if e[0] == "mark"]


@pytest.fixture
def events() -> list[Any]:
    return []


def _drive(monkeypatch, gateway: _Gateway, parent: str, members: int, *, respond, until) -> Any:
    """Run one ``spawn_sub_agents`` call of *members* through the real loop."""
    harness_ = _LoopHarness(monkeypatch, mcp_core._call_tool_deferrable)
    monkeypatch.setattr(mcp_shared, "respond", respond)
    call = _tools_call(1, "spawn_sub_agents")
    call["params"]["arguments"] = {"agents": [{"prompt": f"t{i}"} for i in range(members)]}
    with (
        patch.object(mcp_core, "_post", side_effect=gateway.post),
        patch.object(mcp_core, "_get", side_effect=gateway.get),
        patch.object(mcp_core, "sel"),
        patch.dict("os.environ", {"KIROCREW_SESSION_KEY": parent}),
    ):
        try:
            harness_.send(call)
            until(harness_)
        finally:
            harness_.close()
    return harness_


def _answered(events: list[Any], req_id: Any) -> bool:
    return any(e[0] == "frame" and e[1] == req_id for e in events)


def _then_a_ping_is_answered(harness_: Any, events: list[Any]) -> None:
    """One dispatch thread answers both, in order: once the ping is answered, the
    call's own answer, and anything that follows it, is done."""
    harness_.send({"jsonrpc": "2.0", "id": 2, "method": "ping"})
    wait_until(lambda: _answered(events, 2), describe=lambda: f"events: {events!r}")


def _recording_respond(events: list[Any], written: dict[Any, Any]):
    def respond(req_id: Any, result: Any, error: dict | None = None) -> bool:
        outcome = written.get(req_id, True)
        events.append(("frame", req_id))
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return respond


@pytest.mark.parametrize("parent", PARENTS)
def test_a_written_reply_marks_its_members_after_the_frame(monkeypatch, events, parent):
    gateway = _Gateway(["a1", "a2"], events)

    def until(harness_: Any) -> None:
        wait_until(lambda: gateway.marks(), describe=lambda: f"events: {events!r}")

    _drive(monkeypatch, gateway, parent, 2, respond=_recording_respond(events, {}), until=until)
    assert events == [("frame", 1), ("mark", ["a1", "a2"], parent)]


@pytest.mark.parametrize("parent", PARENTS)
def test_a_torn_reply_marks_nothing(monkeypatch, events, parent):
    gateway = _Gateway(["a1", "a2"], events)

    def until(harness_: Any) -> None:
        wait_until(lambda: _answered(events, 1), describe=lambda: f"events: {events!r}")
        _then_a_ping_is_answered(harness_, events)

    respond = _recording_respond(events, {1: False})
    _drive(monkeypatch, gateway, parent, 2, respond=respond, until=until)
    assert gateway.marks() == [], f"marked though the reply carrying them tore: {events}"


@pytest.mark.parametrize("parent", PARENTS)
def test_a_crash_writing_the_reply_marks_nothing(monkeypatch, events, parent):
    gateway = _Gateway(["a1", "a2"], events)

    def until(harness_: Any) -> None:
        wait_until(lambda: _answered(events, 1), describe=lambda: f"events: {events!r}")
        _then_a_ping_is_answered(harness_, events)

    respond = _recording_respond(events, {1: BrokenPipeError("client gone")})
    _drive(monkeypatch, gateway, parent, 2, respond=respond, until=until)
    assert gateway.marks() == [], f"marked though the reply was never written: {events}"


@pytest.mark.parametrize("parent", PARENTS)
def test_a_pooled_backends_written_reply_marks_nothing(monkeypatch, events, parent):
    """Routed through the MCP broker, ``kirocrew-core`` is a pooled backend: its
    whole frame lands in the broker, whose relay to the parent confirms nothing.
    No member is marked, so each is delivered again by its completion."""
    monkeypatch.setenv(install_liveness.POOLED_BACKEND_ENV, install_liveness.POOLED_BACKEND_VALUE)
    gateway = _Gateway(["a1", "a2"], events)

    def until(harness_: Any) -> None:
        wait_until(lambda: _answered(events, 1), describe=lambda: f"events: {events!r}")
        _then_a_ping_is_answered(harness_, events)

    _drive(monkeypatch, gateway, parent, 2, respond=_recording_respond(events, {}), until=until)
    assert gateway.marks() == [], f"marked on a write only the broker received: {events}"


@pytest.mark.parametrize("parent", PARENTS)
def test_a_cancel_after_the_reply_was_built_marks_nothing(monkeypatch, events, parent):
    """The step that builds the reply returns its text on a pool thread; a cancel
    that lands before the dispatcher harvests it drops the reply unanswered."""
    gateway = _Gateway(["a1", "a2"], events)
    built = threading.Event()
    go_on = threading.Event()
    real_collect = spawn_tools._SubAgentsStep._collect

    def _collect(self):
        text = real_collect(self)
        built.set()
        wait_until(go_on.is_set, describe=lambda: "the cancel never landed")
        return text

    monkeypatch.setattr(spawn_tools._SubAgentsStep, "_collect", _collect)

    def until(harness_: Any) -> None:
        wait_until(built.is_set, describe=lambda: "the reply was never built")
        harness_.send(
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}
        )
        # Frames are read in order: once this ping is answered, the cancel was read.
        _then_a_ping_is_answered(harness_, events)
        go_on.set()

        def dropped() -> bool:
            calls = harness_.sel_mock.log_tool_invocation.call_args_list
            return any(c.kwargs.get("outcome") == "cancelled" for c in calls)

        wait_until(dropped, describe=lambda: f"events: {events!r}")

    _drive(monkeypatch, gateway, parent, 2, respond=_recording_respond(events, {}), until=until)
    assert not _answered(events, 1), "a cancelled call was answered"
    assert gateway.marks() == [], f"marked though the reply was dropped: {events}"


@pytest.mark.parametrize("parent", PARENTS)
def test_a_member_past_the_response_cap_is_not_marked(monkeypatch, events, parent):
    """Seven CJK results overflow ``MAX_RESPONSE_LEN``: the frame the parent
    receives ends inside the last block, so that member reached nobody."""
    # Under the inline summary threshold, so each is carried whole; JSON escapes
    # each character to six, so six blocks fit the cap and a seventh does not.
    text = "結果" * 1300
    assert len(text) <= COMPLETION_KEEP_DEFAULT_CHARS
    ids = [f"a{i}" for i in range(1, 8)]
    gateway = _Gateway(ids, events, result_of=lambda aid: text)
    framed: list[str] = []

    def respond(req_id: Any, result: Any, error: dict | None = None) -> bool:
        if req_id == 1:
            framed.append(result["content"][0]["text"])
        events.append(("frame", req_id))
        return True

    def until(harness_: Any) -> None:
        wait_until(lambda: gateway.marks(), describe=lambda: f"events: {events!r}")

    _drive(monkeypatch, gateway, parent, 7, respond=respond, until=until)
    ((_kind, marked, _parent),) = gateway.marks()
    assert marked == ids[:6]
    (reply,) = framed
    assert reply.endswith("…[response truncated]")


def test_respond_reports_a_whole_write_and_no_write():
    read_fd, write_fd = os.pipe()
    mcp_shared._stdout_fd = write_fd
    try:
        assert mcp_shared.respond(3, {"ok": True}) is True
        assert b'"id": 3' in os.read(read_fd, 65536)
        assert mcp_shared.respond(None, {"ok": True}) is False
    finally:
        mcp_shared._stdout_fd = None
        os.close(write_fd)
        os.close(read_fd)


def _refusing_pipe() -> int:
    """The write end of a pipe whose reader is gone: ``os.write`` refuses it with
    nothing written, as it does once the client has closed its end."""
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    return write_fd


@pytest.mark.parametrize("snapshot", ["refused", "none"])
def test_respond_claims_no_write_the_clients_pipe_did_not_take(monkeypatch, snapshot):
    """While a tool runs, the stdout suppressor rebinds ``sys.stdout`` to a devnull.
    A frame the client's pipe refused, or one written with no pipe captured at all,
    falls back to whatever ``sys.stdout`` is now: it may still be written there,
    but that is no evidence the parent holds the answer."""
    devnull = io.StringIO()
    monkeypatch.setattr(sys, "stdout", devnull)
    fd = _refusing_pipe() if snapshot == "refused" else None
    mcp_shared._stdout_fd = fd
    try:
        assert _REAL_RESPOND(3, {"ok": True}) is False
    finally:
        mcp_shared._stdout_fd = None
        if fd is not None:
            os.close(fd)
    # The fallback write itself is unchanged; only the claim is.
    assert '"id": 3' in devnull.getvalue()


def test_respond_claims_no_write_for_the_error_frame_it_substitutes(monkeypatch):
    """An answer that fails validation goes out as an "Internal error" frame: the
    client then holds an error, not this answer, so nothing it carried arrived."""
    from kiro_crew.validation import ValidationError

    def _refuse(_resp: Any) -> Any:
        raise ValidationError("response", "refused for the test")

    # Through the facade, which mirrors the patch onto every member that binds it.
    monkeypatch.setattr(mcp_shared, "validate_jsonrpc_response", _refuse)
    read_fd, write_fd = os.pipe()
    mcp_shared._stdout_fd = write_fd
    try:
        assert _REAL_RESPOND(4, {"content": []}) is False
        assert b"Internal error" in os.read(read_fd, 65536)
    finally:
        mcp_shared._stdout_fd = None
        os.close(write_fd)
        os.close(read_fd)


def _real_writer(events: list[Any]):
    """The real ``respond``, logging each answered frame after its write."""

    def respond(req_id: Any, result: Any, error: dict | None = None) -> bool:
        written = _REAL_RESPOND(req_id, result, error)
        events.append(("frame", req_id))
        return written

    return respond


@pytest.mark.parametrize("parent", PARENTS)
def test_a_reply_the_clients_pipe_refused_marks_nothing(monkeypatch, events, parent):
    """The real writer under the real loop: the parent's pipe refuses the frame,
    and the fallback writes it into the suppressor's devnull. The parent holds
    none of it, so nothing is marked and each member's completion delivers it."""
    gateway = _Gateway(["a1", "a2"], events)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    # Kept as the loop's snapshot (one already taken is not retaken), and closed
    # by the loop's own release when it exits.
    mcp_shared._stdout_fd = _refusing_pipe()

    def until(harness_: Any) -> None:
        wait_until(lambda: _answered(events, 1), describe=lambda: f"events: {events!r}")
        _then_a_ping_is_answered(harness_, events)

    try:
        _drive(monkeypatch, gateway, parent, 2, respond=_real_writer(events), until=until)
    finally:
        mcp_shared.release_stdout_fd()
    assert gateway.marks() == [], f"marked a reply the parent's pipe refused: {events}"


@pytest.mark.parametrize("parent", PARENTS)
def test_a_reply_the_clients_pipe_took_marks_its_members(monkeypatch, events, parent):
    """The same loop and writer, on a pipe whose reader is there: the frame is in
    the parent's pipe before the mark is sent."""
    gateway = _Gateway(["a1", "a2"], events)
    read_fd, write_fd = os.pipe()
    mcp_shared._stdout_fd = write_fd

    def until(harness_: Any) -> None:
        wait_until(lambda: gateway.marks(), describe=lambda: f"events: {events!r}")

    try:
        _drive(monkeypatch, gateway, parent, 2, respond=_real_writer(events), until=until)
    finally:
        mcp_shared.release_stdout_fd()
    try:
        assert b'"id": 1' in os.read(read_fd, 65536)
    finally:
        os.close(read_fd)
    assert events == [("frame", 1), ("mark", ["a1", "a2"], parent)]


# ── the routes: the real completion consumer ─────────────────────────────────


@pytest.fixture
def done(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Done]:
    yield from harness(monkeypatch)


def _known(done: _Done, *infos: SubagentInfo) -> None:
    records = {info.id: info for info in infos}
    done.mgr.get = MagicMock(side_effect=records.get)


def _streams(done: _Done) -> list[Any]:
    return [c for c in done.calls if c[0] == "stream"]


def _claims(done: _Done) -> list[Any]:
    return [c for c in done.calls if c[0] in ("acquire", "release")]


CRON = "cron:j1:r1"
CHANNEL = "slack:C1:1700000000.1"


def _confirmed(*infos: SubagentInfo) -> None:
    """Marks a release has confirmed: the turn that received the reply landed."""
    for info in infos:
        info._collected_inline = True
        info._inline_state = "confirmed"


def _mark_under_the_live_turn(sessions: Any, mgr: Any, key: str, *ids: str) -> set[str]:
    """POST mark-collected with ``written: true`` while a turn holds ``key``: the
    real endpoint holds each mark under that turn's holding, pending."""
    from kiro_crew.dashboard.messaging_api.run_control import _flag_collected_inline

    return _flag_collected_inline(SimpleNamespace(subagents=mgr, sessions=sessions), list(ids), key)


@pytest.mark.asyncio
@pytest.mark.parametrize("parent", [CRON, CHANNEL])
async def test_a_result_the_call_returned_gets_no_second_turn(done: _Done, parent: str) -> None:
    """Returned in a turn that landed: acknowledged with no claim and no turn."""
    info = make_info(parent=parent)
    _confirmed(info)
    _known(done, info)

    await done.done(info)

    assert _streams(done) == [], "the parent got a turn for a result it already holds"
    assert _claims(done) == [], "a session was claimed for nothing to inject"
    assert info._report_delivered is True
    assert done.orch._cron_injecting == {}
    ((notice_args, _kw),) = done.notices()  # the bell still rings
    assert notice_args[0] == "subagent"
    done.mgr.notify_injection_failed.assert_not_called()


@pytest.mark.asyncio
async def test_a_returned_childs_completion_leaves_the_running_cron_turn_undisturbed() -> None:
    """The cron turn is still writing its answer when its returned child's
    completion arrives. The route parks it and returns at once: it claims nothing,
    resets nothing under the turn, writes no tombstone and does not acknowledge it.
    The turn lands and releases, the release confirms the mark, and only then is
    the completion acknowledged, with no second turn."""
    from test_subagent_injection_live_turn import _HANG_CEILING_S, _gateway, _provider_factory

    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.session import SessionManager

    key = "cron:job-1:run-1"
    sessions = SessionManager(KiroCrewConfig(), provider_factory=_provider_factory())
    gateway = _gateway(sessions)
    gateway.cron_svc = MagicMock(name="cron_svc")
    gateway.subagent_mgr.settle_queued_delivery = AsyncMock()
    gateway._inject_with_retry = AsyncMock(return_value="a second turn")
    info = SubagentInfo(id="child-1", task="work", parent_session_key=key)
    info.result = "done"
    info.done = True
    gateway.subagent_mgr.get = MagicMock(side_effect={info.id: info}.get)
    # The cron turn: it holds the session while it writes its answer.
    provider, _is_new, _resumed = await sessions.get_or_create(key)
    session = sessions._sessions[key]
    assert _mark_under_the_live_turn(sessions, gateway.subagent_mgr, key, info.id) == {info.id}

    await asyncio.wait_for(
        gateway._finish_subagent_cron_or_notify(
            info, announce="child done", body="b", title="t", carried=(info.id,), parent_key=key
        ),
        _HANG_CEILING_S,
    )

    assert sessions._sessions.get(key) is session, "the cron session was reset under its turn"
    provider.shutdown.assert_not_awaited()
    gateway.cron_svc.clear_active_session_key.assert_not_called()
    assert info._report_delivered is False, "acknowledged while the turn that holds it ran"
    assert info._delivery_queued is True, "the run loop would tombstone a parked completion"
    # The turn lands, and its release settles what it received inline.
    sessions.note_turn_landed(key)
    sessions.release(key)
    await asyncio.wait_for(asyncio.gather(*getattr(gateway, "_inline_tasks", ())), _HANG_CEILING_S)

    assert info._report_delivered is True
    gateway._inject_with_retry.assert_not_awaited()
    gateway.subagent_mgr.settle_queued_delivery.assert_awaited_once()
    (owed,) = gateway.subagent_mgr.settle_queued_delivery.await_args.args
    assert [d.agent_id for d in owed] == [info.id]
    assert gateway._cron_injecting == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("parent", [CRON, CHANNEL])
async def test_a_mark_that_lands_while_the_claim_waits_is_honoured(
    done: _Done, parent: str
) -> None:
    """The claim waits out the parent's turn; the turn's tool wrote its reply, the
    mark landed, and the turn's release confirmed it, so the route asks again."""
    info = make_info(parent=parent)
    _known(done, info)
    claim = done.sessions.get_or_create.side_effect

    async def _claim_while_the_turn_returns(key: str, **kw: Any) -> Any:
        _confirmed(info)
        return await claim(key, **kw)

    done.sessions.get_or_create = AsyncMock(side_effect=_claim_while_the_turn_returns)

    await done.done(info)

    assert _streams(done) == []
    assert _claims(done) == [("acquire", parent), ("release", parent)]
    assert info._report_delivered is True
    assert done.orch._cron_injecting == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("parent", [CRON, CHANNEL])
async def test_an_unmarked_result_is_injected(done: _Done, parent: str) -> None:
    """No mark -- the reply tore, crashed, was cancelled, cut or brokered -- and
    the completion delivers the result: never lost."""
    info = make_info(parent=parent)
    _known(done, info)

    await done.done(info)

    assert len(_streams(done)) == 1
    assert info._report_delivered is True


@pytest.mark.asyncio
@pytest.mark.parametrize("parent", [CRON, CHANNEL, "dashboard:main"])
@pytest.mark.parametrize("unmarked", [None, "w1", "w3"])
async def test_a_digest_is_skipped_only_when_every_member_it_carries_was_returned(
    done: _Done, parent: str, unmarked: str | None
) -> None:
    """A three-member wave holds its first two reports and delivers them in the
    third's digest. Skipping that digest because its carrier was returned would
    lose a held member the reply did not carry."""
    if parent == "dashboard:main":
        slot = done.tab("main")
        slot._subagents_inline_collected = {"w1", "w2", "w3"}
    infos = [make_info(f"w{i}", parent=parent, batch_id="b1", batch_total=3) for i in (1, 2, 3)]
    _confirmed(*(info for info in infos if info.id != unmarked))
    _known(done, *infos)

    for info in infos:
        await done.done(info)
    await done.drain()

    delivered = _streams(done) + done.chats
    if unmarked is None:
        assert delivered == [], "a digest the parent already holds was delivered again"
    else:
        assert len(delivered) == 1, f"the digest carrying {unmarked} was skipped"
        assert unmarked in str(delivered[0])


# ── the cron cleanup: never a reset under a live turn ────────────────────────


@pytest.mark.asyncio
async def test_a_confirmed_completion_leaves_a_live_cron_turn_undisturbed() -> None:
    """A completion whose turn already landed arrives while another turn holds the
    cron key (the job's persistent session, say). It is acknowledged with no claim,
    so its cleanup reset must decline under that turn and leave the key registered;
    the turn's own finalizer resets the session after."""
    from test_subagent_injection_live_turn import _HANG_CEILING_S, _gateway, _provider_factory

    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.session import SessionManager

    key = "cron:job-1"
    sessions = SessionManager(KiroCrewConfig(), provider_factory=_provider_factory())
    gateway = _gateway(sessions)
    gateway.cron_svc = MagicMock(name="cron_svc")
    info = _returned_child("child-1", key)
    _confirmed(info)
    gateway.subagent_mgr.get = MagicMock(side_effect={info.id: info}.get)
    provider, _is_new, _resumed = await sessions.get_or_create(key)  # the next turn
    session = sessions._sessions[key]

    await asyncio.wait_for(
        gateway._finish_subagent_cron_or_notify(
            info, announce="child done", body="b", title="t", carried=(info.id,), parent_key=key
        ),
        _HANG_CEILING_S,
    )

    assert sessions._sessions.get(key) is session, "the cron session was reset under its turn"
    provider.shutdown.assert_not_awaited()
    gateway.cron_svc.clear_active_session_key.assert_not_called()
    assert info._report_delivered is True
    sessions.release(key)
    assert await sessions.reset(key, skip_if_busy=True) is True


# ── the cron route: the real cron callback over the real lease ───────────────


def _cron_orchestrator(sessions: Any, infos: list[SubagentInfo]) -> Any:
    """A gateway whose cron callback and completion routes share a real SessionManager."""
    from slack_gateway_helpers import _make_orchestrator, _mock_context_builder

    orch = _make_orchestrator(slack_enabled=False)
    orch.sessions = sessions
    # As service boot wires it, on the manager this gateway now uses. Tolerant of a
    # tree without the ledger, so these tests fail there on behaviour, not names.
    if hasattr(sessions, "parent_turns"):
        sessions.parent_turns.on_released = orch._settle_inline
    orch.ctx_builder = _mock_context_builder()
    orch.ctx_builder._session_memory_modes = {}
    orch.ctx_builder.build_message = MagicMock(return_value=("full msg", None))
    orch.ctx_builder.hooks = MagicMock()
    orch.dashboard_state = None
    orch.subagent_mgr = MagicMock()
    orch.subagent_mgr.running = []
    orch.subagent_mgr.get = MagicMock(side_effect={i.id: i for i in infos}.get)
    orch.subagent_mgr.has_pending_work_for_async = AsyncMock(return_value=False)
    orch.subagent_mgr.queued_count_for_async = AsyncMock(return_value=0)
    orch.subagent_mgr.settle_queued_delivery = AsyncMock()
    orch._deliver_cron_response = AsyncMock()
    return orch


def _cron_job() -> Any:
    job = MagicMock()
    job.execution_context = None
    job.member_id = ""
    job.memory_store = ""
    job.script = ""
    job.command = ""
    job.id = "j1"
    job.name = "test-job"
    job.persistent_session = False
    job.agent_sequence = []
    job.agent_id = None
    job.channel = ""
    job.created_by = "U1"
    job.approval_mode = "auto"
    job.env = None
    job.acked_items = []
    job.silent = True
    job.thread_ts = None
    job.last_posted_hash = ""
    job.consecutive_dupes = 0
    job.last_posted_at = 0.0
    job.last_failure_hash = ""
    job.last_failure_at = 0.0
    job.consecutive_failures = 0
    job.minimal_context = False
    return job


async def _cron_callback(orch: Any) -> Any:
    with patch("kiro_crew.slack.gateway.CronService") as mock_cs:
        cron_svc = MagicMock()
        cron_svc.start = AsyncMock()
        cron_svc.start_reaper = MagicMock()
        mock_cs.return_value = cron_svc
        mock_cs.create = AsyncMock(return_value=cron_svc)
        await orch._init_cron()
    return mock_cs.create.call_args[1]["on_job"]


def _returned_child(cid: str, key: str) -> SubagentInfo:
    info = SubagentInfo(id=cid, task="work", parent_session_key=key)
    info.result = "done"
    info.done = True
    return info


def _tombstone_gate_open(info: SubagentInfo) -> bool:
    """True when the run loop would write the ``delivered`` tombstone for *info*
    once ``_on_done`` returns (``subagent_manager/terminal.py``)."""
    return not (
        info.error or info._digest_held or info._delivery_queued or info._report_undelivered
    )


async def _run_as_far_as_it_goes(route: asyncio.Task[Any]) -> None:
    """Let the route run its synchronous path while the parent turn is live."""
    for _ in range(50):
        if route.done():
            return
        await asyncio.sleep(0)


async def _drive_cron_turn(*, ending: str, children: int = 1) -> dict[str, Any]:
    """The real cron callback over a real lease. The turn's ``spawn_sub_agents``
    reply is written and marked through the real endpoint; each child's completion
    then reaches the real cron route on its own task while the turn is still
    answering. The turn then ends as *ending* says:

    * ``lands`` -- the stream returns a synthesized answer;
    * ``deadline`` -- cancelled, as the job's deadline ``wait_for`` cancels ``_execute``;
    * ``failure`` -- the stream raises after the write (kiro-cli died mid-answer);
    * ``reap`` -- the reaper resets the run's session, then cancels the task, as
      ``run_kill._end_run_sessions`` and ``finish_taken`` do.
    """
    from test_subagent_injection_live_turn import _provider_factory

    from kiro_crew.acp.client import AcpError
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.session import SessionManager

    key = "cron:j1:r1"
    sessions = SessionManager(KiroCrewConfig(), provider_factory=_provider_factory())
    infos = [_returned_child(f"child-{i}", key) for i in range(children)]
    orch = _cron_orchestrator(sessions, infos)
    seen: dict[str, Any] = {"injected": [], "infos": infos, "sessions": sessions, "orch": orch}

    async def inject(i: Any, _client: Any, _msg: Any, parent: str, _label: str) -> str:
        seen["injected"].append(i.id)
        seen["inject_session"] = sessions._sessions.get(parent)
        i._report_delivered = True  # _inject_with_retry acknowledges on landing
        return "answered from the completion"

    orch._inject_with_retry = inject
    callback = await _cron_callback(orch)
    answering = asyncio.Event()

    async def stream(_client: Any, _message: Any, **kw: Any) -> str:
        seen["turn_session"] = sessions._sessions[key]
        _mark_under_the_live_turn(sessions, orch.subagent_mgr, key, *(i.id for i in infos))
        seen["routes"] = [
            asyncio.create_task(
                orch._finish_subagent_cron_or_notify(
                    i, announce=f"{i.id} done", body="b", title="t", carried=(i.id,), parent_key=key
                )
            )
            for i in infos
        ]
        for route in seen["routes"]:
            await _run_as_far_as_it_goes(route)
        seen["routes_returned_while_live"] = all(r.done() for r in seen["routes"])
        seen["acked_while_live"] = any(i._report_delivered for i in infos)
        seen["gate_open_while_live"] = any(_tombstone_gate_open(i) for i in infos)
        seen["alive_while_live"] = sessions._sessions.get(key) is seen["turn_session"]
        answering.set()
        if ending == "lands":
            kw["on_complete"](SimpleNamespace(stop_reason="end_turn"))  # as the stream reports it
            return "the cron's synthesized answer"
        if ending == "failure":
            raise AcpError("kiro-cli exited mid-answer")
        await asyncio.Event().wait()  # the model is still writing when the turn is cut
        return "never"

    with (
        patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=stream),
        patch("kiro_crew.slack.gateway.build_cron_session_context", return_value=(key, "task")),
    ):
        run = asyncio.create_task(callback(_cron_job()))
        await asyncio.wait_for(answering.wait(), 30)
        if ending == "reap":
            await sessions.reset(key)  # the reaper ends the run's session first
        if ending in ("deadline", "reap"):
            run.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            seen["result"] = await asyncio.wait_for(run, 30)
        await asyncio.wait_for(asyncio.gather(*getattr(orch, "_inline_tasks", ())), 30)
    seen["cron_svc"] = orch.cron_svc
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["deadline", "failure", "reap"])
async def test_a_cron_turn_that_does_not_land_still_delivers_the_returned_result(
    ending: str,
) -> None:
    """GPT 6.1's BLOCK (comment 6036084771). The turn's reply was written, but the
    deadline, a failure or a reap ends the turn before it lands. The completion is
    neither acknowledged nor tombstoned while the turn is live, returns from
    ``_on_done`` at once, and is injected once after the turn's release."""
    seen = await _drive_cron_turn(ending=ending)
    (info,) = seen["infos"]

    assert seen["acked_while_live"] is False, (
        "the cron route acknowledged a returned result while the turn that received "
        "the reply was still answering"
    )
    assert seen["gate_open_while_live"] is False, (
        "_on_done returned with no deferral flag, so the run loop writes the delivered "
        "tombstone and restart reconciliation can no longer recover the result"
    )
    assert seen["routes_returned_while_live"], "the route waited inside _on_done"
    assert seen["injected"] == [info.id], f"injected={seen['injected']!r}"
    assert info._report_delivered is True
    seen["orch"].subagent_mgr.settle_queued_delivery.assert_awaited_once()
    assert seen["orch"]._cron_injecting == {}
    assert seen["orch"]._inline_parked == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["deadline", "failure"])
async def test_a_re_routed_cron_completion_injects_into_the_turns_own_session(
    ending: str,
) -> None:
    """The release counts the re-routed completion before the cron ``finally``
    decides its reset, so the reset is deferred and the injection continues the
    same conversation; the completion's own cleanup then resets it once."""
    seen = await _drive_cron_turn(ending=ending)
    sessions = seen["sessions"]

    assert seen["inject_session"] is seen["turn_session"], "the injection cold-started"
    assert "cron:j1:r1" not in sessions._sessions, "nothing reset the session after"
    seen["cron_svc"].clear_active_session_key.assert_called_once_with("j1", "cron:j1:r1")


@pytest.mark.asyncio
@pytest.mark.parametrize("children", [1, 2])
async def test_a_cron_turn_that_lands_plays_no_second_turn_for_its_returned_result(
    children: int,
) -> None:
    """The PR's goal: a turn that landed already used the results. Each completion
    parks while the turn answers, and the turn's release acknowledges it with no
    injection. Nothing resets under the turn; its finalizer resets once and
    retires the key once."""
    seen = await _drive_cron_turn(ending="lands", children=children)
    infos = seen["infos"]

    assert seen["result"] is not None
    assert seen["alive_while_live"], "a route reset the session under the cron turn"
    assert seen["acked_while_live"] is False
    assert seen["injected"] == [], "a result the landed turn already used was injected again"
    assert all(i._report_delivered for i in infos)
    assert "cron:j1:r1" not in seen["sessions"]._sessions
    seen["cron_svc"].clear_active_session_key.assert_called_once_with("j1", "cron:j1:r1")
    assert seen["orch"].subagent_mgr.settle_queued_delivery.await_count == children
    assert seen["orch"]._cron_injecting == {}


@pytest.mark.asyncio
async def test_a_mark_landing_while_the_route_waits_on_the_real_lease_is_honoured() -> None:
    """Unmarked at entry, so the cron route claims and waits out the turn on the real
    semaphore; the turn's reply is written and marked, and the turn lands. Its
    release confirms the mark, so after the claim the route injects nothing and
    resets the idle session once."""
    from test_subagent_injection_live_turn import _gateway, _provider_factory

    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.session import SessionManager

    key = "cron:j1:r1"
    sessions = SessionManager(KiroCrewConfig(), provider_factory=_provider_factory())
    gateway = _gateway(sessions)
    gateway.cron_svc = MagicMock(name="cron_svc")
    info = _returned_child("child-1", key)
    gateway.subagent_mgr.get = MagicMock(side_effect={info.id: info}.get)
    gateway._inject_with_retry = AsyncMock(return_value="reply")
    await sessions.get_or_create(key)  # the cron turn
    route = asyncio.create_task(
        gateway._finish_subagent_cron_or_notify(
            info, announce="done", body="b", title="t", carried=(info.id,), parent_key=key
        )
    )
    await async_wait_until(
        lambda: gateway._cron_injecting.get(key) == 1, describe=lambda: "the route's claim"
    )
    await _run_as_far_as_it_goes(route)
    assert not route.done(), "the route did not wait for the turn's lease"
    _mark_under_the_live_turn(sessions, gateway.subagent_mgr, key, info.id)
    sessions.note_turn_landed(key)
    sessions.release(key)  # the turn ends
    await asyncio.wait_for(route, 30)

    gateway._inject_with_retry.assert_not_awaited()
    assert info._report_delivered is True
    assert key not in sessions._sessions
    gateway.cron_svc.clear_active_session_key.assert_called_once_with("j1", key)


@pytest.mark.asyncio
async def test_the_cron_turn_ending_inside_a_settled_completions_cleanup_tears_down_once() -> None:
    """The turn lands and its release settles the parked completion, whose route is
    then suspended in its queued-count read while the cron finalizer resets the
    session. The route's own reset finds nothing to tear down: one provider
    shutdown, the key retired once, nothing left registered."""
    from test_subagent_injection_live_turn import _provider_factory

    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.session import SessionManager

    key = "cron:j1:r1"
    sessions = SessionManager(KiroCrewConfig(), provider_factory=_provider_factory())
    info = _returned_child("child-1", key)
    orch = _cron_orchestrator(sessions, [info])
    in_read, go_on = asyncio.Event(), asyncio.Event()

    async def queued_count(_key: str) -> int:
        in_read.set()
        await go_on.wait()
        return 0

    orch.subagent_mgr.queued_count_for_async = queued_count
    seen: dict[str, Any] = {}
    callback = await _cron_callback(orch)

    async def stream(client: Any, _message: Any, **kw: Any) -> str:
        seen["client"] = client
        _mark_under_the_live_turn(sessions, orch.subagent_mgr, key, info.id)
        route = asyncio.create_task(
            orch._finish_subagent_cron_or_notify(
                info, announce="done", body="b", title="t", carried=(info.id,), parent_key=key
            )
        )
        await _run_as_far_as_it_goes(route)
        assert route.done(), "the route waited under the live turn"
        kw["on_complete"](SimpleNamespace(stop_reason="end_turn"))
        return "cron result"

    with (
        patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=stream),
        patch("kiro_crew.slack.gateway.build_cron_session_context", return_value=(key, "task")),
    ):
        await asyncio.wait_for(callback(_cron_job()), 30)
    await asyncio.wait_for(in_read.wait(), 30)  # the settled completion's cleanup
    assert key not in sessions._sessions, "the finalizer did not reset the idle session"
    go_on.set()
    await asyncio.wait_for(asyncio.gather(*getattr(orch, "_inline_tasks", ())), 30)

    assert key not in sessions._sessions
    assert seen["client"].shutdown.await_count == 1
    assert {c.args for c in orch.cron_svc.clear_active_session_key.call_args_list} == {("j1", key)}
    assert orch._cron_injecting == {}
    assert info._report_delivered is True


# ── the channel route: settled at the parent turn's release ──────────────────

#: One parent of each kind the channel route serves.
CHANNEL_PARENTS = ["slack:C1:1700000000.1", "telegram:42", "taskrunner:t1:runtime"]
CHANNEL_IDS = ["slack-thread", "telegram", "task-runner"]


def _channel_gateway(sessions: Any, *infos: SubagentInfo) -> Any:
    from test_subagent_injection_live_turn import _gateway

    gateway = _gateway(sessions)

    async def _injected(i: Any, *_a: Any) -> str:
        i._report_delivered = True  # _inject_with_retry acknowledges on landing
        return "answered from the completion"

    gateway._inject_with_retry = AsyncMock(side_effect=_injected)
    gateway.subagent_mgr.get = MagicMock(side_effect={i.id: i for i in infos}.get)
    gateway.subagent_mgr.settle_queued_delivery = AsyncMock()
    return gateway


def _channel_route(gateway: Any, info: SubagentInfo, key: str) -> asyncio.Task[Any]:
    return asyncio.create_task(
        gateway._inject_subagent_into_channel_parent(
            info,
            announce=f"{info.id} done",
            body="b",
            title="t",
            carried=(info.id,),
            parent_key=key,
        )
    )


def _new_sessions() -> Any:
    from test_subagent_injection_live_turn import _provider_factory

    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.session import SessionManager

    return SessionManager(KiroCrewConfig(), provider_factory=_provider_factory())


@pytest.mark.asyncio
@pytest.mark.parametrize("key", CHANNEL_PARENTS, ids=CHANNEL_IDS)
async def test_a_channel_route_does_not_acknowledge_a_returned_result_under_a_live_turn(
    key: str,
) -> None:
    """The channel route had the same window: its first check acknowledged the
    completion before the parent's own turn had finished with the reply. Now it
    parks, returns at once with no tombstone, and the turn's release decides. A
    turn that never notes landing (a stop, a failure, an engine with no note)
    revokes the mark, and the completion is injected once."""
    sessions = _new_sessions()
    info = _returned_child("child-1", key)
    gateway = _channel_gateway(sessions, info)
    await sessions.get_or_create(key)  # the parent's turn: it holds the session, answering
    _mark_under_the_live_turn(sessions, gateway.subagent_mgr, key, info.id)

    route = _channel_route(gateway, info, key)
    await _run_as_far_as_it_goes(route)
    acked_while_live = info._report_delivered
    gate_open_while_live = route.done() and _tombstone_gate_open(info)
    returned_while_live = route.done()
    sessions.release(key)  # the turn ends; it reported no landing
    await asyncio.wait_for(asyncio.gather(*getattr(gateway, "_inline_tasks", ())), 30)

    assert acked_while_live is False, (
        "the channel route acknowledged a returned result while the parent turn that "
        "received the reply was still live"
    )
    assert gate_open_while_live is False, (
        "_on_done returned with no deferral flag, so the delivered tombstone is written "
        "before the parent turn lands"
    )
    assert returned_while_live, "the route waited inside _on_done"
    gateway._inject_with_retry.assert_awaited_once()
    assert info._report_delivered is True
    gateway.subagent_mgr.settle_queued_delivery.assert_awaited_once()
    assert gateway._inline_parked == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("key", CHANNEL_PARENTS, ids=CHANNEL_IDS)
async def test_a_channel_turn_that_lands_plays_no_second_turn(key: str) -> None:
    """The parked completion is acknowledged at the landed turn's release, with no
    injection, and its tombstone is written once, then."""
    sessions = _new_sessions()
    info = _returned_child("child-1", key)
    gateway = _channel_gateway(sessions, info)
    await sessions.get_or_create(key)
    _mark_under_the_live_turn(sessions, gateway.subagent_mgr, key, info.id)
    route = _channel_route(gateway, info, key)
    await _run_as_far_as_it_goes(route)
    gateway.subagent_mgr.settle_queued_delivery.assert_not_awaited()

    sessions.note_turn_landed(key)
    sessions.release(key)
    await asyncio.wait_for(asyncio.gather(*getattr(gateway, "_inline_tasks", ())), 30)

    gateway._inject_with_retry.assert_not_awaited()
    assert info._report_delivered is True
    gateway.subagent_mgr.settle_queued_delivery.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_completion_after_its_landed_turn_released_is_acknowledged_unclaimed() -> None:
    sessions = _new_sessions()
    key = CHANNEL_PARENTS[0]
    info = _returned_child("child-1", key)
    gateway = _channel_gateway(sessions, info)
    await sessions.get_or_create(key)
    _mark_under_the_live_turn(sessions, gateway.subagent_mgr, key, info.id)
    sessions.note_turn_landed(key)
    sessions.release(key)
    claims: list[str] = []
    real_claim = sessions.get_or_create

    async def _claim(k: str, **kw: Any) -> Any:
        claims.append(k)
        return await real_claim(k, **kw)

    sessions.get_or_create = _claim

    await asyncio.wait_for(_channel_route(gateway, info, key), 30)

    assert claims == [], "a session was claimed for a result its landed turn used"
    gateway._inject_with_retry.assert_not_awaited()
    assert info._report_delivered is True
    assert gateway._inline_parked == {}


@pytest.mark.asyncio
async def test_a_mark_that_arrives_after_its_turn_released_is_never_held() -> None:
    """The one duplicate left: the mark landed after the turn gave the session back,
    so nothing can confirm it. It flags nothing and the completion is injected."""
    sessions = _new_sessions()
    key = CHANNEL_PARENTS[0]
    info = _returned_child("child-1", key)
    gateway = _channel_gateway(sessions, info)
    await sessions.get_or_create(key)
    sessions.note_turn_landed(key)
    sessions.release(key)

    assert _mark_under_the_live_turn(sessions, gateway.subagent_mgr, key, info.id) == set()
    await asyncio.wait_for(_channel_route(gateway, info, key), 30)

    gateway._inject_with_retry.assert_awaited_once()
    assert gateway._inline_parked == {}


@pytest.mark.asyncio
async def test_each_sequence_step_settles_only_what_it_received() -> None:
    """A sequence job's steps run one after another, each on its own key. Step 1
    lands and its release acknowledges its child; step 2 is cut and its release
    re-routes its child into step 2's own key, counted before the step's reset
    decision, and nothing of step 1's is touched by it."""
    sessions = _new_sessions()
    k1, k2 = "cron:j1:alpha", "cron:j1:beta"
    c1, c2 = _returned_child("c1", k1), _returned_child("c2", k2)
    orch = _cron_orchestrator(sessions, [c1, c2])
    injected: list[tuple[str, str]] = []

    async def inject(i: Any, _client: Any, _msg: Any, parent: str, _label: str) -> str:
        injected.append((i.id, parent))
        i._report_delivered = True
        return "answered"

    orch._inject_with_retry = inject
    for key, child, lands in ((k1, c1, True), (k2, c2, False)):
        await sessions.get_or_create(key)
        _mark_under_the_live_turn(sessions, orch.subagent_mgr, key, child.id)
        route = asyncio.create_task(
            orch._finish_subagent_cron_or_notify(
                child, announce="done", body="b", title="t", carried=(child.id,), parent_key=key
            )
        )
        await _run_as_far_as_it_goes(route)
        assert route.done() and child._report_delivered is False
        if lands:
            sessions.note_turn_landed(key)
        sessions.release(key)
        if not lands:
            assert orch._cron_injecting.get(k2) == 1, "counted after the step's reset decision"
        await asyncio.wait_for(asyncio.gather(*getattr(orch, "_inline_tasks", ())), 30)

    assert injected == [("c2", k2)]
    assert c1._report_delivered is True and c2._report_delivered is True
    assert orch._cron_injecting == {}


# ── the settlement: the ledger and the session's release ─────────────────────


def test_the_ledger_hands_each_holding_its_own_marks_and_landing() -> None:
    from kiro_crew.parent_turns import ParentTurnLedger

    ledger = ParentTurnLedger()
    settled: list[tuple[Any, bool, list[str]]] = []
    ledger.on_released = lambda token, landed, ids: settled.append((token, landed, ids))
    a, b = ("cron:j1:r1", "task-a"), ("cron:j1:r1", "task-b")
    ledger.hold_marks(a, ["c1"])
    ledger.hold_marks(b, ["c2"])
    ledger.note_landed(a)

    ledger.settle(b)
    ledger.settle(a)
    ledger.settle(a)  # a second release of the same holding settles nothing

    assert settled == [(b, False, ["c2"]), (a, True, ["c1"])]
    assert not ledger.pending(a) and not ledger.pending(b)


def test_a_failing_settle_hook_never_breaks_the_release(caplog) -> None:
    from kiro_crew.parent_turns import ParentTurnLedger

    ledger = ParentTurnLedger()
    ledger.on_released = MagicMock(side_effect=RuntimeError("boom"))
    ledger.hold_marks(("k", "t"), ["c1"])

    ledger.settle(("k", "t"))  # no raise

    assert "settling inline-returned results" in caplog.text


@pytest.mark.asyncio
async def test_a_reset_session_is_settled_by_its_holders_late_release() -> None:
    """A reaper or a reset pops the session while the turn holds it; the holder's
    own release is then absorbed, and it is still the one that settles the
    holding, before the absorb returns."""
    sessions = _new_sessions()
    settled: list[tuple[Any, bool, list[str]]] = []
    sessions.parent_turns.on_released = lambda t, landed, ids: settled.append((t, landed, ids))
    key = "cron:j1:r1"
    started, go_on = asyncio.Event(), asyncio.Event()

    async def turn() -> None:
        await sessions.get_or_create(key)
        started.set()
        await go_on.wait()
        sessions.release(key)  # absorbed: the session it held was popped

    task = asyncio.create_task(turn())
    await asyncio.wait_for(started.wait(), 30)
    token = sessions.turn_token(key)
    assert token is not None and token[1] is task
    sessions.parent_turns.hold_marks(token, ["c1"])
    await sessions.reset(key)
    go_on.set()
    await asyncio.wait_for(task, 30)

    assert settled == [(token, False, ["c1"])]


@pytest.mark.asyncio
async def test_only_the_holding_task_notes_its_own_landing() -> None:
    sessions = _new_sessions()
    settled: list[bool] = []
    sessions.parent_turns.on_released = lambda _t, landed, _ids: settled.append(landed)
    key = "telegram:42"
    await sessions.get_or_create(key)
    token = sessions.turn_token(key)
    sessions.parent_turns.hold_marks(token, ["c1"])

    async def elsewhere() -> None:
        sessions.note_turn_landed(key)  # another task's verdict is not this turn's

    await asyncio.create_task(elsewhere())
    sessions.release(key)

    assert settled == [False]


def test_a_turn_bracket_notes_a_landed_verdict_on_its_session() -> None:
    """Every engine on ``ChannelTurns`` decides landing through the bracket."""
    from kiro_crew.messaging.turn_bracket import TurnBracket

    sessions = MagicMock()
    TurnBracket(sessions, MagicMock(), "discord:7").landed("end_turn")
    sessions.note_turn_landed.assert_called_once_with("discord:7")
    sessions.reset_mock()
    TurnBracket(sessions, MagicMock(), "discord:7").landed("cancelled")
    sessions.note_turn_landed.assert_not_called()


def test_every_engine_that_settles_a_turn_notes_its_landing() -> None:
    """Each turn engine settles its landed verdict (``rollback_skill_bodies``); each
    also notes it, so its release can confirm the results it received inline. The
    dashboard tab's turns keep their own route and note nothing."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
    settlers = {
        path.relative_to(src).as_posix()
        for path in src.rglob("*.py")
        if "rollback_skill_bodies(" in path.read_text(encoding="utf-8")
    }
    noters = {
        path.relative_to(src).as_posix()
        for path in src.rglob("*.py")
        if "note_turn_landed(" in path.read_text(encoding="utf-8")
    }
    # chat_runner: the tab route; turn_bracket: defines both; the other two only
    # name the helper in prose.
    exempt = {
        "dashboard/chat_runner.py",
        "messaging/turn_bracket.py",
        "context.py",
        "context_assembly/delivery_records.py",
    }
    assert settlers - exempt <= noters, sorted(settlers - exempt - noters)


@pytest.mark.asyncio
async def test_a_completion_parked_through_a_restart_is_re_announced(tmp_path, monkeypatch) -> None:
    """The real run loop over a real run folder: the parked completion leaves no
    ``delivered`` tombstone, so a restart before the settle finds the folder in
    ``list_orphans()`` and re-announces it. The landed turn's release then writes
    the tombstone, once."""
    import json

    from kiro_crew.subagent import SubagentManager
    from kiro_crew.subagent_persistence import create_agent_folder, list_orphans, write_result_chunk

    monkeypatch.setattr("kiro_crew.subagent_persistence._SUBAGENTS_DIR", tmp_path)
    key = "cron:j1:r1"
    sessions = _new_sessions()
    info = _returned_child("park001", key)
    create_agent_folder(info.id, task="t", parent_session=key)
    write_result_chunk(info.id, "the full transcript\n")
    mgr = SubagentManager(sessions=sessions, ctx_builder=MagicMock())
    mgr._fire_event = AsyncMock()
    orch = _cron_orchestrator(sessions, [info])
    orch.subagent_mgr.settle_queued_delivery = mgr.settle_queued_delivery

    async def _on_done(i: SubagentInfo) -> None:
        await orch._finish_subagent_cron_or_notify(
            i, announce="done", body="b", title="t", carried=(i.id,), parent_key=key
        )

    mgr._on_done = _on_done
    await sessions.get_or_create(key)  # the cron turn, still answering
    _mark_under_the_live_turn(sessions, orch.subagent_mgr, key, info.id)

    await asyncio.wait_for(
        mgr._report_terminal(
            info, source="test", injection_timeout_reason="x", mark_delivered_on_success=True
        ),
        30,
    )

    assert not (tmp_path / info.id / "tombstone.json").exists()
    assert info.id in [o.get("id") for o in list_orphans()]
    sessions.note_turn_landed(key)
    sessions.release(key)
    await asyncio.wait_for(asyncio.gather(*getattr(orch, "_inline_tasks", ())), 30)
    await async_wait_until(
        lambda: (tmp_path / info.id / "tombstone.json").exists(),
        timeout=10,
        describe=lambda: "the settle never wrote the tombstone",
    )
    tomb = json.loads((tmp_path / info.id / "tombstone.json").read_text(encoding="utf-8"))
    assert tomb["cause"] == "delivered"
    assert info.id not in [o.get("id") for o in list_orphans()]


# ── the mark's scope: only the caller's own children ─────────────────────────


@pytest.mark.asyncio
async def test_mark_collected_flags_only_the_callers_own_children(tmp_path) -> None:
    """A cron parent's mark is held, pending, under the turn holding its permit; a
    mark sent before its write, another parent's child and a parentless claim mark
    nothing, and neither does a mark that arrives after the turn released."""
    from chat_test_helpers import _make_state
    from test_subagent_injection_live_turn import _provider_factory
    from test_subagent_queued_visibility import _Req

    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.dashboard.handlers import messaging
    from kiro_crew.session import SessionManager

    own = make_info("c1", parent="cron:j1:r1")
    foreign = make_info("f1", parent="cron:other:r1")
    late = make_info("l1", parent="cron:j1:r1")
    state = _make_state(tmp_path)
    state.sessions = SessionManager(KiroCrewConfig(), provider_factory=_provider_factory())
    state.subagents = MagicMock()
    records = {r.id: r for r in (own, foreign, late)}
    state.subagents.get = MagicMock(side_effect=records.get)
    await state.sessions.get_or_create("cron:j1:r1")  # the cron turn holds the permit

    # A kirocrew-core that marks without saying the frame was written (one from
    # before the mark moved behind the write) flags nothing.
    legacy = {"ids": ["c1"], "parent_session": "cron:j1:r1"}
    assert (await messaging.api_spawn_mark_collected(_Req(state, body=legacy))).status == 200
    assert own._collected_inline is False, "a mark sent before its write was trusted"

    body = {"ids": ["c1", "f1", "unknown"], "parent_session": "cron:j1:r1", "written": True}
    resp = await messaging.api_spawn_mark_collected(_Req(state, body=body))

    assert resp.status == 200
    assert own._collected_inline is True
    assert own._inline_state == "pending", "a mark was trusted before its turn landed"
    assert own._inline_token == state.sessions.turn_token("cron:j1:r1")
    assert state.sessions.parent_turns.pending(own._inline_token)
    assert foreign._collected_inline is False, "a caller suppressed another parent's result"

    state.sessions.release("cron:j1:r1")  # the turn ended (it noted no landing)
    from kiro_crew.slack.gateway_runtime.subagent_injection import _inline_verdict

    verdict, _holding = _inline_verdict(state.subagents, state.sessions, ("c1",))
    assert verdict == "deliver", "a mark whose turn released unconfirmed was honoured"
    after = {"ids": ["l1"], "parent_session": "cron:j1:r1", "written": True}
    await messaging.api_spawn_mark_collected(_Req(state, body=after))
    assert late._collected_inline is False, "a mark that came after its turn was held"

    orphan = make_info("o1", parent="")
    state.subagents.get = MagicMock(side_effect={"o1": orphan}.get)
    parentless = {"ids": ["o1"], "written": True}
    await messaging.api_spawn_mark_collected(_Req(state, body=parentless))
    assert orphan._collected_inline is False, "a parentless claim marked a result"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("written", "owner"),
    [
        pytest.param(None, "dashboard:chat-1", id="core-from-before-the-write-mark"),
        pytest.param(False, "dashboard:chat-1", id="unwritten"),
        pytest.param("true", "dashboard:chat-1", id="written-not-a-bool"),
        pytest.param(True, "dashboard:other", id="written-for-another-parent"),
    ],
)
async def test_a_queued_tab_completion_stays_without_its_written_mark(
    tmp_path, written: Any, owner: str
) -> None:
    """The tab was busy with the tool's own turn, so a member's completion was
    QUEUED behind it. Only a mark sent after the reply carrying that member was
    written, naming the tab's own child, may take it out of the queue: any other
    mark leaves it there, and it plays as a turn -- a duplicate at worst."""
    from chat_test_helpers import _make_state
    from test_subagent_queued_visibility import _Req

    from kiro_crew.constants import SUBAGENT_COMPLETION_META_KEY
    from kiro_crew.dashboard.chat_utils import SUBAGENT_COMPLETION_KIND
    from kiro_crew.dashboard.handlers import messaging
    from kiro_crew.subagent import SubagentDelivery
    from kiro_crew.subagent_completion_meta import single_completion_meta

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    announce = "[Subagent completion event] a1 finished"
    slot.queue_append(
        announce,
        kind=SUBAGENT_COMPLETION_KIND,
        meta={SUBAGENT_COMPLETION_META_KEY: single_completion_meta(agent_id="a1", outcome="ok")},
    )
    owed = [SubagentDelivery("a1", 1.0, 0.0)]
    slot.note_pending_subagent_delivery(announce, owed)
    child = make_info("a1", parent=owner)
    state.subagents = MagicMock(settle_queued_delivery=AsyncMock())
    state.subagents.get = MagicMock(side_effect={"a1": child}.get)
    state.push_slots_update = MagicMock()
    body: dict[str, Any] = {"ids": ["a1"], "parent_session": "dashboard:chat-1"}
    if written is not None:
        body["written"] = written

    resp = await messaging.api_spawn_mark_collected(_Req(state, body=body))

    assert resp.status == 200
    queued = [q["content"] for q in slot._queue if q.get("kind") == SUBAGENT_COMPLETION_KIND]
    assert queued == [announce], "a queued completion was dropped for a reply nobody proved"
    state.subagents.settle_queued_delivery.assert_not_awaited()
    assert slot.take_pending_subagent_deliveries([announce]) == owed
    assert slot._subagents_inline_collected == set()
    assert child._collected_inline is False
    if written is not True:
        # An unwritten mark changes nothing, so it announces no slot change.
        state.push_slots_update.assert_not_called()


# ── the two halves composed, over random waves ───────────────────────────────


def test_an_announce_carrying_no_member_is_never_skipped() -> None:
    """Nothing carried proves nothing returned: the announce is delivered."""
    from kiro_crew.slack.gateway_runtime.subagent_injection import _delivered_inline

    record = SubagentInfo(id="m1", task="t")
    record._collected_inline = True
    mgr = MagicMock()
    mgr.get = MagicMock(side_effect={"m1": record}.get)

    assert _delivered_inline(mgr, ("m1",)) is True
    assert _delivered_inline(mgr, ()) is False


def test_every_member_reaches_its_parent_at_least_once() -> None:
    """Seeded: the tool's real cut decision (``_carried_in_reply``) and the routes'
    real skip decision (``_delivered_inline``) over random waves, reply sizes,
    answer outcomes and chunkings. Every member must reach its parent through a
    written, unbrokered reply that carried it or through an announce delivered.

    Run at a scaled-down cap, the same one on the reply's cut and the tool's, so
    about half the waves cross it without sanitising megabytes per seed;
    ``test_a_member_past_the_response_cap_is_not_marked`` keeps the real cap."""
    # Imported here, not at module level, so the module collects on a tree that
    # lacks these two helpers and the other tests in it can show the bug there.
    from kiro_crew.mcp_tools.spawn_parts.wave import _carried_in_reply
    from kiro_crew.slack.gateway_runtime.subagent_injection import _delivered_inline

    cap = 1_000
    cut = 0  # waves whose reply the cap cuts short of a settled member
    for seed in range(400):
        rnd = random.Random(seed)
        n = rnd.randint(1, 12)
        ids = [f"m{seed}-{i}" for i in range(n)]
        settled = [aid for aid in ids if rnd.random() < 0.8]
        blocks: list[str] = []
        block_of: dict[str, int] = {}
        for aid in ids:
            if aid in settled:
                block_of[aid] = len(blocks)
            blocks.append('{"agent": "%s", "text": "%s"}' % (aid, "x" * rnd.choice([1, 300])))
        reply = build_tool_response("\n\n".join(blocks), max_len=cap)["content"][0]["text"]
        outcome = rnd.choice(["written", "torn", "pooled", "crash", "cancel"])
        carried_by_reply = set(_carried_in_reply(blocks, block_of, max_len=cap))
        cut += carried_by_reply != set(block_of)
        # The tool marks only what a written, unbrokered reply carried.
        marked = carried_by_reply if outcome == "written" else set()
        held_by_parent = marked
        # What a mark claims is really in the frame: each marked block whole.
        for aid in marked:
            assert blocks[block_of[aid]] in reply[:cap], (seed, aid)

        records = {aid: SubagentInfo(id=aid, task="t") for aid in ids}
        for aid in marked:
            records[aid]._collected_inline = True
        mgr = MagicMock()
        mgr.get = MagicMock(side_effect=records.get)

        order = ids[:]
        rnd.shuffle(order)
        announces: list[tuple[str, ...]] = []
        while order:
            take = rnd.randint(1, len(order))
            announces.append(tuple(order[:take]))
            order = order[take:]
        injected: set[str] = set()
        for carried in announces:
            if _delivered_inline(mgr, carried):
                assert set(carried) <= held_by_parent, (seed, carried)
            else:
                injected.update(carried)
        lost = set(ids) - held_by_parent - injected
        assert not lost, (seed, outcome, sorted(lost))
    # The scaled cap really is crossed: about half the waves leave a member out.
    assert cut >= 150, cut
