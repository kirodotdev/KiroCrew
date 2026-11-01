"""What every owner of the session crew-log emitter reads: the switch, the gates, the maps.

:mod:`kiro_crew.crew_log.emit` is the emitter's import path and patch surface; this module is
the lowest of the owners it is split into. It holds the switch that says whether the emitter
writes (:func:`enabled`), the two boot-path import gates -- :func:`_crew_log` for the storage
package and :func:`_note_eager` for the eager fold's wake -- the session kind and the two sources
an entry is signed with, :func:`session_id_of`, and the process-wide maps the entry points share,
with :data:`_lock`, which guards them.

The collaborators the emitter installs and the counters a ``global`` statement rebinds are not
here: they live in :mod:`kiro_crew.crew_log.emit_runtime`, beside every function that reads them.
"""

from __future__ import annotations

import logging
import threading
import traceback
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import Any, Final

from kiro_crew.constants import crew_log_enabled

#: Named for the facade, so every record the emitter writes carries one logger name,
#: ``kiro_crew.crew_log.emit``, which tests and operator log filters match on.
logger = logging.getLogger("kiro_crew.crew_log.emit")

_KIND = "session"

#: Facts read off the ACP stream vs. facts the gateway decided by itself.
_SRC_ACP = "acp"

_SRC_GATEWAY = "gateway"


#: Bounded so a long-lived gateway cannot grow any map without limit.
_MAX_OPEN_CREW_LOGS = 128


#: Guards this module's own maps below. The writer and the turn tracker each hold their
#: own lock; when this one is held while calling the tracker (its ``trim`` and
#: ``pop_unless_live``), the order is always this lock first, and nothing the tracker
#: does reaches back for it. The writer's adapters (``_handle``, ``_notify_growth``,
#: ``_writer_pool``) DO take this lock, and an inline submit runs them on the caller's
#: thread, so the writer is never called while this lock is held.
_lock = threading.Lock()

#: session -> its open crew log handle, bounded by :data:`_MAX_OPEN_CREW_LOGS` under the
#: tracker's never-evict-a-live-turn rule. This cache IS the writer's ``open_unit``
#: adapter (see :func:`_handle`).
_open: "OrderedDict[str, Any]" = OrderedDict()

#: Sessions whose crew log CREATION failed permanently -- the file-creating record
#: was refused or spent its attempt budget, so no crew log file exists and no later
#: append for the session can ever land. Distinct from a session that legitimately
#: has none (feature off, never opened): that stays a silent policy no-op, this
#: makes later discards COUNT as loss instead of returning a silent None. Cleared
#: when the session closes and by ``reset_caches``; a permanently failed creation does
#: not recover on its own.
_creation_failed: "set[str]" = set()


#: session -> the last request configuration written for it, as a comparable
#: tuple. ``request/configured`` is a CHANGE record: rewriting an identical
#: configuration every turn would bury the turns where it actually moved, which
#: is the only thing a reader wants from it. Keyed per session, not per turn,
#: because the configuration outlives a turn.
_last_config: "OrderedDict[str, tuple[Any, ...]]" = OrderedDict()

#: session -> the last class this process stated for it, as ``(memory, app,
#: channel)``. ``session/class`` is a CHANGE record for the same reason
#: ``request/configured`` is: the class holds still for the whole life of most
#: sessions, and restating it every turn would bury the turn where it moved.
#:
#: A MISSING entry is treated as a change rather than as agreement, so the bound
#: below can cost a redundant line but never a missed restriction. That direction
#: is deliberate: the fold that reads these entries takes the most restrictive
#: value each member ever held, so a duplicate changes no verdict, while a
#: dropped transition would silently widen who may read the log.
_last_class: "OrderedDict[str, tuple[str, str, bool, str]]" = OrderedDict()

#: dispatched child id -> (parent session id, the parent turn that asked, opened,
#: that turn's attempt or 0 when unobserved).
#: A child's spawn entry, steer and terminal outcome are all produced after the
#: asking turn has ended, so the ordinal cannot be re-derived when they land; it is
#: captured at the dispatch and read back here. ``opened`` says whether the run
#: actually STARTED: a spawn is pinned when accepted and promoted only at the site
#: that begins the run, so a spawn declined at the approval gate closes nothing.
#: NOT trimmed by turn liveness like the maps above -- an entry deliberately
#: OUTLIVES the turn that created it, which is the whole reason it exists -- so it
#: is bounded FIFO and released by the child's own terminal entry.
_child_origin: "OrderedDict[str, tuple[str, int, bool, int]]" = OrderedDict()

#: Superseded crew logs whose tail THIS process has repaired, oldest first, so a
#: predecessor walk (:func:`_walk_to_predecessor`) stops at one rather than walking
#: the slot's whole history again on every supersede. Membership is only a
#: shortcut: a log in here was repaired and its own walk went on from it, and a
#: superseded log is never resumed, so nothing reopens a tail this process closed.
#: Bounded FIFO by :data:`_MAX_REPAIR_WALKED`, and an evicted id costs one more
#: whole-file scan of a tail already closed, never a wrong outcome. Cleared by
#: ``reset_caches``, because a new process has repaired nothing yet.
_repair_walked: "OrderedDict[str, None]" = OrderedDict()


