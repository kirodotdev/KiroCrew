"""The append-only ledger store: one file per unit, the gateway the only writer.

Layout, resolved against the live data home on every call (never captured at
import, so pod isolation and test isolation both keep working)::

    <data home>/ledgers/crews/<store name>/ledger.jsonl
    <data home>/ledgers/sessions/<store name>/ledger.jsonl

``<store name>`` is the readable-plus-digest fold of the unit id that
``session_ledger`` and ``work_ledger`` already use, and the raw id lives in the
header (see :func:`ledger_dir` for why the id is not the directory name). Both
files carry a ``.lock`` sibling in the same directory.

One dedicated ``ledgers`` root, holding every kind, is what carries the
protection: the leaf is hidden from a sandboxed process by the OS and fenced from
the agent's own file tools, and both fences are stated per leaf, so every kind
under this root inherits them and a new kind cannot be left out by omission. A
record a conductor is meant to trust as authority must not be forgeable by
anything that can call ``open()``, and a tool gate does not answer a subprocess --
which is why the session half does NOT live under the ``sessions`` transcript
root it would otherwise have shared.

Three properties are the whole design.

**Append only.** A line, once written, is never rewritten. There is exactly ONE
mutation: on ``open``, trailing bytes that are not a complete line are dropped.
Everything else -- a damaged interior line, an unknown envelope key, a type from
a newer writer -- is handled on the READ side by skipping or ignoring, never by
repairing the file. So two readers of the same bytes always agree, and a reader
is never the thing that changes history.

**Torn is decided by termination, not by taste.** Every append writes
``line + "\\n"`` and fsyncs, so a file that does not end in a newline was
interrupted mid-write. Those trailing bytes are the crash artifact and are
truncated -- unless they happen to parse whole, in which case only the newline
was lost: the record is kept and the next append re-supplies the separator. A
line that IS newline-terminated but does not parse is damage *inside* history,
so it is skipped on read and left on disk. Nothing else can be torn, which is
why this rule needs no heuristics.

**Seq comes from the file, under the lock.** ``seq`` is read back from the tail
inside the critical section on every append rather than trusted from the
in-process cache, so two writers cannot both believe they own the same number.
The read is a bounded window at the end of the file (``_TAIL_WINDOW``), not a
scan, so it costs the same on a ledger with ten lines and one with a million.
``.last_seq`` serves the cached value for callers that only want to look.
"""

from __future__ import annotations

import json
import logging
import os
import time
import weakref
from collections import deque
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home
from kiro_crew.jsonl_util import (
    UnreadableRecord,
    bounded_raw_records,
    strict_raw_records,
)
from kiro_crew.ledger.errors import (
    CODE_ALREADY_EXISTS,
    CODE_BAD_DATA,
    CODE_BAD_HEADER,
    CODE_BAD_ROOT,
    CODE_BAD_SEGMENT,
    CODE_BAD_THREAD,
    CODE_INVALID_ID,
    CODE_NO_LEDGER,
    CODE_SEGMENT_GAP,
    CODE_UNKNOWN_ENTRY_TYPE,
    IndeterminateAppend,
    LedgerError,
)
from kiro_crew.ledger.lease import LEASE_FILE
from kiro_crew.ledger.lease import acquire as acquire_lease
from kiro_crew.ledger.lease import release as release_lease
from kiro_crew.ledger.schema import (
    KIND_CREW,
    KIND_SESSION,
    MAX_ENTRY_BYTES,
    Entry,
    Header,
    Ref,
    build_header,
    check_ownership,
    parse_header,
    require_data,
    require_entry_line,
    require_kind,
    require_unit_id,
    serialize,
)
from kiro_crew.platform_compat import file_lock, restrict_dir_to_owner
from kiro_crew.session_ledger import _store_name, resolved_within

logger = logging.getLogger(__name__)

LEDGER_FILE = "ledger.jsonl"

#: How a segment past the first is named and found. ``ledger.jsonl`` holds seq 1
#: onward; ``ledger.<first_seq>.jsonl`` is a later segment.
_SEGMENT_STEM = "ledger"
_SEGMENT_SUFFIX = ".jsonl"
_SEGMENT_GLOB = f"{_SEGMENT_STEM}.*{_SEGMENT_SUFFIX}"
_LOCK_FILE = ".lock"

#: The one data-home leaf that holds every ledger, of every kind.
#:
#: A dedicated root rather than a subdirectory of each unit's existing home,
#: because the protection is what makes the record an authority: this leaf is
#: hidden from a sandboxed process by the OS (``sandbox._CREW_HIDDEN_LEAVES``)
#: and fenced from the agent's own file tools (``security.paths``), and both are
#: stated per LEAF. The ``sessions`` transcript root carries NEITHER entry, so a
#: session ledger placed there is protected by the tool gate alone and a
#: sandboxed subprocess can open it directly and forge the history a conductor
#: reads as fact. One root also means one entry per fence instead of one per
#: kind, so a third unit kind inherits the protection rather than needing a
#: reviewer to notice it was left out.
_ROOT_LEAF = "ledgers"

#: Directory under :data:`_ROOT_LEAF` that holds each kind's units.
_ROOT_DIR: dict[str, str] = {KIND_CREW: "crews", KIND_SESSION: "sessions"}

#: How much of the file's end a tail read covers. One maximum-size entry plus
#: slack, so the newest complete line is inside the window even when it is the
#: largest line the format allows.
_TAIL_WINDOW = 64 * 1024 + 8 * 1024

#: Largest page a single read may materialize.
MAX_PAGE_LIMIT = 500
DEFAULT_PAGE_LIMIT = 50

#: ``resolve`` outcomes. There is no ``forbidden``: this layer claims no
#: authorization, so it has none to deny (see :meth:`Ledger.resolve`).
STATUS_OK = "ok"
STATUS_GONE = "gone"
#: The cited span reaches BELOW the oldest surviving segment: retention removed
#: it. A normal answer -- the citing entry stays honest about having pointed there.
STATUS_PRUNED = "pruned"
#: The cited span lies inside a segment that still exists, but lines in it could
#: not be read. That is DAMAGE, not retention, and it must never be reported as
#: either ``gone`` or ``pruned``: a reader told "retention" stops looking, while a
#: reader told "corrupt" knows the file it still has is not intact.
STATUS_CORRUPT = "corrupt"


def now_ms() -> int:
    """Epoch milliseconds, the clock every ``time`` and ``createdAt`` uses."""
    return int(time.time() * 1000)


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #


def ledger_root(kind: str) -> Path:
    """Root directory holding every ledger of *kind*."""
    return data_home() / _ROOT_LEAF / _ROOT_DIR[require_kind(kind)]


def _checked_ledger_root(kind: str) -> Path:
    """:func:`ledger_root` for *kind*, refused when the directory is not the real one.

    Containment (:func:`resolved_within`) resolves its BASE first and then checks
    only that the child stays under the resolved base. That is the right rule for
    a child, and it is why the base itself has to be established separately: a
    linked kind root makes the link's target the containment root, so every path
    beneath it passes the check while living outside the tree whose protections
    the data home establishes -- readable, and writable by whoever owns the
    target.

    Two conditions, both cheap and both checked on every call rather than cached,
    because the directory can be replaced between calls by anything that can
    write the parent:

    * the kind directory is not itself a link -- ``lstat`` rather than ``stat``,
      so the answer is about the name and not about what it points at;
    * it resolves to exactly the canonical spelling under the data home's own
      ``ledgers`` tree, with both sides resolved so one symlinked ancestor cannot
      make two spellings of the same directory disagree.

    An absent directory is fine and is not an error: the callers that write
    create it, and the ones that read report their own absence. Only a directory
    that EXISTS and is wrong is refused.
    """
    root = ledger_root(kind)
    try:
        is_link = root.is_symlink()
    except OSError as exc:  # pragma: no cover -- a stat fault on the parent
        raise LedgerError(
            f"cannot establish the ledger root {root}: {exc}",
            code=CODE_BAD_ROOT,
        ) from exc
    if is_link:
        raise LedgerError(
            f"refusing a linked ledger root: {root} is a symbolic link",
            code=CODE_BAD_ROOT,
        )
    if not root.exists():
        return root
    try:
        resolved = root.resolve()
        # Anchored at the data home, NOT at ``<home>/ledgers`` resolved: resolving
        # the parent would accept a linked ``ledgers`` -- both sides would then
        # spell the link's target and agree, which is the same escape one level up.
        canonical = data_home().resolve() / _ROOT_LEAF / _ROOT_DIR[require_kind(kind)]
    except (OSError, RuntimeError) as exc:
        raise LedgerError(
            f"cannot resolve the ledger root {root}: {exc}",
            code=CODE_BAD_ROOT,
        ) from exc
    if resolved != canonical:
        raise LedgerError(
            f"refusing a ledger root outside the data home: {root} resolves to {resolved}",
            code=CODE_BAD_ROOT,
        )
    return root


