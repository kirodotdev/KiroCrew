"""Embedding status, the batch embed that fills missing vectors, and the background
rebuild job that re-embeds the corpus.

Served through :mod:`kiro_crew.dashboard.handlers.knowledge`, which callers and tests import
and patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import read_bounded_json
from kiro_crew.dashboard.handlers.knowledge_access import (
    _require_knowledge_owner,
    _sel_log,
    _store,
    _task_registry,
)
from kiro_crew.knowledge.embedder import embedder_signature, floats_to_bytes
from kiro_crew.knowledge.ingestion import rebuild_embeddings, start_rebuild_job

logger = logging.getLogger("kiro_crew.dashboard.handlers.knowledge")


# ---------- Route registration ----------


def _embedding_counts(store) -> tuple[int, int]:
    """(total active items, active items with a vector) in one off-loop take.

    Sync on purpose: the dashboard polls the status endpoint repeatedly, and
    running these COUNTs on the gateway loop busy-waits every task (watchdog
    heartbeat included) whenever the knowledge DB is contended. The caller
    dispatches this to a worker thread; ``store.db`` is thread-local, so the
    thread gets its own connection.
    """
    total = store.db.execute("SELECT COUNT(*) as c FROM items WHERE status = 'active'").fetchone()[
        "c"
    ]
    embedded = store.db.execute(
        "SELECT COUNT(*) as c FROM items WHERE status = 'active' AND embedding IS NOT NULL"
    ).fetchone()["c"]
    return total, embedded


async def get_embedding_status(request: web.Request) -> web.Response:
    """GET /api/knowledge/embedding/status -- embedding config and progress."""
    owner_denied = await _require_knowledge_owner(request, "knowledge.embedding.status.read")
    if owner_denied is not None:
        return owner_denied
    store = _store(request)
    embedder = request.app.get("knowledge_embedder")
    total, embedded = await asyncio.to_thread(_embedding_counts, store)
    # Polled every 30s by the frontend — loop-safe probe.
    available = await embedder.is_available_async() if embedder else False
    return web.json_response(
        {
            "enabled": embedder is not None,
            "available": available,
            "model": embedder.model if embedder else None,
            "total_items": total,
            "embedded_items": embedded,
        }
    )


def _finalize_job(
    store, job_id: str, status: str, *, processed: int | None = None, error: str | None = None
) -> bool:
    """Stamp a terminal state on an ingestion job row, in one off-loop take.

    True when this call is the one that moved the row. Only a row still
    ``processing`` is moved, and that compare-and-set is what stops a second
    finalize from overwriting the first: a shutdown cancel delivered while the
    success finalize is in its worker lands in the caller's
    ``except BaseException`` arm, which would otherwise stamp 'cancelled' over
    the 'completed' that already committed. Reporting the rowcount is what lets
    the caller's audit line record what happened rather than what it attempted
    -- see ``_finalize_job_audited``.

    One take per logical write, for the reason ``_set_sync_status`` documents.
    """
    if processed is not None:
        cur = store.db.execute(
            "UPDATE ingestion_jobs SET status = ?, items_processed = ?, updated_at = ? "
            "WHERE id = ? AND status = 'processing'",
            (status, processed, datetime.now().isoformat(), job_id),
        )
    else:
        cur = store.db.execute(
            "UPDATE ingestion_jobs SET status = ?, error = ?, updated_at = ? "
            "WHERE id = ? AND status = 'processing'",
            (status, error, datetime.now().isoformat(), job_id),
        )
    store.db.commit()
    return cur.rowcount > 0


def _finalize_job_audited(
    store,
    job_id: str,
    status: str,
    *,
    fields: dict,
    processed: int | None = None,
    error: str | None = None,
) -> bool:
    """Finalize a rebuild job and audit the outcome, in ONE off-loop take.

    The audit rides inside the take for the reason ``_audited_write`` documents,
    but this caller cannot use that helper: the line has to be *conditional* on
    the compare-and-set having moved the row. Both terminal paths of a rebuild
    job race -- a shutdown cancel is delivered to the coroutine after the
    success finalize already committed 'completed' -- and the loser's CAS moves
    nothing. Auditing anyway records an outcome the job never reached: a
    'cancelled' line for a run that completed and embedded every vector.
    Gating on the rowcount means exactly one outcome line is written per job,
    and it names the state the row actually holds.
    """
    moved = _finalize_job(store, job_id, status, processed=processed, error=error)
    if moved:
        _sel_log("batch_embed", **fields)
    return moved


def _active_rebuild_job(store):
    """The rebuild job already holding the single-flight slot, in one off-loop take."""
    return store.db.execute(
        "SELECT id FROM ingestion_jobs WHERE source_id IS NULL AND status = 'processing' "
        "ORDER BY created_at DESC LIMIT 1"
    ).fetchone()


def _unembedded_rows(store) -> list:
    """Up to 200 active items still missing an embedding, in one off-loop take.

    Sync on purpose: the predicate is not indexed, so this scans ``items``, which
    grows without bound. The caller dispatches this to a worker thread;
    ``store.db`` is thread-local, so the thread gets its own connection.
    """
    return store.db.execute(
        "SELECT id, title, summary, content FROM items "
        "WHERE status = 'active' AND embedding IS NULL LIMIT 200"
    ).fetchall()


def _write_embedding(store, item_id: str, vector: bytes, sig: str) -> None:
    """Store one item's vector, in one off-loop take.

    Per item rather than per batch on purpose: the connection runs in autocommit
    mode, so each write is already durable on its own, and deferring writes to
    batch them would lose finished embeddings if the request were cancelled.
    """
    store.db.execute(
        "UPDATE items SET embedding = ?, embedding_sig = ?, embedded_at = ? WHERE id = ?",
        (vector, sig, datetime.now().isoformat(), item_id),
    )
    store.db.commit()


def _unembedded_count(store) -> int:
    """How many active items still have no embedding, in one off-loop take."""
    return store.db.execute(
        "SELECT COUNT(*) as c FROM items WHERE status = 'active' AND embedding IS NULL"
    ).fetchone()["c"]


async def _rebuild_embeddings_job(
    app: web.Application, store, embedder, job_id: str, force: bool = False
) -> None:
    """Background wrapper: run the sig-gated rebuild and finalize the job row.

    The re-embed loop itself lives in ``knowledge.ingestion.rebuild_embeddings`` so
    the watcher self-heal path shares one implementation. Vectors are overwritten
    one item at a time, so existing vectors stay queryable throughout -- search
    degrades gracefully during the rebuild instead of going dark.

    PRIORITY_NORMAL, stated rather than defaulted: this is the ATTENDED rebuild --
    a user clicked it and is polling the job row for progress -- so it keeps the
    full interactive pool. The watcher's unattended twin passes PRIORITY_BULK, and
    the difference is the point.
    """
    try:
        # pace=False: this job exists because a human clicked Rebuild and is
        # watching its progress bar — the load is expected, so it runs at the
        # interactive scheduling class with no idling. The watcher self-heal
        # path stays on the paced default.
        processed = await rebuild_embeddings(
            store, embedder, job_id=job_id, force=force, pace=False
        )
        # Audit from inside the worker, gated on the CAS: a cancel delivered
        # while this await is in flight would otherwise skip the completed line
        # (it is the coroutine that dies, not the thread), and a cancel that
        # already stamped the row must not collect a completed line either.
        await asyncio.to_thread(
            _finalize_job_audited,
            store,
            job_id,
            "completed",
            processed=processed,
            fields={"count": processed, "rebuild": True, "force": force, "outcome": "completed"},
        )
    except BaseException as exc:
        # CancelledError is a BaseException in 3.8+; finalize the row so a
        # shutdown cancellation does not leave it 'processing'.
        is_cancel = isinstance(exc, asyncio.CancelledError)
        status = "cancelled" if is_cancel else "failed"
        if is_cancel:
            logger.debug("Embedding rebuild job %s cancelled", job_id)
        else:
            logger.exception("Embedding rebuild job %s failed", job_id)
        # Off-loop like every other write here, on the cancellation path too. A
        # re-cancel can interrupt this await before the worker returns, but the
        # thread runs to completion, so the write still lands; and even if the
        # process dies first, `start_rebuild_job` sweeps a stale 'processing'
        # row to 'abandoned', so the single-flight guard cannot stay blocked.
        # Suppressing the re-cancel keeps `exc` current for the bare `raise`.
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.to_thread(
                _finalize_job_audited,
                store,
                job_id,
                status,
                error=str(exc),
                fields={"rebuild": True, "force": force, "outcome": status},
            )
        if is_cancel:
            raise


async def batch_embed_items(request: web.Request) -> web.Response:
    """POST /api/knowledge/embedding/generate -- embed unembedded items, or re-embed all.

    ``{"rebuild": true}`` re-embeds every active item; because that can span the
    whole corpus it runs as a background job and returns a ``job_id`` to poll via
    ``GET /api/knowledge/jobs/{id}``. The default (fill-NULL) path stays synchronous
    since it only touches items missing an embedding at cold start.
    """
    owner_denied = await _require_knowledge_owner(request, "knowledge.embedding.generate")
    if owner_denied is not None:
        return owner_denied
    store = _store(request)
    embedder = request.app.get("knowledge_embedder")
    if not embedder:
        return web.json_response({"error": "Embedding not enabled"}, status=400)
    if not await embedder.is_available_async():
        return web.json_response({"error": "Embedding model not available"}, status=503)

    body, body_err = await read_bounded_json(request, max_bytes=None, allow_absent=True)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    rebuild = body.get("rebuild", False)
    force = body.get("force", False)

    if rebuild:
        # Single-flight: atomically claim the slot (sweeps crashed leftovers, races
        # safely against the watcher self-heal). None -> a rebuild is already running.
        # Offloaded: start_rebuild_job runs a blocking BEGIN IMMEDIATE write-lock
        # acquisition (busy_timeout up to 10s), which must never block the gateway
        # event loop (no-blocking-call-on-event-loop).
        job_id = await asyncio.to_thread(start_rebuild_job, store)
        if job_id is None:
            active = await asyncio.to_thread(_active_rebuild_job, store)
            return web.json_response(
                {"job_id": active["id"] if active else None, "status": "processing"}
            )
        task = asyncio.create_task(
            _rebuild_embeddings_job(request.app, store, embedder, job_id, force=force)
        )
        app_tasks = _task_registry(request.app, "_bg_tasks")
        app_tasks.add(task)
        task.add_done_callback(app_tasks.discard)
        return web.json_response({"job_id": job_id, "status": "processing"})

    rows = await asyncio.to_thread(_unembedded_rows, store)

    loop = asyncio.get_running_loop()
    sig = embedder_signature(embedder)
    embedded = 0
    for row in rows:
        vec = await loop.run_in_executor(
            None, embedder.embed_for_item, row["title"], row["summary"], row["content"]
        )
        if vec:
            await asyncio.to_thread(_write_embedding, store, row["id"], floats_to_bytes(vec), sig)
            embedded += 1

    remaining = await asyncio.to_thread(_unembedded_count, store)
    _sel_log("batch_embed", count=embedded, rebuild=False)
    return web.json_response({"embedded": embedded, "total": len(rows), "remaining": remaining})
