"""Keep the owner's per-chat "Trust this session" across a gateway restart.

The store itself is :mod:`kiro_crew.dashboard.session_trust_store`; this module is
the dashboard half: WHO may make a grant durable, and how a restored grant is put
back on a chat.

Durable only when a person made it. :func:`persists_grant` accepts the owner's
own dashboard session and nothing else -- not an app token, not the agent's
internal credential, not a non-owner dashboard login -- and :func:`owner_chats`
leaves out app-owned chats, whose trust belongs to the app's own expiring grant.
Trust a session inherits from its creator, a crew worker's scoped grant and the
process-wide safety override never reach the store at all, because none of them
is written through the two owner-click sites that call :func:`persist_grant`.

A revoke is honoured from ANY caller that could make the live revoke: taking
authority away is never something to refuse.

Crash-only. The record carries trust across a restart the owner did NOT ask
for and no other: :func:`clear_on_shutdown_async` deletes it on every owner stop
(exit status 0 -- ``kirocrew stop``/``restart``, a signal, a host reboot), so a
deliberate restart still forces re-consent. A crash, an OOM kill and the
loop-stall watchdog (which dumps and ``_exit``\\s from a signal, never through
the shutdown path) leave it in place, so unattended work in a trusted chat keeps
running after the gateway relaunches itself.

Restoring sets exactly what the click sets -- ``slot._trust`` and the session's
``approval_policy`` -- and nothing more, so every gate the live grant is subject
to (deny lists, ``human_only`` approvals) applies after a restart unchanged.
``trust`` is a non-deniable approval mode, so there is no ``approval_modes``
ceiling to re-check at restore: the grant route never consults one either.

A revoke whose saved copy could not be removed is remembered in memory
(:func:`_withdraw`), so neither restore path hands back a grant the owner took
away in this process even though its record survived on disk.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, AsyncIterator, Iterable

from kiro_crew.dashboard import session_trust_store
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.sel import sel

if TYPE_CHECKING:
    from aiohttp import web

    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

logger = logging.getLogger(__name__)

#: Session keys whose revoke applied live but whose saved record could not be
#: removed. Loop-affine. A restore never hands these back in this process; a later
#: grant that IS saved lifts the entry, since that record is the owner's new click.
_withdrawn_keys: set[str] = set()

#: Set when a revoke of EVERY chat could not clear the store. Lifted only by a
#: clear that succeeds; until then this process restores nothing.
_withdrawn_all = False

#: When this process began. An owner-stop marker newer than this is a stop aimed
#: at this run rather than one a previous run left to settle.
_STARTED = time.time()

#: Session keys a permanent delete is removing trust for right now, counted per
#: delete. A grant on one of them is not saved until every such delete is done.
_delete_fenced: Counter[str] = Counter()


class DeleteFence:
    """The keys one delete holds against grants; see :func:`delete_fence`."""

    def __init__(self) -> None:
        self.keys: list[str] = []

    def hold(self, keys: Iterable[str]) -> None:
        for key in keys:
            self.keys.append(key)
            _delete_fenced[key] += 1


@contextlib.asynccontextmanager
async def delete_fence() -> AsyncIterator[DeleteFence]:
    """Hold the keys a delete removes trust for until it commits or rolls back.

    Without it, a grant saved between the removal and the unlink would survive
    the deleted transcript, and nothing would ever remove it. Released on every
    exit, so a refused or failed delete never leaves a chat unable to save.
    """
    fence = DeleteFence()
    try:
        yield fence
    finally:
        for key in fence.keys:
            _delete_fenced[key] -= 1
            if _delete_fenced[key] <= 0:
                del _delete_fenced[key]


def _withdraw(keys: Iterable[str]) -> None:
    _withdrawn_keys.update(keys)


def is_withdrawn(session_key: str) -> bool:
    """Whether a revoke in this process took *session_key*'s trust away for good."""
    return _withdrawn_all or session_key in _withdrawn_keys


