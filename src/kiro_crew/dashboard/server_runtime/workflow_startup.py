"""The workflow service's startup.

The readiness gate installed before the bind, the off-boot restore after credential
publication, and its cancellation at shutdown.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.server import (
        DashboardState,
        KiroCrewConfig,
        _autonudge_get,
        authorize_and_add_nudge,
        logger,
    )


async def _initialize_workflow_service(state: DashboardState) -> None:
    """Restore fully off the boot path; publish only on the owning loop."""
    service = None
    attachment_started = False
    try:
        from kiro_crew.dashboard.handlers import workflows as wf_handlers
        from kiro_crew.dashboard.workflow_inject import inject_bound_workflow_result
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls
        from kiro_crew.workflows.service import WorkflowService

        def _wf_on_event(run_id: str, event_json: dict) -> None:
            try:
                sess = ""
                svc = getattr(state, "workflow_service", None)
                if svc is not None:
                    h = svc.registry.get(run_id)
                    if h is not None:
                        sess = h.session_key
                safe_event = wf_handlers._redact_obj(event_json)
                state.broadcast_ws(
                    "workflow_run_event",
                    {"run_id": run_id, "session_key": sess, **safe_event},
                )
            except Exception:
                logger.debug("workflow on_event broadcast failed", exc_info=True)

        def _wf_on_done(run_id: str, snapshot: dict) -> None:
            def _auto_turn(slot: Any, snap: dict) -> None:
                try:
                    from kiro_crew.dashboard.chat import _run_chat

                    raw_name = snap.get("name") or snap.get("run_id", run_id)
                    name, _ = redact_exfiltration_urls(str(raw_name))
                    name, _ = redact_credentials(name)
                    status, _ = redact_exfiltration_urls(str(snap.get("status", "")))
                    status, _ = redact_credentials(status)
                    prompt = (
                        f"[Workflow `{name}` finished: {status}] Its result was just "
                        "posted above. The user is waiting on the answer to the "
                        "request that prompted this workflow — find that request "
                        "earlier in this conversation and answer it directly. Your "
                        "final message is the only part of this turn the user is "
                        "guaranteed to see, so make it a standalone deliverable: lead "
                        "with the answer, and keep run mechanics (which agents ran, "
                        "what was verified, what is still uncertain) to a short "
                        "closing note or a collapsed fold. If the workflow failed or "
                        "came back incomplete, say that plainly and state what is "
                        "still unknown."
                    )
                    started = slot.enqueue_or_run_prompt(prompt, _run_chat, state)
                    state.push_slots_update()
                    logger.info(
                        "workflow %s result -> chat slot %s: agent turn %s",
                        run_id,
                        getattr(slot, "key", "?"),
                        "started" if started else "queued",
                    )
                except Exception:
                    logger.warning("workflow %s auto-turn failed", run_id, exc_info=True)

            try:
                delivery = asyncio.create_task(
                    inject_bound_workflow_result(state, run_id, snapshot, on_injected=_auto_turn)
                )
                state._background_tasks.add(delivery)
                delivery.add_done_callback(state._background_tasks.discard)
            except Exception:
                logger.debug("workflow on_done injection failed", exc_info=True)

        # Workflow agent concurrency stays at this fixed cap ON PURPOSE. Sizing it
        # from resolve_max_subagents() looks tempting (it is the sizing authority
        # in mcp_core / slack gateway / context), but the warm pool keeps a
        # SEPARATE sub-pool per agent/model/CWD identity and its own documented
        # aggregate bound is ``(max_identities + 1) * max_workers`` — 9 * this
        # value (see workflows/agent_pool.py). Feeding an auto-sized cap in here
        # would raise the worst-case resident kiro-cli workers from 9*4=36 to
        # 9*subagent_auto_max=288 and OOM the gateway on a large host. Revisit
        # only once the pool enforces ONE aggregate worker limit.
        _wf_concurrency = 4
        # The run ceiling is unaffected by that and IS config-driven.
        _wf_timeout_secs: int | None = None
        try:
            cfg = await asyncio.to_thread(KiroCrewConfig.load)
            _wf_timeout_secs = int(cfg.agent.workflow_run_timeout_secs)
        except Exception:
            logger.debug("workflow run-ceiling config unavailable; using default", exc_info=True)

        async def _wf_nudge_authorizer(
            *, slot_key: str, message: str, idle_secs: int, max_cycles: int
        ) -> str | None:
            """Keep workflow nudges on the shared authorization/audit chokepoint."""
            _loop, error, _status = await authorize_and_add_nudge(
                svc=_autonudge_get(),
                state=state,
                slot_key=slot_key,
                message=message,
                idle_secs=idle_secs,
                max_cycles=max_cycles,
                source="workflow",
            )
            if error is not None:
                logger.info("workflow ctx.nudge not armed for %s: %s", slot_key, error)
            return error

        service = await WorkflowService.create(
            sessions=state.sessions,
            context_builder=state.context_builder,
            on_done=_wf_on_done,
            on_event=_wf_on_event,
            now_fn=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            concurrency=_wf_concurrency,
            nudge_authorizer=_wf_nudge_authorizer,
            timeout_secs=_wf_timeout_secs,
        )
        # Cancellation cannot stop a to_thread worker. Even if the factory
        # finishes while shutdown drains it, its result must remain unpublished.
        if (
            state.workflow_startup_stopping
            or getattr(state.sessions, "admission_closed", False) is True
        ):
            state.workflow_startup_status = "stopped"
            return
        if state.task_runner is not None:
            attachment_started = True
            service.attach_task_runner(state.task_runner)
            state.task_runner.attach_workflow_service(service)
        # No await between attachment, publication and opening admission.
        state.workflow_service = service
        state.workflow_startup_status = "ready"
        logger.info("WorkflowService ready (run ceiling=%ss)", service.timeout_secs)
    except asyncio.CancelledError:
        state.workflow_startup_status = "stopped" if state.workflow_startup_stopping else "failed"
        raise
    except Exception:
        state.workflow_startup_status = "failed"
        logger.warning("WorkflowService unavailable", exc_info=True)
    finally:
        if state.workflow_startup_status != "ready":
            state.workflow_service = None
            if state.task_runner is not None:
                try:
                    if attachment_started:
                        state.task_runner.attach_workflow_service(None)
                finally:
                    state.task_runner.defer_workflow_attachment(
                        failed=state.workflow_startup_status == "failed"
                    )
            if service is not None and attachment_started:
                service.attach_task_runner(None)


def _register_workflow_lifecycle(app: web.Application, state: DashboardState) -> None:
    """Install gates before bind, without starting imports or disk recovery."""
    state.workflow_startup_status = "pending"
    state.workflow_startup_stopping = False
    if state.task_runner is not None:
        state.task_runner.defer_workflow_attachment()

    @web.middleware
    async def _workflow_ready(request: web.Request, handler: Any) -> web.StreamResponse:
        # TaskRunner owns its typed mutation gate; status and cancel stay usable.
        dependent = request.path == "/api/workflows" or request.path.startswith("/api/workflows/")
        if dependent and state.workflow_startup_status != "ready":
            failed = state.workflow_startup_status == "failed"
            return web.json_response(
                {
                    "error": (
                        "Workflow initialization failed; restart the gateway."
                        if failed
                        else "Workflows are not ready; retry later"
                    ),
                    "code": "workflow_initialization_failed" if failed else "workflows_unavailable",
                },
                status=503,
            )
        return await handler(request)

    async def _workflow_stop_publication(_app: web.Application) -> None:
        state.workflow_startup_stopping = True
        state.workflow_startup_status = "stopped"
        if state.task_runner is not None:
            state.task_runner.defer_workflow_attachment()

    async def _workflow_shutdown(_app: web.Application) -> None:
        task = state.workflow_startup_task
        if task is None:
            return
        if not task.done():
            task.cancel()
        drain = asyncio.gather(task, return_exceptions=True)
        while not drain.done():
            try:
                await asyncio.shield(drain)
            except asyncio.CancelledError:
                pass

    app.middlewares.append(_workflow_ready)
    app.on_shutdown.append(_workflow_stop_publication)
    # Registered after tunnel cleanup, but fenced before any cleanup can yield.
    app.on_cleanup.append(_workflow_shutdown)


def _kick_workflow_initialization(state: DashboardState) -> None:
    """Called only after listener bind and successful credential publication."""
    if state.workflow_startup_task is not None or state.workflow_startup_stopping:
        return
    task = asyncio.create_task(_initialize_workflow_service(state), name="workflow-initialization")
    state.workflow_startup_task = task
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)
