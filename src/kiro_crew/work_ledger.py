"""The shared work record between a conductor session and the workers it dispatched.

Three ledgers now carry that name, and they are not interchangeable.
:mod:`kiro_crew.session_ledger` is ONE session's own durable state. Issue Radar's
``crew_store`` is a per-repository work ledger keyed by a forge issue number. This
module is the third: a record two parties write and neither owns, so that a
conductor learns what a worker did as DATA instead of reading its transcript.

Phase 1 of the conductor-work-ledger RFC (pull request #8842) — storage only.
No MCP tool, no HTTP route, no UI, and deliberately no importer anywhere else in the
tree, so the phase reverts by deleting this file and its test.

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

import hashlib
import json
import logging
import re
import secrets
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home
from kiro_crew.platform_compat import file_lock
from kiro_crew.session_ledger import _store_name

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# --------------------------------------------------------------------------- #
# Vocabularies
# --------------------------------------------------------------------------- #

#: A work item's disposition, written by the conductor. A DIFFERENT question from
#: ``verdict``: an item may hold ``verdict: fail`` and stay ``open`` while the
#: worker retries, which is the state ``ledger_entry.py`` encodes today as "fails
#: incremented but still running".
ITEM_STATES: frozenset[str] = frozenset({"open", "accepted", "rejected", "abandoned"})

#: States from which no further write is accepted.
TERMINAL_ITEM_STATES: frozenset[str] = frozenset({"accepted", "rejected", "abandoned"})

#: ``accept_eval.py``'s own five values, reused rather than paralleled, so a verdict
#: crosses from that script into this store with no translation.
VERDICTS: frozenset[str] = frozenset({"pass", "fail", "pending", "refused", "error"})

#: What a worker may say about itself. ``blocked`` and ``question`` are separate
#: because they differ in WHO must act: an external dependency versus the conductor.
WORKER_STATUSES: frozenset[str] = frozenset({"progress", "done", "blocked", "question"})

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
CODE_DEPTH_EXCEEDED = "depth_exceeded"
CODE_FIELD_TOO_LONG = "field_too_long"
CODE_INVALID_ACTION = "invalid_action"
CODE_INVALID_STATUS = "invalid_status"
CODE_INVALID_VALUE = "invalid_value"


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "slot_key": self.slot_key,
            "goal": self.goal,
            "round": self.round,
            "depth": self.depth,
            "parent_item": self.parent_item,
            "created_at": self.created_at,
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
#: imports only the three modules this one already imports, so the leaf-module
#: argument that justifies ``crew_chat``'s own copy does not apply here.

#: The only shape an item id may have — ``it_`` plus the eight hex chars
#: :func:`mint_item_id` produces.
_ITEM_ID_RE = re.compile(r"^it_[0-9a-f]{8}$")

_CONDUCTOR_FILE = "conductor.json"
_KEY_FILE = "slot_key"
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


def conductor_dir(slot_key: str) -> Path:
    """The directory holding *slot_key*'s ledger. Does not create it."""
    if not slot_key or "\0" in slot_key or "/" in slot_key or "\\" in slot_key:
        raise WorkLedgerError(
            f"invalid slot key for work ledger: {slot_key!r}", code=CODE_INVALID_VALUE
        )
    base = _work_ledger_root()
    resolved = (base / _store_name(slot_key)).resolve()
    parent = base.resolve()
    if resolved == parent or not resolved.is_relative_to(parent):
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
    base = bindings_dir()
    resolved = (base / f"{_store_name(worker_slot_key)}.json").resolve()
    if not resolved.is_relative_to(base.resolve()):
        raise WorkLedgerError(
            f"path traversal blocked for worker key: {worker_slot_key!r}",
            code=CODE_INVALID_VALUE,
        )
    return resolved


# --------------------------------------------------------------------------- #
# Locks
# --------------------------------------------------------------------------- #


@contextmanager
def _open_lock(path: Path) -> Iterator[None]:
    """Hold an advisory lock on *path*, creating the lock file if absent.

    ``file_lock`` takes an already-open descriptor and fails CLOSED — it raises
    rather than entering the critical section unserialised — which is why nothing
    here has a lock-less fallback.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as handle:
        with file_lock(handle.fileno(), exclusive=True):
            yield


@contextmanager
def conductor_lock(slot_key: str) -> Iterator[None]:
    """Hold the conductor lock. FIRST in the lock order; see the module docstring."""
    with _open_lock(conductor_dir(slot_key) / _LOCK_FILE):
        yield


@contextmanager
def item_lock(slot_key: str, item_id: str) -> Iterator[None]:
    """Hold one item's lock. SECOND in the lock order; see the module docstring."""
    with _open_lock(_item_lock_path(slot_key, item_id)):
        yield


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
    """
    try:
        if path.stat().st_size > MAX_RECORD_BYTES:
            logger.warning(
                "work ledger record over the size ceiling; treating as absent: %s",
                path.name,
            )
            return None
        return json.loads(path.read_text(encoding="utf-8"))
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
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, _serialize(payload), mode=0o600, restrict_to_owner=True)


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

    Pipe-separated rather than concatenated, matching Issue Radar's ``_event_id``:
    bare concatenation lets two different tuples produce one string, so two distinct
    events could collapse into each other on read.

    ``status`` IS part of the identity. Timestamps are seconds-precision, so a
    ``progress`` report and a ``blocked`` report with the same summary in the same
    second would otherwise share an id, and first-seen-wins dedupe would drop the
    transition — the one line the conductor most needs to see. ``None`` renders as
    the empty string, so every non-report kind keeps a stable formula.
    """
    raw = f"{ts}|{item_id}|{kind}|{status or ''}|{text}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


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

    A terminal item is never stale — there is nothing left to report. An item with no
    report yet is measured from ``created_at``, which is what makes the bind-to-first-
    report gap visible. An unparseable timestamp reads as stale, because the
    alternative is an item that can never be flagged.
    """
    if item.is_terminal or worker_running:
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
    with conductor_lock(slot_key):
        current = read_conductor(slot_key, strict=True) or record
        if checked_goal is not None:
            current.goal = checked_goal
        if checked_round is not None:
            current.round = checked_round
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
    with conductor_lock(slot_key):
        # The default round comes from the header re-read INSIDE the lock, not
        # from the pre-lock snapshot, so a concurrent ``goal`` round bump is seen.
        if checked_round is None:
            live = read_conductor(slot_key)
            checked_round = live.round if live is not None else record.round
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

    with item_lock(slot_key, checked_id):
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

    with item_lock(slot_key, checked_id):
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
    """
    return {
        "items": [
            # ``id`` / ``accept`` are the keys accept_eval.py reads; ``item_id`` /
            # ``acceptance`` are this store's field names. The rename happens here,
            # at the one seam between the two, so neither side learns the other's
            # vocabulary.
            {"id": item.item_id, "accept": item.acceptance}
            for item in items
            if not item.is_terminal and item.acceptance
        ]
    }
