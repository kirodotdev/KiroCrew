"""A subagent's MID-RUN tool approval reaches the crew log.

The reported shape: a running subagent hits a tool call that needs the user's
answer, parks on the prompt, and the crew log holds nothing about the wait -- no
``approval/requested`` while it is open and no ``approval/decided`` once it is
answered. ``fold_approvals`` is therefore empty for a session whose child is
blocked on a human, while the parent's OWN mid-turn prompts do land because the
dashboard chat runner writes both halves around its permission await.

This is the sibling of the spawn gate: that prompt is the one before the child
starts, and ``test_spawn_approval_crew_log.py`` pins it. This one is a prompt
raised by a child that is already running, which is a different population --
potentially many per run -- and a different origin reader.

What these tests pin is the pair at all THREE awaited approval arms in
``subagent_manager/run.py``: the per-subagent factory, the gateway-level
fallback, and the low-fidelity child branch that downgrades to whichever of the
two is attached. One entry before the await, keyed by the request's own id and
filed under the PARENT turn that asked for the child, and one answering entry on
every exit -- a yes, a no, a callback that raised, and a wait that was cancelled
rather than answered. The recorded id is scoped to the child, because the id a
child answers on is unique only to its own connection while the parent's fold
keys every pending request by that id alone.

Deliberately NOT covered, because the change does not make them: the child's own
crew log (no subagent path opens one), the dashboard's rendering of the folded
pending row, and the auto-approve rungs, which answer without a prompt and so
have no approval to record.
"""

from __future__ import annotations

import asyncio
import json
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from off_loop_helpers import off_loop

from kiro_crew.crew_log import crew_log_path, emit
from kiro_crew.crew_log.projection import fold_approvals
from kiro_crew.crew_log.store import CrewLog
from kiro_crew.execution_context import execution_for_store
from kiro_crew.hooks import TOOL_ALLOW, ToolHookResult
from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.subagent_manager._component import ManagerComponent

SESSION = "acp-child-tool-approval-1"
ASKING_TURN = 4
AGENT_ID = "ct01"
#: The id the child answers on the wire -- its own connection's JSON-RPC message
#: id, which each backend counts up from zero on its own.
REQUEST_ID = 9001
#: The id the crew log files that same prompt under, scoped to the child so two
#: children sharing a wire id cannot overwrite each other in one parent's fold.
LOGGED_ID = f"{AGENT_ID}:{REQUEST_ID}"


@pytest.fixture(autouse=True)
def _close_subagent_managers(close_subagent_managers):
    """Every manager built here is closed at teardown; the body is in ``conftest``.

    Construction opens the durable task queue against the data home this module
    points at, and a manager merely dropped keeps that open -- so a later test in
    the same worker reads a run record through a handle onto this module's
    deleted ``tmp_path``.
    """


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


def _folded() -> dict:
    """The ``approvals`` fold over the session's whole log, as a reader sees it.

    Read off the event-loop thread, the way the dashboard's own handlers reach
    the store. A crew-log read takes the unit's append lock, and an acquire on
    the loop thread makes a single attempt -- so a read from an ``async def``
    body is refused outright whenever the writer thread or the eager folder
    holds that lock.
    """

    def _read() -> dict:
        handle = CrewLog.open("session", SESSION)
        try:
            return fold_approvals(tuple(handle.iter_from(1)))
        finally:
            handle.release_ownership()

    return off_loop(_read)


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
# a running child that raises one permission request
# --------------------------------------------------------------------------


def _permission_event(**kw: object) -> LLMEvent:
    """A gated tool call from the subagent's own slot, with full security context."""
    fields: dict = {
        "kind": EVENT_PERMISSION_REQUEST,
        "title": "Run: rm -rf build",
        "request_id": REQUEST_ID,
        "tool_name": "execute_bash",
        "is_shell": True,
        "shell_classified": True,
        "raw_params_trusted": True,
        "raw_tool_params": {"command": "rm -rf build"},
    }
    fields.update(kw)
    return LLMEvent(**fields)  # type: ignore[arg-type]


