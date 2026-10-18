"""How the shared channel turn pipeline replays an abandoned turn and charges a failed one.

The Stop and ``/new`` snapshots a replay is checked against
(:func:`session_stop_generation`, :func:`session_conversation_generation`), the replay gap that
orders it ahead of newer messages for the key (:func:`await_replay_gap`), and the circuit
breaker accounting that exempts a shared runtime's death (:func:`charge_turn_failure`) and
clears it on a delivered turn. Callers import these names from
:mod:`kiro_crew.messaging.dispatch`.
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew import runtime_death
from kiro_crew.agent_sdk.drivers.acp_vocab import is_runtime_death
from kiro_crew.messaging.link import canonical_key, split_dm_session_key

logger = logging.getLogger("kiro_crew.messaging.dispatch")


def session_stop_generation(sessions: Any, session_key: str) -> int:
    """The session manager's user-Stop count for *session_key*, read defensively.

    ``SessionManager.stop_turn`` and ``note_stop`` bump it before anything is
    awaited, on every surface that can stop the session. A turn snapshots it
    when it acquires its session and treats any later change as a user Stop --
    the same reading the dashboard runner takes. Doubles for ``sessions`` may
    lack the method or answer with a non-int; both read as 0, so a stand-in
    predating the counter never turns a missing attribute into a stopped turn.
    """
    reader = getattr(sessions, "stop_generation", None)
    if not callable(reader):
        return 0
    try:
        value = reader(session_key)
    except Exception:
        return 0
    return value if isinstance(value, int) else 0


def session_conversation_generation(sessions: Any, session_key: str) -> int:
    """The highest generation persisted for *session_key*'s conversation bucket.

    ``/new`` on every channel of this pipeline advances the conversation's
    generation and persists it (``reserve_new_generation`` ->
    ``SessionManager.reserve_generation``) BEFORE acknowledging, so a key whose
    bucket has since grown a higher generation is a retired conversation. A turn
    snapshots this when it acquires its session and treats any later increase
    as supersession -- the same reading it takes of the Stop counter. Keys
    without a generation grammar (a Slack thread) and doubles lacking the reader
    both read as 0, so neither can turn into a false supersession.
    """
    parsed = split_dm_session_key(canonical_key(session_key))
    reader = getattr(sessions, "max_generation", None)
    if parsed is None or not callable(reader):
        return 0
    try:
        value = reader(parsed[0])
    except Exception:
        return 0
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


async def await_replay_gap(sessions: Any, session_key: str) -> None:
    """Wait out an open replay gap on *session_key* before writing its transcript.

    ``get_or_create`` waits on the gap for every turn that acquires a session,
    but a hook auto-reply never acquires one: it answers from the context
    builder's hooks and persists the exchange straight away. Arriving while an
    older message on the same key sits between its reset and its replay, that
    persist would land in the transcript AHEAD of the replayed turn -- the
    reader saw the older message first, the record would say otherwise. So the
    hook paths wait here, right before their persist, the same way the
    allocation path waits before its claim. The gap owner's own task passes
    straight through, and a session stand-in without the method (the focused
    doubles across the suite) waits for nothing.
    """
    waiter = getattr(sessions, "await_replay_gap", None)
    if callable(waiter):
        await waiter(session_key)


def _set_replay_gap(sessions: Any, session_key: str, *, opened: bool) -> None:
    """Open or close the manager's replay gap for *session_key*, if it has one.

    While the gap is open a Stop that finds no live session is still recorded,
    and any OTHER task's ``get_or_create`` for the key waits -- so a newer
    message arriving between the reset and the replay's reacquire claims the
    successor after the replay, not ahead of it (``SessionManager.open_replay_gap``).
    The pipeline opens it before the first reset that precedes a replay and
    closes it only when the whole turn has settled and its permit is released:
    a waiter admitted earlier would park on the successor's semaphore, and a
    further retry's reset would pop that session from under it, stranding the
    message for good. Probed with ``getattr`` for the same reason as the reader
    above.
    """
    method = getattr(sessions, "open_replay_gap" if opened else "close_replay_gap", None)
    if callable(method):
        try:
            method(session_key)
        except Exception:
            logger.debug(
                "replay gap %s failed for %s",
                "open" if opened else "close",
                session_key,
                exc_info=True,
            )


def _breaker_threshold(sessions: Any) -> int | None:
    """The circuit breaker's OWN trip threshold, read from the manager applying it.

    Deliberately not a literal here. The number is handed to the allocation layer
    through ``AllocationConstants``, so a copy in this module would be a second
    value to keep in step with the counter the bound below stands in for -- and
    importing the one definition is not available either, because this module
    stays off the session package's import graph (see the ``SessionClosingError``
    import in ``dispatch.py``). Reading it from the manager is therefore the only way
    to be sure the substitute bound and the real counter share a limit.

    ``None`` when it cannot be read, and the caller then charges exactly as it
    does today: an exemption whose bound is unknown is not an exemption.
    """
    build = getattr(sessions, "_allocation_deps", None)
    if not callable(build):
        return None
    try:
        threshold = build().constants.circuit_breaker_threshold
    except Exception:
        return None
    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold <= 0:
        return None
    return threshold


async def charge_turn_failure(
    sessions: Any,
    session_key: str,
    *,
    exc: BaseException,
    provider: object | None,
    channel_type: str,
) -> None:
    """Charge one failed turn to *session_key*'s breaker, unless a SHARED process died.

    The channel dispatchers catch a failed turn generically, so a dying runtime
    reaches them as one more exception and every tenant of that process charges
    its own breaker for it -- the misattribution :mod:`kiro_crew.runtime_death`
    exists to end, arriving by a path no typed handler covers. One helper rather
    than one copy per channel: four copies of an attribution rule drift, and the
    rule is identical because the counter is.

    Only a process death is ever exempt. Every other failure is the turn's own
    and charges exactly as before -- consulting the death record for an unrelated
    exception would exempt a real fault whenever some co-tenant's death happened
    to be recorded against the same provider.

    *provider* must be the one this turn ACQUIRED, never a fresh lookup. The
    recovery around these handlers replaces a dead session, so a lookup at
    failure time answers for the replacement and the question silently becomes
    "was the NEW runtime shared".

    The exemption is bounded, and at the limit it PERFORMS the actuator rather
    than charging the counter it stood in for. ``record_failure`` trips into this
    same reset, so a session on a permanently dying shared runtime recovers after
    the threshold rather than after twice it, and its own failure count is left
    alone -- it never misbehaved.
    """
    threshold = _breaker_threshold(sessions)
    if (
        threshold is None
        or not is_runtime_death(exc)
        or runtime_death.caused_by_this_session(provider)
    ):
        await sessions.record_failure(session_key)
        return
    streak = runtime_death.note_shared_death(session_key)
    if streak < threshold:
        logger.warning(
            "%s: %s lost a turn to a SHARED runtime's death (%d running) — "
            "not counting it toward the circuit breaker",
            channel_type,
            session_key,
            streak,
        )
        return
    logger.warning(
        "%s: the runtime %s shares has died %d times running — resetting it now, "
        "the same recovery the breaker performs",
        channel_type,
        session_key,
        streak,
    )
    try:
        await sessions.reset(session_key)
        # Same transfer rule as the two typed hand-overs: the reset spends the
        # streak, so it is forgotten here. Left in place it would sit at the
        # threshold forever and every later shared death would reset again --
        # the unexempted behaviour, arrived at by keeping the exemption's own
        # bookkeeping. Cleared only after the reset returns, so a failed reset
        # keeps the streak and the next death retries the actuator.
        runtime_death.clear_shared_deaths(session_key)
    except Exception:
        logger.warning(
            "%s: reset of %s after a shared runtime's deaths failed",
            channel_type,
            session_key,
            exc_info=True,
        )


def _record_success(sessions: Any, session_key: str, channel: str) -> None:
    """Count a delivered turn a success, guarded so a failure cannot re-charge it."""
    try:
        sessions.record_success(session_key)
        # Beside the counter it stands in for. The shared-death streak is a
        # reading of whether this session can get work done at all, so a
        # landed turn clears it exactly as it clears the consecutive-failure
        # count -- left uncleared it would be a lifetime total, and the bound
        # it feeds would stay permanently tripped while reporting the total as
        # a consecutive run.
        runtime_death.clear_shared_deaths(session_key)
    except Exception:
        logger.warning("%s: record_success failed session=%s", channel, session_key, exc_info=True)
