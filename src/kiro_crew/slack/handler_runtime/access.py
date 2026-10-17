"""Who may drive the native Slack path, and the live references it reads.

The owner and allowlist predicates every Slack entry point gates on, the per-session
Trust and global YOLO grants (both kept by their shared owners, ``messaging.session_trust``
and ``safety_override``), the tracked-channel set, the orchestrator config and dashboard
state the gateway installs after import, and the background-task set that shutdown cancels.
The values themselves are module state of :mod:`kiro_crew.slack.handler`; these functions
read and write them there.

Composed onto :mod:`kiro_crew.slack.handler`; see
:mod:`kiro_crew.slack.handler_runtime`.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.slack.handler import _allowed_users  # noqa: F401 - set via ``global``
    from kiro_crew.slack.handler import _open_channels  # noqa: F401 - set via ``global``
    from kiro_crew.slack.handler import (
        _SLACK_OWNED_FIELDS,
        KiroCrewConfig,
        SessionManager,
        _add_trusted_session,
        _background_tasks,
        _dashboard_state,
        _orch_cfg,
        _owner_id,
        _tracking_channels,
        apply_config_duration,
        clear_trusted_sessions,
        grant_declared_yolo,
        is_session_trusted,
        load_voice_reply_config,
        logger,
        safety_override,
    )


def cancel_background_tasks() -> None:
    """Cancel pending background tasks during gateway shutdown."""
    for t in _background_tasks:
        t.cancel()
    _background_tasks.clear()


def track_background_task(task: "asyncio.Task[Any]") -> None:
    """Hold a strong reference to *task* until it finishes.

    Both halves matter: without the reference the loop may collect a running task
    mid-flight, and without the registration :func:`cancel_background_tasks`
    cannot stop it at shutdown. The transport dispatcher shares this set so a
    fire-and-forget turn it starts is torn down with the gateway too.
    """
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def set_allowed_users(user_ids: set[str]) -> None:
    """Set the allowed user IDs for Slack access (called by gateway)."""
    global _allowed_users
    _allowed_users = user_ids


def set_owner_id(owner_id: str) -> None:
    """Set the primary owner ID for owner-only commands (called by gateway)."""
    global _owner_id
    _owner_id = owner_id


def set_yolo_mode(enabled: bool) -> None:
    """Set YOLO mode at startup from config (called by gateway).

    ``dangerouslySkipPermissions`` is a standing instruction, so the grant does not
    expire — see ``safety_override.grant_declared_yolo``. A headless
    ``--slack-only`` gateway never runs the dashboard startup path, so the same
    helper is called here or YOLO would still lapse for exactly the users
    driving the agent from another channel.
    """
    apply_config_duration()
    if enabled:
        grant_declared_yolo()


def set_orch_cfg(cfg: KiroCrewConfig) -> None:
    """Store a live reference to the orchestrator's config (called by events.py)."""
    global _orch_cfg
    _orch_cfg = cfg
    load_voice_reply_config(cfg)


def set_dashboard_state(state: object) -> None:
    """Store dashboard state reference for push_refresh (called by gateway)."""
    global _dashboard_state
    _dashboard_state = state


def get_dashboard_state() -> object | None:
    """The live dashboard state, or None when running without a dashboard.

    An accessor rather than a direct read of the global: the gateway installs
    the state AFTER import, so a caller that imported the name would capture
    None forever.
    """
    return _dashboard_state


def get_orch_cfg() -> "KiroCrewConfig | None":
    """The orchestrator's live config, or None before the gateway installs it.

    Same reason as :func:`get_dashboard_state` -- the value is set post-import.
    """
    return _orch_cfg


def slack_cfg(orch: object | None = None) -> KiroCrewConfig:
    """The config every Slack read consults -- one object, whichever door you enter by.

    ``orch._cfg``, this module's ``_orch_cfg`` and every dispatcher's captured
    ``cfg`` are the SAME object in a running gateway: :func:`set_orch_cfg`
    installs the orchestrator's own config, and nothing rebinds it any more --
    the ``!channel`` path and the config applier both mutate it IN PLACE
    (:func:`_reload_orch_cfg`, :func:`adopt_slack_config`). That is what closes
    the divergence a rebind would otherwise open, and it is why reading through the
    caller's *orch* is safe rather than a second view.

    Resolution order: the caller's orchestrator, then the installed global, then
    the config watcher's snapshot, then a load. So a Slack read reaches the same
    object whether it holds the orchestrator or not, and a process with no
    orchestrator at all (a dashboard-only gateway) still reads live config.
    """
    cfg = getattr(orch, "_cfg", None)
    if cfg is not None:
        return cfg  # type: ignore[return-value]
    if _orch_cfg is not None:
        return _orch_cfg
    from kiro_crew.config import live

    return live.snapshot() or KiroCrewConfig.load()


