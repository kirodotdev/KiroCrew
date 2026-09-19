"""One member's append-only log, stored as a ``member``-kind crew log.

This module is an ADAPTER, not a log implementation. The bytes, the locking, the
durability and the repair all belong to :mod:`kiro_crew.crew_log.store`, which
already owns them for the ``crew`` and ``session`` kinds::

    <data home>/crew-log/members/<store name>/log.jsonl

A member's log is a third KIND there rather than a second mechanism beside it.
That is the whole point of this file being thin: the ``crew-log`` root is masked
from the sandbox and fenced against the agent file tools (``security/paths.py``,
``sandbox.py``), named at the root so every kind inherits it. A per-member log
stored anywhere else would need its own fence entry, and the next log added would
miss it the same way -- an append-only record the agent can rewrite is not an
append-only record, and that property has to hold by WHERE THE FILE LIVES rather
than by someone remembering to list it.

Two translations live here and nowhere else, so nothing above this layer changes:

**Sequence numbers.** A crew log numbers the header 0 and the first entry 1. This
surface has always numbered the first event 0, and its ``seq`` travels to the
browser in ``member_projection`` frames and back in catch-up reads, so the
translation is applied at the boundary (``entry.seq - 1``) rather than renumbering
a protocol that clients already speak.

**Contributed event types.** A crew log keeps exactly one guest TYPE namespace,
``app:<name>/<action>``, and grants it to the ``member`` kind. The contribution
protocol spells the same thing ``<app>/<action>``. The stored form takes the
``app:`` prefix so the log's own ownership rule decides the write, and the read
gives the protocol spelling back, so an app's declared ``contributions.events``
and every frame carrying them are untouched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kiro_crew.crew_log.errors import (
    CODE_ALREADY_EXISTS,
    CODE_BAD_DATA,
    CODE_NO_LEDGER,
    CrewLogError,
)
from kiro_crew.crew_log.schema import APP_SOURCE_PREFIX, KIND_MEMBER
from kiro_crew.crew_log.store import CrewLog, crew_log_path
from kiro_crew.eventlog.types import (
    Event,
    is_contributed_event_type,
    is_known_event_type,
)

#: The fixed emitter for every built-in event. These facts are observed BY the
#: gateway about the member, never written by the member itself, which is why the
#: ``member`` kind takes no ``crew:`` guest at all.
_BUILTIN_SRC = "gateway"


class LogCorrupt(Exception):
    """A committed region of a member log is unreadable.

    Kept as this module's own exception because its callers catch it by name. It
    now wraps the refusal the crew log store raises rather than detecting
    corruption here: a gap or an unparseable committed line is that layer's
    judgement to make, and making it twice is how two answers drift apart.
    """

    def __init__(self, path: Path, line_no: int, detail: str) -> None:
        self.path = path
        self.line_no = line_no
        super().__init__(f"{path}: line {line_no}: {detail}")


def _stored_type(type_: str) -> str:
    """The spelling the crew log stores for *type_*.

    A contributed ``<app>/<action>`` becomes ``app:<app>/<action>``; a built-in
    type is already a domain the ``member`` kind owns and is returned unchanged.
    """
    if is_contributed_event_type(type_):
        return APP_SOURCE_PREFIX + type_
    return type_


def _wire_type(stored: str) -> str:
    """The protocol spelling for a *stored* type -- the inverse of :func:`_stored_type`."""
    if stored.startswith(APP_SOURCE_PREFIX):
        return stored[len(APP_SOURCE_PREFIX) :]
    return stored


def _src_for(stored_type: str) -> str:
    """The emitter to record for *stored_type*.

    A guest type carries its app's identity in the type itself, and the crew log
    requires the two to agree -- an ``app:<name>`` emitter may write only under its
    own ``app:<name>/`` prefix -- so the emitter is DERIVED here instead of being
    passed in. A caller that could name a different app than the type it is writing
    would be a caller that can attribute an entry to somebody else.
    """
    if stored_type.startswith(APP_SOURCE_PREFIX):
        domain = stored_type.split("/", 1)[0]
        return domain
    return _BUILTIN_SRC


def _as_event(entry: Any) -> Event:
    """One crew log entry as this surface's :class:`Event`."""
    return {
        "type": _wire_type(entry.type),
        "seq": entry.seq - 1,
        "time": entry.time,
        "data": entry.data,
    }


