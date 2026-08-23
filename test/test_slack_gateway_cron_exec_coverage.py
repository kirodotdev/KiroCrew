"""Coverage for the Kiro Crew gateway's deterministic cron-execution paths.

``test_slack_gateway.py`` and ``test_cron_gateway_integration.py`` drive the
LLM (``message``) arm of ``_cron_callback`` thoroughly, but the two
deterministic arms — ``job.command`` (shell) and ``job.script`` (Python
callable) — only had their happy paths exercised. Everything reached here was
uncovered by the whole suite before this file:

* the shared concurrent-execution guard (``_running_script_ids``);
* command mode: fire-time governance denial, ``cancelled`` status, the
  empty-output ok/non-ok split, non-ok-with-output, timeout and generic-error
  arms, plus the best-effort SEL-audit ``except`` around each;
* script mode: governance denial, ``cancelled`` / ``ok`` / ``skip`` / ``done``
  / ``report`` / unknown-status dispositions, the auto-paused warning arms of
  timeout and generic error, and their SEL-audit ``except`` twins;
* the ``message``-arm fire-time denial audit ``except``;
* ``_deliver_script_result``'s queued-slot, rehydrate-miss, no-session-key,
  delivery-failure and ``CronStoreBusy`` deferred-removal branches;
* the "cron reaper not started" arm of ``_init_cron``;
* ``_channel_reply_link`` / ``_deliver_channel_reply`` resolution refusals;
* ``_persist_turn_row``'s best-effort ``except`` and ``_is_read_only_tool``'s
  no-token guard.

Everything is driven through mocked collaborators: the sandbox runners, the
governance gate, SEL and the cron service are all patched, so no subprocess, no
socket and no write outside the per-test ``KIROCREW_HOME`` (pinned by
``test/conftest.py``) happens. Style and patch seams mirror
``test_slack_gateway.py`` / ``test_cron_gateway_integration.py``.
"""

from __future__ import annotations

import ast
import asyncio
import threading
import time
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.cron import CronJob, CronStoreBusy
from kiro_crew.slack import gateway as gw

# ─── Helpers ─────────────────────────────────────────────────────────────


def _make_orchestrator(**kwargs: Any) -> Any:
    """Build a GatewayOrchestrator with mocked credentials (no Slack tokens).

    Returned as ``Any`` on purpose: every test below swaps real collaborators
    for mocks, which do not satisfy the declared attribute types.
    """
    cfg = KiroCrewConfig()
    creds = {"KIROCREW_OWNER_ID": "U_OWNER"}
    with patch.object(cfg, "load_credentials", return_value=creds):
        return gw.GatewayOrchestrator(
            cfg,
            no_dashboard=kwargs.pop("no_dashboard", True),
            no_crons=kwargs.pop("no_crons", True),
            no_open=True,
        )


def _mock_dashboard_state() -> MagicMock:
    ds = MagicMock()
    ds._slots = {}
    ds.notify = MagicMock()
    ds.push_slots_update = MagicMock()
    ds.push_refresh = MagicMock()
    ds.get_slot = MagicMock(return_value=None)
    ds.has_slot = MagicMock(return_value=False)
    ds.conversation_log = None
    return ds


def _mock_slot(*, running: bool = False) -> MagicMock:
    slot = MagicMock()
    slot.running = running
    slot.queue_append = MagicMock(return_value="q-1")
    slot.append = MagicMock()
    slot.task = None
    return slot


def _job(**kwargs: Any) -> CronJob:
    """A real CronJob, so record_success / record_failure semantics are real."""
    job = CronJob(
        id=kwargs.pop("id", "j1"),
        name=kwargs.pop("name", "nightly probe"),
        message=kwargs.pop("message", "go"),
    )
    for key, value in kwargs.items():
        setattr(job, key, value)
    return job


def _cron_service_double() -> MagicMock:
    svc = MagicMock()
    svc.start = AsyncMock()
    svc.start_reaper = MagicMock()
    svc.remove_job_async = AsyncMock(return_value=True)
    svc.defer_removal = MagicMock()
    svc.set_refresh_callback = MagicMock()
    svc.register_active_session_key = MagicMock()
    svc.clear_active_session_key = MagicMock()
    svc.get_job = MagicMock(return_value=None)
    return svc


def _blind_sel() -> MagicMock:
    """A SEL double whose audit write always fails (drives the best-effort arms)."""
    sel_obj = MagicMock()
    sel_obj.log_tool_invocation = MagicMock(side_effect=RuntimeError("sel down"))
    return sel_obj


def _sandboxed(result: Any) -> Any:
    """A sandbox-runner double: returns *result*, or raises it when it is an error.

    The real runners execute inside ``run_in_executor``, so raising here
    propagates through ``asyncio.wait_for`` exactly as a real failure does —
    which is what lets the timeout arm be driven without a real wall-clock wait.
    """

    def _run(*_args: Any, **_kwargs: Any) -> Any:
        if isinstance(result, BaseException):
            raise result
        return result

    return _run


@asynccontextmanager
async def _cron_cb(
    orch: Any,
    *,
    svc: MagicMock | None = None,
    gate_reason: Any = "",
    sel_obj: MagicMock | None = None,
    command_result: Any = None,
    script_result: Any = None,
    queue_hook: Any = None,
) -> AsyncIterator[Any]:
    """Run ``_init_cron`` with every collaborator patched and yield ``on_job``.

    The callback resolves ``sel`` / ``vet_job_at_fire_time`` / the sandbox
    runners from module globals at CALL time, so it must be invoked while these
    patches are still active — hence a context manager rather than a plain
    factory.

    ``gate_reason`` may be a plain string (a fixed verdict, the common case) or
    a ``job -> str`` callable, for the cases where the verdict must CHANGE
    between the fire-time gate and a later re-vet — a script body edited on
    disk, or a policy tightened, while the call sat in the pool queue.

    ``queue_hook`` models that queue wait. When given, the execution dispatch
    (``run_in_cron_pool``) is replaced by a double that runs the hook and only
    THEN invokes whatever callable production code submitted. That is the
    window the queue wait opens: it is deliberately not charged to the job's
    deadline, so the world can change inside it. Note it patches only the
    EXECUTION dispatch — the fire-time gate goes through
    ``run_in_cron_gate_pool`` and still runs for real, keeping the two seams
    distinct.
    """
    service = svc if svc is not None else _cron_service_double()
    captured: dict[str, Any] = {}

    async def _create(**kw: Any) -> MagicMock:
        captured["on_job"] = kw["on_job"]
        return service

    _vet = gate_reason if callable(gate_reason) else (lambda job: gate_reason)

    async def _queued_pool(fn: Any, *args: Any, **_kwargs: Any) -> Any:
        queue_hook()
        return fn(*args)

    _sel = sel_obj if sel_obj is not None else MagicMock()
    with ExitStack() as stack:
        for patcher in (
            patch.object(gw.CronService, "create", AsyncMock(side_effect=_create)),
            # No executor patch: the fire-time gate no longer resolves a pool from
            # this module -- it goes through run_in_cron_gate_pool, which owns its
            # own bounded pool. `vet_job_at_fire_time` is still patched below, so the
            # gate submits a trivial callable and returns immediately.
            patch.object(gw, "vet_job_at_fire_time", _vet),
            patch.object(gw, "sel", lambda: _sel),
            patch.object(gw, "run_command_sandboxed", _sandboxed(command_result)),
            patch.object(gw, "run_script_sandboxed", _sandboxed(script_result)),
            patch.object(
                gw, "build_cron_session_context", lambda job: (f"cron:{job.id}", job.message)
            ),
            patch("kiro_crew.apps.bridges.reconcile_app_crons_for_execution", AsyncMock()),
        ):
            stack.enter_context(patcher)
        if queue_hook is not None:
            stack.enter_context(patch.object(gw, "run_in_cron_pool", _queued_pool))
        await orch._init_cron()
        assert "on_job" in captured
        yield captured["on_job"]


