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
import math
from typing import Any, Awaitable, Callable, Optional

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import internal_memory_scope, read_bounded_json
from kiro_crew.dashboard.request_priority import owner_start_priority
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.workflows.preview import plan_from_source

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

    A non-finite float (``inf``, ``-inf``, ``nan``) becomes ``None``: Python's
    ``json`` writes it as a bare ``Infinity``/``NaN`` token, which no browser
    ``JSON.parse`` accepts. A script reaches one through ``ctx.budget.remaining()``
    on a run without a budget.

    A tuple is walked like a list and comes back as one. The live
    ``workflow_run_event`` broadcast passes a run's raw result through here, so
    a script that returns a tuple holding ``inf`` would otherwise reach
    ``json.dumps`` untouched.
    """
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, str):
        s, _ = redact_exfiltration_urls(obj)
        s, _ = redact_credentials(s)
        return s
    if isinstance(obj, (list, tuple)):
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


def _workflow_worker_keys(run_id: str) -> tuple[str, ...]:
    """The keys a run's worker sessions sit under (``wf:<run>:<n>`` and its kin).

    Bound to the run's admitted budget at attach, so a worker's ``spawn_run`` is
    charged to the person who started the workflow (``AdmissionService._binding``).
    """
    return tuple(f"{prefix}:{run_id}" for prefix in ("wf", "wf-pool", "wf-unpooled", "wf-worker"))


def _svc(request: web.Request):
    state: DashboardState = request.app["state"]
    return getattr(state, "workflow_service", None)


async def _resolve_fallback_folder_id(
    request: web.Request, ref: str
) -> tuple[str, Optional[web.Response]]:
    """Resolve a fallback-slot folder reference to an EXISTING folder id.

    ``ref`` is the opt-in folder for the ``workflow-<run_id>`` fallback session:
    a sidebar folder id or a ``/``-separated human path, like
    ``session_create``'s ``folder``. It must name a folder that ALREADY exists —
    nothing is created here. The run owns no identity under which to create a
    folder in the person's sidebar, so a reference that does not resolve is a
    caller-visible error, not a silent create or a silent drop.

    Resolution goes through the ONE canonical read-only resolver
    ``_resolve_chat_folder_id`` — imported here through the MCP facade
    ``kiro_crew.mcp_dashboard`` (which re-exports it), NOT from the owner module
    directly, so the facade-family boundary stays intact
    (``test_no_module_outside_the_family_imports_an_owner``). It accepts an id or
    a human path, refuses an ambiguous path (two folders rendering the same
    path, or a name that is both a folder and a nested path) with a message
    naming the candidates, and never builds a client or writes. The import is
    lazy (inside the function) to avoid an import cycle at module load. An empty
    ref (or ``root``) means "do not file" — ``("", None)`` — today's default.

    Returns ``(folder_id, None)`` on success, or ``("", response)`` with a ready
    400 the caller returns as-is when the folder is missing or ambiguous.
    """
    from kiro_crew.mcp_dashboard import _resolve_chat_folder_id

    ref = (ref or "").strip()
    if not ref or ref.lower() == "root":
        # No folder given (the common case): do not file, and do NOT touch
        # request state — a caller/test that passes no folder need not carry it.
        return "", None
    # A crew-member caller may not aim the fallback slot at the PERSON's sidebar
    # tree. ``_resolve_chat_folder_id`` below walks the whole unfiltered tree,
    # while ``api_chat_folders`` scopes a member's view to the folders it owns
    # plus its own home chain; resolving the raw ref here would let a member
    # probe the person's layout (existence vs 400, an ambiguous path's error
    # names ids) and file into a folder its read scope hides. There is no
    # reusable member-scoped filter to resolve against (``api_chat_folders``
    # builds its view inline), so the smallest fail-safe is to refuse any
    # non-empty folder for a member caller BEFORE resolving it, with one uniform
    # 400 whose text does not depend on whether the folder exists — nothing is
    # learned and nothing reaches the person's tree. ``workflow_expected_store``
    # is the verified member store ``_private_memory_refusal`` already read (set
    # non-empty only for an internal-auth member caller, ``None`` for the owner),
    # so the person's own calls are unchanged.
    if request.get("workflow_expected_store"):
        return "", web.json_response(
            {"error": "folder is not available for this caller", "code": "folder_unresolvable"},
            status=400,
        )
    state: DashboardState = request.app["state"]
    folder_id, err = _resolve_chat_folder_id(ref, state._folders)
    if err:
        return "", web.json_response({"error": err, "code": "folder_unresolvable"}, status=400)
    if not folder_id:
        return "", web.json_response(
            {"error": f"folder not found: {ref}", "code": "folder_unresolvable"},
            status=400,
        )
    # A mate's home folder (and everything inside it) holds only that mate's own
    # sessions. A fallback ``workflow-<run_id>`` slot may run on any store, so it
    # must never be filed there — the same refusal ``slot_create`` makes at a
    # chat's birth (``home_owner_of`` → ``mate_home_locked`` 403). The run's own
    # store is not resolved until admission (it is captured inside the service),
    # so this refuses on the one principal the handler already has: a mate's home
    # is refused for every caller, which is the fail-safe direction. It may turn
    # away the rare legitimate case of a run bound to that mate's store, but it
    # can never file into a home it should not; delivery re-checks against the
    # run's real store. The 403 body matches ``slot_create._mate_home_locked_response``
    # exactly; it is built here through this module's ``_error`` rather than
    # imported, because that helper is a private chat_api owner name the facade
    # boundary keeps behind ``chat_handlers``.
    from kiro_crew.dashboard.mate_folders import home_owner_of

    if home_owner_of(state._folders, folder_id):
        return "", _error(
            "this folder belongs to a mate and is managed for you", "mate_home_locked", 403
        )
    return folder_id, None


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
    exact_slug = body.get("exact_slug", False)
    if not isinstance(exact_slug, bool):
        return _error("exact_slug must be a boolean", "workflow_exact_slug_invalid", 400)
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
            exact_slug=exact_slug,
            **lineage_kwargs,
        )
    except Exception:
        logger.exception("workflow definition save failed")
        return _error("could not save workflow", "workflow_definition_write_failed", 500)
    if out.get("ok"):
        return web.json_response(_redact_obj(out), status=201)
    if out.get("slug_taken"):
        return _error("that command is already a saved workflow", "workflow_slug_taken", 409)
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
    folder_id, folder_refusal = await _resolve_fallback_folder_id(
        request, str(body.get("folder") or "")
    )
    if folder_refusal is not None:
        return folder_refusal
    try:
        out = await _start_admitted(
            request,
            svc,
            ("definition_run", request.match_info.get("workflow_ref", ""), body),
            lambda: svc.start_definition(
                request.match_info.get("workflow_ref", ""),
                input_text=input_text,
                args=body.get("args") if isinstance(body.get("args"), dict) else {},
                author=session_key,
                session_key=session_key,
                expected_store=request.get("workflow_expected_store"),
                budget_total=budget_total,
                timeout_secs=_opt_int(body.get("timeout_secs")),
                fallback_title=str(body.get("title") or ""),
                fallback_folder_id=folder_id,
            ),
        )
    except Exception:
        logger.exception("saved workflow start failed")
        return _error("could not start saved workflow", "workflow_definition_start_failed", 500)
    if isinstance(out, web.Response):
        return out
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
    intent = body.get("intent") or ""
    if not isinstance(intent, str):
        return _error("intent must be a string", "invalid_field_type", 400)
    intent = intent.strip()
    if not intent:
        return web.json_response({"error": "intent is required"}, status=400)
    author = request.headers.get("X-Session-Key", "")
    refusal = await _private_memory_refusal(request, "workflow.author")
    if refusal is not None:
        return refusal
    try:
        out = await svc.author(
            intent,
            author=author,
            expected_store=request.get("workflow_expected_store"),
            # The dashboard owner waits on the Create-draft spinner; an app token or an
            # agent's internal call does not (kiro_crew.start_priority).
            start_priority=owner_start_priority(request),
        )
    except Exception:
        logger.exception("workflow author failed")
        return _error("could not author workflow", "workflow_author_failed", 500)
    return web.json_response(_redact_obj(out))


def _opt_int(value: Any) -> Optional[int]:
    """Coerce an optional integer body field; anything unusable → None.

    None means "no override" at the service layer, which then applies its own
    default — so a malformed value can never widen or remove a run's ceiling.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


