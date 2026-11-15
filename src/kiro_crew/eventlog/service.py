"""MemberEventLogService — the one door to per-member append-only logs.

Contract (implemented in this module; callers import only from here):

    svc = get_service()                       # lazy singleton rooted at the member crew log root
    svc.attach_broadcast(state.broadcast_ws)  # once, at dashboard startup
    svc.ensure(slug, name)                    # create the log + header if missing (migrates legacy files)
    ev = svc.append(slug, type, data)         # write + fsync, fold projections, push member_projection frames
    svc.snapshot(slug)                        # {"asOfSeq": int, "values": {key: view}}
    svc.history(slug, before=None, limit=50)  # newest-first page of envelopes
    svc.last_seq(slug)                        # 0 for an empty log
    svc.last_seqs()                           # {slug: last_seq} for every known log
    svc.slugs()                               # every member with a log on disk

All methods are synchronous. A write is one line appended under a per-slug
lock and fsync'd before it returns; projections fold in the same call and the
broadcast is only enqueued. Callers on the event loop pay one fsync per
append, which is the pilot's accepted cost.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TextIO

from kiro_crew import platform_compat
from kiro_crew.atomic_write import fsync_dir
from kiro_crew.crew_log.checkpoint import PrefixWitness, witness_mapping
from kiro_crew.crew_log.schema import KIND_MEMBER
from kiro_crew.eventlog import members_projections, types
from kiro_crew.eventlog.log import APPEND_CONTENTION_SECONDS, MemberLog
from kiro_crew.eventlog.members_projections import all_units
from kiro_crew.eventlog.types import Event
from kiro_crew.owner_only_files import OWNER_ONLY_FILE_MODE, mkdirs_owner_only
from kiro_crew.projection import EMPTY_WATERMARK, DirectoryCheckpointStore, ProjectionRegistry

logger = logging.getLogger(__name__)

#: The dashboard's fan-out signature: its payload is ``websocket_hub.WsPayload``,
#: spelled out here because this layer does not import the dashboard.
Broadcast = Callable[[str, Mapping[str, Any]], None]

#: Events a prime must have folded past its savepoint before a new one is written.
#: A savepoint may LAG (an older one replays more tail to the same value), so this
#: keeps every load from rewriting one file per unit. Matches the crew log's
#: ``MIN_ADVANCE_ENTRIES``.
_SAVEPOINT_MIN_ADVANCE = 256

#: How many times a closer re-folds and re-asks its predicate after losing the tail
#: to another process. Small on purpose: the retry covers a one-commit collision;
#: under sustained foreign writes declining is right, and the next read decides again.
_CLOSER_TAIL_ATTEMPTS = 3


class CloserTailContention(Exception):
    """Every attempt to place a closer lost the tail to another process.

    Distinct from ``None`` (the closer does not apply): here nothing was learned. A
    caller that runs ONCE, such as the startup sweep, must handle it, or the state
    it was closing stays open until the next restart.
    """

    def __init__(self, slug: str, type: str) -> None:
        super().__init__(f"closer {type!r} for slug {slug!r} lost the tail on every attempt")
        self.slug = slug
        self.type = type


def _redact_projection_value(value: object) -> object:
    """Redact every string in a projection view before it leaves over the WS.

    Runs the shared exfiltration-URL + credential chain the dashboard's HTTP
    reads use, recursively, so a credential- or presigned-URL-shaped value an
    operator planted in an activity ``project`` (or any nested string) cannot
    reach the browser through the live projection push.

    The string branch also applies the URL-secret-parameter layer
    (``external_text.redact_url_secret_params``) that the sibling roster/agents
    egress chain (``external_text.redact_external_text``, via ``_roster_mask``)
    carries, so the two chains agree by construction. That layer catches a short
    URL-embedded credential such as ``https://h.example/v1?api_key=abc123`` -- a
    query under the exfiltration pass's length floor whose value is not
    credential-shaped -- which the exfiltration and credential passes leave
    untouched. Both the roster projection block (``GET /api/members``) and the
    live member-projection WS push fold this function, and the roster ROW masks
    the same leaf, so projection and row render it the same way.
    """
    from kiro_crew.external_text import redact_url_secret_params
    from kiro_crew.security.exfil import redact_exfiltration_urls
    from kiro_crew.security.redaction import redact_credentials

    if isinstance(value, str):
        text, _ = redact_exfiltration_urls(value)
        text, _ = redact_credentials(text)
        return redact_url_secret_params(text)
    if isinstance(value, dict):
        # Redact keys too, not just values: a contributed projection key is
        # app-authored (`<app>/<name>`) and a nested data key can be arbitrary
        # agent text, so a credential- or URL-shaped key would otherwise cross
        # unredacted. Keys are strings in JSON; a non-string key is left as-is.
        out: dict = {}
        for k, v in value.items():
            rk = _redact_projection_value(k) if isinstance(k, str) else k
            out[rk] = _redact_projection_value(v)
        return out
    if isinstance(value, list):
        return [_redact_projection_value(v) for v in value]
    return value


#: Suffix the legacy activity file is renamed to once the fold has run. Hygiene
#: rather than the protection: the completion record is the fenced
#: :data:`LEGACY_FOLDED_MARKER`, because the member directory is writable by the
#: party that record defends against. Renaming keeps a member's own rows readable
#: under a retired name instead of deleting them, and keeps the byte budget off a
#: file already folded.
LEGACY_MIGRATED_SUFFIX = ".migrated"

#: Records, inside the member's own FENCED log directory, that the legacy activity
#: fold ran to completion for that member. It lives here rather than beside the
#: legacy file because the member directory is writable by the party this fact
#: defends against: a marker there can simply be deleted, and a fresh
#: ``activity.jsonl`` then imports as trusted history. A file under the crew-log
#: root cannot be written by that party at all.
#:
#: A file rather than an event in the log: an event would sit in every member's log
#: forever and shift the seq of every event after it, an on-disk cost every member
#: pays for a concern that ends with the first successful pass. ``unit_ids`` skips
#: any child of a unit directory that is not a log segment, so a sidecar here is not
#: mistaken for one.
LEGACY_FOLDED_MARKER = ".legacy-activity-folded"


def _legacy_folded_marker_path(slug: str) -> Path | None:
    """The fenced completion marker for *slug*, or None if it cannot be located."""
    from kiro_crew.crew_log.schema import KIND_MEMBER
    from kiro_crew.crew_log.store import crew_log_dir

    try:
        return crew_log_dir(KIND_MEMBER, slug) / LEGACY_FOLDED_MARKER
    except Exception:
        logger.debug("fenced legacy marker path unavailable for %r", slug, exc_info=True)
        return None


def _legacy_fold_completed(slug: str) -> bool:
    """Whether the legacy activity fold has already completed for *slug*.

    Fails CLOSED on an unreadable answer, meaning it reports NOT completed. That is
    the safe direction here for a reason worth stating: reporting completed would
    skip the fold and silently drop a member's real history, while reporting not
    completed re-reads a source the counted dedupe already makes idempotent. The
    cost of the wrong answer is asymmetric, so the fallback follows the cheap side.
    """
    marker = _legacy_folded_marker_path(slug)
    if marker is None:
        return False
    try:
        return marker.exists()
    except OSError:
        logger.debug("fenced legacy marker unreadable for %r", slug, exc_info=True)
        return False


def _legacy_binding_present(slug: str) -> bool:
    """Whether a legacy DM-binding file EXISTS for *slug*.

    :func:`members.read_dm_binding` is total: missing, unreadable and malformed all
    read as "not bound", but the fold must know whether it READ the source.

    Fails CLOSED (reports present): that costs a re-read the counted dedupe makes
    safe, while absent would drop a binding. ``stat`` is used because
    ``Path.exists`` answers False for every error; only a missing file is absent.
    """
    from kiro_crew import members

    try:
        members.dm_binding_path(slug).stat()
    except FileNotFoundError:
        return False
    except Exception:
        logger.debug("legacy binding path unanswerable for %r", slug, exc_info=True)
        return True
    return True


#: How much of a legacy activity file the fold will read. The file is
#: agent-writable and the fold runs from ``ensure``, which the roster
#: projection calls, so an unbounded read sits on a request path.
MAX_LEGACY_ACTIVITY_BYTES = 8 * 1024 * 1024

#: The legacy source went, with this member's unit lease held over the removal.
_CLEAN_REMOVED = "removed"
#: Nothing was touched: the lease could not be had, or something the removal needs
#: was unreadable. The source and whatever gates it are both still in place.
_CLEAN_KEPT = "kept"
#: The store's absence was STALE: a peer created this member's unit (and likely
#: folded the legacy source into it) before this hold. Nothing is removed; the
#: caller re-runs ``remove_unit`` against the unit that now exists.
_CLEAN_RACED = "raced"

_singleton: "MemberEventLogService | None" = None
_singleton_lock = threading.Lock()


def _remove_legacy_activity(slug: str) -> bool:
    """Remove *slug*'s pre-log activity files. Never raises; answers whether all went.

    The answer is what the unit's own removal is gated on, so it is true only when
    none of the four names is left -- including one left deliberately, because a name
    that is a link is a name the fold can still read through. Anything short of that
    reports false, and the caller then keeps the unit, which keeps the marker that
    stops the fold reading what survived here.
    The companion to removing a member's unit. These rows are the member's own
    history from before the log existed, and they sit OUTSIDE the unit while the
    marker recording that they were folded sits inside it -- so taking the unit
    alone both leaves the history on disk and re-arms the fold, because the next
    fresh ``ensure`` finds no marker and reads the source again. Every name the
    fold reads is covered: the live file, its one rotation, and the retired names
    the fold renames them to.

    **The directory is PINNED to a descriptor and the leaves are unlinked by
    BASENAME against it, so there is no window between deciding and deleting.**
    ``member_dir`` resolves and then only containment-checks the result, so
    ``members/<slug>`` swapped for a link to a PEER's directory resolves inside the
    members root, passes that check, and hands back the peer's real directory --
    where these four names are ordinary files, so a link test on the leaves is false
    and the unlink destroys a live member's history. For a member whose fold has not
    run that file is the sole copy. ``members/<slug>`` is deliberately agent-writable,
    which is what makes the swap reachable rather than hypothetical.

    A test on the name cannot close that, whatever it tests FOR: the test and the
    unlink are separate syscalls, and whoever can plant the link chooses when to
    plant it. ``platform_compat.pin_directory`` refuses a link AS IT OPENS -- POSIX
    through ``O_DIRECTORY | O_NOFOLLOW``, Windows by opening a reparse point as
    itself -- so the refusal is the open rather than a prediction about it, and it
    answers for a junction as well as a symlink. Every unlink then names a basename
    against that descriptor, so it resolves against the directory that was inspected
    and a later swap of the NAME reaches nothing. Windows has no ``dir_fd``; there
    the same handle is what closes the window, because a directory held open without
    ``FILE_SHARE_DELETE`` can be neither renamed nor deleted, nor can any directory
    above it, so the by-path unlink under that hold cannot be redirected either.

    **``O_NOFOLLOW`` binds the FINAL component only, so the members ROOT is pinned
    first and the slug is opened relative to it.** ``members_root()`` is an
    unresolved path under the data home, and nothing seals the ``members`` component
    itself -- swapped for a link, it redirects an open of ``members/<slug>`` however
    carefully that leaf is no-followed, and the four unlinks land in a tree outside
    the member area entirely. Two pins settle it: the root refuses a link as it
    opens, and the slug is then opened THROUGH that descriptor, so neither name is
    re-resolved from a string afterwards. On Windows the root's own handle is what
    holds, since it blocks renaming ``members`` and everything above it.

    The leaf test is kept as well, and it is not a second guess at the directory: it
    covers a single file swapped for a link inside a directory that is genuinely this
    member's, where the worst case is removing a link instead of the file it names.

    Every name is attempted even after one refuses, so a single stuck file does not
    hide the rest; the refusal is carried to the answer rather than to an exception,
    because the caller needs a verdict it can act on and not a second failure mode.
    """
    from kiro_crew import members

    try:
        members.validate_slug(slug)
        named_dir = members.members_root() / slug
    except Exception:
        logger.debug("legacy activity path unavailable for %r", slug, exc_info=True)
        return False
    live = members.ACTIVITY_FILE_NAME
    retired = live + LEGACY_MIGRATED_SUFFIX
    names = (live, live + ".1", retired, retired + ".1")
    relative = os.open in os.supports_dir_fd and os.unlink in os.supports_dir_fd
    try:
        root = platform_compat.pin_directory(members.members_root())
    except FileNotFoundError:
        return True
    except OSError:
        logger.warning(
            "crew log: the members root is not a real directory; "
            "refusing to remove what its name reaches for %r",
            slug,
        )
        return False
    try:
        try:
            if relative:
                pinned = os.open(slug, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            else:
                pinned = platform_compat.pin_directory(named_dir)
        except FileNotFoundError:
            return True
        except OSError:
            logger.warning(
                "crew log: member directory for %r is not a real directory; "
                "refusing to remove what its name reaches",
                slug,
            )
            return False
        all_gone = True
        try:
            for name in names:
                try:
                    if relative:
                        if stat.S_ISLNK(os.lstat(name, dir_fd=pinned).st_mode):
                            logger.warning(
                                "crew log: legacy activity at %s is a link; leaving it in place",
                                name,
                            )
                            all_gone = False
                            continue
                        os.unlink(name, dir_fd=pinned)
                    else:
                        leaf = named_dir / name
                        if leaf.is_symlink():
                            logger.warning(
                                "crew log: legacy activity at %s is a link; leaving it in place",
                                name,
                            )
                            all_gone = False
                            continue
                        leaf.unlink(missing_ok=True)
                except FileNotFoundError:
                    continue
                except OSError:
                    logger.warning(
                        "crew log: could not remove legacy activity %s for %r", name, slug
                    )
                    all_gone = False
        finally:
            os.close(pinned)
        return all_gone
    finally:
        os.close(root)


LEGACY_PROVENANCE_KEY = "legacy_unverified"
"""Marks an activity record imported from the pre-fold legacy file.

