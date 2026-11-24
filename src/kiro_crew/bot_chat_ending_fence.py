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
import contextvars
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


#: The chat key of the recorded start (:func:`recorded_session_start`) the current
#: task runs in, or was created by.
_START_KEY: contextvars.ContextVar[str] = contextvars.ContextVar("bot_chat_start_key", default="")


def _release_start(key: str) -> None:
    """Take one start of *key* off :data:`_STARTING`; the last one forgets the outrun."""
    left = _STARTING.pop(key, 1) - 1
    if left > 0:
        _STARTING[key] = left
    else:
        _START_OUTRUN.discard(key)


def hold_start(session_key: str) -> Callable[[], None]:
    """Count a start of *session_key* as still in flight until the returned release
    runs (once): a ``session/new`` that outlived its caller is still a start while
    its collector waits for the answer, so a stop beside it is not confirmed and the
    chat's app snapshot is kept for the session it may still produce."""
    if not session_key:
        return lambda: None
    _STARTING[session_key] = _STARTING.get(session_key, 0) + 1
    done = False

    def release() -> None:
        nonlocal done
        if not done:
            done = True
            _release_start(session_key)

    return release


def current_start_key() -> str:
    """The chat key of the recorded start the current task runs in, or ``""``."""
    return _START_KEY.get()


def abandoned_start(runtime: Any, attempt: str) -> Callable[..., None]:
    """For a request of the current start whose caller left while it may still
    produce a session (a cancelled or timed-out ``session/new`` or
    ``session/load``): hold the chat's start, and put the session on the record
    under this request's own token (:func:`hold_start_record`). The returned
    ``finish(session_id="", closed=None)`` runs once, when the request's own
    outcome is known (:func:`settle_start_record`): a confirmed close, a dead
    process tree or a host refusal settles it; anything else stays kept. Then the
    start is released."""
    key = _START_KEY.get()
    release = hold_start(key)
    token = hold_start_record(runtime, key, attempt) if key else ""
    done = False

    def finish(session_id: str = "", closed: bool | None = None) -> None:
        nonlocal done
        if done:
            return
        done = True
        try:
            if token:
                settle_start_record(runtime, key, token, session_id, closed)
        finally:
            release()

    return finish


def note_start_teardown(runtime: Any, session_id: str) -> None:
    """A teardown of *session_id* on *runtime* just ran (``terminate_session``,
    ``evict_session``). Inside a recorded start, the session it ends is that start's
    own and no handle reached a caller, so an unconfirmed close is kept against the
    chat (:func:`keep_unsettled_start`). Elsewhere a caller holds the handle."""
    key = _START_KEY.get()
    if not key or not session_id:
        return
    # A close retry the start scheduled inherits the key: what it retries is kept.
    if close_pending(runtime, session_id):
        return
    keep_unsettled_start(runtime, key, session_id)


def keep_unsettled_start(
    runtime: Any,
    session_key: str,
    session_id: str,
    *,
    closed: bool | None = None,
    snapshot: tuple | None = None,
) -> None:
    """Keep a native session *runtime* may still run for *session_key*, which a start
    that never handed it to a caller left behind, until evidence settles it.

    *session_id* is the native id, or ``""`` when the host never answered (the
    session may exist under an id nobody holds). *closed* False means no teardown
    ran; None reads the runtime's own answer (``_unconfirmed_teardowns``). A close
    the host confirmed settles it, except on a host whose close leaves the servers
    running (codex), or with no id: there only the process tree's death does
    (``RuntimeTreeGone``). The session is put on the chat's record with its app
    snapshot, so a later stop hands it to the close retry; when a stop already ran
    beside the start it is handed to the retry now.
    """
    from kiro_crew import bot_chat_app_tools as tools
    from kiro_crew.agent_sdk.close_handles import local_close_token

    handle = _unsettled_handle(runtime, session_id, closed)
    if handle is None:
        return  # the host answered the close, or the process tree is gone
    token = session_id or local_close_token("unconfirmed-start:", runtime, session_key)
    # On the chat's record under *token*, with the app snapshot this start took
    # (:func:`capture_start_snapshot`): the one it carries, else the one it is
    # already bound to, else its own, else the chat's pending one, which stays put.
    # A later start of the chat writes its own pending snapshot, never this start's.
    snap = (
        snapshot
        or tools._SESSION_APP_SNAPSHOT.get(token)
        or _own_snapshot(session_key)
        or tools._SESSION_APP_SNAPSHOT_PENDING.get(session_key)
    )
    if snap is None or not snap[1]:
        return  # not a Bot chat with app servers
    tools._SESSION_APP_SNAPSHOT[token] = snap
    tools._SESSION_RUNTIMES[token] = handle
    if session_key in _START_OUTRUN:
        tools.note_unconfirmed_close(session_key, handle, token)