def ledger_dir(kind: str, unit_id: str) -> Path:
    """The validated directory for one unit's ledger. Does not create it.

    The directory is named with the readable-plus-digest fold
    (``session_ledger._store_name``) rather than the raw id, and the raw id is
    persisted in the header instead. Two reasons, and the second is the load
    bearing one:

    * A legitimate id is not always a legitimate FILENAME. A channel session key
      carries a colon (``slack:1712793600.123``), which POSIX accepts and Windows
      refuses, so the raw id as a directory name turns a sanctioned id into an
      ``OSError`` on a supported platform.
    * Identity is the DIGEST over the exact id, so ``Foo`` and ``foo`` get
      distinct directories on a case-insensitive filesystem and two long ids
      sharing a prefix cannot land in one file. The readable half is a
      convenience for a human reading the directory listing and is capped for
      filesystem name limits; it is not the identity, and the fold is not
      reversible -- ``open`` proves it reached the right unit by checking the id
      the header stores.

    Containment is what makes the path safe regardless: the shape gate refuses a
    separator or a NUL in the raw id, and ``resolved_within`` re-checks
    symlink-safely that the resolved path stays under the root.
    """
    require_unit_id(unit_id)
    resolved = resolved_within(_checked_ledger_root(kind), _store_name(unit_id))
    if resolved is None:
        raise LedgerError(
            f"path traversal blocked for ledger id: {unit_id!r}",
            code=CODE_INVALID_ID,
            field="id",
        )
    return resolved


def ledger_path(kind: str, unit_id: str) -> Path:
    """The ledger file for one unit."""
    return ledger_dir(kind, unit_id) / LEDGER_FILE


def _segment_first_seq(path: Path) -> int | None:
    """The first seq declared by a canonical segment filename, or ``None``."""
    if path.name == LEDGER_FILE:
        return 1
    prefix = f"{_SEGMENT_STEM}."
    if not path.name.startswith(prefix) or not path.name.endswith(_SEGMENT_SUFFIX):
        return None
    middle = path.name[len(prefix) : -len(_SEGMENT_SUFFIX)]
    if not middle.isdigit() or int(middle) <= 1:
        return None
    return int(middle)


def segment_paths(kind: str, unit_id: str) -> list[Path]:
    """Every segment of one unit's ledger, in ascending first-seq order.

    Retention is deleting whole segments off the FRONT, which is why the format
    is segments rather than one growing file: dropping the oldest lines out of a
    single file would rewrite it, and this store's whole guarantee is that a
    written line is never rewritten. Deleting a segment leaves every remaining
    line byte-identical, so retention costs a reader the old entries and costs
    the format nothing.

    ``ledger.jsonl`` is the segment that starts at seq 1 and is the only one a
    writer creates today; a later segment is ``ledger.<first_seq>.jsonl``. The
    first-seq is IN the name so ordering needs no file read, and so a reader can
    tell a gap at the front (retention) from a gap in the middle (damage).
    """
    directory = ledger_dir(kind, unit_id)
    found: list[tuple[int, Path]] = []
    head = directory / LEDGER_FILE
    if head.is_file():
        found.append((1, head))
    for candidate in directory.glob(_SEGMENT_GLOB):
        first = _segment_first_seq(candidate)
        if first is None:
            # Not a segment: a neighbour that merely shares the prefix. Ignored
            # rather than refused, so an editor's backup file cannot make a
            # readable ledger unreadable.
            continue
        found.append((first, candidate))
    found.sort(key=lambda pair: pair[0])
    return [path for _first, path in found]


def segment_first_seqs(kind: str, unit_id: str) -> list[int]:
    """The first seq of every surviving segment, ascending.

    The companion to :func:`segment_paths`, and the reason the first-seq is in the
    file NAME: telling a gap at the front from a gap in the middle needs only the
    directory listing, so the decision costs no file read and stays correct on a
    ledger too large to scan.
    """
    return [
        first
        for path in segment_paths(kind, unit_id)
        if (first := _segment_first_seq(path)) is not None
    ]


def _lock_path(kind: str, unit_id: str) -> Path:
    return ledger_dir(kind, unit_id) / _LOCK_FILE


@contextmanager
def _open_lock(path: Path) -> Iterator[None]:
    """Hold the advisory lock on *path*, creating the lock file if absent.

    ``"r+"`` -- writable but NOT truncating -- for the reason ``work_ledger``
    documents: ``msvcrt.locking`` needs a writable handle, so ``"r"`` is out,
    while ``"w"`` truncates, and on Windows a truncating open of a file another
    process already holds locked raises a sharing violation instead of waiting.
    That would make the second contender crash before it reached the lock,
    defeating the serialization the lock exists for. ``file_lock`` itself fails
    closed, which is why nothing here has a lock-less fallback.
    """
    _mkdir_private(path.parent)
    path.touch(exist_ok=True)
    with open(path, "r+") as handle:
        with file_lock(handle.fileno(), exclusive=True):
            yield


# --------------------------------------------------------------------------- #
# Tail scan
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Tail:
    """What a bounded read of the file's end says about its state.

    ``torn_offset`` and ``needs_newline`` are mutually exclusive: the trailing
    unterminated bytes either parse (the newline was lost, keep the record) or
    they do not (a crash artifact, drop it).

    ``window`` is the bytes that were read, already stripped of any torn tail,
    and ``at_start`` says whether they reach the beginning of the file. Both are
    carried so a caller that needs a SECOND answer about the tail -- does this
    seq exist -- can have it without a second read, and without this function
    parsing the whole window for a caller that does not ask.
    """

    last_seq: int
    torn_offset: int | None
    needs_newline: bool
    empty: bool
    window: bytes = b""
    at_start: bool = True


def _parses_to_object(blob: bytes) -> dict[str, Any] | None:
    """*blob* as a JSON object, or ``None``. Never raises."""
    try:
        parsed = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _seq_of(blob: bytes) -> int | None:
    """The ``seq`` of the entry *blob* encodes, or ``None`` when it has none."""
    parsed = _parses_to_object(blob)
    if parsed is None:
        return None
    entry = Entry.from_dict(parsed)
    return None if entry is None else entry.seq


def _scan_tail(path: Path) -> _Tail:
    """Read the end of *path* and report seq, torn bytes, and emptiness."""
    size = path.stat().st_size
    if size == 0:
        return _Tail(last_seq=0, torn_offset=None, needs_newline=False, empty=True)
    window = min(size, _TAIL_WINDOW)
    with open(path, "rb") as handle:
        handle.seek(size - window)
        blob = handle.read(window)
    at_start = window == size

    torn_offset: int | None = None
    needs_newline = False
    if not blob.endswith(b"\n"):
        cut = blob.rfind(b"\n")
        trailing = blob[cut + 1 :]
        if _parses_to_object(trailing) is not None:
            needs_newline = True
        else:
            torn_offset = size - len(trailing)
            blob = blob[: cut + 1]

    segments = blob.split(b"\n")
    if not at_start:
        # The window may begin mid-line; that first fragment is not a record.
        segments = segments[1:]
    last_seq = 0
    for segment in reversed(segments):
        stripped = segment.strip()
        if not stripped:
            continue
        seq = _seq_of(stripped)
        if seq is not None:
            last_seq = seq
            break
    if last_seq == 0 and not at_start:
        # Every line in the window was the header-less kind or damaged; only a
        # full scan can answer, and it is the rare path by construction.
        last_seq = _full_scan_last_seq(path)
    return _Tail(
        last_seq=last_seq,
        torn_offset=torn_offset,
        needs_newline=needs_newline,
        empty=False,
        window=blob,
        at_start=at_start,
    )


def _anchor_exists(path: Path, seq: int, tail: _Tail) -> bool:
    """Whether *seq* names a PARSEABLE entry in *path*.

    A thread pointing at a line no reader can parse is a pointer to nothing, so
    the range check ``1 <= seq <= last_seq`` is not enough on its own: a damaged
    interior line at exactly that seq is skipped by every reader, and the group
    would hang off an anchor that never appears.

    Bounded, in three steps, so proving this costs an ordinary append nothing:

    1. The window is already in memory, so an anchor inside it is free. A thread
       anchor is normally recent, which is exactly where that lands.
    2. If the window reached the start of the file, its answer is complete -- the
       whole file was examined.
    3. Only an anchor OLDER than the window falls back to a scan, and that scan
       stops AT the anchor instead of reading to the end.

    An append that passes no ``thread`` never calls this, so the bounded-tail
    cost of the ordinary write path is unchanged.
    """
    segments = tail.window.split(b"\n")
    if not tail.at_start:
        segments = segments[1:]
    for segment in segments:
        stripped = segment.strip()
        if stripped and _seq_of(stripped) == seq:
            return True
    if tail.at_start:
        return False
    for entry in _iter_entries(path):
        if entry.seq == seq:
            return True
        if entry.seq > seq:
            return False
    return False


