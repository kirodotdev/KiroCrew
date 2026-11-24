"""Resource-pressure notification producer (posture → notification bus).

Bridges the advisory resource tier to the notification bus. The posture
computed by :mod:`kiro_crew.resource_status` reaches agents (the injected
``[RESOURCES]`` context line and the ``resource_status`` pull tool) but not
the human: a host can sit at CRITICAL posture — agent runtimes OOM-killed,
freeze imminent — while the only warnings go to logs. This producer samples
the same posture on the gateway's event-loop heartbeat cadence and publishes
user-visible notes to the ``system.resources`` channel.

Episode model (the dedupe/hysteresis core, kept pure for testability):

* An **episode** spans from the first non-ample probe to the first ample
  probe after it. Within one episode, each signal fires at most once:

  - entering **critical** pushes one critical-priority note immediately,
    and re-pushes it every :data:`CRITICAL_REALERT_SECS` while the posture
    stays critical (same ``group_key``, so the feed stacks rather than
    spams) — a single missed note must not be the only warning before a
    freeze;
  - **tight sustained** for :data:`SUSTAINED_TIGHT_SECS` pushes one
    default-priority note — suppressed once the critical note has fired
    (it is strictly weaker news), but escalation the other way (tight note
    already sent, posture then reaches critical) still fires the critical
    note, because "about to freeze" must not be muffled by an earlier
    milder warning;
  - returning to **ample** after any alert pushes one recovery note and
    closes the episode. An episode that never alerted (a brief tight blip)
    closes silently.

* ``unknown`` posture (probe failure) is a non-signal: it neither starts,
  advances, nor closes an episode, so a transient probe hiccup cannot fire
  a false recovery or reset the sustained-tight timer.

* Hysteresis falls out of the model: critical↔tight oscillation re-alerts
  nothing because an episode only ends at ample.

A note names the memory its figure measured. When the agent slice's ceiling is
the tighter reading (``ResourceStatus.agent_ceiling``), the note says "Agent
memory" and adds the host's own figure, which is what the dashboard's memory
gauge shows; otherwise it says "Host memory", as it always did.

Thresholds are the existing ``agent.resource_pressure_gb`` /
``agent.resource_critical_gb`` config keys (read by ``resource_status.probe``
on every sample — config is fingerprint-cached, so this is cheap and live).
The off-switch is the standard per-channel control: muting
``system.resources`` in Settings → Notifications silences every surface
(badge, sound, native banner) via :class:`ChannelSettings`, the same
convention every other channel follows. Setting ``resource_pressure_gb`` to
``0`` disables the tight tier at the source, exactly as it does for the
agent-facing context line.

The per-app :class:`AppRateLimiter` is deliberately not consulted: it caps
app producers, and system channels never pass through it (see
``rate_limit.py``); the episode model already bounds this producer to a few
notes per pressure episode.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Callable

from kiro_crew.notifications.bus import _MAX_BODY_LEN, NotificationPayload
from kiro_crew.notifications.memory_sharing_hint import SharingHint, sharing_hint
from kiro_crew.platform_compat_metrics import linux_memory_psi_full_avg10
from kiro_crew.resource_status import (
    POSTURE_AMPLE,
    POSTURE_CRITICAL,
    POSTURE_TIGHT,
    ResourceStatus,
    TempHeadroom,
    probe,
    temp_headroom,
    temp_roots,
)
from kiro_crew.resource_status_agent_ceiling import gb_text
from kiro_crew.sandbox import check_agents_slice_pressure, reclaim_agents_slice_page_cache

if TYPE_CHECKING:
    from kiro_crew.notifications.bus import NotificationBus

logger = logging.getLogger(__name__)

CHANNEL = "system.resources"

# How often maybe_sample() actually probes. The caller (the event-loop
# heartbeat) ticks faster; this gate makes the probe cadence independent of
# the caller's interval.
SAMPLE_INTERVAL_SECS = 30.0

# The cadence while the last posture was tight or critical: the heartbeat's own
# 5 s, so every beat samples and no timer is added. At 30 s a swapless host can
# go from tight to livelock between two samples (rfc-host-memory-guard.md §4.1).
FAST_SAMPLE_INTERVAL_SECS = 5.0

# PSI ``full avg10`` at or above this, on a TIGHT reading, counts as critical for
# the memory guard only: not for the notes, the spawn gate or the create refusal.
# While the memory cgroup is out of view, the same percentage is the bar for the
# share of time between two samples in which the topmost visible cgroup stalled.
GUARD_PSI_FULL_AVG10 = 10.0

# How long posture must stay tight (without recovering to ample) before the
# sustained-tight note fires.
SUSTAINED_TIGHT_SECS = 600.0

# While posture STAYS critical, re-push the critical note on this cadence so
# one missed notification is not the only warning before a freeze. Same
# group_key as the entry note, so the feed stacks the repeats.
CRITICAL_REALERT_SECS = 1800.0

# How often, at most, a pressured sample re-reads whether a sharing switch can be
# offered: the read scans the agent specs, and the answer changes only on a settings
# change.
SHARING_HINT_REFRESH_SECS = 60.0

# Upper bound on one probe. Must stay BELOW the heartbeat's 5s interval: the
# heartbeat awaits this sample, so the worst-case gap between beats is
# interval + this bound. Keeping the bound under the interval leaves the gap
# far inside any watchdog exit threshold; a memory probe that needs more than
# this is already pathological and forfeits its sample.
PROBE_TIMEOUT_SECS = 2.0

# The most slice OOM reports held for the loop. Older ones are counted, not
# kept, so probes that keep finishing late cannot grow the stash without end.
MAX_PENDING_OOM_REPORTS = 50

# Disk posture (rfc-host-memory-guard.md §5.2): a temp root is ``tight`` by the
# existing ``TempHeadroom.low`` rule and ``critical`` under these floors.
DISK_CRITICAL_FREE_PCT = 3.0
DISK_CRITICAL_FREE_INODES = 200

_POSTURE_RANK = {POSTURE_AMPLE: 0, POSTURE_TIGHT: 1, POSTURE_CRITICAL: 2}

# While the posture STAYS tight or critical, one more wake each time free memory
# (GB) or the worst temp root's free space (percentage points) falls this far
# below the reading at the last posture wake (rfc-resource-control.md §4.2).
# Tight spans 4 -> 2 GB and 10 -> 3% by default, so a step gives one or two extra
# wakes inside it; under critical the fast layer acts, so a smaller step only
# reports how deep it is.
WORSEN_STEPS: dict[str, tuple[float, float]] = {
    POSTURE_TIGHT: (1.0, 2.0),
    POSTURE_CRITICAL: (0.5, 1.0),
}

# A worker's growth wake (rfc-resource-control.md §4.3): its session tree first
# holds WORKER_HOST_SHARE of host memory (any posture; re-armed once it drops
# below WORKER_SHARE_REARM), or it grew WORKER_GROWTH_STEP_GB since the last
# growth wake about it while the host is tight or critical.
WORKER_HOST_SHARE = 0.25
WORKER_SHARE_REARM = 0.20
WORKER_GROWTH_STEP_GB = 2.0

# The combined host posture last sampled, as ``(level, reading)``, or ``None``
# before the first sample. Written on the loop by the gateway's one notifier and
# read by the work-ledger probe on its worker thread; a tuple swap is atomic.
_current_posture: tuple[str, str] | None = None

# ``(token, text)`` for the latest same-level step of decline, or ``None`` when
# the level has not worsened since it began. The token carries the level and the
# reading the step woke at; the text says what moved. Swapped like the tuple above.
_worsened: tuple[str, str] | None = None

# Session ledger key -> ``(stamp, rss_mb, share_pct, grown_mb)`` for the latest
# growth wake each session earned, from the cleanup watchdog's RSS readings.
# Replaced whole on every watchdog tick.
_worker_growth: dict[str, tuple[str, float, float, float]] = {}

# Ledger key -> ``(rss_mb at its last growth wake or first sight, share armed)``.
_session_marks: dict[str, tuple[float, bool]] = {}


def current_posture() -> tuple[str, str] | None:
    """The combined memory-and-disk posture and its one-line reading, or ``None``."""
    return _current_posture


def worsened() -> tuple[str, str] | None:
    """The latest same-level step of decline as ``(token, text)``, or ``None``."""
    return _worsened


def worker_growth() -> dict[str, tuple[str, float, float, float]]:
    """Each session's latest growth wake, keyed by its ledger key."""
    return _worker_growth