The legacy activity file is agent-writable and is read on every ``ensure`` for a
member until the fenced completion marker exists, so rows folded in from it carry
this key: their provenance is the file, not this log. They are kept, not dropped.
:func:`_activity_key` strips the key, so adding it changes no row's migration
identity. The full rationale is ``docs/system-specs/modules/member-event-log.md``
section 8.
"""


def _activity_key(row: object) -> str:
    """A stable identity for one activity row, for migration dedupe.

    Canonical JSON with sorted keys, so two dicts that differ only in key order
    are one row. Falls back to ``repr`` for anything JSON cannot hold, which keeps
    an odd row comparable rather than crashing the migration that reads it.

    :data:`LEGACY_PROVENANCE_KEY` is STRIPPED before hashing, and that is what lets
    the marker be added at all. Identity here is the whole row, so a marked stored
    event and the unmarked file row it came from would otherwise be two different
    rows -- and every later pass would re-append the file's rows as new. Stripping
    also gives a stored row without the key, such as a live ``record_activity``
    entry, the same identity as that row with the key added, so a stored row of
    either shape is one match for the same legacy row.
    """
    if isinstance(row, dict) and LEGACY_PROVENANCE_KEY in row:
        row = {k: v for k, v in row.items() if k != LEGACY_PROVENANCE_KEY}
    try:
        return json.dumps(row, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return repr(row)


def _open_legacy_regular_file(path: Path) -> TextIO | None:
    """*path* opened for reading, or ``None`` if what it opened is not a regular file.

    The legacy path is AGENT-WRITABLE -- that is this module's own premise for
    keeping the completion marker in the fenced log root instead -- and the fold
    runs inside ``ensure`` under the per-slug lock, which the roster request path
    calls. So the party the marker defends against also chooses what KIND of
    thing sits at this path, and a plain ``open`` trusts that choice twice:

    * a FIFO blocks the open until a writer appears, which no writer ever has to
      be. That stalls the fold, the lock, and the request behind it, and it
      outlives a restart: the hang happens BEFORE the marker can be written, so
      the FIFO survives and the marker never lands.
    * a symlink redirects the read somewhere the member directory does not own.

    ``platform_compat.open_file_no_reparse`` is that open, named: ``O_NONBLOCK``
    makes the FIFO open return at once instead of waiting, and the ``fstat`` then
    refuses it for what it is, while the reparse-point refusal is settled in the
    SAME operation that opens -- on POSIX by ``O_NOFOLLOW``, and on Windows by
    ``FILE_FLAG_OPEN_REPARSE_POINT``, because Windows has no ``O_NOFOLLOW`` and a
    feature-detected ``getattr(os, "O_NOFOLLOW", 0)`` is therefore 0 there. The
    regular-file check does NOT cover that case: a link whose target is a regular
    file passes ``S_ISREG`` while the read has already been redirected. On a
    regular file ``O_NONBLOCK`` changes nothing about the reads.

    ``None`` means ONLY "this file is not something to migrate from" -- what the
    open reached is not a regular file. A missing file raises ``FileNotFoundError``,
    a reparse point at the name (on POSIX, a symlink) raises ``OSError``
    (``ELOOP``), and on Windows a directory raises ``IsADirectoryError``. A
    genuine I/O FAILURE is raised instead of
    being folded into ``None``, and the difference decides whether rows are lost:
    the caller keeps ``complete`` true when there is nothing to migrate, and the
    retirement it then writes is permanent and never re-runs the fold. If a transient
    open failure (EMFILE, a permission blip, an unresponsive mount) were reported as
    absence, the rows in an existing file would be retired UNREAD, with no recovery.
    The caller already separates the two: ``FileNotFoundError`` continues, any other
    ``OSError`` sets ``complete = False``.
    """
    # An OSError from the open propagates rather than being swallowed.
    # FileNotFoundError is an OSError subclass and the caller catches it FIRST, so a
    # genuinely absent file still reads as absence.
    fd = platform_compat.open_file_no_reparse(path, nonblocking=True)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            logger.warning(
                "legacy activity at %s is not a regular file; nothing is migrated from it",
                path,
            )
            os.close(fd)
            return None
    except OSError:
        os.close(fd)
        raise
    try:
        # ``errors="replace"``, not strict. The reads below are wrapped against a bad
        # JSON LINE (`except ValueError: continue`) but that guard sits on
        # `json.loads`, and a strict decode raises from `readline` INSTEAD -- one
        # frame further out, where nothing catches it. This file is agent-writable
        # and is read on every `ensure`, so a single malformed byte in it would
        # propagate out of the migration and make every later activity write fail:
        # the rows would be lost, permanently, on the strength of one torn write.
        # Replacement turns that byte into U+FFFD, which makes the line invalid JSON
        # and sends it down the skip path the loop already has for a bad row.
        return os.fdopen(fd, "r", encoding="utf-8", errors="replace")
    except OSError:
        os.close(fd)
        raise


def _read_legacy_activity_files(slug: str) -> tuple[list[dict], bool]:
    """Rows from the pre-log ``activity.jsonl.1`` then ``activity.jsonl``, oldest first.

    Deliberately reads the files by hand rather than through
    ``members.read_activity``: that function reads the event log, and the
    only caller here holds the per-slug lock it would need. Unparseable lines
    are skipped — the legacy writer was best-effort and never fsync'd, so a
    torn tail is expected, not corruption.
    """
    from kiro_crew import members

    rows: list[dict] = []
    complete = True
    try:
        base = members.member_dir(slug) / members.ACTIVITY_FILE_NAME
    except Exception:
        logger.debug("legacy activity path unavailable for %r", slug, exc_info=True)
        return rows, False
    if _legacy_fold_completed(slug):
        # The fold already completed for this member. Anything under the legacy name
        # now was written AFTER that, so importing it would let whoever wrote it
        # forge a trusted activity row: this reader has no other way to tell a row
        # the migration has not seen yet from one that appeared after it finished.
        #
        # The fact is read from the FENCED log directory, never from a marker beside
        # the legacy file. A marker in the member directory is writable by the same
        # party the check defends against, so deleting it and writing a fresh
        # ``activity.jsonl`` would reopen the whole path -- a guard an adversary can
        # remove is not a guard. Retiring the source by rename is hygiene, and keeps
        # the byte budget off a file already folded; it is not the protection.
        return rows, True
    for path in (base.with_name(base.name + ".1"), base):
        try:
            # Streamed under a byte budget rather than read whole. This file is
            # agent-writable and is read on every `ensure`, which the roster
            # projection calls, so an oversized one would be allocated in full on a
            # request path. Bounded here rather than by the member module's reader,
            # which reads the event LOG and touches no file this could bound.
            budget = MAX_LEGACY_ACTIVITY_BYTES
            handle = _open_legacy_regular_file(path)
            if handle is None:
                continue
            with handle:
                while True:
                    # Capped per READ, not per line. Iterating the handle hands back
                    # a whole line, so one row written without a newline is
                    # materialised in full before any budget could look at it --
                    # which is the same mistake as reading the file whole, just
                    # harder to see. `readline` takes the cap and stops there.
                    line = handle.readline(budget + 1)
                    if not line:
                        break
                    budget -= len(line)
                    if budget < 0:
                        logger.warning(
                            "legacy activity at %s exceeds %d bytes; the rest is not migrated",
                            path,
                            MAX_LEGACY_ACTIVITY_BYTES,
                        )
                        complete = False
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and row.get("ts"):
                        rows.append(row)
        except FileNotFoundError:
            continue
        except OSError:
            logger.debug("legacy activity read failed for %s", path, exc_info=True)
            complete = False
            continue
    return rows, complete


def _retire_legacy_activity(slug: str) -> bool:
    """Return whether the fenced fold-complete marker is durably recorded.

    Renames rather than deletes: the rows are the member's own history and this is a
    one-way migration, so the file is kept readable under its retired name. The
    MARKER is what closes the forgery path -- a reader that only counted rows could
    never tell an unmigrated row from one written after the migration finished.
    """
    from kiro_crew import members

    try:
        base = members.member_dir(slug) / members.ACTIVITY_FILE_NAME
    except Exception:
        logger.debug("legacy activity path unavailable for %r", slug, exc_info=True)
        return False
    # The fenced marker is written and made DURABLE BEFORE the legacy name is
    # freed. In the other order a crash between the rename and the marker leaves the
    # live name available with no marker recorded, and the next ensure folds whatever
    # an agent has since written there as trusted history -- the exact forgery the
    # marker exists to close, reopened by a power cut. This order's crash window
    # costs nothing: the marker is present, the rows were appended before this ran,
    # and the un-renamed file is simply never read again.
    #
    # Written for every member whose fold completed, including one with no legacy
    # file at all. A member with nothing to migrate is exactly the case a
    # rename-gated marker would miss, leaving the members with the LEAST to migrate
    # as the only ones whose legacy name would stay open for whoever writes it next.
    fenced = _legacy_folded_marker_path(slug)
    if fenced is None:
        return False
    try:
        mkdirs_owner_only(fenced.parent)
        fenced.touch(mode=OWNER_ONLY_FILE_MODE, exist_ok=True)
        # touch() puts the directory entry in the page cache only. Without this the
        # entry can be absent after a crash while the rename below has already
        # landed, which is the one combination the ordering above rules out.
        fsync_dir(fenced.parent)
    except OSError:
        # The entry is LEFT in place. A later pass runs this again for every member
        # whose fold completes -- a member already retired reads as a complete fold
        # with no rows -- so ``touch`` here can meet a marker an earlier pass made
        # durable, and removing it on a sync failure would free the live legacy name
        # again with no marker recorded, which is the forgery this marker closes.
        # Reporting the failure is enough: the caller withholds the memo, the retry
        # syncs the same entry, and an entry that survives without its sync is a
        # closed fence in every process that can see it.
        logger.debug("could not mark legacy activity folded for %r", slug, exc_info=True)
        return False
    marker = base.with_name(base.name + LEGACY_MIGRATED_SUFFIX)
    for path in (base, base.with_name(base.name + ".1")):
        try:
            if path.exists():
                target = marker if path == base else marker.with_name(marker.name + ".1")
                os.replace(path, target)
        except OSError:
            # Left under its live name, and that is the safe outcome rather than a
            # loss: the fenced marker is already recorded, so the source is never
            # read again, and the rows it held were appended before this ran.
            logger.debug("could not retire legacy activity at %s", path, exc_info=True)
            return True
    return True


class MemberEventLogService:
    def __init__(self, root: Path, broadcast: Broadcast | None = None) -> None:
        self._root = Path(root)
        self._broadcast = broadcast
        self._logs: dict[str, MemberLog] = {}
        self._slug_locks: dict[str, threading.Lock] = {}
        self._map_lock = threading.Lock()
        self._registry = ProjectionRegistry()
        for unit in all_units():
            self._registry.register(unit)
        self._registry.set_on_change(self._on_change)
        # Names carried by each slug's header, overlaid onto the roster view.
        self._names: dict[str, str] = {}
        # Slugs whose legacy fold has completed under this process. See
        # :meth:`_migrate_legacy` for why the memo is per process and not a file.
        self._legacy_folded: set[str] = set()

    # Wiring
    def attach_broadcast(self, broadcast: Broadcast) -> None:
        self._broadcast = broadcast

    @property
    def root(self) -> Path:
        """The ``member`` crew log root this service is bound to.

        :func:`get_service` compares it to rebuild the singleton when the data home
        moves (tests only), so stale logs from an old root are never served.
        """
        return self._root

    @property
    def broadcast(self) -> Broadcast | None:
        """The frame sink attached at dashboard startup, if any."""
        return self._broadcast

    def _on_change(self, slug: str, key: str, view: dict, seq: int) -> None:
        if key == types.PROJ_ROSTER:
            view = self._overlay_roster(slug, view)
        elif key == types.PROJ_ACTIVITY:
            view = self._scope_activity(slug, view)
        fn = self._broadcast
        if fn is None:
            return
        # Network-boundary redaction, same chain the /history and /activity
        # routes run. A folded view carries operator-supplied free text -- an
        # activity record's `project` path can embed a credential or presigned
        # URL -- and this broadcast is a dashboard WebSocket egress, so it must
        # redact the same class of value the sibling HTTP reads do or it leaks
        # what they protect.
        try:
            egress: object = _redact_projection_value(view)
        except Exception:
            # DROPPED, not published raw. The paragraph above is the whole reason
            # this call exists: the value carries operator free text that can embed
            # a credential, and this is a WebSocket egress. Falling back to `view`
            # would publish exactly what the redaction was added to withhold, and it
            # would do so on the one input redaction could not handle -- so the
            # failure mode would leak more reliably than the success path protects.
            # A dropped frame costs a projection update the next event re-sends;
            # a leaked credential cannot be taken back.
            logger.warning(
                "member projection redaction failed for %r/%r; the frame is dropped",
                slug,
                key,
                exc_info=True,
            )
            return
        try:
            fn(types.WS_MEMBER_PROJECTION, {"slug": slug, "key": key, "value": egress, "seq": seq})
        except Exception:
            logger.debug("member projection broadcast failed for %r/%r", slug, key, exc_info=True)

    def _scope_activity(self, slug: str, view: dict) -> dict:
        """Drop records belonging to a member who merely shares this slug.

        Applied at both the snapshot read and the change broadcast, like the roster
        name overlay, so neither path shows a colliding member's activity.
        """
        owner = self._names.get(slug)
        if owner is None:
            return view
        try:
            return members_projections.scope_activity_view(view, owner)
        except Exception:
            logger.debug("activity scoping failed for %r", slug, exc_info=True)
            return {"recent": [], "today": 0, "week": 0}

    def _overlay_roster(self, slug: str, view: dict) -> dict:
        out = dict(view)
        out["slug"] = slug
        name = self._names.get(slug)
        if name is not None:
            out["name"] = name
        return out

    # Internal plumbing
    def _log_path(self, slug: str) -> Path:
        """Where this member's log lives -- inside the fenced ``crew-log`` tree.

        Asks the store, which owns the layout (including the slug's
        readable-plus-digest directory name), rather than composing a path.
        """
        from kiro_crew.crew_log.store import crew_log_path

        return crew_log_path(KIND_MEMBER, slug)

    def _slug_lock(self, slug: str) -> threading.Lock:
        with self._map_lock:
            lock = self._slug_locks.get(slug)
            if lock is None:
                lock = threading.Lock()
                self._slug_locks[slug] = lock
            return lock

    def _get_log(self, slug: str) -> MemberLog | None:
        """Return a loaded, primed MemberLog, or None if it has no log on disk."""
        with self._map_lock:
            log = self._logs.get(slug)
        if log is not None:
            # Another process may have appended; refreshing is a stat when unchanged.
            if log.refresh_if_changed():
                self._fold_gap_locked(slug, log)
            return log
        log = MemberLog(slug)
        if not log.exists():
            return None
        log.load()
        if log.header is not None:
            header_name = log.header.get("name")
            self._names[slug] = header_name if isinstance(header_name, str) else slug
        self._prime_checkpointed(slug, log)
        with self._map_lock:
            # First primer wins; a concurrent loser's fold is wasted, not wrong.
            existing = self._logs.get(slug)
            if existing is not None:
                return existing
            self._logs[slug] = log
        return log

    def _prime_checkpointed(self, slug: str, log: MemberLog) -> None:
        """Prime *slug* from its savepoints, folding only the tail past the watermark.

        Falls back to the full fold (same value, more cost) whenever the shortcut
        cannot be trusted: no identity to compare, or no usable savepoint.

        Two checks gate a resume. The identity block is compared by equality. The
        prefix digest proves the bytes the state was folded from are still in the
        file: a damaged line is skipped on load, so without it a cold fold and a
        resumed one would disagree for the life of the member.

        This saves the FOLD, not the read: ``MemberLog`` parses the file either way.
        """
        identity = log.checkpoint_identity()
        admit = log.checkpoint_admit(identity["first_seq"]) if identity is not None else None
        if identity is None or admit is None:
            self._registry.prime(slug, log.iter_events())
            return

        # The prefix witness, read in ``tail_from`` and kept for the recheck and write.
        witness: list = []

        def tail_from(watermark: int):
            # Called ONCE, before the pass: a witness counts only if read before the
            # fold. A resume always needs one; a fresh pass only when a write may be owed.
            if watermark != EMPTY_WATERMARK or self._write_may_be_owed(log, watermark):
                prefix = log.checkpoint_witness(log.last_seq())
                if prefix is not None:
                    witness.append(prefix)
            return (ev for ev in log.iter_events() if ev["seq"] > watermark)

        floor = self._registry.prime_checkpointed(
            slug, self._checkpoints(slug), identity, tail_from, admit=admit
        )
        prefix = witness[0] if witness else None
        if floor != EMPTY_WATERMARK and not self._resumed_prefix_still_holds(log, prefix):
            # The bytes below the watermark moved during the fold, so the restored
            # state is already wrong in the registry; refusing the write is not
            # enough. Fold from the start instead.
            self._registry.prime(slug, log.iter_events())
            return
        self._maybe_save_savepoints(slug, log, identity, floor, prefix)

    @staticmethod
    def _resumed_prefix_still_holds(log: MemberLog, prefix: PrefixWitness | None) -> bool:
        """Whether a resumed pass can still vouch for the prefix it stood on.

        No witness answers False: a resume that cannot be checked must fall back.
        """
        if prefix is None:
            return False
        return log.checkpoint_prefix_unchanged(prefix)

    def _checkpoints(self, slug: str) -> DirectoryCheckpointStore:
        """This member's savepoint store, inside the directory its own log lives in.

        That directory is already fenced from sandboxed processes and agent file
        tools, so savepoints need no fence entry, and removing the member removes them.
        """
        from kiro_crew.crew_log.store import crew_log_dir

        return DirectoryCheckpointStore(crew_log_dir(KIND_MEMBER, slug) / "projections")

    @staticmethod
    def _write_may_be_owed(log: MemberLog, floor: int) -> bool:
        """Whether a fold reaching this log's end from *floor* could owe a write.

        :meth:`_maybe_save_savepoints`'s threshold, asked before the pass so a load
        that cannot write skips the witness read. An upper bound, not the decision.
        """
        return log.last_seq() - max(floor, 0) >= _SAVEPOINT_MIN_ADVANCE

    def _maybe_save_savepoints(
        self,
        slug: str,
        log: MemberLog,
        identity: dict,
        floor: int,
        prefix: PrefixWitness | None,
    ) -> None:
        """Write savepoints when the tail just folded was long enough to be worth it.

        *prefix* is the digest read before the pass. Nothing is written without one
        or if its bytes moved during the pass: a savepoint that cannot name the bytes
        it came from is worse than none. It certifies ONE boundary, so a unit at
        another seq waits for a later pass.
        """
        reached = log.last_seq()
        if reached - max(floor, 0) < _SAVEPOINT_MIN_ADVANCE:
            return
        if prefix is None:
            return
        if not log.checkpoint_prefix_unchanged(prefix):
            return
        store = self._checkpoints(slug)
        witness = witness_mapping(prefix)
        for savepoint in self._registry.savepoints(slug, identity, witness=witness):
            if savepoint.watermark != prefix.seq:
                continue
            store.save(slug, savepoint)

    # Units
    def ensure(self, slug: str, name: str, config=None) -> None:
        """Make sure *slug* has a log, and fold its legacy files in once.

        Reads through :meth:`_get_log`, so a repeat call (every roster read and every
        message) costs one ``stat`` and keeps one answer to whether the file moved.
        """
        from kiro_crew.members import validate_slug

        validate_slug(slug)
        lock = self._slug_lock(slug)
        with lock:
            log = self._get_log(slug)
            if log is None:
                # The header is written ONCE and fixes the owner for life, so a
                # nameless writer's slug placeholder is resolved to the roster name.
                name = self._resolved_name(slug, name, config)
                log = MemberLog(slug)
                log.create(name)
                log.load()
                with self._map_lock:
                    self._logs[slug] = log
                # A new log has no events to skip. ``prime`` fires no change callbacks,
                # so the name below is set before any projection is published.
                self._registry.prime(slug, log.iter_events())

            # The HEADER names the owner, not this argument: a slug placeholder in
            # ``_names`` would scope the member's own activity out of their view.
            header_name = (log.header or {}).get("name")
            if isinstance(header_name, str) and header_name:
                name = header_name

            self._names[slug] = name

            # Idempotent per item, so an interrupted pass resumes on a later call.
            self._migrate_legacy(slug, name, log)

    def _resolved_name(self, slug: str, name: str, config=None) -> str:
        """*name*, or the roster's exact name for *slug* when *name* is a placeholder.

        ``emit`` passes ``name or slug``. Any other *name* is returned untouched, and
        a failed resolution returns the placeholder, so the header is never worse.

        ``name == slug`` is ambiguous (a member named ``code-reviewer`` has that
        slug), so a resolution can run for a real name; pass a loaded *config* to
        skip the load.
        """
        if name != slug:
            return name
        try:
            from kiro_crew.eventlog_hooks import member_name_for_slug

            if config is None:
                from kiro_crew.config.loader import KiroCrewConfig

                config = KiroCrewConfig.load()
            return member_name_for_slug(config, slug) or name
        except Exception:
            logger.debug("header name resolution failed for %r", slug, exc_info=True)
            return name

    def _migrate_legacy(self, slug: str, name: str, log: MemberLog) -> None:
        """Fold this member's legacy files into events, once per item.

        Each item skips itself when the log already carries its event, so an
        interrupted migration resumes on a later call.

        One completed pass per process is memoised (in memory, so a run that dies
        mid-fold leaves none), and later calls return before taking the lease. The
        memo is set only when the fold read every source to its END: an over-budget
        or interrupted read RETURNS short rather than raising.

        The whole fold holds the unit's CROSS-PROCESS lease. Per-append leases would
        let the gateway and ``kirocrew-core`` both fold between appends and duplicate
        every row permanently. A refused lease does nothing and retires nothing; the
        counted dedupe makes the next re-read safe.
        """
        if slug in self._legacy_folded:
            return
        lease = self._hold_unit(slug)
        if lease is None:
            logger.debug("legacy fold for %r skipped: another process holds the log", slug)
            return
        from kiro_crew.crew_log.lease import release as release_lease

        try:
            settled = self._migrate_legacy_locked(slug, name, log)
        finally:
            release_lease(lease)
        if settled:
            self._legacy_folded.add(slug)

    @staticmethod
    def _hold_unit(slug: str) -> str | None:
        """A non-sole lease on this member's unit, or ``None`` when it cannot be had.

        Non-sole because the fold's own appends re-acquire it (a refcount bump);
        ``sole`` would refuse them. A held lease and an unopenable lease file both
        answer ``None``: write nothing this pass.
        """
        from kiro_crew.crew_log.lease import LEASE_FILE
        from kiro_crew.crew_log.lease import acquire as acquire_lease
        from kiro_crew.crew_log.store import crew_log_dir

        try:
            return acquire_lease(
                crew_log_dir(KIND_MEMBER, slug) / LEASE_FILE,
                kind=KIND_MEMBER,
                unit_id=slug,
            )
        except Exception:
            logger.debug("legacy fold lease refused for %r", slug, exc_info=True)
            return None

    def _clean_legacy_under_unit_lease(self, slug: str) -> str:
        """Remove *slug*'s legacy source with this member's unit lease HELD.

        Answers :data:`_CLEAN_REMOVED`, :data:`_CLEAN_KEPT` or :data:`_CLEAN_RACED`.

        For a unit the store did not find. A peer's fresh ``ensure`` could create
        the unit and fold this source into it, so the lease (a file beside the
        segments) is taken in a directory created to carry it; the fold takes the
        same lease, so the peer writes nothing meanwhile.

        The absence is RE-DECIDED inside the hold: the store's ``is_dir`` ran with
        no lease, so a peer may have created, folded and released already. A segment
        under the hold proves that, and the answer is :data:`_CLEAN_RACED` with
        everything untouched, so the caller re-runs ``remove_unit``.

        The created directory holds no segment, so :func:`store.unit_ids` never
        lists it; it is removed anyway. A refused lease touches nothing, not even
        the lease file, because the peer's lock names that file's inode.
        """
        from kiro_crew.crew_log.lease import LEASE_FILE
        from kiro_crew.crew_log.lease import release as release_lease
        from kiro_crew.crew_log.store import crew_log_dir, segment_paths
        from kiro_crew.session_ledger import unlink_lock_in_hold

        try:
            directory = crew_log_dir(KIND_MEMBER, slug)
            # Owner-only at every level, parent included (``mkdir(parents=True,
            # mode=...)`` sets only the leaf): a peer's later writes inherit it.
            mkdirs_owner_only(directory)
        except OSError:
            logger.warning(
                "crew log: cannot hold the unit lease for %r; keeping its legacy activity",
                slug,
            )
            return _CLEAN_KEPT
        lease = self._hold_unit(slug)
        if lease is None:
            # Touch nothing: the peer's lock names the lease file's INODE, so
            # unlinking it would let a later acquire lock a fresh inode and two
            # writers fold at once. The directory is the peer's now too.
            logger.warning(
                "crew log: another writer owns %r; keeping its legacy activity",
                slug,
            )
            return _CLEAN_KEPT
        try:
            # A segment means a peer created and folded the unit since the store's
            # unleased check. Removing the source would strand that fold (retention
            # ages session logs only), so the caller re-runs ``remove_unit``.
            if segment_paths(KIND_MEMBER, slug):
                logger.info(
                    "crew log: %r was recreated while its removal decided absence; "
                    "removing that unit instead",
                    slug,
                )
                return _CLEAN_RACED
            removed = _remove_legacy_activity(slug)
            lease_gone = unlink_lock_in_hold(directory / LEASE_FILE)
        finally:
            release_lease(lease)
        self._discard_lease_carrier(directory / LEASE_FILE, directory, held=lease_gone)
        return _CLEAN_REMOVED if removed else _CLEAN_KEPT

    @staticmethod
    def _discard_lease_carrier(lease_path: "Path", directory: "Path", *, held: bool) -> None:
        """Drop the lease file and the directory that carried it, best-effort.

        Reached ONLY by the process that held this lease, never on a refusal: a lock
        names an inode, so unlinking another process's lease would break it.

        ``rmdir`` refuses a non-empty directory, so a peer's new lease or segment
        survives. Windows refuses the in-hold unlink, hence *held*; the late attempt
        fails safely there while any handle is open.
        """
        if not held:
            try:
                lease_path.unlink(missing_ok=True)
            except OSError:
                logger.debug("crew log: lease file for the absent-unit hold still held")
        try:
            directory.rmdir()
        except OSError:
            logger.debug("crew log: the absent-unit hold's directory is not empty")

    def _migrate_legacy_locked(self, slug: str, name: str, log: MemberLog) -> bool:
        """The fold itself. Runs only with this member's unit lease held.

        Answers whether every legacy source was read to its END; the caller memoises
        on it. Short reads (unreachable path, byte budget, ``OSError``) RETURN
        rather than raise, so "it did not throw" is not enough. The total binding
        reader needs :func:`_legacy_binding_present` to tell unreadable from absent.
        """
        from kiro_crew import members

        # False only after a short read, never for an item with nothing to migrate.
        settled = True

        # Streamed, not the retained tail: an event aged out of that bounded window
        # would read as unmigrated and fold twice.
        have = {e["type"] for e in log.iter_events()}

        # 1. DM binding -> member/binding {slot_key}
        if types.MEMBER_BINDING not in have:
            try:
                binding = members.read_dm_binding(slug)
            except Exception:
                binding = None
                settled = False
                logger.debug("legacy binding read failed for %r", slug, exc_info=True)
            else:
                if binding is None and _legacy_binding_present(slug):
                    # "Not bound" with a file present: this pass did not read it, so
                    # the member stays open. Only that member pays the repeat reads.
                    settled = False
            if binding is not None and binding.get("member") == name:
                slot_key = binding.get("slot_key")
                if isinstance(slot_key, str) and slot_key:
                    self._append_locked(slug, log, types.MEMBER_BINDING, {"slot_key": slot_key})

        # 2. member rules -> member/rules {text}
        if types.MEMBER_RULES not in have:
            try:
                text = members.read_member_rules(slug, name)
            except Exception:
                text = ""
                settled = False
                logger.debug("legacy rules read failed for %r", slug, exc_info=True)
            if text:
                self._append_locked(slug, log, types.MEMBER_RULES, {"text": text})

        # 3. activity.jsonl(.1) -> activity/record, oldest first.
        # Read the legacy FILES directly: ``members.read_activity`` goes through
        # ``history()``, which takes the per-slug lock the caller holds (deadlock).
        # Deduped per ROW, keyed on the canonical JSON the append stores, so a crash
        # after the first append does not drop the remaining rows. Counted, NOT a
        # set: identical legacy rows are ordinary, so each row consumes ONE recorded
        # match and a row with no match left is appended.
        migrated: Counter[str] = Counter(
            _activity_key(e["data"])
            for e in log.iter_events()
            if e["type"] == types.ACTIVITY_RECORD
        )
        legacy, legacy_complete = _read_legacy_activity_files(slug)
        for row in legacy:
            key = _activity_key(row)
            if migrated[key] > 0:
                migrated[key] -= 1
                continue
            self._append_locked(
                slug, log, types.ACTIVITY_RECORD, {**row, LEGACY_PROVENANCE_KEY: True}
            )
        # Retired whether or not any rows were read: the marker records that the
        # fold COMPLETED, and gating on rows would leave an empty file's path open
        # forever. But only on a complete read: retirement is one-way, so a read cut
        # short (byte budget, OSError) stays unretired and is re-read next ensure,
        # which the counted dedupe makes safe. An over-budget file is reported every
        # pass.
        if legacy_complete and not _retire_legacy_activity(slug):
            settled = False
        return settled and legacy_complete

    def logged_name(self, slug: str) -> str | None:
        """The EXACT member name this slug's log was created for, or None.

        A slug is lossy (`Review_Agent` and `review-agent` both fold to
        `review-agent`), so callers ask the header whose log this is. A name equal to
        the slug may be a locked-in placeholder; do not read it as a second member.
        """
        log = self._get_log(slug)
        if log is None or not isinstance(log.header, dict):
            return None
        held = log.header.get("name")
        return held if isinstance(held, str) else None

    def slugs(self) -> list[str]:
        """Every member with a log, sorted.

        Asks the store: directory names are an irreversible fold of the slug, so
        the slug comes from each header that folds back to its directory.
        """
        from kiro_crew.crew_log.store import unit_ids

        return unit_ids(KIND_MEMBER)

    # Write
    def append(self, slug: str, type: str, data: dict) -> Event:
        lock = self._slug_lock(slug)
        with lock:
            log = self._get_log(slug)
            if log is None:
                raise FileNotFoundError(f"no member log for {slug!r}; call ensure() first")
            return self._append_locked(slug, log, type, data)

    def append_closer_if_still_applies(
        self,
        slug: str,
        type: str,
        data: dict,
        *,
        still_applies: Callable[[dict, dict], bool],
        observed: dict | None = None,
    ) -> Event | None:
        """Append a closer only while the projection still shows what it closes.

        A closer is decided from a SNAPSHOT and written later; a slot reopened or a
        patrol re-armed in between would make it a permanent regression in an
        append-only log. So ``still_applies`` is re-asked here, under the writing
        lock, with the CURRENT values and with ``observed`` (the values the caller
        decided on, which tell a re-armed episode from the old one). ``None`` means
        the closer does not apply now.

        Not gated on ``asOfSeq``, the max across every cell, which any unrelated
        event advances. The predicate must not re-enter the service (``snapshot``
        takes this same non-reentrant lock).

        The per-slug lock orders only this process's writers. Against other
        processes the store's ``append_if`` compares the tail with the seq this fold
        reached and declines if a foreign commit landed; the loop then re-folds and
        re-asks. The fold stays outside the cross-process hold, because a parse
        there is unbounded and a peer append that times out is lost. Seqs only
        increase, so the comparison cannot be fooled.
        """
        lock = self._slug_lock(slug)
        with lock:
            # One contention budget for the whole loop: the per-slug lock is held
            # across attempts, so a per-attempt budget would multiply the hold.
            deadline = time.monotonic() + APPEND_CONTENTION_SECONDS
            for _ in range(_CLOSER_TAIL_ATTEMPTS):
                log = self._get_log(slug)
                if log is None:
                    return None
                self._fold_gap_locked(slug, log)
                # The seq every cell has folded. Clamped to 0, the store's tail for
                # a header-only file, since an undriven registry reports -1.
                folded_to = max(self._registry.observed_floor(slug), 0)
                values = self._registry.snapshot(slug).get("values", {})
                if not still_applies(
                    values if isinstance(values, dict) else {},
                    observed if isinstance(observed, dict) else {},
                ):
                    return None
                event = log.append_if(type, data, max_tail_seq=folded_to, deadline=deadline)
                if event is not None:
                    # No gap fold: the write took the seq right after the fold, so the
                    # gap is empty, and scanning the file to learn that is not free.
                    self._registry.drive(slug, event)
                    return event
            # Not writing is safe but leaves the projection stale; raise so a
            # run-once caller can tell this from "does not apply".
            logger.warning(
                "closer for slug=%r type=%r lost the tail race on every attempt; "
                "the projection stays stale until a caller re-decides",
                slug,
                type,
            )
            raise CloserTailContention(slug, type)

    def _fold_gap_locked(self, slug: str, log: MemberLog, *, below: int | None = None) -> None:
        """Fold events on disk that this process has not folded; caller holds the lock.

        Other processes write the log too, so entries can land between our loads.
        ``drive`` drops anything at or below a cell's ``observed_seq``, so folding a
        newer event first would strand the earlier ones until a restart re-primed.

        ``below`` bounds the range when the caller is about to drive its own event
        (the append path); a read folds to the end. ``drive`` is idempotent per cell.
        """
        floor = self._registry.observed_floor(slug)
        if floor < 0:
            # Nothing folded yet, and such a cell cannot be driven a range, so prime
            # instead; returning would let the next append drive past other
            # processes' entries. ``below`` still bounds it, so the append path's
            # ``drive`` fires the change callbacks ``prime`` withholds.
            self._registry.prime(
                slug,
                (ev for ev in log.iter_events() if below is None or ev["seq"] < below),
            )
            return
        # The append path (``below`` given) has just loaded or extended the log through
        # its own event, so the events held answer it without a walk of the whole file.
        # A read keeps the walk, which is what sees another process's newest entries.
        gap = log.iter_events() if below is None else log.iter_held_events_after(floor)
        for earlier in gap:
            if earlier["seq"] <= floor:
                continue
            if below is not None and earlier["seq"] >= below:
                break
            self._registry.drive(slug, earlier)

    def _append_locked(self, slug: str, log: MemberLog, type: str, data: dict) -> Event:
        """Append + fold; caller holds the per-slug lock."""
        event = log.append(type, data)
        # Fold other processes' entries first: driving only this event would move
        # `observed_seq` past them and `drive` would drop them for good.
        self._fold_gap_locked(slug, log, below=event["seq"])
        self._registry.drive(slug, event)
        return event

    # Removal
    def remove_unit(self, slug: str, *, still_unclaimed: Callable[[], bool]) -> str:
        """Remove *slug*'s crew log and forget it here. One of the ``REMOVE_*`` statuses.

        The member counterpart of ``sessions._remove_session_crew_log``: it runs the
        store's removal AND drops this service's caches, so no cached ``MemberLog``
        answers for history that is gone.

        *still_unclaimed* (is the member still off the roster?) is re-asked under the
        removal's lease hold as the store's ``guard``, because a namesake recreated
        after the caller decided derives the same unit. False removes nothing.

        Runs under the per-slug lock, so appends here cannot race the unlink. The
        lock ENTRY is kept (a fresh one would give one slug two locks); folded
        registry cells stay but are unreachable once ``_get_log`` finds no file.

        **The legacy activity source goes with the unit, INSIDE the store's own lease
        hold and BEFORE the unit itself, and that is what makes the removal mean
        anything.** Those rows are the member's own pre-log history, they live outside
        the unit under ``members/<slug>/``, and the marker saying they were already
        folded lives INSIDE it -- so a removal that took only the unit would leave the
        history on disk AND leave the next fresh ``ensure`` free to fold it into a new
        log, in this process or any other writer's. Taking it after the removal
        RETURNED would leave the same fold reachable in the window between the lease's
        release and the unlink, so it is handed to ``remove_unit`` as its ``in_hold``
        action instead.

        The order inside that hold is the source first, and it is not arbitrary: the
        marker gating the fold is part of the unit, so a unit destroyed while the
        source survives has ARMED the fold rather than half-finished it. So the
        cleanup answers whether the source is really gone, and the store keeps
        everything on a false answer -- the unit, its marker, and the source under it
        -- and reports ``failed``. Taken, a later append can recreate at most an empty
        header, which carries nothing.

        **A unit the store does not find leaves that source behind too, so the
        cleanup runs for an absent unit as well, and there it re-asks the roster
        itself.** A member whose log was never written, or whose fold has not run, has
        its history ONLY in that source, and skipping it there would leave the delete
        having removed nothing at all. The store calls the predicate as its guard only
        when there is a unit to hold, so for an absent one there is no answer to
        inherit and this asks again; the question raising rather than answering keeps
        the source, the same direction every other decision here takes. That branch
        takes the unit's lease ITSELF rather than running bare, because the race it
        has to win is a peer creating the very unit the store just failed to find and
        folding this source into it. **And it re-decides the absence under that hold
        rather than trusting the one it inherited**: the store's ``is_dir`` ran with no
        lease, so a peer can create, fold and release entirely inside the window that
        ends at the acquire. A unit found there means the absence is stale, nothing is
        removed, and this method calls ``remove_unit`` a second time -- now against a
        unit that exists, so the fold and the source go inside one hold of its own and
        the status returned describes THAT removal rather than the absence that is no
        longer true.

        Only PROVEN absence reaches it. A unit whose own name is a link answers
        ``linked``, not ``absent``, and that distinction is load-bearing here rather
        than tidy: this branch re-derives the unit path, which RESOLVES, so acting on a
        link as absence would take the lease of whatever it points at -- very likely a
        live member's unit -- and unlink that unit's lease file inside its own hold. A
        link is left entirely alone, for a person to remove.

        The caches are dropped on ``removed`` and on ``failed`` (which may be partial;
        a drop costs only a reopen). Only ``owned``, where nothing was touched,
        keeps them.
        """
        from kiro_crew.crew_log.store import (
            REMOVE_ABSENT,
            REMOVE_FAILED,
            REMOVE_REMOVED,
            remove_unit,
        )

        with self._slug_lock(slug):
            status = remove_unit(
                KIND_MEMBER,
                slug,
                guard=lambda _directory: still_unclaimed(),
                in_hold=lambda: _remove_legacy_activity(slug),
            )
            reclaim = False
            if status == REMOVE_ABSENT:
                try:
                    reclaim = bool(still_unclaimed())
                except Exception:
                    logger.debug(
                        "crew log: roster unreadable for %r; keeping legacy activity",
                        slug,
                        exc_info=True,
                    )
                    reclaim = False
                if reclaim:
                    outcome = self._clean_legacy_under_unit_lease(slug)
                    if outcome == _CLEAN_RACED:
                        # A peer created the unit meanwhile; re-run the same call so
                        # fold and source go together under one hold.
                        status = remove_unit(
                            KIND_MEMBER,
                            slug,
                            guard=lambda _directory: still_unclaimed(),
                            in_hold=lambda: _remove_legacy_activity(slug),
                        )
                        reclaim = False
                    else:
                        reclaim = outcome == _CLEAN_REMOVED
            if status in (REMOVE_REMOVED, REMOVE_FAILED) or reclaim:
                with self._map_lock:
                    self._logs.pop(slug, None)
                    self._names.pop(slug, None)
        return status

    # Read
    def snapshot(self, slug: str) -> dict:
        lock = self._slug_lock(slug)
        with lock:
            log = self._get_log(slug)
            if log is None:
                return {"asOfSeq": -1, "values": {}}
            snap = self._registry.snapshot(slug)
        values = snap.get("values", {})
        if types.PROJ_ROSTER in values:
            values[types.PROJ_ROSTER] = self._overlay_roster(slug, values[types.PROJ_ROSTER])
        if types.PROJ_ACTIVITY in values:
            values[types.PROJ_ACTIVITY] = self._scope_activity(slug, values[types.PROJ_ACTIVITY])
        return snap

    def redacted_snapshot(self, slug: str) -> dict:
        """``snapshot`` with every value through the SAME chain the broadcast runs.

        A caller that sends a projection to one socket OUTSIDE the broadcast path
        must not re-implement the redaction: a folded view carries
        operator-supplied free text, an activity record's ``project`` can embed a
        credential or a presigned URL, and a second copy of the chain is a second
        thing to forget. The one caller today is the connect-time replay of frames
        held back while a socket waited for its ``members_subscribed`` baseline.

        A redaction failure PROPAGATES rather than falling back to the raw view, so
        a caller's own best-effort arm drops the frame instead of shipping it
        unredacted.
        """
        snap = self.snapshot(slug)
        values = snap.get("values", {})
        return {
            "asOfSeq": snap.get("asOfSeq", -1),
            "values": {key: _redact_projection_value(view) for key, view in values.items()},
        }

    def history(
        self, slug: str, *, before: int | None = None, limit: int | None = 50
    ) -> list[Event]:
        lock = self._slug_lock(slug)
        with lock:
            log = self._get_log(slug)
            if log is None:
                return []
            return log.history(before, limit)

    def last_seq(self, slug: str) -> int:
        lock = self._slug_lock(slug)
        with lock:
            log = self._get_log(slug)
            if log is None:
                return -1
            return log.last_seq()

    def last_seqs(self) -> dict[str, int]:
        """Every member's cursor, skipping any member whose log cannot be read.

        This is the SUBSCRIBE BASELINE, so one damaged header must not raise out of
        the whole dict. A skipped member is absent, not -1: -1 means "no log yet",
        and a client told that would prune what it has.
        """
        out: dict[str, int] = {}
        for slug in self.slugs():
            try:
                out[slug] = self.last_seq(slug)
            except Exception:
                logger.debug("baseline cursor unreadable for %r, skipped", slug, exc_info=True)
        return out

    def close(self) -> None:
        """Retire this service: release every cached log's lease and break the cycle.

        The registry holds a bound method back to the service, so without this the
        :class:`CrewLog` leases wait for the cyclic collector; on Windows that strands
        a descriptor under a torn-down directory and the next write fails.

        Idempotent and best-effort; the service then re-opens on demand. Called only
        on retirement (:func:`set_service`, :func:`get_service` on a home change).
        """
        with self._map_lock:
            logs = list(self._logs.values())
            self._logs.clear()
            self._names.clear()
        for log in logs:
            try:
                log.close()
            except Exception:
                logger.debug("member log close failed during service retirement", exc_info=True)
        # Sever the cycle last: a close above may still touch the registry.
        try:
            self._registry.set_on_change(None)
        except Exception:
            logger.debug(
                "registry on-change detach failed during service retirement", exc_info=True
            )


def get_service() -> MemberEventLogService:
    """Lazy process-wide singleton rooted at the ``member`` crew log root."""
    global _singleton
    with _singleton_lock:
        from kiro_crew.crew_log.store import crew_log_root

        root = crew_log_root(KIND_MEMBER)
        # Rebuild when the data home moved (tests only), so no cached MemberLog
        # from the old root answers.
        if _singleton is None or _singleton.root != root:
            previous = _singleton
            _singleton = MemberEventLogService(root, previous.broadcast if previous else None)
            # Retire it now rather than at GC (see ``close``); nothing is mid-write
            # at a test boundary.
            if previous is not None:
                previous.close()
        return _singleton


def set_service(svc: MemberEventLogService | None) -> None:
    """Test seam. Retires the outgoing service so it holds no lease afterwards."""
    global _singleton
    with _singleton_lock:
        previous = _singleton
        _singleton = svc
        # Release leases now, not at GC: lingering descriptors block the tmp-home
        # teardown on Windows.
        if previous is not None and previous is not svc:
            previous.close()