#: ``(chat key, app snapshot)`` of the recorded start the current task runs in, or
#: was created by, as it stood when its request went out (:func:`capture_start_snapshot`).
_START_SNAP: contextvars.ContextVar[tuple | None] = contextvars.ContextVar(
    "bot_chat_start_snapshot", default=None
)


def capture_start_snapshot(session_key: str) -> None:
    """Take this start's own app snapshot of *session_key* (``record_mounted``'s pending
    entry) just before its ``session/new`` or ``session/load`` goes out. Everything
    this start leaves behind -- its collector, its teardowns -- is kept with it,
    whatever a later start of the same chat records."""
    from kiro_crew import bot_chat_app_tools as tools

    if session_key and _START_KEY.get() == session_key:
        _START_SNAP.set((session_key, tools._SESSION_APP_SNAPSHOT_PENDING.get(session_key)))


def _own_snapshot(session_key: str) -> tuple | None:
    held = _START_SNAP.get()
    return held[1] if held is not None and held[0] == session_key else None


def hold_start_record(runtime: Any, session_key: str, attempt: str) -> str:
    """Put the session a timed-out start (*attempt*) may still produce on the chat's
    record, under a token naming that start, with the snapshot it took: a stop while
    the collector waits sees the app and hands the token to the close retry. Returns
    the token, or ``""`` when the start mounts no app server."""
    from kiro_crew import bot_chat_app_tools as tools
    from kiro_crew.agent_sdk.close_handles import RuntimeTreeGone, local_close_token

    snap = _own_snapshot(session_key)
    if snap is None or not snap[1]:
        return ""
    token = local_close_token("unconfirmed-start:", runtime, session_key, attempt)
    tools._SESSION_APP_SNAPSHOT[token] = snap
    tools._SESSION_RUNTIMES[token] = RuntimeTreeGone(runtime)
    return token


def settle_start_record(
    runtime: Any, session_key: str, token: str, session_id: str, closed: bool | None
) -> None:
    """Settle the record :func:`hold_start_record` kept once its start's collector
    did. *session_id* is what the host answered (``""`` for none); *closed* is as in
    :func:`keep_unsettled_start`, and True when the host refused the start, so no
    session exists. Only entries under this start's own token move: a close retry a
    stop handed it to takes the real id, or is dropped when the host's answer
    settles it; with no answer the token keeps its tree handle."""
    from kiro_crew import bot_chat_app_tools as tools

    snap = tools._SESSION_APP_SNAPSHOT.pop(token, None)
    tree = tools._SESSION_RUNTIMES.pop(token, None)
    handed = [held for held in tools._PENDING_CLOSE.values() if any(s == token for _h, s in held)]
    if not handed:
        # Still on the record only: the start's own answer decides what stays.
        if snap is None:
            return
        if session_id:
            keep_unsettled_start(runtime, session_key, session_id, closed=closed, snapshot=snap)
        elif closed is not True:
            # No answer: the session may run under an id nobody holds.
            tools._SESSION_APP_SNAPSHOT[token] = snap
            tools._SESSION_RUNTIMES[token] = tree
            if session_key in _START_OUTRUN:
                tools.note_unconfirmed_close(session_key, tree, token)
        return
    if not session_id and closed is not True:
        return  # no answer: the retry keeps the token on its tree handle
    # The retry holds this start's close: its own answer replaces the token, or
    # drops it when the host settled it (a confirmed close, a refused start).
    handle = _unsettled_handle(runtime, session_id, closed) if session_id else None
    for held in handed:
        held[:] = [(h, s) for h, s in held if s != token]
        if handle is not None and (handle, session_id) not in held:
            held.append((handle, session_id))


