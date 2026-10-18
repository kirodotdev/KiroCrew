"""Shared harness for the ``test_slack_gateway_cron_exec_coverage*.py`` modules.

``_make_orchestrator`` (a ``GatewayOrchestrator`` with mocked credentials and no Slack
tokens), ``_job`` (a real ``CronJob``), the dashboard-state, cron-service, SEL and
sandbox-runner doubles, ``_cron_cb`` (the cron callback with its collaborators
patched) and, for the ``message`` arm, ``_message_arm_sessions`` and ``_cron_message_cb``,
which stubs out the LLM turn.
"""

from __future__ import annotations

from contextlib import ExitStack, asynccontextmanager
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.cron import CronJob
from kiro_crew.slack import gateway as gw


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
            # No executor patch: the fire-time gate does not resolve a pool from
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


def _message_arm_sessions() -> MagicMock:
    """A SessionManager double for the ``message`` (LLM) cron arm."""
    sessions = MagicMock()
    sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    sessions.set_thread = AsyncMock()
    sessions.set_channel = AsyncMock()
    sessions.get_origin_link = MagicMock(return_value=gw.ChannelLink("telegram", channel_id="C77"))
    sessions.get_mirror_link = MagicMock(return_value=None)
    sessions.get_channel = MagicMock(return_value="")
    return sessions


@asynccontextmanager
async def _cron_message_cb(
    orch: Any, *, result_text: str = "", error: BaseException | None = None
) -> AsyncIterator[Any]:
    """Yield ``on_job`` with the ``message`` arm's LLM turn stubbed out.

    Everything between session acquisition and the delivery fan-out is replaced:
    the point of these tests is which SURFACE a finished result reaches, so the
    turn itself is a constant. ``_cron_stream_with_posttoken_resume`` is the one
    seam that has to be patched by name — the arm resolves it from module
    globals at call time, which is why this nests inside ``_cron_cb`` rather
    than wrapping it.

    ``error`` drives the FAILURE fan-out instead of the success one by making the
    stubbed turn raise, which is the only way into the arm's ``except`` branch.
    It must NOT look like a transient backend error, or the arm retries the whole
    callback rather than alerting.
    """
    orch.ctx_builder = MagicMock()
    orch.ctx_builder.hooks = MagicMock()
    orch.subagent_mgr = MagicMock()
    orch.subagent_mgr.has_pending_work_for = MagicMock(return_value=False)
    orch.subagent_mgr.has_pending_work_for_async = AsyncMock(return_value=False)
    _turn = (
        AsyncMock(side_effect=error)
        if error is not None
        else AsyncMock(return_value=(result_text, 0))
    )
    with ExitStack() as stack:
        for patcher in (
            patch.object(gw, "_cron_stream_with_posttoken_resume", _turn),
            patch.object(gw, "publish_turn_identity", AsyncMock()),
            patch.object(gw, "run_in_embed_pool", AsyncMock(return_value=("full msg", None))),
            patch.object(gw, "persist_token_record_async", AsyncMock()),
            patch.object(gw, "read_context_tokens", MagicMock(return_value=(0, 0))),
            patch.object(gw, "provider_last_turn_usage", MagicMock()),
            patch.object(gw, "read_effective_agent", MagicMock(return_value="")),
            patch.object(gw, "context_meter_reading", MagicMock(return_value=None)),
        ):
            stack.enter_context(patcher)
        async with _cron_cb(orch) as callback:
            yield callback
