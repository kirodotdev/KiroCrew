"""Route consolidation's ``project.*`` writes by the ``kind`` the model tagged them with.

The consolidation prompt asks the model to tag each semantic item with a ``kind``:

* ``fact`` -- still true in a month, and not looked up somewhere else.
* ``event`` -- something that happened, worth a dated record.
* ``state`` -- the current status of a PR, issue, CI run, branch, round or SHA,
  which GitHub, git or the work ledger already holds.

A missing or unknown ``kind`` reads as ``fact``, which is how every item was
treated before the tag existed, so an older model answer routes exactly as today.
The class comes from the model, not from a key or value pattern: on a hand-graded
sample a key-or-value regex that caught most work state also flagged about half of
the durable facts.

Routing covers ``project.*`` creates and updates only. ``pref.*``, ``user.*`` and
``lesson.*`` keys are never routed, and a ``delete`` is never routed. Consolidation
writes never carry ``source="user_explicit"``, so a user-stated write cannot reach
this code at all.

``memory.kind_routing`` picks the mode:

* ``off`` (default) -- the prompt does not ask for a tag, any tag is ignored and
  nothing is logged.
* ``shadow`` (opt-in) -- the prompt asks for the tag; every item is written exactly as before; the log records
  what ``enforce`` would have done.
* ``enforce`` -- a ``state`` item is not written and is audited under
  ``SemanticRejectCode.WORK_STATE``; a ``fact`` and an ``event`` are written as
  facts, as before, and an ``event`` is still counted and sampled as one.
  Nothing already stored is changed. Turning an ``event`` into a dated episode
  is left to a follow-up that reads the shadow samples first.

The log lives in the store's own ``memory_meta`` table, which every lineage has
(the V1 file, a crew silo and a member database alike), so no schema changes and
no new file. It is bounded by construction:

* one counter row per UTC day (``memory_kind_log:counts:<day>``), counts by mode
  and kind, plus ``sibling_suffix`` by mode on a day that saw one (see
  :func:`capture_prior`); rows older than :data:`COUNTER_DAYS_KEPT` days are pruned;
* sample rows (``memory_kind_log:samples:<day>``), shadow mode only: a uniform
  reservoir of at most :data:`SAMPLES_PER_DAY` over the whole UTC day, and
  :data:`SAMPLES_TOTAL` in all, oldest day dropped first, never more than
  :data:`SAMPLE_BYTES_CAP` bytes together, each value cut to
  :data:`SAMPLE_VALUE_CHARS` characters;
* samples stop :data:`SHADOW_SAMPLE_WINDOW_SECS` after shadow mode first logged
  in the store (``memory_kind_log:shadow_started_at``); counters keep going;
* ``memory_kind_log:rules_version`` names the :data:`RULES_VERSION` of the
  candidate rules shadow trialled while the log was collected. When the code's version differs, the
  sample window restarts, so samples under the new rules are collected too and
  each sample's ``rules`` field tells the two apart.

A sample may also carry ``existed`` (the key was active before the pass) and
``same_value`` (its stored value equals the new one); a reader must accept a
sample without them, as every sample written before they existed is.

The samples exist so a small local classifier can be trained on them later.
"""

from __future__ import annotations

import json
import logging
import random
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Final, Mapping, NamedTuple

from kiro_crew.config.memory_sections import kind_routing_mode_from
from kiro_crew.vector_memory_constants import _MAX_SEMANTIC_PER_CONSOLIDATION

if TYPE_CHECKING:
    import sqlite3

    from kiro_crew.vector_memory import VectorMemoryStore

logger = logging.getLogger(__name__)

KIND_FACT: Final = "fact"
KIND_EVENT: Final = "event"
KIND_STATE: Final = "state"
KINDS: Final = (KIND_FACT, KIND_EVENT, KIND_STATE)

MODE_OFF: Final = "off"
MODE_SHADOW: Final = "shadow"
MODE_ENFORCE: Final = "enforce"

#: The only key family routing reads.
ROUTED_PREFIX: Final = "project."

SAMPLES_PER_DAY: Final = 150
SAMPLES_TOTAL: Final = 2_000
SAMPLE_BYTES_CAP: Final = 1_000_000
SAMPLE_VALUE_CHARS: Final = 200
SHADOW_SAMPLE_WINDOW_SECS: Final = 14 * 86_400
COUNTER_DAYS_KEPT: Final = 400

