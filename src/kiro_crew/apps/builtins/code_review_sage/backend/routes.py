"""Deterministic backend route for the Code Review Sage builtin.

Kicks off the two-stage review driver **in-process** (no LLM orchestrator), so
the Phase 1 -> Phase 2 switch AND the finalize step (report -> archive -> clean)
are guaranteed to run as Python, never left to an LLM session that can deviate.
The only LLM work is the per-change gate/deep tasks the driver dispatches to a
reusable worker pool (``sage_lib/review_pool.py``) — long-lived ACP sessions reused
across CRs, not per-CR ``/api/spawn`` sub-agents — so reviews run silently.

Why this exists: when an LLM orchestrator session was merely *asked* to shell
``review_driver.py``, it could instead run Phase 1 inline and stop — leaving a
gate-only record, a stale report, and no Phase 2 (observed in practice).
Routing the kickoff through this route removes that discretion entirely.

Registered at gateway startup by ``apps/routes.py:register_app_routes`` (loaded
by file path because the app dir name ``code-review-sage`` is hyphenated and
cannot be imported as a Python package).

Routes (browser-facing, same-origin authed exactly like ``/config``):
  POST /api/apps/code-review-sage/review   {links | changes} -> {run_id, changes}
  GET  /api/apps/code-review-sage/runs                       -> {runs: [...]}

The ``/runs`` registry lets the page render live status and reconstruct it after
navigating away (the backend owns the run, not ephemeral React state). Per-change
detail still comes from the on-disk result records the driver writes.

The handlers and their helpers live in the ``routes_parts`` package beside this file,
one module per responsibility, and run on this module's globals through
:func:`kiro_crew._facade_compose.compose` (see ``routes_parts/__init__.py``), so this
file stays the one import path and the one patch surface. It keeps the import header,
the process state, the follow-up endpoints and ``register_routes``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from functools import wraps
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew import hooks, model_registry
from kiro_crew.apps.manager import is_app_enabled
from kiro_crew.atomic_write import atomic_write
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.loop_lock import LoopBoundLock

logger = logging.getLogger("kirocrew.app.code-review-sage")

# The app root (.../apps/builtins/code-review-sage) holds ``sage_lib/`` next to this
# ``backend/`` dir. Put it on sys.path so ``from sage_lib import review_driver`` works
# the same way the driver resolves its own siblings.
_APP_ROOT = Path(__file__).resolve().parent.parent
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

# Sibling app modules — importable now that the app root is on sys.path (the app
# dir is hyphenated, so these are not auto-discovered packages). Imported at the
# top per the top-level-imports guideline; the sys.path setup above executes at
# module load, so this resolves on first import.
from sage_lib import (  # noqa: E402,E501
    adapters,
    discovery,
    followup,
    learning,
    pipeline,
    report,
    results,
    review_driver,
    review_pool,
    store,
)

# In-memory run registry, most-recent first. Bounded so it can't grow unbounded.
# Holds lightweight run descriptors the page polls; on-disk result records carry
# per-change detail.
_RUNS: list[dict[str, Any]] = []
_RUNS_MAX = 25
_LOCK = LoopBoundLock()
# Guards the claim/dedup step below. Runs themselves are NOT serialized: each run
# owns a private ``data/runs/<run_id>/`` subtree (results + report), so several
# reviews can be in flight at once. What still needs mutual exclusion is the
# moment a run decides WHICH changes it owns.
# Serializes whole runs. Workers hand results back through a directory shared
# ACROSS runs, so overlapping runs would mean two writers to one path.
# LoopBoundLock excludes within one loop only; all run starters are route
# handlers on the app's single gateway loop, so every contender shares it.
_RUN_LOCK = LoopBoundLock()

# Serialises "start a consolidation" against "delete this namespace". Both are
# short critical sections; holding one lock across each removes the interleaving
# rather than trying to place checks around the awaits.
_NS_OPS_LOCK = LoopBoundLock()
# reviewed-key -> run_id for every change a LIVE run has claimed. Two runs must
# never review and post to the same PR concurrently: the old whole-run lock
# prevented that by refusing to overlap at all; this claim registry gets the same
# guarantee while letting unrelated runs proceed in parallel. It also closes a gap
# the old lock never covered — a pasted-link run overlapping a repo run.
_INFLIGHT: dict[str, str] = {}
# staging-key -> the reviewed key that owns it. `_INFLIGHT` alone cannot express
# this: exempting a run's own claims (needed so re-claiming is idempotent) also
# exempted a SECOND change in the same run whose lossy id collapsed onto the same
# staging file — both were kept, both workers wrote one path, and one review was
# adopted under the wrong pull request. Ownership is per CHANGE, not per run.
_STAGE_OWNER: dict[str, str] = {}
# Run ids the user asked to cancel. The driver polls this between changes.
_CANCELLED: set[str] = set()
# Guards copy-on-write updates to a run's per-change ``progress`` map, which the
# (threaded) driver writes and the /runs handler reads concurrently.
_PROGRESS_LOCK = threading.Lock()
# Keep strong refs to background tasks so they aren't garbage-collected mid-flight.
_TASKS: set[asyncio.Task] = set()  # type: ignore[type-arg]
# Dashboard state, captured at route registration. A finished run notifies the
# bell feed, and that happens in a background task with no ``request`` in scope —
# so the state has to be reachable without one. Populated lazily and treated as
# optional everywhere (tests register routes on a bare aiohttp app).
_APP_STATE: dict[str, Any] = {}


# --- Composed parts -----------------------------------------------------------
# The run registry, the review runs, the per-run endpoints, publishing, repo
# discovery, the settings and the learning namespaces live in ``routes_parts`` and run
# on THIS module's globals (:func:`kiro_crew._facade_compose.compose`): a patch of a
# name here reaches them, a ``global`` write in one lands here, and each reports this
# module as its ``__module__``. A test's private copy of this file composes them onto
# that copy. The imports are absolute because such a copy has no parent package.
from kiro_crew._facade_compose import compose as _facade_compose  # noqa: E402
from kiro_crew.apps.builtins.code_review_sage.backend import routes_parts as _parts  # noqa: E402
from kiro_crew.apps.builtins.code_review_sage.backend.routes_parts.namespaces import (  # noqa: E402,F401
    _consolidate_bg,
    _handle_consolidate,
    _handle_learnings,
    _handle_namespaces,
)
from kiro_crew.apps.builtins.code_review_sage.backend.routes_parts.publishing import (  # noqa: E402,F401
    _DISMISS_REASON_MAX,
    _collect_delivered,
    _dismissed_keys,
    _handle_run_dismiss,
    _handle_run_post,
    _notify_posted,
    _pending_comment_count,
    _post_comments_bg,
    _without_dismissed,
)
from kiro_crew.apps.builtins.code_review_sage.backend.routes_parts.registry import (  # noqa: E402,F401
    _claim_changes_under_lock,
    _find_run,
    _is_live,
    _load_runs,
    _make_progress,
    _now,
    _reap_orphan_run_dirs,
    _record,
    _release_claims,
    _runs_file,
    _save_runs,
    _stage_key,
    _write_runs,
)
from kiro_crew.apps.builtins.code_review_sage.backend.routes_parts.repos import (  # noqa: E402,F401
    _handle_my_repos,
    _handle_recent_repos,
    _handle_repos,
    _handle_review_queue,
    _pull_request_ref,
)
from kiro_crew.apps.builtins.code_review_sage.backend.routes_parts.review_runs import (  # noqa: E402,F401
    _first_change_error,
    _first_change_failure,
    _handle_repo_prs,
    _handle_review,
    _handle_review_repo,
    _handle_runs,
    _list_repo_prs,
    _notify_finished,
    _posting_expected,
    _record_reviewed,
    _run_headline,
    _run_review_bg,
)
from kiro_crew.apps.builtins.code_review_sage.backend.routes_parts.run_threads import (  # noqa: E402,F401
    _handle_run_archive,
    _handle_run_cancel,
    _handle_run_delete,
    _handle_run_detail,
    _handle_run_report,
    _rebanded_report,
    _run_id_param,
)
from kiro_crew.apps.builtins.code_review_sage.backend.routes_parts.settings import (  # noqa: E402,F401
    _handle_settings,
    _known_models,
    _load_known_models,
    _load_review_section,
    _valid_model,
    _write_review_section,
)

_facade_compose(
    globals(),
    (
        _parts.registry,
        _parts.review_runs,
        _parts.run_threads,
        _parts.publishing,
        _parts.repos,
        _parts.settings,
        _parts.namespaces,
    ),
)


# Computed once at import (the registry is immutable after load). A module-level
# constant so the settings validator and the /settings enumerator share one list.
_KNOWN_MODELS: list[str] = _load_known_models()


# Namespaces with a merge in flight. Consolidation replaces the whole ruleset, so
# two concurrent merges could interleave writes and lose patterns; the claim is
# held for the life of the request rather than per-write.
# A consolidated ruleset is a handful of short patterns; anything larger is not
# a merge result. Bounds the read of an LLM-written file.
_MERGE_MAX_BYTES = 256 * 1024

_CONSOLIDATING: set[str] = set()

# Last outcome per namespace, so a merge that finished while the page was closed
# still reports itself instead of looking like it never ran.
_CONSOLIDATE_STATE: dict[str, dict] = {}


# --- follow-up sessions -----------------------------------------------------
# A finished review can be asked about later. Its session transcript is kept (see
# ``sage_lib/followup.py``) and a follow-up RESUMES it as an ordinary chat
# session, so the reviewer's own reasoning comes back cold from disk and the
# conversation from then on is a normal session: it survives restarts, is filed in
# the sidebar, and its tool use runs through the dashboard's approval pipeline.
#
# This app's part is deliberately small — validate, seed the resume, and hand back
# what the slot needs. Creating the slot itself stays with the dashboard's own
# endpoint so slot policy (agent binding, workspace, title redaction, ownership)
# has exactly one implementation.

# How often kept transcripts are aged out. They are the largest thing this app
# leaves on disk and a closed tab reports nothing, so this cannot be lazy.
_FOLLOWUP_SWEEP_INTERVAL = 3600.0


def _chat_params(run_id: str, change_id: str) -> str:
    """Validate the pair identifying a review. Returns an error code, or ""."""
    if not run_id or not change_id:
        return "chat_params_required"
    return ""


_ChatHandler = Callable[[web.Request], Awaitable[web.Response]]


def _require_enabled(handler: _ChatHandler) -> _ChatHandler:
    """Deny when the app is disabled.

    Routes are registered once at gateway startup, so a disabled app's endpoints
    stay reachable — the platform's convention is that handlers check enabled
    state themselves. Without this a disabled app could still start a session on
    a reviewer's context, which no teardown hook can retroactively prevent for a
    request already in flight.

    ``is_app_enabled`` reads ``installed.json`` synchronously, so it runs off the
    event loop. Deny-by-default: an unreadable state file disables the surface
    rather than opening it.
    """

    @wraps(handler)
    async def _wrapped(request: web.Request) -> web.Response:
        if not await asyncio.to_thread(is_app_enabled, "code-review-sage"):
            return web.json_response(
                {"code": "app_disabled",
                 "error": "code-review-sage is disabled"}, status=403)
        return await handler(request)

    return _wrapped


@_require_enabled
async def _handle_chat_get(request: web.Request) -> web.Response:
    """GET .../chat?run_id=&change_id= — stored history plus follow-up readiness.

    200 even with nothing to show: "this review was never discussed" is a normal
    state for the panel, not an error.
    """
    run_id = (request.query.get("run_id") or "").strip()
    change_id = (request.query.get("change_id") or "").strip()
    bad = _chat_params(run_id, change_id)
    if bad:
        return web.json_response(
            {"code": bad, "error": "missing run_id or change_id"}, status=400)
    turns = await asyncio.to_thread(
        followup.read_transcript, run_id, change_id)
    async with _LOCK:
        run = _find_run(run_id)
        run_live = run is not None and _is_live(run)
    desc, reason = await asyncio.to_thread(
        followup.resumable, run_id, change_id)
    # A run still in flight is not offerable even with a descriptor on disk: its
    # first pass may be superseded by a second, which retires that descriptor and
    # leaves an already-open conversation pointing at findings the run replaced.
    if run_live:
        desc, reason = None, followup.ERR_RUN_LIVE
    slot_key = followup.slot_key(run_id, change_id)
    return web.json_response({
        "run_id": run_id,
        "change_id": change_id,
        "turns": turns,
        # Whether a follow-up would restore the reviewer's own context. Told to
        # the UI so it can explain why the button is absent instead of offering
        # one that opens a session which knows nothing about the review.
        "resumable": desc is not None,
        "reason": reason,
        "slot_key": slot_key,
        # Whether that session already exists. Without it the panel invites a
        # returning user to "open" a conversation they already had, with no trace
        # of it -- which reads as the review having lost it.
        "followup_open": bool(desc is not None
                              and _followup_session_exists(slot_key)),
    })


def _followup_session_exists(slot_key: str) -> bool:
    """Whether a chat session for this review is actually in the sidebar.

    Read from the SLOT, never from the session map. The two are independent and
    the map deliberately outlives the slot: closing a tab pops it from the slot
    table but keeps its mapping so a later resume can reload the conversation. So
    a mapping proves a session once existed, not that one exists now -- and this
    answer decides whether the panel offers to continue a conversation or to
    create it, which in turn decides whether the slot is handed a title and a
    folder. Deriving it from the map made a closed session read as open, so the
    recreate that followed omitted both and landed unfiled under the placeholder
    title.

    Reads the slot table directly because the dashboard exposes no read-only
    existence check; absent state (tests, a bare app) answers False, which is the
    safe direction -- it offers to create, and creating is idempotent.
    """
    slots = getattr(_APP_STATE.get("state"), "_slots", None)
    if not isinstance(slots, dict):
        return False
    return slot_key in slots


async def _ensure_followup_folder(state: Any) -> str:
    """Find-or-create the folder follow-up sessions are filed under.

    Through ``mutate_folders`` so find-then-create is one transaction: two tabs
    starting a follow-up at the same moment would otherwise each miss the other's
    folder and build a duplicate. Idempotent — an existing folder is adopted and
    nothing is written.
    """
    def _create_or_adopt(folders: list[dict]) -> tuple[bool, str]:
        for f in folders:
            if str(f.get("name", "")).strip() != followup.FOLDER_NAME:
                continue
            # Re-engaging the folder clears `hidden`, matching the folder CRUD
            # handler's own unhide-on-assign rule.
            if f.get("hidden"):
                f["hidden"] = False
                return True, str(f.get("id", ""))
            return False, str(f.get("id", ""))
        folder = {
            "id": uuid.uuid4().hex[:12],
            "name": followup.FOLDER_NAME,
            "order": len(folders),
            "collapsed": False,
            "hidden": False,
            "parent_id": "",
            "project_dir": "",
            # Same stamp the dashboard's own folder creators write; the sidebar's
            # ``created`` folder sort reads it.
            "created_at": time.time(),
        }
        folders.append(folder)
        return True, str(folder["id"])

    try:
        return str(await state.mutate_folders(_create_or_adopt))
    except Exception:  # noqa: BLE001 - filing is a nicety, the session is not
        logger.warning("code-review-sage: follow-up folder write failed",
                       exc_info=True)
        return ""


def _change_title(run_id: str, change_id: str) -> str:
    """The reviewed pull request's title, or "" when it cannot be read."""
    try:
        rec = results.read_result(change_id, None, run_id) or {}
        return store.redact_text(str(rec.get("title") or ""))
    except Exception:  # pragma: no cover - a title is cosmetic
        logger.debug("code-review-sage: could not read change title",
                     exc_info=True)
        return ""


