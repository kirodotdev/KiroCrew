"""A Stop declined while the session's own compaction holds it, and the escalation it arms.

``compaction_in_flight`` is the one probe every channel's stop path runs before its side
effects, and ``stop_turn`` reads the same answer, so the probe and the decline cannot
disagree. ``decline_stop`` tells the presser first and arms their marker only once the
send reports delivery; ``consume_stop_declined`` turns that presser's repeat inside
``STOP_DECLINED_ESCALATION_SECS`` into the forcing press, and ``clear_stop_declined``
drops a key's markers when its compaction ends. The markers are one process-wide record,
keyed by session and presser and bounded in count and key length. A new rule about when
a Stop is declined, or what a declined Stop arms, goes here.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any, Literal

#: ``compacting`` is the one outcome that changed nothing: a cooperative Stop
#: arrived while the session's own ``/compact`` turn held it, and cancelling that
#: turn would fail the compaction and recycle the session. The
#: caller tells the user, and the compaction finishes or times out on its own.
#: A ``force`` stop is never answered this way -- it is the user's escape hatch.
StopOutcome = Literal["soft", "hard", "idle", "compacting"]


#: What a channel says for the ``compacting`` outcome. One string, so every
#: surface that declines the Stop declines it in the same words.
STOP_DECLINED_COMPACTING_TEXT = (
    "⏳ Compacting context — nothing was stopped. The compaction finishes on its own; "
    "a message sent now runs once it has. Send stop again within a minute to force it, "
    "which restarts the session (the agent keeps only a recent excerpt of the conversation)."
)


#: How long after a declined stop a repeat counts as the second press and forces.
#: ONE constant for every surface: the dashboard's escalation gate
#: (``slot_projection.stop_declined_armed``) imports it from ``session_lifecycle``, and the
#: composer's "click again" hint mirrors the same number.
STOP_DECLINED_ESCALATION_SECS = 60.0


#: Count bound on live decline markers. A marker lives for one window and is
#: swept on every write, so the bound is only reached by that many DISTINCT
#: (session, presser) pairs declined inside one minute. Past it the oldest marker
#: is dropped and the drop is counted and logged: that presser's repeat is
#: declined once more rather than forcing, which is the safe side.
STOP_DECLINED_MARKERS_MAX = 256
#: Retained-string bound on each half of a marker's key. Both halves are
#: externally derived (a session key from a route, a presser from a sender id).
#: A longer one is refused at the point of retention, not truncated: a truncated
#: key could collide two pressers, which is the sharing this keying exists to
#: prevent. The refusal is counted and logged like an overflow.
STOP_DECLINED_KEY_MAX_CHARS = 512


# The facade's logger: records keep the name ``kiro_crew.session_lifecycle``.
_logger = logging.getLogger("kiro_crew.session_lifecycle")


#: Live decline markers, oldest first: ``(session key, presser)`` -> monotonic time
#: of the decline. Keyed by the PRESSER as well as the session because one session
#: key is shared by every member of a group route and, under
#: ``dm_scope = "unified"``, by every allow-listed person's DM: a marker keyed by
#: session alone lets one person's declined Stop arm the next person's press as
#: the FORCE that resets the session under both of them. Module level so every
#: channel dispatcher shares one record through the two functions below, exactly
#: as they share the probe.
_stop_declined_markers: OrderedDict[tuple[str, str], float] = OrderedDict()
#: Markers refused or evicted since import, so the overflow is said out loud.
_stop_declined_dropped = 0


def _stop_declined_key(key: str, presser: str) -> tuple[str, str] | None:
    """The marker key for *key* pressed by *presser*, or None past the string bound."""
    if len(key) > STOP_DECLINED_KEY_MAX_CHARS or len(presser) > STOP_DECLINED_KEY_MAX_CHARS:
        return None
    return (key, presser)


def _count_stop_declined_drop(reason: str, key: str, presser: str) -> None:
    global _stop_declined_dropped
    _stop_declined_dropped += 1
    _logger.warning(
        "stop-declined marker %s (session=%.64s presser=%.64s); %d dropped so far, "
        "cap %d markers / %d chars per key",
        reason,
        key,
        presser,
        _stop_declined_dropped,
        STOP_DECLINED_MARKERS_MAX,
        STOP_DECLINED_KEY_MAX_CHARS,
    )


def note_stop_declined(key: str, presser: str, *, now: float | None = None) -> None:
    """Record that *presser*'s stop on *key* was just declined for compaction.

    *presser* is REQUIRED, with no default, for the reason ``stop_running_turn``'s
    ``owner`` is: a channel wired up later cannot inherit a session-wide marker by
    leaving it out. A channel that genuinely cannot name its presser passes ``""``,
    which arms a marker only that same anonymous route can consume.
    """
    current = time.monotonic() if now is None else now
    marker = _stop_declined_key(key, presser)
    if marker is None:
        _count_stop_declined_drop("refused: key past the string bound", key, presser)
        return
    for stale in [
        m
        for m, at in _stop_declined_markers.items()
        if current - at >= STOP_DECLINED_ESCALATION_SECS
    ]:
        del _stop_declined_markers[stale]
    _stop_declined_markers.pop(marker, None)
    _stop_declined_markers[marker] = current
    while len(_stop_declined_markers) > STOP_DECLINED_MARKERS_MAX:
        evicted, _ = _stop_declined_markers.popitem(last=False)
        _count_stop_declined_drop("evicted: past the count bound", *evicted)


async def decline_stop(
    key: str, presser: str, deliver: Callable[[], Awaitable[bool]], *, now: float | None = None
) -> bool:
    """Tell *presser* their stop was declined, and only then arm their escalation.

    The marker this writes is what turns the SAME presser's next press inside
    the window into a hard reset that pops the session and trims the
    conversation to an excerpt. The only thing that makes that press informed is
    the sentence *deliver* carries, so the send goes first and the marker is
    written after it returns. A transport that could not reach the presser
    therefore arms nothing, and their retry -- which a missing acknowledgement
    makes MORE likely, not less, since a person who saw no answer presses again
    within seconds -- is a first press again and is declined again.

    *deliver* must report delivery EXPLICITLY: only ``True`` arms the marker.
    Nothing else does, ``None`` included, because that is the value a transport
    whose send swallows its own error returns when the send did not land -- and
    reading an unknown value as success is the one mistake here that costs the
    user their conversation. A send that raises propagates exactly as the bare
    send did. Returns whether the escalation is armed.
    """
    if await deliver() is not True:
        return False
    note_stop_declined(key, presser, now=now)
    return True


def clear_stop_declined(key: str, *, fold: Callable[[str], str] | None = None) -> int:
    """Drop every decline marker on *key*; returns how many.

    Called when the compaction that declined them ENDS (``_set_compacting`` off):
    a marker is a memory of one refusal, and the next compaction on the key,
    even one starting inside the window, owes its own first refusal. Without
    this a person declined on compaction A could hard-reset compaction B with
    what reads to them as a first press. *fold* is the manager's key fold: a
    channel presses with its own spelling of the key, which the probe folds onto
    the live one, so a marker is this compaction's when its key folds to *key*.
    """
    same = (lambda k: k == key) if fold is None else (lambda k: k == key or fold(k) == key)
    stale = [m for m in _stop_declined_markers if same(m[0])]
    for m in stale:
        del _stop_declined_markers[m]
    return len(stale)


def consume_stop_declined(key: str, presser: str, *, now: float | None = None) -> bool:
    """Whether *presser*'s fresh decline on *key* arms THIS stop as the forcing press.

    Only the presser who was declined can spend the marker. Consumes it either
    way: a stale one is dropped, a fresh one is spent by the press it armed.
    """
    marker = _stop_declined_key(key, presser)
    if marker is None:
        return False
    at = _stop_declined_markers.pop(marker, None)
    if at is None:
        return False
    current = time.monotonic() if now is None else now
    return current - at < STOP_DECLINED_ESCALATION_SECS


def compaction_in_flight(sessions: Any, key: str) -> bool:
    """Whether an automatic compaction holds the session a Stop would cancel.

    The ONE probe a stop path runs BEFORE its side effects (the Stop record, a
    queue clear, a pending-file unlink), because ``stop_turn`` declining after
    them leaves a Stop that ended nothing having destroyed queued work. Callers:
    the dashboard stop and interrupt routes, Slack (events, handler,
    interactions), the shared ``messaging.commands.stop_running_turn`` (Discord,
    Telegram, Teams), the Webex, WeCom and Weixin dispatchers, WhatsApp, and Spec
    Builder's Pause. Fail-soft
    to False and ``is True`` rather than truthiness: a manager double without the
    method, or one answering a truthy Mock, keeps the ordinary stop path.

    A compaction that is still waiting for the turn permit does not hold the
    session yet: the turn holding the permit does, and the Stop is aimed at it
    (``_compaction_waits_for_turn``). ``stop_turn`` reads the same answer, so the
    probe and the decline cannot disagree.
    """
    probe = getattr(sessions, "is_compacting", None)
    if not callable(probe):
        return False
    try:
        return probe(key) is True and not _compaction_waits_for_turn(sessions, key)
    except Exception:
        return False


def _compaction_waits_for_turn(sessions: Any, key: str) -> bool:
    """Whether *key*'s compaction still waits for the turn that holds the session.

    Narrows the Stop decline only: anything but an explicit True (a double
    without the method, a truthy Mock, a raise) answers False, which keeps it.
    """
    probe = getattr(sessions, "compaction_waits_for_turn", None)
    if not callable(probe):
        return False
    try:
        return probe(key) is True
    except Exception:
        return False