#: The version of :data:`SHADOW_KIND_RULES`, the rules shadow mode trials. Bump
#: it whenever their wording changes, so the log can tell samples taken under the
#: old rules from the new ones.
RULES_VERSION: Final = 2

_META_PREFIX: Final = "memory_kind_log:"
SHADOW_STARTED_KEY: Final = _META_PREFIX + "shadow_started_at"
RULES_VERSION_KEY: Final = _META_PREFIX + "rules_version"
RULES_CHANGED_KEY: Final = _META_PREFIX + "rules_changed_at"
COUNTS_PREFIX: Final = _META_PREFIX + "counts:"
#: ``{"day": <UTC day>, "rules": <version>, "seen": n}``: how many items today's
#: reservoir has been offered under those rules, which is its stream length.
#: One row, overwritten each day, so it needs no pruning.
RESERVOIR_KEY: Final = _META_PREFIX + "reservoir"
SAMPLES_PREFIX: Final = _META_PREFIX + "samples:"
#: The counts-row entry, ``{mode: n}``, beside the ``{mode: {kind: n}}`` entries.
SIBLING_SUFFIX_COUNT: Final = "sibling_suffix"

_KIND_RULES_HEAD: Final = (
    "Tag every item with kind. Ask two questions: Will this still be true in a month? "
    "Can this be looked up somewhere else? "
    'kind "fact" = still true in a month and not looked up elsewhere '
    "(a decision, a root cause, how something works). "
    'kind "event" = something that happened, worth a dated record. '
    'kind "state" = the current status of a PR, issue, CI run, branch, round or SHA, '
    "which GitHub, git or the work ledger already holds. "
)
_KIND_RULES_TAIL: Final = "pref.* and user.* items are always facts. "

#: The candidate widening of ``state`` that shadow mode trials (version
#: :data:`RULES_VERSION`). ``enforce`` drops what is tagged ``state``, so it does
#: not ask this until a separate promotion change adopts it, once the shadow
#: samples back it; until then ``enforce`` writes exactly what it wrote before.
CANDIDATE_STATE_RULES: Final = (
    'kind "state" also covers a next step, a pending action, what something is blocked on, '
    "a count (tests, cycles, budgets), the local path of a worktree or scratch folder, "
    "and a value marked final or latest that will change again. "
)

#: The ``kind`` rules ``enforce`` asks: the adopted rules, unchanged.
ENFORCE_KIND_RULES: Final = _KIND_RULES_HEAD + _KIND_RULES_TAIL
#: The ``kind`` rules ``shadow`` asks: the adopted rules plus the candidate.
SHADOW_KIND_RULES: Final = _KIND_RULES_HEAD + CANDIDATE_STATE_RULES + _KIND_RULES_TAIL


def kind_rules_for(mode: str) -> str:
    """The prompt's ``kind`` rules under *mode*; ``off`` asks none of this text."""
    mode = kind_routing_mode_from(mode)
    if mode == MODE_SHADOW:
        return SHADOW_KIND_RULES
    if mode == MODE_ENFORCE:
        return ENFORCE_KIND_RULES
    return ""


#: A key suffix that marks one more copy of a topic rather than a topic of its own.
_SIBLING_SUFFIX = re.compile(r"^(?P<base>.+?)_(?:final|latest|new|updated|v[2-9]|v[1-9][0-9]+)$")

#: The reservoir's random source. Measurement only, never security; tests seed it.
_RNG = random.Random()  # nosec B311


def _utcnow() -> datetime:
    """The one clock routing and its log read; tests pin it."""
    return datetime.now(timezone.utc)


def _utf8_clean(text: str) -> str:
    """*text* with any lone surrogate replaced, so SQLite can bind it."""
    return text.encode("utf-8", "replace").decode("utf-8")


def configured_kind_routing_mode() -> str:
    """``memory.kind_routing`` from the live config; the default when it cannot be read."""
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        return kind_routing_mode_from(getattr(KiroCrewConfig.load().memory, "kind_routing", None))
    except Exception:
        logger.debug("Could not read memory.kind_routing; using the default", exc_info=True)
        return kind_routing_mode_from(None)