def _full_scan_last_seq(path: Path) -> int:
    """The highest seq any parseable line carries. The fallback path only."""
    highest = 0
    for entry in _iter_entries(path):
        if entry.seq > highest:
            highest = entry.seq
    return highest


def _has_content(path: Path) -> bool:
    """Whether *path* holds any bytes. A zero-byte file reads as absent.

    ``create`` and ``open`` need the same answer: an empty file is not a ledger
    and never was one, so treating it as existing would strand a unit on a file
    that carries nothing to protect.
    """
    try:
        return path.stat().st_size > 0
    except FileNotFoundError:
        return False


def _iter_entries(path: Path) -> Iterator[Entry]:
    """Every parseable entry in *path*, oldest first, header excluded.

    A malformed interior line is SKIPPED, not raised on: the log is append-only
    and one damaged line must not hide the history in front of it. The file is
    streamed line by line, so a large ledger costs one line of memory, not its
    size.

    Decoding is STRICT and per line. Replacement-decoding would be the wrong
    kind of tolerance here: invalid bytes inside a JSON string can decode into
    still-valid JSON, so a damaged line would be yielded with silently altered
    values instead of skipped -- handing a consumer corrupted data as authority,
    which is precisely what a record that calls itself the authority must never
    do. Byte damage is this machinery's expected adversary, so an undecodable
    line is damage and is skipped exactly like unparseable JSON.

    The file is read in BINARY mode and framed by
    :func:`jsonl_util.bounded_raw_records`, so no universal-newline translation
    can rewrite the bytes on the way in, and one planted line cannot cost more
    than :data:`MAX_ENTRY_BYTES` of memory however long it is. That cap is the
    format's own write limit, so a longer line is not something this writer
    produced: it is damage, and the skip posture already applies to damage.
    """
    try:
        with open(path, "rb") as source:
            for index, raw in enumerate(
                bounded_raw_records(source, path, cap=MAX_ENTRY_BYTES, label="ledger")
            ):
                if index == 0:
                    continue  # the header
                stripped = raw.strip()
                if not stripped:
                    continue
                parsed = _parses_to_object(stripped)
                if parsed is None:
                    continue
                entry = Entry.from_dict(parsed)
                if entry is not None:
                    yield entry
    except FileNotFoundError:
        return


def _read_header_line(path: Path) -> bytes | None:
    """Line 1 of *path* as raw bytes, or ``None`` when there is no usable one.

    Bytes, not text: the caller decodes strictly and reports a header it cannot
    decode as ``bad_header`` rather than accepting a replacement-decoded one.

    Framed with the ABORT posture (:func:`jsonl_util.strict_raw_records`) where
    the entry read below uses the skip posture, and the difference is the point.
    Skipping an over-cap line 1 would hand back line 2 -- an ENTRY -- as though it
    were the header, which is a wrong answer rather than a missing one. Raising
    instead becomes ``None`` here, and ``None`` is what the caller reports as a
    bad header, so an unreadable line 1 fails closed on the identity of the unit.
    """
    try:
        with open(path, "rb") as source:
            for raw in strict_raw_records(source, path, cap=MAX_ENTRY_BYTES):
                return raw.strip()
    except FileNotFoundError:
        return None
    except UnreadableRecord:
        return None
    return None


def _read_first_entry_seq(path: Path) -> int | None:
    """Seq on the physical first entry line, or ``None`` when unreadable.

    Filename provenance applies only when that line carries a usable entry. A
    damaged first record has no sequence claim to compare and remains subject to
    the same per-record skip posture as any later damaged record.
    """
    try:
        with open(path, "rb") as source:
            records = iter(strict_raw_records(source, path, cap=MAX_ENTRY_BYTES))
            next(records, None)  # header
            raw = next(records, b"").strip()
    except FileNotFoundError:
        return None
    except UnreadableRecord:
        return None
    parsed = _parses_to_object(raw)
    entry = None if parsed is None else Entry.from_dict(parsed)
    return None if entry is None else entry.seq


# --------------------------------------------------------------------------- #
# Read results
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Page:
    """One page of entries, NEWEST first.

    ``next_before`` is the cursor for the following page, or ``None`` when this
    page reached the oldest entry. It is set only when an older entry actually
    exists, so paging never hands back a phantom empty page at the end.
    """

    entries: tuple[Entry, ...]
    next_before: int | None


@dataclass(frozen=True)
class Resolution:
    """The outcome of following a :class:`~kiro_crew.ledger.schema.Ref`."""

    status: str
    entries: tuple[Entry, ...]

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK


# --------------------------------------------------------------------------- #
# The ledger
# --------------------------------------------------------------------------- #


