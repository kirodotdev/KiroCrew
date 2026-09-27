"""Research Lab routes: the HTTP/SSE adapters and the historic import facade.

The campaign engine lives in ``campaign/``; its package docstring names the
owner of each concern. This module keeps what is HTTP: auth, request parsing,
the status and JSON each route answers with, route registration, and the
watchdog task handle. Every other name it defined, and every non-stdlib
collaborator it imported, still resolves as ``handlers.<name>``: reads and writes
are forwarded to the one component that binds the name, so a patch applied
through this module is the one every caller sees.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import sqlite3
import sys
from types import ModuleType
from typing import Any

from aiohttp import web

from kiro_crew.apps.builtins.auto_research.campaign import (
    agent_mode,
    grill,
    lifecycle,
    publication,
    storage,
    untrusted,
    watchdog,
    workflow_mode,
)
from kiro_crew.knowledge.llm_pool import LLMPool

logger = logging.getLogger(__name__)

_CAMPAIGN = "kiro_crew.apps.builtins.auto_research.campaign"
_UNTRUSTED = f"{_CAMPAIGN}.untrusted"
_STORAGE = f"{_CAMPAIGN}.storage"
_LIFECYCLE = f"{_CAMPAIGN}.lifecycle"
_PUBLICATION = f"{_CAMPAIGN}.publication"
_EXPLORATION = f"{_CAMPAIGN}.exploration"
_AGENT_MODE = f"{_CAMPAIGN}.agent_mode"
_WORKFLOW_MODE = f"{_CAMPAIGN}.workflow_mode"
_WATCHDOG = f"{_CAMPAIGN}.watchdog"
_GRILL = f"{_CAMPAIGN}.grill"

#: Historic ``handlers`` name -> the dotted name of the ``campaign`` component
#: that binds it. A name, not a module object, so a purged-and-reimported
#: component is resolved afresh from ``sys.modules`` on every access.
_EXPORTS: dict[str, str] = {
    "_HAS_SECURITY": _UNTRUSTED,
    "redact_credentials": _UNTRUSTED,
    "redact_exfiltration_urls": _UNTRUSTED,
    "_UNTRUSTED_DATA_NOTICE": _UNTRUSTED,
    "_fence_untrusted": _UNTRUSTED,
    "_redact_finding": _UNTRUSTED,
    "_redact_tree_node": _UNTRUSTED,
    "data_home": _STORAGE,
    "OnLoopDBGuard": _STORAGE,
    "is_campaign_id": _STORAGE,
    "RESEARCH_DIR": _STORAGE,
    "DB_PATH": _STORAGE,
    "research_dir": _STORAGE,
    "db_path": _STORAGE,
    "_DB_INIT_LOCK": _STORAGE,
    "_INITIALIZED_DBS": _STORAGE,
    "VALID_EXECUTION_MODES": _STORAGE,
    "DEFAULT_EXECUTION_MODE": _STORAGE,
    "DEFAULT_MAX_SUBQUESTIONS_PER_ROUND": _STORAGE,
    "DEFAULT_DEPTH_DECAY": _STORAGE,
    "DEFAULT_RESERVE_FRACTION": _STORAGE,
    "CampaignStatus": _STORAGE,
    "_validate_campaign_id": _STORAGE,
    "_safe_campaign_dir": _STORAGE,
    "_ON_LOOP_DB_GUARD": _STORAGE,
    "_get_db": _STORAGE,
    "_ensure_schema": _STORAGE,
    "_CYCLE_FILE_RE": _STORAGE,
    "_cycle_index": _STORAGE,
    "_cycle_finding_files": _STORAGE,
    "_campaign_dir": _STORAGE,
    "_read_text_or_missing": _STORAGE,
    "_read_json_or_missing": _STORAGE,
    "_write_text": _STORAGE,
    "_write_new_cycle_files": _STORAGE,
    "_copy_parent_findings": _STORAGE,
    "_unlink_if_present": _STORAGE,
    "_questions_path": _STORAGE,
    "_pending_question": _STORAGE,
    "write_status": _STORAGE,
    "write_guidance": _STORAGE,
    "get_findings": _STORAGE,
    "_list_cycle_files": _STORAGE,
    "_read_finding_file": _STORAGE,
    "_redact_campaign": _STORAGE,
    "get_campaign": _STORAGE,
    "list_campaigns": _STORAGE,
    "_campaign_execution_mode": _STORAGE,
    "AUTO_RESEARCH_APP": _AGENT_MODE,
    "research_slot_key": _AGENT_MODE,
    "is_link_or_junction": _AGENT_MODE,
    "unlink_link_or_junction": _AGENT_MODE,
    "sel": _LIFECYCLE,
    "MAX_CYCLES_HARD_CAP": _LIFECYCLE,
    "_MAX_PARALLEL_WORKERS": _LIFECYCLE,
    "DEFAULT_IDLE_SECS": _LIFECYCLE,
    "_MAX_MODEL_LEN": _LIFECYCLE,
    "_TERMINAL_STATUSES": _LIFECYCLE,
    "_audit": _LIFECYCLE,
    "_campaign_model": _LIFECYCLE,
    "validate_campaign": _LIFECYCLE,
    "_FORK_NAME_PREFIX": _LIFECYCLE,
    "_fork_name": _LIFECYCLE,
    "create_campaign": _LIFECYCLE,
    "update_campaign_status": _LIFECYCLE,
    "delete_campaign": _LIFECYCLE,
    "_SSE_QUEUE_MAXSIZE": _LIFECYCLE,
    "_sse_queues": _LIFECYCLE,
    "_emit_sse": _LIFECYCLE,
    "_sse_from_thread": _LIFECYCLE,
    "_campaign_transition_locks": _LIFECYCLE,
    "_campaign_transition_lock": _LIFECYCLE,
    "_settle_before_cancellation": _LIFECYCLE,
    "_guarded_txn": _LIFECYCLE,
    "_guarded_transition": _LIFECYCLE,
    "_campaign_run_has_status": _LIFECYCLE,
    "_campaign_run_is_current": _LIFECYCLE,
    "ArtifactNotFoundError": _PUBLICATION,
    "ArtifactStore": _PUBLICATION,
    "_HAS_ARTIFACTS": _PUBLICATION,
    "ImportChunkBudgetError": _PUBLICATION,
    "_brief_publish_locks": _PUBLICATION,
    "_brief_publish_locks_guard": _PUBLICATION,
    "_brief_publish_lock": _PUBLICATION,
    "_write_brief": _PUBLICATION,
    "_read_report": _PUBLICATION,
    "_REPORT_TIMEOUT": _PUBLICATION,
    "_build_report_prompt": _PUBLICATION,
    "_render_findings_html": _PUBLICATION,
    "_sq": _EXPLORATION,
    "_EMERGENT_FILENAME": _EXPLORATION,
    "_FINALIZE_FLAG": _EXPLORATION,
    "_reserve_cycles": _EXPLORATION,
    "_in_reserve_zone": _EXPLORATION,
    "_ingest_emergent_questions": _EXPLORATION,
    "_activate_emergent": _EXPLORATION,
    "_should_finalize": _EXPLORATION,
    "_enter_finalize": _EXPLORATION,
    "_advance_exploration": _EXPLORATION,
    "_autonudge_instance": _AGENT_MODE,
    "slot_history_key": _AGENT_MODE,
    "_RESEARCH_AGENT": _AGENT_MODE,
    "_RESEARCH_NUDGE": _AGENT_MODE,
    "_WORKER_DONE_FILENAME": _AGENT_MODE,
    "_WORKER_DONE_MAX_BYTES": _AGENT_MODE,
    "_read_worker_done": _AGENT_MODE,
    "_clear_worker_done_marker": _AGENT_MODE,
    "_prepare_loop_launch": _AGENT_MODE,
    "_launch_loop": _AGENT_MODE,
    "_stop_loop": _AGENT_MODE,
    "_persist_new_cycle_bookkeeping": _AGENT_MODE,
    "_record_new_cycle_from_watchdog": _AGENT_MODE,
    "AUTONUDGE_STOP_REASON": _AGENT_MODE,
    "RESEARCH_WORKFLOW_SOURCE": _WORKFLOW_MODE,
    "build_workflow_args": _WORKFLOW_MODE,
    "_WORKFLOW_RUN_FILE": _WORKFLOW_MODE,
    "_write_workflow_run_id": _WORKFLOW_MODE,
    "_read_workflow_cycle_offset": _WORKFLOW_MODE,
    "_read_workflow_run_id": _WORKFLOW_MODE,
    "_launch_workflow": _WORKFLOW_MODE,
    "_stop_workflow": _WORKFLOW_MODE,
    "_poll_workflow_campaign": _WORKFLOW_MODE,
    "is_app_enabled": _WATCHDOG,
    "is_research_slot_key": _WATCHDOG,
    "POLL_INTERVAL": _WATCHDOG,
    "_TERMINAL_LOOP_REMOVAL_ATTEMPTS": _WATCHDOG,
    "_FIRST_CYCLE_GRACE_SECS": _WATCHDOG,
    "_TRUST_TTL_SECS": _WATCHDOG,
    "_unresponsive_deadline": _WATCHDOG,
    "check_stagnation": _WATCHDOG,
    "_should_pause_for_question": _WATCHDOG,
    "_suspend_research_loops_while_disabled": _WATCHDOG,
    "_expire_trust": _WATCHDOG,
    "_stalled_campaign_verdict": _WATCHDOG,
    "_settle_campaign_from_watchdog": _WATCHDOG,
    "_watchdog_loop": _WATCHDOG,
    "_extract_json_of_type": _GRILL,
    "_MAX_GRILL_DEPTH": _GRILL,
    "_GRILL_CHILD_CAP": _GRILL,
    "_new_node_id": _GRILL,
    "_node_depth": _GRILL,
    "_GRILL_EXPAND_PROMPT": _GRILL,
    "_compact_tree": _GRILL,
    "_grill_node_shaped": _GRILL,
    "_parse_grill_nodes": _GRILL,
    "_grill_expand_children": _GRILL,
}


def _owner(name: str) -> ModuleType:
    """The component that binds historic name ``name``."""
    return importlib.import_module(_EXPORTS[name])


def __getattr__(name: str) -> Any:
    """Read a historic name from the component that binds it (:pep:`562`)."""
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(_owner(name), name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))


class _ReExportModule(ModuleType):
    """Send a write to a historic name to the component that binds it.

    A binding in this module's own namespace would shadow the owner: this
    module would read it, but the component's own callers would not, so a patch
    of ``handlers.<name>`` would silently stop reaching the code it targets.
    Forwarding keeps one value per name, which also keeps ``monkeypatch``'s
    read-then-reassign restore exact.
    """

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _EXPORTS:
            setattr(_owner(name), name, value)
        else:
            super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        if name in _EXPORTS:
            delattr(_owner(name), name)
        else:
            super().__delattr__(name)


_watchdog_task: asyncio.Task | None = None


def _require_auth(request: web.Request) -> web.Response | None:
    """Defense-in-depth auth check. Returns 401 response if unauthorized, None if OK.

    Primary auth is enforced by the gateway _auth_middleware in server.py which
    validates tokens against the session store and sets request["user"] on
    success. This check rejects any request where middleware did not run (e.g.
    misconfigured proxy bypass) — we trust only the middleware-set user, never
    a raw token string, to avoid a fail-open bypass.
    """
    if request.get("user") is not None:
        return None
    return web.json_response({"error": "Unauthorized"}, status=401)


async def _read_json_body(request: web.Request):
    """Parse a JSON object body, or return a 400 ``web.Response``.

    aiohttp's ``request.json()`` raises ``json.JSONDecodeError`` on a malformed
    body; without this a client input error becomes an unhandled 500 (CWE-703).
    Also type-checks the decoded body is a dict so downstream ``.get()``/``[]``
    access can't raise AttributeError/KeyError on a valid-JSON non-object.
    Callers: ``body = await _read_json_body(request); if isinstance(body,
    web.Response): return body``.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "request body must be a JSON object"}, status=400)
    return body


