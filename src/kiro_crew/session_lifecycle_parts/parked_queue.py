"""Queued messages a stop or a reset keeps for the key's successor session.

``force_stop_keeping_others`` detaches a shared key's queue before a forced stop, drops
only the presser's entries and hands the rest on; ``hand_queue_to_successor`` puts kept
entries at the head of the successor's queue and wakes their channels. When no successor
starts, the entries are parked, files kept and bounded per key, in total and per field,
and the next registration under the key adopts them (``adopt_parked_queue``).
``allocation_identity`` is the ``get_or_create`` identity a reset or compaction successor
is rebuilt with. A new way a stop or reset keeps queued work goes here.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from kiro_crew.messaging.queue_drain import entry_channel, wake_other_drains
from kiro_crew.session_lifecycle_parts.stop_decline import STOP_DECLINED_KEY_MAX_CHARS, _logger


async def force_stop_keeping_others(
    sessions: Any, key: str, owned_by: Callable[[dict], bool]
) -> bool:
    """A channel's forced stop on *key* that drops ONLY the presser's queued work.

    The forced stop hard-resets the session, and ``reset`` pops the ``_Session``
    with its queue and unlinks every queued attachment -- ``preserve_queue`` only
    skips ``stop_turn``'s own clear, it cannot save what the pop discards. On a
    key several people share (a group route, ``dm_scope = "unified"``) that is
    other people's unanswered messages. So the queue is DETACHED before the stop
    (``detach_queue``, files kept), the presser's entries are dropped from the
    handles after it (``clear_queue(only=...)`` unlinks their files whether or
    not a session holds the key), and the rest are handed to the successor the
    hard stop respawns: ``get_or_create`` claims that successor (or builds it),
    ``restore_queue`` puts them at its head, ``release`` lets its drain run, and
    ``wake_other_drains`` starts the channels those entries belong to
    (``hand_queue_to_successor``). If no successor starts, the kept entries are
    parked, files intact, for the hard stop's own respawn retry. Returns whether
    the running turn was ended.
    """
    taken = tuple(sessions.detach_queue(key))
    try:
        ended = (await sessions.stop_turn(key, force=True, preserve_queue=True)) == "hard"
    except BaseException:
        # A reset that raised after popping the session leaves no queue for the
        # restore to land on; those entries are then parked for the next start
        # rather than lost in a local that is about to unwind.
        if _has_session(sessions, key):
            sessions.restore_queue(key, taken)
        elif taken:
            _park_queue(sessions, key, taken)
        raise
    mine = tuple(e for e in taken if owned_by(e[2]))
    others = tuple(e for e in taken if not owned_by(e[2]))
    if mine:
        sessions.clear_queue(key, only=mine)
    if others or _parked_for(sessions, key):
        # ``others`` from the detach, plus anything ``stop_turn`` parked from a
        # message admitted after the detach, both go to the successor now.
        await hand_queue_to_successor(sessions, key, others)
    return ended


#: Count bound on queue entries parked for ONE key whose successor could not
#: start, and on the parked entries across ALL keys. Past either the OLDEST
#: parked entry (of that key, or of the whole store) is dropped, its files
#: unlinked, and the drop is counted and logged: parking is a retry buffer for a
#: failed start, not a second queue. The keys are bounded by the same string
#: bound as the decline markers (``STOP_DECLINED_KEY_MAX_CHARS``); a key past it
#: is refused, not truncated, and its entries dropped with their files unlinked.
#: Each entry's own fields are bounded too, at the point of retention: a message
#: id past ``PARKED_ENTRY_TS_MAX_CHARS``, a text past
#: ``PARKED_ENTRY_TEXT_MAX_CHARS`` or kwargs whose repr is past
#: ``PARKED_ENTRY_KWARGS_MAX_CHARS`` refuse the entry (files unlinked, counted,
#: logged) rather than retain an externally sized payload for the store's life.
PARKED_QUEUE_MAX = 64
PARKED_QUEUE_TOTAL_MAX = 256
PARKED_ENTRY_TEXT_MAX_CHARS = 64 * 1024
PARKED_ENTRY_KWARGS_MAX_CHARS = 16 * 1024
#: The entry's message id (a Slack ts, a Telegram message id): a short token on
#: every channel, so the bound is generous and a value past it is a malformed entry.
PARKED_ENTRY_TS_MAX_CHARS = 256


#: Queue entries a forced stop kept but could not hand to a successor because
#: none would start (a provider failing to spawn). Retained rather than dropped:
#: they are other people's unanswered messages and their staged attachments.
#: Oldest key first. Drained onto the key's queue by the next
#: ``hand_queue_to_successor`` for the key, and by the hard stop's own respawn
#: path calling it, so a start that later succeeds delivers them.
_parked_queue: OrderedDict[str, list[Any]] = OrderedDict()
#: Entries refused or evicted since import, so the overflow is said out loud.
_parked_dropped = 0


def _parked_total() -> int:
    return sum(len(v) for v in _parked_queue.values())


def _drop_parked(sessions: Any, key: str, entry: Any, reason: str) -> None:
    global _parked_dropped
    _parked_dropped += 1
    sessions.clear_queue(key, only=(entry,))  # unlinks the handle's files
    _logger.warning(
        "parked queue entry for %.64s dropped (%s); %d dropped so far, caps %d/key %d total",
        key,
        reason,
        _parked_dropped,
        PARKED_QUEUE_MAX,
        PARKED_QUEUE_TOTAL_MAX,
    )


def _has_session(sessions: Any, key: str) -> bool:
    probe = getattr(sessions, "has_session", None)
    try:
        return bool(probe(key)) if callable(probe) else False
    except Exception:
        return False


def _entry_within_bounds(entry: Any) -> bool:
    try:
        ts, text, kwargs = entry
    except (TypeError, ValueError):
        return False
    if len(str(ts)) > PARKED_ENTRY_TS_MAX_CHARS:
        return False
    if len(str(text)) > PARKED_ENTRY_TEXT_MAX_CHARS:
        return False
    try:
        return len(repr(kwargs)) <= PARKED_ENTRY_KWARGS_MAX_CHARS
    except Exception:
        return False


def _park_queue(sessions: Any, key: str, entries: tuple[Any, ...]) -> None:
    # Under the REGISTRY key: adoption happens where a session is registered,
    # which is under the folded spelling, whatever spelling the channel passed.
    key = _folded(sessions, key)
    if len(key) > STOP_DECLINED_KEY_MAX_CHARS:
        for entry in entries:
            _drop_parked(sessions, key, entry, "refused: key past the string bound")
        return
    admitted = []
    for entry in entries:
        if _entry_within_bounds(entry):
            admitted.append(entry)
        else:
            _drop_parked(sessions, key, entry, "refused: entry past the field bounds")
    if not admitted:
        return
    parked = _parked_queue.setdefault(key, [])
    _parked_queue.move_to_end(key)
    parked.extend(admitted)
    while len(parked) > PARKED_QUEUE_MAX:
        _drop_parked(sessions, key, parked.pop(0), "evicted: past the per-key bound")
    while _parked_total() > PARKED_QUEUE_TOTAL_MAX:
        oldest_key = next(iter(_parked_queue))
        oldest = _parked_queue[oldest_key]
        _drop_parked(sessions, oldest_key, oldest.pop(0), "evicted: past the total bound")
        if not oldest:
            del _parked_queue[oldest_key]


def _folded(sessions: Any, key: str) -> str:
    """*key* under the registry's spelling, or unchanged when *sessions* cannot fold."""
    fold = getattr(sessions, "_fold_key", None)
    if callable(fold):
        try:
            folded = fold(key)
        except Exception:
            folded = None
        if isinstance(folded, str) and folded:
            return folded
    return key