class Ledger:
    """One unit's append-only ledger.

    Construct through :meth:`create` or :meth:`open`, never directly: both do
    the file-level work (existence, header, torn-tail repair, seq recovery) that
    the instance then assumes has happened.
    """

    __slots__ = (
        "_kind",
        "_id",
        "_path",
        "_header",
        "_last_seq",
        "_needs_newline",
        "_lease_key",
        # Weak-referenceable so this handle being dropped is what releases its
        # write ownership. See :meth:`_claim`.
        "__weakref__",
    )

    def __init__(
        self,
        *,
        kind: str,
        unit_id: str,
        path: Path,
        header: Header,
        last_seq: int,
        needs_newline: bool,
        lease_key: str | None = None,
    ) -> None:
        self._kind = kind
        self._id = unit_id
        self._path = path
        self._header = header
        self._last_seq = last_seq
        self._needs_newline = needs_newline
        self._lease_key: str | None = None
        if lease_key is not None:
            # A reference already taken on this handle's behalf -- the repair an
            # ``open`` ran before the instance existed. Adopting it here is what
            # binds its release to this handle's lifetime.
            self._adopt_lease(lease_key)

    # -- identity ----------------------------------------------------------- #

    @property
    def kind(self) -> str:
        return self._kind

    @property
    def id(self) -> str:
        return self._id

    @property
    def path(self) -> Path:
        return self._path

    @property
    def header(self) -> Header:
        return self._header

    @property
    def last_seq(self) -> int:
        """The newest seq this instance knows of. Authoritative only for its own
        appends -- the file is re-read under the lock on every write."""
        return self._last_seq

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"Ledger(kind={self._kind!r}, id={self._id!r}, last_seq={self._last_seq})"

    # -- lifecycle ---------------------------------------------------------- #

    @classmethod
    def exists(cls, kind: str, unit_id: str) -> bool:
        """Whether *unit_id* has a ledger with content. Raises on an invalid id.

        Empty means absent, the same answer ``create`` and ``open`` give, so all
        three agree about a file that carries nothing.
        """
        return any(_has_content(p) for p in segment_paths(kind, unit_id))

    @classmethod
    def create(cls, kind: str, unit_id: str, **header_fields: Any) -> Ledger:
        """Write a new ledger's header. Refuses if the file already exists.

        The header is PUBLISHED atomically -- temp file, fsync, rename -- while
        every later line is a plain append. Creation is the one moment the file
        holds no history, so nothing is lost by writing it whole, and an
        interrupted create must not be able to leave a partial header behind: a
        short write or ENOSPC mid-header would otherwise wedge the unit forever,
        since ``open`` would read the fragment as a torn tail, truncate it to an
        empty file, and refuse -- while a retried ``create`` refused the very
        file it had just produced. After the rename the file is append-only for
        the rest of its life.

        A ZERO-BYTE file counts as absent for the same reason: it carries no
        header and no entries, so there is nothing to protect and refusing would
        only strand the unit. Anything longer is real content and is refused.

        Existence is checked twice, the second time under the lock: the first
        check is the cheap answer for the ordinary caller, the second is what
        makes "create refuses an existing ledger" true when two processes race.
        """
        kind = require_kind(kind)
        path = ledger_path(kind, unit_id)
        if _has_content(path):
            raise LedgerError(
                f"{kind} ledger {unit_id!r} already exists", code=CODE_ALREADY_EXISTS, field="id"
            )
        header = build_header(kind, unit_id, now_ms(), header_fields)
        line = require_entry_line(serialize(header.to_dict()))
        with _open_lock(_lock_path(kind, unit_id)):
            if _has_content(path):
                raise LedgerError(
                    f"{kind} ledger {unit_id!r} already exists",
                    code=CODE_ALREADY_EXISTS,
                    field="id",
                )
            _mkdir_private(path.parent)
            atomic_write(path, f"{line}\n", fsync=True, newline="\n")
        return cls(
            kind=kind,
            unit_id=unit_id,
            path=path,
            header=header,
            last_seq=0,
            needs_newline=False,
        )

    @classmethod
    def open(cls, kind: str, unit_id: str, *, repair: bool = False) -> Ledger:
        """Open an existing ledger, repairing a torn tail if there is one.

        A zero-byte file answers ``no_ledger``, the same answer ``create`` gives
        it. One meaning for an empty file across both paths is what keeps them
        from disagreeing: a create that could not finish leaves nothing behind,
        and a retry succeeds instead of being refused by the fragment.

        ``repair`` is what closes an INTERRUPTED TURN, and it is opt-in because
        the two callers of ``open`` want opposite things. A RESUME -- the gateway
        finding a session ledger whose writer is gone -- wants the open turn
        closed, and passes ``repair=True``. A live writer RECONNECTING to its own
        ledger must not: its turn is still running, and closing it would append a
        ``turn/completed {interrupted}`` in the middle of a turn that then keeps
        writing, so the record would claim an outcome the turn never had. A
        reconnect happens for reasons that have nothing to do with the writer's
        health -- a handle evicted from a bounded cache is enough -- so repair
        cannot be inferred from the fact that an ``open`` is happening at all. See
        :func:`_close_interrupted_tail`; the torn-tail truncation below is
        unconditional because trailing bytes that are not a complete line are not
        a record, so nothing can be reading them.
        """
        kind = require_kind(kind)
        # The NEWEST segment is the one a writer appends to, and with nothing
        # rotating yet that is ``ledger.jsonl`` in every ledger that exists. Taking
        # it from the segment list rather than the fixed name is what lets a log
        # whose oldest segment was pruned still open: retention removes files off
        # the front, and the header travels on each segment so the survivor carries
        # it.
        segments = [
            candidate for candidate in segment_paths(kind, unit_id) if _has_content(candidate)
        ]
        if not segments:
            raise LedgerError(f"no {kind} ledger for {unit_id!r}", code=CODE_NO_LEDGER, field="id")
        path = segments[-1]
        header_path = segments[0]
        with _open_lock(_lock_path(kind, unit_id)):
            tail = _scan_tail(path)
            if tail.torn_offset is not None:
                dropped = path.stat().st_size - tail.torn_offset
                _truncate(path, tail.torn_offset)
                logger.warning(
                    "dropped %d torn trailing byte(s) from %s ledger %r",
                    dropped,
                    kind,
                    unit_id,
                )
            raw = _read_header_line(header_path)
        parsed = None if not raw else _parses_to_object(raw)
        if parsed is None:
            raise LedgerError(
                f"{kind} ledger {unit_id!r} has no readable header line",
                code=CODE_BAD_HEADER,
                field="type",
            )
        header = parse_header(parsed, kind=kind, unit_id=unit_id)
        lease_key: str | None = None
        if repair:
            # Ownership BEFORE the closers, and the reason repair is the one thing
            # ``open`` claims it for: the closers are appends, and a repair running
            # while a writer in ANOTHER process still holds the turn is what makes
            # the file state two outcomes for one turn. Refused here, this open
            # raises and nothing is written -- the live writer's log is left as it
            # stands.
            _mkdir_private(path.parent)
            lease_key = acquire_lease(path.parent / LEASE_FILE, kind=kind, unit_id=unit_id)
            try:
                if _close_interrupted_tail(kind, unit_id, path):
                    # The closers moved the tail, so this object's cached seq has to
                    # be re-read or its first append would collide with them.
                    tail = _scan_tail(path)
            except BaseException:
                release_lease(lease_key)
                raise
        return cls(
            kind=kind,
            unit_id=unit_id,
            path=path,
            header=header,
            last_seq=tail.last_seq,
            needs_newline=tail.needs_newline,
            lease_key=lease_key,
        )

    def repair_interrupted_turn(self) -> int:
        """Close an open turn on this ledger. Returns how many closers landed.

        The method form of ``open(repair=True)``, for a caller that already holds
        a handle. Same rule: only a resume calls it, never a live writer.
        """
        # The closers are appends, so this takes write ownership exactly as one
        # does, and is refused the same way when another process holds the log.
        self._claim()
        written = _close_interrupted_tail(self._kind, self._id, self._path)
        # Refreshed unconditionally: the repair also TRUNCATES an unreachable chunk
        # group, which changes the tail without writing a closer, and a cached
        # ``last_seq`` past the end of the file would be served to a reader that only
        # wants to look.
        tail = _scan_tail(self._path)
        self._last_seq = tail.last_seq
        self._needs_newline = tail.needs_newline
        return written

    # -- write ownership ---------------------------------------------------- #

    def _claim(self) -> None:
        """Take write ownership of this unit before the first write of this handle.

        Lazy, and never taken by ``open`` itself, because ``open`` also serves
        READERS: ``iter_from``, ``page`` and ``resolve`` need no ownership, and
        making a reader contend with the writer would be a regression in exchange
        for nothing. The first write is the moment ownership starts to mean
        something, so that is where it is claimed.

        Raises the ``already_owned`` refusal when another PROCESS owns the log.
        Another handle in THIS process is not contention -- it shares the same
        kernel lock through the reference count -- which is what lets the emitter's
        cached handle and a session claim's handle overlap.
        """
        if self._lease_key is not None:
            return
        # The directory of the segment this handle appends to, not one resolved
        # from the data home again: a handle writes the file it was opened on, and
        # the lock has to name that file even if the home was repointed since.
        _mkdir_private(self._path.parent)
        self._adopt_lease(
            acquire_lease(self._path.parent / LEASE_FILE, kind=self._kind, unit_id=self._id)
        )

    def _adopt_lease(self, key: str) -> None:
        """Bind an acquired lease reference to this handle's lifetime.

        Released when this object is dropped, so a holder keeps ownership for
        exactly as long as it can still append through it. That places one
        requirement on whatever holds a handle: it must not drop one belonging to
        a LIVE turn, or ownership ends while that turn is still producing entries
        and a successor's repair closes a turn that completes for real a moment
        later. Both of the emitter's release paths honour it -- capacity eviction
        and session teardown each skip a session with a live turn -- so ownership
        ends between turns, where there is no live turn for a repair to damage.

        Binding release to the object rather than to an explicit call is also what
        keeps a queued write that still holds the handle inside the ownership it
        needs: an eager release at eviction would take it out from under a job
        that is about to append.
        """
        self._lease_key = key
        weakref.finalize(self, release_lease, key)

    # -- write -------------------------------------------------------------- #

    def append(
        self,
        type: str,
        data: dict[str, Any],
        *,
        src: str,
        thread: int | None = None,
        ref: Ref | dict[str, Any] | None = None,
        ignorable: bool = False,
    ) -> Entry:
        """Append one entry and return it, with ``seq`` and ``time`` filled in.

        Every refusal happens before any byte is written, so a rejected append
        leaves the file identical. ``thread`` is checked against the seq read
        back under the lock, which is also what assigns this entry's own seq.

        ``ignorable`` promises that nothing later in the file depends on this
        entry being understood, so a reader that does not know the type may skip
        it instead of stopping. Only the writer can make that promise -- it is
        the one that knows whether the entry is a sample or a fact -- which is
        why it lives on the append and not on the read.
        """
        require_data(data)
        check_ownership(self._kind, type, src)
        pointer = None if ref is None else (ref if isinstance(ref, Ref) else Ref.from_dict(ref))
        if thread is not None and (
            not isinstance(thread, int) or isinstance(thread, bool) or thread < 1
        ):
            raise LedgerError(
                f"thread must be a positive seq in this ledger: {thread!r}",
                code=CODE_BAD_THREAD,
                field="thread",
            )
        # After the format checks, so a malformed entry is refused without this
        # process claiming anything, and before the write lock, so ownership is
        # settled before any byte is at stake.
        self._claim()
        with _open_lock(_lock_path(self._kind, self._id)):
            tail = _scan_tail(self._path)
            if tail.empty:
                raise LedgerError(
                    f"no {self._kind} ledger for {self._id!r}",
                    code=CODE_NO_LEDGER,
                    field="id",
                )
            if tail.torn_offset is not None:
                _truncate(self._path, tail.torn_offset)
            if thread is not None and (
                thread > tail.last_seq or not _anchor_exists(self._path, thread, tail)
            ):
                raise LedgerError(
                    f"thread {thread} names no parseable entry in this ledger "
                    f"(newest seq is {tail.last_seq})",
                    code=CODE_BAD_THREAD,
                    field="thread",
                )
            entry = Entry(
                type=type,
                seq=tail.last_seq + 1,
                time=now_ms(),
                src=src,
                data=data,
                thread=thread,
                ref=pointer,
                ignorable=bool(ignorable),
            )
            line = require_entry_line(serialize(entry.to_dict()))
            _append_line(self._path, line, needs_newline=tail.needs_newline)
        self._last_seq = entry.seq
        self._needs_newline = False
        return entry

    def append_many(
        self,
        items: "list[dict[str, Any]]",
        *,
        src: str,
        cite: "Callable[[list[int]], dict[str, Any]] | None" = None,
    ) -> "list[Entry]":
        """Append a GROUP of entries as one write, and return them.

        For a group whose members are meaningless apart: a body too large for one
        line becomes several ``message/chunk`` entries plus the entry that CITES
        their seqs, and appending those one at a time leaves a window where a hard
        kill puts the chunks on disk with nothing pointing at them -- the body is
        stored and unreachable, and the citing entry that would have explained it
        never exists. Written together, a crash leaves either the whole group or a
        torn tail, and the tail is what the next append truncates.

        *cite* is how a citation is made unable to disagree with the allocation. The
        caller passes ONLY the cited entries in *items* and a callable; this method
        calls it INSIDE the lock, with the seqs it has just allocated for those
        entries, and appends what it returns as the group's last member. Allocating
        outside the lock and citing the result cannot be made correct: the lock is a
        cross-process one, so between a caller's read of the tail and this method's
        read another handle -- a resumed session, a second process -- can append and
        shift the whole run, after which the citation names seqs belonging to the
        intruder. Nothing detects that later: the seqs exist and parse.

        Every refusal still happens before any byte is written -- each item is
        validated up front, and a *cite* result is validated before the write it
        joins -- so a rejected group leaves the file identical. One lock, one
        ``write()``, one ``fsync``.

        ``thread`` is deliberately not accepted here. A group is self-contained by
        construction, and an anchor check per member would have to re-read the tail
        it is being allocated from.
        """
        if not items:
            return []
        for item in items:
            # ``item["data"]`` itself, not ``item.get("data") or {}``: the fallback
            # made a MISSING key and a falsey non-dict both validate as an empty
            # object, so a caller that forgot the body -- or passed None -- would
            # have its entry written with no data at all. A group is refused whole,
            # and this is where that promise is kept.
            require_data(item.get("data"))
            check_ownership(self._kind, str(item.get("type") or ""), src)
        self._claim()
        with _open_lock(_lock_path(self._kind, self._id)):
            tail = _scan_tail(self._path)
            if tail.empty:
                raise LedgerError(
                    f"no {self._kind} ledger for {self._id!r}",
                    code=CODE_NO_LEDGER,
                    field="id",
                )
            if tail.torn_offset is not None:
                _truncate(self._path, tail.torn_offset)
                tail = _scan_tail(self._path)
            members = items
            if cite is not None:
                # The seqs the cited entries are about to get, from the allocation
                # this call is committing to -- not from a read the caller made
                # earlier and hoped still held.
                allocated = [tail.last_seq + 1 + offset for offset in range(len(items))]
                citing = cite(allocated)
                # Refused, not raised through. An entry shape this cannot use is a
                # deterministic caller bug, and letting it surface as a TypeError or
                # an AttributeError would put it in the "might clear" class the
                # write-behind RETRIES -- spending the whole attempt budget, and
                # holding every later entry of this session behind it, on a verdict
                # that cannot change. A refusal is a loss now, which is the honest
                # outcome and the one the retention policy already handles.
                if not isinstance(citing, dict):
                    raise LedgerError(
                        f"cite must return an entry mapping, got {type(citing).__name__}",
                        code=CODE_BAD_DATA,
                        field="cite",
                    )
                require_data(citing.get("data"))
                check_ownership(self._kind, str(citing.get("type") or ""), src)
                # LAST, so a torn tail can only cost the citing entry -- the orphan
                # shape the repair drops -- never a chunk it already named.
                members = [*items, citing]
            stamped = now_ms()
            entries: "list[Entry]" = []
            for offset, item in enumerate(members):
                pointer = item.get("ref")
                entries.append(
                    Entry(
                        type=str(item["type"]),
                        seq=tail.last_seq + 1 + offset,
                        time=stamped,
                        src=src,
                        data=item["data"],
                        thread=None,
                        ref=(
                            None
                            if pointer is None
                            else (pointer if isinstance(pointer, Ref) else Ref.from_dict(pointer))
                        ),
                        ignorable=bool(item.get("ignorable")),
                    )
                )
            lines = [require_entry_line(serialize(entry.to_dict())) for entry in entries]
            _append_lines(self._path, lines, needs_newline=tail.needs_newline)
        self._last_seq = entries[-1].seq
        self._needs_newline = False
        return entries

    # -- read --------------------------------------------------------------- #

    def iter_from(self, seq: int = 1, *, known: Collection[str] | None = None) -> Iterator[Entry]:
        """Every entry from *seq* onward, OLDEST first -- the shape a fold wants.

        *known* is the reader DECLARING the types it can interpret, and passing
        it is what turns this read into a reconstruction rather than a listing.
        Then an entry whose type is not in *known* either is skipped, when the
        writer marked it ``ignorable``, or raises ``unknown_entry_type`` when it
        did not: a required entry the reader cannot interpret may change the
        meaning of every entry after it, so a fold that continued past one would
        produce a confident wrong answer instead of an admitted failure.

        The default, ``None``, means "I understand everything" and is exactly the
        behaviour every caller had before the marker existed -- no refusal, every
        entry yielded. The gate is opt-in because only a caller that FOLDS state
        is harmed by a line it skipped; :meth:`page` deliberately has no such
        parameter, since paging renders history for a human and showing an
        unfamiliar line is not a wrong answer.

        Reads across SEGMENTS, oldest first, and requires seq to stay contiguous
        over each boundary -- a gap between segments is damage or a partial copy,
        and yielding across it would hand a fold a hole it cannot see. A gap at the
        FRONT is not damage: that is retention, so a first segment starting above 1
        is read as it stands.
        """
        for entry in self._iter_segments():
            if entry.seq < seq:
                continue
            if known is not None and entry.type not in known:
                if not entry.ignorable:
                    raise LedgerError(
                        f"entry {entry.seq} has type {entry.type!r}, which this reader "
                        "does not know and which is not marked ignorable; "
                        "reconstruction stops here",
                        code=CODE_UNKNOWN_ENTRY_TYPE,
                        field="type",
                    )
                continue
            yield entry

    def _iter_segments(self) -> Iterator[Entry]:
        """Every entry of every segment, oldest first, refusing bad provenance.

        Every segment repeats the header because retention may remove the first one.
        Each header must therefore identify this same ledger and schema before any
        entry from that independent file becomes authoritative. The filename's first
        seq is a second provenance claim and must agree with the physical first entry
        when that record is readable.

        Seq continuity remains a boundary-only check. Inside one file a missing seq
        means a damaged line, which ``_iter_entries`` skips on purpose -- one
        unreadable record must not make the rest of the file unreadable. Across two
        files a gap is a different thing: a half-finished copy or deleted middle
        segment is invisible unless the boundary is checked.
        """
        expected = 0
        for index, path in enumerate(segment_paths(self._kind, self._id)):
            raw_header = _read_header_line(path)
            parsed_header = None if not raw_header else _parses_to_object(raw_header)
            try:
                segment_header = parse_header(
                    parsed_header,
                    kind=self._kind,
                    unit_id=self._id,
                )
            except LedgerError as exc:
                raise LedgerError(
                    f"segment {path.name} has an invalid header: {exc}",
                    code=CODE_BAD_SEGMENT,
                    field=exc.field,
                ) from exc
            if segment_header.version != self._header.version:
                raise LedgerError(
                    f"segment {path.name} has schema version {segment_header.version}, "
                    f"but this ledger uses {self._header.version}",
                    code=CODE_BAD_SEGMENT,
                    field="version",
                )

            declared_first = _segment_first_seq(path)
            actual_first = _read_first_entry_seq(path)
            if actual_first is not None and actual_first != declared_first:
                raise LedgerError(
                    f"segment {path.name} declares first seq {declared_first} in its filename, "
                    f"but its first entry is seq {actual_first}",
                    code=CODE_BAD_SEGMENT,
                    field="seq",
                )

            at_boundary = bool(index) and expected > 0
            for entry in _iter_entries(path):
                if at_boundary and entry.seq != expected:
                    raise LedgerError(
                        f"segment {path.name} starts at seq {entry.seq}, but the "
                        f"previous segment ended at {expected - 1}: the log is not "
                        "contiguous across the boundary",
                        code=CODE_SEGMENT_GAP,
                        field="seq",
                    )
                at_boundary = False
                expected = entry.seq + 1
                yield entry

    def get(self, seq: int) -> Entry | None:
        """The entry at *seq*, or ``None``.

        Stops as soon as the stream passes *seq*: entries are written in seq
        order, so a miss costs the prefix, not the file.

        Reads across SEGMENTS, like :meth:`iter_from`. Reading only the newest file
        made a point read of a rotated entry answer ``None`` -- indistinguishable
        from "no such entry" -- so a caller resolving a seq it had just been handed
        would be told it does not exist. Retention is the one thing that legitimately
        removes an entry, and it deletes whole segments off the front; while a
        segment is still on disk its entries are still readable.
        """
        for entry in self._iter_segments():
            if entry.seq == seq:
                return entry
            if entry.seq > seq:
                return None
        return None

    def page(self, before: int | None = None, limit: int = DEFAULT_PAGE_LIMIT) -> Page:
        """Up to *limit* entries older than *before*, NEWEST first.

        ``before`` is exclusive, so feeding ``next_before`` straight back walks
        the history without repeating or skipping a line.
        """
        return self._page(before, limit, keep=lambda _entry: True)

    def thread_page(
        self, anchor: int, before: int | None = None, limit: int = DEFAULT_PAGE_LIMIT
    ) -> Page:
        """One thread's entries, NEWEST first, the anchor last.

        A thread is a GROUPING key, not a tree: members carry ``thread ==
        anchor`` and the anchor carries its own seq. The anchor is included, so
        the final page of a thread ends with the entry the group hangs off --
        which is what makes a thread readable bottom-up without a second call.
        """
        return self._page(
            before, limit, keep=lambda entry: entry.thread == anchor or entry.seq == anchor
        )

    def _page(self, before: int | None, limit: int, *, keep: Callable[[Entry], bool]) -> Page:
        bound = max(1, min(int(limit), MAX_PAGE_LIMIT))
        # One extra slot answers "is there an older entry" exactly, so the
        # cursor is None precisely when the caller has seen everything.
        window: deque[Entry] = deque(maxlen=bound + 1)
        # Across SEGMENTS, like `iter_from` and `get`. Reading only the newest file
        # made paging stop dead at a rotation boundary and report `next_before` as
        # None -- telling the caller it had seen the whole history when it had seen
        # one segment of it. Paging renders history for a human, and silently
        # truncating that is the one answer it must not give.
        for entry in self._iter_segments():
            if before is not None and entry.seq >= before:
                continue
            if keep(entry):
                window.append(entry)
        newest_first = list(reversed(window))
        has_more = len(newest_first) > bound
        entries = tuple(newest_first[:bound])
        return Page(entries=entries, next_before=entries[-1].seq if has_more and entries else None)

    def resolve(self, ref: Ref | dict[str, Any]) -> Resolution:
        """Follow *ref* and return the segment it cites.

        Four outcomes. ``ok`` carries the entries. ``gone`` means the cited ledger
        does not exist at all -- a normal answer, not an error, since the cited
        ledger may legitimately have been deleted and the citing entry stays honest
        about having pointed at it. ``pruned`` means the span reaches below the
        oldest surviving segment's first seq, so retention removed those lines.
        ``corrupt`` means the span lies inside a segment that still exists yet read
        short, which is damage.

        The last two are never reported as each other, and the classification is
        made from the segment names rather than from the short read: a reader told
        ``pruned`` stops looking, because retention removing old lines is a normal
        answer, while ``corrupt`` tells it the file it still has is not intact.
        Reporting damage as retention would turn a recoverable alarm into silence.
        Citing PAST the newest entry is ``corrupt`` as well, and deliberately not
        ``ok``: a ``Ref`` always names a definite span -- an absent ``to_seq``
        means the single line at ``from_seq``, so there is no open-ended spelling
        -- which makes a span reaching beyond the newest entry a claim about lines
        that are not in the file. ``ok`` means every cited seq was read back.

        This layer makes NO authorization claim, and takes no access callback. A
        half-built one would be worse than none: a check that defaults to allow
        makes the shortest call shape the insecure one, and a check with no
        permission model behind it only looks like a boundary. Every caller today
        is in-process gateway code that can already read the file. A ``forbidden``
        status arrives with the first caller that HAS a permission model -- the
        routes that mount this -- and it belongs there, where the caller identity
        it must be derived from actually exists.
        """
        pointer = ref if isinstance(ref, Ref) else Ref.from_dict(ref)
        if pointer.unit == self._kind and pointer.id == self._id:
            target: Ledger | None = self
        else:
            try:
                target = Ledger.open(pointer.unit, pointer.id)
            except LedgerError as exc:
                # Only "there is no such ledger" is `gone`. A ledger that EXISTS
                # but will not open -- a damaged header, an unreadable segment --
                # is damage, and answering `gone` for it tells the reader to stop
                # looking for a file that is right there and broken.
                if getattr(exc, "code", "") == CODE_NO_LEDGER:
                    return Resolution(status=STATUS_GONE, entries=())
                return Resolution(status=STATUS_CORRUPT, entries=())
        if target is None:
            return Resolution(status=STATUS_GONE, entries=())
        last = pointer.last_seq
        try:
            found = tuple(
                entry for entry in target.iter_from(pointer.from_seq) if entry.seq <= last
            )
        except LedgerError:
            # A missing middle segment or a torn line RAISES out of the read. That
            # is the very condition this method promises to report, so it is
            # answered rather than propagated -- a caller resolving a citation
            # wants a verdict, and an exception here would make `corrupt`
            # unreachable for exactly the damage it names.
            return Resolution(status=STATUS_CORRUPT, entries=())
        # A SHORT answer has two possible causes and they are not the same fact.
        # Retention deletes whole segments off the front, so a span reaching below
        # the oldest survivor is `pruned` -- a normal answer. A span that lies
        # inside a segment which still exists, yet reads short, is DAMAGE: the
        # lines should be there. Reporting that as retention tells a reader to
        # stop looking for a file that is in fact corrupt.
        firsts = segment_first_seqs(target.kind, target.id)
        oldest = firsts[0] if firsts else 1
        if pointer.from_seq < oldest:
            surviving_from = max(pointer.from_seq, oldest)
            surviving_expected = max(0, last - surviving_from + 1)
            surviving = {entry.seq for entry in found}
            if len(surviving) < surviving_expected or len(surviving) != len(found):
                return Resolution(status=STATUS_CORRUPT, entries=found)
            return Resolution(status=STATUS_PRUNED, entries=found)
        # Seq is contiguous inside a file, so the count is the test -- and the count
        # comes from what the CITATION claims existed, never from what is on disk
        # now. `ok` means every cited seq was read back; anything short of that is
        # the citation failing, whatever shortened the file.
        #
        # Clamping to the file's own tail is what made this wrong. A clean
        # end-truncation -- whole lines removed, no torn bytes, nothing for the read
        # to raise on -- lowers both the walked entries and a reopened handle's
        # `last_seq` together, so the expected count shrinks to exactly what
        # survived and the verdict came back `ok` with the cited lines missing. That
        # is the worst available answer: a caller resolving a citation is asking
        # whether it can still be read, and `ok` with fewer entries tells it yes
        # while handing it a hole.
        #
        # A file that GREW is why the clamp existed, and dropping it is safe: this
        # walk stops at `last`, so extra entries past the citation are never in
        # `found` and cannot inflate the count.
        # The test is COVERAGE of the cited seqs, and a duplicate is damage in its
        # own right. Seq is unique under the append lock, so two lines claiming one
        # seq cannot both be the writer's -- and a tally of LINES would let that
        # duplicate fill the place of a line that is gone, answering `ok` for a span
        # with a hole in it. Counting distinct seqs catches that substitution;
        # comparing the two counts catches the duplicate even when nothing is
        # missing, which is a file a reader must not be told is intact.
        covered = {entry.seq for entry in found}
        expected = max(0, last - pointer.from_seq + 1)
        if len(covered) < expected or len(covered) != len(found):
            # `corrupt` rather than a new status. The distinction a caller acts on is
            # "resolvable or not", and retention -- the one shortening that is normal
            # -- already has its own answer in the `pruned` branch above.
            return Resolution(status=STATUS_CORRUPT, entries=found)
        return Resolution(status=STATUS_OK, entries=found)


