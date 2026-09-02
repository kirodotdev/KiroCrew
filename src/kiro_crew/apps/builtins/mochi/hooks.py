"""Mochi builtin — lifecycle hooks and the owner loop.

The ported modules are all OWNER-DRIVEN: none of them runs a timer of its
own; each exposes ``poll()`` / ``guard()`` / ``tick(now_ms)`` and expects
somebody with a loop to call them on a cadence. This file is that somebody.

``on_startup`` (dispatched by ``LifecycleDispatcher`` at gateway start AND on
app enable) builds the service graph and starts one asyncio task; the loop
wakes every second and drives:

* ``QueuePoller.poll()``  — every POLL_INTERVAL_MS (1s)
* ``QueuePoller.tick``, ``PinnedFilesService.tick``, ``StatsService.tick``,
  ``PetStateManager.tick`` — every wake (deadline-based, cheap no-ops)
* ``WatchlistService.guard()`` — every GUARD_INTERVAL_MS (5 min)

``on_shutdown`` (gateway stop AND app disable) cancels the task, flushes
stats, and disposes everything. Both hooks are idempotent: enabling an
already-started app or disabling a stopped one is a no-op, so the dispatcher
can call them freely.

SPAWN SEAM: the poller's ``spawn_agent`` callback needs the gateway's
``SubagentManager``, which is not part of ``AppContext``. It is LATE-BOUND
via :func:`set_spawn_fn` (and always targets the background agent,
``mochi-bg`` — never the foreground chat agent) — until the gateway (or a
test) injects one, spawn attempts raise and the ported failure paths take over
exactly as designed
(watch lock backoff, plan backoff, degraded direct-notify). The pet therefore
degrades gracefully rather than half-working: deterministic tasks, stats,
pinned files and archiving all run without any spawner.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import os
import re
import time
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable

from kiro_crew.apps.builtins.mochi import activity_log
from kiro_crew.apps.builtins.mochi import watchlist_file as wf
from kiro_crew.apps.builtins.mochi.activity_budget import SpawnLedger, resolve_activity_budget
from kiro_crew.apps.builtins.mochi.agent_policy import apply_policy
from kiro_crew.apps.builtins.mochi.idle_manager import (
    IDLE_CHECK_INTERVAL_MS,
    IdleManager,
)
from kiro_crew.apps.builtins.mochi.notification_gate import NotificationGate
from kiro_crew.apps.builtins.mochi.pet_state_manager import PetStateManager
from kiro_crew.apps.builtins.mochi.pinned_files_service import PinnedFilesService
from kiro_crew.apps.builtins.mochi.queue_file import QUEUE_FILE as _QUEUE_FILE
from kiro_crew.apps.builtins.mochi.queue_file import _epoch_ms
from kiro_crew.apps.builtins.mochi.queue_poller import (
    POLL_INTERVAL_MS,
    SPAWN_TIMEOUT_MS,
    QueuePoller,
)
from kiro_crew.apps.builtins.mochi.redact import redact_tree
from kiro_crew.apps.builtins.mochi.settings import load_settings
from kiro_crew.apps.builtins.mochi.soul_loader import (
    SoulLoader,
    load_skill_line,
    write_agent_prompts,
)
from kiro_crew.apps.builtins.mochi.stats_service import StatsService
from kiro_crew.apps.builtins.mochi.watchlist_service import (
    GUARD_INTERVAL_MS,
    REMINDER_INTERVAL_MS,
    WatchlistService,
)

logger = logging.getLogger(__name__)

#: How often the completion watcher probes a running background agent.
_SPAWN_PROBE_INTERVAL_SECS = 2.0

_PRESENCE_FRESH_MS = 90_000  # 3 missed 30s heartbeats = pet gone
_SHELL_FRESH_MS = 120_000  # 4 missed 30s heartbeats = shell closed


SpawnFn = Callable[[str, str], Awaitable[str]]

# Background work runs as this dedicated agent, NEVER the foreground chat agent
# ('mochi'): mochi-bg's managedToolPolicy bars spawn_run/cron so a watch/plan
# spawn can't fan out, and its output is injected into the foreground context
# rather than answering the user. The seam carries the agent name so the
# gateway wiring targets it explicitly instead of guessing a default.
#
# NAMES ARE A FLAT GLOBAL NAMESPACE: kiro-cli resolves an agent by the "name"
# field inside the JSON, NOT by the (namespaced) link filename the app bridge
# writes into ~/.kiro/agents/. Two installed agents claiming the same name
# collide silently -- kiro-cli warns and picks one. Keep these prefixed with the
# app id so a public install can never squat a generic name.
BG_AGENT_NAME = "mochi-bg"

# Late-bound spawn seam (see module docstring).
_spawn_fn: SpawnFn | None = None

# The one live runtime per gateway process (None when disabled/stopped).
_runtime: "MochiRuntime | None" = None


def set_spawn_fn(fn: SpawnFn | None) -> None:
    """Inject the agent-spawn implementation (gateway wiring or tests)."""
    global _spawn_fn
    _spawn_fn = fn


async def _spawn_agent(prompt: str) -> str:
    if _spawn_fn is None:
        raise RuntimeError("mochi: no spawn function bound")
    return await _spawn_fn(prompt, BG_AGENT_NAME)


def _now_ms() -> int:
    return int(time.time() * 1000)


class _PollerCallbacks:
    """QueuePoller sink bag: deterministic actions publish app events; agent
    spawns go through the late-bound seam; plan/replan are freestyle spawns
    with the planner skill (full prompt wiring lands with Phase D/MCP)."""

    def __init__(self, runtime: "MochiRuntime") -> None:
        self._rt = runtime

    async def on_move(self, action: dict[str, Any]) -> None:
        self._rt.publish("mochi:move", action)

    async def on_notify(self, action: dict[str, Any]) -> None:
        self._rt.notify_gate.push(action, _now_ms())

    async def on_mood(self, action: dict[str, Any]) -> None:
        # The pet state manager owns mood now (single source of truth + transient
        # auto-reset) and broadcasts mochi:mood itself. The planner writes the
        # mood as action["mood"] or action["value"] (mirrors the original
        # index.ts onMood).
        mood = action.get("mood") or action.get("value")
        if mood:
            # Mood is agent-authored free text that surfaces in the stats/mood
            # label in the browser — scrub credentials/exfiltration URLs first.
            self._rt.state_manager.set_mood(redact_tree(str(mood)), _now_ms())

    async def trigger_plan(self) -> None:
        await _spawn_agent(self._rt.plan_prompt())

    async def trigger_replan(self) -> None:
        await _spawn_agent(self._rt.replan_prompt())

    async def spawn_agent(self, task_prompt: str) -> str:
        return await _spawn_agent(task_prompt)

    def get_due_watch_items(self) -> list[dict[str, Any]]:
        return self._rt.due_watch_items()

    def get_unnotified_watch_items(self) -> list[dict[str, Any]]:
        return self._rt.unnotified_watch_items()

    def mark_items_notified(self, ids: list[Any]) -> None:
        self._rt.mark_items_notified(ids)

    def on_agent_spawn_start(self) -> None:
        pass

    def on_budget_exhausted(self, resume_at_ms: int) -> None:
        # Deliberately NOT gated on silentSubagents: a silently degraded
        # autonomous side is the worst failure mode this feature has — the user
        # must be able to see WHY checks stopped and when they resume.

        resume = _dt.datetime.fromtimestamp(resume_at_ms / 1000).strftime("%H:%M")
        # Through the gate like every notify: during quiet mode the bubble
        # waits, but the durable record (the activity line below) is written
        # immediately — the "visible somewhere durable" requirement holds.
        self._rt.notify_gate.push(
            {
                "action": "notify",
                "summary": f"Background budget for this hour is used up — next check after {resume}.",
                "mood": "sleepy",
                "source": "system",
            },
            _now_ms(),
        )
        self._rt._log_activity("budget", f"hourly spawn budget exhausted, resumes {resume}")

    def on_agent_spawn_end(self) -> None:
        # "Called by QueuePoller or WS event when a watchlist-related agent
        # finishes" — the poller finished its serial wait, release the
        # watchlist service's timestamp lock.
        self._rt.watchlist.clear_spawn_lock()


class MochiRuntime:
    """Service graph + owner loop for one enabled Mochi instance."""

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        # Kept on the instance so route handlers (settings) can reach it without
        # re-deriving it from the app context.
        self.data_dir = Path(ctx.data_dir)
        data_dir = str(ctx.data_dir)
        self._watchlist_path = os.path.join(data_dir, "mochi-watchlist.json")
        # Last seen watchlist file mtime (ns). The MCP server is a SEPARATE
        # process writing this file directly, so its writes never pass through
        # WatchlistService's on_write_complete — without an mtime watch the
        # panel only learns about agent-created items on its slow poll.
        self._watchlist_mtime_ns = self._stat_mtime_ns(self._watchlist_path)
        self.stats = StatsService(data_dir, log_activity=self._log_activity)
        # Recent accepted chat pushes, for notify_user's re-notify guard:
        # (content, accepted_at_ms), newest last. Capped at the original's
        # recent-15 scan depth; the TIME window comes from the user's
        # `quietPeriodMins` setting at push time.
        self._recent_chat_pushes: deque[tuple[str, int]] = deque()
        # Foreground chat-turn awareness for the conversation-interleave gate
        # (see note_chat_lifecycle / _push_to_chat). An ambient pet push must
        # not land between a user's message and the agent's reply, so a
        # non-critical push is deferred while a turn is in flight (or within a
        # short grace window after the last user input) and flushed when the
        # turn ends. Fed by the /pet-event route — the SAME foreground-only
        # signal that already drives the pet's thinking/working animation.
        self._chat_turn_active = False
        self._last_user_input_ms = 0
        self._deferred_chat_pushes: list[dict[str, Any]] = []
        # Budget is re-resolved on every read so a tier change in Settings
        # takes effect on the next poll — same live-read contract as
        # silentSubagents. The ledger is the budget's persisted memory AND
        # the usage display's data source (one file, so the number shown can
        # never disagree with the number limited).
        self.spawn_ledger = SpawnLedger(data_dir)
        self.pinned = PinnedFilesService(data_dir, self._broadcast)
        self.state_manager = PetStateManager(self._broadcast, stats=self.stats)
        # Every non-chat notification passes through the gate on its way to
        # notify_user: bursts coalesce (the bubble surface REPLACES its text,
        # so back-to-back notifies would drop all but the last), quiet mode
        # holds the backlog, `critical` bypasses both. Ticked by the owner
        # loop; delivery below the gate is the unchanged direct fan-out.
        self.notify_gate = NotificationGate(self.notify_user)
        self.soul = SoulLoader()
        # The appearance decides the personality, so it must be applied before
        # the first agent spawn — otherwise the pet's opening turns run as a
        # generic companion and only adopt their character after a restart.
        self.soul.set_appearance(*self._stored_appearance())
        # Order matters: set_appearance picks the pack's default name, so an
        # explicit user name must be applied AFTER it to win.
        stored_name = self._stored_pet_name()
        if stored_name:
            self.soul.set_pet_name(stored_name)
        # Render the agent prompt NOW, before anything can spawn. The prompt is
        # generated (it carries the pet's name and its appearance's persona), so it
        # cannot ship inside the packaged agent template — the framework picks it up
        # from the app policy, which points at this file.
        self._write_agent_prompt()
        self.idle = IdleManager(_IdleCallbacks(self))
        self.watchlist = WatchlistService(
            _WatchlistCallbacks(self), data_dir, on_write_complete=self._watchlist_changed
        )
        self.poller = QueuePoller(
            os.path.join(data_dir, _QUEUE_FILE),
            _PollerCallbacks(self),
            clock=_now_ms,
            budget_provider=self.activity_budget,
        )
        self._task: asyncio.Task[None] | None = None
        # Strong refs to in-flight polls (asyncio may GC unreferenced tasks);
        # poll() serializes itself internally, so at most one runs the body.
        self._poll_tasks: set[asyncio.Task[None]] = set()
        self._next_guard = 0
        self._next_idle_check = 0
        # Pet-window presence heartbeat (see presence_beat). 0 = never seen.
        self._last_presence_ms = 0
        self._was_present = False
        self._last_shell_beat_ms = 0
        self._shell_was_on = False
        self._next_reminder = 0

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return  # idempotent: already running
        now = _now_ms()
        # stats.load/pinned.load are disk reads AND seed the in-memory structure
        # that synchronous stats/pin access depends on — so they must complete
        # before start() returns, but run OFF the event loop so enabling/disabling
        # Mochi never stalls gateway requests or heartbeats. Awaited via
        # to_thread: startup is async (the lifecycle dispatcher awaits the hook).
        await asyncio.to_thread(self.stats.load, now)
        self.stats.record_app_launch(now)
        await asyncio.to_thread(self.pinned.load, now)
        # The autonomous core is now live in-process; leave the cold-start
        # 'offline' so the panel title bar and pet reflect a running companion
        # (only 'connect' exits offline — verified against petStateManager.ts).
        self.state_manager.apply_event("connect", now)
        self.poller.start()
        self._next_guard = now + GUARD_INTERVAL_MS
        self._next_idle_check = now + IDLE_CHECK_INTERVAL_MS
        self._next_reminder = now + REMINDER_INTERVAL_MS
        # create_task, NOT get_event_loop().create_task: with no running loop the
        # latter CREATES one, schedules the owner loop onto it, and returns —
        # nobody ever runs that loop, so the entire autonomous side is silently
        # dead with no error anywhere. create_task raises RuntimeError instead,
        # which is the honest outcome for "started outside the gateway's loop".
        # (get_event_loop is also deprecated from 3.12 and raises in 3.14.)
        self._task = asyncio.create_task(self._loop())
        logger.info("[mochi] runtime started")

    async def stop(self) -> None:
        if self._task is None:
            return  # idempotent
        task, self._task = self._task, None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # Cancelling the owner loop is not enough: it HANDS WORK OFF (poll() is
        # fire-and-forget, and a spawn watcher lives for up to SPAWN_TIMEOUT_MS).
        # Those tasks call back into the very services torn down below — mood
        # onto a destroyed state manager, counters after the stats flush,
        # notifications for an app that is now disabled. On a disable→enable
        # cycle they would land in a graph the next start() already replaced.
        await self._drain_handoff_tasks()
        now = _now_ms()
        self.poller.stop()
        await asyncio.to_thread(self.stats.flush, now)
        self.pinned.dispose()
        # The core is going away — reflect it in the shared state before tearing
        # down the manager's timers ('disconnect' → offline from any state).
        self.state_manager.apply_event("disconnect", now)
        self.state_manager.destroy()
        logger.info("[mochi] runtime stopped")

    async def _drain_handoff_tasks(self) -> None:
        """Cancel and await every task the owner loop handed off.

        ``return_exceptions=True``: one task failing must not stop the others
        from being awaited, and a shutdown path is the wrong place to raise.
        """
        pending = list(self._poll_tasks)
        self._poll_tasks.clear()
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _loop(self) -> None:
        """The owner loop: 1s cadence, deadline fan-out to every service."""
        while True:
            await asyncio.sleep(POLL_INTERVAL_MS / 1000)
            now = _now_ms()
            try:
                # Cheap deadline-based ticks first (pure in-memory).
                self.poller.tick(now)
                # Time-based backstop for the conversation-interleave gate:
                # deliver any chat push stranded past the busy window (a push
                # buffered after the terminal flush, or a turn whose flag aged
                # out with no terminal event). Runs every wake, ungated by shell
                # presence — a deferred push must still drain when Mochi is not
                # on screen. Pure in-memory unless there is a backlog to flush.
                self._drain_deferred_chat_pushes(now)
                # The pinned-file and watchlist ticks below each do blocking
                # filesystem work — an os.stat per watched path, plus an
                # atomic_write when a debounce fires — under a cross-process lock,
                # so on a slow or contended filesystem they would stall chat
                # streaming and the heartbeat for as long as the disk (or the
                # other process holding the lock) takes. Offloaded, exactly like
                # the pin mutations the route handlers offload. Kept as separate
                # awaits so the documented deadline ORDER is preserved (each
                # completes before the next starts, so there is also no in-process
                # race on the owner loop's private watcher state). The broadcasts
                # these fire go through `publish`, which the route-handler path
                # already invokes from a worker thread, so firing them off-loop
                # here is not new.
                #
                # Feeds the pinned-file watcher (mtime poll stands in for the
                # original's fs.watch — see poll_file_changes).
                await asyncio.to_thread(self.pinned.poll_file_changes, now)
                await asyncio.to_thread(self.pinned.tick, now)
                await asyncio.to_thread(self._poll_watchlist_file)
                # Companion time counts only while the pet window is ON
                # SCREEN. In the original the uptime ticker lived in the pet
                # app's own process, so "app running" WAS "pet present"; this
                # loop lives in the gateway, which never exits — an ungated
                # tick here counts server uptime as companionship (a 24/7
                # gateway would read "together 24h every day"). On return the
                # accrual origin resets so the away gap is never credited (the
                # original never counted gaps either: a closed app simply had
                # no ticker running).
                present = self.pet_present(now)
                if present and not self._was_present:
                    self.stats.reset_uptime_origin(now)
                self._was_present = present
                if present:
                    # Writes stats to disk when the flush debounce fires — same
                    # off-loop treatment as the pinned ticks above.
                    await asyncio.to_thread(self.stats.tick, now)
                self.state_manager.tick(now)
                # Merge-window / quiet-mode deadlines ride the same cadence —
                # no extra task (same pattern as every tick above).
                self.notify_gate.tick(now)
                if now >= self._next_idle_check:
                    self._next_idle_check = now + IDLE_CHECK_INTERVAL_MS
                    # System idle seconds arrive from the shell (C track);
                    # until wired, 0 keeps the manager in its active state.
                    self.idle.check_idle(self._shell_idle_secs())
                shell_on = self.shell_present(now)
                if shell_on != self._shell_was_on:
                    self._shell_was_on = shell_on
                    if shell_on:
                        # Mochi just became reachable -- the builtin's equivalent
                        # of the original's "app opened". The grace suppresses
                        # plan/replan briefly so the FIRST thing the poller does
                        # is drain queued move/notify tasks; without it the first
                        # poll spawns a planner that holds the serial spawn lock,
                        # and the pet cannot walk or bubble until that agent
                        # returns (everything then arrives at once).
                        self.poller.enable_first_launch_grace()
                    # The pause must be VISIBLE somewhere durable — a silently
                    # frozen autonomous side reads as breakage, not battery
                    # saving. The activity log is the one surface that exists
                    # without the shell.
                    self._log_activity(
                        "presence",
                        (
                            "background work resumed — Mochi is open"
                            if shell_on
                            else "background work paused — Mochi is not running"
                        ),
                    )
                # Queue, watch checks, plans, and reminders all stop with the
                # shell (original semantics: a closed app had none of them).
                # The watchlist guard below stays on: file hygiene, no spawns,
                # no user-visible output.
                if not self.idle.is_paused and shell_on:
                    # FIRE-AND-FORGET, matching the original's
                    # `setInterval(() => void this.poll())`. Awaiting here
                    # deadlocks the whole autonomous side: a watch-check poll
                    # blocks inside _await_spawn, whose TIMEOUT is fired by
                    # poller.tick() -- which runs in THIS loop, now starved by
                    # the await. Every queued move/notify then freezes until
                    # the gateway restarts. poll() itself serializes concurrent
                    # entries, so overlapping calls are safe by design.
                    poll_task = asyncio.create_task(self.poller.poll())
                    self._poll_tasks.add(poll_task)
                    poll_task.add_done_callback(self._poll_tasks.discard)
                if shell_on and now >= self._next_reminder:
                    self._next_reminder = now + REMINDER_INTERVAL_MS
                    await self.watchlist.check_reminders(now)
                if now >= self._next_guard:
                    self._next_guard = now + GUARD_INTERVAL_MS
                    await self.watchlist.guard(now)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — the loop must survive anything
                logger.exception("[mochi] owner loop iteration failed")

    def _write_agent_prompt(self) -> None:
        """Render BOTH agents' system prompts from the CURRENT identity.

        Best-effort: a failure here must not stop the runtime from starting. The
        cost of not having it is a pet that answers to its previous name until the
        next save, which is far better than no pet at all.
        """
        try:

            write_agent_prompts(self.data_dir, self.soul.pet_name, self.soul.get())
        except Exception:  # noqa: BLE001 — never block startup on the prompt
            logger.warning("[mochi] could not render the agent prompt", exc_info=True)

    def _stored_pet_name(self) -> str:
        """The user's chosen pet name, or "" to follow the pack's own name."""
        try:

            value = load_settings(self.data_dir).get("petName")
            return value if isinstance(value, str) else ""
        except Exception:  # noqa: BLE001 — settings must never block startup
            return ""

    def _stored_appearance(self) -> tuple[str | None, str | None]:
        """The active appearance pack id (the persona description is NEVER
        sourced from a pack).

        A pack's ``meta.description`` is author-controlled and, for an imported
        pack, downloaded from an untrusted source — feeding it into the persona
        puts attacker-controlled text into the agent's SYSTEM PROMPT, a prompt-
        injection vector. So no pack contributes description text to the persona:
        built-ins carry curated persona keyed by pack id, and imported packs get
        the generic companion persona (the pack still renders visually — only the
        auto-derived persona text is dropped).

        Never raises: an unreadable settings file must not stop the runtime from
        starting, and ``(None, None)`` reads as a generic companion.
        """
        try:

            pack_id = load_settings(self.data_dir).get("activeAppearance")
            if not isinstance(pack_id, str) or not pack_id:
                return (None, None)
            return (pack_id, None)
        except Exception:  # noqa: BLE001 — settings must never block startup
            logger.warning("[mochi] could not read the active appearance")
            return (None, None)

    # ── Shell-fed events ──────────────────────────────────────────────────

    def on_power_event(self, event: str) -> None:
        """Relay a macOS power event into the idle manager.

        The original subscribed Electron's ``powerMonitor`` directly
        (lock-screen / unlock-screen / suspend / resume) to pause the queue
        poller and watchlist while the user is away. In the builtin split the
        shell owns those events and the autonomous core lives here, so the shell
        POSTs them and this relays them on.

        This is what stops the pet from polling every second and spawning agents
        while the laptop lid is shut — without it the "pause when away" behavior
        is silently absent.

        Unknown event names are ignored by the idle manager.
        """
        self.idle.handle_power_event(event)

    # ── Seams the callback bags use (fleshed out with routes / Phase D) ───

    def publish(self, event_type: str, data: dict[str, Any]) -> None:
        events = getattr(self._ctx, "events", None)
        if events is not None:
            events.publish_to_app(event_type, data)

    def _broadcast(self, channel: str, *args: Any) -> None:
        # Broadcast args are agent-authored (app SDK ctx.broadcast) and reach the
        # browser raw -- scrub credentials/exfiltration URLs at the egress sink.
        self.publish(channel, redact_tree({"args": list(args)}))

    def watch_spawn_completion(self, spawn_id: str, spawn: Any) -> None:
        """Release the poller's serial spawn wait as soon as the agent is done.

        Probes ``spawn.is_done`` every few seconds and forwards the completion
        to ``poller.notify_agent_done`` (the original resolved the same wait
        from its WS subagent_done event). Stops on its own at the poller's
        spawn timeout — past that the tick-fired timeout has already resolved
        the wait, so there is nothing left to release.
        """
        if not spawn_id:
            return

        # The autonomous side is the OTHER driver of the pet's behaviour state.
        # Upstream showed the pet working while a background check ran; the chat
        # surface reports its own lifecycle over /pet-event, and this is the same
        # transition for work the user never typed.
        self.state_manager.apply_event("task_start", _now_ms())

        async def _watch() -> None:
            deadline = _now_ms() + SPAWN_TIMEOUT_MS
            while _now_ms() < deadline:
                await asyncio.sleep(_SPAWN_PROBE_INTERVAL_SECS)
                try:
                    done = spawn.is_done(spawn_id)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — see below
                    # is_done is a host capability call. Unguarded, one raise
                    # killed this watcher with an unretrieved exception AND left
                    # the poller's serial spawn wait to expire on the full
                    # timeout — silently reverting to the exact stall this
                    # early-release exists to prevent. Log once and stop
                    # probing; the tick-fired timeout still resolves the wait.
                    logger.warning(
                        "[mochi] spawn completion probe failed for %s; "
                        "falling back to the spawn timeout",
                        spawn_id,
                        exc_info=True,
                    )
                    # Do not leave the pet stuck mid-task because a PROBE broke.
                    self.state_manager.apply_event("task_complete", _now_ms())
                    return
                if done:
                    self.state_manager.apply_event("task_complete", _now_ms())
                    self.poller.notify_agent_done(spawn_id)
                    return
            # Timed out: the wait is resolved elsewhere, but the state still has
            # to come back down or the pet would work forever.
            self.state_manager.apply_event("task_complete", _now_ms())

        task = asyncio.create_task(_watch())
        self._poll_tasks.add(task)
        task.add_done_callback(self._poll_tasks.discard)

    def _log_activity(self, kind: str, content: str) -> None:
        # Persisted, not just logged: the mochi-plan skill reads this file to
        # avoid repeating itself, and the dashboard's Recent Activity card is a
        # view over it. A logger-only sink left both permanently empty.
        logger.info("[mochi:activity] %s: %s", kind, content)
        # The persist is a LOCKED read-modify-write of the log file — cheap on a
        # fast disk, but on the event loop a slow or contended disk would stall
        # chat and the heartbeat. _log_activity is called from BOTH loop paths
        # (the owner loop, notify delivery, the spawn watcher) and already-threaded
        # ones (the StatsService callback runs inside stats.tick's to_thread). So
        # offload only when a loop is actually running; otherwise write inline.
        # Fire-and-forget: ordering is preserved by the file lock in log_activity,
        # and a dropped activity line must never block the loop.
        try:
            _loop = asyncio.get_running_loop()
        except RuntimeError:
            _loop = None
        if _loop is not None:
            _fut = _loop.run_in_executor(
                None, activity_log.log_activity, self.data_dir, kind, content
            )
            _fut.add_done_callback(lambda f: f.exception())  # consume best-effort errors
        else:
            activity_log.log_activity(self.data_dir, kind, content)

    def _watchlist_changed(self) -> None:
        # Carry the ITEMS, matching the original's
        # `broadcastToRenderers('watchlist:changed', wl.items)`. A bare signal
        # forced every listener into an HTTP refetch, and that extra async
        # round-trip lands mid-animation -- the panel replaced its list while a
        # row was expanding, which is the flicker.

        try:
            wl = wf.read_watchlist(self._watchlist_path, now_ms=_now_ms())
            items = wl.get("items", [])
        except Exception:  # noqa: BLE001 -- a read failure must not skip the signal
            items = []
        # Watch items carry agent-authored titles/labels -> browser event; scrub.
        self.publish("mochi:watchlist-changed", redact_tree({"items": items}))

    @staticmethod
    def _stat_mtime_ns(path: str) -> int:
        try:
            return os.stat(path).st_mtime_ns
        except OSError:
            return 0

    def _poll_watchlist_file(self) -> None:
        """Publish watchlist-changed for writes made by OTHER processes.

        Self-writes also move the mtime, so those publish twice (push +
        this) — a harmless extra refresh, cheaper than write attribution.
        """
        mtime = self._stat_mtime_ns(self._watchlist_path)
        if mtime != self._watchlist_mtime_ns:
            self._watchlist_mtime_ns = mtime
            self._watchlist_changed()

    #: How many accepted chat pushes the re-notify guard scans. Mirrors the
    #: original's `chatHistory.getRecent(15)` scan depth.
    _CHAT_PUSH_SCAN_DEPTH = 15

    #: Grace window after the last user input during which a non-critical chat
    #: push is still deferred, so a rapid back-and-forth reads as one live
    #: conversation rather than a gap the pet can barge into. The observed
    #: interleave landed ~4s before the next user turn began, inside exactly
    #: such a gap — an "in-flight only" check would have missed it.
    _CHAT_ACTIVE_GRACE_MS = 8_000

    #: Hard ceiling on how long a single ``user_input`` keeps the turn "active".
    #: ``_chat_turn_active`` is set by ``user_input`` and cleared only by a
    #: terminal chat event on ``/pet-event`` (posted from the panel's WebSocket
    #: chat_done handler). If the panel closes mid-turn or the socket drops
    #: before chat_done, that terminal event never arrives and the flag would
    #: wedge True for the runtime's lifetime — every non-critical push then
    #: defers forever and the capped buffer silently discards the oldest. Bound
    #: it by the timestamp already recorded: past this age the turn is treated
    #: as ended even with the flag still set, so the drain can deliver the
    #: backlog. Ten minutes comfortably exceeds any real single agent turn.
    _CHAT_TURN_MAX_MS = 600_000

    def set_quiet(self, minutes: int) -> int:
        """Enter (minutes > 0) or leave (0) quiet mode. Returns silent-until ms.

        The user-facing DND control: entering holds non-critical notifications
        in the gate; leaving flushes the backlog merged. State is broadcast as
        ``mochi:quiet`` so an open overlay updates; the menu also re-reads it
        via ``/pet-state`` on open, which covers natural expiry (tick clears
        the deadline without a broadcast).
        """
        now = _now_ms()
        if minutes > 0:
            self.notify_gate.silence(now, minutes)
            self._log_activity("system", f"notifications quiet for {minutes} min")
        else:
            self.notify_gate.unsilence(now)
            self._log_activity("system", "notifications resumed")
        until = self.notify_gate.silent_until
        self.publish("mochi:quiet", {"silentUntil": until})
        return until

    def notify_user(self, action: dict[str, Any]) -> None:
        """Deliver a notify action to its sinks (original: notifyUser/onNotify).

        The original main process fanned one notify out to independent sinks,
        in order: bubble, activity log, chat push, mood. The bubble is the
        `mochi:notify` broadcast (the pet window renders it); the activity log
        entry is load-bearing beyond history — the watch skill instructs the
        agent to read it before notifying, so a notify that skipped the log
        would defeat the agent-side dedup rule; the chat push appends a pet
        message to the panel transcript (see `_push_to_chat`); mood goes to
        the state manager, which owns it (single source of truth + transient
        auto-reset) and broadcasts `mochi:mood` itself.
        """
        summary = action.get("summary")
        has_summary = isinstance(summary, str) and bool(summary)
        # summary/chatMessage AND mood are agent-authored free text that reach the
        # browser — summary/chatMessage via the mochi:notify broadcast and the chat
        # push, mood via the state manager and the /pet-state + /stats reads. Scrub
        # credentials/exfiltration URLs ONCE up front so EVERY sink below consumes
        # the redacted copy. A mood-only notify (no summary) previously skipped the
        # redaction that lived inside the has_summary block and leaked the raw mood
        # to /pet-state and /stats — hoisting it closes that path.
        action = redact_tree(action)
        summary = action.get("summary")
        if has_summary:
            self.publish("mochi:notify", action)
            self._log_activity("notification", str(summary))
            if action.get("pushToChat") is True:
                self._push_to_chat(action)
        mood = action.get("mood")
        if isinstance(mood, str) and mood:
            self.state_manager.set_mood(mood, _now_ms())

    def note_chat_lifecycle(self, event: str, now_ms: int) -> None:
        """Track foreground chat-turn activity for the interleave gate.

        Fed by the ``/pet-event`` route — the SAME signal that drives the pet's
        thinking/working animation. That route carries only FOREGROUND chat
        events (background spawns reach the state manager by another path, via
        ``watch_spawn_completion``), which is exactly why it is the right seam:
        it isolates the user's live turn, so a background check can't falsely
        gate an ambient push.

        ``user_input`` opens a turn; the terminal events (``task_complete``,
        ``error``) close it and flush anything deferred while it was open.
        ``approval_rejected`` is deliberately NOT terminal: unlike
        ``task_complete``/``error`` (slot-filtered to the pet's own turn on the
        panel WebSocket), the ``approval_resolved`` frame it derives from is
        gateway-level and slotless, so a rejection answered on ANOTHER chat
        surface would otherwise clear THIS turn's gate mid-conversation. A
        rejected pet-slot turn still ends with a ``chat_done`` → ``task_complete``,
        and the ``_CHAT_TURN_MAX_MS`` ceiling backstops any turn that ends with
        no terminal frame at all. Other chat events (``tool_call``,
        ``task_start``, ``approval_required``/``approval_granted``) neither open
        nor close a turn — they occur mid-turn, when it is already active.
        """
        if event == "user_input":
            self._chat_turn_active = True
            self._last_user_input_ms = now_ms
        elif event in ("task_complete", "error"):
            self._chat_turn_active = False
            self._flush_deferred_chat_pushes()

    def _chat_turn_busy(self, now_ms: int) -> bool:
        """True while a foreground chat turn is in flight, or within the grace
        window after the last user input (rapid back-and-forth counts as one
        live conversation).

        The active flag is bounded by ``_CHAT_TURN_MAX_MS``: past that age a
        turn whose terminal event never arrived (panel closed / socket dropped)
        no longer counts as busy, so the drain can release its backlog rather
        than deferring forever."""
        if self._chat_turn_active and now_ms - self._last_user_input_ms < self._CHAT_TURN_MAX_MS:
            return True
        return now_ms - self._last_user_input_ms < self._CHAT_ACTIVE_GRACE_MS

    def _drain_deferred_chat_pushes(self, now_ms: int) -> None:
        """Time-based backstop that delivers deferred pushes with no further
        pet-event.

        The terminal chat events flush on turn end, but two paths strand a
        deferred push with nothing left to release it:

        * a push that arrives AFTER the terminal event but inside the grace
          window — it is buffered after the only flush already ran; and
        * a turn whose panel closed / socket dropped before any terminal frame
          — the ``_chat_turn_active`` flag ages out via ``_CHAT_TURN_MAX_MS``
          but no event fires to flush.

        Driven by the owner loop's 1s cadence (see ``_loop``): once the busy
        window has closed it clears any stale wedged flag and delivers the
        backlog through the SAME dedup as a live push. Piggybacking on the
        existing loop — rather than arming a per-push ``asyncio`` task from
        ``notify_user`` — is the cleaner seam: it needs no task lifecycle
        (arm/refresh/cancel) and sidesteps having to prove which loop
        ``notify_user`` runs on, since the loop is unconditionally the gateway's.
        Delivery is bounded by the grace/ceiling window plus at most one poll
        interval, with no dependency on another pet-event arriving.
        """
        if not self._deferred_chat_pushes and not self._chat_turn_active:
            return
        if self._chat_turn_busy(now_ms):
            return
        # The window has closed (terminal event, grace expiry, or the ceiling
        # aging out a wedged flag). Clear the flag so state stays honest, then
        # deliver any backlog (a no-op when empty).
        self._chat_turn_active = False
        self._flush_deferred_chat_pushes()

    def _flush_deferred_chat_pushes(self) -> None:
        """Deliver pushes deferred during a chat turn, in arrival order.

        Each goes through the SAME dedup as a live push (``_deliver_chat_push``),
        so a deferred greeting that now duplicates the agent's own reply — or an
        earlier accepted push — is still dropped rather than double-posted.
        """
        if not self._deferred_chat_pushes:
            return
        deferred, self._deferred_chat_pushes = self._deferred_chat_pushes, []
        for action in deferred:
            self._deliver_chat_push(action)

    def _push_to_chat(self, action: dict[str, Any]) -> None:
        """Append a pet message to the panel transcript, gated and deduplicated.

        Content is `chatMessage` (detailed, no length limit) falling back to
        the bubble summary — the original's `action.chatMessage || summary`.

        THE INTERLEAVE GATE. An ambient pet push (a scheduled greeting, a watch
        result) must not land between the user's message and the agent's reply —
        the reported bug was a "Good evening!" greeting injected mid-turn. While
        a foreground chat turn is in flight, or within the grace window after the
        last user input, a non-critical push is DEFERRED (buffered, capped at the
        scan depth) and flushed when the turn ends (see note_chat_lifecycle).
        `critical` priority (e.g. a "meeting in 5 minutes" reminder) bypasses the
        gate and lands immediately. Deferring, not dropping, preserves the push,
        and the durable activity-log entry `notify_user` writes is unaffected —
        so an ambient message deferred right at the end of a conversation (no
        following turn to flush it) is still recorded there immediately.

        Delivery below the gate is unchanged — see `_deliver_chat_push`.
        """
        content = action.get("chatMessage") or action.get("summary")
        if not isinstance(content, str) or not content:
            return
        if action.get("priority") != "critical" and self._chat_turn_busy(_now_ms()):
            self._deferred_chat_pushes.append(action)
            while len(self._deferred_chat_pushes) > self._CHAT_PUSH_SCAN_DEPTH:
                self._deferred_chat_pushes.pop(0)
            return
        self._deliver_chat_push(action)

    def _deliver_chat_push(self, action: dict[str, Any]) -> None:
        """Dedup against recent accepted pushes, then broadcast `mochi:chat-push`.

        THE GUARD IS THE POINT. Watch-check agents are independent spawns and
        cannot see each other's output, so without it every poll cycle would
        repeat "still pending" into the chat. A push is dropped when a push
        accepted within the last `quietPeriodMins` minutes (the setting's
        documented "don't re-notify within this window") is an exact match OR
        shares >80% of its words — both halves ported from the original,
        which scanned the recent transcript the same way. Empty word sets
        never fuzzy-match (an emoji-only push cannot swallow a later one).

        The accepted push is broadcast as `mochi:chat-push`; the panel appends
        it to the transcript it renders. Divergence from the original, which
        persisted pushes in its own chat store: the migrated transcript is
        backed by the core chat slot, which has no append-without-a-turn API,
        so a push lives until the panel reloads. The durable record is the
        activity-log entry written by `notify_user`.
        """
        content = action.get("chatMessage") or action.get("summary")
        if not isinstance(content, str) or not content:
            return
        now = _now_ms()
        try:
            quiet_mins = int(load_settings(self.data_dir).get("quietPeriodMins", 5))
        except (TypeError, ValueError):
            quiet_mins = 5
        window_ms = max(0, quiet_mins) * 60_000
        words = self._normalized_words(content)
        for prior, accepted_at in self._recent_chat_pushes:
            if now - accepted_at > window_ms:
                continue
            if prior == content:
                return
            prior_words = self._normalized_words(prior)
            if not words or not prior_words:
                continue
            overlap = len(words & prior_words)
            if overlap / max(len(words), len(prior_words)) > 0.8:
                return
        self._recent_chat_pushes.append((content, now))
        while len(self._recent_chat_pushes) > self._CHAT_PUSH_SCAN_DEPTH:
            self._recent_chat_pushes.popleft()
        # Redact at the sink too: notify_user pre-scrubs, but other callers must
        # not be able to route un-redacted chat content to the browser.
        self.publish("mochi:chat-push", redact_tree({"content": content, "timestamp": now}))

    @staticmethod
    def _normalized_words(text: str) -> set[str]:
        """The original's normalize(): lowercase, strip punctuation, split."""
        return {w for w in re.sub(r"[^\w\s]", "", text.lower()).split() if w}

    def _shell_idle_secs(self) -> int:
        return 0  # provided by the Electron shell over a route (C track)

    # ── Presence (companion-time gate) ─────────────────────────────────────

    def presence_beat(self, now_ms: int | None = None, *, visible: bool = True) -> None:
        """Record a pet-window heartbeat (route-fed, every 30s).

        One beat carries two facts with different lifetimes:

        * the beat ARRIVING means the Electron shell is running (the pet window
          exists only inside it — a web-only dashboard session cannot open
          Mochi at all), which is what gates the autonomous side;
        * ``visible`` means the pet is actually ON SCREEN, which is what
          companion time counts. hideAll hides the window without destroying
          it, so a hidden pet keeps beating with ``visible=False`` — polling
          continues (the original polled while hidden too) but the
          companionship clock stops.
        """
        now = now_ms if now_ms is not None else _now_ms()
        self._last_shell_beat_ms = now
        if visible:
            self._last_presence_ms = now

    def pet_present(self, now_ms: int) -> bool:
        return now_ms - self._last_presence_ms <= _PRESENCE_FRESH_MS

    def shell_present(self, now_ms: int) -> bool:
        """The Mochi runtime environment (Electron shell) is running.

        Gates ALL autonomous work. In the original the poller lived in the pet
        app's own process — quitting the app WAS the off switch. This loop
        lives in the gateway, which also serves headless/web-only users; without
        this gate the background agent burns tokens producing bubbles and moods
        that no surface can ever show.
        """
        return now_ms - self._last_shell_beat_ms <= _SHELL_FRESH_MS

    def plan_prompt(self) -> str:
        return load_skill_line("mochi-plan") + "\nFollow its instructions."

    def replan_prompt(self) -> str:
        # mochi-replan, NOT mochi-plan: the queue is flagged for a partial
        # adjustment, which is the cheap path. The heavyweight planner has no
        # replan section, so naming it here both paid full price and pointed the
        # agent at instructions that do not exist.
        return (
            load_skill_line("mochi-replan")
            + "\nThe queue is flagged needs_replan. Follow the skill's automatic mode."
        )

    def activity_budget(self):

        return resolve_activity_budget(load_settings(self.data_dir))

    def due_watch_items(self) -> list[dict[str, Any]]:

        wl = wf.read_watchlist(self._watchlist_path, now_ms=_now_ms())
        due = wf.find_due_watch_items(wl, now_ms=_now_ms())
        budget = self.activity_budget()
        if budget is None or budget.watch_min_interval_ms <= 0:
            return due
        # The tier is a FLOOR under per-item intervals, never an override: an
        # item is deferred (still due later), not dropped, when its last check
        # is more recent than the floor. Items never checked pass through.
        floor_ms = budget.watch_min_interval_ms
        now = _now_ms()
        out = []
        for item in due:
            last = item.get("lastCheckedAt")
            last_ms = _epoch_ms(last) if last else None
            if last_ms is None or now - last_ms >= floor_ms:
                out.append(item)
        return out

    def unnotified_watch_items(self) -> list[dict[str, Any]]:

        wl = wf.read_watchlist(self._watchlist_path, now_ms=_now_ms())
        out = []
        for item in wl.get("items", []):
            changed = item.get("lastChangedAt")
            notified = item.get("lastNotifiedAt")
            if changed and (not notified or notified < changed):
                out.append(item)
        return out

    def mark_items_notified(self, ids: list[Any]) -> None:

        def do_mark() -> None:
            now = _now_ms()
            # Under the cross-process lock like every other watchlist
            # read-modify-write: enqueue_write only orders writers in THIS process.
            with wf.watchlist_mutation(self._watchlist_path):
                wl = wf.read_watchlist(self._watchlist_path, now_ms=now)
                result = wf.apply_watchlist_update(
                    wl,
                    {"update": [{"id": i, "notified": True} for i in ids]},
                    now_ms=now,
                )
                wf.write_atomic(
                    self._watchlist_path,
                    {"version": 1, "items": result["items"]},
                )

        fut = self.watchlist.enqueue_write(do_mark)

        def _report(f: Any) -> None:
            # .exception() itself RAISES on a cancelled future, and this runs as a
            # done-callback, where a raise becomes an unhandled loop exception.
            # Shutdown cancels pending writes, so cancellation is a normal path.
            if f.cancelled():
                return
            err = f.exception()
            if err is not None:
                logger.warning("[mochi] mark-notified write failed: %s", err)

        # Fire-and-forget from the poller's perspective; consume the outcome so a
        # failed write is logged instead of warning about an unretrieved exception.
        fut.add_done_callback(_report)