def _channel_sessions(
    *,
    origin: Any = None,
    mirror: Any = None,
    stored: Any = "",
    origin_raises: bool = False,
    stored_raises: bool = False,
) -> MagicMock:
    """A SessionManager double exposing only the link-resolution surface."""
    sessions = MagicMock()
    if origin_raises:
        sessions.get_origin_link = MagicMock(side_effect=RuntimeError("map wedged"))
    else:
        sessions.get_origin_link = MagicMock(return_value=origin)
    sessions.get_mirror_link = MagicMock(return_value=mirror)
    if stored_raises:
        sessions.get_channel = MagicMock(side_effect=RuntimeError("map wedged"))
    else:
        sessions.get_channel = MagicMock(return_value=stored)
    return sessions


# ═══════════════════════════════════════════════════════════════════════════
# Module-level helpers
# ═══════════════════════════════════════════════════════════════════════════


class TestModuleHelpers:
    """``_persist_turn_row`` and ``_is_read_only_tool`` guard arms."""

    @pytest.mark.asyncio
    async def test_persist_turn_row_swallows_failure(self):
        """A usage-row persistence failure never propagates into the caller."""
        with patch.object(gw, "read_context_tokens", side_effect=RuntimeError("no provider")):
            await gw._persist_turn_row(
                object(),
                "_hb",
                provider="anthropic",
                surface="heartbeat",
                agent_fallback=lambda: "kirocrew",
                t0=0.0,
            )

    def test_read_only_tool_rejects_punctuation_only_title(self):
        """A title that tokenizes to nothing fails closed rather than approving."""
        assert gw._is_read_only_tool("___") is False
        assert gw._is_read_only_tool("read") is True


# ═══════════════════════════════════════════════════════════════════════════
# Command-mode cron execution
# ═══════════════════════════════════════════════════════════════════════════


class TestCronCommandMode:
    """``_cron_callback``'s ``job.command`` arm."""

    @pytest.mark.asyncio
    async def test_concurrent_run_is_skipped(self):
        """A job still running is skipped rather than started twice."""
        orch = _make_orchestrator()
        job = _job(command="echo hi")
        orch._running_script_ids.add(job.id)
        async with _cron_cb(orch, command_result={"status": "ok", "output": "hi"}) as cb:
            assert await cb(job) is None
        # The guard must not consume the marker — the in-flight run owns it.
        assert job.id in orch._running_script_ids

    @pytest.mark.asyncio
    async def test_fire_time_denial_keeps_job_and_audits(self):
        """Governance denial marks the run failed without counting a failure."""
        orch = _make_orchestrator()
        job = _job(command="printf hi")
        async with _cron_cb(
            orch, gate_reason="capabilities.cron denied", sel_obj=_blind_sel()
        ) as cb:
            assert await cb(job) is None
        assert job.last_status == "error"
        assert job.fire_time_denied is True
        # A policy denial must never feed the auto-pause counter.
        assert job.consecutive_failures == 0
        assert job.id not in orch._running_script_ids

    @pytest.mark.asyncio
    async def test_cancelled_status_is_not_a_failure(self):
        """A user cancel leaves the bookkeeping to CronService.cancel()."""
        orch = _make_orchestrator()
        job = _job(command="sleep 1")
        async with _cron_cb(orch, command_result={"status": "cancelled"}) as cb:
            assert await cb(job) is None
        assert job.consecutive_failures == 0
        assert job.last_status is None

    @pytest.mark.asyncio
    async def test_empty_output_ok_records_success(self):
        """No output on an ok status is a success with nothing to deliver."""
        orch = _make_orchestrator()
        job = _job(command="true", consecutive_failures=2)
        async with _cron_cb(orch, command_result={"status": "ok", "output": "   "}) as cb:
            assert await cb(job) is None
        assert job.last_status == "ok"
        assert job.consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_empty_output_non_ok_records_failure(self):
        """No output on a non-ok status is a failure, and it says why."""
        orch = _make_orchestrator()
        job = _job(command="false")
        async with _cron_cb(orch, command_result={"status": "error", "output": ""}) as cb:
            assert await cb(job) is None
        assert job.last_status == "error"
        assert "non-ok status with no output" in (job.last_error or "")
        assert job.consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_non_ok_with_output_delivers_and_counts_failure(self):
        """Output is still delivered on a non-ok exit, but the run is a failure."""
        orch = _make_orchestrator()
        job = _job(command="false")
        result = {"status": "error", "output": "partial output", "exit_code": 3}
        async with _cron_cb(orch, sel_obj=_blind_sel(), command_result=result) as cb:
            assert await cb(job) == "partial output"
        assert job.last_status == "error"
        assert "exit_code=3" in (job.last_error or "")
        assert job.consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_timeout_records_failure(self):
        """A sandbox timeout is recorded against the job with its own audit arm."""
        orch = _make_orchestrator()
        job = _job(command="sleep 9999", timeout=7)
        async with _cron_cb(
            orch, sel_obj=_blind_sel(), command_result=asyncio.TimeoutError()
        ) as cb:
            assert await cb(job) is None
        assert job.last_status == "error"
        assert job.last_error == "timeout (12s)"
        assert job.consecutive_failures == 1
        assert job.id not in orch._running_script_ids

    @pytest.mark.asyncio
    async def test_unexpected_error_records_failure(self):
        """An unexpected runner error is redacted, truncated and counted."""
        orch = _make_orchestrator()
        job = _job(command="printf hi")
        async with _cron_cb(
            orch, sel_obj=_blind_sel(), command_result=RuntimeError("x" * 400)
        ) as cb:
            assert await cb(job) is None
        assert job.last_status == "error"
        assert len(job.last_error or "") <= 200
        assert job.consecutive_failures == 1


# ═══════════════════════════════════════════════════════════════════════════
# Script-mode cron execution
# ═══════════════════════════════════════════════════════════════════════════


