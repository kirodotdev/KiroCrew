"""Crewmate-to-crewmate messages: one mate sends a short text to another mate.

Design: the RFC *mate messaging* (``rfc-mate-messaging``). The route
(:mod:`kiro_crew.dashboard.handlers.member_peer_message`) finds the sender and
the target and calls :func:`plan_send` and then :func:`commit_send`. This module
holds the rules and the delivery:

* **Text.** :func:`peer_text` makes the one string that both the rows and the
  wake carry: credentials scrubbed, Unicode folded (NFKC and the context fold
  table), characters that do not print removed, and prompt boundary markers
  neutralized. The user sees what the target model reads.
* **Limits.** A hop limit for chains of replies (:data:`MAX_HOPS`), a cap of
  sends in one turn (:data:`MAX_SENDS_PER_TURN`), a sliding-window rate per
  sender and per pair, and a cap of messages that wait for one target.
* **Delivery.** Each message is one display-only row in both threads
  (:func:`chat_cards.record_peer_message`), then it waits for the target thread
  to be idle. One hidden turn (:func:`mate_welcome.dispatch_hidden_turn`)
  carries every waiting message, each in a fence with a new random id, as data
  from the sender (:func:`peer_prompt`). A message is never steered into a
  running turn. A wake that does not reach the model is tried once more.
* **Peer influence.** The hidden turn's task, its messages and the hop of each
  sender are kept per thread on the state (``state._mate_peer_turns``).
  ``slot.task`` is a new task for each turn, so :func:`peer_turn` matches only
  the turn that a peer message started. The thread then stays under peer
  influence (:func:`peer_influenced`) for :data:`PEER_INFLUENCE_SECS`, in every
  turn the user did not send, until a turn the user sent ends: a sub-agent
  result, a worker report or a heartbeat in that window is treated like the
  peer turn. The gateway refuses the self-change operations under peer
  influence (``handlers.guide._resolve_agent_caller``, the lesson route and the
  cron route), and a send under it continues the hop count.

All state is on the dashboard state object, never in module globals.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
import unicodedata
import uuid
from collections import deque
from typing import Any

from kiro_crew import crewmate_prompts

logger = logging.getLogger(__name__)

#: The envelope a peer-message wake opens with.
PEER_MESSAGE_PREFIX = "[Mate message]"

#: The longest message one send carries, in characters.
MAX_MESSAGE_CHARS = 4000
#: The highest hop a message can have. A first message is hop 1; a reply sent
#: from the turn that a hop-N message started is hop N+1.
MAX_HOPS = 3
#: The most messages one turn can send.
MAX_SENDS_PER_TURN = 3
#: The sliding window of the two rate limits, in seconds.
RATE_WINDOW_SECS = 600.0
#: The most messages one crewmate can send in one window.
RATE_MAX_PER_SENDER = 10
#: The most messages one crewmate can send to one target in one window.
RATE_MAX_PER_PAIR = 5
#: The most undelivered messages that can wait for one target thread.
MAX_PENDING_PER_TARGET = 10
#: How long a thread stays under peer influence after a peer turn starts, in seconds.
PEER_INFLUENCE_SECS = 1800.0
#: How many wakes one message gets before it is dropped.
MAX_DELIVERY_TRIES = 2

#: What a refused operation under peer influence says.
PEER_TURN_REFUSAL = (
    "another crewmate's message started this turn, or a turn before it that the "
    "user has not answered yet, so it cannot change this crewmate or put a card or "
    "a guide in front of the user; if the user asked for this, it works on the "
    "user's next message"
)

_BEGIN = "<<<mate message"
_END = ">>>end of mate message"
#: A run of fence characters in a message. The sender cannot write a fence.
_FENCE_RUN = re.compile(r"<{3,}|>{3,}")
#: The envelope tag in a message. The sender cannot open a second envelope.
_ENVELOPE = re.compile(re.escape(PEER_MESSAGE_PREFIX), re.IGNORECASE)
#: Character categories that do not print and are removed: format, private use,
#: surrogate and unassigned. Control characters other than a line break and a
#: tab are removed too.
_HIDDEN_CATEGORIES = frozenset({"Cf", "Co", "Cs", "Cn"})

_PREAMBLE = crewmate_prompts.text("wake.preamble")


def _now() -> float:
    """The clock of the rate window and the peer-influence window. Tests patch this binding."""
    return time.monotonic()


class PeerRefusal(Exception):
    """A send that a rule refuses: an HTTP *status*, a stable *code* and a reason."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def peer_text(text: Any) -> str:
    """The one form of a message that the rows and the wake carry, else ``""``."""
    if not isinstance(text, str) or not text:
        return ""
    from kiro_crew.context_assembly.markers import (
        _MULTIBYTE_TABLE,
        _neutralize_dashed_turn_fences,
        neutralize_untrusted_text,
    )
    from kiro_crew.member_goals import scrub_text

    out = unicodedata.normalize("NFKC", text).translate(_MULTIBYTE_TABLE)
    out = "".join(
        ch
        for ch in out
        if ch in "\n\t"
        or (unicodedata.category(ch) not in _HIDDEN_CATEGORIES and unicodedata.category(ch) != "Cc")
    )
    out = _neutralize_dashed_turn_fences(neutralize_untrusted_text(out))
    return _fenced(scrub_text(out)).strip()


