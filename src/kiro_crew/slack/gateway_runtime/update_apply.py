"""Stage an available update unattended.

A managed-venv install gets the new wheel built beside the live venv and promoted,
and is left waiting for a restart a person chooses (``update_restart_ready``): no
unattended path restarts a running gateway or rewrites the tree it serves from.
Every other install route only notifies; a person's Update arms the apply for the
next start (:mod:`kiro_crew.update_boot_apply`).

Orchestrator fields read: ``_wheel_update_noticed``, ``dashboard_state``.
Orchestrator fields written: ``_update_apply_deferred``.

Composed by :mod:`kiro_crew.slack.gateway`, whose globals its functions run on;
see :mod:`kiro_crew.slack.gateway_runtime`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio

    from kiro_crew.slack.gateway import GatewayOrchestrator, logger


async def _auto_apply_wheel_update(
    self: GatewayOrchestrator,
    channel: str,
    version: str,
    *,
    mandatory: bool = False,
) -> None:
    """Stage a managed-venv update unattended, building it BESIDE the live venv.

    Runs :func:`kiro_crew.platform.wheel_apply.run_wheel_apply`, the path
    ``POST /api/update/approve`` runs too (and the engine ``kirocrew update``
    drives): the signed manifest and wheel digest are verified,
    ``crew-venv-<version>`` is built as a sibling tree, proven to serve the
    version, memory is copied, and ``crew-venv-current`` is flipped with
    ``os.replace``. Nothing this process loads from is moved, rewritten or
    deleted, so a stop at any point before the flip leaves the running
    install whole. That holds because this process runs from a versioned
    tree, not through the link: a restart execs the link's RESOLVED tree, and
    a gateway an earlier version restarted through the link itself
    (:func:`~kiro_crew.platform.wheel_apply.relaunch_before_apply`) restarts
    onto its resolved tree, at the same version, before anything is built.

    Admission stays OPEN while the tree builds, and the gateway is NEVER
    restarted into the promoted tree from here (decision entry "updates never
    restart a running gateway" in ``docs/decisions``). A
    promoted tree is recorded as "update ready, restart to use it"
    (:mod:`kiro_crew.update_restart_ready`), the dashboard shows it, and the new
    version starts at the next stop or restart a person chooses. A policy floor
    stages the same way and keeps its prominent notice, without the restart.

    Preconditions (checked by the caller): ``auto_update`` is on or a policy
    floor mandates the update, ``auto_update_effect`` chose the wheel route (a
    managed venv on POSIX, not a git checkout, not externally managed), and
    the check reported *version* newer on *channel*. The source pin and the
    CDN shape are checked again here, by the shared preflight.
    """
    from kiro_crew import update_restart_ready
    from kiro_crew.platform import wheel_apply
    from kiro_crew.platform.wheel_engine import WheelUpdateError, check_release_version

    state = self.dashboard_state
    try:
        check_release_version(version)
        feed_base, artifact_base = wheel_apply.preflight_bases()
    except (WheelUpdateError, wheel_apply.WheelApplyRefused) as exc:
        # A version no tree can be named for, or a preflight refusal.
        logger.warning("Auto-update (wheel) refused: %s", exc)
        if state:
            state.push_refresh("update_available")
        return
    # Promotion would move the launcher a userns AppArmor profile applies to,
    # and only the operator can re-attach it, so this waits for them, a policy
    # floor included: the floor is then retried on the short cadence, and the
    # notice says exactly what to run.
    if await asyncio.to_thread(wheel_apply.userns_reattach_needed, version):
        logger.warning(
            "Auto-update (wheel) to %s waits for the operator: it would detach "
            "the sandbox's AppArmor profile",
            version,
        )
        self._notice_wheel_update_once(
            f"reattach:{version}",
            f"Kiro Crew {version} needs you to apply it",
            wheel_apply.userns_reattach_remedy(version),
        )
        if mandatory:
            self._update_apply_deferred = True
        if state:
            state.push_refresh("update_available")
        return

    if await asyncio.to_thread(wheel_apply.relaunch_before_apply):
        # The flip would swap this process's own modules under it, and the only
        # way past that is to restart a running gateway, which no unattended path
        # does. The gateway keeps its loaded code and offers the update to a
        # person; once it has restarted onto its resolved tree this builds.
        logger.info(
            "Auto-update (wheel): this gateway loads its code through the stable "
            "link, so %s is offered for a restart instead of built beside it",
            version,
        )
        if state:
            state.push_refresh("update_available")
        return

    ready = update_restart_ready.current()
    if (
        ready is not None
        and ready.reason == update_restart_ready.REASON_STAGED
        and ready.version == version
    ):
        # Built and promoted on an earlier cycle; only a restart is left.
        return

    logger.info("Auto-update (wheel): building %s beside the running install", version)
    outcome = await wheel_apply.run_wheel_apply(
        channel=channel,
        version=version,
        feed_base=feed_base,
        artifact_base=artifact_base,
        state=state,
        refuse_if_unattended_off=not mandatory,
    )
    if outcome.status == "promoted":
        if not await asyncio.to_thread(wheel_apply.restart_reaches, version):
            # Restarting would exec the running version again, and its next
            # cycle would promote and restart again, for ever.
            logger.error(
                "Auto-update (wheel): %s promoted, but the restart would not reach "
                "it; not restarting",
                version,
            )
            self._notice_wheel_update_once(
                f"unreachable:{version}",
                f"Kiro Crew {version} needs the installer to finish",
                wheel_apply.restart_unreachable_remedy(version, channel),
            )
            if state:
                state.push_refresh("update_available")
            return
        logger.info("Auto-update (wheel): %s promoted; it starts at the next restart", version)
        update_restart_ready.mark(
            update_restart_ready.REASON_STAGED, version=version, mandatory=mandatory
        )
        if state:
            state.push_refresh("update_available")
    elif outcome.status in wheel_apply.RETRY_SOON:
        # Another apply holds the lock, memory is still preparing, or the
        # apply was stopped: retry on the short cadence, pushing nothing onto
        # another apply's progress feed.
        self._update_apply_deferred = True
    elif outcome.status == "incompatible":
        self._notice_wheel_update_once(
            f"incompatible:{version}",
            f"Kiro Crew {version} cannot be applied automatically",
            f"{outcome.message}. {wheel_apply.incompatible_remedy(channel)}",
        )
        if state:
            state.push_refresh("update_available")
    elif state:
        # failed, timed_out, snapshot_failed: an ordinary failure, retried on
        # the normal cadence. A refused memory copy names its own repair.
        suffix = "" if outcome.status == "snapshot_failed" else " — run manually: kirocrew update"
        state.push_update_progress("failed", f"{outcome.message}{suffix}")


def _notice_wheel_update_once(self: GatewayOrchestrator, key: str, title: str, body: str) -> None:
    """Send one dashboard notice per *key* for the life of this process."""
    if key in self._wheel_update_noticed:
        return
    self._wheel_update_noticed.add(key)
    if self.dashboard_state:
        self.dashboard_state.notify("update", title, body)