# --------------------------------------------------------------------------- #
# File primitives
# --------------------------------------------------------------------------- #


#: Directories this process has already failed to restrict. Every append goes
#: through :func:`_mkdir_private`, so a filesystem that refuses ``chmod`` would
#: otherwise log a full traceback twice per entry -- turning one true fact about the
#: host into a flood that buries the entries it is warning about. Warn once per
#: directory instead, like the boot-time restriction does.
_restrict_failed: set[str] = set()


def _mkdir_private(directory: Path) -> None:
    """Create *directory* and its parents owner-only.

    A bare ``mkdir`` takes the process umask, which is commonly world-readable, and
    these directories hold conversation bodies. The eager tightening at
    ``<home>/ledgers`` normally makes a leaf's own mode moot -- an unreadable parent
    is enough -- but it is best-effort, and this runs on the path taken when it did
    not succeed. So the guarantee is asserted per directory as well as once at the
    root, rather than resting on a parent that may not have been tightened.

    Already-correct is the common case and costs nothing: this runs on EVERY append,
    so the mode is checked before it is set and the ``chmod`` is skipped when the
    directory is already owner-only.

    Best-effort for the same reason the root's is: a filesystem that refuses the mode
    change must not make a ledger unwritable, and the sandbox mask and the file-tool
    fence still stand.
    """
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            if directory.stat().st_mode & 0o777 == 0o700:
                return
        except OSError:
            # Fall through and try to set it; a stat that fails is not a reason to
            # skip the restriction.
            pass
    try:
        restrict_dir_to_owner(directory)
    except OSError:
        key = str(directory)
        if key not in _restrict_failed:
            _restrict_failed.add(key)
            logger.warning(
                "Cannot restrict %s to owner-only; it may be readable by other users",
                directory,
                exc_info=True,
            )


