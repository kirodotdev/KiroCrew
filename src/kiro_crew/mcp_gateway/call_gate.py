"""One ``tools/call`` in flight at a time on a backend whose tools change state.

Some servers keep state the whole process shares and change it with awaits in
between: server-memory loads its graph, changes it and saves it, so two calls in
flight both start from the old graph and the last save wins (20 parallel writes
kept 1 entity). Serializing the calls keeps every write, and costs only the
queue wait. A browser server needs the same, because each call acts on the
"current" tab, which another caller's call could move.

Which calls take turns is read from the tool list (:func:`serialized_tools`),
never from a server name: a browser with a tab per caller serializes every
call, and a store whose declared writers name no target serializes those
writers. Everything else runs concurrently. Kiro Crew's own servers read the
caller block and are never gated.

The gate never awaits on the stub's read path: :meth:`CallGate.submit` queues
and returns, so a stub's own ``notifications/cancelled`` still gets through.
Every cancel path (the chat's own cancel, a Stop, a disconnect) goes through
:meth:`CallGate.cancel`: a call not yet written never reaches the server; one
already written keeps the slot until its reply, or :data:`CANCEL_GRACE_SECS`
after its cancel, since a server answers nothing to a call it cancelled.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("kiro_crew.mcp_gateway.backend")

#: How long a cancelled in-flight call keeps the slot before the next call runs.
CANCEL_GRACE_SECS = 5.0

#: Sends one queued frame; returns False when it sent nothing (the call was
#: cancelled first, or answered by the gateway), which frees the slot.
Writer = Callable[[str, dict[str, Any]], Awaitable[bool]]

DROPPED = "dropped"
UNSENT = "unsent"
SENT = "sent"
NOT_GATED = "not_gated"


#: Top-level arguments that name WHERE a call acts (a file, a repository, a
#: page). A declared writer that takes one acts on what the caller named, so two
#: callers' writes do not race; one with none writes the server's own store.
LOCATOR_KEYS = frozenset(
    {
        "path",
        "paths",
        "file",
        "file_path",
        "filepath",
        "filename",
        "source",
        "destination",
        "directory",
        "dir",
        "repo_path",
        "cwd",
        "url",
        "uri",
    }
)


def _declared_writer(tool: dict[str, Any]) -> bool:
    ann = tool.get("annotations")
    return isinstance(ann, dict) and ann.get("readOnlyHint") is False


def _names_its_target(tool: dict[str, Any]) -> bool:
    schema = tool.get("inputSchema")
    props = schema.get("properties") if isinstance(schema, dict) else None
    return isinstance(props, dict) and any(
        isinstance(k, str) and k.lower() in LOCATOR_KEYS for k in props
    )


def serialized_tools(tools: Any, *, whole_server: bool = False) -> frozenset[str]:
    """The tools whose calls take turns on a shared backend; empty means none do.

    *whole_server* (a browser with a tab per caller): every call, readers too,
    since each acts on the current tab. Otherwise a server's declared writers
    (``readOnlyHint: false``) take turns only when one of them names no target
    (no path, repository or URL argument): those write one store the process
    holds (server-memory loads, changes and saves its whole graph), and two at
    once lose a write. Readers never wait, and a server that declares nothing
    is treated as plain request and response.
    """
    listed = (
        [t for t in tools if isinstance(t, dict) and isinstance(t.get("name"), str)]
        if isinstance(tools, list)
        else []
    )
    if whole_server:
        return frozenset(t["name"] for t in listed)
    writers = [t for t in listed if _declared_writer(t)]
    if not any(not _names_its_target(t) for t in writers):
        return frozenset()
    return frozenset(t["name"] for t in writers)


class CallGate:
    """FIFO of forwarded ``tools/call`` frames; one is written at a time.

    *write* sends one frame upstream under its forward id; it runs after any
    per-call preparation the frame was queued with, and before the slot can be
    released by that frame's reply.
    """

    def __init__(self, write: Writer) -> None:
        self._write = write
        # fid -> (the frame to write, the caller's own frame it came from)
        self._queue: "OrderedDict[str, tuple[dict[str, Any], Optional[dict[str, Any]]]]" = (
            OrderedDict()
        )
        self._current: Optional[str] = None
        self._current_origin: Optional[dict[str, Any]] = None
        # Whether the frame of the call in flight has been written, and whether
        # it was cancelled before it was.
        self._sent = False
        self._unwanted = False
        self._task: Optional["asyncio.Task[None]"] = None
        # A write failed: the pipe is dead, so nothing more is pumped.
        self._broken = False
        self._grace: Optional[asyncio.TimerHandle] = None

    @property
    def in_flight(self) -> Optional[str]:
        return self._current

    def queued(self) -> list[str]:
        return list(self._queue)

    def submit(
        self, fid: str, frame: dict[str, Any], origin: Optional[dict[str, Any]] = None
    ) -> None:
        """Queue *frame* under *fid*; it is written when the slot is free.

        *origin* is the caller's own frame, handed back by :meth:`fail_all`
        while the call is still unsent.
        """
        self._queue[fid] = (frame, origin)
        self._pump()

    def release(self, fid: str) -> None:
        """The call *fid* ended (a reply, or none can arrive): free its slot."""
        if self._queue.pop(fid, None) is not None:
            return
        if fid == self._current:
            self._current = None
            if self._grace is not None:
                self._grace.cancel()
                self._grace = None
            self._pump()

    def rekey(self, old: str, new: str) -> None:
        """The call in flight as *old* was re-sent as *new* (a rehandshake retry)."""
        if self._current == old:
            self._current = new
            if self._grace is not None:
                # A cancel's grace keeps its deadline but now frees the retry.
                when = self._grace.when()
                self._grace.cancel()
                self._grace = asyncio.get_running_loop().call_at(when, self.release, new)

    def cancel(self, fid: str) -> str:
        """*fid* was cancelled (by its chat, a Stop, or a disconnect). Returns what that means.

        ``DROPPED``: it was queued and is forgotten; ``UNSENT``: it holds the slot
        but its frame is not out yet, so the writer sends nothing and frees the
        slot; for both the server never sees the call, so no upstream cancel is
        owed. ``SENT``: it is running, so the upstream cancel goes out and the
        slot frees on its reply or :data:`CANCEL_GRACE_SECS` later.
        ``NOT_GATED``: the gate never held it.
        """
        if self._queue.pop(fid, None) is not None:
            return DROPPED
        if fid != self._current:
            return NOT_GATED
        if not self._sent:
            self._unwanted = True
            return UNSENT
        if self._grace is None:
            loop = asyncio.get_running_loop()
            self._grace = loop.call_later(CANCEL_GRACE_SECS, self.release, fid)
        return SENT

    def mark_sending(self, fid: str) -> None:
        """*fid*'s frame goes out now: a cancel from here on is ``SENT``."""
        if fid == self._current:
            self._sent = True

    def wanted(self, fid: str) -> bool:
        """Whether *fid* holds the slot and was not cancelled: the writer's last check."""
        return fid == self._current and not self._unwanted

    def fail_all(self) -> list[tuple[str, dict[str, Any]]]:
        """The backend is gone: forget every queued call and the one in flight.

        Returns ``(fid, origin)`` for each call whose frame was never written
        and that nobody cancelled: the server never saw it, so it may run on a
        replacement.
        """
        unsent = [(fid, origin) for fid, (_f, origin) in self._queue.items() if origin is not None]
        if (
            self._current is not None
            and not self._sent
            and not self._unwanted
            and self._current_origin is not None
        ):
            unsent.insert(0, (self._current, self._current_origin))
        self._queue.clear()
        self._current = None
        self._current_origin = None
        if self._grace is not None:
            self._grace.cancel()
            self._grace = None
        return unsent

    def _pump(self) -> None:
        if self._broken or self._current is not None or not self._queue:
            return
        fid, (frame, origin) = self._queue.popitem(last=False)
        self._current = fid
        self._current_origin = origin
        self._sent = False
        self._unwanted = False
        self._task = asyncio.get_running_loop().create_task(self._send(fid, frame))

    async def _send(self, fid: str, frame: dict[str, Any]) -> None:
        try:
            sent = await self._write(fid, frame)
        except Exception:
            # The write failed (a closed pipe): the backend-gone path settles
            # every pending call. The slot stays taken so nothing queued is
            # pumped into the dead pipe; ``fail_all`` hands the queue back.
            logger.debug("call gate: write of %s failed", fid, exc_info=True)
            self._broken = True
            return
        if not sent:
            self.release(fid)
        elif self._current == fid:
            self._sent = True
