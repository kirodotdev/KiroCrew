"""A packaged build prefers Kiro Crew's own KAS engine when ``agent.kas_bundle`` is unset.

A saved ``agent.kas_bundle`` always wins: ``true`` is the engine on every KAS spawn
(``kas_route`` rule 1), ``false`` is the relay wherever it can run. With the key
unset, a packaged build (one ``scripts/stamp-distribution.sh`` stamped: a desktop
app, a wheel, an image; never a source checkout) PREFERS the engine: a KAS spawn
takes it when Kiro Crew's own sign-in is usable, and otherwise keeps the rules it had
(``kas_route`` rule 1b). Insider and stable ship the same bytes, so the stamp that
decides is the distribution, not the release lane. A host the engine cannot run on
never prefers it.

kiro-cli's sign-in is never copied: a host signed in to kiro-cli and not to Kiro
Crew stays on kiro-cli until someone signs in to Kiro Crew.

Two kinds of host, told apart by :data:`ENGINE_RAN_LEAF`:

* **First switch**: the default moved this host off kiro-cli and the engine has
  never started here. If that start fails, the chat runs on kiro-cli once
  (``runtime_spawn_entry``) and new chats keep kiro-cli for
  :data:`FIRST_SWITCH_COOLDOWN_SECS`.
* **Already on the engine**: an engine start has succeeded here once. A failure is
  fixed in place (bounded retries, then the chat's own Retry); kiro-cli is never
  offered again.
"""

from __future__ import annotations

import asyncio
import logging
import time
from functools import partial
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "ENGINE_FALLBACK_NOTICE",
    "ENGINE_RAN_LEAF",
    "build_default_plan",
    "FIRST_SWITCH_COOLDOWN_SECS",
    "engine_has_run",
    "first_switch_cooling_down",
    "kas_bundle_preferred_by_build",
    "note_first_switch_failed",
    "remember_engine_ran",
]

#: Data-home marker written after the first engine start that succeeded here. No
#: secret and no ceiling, so an ordinary sandbox-visible leaf: a hand-written one
#: can only turn off the kiro-cli fallback for a first switch.
ENGINE_RAN_LEAF = "kas_engine_ran"

#: What a chat says when the build default's first move to the engine failed and the
#: chat runs on kiro-cli instead. The cause is in the gateway log only.
ENGINE_FALLBACK_NOTICE = (
    "Kiro Crew's engine didn't start, so this chat uses Kiro CLI. "
    "New chats try the engine again in 10 minutes."
)

#: How long a failed first switch keeps new chats on kiro-cli before the next one
#: tries the engine again.
FIRST_SWITCH_COOLDOWN_SECS = 600.0


#: ``time.monotonic`` of the last failed first switch in this process, or ``0.0``.
_FIRST_SWITCH_FAILED_AT = 0.0
#: Set once this process has written or read the marker, so the hot path skips the stat.
_ENGINE_RAN = False


def kas_bundle_preferred_by_build() -> bool:
    """Whether this build prefers the engine for an unset ``agent.kas_bundle``.

    True in a packaged build on a host the engine can run on. Blocking (a glibc
    read on Linux); never raises, and an unanswerable probe answers False.
    """
    from kiro_crew import beacon

    # Every stamped distribution but ``source``, the one a checkout reads as.
    if beacon.baked_distribution() not in beacon.KNOWN_DISTRIBUTIONS - {"source"}:
        return False
    try:
        from kiro_crew.agent_sdk import kas_bundle as sdk_kas_bundle

        # An installed engine already answers "this host runs it": no host probe.
        if sdk_kas_bundle.installed_bundle() is not None:
            return True
        return not sdk_kas_bundle.host_unsupported_reason()
    except Exception:  # noqa: BLE001 - an unanswerable probe keeps the relay
        logger.debug("could not tell whether this host runs the KAS engine", exc_info=True)
        return False


def _marker_path() -> Path:
    from kiro_crew.config.paths import data_home

    return data_home() / ENGINE_RAN_LEAF


