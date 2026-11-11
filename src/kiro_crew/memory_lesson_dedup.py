"""Fold a private store's consolidation lessons into stored copies of the same rule.

Private consolidation keys a lesson on its exact rule and scope, so a rewording that
differs only in case, spacing or Unicode form, or a near-copy, becomes one more
standing row each time. ``memory.member_lesson_dedup`` picks the mode:

* ``off`` (default) -- nothing is read or logged; writes are today's.
* ``shadow`` -- every lesson is written as before; the log counts what ``enforce``
  would fold, with lesson keys only.
* ``enforce`` -- applies the fold, in two places:

  1. In the consolidation transaction, a new lesson whose normalized text equals a
     live lesson of the same scope and tier (:func:`kiro_crew.memory_item_identity.same_item`,
     steps 1-2) is not written; the stored row is left as it is.
  2. When the embedding backfill gives a lesson this consolidation wrote its vector,
     and no dashboard turn's injection receipt names that lesson yet, a stored
     vector match (step 3) retires the NEW row with a saved revision. The stored
     row is untouched.

Only a row consolidation just wrote is ever skipped or retired, so no rollback needs
a data migration. Rollback is the flag set to ``off``: the next backfill sweep, or
the gateway's repair visit while no embedding model is ready (:func:`rollback_if_off`),
calls :func:`restore_retired`, which brings back every row step 2 retired from its
revision. A lesson a person taught (``user_explicit``) is never folded: it is never
the new row here, and it is always a leader. A copy that would add a NOT-clause the
stored row lacks is written as today, so no clause is lost. With
``memory.writes_enabled`` or ``memory.persistence_enabled`` off, the sweep retires,
restores and drops nothing; the queue waits.

"Not served" is read from the injection receipts (:mod:`kiro_crew.injection_receipts`)
and fails closed: a receipt shard that cannot be read, or a scan past
:data:`RECEIPT_BYTES_CAP`, counts as served, and so does a lesson still waiting
after :data:`PENDING_MAX_AGE_SECS`. shortcut: only dashboard turns write receipts,
and the dispatch gate writes them on a worker thread, so a row an incognito, CLI or
task-runner session was given, or a dashboard turn given it while the sweep runs,
can still be retired. The row it folds into is the same rule at cosine 0.95 or more
and stays served, and the revision keeps the retired row restorable. Upgrade when
every prompt builder records receipts synchronously.

The default store keeps its own ``> 0.85`` merge in ``write_lesson``. Under
``shadow`` or ``enforce`` that writer counts its cosine merges and how many fall
below :data:`~kiro_crew.memory_item_identity.SAME_ITEM_COSINE`, so reviewers can
decide whether to move it to the shared rule. Its merges do not change.

The log follows the kind-routing log (:mod:`kiro_crew.memory_kind_routing`): it lives
in the store's ``memory_meta`` table, holds keys and counts only, never text, and is
bounded:

* one counter row per UTC day (``memory_dedup_log:counts:<day>``), by mode and
  counter; rows older than ``COUNTER_DAYS_KEPT`` days are pruned;
* id rows (``memory_dedup_log:ids:<day>``), shadow mode only: at most
  :data:`IDS_PER_DAY` a day and the kind-routing sample caps in all
  (``SAMPLES_TOTAL`` rows, ``SAMPLE_BYTES_CAP`` bytes), oldest day dropped first;
* ids stop ``SHADOW_SAMPLE_WINDOW_SECS`` after shadow first logged in the store,
  and that window restarts when :data:`RULES_VERSION` changes.

The pending list (``memory_dedup_log:pending``) holds at most :data:`PENDING_CAP`
keys of new rows waiting for their vector, oldest dropped first.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final, Iterable

from kiro_crew import jsonl_util
from kiro_crew import memory_kind_routing as _kind_log
from kiro_crew.config.memory_sections import member_lesson_dedup_mode_from
from kiro_crew.lesson_validation import LESSON_APPLIES_ON_TOPIC, authored_lesson_applies
from kiro_crew.memory_item_identity import (
    SAME_ITEM_COSINE,
    TIER_FINDINGS,
    TIER_RULE,
    Item,
    leader_fold,
    leader_of,
    normalize_text,
    unit_vector,
)

if TYPE_CHECKING:
    import sqlite3

    from kiro_crew.vector_memory import VectorMemoryStore

logger = logging.getLogger(__name__)

MODE_OFF: Final = "off"
MODE_SHADOW: Final = "shadow"
MODE_ENFORCE: Final = "enforce"

STEP_TEXT: Final = "text"
STEP_VECTOR: Final = "vector"

#: The revision ``source`` of a row step 2 retired; :func:`restore_retired` reads it.
RETIRE_SOURCE: Final = "member_lesson_dedup"

IDS_PER_DAY: Final = 150
#: Bump when what counts as the same item changes, so the log can tell ids taken
#: under the old rules from the new ones.
RULES_VERSION: Final = 1

PENDING_CAP: Final = 200
PENDING_MAX_AGE_SECS: Final = 2 * 86_400
#: The most receipt bytes one check reads before it gives up and counts as served.
RECEIPT_BYTES_CAP: Final = 32 * 1024 * 1024

_META_PREFIX: Final = "memory_dedup_log:"
COUNTS_PREFIX: Final = _META_PREFIX + "counts:"
IDS_PREFIX: Final = _META_PREFIX + "ids:"
PENDING_KEY: Final = _META_PREFIX + "pending"
#: Set while some row the vector step retired may still need restoring.
RETIRED_KEY: Final = _META_PREFIX + "retired_at"
SHADOW_STARTED_KEY: Final = _META_PREFIX + "shadow_started_at"
RULES_VERSION_KEY: Final = _META_PREFIX + "rules_version"

_LIVE_LESSONS_SQL: Final = (
    "SELECT key, value_json, source, embedding, created_at FROM semantic_memory "
    "WHERE is_deleted = 0 AND key LIKE 'lesson.%' "
    # Leader order: what a person taught first, then newest first.
    "ORDER BY (source = 'user_explicit') DESC, updated_at DESC, key"
)


def configured_mode() -> str:
    """``memory.member_lesson_dedup`` from the live config; the default when unreadable."""
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        raw = getattr(KiroCrewConfig.load().memory, "member_lesson_dedup", None)
    except Exception:
        logger.debug("Could not read memory.member_lesson_dedup; using the default", exc_info=True)
        raw = None
    return member_lesson_dedup_mode_from(raw)


def automatic_writes_off() -> bool:
    """Whether a memory switch stops automatic writes; ``True`` when config is unreadable."""
    try:
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.config.memory_sections import memory_writes_off_key

        return memory_writes_off_key(KiroCrewConfig.load().memory) is not None
    except Exception:
        logger.debug("Could not read the memory write switches", exc_info=True)
        return True


# ── Rows as items ───────────────────────────────────────────────────────────


def _decode(value_json: object) -> object:
    try:
        return json.loads(value_json) if isinstance(value_json, str) else None
    except ValueError:
        return None


def lesson_item(
    key: str,
    value: object,
    *,
    source: str = "",
    blob: object = None,
    dim: int = 0,
    space: object = None,
) -> Item | None:
    """Lesson *value* (a stored mapping or a legacy string) as an :class:`Item`.

    The text is the rule alone, the input its vector is computed from, so the text
    and vector steps compare the same thing. ``None`` when *value* has no rule.
    Which stored rows may become items is :func:`servable_lessons`'s answer.
    """
    from kiro_crew.vector_memory_runtime import lessons as _lessons

    if not isinstance(value, (dict, str)):
        return None
    text = _lessons._lesson_embed_text(value)
    if not text:
        return None
    applies = value.get("applies") if isinstance(value, dict) else None
    tier = (
        TIER_FINDINGS if authored_lesson_applies(applies) == LESSON_APPLIES_ON_TOPIC else TIER_RULE
    )
    return Item(
        key=key,
        text=text,
        scope=_lessons._lesson_scope(value),
        tier=tier,
        vector=unit_vector(blob, dim) if blob is not None else None,
        space=space,
        user_taught=source == "user_explicit",
    )


def _clause(value: object) -> str:
    return normalize_text(value.get("negative")) if isinstance(value, dict) else ""


def adds_clause(candidate: object, leader: object) -> bool:
    """Whether lesson value *candidate* carries a NOT-clause *leader* does not."""
    clause = _clause(candidate)
    return bool(clause) and clause != _clause(leader)


def servable_lessons(store: "VectorMemoryStore", rows: list) -> list:
    """The stored lesson *rows* the rule block would serve, in their given order.

    The one admission rule for both leader pools: a row passes exactly the two
    filters the session-start renderer applies, ``_eligible_rows`` (record status
    and validity window) and ``_renderable_lesson_text`` (a lesson shape, no
    withheld or volatile content, a usable ``repo_scope``). A row no prompt shows
    can then never lead, so no copy is ever folded into a row the agent never sees.
    """
    from kiro_crew.vector_memory_runtime import lessons as _lessons

    return [
        row
        for row in store._eligible_rows(rows, "directive")
        if _lessons._renderable_lesson_text(_decode(row["value_json"]), row["key"])
    ]


def _live_lessons(store: "VectorMemoryStore") -> list:
    """Live lesson rows a prompt can still serve, in leader order."""
    return servable_lessons(store, store.db.execute(_LIVE_LESSONS_SQL).fetchall())


def text_leader_in_transaction(store: "VectorMemoryStore", key: str, value: object) -> str | None:
    """The live lesson new lesson *value* (key *key*) is a textual copy of, or ``None``.

    Steps 1-2 of the shared rule: same scope, same tier, equal normalized text. A
    live row under *key* itself means the writer's own same-key path decides, so
    nothing here does. ``None`` too when the copy would add a NOT-clause. Only a
    row :func:`servable_lessons` admits can be the leader.
    """
    candidate = lesson_item(key, value)
    if candidate is None:
        return None
    raw = store.db.execute(_LIVE_LESSONS_SQL).fetchall()
    if any(row["key"] == key for row in raw):
        return None
    rows = servable_lessons(store, raw)
    stored = {row["key"]: _decode(row["value_json"]) for row in rows}
    pool = (
        item
        for item in (
            lesson_item(row["key"], stored[row["key"]], source=row["source"]) for row in rows
        )
        if item is not None
    )
    leader = leader_of(candidate, pool)
    if leader is None or adds_clause(value, stored[leader.key]):
        return None
    return leader.key


# ── The bounded log ─────────────────────────────────────────────────────────


class DedupLog:
    """What one pass decided: counters and, for shadow, ``(key, leader, step)`` ids."""

    def __init__(self, mode: str) -> None:
        self.mode = member_lesson_dedup_mode_from(mode)
        self.counts: dict[str, int] = {}
        self.ids: list[dict] = []

    def count(self, name: str, n: int = 1) -> None:
        if n:
            self.counts[name] = self.counts.get(name, 0) + n

    def fold(self, step: str, key: str, leader: str) -> None:
        self.count(f"{step}_folds")
        self.ids.append({"key": key, "leader": leader, "step": step, "rules": RULES_VERSION})

    @property
    def loggable(self) -> bool:
        return self.mode != MODE_OFF and bool(self.counts)


def _bump_counts(db: "sqlite3.Connection", log: DedupLog, now: datetime) -> None:
    day = now.date().isoformat()
    key = COUNTS_PREFIX + day
    counts = _kind_log._loads_dict(_kind_log._read(db, key))
    entry = counts.get(log.mode)
    entry = entry if isinstance(entry, dict) else {}
    for name, n in log.counts.items():
        entry[name] = _kind_log._count(entry.get(name)) + n
    counts[log.mode] = entry
    _kind_log._upsert(db, key, json.dumps(counts, sort_keys=True), now.isoformat())
    cutoff = (now - timedelta(days=_kind_log.COUNTER_DAYS_KEPT)).date().isoformat()
    db.execute(
        "DELETE FROM memory_meta WHERE key GLOB ? AND key < ?",
        (COUNTS_PREFIX + "*", COUNTS_PREFIX + cutoff),
    )


def _add_ids(db: "sqlite3.Connection", log: DedupLog, now: datetime) -> None:
    """Append *log*'s ids to today's id row, within every cap."""
    if not log.ids:
        return
    stamp = now.isoformat()
    if _kind_log._read(db, RULES_VERSION_KEY) != str(RULES_VERSION):
        _kind_log._upsert(db, RULES_VERSION_KEY, str(RULES_VERSION), stamp)
        _kind_log._upsert(db, SHADOW_STARTED_KEY, stamp, stamp)
    started = _kind_log._parse_instant(_kind_log._read(db, SHADOW_STARTED_KEY))
    if started is None:
        started = now
        _kind_log._upsert(db, SHADOW_STARTED_KEY, stamp, stamp)
    if (now - started).total_seconds() >= _kind_log.SHADOW_SAMPLE_WINDOW_SECS:
        return
    today_key = IDS_PREFIX + now.date().isoformat()
    today = _kind_log._loads_list(_kind_log._read(db, today_key))[:IDS_PER_DAY]
    room = IDS_PER_DAY - len(today)
    if room <= 0:
        return
    today += log.ids[:room]
    rows: dict[str, list] = {
        str(key): _kind_log._loads_list(raw)
        for key, raw in db.execute(
            "SELECT key, value FROM memory_meta WHERE key GLOB ? ORDER BY key",
            (IDS_PREFIX + "*",),
        ).fetchall()
    }
    rows[today_key] = today
    _kind_log._trim_samples(db, rows, today_key, stamp)


def record_in_transaction(
    db: "sqlite3.Connection", log: DedupLog, *, now: datetime | None = None
) -> bool:
    """Write *log* inside the caller's open transaction, under its own savepoint.

    Never raises: a log that cannot be written leaves the caller's writes as they were.
    """
    if not log.loggable:
        return False
    instant = now or _kind_log._utcnow()
    db.execute("SAVEPOINT memory_dedup_log")
    try:
        _bump_counts(db, log, instant)
        if log.mode == MODE_SHADOW:
            _add_ids(db, log, instant)
    except Exception:
        db.execute("ROLLBACK TO memory_dedup_log")
        logger.warning("Could not record the lesson dedup log", exc_info=True)
        return False
    finally:
        db.execute("RELEASE memory_dedup_log")
    return True


def record(store: "VectorMemoryStore", log: DedupLog, *, now: datetime | None = None) -> bool:
    """Write *log* in its own transaction under the store lock. Never raises."""
    if not log.loggable:
        return False
    try:
        with store._db_lock:
            db = store.db
            db.execute("BEGIN IMMEDIATE")
            try:
                wrote = record_in_transaction(db, log, now=now)
                db.commit()
            except BaseException:
                db.rollback()
                raise
    except Exception:
        logger.warning("Could not record the lesson dedup log", exc_info=True)
        return False
    return wrote


def record_default_merges(store: "VectorMemoryStore", similarities: Iterable[float]) -> bool:
    """Count the default store's cosine merges, and those below the shared threshold.

    *similarities* are the cosines of the merges one ``write_lesson`` call made on
    a global (V1) store. Reads the mode only when there is something to count.
    Counts only; no merge decision reads it. Never raises.
    """
    sims = list(similarities)
    if not sims or store.algorithm_version == "v2":
        return False
    log = DedupLog(configured_mode())
    if log.mode == MODE_OFF:
        return False
    log.count("default_cosine_merges", len(sims))
    log.count("default_below_same_item", sum(1 for s in sims if s < SAME_ITEM_COSINE))
    return record(store, log)


# ── Pending rows and the vector step ────────────────────────────────────────


def remember_pending_in_transaction(
    db: "sqlite3.Connection", keys: list[str], *, now: datetime | None = None
) -> None:
    """Add new lesson *keys* to the pending list, inside the caller's transaction.

    Under its own savepoint and never raises: a lost entry only means that row
    is not checked by the vector step, which is today's behavior.
    """
    if not keys:
        return
    stamp = (now or _kind_log._utcnow()).isoformat()
    db.execute("SAVEPOINT memory_dedup_pending")
    try:
        pending = [
            p
            for p in _kind_log._loads_list(_kind_log._read(db, PENDING_KEY))
            if isinstance(p, dict)
        ]
        known = {p.get("key") for p in pending}
        pending += [{"key": k, "at": stamp} for k in keys if k not in known]
        _kind_log._upsert(db, PENDING_KEY, json.dumps(pending[-PENDING_CAP:]), stamp)
    except Exception:
        db.execute("ROLLBACK TO memory_dedup_pending")
        logger.warning("Could not queue new private lessons for the copy check", exc_info=True)
    finally:
        db.execute("RELEASE memory_dedup_pending")


def _served_keys(store_name: str, keys: set[str], since: datetime) -> set[str] | None:
    """Which of *keys* a receipt of *store_name* named since *since*; ``None`` if unknown."""
    from kiro_crew import injection_receipts

    directory: Path = injection_receipts._receipt_dir()
    first = since.astimezone().date()
    seen: set[str] = set()
    read = 0
    try:
        if not directory.exists():
            return seen
        for path in sorted(directory.glob("*.jsonl")):
            try:
                day = datetime.strptime(path.stem, "%Y-%m-%d").date()
            except ValueError:
                continue
            if day < first:
                continue
            read += path.stat().st_size
            if read > RECEIPT_BYTES_CAP:
                return None
            with open(path, "rb") as fh:
                for raw in jsonl_util.bounded_records(fh, path, label="injection receipt"):
                    try:
                        line = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(line, dict) or line.get("memory_store") != store_name:
                        continue
                    for kind in injection_receipts.KINDS:
                        ids = line.get(kind)
                        if isinstance(ids, list):
                            seen.update(k for k in ids if k in keys)
    except OSError:
        logger.debug("Could not read injection receipts", exc_info=True)
        return None
    return seen


def resolve_pending(store: "VectorMemoryStore", *, now: datetime | None = None) -> int:
    """Run the vector step for pending new rows that now have a vector; return retirements.

    Rows still without a vector stay pending until :data:`PENDING_MAX_AGE_SECS`.
    Never raises: a failure leaves every row as it is.
    """
    try:
        return _resolve_pending(store, now or _kind_log._utcnow())
    except Exception:
        logger.warning("Could not check new private lessons for copies", exc_info=True)
        return 0


def _read_pending(db: "sqlite3.Connection") -> list[dict]:
    return [
        p
        for p in _kind_log._loads_list(_kind_log._read(db, PENDING_KEY))
        if isinstance(p, dict) and isinstance(p.get("key"), str)
    ]


def rollback_if_off(store: "VectorMemoryStore") -> None:
    """With the flag ``off``, restore what the vector step retired and drop its queue.

    Needs no embedding model, so a sweep that cannot embed still rolls back.
    Does nothing with a write switch off, or while the flag is on. Never raises.
    """
    try:
        if store.algorithm_version != "v2":
            return
        with store._db_lock:
            retired_any = _kind_log._read(store.db, RETIRED_KEY) is not None
            queued = _kind_log._read(store.db, PENDING_KEY) is not None
        if not (retired_any or queued) or automatic_writes_off():
            return
        if configured_mode() == MODE_OFF:
            _roll_back(store, retired_any)
    except Exception:
        logger.warning("Could not roll back private lesson dedup", exc_info=True)


def _roll_back(store: "VectorMemoryStore", retired_any: bool) -> None:
    """Restore every retired row, then drop the queue and the retired marker."""
    if retired_any:
        restore_retired(store)
    with store._db_lock:
        store.db.execute("BEGIN IMMEDIATE")
        try:
            store.db.execute(
                "DELETE FROM memory_meta WHERE key IN (?, ?)", (PENDING_KEY, RETIRED_KEY)
            )
            store.db.commit()
        except BaseException:
            store.db.rollback()
            raise


def _resolve_pending(store: "VectorMemoryStore", now: datetime) -> int:
    if store.algorithm_version != "v2":
        return 0
    with store._db_lock:
        queued = _kind_log._read(store.db, PENDING_KEY) is not None
        pending = _read_pending(store.db)
        retired_any = _kind_log._read(store.db, RETIRED_KEY) is not None
    if not queued and not retired_any:
        return 0
    # Retiring, restoring and dropping the queue are all automatic writes: with
    # a write switch off nothing changes, and the queue waits for it to come back.
    if automatic_writes_off():
        return 0
    log = DedupLog(configured_mode())
    if log.mode == MODE_OFF:
        _roll_back(store, retired_any)
        return 0
    if not pending:
        return 0
    by_key = {p["key"]: p for p in pending}
    oldest = min((_kind_log._parse_instant(p.get("at")) or now for p in pending), default=now)
    store_name = str(getattr(store, "_memory_store_name", "") or "")
    served = _served_keys(store_name, set(by_key), oldest) if store_name else None
    dim = int(store._embedding_dim)
    retired = 0
    with store._db_lock:
        db = store.db
        db.execute("BEGIN IMMEDIATE")
        try:
            signature = store._read_meta("embedding_space_sig")
            space = (signature, store._space_generation) if signature is not None else None
            rows = _live_lessons(store)
            stored = {row["key"]: row for row in rows}
            items = {
                row["key"]: lesson_item(
                    row["key"],
                    _decode(row["value_json"]),
                    source=row["source"],
                    blob=row["embedding"],
                    dim=dim,
                    space=space,
                )
                for row in rows
            }
            # Stored rows fold among themselves first, so a new row is compared
            # with their leaders only, and only with leaders written before it:
            # a later row (a textual copy shadow let through) never claims it.
            # Each new row, oldest first, then joins a leader or becomes one for
            # the new rows after it.
            pool = [i for k, i in items.items() if i is not None and k not in by_key]
            leaders = [group[0] for group in leader_fold(pool)]
            # Entries a consolidation queued while the receipts were read wait
            # for the next sweep; this one checks only the snapshot it scanned.
            newer = [p for p in _read_pending(db) if p["key"] not in by_key]
            keep: list[dict] = []
            for entry in pending:
                key = entry["key"]
                row, item = stored.get(key), items.get(key)
                if row is None or item is None:
                    continue  # retired, forgotten or unreadable: nothing to check
                if item.vector is None:
                    at = _kind_log._parse_instant(entry.get("at")) or now
                    if (now - at).total_seconds() < PENDING_MAX_AGE_SECS:
                        keep.append(entry)
                    else:
                        log.count("expired")
                    continue
                log.count("checked")
                if served is None or key in served:
                    log.count("served")
                    leaders.append(item)
                    continue
                born = str(row["created_at"] or "")
                leader = leader_of(
                    item, (i for i in leaders if str(stored[i.key]["created_at"] or "") < born)
                )
                if leader is None:
                    leaders.append(item)
                    continue
                if adds_clause(
                    _decode(row["value_json"]), _decode(stored[leader.key]["value_json"])
                ):
                    log.count("kept_for_clause")
                    leaders.append(item)
                    continue
                log.fold(STEP_VECTOR, key, leader.key)
                if log.mode == MODE_ENFORCE:
                    _retire(store, key)
                    retired += 1
            remaining = (keep + newer)[-PENDING_CAP:]
            if remaining:
                _kind_log._upsert(db, PENDING_KEY, json.dumps(remaining), now.isoformat())
            else:
                # No queue row once it is empty, so the repair probe stops
                # seeing work (has_pending_embeddings).
                db.execute("DELETE FROM memory_meta WHERE key = ?", (PENDING_KEY,))
            if retired:
                _kind_log._upsert(db, RETIRED_KEY, now.isoformat(), now.isoformat())
            record_in_transaction(db, log, now=now)
            db.commit()
        except BaseException:
            db.rollback()
            raise
    if retired:
        store._invalidate_semantic_scoring()
    return retired


def _retire(store: "VectorMemoryStore", key: str) -> None:
    """Tombstone new row *key* with an accepted revision that names this stage."""
    before = store.db.execute("SELECT * FROM semantic_memory WHERE key = ?", (key,)).fetchone()
    store._record_mutation(
        "directive",
        key,
        dict(before),
        RETIRE_SOURCE,
        metadata={"status": "superseded"},
        operation="supersede",
    )


def restore_retired(store: "VectorMemoryStore") -> list[str]:
    """Bring back every row the vector step retired, unless a later change touched it.

    A row is restored when its latest accepted revision is still this stage's
    retirement and its stored value is the one that revision saved. Returns the restored keys.
    """
    restored: list[str] = []
    with store._db_lock:
        db = store.db
        db.execute("BEGIN IMMEDIATE")
        try:
            ids = [
                row[0]
                for row in db.execute(
                    "SELECT DISTINCT record_id FROM memory_revisions "
                    "WHERE source = ? AND operation = 'supersede'",
                    (RETIRE_SOURCE,),
                ).fetchall()
            ]
            for record_id in ids:
                # Accepted revisions only: a pending conflict proposal (a later
                # consolidation re-extracting the rule) changed nothing stored.
                latest = db.execute(
                    "SELECT source, operation, before_json FROM memory_revisions "
                    "WHERE record_id = ? AND status = 'accepted' ORDER BY id DESC LIMIT 1",
                    (record_id,),
                ).fetchone()
                if latest is None or (latest[0], latest[1]) != (RETIRE_SOURCE, "supersede"):
                    continue
                key = str(record_id).removeprefix("key:")
                row = db.execute(
                    "SELECT * FROM semantic_memory WHERE key = ? AND is_deleted = 1", (key,)
                ).fetchone()
                saved = _kind_log._loads_dict(latest[2])
                if row is None or saved.get("value_json") != row["value_json"]:
                    continue
                db.execute(
                    f"UPDATE {store._sem_rel} SET is_deleted = 0 WHERE key = ?{store._sem_guard}",
                    (key,),
                )
                store._record_mutation(
                    "directive", key, dict(row), RETIRE_SOURCE, operation="restore"
                )
                restored.append(key)
            db.commit()
        except BaseException:
            db.rollback()
            raise
    if restored:
        store._invalidate_semantic_scoring()
    return restored