def _append_line(path: Path, line: str, *, needs_newline: bool) -> None:
    """Append *line* plus its terminator, then fsync.

    The handle is BINARY, which is what keeps the terminator exactly one ``\\n``:
    text mode on Windows translates it to ``\\r\\n``, and the byte offsets the
    torn-tail truncation computes then stop matching what is on disk.

    *needs_newline* re-supplies a separator the previous write lost -- the one
    case where a record survived but its terminator did not. It PREPENDS rather
    than rewriting that line, so the append-only rule holds.

    A failure ROLLS BACK. See :func:`_write_then_sync`.
    """
    _mkdir_private(path.parent)
    prefix = "\n" if needs_newline else ""
    _write_then_sync(path, f"{prefix}{line}\n".encode())


def _append_lines(path: Path, lines: "list[str]", *, needs_newline: bool) -> None:
    """Append every line in ONE write and ONE fsync.

    The atomicity this buys is not a filesystem guarantee -- a write can still tear
    -- but it collapses the window: a group written this way is on disk as a whole
    or ends in a torn tail the existing truncation removes, instead of leaving each
    line separately durable and the group half-present.

    Same binary-handle and *needs_newline* rules as :func:`_append_line`, and the
    same rollback on failure, for the same reasons.
    """
    _mkdir_private(path.parent)
    prefix = "\n" if needs_newline else ""
    body = "".join(f"{line}\n" for line in lines)
    _write_then_sync(path, f"{prefix}{body}".encode())


