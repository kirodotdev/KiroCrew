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
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from kiro_crew import platform_compat
from kiro_crew.atomic_write import fsync_dir
from kiro_crew.crew_log.checkpoint import PrefixWitness, witness_mapping
from kiro_crew.crew_log.schema import KIND_MEMBER
from kiro_crew.eventlog import members_projections, types
from kiro_crew.eventlog.log import MemberLog
from kiro_crew.eventlog.members_projections import all_units
from kiro_crew.eventlog.types import Event
from kiro_crew.projection import EMPTY_WATERMARK, DirectoryCheckpointStore, ProjectionRegistry

logger = logging.getLogger(__name__)

Broadcast = Callable[[str, object], None]

#: Events a prime must have folded past its savepoint before a new one is written.
#: A savepoint is allowed to LAG -- resuming from an older one replays more tail and
#: reaches the same value -- so a write is spent only when it saves a meaningful
#: replay. Without this every load of every member would rewrite one file per
#: registered unit, which is the cost savepoints exist to remove rather than move,
#: and a short-lived member would leave files behind that folding from the start
#: already handles for free. Matches the crew log's own ``MIN_ADVANCE_ENTRIES``.
_SAVEPOINT_MIN_ADVANCE = 256

#: Called after every successful append with ``(kind, id, event)``. The kind is
#: passed even though this service only serves ``member``, so the hub it feeds
#: stays kind-generic and a second kind's service is a registration rather than
#: a second fan-out path.
EventSink = Callable[[str, str, Event], None]


def _redact_projection_value(value: object) -> object:
    """Redact every string in a projection view before it leaves over the WS.

    Runs the shared exfiltration-URL + credential chain the dashboard's HTTP
    reads use, recursively, so a credential- or presigned-URL-shaped value an
    operator planted in an activity ``project`` (or any nested string) cannot
    reach the browser through the live projection push.
    """
    from kiro_crew.security.exfil import redact_exfiltration_urls
    from kiro_crew.security.redaction import redact_credentials

    if isinstance(value, str):
        text, _ = redact_exfiltration_urls(value)
        text, _ = redact_credentials(text)
        return text
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


def redact_projection_identifier(value: str) -> str:
    """Redact a single app-chosen IDENTIFIER -- a string in, a string out.

    Same chain as :func:`_redact_projection_value`, narrowed to the one shape an
    identifier has. It exists because a projection ``key`` and an event ``type`` are
    typed ``str`` at their egress sites (``Event`` is a ``TypedDict``), while the
    recursive redactor is typed ``object -> object`` for the nested payload it walks.
    Passing the broad one straight into a ``str`` field type-checks nowhere, and the
    honest fix is a narrow signature rather than a cast that silences the checker.

    A non-string somehow arriving here is returned unchanged rather than coerced: a
    caller's type error must not turn into a silently different stored identifier.
    """
    out = _redact_projection_value(value)
    return out if isinstance(out, str) else value


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


#: How much of a legacy activity file the fold will read. The file is
#: agent-writable and the fold runs on every ``ensure``, which the roster
#: projection calls, so an unbounded read sits on a request path.
MAX_LEGACY_ACTIVITY_BYTES = 8 * 1024 * 1024

UNIT_KIND = "member"

_singleton: "MemberEventLogService | None" = None
_singleton_lock = threading.Lock()


