"""The write fence that keeps a natively stopped Slack turn silent.

When a user presses Slack's native Stop, the turn they stopped must post
nothing further: no partial answer sealed into the thread, no timing footer, no
"Working..." status flipped back on by a tool event that was already in flight.
The turn does not know it was stopped by Slack -- it only sees a cooperative
cancel, the same one the dashboard Stop sends -- so the fence sits where every
Slack write already goes through, the client.

Each Slack turn runs inside a :class:`TurnScope` (:func:`fenced_turn` wraps the
transport entry point; the native one calls :func:`enter_turn`). The scope rides a context variable, so every task the
turn spawns inherits it, and :meth:`RealSlackClient` asks :func:`turn_muted`
before each write. A Stop marks the live scopes of the thread it addresses as
stopped (:func:`mute_turns`); from then on those turns' writes are dropped
quietly. A NEW turn opens a new scope, so the next message in the thread is
answered normally -- nothing has to "resume" the thread.

Nothing here ever mutes a write unless a native Stop marked a scope, and only
the agent-sessions runtime does that, so with the feature off the fence is
inert.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import weakref
from collections.abc import Callable, Coroutine
from contextvars import ContextVar
from typing import Any, ParamSpec, TypeAlias, TypeVar

logger = logging.getLogger(__name__)

_P = ParamSpec("_P")
_R = TypeVar("_R")


class TurnScope:
    """One Slack turn: where it replies, and whether a native Stop silenced it."""

    __slots__ = ("channel", "thread_ts", "holds", "ended", "messages", "__weakref__")

    def __init__(self, channel: str, thread_ts: str) -> None:
        self.channel = channel
        #: The thread the turn replies in (``thread_ts or msg_ts``), the one Slack's
        #: agent session -- and so its Stop button -- is scoped to.
        self.thread_ts = thread_ts
        #: Stops that silenced this turn and have not decided yet.
        self.holds = 0
        #: A Stop decided it ended this turn: silenced for good, whatever any other
        #: overlapping Stop then decides.
        self.ended = False
        #: The ts of every message and stream this turn's scope posted, so an edit or
        #: stream call that names only a message ts is fenced by WHICH message it is.
        self.messages: set[str] = set()

    @property
    def stopped(self) -> bool:
        """Whether this turn's writes are dropped: ended, or held by a Stop."""
        return self.ended or self.holds > 0


_CURRENT: ContextVar[TurnScope | None] = ContextVar("slack_turn_scope", default=None)

#: The scopes of turns still running. Weak, so a scope lives exactly as long as the
#: turn (and any task it spawned) still holds it; nothing here has to be pruned.
_LIVE: weakref.WeakSet[TurnScope] = weakref.WeakSet()

#: What :func:`enter_turn` hands back for :func:`exit_turn` to restore.
_Reset: TypeAlias = "contextvars.Token[TurnScope | None]"

#: Bound on the message ts a scope remembers: a turn posts a handful, a long tool run
#: a few dozen task-card streams at most.
_MAX_MESSAGES = 512


def note_message(channel: str, ts: str | None) -> None:
    """Record that the current turn posted message *ts* in *channel*."""
    scope = _CURRENT.get()
    if scope is None or not ts or channel != scope.channel:
        return
    if len(scope.messages) < _MAX_MESSAGES:
        scope.messages.add(ts)


def turn_muted(channel: str, thread_ts: str | None = None, *, ts: str | None = None) -> bool:
    """Whether a Slack write is one a native Stop silenced.

    Only writes to the stopped turn's OWN conversation are dropped:
    * a post naming a thread -- the turn's reply thread;
    * an edit or stream call naming a message *ts* -- a message the turn posted
      (or the thread root it replies under);
    * a post naming neither -- a top-level post in the turn's channel.
    A long-lived task the turn happened to start keeps writing anywhere else, so a
    watcher or a completion notice for another thread or message is never silenced
    by an unrelated Stop. Each dropped write is logged at debug, so a missing
    message is traceable to the Stop that silenced it.
    """
    scope = _CURRENT.get()
    if scope is None or not scope.stopped or channel != scope.channel:
        return False
    if ts is not None:
        if ts not in scope.messages and ts != scope.thread_ts:
            return False
    elif thread_ts and thread_ts != scope.thread_ts:
        return False
    logger.debug(
        "Slack write to %s/%s dropped: the turn in %s/%s was stopped",
        channel,
        ts or thread_ts or "-",
        scope.channel,
        scope.thread_ts,
    )
    return True