def _low_fidelity_event() -> LLMEvent:
    """A runtime-routed child request whose security context is absent.

    ``child_low_fidelity`` needs a ``sub_session_id`` (the request came through
    the backend's own child routing) plus a missing provenance -- here the shell
    classification never resolved, so ``is_shell`` is the miss default and every
    path-scope check is blind. That is the one shape reaching the third arm.
    """
    return _permission_event(
        sub_session_id="child-sess-1",
        shell_classified=False,
        raw_params_trusted=False,
    )


def _manager(event: LLMEvent, *, agent_id: str = AGENT_ID, **manager_kw: object) -> tuple:
    """A manager whose child streams *event* once, with the pin already placed."""
    provider = MagicMock()

    async def _stream(*_a: object, **_kw: object):  # type: ignore[no-untyped-def]
        yield event

    provider.stream = MagicMock(side_effect=lambda *a, **kw: _stream())
    provider.approve_tool = AsyncMock()
    provider.reject_tool = AsyncMock()
    provider.supports_steer = False

    sessions = MagicMock()
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    sessions.get_approval_policy = MagicMock(return_value="ask")
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    sessions.release_subagent_runtime = AsyncMock()

    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("msg", None))
    # TOOL_ALLOW is the hook saying "not my call to auto-approve or deny", which
    # is what lets the request reach the approval arms under test.
    ctx.hooks.on_tool_call = MagicMock(return_value=ToolHookResult(action=TOOL_ALLOW))

    manager = SubagentManager(
        sessions=sessions,
        ctx_builder=ctx,
        default_turn_limit=1,
        **manager_kw,  # type: ignore[arg-type]
    )
    info = SubagentInfo(
        execution_context=execution_for_store(""),
        id=agent_id,
        task="Delete the build directory",
        parent_session_key="dashboard:chat-7",
    )
    # The pin the dispatch leaves behind, then the opener the start writes. Both
    # are needed: ``child_origin`` refuses an unopened pin, because an entry
    # about a child with no ``subagent/spawned`` line is a fact with no cause.
    emit.remember_child_origin(agent_id, SESSION, ASKING_TURN)
    manager._agents[agent_id] = info
    manager._log_spawned(info)
    return manager, info, provider


async def _drive(manager: SubagentManager, info: SubagentInfo) -> None:
    """Run the child's event loop to completion."""
    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
        patch("kiro_crew.subagent.update_state"),
        patch("kiro_crew.subagent.create_agent_folder", MagicMock()),
    ):
        await manager._run_inner(info, f"subagent:{info.id}")


class _ParkedApproval:
    """An approval callback that parks until released -- a delivered prompt."""

    def __init__(self, answer: bool = True) -> None:
        self.gate = asyncio.Event()
        self.answer = answer
        self.entered = asyncio.Event()

    async def __call__(self, *_a: object, **_kw: object) -> bool:
        self.entered.set()
        await self.gate.wait()
        return self.answer


async def _reach_the_prompt(approval: _ParkedApproval) -> None:
    """Let the run task get as far as its approval await.

    A real sleep rather than ``sleep(0)``: the path from the stream to the
    approval arm awaits provider calls of its own, and yielding without
    advancing the clock does not always let them all resolve.
    """
    for _ in range(600):
        if approval.entered.is_set():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the approval callback was never reached")


# --------------------------------------------------------------------------
# the bug
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_child_waiting_on_a_human_is_pending_in_the_approvals_fold():
    """The whole report: an open child tool prompt the fold can see.

    Red before this change: ``run.py`` awaited the approval callback and recorded
    the wait only to the SEL audit, so the log held no ``approval/requested`` and
    the fold's ``pending`` list was empty while the child sat on a human.
    """
    _open_session()
    approval = _ParkedApproval()
    manager, info, _provider = _manager(
        _permission_event(), on_tool_approval_factory=lambda _info: approval
    )
    task = asyncio.ensure_future(_drive(manager, info))
    await _reach_the_prompt(approval)
    assert emit.flush()

    folded = _folded()
    assert folded["pending"] == 1, folded
    (row,) = folded["pending_requests"]
    assert row["approval_id"] == LOGGED_ID
    assert row["tool"] == "execute_bash"
    assert "rm -rf build" in row["reason"], row
    assert row["turn"] == ASKING_TURN, "the entry is filed under the turn that asked"
    assert folded["decided"] == 0, "nobody has answered yet"

    approval.gate.set()
    await task


