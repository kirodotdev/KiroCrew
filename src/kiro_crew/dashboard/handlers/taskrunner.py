"""Task runner API handlers — start, cancel, plan, refine, from-chat, to-chat."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from pathlib import Path

from aiohttp import web

from kiro_crew.dashboard.state import DashboardState
from kiro_crew.security import is_sensitive_path, redact_credentials, redact_exfiltration_urls
from kiro_crew.task_planner import parse_spec_approval_mode, plan_to_yaml

logger = logging.getLogger(__name__)


def _sel():
    """Late-binding _sel() for test monkeypatch compatibility."""
    import kiro_crew.dashboard.handlers as _pkg  # noqa: F811
    return _pkg.sel()


async def _gate_auto_approve(
    request: web.Request, requested: bool, claimed_source, endpoint: str
) -> bool:
    """Provenance gate for per-run auto-approve, shared by every launch endpoint.

    Per-run trust is a human-at-the-dashboard decision, so it is honored ONLY for a
    dashboard-context request — ``request["app"] == ""`` (set by
    ``token_auth_middleware`` for the dashboard itself), which blocks an
    app/proxy-embedded caller from minting trust even while claiming
    ``source: "dashboard"``. When a caller declares a ``source`` (the start
    endpoint), it must be the literal ``"dashboard"`` (checked on the raw claimed
    value so an omitted/unknown source cannot inherit trust via coercion). The
    grant decision is SEL-audited (claimed-vs-resolved source + request app), so a
    machine grant cannot masquerade as a human one without a trace. Residual: a raw
    token-holder is indistinguishable from the dashboard UI (gateway trust model is
    "token == user"); a sub-principal auth model is a platform-level follow-up.

    ``claimed_source=None`` means the endpoint carries no source claim (e.g.
    ``/execute`` operates on an existing run) — only the app-context check applies.
    """
    request_app = request.get("app", "")
    granted = requested
    if requested and (request_app != "" or (claimed_source is not None and claimed_source != "dashboard")):
        granted = False
    if requested:
        try:
            # Offloaded to a worker thread: critical=True writes SYNCHRONOUSLY and
            # RAISES on a sink failure (so an unwritable/full SEL store reaches the
            # except below and fails the grant closed — the default critical=False
            # only queues the write and would swallow an async failure). Running
            # that synchronous flush inline would block the gateway event loop
            # (no-blocking-call-on-event-loop), so it is awaited via to_thread:
            # off the loop, yet the await still propagates a write failure here.
            # The whole call — INCLUDING the _sel() lookup (which may lazily
            # initialize the log) — runs in the worker so nothing touches the loop.
            await asyncio.to_thread(
                lambda: _sel().log_tool_invocation(
                    session_key="dashboard",
                    source="taskrunner",
                    tool_name="auto_approve_grant",
                    outcome="granted" if granted else "denied",
                    metadata={
                        "endpoint": endpoint,
                        "claimed_source": str(claimed_source),
                        "request_app": str(request_app),
                    },
                    critical=True,
                )
            )
        except Exception:
            # The audit write is a fallible side-effect. Contain it HERE so the
            # invariant holds for EVERY caller (current and future) rather than
            # relying on each endpoint to remember to wrap the gate in try/except
            # (CWE-755 — an unsanitized traceback would otherwise escape as a
            # 500). Fail closed: a grant we could not audit is downgraded to
            # denied, never silently honored.
            logger.exception(
                "auto_approve grant audit-write failed for endpoint=%s; failing closed",
                endpoint,
            )
            return False
    return granted


# ── Task Runner ──


async def api_taskrunner_status(request: web.Request) -> web.Response:
    """GET /api/taskrunner — current task runner status."""
    state: DashboardState = request.app["state"]
    if not state.task_runner:
        return web.json_response({"running": False, "available": False})
    data = state.task_runner.status()
    visible_sources = {"text", "spec", "file", "chat", "dashboard", "mcp", "yaml"}
    data["runs"] = [r for r in data["runs"] if r.get("source") in visible_sources]
    for run in data["runs"]:
        if run.get("error"):
            run["error"] = redact_exfiltration_urls(run["error"])[0]
            run["error"] = redact_credentials(run["error"])[0]
        for step in run.get("task_details", []):
            for field in ("title", "description", "result", "error"):
                if step.get(field):
                    step[field] = redact_exfiltration_urls(step[field])[0]
                    step[field] = redact_credentials(step[field])[0]
        if run.get("lessons_learned"):
            # lessons_learned is list[str] of LLM-generated text — redact each
            # element (same untrusted-string class as the fields above).
            run["lessons_learned"] = [
                redact_credentials(redact_exfiltration_urls(lesson)[0])[0]
                for lesson in run["lessons_learned"]
            ]
    data["available"] = True
    # Pre-fill value for the UI's per-run workspace-folder selector: the
    # configured workspace_dir if set, else the default per-run base directory.
    # str-coerced so the payload always JSON-serializes.
    _ws = state.task_runner._workspace_dir
    data["default_workspace_dir"] = str(_ws) if _ws else str(state.task_runner._work_dir)
    return web.json_response(data)


async def api_taskrunner_start(request: web.Request) -> web.Response:
    """POST /api/taskrunner — start a task from a spec file path or inline content.

    Body: ``{"spec": "path/to/file.md"}`` or ``{"spec": "__inline__:# Task content..."}``
    Inline specs are written to a temp file in the work directory.
    """
    state: DashboardState = request.app["state"]
    if not state.task_runner:
        return web.json_response({"error": "task runner not available"}, status=400)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    spec_path = body.get("spec", "")
    if not spec_path:
        return web.json_response({"error": "spec path required"}, status=400)

    # Validate non-inline paths against traversal, then forward the *validated*
    # resolved path (not the raw input) to the sink below so the guard and the
    # use share one value — no gap where spec_path could differ from what was
    # checked, and the containment guard is visible to static analysis.
    if not spec_path.startswith("__inline__:"):
        resolved = Path(spec_path).resolve()
        if ".." in Path(spec_path).parts or not resolved.is_file():
            return web.json_response({"error": "invalid spec path"}, status=400)
        if is_sensitive_path(str(resolved)):
            return web.json_response({"error": "access denied"}, status=403)
        spec_path = str(resolved)

    # Spec text used ONLY to read a declared approval mode (see below). Captured
    # for the inline case here; for a file path it is read as a bounded prefix.
    spec_text_for_approval = ""

    # Handle inline spec content
    if spec_path.startswith("__inline__:"):
        content = spec_path[len("__inline__:"):]
        if not content.strip():
            return web.json_response({"error": "empty spec content"}, status=400)
        spec_text_for_approval = content
        work_dir = state.task_runner._work_dir
        fname = f"TASK_{uuid.uuid4().hex[:8]}.md"
        fpath = Path(work_dir) / fname
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(content, encoding="utf-8")
        spec_path = str(fpath)
    else:
        # Bounded read of the top of the validated file — only for the approval
        # directive; decomposition re-reads the full spec later. A read failure
        # is non-fatal: it just leaves the declared mode empty (per-action).
        try:
            spec_text_for_approval = await asyncio.to_thread(
                lambda: Path(spec_path).open(encoding="utf-8").read(4000)
            )
        except OSError:
            spec_text_for_approval = ""

    try:
        agent = body.get("agent", "")
        task_name = body.get("name", "")
        workspace_dir = body.get("workspace_dir", "")
        allowed_sources = {"dashboard", "text", "spec", "file", "chat", "mcp", "cron", "yaml"}
        claimed_source = body.get("source")
        source = claimed_source if claimed_source in allowed_sources else "dashboard"
        # A spec may DECLARE `approval: auto` (frontmatter or a top-of-file
        # directive) as an alternative to the UI's per-run toggle, so a trusted
        # plan can commit its unattended-execution intent into version control.
        # This is only a REQUEST — it is OR-ed into the same `_gate_auto_approve`
        # provenance check as the UI flag, so it can never widen trust beyond
        # what the launching surface already allows: a dashboard human's spec can
        # run unattended, but the identical directive on a cron/MCP launch is
        # still denied. Deny-by-default: only a literal `auto` requests it.
        spec_declares_auto = parse_spec_approval_mode(spec_text_for_approval) == "auto"
        auto_approve = await _gate_auto_approve(
            request,
            body.get("auto_approve") is True or spec_declares_auto,
            claimed_source,
            endpoint="start",
        )
        task_id = await state.task_runner.start_background(
            spec_path, agent=agent, name=task_name, source=source, workspace_dir=workspace_dir,
            auto_approve=auto_approve,
        )
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=400)
    return web.json_response({"ok": True, "spec": spec_path, "task_id": task_id})


async def api_taskrunner_cancel(request: web.Request) -> web.Response:
    """POST /api/taskrunner/cancel — cancel a specific or all running tasks."""
    state: DashboardState = request.app["state"]
    if not state.task_runner:
        return web.json_response({"error": "task runner not available"}, status=400)
    try:
        body = await request.json() if request.content_length else {}
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    state.task_runner.cancel(body.get("task_id"))
    return web.json_response({"ok": True})


async def api_taskrunner_pause(request: web.Request) -> web.Response:
    """POST /api/taskrunner/{task_id}/pause — pause a running task (resumable via execute)."""
    state: DashboardState = request.app["state"]
    if not state.task_runner:
        return web.json_response({"error": "task runner not available"}, status=400)
    task_id = request.match_info["task_id"]
    runner = state.task_runner
    run = runner._runs.get(task_id)
    if not run:
        matches = [r for r in runner._runs.values() if r.name == task_id]
        run = matches[0] if matches else None
    if not run:
        _sel().log_tool_invocation(
            session_key="api", source="dashboard", tool_name="taskrunner_pause",
            outcome="error", resources=task_id, metadata={"reason": "not found"},
        )
        return web.json_response({"error": "not found"}, status=404)
    if run.status != "running":
        _sel().log_tool_invocation(
            session_key="api", source="dashboard", tool_name="taskrunner_pause",
            outcome="denied", resources=run.task_id,
            metadata={"reason": f"status={run.status}"},
        )
        return web.json_response({"error": f"cannot pause (status={run.status})"}, status=409)
    runner.pause(run.task_id)
    _sel().log_tool_invocation(
        session_key="api", source="dashboard", tool_name="taskrunner_pause",
        outcome="success", resources=run.task_id,
    )
    return web.json_response({"ok": True, "message": "Paused — use execute to resume"})


async def api_taskrunner_delete(request: web.Request) -> web.Response:
    """DELETE /api/taskrunner/{task_id} — remove a finished run."""
    state: DashboardState = request.app["state"]
    if not state.task_runner:
        return web.json_response({"error": "task runner not available"}, status=400)
    task_id = request.match_info["task_id"]
    run = state.task_runner._runs.get(task_id)
    if not run:
        return web.json_response({"error": "not found"}, status=404)
    if run.status in ("running", "cancelling"):
        return web.json_response({"error": "cancel first"}, status=409)
    state.task_runner._runs.pop(task_id, None)
    state.task_runner._stall_cancelled_ids.discard(task_id)
    await state.task_runner._apersist_runs()
    return web.json_response({"ok": True})


async def api_taskrunner_rename(request: web.Request) -> web.Response:
    """PATCH /api/taskrunner/{task_id}/name — rename a task run."""
    state: DashboardState = request.app["state"]
    if not state.task_runner:
        return web.json_response({"error": "task runner not available"}, status=400)
    task_id = request.match_info["task_id"]
    run = state.task_runner._runs.get(task_id)
    if not run:
        return web.json_response({"error": "not found"}, status=404)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    name = data.get("name", "").strip()
    if not name:
        return web.json_response({"error": "name required"}, status=400)
    run.name = name
    await state.task_runner._apersist_runs()
    return web.json_response({"ok": True, "name": name})


async def api_taskrunner_update_task(request: web.Request) -> web.Response:
    """PATCH /api/taskrunner/{task_id}/tasks/{index} — edit a pending task in-place."""
    state: DashboardState = request.app["state"]
    if not state.task_runner:
        return web.json_response({"error": "task runner not available"}, status=400)
    task_id = request.match_info["task_id"]
    try:
        index = int(request.match_info["index"])
    except ValueError:
        return web.json_response({"error": "invalid index"}, status=400)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    try:
        result = await state.task_runner.update_task(task_id, index, data)
        # SEL audit for all task field changes
        _sel().log_tool_invocation(
            session_key="dashboard",
            agent="user",
            source="taskrunner_update_task",
            tool_name="update_task",
            tool_kind="write",
            outcome="success",
            metadata={"task_id": task_id, "index": index, "fields": list(data.keys())},
        )
        return web.json_response({"ok": True, **result})
    except ValueError as exc:
        _sel().log_tool_invocation(
            session_key="dashboard", agent="user", source="taskrunner_update_task",
            tool_name="update_task", tool_kind="write", outcome="denied",
            metadata={"task_id": task_id, "index": index, "error": str(exc)},
        )
        return web.json_response({"error": str(exc)}, status=409)


async def api_taskrunner_retry(request: web.Request) -> web.Response:
    """POST /api/taskrunner/{task_id}/retry — retry from a specific step."""
    state: DashboardState = request.app["state"]
    if not state.task_runner:
        return web.json_response({"error": "task runner not available"}, status=400)
    task_id = request.match_info["task_id"]
    try:
        body = await request.json() if request.content_length else {}
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    from_step = body.get("from_step", 1)
    try:
        await state.task_runner.retry_from_task(task_id, from_step, agent=state.task_runner._agent or "")
        return web.json_response({"ok": True, "task_id": task_id})
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)


async def api_taskrunner_plan_context(request: web.Request) -> web.Response:
    """GET /api/taskrunner/{task_id}/plan-context — return plan text for chat pre-fill."""
    state: DashboardState = request.app["state"]
    if not state.task_runner:
        return web.json_response({"error": "task runner not available"}, status=400)
    task_id = request.match_info["task_id"]
    run = state.task_runner._runs.get(task_id)
    if not run or run.status != "planned":
        return web.json_response({"error": "not found or not planned"}, status=404)
    context = state.task_runner.plan_to_chat_context(task_id)
    return web.json_response({"ok": True, "context": context, "task_id": task_id})


async def api_taskrunner_export_yaml(request: web.Request) -> web.Response:
    """GET /api/taskrunner/{task_id}/plan.yaml — download the run's plan as a YAML workflow.

    Serializes the run's tasks to the ``agents:`` schema so the file round-trips
    back through the "From YAML" import path.
    """
    state: DashboardState = request.app["state"]
    if not state.task_runner:
        return web.json_response({"error": "task runner not available"}, status=400)
    task_id = request.match_info["task_id"]
    run = state.task_runner._runs.get(task_id)
    if not run:
        # Generic 404 — do not reflect the requested id or reveal existence.
        return web.json_response({"error": "not found"}, status=404)
    if not run.tasks:
        return web.json_response({"error": "no plan to export"}, status=409)

    try:
        yaml_text = plan_to_yaml(run.tasks)
    except Exception:
        # Generic message; details stay in server logs (no stack trace to client).
        logger.exception("plan_to_yaml failed for task_id=%s", task_id)
        return web.json_response({"error": "failed to export plan"}, status=500)
    # Plan titles/descriptions are LLM/user-authored text — redact before download,
    # mirroring the status endpoint's treatment of the same fields.
    yaml_text = redact_exfiltration_urls(yaml_text)[0]
    yaml_text = redact_credentials(yaml_text)[0]
    # Sanitize the run name into a safe download filename (prevents header injection
    # / path chars leaking into Content-Disposition). Collapse any run of dots so a
    # traversal-looking ".." cannot survive the allowlist (which permits a single '.').
    # run.name is LLM-generated (auto_name) — apply BOTH redactors (matching the
    # yaml_text body above) before sanitizing: credential/URL fragments are
    # alphanumeric and would otherwise survive the allowlist into the header.
    raw_name = redact_exfiltration_urls(run.name or run.task_id)[0]
    raw_name = redact_credentials(raw_name)[0]
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", raw_name)
    safe_name = re.sub(r"\.{2,}", "_", safe_name).strip("._-") or "plan"
    return web.Response(
        text=yaml_text,
        content_type="application/x-yaml",  # explicit type — mitigates MIME sniffing
        charset="utf-8",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.yaml"'},
    )


async def api_taskrunner_to_chat(request: web.Request) -> web.Response:
    """POST /api/taskrunner/{task_id}/to-chat — open task results in a chat slot."""
    state: DashboardState = request.app["state"]
    if not state.task_runner:
        return web.json_response({"error": "task runner not available"}, status=400)
    task_id = request.match_info["task_id"]
    run = state.task_runner._runs.get(task_id)
    if not run:
        return web.json_response({"error": "not found"}, status=404)

    # Handle planned runs — send to chat for optimization
    if run.status == "planned":
        summary = state.task_runner.plan_to_chat_context(task_id)
        slot = state.get_or_create_slot()
        slot.title = f"Plan: {run.task_id}"
        slot.append("user", summary, "msg msg-u")
        from kiro_crew.dashboard.chat import _run_chat  # noqa: F811

        task = asyncio.create_task(_run_chat(state, slot, summary))
        slot.task = task
        state._background_tasks.add(task)
        task.add_done_callback(state._background_tasks.discard)
        state.push_slots_update()
        return web.json_response({"ok": True, "slot": slot.key, "task_id": task_id})

    # Build task summary for chat context
    from kiro_crew.taskrunner import TaskStatus  # noqa: F811

    spec_name = (
        Path(run.spec_path).stem if not run.spec_path.startswith("__inline__:") else run.task_id
    )
    lines = [f"# Task Review: {spec_name}\n"]
    lines.append(f"**Status**: {run.status} | **Steps**: {len(run.tasks)}\n")
    if run.work_dir:
        lines.append(f"**Work dir**: `{run.work_dir}`")
    if run.branch_name:
        lines.append(f"**Branch**: `{run.branch_name}`\n")
    lines.append("## Original Spec\n")
    lines.append(run.spec_content[:3000] if run.spec_content else "(no spec)")
    lines.append("\n## Step Results\n")
    for s in run.tasks:
        icon = (
            "✅"
            if s.status == TaskStatus.PASSED
            else (
                "❌"
                if s.status == TaskStatus.FAILED
                else "🔄" if s.status in (TaskStatus.IN_PROGRESS, TaskStatus.REVIEWING) else "⏭"
            )
        )
        lines.append(f"{icon} **Step {s.index}: {s.title}**")
        if s.error:
            lines.append(f"  Error: {s.error[:300]}")
    if run.error:
        lines.append(f"\n## Task Error\n{run.error}")
    if run.lessons_learned:
        lines.append("\n## Lessons Learned")
        for lesson in run.lessons_learned:
            lines.append(f"- {lesson}")

    # Status-specific prompt
    failed_steps = [s for s in run.tasks if s.status == TaskStatus.FAILED]
    if run.status == "completed" and not failed_steps:
        lines.append(
            "\n---\nThis task completed successfully. Review the changes in "
            f"`{run.work_dir}` and verify everything works as expected. "
            "Check for edge cases, missing tests, or improvements. "
            "If anything needs fixing, make the changes directly."
        )
    elif failed_steps:
        titles = ", ".join(f"Step {s.index}" for s in failed_steps)
        lines.append(
            f"\n---\nThis task failed at {titles}. Read the errors above, "
            "diagnose the root cause, and fix the issues. The work dir and "
            "git branch have the partial progress from passed steps."
        )
    else:
        lines.append(
            "\n---\nReview this task run. Check the results, "
            "fix any issues, or continue the work."
        )
    summary = "\n".join(lines)

    slot = state.get_or_create_slot()
    slot.title = f"Review: {spec_name}"
    slot.append("user", summary, "msg msg-u")

    # Auto-trigger LLM response so user doesn't have to send a message
    from kiro_crew.dashboard.chat import _run_chat  # noqa: F811

    task = asyncio.create_task(_run_chat(state, slot, summary))
    slot.task = task
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)

    state.push_slots_update()
    return web.json_response({"ok": True, "slot": slot.key})


async def api_taskrunner_plan(request: web.Request) -> web.Response:
    """POST /api/taskrunner/plan — decompose input into a plan without executing."""
    state: DashboardState = request.app["state"]
    if not state.task_runner:
        return web.json_response({"error": "task runner not available"}, status=400)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    input_text = body.get("input", "")
    source = body.get("source", "text")
    spec_path = body.get("spec", "")
    agent = body.get("agent", "")
    workspace_dir = body.get("workspace_dir", "")
    try:
        plan_coro = state.task_runner.plan(
            input_text=input_text,
            source=source,
            spec_path=spec_path,
            agent=agent,
            workspace_dir=workspace_dir,
        )
        state.task_runner._plan_task = asyncio.current_task()
        run = await plan_coro
    except asyncio.CancelledError:
        return web.json_response({"error": "Planning was cancelled."}, status=400)
    except (FileNotFoundError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)
    finally:
        state.task_runner._plan_task = None
    return web.json_response(
        {
            "ok": True,
            "task_id": run.task_id,
            "steps": [
                {
                    "index": s.index,
                    "title": redact_credentials(redact_exfiltration_urls(s.title or "")[0])[0],
                    "description": redact_credentials(redact_exfiltration_urls(s.description or "")[0])[0],
                    "depends_on": s.depends_on,
                    "requires_approval": s.requires_approval,
                    "force_approval": s.force_approval,
                }
                for s in run.tasks
            ],
            "groups": [
                [s.index for s in group]
                for group in state.task_runner._group_parallel_tasks(run.tasks, set())
            ],
        }
    )


async def api_taskrunner_plan_cancel(request: web.Request) -> web.Response:
    """POST /api/taskrunner/plan/cancel — cancel running plan decomposition."""
    state: DashboardState = request.app["state"]
    if state.task_runner:
        state.task_runner.cancel_plan()
    return web.json_response({"ok": True})


async def api_taskrunner_update_plan(request: web.Request) -> web.Response:
    """PUT /api/taskrunner/{task_id}/plan — update steps on a planned run."""
    state: DashboardState = request.app["state"]
    if not state.task_runner:
        return web.json_response({"error": "task runner not available"}, status=400)
    task_id = request.match_info["task_id"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    steps = body.get("steps", [])
    try:
        run = await state.task_runner.update_plan(task_id, steps)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    return web.json_response(
        {
            "ok": True,
            "steps": [
                {
                    "index": s.index,
                    "title": redact_credentials(redact_exfiltration_urls(s.title or "")[0])[0],
                    "description": redact_credentials(redact_exfiltration_urls(s.description or "")[0])[0],
                    "depends_on": s.depends_on,
                    "requires_approval": s.requires_approval,
                    "force_approval": s.force_approval,
                }
                for s in run.tasks
            ],
            "groups": [
                [s.index for s in group]
                for group in state.task_runner._group_parallel_tasks(run.tasks, set())
            ],
        }
    )


async def api_taskrunner_execute_plan(request: web.Request) -> web.Response:
    """POST /api/taskrunner/{task_id}/execute — execute a planned run."""
    state: DashboardState = request.app["state"]
    if not state.task_runner:
        return web.json_response({"error": "task runner not available"}, status=400)
    task_id = request.match_info["task_id"]
    try:
        body = await request.json() if request.content_length else {}
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    agent = body.get("agent", "")
    fresh = body.get("fresh", False)
    workspace_dir = body.get("workspace_dir", "")
    # Same provenance gate as /start: an app/proxy-embedded caller cannot mint
    # trust on the resume/execute path either (no source claim here — the run
    # already exists — so only the dashboard-context check applies). The gate
    # contains its own fallible audit write (fails closed to auto_approve=False),
    # so no CWE-755 try/except is needed at the call site — the invariant lives
    # in the gate, not here.
    auto_approve = await _gate_auto_approve(
        request, body.get("auto_approve") is True, None, endpoint="execute"
    )
    try:
        await state.task_runner.execute_plan(task_id, agent=agent, fresh=fresh, workspace_dir=workspace_dir, auto_approve=auto_approve)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    return web.json_response({"ok": True, "task_id": task_id})


async def api_taskrunner_from_chat(request: web.Request) -> web.Response:
    """POST /api/taskrunner/from-chat — create or update a plan from chat-provided steps."""
    from kiro_crew.taskrunner import Project  # noqa: F811

    state: DashboardState = request.app["state"]
    if not state.task_runner:
        return web.json_response({"error": "task runner not available"}, status=400)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    steps = body.get("steps", [])
    task_id = body.get("task_id", "")
    if not steps or not isinstance(steps, list):
        return web.json_response({"error": "steps array required"}, status=400)
    try:
        if task_id:
            run = await state.task_runner.update_plan(task_id, steps)
        else:
            new_id = f"plan_{uuid.uuid4().hex[:8]}"
            task_dir = state.task_runner._work_dir / new_id
            task_dir.mkdir(parents=True, exist_ok=True)
            run = Project(
                spec_path="",
                spec_content="",
                original_input=body.get("original_input", ""),
                source="chat",
                status="planned",
                task_id=new_id,
                work_dir=str(task_dir),
            )
            state.task_runner._runs[new_id] = run
            try:
                run = await state.task_runner.update_plan(new_id, steps)
            except ValueError:
                state.task_runner._runs.pop(new_id, None)
                raise
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    return web.json_response(
        {
            "ok": True,
            "task_id": run.task_id,
            "steps": [
                {
                    "index": s.index,
                    "title": redact_credentials(redact_exfiltration_urls(s.title or "")[0])[0],
                    "description": redact_credentials(redact_exfiltration_urls(s.description or "")[0])[0],
                    "depends_on": s.depends_on,
                    "requires_approval": s.requires_approval,
                    "force_approval": s.force_approval,
                }
                for s in run.tasks
            ],
            "groups": [
                [s.index for s in group]
                for group in state.task_runner._group_parallel_tasks(run.tasks, set())
            ],
        }
    )


_REFINE_PROMPT = (
    "You are a task spec writer. Rewrite the user's request into a clear, structured task specification.\n\n"
    "Output ONLY the spec in this format — no preamble, no commentary:\n\n"
    "# Task: <title>\n\n"
    "## Goal\n<1-2 sentence summary of what needs to be done>\n\n"
    "## Requirements\n- <concrete bullet points — be specific about what, not how>\n\n"
    "## Acceptance Criteria\n- <how to verify it's done>\n\n"
    "Rules:\n"
    "- Be specific and actionable\n"
    "- Preserve all details from the user's request (URLs, file paths, names)\n"
    "- No filler, no tool calls, no questions\n\n"
    "User request:\n{input}"
)


async def _run_refine(state: DashboardState, user_input: str) -> None:
    """Background task: multi-turn LLM refine with tool access and Q&A."""
    import time as _time  # noqa: F811

    from kiro_crew.providers.base import (  # noqa: F811
        EVENT_COMPLETE,
        EVENT_PERMISSION_REQUEST,
        EVENT_TEXT_CHUNK,
    )

    session_key = f"taskrunner:refine:{int(_time.time() * 1000)}"
    _last_push = 0.0
    state._refine_session_key = session_key
    state._refine_answer_future = None  # type: ignore[attr-defined]

    def _push(extra: dict | None = None) -> None:
        safe_text, _ = redact_exfiltration_urls(state._refine_text)
        safe_text, _ = redact_credentials(safe_text)
        d = {"status": "running", "text": safe_text, "error": ""}
        if extra:
            d.update(extra)
        state.broadcast_ws("refine", d)

    try:
        prompt = _REFINE_PROMPT.format(input=user_input)
        state._refine_text = ""
        _push()
        client, _is_new, _resumed = await state.sessions.get_or_create(session_key)

        async for event in client.stream(prompt):
            if event.kind == EVENT_TEXT_CHUNK:
                state._refine_text += event.text
                now = _time.monotonic()
                if now - _last_push > 0.25:
                    _last_push = now
                    _push()
            elif event.kind == EVENT_PERMISSION_REQUEST:
                await client.reject_tool(event.request_id)
            elif event.kind == EVENT_COMPLETE:
                break

        _push()
        state._refine_status = "done"
    except asyncio.CancelledError:
        state._refine_status = "cancelled"
    except Exception as exc:
        logger.exception("Task refine error")
        state._refine_error = str(exc)
        state._refine_status = "error"
    finally:
        state._refine_answer_future = None  # type: ignore[attr-defined]
        state._refine_session_key = ""  # type: ignore[attr-defined]
        try:
            state.sessions.release(session_key)
        except Exception:
            logger.exception("Refine session release failed: %s", session_key)
        try:
            await state.sessions.reset(session_key)
        except Exception:
            logger.exception("Refine session reset failed: %s", session_key)
        state._refine_task = None
        final_text, _ = redact_exfiltration_urls(state._refine_text)
        final_text, _ = redact_credentials(final_text)
        state.broadcast_ws(
            "refine",
            {
                "status": state._refine_status,
                "text": final_text,
                "error": state._refine_error,
            },
        )
        state.push_refresh("taskrunner")


async def api_taskrunner_refine(request: web.Request) -> web.Response:
    """POST /api/taskrunner/refine — start background spec generation from user input."""
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    user_input = body.get("input", "").strip()
    if not user_input:
        return web.json_response({"error": "input is required"}, status=400)

    # Cancel any existing refine
    if state._refine_task and not state._refine_task.done():
        state._refine_task.cancel()

    state._refine_text = ""
    state._refine_error = ""
    state._refine_status = "running"
    state._refine_input = user_input
    task = asyncio.create_task(_run_refine(state, user_input))
    state._refine_task = task
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)
    return web.json_response({"ok": True})


async def api_taskrunner_refine_status(request: web.Request) -> web.Response:
    """GET /api/taskrunner/refine — poll refine progress."""
    state: DashboardState = request.app["state"]
    return web.json_response(
        {
            "status": state._refine_status,
            "text": state._refine_text,
            "error": state._refine_error,
            "input": state._refine_input,
            "waiting": bool(getattr(state, "_refine_answer_future", None)),
        }
    )


async def api_taskrunner_refine_cancel(request: web.Request) -> web.Response:
    """POST /api/taskrunner/refine/cancel — cancel running refine."""
    state: DashboardState = request.app["state"]
    if state._refine_task and not state._refine_task.done():
        state._refine_task.cancel()
    return web.json_response({"ok": True})


async def api_taskrunner_refine_answer(request: web.Request) -> web.Response:
    """POST /api/taskrunner/refine/answer — answer a clarifying question."""
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    answer = body.get("answer", "").strip()
    if not answer:
        return web.json_response({"error": "answer required"}, status=400)
    future = getattr(state, "_refine_answer_future", None)
    if not future or future.done():
        return web.json_response({"error": "no pending question"}, status=409)
    try:
        future.set_result(answer)
    except asyncio.InvalidStateError:
        return web.json_response({"error": "question already resolved"}, status=409)
    return web.json_response({"ok": True})
