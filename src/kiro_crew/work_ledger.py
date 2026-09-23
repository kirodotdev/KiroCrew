"""The shared work record between a conductor session and the workers it dispatched.

Three ledgers carry that name, and they are not interchangeable.
:mod:`kiro_crew.session_ledger` is ONE session's own durable state. Issue Radar's
``crew_store`` is a per-repository work ledger keyed by a forge issue number. This
module is the third: a record two parties write and neither owns, so that a
conductor learns what a worker did as DATA instead of reading its transcript.

This module is the STORAGE layer only. Its one importer is
``dashboard/handlers/work_ledger.py``, which serves the ``/api/work-ledger``
routes; the MCP tools in :mod:`kiro_crew.mcp_work` (``work_brief``,
``work_report``, ``work_ledger_read``, ``work_ledger_record``) reach it only
through those routes. Every write therefore passes the two entry points below,
so the writer-ownership rule is enforced in one place.

WRITER OWNERSHIP is the whole design, and it is expressed as two entry points rather
than one update function with a field allowlist:

  * :func:`apply_conductor_action` writes ``title``, ``acceptance``, ``state``,
    ``verdict``, ``decision``, ``worker_session_key``, ``round`` and ``fails``.
  * :func:`apply_worker_report` writes ``status``, ``summary``, ``artifacts``,
    ``pr`` and ``last_report_at``.

The two field sets are disjoint. Phase 2 mounts one tool on each, so a worker cannot
reach a conductor field because the function it can call takes no parameter that
names one — an absent parameter outlives an allowlist that must be kept correct as
fields are added. Phase 1 performs NO identity resolution; that is Phase 2's job at
the tool layer, and the split shape here is what lets it be done by construction.

LOCK ORDER, for the two paths that hold more than one lock. ``create`` enforces the
per-conductor item cap, which means reading the items directory, so it holds the
conductor lock across the whole transaction and takes the new item's lock from
inside that hold. ``bind`` holds the item lock and takes the worker's binding lock
from inside it, because "this worker holds no other open item" is a property of
the binding file, not of the item. So: **conductor -> item -> binding(worker)**.
No path anywhere takes any two of these in the other relative order, so the order
is total and two conductors cannot deadlock. Every other write takes exactly one
lock: the conductor lock for ``goal``, the item lock for ``decide``/``verdict``/
``close`` and for a worker report. File locks on fresh descriptors do NOT nest, so
each locked body has a ``_locked`` twin that a holder calls directly rather than
re-acquiring.

CAPS REFUSE, THEY DO NOT TRUNCATE. Every bound on a STORED field is validated
before the first write, so a refusal leaves every file byte-identical. A truncated
summary the worker believes landed whole is a silent loss the worker cannot detect.
The one bounded projection is the event line's ``text``: it is a 500-char excerpt of
a field the item record already holds in full, so clipping it loses nothing.

CORRUPTION READS AS ABSENT. A torn, truncated, non-UTF-8 or oversized record returns
``None`` rather than raising, matching :mod:`kiro_crew.session_ledger`'s treatment of
an oversized state file. A reader of a two-writer store must not be the thing that
crashes because the other writer was interrupted.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import secrets
import shutil
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from kiro_crew.atomic_write import atomic_write, read_bytes_with_retry
from kiro_crew.config.paths import data_home
from kiro_crew.platform_compat import file_lock, release_lock, try_acquire_lock
from kiro_crew.session_ledger import (
    _store_name,
    is_link,
    require_lock_inode,
    resolved_within,
    unlink_lock_in_hold,
)
from kiro_crew.work_vocab import (
    WORK_ITEM_STATES,
    WORK_VERDICTS,
    WORK_WORKER_STATUSES,
    work_event_id,
)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# --------------------------------------------------------------------------- #
# Vocabularies
# --------------------------------------------------------------------------- #

#: A work item's disposition, written by the conductor. A DIFFERENT question from
#: ``verdict``: an item may hold ``verdict: fail`` and stay ``open`` while the
#: worker retries, which is the state ``ledger_entry.py`` encodes today as "fails
#: incremented but still running".
ITEM_STATES: frozenset[str] = frozenset(WORK_ITEM_STATES)

#: States from which no further write is accepted.
TERMINAL_ITEM_STATES: frozenset[str] = frozenset({"accepted", "rejected", "abandoned"})

#: ``accept_eval.py``'s own five values, reused rather than paralleled, so a verdict
#: crosses from that script into this store with no translation.
VERDICTS: frozenset[str] = frozenset(WORK_VERDICTS)

#: What a worker may say about itself. ``blocked`` and ``question`` are separate
#: because they differ in WHO must act: an external dependency versus the conductor.
WORKER_STATUSES: frozenset[str] = frozenset(WORK_WORKER_STATUSES)

#: The statuses from which a report gap still means "the WORKER went quiet", which is
#: the only thing :func:`is_stale` exists to surface. ``done`` is deliberately absent:
#: a worker that has claimed its bar is met has nothing left to report, and the next
#: move belongs to the conductor (verify, promote, close) or to a human. Flagging it
#: would point the flag at the reader instead of the worker.
STALE_ELIGIBLE_STATUSES: frozenset[str] = frozenset({"progress", "blocked", "question"})

#: Field values that mean "not filled in yet". A conductor legitimately opens an item
#: whose bar is not knowable at dispatch time -- the worker is what learns the pull
#: request number -- and writes ``"TBD"`` in the field meanwhile, so the placeholder is
#: part of the shape rather than a typo. See :func:`is_acceptance_concrete`.
ACCEPTANCE_PLACEHOLDERS: frozenset[str] = frozenset({"tbd", ""})

#: Per ``kind``, the fields ``accept_eval.py`` actually READS. This is the whole
#: vocabulary :func:`is_acceptance_concrete` judges a bar by: a placeholder in a field
#: that script consumes is what makes a bar unevaluable, and a placeholder anywhere
#: else is in a field it never looks at, so it is none of this predicate's business.
#: ``cmd`` reads nothing because that script always REFUSES it, and ``refused`` is a
#: message the conductor must receive -- re-express the condition, never route around
#: it -- so dropping such an item from the batch would delete its only delivery.
#: A test derives this map's keys from the script's own dispatch and fails on drift.
ACCEPTANCE_READ_FIELDS: dict[str, tuple[str, ...]] = {
    "pr_checks": ("pr", "repo"),
    "file": ("path", "exists"),
    "human_approval": (),
    "cmd": (),
}

#: The ``kind`` values that script dispatches on at all. Anything else is an error-only
#: spec (its closing ``return ("error", f"unknown accept kind ...")``), so it is not
#: concrete. Derived from the map above rather than spelled twice.
ACCEPTANCE_KINDS: frozenset[str] = frozenset(ACCEPTANCE_READ_FIELDS)

#: Every ITEM write appends exactly one event, so there is no way to move an item
#: field without a line explaining it (the conductor's own ``goal``/``round``
#: header is the one eventless write, because it belongs to no item). ``report``
#: is the only kind a worker can produce.
EVENT_KINDS: frozenset[str] = frozenset(
    {"create", "bind", "report", "decision", "verdict", "close"}
)

#: The conductor's disjoint operations. One action per call, because the field sets
#: do not overlap and a single flat schema would accept nonsense combinations.
CONDUCTOR_ACTIONS: frozenset[str] = frozenset(
    {"create", "bind", "decide", "verdict", "close", "goal"}
)

# --------------------------------------------------------------------------- #
# Caps. Each one refuses; none truncates.
# --------------------------------------------------------------------------- #

MAX_ITEMS_PER_CONDUCTOR = 32
MAX_EVENTS_PER_ITEM = 200
MAX_DEPTH = 2

MAX_GOAL_CHARS = 2000
MAX_TITLE_CHARS = 200
MAX_DECISION_CHARS = 2000
MAX_SUMMARY_CHARS = 500
MAX_EVENT_TEXT_CHARS = 500

MAX_ARTIFACT_KEYS = 16
MAX_ARTIFACT_KEY_CHARS = 64
MAX_ARTIFACT_VALUE_CHARS = 512

MIN_PR = 1
MAX_PR = 1_000_000_000

#: A record over this size reads as absent. The writers cannot approach it — every
#: field is capped far below — so crossing it means the file was torn, hand-edited,
#: or written by something that is not this module.
MAX_RECORD_BYTES = 1_000_000

#: How long an item may go unreported before :func:`is_stale` will consider it, once
#: its worker is also confirmed not running. A default, not a policy: the caller
#: passes its own window.
DEFAULT_STALE_WINDOW_SECS = 900.0

# --------------------------------------------------------------------------- #
# Error codes. Named for the HTTP-facing vocabulary Phase 2 maps them onto, so the
# mapping is a lookup rather than a re-classification.
# --------------------------------------------------------------------------- #

CODE_NO_LEDGER = "no_ledger"
CODE_UNKNOWN_ITEM = "unknown_item"
CODE_ALREADY_BOUND = "already_bound"
CODE_ITEM_CLOSED = "item_closed"
CODE_ITEM_CAP_EXCEEDED = "item_cap_exceeded"
CODE_CREW_LOG_INCOMPLETE = "crew_log_incomplete"
CODE_CACHE_DIRTY = "cache_dirty"
CODE_DEPTH_EXCEEDED = "depth_exceeded"
CODE_FIELD_TOO_LONG = "field_too_long"
CODE_INVALID_ACTION = "invalid_action"
CODE_INVALID_STATUS = "invalid_status"
CODE_INVALID_VALUE = "invalid_value"
CODE_LEDGER_NOT_FINISHED = "ledger_not_finished"


class WorkLedgerError(Exception):
    """A store invariant was violated, or a cap refused a write.

    Carries ``code`` — one of the ``CODE_*`` constants — and, for a bound that
    refused, the ``field`` whose cap it was. Phase 2 maps ``code`` onto a status and
    quotes ``field`` back, so the caller learns WHICH bound it crossed rather than
    that something was too long.
    """

    def __init__(self, message: str, *, code: str, field: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.field = field


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #


@dataclass
class ConductorRecord:
    """One conductor session's ledger header. The conductor is the sole writer.

    There is deliberately NO item roster field. The item list is derived by listing
    the items directory (:func:`list_work_items`), which removes a writer and with
    it a class of clobber — the same choice Issue Radar's ``list_work_items`` makes.
    """

    slot_key: str = ""
    goal: str = ""
    round: int = 0
    depth: int = 0
    parent_item: str | None = None
    created_at: str = ""
    schema: int = SCHEMA_VERSION
    #: An opaque id minted when the record is created. A slot reused after its board
    #: was purged mints a new one, which is how the crew-log fold tells the two
    #: boards apart without comparing timestamps. Empty on records from before it.
    generation: str = ""
    #: Counts the header's goal writes. A goal entry carries it and the fold keeps
    #: the highest it saw, so a rebuild can tell a header the record holds whole
    #: from one the cache wrote after the record's last goal entry. Zero on
    #: records from before it.
    goal_version: int = 0
    #: Stamped once a goal entry has landed in the crew log (the header's
    #: counterpart of an item's ``recorded_at``): from then on the record is
    #: expected to hold every goal write, and a rebuild checks that it does.
    recorded_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "slot_key": self.slot_key,
            "goal": self.goal,
            "round": self.round,
            "depth": self.depth,
            "parent_item": self.parent_item,
            "created_at": self.created_at,
            "generation": self.generation,
            "goal_version": self.goal_version,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> ConductorRecord:
        """Coerce stored JSON into a record. NEVER raises.

        A wrong-typed field resets to its default rather than failing the read: this
        file is written by one party and read by a probe, a page and the conductor
        itself, and a single bad field must not take all three down.
        """
        if not isinstance(raw, dict):
            return cls()
        return cls(
            slot_key=_as_str(raw.get("slot_key")),
            goal=_as_str(raw.get("goal")),
            round=_as_int(raw.get("round"), 0),
            depth=_as_int(raw.get("depth"), 0),
            parent_item=_as_opt_str(raw.get("parent_item")),
            created_at=_as_str(raw.get("created_at")),
            schema=_as_int(raw.get("schema"), SCHEMA_VERSION),
            generation=_as_str(raw.get("generation")),
            goal_version=_as_int(raw.get("goal_version"), 0),
            recorded_at=_as_str(raw.get("recorded_at")),
        )


@dataclass
class WorkItem:
    """One dispatched work item. Two writers, one per-item lock, disjoint fields.

    Conductor-owned: ``title``, ``acceptance``, ``state``, ``verdict``, ``decision``,
    ``worker_session_key``, ``round``, ``fails``.
    Worker-owned: ``status``, ``summary``, ``artifacts``, ``pr``, ``last_report_at``.
    Server-owned: ``item_id``, ``created_at``, ``closed_at``.

    ``orphaned`` and ``stale`` are NOT fields. They are derived at read time by
    :func:`is_orphaned` and :func:`is_stale`, because a stamped flag would need
    something running at close time to stamp it, a missed stamp would stay wrong
    forever, and a derived flag self-heals when a session is reopened.
    """

    item_id: str = ""
    title: str = ""
    acceptance: dict[str, Any] = field(default_factory=dict)
    state: str = "open"
    verdict: str | None = None
    decision: str = ""
    worker_session_key: str | None = None
    round: int = 0
    fails: int = 0
    status: str | None = None
    summary: str = ""
    artifacts: dict[str, str] = field(default_factory=dict)
    pr: int | None = None
    last_report_at: str | None = None
    created_at: str = ""
    closed_at: str | None = None
    schema: int = SCHEMA_VERSION
    #: When the crew log first held this item whole (its recorded create, or the
    #: baseline its first recorded mutation carried). Empty until then: the write
    #: routes carry the whole item on the next entry, so a lost file rebuilds.
    recorded_at: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_ITEM_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "item_id": self.item_id,
            "title": self.title,
            "acceptance": self.acceptance,
            "state": self.state,
            "verdict": self.verdict,
            "decision": self.decision,
            "worker_session_key": self.worker_session_key,
            "round": self.round,
            "fails": self.fails,
            "status": self.status,
            "summary": self.summary,
            "artifacts": self.artifacts,
            "pr": self.pr,
            "last_report_at": self.last_report_at,
            "created_at": self.created_at,
            "closed_at": self.closed_at,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> WorkItem:
        """Coerce stored JSON into an item. NEVER raises. See
        :meth:`ConductorRecord.from_dict` for why a bad field resets rather than
        failing the read."""
        if not isinstance(raw, dict):
            return cls()
        state = _as_str(raw.get("state"))
        status = _as_opt_str(raw.get("status"))
        verdict = _as_opt_str(raw.get("verdict"))
        artifacts_raw = raw.get("artifacts")
        artifacts: dict[str, str] = {}
        if isinstance(artifacts_raw, dict):
            artifacts = {str(k): v for k, v in artifacts_raw.items() if isinstance(v, str)}
        return cls(
            item_id=_as_str(raw.get("item_id")),
            title=_as_str(raw.get("title")),
            acceptance=raw["acceptance"] if isinstance(raw.get("acceptance"), dict) else {},
            state=state if state in ITEM_STATES else "open",
            verdict=verdict if verdict in VERDICTS else None,
            decision=_as_str(raw.get("decision")),
            worker_session_key=_as_opt_str(raw.get("worker_session_key")),
            round=_as_int(raw.get("round"), 0),
            fails=_as_int(raw.get("fails"), 0),
            status=status if status in WORKER_STATUSES else None,
            summary=_as_str(raw.get("summary")),
            artifacts=artifacts,
            pr=_finite_int(raw.get("pr")),
            last_report_at=_as_opt_str(raw.get("last_report_at")),
            created_at=_as_str(raw.get("created_at")),
            closed_at=_as_opt_str(raw.get("closed_at")),
            recorded_at=_as_str(raw.get("recorded_at")),
            schema=_as_int(raw.get("schema"), SCHEMA_VERSION),
        )


@dataclass
class WorkEvent:
    """One line in an item's event log.

    ``id`` is content-addressed, so a line written twice — a retry, a restored
    backup, a rollback that replayed — collapses on read instead of double-counting.
    """

    id: str = ""
    ts: str = ""
    item_id: str = ""
    kind: str = ""
    status: str | None = None
    text: str = ""

    @property
    def is_progress_report(self) -> bool:
        """Whether this is the one event kind that coalesces. See
        :func:`_coalesce_progress`."""
        return self.kind == "report" and self.status == "progress"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "item_id": self.item_id,
            "kind": self.kind,
            "status": self.status,
            "text": self.text,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> WorkEvent | None:
        """Parse one stored line, or ``None`` when it is not one.

        ``None`` rather than a defaulted record: an event log is append-only and a
        torn tail must be SKIPPED, not folded in as an event with empty fields that
        a reader would then have to distinguish from a real one.
        """
        if not isinstance(raw, dict):
            return None
        kind = _as_str(raw.get("kind"))
        if kind not in EVENT_KINDS:
            return None
        status = _as_opt_str(raw.get("status"))
        return cls(
            id=_as_str(raw.get("id")),
            ts=_as_str(raw.get("ts")),
            item_id=_as_str(raw.get("item_id")),
            kind=kind,
            status=status if status in WORKER_STATUSES else None,
            text=_as_str(raw.get("text")),
        )


# --------------------------------------------------------------------------- #
# Coercion helpers. None of these raise.
# --------------------------------------------------------------------------- #


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _as_opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _as_int(value: Any, default: int) -> int:
    parsed = _finite_int(value)
    return default if parsed is None else parsed


def _finite_int(value: Any) -> int | None:
    """*value* as an int, or ``None`` when it is not one.

    ``bool`` is rejected before ``int`` because ``True`` is an ``int`` in Python, so
    a cap written as ``value <= MAX`` is silently defeated by a boolean. A FRACTIONAL
    float is rejected too rather than truncated: ``int()`` would store a number the
    caller never asked for while reporting success.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        if not value.is_integer():
            return None
    return int(value)


