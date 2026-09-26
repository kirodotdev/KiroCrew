"""Gateway HTTP handlers for dynamic workflows.

These back both the chat ``workflow_*`` MCP tools (which call them with
``X-Internal-Secret``) and the Workflows dashboard tab. All reach the single
shared ``state.workflow_service`` (a ``WorkflowService`` owning the run registry +
runner). LLM-derived strings are redacted before they hit a response surface.

Routes (registered in dashboard/server.py):
  POST /api/workflows/author   {intent}           → {ok, source, meta} | {ok:false, errors}
  POST /api/workflows/run      {source, args?, name?, budget_total?, timeout_secs?}
                                                    → {run_id} | {error}
  POST /api/workflows/background {command, cwd?, label?, timeout_secs?}
                                                    → {run_id, name, log_path, timeout_secs}
  GET  /api/workflows/runs                          → [{run_id, name, status, ...}]
  GET  /api/workflows/runs/{id}                     → {…, events:[…], plan?}  (full)
                                                      ``?plan=1`` adds ``plan``
  POST /api/workflows/runs/{id}/cancel              → {cancelled: bool}
  POST /api/workflows/runs/{id}/promote             → save the original completed source
  GET  /api/workflows/definitions                   → reusable global definitions
  POST /api/workflows/definitions                   → explicitly save a definition
  GET/PATCH /api/workflows/definitions/{ref}        → view or append a revision
  POST /api/workflows/definitions/{ref}/run         → run the exact saved revision
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Optional

from aiohttp import web

from kiro_crew.dashboard.chat_utils import dashboard_slot_key, effective_session_key
from kiro_crew.dashboard.handlers._shared import internal_memory_scope, read_bounded_json
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.security import is_sensitive_path, redact_credentials, redact_exfiltration_urls
from kiro_crew.workflows.preview import plan_from_source

logger = logging.getLogger(__name__)

_OP_DEFINITION_CREATE = "workflow_definition_create"
_OP_DEFINITION_UPDATE = "workflow_definition_update"
_OP_DEFINITION_RUN = "workflow_definition_run"
_OP_DEFINITION_PROMOTE = "workflow_definition_promote"
_OP_BACKGROUND_RUN = "workflow_background_run"
# How a background command is presented to the tool gate: as the agent's own
# sandboxed shell call, carrying no MCP identity.
_AGENT_SHELL_CALL: dict[str, Any] = {"tool_kind": "execute", "is_shell": True}


def _redact_obj(obj):
    """Recursively redact LLM-derived strings in a JSON-able structure.

    Redacts dict KEYS as well as values: agent output is parsed straight into
    these structures, so a credential can arrive as a mapping key (``{"ghp_…":
    …}``) and a values-only walk would pass it through untouched. Two distinct
    credential-shaped keys can collapse into one redacted key — losing a
    pathological key is strictly preferable to leaking the secret.
    """
    if isinstance(obj, str):
        s, _ = redact_exfiltration_urls(obj)
        s, _ = redact_credentials(s)
        return s
    if isinstance(obj, list):
        return [_redact_obj(x) for x in obj]
    if isinstance(obj, dict):
        return {_redact_obj(k): _redact_obj(v) for k, v in obj.items()}
    return obj


async def _json_response_off_loop(payload: Any, *, status: int = 200) -> web.Response:
    """Redact + serialize ``payload`` on a worker thread, not the event loop.

    ``_redact_obj`` runs two regex redactors over every string in the payload
    and ``json.dumps`` walks it again. For the run and definition views the
    payload scales with stored data (a run detail snapshot with its event
    stream can be MBs; every saved definition carries its script source), so
    doing that work inline stalls every other request on the single-threaded
    loop.

    The loop-side ``json.dumps`` below is the detachment boundary: it runs
    synchronously on the event loop (no await, so no loop-driven mutation can
    interleave) and produces an immutable string. The worker thread then
    rebuilds its own structure from that string, so it only ever touches
    objects it created — it can never observe, or race, live registry state.
    """
    unredacted = json.dumps(payload)

    def _redact_and_serialize() -> str:
        return json.dumps(_redact_obj(json.loads(unredacted)))

    text = await asyncio.to_thread(_redact_and_serialize)
    return web.Response(text=text, status=status, content_type="application/json")


def _svc(request: web.Request):
    state: DashboardState = request.app["state"]
    return getattr(state, "workflow_service", None)


def _sel():
    """Late-bind the shared SEL provider for handler-package import safety."""
    # Circular import: handlers.__init__ re-exports this module, while tests patch
    # the package-level sel seam that must be resolved at call time.
    import kiro_crew.dashboard.handlers as handlers  # noqa: F811

    return handlers.sel()


def _audit_authorization(
    request: web.Request,
    operation: str,
    outcome: str,
    *,
    error: str = "",
) -> None:
    """Best-effort audit for a workflow authorization decision."""
    try:
        _sel().log_api_access(
            caller=str(request.get("app") or request.get("user") or "unknown"),
            operation=operation,
            outcome=outcome,
            source="browser_api",
            resources=request.path,
            error=error,
        )
    except Exception:
        logger.warning("SEL logging failed for %s", operation, exc_info=True)


def _require_dashboard_user(request: web.Request, operation: str) -> Optional[web.Response]:
    """Allow only a positively authenticated dashboard-user request."""
    if request.get("app") == "":
        _audit_authorization(request, operation, "allowed")
        return None
    error = "workflow library mutations require the dashboard user"
    _audit_authorization(request, operation, "denied", error=error)
    return _error("dashboard user required", "dashboard_user_required", 403)


def _reject_app_caller(request: web.Request, operation: str) -> Optional[web.Response]:
    """Reject an app token before trusting its caller-supplied session header."""
    if not request.get("app"):
        _audit_authorization(request, operation, "allowed")
        return None
    error = "app tokens cannot start session-bound saved workflows"
    _audit_authorization(request, operation, "denied", error=error)
    return _error("dashboard user required", "dashboard_user_required", 403)


def _error(message: str, code: str, status: int) -> web.Response:
    if status == 400:
        return web.json_response({"error": message, "code": code}, status=400)
    if status == 403:
        return web.json_response({"error": message, "code": code}, status=403)
    if status == 404:
        return web.json_response({"error": message, "code": code}, status=404)
    if status == 409:
        return web.json_response({"error": message, "code": code}, status=409)
    if status == 500:
        return web.json_response({"error": message, "code": code}, status=500)
    if status == 503:
        return web.json_response({"error": message, "code": code}, status=503)
    raise ValueError(f"unsupported workflow error status: {status}")


async def _private_memory_refusal(request: web.Request, operation: str) -> web.Response | None:
    """Authenticate the caller before a workflow can lose its private binding."""
    _store, refusal = await internal_memory_scope(
        request, operation, claimed_session=request.headers.get("X-Session-Key", "")
    )
    if refusal is not None:
        return refusal
    if request.get("app"):
        return _error("dashboard user required", "dashboard_user_required", 403)
    request["workflow_expected_store"] = (
        (_store or "") if request.get("internal_auth") is True else None
    )
    return None


async def _run_scope_refusal(
    request: web.Request, run_id: str, *, cancelling: bool = False
) -> web.Response | None:
    from kiro_crew.workflow_memory import WorkflowMemoryError, authorize_run

    refusal = await _private_memory_refusal(request, "workflow.access")
    if refusal is not None:
        return refusal
    svc = _svc(request)
    registry = getattr(svc, "registry", None)
    handle = registry.get(run_id) if registry is not None else None
    try:
        await authorize_run(
            run_id,
            request.headers.get("X-Session-Key", ""),
            owner=request.get("app") == "" and request.get("internal_auth") is not True,
            required=bool(handle is not None and handle.execution_binding_version),
            record=handle.to_store_json() if handle is not None else None,
            require_active=not cancelling,
        )
    except (WorkflowMemoryError, ValueError):
        return _error("Workflow memory access refused", "workflow_memory_unavailable", 403)
    return None


def _lineage(value: Any) -> Optional[dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    workflow_id = value.get("workflow_id")
    revision = value.get("revision")
    if not isinstance(workflow_id, str) or not workflow_id or not isinstance(revision, int):
        return None
    return {"workflow_id": workflow_id, "revision": revision}


async def api_workflow_definitions(request: web.Request) -> web.Response:
    """GET /api/workflows/definitions — list or locally search saved workflows."""
    svc = _svc(request)
    if svc is None:
        return _error("workflows not available", "workflows_unavailable", 503)
    search = (request.query.get("q") or "").strip()
    try:
        definitions = await asyncio.to_thread(svc.list_definitions, search)
    except Exception:
        logger.exception("workflow definition list failed")
        return _error("could not read saved workflows", "workflow_definition_read_failed", 500)
    # Every saved definition carries its full script source, so this payload
    # scales with the library — serialize it off-loop like the run views.
    return await _json_response_off_loop({"definitions": definitions})


async def api_workflow_definitions_create(request: web.Request) -> web.Response:
    """POST /api/workflows/definitions — explicitly promote a script."""
    denied = _require_dashboard_user(request, _OP_DEFINITION_CREATE)
    if denied is not None:
        return denied
    # Body-scope import, like the sibling gates in this package
    # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
    # reaches back into sibling handler modules, so importing the helper at
    # module scope from here would close a cycle.
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    owner_denied = await require_owner_dashboard_request(request, "workflow_definition_create")
    if owner_denied is not None:
        return owner_denied
    svc = _svc(request)
    if svc is None:
        return _error("workflows not available", "workflows_unavailable", 503)
    body, body_err = await read_bounded_json(request, max_bytes=None)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    source = body.get("source")
    if not isinstance(source, str) or not source.strip():
        return _error("source is required", "workflow_source_required", 400)
    lineage_value = body.get("derived_from")
    derived_from = _lineage(lineage_value)
    if lineage_value is not None and derived_from is None:
        return _error("derived_from is invalid", "workflow_lineage_invalid", 400)
    lineage_kwargs = {"derived_from": derived_from} if "derived_from" in body else {}
    source_format = body.get("format", "python")
    if source_format not in ("python", "task-plan"):
        return _error("format is invalid", "workflow_format_invalid", 400)
    try:
        out = await asyncio.to_thread(
            svc.save_definition,
            source,
            name=body.get("name", "") if isinstance(body.get("name"), str) else "",
            description=(
                body.get("description", "") if isinstance(body.get("description"), str) else ""
            ),
            slug=body.get("slug", "") if isinstance(body.get("slug"), str) else "",
            source_format=source_format,
            **lineage_kwargs,
        )
    except Exception:
        logger.exception("workflow definition save failed")
        return _error("could not save workflow", "workflow_definition_write_failed", 500)
    if out.get("ok"):
        return web.json_response(_redact_obj(out), status=201)
    return web.json_response(
        {
            "error": _redact_obj(out.get("error") or "invalid workflow"),
            "errors": _redact_obj(out.get("errors") or []),
            "code": "workflow_definition_invalid",
        },
        status=400,
    )


async def api_workflow_definition_get(request: web.Request) -> web.Response:
    """GET /api/workflows/definitions/{ref} — resolve by id or slug."""
    svc = _svc(request)
    if svc is None:
        return _error("workflows not available", "workflows_unavailable", 503)
    workflow_ref = request.match_info.get("workflow_ref", "")
    try:
        definition = await asyncio.to_thread(svc.get_definition, workflow_ref)
    except Exception:
        logger.exception("workflow definition read failed")
        return _error("could not read saved workflow", "workflow_definition_read_failed", 500)
    if definition is None:
        return _error("no such saved workflow", "workflow_definition_not_found", 404)
    return await _json_response_off_loop({"definition": definition})


async def api_workflow_definition_update(request: web.Request) -> web.Response:
    """PATCH /api/workflows/definitions/{ref} — append a validated revision."""
    denied = _require_dashboard_user(request, _OP_DEFINITION_UPDATE)
    if denied is not None:
        return denied
    # Body-scope import, like the sibling gates in this package
    # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
    # reaches back into sibling handler modules, so importing the helper at
    # module scope from here would close a cycle.
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    owner_denied = await require_owner_dashboard_request(request, "workflow_definition_update")
    if owner_denied is not None:
        return owner_denied
    svc = _svc(request)
    if svc is None:
        return _error("workflows not available", "workflows_unavailable", 503)
    body, body_err = await read_bounded_json(request, max_bytes=None)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    source = body.get("source")
    expected_revision = body.get("expected_revision")
    if not isinstance(source, str) or not source.strip():
        return _error("source is required", "workflow_source_required", 400)
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
        return _error("expected_revision is required", "workflow_revision_required", 400)
    optional_text = {
        key: body[key] for key in ("name", "description", "slug") if isinstance(body.get(key), str)
    }
    try:
        out = await asyncio.to_thread(
            svc.update_definition,
            request.match_info.get("workflow_ref", ""),
            source=source,
            expected_revision=expected_revision,
            **optional_text,
        )
    except Exception:
        logger.exception("workflow definition update failed")
        return _error("could not update workflow", "workflow_definition_write_failed", 500)
    if out.get("ok"):
        return web.json_response(_redact_obj(out))
    if out.get("not_found"):
        return _error(
            _redact_obj(out.get("error") or "no such saved workflow"),
            "workflow_definition_not_found",
            404,
        )
    if out.get("conflict"):
        return web.json_response(
            {
                "error": _redact_obj(out.get("error") or "workflow revision conflict"),
                "code": "workflow_definition_conflict",
            },
            status=409,
        )
    return web.json_response(
        {
            "error": _redact_obj(out.get("error") or "invalid workflow"),
            "errors": _redact_obj(out.get("errors") or []),
            "code": "workflow_definition_invalid",
        },
        status=400,
    )


async def api_workflow_definition_run(request: web.Request) -> web.Response:
    """POST /api/workflows/definitions/{ref}/run — execute the exact saved source."""
    denied = _reject_app_caller(request, _OP_DEFINITION_RUN)
    if denied is not None:
        return denied
    # Owner identity is a property of a dashboard-user request: ``app == ""`` is
    # the class ``is_owner_dashboard_request`` can rule on at all. The other two
    # caller classes keep the control that already governs them -- an
    # ``X-Internal-Secret`` loopback process is admitted by the constant-time
    # secret match and reaches here with ``app`` ABSENT, and an app token is
    # confined to its manifest's declared paths by ``_enforce_app_scope``.
    if request.get("internal_auth") is not True and request.get("app") == "":
        # Body-scope import, like the sibling gates in this package
        # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
        # reaches back into sibling handler modules, so importing the helper at
        # module scope from here would close a cycle.
        from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

        owner_denied = await require_owner_dashboard_request(request, "workflow_definition_run")
        if owner_denied is not None:
            return owner_denied
    svc = _svc(request)
    if svc is None:
        return _error("workflows not available", "workflows_unavailable", 503)
    body, body_err = await read_bounded_json(request, max_bytes=None)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    input_text = body.get("input", "")
    if not isinstance(input_text, str):
        return _error("input must be a string", "workflow_input_invalid", 400)
    session_key = request.headers.get("X-Session-Key", "")
    refusal = await _private_memory_refusal(request, _OP_DEFINITION_RUN)
    if refusal is not None:
        return refusal
    budget_total = body.get("budget_total")
    if isinstance(budget_total, bool) or not isinstance(budget_total, int):
        budget_total = None
    try:
        out = await svc.start_definition(
            request.match_info.get("workflow_ref", ""),
            input_text=input_text,
            args=body.get("args") if isinstance(body.get("args"), dict) else {},
            author=session_key,
            session_key=session_key,
            expected_store=request.get("workflow_expected_store"),
            budget_total=budget_total,
            timeout_secs=_opt_int(body.get("timeout_secs")),
        )
    except Exception:
        logger.exception("saved workflow start failed")
        return _error("could not start saved workflow", "workflow_definition_start_failed", 500)
    if "run_id" in out:
        return web.json_response(_redact_obj(out))
    error = _redact_obj(out.get("error") or "could not start saved workflow")
    if out.get("not_found"):
        return _error(error, "workflow_definition_not_found", 404)
    if out.get("unavailable"):
        return _error(error, "workflow_executor_unavailable", 503)
    return web.json_response(
        {
            "error": error,
            "code": "workflow_definition_start_rejected",
        },
        status=409,
    )


async def api_workflow_author(request: web.Request) -> web.Response:
    """POST /api/workflows/author — NL intent → validated workflow script."""
    svc = _svc(request)
    if svc is None:
        return web.json_response({"error": "workflows not available"}, status=503)
    body, body_err = await read_bounded_json(request, max_bytes=None)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    intent = (body.get("intent") or "").strip()
    if not intent:
        return web.json_response({"error": "intent is required"}, status=400)
    author = request.headers.get("X-Session-Key", "")
    refusal = await _private_memory_refusal(request, "workflow.author")
    if refusal is not None:
        return refusal
    out = await svc.author(
        intent, author=author, expected_store=request.get("workflow_expected_store")
    )
    return web.json_response(_redact_obj(out))


def _opt_int(value: Any) -> Optional[int]:
    """Coerce an optional integer body field; anything unusable → None.

    None means "no override" at the service layer, which then applies its own
    default — so a malformed value can never widen or remove a run's ceiling.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


