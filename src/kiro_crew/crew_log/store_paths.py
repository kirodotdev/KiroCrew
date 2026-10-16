"""Where a unit's crew log lives on disk, and the private-file primitives over it.

The data-home root of every kind (``crew_log_root``) and the checked form every walk uses
(``_checked_crew_log_root``), one unit's directory and file (``crew_log_dir``,
``crew_log_path``), how a segment is named and found (``segment_paths``,
``segment_first_seqs``, ``oldest_segment``, ``newest_segment``), the lock beside the log
(``_lock_path``, ``_open_lock``), the owner-only directory every write creates
(``_mkdir_private``), the clock (``now_ms``), and the store's one logger with its
traceback-as-text helper (``log_exception_text``). The owners that reach a unit's files on
disk build on these, and a new segment or lock name goes here; the cut counter's and the
trash hold's file names live with their owners.
"""

from __future__ import annotations

import logging
import os
import time
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from kiro_crew.config.paths import data_home
from kiro_crew.crew_log.errors import CODE_BAD_ROOT, CODE_INVALID_ID, CrewLogError
from kiro_crew.crew_log.schema import (
    KIND_CREW,
    KIND_MEMBER,
    KIND_SESSION,
    require_kind,
    require_unit_id,
)
from kiro_crew.owner_only_files import OWNER_ONLY_FILE_MODE, mkdirs_owner_only
from kiro_crew.platform_compat import file_lock, restrict_dir_to_owner
from kiro_crew.session_ledger import _store_name, resolved_within

#: The store's one logger, named for the facade every caller imports, so a record
#: names the store whichever owner writes it.
logger = logging.getLogger("kiro_crew.crew_log.store")

LOG_FILE = "log.jsonl"

#: How a segment past the first is named and found. ``log.jsonl`` holds seq 1
#: onward; ``log.<first_seq>.jsonl`` is a later segment.
_SEGMENT_STEM = "log"
_SEGMENT_SUFFIX = ".jsonl"
_SEGMENT_GLOB = f"{_SEGMENT_STEM}.*{_SEGMENT_SUFFIX}"
_LOCK_FILE = ".lock"


#: The one data-home leaf that holds every crew log, of every kind.
#:
#: A dedicated root rather than a subdirectory of each unit's existing home,
#: because the protection is what makes the record an authority: this leaf is
#: hidden from a sandboxed process by the OS (``sandbox._CREW_HIDDEN_LEAVES``)
#: and fenced from the agent's own file tools (``security.paths``), and both are
#: stated per LEAF. The ``sessions`` transcript root carries NEITHER entry, so a
#: session log placed there is protected by the tool gate alone and a
#: sandboxed subprocess can open it directly and forge the history a conductor
#: reads as fact. One root also means one entry per fence instead of one per
#: kind, so a third unit kind inherits the protection rather than needing a
#: reviewer to notice it was left out.
_ROOT_LEAF = "crew-log"

#: Directory under :data:`_ROOT_LEAF` that holds each kind's units.
_ROOT_DIR: dict[str, str] = {
    KIND_CREW: "crews",
    KIND_SESSION: "sessions",
    KIND_MEMBER: "members",
}


def now_ms() -> int:
    """Epoch milliseconds, the clock every ``time`` and ``createdAt`` uses."""
    return int(time.time() * 1000)


def crew_log_root(kind: str) -> Path:
    """Root directory holding every crew log of *kind*."""
    return data_home() / _ROOT_LEAF / _ROOT_DIR[require_kind(kind)]


def _checked_crew_log_root(kind: str) -> Path:
    """:func:`crew_log_root` for *kind*, refused when the directory is not the real one.

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
      ``crew-log`` tree, with both sides resolved so one symlinked ancestor cannot
      make two spellings of the same directory disagree.

    An absent directory is fine and is not an error: the callers that write
    create it, and the ones that read report their own absence. Only a directory
    that EXISTS and is wrong is refused.
    """
    root = crew_log_root(kind)
    try:
        is_link = root.is_symlink()
    except OSError as exc:  # pragma: no cover -- a stat fault on the parent
        raise CrewLogError(
            f"cannot establish the crew log root {root}: {exc}",
            code=CODE_BAD_ROOT,
        ) from exc
    if is_link:
        raise CrewLogError(
            f"refusing a linked crew log root: {root} is a symbolic link",
            code=CODE_BAD_ROOT,
        )
    if not root.exists():
        return root
    try:
        resolved = root.resolve()
        # Anchored at the data home, NOT at ``<home>/crew-log`` resolved: resolving
        # the parent would accept a linked ``crew-log`` -- both sides would then
        # spell the link's target and agree, which is the same escape one level up.
        canonical = data_home().resolve() / _ROOT_LEAF / _ROOT_DIR[require_kind(kind)]
    except (OSError, RuntimeError) as exc:
        raise CrewLogError(
            f"cannot resolve the crew log root {root}: {exc}",
            code=CODE_BAD_ROOT,
        ) from exc
    if resolved != canonical:
        raise CrewLogError(
            f"refusing a crew log root outside the data home: {root} resolves to {resolved}",
            code=CODE_BAD_ROOT,
        )
    return root