def _now_iso() -> str:
    """Local time with offset, seconds precision — the same stamp
    :mod:`kiro_crew.session_ledger` writes, so two ledgers read side by side sort
    against each other."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

#: Directory naming is :func:`kiro_crew.session_ledger._store_name` -- readable
#: fold plus ``sha256[:8]`` -- imported rather than copied. ``session_ledger``
#: imports only the three modules this one already imports, so there is no
#: import-graph reason to keep a private copy of the fold here.

#: The only shape an item id may have — ``it_`` plus the eight hex chars
#: :func:`mint_item_id` produces.
_ITEM_ID_RE = re.compile(r"^it_[0-9a-f]{8}$")

_CONDUCTOR_FILE = "conductor.json"
_KEY_FILE = "slot_key"
#: Present while the cache may hold what the record does not (an undo failed).
#: Public under ``DIRTY_FILE`` because the ``cache_dirty`` refusal names it: an
#: operator who decides the cache is right removes this marker to serve it again.
_DIRTY_FILE = "cache_dirty"
DIRTY_FILE = _DIRTY_FILE
_LOCK_FILE = ".lock"
_ITEMS_DIR = "items"
_BINDINGS_DIR = "bindings"


def mint_item_id() -> str:
    """A fresh server-side item id.

    Minted here and never accepted from a model, which is what keeps a model-supplied
    string out of a path component. ``secrets`` rather than ``random`` because an id
    a caller can predict is an id it can name in a request before the item exists.
    """
    return f"it_{secrets.token_hex(4)}"


def _require_item_id(item_id: str) -> str:
    """Gate an item id before it can reach a filesystem path.

    ``Path("/store") / item_id`` DISCARDS the base when *item_id* is absolute and
    honours ``..`` when it is relative, so an unchecked id is an arbitrary-file read
    on any read path and an arbitrary-file WRITE on any write path. The check lives
    at this single choke point every path constructor passes through rather than at
    each caller, because a caller-level check protects only the callers someone
    remembered.
    """
    if not _ITEM_ID_RE.match(item_id or ""):
        raise WorkLedgerError(f"invalid item id {item_id!r}", code=CODE_INVALID_VALUE)
    return item_id


def _work_ledger_root() -> Path:
    """Resolved per call, never cached at import: ``data_home()`` is overridable and
    a module-level constant would freeze the first value a test happened to set."""
    return data_home() / "work-ledger"


def _slot_key_is_shaped(slot_key: str) -> bool:
    """Whether *slot_key* has the shape a store path may be built from.

    A slot key names a directory, so a null byte or a path separator would let it
    escape its own directory. This is the shape check :func:`conductor_dir` raises
    on; it is a named predicate so a reader that must decide UNBOUND-vs-RAISE on a
    stored key can ask the same question without provoking the raise.
    """
    return bool(slot_key) and "\0" not in slot_key and "/" not in slot_key and "\\" not in slot_key


def conductor_dir(slot_key: str) -> Path:
    """The directory holding *slot_key*'s ledger. Does not create it."""
    if not _slot_key_is_shaped(slot_key):
        raise WorkLedgerError(
            f"invalid slot key for work ledger: {slot_key!r}", code=CODE_INVALID_VALUE
        )
    resolved = resolved_within(_work_ledger_root(), _store_name(slot_key))
    if resolved is None:
        raise WorkLedgerError(
            f"path traversal blocked for slot key: {slot_key!r}", code=CODE_INVALID_VALUE
        )
    return resolved


def items_dir(slot_key: str) -> Path:
    return conductor_dir(slot_key) / _ITEMS_DIR


def item_path(slot_key: str, item_id: str) -> Path:
    return items_dir(slot_key) / f"{_require_item_id(item_id)}.json"


def item_events_path(slot_key: str, item_id: str) -> Path:
    return items_dir(slot_key) / f"{_require_item_id(item_id)}.jsonl"


def _item_lock_path(slot_key: str, item_id: str) -> Path:
    return items_dir(slot_key) / f"{_require_item_id(item_id)}.lock"


def bindings_dir() -> Path:
    return _work_ledger_root() / _BINDINGS_DIR


def binding_path(worker_slot_key: str) -> Path:
    """Where a worker's binding lives, keyed by the worker's own session key.

    Named with the SAME readable-plus-digest fold as a conductor directory rather
    than the digest alone: the fold is strictly more collision-resistant (a
    collision needs both the same sanitised prefix and the same digest), and it
    keeps one naming scheme across the store instead of two.
    """
    if not worker_slot_key or "\0" in worker_slot_key:
        raise WorkLedgerError(
            f"invalid worker slot key: {worker_slot_key!r}", code=CODE_INVALID_VALUE
        )
    resolved = resolved_within(bindings_dir(), f"{_store_name(worker_slot_key)}.json")
    if resolved is None:
        raise WorkLedgerError(
            f"path traversal blocked for worker key: {worker_slot_key!r}",
            code=CODE_INVALID_VALUE,
        )
    return resolved


# --------------------------------------------------------------------------- #
# Locks
# --------------------------------------------------------------------------- #


@contextmanager
def _open_lock(path: Path, *, create: bool = True) -> Iterator[None]:
    """Hold an advisory lock on *path*, creating the lock file if absent.

    ``file_lock`` takes an already-open descriptor and fails CLOSED — it raises
    rather than entering the critical section unserialised — which is why nothing
    here has a lock-less fallback.

    The lock file is created with ``touch`` and opened ``"r+"`` — WRITABLE, and
    crucially WITHOUT truncation. ``msvcrt.locking`` needs a writable handle, so
    ``"r"`` is not an option; but ``"w"`` TRUNCATES on open, and on Windows a
    truncating open of a file whose first byte another thread or process already
    holds under ``msvcrt.locking`` raises a sharing violation (``PermissionError``)
    rather than waiting for the lock — so a second, contending acquirer crashes
    before it ever reaches ``file_lock``, defeating the serialisation this lock
    exists to provide. POSIX ``flock`` tolerates the truncate, which is why the bug
    is Windows-only. Same reasoning, same fix as ``dashboard/handlers/mcp.py``'s
    ``_McpFileLock``.
    """
    # ``create=False`` is the purge's form: a deleter must not bring a store
    # into being by locking it, or a second sweep racing the first recreates the
    # directory the first just removed. The open then raises ``FileNotFoundError``
    # for a store that is gone, and the caller skips it.
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
    with open(path, "r+") as handle:
        with file_lock(handle.fileno(), exclusive=True):
            # A purge may have removed this store while the acquire waited. The
            # kernel still grants the lock on the detached inode, so without this
            # the queued writer would publish a torn store into a purged key. See
            # ``session_ledger.require_lock_inode``; the raise is the same
            # ``OSError`` a held lock produces, and the caller retries.
            require_lock_inode(handle.fileno(), path)
            yield


@contextmanager
def conductor_lock(slot_key: str, *, create: bool = True) -> Iterator[None]:
    """Hold the conductor lock. FIRST in the lock order; see the module docstring.

    ``create=False`` refuses with ``FileNotFoundError`` instead of creating the
    store; it is the form a deleter takes.
    """
    with _open_lock(conductor_dir(slot_key) / _LOCK_FILE, create=create):
        yield


@contextmanager
def item_lock(slot_key: str, item_id: str, *, create: bool = True) -> Iterator[None]:
    """Hold one item's lock. SECOND in the lock order; see the module docstring.

    ``create=False`` is the form every writer to an EXISTING item takes. A lock
    that creates its file also ``mkdir``s the store around it, so a late report
    against a purged ledger -- a worker whose binding outlived the conductor --
    would rebuild ``<store>/items/<id>.lock`` in a directory the sweep had
    removed, and a lock-only store with no header and no items is one the sweep
    keeps forever. With ``create=False`` a missing lock file is read as the
    missing item it is: ``CODE_UNKNOWN_ITEM``, and nothing is written. The one
    exception is a store that still HOLDS the item record but lost its lock file
    (hand-removed); the store exists, so the lock is recreated for it -- UNDER
    THE CONDUCTOR LOCK, taken non-creating, with the record re-checked inside
    the hold. The purge holds that same lock through its removal, so the store
    cannot vanish between the re-check and the ``touch``; a plain creating open
    here could lose that race and rebuild the lock-only store this parameter
    exists to prevent. Only ``_create_item`` creates, and it does so under the
    conductor lock with the header verified present.
    """
    path = _item_lock_path(slot_key, item_id)
    if create:
        with _open_lock(path):
            yield
        return
    try:
        lock_cm = _open_lock(path, create=False)
        lock_cm.__enter__()
    except FileNotFoundError:
        if not item_path(slot_key, item_id).exists():
            # The common case -- the item, or its whole store, is gone -- is
            # answered without a lock. The locked recreate below re-checks.
            raise WorkLedgerError(
                f"unknown item {item_id!r}", code=CODE_UNKNOWN_ITEM, field="item_id"
            ) from None
        _recreate_item_lock_file(slot_key, item_id, path)
        try:
            lock_cm = _open_lock(path, create=False)
            lock_cm.__enter__()
        except FileNotFoundError:
            # Purged between the recreate and this acquire. Nothing was created
            # in the gap -- both opens are non-creating -- so refuse as missing.
            raise WorkLedgerError(
                f"unknown item {item_id!r}", code=CODE_UNKNOWN_ITEM, field="item_id"
            ) from None
    try:
        yield
    finally:
        lock_cm.__exit__(None, None, None)


def _recreate_item_lock_file(slot_key: str, item_id: str, lock_path: Path) -> None:
    """Restore a hand-removed ``items/<id>.lock`` for an item whose record exists.

    Serialised against the purge by the conductor lock (non-creating: a missing
    conductor lock file is a missing ledger), and the record is re-read INSIDE
    that hold -- ``purge_conductor`` removes records and lock files under the same
    lock, so a record that is present here stays present until this returns. An
    absent record is ``CODE_UNKNOWN_ITEM`` and nothing is touched.
    """
    with _existing_conductor_lock(slot_key):
        if not item_path(slot_key, item_id).exists():
            raise WorkLedgerError(
                f"unknown item {item_id!r}", code=CODE_UNKNOWN_ITEM, field="item_id"
            )
        lock_path.touch(exist_ok=True)


@contextmanager
def _existing_conductor_lock(slot_key: str) -> Iterator[None]:
    """Hold the conductor lock of a store that must ALREADY exist.

    ``conductor_lock`` creates the store's directory and lock file when they are
    absent, which is right for ``ensure_conductor`` and wrong for every other
    writer: a ``goal`` or item-create that waited behind a purge would rebuild the
    directory and its lock file before finding no header to refuse on, leaving a
    lock-only store the sweep keeps forever. A missing lock file here is a missing
    ledger -- ``CODE_NO_LEDGER`` -- and nothing is created. Scoped to the acquire:
    a ``FileNotFoundError`` raised by the body is the body's own.
    """
    try:
        lock_cm = conductor_lock(slot_key, create=False)
        lock_cm.__enter__()
    except FileNotFoundError:
        raise WorkLedgerError(
            f"no work ledger for {slot_key!r}: its lock file is missing, so nothing can be "
            "written to it -- the store was purged or is incomplete; ensure_conductor "
            "recreates it",
            code=CODE_NO_LEDGER,
        ) from None
    try:
        yield
    finally:
        lock_cm.__exit__(None, None, None)


@contextmanager
def binding_lock(worker_slot_key: str) -> Iterator[None]:
    """Hold one worker's binding lock. THIRD in the lock order; see the module
    docstring. Guards the read-then-write on ``bindings/<worker>.json`` so two
    conductors binding the same worker at once cannot both see it free."""
    path = binding_path(worker_slot_key)
    with _open_lock(path.with_suffix(".lock")):
        yield


# --------------------------------------------------------------------------- #
# Whole-file reads. A corrupt record reads as absent.
# --------------------------------------------------------------------------- #


def _read_json_record(path: Path, *, strict: bool = False) -> Any | None:
    """Parse a whole-file JSON record, or ``None`` when it cannot be trusted.

    ``strict=True`` keeps the corruption-reads-as-absent contract for CONTENT
    (torn, oversized, non-UTF-8) but re-raises an I/O error other than
    ``FileNotFoundError``. A guard that must fail CLOSED uses it: a file that is
    present but momentarily unreadable -- a Windows rename race, a permission
    blip -- must not be mistaken for a file that is gone.

    Absent, unreadable, non-UTF-8, unparseable, and OVER the size ceiling all read
    the same way, because a two-writer store's reader must not be what crashes when
    the other writer was interrupted mid-write. The ceiling is checked before the
    read so a hand-grown file cannot be pulled into memory first.

    The bytes come from ``read_bytes_with_retry`` because a strict read here is
    LOCK-FREE across writers: :func:`_refuse_if_worker_holds_open_item` reads the
    prior item's file under the WORKER's binding lock, while that item's own
    conductor may be replacing it under a different item lock. On Windows a read of
    a file another handle holds open for write raises ``PermissionError``, so one
    correct concurrent writer is enough to turn a strict read into a bare
    ``OSError`` — which the dashboard route maps to a transient 503 "try again"
    instead of the permanent already-bound refusal the guard exists to raise. The
    retry closes that window. POSIX permits the read, and there a
    ``PermissionError`` is a genuine access fault the helper re-raises at once.
    """
    try:
        if path.stat().st_size > MAX_RECORD_BYTES:
            logger.warning(
                "work ledger record over the size ceiling; treating as absent: %s",
                path.name,
            )
            return None
        return json.loads(read_bytes_with_retry(path).decode("utf-8"))
    except FileNotFoundError:
        return None
    except OSError:
        if strict:
            raise
        return None
    except (ValueError, UnicodeDecodeError):
        # ``ValueError`` already covers ``json.JSONDecodeError``.
        return None


