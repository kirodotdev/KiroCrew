"""The corpus re-embed sweep: re-embedding active items in place under the current
embedding signature.

``rebuild_embeddings`` is the loop the dashboard rebuild and the watcher's self-heal both
run, ``start_rebuild_job`` claims its single-flight job row, and ``count_stale_items`` is
the count the watcher triggers on. The private helpers are the sweep's off-loop SQLite
reads and writes and its per-row embed-and-pace step.

:mod:`kiro_crew.knowledge.ingestion` stays the import path and the patch surface for every
name here: a patch of ``kiro_crew.knowledge.ingestion.bulk_pace_delay`` reaches
``_embed_row_paced`` through :mod:`kiro_crew._facade_mirror`. This module imports the
embedding helpers it uses, never the facade.
"""

from __future__ import annotations

import asyncio
import functools
import time as _time
from datetime import datetime, timedelta
from uuid import uuid4

from kiro_crew.embeddings import PRIORITY_BULK, PRIORITY_NORMAL, bulk_pace_delay

from .embedder import embedder_signature, floats_to_bytes

_REBUILD_BATCH_SIZE = 50

# A rebuild commits progress (refreshing updated_at) at least every batch. A job
# row stuck in 'processing' past this window is from a crash that bypassed cleanup,
# so the single-flight guard treats it as dead and lets a new rebuild start.
_REBUILD_STALE_AFTER = timedelta(minutes=10)

# Items that just failed a re-embed (vec is None) keep a stale sig but get an
# `embedded_at` stamp; the watcher backs off from re-triggering on them until this
# window elapses, so a perpetually-failing item (model not resident) can't drive a fresh
# rebuild every scan interval. Longer than _REBUILD_STALE_AFTER so a legit retry
# isn't suppressed but a tight retrigger loop is.
_REEMBED_RETRY_BACKOFF = timedelta(minutes=15)


def _heartbeat_rebuild_job(store, job_id: str, now_iso: str) -> None:
    """Advance a rebuild job's ``updated_at`` (single write + commit).

    Called via ``asyncio.to_thread`` from the async rebuild loop so the blocking
    SQLite write never runs on the event loop. ``store.db`` resolves to the
    WORKER thread's own connection (per-thread ``threading.local``), so this is
    safe to run off-loop; the loop awaits it serially.
    """
    store.db.execute("UPDATE ingestion_jobs SET updated_at = ? WHERE id = ?", (now_iso, job_id))
    store.db.commit()


def _write_item_embedding(store, item_id: str, blob: bytes, sig: str, now_iso: str, snap) -> bool:
    """Persist a freshly-computed vector for one item; return True if it landed.

    Runs via ``asyncio.to_thread`` (blocking SQLite off the event loop; worker's
    own thread-local connection). The ``updated_at <= snap`` guard skips the
    write when a concurrent re-ingest rewrote the item mid-embed (lost-update
    protection) — ``cur.rowcount == 0`` then, so the caller counts it as failed.
    """
    cur = store.db.execute(
        "UPDATE items SET embedding = ?, embedding_sig = ?, embedded_at = ? "
        "WHERE id = ? AND (updated_at IS NULL OR updated_at <= ?)",
        (blob, sig, now_iso, item_id, snap),
    )
    store.db.commit()
    return bool(cur.rowcount)


def _stamp_embed_attempt(store, item_id: str, now_iso: str) -> None:
    """Stamp ``embedded_at`` after a transient embed failure (leaves sig stale so
    the item is retried, but backs the watcher off it). Offloaded like the other
    per-item writes so it never blocks the event loop.
    """
    store.db.execute("UPDATE items SET embedded_at = ? WHERE id = ?", (now_iso, item_id))
    store.db.commit()


def _init_rebuild_total(store, count_where: str, params_tail: tuple, job_id: str) -> None:
    """Count the rebuild's total items and stamp it on the job row (single commit).

    Runs on a worker thread (``asyncio.to_thread``): the COUNT(*) scans the items
    table and can stall for tens of seconds on a large KB under WAL contention, so
    it must never run inline on the gateway event loop. ``store.db`` is a
    per-thread connection; the UPDATE and its commit stay on this thread's own
    connection.
    """
    total = store.db.execute(
        f"SELECT COUNT(*) AS c FROM items WHERE {count_where}", params_tail  # noqa: S608
    ).fetchone()["c"]
    store.db.execute(
        "UPDATE ingestion_jobs SET items_total = ?, updated_at = ? WHERE id = ?",
        (total, datetime.now().isoformat(), job_id),
    )
    store.db.commit()