def _write_then_sync(path: Path, blob: bytes) -> None:
    """Append *blob* and fsync it, restoring the previous size if either fails.

    The rollback is what makes a failure DEFINITE. Bytes reach the file before the
    fsync runs, so a failing fsync leaves an outcome nobody knows: the entry may well
    be durable. The write-behind retains and retries a failure, and a retry against
    an unknown outcome either writes the same fact twice under two seqs -- a duplicate
    no reader can tell from a real repeat -- or has to reason about what is already
    there. Truncating back to the size the file had before removes the question:
    either the append is whole and synced, or it is gone and the retry writes it
    cleanly.

    A partial write is rolled back for the same reason, and one more: leaving half a
    line behind would make the next append's torn-tail truncation the thing that
    cleans up, so the file would carry a fragment until then.

    If the ROLLBACK itself fails -- likely, since whatever broke the write is often
    still broken -- the file may hold bytes nobody can account for, and that is what
    :class:`IndeterminateAppend` reports. Its bytes are unterminated or unparseable
    at the tail, which is exactly the shape the next ``open`` truncates, so the
    recovery already exists; the distinct type is so a caller can tell "nothing
    happened" from "something may have".

    Truncating is safe against a concurrent writer because every caller reaches here
    holding the session's append lock, so no other handle can have appended between
    this write and its rollback -- the bytes removed can only be this call's own.
    """
    with open(path, "ab") as handle:
        before = handle.tell()
        try:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        except OSError as exc:
            try:
                handle.close()
                _rollback_append(path, before)
            except OSError as undo:
                raise IndeterminateAppend(
                    f"append to {path.name} failed ({exc}) and could not be rolled "
                    f"back ({undo}): the file may hold bytes no entry claims",
                    written=blob,
                    offset=before,
                ) from exc
            raise


def _rollback_append(path: Path, size: int) -> None:
    """Cut *path* back to *size* and fsync it.

    Not an exception to the never-rewrite rule: the bytes removed are the ones the
    caller's own failed append just wrote, so this restores the file to a state a
    reader could already have seen rather than editing history. It is the same
    category as the torn-tail truncation, moved to the moment the tear happens
    instead of the next open.
    """
    with open(path, "r+b") as handle:
        handle.truncate(size)
        handle.flush()
        os.fsync(handle.fileno())


def _refuse_content_repair(unit_id: str, skipped: str) -> None:
    """Say why a content-aware repair is declining to touch this file.

    Named once because both check sites answer the same question -- the fold ran
    over records it could not read, so what it reports about the tail is not what
    the file says -- and a repair that acts on it appends an outcome no writer
    observed, permanently, to a log nothing rewrites.
    """
    logger.warning(
        "session ledger %r: not repairing an interrupted turn -- the fold skipped a "
        "record (%s), so its result cannot be trusted and appending a closer could "
        "fabricate an outcome",
        unit_id,
        skipped,
    )


def _classify_record(raw: bytes, index: int) -> "tuple[Entry | None, str | None]":
    """*raw* as an entry, or the reason it is not one.

    Both strict walks share this: the scan that produces a truncation offset, and
    the fold that decides whether a closer may be appended. Their results feed
    the same mutation -- one chooses where the file is cut, the other whether
    history is added to it -- so they have to agree about what counts as a
    record. A second spelling of this classification is a second answer waiting
    to disagree with the first.

    *index* is the record's position in the file, so the reason names the line a
    reader can go and look at. Record 1 is the header and is the caller's to skip.
    """
    stripped = raw.strip()
    if not stripped:
        return None, f"record {index + 1} is blank"
    parsed = _parses_to_object(stripped)
    if parsed is None:
        return None, f"record {index + 1} is malformed"
    entry = Entry.from_dict(parsed)
    if entry is None:
        return None, f"record {index + 1} is not a valid entry"
    return entry, None