@pytest.mark.asyncio
async def test_an_approved_child_tool_is_decided_without_naming_who_answered():
    """A person answered, at a surface the run cannot see, so it asserts neither."""
    _open_session()
    approval = _ParkedApproval(answer=True)
    manager, info, provider = _manager(
        _permission_event(), on_tool_approval_factory=lambda _info: approval
    )
    approval.gate.set()
    await _drive(manager, info)
    assert emit.flush()

    provider.approve_tool.assert_awaited_once_with(REQUEST_ID)
    (decided,) = _of("approval/decided")
    assert decided["approval_id"] == LOGGED_ID
    assert decided["decision"] == "approved"
    assert "by" not in decided, "a person's answer is attributed to nobody"
    assert "cause" not in decided
    folded = _folded()
    assert folded["pending"] == 0, "the decision retires the pending row"
    assert folded["by_decision"] == {"approved": 1}


@pytest.mark.asyncio
async def test_a_declined_child_tool_is_recorded_as_rejected():
    """The decline is as much a fact as the request, and pairs with it."""
    _open_session()

    async def _declined(*_a: object, **_kw: object) -> bool:
        return False

    manager, info, provider = _manager(
        _permission_event(), on_tool_approval_factory=lambda _info: _declined
    )
    await _drive(manager, info)
    assert emit.flush()

    provider.reject_tool.assert_awaited_once_with(REQUEST_ID)
    folded = _folded()
    assert folded["requested"] == 1 and folded["decided"] == 1
    assert folded["pending"] == 0
    assert folded["unmatched_decisions"] == 0, "the decision found its own request"
    assert folded["by_decision"] == {"rejected": 1}
    assert folded["last"]["by"] == "", "a person declined; the host did not"


@pytest.mark.asyncio
async def test_the_gateway_level_arm_records_its_own_pair():
    """The second awaited site: no per-subagent factory, one gateway callback.

    A manager built without ``on_tool_approval_factory`` falls to the
    gateway-level ``on_tool_approval``, and that arm awaits a human exactly as
    the factory one does -- so it owes the log the same pair.
    """
    _open_session()
    approval = _ParkedApproval(answer=True)
    manager, info, provider = _manager(_permission_event(), on_tool_approval=approval)
    task = asyncio.ensure_future(_drive(manager, info))
    await _reach_the_prompt(approval)
    assert emit.flush()
    assert _folded()["pending"] == 1, "the gateway arm's wait is visible too"

    approval.gate.set()
    await task
    assert emit.flush()

    provider.approve_tool.assert_awaited_once_with(REQUEST_ID)
    (requested,) = _of("approval/requested")
    assert requested["approval_id"] == LOGGED_ID
    assert requested["turn"] == ASKING_TURN
    (decided,) = _of("approval/decided")
    assert decided["decision"] == "approved"
    assert _folded()["pending"] == 0


@pytest.mark.asyncio
async def test_the_low_fidelity_child_arm_records_its_own_pair():
    """The third awaited site, unnamed in the report and the easiest to miss.

    A child request with no recoverable security context skips every
    auto-approve rung and downgrades to the interactive approver. It awaits a
    human like the other two arms, so a fix covering only the named two would
    leave this one silent.
    """
    _open_session()
    approval = _ParkedApproval(answer=True)
    manager, info, provider = _manager(
        _low_fidelity_event(), on_tool_approval_factory=lambda _info: approval
    )
    task = asyncio.ensure_future(_drive(manager, info))
    await _reach_the_prompt(approval)
    assert emit.flush()

    folded = _folded()
    assert folded["pending"] == 1, folded
    (row,) = folded["pending_requests"]
    assert row["approval_id"] == LOGGED_ID
    assert row["turn"] == ASKING_TURN

    approval.gate.set()
    await task
    assert emit.flush()

    provider.approve_tool.assert_awaited_once_with(REQUEST_ID)
    (decided,) = _of("approval/decided")
    assert decided["decision"] == "approved"
    assert _folded()["pending"] == 0