def growth_steps(
    rss_mb: "Mapping[str, float]", host_total_mb: float, level: str | None, now: float
) -> list[str]:
    """Advance every session's growth marks by one watchdog reading; the keys that stepped.

    A first sight sets the mark and wakes nobody unless the tree already holds
    the share. The share wake re-arms only below :data:`WORKER_SHARE_REARM`, so a
    tree sitting on the line wakes once.
    """
    global _worker_growth, _session_marks
    growth = {key: value for key, value in _worker_growth.items() if key in rss_mb}
    marks: dict[str, tuple[float, bool]] = {}
    stepped: list[str] = []
    pressed = level in WORSEN_STEPS
    for key, size in rss_mb.items():
        share = size / host_total_mb if host_total_mb > 0 else 0.0
        mark, armed = _session_marks.get(key, (size, True))
        crossed = armed and share >= WORKER_HOST_SHARE
        grew = pressed and size - mark >= WORKER_GROWTH_STEP_GB * 1024
        if crossed or grew:
            growth[key] = (f"{now:.3f}", size, 100.0 * share, size - mark if grew else 0.0)
            stepped.append(key)
            mark = size
        if share >= WORKER_HOST_SHARE:
            armed = False
        elif share < WORKER_SHARE_REARM:
            armed = True
        marks[key] = (mark, armed)
    _session_marks, _worker_growth = marks, growth
    return stepped