def parse_pairs(entries: Any) -> frozenset[tuple[str, str]]:
    """:func:`kiro_crew.mate_messaging.parse_pairs`."""
    from kiro_crew import mate_messaging

    return mate_messaging.parse_pairs(entries)


def read_settings() -> tuple[bool, frozenset[tuple[str, str]]]:
    """``(switch on, allowed pairs)`` from the live config. Blocking IO."""
    from kiro_crew import mate_messaging

    return mate_messaging.read_settings()


def _store(state: Any, name: str, kind: type) -> Any:
    """The per-gateway record *name* on *state*, made on first use.

    A state object that cannot hold attributes gets an empty record that is
    not kept: no crewmate message can exist there, so nothing is under peer
    influence.
    """
    value = getattr(state, name, None)
    if not isinstance(value, kind):
        value = kind()
        try:
            setattr(state, name, value)
        except (AttributeError, TypeError):
            return value
    return value


def _pending(state: Any) -> dict[str, list[dict[str, Any]]]:
    """Target thread slot key -> the messages that wait for it."""
    return _store(state, "_mate_peer_pending", dict)


def _sent(state: Any) -> deque[tuple[float, str, str]]:
    """``(time, sender, target)`` of each send in the rate window."""
    return _store(state, "_mate_peer_sent", deque)


def _tasks(state: Any) -> dict[str, asyncio.Task[Any]]:
    """Target thread slot key -> its delivery task."""
    return _store(state, "_mate_peer_tasks", dict)


def _turns(state: Any) -> dict[str, dict[str, Any]]:
    """Thread slot key -> ``{"task", "senders", "messages", "ended"}`` of its last peer turn."""
    return _store(state, "_mate_peer_turns", dict)


def _influence(state: Any) -> dict[str, dict[str, float]]:
    """Thread slot key -> ``{"ts", "hop"}``: when its last peer turn started, its top hop."""
    return _store(state, "_mate_peer_influence", dict)


def _turn_counts(state: Any) -> dict[str, dict[str, Any]]:
    """Thread slot key -> ``{"task", "count"}``: the sends of its current turn."""
    return _store(state, "_mate_peer_turn_sends", dict)


def _live_task(slot: Any) -> Any:
    """The slot's running turn task, else ``None``."""
    task = getattr(slot, "task", None)
    if task is None:
        return None
    done = getattr(task, "done", None)
    if callable(done) and done():
        return None
    return task


def _user_sent(slot: Any) -> bool:
    return getattr(slot, "_turn_user_sent", False) is True


