"""In-memory agent->Electron browser command bus.

The dashboard's Browser panel hosts a native embedded Chromium view owned by the
Electron main process. The agent's ``browser`` MCP tool call originates in Python
and needs a route into that native view. This module is the gateway-side half of
that route: a tiny, framework-free command bus plus the three loopback HTTP
endpoints wired in ``dashboard/handlers/messaging.py``.

Shape (why this design):
- The ``browser`` MCP tool calls ``POST /api/browser/command`` to run one op. That maps to
  :meth:`BrowserCommandBus.submit`, which enqueues the command and awaits its
  result (bounded by ``timeout_ms``).
- The Electron main process long-polls ``POST /api/browser/command-drain``
  (:meth:`drain`) for queued commands, and posts each result back via
  ``POST /api/browser/command-result`` (:meth:`complete`).
- ``drain`` is also the *liveness signal*: draining a set of session keys
  REGISTERS them as having a live native panel for a fixed ``panel_ttl_s``
  window (refreshed by every drain AND every result post, independent of the
  poll wait), and marks a native host as present for the same window (even an
  empty-keys heartbeat does). :meth:`submit` raises :class:`NoPanelError` at
  once when no host is polling at all (a remote / non-Electron gateway), so the
  proxy falls back to Playwright without delay; when a host IS present but the
  target's panel is not registered yet it holds for a bounded ``panel_wait_ms``,
  closing the cold-start race where the first ``navigate`` of a fresh session
  beats the drain loop's registration.

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
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Default ceiling on commands queued per session before we reject. Matches the
# spec's suggested bound; a live native panel drains far faster than this fills.
DEFAULT_MAX_QUEUE_PER_SESSION = 32

# Default per-command wait (ms) when the caller does not specify ``timeout_ms``.
DEFAULT_COMMAND_TIMEOUT_MS = 15000

# Default long-poll wait (ms) for ``drain`` when the caller omits ``wait_ms``.
DEFAULT_DRAIN_WAIT_MS = 25000

# Bounded wait (ms) inside ``submit`` for a panel to REGISTER before giving up
# with :class:`NoPanelError`. Covers the cold-start race between a chat slot
# mounting (declaring its key so the Electron drain loop starts polling it) and
# that key actually registering on the bus: the first ``navigate`` in a fresh
# session otherwise beats registration and 503s straight to the Playwright
# mirror. Only waited when a native host is actually polling (see
# ``_host_present_locked``), so a remote / non-Electron gateway still fails fast.
# Kept comfortably above the Electron drain loop's poll interval
# (browser-agent-channel.js DEFAULT_WAIT_MS) so a freshly declared key is picked
# up and registered by the next drain well within this window.
DEFAULT_PANEL_WAIT_MS = 3000

# Panel-liveness TTL (seconds), DECOUPLED from the drain poll interval. A panel
# is "live" for this long after any contact from its window -- a drain OR a
# result post (:meth:`complete`) -- NOT merely for ~2x the poll wait. This is
# what lets the Electron loop use a short drain wait (frequent re-reads for
# prompt cold-start registration) without the liveness lapsing while the loop is
# busy dispatching a long op (e.g. ``wait_for``): the op's own result refreshes
# it, and agent ops are serial per session so no submit races the gap. Also the
# crash-safety net: a window that stops answering deregisters after this long.
DEFAULT_PANEL_TTL_S = 30.0


class BusError(Exception):
    """Base class for command-bus errors."""


class NoPanelError(BusError):
    """No live native panel is registered for the target session (maps to 503).

    Raised by :meth:`BrowserCommandBus.submit` WITHOUT waiting, so the ``browser``
    MCP tool can immediately fall back to playwright-cli.
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
        panel_wait_ms: int = DEFAULT_PANEL_WAIT_MS,
        panel_ttl_s: float = DEFAULT_PANEL_TTL_S,
    ) -> None:
        self._now = now
        self._max_queue = max_queue_per_session
        self._panel_wait_ms = panel_wait_ms
        self._panel_ttl_s = panel_ttl_s
        # session_key -> queued commands not yet handed to a drain call.
        self._queues: dict[str, deque[_Command]] = {}
        # command id -> command handed to a drain call, awaiting its result.
        self._inflight: dict[str, _Command] = {}
        # session_key -> monotonic expiry; a session is "live" while now < expiry.
        self._panels: dict[str, float] = {}
        # Monotonic expiry of the "an Electron host is polling" signal, refreshed
        # by every ``drain`` (including empty-keys heartbeats) with the SAME TTL a
        # drain grants a panel. Lets ``submit`` distinguish "panel not registered
        # YET, keep waiting" (a local host IS draining) from "no native host at
        # all" (a remote / non-Electron gateway is never drained), so it only
        # blocks in the first case.
        self._host_expiry: float = float("-inf")
        self._lock = asyncio.Lock()
        # Set whenever a command is enqueued; a waiting drain wakes on it.
        self._signal = asyncio.Event()
        # Set whenever a drain REGISTERS one or more panels; a submit blocked on a
        # cold-starting panel wakes on it to re-check liveness.
        self._register_signal = asyncio.Event()

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

    def _host_present_locked(self) -> bool:
        """Whether an Electron host is currently polling (drained recently).

        True while within the TTL of the most recent ``drain`` -- a live host
        re-polls well inside that window, so this stays true between its polls
        and lapses only once it stops. A remote / non-Electron gateway is never
        drained, so this is permanently false there and ``submit`` fails fast.
        """
        return self._now() < self._host_expiry

    def _register_locked(self, session_keys: list[str], ttl_s: float) -> None:
        exp = self._now() + ttl_s
        for key in session_keys:
            if isinstance(key, str) and key:
                # Log only the not-alive -> alive transition (first drain of a
                # session, or re-registration after a TTL lapse), never the
                # per-drain refresh, so this proves an Electron host is polling
                # THIS gateway for THIS session without flooding the log.
                if not self._panel_alive_locked(key):
                    logger.debug(
                        "browser-cmdbus: native panel registered (Electron polling) session=%s",
                        key,
                    )
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
        :class:`NoPanelError` when no live panel is registered -- immediately if
        no native host is polling at all (remote / non-Electron gateway), or
        after a bounded ``panel_wait_ms`` if a host is present but the panel does
        not register in time (cold-start race). Raises :class:`QueueFullError`
        when the per-session queue is full, and :class:`asyncio.TimeoutError`
        when the panel does not answer within ``timeout_ms``.
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
        # Cold-start race: the slot may have declared its key microseconds ago
        # and the Electron drain loop has not registered it on the bus yet. Hold
        # briefly for that registration instead of 503-ing straight to the
        # Playwright mirror -- but ONLY while a native host is actually polling
        # (``_await_panel`` fails fast otherwise), so a remote / non-Electron
        # gateway still falls back without delay.
        await self._await_panel(session_key)
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

    async def _await_panel(self, session_key: str) -> None:
        """Block until ``session_key`` has a live panel, bounded by ``panel_wait_ms``.

        Returns immediately if the panel is already live. Raises
        :class:`NoPanelError` immediately when NO native host is polling (a
        remote / non-Electron gateway), and after ``panel_wait_ms`` if a host is
        present but the panel never registers within the window. Waiting only in
        the host-present case is what closes the cold-start race without delaying
        the Playwright fall-back on a host that has no native view.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(self._panel_wait_ms, 0) / 1000.0
        while True:
            async with self._lock:
                self._purge_locked()
                if self._panel_alive_locked(session_key):
                    return
                if not self._host_present_locked():
                    logger.debug(
                        "browser-cmdbus: submit session=%s but NO Electron host polling -> NoPanel (fast fallback)",
                        session_key,
                    )
                    raise NoPanelError(session_key)
                # Clear under the lock: a drain sets the register signal only
                # after releasing its lock, so a registration that races this
                # check cannot be lost between the clear and the wait below.
                self._register_signal.clear()
            remaining = deadline - loop.time()
            if remaining <= 0:
                logger.debug(
                    "browser-cmdbus: submit session=%s host present but panel not registered within %dms -> NoPanel (cold-start)",
                    session_key,
                    self._panel_wait_ms,
                )
                raise NoPanelError(session_key)
            try:
                await asyncio.wait_for(self._register_signal.wait(), remaining)
            except asyncio.TimeoutError:
                logger.debug(
                    "browser-cmdbus: submit session=%s host present but panel not registered within %dms -> NoPanel (cold-start)",
                    session_key,
                    self._panel_wait_ms,
                )
                raise NoPanelError(session_key) from None

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
        a fixed ``panel_ttl_s`` window, independent of ``wait_ms`` (this is what
        makes :meth:`submit` stop returning :class:`NoPanelError`). Returns ``{"id", "session_key",
        "op", "args"}`` when a command is available, or ``None`` if nothing
        arrives within ``wait_ms``.
        """
        keys = [k for k in session_keys if isinstance(k, str) and k]
        wait_s = max(wait_ms, 0) / 1000.0
        # Liveness TTL is a FIXED window, independent of the poll wait, so a short
        # drain interval does not shorten how long a panel stays live.
        ttl_s = self._panel_ttl_s
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s

        async with self._lock:
            # A drain call -- even an empty-keys heartbeat -- proves a local
            # Electron host is polling: refresh the host-present signal with the
            # same fixed TTL a panel gets, so ``submit`` waits for a cold-starting
            # panel here yet fails fast on a never-drained remote gateway.
            self._host_expiry = max(self._host_expiry, self._now() + ttl_s)
            self._register_locked(keys, ttl_s)
        if keys:
            # Wake any submit blocked on a cold-starting panel to re-check.
            self._register_signal.set()

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
            # A result post proves the window is alive: refresh its panel
            # liveness (and the host-present signal) so a long op whose dispatch
            # outlasts a poll gap cannot let the panel lapse before the next op.
            self._host_expiry = max(self._host_expiry, self._now() + self._panel_ttl_s)
            self._register_locked([cmd.session_key], self._panel_ttl_s)
            if not cmd.future.done():
                cmd.future.set_result(
                    {"id": command_id, "ok": bool(ok), "result": result, "error": error}
                )
            return True


# ── process-wide singleton ────────────────────────────────────────────────
# The aiohttp handlers and the ``browser`` MCP tool ingress share one bus per
# gateway process, mirroring how the frame path shares one DashboardState. Tests
# construct their own BrowserCommandBus(now=...) directly for clock injection.
_default_bus: Optional[BrowserCommandBus] = None


def get_command_bus() -> BrowserCommandBus:
    """Return the process-wide :class:`BrowserCommandBus`, creating it lazily."""
    global _default_bus
    if _default_bus is None:
        _default_bus = BrowserCommandBus()
    return _default_bus