async def api_workflow_run(request: web.Request) -> web.Response:
    """POST /api/workflows/run — launch a background run, return its run_id."""
    # Owner identity is a property of a dashboard-user request: ``app == ""`` is
    # the class ``is_owner_dashboard_request`` can rule on at all. The other two
    # caller classes keep the control that already governs them -- an
    # ``X-Internal-Secret`` loopback process is admitted by the constant-time
    # secret match and reaches here with ``app`` ABSENT, and an app token is
    # confined to its manifest's declared paths by ``_enforce_app_scope``.
    if request.get("internal_auth") is not True and request.get("app") == "":
        # Body-scope import, like the sibling gates in this package
        # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
        # reaches back into sibling handler modules, so importing the helper at
        # module scope from here would close a cycle.
        from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

        owner_denied = await require_owner_dashboard_request(request, "workflow.run")
        if owner_denied is not None:
            return owner_denied
    svc = _svc(request)
    if svc is None:
        return web.json_response({"error": "workflows not available"}, status=503)
    body, body_err = await read_bounded_json(request, max_bytes=None)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    source = body.get("source", "")
    if not isinstance(source, str) or not source.strip():
        return web.json_response({"error": "source is required"}, status=400)
    budget_total = body.get("budget_total")
    if not isinstance(budget_total, int):
        budget_total = None
    refusal = await _private_memory_refusal(request, "workflow.run")
    if refusal is not None:
        return refusal
    out = await svc.start(
        source,
        name=body.get("name", "") or "",
        args=body.get("args") if isinstance(body.get("args"), dict) else {},
        author=request.headers.get("X-Session-Key", ""),
        session_key=request.headers.get("X-Session-Key", ""),
        expected_store=request.get("workflow_expected_store"),
        budget_total=budget_total,
        timeout_secs=_opt_int(body.get("timeout_secs")),
    )
    status = 200 if "run_id" in out else 400
    return web.json_response(_redact_obj(out), status=status)


