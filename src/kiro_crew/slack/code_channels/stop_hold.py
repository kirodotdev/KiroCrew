"""After a code channel's Stop, nothing it stopped starts again until a person writes.

``press`` (``stop_helpers.stop_channel``, before its first await) holds the channel's
session and every helper agent the store records for it: ``slot._stop_held``,
persisted as the slot's own ``stop_held`` line field, and ``slot._stop_hold_seq``,
the press that set it. The one decision is :func:`decide`, asked synchronously by
every path that can start a turn, in the same span as the effect it guards, and
last by ``_turn_dispatch_gates`` (:func:`gate`), where a hold is released (a local
``/`` command, whose effect runs before that, is asked at the start of
``_turn_slash_gate``).

A person's request carries a stamp, ``<boot>:<press>``: the last press this process
had made when the request ARRIVED, read only at an arrival point (:data:`ENTRIES`)
before any await of ours (:func:`seen`), and built once the gateway's own
authorization admitted a human (:func:`stamp`, :func:`http_stamp`). Everything
below takes that press or stamp as a parameter and passes it on unchanged. It
releases a hold no newer than that press, so a request that entered before the
press, or before a restart (another boot), never does. Wakes, peers (the
channel's own session included), apps, births and resume carry none, so a held
helper is released only by a person's message to that helper. No clock is
compared anywhere.

Writes go through the slot's own save, which encodes ``stop_held`` from the live
slot under the transcript lock: the latest value is what lands, whichever write
is last. A release is a synchronous flip whose save is started, not awaited.
"""

from __future__ import annotations

import asyncio
import inspect
import itertools
import logging
import os
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)

BOOT = os.urandom(6).hex()
FREE, HELD, RELEASE = "free", "held", "release"
#: The queue-entry meta key a person's stamp rides on.
STAMP_META = "stopHoldStamp"
STOPPED_TEXT = "Stopped from Slack. Nothing starts here until someone writes."
#: A peer's send to a held session: what the sender's tool result says.
QUEUED_TEXT = (
    "Queued: a Stop in its Slack channel holds this session, and it starts when a person "
    "messages it."
)
#: The only functions that call :func:`seen` (module path, function): where a request
#: ARRIVES, each before its first await. The outermost HTTP middleware (first in both
#: apps' lists) and the Slack Socket Mode listener; Slack reaches the gateway over no
#: HTTP route. Checked by an AST scan in the tests.
ENTRIES = frozenset(
    {
        ("kiro_crew/slack/code_channels/stop_hold.py", "stop_hold_arrival_middleware"),
        ("kiro_crew/slack/events.py", "_on_event"),
    }
)
#: The request key the arrival middleware writes the press it read into.
STOP_SEEN = "stop_hold_seen"

_presses = itertools.count(1)
_last = 0
#: The authority: the press that holds each session key, whether its slot is live, absent
#: or replaced by another object of the same key. A slot's own fields only mirror it, for
#: its line and a restart. Bounded by the Stops that add keys (a channel's session and its
#: helpers): a release drops its key, an undo its own press. Memory only; a restart seeds
#: a held line's key at press 0 (:func:`restore`).
_keys: dict[str, int] = {}
#: The dashboard state the last press ran under: the save of a hold applied on first ask.
_state_seen: Any = None
#: Each slot's running harness-local command (:func:`run_local`): a press ends it.
_local: dict[str, Any] = {}
#: The KAS commands a press may end mid-way: each only sends engine RPCs. Any other
#: local command (``/clear``'s teardown above all) is never cancelled.
ENDABLE_LOCAL = frozenset({"/context", "/code", "/knowledge"})
#: The channel's session each press held, by channel: ``(session key, press)``, for a
#: session that replaces it under a new key (:func:`carry`).
_channels: dict[str, tuple[str, int]] = {}
#: Keys whose mirror was set but not yet saved: the first ask on the published object
#: saves it (:func:`holds`).
_owed: set[str] = set()


def seen() -> int:
    """The last press, read where a request arrives, before any await (:data:`ENTRIES`)."""
    return _last


@web.middleware
async def stop_hold_arrival_middleware(request: web.Request, handler: Any) -> Any:
    """Outermost in both apps: the last press as the request arrives, before any await
    of ours, for :func:`http_stamp`. A request that never passed here carries none."""
    request[STOP_SEEN] = seen()
    return await handler(request)


def stamp(seen_press: int | None, person: bool) -> str:
    """A person's stamp for a request that read *seen_press* on arrival; ``""`` for anyone
    else, and for a caller that read none."""
    return f"{BOOT}:{int(seen_press)}" if person and seen_press is not None else ""


