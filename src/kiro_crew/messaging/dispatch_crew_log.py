"""Opening a channel conversation's crew log ahead of its turn.

:func:`open_turn_crew_log` writes the ``session/opened`` entry the dashboard runner writes for
its own turns, from the facts the shared channel turn pipeline
(:mod:`kiro_crew.messaging.dispatch`) can establish: the predecessor the allocation captured
or the store holds (the opener's own :func:`predecessor_edge`, over :func:`predecessor_sid`
and the caller's :func:`store_predecessor`), the requested model the allocation captured
(:func:`requested_model_sid`) and the workspace the dashboard states for the conversation's
slot (:func:`slot_workspace`). Callers import these names from
:mod:`kiro_crew.messaging.dispatch`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kiro_crew.history import transcript_stem

logger = logging.getLogger("kiro_crew.messaging.dispatch")


def predecessor_sid(sessions: Any, session_key: str) -> str:
    """The crew log *session_key*'s live session superseded -- read AFTER the allocation.

    The ``previous_sid`` producer for :func:`open_turn_crew_log`. It returns what the
    allocation boundary captured under its own lock
    (``SessionManager.allocation_predecessor``), not the slot mapping: a read before
    ``get_or_create`` can be suspended inside the allocation while a concurrent turn
    allocates and recycles an intermediate session, dropping that log from the
    succession chain; a read after it already names the successor. Consume it right
    after ``get_or_create`` returns, while this turn holds the key's permit.

    The emitter cites the predecessor only when it creates a cold successor's log
    for the same slot. A store without the reader answers ``""`` ("nothing to
    follow").
    """
    reader = getattr(sessions, "allocation_predecessor", None)
    if not callable(reader):
        return ""
    try:
        return str(reader(session_key) or "")
    except Exception:
        logger.debug("crew log: predecessor unreadable for %s", session_key, exc_info=True)
        return ""


async def store_predecessor(sessions: Any, session_key: str) -> tuple[str, bool, bool] | None:
    """The slot's newest crew log IN THE STORE -- read BEFORE the allocation, off the loop.

    The durable tier behind :func:`predecessor_sid`, for :func:`predecessor_edge`:
    ``crew_log.emit.slot_previous_store`` reads the units on disk, so a gateway that
    restarted gets the answer the process before it held. Read before the claim, because
    the read blocks and the opener must not suspend, and only when no session is
    registered for the key: a warm turn would otherwise pay a store listing every time. A
    successor started behind a registered session (a recycle, a dead provider) keeps the
    capture alone. ``None`` means not read (a registered session, the crew log off); a
    read that failed is undecided.
    """
    try:
        if sessions.has_session(session_key):
            return None
    except Exception:
        return None
    from kiro_crew.crew_log import emit as crew_log_emit

    if not crew_log_emit.enabled():
        return None
    try:
        return await asyncio.to_thread(
            crew_log_emit.slot_previous_store, transcript_stem(session_key)
        )
    except Exception:
        logger.debug("crew log: slot store unreadable for %s", session_key, exc_info=True)
        return ("", False, False)


def predecessor_edge(
    sessions: Any, session_key: str, store: tuple[str, bool, bool] | None
) -> tuple[str, bool | None]:
    """``(previous_sid, previous_undecided)`` for :func:`open_turn_crew_log`, after the claim.

    The tiers the dashboard runner reads (``_slot_predecessor_store``), with the
    allocation's capture ahead of the store: it alone can name a store whose create is
    still queued to the writer. The capture is the mapping as registration read it,
    though, and while the live session owes a history replay the mapping knowingly holds
    the generation before -- asked HERE, where that session exists -- so the store decides
    then. A named store is cited; a store holding no unit of the slot states there is none
    (``False``); anything that cannot be named is recorded as undetermined (``True``)
    rather than written as an absence; ``None`` determines nothing, which writes no key.
    """
    capture = predecessor_sid(sessions, session_key)
    if store is None or (capture and not _replay_pending(sessions, session_key)):
        return capture, (False if capture else None)
    sid, decided, complete = store
    if sid:
        return sid, False
    if not decided or capture:
        # Unreadable, or a mapped id inside the replay window the store cannot confirm:
        # a predecessor exists and cannot be named, so the entry records a break.
        return "", True
    return "", (False if complete else None)


def _replay_pending(sessions: Any, session_key: str) -> bool:
    """Whether *session_key*'s live session still owes its history replay. Never raises."""
    reader = getattr(sessions, "provider_switch_replay_pending", None)
    if not callable(reader):
        return False
    try:
        return reader(session_key) is True
    except Exception:
        logger.debug("crew log: replay marker unreadable for %s", session_key, exc_info=True)
        return False


def requested_model_sid(sessions: Any, session_key: str) -> str:
    """The model *session_key*'s live allocation SELECTED, or ``""`` -- read after the claim.

    The ``model_requested`` half of the ``session/opened`` requested/served pair. A
    call handed ``model=None`` has the allocation resolve the id from config, which
    ``get_or_create`` does not return, so only the allocation's stamp
    (``SessionManager.allocation_requested_model``) says what was asked for. The
    entry is append-only, so a missing value is lost for good. A store without the
    reader answers ``""`` ("no selection to report").
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

    The ``workspace`` producer for :func:`open_turn_crew_log`. It reads the same
    source as the dashboard writer (``chat_runner._crew_log_workspace``: the slot's
    ``workspace``, slot name from :func:`transcript_stem`). Both write the same crew
    log, and the emitter records a ``session/class`` move whenever two statements
    differ, so different sources would log moves that never happened.

    ``""`` when the conversation has no slot yet (a channel slot appears after its
    first persisted turn) or no gateway state is attached; an unstated workspace
    means "not observed", so nothing is guessed. Only a ``str`` counts as a value.
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
    sessions: Any = None,
    store: tuple[str, bool, bool] | None = None,
    model_requested: str = "",
    workspace: str = "",
) -> None:
    """Open the channel session's crew log ahead of its turn, as the dashboard runner does.

    ``crew_log_emit.on_session_opened`` CREATES a session's crew log, keyed by its
    ACP session id; the dashboard's ``_turn_admit`` calls it every turn. Without
    this call a channel conversation has no log, and every ``work_ledger_record`` /
    ``work_report`` write from it is rolled back as ``crew_log_unrecorded``. Call it
    before ``TurnDriver.run``. Free while the emitter is off, and it never raises:
    the turn must not be lost to its own record.

    Only facts the dispatcher can establish are recorded; an absent field means
    "not observed". No ACP session id on *provider* means no log. ``slot`` is the
    channel key folded to the filename charset (``channel_slot_name``, also what
    ``session_create`` stamps as ``_created_by``), so the session tree joins them.
    Served model and cwd come off the provider; ``model_requested`` comes from
    :func:`requested_model_sid`; ``resumed`` is ``get_or_create``'s answer. The
    class is stated only when ``ctx_builder.live_memory_mode_for_session`` knows the
    memory mode, with ``channel=True`` (as ``_crew_log_class`` reads a linked slot).
    ``workspace`` must come from :func:`slot_workspace`, the dashboard writer's own
    source, or the two writers record false ``session/class`` moves. No ``parent``
    and ``depth`` from :func:`~kiro_crew.crew_log.session_tree_projection.root_depth`:
    a conversation the person opened is a root. The ``previous`` edge is
    :func:`predecessor_edge` over *sessions* and the *store* answer
    :func:`store_predecessor` read before the claim; the emitter writes it only for a
    new log, which keeps history reachable across a cold successor after a failed
    compaction or a restart.

    For the channel's OWN sessions only. A dashboard session resumed into the chat
    (``!sessions``) is opened by the dashboard runner, which alone holds its
    lineage: an opener from here would create that log without its ``parent``.
    """
    # Function-local on purpose: this module is on the dashboard's boot path, and a
    # flag-off launch must not load the crew-log storage package.
    # ``test_crew_log_routes.py::test_this_module_does_not_load_the_storage_package_at_import``
    # fails if these move to module scope.
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.crew_log import session_tree_projection as crew_log_tree

    try:
        session_id = crew_log_emit.session_id_of(provider)
        if not session_id:
            return
        previous_sid, previous_undecided = predecessor_edge(sessions, session_key, store)
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
            # A root, stated rather than left absent, so channel conversations count
            # in the tree. ``root_depth`` withholds the zero while the fold is
            # unseeded or over its cap, as a child's depth is withheld then.
            depth=crew_log_tree.root_depth(),
            memory=memory_mode,
            channel=True,
            workspace=workspace,
            previous_sid=previous_sid,
            previous_undecided=previous_undecided,
        )
    except Exception:
        logger.debug("crew log: opener skipped for %s", session_key, exc_info=True)
