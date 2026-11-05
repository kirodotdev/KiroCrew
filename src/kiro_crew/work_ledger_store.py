"""Where the conductor work ledger lives on disk, how it is locked, and how it is read.

Item ids and the per-conductor layout (header, key file, items, bindings); the three file
locks and their lock order's primitives (``conductor_lock``, ``item_lock``,
``binding_lock``); the bounded record reads that read corruption as absent and the record
write; the per-item event log with its content-addressed event ids; and the one commit that
writes an item and its event together. :mod:`kiro_crew.work_ledger` binds every name
defined here, so each one reads, imports and patches as ``kiro_crew.work_ledger.<name>``.
"""

from __future__ import annotations

import json
import re
import secrets
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from kiro_crew.atomic_write import atomic_write, read_bytes_with_retry
from kiro_crew.config.paths import data_home
from kiro_crew.platform_compat import file_lock, open_lock_file
from kiro_crew.session_ledger import _store_name, require_lock_inode, resolved_within
from kiro_crew.work_ledger_records import (
    CODE_FIELD_TOO_LONG,
    CODE_INVALID_VALUE,
    CODE_NO_LEDGER,
    CODE_UNKNOWN_ITEM,
    EVENT_KINDS,
    MAX_EVENT_TEXT_CHARS,
    MAX_EVENTS_PER_ITEM,
    MAX_RECORD_BYTES,
    ConductorRecord,
    WorkEvent,
    WorkItem,
    WorkLedgerError,
    _now_iso,
    logger,
)
from kiro_crew.work_vocab import work_event_id

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
#: Per-worker records of a worker-tool call the harness never delivered
#: (:mod:`kiro_crew.work_ledger_undelivered`). Not a ledger: a gateway observation
#: kept beside it.
_UNDELIVERED_DIR = "undelivered"
#: The directories under the work-ledger root that are NOT a conductor's store, so a
#: sweep or a purge walking the root must step over them.
#: Per-item records of what the hub last learnt about a REMOTE worker's slot
#: (:mod:`kiro_crew.work_ledger_remote`). A gateway observation, like the above.
_REMOTE_DIR = "remote"
RESERVED_ROOT_DIRS: frozenset[str] = frozenset({_BINDINGS_DIR, _UNDELIVERED_DIR, _REMOTE_DIR})


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


def undelivered_dir() -> Path:
    return _work_ledger_root() / _UNDELIVERED_DIR