def _background_cwd(raw: Any, slot: Any) -> tuple[str, Optional[str]]:
    """Resolve the command's working directory; relative paths join the chat's project."""
    base = Path(getattr(slot, "project", "") or Path.home()).expanduser()
    if raw is None or raw == "":
        candidate = base
    elif isinstance(raw, str):
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = base / candidate
    else:
        return "", "cwd must be a string"
    resolved = candidate.resolve()
    if not resolved.is_dir():
        return "", f"cwd is not a directory: {resolved}"
    if is_sensitive_path(str(resolved)):
        return "", "cwd is a protected location"
    return str(resolved), None


def _background_command_denial(state: DashboardState, slot: Any, command: str) -> Optional[str]:
    """Judge ``command`` with the same gate that judges the agent's own shell calls.

    The ACP permission path sees only the MCP tool call, never the command inside
    it, so the shell tiers (deny floor, sensitive paths, exfiltration, governance
    ``commands``) run here instead. A deny refuses; anything else only means the
    command passed the floor, because approval already happened on the tool call.
    """
    from types import SimpleNamespace

    from kiro_crew.hooks import TOOL_DENY, hook_gate_kwargs

    hooks = getattr(getattr(state, "context_builder", None), "hooks", None)
    if hooks is None:
        return "the tool gate is unavailable"
    shell_call = SimpleNamespace(
        raw_tool_params={"command": command}, shell_command=command, **_AGENT_SHELL_CALL
    )
    verdict = hooks.on_tool_call(
        "execute_bash",
        session_key=effective_session_key(slot),
        agent=getattr(slot, "agent", "") or "",
        app=getattr(slot, "_app", "") or "",
        **hook_gate_kwargs(shell_call),
    )
    if verdict.action == TOOL_DENY:
        return (verdict.reason or "blocked").strip()
    return None


