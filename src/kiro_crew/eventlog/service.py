"""MemberEventLogService — the one door to per-member append-only logs.

Contract (implemented in this module; callers import only from here):

    svc = get_service()                       # lazy singleton rooted at the member crew log root
    svc.attach_broadcast(state.broadcast_ws)  # once, at dashboard startup
    svc.ensure(slug, name)                    # create the log + header if missing (migrates legacy files)
    ev = svc.append(slug, type, data)         # write + fsync, fold projections, push member_projection frames
    svc.snapshot(slug)                        # {"asOfSeq": int, "values": {key: view}}
    svc.history(slug, before=None, limit=50)  # newest-first page of envelopes
    svc.last_seq(slug)                        # -1 for an empty log
    svc.last_seqs()                           # {slug: last_seq} for every known log
    svc.slugs()                               # every member with a log on disk

All methods are synchronous. A write is one line appended under a per-slug
lock and fsync'd before it returns; projections fold in the same call and the
broadcast is only enqueued. Callers on the event loop pay one fsync per
append, which is the pilot's accepted cost.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

from kiro_crew.crew_log.schema import KIND_MEMBER
from kiro_crew.eventlog import types
from kiro_crew.eventlog.log import MemberLog
from kiro_crew.eventlog.members_projections import all_units
from kiro_crew.eventlog.projection import ProjectionRegistry
from kiro_crew.eventlog.types import Event

logger = logging.getLogger(__name__)

Broadcast = Callable[[str, object], None]


#: What replaces a node nested past :data:`types.MAX_VALUE_DEPTH` on egress. A
#: marker rather than a silent drop: a reader can tell "the host stopped here"
#: from "the contributor published nothing".
_TOO_DEEP = "[too deeply nested]"


def _redact_projection_value(value: object, _depth: int = 1) -> object:
    """Redact every string in a projection view before it leaves over the WS.

    Runs the shared exfiltration-URL + credential chain the dashboard's HTTP
    reads use, recursively, so a credential- or presigned-URL-shaped value an
    operator planted in an activity ``project`` (or any nested string) cannot
    reach the browser through the live projection push.

    Bounded at :data:`types.MAX_VALUE_DEPTH`, the same depth the contribution
    door refuses past. Without the bound this pass is where an over-deep payload
    lands: a contributor could craft one inside the 64 KiB cap, the recursion
    would raise, and the caller's fallback was the RAW value. A node past the
    bound is replaced by :data:`_TOO_DEEP` rather than descended, so the function
    is total for anything the store holds -- including a value an older release
    accepted before the door had a depth check.
    """
    from kiro_crew.security.exfil import redact_exfiltration_urls
    from kiro_crew.security.redaction import redact_credentials

    if isinstance(value, str):
        text, _ = redact_exfiltration_urls(value)
        text, _ = redact_credentials(text)
        return text
    if isinstance(value, (dict, list)) and _depth > types.MAX_VALUE_DEPTH:
        return _TOO_DEEP
    if isinstance(value, dict):
        # Redact keys too, not just values: a contributed projection key is
        # app-authored (`<app>/<name>`) and a nested data key can be arbitrary
        # agent text, so a credential- or URL-shaped key would otherwise cross
        # unredacted. Keys are strings in JSON; a non-string key is left as-is.
        out: dict = {}
        for k, v in value.items():
            rk = _redact_projection_value(k, _depth + 1) if isinstance(k, str) else k
            out[rk] = _redact_projection_value(v, _depth + 1)
        return out
    if isinstance(value, list):
        return [_redact_projection_value(v, _depth + 1) for v in value]
    return value


#: Called after every successful append with ``(kind, id, event)``. The kind is
#: passed even though this service only serves ``member``, so the hub it feeds
#: stays kind-generic and a second kind's service is a registration rather than
#: a second fan-out path.
EventSink = Callable[[str, str, Event], None]

#: The unit kind this service serves, as registered in ``eventlog.contrib``.
UNIT_KIND = "member"

_singleton: "MemberEventLogService | None" = None
_singleton_lock = threading.Lock()


def _read_legacy_activity_files(slug: str) -> list[dict]:
    """Rows from the pre-log ``activity.jsonl.1`` then ``activity.jsonl``, oldest first.

    Deliberately reads the files by hand rather than through
    ``members.read_activity``: that function now reads the event log, and the
    only caller here holds the per-slug lock it would need. Unparseable lines
    are skipped — the legacy writer was best-effort and never fsync'd, so a
    torn tail is expected, not corruption.
    """
    import json

    from kiro_crew import members

    rows: list[dict] = []
    try:
        base = members.member_dir(slug) / members.ACTIVITY_FILE_NAME
    except Exception:
        logger.debug("legacy activity path unavailable for %r", slug, exc_info=True)
        return rows
    for path in (base.with_name(base.name + ".1"), base):
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError:
            logger.debug("legacy activity read failed for %s", path, exc_info=True)
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("ts"):
                rows.append(row)
    return rows


class MemberEventLogService:
    def __init__(self, root: Path, broadcast: Broadcast | None = None) -> None:
        self._root = Path(root)
        self._broadcast = broadcast
        self._event_sink: EventSink | None = None
        self._logs: dict[str, MemberLog] = {}
        self._slug_locks: dict[str, threading.Lock] = {}
        self._map_lock = threading.Lock()
        self._registry = ProjectionRegistry()
        for unit in all_units():
            self._registry.register(unit)
        self._registry.set_on_change(self._on_change)
        # Names carried by each slug's header, overlaid onto the roster view.
        self._names: dict[str, str] = {}
        # Slugs whose legacy-file migration this PROCESS has seen through to the
        # end. A memo, never the source of truth: the log's own content is, which
        # is why a gateway killed mid-migration finishes the job on its next run
        # instead of inheriting a marker that lied about it.
        self._migrated: set[str] = set()
        # Per slug, the (member, session) pairs the log already carries a
        # participation record for. Rebuilt from the log on load, so it is EXACT
        # rather than a window: a bounded scan of recent events misses a session
        # that has since been pushed past the window and writes the duplicate
        # participation fact the caller asked to be deduped. O(1) to consult and
        # bounded by the log's own size.
        self._activity_pairs: dict[str, set[tuple[str, str]]] = {}

    # ---- wiring -----------------------------------------------------------
    def attach_broadcast(self, broadcast: Broadcast) -> None:
        self._broadcast = broadcast

    def attach_event_sink(self, sink: "EventSink | None") -> None:
        """Set the per-append sink that fans events to log subscribers.

        Called once at dashboard startup with the eventlog WebSocket hub. The
        sink runs INSIDE the per-slug lock, on whatever thread appended, so it
        must only enqueue -- see ``dashboard.eventlog_ws.EventLogHub.publish``,
        which does exactly that and never blocks or raises.
        """
        self._event_sink = sink

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

    @property
    def event_sink(self) -> "EventSink | None":
        """The per-append fan-out sink attached at dashboard startup, if any."""
        return self._event_sink

    def _on_change(self, slug: str, key: str, view: dict, seq: int) -> None:
        if key == types.PROJ_ROSTER:
            view = self._overlay_roster(slug, view)
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
            logger.debug("member projection redaction failed for %r/%r", slug, key, exc_info=True)
            egress = view
        try:
            fn(types.WS_MEMBER_PROJECTION, {"slug": slug, "key": key, "value": egress, "seq": seq})
        except Exception:
            logger.debug("member projection broadcast failed for %r/%r", slug, key, exc_info=True)

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
        if log is None:
            log = MemberLog(slug)
            if not log.exists():
                return None
            log.load()
            events = log.all_events()
            if log.header is not None:
                header_name = log.header.get("name")
                self._names[slug] = header_name if isinstance(header_name, str) else slug
            self._registry.prime(slug, events)
            self._prime_activity_pairs(slug, events)
            with self._map_lock:
                # Another thread may have primed concurrently; last writer wins
                # the map slot but priming is idempotent.
                existing = self._logs.get(slug)
                if existing is not None:
                    return existing
                self._logs[slug] = log
        return log

    # ---- units ------------------------------------------------------------
    def ensure(self, slug: str, name: str) -> None:
        from kiro_crew.members import validate_slug

        validate_slug(slug)
        lock = self._slug_lock(slug)
        with lock:
            log = MemberLog(slug)
            if log.exists():
                # Existing log: it may still be MID-MIGRATION. A crash (or a
                # gateway kill) between `create` below and the last legacy append
                # leaves a log that exists and holds only part of the member's
                # past, and returning here on existence alone made that permanent
                # -- every later ensure short-circuited, so the remaining binding,
                # rules and activity records were never migrated and nothing ever
                # said so. The completion marker is what tells the two apart.
                self._resume_migration(slug, name, log)
                return
            log.create(name)
            log.load()
            self._names[slug] = name
            with self._map_lock:
                self._logs[slug] = log
            self._registry.prime(slug, log.all_events())
            self._prime_activity_pairs(slug, log.all_events())
            # Migrate legacy files into events, in order.
            self._migrate_legacy(slug, name, log)

    def _resume_migration(self, slug: str, name: str, log: MemberLog) -> None:
        """Finish a migration that an earlier run started and did not complete.

        Caller holds the slug lock. Cheap after the first call per slug: this
        process remembers which migrations it finished, and the common case (no
        legacy files at all) is two failed stats inside :meth:`_migrate_legacy`.

        The migration is idempotent -- each legacy record is appended only when
        the log does not already hold it -- so resuming finishes an interrupted
        run rather than duplicating its first half.
        """
        try:
            loaded = self._get_log(slug)
            if loaded is None:  # pragma: no cover - exists() said otherwise
                return
            with self._map_lock:
                if slug in self._migrated:
                    return
            self._migrate_legacy(slug, self._names.get(slug) or name, loaded)
        except Exception:
            # Same posture as every other hook on this path: a member whose
            # migration cannot be finished must not break the caller that only
            # wanted to append. The next ensure tries again, because nothing
            # wrote the marker.
            logger.warning("member event log %r could not resume migration", slug, exc_info=True)

    @staticmethod
    def _activity_pair(data: object) -> "tuple[str, str] | None":
        """The (member, session) identity of one participation record, or None.

        BOTH fields: a colliding slug can put two members in one log, so the
        session alone would suppress the wrong member's entry. Only participation
        records carry ``session`` -- a routing decision carries ``decided_in`` and
        is a distinct fact, never deduped.
        """
        if not isinstance(data, dict):
            return None
        member = data.get("member")
        session = data.get("session")
        if isinstance(member, str) and isinstance(session, str) and member and session:
            return (member, session)
        return None

    def _prime_activity_pairs(self, slug: str, events: "list[Event]") -> None:
        pairs: set[tuple[str, str]] = set()
        for ev in events:
            if ev.get("type") != types.ACTIVITY_RECORD:
                continue
            pair = self._activity_pair(ev.get("data"))
            if pair is not None:
                pairs.add(pair)
        with self._map_lock:
            self._activity_pairs[slug] = pairs

    def _note_activity_pair(self, slug: str, event: "Event") -> None:
        if event.get("type") != types.ACTIVITY_RECORD:
            return
        pair = self._activity_pair(event.get("data"))
        if pair is None:
            return
        with self._map_lock:
            self._activity_pairs.setdefault(slug, set()).add(pair)

    def has_participation(self, slug: str, member: str, session: str) -> bool:
        """Whether this member's log already records *session* for *member*.

        Answers from the WHOLE log, not a recent window: the caller uses it to
        decide whether a participation record would be a duplicate, and a windowed
        answer says "no" as soon as the original has been pushed past the window.
        A slug with no log answers False -- there is nothing to duplicate.
        """
        if not slug or not member or not session:
            return False
        lock = self._slug_lock(slug)
        with lock:
            if self._get_log(slug) is None:
                return False
            with self._map_lock:
                return (member, session) in self._activity_pairs.get(slug, set())

    @staticmethod
    def _already_holds(log: MemberLog, type_: str, data: dict) -> bool:
        """Whether this exact record was already migrated into the log.

        Compares the whole ``data`` dict, which is what makes a resume idempotent:
        the legacy rows are fixed values read off disk, so a record that landed in
        the interrupted run is byte-identical to the one this run would append.
        """
        return any(ev.get("type") == type_ and ev.get("data") == data for ev in log.all_events())

    def _migrate_legacy(self, slug: str, name: str, log: MemberLog) -> None:
        """Fold the pre-log files into events, then mark the migration complete.

        IDEMPOTENT, step by step: each legacy record is appended only when the log
        does not already hold it, so a run that resumes an interrupted migration
        finishes it instead of duplicating its first half. The completion marker is
        written LAST -- only after every step has landed -- because its whole job
        is to prove that they did.
        """
        from kiro_crew import members

        # 1. DM binding -> member/binding {slot_key}
        try:
            binding = members.read_dm_binding(slug)
        except Exception:
            binding = None
            logger.debug("legacy binding read failed for %r", slug, exc_info=True)
        if binding is not None and binding.get("member") == name:
            slot_key = binding.get("slot_key")
            if isinstance(slot_key, str) and slot_key:
                data = {"slot_key": slot_key}
                if not self._already_holds(log, types.MEMBER_BINDING, data):
                    self._append_locked(slug, log, types.MEMBER_BINDING, data)

        # 2. member rules -> member/rules {text}
        try:
            text = members.read_member_rules(slug, name)
        except Exception:
            text = ""
            logger.debug("legacy rules read failed for %r", slug, exc_info=True)
        if text:
            data = {"text": text}
            if not self._already_holds(log, types.MEMBER_RULES, data):
                self._append_locked(slug, log, types.MEMBER_RULES, data)

        # 3. activity.jsonl(.1) -> activity/record, oldest first.
        # Read the legacy FILES directly: ``members.read_activity`` now reads
        # from this very log through ``history()``, which takes the per-slug
        # lock the caller already holds. Going through it here deadlocks.
        #
        # The already-present set is built ONCE: a member can carry thousands of
        # legacy rows, and asking per row would re-walk the log each time.
        present = {
            self._record_key(ev.get("data"))
            for ev in log.all_events()
            if ev.get("type") == types.ACTIVITY_RECORD
        }
        for row in _read_legacy_activity_files(slug):
            key = self._record_key(row)
            if key in present:
                continue
            self._append_locked(slug, log, types.ACTIVITY_RECORD, row)
            present.add(key)

        # Every legacy record is now in the log. Remembered for THIS PROCESS so
        # the next ensure does not re-read the legacy files: the durable truth
        # stays the log's own content, which is what makes a crash here
        # self-healing -- a new process re-derives what is missing and finishes
        # it, where a marker written before the last append would have said the
        # migration was done when it was not.
        with self._map_lock:
            self._migrated.add(slug)

    @staticmethod
    def _record_key(data: object) -> str:
        """A comparable key for one legacy activity row.

        JSON with sorted keys rather than the dict itself: dicts are unhashable,
        and the rows are plain JSON read off disk, so a stable serialization is an
        exact identity for them.
        """
        import json as _json

        try:
            return _json.dumps(data, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):  # pragma: no cover - rows come from JSON
            return repr(data)

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

    def _append_locked(self, slug: str, log: MemberLog, type: str, data: dict) -> Event:
        """Append + fold; caller holds the per-slug lock."""
        event = log.append(type, data)
        self._registry.drive(slug, event)
        self._note_activity_pair(slug, event)
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
        return snap

    def history(
        self, slug: str, *, before: int | None = None, limit: int | None = 50
    ) -> list[Event]:
        lock = self._slug_lock(slug)
        with lock:
            log = self._get_log(slug)
            if log is None:
                return []
            return log.history(before, limit)

    def events_after(self, slug: str, *, after: int = -1, limit: int = 200) -> list[Event]:
        """Oldest-first page of events with ``seq > after`` (contribution §3).

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

    def last_seq(self, slug: str) -> int:
        lock = self._slug_lock(slug)
        with lock:
            log = self._get_log(slug)
            if log is None:
                return -1
            return log.last_seq()

    def last_seqs(self) -> dict[str, int]:
        return {slug: self.last_seq(slug) for slug in self.slugs()}


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
                # The hub is attached once at startup and is not rebound when the
                # data home moves, so a rebuild that dropped the sink would leave
                # every later append invisible to its subscribers.
                _singleton.attach_event_sink(previous.event_sink)
        return _singleton


def set_service(svc: MemberEventLogService | None) -> None:
    """Test seam."""
    global _singleton
    with _singleton_lock:
        _singleton = svc
