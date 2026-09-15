"""MemberEventLogService — the one door to per-member append-only logs.

Contract (implemented in this module; callers import only from here):

    svc = get_service()                       # lazy singleton rooted at members.members_root()
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

from kiro_crew.eventlog import types
from kiro_crew.eventlog.log import MemberLog
from kiro_crew.eventlog.members_projections import all_units
from kiro_crew.eventlog.projection import ProjectionRegistry
from kiro_crew.eventlog.types import Event

logger = logging.getLogger(__name__)

Broadcast = Callable[[str, object], None]

#: Called after every successful append with ``(kind, id, event)``. The kind is
#: passed even though this service only serves ``member``, so the hub it feeds
#: stays kind-generic and a second kind's service is a registration rather than
#: a second fan-out path.
EventSink = Callable[[str, str, Event], None]

#: The unit kind this service serves, as registered in ``eventlog.contrib``.
UNIT_KIND = "member"

_singleton: "MemberEventLogService | None" = None
_singleton_lock = threading.Lock()

_LOG_FILE_NAME = "log.jsonl"


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
        """The members root this service is bound to."""
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
        try:
            fn(types.WS_MEMBER_PROJECTION, {"slug": slug, "key": key, "value": view, "seq": seq})
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
        from kiro_crew.members import member_dir

        return member_dir(slug) / _LOG_FILE_NAME

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
            path = self._log_path(slug)
            if not path.exists():
                return None
            log = MemberLog(path)
            log.load()
            events = log.all_events()
            if log.header is not None:
                self._names[slug] = log.header.get("name")
            self._registry.prime(slug, events)
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
        path = self._log_path(slug)
        lock = self._slug_lock(slug)
        with lock:
            if path.exists():
                return
            log = MemberLog(path)
            log.create(name)
            log.load()
            self._names[slug] = name
            with self._map_lock:
                self._logs[slug] = log
            self._registry.prime(slug, log.all_events())
            # Migrate legacy files into events, in order.
            self._migrate_legacy(slug, name, log)

    def _migrate_legacy(self, slug: str, name: str, log: MemberLog) -> None:
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
                self._append_locked(slug, log, types.MEMBER_BINDING, {"slot_key": slot_key})

        # 2. member rules -> member/rules {text}
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
        for row in _read_legacy_activity_files(slug):
            self._append_locked(slug, log, types.ACTIVITY_RECORD, row)

    def slugs(self) -> list[str]:
        out: list[str] = []
        if not self._root.exists():
            return out
        for child in self._root.iterdir():
            if child.is_dir() and (child / _LOG_FILE_NAME).exists():
                out.append(child.name)
        return sorted(out)

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

    def history(self, slug: str, *, before: int | None = None, limit: int = 50) -> list[Event]:
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
    """Lazy process-wide singleton rooted at ``kiro_crew.members.members_root()``."""
    global _singleton
    with _singleton_lock:
        from kiro_crew.members import members_root

        root = members_root()
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