#: Consumers to wake when a session's log grows. Registered by
#: :func:`add_growth_listener`, held here rather than imported so the writer
#: names no reader. Survives ``reset_caches``: a listener is installed once per
#: process, and each writer generation reports growth through the same list.
_growth_listeners: "list[Callable[[str], None]]" = []

#: Guards installing the collaborators ``emit_runtime`` holds. Its own lock, so a caller already
#: holding :data:`_lock` (the handle cache trimming through the tracker) can install
#: the tracker without self-deadlocking.
_install_lock = threading.Lock()


_subsystem: Any = None


def _crew_log() -> Any:
    """The storage package, imported the first time a call actually needs it.

    This module is reachable from the gateway boot path, and AUTOSDE's
    ``no-new-work-on-gateway-boot-path`` rule asks for an optional subsystem's
    IMPORT to be gated, not merely its calls. So the split is: THIS module is the
    gate -- pure glue, no import-time work, no shutdown hook registered until a
    write happens -- and the package it fronts (the store, the schema and the
    lease) stays unloaded until one of the entry points below reaches storage. A
    launch with ``KIROCREW_CREW_LOG`` switched off never imports it, because
    ``enabled()`` refuses before any of those paths is taken.
    """
    global _subsystem
    if _subsystem is None:
        from kiro_crew import crew_log

        _subsystem = crew_log
    return _subsystem


def enabled() -> bool:
    """True when the emitter should write. Read per call, never cached."""
    return crew_log_enabled()


def session_id_of(client: Any) -> str:
    """The ACP session id behind a session handle, or ``""`` when it has none.

    A provider exposes it as ``session_id`` and the inner client as
    ``_session_id``; a turn that failed before ``session/new`` has neither, and
    an empty id makes every call in this module a no-op.
    """
    for candidate in (client, getattr(client, "client", None)):
        if candidate is None:
            continue
        for attr in ("session_id", "_session_id"):
            value = getattr(candidate, attr, "")
            if isinstance(value, str) and value:
                return value
    return ""


#: The one entry type whose payload names a BOARD other than its unit's own slot. A
#: worker's report carries the conductor's ``slot``, so that is the fold it belongs to.
#: Spelled here rather than imported from ``entry_types``: this module is the boot-path
#: import gate (see ``_crew_log``), and one string is cheaper than pulling the vocabulary
#: in. ``test_the_real_append_path_wakes_the_eager_fold`` drives this path end to end.
_WORK_TYPE: Final[str] = "work/recorded"


def _note_eager(
    entry: Any,
    entry_type: str,
    session_id: str,
    data: Mapping[str, Any],
    *,
    board: "str | None" = None,
) -> None:
    """Tell the eager folder one entry of *entry_type* committed. Never raises.

    Called from inside the append job, immediately after the append returned -- the same
    place the causal-order publish goes, and for the same reason: until the entry is
    really on disk there is nothing to fold, and a fold run before it would have to be
    run again.

    The whole call is one ``put_nowait`` behind a set membership test
    (:func:`kiro_crew.crew_log.eager.note_commit`). It does not resolve the slot, fold
    anything or build a frame: this runs on the writer thread that every append of this
    session is serialized through, so work done here is latency for the next entry.

    The BOARD is read from *data* here rather than at each call site, so the rule lives in
    one place. Only ``work/recorded`` carries a board of its own: a worker's report names
    the CONDUCTOR's slot, which is not what the worker unit's header says, so folding by
    the header would advance the worker's board and leave the conductor's -- the one a
    dashboard reads -- stale. Every other type has no board field and the header is right
    for it, which is what an empty value asks the folder to use.

    The import is function-local, which is this module's standing rule for anything that
    reaches the fold surface -- a launch that never commits an eager entry never loads
    it.
    """
    seq = int(getattr(entry, "seq", 0) or 0)
    if seq <= 0:
        return
    try:
        # boot-path import gate, the same one ``_crew_log`` above documents: this module is
        # reachable from the gateway's boot path and the fold surface is not, so the import
        # is paid by the first process that actually commits an eager entry.
        from kiro_crew.crew_log import eager

        if board is None:
            board = str(data.get("slot") or "") if entry_type == _WORK_TYPE else ""
        eager.note_commit(session_id, entry_type, seq, board)
    except Exception:  # pragma: no cover - a cache must not cost a committed entry
        # Rendered text, never ``exc_info``: this runs inside the append job, whose frame
        # binds the live ``CrewLog`` whose finalizer releases the write lease, so a record
        # carrying the traceback would keep that handle and its lease alive past the drop
        # that should have released it. The store's ``log_exception_text`` does exactly
        # this, but this module is the boot-path import gate (see ``_crew_log``) and may
        # not import the store at module level, so the render uses the ``traceback``
        # module already imported above. Pinned by test_crew_log_exc_info_sites.py.
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "crew log eager wake not delivered for %s:\n%s",
                entry_type,
                traceback.format_exc().rstrip(),
            )
    # The board above this unit hears about the same commit here, for the same reason
    # the eager wake does: the entry is on disk and nothing has read it yet. A charge or
    # a sub-board record is copied into the parent board's own log, which is the only
    # log that board's folds read (``work_relay``). Its own membership test returns at
    # once for every other type.
    from kiro_crew.crew_log import work_relay

    work_relay.relay(entry, entry_type, session_id, data)