async def _admit_workflow_run(request: web.Request, svc: Any, body: object) -> Any:
    """Fair admission (Shared Crew) for a workflow run: it starts now or is refused."""
    from kiro_crew.shared_crew.admission import OperationKind
    from kiro_crew.shared_crew.admission_entry import admit_background_run, workflow_probe

    return await admit_background_run(
        request,
        operation=OperationKind.WORKFLOW,
        session_key=request.headers.get("X-Session-Key", "") or "workflow",
        probe=workflow_probe(svc),
        cancel=getattr(svc, "cancel", None),
        body=body,
    )


async def _start_admitted(
    request: web.Request,
    svc: Any,
    body: object,
    start: Callable[[], Awaitable[dict[str, Any]]],
) -> dict[str, Any] | web.Response:
    """Start a workflow run under fair admission (Shared Crew): now, or refused.

    Returns the refusal response, or the start's own result. An admitted start
    that produced no run gives its slot back.
    """
    from kiro_crew.shared_crew.containment import principal_scope

    run_admission = await _admit_workflow_run(request, svc, body)
    if run_admission.rejected:
        return run_admission.refusal_response()
    if run_admission.replayed:
        from kiro_crew.shared_crew.admission_entry import background_replay_response

        return background_replay_response(run_admission)
    try:
        with principal_scope(None if run_admission.work is None else run_admission.work.budget_key):
            out = await start()
    except BaseException:
        await run_admission.abandon()
        raise
    if "run_id" in out:
        await run_admission.attach_child(
            str(out["run_id"]), also=_workflow_worker_keys(str(out["run_id"]))
        )
    else:
        await run_admission.abandon()
    return out


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
    # Resolve the opt-in fallback folder to an EXISTING folder id BEFORE
    # admission reserves capacity, so an unresolvable folder returns 400 without
    # leaving an ADMITTED record behind (the sibling handlers resolve before
    # admission/start for the same reason).
    folder_id, folder_refusal = await _resolve_fallback_folder_id(
        request, str(body.get("folder") or "")
    )
    if folder_refusal is not None:
        return folder_refusal
    from kiro_crew.shared_crew.containment import principal_scope

    run_admission = await _admit_workflow_run(request, svc, body)
    if run_admission.rejected:
        return run_admission.refusal_response()
    if run_admission.replayed:
        from kiro_crew.shared_crew.admission_entry import background_replay_response

        return background_replay_response(run_admission)
    try:
        with principal_scope(None if run_admission.work is None else run_admission.work.budget_key):
            out = await svc.start(
                source,
                name=body.get("name", "") or "",
                args=body.get("args") if isinstance(body.get("args"), dict) else {},
                author=request.headers.get("X-Session-Key", ""),
                session_key=request.headers.get("X-Session-Key", ""),
                expected_store=request.get("workflow_expected_store"),
                budget_total=budget_total,
                timeout_secs=_opt_int(body.get("timeout_secs")),
                fallback_title=str(body.get("title") or ""),
                fallback_folder_id=folder_id,
            )
    except Exception:
        logger.exception("workflow run failed")
        await run_admission.abandon()
        return _error("could not start workflow", "workflow_run_failed", 500)
    if "run_id" in out:
        await run_admission.attach_child(
            str(out["run_id"]), also=_workflow_worker_keys(str(out["run_id"]))
        )
    else:
        await run_admission.abandon()
    status = 200 if "run_id" in out else 400
    return web.json_response(_redact_obj(out), status=status)


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
    intent = body.get("intent") or ""
    if not isinstance(intent, str):
        return _error("intent must be a string", "invalid_field_type", 400)
    intent = intent.strip()
    if not intent:
        return web.json_response({"error": "intent is required"}, status=400)
    budget_total = body.get("budget_total")
    if not isinstance(budget_total, int):
        budget_total = None
    refusal = await _private_memory_refusal(request, "workflow.run_intent")
    if refusal is not None:
        return refusal
    folder_id, folder_refusal = await _resolve_fallback_folder_id(
        request, str(body.get("folder") or "")
    )
    if folder_refusal is not None:
        return folder_refusal
    try:
        out = await _start_admitted(
            request,
            svc,
            body,
            lambda: svc.start_from_intent(
                intent,
                name=body.get("name", "") or "",
                args=body.get("args") if isinstance(body.get("args"), dict) else {},
                author=request.headers.get("X-Session-Key", ""),
                session_key=request.headers.get("X-Session-Key", ""),
                expected_store=request.get("workflow_expected_store"),
                budget_total=budget_total,
                timeout_secs=_opt_int(body.get("timeout_secs")),
                fallback_title=str(body.get("title") or ""),
                fallback_folder_id=folder_id,
            ),
        )
    except Exception:
        logger.exception("workflow run_intent failed")
        return _error("could not start workflow", "workflow_run_intent_failed", 500)
    if isinstance(out, web.Response):
        return out
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
    # A rerun launches a new run under the prior run's origin session, optionally
    # with an edited script, so it is gated like the other launch routes above.
    if request.get("internal_auth") is not True and request.get("app") == "":
        from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

        owner_denied = await require_owner_dashboard_request(request, "workflow.rerun")
        if owner_denied is not None:
            return owner_denied
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
    try:
        out = await _start_admitted(
            request,
            svc,
            ("rerun", run_id, body),
            lambda: svc.rerun_subtree(
                run_id,
                from_index,
                source=edited_source,
                caller_session=request.headers.get("X-Session-Key", ""),
                owner=request.get("app") == "" and request.get("internal_auth") is not True,
            ),
        )
    except Exception:
        logger.exception("workflow rerun failed")
        return _error("could not rerun workflow", "workflow_rerun_failed", 500)
    if isinstance(out, web.Response):
        return out
    # 400 on validation error (bad edited script), 404 when the run is missing.
    if "run_id" in out:
        status = 200
    elif out.get("errors"):
        status = 400
    else:
        status = 404
    return web.json_response(_redact_obj(out), status=status)