def _fetch_rebuild_page(store, page_where: str, params_tail: tuple, last_id: str):
    """Fetch one keyset page of items to re-embed (worker thread; see above)."""
    return store.db.execute(
        f"SELECT id, title, summary, content, updated_at FROM items WHERE {page_where} "  # noqa: S608
        "ORDER BY id LIMIT ?",
        (*params_tail, last_id, _REBUILD_BATCH_SIZE),
    ).fetchall()


def _commit_rebuild_progress(store, job_id: str | None, processed: int, failed: int) -> None:
    """Write end-of-batch progress counters and commit (worker thread; see above)."""
    if job_id is not None:
        store.db.execute(
            "UPDATE ingestion_jobs SET items_processed = ?, items_failed = ?, "
            "updated_at = ? WHERE id = ?",
            (processed, failed, datetime.now().isoformat(), job_id),
        )
    store.db.commit()


def _select_active_rebuild_job(store, *, now: datetime | None = None):
    """Return a FRESH (within the staleness window) active rebuild job row, or None.

    A corpus-wide rebuild is identified by ``source_id IS NULL`` + ``status='processing'``.
    Rows older than the staleness window are crashed leftovers and are NOT returned
    (callers sweep them). Must be called inside an open transaction by the claimer.
    """
    fresh = ((now or datetime.now()) - _REBUILD_STALE_AFTER).isoformat()
    return store.db.execute(
        "SELECT id FROM ingestion_jobs WHERE source_id IS NULL AND status = 'processing' "
        "AND updated_at > ? ORDER BY created_at DESC LIMIT 1",
        (fresh,),
    ).fetchone()


def start_rebuild_job(store, *, now: datetime | None = None) -> str | None:
    """Atomically claim the single-flight slot for a corpus rebuild.

    Wraps check-then-insert in a single ``BEGIN IMMEDIATE`` transaction so the
    watcher's 300s tick and a user-clicked rebuild can't both observe "no active
    job" and each insert one (the per-path single-flight guard was not safe across
    paths). Also sweeps any stale ``processing`` rows from prior crashes to
    ``abandoned`` so they don't accumulate as phantom jobs forever.

    Returns the new ``job_id`` if this caller claimed the slot, or ``None`` if a
    fresh rebuild is already in flight (caller should not start one).
    """
    now = now or datetime.now()
    ts = now.isoformat()
    fresh = (now - _REBUILD_STALE_AFTER).isoformat()
    store.db.execute("BEGIN IMMEDIATE")
    try:
        if _select_active_rebuild_job(store, now=now) is not None:
            store.db.execute("COMMIT")
            return None
        # Sweep crashed leftovers (stale 'processing' rows) so they don't linger.
        store.db.execute(
            "UPDATE ingestion_jobs SET status = 'abandoned', "
            "error = 'abandoned: updated_at past staleness window', updated_at = ? "
            "WHERE source_id IS NULL AND status = 'processing' AND updated_at <= ?",
            (ts, fresh),
        )
        job_id = uuid4().hex[:12]
        store.db.execute(
            "INSERT INTO ingestion_jobs (id, source_id, status, created_at, updated_at) "
            "VALUES (?, NULL, 'processing', ?, ?)",
            (job_id, ts, ts),
        )
        store.db.execute("COMMIT")
        return job_id
    except Exception:
        store.db.execute("ROLLBACK")
        raise


def count_stale_items(store, sig: str, *, now: datetime | None = None) -> int:
    """Count active items whose embedding sig is stale and not in retry backoff.

    Excludes items re-attempted within ``_REEMBED_RETRY_BACKOFF`` (stale sig but a
    recent ``embedded_at``) so a perpetually-failing item can't make the watcher
    re-trigger every scan interval.
    """
    cutoff = ((now or datetime.now()) - _REEMBED_RETRY_BACKOFF).isoformat()
    return store.db.execute(
        "SELECT COUNT(*) AS c FROM items WHERE status = 'active' "
        "AND (embedding_sig IS NULL OR embedding_sig != ?) "
        "AND (embedded_at IS NULL OR embedded_at < ?)",
        (sig, cutoff),
    ).fetchone()["c"]