def _serialize(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _write_record(path: Path, payload: dict[str, Any]) -> None:
    """Whole-file atomic replace, owner-only.

    ``restrict_to_owner=True`` alongside ``mode=0o600`` — ``atomic_write`` accepts
    the pair and refuses the flag with any other mode, so the two are effectively
    one choice. ``session_ledger`` passes only the mode; this store carries a
    worker's own words into a conductor's context, so it takes the stronger form.

    ``newline="\\n"`` keeps the stored bytes equal to the bytes the ceiling checks
    measured: with the default translation Windows writes ``\\r\\n``, one byte per
    line more than :func:`_serialize` produced, so a record measured just under
    :data:`MAX_RECORD_BYTES` could land over it and read back as absent.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, _serialize(payload), mode=0o600, restrict_to_owner=True, newline="\n")


def read_conductor(slot_key: str, *, strict: bool = False) -> ConductorRecord | None:
    """*slot_key*'s conductor record, or ``None`` when there is no readable ledger.

    ``strict`` has :func:`_read_json_record`'s meaning; the two header WRITERS use
    it so a transient I/O error cannot masquerade as an absent ledger and mint a
    fresh header over the real one.

    Lock-free: a reader that took the write lock would serialise every probe tick
    behind every write, and a whole-file atomic replace means a reader sees either
    the old record or the new one, never a blend.
    """
    raw = _read_json_record(conductor_dir(slot_key) / _CONDUCTOR_FILE, strict=strict)
    if raw is None:
        return None
    record = ConductorRecord.from_dict(raw)
    if not record.slot_key:
        record.slot_key = slot_key
    return record


def read_work_item(slot_key: str, item_id: str, *, strict: bool = False) -> WorkItem | None:
    """One item, or ``None`` when it is absent or unreadable. Lock-free.

    A record whose stored ``item_id`` names a DIFFERENT item than the file it sits
    in reads as absent. The writer always keeps the two equal, so a mismatch is a
    misnamed or hand-moved file, and honouring its stored id would let a write
    taken under THIS item's lock land on THAT item's path.
    """
    raw = _read_json_record(item_path(slot_key, item_id), strict=strict)
    if raw is None:
        return None
    item = WorkItem.from_dict(raw)
    if not item.item_id:
        item.item_id = item_id
    elif item.item_id != item_id:
        logger.warning(
            "work ledger item %s stores id %s; treating as absent", item_id, item.item_id
        )
        return None
    return item


def list_work_items(slot_key: str) -> list[WorkItem]:
    """Every readable item, oldest first.

    DERIVED by listing the items directory rather than read from an index, so there
    is no third writer over a file both parties care about, and therefore no index
    to fall out of step with the files it names. An unreadable item is skipped: one
    torn file must not hide the rest.
    """
    try:
        entries = sorted(items_dir(slot_key).glob("it_*.json"))
    except OSError:
        return []
    items: list[WorkItem] = []
    for entry in entries:
        # The glob is a prefix match; only a full ``it_<8 hex>`` stem is an item.
        # A stray ``it_bad.json`` is corruption in the directory, and corruption
        # reads as absent here too rather than crashing every listing.
        if not _ITEM_ID_RE.match(entry.stem):
            continue
        item = read_work_item(slot_key, entry.stem)
        if item is not None:
            items.append(item)
    items.sort(key=lambda it: (it.created_at, it.item_id))
    return items


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #


def event_id(ts: str, item_id: str, kind: str, text: str, *, status: str | None = None) -> str:
    """Content-addressed id for one event line.

    The formula itself lives in :func:`kiro_crew.work_vocab.work_event_id`, and this is
    the store's name for it. It is spelled once and in a leaf because the crew-log fold
    reproduces the same ids and may not import this module, so a second spelling here
    could drift from the one the fold computes -- and the rebuild matches events BY id,
    which is exactly the comparison that would then stop meaning anything.
    """
    return work_event_id(ts, item_id, kind, text, status=status)


def _read_events_unlocked(path: Path, *, strict: bool = False) -> list[WorkEvent]:
    """Every parseable line, oldest first, duplicate ids collapsed FIRST-seen-wins.

    A malformed line is skipped rather than failing the read: the log is append-only
    and a torn tail must not hide the history in front of it. ``strict`` re-raises
    an I/O error other than ``FileNotFoundError``; the WRITER reads that way, because
    it rewrites the whole log from what it read and a transient error read as
    "empty" would replace the entire history with one line.
    """
    try:
        if path.stat().st_size > MAX_RECORD_BYTES:
            logger.warning("work ledger event log over the size ceiling: %s", path.name)
            return []
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []
    except OSError:
        if strict:
            raise
        return []
    seen: set[str] = set()
    out: list[WorkEvent] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        event = WorkEvent.from_dict(parsed)
        if event is None:
            continue
        if event.id and event.id in seen:
            continue
        if event.id:
            seen.add(event.id)
        out.append(event)
    return out


def read_events(slot_key: str, item_id: str, *, limit: int | None = None) -> list[WorkEvent]:
    """One item's events, oldest first, deduplicated. ``limit`` keeps the NEWEST."""
    events = _read_events_unlocked(item_events_path(slot_key, item_id))
    if limit is not None and limit >= 0:
        return events[-limit:] if limit else []
    return events


def _coalesce_progress(events: list[WorkEvent], incoming: WorkEvent) -> list[WorkEvent]:
    """Drop the trailing ``progress`` report when *incoming* is another one.

    This is the answer to the RFC's open question Q6. A worker in a tight loop can
    otherwise append 200 ``progress`` lines and roll its own history off the back of
    the cap before the conductor ever wakes — ``progress`` does not wake it — so the
    events that DID matter are the ones lost. Collapsing consecutive progress keeps
    the newest position and spends the cap on transitions instead of on chatter.

    Only CONSECUTIVE progress reports merge, and only with each other. A ``done``,
    ``blocked`` or ``question`` between two progress lines stops the merge, because
    a status change is exactly the history worth keeping.
    """
    if not incoming.is_progress_report or not events:
        return events
    if not events[-1].is_progress_report:
        return events
    return events[:-1]


def _append_event_locked(
    slot_key: str, item_id: str, kind: str, text: str, *, status: str | None = None
) -> WorkEvent:
    """Append one event to *item_id*'s log. THE CALLER MUST HOLD THE ITEM LOCK.

    Stamps ``ts`` here, under the lock and immediately before the write, so file
    order and timestamp order agree. A caller that built its entry first and then
    blocked on the lock would write a line whose timestamp precedes the line already
    above it, and a reader sorting by ``ts`` would disagree with one walking the file.

    The whole log is REWRITTEN rather than appended to. The RFC specifies an append
    under the lock, and a plain append cannot implement either the oldest-dropped
    cap or the progress-coalescing rule, both of which delete a line. One rewrite
    path does both, and at 200 short lines it costs less than the second code path
    would. Atomicity is unchanged: the rewrite is an ``atomic_write`` rename held
    under the same lock, so a reader sees the old log or the new one.
    """
    if kind not in EVENT_KINDS:
        raise WorkLedgerError(f"unknown event kind {kind!r}", code=CODE_INVALID_VALUE)
    ts = _now_iso()
    trimmed = text[:MAX_EVENT_TEXT_CHARS]
    incoming = WorkEvent(
        id=event_id(ts, item_id, kind, trimmed, status=status),
        ts=ts,
        item_id=item_id,
        kind=kind,
        status=status,
        text=trimmed,
    )
    path = item_events_path(slot_key, item_id)
    events = _coalesce_progress(_read_events_unlocked(path, strict=True), incoming)
    events.append(incoming)
    if len(events) > MAX_EVENTS_PER_ITEM:
        events = events[-MAX_EVENTS_PER_ITEM:]
    body = "".join(json.dumps(event.to_dict(), ensure_ascii=False) + "\n" for event in events)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, body, mode=0o600, restrict_to_owner=True, newline="\n")
    return incoming


def _read_text_or_none(path: Path) -> str | None:
    """A file's text as it stands, or ``None`` when it does not exist. For rollback:
    ``newline=""`` so what is restored is byte-for-byte what was there."""
    try:
        with path.open("r", encoding="utf-8", newline="") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def _restore_text(path: Path, snapshot: str | None) -> None:
    """Put *path* back as *snapshot* found it; ``None`` means delete. Never raises --
    the caller already holds the error it must surface."""
    try:
        if snapshot is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write(path, snapshot, mode=0o600, restrict_to_owner=True, newline="")
    except OSError:
        logger.error(
            "work ledger: could not restore %s after a failed write", path.name, exc_info=True
        )


def _commit_item_locked(
    slot_key: str, item: WorkItem, kind: str, text: str, *, status: str | None = None
) -> WorkEvent:
    """Persist one item change AND the event that explains it. HOLD THE ITEM LOCK.

    Two files, no atomic two-file rename, so the order and the rollback are what make
    the pair safe. EVENT FIRST, ITEM SECOND: if the item write then fails, the log is
    put back exactly as it was, so the two never disagree. The other order would let
    a disk-full between the writes persist a state change with no line explaining
    it -- a terminal item with no ``close`` event, forever.

    The serialized item is measured against the read ceiling BEFORE either write.
    ``_require_acceptance`` bounds the compact form, but the stored form is indented,
    and an item that writes successfully and then reads as absent is the silent loss
    this module exists to refuse.
    """
    payload = item.to_dict()
    body = _serialize(payload)
    if len(body.encode("utf-8")) > MAX_RECORD_BYTES:
        raise WorkLedgerError(
            "item record would exceed the read ceiling; shrink acceptance",
            code=CODE_FIELD_TOO_LONG,
            field="acceptance",
        )
    events_path = item_events_path(slot_key, item.item_id)
    log_before = _read_text_or_none(events_path)
    event = _append_event_locked(slot_key, item.item_id, kind, text, status=status)
    try:
        _write_record(item_path(slot_key, item.item_id), payload)
    except BaseException:
        _restore_text(events_path, log_before)
        raise
    return event


# --------------------------------------------------------------------------- #
# Validation. Every bound is checked BEFORE the first write, so a refusal leaves
# every file byte-identical.
# --------------------------------------------------------------------------- #


def _require_text(value: Any, cap: int, name: str, *, required: bool = True) -> str:
    if value is None:
        if required:
            raise WorkLedgerError(f"{name} is required", code=CODE_INVALID_VALUE, field=name)
        return ""
    if not isinstance(value, str):
        raise WorkLedgerError(f"{name} must be a string", code=CODE_INVALID_VALUE, field=name)
    if len(value) > cap:
        raise WorkLedgerError(
            f"{name} is {len(value)} chars; the cap is {cap}",
            code=CODE_FIELD_TOO_LONG,
            field=name,
        )
    return value


