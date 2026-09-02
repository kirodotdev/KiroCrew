"""Regression tests for issue #6484 — a subagent parked on an unanswered
spawn approval must say so.

A default install has no YOLO override, no ``auto_approve_subagent_spawn``
and no session trust, so every ``spawn_run`` is gated behind the interactive
approval callback. While that prompt is unanswered the run is registered in
``_agents`` and counted by ``count``, so it is reported exactly like an agent
that is actually executing:

  * ``subagents`` (status API) counts it, ``subagents_spawned`` does not
  * no child ACP process exists
  * nothing in the run's state, the ``/api/spawn`` payload or the log names
    the approval gate as the reason

That combination is the whole content of the bug report: the reporter's only
lead was that no log line and no field mentioned the run. These tests pin the
observable state so the parked run is distinguishable from a running one.

Two adjacent halves of the report have since landed separately on main and are
NOT re-tested here: the reap of such a run no longer blames an execution
deadline it never reached (#7325, which is what put ``_awaiting_approval`` on
the spawn gate in the first place), and a chat tab no longer renders an owned
parked run as executing (#7477, which derives its cue from the WS ``approval``
event, not from this payload). What is left, and what this file covers, is
every reader that goes through ``/api/spawn`` — the two HTTP shapes, the CLI's
``spawn list`` and blocking poll, and MCP ``spawn_list`` — including the
unowned CLI spawn that reaches no chat tab at all.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.subagent import SubagentManager

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


def _mock_sessions() -> MagicMock:
    """Minimal SessionManager double: no trust, no live provider stream."""
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    provider = AsyncMock()
    provider.start = AsyncMock()
    provider.shutdown = AsyncMock()
    provider.context_usage_pct = lambda: 0.0

    async def _empty_stream(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        return
        yield  # noqa: unreachable — makes this an async generator

    provider.stream = MagicMock(side_effect=lambda *a, **kw: _empty_stream())
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    sessions.record_success = MagicMock()
    sessions.get_agent = MagicMock(return_value="")
    # NOT "auto": a default install has no session trust, so the spawn falls
    # through to the interactive approval callback.
    sessions.get_approval_policy = MagicMock(return_value="ask")
    return sessions


def _mock_ctx_builder() -> MagicMock:
    """ContextBuilder double with the default hooks posture."""
    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("built_message", None))
    ctx.hooks.on_tool_call = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = False
    return ctx


class _ParkedApproval:
    """Spawn-approval callback that parks until the test releases it.

    Models the reported environment: the prompt is raised on a surface nobody
    is watching (an unowned CLI spawn carries ``slot=""`` and is surfaced only
    on the global approvals feed), so it is never answered.
    """

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.calls: list[tuple[str, str, str]] = []

    async def __call__(
        self, request_id: str, description: str, parent_session_key: str = ""
    ) -> bool:
        self.calls.append((request_id, description, parent_session_key))
        await self.gate.wait()
        return True


def _manager(approval: _ParkedApproval, **kw: object) -> SubagentManager:
    return SubagentManager(
        sessions=_mock_sessions(),
        ctx_builder=_mock_ctx_builder(),
        on_spawn_approval=approval,
        is_yolo=lambda: False,
        **kw,  # type: ignore[arg-type]
    )


async def _park(
    mgr: SubagentManager, task: str = "Return only the result of 1+1", parent: str = ""
):
    """Spawn and let the approval task reach its await."""
    info = mgr.spawn(task, parent_session_key=parent)
    assert info is not None
    for _ in range(20):
        await asyncio.sleep(0)
    return info


async def _drain(mgr: SubagentManager, approval: _ParkedApproval) -> None:
    approval.gate.set()
    for t in list(mgr._tasks.values()):
        t.cancel()
    await asyncio.sleep(0)


@asynccontextmanager
async def _parked(parent: str = ""):
    """Yield ``(mgr, info, approval)`` for a run parked on its spawn prompt.

    The drain is in a ``finally`` on purpose: a failing assertion must not
    leave the approval coroutine suspended on its event, which surfaces as
    "Task was destroyed but it is pending!" and attributes the noise to
    whichever test runs next.
    """
    approval = _ParkedApproval()
    mgr = _manager(approval)
    try:
        info = await _park(mgr, parent=parent)
        yield mgr, info, approval
    finally:
        await _drain(mgr, approval)


class TestSpawnParkedOnApprovalIsObservable:
    """The parked run must be distinguishable from one that is executing."""

    @pytest.mark.asyncio
    async def test_parked_run_is_marked_awaiting_approval(self) -> None:
        """``_awaiting_approval`` is set while the spawn prompt is unanswered.

        This is a PRECONDITION pin, not new behaviour: the spawn gate began
        setting the flag in #7325, for the reaper's terminal message. The wire
        predicate added by this change reads that same flag, and every payload
        test below feeds the predicate a hand-built ``SubagentInfo`` — so if the
        gate stopped setting it, those tests would still pass while the field
        went dark in production. Nothing else covers the seam, because #7325's
        own tests assert on the reap message rather than on the flag.

        The other assertions record the reported state itself: a run that is
        counted like an executing one while owning no process and no turn.
        """
        async with _parked() as (mgr, info, approval):
            assert approval.calls, "the interactive spawn prompt must have been raised"
            registered = mgr._agents[info.id]
            # Preconditions: this is the reported state, not an executing agent.
            assert registered.done is False
            assert registered.turns == 0
            assert registered._pid is None
            assert registered._exec_started is None
            assert mgr.count == 1, "status API counts it exactly like a running agent"

            assert registered._awaiting_approval is True

    @pytest.mark.asyncio
    async def test_parked_run_logs_under_its_run_id(self, caplog) -> None:  # type: ignore[no-untyped-def]
        """An operator grepping the logs for a stuck run id finds the reason.

        The report's dead-end was "``kirocrew logs`` contained no error or
        warning keyed by the affected run ID". #7325 later marked this wait in
        machine state, for the reaper — but a mark is not a message, and still
        nothing was WRITTEN when a spawn parked, so the single most useful
        diagnostic (which run is waiting, and for what) did not exist. This is
        the only assertion in this file that covers a change to
        ``admission.py``; everything else covers a reader of the state.
        """
        approval = _ParkedApproval()
        mgr = _manager(approval)
        # Captured at root, not at a named logger: the coordinator modules
        # receive ``logger`` by injection (``bind_component_globals``), so the
        # record is emitted under ``kiro_crew.subagent`` rather than under
        # ``admission``'s own module name. What matters to the operator is that
        # SOME record names the run and the gate.
        try:
            with caplog.at_level(logging.INFO):
                info = await _park(mgr)
                keyed = [r for r in caplog.records if info.id in r.getMessage()]
                assert keyed, f"no log record mentions run id {info.id}"
                assert any(
                    "approval" in r.getMessage().lower() for r in keyed
                ), f"no log record names the approval gate: {[r.getMessage() for r in keyed]}"
        finally:
            await _drain(mgr, approval)

    @pytest.mark.asyncio
    async def test_approval_flag_clears_once_answered(self) -> None:
        """The flag is a wait marker, not a sticky one: it clears on answer.

        The second half of the precondition above. The wire predicate reports a
        wait purely from state, so a flag left set after the answer would
        advertise "waiting for your approval" on a run that is executing.
        """
        async with _parked() as (mgr, info, approval):
            assert mgr._agents[info.id]._awaiting_approval is True

            approval.gate.set()
            for _ in range(20):
                await asyncio.sleep(0)
            assert mgr._agents[info.id]._awaiting_approval is False


class TestParkedRunIsVisibleOnBothReadPaths:
    """Both /api/spawn shapes must carry the wait, not just the list one.

    A blocking ``kirocrew spawn run`` polls the SINGLE-run status endpoint
    (``/api/spawn/<id>``) every 2s, not the list. Reporting the wait only on the
    list left the CLI reproduction of #6484 exactly as silent as before: the
    caller sat on "waiting for result..." while the reason was discoverable only
    from a separate ``spawn list`` or a log grep.
    """

    def _payload_flag(self, *, awaiting: bool, exec_started: float | None) -> bool:
        """What the two handlers now emit, via their shared predicate."""
        from kiro_crew.dashboard.handlers.messaging import _awaiting_spawn_approval
        from kiro_crew.subagent import SubagentInfo

        info = SubagentInfo(id="parked01", task="Return only the result of 1+1")
        info._awaiting_approval = awaiting
        info._exec_started = exec_started
        return _awaiting_spawn_approval(info)

    def test_field_present_only_while_parked_on_the_spawn_gate(self) -> None:
        assert self._payload_flag(awaiting=True, exec_started=None) is True
        # Not parked at all -> absent, so the payload of an ordinary executing
        # run is unchanged.
        assert self._payload_flag(awaiting=False, exec_started=None) is False

    def test_mid_run_tool_approval_is_not_reported_as_the_spawn_gate(self) -> None:
        """The flag is shared; the wire read must not be.

        ``run.py`` sets ``_awaiting_approval`` at three in-run TOOL-approval
        sites, and ``_exec_started`` is stamped once when execution begins (in
        ``_run_inner_impl``). Reading the flag bare would render a run at turn 5
        waiting on a tool prompt as "waiting for spawn approval" and tell a
        still-polling caller to approve it "to start this run" that already
        started. ``_exec_started is None`` is what separates the two — the same
        pair ``terminal.py`` uses to pick the reap message.
        """
        assert self._payload_flag(awaiting=True, exec_started=1.0) is False

    def test_both_endpoints_use_the_shared_predicate(self) -> None:
        """Source ratchet: neither read path may inline its own condition.

        A behavioural test cannot cover this -- the two handlers build their own
        dicts independently, so one of them dropping the field, or drifting to a
        bare flag read, looks identical to a run that simply is not parked.
        """
        from pathlib import Path

        import kiro_crew.dashboard.handlers.messaging as messaging

        src = Path(messaging.__file__).read_text(encoding="utf-8")
        assert src.count("if _awaiting_spawn_approval(info):") == 2, (
            "expected both api_spawn_status and api_spawn_list to gate on the "
            "shared _awaiting_spawn_approval predicate"
        )
        assert src.count('"awaiting_approval"] = True') == 2
        # No bare flag read may creep back into a payload builder: that is the
        # exact drift that mislabels an in-run tool approval.
        assert 'getattr(info, "_awaiting_approval", False) is True' not in src.replace(
            'getattr(info, "_awaiting_approval", False) is True\n        and getattr(info, "_exec_started", None) is None',
            "<predicate>",
        )

    def test_cli_poll_announces_the_wait_once(self) -> None:
        """The blocking CLI must tell the user, and only once per run."""
        from pathlib import Path

        import kiro_crew.cli_commands as cli

        src = Path(cli.__file__).read_text(encoding="utf-8")
        assert 'status.get("awaiting_approval")' in src, (
            "the blocking spawn-run poll does not consult awaiting_approval, so "
            "a parked run still reports nothing to the waiting caller"
        )
        # Guarded by a one-shot flag rather than printing on every 2s poll.
        assert "told_awaiting" in src

    def test_mcp_spawn_list_does_not_call_a_parked_run_running(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """The MCP roster is the 4th read surface, and an LLM's one.

        ``spawn.py`` itself tells a caller whose spawn POST failed to "Check
        spawn_list", so answering ``[running]`` for a run that has launched
        nothing and is waiting on a human misinforms the agent at exactly the
        moment it is trying to reconcile.
        """
        from kiro_crew.mcp_tools import spawn as spawn_tools

        def _fake_get(path: str) -> dict:
            assert path == "/api/spawn"
            return {
                "agents": [
                    {
                        "id": "parked01",
                        "task": "Return only the result of 1+1",
                        "done": False,
                        "awaiting_approval": True,
                        "turns": 0,
                        "elapsed": 12,
                    }
                ]
            }

        monkeypatch.setattr(spawn_tools.mcp_core, "_get", _fake_get)
        out = spawn_tools.spawn_list("spawn_list", {})
        assert "awaiting-approval" in out, f"parked run not reported as waiting: {out!r}"
        assert "[running]" not in out, f"parked run still reported as running: {out!r}"

    def test_mcp_spawn_list_still_says_running_for_a_live_run(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """The new status must not swallow the ordinary one."""
        from kiro_crew.mcp_tools import spawn as spawn_tools

        monkeypatch.setattr(
            spawn_tools.mcp_core,
            "_get",
            lambda _p: {
                "agents": [
                    {"id": "live0001", "task": "real work", "done": False, "turns": 3, "elapsed": 9}
                ]
            },
        )
        out = spawn_tools.spawn_list("spawn_list", {})
        assert "[running]" in out
        assert "awaiting-approval" not in out


# NOTE on the two halves of #6484 that are NOT covered here, because main
# already carries them.
#
# The terminal message ("Reaped after Ns (exceeded Ns deadline)" on a run that
# never executed) was fixed by #7325, which reads
# `_awaiting_approval and _exec_started is None` in `subagent_manager/
# terminal.py` — the same pair as the wire predicate above, arrived at
# independently. That is also where the spawn gate's own
# `info._awaiting_approval = True` comes from, so this change no longer needs to
# set the flag; it names the wait (the log line) and reports it (the field).
#
# The chat-tab rendering was fixed by #7477, which is frontend-only: it derives
# its cue from the WS `approval` event routed into `sseSubagentPending`, i.e.
# `status === 'pending' && approval_id`, NOT from this payload. It is therefore
# scoped to a slot, and an unowned spawn (`slot=""`) still reaches no chat tab —
# which is precisely the run the CLI and MCP surfaces above are for.
