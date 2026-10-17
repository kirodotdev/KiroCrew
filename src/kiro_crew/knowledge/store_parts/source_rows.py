"""Source rows: registration, the properties blob and its one-status rule, and retirement.

A source's ``sync_status`` lives in its column alone (``_without_sync_status`` strips
it from every properties write), a properties rewrite takes the write lock before it
reads (``revise_source_properties``), and a folder a feature registered by itself is
retired to confirmation when the scan funnel refuses it.

A function here whose first parameter is ``self`` is a ``KnowledgeStore`` method:
the class binds it under the same name in ``knowledge/store.py``, so it reads the
store's fields and calls its sibling methods through ``self``.

The ``KnowledgeStore`` fields this module uses. Writes: none. Reads: none; every query
goes through ``self.db``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from kiro_crew._sqlite_compat import sqlite3

if TYPE_CHECKING:
    from kiro_crew.knowledge.store import KnowledgeStore

logger = logging.getLogger("kiro_crew.knowledge.store")


# Marker in a source row's properties for a source Kiro Crew created itself rather
# than the user registering it by hand. Written today by the aggregate row the
# agent's "add document" tool owns, and carried by legacy rows the removed folder
# auto-registration paths left behind. Two readers: the ingestion pipeline, which
# redacts what an auto-added source ingests, and `is_auto_registered` below, which
# the scan funnel consults before it will walk a directory. Lived in the removed
# autosource module until those paths were deleted.
AUTO_ADDED_PROP = "auto_added"


# Marker recording that a row Kiro Crew registered itself has been adopted by the
# user (the auto-registration feature that created such rows is gone). Written by
# `retire_auto_registered_folder` when the scan funnel refuses such a row, and by the
# confirm and resume endpoints when the user adopts one; its presence is what keeps a
# later refusal from undoing that decision.
AUTO_REGISTRATION_RETIRED_PROP = "auto_registration_retired"


def is_auto_registered(props: dict) -> bool:
    """True when *props* belong to a source Kiro Crew registered itself, unadopted.

    Both markers are compared with ``is True`` rather than tested for truthiness.
    ``properties`` is user-editable JSON that also arrives through ``import_bundle``,
    and the string ``"false"`` is truthy: on the retired marker that direction is
    FAIL-OPEN -- the row would read as already adopted and skip the refusal below --
    while on the auto-added marker it would retire a folder the user added by hand.
    Every writer in-tree stores a real boolean, so nothing legitimate is excluded.
    """
    return (
        props.get(AUTO_ADDED_PROP) is True and props.get(AUTO_REGISTRATION_RETIRED_PROP) is not True
    )


# Source types whose scan walks a directory tree, and therefore the only ones
# retirement applies to. The ``agent`` aggregate also carries AUTO_ADDED_PROP -- it is
# auto-added in exactly that sense -- and must not be touched: it holds documents
# handed over one at a time and walks nothing, so gating it would put a Confirm
# control in front of a source that is not a directory. (The ``artifact`` aggregate is
# registered with empty properties and never carries the marker at all; it is excluded
# by type here for the same structural reason.)
_WALKING_SOURCE_TYPES = ("local_folder", "obsidian_vault")


def _without_sync_status(properties):
    """*properties* with any ``sync_status`` key removed.

    The ``sources.sync_status`` COLUMN is the single source of truth for a
    source's sync state: the dashboard list, the watcher's pre-scan skip and
    ``SyncScheduler.sync_all`` all read it. A ``sync_status`` key inside the
    properties JSON is a SECOND store that only the writer touching it observes
    -- the divergence that let a paused folder go on being walked every sweep
    and a vanished file go on rendering 'synced'.

    Callers may still STATE a status in properties at INSERT time (that is the
    channel ``_initial_sync_status`` reads, under an allowlist); it is dropped
    from what gets persisted, so no row carries two answers. After insert the
    column is written explicitly or not at all: a status found in a properties
    write is discarded rather than applied, because a blob read off a legacy row
    carries a value that is stale by definition, and honouring it would let the
    watcher stamp 'missing' back onto a file it had just re-ingested.

    The input is never mutated. A value that is not a JSON object (legacy
    imports hold arrays), an unparsable blob, and a blob without the key all
    pass through unchanged.
    """
    if isinstance(properties, str):
        try:
            parsed = json.loads(properties)
        except (ValueError, TypeError, RecursionError):
            # RecursionError is a RuntimeError, so it needs naming: json.loads
            # recurses per nesting level, and this helper sits on every insert
            # and update path. A pathologically nested blob is left exactly as
            # it was rather than failing the write.
            return properties
        if not isinstance(parsed, dict) or "sync_status" not in parsed:
            return properties
        return json.dumps(_without_sync_status(parsed))
    if not isinstance(properties, dict) or "sync_status" not in properties:
        return properties
    return {k: v for k, v in properties.items() if k != "sync_status"}


def _retire_one_in_txn(self: KnowledgeStore, row, props: dict) -> None:
    """Retire ONE candidate row. Caller holds the write lock and has vetted props.

    The marker is written for every candidate; the status moves only for a row
    that could still be scanned, so a paused row keeps the pause the caller read.
    """
    retired = dict(props)
    retired[AUTO_REGISTRATION_RETIRED_PROP] = True
    if row["sync_status"] == "paused":
        self.db.execute(
            "UPDATE sources SET properties = ? "
            "WHERE id = ? AND properties = ? AND sync_status = 'paused'",
            (_without_sync_status(json.dumps(retired)), row["id"], row["properties"]),
        )
    else:
        self.db.execute(
            "UPDATE sources SET sync_status = 'pending_confirmation', "
            "properties = ? WHERE id = ? AND properties = ? AND sync_status = ?",
            (
                _without_sync_status(json.dumps(retired)),
                row["id"],
                row["properties"],
                row["sync_status"],
            ),
        )
        logger.info(
            "Knowledge source %s was registered automatically by a feature that "
            "no longer exists; it now needs confirmation before it is scanned "
            "again",
            row["id"],
        )


def retire_auto_registered_folder(self: KnowledgeStore, source_id: str) -> bool:
    """Retire ONE auto-registered walking source by id. True when it moved.

    Called by ``FolderWatcher.scan_source`` when it refuses such a row, which is
    the one funnel every scan goes through. Retiring at scan time rather than at
    store open is what makes the coverage complete AND keeps the write off the
    startup path: a row can arrive at any moment -- :meth:`import_bundle` restores
    a bundle's source rows verbatim, so a bundle from an install that had
    auto-registration enabled re-creates one while the gateway is already up --
    and this takes the write lock, which a constructor-time caller could be
    holding the event loop for.

    Synchronous and takes the write lock, so callers on the event loop hand it to
    ``asyncio.to_thread``. False means nothing moved -- not a candidate, or the
    lock was unavailable -- and the sweep refuses to scan the row either way.
    """
    try:
        self.db.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        logger.warning(
            "Could not take the write lock to retire knowledge source %s; the "
            "sweep skips it and the next sweep retries",
            source_id,
            exc_info=True,
        )
        return False
    try:
        row = self.db.execute(
            "SELECT id, properties, sync_status, source_type FROM sources WHERE id = ?",
            (source_id,),
        ).fetchone()
        moved = False
        if row and row["source_type"] in _WALKING_SOURCE_TYPES:
            try:
                props = json.loads(row["properties"] or "{}")
            except (ValueError, TypeError, RecursionError):
                props = None
            if isinstance(props, dict) and is_auto_registered(props):
                self._retire_one_in_txn(row, props)
                moved = True
        self.db.execute("COMMIT")
        return moved
    except Exception:
        self.db.execute("ROLLBACK")
        raise


def merge_source_properties(
    self: KnowledgeStore,
    source_id: str,
    *,
    set_keys: dict | None = None,
    remove_keys: tuple[str, ...] = (),
    sync_status: str | None = None,
    last_synced: str | None = None,
) -> dict | None:
    """Apply a key delta to one source's ``properties``, in ONE write-locked take.

    Returns the properties as persisted, or None when the row is gone.

    The delta is fixed up front; a caller whose new blob depends on what the
    row reads (a counter increment) uses :meth:`revise_source_properties`,
    which this is the fixed-delta form of. The transaction shape and its
    reasons are documented there.
    """

    def revise(props: dict) -> str | None:
        for key in remove_keys:
            props.pop(key, None)
        props.update(set_keys or {})
        return sync_status

    return self.revise_source_properties(source_id, revise, last_synced=last_synced)


def revise_source_properties(
    self: KnowledgeStore,
    source_id: str,
    revise: Callable[[dict], str | None],
    *,
    last_synced: str | None = None,
) -> dict | None:
    """Rewrite one source's ``properties`` from its CURRENT blob, in ONE
    write-locked take.

    *revise* is called with the row's parsed properties under the write lock
    and mutates them in place; it returns the ``sync_status`` to stamp on
    the COLUMN, or None to leave the column alone. *last_synced*, when
    given, lands in the same statement. Returns the properties as
    persisted, or None when the row is gone.

    ``properties`` is a whole-column rewrite, so a read-modify-write split
    across two statements loses a concurrent writer's change: whoever writes
    last replaces the other's blob wholesale. Every same-row writer that
    derives its blob from a read -- the dashboard's pause/resume, the
    watcher's scan stamps, the sync scheduler's outcome counter and the
    ingest finalize's content-hash stamp -- comes through here, so the
    database serializes them against each other: the write lock is taken
    BEFORE the read (``BEGIN IMMEDIATE``, the shape
    :meth:`retire_auto_registered_folder` uses), so no other writer can land
    between this read and this write, and the UPDATE is guarded with the
    blob it read (``WHERE properties = ?``, the shape
    :meth:`_retire_one_in_txn` uses). Under the lock that guard cannot fail,
    which is the point: it states the invariant in SQL, so a future caller
    that drops the transaction gets a no-op rather than a silent overwrite.
    A writer that works from a snapshot it took earlier and rewrites the
    whole blob would resurrect that snapshot over everything committed since
    -- which is why the finalize stamps a delta here instead.

    A failed ``BEGIN IMMEDIATE`` is NOT swallowed here, unlike in
    :meth:`retire_auto_registered_folder`: that sweep gets another pass, a
    request does not, so a lock timeout has to reach the caller instead of
    being reported as a missing row.

    ``sync_status`` is written to the COLUMN and stripped from the blob by
    ``_without_sync_status``, for the reason that helper documents.

    Synchronous and takes the write lock, so an event-loop caller hands it to
    ``asyncio.to_thread``.
    """
    self.db.execute("BEGIN IMMEDIATE")
    try:
        row = self.db.execute(
            "SELECT properties FROM sources WHERE id = ?", (source_id,)
        ).fetchone()
        if row is None:
            self.db.execute("COMMIT")
            return None
        try:
            props = json.loads(row["properties"] or "{}")
        except (ValueError, TypeError, RecursionError):
            props = {}
        if not isinstance(props, dict):
            props = {}
        sync_status = revise(props)
        text = _without_sync_status(json.dumps(props))
        sets = ["properties = ?", "updated_at = ?"]
        params: list = [text, datetime.now().isoformat()]
        if sync_status is not None:
            sets.append("sync_status = ?")
            params.append(sync_status)
        if last_synced is not None:
            sets.append("last_synced = ?")
            params.append(last_synced)
        cur = self.db.execute(
            f"UPDATE sources SET {', '.join(sets)} WHERE id = ? AND properties = ?",  # noqa: S608
            (*params, source_id, row["properties"]),
        )
        self.db.execute("COMMIT")
        return props if cur.rowcount > 0 else None
    except Exception:
        self.db.execute("ROLLBACK")
        raise


def add_source(self: KnowledgeStore, name, source_type, uri, **kwargs) -> str:
    sid = str(uuid4())
    now = datetime.now().isoformat()
    properties = kwargs.get("properties", {})
    stored = _without_sync_status(properties)
    self.db.execute(
        "INSERT INTO sources (id, name, source_type, uri, properties, sync_status, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            sid,
            name,
            source_type,
            uri,
            json.dumps(stored),
            self._initial_sync_status(properties),
            now,
            now,
        ),
    )
    self.db.commit()
    return sid


def get_source_by_uri(self: KnowledgeStore, uri):
    row = self.db.execute("SELECT * FROM sources WHERE uri = ?", (uri,)).fetchone()
    return dict(row) if row else None


_SOURCE_COLUMNS = {
    "name",
    "source_type",
    "uri",
    "properties",
    "last_synced",
    "sync_status",
    "updated_at",
}


def update_source(self: KnowledgeStore, source_id, **fields):
    """Write *fields* to a sources row.

    ``if_sync_status`` makes the write a compare-and-set on the status
    column: the row is written only while it still reads that value. A caller
    deriving a status from a SNAPSHOT it took earlier must pass it, because
    the row can move in between -- a sweep that observed 'missing' and then
    writes 'synced' would otherwise overwrite the 'error' a manual sync
    recorded in the meantime. A caller writing the outcome of something that
    just happened has current information and does not need it.
    """
    expected = fields.pop("if_sync_status", None)
    if not fields:
        return
    if "properties" in fields:
        # The blob is not a place a status can live. Dropping it here means a
        # legacy row's second copy disappears the first time anything writes
        # its properties, and no caller can mint a new one. It is DROPPED,
        # not applied to the column: a blob read off a legacy row carries a
        # stale value, so honouring it would let the watcher stamp 'missing'
        # back onto a file it had just re-ingested. A transition passes
        # sync_status= explicitly.
        fields["properties"] = _without_sync_status(fields["properties"])
    fields["updated_at"] = datetime.now().isoformat()
    safe = {k: v for k, v in fields.items() if k in self._SOURCE_COLUMNS}
    if not safe:
        return
    cols = ", ".join(f"{k} = ?" for k in safe)
    vals = [json.dumps(v) if isinstance(v, (list, dict)) else v for v in safe.values()]
    sql = f"UPDATE sources SET {cols} WHERE id = ?"  # noqa: S608
    params: list = [*vals, source_id]
    if expected is not None:
        # IS, not =, so a NULL column compares as a value rather than
        # silently matching nothing.
        sql += " AND sync_status IS ?"
        params.append(expected)
    self.db.execute(sql, params)
    self.db.commit()
