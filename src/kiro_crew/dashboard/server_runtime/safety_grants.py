"""The safety-override grant at startup and the notices around it.

The declared standing grant, the record of a grant the previous process lost, and the
expiry notices for an override and for an unattended run it was carrying.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.dashboard.server import (
        _UNATTENDED_EXPIRY_TITLE,
        POLICY_REVOKED_SOURCE,
        DashboardState,
        KiroCrewConfig,
        _autonudge_get,
        _dm_owner,
        apply_config_duration,
        describe_dropped_grant,
        grant_declared_yolo,
        logger,
        safety_override,
        standing_approval,
        take_dropped_grant,
    )


def _take_prior_dropped_grant() -> Any:
    """Consume the PREVIOUS process's safety-override record, if any.

    Run off the event loop (the caller wraps it in ``asyncio.to_thread``): it is a
    file open on a filesystem that may be slow, and nothing about boot should wait
    on it. Ordering against ``_apply_startup_yolo`` does not matter, because the
    record carries the writing pid and this process's own record is never read as
    a dropped one. Never raises: the gateway must not fail to boot over a
    notification, and the grant is off either way.
    """
    try:
        return take_dropped_grant()
    except Exception:
        logger.debug("Could not read the prior safety-override record", exc_info=True)
        return None


def _apply_startup_yolo(state: DashboardState, cfg: Any) -> None:
    """Enable the safety override at startup if the operator declared it.

    The declaration is read from the operator-owned keystone
    (``standing-approval/grant.json``), never from ``config.json``. A standing skip of
    every tool approval is the widest authorization this product grants, and
    ``config.json`` is a document a sandboxed process can reach: sealed read-only, but
    readable, which leaves the inode behind the sealed name a ``link(2)`` source. The
    keystone is bind-masked instead, so no sandboxed process can open it at all, and it
    is a directory, which has no ``link(2)`` source even in principle.

    The grant it creates does not expire -- a lapse after 24h would silently drop the
    user back to prompt-for-everything, which breaks flows driven from Slack/Discord and
    from cron where nobody is watching the dashboard to re-enable it.

    State is in-memory, so the grant is re-established and re-audited on every startup
    rather than persisted. An enterprise policy can forbid a never-expiring grant (the
    ``yolo_duration`` governance scope), in which case it falls back to the ad-hoc
    duration. Picking another approval mode still clears it immediately.

    ``agent.dangerously_skip_permissions`` in ``config.json`` is a DEPRECATED ALIAS: it
    still grants on every platform, so no operator loses the grant on upgrade and no
    platform is narrowed. When it is set, the operator is told once, at WARNING, that the
    key is deprecated and names the keystone to move to; the grant is still made. Retiring
    it would be a product-shape change the First-Principles review blocks.

    Ad-hoc grants are untouched: Slack, the dashboard picker and the API all expire on
    the single ``agent.yolo_duration`` value (default 6h).
    """
    # Seed the ad-hoc TTL even when yolo is off, so a later dashboard/Slack
    # activation uses the configured duration rather than the built-in default.
    try:
        apply_config_duration()
    except Exception:
        logger.warning("Could not apply the configured YOLO duration", exc_info=True)

    # Resolve the sandbox mode from the config in force NOW, immediately before
    # activation -- never the boot copy. The keystone grant is honoured only where
    # Kiro Crew's own sandbox masks the leaf away from the agent; if the operator
    # flipped sandbox OFF (or down to an unmasked tier) between boot and this
    # activation, the cached ``cfg.agent.sandbox`` would still read "masked" and
    # activate a grant the live host does not protect. ``live.current`` returns the
    # watcher snapshot (else a disk reload, else the boot copy) so a mid-flight unmask
    # leaves the grant suspended rather than activated under a stale mode.
    from kiro_crew.config import live

    sandbox_mode = live.current(cfg, log_prefix="dashboard-yolo").agent.sandbox
    # Two paths grant the standing override, in order of preference:
    #   1. the keystone -- the SECURE path, honoured only where the sandbox masks the
    #      leaf away from the agent (``is_declared``);
    #   2. ``agent.dangerously_skip_permissions`` in config.json -- a DEPRECATED ALIAS
    #      kept working so no operator loses their standing grant on upgrade, and so the
    #      grant is NOT narrowed off platforms the keystone cannot cover yet (Windows,
    #      kiro-cli-delegated macOS, ``sandbox: off``). Retiring it, or refusing it on
    #      those platforms, would be a product-shape change the First-Principles review
    #      blocks; this keeps today's behaviour and warns instead.
    # The config key is the agent-writable surface the keystone exists to replace, so the
    # deprecation warning tells the operator to migrate -- but it still GRANTS until they
    # do, which is the backwards-compatible, non-narrowing behaviour.
    source_desc: str
    if standing_approval.is_declared(sandbox_mode):
        source_desc = "standing-approval keystone"
    elif cfg.agent.dangerously_skip_permissions:
        # Deprecated alias: still honoured, with a one-line migration warning and the
        # keystone path/line to move to. This path is reachable on every platform,
        # including those where the keystone mask is unavailable, so nobody loses the
        # grant they have today.
        logger.warning(
            "agent.dangerously_skip_permissions in config.json is DEPRECATED as a "
            "standing auto-approve switch and will stop granting in a future release. "
            "It still grants for now. %s",
            standing_approval.migration_notice(sandbox_mode),
        )
        source_desc = "config.json agent.dangerously_skip_permissions (deprecated)"
    else:
        return
    try:
        result = grant_declared_yolo()
    except Exception:
        logger.error("Failed to activate safety override at startup", exc_info=True)
        return
    if not result.active:
        logger.error("Safety override activation refused (SEL audit failure?)")
        return
    logger.info(
        "Safety override enabled at startup (%s, %s)",
        source_desc,
        "no expiry" if result.ttl == 0 else f"expires in {result.ttl}s per policy",
    )


def _armed_unattended_loops() -> "list[Any]":
    """Nudge loops still marked active, for the expiry notice only.

    Deliberately a plain ``active`` read rather than a careful liveness test: this
    decides whether to TELL someone, and a false positive costs one redundant
    notice. Nothing is granted on the strength of it, so there is no reason to pay
    for a stop-sentinel stat or to re-derive the loop's bounds — and this runs on
    the event loop, reached from tool-approval paths.
    """
    try:
        svc = _autonudge_get()
        if svc is None:
            return []
        return [lp for lp in svc.list_all() if getattr(lp, "active", False)]
    except Exception:
        logger.debug("could not enumerate nudge loops for the expiry notice", exc_info=True)
        return []


def _unattended_expiry_text(loop_count: int, source: str) -> str:
    """Body shared by the dashboard note and the owner DM, so the two cannot drift.

    Names the remedy as well as the cause: ``agent.yolo_duration`` accepts
    ``until_shutdown``, which has no timed expiry. The cheapest half of this
    problem is that operators do not know that option exists, and the moment it
    would have helped is the moment worth saying so.

    EXCEPT after a policy revocation (``source == POLICY_REVOKED_SOURCE``): both
    halves of that remedy — re-enabling auto-approve and ``until_shutdown``, a
    ``yolo_duration`` scope member — are refused by the same fail-closed
    ``approval_modes`` gate that revoked the grant, so suggesting them directs
    the one operator who is not present into a wall. The stall
    description stays; only the remedy is replaced with the actual cause.

    The stall is stated conditionally because global auto-approve is not the only
    path to one: a slot carrying its own trust grant is approved by ``slot._trust``
    independently of the grant, so its cycles keep running after this expiry.
    Claiming the run has stopped would send an operator to rescue a healthy one.
    """
    stall = (
        f"{loop_count} monitor loop(s) are still running, but auto-approval has "
        f"ended, so any cycle that relied on it now waits on a per-tool approval "
        f"that nobody is there to give. (A session granted its own trust is "
        f"unaffected.)"
    )
    if source == POLICY_REVOKED_SOURCE:
        return (
            f"{stall} Auto-approve was disabled by organization policy, so it "
            f"cannot be re-enabled while the policy is in effect — contact your "
            f"administrator if you believe this is unexpected."
        )
    return (
        f"{stall} Re-enable auto-approve to resume. For runs meant to go "
        f"unattended overnight, Settings → agent.yolo_duration has an "
        f"'until_shutdown' option that has no timed expiry."
    )


def _notify_unattended_expiry(state: "DashboardState", source: str) -> None:
    """Report an expiry that landed on an unattended run, on BOTH surfaces.

    An ordinary expiry degrades gracefully — the next tool call asks a human, and
    a human is there to answer. This one degrades into nothing: the loop keeps
    waking, dispatches a tool, waits out the approval window with nobody present,
    and accomplishes no work until someone notices.

    Delivered to the dashboard feed AND pushed to the owner's DM, because the
    operator this exists for is by definition not looking at a dashboard. Neither
    delivery is gated behind ``agent.notify_override_expiry``: that switch silences
    a recurring *expiry* notice, while this says a run in flight stopped being able
    to work — a different and stronger fact, and one an operator who muted the
    former did not ask to be uninformed about.
    """
    armed = _armed_unattended_loops()
    if not armed:
        return
    logger.warning(
        "Safety override expired with %d unattended loop(s) still running; "
        "every further cycle will wait on per-tool approval",
        len(armed),
    )
    body = _unattended_expiry_text(len(armed), source)
    try:
        state.notify(
            "safety_override",
            _UNATTENDED_EXPIRY_TITLE,
            body,
            meta={"loops": len(armed), "source": source},
        )
    except Exception:
        # ERROR, not debug: this notice is the only operator-visible trace that an
        # unattended run stopped working rather than finished. Losing it silently
        # reproduces the failure it exists to explain.
        logger.error("unattended-expiry notification failed", exc_info=True)

    # The push half. Scheduled directly rather than through
    # _dispatch_override_expiry_notification, which applies the recurring-expiry
    # mute this notice deliberately does not inherit.
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("no running event loop — unattended-expiry DM skipped")
        return
    task = loop.create_task(_dm_owner(state, f"{_UNATTENDED_EXPIRY_TITLE}\n\n{body}"))
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)


def _override_expiry_dm_text(source: str) -> str:
    """Owner-DM body for an override expiry, worded by what actually happened.

    A POLICY revocation (``source == POLICY_REVOKED_SOURCE``) is not an expiry
    the operator can undo: ``_commit_activation``'s fail-closed
    ``approval_modes`` gate refuses the very ``/kirocrew yolo`` a re-arm
    suggestion would name, so suggesting it directs the operator into a wall
    without naming the cause. Presentation only — the gate and its SEL audit are
    untouched. (The unattended-run notice applies the same source split in
    ``_unattended_expiry_text``.)

    Every other source keeps the re-armable wording byte-identical: a TTL lapse
    IS re-armable, and that text is pinned by tests.
    """
    if source == POLICY_REVOKED_SOURCE:
        return (
            "\U0001f512 Auto-approve was disabled by organization policy, and the "
            "active safety override has been revoked. Tools now require approval. "
            "Re-authorization is refused while the policy is in effect — contact "
            "your administrator if you believe this is unexpected."
        )
    return "\U0001f512 Safety override expired. Tools now require approval. Reply `/kirocrew yolo` to re-authorize."


async def _notify_slack_override_expired(state: DashboardState, source: str) -> None:
    """Post the override expiry notice to the owner DM, worded by cause.

    Module-level (not a ``start_dashboard`` closure) so the seam this fix added —
    ``source`` travelling from the expiry callback into the DM body — is
    directly testable; a closure would leave that wiring uncovered.
    """
    await _dm_owner(state, _override_expiry_dm_text(source))


def _dispatch_override_expiry_notification(
    state: DashboardState, notify_coro_factory: Any, source: str
) -> bool:
    """Schedule the Slack override-expiry DM unless disabled via config.

    Gated by ``agent.notify_override_expiry`` (read live so it can be toggled
    without a restart). Returns True if a notification task was scheduled, False
    if skipped — either disabled via config or no running event loop.

    ``source`` is the expiry trigger (``policy`` for a policy revocation, else
    the activating source) and is handed to ``notify_coro_factory`` so the DM
    can word the notice by cause. The config gate deliberately does not vary by
    source: ``agent.notify_override_expiry`` mutes the recurring expiry notice
    as a class, whichever way the grant ended.
    """
    if not KiroCrewConfig.load().agent.notify_override_expiry:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("No running event loop — Slack expiry notification skipped")
        return False
    task = loop.create_task(notify_coro_factory(source))
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)
    return True


async def _notify_restart_dropped_grant(state: DashboardState) -> None:
    """Tell the operator about a safety-override grant the previous process lost.

    A grant that was live when the process went down is GONE -- grants are
    in-memory by design and this does not change that. What it changes is that
    the operator now hears about it. Without this, someone who granted six
    hours of auto-approval and restarted an hour later got no signal at all:
    the next unattended run just stopped on a prompt nobody was waiting for.

    Read OFF the loop and off the boot path: it is a file open on a filesystem
    that may be slow, and nothing about boot should wait on it (found in
    review). Safe to run after the startup grant because the record carries the
    writing pid, so this process's own record is never read as a dropped one.

    Notice only, never a restored grant, and withheld when auto-approve is live
    RIGHT NOW: a declared grant that the enterprise ceiling clamps to a timed
    one is re-established by ``_apply_startup_yolo`` first, and telling the operator
    it is "OFF" while it is on would be worse than saying nothing. A lapsed
    grant, a config-declared one and an ``until_shutdown`` one are all silent
    too -- see ``take_dropped_grant``.
    """
    try:
        _dropped_grant = await asyncio.to_thread(_take_prior_dropped_grant)
        if _dropped_grant is not None and not safety_override().is_active():
            state.notify(
                "safety",
                "Auto-approve was dropped by a restart",
                describe_dropped_grant(_dropped_grant),
                meta={
                    "source": _dropped_grant.source,
                    "remaining_secs": _dropped_grant.remaining_secs,
                },
            )
    except Exception:
        # Startup must not fail over a notification. The grant is off either
        # way; the worst case is the operator not being told.
        logger.debug("Could not report a restart-dropped safety override", exc_info=True)
