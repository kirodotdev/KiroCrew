"""Record which lessons a turn's prompt carried, as a local per-turn receipt.

The lesson block of an assembled prompt is ranked against the request, trimmed to a
budget and filtered by topic, so the set a turn received changes from turn to turn
and cannot be rebuilt afterwards from the store. The transcript keeps only what the
person typed, and the usage record keeps only block sizes. This module is the
record of WHICH lessons went in: the renderer that decides a lesson tier's kept set
calls :func:`record` with that set, the turn that composed the prompt gathers it
with :func:`collect`, and the dispatch gate appends one line per admitted turn to
``<data home>/usage/injections/YYYY-MM-DD.jsonl`` with :func:`schedule_write`.

It follows :mod:`kiro_crew.context_omissions`: the collector is a
:class:`~contextvars.ContextVar` so two sessions rendering against one store never
see each other's ids, the composer's worker thread inherits it through the copied
context, and every call outside a collection is a no-op, so a recall tool, a
preview or a warm-up render records nothing.

A line carries memory keys (``lesson.<hash>``) and counts, never lesson text. The
keys are the join to the store's own rows; the transcript message id is the join to
the reply that follows. Incognito and temporary sessions write nothing.

A memory stage under a ``shadow`` or ``enforce`` flag adds its own field to the
line (:data:`STAGE_KINDS`, recorded with :func:`record_stage`): its mode, its
policy version and the ids its new policy would add to or drop from a block. A
line with no stage record is the same bytes it was before stages existed. Stage
fields are bounded by the kind-routing caps (:mod:`kiro_crew.memory_kind_routing`),
kept in ``stage-log.json`` beside the shards:

* one counter entry per UTC day, by kind and mode (lines, would-add and
  would-drop counts), pruned past ``COUNTER_DAYS_KEPT`` days;
* a stage field is written on at most ``SAMPLES_PER_DAY`` lines a day,
  ``SAMPLES_TOTAL`` lines and ``SAMPLE_BYTES_CAP`` bytes in all, and only for
  ``SHADOW_SAMPLE_WINDOW_SECS`` after the stage first wrote one; a new policy
  version restarts the window and the sample budget. Past a cap the line is
  written without the field and only the counters move.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

from kiro_crew import memory_kind_routing as _caps
from kiro_crew.config.paths import data_home
from kiro_crew.owner_only_files import mkdirs_owner_only, owner_only_opener_for

logger = logging.getLogger(__name__)

LESSONS: Final = "lessons"
FINDINGS: Final = "findings"
#: The per-message lessons ``memory.inject_lessons_per_turn`` adds to a follow-up.
TURN_LESSONS: Final = "turn_lessons"
#: ``memory.rule_authority_order``: the rule-block ids its order would add or drop.
RULE_AUTHORITY_ORDER: Final = "rule_authority_order"
#: ``memory.lesson_fold``: the rule-block ids it would fold, as ``[leader, folded]`` pairs.
LESSON_FOLD: Final = "lesson_fold"
#: ``memory.relevance_rule``: the findings, task-fact and activity ids its rule
#: would add or drop, each block recorded as one part.
RELEVANCE_RULE: Final = "relevance_rule"

#: The blocks a line always names, recorded with :func:`record`.
_BLOCK_KINDS: Final[tuple[str, ...]] = (LESSONS, FINDINGS, TURN_LESSONS)
#: Stage fields, recorded with :func:`record_stage` and named only when recorded.
STAGE_KINDS: Final[tuple[str, ...]] = (RULE_AUTHORITY_ORDER, LESSON_FOLD, RELEVANCE_RULE)

#: Every name a record is keyed by.
KINDS: Final[tuple[str, ...]] = _BLOCK_KINDS + STAGE_KINDS

#: The budgeted tiers, whose line also states how many rows the budget cut.
_COUNTED_KINDS: Final[tuple[str, ...]] = (LESSONS, FINDINGS)

_STAGE_LOG_NAME: Final = "stage-log.json"
#: One writer at a time reads and rewrites the stage log.
_STAGE_LOG_LOCK = threading.Lock()

_SINK: ContextVar[dict[str, dict[str, Any]] | None] = ContextVar(
    "kiro_crew_injection_receipts", default=None
)


@contextmanager
def collect() -> Iterator[dict[str, dict[str, Any]]]:
    """Gather every tier rendered while the block runs, keyed by kind.

    The yielded mapping is filled in place; read it after the block exits.
    """
    sink: dict[str, dict[str, Any]] = {}
    token = _SINK.set(sink)
    try:
        yield sink
    finally:
        _SINK.reset(token)


def replay(records: dict[str, dict[str, Any]]) -> None:
    """Record tiers another process rendered, as if they were rendered here.

    The twin of :func:`kiro_crew.context_omissions.replay`, for a read done in a
    recall child: its records were gathered there and belong to the build that took
    the result. Kinds outside :data:`KINDS` are ignored. Outside a collection this is
    a no-op, like :func:`record`.
    """
    sink = _SINK.get()
    if sink is None:
        return
    for kind, entry in records.items():
        if kind in STAGE_KINDS and isinstance(entry, dict):
            parts = entry.get("parts")
            if isinstance(parts, dict) and parts:
                for part, ids in parts.items():
                    if isinstance(part, str) and isinstance(ids, dict):
                        _put_stage(
                            sink,
                            kind,
                            entry.get("mode"),
                            entry.get("policy"),
                            _str_list(ids.get("would_add")),
                            _str_list(ids.get("would_drop")),
                            part,
                        )
                continue
            sink[kind] = _stage_entry(
                entry.get("mode"),
                entry.get("policy"),
                _str_list(entry.get("would_add")),
                _str_list(entry.get("would_drop")),
                _pair_list(entry.get("pairs")) if "pairs" in entry else None,
            )
        elif kind in KINDS and isinstance(entry, dict):
            ids = entry.get("ids")
            omitted = entry.get("omitted")
            sink[kind] = {
                "ids": [i for i in ids if isinstance(i, str)] if isinstance(ids, list) else [],
                "omitted": max(0, omitted) if isinstance(omitted, int) else 0,
            }


def row_key(row: object) -> str:
    """The memory key of a rendered lesson row, or ``""`` when it has none.

    Vector-store rows are mappings carrying their ``key``. The legacy JSONL store's
    rows carry no stable id, so they record as unnamed and only count.
    """
    if isinstance(row, dict):
        key = row.get("key")
        if isinstance(key, str):
            return key
    return ""


def record(kind: str, rows: Sequence[object] | Iterable[object], *, omitted: int = 0) -> None:
    """Record the rows tier *kind* rendered, replacing any earlier record of it.

    A tier can be rendered more than once in one composition (lessons are re-fitted
    when protected content runs over its ceiling), and the LAST render is the one
    the prompt carries, so a later record replaces an earlier one.
    """
    if kind not in _BLOCK_KINDS:
        raise ValueError(f"unknown receipt kind {kind!r}")
    sink = _SINK.get()
    if sink is None:
        return
    keys = [key for key in (row_key(row) for row in rows) if key]
    sink[kind] = {"ids": keys, "omitted": max(0, int(omitted))}


def _str_list(raw: object) -> list[str]:
    return [i for i in raw if isinstance(i, str)] if isinstance(raw, list) else []


def _pair_list(raw: object) -> list[list[str]]:
    if not isinstance(raw, (list, tuple)):
        return []
    return [
        [pair[0], pair[1]]
        for pair in raw
        if isinstance(pair, (list, tuple))
        and len(pair) == 2
        and all(isinstance(key, str) and key for key in pair)
    ]


def current_ids(kind: str) -> list[str]:
    """The ids tier *kind* last recorded in this collection; empty outside one."""
    sink = _SINK.get()
    entry = sink.get(kind) if sink is not None else None
    return _str_list(entry.get("ids")) if isinstance(entry, dict) else []


def _stage_entry(
    mode: object,
    policy: object,
    add: list[str],
    drop: list[str],
    pairs: list[list[str]] | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "mode": mode if isinstance(mode, str) else "",
        "policy": policy if isinstance(policy, int) and not isinstance(policy, bool) else 0,
        "would_add": add,
        "would_drop": drop,
    }
    # Only a stage that groups rows names pairs, so every other stage field keeps
    # its four keys.
    if pairs is not None:
        entry["pairs"] = pairs
    return entry


def _put_stage(
    sink: dict[str, dict[str, Any]],
    kind: str,
    mode: object,
    policy: object,
    add: list[str],
    drop: list[str],
    part: str,
) -> None:
    """Store one stage record; with *part*, replace only that part's ids.

    A stage that gates several blocks records each block as a part, so a block
    rendered again replaces its own ids and leaves the other blocks'. The line
    carries the union.
    """
    if not part:
        sink[kind] = _stage_entry(mode, policy, add, drop)
        return
    previous = sink.get(kind)
    raw = previous.get("parts") if isinstance(previous, dict) else None
    parts: dict[str, dict[str, list[str]]] = dict(raw) if isinstance(raw, dict) else {}
    parts[part] = {"would_add": add, "would_drop": drop}
    entry = _stage_entry(
        mode,
        policy,
        sorted({i for ids in parts.values() for i in ids["would_add"]}),
        sorted({i for ids in parts.values() for i in ids["would_drop"]}),
    )
    entry["parts"] = parts
    sink[kind] = entry


def record_stage(
    kind: str,
    *,
    mode: str,
    policy: int,
    would_add: Iterable[object] = (),
    would_drop: Iterable[object] = (),
    pairs: Iterable[tuple[str, str]] | None = None,
    part: str = "",
) -> None:
    """Record what stage *kind*'s new policy would change in this composition.

    *would_add* and *would_drop* are rows (or their keys) the new policy would put
    into or take out of the block the prompt carries. *pairs*, for a stage that
    groups rows, are ``(kept key, folded key)`` pairs. Like :func:`record`, a later
    record replaces an earlier one and a call outside a collection is a no-op;
    with *part*, only the earlier record of that part is replaced.
    """
    if kind not in STAGE_KINDS:
        raise ValueError(f"unknown receipt stage {kind!r}")
    sink = _SINK.get()
    if sink is None:
        return

    def keys(rows: Iterable[object]) -> list[str]:
        named = (row if isinstance(row, str) else row_key(row) for row in rows)
        return [key for key in named if key]

    if part:
        _put_stage(sink, kind, mode, policy, keys(would_add), keys(would_drop), part)
        return
    sink[kind] = _stage_entry(
        mode,
        policy,
        keys(would_add),
        keys(would_drop),
        _pair_list(list(pairs)) if pairs is not None else None,
    )


def build_line(
    receipts: dict[str, dict[str, Any]],
    *,
    session_key: str,
    turn_index: int,
    user_msg_id: str,
    model: str,
    agent: str,
    phase: str,
    store: str,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """The receipt line for one composition, or ``None`` when nothing was recorded.

    A turn whose composition rendered no lesson block (a per-turn build after the
    session start, with no per-message lessons) and recorded no stage writes no
    line: the lessons in front of the model are still the ones the session's
    earlier lines recorded. A stage record alone (an activity block gated with
    lessons switched off) still writes one, so its shadow data is kept.
    """
    if not any(kind in receipts for kind in KINDS):
        return None
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    head, sep, _ = session_key.partition(":")
    line: dict[str, Any] = {
        "ts": stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "session_key": session_key,
        "turn_index": int(turn_index),
        "user_msg_id": user_msg_id or "",
        "model": model or "",
        "agent": agent or "",
        "surface": (head.strip() or "dashboard") if sep else "dashboard",
        "phase": phase or "",
        "memory_store": store or "",
    }
    for kind in _BLOCK_KINDS:
        entry = receipts.get(kind) or {}
        line[kind] = list(entry.get("ids") or [])
        if kind in _COUNTED_KINDS:
            line[f"{kind}_omitted"] = int(entry.get("omitted") or 0)
    for kind in STAGE_KINDS:
        stage = receipts.get(kind)
        if isinstance(stage, dict):
            line[kind] = _stage_entry(
                stage.get("mode"),
                stage.get("policy"),
                _str_list(stage.get("would_add")),
                _str_list(stage.get("would_drop")),
                _pair_list(stage.get("pairs")) if "pairs" in stage else None,
            )
    return line


def user_row_mid(row: object) -> str:
    """The transcript id (``meta.mid``) of the row that opened the turn, or ``""``.

    *row* is the triggering row the turn froze on entry, before prompt expansion
    or a delivery could append another. Only a ``user`` row names a message a
    person sent; a synthetic turn (a nudge, a sub-agent completion) has none to
    join on.
    """
    if not isinstance(row, dict) or row.get("role") != "user":
        return ""
    from kiro_crew.dashboard.state import row_mid

    return row_mid(row) or ""


def records_session(memory_mode: object) -> bool:
    """Whether a session in *memory_mode* writes receipts: never a private one."""
    # Imported here: the lesson renderer imports this module, and the history
    # module's import chain reaches that renderer.
    from kiro_crew.history_derivation import is_incognito_transcript

    return not is_incognito_transcript(memory_mode)


def _receipt_dir() -> Path:
    return data_home() / "usage" / "injections"


def _as_dict(raw: object) -> dict[str, Any]:
    return raw if isinstance(raw, dict) else {}


def _sample(raw: object) -> tuple[int, int]:
    """A ``[lines, bytes]`` sample entry; a malformed one counts as full, never empty."""
    if raw is None:
        return 0, 0
    if (
        isinstance(raw, list)
        and len(raw) == 2
        and all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in raw)
    ):
        return raw[0], raw[1]
    return _caps.SAMPLES_TOTAL, _caps.SAMPLE_BYTES_CAP


def _cap_stage_fields(line: dict[str, Any], now: datetime) -> dict[str, Any]:
    """*line* with each stage field kept only while it is inside every cap.

    Counts every stage field in the stage log first, so a field dropped by a cap
    is still counted. The caller holds :data:`_STAGE_LOG_LOCK`.
    """
    present = [kind for kind in STAGE_KINDS if isinstance(line.get(kind), dict)]
    if not present:
        return line
    path = _receipt_dir() / _STAGE_LOG_NAME
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        state = {}
    # Any other read or parse failure raises: the caller then drops the field,
    # because a log it cannot read cannot bound it.
    state = _as_dict(state)
    counts = _as_dict(state.get("counts"))
    stages = _as_dict(state.get("stages"))
    day = now.date().isoformat()
    capped = dict(line)
    for kind in present:
        entry = line[kind]
        by_kind = _as_dict(counts.get(day))
        by_mode = _as_dict(by_kind.get(kind))
        tally = _as_dict(by_mode.get(str(entry.get("mode"))))
        for name, n in (
            ("lines", 1),
            ("would_add", len(entry.get("would_add") or ())),
            ("would_drop", len(entry.get("would_drop") or ())),
        ):
            tally[name] = _caps._count(tally.get(name)) + n
        by_mode[str(entry.get("mode"))] = tally
        by_kind[kind] = by_mode
        counts[day] = by_kind
        stage = _as_dict(stages.get(kind))
        raw_start = stage.get("started_at")
        started = _caps._parse_instant(raw_start) if isinstance(raw_start, str) else None
        if stage.get("policy") != entry.get("policy") or started is None:
            started = now
            stage = {"policy": entry.get("policy"), "started_at": now.isoformat()}
        # ``{day: [lines, bytes]}`` of fields written under this policy version.
        samples = _as_dict(stage.get("samples"))
        size = len(json.dumps(entry, separators=(",", ":")))
        spent = [_sample(v) for v in samples.values()]
        today = _sample(samples.get(day))
        if (
            (now - started).total_seconds() < _caps.SHADOW_SAMPLE_WINDOW_SECS
            and today[0] < _caps.SAMPLES_PER_DAY
            and sum(n for n, _ in spent) < _caps.SAMPLES_TOTAL
            and sum(b for _, b in spent) + size <= _caps.SAMPLE_BYTES_CAP
        ):
            samples[day] = [today[0] + 1, today[1] + size]
        else:
            del capped[kind]
        stage["samples"] = samples
        stages[kind] = stage
    cutoff = (now - timedelta(days=_caps.COUNTER_DAYS_KEPT)).date().isoformat()
    counts = {d: v for d, v in counts.items() if d >= cutoff}
    from kiro_crew.atomic_write import atomic_write

    if not path.parent.exists():
        mkdirs_owner_only(path.parent)
    atomic_write(
        path,
        json.dumps({"counts": counts, "stages": stages}, sort_keys=True),
        mode=0o600,
    )
    return capped


def write_line(line: dict[str, Any]) -> None:
    """Append *line* to the shard for its local day (blocking I/O).

    Never raises: the receipt is an observation of a turn, and failing to write it
    must not fail the turn. A stage field past its caps is left off the line.
    """
    try:
        stamp = datetime.fromisoformat(str(line["ts"]).replace("Z", "+00:00"))
        path = _receipt_dir() / f"{stamp.astimezone().strftime('%Y-%m-%d')}.jsonl"
        with _STAGE_LOG_LOCK:
            try:
                line = _cap_stage_fields(line, stamp.astimezone(timezone.utc))
            except Exception:  # noqa: BLE001 -- an unbounded field is never written
                logger.warning("injection receipt: stage log unreadable", exc_info=True)
                line = {k: v for k, v in line.items() if k not in STAGE_KINDS}
        if not path.parent.exists():
            mkdirs_owner_only(path.parent)
        text = json.dumps(line, ensure_ascii=False, separators=(",", ":"))
        with open(path, "a", encoding="utf-8", opener=owner_only_opener_for(path)) as fh:
            fh.write(text + "\n")
    except Exception:  # noqa: BLE001 -- an observation must never fail the turn
        logger.warning("injection receipt: could not append the line", exc_info=True)


def schedule_write(line: dict[str, Any] | None) -> None:
    """Hand *line* to a worker thread and return at once.

    No suspension point: the dispatch gate that calls this sits between two reads
    that must stay atomic. Outside a running loop the line is written inline.
    """
    if line is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        write_line(line)
        return
    loop.run_in_executor(None, write_line, line)