def item_kind(item: object) -> str:
    """The kind a semantic item carries; missing, non-text or unknown reads as ``fact``."""
    raw = item.get("kind") if isinstance(item, dict) else None
    if isinstance(raw, str):
        kind = raw.strip().lower()
        if kind in KINDS:
            return kind
    return KIND_FACT


def is_routed(item: object) -> bool:
    """True for a ``project.*`` create or update under a well-formed key.

    The key must pass the store's own key shape (``_KEY_PATTERN``, at most
    ``_MAX_KEY_LEN`` characters), so every key the log retains is bounded and
    plain ASCII. A malformed key is not routed; the writer refuses it as before.
    """
    from kiro_crew.vector_memory_runtime.semantic import _KEY_PATTERN, _MAX_KEY_LEN

    if not isinstance(item, dict) or item.get("delete") or item.get("value") is None:
        return False
    key = item.get("key")
    return (
        isinstance(key, str)
        and key.startswith(ROUTED_PREFIX)
        and len(key) <= _MAX_KEY_LEN
        and _KEY_PATTERN.match(key) is not None
    )


class KindDecision(NamedTuple):
    """One routed item: its key, the kind it was tagged with, and its value."""

    key: str
    kind: str
    value: object


class PriorState(NamedTuple):
    """What the store held for the routed keys before the pass wrote anything.

    *values* maps each routed key that was active to its stored ``value_json``;
    *siblings* names each routed key with a sibling-suffix copy already active.
    """

    values: Mapping[str, str]
    siblings: tuple[str, ...]


class KindRouting(NamedTuple):
    """The result the writers should apply, and what routing decided on the way."""

    result: dict
    mode: str
    decisions: tuple[KindDecision, ...]
    #: Set by :func:`capture_prior`; ``None`` when it did not run or could not read.
    prior: PriorState | None = None

    @property
    def refused(self) -> tuple[KindDecision, ...]:
        """The ``state`` items ``enforce`` kept out of semantic memory."""
        if self.mode != MODE_ENFORCE:
            return ()
        return tuple(d for d in self.decisions if d.kind == KIND_STATE)


def _value_text(value: object) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def route_semantic_by_kind(result: dict, mode: str) -> KindRouting:
    """Apply *mode* to the model's ``semantic`` array; see the module docstring.

    Runs on the model's answer right after ``_withhold_unseen_deletes``, on both
    write paths, so the two cannot drift. Only the first
    ``_MAX_SEMANTIC_PER_CONSOLIDATION`` items are read, the ones a writer would
    reach. ``off`` and ``shadow`` return *result* itself, unchanged, so what is
    written is byte-identical to a pass without routing. ``enforce`` returns a
    shallow copy whose ``semantic`` array leaves out each routed ``state`` item
    (still capped); a ``fact`` and an ``event`` are both written as facts.
    """
    mode = kind_routing_mode_from(mode)
    items = result.get("semantic")
    if mode == MODE_OFF or not isinstance(items, list):
        return KindRouting(result, mode, ())
    window = items[:_MAX_SEMANTIC_PER_CONSOLIDATION]
    decisions = tuple(
        KindDecision(item["key"], item_kind(item), item["value"])
        for item in window
        if is_routed(item)
    )
    if mode == MODE_SHADOW or not any(d.kind == KIND_STATE for d in decisions):
        return KindRouting(result, mode, decisions)
    kept = [item for item in window if not (is_routed(item) and item_kind(item) == KIND_STATE)]
    return KindRouting({**result, "semantic": kept}, mode, decisions)


def route_consolidation_by_kind(result: dict, log: logging.Logger) -> KindRouting:
    """Apply ``memory.kind_routing`` to the model's answer, right after the delete fence.

    Both consolidation write paths call this on the answer
    ``_withhold_unseen_deletes`` returned -- ``_write_structured_memory`` (V1) and
    the pass's member-store branch before ``apply_consolidation`` -- so the two
    route the same items the same way, under the live setting. The caller
    writes the returned routing's log after its own writes.
    """
    routing = route_semantic_by_kind(result, configured_kind_routing_mode())
    if routing.decisions:
        counts: dict[str, int] = {}
        for decision in routing.decisions:
            counts[decision.kind] = counts.get(decision.kind, 0) + 1
        log.log(
            logging.INFO if routing.mode == MODE_ENFORCE else logging.DEBUG,
            "Consolidation kind routing (%s): %s",
            routing.mode,
            ", ".join(f"{count} {kind}" for kind, count in sorted(counts.items())),
        )
    return routing


