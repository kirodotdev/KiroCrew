"""Queue poller for Mochi — executes the plan, spawns the agents, self-heals.

Ported from ``src/main/queuePoller.ts`` (693 lines, zero tests). Spec: the
characterization suite written against the TypeScript at port time
(``queuePoller.characterization.test.ts``, 23 tests — migration-only tooling,
not shipped). The poller reads ``mochi-queue.json`` every second, executes due
deterministic tasks (move/notify/mood) through callbacks, serialises agent
spawns (watch checks first, then freestyle), and triggers plan/replan cycles
with failure backoff.

Characterized quirks, preserved:

1. The stale-task comment claimed 2 minutes; the CODE says 30 —
   :data:`STALE_THRESHOLD_MS` is 30 min and planned (non-urgent) move/mood
   tasks older than that are marked done UNEXECUTED.
2. Plan-lock expiry is detected lazily inside a poll and consumes the whole
   cycle (early return: no tasks, no watch checks, no cleanup that round).
3. A successful watch spawn back-dates the lock to a 30s cooldown — which
   also blocks missed-notification recovery within the same poll.
4. A FAILED watch spawn keeps the full 5-minute lock (no 30s shortcut), so a
   dead gateway backs off naturally.
5. Plan-lock release compares ``planned_at >= spawn_at - 1000`` (agents write
   second-precision timestamps).
6. Storm breaker: at most 5 watch spawns per 10-minute window; the cap
   throttles by taking the full watch lock.

PORTING NOTES
-------------

**Clock is a callable, not a parameter.** Every other ported module takes
``now_ms`` per call, but a poll SUSPENDS across virtual time (a spawn wait is
up to five minutes), so a snapshot taken at entry would be stale after the
await. The constructor takes ``clock: Callable[[], int]`` (defaults to wall
time) and reads it exactly where the original read ``Date.now()``.

**Timers are owner-driven.** The 1s poll interval is gone — the owner calls
``await poll()`` on the :data:`POLL_INTERVAL_MS` cadence (``poll`` itself
keeps the original's serialisation: a call landing mid-poll queues exactly one
follow-up). The spawn-timeout ``setTimeout`` became a deadline fired by
``tick(now_ms)``; ``notify_agent_done`` resolves the wait early, exactly like
the WS ``subagent_done`` path. The first-launch grace timer is a deadline
checked lazily.

**Spawn waits are futures.** ``_resolveCurrentSpawn`` maps to a pending
``asyncio.Future`` resolved by ``notify_agent_done`` (id-matched) or by
``tick`` when the deadline passes; the timed-out flag routes the
failure-bookkeeping path, byte-for-byte prompts included.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Protocol

from kiro_crew.apps.builtins.mochi import queue_file as qf
from kiro_crew.apps.builtins.mochi.queue_file import _epoch_ms, _iso
from kiro_crew.apps.builtins.mochi.soul_loader import load_skill_line

logger = logging.getLogger(__name__)

# Owner cadence for poll() — the original's setInterval period.
POLL_INTERVAL_MS = 1_000

# Serial agent-spawn timeout (AGENT_SPAWN_TIMEOUT_MS upstream).
SPAWN_TIMEOUT_MS = 5 * 60_000

# Consecutive freestyle-task failures before a sticky notify task is queued.
MAX_FAIL_COUNT = 3

# Max watch items batched into a single spawn.
MAX_WATCH_BATCH = 5

# Plan agents are heavier (skill load + calendar + watchlist + LLM reasoning).
PLAN_SPAWN_TIMEOUT_MS = 10 * 60_000

# Give up plan retries after this many consecutive failures.
MAX_PLAN_FAILS = 5

# Exponential plan-retry backoff: base and cap.
PLAN_BACKOFF_BASE_MS = 60_000
_PLAN_BACKOFF_CAP_MS = 16 * 60_000

# Storm breaker: hard rate-limit on watch spawns regardless of lock state.
WATCH_STORM_WINDOW_MS = 10 * 60_000
MAX_WATCH_SPAWNS_PER_WINDOW = 5

# Planned (non-urgent) move/mood tasks older than this are skipped as stale.
# (The original comment said 2 minutes; the code says 30 — quirk 1.)
STALE_THRESHOLD_MS = 30 * 60_000

# Watch-lock cooldown after a successful spawn (back-dating trick — quirk 3).
_WATCH_COOLDOWN_MS = 30_000

_FIRST_LAUNCH_GRACE_MS = 60_000

DETERMINISTIC_TYPES = ("move", "notify", "mood")
AGENT_TYPES = ("freestyle",)

# ── Prompts ────────────────────────────────────────────────────────────────
#
# These were ported byte-exact from the original and then corrected: the original
# ran where a skill-loading tool existed and where one specific Slack server was
# always installed. Neither holds here, so naming them made every spawn open with
# a failing call and a false claim about its own tool set.

_BG_PREAMBLE = (
    "You are Mochi background agent running inside KiroCrew with full Mochi "
    "MCP tool access. "
    "The Mochi tools (perform_pet_action, update_watchlist, get_watchlist, "
    "read_mochi_file, ...) are available to you — call them directly as tool "
    "calls. "
    "Do NOT claim they are unavailable or attempt filesystem workarounds.\n"
    "Other tools (Slack, calendar) depend on what the user has connected: "
    "check what you actually have and say plainly when a capability is "
    "missing, rather than guessing at a tool name.\n"
    "The ONLY way to send messages to the user is: perform_pet_action({ "
    'action: "notify", pushToChat: true, ... })\n'
)

_TOOL_FENCE = (
    "\n⛔ FORBIDDEN tools — calling ANY of these is a critical error and will "
    "be rejected: "
    "spawn_run, update_plan, cron_add, execute_bash. Do NOT attempt to "
    "delegate or spawn subagents.\n"
)


def build_agent_prompt(task: dict[str, Any]) -> str:
    """Freestyle task prompt: the planner's own prompt, or a fallback."""
    action = task.get("action") or {}
    return action.get("prompt") or f"Execute freestyle task: {task.get('id')}"