def persists_grant(request: "web.Request") -> bool:
    """Whether a trust grant made by *request* may outlive the gateway.

    Only the owner's own dashboard session. The agent's internal credential is
    refused explicitly even though it carries no ``user`` claim today: an agent
    that could make its own trust durable would have defeated the prompt it is
    trusted past.
    """
    if request.get("internal_auth") is True:
        return False
    if str(request.get("app") or ""):
        return False
    try:
        return is_owner_dashboard_request(request)
    except Exception:
        return False


def owner_chats(slots: Iterable["_ChatSlot"]) -> list["_ChatSlot"]:
    """The chats a grant on *slots* may make durable: every one not owned by an app."""
    return [s for s in slots if not str(getattr(s, "_app", "") or "")]


def session_keys(slots: Iterable["_ChatSlot"]) -> list[str]:
    """The distinct session keys *slots* address, in order."""
    return list(dict.fromkeys(effective_session_key(s) for s in slots))


async def persist_grant(request: "web.Request", slots: Iterable["_ChatSlot"]) -> bool | None:
    """Make the owner's grant on *slots* durable, BEFORE the caller publishes it.

    ``None`` when this grant is not one that persists (not the owner's own
    dashboard session, or no owner chat among *slots*); ``True`` when it is
    saved; ``False`` when the write failed, which the caller publishes live-only
    (:func:`notify_live_only`): a crash then drops it, as before it was saved.
    A revoke racing the write is the caller's to settle, by comparing
    :func:`revoke_generation` before and after.
    """
    if not persists_grant(request):
        return None
    held = owner_chats(slots)
    keys = session_keys(held)
    if not keys:
        return None
    global _owner_stopped, _withdrawn_all
    async with _write_order():
        if _stopping:
            # An owner stop is clearing (or has cleared) the store: a record
            # written now would outlive the stop that withdrew every grant.
            return False
        # A previous owner stop whose clear failed may have left the marker --
        # whether or not this process has read it yet (a boot restore still
        # pending reads it later). Settle it here, in the write order, so that
        # restore cannot clear this grant afterwards: settling removes the marker,
        # and a settle that finds no marker clears nothing. One that cannot be
        # settled is not saved, so the grant stays live-only. A marker written
        # since this process started is an owner stop in progress from outside
        # (the CLI marks before it ends this gateway): that one is never settled.
        if not await asyncio.to_thread(session_trust_store.settle_owner_stop, _STARTED):
            return False
        if any(k in _delete_fenced for k in keys):
            # A permanent delete of this session is removing its trust; a record
            # written now could outlive the deleted transcript.
            return False
        # A revoke that applied live but could not remove its record left that
        # record on disk: this grant's rewrite must not carry it forward, or the
        # next crash would restore what the owner took away.
        replace_all = _withdrawn_all
        withdrawn = set(_withdrawn_keys) - set(keys)
        generation = revoke_generation()
        written, dropped = await asyncio.to_thread(
            lambda: session_trust_store.grant_counting(keys, drop=withdrawn, replace=replace_all)
        )
        if written and revoke_generation() == generation:
            # A saved grant is the owner's fresh click; it supersedes a withdrawn
            # one, and a settled owner stop withholds nothing from this record.
            # The rewrite also removed every withdrawn record from disk, so the
            # in-memory withholding that stood in for those removals is cleared.
            # Only when no revoke started during the write: that revoke's own
            # withholding is not this rewrite's to lift, whichever chat it names.
            _withdrawn_keys.clear()
            _withdrawn_all = False
            _owner_stopped = False
    if dropped:
        _notify_dropped(request, dropped)
    return written


def notify_live_only(state: Any, *, caller: str) -> None:
    """Tell the owner a grant is on but was not saved, so a crash will drop it.

    A save that fails degrades the grant to live-only -- what trust was before it
    was ever saved -- rather than refusing it, so a host that cannot write the
    record keeps the capability it always had. Audited and announced so the owner
    is not surprised when a crash comes back untrusted.
    """
    try:
        sel().log_api_access(
            caller=caller,
            operation="session_trust:not_saved",
            outcome="live_only",
            resources="session-trust",
        )
    except Exception:
        logger.warning("SEL audit failed for live-only session trust", exc_info=True)
    try:
        state.notify(
            "agent",
            "Chat trust is on until the next restart",
            "Trust is on for this chat, but it could not be saved, so a crash or "
            "restart will turn it off and the chat will ask for approval again.",
        )
    except Exception:
        logger.warning("could not deliver the live-only trust notice", exc_info=True)