def copy_slack_fields(source: KiroCrewConfig, target: KiroCrewConfig) -> None:
    """Copy the Slack-owned fields of *source* onto *target* in place."""
    for name in _SLACK_OWNED_FIELDS:
        if hasattr(source, name):
            setattr(target, name, getattr(source, name))


def adopt_slack_config(fresh: KiroCrewConfig) -> None:
    """Bring the shared config object up to date with *fresh*, in place.

    In place and never rebound: the object installed by :func:`set_orch_cfg` is
    the same one the orchestrator and every dispatcher hold, so replacing the
    binding here would leave those holders on the old object. Called by the
    gateway's config applier with the reloaded config; a no-op before the
    orchestrator has installed one.
    """
    if _orch_cfg is not None and _orch_cfg is not fresh:
        copy_slack_fields(fresh, _orch_cfg)


def _reload_orch_cfg(fresh: "KiroCrewConfig | None" = None) -> None:
    """Refresh channel activations on the shared config object after a ``!channel`` write.

    Synchronous on purpose: the write just landed and the next inbound message
    may arrive before the config watcher's poll, so the caller must not wait for
    it. The watcher applies the same two fields again when it sees the write,
    which is idempotent. *fresh* lets a caller that already holds the reloaded
    config skip the load.
    """
    if _orch_cfg is not None:
        if fresh is None:
            fresh = KiroCrewConfig.load()
        _orch_cfg.slack_channels = fresh.slack_channels
        _orch_cfg.slack_dm_activation = fresh.slack_dm_activation


def is_owner(user_id: str) -> bool:
    """Check if *user_id* is the primary owner (with W/U prefix cross-match)."""
    if not _owner_id or not user_id:
        return False
    if user_id == _owner_id:
        return True
    return user_id.replace("W", "U", 1) == _owner_id or user_id.replace("U", "W", 1) == _owner_id


def disable_yolo() -> None:
    """Disable YOLO mode (global auto-approve).

    Gated on ``has_grant()``, NOT ``is_active()``. The latter is policy-filtered and
    reports False while the governance verdict is momentarily unknown, so gating an
    explicit off on it skipped the teardown and let the grant resume once the refresh
    settled -- the operator's revocation silently undone.
    """
    if not safety_override().has_grant():
        return
    safety_override().deactivate("slack")
    # Through the shared revoke, which undoes BOTH halves of each grant. Dropping
    # only the in-memory mapping leaves every granted session's approval_policy at
    # "auto", and a subagent reads that policy rather than the mapping, so a later
    # spawn would inherit a trust this call just revoked.
    clear_trusted_sessions()
    logger.info("YOLO mode OFF")


def enable_yolo_with_ttl(ttl_secs: int) -> None:
    """Enable YOLO mode with a specific TTL."""
    safety_override().activate("slack", ttl=ttl_secs)
    logger.info("YOLO mode ON (expires in %ds)", ttl_secs)


def is_yolo_mode() -> bool:
    """Return whether YOLO mode is currently active."""
    return safety_override().is_active()


def is_slack_session_trusted(session_key: str) -> bool:
    """Return whether *session_key* has been granted per-session Trust.

    Per-session trust auto-approves all subsequent tools for THIS session only
    (distinct from global YOLO). Populated by the Trust button on both the
    native and messaging-transport approval prompts.
    """
    return is_session_trusted(session_key)


def add_trusted_session(
    session_key: str, sessions: "SessionManager | None" = None, strict: bool = False
) -> None:
    """Grant per-session Trust for *session_key* (mirrors native trust_tool).

    Adds the session to the in-memory trust set and, when a SessionManager is
    supplied, sets its approval policy to ``auto`` so spawned subagents inherit
    the trust (they read the parent's approval policy, not the in-memory set).

    ``strict=True`` propagates a failing policy write (after undoing the in-memory
    half) rather than logging it, for a caller that has to report the grant back to
    the clicker and must not call a partial grant a grant.
    """
    _add_trusted_session(session_key, sessions, strict=strict)


def is_allowed_user(user_id: str) -> bool:
    """Check if user_id is the owner.

    Multi-user access is disabled for security — only the owner
    (KIROCREW_OWNER_ID) is authorized to interact via Slack.
    """
    if not user_id:
        return False
    return is_owner(user_id)


def set_tracking_channels(channel_ids: set[str]) -> None:
    """Set the tracked channel IDs (called by gateway/interactions)."""
    global _tracking_channels
    _tracking_channels = channel_ids


def set_open_channels(channel_ids: set[str]) -> None:
    """Set channel IDs where all users are authorized (no allowlist needed)."""
    global _open_channels
    _open_channels = channel_ids


def is_open_channel(channel_id: str) -> bool:
    """Open channels are disabled — multi-user access is blocked for security."""
    return False


def is_tracked_channel(channel_id: str) -> bool:
    """Check if *channel_id* is in the tracking set."""
    return bool(channel_id and channel_id in _tracking_channels)