def note_watchdog_sizes(
    sessions: "Iterable[tuple[str, int, object]]", rss_by_pid: "Mapping[int, int]"
) -> None:
    """Hand one cleanup-watchdog tick's tree sizes to the worker growth wake.

    ``sessions`` is the tick's ``(key, pid, session)`` list and ``rss_by_pid`` the
    sizes it already measured, so nothing new is measured. A shared runtime's tree
    holds its co-tenants too, so it is not one session's size and is left out.
    Never raises.
    """
    try:
        from kiro_crew.runtime_ownership import RUNTIME_OWNERSHIP

        sizes = {
            key: float(rss_by_pid[pid])
            for key, pid, _session in sessions
            if pid in rss_by_pid and RUNTIME_OWNERSHIP.leases_on_pid(pid) <= 1
        }
    except Exception:
        logger.debug("worker growth sizes could not be read", exc_info=True)
        return
    note_session_rss(sizes)


def note_session_rss(rss_mb: "Mapping[str, float]") -> None:
    """One cleanup-watchdog reading of each own-runtime session's tree, in MB.

    Called from ``SessionCleanup._rss_threshold_check`` with the sizes it already
    measured, so the growth wake adds no sampler and no tree walk; with the
    watchdog's RSS check off (``session.watchdog_rss_max_mb = 0``) it is never
    called and the growth wake is off. A step pulls the watching conductors
    forward through the posture push; each one's probe keeps only its own open
    items' workers and charges the wake to that item. Never raises.
    """
    try:
        from kiro_crew.platform_compat import host_total_mib
        from kiro_crew.session_ledger_layout import ledger_key

        posture = _current_posture
        folded: dict[str, float] = {}
        for key, size in rss_mb.items():
            folded.setdefault(ledger_key(key), float(size))
        stepped = growth_steps(
            folded, float(host_total_mib()), posture[0] if posture else None, time.time()
        )
        if stepped:
            _pull_watching_conductors()
    except Exception:
        logger.debug("worker growth reading failed", exc_info=True)


def disk_posture(readings: "list[TempHeadroom]") -> tuple[str, str]:
    """The worst posture over *readings* and the text naming the volumes not ample."""
    level = POSTURE_AMPLE
    named: list[str] = []
    for reading in readings:
        critical = reading.free_space_pct < DISK_CRITICAL_FREE_PCT or (
            reading.counts_inodes and reading.free_inodes < DISK_CRITICAL_FREE_INODES
        )
        if critical:
            this = POSTURE_CRITICAL
        elif reading.low:
            this = POSTURE_TIGHT
        else:
            continue
        # ``text()`` names the axis that tripped (space, inodes or both).
        named.append(f"{reading.root} {reading.text()}")
        if _POSTURE_RANK[this] > _POSTURE_RANK[level]:
            level = this
    return level, "; ".join(named)


def _read_disk_posture() -> tuple[str, str, float | None]:
    """Measure every temp root: posture, reading and the worst free-space percent.

    Blocking; runs in the probe's worker thread.
    """
    readings = [r for r in (temp_headroom(root) for root in temp_roots()) if r is not None]
    worst = min((r.free_space_pct for r in readings), default=None)
    return (*disk_posture(readings), worst)


def _out_of_view_memory_stall_us() -> int | None:
    """The topmost visible cgroup's total ``full`` memory stall, while out of view.

    In microseconds; None while memory accounting is in view or the file is
    unreadable. Blocking; runs in the probe's worker thread. ``subagent`` is imported
    lazily, as ``resource_status`` does, because it is heavy.
    """
    from kiro_crew.subagent import out_of_view_memory_stall_us

    return out_of_view_memory_stall_us()


def stall_share(before: tuple[float, int] | None, after: tuple[float, int] | None) -> float | None:
    """The percentage of the time between two ``(clock, total stall)`` readings spent stalled.

    The totals are in microseconds and the clock in seconds. None without both
    readings, when no time passed, or when the total went back (a new cgroup).
    """
    if before is None or after is None:
        return None
    elapsed = after[0] - before[0]
    stalled = after[1] - before[1]
    if elapsed <= 0 or stalled < 0:
        return None
    return min(100.0, stalled / (elapsed * 1e4))


def combined_posture(status: ResourceStatus, disk: tuple[str, str]) -> tuple[str, str] | None:
    """The worse operational memory/disk posture; ``None`` when memory is unreadable."""
    memory = status.decision_posture
    if memory not in _POSTURE_RANK:
        return None
    disk_level, disk_text = disk
    level = memory if _POSTURE_RANK[memory] >= _POSTURE_RANK[disk_level] else disk_level
    reading = f"memory {status.available_gb:.1f} GB free"
    if disk_text:
        reading += f"; {disk_text}"
    return level, reading


def _pull_watching_conductors() -> None:
    """Pull every watching conductor's work-ledger patrol forward. Never raises."""
    try:
        from kiro_crew import conductor_wake

        conductor_wake.wake_for_posture_change()
    except Exception:
        logger.debug("posture-change wake could not be scheduled", exc_info=True)


def _fmt_load(status: ResourceStatus) -> str:
    if status.load_per_cpu is None:
        return ""
    return f", load {status.load_per_cpu}/core"