async def api_workflow_background_run(request: web.Request) -> web.Response:
    """POST /api/workflows/background — start a gateway-owned background command."""
    from kiro_crew.background_commands import MAX_COMMAND_CHARS, BackgroundCommandError

    if request.get("internal_auth") is not True:
        _audit_authorization(
            request, _OP_BACKGROUND_RUN, "denied", error="not an internal tool call"
        )
        return _error(
            "background commands start only from a chat session's tool call",
            "background_run_internal_only",
            403,
        )
    state: DashboardState = request.app["state"]
    service = getattr(state, "background_commands", None)
    if _svc(request) is None or service is None:
        return _error("background commands are not available", "background_run_unavailable", 503)
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    command = body.get("command")
    if not isinstance(command, str) or not command.strip():
        return _error("command is required", "background_run_command_required", 400)
    if len(command) > MAX_COMMAND_CHARS or "\x00" in command:
        return _error(
            f"command must be at most {MAX_COMMAND_CHARS} characters with no NUL byte",
            "background_run_command_invalid",
            400,
        )
    label = body.get("label", "")
    if not isinstance(label, str):
        return _error("label must be a string", "background_run_label_invalid", 400)
    refusal = await _private_memory_refusal(request, _OP_BACKGROUND_RUN)
    if refusal is not None:
        return refusal
    session_key = request.headers.get("X-Session-Key", "")
    slot = state.get_slot(dashboard_slot_key(session_key)) if session_key else None
    if slot is None or effective_session_key(slot) != session_key:
        return _error(
            "background_run needs an open dashboard chat to deliver its result to",
            "background_run_no_session",
            409,
        )
    denial = _background_command_denial(state, slot, command)
    if denial is not None:
        _audit_authorization(request, _OP_BACKGROUND_RUN, "denied", error=denial)
        return _error(
            f"Blocked by security policy: {_redact_obj(denial)}", "background_run_denied", 403
        )
    cwd, cwd_error = await asyncio.to_thread(_background_cwd, body.get("cwd"), slot)
    if cwd_error is not None:
        return _error(_redact_obj(cwd_error), "background_run_cwd_invalid", 400)
    try:
        started = await service.start(
            session_key=session_key,
            command=command,
            cwd=cwd,
            label=label,
            timeout_secs=_opt_int(body.get("timeout_secs")),
            expected_store=request.get("workflow_expected_store"),
        )
    except BackgroundCommandError as exc:
        return _error(_redact_obj(str(exc)), exc.code, exc.status)
    _audit_authorization(request, _OP_BACKGROUND_RUN, "allowed")
    return web.json_response(_redact_obj(started))


