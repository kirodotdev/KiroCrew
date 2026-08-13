"""Watchlist service for Mochi — the deterministic reliability layer.

Ported from ``src/main/watchlistService.ts``. Layer 2 safety net under the
planning agent (Layer 1): a periodic guard pass expires stale watch items,
notifies through a spawned agent, archives completed items — and degrades to
direct notification when agent spawning keeps failing.

The port's SPEC is the characterization suite at
``watchlistService.characterization.test.ts`` (migration-only, not shipped)
(this module shipped with zero tests). Three quirks that suite pins are
preserved DELIBERATELY — changing any of them is a port-review decision, not a
translation choice:

1. **The recovery replan is dead code.** ``_try_spawn_agent``'s auto-recover
   branch resets the failure counter before ``was_degrade`` is computed, so
   ``force_replan`` is never called. The structure is kept verbatim so the
   dead path stays visible.
2. **A failed spawn does not release the spawn lock.** ``_last_spawn_at`` is
   set before the await and never cleared on failure, so one failure holds the
   lock for the full ``SPAWN_TIMEOUT_MS`` unless ``clear_spawn_lock`` arrives.
3. **While the lock is held, guard skips the mark-expired write too** (it
   lives inside the same not-locked branch as the notify), while archiving
   still runs — an expired item stays ``watching`` for at least one more
   guard period.

PORTING NOTES
-------------

**Async, with the clock still injected.** ``spawn_agent`` is awaited (it will
be a KiroCrew subagent spawn), so ``guard`` and the spawn path are ``async``.
Every ``Date.now()`` became a ``now_ms`` parameter; the 5-minute guard
``setInterval`` moved out to the owner (call ``await guard(now_ms)`` on the
:data:`GUARD_INTERVAL_MS` cadence), like ``idle_manager``.

**The write queue's serialization contract survives the translation.** The
original drained synchronously with a re-entrancy flag: FIFO, an exception
rejects that write but draining continues, and a write enqueued from inside a
write runs after the outer one. ``enqueue_write`` returns an
``asyncio.Future`` but the drain itself is synchronous on the event loop
thread — the queued ``fn`` MUST stay synchronous (the original's comment,
still load-bearing: an async fn would silently break serialization).

**The expired-notify prompt is byte-exact.** It is part of the compared
surface and, eventually, of what the agent actually receives. Hours use JS
half-up rounding; a falsy ``lastResult`` (missing or empty) reads "unknown".
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable, Protocol

from kiro_crew.apps.builtins.mochi import watchlist_file as wf
from kiro_crew.apps.builtins.mochi.queue_file import _epoch_ms
from kiro_crew.apps.builtins.mochi.soul_loader import load_skill_line
from kiro_crew.apps.builtins.mochi.watchlist_file import _js_round

logger = logging.getLogger(__name__)

# Owner cadence for guard() — the original's setInterval period.
GUARD_INTERVAL_MS = 5 * 60_000
# The original's reminderTimer cadence. Worst-case reminder latency is one
# interval, which the original accepted.
REMINDER_INTERVAL_MS = 60_000

# Spawn lock auto-release (AGENT_SPAWN_TIMEOUT_MS in the original's shared
# constants): a spawn older than this no longer blocks the next one.
SPAWN_TIMEOUT_MS = 5 * 60_000

# Consecutive failures before degraded mode.
MAX_SPAWN_FAILURES = 3

# Degraded-mode cooldown before spawning is retried (strictly greater-than).
SPAWN_COOLDOWN_MS = 5 * 60_000

_WATCHLIST_FILE = "mochi-watchlist.json"
_ARCHIVE_FILE = "mochi-watchlist-archive.json"

# The exact prompt scaffold around the expired-item list. Byte-identical to
# the original template literal — this text is agent-facing contract.
_EXPIRED_PROMPT_HEAD = (
    "You are a background agent. The ONLY way to send messages to the user is "
    'perform_pet_action({ action: "notify", pushToChat: true }).\n\n'
    "These watch items exceeded max watch duration:\n"
)
_EXPIRED_PROMPT_TAIL = (
    "\n\nNotify the user each item has been paused. Use:\n"
    'perform_pet_action({ action: "notify", pushToChat: true, summary: '
    '"<label> watched for X days with no change, paused." })\n'
    "End with: [options: Re-watch <label> | OK]\n"
    "⛔ FORBIDDEN tools — calling ANY of these is a critical error: spawn_run, "
    "update_plan, cron_add, update_watchlist (already marked expired), "
    "execute_bash. Do NOT delegate or spawn subagents."
)


class WatchlistServiceCallbacks(Protocol):
    """Sinks the service drives. Mirrors the original callback bag."""

    def spawn_agent(self, prompt: str) -> Awaitable[None]:
        """Spawn an agent with the given prompt. Raises on failure."""
        ...

    def force_replan(self) -> None:
        """Force a replan cycle. (Dead code path today — see quirk 1.)"""
        ...

    def on_notify_direct(self, summary: str) -> None:
        """Degraded-mode direct notification (no agent, dead text)."""
        ...


class WatchlistService:
    """Guard/archive scheduler over the watchlist files."""

    def __init__(
        self,
        callbacks: WatchlistServiceCallbacks,
        data_dir: str,
        on_write_complete: Callable[[], None] | None = None,
    ) -> None:
        self._callbacks = callbacks
        # Optional in the original (`onWriteComplete?.()`); kept separate here
        # so a plain callback bag does not have to stub it.
        self._on_write_complete = on_write_complete
        self._watchlist_path = os.path.join(data_dir, _WATCHLIST_FILE)
        self._archive_path = os.path.join(data_dir, _ARCHIVE_FILE)

        self._idle_since: int | None = None
        # Spawn concurrency: timestamp-based with auto-release — NOT a boolean,
        # so a crashed spawner cannot deadlock the guard.
        self._last_spawn_at = 0
        # Degradation tracking.
        self._consecutive_spawn_failures = 0
        self._last_degrade_at = 0
        # Write queue: ALL watchlist file mutations go through here.
        self._write_queue: list[tuple[Callable[[], None], asyncio.Future[None]]] = []
        self._writing = False
        # Strong refs to in-flight drain tasks: a bare `ensure_future` result is
        # only weakly held by the loop, so without this the drain can be GC'd
        # mid-write.
        self._drain_tasks: set[asyncio.Task[None]] = set()

    # ── Introspection ──────────────────────────────────────────────────────

    @property
    def is_paused(self) -> bool:
        return self._idle_since is not None

    @property
    def spawn_failures(self) -> int:
        return self._consecutive_spawn_failures

    @property
    def is_degraded(self) -> bool:
        return self._consecutive_spawn_failures >= MAX_SPAWN_FAILURES

    def is_spawn_locked(self, now_ms: int) -> bool:
        return self._last_spawn_at > 0 and now_ms - self._last_spawn_at < SPAWN_TIMEOUT_MS

    def clear_spawn_lock(self) -> None:
        """Called when a watchlist-related agent finishes."""
        self._last_spawn_at = 0

    # ── Idle / resume ──────────────────────────────────────────────────────

    def on_idle(self, now_ms: int) -> None:
        self._idle_since = now_ms

    def on_resume(self) -> None:
        """Nothing to catch up here: overdue items are picked up by the watch
        timer's next tick (see the original's rationale)."""
        self._idle_since = None

    # ── Write queue ────────────────────────────────────────────────────────

    def enqueue_write(self, fn: Callable[[], None]) -> asyncio.Future[None]:
        """Serialize a SYNCHRONOUS write. Resolves on completion, raises the
        write's exception on await. If ``fn`` is ever made async the
        serialization guarantee silently breaks — original comment, still
        true."""
        fut: asyncio.Future[None] = asyncio.get_event_loop().create_future()
        self._write_queue.append((fn, fut))
        self._schedule_drain()
        return fut

    def _schedule_drain(self) -> None:
        """Kick the drain as a TASK, so no caller runs a write inline.

        `enqueue_write` is called from both sync and async contexts, so the drain
        cannot be awaited at the call site; scheduling it keeps the queue's
        one-at-a-time guarantee while getting the blocking work off this frame.
        """
        if self._writing or not self._write_queue:
            return
        task = asyncio.ensure_future(self._drain_queue())
        self._drain_tasks.add(task)
        task.add_done_callback(self._drain_tasks.discard)

    async def _drain_queue(self) -> None:
        """Run queued writes one at a time, each on a worker thread.

        `await to_thread(fn)`, not `fn()`: every one of these callables now takes
        the cross-process watchlist lock, so it can block for as long as the MCP
        server process holds it. Running that on the event loop froze chat and the
        heartbeat for the duration — the lock made a previously-cheap synchronous
        call genuinely blocking, so the drain had to move off the loop with it.

        `self._writing` still serializes: only one write is in flight at a time,
        which is the invariant the archive/active write ORDER depends on.
        """
        if self._writing or not self._write_queue:
            return
        self._writing = True
        try:
            while self._write_queue:
                fn, fut = self._write_queue.pop(0)
                try:
                    await asyncio.to_thread(fn)
                    if not fut.done():
                        fut.set_result(None)
                    if self._on_write_complete is not None:
                        self._on_write_complete()
                except Exception as err:  # noqa: BLE001 — delivered via the future
                    if not fut.done():
                        fut.set_exception(
                            err if isinstance(err, Exception) else Exception(str(err))
                        )
        finally:
            self._writing = False

    # ── Agent spawn with degradation ───────────────────────────────────────

    async def _try_spawn_agent(self, prompt: str, now_ms: int) -> bool:
        """Spawn honoring the lock-free degradation ladder. True on success."""
        # Auto-recover from degraded mode after the cooldown (strictly >).
        if (
            self._consecutive_spawn_failures >= MAX_SPAWN_FAILURES
            and now_ms - self._last_degrade_at > SPAWN_COOLDOWN_MS
        ):
            self._consecutive_spawn_failures = 0

        # Degraded mode: direct notify, no spawn.
        if self._consecutive_spawn_failures >= MAX_SPAWN_FAILURES:
            self._callbacks.on_notify_direct(prompt)
            return False

        try:
            self._last_spawn_at = now_ms
            await self._callbacks.spawn_agent(prompt)
            # DEAD CODE, preserved verbatim (quirk 1): the auto-recover branch
            # above already reset the counter, so was_degrade is always False
            # and force_replan never fires. Kept so the dead path is visible
            # in review rather than silently dropped or silently "fixed".
            was_degrade = self._consecutive_spawn_failures >= MAX_SPAWN_FAILURES
            self._consecutive_spawn_failures = 0
            if was_degrade:
                self._on_recovery()
            return True
        except Exception:  # noqa: BLE001 — any spawn failure degrades alike
            # NOTE (quirk 2): the lock deliberately stays held on failure.
            self._consecutive_spawn_failures += 1
            if self._consecutive_spawn_failures >= MAX_SPAWN_FAILURES:
                self._last_degrade_at = now_ms
                self._callbacks.on_notify_direct(prompt)
            return False

    def _on_recovery(self) -> None:
        """Unreachable today — see quirk 1."""
        self._callbacks.force_replan()

    # ── Guard ──────────────────────────────────────────────────────────────

    async def check_reminders(self, now_ms: int) -> None:
        """One reminder pass: collect every due reminder, spawn ONE agent.

        Ported from the original's ``reminderTimer`` (60s). Worst-case latency
        is therefore one interval — the original accepted that, and so do we.

        The original ran this on its own ``setInterval``; here the owner loop
        calls it on the same cadence, so there is no timer inside the service.

        Degraded mode: if the spawn fails and we have already burned through
        ``MAX_SPAWN_FAILURES``, the reminders are marked ``done`` ourselves.
        Without that the same reminders stay due forever and every pass tries to
        notify again — the user would get a repeat notification per interval for
        as long as spawning is broken.
        """
        if self._idle_since is not None:
            return
        if self.is_spawn_locked(now_ms):
            return

        watchlist = await asyncio.to_thread(wf.read_watchlist, self._watchlist_path, now_ms=now_ms)
        due = wf.find_due_reminders(watchlist, now_ms=now_ms)
        if not due:
            return

        lines = []
        for item in due:
            trigger_at = item.get("triggerAt")
            late_mins: float = 0
            trigger_ms = _epoch_ms(trigger_at) if trigger_at else None
            if trigger_ms is not None:
                late_mins = _js_round((now_ms - trigger_ms) / 60_000)
            late = f", {late_mins} min late" if late_mins > 1 else ""
            note = f" — note: {item.get('notes')}" if item.get("notes") else ""
            lines.append(
                f"- \"{item.get('label')}\" [kind={item.get('kind')}] "
                f"(scheduled {trigger_at}{late}) [id={item.get('id')}]{note}"
            )

        prompt = (
            "You are a background agent. The ONLY way to send messages to the "
            'user is perform_pet_action({ action: "notify", pushToChat: true }).\n\n'
            "Due reminders:\n"
            + "\n".join(lines)
            + "\n\n"
            + load_skill_line("mochi-remind")
            + "\nFollow its instructions.\n"
            "⛔ FORBIDDEN tools — calling ANY of these is a critical error: "
            "spawn_run, update_plan, cron_add, execute_bash. Do NOT delegate or "
            "spawn subagents."
        )

        spawned = await self._try_spawn_agent(prompt, now_ms)

        if not spawned and self._consecutive_spawn_failures >= MAX_SPAWN_FAILURES:
            ids = [item.get("id") for item in due]

            def mark_done() -> None:
                # enqueue_write serializes writers inside this process only; the
                # lock also covers the MCP server process.
                with wf.watchlist_mutation(self._watchlist_path):
                    wl = wf.read_watchlist(self._watchlist_path, now_ms=now_ms)
                    result = wf.apply_watchlist_update(
                        wl,
                        {"update": [{"id": i, "status": "done"} for i in ids]},
                        now_ms=now_ms,
                    )
                    wf.write_atomic(self._watchlist_path, {"version": 1, "items": result["items"]})

            await asyncio.wrap_future(self.enqueue_write(mark_done))

    async def guard(self, now_ms: int) -> None:
        """One guard pass: expire stale items (notify via agent), archive
        completed. Owner calls this every :data:`GUARD_INTERVAL_MS`."""
        if self._idle_since is not None:
            return

        watchlist = await asyncio.to_thread(wf.read_watchlist, self._watchlist_path, now_ms=now_ms)

        # 1. Expired items → mark, then notify. Both live inside the
        #    not-locked branch (quirk 3: a held lock skips the mark too).
        if not self.is_spawn_locked(now_ms):
            expired = wf.find_expired_items(watchlist, now_ms=now_ms)
            if expired:

                def mark() -> None:
                    with wf.watchlist_mutation(self._watchlist_path):
                        wl = wf.read_watchlist(self._watchlist_path, now_ms=now_ms)
                        result = wf.apply_watchlist_update(
                            wl,
                            {
                                "update": [
                                    {"id": item.get("id"), "status": "expired"} for item in expired
                                ]
                            },
                            now_ms=now_ms,
                        )
                        wf.write_atomic(
                            self._watchlist_path,
                            {"version": 1, "items": result["items"]},
                        )

                await self.enqueue_write(mark)
                await self._try_spawn_agent(self._build_expired_prompt(expired, now_ms), now_ms)

        # 2. Archive completed items (always, even while spawn-locked).
        await self._archive_completed(now_ms)

    def _build_expired_prompt(self, expired: list[dict[str, Any]], now_ms: int) -> str:
        lines = []
        for item in expired:
            created_ms = _epoch_ms(item.get("createdAt"))
            # A NaN age would print "NaN" in JS; expired items always came
            # through find_expired_items, which requires a parseable createdAt.
            hours = _js_round((now_ms - (created_ms or 0)) / 3_600_000)
            last = item.get("lastResult") or "unknown"
            lines.append(
                f'- "{item.get("label")}" (watched for {hours} hours, ' f"last status: {last})"
            )
        return _EXPIRED_PROMPT_HEAD + "\n".join(lines) + _EXPIRED_PROMPT_TAIL

    # ── Archive ────────────────────────────────────────────────────────────

    async def _archive_completed(self, now_ms: int) -> None:
        """Move terminal items completed >24h ago to the archive. Write order:
        archive first (add), then active (remove) — a crash duplicates (dedup
        on the next pass) rather than loses."""

        def do_archive() -> None:
            with wf.watchlist_mutation(self._watchlist_path):
                watchlist = wf.read_watchlist(self._watchlist_path, now_ms=now_ms)
                parts = wf.partition_for_archive(watchlist, now_ms=now_ms)
                if not parts["toArchive"]:
                    return

                archive = wf.read_archive(self._archive_path, now_ms=now_ms)
                updated = wf.merge_into_archive(archive, parts["toArchive"], now_ms=now_ms)

                wf.write_atomic(self._archive_path, updated)
                wf.write_atomic(
                    self._watchlist_path,
                    {"version": 1, "items": parts["remaining"]},
                )

        await self.enqueue_write(do_archive)