def notify_revoke_not_durable(state: Any) -> None:
    """Tell the owner a saved grant outlived its revoke, so a crash could return it.

    The response carries ``trust_revoke_not_durable`` too, but the dashboard's
    picker does not render that code, so the notice is what the owner sees. This
    process withholds the grant (:func:`_withdraw`); a later crash would not.
    """
    try:
        state.notify(
            "agent",
            "Saved chat trust could not be removed",
            "Trust was turned off, but its saved copy could not be deleted, so a "
            "crash could turn it back on. Turn trust off again once the data "
            "folder is writable.",
        )
    except Exception:
        logger.warning("could not deliver the revoke-not-durable notice", exc_info=True)


#: Serializes every store write -- grant, revoke, clear -- in the order the event
#: loop issued them. Each write runs on a worker thread, and without this a revoke
#: issued first could reach the store after a later grant and erase it while
#: that grant had already been reported saved. ``asyncio.Lock`` wakes waiters in
#: arrival order. Created per running loop, since a lock is bound to one.
_order_lock: "tuple[asyncio.AbstractEventLoop, asyncio.Lock] | None" = None


def _write_order() -> asyncio.Lock:
    global _order_lock
    loop = asyncio.get_running_loop()
    if _order_lock is None or _order_lock[0] is not loop:
        _order_lock = (loop, asyncio.Lock())
    return _order_lock[1]


def _notify_dropped(request: "web.Request", dropped: int) -> None:
    """Tell the owner the store's bound forgot their oldest trusted chats."""
    try:
        state = request.app["state"]
        state.notify(
            "agent",
            "Oldest saved chat trust dropped",
            f"Kiro Crew remembers at most {session_trust_store.MAX_SESSIONS} trusted "
            f"chats across a crash. Trusting this chat made it forget the oldest "
            f"{dropped}; those chats come back untrusted after a crash.",
        )
    except Exception:
        logger.warning("could not deliver the session-trust overflow notice", exc_info=True)


def grant_identity(slots: Iterable["_ChatSlot"]) -> tuple[str, ...]:
    """The session keys a grant on *slots* writes. Compare across an await.

    A slot's effective key can be rebound while a grant is being saved (a
    channel binding resolved meanwhile), so the key that was saved is not the
    key now trusted; a caller that sees this move refuses the grant and takes
    back what it saved, by these keys, through :func:`persist_revoke_keys`.
    """
    return tuple(session_keys(owner_chats(slots)))


async def persist_revoke_keys(keys: Iterable[str]) -> bool:
    """Forget any durable grant under *keys*. Fails closed inside the store.

    Keys, not slots: a caller resolves them before any await, so a slot rebound
    meanwhile cannot aim the removal at a key that was never saved. Every chat,
    app-owned or not -- forgetting authority is never refused.
    """
    keys = list(dict.fromkeys(keys))
    if not keys:
        return True
    _bump_revoke_generation()
    # Withheld BEFORE the store write is awaited: a restore verdict read while
    # the write is in flight (or after it fails) must already see the revoke.
    # Lifted once the record is provably gone.
    _withdraw(keys)

    async def _ordered_revoke() -> bool:
        async with _write_order():
            return await asyncio.to_thread(session_trust_store.revoke, keys)

    try:
        # Bounded: every live revoke waits on this, and a stalled disk must not
        # keep a chat auto-approving. A timeout is a failed removal -- the keys
        # stay withheld in memory and the caller announces it -- and the live
        # revoke goes ahead regardless.
        removed = await asyncio.wait_for(_ordered_revoke(), timeout=_REVOKE_TIMEOUT)
    except asyncio.TimeoutError:
        logger.error("removing saved session trust timed out; withheld in memory")
        removed = False
    if removed:
        _withdrawn_keys.difference_update(keys)
    return removed