def _sibling_base(key: str) -> str | None:
    match = _SIBLING_SUFFIX.match(key)
    return match.group("base") if match else None


def capture_prior_in_transaction(db: "sqlite3.Connection", routing: KindRouting) -> KindRouting:
    """*routing* with :class:`PriorState` read from *db*, before the pass writes.

    Each read is under the key the model named, which is what the samples
    describe; a writer that files the value under another key (the member
    store's identity match) does not change it.

    The caller holds the store's lock. Read-only and indexed on the key: one
    read for the routed keys, and one prefix read per key carrying a sibling
    suffix. Never raises; on a failed read the routing comes back unchanged,
    and the log then leaves those fields out.
    """
    if routing.mode == MODE_OFF or not routing.decisions:
        return routing
    keys = sorted({d.key for d in routing.decisions})
    try:
        marks = ",".join("?" * len(keys))
        values = {
            str(key): str(raw)
            for key, raw in db.execute(
                f"SELECT key, value_json FROM semantic_memory "  # nosec B608: placeholders only
                f"WHERE is_deleted = 0 AND key IN ({marks})",
                keys,
            ).fetchall()
        }
        siblings = []
        for decision in routing.decisions:
            base = _sibling_base(decision.key)
            if base is None:
                continue
            # Keys are [a-z0-9_.] only (_KEY_PATTERN), so the GLOB has no wildcard but its own.
            active = {
                str(row[0])
                for row in db.execute(
                    "SELECT key FROM semantic_memory WHERE is_deleted = 0 AND (key = ? OR key GLOB ?)",
                    (base, base + "_*"),
                ).fetchall()
            }
            if any(k == base or (k != decision.key and _sibling_base(k) == base) for k in active):
                siblings.append(decision.key)
    except Exception:
        logger.debug("Could not read the prior state for kind routing", exc_info=True)
        return routing
    return routing._replace(prior=PriorState(values, tuple(siblings)))


def capture_prior(store: "VectorMemoryStore", routing: KindRouting) -> KindRouting:
    """:func:`capture_prior_in_transaction` under the store's own lock."""
    if routing.mode == MODE_OFF or not routing.decisions:
        return routing
    try:
        with store._db_lock:
            return capture_prior_in_transaction(store.db, routing)
    except Exception:
        logger.debug("Could not read the prior state for kind routing", exc_info=True)
        return routing


# ── The bounded log ─────────────────────────────────────────────────────────