def _embed_row_paced(
    embedder, title, summary, content, priority: int, pace: bool
) -> "tuple[list[float] | None, float]":
    """Embed one row and derive its pace delay, both on the worker thread.

    Runs via ``run_in_executor`` — the inference is the CPU floor, and the
    delay is computed here rather than on the event loop because
    ``bulk_pace_delay`` re-reads the duty cycle from ``config.json`` on every
    call (an uncached stat + read + parse that must not run per row on the
    gateway loop; the no-blocking-call-on-event-loop rule). This is the same
    division the vector-memory sweep uses: measure and derive on the sweep's
    own thread, never on the loop. Measuring around the embed call itself also
    keeps the executor queue wait out of the paced elapsed. The delay derives
    from measured elapsed time (0.0 for ``elapsed <= 0``), so a row that
    failed fast paces to nothing on its own — failures need no separate
    branch. Returns ``(vec, delay)``; ``delay`` is always 0.0 when unpaced.
    """
    started = _time.monotonic()
    vec = embedder.embed_for_item(title, summary, content, priority=priority)
    delay = bulk_pace_delay(_time.monotonic() - started) if pace else 0.0
    return vec, delay


async def rebuild_embeddings(
    store, embedder, *, job_id: str | None = None, force: bool = False, pace: bool = True
) -> int:
    """Re-embed active items in place, stamping the current embedding signature.

    Sig-gated by default: only items whose stored ``embedding_sig`` differs from the
    current setup (or is NULL) are re-embedded, which makes the operation idempotent
    — a partial-failure retry skips already-done items, and a re-run on an unchanged
    setup is a no-op. ``force=True`` re-embeds every active item regardless of sig
    (escape hatch for suspected vector corruption).

    Vectors are overwritten one item at a time so search stays queryable throughout.
    When ``job_id`` is given, progress is written to that ``ingestion_jobs`` row; the
    same function powers the dashboard trigger and the watcher self-heal. Returns the
    number of items successfully re-embedded.

    Serial single-item embed (the in-process embedder is the CPU floor and fans out
    internally); batch size is only the commit/progress cadence, not a throttle.

    ``pace`` keys the sweep's resource envelope on attendance. This is an
    unattended corpus loop when the watcher self-heal fires it — to a user, a
    full-corpus re-embed at full speed is indistinguishable from a runaway
    process — so the paced default embeds at ``PRIORITY_BULK`` (the reduced
    ``memory.embedding_bulk_threads`` pool) and idles between rows per
    ``memory.embedding_bulk_duty`` (see :func:`kiro_crew.embeddings.bulk_pace_delay`).
    Paced is the default so a caller that forgets the argument gets the quiet
    behaviour. ``pace=False`` is for a sweep a human explicitly asked for and is
    watching a progress bar on: it embeds at ``PRIORITY_NORMAL`` and never idles.
    ``pace`` selects the scheduling class as well as the idling — an attended
    sweep that only skipped the pauses would stay on the reduced bulk pool and
    run several times slower than before pacing existed, on exactly the path
    declared "full speed".
    """
    loop = asyncio.get_running_loop()
    sig = embedder_signature(embedder)
    priority = PRIORITY_BULK if pace else PRIORITY_NORMAL
    processed = 0
    failed = 0
    # Keep the COUNT predicate and the page predicate as separate strings so neither
    # is derived by stripping a clause out of the other (a string-replace that would
    # silently no-op if the WHERE were ever reworded).
    if force:
        count_where = "status = 'active'"
        page_where = "status = 'active' AND id > ?"
        params_tail: tuple = ()
    else:
        count_where = "status = 'active' AND (embedding_sig IS NULL OR embedding_sig != ?)"
        page_where = count_where + " AND id > ?"
        params_tail = (sig,)

    if job_id is not None:
        # OFFLOADED: the total COUNT scans the items table and can stall on a
        # large KB; an inline call blocks the event loop (loop-watchdog risk).
        await asyncio.to_thread(_init_rebuild_total, store, count_where, params_tail, job_id)

    last_id = ""
    while True:
        # OFFLOADED: page reads can block behind a concurrent writer's
        # busy_timeout; keep every DB touch in this loop off the event loop.
        rows = await asyncio.to_thread(_fetch_rebuild_page, store, page_where, params_tail, last_id)
        if not rows:
            break
        for row in rows:
            # functools.partial rather than a local closure: run_in_executor
            # forwards positional args only, and the partial names the bound
            # arguments at the call site instead of one hop away in a nested
            # def. Embed + delay derivation both happen on the worker thread
            # (see _embed_row_paced); only the idle itself runs here.
            vec, delay = await loop.run_in_executor(
                None,
                functools.partial(
                    _embed_row_paced,
                    embedder,
                    row["title"],
                    row["summary"],
                    row["content"],
                    priority,
                    pace,
                ),
            )
            if delay > 0:
                # Idle between this row's inference and its write — the
                # interruption-safe point: a sweep killed mid-pause leaves the
                # row's sig stale and the next sweep re-embeds it, the same
                # idempotent contract every row it never reached already has.
                # ``await asyncio.sleep`` is this coroutine's equivalent of the
                # vector-memory sweep's on-thread pause: it yields the event
                # loop and holds neither the DB connection nor the model, so an
                # interactive embed arriving mid-pause is served at full speed.
                await asyncio.sleep(delay)
            now_iso = datetime.now().isoformat()
            # Per-item SQLite writes are OFFLOADED (asyncio.to_thread): a sync
            # write can block up to the busy_timeout under a concurrent writer,
            # and rebuild_embeddings is awaited on the gateway event loop — an
            # inline write would freeze chat/liveness (the
            # no-blocking-call-on-event-loop rule). store.db is a per-thread connection
            # (threading.local, WAL); the loop awaits each serially, so there is
            # no concurrent-connection use.
            if vec:
                # Guard against a lost update: if ingestion (file-change re-ingest)
                # rewrote title/content while we were embedding, its updated_at moved
                # past our snapshot's -- skip our stale-vector UPDATE and let that
                # item re-embed via its own _embed_item. ``snap`` is the row's
                # updated_at at read time.
                snap = row["updated_at"]
                landed = await asyncio.to_thread(
                    _write_item_embedding,
                    store,
                    row["id"],
                    floats_to_bytes(vec),
                    sig,
                    now_iso,
                    snap,
                )
                if landed:
                    processed += 1
                else:
                    failed += 1  # raced with a concurrent writer; counts as not-done
            else:
                # Transient embed failure: leave sig stale (so it's retried) but stamp
                # embedded_at as the attempt time so the watcher backs off this item.
                await asyncio.to_thread(_stamp_embed_attempt, store, row["id"], now_iso)
                failed += 1
            last_id = row["id"]
            if job_id is not None:
                # Heartbeat the job row PER ITEM, not just per batch: a single embed
                # is the CPU floor, so 50 serial embeds can exceed
                # _REBUILD_STALE_AFTER on a slow/cold host. If updated_at only
                # advanced at end-of-batch, the single-flight claimer would judge a
                # live rebuild abandoned mid-batch and start a second one (duplicated
                # embedding work). Committing the timestamp each item keeps the job
                # demonstrably alive within the staleness window. The heavier
                # progress counters still land once per batch below.
                #
                # OFFLOAD the write: rebuild_embeddings is awaited on the gateway
                # event loop, and a synchronous SQLite write can block up to the
                # busy_timeout when another writer holds the lock — freezing chat /
                # liveness (the no-blocking-call-on-event-loop rule). ``store.db`` is
                # a per-thread connection (threading.local, WAL), so the worker
                # thread safely uses its OWN connection to the same db; the loop
                # awaits it serially, so there is no concurrent-connection use.
                await asyncio.to_thread(_heartbeat_rebuild_job, store, job_id, now_iso)
        # OFFLOADED end-of-batch progress write + commit (same no-blocking rule).
        await asyncio.to_thread(_commit_rebuild_progress, store, job_id, processed, failed)

    return processed