def peer_turn(state: Any, slot: Any) -> dict[str, Any] | None:
    """The record of the peer-message turn that runs in *slot* now, else ``None``."""
    record = _turns(state).get(getattr(slot, "key", ""))
    task = _live_task(slot)
    if not isinstance(record, dict) or task is None or record.get("task") is not task:
        return None
    return record


def is_peer_turn(state: Any, slot: Any) -> bool:
    """Whether the turn that runs in *slot* now is one a peer message started."""
    return peer_turn(state, slot) is not None


def _live_influence(state: Any, slot: Any) -> dict[str, float] | None:
    """*slot*'s peer-influence record while its window is open, else ``None``."""
    key = getattr(slot, "key", "")
    record = _influence(state).get(key)
    if not isinstance(record, dict):
        return None
    if _now() - float(record.get("ts", 0.0)) >= PEER_INFLUENCE_SECS:
        _influence(state).pop(key, None)
        return None
    return record


def peer_influenced(state: Any, slot: Any) -> bool:
    """Whether the turn that runs in *slot* now is under peer influence.

    True in a peer turn, and in a turn the user did not send while the thread's
    peer-influence window is open. A turn the user sent is never under it.
    """
    if is_peer_turn(state, slot):
        return True
    if _live_task(slot) is None or _user_sent(slot):
        return False
    return _live_influence(state, slot) is not None


#: The most parents :func:`peer_influenced_session` follows from a session. A
#: longer chain is treated as under peer influence (fail closed).
_LINEAGE_DEPTH = 32


def _tainted(state: Any) -> dict[str, float]:
    """Session or slot key -> when a turn under peer influence last gave it work."""
    return _store(state, "_mate_peer_tainted", dict)


def _taint_keys(state: Any, key: str) -> set[str]:
    """*key*, and the slot key it resolves to, if any."""
    keys = {key}
    try:
        from kiro_crew.dashboard.session_control import caller_slot_key

        slot_key = caller_slot_key(state, key)
        if slot_key:
            keys.add(slot_key)
    except Exception:  # noqa: BLE001 - the plain key is still recorded
        logger.debug("taint key resolution failed for %s", key, exc_info=True)
    return keys


def note_delegation(state: Any, caller_session_key: str, child_key: str) -> None:
    """A caller gave *child_key* work (a spawn, a continue, a steer, a create, a send).

    When the caller is under peer influence, the child is marked: the work it
    holds can carry another crewmate's words, so it stays under peer influence
    after the caller's own window closes, until a turn the user sent runs in
    the child itself. Work from a caller not under peer influence clears the
    mark, the same way the user's own turn clears a crewmate thread's window.
    Never raises.
    """
    if not child_key:
        return
    try:
        influenced = peer_influenced_session(state, caller_session_key)
        for key in _taint_keys(state, child_key):
            if influenced:
                _tainted(state)[key] = _now()
            else:
                _tainted(state).pop(key, None)
    except Exception:  # noqa: BLE001 - bookkeeping never fails a delegation
        logger.warning("crewmate message delegation mark failed for %s", child_key, exc_info=True)


def _is_tainted(state: Any, key: str, slot: Any) -> bool:
    marks = _tainted(state)
    return key in marks or (slot is not None and getattr(slot, "key", "") in marks)


def _subagent_parent(state: Any, session_key: str) -> str:
    """The parent session key of sub-agent *session_key*, else ``""``. No I/O."""
    manager = getattr(state, "subagents", None)
    for info in list(getattr(manager, "running", ()) or ()):
        key = getattr(info, "conversation_key", "") or f"subagent:{getattr(info, 'id', '')}"
        if key == session_key:
            parent = getattr(info, "parent_session_key", "")
            return parent if isinstance(parent, str) else ""
    return ""


