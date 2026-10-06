"""A subagent spawn approval reaches the crew log, so the approvals fold sees it.

The reported shape: a ``spawn_run`` that needs the user's answer parks on the
prompt, and the crew log holds nothing about the wait -- no ``approval/requested``
while it is open and no ``approval/decided`` once it is answered. ``fold_approvals``
is therefore empty for a session whose spawn is blocked on a human, and every
surface built on it reports that nothing is waiting. Ordinary mid-turn tool
approvals do land, because the dashboard chat runner writes both halves around its
own permission await.

What these tests pin is the pair: one entry before the await, keyed by the spawn's
own ``spawn:<agent_id>`` request id and filed under the turn that ASKED for the
spawn, and one answering entry on every exit from the gate -- a person's yes, a
person's no, a prompt no surface received, a callback that raised, and a wait that
was cancelled rather than answered.

Deliberately NOT covered, because the change does not make them: the child's own
crew log (no subagent path opens one), the dashboard's rendering of the folded
pending row, and the auto-approve rungs, which answer without a prompt and so have
no approval to record.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from off_loop_helpers import off_loop

from kiro_crew.crew_log import crew_log_path, emit
from kiro_crew.crew_log.projection import fold_approvals
from kiro_crew.crew_log.store import CrewLog
from kiro_crew.subagent import SpawnApprovalUnreachable, SubagentManager

SESSION = "acp-spawn-approval-1"
ASKING_TURN = 6

#: ``SubagentManager.spawn`` refuses before it registers anything when the host
#: looks short of memory, and that refusal never reaches the approval gate -- so
#: without this the failure would read as the emitter having skipped an entry.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")


@pytest.fixture(autouse=True)
def _isolated_crew_log(tmp_path, monkeypatch):
    """Every test writes into its own data home with the crew log switched on."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    monkeypatch.setattr(emit, "_retry_delay", lambda _attempts: 0.0)
    emit.reset_caches()
    emit._child_origin.clear()
    yield
    emit.drain_for_shutdown(timeout=2.0)
    emit.reset_caches()
    emit._child_origin.clear()


@pytest.fixture(autouse=True)
def _close_subagent_managers(close_subagent_managers):
    """Every manager built here is closed at teardown; the body is in ``conftest``."""


# --------------------------------------------------------------------------
# reading the log
# --------------------------------------------------------------------------


def _entries() -> list[dict]:
    path: Path = crew_log_path("session", SESSION)
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _of(kind: str) -> list[dict]:
    return [entry["data"] for entry in _entries()[1:] if entry["type"] == kind]


def _read_folded() -> dict:
    handle = CrewLog.open("session", SESSION)
    try:
        return fold_approvals(tuple(handle.iter_from(1)))
    finally:
        handle.release_ownership()


def _folded() -> dict:
    """The ``approvals`` fold over the session's whole log, as a reader sees it.

    Read off the event-loop thread, as product readers do: the callers are async
    tests, and an on-loop acquire of the unit lock is refused outright whenever the
    writer or the eager folder holds it (``test_crew_log_off_loop_pin.py``).
    """
    return off_loop(_read_folded)


def _open_session() -> None:
    emit.on_session_opened(
        SESSION,
        agent="kirocrew",
        slot="chat-7",
        model="claude-opus-5",
        cwd="/home/dev/project",
        owner="default",
    )
    emit.on_turn_started(SESSION, ASKING_TURN, "user")


# --------------------------------------------------------------------------
# manager doubles -- a DEFAULT install, where every auto-approve rung is off
# --------------------------------------------------------------------------


def _mock_sessions() -> MagicMock:
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    provider = AsyncMock()
    provider.start = AsyncMock()
    provider.shutdown = AsyncMock()
    provider.context_usage_pct = lambda: 0.0

    async def _empty_stream(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        return
        yield  # noqa: unreachable -- makes this an async generator

    provider.stream = MagicMock(side_effect=lambda *a, **kw: _empty_stream())
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    sessions.record_success = MagicMock()
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    sessions.get_approval_policy = MagicMock(return_value="ask")
    return sessions


def _mock_ctx_builder() -> MagicMock:
    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("built_message", None))
    ctx.hooks.on_tool_call = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = False
    return ctx


def _manager(approval) -> SubagentManager:  # type: ignore[no-untyped-def]
    return SubagentManager(
        sessions=_mock_sessions(),
        ctx_builder=_mock_ctx_builder(),
        on_spawn_approval=approval,
        is_yolo=lambda: False,
    )


class _ParkedApproval:
    """An approval callback that parks until released -- a delivered prompt."""

    def __init__(self, answer: bool = True) -> None:
        self.gate = asyncio.Event()
        self.answer = answer
        self.entered = asyncio.Event()

    async def __call__(self, _rid: str, _desc: str, _parent: str = "") -> bool:
        self.entered.set()
        await self.gate.wait()
        return self.answer