def _unsettled_handle(runtime: Any, session_id: str, closed: bool | None) -> Any:
    """The retry handle a session *runtime* may still run is kept under, or None when
    evidence already settles it (see :func:`keep_unsettled_start`)."""
    from kiro_crew.agent_sdk.close_handles import RuntimeTreeGone

    leaves = runtime_close_leaves_servers(runtime)
    if session_id and not leaves and closed is not False:
        dead = getattr(runtime, "_dead", False)
        if dead and getattr(runtime, "process_tree_confirmed_dead", False) is True:
            return None  # the process and its whole tree are gone
        if not dead and session_id not in getattr(runtime, "_unconfirmed_teardowns", ()):
            return None  # the host answered the close
    return RuntimeTreeGone(runtime) if leaves or not session_id else runtime


def start_outran(session_key: str) -> bool:
    """Whether a Bot-chat stop ran beside a start of *session_key* still in flight."""
    return session_key in _START_OUTRUN


def close_pending(runtime: Any, session_id: str) -> bool:
    """Whether a Bot chat's close of native *session_id* on *runtime* waits on its
    retry (``bot_chat_app_tools._PENDING_CLOSE``). A load of that id there is refused
    while it does: the retry would close the session the load restored. A handle
    with no process owner (``_UnobservedKill``) names no runtime, so it counts too."""
    from kiro_crew import bot_chat_app_tools as tools
    from kiro_crew.agent_sdk.close_handles import owner

    return any(
        s == session_id and (owner(h) is runtime or isinstance(h, _UnobservedKill))
        for held in tools._PENDING_CLOSE.values()
        for h, s in held
    )