class TestCronScriptMode:
    """``_cron_callback``'s ``job.script`` arm and its dispositions."""

    @pytest.mark.asyncio
    async def test_fire_time_denial_keeps_job(self):
        orch = _make_orchestrator()
        job = _job(script="probes.py:check")
        async with _cron_cb(
            orch, gate_reason="script body rescan denied", sel_obj=_blind_sel()
        ) as cb:
            assert await cb(job) is None
        assert job.fire_time_denied is True
        assert job.consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_cancelled_status_is_not_a_failure(self):
        orch = _make_orchestrator()
        job = _job(script="probes.py:check")
        async with _cron_cb(orch, script_result={"status": "cancelled"}) as cb:
            assert await cb(job) is None
        assert job.consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_ok_status_records_success(self):
        """An ok status records success without inventing a result string.

        The status is the signal and there is no output to show, so a carried
        result is cleared rather than replaced with an "ok" sentinel that every
        reader then had to filter out.
        """
        orch = _make_orchestrator()
        job = _job(script="probes.py:check", consecutive_failures=1, last_result="42 widgets")
        async with _cron_cb(orch, sel_obj=_blind_sel(), script_result={"status": "ok"}) as cb:
            assert await cb(job) == "ok"
        assert job.last_status == "ok"
        assert job.last_result == ""
        assert job.consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_skip_status_delivers_nothing(self):
        """A Skip is result-less, so it must not present the PREVIOUS run's output."""
        orch = _make_orchestrator()
        job = _job(script="probes.py:check", last_result="42 widgets")
        async with _cron_cb(orch, sel_obj=_blind_sel(), script_result={"status": "skip"}) as cb:
            assert await cb(job) is None
        assert job.last_result == ""

    @pytest.mark.asyncio
    async def test_unknown_status_is_an_error(self):
        """An unrecognized status raises and lands in the generic error arm."""
        orch = _make_orchestrator()
        job = _job(script="probes.py:check")
        result = {"status": "weird", "error": "bad disposition"}
        async with _cron_cb(orch, script_result=result) as cb:
            assert await cb(job) is None
        assert job.last_status == "error"
        assert job.last_error == "bad disposition"
        assert job.consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_timeout_on_already_paused_job_warns(self):
        """An auto-paused job's timeout logs the pause without re-auditing it."""
        orch = _make_orchestrator()
        job = _job(
            script="probes.py:check", timeout=3, auto_paused=True, consecutive_failures=5
        )
        async with _cron_cb(
            orch, sel_obj=_blind_sel(), script_result=asyncio.TimeoutError()
        ) as cb:
            assert await cb(job) is None
        assert job.last_error == "timeout (8s)"
        assert job.auto_paused is True
        assert job.id not in orch._running_script_ids

    @pytest.mark.asyncio
    async def test_error_on_already_paused_job_warns(self):
        orch = _make_orchestrator()
        job = _job(script="probes.py:check", auto_paused=True, consecutive_failures=5)
        async with _cron_cb(
            orch, sel_obj=_blind_sel(), script_result=RuntimeError("callable exploded")
        ) as cb:
            assert await cb(job) is None
        assert job.last_status == "error"
        assert "callable exploded" in (job.last_error or "")


# ═══════════════════════════════════════════════════════════════════════════
# Vetting must hold at the moment the worker claims the execution
# ═══════════════════════════════════════════════════════════════════════════


