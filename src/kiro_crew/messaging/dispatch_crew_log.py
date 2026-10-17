"""Opening a channel conversation's crew log ahead of its turn.

:func:`open_turn_crew_log` writes the ``session/opened`` entry the dashboard runner writes for
its own turns, from the facts the shared channel turn pipeline
(:mod:`kiro_crew.messaging.dispatch`) can establish: the predecessor and the requested model
the allocation captured (:func:`predecessor_sid`, :func:`requested_model_sid`) and the
workspace the dashboard states for the conversation's slot (:func:`slot_workspace`). Callers
import these names from :mod:`kiro_crew.messaging.dispatch`.
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew.history import transcript_stem

logger = logging.getLogger("kiro_crew.messaging.dispatch")


def predecessor_sid(sessions: Any, session_key: str) -> str:
    """The crew log *session_key*'s live session superseded -- read AFTER the allocation.

    The ``previous_sid`` producer for :func:`open_turn_crew_log`. It does not read
    the slot-to-session mapping at all: it returns what the allocation boundary
    captured for the key (``SessionManager.allocation_predecessor``), which the
    boundary reads under its own lock, in the same tick that registers a
    cold-started session and before that session's id is mapped. No read taken
    around ``get_or_create`` can stand in for that: a caller reading the mapping
    before its call can be suspended INSIDE the allocation, waiting for the turn
    permit, while a concurrent turn on the same key allocates an intermediate
    session and has it recycled by a failed compaction -- the caller's value then
    names the store before that intermediate one, its successor cites its
    grandparent, and the intermediate log falls off the succession chain. Read
    after the call, the mapping already names the successor itself. The
    boundary's capture is the only read that is neither too early nor too late,
    so this is consumed right after ``get_or_create`` returns, while this turn
    holds the key's permit.

    The emitter does the comparing: a warm claim hands back the value its live
    session was registered with and the log already exists, so nothing is
    written; only the creation of a cold successor's log cites its predecessor,
    and only after the emitter has checked that predecessor's header names the
    same slot. Best-effort: a store without the reader answers ``""``, which the
    emitter reads as "nothing to follow".
    """
    reader = getattr(sessions, "allocation_predecessor", None)
    if not callable(reader):
        return ""
    try:
        return str(reader(session_key) or "")
    except Exception:
        logger.debug("crew log: predecessor unreadable for %s", session_key, exc_info=True)
        return ""


def requested_model_sid(sessions: Any, session_key: str) -> str:
    """The model *session_key*'s live allocation SELECTED, or ``""`` -- read after the claim.

    The ``model_requested`` half of the requested/served pair the ``session/opened``
    entry records. The dispatcher's own choice is not the whole story: a call handed
    ``model=None`` has the allocation resolve an id from config itself, and that
    resolution is invisible in ``get_or_create``'s return, so only the stamp the
    allocation left on the session (``SessionManager.allocation_requested_model``,
    the value the provider was constructed with) can say what was asked for. The
    dashboard runner records the same stamp; without it a channel session's log
    would carry the served model alone and lose the selected side of the pair for
    good, the entry being append-only. Best-effort like :func:`predecessor_sid`: a
    store without the reader answers ``""``, which the emitter records as "no
    selection to report".
    """
    reader = getattr(sessions, "allocation_requested_model", None)
    if not callable(reader):
        return ""
    try:
        return str(reader(session_key) or "")
    except Exception:
        logger.debug("crew log: requested model unreadable for %s", session_key, exc_info=True)
        return ""


def slot_workspace(dashboard_state: Any, session_key: str) -> str:
    """The workspace the dashboard states for *session_key*'s conversation, or ``""``.

    The ``workspace`` producer for :func:`open_turn_crew_log`, and the SAME source
    the dashboard runner's writer reads: ``chat_runner._crew_log_workspace`` states
    ``slot.workspace`` off the live slot, and a channel conversation's slot is the
    one the dashboard surfaces it under -- ``channel_slot_name(session_key)``, the
    channel key folded to the filename charset, which is the ``slot`` field this
    opener already records (:func:`transcript_stem` spells the same fold). A tab
    opened on that conversation writes its ``session/opened`` from that slot, into
    the same crew log this dispatcher writes, and the emitter appends a
    ``session/class`` line whenever the class it is handed differs from the last
    one stated -- so if the two writers named different workspaces for one
    session, every switch between them would record a move that never happened.
    Reading the slot the other writer reads is what makes the two statements one.

    ``""`` when the conversation has no slot yet -- a channel slot is surfaced
    after its first persisted turn, so the log's opening entry states no workspace
    -- or when the gateway state is not attached. An unstated workspace is "not
    observed": the class fold holds the first workspace STATED and records a later
    different one as a move, so nothing is guessed here for the slot to contradict.
    Only a ``str`` counts as a statement, so a state double answering with an
    object of another shape states nothing rather than its ``repr``.
    """
    if dashboard_state is None:
        return ""
    try:
        getter = getattr(dashboard_state, "get_slot", None)
        slot = getter(transcript_stem(session_key)) if callable(getter) else None
    except Exception:
        logger.debug("crew log: slot unreadable for %s", session_key, exc_info=True)
        return ""
    workspace = getattr(slot, "workspace", "") if slot is not None else ""
    return workspace if isinstance(workspace, str) else ""


def open_turn_crew_log(
    provider: Any,
    *,
    session_key: str,
    agent: str,
    resumed: bool,
    ctx_builder: Any = None,
    previous_sid: str = "",
    model_requested: str = "",
    workspace: str = "",
) -> None:
    """Open the channel session's crew log ahead of its turn, as the dashboard runner does.

    ``crew_log_emit.on_session_opened`` is what CREATES a session's crew log, keyed
    by its ACP session id; ``chat_runner._run_chat`` calls it on every dashboard turn
    once the handle exists, and a warm reuse is silent. A channel conversation is
    not a dashboard turn, and without this call it opens no log at all.
    That is a hole the work ledger falls into: the ledger is a projection of the
    crew log, every ``work_ledger_record`` / ``work_report`` write appends one
    ``work/recorded`` entry to the ACTING session's log, and a write with nowhere
    to append is rolled back and refused (``crew_log_unrecorded``). An owner DM
    that session control admits as a conductor therefore reached the ledger and
    lost every write to it. Opening the log here, before ``TurnDriver.run``, is
    what makes that admission usable. Free while the emitter is off -- the emitter
    checks its own flag -- and it never raises, because the turn must not be lost
    to its own record.

    Only facts the dispatcher can establish are recorded; the emitter reads an
    absent field as "not observed", never as false. The ACP session id comes off
    *provider* (no id, no log: a turn that never got a session emits nothing, as
    on the dashboard). ``slot`` is the key the dashboard surfaces this conversation
    under -- the channel key folded to the filename charset, which is what
    ``channel_slot_name`` spells and what ``session_create`` stamps as
    ``_created_by`` on the workers this session dispatches, so the session tree
    joins the two. The served model and the cwd are read off the provider, the
    dashboard's own sources for them, and ``model_requested`` is the allocation's
    stamp of what was SELECTED (:func:`requested_model_sid`, read after the claim
    like the predecessor) -- the pair the entry records, since a call handed
    ``model=None`` has the allocation resolve the id itself and nothing else can
    say what it chose; ``resumed`` is ``get_or_create``'s answer.
    The class is stated only when the memory mode is known, from the gateway's
    live policy for the key (``ctx_builder.live_memory_mode_for_session``, wired by
    the dashboard state; a builder without it states no class, which readers
    refuse rather than assume), and it carries ``channel=True`` because a
    channel-born conversation is published to its channel by definition -- the
    same reading ``_crew_log_class`` takes off a linked slot. ``workspace`` is
    stated the way the dashboard writer states it, off the slot the dashboard
    surfaces this conversation under (:func:`slot_workspace`), because a tab on
    the conversation writes into this same log and the emitter records a
    ``session/class`` move whenever two statements of one session's class differ:
    every member the dashboard states, this opener states from the same source, or
    the two writers would take turns recording a move that never happened. No
    ``parent``: a
    conversation the person opened themselves is nobody's child. ``previous_sid``
    is the crew log this conversation's live session superseded, as the
    allocation boundary captured it while registering that session
    (:func:`predecessor_sid`, consumed by the dispatcher right after
    ``get_or_create`` returns): the emitter writes the ``previous`` edge only when
    it creates a log that names a different store, which is what keeps a
    conversation's history reachable across the cold successor a failed
    compaction leaves behind.

    For the channel's OWN sessions only. A dashboard session resumed into the chat
    (``!sessions``) is opened by the dashboard runner, which alone holds its
    lineage: an opener from here would create that log without its ``parent``.
    """
    # Imported here, not at module scope, on purpose: this module is on the
    # dashboard's boot path (``dashboard.handlers.crew_log`` reaches it through the
    # handlers package -> ``handlers.taskrunner`` -> ``taskrunner`` ->
    # ``task_executor``), and the crew log is optional -- a flag-off launch must
    # not load the storage package.
    # ``test_crew_log_routes.py::test_this_module_does_not_load_the_storage_package_at_import``
    # pins that from a clean interpreter and fails when this moves up; it is not a
    # circular import. ``handlers/crew_log.py`` and
    # ``work_ledger.rebuild_from_projection`` import the emitter the same way. The
    # ``top-level-imports`` convention is advisory; this boot-path invariant is
    # enforced, so the invariant wins.
    from kiro_crew.crew_log import emit as crew_log_emit

    try:
        session_id = crew_log_emit.session_id_of(provider)
        if not session_id:
            return
        memory_mode = ""
        live_mode = getattr(ctx_builder, "live_memory_mode_for_session", None)
        if callable(live_mode):
            try:
                memory_mode = str(live_mode(session_key) or "")
            except Exception:
                logger.debug("crew log: memory mode unreadable for %s", session_key, exc_info=True)
        crew_log_emit.on_session_opened(
            session_id,
            agent=agent or "",
            slot=transcript_stem(session_key),
            model=str(getattr(provider, "served_model", "") or ""),
            model_requested=model_requested,
            cwd=str(getattr(provider, "cwd", "") or ""),
            resumed=bool(resumed),
            memory=memory_mode,
            channel=True,
            workspace=workspace,
            previous_sid=previous_sid,
        )
    except Exception:
        logger.debug("crew log: opener skipped for %s", session_key, exc_info=True)