def recorded_session_start(
    key_of: Callable[[Any, dict], str], keep_unconfirmed: Callable[[Any, str, Any], None]
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Wrap a process owner's ONE method that creates or loads a native session.

    While it runs, its chat's key (``key_of(self, kwargs)``) is recorded as a start
    in flight, which a Bot-chat stop reads (:func:`discard_with_ending_fence`) and
    answers unconfirmed. A start asked for while a stop of the chat runs is refused
    with :class:`BotChatStopping`. A start a stop ran beside keeps what it produced
    as an unconfirmed close (``keep_unconfirmed(self, key, result)``), which the
    pending-close retry ends. A start that raised is kept the same way
    (``keep_unconfirmed(self, key, error)``, with what it raised) only when a stop ran beside it: a failure
    with no stop is the caller's to clean up (a retry on the same process, or the
    start's own cleanup kill). A start with no key is not a Bot chat's and runs as is.
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
            # Every task this start creates (a timed-out start's collector, a
            # scheduled close) inherits the key, so a teardown it leaves
            # unconfirmed is kept against the chat (:func:`note_start_teardown`).
            ctx_token = _START_KEY.set(key)
            snap_token = _START_SNAP.set(None)
            failed = True
            error: BaseException | None = None
            try:
                result = await fn(self, *args, **kwargs)
                failed = False
            except BaseException as exc:
                error = exc
                raise
            finally:
                _START_SNAP.reset(snap_token)
                _START_KEY.reset(ctx_token)
                outrun = key in _START_OUTRUN
                _release_start(key)
                if failed and outrun:
                    try:
                        keep_unconfirmed(self, key, error)
                    except Exception:  # noqa: BLE001 - the start's own error propagates
                        logger.warning("bot chat app tools: could not keep %s's failed start", key)
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

    sid = getattr(handle, "session_id", None)
    if runtime_close_leaves_servers(runtime):
        # Codex's close leaves the session's servers running: only the tree's death
        # settles it, so the start is kept under a tree handle whatever its id.
        from kiro_crew.agent_sdk.close_handles import RuntimeTreeGone

        note_unconfirmed_close(key, RuntimeTreeGone(runtime), sid)
        return
    if not sid:
        # A failed start with no native id on a host whose close ends the session's
        # servers. A host's own refusal left nothing, and neither did a failure this
        # side made before the host was asked. A timeout's late answer is its
        # collector's to keep (``StartCollector._keep_unsettled``), and a session a
        # teardown ended was kept by it (:func:`note_start_teardown`). An answer lost
        # on the way back (``answer_lost``: a frame too large, the runtime's death)
        # may have left a session nobody can name: kept under the runtime's tree.
        from kiro_crew.agent_sdk.close_handles import RuntimeTreeGone, answer_lost

        if answer_lost(handle):
            note_unconfirmed_close(key, RuntimeTreeGone(runtime), None)
        return
    note_unconfirmed_close(key, runtime, sid)


def runtime_close_leaves_servers(runtime: Any) -> bool:
    """Whether a ``session/close`` on *runtime* leaves the session's MCP servers
    running (``AcpSessionProvider.close_leaves_mcp_servers``: codex)."""
    from kiro_crew.acp_backends import ACP_BACKEND_CODEX

    return getattr(runtime, "acp_backend", None) == ACP_BACKEND_CODEX


def _keep_client_session(client: Any, key: str, _result: Any) -> None:
    # The client is its own process owner: its close is confirmed by its tree's death
    # (``RuntimeTreeGone``), which the provider's shutdown records on it.
    from kiro_crew.agent_sdk.close_handles import local_close_token

    token = getattr(client, "_session_id", None) or local_close_token(
        "unconfirmed-start:", client, key
    )
    note_unobserved_kill(key, token, owner=client)


def _runtime_start_key(runtime: Any, kwargs: dict) -> str:
    from kiro_crew.bot_chat_app_tools import carries_app_tools

    key = kwargs.get("session_key", "") or ""
    agent = kwargs.get("agent") or getattr(runtime, "_agent", None)
    return key if carries_app_tools(agent, key, getattr(runtime, "acp_backend", None)) else ""


def _client_start_key(client: Any, _kwargs: dict) -> str:
    from kiro_crew.bot_chat_app_tools import carries_app_tools

    key = getattr(client, "_session_key", "") or ""
    agent = getattr(client, "_agent", None)
    return key if carries_app_tools(agent, key, getattr(client, "_acp_backend", None)) else ""


#: The record for an ``AcpRuntime`` method (``create_session``, ``load_session``).
#: Only a Bot's own chat on a host that carries its app servers is recorded
#: (``carries_app_tools``, the test its mount uses); every other start runs as is.
runtime_session_start = recorded_session_start(_runtime_start_key, _keep_runtime_session)
#: The record for ``AcpClient._initialize_session``, which runs ``session/new`` or
#: ``session/load`` on the client's own process.
client_session_start = recorded_session_start(_client_start_key, _keep_client_session)


#: Native session ids whose close some teardown confirmed (``teardown_confirmed``),
#: newest last and bounded: the evidence an :class:`_UnobservedKill` waits for.
_CLOSED_SIDS: dict[str, None] = {}
_CLOSED_SIDS_CAP = 4096


def note_closed_session(session_id: str) -> None:
    """Record that *session_id*'s close was confirmed, for :class:`_UnobservedKill`."""
    _CLOSED_SIDS.pop(session_id, None)
    _CLOSED_SIDS[session_id] = None
    while len(_CLOSED_SIDS) > _CLOSED_SIDS_CAP:
        _CLOSED_SIDS.pop(next(iter(_CLOSED_SIDS)))


class _UnobservedKill:
    """The retry handle of a native session whose process owner is not on record.

    It never asks a host. It confirms once some teardown confirmed that session's
    close (:func:`note_closed_session`, from ``teardown_confirmed``); until then the
    close stays pending and the Bot's stop reports incomplete.
    """

    local_close_handle = True

    async def evict_session(self, session_id: str) -> bool:
        return session_id in _CLOSED_SIDS


def note_unobserved_kill(session_key: str, token: str, *, owner: Any = None) -> None:
    """Keep a Bot chat's close unconfirmed after a kill whose outcome was not read.

    With the process *owner* (a runtime or a client), the handle confirms on its
    tree's death (``RuntimeTreeGone``); without one, on a confirmed close of *token*.
    """
    from kiro_crew.agent_sdk.close_handles import RuntimeTreeGone
    from kiro_crew.bot_chat_app_tools import note_unconfirmed_close

    handle = RuntimeTreeGone(owner) if owner is not None else _UnobservedKill()
    note_unconfirmed_close(session_key, handle, token)


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
