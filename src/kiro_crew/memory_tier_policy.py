"""Store a new consolidation lesson as a standing rule only when the person's words back it.

Consolidation asks the model to tag each lesson ``always`` (a standing rule) or
``on_topic`` (a finding), and a lesson with no tag is served as a standing rule.
The model guesses: most of what it files as standing is its own inference. This
policy decides the tier of each NEW consolidation lesson in code:

* a lesson the model tagged ``on_topic`` stays ``on_topic``;
* a lesson with no tag becomes ``on_topic``;
* a lesson tagged ``always`` stays ``always`` only when its ``user_quote`` passes
  :func:`quote_check`; otherwise it becomes ``on_topic``.

A lesson is never dropped. A person's own ``learn_add`` and dashboard writes do not
pass through here, and no stored row is changed.

``memory.lesson_tier_policy`` picks the mode:

* ``off`` (default) -- nothing is read or logged; prompts and writes are today's.
* ``shadow`` -- the prompt and every write are today's, byte for byte. The prompt
  does not ask for ``user_quote``, because asking would change which tier the
  model picks and the counts would stop describing today's writes. So the log
  counts every standing lesson as a would-be demotion (an upper bound), counts
  any quote the model offers unasked, and keeps the would-be demoted keys. A later
  ``learn_add`` whose rule is within cosine :data:`RETEACH_COSINE` of one of those
  keys counts as a re-teach: the person had to say it again.
* ``enforce`` -- the prompt asks for ``user_quote`` (:data:`QUOTE_INSTRUCTION`) and
  the policy above is applied.

A messaging-channel session is left as it is and not logged, as in
:mod:`kiro_crew.memory_origin_gate`: most channel transports write the person's
row without ``meta.human``, so no quote there could ever pass.

The log follows :mod:`kiro_crew.memory_origin_gate`: the store's ``memory_meta``
table, keys and counts only, never text, under the kind-routing caps:

* one counter row per UTC day (``memory_tier_log:counts:<day>``), by mode and path;
  rows older than ``COUNTER_DAYS_KEPT`` days are pruned;
* id rows (``memory_tier_log:ids:<day>``), shadow mode only: at most
  :data:`IDS_PER_DAY` a day and the kind-routing totals (``SAMPLES_TOTAL`` rows,
  ``SAMPLE_BYTES_CAP`` bytes), oldest day dropped first; ids stop
  ``SHADOW_SAMPLE_WINDOW_SECS`` after shadow first logged in the store, and that
  window restarts when :data:`RULES_VERSION` changes.
"""

from __future__ import annotations

import json
import logging
import unicodedata
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final, NamedTuple

from kiro_crew import memory_kind_routing as _kind_log
from kiro_crew.config.memory_sections import lesson_tier_policy_mode_from
from kiro_crew.crew_log.projection_workers import _RELAY_PREFIX
from kiro_crew.lesson_validation import (
    LESSON_APPLIES_ALWAYS,
    LESSON_APPLIES_ON_TOPIC,
    _relevance_tokens,
    authored_lesson_applies,
)
from kiro_crew.memory_origin_gate import TURN_HUMAN, _candidate_key, _demoted, classify_turn
from kiro_crew.vector_memory_constants import _MAX_LESSONS_PER_CONSOLIDATION

if TYPE_CHECKING:
    import sqlite3

    from kiro_crew.vector_memory import VectorMemoryStore

logger = logging.getLogger(__name__)

MODE_OFF: Final = "off"
MODE_SHADOW: Final = "shadow"
MODE_ENFORCE: Final = "enforce"

PATH_CONSOLIDATION: Final = "consolidation"
PATH_RETEACH: Final = "reteach"

QUOTE_ABSENT: Final = "absent"
QUOTE_PASS: Final = "pass"
QUOTE_FAIL: Final = "fail"

#: A quote needs at least this many significant tokens (the shared relevance
#: tokenizer: Latin or digit words of 3+ characters, CJK character pairs).
MIN_QUOTE_TOKENS: Final = 3
#: A ``learn_add`` this close to a would-be demoted lesson is a re-teach.
RETEACH_COSINE: Final = 0.85

IDS_PER_DAY: Final = 150
#: Bump when the policy or the quote check changes, so the log can tell ids
#: taken under the old rules from the new ones.
RULES_VERSION: Final = 1

