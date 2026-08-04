"""In-memory agent->Electron browser command bus.

The dashboard's Browser panel is getting a native embedded Chromium view owned by
the Electron main process. The agent's ``browser_*`` MCP tool calls originate in
Python (``mcp_playwright_proxy``) and need a route into that native view. This
module is the gateway-side half of that route: a tiny, framework-free command bus
plus the three loopback HTTP endpoints wired in ``dashboard/handlers/messaging.py``.

Shape (why this design):
- The MCP proxy calls ``POST /api/browser/command`` to run one op. That maps to
  :meth:`BrowserCommandBus.submit`, which enqueues the command and awaits its
  result (bounded by ``timeout_ms``).
- The Electron main process long-polls ``POST /api/browser/command-drain``
  (:meth:`drain`) for queued commands, and posts each result back via
  ``POST /api/browser/command-result`` (:meth:`complete`).
- ``drain`` is also the *liveness signal*: draining a set of session keys
  REGISTERS them as having a live native panel for a TTL of roughly ``2x`` the
  max wait. :meth:`submit` fails fast with :class:`NoPanelError` when the target
  session has no live panel, so the proxy can fall back to Playwright without
  waiting.

Everything is bounded so a stuck or absent poller cannot grow memory:
- at most ``max_queue_per_session`` (default 32) commands queue per session;
- a command that times out in ``submit`` is removed from the queue / in-flight
  map, so its memory is reclaimed even if the panel never answers;
- completing an unknown id is a no-op that returns ``False`` (the handler maps
  that to 404);
- panel registrations expire on a TTL and are purged lazily.

The class takes an injectable ``now`` clock (defaulting to ``time.monotonic``) so
TTL expiry is unit-testable without sleeping. It depends only on ``asyncio`` and
the stdlib -- no aiohttp -- so the bus logic can be tested in isolation.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Optional

# Default ceiling on commands queued per session before we reject. Matches the
# spec's suggested bound; a live native panel drains far faster than this fills.
DEFAULT_MAX_QUEUE_PER_SESSION = 32

# Default per-command wait (ms) when the caller does not specify ``timeout_ms``.
DEFAULT_COMMAND_TIMEOUT_MS = 15000

# Default long-poll wait (ms) for ``drain`` when the caller omits ``wait_ms``.
DEFAULT_DRAIN_WAIT_MS = 25000


class BusError(Exception):
    """Base class for command-bus errors."""


class NoPanelError(BusError):
    """No live native panel is registered for the target session (maps to 503).

    Raised by :meth:`BrowserCommandBus.submit` WITHOUT waiting, so the MCP proxy
    can immediately fall back to Playwright.
    """


class QueueFullError(BusError):
    """The per-session command queue is at capacity (maps to a reject)."""


@dataclass
class _Command:
    id: str
    session_key: str
    op: str
    args: dict
    future: "asyncio.Future[dict]"
    enqueued_at: float


class BrowserCommandBus:
    """Async, in-memory command bus bridging the MCP proxy and the native panel.

    All public coroutines are safe to call concurrently; internal state is
    guarded by a single :class:`asyncio.Lock`, and a shared :class:`asyncio.Event`
    wakes a waiting :meth:`drain` when a command is enqueued.
    """

    def __init__(
        self,
        now: Callable[[], float] = time.monotonic,
        *,
        max_queue_per_session: int = DEFAULT_MAX_QUEUE_PER_SESSION,
    ) -> None:
        self._now = now
        self._max_queue = max_queue_per_session
        # session_key -> queued commands not yet handed to a drain call.
        self._queues: dict[str, deque[_Command]] = {}
        # command id -> command handed to a drain call, awaiting its result.
        self._inflight: dict[str, _Command] = {}
        # session_key -> monotonic expiry; a session is "live" while now < expiry.
        self._panels: dict[str, float] = {}
        self._lock = asyncio.Lock()
        # Set whenever a command is enqueued; a waiting drain wakes on it.
        self._signal = asyncio.Event()

    # ── panel registration ────────────────────────────────────────────────

    def _purge_locked(self) -> None:
        """Drop expired panel registrations. Caller must hold ``self._lock``."""
        now = self._now()
        expired = [key for key, exp in self._panels.items() if exp <= now]
        for key in expired:
            self._panels.pop(key, None)

    def _panel_alive_locked(self, session_key: str) -> bool:
        exp = self._panels.get(session_key)
        return exp is not None and exp > self._now()

    def _register_locked(self, session_keys: list[str], ttl_s: float) -> None:
        exp = self._now() + ttl_s
        for key in session_keys:
            if isinstance(key, str) and key:
                self._panels[key] = exp

    async def is_registered(self, session_key: str) -> bool:
        """Return whether ``session_key`` currently has a live native panel."""
        async with self._lock:
            self._purge_locked()
            return self._panel_alive_locked(session_key)

    # ── submit (endpoint 1) ───────────────────────────────────────────────

    async def submit(
        self,
        session_key: str,
        op: str,
        args: Optional[dict] = None,
        timeout_ms: int = DEFAULT_COMMAND_TIMEOUT_MS,
    ) -> dict:
        """Enqueue one command and await its result.

        Returns ``{"id", "ok", "result"}`` on success or ``{"id", "ok": False,
        "error"}`` when the panel ran the op but it failed. Raises
        :class:`NoPanelError` (fast, no wait) when no live panel is registered,
        :class:`QueueFullError` when the per-session queue is full, and
        :class:`asyncio.TimeoutError` when the panel does not answer within
        ``timeout_ms``.
        """
        loop = asyncio.get_running_loop()
        cmd = _Command(
            id=uuid.uuid4().hex,
            session_key=session_key,
            op=op,
            args=dict(args or {}),
            future=loop.create_future(),
            enqueued_at=self._now(),
        )
        async with self._lock:
            self._purge_locked()
            if not self._panel_alive_locked(session_key):
                raise NoPanelError(session_key)
            queue = self._queues.setdefault(session_key, deque())
            if len(queue) >= self._max_queue:
                raise QueueFullError(session_key)
            queue.append(cmd)
        # Wake any waiting drain AFTER releasing the lock.
        self._signal.set()

        timeout_s = max(timeout_ms, 0) / 1000.0
        try:
            return await asyncio.wait_for(cmd.future, timeout_s)
        except asyncio.TimeoutError:
            # Reclaim the command's memory whether it is still queued (never
            # drained) or in-flight (drained, awaiting a result that never came).
            async with self._lock:
                self._discard_locked(cmd)
            raise

    def _discard_locked(self, cmd: _Command) -> None:
        queue = self._queues.get(cmd.session_key)
        if queue is not None:
            try:
                queue.remove(cmd)
            except ValueError:
                pass
            if not queue:
                self._queues.pop(cmd.session_key, None)
        self._inflight.pop(cmd.id, None)

    # ── drain (endpoint 2) ────────────────────────────────────────────────

    def _pop_ready_locked(self, session_keys: list[str]) -> Optional[_Command]:
        for key in session_keys:
            queue = self._queues.get(key)
            while queue:
                cmd = queue.popleft()
                if not queue:
                    self._queues.pop(key, None)
                if not cmd.future.done():
                    return cmd
                # Future already resolved (e.g. timed out) -- skip it.
            # continue to next session key
        return None

    async def drain(
        self,
        session_keys: list[str],
        wait_ms: int = DEFAULT_DRAIN_WAIT_MS,
    ) -> Optional[dict]:
        """Long-poll for one queued command across ``session_keys``.

        REGISTERS every key in ``session_keys`` as having a live native panel for
        a TTL of roughly ``2x`` ``wait_ms`` (this is what makes :meth:`submit`
        stop returning :class:`NoPanelError`). Returns ``{"id", "session_key",
        "op", "args"}`` when a command is available, or ``None`` if nothing
        arrives within ``wait_ms``.
        """
        keys = [k for k in session_keys if isinstance(k, str) and k]
        wait_s = max(wait_ms, 0) / 1000.0
        # TTL is ~2x the max wait so a panel that is actively long-polling never
        # lapses between polls; a panel that stops polling expires within ~2x.
        ttl_s = max(wait_s * 2.0, 1.0)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s

        async with self._lock:
            self._register_locked(keys, ttl_s)

        while True:
            async with self._lock:
                self._purge_locked()
                cmd = self._pop_ready_locked(keys)
                if cmd is not None:
                    self._inflight[cmd.id] = cmd
                    return {
                        "id": cmd.id,
                        "session_key": cmd.session_key,
                        "op": cmd.op,
                        "args": cmd.args,
                    }
                # Nothing ready: clear the signal so we block until the next
                # enqueue sets it. Safe because submit sets the signal only after
                # releasing the lock we hold here, so no enqueue can be missed.
                self._signal.clear()

            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                await asyncio.wait_for(self._signal.wait(), remaining)
            except asyncio.TimeoutError:
                return None

    # ── complete (endpoint 3) ─────────────────────────────────────────────

    async def complete(
        self,
        command_id: str,
        ok: bool,
        result: Any = None,
        error: Optional[str] = None,
    ) -> bool:
        """Resolve a drained command with its result.

        Returns ``True`` when ``command_id`` matched a live in-flight command,
        ``False`` when it is unknown (already timed out or never existed) -- the
        handler maps ``False`` to 404.
        """
        async with self._lock:
            cmd = self._inflight.pop(command_id, None)
            if cmd is None:
                return False
            if not cmd.future.done():
                cmd.future.set_result(
                    {"id": command_id, "ok": bool(ok), "result": result, "error": error}
                )
            return True


# ── process-wide singleton ────────────────────────────────────────────────
# The aiohttp handlers and the MCP-proxy ingress share one bus per gateway
# process, mirroring how the frame path shares one DashboardState. Tests
# construct their own BrowserCommandBus(now=...) directly for clock injection.
_default_bus: Optional[BrowserCommandBus] = None


def get_command_bus() -> BrowserCommandBus:
    """Return the process-wide :class:`BrowserCommandBus`, creating it lazily."""
    global _default_bus
    if _default_bus is None:
        _default_bus = BrowserCommandBus()
    return _default_bus
