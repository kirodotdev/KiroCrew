"""Gateway HTTP handlers for dynamic workflows.

These back both the chat ``workflow_*`` MCP tools (which call them with
``X-Internal-Secret``) and the Workflows dashboard tab. All reach the single
shared ``state.workflow_service`` (a ``WorkflowService`` owning the run registry +
runner). LLM-derived strings are redacted before they hit a response surface.

Routes (registered in dashboard/server.py):
  POST /api/workflows/author   {intent}           → {ok, source, meta} | {ok:false, errors}
  POST /api/workflows/run      {source, args?, name?, budget_total?, timeout_secs?}
                                                    → {run_id} | {error}
  GET  /api/workflows/runs                          → [{run_id, name, status, ...}]
  GET  /api/workflows/runs/{id}                     → {…, events:[…]}  (full)
  POST /api/workflows/runs/{id}/cancel              → {cancelled: bool}
  POST /api/workflows/runs/{id}/promote             → save the original completed source
  GET  /api/workflows/definitions                   → reusable global definitions
  POST /api/workflows/definitions                   → explicitly save a definition
  GET/PATCH /api/workflows/definitions/{ref}        → view or append a revision
  POST /api/workflows/definitions/{ref}/run         → run the exact saved revision
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import read_bounded_json
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

_OP_DEFINITION_CREATE = "workflow_definition_create"
_OP_DEFINITION_UPDATE = "workflow_definition_update"
_OP_DEFINITION_RUN = "workflow_definition_run"
_OP_DEFINITION_PROMOTE = "workflow_definition_promote"


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
    return web.json_response(_redact_obj({"definitions": definitions}))


async def api_workflow_definitions_create(request: web.Request) -> web.Response:
    """POST /api/workflows/definitions — explicitly promote a script."""
    denied = _require_dashboard_user(request, _OP_DEFINITION_CREATE)
    if denied is not None:
        return denied
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
    return web.json_response(_redact_obj({"definition": definition}))


async def api_workflow_definition_update(request: web.Request) -> web.Response:
    """PATCH /api/workflows/definitions/{ref} — append a validated revision."""
    denied = _require_dashboard_user(request, _OP_DEFINITION_UPDATE)
    if denied is not None:
        return denied
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
    out = await svc.author(intent, author=author)
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
    out = await svc.start(
        source,
        name=body.get("name", "") or "",
        args=body.get("args") if isinstance(body.get("args"), dict) else {},
        author=request.headers.get("X-Session-Key", ""),
        session_key=request.headers.get("X-Session-Key", ""),
        budget_total=budget_total,
        timeout_secs=_opt_int(body.get("timeout_secs")),
    )
    status = 200 if "run_id" in out else 400
    return web.json_response(_redact_obj(out), status=status)


async def api_workflow_run_intent(request: web.Request) -> web.Response:
    """POST /api/workflows/run_intent — launch a run that authors itself.

    Returns a run_id IMMEDIATELY; the NL intent is turned into a script inside the
    background run (a visible "Authoring" phase) so the slow model call never
    blocks this request (no 30s synchronous-author timeout).
    """
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
    out = await svc.start_from_intent(
        intent,
        name=body.get("name", "") or "",
        args=body.get("args") if isinstance(body.get("args"), dict) else {},
        author=request.headers.get("X-Session-Key", ""),
        session_key=request.headers.get("X-Session-Key", ""),
        budget_total=budget_total,
        timeout_secs=_opt_int(body.get("timeout_secs")),
    )
    status = 200 if "run_id" in out else 400
    return web.json_response(_redact_obj(out), status=status)


async def api_workflow_runs(request: web.Request) -> web.Response:
    """GET /api/workflows/runs — list runs (compact, newest first)."""
    svc = _svc(request)
    if svc is None:
        return web.json_response({"error": "workflows not available"}, status=503)
    return web.json_response(_redact_obj({"runs": svc.list_runs()}))


async def api_workflow_run_get(request: web.Request) -> web.Response:
    """GET /api/workflows/runs/{id} — full run snapshot incl. events."""
    svc = _svc(request)
    if svc is None:
        return web.json_response({"error": "workflows not available"}, status=503)
    run_id = request.match_info.get("run_id", "")
    snap = svc.result(run_id)
    if snap is None:
        return web.json_response({"error": "no such run"}, status=404)
    return web.json_response(_redact_obj(snap))


async def api_workflow_run_promote(request: web.Request) -> web.Response:
    """POST /api/workflows/runs/{id}/promote — save the original completed source."""
    denied = _require_dashboard_user(request, _OP_DEFINITION_PROMOTE)
    if denied is not None:
        return denied
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
    out = await svc.rerun_subtree(run_id, from_index, source=edited_source)
    # 400 on validation error (bad edited script), 404 when the run is missing.
    if "run_id" in out:
        status = 200
    elif out.get("errors"):
        status = 400
    else:
        status = 404
    return web.json_response(_redact_obj(out), status=status)
