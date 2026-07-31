"""Gateway HTTP handlers for dynamic workflows (M6.3/M6.5).

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
"""

from __future__ import annotations

from typing import Any, Optional

from aiohttp import web

from kiro_crew.dashboard.state import DashboardState
from kiro_crew.security import redact_credentials, redact_exfiltration_urls


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


async def api_workflow_author(request: web.Request) -> web.Response:
    """POST /api/workflows/author — NL intent → validated workflow script."""
    svc = _svc(request)
    if svc is None:
        return web.json_response({"error": "workflows not available"}, status=503)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
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
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
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
    """POST /api/workflows/run_intent — launch a run that authors itself (M6.7).

    Returns a run_id IMMEDIATELY; the NL intent is turned into a script inside the
    background run (a visible "Authoring" phase) so the slow model call never
    blocks this request (no 30s synchronous-author timeout).
    """
    svc = _svc(request)
    if svc is None:
        return web.json_response({"error": "workflows not available"}, status=503)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
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


async def api_workflow_run_cancel(request: web.Request) -> web.Response:
    """POST /api/workflows/runs/{id}/cancel — request cancellation."""
    svc = _svc(request)
    if svc is None:
        return web.json_response({"error": "workflows not available"}, status=503)
    run_id = request.match_info.get("run_id", "")
    cancelled = await svc.cancel(run_id)
    return web.json_response({"run_id": run_id, "cancelled": cancelled})


async def api_workflow_run_rerun(request: web.Request) -> web.Response:
    """POST /api/workflows/runs/{id}/rerun — restart, replaying the prefix (M6.6)."""
    svc = _svc(request)
    if svc is None:
        return web.json_response({"error": "workflows not available"}, status=503)
    run_id = request.match_info.get("run_id", "")
    try:
        body = await request.json()
    except Exception:
        body = {}
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