#: Bound on one durable revoke, including its wait for the store's write order.
_REVOKE_TIMEOUT = 2.0


@dataclass(frozen=True)
class DeletedTrust:
    """What :func:`forget_deleted_session` did: whether it is safe to unlink, and
    which keys it took away, so a delete that does not commit can put them back."""

    ok: bool
    held: tuple[str, ...] = ()
    #: The revoke generation right after this delete's own removal. A restore is
    #: skipped once any other revoke has moved it.
    generation: int = -1


async def forget_deleted_session(
    state: Any, keys: Iterable[str | None], fence: DeleteFence | None = None
) -> DeletedTrust:
    """Forget saved trust for a session about to be permanently deleted. Never raises.

    Session keys are deterministic, so a chat recreated under a deleted one's key
    would otherwise inherit the deleted chat's saved grant after a crash. Called
    BEFORE the delete unlinks anything, with every spelling of the session's key
    the delete can resolve: ``ok`` False refuses the delete, since after the unlink
    nothing would ever come back to remove the grant. ``held`` names the keys that
    were saved, for :func:`restore_deleted_session_trust` when the delete is then
    refused or fails and the session stays. *fence* holds the keys against grants
    from before the removal until the delete is over (:func:`delete_fence`).
    """
    wanted = [k for k in dict.fromkeys(keys) if k]
    if fence is not None:
        fence.hold(wanted)
    try:
        snapshot = await asyncio.to_thread(session_trust_store.load)
        # Only a grant still in force: one a revoke withdrew but could not remove
        # from disk is not the session's to get back.
        held = tuple(k for k in wanted if snapshot.holds(k) and not is_withdrawn(k))
        removed = await persist_revoke_keys(wanted)
    except Exception:
        logger.warning("could not forget saved trust for a deleted session", exc_info=True)
        held, removed = (), False
    if not removed:
        notify_revoke_not_durable(state)
    return DeletedTrust(ok=removed, held=held, generation=revoke_generation())


async def restore_deleted_session_trust(trust: DeletedTrust) -> None:
    """Put back saved trust a delete took away and then did not commit. Never raises.

    The session still exists, so its owner's grant must survive the next crash
    again. Under the same rules as :func:`persist_grant`: never once an owner
    stop has begun, never after any later revoke (the generation moved), and the
    rewrite never carries a withdrawn grant forward. Best-effort: a re-save that
    is skipped or fails leaves the chat live-only, the re-consent direction.
    """
    if not trust.held:
        return
    try:
        async with _write_order():
            if _stopping or revoke_generation() != trust.generation:
                return
            keys = tuple(k for k in trust.held if not is_withdrawn(k))
            if not keys:
                return
            withdrawn = set(_withdrawn_keys) - set(keys)
            replace_all = _withdrawn_all
            await asyncio.to_thread(
                lambda: session_trust_store.grant_counting(
                    keys, drop=withdrawn, replace=replace_all
                )
            )
    except Exception:
        logger.warning("could not restore saved trust for a kept session", exc_info=True)


async def saved_session_keys() -> tuple[str, ...]:
    """Every session key the signed store holds. Never raises; unreadable reads as none.

    Unreadable restores nothing either, so a caller resolving which keys to
    revoke loses nothing by it.
    """
    try:
        snapshot = await asyncio.to_thread(session_trust_store.load)
    except Exception:
        return ()
    return tuple(snapshot.sessions)


async def saved_keys(keys: Iterable[str]) -> frozenset[str]:
    """Which of *keys* already hold a saved grant. Never raises; unknown reads as none.

    For a grant that is then refused: its roll-back takes back only what that
    grant added, never a grant the owner made durable earlier.
    """
    wanted = list(keys)
    try:
        snapshot = await asyncio.to_thread(session_trust_store.load)
    except Exception:
        return frozenset()
    return frozenset(k for k in wanted if snapshot.holds(k))