async def _reach_the_prompt(approval: _ParkedApproval) -> None:
    """Let the approval task run as far as its await."""
    for _ in range(200):
        if approval.entered.is_set():
            return
        await asyncio.sleep(0)
    raise AssertionError("the approval callback was never reached")


async def _settle(info) -> None:  # type: ignore[no-untyped-def]
    """Let the approval task run to its terminal state."""
    for _ in range(200):
        if info.done:
            return
        await asyncio.sleep(0)


@pytest.fixture
def _pinned_parent(monkeypatch):
    """Resolve every spawn's parent key to this test's crew-log unit."""
    from kiro_crew.crew_log import resolve

    monkeypatch.setattr(resolve, "unit_for_session_key", lambda _sessions, _key: SESSION)


# --------------------------------------------------------------------------
# the bug
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_spawn_waiting_on_a_human_is_pending_in_the_approvals_fold(_pinned_parent):
    """The whole report: an open spawn prompt the fold can see.

    Red before this change: the gate awaited ``_on_spawn_approval`` and recorded
    the wait only to the SEL audit, so the log held no ``approval/requested`` and
    the fold's ``pending`` list was empty while the spawn sat on a human.
    """
    _open_session()
    approval = _ParkedApproval()
    mgr = _manager(approval)
    info = mgr.spawn("Return only the result of 1+1", parent_session_key="dashboard:chat-7")
    assert info is not None and not info.done
    await _reach_the_prompt(approval)
    assert emit.flush()

    folded = _folded()
    assert folded["pending"] == 1, folded
    (row,) = folded["pending_requests"]
    assert row["approval_id"] == f"spawn:{info.id}"
    assert row["tool"] == "spawn_run"
    assert "1+1" in row["reason"], row
    assert row["turn"] == ASKING_TURN, "the entry is filed under the turn that asked"
    assert folded["decided"] == 0, "nobody has answered yet"

    approval.gate.set()
    await _settle(info)


@pytest.mark.asyncio
async def test_an_approved_spawn_is_decided_without_naming_who_answered(_pinned_parent):
    """A person answered, at a surface the gate cannot see, so it asserts neither."""
    _open_session()
    approval = _ParkedApproval(answer=True)
    mgr = _manager(approval)
    info = mgr.spawn("Return only the result of 1+1", parent_session_key="dashboard:chat-7")
    assert info is not None
    await _reach_the_prompt(approval)
    approval.gate.set()
    for _ in range(200):
        if _of("approval/decided"):
            break
        emit.flush()
        await asyncio.sleep(0)
    assert emit.flush()

    (decided,) = _of("approval/decided")
    assert decided["approval_id"] == f"spawn:{info.id}"
    assert decided["decision"] == "approved"
    assert "by" not in decided, "a person's answer is attributed to nobody"
    assert "cause" not in decided
    folded = _folded()
    assert folded["pending"] == 0, "the decision retires the pending row"
    assert folded["by_decision"] == {"approved": 1}


@pytest.mark.asyncio
async def test_a_declined_spawn_is_recorded_as_rejected(_pinned_parent):
    """The decline is as much a fact as the request, and pairs with it."""
    _open_session()

    async def _declined(_rid: str, _desc: str, _parent: str = "") -> bool:
        return False

    mgr = _manager(_declined)
    info = mgr.spawn("Return only the result of 1+1", parent_session_key="dashboard:chat-7")
    assert info is not None
    await _settle(info)
    assert emit.flush()

    folded = _folded()
    assert folded["requested"] == 1 and folded["decided"] == 1
    assert folded["pending"] == 0
    assert folded["unmatched_decisions"] == 0, "the decision found its own request"
    assert folded["by_decision"] == {"rejected": 1}
    assert folded["last"]["approval_id"] == f"spawn:{info.id}"
    assert folded["last"]["by"] == "", "a person declined; the host did not"


@pytest.mark.asyncio
async def test_a_prompt_no_surface_received_is_a_host_decline_with_its_cause(_pinned_parent):
    """An undeliverable prompt is attributable, and its reason code says why.

    The request is still written: the gate DID ask, and a reader who sees only the
    refusal cannot tell an undeliverable prompt from a spawn nobody asked about.
    """
    from kiro_crew.constants import DENY_CAUSE_APPROVAL_UNDELIVERABLE

    _open_session()

    async def _unreachable(_rid: str, _desc: str, _parent: str = "") -> bool:
        raise SpawnApprovalUnreachable("no dashboard client is connected")

    mgr = _manager(_unreachable)
    info = mgr.spawn("Return only the result of 1+1", parent_session_key="dashboard:chat-7")
    assert info is not None
    await _settle(info)
    assert emit.flush()

    assert [row["approval_id"] for row in _of("approval/requested")] == [f"spawn:{info.id}"]
    (decided,) = _of("approval/decided")
    assert decided["decision"] == "rejected"
    assert decided["by"] == "host"
    assert decided["cause"] == DENY_CAUSE_APPROVAL_UNDELIVERABLE
    assert _folded()["pending"] == 0