def http_stamp(request: Any, person: bool = True) -> str:
    """:func:`stamp` for a dashboard request, from the press read on its arrival: a person
    only when token auth marked a dashboard user, never the loopback internal caller (an
    MCP tool, a script)."""
    human = request.get("is_dashboard_user") is True and not request.get("internal_auth")
    return stamp(request.get(STOP_SEEN), person and human and not request.get("app", ""))


def _recorded(key: str) -> int | None:
    """The press that holds *key* (its record or the channel's fact); None for none."""
    facts = [p for k, p in _channels.values() if k == key]
    if not key or (key not in _keys and not facts):
        return None
    return max([_keys.get(key, 0), *facts])


def holds(slot: Any, save: bool = True) -> bool:
    """Whether a Stop holds *slot*, answered from its key's record (and the channel's
    fact), never from the object: whatever object carries the key, published by any path,
    is held and its mirror set. The mirror is saved once the object is the live registry
    entry and not under construction (only its constructor writes it until then):
    owed until then, and never by a registration (*save* False, :func:`on_register`)."""
    key = str(getattr(slot, "key", "") or "")
    if (press_ := _recorded(key)) is None:
        return False
    if not (getattr(slot, "_stop_held", False) is True and slot._stop_hold_seq >= press_):
        _hold(slot, press_)
        _owed.add(key)
    if save and key in _owed and _published(_state_seen, slot):
        _owed.discard(key)
        _save_soon(_state_seen, slot)
    return True


def _published(state: Any, slot: Any) -> bool:
    """*slot* is the live registry entry of its key and not under construction."""
    key = getattr(slot, "key", "")
    return (getattr(state, "_slots", None) or {}).get(key) is slot and key not in getattr(
        state, "_slots_under_construction", ()
    )


def held_flag(slot: Any) -> bool:
    """What the slot's line writes (off the loop, atomic reads only): held while its key's
    record says so, never from the object's mirror. Every writer of the line passes here,
    so a save of an object no ask has mirrored yet (a direct publication before a shutdown
    save) still writes the hold, and a retained old object whose key a person released
    through another object writes the release, fenced or not."""
    key = str(getattr(slot, "key", "") or "")
    facts = list(_channels.values())
    return bool(key) and (key in _keys or any(k == key for k, _ in facts))


def restore(slot: Any) -> None:
    """A line restored with ``stop_held``: its key held at press 0 for this boot, never
    lowering a press already recorded."""
    _hold(slot, _keys.get(str(getattr(slot, "key", "") or ""), 0))


def fresh(slot: Any) -> None:
    """A new slot's hold fields: not held, by no press."""
    slot._stop_held, slot._stop_hold_seq = False, 0


def _admits(slot: Any, stamp_: str) -> bool:
    """A person's stamp of this boot, no older than the press *slot*'s key is held at."""
    return _stamp_admits(stamp_, _recorded(str(getattr(slot, "key", "") or "")) or 0)


def _stamp_admits(stamp_: str, floor: int) -> bool:
    boot, _, press_ = str(stamp_ or "").partition(":")
    return boot == BOOT and press_.isdigit() and int(press_) >= floor


def held_now(slot: Any, stamp_: str = "") -> bool:
    """:func:`queues`' answer with none of its writes, safe on a thread off the loop (a
    hook's spawn worker): the key's press read once, from snapshots, then decided from
    that one value. Writes nothing, calls no asyncio, does no I/O; the mirror and its
    save are the next :func:`queues` on the loop's."""
    key = str(getattr(slot, "key", "") or "")
    record = _keys.get(key)
    facts = [p for k, p in list(_channels.values()) if k == key]
    if not key or (record is None and not facts):
        return False
    return not _stamp_admits(stamp_, max([record or 0, *facts]))


def decide(slot: Any, stamp_: str = "") -> str:
    """FREE (not held), RELEASE (a stamp no older than the hold) or HELD."""
    if not holds(slot):
        return FREE
    return RELEASE if _admits(slot, stamp_) else HELD


def queues(slot: Any, stamp_: str = "") -> bool:
    return decide(slot, stamp_) == HELD


def holds_queue(slot: Any) -> bool:
    """The queue drain's ask: held unless a person's entry waits in the queue."""
    return queues(slot, queue_stamp(slot))


def queue_stamp(slot: Any, items: Any = None) -> str:
    """The newest person's stamp among *items* (*slot*'s queued entries by default) that
    *slot* admits, else ``""``: on a held slot, one that releases it."""
    found = [
        str((item.get("meta") or {}).get(STAMP_META) or "")
        for item in (getattr(slot, "_queue", ()) if items is None else items)
    ]
    admitted = [s for s in found if _admits(slot, s)]
    return max(admitted, key=lambda s: int(s.partition(":")[2]), default="")


def meta(stamp_: str) -> dict[str, str]:
    return {STAMP_META: stamp_} if stamp_ else {}