async def persist_revoke_all() -> bool:
    """Forget every durable grant, including chats that are not open now."""
    global _withdrawn_all
    _bump_revoke_generation()
    _withdrawn_all = True

    async def _ordered_clear() -> bool:
        async with _write_order():
            return await asyncio.to_thread(session_trust_store.clear)

    try:
        # Bounded like :func:`persist_revoke_keys`: a timeout keeps every chat
        # withheld in memory and the live off switch goes ahead.
        cleared = await asyncio.wait_for(_ordered_clear(), timeout=_REVOKE_TIMEOUT)
    except asyncio.TimeoutError:
        logger.error("clearing saved session trust timed out; withheld in memory")
        cleared = False
    if cleared:
        _withdrawn_all = False
        _withdrawn_keys.clear()
    return cleared


#: Bumped on the loop, BEFORE the store write is offloaded, by every revoke. A
#: restore verdict read off-loop is applied only when this has not moved since
#: the read began: a revoke that landed in that window -- above all the
#: all-chats switch, which reaches chats not yet rebuilt -- must win over a
#: verdict read before it.
_revoke_generation = 0


def _bump_revoke_generation() -> None:
    global _revoke_generation
    _revoke_generation += 1


def revoke_generation() -> int:
    """The current revoke generation. Loop-affine; compare before and after a hop."""
    return _revoke_generation


def clear_on_operator_stop_sync() -> bool:
    """Forget every durable grant because the owner stopped the gateway. Blocking.

    A stop the owner asked for -- ``kirocrew stop`` / ``restart``, Ctrl-C, a
    ``systemctl stop``, the SIGTERM a host reboot delivers, the owner shutdown
    route -- is the re-consent checkpoint: the next boot comes back with every
    chat untrusted, exactly as before this module existed. Only an exit the owner
    did NOT ask for (a crash, an OOM kill, the loop-stall watchdog's dump-then-exit,
    a self-initiated restart) leaves the record for the next boot to restore.

    Synchronous so the force-exit path, which cannot await, can call it too.
    True when nothing remains on disk for the next boot to hand back.
    """
    cleared = session_trust_store.clear()
    if not cleared:
        # The store refused both its rewrite and its removal. Leave a marker
        # elsewhere so the next boot restores nothing even though the record
        # survived.
        cleared = session_trust_store.mark_owner_stop()
    try:
        sel().log_api_access(
            caller="gateway:shutdown",
            operation="session_trust:cleared_on_stop",
            outcome="cleared" if cleared else "clear_failed",
            resources="session-trust",
        )
    except Exception:
        logger.warning("SEL audit failed for session trust cleared on stop", exc_info=True)
    if not cleared:
        logger.error(
            "could not clear saved session trust on an owner stop; the next boot may "
            "restore chat trust it should have asked for again"
        )
    return cleared


def _signal_stop() -> bool:
    """Mark the owner stop, then clear. The marker first, so a clear the exit cuts
    short -- often on the very I/O whose stall makes the operator signal again --
    still leaves the next boot restoring nothing."""
    if session_trust_store.mark_owner_stop():
        _stop_marked.set()
    return clear_on_operator_stop_sync()


#: Set once a signal's worker has durably written the owner-stop marker. The
#: force exit reads it -- never waits on it -- to decide whether exiting now
#: could still let the next boot restore trust this stop withdrew.
_stop_marked = threading.Event()


def owner_stop_recorded() -> bool:
    """Whether this process's stop is already durable for saved trust. Never blocks."""
    return _stop_marked.is_set()


def start_clear_on_signal() -> threading.Thread:
    """Start :func:`_signal_stop` on a daemon thread; never wait.

    Called from the first-signal callback, which runs ON the event loop and so
    must not block it. The graceful path awaits its own bounded clear right
    after, so nothing is lost by not joining here.
    """
    worker = threading.Thread(target=_signal_stop, name="session-trust-signal-clear", daemon=True)
    worker.start()
    return worker