@pytest.mark.asyncio
async def test_a_callback_that_raised_answers_its_own_request():
    """A broken approver must not leave the request pending for the log's life.

    The low-fidelity arm swallows the exception and treats it as a refusal, so
    the pair still closes -- and the host is named, because nothing a person did
    produced that outcome.
    """
    _open_session()

    async def _broken(*_a: object, **_kw: object) -> bool:
        raise RuntimeError("the approval surface exploded")

    manager, info, provider = _manager(
        _low_fidelity_event(), on_tool_approval_factory=lambda _info: _broken
    )
    await _drive(manager, info)
    assert emit.flush()

    provider.reject_tool.assert_awaited_once_with(REQUEST_ID)
    assert [row["approval_id"] for row in _of("approval/requested")] == [LOGGED_ID]
    (decided,) = _of("approval/decided")
    assert decided["decision"] == "rejected"
    assert decided["by"] == "host", "nothing a person did produced this outcome"
    assert _folded()["pending"] == 0


@pytest.mark.asyncio
async def test_a_cancelled_wait_still_answers_its_own_request():
    """A user Stop cancels the running task, and no handler in the arm sees it.

    Without the ``finally`` the request would sit in the fold's ``pending`` map
    for the life of the log -- the same silence this fix removes, one step along.
    """
    _open_session()
    approval = _ParkedApproval()
    manager, info, _provider = _manager(
        _permission_event(), on_tool_approval_factory=lambda _info: approval
    )
    task = asyncio.ensure_future(_drive(manager, info))
    await _reach_the_prompt(approval)
    assert emit.flush()
    assert _folded()["pending"] == 1

    task.cancel()
    for _ in range(400):
        if task.done():
            break
        await asyncio.sleep(0)
    assert emit.flush()

    (decided,) = _of("approval/decided")
    assert decided["approval_id"] == LOGGED_ID
    assert decided["decision"] == "rejected"
    assert decided["by"] == "host", "nothing judged the call; the wait just ended"
    assert "cause" not in decided
    assert _folded()["pending"] == 0


@pytest.mark.asyncio
async def test_an_unpinned_child_writes_neither_half():
    """No pin means no parent to file the pair under, and a guess would be worse.

    Checked as a PAIR: a decision written without its request would read to the
    fold as an unmatched decision about a session that never asked.
    """
    _open_session()

    async def _declined(*_a: object, **_kw: object) -> bool:
        return False

    manager, info, _provider = _manager(
        _permission_event(), on_tool_approval_factory=lambda _info: _declined
    )
    emit.forget_child_origin(AGENT_ID)
    await _drive(manager, info)
    assert emit.flush()

    folded = _folded()
    assert folded["requested"] == 0 and folded["decided"] == 0
    assert folded["unmatched_decisions"] == 0


