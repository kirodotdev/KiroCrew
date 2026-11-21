"""Keep facts about the person out of memory when no person spoke in the span.

``pref.*`` and ``user.*`` facts, and the V1 preferences file, claim to describe the
person: what they want, who they are. Consolidation reads every ``user`` row as the
person, so a worker whose every input came from another agent writes a conductor's
brief or its own status as a "user preference". This gate reads the turn classes of
:mod:`kiro_crew.memory_origin_gate` (the same allowlist, no second detector) over
the rows the model read.

Only these kinds are gated. ``project.*`` facts and episodes are not: most of them
come from agent chats and carry real findings, and they make no claim about the
person.

``memory.fact_origin_gate`` picks the mode:

* ``off`` (default) -- nothing is read or logged; prompts and writes are today's.
* ``shadow`` -- every fact is written as before; the log counts what ``enforce``
  would hold back.
* ``enforce`` -- in a span with no human turn, ``pref.*`` and ``user.*`` items
  (writes and deletes) are dropped from the answer, and so is
  ``preferences_update``. Every other item is written as before. Stored rows are
  never changed.

A span that holds a human turn keeps every item, even with agent turns beside it
(the mixed-span gap :mod:`kiro_crew.memory_origin_gate` names). A messaging-channel
session is left as it is and not logged, for that module's reason.

The log follows :mod:`kiro_crew.memory_origin_gate`: the store's ``memory_meta``
table, fact keys and counts only, never a value, under the kind-routing caps:

* one counter row per UTC day (``memory_fact_origin_log:counts:<day>``), by mode
  and kind (``pref``, ``user``, ``preferences_file``); rows older than
  ``COUNTER_DAYS_KEPT`` days are pruned;
* id rows (``memory_fact_origin_log:ids:<day>``), shadow mode only: the keys held
  back, at most :data:`IDS_PER_DAY` a day and the kind-routing totals
  (``SAMPLES_TOTAL`` rows, ``SAMPLE_BYTES_CAP`` bytes), oldest day dropped first;
  ids stop ``SHADOW_SAMPLE_WINDOW_SECS`` after shadow first logged in the store,
  and that window restarts when :data:`RULES_VERSION` changes;
* control rows (``memory_fact_origin_log:control:<day>``), shadow mode only: a
  hashed sample of the keys the gate keeps.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final, NamedTuple

from kiro_crew import memory_kind_routing as _kind_log
from kiro_crew import memory_shadow_trace as _trace
from kiro_crew.config.memory_sections import fact_origin_gate_mode_from
from kiro_crew.memory_origin_gate import has_human_turn

if TYPE_CHECKING:
    import sqlite3

    from kiro_crew.vector_memory import VectorMemoryStore

logger = logging.getLogger(__name__)

MODE_OFF: Final = "off"
MODE_SHADOW: Final = "shadow"
MODE_ENFORCE: Final = "enforce"

#: Key prefixes that describe the person, and the kind each is counted under.
GATED_PREFIXES: Final = (("pref.", "pref"), ("user.", "user"))
#: The kind the V1 preferences file is counted under.
KIND_PREFERENCES_FILE: Final = "preferences_file"

IDS_PER_DAY: Final = 150
#: Bump when what is gated changes, so the log can tell ids taken under the old
#: rules from the new ones.
RULES_VERSION: Final = 1

_META_PREFIX: Final = "memory_fact_origin_log:"
COUNTS_PREFIX: Final = _META_PREFIX + "counts:"
IDS_PREFIX: Final = _META_PREFIX + "ids:"
SHADOW_STARTED_KEY: Final = _META_PREFIX + "shadow_started_at"
RULES_VERSION_KEY: Final = _META_PREFIX + "rules_version"


def configured_mode() -> str:
    """``memory.fact_origin_gate`` from the live config; the default when unreadable."""
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        raw = getattr(KiroCrewConfig.load().memory, "fact_origin_gate", None)
    except Exception:
        logger.debug("Could not read memory.fact_origin_gate; using the default", exc_info=True)
        raw = None
    return fact_origin_gate_mode_from(raw)


def _gated_kind(item: object) -> str | None:
    """The kind a semantic *item* is gated under, or ``None`` when it is not gated."""
    key = item.get("key") if isinstance(item, dict) else None
    if not isinstance(key, str):
        return None
    for prefix, kind in GATED_PREFIXES:
        if key.startswith(prefix):
            return kind
    return None


class FactGate(NamedTuple):
    """The result to write, and what the gate decided on the way."""

    result: dict
    mode: str
    #: True when no turn the gate read was a person's.
    no_human: bool = False
    #: ``(kind, seen, held back)`` for every gated kind the answer carried.
    by_kind: tuple[tuple[str, int, int], ...] = ()
    #: The keys ``enforce`` holds back (or would, under ``shadow``).
    gated_keys: tuple[str, ...] = ()
    #: The gated-kind keys the gate keeps.
    kept_keys: tuple[str, ...] = ()

    @property
    def loggable(self) -> bool:
        return self.mode != MODE_OFF and bool(self.by_kind)


def gate_consolidation_facts(
    result: dict, rows: list[dict], mode: str, *, session_key: str = ""
) -> FactGate:
    """Apply *mode* to the person facts the model extracted from span *rows*.

    *rows* are the rows the model read. ``off`` returns *result* itself without
    reading *rows*; ``shadow`` returns it unchanged; ``enforce`` returns a shallow
    copy, for a span with no human turn, without its ``pref.*`` / ``user.*``
    semantic items and its ``preferences_update``. The whole semantic list is read,
    not only the first ``_MAX_SEMANTIC_PER_CONSOLIDATION``: a writer drops unseen
    deletes before it takes its window, so a later item can still reach it.
    """
    from kiro_crew.messaging.link import is_channel_session_key

    mode = fact_origin_gate_mode_from(mode)
    if mode == MODE_OFF or not isinstance(result, dict) or is_channel_session_key(session_key):
        return FactGate(result, mode)
    items = result.get("semantic")
    gated = [(i, k) for i in items if (k := _gated_kind(i))] if isinstance(items, list) else []
    prefs_file = bool(result.get("preferences_update"))
    if not gated and not prefs_file:
        return FactGate(result, mode)
    no_human = not has_human_turn(rows)
    seen: dict[str, list[int]] = {}
    for _, kind in gated:
        seen.setdefault(kind, [0, 0])[0] += 1
    if prefs_file:
        seen[KIND_PREFERENCES_FILE] = [1, 0]
    if no_human:
        for counts in seen.values():
            counts[1] = counts[0]
    keys = tuple(dict.fromkeys(str(i["key"]) for i, _ in gated))
    gate = FactGate(
        result,
        mode,
        no_human,
        tuple((k, v[0], v[1]) for k, v in sorted(seen.items())),
        keys if no_human else (),
        () if no_human else keys,
    )
    if mode == MODE_SHADOW or not no_human:
        return gate
    held = {id(i) for i, _ in gated}
    kept = {k: v for k, v in result.items() if k != "preferences_update"}
    if isinstance(items, list):
        kept["semantic"] = [i for i in items if id(i) not in held]
    return gate._replace(result=kept)


# ── The bounded log ─────────────────────────────────────────────────────────


def _bump_counts(db: "sqlite3.Connection", gate: FactGate, now: datetime) -> None:
    day = now.date().isoformat()
    key = COUNTS_PREFIX + day
    counts = _kind_log._loads_dict(_kind_log._read(db, key))
    by_kind = counts.get(gate.mode)
    by_kind = by_kind if isinstance(by_kind, dict) else {}
    for kind, seen, held in gate.by_kind:
        entry = by_kind.get(kind)
        entry = entry if isinstance(entry, dict) else {}
        for name, n in (
            ("passes", 1),
            ("no_human", int(gate.no_human)),
            ("facts", seen),
            ("gated", held),
        ):
            entry[name] = _kind_log._count(entry.get(name)) + n
        by_kind[kind] = entry
    counts[gate.mode] = by_kind
    _kind_log._upsert(db, key, json.dumps(counts, sort_keys=True), now.isoformat())
    cutoff = (now - timedelta(days=_kind_log.COUNTER_DAYS_KEPT)).date().isoformat()
    db.execute(
        "DELETE FROM memory_meta WHERE key GLOB ? AND key < ?",
        (COUNTS_PREFIX + "*", COUNTS_PREFIX + cutoff),
    )


def _add_ids(db: "sqlite3.Connection", gate: FactGate, now: datetime, session_key: str) -> None:
    """Append the held-back keys to today's id row, within every cap."""
    if not gate.gated_keys:
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
    today += [
        {
            "key": k,
            "path": "consolidation",
            "rules": RULES_VERSION,
            **_trace.stamp(session_key, now),
        }
        for k in gate.gated_keys[:room]
    ]
    rows: dict[str, list] = {
        str(key): _kind_log._loads_list(raw)
        for key, raw in db.execute(
            "SELECT key, value FROM memory_meta WHERE key GLOB ? ORDER BY key",
            (IDS_PREFIX + "*",),
        ).fetchall()
    }
    rows[today_key] = today
    _kind_log._trim_samples(db, rows, today_key, stamp)


def record_fact_origin_gate(
    store: "VectorMemoryStore",
    gate: FactGate,
    *,
    now: datetime | None = None,
    session_key: str = "",
) -> bool:
    """Write *gate*'s counters (and, under ``shadow``, its ids); return whether it committed.

    *session_key* is the chat the facts came from; only its tag is written. Its own
    transaction under the store's lock. Never raises: a log is not worth a pass.
    """
    if not gate.loggable:
        return False
    instant = now or _kind_log._utcnow()
    try:
        with store._db_lock:
            db = store.db
            db.execute("BEGIN IMMEDIATE")
            try:
                _bump_counts(db, gate, instant)
                if gate.mode == MODE_SHADOW:
                    _add_ids(db, gate, instant, session_key)
                    _trace.add_control(
                        db,
                        _META_PREFIX,
                        gate.kept_keys,
                        session_key=session_key,
                        rules=RULES_VERSION,
                        now=instant,
                    )
                db.commit()
            except BaseException:
                db.rollback()
                raise
    except Exception:
        logger.warning("Could not record the fact origin gate", exc_info=True)
        return False
    return True