def _require_choice(value: Any, allowed: frozenset[str], name: str, code: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise WorkLedgerError(
            f"{name} must be one of {sorted(allowed)}; got {value!r}",
            code=code,
            field=name,
        )
    return value


def _require_acceptance(value: Any) -> dict[str, Any]:
    """``acceptance`` is stored VERBATIM and never interpreted here.

    ``accept_eval.py`` is the only thing that decides whether an item passed, so
    this store validates the container and not the contents — a shape check here
    would be a second, drifting copy of that script's contract.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise WorkLedgerError(
            "acceptance must be an object", code=CODE_INVALID_VALUE, field="acceptance"
        )
    try:
        serialized = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise WorkLedgerError(
            f"acceptance must be JSON-serialisable: {exc}",
            code=CODE_INVALID_VALUE,
            field="acceptance",
        ) from exc
    if len(serialized.encode("utf-8")) > MAX_RECORD_BYTES // 2:
        raise WorkLedgerError(
            "acceptance is too large to store",
            code=CODE_FIELD_TOO_LONG,
            field="acceptance",
        )
    return value


def _require_artifacts(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise WorkLedgerError(
            "artifacts must be an object", code=CODE_INVALID_VALUE, field="artifacts"
        )
    if len(value) > MAX_ARTIFACT_KEYS:
        raise WorkLedgerError(
            f"artifacts has {len(value)} keys; the cap is {MAX_ARTIFACT_KEYS}",
            code=CODE_FIELD_TOO_LONG,
            field="artifacts",
        )
    out: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise WorkLedgerError(
                "artifacts must map strings to strings",
                code=CODE_INVALID_VALUE,
                field="artifacts",
            )
        if len(key) > MAX_ARTIFACT_KEY_CHARS:
            raise WorkLedgerError(
                f"artifacts key is {len(key)} chars; the cap is " f"{MAX_ARTIFACT_KEY_CHARS}",
                code=CODE_FIELD_TOO_LONG,
                field="artifacts",
            )
        if len(item) > MAX_ARTIFACT_VALUE_CHARS:
            raise WorkLedgerError(
                f"artifacts value is {len(item)} chars; the cap is " f"{MAX_ARTIFACT_VALUE_CHARS}",
                code=CODE_FIELD_TOO_LONG,
                field="artifacts",
            )
        out[key] = item
    return out


def _require_pr(value: Any) -> int | None:
    """A worker's CLAIMED pull-request number.

    A claim only, and the docstring says so because the shape nearly leaked here:
    ``accept_eval.py`` needs an integer ``pr``, the worker is what learns the
    number, and filling ``acceptance.pr`` from this field would let a worker name
    any already-green pull request and pass. Promoting a claim into ``acceptance``
    is a conductor action.
    """
    if value is None:
        return None
    number = _finite_int(value)
    if number is None or not (MIN_PR <= number <= MAX_PR):
        raise WorkLedgerError(
            f"pr must be an integer in {MIN_PR}..{MAX_PR}; got {value!r}",
            code=CODE_INVALID_VALUE,
            field="pr",
        )
    return number


def _require_count(value: Any, name: str) -> int:
    number = _finite_int(value)
    if number is None or number < 0:
        raise WorkLedgerError(
            f"{name} must be a non-negative integer; got {value!r}",
            code=CODE_INVALID_VALUE,
            field=name,
        )
    return number


# --------------------------------------------------------------------------- #
# Depth
# --------------------------------------------------------------------------- #


def child_depth(parent_depth: int) -> int:
    """The depth a conductor dispatched BY one at *parent_depth* would have.

    Refuses past the cap rather than clamping: a clamped depth would let level three
    exist while reporting as level two, which is the failure the cap exists to stop.
    Two levels rather than three because each level multiplies sessions — three
    levels of three items is twenty-seven — and because a summary of summaries of
    summaries is not evidence any more. That number is a guess informed by session
    multiplication, not a measurement (RFC Q5).
    """
    depth = _require_count(parent_depth, "depth")
    if depth + 1 > MAX_DEPTH:
        raise WorkLedgerError(
            f"depth {depth} is at the cap of {MAX_DEPTH}; this session may not "
            "dispatch a conductor",
            code=CODE_DEPTH_EXCEEDED,
            field="depth",
        )
    return depth + 1


# --------------------------------------------------------------------------- #
# Derived flags. Pure functions — Phase 1 wires no dashboard state.
# --------------------------------------------------------------------------- #


def is_orphaned(item: WorkItem, *, conductor_slot_exists: bool) -> bool:
    """Whether nothing is left to read *item*'s reports.

    Derived, never stored: nothing is running at session-close time to stamp a flag,
    a missed stamp would stay wrong forever, and a derived flag self-heals when the
    session is reopened. The worker keeps writing — its binding is still valid — and
    the writes simply accumulate unread.
    """
    return not conductor_slot_exists and not item.is_terminal


def is_stale(
    item: WorkItem,
    *,
    worker_running: bool,
    now: datetime | None = None,
    window_secs: float = DEFAULT_STALE_WINDOW_SECS,
) -> bool:
    """Whether *item* has gone quiet in a way that is worth waking someone for.

    A CONJUNCTION, and the conjunction is the point: quiet plus not running. A worker
    in a thirty-minute build is running, so it is never flagged however long it stays
    silent. The window exists only to cover the gap between binding and the first
    report, and to catch a session that ended without reporting.

    A terminal item is never stale — there is nothing left to report. Neither is one
    whose last report was ``done``: the ball is in the conductor's court (verify,
    promote, close) or a human's, and silence from a worker that has already claimed
    its bar is met is the expected end of its work, not a gap worth waking anyone for.
    :data:`STALE_ELIGIBLE_STATUSES` names the statuses that do still count, and no
    report yet counts too — that is the bind-to-first-report gap.

    One exception, and it is the same rule read carefully: a ``done`` item the conductor
    has already ruled ``verdict: fail`` on and left OPEN is a retry, so the move is back
    with the worker and its silence is a gap again. The flag follows who owns the next
    move, not the words of the last report.

    An item with no report yet is measured from ``created_at``, which is what makes
    that gap visible. An unparseable timestamp reads as stale, because the alternative
    is an item that can never be flagged.
    """
    if item.is_terminal or worker_running:
        return False
    if not _worker_owns_next_move(item):
        return False
    reference = item.last_report_at or item.created_at
    if not reference:
        return True
    stamped = _parse_iso(reference)
    if stamped is None:
        return True
    moment = now or datetime.now().astimezone()
    if stamped.tzinfo is None:
        stamped = stamped.astimezone()
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment - stamped > timedelta(seconds=max(window_secs, 0.0))


def _worker_owns_next_move(item: WorkItem) -> bool:
    """Whether *item*'s last report leaves the next move with the WORKER.

    The half of :func:`is_stale` that keeps the flag pointed at a worker rather than at
    whoever is reading. No report yet counts (the worker owes the first one), and so
    does a ``done`` whose verdict came back ``fail`` on a still-open item — the
    conductor handed that work back.
    """
    if item.status is None or item.status in STALE_ELIGIBLE_STATUSES:
        return True
    return item.status == "done" and item.verdict == "fail"


def is_acceptance_concrete(acceptance: Any) -> bool:
    """Whether *acceptance* names a bar ``accept_eval.py`` can actually evaluate.

    Derived at read time like :func:`is_stale` and :func:`is_orphaned`, and for the
    same reason: the answer changes when the conductor promotes the bar, so a stamped
    flag would go stale in the one direction that matters.

    A conductor may dispatch an item before its bar is knowable and write ``"TBD"`` in
    the field until a worker reports the real value. Handed such a condition,
    ``accept_eval.py`` answers ``error`` — "pr_checks spec needs an integer pr" — which
    a conductor reading a column of verdicts is then tempted to take for a real failure
    of the work. So a non-concrete bar is left OUT of :func:`accept_batch` entirely
    until an ``accept`` promotion fills it in, and this predicate is what the read
    surfaces so the omission is visible rather than mysterious.

    Judged FIELD BY FIELD, over :data:`ACCEPTANCE_READ_FIELDS` — only what that script
    reads for this ``kind``. Deliberately NOT a scan of the whole object: an acceptance
    is stored verbatim and may legitimately carry metadata the evaluator never looks at
    (a branch name, a note, a `cmd` argv that mentions the word TBD), and judging those
    would drop an evaluable bar for a field that cannot affect the verdict. A whole-
    object walk was also a recursion over caller-supplied nesting on the read path,
    which a deep record could turn into a failed ledger read for the whole slot.

    Non-concrete means: an empty acceptance (the absence of a bar); a ``kind`` outside
    :data:`ACCEPTANCE_KINDS`; a placeholder STRING (blank, or ``"TBD"`` in any case) in
    a field this ``kind``'s evaluator reads; or one of those fields mistyped — a
    ``pr_checks`` ``pr`` that is not a positive integer, a ``file`` ``path`` that is not
    a string, a ``file`` ``exists`` that is not a bool. An explicit ``null`` in an
    OPTIONAL read field is absence, which the evaluator handles, so it is concrete; in a
    required one the type rule refuses it.

    Those type rules are ``accept_eval.py``'s own error-only guards, mirrored here so
    the batch never carries a spec that script can answer nothing but ``error`` to. The
    mirror is deliberate duplication across a process boundary: the two must agree, and
    a test pins them against the script's real behaviour. It stops at the guards that
    are error-ONLY — a ``cmd`` bar stays in the batch, because ``refused`` is a verdict
    the conductor is supposed to receive and act on.
    """
    if not isinstance(acceptance, dict) or not acceptance:
        return False
    kind = acceptance.get("kind")
    if not isinstance(kind, str) or kind not in ACCEPTANCE_KINDS:
        return False
    for name in ACCEPTANCE_READ_FIELDS[kind]:
        # An ABSENT read field is not a placeholder: ``repo`` and ``exists`` are both
        # optional in the evaluator, and the type rules below are what catch a missing
        # field that is actually required.
        if name in acceptance and _is_placeholder(acceptance[name]):
            return False
    if kind == "pr_checks":
        pr = acceptance.get("pr")
        # ``bool`` is an ``int`` subclass, and ``pr: true`` is not a pull request.
        if isinstance(pr, bool) or not isinstance(pr, int) or pr < MIN_PR:
            return False
    if kind == "file":
        if not isinstance(acceptance.get("path"), str):
            return False
        # ``exists`` defaults to True in the evaluator, so its ABSENCE is fine; only a
        # present non-bool is not. ``1``/``0`` are rejected there too, and coercing
        # here would let a truthy ``"false"`` invert an absence check into a presence
        # one — the reason that guard is a type check rather than ``bool()``.
        if not isinstance(acceptance.get("exists", True), bool):
            return False
    return True


def _is_placeholder(value: Any) -> bool:
    """Whether ONE field value still says "not filled in yet".

    Only a STRING can say it. ``None`` is deliberately not a placeholder: the evaluator
    treats an absent optional field as absent (``repo: null`` simply omits ``--repo``),
    so calling it unfilled would drop an evaluable bar — and where a field is genuinely
    required, the per-kind type rules below refuse ``None`` anyway, which is the honest
    place for that judgement.

    Flat on purpose: it is applied to the named fields the evaluator reads, never walked
    over a caller-supplied object graph. A container in such a field is not a
    placeholder — it is a wrong type, and the type rules refuse it.
    """
    return isinstance(value, str) and value.strip().lower() in ACCEPTANCE_PLACEHOLDERS


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Bindings. One file per worker, written only by a conductor's ``bind`` action, and
# only as a whole-file replacement under that worker's binding lock -- so a
# worker's binding moves to a new item exactly when its prior item is terminal,
# and two conductors cannot both believe they bound it.
# --------------------------------------------------------------------------- #


def read_binding(worker_slot_key: str, *, strict: bool = False) -> tuple[str, str] | None:
    """The ``(conductor_slot_key, item_id)`` *worker_slot_key* is bound to.

    ``None`` when unbound, which is the state Phase 2 answers as ``not_bound``. A
    binding naming a malformed item id also reads as unbound rather than raising:
    a worker cannot repair its own binding, so a clean refusal is the only useful
    answer. ``strict`` has :func:`_read_json_record`'s meaning -- a present but
    momentarily unreadable file raises instead of reading as unbound -- and is
    what the bind guard uses so it fails closed.
    """
    raw = _read_json_record(binding_path(worker_slot_key), strict=strict)
    if not isinstance(raw, dict):
        return None
    conductor = _as_str(raw.get("conductor_slot_key"))
    item_id = _as_str(raw.get("item_id"))
    if not conductor or not _ITEM_ID_RE.match(item_id):
        return None
    return conductor, item_id


def _refuse_if_worker_holds_open_item(worker_slot_key: str) -> None:
    """Refuse a bind that would overwrite a binding whose item is still open.

    A worker has exactly ONE binding file, so a second bind silently repoints its
    only report channel: the first item keeps naming the worker while every report
    lands on the second, and with no unbind path that is permanent. A binding whose
    item is terminal, or whose item FILE IS GONE, is stale and may be replaced --
    that is how a worker session is reused for a new item once its last one closed.

    A binding whose item does NOT name this worker back is stale too. ``bind``
    writes the binding first and the item second, so a process killed between the
    two leaves exactly that half-state; the writer never produces it any other way.
    Treating it as live would make the retry of that very bind refuse forever.

    FAILS CLOSED on a transient read error. The prior item's file is not under this
    lock -- its own conductor may be mid-rename on it -- so a momentary ``OSError``
    is propagated rather than read as "absent": the bind refuses and the caller
    retries, instead of stranding an item that was open all along.

    CALL UNDER THE WORKER'S BINDING LOCK, so the check and the write it guards are
    one critical section.
    """
    existing = read_binding(worker_slot_key, strict=True)
    if existing is None:
        return
    prior_conductor, prior_item_id = existing
    try:
        prior = read_work_item(prior_conductor, prior_item_id, strict=True)
    except WorkLedgerError:
        return
    if prior is None or prior.is_terminal:
        return
    if prior.worker_session_key != worker_slot_key:
        logger.warning(
            "work ledger: binding for a worker names item %s, which does not name the "
            "worker back; treating the binding as an interrupted bind and replacing it",
            prior_item_id,
        )
        return
    raise WorkLedgerError(
        f"worker is already bound to open item {prior_item_id!r}; close that item "
        "before binding the worker again",
        code=CODE_ALREADY_BOUND,
        field="worker_session_key",
    )


def _read_binding_text(worker_slot_key: str) -> str | None:
    """The binding file's bytes-as-text, or ``None`` if absent. For rollback: text
    rather than a parsed record, so what is restored is what was there."""
    try:
        return _read_text_or_none(binding_path(worker_slot_key))
    except OSError:
        return None


def _restore_binding_text(worker_slot_key: str, snapshot: str | None) -> None:
    """Put the binding back as *snapshot* found it. ``None`` means delete. Never
    raises: the caller already holds the error it must surface."""
    _restore_text(binding_path(worker_slot_key), snapshot)


def _write_binding(worker_slot_key: str, conductor_slot_key: str, item_id: str) -> None:
    _write_record(
        binding_path(worker_slot_key),
        {
            "schema": SCHEMA_VERSION,
            "conductor_slot_key": conductor_slot_key,
            "item_id": item_id,
            "worker_slot_key": worker_slot_key,
            "created_at": _now_iso(),
        },
    )


# --------------------------------------------------------------------------- #
# Conductor writes
# --------------------------------------------------------------------------- #


def ensure_conductor(
    slot_key: str,
    *,
    goal: str = "",
    depth: int = 0,
    parent_item: str | None = None,
) -> ConductorRecord:
    """Create *slot_key*'s ledger if absent, and return the record either way.

    Idempotent, and it does NOT overwrite an existing goal, round or depth — a
    conductor that re-enters its own ledger on a later round must not reset it.
    Use ``apply_conductor_action(slot_key, "goal", ...)`` to change the goal.

    *depth* and *parent_item* are SERVER-owned in Phase 2: the value passed here
    comes from the creating session's own record via :func:`child_depth`, never from
    a model. Refuses a depth past the cap so a ledger cannot exist at a level that
    is not allowed to conduct.
    """
    checked_goal = _require_text(goal, MAX_GOAL_CHARS, "goal", required=False)
    checked_depth = _require_count(depth, "depth")
    if checked_depth > MAX_DEPTH:
        raise WorkLedgerError(
            f"depth {checked_depth} is past the cap of {MAX_DEPTH}",
            code=CODE_DEPTH_EXCEEDED,
            field="depth",
        )
    if parent_item is not None:
        _require_item_id(parent_item)
    directory = conductor_dir(slot_key)
    with conductor_lock(slot_key):
        existing = read_conductor(slot_key, strict=True)
        if existing is not None:
            return existing
        record = ConductorRecord(
            slot_key=slot_key,
            goal=checked_goal,
            round=0,
            depth=checked_depth,
            parent_item=parent_item,
            created_at=_now_iso(),
            generation=secrets.token_hex(8),
        )
        _write_record(directory / _CONDUCTOR_FILE, record.to_dict())
        try:
            atomic_write(directory / _KEY_FILE, slot_key + "\n", mode=0o600)
        except OSError:
            logger.debug("work ledger: slot_key breadcrumb write failed", exc_info=True)
        return record


def apply_conductor_action(
    slot_key: str,
    action: str,
    *,
    item_id: str | None = None,
    title: Any = None,
    acceptance: Any = None,
    worker_session_key: Any = None,
    decision: Any = None,
    verdict: Any = None,
    state: Any = None,
    goal: Any = None,
    round_number: Any = None,
    fails: Any = None,
) -> dict[str, Any]:
    """Write the fields the CONDUCTOR owns, and append the one event that explains it.

    Never writes ``status``, ``summary``, ``artifacts``, ``pr`` or ``last_report_at``
    — those are the worker's, and :func:`apply_worker_report` is the only path to
    them. Phase 2 mounts this behind the conductor's tool and that one behind the
    worker's, so ownership is enforced by which function a caller can reach rather
    than by filtering inside a shared one.

    Actions and their fields:

    ``create``  ``title``, ``acceptance``, optional ``round_number`` — mints an id.
    ``bind``    ``item_id``, ``worker_session_key`` — writes the binding file.
    ``decide``  ``item_id``, ``decision``, optional ``round_number``.
    ``verdict`` ``item_id``, ``verdict``, optional ``fails``.
    ``close``   ``item_id``, ``state``, optional ``decision`` — stamps ``closed_at``.
    ``goal``    ``goal``, optional ``round_number`` — the conductor record only.

    Returns ``{"conductor", "item", "event"}``; ``item`` and ``event`` are ``None``
    for ``goal``. Raises :class:`WorkLedgerError` with a ``code`` from the ``CODE_*``
    constants. Every bound is checked before the first write, so a refused call
    leaves every file byte-identical.
    """
    if action not in CONDUCTOR_ACTIONS:
        raise WorkLedgerError(
            f"unknown action {action!r}; expected one of {sorted(CONDUCTOR_ACTIONS)}",
            code=CODE_INVALID_ACTION,
            field="action",
        )
    record = read_conductor(slot_key)
    if record is None:
        raise WorkLedgerError(f"no work ledger for {slot_key!r}", code=CODE_NO_LEDGER)

    if action == "goal":
        return {
            "conductor": _write_goal(slot_key, record, goal, round_number),
            "item": None,
            "event": None,
        }
    if action == "create":
        return _create_item(slot_key, record, title, acceptance, round_number)
    return _write_item_action(
        slot_key,
        record,
        action,
        item_id,
        worker_session_key=worker_session_key,
        decision=decision,
        verdict=verdict,
        state=state,
        round_number=round_number,
        fails=fails,
    )


def _header_under_lock(slot_key: str, snapshot: ConductorRecord, verb: str) -> ConductorRecord:
    """The live header for a writer that holds the conductor lock.

    Refuses with ``CODE_NO_LEDGER`` when the header FILE IS ABSENT: the ledger was
    purged while this writer waited on the lock, and writing the pre-lock
    snapshot back would resurrect a header into a removed store -- one with no
    breadcrumb, which no later purge could name. That is the one case the purge
    needs refused, and it is the only one refused here.

    A header that is PRESENT but does not parse is a different situation. The
    caller holds the lock, so no other writer is mid-replace; what is on disk is
    a torn record, and *snapshot* -- read moments ago from this same file, before
    the lock -- is the best account of it. Returning the snapshot lets the write
    proceed on that account: a ``goal`` rewrites the header and so repairs it, and
    a create takes its default round from it. That is what the store always did
    before the purge existed and what a live conductor needs from a crash-torn
    header. The distinction is the file's presence, which is what separates
    "purged" from "damaged" under a lock the purge also takes.
    """
    live = read_conductor(slot_key, strict=True)
    if live is not None:
        return live
    if not (conductor_dir(slot_key) / _CONDUCTOR_FILE).exists():
        raise WorkLedgerError(
            f"conductor ledger has no header file, so {verb} -- the store was purged "
            "or is incomplete; ensure_conductor recreates the header",
            code=CODE_NO_LEDGER,
        )
    logger.warning(
        "work ledger header for %s is present but unreadable under the lock; "
        "repairing it from the pre-lock snapshot",
        slot_key,
    )
    return snapshot


def _write_goal(
    slot_key: str, record: ConductorRecord, goal: Any, round_number: Any
) -> ConductorRecord:
    """Partial update of the conductor header, merged UNDER the lock.

    Bounds are checked before the lock; the fields a caller omitted are filled from
    the record re-read inside it, never from the pre-lock snapshot. Otherwise a
    goal-only call and a round-only call racing each other would each restore the
    other's field to the stale value it read before waiting.
    """
    checked_goal = None if goal is None else _require_text(goal, MAX_GOAL_CHARS, "goal")
    checked_round = None if round_number is None else _require_count(round_number, "round")
    with _existing_conductor_lock(slot_key):
        # The header is re-read under the lock and REQUIRED to be present, like
        # ``_create_item``: a ``goal`` that waited behind a purge must not
        # rewrite its pre-lock snapshot into the removed store. A present but
        # torn header is repaired from that snapshot instead; see
        # ``_header_under_lock``.
        current = _header_under_lock(slot_key, record, "its goal cannot be updated")
        if checked_goal is not None:
            current.goal = checked_goal
        if checked_round is not None:
            current.round = checked_round
        current.goal_version += 1
        _write_record(conductor_dir(slot_key) / _CONDUCTOR_FILE, current.to_dict())
        return current


def _create_item(
    slot_key: str,
    record: ConductorRecord,
    title: Any,
    acceptance: Any,
    round_number: Any,
) -> dict[str, Any]:
    """Mint one item under the conductor lock.

    The lock is the conductor's, not the item's, because the cap it enforces is a
    property of the SET: counting the items and adding one must not interleave with
    another call doing the same, or two calls each see thirty-one and both write.
    The new item's own lock is taken from inside that hold, in the documented order.

    Refuses when the conductor is at the depth cap, because an item is a dispatch and
    a session at the cap may not dispatch.
    """
    checked_title = _require_text(title, MAX_TITLE_CHARS, "title")
    checked_acceptance = _require_acceptance(acceptance)
    checked_round = None if round_number is None else _require_count(round_number, "round")
    if record.depth >= MAX_DEPTH:
        raise WorkLedgerError(
            f"conductor is at depth {record.depth} and the cap is {MAX_DEPTH}; it may "
            "not dispatch further work",
            code=CODE_DEPTH_EXCEEDED,
            field="depth",
        )
    with _existing_conductor_lock(slot_key):
        # The header is re-read INSIDE the lock and is REQUIRED to be present. Two
        # reasons. The default round must come from the live header, not the
        # pre-lock snapshot, so a concurrent ``goal`` round bump is seen. And a
        # header that is GONE means the ledger was purged while this call waited
        # on the lock: minting an item now would write a record into a store with
        # no header -- a ledger destroyed down to the records that made it one.
        # A header that is present but torn is a damaged live ledger, not a
        # purged one, and the snapshot stands in for it; see
        # ``_header_under_lock``.
        live = _header_under_lock(slot_key, record, "no item can be created in it")
        if checked_round is None:
            checked_round = live.round
        existing = list_work_items(slot_key)
        if len(existing) >= MAX_ITEMS_PER_CONDUCTOR:
            raise WorkLedgerError(
                f"conductor holds {len(existing)} items; the cap is " f"{MAX_ITEMS_PER_CONDUCTOR}",
                code=CODE_ITEM_CAP_EXCEEDED,
                field="items",
            )
        item_id = mint_item_id()
        while (items_dir(slot_key) / f"{item_id}.json").exists():
            item_id = mint_item_id()
        item = WorkItem(
            item_id=item_id,
            title=checked_title,
            acceptance=checked_acceptance,
            state="open",
            round=checked_round,
            created_at=_now_iso(),
        )
        with item_lock(slot_key, item_id):
            event = _commit_item_locked(slot_key, item, "create", checked_title)
        return {"conductor": read_conductor(slot_key), "item": item, "event": event}


def _write_item_action(
    slot_key: str,
    record: ConductorRecord,
    action: str,
    item_id: str | None,
    *,
    worker_session_key: Any,
    decision: Any,
    verdict: Any,
    state: Any,
    round_number: Any,
    fails: Any,
) -> dict[str, Any]:
    """``bind``, ``decide``, ``verdict`` and ``close``, each one item lock deep.

    Field validation is complete before the lock is taken, so a cap or shape refusal
    never creates a lock file for an item it declined to touch. Refusals that need
    the stored record — unknown item, closed item, already bound — happen under the
    lock, after that file exists.
    """
    checked_id = _require_item_id(item_id or "")
    checked_worker: str | None = None
    checked_decision: str | None = None
    checked_verdict: str | None = None
    checked_state: str | None = None
    checked_fails: int | None = None
    checked_round: int | None = None

    if action == "bind":
        checked_worker = _require_text(worker_session_key, 512, "worker_session_key")
        if "\0" in checked_worker:
            raise WorkLedgerError(
                "worker_session_key must not contain a null byte",
                code=CODE_INVALID_VALUE,
                field="worker_session_key",
            )
    elif action == "decide":
        checked_decision = _require_text(decision, MAX_DECISION_CHARS, "decision")
    elif action == "verdict":
        checked_verdict = _require_choice(verdict, VERDICTS, "verdict", CODE_INVALID_VALUE)
        if fails is not None:
            checked_fails = _require_count(fails, "fails")
    else:  # close
        checked_state = _require_choice(state, ITEM_STATES, "state", CODE_INVALID_VALUE)
        if checked_state == "open":
            raise WorkLedgerError(
                "close needs a terminal state, not 'open'",
                code=CODE_INVALID_VALUE,
                field="state",
            )
        if decision is not None:
            checked_decision = _require_text(decision, MAX_DECISION_CHARS, "decision")
    if round_number is not None:
        if action != "decide":
            raise WorkLedgerError(
                f"round_number is not a field of {action!r}; only decide (and create) " "take it",
                code=CODE_INVALID_VALUE,
                field="round_number",
            )
        checked_round = _require_count(round_number, "round")

    with item_lock(slot_key, checked_id, create=False):
        item = read_work_item(slot_key, checked_id)
        if item is None:
            raise WorkLedgerError(
                f"unknown item {checked_id!r}", code=CODE_UNKNOWN_ITEM, field="item_id"
            )
        if item.is_terminal:
            raise WorkLedgerError(f"item {checked_id!r} is {item.state}", code=CODE_ITEM_CLOSED)
        if checked_round is not None:
            item.round = checked_round

        if action == "bind":
            if item.worker_session_key:
                raise WorkLedgerError(
                    f"item {checked_id!r} is already bound",
                    code=CODE_ALREADY_BOUND,
                    field="worker_session_key",
                )
            assert checked_worker is not None
            with binding_lock(checked_worker):
                prior_binding = _read_binding_text(checked_worker)
                _refuse_if_worker_holds_open_item(checked_worker)
                # Binding FIRST, item SECOND, so a failure between the two never
                # leaves an item that names a worker with no binding behind it --
                # that item would refuse every retry with ``already_bound`` and the
                # worker could never report. The other half-state is harmless: a
                # binding naming an item that does not name the worker is simply
                # stale and the next bind replaces it. Should the item write fail
                # anyway, the binding file is put back exactly as it was found.
                _write_binding(checked_worker, slot_key, checked_id)
                try:
                    item.worker_session_key = checked_worker
                    event = _commit_item_locked(slot_key, item, "bind", _store_name(checked_worker))
                except BaseException:
                    _restore_binding_text(checked_worker, prior_binding)
                    raise
        elif action == "decide":
            assert checked_decision is not None
            item.decision = checked_decision
            event = _commit_item_locked(slot_key, item, "decision", checked_decision)
        elif action == "verdict":
            assert checked_verdict is not None
            item.verdict = checked_verdict
            if checked_fails is not None:
                item.fails = checked_fails
            event = _commit_item_locked(slot_key, item, "verdict", checked_verdict)
        else:
            assert checked_state is not None
            item.state = checked_state
            if checked_decision is not None:
                item.decision = checked_decision
            item.closed_at = _now_iso()
            event = _commit_item_locked(slot_key, item, "close", checked_decision or checked_state)
        return {"conductor": record, "item": item, "event": event}


# --------------------------------------------------------------------------- #
# Worker writes
# --------------------------------------------------------------------------- #


def apply_worker_report(
    slot_key: str,
    item_id: str,
    *,
    status: Any,
    summary: Any,
    artifacts: Any = None,
    pr: Any = None,
) -> dict[str, Any]:
    """Write the fields the WORKER owns: ``status``, ``summary``, ``artifacts``,
    ``pr`` and ``last_report_at``. Nothing else is reachable from here.

    There is no ``verdict``, ``state``, ``acceptance``, ``decision`` or ``round``
    parameter, so a worker cannot mark itself accepted or widen its own bar — the
    strongest thing it can say is ``status: done``, which is the TRIGGER for the
    conductor to run ``accept_eval.py``, not a substitute for it.

    *slot_key* and *item_id* are the CONDUCTOR's key and the bound item, which
    Phase 2 resolves from the caller's own binding via :func:`read_binding` rather
    than accepting either from the worker. Phase 1 takes them as arguments because
    there is no identity layer yet; that is the whole reason nothing imports this
    module until Phase 2 puts the resolver in front of it.

    ``status: progress`` twice in a row collapses in the event log — see
    :func:`_coalesce_progress` — but the item's own fields always hold the newest
    report.
    """
    checked_status = _require_choice(status, WORKER_STATUSES, "status", CODE_INVALID_STATUS)
    checked_summary = _require_text(summary, MAX_SUMMARY_CHARS, "summary")
    checked_artifacts = _require_artifacts(artifacts)
    checked_pr = _require_pr(pr)
    checked_id = _require_item_id(item_id)

    with item_lock(slot_key, checked_id, create=False):
        item = read_work_item(slot_key, checked_id)
        if item is None:
            raise WorkLedgerError(
                f"unknown item {checked_id!r}", code=CODE_UNKNOWN_ITEM, field="item_id"
            )
        if item.is_terminal:
            raise WorkLedgerError(f"item {checked_id!r} is {item.state}", code=CODE_ITEM_CLOSED)
        item.status = checked_status
        item.summary = checked_summary
        item.artifacts = checked_artifacts
        if checked_pr is not None:
            item.pr = checked_pr
        item.last_report_at = _now_iso()
        event = _commit_item_locked(
            slot_key, item, "report", checked_summary, status=checked_status
        )
        return {"item": item, "event": event}


def read_work_brief(slot_key: str, item_id: str) -> dict[str, Any] | None:
    """What ONE worker may see about its own item, and nothing more.

    Not the conductor's goal, not its other items, not a sibling's state: a worker
    has no reason to see its peers and every reason not to be able to. Phase 2's
    ``work_brief`` tool is this function behind the binding resolver.
    """
    item = read_work_item(slot_key, item_id)
    if item is None:
        return None
    return {
        "item_id": item.item_id,
        "title": item.title,
        "acceptance": item.acceptance,
        "round": item.round,
        "decision": item.decision,
        "status": item.status,
        "summary": item.summary,
    }


def accept_batch(items: list[WorkItem]) -> dict[str, Any]:
    """The ``{"items": [...]}`` document ``accept_eval.py`` reads on stdin.

    Composed from ``acceptance`` ALONE. The worker's claimed ``pr`` is deliberately
    absent: filling ``acceptance.pr`` from a worker's report would let a worker name
    any already-green pull request and pass. The claim is surfaced beside the item
    for a conductor to promote explicitly, which turns the two-phase acceptance the
    skill performs by hand into a visible field without moving control of the bar.

    An item whose bar is not yet concrete — a ``"TBD"`` pull request number, a blank
    field — is left out, which is the other half of that same two-phase shape: the
    skill promises the omission, and doing it here is what makes the promise true.
    :func:`is_acceptance_concrete` is the test, and the read surfaces it per item so a
    conductor can see WHY an item is missing from the batch.

    ``status`` rides along on each entry, and the batch is deliberately NOT filtered by
    it. The conductor applies the "only ``done`` items" filter — that judgement is its
    own, and the seam is load-bearing — but it should not need a second lookup to
    apply it. ``accept_eval.py`` reads ``id`` and ``accept`` and ignores the rest.
    """
    return {
        "items": [
            # ``id`` / ``accept`` are the keys accept_eval.py reads; ``item_id`` /
            # ``acceptance`` are this store's field names. The rename happens here,
            # at the one seam between the two, so neither side learns the other's
            # vocabulary.
            {"id": item.item_id, "accept": item.acceptance, "status": item.status}
            for item in items
            if not item.is_terminal and is_acceptance_concrete(item.acceptance)
        ]
    }


def apply_acceptance_update(
    slot_key: str,
    item_id: str,
    *,
    acceptance: Any,
) -> dict[str, Any]:
    """Replace ONE item's ``acceptance``, appending a ``decision`` event.

    This is the write behind the ``accept`` tool action, and it exists as its own
    function rather than as a seventh :data:`CONDUCTOR_ACTIONS` member on purpose.
    Its job is the promotion :func:`accept_batch` deliberately refuses to do for
    the worker: a conductor that has READ a worker's claimed ``pr`` and checked it
    substitutes the real number into the bar, turning the manual omission the
    skill performs by hand into one visible write. The bar still only ever moves
    under the conductor's own key — the worker has no parameter that reaches here.

    The event is kind ``decision`` because promoting a bar IS a conductor
    decision, and because :data:`EVENT_KINDS` is a closed vocabulary the store's
    readers (and Phase 3's probe) already dispatch on; a seventh kind would be a
    schema change for a write that is semantically one of the six.
    """
    checked_id = _require_item_id(item_id)
    if not acceptance:
        # REFUSED, not coerced. :func:`_require_acceptance` answers ``{}`` for
        # ``None`` because ``create`` may legitimately open an item whose bar is not
        # known yet — but a PROMOTION never legitimately clears one. Without this,
        # an ``accept`` that omitted its argument replaced the real condition with
        # ``{}``, :func:`accept_batch` then dropped the item for having no
        # acceptance, ``accept_eval.py`` never evaluated it, and the caller was told
        # 200. That is conductor-owned state lost silently and unrecoverably, which
        # is the one outcome a cap-and-refuse store must not produce.
        raise WorkLedgerError(
            "accept needs the acceptance object to promote; it cannot clear the bar",
            code=CODE_INVALID_VALUE,
            field="acceptance",
        )
    checked_acceptance = _require_acceptance(acceptance)

    with item_lock(slot_key, checked_id, create=False):
        item = read_work_item(slot_key, checked_id)
        if item is None:
            raise WorkLedgerError(
                f"unknown item {checked_id!r}", code=CODE_UNKNOWN_ITEM, field="item_id"
            )
        if item.is_terminal:
            raise WorkLedgerError(f"item {checked_id!r} is {item.state}", code=CODE_ITEM_CLOSED)
        item.acceptance = checked_acceptance
        event = _commit_item_locked(
            slot_key, item, "decision", "acceptance promoted by the conductor"
        )
        return {"item": item, "event": event}


# --------------------------------------------------------------------------- #
# Maintenance
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ItemCensus:
    """What one pass over a conductor's item files found.

    ``damage`` is set when the items DIRECTORY could not be enumerated -- its own
    answer, never folded into the counts, because an unreadable directory is the
    one case where the counts and the truth are unrelated: read as "zero items" it
    selects an ordinary purge, which removes the header and leaves behind the
    item data it could not see. An ABSENT directory is not damage: a conductor
    that never created an item has none.
    """

    open_items: int = 0
    closed: int = 0
    unreadable: int = 0
    newest_closed_at: str = ""
    damage: str = ""
    #: The newest mtime of any non-lock file under ``items/`` -- records, event
    #: logs, and a torn or misnamed file the census could not read. A ledger's
    #: age must see EVERY write to it: an unreadable item carries no stamp the
    #: census can read, and ``_create_item`` writes it without touching the
    #: header, so without this a crash-torn write into an old conductor would be
    #: invisible to the retention window and deletable under ``allow_unreadable``
    #: the moment it landed. Lock files are excluded because the purge itself
    #: touches them (``_hold_every_item_lock``), and a reading that moved under
    #: the reader would refuse forever.
    newest_write_at: datetime | None = None


def header_damage(directory: Path) -> str:
    """Why *directory*'s ``conductor.json`` cannot be trusted, or ``""`` when it reads.

    Presence is not readability. An absent, torn, oversized or non-object header
    all mean the same thing for a DELETE decision -- this store's own account of
    itself is missing -- and treating only absence as damage let a malformed
    header read as a finished ledger. The one check both the sweep's scanner and
    :func:`purge_conductor`'s locked recheck use, so a header that tears between
    the report and the purge is refused by the store exactly as the scanner would
    have refused to list it as finished.
    """
    header = directory / _CONDUCTOR_FILE
    try:
        size = header.stat().st_size
    except FileNotFoundError:
        return "no conductor record"
    except OSError as exc:
        return f"conductor record unreadable ({exc.strerror or exc})"
    if size > MAX_RECORD_BYTES:
        return "conductor record over the size ceiling"
    try:
        raw = json.loads(read_bytes_with_retry(header).decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return "conductor record is not readable JSON"
    if not isinstance(raw, dict):
        return "conductor record is not a JSON object"
    return ""


def census_items(directory: Path) -> ItemCensus:
    """Classify every item file under *directory* for a DELETE decision.

    The one census both the sweep's scanner and :func:`purge_conductor` use, so
    the classification rule lives once. It reads the files directly rather than
    through :func:`list_work_items`, which SKIPS an unreadable item: right for a
    listing, wrong here, because a torn record would then be invisible and the
    ledger would look finished. An item whose stored ``state`` is not one of
    :data:`ITEM_STATES` counts as unreadable rather than closed -- an
    unrecognised disposition is not evidence of closure. The newest ``closed_at``
    is chosen as an INSTANT: the stamps carry the local offset at write time, and
    across a DST change two of them sort lexically in the wrong order.

    Lock-free. Under the conductor lock it is authoritative; outside it, a
    snapshot the caller must re-take under the lock before acting on it.
    """
    open_items = closed = unreadable = 0
    newest = ""
    newest_at: datetime | None = None
    items_dir_path = directory / _ITEMS_DIR
    if is_link(items_dir_path):
        # A linked ``items/`` names another directory's files as this store's
        # items. Counting them would let a purge of THIS store delete THOSE
        # files; this is damage, and the purge refuses it under its own lock.
        return ItemCensus(damage="items directory is a link, not a directory")
    try:
        with os.scandir(items_dir_path) as scan:
            names = sorted(entry.name for entry in scan if entry.is_file())
    except FileNotFoundError:
        return ItemCensus()
    except OSError as exc:
        return ItemCensus(damage=f"items directory could not be read ({exc.strerror or exc})")
    newest_write: datetime | None = None
    for name in names:
        if not name.endswith(".lock"):
            try:
                written = datetime.fromtimestamp((items_dir_path / name).stat().st_mtime)
            except OSError:
                pass
            else:
                written = written.astimezone()
                if newest_write is None or written > newest_write:
                    newest_write = written
        if not name.endswith(".json"):
            continue
        if not _ITEM_ID_RE.match(name[: -len(".json")]):
            # Item-shaped, but its stem is not an id the store ever minted: a
            # renamed or hand-moved record. Skipping it would let a plain purge
            # delete it with the rest of the contents; for a delete decision it
            # is unreadable, like a record whose stored id disagrees with its
            # file. Event logs (``.jsonl``) and atomic-write temps (``.tmp``)
            # are not item-shaped and are not counted.
            unreadable += 1
            continue
        raw = _read_json_record(items_dir_path / name)
        if not isinstance(raw, dict):
            unreadable += 1
            continue
        stored_id = raw.get("item_id")
        if isinstance(stored_id, str) and stored_id and stored_id != name[: -len(".json")]:
            # The reader's own rule (``read_work_item``): a record whose stored
            # id names a different item than the file it sits in is a misnamed
            # or hand-moved file and reads as absent. For a DELETE decision that
            # is unreadable, not closed -- counting its terminal ``state`` would
            # let a plain purge remove a record the store itself will not read.
            unreadable += 1
            continue
        state = raw.get("state")
        if not isinstance(state, str) or state not in ITEM_STATES:
            unreadable += 1
        elif state in TERMINAL_ITEM_STATES:
            closed += 1
            closed_at = raw.get("closed_at")
            parsed = _parse_iso(closed_at) if isinstance(closed_at, str) else None
            if parsed is not None and parsed.tzinfo is None:
                # A naive stamp beside an offset-bearing one would make the ``>``
                # below raise TypeError out of a function that must not.
                parsed = parsed.astimezone()
            if (
                isinstance(closed_at, str)
                and parsed is not None
                and (newest_at is None or parsed > newest_at)
            ):
                newest_at, newest = parsed, closed_at
        else:
            open_items += 1
    return ItemCensus(
        open_items=open_items,
        closed=closed,
        unreadable=unreadable,
        newest_closed_at=newest,
        newest_write_at=newest_write,
    )


def _newest_activity(directory: Path, census: ItemCensus) -> datetime | None:
    """The instant this ledger last changed, by either of its two writers.

    The newest item ``closed_at`` alone is NOT the ledger's age: the conductor's
    own ``goal`` action rewrites ``conductor.json`` with no item involved, and a
    conductor that just bumped its round on a set of old closed items is a live
    conductor about to create. So age is the newer of the newest close and the
    header's mtime, and it is what :func:`purge_conductor` re-checks under the
    lock against the window the caller passes.
    """
    candidates: list[datetime] = []
    parsed = _parse_iso(census.newest_closed_at) if census.newest_closed_at else None
    if parsed is not None:
        candidates.append(parsed.astimezone() if parsed.tzinfo is None else parsed)
    # The newest write under ``items/`` by mtime, readable or not: the one
    # reading that sees a crash-torn item the census could not stamp. See
    # ``ItemCensus.newest_write_at``.
    if census.newest_write_at is not None:
        candidates.append(census.newest_write_at)
    # The header's mtime, or the DIRECTORY's when the header cannot be statted:
    # ``atomic_write`` renames into this directory, so a store whose header is
    # gone or unreadable still shows when it was last written to. Dropping the
    # reading would make the newest close the only age, and a store touched a
    # minute ago would then pass a window it should refuse. The same fallback
    # the sweep's scanner uses.
    for candidate_path in (directory / _CONDUCTOR_FILE, directory):
        try:
            mtime = candidate_path.stat().st_mtime
        except OSError:
            continue
        candidates.append(datetime.fromtimestamp(mtime).astimezone())
        break
    return max(candidates) if candidates else None


@contextmanager
def _hold_every_item_lock(directory: Path) -> Iterator[None]:
    """Hold every item lock under *directory*, or refuse. Call under the conductor lock.

    The census reads item FILES, and a file another writer is replacing at that
    moment reads as unreadable -- on Windows a read of a file another handle holds
    open raises, on POSIX a half-renamed record is torn. So a census alone cannot
    tell "damaged" from "being written", and ``allow_unreadable`` would let an
    operator remove an item whose worker holds its lock right now. The item lock
    is the one thing that CAN tell: a writer holds it for the whole of its
    read-modify-write. Taking every item lock NON-BLOCKING before the census, and
    holding them through the removal, makes "someone is writing" a refusal rather
    than a misclassification. A lock that cannot be taken is a live writer, and a
    live writer is not finished, whatever its file looks like.

    Non-blocking on purpose: a blocking acquire would make the purge wait for a
    worker's write to finish and then delete the item it just wrote. Taken in
    sorted name order, which is the same relative order every other path uses,
    so this cannot deadlock against a writer holding one item lock and waiting on
    another -- no writer holds two. Lock files are created for items that have
    none yet, exactly as :func:`item_lock` would; they go with the store.
    """
    items_dir_path = directory / _ITEMS_DIR
    try:
        with os.scandir(items_dir_path) as scan:
            stems = sorted(
                entry.name[: -len(".json")]
                for entry in scan
                if entry.is_file()
                and entry.name.endswith(".json")
                and _ITEM_ID_RE.match(entry.name[: -len(".json")])
            )
    except OSError:
        # Absent: a conductor with no items has no locks to take. Unreadable:
        # nothing can be enumerated, so nothing can be locked either -- the
        # census reports that same failure as damage and the purge refuses on it
        # (or, with allow_unreadable, the removal counts its own failure and
        # keeps the identity files). Either way this is not the place to decide.
        stems = []
    handles: list[int] = []
    try:
        for stem in stems:
            lock_path = items_dir_path / f"{stem}.lock"
            lock_path.touch(exist_ok=True)
            fd = os.open(str(lock_path), os.O_RDWR)
            if not try_acquire_lock(fd, exclusive=True):
                os.close(fd)
                raise WorkLedgerError(
                    f"item {stem!r} is being written right now; refusing to purge the ledger",
                    code=CODE_LEDGER_NOT_FINISHED,
                    field="state",
                )
            handles.append(fd)
        yield
    finally:
        for fd in reversed(handles):
            try:
                release_lock(fd)
            finally:
                os.close(fd)


def purge_conductor(slot_key: str, *, allow_unreadable: bool, idle_for: timedelta) -> bool:
    """Delete one conductor's whole ledger directory. Returns whether it went.

    Both keywords are REQUIRED, with no defaults: this is an irreversible delete
    with one production caller (``ledger_sweep.purge``), and a default would let
    a future caller skip the retention window or the unreadable-record refusal
    by omission. "No window" is spelled ``idle_for=timedelta(0)``, visibly.

    An EXPLICIT maintenance primitive, like ``session_ledger.purge_matching``:
    nothing in the request path calls it. The caller owns the POLICY decision -- which
    ledgers are old enough, which an operator asked about -- but not the
    correctness one: this function re-establishes for itself, under the lock,
    that the ledger is actually finished.

    IT IS AIMED BY KEY, NOT BY PATH, and resolves the directory itself through
    :func:`conductor_dir`. A caller that found a store by WALKING the root must
    therefore check that the directory it looked at is the one this key resolves
    to before calling: a copied or hand-made store carrying another ledger's
    ``slot_key`` breadcrumb otherwise sends this function at the canonical
    ledger, which the caller never listed and an operator never saw.

    Serialised by :func:`conductor_lock`, FIRST in the lock order, then EVERY
    item lock taken non-blocking in sorted order (:func:`_hold_every_item_lock`)
    -- the documented conductor -> item order, so no writer can be waiting the
    other way -- and the re-check happens INSIDE both holds. An item lock that
    cannot be taken is a worker mid-write, and the purge refuses: a file being
    replaced reads as unreadable to the census, so without the item locks
    ``allow_unreadable`` would let an operator remove an item its worker is
    writing to this instant. It refuses -- ``WorkLedgerError`` with
    code :data:`CODE_LEDGER_NOT_FINISHED` -- when any item is non-terminal, when
    the items directory cannot be enumerated, when any item record is unreadable
    unless *allow_unreadable* says the operator asked for that too, and when the
    ledger's newest activity by EITHER writer (an item close, a header rewrite or
    any write under ``items/`` -- see :func:`_newest_activity`) is younger than
    *idle_for*. The caller's scan measured age outside the lock; a
    ``goal`` round-bump between that scan and this hold is a live conductor, and
    the recheck is what catches it. ``_create_item`` and ``_write_goal`` take
    this same lock across their whole transaction, so neither can land while the
    re-check and the removal run.

    REMOVAL IS ORDERED, AND THE IDENTITY FILES GO LAST. Items and any other
    content are removed first, every failure is counted rather than ignored, and
    only once everything else is gone do ``conductor.json`` and then the
    ``slot_key`` breadcrumb go -- header first, breadcrumb after, so a header
    unlink that fails leaves a store that still NAMES itself and can be purged
    again, rather than one no key can reach. On any failure the store keeps
    whatever identity it still has and reads as ``unreadable`` to the next
    sweep instead of as a half-destroyed ledger. The return value is whether the
    directory is gone.

    THE LOCK INODES. Every lock file -- the conductor's and each item's -- is
    unlinked INSIDE the holds on POSIX (``unlink_lock_in_hold``), so a writer
    queued on any of them acquires a detached inode, finds its path gone and
    refuses; nothing is ever handed a second inode while a first is held.
    Windows refuses an unlink under an open handle and gets them after release
    (:func:`_remove_lock_shell`), which is safe there because that unlink fails
    whenever a writer still holds a handle -- the OS keeps the identity stable. Removing
    a store necessarily removes the inode its lock is taken on, so a writer that
    was blocked on the OLD inode is not serialised against a later writer that
    creates a NEW one. No ordering fixes that -- unlinking inside the hold has the
    same effect -- and renaming the store away first is refused on Windows while
    this function holds its own handle inside it. Every path-based advisory lock
    has this property the moment its store is deleted. What makes it
    harmless here is that every writer checks, inside its hold, that the inode it
    acquired is still the one at the lock's path (``require_lock_inode`` in
    ``_open_lock``): a writer that was queued behind this purge refuses with the
    same ``OSError`` a held lock produces, and ``_create_item`` additionally
    refuses when it finds no header. A queued writer therefore cannot publish
    into the removed store; the worst outcome is one refused write.
    """
    directory = conductor_dir(slot_key)
    if is_link(directory) or is_link(directory / _ITEMS_DIR):
        # A linked store, or a linked ``items/`` inside it, names files that are
        # not this ledger's; a removal would land on them. Refused outright,
        # before any lock is taken, whatever the caller passed: this is not a
        # damaged record an operator can ask to clear, it is a delete aimed
        # somewhere else.
        raise WorkLedgerError(
            "conductor ledger directory (or its items/) is a link, not a directory; "
            "refusing to purge through it",
            code=CODE_LEDGER_NOT_FINISHED,
            field="state",
        )
    try:
        lock_cm = conductor_lock(slot_key, create=False)
        lock_cm.__enter__()
    except FileNotFoundError:
        # Already gone -- removed by a concurrent sweep, or never there. Nothing
        # to do, and nothing must be created in its place.
        return False
    try:
        shell = _purge_conductor_locked(
            directory, allow_unreadable=allow_unreadable, idle_for=idle_for
        )
    finally:
        lock_cm.__exit__(None, None, None)
    if shell is None:
        return False
    # AFTER the conductor lock is released, never inside it. The shell's job is
    # the lock files the holds could not unlink, and on Windows the conductor's
    # own is one of them: its handle is still open until the line above, so an
    # unlink here while it was held raised, the directory stayed non-empty, and
    # the purge reported False over a store it had already emptied. The session
    # half (``session_ledger.purge_matching``) releases before its shell for the
    # same reason.
    conductor_lock_gone, item_locks_left = shell
    _remove_lock_shell(
        directory, conductor_lock_gone=conductor_lock_gone, item_locks_left=item_locks_left
    )
    return not directory.exists()


def _purge_conductor_locked(
    directory: Path, *, allow_unreadable: bool, idle_for: timedelta
) -> tuple[bool, list[Path]] | None:
    """The body of :func:`purge_conductor`, under the conductor lock it acquired.

    Returns what :func:`_remove_lock_shell` needs once that lock is released --
    whether the conductor lock went inside the hold, and the item lock paths that
    did not -- or ``None`` when the store was kept.
    """
    with _hold_every_item_lock(directory):
        census = census_items(directory)
        if census.open_items:
            raise WorkLedgerError(
                f"conductor ledger has {census.open_items} open item(s); refusing to purge it",
                code=CODE_LEDGER_NOT_FINISHED,
                field="state",
            )
        if census.damage and not allow_unreadable:
            raise WorkLedgerError(
                f"conductor ledger's {census.damage}; refusing to purge it",
                code=CODE_LEDGER_NOT_FINISHED,
                field="state",
            )
        if census.unreadable and not allow_unreadable:
            raise WorkLedgerError(
                f"conductor ledger has {census.unreadable} unreadable item record(s); "
                "refusing to purge it",
                code=CODE_LEDGER_NOT_FINISHED,
                field="state",
            )
        if (
            not census.closed
            and not census.unreadable
            and not census.damage
            and (directory / _CONDUCTOR_FILE).exists()
        ):
            # No items at all, and a header: finished-LOOKING, not finished. The
            # same rule the sweep's scanner applies, re-applied here because
            # this is the binding decision -- a caller-built report, or a store
            # whose items vanished between the scan and this hold, must not turn
            # an empty conductor into a purgeable one, torn header or not. A
            # directory with NEITHER header nor items is not that case: it is
            # the residue a purge leaves when a lock file could not be unlinked
            # -- on Windows, a writer queued on the conductor lock holds its
            # handle through the post-release unlink -- and it falls through to
            # the header check below as damage, removable with allow_unreadable.
            raise WorkLedgerError(
                "conductor ledger has no items; an empty conductor is not finished, "
                "refusing to purge it",
                code=CODE_LEDGER_NOT_FINISHED,
                field="state",
            )
        # The header is re-read under the lock too. The scanner lists a store
        # whose header does not read as ``unreadable``, which a plain purge skips;
        # a header that tore between the report and this hold must meet the same
        # refusal here, or the report's "finished" would be trusted over the
        # store's own account of itself.
        damaged_header = header_damage(directory)
        if damaged_header and not allow_unreadable:
            raise WorkLedgerError(
                f"conductor ledger's {damaged_header}; refusing to purge it",
                code=CODE_LEDGER_NOT_FINISHED,
                field="state",
            )
        latest = _newest_activity(directory, census)
        if latest is not None and datetime.now().astimezone() - latest < idle_for:
            raise WorkLedgerError(
                "conductor ledger changed within the retention window; refusing to purge it",
                code=CODE_LEDGER_NOT_FINISHED,
                field="state",
            )
        failures = _remove_contents_locked(directory)
        if failures:
            logger.warning(
                "work ledger purge: %d entr(y/ies) could not be removed; the header and "
                "breadcrumb are kept so the store stays identifiable",
                failures,
            )
            return None
        # Header first, breadcrumb second: a header that will not unlink leaves a
        # store that still names itself, so a later purge can still be aimed at it.
        for name in (_CONDUCTOR_FILE, _KEY_FILE):
            try:
                (directory / name).unlink(missing_ok=True)
            except OSError:
                logger.warning("work ledger purge: %s could not be removed; store kept", name)
                return None
        # Every lock inode goes INSIDE the holds where the OS allows it, so a
        # writer queued on any of them finds its path gone on acquire and
        # refuses; see ``session_ledger.unlink_lock_in_hold``. Windows refuses
        # these and gets them after release from ``_remove_lock_shell``.
        held_item_locks = _unlink_item_locks_in_hold(directory)
        conductor_lock_gone = unlink_lock_in_hold(directory / _LOCK_FILE)
    return conductor_lock_gone, held_item_locks


def _unlink_item_locks_in_hold(directory: Path) -> list[Path]:
    """Unlink every ``items/<stem>.lock`` while :func:`_hold_every_item_lock` holds it.

    Returns the lock paths the OS REFUSED to unlink under the hold (Windows), which
    are the only ones :func:`_remove_lock_shell` may touch afterwards.
    """
    try:
        entries = list((directory / _ITEMS_DIR).iterdir())
    except OSError:
        return []
    return [
        entry
        for entry in entries
        if entry.name.endswith(".lock") and not unlink_lock_in_hold(entry)
    ]


#: The files that make a store a ledger and let a purge NAME it. Removed last,
#: header before breadcrumb, and only when everything else is already gone.
_IDENTITY_FILES = frozenset({_CONDUCTOR_FILE, _KEY_FILE})


def _remove_contents_locked(directory: Path) -> int:
    """Remove *directory*'s contents except the identity files and EVERY lock file.

    Returns how many entries could NOT be removed. Failures are counted, never
    ignored: ``shutil.rmtree(ignore_errors=True)`` would report success over a
    subtree it silently left standing, and the caller's decision to remove the
    header depends on this count being honest.

    Lock files -- the conductor's and every ``items/<stem>.lock`` -- are LEFT
    STANDING by this pass; they are not content, and they are not counted. What
    happens to them next differs by platform. On POSIX the caller unlinks each
    one INSIDE its hold (:func:`_unlink_item_locks_in_hold`,
    ``unlink_lock_in_hold``), so a queued writer acquires a detached inode and
    refuses. On Windows a handle opened with ``os.open`` has no
    ``FILE_SHARE_DELETE``, unlinking a held lock raises, and those paths are
    handed to :func:`_remove_lock_shell` to unlink after every hold is released.
    Either way this pass must not try: counting a refused lock unlink as a
    content failure would keep the header on every Windows purge, and the
    work-half purge could never succeed there. Because of that the ``items/``
    directory is walked file by file rather than ``rmtree``'d: the tree cannot
    go while the locks in it stay. Call under the conductor lock.
    """
    failures = 0

    def _count(_fn: Any, _path: Any, _exc: Any) -> None:
        nonlocal failures
        failures += 1

    try:
        children = list(directory.iterdir())
    except OSError:
        return 1
    for child in children:
        if child.name == _LOCK_FILE or child.name in _IDENTITY_FILES:
            continue
        # A linked entry is unlinked as a NAME, never followed: ``is_dir`` is
        # true through a link to a directory, and walking it would delete the
        # target's files. The caller refuses a linked ``items/`` before it gets
        # here; this is the same rule for every other entry.
        if child.name == _ITEMS_DIR and child.is_dir() and not is_link(child):
            try:
                entries = list(child.iterdir())
            except OSError:
                failures += 1
                continue
            for entry in entries:
                if entry.name.endswith(".lock"):
                    continue
                if entry.is_dir() and not is_link(entry):
                    shutil.rmtree(entry, onerror=_count)
                else:
                    try:
                        entry.unlink()
                    except OSError:
                        failures += 1
            continue
        if child.is_dir() and not is_link(child):
            shutil.rmtree(child, onerror=_count)
        else:
            try:
                child.unlink()
            except OSError:
                failures += 1
    return failures


def _remove_lock_shell(
    directory: Path, *, conductor_lock_gone: bool, item_locks_left: list[Path]
) -> None:
    """Remove the empty directories, and ONLY the lock files the holds could not.

    Call AFTER releasing. A lock path that was unlinked inside its hold is never
    touched again here: a writer that refused on the detached inode may already
    have retried, recreated the store and taken a FRESH lock at that path, and a
    second unlink would detach that fresh inode under its holder -- the next writer
    would take a third, un-serialised against the second. So this function
    receives exactly the paths the OS refused to unlink under the hold (Windows)
    and unlinks those alone, which is safe there because a late unlink fails
    whenever any writer holds a handle. The ``rmdir``s simply fail if a writer
    has rebuilt the store, and that is the correct outcome.
    """
    for lock_path in item_locks_left:
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            logger.debug("work ledger purge: item lock still held; leaving it")
    try:
        (directory / _ITEMS_DIR).rmdir()
    except OSError:
        logger.debug("work ledger purge: items directory not fully removed")
    if not conductor_lock_gone:
        try:
            (directory / _LOCK_FILE).unlink(missing_ok=True)
        except OSError:
            logger.debug("work ledger purge: conductor lock still held; leaving it")
    try:
        directory.rmdir()
    except OSError:
        logger.debug("work ledger purge: ledger directory not fully removed")


# --------------------------------------------------------------------------- #
# Projection. The crew log is the record; the files above are its cache.
# --------------------------------------------------------------------------- #


def rebuild_from_projection(slot_key: str) -> dict[str, Any]:
    """Re-materialise *slot_key*'s ledger files from the crew log's ``work`` fold.

    Every write the routes accept is recorded as one ``work/recorded`` entry in
    the acting session's crew log, so the JSON under ``work-ledger/<conductor>/``
    is a cache of those entries: this rewrites the header, every item and every
    item's event log from the fold, and removes any item file the fold does not
    know, so the cache afterwards holds exactly the recorded board. Every writer
    is held off for the whole operation: the conductor lock (every conductor
    action takes it) and the lock of every item the cache holds (a report takes
    its item's lock and nothing else) are acquired first, then the crew-log writer
    is drained so an entry acknowledged to a caller but still queued is folded,
    then the fold is read and written back -- so no write accepted while the fold
    was read can be overwritten by it. An item the fold CREATES is locked as soon
    as the fold names it, which is after that read and still early enough: no
    writer can take the lock of an item whose record does not exist yet, so the
    window a writer could use opens only once this rebuild writes the file, by
    which time its lock is held. A slot whose fold holds no entry leaves the files
    untouched, so a caller can tell "no entries" from "rebuilt empty".

    Returns the counts: ``{"slot_key", "items", "events", "removed", "legacy"}``.
    """
    # Imported here, not at module scope, on purpose: ``ledger_sweep`` imports this
    # module at boot, so a module-scope import would load the crew log's storage
    # subsystem on a flag-off launch. That is not a style preference -- it is pinned
    # by ``test_crew_log_emit.py::test_a_flag_off_launch_does_not_load_the_storage_subsystem``,
    # which fails when these move up. The ``top-level-imports`` convention is
    # advisory; this boot-path invariant is enforced, so the invariant wins.
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.crew_log.projection import read_slot_projection, work_slots_naming_board
    from kiro_crew.crew_log.store import unprovable_session_units

    directory = items_dir(slot_key)
    with ExitStack() as held:
        held.enter_context(conductor_lock(slot_key, create=True))
        present = sorted(
            path.stem
            for path in (directory.glob("it_*.json") if directory.is_dir() else ())
            if _ITEM_ID_RE.fullmatch(path.stem)
        )
        for item_id in present:
            held.enter_context(item_lock(slot_key, item_id, create=True))
        crew_log_emit.flush(timeout=5.0)
        unprovable = unprovable_session_units()
        if unprovable:
            # A unit whose header cannot be proved might be this board's worker;
            # a fold without it would read as complete and erase what it held.
            raise WorkLedgerError(
                f"{unprovable} crew-log session unit(s) have an unreadable header, so the fold "
                "would be partial; repair or remove those units under the crew log root "
                "(any slot's), then rebuild",
                code=CODE_CREW_LOG_INCOMPLETE,
            )
        # Workers the record's own bind entries may not name (bound before the
        # board was recorded) join the fold from two more places: the cached binding
        # files, and every other slot whose log carries an entry naming this board
        # -- the one source that survives losing the cache and the bindings both.
        extra_slots = list(_bound_workers(slot_key))
        for other in work_slots_naming_board(slot_key):
            if other not in extra_slots:
                extra_slots.append(other)
        folded = read_slot_projection(slot_key, "work", also_slots=extra_slots).value
        header = folded.get("conductor") if isinstance(folded, dict) else None
        if not isinstance(header, dict) or not header.get("entries"):
            # No entry names this board: nothing recorded, so nothing to rebuild
            # from -- and never a reason to remove what the cache holds.
            return {"slot_key": slot_key, "items": 0, "events": 0, "removed": 0}
        # Every file this rebuild may touch is snapshotted first; a failure part
        # way through puts all of them back, so the cache is never left half
        # rewritten: it is the old board or the rebuilt one, nothing between.
        fold_items = [raw for raw in (folded.get("items") or ()) if isinstance(raw, dict)]
        # An item the fold CREATES needs its lock held here, not merely while its
        # own files are written. The write routes take an item's lock alone and
        # never the conductor lock, so a lock released as soon as that item was
        # written leaves a window where a second gateway commits a report against
        # it -- and a not-yet-present item's footprint snapshot is ``None``, so the
        # undo below unlinks that committed write without a trace. Held on
        # ``held``, every affected lock outlives both the rebuild and its undo.
        # Sorted, the order every other multi-item hold here uses, so this cannot
        # deadlock against one of those; deduplicated against ``present``, whose
        # locks are already held, because one process cannot hold a lock twice.
        for new_id in sorted(
            {
                raw["item_id"]
                for raw in fold_items
                if isinstance(raw.get("item_id"), str)
                and raw["item_id"] not in present
                and _ITEM_ID_RE.fullmatch(raw["item_id"])
            }
        ):
            held.enter_context(item_lock(slot_key, new_id, create=True))
        _refuse_fold_behind_cache(slot_key, present, fold_items, header)
        touched = _rebuild_footprint(slot_key, present, fold_items)
        try:
            counts = _rebuild_locked(slot_key, header, fold_items, present)
        except BaseException:
            try:
                _restore_files(touched)  # the locks are already held here
            except Exception:
                # Neither the old board nor the rebuilt one: say so where every
                # reader looks, until a rebuild succeeds.
                mark_cache_dirty(slot_key, "a rebuild failed and its undo failed")
                logger.warning("work ledger rebuild undo failed for %s", slot_key, exc_info=True)
            raise
        clear_cache_dirty(slot_key)
        return counts


def _refuse_fold_behind_cache(
    slot_key: str,
    present: "list[str]",
    fold_items: "list[dict[str, Any]]",
    header: "dict[str, Any] | None" = None,
) -> None:
    """Refuse the rebuild when the record holds LESS than the cache does.

    A unit retention pruned, or one that never made it to disk, leaves the fold
    short of what the cache saw land: an item created after this board's first
    recorded entry that no entry names any more, a cached report later than the
    fold's latest, or more event lines than the fold produced. Rebuilding from
    such a fold would erase work, so it refuses instead and names the item; the
    caller decides what the cache is worth. An item born before that projection
    epoch is compared on its reports only: its earlier history was never recorded,
    by design. The fold's ``first_entry_at`` and the item's own ``created_at`` are
    the epoch signal, matching the legacy-preservation rule in
    :func:`_rebuild_locked`.
    """
    cached_header = read_conductor(slot_key)
    # An ABANDONED write is the only reason the cache can be ahead here: the flag is set
    # solely when an unrecorded write could not be undone, so the cache's surplus is
    # explicitly not authoritative -- the record never saw it and the store already tried
    # to remove it. Refusing the rebuild to protect that surplus protected nothing, and it
    # closed the only exit: every route answers 409 cache_dirty naming a rebuild as the
    # cure, while the rebuild answered 409 crew_log_incomplete because the cache was ahead.
    # The board was locked with no way out. So when the flag stands, prefer the record.
    #
    # The generation guard below is deliberately NOT skipped: a generation mismatch means
    # the slot was reused and the fold answers with a PURGED board, which is not a
    # cache-ahead question and stays refusable however this flag reads.
    abandoned = cache_dirty(slot_key)
    if (
        cached_header is not None
        and not abandoned
        and cached_header.recorded_at
        and header is not None
        and cached_header.goal_version > _as_int(header.get("goal_version"), 0)
    ):
        # The header has the same two-step write as an item: a goal committed to
        # the cache whose entry never landed (the gateway died between the two)
        # leaves the cache one goal ahead of the record, and rebuilding from the
        # record would put the older goal and round back.
        raise WorkLedgerError(
            "the board's goal was written after the last goal the crew log holds; "
            "a unit was pruned or a goal write was never recorded, so the record "
            "cannot rebuild this board -- keep the cache as it stands, or record "
            "the goal again (work_ledger_record action=goal) and rebuild",
            code=CODE_CREW_LOG_INCOMPLETE,
        )
    folded_generation = _as_str((header or {}).get("generation"))
    if (
        cached_header is not None
        and cached_header.generation
        and folded_generation
        and cached_header.generation != folded_generation
    ):
        # A board is stamped with its own generation once, when it is created, so
        # two generations under one slot are two different boards: the slot was
        # reused after the earlier one was purged. A purge cannot take the earlier
        # board's ENTRIES back -- the crew log is append-only -- so the fold still
        # answers with the purged board while the cache holds the live one, and
        # rebuilding would replace the live board with the dead one.
        #
        # Not covered by the goal_version guard above, which is conditioned on
        # ``recorded_at``. That stamp lands only once a goal entry has, so a board
        # whose FIRST goal died between the cache commit and the append has an
        # unstamped header and skips that guard -- the very case it exists for.
        #
        # BOTH generations must be non-empty. A fold carrying none holds no entries
        # for a generation-stamped board, and a cache carrying none is a legacy
        # board from before the field; neither can be confused with a second board,
        # and :func:`_rebuild_locked` already adopts the cache's generation when the
        # record has none.
        raise WorkLedgerError(
            "the board in the cache is not the board the crew log holds: the two "
            "carry different generations, so this slot was reused after an earlier "
            "board was purged and the record still holds that earlier board's "
            "entries. Rebuilding would put the purged board back -- keep the cache "
            "as it stands, or remove this board's cached files and rebuild to "
            "reproduce the recorded board in its place",
            code=CODE_CREW_LOG_INCOMPLETE,
        )
    if abandoned:
        # Past the generation guard, every remaining check below asks whether the CACHE
        # holds more than the fold. With the board flagged, that surplus is the abandoned
        # write itself, so there is nothing left here worth refusing for -- and refusing is
        # what sealed the board. The rebuild proceeds and clears the flag, which is the
        # cure the dirty refusal has been naming all along.
        return
    by_id = {raw["item_id"]: raw for raw in fold_items if isinstance(raw.get("item_id"), str)}
    epoch = _parse_iso(_as_str((header or {}).get("first_entry_at")))
    for item_id in present:
        cached = read_work_item(slot_key, item_id)
        if cached is None:
            continue
        folded = by_id.get(item_id)
        if folded is None:
            created = _parse_iso(cached.created_at)
            post_epoch = (
                epoch is not None
                and created is not None
                and (created.tzinfo is None) == (epoch.tzinfo is None)
                and created > epoch
            )
            if not cached.recorded_at and not post_epoch:
                # A confirmed pre-epoch item is a legacy baseline. An absent or
                # same-second stamp cannot prove the create followed the epoch;
                # the rebuild's existing rule keeps same-second items and removes
                # malformed cache rows, so this guard preserves both outcomes.
                continue
            raise WorkLedgerError(
                f"item {item_id} was created after the board's projection epoch but "
                "no crew-log entry names it now; a unit was pruned or its create "
                "was never recorded, so the record cannot rebuild this board -- keep "
                "the cache as it stands, or remove that item's cached files and "
                "rebuild to reproduce the board without it",
                code=CODE_CREW_LOG_INCOMPLETE,
            )
        if not cached.recorded_at:
            continue
        latest = folded.get("last_report_at")
        if cached.last_report_at and (
            not isinstance(latest, str) or latest < cached.last_report_at
        ):
            raise WorkLedgerError(
                f"item {item_id}'s latest report is not in the crew log; "
                "a unit was pruned, so the record cannot rebuild this board -- keep the "
                "cache as it stands, or remove that item's cached files and rebuild to "
                "reproduce it without the unrecorded report",
                code=CODE_CREW_LOG_INCOMPLETE,
            )
        folded_events = folded.get("events")
        folded_list = folded_events if isinstance(folded_events, list) else []
        cached_list = _read_events_unlocked(item_events_path(slot_key, item_id))
        cached_events = len(cached_list)
        if folded.get("baseline") is True:
            # Born from a baseline: the events before its first recorded mutation
            # were never recorded, by design, so only the tail from that mutation
            # on is compared. The first folded event carries the store's own id,
            # which finds it in the cache; a cached tail longer than the fold
            # means a later mutation's unit is gone.
            first = (
                folded_list[0].get("id")
                if folded_list and isinstance(folded_list[0], dict)
                else None
            )
            start = next((i for i, ev in enumerate(cached_list) if ev.id == first), None)
            if not isinstance(first, str) or start is None:
                continue
            cached_events = len(cached_list) - start
        folded_ids = {ev.get("id") for ev in folded_list if isinstance(ev, dict)}
        # Every comparable cached event is checked by identity, not just the newest.
        # The count below is uninformative once the cache holds its cap (the store
        # keeps the newest cap and the fold the same, so the comparison is 200 > 200),
        # and a pruned unit does not have to be the newest one: a middle event whose
        # unit is gone leaves the count and the latest id both matching, and only its
        # own absence from the fold shows it.
        absent_event: WorkEvent | None = None
        for cached_event in cached_list[len(cached_list) - cached_events :]:
            if cached_event.id not in folded_ids:
                absent_event = cached_event
                break
        if absent_event is not None:
            raise WorkLedgerError(
                f"item {item_id}'s cached event {absent_event.id} is not in the crew log; a "
                "unit was pruned, so the record cannot rebuild this board -- keep the "
                "cache as it stands, or remove that item's cached files and rebuild to "
                "reproduce it without the unrecorded events",
                code=CODE_CREW_LOG_INCOMPLETE,
            )
        if cached_events < MAX_EVENTS_PER_ITEM and cached_events > len(folded_list):
            raise WorkLedgerError(
                f"item {item_id} has {cached_events} cached events but the crew log "
                "folds fewer; a unit was pruned, so the record cannot rebuild this board "
                "-- keep the cache as it stands, or remove that item's cached files and "
                "rebuild to reproduce it without the unrecorded events",
                code=CODE_CREW_LOG_INCOMPLETE,
            )


def mark_cache_dirty(slot_key: str, reason: str) -> None:
    """Flag *slot_key*'s cache as possibly holding what the record does not.

    Written when an undo failed: an unrecorded write that could not be put back,
    or a rebuild that failed part way and could not be restored. Every read and
    write of the board refuses ``cache_dirty`` while the flag stands; a rebuild
    that completes clears it, because the cache is then the record again.
    """
    directory = conductor_dir(slot_key)
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write(directory / _DIRTY_FILE, reason.strip()[:200] + "\n", mode=0o600)


def cache_dirty(slot_key: str) -> "str | None":
    """The reason the cache is flagged dirty, or None when it is not."""
    path = conductor_dir(slot_key) / _DIRTY_FILE
    try:
        return path.read_text(encoding="utf-8").strip() or "unknown"
    except FileNotFoundError:
        return None
    except OSError:
        return "the dirty marker could not be read"


def clear_cache_dirty(slot_key: str) -> None:
    """Drop the flag: the cache was just rebuilt from the record."""
    try:
        (conductor_dir(slot_key) / _DIRTY_FILE).unlink()
    except FileNotFoundError:
        pass


def _bound_workers(slot_key: str) -> "tuple[str, ...]":
    """Every worker whose binding file points at *slot_key*'s board, oldest first."""
    root = bindings_dir()
    if not root.is_dir():
        return ()
    workers: list[str] = []
    for path in sorted(root.glob("*.json")):
        raw = _read_json_record(path, strict=False)
        if not isinstance(raw, dict) or _as_str(raw.get("conductor_slot_key")) != slot_key:
            continue
        worker = _as_str(raw.get("worker_slot_key"))
        if worker and worker not in workers:
            workers.append(worker)
    return tuple(workers)


def _rebuild_footprint(
    slot_key: str, present: "list[str]", fold_items: "list[dict[str, Any]]"
) -> dict[str, bytes | None]:
    """The bytes of every file :func:`rebuild_from_projection` may write or remove."""
    snapshot: dict[str, bytes | None] = {}
    paths: list[Path] = [
        conductor_dir(slot_key) / _CONDUCTOR_FILE,
        conductor_dir(slot_key) / _KEY_FILE,
    ]
    ids = set(present)
    workers: set[str] = set()
    for raw in fold_items:
        item_id = raw.get("item_id")
        if isinstance(item_id, str) and _ITEM_ID_RE.fullmatch(item_id):
            ids.add(item_id)
        worker = raw.get("worker_session_key")
        if isinstance(worker, str) and worker:
            workers.add(worker)
    for item_id in sorted(ids):
        paths += [item_path(slot_key, item_id), item_events_path(slot_key, item_id)]
    root = bindings_dir()
    if root.is_dir():
        for path in sorted(root.glob("*.json")):
            binding = _read_json_record(path, strict=False)
            if isinstance(binding, dict) and _as_str(binding.get("conductor_slot_key")) == slot_key:
                paths.append(path)
    for worker in sorted(workers):
        paths.append(binding_path(worker))
    for path in paths:
        try:
            snapshot[str(path)] = path.read_bytes()
        except FileNotFoundError:
            snapshot[str(path)] = None
    return snapshot


def _rebuild_locked(
    slot_key: str, header: dict[str, Any], fold_items: "list[dict[str, Any]]", present: "list[str]"
) -> dict[str, Any]:
    """The rebuild's writes, run under the locks :func:`rebuild_from_projection` holds."""
    epoch = _parse_iso(_as_str(header.get("first_entry_at")))
    record = ConductorRecord.from_dict(
        {
            k: v
            for k, v in dict(header, slot_key=slot_key).items()
            if k not in ("entries", "first_entry_at")
        }
    )
    existing = read_conductor(slot_key)
    if existing is not None:
        # Header fields the record never saw -- a goal set before the board's
        # first entry, the true creation stamp -- are the cache's to keep.
        if not header.get("goal"):
            record = dataclasses.replace(
                record,
                goal=existing.goal,
                round=max(record.round, existing.round),
                depth=existing.depth,
                parent_item=existing.parent_item,
            )
        if _predates(existing.created_at, _parse_iso(record.created_at)):
            record = dataclasses.replace(record, created_at=existing.created_at)
        if not record.generation and existing.generation:
            record = dataclasses.replace(record, generation=existing.generation)
        if existing.recorded_at:
            record = dataclasses.replace(record, recorded_at=existing.recorded_at)
    if not record.recorded_at and record.goal_version:
        # The fold holds a goal entry, which is what the stamp asserts; a rebuild
        # that left it empty would exempt the header from the next check.
        record = dataclasses.replace(record, recorded_at=_now_iso())
    _write_record(conductor_dir(slot_key) / _CONDUCTOR_FILE, record.to_dict())
    # The identity breadcrumb beside the record, as the bootstrap writes it, so a
    # cache rebuilt into an empty directory carries the same two files a new one does.
    atomic_write(conductor_dir(slot_key) / _KEY_FILE, slot_key + "\n", mode=0o600)

    written_items = 0
    written_events = 0
    kept: set[str] = set()
    for raw in fold_items:
        item = WorkItem.from_dict({key: value for key, value in raw.items() if key != "events"})
        if not item.item_id:
            continue
        # The fold produced this item from entries the log holds whole, which is
        # exactly what ``recorded_at`` asserts. Entries never carry the stamp, so
        # a rebuild that copied the fold verbatim would clear it -- and the next
        # completeness check would skip the item, letting a later pruned unit
        # erase its reports. Keep the cache's stamp when it has one; otherwise
        # this rebuild is the moment the store confirmed the log holds it.
        cached_item = read_work_item(slot_key, item.item_id) if item.item_id in present else None
        if cached_item is not None and cached_item.recorded_at:
            item.recorded_at = cached_item.recorded_at
        else:
            item.recorded_at = _now_iso()
        parsed = (WorkEvent.from_dict(entry) for entry in raw.get("events") or ())
        events = [event for event in parsed if event is not None][-MAX_EVENTS_PER_ITEM:]
        # Every lock these writes need is held by the caller for the whole rebuild
        # AND its undo -- this item's included, whether the cache already held it
        # or the fold creates it -- so nothing is acquired or released here. A
        # second acquire of a lock this process already holds would wait on itself.
        events_path = item_events_path(slot_key, item.item_id)
        lines = (json.dumps(event.to_dict(), ensure_ascii=False) + "\n" for event in events)
        events_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(events_path, "".join(lines), mode=0o600, restrict_to_owner=True, newline="\n")
        _write_record(item_path(slot_key, item.item_id), item.to_dict())
        kept.add(item.item_id)
        written_items += 1
        written_events += len(events)

    removed = 0
    legacy: set[str] = set()
    for item_id in present:
        if item_id in kept:
            continue
        cached = read_work_item(slot_key, item_id)
        if cached is not None and _predates(cached.created_at, epoch):
            # Created before this board's first recorded entry: the log never
            # saw it, so the log cannot judge it. It stays as the cache holds it.
            legacy.add(item_id)
            continue
        item_path(slot_key, item_id).unlink(missing_ok=True)
        item_events_path(slot_key, item_id).unlink(missing_ok=True)
        removed += 1
    _reconcile_bindings(slot_key, fold_items, kept, legacy)
    return {
        "slot_key": slot_key,
        "items": written_items,
        "events": written_events,
        "removed": removed,
        "legacy": len(legacy),
    }


def _predates(created_at: str, epoch: "datetime | None") -> bool:
    """Whether an item stamped *created_at* was made before the board's first entry."""
    if epoch is None:
        return False
    created = _parse_iso(created_at)
    if created is None or (created.tzinfo is None) != (epoch.tzinfo is None):
        return False
    # Stamps are second-resolution, so an item in the SAME second as the first
    # entry cannot be told from one made just before it: it is kept, not judged.
    return created <= epoch


def _reconcile_bindings(
    slot_key: str, folded_items: Any, kept: "set[str]", legacy: "set[str]"
) -> None:
    """Bindings under ``work-ledger/bindings/`` match the recorded board.

    Every recorded OPEN item bound to a worker has that worker's binding pointing
    at it, unless another board holds the worker now; a binding that points at
    this board's item which the record does not bind to that worker is removed.
    A terminal item still names the worker that finished it but does not route
    it (the store lets another board bind a worker whose item is terminal), so its
    binding is kept where it points and never claimed back. Legacy items keep
    theirs.
    """
    wanted: dict[str, str] = {}
    live: set[str] = set()
    for raw in folded_items:
        if not isinstance(raw, dict):
            continue
        worker = raw.get("worker_session_key")
        item_id = raw.get("item_id")
        if isinstance(worker, str) and worker and isinstance(item_id, str) and item_id in kept:
            wanted[worker] = item_id
            if raw.get("state") not in TERMINAL_ITEM_STATES:
                live.add(worker)
    for worker, item_id in wanted.items():
        if worker not in live:
            # A terminal item names the worker that finished it, but it does not
            # route that worker: once the item is terminal the store lets another
            # board bind the worker. A binding still pointing here is kept (below);
            # one that has moved on is not claimed back.
            continue
        with binding_lock(worker):
            current = read_binding(worker)
            if current is not None and current[0] != slot_key:
                # Another board holds this worker now. The store let it bind because
                # this board's item was terminal or unreadable at the time; writing
                # over it would route that board's worker to this one's item.
                continue
            if current != (slot_key, item_id):
                _write_binding(worker, slot_key, item_id)
    root = bindings_dir()
    if not root.is_dir():
        return
    for path in sorted(root.glob("*.json")):
        raw = _read_json_record(path, strict=False)
        if not isinstance(raw, dict):
            continue
        worker = _as_str(raw.get("worker_slot_key"))
        bound_conductor = _as_str(raw.get("conductor_slot_key"))
        bound_item = _as_str(raw.get("item_id"))
        if not worker or bound_conductor != slot_key:
            continue
        if bound_item in legacy or wanted.get(worker) == bound_item:
            continue
        with binding_lock(worker):
            # Re-read under the lock: another board may have rebound this worker
            # since the scan, and its current binding is not ours to remove.
            if read_binding(worker) == (slot_key, bound_item):
                binding_path(worker).unlink(missing_ok=True)


def snapshot_for_write(
    slot_key: str, *, item_id: str | None = None, worker_session_key: str | None = None
) -> dict[str, bytes | None]:
    """The bytes of every ledger file one write can touch, keyed by path.

    Taken by a route BEFORE it asks the store to write, under the board's lock,
    so that a write whose crew-log entry is then not confirmed can be undone
    exactly: the conductor record, the named item's record and event log, and
    the named worker's binding. ``None`` marks a file that did not exist, so the
    restore removes it. A write can create only one item and touch only the
    files named here, so the snapshot is complete for every action.
    """
    paths = [conductor_dir(slot_key) / _CONDUCTOR_FILE, conductor_dir(slot_key) / _KEY_FILE]
    if item_id:
        paths += [item_path(slot_key, item_id), item_events_path(slot_key, item_id)]
    if isinstance(worker_session_key, str) and worker_session_key:
        # The same spelling `bind` stores under: `_require_text` keeps the key as
        # given, so the path is derived from the request's key unchanged.
        paths.append(binding_path(worker_session_key))
    snapshot: dict[str, bytes | None] = {}
    for path in paths:
        try:
            snapshot[str(path)] = path.read_bytes()
        except FileNotFoundError:
            snapshot[str(path)] = None
    return snapshot


def current_bytes(snapshot: dict[str, bytes | None]) -> dict[str, bytes | None]:
    """What the files *snapshot* names hold NOW, keyed the way it keys them.

    Read by a route between its store commit and its undo, so the undo can tell
    the bytes its own write left behind from bytes a second gateway on the same
    store has since committed. ``None`` marks a file that is absent.
    """
    return {raw: _read_or_none(Path(raw)) for raw in snapshot}


def _read_or_none(path: Path) -> bytes | None:
    """The file's bytes, or None when it is absent or cannot be read.

    Total on purpose: an unreadable file reads as None on both sides of the
    comparison below, which leaves the plain restore in place rather than
    abandoning an undo over a file the process cannot open.
    """
    try:
        return path.read_bytes()
    except OSError:
        return None


def _written_by_another(raw: str, expected: dict[str, bytes | None] | None) -> bool:
    """True when *raw* holds neither the bytes the undoing write left nor nothing.

    The store's per-file locks serialise writers, so they cannot tear a record,
    but they do not order them: a second gateway that commits between this
    write's commit and its undo holds a record these older bytes have no claim
    on. Comparing against what this write left is what tells the two apart. A
    path *expected* does not name counts as this write's own, which keeps a
    caller that passes nothing on the plain restore.
    """
    if expected is None or raw not in expected:
        return False
    return _read_or_none(Path(raw)) != expected[raw]


def restore_snapshot(
    slot_key: str,
    snapshot: dict[str, bytes | None],
    *,
    created_item: str | None = None,
    item_id: str | None = None,
    worker_session_key: str | None = None,
    expected: dict[str, bytes | None] | None = None,
) -> None:
    """Put every file in *snapshot* back as it was, and remove a created item.

    The undo for a write whose crew-log entry was not confirmed: the cache must
    not keep a mutation the record never saw, whatever the board's history --
    a board from before the projection is undone the same way as any other,
    because this needs no fold, only the bytes the write replaced.

    Holds every lock the writers of these files take, in the module's lock
    order: the conductor lock, then each named item's lock, then the worker's
    binding lock. Pass the SAME ``item_id`` and ``worker_session_key`` the
    snapshot was taken with. The restore rewrites an existing item's record and
    event log and a worker's binding, and the route that took the snapshot has
    released those locks by the time this runs, so without them a writer
    admitted in the gap has its record replaced mid-write -- a torn record on
    POSIX, a refused open on Windows. The dashboard's board lock does not close
    that gap: it serialises one process, and these file locks are what a second
    gateway on the same store obeys.

    Locks bound the tearing, not the ordering: a writer that COMPLETES in that
    gap holds bytes this undo has no claim on. Pass *expected* -- what this
    write left in each of these files, read by the caller between its commit and
    this undo -- and any file holding something else is left alone and the cache
    flagged dirty, so the rebuild that reconciles the two is triggered rather
    than merely available. Called without *expected*, the older bytes win, which
    leaves the cache behind the record rather than ahead of it -- the direction a
    rebuild converges.

    The conductor lock is taken NON-CREATING, and a missing one ends the undo
    having written nothing: a store purged between the write and here holds no
    mutation to take back, and creating its directory to hold a lock would
    rebuild the board an operator deleted. Under that hold the store provably
    exists -- the purge takes the same lock -- so the item locks below may
    create their files, the reasoning ``rebuild_from_crew_log`` uses.
    """
    with ExitStack() as held:
        try:
            held.enter_context(conductor_lock(slot_key, create=False))
        except FileNotFoundError:
            return
        created = created_item if created_item and _ITEM_ID_RE.fullmatch(created_item) else None
        named = {created} if created else set()
        if item_id and _ITEM_ID_RE.fullmatch(item_id):
            named.add(item_id)
        for locked_item in sorted(named):
            # Sorted, the order every other multi-item hold here uses, so this
            # cannot deadlock against one of those. Deduplicated because a
            # create names the same id twice, and two acquires of one lock file
            # from one process wait on each other forever.
            held.enter_context(item_lock(slot_key, locked_item, create=True))
        if isinstance(worker_session_key, str) and worker_session_key:
            held.enter_context(binding_lock(worker_session_key))
        passed_over: list[str] = []
        if created:
            for path in (item_path(slot_key, created), item_events_path(slot_key, created)):
                # A create becomes visible to the other gateway the moment it
                # commits, so its files can carry that gateway's later write.
                if _written_by_another(str(path), expected):
                    passed_over.append(str(path))
                    continue
                path.unlink(missing_ok=True)
        passed_over += _restore_files(snapshot, expected)
        if passed_over:
            mark_cache_dirty(
                slot_key,
                "an unrecorded write could not be undone whole: another writer holds "
                f"{len(passed_over)} of its files, so a rebuild reconciles the two",
            )


def _restore_files(
    snapshot: dict[str, bytes | None], expected: dict[str, bytes | None] | None = None
) -> list[str]:
    """Write every snapshotted file back, removing those that did not exist.

    A file whose directory has gone is SKIPPED, not recreated: every snapshotted
    file that held bytes had its directory at snapshot time too, so a missing one
    means the store was removed afterwards, and rebuilding the tree around it
    would resurrect a purged board instead of undoing a write.

    A file holding bytes *expected* does not account for is skipped as well, and
    its path returned: another writer committed after the write being undone, and
    replacing its record with these older bytes would drop an acknowledged write.
    """
    passed_over: list[str] = []
    for raw, data in snapshot.items():
        path = Path(raw)
        if _written_by_another(raw, expected):
            passed_over.append(raw)
            continue
        if data is None:
            path.unlink(missing_ok=True)
            continue
        if not path.parent.is_dir():
            continue
        atomic_write(path, data, mode=0o600, restrict_to_owner=True)
    return passed_over


def mark_goal_recorded(slot_key: str) -> None:
    """Stamp the header as held by the crew log, so a rebuild checks its goal writes.

    Called by the write route once a ``goal`` entry has landed. A failure here is
    harmless: the header is checked from the first stamp that does land.
    """
    with _existing_conductor_lock(slot_key):
        record = read_conductor(slot_key)
        if record is None or record.recorded_at:
            return
        record.recorded_at = _now_iso()
        _write_record(conductor_dir(slot_key) / _CONDUCTOR_FILE, record.to_dict())


def mark_item_recorded(slot_key: str, item_id: str) -> None:
    """Stamp *item_id* as held whole by the crew log, so later entries carry deltas only.

    Called by a write route once the entry that carried the whole item (a create,
    or a baseline) has landed. A failure here is harmless: the next mutation
    simply carries the whole item again.
    """
    checked_id = _require_item_id(item_id)
    with item_lock(slot_key, checked_id, create=False):
        item = read_work_item(slot_key, checked_id)
        if item is None or item.recorded_at:
            return
        item.recorded_at = _now_iso()
        _write_record(item_path(slot_key, checked_id), item.to_dict())
