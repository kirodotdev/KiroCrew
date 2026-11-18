"""Discarding a Bot chat's session with its allocation ending fence held.

``bot_chat_app_tools.stop_app_bot_chat_servers`` stops a Bot chat that mounts a
withdrawn app's servers. A discard alone cannot tell "nothing runs" from "a start
is inside ``provider.start()``, recorded as mounted but not registered yet": both
find no registered session. The ending fence of the session allocator
(``SessionAllocationService.begin_ending`` / ``spawn_in_flight`` / ``end_ending``)
is what can, so the discard runs inside it.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger("kiro_crew.bot_chat_app_tools")


async def discard_with_ending_fence(
    sessions: Any, session_key: str, unconfirmed_out: list | None
) -> tuple[bool, str | None]:
    """Discard *session_key* with its ending fence held throughout.

    Returns ``(confirmed, in_flight)``. The fence invalidates any start already in
    flight and holds new ones at the door while the discard runs; ``spawn_in_flight``
    is read before it lifts, so a start inside ``provider.start()`` or one refused
    during the discard is named. A named start, or a fence that could not be raised,
    means the stop is not confirmed: no registered session is not evidence that
    nothing runs. Any other new native session for the chat being created or
    loaded (an in-place swap, an MCP re-injection: :func:`recorded_session_start`)
    is named the same way: it may already carry the app's servers, so the stop is
    not confirmed, and the session it produces is kept for the close retry. A
    start asked for while the stop runs is refused.

    One stop per chat at a time: a second stop of the same chat (another app the
    chat also mounts) does not discard on its own, where it would find no session
    and read that as a confirmed close, and lift the first stop's fences early. It
    waits for the running stop and takes its answer, and the unconfirmed handles
    that stop recorded. Only the stop that raised the fences lowers them.
    """
    folded = fold_session_key(sessions, session_key)
    running = _STOPPING.get(folded)
    if running is not None:
        confirmed, in_flight, handles = await asyncio.shield(running)
        if unconfirmed_out is not None:
            unconfirmed_out.extend(handles)
        return confirmed, in_flight
    outcome: asyncio.Future = asyncio.get_running_loop().create_future()
    _STOPPING[folded] = outcome
    answer: tuple[bool, str | None, list] = (False, "the stop it shared did not finish", [])
    try:
        answer = await _discard_as_owner(sessions, session_key, folded)
    finally:
        if _STOPPING.get(folded) is outcome:
            del _STOPPING[folded]
        outcome.set_result(answer)
    confirmed, in_flight, handles = answer
    if unconfirmed_out is not None:
        unconfirmed_out.extend(handles)
    return confirmed, in_flight


async def _discard_as_owner(
    sessions: Any, session_key: str, folded: str
) -> tuple[bool, str | None, list]:
    """The stop :func:`discard_with_ending_fence` runs for the first caller on a chat."""
    try:
        alloc = sessions._allocation_boundary()
        alloc.begin_ending(session_key)
    except Exception:  # noqa: BLE001
        logger.warning(
            "bot chat app tools: could not raise the ending fence for %s",
            session_key,
            exc_info=True,
        )
        return False, "ending fence unavailable", []
    handles: list = []
    try:
        kwargs: dict[str, Any] = {
            "replay": False,
            "confirm_teardown": True,
            "unconfirmed_handle_out": handles,
        }
        confirmed = await sessions.discard_conversation(session_key, **kwargs)
        in_flight = alloc.spawn_in_flight(session_key)
        if folded in _STARTING:
            _START_OUTRUN.add(folded)
            start = "a new native session for the chat being created or loaded"
            in_flight = f"{in_flight}; {start}" if in_flight else start
    finally:
        alloc.end_ending(session_key)
    return bool(confirmed), in_flight if isinstance(in_flight, str) and in_flight else None, handles


#: Session keys with a native session being created or loaded for them now
#: (:func:`recorded_session_start`), by count: the one record every way a chat
#: gets a new native session goes through.
_STARTING: dict[str, int] = {}
#: Folded keys a Bot-chat stop is ending now, each with the answer it will give
#: (:func:`discard_with_ending_fence`): a second stop of the chat awaits it.
_STOPPING: dict[str, asyncio.Future] = {}
#: Keys whose in-flight start a stop ran beside: the session it produces is kept
#: as an unconfirmed close.
_START_OUTRUN: set[str] = set()


class BotChatStopping(RuntimeError):
    """A native session refused to start for a chat a Bot-chat stop is ending."""


def fold_session_key(sessions: Any, key: str) -> str:
    """*key* as *sessions* folds it, so a stop and a start name the same chat."""
    fold = getattr(sessions, "_fold_key", None)
    return fold(key) if callable(fold) else key


def start_in_flight(session_key: str) -> bool:
    """Whether a native session for *session_key* is being created or loaded now."""
    return session_key in _STARTING


def recorded_session_start(
    key_of: Callable[[Any, dict], str], keep_unconfirmed: Callable[[Any, str, Any], None]
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Wrap a process owner's ONE method that creates or loads a native session.

    While it runs, its chat's key (``key_of(self, kwargs)``) is recorded as a start
    in flight, which a Bot-chat stop reads (:func:`discard_with_ending_fence`) and
    answers unconfirmed. A start asked for while a stop of the chat runs is refused
    with :class:`BotChatStopping`. A start a stop ran beside keeps what it produced
    as an unconfirmed close (``keep_unconfirmed(self, key, result)``), which the
    pending-close retry ends. A start with no key is not a Bot chat's and runs as is.
    """

    def wrap(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(fn)
        async def run(self: Any, *args: Any, **kwargs: Any) -> Any:
            key = key_of(self, kwargs) or ""
            if not key:
                return await fn(self, *args, **kwargs)
            if key in _STOPPING:
                raise BotChatStopping(f"a Bot-chat stop is ending {key!r}; no session starts now")
            _STARTING[key] = _STARTING.get(key, 0) + 1
            try:
                result = await fn(self, *args, **kwargs)
            finally:
                left = _STARTING.pop(key) - 1
                if left:
                    _STARTING[key] = left
                outrun = key in _START_OUTRUN
                if not left:
                    _START_OUTRUN.discard(key)
            if outrun:
                try:
                    keep_unconfirmed(self, key, result)
                except Exception:  # noqa: BLE001 - the stop already reports unconfirmed
                    logger.warning("bot chat app tools: could not keep %s's new session", key)
            return result

        run.__bot_chat_start_recorded__ = True  # type: ignore[attr-defined]
        # Name the wrapper's code after the method, as an unwrapped one reads (a
        # coroutine's and a task's repr print it; the runtime bindings check it).
        code = run.__code__  # type: ignore[attr-defined]
        if hasattr(code, "co_qualname"):
            run.__code__ = code.replace(  # type: ignore[attr-defined]
                co_name=fn.__name__, co_qualname=fn.__code__.co_qualname  # type: ignore[attr-defined]
            )
        return run

    return wrap


def _keep_runtime_session(runtime: Any, key: str, handle: Any) -> None:
    from kiro_crew.bot_chat_app_tools import note_unconfirmed_close

    note_unconfirmed_close(key, runtime, getattr(handle, "session_id", None))


def _keep_client_session(client: Any, key: str, _result: Any) -> None:
    note_unobserved_kill(key, getattr(client, "_session_id", None) or f"unconfirmed-start:{key}")


#: The record for an ``AcpRuntime`` method (``create_session``, ``load_session``).
runtime_session_start = recorded_session_start(
    lambda _rt, kwargs: kwargs.get("session_key", ""), _keep_runtime_session
)
#: The record for ``AcpClient._initialize_session``, which runs ``session/new`` or
#: ``session/load`` on the client's own process.
client_session_start = recorded_session_start(
    lambda client, _kwargs: getattr(client, "_session_key", "") or "", _keep_client_session
)


class _UnobservedKill:
    """The retry handle of a hard kill nothing reads back: it never confirms.

    The close stays pending, so the Bot's stop keeps reporting incomplete, until the
    gateway restarts and every chat starts on a freshly built spec.
    """

    async def evict_session(self, session_id: str) -> bool:
        return False


def note_unobserved_kill(session_key: str, token: str) -> None:
    """Keep a Bot chat's close unconfirmed after a hard kill whose outcome is unknown."""
    from kiro_crew.bot_chat_app_tools import note_unconfirmed_close

    note_unconfirmed_close(session_key, _UnobservedKill(), token)


async def stop_tracked_bot_chats(state: Any, app_name: str) -> list[str] | None:
    """Stop the Bot chats the tracking tables tie to *app_name*, from those tables alone.

    For a lifecycle operation that finds no installed record for the app: an app
    that deleted its own ``installed.json`` is still mounted, pending delivery or
    pending close in ``bot_chat_app_tools``, and its Bot chats must stop all the
    same. Returns ``None`` when nothing is tracked for the app, else the slugs (or
    session keys) whose stop is not confirmed; an empty list means every one
    stopped. Runs inside the app's stop fence.
    """
    from kiro_crew import bot_chat_app_tools as tools

    if not tools.app_has_active_bot_sessions(app_name):
        return None
    async with tools.app_stop_fence(app_name):
        tools.revoke_app_generation(app_name)
        try:
            _stopped, failed = await tools.stop_app_bot_chat_servers(state, app_name)
        except Exception:  # noqa: BLE001 - reported as incomplete, never swallowed
            logger.warning("stopping tracked Bot chats for %r failed", app_name, exc_info=True)
            failed = ["unknown"]
    return list(failed)