LEGACY_PROVENANCE_KEY = "legacy_unverified"
"""Marks an activity record imported from the pre-fold legacy file.

The event log is fenced, ordered and append-only, and a reader is entitled to
treat what is in it as having been written through those guarantees. Rows folded
in from the legacy activity file were NOT: that file is agent-writable and is read
on the first ``ensure`` for a member, so anything able to write it before the fold
chooses what the fold imports. Without a marker those rows become
indistinguishable from records this service itself appended, which presents
unauthenticated content with the ledger's own authority.

The marker does not drop them -- they are that member's real history as far as
anyone can tell, and discarding them would lose activity the dashboard has always
shown. It records that their provenance is the file, not this log, so a consumer
that needs the stronger claim can tell the two apart. :func:`_activity_key`
strips it, so adding it changes no row's migration identity.
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
    also covers the other direction: rows a pass before this change appended bare
    still match the marked ones a pass after it writes, so no member gets a
    duplicate for having been migrated by the older code.
    """
    if isinstance(row, dict) and LEGACY_PROVENANCE_KEY in row:
        row = {k: v for k, v in row.items() if k != LEGACY_PROVENANCE_KEY}
    try:
        return json.dumps(row, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return repr(row)


def _open_legacy_regular_file(path: Path) -> TextIO | None:
    """*path* opened for reading, or ``None`` unless it is a plain regular file.

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

    ``None`` means ONLY "this file is not something to migrate from" -- it does not
    exist, or it is not a regular file. A genuine I/O FAILURE is raised instead of
    being folded into ``None``, and the difference decides whether rows are lost:
    the caller keeps ``complete`` true when there is nothing to migrate, and the
    retirement it then writes is permanent and never re-runs the fold. If a transient
    open failure (EMFILE, a permission blip, an unresponsive mount) were reported as
    absence, the rows in an existing file would be retired UNREAD, with no recovery.
    The caller already separates the two: ``FileNotFoundError`` continues, any other
    ``OSError`` sets ``complete = False``.
    """
    try:
        fd = platform_compat.open_file_no_reparse(path, nonblocking=True)
    except OSError:
        # Raised on, not swallowed. FileNotFoundError is an OSError subclass and the
        # caller catches it FIRST, so a genuinely absent file still reads as absence.
        raise
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
    ``members.read_activity``: that function now reads the event log, and the
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
        # ``activity.jsonl`` reopened the whole path -- a guard an adversary can
        # remove is not a guard. Retiring the source by rename stays, but as hygiene
        # and to keep the byte budget off a file already folded, not as the
        # protection.
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
                            "legacy activity at %s exceeds %d bytes; the rest is not " "migrated",
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


def _retire_legacy_activity(slug: str) -> None:
    """Mark the fold complete so a later write cannot enter the ledger.

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
        return
    # The fenced marker is written and made DURABLE BEFORE the legacy name is
    # freed. In the other order a crash between the rename and the marker leaves the
    # live name available with no marker recorded, and the next ensure folds whatever
    # an agent has since written there as trusted history -- the exact forgery the
    # marker exists to close, reopened by a power cut. This order's crash window
    # costs nothing: the marker is present, the rows were appended before this ran,
    # and the un-renamed file is simply never read again.
    #
    # Written for every member whose fold completed, including one with no legacy
    # file at all. A member with nothing to migrate is exactly the case an earlier
    # rename-gated marker missed, which left the members with the LEAST to migrate as
    # the only ones whose legacy name stayed open for whoever wrote it next.
    fenced = _legacy_folded_marker_path(slug)
    if fenced is None:
        return
    try:
        fenced.parent.mkdir(parents=True, exist_ok=True)
        fenced.touch(exist_ok=True)
        # touch() puts the directory entry in the page cache only. Without this the
        # entry can be absent after a crash while the rename below has already
        # landed, which is the one combination the ordering above rules out.
        fsync_dir(fenced.parent)
    except OSError:
        # Unwritten, so nothing is renamed either: the next ensure folds again rather
        # than trusting a source it cannot prove it has finished with. Idempotent by
        # the counted dedupe.
        logger.debug("could not mark legacy activity folded for %r", slug, exc_info=True)
        return
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
            return


class MemberEventLogService:
    def __init__(self, root: Path, broadcast: Broadcast | None = None) -> None:
        self._root = Path(root)
        self._broadcast = broadcast
        self._logs: dict[str, MemberLog] = {}
        self._slug_locks: dict[str, threading.Lock] = {}
        self._map_lock = threading.Lock()
        self._registry = ProjectionRegistry()
        #: Per-append sink that fans events to log subscribers; attached once
        #: at dashboard startup by the contribution protocol's WS hub.
        self._event_sink: EventSink | None = None
        for unit in all_units():
            self._registry.register(unit)
        self._registry.set_on_change(self._on_change)
        # Names carried by each slug's header, overlaid onto the roster view.
        self._names: dict[str, str] = {}

    # ---- wiring -----------------------------------------------------------
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

    # ---- internal plumbing ------------------------------------------------
    def _log_path(self, slug: str) -> Path:
        """Where this member's log lives -- inside the fenced ``crew-log`` tree.

        The store owns the layout, including the readable-plus-digest fold of the
        slug that names the directory, so this asks it rather than composing a
        path. That is what puts the file under the root the sandbox masks and the
        agent file tools refuse.
        """
        from kiro_crew.crew_log.store import crew_log_path

        return crew_log_path(KIND_MEMBER, slug)

    def attach_event_sink(self, sink: "EventSink | None") -> None:
        """Set the per-append sink that fans events to log subscribers.

        Called once at dashboard startup with the eventlog WebSocket hub. The
        sink runs INSIDE the per-slug lock, on whatever thread appended, so it
        must only enqueue -- see ``dashboard.eventlog_ws.EventLogHub.publish``,
        which does exactly that and never blocks or raises.
        """
        self._event_sink = sink

    @property
    def event_sink(self) -> "EventSink | None":
        """The attached sink, so a service rebuild can carry it over."""
        return self._event_sink

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

        WHAT THIS SAVES, stated honestly: the FOLD, not the read. ``MemberLog``
        materialises its event list on load, so the file is parsed either way; what
        the watermark removes is one ``apply`` per definition per skipped event, which
        with four registered units is the dominant cost of priming a long-lived
        member. Removing the read cost too needs a windowed reader that starts at a
        seq, which is a separate change to the log rather than to the fold.
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
            # ``prime_checkpointed`` calls this ONCE with the floor and then consumes
            # what it returns, so this is the only moment that knows the floor and is
            # still ahead of the pass -- and a witness is evidence only when it was
            # read before the bytes were folded.
            #
            # A resume needs one whatever it intends to write: the restored state
            # stands on a prefix this pass never revisits, so without a digest read
            # here nothing afterwards can say that prefix is still in the file. A pass
            # that resumed nothing asks only whether a write is owed, which keeps the
            # boundary walk off the reads that could not spend it either way.
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

    # ---- units ------------------------------------------------------------
    def ensure(self, slug: str, name: str, config=None) -> None:
        from kiro_crew.members import validate_slug

        validate_slug(slug)
        lock = self._slug_lock(slug)
        with lock:
            log = MemberLog(slug)
            fresh = not log.exists()
            if fresh:
                # The header is written ONCE, so this call decides what the log says
                # it belongs to for life. A writer with no name in hand reaches here
                # with the slug (``emit`` passes ``name or slug``), and the slug names
                # nobody: the roster has to treat it as unnamed, which costs this log
                # its collision check for good. Resolve the exact name from the roster
                # instead, and use it for the migration below too, whose rules and
                # binding reads are name-scoped. Only on the fresh path, so a member's
                # config is read once ever rather than on every message.
                name = self._resolved_name(slug, name, config)
                log.create(name)
            log.load()

            # The HEADER decides who this log belongs to, not this call's argument.
            # It is written once, so on an EXISTING log the argument is only whatever
            # this writer happened to hold -- and ``emit`` passes ``name or slug``, so
            # a nameless writer holds the slug. Taking that would overwrite the real
            # name with a placeholder, and ``_names`` is what scopes the activity
            # view, so the member's own entries (recorded under their real name)
            # would be scoped out of their own projection. :meth:`_get_log` already
            # answers this from the header; reading it here is the same answer, so
            # the write path and the read path cannot disagree about one slug. The
            # migration below takes it too: its rules and binding reads are
            # name-scoped, and the header name is the name they are scoped by.
            header_name = (log.header or {}).get("name")
            if isinstance(header_name, str) and header_name:
                name = header_name

            self._names[slug] = name
            with self._map_lock:
                self._logs[slug] = log
            self._registry.prime(slug, log.iter_events())

            # Run the migration on EVERY ensure, not only at create: returning
            # early whenever the log existed meant a process that died between
            # `create` and the end of the migration left that member's bindings,
            # rules and activity unmigrated for good. Each item is skipped once the
            # log carries its event, so the pass is idempotent and cheap.
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

        Called on every ``ensure``, so each item asks whether the log already
        carries its event and skips the legacy read when it does. That is what lets
        an interrupted migration resume: whatever the dead run got through stays
        done, and whatever it did not is picked up on the next call.

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
        lease = self._hold_unit(slug)
        if lease is None:
            logger.debug("legacy fold for %r skipped: another process holds the log", slug)
            return
        from kiro_crew.crew_log.lease import release as release_lease

        try:
            self._migrate_legacy_locked(slug, name, log)
        finally:
            release_lease(lease)

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

    def _migrate_legacy_locked(self, slug: str, name: str, log: MemberLog) -> None:
        """The fold itself. Runs only with this member's unit lease held."""
        from kiro_crew import members

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
                logger.debug("legacy binding read failed for %r", slug, exc_info=True)
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
                logger.debug("legacy rules read failed for %r", slug, exc_info=True)
            if text:
                self._append_locked(slug, log, types.MEMBER_RULES, {"text": text})

        # 3. activity.jsonl(.1) -> activity/record, oldest first.
        # Read the legacy FILES directly: ``members.read_activity`` now reads
        # from this very log through ``history()``, which takes the per-slug
        # lock the caller already holds. Going through it here deadlocks.
        # Per ROW, not per type: a crash after the first append leaves the log
        # holding one activity record, and skipping on "any record exists" would
        # then drop every remaining legacy row for good. Keyed on the row's own
        # canonical JSON, which is what the append stores, so a row already in the
        # log matches itself exactly.
        # Counted, NOT a set: legacy rows are not distinct. Two identical rows are
        # an ordinary shape (one member, one second, the same via and project), so
        # a set would let the single copy a crashed pass had already appended stand
        # for every occurrence and silently drop the rest. Each legacy row consumes
        # ONE recorded match, and a row with no match left is appended.
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
        # Retired whether or not the pass READ anything -- an empty or absent legacy
        # file yields no rows, so a retirement gated on rows would leave that
        # member's marker unwritten forever and the path open for whoever writes the
        # file next. What the marker records is that the fold COMPLETED, not that it
        # found something.
        #
        # But only when it did complete. A read the byte budget cut short, or one an
        # OSError interrupted, has rows it never saw -- and retirement is one-way, so
        # finalising an incomplete fold discards them permanently. An unretired
        # source is re-read on the next ensure, which the counted dedupe above makes
        # safe; an over-budget file stays unretired and is reported every pass, which
        # is the correct outcome for a file too large to migrate.
        if legacy_complete:
            _retire_legacy_activity(slug)

    def logged_name(self, slug: str) -> str | None:
        """The EXACT member name this slug's log was created for, or None.

        A slug is lossy -- ``slug_for_name`` says so, and `Review_Agent` and
        `review-agent` both fold to `review-agent`. Colliding names are SUPPORTED
        (each activity entry stores the exact name, which is what keeps attribution
        working), so this is a query rather than a refusal: a caller that presents
        per-member state has to know the log it is reading belongs to the member it
        is rendering, and only the header can tell it.

        One answer needs care at the CALLER: a header whose name IS the slug names no
        member. ``ensure`` writes the header only while the log is fresh, so a writer
        that does not know the name and passes the slug locks that placeholder in for
        the log's whole life. It is reported as held, because it is what the header
        holds, but a caller comparing it against a real name must not read it as a
        second member -- a slug is a lossy fold, so the placeholder differs from
        almost every real name.
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

    # ---- write ------------------------------------------------------------
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

        A closer is decided from a SNAPSHOT and written afterwards, and between those
        two the state it describes can legitimately change: the startup reconcile runs
        as a background task concurrent with the gateway going live, so a slot it read
        as durably open can be reopened, or a patrol re-armed, by a live writer before
        the closer lands. The log is append-only with no compaction, so a closer that
        lands after a live open is a permanent regression of the projection -- and not
        a self-correcting one, because a later restart's reconcile reads the state as
        closed and has no reason to reopen it.

        ``still_applies`` is therefore re-asked HERE, under the lock that writes, and
        is handed the CURRENT projection values rather than the caller's snapshot.
        ``_get_log`` has already folded anything a concurrent writer committed, so
        those values include the live change the caller could not have seen. Returns
        ``None`` when the closer does not apply, which is a normal outcome and not
        a failure: it means the state closed itself while the reconcile was deciding.

        It is handed ``observed`` -- the values the caller DECIDED on -- as a second
        argument, because the current state alone cannot answer the question. The
        state is not the episode: a patrol that stopped and was RE-ARMED in this
        window reads ``armed`` exactly like the original episode the caller meant to
        close, so a predicate that tests only the current state stamps the NEW
        episode with the OLD one's closer, and the fresh patrol then reads as
        stopped. Comparing against ``observed`` is what tells those two apart.

        Note what is deliberately NOT done: refusing whenever the projection sequence
        advanced. ``snapshot`` reports one ``asOfSeq`` for the whole slug -- the max
        across every projection cell -- and ``_get_log`` folds all pending commits
        first, so ANY unrelated event on the member advances it and a closer gated on
        it would essentially never fire. The comparison belongs on the projection
        block the closer concerns, which is why the predicate makes it and not this
        method.

        This exists rather than a predicate on :meth:`append` because the predicate
        must not re-enter the service to read state -- ``snapshot`` takes this same
        non-reentrant lock, so a caller that reached for it would deadlock.
        """
        lock = self._slug_lock(slug)
        with lock:
            log = self._get_log(slug)
            if log is None:
                return None
            values = self._registry.snapshot(slug).get("values", {})
            if not still_applies(
                values if isinstance(values, dict) else {},
                observed if isinstance(observed, dict) else {},
            ):
                return None
            return self._append_locked(slug, log, type, data)

    def _fold_gap_locked(self, slug: str, log: MemberLog, *, below: int | None = None) -> None:
        """Fold events on disk that this process has not folded; caller holds the lock.

        The gateway is not the log's only writer, so entries can be committed
        between our loads. ``drive`` advances each cell's ``observed_seq`` and then
        drops anything at or below it, so folding a newer event first would strand
        the ones before it for good -- they would never appear in a projection or a
        snapshot until a restart re-primed from the file.

        ``below`` bounds the range when the caller is about to drive an event of
        its own (the append path); a read passes nothing and folds to the end.
        ``drive`` is idempotent per cell, so replaying a seq a cell already holds
        costs it nothing.
        """
        floor = self._registry.observed_floor(slug)
        if floor < 0:
            # No cell yet: such a cell folds from init() over whatever it is first
            # driven with, so it must be primed rather than driven at a range.
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
        # `log.append` re-reads the file, so the seq it returns can sit ABOVE the
        # one after what this process folded: the gateway is not the only writer
        # (`kirocrew-core` runs as its own stdio subprocess and records member
        # activity through this same service), so entries can be committed
        # between our last load and this append. Driving only the returned event
        # would advance every cell's `observed_seq` straight to it, and `drive`
        # drops anything at or below that afterwards -- so those intervening
        # seqs would never fold, and the pushed projection and `snapshot` would
        # undercount them until a restart re-primed from the file.
        #
        # `drive` is idempotent per cell (it skips a seq that cell already has),
        # so replaying the range costs a cell nothing it has seen.
        self._fold_gap_locked(slug, log, below=event["seq"])
        self._registry.drive(slug, event)
        sink = self._event_sink
        if sink is not None:
            try:
                sink(UNIT_KIND, slug, event)
            except Exception:
                # The event is already durable and folded; a subscriber fan-out
                # fault must not turn a committed append into a failed one. The
                # subscriber detects the gap on its next seq check and heals with
                # a catch-up read, which is the contract's own recovery path.
                logger.debug("eventlog sink failed for %r/%r", slug, type, exc_info=True)
        return event

    def events_after(self, slug: str, *, after: int = -1, limit: int = 200) -> list[Event]:
        """Oldest-first page of events with ``seq > after`` (contribution protocol).

        The catch-up half of the delta channel: a subscriber that lost frames, or
        one starting cold, folds this page in order and then streams. Returns an
        empty list for a slug with no log rather than raising -- a caller asking
        about a unit that does not exist has already been answered 404 by the
        route's own existence check.
        """
        lock = self._slug_lock(slug)
        with lock:
            log = self._get_log(slug)
            if log is None:
                return []
            return log.events_after(after, limit)

    # ---- read -------------------------------------------------------------
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
            if previous is not None and previous.event_sink is not None:
                # The hub is attached once at startup and is not rebound when
                # the data home moves, so a rebuild that dropped the sink
                # would leave every later append invisible to its subscribers.
                _singleton.attach_event_sink(previous.event_sink)
        return _singleton


def set_service(svc: MemberEventLogService | None) -> None:
    """Test seam."""
    global _singleton
    with _singleton_lock:
        _singleton = svc
