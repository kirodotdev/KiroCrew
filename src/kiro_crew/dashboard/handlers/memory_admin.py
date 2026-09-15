"""Owner-only administration of the memory stores themselves.

The sibling ``memory.py`` serves the CONTENTS of one store — preferences,
semantic rows, episodes, stats — in the global store or an owner-selected store. This module answers the operator's questions ABOUT the stores: which
ones exist, what is in each, what was retired out of one, what copies survive of
it.

**Every route here is owner-gated unconditionally**, via
:func:`require_owner_dashboard_request`, because each one either enumerates every
silo or mutates a store. The routes that additionally take a store also run it
through :func:`resolve_requested_memory_store`, whose own gate fires on the
parameter's PRESENCE — so a route is never reachable by a non-owner merely by
omitting the store. The unconditional gate is what closes that, and it is also
what makes an agent structurally unable to reach these routes at all: the gate
requires a ``request["user"]`` that ``token_auth_middleware`` publishes on the
cookie/query-token path only, never on the ``X-Internal-Secret`` branch that
kiro-cli, the MCP servers and subagents authenticate over.

Two consequences worth stating before reading the handlers:

* **The store list is best-effort PER STORE.** One damaged silo must not hide
  every healthy one from the picker, so each store's probe is wrapped on its own
  and a store that cannot be read reports itself unreadable instead of failing
  the response.
* **No response carries a filesystem path.** A backup is addressed by its
  stamped NAME, which is also the handle ``POST /api/memory/restore`` takes;
  returning a path would disclose the data-home layout, and for a silo the
  ``memory_stores/<name>/`` layout the keystone fence exists to keep out of the
  browser.

There is deliberately no delete route — see the module spec.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import closing
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew import memory_backup, memory_schema
from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.memory_stores import (
    DEFAULT_MEMORY_STORE,
    declared_store_names,
    owned_store_path,
)

from ._shared import (
    _admin_store,
    _audit,
    _redact_memory_field,
    _store_name,
    _store_unavailable,
    read_bounded_json,
    require_owner_dashboard_request,
    vector_memory_for_store,
)

logger = logging.getLogger(__name__)

#: Rows one page of retired episodes may return. Matches the ceiling
#: ``api_memory_episodic_list`` puts on live episodes, because a retired row
#: carries the same full episode text: the payload width is identical, so the
#: two list surfaces have no reason to disagree about how much of it one GET
#: serializes (CWE-770).
MAX_RETIRED_PAGE = 100

#: Default page size when the caller names none, matching
#: :data:`memory_schema.DEFAULT_FACET_PAGE` so every memory list surface pages
#: alike.
DEFAULT_RETIRED_PAGE = memory_schema.DEFAULT_FACET_PAGE

#: LIKE pattern discriminating a lesson from any other semantic row, the same
#: discrimination the engine applies inline wherever it needs one. Named here so
#: the count below cannot drift from it by a re-typed literal.
_LESSON_KEY_PATTERN = "lesson.%"

#: The three per-store counts, as ONE statement so a probe costs one round trip.
#: Both lineages are queried through the v1 relation NAMES — real tables on a v1
#: file, read-only views on a crew silo — which is what lets one statement answer
#: for either without asking which lineage it is first.
#:
#: ``lessons_count`` is a SUBSET of ``semantic_count``, not a fourth partition. A
#: lesson IS a semantic row (a ``directive`` on the crew lineage), and both
#: ``/api/memory/stats``'s ``semantic_active`` and the ``/api/memory/semantic``
#: list count it as one — so subtracting it here would make this the only surface
#: that disagrees with the tab rendered beside it.
_STORE_COUNTS_SQL = (
    "SELECT "
    "(SELECT COUNT(*) FROM semantic_memory WHERE is_deleted = 0), "
    "(SELECT COUNT(*) FROM episodic_memories WHERE is_deleted = 0), "
    "(SELECT COUNT(*) FROM semantic_memory WHERE is_deleted = 0 AND key LIKE ?)"
)

#: How a stamped backup name is rendered back to the browser. UTC with an
#: explicit ``Z``, so the value is unambiguous on the wire and the frontend owns
#: the locale-aware formatting.
_ISO_UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _taken_at(backup: Path) -> str | None:
    """When *backup*'s contents were taken, read from its STAMPED NAME.

    Never from its mtime: a restore copies a file and resets that, while the name
    still says when the contents were captured — the same reason
    ``memory_backup.list_backups`` sorts by its parsed stamp. ``None`` for a name this format
    cannot parse, which the caller reports rather than guessing at.

    Reaches for ``memory_backup``'s own stamp parser rather than re-typing it. The
    writer, the retention window's age test and this reader must agree on one
    format, and a second copy of it here is how a renamed stamp starts reading as
    unparseable in exactly one of the three.
    """
    try:
        if backup.suffix == ".zip":
            from kiro_crew.member_memory_backup import snapshot_time

            return snapshot_time(backup).strftime(_ISO_UTC_FORMAT)
        stamp = memory_backup.snapshot_time(backup)
    except ValueError:
        return None
    return stamp.strftime(_ISO_UTC_FORMAT)


def _unprobed_store_row(name: str) -> dict[str, Any]:
    """The row for a store nothing could be read from.

    Null counts rather than zeros. Zeros would read as "this store is empty",
    which for a silo whose file is merely unreadable is a wrong answer to the one
    question the picker asks — and the operator would go looking for the memory
    rather than for the file.
    """
    return {
        "name": name,
        "is_default": name == DEFAULT_MEMORY_STORE,
        "lineage": None,
        "exists": False,
        "semantic_count": None,
        "episodic_count": None,
        "lessons_count": None,
        "facets_supported": False,
        "backup_count": 0,
        "newest_backup": None,
    }


def _probe_store_blocking(name: str) -> dict[str, Any]:
    """One store's row: its lineage, its three counts and its backup summary.

    Blocking end to end — a path resolution, a directory listing, a ``stat`` per
    backup and a read-only SQLite open — so the caller runs the whole enumeration
    in a worker thread.

    ``exists`` answers whether the counts beside it are REAL: it is true only when
    the file opened and carried a memory schema. A store that has never been
    written has no file yet, and a file that is present but holds no product
    table cannot be counted, so both report ``false`` with null counts instead of
    three zeros.

    The connection is READ-ONLY, and through ``memory_backup``'s own URI builder
    so the two readers escape a data-home path identically: interpolating one into
    ``file:...`` lets a ``?`` or ``#`` in it be parsed as the URI's query or
    fragment, truncating the path so a DIFFERENT database is opened. A probe of
    every declared store must also be incapable of creating, migrating or
    otherwise touching one — opening a silo read-write to count it would stand up
    the very file whose absence the row is reporting.
    """
    row = _unprobed_store_row(name)
    # Resolve and confirm the declared store's path before opening it. A missing
    # or invalid named identity must never probe the global file under its label.
    path = owned_store_path(name)
    if path is None:
        return row
    try:
        backups = memory_backup.list_backups(path)
    except OSError:
        # An unreadable backups directory costs the backup summary, not the
        # counts: the two answers are independent and one of them is still real.
        logger.warning("could not list the backups of memory store %r", name, exc_info=True)
        backups = []
    row["backup_count"] = len(backups)
    row["newest_backup"] = _taken_at(backups[0]) if backups else None
    from kiro_crew.memory_startup import MemoryStartupUnavailable, require_memory_ready

    try:
        require_memory_ready(name)
    except MemoryStartupUnavailable as exc:
        row["unavailable_reason"] = _redact_memory_field(str(exc))
        return row
    if not path.is_file():
        return row
    with closing(sqlite3.connect(memory_backup._read_only_uri(path), uri=True)) as db:
        lineage = memory_schema.detect_lineage(db)
        if lineage is None:
            return row
        counts = db.execute(_STORE_COUNTS_SQL, (_LESSON_KEY_PATTERN,)).fetchone()
    row["lineage"] = lineage
    row["exists"] = True
    row["facets_supported"] = lineage == memory_schema.LINEAGE_CREW
    row["semantic_count"] = int(counts[0])
    row["episodic_count"] = int(counts[1])
    row["lessons_count"] = int(counts[2])
    return row


def _list_stores_blocking() -> list[dict[str, Any]]:
    """Every declared store's row, in :func:`declared_store_names` order.

    FAIL SOFT PER STORE, which is why the loop is here rather than at the
    handler: one silo with a corrupt file, a stale WAL sibling or a permission
    problem must not cost the operator the whole picker. Its row still lists,
    marked unreadable.

    The order is NOT re-sorted. ``declared_store_names`` puts the default store
    first and sorts the rest, and two passes over one install that disagree on
    order are how the picker and a report stop describing the same list.
    """
    from kiro_crew.config.loader import KiroCrewConfig

    config = KiroCrewConfig.load()
    rows: list[dict[str, Any]] = []
    for name in declared_store_names():
        try:
            rows.append(_probe_store_blocking(name))
        except Exception:
            logger.warning(
                "could not probe memory store %r; listing it unreadable", name, exc_info=True
            )
            rows.append(_unprobed_store_row(name))
        record = config.memory_stores.get(name)
        owner = getattr(record, "owner_member", "")
        rows[-1]["owner_member"] = owner
        # Reuse the roster's validated avatar descriptor and exact member name.
        # A store UUID is storage identity, never the seed for a different face.
        member = config.agents.get(owner)
        rows[-1]["owner_avatar"] = member.avatar if member is not None else {}
        rows[-1]["memory_version"] = getattr(record, "memory_version", 1)
    return rows


async def api_memory_stores(request: web.Request) -> web.Response:
    """GET /api/memory/stores — every declared store, for the picker.

    Owner-gated unconditionally and takes no ``?store=``: it enumerates every
    silo, so there is no reading of it that is not the operator's. An unknown
    query key is ignored rather than refused, because a request legitimately
    carries keys that are not parameters (``?token=`` among them).

    ``active`` names the store the page reads when it sends NO ``?store=`` — which
    is the GLOBAL store, because that is what every store-scoped content route
    resolves an absent parameter to. The picker
    needs it so it can display that store as selected while still leaving the
    parameter off: sending ``?store=default`` is semantically identical and
    behaviourally worse, since the parameter's presence takes the owner gate, and
    on an install with no configured owner that turns a working page into refusals.

    Deliberately NOT the caller's recorded session binding, even though that reads
    as the more informative answer. The binding is reachable only through
    ``X-Session-Key``, which is accepted on the caller's word, so reporting it here
    would invite the picker to follow a header no gate has checked — and the routes
    it drives do not follow that header either, so it would name a store the page
    is not showing. Derived from the same constant the resolver branches on rather
    than restated, so the two cannot disagree about what an absent parameter means.
    """
    denial = await require_owner_dashboard_request(request, "memory.stores.list")
    if denial is not None:
        return denial
    stores = await asyncio.to_thread(_list_stores_blocking)
    return web.json_response({"stores": stores, "active": DEFAULT_MEMORY_STORE})


async def api_memory_retired(request: web.Request) -> web.Response:
    """GET /api/memory/retired — episodes a semantic write superseded.

    The recovery half of the conflict-retire rule: nothing here hard-deletes an
    episode, so the row and its full text survive a tombstone, and this is the
    only way to look at them. A user's own delete is NOT listed — the engine's
    join is scoped to the ``conflict_retire`` / ``semantic_update`` pair, because
    only one of the two was a guess.

    ``ts`` is when the row was retired (its most recent retirement, when it has
    been retired more than once), not when it was written: the question this
    surface answers is "what did the rule take, and when".
    """
    denial = await require_owner_dashboard_request(request, "memory.retired.list")
    if denial is not None:
        return denial
    store, refusal = await _admin_store(request, "memory.retired.list")
    if refusal is not None:
        return refusal
    # Parsed before the store is resolved: a 400 should not have paid for a silo's
    # first ``init()``.
    try:
        limit = min(int(request.query.get("limit", str(DEFAULT_RETIRED_PAGE))), MAX_RETIRED_PAGE)
        offset = int(request.query.get("offset", "0"))
    except (ValueError, TypeError):
        return web.json_response(
            {"error": "limit/offset must be integers", "code": "invalid_pagination"}, status=400
        )
    state: DashboardState = request.app["state"]
    vectors = await vector_memory_for_store(state, store)
    if vectors is None:
        return _store_unavailable(store)
    # Offload: the read serializes on the store's _db_lock, and a worker holding
    # it would otherwise block the gateway event loop here.
    rows = await asyncio.to_thread(
        vectors.get_retired_episodic, limit=max(1, limit), offset=max(0, offset)
    )
    retired = [
        {
            "id": row.get("id"),
            "text": _redact_memory_field(row.get("text")),
            "superseded_by": _redact_memory_field(row.get("superseded_by")),
            "retired_times": row.get("retired_times"),
            "ts": row.get("retired_at"),
        }
        for row in rows
    ]
    return web.json_response({"retired": retired})


async def api_memory_retired_restore(request: web.Request) -> web.Response:
    """POST /api/memory/retired/restore — clear one episode's tombstone in place.

    Restores rather than re-inserting, so the row keeps its id, its text, its
    vector and its ``created_at``; a re-insert would look like a new memory and
    would re-enter the similarity dedup that may have been what removed it.
    """
    denial = await require_owner_dashboard_request(request, "memory.retired.restore")
    if denial is not None:
        return denial
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    store, refusal = await _admin_store(request, "memory.retired.restore", body)
    if refusal is not None:
        return refusal
    raw = body.get("id")
    mem_id = raw.strip() if isinstance(raw, str) else ""
    if not mem_id:
        return web.json_response(
            {"error": "id is required", "code": "invalid_episode_id"}, status=400
        )
    state: DashboardState = request.app["state"]
    vectors = await vector_memory_for_store(state, store)
    if vectors is None:
        return _store_unavailable(store)
    # Offload: acquires the store's _db_lock internally.
    ok = await asyncio.to_thread(vectors.restore_episodic, mem_id)
    if not ok:
        return web.json_response(
            {
                "error": "no retired episode with that id",
                "code": "unknown_retired_episode",
            },
            status=404,
        )
    await _audit(request, "memory.retired.restore", "success", f"{_store_name(store)}:{mem_id}")
    return web.json_response({"ok": True})


def _list_backups_blocking(db_path: Path) -> list[dict[str, Any]]:
    """*db_path*'s backups as wire rows, newest first.

    A ``stat`` per file, hence blocking. A file that vanished between the listing
    and the ``stat`` — a concurrent prune, or the heartbeat's own pass — is
    dropped rather than reported with a null size: the restore route cannot use
    a missing backup.
    """
    rows: list[dict[str, Any]] = []
    for backup in memory_backup.list_backups(db_path):
        try:
            size = backup.stat().st_size
        except OSError:
            continue
        rows.append({"name": backup.name, "size_bytes": int(size), "taken_at": _taken_at(backup)})
    return rows


async def api_memory_backups(request: web.Request) -> web.Response:
    """GET /api/memory/backups — the hot copies of one store.

    Returns the stamped NAME and never a path: the name is the handle
    ``POST /api/memory/restore`` takes, while a path would disclose the data-home
    layout and, for a silo, the ``memory_stores/<name>/`` layout the keystone
    fence exists to keep out of the browser.
    """
    denial = await require_owner_dashboard_request(request, "memory.backups.list")
    if denial is not None:
        return denial
    store, refusal = await _admin_store(request, "memory.backups.list")
    if refusal is not None:
        return refusal
    db_path = owned_store_path(_store_name(store))
    if db_path is None:
        return _store_unavailable(store)
    backups = await asyncio.to_thread(_list_backups_blocking, db_path)
    result: dict[str, Any] = {"backups": backups}
    try:
        result.update(await asyncio.to_thread(memory_backup.pending_restore_status, db_path))
    except (ValueError, OSError) as exc:
        return web.json_response(
            {"error": _redact_memory_field(str(exc)), "code": "restore_status_unavailable"},
            status=503,
        )
    return web.json_response(_redact_memory_field(result))


async def api_memory_restore_cancel(request: web.Request) -> web.Response:
    """POST /api/memory/restore/cancel — cancel an owned pending restore."""
    denial = await require_owner_dashboard_request(request, "memory.restore.cancel")
    if denial is not None:
        return denial
    body, error = await read_bounded_json(request)
    if error is not None:
        return error
    assert body is not None
    store, error = await _admin_store(request, "memory.restore.cancel", body)
    if error is not None:
        return error
    db_path = owned_store_path(_store_name(store))
    if db_path is None:
        return _store_unavailable(store)
    try:
        cancelled = await asyncio.to_thread(memory_backup.cancel_pending_restore, db_path)
    except (ValueError, OSError) as exc:
        return web.json_response(
            {"error": _redact_memory_field(str(exc)), "code": "restore_refused"}, status=409
        )
    await _audit(request, "memory.restore.cancel", "cancelled" if cancelled else "unchanged", store)
    from kiro_crew.memory_startup import memory_restore_startup_status

    return web.json_response(
        _redact_memory_field(
            {
                "ok": True,
                "cancelled": cancelled,
                "pending": False,
                "restart_required": False,
                "pending_restore": None,
                **memory_restore_startup_status(_store_name(store)),
            }
        )
    )


def _backup_now_blocking(db_path: Path) -> dict[str, int]:
    """Copy *db_path* now and prune, reporting the same four counters the
    unattended sweep does.

    The sweep's MINIMUM INTERVAL is deliberately NOT applied. That guard exists
    because the heartbeat's tick counter is per process and resets on every
    gateway start, so without it a restart loop collapses the retention window;
    an operator pressing "back up now" is asking for a copy, and silently
    declining for the next twenty hours would make the control do nothing at all.

    ``skipped`` therefore carries ``backup_store``'s other documented outcome —
    there was nothing to copy, because the store has never been opened or its
    file is empty — which is distinct from ``failed``, where a copy was attempted
    and did not land.
    """
    from kiro_crew.config.loader import KiroCrewConfig

    result = {"backed_up": 0, "skipped": 0, "pruned": 0, "failed": 0}
    try:
        keep = KiroCrewConfig.load().memory.backup_keep
        if memory_backup.backup_store(db_path) is None:
            result["skipped"] = 1
            return result
        result["backed_up"] = 1
        result["pruned"] = memory_backup.prune_backups(db_path, keep=keep)
    except memory_backup.MemoryBackupFailed:
        result["failed"] = 1
        logger.warning("memory backup failed for %s", db_path, exc_info=True)
    except Exception:
        result["failed"] = 1
        logger.warning("memory backup pass failed for %s", db_path, exc_info=True)
    return result


async def api_memory_backup(request: web.Request) -> web.Response:
    """POST /api/memory/backup — take one store's hot copy now.

    ONE store, the one the body names — not the whole install. ``back_up_all_stores``
    stays the unattended sweep's entry point, where backing up every declared store
    and declining a copy inside the retention interval are both right; a control
    the operator pressed is about the store they are looking at.

    Uses SQLite's online backup API rather than a file copy, so the copy is
    consistent with the gateway still writing: ``memory.db`` alone would capture a
    file whose committed tail lives in a ``-wal`` sibling it did not take, and the
    result parses cleanly while missing recent writes.

    The store is never OPENED here, only resolved. ``init()`` runs
    ``PRAGMA journal_mode=WAL``, which raises ``file is not a database`` on exactly
    the corrupt file a backup-and-restore pair exists to recover from, so opening
    first would make this surface unreachable in the situation it is for.
    """
    denial = await require_owner_dashboard_request(request, "memory.backup")
    if denial is not None:
        return denial
    body, body_err = await read_bounded_json(request, allow_absent=True)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    store, refusal = await _admin_store(request, "memory.backup", body)
    if refusal is not None:
        return refusal
    db_path = owned_store_path(_store_name(store))
    if db_path is None:
        return _store_unavailable(store)
    result = await asyncio.to_thread(_backup_now_blocking, db_path)
    await _audit(
        request,
        "memory.backup",
        "success" if not result["failed"] else "error",
        _store_name(store),
    )
    return web.json_response(result)


def _resolve_backup_name(db_path: Path, name: str) -> Path | None:
    """*name* resolved INSIDE *db_path*'s backup directory, or ``None``.

    Validate-then-re-check-after-composition, the pairing
    ``memory_stores._named_store_dir`` uses, because validation and use are
    separated by a call boundary:

    1. *name* must be a single path segment. ``Path(dir) / "/etc/passwd"``
       evaluates to ``/etc/passwd`` — an absolute right-hand side overrides the
       base — so an absolute or separator-bearing name has to be refused BEFORE
       the join, not caught after it. Both separators are checked: ``a\\b`` is one
       segment to posixpath and two on Windows.
    2. The resolved path must be EXACTLY the composed one. Identity, not
       containment: a containment test refuses a link that escapes the directory
       and ACCEPTS one that redirects inside it, which is enough to restore
       another store's file under this store's name. The directory is resolved
       first, so a root reached through a symlinked ancestor (``/tmp`` on macOS)
       still passes.

    Step 1 also covers the traversal spellings on its own: ``Path("..").name`` and
    ``Path(".").name`` are both ``""``, so neither survives the segment test.
    """
    if not name or name != Path(name).name or "\\" in name:
        return None
    out_dir = memory_backup.backup_dir_for(db_path)
    try:
        root = out_dir.resolve()
        candidate = (root / name).resolve()
    except OSError:
        return None
    if candidate != root / name:
        return None
    return candidate


def _restore_blocking(db_path: Path, backup: Path, store: str) -> str | None:
    """Stage recovery without displacing a live database or its WAL."""
    memory_backup.restore_from_backup(backup, store)
    return None


async def api_memory_restore(request: web.Request) -> web.Response:
    """POST /api/memory/restore — put one of a store's backups back in place.

    The caller supplies a backup NAME, never a path, and it is resolved inside
    that store's own backup directory; see :func:`_resolve_backup_name` for the
    containment rule. ``restore_from_backup`` refuses a source that fails its
    integrity check BEFORE displacing anything, so a damaged backup cannot leave
    the operator worse off than they already were.

    Both lineages stage a verified copy and require a gateway restart. The
    startup barrier preserves the prior memory before activation, including
    V1's WAL. No live connection is renamed, reopened or deprived of sidecars.
    """
    from kiro_crew.memory_startup import MemoryStartupUnavailable

    denial = await require_owner_dashboard_request(request, "memory.restore")
    if denial is not None:
        return denial
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    store, refusal = await _admin_store(request, "memory.restore", body)
    if refusal is not None:
        return refusal
    db_path = owned_store_path(_store_name(store))
    if db_path is None:
        return _store_unavailable(store)
    raw = body.get("name")
    backup = _resolve_backup_name(db_path, raw.strip() if isinstance(raw, str) else "")
    if backup is None:
        return web.json_response(
            {
                "error": "name must be a backup file in this store's backup directory",
                "code": "invalid_backup_name",
            },
            status=400,
        )
    if not backup.is_file():
        return web.json_response(
            {"error": "no such backup for this memory store", "code": "backup_not_found"},
            status=404,
        )
    try:
        superseded = await asyncio.to_thread(_restore_blocking, db_path, backup, _store_name(store))
    except FileNotFoundError:
        # Raced a prune between the is_file() above and the restore itself.
        return web.json_response(
            {"error": "no such backup for this memory store", "code": "backup_not_found"},
            status=404,
        )
    except MemoryStartupUnavailable as exc:
        return web.json_response(
            {"error": _redact_memory_field(str(exc)), "code": "store_unavailable"}, status=503
        )
    except ValueError as exc:
        # The backup failed its integrity check; nothing was displaced.
        logger.warning("refusing to restore %s", backup.name, exc_info=True)
        return web.json_response(
            {"error": _redact_memory_field(str(exc)), "code": "restore_refused"},
            status=409,
        )
    except Exception:
        logger.exception(
            "restoring memory store %r from %s failed", _store_name(store), backup.name
        )
        await _audit(request, "memory.restore", "error", f"{_store_name(store)}:{backup.name}")
        return web.json_response(
            {"error": "the restore did not complete", "code": "restore_failed"}, status=500
        )
    await _audit(request, "memory.restore", "success", f"{_store_name(store)}:{backup.name}")
    return web.json_response(
        {"ok": True, "superseded": superseded, "pending": True, "restart_required": True}
    )