@_require_enabled
async def _handle_followup_start(request: web.Request) -> web.Response:
    """POST .../followup {run_id, change_id} — prepare a session on this review.

    Returns what the caller hands to ``POST /api/chat/slots``: the derived slot
    key, the agent the review ran as, the folder to file it in, and a title. The
    resume itself is already armed when this returns, so the slot's first turn
    loads the reviewer's transcript.
    """
    owner_denied = await require_owner_dashboard_request(request, "code_review_sage.followup_start")
    if owner_denied is not None:
        return owner_denied
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    run_id = str(body.get("run_id") or "").strip()
    change_id = str(body.get("change_id") or "").strip()
    bad = _chat_params(run_id, change_id)
    if bad:
        return web.json_response(
            {"code": bad, "error": "missing run_id or change_id"}, status=400)
    async with _LOCK:
        run = _find_run(run_id)
        run_known = run is not None
        run_live = run is not None and _is_live(run)
    if not run_known:
        return web.json_response(
            {"code": followup.ERR_RUN_GONE,
             "error": "the review this belongs to was deleted"}, status=409)
    if run_live:
        # A first pass can be superseded by a second one in the same run, and that
        # retires the descriptor. Opening now would hand the user a conversation
        # whose findings the run then replaces, with nothing saying so.
        return web.json_response(
            {"code": followup.ERR_RUN_LIVE,
             "error": "this review is still running"}, status=409)

    desc, reason = await asyncio.to_thread(
        followup.resumable, run_id, change_id)
    if desc is None:
        # Literal-status returns rather than one computed `status=`: the
        # error-code contract gate cannot statically verify a site whose status is
        # a variable, so a dynamic status reads as non-compliant even when the
        # body carries a `code`.
        if reason == followup.ERR_TRANSCRIPT_GONE:
            return web.json_response(
                {"code": followup.ERR_TRANSCRIPT_GONE,
                 "error": "the reviewer's session is no longer on disk"},
                status=409)
        return web.json_response(
            {"code": followup.ERR_NO_DESCRIPTOR,
             "error": "this review did not keep a resumable session"},
            status=409)

    state = _APP_STATE.get("state")
    sessions = getattr(state, "sessions", None)
    if sessions is None or not hasattr(sessions, "seed_conversation"):
        return web.json_response(
            {"code": "followup_unavailable",
             "error": "sessions are not available in this context"}, status=503)

    slot_key = followup.slot_key(run_id, change_id)
    # The session-map key a dashboard slot resolves its resume from. Seeded before
    # the slot exists on purpose: the key is derived, so the mapping can be armed
    # first and the slot's very first turn already resumes.
    session_key = f"dashboard:{slot_key}"
    # A slot that already carries a mapping is left alone. Re-seeding would point
    # a follow-up conversation back at the review's own starting transcript and
    # discard everything discussed since.
    mapped = await asyncio.to_thread(sessions.resumable_sid, session_key)
    if not mapped:
        await asyncio.to_thread(
            sessions.seed_conversation, session_key, desc["sid"],
            provider=desc["provider"], cwd=desc["cwd"])
        # Read back rather than trust the write: the session map self-prunes an
        # entry whose files are gone, so this doubles as the last check that the
        # transcript is still there. Refusing here is the point — a slot created
        # without a mapping resumes nothing and then answers anyway, with no idea
        # what was reviewed.
        mapped = await asyncio.to_thread(sessions.resumable_sid, session_key)
    if not mapped:
        return web.json_response(
            {"code": followup.ERR_TRANSCRIPT_GONE,
             "error": "the reviewer's session is no longer on disk"}, status=409)

    folder_id = ""
    if hasattr(state, "mutate_folders"):
        folder_id = await _ensure_followup_folder(state)
    title = followup.slot_title(
        change_id, await asyncio.to_thread(_change_title, run_id, change_id))
    return web.json_response({
        "ok": True,
        "slot_key": slot_key,
        "agent": desc["agent"],
        "folder_id": folder_id,
        "title": title,
    })


