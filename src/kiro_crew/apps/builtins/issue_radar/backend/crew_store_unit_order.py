"""The order a crew's crew log units first recorded in.

The ``crews/<crew_id>.unit-order`` file, read and appended through holds that pin
every directory of its path without following a link. Callers reach every name
here through :mod:`kiro_crew.apps.builtins.issue_radar.backend.crew_store`.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from kiro_crew import atomic_write as atomic_write_module
from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write

from . import store
from .crew_store_records import _crews_path, _require_crew_id, logger

#: The crew's units in the order they first RECORDED, one id per line, kept beside the
#: crew's record (``crews/<crew_id>.unit-order``; NOT inside ``crews/<crew_id>/``,
#: which is the pre-projection items dir the one-time carry reads, and whose mere
#: existence it takes as legacy state to carry). Append order is what makes it
#: causal: a unit is written here by the write that records into it, so the sequence
#: is what happened, not what a clock said. Unit headers carry a wall clock, and a
#: clock stepped backward before a replacement unit was created sorts the
#: replacement BEFORE its predecessor, which applies a retired session's phases over
#: the current ones -- on the READ path, where no caller is inside a unit to pin it
#: last. The newest ids are kept.
_UNIT_ORDER_SUFFIX = ".unit-order"
_MAX_ORDERED_UNITS = 64
#: Reads at most this many bytes of the order file, so a file that grew before the
#: bound existed cannot make a crew's every cycle read unboundedly.
_MAX_ORDER_READ_BYTES = _MAX_ORDERED_UNITS * 2 * 256


def _unit_order_path(owner: str, repo: str, crew_id: str, root: Path | None) -> Path:
    """The order file's path: no component below the app's data root is created or probed.

    Built from :func:`_crews_path`, not :func:`crews_dir`, because the hold its callers
    take must be what FIRST resolves this chain. Creating it by name a line earlier is
    the unheld resolution the hold exists to prevent, and it would also make a read
    create the store it reads.
    """
    return _crews_path(owner, repo, root) / f"{_require_crew_id(crew_id)}{_UNIT_ORDER_SUFFIX}"


def _recorded_unit_order(owner: str, repo: str, crew_id: str, root: Path | None) -> tuple[str, ...]:
    """The units this crew recorded into, oldest first; ``()`` when none is recorded.

    Deduplicated, then the newest :data:`_MAX_ORDERED_UNITS` kept. A failure to read
    answers ``()``: the caller falls back to header order, which is what every read
    did before the order existed. The file is opened refusing a link at its name
    (:func:`platform_compat.open_file_no_reparse`): the order lives where a sandboxed
    agent may be able to plant a link, and a read through one would take another
    file's lines for unit ids -- and, since the writer re-states what it read, copy
    them into this crew's order file.
    """
    try:
        path = _unit_order_path(owner, repo, crew_id, root)
        # Beside the path derivation, and for the same reason: the bound resolves through
        # :func:`store.app_data_dir`, which mkdirs, so it fails where that fails.
        bound = _chain_depth_bound(root)
    except (OSError, ValueError):
        return ()
    return _read_unit_order_held(path, max_depth=bound)


def _read_unit_order_held(path: Path, *, max_depth: int) -> tuple[str, ...]:
    """:func:`_recorded_unit_order_at` with *path*'s parent chain held while it reads.

    The read names the whole path, so where the write cannot go through a descriptor its
    ANCESTORS carry the same exposure as the write's: resolving a junction aimed at a UNC
    share is itself an outbound authentication as this process, to a host whoever planted
    the link chose. The leaf differs -- the write screens it with an ``lstat``-based
    predicate and the read settles it inside the open -- so what this hold adds is the
    ancestors, which neither of those touches. The chain is held first and the name resolved under it,
    by :func:`_hold_chain_for_by_name_use`, which creates nothing -- an absent component
    means no order file can exist, and a read must not create the store it reads.

    An absent or unholdable component answers ``()``, as an unreadable file does: the
    caller falls back to header order. A chain the walk REFUSES to chase -- a relative
    path, or one past its *max_depth* -- answers the same way, because a read
    that cannot hold its ancestors has nothing to say about the order file under them,
    and header order is the fold's answer for a crew whose order cannot be read.
    """
    try:
        held = _hold_chain_for_by_name_use(path.parent, max_depth=max_depth)
    except (OSError, ValueError):
        return ()
    try:
        return _recorded_unit_order_at(path)
    finally:
        _release_held(held)


def _recorded_unit_order_at(path: Path) -> tuple[str, ...]:
    """The units recorded in *path*, oldest first; ``()`` when it holds none.

    Takes the path its caller already derived, so a caller holding the parent chain
    reads the object it holds instead of resolving the same name a second time.
    Whatever protection that name needs belongs to the caller: this is the parse.

    The leaf is settled OFF ITS OWN DESCRIPTOR, never by a name. An ``is_file`` probe
    would be an ``os.stat``, which FOLLOWS a reparse point: a junction at the order
    file's own name aimed at a remote share would be traversed by the probe -- the
    outbound authentication this module screens for -- before the open could refuse it.
    So the open comes first and the kind is read from the descriptor it returns.

    Three properties the descriptor route has to carry itself, because a name probe is
    not there to carry them:

    * The open is NON-BLOCKING. ``O_RDONLY`` on a FIFO waits for a writer that a planted
      FIFO never has, and this runs on a crew's every read -- and inside the write lock
      on the recording path, so one FIFO would stop every update of that crew.
    * Only a REGULAR file is read. A directory opens successfully on POSIX, and a FIFO
      or device opened non-blocking does too, so ``S_ISREG`` off the descriptor is what
      refuses them.
    * The descriptor is closed exactly once on every path. ``os.fdopen`` TAKES the
      descriptor on entry and closes it itself on its own failure as well as on success,
      so ownership passes at the call and not at its return: closing here after it
      raised would close a number another thread may already have reopened.

    Every refusal answers ``()``, which the caller reads as "no order recorded".
    """
    try:
        fd = platform_compat.open_file_no_reparse(path, nonblocking=True)
    except (OSError, ValueError):
        return ()
    try:
        regular = stat.S_ISREG(os.fstat(fd).st_mode)
    except OSError:
        os.close(fd)
        return ()
    if not regular:
        os.close(fd)
        return ()
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            text = handle.read(_MAX_ORDER_READ_BYTES)
    except (OSError, ValueError):
        return ()
    seen: list[str] = []
    known: set[str] = set()
    for line in text.splitlines():
        unit = line.strip()
        if unit and unit not in known:
            known.add(unit)
            seen.append(unit)
    return tuple(seen[-_MAX_ORDERED_UNITS:])


def _record_unit_order(
    owner: str, repo: str, crew_id: str, session_id: str, root: Path | None
) -> None:
    """Note that *session_id* is the unit this crew records into now.

    Called under the crew's write lock once that unit's entry is read back from its
    own log by content, so a line here always names a unit whose append LANDED and a
    refused or queued write cannot claim precedence it never earned. A unit already
    newest is a bare read; one recorded earlier that records again is MOVED to the
    end, not appended twice, so the fold never folds a unit twice. The file is
    compacted to the bound once it outgrows it. Best-effort: a crew whose order
    cannot be written folds its units in header order instead.

    The path is derived without resolving any component the hold covers, and every
    component of its parent is then held (:func:`_hold_chain_for_by_name_use`) for as
    long as the read AND the write take. The read names the whole path too, so a chain
    held only for the write would leave that resolution -- and, where junctions exist, an
    outbound authentication through one -- outside every screen. The write walks the chain again
    under this hold; that walk cannot reach other objects, because nothing a hold
    covers can be renamed or deleted while it lives, so it re-proves the chain rather
    than opening a second window onto it.
    """
    if not session_id:
        return
    try:
        # INSIDE the guard on purpose: resolving the bound reaches
        # :func:`store.app_data_dir`, which mkdirs, so it can fail for the same reasons
        # the path derivation can. Computed above this ``try`` it would escape the
        # ``except`` below and turn a documented best-effort write into a raised one.
        bound = _chain_depth_bound(root)
        path = _unit_order_path(owner, repo, crew_id, root)
        held = _hold_chain_for_by_name_use(path.parent, max_depth=bound)
        try:
            known = _recorded_unit_order_at(path)
            if known and known[-1] == session_id:
                return
            ordered = tuple(u for u in known if u != session_id) + (session_id,)
            _write_unit_order(path, ordered[-_MAX_ORDERED_UNITS:], max_depth=bound)
        finally:
            _release_held(held)
    except (OSError, ValueError):
        logger.warning("crew ledger: could not record crew %s's unit order", crew_id, exc_info=True)


def _write_unit_order(path: Path, lines: tuple[str, ...], *, max_depth: int) -> None:
    """Replace *path* with *lines*: a reader sees the old file or the new, and no link
    is followed on the way.

    The file is never appended to in place -- an ``open("a")`` follows a link planted
    at the name and would append to whatever it points at -- and never staged under a
    predictable temp name, which a link could be planted at just the same. It is
    written by :func:`atomic_write`: a UNIQUELY named temp file created ``O_EXCL``
    and renamed over the name, which replaces a planted link rather than writing
    through it. Where the platform supports descriptor-relative writes the parent is
    PINNED first (``O_DIRECTORY | O_NOFOLLOW``), so a link planted at the directory
    is refused too.

    Elsewhere -- the platform with no descriptor-relative rename, which is also the
    platform that has junctions -- the write still names the path, so the screen
    alone is not enough: it answers about a NAME, and the write below resolves that
    same name again. :func:`_hold_chain_no_follow` closes the gap by holding every
    component of the parent open for as long as the write takes, so the object the
    screen inspected is the object the write reaches. The name screens stay on top of
    it: :func:`platform_compat.is_link_or_junction` unlike ``is_symlink`` also
    answers for a Windows directory junction, and an ``islink``-only check would
    leave this branch with no leaf boundary at all.
    Whole-file writes are cheap here: the file holds at most
    :data:`_MAX_ORDERED_UNITS` short lines.
    """
    content = "".join(f"{line}\n" for line in lines)
    if atomic_write_module.pinned_parent_replace_supported():
        path.parent.mkdir(parents=True, exist_ok=True)
        parent_fd = platform_compat.pin_directory(path.parent)
        try:
            atomic_write(path, content, fsync=True, newline="", parent_dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
        return
    held = _hold_chain_no_follow(path.parent, max_depth=max_depth)
    try:
        if platform_compat.is_link_or_junction(path) or platform_compat.first_linked_ancestor(path):
            raise OSError(
                f"{path} is reached through a link; the unit order is not written through it"
            )
        atomic_write(path, content, fsync=True, newline="")
    finally:
        _release_held(held)


#: How many components BELOW the store's base :func:`_hold_chain_no_follow` may hold.
#:
#: The walk costs one open per component, so an unbounded depth is a stall INSIDE the
#: hold, before any of the work the hold protects begins. What must be bounded is the
#: part a caller's INPUT can grow, not the whole absolute path: everything above the
#: base is the operator's own data root, and spending one budget on both makes a deep
#: but legitimate data home refuse every write for that home -- which drops the unit
#: order permanently and leaves the header-clock fallback in force, the exact failure
#: the order file exists to prevent (see :data:`_UNIT_ORDER_SUFFIX`).
#:
#: Below the base the layout is ``repos/<owner>/<repo>/crews``: three fixed components
#: plus ``owner``, which is a provider NAMESPACE carrying one component per level of
#: nesting. GitLab caps its own nesting at twenty levels, so the deepest legitimate
#: suffix is twenty-three, and this leaves nine components of headroom above that while
#: still refusing a namespace no provider will produce.
_MAX_SUFFIX_DEPTH = 32


def _chain_depth_bound(root: Path | None) -> int:
    """The absolute-component bound for a chain under *root*, for the walk's ``max_depth``.

    The bound is the caller's DECLARED intent, computed where the base is known, rather
    than something the walk infers from the path it is handed. A walk that derived it
    would read policy out of data and change what it refuses when the data home moves;
    computed here it is checkable at each call, and a call that cannot state a bound
    cannot walk at all.

    *root* is resolved exactly as :func:`_crews_path` resolves it, so the bound and the
    path it bounds cannot disagree about where the store's base is.
    """
    base = root if root is not None else store.app_data_dir(store.APP_NAME)
    return len(base.parts) + _MAX_SUFFIX_DEPTH


def _hold_chain_no_follow(directory: Path, *, max_depth: int) -> list[int]:
    """Open every component of *directory* without following a link, and keep them open.

    Returns the descriptors, outermost first, and the CALLER closes them. Holding
    them is the whole point, so there is no context manager that could tempt a caller
    into releasing them before the work they protect: the window this closes is
    between the screen and the write, and a descriptor released at the end of the walk
    protects nothing.

    Each component is opened by :func:`platform_compat.pin_directory`, which opens a
    reparse point AS ITSELF rather than following it, so a junction or symlink sitting
    at a component fails the open instead of being traversed. That refusal is the
    security property and it is atomic: no ``lstat`` verdict is taken and then trusted,
    which matters on Windows because resolving a junction aimed at a UNC share is
    itself an outbound authentication as this process, to a host whoever planted the
    link chose. Components are walked ROOT-FIRST, so each open runs only after every
    component above it is held and proved.

    What the hold buys differs by platform, and the caller relies on both halves:

    * Windows: the handle omits ``FILE_SHARE_DELETE``, so while it lives that
      directory can be neither renamed nor deleted -- nor can anything above it -- so
      the name a by-name write below resolves stays bound to the object this walk
      proved. What it does not cover is reparse data set IN PLACE on a component,
      which the platform accepts only while that directory holds no children --
      which is why this walk creates nothing, as the note below records.
    * POSIX: the descriptor pins the inode the walk verified. A rename is not blocked
      (POSIX has no such lock), which is why the POSIX callers write through the
      descriptor instead; this branch exists for the platform that cannot.

    NOTHING is created here, on the read path or the write path. A component that is
    absent raises ``FileNotFoundError`` and a component that exists and cannot be opened
    refuses: becoming the boundary the walk stops at would leave a resolution passing
    through an object nothing proved, and CREATING it would open a window this hold
    cannot close. A directory just created is empty, Windows accepts reparse data only
    on a directory that holds no children, and the child is opened BY NAME on the next
    step -- so between the two an outside writer can re-point it, and the child open
    resolves through whatever it points at. Withholding WRITE sharing on the hold cannot
    close that either: setting reparse data on a directory and adding a child to it need
    the same write access, so a share mode that denies it to another handle denies it to
    this process as well, and held that way on a Windows runner the write's own rename
    fails with a sharing violation on every attempt of the retry budget in
    :func:`atomic_write.replace_with_retry`. Refusing is the fail-closed answer, and it
    costs little: the caller's contract is best-effort, so a crew whose chain is absent
    folds its units in header order until an ordinary crew-record write puts the
    directory back.

    The walk starts at *directory*'s own root, so a caller must not hand it a UNC
    path -- the first open would then be the outbound authentication this exists to
    prevent. :func:`_unit_order_path` builds from :func:`data_home`, which is local.

    Two refusals come before the first open, both ``ValueError``, because neither is a
    fact about the filesystem and neither is worth one open to discover:

    * A path that is not ABSOLUTE is refused. Its components resolve against a current
      directory this walk never inspects, so the chain it would hold is not the chain
      the caller named. ``Path.is_absolute`` is the test, which on Windows also refuses
      a rooted path carrying no drive -- that one is anchored to whichever drive is
      current, which is the same unexamined base under a different spelling.
    * A path deeper than *max_depth* components is refused. One open per component is
      the walk's whole cost, so depth is the one input that turns this guard into the
      delay it exists to prevent. The bound is REQUIRED and arrives from the call site,
      which knows the store's base and can therefore bound the part a caller's input
      grows rather than the operator's data root as well; see
      :func:`_chain_depth_bound`. A walk with no bound to state does not run.

    Root-first also decides the failure shape: a failure part-way releases what it took,
    because a half-held chain protects nothing and its descriptors would leak.
    """
    if not directory.is_absolute():
        raise ValueError(f"refusing to hold a chain under a relative path: {directory}")
    components = [*reversed(directory.parents), directory]
    if len(components) > max_depth:
        raise ValueError(
            f"refusing to hold a chain {len(components)} components deep, "
            f"over a bound of {max_depth}: {directory}"
        )
    held: list[int] = []
    try:
        for component in components:
            held.append(_pin_held(component))
    except BaseException:
        _release_held(held)
        raise
    return held


def _hold_chain_for_by_name_use(directory: Path, *, max_depth: int) -> list[int]:
    """The hold a BY-NAME read or write of the unit order needs under *directory*.

    ``[]`` where :func:`atomic_write.pinned_parent_replace_supported` answers yes: the
    write there goes through a pinned descriptor and the hold would buy nothing it does
    not already have, while changing what that platform does -- POSIX cannot block a
    rename, and refusing a component on the walk is behaviour of its own.

    Elsewhere the chain is walked and held, because that is the platform with junctions
    and a by-name resolution through one is an outbound authentication.
    """
    if atomic_write_module.pinned_parent_replace_supported():
        return []
    return _hold_chain_no_follow(directory, max_depth=max_depth)


def _pin_held(component: Path) -> int:
    """Hold *component* open without following a link, and create nothing.

    The pin withholds DELETE sharing, so while it lives the component can be neither
    renamed nor deleted, and the name the next step resolves stays bound to the object
    this open proved. It deliberately does not withhold WRITE sharing, and it creates
    nothing: :func:`_hold_chain_no_follow` carries the measurement behind both.

    Two refusal shapes, because the callers read them differently. ABSENCE passes through
    as ``FileNotFoundError``: it is the expected answer for a store that holds no order
    yet, and the walk's own tests turn on telling it apart. Every other ``OSError`` --
    a link at the name, a component that cannot be opened -- is re-raised naming the
    component, because that one reaches a log where the name is the whole diagnosis. A
    link reports something different on each platform, POSIX ``O_NOFOLLOW`` failing a
    symlink with ``ELOOP`` and a file with ``ENOTDIR`` while Windows opens the reparse
    point and raises off the descriptor, so the wrapping is what gives them one shape.
    """
    try:
        return platform_compat.pin_directory(component)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise OSError(
            f"{component} could not be held open; the unit order is not written under it"
        ) from exc


def _release_held(held: list[int]) -> None:
    """Close a held chain, innermost first, and keep going when one will not close."""
    for fd in reversed(held):
        try:
            os.close(fd)
        except OSError:
            logger.debug("crew ledger: a held unit-order component would not close")