_META_PREFIX: Final = "memory_tier_log:"
COUNTS_PREFIX: Final = _META_PREFIX + "counts:"
IDS_PREFIX: Final = _META_PREFIX + "ids:"
SHADOW_STARTED_KEY: Final = _META_PREFIX + "shadow_started_at"
RULES_VERSION_KEY: Final = _META_PREFIX + "rules_version"

#: Appended to the consolidation prompt's lesson schema under ``enforce`` only.
QUOTE_INSTRUCTION: Final = (
    'For "applies": "always", add "user_quote": the words of one of the user\'s own '
    "messages that state this rule, copied exactly. Never fabricate or paraphrase a "
    'quote. A lesson with no matching quote, or with no "applies", is stored on_topic. '
)


def configured_mode() -> str:
    """``memory.lesson_tier_policy`` from the live config; the default when unreadable."""
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        raw = getattr(KiroCrewConfig.load().memory, "lesson_tier_policy", None)
    except Exception:
        logger.debug("Could not read memory.lesson_tier_policy; using the default", exc_info=True)
        raw = None
    return lesson_tier_policy_mode_from(raw)


def prompt_rule_for(mode: str) -> str:
    """The text the lesson schema gains under *mode*: only ``enforce`` asks for a quote."""
    return QUOTE_INSTRUCTION if lesson_tier_policy_mode_from(mode) == MODE_ENFORCE else ""


def _normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _is_relayed(row: dict) -> bool:
    meta = row.get("meta")
    content = row.get("content")
    return bool(isinstance(meta, dict) and meta.get("relay")) or (
        isinstance(content, str) and content.lstrip().startswith(_RELAY_PREFIX)
    )


def quote_check(quote: object, rows: object) -> str:
    """Whether *quote* is the person's own words in one of *rows*.

    After NFKC normalization, case folding and whitespace collapse, the quote must
    be a substring of one row the person sent (:func:`classify_turn` says human)
    that carries no relay marker, and hold at least :data:`MIN_QUOTE_TOKENS`
    significant tokens. A row is matched in its stored form, which is the final
    form of an edited turn.
    """
    if not isinstance(quote, str) or not quote.strip():
        return QUOTE_ABSENT
    wanted = _normalized(quote)
    if len(_relevance_tokens(wanted)) < MIN_QUOTE_TOKENS or not isinstance(rows, list):
        return QUOTE_FAIL
    for row in rows:
        if classify_turn(row) != TURN_HUMAN or _is_relayed(row):
            continue
        content = row.get("content")
        if isinstance(content, str) and wanted in _normalized(content):
            return QUOTE_PASS
    return QUOTE_FAIL


class TierPolicy(NamedTuple):
    """The result to write, and what the policy decided on the way."""

    result: dict
    mode: str
    #: Lessons the policy read.
    lessons: int = 0
    #: Of those, how many carried no tier, and how many carried ``always``.
    unstated: int = 0
    always: int = 0
    #: Quote outcomes over the ``always`` lessons.
    quote_present: int = 0
    quote_pass: int = 0
    quote_fail: int = 0
    #: Lessons ``enforce`` stores as ``on_topic`` (or would, under ``shadow``).
    demoted: int = 0
    #: The keys of those lessons, where derivable.
    demoted_keys: tuple[str, ...] = ()

    @property
    def loggable(self) -> bool:
        return self.mode != MODE_OFF and self.lessons > 0


