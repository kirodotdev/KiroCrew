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
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from kiro_crew.config.paths import data_home
from kiro_crew.owner_only_files import owner_only_opener_for

logger = logging.getLogger(__name__)

LESSONS: Final = "lessons"
FINDINGS: Final = "findings"
#: The per-message lessons ``memory.inject_lessons_per_turn`` adds to a follow-up.
TURN_LESSONS: Final = "turn_lessons"

#: The blocks a receipt names. A record is keyed by these names only.
KINDS: Final[tuple[str, ...]] = (LESSONS, FINDINGS, TURN_LESSONS)

#: The budgeted tiers, whose line also states how many rows the budget cut.
_COUNTED_KINDS: Final[tuple[str, ...]] = (LESSONS, FINDINGS)

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
        if kind in KINDS and isinstance(entry, dict):
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
    if kind not in KINDS:
        raise ValueError(f"unknown receipt kind {kind!r}")
    sink = _SINK.get()
    if sink is None:
        return
    keys = [key for key in (row_key(row) for row in rows) if key]
    sink[kind] = {"ids": keys, "omitted": max(0, int(omitted))}


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
    """The receipt line for one composition, or ``None`` when no lesson tier rendered.

    A turn whose composition rendered no lesson block (a per-turn build after the
    session start, with no per-message lessons) writes no line: the lessons in
    front of the model are still the ones the session's earlier lines recorded.
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
    for kind in KINDS:
        entry = receipts.get(kind) or {}
        line[kind] = list(entry.get("ids") or [])
        if kind in _COUNTED_KINDS:
            line[f"{kind}_omitted"] = int(entry.get("omitted") or 0)
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


def write_line(line: dict[str, Any]) -> None:
    """Append *line* to the shard for its local day (blocking I/O).

    Never raises: the receipt is an observation of a turn, and failing to write it
    must not fail the turn.
    """
    try:
        stamp = datetime.fromisoformat(str(line["ts"]).replace("Z", "+00:00"))
        path = _receipt_dir() / f"{stamp.astimezone().strftime('%Y-%m-%d')}.jsonl"
        if not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
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