def engine_has_run() -> bool:
    """Whether an engine start has succeeded on this host. Blocking (one stat)."""
    global _ENGINE_RAN
    if _ENGINE_RAN:
        return True
    try:
        _ENGINE_RAN = _marker_path().is_file()
    except OSError:
        return False
    return _ENGINE_RAN


def remember_engine_ran() -> None:
    """Record that an engine start succeeded here. Blocking; never raises."""
    global _ENGINE_RAN, _FIRST_SWITCH_FAILED_AT
    _FIRST_SWITCH_FAILED_AT = 0.0
    if engine_has_run():
        return
    try:
        from kiro_crew.atomic_write import atomic_write

        atomic_write(_marker_path(), "1\n")
        _ENGINE_RAN = True
        logger.info("KAS engine started here once; later failures are fixed in place")
    except OSError as exc:
        logger.warning("could not record that the KAS engine ran: %s", exc)


def already_on_engine() -> bool:
    """Whether a failed engine start here is fixed in place, never moved to kiro-cli.

    The engine already ran on this host, or kiro-cli's relay cannot run here, so
    there is nothing to fall back to. Blocking (a stat, plus the relay check).
    """
    if engine_has_run():
        return True
    from kiro_crew.acp import kas_route

    return not kas_route.relay_can_run()


def note_first_switch_failed(detail: str) -> None:
    """Record a failed first switch; new chats keep kiro-cli for the cooldown."""
    global _FIRST_SWITCH_FAILED_AT
    _FIRST_SWITCH_FAILED_AT = time.monotonic()
    logger.warning(
        "Kiro Crew's KAS engine did not start on its first try here (%s); this chat "
        "runs on kiro-cli, and new chats try the engine again after %gs",
        detail or "start failed",
        FIRST_SWITCH_COOLDOWN_SECS,
    )


def first_switch_cooling_down() -> bool:
    """Whether a failed first switch still keeps new chats on kiro-cli. No IO."""
    if not _FIRST_SWITCH_FAILED_AT:
        return False
    return time.monotonic() - _FIRST_SWITCH_FAILED_AT < FIRST_SWITCH_COOLDOWN_SECS


async def build_default_plan(harness: Any, ctx: Any) -> Any:
    """Rule 1b's spawn plan for ``KasHarness.resolve_spawn``, or ``None`` to go on.

    ``None`` when the rule does not hold, and on a first switch whose plan cannot be
    built (the engine cannot be installed or its Node cannot run): that chat keeps
    kiro-cli and carries :data:`ENGINE_FALLBACK_NOTICE`. On a host the engine already
    ran on, the failure is the chat's own, so it propagates.
    """
    from kiro_crew.acp import kas_route
    from kiro_crew.acp.session_handle import AcpRuntimeError

    selects = partial(
        kas_route.build_default_selects_bundle, pin=ctx.harness_pin, environ=ctx.environ
    )
    if not await asyncio.to_thread(selects):
        # Kept on kiro-cli only by a failed first switch: this chat says so too.
        # It may be a later spawn than the one that failed (another chat, or a
        # pre-started session), which never carried that start's notice.
        if first_switch_cooling_down() and await asyncio.to_thread(selects, cooldown=False):
            harness.engine_fallback_notice = ENGINE_FALLBACK_NOTICE
        return None
    try:
        plan = await harness._bundle_spawn_plan(
            ctx,
            kas_route.KasRoute(bundle=True, reason=kas_route.REASON_BUILD_DEFAULT),
            probe_node=True,
        )
    except AcpRuntimeError as exc:
        harness.bundle_engine_version = ""
        if await asyncio.to_thread(already_on_engine):
            raise
        note_first_switch_failed(str(exc))
        harness.engine_fallback_notice = ENGINE_FALLBACK_NOTICE
        return None
    harness.spawn_route_reason = kas_route.REASON_BUILD_DEFAULT
    return plan