def build_watch_check_prompt(items: list[dict[str, Any]]) -> str:
    """Prompt for a batch of due watch items."""
    details = []
    for item in items:
        parts = [f"- {item.get('label')} (kind: {item.get('kind')}, id: {item.get('id')})"]
        if item.get("target"):
            parts.append(f"  target: {item['target']}")
        if item.get("triggerCondition"):
            parts.append(f"  triggerCondition: {item['triggerCondition']}")
        if item.get("lastResult"):
            parts.append(f"  lastResult: {item['lastResult']}")
        if item.get("priority") and item.get("priority") != "normal":
            parts.append(f"  priority: {item['priority']}")
        if item.get("checkCount"):
            parts.append(f"  checkCount: {item['checkCount']}")
        details.append("\n".join(parts))

    return (
        _BG_PREAMBLE
        + "\nCheck the status of these watched items:\n"
        + "\n".join(details)
        + "\n\n"
        + load_skill_line("mochi-watch")
        + "\nFollow its instructions."
        + _TOOL_FENCE
    )


def build_missed_notify_prompt(items: list[dict[str, Any]]) -> str:
    """Recovery prompt: notify only, no re-check, no skill load."""
    details = "\n".join(
        f"- {item.get('label')} (id: {item.get('id')}): "
        f"{item.get('lastResult') or 'status changed'}"
        for item in items
    )
    return (
        "You are a background agent. "
        "The ONLY way to send messages to the user is: perform_pet_action({ "
        'action: "notify", pushToChat: true, ... })\n'
        "Do NOT load any skills. Just call the tools directly.\n\n"
        "These watched items changed but the user was NOT notified. "
        "Your ONLY job is to notify the user about each one.\n"
        + details
        + "\n\n"
        + "For each item, do TWO things:\n"
        + '1. perform_pet_action({ action: "notify", summary: "<concise '
        'update>", pushToChat: true, watchItemId: "<item id>" })\n'
        + '2. update_watchlist({ update: [{ id: "<item id>", notified: true '
        "}] })\n" + "Do NOT re-check the items. Just notify and mark as notified." + _TOOL_FENCE
    )


class QueuePollerCallbacks(Protocol):
    """Sinks and sources the poller drives. Mirrors the original bag; the
    optional members may be absent or None on a plain object."""

    def on_move(self, action: dict[str, Any]) -> Awaitable[None]: ...

    def on_notify(self, action: dict[str, Any]) -> Awaitable[None]: ...

    def on_mood(self, action: dict[str, Any]) -> Awaitable[None]: ...

    def trigger_plan(self) -> Awaitable[None]: ...

    def trigger_replan(self) -> Awaitable[None]: ...

    def spawn_agent(self, task_prompt: str) -> Awaitable[str]:
        """Spawn an agent; returns the subagent id."""
        ...