@pytest.mark.asyncio
async def test_a_callback_that_raised_is_a_host_decline_with_its_own_cause(_pinned_parent):
    """A broken approval path must not read as a person saying no."""
    from kiro_crew.constants import DENY_CAUSE_HOOK_ERROR

    _open_session()

    async def _broken(_rid: str, _desc: str, _parent: str = "") -> bool:
        raise RuntimeError("the approval surface exploded")

    mgr = _manager(_broken)
    info = mgr.spawn("Return only the result of 1+1", parent_session_key="dashboard:chat-7")
    assert info is not None
    await _settle(info)
    assert emit.flush()

    (decided,) = _of("approval/decided")
    assert decided["decision"] == "rejected"
    assert decided["by"] == "host"
    assert decided["cause"] == DENY_CAUSE_HOOK_ERROR


@pytest.mark.asyncio
async def test_a_cancelled_wait_still_answers_its_own_request(_pinned_parent):
    """A user Stop cancels the awaiting task, and no handler in the gate sees it.

    Without the ``finally`` the request would sit in the fold's ``pending`` map for
    the life of the log -- the same silence this fix removes, one step along. The
    host is named because nothing judged the spawn, and no cause is written
    because there is no reason code for a wait that ended rather than resolved.
    """
    _open_session()
    approval = _ParkedApproval()
    mgr = _manager(approval)
    info = mgr.spawn("Return only the result of 1+1", parent_session_key="dashboard:chat-7")
    assert info is not None
    await _reach_the_prompt(approval)
    assert emit.flush()
    assert _folded()["pending"] == 1

    task = mgr._tasks.get(info.id)
    assert task is not None
    task.cancel()
    for _ in range(200):
        if task.done():
            break
        await asyncio.sleep(0)
    assert emit.flush()

    (decided,) = _of("approval/decided")
    assert decided["approval_id"] == f"spawn:{info.id}"
    assert decided["decision"] == "rejected"
    assert decided["by"] == "host"
    assert "cause" not in decided
    assert _folded()["pending"] == 0


@pytest.mark.asyncio
async def test_an_unresolvable_parent_writes_neither_half(_pinned_parent, monkeypatch):
    """No pin means no parent to file the pair under, and a guess would be worse.

    Checked as a PAIR: a decision written without its request would read to the
    fold as an unmatched decision about a session that never asked.
    """
    from kiro_crew.crew_log import resolve

    monkeypatch.setattr(resolve, "unit_for_session_key", lambda _sessions, _key: "")
    _open_session()

    async def _declined(_rid: str, _desc: str, _parent: str = "") -> bool:
        return False

    mgr = _manager(_declined)
    info = mgr.spawn("Return only the result of 1+1", parent_session_key="dashboard:chat-7")
    assert info is not None
    await _settle(info)
    assert emit.flush()

    folded = _folded()
    assert folded["requested"] == 0 and folded["decided"] == 0
    assert folded["unmatched_decisions"] == 0


# --------------------------------------------------------------------------
# the reader the gate needs
# --------------------------------------------------------------------------


def test_dispatch_origin_answers_for_a_pin_the_opener_has_not_reached():
    """The prompt happens in the window ``child_origin`` is designed to refuse.

    ``child_origin`` gates on opened so a fact about a child that never started
    has no cause in the log. A spawn approval is the exception: the wait IS the
    cause of the gap, and on two of its three exits no run ever starts.
    """
    emit.remember_child_origin("ab12", SESSION, 4)
    assert emit.child_origin("ab12") == ("", 0), "unopened, so the gated reader refuses"
    assert emit.dispatch_origin("ab12") == (SESSION, 4)
    emit.open_child_origin("ab12")
    assert emit.dispatch_origin("ab12") == (SESSION, 4), "and it keeps answering after"


def test_dispatch_origin_refuses_a_child_with_no_pin_at_all():
    """``("", 0)`` is what tells a caller to skip the write rather than guess."""
    assert emit.dispatch_origin("never-pinned") == ("", 0)
    assert emit.dispatch_origin("") == ("", 0)


# --------------------------------------------------------------------------
# the pair is all-or-nothing by construction
# --------------------------------------------------------------------------


def test_the_decision_helper_is_a_no_op_for_a_request_that_was_not_written():
    """The two halves are bound by the returned origin, not by two separate checks.

    The request helper answers ``("", 0)`` when it wrote nothing, and handing that
    back is what makes the decision write nothing too -- so a log can never hold a
    spawn decision whose request is absent. The closer lives on
    ``ManagerComponent`` because a running child's tool prompts owe the log the
    same pair, so this reaches it through the gate, the asker under test here.
    """
    from kiro_crew.subagent_manager.admission.gate import _GateMixin

    _open_session()
    _GateMixin._record_crew_log_approval_decided(
        SimpleNamespace(),
        ("", 0),
        approval_id="spawn:orphan",
        decision="approved",
    )
    assert emit.flush()
    assert _of("approval/decided") == []
