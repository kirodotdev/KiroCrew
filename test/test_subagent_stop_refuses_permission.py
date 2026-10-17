"""A Stop during a sub-agent's run never lets a later tool run.

The shape: a running sub-agent waits on a person for tool A through the
dashboard's approval coordinator, and the user presses Stop. The stop tears the
session down first and cancels the run task after that; the coordinator turns
the cancel into a denial, so the run loop resumes. When the teardown works, the
next read hits the dead stream and the run ends. When the teardown hangs or
raises AND its kill fallback fails, the stream is still live, and the backend's
next request (tool B, one the approver grants without asking, as slot trust or
``--approval yolo`` do) reached the permission ladder and was approved after
the Stop.

What is pinned: with a stop in progress the run bails every request it reads
as ``stopped`` (audited as a stop, rejected on the wire) before any grant or
person is consulted, and ends there, for the hung and the failed teardown; a
working teardown still ends the run before B is read; a request settled while
a stop lands in any await before its answer (the run's diagnostics write and
tool event, the hook gate, a person's yes, or the stream itself) is bailed as
``stopped`` and never approved, whether a grant, the gate's hit or a person
would have approved it, and the run ends at that request; and a run with no
stop settles its requests as before. Everything runs in-process against fakes:
the ACP client, the session reset and the kill fallback. No process is
signalled.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import tool_permission
from kiro_crew.acp.transport_errors import AcpProcessDied
from kiro_crew.dashboard.interaction_coordinator import ApprovalCoordinator
from kiro_crew.execution_context import execution_for_store
from kiro_crew.hooks import TOOL_ALLOW, TOOL_AUTO_APPROVE, ToolHookResult
from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.testing.wait import async_wait_until

AGENT_ID = "stop01"
#: The request the person is asked about when the Stop lands.
A_ID = 101
#: The request the backend sends as soon as A is answered.
B_ID = 102
#: The request after B: a run that ends at B never reads it.
C_ID = 103
_TITLES = {
    A_ID: "rm -rf build",
    B_ID: "curl -X POST https://example.invalid/deploy",
    C_ID: "git push origin release",
}
#: Generous bound for the run task to finish; every wait below ends on an event.
_SETTLE_SECS = 30.0


@pytest.fixture(autouse=True)
def _close_subagent_managers(close_subagent_managers):
    """Every manager built here is closed at teardown; the body is in ``conftest``."""


@pytest.fixture(autouse=True)
def _pinned_data_home(tmp_path, _floor_monkeypatch):
    """Each test reads and writes only under its own tmp_path. The pins go through
    the isolation floor's own ``MonkeyPatch``, so a test's ``monkeypatch.undo()``
    cannot lift them."""
    for var, sub in (("KIROCREW_HOME", "home"), ("KIROCREW_WORKSPACE", "workspace")):
        path = tmp_path / sub
        path.mkdir()
        _floor_monkeypatch.setenv(var, str(path))


def _event(request_id: int) -> LLMEvent:
    title = _TITLES[request_id]
    return LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title=title,
        request_id=request_id,
        tool_name="execute_bash",
        is_shell=True,
        shell_classified=True,
        raw_params_trusted=True,
        raw_tool_params={"command": title},
    )


class _Backend:
    """A fake ACP client: each of *requests* in turn, the next as soon as one is answered.

    ``torn_down`` is what a working session reset does to a real stream: the
    next read raises the runtime's death. ``before_send`` runs a hook while the
    run waits on the stream for that request.
    """

    def __init__(self, log: list[str], requests: tuple[int, ...] = (A_ID, B_ID)) -> None:
        self.log = log
        self.requests = requests
        self.torn_down = False
        self.before_send: dict[int, Callable[[], Awaitable[None]]] = {}
        self._answered: dict[int, asyncio.Event] = {}

    def _answer_of(self, request_id: object) -> asyncio.Event:
        return self._answered.setdefault(int(request_id), asyncio.Event())  # type: ignore[call-overload]

    def answered(self, request_id: int) -> bool:
        return self._answer_of(request_id).is_set()

    async def approve_tool(self, request_id: object) -> bool:
        self.log.append(f"approve_tool:{request_id}")
        self._answer_of(request_id).set()
        return True

    async def reject_tool(self, request_id: object) -> None:
        self.log.append(f"reject_tool:{request_id}")
        self._answer_of(request_id).set()

    async def _await_answer(self, request_id: int) -> None:
        def answered() -> bool:
            if self._answer_of(request_id).is_set():
                return True
            if self.torn_down:
                raise AcpProcessDied("runtime torn down by the reset")
            return False

        await async_wait_until(
            answered,
            timeout=_SETTLE_SECS,
            describe=lambda: f"request {request_id} was never answered: {self.log}",
        )

    async def stream(self, *_a: object, **_kw: object):  # type: ignore[no-untyped-def]
        for index, request_id in enumerate(self.requests):
            if index:
                if self.torn_down:
                    raise AcpProcessDied("runtime torn down by the reset")
                self.log.append(f"backend_sends:{request_id}")
            hook = self.before_send.get(request_id)
            if hook is not None:
                await hook()
            yield _event(request_id)
            await self._await_answer(request_id)


def _provider(backend: _Backend) -> MagicMock:
    provider = MagicMock()
    provider.stream = MagicMock(side_effect=lambda *a, **kw: backend.stream())
    provider.approve_tool = AsyncMock(side_effect=backend.approve_tool)
    provider.reject_tool = AsyncMock(side_effect=backend.reject_tool)
    provider.supports_steer = False
    return provider


def _sessions(
    provider: MagicMock, *, policy: str, reset: Callable[..., Awaitable[bool]]
) -> MagicMock:
    sessions = MagicMock()
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    sessions.get_approval_policy = MagicMock(return_value=policy)
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    sessions.release_subagent_runtime = AsyncMock()
    sessions.reset = AsyncMock(side_effect=reset)
    return sessions


def _info() -> SubagentInfo:
    return SubagentInfo(
        execution_context=execution_for_store(""),
        id=AGENT_ID,
        task="Delete the build directory",
        parent_session_key="dashboard:chat-7",
    )


async def _failed_kill(log: list[str]) -> str:
    log.append("kill_failed")
    return "the fallback kill signalled nothing"


class _Harness:
    """One sub-agent run whose approver asks a person for A and grants B."""

    def __init__(self, reset: str, requests: tuple[int, ...] = (A_ID, B_ID)) -> None:
        self.log: list[str] = []
        self.backend = _Backend(self.log, requests)
        self.state = MagicMock()
        self.state._approval_futures = {}
        self.state._pending_approvals = {}
        self.state._BACKGROUND_APPROVAL_TIMEOUT_SECS = 180
        self.state._APPROVAL_TIMEOUT = 7200
        self.reset_mode = reset

        ctx = MagicMock()
        ctx.build_message = MagicMock(return_value=("msg", None))
        ctx.hooks.on_tool_call = MagicMock(return_value=ToolHookResult(action=TOOL_ALLOW))

        self.manager = SubagentManager(
            sessions=_sessions(_provider(self.backend), policy="ask", reset=self._reset),
            ctx_builder=ctx,
            default_turn_limit=10,
            on_tool_approval_factory=lambda _info: self._approver,
        )
        self.info = _info()
        self.manager._agents[AGENT_ID] = self.info
        self.manager._log_spawned(self.info)

    async def _reset(self, _key: str, **_kw: object) -> bool:
        self.log.append("reset")
        if self.reset_mode == "tears_down":
            self.backend.torn_down = True
            return True
        if self.reset_mode == "hangs":
            await asyncio.Event().wait()
        raise RuntimeError("session reset failed")

    async def _approver(self, event: LLMEvent) -> bool:
        if event.request_id != A_ID:
            # A grant the gateway's callback answers without a card.
            self.log.append(f"granted:{event.request_id}")
            return True
        outcome = await ApprovalCoordinator.request(
            self.state,
            str(event.request_id),
            "subagent",
            event.title or "",
            tool_input="",
            tool_purpose="",
            slot="chat-7",
            is_background=True,
            redact_url=lambda text: (text, None),
            redact_secret=lambda text: (text, None),
        )
        self.log.append(f"approval_returned:{event.request_id}:{outcome}")
        return outcome

    async def run(self, *, stop: bool) -> asyncio.Task:  # type: ignore[type-arg]
        manager = self.manager
        cancel = manager._cancel_task_intentionally

        def _cancel(task, info=None, *, reason):  # type: ignore[no-untyped-def]
            self.log.append("cancel")
            return cancel(task, info, reason=reason)

        async def _kill(*_a: object, **_kw: object) -> str:
            return await _failed_kill(self.log)

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as self.sel,
            patch("kiro_crew.subagent.update_state"),
            patch("kiro_crew.subagent.create_agent_folder", MagicMock()),
            patch("kiro_crew.subagent._RESET_TIMEOUT", 0.2),
            patch.object(manager, "_cancel_task_intentionally", _cancel),
            patch.object(manager, "_sessions_under", MagicMock(return_value=[])),
            patch.object(manager, "_sigkill_sessions", _kill),
        ):
            task = asyncio.ensure_future(manager._run_inner(self.info, f"subagent:{AGENT_ID}"))
            manager._tasks[AGENT_ID] = task
            await self._until_a_waits(task)
            if stop:
                self.log.append("STOP")
                stopping = asyncio.ensure_future(manager.cancel(AGENT_ID))
                await asyncio.wait({task}, timeout=_SETTLE_SECS)
                await asyncio.wait({stopping}, timeout=_SETTLE_SECS)
            else:
                self.state._approval_futures[str(A_ID)].set_result(True)
                await asyncio.wait({task}, timeout=_SETTLE_SECS)
        assert task.done(), f"the run did not finish: {self.log}"
        return task

    async def _until_a_waits(self, task: asyncio.Task) -> None:  # type: ignore[type-arg]
        await async_wait_until(
            lambda: str(A_ID) in self.state._pending_approvals or task.done(),
            timeout=_SETTLE_SECS,
            describe=lambda: f"A never waited: {self.log}",
        )
        assert str(A_ID) in self.state._pending_approvals, f"A never waited: {self.log}"

    def after_stop(self) -> list[str]:
        return self.log[self.log.index("STOP") :]


def _audit_rows(sel: MagicMock, request_id: int) -> list[dict]:
    """The SEL rows the run's permission audit wrote for *request_id*."""
    calls = sel.return_value.log_tool_invocation.call_args_list
    return [call.kwargs for call in calls if call.kwargs.get("request_id") == request_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("reset", ["hangs", "raises"])
async def test_a_stop_whose_teardown_fails_approves_no_further_tool(reset):
    """The reported shape: the teardown and its kill fail, and B must still not run.

    Red before the fix: the swallowed cancel resumed the loop, B was granted,
    and ``approve_tool:102`` was sent after the Stop. B is now bailed before any
    grant is consulted, and the run ends there: C is never read.
    """
    harness = _Harness(reset, requests=(A_ID, B_ID, C_ID))
    await harness.run(stop=True)
    log = harness.after_stop()

    assert "kill_failed" in log, "the variant needs the kill fallback to fail"
    assert log.index("reset") < log.index("cancel") < log.index(f"approval_returned:{A_ID}:False")
    assert not [entry for entry in log if entry.startswith("approve_tool:")], log
    assert f"granted:{B_ID}" not in log, "no grant may be consulted for a stopped run"
    assert f"reject_tool:{B_ID}" in log, "the backend is told no"
    (row,) = _audit_rows(harness.sel, B_ID)
    assert (row["outcome"], row["error"]) == ("denied", "stopped"), "audited as a stop"
    assert log[-1] == f"reject_tool:{B_ID}", "the run ends right after B is rejected"
    assert f"backend_sends:{C_ID}" not in log, "a stopped run reads nothing after the bail"


@pytest.mark.asyncio
async def test_a_stop_whose_teardown_works_ends_the_run_before_the_next_request():
    """Control: a working teardown kills the stream, so B is never read."""
    harness = _Harness("tears_down")
    task = await harness.run(stop=True)
    log = harness.after_stop()

    assert log.index("reset") < log.index("cancel")
    assert f"backend_sends:{B_ID}" not in log
    assert not [entry for entry in log if entry.startswith("approve_tool:")], log
    assert isinstance(task.exception(), AcpProcessDied)


@pytest.mark.asyncio
async def test_a_request_with_no_stop_is_settled_as_before():
    """Control: without a stop, the person's yes and the grant both reach the wire."""
    harness = _Harness("raises")
    task = await harness.run(stop=False)

    assert task.exception() is None
    assert harness.log == [
        f"approval_returned:{A_ID}:True",
        f"approve_tool:{A_ID}",
        f"backend_sends:{B_ID}",
        f"granted:{B_ID}",
        f"approve_tool:{B_ID}",
    ]


class _RaceHarness:
    """One run settling B while a Stop lands in one await before B is answered.

    The session reset is held until B is answered, so the stop stays in
    progress (``_reap_started`` set, the run task not cancelled yet) while the
    run goes on: the window a hung teardown opens. *route* is what would
    approve B: ``auto`` (``parent_policy=auto``), ``gate`` (the hook gate's
    auto-approve) or ``person`` (an approver who says yes). *step* is the await
    the Stop lands in.
    """

    def __init__(self, route: str, step: str) -> None:
        self.log: list[str] = []
        self.route = route
        self.step = step
        self.backend = _Backend(self.log, requests=(B_ID, C_ID))
        self.release = asyncio.Event()
        self.stopping: asyncio.Future | None = None  # type: ignore[type-arg]
        if step == "stream":
            self.backend.before_send[B_ID] = self.land_stop

        self.ctx = MagicMock()
        self.ctx.build_message = MagicMock(return_value=("msg", None))
        verdict = TOOL_AUTO_APPROVE if route == "gate" else TOOL_ALLOW
        self.ctx.hooks.on_tool_call = MagicMock(return_value=ToolHookResult(action=verdict))

        self.manager = SubagentManager(
            sessions=_sessions(
                _provider(self.backend),
                policy="auto" if route == "auto" else "ask",
                reset=self._reset,
            ),
            ctx_builder=self.ctx,
            default_turn_limit=10,
            on_tool_approval_factory=lambda _info: self._approver,
        )
        self.info = _info()
        self.manager._agents[AGENT_ID] = self.info
        self.manager._log_spawned(self.info)

    async def land_stop(self) -> None:
        """Press Stop, and return once its reap is parked in the held reset."""
        if self.stopping is not None:
            return
        self.log.append("STOP")
        self.stopping = asyncio.ensure_future(self.manager.cancel(AGENT_ID))
        await async_wait_until(
            lambda: "reset" in self.log,
            timeout=_SETTLE_SECS,
            describe=lambda: f"the stop never reached its reset: {self.log}",
        )

    async def _reset(self, _key: str, **_kw: object) -> bool:
        self.log.append("reset")
        await asyncio.wait_for(self.release.wait(), timeout=_SETTLE_SECS)
        raise RuntimeError("session reset failed")

    async def _approver(self, event: LLMEvent) -> bool:
        self.log.append(f"asked:{event.request_id}")
        if self.step == "approver":
            await self.land_stop()
        return True

    def _step_patches(self) -> list:  # type: ignore[type-arg]
        manager = self.manager
        if self.step == "diagnostics":
            write = manager._write_state_off_loop

            async def _write(info: SubagentInfo, what: str, **fields: object) -> object:
                if what == "diagnostics":
                    await self.land_stop()
                return await write(info, what, **fields)

            return [patch.object(manager, "_write_state_off_loop", _write)]
        if self.step == "tool_event":
            fire = manager._fire_event

            async def _fire(etype: str, *args: object, **kwargs: object) -> None:
                if etype == "subagent_tool":
                    await self.land_stop()
                await fire(etype, *args, **kwargs)  # type: ignore[arg-type]

            return [patch.object(manager, "_fire_event", _fire)]
        if self.step == "gate":
            pool = tool_permission.run_in_tool_gate_pool

            async def _pool(fn: Callable[..., object], *args: object, **kwargs: object) -> object:
                await self.land_stop()
                return await pool(fn, *args, **kwargs)  # type: ignore[arg-type]

            return [patch("kiro_crew.tool_permission.run_in_tool_gate_pool", _pool)]
        return []

    async def run(self) -> asyncio.Task:  # type: ignore[type-arg]
        manager = self.manager

        async def _kill(*_a: object, **_kw: object) -> str:
            return await _failed_kill(self.log)

        with ExitStack() as stack:
            stack.enter_context(patch("kiro_crew.subagent.Stats"))
            self.sel = stack.enter_context(patch("kiro_crew.subagent.sel"))
            stack.enter_context(patch("kiro_crew.subagent.update_state"))
            stack.enter_context(patch("kiro_crew.subagent.create_agent_folder", MagicMock()))
            stack.enter_context(patch("kiro_crew.subagent._RESET_TIMEOUT", _SETTLE_SECS))
            stack.enter_context(
                patch.object(manager, "_sessions_under", MagicMock(return_value=[]))
            )
            stack.enter_context(patch.object(manager, "_sigkill_sessions", _kill))
            # The gate's hit stands on its own verdict, whatever programs this
            # host resolves.
            stack.enter_context(
                patch("kiro_crew.name_grant.refusal_for_event", AsyncMock(return_value=None))
            )
            for step_patch in self._step_patches():
                stack.enter_context(step_patch)
            task = asyncio.ensure_future(manager._run_inner(self.info, f"subagent:{AGENT_ID}"))
            manager._tasks[AGENT_ID] = task
            await async_wait_until(
                lambda: self.backend.answered(B_ID) or task.done(),
                timeout=_SETTLE_SECS,
                describe=lambda: f"B was never answered: {self.log}",
            )
            # The run goes on with the stop in progress; only then does the
            # reset fail, and its kill fallback after it.
            await asyncio.wait({task}, timeout=_SETTLE_SECS)
            self.release.set()
            if self.stopping is not None:
                await asyncio.wait({self.stopping}, timeout=_SETTLE_SECS)
        assert task.done(), f"the run did not finish: {self.log}"
        return task


_RACES = [
    (route, step)
    for route in ("auto", "gate", "person")
    for step in ("stream", "diagnostics", "tool_event", "gate")
] + [("person", "approver")]


@pytest.mark.asyncio
@pytest.mark.parametrize(("route", "step"), _RACES, ids=[f"{r}-{s}" for r, s in _RACES])
async def test_a_stop_landing_before_the_answer_bails_the_request(route, step):
    """GPT's race: the stop lands after the run's first read of it, in an await
    before B is answered. Red before the fix: B was approved after the Stop.

    The run reads the stop at the request, the ladder at its entry, before it
    asks a person and at the commit point of an approval, so B is bailed as
    ``stopped`` whichever await the stop landed in and whatever would have
    approved it, and the run ends there: C is never read.
    """
    harness = _RaceHarness(route, step)
    task = await harness.run()
    log = harness.log

    assert task.exception() is None
    assert "STOP" in log, f"the stop never landed in {step}: {log}"
    assert f"approve_tool:{B_ID}" not in log, log
    assert f"reject_tool:{B_ID}" in log, "the backend is told no"
    (row,) = _audit_rows(harness.sel, B_ID)
    assert (row["outcome"], row["error"]) == ("denied", "stopped"), "audited as a stop"
    assert f"backend_sends:{C_ID}" not in log, "the run ends at B"
    if step != "approver":
        assert f"asked:{B_ID}" not in log, "no person is asked for a stopped run"
    if step in ("stream", "diagnostics", "tool_event"):
        assert harness.ctx.hooks.on_tool_call.call_count == 0, "no gate runs for a stopped run"