async def api_workflow_run_intent(request: web.Request) -> web.Response:
    """POST /api/workflows/run_intent — launch a run that authors itself.

    Returns a run_id IMMEDIATELY; the NL intent is turned into a script inside the
    background run (a visible "Authoring" phase) so the slow model call never
    blocks this request (no 30s synchronous-author timeout).
    """
    # Owner identity is a property of a dashboard-user request: ``app == ""`` is
    # the class ``is_owner_dashboard_request`` can rule on at all. The other two
    # caller classes keep the control that already governs them -- an
    # ``X-Internal-Secret`` loopback process is admitted by the constant-time
    # secret match and reaches here with ``app`` ABSENT, and an app token is
    # confined to its manifest's declared paths by ``_enforce_app_scope``.
    if request.get("internal_auth") is not True and request.get("app") == "":
        # Body-scope import, like the sibling gates in this package
        # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
        # reaches back into sibling handler modules, so importing the helper at
        # module scope from here would close a cycle.
        from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

        owner_denied = await require_owner_dashboard_request(request, "workflow.run_intent")
        if owner_denied is not None:
            return owner_denied
    svc = _svc(request)
    if svc is None:
        return web.json_response({"error": "workflows not available"}, status=503)
    body, body_err = await read_bounded_json(request, max_bytes=None)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    intent = (body.get("intent") or "").strip()
    if not intent:
        return web.json_response({"error": "intent is required"}, status=400)
    budget_total = body.get("budget_total")
    if not isinstance(budget_total, int):
        budget_total = None
    refusal = await _private_memory_refusal(request, "workflow.run_intent")
    if refusal is not None:
        return refusal
    out = await svc.start_from_intent(
        intent,
        name=body.get("name", "") or "",
        args=body.get("args") if isinstance(body.get("args"), dict) else {},
        author=request.headers.get("X-Session-Key", ""),
        session_key=request.headers.get("X-Session-Key", ""),
        expected_store=request.get("workflow_expected_store"),
        budget_total=budget_total,
        timeout_secs=_opt_int(body.get("timeout_secs")),
    )
    status = 200 if "run_id" in out else 400
    return web.json_response(_redact_obj(out), status=status)