def refusal(slot: Any) -> dict[str, str]:
    """The refused send's body while *slot* is held, ``{}`` otherwise."""
    return {"error": STOPPED_TEXT, "code": "channel_stopped"} if holds(slot) else {}


def run_kwargs(stamp_: str) -> dict[str, Any]:
    """``_run_chat``'s keyword for a person's stamp, only when there is one."""
    return {"_stop_hold_stamp": stamp_} if stamp_ else {}


class HeldAtWrite(Exception):
    """A Stop held the session after the dispatch gate: nothing was written or run."""


def write_gate(client: Any, slot: Any, stamp_: str, *, call: str = "stream") -> dict[str, Any]:
    """The ``prompt_gate`` keyword for ``client.<call>`` (``stream``, or ``stream_command``
    for a ``/`` command): the turn's last ask, called by the transport synchronously
    under its write lock, right before the prompt or command is written, so a Stop that
    landed in the transport's awaits since the dispatch gate ends the turn with nothing
    sent. Both real providers declare it on both; a client whose method does not is
    called as before."""

    def ask() -> None:
        if queues(slot, stamp_):
            raise HeldAtWrite(getattr(slot, "key", ""))

    try:
        takes = "prompt_gate" in inspect.signature(getattr(client, call)).parameters
    except (AttributeError, TypeError, ValueError):  # no stream, or no signature to read
        takes = False
    return {"prompt_gate": ask} if takes else {}


def drain_kwargs(slot: Any, consumed: list[Any]) -> dict[str, Any]:
    """The queue drain's stamp: the newest person's among the entries this turn runs
    and those still queued behind them, so a person's entry anywhere in the queue
    releases the hold and the queue drains in order."""
    return run_kwargs(queue_stamp(slot, [*consumed, *getattr(slot, "_queue", ())]))


def gate(state: Any, slot: Any, stamp_: str) -> bool:
    """A turn's hold decision, synchronous: False ends a held turn; a person's releases."""
    verdict = decide(slot, stamp_)
    if verdict == RELEASE:
        slot._stop_held = False
        _drop(str(getattr(slot, "key", "") or ""))
        _save_soon(state, slot)
    return verdict != HELD


def _drop(key: str, press_: int = 0) -> None:
    """*key*'s record and channel fact (only those of *press_* when given)."""
    if not press_ or _keys.get(key) == press_:
        _keys.pop(key, None)
        _owed.discard(key)
    for channel in [c for c, (k, p) in _channels.items() if k == key and press_ in (0, p)]:
        del _channels[channel]


def _hold(slot: Any, press_: int) -> None:
    """Record *press_* for *slot*'s key (never lowering it) and mirror it on the object."""
    key = str(getattr(slot, "key", "") or "")
    if key:
        _keys[key] = max(press_, _keys.get(key, 0))
    slot._stop_held = True
    slot._stop_hold_seq = max(press_, _keys.get(key, 0), int(getattr(slot, "_stop_hold_seq", 0)))


class Pressed(list):  # type: ignore[type-arg]
    """The slots a channel's Stop held; ``unread`` when its helper records could not be
    read. *before* is each slot's hold before this press, for :meth:`undo`."""

    unread = False

    def __init__(self, press_: int = 0) -> None:
        super().__init__()
        self.press = press_
        self.before: dict[int, tuple[bool, int]] = {}
        self.keyed: set[str] = set()  # the keys it held with no live slot

    def hold(self, slot: Any) -> None:
        if slot not in self:
            key = str(getattr(slot, "key", "") or "")
            self.before[id(slot)] = (key in _keys, _keys.get(key, 0))
            _hold(slot, self.press)
            self.append(slot)
            if (local := _local.get(getattr(slot, "key", ""))) is not None:
                local.cancel()  # a running local command ends at its next await

    def holds_any(self) -> bool:
        """A slot it holds and did not undo, or a key it held with no live slot whose record
        is still this press (not released or pressed again since)."""
        return len(self) > 0 or any(_keys.get(k) == self.press for k in self.keyed)

    def undo(self, state: Any, slot: Any) -> None:
        """*slot*'s hold as it was before this press, unless a later press or a release
        changed it since (a Stop that was not applied to it)."""
        if slot in self and _keys.get(slot.key) == self.press:
            slot._stop_held, slot._stop_hold_seq = self.before[id(slot)]
            self.remove(slot)
            if slot._stop_held:  # restored first, so a line encoded meanwhile reads it
                _keys[slot.key] = slot._stop_hold_seq
            _drop(slot.key, self.press)  # the record goes back to what it was
            _save_soon(state, slot)