def _parked_for(sessions: Any, key: str) -> bool:
    return bool(_parked_queue.get(_folded(sessions, key)))


def take_parked_queue(key: str) -> tuple[Any, ...]:
    """Remove and return the entries parked for *key* (empty when none)."""
    return tuple(_parked_queue.pop(key, ()))


def adopt_parked_queue(session: Any, key: str) -> int:
    """Move *key*'s parked entries onto a just-registered *session*'s queue head.

    Called by the allocation layer at the moment it registers a session under
    *key*, so ANY start that succeeds -- the hard stop's eager respawn, or the
    next ordinary allocation after that respawn failed too -- delivers what a
    forced stop parked. Returns how many entries moved.
    """
    parked = take_parked_queue(key)
    if not parked:
        return 0
    session.queue.extendleft(reversed(parked))
    return len(parked)


def allocation_identity(owner: Any, key: str, session: Any) -> dict[str, Any]:
    """The ``get_or_create`` arguments that rebuild *session*'s identity for *key*.

    Shared by the reset successor and the compaction restart
    (``session_compaction._restart_held``) so the two cannot drift: agent,
    approval policy, cwd, bound channel, the model the allocation selected, the
    crew member, the per-spawn backend, and the ``extra_env`` and
    ``reasoning_effort_override`` the caller passed.
    """
    backend = getattr(session, "backend_override", None)
    env = getattr(session, "extra_env", None)
    effort = getattr(session, "reasoning_effort_override", None)
    return {
        "agent": getattr(session, "agent", "") or None,
        "approval_policy": getattr(session, "approval_policy", ""),
        "cwd": getattr(session.provider, "cwd", None) or None,
        "channel_id": owner.get_channel(key) or None,
        "model": getattr(session, "requested_model", "") or None,
        "crew_agent": getattr(session, "capability_member", "") or None,
        # Each only when the session was started with one, so a session that
        # named none rebuilds with exactly the kwargs it always had.
        **({"backend_override": backend} if isinstance(backend, str) else {}),
        **({"extra_env": env} if env else {}),
        **({"reasoning_effort_override": effort} if effort else {}),
    }