class TestVettingHoldsAtClaimTime:
    """The queue wait must not open a window between vetting and execution.

    The fire-time gate authorises a run, then the execution is submitted to a
    pool whose queue wait is deliberately NOT charged to the job's deadline. So
    the authorisation and the use it authorises are separated by a wait bounded
    only by ``_CRON_QUEUE_WAIT_SECS``. Two things change inside that window:

    * a script's BODY, which ``run_script_sandboxed``'s launcher re-reads from
      disk in the sandboxed child (``open`` + ``compile`` + ``exec``), so the
      bytes that execute are whatever is on disk when the worker gets there —
      not the bytes the gate scanned;
    * the governance POLICY, which applies to command jobs too even though a
      command's text is already captured in ``job.command``.

    Both are refused only if the vet holds when the worker claims the call.
    """

    @staticmethod
    def _script_on_disk(tmp_path: Any) -> Any:
        """A real file whose CURRENT content is what a vet or a run would see."""
        path = tmp_path / "probe.py"
        path.write_text("def check(ctx):\n    return 'benign'\n", encoding="utf-8")
        return path

    @staticmethod
    def _body_scanning_vet(path: Any) -> Any:
        """A ``job -> str`` vet that re-reads the body, as ``_vet_script_file`` does."""

        def _vet(_job: Any) -> str:
            if "EXFILTRATE" in path.read_text(encoding="utf-8"):
                return "Error: script body denied by policy"
            return ""

        return _vet

    @staticmethod
    def _recording_runner(path: Any, executed: list[str]) -> Any:
        """A sandbox double that reads the body from disk, as the launcher does."""

        def _run(*_args: Any, **_kwargs: Any) -> dict[str, str]:
            executed.append(path.read_text(encoding="utf-8"))
            return {"status": "ok"}

        return _run

    @pytest.mark.asyncio
    async def test_a_script_edited_during_the_queue_wait_does_not_execute(self, tmp_path):
        """The cited defect: benign at gate time, hostile by the time it runs.

        The edit lands while the call is queued, which is exactly the interval
        the gate cannot see. Asserting on what the sandbox was handed — rather
        than on a budget helper — is what makes this measure the wiring.
        """
        orch = _make_orchestrator()
        path = self._script_on_disk(tmp_path)
        executed: list[str] = []
        job = _job(script=f"{path}:check")

        def _edit_during_queue() -> None:
            path.write_text(
                "def check(ctx):\n    EXFILTRATE = open('/etc/passwd').read()\n",
                encoding="utf-8",
            )

        async with _cron_cb(
            orch,
            gate_reason=self._body_scanning_vet(path),
            queue_hook=_edit_during_queue,
        ) as cb:
            with patch.object(gw, "run_script_sandboxed", self._recording_runner(path, executed)):
                await cb(job)

        assert not any("EXFILTRATE" in body for body in executed), (
            "the script body edited during the queue wait was executed: "
            "the fire-time vet authorised a body that is no longer on disk"
        )
        assert job.fire_time_denied is True, "a refused run must be recorded as a policy denial"

    @pytest.mark.asyncio
    async def test_an_unmodified_script_still_runs(self, tmp_path):
        """Opposite direction: re-vetting must not refuse a legitimate run."""
        orch = _make_orchestrator()
        path = self._script_on_disk(tmp_path)
        executed: list[str] = []
        job = _job(script=f"{path}:check")

        async with _cron_cb(
            orch,
            gate_reason=self._body_scanning_vet(path),
            queue_hook=lambda: None,
        ) as cb:
            with patch.object(gw, "run_script_sandboxed", self._recording_runner(path, executed)):
                assert await cb(job) == "ok"

        assert len(executed) == 1, "an unmodified, vetted script must still reach the sandbox"
        assert "benign" in executed[0]
        assert job.last_status == "ok"
        assert job.fire_time_denied is False
        assert job.consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_a_claim_time_denial_does_not_feed_the_auto_pause_counter(self, tmp_path):
        """A denial is a policy state, so it must not count toward auto-pause.

        Counting it would disable the job at ``_AUTO_PAUSE_THRESHOLD`` and a
        paused job never fires again — breaking the resume-on-policy-loosening
        semantic the fire-time deny path is careful to preserve. The generic
        error arm DOES call ``record_failure``, so a claim-time denial that
        fell through to it would be silently destructive.
        """
        orch = _make_orchestrator()
        path = self._script_on_disk(tmp_path)
        job = _job(script=f"{path}:check", consecutive_failures=2, last_result="42 widgets")

        def _edit_during_queue() -> None:
            path.write_text("EXFILTRATE = 1\n", encoding="utf-8")

        async with _cron_cb(
            orch,
            gate_reason=self._body_scanning_vet(path),
            queue_hook=_edit_during_queue,
            script_result={"status": "ok"},
        ) as cb:
            assert await cb(job) is None

        assert job.consecutive_failures == 2, "a policy denial must not count as a job failure"
        assert job.fire_time_denied is True
        assert job.last_status == "error"
        assert "denied" in (job.last_error or "")
        assert job.last_result == "", "a denial is result-less: it must not carry prior output"

    @pytest.mark.asyncio
    async def test_a_policy_tightened_during_the_queue_wait_stops_a_command(self):
        """The command sibling: same shape, and policy still changes under it.

        A command's text is already captured in ``job.command``, so file
        substitution does not apply here — but a governance ceiling tightened
        while the call sat in the queue does, exactly as for a script.
        """
        orch = _make_orchestrator()
        calls: list[int] = []
        ran: list[str] = []

        def _tightening_vet(_job: Any) -> str:
            calls.append(1)
            return "" if len(calls) == 1 else "Error: command denied by policy"

        def _runner(*_args: Any, **_kwargs: Any) -> dict[str, str]:
            ran.append("ran")
            return {"status": "ok", "output": "done"}

        job = _job(command="curl example.com", consecutive_failures=1)
        async with _cron_cb(
            orch,
            gate_reason=_tightening_vet,
            queue_hook=lambda: None,
        ) as cb:
            with patch.object(gw, "run_command_sandboxed", _runner):
                assert await cb(job) is None

        assert ran == [], "a command denied at claim time must not reach the sandbox"
        assert job.fire_time_denied is True
        assert job.consecutive_failures == 1, "a policy denial must not count as a job failure"

    def test_the_message_arm_has_no_pool_queue_between_vet_and_dispatch(self):
        """Disposition for the third gate site, by measurement not assertion.

        ``gateway.py`` has exactly three fire-time gate sites and exactly two
        execution-pool dispatches. Both dispatches sit in the command/script
        blocks, which return before the message gate is reached — so the
        message arm has no queue between its vet and its LLM dispatch and the
        defect class cannot apply to it. Pinned here so a future dispatch added
        after the message gate fails this test rather than shipping unnoticed.
        """
        source = Path(gw.__file__).read_text(encoding="utf-8")
        gate_sites = source.count("await _await_cron_fire_time_gate(")
        pool_sites = source.count("await run_in_cron_pool(")
        assert gate_sites == 3, f"gate-site count changed ({gate_sites}); re-audit the sweep"
        assert pool_sites == 2, f"pool-dispatch count changed ({pool_sites}); re-audit the sweep"
        message_gate = source.index('tool_name="cron_message_dispatch"')
        assert (
            source.find("await run_in_cron_pool(", message_gate) == -1
        ), "a pool dispatch now follows the message gate: it needs claim-time vetting too"

    def test_no_text_io_here_relies_on_the_platform_default_encoding(self):
        """Every text read/write in this module must name its encoding.

        A bare ``read_text()`` takes the PLATFORM default, which is UTF-8 on the
        Linux shards and ``cp1252`` on the Windows ones.  So a module that reads
        a UTF-8 source file without saying so passes every Linux shard and fails
        only on Windows -- the defect is invisible where it is cheap to catch.
        ``gateway.py`` carries em dashes and box-drawing characters, and
        ``cp1252`` has no mapping for the continuation bytes of those sequences.

        Enforced statically rather than by driving a hostile locale: the runtime
        failure cannot be provoked in-process (``TextIOWrapper`` resolves the
        default at the C level, so patching ``locale.getpreferredencoding`` does
        not steer it), and a subprocess that sets ``LC_ALL`` would reproduce it
        only where the default is already UTF-8 -- while this check fails
        identically on every platform.  Parsed with ``ast`` so a call written
        inside a fixture's script BODY (this module has one) is not mistaken for
        a real call site.
        """
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        naive = [
            (node.lineno, getattr(node.func, "attr", None) or getattr(node.func, "id", ""))
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and (getattr(node.func, "attr", None) or getattr(node.func, "id", ""))
            in {"read_text", "write_text", "open"}
            and not any(kw.arg == "encoding" for kw in node.keywords)
        ]
        assert naive == [], (
            "text I/O without an explicit encoding, which passes on Linux and "
            f"fails on the Windows shards: {naive}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# An abandoned claim-time vet must not license a later execution
# ═══════════════════════════════════════════════════════════════════════════


class TestAnAbandonedVetDoesNotExecute:
    """A deadline that lands mid-vet must not leave a payload free to start.

    ``run_in_cron_pool`` only reaches its execution phase once a worker has
    CLAIMED the call, and a thread cannot be interrupted -- so when that phase
    times out the submitted callable keeps running. Before the claim-time vet
    existed the claimed thread was already inside the sandbox, whose own
    ``timeout`` bounds it. Now the vet runs FIRST, so the deadline can land
    while the payload has not started, and it would otherwise start after the
    caller's ``finally`` released the overlap guard -- running alongside the
    next fire. ``run_in_cron_pool``'s own docstring names that harm for the
    queue phase; these pin it closed for a deadline landing mid-vet.

    ``run_in_cron_pool`` is doubled here on purpose: its two-phase behaviour is
    pinned in ``test_executors.py``, and what needs exercising is what the
    GATEWAY does when the execution phase times out with the callable still in
    flight. Everything under test -- the wrapper, the hand-off, the ``finally``
    -- is the real thing.
    """

    @staticmethod
    def _abandoning_pool(started: threading.Event, done: list[str]):
        """A pool double whose execution phase times out with the call in flight.

        Models the one reachable shape: a worker HAS claimed the call (the
        execution phase is reached in no other case), so the thread runs on
        after the awaiter gives up.
        """

        async def _pool(fn: Any, *args: Any, **_kwargs: Any) -> Any:
            def _worker() -> None:
                try:
                    fn(*args)
                    done.append("dispatched")
                except gw.CronClaimAbandoned:
                    done.append("refused")
                except BaseException as exc:  # noqa: BLE001 - recorded, not handled
                    done.append(f"raised:{type(exc).__name__}")

            thread = threading.Thread(target=_worker, daemon=True)
            thread.start()
            assert started.wait(timeout=5), "the claim-time vet never began"
            raise asyncio.TimeoutError

        return _pool

    @staticmethod
    def _slow_vet(started: threading.Event, release: threading.Event):
        """Fast at fire time, still running at claim time, and then ALLOWS.

        One patched name serves both gates, so the FIRE-TIME call must return
        at once -- blocking there would starve the gate instead of exercising
        the window under test, which opens only after a worker claims the call.
        """
        calls: list[int] = []

        def _vet(_job: Any) -> str:
            calls.append(1)
            if len(calls) == 1:
                return ""
            started.set()
            assert release.wait(timeout=5), "the vet was never released"
            return ""

        return _vet

    @pytest.mark.asyncio
    async def test_a_script_payload_does_not_run_after_its_awaiter_gave_up(self):
        orch = _make_orchestrator()
        started, release = threading.Event(), threading.Event()
        done: list[str] = []
        executed: list[str] = []
        job = _job(script="probes.py:check")

        def _payload(*_args: Any, **_kwargs: Any) -> dict[str, str]:
            executed.append("ran")
            return {"status": "ok"}

        async with _cron_cb(orch, gate_reason=self._slow_vet(started, release)) as cb:
            with (
                patch.object(gw, "run_in_cron_pool", self._abandoning_pool(started, done)),
                patch.object(gw, "run_script_sandboxed", _payload),
            ):
                assert await cb(job) is None
                # Only NOW let the abandoned vet finish, so it reaches its
                # dispatch decision strictly after the caller released the guard.
                release.set()
                for _ in range(500):
                    if done:
                        break
                    await asyncio.sleep(0.01)

        assert executed == [], (
            "the payload executed after its awaiter gave up and released the "
            "overlap guard, so it can run alongside the next fire"
        )
        assert done == ["refused"], f"the abandoned call did not refuse its payload: {done}"

    @pytest.mark.asyncio
    async def test_a_command_payload_does_not_run_after_its_awaiter_gave_up(self):
        """The sibling call site, which shares the shape exactly."""
        orch = _make_orchestrator()
        started, release = threading.Event(), threading.Event()
        done: list[str] = []
        executed: list[str] = []
        job = _job(command="curl example.com")

        def _payload(*_args: Any, **_kwargs: Any) -> dict[str, str]:
            executed.append("ran")
            return {"status": "ok", "output": "done"}

        async with _cron_cb(orch, gate_reason=self._slow_vet(started, release)) as cb:
            with (
                patch.object(gw, "run_in_cron_pool", self._abandoning_pool(started, done)),
                patch.object(gw, "run_command_sandboxed", _payload),
            ):
                assert await cb(job) is None
                release.set()
                for _ in range(500):
                    if done:
                        break
                    await asyncio.sleep(0.01)

        assert executed == [], "the command executed after its awaiter gave up"
        assert done == ["refused"], f"the abandoned call did not refuse its payload: {done}"

    @pytest.mark.asyncio
    async def test_the_overlap_guard_is_still_released_so_the_job_fires_again(self):
        """The opposite direction, which is what the chosen remedy could break.

        Refusing the abandoned payload is the fix; PINNING the guard set would
        also stop the overlap, but a payload that never starts would then hold
        the guard forever and the job would never fire again -- a permanent
        silent stall, strictly worse than the harm. So the guard must still be
        released, and the refusal is what makes releasing it safe.
        """
        orch = _make_orchestrator()
        started, release = threading.Event(), threading.Event()
        done: list[str] = []
        job = _job(script="probes.py:check")

        async with _cron_cb(orch, gate_reason=self._slow_vet(started, release)) as cb:
            with (
                patch.object(gw, "run_in_cron_pool", self._abandoning_pool(started, done)),
                patch.object(gw, "run_script_sandboxed", lambda *_a, **_k: {"status": "ok"}),
            ):
                assert await cb(job) is None
                release.set()

        assert job.id not in orch._running_script_ids, (
            "the overlap guard was left held after an abandoned run, so this job "
            "can never fire again"
        )

    def test_the_hand_off_resolves_deterministically_in_both_orders(self):
        """Exactly one of claim/abandon may win, so the outcome is not a race.

        Without the lock a payload could start after the awaiter gave up:
        ``claim`` would read an unset flag that ``abandon`` sets an instant
        later, and the refusal would silently not happen.
        """
        abandoned_first = gw._ClaimHandoff()
        assert abandoned_first.abandon() is False, "nothing had started yet"
        assert abandoned_first.claim() is False, "a payload started after abandonment"

        claimed_first = gw._ClaimHandoff()
        assert claimed_first.claim() is True, "an unabandoned call must be allowed to start"
        assert claimed_first.abandon() is True, "abandon must report the payload as started"


# ═══════════════════════════════════════════════════════════════════════════
# The claim-time vet must not eat the subprocess cleanup margin
# ═══════════════════════════════════════════════════════════════════════════


class TestTheVetIsAnAccountedBudgetTerm:
    """A vet spending part of the payload's margin must not orphan the payload.

    ``timeout=cmd_timeout + 5`` is one budget covering, serially, the vet, the
    subprocess, and the subprocess's teardown -- and ``cron.py``'s
    ``_SUBPROC_CLEANUP_ALLOWANCE_SECS`` comment says what the 5s is for: "the
    wake deadline cancels only the executor FUTURE (threads are not
    interruptible), so a budget shorter than the subprocess bound leaves the
    subprocess running while the guards clear and later wakes duplicate it."

    So when the deadline lands with the payload ALREADY started, nothing can
    stop the thread; the guard releases while the subprocess runs on, and the
    next fire duplicates its side effects.  Distinct from the abandoned-vet
    case above, which is a payload that has NOT started and is curable by
    refusing it -- here it is running, so the only cure is budget accounting.

    Deliberately drives the REAL ``run_in_cron_pool``: the sibling class doubles
    it and ignores ``timeout``, so nothing there evaluates the arithmetic this
    turns on.  Durations are derived from the live budget functions rather than
    hardcoded, so the test follows the constants if they move.
    """

    @staticmethod
    def _budgets(job: Any) -> tuple[float, float]:
        """(vet bound, pre-fix inner backstop) from the live functions."""
        from kiro_crew.cron import effective_wake_budget
        from kiro_crew.executors import cron_gate_budget

        return cron_gate_budget(effective_wake_budget(job)), float((job.timeout or 300) + 5)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kind,runner",
        [("command", "run_command_sandboxed"), ("script", "run_script_sandboxed")],
    )
    async def test_the_guard_outlives_a_payload_started_after_a_slow_vet(self, kind, runner):
        """The guard must still be held when the payload finishes.

        If it is not, the deadline fired mid-subprocess and the next fire is
        free to run the same non-idempotent command concurrently.

        Parametrised over BOTH call sites deliberately.  They share the shape,
        and the script site is the worse of the two -- ``script_timeout =
        job.timeout or 30`` against ``cmd_timeout = job.timeout or 300``, so the
        same fixed teardown margin is proportionally far larger a share of it,
        while the script vet does strictly MORE work (it adds a capped body read
        from disk).  A fix pinned at one site only would let the other be
        reverted silently.
        """
        orch = _make_orchestrator()
        job = _job(**{kind: "probes.py:check" if kind == "script" else "curl example.com"},
                   timeout=1, timeout_secs=8)
        vet_bound, pre_fix_budget = self._budgets(job)

        # A vet inside its own bound, and a payload that then runs past the
        # PRE-FIX budget but inside the post-fix one -- so the only thing that
        # decides the outcome is whether the vet is accounted for.
        vet_secs = vet_bound * 0.75
        payload_secs = (pre_fix_budget - vet_secs) + 1.0
        assert vet_secs < vet_bound, "the vet must sit inside its own bound"
        assert vet_secs + payload_secs > pre_fix_budget, "must overrun the pre-fix budget"

        calls: list[int] = []
        guard_at_exit: list[bool] = []
        concurrent, peak = [0], [0]

        def _vet(_job: Any) -> str:
            calls.append(1)
            if len(calls) > 1:  # the CLAIM-time call; fire-time stays instant
                time.sleep(vet_secs)
            return ""

        def _payload(*_args: Any, **_kwargs: Any) -> dict[str, str]:
            concurrent[0] += 1
            peak[0] = max(peak[0], concurrent[0])
            try:
                time.sleep(payload_secs)
                guard_at_exit.append(job.id in orch._running_script_ids)
                return {"status": "ok", "output": "done"}
            finally:
                concurrent[0] -= 1

        async with _cron_cb(orch, gate_reason=_vet) as cb:
            with patch.object(gw, runner, _payload):
                await cb(job)
                # Let an orphaned subprocess finish so its observation lands.
                for _ in range(400):
                    if guard_at_exit:
                        break
                    await asyncio.sleep(0.05)

        assert guard_at_exit == [True], (
            f"the {kind} overlap guard was released while the payload was still "
            f"running (vet {vet_secs:.2f}s + payload {payload_secs:.2f}s against a "
            f"{pre_fix_budget:.0f}s backstop), so the next fire can duplicate "
            "its non-idempotent side effects"
        )
        assert peak[0] == 1, f"payload ran concurrently with itself: peak {peak[0]}"

    @pytest.mark.asyncio
    async def test_a_vet_that_outruns_its_allowance_refuses_the_payload(self):
        """An allowance is only a guarantee if the thing it covers cannot exceed it.

        The widened backstop carries a term for the vet; a vet that spends MORE
        than that term has eaten into the subprocess's own bound plus teardown,
        so starting the payload would recreate exactly the harm above.  Refusing
        before it starts trades a reported missed run for a silent duplicate.

        The disposition matters as much as the refusal: nothing ran, so the run
        is retention-shaped like starvation -- marked never-started, and
        deliberately NOT counted toward auto-pause, because a slow governance
        read is a fleet state rather than a job defect and
        ``_AUTO_PAUSE_THRESHOLD`` consecutive counts would disable a healthy job.
        """
        orch = _make_orchestrator()
        job = _job(command="curl example.com", timeout=1, timeout_secs=8, consecutive_failures=2)
        vet_bound, _ = self._budgets(job)
        calls: list[int] = []
        executed: list[str] = []

        def _vet(_job: Any) -> str:
            calls.append(1)
            if len(calls) > 1:
                time.sleep(vet_bound * 1.5)  # deliberately past the allowance
            return ""

        def _payload(*_args: Any, **_kwargs: Any) -> dict[str, str]:
            executed.append("ran")
            return {"status": "ok", "output": "done"}

        async with _cron_cb(orch, gate_reason=_vet) as cb:
            with patch.object(gw, "run_command_sandboxed", _payload):
                result = await cb(job)

        assert executed == [], (
            "the payload started after a vet that had already spent more than the "
            "allowance the deadline carries for it"
        )
        assert result is None
        assert job.run_never_started is True, "a refused payload never started"
        assert job.last_status == "error"
        assert job.consecutive_failures == 2, "a slow vet must not count toward auto-pause"
        assert job.fire_time_denied is False, "no policy decision was made"

    @pytest.mark.asyncio
    async def test_the_harness_can_observe_a_duplicate_at_all(self):
        """Positive control: two overlapping fires must show a peak of 2.

        Without this, a peak of 1 above could mean the harness simply cannot
        see concurrency rather than that the fix works.
        """
        orch = _make_orchestrator()
        job = _job(command="curl example.com", timeout=1, timeout_secs=8)
        concurrent, peak = [0], [0]
        entered = threading.Event()

        def _payload(*_args: Any, **_kwargs: Any) -> dict[str, str]:
            concurrent[0] += 1
            peak[0] = max(peak[0], concurrent[0])
            entered.set()
            try:
                time.sleep(0.4)
                return {"status": "ok", "output": "done"}
            finally:
                concurrent[0] -= 1

        async with _cron_cb(orch, gate_reason=lambda _j: "") as cb:
            with patch.object(gw, "run_command_sandboxed", _payload):
                # Bypass the overlap guard by firing two DISTINCT job ids, which
                # is what the guard is keyed on -- so this measures the counter,
                # not the guard.
                other = _job(id="j2", command="curl example.com", timeout=1, timeout_secs=8)
                await asyncio.gather(cb(job), cb(other))

        assert peak[0] == 2, f"the harness cannot observe concurrency: peak {peak[0]}"

    def test_every_term_spent_inside_the_run_budget_is_accounted_for(self):
        """Every deadline bounding a run must carry a term for each thing spent inside it.

        ``cron.py``'s own accounting names three: the subprocess bound, the pool
        queue wait, and the fire-time gate.  The claim-time vet is a fourth --
        it runs inside the worker, inside the same deadline -- and asserting on
        the helper alone would pass with the term missing from the deadline, so
        this reads the DEADLINE EXPRESSIONS themselves.

        Checks BOTH of them.  The execution guard and the reaper's
        defence-in-depth sweep bound the same run, and the helpers' own
        docstrings say why they are shared: "if only one of them accounts for
        that wait, the other pre-empts it, and the two failures are opposite and
        both silent".  A fourth term added to one and not the other reintroduces
        exactly that drift.  Parsed with ``ast`` so the assertion survives the
        expression being wrapped across lines.
        """
        from kiro_crew import cron as cron_mod

        tree = ast.parse(Path(cron_mod.__file__).read_text(encoding="utf-8"))
        owed = {"_pool_queue_allowance", "_gate_budget_allowance", "_vet_allowance"}
        run_deadlines: list[tuple[int, set[str]]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not any(
                isinstance(t, ast.Name) and t.id == "deadline" for t in node.targets
            ):
                continue
            called = {
                child.func.id
                for child in ast.walk(node.value)
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
            }
            # A RUN deadline is one that already accounts for at least one term.
            # That excludes the file-lock spin deadline (``time.monotonic() +
            # timeout``), which bounds a lock acquisition rather than a run, and
            # it needs no magic count that would rot as the file changes.
            if called & owed:
                run_deadlines.append((node.lineno, called & owed))

        assert len(run_deadlines) >= 2, (
            "expected at least the execution guard and the reaper sweep to bound "
            f"a run; found {len(run_deadlines)} -- this check may have gone vacuous"
        )
        for lineno, terms in run_deadlines:
            missing = owed - terms
            assert not missing, (
                f"the run deadline at cron.py:{lineno} does not account for "
                f"{sorted(missing)}; a term spent inside it but absent from it means "
                "this deadline pre-empts the one that does carry it"
            )


# ═══════════════════════════════════════════════════════════════════════════
# _deliver_script_result
# ═══════════════════════════════════════════════════════════════════════════


class TestDeliverScriptResult:
    """Delivery of a Report / Done message back into the originating session."""

    @pytest.mark.asyncio
    async def test_report_queues_into_a_running_slot(self):
        """A busy slot gets the notification queued, not dispatched."""
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        slot = _mock_slot(running=True)
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        job = _job(script="probes.py:check", session_key="dashboard:chat-1-2")
        result = {"status": "report", "message": "still red"}
        async with _cron_cb(orch, script_result=result) as cb:
            assert await cb(job) == "still red"
        slot.queue_append.assert_called_once()
        assert slot.append.call_args[0][0] == "queued"
        orch.dashboard_state.push_slots_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_report_injects_into_an_idle_slot(self):
        """An idle slot takes the notification as a real turn."""
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        slot = _mock_slot(running=False)
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        job = _job(script="probes.py:check", session_key="dashboard:chat-1-2")
        turn = MagicMock()
        result = {"status": "report", "message": "green again"}
        spawn = MagicMock(return_value=turn)
        with (
            patch.object(gw, "spawn_guarded_turn", spawn),
            patch.object(gw, "_run_chat", MagicMock(return_value=MagicMock())),
        ):
            async with _cron_cb(orch, script_result=result) as cb:
                assert await cb(job) == "green again"
        spawn.assert_called_once()
        assert slot.append.call_args[0][0] == "inject"
        assert slot.task is turn

    @pytest.mark.asyncio
    async def test_report_falls_back_to_notification_when_no_slot(self):
        """No live and no rehydratable slot degrades to a bell notification."""
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        job = _job(script="probes.py:check", session_key="dashboard:chat-gone")
        result = {"status": "report", "message": "orphaned"}
        with patch.object(gw, "_rehydrate_slot_from_history", MagicMock(return_value=None)):
            async with _cron_cb(orch, script_result=result) as cb:
                assert await cb(job) == "orphaned"
        orch.dashboard_state.notify.assert_called_once()
        assert orch.dashboard_state.notify.call_args[0][2] == "orphaned"

    @pytest.mark.asyncio
    async def test_report_without_session_key_notifies(self):
        """A job with no originating session still reaches the bell feed."""
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        job = _job(script="probes.py:check", session_key="")
        result = {"status": "report", "message": "no session"}
        async with _cron_cb(orch, script_result=result) as cb:
            assert await cb(job) == "no session"
        orch.dashboard_state.notify.assert_called_once()
        orch.dashboard_state.get_slot.assert_not_called()

    @pytest.mark.asyncio
    async def test_delivery_failure_does_not_break_the_run(self):
        """A delivery exception is logged; the run's own verdict still stands."""
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        orch.dashboard_state.get_slot = MagicMock(side_effect=RuntimeError("slots wedged"))
        job = _job(script="probes.py:check", session_key="dashboard:chat-1-2")
        result = {"status": "report", "message": "delivered nowhere"}
        async with _cron_cb(orch, script_result=result) as cb:
            assert await cb(job) == "delivered nowhere"
        assert job.last_status == "ok"

    @pytest.mark.asyncio
    async def test_done_removes_the_job(self):
        """A Done disposition delivers and then removes the one-shot job."""
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        svc = _cron_service_double()
        job = _job(script="probes.py:check", session_key="")
        result = {"status": "done", "message": "all clear"}
        async with _cron_cb(orch, svc=svc, sel_obj=_blind_sel(), script_result=result) as cb:
            assert await cb(job) == "all clear"
        svc.remove_job_async.assert_awaited_once_with(job.id)

    @pytest.mark.asyncio
    async def test_done_defers_removal_when_store_is_busy(self):
        """A busy store hands the removal to the deferred queue, not a retry."""
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        svc = _cron_service_double()
        svc.remove_job_async = AsyncMock(side_effect=CronStoreBusy("locked"))
        job = _job(script="probes.py:check", session_key="")
        result = {"status": "done", "message": "finished"}
        async with _cron_cb(orch, svc=svc, script_result=result) as cb:
            assert await cb(job) == "finished"
        svc.defer_removal.assert_called_once_with(job.id)

    @pytest.mark.asyncio
    async def test_silent_job_delivers_nothing(self):
        """A silent job's Report reaches neither a slot nor the bell."""
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        job = _job(script="probes.py:check", session_key="dashboard:chat-1-2", silent=True)
        result = {"status": "report", "message": "quiet"}
        async with _cron_cb(orch, script_result=result) as cb:
            assert await cb(job) == "quiet"
        orch.dashboard_state.notify.assert_not_called()
        orch.dashboard_state.get_slot.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# message-mode denial + _init_cron wiring
# ═══════════════════════════════════════════════════════════════════════════


class TestCronMessageDenialAndWiring:
    @pytest.mark.asyncio
    async def test_message_job_fire_time_denial(self):
        """An LLM cron denied at fire time never dispatches a turn."""
        orch = _make_orchestrator()
        orch.sessions = MagicMock()
        orch.ctx_builder = MagicMock()
        job = _job()
        async with _cron_cb(
            orch, gate_reason="capabilities.cron off", sel_obj=_blind_sel()
        ) as cb:
            assert await cb(job) is None
        assert job.fire_time_denied is True
        assert job.consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_reaper_not_started_without_sessions(self):
        """Arming the scheduler without a session manager skips the reaper."""
        orch = _make_orchestrator(no_crons=False)
        orch.sessions = None
        svc = _cron_service_double()
        async with _cron_cb(orch, svc=svc):
            pass
        svc.start.assert_awaited_once()
        svc.start_reaper.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# Non-Slack channel reply resolution
# ═══════════════════════════════════════════════════════════════════════════


class TestChannelReplyLink:
    """``_channel_reply_link``'s resolution ladder and its refusals."""

    def test_slack_and_local_keys_return_none(self):
        orch = _make_orchestrator()
        orch.sessions = _channel_sessions()
        assert orch._channel_reply_link("slack:C1") is None
        assert orch._channel_reply_link("dashboard:chat-1-2") is None

    def test_no_session_manager_returns_none(self):
        orch = _make_orchestrator()
        orch.sessions = None
        assert orch._channel_reply_link("discord:kirocrew:direct:U9") is None

    def test_link_getter_failure_falls_through_to_stored_channel(self):
        """A raising link getter degrades to the next rung, it does not propagate."""
        orch = _make_orchestrator()
        orch.sessions = _channel_sessions(origin_raises=True, stored="discord:U9")
        resolved = orch._channel_reply_link("discord:kirocrew:direct:U9")
        assert resolved is not None
        link, needs_dm = resolved
        assert (link.channel_type, link.channel_id, needs_dm) == ("discord", "U9", True)

    def test_stored_channel_lookup_failure_returns_none(self):
        orch = _make_orchestrator()
        orch.sessions = _channel_sessions(stored_raises=True)
        assert orch._channel_reply_link("discord:kirocrew:direct:U9") is None

    def test_no_stored_channel_returns_none(self):
        orch = _make_orchestrator()
        orch.sessions = _channel_sessions(stored="")
        assert orch._channel_reply_link("discord:kirocrew:direct:U9") is None

    @pytest.mark.parametrize(
        "stored",
        ["nocolon", "slack:U9", ":U9", "discord:"],
        ids=["no-separator", "slack-typed", "empty-type", "empty-peer"],
    )
    def test_unusable_stored_value_returns_none(self, stored):
        orch = _make_orchestrator()
        orch.sessions = _channel_sessions(stored=stored)
        assert orch._channel_reply_link("discord:kirocrew:direct:U9") is None

    @pytest.mark.parametrize(
        "stored",
        ["bogus:U9", "unified:U9"],
        ids=["unregistered-namespace", "unified-namespace"],
    )
    def test_unified_bucket_validates_stored_namespace(self, stored):
        """A unified DM bucket only accepts a registered non-unified namespace."""
        orch = _make_orchestrator()
        orch.sessions = _channel_sessions(stored=stored)
        assert orch._channel_reply_link("unified:kirocrew:direct:U9") is None

    def test_group_session_never_takes_the_stored_rung(self):
        """A group key's stored value is the sender, so it must not become a DM."""
        orch = _make_orchestrator()
        orch.sessions = _channel_sessions(stored="discord:U9")
        assert orch._channel_reply_link("discord:kirocrew:group:C1") is None

    def test_origin_link_wins_and_needs_no_dm_resolution(self):
        orch = _make_orchestrator()
        origin = gw.ChannelLink("discord", channel_id="C77", thread_id="T1")
        orch.sessions = _channel_sessions(origin=origin, stored="discord:U9")
        assert orch._channel_reply_link("discord:kirocrew:direct:U9") == (origin, False)


class TestDeliverChannelReply:
    """``_deliver_channel_reply``'s early refusals."""

    @pytest.mark.asyncio
    async def test_blank_text_is_not_delivered(self):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        assert await orch._deliver_channel_reply("discord:kirocrew:direct:U9", "   ") is False

    @pytest.mark.asyncio
    async def test_no_dashboard_state_is_not_delivered(self):
        orch = _make_orchestrator()
        orch.dashboard_state = None
        assert await orch._deliver_channel_reply("discord:kirocrew:direct:U9", "hi") is False

    @pytest.mark.asyncio
    async def test_unresolvable_key_is_not_delivered(self):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        orch.sessions = _channel_sessions(stored="")
        assert await orch._deliver_channel_reply("discord:kirocrew:direct:U9", "hi") is False

    @pytest.mark.asyncio
    async def test_target_resolution_failure_degrades_to_false(self):
        """A raising governance ladder degrades the caller, it never propagates."""
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        link = gw.ChannelLink("discord", channel_id="C77")

        def _boom(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("profile dir unreadable")

        with patch.object(gw, "_resolve_channel_target", _boom):
            delivered = await orch._deliver_channel_reply(
                "discord:kirocrew:direct:U9", "hi", resolved_link=(link, False)
            )
        assert delivered is False

    @pytest.mark.asyncio
    async def test_no_governed_target_is_not_delivered(self):
        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        link = gw.ChannelLink("discord", channel_id="C77")
        with patch.object(gw, "_resolve_channel_target", MagicMock(return_value=None)):
            delivered = await orch._deliver_channel_reply(
                "discord:kirocrew:direct:U9", "hi", resolved_link=(link, False)
            )
        assert delivered is False

    # A sub-agent result whose code block spans the channel's message cap: the
    # shape of a diff or log dump, and the one a fixed-width slice mangles. The
    # lines inside carry prose markup a dialect converter WOULD rewrite if the
    # part they land in has lost the fence opener.
    _FENCED = (
        "Here is the diff:\n\n"
        "```diff\n"
        "**never bold** inside a code block\n"
        "# never a heading inside a code block\n"
        "- never a bullet inside a code block\n"
        "```\n"
    )

    @pytest.mark.asyncio
    async def test_a_long_fenced_result_keeps_every_code_line_inside_its_fence(self):
        """The pre-split IS final: each part arrives already under the cap.

        So the channel's own fence-safe splitter is a no-op on it, and a blind
        fixed-width cut through the block leaves part two with no opener: every
        line in it then takes the prose branch of the dialect converter and the
        markup INSIDE the code is rewritten.
        """
        from kiro_crew.messaging.split import FENCE_OUTSIDE, iter_fence_lines

        orch = _make_orchestrator()
        orch.dashboard_state = _mock_dashboard_state()
        link = gw.ChannelLink("discord", channel_id="C77")
        transport = SimpleNamespace(
            capabilities=SimpleNamespace(max_message_chars=90),
            send_message=AsyncMock(return_value="mid"),
        )
        resolved = SimpleNamespace(channel_id="C77", thread_id=None)
        with patch.object(
            gw, "_resolve_channel_target", MagicMock(return_value=(resolved, transport))
        ):
            delivered = await orch._deliver_channel_reply(
                "discord:kirocrew:direct:U9", self._FENCED, resolved_link=(link, False)
            )

        assert delivered is True
        parts = [c.args[1] for c in transport.send_message.await_args_list]
        assert len(parts) > 1, "precondition: the result must actually have been split"
        markers = [
            "**never bold** inside a code block",
            "# never a heading inside a code block",
            "- never a bullet inside a code block",
        ]
        for part in parts:
            prose = [ln for ln, role in iter_fence_lines(part) if role == FENCE_OUTSIDE]
            for marker in markers:
                assert marker not in prose, (
                    f"a code line landed outside its fence:\n{part}"
                )
        joined = "\n".join(parts)
        for marker in markers:
            assert marker in joined, "content must survive the split"
