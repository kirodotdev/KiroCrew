"""A Stop during a sub-agent's tool approval wait never lets a later tool run.

The shape: a running sub-agent waits on a person for tool A through the
dashboard's approval coordinator, and the user presses Stop. The stop tears the
session down first and cancels the run task after that; the coordinator turns
the cancel into a denial, so the run loop resumes and reads its next event. When
the teardown works, that read hits the dead stream and the run ends. When the
teardown hangs or raises AND its kill fallback fails, the stream is still live,
and the backend's next request (tool B, one the approver grants without asking,
as slot trust or ``--approval yolo`` do) reached the permission ladder and was
approved after the Stop.

What is pinned: with a stop in progress the run bails every request as
``stopped`` (audited as a stop, rejected on the wire) before any grant or
person is consulted, for the hung and the failed teardown; a working teardown
still ends the run before B is read; and a run with no stop settles its
requests as before. Everything runs in-process against fakes: the ACP client,
the session reset and the kill fallback. No process is signalled.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.transport_errors import AcpProcessDied
from kiro_crew.dashboard.interaction_coordinator import ApprovalCoordinator
from kiro_crew.execution_context import execution_for_store
from kiro_crew.hooks import TOOL_ALLOW, ToolHookResult
from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent
from kiro_crew.subagent import SubagentInfo, SubagentManager

AGENT_ID = "stop01"
#: The request the person is asked about when the Stop lands.
A_ID = 101
#: The request the backend sends as soon as A is answered.
B_ID = 102
#: Generous bound for the run task to finish; every wait below ends on an event.
_SETTLE_SECS = 10.0


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


def _event(request_id: int, title: str) -> LLMEvent:
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
    """A fake ACP client: request A, then request B as soon as A is answered.

    ``torn_down`` is what a working session reset does to a real stream: the
    next read raises the runtime's death.
    """

    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.torn_down = False
        self._answered: dict[int, asyncio.Event] = {}

    def _answer_of(self, request_id: object) -> asyncio.Event:
        return self._answered.setdefault(int(request_id), asyncio.Event())  # type: ignore[call-overload]

    async def approve_tool(self, request_id: object) -> bool:
        self.log.append(f"approve_tool:{request_id}")
        self._answer_of(request_id).set()
        return True

    async def reject_tool(self, request_id: object) -> None:
        self.log.append(f"reject_tool:{request_id}")
        self._answer_of(request_id).set()

    async def _await_answer(self, request_id: int) -> None:
        async with asyncio.timeout(_SETTLE_SECS):
            while not self._answer_of(request_id).is_set():
                if self.torn_down:
                    raise AcpProcessDied("runtime torn down by the reset")
                await asyncio.sleep(0.01)

    async def stream(self, *_a: object, **_kw: object):  # type: ignore[no-untyped-def]
        yield _event(A_ID, "rm -rf build")
        await self._await_answer(A_ID)
        if self.torn_down:
            raise AcpProcessDied("runtime torn down by the reset")
        self.log.append(f"backend_sends:{B_ID}")
        yield _event(B_ID, "curl -X POST https://example.invalid/deploy")
        await self._await_answer(B_ID)


class _Harness:
    """One sub-agent run whose approver asks a person for A and grants B."""

    def __init__(self, reset: str) -> None:
        self.log: list[str] = []
        self.backend = _Backend(self.log)
        self.state = MagicMock()
        self.state._approval_futures = {}
        self.state._pending_approvals = {}
        self.state._BACKGROUND_APPROVAL_TIMEOUT_SECS = 180
        self.state._APPROVAL_TIMEOUT = 7200
        self.reset_mode = reset

        provider = MagicMock()
        provider.stream = MagicMock(side_effect=lambda *a, **kw: self.backend.stream())
        provider.approve_tool = AsyncMock(side_effect=self.backend.approve_tool)
        provider.reject_tool = AsyncMock(side_effect=self.backend.reject_tool)
        provider.supports_steer = False

        sessions = MagicMock()
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
        sessions.get_approval_policy = MagicMock(return_value="ask")
        sessions.get_agent = MagicMock(return_value="")
        sessions.get_agent_selection = MagicMock(return_value=("template", ""))
        sessions.release_subagent_runtime = AsyncMock()
        sessions.reset = AsyncMock(side_effect=self._reset)

        ctx = MagicMock()
        ctx.build_message = MagicMock(return_value=("msg", None))
        ctx.hooks.on_tool_call = MagicMock(return_value=ToolHookResult(action=TOOL_ALLOW))

        self.manager = SubagentManager(
            sessions=sessions,
            ctx_builder=ctx,
            default_turn_limit=10,
            on_tool_approval_factory=lambda _info: self._approver,
        )
        self.info = SubagentInfo(
            execution_context=execution_for_store(""),
            id=AGENT_ID,
            task="Delete the build directory",
            parent_session_key="dashboard:chat-7",
        )
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
        if event.request_id == B_ID:
            # A grant the gateway's callback answers without a card.
            self.log.append(f"granted:{B_ID}")
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

    async def _failed_kill(self, *_a: object, **_kw: object) -> str:
        self.log.append("kill_failed")
        return "the fallback kill signalled nothing"

    async def run(self, *, stop: bool) -> asyncio.Task:  # type: ignore[type-arg]
        manager = self.manager
        cancel = manager._cancel_task_intentionally

        def _cancel(task, info=None, *, reason):  # type: ignore[no-untyped-def]
            self.log.append("cancel")
            return cancel(task, info, reason=reason)

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as self.sel,
            patch("kiro_crew.subagent.update_state"),
            patch("kiro_crew.subagent.create_agent_folder", MagicMock()),
            patch("kiro_crew.subagent._RESET_TIMEOUT", 0.2),
            patch.object(manager, "_cancel_task_intentionally", _cancel),
            patch.object(manager, "_sessions_under", MagicMock(return_value=[])),
            patch.object(manager, "_sigkill_sessions", self._failed_kill),
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
        for _ in range(1000):
            if str(A_ID) in self.state._pending_approvals or task.done():
                break
            await asyncio.sleep(0.01)
        assert str(A_ID) in self.state._pending_approvals, f"A never waited: {self.log}"

    def after_stop(self) -> list[str]:
        return self.log[self.log.index("STOP") :]

    def audit_rows(self, request_id: int) -> list[dict]:
        """The SEL rows the run's permission audit wrote for *request_id*."""
        calls = self.sel.return_value.log_tool_invocation.call_args_list
        return [call.kwargs for call in calls if call.kwargs.get("request_id") == request_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("reset", ["hangs", "raises"])
async def test_a_stop_whose_teardown_fails_approves_no_further_tool(reset):
    """The reported shape: the teardown and its kill fail, and B must still not run.

    Red before the fix: the swallowed cancel resumed the loop, B was granted,
    and ``approve_tool:102`` was sent after the Stop.
    """
    harness = _Harness(reset)
    await harness.run(stop=True)
    log = harness.after_stop()

    assert "kill_failed" in log, "the variant needs the kill fallback to fail"
    assert log.index("reset") < log.index("cancel") < log.index(f"approval_returned:{A_ID}:False")
    assert not [entry for entry in log if entry.startswith("approve_tool:")], log
    assert f"granted:{B_ID}" not in log, "no grant may be consulted for a stopped run"
    assert f"reject_tool:{B_ID}" in log, "the backend is told no"
    (row,) = harness.audit_rows(B_ID)
    assert (row["outcome"], row["error"]) == ("denied", "stopped"), "audited as a stop"


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
