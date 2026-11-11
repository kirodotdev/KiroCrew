"""Keep lessons learned from turns no person sent out of the standing tier.

A transcript row with role ``user`` is not always a person: ``session_send``
delivers another session's text under that role, and automation adds auto-nudge,
cron, heartbeat and work-ledger turns. Consolidation reads them all as the user,
so a worker whose every input came from another agent writes standing rules no
person taught. :func:`classify_turn` reads markers the gateway already writes,
never wording or a model guess. Human is an ALLOWLIST, as
``history.HUMAN_TURN_META_KEY`` requires of every reader of human activity:

* ``human`` -- a ``user`` row the send paths a person reaches stamped
  ``meta.human`` (a steer the person typed carries it too);
* ``agent`` -- an unmarked ``user`` row with a ``meta.relay`` record or the
  ``session_send`` envelope prefix;
* ``automatic`` -- every other input row: a ``nudge``, ``inject``, ``subagent``
  or ``system`` role, or an unmarked ``user`` row (an Issue Radar wake, a cron
  prompt, a transcript written before the marker existed).

``memory.lesson_origin_gate`` picks the mode:

* ``off`` (default) -- nothing is read or logged; prompts and writes are today's.
* ``shadow`` -- every lesson is written as before; the log counts what
  ``enforce`` would demote.
* ``enforce`` -- a consolidation span with no human turn stores its standing
  lessons (``always`` or untagged) as ``on_topic`` findings, never dropping one.
  A ``learn_add`` whose triggering turn is not human is stored ``on_topic`` under
  :data:`LESSON_SOURCE_AGENT_LEARN_ADD`, so it never carries ``user_explicit``
  authority.

``kirocrew learn add`` has no chat to read. Run from a shell inside an agent
session (the markers ``cloud.aws._in_agent_session`` reads), it counts as a
``learn_add`` no person triggered, even when a person asked the agent to run it:
the shell cannot tell. Run from a person's own terminal, it is never gated.
A span that holds a human turn keeps its tiers, even when agent turns sit beside
it: tracing one lesson to one turn is not deterministic, so a mixed span is a
known gap. A ``learn_add`` from a session with no live chat (a sub-agent, a
channel thread) has no turn to read and is left as it is.

The log follows the kind-routing log (:mod:`kiro_crew.memory_kind_routing`): it
lives in the store's ``memory_meta`` table, holds lesson keys and counts only,
never text, and is bounded:

* one counter row per UTC day (``memory_origin_log:counts:<day>``), by mode, path
  and count; rows older than ``COUNTER_DAYS_KEPT`` days are pruned;
* id rows (``memory_origin_log:ids:<day>``), shadow mode only: at most
  :data:`IDS_PER_DAY` a day and the kind-routing sample caps in all
  (``SAMPLES_TOTAL`` rows, ``SAMPLE_BYTES_CAP`` bytes), oldest day dropped first;
* ids stop ``SHADOW_SAMPLE_WINDOW_SECS`` after shadow first logged in the store,
  and that window restarts when :data:`RULES_VERSION` changes.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final, NamedTuple

from kiro_crew import memory_kind_routing as _kind_log
from kiro_crew.config.memory_sections import lesson_origin_gate_mode_from
from kiro_crew.crew_log.projection_workers import _RELAY_PREFIX
from kiro_crew.history_schema import HUMAN_TURN_META_KEY
from kiro_crew.lesson_validation import LESSON_APPLIES_ON_TOPIC, authored_lesson_applies
from kiro_crew.vector_memory_constants import _MAX_LESSONS_PER_CONSOLIDATION

if TYPE_CHECKING:
    import sqlite3

    from kiro_crew.vector_memory import VectorMemoryStore

logger = logging.getLogger(__name__)

TURN_HUMAN: Final = "human"
TURN_AGENT: Final = "agent"
TURN_AUTOMATIC: Final = "automatic"

MODE_OFF: Final = "off"
MODE_SHADOW: Final = "shadow"
MODE_ENFORCE: Final = "enforce"

PATH_CONSOLIDATION: Final = "consolidation"
PATH_LEARN_ADD: Final = "learn_add"
#: ``kirocrew learn add`` run from a shell inside an agent session.
PATH_CLI_LEARN_ADD: Final = "cli_learn_add"

#: The ``source`` of a ``learn_add`` an agent-sent or automatic turn triggered,
#: under ``enforce``. Anything but ``user_explicit`` ranks below a person's write
#: in every authority check (``lesson_write._outranks_write``).
LESSON_SOURCE_AGENT_LEARN_ADD: Final = "agent_learn_add"

#: Roles the gateway writes for turns no person typed.
_AUTOMATIC_ROLES: Final = frozenset({"nudge", "inject", "subagent", "system"})

IDS_PER_DAY: Final = 150
#: Bump when what counts as human, agent or automatic changes, so the log can
#: tell ids taken under the old rules from the new ones.
RULES_VERSION: Final = 1

_META_PREFIX: Final = "memory_origin_log:"
COUNTS_PREFIX: Final = _META_PREFIX + "counts:"
IDS_PREFIX: Final = _META_PREFIX + "ids:"
SHADOW_STARTED_KEY: Final = _META_PREFIX + "shadow_started_at"
RULES_VERSION_KEY: Final = _META_PREFIX + "rules_version"


def configured_mode() -> str:
    """``memory.lesson_origin_gate`` from the live config; the default when unreadable."""
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        raw = getattr(KiroCrewConfig.load().memory, "lesson_origin_gate", None)
    except Exception:
        logger.debug("Could not read memory.lesson_origin_gate; using the default", exc_info=True)
        raw = None
    return lesson_origin_gate_mode_from(raw)


def classify_turn(row: object) -> str | None:
    """Who sent transcript *row*: human, agent or automatic; ``None`` for no input row."""
    if not isinstance(row, dict):
        return None
    role = row.get("role")
    if role in _AUTOMATIC_ROLES:
        return TURN_AUTOMATIC
    if role != "user":
        return None
    meta = row.get("meta")
    meta = meta if isinstance(meta, dict) else {}
    if meta.get(HUMAN_TURN_META_KEY) is True:
        return TURN_HUMAN
    content = row.get("content")
    if meta.get("relay") or (
        isinstance(content, str) and content.lstrip().startswith(_RELAY_PREFIX)
    ):
        return TURN_AGENT
    return TURN_AUTOMATIC


def has_human_turn(rows: object) -> bool:
    """Whether *rows* hold at least one turn a person sent."""
    return isinstance(rows, list) and any(classify_turn(row) == TURN_HUMAN for row in rows)


def triggering_turn(rows: object) -> str | None:
    """Who sent the latest input row in *rows*; ``None`` when there is none.

    Fail-closed on purpose: the latest row of ANY input role counts, so a
    subagent completion, a cron or app inject, or a note the gateway wrote inside
    the turn all make the answer non-human. Naming which rows open a turn was
    tried and kept missing one; the cost of this rule is that a person's
    ``learn_add`` made after such a note is stored as a finding, never lost.
    """
    if not isinstance(rows, list):
        return None
    for row in reversed(rows):
        turn = classify_turn(row)
        if turn is not None:
            return turn
    return None


def _is_standing(item: dict) -> bool:
    """Whether *item* would be stored in the standing tier (``always`` or untagged)."""
    return authored_lesson_applies(item.get("applies")) != LESSON_APPLIES_ON_TOPIC


def _candidate_key(item: dict) -> str | None:
    """The key *item* would be stored under, or ``None`` when it cannot be derived."""
    from kiro_crew.project_scope import canonical_scope
    from kiro_crew.vector_memory import _lesson_key

    rule = item.get("rule")
    if not isinstance(rule, str) or not rule.strip():
        return None
    try:
        return _lesson_key(rule.strip(), canonical_scope(item.get("repo_scope")))
    except Exception:  # an unencodable or malformed value: counted, not named
        return None


class OriginGate(NamedTuple):
    """The result to write, and what the gate decided on the way."""

    result: dict
    mode: str
    path: str = PATH_CONSOLIDATION
    #: Lessons the gate read.
    lessons: int = 0
    #: Of those, how many sit in the standing tier as submitted.
    standing: int = 0
    #: True when no turn the gate read was a person's.
    no_human: bool = False
    #: Standing lessons ``enforce`` demotes (or would, under ``shadow``).
    demoted: int = 0
    #: The keys of those lessons, where derivable.
    demoted_keys: tuple[str, ...] = ()

    @property
    def loggable(self) -> bool:
        return self.mode != MODE_OFF and self.lessons > 0


def gate_consolidation_lessons(
    result: dict, rows: list[dict], mode: str, *, session_key: str = ""
) -> OriginGate:
    """Apply *mode* to the ``lessons`` the model extracted from span *rows*.

    *rows* are the rows the model read. ``off`` returns *result* itself without
    reading *rows*; ``shadow`` returns it unchanged; ``enforce`` returns a shallow
    copy, for a span with no human turn, whose lessons all carry ``applies:
    on_topic`` and no NOT-clause. Only the first
    ``_MAX_LESSONS_PER_CONSOLIDATION`` lessons are read, the ones a writer reaches.

    A messaging-channel session (*session_key* a channel key) is left as it is and
    not logged: most channel transports write the person's row without
    ``meta.human``, so the allowlist would read a person's own rules as automatic.
    """
    from kiro_crew.messaging.link import is_channel_session_key

    mode = lesson_origin_gate_mode_from(mode)
    items = result.get("lessons") if isinstance(result, dict) else None
    if mode == MODE_OFF or not isinstance(items, list) or is_channel_session_key(session_key):
        return OriginGate(result, mode)
    window = items[:_MAX_LESSONS_PER_CONSOLIDATION]
    lessons = [i for i in window if isinstance(i, dict) and isinstance(i.get("rule"), str)]
    standing = [i for i in lessons if _is_standing(i)]
    no_human = not has_human_turn(rows)
    demote = standing if no_human else []
    keys = tuple(k for k in (_candidate_key(i) for i in demote) if k)
    gate = OriginGate(
        result, mode, PATH_CONSOLIDATION, len(lessons), len(standing), no_human, len(demote), keys
    )
    if mode == MODE_SHADOW or not no_human or not lessons:
        return gate
    # Every lesson of a span no person spoke in is written as a finding with no
    # NOT-clause, including one the model already tagged ``on_topic``: its clause
    # could otherwise enrich a standing row with the same rule (see ``_demoted``).
    fenced = {id(i) for i in lessons}
    kept = [_demoted(i) if id(i) in fenced else i for i in window]
    return gate._replace(result={**result, "lessons": kept})


def _demoted(item: dict) -> dict:
    """*item* as a finding, with no NOT-clause.

    The clause is dropped so a demoted lesson whose rule already exists as a
    standing row cannot enrich that row: an exact-rule re-submit keeps the stored
    tier and attaches the submitted clause, which would carry agent text into the
    standing tier after all.
    """
    demoted = {k: v for k, v in item.items() if k != "negative"}
    demoted["applies"] = LESSON_APPLIES_ON_TOPIC
    return demoted


class LearnAddGate(NamedTuple):
    """The tier and source a ``learn_add`` is written with, and the log entry.

    ``drop_negative`` tells the caller to write no NOT-clause, for the reason
    :func:`_demoted` gives.
    """

    applies: str | None
    source: str
    gate: OriginGate
    drop_negative: bool = False


def gate_learn_add(
    applies: str | None, rows: object, mode: str, *, rule: str = "", repo_scope: str | None = None
) -> LearnAddGate:
    """Apply *mode* to one ``learn_add`` the agent made in a chat holding *rows*.

    The triggering turn is the chat's latest input row. With no such row
    the write is left as submitted. ``enforce`` stores a write that an agent or
    automatic turn triggered as ``on_topic`` under
    :data:`LESSON_SOURCE_AGENT_LEARN_ADD`.
    """
    mode = lesson_origin_gate_mode_from(mode)
    trigger = None if mode == MODE_OFF else triggering_turn(rows)
    return gate_learn_add_turn(applies, trigger, mode, rule=rule, repo_scope=repo_scope)


def gate_learn_add_turn(
    applies: str | None,
    trigger: str | None,
    mode: str,
    *,
    rule: str = "",
    repo_scope: str | None = None,
    path: str = PATH_LEARN_ADD,
) -> LearnAddGate:
    """:func:`gate_learn_add` for a caller that already knows the *trigger* turn class.

    ``None`` (no triggering turn) leaves the write as submitted, as does ``off``.
    """
    mode = lesson_origin_gate_mode_from(mode)
    unchanged = LearnAddGate(applies, "user_explicit", OriginGate({}, mode, path))
    if mode == MODE_OFF or trigger is None:
        return unchanged
    item = {"rule": rule, "applies": applies, "repo_scope": repo_scope}
    standing = _is_standing(item)
    no_human = trigger != TURN_HUMAN
    demoted = int(standing and no_human)
    key = _candidate_key(item) if demoted else None
    gate = OriginGate({}, mode, path, 1, int(standing), no_human, demoted, (key,) if key else ())
    if mode == MODE_ENFORCE and no_human:
        return LearnAddGate(LESSON_APPLIES_ON_TOPIC, LESSON_SOURCE_AGENT_LEARN_ADD, gate, True)
    return LearnAddGate(applies, "user_explicit", gate)


# ── The bounded log ─────────────────────────────────────────────────────────


def _bump_counts(db: "sqlite3.Connection", gate: OriginGate, now: datetime) -> None:
    day = now.date().isoformat()
    key = COUNTS_PREFIX + day
    counts = _kind_log._loads_dict(_kind_log._read(db, key))
    by_path = counts.get(gate.mode)
    by_path = by_path if isinstance(by_path, dict) else {}
    entry = by_path.get(gate.path)
    entry = entry if isinstance(entry, dict) else {}
    for name, n in (
        ("passes", 1),
        ("no_human", int(gate.no_human)),
        ("lessons", gate.lessons),
        ("standing", gate.standing),
        ("demoted", gate.demoted),
    ):
        entry[name] = _kind_log._count(entry.get(name)) + n
    by_path[gate.path] = entry
    counts[gate.mode] = by_path
    _kind_log._upsert(db, key, json.dumps(counts, sort_keys=True), now.isoformat())
    cutoff = (now - timedelta(days=_kind_log.COUNTER_DAYS_KEPT)).date().isoformat()
    db.execute(
        "DELETE FROM memory_meta WHERE key GLOB ? AND key < ?",
        (COUNTS_PREFIX + "*", COUNTS_PREFIX + cutoff),
    )


def _add_ids(db: "sqlite3.Connection", gate: OriginGate, now: datetime) -> None:
    """Append the demoted keys to today's id row, within every cap."""
    if not gate.demoted_keys:
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
        {"key": k, "path": gate.path, "rules": RULES_VERSION} for k in gate.demoted_keys[:room]
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


def record_origin_gate(
    store: "VectorMemoryStore", gate: OriginGate, *, now: datetime | None = None
) -> bool:
    """Write *gate*'s counters (and, under ``shadow``, its ids); return whether it committed.

    Its own transaction under the store's lock. Never raises: a log is not worth
    a pass or a ``learn_add``.
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
                    _add_ids(db, gate, instant)
                db.commit()
            except BaseException:
                db.rollback()
                raise
    except Exception:
        logger.warning("Could not record the lesson origin gate", exc_info=True)
        return False
    return True