async def api_workflow_runs(request: web.Request) -> web.Response:
    """GET /api/workflows/runs — list runs (compact, newest first).

    Compact means no event bodies, no source, and no result payloads — the
    detail endpoint (``GET /api/workflows/runs/{id}``) carries the result.
    """
    svc = _svc(request)
    if svc is None:
        return web.json_response({"error": "workflows not available"}, status=503)
    refusal = await _private_memory_refusal(request, "workflow.list")
    if refusal is not None:
        return refusal
    runs = []
    for snapshot in svc.list_runs():
        if await _run_scope_refusal(request, snapshot["run_id"]) is None:
            runs.append(snapshot)
    return await _json_response_off_loop({"runs": runs})


async def api_workflow_run_get(request: web.Request) -> web.Response:
    """GET /api/workflows/runs/{id} — full run snapshot incl. events."""
    svc = _svc(request)
    if svc is None:
        return web.json_response({"error": "workflows not available"}, status=503)
    run_id = request.match_info.get("run_id", "")
    refusal = await _run_scope_refusal(request, run_id)
    if refusal is not None:
        return refusal
    snap = svc.result(run_id)
    if snap is None:
        return web.json_response({"error": "no such run"}, status=404)
    # Predicting the shape means parsing the script, and only Graph mode draws it. The
    # tree view polls this endpoint every couple of seconds, so the parse is opt-in
    # rather than something every poll pays for.
    if request.query.get("plan") in ("1", "true", "yes"):
        snap = await _with_plan(snap)
    return await _json_response_off_loop(snap)