def peer_influenced_session(state: Any, session_key: str) -> bool:
    """Whether the turn of *session_key* is under peer influence, its own or inherited.

    A crewmate's thread is under it as :func:`peer_influenced` says. A session
    that thread delegated to (a worker it created, a sub-agent it started, and
    their own children) inherits its live window, unless that session's own
    running turn is one the user sent. The walk follows the gateway's own
    lineage records (``slot._created_by``, the sub-agent's
    ``parent_session_key``), never a name from the request. Never raises.
    """
    if not session_key:
        return False
    try:
        slot = _slot_for(state, session_key)
        key = session_key
        seen: set[str] = set()
        for depth in range(_LINEAGE_DEPTH):
            if slot is not None:
                if depth == 0 and peer_influenced(state, slot):
                    return True
                if _user_sent(slot) and _live_task(slot) is not None:
                    # The user's own turn here, in the session or in an
                    # ancestor that is running it, is not a peer's work.
                    return False
                if depth > 0 and (
                    is_peer_turn(state, slot) or _live_influence(state, slot) is not None
                ):
                    return True
            if _is_tainted(state, key, slot):
                return True
            node = getattr(slot, "key", "") if slot is not None else key
            if not node or node in seen:
                return False
            seen.add(node)
            if slot is not None:
                creator = getattr(slot, "_created_by", "")
                key = f"dashboard:{creator}" if isinstance(creator, str) and creator else ""
                slot = state.get_slot(creator) if key else None
            else:
                key = _subagent_parent(state, key)
                slot = _slot_for(state, key) if key else None
            if not key:
                return False
        return True
    except Exception:  # noqa: BLE001 - an unknown session is not under peer influence
        logger.debug("peer influence check failed for %s", session_key, exc_info=True)
        return False


def _slot_for(state: Any, session_key: str) -> Any:
    """The dashboard slot *session_key* runs in, else ``None``."""
    from kiro_crew.dashboard.session_control import caller_slot_key

    slot_key = caller_slot_key(state, session_key)
    return state.get_slot(slot_key) if slot_key else None


def note_user_turn(state: Any, slot: Any) -> None:
    """A turn the user sent starts in *slot*: its peer-influence window closes. Never raises."""
    try:
        key = getattr(slot, "key", "")
        _influence(state).pop(key, None)
        _tainted(state).pop(key, None)
    except Exception:  # noqa: BLE001 - bookkeeping never stops a turn
        logger.debug("peer influence reset failed", exc_info=True)


def _turn_sends(state: Any, slot: Any) -> int:
    record = _turn_counts(state).get(getattr(slot, "key", ""))
    task = _live_task(slot)
    if isinstance(record, dict) and task is not None and record.get("task") is task:
        return int(record.get("count", 0))
    return 0


def _prune(sent: deque[tuple[float, str, str]], now: float) -> None:
    while sent and now - sent[0][0] >= RATE_WINDOW_SECS:
        sent.popleft()


def pair_allowed(state: Any, sender_slot: Any, sender: str, target: str, pairs: Any) -> bool:
    """Whether *sender* may message *target*: an allowed pair, or a reply in a peer turn."""
    if (sender, target) in pairs:
        return True
    turn = peer_turn(state, sender_slot)
    return turn is not None and target in (turn.get("senders") or {})


def _hop(state: Any, sender_slot: Any, target: str) -> int:
    """The hop a send from *sender_slot*'s running turn to *target* would have."""
    turn = peer_turn(state, sender_slot)
    if turn:
        senders: dict[str, int] = dict(turn.get("senders") or {})
        return senders.get(target, max(senders.values(), default=0)) + 1
    if _user_sent(sender_slot):
        return 1
    influence = _live_influence(state, sender_slot)
    return int(influence["hop"]) + 1 if influence else 1