def press(state: Any, conductor_key: str, channel_slot: Any = None, channel: str = "") -> Pressed:
    """``stop_helpers.stop_channel``'s one call, before its first await: one press holds the
    channel's session (*channel_slot*), THEN reads its helpers from the store (in
    memory) and holds them, so a helper born meanwhile is held by :func:`inherit`
    or by this read. A helper with no live slot is held when it registers
    (:func:`on_register`), and a channel session replacing it by :func:`carry`: the
    press writes the channel's fact, then reads the slots, with no await between.
    Memory only; :func:`persist` writes it."""
    from kiro_crew.slack.code_channels.store import StoreUnavailable, store

    global _last, _state_seen
    _last, _state_seen = next(_presses), state
    pressed = Pressed(_last)
    if channel and conductor_key:
        _channels[channel] = (conductor_key, _last)
    if channel_slot is not None:
        pressed.hold(channel_slot)
    elif conductor_key:  # the channel's session with no live slot: held when it registers
        _keys[conductor_key] = max(_last, _keys.get(conductor_key, 0))
        pressed.keyed.add(conductor_key)
    if not conductor_key:
        return pressed
    try:
        keys = [k for k in store().workers_of(conductor_key) if k and k != conductor_key]
    except (StoreUnavailable, OSError):
        pressed.unread = True
        return pressed
    get_slot = getattr(state, "get_slot", None)
    for key in keys:
        slot = get_slot(key) if callable(get_slot) else None
        if slot is None:
            _keys[key] = max(_last, _keys.get(key, 0))
            pressed.keyed.add(key)
        else:
            pressed.hold(slot)
    return pressed


def on_register(slot: Any) -> None:
    """A registering slot mirrors its key's record, for its line, unsaved: a registration
    may be a construction's (:func:`holds` decides, and saves once it is published)."""
    holds(slot, save=False)


async def run_local(slot: Any, backend: str, word: str, command: Any) -> None:
    """Run a harness-local command; one in :data:`ENDABLE_LOCAL` (on KAS) as a task a
    Stop's press cancels (:class:`Pressed`). Only that cancel ends here."""
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS

    if not (backend == ACP_BACKEND_KAS and word.lower() in ENDABLE_LOCAL):
        return await command
    task = _local[slot.key] = asyncio.ensure_future(command)
    me = asyncio.current_task()
    before = me.cancelling() if me else 0  # an outer cancel absorbed earlier is not this one
    try:
        await task
    except asyncio.CancelledError:
        if not task.cancelled() or (me is not None and me.cancelling() > before):
            raise
        logger.info("local command in %s ended by its code channel's Stop", slot.key)
    finally:
        if _local.get(slot.key) is task:
            del _local[slot.key]


def carry(channel: str, slot: Any) -> None:
    """After a replacement session's binding is written, read the channel's press: the
    press that held the session it replaces holds it (:func:`press` writes, then reads)."""
    if (entry := _channels.get(channel)) is not None:
        _channels[channel] = (slot.key, entry[1])
        _hold(slot, entry[1])


def inherit(state: Any, creator: Any, newborn: Any) -> None:
    """A helper born of a held session is held too (read on the loop, after its record)."""
    if holds(creator):
        _hold(newborn, _recorded(str(getattr(creator, "key", "") or "")) or 0)
        _save_soon(state, newborn)


async def persist(state: Any, slots: list[Any]) -> None:
    """The Stop's own writes, awaited before its brake: each encodes the live value, and
    each writes only the object the registry holds for its key, checked right before the
    save and again at its locked commit (``expected_slot_name``). An object closed,
    replaced or under construction meanwhile is not written: its key stays owed, and the
    first ask on the object published under it saves it (:func:`holds`)."""
    from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
    from kiro_crew.dashboard.chat_utils_slots import slot_history_key

    for slot in list(slots):
        if not _published(state, slot):
            _owe(slot.key)
            continue
        try:
            if not getattr(slot, "window_pending", False) and not slot.messages:
                # A live helper with no rows yet (its birth record only): only a forced
                # save writes an empty window's line, pinned to the transcript read here.
                pin: dict[str, Any] = {
                    "force": True,
                    "expected_history_key": slot_history_key(slot),
                }
            else:
                slot._dirty = True  # the save writes a dirty slot's line, loaded or not
                pin = {}
            await save_slot_off_loop(state, slot, expected_slot_name=slot.key, **pin)
        except Exception:
            logger.warning("code channel stop: hold of %s not saved", slot.key, exc_info=True)


def _owe(key: str) -> None:
    """*key*'s hold, not written here, for the first ask on its published object."""
    if _recorded(key) is not None:
        _owed.add(key)


def _save_soon(state: Any, slot: Any) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # no loop here: the slot's next save writes the live value
        return
    if getattr(state, "_background_tasks", None) is None:
        return
    task = loop.create_task(persist(state, [slot]))
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)


def _reset_for_tests() -> None:
    """No press recorded: the process-wide records a test's Stop leaves behind."""
    global _state_seen
    for record in (_keys, _channels, _owed, _local):
        record.clear()
    _state_seen = None
