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
#: A savepoint is allowed to LAG -- resuming from an older one replays more tail and
#: reaches the same value -- so a write is spent only when it saves a meaningful
#: replay. Without this every load of every member would rewrite one file per
#: registered unit, which is the cost savepoints exist to remove rather than move,
#: and a short-lived member would leave files behind that folding from the start
#: already handles for free. Matches the crew log's own ``MIN_ADVANCE_ENTRIES``.
_SAVEPOINT_MIN_ADVANCE = 256

#: How many times a closer re-folds and re-asks its predicate after losing the tail
#: to another process. Each attempt costs one fold, and a closer that keeps losing is
#: a member under sustained foreign writes -- where declining is right anyway, since
#: the next read decides again against a state that has settled. Small for that
#: reason: the retry exists for the one-commit collision, not to win a write war.
_CLOSER_TAIL_ATTEMPTS = 3


class CloserTailContention(Exception):
    """Every attempt to place a closer lost the tail to another process.

    Distinct from a closer that does not apply, which is an ordinary ``None``: this
    says the decision was never given a window, so nothing was learned about whether
    it holds. A caller invoked repeatedly can ignore it, because its next run decides
    again. A caller that runs ONCE -- the startup sweep -- must not, or the state it
    was closing stays open until the next restart, and that is the whole reason the
    two outcomes are told apart instead of sharing ``None``.
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

    Asked because :func:`members.read_dm_binding` is TOTAL by contract: a missing
    file, an unreadable one and a malformed payload all read as "not bound". That
    is the right answer for a caller that only wants to re-create the binding, and
    the wrong one for the fold, which has to know whether it READ the source -- a
    source it did not read is one a later pass still can. Its sibling for rules
    raises instead, so only the binding needs this second question asked.

    Fails CLOSED on an unanswerable path, meaning it reports PRESENT. The wrong
    answer is asymmetric the same way :func:`_legacy_fold_completed`'s is: present
    costs a re-read the counted dedupe makes safe, absent drops a binding this
    process could have migrated.

    ``stat`` carries that, where ``Path.exists`` cannot: it answers False for every
    error it meets, so a file this process may not reach reads exactly like one that
    is not there -- the one confusion this function exists to prevent. Only a missing
    file is absent here, and every other error reports present.
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
#: The absence the store answered was STALE -- a peer created this member's unit,
#: and very likely folded the legacy source into it, in the window between that
#: ``is_dir`` and this hold. Nothing here is removed: there is now a unit to lease,
#: so the caller re-runs the one spelling of deletion instead of removing the
#: source beside a fold nothing would ever collect.
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

        Read by :func:`get_service` to decide whether the cached singleton still
        belongs to the process's data home: the root moves when the home moves,
        which every test does and production never does, and a service holding
        logs opened under the old root would answer from files nothing writes.
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

        Applied at the SAME two sites as the roster name overlay -- the snapshot
        read and the change broadcast -- because a colliding member's activity
        reaching the wrong drawer is the same exposure on either path.
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

        The store owns the layout, including the readable-plus-digest fold of the
        slug that names the directory, so this asks it rather than composing a
        path. That is what puts the file under the root the sandbox masks and the
        agent file tools refuse.
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
            # A held instance can be arbitrarily behind the file: another process
            # appends through its own service, and every read here short-circuits
            # on the cached events. Refreshing is a stat when nothing changed.
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
            # Another thread may have primed concurrently. The FIRST primer into
            # this lock installs its instance and every later one adopts it, so a
            # loser's own fold is wasted rather than wrong: priming is idempotent.
            existing = self._logs.get(slug)
            if existing is not None:
                return existing
            self._logs[slug] = log
        return log

    def _prime_checkpointed(self, slug: str, log: MemberLog) -> None:
        """Prime *slug* from its savepoints, folding only the tail past the watermark.

        Falls back to the full fold whenever the shortcut cannot be trusted -- no
        identity to compare, or no usable savepoint -- because a cold fold reaches the
        same value at more cost, and that is the whole posture of a savepoint.

        TWO conditions decide that, not one. The identity block covers what is fixed
        once a fold is done and is compared by equality. The prefix digest covers what
        equality cannot reach: that the bytes the state was folded from are still the
        bytes in the file. This log needs the second one on its own terms -- a damaged
        committed line is skipped on load, so a cold fold omits what it contributed
        while a savepoint written before the damage keeps it, and a resumed fold never
        revisits the region below its watermark. Without the digest those two reads
        disagree for the life of the member, which is the one thing a savepoint may
        not do.

        This saves the FOLD, not the read: ``MemberLog`` parses the file on load
        either way, and the watermark skips one ``apply`` per unit per event.
        """
        identity = log.checkpoint_identity()
        admit = log.checkpoint_admit(identity["first_seq"]) if identity is not None else None
        if identity is None or admit is None:
            self._registry.prime(slug, log.iter_events())
            return

        # The witness for the prefix this pass trusts, read in ``tail_from`` below and
        # held here for the post-fold recheck and the write.
        witness: list = []

        def tail_from(watermark: int):
            # ``prime_checkpointed`` calls this ONCE with the floor, before the pass,
            # and a witness is evidence only when read before the bytes are folded.
            # A resume always needs one, since the restored state stands on a prefix
            # this pass never revisits; a pass that resumed nothing reads one only
            # when a write may be owed.
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
            # The bytes below the watermark moved while the tail was folding, so the
            # restored state carries an entry the file does not yield any more -- and a
            # resumed fold never returns to that region to notice. Refusing the write
            # is not enough here: the state is already in the registry and would be
            # served for the life of this instance while disagreeing with every cold
            # fold. Fold from the start instead, which is what the file now says.
            self._registry.prime(slug, log.iter_events())
            return
        self._maybe_save_savepoints(slug, log, identity, floor, prefix)

    @staticmethod
    def _resumed_prefix_still_holds(log: MemberLog, prefix: PrefixWitness | None) -> bool:
        """Whether a resumed pass can still vouch for the prefix it stood on.

        No witness is not a pass: a boundary the file does not resolve leaves nothing
        to compare, and a resume that cannot be checked is the one case that must fall
        back rather than be trusted.
        """
        if prefix is None:
            return False
        return log.checkpoint_prefix_unchanged(prefix)

    def _checkpoints(self, slug: str) -> DirectoryCheckpointStore:
        """This member's savepoint store, inside the directory its own log lives in.

        The kernel owns no path, so the directory is chosen here. It is the log's own
        store directory, which is already fenced from a sandboxed process and from the
        agent's file tools -- so a savepoint inherits that protection by living there
        and needs no fence entry of its own. It also means removing the member
        removes its savepoints, with no second place to clean up.
        """
        from kiro_crew.crew_log.store import crew_log_dir

        return DirectoryCheckpointStore(crew_log_dir(KIND_MEMBER, slug) / "projections")

    @staticmethod
    def _write_may_be_owed(log: MemberLog, floor: int) -> bool:
        """Whether a fold reaching this log's end from *floor* could owe a write.

        The same threshold :meth:`_maybe_save_savepoints` enforces, asked BEFORE the
        pass so the witness read can be skipped on a load that cannot write anything.
        It is an upper bound on that decision and not a second copy of it: the fold
        can end below the log's current end, and the write is refused there.
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

        A savepoint is allowed to LAG, so a write is spent only when it saves a
        meaningful replay. Without a threshold this would rewrite every unit's file on
        every load of every member, which is the cost savepoints exist to remove
        rather than relocate -- and a short-lived member would leave files behind that
        folding from the start already handles for free.

        *prefix* is the digest read before the pass. Nothing is written without one,
        and nothing is written if the bytes it covers moved while the pass ran: either
        way nothing here could say which bytes produced this state, and a savepoint
        that cannot say so is the one thing worse than none. It certifies ONE
        boundary, so a unit standing at another seq waits for a pass whose witness
        covers it.
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

        Reads through :meth:`_get_log`, so a slug this process has already ensured
        costs one ``stat``: the held instance is kept, its events are parsed again
        only when the file moved, and the fold resumes from this member's savepoints
        instead of from the first event. That repeat is the common case -- ``ensure``
        is called once per member on every roster read and once per message -- and
        the creation below is the rare one.

        Reusing that read is also what keeps ONE answer in this class to whether the
        file moved. A second copy here would be a second thing to keep correct, and
        what a wrong answer serves is the member's own drawer.
        """
        from kiro_crew.members import validate_slug

        validate_slug(slug)
        lock = self._slug_lock(slug)
        with lock:
            log = self._get_log(slug)
            if log is None:
                # The header is written ONCE, so this call fixes the log's owner for
                # life. A nameless writer arrives with the slug (``emit`` passes
                # ``name or slug``), which would cost the log its collision check for
                # good, so resolve the exact name from the roster; the name-scoped
                # migration below uses it too. Only here, so config is read once ever.
                name = self._resolved_name(slug, name, config)
                log = MemberLog(slug)
                log.create(name)
                log.load()
                with self._map_lock:
                    self._logs[slug] = log
                # Folded from the start: a log this call publishes carries its header
                # and no events, so a savepoint resume has nothing to skip. ``prime``
                # runs no change callbacks, so the name below is in place before any
                # projection of this member is published.
                self._registry.prime(slug, log.iter_events())

            # The HEADER decides who this log belongs to, not this call's argument: a
            # nameless writer holds the slug, and putting that in ``_names`` (which
            # scopes the activity view) would scope the member's own entries out of
            # their projection. :meth:`_get_log` reads the header the same way, so the
            # write and read paths agree; the name-scoped migration below uses it too.
            header_name = (log.header or {}).get("name")
            if isinstance(header_name, str) and header_name:
                name = header_name

            self._names[slug] = name

            # Each item of the migration asks whether the log already carries its
            # event, so a pass is idempotent and an interrupted one resumes on a
            # later call -- in this process or in the next. One completed pass per
            # process settles it, which :meth:`_migrate_legacy` keeps track of.
            self._migrate_legacy(slug, name, log)

    def _resolved_name(self, slug: str, name: str, config=None) -> str:
        """*name*, or the roster's exact name for *slug* when *name* is a placeholder.

        ``emit`` passes ``name or slug``, so a writer that does not know the name
        arrives here with the slug. Only that case is resolved: any other value is
        a name a caller actually holds and is returned untouched. A failed
        resolution returns the placeholder, which is what the caller passed, so
        this can never make the header worse than not asking.

        *config* exists because ``name == slug`` is an AMBIGUOUS test: a member
        legitimately named ``code-reviewer`` folds to that same slug, so a caller
        holding a real name can land here too. A caller that has already loaded the
        config passes it and this costs no I/O; only a caller with nothing to pass
        pays a load, and the loader caches on unchanged files.
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

        Each item asks whether the log already carries its event and skips the legacy
        read when it does, which is what lets an interrupted migration resume:
        whatever a dead run got through stays done, and whatever it did not is picked
        up by a later call.

        ONE completed pass per process settles the member, and every call after it
        returns here -- without the lease, which is the point. ``ensure`` is called
        once per member on every roster read and once per message, and by then every
        item skips itself, so a call that cannot do any work would still be spending
        a cross-process lease acquire and a set of legacy path reads to find that
        out. The memo is per PROCESS and deliberately not a file: a run that dies
        mid-fold leaves no memo behind, so the next process folds the member again.

        Nothing is recorded unless the pass read every legacy source to its END. A
        refused lease, an exception out of the fold, and a read that came back short
        of what the file holds all leave the memo unwritten, and the member is folded
        again on a later call. The last of those is the one worth naming: a file over
        the byte budget, and a read an ``OSError`` interrupted, RETURN rather than
        raise, so "the pass did not throw" is not the question -- which is why the
        fold answers it instead.

        A completion marker event would answer the same question in one check, and
        is deliberately not used: it would sit in every member's log forever and
        shift the seq of every event after it -- an on-disk cost paid by every
        member, for a concern that ends with the first successful pass.

        Held under the unit's CROSS-PROCESS lease for its whole length, which the
        per-append lease cannot supply. The append path takes and releases the lease
        inside each append, so between two of them another process is free to run
        its own fold: both snapshot a log with no activity records, both find every
        legacy row unmatched, and both append it. Retirement then makes the
        duplication permanent, and nothing reports it. This log has two ordinary
        writers -- the gateway and ``kirocrew-core`` -- so two folds of one member
        during an upgrade is routine, not exotic.

        A refusal means another process is already folding this member. Nothing is
        done, and nothing is retired either, so the source is re-read on the next
        ensure -- which the counted dedupe makes safe whether or not the other
        process finished.
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

        Non-sole deliberately: the appends inside the fold acquire the same lease,
        and ``acquire`` documents a plain acquire against a unit this process
        already holds as succeeding by incrementing the reference count. ``sole``
        would refuse exactly those, so the fold could not write at all.

        Both refusals collapse to ``None`` because the answer is the same -- write
        nothing this pass -- and a lease file that cannot be opened is no more a
        licence to fold unserialized than a lease another process holds.
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

        Answers one of :data:`_CLEAN_REMOVED`, :data:`_CLEAN_KEPT` or
        :data:`_CLEAN_RACED`. The third is not a failure and not a removal: it says
        the store's absence has been overtaken and there is a unit for the caller to
        aim ``remove_unit`` at, which is a different instruction from "the source is
        still there because nothing could be held".

        The branch for a unit the store did not find. There is nothing there to hold
        a lease in, and the race that needs holding is precisely a peer creating that
        unit: its fresh ``ensure`` writes a header and then folds this source, so a
        cleanup with no lease can lose to it and leave the rows in a unit nobody
        removes. The lease is a file BESIDE the unit's segments, so the directory is
        created to carry it, the lease is taken, and the directory goes again after.

        Serializing against the fold is what the lease buys: the fold takes this same
        lease before reading, and a non-sole acquire from another PROCESS is refused
        rather than shared, so the peer writes nothing while this runs.

        **A peer that got there FIRST is caught by re-deciding the absence inside the
        hold, not by the store's decision.** The store answered ``absent`` from an
        ``is_dir`` taken with no lease held, so "the store would have found a unit"
        is only true of a unit that existed at that moment: a peer can create one,
        fold this source into it and release entirely inside the window that ends
        when the acquire here returns. A segment present under the hold is what proves
        it, since the directory this method creates to carry the lease holds none.
        Removing the source then would be the worst of the three outcomes -- the fold
        survives in a unit for a member the roster does not have, and no sweep
        collects it -- so that case answers :data:`_CLEAN_RACED` with the source, the
        lease and the directory all untouched, and the caller re-runs the one spelling
        of deletion against the unit that now exists.

        The created directory is owner-only and holds no segment, so it is not a unit
        to any reader: :func:`store.unit_ids` proves identity from a segment header
        and skips a directory that has none, which is why this cannot make a deleted
        member list again. It is removed anyway, so nothing is left to explain.

        A lease that cannot be had answers false with the source untouched, the same
        direction every other decision in this teardown takes -- and it leaves the
        lease file and the directory alone as well, because a refusal means a peer
        holds that lease and its lock names that file's inode.
        """
        from kiro_crew.crew_log.lease import LEASE_FILE
        from kiro_crew.crew_log.lease import release as release_lease
        from kiro_crew.crew_log.store import crew_log_dir, segment_paths
        from kiro_crew.session_ledger import unlink_lock_in_hold

        try:
            directory = crew_log_dir(KIND_MEMBER, slug)
            # Owner-only at every level it creates, the parent ``crew-log/members``
            # included (``mkdir(parents=True, mode=...)`` applies the mode to the
            # leaf only). These directories hold conversation bodies once a writer
            # uses one, and a peer's create would inherit this mode rather than set
            # its own.
            mkdirs_owner_only(directory)
        except OSError:
            logger.warning(
                "crew log: cannot hold the unit lease for %r; keeping its legacy activity",
                slug,
            )
            return _CLEAN_KEPT
        lease = self._hold_unit(slug)
        if lease is None:
            # Nothing is touched here, and that is the point: a refusal means a PEER
            # holds this lease, and the lock it holds names the lease file's INODE.
            # Unlinking that file would leave the peer's lock naming an inode no path
            # reaches, so a later acquire would create a fresh one, lock it, and prove
            # nothing -- two writers folding the same source at once. The directory
            # stays for the same reason: it is the peer's working directory now,
            # whether or not this call is what created it.
            logger.warning(
                "crew log: another writer owns %r; keeping its legacy activity",
                slug,
            )
            return _CLEAN_KEPT
        try:
            # The absence is RE-DECIDED inside the hold, as ``remove_unit`` re-asks its
            # guard: ``absent`` came from an unleased ``is_dir``, and a peer can create
            # the unit, fold this source and release before the acquire above. A
            # segment proves that (the lease directory holds none). Removing the source
            # then would strand the fold with no collector (retention ages session logs
            # only), so nothing is touched and the caller re-runs ``remove_unit``,
            # which removes fold and source under one hold.
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

        Reached ONLY by the process that held this lease, never on a refusal. A lock
        names an inode, so unlinking a lease another process holds would leave its
        lock naming a file no path reaches and let the next acquire lock a fresh inode
        and prove nothing. Neither of these is data: the directory was created to give
        the lease a place to be, and by here the work it guarded is over.

        ``rmdir`` is the guard on the directory rather than a check before it: it
        refuses a directory that is not empty, so a peer that has since created its
        own lease or a segment keeps both.

        Windows refuses an in-hold unlink of the lease, which is why the caller reports
        whether that already happened -- and the late attempt is safe there for the
        same reason it was refused, since it fails while any handle is open.
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

        Answers whether every legacy source was read to its END, which is what says
        the member is settled. A read that returned less than the file holds -- an
        unreachable path, a file over the byte budget, an ``OSError`` part-way --
        leaves rows this pass never saw, and it does so by RETURNING rather than
        raising. So the answer cannot be "it did not throw": the caller memoises on
        it, and a pass recorded as settled is a pass nothing repeats.

        The binding read needs one extra question for the same reason. It is total by
        contract, so it reports an unreadable file exactly as it reports an absent
        one; :func:`_legacy_binding_present` is what tells those apart here. The
        rules read raises on an existing file it cannot use, so it needs nothing.
        """
        from kiro_crew import members

        # Every legacy source this pass could read, read whole. Set false by a read
        # that came back short, never by an item that had nothing to migrate.
        settled = True

        # Streamed, not the retained tail: the tail is a bounded WINDOW, so a
        # membership test against it would report an item unmigrated because its
        # event had aged out, and fold it a second time.
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
                    # "Not bound" with a file present means this pass did not read
                    # what is there, so the member stays open. The same None comes back
                    # for an unreadable file, a non-canonical `slot_key` and a member
                    # that slugifies elsewhere, and a total reader cannot tell them
                    # apart. Only that member pays (lease and legacy reads on every
                    # ensure until repaired); classifying it would cost a second read on
                    # every pass for every member. The source is never retired.
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
        `review-agent`) and colliding names are supported, so a caller rendering
        per-member state asks the header whether the log belongs to that member.

        A header whose name IS the slug is a placeholder that names no member:
        ``ensure`` writes the header once, so a writer that passed the slug locks it
        in. It is returned as held; a caller must not read it as a second member.
        """
        log = self._get_log(slug)
        if log is None or not isinstance(log.header, dict):
            return None
        held = log.header.get("name")
        return held if isinstance(held, str) else None

    def slugs(self) -> list[str]:
        """Every member with a log, sorted.

        Asks the store rather than listing a directory: under ``crew-log`` a unit's
        directory is named with a readable-plus-digest FOLD of the slug, and the
        fold is not reversible, so the slug comes from each log's header and only
        when that header's id folds back to the directory holding it.
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

        A closer is decided from a SNAPSHOT and written later. The startup reconcile
        runs concurrently with live writers, so a slot can be reopened or a patrol
        re-armed in between, and in an append-only log a closer landing after that
        is a permanent regression no later reconcile undoes.

        So ``still_applies`` is re-asked HERE, under the writing lock, with the
        CURRENT projection values (``_get_log`` has folded concurrent commits).
        ``None`` means the closer does not apply now, a normal outcome.

        It also gets ``observed``, the values the caller decided on: a patrol
        stopped and RE-ARMED in the window reads ``armed`` like the original, and
        only ``observed`` tells the new episode from the old one.

        Not gated on ``asOfSeq``: that is the max across every cell, so any
        unrelated event would advance it and the closer would never fire. The
        predicate compares the block the closer concerns. It must not re-enter the
        service (``snapshot`` takes this same non-reentrant lock).

        The predicate is asked against a state no foreign commit can have moved,
        and that guarantee comes from the store rather than from the per-slug lock
        above. The per-slug lock orders this process's writers, and for them it is
        enough: a concurrent in-process append queues behind this hold and lands
        after, which is the winning order. It says nothing about another PROCESS,
        and the member log has more than one writer -- an entry another process
        commits between our fold and our write lands FIRST, and a last-wins
        projection then takes ours as the newer word for a state that had already
        moved.

        What the store is given is the seq this fold reached, and what runs inside
        its hold is ONE comparison against the tail. The fold itself parses the log,
        and a parse under a cross-process lock is a hold nothing bounds -- a peer
        append gives up after a bounded wait and its event is then lost for good, so
        the expensive half stays outside, and passing a seq rather than a callback is
        what keeps it there. A foreign commit makes the tail exceed what we folded,
        the append declines without writing, and the loop folds that entry and asks
        the predicate again. Seqs only increase, so the comparison cannot be fooled
        by a tail that moved and came back.
        """
        lock = self._slug_lock(slug)
        with lock:
            # ONE contention budget for the whole loop, not one per attempt. Each
            # attempt's append waits out a lease collision, and the per-slug lock is
            # held across all of them, so a per-attempt budget would multiply the
            # worst-case hold by the attempt count and stall every other in-process
            # writer for this member that much longer.
            deadline = time.monotonic() + APPEND_CONTENTION_SECONDS
            for _ in range(_CLOSER_TAIL_ATTEMPTS):
                log = self._get_log(slug)
                if log is None:
                    return None
                self._fold_gap_locked(slug, log)
                # The newest seq every cell has folded, which is the state the
                # predicate is about to read. An empty log reports -1 (no cell has
                # been driven), and the store reports 0 for a file with a header and
                # no events, so the two agree only once the floor is clamped up.
                folded_to = max(self._registry.observed_floor(slug), 0)
                values = self._registry.snapshot(slug).get("values", {})
                if not still_applies(
                    values if isinstance(values, dict) else {},
                    observed if isinstance(observed, dict) else {},
                ):
                    return None
                event = log.append_if(type, data, max_tail_seq=folded_to, deadline=deadline)
                if event is not None:
                    # No gap fold here, unlike the plain append path. The write only
                    # happened because the tail was still at or below what the fold
                    # above reached, and this event takes the seq straight after it,
                    # so the range a gap fold would cover is empty by construction --
                    # and it is not free: it streams the whole file to discover that.
                    # ``drive`` folds this event, which is the only new one.
                    self._registry.drive(slug, event)
                    return event
            # Every attempt lost the race. Not writing is the safe direction (a
            # closer written against moved state is permanent), but it leaves the
            # projection stale, so this warns and raises: a caller that runs ONCE
            # must be able to tell "does not apply" from "never got a clean window".
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
            # Nothing is folded yet (no cell, or one primed over an empty stream), and
            # such a cell cannot be driven a range, so prime over the same events,
            # which reaches the cold-fold state. Returning instead would leave other
            # processes' entries unfolded, and the next append would drive past them.
            # ``below`` still bounds it, so the append path's ``drive`` fires the change
            # callbacks ``prime`` withholds.
            self._registry.prime(
                slug,
                (ev for ev in log.iter_events() if below is None or ev["seq"] < below),
            )
            return
        for earlier in log.iter_events():
            if earlier["seq"] <= floor:
                continue
            if below is not None and earlier["seq"] >= below:
                break
            self._registry.drive(slug, earlier)

    def _append_locked(self, slug: str, log: MemberLog, type: str, data: dict) -> Event:
        """Append + fold; caller holds the per-slug lock."""
        event = log.append(type, data)
        # Another process (`kirocrew-core`) may have committed entries since our
        # last load, so the returned seq can sit above our fold. Driving only this
        # event would advance `observed_seq` past them and `drive` would then drop
        # them for good, so fold the gap first. `drive` is idempotent per cell.
        self._fold_gap_locked(slug, log, below=event["seq"])
        self._registry.drive(slug, event)
        return event

    # Removal
    def remove_unit(self, slug: str, *, still_unclaimed: Callable[[], bool]) -> str:
        """Remove *slug*'s crew log and forget it here. One of the ``REMOVE_*`` statuses.

        The member half of the door ``sessions._remove_session_crew_log`` is for
        sessions: a caller outside this package says WHICH member's history has no
        owner left, and this owns the two steps that knowledge implies -- the
        store's removal, and dropping what this service caches for that slug. A
        handler reaching into the store itself would do the first and forget the
        second, and a cached ``MemberLog`` that outlives its files answers reads
        for a member whose history is gone.

        *still_unclaimed* is the caller's reason, re-asked under the removal's own
        lease hold: the store calls it as the ``guard`` it requires. It takes no
        arguments because this reason is not a property of the file -- whether a
        member is still in the roster is a property of the config -- so re-reading
        the log here would answer a question nobody asked. Re-asking it at all is
        what the session sweep's guard is for: the caller decided outside the
        hold, and a same-name member committed in that window owns this very unit,
        because the unit is keyed by the slug and a recreated namesake derives the
        same one. The predicate answering false is an ordinary outcome, not a
        failure: nothing is removed and nothing is written.

        Called under the per-slug lock, so an append through this service is
        serialized against it rather than racing the unlink. The lock ENTRY is
        kept afterwards while the log and name caches are dropped: a later caller
        that found no entry would build a second lock for the same slug, and two
        threads holding different locks for one slug is worse than a dict entry
        for a member that is gone. Nothing else is dropped, because nothing else
        survives the removal as an answer -- the folded cells for this slug stay
        in the registry, and are unreachable through every read here, each of
        which returns empty once ``_get_log`` finds no file.

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

        **The caches go whenever the store removed anything, which includes a partial
        removal.** A ``failed`` status can mean the contents partly went, and a reader
        holding this slug's cached handle would then serve a projection folded from
        history that is gone. Dropping a cache entry costs a reopen and nothing else,
        so it is done on both ``removed`` and ``failed``; only ``owned`` -- where
        nothing was touched, because a namesake owns the slug again -- keeps them.
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
                        # The absence was overtaken: a peer created this member's unit
                        # inside the window that ended at that hold. There is a unit
                        # to lease now, so this re-runs the SAME call rather than
                        # spelling the removal a second way -- it re-asks the roster
                        # under its own hold and removes the fold with the source
                        # inside it, which is the pairing that made the source safe to
                        # remove in the first place.
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

        Built one member at a time rather than as a comprehension over
        ``last_seq``, because this is the SUBSCRIBE BASELINE: one member whose
        header is damaged would otherwise raise out of the whole dict and leave
        every OTHER member without a cursor, so a single corrupt file costs the
        baseline for all of them and nothing retries it.

        Skipping is the rule the store already states one level down, where a
        unit it cannot prove is skipped so that "one unreadable unit must not
        make the roster unlistable". A member left out is absent from the
        baseline rather than reported at -1: -1 is the cursor of a member with no
        log yet, and a client told that would prune what it has.
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

        The registry holds a bound method back to the service, so the service is
        freed only by the cyclic collector, and with it each :class:`CrewLog` lease.
        On Windows that strands an open descriptor under a torn-down directory and
        the next write fails, so a retiring caller must not depend on GC timing.

        This releases every cached lease now, drops the caches and severs the cycle.
        Idempotent and best-effort (one stuck log does not keep the rest leased);
        afterwards the service behaves like a fresh one and re-opens on demand.

        Called only on retirement (:func:`set_service`, or :func:`get_service` on a
        home change), never while an append is in flight on the instance.
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
        # Sever the registry -> bound-method -> service cycle so the collector is not
        # the only thing that can free this instance. Done last: a close above may
        # still touch the registry, and after this the service is inert.
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
        # A service is bound to the root it was created for. The root only
        # moves when the process's data home moves — never in production, but
        # every test repoints it — and a cached MemberLog from the old root
        # would then answer for a slug that lives elsewhere now. Rebuild.
        if _singleton is None or _singleton.root != root:
            previous = _singleton
            _singleton = MemberEventLogService(root, previous.broadcast if previous else None)
            # Retire the replaced service deterministically. It is reachable only
            # through its own registry cycle now, so its cached logs' write leases
            # would otherwise be released whenever the cyclic collector next runs --
            # which strands an open descriptor under the old (torn-down) root on
            # Windows. The root moves only at a test boundary, never in production,
            # so nothing here is mid-write.
            if previous is not None:
                previous.close()
        return _singleton


def set_service(svc: MemberEventLogService | None) -> None:
    """Test seam. Retires the outgoing service so it holds no lease afterwards."""
    global _singleton
    with _singleton_lock:
        previous = _singleton
        _singleton = svc
        # Release the replaced service's cached leases NOW rather than at the next
        # GC pass. A test resets the singleton at each boundary; without this the
        # retired service's CrewLog descriptors linger (it sits in a reference
        # cycle, so refcount never frees it), which is harmless on POSIX but blocks
        # the tmp-home teardown on Windows and fails the next writer on that worker.
        if previous is not None and previous is not svc:
            previous.close()