class _IdleCallbacks:
    def __init__(self, runtime: MochiRuntime) -> None:
        self._rt = runtime

    def on_pause(self) -> None:
        now = _now_ms()
        self._rt.poller.stop()
        self._rt.watchlist.on_idle(now)
        self._rt.publish("mochi:idle", {"paused": True})

    def on_resume(self) -> None:
        self._rt.poller.start()
        self._rt.watchlist.on_resume()
        self._rt.publish("mochi:idle", {"paused": False})


class _WatchlistCallbacks:
    def __init__(self, runtime: MochiRuntime) -> None:
        self._rt = runtime

    async def spawn_agent(self, prompt: str) -> None:
        await _spawn_agent(prompt)

    def force_replan(self) -> None:
        # Dead code path in the ported service (quirk 1) — kept wired anyway.
        self._rt.publish("mochi:force-replan", {})

    def on_notify_direct(self, summary: str) -> None:
        # summary is agent-authored free text -> browser via mochi:notify; scrub.
        self._rt.publish("mochi:notify", redact_tree({"summary": summary, "degraded": True}))


# ── Hook entry points (manifest: backend.hooks) ────────────────────────────


def _bind_spawn_from_context(ctx: Any) -> None:
    """Bind the spawn seam to the host capability the manifest asked for.

    Mochi deliberately uses ONLY ``ctx.spawn`` -- not an import of the host's
    SubagentManager, and not an HTTP call to /api/spawn with the internal secret.
    Those are shortcuts only an in-package app could take, and an app that takes
    them stops being a model for the (external) apps that cannot.

    ``ctx.spawn`` is None when the manifest did not declare ``permissions.spawn``
    or the host supplied no implementation; the seam then stays unbound and the
    poller's own failure accounting reports it, rather than the app pretending
    the autonomous half is running.
    """
    spawn = getattr(ctx, "spawn", None)
    if spawn is None:
        # Do NOT clear an existing binding: an absent capability must not wipe one
        # that was injected deliberately (the gateway on a later pass, or a test).
        # Warn only when nothing is bound at all, which is the real dead end.
        if _spawn_fn is None:
            logger.warning(
                "[mochi] no spawn capability on the app context — planning and "
                "watch checks will not run (declare permissions.spawn in app.json)"
            )
        return

    async def _spawn(prompt: str, agent: str) -> str:
        # silent mirrors the original: a USER SETTING (Settings -> Notifications),
        # not a hardcode, because whether background work notifies is a taste
        # call. Read per spawn so toggling it takes effect without a restart.
        silent = True
        model = ""
        try:
            settings = await asyncio.to_thread(load_settings, Path(ctx.data_dir))
            silent = bool(settings.get("silentSubagents", True))
            # bgModel is its own setting, NOT part of the activity tier: cadence
            # and model quality are independent choices (economy cadence with a
            # strong planning model is a legitimate combination).
            model = str(settings.get("bgModel") or "")
        except Exception:  # noqa: BLE001 — a settings read must not block a spawn
            pass
        spawn_id = await spawn.run(prompt, agent, silent=silent, model=model)
        # The original logged every spawn to the activity log; with silent
        # subagents that log is the ONLY place background work is visible.
        if _runtime is not None:
            _runtime._log_activity("spawn", f"{agent} → {prompt[:80]}")
            # Count AFTER a successful spawn (a declined spawn raised above and
            # must not consume budget). Same file the usage display reads. It is a
            # locked read-modify-write + atomic replace, so off the loop it goes:
            # _spawn runs on the gateway loop and a slow/contended disk would
            # otherwise freeze chat and the heartbeat.
            await asyncio.to_thread(_runtime.spawn_ledger.record_spawn)
            # Early-release the poller's serial wait when the agent finishes.
            # The original resolved this from the WS subagent_done event; here
            # a light probe loop does the same job in-process. Without it every
            # watch check holds the poll lock for the FULL spawn timeout.
            _runtime.watch_spawn_completion(spawn_id, spawn)
        return spawn_id

    set_spawn_fn(_spawn)