async def _handle_validate(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    lifecycle._audit("campaign_validate", "*")
    body = await _read_json_body(request)
    if isinstance(body, web.Response):
        return body
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, lifecycle.validate_campaign, body)
    return web.json_response(result)


async def _handle_grill_expand(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    body = await _read_json_body(request)
    if isinstance(body, web.Response):
        return body
    question = (body.get("question") or "").strip()
    if len(question) < 20:
        return web.json_response({"error": "Question too short"}, status=400)
    tree = body.get("tree") or []
    node_id = body.get("node_id")
    if not isinstance(tree, list):
        return web.json_response({"error": "tree must be a list"}, status=400)
    if node_id is not None:
        depth = grill._node_depth(tree, node_id)
        if depth < 0:
            return web.json_response({"error": "Unknown node_id"}, status=400)
        if depth >= grill._MAX_GRILL_DEPTH:
            return web.json_response({"nodes": [], "reason": "max_depth"})
    lifecycle._audit("grill_expand", "*")
    pool = request.app.get("auto_research_llm_pool")
    raw = await grill._grill_expand_children(pool, question, tree, node_id)
    nodes = grill._child_nodes(raw, node_id)
    return web.json_response(untrusted._redact_finding({"nodes": nodes}))


async def _handle_create(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    body = await _read_json_body(request)
    if isinstance(body, web.Response):
        return body
    loop = asyncio.get_running_loop()
    v = await loop.run_in_executor(None, lifecycle.validate_campaign, body)
    if not v["can_start"]:
        return web.json_response({"error": "Validation failed", **v}, status=400)
    result = await loop.run_in_executor(None, lifecycle.create_campaign, body)
    result["name"] = untrusted._redact_finding({"v": result["name"]})["v"]
    return web.json_response(result, status=201)


async def _handle_list(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    lifecycle._audit("campaign_list", "*")
    loop = asyncio.get_running_loop()
    campaigns = await loop.run_in_executor(None, storage.list_campaigns)
    return web.json_response(campaigns)


async def _handle_get(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    cid = request.match_info["id"]
    if not storage._validate_campaign_id(cid):
        return web.json_response({"error": "Invalid campaign ID"}, status=400)
    lifecycle._audit("campaign_get", cid)
    loop = asyncio.get_running_loop()
    c = await loop.run_in_executor(None, storage.get_campaign, cid)
    return web.json_response(c) if c else web.json_response({"error": "Not found"}, status=404)


async def _handle_report(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    cid = request.match_info["id"]
    if not storage._validate_campaign_id(cid):
        return web.json_response({"error": "Invalid campaign ID"}, status=400)
    lifecycle._audit("campaign_report", cid)
    # FINDINGS.md is agent-authored — redact before serving to the dashboard.
    report = untrusted._redact_finding({"v": publication._read_report(cid)})["v"]
    return web.json_response({"report": report})


async def _handle_action(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    cid = request.match_info["id"]
    if not storage._validate_campaign_id(cid):
        return web.json_response({"error": "Invalid campaign ID"}, status=400)
    body = await _read_json_body(request)
    if isinstance(body, web.Response):
        return body
    action = body.get("action")
    if action not in lifecycle._ACTION_TARGET_STATUS and action != "fork":
        return web.json_response({"error": f"Unknown action: {action}"}, status=400)

    # Fork: creates a new child campaign from a completed parent.
    if action == "fork":

        def _read_fork_parent() -> sqlite3.Row | None:
            db = storage._get_db()
            try:
                return db.execute(
                    "SELECT id, question, sources, status, model FROM campaigns WHERE id = ?",
                    (cid,),
                ).fetchone()
            finally:
                db.close()

        parent = await asyncio.to_thread(_read_fork_parent)
        if parent is None:
            return web.json_response({"error": "Not found"}, status=404)
        if parent["status"] not in (
            storage.CampaignStatus.COMPLETE,
            storage.CampaignStatus.STOPPED,
        ):
            return web.json_response(
                {"error": "Can only fork a completed or stopped campaign"}, status=409
            )
        # Build the fork config from the request body (sub_questions come from
        # the frontend's challenge-mode grill tree).
        fork_config = {
            "question": body.get("question") or parent["question"],
            "name": lifecycle._fork_name(
                body.get("name") or body.get("question") or parent["question"]
            ),
            "sub_questions": body.get("sub_questions", []),
            "sources": json.loads(parent["sources"] or "[]"),
            "max_cycles": body.get("max_cycles", 30),
            "idle_secs": body.get("idle_secs", lifecycle.DEFAULT_IDLE_SECS),
            "success_criteria": body.get("success_criteria"),
            "auto_approve": body.get("auto_approve", False),
            "parent_id": cid,
            "model": parent["model"] or "",  # fork continues on the parent's pick
            "grill_tree": body.get("grill_tree"),
        }
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, lifecycle.create_campaign, fork_config)
        # Copy parent FINDINGS.md as context into the fork's dir. Use the
        # path-traversal-guarded _safe_campaign_dir (resolve + is_relative_to)
        # for both ids — defense-in-depth even though both are already
        # format-validated (cid via _validate_campaign_id, result["id"] is a
        # freshly generated uuid) — consistent with _handle_grill_tree /
        # get_findings.
        parent_dir = storage._safe_campaign_dir(cid)
        fork_dir = storage._safe_campaign_dir(result["id"])
        if parent_dir is None or fork_dir is None:
            return web.json_response({"error": "Invalid campaign ID"}, status=400)
        # One hop: the copy reads the parent's findings (unbounded LLM output)
        # and writes them into the fork, neither of which belongs on the loop.
        await asyncio.to_thread(
            storage._copy_parent_findings,
            parent_dir / "FINDINGS.md",
            fork_dir / "parent_findings.md",
        )
        lifecycle._audit("campaign_forked", result["id"], parent=cid)
        return web.json_response(result, status=201)

    # Guard invalid source-state transitions (lifecycle._ACTION_SOURCE_STATUSES).
    async with lifecycle._campaign_transition_lock(cid):

        def _read_status_row() -> sqlite3.Row | None:
            db = storage._get_db()
            try:
                return db.execute("SELECT status FROM campaigns WHERE id = ?", (cid,)).fetchone()
            finally:
                db.close()

        srow = await asyncio.to_thread(_read_status_row)
        if srow is None:
            return web.json_response({"error": "Not found"}, status=404)
        if srow["status"] not in lifecycle._ACTION_SOURCE_STATUSES[action]:
            return web.json_response(
                {"error": f"Cannot {action} a campaign in '{srow['status']}' state"}, status=409
            )
        mode = await asyncio.to_thread(storage._campaign_execution_mode, cid)
        if action in ("start", "resume") and mode != "workflow":
            # Publish RUNNING only after old stop evidence is gone. The watchdog
            # selects RUNNING campaigns, so reversing this order exposes a partial
            # resume while marker cleanup or tombstone persistence is still pending.
            await agent_mode._prepare_loop_launch(cid)
        result = await asyncio.to_thread(
            lifecycle.update_campaign_status, cid, lifecycle._ACTION_TARGET_STATUS[action]
        )
        if "error" in result:
            return web.json_response(result, status=404)
        if action in ("start", "resume"):
            if mode == "workflow":
                await workflow_mode._launch_workflow(request, cid)
            else:
                await agent_mode._launch_loop(request, cid, prepared=True)
        elif action == "pause":
            if mode == "workflow":
                await workflow_mode._stop_workflow(request, cid)
            else:
                await agent_mode._stop_loop(cid, remove=False)
        elif action == "stop":
            if mode == "workflow":
                await workflow_mode._stop_workflow(request, cid)
            else:
                await agent_mode._stop_loop(cid, remove=True, stop_reason="campaign_stopped")
        return web.json_response(result)


async def _handle_delete(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    cid = request.match_info["id"]
    if not storage._validate_campaign_id(cid):
        return web.json_response({"error": "Invalid campaign ID"}, status=400)
    async with lifecycle._campaign_transition_lock(cid):
        # Tear down any running worker (agent loop or workflow run) first.
        mode = await asyncio.to_thread(storage._campaign_execution_mode, cid)
        if mode == "workflow":
            await workflow_mode._stop_workflow(request, cid)
        else:
            await agent_mode._stop_loop(cid, remove=True, stop_reason="campaign_deleted")
        result = await asyncio.to_thread(lifecycle.delete_campaign, cid)
        if "error" in result:
            return web.json_response(result, status=404)
        lifecycle._audit("campaign_deleted", cid)
        return web.json_response(result)


async def _handle_nudge(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    cid = request.match_info["id"]
    if not storage._validate_campaign_id(cid):
        return web.json_response({"error": "Invalid campaign ID"}, status=400)
    # Workflow-mode campaigns are driven by a deterministic DW script; guidance
    # injected mid-run has no effect (the script doesn't read guidance.txt).
    if await asyncio.to_thread(storage._campaign_execution_mode, cid) == "workflow":
        return web.json_response(
            {
                "error": "Nudge/guidance not supported in workflow mode — the script "
                "runs autonomously. Use agent mode for interactive guidance."
            },
            status=409,
        )
    body = await _read_json_body(request)
    if isinstance(body, web.Response):
        return body
    text = body.get("text", "")
    if not text:
        return web.json_response({"error": "text required"}, status=400)
    storage.write_guidance(cid, text)
    # If the agent paused awaiting input, clear the question and resume.
    # Guarded: a Stop/Pause that committed while this handler ran must win —
    # restoring RUNNING over it would resurrect a campaign with no worker.
    qp = storage._questions_path(cid)
    cleared = await asyncio.to_thread(storage._unlink_if_present, qp) if qp is not None else False
    if cleared:
        await lifecycle._guarded_transition(
            cid,
            storage.CampaignStatus.RUNNING,
            allowed_current=(storage.CampaignStatus.NEEDS_INPUT,),
        )
    lifecycle._audit("campaign_nudge", cid)
    return web.json_response({"ok": True})


async def _handle_report_status(request: web.Request) -> web.Response:
    """GET /campaigns/{id}/report-status -- has a report artifact already been
    exported for this campaign, and does it still exist?

    Returns ``{slug}`` (the live artifact slug) or ``{slug: null}``. Read-only
    status probe so the UI can show "View report" + "Regenerate" upfront
    instead of a bare "Export". Degrades gracefully when artifacts are off.
    """
    if denied := _require_auth(request):
        return denied
    cid = request.match_info["id"]
    if not storage._validate_campaign_id(cid):
        return web.json_response({"error": "Invalid campaign ID"}, status=400)
    if not publication._HAS_ARTIFACTS:
        return web.json_response({"slug": None})
    row = await asyncio.to_thread(publication._read_report_slug, cid)
    if row is None:
        return web.json_response({"error": "Not found"}, status=404)
    slug = row["report_artifact_slug"]
    if not slug:
        return web.json_response({"slug": None})
    return web.json_response({"slug": publication._live_report_slug(slug, cid)})


async def _handle_to_artifact(request: web.Request) -> web.Response:
    """POST /campaigns/{id}/to-artifact -- author an HTML report artifact.

    The report is LLM-authored (a polished, synthesized document) so it is nice
    to read; if the LLM pool is unavailable or returns nothing, we fall back to
    a mechanical render of FINDINGS.md so the action never hard-fails.
    """
    if denied := _require_auth(request):
        return denied
    cid = request.match_info["id"]
    if not storage._validate_campaign_id(cid):
        return web.json_response({"error": "Invalid campaign ID"}, status=400)
    # Fail fast before any filesystem / DB / render work if artifacts are off.
    if not publication._HAS_ARTIFACTS:
        return web.json_response({"error": "Artifact system unavailable"}, status=503)
    d = storage._safe_campaign_dir(cid)
    if d is None:
        return web.json_response({"error": "Invalid campaign ID"}, status=400)
    findings_path = d / "FINDINGS.md"
    if not await asyncio.to_thread(findings_path.exists):
        return web.json_response({"error": "No findings yet"}, status=404)
    row = await asyncio.to_thread(publication._read_export_row, cid)
    if row is None:
        return web.json_response({"error": "Not found"}, status=404)
    question = row["question"]
    findings_md = await asyncio.to_thread(storage._read_text_or_missing, findings_path)
    if findings_md is None:
        return web.json_response({"error": "No findings yet", "code": "findings_missing"}, status=404)
    subs = json.loads(row["sub_questions"] or "[]")
    html = await publication._author_report_html(
        request.app.get("auto_research_llm_pool"),
        question,
        subs,
        findings_md,
        row["total_cycles"],
        cid,
    )
    art, name, regenerated = await publication._publish_report_artifact(
        cid, question, html, row["report_artifact_slug"]
    )
    lifecycle._audit("campaign_to_artifact", cid, slug=art.slug)
    return web.json_response(
        {"slug": art.slug, "name": name, "regenerated": regenerated},
        status=200 if regenerated else 201,
    )


async def _handle_knowledge_status(request: web.Request) -> web.Response:
    """GET /campaigns/{id}/knowledge-status -- has this campaign's findings
    already been ingested into the Knowledge Library?

    Read-only status probe so the UI can render "Already in Knowledge" upfront
    instead of discovering it via a 409 after the user clicks. Degrades
    gracefully (``in_library: false``) when the Knowledge Library is
    unavailable -- a status check must never surface a 503.
    """
    if denied := _require_auth(request):
        return denied
    cid = request.match_info["id"]
    if not storage._validate_campaign_id(cid):
        return web.json_response({"error": "Invalid campaign ID"}, status=400)
    d = storage._safe_campaign_dir(cid)
    if d is None:
        return web.json_response({"error": "Invalid campaign ID"}, status=400)
    state = request.app.get("state")
    if state is None or not hasattr(state, "knowledge_store"):
        return web.json_response({"in_library": False})
    store = state.knowledge_store
    uri = publication._knowledge_uri(d)
    try:
        existing = await asyncio.to_thread(store.get_source_by_uri, uri)
    except Exception:
        logger.exception("knowledge-status lookup failed for %s", cid)
        return web.json_response({"in_library": False})
    if existing:
        return web.json_response({"in_library": True, "source_id": existing["id"]})
    return web.json_response({"in_library": False})


async def _handle_to_knowledge(request: web.Request) -> web.Response:
    """POST /campaigns/{id}/to-knowledge -- ingest FINDINGS.md into Knowledge Library."""
    if denied := _require_auth(request):
        return denied
    cid = request.match_info["id"]
    if not storage._validate_campaign_id(cid):
        return web.json_response({"error": "Invalid campaign ID"}, status=400)
    d = storage._safe_campaign_dir(cid)
    if d is None:
        return web.json_response({"error": "Invalid campaign ID"}, status=400)
    findings_path = d / "FINDINGS.md"
    if not await asyncio.to_thread(findings_path.exists):
        return web.json_response({"error": "No findings yet"}, status=404)
    # Access knowledge store and pipeline from app state
    state = request.app.get("state")
    if state is None or not hasattr(state, "knowledge_store"):
        return web.json_response({"error": "Knowledge Library unavailable"}, status=503)
    store = state.knowledge_store
    pipeline = request.app.get("knowledge_pipeline")
    if pipeline is None:
        return web.json_response({"error": "Knowledge pipeline unavailable"}, status=503)
    raw_findings = await asyncio.to_thread(storage._read_text_or_missing, findings_path)
    if raw_findings is None:
        return web.json_response({"error": "No findings yet", "code": "findings_missing"}, status=404)
    uri = await publication._write_knowledge_copy(d, raw_findings)
    # Dedup check
    existing = await asyncio.to_thread(store.get_source_by_uri, uri)
    if existing:
        return web.json_response(
            {"error": "Already in Knowledge Library", "id": existing["id"]}, status=409
        )
    # Add source and trigger ingestion
    row = await asyncio.to_thread(publication._read_question_row, cid)
    name = publication._knowledge_source_name(row, cid)
    sid = await asyncio.to_thread(publication._add_source_marked_syncing, store, name, uri)
    task = asyncio.create_task(publication._ingest_findings(pipeline, store, uri, sid, cid))
    # Seeded by register_routes; the create branch serves an Application that
    # skipped registration and is still mutable (a directly driven handler).
    app_tasks = request.app.get("_bg_tasks")
    if app_tasks is None:
        app_tasks = set()
        request.app["_bg_tasks"] = app_tasks
    app_tasks.add(task)
    task.add_done_callback(app_tasks.discard)
    lifecycle._audit("campaign_to_knowledge", cid, source_id=sid)
    return web.json_response({"id": sid, "status": "ingesting"}, status=201)


async def _handle_add_question(request: web.Request) -> web.Response:
    """Append a user-authored sub-question to a campaign mid-run."""
    if denied := _require_auth(request):
        return denied
    cid = request.match_info["id"]
    if not storage._validate_campaign_id(cid):
        return web.json_response({"error": "Invalid campaign ID"}, status=400)
    # Workflow-mode campaigns plan sub-questions at launch (the DW script
    # decomposes them internally); adding questions mid-run has no effect.
    if await asyncio.to_thread(storage._campaign_execution_mode, cid) == "workflow":
        return web.json_response(
            {
                "error": "Adding questions mid-run not supported in workflow mode — "
                "sub-questions are planned at launch. Use agent mode for "
                "interactive exploration."
            },
            status=409,
        )
    body = await _read_json_body(request)
    if isinstance(body, web.Response):
        return body
    text = (body.get("text") or "").strip()
    if not text:
        return web.json_response({"error": "text required"}, status=400)

    subs = await asyncio.to_thread(publication._append_question, cid, text)
    if subs is None:
        return web.json_response({"error": "Not found"}, status=404)
    lifecycle._audit("campaign_add_question", cid)
    lifecycle._emit_sse({"type": "question_added", "campaign_id": cid})
    return web.json_response({"ok": True, "sub_questions": subs})


async def _handle_stream(request: web.Request) -> web.StreamResponse:
    if denied := _require_auth(request):
        return denied
    cid = request.match_info["id"]
    if not storage._validate_campaign_id(cid):
        return web.json_response({"error": "Invalid campaign ID"}, status=400)
    lifecycle._audit("campaign_stream", cid)
    resp = web.StreamResponse(
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )
    await resp.prepare(request)
    q: asyncio.Queue = asyncio.Queue(maxsize=lifecycle._SSE_QUEUE_MAXSIZE)
    lifecycle._sse_queues.append(q)
    try:
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=15.0)
                if event.get("campaign_id") == cid:
                    # Findings are already redacted at the source
                    # (get_findings -> _redact_finding); avoid re-redacting.
                    data = json.dumps(event)
                    await resp.write(f"data: {data}\n\n".encode())
            except asyncio.TimeoutError:
                await resp.write(b": keepalive\n\n")
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        lifecycle._sse_queues.remove(q)
    return resp


async def _handle_grill_tree(request: web.Request) -> web.Response:
    """Serve the persisted grill tree for a campaign (for revisiting / challenge mode)."""
    if denied := _require_auth(request):
        return denied
    cid = request.match_info["id"]
    d = storage._safe_campaign_dir(cid)
    if d is None:
        return web.json_response({"error": "Invalid campaign ID"}, status=400)
    tree_path = d / "grill_tree.json"
    tree = await asyncio.to_thread(storage._read_json_or_missing, tree_path)
    if tree is None:
        return web.json_response({"tree": []})
    # Never trust LLM output: node text/recommended fields are model-generated,
    # so redact credentials + exfiltration URLs before serving to the dashboard
    # (same treatment as cycle findings via _redact_finding).
    if not isinstance(tree, list):
        # Fail-closed: a non-list payload (file corruption/tampering) is not a
        # valid grill tree and can't be element-redacted — drop it entirely
        # rather than serving unscanned LLM-generated content to the client.
        tree = []
    else:
        # Scan EVERY element, not just dicts: stray strings would otherwise be
        # served unredacted.
        tree = [untrusted._redact_tree_node(n) for n in tree]
    return web.json_response({"tree": tree})


def register_routes(app: web.Application) -> None:
    # Seeded while the app is still mutable; a handler-time ``setdefault`` would
    # write to the frozen app. Shared with the knowledge routes, so setdefault.
    app.setdefault("_bg_tasks", set())
    app.router.add_post("/api/apps/auto-research/validate", _handle_validate)
    app.router.add_post("/api/apps/auto-research/grill/expand", _handle_grill_expand)
    app.router.add_post("/api/apps/auto-research/campaigns", _handle_create)
    app.router.add_get("/api/apps/auto-research/campaigns", _handle_list)
    app.router.add_get("/api/apps/auto-research/campaigns/{id}", _handle_get)
    app.router.add_get("/api/apps/auto-research/campaigns/{id}/report", _handle_report)
    app.router.add_get("/api/apps/auto-research/campaigns/{id}/grill-tree", _handle_grill_tree)
    app.router.add_patch("/api/apps/auto-research/campaigns/{id}", _handle_action)
    app.router.add_delete("/api/apps/auto-research/campaigns/{id}", _handle_delete)
    app.router.add_post("/api/apps/auto-research/campaigns/{id}/nudge", _handle_nudge)
    app.router.add_post("/api/apps/auto-research/campaigns/{id}/questions", _handle_add_question)
    app.router.add_post("/api/apps/auto-research/campaigns/{id}/to-knowledge", _handle_to_knowledge)
    app.router.add_get(
        "/api/apps/auto-research/campaigns/{id}/knowledge-status", _handle_knowledge_status
    )
    app.router.add_post("/api/apps/auto-research/campaigns/{id}/to-artifact", _handle_to_artifact)
    app.router.add_get(
        "/api/apps/auto-research/campaigns/{id}/report-status", _handle_report_status
    )
    app.router.add_get("/api/apps/auto-research/campaigns/{id}/stream", _handle_stream)

    async def _start_watchdog(_app: web.Application) -> None:
        global _watchdog_task
        # Dedicated LLM pool for the grill expand endpoint — isolated from the
        # Knowledge Library's pool so the two apps don't share workers.
        _app["auto_research_llm_pool"] = LLMPool(pool_size=1)
        _watchdog_task = asyncio.create_task(watchdog._watchdog_loop(_app))

    async def _stop_watchdog(_app: web.Application) -> None:
        if _watchdog_task and not _watchdog_task.done():
            _watchdog_task.cancel()
            try:
                await _watchdog_task
            except asyncio.CancelledError:
                pass
        pool = _app.get("auto_research_llm_pool")
        if pool is not None:
            await pool.shutdown()

    app.on_startup.append(_start_watchdog)
    app.on_shutdown.append(_stop_watchdog)


# Installed last, so the forwarding is live for every caller but never runs
# while this module is still binding its own names.
sys.modules[__name__].__class__ = _ReExportModule