class QueuePoller:
    """Second-cadence executor over ``mochi-queue.json``."""

    def __init__(
        self,
        queue_path: str,
        callbacks: Any,
        clock: Callable[[], int] | None = None,
        budget_provider: Callable[[], Any] | None = None,
    ) -> None:
        self._queue_path = queue_path
        self._callbacks = callbacks
        # Optional live budget (activity_budget.resolve_activity_budget per
        # poll). None preserves the vendored constants exactly — the ported
        # behaviour stays the differential-testing baseline.
        self._budget_provider = budget_provider
        self._clock: Callable[[], int] = clock or (lambda: int(time.time() * 1000))

        self._polling = False
        self._poll_queued = False
        self._stopped = True  # until start(); gates executeNext while idle-paused
        self._plan_triggered = False
        self._plan_spawn_at = 0
        self._watch_spawn_at = 0
        self._plan_fail_count = 0
        self._plan_next_retry_at = 0
        self._watch_spawn_count = 0
        self._watch_window_start = 0
        self._first_launch_grace = False
        self._grace_deadline: int | None = None
        # Serial spawn wait: pending future + its timeout deadline + spawn id.
        self._spawn_future: asyncio.Future[bool] | None = None
        self._spawn_deadline: int | None = None
        self._current_spawn_id: str | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        """Enable polling. The owner drives ``poll()`` on POLL_INTERVAL_MS."""
        self._stopped = False

    def stop(self) -> None:
        """Disable polling and drop the pending spawn timeout (the wait then
        resolves only via ``notify_agent_done`` — original semantics)."""
        self._stopped = True
        self._poll_queued = False
        self._spawn_deadline = None

    def enable_first_launch_grace(self, duration_ms: int = _FIRST_LAUNCH_GRACE_MS) -> None:
        """Suppress plan/replan triggers briefly after first open."""
        self._first_launch_grace = True
        self._grace_deadline = self._clock() + duration_ms

    def end_first_launch_grace(self) -> None:
        self._first_launch_grace = False
        self._grace_deadline = None

    def clear_plan_spawn_lock(self) -> None:
        """A plan/replan agent finished (e.g. WS chat_done)."""
        self._plan_spawn_at = 0
        self._plan_triggered = False
        self._plan_fail_count = 0
        self._plan_next_retry_at = 0

    def on_user_chat_success(self) -> None:
        """A chat round-trip proves the gateway is reachable: reset backoff."""
        if self._plan_fail_count > 0:
            logger.info("[QueuePoller] user chat succeeded — resetting plan backoff")
            self._plan_fail_count = 0
            self._plan_next_retry_at = 0

    def notify_agent_done(self, subagent_id: str | None = None) -> None:
        """Resolve the current spawn wait early if the id matches (or none is
        tracked/given)."""
        fut = self._spawn_future
        if fut is None or fut.done():
            return
        if not subagent_id or not self._current_spawn_id or subagent_id == self._current_spawn_id:
            self._current_spawn_id = None
            self._spawn_deadline = None
            fut.set_result(False)  # not timed out

    def tick(self, now_ms: int) -> None:
        """Fire due deadlines: the spawn timeout and the grace expiry."""
        if self._grace_deadline is not None and now_ms >= self._grace_deadline:
            self._first_launch_grace = False
            self._grace_deadline = None
        if (
            self._spawn_deadline is not None
            and now_ms >= self._spawn_deadline
            and self._spawn_future is not None
            and not self._spawn_future.done()
        ):
            self._spawn_deadline = None
            self._spawn_future.set_result(True)  # timed out

    # ── Poll entry points ──────────────────────────────────────────────────

    def execute_next(self) -> Awaitable[None] | None:
        """Immediate poll request (perform_pet_action). Respects idle pause."""
        if self._stopped:
            return None
        return self.poll()

    async def poll(self) -> None:
        """One serialized poll. Concurrent calls queue at most one follow-up."""
        if self._stopped:
            return
        if self._polling:
            self._poll_queued = True
            return
        self._polling = True
        try:
            await self._do_poll()
        except Exception:  # noqa: BLE001 — poll errors never kill the loop
            logger.exception("[QueuePoller] poll error")
        finally:
            self._polling = False
            if self._poll_queued and not self._stopped:
                self._poll_queued = False
                await self.poll()

    # ── Locks ──────────────────────────────────────────────────────────────

    def _is_plan_spawn_locked(self) -> bool:
        return (
            self._plan_spawn_at > 0 and self._clock() - self._plan_spawn_at < PLAN_SPAWN_TIMEOUT_MS
        )

    def _is_watch_spawn_locked(self) -> bool:
        return self._watch_spawn_at > 0 and self._clock() - self._watch_spawn_at < SPAWN_TIMEOUT_MS

    def _can_trigger_plan(self) -> bool:
        return (
            not self._plan_triggered
            and not self._is_plan_spawn_locked()
            and not self._first_launch_grace
            and self._plan_fail_count < MAX_PLAN_FAILS
            and self._clock() >= self._plan_next_retry_at
        )

    # ── Core poll ──────────────────────────────────────────────────────────

    async def _do_poll(self) -> None:  # noqa: C901 — mirrors the original's shape
        if self._stopped:
            return

        # 1. Read queue.
        # Off the loop: the queue grows with every `update_plan`, and this read +
        # JSON parse runs on the one-second poll — inline it stalls chat and the
        # heartbeat for as long as the parse takes.
        queue = await asyncio.to_thread(qf.read_queue, self._queue_path)

        # 2. Missing queue → trigger plan (first run / deleted file).
        if queue is None:
            if self._can_trigger_plan():
                self._plan_triggered = True
                self._plan_spawn_at = self._clock()
                try:
                    await self._callbacks.trigger_plan()
                except Exception:  # noqa: BLE001 — matches the original's bare catch
                    pass
            return

        # Release the plan lock when a fresh plan landed (quirk 5: `>=` with a
        # 1s allowance — agents write second-precision timestamps).
        if self._is_plan_spawn_locked() and queue.get("planned_at"):
            planned_ms = _epoch_ms(queue.get("planned_at"))
            if planned_ms is not None and planned_ms >= self._plan_spawn_at - 1000:
                self._plan_spawn_at = 0
                self._plan_triggered = False
                self._plan_fail_count = 0
                self._plan_next_retry_at = 0

        # Lock expired without a fresh plan → count a failure, back off, and
        # consume the whole poll (quirk 2).
        if not self._is_plan_spawn_locked() and self._plan_spawn_at > 0:
            self._plan_fail_count += 1
            backoff_ms = min(
                PLAN_BACKOFF_BASE_MS * (2 ** (self._plan_fail_count - 1)),
                _PLAN_BACKOFF_CAP_MS,
            )
            self._plan_next_retry_at = self._clock() + backoff_ms
            self._plan_spawn_at = 0
            self._plan_triggered = False
            logger.warning(
                "[QueuePoller] plan agent failed (attempt %d), next retry in %ds",
                self._plan_fail_count,
                round(backoff_ms / 1000),
            )
            return
        if self._plan_spawn_at == 0 and self._plan_triggered:
            self._plan_triggered = False
            return

        # 3. Due deterministic tasks. mood/notify: all; move: one per poll.
        now_ms = self._clock()
        due_tasks = qf.get_executable_tasks(queue, now_ms)
        queue_modified = False
        move_executed = False

        # Stale skip (quirk 1): planned move/mood > STALE_THRESHOLD_MS overdue
        # are marked done unexecuted; urgent tasks are exempt.
        for task in due_tasks:
            if task.get("type") in ("move", "mood") and not task.get("urgent"):
                exec_ms = _epoch_ms(task.get("execute_after"))
                if exec_ms is not None and now_ms - exec_ms > STALE_THRESHOLD_MS:
                    idx = self._find_task(queue, task.get("id"))
                    if idx != -1:
                        queue["tasks"][idx]["done"] = True
                        queue["tasks"][idx]["executed_at"] = _iso(now_ms)
                        queue_modified = True

        for task in due_tasks:
            if task.get("type") not in DETERMINISTIC_TYPES:
                continue
            if task.get("done"):
                continue  # may have been marked stale above
            if task.get("type") == "move":
                if move_executed:
                    continue
                move_executed = True
            try:
                await self._execute_deterministic_task(task)
                idx = self._find_task(queue, task.get("id"))
                if idx != -1:
                    queue["tasks"][idx]["done"] = True
                    queue["tasks"][idx]["executed_at"] = _iso(self._clock())
                    queue_modified = True
            except Exception:  # noqa: BLE001 — one bad task never stops the poll
                logger.exception(
                    "[QueuePoller] failed to execute deterministic task %s",
                    task.get("id"),
                )

        # 4. Watch checks — always before route branching; never blocked by
        #    plan cycles. Serial: one batch per poll, guarded by the lock.
        get_due = getattr(self._callbacks, "get_due_watch_items", None)
        if get_due is not None and not self._is_watch_spawn_locked():
            try:
                # Off the loop: these callbacks READ THE WATCHLIST FILE (up to
                # 10 MiB, parsed) on every poll. A callback is opaque to the
                # sweep that found the direct file reads, which is exactly why
                # these two survived it — an indirect read blocks the loop just
                # as much as a direct one.
                due_items = await asyncio.to_thread(get_due)
                if due_items:
                    now = self._clock()
                    budget = self._budget_provider() if self._budget_provider is not None else None
                    if budget is not None:
                        window_ms = 3_600_000  # the budget contract is per-hour
                        max_spawns = budget.max_spawns_per_hour
                        max_batch = budget.max_watch_batch
                    else:
                        # No provider, or the unlimited tier: the vendored
                        # constants apply unchanged (original behaviour).
                        window_ms = WATCH_STORM_WINDOW_MS
                        max_spawns = MAX_WATCH_SPAWNS_PER_WINDOW
                        max_batch = MAX_WATCH_BATCH
                    if now - self._watch_window_start > window_ms:
                        self._watch_window_start = now
                        self._watch_spawn_count = 0
                    if self._watch_spawn_count >= max_spawns:
                        # Quirk 6: throttle by taking the FULL lock, else this
                        # warning would repeat every poll for the whole window.
                        self._watch_spawn_at = self._clock()
                        logger.warning(
                            "[QueuePoller] watch spawn storm breaker: %d spawns "
                            "in %dmin window, throttling",
                            self._watch_spawn_count,
                            window_ms // 60_000,
                        )
                        on_exhausted = getattr(self._callbacks, "on_budget_exhausted", None)
                        if on_exhausted is not None:
                            # Once per window: the throttle above takes the full
                            # lock, so this branch cannot re-fire every poll.
                            # Writes the activity log, so off the loop with the
                            # rest. The other poller callbacks were audited and
                            # left inline deliberately: `on_agent_spawn_start` is
                            # a no-op, `on_agent_spawn_end` clears an in-memory
                            # lock, and `mark_watch_items_notified` only enqueues
                            # — its file write already happens on a worker.
                            await asyncio.to_thread(
                                on_exhausted, self._watch_window_start + window_ms
                            )
                    else:
                        self._watch_spawn_count += 1
                        batch = due_items[:max_batch]
                        prompt = build_watch_check_prompt(batch)
                        self._watch_spawn_at = self._clock()
                        await self._spawn_watch_check(prompt)
                        # Quirk 3: back-date to a 30s cooldown so the agent's
                        # update_watchlist can land before the next batch.
                        self._watch_spawn_at = self._clock() - SPAWN_TIMEOUT_MS + _WATCH_COOLDOWN_MS
            except Exception:  # noqa: BLE001 — quirk 4: keep the full lock
                logger.exception("[QueuePoller] watch check failed")
                # _watch_spawn_at was set before the spawn attempt; leaving it
                # makes the 5-min auto-expire the natural backoff.

        # 5. Idle guard before expensive spawns.
        if self._stopped:
            return

        # 5b. Missed-notification recovery (same lock as watch checks).
        get_missed = getattr(self._callbacks, "get_unnotified_watch_items", None)
        if get_missed is not None and not self._is_watch_spawn_locked():
            try:
                missed = await asyncio.to_thread(get_missed)
                if missed:
                    # Do NOT pre-mark here: the recovery agent marks each item
                    # (update_watchlist notified:true) only AFTER it notifies, so
                    # a spawn refusal/failure leaves the items unnotified and a
                    # later poll retries them — pre-marking would record a
                    # never-sent notification and permanently drop it. The
                    # _watch_spawn_at cooldown below throttles the retry, so a
                    # persistently failing spawn re-attempts on the cooldown
                    # cadence rather than in a tight loop.
                    prompt = build_missed_notify_prompt(missed)
                    self._watch_spawn_at = self._clock()
                    await self._spawn_watch_check(prompt)
                    self._watch_spawn_at = self._clock() - SPAWN_TIMEOUT_MS + _WATCH_COOLDOWN_MS
            except Exception:  # noqa: BLE001
                logger.exception("[QueuePoller] missed-notification recovery failed")

        # 6. Route check — plan / replan / execute.
        route = qf.route_poll(queue, now_ms=self._clock())
        if route in ("plan", "replan"):
            if queue_modified:
                queue = qf.cleanup_done_tasks(queue, now_ms=self._clock())
                try:
                    # Off the event loop: the lock is held across processes (the
                    # MCP server writes the same file), so blocking on it here
                    # would freeze chat and the heartbeats for as long as the
                    # other process holds it. Use the MERGE write (not a plain
                    # clobber): it re-reads under the lock and honours a concurrent
                    # reset/full_replace by writing nothing, so a stale pre-reset
                    # snapshot can't resurrect the deleted queue and re-run tasks.
                    await asyncio.to_thread(self._locked_merge_write, queue)
                except OSError:
                    pass
            if self._can_trigger_plan():
                self._plan_triggered = True
                self._plan_spawn_at = self._clock()
                try:
                    if route == "plan":
                        await self._callbacks.trigger_plan()
                    else:
                        await self._callbacks.trigger_replan()
                except Exception:  # noqa: BLE001
                    pass
            return

        # 7. route == 'execute': freestyle agent tasks, serially.
        for task in due_tasks:
            if task.get("type") not in AGENT_TYPES:
                continue
            try:
                await self._spawn_agent_task_serial(task)
            except Exception:  # noqa: BLE001
                logger.exception("[QueuePoller] failed to spawn agent task %s", task.get("id"))

        # 8. Cleanup + write-back, merged onto a FRESH read so tasks written
        #    by agents during steps 4-7 are not clobbered.
        # The lock closes the remaining hole: an MCP append landing between
        # the fresh read below and the atomic replace at the end would be
        # dropped by that replace (see queue_file.queue_mutation).
        await asyncio.to_thread(self._locked_merge_write, queue)

    def _locked_write(self, queue: Any) -> None:
        """Atomic write under the cross-process queue lock. Blocking — call via
        ``asyncio.to_thread``."""
        with qf.queue_mutation(self._queue_path):
            qf.write_queue_atomic(self._queue_path, queue)

    def _locked_merge_write(self, queue: Any) -> None:
        """Merge done-markers onto a FRESH read, then replace, all under the
        lock. Blocking — call via ``asyncio.to_thread``."""
        with qf.queue_mutation(self._queue_path):
            fresh_queue = qf.read_queue(self._queue_path)
            if fresh_queue is None:
                # The queue was reset/deleted concurrently (a full_replace or a
                # reset cleared it between our poll snapshot and this locked
                # write). Merging done-markers onto — and writing back — our STALE
                # pre-reset snapshot would RESURRECT exactly the tasks the reset
                # just removed. Honour the reset: write nothing.
                return
            for stale_task in queue.get("tasks", []):
                if stale_task.get("done"):
                    fresh = next(
                        (
                            t
                            for t in fresh_queue.get("tasks", [])
                            if t.get("id") == stale_task.get("id")
                        ),
                        None,
                    )
                    if fresh is not None and not fresh.get("done"):
                        fresh["done"] = True
                        fresh["executed_at"] = stale_task.get("executed_at")
            queue = qf.cleanup_done_tasks(fresh_queue, now_ms=self._clock())
            try:
                qf.write_queue_atomic(self._queue_path, queue)
            except OSError:
                logger.exception("[QueuePoller] failed to write queue")

    # ── Task execution ─────────────────────────────────────────────────────

    @staticmethod
    def _find_task(queue: dict[str, Any], task_id: Any) -> int:
        return next(
            (i for i, t in enumerate(queue.get("tasks", [])) if t.get("id") == task_id),
            -1,
        )

    async def _execute_deterministic_task(self, task: dict[str, Any]) -> None:
        # Planners often write notify/move/mood fields at the top level rather
        # than inside `action`; merge task < action < payload so callbacks
        # always receive the full bag.
        action = {**task, **(task.get("action") or {}), **(task.get("payload") or {})}
        kind = task.get("type")
        if kind == "move":
            await self._callbacks.on_move(action)
        elif kind == "notify":
            await self._callbacks.on_notify(action)
        elif kind == "mood":
            await self._callbacks.on_mood(action)

    async def _await_spawn(self) -> bool:
        """Wait for notify_agent_done (early) or the tick-fired timeout.
        Returns True when the wait ended by timeout."""
        loop = asyncio.get_event_loop()
        self._spawn_future = loop.create_future()
        self._spawn_deadline = self._clock() + SPAWN_TIMEOUT_MS
        try:
            return await self._spawn_future
        finally:
            self._spawn_future = None
            self._spawn_deadline = None

    async def _spawn_agent_task_serial(self, task: dict[str, Any]) -> None:
        """Spawn a freestyle task's agent and wait; on timeout, mark failure
        and queue the sticky fail-notify after MAX_FAIL_COUNT."""
        task_id = task.get("id")
        prompt = build_agent_prompt(task)

        on_start = getattr(self._callbacks, "on_agent_spawn_start", None)
        if on_start is not None:
            on_start()

        try:
            spawn_id = await self._callbacks.spawn_agent(prompt)
        except Exception:  # noqa: BLE001
            logger.exception("[QueuePoller] spawnAgent failed for %s", task_id)
            on_end = getattr(self._callbacks, "on_agent_spawn_end", None)
            if on_end is not None:
                on_end()
            return

        self._current_spawn_id = spawn_id or None
        timed_out = await self._await_spawn()

        on_end = getattr(self._callbacks, "on_agent_spawn_end", None)
        if on_end is not None:
            on_end()

        if timed_out:
            try:
                # Off the event loop: this waits on a cross-process lock.
                await asyncio.to_thread(self._locked_mark_timeout, task_id)
            except OSError:
                logger.exception("[QueuePoller] spawn timeout handler error for %s", task_id)

    async def _spawn_watch_check(self, prompt: str) -> None:
        """Spawn a watch/recovery agent and wait — same serial mechanism,
        no queue-task bookkeeping."""
        on_start = getattr(self._callbacks, "on_agent_spawn_start", None)
        if on_start is not None:
            on_start()

        try:
            spawn_id = await self._callbacks.spawn_agent(prompt)
        except Exception:
            logger.exception("[QueuePoller] watch check spawnAgent failed")
            on_end = getattr(self._callbacks, "on_agent_spawn_end", None)
            if on_end is not None:
                on_end()
            # Re-raise so the caller's 30s-cooldown shortcut is skipped and
            # the full lock stands (quirk 4).
            raise

        self._current_spawn_id = spawn_id or None
        await self._await_spawn()

        on_end = getattr(self._callbacks, "on_agent_spawn_end", None)
        if on_end is not None:
            on_end()

    def _locked_mark_timeout(self, task_id: Any) -> None:
        """Mark a timed-out spawn task failed under the queue lock.

        Blocking — call via ``asyncio.to_thread``.
        """
        with qf.queue_mutation(self._queue_path):
            q = qf.read_queue(self._queue_path)
            if q is None:
                return
            t = next((x for x in q.get("tasks", []) if x.get("id") == task_id), None)
            if t is None or t.get("done"):
                return  # completed in time after all

            t["failed"] = True
            t["fail_count"] = (t.get("fail_count") or 0) + 1

            if t["fail_count"] >= MAX_FAIL_COUNT:
                q["tasks"].append(
                    {
                        "id": f"fail-notify-{task_id}-{self._clock()}",
                        "type": "notify",
                        "action": {
                            "summary": (
                                f"Task {task_id} failed {t['fail_count']} " "times. Please check."
                            ),
                            "sticky": True,
                        },
                        "execute_after": _iso(self._clock()),
                        "done": False,
                        "source": "system",
                        "category": "error",
                    }
                )

            qf.write_queue_atomic(self._queue_path, q)