async def _with_plan(snap: dict[str, Any]) -> dict[str, Any]:
    """``snap`` plus a ``plan``: what the script SAYS it will do, if that is readable.

    Asked for by ``?plan=1``. The graph view draws the plan behind the run, so it is
    derived here rather than in the browser — predicting the shape means parsing
    Python, and the source is already on this endpoint. ``plan`` is ABSENT, never null,
    when the source yields none (no ``workflow(ctx)`` entrypoint, unparseable, or a
    task-plan source), so the UI can tell "no plan" from "an empty plan".

    A copy is returned; ``snap`` belongs to the live registry and is not mutated. The
    parse runs on a worker thread for the same reason the serialization below does: a
    script may be up to ``MAX_SCRIPT_BYTES``, and nothing that scales with stored data
    belongs on the single-threaded loop.
    """
    source = snap.get("source")
    if not isinstance(source, str) or not source.strip():
        return snap
    try:
        plan = await asyncio.to_thread(plan_from_source, source)
    except Exception:
        # A preview is decoration on a snapshot the caller asked for. Nothing about a
        # failure to predict the shape justifies failing the run's own record.
        logger.exception("workflow plan preview failed for run %s", snap.get("run_id"))
        return snap
    if plan is None:
        return snap
    return {**snap, "plan": plan}