class MemberLog:
    """Append-only log for one member, backed by a ``member``-kind crew log."""

    def __init__(self, slug: str) -> None:
        self.slug = str(slug)
        self.header: dict | None = None
        self.events: list[Event] = []
        self._ledger: CrewLog | None = None
        self._loaded = False

    @property
    def path(self) -> Path:
        """The file the crew log store keeps this member's entries in."""
        return crew_log_path(KIND_MEMBER, self.slug)

    @property
    def committed_bytes(self) -> int:
        """Bytes durably committed, i.e. the size of the stored log."""
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    # ---- lifecycle --------------------------------------------------------
    def create(self, name: str) -> None:
        """Materialise the log with its header, or do nothing if it exists.

        Atomicity, the private directory mode and the directory fsync are the
        store's, which does all three for every kind. ``name`` is written as the
        header's optional display name so a cold reader has one before it has
        folded anything; a later rename arrives as a ``member/config`` fact that a
        fold applies over it.
        """
        if CrewLog.exists(KIND_MEMBER, self.slug):
            return
        try:
            CrewLog.create(KIND_MEMBER, self.slug, name=name)
        except CrewLogError as exc:
            # Another writer won the race between the check and the create. The
            # store's own refusal is the authority on that, and the log it refused
            # to overwrite is the one we wanted, so this is a no-op and not a fault.
            if getattr(exc, "code", "") != CODE_ALREADY_EXISTS:
                raise
        self._loaded = False

    def load(self) -> None:
        self._loaded = False
        self._ensure_loaded()

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        try:
            ledger = CrewLog.open(KIND_MEMBER, self.slug)
        except CrewLogError as exc:
            # Absent is an answer, not a fault: the callers above treat "no log for
            # this slug" as an empty read. Any OTHER refusal is the store judging
            # the committed region unreadable, which is precisely LogCorrupt.
            if getattr(exc, "code", "") == CODE_NO_LEDGER:
                self._ledger = None
                self.header = None
                self.events = []
                self._loaded = True
                return
            raise LogCorrupt(self.path, 0, str(exc)) from exc
        self._ledger = ledger
        self.header = ledger.header.to_dict()
        try:
            self.events = [_as_event(entry) for entry in ledger.iter_from(1)]
        except CrewLogError as exc:
            raise LogCorrupt(self.path, 0, str(exc)) from exc
        self._loaded = True

    # ---- write ------------------------------------------------------------
    def append(self, type: str, data: dict) -> Event:
        """Append one event and return it.

        The type check stays here because it is this surface's vocabulary: the
        crew log owns the four domains but not the built-in action names, so a
        typo'd built-in would otherwise be written as a fact nothing folds.
        """
        if not is_known_event_type(type):
            raise ValueError(f"unknown event type {type!r}")
        self._ensure_loaded()
        if self._ledger is None:
            raise LogCorrupt(self.path, 0, "cannot append to a log with no header")
        stored = _stored_type(type)
        try:
            entry = self._ledger.append(stored, data, src=_src_for(stored))
        except CrewLogError as exc:
            # A refused PAYLOAD keeps this surface's ValueError, the same type the
            # unknown-type check above raises: a caller telling a client's bad
            # append apart from a server fault reads the exception type, and
            # splitting one bad-input answer across two types is how the caller
            # starts reporting half of them as a fault. The store still makes the
            # judgement -- this only carries its verdict in the shape callers
            # already handle.
            if getattr(exc, "code", "") == CODE_BAD_DATA:
                raise ValueError(str(exc)) from exc
            raise
        event = _as_event(entry)
        # Refresh from disk rather than appending to the cached list: the store
        # re-reads under its own lock on every write, so another PROCESS appending
        # between our last load and this one is already committed ahead of us. A
        # cache that only grew by our own entry would hold a hole at those seqs
        # and answer reads from it.
        self._loaded = False
        self._ensure_loaded()
        return event

    # ---- read -------------------------------------------------------------
    def history(self, before: int | None, limit: int | None) -> list[Event]:
        """Newest-first page of events with ``seq < before`` (or all)."""
        self._ensure_loaded()
        evs = self.events
        if before is not None:
            evs = [e for e in evs if e["seq"] < before]
        newest_first = list(reversed(evs))
        if limit is not None and limit >= 0:
            return newest_first[:limit]
        return newest_first

    def events_after(self, after: int, limit: int) -> list[Event]:
        """Oldest-first page of events with ``seq > after``, at most *limit*.

        The catch-up read of the contribution protocol's §3: a consumer that
        folded up to ``after`` asks for what came next, in the order it must fold
        it. Distinct from :meth:`history`, which pages BACKWARDS for a timeline
        view -- folding a newest-first page would apply a later event before an
        earlier one.
        """
        self._ensure_loaded()
        out = [e for e in self.events if e["seq"] > after]
        if limit is not None and limit >= 0:
            return out[:limit]
        return out

    def last_seq(self) -> int:
        self._ensure_loaded()
        return len(self.events) - 1

    def all_events(self) -> list[Event]:
        """A copy of every event, oldest-first (for priming projections)."""
        self._ensure_loaded()
        return list(self.events)

    def exists(self) -> bool:
        return CrewLog.exists(KIND_MEMBER, self.slug)