def _orphan_chunk_offset(path: Path) -> "tuple[int, int, int] | None":
    """A trailing chunk group with no citing entry: its offset and seq range.

    A body too large for one line is written as chunks plus the entry that cites
    their seqs, in one batch. A hard kill during that write can still tear the tail,
    and the citing entry is the LAST line -- so what survives is chunks nothing
    points at. The body is on disk and unreachable: no entry names those seqs, and
    the message the group belongs to has no record at all.

    Dropping them is the honest repair. It costs the one message that was mid-write,
    which is the same residual as any single entry lost to a hard kill, and it leaves
    the file free of lines whose only purpose was to be cited by an entry that does
    not exist.

    Only a TRAILING run counts. A chunk group followed by any other entry was
    completed -- its citing entry landed -- and re-reading which entry cites what is
    not this function's job.

    Returns ``(byte offset of the first orphan, first seq, last seq)`` or ``None``.

    Reads with the ABORT posture, not the skipping one. The offsets computed here
    feed a truncation, and ``bounded_raw_records`` drops an over-cap record in full
    -- terminator included -- without yielding it, so a byte walk over what it does
    yield stops matching the file at the first oversized record and every offset
    after it is short by that record's length. Truncating at a short offset cuts
    valid history, which is the one thing this file's rules forbid. So a record that
    cannot be delivered intact aborts the scan and this returns ``None``: no offset,
    no truncation, the file left exactly as it is with a warning naming why.
    """
    offsets: "list[tuple[int, int, str]]" = []
    try:
        with open(path, "rb") as source:
            at = 0
            for index, raw in enumerate(strict_raw_records(source, path, cap=MAX_ENTRY_BYTES)):
                start = at
                at += len(raw)
                if index == 0:
                    continue
                entry, _ = _classify_record(raw, index)
                if entry is not None:
                    offsets.append((start, entry.seq, entry.type))
    except FileNotFoundError:
        return None
    except UnreadableRecord as exc:
        # Fail closed. Every byte has to be accounted for before an offset into this
        # file can be trusted, and this read could not account for one.
        logger.warning(
            "ledger %r: not scanning for an orphan chunk group -- a record could not "
            "be read intact (%s), so a byte offset into this file cannot be trusted "
            "and truncating on one could cut valid entries",
            path,
            exc,
        )
        return None
    trailing = 0
    while trailing < len(offsets) and offsets[len(offsets) - 1 - trailing][2] == "message/chunk":
        trailing += 1
    if not trailing:
        return None
    first = offsets[len(offsets) - trailing]
    return (first[0], first[1], offsets[-1][1])


def _truncate(path: Path, offset: int) -> None:
    """Drop everything at or after *offset* -- the one allowed mutation."""
    with open(path, "r+b") as handle:
        handle.truncate(offset)
        handle.flush()
        os.fsync(handle.fileno())


# --------------------------------------------------------------------------- #
# Interrupted-tail closers
# --------------------------------------------------------------------------- #

#: What a closer records as the reason a turn ended, and as a tool's outcome.
#: Two distinct words on purpose: the turn ENDED (the writer stopped), while the
#: tool's result is simply not knowable from the record.
STOP_REASON_INTERRUPTED = "interrupted"
TOOL_STATUS_UNKNOWN = "unknown"


@dataclass(frozen=True)
class _OpenTail:
    """What a session ledger's last turn left open, in first-seen order."""

    turn: Any
    calls: tuple[dict[str, Any], ...]
    last_time: int


def _open_tail(path: Path) -> "tuple[_OpenTail | None, str | None]":
    """The unbalanced tail and why a repair fold skipped a record, if it did.

    Unbalanced means the newest ``turn/started`` has no ``turn/completed`` after
    it: the writer stopped mid-turn. Unmatched ``tool/called`` entries are
    collected only from INSIDE that turn -- an unmatched call in a turn that did
    complete is a different anomaly, and inventing a result for it here would be
    this reader editing history it was not asked about.

    This fold feeds a mutation, so it reports every record the ordinary reader
    would skip. A repair cannot distinguish a truly open turn from one whose real
    completion is damaged, and appending a closer after such a skip would make a
    false outcome permanent.
    """
    open_turn: Any = None
    last_time = 0
    calls: dict[str, dict[str, Any]] = {}
    skipped: str | None = None
    try:
        with open(path, "rb") as source:
            for index, raw in enumerate(strict_raw_records(source, path, cap=MAX_ENTRY_BYTES)):
                if index == 0:
                    continue
                entry, reason = _classify_record(raw, index)
                if entry is None:
                    skipped = skipped or reason
                    continue
                last_time = entry.time
                data = entry.data if isinstance(entry.data, dict) else {}
                if entry.type == "turn/started":
                    open_turn = data.get("turn")
                    calls = {}
                elif entry.type == "turn/completed":
                    open_turn = None
                    calls = {}
                elif entry.type == "tool/called" and open_turn is not None:
                    call_id = data.get("call_id")
                    if isinstance(call_id, str) and call_id and call_id not in calls:
                        calls[call_id] = {
                            "call_id": call_id,
                            "name": data.get("name", ""),
                            "server": data.get("server", ""),
                        }
                elif entry.type == "tool/completed" and open_turn is not None:
                    call_id = data.get("call_id")
                    if isinstance(call_id, str):
                        calls.pop(call_id, None)
    except FileNotFoundError:
        return None, None
    except UnreadableRecord as exc:
        skipped = skipped or str(exc)
    if open_turn is None:
        return None, skipped
    return (
        _OpenTail(turn=open_turn, calls=tuple(calls.values()), last_time=last_time),
        skipped,
    )


def _closer_entries(tail: _OpenTail, first_seq: int) -> list[Entry]:
    """The closers for *tail*, in the order they are appended.

    Unmatched calls first, then the turn -- a turn cannot be closed while a call
    inside it is still open, so closing them the other way round would produce a
    record no live writer could ever have produced.

    Every closer reuses the LAST REAL entry's ``time``. A closer describes
    something that happened when the writer stopped, not when a later process
    happened to open the file, so stamping it with the current clock would put a
    gap of arbitrary length inside a turn and make a duration computed off these
    entries a measure of downtime. Reusing the time also makes the repair
    deterministic: the same bytes in produce the same bytes out, whenever it runs.
    """
    entries: list[Entry] = []
    seq = first_seq
    for call in tail.calls:
        entries.append(
            Entry(
                type="tool/completed",
                seq=seq,
                time=tail.last_time,
                src="gateway",
                data={
                    "turn": tail.turn,
                    "call_id": call["call_id"],
                    "name": call["name"],
                    "server": call["server"],
                    "status": TOOL_STATUS_UNKNOWN,
                },
            )
        )
        seq += 1
    entries.append(
        Entry(
            type="turn/completed",
            seq=seq,
            time=tail.last_time,
            src="gateway",
            data={"turn": tail.turn, "stop_reason": STOP_REASON_INTERRUPTED},
        )
    )
    return entries


def _close_interrupted_tail(kind: str, unit_id: str, path: Path) -> int:
    """Append closers for an interrupted turn. Returns how many were written.

    Session ledgers only, and RESUME ONLY. A crash, a SIGKILL or a pod eviction
    leaves the newest turn open, and every later reader then has to carry the same
    special case: is this turn still running, or did its writer die? Closing the
    tail when the ledger is LOADED after an interruption answers that once, in the
    record, instead of in each reader.

    "Loaded after an interruption" is the whole precondition, which is why this is
    never reached from a plain ``open``. An open turn is indistinguishable from a
    dead one by looking at the file, so the CALLER's situation is the only thing
    that can tell them apart: a resume knows the previous writer is gone, and a
    live writer reconnecting to its own ledger knows it is not. Closing a turn
    that is still running would append an outcome it never had and then let the
    turn keep writing past its own completion.

    It stays append-only -- nothing is rewritten, seq continues -- so a reader that
    already folded the file sees only new lines.

    Best-effort by design: a closer that cannot be written leaves the tail open,
    which is the state every reader must already tolerate.
    """
    if kind != KIND_SESSION:
        return 0
    with _open_lock(_lock_path(kind, unit_id)):
        tail = _scan_tail(path)
        if tail.empty:
            return 0
        if tail.torn_offset is not None:
            _truncate(path, tail.torn_offset)
            tail = _scan_tail(path)
        # Check the fold before any content-aware repair. A damaged completion can
        # look exactly like an open turn once the ordinary reader skips it, and a
        # closer appended from that fold would state an outcome no writer observed.
        opened, skipped = _open_tail(path)
        if skipped is not None:
            _refuse_content_repair(unit_id, skipped)
            return 0
        orphan = _orphan_chunk_offset(path)
        # A chunk group whose citing entry never landed. Dropped BEFORE the closers,
        # so the closers do not sit on top of lines nothing can reach, and before the
        # tail is re-read, so their seqs continue from the truncated file.
        if orphan is not None:
            offset, first_seq, last_seq = orphan
            _truncate(path, offset)
            tail = _scan_tail(path)
            logger.warning(
                "dropped an unreachable chunk group from session ledger %r: seq %d-%d "
                "were written for a message whose citing entry never landed, so that "
                "one message is missing from this log",
                unit_id,
                first_seq,
                last_seq,
            )
            opened, skipped = _open_tail(path)
            if skipped is not None:
                _refuse_content_repair(unit_id, skipped)
                return 0
        if opened is None:
            return 0
        needs_newline = tail.needs_newline
        written = 0
        for entry in _closer_entries(opened, tail.last_seq + 1):
            line = require_entry_line(serialize(entry.to_dict()))
            _append_line(path, line, needs_newline=needs_newline)
            needs_newline = False
            written += 1
    logger.info(
        "closed an interrupted turn in session ledger %r: %d closer(s) appended",
        unit_id,
        written,
    )
    return written