def plan_send(
    state: Any, sender_slot: Any, sender: str, target: str, target_key: str, pairs: Any
) -> int:
    """The hop of a send from *sender* to *target*, or :class:`PeerRefusal`.

    Changes nothing. The caller has checked the switch, the sender and the
    target thread. No await may run between this and :func:`commit_send`.
    """
    if _live_task(sender_slot) is None:
        raise PeerRefusal(409, "no_dashboard_turn", "this request did not come from a running turn")
    if not pair_allowed(state, sender_slot, sender, target, pairs):
        raise PeerRefusal(
            403,
            "pair_not_allowed",
            f'the user has not let {sender} start a message to {target} (no "{sender}>{target}" '
            "entry in agent.mate_messaging_allow)",
        )
    hop = _hop(state, sender_slot, target)
    if hop > MAX_HOPS:
        raise PeerRefusal(
            409,
            "hop_limit",
            f"this message would be hop {hop}; a chain of crewmate messages stops at hop "
            f"{MAX_HOPS} until the user sends a message",
        )
    if _turn_sends(state, sender_slot) >= MAX_SENDS_PER_TURN:
        raise PeerRefusal(
            429, "turn_limit", f"one turn can send at most {MAX_SENDS_PER_TURN} crewmate messages"
        )
    sent = _sent(state)
    _prune(sent, _now())
    minutes = int(RATE_WINDOW_SECS // 60)
    by_sender = sum(1 for _ts, who, _to in sent if who == sender)
    if by_sender >= RATE_MAX_PER_SENDER:
        raise PeerRefusal(
            429,
            "rate_limited",
            f"{sender} sent {by_sender} crewmate messages in the last {minutes} minutes; "
            "try again later",
        )
    by_pair = sum(1 for _ts, who, to in sent if who == sender and to == target)
    if by_pair >= RATE_MAX_PER_PAIR:
        raise PeerRefusal(
            429,
            "rate_limited",
            f"{sender} sent {by_pair} messages to {target} in the last {minutes} minutes; "
            "try again later",
        )
    if len(_pending(state).get(target_key) or ()) >= MAX_PENDING_PER_TARGET:
        raise PeerRefusal(
            429, "queue_full", f"{target} has {MAX_PENDING_PER_TARGET} messages it has not read yet"
        )
    return hop


def commit_send(
    state: Any,
    sender_slot: Any,
    target_slot: Any,
    sender: str,
    target: str,
    text: str,
    hop: int,
) -> str:
    """Record and queue one planned send; returns the message id. Never awaits.

    *text* is :func:`peer_text`'s output. The target's row is written first:
    when it cannot be written, :class:`PeerRefusal` is raised and nothing
    changed, so the target never reads a message the user cannot see.
    """
    from kiro_crew.dashboard import chat_cards

    message_id = uuid.uuid4().hex[:16]
    if not chat_cards.record_peer_message(
        state,
        target_slot.key,
        direction="in",
        peer=sender,
        text=text,
        hop=hop,
        message_id=message_id,
    ):
        raise PeerRefusal(
            500, "not_recorded", f"the message could not be shown in {target}'s thread"
        )
    if not chat_cards.record_peer_message(
        state,
        sender_slot.key,
        direction="out",
        peer=target,
        text=text,
        hop=hop,
        message_id=message_id,
    ):
        logger.warning("crewmate message %s: the sender's row was not written", message_id)
    _turn_counts(state)[sender_slot.key] = {
        "task": _live_task(sender_slot),
        "count": _turn_sends(state, sender_slot) + 1,
    }
    _sent(state).append((_now(), sender, target))
    _pending(state).setdefault(target_slot.key, []).append(
        {"id": message_id, "sender": sender, "text": text, "hop": hop, "tries": 0}
    )
    schedule_flush(state, target_slot.key)
    return message_id


def _fenced(text: str) -> str:
    """*text* with each fence run broken and each envelope tag defused."""
    text = _FENCE_RUN.sub(lambda m: " ".join(m.group(0)), text)
    return _ENVELOPE.sub("(Mate message)", text)


def peer_prompt(messages: list[dict[str, Any]], *, nonce: str | None = None) -> str:
    """The hidden turn that gives a crewmate the messages other crewmates sent it.

    Each block opens and closes with a line that carries *nonce*, a new random
    id for each wake that no sender can know when it writes its text.
    """
    nonce = nonce or secrets.token_hex(6)
    lines = [PEER_MESSAGE_PREFIX + " " + _PREAMBLE.rstrip(), ""]
    for message in messages:
        sender = str(message.get("sender", ""))
        lines.append(
            crewmate_prompts.fill("peer_message.from", sender=sender, hop=int(message["hop"]))
        )
        lines.extend(
            [
                f"{_BEGIN} {nonce} from {sender}",
                _fenced(str(message.get("text", ""))),
                f"{_END} {nonce}",
                "",
            ]
        )
    lines.append(crewmate_prompts.fill("peer_message.guidance", max_hops=MAX_HOPS))
    return "\n".join(lines)


def thread_idle(slot: Any) -> bool:
    return not slot.running and not getattr(slot, "_queue", None)


def _drop_pending(state: Any, key: str, reason: str) -> None:
    dropped = _pending(state).pop(key, None) or []
    if dropped:
        _audit_drop(key, len(dropped), reason)


async def _flush(state: Any, key: str) -> None:
    """Deliver *key*'s waiting messages as one hidden turn once the thread is idle."""
    from kiro_crew.dashboard.mate_thread_wakes import is_crewmate_thread
    from kiro_crew.dashboard.mate_welcome import app_is_stopping, dispatch_hidden_turn

    while True:
        if not _pending(state).get(key):
            return
        enabled, _pairs = await asyncio.to_thread(read_settings)
        if not enabled:
            # The user turned crewmate messages off after the send: the kill
            # switch holds for messages that wait too.
            _drop_pending(state, key, "mate_messaging_off")
            return
        slot = state.get_slot(key)
        if not _pending(state).get(key) or slot is None or not is_crewmate_thread(slot):
            # Nothing waits, or the thread is gone: what waits stays, and the
            # thread's next turn end schedules this again.
            return
        if app_is_stopping(state):
            return
        if thread_idle(slot):
            messages = _pending(state).pop(key)
            if not dispatch_hidden_turn(state, slot, peer_prompt(messages), human_seen=False):
                _pending(state)[key] = messages + _pending(state).get(key, [])
                return
            senders: dict[str, int] = {}
            for message in messages:
                message["tries"] = int(message.get("tries", 0)) + 1
                senders[message["sender"]] = max(senders.get(message["sender"], 0), message["hop"])
            _turns(state)[key] = {
                "task": slot.task,
                "senders": senders,
                "messages": messages,
                "ended": False,
                "stop_gen": getattr(slot, "_stop_generation", 0),
            }
            add_done = getattr(slot.task, "add_done_callback", None)
            if callable(add_done):
                # A wake whose task ends with no turn end never ran: settle it then.
                wake = slot.task
                add_done(lambda _t: _settle(state, key, wake, reached_model=False))
            prior = _live_influence(state, slot)
            top = max(senders.values(), default=1)
            _influence(state)[key] = {
                "ts": _now(),
                "hop": max(top, int(prior["hop"])) if prior else top,
            }
            logger.info("crewmate message woke %s (%d message(s))", key, len(messages))
            return
        task = getattr(slot, "task", None)
        if not isinstance(task, asyncio.Future) or task.done():
            return
        await asyncio.wait({task})


def schedule_flush(state: Any, key: str) -> None:
    """Start *key*'s flush unless one already waits on its thread."""
    tasks = _tasks(state)
    running = tasks.get(key)
    if running is not None and not running.done():
        return

    async def run() -> None:
        try:
            await _flush(state, key)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("crewmate message delivery for %s failed", key, exc_info=True)
        finally:
            if tasks.get(key) is task:
                tasks.pop(key, None)

    task = asyncio.get_running_loop().create_task(run())
    tasks[key] = task


def _settle(
    state: Any, key: str, task: Any, *, reached_model: bool, stop_gen: int | None = None
) -> None:
    """End the peer wake *task* of thread *key* once. Never raises.

    The thread's peer-influence window restarts from now, so it covers the
    turns that follow a long wake. A wake that did not reach the model puts its
    messages back for one more try, unless the user pressed Stop on it; a
    message out of tries is dropped and audited.
    """
    try:
        record = _turns(state).get(key)
        if not isinstance(record, dict) or record.get("task") is not task or record.get("ended"):
            return
        record["ended"] = True
        influence = _influence(state).get(key)
        if isinstance(influence, dict):
            influence["ts"] = _now()
        if reached_model:
            return
        messages = list(record.get("messages") or [])
        stopped = stop_gen is not None and stop_gen != record.get("stop_gen", stop_gen)
        again = [] if stopped else [m for m in messages if m["tries"] < MAX_DELIVERY_TRIES]
        if again:
            _pending(state)[key] = again + _pending(state).get(key, [])
            schedule_flush(state, key)
        lost = len(messages) - len(again)
        if lost:
            _audit_drop(key, lost, "stopped" if stopped else "not_delivered")
    except Exception:  # noqa: BLE001 - bookkeeping never raises into a turn
        logger.warning("crewmate message settle failed for %s", key, exc_info=True)


def _audit_drop(key: str, count: int, reason: str) -> None:
    logger.warning("crewmate messages for %s not delivered (%d): %s", key, count, reason)
    try:
        from kiro_crew.dashboard.handlers.guide import _audit

        _audit("gateway", "member.message_mate", "denied", f"{reason}: {count} dropped")
    except Exception:  # noqa: BLE001 - an audit never changes the outcome
        logger.debug("crewmate message drop audit failed", exc_info=True)


def note_turn_end(state: Any, slot: Any, *, reached_model: bool = True) -> None:
    """Called at the end of a crewmate thread's top-level turn. Never raises.

    *reached_model* is False for a turn that ended before it reached the model.
    When that turn is a peer wake, :func:`_settle` puts its messages back for
    one more try. A turn the user sent closes the thread's peer-influence
    window when it starts (:func:`note_user_turn`).
    """
    try:
        key = getattr(slot, "key", "")
        if not key:
            return
        task = getattr(slot, "task", None)
        _settle(
            state,
            key,
            task,
            reached_model=reached_model,
            stop_gen=getattr(slot, "_stop_generation", 0),
        )
        counts = _turn_counts(state).get(key)
        if isinstance(counts, dict) and counts.get("task") is task:
            _turn_counts(state).pop(key, None)
        if _pending(state).get(key):
            schedule_flush(state, key)
    except Exception:
        logger.warning(
            "crewmate message bookkeeping failed for %s", getattr(slot, "key", "?"), exc_info=True
        )


def refuse_under_peer_influence(request: Any, operation: str) -> Any:
    """A 403 ``peer_message_turn`` for an agent's call under peer influence, else ``None``.

    For the routes that start work this module cannot follow (a workflow, a
    task run): their sessions are not in the lineage walk, so a turn under
    peer influence may not start one. Only the agent's own MCP call (internal
    secret) is checked. Never raises.
    """
    try:
        if request.get("internal_auth") is not True:
            return None
        session_key = request.headers.get("X-Session-Key", "").strip()
        if not peer_influenced_session(request.app["state"], session_key):
            return None
        from aiohttp import web

        from kiro_crew.dashboard.handlers.guide import _audit

        _audit(session_key, operation, "denied", "peer message turn")
        return web.json_response(
            {"error": PEER_TURN_REFUSAL, "code": "peer_message_turn"}, status=403
        )
    except Exception:  # noqa: BLE001 - a failed check never blocks the owner's own work
        logger.debug("peer influence route check failed for %s", operation, exc_info=True)
        return None
