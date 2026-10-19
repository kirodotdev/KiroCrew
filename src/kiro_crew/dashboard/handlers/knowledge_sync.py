"""Syncing a source: its status row, the sync claim, and the sync tasks.

``POST /api/knowledge/sources/{id}/sync`` and the work it hands off -- a connector's
sync, a local file's re-ingest, the artifact source's reconcile, an agent source's
URL fetch -- with the status writes that must land even when the task is cancelled.
Also the source-row read and the unique source insert the add and ingest routes share.

Served through :mod:`kiro_crew.dashboard.handlers.knowledge`, which callers and tests import
and patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import tempfile
from pathlib import Path

from aiohttp import web

from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.dashboard.handlers.files import _PathProbeBusy, _run_path_probe
from kiro_crew.dashboard.handlers.knowledge_access import (
    _pipeline,
    _require_knowledge_owner,
    _sel_log,
    _store,
    _task_registry,
)
from kiro_crew.knowledge.agent_fetch import fetch_url_content
from kiro_crew.knowledge.agent_source import AGENT_SOURCE_URI
from kiro_crew.knowledge.artifact_ingest import ARTIFACT_SOURCE_URI, ArtifactKnowledgeSync
from kiro_crew.knowledge.ingestion import ImportChunkBudgetError, _redact, handoff_gate_context
from kiro_crew.knowledge.llm_pool import LLMPool
from kiro_crew.security import is_sensitive_path

logger = logging.getLogger("kiro_crew.dashboard.handlers.knowledge")


def _source_row(store, source_id: str):
    """One source row by id, in one off-loop take.

    Sync on purpose: a primary-key read is cheap only when the connection is
    free, and these handlers run while a sync holds the write lock -- the
    connection's busy timeout is 10s, which on the loop is most of the
    watchdog's budget. The caller dispatches this to a worker thread;
    ``store.db`` is thread-local, so the thread gets its own connection.
    """
    return store.db.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()


def _set_sync_status(store, source_id: str, status: str) -> None:
    """Stamp a source's ``sync_status``, in one off-loop take.

    The connection runs in autocommit mode (``isolation_level=None``), so the
    ``commit()`` is a no-op kept for uniformity with the module's other writers
    and to stay correct if that ever changes. What one take buys is that the
    statements of ONE logical write cannot interleave with another caller's.
    ``store.db`` is thread-local, so the worker gets its own connection.
    """
    store.db.execute("UPDATE sources SET sync_status = ? WHERE id = ?", (status, source_id))
    store.db.commit()


def _finalize_sync_status_write(
    store, source_id: str, status: str, from_statuses: tuple[str, ...] = ("syncing",)  # type: ignore[no-untyped-def]
) -> bool:
    """CAS a terminal state over a row still mid-sync, in one off-loop take.

    Compare-and-set for the reason ``_finalize_job`` documents: the terminal
    paths race. A shutdown cancel can be delivered to the coroutine after the
    success write's worker already committed 'synced', and a blind second
    write would stamp 'error' over the state the work actually reached. Only
    a row still in one of *from_statuses* is moved, so the loser's write
    lands on nothing. The upload path passes ('syncing', 'pending') because a
    cancel can land before its best-effort 'syncing' stamp, while the freshly
    inserted row still reads the INSERT default 'pending'.
    """
    marks = ", ".join("?" for _ in from_statuses)
    cur = store.db.execute(
        f"UPDATE sources SET sync_status = ? WHERE id = ? AND sync_status IN ({marks})",
        (status, source_id, *from_statuses),
    )
    store.db.commit()
    return cur.rowcount > 0


async def _finalize_sync_status(
    store, source_id: str, status: str, from_statuses: tuple[str, ...] = ("syncing",)  # type: ignore[no-untyped-def]
) -> None:
    """Write a terminal ``sync_status`` from a finalizer, and never raise.

    Runs inside an ``except BaseException`` arm, so an exception escaping here
    would mask the original one -- a failed DB write is logged and dropped
    instead. A re-cancel can interrupt the ``await`` before the worker returns;
    a worker already RUNNING completes, so a write in flight still lands (only
    a re-cancel that arrives before the executor picks the item up can drop
    it). Suppressing the re-cancel keeps the caller's original exception
    current for its bare ``raise`` (the same shape as
    ``_rebuild_embeddings_job``'s finalize).
    """
    try:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.to_thread(
                _finalize_sync_status_write, store, source_id, status, from_statuses
            )
    except Exception:
        logger.exception("Could not finalize sync_status=%r for source %s", status, source_id)


async def _write_status_cancel_safe(store, source_id: str, status: str) -> None:  # type: ignore[no-untyped-def]
    """Write *status* so that a cancel cannot strand the row at 'syncing'.

    An exception raised INSIDE an ``except`` arm is not caught by its sibling
    arms, so a cancel delivered during a status write made from one (the
    budget-deferral 'pending' writes) would otherwise propagate with the row
    still 'syncing'. The worker also outlives the cancel, so it is drained
    first; whatever it landed, the CAS finalize then moves only a row still
    'syncing' -- a landed write leaves it nothing to do -- and the cancel is
    re-raised.
    """
    write = asyncio.ensure_future(asyncio.to_thread(_set_sync_status, store, source_id, status))
    try:
        await asyncio.shield(write)
    except asyncio.CancelledError:
        with contextlib.suppress(BaseException):
            await write
        await _finalize_sync_status(store, source_id, "error")
        raise


def _claim_sync(store, source_id: str) -> bool:
    """Move a source into 'syncing' and report whether THIS call won it.

    The UPDATE's own WHERE clause is the guard, so the check and the write are
    one statement: two concurrent syncs of one source cannot both start, however
    the callers interleave. A read the caller re-checks cannot promise that once
    an ``await`` sits between the read and the write.

    Claimed by the background task rather than the handler, so a client
    disconnect cannot commit 'syncing' and then fail to schedule the work that
    clears it.
    """
    cur = store.db.execute(
        "UPDATE sources SET sync_status = 'syncing' " "WHERE id = ? AND sync_status <> 'syncing'",
        (source_id,),
    )
    store.db.commit()
    return cur.rowcount > 0


def _add_source_unique(store, **kwargs) -> tuple[str, bool]:
    """Insert a source, or name the row that already holds its ``uri``.

    Returns ``(source_id, created)``. ``sources.uri`` is UNIQUE, so the insert
    itself decides: two concurrent adds of one uri cannot both create a row, and
    the loser is told which row won instead of getting a 500 from the
    constraint. One off-loop take, so the insert and the recovery read cannot
    straddle an await.
    """
    try:
        return store.add_source(**kwargs), True
    except sqlite3.IntegrityError:
        row = store.get_source_by_uri(kwargs["uri"])
        if row is None:
            raise  # a different constraint, not the uri collision
        return row["id"], False


def _is_syncable(source) -> bool:  # type: ignore[no-untyped-def]
    """False for the aggregate Auto-added source, whose uri is a control uri."""
    return source["uri"] != AGENT_SOURCE_URI


async def _claim_sync_for_task(  # type: ignore[no-untyped-def]
    store, source_id: str, claim_settled: asyncio.Event | None
) -> bool:
    """Take a source's 'syncing' claim from a background sync task.

    Returns whether THIS task won the claim. ``False`` also covers a claim that
    could not be taken at all, which leaves the row's status untouched.
    *claim_settled* is set once the answer is known either way, which is what
    ``_hand_off_under_gate`` waits on.
    """
    claim = asyncio.ensure_future(asyncio.to_thread(_claim_sync, store, source_id))
    try:
        try:
            return bool(await asyncio.shield(claim))
        except asyncio.CancelledError:
            # A cancel here does not stop the worker: the thread runs to
            # completion and can still commit 'syncing' after this task is
            # gone -- the same strand the work sites in the sync tasks close.
            # Wait for the thread's answer and finalize only a claim THIS task
            # won; a lost claim is a sibling sync's row. Everything is
            # suppressed because a finalizer must never replace the
            # cancellation.
            with contextlib.suppress(BaseException):
                if await claim:
                    await _finalize_sync_status(store, source_id, "error")
            raise
        except Exception:
            # Failing to TAKE the work is not the work failing. 'error' is terminal --
            # sync_all skips an errored row on every sweep -- so stamping it for a
            # transient writer-lock timeout would quiesce a healthy source until
            # someone re-syncs by hand. Leave the row as it was and let the next sweep
            # retry it. Caught rather than left to propagate: these tasks carry
            # `add_done_callback(set.discard)`, which never retrieves an exception.
            logger.exception(
                "Could not claim sync for source %s; leaving its status untouched", source_id
            )
            return False
    finally:
        if claim_settled is not None:
            claim_settled.set()


def _resolved_is_sensitive(path: str) -> bool:
    """Whether *path* resolves to a sensitive location.

    Blocking (``Path.resolve``), so ``_ingest_local_file_task`` runs it through
    ``_run_path_probe``.
    """
    return is_sensitive_path(str(Path(path).resolve()))


async def _ingest_local_file_task(  # type: ignore[no-untyped-def]
    pipeline, store, path: str, source_id: str, *, claim_settled: asyncio.Event | None = None
) -> None:
    """Re-ingest a local_file source via the FileReader pipeline.

    Shared by add_source (initial ingest) and sync_source (manual re-sync) so both
    entry points route local files through the same FileReader path and apply the
    same read-time sensitive-path re-validation (defense-in-depth against TOCTOU).

    Owns the row's whole sync lifecycle: it claims 'syncing' atomically, and
    then stamps 'synced', 'pending' when the import budget defers the ingest,
    or 'error'. Holding both ends here is what stops a cancelled request from
    leaving a row 'syncing' with no work behind it. It returns without touching
    the row at all in two cases: another sync already holds the claim, or the
    claim could not be taken -- neither is this source failing to sync.

    *claim_settled* is set once the claim has been decided either way: a
    scheduling caller that holds the ingestion gate across this task's claim
    (``sync_source``) waits on it, so the orphan sweep cannot take an itemless
    row in a terminal status between that caller's read and the claim here.
    """
    if not await _claim_sync_for_task(store, source_id, claim_settled):
        return
    try:
        try:
            restricted = await _run_path_probe(_resolved_is_sensitive, path)
        except _PathProbeBusy:
            # Transient, like the budget deferral below: 'pending' keeps the
            # file in the sweep, where 'error' would quiesce it for good.
            logger.warning("Ingestion deferred, path probe pool busy: %s", path)
            await _write_status_cancel_safe(store, source_id, "pending")
            return
        if restricted:
            _sel_log("source.sensitive_path_blocked", path=path, source_id=source_id)
            logger.warning("Sensitive path detected at ingest time: %s", path)
            await asyncio.to_thread(_set_sync_status, store, source_id, "error")
            return
        await pipeline.ingest_file(path, source_id=source_id)
        await asyncio.to_thread(_set_sync_status, store, source_id, "synced")
    except ImportChunkBudgetError as exc:
        # A budget deferral is transient, so it must not land in 'error': sync_all
        # skips an errored source, which would quiesce this local_file permanently
        # over a window that clears in a minute. 'pending' keeps it in the sweep,
        # and the file on disk is still there to re-read -- the same test that
        # keeps 'pending' off an upload, whose only copy is the unlinked temp file.
        # SyncScheduler.sync_source treats this exception the same way.
        logger.warning("Ingestion deferred by import budget for %s: %s", path, exc)
        await _write_status_cancel_safe(store, source_id, "pending")
    except BaseException as exc:
        # CancelledError is a BaseException in 3.8+, so an ``except Exception``
        # arm lets a shutdown cancel skip finalization and strand the row at
        # 'syncing' -- every later sync then answers 409 with no recovery. A
        # cancelled sync lands in 'error' like any other incomplete sync (no
        # new status value: the settings UI and the watcher read this column),
        # and the cancellation is re-raised so task semantics are preserved --
        # shutdown drains ``_bg_tasks`` by cancelling them. The shape follows
        # ``_rebuild_embeddings_job``, the precedent in ``knowledge_embeddings``.
        is_cancel = isinstance(exc, asyncio.CancelledError)
        if is_cancel:
            logger.debug("Background ingestion cancelled for %s", path)
        else:
            logger.exception("Background ingestion failed for %s", path)
        await _finalize_sync_status(store, source_id, "error")
        if is_cancel:
            raise


async def _hand_off_under_gate(  # type: ignore[no-untyped-def]
    request: web.Request, pipeline, make_task
) -> asyncio.Task[None]:
    """Start a background task while the ingestion gate is held, and keep the
    hold until the task reports the row is protected.

    The handler's own row read happens before the task exists, and an itemless
    row in a terminal status is what the post-bind orphan sweep reclaims, so
    without the hold the sweep could delete the row in that gap -- the client
    told "syncing" / "processing" for a source that is gone. *make_task*
    receives an event the task sets once the row is safe from the sweep: a
    sync task once its 'syncing' claim is decided (``_claim_sync``), an upload
    task once it holds its own gate. The caller enters the gate BEFORE this
    call so its own lookup is covered too, and the gate's per-task re-entrancy
    makes that outer hold the one that counts. The task is created in a
    hand-off context: it takes a real hold of its own, admitted past any
    maintenance window that began waiting while this hold is open, so the
    wait below cannot deadlock against that window (``handoff_gate_context``).
    """
    settled: asyncio.Event = asyncio.Event()
    async with pipeline.ingestion_in_flight():
        # The task takes its own hold for its ingest, so it must not inherit
        # this one through the copied context (it would read as nested).
        task = asyncio.create_task(make_task(settled), context=handoff_gate_context())
        app_tasks = _task_registry(request.app, "_bg_tasks")
        app_tasks.add(task)
        task.add_done_callback(app_tasks.discard)
        await settled.wait()
    return task


async def sync_source(request: web.Request) -> web.Response:
    """POST /api/knowledge/sources/{id}/sync -- trigger sync for a source."""
    owner_denied = await _require_knowledge_owner(request, "knowledge.source.sync")
    if owner_denied is not None:
        return owner_denied
    pipeline = _pipeline(request)
    if pipeline is None:
        return await _sync_source_body(request)
    # The gate is held from the row read through the task hand-off: an itemless
    # row in a terminal status is what the post-bind orphan sweep reclaims, and
    # the read here is what the branches below act on.
    async with pipeline.ingestion_in_flight():
        return await _sync_source_body(request)


async def _sync_source_body(request: web.Request) -> web.Response:
    source_id = request.match_info["id"]
    store = _store(request)
    source = await asyncio.to_thread(_source_row, store, source_id)
    if not source:
        return web.json_response({"error": "not found"}, status=404)

    sync_scheduler = request.app.get("knowledge_sync")
    if sync_scheduler:
        connector = sync_scheduler.get_connector(source["source_type"])
        if connector:
            result = await sync_scheduler.sync_source(source_id)
            _sel_log("source.sync", source_id=source_id)
            return web.json_response(result)

    # The aggregate Artifacts source is kept in step with the artifact store by
    # ArtifactKnowledgeSync, not fetched from its uri: 'artifact://' is not a
    # URL, and the agent URL-fetch fallback below can only fail on it.
    if source["uri"] == ARTIFACT_SOURCE_URI:
        return await _sync_artifact_source(request, store, source)

    # The aggregate Auto-added source has nothing to fetch: its documents arrive
    # only when the agent submits text through knowledge_add_document. 'agent://'
    # is a control uri, so the URL-fetch fallback below could only fail and leave
    # the row in a sticky 'error'. Refuse, and leave the status as it is.
    if not _is_syncable(source):
        return web.json_response(
            {
                "error": "this source has nothing to sync",
                "code": "source_not_syncable",
                "source_id": source_id,
            },
            status=409,
        )

    # local_file sources carry a filesystem path, not a URL -- re-ingest the file
    # directly through the FileReader pipeline (same path as add_source), never the
    # agent URL-fetch fallback below (which would hand the path to ReadInternalWebsites
    # and fail with "Invalid URL format").
    if source["source_type"] == "local_file":
        file_uri = source["uri"] or ""
        if not file_uri:
            return web.json_response({"error": "no file path to sync"}, status=400)
        if source["sync_status"] == "syncing":
            return web.json_response(
                {"error": "sync already in progress", "source_id": source_id}, status=409
            )
        pipeline = _pipeline(request)
        if not pipeline:
            return web.json_response({"error": "pipeline not configured"}, status=503)
        # The task claims the row; this read is only the fast 409 for the common
        # case, so a lost claim ends the task rather than double-starting a sync.
        await _hand_off_under_gate(
            request,
            pipeline,
            lambda settled: _ingest_local_file_task(
                pipeline, store, file_uri, source_id, claim_settled=settled
            ),
        )
        _sel_log("source.sync.local_file", source_id=source_id)
        return web.json_response({"synced": False, "status": "syncing", "source_id": source_id})

    # Agent-assisted sync: fetch in background, no chat session needed
    uri = source["uri"] or ""
    props = (
        json.loads(source["properties"] or "{}")
        if isinstance(source["properties"], str)
        else (source["properties"] or {})
    )
    url = uri or props.get("url", "")
    if not url:
        return web.json_response({"error": "no URL to fetch"}, status=400)

    if source["sync_status"] == "syncing":
        return web.json_response(
            {"error": "sync already in progress", "source_id": source_id}, status=409
        )

    pipeline = _pipeline(request)
    if not pipeline:
        return web.json_response({"error": "pipeline not configured"}, status=503)
    pool = request.app.get("knowledge_fetch_pool")
    if pool is None:
        # Every production construction site sets knowledge_fetch_pool; a
        # minimal/hand-built app missing it fails loudly rather than silently
        # running URL sync through the extraction pool.
        return web.json_response(
            {
                "error": "knowledge_fetch_pool is not configured on this application",
                "code": "knowledge_fetch_pool_unavailable",
            },
            status=503,
        )
    await _hand_off_under_gate(
        request,
        pipeline,
        lambda settled: _background_agent_sync(
            source_id, url, source["name"], store, pipeline, pool, claim_settled=settled
        ),
    )
    _sel_log("source.sync.agent", source_id=source_id, url=url)
    return web.json_response({"synced": False, "status": "syncing", "source_id": source_id})


async def _sync_artifact_source(request: web.Request, store, source) -> web.Response:  # type: ignore[no-untyped-def]
    """Manual Sync on the aggregate Artifacts source: run its reconcile pass.

    The pass is the repair that runs at every gateway start (ingest what the
    artifact store has and the Library lacks, drop what is gone), run here in
    the background under the sync's own lock. The row survives
    ``knowledge.auto_ingest_artifacts`` being switched off, and then no sync
    object is running to do the pass, so the request is refused with the row's
    status left as it is.
    """
    source_id = source["id"]
    sync = request.app.get("artifact_knowledge_sync")
    if sync is None:
        return web.json_response(
            {
                "error": "artifact auto-ingest is disabled",
                "code": "artifact_auto_ingest_disabled",
                "source_id": source_id,
            },
            status=409,
        )
    if source["sync_status"] == "syncing":
        return web.json_response(
            {
                "error": "sync already in progress",
                "code": "sync_in_progress",
                "source_id": source_id,
            },
            status=409,
        )
    pipeline = _pipeline(request)
    if not pipeline:
        return web.json_response(
            {"error": "pipeline not configured", "code": "pipeline_unavailable"}, status=503
        )
    await _hand_off_under_gate(
        request,
        pipeline,
        lambda settled: _reconcile_artifact_source_task(
            sync, store, source_id, claim_settled=settled
        ),
    )
    _sel_log("source.sync.artifact", source_id=source_id)
    return web.json_response({"synced": False, "status": "syncing", "source_id": source_id})


async def _reconcile_artifact_source_task(  # type: ignore[no-untyped-def]
    sync: ArtifactKnowledgeSync,
    store,
    source_id: str,
    *,
    claim_settled: asyncio.Event | None = None,
) -> None:
    """Background task: reconcile the aggregate Artifacts source, then stamp it.

    Claims 'syncing' first, for the reason ``_ingest_local_file_task`` documents.
    Each artifact the pass ingests stamps the row itself as it lands, so the
    closing 'synced' is a compare-and-set over a row still 'syncing': a
    terminal state one of those ingests wrote is left as it is. A failure of the
    pass itself, or a cancel, ends in 'error'.
    """
    if not await _claim_sync_for_task(store, source_id, claim_settled):
        return
    try:
        await sync.reconcile_now(source_id)
        await asyncio.to_thread(_finalize_sync_status_write, store, source_id, "synced")
    except BaseException as exc:
        # ``except BaseException`` for the reason _ingest_local_file_task
        # documents: a cancel must not strand the row at 'syncing'.
        is_cancel = isinstance(exc, asyncio.CancelledError)
        if is_cancel:
            logger.debug("Artifact source reconcile cancelled: source=%s", source_id)
        else:
            logger.exception("Artifact source reconcile failed: source=%s", source_id)
        await _finalize_sync_status(store, source_id, "error")
        if is_cancel:
            raise


async def _background_agent_sync(  # type: ignore[no-untyped-def]
    source_id: str,
    url: str,
    name: str,
    store,
    pipeline,
    pool: LLMPool,
    *,
    claim_settled: asyncio.Event | None = None,
) -> None:
    """Background task: fetch content via agent, then ingest.

    Claims 'syncing' first, for the reason ``_ingest_local_file_task`` documents:
    the claim is atomic and the task owns both ends of the row's lifecycle.
    *claim_settled* is set once the claim is decided (see that task).
    """
    if not await _claim_sync_for_task(store, source_id, claim_settled):
        return
    try:
        content = await fetch_url_content(url, pool)
        redacted = _redact(content)
        content = redacted if redacted is not None else content
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".md", prefix="agent_sync_")
        try:
            tmp.write(content.encode())
            tmp.close()
            await pipeline.ingest_file(tmp.name, original_name=name, source_id=source_id)
        finally:
            Path(tmp.name).unlink(missing_ok=True)
        await asyncio.to_thread(_set_sync_status, store, source_id, "synced")
        logger.info("Agent sync complete: source=%s url=%s", source_id, url)
    except ImportChunkBudgetError as exc:
        # Transient, so not 'error': sync_all skips an errored source, which would
        # quiesce this agent-url source permanently over a window that clears in a
        # minute. The URL is re-fetchable, so a retry has content to act on.
        logger.warning(
            "Agent sync deferred by import budget: source=%s url=%s: %s", source_id, url, exc
        )
        await _write_status_cancel_safe(store, source_id, "pending")
    except BaseException as exc:
        # ``except BaseException`` for the reason _ingest_local_file_task
        # documents: a cancel must not strand the row at 'syncing'.
        is_cancel = isinstance(exc, asyncio.CancelledError)
        if is_cancel:
            logger.debug("Agent sync cancelled: source=%s url=%s", source_id, url)
        else:
            logger.exception("Agent sync failed: source=%s url=%s", source_id, url)
        await _finalize_sync_status(store, source_id, "error")
        if is_cancel:
            raise