def clear_on_force_exit() -> None:
    """Start one more best-effort :func:`_signal_stop` from the force exit; never wait.

    The force exit runs on the event loop and exits at once, so it must not
    block. Every force exit follows a first signal, whose worker wrote the
    owner-stop marker before it began clearing; this backstops a grant made
    between the two signals. A disk too stalled to take even that first marker
    leaves the record, the same accepted gap as a gateway that was already frozen
    when the stop arrived.
    """
    threading.Thread(target=_signal_stop, name="session-trust-force-clear", daemon=True).start()


#: Bound on the owner-stop clear, including its wait for the store's write order.
_SHUTDOWN_CLEAR_TIMEOUT = 2.0


async def clear_on_shutdown_async(exit_code: int) -> bool:
    """Clear durable trust when this shutdown is an owner stop (*exit_code* 0).

    The gateway exits 0 only for a stop the owner asked for; every
    self-initiated shutdown that exists to be relaunched exits non-zero, and a
    crash or the loop-stall watchdog never reaches the shutdown path at all.
    Returns True when the record was cleared, False when it was kept or the
    clear failed. Bounded so a wedged disk cannot spend the shutdown budget.
    """
    if exit_code != 0:
        return False
    global _stopping
    # Fence first, on the loop: from here no grant writes the store, so none can
    # land after the clear below and be restored by the next boot. Then clear in
    # the store's write order, so a grant already writing finishes and is cleared
    # with the rest rather than overtaking the clear.
    _stopping = True

    async def _ordered_clear() -> bool:
        async with _write_order():
            return await asyncio.to_thread(clear_on_operator_stop_sync)

    try:
        cleared = await asyncio.wait_for(_ordered_clear(), timeout=_SHUTDOWN_CLEAR_TIMEOUT)
    except Exception:
        logger.error("clearing saved session trust on shutdown failed", exc_info=True)
        cleared = False
    if not cleared:
        # The ordered clear timed out (a write held the order past the bound) or
        # failed. The owner-stop marker does not need the store's order -- it is
        # its own leaf -- so write it directly: the next boot then restores
        # nothing even though the record survived.
        try:
            cleared = await asyncio.wait_for(
                asyncio.to_thread(session_trust_store.mark_owner_stop), timeout=1.0
            )
        except Exception:
            logger.error("could not record the owner stop for session trust", exc_info=True)
            cleared = False
    if not cleared:
        # A refused in-place restart keeps serving, so its grants save again.
        _stopping = False
    return cleared


#: Set on the loop when an owner stop begins clearing. A grant after it is not
#: saved (it stays live-only for the moments the gateway has left), so nothing
#: written after the clear can survive the stop.
_stopping = False


def restore_verdict(session_key: str) -> bool:
    """Whether the chat at *session_key* gets its trust back. Call off the loop.

    True only when the store holds the key and no revoke in this process took it
    away -- the record surviving a revoke that could not remove it is not consent.
    """
    if is_withdrawn(session_key):
        return False
    # Through the same gate the boot restore uses, so a rehydration that runs
    # before the boot restore (or in a process that never ran it) still honours
    # -- and settles -- an owner-stop marker a failed clear left behind.
    snapshot = _load_for_restore()
    return snapshot.holds(session_key)


def apply_restored_trust(state: "DashboardState", slot: "_ChatSlot") -> bool:
    """Put a restored grant back on *slot*. Loop-affine: touches slot state only.

    Refuses an app-owned chat even when its key is stored: an app's trust is its
    own expiring grant, and a key reused by an app worker must not inherit the
    owner's.
    """
    if str(getattr(slot, "_app", "") or ""):
        return False
    key = effective_session_key(slot)
    slot._trust = True
    sessions: Any = getattr(state, "sessions", None)
    if sessions is not None:
        try:
            sessions.set_approval_policy(key, "auto")
        except Exception:
            # The runner writes the policy from ``slot._trust`` on its next turn,
            # so a session the map does not hold yet still ends up at "auto".
            logger.debug("could not set restored policy on %s", key, exc_info=True)
    try:
        sel().log_api_access(
            caller="dashboard:restore",
            operation="session_trust:restored",
            outcome="enabled",
            resources=slot.key,
        )
    except Exception:
        logger.warning("SEL audit failed for restored session trust", exc_info=True)
    return True