async def on_startup(ctx: Any) -> None:
    """Build the runtime and start the owner loop. Idempotent.

    Async because start() loads persisted stats/pins off the event loop (via
    asyncio.to_thread) — the lifecycle dispatcher awaits the hook when it returns
    a coroutine, so enabling/disabling Mochi never blocks the gateway on disk I/O.
    """
    global _runtime
    _bind_spawn_from_context(ctx)
    # Deny-by-default MCP policy must be materialized into the app's agent
    # configs BEFORE the runtime (and therefore the agent) starts. The only
    # other place apply_policy runs is the settings-save route, so on the FIRST
    # enable — before any settings save — the agent would otherwise be
    # materialized inheriting any ambient MCP server ungranted. Re-asserting on
    # every enable also re-closes servers that appeared while Mochi was disabled.
    # Off the loop: it does disk I/O and re-materializes agent configs.
    #
    # NOT swallowed. A failure here means the `neutralize` entries did not reach
    # the agent configs, and kiro-cli loads every globally configured MCP server
    # into an agent regardless of that agent's own config — so starting anyway
    # gives the pet ambient reach to servers the user never granted it. Letting it
    # propagate is the fail-closed path AND the visible one: the lifecycle
    # dispatcher logs the exception, marks the app degraded, and audits it, rather
    # than leaving a running pet that looks governed and is not.

    _settings = await asyncio.to_thread(load_settings, Path(ctx.data_dir))
    await asyncio.to_thread(apply_policy, Path(ctx.data_dir), _settings)
    if _runtime is not None:
        await _runtime.start()  # re-enable after disable: same instance restarts
        return
    _runtime = MochiRuntime(ctx)
    await _runtime.start()


async def on_shutdown(ctx: Any) -> None:
    """Stop the owner loop and flush state. Idempotent."""
    if _runtime is not None:
        await _runtime.stop()