def test_two_children_of_one_parent_sharing_a_wire_id_keep_separate_rows():
    """The id a child answers on is unique to its own connection, not to the log.

    ``event.request_id`` is the JSON-RPC message id of one child's ACP
    connection, which each backend counts up from zero on its own, so two
    children of one parent routinely raise the SAME id. They share that parent's
    crew log, the fold keys pending requests by the recorded id alone, and the
    parent's own prompts share that map -- so recording the bare wire id would
    let the second request overwrite the first: answering one would clear the row
    while the other child is still parked, and pair that decision with the wrong
    tool.

    Driven through the recorder pair rather than two concurrent runs. Each arm's
    own test above already proves it records through ``_crew_log_approval_id``;
    what is unproven until here is that the ids that helper mints for two
    children survive together in one parent's fold.
    """
    from kiro_crew.subagent_manager.run import RunEventCoordinator

    _open_session()
    recorder = SimpleNamespace(
        _crew_log_approval_id=partial(RunEventCoordinator._crew_log_approval_id, None),
        _record_crew_log_tool_approval_requested=partial(
            RunEventCoordinator._record_crew_log_tool_approval_requested, None
        ),
        _record_crew_log_approval_decided=partial(
            ManagerComponent._record_crew_log_approval_decided, None
        ),
    )
    first = SimpleNamespace(id="ct0a")
    second = SimpleNamespace(id="ct0b")
    for child in (first, second):
        emit.remember_child_origin(child.id, SESSION, ASKING_TURN)
        emit.open_child_origin(child.id)

    # Both children raise the SAME wire id, which is the ordinary case.
    first_id = recorder._crew_log_approval_id(first, REQUEST_ID)
    second_id = recorder._crew_log_approval_id(second, REQUEST_ID)
    assert first_id != second_id, "the recorded id must tell two children apart"

    first_origin = recorder._record_crew_log_tool_approval_requested(
        first, approval_id=first_id, tool="execute_bash", reason="Run: rm -rf build"
    )
    second_origin = recorder._record_crew_log_tool_approval_requested(
        second, approval_id=second_id, tool="fs_write", reason="Write: src/app.py"
    )
    assert emit.flush()

    folded = _folded()
    assert folded["pending"] == 2, folded
    assert {row["approval_id"] for row in folded["pending_requests"]} == {first_id, second_id}

    # Answer the FIRST child only. The second is still parked, so its row must
    # survive -- that survival is the whole finding.
    recorder._record_crew_log_approval_decided(
        first_origin, approval_id=first_id, decision="approved"
    )
    assert emit.flush()
    folded = _folded()
    assert folded["pending"] == 1, folded
    (still_open,) = folded["pending_requests"]
    assert still_open["approval_id"] == second_id
    assert still_open["tool"] == "fs_write", "the surviving row is the right child's"
    assert folded["by_decision"] == {"approved": 1}
    assert folded["unmatched_decisions"] == 0

    recorder._record_crew_log_approval_decided(
        second_origin, approval_id=second_id, decision="rejected"
    )
    assert emit.flush()
    folded = _folded()
    assert folded["pending"] == 0
    assert folded["by_decision"] == {"approved": 1, "rejected": 1}
    assert folded["unmatched_decisions"] == 0


# --------------------------------------------------------------------------
# the reader the run needs
# --------------------------------------------------------------------------


def test_child_origin_is_the_right_reader_for_a_prompt_raised_mid_run():
    """A mid-run prompt happens AFTER the opener, where the gated reader answers.

    ``dispatch_origin`` is the spawn gate's reader because that prompt precedes
    the opener. Here the run has started, so the ordinary gated reader resolves
    and keeps the invariant it exists for: no entry about a child whose
    ``subagent/spawned`` line is absent.
    """
    emit.remember_child_origin("cd34", SESSION, ASKING_TURN)
    assert emit.child_origin("cd34") == ("", 0), "unopened, so the gated reader refuses"
    emit.open_child_origin("cd34")
    assert emit.child_origin("cd34") == (SESSION, ASKING_TURN)


def test_child_origin_refuses_a_child_with_no_pin_at_all():
    """``("", 0)`` is what tells a caller to skip the write rather than guess."""
    assert emit.child_origin("never-pinned") == ("", 0)
    assert emit.child_origin("") == ("", 0)


# --------------------------------------------------------------------------
# the pair is all-or-nothing by construction
# --------------------------------------------------------------------------


def test_the_decision_helper_is_a_no_op_for_a_request_that_was_not_written():
    """The two halves are bound by the returned origin, not by two separate checks.

    The request helper answers ``("", 0)`` when it wrote nothing, and handing that
    back is what makes the decision write nothing too -- so a log can never hold a
    child tool decision whose request is absent. One closer serves both askers,
    the spawn gate and this one, so this pins the guarantee for both.
    """
    _open_session()
    ManagerComponent._record_crew_log_approval_decided(
        SimpleNamespace(),
        ("", 0),
        approval_id="orphan",
        decision="approved",
    )
    assert emit.flush()
    assert _of("approval/decided") == []
