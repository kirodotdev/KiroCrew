"""Shared pieces that let ``kirocrew memory shadow report`` tie a shadow decision to an outcome.

A memory stage under ``shadow`` records what its new policy would drop, demote or
fold. On its own that says how much would change, not whether the change would
hurt. Three additive pieces make the join possible, all ids, hashes and counts:

* a session tag (:func:`session_tag`, the first :data:`TAG_CHARS` hex characters of
  the SHA-256 of the session key) and a UTC timestamp on each id entry a write
  stage keeps (:func:`stamp`), so an entry can be ordered against what followed;
* a control sample (:func:`control_keys`): a few rows the stage would KEEP, picked
  by hashing the session tag with the key, so the miss rate of what a stage drops
  can be compared with the miss rate of what it keeps. A write stage keeps them in
  its own ``control:<day>`` rows (:func:`add_control`), a render stage on its
  receipt field;
* outcome receipts (:mod:`kiro_crew.injection_receipts`, kinds ``recall`` and
  ``learn_add``): the memory keys a ``memory_recall`` returned, and the stored
  lessons within :data:`RETEACH_COSINE` of a ``learn_add`` (:func:`reteach_neighbours`).
  Never the query, the rule or any row text.

Nothing here is written while every stage flag is ``off`` (:func:`observing`), and
the callers write nothing for an incognito or temporary session. Readers accept an
entry without the new fields: an old entry has no tag and is placed at the end of
its own day.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from kiro_crew import memory_kind_routing as _caps

if TYPE_CHECKING:
    import sqlite3

    from kiro_crew.vector_memory import VectorMemoryStore

logger = logging.getLogger(__name__)

TAG_CHARS: Final = 12
#: One kept row in this many hundred joins the control sample.
CONTROL_PERCENT: Final = 10
#: Most control keys one receipt field or one pass names.
CONTROL_PER_ENTRY: Final = 20
#: Most control entries one write stage keeps a day.
CONTROL_PER_DAY: Final = 20
#: Most memory keys one outcome receipt names.
OUTCOME_IDS_CAP: Final = 20
#: A ``learn_add`` this close to a stored lesson re-teaches it (the tier policy's bar).
RETEACH_COSINE: Final = 0.85
#: Bump when what a control or outcome entry means changes.
TRACE_VERSION: Final = 1


def session_tag(session_key: object) -> str:
    """The short hash a trace entry carries for *session_key*; ``""`` for none."""
    if not isinstance(session_key, str) or not session_key:
        return ""
    return hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:TAG_CHARS]


def stamp(session_key: object, now: datetime) -> dict[str, str]:
    """The fields added to an id entry: ``at`` always, ``sk`` when there is a session."""
    fields = {"at": now.isoformat()}
    tag = session_tag(session_key)
    if tag:
        fields["sk"] = tag
    return fields


def in_control(tag: str, key: str) -> bool:
    """Whether kept row *key* joins the control sample for session *tag*.

    A hash, not a draw, so every render of one session picks the same rows and a
    re-render adds no noise.
    """
    digest = hashlib.sha256(f"{tag}\0{key}".encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % 100 < CONTROL_PERCENT


def control_keys(session_key: object, kept: Iterable[object]) -> list[str]:
    """The control sample of *kept* keys, at most :data:`CONTROL_PER_ENTRY`, in order."""
    tag = session_tag(session_key)
    picked: list[str] = []
    for key in dict.fromkeys(k for k in kept if isinstance(k, str) and k):
        if in_control(tag, key):
            picked.append(key)
            if len(picked) >= CONTROL_PER_ENTRY:
                break
    return picked


def observing(memory_config: object = None) -> bool:
    """Whether any memory stage flag is on (``shadow``, ``enforce`` or ``propose``).

    Reads the live config when *memory_config* is ``None``; ``False`` when it is
    unreadable, so an outcome receipt is never written on a guess.
    """
    from kiro_crew.memory_shadow_report import STAGES

    if memory_config is None:
        try:
            from kiro_crew.config.loader import KiroCrewConfig

            memory_config = KiroCrewConfig.load().memory
        except Exception:
            logger.debug("Could not read the memory stage flags", exc_info=True)
            return False
    return any(s.mode_from(getattr(memory_config, s.name, None)) != "off" for s in STAGES)


def recall_ids(payload: object, lessons: Iterable[object] = ()) -> list[str]:
    """The memory keys a recall response delivered, at most :data:`OUTCOME_IDS_CAP`.

    *lessons* are the keys of the lesson block the recall rendered
    (``injection_receipts.RECALLED_LESSONS``), listed first since the stages
    score lessons; then the keys of the delivered facts.

    shortcut: a lesson block the payload bound later shortens still counts all
    its keys; bound per lesson if recall payloads start clipping lesson text.
    """
    retrieval = payload.get("retrieval") if isinstance(payload, dict) else None
    facts = retrieval.get("facts") if isinstance(retrieval, dict) else None
    keys = list(lessons)
    if isinstance(facts, list):
        keys += [f.get("key") for f in facts if isinstance(f, dict)]
    return list(dict.fromkeys(k for k in keys if isinstance(k, str) and k))[:OUTCOME_IDS_CAP]


def reteach_neighbours(store: "VectorMemoryStore", rule_emb: list[float] | None) -> list[str]:
    """The stored lessons within :data:`RETEACH_COSINE` of *rule_emb*, closest first.

    Called before the ``learn_add`` write, so a lesson the write merges into is
    still there. Reads keys and vectors only; a row with no vector, or one of
    another width, never matches. Never raises.

    shortcut: scans every live lesson vector of the store, like the write's own
    dedup scan; bound it by the stages' logged keys if lesson counts grow past a
    few thousand.
    """
    if not rule_emb:
        return []
    try:
        scorer = store._stored_similarity_scorer(rule_emb)
        scored: list[tuple[float, str]] = []
        with store._db_lock:
            for key, blob in store.db.execute(
                "SELECT key, embedding FROM semantic_memory WHERE is_deleted = 0 "
                "AND embedding IS NOT NULL AND key LIKE 'lesson.%'"
            ).fetchall():
                score = scorer({"embedding": blob})
                if score > RETEACH_COSINE:
                    scored.append((score, str(key)))
    except Exception:
        logger.warning("Could not compare a learn_add with the stored lessons", exc_info=True)
        return []
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [key for _, key in scored[:OUTCOME_IDS_CAP]]


def add_control(
    db: "sqlite3.Connection",
    prefix: str,
    keys: Iterable[str],
    *,
    session_key: object,
    rules: int,
    now: datetime,
) -> None:
    """Append the control sample of *keys* to *prefix*'s ``control:<day>`` row.

    Within :data:`CONTROL_PER_DAY` a day and the kind-routing totals for the
    family (``SAMPLES_TOTAL`` entries, ``SAMPLE_BYTES_CAP`` bytes, oldest day
    first), and only inside the stage's shadow window (``<prefix>shadow_started_at``).
    It never writes the window key itself, so the stage's own id log is unchanged.
    The caller holds the transaction.
    """
    picked = control_keys(session_key, keys)
    if not picked:
        return
    started = _caps._parse_instant(_caps._read(db, prefix + "shadow_started_at"))
    if started is not None and (now - started).total_seconds() >= _caps.SHADOW_SAMPLE_WINDOW_SECS:
        return
    family = prefix + "control:"
    today_key = family + now.date().isoformat()
    today = _caps._loads_list(_caps._read(db, today_key))[:CONTROL_PER_DAY]
    room = CONTROL_PER_DAY - len(today)
    if room <= 0:
        return
    today += [{"key": k, "rules": rules, **stamp(session_key, now)} for k in picked[:room]]
    rows: dict[str, list[Any]] = {
        str(key): _caps._loads_list(raw)
        for key, raw in db.execute(
            "SELECT key, value FROM memory_meta WHERE key GLOB ? ORDER BY key", (family + "*",)
        ).fetchall()
    }
    rows[today_key] = today
    _caps._trim_samples(db, rows, today_key, now.isoformat())