def _agent_ceiling_context(status: ResourceStatus) -> str:
    """What a slice-bound note adds after its figure: the host's own figure, and why.

    Empty for a host-bound reading. The host figure is what the dashboard's
    memory gauge shows, so without it a note reading "0.0 GB" beside a gauge at
    57% looks like a false alarm when it is the agent ceiling that is full.
    """
    ceiling = status.agent_ceiling
    if ceiling is None:
        return ""
    shmem = ceiling.shmem_sentence()
    return (
        f" The host itself still has {gb_text(ceiling.host_available_gb)} GB available: this "
        "ceiling is what agent processes may use together, and at it the kernel slows "
        "every one of them." + (f" {shmem}" if shmem else "")
    )


class ResourcePressureNotifier:
    """Turns the advisory posture stream into at-most-once-per-episode notes.

    Owned by ``DashboardState`` alongside the bus it publishes to; driven by
    :meth:`maybe_sample` from the event-loop heartbeat. ``probe_fn`` and
    ``clock`` are injectable so tests exercise the episode logic with no
    real ``/proc`` or wall-clock dependence.
    """

    def __init__(
        self,
        bus: "NotificationBus",
        *,
        probe_fn: Callable[[], ResourceStatus] = probe,
        clock: Callable[[], float] = time.monotonic,
        disk_fn: Callable[[], tuple] = _read_disk_posture,
        on_posture_change: Callable[[], None] = _pull_watching_conductors,
        psi_fn: Callable[[], float | None] = linux_memory_psi_full_avg10,
        view_stall_fn: Callable[[], int | None] = _out_of_view_memory_stall_us,
        sharing_hint_fn: Callable[[], SharingHint | None] = sharing_hint,
    ) -> None:
        self._bus = bus
        self._probe = probe_fn
        self._clock = clock
        self._disk_fn = disk_fn
        self._on_posture_change = on_posture_change
        # The "Reduce memory use" button the tight and critical notes carry while a
        # sharing switch is off and can be turned on (``memory_sharing_hint``). The
        # reader scans the agent specs, so ``maybe_sample`` runs it off the loop at
        # most every SHARING_HINT_REFRESH_SECS while memory is pressured, and the
        # pushes read the cached answer.
        self._sharing_hint = sharing_hint_fn
        self._hint: SharingHint | None = None
        # When the last read was started (stamped at launch, so a read that times out
        # still waits out the interval), and whether one is still running: at most one
        # read thread at a time, guarded like the probe's ``_inflight`` and
        # ``_probe_started`` below.
        self._hint_read_at: float | None = None
        self._hint_inflight = False
        self._hint_started = False
        # The disk reading taken beside the last memory probe, in the worker thread.
        self._disk: tuple[str, str] = (POSTURE_AMPLE, "")
        self._disk_free_pct: float | None = None
        # The combined level last published; a change of it pulls conductors forward.
        self._posture_level: str | None = None
        self._psi_fn = psi_fn
        # The newest PSI reading, written by the worker thread with the probe and
        # read on the loop after the probe returned in time.
        self._psi: float | None = None
        self._view_stall_fn = view_stall_fn
        # ``(clock, total full stall)`` of the topmost visible cgroup while the memory
        # cgroup is out of view (the free figure is then the host's), read beside a
        # probe that is not critical while the guard is armed. Written and read like
        # ``_psi``.
        self._view_reading: tuple[float, int] | None = None
        # On the loop: the reading the next stall share is measured from, and the
        # share this sample measured. No reading taken at a hand-off to the guard, or
        # while the pass it returned still runs, becomes the baseline, so the stall
        # behind one stop cannot cause a second.
        self._view_base: tuple[float, int] | None = None
        self._view_share: float | None = None
        self._guard_pass: object = None
        # The memory guard (``session_memory_guard.request``): called on the loop
        # with ``(status, psi, trigger)`` when the guard trigger holds, where ``psi``
        # is the host's ``full avg10``, or the stall share for ``psi-cgroup``. It
        # schedules its own pass and returns at once, with that pass's task. None
        # until the gateway wires it.
        self.memory_guard: Callable[[ResourceStatus, float | None, str], object] | None = None
        # Free memory (GB) and worst disk free space (%) the next step is measured
        # from: the reading at the last posture wake, never raised by a recovery.
        self._worsen_ref: tuple[float, float | None] = (0.0, None)
        # Which level period this is (this notifier's start time and a count of its
        # level changes, so a restart never repeats one) and the steps it has made.
        self._started = time.time()
        self._period = 0
        self._decline_count = 0
        self._next_sample = 0.0
        self._inflight = False
        # Set by the worker thread at entry, read on the loop side after a
        # probe timeout: a timed-out probe whose worker never started (the job
        # was still queued in the shared executor and asyncio cancelled it) left
        # `_inflight` stuck True forever, because the worker's finally never ran
        # and sampling was disabled for the rest of the process. The loop clears
        # the slot in that case. A worker that did start keeps the slot (a
        # genuinely hung probe must not get a second thread beside it).
        self._probe_started = False
        # Episode state. ``_episode_since`` doubles as the active flag (None =
        # no episode) and as the sustained-tight timer origin.
        self._episode_since: float | None = None
        self._critical_alerted = False
        self._tight_alerted = False
        self._last_critical_push = 0.0
        # Whether the episode's latest alert measured the agent ceiling, so its
        # recovery note names the same memory the alert did. The recovery
        # reading itself is ample and carries no attribution.
        self._alerted_agent_ceiling = False
        # Slice OOM reports captured by the worker thread, consumed (and
        # cleared) on the loop side so the bus is only touched from the loop.
        # The loop takes them only after a probe returned in time, when no
        # worker is running, so the two sides never touch the list at once.
        self._slice_oom_reports: list[str] = []
        self._slice_oom_dropped = 0

    async def maybe_sample(self) -> None:
        """Probe (off-loop, bounded) and evaluate if the interval elapsed.

        Never raises — this runs inside the event-loop heartbeat, whose only
        job is proving loop liveness; a broken probe or sink must degrade to
        a debug log, not kill the heartbeat task. Three guards keep a
        misbehaving probe from harming the loop it reports on:

        * the probe's synchronous config/procfs reads run in a worker thread
          (``asyncio.to_thread``), so a slow filesystem cannot block the loop;
        * the await is bounded by :data:`PROBE_TIMEOUT_SECS` (well below the
          watchdog's stall threshold) — a stalled probe forfeits its sample
          instead of starving the heartbeat until the watchdog kills the
          process;
        * ``_inflight`` (cleared by the worker thread itself, or by the loop
          when a timed-out probe never started) ensures at most one probe
          thread exists — after a timeout a probe that did start still occupies
          the slot, so a permanently hung probe disables sampling rather than
          leaking a thread every interval, while a probe cancelled before it
          ran releases the slot so sampling resumes.
        """
        try:
            now = self._clock()
            if now < self._next_sample or self._inflight:
                return
            self._next_sample = now + SAMPLE_INTERVAL_SECS
            self._inflight = True
            self._probe_started = False
            # A reading the worker takes while a stop runs is never a baseline. A stop
            # starts only in ``_maybe_guard``, after the probe, so this check covers it.
            stopping = self._guard_running()
            try:
                status = await asyncio.wait_for(
                    asyncio.to_thread(self._run_probe), PROBE_TIMEOUT_SECS
                )
            except (asyncio.TimeoutError, TimeoutError):
                logger.debug("resource-pressure probe timed out; sample skipped")
                # Release the slot when the worker never started: asyncio cancels
                # a still-queued to_thread job, so its finally never runs and the
                # slot would otherwise stay held for the life of the process. A
                # worker that did start keeps the slot (its finally clears it, and
                # a hung running probe must not get a second thread).
                if not self._probe_started:
                    self._inflight = False
                return
            if self._slice_oom_reports or self._slice_oom_dropped:
                # A push failure leaves its reports for the next tick and must not
                # cost this sample its pressure check.
                try:
                    self._deliver_slice_oom()
                except Exception:
                    logger.debug("agents-slice OOM push failed", exc_info=True)
            reading = None if stopping else self._view_reading
            self._view_share = stall_share(self._view_base, reading)
            self._view_base = reading
            if (
                status.posture in (POSTURE_TIGHT, POSTURE_CRITICAL)
                and not self._hint_inflight
                and (
                    self._hint_read_at is None
                    or now - self._hint_read_at >= SHARING_HINT_REFRESH_SECS
                )
            ):
                self._hint_inflight = True
                self._hint_started = False
                self._hint_read_at = now
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(self._run_hint_refresh), PROBE_TIMEOUT_SECS
                    )
                except (asyncio.TimeoutError, TimeoutError):
                    logger.debug("memory sharing hint read timed out; the note keeps the last one")
                    # A read cancelled before its worker started never clears the slot.
                    if not self._hint_started:
                        self._hint_inflight = False
            self.observe(status, now)
            self.observe_posture(status)
            # While the memory cgroup is out of view the free figure does not show the
            # limit, so the stall is sampled at the heartbeat's cadence too: at 30 s a
            # stall can reach livelock between two samples.
            if (
                status.decision_posture in (POSTURE_TIGHT, POSTURE_CRITICAL)
                or self._view_reading is not None
            ):
                self._next_sample = now + FAST_SAMPLE_INTERVAL_SECS
            self._maybe_guard(status, self.guard_trigger(status))
        except Exception:
            logger.debug("resource-pressure sample failed", exc_info=True)

    def _run_probe(self) -> ResourceStatus:
        """Worker-thread wrapper: run the probe and release the in-flight slot."""
        # First line, before any blocking work: tells the loop side the worker
        # really started, so a timeout does not wrongly release the slot from
        # under a running probe.
        self._probe_started = True
        try:
            # Piggyback the agents-slice OOM check on the same off-loop
            # cadence: new OOM kills inside kirocrew-agents.slice are logged
            # with victim scopes and slice memory state (sandbox owns the
            # format). The message is stashed for the loop side to push onto
            # the bus — "a random subagent died" must be diagnosable from the
            # dashboard, not just the log file. Never raises; reads a handful
            # of cgroup files.
            #
            # A report is only ever added to, never replaced: the check
            # reports each kill once, and a probe that timed out leaves its
            # report here unread, so the next tick's (usually empty) result
            # must not overwrite it before the loop pushes it.
            try:
                msg = check_agents_slice_pressure()
            except Exception:
                msg = None
                logger.debug("agents-slice OOM check failed", exc_info=True)
            if msg:
                self._slice_oom_reports.append(msg)
                excess = len(self._slice_oom_reports) - MAX_PENDING_OOM_REPORTS
                if excess > 0:
                    del self._slice_oom_reports[:excess]
                    self._slice_oom_dropped += excess
            # Same tick: drop clean page cache on the agents slice before it
            # holds the slice at memory.high. The decision is a few file
            # reads; the writes run in their own thread, so this returns at
            # once and never spends the probe's budget.
            try:
                reclaim_agents_slice_page_cache()
            except Exception:
                logger.debug("agents-slice page-cache reclaim failed", exc_info=True)
            try:
                disk = self._disk_fn()
                self._disk = (disk[0], disk[1])
                self._disk_free_pct = disk[2] if len(disk) > 2 else None
            except Exception:
                logger.debug("disk posture read failed", exc_info=True)
            status = self._probe()
            try:
                self._psi = self._psi_fn()
            except Exception:
                self._psi = None
                logger.debug("memory PSI read failed", exc_info=True)
            self._view_reading = None
            # A critical reading hands off without it and drops the baseline anyway.
            if (
                self.memory_guard is not None
                and status.critical_gb > 0
                and status.decision_posture != POSTURE_CRITICAL
            ):
                try:
                    total = self._view_stall_fn()
                    if total is not None:
                        self._view_reading = (self._clock(), total)
                except Exception:
                    logger.debug("out-of-view memory stall read failed", exc_info=True)
            return status
        finally:
            self._inflight = False

    def guard_trigger(self, status: ResourceStatus) -> str | None:
        """``"critical"``, ``"psi"``, ``"psi-cgroup"`` or None: whether the guard should run now.

        Off with ``agent.resource_critical_gb = 0`` on every arm: the critical
        decision posture never occurs then, and PSI only stands in for critical,
        so it is armed only while that threshold is on. ``psi`` is the host's PSI
        when its visible-memory figure is tight; where the limit is in view an
        IO-only stall on an ample host stops nothing. ``psi-cgroup`` is the share
        of time since the last sample in which the topmost visible cgroup stalled
        while the memory cgroup is out of view. An ample host figure reports an
        unknown posture; tight and critical host figures stay visible. The free
        figure is then the host's, not the limit the kernel reclaims at, so that
        stall is the only evidence of it, and it counts no task outside this
        view. A share, not ``avg10``, because
        ``avg10`` takes about 13 s to fall below the bar after a stall ends, and
        each sample in between would stop another tree.
        """
        if not status.critical_gb or status.critical_gb <= 0:
            return None
        decision_posture = status.decision_posture
        if decision_posture == POSTURE_CRITICAL:
            return "critical"
        if decision_posture not in (POSTURE_TIGHT, POSTURE_AMPLE):
            return None
        psi = self._psi
        if decision_posture == POSTURE_TIGHT and psi is not None and psi >= GUARD_PSI_FULL_AVG10:
            return "psi"
        share = self._view_share
        if share is not None and share >= GUARD_PSI_FULL_AVG10:
            return "psi-cgroup"
        return None

    def _guard_running(self) -> bool:
        """Whether the pass the guard returned for the last hand-off is still running."""
        task = self._guard_pass
        return isinstance(task, asyncio.Future) and not task.done()

    def _maybe_guard(self, status: ResourceStatus, trigger: str | None) -> None:
        """Hand the sample to the memory guard when *trigger* holds. Never raises."""
        guard = self.memory_guard
        if guard is None or trigger is None:
            return
        self._view_base = None
        psi = self._view_share if trigger == "psi-cgroup" else self._psi
        try:
            self._guard_pass = guard(status, psi, trigger)
        except Exception:
            logger.warning("memory guard request failed", exc_info=True)

    def observe(self, status: ResourceStatus, now: float) -> None:
        """Advance the episode state machine with one decision-posture reading."""
        posture = status.decision_posture
        if posture == POSTURE_CRITICAL:
            if self._episode_since is None:
                self._episode_since = now
            if not self._critical_alerted:
                self._push_critical(status)
                self._critical_alerted = True
                self._last_critical_push = now
            elif now - self._last_critical_push >= CRITICAL_REALERT_SECS:
                self._push_critical(status, realert=True)
                self._last_critical_push = now
        elif posture == POSTURE_TIGHT:
            if self._critical_alerted:
                # A tight reading interrupts "continuously critical", so the
                # re-alert cadence restarts here: critical<->tight oscillation
                # must never produce repeat notes (the episode dedupe
                # promise) — only sustained critical does.
                self._last_critical_push = now
            if self._episode_since is None:
                self._episode_since = now
            elif (
                not self._tight_alerted
                and not self._critical_alerted
                and now - self._episode_since >= SUSTAINED_TIGHT_SECS
            ):
                self._push_tight(status, now - self._episode_since)
                self._tight_alerted = True
        elif posture == POSTURE_AMPLE:
            if self._episode_since is not None:
                alerted = self._critical_alerted or self._tight_alerted
                agent_ceiling = self._alerted_agent_ceiling
                self._episode_since = None
                self._critical_alerted = False
                self._tight_alerted = False
                self._alerted_agent_ceiling = False
                if alerted:
                    self._push_recovery(status, agent_ceiling=agent_ceiling)
        # unknown: non-signal — keep the episode state untouched (see module
        # docstring).

    def observe_posture(self, status: ResourceStatus) -> None:
        """Publish the combined memory-and-disk posture; pull conductors on a change.

        The pull only moves each watching conductor's patrol tick forward
        (``conductor_wake.wake_for_posture_change``); the work-ledger probe reads
        :func:`current_posture` on that tick and decides the one wake per level
        change. An unknown memory reading is a non-signal here too. The first
        reading after start pulls only when it is not ample. While the level stays
        tight or critical, a fall by one step (:data:`WORSEN_STEPS`) below the
        reading at the last posture wake publishes a new :func:`worsened` and pulls
        again; a recovery inside the level wakes nobody and leaves that reference
        where it is, so one input that recovers and falls back steps at most once
        until the other input steps (rfc-resource-control.md §4.2).
        """
        global _current_posture, _worsened
        combined = combined_posture(status, self._disk)
        if combined is None:
            return
        _current_posture = combined
        level = combined[0]
        reading = (status.available_gb, self._disk_free_pct)
        previous, self._posture_level = self._posture_level, level
        if level != previous:
            self._worsen_ref, _worsened = reading, None
            self._period, self._decline_count = self._period + 1, 0
            if previous is not None or level != POSTURE_AMPLE:
                self._on_posture_change()
            return
        step = WORSEN_STEPS.get(level)
        if step is None:
            return
        moved = self._step_of_decline(
            reading, step, (status.decision_posture == level, self._disk[0] == level)
        )
        if moved is None:
            return
        # The level period and a count of its steps, beside the reading: the same
        # reading again in a later period at this level is a new step, never one
        # the seen set already holds.
        self._decline_count += 1
        disk = "-" if reading[1] is None else f"{reading[1]:.0f}"
        _worsened = (
            f"{level}:{self._started:.3f}.{self._period}:{self._decline_count}"
            f":mem{reading[0]:.1f}:disk{disk}",
            f"host posture still {level} and worse ({moved})",
        )
        self._on_posture_change()

    def _step_of_decline(
        self,
        reading: tuple[float, float | None],
        step: tuple[float, float],
        binding: tuple[bool, bool],
    ) -> str | None:
        """What fell a step below the reference, or ``None``. Moves the reference.

        Only an axis at the current level (*binding*: memory, disk) can step: on a
        host tight on disk alone, ordinary swings of ample memory are not news. No
        reference rises between two wakes: a binding axis keeps its reference, and
        an axis that is not binding follows its reading down only, so a step is
        measured from the lowest it stood since the last wake and a recovery
        followed by a fall back is no step.
        """
        (memory, disk), (now_memory, now_disk) = self._worsen_ref, reading
        moved: list[str] = []
        new_memory = memory if binding[0] else min(memory, now_memory)
        if binding[0] and memory - now_memory >= step[0]:
            moved.append(f"memory {now_memory:.1f} GB free, was {memory:.1f} GB at the last wake")
        if disk is None or now_disk is None:
            new_disk = now_disk
        else:
            new_disk = disk if binding[1] else min(disk, now_disk)
        if binding[1] and disk is not None and now_disk is not None and disk - now_disk >= step[1]:
            moved.append(f"worst temp root {now_disk:.0f}% free, was {disk:.0f}% at the last wake")
        # A step is a posture wake: the whole reading becomes the next reference.
        self._worsen_ref = reading if moved else (new_memory, new_disk)
        return "; ".join(moved) if moved else None

    # ── note builders ────────────────────────────────────────────────────

    def _deliver_slice_oom(self) -> None:
        """Push the waiting OOM reports as notes that each fit the body limit.

        A report leaves the stash only once the note carrying it was pushed:
        a push that fails leaves it and every later one for the next tick.
        """
        reports, self._slice_oom_reports = self._slice_oom_reports, []
        dropped, self._slice_oom_dropped = self._slice_oom_dropped, 0
        lines = [r[:_MAX_BODY_LEN] for r in reports]
        if dropped:
            lines.insert(0, f"{dropped} earlier OOM report(s) were not kept.")
        batches: list[list[str]] = []
        for line in lines:
            if batches and len("\n".join([*batches[-1], line])) <= _MAX_BODY_LEN:
                batches[-1].append(line)
            else:
                batches.append([line])
        sent = 0
        try:
            for batch in batches:
                self._push_slice_oom("\n".join(batch))
                sent += len(batch)
        except Exception:
            # The dropped-count line is first in the first batch, so it is
            # either sent or put back with the rest.
            unsent = lines[sent:]
            if dropped and sent == 0:
                unsent = unsent[1:]
                self._slice_oom_dropped += dropped
            self._slice_oom_reports[:0] = unsent
            excess = len(self._slice_oom_reports) - MAX_PENDING_OOM_REPORTS
            if excess > 0:
                del self._slice_oom_reports[:excess]
                self._slice_oom_dropped += excess
            raise

    def _push_slice_oom(self, message: str) -> None:
        """One note per detected OOM batch inside the agents slice.

        The kernel picks the victim on an aggregate breach, so without this
        the user-visible symptom is a subagent dying with exit 137 and no
        explanation anywhere but the log file. ``group_key`` stacks repeats
        so a thrashing host does not flood the bell feed.
        """
        self._bus.push(
            NotificationPayload(
                source="system",
                channel=CHANNEL,
                title="Agent subprocess OOM-killed by cgroup ceiling",
                body=message,
                group_key="agents-slice-oom",
                meta={"kind": "agents-slice-oom"},
            )
        )

    def _push_critical(self, status: ResourceStatus, *, realert: bool = False) -> None:
        subject = status.memory_subject
        title = f"{subject} still critically low" if realert else f"{subject} critically low"
        ceiling = status.agent_ceiling
        self._alerted_agent_ceiling = ceiling is not None
        # The ceiling keeps the host responsive, so only a host-bound reading
        # warns that the host itself may stop answering.
        if ceiling is None:
            figure = f"Available memory is down to {status.available_gb:.1f} GB"
            risk = "Agent processes may be killed and the host can become unresponsive"
        else:
            figure = (
                f"Agent processes have {status.available_gb:.1f} GB left under their "
                f"{gb_text(ceiling.ceiling_gb)} GB memory ceiling"
            )
            risk = "Agent processes may be killed if their memory keeps growing"
        hint = self._read_sharing_hint()
        self._bus.push(
            NotificationPayload(
                source="system",
                channel=CHANNEL,
                priority="critical",
                title=title,
                body=(
                    f"{figure} (critical threshold {status.critical_gb:g} GB"
                    f"{_fmt_load(status)}).{_agent_ceiling_context(status)} {risk} — "
                    "stop or defer heavy work (builds, full test suites, sub-agent "
                    "waves) or free memory now."
                ),
                actions=hint.actions() if hint else None,
                group_key="resource-pressure",
                meta={"posture": status.posture, "available_gb": status.available_gb},
            )
        )

    def _push_tight(self, status: ResourceStatus, sustained_secs: float) -> None:
        minutes = int(sustained_secs // 60)
        ceiling = status.agent_ceiling
        self._alerted_agent_ceiling = ceiling is not None
        if ceiling is None:
            figure = (
                f"Available memory has stayed at or below {status.pressure_gb:g} GB "
                f"for ~{minutes} minutes (currently {status.available_gb:.1f} GB free"
            )
        else:
            figure = (
                f"Agent processes have had at most {status.pressure_gb:g} GB left under "
                f"their {gb_text(ceiling.ceiling_gb)} GB memory ceiling for ~{minutes} minutes "
                f"(currently {status.available_gb:.1f} GB"
            )
        hint = self._read_sharing_hint()
        self._bus.push(
            NotificationPayload(
                source="system",
                channel=CHANNEL,
                title=f"{status.memory_subject} tight for {minutes} minutes",
                body=(
                    f"{figure}{_fmt_load(status)}).{_agent_ceiling_context(status)} "
                    "Consider deferring heavy work until memory frees."
                ),
                actions=hint.actions() if hint else None,
                group_key="resource-pressure",
                meta={"posture": status.posture, "available_gb": status.available_gb},
            )
        )

    def refresh_sharing_hint(self) -> None:
        """Read the sharing hint again and cache it. BLOCKING; never raises."""
        try:
            hint = self._sharing_hint()
        except Exception:  # noqa: BLE001 - the note goes out without its button
            logger.debug("memory sharing hint failed", exc_info=True)
            hint = None
        self._hint = hint
        self._hint_read_at = self._clock()

    def _run_hint_refresh(self) -> None:
        """Worker-thread wrapper: refresh the hint and release its in-flight slot."""
        self._hint_started = True
        try:
            self.refresh_sharing_hint()
        finally:
            self._hint_inflight = False

    def _read_sharing_hint(self) -> SharingHint | None:
        """The cached sharing hint for a tight or critical note, or None."""
        return self._hint

    def _push_recovery(self, status: ResourceStatus, *, agent_ceiling: bool = False) -> None:
        subject = "Agent memory" if agent_ceiling else "Host memory"
        self._bus.push(
            NotificationPayload(
                source="system",
                channel=CHANNEL,
                title=f"{subject} pressure resolved",
                body=(
                    f"Available memory recovered to {status.available_gb:.1f} GB "
                    f"free{_fmt_load(status)}."
                ),
                group_key="resource-pressure",
                meta={"posture": status.posture, "available_gb": status.available_gb},
            )
        )
