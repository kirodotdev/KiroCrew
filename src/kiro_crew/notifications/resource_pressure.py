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
from typing import TYPE_CHECKING, Callable

from kiro_crew.notifications.bus import NotificationPayload
from kiro_crew.resource_status import (
    POSTURE_AMPLE,
    POSTURE_CRITICAL,
    POSTURE_TIGHT,
    ResourceStatus,
    probe,
)
from kiro_crew.sandbox import check_agents_slice_pressure

if TYPE_CHECKING:
    from kiro_crew.notifications.bus import NotificationBus

logger = logging.getLogger(__name__)

CHANNEL = "system.resources"

# How often maybe_sample() actually probes. The caller (the event-loop
# heartbeat) ticks faster; this gate makes the probe cadence independent of
# the caller's interval.
SAMPLE_INTERVAL_SECS = 30.0

# How long posture must stay tight (without recovering to ample) before the
# sustained-tight note fires.
SUSTAINED_TIGHT_SECS = 600.0

# While posture STAYS critical, re-push the critical note on this cadence so
# one missed notification is not the only warning before a freeze. Same
# group_key as the entry note, so the feed stacks the repeats.
CRITICAL_REALERT_SECS = 1800.0

# Upper bound on one probe. Must stay BELOW the heartbeat's 5s interval: the
# heartbeat awaits this sample, so the worst-case gap between beats is
# interval + this bound. Keeping the bound under the interval leaves the gap
# far inside any watchdog exit threshold; a memory probe that needs more than
# this is already pathological and forfeits its sample.
PROBE_TIMEOUT_SECS = 2.0


def _fmt_load(status: ResourceStatus) -> str:
    if status.load_per_cpu is None:
        return ""
    return f", load {status.load_per_cpu}/core"


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
    ) -> None:
        self._bus = bus
        self._probe = probe_fn
        self._clock = clock
        self._next_sample = 0.0
        self._inflight = False
        # Episode state. ``_episode_since`` doubles as the active flag (None =
        # no episode) and as the sustained-tight timer origin.
        self._episode_since: float | None = None
        self._critical_alerted = False
        self._tight_alerted = False
        self._last_critical_push = 0.0
        # Slice OOM report captured by the worker thread, consumed (and
        # cleared) on the loop side so the bus is only touched from the loop.
        self._slice_oom_msg: str | None = None

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
        * ``_inflight`` (cleared by the worker thread itself) ensures at most
          one probe thread exists — after a timeout the abandoned thread
          still occupies the slot, so a permanently hung probe disables
          sampling rather than leaking a thread every interval.
        """
        try:
            now = self._clock()
            if now < self._next_sample or self._inflight:
                return
            self._next_sample = now + SAMPLE_INTERVAL_SECS
            self._inflight = True
            try:
                status = await asyncio.wait_for(
                    asyncio.to_thread(self._run_probe), PROBE_TIMEOUT_SECS
                )
            except (asyncio.TimeoutError, TimeoutError):
                logger.debug("resource-pressure probe timed out; sample skipped")
                return
            oom_msg, self._slice_oom_msg = self._slice_oom_msg, None
            if oom_msg:
                self._push_slice_oom(oom_msg)
            self.observe(status, now)
        except Exception:
            logger.debug("resource-pressure sample failed", exc_info=True)

    def _run_probe(self) -> ResourceStatus:
        """Worker-thread wrapper: run the probe and release the in-flight slot."""
        try:
            # Piggyback the agents-slice OOM check on the same off-loop
            # cadence: new OOM kills inside kirocrew-agents.slice are logged
            # with victim scopes and slice memory state (sandbox owns the
            # format). The message is stashed for the loop side to push onto
            # the bus — "a random subagent died" must be diagnosable from the
            # dashboard, not just the log file. Never raises; reads a handful
            # of cgroup files.
            try:
                self._slice_oom_msg = check_agents_slice_pressure()
            except Exception:
                self._slice_oom_msg = None
                logger.debug("agents-slice OOM check failed", exc_info=True)
            return self._probe()
        finally:
            self._inflight = False

    def observe(self, status: ResourceStatus, now: float) -> None:
        """Advance the episode state machine with one posture reading."""
        posture = status.posture
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
                self._episode_since = None
                self._critical_alerted = False
                self._tight_alerted = False
                if alerted:
                    self._push_recovery(status)
        # unknown: non-signal — keep the episode state untouched (see module
        # docstring).

    # ── note builders ────────────────────────────────────────────────────

    def _push(self, payload: NotificationPayload) -> None:
        self._bus.push(payload)

    def _push_slice_oom(self, message: str) -> None:
        """One note per detected OOM batch inside the agents slice.

        The kernel picks the victim on an aggregate breach, so without this
        the user-visible symptom is a subagent dying with exit 137 and no
        explanation anywhere but the log file. ``group_key`` stacks repeats
        so a thrashing host does not flood the bell feed.
        """
        self._push(
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
        title = (
            "Host memory still critically low"
            if realert
            else "Host memory critically low"
        )
        self._push(
            NotificationPayload(
                source="system",
                channel=CHANNEL,
                priority="critical",
                title=title,
                body=(
                    f"Available memory is down to {status.available_gb:.1f} GB "
                    f"(critical threshold {status.critical_gb:g} GB"
                    f"{_fmt_load(status)}). Agent processes may be killed and "
                    "the host can become unresponsive — stop or defer heavy "
                    "work (builds, full test suites, sub-agent waves) or free "
                    "memory now."
                ),
                group_key="resource-pressure",
                meta={"posture": status.posture, "available_gb": status.available_gb},
            )
        )

    def _push_tight(self, status: ResourceStatus, sustained_secs: float) -> None:
        minutes = int(sustained_secs // 60)
        self._push(
            NotificationPayload(
                source="system",
                channel=CHANNEL,
                title=f"Host memory tight for {minutes} minutes",
                body=(
                    f"Available memory has stayed at or below "
                    f"{status.pressure_gb:g} GB for ~{minutes} minutes "
                    f"(currently {status.available_gb:.1f} GB free"
                    f"{_fmt_load(status)}). Consider deferring heavy work "
                    "until memory frees."
                ),
                group_key="resource-pressure",
                meta={"posture": status.posture, "available_gb": status.available_gb},
            )
        )

    def _push_recovery(self, status: ResourceStatus) -> None:
        self._push(
            NotificationPayload(
                source="system",
                channel=CHANNEL,
                title="Host memory pressure resolved",
                body=(
                    f"Available memory recovered to {status.available_gb:.1f} GB "
                    f"free{_fmt_load(status)}."
                ),
                group_key="resource-pressure",
                meta={"posture": status.posture, "available_gb": status.available_gb},
            )
        )