async def hand_queue_to_successor(sessions: Any, key: str, entries: tuple[Any, ...]) -> bool:
    """Put *entries* (plus anything already parked for *key*) at the head of the
    key's successor queue and wake their channels. Returns whether they landed.

    ``get_or_create`` claims the successor or builds it; on a failed build the
    entries are PARKED, files kept, and the next registration under this key --
    the hard stop's eager respawn, or any later ordinary allocation -- adopts
    them (``adopt_parked_queue``). Nothing is unlinked on failure: these are
    other people's unanswered messages.
    """
    pending = tuple(entries)
    if not pending and not _parked_for(sessions, key):
        return True
    try:
        await sessions.get_or_create(key)
    except BaseException as exc:
        # ``BaseException``: a cancellation of the calling task lands here too,
        # and the entries are other people's messages either way -- parked, then
        # the cancellation is re-raised so the caller still unwinds.
        if pending:
            _park_queue(sessions, key, pending)
        _logger.warning(
            "no successor for %s yet: %d queue entr(y/ies) parked for its next start",
            key,
            len(pending),
            exc_info=True,
        )
        if not isinstance(exc, Exception):
            raise
        return False
    # ``get_or_create`` adopted whatever was parked, at registration or claim;
    # the new entries go ahead of it, as they were queued first, and the release
    # wakes the adopted entries' drains. The restored entries' drains are woken
    # here, after the release, as ``wake_other_drains`` requires.
    try:
        sessions.restore_queue(key, pending)
    finally:
        sessions.release(key)
    channels = [entry_channel(e[2]) for e in pending]
    if any(channels):
        await wake_other_drains(waker="", session_key=key, channels=channels)
    return True