async def _followup_sweep_loop() -> None:
    """Age out kept transcripts forever, so they cannot accumulate unbounded."""
    while True:
        try:
            await asyncio.sleep(_FOLLOWUP_SWEEP_INTERVAL)
            dropped = await asyncio.to_thread(followup.prune)
            if dropped:
                logger.info(
                    "code-review-sage: dropped %d idle review transcript(s)",
                    dropped)
        except asyncio.CancelledError:  # pragma: no cover - shutdown
            raise
        except Exception:  # pragma: no cover - never let the sweeper die
            logger.debug("code-review-sage: transcript sweep failed",
                         exc_info=True)


def register_routes(app: web.Application) -> None:
    """Register the deterministic review routes on the gateway app."""
    # Self-heal: ensure the data layout (dirs + config.json with resolved_paths)
    # exists at startup. Without this, the UI gets {} from the generic config
    # endpoint and shows a perpetual "Initializing…" message because it needs
    # resolved_paths.reports to render the main view.
    #
    # OFF the event loop, for the same reason as the reap below: `register_routes`
    # is sync and `start_dashboard` is a coroutine, so anything in this body runs
    # on the loop, and the layout pass walks each of nine directories' ancestor
    # chain to refuse a planted link before creating it. On a network-homed or
    # stalled data directory those walks are exactly the "synchronous filesystem
    # walk" that freezes the gateway heartbeat.
    #
    # An `on_startup` hook keeps the ordering the UI depends on: aiohttp runs
    # these hooks during runner setup, BEFORE the site accepts a connection, so
    # the layout is in place before any request can read `resolved_paths`. The
    # hook is what lets the walk happen in a worker thread while preserving that.
    async def _ensure_layout_on_startup(_app: web.Application) -> None:
        try:
            await asyncio.to_thread(store.ensure_layout)
        except Exception:  # pragma: no cover - never break gateway startup
            logger.warning(
                "code-review-sage: ensure_layout failed at startup", exc_info=True)

    app.on_startup.append(_ensure_layout_on_startup)
    _load_runs()  # restore durable job status (mark orphaned 'running' as 'interrupted')
    # A run dir with no registry entry is unreachable residue (crash between the
    # two writes, or an older layout) — reap it once at startup, but OFF the event
    # loop. `register_routes` is a sync function called from `start_dashboard`,
    # which is a coroutine, so everything here runs on the loop: this reap walks
    # every run dir and deletes the unreferenced ones, and its cost grows with
    # accumulated residue, so on a host with stale runs it stalls gateway startup.
    # The layout pass above is a startup hook for the same reason.
    #
    # `_load_runs` is the one that stays inline, and it is the cheap one: a single
    # `read_text` of one registry file, which returns immediately when the file is
    # absent. What both hooks keep is the ordering the UI depends on -- aiohttp
    # runs `on_startup` before the site accepts a connection, so a request never
    # observes a missing `resolved_paths` or an empty `_RUNS`, which is what it
    # would render as a perpetual "Initializing" message.

    async def _reap_on_startup(_app: web.Application) -> None:
        try:
            reaped = await asyncio.to_thread(_reap_orphan_run_dirs)
            if reaped:
                logger.info("code-review-sage: reaped %d orphan run dir(s)", reaped)
        except Exception:  # pragma: no cover - never break startup
            logger.debug("code-review-sage: orphan reap failed", exc_info=True)

    app.on_startup.append(_reap_on_startup)

    async def _start_followup_sweeper(_app: web.Application) -> None:
        task = asyncio.create_task(_followup_sweep_loop())
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)

    app.on_startup.append(_start_followup_sweeper)
    # Cache the dashboard state so a finished run can push a bell notification
    # from a background task (no request in scope there). Absent in tests that
    # register routes on a bare app — every read site treats it as optional.
    try:
        state = app.get("state")
        if state is not None:
            _APP_STATE["state"] = state
    except Exception:  # pragma: no cover - defensive
        pass
    app.router.add_get("/api/apps/code-review-sage/chat", _handle_chat_get)
    app.router.add_post("/api/apps/code-review-sage/followup",
                        _handle_followup_start)
    app.router.add_post("/api/apps/code-review-sage/review", _handle_review)
    app.router.add_post("/api/apps/code-review-sage/review-repo", _handle_review_repo)
    app.router.add_get("/api/apps/code-review-sage/repo-prs", _handle_repo_prs)
    app.router.add_get("/api/apps/code-review-sage/recent-repos", _handle_recent_repos)
    app.router.add_get("/api/apps/code-review-sage/my-repos", _handle_my_repos)
    app.router.add_get("/api/apps/code-review-sage/review-queue", _handle_review_queue)
    app.router.add_get("/api/apps/code-review-sage/repos", _handle_repos)
    app.router.add_post("/api/apps/code-review-sage/repos", _handle_repos)
    app.router.add_delete("/api/apps/code-review-sage/repos", _handle_repos)
    app.router.add_get("/api/apps/code-review-sage/runs", _handle_runs)
    # Per-run (one thread in the UI). Registered AFTER /runs so the static path
    # is matched first and never shadowed by the {run_id} pattern.
    app.router.add_get("/api/apps/code-review-sage/runs/{run_id}", _handle_run_detail)
    app.router.add_delete("/api/apps/code-review-sage/runs/{run_id}", _handle_run_delete)
    app.router.add_get(
        "/api/apps/code-review-sage/runs/{run_id}/report", _handle_run_report)
    app.router.add_post(
        "/api/apps/code-review-sage/runs/{run_id}/cancel", _handle_run_cancel)
    app.router.add_post(
        "/api/apps/code-review-sage/runs/{run_id}/archive", _handle_run_archive)
    app.router.add_post(
        "/api/apps/code-review-sage/runs/{run_id}/post", _handle_run_post)
    app.router.add_post(
        "/api/apps/code-review-sage/runs/{run_id}/dismiss", _handle_run_dismiss)
    app.router.add_get("/api/apps/code-review-sage/settings", _handle_settings)
    app.router.add_put("/api/apps/code-review-sage/settings", _handle_settings)
    app.router.add_get("/api/apps/code-review-sage/namespaces", _handle_namespaces)
    app.router.add_post("/api/apps/code-review-sage/namespaces", _handle_namespaces)
    app.router.add_delete("/api/apps/code-review-sage/namespaces", _handle_namespaces)
    app.router.add_get("/api/apps/code-review-sage/learnings", _handle_learnings)
    app.router.add_post(
        "/api/apps/code-review-sage/learnings/consolidate", _handle_consolidate)

    async def _shutdown_pool(_app: web.Application) -> None:
        """Retire the reusable review workers when the gateway shuts down."""
        try:
            await review_pool.shutdown_pool()
        except Exception:  # pragma: no cover - defensive
            logger.warning("failed to shut down review pool", exc_info=True)

    # register_app_routes runs before runner.setup() freezes the signal lists,
    # so this append is safe; guarded anyway so it can never break startup.
    try:
        app.on_cleanup.append(_shutdown_pool)
    except Exception:  # pragma: no cover - defensive
        logger.warning("could not register review-pool cleanup hook", exc_info=True)

    logger.info("code-review-sage backend routes registered (deterministic review kickoff)")