def _load_for_restore() -> session_trust_store.TrustSnapshot:
    """The record, unless the last stop was the owner's.

    A previous owner stop whose clear failed left the owner-stop marker: honour
    it by restoring nothing, retry the clear, and remember it for this process so
    a chat rebuilt later is not restored from the record that survived.
    """
    global _owner_stopped
    if session_trust_store.owner_stop_marked():
        if session_trust_store.owner_stop_marked_since(_STARTED):
            # A stop aimed at THIS run, still in progress (the CLI marks before it
            # ends the gateway from outside): restore nothing now, but neither
            # settle it -- that would erase the stop -- nor latch, since a stop
            # that is refused withdraws the marker and this gateway keeps serving.
            return session_trust_store.TrustSnapshot(sessions=(), readable=True)
        _owner_stopped = True
        # Marker removed only once the clear succeeded; otherwise it stays for
        # the next boot, so a later crash cannot restore what this stop withdrew.
        session_trust_store.settle_owner_stop(_STARTED)
    if _owner_stopped:
        return session_trust_store.TrustSnapshot(sessions=(), readable=True)
    return session_trust_store.load()


#: Set once per process when a restore found the owner-stop marker. Reset only by
#: a fresh owner grant that is saved (:func:`persist_grant`): the record that
#: survived that stop is never trusted again, and the fresh grant rewrites it.
_owner_stopped = False


async def restore_in_background(state: "DashboardState") -> int:
    """:func:`restore_persisted_trust_async` off the boot path; never raises.

    Started as a task after the boot restore, so no part of binding the listener
    waits on the store. Pushes a slots update when it restored anything.
    """
    try:
        restored = await restore_persisted_trust_async(state)
    except Exception:
        # Restoring nothing is the safe direction; the chats start untrusted.
        logger.warning("session trust restore failed", exc_info=True)
        return 0
    if restored:
        try:
            state.push_slots_update()
        except Exception:
            logger.debug("slots push after trust restore failed", exc_info=True)
    return restored


#: Strong references to in-flight background restores, so the event loop's weak
#: reference to a task cannot let one be collected mid-run.
_restore_tasks: set["asyncio.Task[int]"] = set()


def schedule_restore(state: "DashboardState") -> "asyncio.Task[int]":
    """Start :func:`restore_in_background` as a task. Loop-affine, returns at once."""
    task = asyncio.get_running_loop().create_task(
        restore_in_background(state), name="session-trust-restore"
    )
    _restore_tasks.add(task)
    task.add_done_callback(_restore_tasks.discard)
    return task


async def restore_persisted_trust_async(state: "DashboardState") -> int:
    """Re-apply every remembered grant to the chats the boot restore rebuilt.

    Returns how many chats got their trust back. A store that could not be read
    restores nothing and tells the owner so -- a silent reset is exactly the
    failure this module exists to end.
    """
    # A revoke that lands while the record is read makes that snapshot stale, so
    # it is read again rather than abandoned: one chat's revoke must not quietly
    # un-restore every other chat. Bounded; a store revoked on every read
    # restores nothing, the safe direction.
    for _attempt in range(3):
        _gen = revoke_generation()
        snapshot = await asyncio.to_thread(_load_for_restore)
        if revoke_generation() == _gen:
            break
    else:
        return 0
    if not snapshot.readable:
        _notify_reset(
            state,
            "Saved chat trust could not be read",
            "The record of which chats you trusted could not be read after the "
            "restart, so every chat starts untrusted. Trust them again from the "
            "chat's approval menu.",
        )
        return 0
    if not snapshot.sessions:
        return 0
    restored = 0
    for slot in list(state._slots.values()):
        key = effective_session_key(slot)
        if snapshot.holds(key) and not is_withdrawn(key) and apply_restored_trust(state, slot):
            restored += 1
    if restored:
        logger.info("Restored session trust on %d chat(s)", restored)
    return restored


def _notify_reset(state: "DashboardState", title: str, body: str) -> None:
    try:
        state.notify("agent", title, body)
    except Exception:
        logger.warning("could not deliver the session-trust reset notice", exc_info=True)