def current_scope() -> TurnScope | None:
    """The scope of the Slack turn the caller runs in, if any."""
    return _CURRENT.get()


def is_running(scope: TurnScope | None) -> bool:
    """Whether *scope*'s turn is still running and was not stopped."""
    return scope is not None and not scope.stopped and scope in _LIVE


def _arg(args: tuple[Any, ...], kwargs: dict[str, Any], index: int, name: str) -> Any:
    if name in kwargs:
        return kwargs[name]
    return args[index] if len(args) > index else None


def enter_turn(channel: str, thread_ts: str) -> _Reset:
    """Open a :class:`TurnScope` for the rest of the caller's task.

    Pair it with :func:`exit_turn` in the entry point's last ``finally``: the native
    path opens it in ``_run_turn`` and ``handle_message`` closes it after delivery.
    A scope whose exit is skipped stays current for the rest of that task and stops
    counting as running when the task ends.
    """
    scope = TurnScope(channel, thread_ts)
    _LIVE.add(scope)
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    if task is not None:
        ref = weakref.ref(scope)
        task.add_done_callback(lambda _t: _LIVE.discard(s) if (s := ref()) is not None else None)
    return _CURRENT.set(scope)


def exit_turn(reset: _Reset) -> None:
    """Close the scope :func:`enter_turn` opened; tolerant of a second call."""
    scope = _CURRENT.get()
    if scope is not None:
        _LIVE.discard(scope)
    try:
        _CURRENT.reset(reset)
    except (RuntimeError, ValueError):
        # Already reset, or a nested scope was left open by an early return: the
        # current scope is out of _LIVE either way.
        pass


def fenced_turn(
    fn: Callable[_P, Coroutine[Any, Any, _R]],
) -> Callable[_P, Coroutine[Any, Any, _R]]:
    """Run a Slack turn entry point inside its own :class:`TurnScope`.

    For ``handle_message_transport``, whose leading positional shape is
    ``(slack, sessions, channel, text, thread_ts, msg_ts)``.
    """

    @functools.wraps(fn)
    async def _wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        channel = str(_arg(args, kwargs, 2, "channel") or "")
        thread = _arg(args, kwargs, 4, "thread_ts") or _arg(args, kwargs, 5, "msg_ts") or ""
        reset = enter_turn(channel, str(thread))
        try:
            return await fn(*args, **kwargs)
        finally:
            exit_turn(reset)

    return _wrapper


def mute_turns(channel: str, thread_ts: str | None) -> list[TurnScope]:
    """Silence the running turns a Stop addresses; returns the scopes it marked.

    *thread_ts* ``None`` addresses every running turn in *channel*: a single-session
    DM is one conversation whichever thread a turn replies in. Each returned scope is
    held until this Stop passes it to :func:`release`; a scope an earlier Stop
    already ended is not returned. A turn another Stop still holds is held again,
    so each Stop decides for itself and none can lift another's hold.
    """
    marked = [
        scope
        for scope in list(_LIVE)
        if not scope.ended
        and scope.channel == channel
        and (thread_ts is None or scope.thread_ts == thread_ts)
    ]
    for scope in marked:
        scope.holds += 1
    return marked


def release(scopes: list[TurnScope], *, ended: bool) -> None:
    """Give back a Stop's holds from :func:`mute_turns` once it has decided.

    *ended* is a Stop that ended the turn: it stays silenced for good. Otherwise
    the turn is audible again once no other Stop still holds it.
    """
    for scope in scopes:
        scope.holds = max(0, scope.holds - 1)
        if ended:
            scope.ended = True


def unmute(scopes: list[TurnScope]) -> None:
    """Release a Stop's holds on turns it turned out not to end."""
    release(scopes, ended=False)