def undelivered_path(worker_slot_key: str) -> Path:
    """Where a worker's undelivered-call record lives, named like its binding."""
    if not worker_slot_key or "\0" in worker_slot_key:
        raise WorkLedgerError(
            f"invalid worker slot key: {worker_slot_key!r}", code=CODE_INVALID_VALUE
        )
    resolved = resolved_within(undelivered_dir(), f"{_store_name(worker_slot_key)}.json")
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

    The lock file is opened through ``platform_compat.open_lock_file``: writable,
    and never truncated, which is what keeps a contending acquirer on Windows
    from crashing before it reaches ``file_lock``. Its docstring carries the
    reasoning.
    """
    # ``create=False`` is the purge's form: a deleter must not bring a store
    # into being by locking it, or a second sweep racing the first recreates the
    # directory the first just removed. The open then raises ``FileNotFoundError``
    # for a store that is gone, and the caller skips it.
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    with open_lock_file(path, create=create) as fd:
        with file_lock(fd, exclusive=True):
            # A purge may have removed this store while the acquire waited. The
            # kernel still grants the lock on the detached inode, so without this
            # the queued writer would publish a torn store into a purged key. See
            # ``session_ledger.require_lock_inode``; the raise is the same
            # ``OSError`` a held lock produces, and the caller retries.
            require_lock_inode(fd, path)
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
        # Created through the same opener as every other ledger lock, so the
        # restored file has the same owner-only mode.
        with open_lock_file(lock_path):
            pass


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


def forget_undelivered_for_item(
    worker_slot_key: str, conductor_slot_key: str, item_id: str
) -> None:
    """Delete *worker_slot_key*'s undelivered-call record when it names this item,
    which has closed. A record for another item is left alone. Never raises.

    Read under the binding lock, the lock a note writes under, so a note that read
    the item as open just before it closed cannot write after this looked."""
    try:
        path = undelivered_path(worker_slot_key)
        with binding_lock(worker_slot_key):
            raw = _read_json_record(path)
            if (
                not isinstance(raw, dict)
                or raw.get("item_id") != item_id
                or raw.get("conductor_slot_key") != conductor_slot_key
            ):
                return
            path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 - a record left behind reads as absent once closed
        logger.debug("could not drop an undelivered-call record", exc_info=True)


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
    except (ValueError, UnicodeDecodeError, RecursionError):
        # ``ValueError`` already covers ``json.JSONDecodeError``. A record nested
        # deeper than the parser follows (a 20 KB file of ten thousand nested
        # arrays is well under the ceiling) raises ``RecursionError`` from inside
        # ``json.loads``, and reads the same way: content this reader cannot trust.
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


def _stored_item_ids(slot_key: str) -> list[str]:
    """The id of every item RECORD on the board, sorted, whether or not it reads.

    The roster of files a board holds, where :func:`list_work_items` is the roster
    of items that read. :func:`rebuild_from_projection` locks and snapshots every
    record by this list, a torn one included, and :func:`_create_item` seeds a
    header that predates :attr:`ConductorRecord.created_total` from it, once. It
    is NOT the board's create count: a directory listing goes down when a record
    is removed, and the fold's count of creates never does. Only a full
    ``it_<8 hex>`` stem is a record, as in the listing: a stray ``it_bad.json``
    was never minted here and has no create in the fold. Lock-free, like the
    listing; both callers hold the conductor lock.
    """
    directory = items_dir(slot_key)
    return sorted(
        path.stem
        for path in (directory.glob("it_*.json") if directory.is_dir() else ())
        if _ITEM_ID_RE.fullmatch(path.stem)
    )


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
        except (ValueError, RecursionError):
            # ``ValueError`` covers ``JSONDecodeError`` and the parser's other
            # refusals (an integer past the interpreter's digit limit is one). A
            # line nested deeper than the parser follows is as unreadable as a torn
            # one. One bad line must not cost the rest of the log.
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
    slot_key: str,
    item_id: str,
    kind: str,
    text: str,
    *,
    status: str | None = None,
    id_salt: str = "",
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
        # *id_salt* tells apart two events the same second with the same text: an
        # ingested remote report passes the peer's own event identity.
        id=event_id(ts, item_id, kind, trimmed + id_salt, status=status),
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


def _require_record_fits(payload: dict[str, Any]) -> None:
    """Refuse with ``CODE_FIELD_TOO_LONG`` when *payload*'s stored form would exceed
    the read ceiling.

    Measured BEFORE any write. ``_require_acceptance`` bounds the compact form, but
    the stored form is indented, and an item that writes successfully and then
    reads as absent is the silent loss this module exists to refuse.
    """
    if len(_serialize(payload).encode("utf-8")) > MAX_RECORD_BYTES:
        raise WorkLedgerError(
            "item record would exceed the read ceiling; shrink acceptance",
            code=CODE_FIELD_TOO_LONG,
            field="acceptance",
        )


def _commit_item_locked(
    slot_key: str,
    item: WorkItem,
    kind: str,
    text: str,
    *,
    status: str | None = None,
    id_salt: str = "",
) -> WorkEvent:
    """Persist one item change AND the event that explains it. HOLD THE ITEM LOCK.

    Two files, no atomic two-file rename, so the order and the rollback are what make
    the pair safe. EVENT FIRST, ITEM SECOND: if the item write then fails, the log is
    put back exactly as it was, so the two never disagree. The other order would let
    a disk-full between the writes persist a state change with no line explaining
    it -- a terminal item with no ``close`` event, forever.

    The serialized item is measured against the read ceiling before either write
    (:func:`_require_record_fits`).
    """
    payload = item.to_dict()
    _require_record_fits(payload)
    events_path = item_events_path(slot_key, item.item_id)
    log_before = _read_text_or_none(events_path)
    event = _append_event_locked(slot_key, item.item_id, kind, text, status=status, id_salt=id_salt)
    try:
        _write_record(item_path(slot_key, item.item_id), payload)
    except BaseException:
        _restore_text(events_path, log_before)
        raise
    return event
