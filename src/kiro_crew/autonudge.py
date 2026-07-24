"""Auto-nudge service — reactive same-session self-prompting loop.

Each active loop is bound to a dashboard chat slot. When the slot's turn
completes (``HOOK_EVENT_STOP``), we arm an idle timer. If no new user input
arrives within ``idle_secs``, we inject the configured nudge message as the
next turn into the same slot.

State is persisted to ``~/.kirocrew/autonudge.json`` (fcntl-locked, atomic
write). On gateway restart, active loops are reloaded and timers re-armed.

The browser observes the loop through the normal chat stream path — nudges
appear as user-style messages tagged ``[auto-nudge cycle N]`` so they are
visually distinct from human input.

Feature-flagged via env ``KIROCREW_AUTONUDGE`` (on by default; set to ``0`` to disable).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator

from kiro_crew import platform_compat, shutdown_event
from kiro_crew.config.loader import config_dir

logger = logging.getLogger(__name__)

_NUDGES_FILE = "autonudge.json"
_STORE_VERSION = 1
_MIN_IDLE_SECS = 15
_MAX_IDLE_SECS = 86400  # 24h
# Re-arm delay after a skipped/failed fire so a busy slot or a transient fire
# error can't silently orphan the loop. The delay escalates exponentially per
# consecutive failure (base << streak) up to _REARM_MAX_BACKOFF_SECS, and is
# always capped by the loop's idle_secs, so a permanently-wedged callback backs
# off to a slow poll instead of hammering every base interval.
_REARM_BACKOFF_SECS = 15
_REARM_MAX_BACKOFF_SECS = 300  # 5m ceiling for the escalated re-arm delay
_REARM_BACKOFF_MAX_SHIFT = 16  # clamp the 2**shift exponent

# Sentinel file per loop: creating it halts the loop on next cycle.
STOP_SENTINEL = "STOP"

# Namespaced session-key prefixes that identify messaging-channel sessions
# (as opposed to bare dashboard chat-slot keys). Channel-bound loops have no
# dashboard turn-lifecycle hooks (notify_turn_complete / notify_user_input),
# so they run on a fixed interval instead of an idle timer: the timer re-arms
# itself right after every delivered fire.
_CHANNEL_KEY_PREFIXES = ("slack:", "discord:", "telegram:", "whatsapp:", "unified:")


def is_channel_key(key: str) -> bool:
    """True when *key* names a messaging-channel session (``slack:<ts>``,
    ``discord:{agent}:direct:{user}`` ...) rather than a dashboard chat slot."""
    return key.startswith(_CHANNEL_KEY_PREFIXES)


def binding_key_for(session_key: str) -> str | None:
    """Map a session key to its AutoNudge binding (slot) key, or ``None`` if the
    session is not nudge-able.

    ``dashboard:chat-N-TS`` → bare slot key ``chat-N-TS`` (the autonudge layer
    keys dashboard loops on the bare slot key); ``slack:``/``discord:`` session
    keys pass through unchanged (channel-bound loops). Anything else
    (``cron:``, ``hook:``, ``subagent:`` ...) is not a nudge-able session.

    Single source of truth shared by the ``monitor_start`` MCP tool and the
    workflow ``ctx.nudge`` port so both agree on what "nudge-able" means.
    """
    if not session_key:
        return None
    if session_key.startswith("dashboard:"):
        return session_key.split(":", 1)[1]
    if session_key.startswith(("slack:", "discord:")):
        return session_key
    return None


def enabled() -> bool:
    """Feature flag — on by default. Set ``KIROCREW_AUTONUDGE=0`` to disable."""
    return os.environ.get("KIROCREW_AUTONUDGE", "1").lower() not in ("0", "false", "no")


# Module-level singleton so hooks in chat.py / messaging.py can notify the
# service without needing a reference to the gateway. Set by AutoNudgeService
# on start(); cleared on stop().
_INSTANCE: "AutoNudgeService | None" = None


def get_instance() -> "AutoNudgeService | None":
    return _INSTANCE


@dataclass
class NudgeLoop:
    """A single auto-nudge loop bound to one session.

    ``slot_key`` is the binding key: either a bare dashboard chat-slot key
    (e.g. ``chat-1-1721...``, idle-timer driven via notify_turn_complete) or a
    namespaced messaging-channel session key (e.g. ``slack:<thread_ts>``,
    ``discord:{agent}:direct:{user_id}``), which runs on a fixed interval.
    The field keeps its historical name for store/REST/WS compatibility.
    """

    id: str
    slot_key: str
    message: str
    idle_secs: int = 60
    max_cycles: int = 0  # 0 = unlimited
    cycle_count: int = 0
    active: bool = True
    last_fire_ts: float = 0.0
    created_ts: float = 0.0
    stop_sentinel_path: str = ""  # optional absolute path; if present loop halts


@contextmanager
def _locked_file(path: Path, mode: str) -> Iterator[Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if "r" in mode and not path.exists():
        path.write_text(json.dumps({"version": _STORE_VERSION, "loops": []}))
    # "r" -> "r+": Windows msvcrt.locking requires WRITE access on the fd — a
    # read-only handle fails with EACCES, which platform_compat.file_lock
    # swallows (best-effort), silently degrading the reader's lock to a no-op
    # and letting a concurrent _save race the read (same fix as
    # apps/bridges.py:_mcp_lock). The shared/exclusive decision keys off the
    # ORIGINAL mode so a reader still requests a shared lock.
    exclusive = "w" in mode or "+" in mode
    if mode == "r":
        mode = "r+"
    with open(path, mode, encoding="utf-8") as fh:
        with platform_compat.file_lock(fh.fileno(), exclusive=exclusive):
            yield fh


class AutoNudgeService:
    """Manages reactive per-slot nudge loops with restart-survival."""

    def __init__(
        self,
        base_dir: Path | None = None,
        on_fire: Callable[[NudgeLoop], Awaitable[bool]] | None = None,
    ) -> None:
        self._base_dir = base_dir or config_dir()
        self._path = self._base_dir / _NUDGES_FILE
        self._on_fire = on_fire
        self._loops: dict[str, NudgeLoop] = {}
        self._timers: dict[str, asyncio.Task] = {}
        # Consecutive non-delivery count per loop (drives escalating re-arm
        # backoff + once-per-streak failure logging). Not persisted; resets on
        # a delivered fire, on removal, and on restart.
        self._rearm_fail_count: dict[str, int] = {}
        # Strong refs to in-flight shielded add() tasks: keeps a detached
        # mutation supervised (no GC, failures logged) even when every awaiting
        # caller was cancelled. Discarded on completion.
        self._inflight_adds: set = set()
        self._observers: list[Callable[[str, NudgeLoop | None], None]] = []
        self._lock = asyncio.Lock()

    # ── Persistence ──

    def _load(self) -> None:
        with _locked_file(self._path, "r") as fh:
            data = json.load(fh)
        for raw in data.get("loops", []):
            try:
                loop = NudgeLoop(**{k: raw[k] for k in raw if k in NudgeLoop.__dataclass_fields__})
            except Exception:
                logger.warning("AutoNudge: skipping malformed loop entry: %r", raw, exc_info=True)
                continue
            self._loops[loop.id] = loop
        logger.info("AutoNudge: loaded %d loops", len(self._loops))

    def _serialize_state(self) -> dict:
        """Snapshot the store payload ON THE CALLER'S THREAD.

        Loop state is mutated only under the service lock on the event loop, so
        the serialization must happen there too — a worker thread iterating
        ``self._loops`` concurrently with a mutation would race. The returned
        payload is immutable-by-convention and safe to hand to an executor.
        """
        return {
            "version": _STORE_VERSION,
            "loops": [asdict(lp) for lp in self._loops.values()],
        }

    def _write_state(self, payload: dict) -> None:
        # Atomic write: serialize to a temp file in the same dir, fsync, then
        # os.replace() onto the target path. Eliminates the truncate-before-
        # flock race that plain open(path, "w") has — readers always see either
        # the old complete file or the new complete file, never a partial one.
        # Blocking (fsync) — async callers offload this to an executor.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise

    def _save(self) -> None:
        self._write_state(self._serialize_state())

    # ── Observer hook (for WS broadcasts) ──

    def subscribe(self, cb: Callable[[str, NudgeLoop | None], None]) -> None:
        self._observers.append(cb)

    def _emit(self, event: str, loop: NudgeLoop | None) -> None:
        for cb in self._observers:
            try:
                cb(event, loop)
            except Exception:
                logger.warning("AutoNudge observer failed", exc_info=True)

    # ── Lifecycle ──

    async def start(self) -> None:
        if not enabled():
            logger.info("AutoNudge disabled (KIROCREW_AUTONUDGE not set)")
            return
        self._load()
        # Re-arm timers for active loops on startup.
        for loop in self._loops.values():
            if loop.active:
                self._arm_timer(loop)
        global _INSTANCE
        _INSTANCE = self
        logger.info("AutoNudge started")

    def stop(self) -> None:
        for t in self._timers.values():
            t.cancel()
        self._timers.clear()
        global _INSTANCE
        if _INSTANCE is self:
            _INSTANCE = None

    # ── Loop CRUD ──

    async def add(
        self,
        slot_key: str,
        message: str,
        idle_secs: int = 60,
        max_cycles: int = 0,
        stop_sentinel_path: str = "",
    ) -> NudgeLoop:
        # CANCELLATION SAFETY: the mutate+persist runs as a SHIELDED task. If
        # the awaiting caller is cancelled mid-write, a bare await would release
        # ``_lock`` while the executor write is still running — a subsequent
        # add/update could persist newer state first and then be clobbered by
        # this operation's stale snapshot (lost update after restart). Shielding
        # keeps the inner task (and the lock) alive until the write completes,
        # so writes remain strictly serialized; the cancelled caller still sees
        # CancelledError, with the arm possibly landed (same "mutation may have
        # already landed" semantics as other cancellation-uncertain mutations).
        # The inner task is retained in ``_inflight_adds`` (discarded when done)
        # so it stays SUPERVISED — strongly referenced and completion-logged —
        # even if every awaiting caller has been cancelled.
        inner: "asyncio.Task[NudgeLoop]" = asyncio.ensure_future(
            self._add_locked(
                slot_key,
                message,
                idle_secs=idle_secs,
                max_cycles=max_cycles,
                stop_sentinel_path=stop_sentinel_path,
            )
        )
        self._inflight_adds.add(inner)

        def _finish(t: "asyncio.Task[NudgeLoop]") -> None:
            self._inflight_adds.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logger.warning("AutoNudge: detached add() failed", exc_info=t.exception())

        inner.add_done_callback(_finish)
        return await asyncio.shield(inner)

    async def _add_locked(
        self,
        slot_key: str,
        message: str,
        *,
        idle_secs: int,
        max_cycles: int,
        stop_sentinel_path: str,
    ) -> NudgeLoop:
        idle_secs = max(_MIN_IDLE_SECS, min(_MAX_IDLE_SECS, int(idle_secs)))
        async with self._lock:
            # One loop per slot — replace any existing loop on this slot.
            # persist=False: the offloaded write below persists the combined
            # removal+add atomically, avoiding a duplicate blocking save here.
            existing = self._find_by_slot(slot_key)
            if existing:
                self.remove_sync(existing.id, persist=False)
            loop = NudgeLoop(
                id=uuid.uuid4().hex[:8],
                slot_key=slot_key,
                message=message,
                idle_secs=idle_secs,
                max_cycles=max(0, int(max_cycles)),
                created_ts=time.time(),
                stop_sentinel_path=stop_sentinel_path,
            )
            self._loops[loop.id] = loop
            # Persist WITHOUT blocking the event loop (no-blocking-call rule:
            # _write_state fsyncs, and a wedged disk must not freeze the
            # gateway). Snapshot under the lock (mutation safety), write on a
            # worker thread, and await it so a persistence failure still
            # propagates to the caller before the loop is reported armed.
            payload = self._serialize_state()
            await asyncio.get_running_loop().run_in_executor(
                None, self._write_state, payload
            )
            self._arm_timer(loop)
        self._emit("added", loop)
        logger.info("AutoNudge: added loop %s on slot %s (idle=%ds)", loop.id, slot_key, idle_secs)
        return loop

    async def update(
        self,
        loop_id: str,
        *,
        message: str | None = None,
        idle_secs: int | None = None,
        max_cycles: int | None = None,
        active: bool | None = None,
    ) -> NudgeLoop | None:
        async with self._lock:
            loop = self._loops.get(loop_id)
            if not loop:
                return None
            if message is not None:
                loop.message = message
            if idle_secs is not None:
                loop.idle_secs = max(_MIN_IDLE_SECS, min(_MAX_IDLE_SECS, int(idle_secs)))
            if max_cycles is not None:
                loop.max_cycles = max(0, int(max_cycles))
            if active is not None:
                loop.active = bool(active)
            self._save()
            # Re-arm timer with new settings.
            self._cancel_timer(loop_id)
            if loop.active:
                self._arm_timer(loop)
        self._emit("updated", loop)
        return loop

    def remove_sync(self, loop_id: str, *, persist: bool = True) -> None:
        """Remove a loop. ``persist=False`` skips the blocking save — used by
        async callers that snapshot+offload the write themselves right after."""
        loop = self._loops.pop(loop_id, None)
        if loop is None:
            return
        self._cancel_timer(loop_id)
        self._rearm_fail_count.pop(loop_id, None)
        if persist:
            self._save()
        self._emit("removed", loop)

    async def remove(self, loop_id: str) -> None:
        async with self._lock:
            self.remove_sync(loop_id)

    def get_by_slot(self, slot_key: str) -> NudgeLoop | None:
        return self._find_by_slot(slot_key)

    def list_all(self) -> list[NudgeLoop]:
        return list(self._loops.values())

    def _find_by_slot(self, slot_key: str) -> NudgeLoop | None:
        for lp in self._loops.values():
            if lp.slot_key == slot_key:
                return lp
        return None

    # ── Reactive arming ──

    def notify_turn_complete(self, slot_key: str) -> None:
        """Called by gateway after HOOK_EVENT_STOP — (re)arm idle timer for this slot."""
        loop = self._find_by_slot(slot_key)
        if not loop or not loop.active:
            return
        self._arm_timer(loop)

    def notify_user_input(self, slot_key: str) -> None:
        """Called when user sends a message — cancel pending nudge (user takes priority)."""
        loop = self._find_by_slot(slot_key)
        if not loop:
            return
        self._cancel_timer(loop.id)

    def _cancel_timer(self, loop_id: str) -> None:
        t = self._timers.pop(loop_id, None)
        # Never cancel the currently running timer task (self-re-arm from inside
        # _timer): it is about to return on its own, and cancelling it would
        # inject a spurious CancelledError into the finishing task.
        if t and not t.done() and t is not asyncio.current_task():
            t.cancel()

    def _arm_timer(self, loop: NudgeLoop, delay: float | None = None) -> None:
        self._cancel_timer(loop.id)
        self._timers[loop.id] = asyncio.create_task(self._timer(loop, delay))

    async def _timer(self, loop: NudgeLoop, delay: float | None = None) -> None:
        try:
            await asyncio.sleep(loop.idle_secs if delay is None else delay)
        except asyncio.CancelledError:
            return
        if shutdown_event.is_set():
            return
        # Kill switch: sentinel file present?
        if loop.stop_sentinel_path and Path(loop.stop_sentinel_path).exists():
            logger.info("AutoNudge: stop sentinel found for %s — removing loop", loop.id)
            await self.remove(loop.id)
            return
        # Cycle cap reached?
        if loop.max_cycles and loop.cycle_count >= loop.max_cycles:
            logger.info("AutoNudge: loop %s reached max_cycles — deactivating", loop.id)
            await self.update(loop.id, active=False)
            return
        # Fire. Update state only if the callback reports actual delivery —
        # otherwise skipped nudges (e.g. slot mid-turn) inflate cycle_count and
        # prematurely trip max_cycles. Missing callback → nothing to deliver.
        if self._on_fire is None:
            return
        try:
            delivered = await self._on_fire(loop)
        except Exception:
            delivered = False
            # Full traceback only on the first failure of a streak; subsequent
            # failures stay at debug so a permanently-wedged callback can't spam
            # a traceback every re-arm.
            if self._rearm_fail_count.get(loop.id, 0) == 0:
                logger.exception("AutoNudge fire callback failed for %s", loop.id)
            else:
                logger.debug(
                    "AutoNudge fire still failing for %s (streak=%d)",
                    loop.id,
                    self._rearm_fail_count.get(loop.id, 0) + 1,
                )
        if not delivered:
            # If the fire path already removed the loop (e.g. slot missing →
            # remove()), do NOT resurrect it with a fresh timer — that would
            # orphan-poll forever. Clear the streak and stop.
            if loop.id not in self._loops:
                self._rearm_fail_count.pop(loop.id, None)
                return
            # Slot was busy mid-turn, or the fire callback errored. Do NOT end
            # the loop — re-arm so it self-heals and never depends solely on the
            # external notify_turn_complete hook (skipped on a slot's error/
            # timeout/cancel exit paths). Escalate the delay per consecutive
            # failure so a never-delivering loop backs off to a slow poll
            # instead of hammering, capped by idle_secs and _REARM_MAX_BACKOFF.
            n = self._rearm_fail_count.get(loop.id, 0) + 1
            self._rearm_fail_count[loop.id] = n
            shift = min(n - 1, _REARM_BACKOFF_MAX_SHIFT)
            backoff = min(
                _REARM_BACKOFF_SECS * (2**shift),
                _REARM_MAX_BACKOFF_SECS,
                loop.idle_secs,
            )
            self._arm_timer(loop, delay=backoff)
            return
        # Delivered — clear any failure streak so the next skip starts fresh.
        self._rearm_fail_count.pop(loop.id, None)
        loop.cycle_count += 1
        loop.last_fire_ts = time.time()
        self._save()
        self._emit("fired", loop)
        # Channel-bound loops (Slack/Discord/...) have no dashboard
        # turn-lifecycle hook to re-arm them (notify_turn_complete never fires
        # for these keys), so they self-re-arm on a fixed interval. The fire
        # callback runs the turn inline, so the next fire lands idle_secs
        # after the previous turn finished; the busy-skip + backoff above
        # handles any overlap.
        if is_channel_key(loop.slot_key) and loop.active and loop.id in self._loops:
            self._arm_timer(loop)