def _read(db: "sqlite3.Connection", key: str) -> str | None:
    row = db.execute("SELECT value FROM memory_meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])


def _upsert(db: "sqlite3.Connection", key: str, value: str, now: str) -> None:
    db.execute(
        "INSERT INTO memory_meta (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, now),
    )


def _decode(raw: object) -> object:
    try:
        return json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        return None


def _loads_dict(raw: object) -> dict:
    decoded = _decode(raw)
    return decoded if isinstance(decoded, dict) else {}


def _loads_list(raw: object) -> list:
    decoded = _decode(raw)
    return decoded if isinstance(decoded, list) else []


def _parse_instant(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _count(raw: object) -> int:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else 0


def _bump_counts(db: "sqlite3.Connection", routing: KindRouting, now: datetime) -> None:
    """Count *routing*'s items in today's row."""
    day = now.date().isoformat()
    key = COUNTS_PREFIX + day
    counts = _loads_dict(_read(db, key))
    by_kind = counts.get(routing.mode)
    if not isinstance(by_kind, dict):
        by_kind = {}
    for decision in routing.decisions:
        current = by_kind.get(decision.kind)
        by_kind[decision.kind] = (current if isinstance(current, int) else 0) + 1
    counts[routing.mode] = by_kind
    siblings = len(routing.prior.siblings) if routing.prior is not None else 0
    if siblings:
        by_mode = counts.get(SIBLING_SUFFIX_COUNT)
        if not isinstance(by_mode, dict):
            by_mode = {}
        by_mode[routing.mode] = _count(by_mode.get(routing.mode)) + siblings
        counts[SIBLING_SUFFIX_COUNT] = by_mode
    _upsert(db, key, json.dumps(counts, sort_keys=True), now.isoformat())
    cutoff = (now - timedelta(days=COUNTER_DAYS_KEPT)).date().isoformat()
    db.execute(
        "DELETE FROM memory_meta WHERE key GLOB ? AND key < ?",
        (COUNTS_PREFIX + "*", COUNTS_PREFIX + cutoff),
    )


def _same_value(stored: str, value: object) -> bool:
    try:
        decoded = json.loads(stored)
    except ValueError:
        return stored == _value_text(value)
    from kiro_crew.user_json import strict_json_equal

    return strict_json_equal(decoded, value)


def _sample(
    decision: KindDecision, store_name: str, now: datetime, prior: PriorState | None
) -> dict:
    sample: dict = {
        "key": decision.key,
        "kind": decision.kind,
        "value": _utf8_clean(_value_text(decision.value)[:SAMPLE_VALUE_CHARS]),
        "store": store_name,
        "ts": now.isoformat(),
        "rules": RULES_VERSION,
    }
    if prior is not None:
        stored = prior.values.get(decision.key)
        sample["existed"] = stored is not None
        sample["same_value"] = stored is not None and _same_value(stored, decision.value)
    return sample


def _sync_rules_version(db: "sqlite3.Connection", now: datetime) -> None:
    """Record :data:`RULES_VERSION`; on a change, restart the sample window.

    Shadow mode only, so a pass under ``enforce`` cannot spend the restarted
    window while nothing is being sampled.
    """
    stored = _read(db, RULES_VERSION_KEY)
    if stored == str(RULES_VERSION):
        return
    stamp = now.isoformat()
    _upsert(db, RULES_VERSION_KEY, str(RULES_VERSION), stamp)
    _upsert(db, RULES_CHANGED_KEY, stamp, stamp)
    if _read(db, SHADOW_STARTED_KEY) is not None:
        _upsert(db, SHADOW_STARTED_KEY, stamp, stamp)


def _row_bytes(samples: list) -> int:
    return len(json.dumps(samples).encode("utf-8"))


def _reservoir_seen(db: "sqlite3.Connection", day: str) -> int:
    """The length of today's reservoir stream under these rules; 0 for a new stream."""
    entry = _loads_dict(_read(db, RESERVOIR_KEY))
    if entry.get("day") == day and entry.get("rules") == RULES_VERSION:
        return _count(entry.get("seen"))
    return 0


def _add_samples(
    db: "sqlite3.Connection", routing: KindRouting, store_name: str, now: datetime
) -> None:
    """Fold this pass into today's reservoir, then trim every sample row to the caps.

    Reservoir sampling (Algorithm R) over the day's stream: the item numbered
    *t* replaces a random kept sample with probability ``k / t``, so every item
    the day offers is equally likely to be kept, not only the first. The stream
    is counted in :data:`RESERVOIR_KEY` per day and rules version, so a window that restarts mid-day starts a fresh stream rather
    than one numbered by the items counted before it. Samples of other rules
    already in today's row stay as they are and take their share of
    :data:`SAMPLES_PER_DAY`; *k* is what is left.
    """
    started = _parse_instant(_read(db, SHADOW_STARTED_KEY))
    if started is None:
        started = now
        _upsert(db, SHADOW_STARTED_KEY, now.isoformat(), now.isoformat())
    if (now - started).total_seconds() >= SHADOW_SAMPLE_WINDOW_SECS:
        return
    day = now.date().isoformat()
    today_key = SAMPLES_PREFIX + day
    other: list = []
    current: list = []
    for sample in _loads_list(_read(db, today_key)):
        is_current = isinstance(sample, dict) and sample.get("rules") == RULES_VERSION
        (current if is_current else other).append(sample)
    other = other[:SAMPLES_PER_DAY]
    room = SAMPLES_PER_DAY - len(other)
    current = current[:room]
    seen = _reservoir_seen(db, day)
    seen = max(seen, len(current))
    changed = False
    for decision in routing.decisions:
        seen += 1
        if len(current) < room:
            current.append(_sample(decision, store_name, now, routing.prior))
            changed = True
            continue
        slot = _RNG.randrange(seen)
        if slot < room:
            current[slot] = _sample(decision, store_name, now, routing.prior)
            changed = True
    entry = {"day": day, "rules": RULES_VERSION, "seen": seen}
    _upsert(db, RESERVOIR_KEY, json.dumps(entry, sort_keys=True), now.isoformat())
    if not changed:
        return
    today = other + current
    rows: dict[str, list] = {}
    for key, raw in db.execute(
        "SELECT key, value FROM memory_meta WHERE key GLOB ? ORDER BY key",
        (SAMPLES_PREFIX + "*",),
    ).fetchall():
        rows[str(key)] = _loads_list(raw)
    rows[today_key] = today
    _trim_samples(db, rows, today_key, now.isoformat())


def _trim_samples(db: "sqlite3.Connection", rows: dict[str, list], changed: str, now: str) -> None:
    """Drop the oldest samples until the total count and byte caps both hold."""
    order = sorted(rows)
    total = sum(len(rows[k]) for k in order)
    size = {k: _row_bytes(rows[k]) for k in order}
    touched = {changed}
    while order and (total > SAMPLES_TOTAL or sum(size.values()) > SAMPLE_BYTES_CAP):
        oldest = order[0]
        if rows[oldest]:
            rows[oldest].pop(0)
            total -= 1
            touched.add(oldest)
        if rows[oldest]:
            size[oldest] = _row_bytes(rows[oldest])
        else:
            order.pop(0)
            size.pop(oldest)
            touched.add(oldest)
    for key in touched:
        if rows.get(key) and key in size:
            _upsert(db, key, json.dumps(rows[key]), now)
        else:
            db.execute("DELETE FROM memory_meta WHERE key = ?", (key,))


def _audit_refusals(db: "sqlite3.Connection", routing: KindRouting, source: str) -> None:
    """One ``work_state`` ``memory_events`` row per ``state`` item ``enforce`` refused."""
    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    code = vm.SemanticRejectCode.WORK_STATE
    if code not in vm._AUDITABLE_REJECT_CODES:
        return
    for decision in routing.refused:
        db.execute(
            "INSERT INTO memory_events (event_type, memory_type, memory_key, "
            "old_value, new_value, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                code.value,
                "semantic",
                decision.key,
                None,
                _utf8_clean(_value_text(decision.value)[:200]),
                source,
                vm._now_iso(),
            ),
        )


def record_kind_routing_in_transaction(
    store: "VectorMemoryStore",
    routing: KindRouting,
    source: str,
    *,
    now: datetime | None = None,
) -> bool:
    """Write *routing*'s counters, samples and refusals inside the caller's transaction.

    The caller holds ``store._db_lock`` and an open transaction, and commits. The
    log runs under its own savepoint and never raises, so a log that cannot be
    written leaves the caller's writes, and its transaction, as they were.
    Returns whether anything was written.
    """
    if routing.mode == MODE_OFF or not routing.decisions:
        return False
    instant = now or _utcnow()
    db = store.db
    db.execute("SAVEPOINT memory_kind_log")
    try:
        _bump_counts(db, routing, instant)
        if routing.mode == MODE_SHADOW:
            _sync_rules_version(db, instant)
            store_name = getattr(store, "_memory_store_name", None) or "default"
            _add_samples(db, routing, str(store_name), instant)
        _audit_refusals(db, routing, source)
    except Exception:
        db.execute("ROLLBACK TO memory_kind_log")
        logger.warning("Could not record memory kind routing", exc_info=True)
        return False
    finally:
        db.execute("RELEASE memory_kind_log")
    return True


def record_kind_routing(
    store: "VectorMemoryStore",
    routing: KindRouting,
    source: str,
    *,
    now: datetime | None = None,
) -> bool:
    """Write *routing*'s log in its own transaction; return whether it committed.

    Never raises: a log is not worth a pass. The consolidator calls it inside
    its publication hold, the same fence every other consolidation write takes.
    """
    if routing.mode == MODE_OFF or not routing.decisions:
        return False
    try:
        with store._db_lock:
            store.db.execute("BEGIN IMMEDIATE")
            try:
                wrote = record_kind_routing_in_transaction(store, routing, source, now=now)
                store.db.commit()
            except BaseException:
                store.db.rollback()
                raise
    except Exception:
        logger.warning("Could not record memory kind routing", exc_info=True)
        return False
    return wrote
