"""Human answers to needs-human passes, kept in the app's own ``crew-answer-checks.json``.

When a person (not a crew) removes the needs-human label after a pass, the gateway's
sweep records that answer here, in the ``answers`` section, beside the sweep's own
per-issue check marks (``items``). The crew log is not involved: Issue Radar is
moving out of core, so its answer state stays in a file the app owns and the core
crew-log schema and the ``radar`` fold are unchanged.

An answer hides an answerable pass (``crew_store_ledger.read_skips``) that it is
newer than. A crew recording a new pass on that number after the answer marks the
answer ``superseded_at``: the issue is "still undecided, skipped again", so the
number is indexed again, and the same label removal read on a later sweep is not
newer than the stored answer, so it cannot hide the number a second time.

One lock serialises every read-modify-write of the file in this process, so the
sweep's check marks and the answers never overwrite each other.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write, read_json_or
from kiro_crew.crew_log.projection_radar import RADAR_TEXT_LIMIT

from . import store
from .crew_store_records import ANSWERABLE_SKIP_SCOPES

logger = logging.getLogger(__name__)

SCHEMA = 1
#: Answers kept in all, one per number (the newest), standing and superseded alike.
#: Past the bound the OLDEST evictable answer goes, and ``answer_floor`` rises to its
#: time. :func:`record` refuses any answer at or below the floor, so an evicted row's
#: label removal read again can never be recorded a second time: a superseded
#: answer evicted keeps its removal consumed, and a standing one evicted leaves its
#: pass indexed again -- the safe direction, a re-investigation rather than a wrong
#: pick-up. One string replaces every evicted row, so the file stays bounded.
#:
#: A superseded answer whose new pass has a scope a human cannot answer (a scope
#: gate, a duplicate) is PINNED, never evicted: that pass is kept only here (the crew
#: log's fold holds the original pass), so evicting it would let a later label
#: removal hand back an issue a crew had closed to answers. Pinned rows are made only
#: from rows already counted, so the total never passes the bound; once every row is
#: pinned, a new answer is evicted as it is written, and counted.
ANSWERS_LIMIT = 500
_LOCK = threading.Lock()
#: Held across a re-skip of an answered number -- the answer's observation, its
#: supersede mark, the crew-log append and any rollback -- so two crews re-skipping
#: the same answer cannot interleave (``crew_store_ledger.commit_work_progress``).
RESKIP_LOCK = threading.Lock()


def path(owner: str, repo: str, root: Path | None = None) -> Path:
    """Beside ``crew-signals.json`` and for the same reason: not inside ``crews/``."""
    return store.repo_data_dir(owner, repo, root) / "crew-answer-checks.json"


class AnswerFileUnreadable(OSError):
    """The file exists but could not be read as this schema: nothing is written."""


def _read(owner: str, repo: str, root: Path | None, *, for_write: bool = False) -> dict[str, Any]:
    file = path(owner, repo, root)
    exists = file.is_file()
    doc = read_json_or(file, None, logger=logger, what="crew answer checks") if exists else None
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        if exists and for_write:
            # A transient read failure (or a file from another schema) must not be
            # written over: that would erase every answer and check mark it holds.
            raise AnswerFileUnreadable(f"{file} could not be read; not written")
        doc = {}
    for section in ("items", "answers"):
        if not isinstance(doc.get(section), dict):
            doc[section] = {}
    if not isinstance(doc.get("evicted_answers"), int):
        doc["evicted_answers"] = 0
    if not isinstance(doc.get("answer_floor"), str):
        doc["answer_floor"] = ""
    doc["schema"] = SCHEMA
    return doc


def read(owner: str, repo: str, root: Path | None = None) -> dict[str, Any]:
    """The whole file, every section present."""
    with _LOCK:
        return _read(owner, repo, root)


def update(
    owner: str, repo: str, root: Path | None, change: Callable[[dict[str, Any]], Any]
) -> Any:
    """Apply *change* to the file under the lock and write it back; return its result."""
    with _LOCK:
        doc = _read(owner, repo, root, for_write=True)
        result = change(doc)
        atomic_write(path(owner, repo, root), json.dumps(doc, indent=2, sort_keys=True))
        return result


def answers(owner: str, repo: str, root: Path | None = None) -> dict[str, dict[str, Any]]:
    """The newest recorded answer per number, keyed by ``str(number)``."""
    rows = read(owner, repo, root)["answers"]
    return {str(k): dict(v) for k, v in rows.items() if isinstance(v, dict)}


def evicted(owner: str, repo: str, root: Path | None = None) -> int:
    return int(read(owner, repo, root)["evicted_answers"])


def record(
    owner: str, repo: str, root: Path | None, number: int, actor: str, at: str, crew_id: str
) -> bool:
    """Keep *at* as the answer on *number* unless an equal or newer one is held."""

    def change(doc: dict[str, Any]) -> bool:
        rows: dict[str, Any] = doc["answers"]
        key = str(int(number))
        standing = rows.get(key)
        if isinstance(standing, dict) and str(standing.get("at") or "") >= at:
            return False
        if at <= str(doc["answer_floor"]):
            # Not newer than an answer already evicted: it would be evicted as it is
            # written, and an evicted row's own removal must stay consumed.
            doc["evicted_answers"] = int(doc["evicted_answers"]) + 1
            return False
        rows[key] = {
            "number": int(number),
            "actor": actor,
            "at": at,
            "crew_id": crew_id,
            "recorded_at": _now(),
        }
        _bound(doc, rows)
        return True

    return bool(update(owner, repo, root, change))


def _bound(doc: dict[str, Any], rows: dict[str, Any]) -> None:
    """Evict answers past :data:`ANSWERS_LIMIT`, oldest first, superseded rows
    counted too and pinned ones kept, raising ``answer_floor`` to each evicted time
    (see :data:`ANSWERS_LIMIT`); every eviction is counted in ``evicted_answers``.
    The one just written can go too, which the sweep reads as an answer evicted as it
    was written."""
    group = sorted((str((rows[k] or {}).get("at") or ""), k) for k in rows if not _pinned(rows[k]))
    for at, k in group[: max(0, len(rows) - ANSWERS_LIMIT)]:
        del rows[k]
        doc["answer_floor"] = max(str(doc["answer_floor"]), at)
        doc["evicted_answers"] = int(doc["evicted_answers"]) + 1


def _pinned(row: Any) -> bool:
    """A superseded answer whose new pass a human cannot answer: never evicted."""
    if not isinstance(row, dict) or not row.get("superseded_at"):
        return False
    fresh = row.get("pass")
    return not isinstance(fresh, dict) or fresh.get("scope") not in ANSWERABLE_SKIP_SCOPES


def supersede(
    owner: str, repo: str, root: Path | None, number: int, at: str, new_pass: dict[str, Any]
) -> bool:
    """A crew is recording *new_pass* (``crew_id``, ``reason``, ``scope``,
    ``decided_at``) on *number* after the answer at *at*: mark the answer, and keep
    the pass the index shows. The reason is clamped to the fold's own text bound.
    Returns whether THIS call set the mark, so only it may roll the mark back."""

    def change(doc: dict[str, Any]) -> bool:
        row = doc["answers"].get(str(int(number)))
        if not isinstance(row, dict) or str(row.get("at") or "") != at:
            return False
        if not row.get("superseded_at"):
            row["superseded_at"] = _now()
            fresh = dict(new_pass)
            fresh["reason"] = str(fresh.get("reason") or "")[:RADAR_TEXT_LIMIT]
            fresh.setdefault("decided_at", row["superseded_at"])
            row["pass"] = fresh
            return True
        return False

    return bool(update(owner, repo, root, change))


def unsupersede(owner: str, repo: str, root: Path | None, number: int, at: str) -> None:
    """Undo :func:`supersede` for the answer at *at*: its pass was refused by the log."""

    def change(doc: dict[str, Any]) -> None:
        row = doc["answers"].get(str(int(number)))
        if isinstance(row, dict) and str(row.get("at") or "") == at:
            row.pop("superseded_at", None)
            row.pop("pass", None)
            _bound(doc, doc["answers"])

    update(owner, repo, root, change)


def now_stamp() -> str:
    """Now, in the crew log's own UTC spelling."""
    return _now()


def _now() -> str:
    """The crew log's own UTC spelling, so it compares with a pass's ``decided_at``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