async def api_workflow_run_promote(request: web.Request) -> web.Response:
    """POST /api/workflows/runs/{id}/promote — save the original completed source."""
    denied = _require_dashboard_user(request, _OP_DEFINITION_PROMOTE)
    if denied is not None:
        return denied
    # Body-scope import, like the sibling gates in this package
    # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
    # reaches back into sibling handler modules, so importing the helper at
    # module scope from here would close a cycle.
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    owner_denied = await require_owner_dashboard_request(request, "workflow_definition_promote")
    if owner_denied is not None:
        return owner_denied
    svc = _svc(request)
    if svc is None:
        return _error("workflows not available", "workflows_unavailable", 503)
    # Default cap: the body is fixed metadata fields (name, description, slug);
    # the promoted source comes from the completed run, not this request.
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    run_id = request.match_info.get("run_id", "")
    try:
        out = await svc.promote_run_definition(
            run_id,
            name=body.get("name", "") if isinstance(body.get("name"), str) else "",
            description=(
                body.get("description", "") if isinstance(body.get("description"), str) else ""
            ),
            slug=body.get("slug", "") if isinstance(body.get("slug"), str) else "",
        )
    except Exception:
        logger.exception("workflow run promotion failed")
        return _error("could not save workflow", "workflow_definition_write_failed", 500)
    if out.get("ok"):
        return web.json_response(_redact_obj(out), status=201)
    if out.get("not_found"):
        return _error("no such workflow run", "workflow_run_not_found", 404)
    if out.get("not_finished"):
        return _error("workflow run is not finished", "workflow_run_not_finished", 409)
    if out.get("source_not_original"):
        return _error(
            "original workflow source is no longer available",
            "workflow_run_source_not_original",
            409,
        )
    return web.json_response(
        {
            "error": _redact_obj(out.get("error") or "invalid workflow"),
            "errors": _redact_obj(out.get("errors") or []),
            "code": "workflow_definition_invalid",
        },
        status=400,
    )


async def api_workflow_run_cancel(request: web.Request) -> web.Response:
    """POST /api/workflows/runs/{id}/cancel — request cancellation."""
    svc = _svc(request)
    if svc is None:
        return web.json_response({"error": "workflows not available"}, status=503)
    run_id = request.match_info.get("run_id", "")
    refusal = await _run_scope_refusal(request, run_id, cancelling=True)
    if refusal is not None:
        return refusal
    cancelled = await svc.cancel(run_id)
    return web.json_response({"run_id": run_id, "cancelled": cancelled})


async def api_workflow_run_rerun(request: web.Request) -> web.Response:
    """POST /api/workflows/runs/{id}/rerun — restart, replaying the prefix."""
    svc = _svc(request)
    if svc is None:
        return web.json_response({"error": "workflows not available"}, status=503)
    run_id = request.match_info.get("run_id", "")
    # allow_absent: every field defaults, so a bodyless rerun replays from index 0.
    body, body_err = await read_bounded_json(request, max_bytes=None, allow_absent=True)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    from_index = body.get("from_index", 0)
    if not isinstance(from_index, int):
        from_index = 0
    # Optional edited script: review → tweak → rerun with the edited source.
    edited_source = body.get("source")
    if not isinstance(edited_source, str):
        edited_source = None
    refusal = await _run_scope_refusal(request, run_id)
    if refusal is not None:
        return refusal
    out = await svc.rerun_subtree(
        run_id,
        from_index,
        source=edited_source,
        caller_session=request.headers.get("X-Session-Key", ""),
        owner=request.get("app") == "" and request.get("internal_auth") is not True,
    )
    # 400 on validation error (bad edited script), 404 when the run is missing.
    if "run_id" in out:
        status = 200
    elif out.get("errors"):
        status = 400
    else:
        status = 404
    return web.json_response(_redact_obj(out), status=status)
