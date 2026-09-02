"""Cautious boot: stagger the startup burst after a recent loop-stall crash.

When the gateway starts and the crash-dump store holds a RECENT loop-stall
dump, the previous instance wedged and hard-exited only minutes ago — very
likely under host resource pressure that has not cleared yet. Launching the
entire startup battery at once (MCP gateway, cron scheduler, app backends,
MCP probes, session restores) onto such a host is exactly what re-wedges the
new instance: the gateway *had* the signal (it logs the prior dump at boot)
but did not act on it.

This module turns that signal into behavior. The decision is made ONCE, early
in gateway startup, entirely off the event loop (``initialize()``). Battery
groups then call ``pause_before(<group>)``, which sleeps a short delay when
cautious mode is active and is a no-op otherwise — the burst becomes a
sequence of small groups separated by breathing room, giving the host (and
the kernel's reclaim machinery) time between spikes.

The delay is scaled by the CURRENT resource posture (``resource_status``):

* recent dump + ``tight``/``critical`` host → maximum caution (longer pauses)
* recent dump + ``ample``/``unknown`` host  → mild stagger only

Everything fails OPEN: an unreadable dump store, a config error, or a probe
failure means a normal, un-staggered boot. ``pause_before`` called without a
prior ``initialize()`` (tests, embedded dashboard starts) is also a no-op.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from kiro_crew import resource_status
from kiro_crew.config import KiroCrewConfig
from kiro_crew.dashboard.crash_dump_store import dump_age_seconds, newest_dump_with_stacks

logger = logging.getLogger(__name__)

# A dump older than this is history, not an active incident: the host has been
# up (or the operator intervened) long enough that pressure from the crash has
# either cleared or become someone's explicit problem. Deliberately a module
# constant, not config — the config surface is one bool (dashboard.cautious_boot).
RECENT_DUMP_MAX_AGE_SECS = 30.0 * 60.0

# Pause inserted between startup battery groups. "Maximum" is used when the
# host is ALSO currently tight/critical on memory; "mild" when the dump is
# recent but the host looks healthy again (or posture is unknown — an unknown
# reading must not escalate, only the positive tight/critical signal does).
MILD_DELAY_SECS = 2.0
MAX_DELAY_SECS = 10.0


@dataclass(frozen=True)
class CautiousBootDecision:
    """Immutable boot-scoped verdict. Computed once; only ever read afterwards."""

    active: bool
    delay_secs: float
    reason: str


# Boot-scoped cache. Written exactly once by ``initialize()`` (awaited on the
# gateway loop before any pause site runs) and only read by ``pause_before``.
# ``None`` means "never initialized" and is treated as inactive — deterministic
# fail-open for tests and any embedded caller that skips initialize().
_decision: CautiousBootDecision | None = None


def _evaluate(
    cfg: object | None = None,
    dumps_dir: Path | None = None,
) -> CautiousBootDecision:
    """Synchronous evaluation — blocking I/O allowed; callers keep it off-loop.

    *cfg* and *dumps_dir* are injectable for tests (same pattern as the
    crash-dump store). Never raises: any failure returns an inactive decision.
    """
    try:
        if cfg is None:
            cfg = KiroCrewConfig.load()
        if not getattr(getattr(cfg, "dashboard", None), "cautious_boot", True):
            return CautiousBootDecision(False, 0.0, "disabled via dashboard.cautious_boot")

        dump = newest_dump_with_stacks(dumps_dir)
        if dump is None:
            return CautiousBootDecision(False, 0.0, "no prior loop-stall crash dump")

        age = dump_age_seconds(dump)
        if age >= RECENT_DUMP_MAX_AGE_SECS:
            return CautiousBootDecision(
                False, 0.0, f"prior dump is {age / 60.0:.0f} min old (not recent)"
            )

        # The dump says the LAST boot died under pressure; the posture probe
        # says whether the pressure is STILL here. Both signals together pick
        # the delay. probe() never raises (returns "unknown" on failure).
        posture = resource_status.probe(cfg).posture
        if posture in (resource_status.POSTURE_TIGHT, resource_status.POSTURE_CRITICAL):
            delay = MAX_DELAY_SECS
        else:
            delay = MILD_DELAY_SECS
        return CautiousBootDecision(
            True,
            delay,
            (
                f"prior loop-stall crash dump {dump.name} is {age / 60.0:.1f} min old; "
                f"current host posture is {posture}"
            ),
        )
    except Exception:
        # Fail OPEN — a broken evaluation must never delay or block a boot.
        logger.warning("cautious-boot evaluation failed; booting normally", exc_info=True)
        return CautiousBootDecision(False, 0.0, "evaluation failed (fail-open)")


async def initialize() -> CautiousBootDecision:
    """Evaluate the cautious-boot decision once, off-loop, and cache it.

    Awaited exactly once, early in gateway startup — before the first battery
    group. The whole evaluation (config load, dump ``stat``, ``/proc`` reads)
    runs in a worker thread so nothing blocks the event loop. ``RuntimeError``
    from ``asyncio.to_thread`` (executor exhaustion/shutdown) fails open.
    """
    global _decision
    try:
        _decision = await asyncio.to_thread(_evaluate)
    except RuntimeError:
        logger.warning("cautious-boot probe skipped (no worker thread); booting normally")
        _decision = CautiousBootDecision(False, 0.0, "no worker thread (fail-open)")
    if _decision.active:
        # Loud on purpose: an operator reading the journal after an incident
        # must see that the gateway noticed the crash AND changed its behavior.
        logger.warning(
            "🐢 Cautious boot ACTIVE: %s — staggering the startup battery "
            "(%.0fs pause between groups). Disable via dashboard.cautious_boot.",
            _decision.reason,
            _decision.delay_secs,
        )
    else:
        logger.debug("cautious boot inactive: %s", _decision.reason)
    return _decision


async def pause_before(group: str) -> None:
    """Pause briefly before launching *group* when cautious boot is active.

    No-op when inactive or never initialized. The cached decision is immutable,
    so nothing read before this await can go stale because of it; callers'
    own post-await state is unaffected (this function only sleeps).
    """
    decision = _decision
    if decision is None or not decision.active:
        return
    logger.info(
        "cautious boot: pausing %.0fs before starting %s", decision.delay_secs, group
    )
    await asyncio.sleep(decision.delay_secs)


def _reset_for_tests() -> None:
    """Clear the boot-scoped cache (test isolation only)."""
    global _decision
    _decision = None