def apply_tier_policy(
    result: dict, rows: list[dict], mode: str, *, session_key: str = ""
) -> TierPolicy:
    """Apply *mode* to the ``lessons`` the model extracted from span *rows*.

    *rows* are the rows the model read. ``off`` returns *result* itself without
    reading *rows*; ``shadow`` returns it unchanged; ``enforce`` returns a shallow
    copy in which every lesson but a quoted standing rule carries ``applies:
    on_topic`` and no NOT-clause, for the
    reason ``memory_origin_gate._demoted`` gives: an exact-rule match with a stored
    standing row would otherwise attach the clause to that row. Only the first
    ``_MAX_LESSONS_PER_CONSOLIDATION`` lessons are read, the ones a writer reaches.
    """
    from kiro_crew.messaging.link import is_channel_session_key

    mode = lesson_tier_policy_mode_from(mode)
    items = result.get("lessons") if isinstance(result, dict) else None
    if mode == MODE_OFF or not isinstance(items, list) or is_channel_session_key(session_key):
        return TierPolicy(result, mode)
    window = items[:_MAX_LESSONS_PER_CONSOLIDATION]
    lessons = [i for i in window if isinstance(i, dict) and isinstance(i.get("rule"), str)]
    unstated = always = present = passed = 0
    demote: list[dict] = []
    for item in lessons:
        tier = authored_lesson_applies(item.get("applies"))
        if tier == LESSON_APPLIES_ON_TOPIC:
            continue
        if tier is None:
            unstated += 1
            demote.append(item)
            continue
        always += 1
        outcome = quote_check(item.get("user_quote"), rows)
        present += int(outcome != QUOTE_ABSENT)
        passed += int(outcome == QUOTE_PASS)
        # Shadow did not ask for a quote, so every standing lesson counts: the
        # upper bound of what enforce would demote with today's answers.
        if mode == MODE_SHADOW or outcome != QUOTE_PASS:
            demote.append(item)
    keys = tuple(dict.fromkeys(k for i in demote for k in _stored_keys(i)))
    policy = TierPolicy(
        result,
        mode,
        len(lessons),
        unstated,
        always,
        present,
        passed,
        present - passed,
        len(demote),
        keys,
    )
    if mode == MODE_SHADOW or not lessons:
        return policy
    # Every lesson that is not a quoted standing rule is written as a finding with
    # no NOT-clause, one the model already tagged ``on_topic`` included: its clause
    # could otherwise enrich a stored standing row with the same rule.
    demoted_ids = {id(i) for i in demote}
    fenced = {id(i) for i in lessons if id(i) in demoted_ids or not _is_always(i)}
    kept = [_demoted(i) if id(i) in fenced else i for i in window]
    return policy._replace(result={**result, "lessons": kept})


def _is_always(item: dict) -> bool:
    return authored_lesson_applies(item.get("applies")) == LESSON_APPLIES_ALWAYS


def _stored_keys(item: dict) -> tuple[str, ...]:
    """The keys *item* may be stored under: the member store strips the rule, the
    default store's ``write_lesson`` keys it as given. Both are logged so a
    re-teach finds the row whichever store wrote it."""
    keys = [_candidate_key(item)]
    rule = item.get("rule")
    if isinstance(rule, str) and rule != rule.strip():
        from kiro_crew.project_scope import canonical_scope
        from kiro_crew.vector_memory import _lesson_key

        try:
            keys.append(_lesson_key(rule, canonical_scope(item.get("repo_scope"))))
        except Exception:  # an unencodable or malformed value: counted, not named
            pass
    return tuple(k for k in keys if k)


# ── The bounded log ─────────────────────────────────────────────────────────


def _bump(db: "sqlite3.Connection", mode: str, path: str, fields: dict, now: datetime) -> None:
    day = now.date().isoformat()
    key = COUNTS_PREFIX + day
    counts = _kind_log._loads_dict(_kind_log._read(db, key))
    by_path = counts.get(mode)
    by_path = by_path if isinstance(by_path, dict) else {}
    entry = by_path.get(path)
    entry = entry if isinstance(entry, dict) else {}
    for name, n in fields.items():
        entry[name] = _kind_log._count(entry.get(name)) + n
    by_path[path] = entry
    counts[mode] = by_path
    _kind_log._upsert(db, key, json.dumps(counts, sort_keys=True), now.isoformat())
    cutoff = (now - timedelta(days=_kind_log.COUNTER_DAYS_KEPT)).date().isoformat()
    db.execute(
        "DELETE FROM memory_meta WHERE key GLOB ? AND key < ?",
        (COUNTS_PREFIX + "*", COUNTS_PREFIX + cutoff),
    )


def _add_ids(db: "sqlite3.Connection", path: str, keys: tuple[str, ...], now: datetime) -> None:
    """Append *keys* to today's id row, within every cap."""
    if not keys:
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
    today += [{"key": k, "path": path, "rules": RULES_VERSION} for k in keys[:room]]
    rows: dict[str, list] = {
        str(key): _kind_log._loads_list(raw)
        for key, raw in db.execute(
            "SELECT key, value FROM memory_meta WHERE key GLOB ? ORDER BY key",
            (IDS_PREFIX + "*",),
        ).fetchall()
    }
    rows[today_key] = today
    _kind_log._trim_samples(db, rows, today_key, stamp)