def crew_log_dir(kind: str, unit_id: str) -> Path:
    """The validated directory for one unit's crew log. Does not create it.

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
    resolved = resolved_within(_checked_crew_log_root(kind), _store_name(unit_id))
    if resolved is None:
        raise CrewLogError(
            f"path traversal blocked for crew log id: {unit_id!r}",
            code=CODE_INVALID_ID,
            field="id",
        )
    return resolved


def crew_log_path(kind: str, unit_id: str) -> Path:
    """The crew log file for one unit."""
    return crew_log_dir(kind, unit_id) / LOG_FILE


def _numbered_segment_seq(name: str) -> int | None:
    """The number in a ``log.<n>.jsonl`` name, or ``None`` for any other name.

    Only the canonical spelling ``str(n)`` counts: ASCII digits, no leading
    zero. ``str.isdigit`` also accepts a superscript, which ``int`` cannot
    parse, so a stray ``log.².jsonl`` would raise out of every read of the
    unit. ``int`` does accept Arabic-Indic digits and a leading zero, so
    ``log.١٢.jsonl`` or ``log.05.jsonl`` would pass for a real segment and
    break the seq continuity check. All three are stray files, not segments.
    """
    prefix = f"{_SEGMENT_STEM}."
    if not name.startswith(prefix) or not name.endswith(_SEGMENT_SUFFIX):
        return None
    middle = name[len(prefix) : -len(_SEGMENT_SUFFIX)]
    if not (middle.isascii() and middle.isdecimal()):
        return None
    number = int(middle)
    return number if str(number) == middle else None


def _segment_first_seq(path: Path) -> int | None:
    """The first seq declared by a canonical segment filename, or ``None``."""
    if path.name == LOG_FILE:
        return 1
    first = _numbered_segment_seq(path.name)
    if first is None or first <= 1:
        return None
    return first


def segment_paths(kind: str, unit_id: str) -> list[Path]:
    """Every segment of one unit's crew log, in ascending first-seq order.

    Retention is deleting whole segments off the FRONT, which is why the format
    is segments rather than one growing file: dropping the oldest lines out of a
    single file would rewrite it, and this store's whole guarantee is that a
    written line is never rewritten. Deleting a segment leaves every remaining
    line byte-identical, so retention costs a reader the old entries and costs
    the format nothing.

    ``log.jsonl`` is the segment that starts at seq 1 and is the only one a
    writer creates today; a later segment is ``log.<first_seq>.jsonl``. The
    first-seq is IN the name so ordering needs no file read, and so a reader can
    tell a gap at the front (retention) from a gap in the middle (damage).
    """
    directory = crew_log_dir(kind, unit_id)
    found: list[tuple[int, Path]] = []
    head = directory / LOG_FILE
    if head.is_file():
        found.append((1, head))
    for candidate in directory.glob(_SEGMENT_GLOB):
        first = _segment_first_seq(candidate)
        if first is None:
            # Not a segment: a neighbour that merely shares the prefix. Ignored
            # rather than refused, so an editor's backup file cannot make a
            # readable crew log unreadable.
            continue
        found.append((first, candidate))
    found.sort(key=lambda pair: pair[0])
    return [path for _first, path in found]


def segment_first_seqs(kind: str, unit_id: str) -> list[int]:
    """The first seq of every surviving segment, ascending.

    The companion to :func:`segment_paths`, and the reason the first-seq is in the
    file NAME: telling a gap at the front from a gap in the middle needs only the
    directory listing, so the decision costs no file read and stays correct on a
    crew log too large to scan.
    """
    return [
        first
        for path in segment_paths(kind, unit_id)
        if (first := _segment_first_seq(path)) is not None
    ]


def _lock_path(kind: str, unit_id: str) -> Path:
    return crew_log_dir(kind, unit_id) / _LOCK_FILE


def oldest_segment(directory: Path) -> Path | None:
    """The surviving segment of *directory* with the lowest first seq, or ``None``.

    Where a unit's history starts TODAY: ``log.jsonl`` while it survives,
    otherwise the lowest-numbered later segment retention left behind. One
    ``stat`` in the common case: the head segment is checked by name before the
    directory is listed.
    """
    head = directory / LOG_FILE
    try:
        if head.is_file():
            return head
        found = [
            (first, child)
            for child in directory.iterdir()
            if (first := _segment_first_seq(child)) is not None
        ]
    except OSError:
        return None
    if not found:
        return None
    found.sort(key=lambda pair: pair[0])
    return found[0][1]


def newest_segment(directory: Path) -> Path | None:
    """The surviving segment of *directory* with the HIGHEST first seq, or ``None``.

    Where a unit's history ENDS today, which is the half :func:`oldest_segment`
    cannot answer: a decision recorded after the session opened lands at the end of
    the newest segment, not behind the header of the oldest one.

    No name shortcut. ``log.jsonl`` is the oldest segment while it survives, so the
    newest is only knowable from the listing -- and a unit that has never been
    rotated has exactly one segment, which the listing finds in the same call.

    Raises what the listing raises. ``None`` therefore means one thing -- no surviving
    segment -- rather than standing for an unreadable directory as well, which is the
    distinction the tree-edge caller decides on: a unit whose listing failed has not
    told anyone it records no decision, and reported as though it had, its stale
    checkpoint edge survives as the tree's answer.
    """
    found = [
        (first, child)
        for child in directory.iterdir()
        if (first := _segment_first_seq(child)) is not None
    ]
    if not found:
        return None
    found.sort(key=lambda pair: pair[0])
    return found[-1][1]


def _is_segment_name(name: str) -> bool:
    """Whether *name* is segment-SHAPED, for ordering a removal.

    Deliberately broader than :func:`_segment_first_seq`, which answers the
    different question of whether a file is a canonical segment a reader may
    trust: that one rejects ``log.1.jsonl`` and ``log.0.jsonl``, because no
    real segment starts below seq 2. Removal must not inherit that rule. It
    deletes every file in the unit's directory whatever its name, and this
    predicate only decides what goes FIRST, so a stray numbered file is better
    grouped with the segments than left to the tail of the pass -- it is history
    by shape, and the ordering exists so the identity files outlive the history.
    The number itself must still be spelled as a reader parses it, so a name a
    reader ignores as a stray (``log.².jsonl``, ``log.05.jsonl``) is not history.
    """
    return name == LOG_FILE or _numbered_segment_seq(name) is not None


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
    path.touch(mode=OWNER_ONLY_FILE_MODE, exist_ok=True)
    with open(path, "r+") as handle:
        with file_lock(handle.fileno(), exclusive=True):
            yield


#: Directories this process has already failed to restrict. Every append goes
#: through :func:`_mkdir_private`, so a filesystem that refuses ``chmod`` would
#: otherwise log a full traceback twice per entry -- turning one true fact about the
#: host into a flood that buries the entries it is warning about. Warn once per
#: directory instead, like the boot-time restriction does.
_restrict_failed: set[str] = set()


def log_exception_text(log: logging.Logger, level: int, msg: str, *args: object) -> None:
    """Log the active exception with its traceback RENDERED to text, never as ``exc_info``.

    Every caller sits in a frame that holds a ``CrewLog`` handle (a method's ``self``, a
    local ``handle``), and a handle's write lease is released by a finalizer when the
    handle is dropped. An ``exc_info`` triple on the record keeps the traceback, the
    traceback keeps that frame, and a handler that keeps records (a ``MemoryHandler``, a
    test harness) then keeps the handle -- and its lease -- for as long as it keeps the
    record. A string keeps nothing; the render is skipped when the level is off.
    """
    if not log.isEnabledFor(level):
        return
    log.log(level, msg + "\n%s", *args, traceback.format_exc().rstrip())


def _mkdir_private(directory: Path) -> None:
    """Create *directory* and its parents owner-only.

    A bare ``mkdir`` takes the process umask, which is commonly world-readable, and
    these directories hold conversation bodies. The eager tightening at
    ``<home>/crew-log`` normally makes a leaf's own mode moot -- an unreadable parent
    is enough -- but it is best-effort, and this runs on the path taken when it did
    not succeed. So the guarantee is asserted per directory as well as once at the
    root, rather than resting on a parent that may not have been tightened.

    Already-correct is the common case and costs nothing: this runs on EVERY append,
    so the mode is checked before it is set and the ``chmod`` is skipped when the
    directory is already owner-only.

    Best-effort for the same reason the root's is: a filesystem that refuses the mode
    change must not make a crew log unwritable, and the sandbox mask and the file-tool
    fence still stand. Every level this creates is born ``0700`` (a ``parents=True``
    mkdir would leave the intermediate ``crew-log/<kind>`` at the umask default); the
    ``chmod`` below is for the leaf that already existed.
    """
    mkdirs_owner_only(directory)
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
            log_exception_text(
                logger,
                logging.WARNING,
                "Cannot restrict %s to owner-only; it may be readable by other users",
                directory,
            )