def _write_log(store: "VectorMemoryStore", write) -> bool:
    """Run *write(db)* in its own transaction under the store's lock; never raise."""
    try:
        with store._db_lock:
            db = store.db
            db.execute("BEGIN IMMEDIATE")
            try:
                write(db)
                db.commit()
            except BaseException:
                db.rollback()
                raise
    except Exception:
        logger.warning("Could not record the lesson tier policy", exc_info=True)
        return False
    return True


def record_tier_policy(
    store: "VectorMemoryStore", policy: TierPolicy, *, now: datetime | None = None
) -> bool:
    """Write *policy*'s counters (and, under ``shadow``, its ids); return whether it committed."""
    if not policy.loggable:
        return False
    instant = now or _kind_log._utcnow()
    fields = {
        "passes": 1,
        "lessons": policy.lessons,
        "unstated": policy.unstated,
        "always": policy.always,
        "quote_present": policy.quote_present,
        "quote_pass": policy.quote_pass,
        "quote_fail": policy.quote_fail,
        "demoted": policy.demoted,
    }

    def write(db: "sqlite3.Connection") -> None:
        _bump(db, policy.mode, PATH_CONSOLIDATION, fields, instant)
        if policy.mode == MODE_SHADOW:
            _add_ids(db, PATH_CONSOLIDATION, policy.demoted_keys, instant)

    return _write_log(store, write)


def _would_demote_keys(db: "sqlite3.Connection") -> list[str]:
    keys: set[str] = set()
    for (raw,) in db.execute(
        "SELECT value FROM memory_meta WHERE key GLOB ?", (IDS_PREFIX + "*",)
    ).fetchall():
        for entry in _kind_log._loads_list(raw):
            if isinstance(entry, dict) and entry.get("path") == PATH_CONSOLIDATION:
                key = entry.get("key")
                if isinstance(key, str):
                    keys.add(key)
    return sorted(keys)


def _reteach_match(store: "VectorMemoryStore", rule_emb: list[float]) -> str | None:
    """The would-be demoted key closest to *rule_emb* above :data:`RETEACH_COSINE`.

    Reads only rows the shadow log names (at most ``SAMPLES_TOTAL``). A row with
    no vector, or one of another width, never matches.

    shortcut: a stored vector from an earlier embedding space of the same width
    is compared as is; read the space generation per row if a model swap lands
    during a shadow run.
    """
    scorer = store._stored_similarity_scorer(rule_emb)
    with store._db_lock:
        keys = _would_demote_keys(store.db)
        best, best_key = RETEACH_COSINE, None
        for start in range(0, len(keys), 500):
            chunk = keys[start : start + 500]
            marks = ",".join("?" * len(chunk))
            for key, blob in store.db.execute(
                f"SELECT key, embedding FROM semantic_memory "  # nosec B608: placeholders only
                f"WHERE is_deleted = 0 AND embedding IS NOT NULL AND key IN ({marks})",
                chunk,
            ).fetchall():
                score = scorer({"embedding": blob})
                if score > best:
                    best, best_key = score, str(key)
    return best_key


def record_reteach(
    store: "VectorMemoryStore",
    rule_emb: list[float] | None,
    mode: str,
    *,
    now: datetime | None = None,
) -> bool:
    """Count one ``learn_add`` under ``shadow``, and whether it re-teaches a would-be demotion.

    Call it before the write, so a lesson the write merges into is still there to
    compare with. A matched key joins today's id row under :data:`PATH_RETEACH`.
    Never raises.
    """
    if lesson_tier_policy_mode_from(mode) != MODE_SHADOW or not rule_emb:
        return False
    instant = now or _kind_log._utcnow()
    try:
        matched = _reteach_match(store, rule_emb)
    except Exception:
        logger.warning("Could not compare a learn_add with the lesson tier log", exc_info=True)
        return False

    def write(db: "sqlite3.Connection") -> None:
        _bump(db, MODE_SHADOW, PATH_RETEACH, {"checked": 1, "matched": int(bool(matched))}, instant)
        if matched:
            _add_ids(db, PATH_RETEACH, (matched,), instant)

    return _write_log(store, write)
