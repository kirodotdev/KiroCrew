"""Slack user allowlist and tracking-channel management.

Handles two owner-approval workflows:

1. **User allowlist** — when a user joins a tracked channel
   (``member_joined_channel``) or is nominated via ``/kirocrew @user``,
   the owner gets a DM with Allow / Deny buttons.
2. **Tracking channel** — ``/kirocrew #channel`` sends an Add / Ignore
   prompt to the owner.  Approved channels are persisted to config.json.

Both flows share the same config persistence helpers so changes survive
gateway restarts.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.loader import (
    config_path,
    update_config_locked,
)
from kiro_crew.dashboard.origin import (
    dashboard_origin,
    devspaces_proxy_url,
    is_local_only,
    parse_dashboard_url,
    resolve_dashboard_host,
)
from kiro_crew.dashboard.token_auth import LINK_WINDOW_SECS, MAX_SESSION_TTL_SECS, generate_token
from kiro_crew.sel import sel
from kiro_crew.slack.handler import is_allowed_user, is_tracked_channel
from kiro_crew.tunnel import get_tunnel_url

if TYPE_CHECKING:
    from kiro_crew.slack.client import SlackClientOps

logger = logging.getLogger(__name__)

# Block Kit action IDs shared with the interaction router
ACTION_ALLOWLIST_APPROVE = "allowlist_approve"
ACTION_ALLOWLIST_DENY = "allowlist_deny"
ACTION_TRACK_APPROVE = "track_channel_approve"
ACTION_TRACK_DENY = "track_channel_deny"


# ---------------------------------------------------------------------------
# Owner prompts — builds the Allow/Deny DMs
# ---------------------------------------------------------------------------


async def _send_prompt(
    slack: SlackClientOps,
    owner_id: str,
    text: str,
    approve_label: str,
    deny_label: str,
    approve_action: str,
    deny_action: str,
    value: str,
    fallback: str,
) -> None:
    """Build a two-button Slack prompt and DM it to the owner."""
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": approve_label},
                    "style": "primary",
                    "action_id": approve_action,
                    "value": value,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": deny_label},
                    "style": "danger",
                    "action_id": deny_action,
                    "value": value,
                },
            ],
        },
    ]
    try:
        dm = await slack.open_dm(owner_id)
        await slack.post_blocks(dm, blocks, fallback)
    except Exception:
        logger.exception("Failed to send prompt: %s", fallback)


async def prompt_allowlist(
    slack: SlackClientOps,
    owner_id: str,
    user_id: str,
    channel_id: str = "",
) -> None:
    """Send an Allow / Deny prompt to the owner for *user_id*.

    When the user is already allowed (manual ``/kirocrew @user`` recall),
    the prompt offers Keep / Remove instead of Allow / Deny.  Automatic
    channel-join prompts are silently skipped for already-allowed users.
    """
    if not user_id:
        return

    already = is_allowed_user(user_id)

    # Automatic channel-join → skip if already allowed
    if channel_id and already:
        return

    logger.info("allowlist prompt: user=%s channel=%s already=%s", user_id, channel_id, already)

    if already:
        text = f"<@{user_id}> is currently on the allowlist.\nKeep or remove?"
        approve_label = "✅ Keep"
        deny_label = "🚫 Remove"
    elif channel_id:
        text = f"👋 <@{user_id}> just joined <#{channel_id}>.\nAdd to allowlist?"
        approve_label = "✅ Allow"
        deny_label = "🚫 Deny"
    else:
        text = f"👋 <@{user_id}> — allowlist requested.\nAdd to allowlist?"
        approve_label = "✅ Allow"
        deny_label = "🚫 Deny"

    await _send_prompt(
        slack,
        owner_id,
        text,
        approve_label,
        deny_label,
        ACTION_ALLOWLIST_APPROVE,
        ACTION_ALLOWLIST_DENY,
        f"{user_id}:",
        "Allowlist prompt",
    )


async def prompt_track_channel(
    slack: SlackClientOps,
    owner_id: str,
    channel_id: str,
    channel_name: str = "",
) -> None:
    """Send a Track / Ignore prompt to the owner for *channel_id*.

    When the channel is already tracked the prompt offers to keep or
    remove it instead of add/ignore.
    """
    if not channel_id:
        return

    already = is_tracked_channel(channel_id)
    logger.info(
        "track channel prompt: channel=%s (%s) already=%s",
        channel_id,
        channel_name,
        already,
    )

    if already:
        text = f"📡 <#{channel_id}> is currently tracked.\nKeep tracking or remove?"
        approve_label = "✅ Keep"
        deny_label = "🚫 Remove"
    else:
        text = f"📡 Track <#{channel_id}> for new member allowlist prompts?"
        approve_label = "✅ Track"
        deny_label = "🚫 Ignore"

    await _send_prompt(
        slack,
        owner_id,
        text,
        approve_label,
        deny_label,
        ACTION_TRACK_APPROVE,
        ACTION_TRACK_DENY,
        f"{channel_id}:{channel_name}",
        "Track channel — prompt",
    )


# ---------------------------------------------------------------------------
# Tunnel URL resolution
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Dashboard presigned link — always sent via DM, never in a channel
# ---------------------------------------------------------------------------


async def send_dashboard_link(
    slack: SlackClientOps,
    user_id: str,
    ttl: int = 3600,
) -> str:
    """Generate a presigned dashboard URL and DM it to *user_id*.

    Returns the generated URL (for logging), or an empty string on failure.
    The link is always sent as a DM to prevent token leakage in channels.

    The URL must be clicked within 5 minutes. Once opened, the session
    cookie lasts for *ttl* seconds (capped at ``MAX_SESSION_TTL_SECS``).
    """
    session_ttl = min(ttl, MAX_SESSION_TTL_SECS)
    cfg = KiroCrewConfig.load()
    configured_host, port = parse_dashboard_url(cfg.dashboard.url)
    local_only = is_local_only(configured_host, True)
    host = resolve_dashboard_host(local_only, configured_host)

    token = generate_token(user_id, session_ttl)

    # Tunnel URL is only used when explicitly opted in via slack.use_tunnel_url
    # (default false until tunnel mechanism is scaled for general use).
    tunnel_url = get_tunnel_url() if cfg.slack.use_tunnel_url else ""
    # Zero-touch provisioning seam: when opted into the tunnel URL, the box is
    # localhost-only, and no tunnel is live yet, give a composed edition a chance
    # to provision one on demand (install + enable + start), then re-read the URL.
    # The public Default's ensure_available() returns "disabled" (no-op), so the
    # standalone path is byte-identical — this block only does work for an edition
    # that implements the seam. PlatformCompositionError is re-raised (fail-closed).
    if cfg.slack.use_tunnel_url and local_only and not tunnel_url:
        from kiro_crew.platform import current_context
        from kiro_crew.platform.context import async_safe_context_call

        state = await async_safe_context_call(
            lambda: current_context().tunnel.ensure_available(),
            fallback="disabled",
            log_message="tunnel.ensure_available failed; falling back to local link",
        )
        # Only adopt a tunnel URL once the tunnel is actually CONNECTED — a
        # "starting" tunnel has no public URL live yet, so re-reading it would
        # yield "" and we would (correctly) fall through to the local link
        # anyway; gating on "connected" makes that explicit and never risks
        # sending a half-provisioned/localhost link as if it were the tunnel.
        # The edition can re-issue the link on a later message once connected.
        if state == "connected":
            tunnel_url = get_tunnel_url() or tunnel_url
    if tunnel_url:
        url = f"{tunnel_url}/?token={token}"
    else:
        origin = dashboard_origin(cfg.dashboard.url)
        url = f"{origin}/?token={token}" if origin else f"http://{host}:{port}/?token={token}"

    # DevSpaces/AgentSpaces: also provide proxy URL
    proxy_line = ""
    proxy = devspaces_proxy_url(port)
    if proxy:
        proxy_line = f"\n🔗 <{proxy}/?token={token}|Open via DevSpaces Proxy>"

    link_mins = LINK_WINDOW_SECS // 60
    session_mins = session_ttl // 60
    try:
        dm = await slack.open_dm(user_id)
        await slack.post_message(
            dm,
            f"🔗 <{url}|Open Dashboard>{proxy_line}\n"
            f"⏱ Click within {link_mins}m · session lasts {session_mins}m",
        )
        sel().log_api_access(
            caller=user_id,
            operation="slack.dashboard_token",
            outcome="ok",
            resources=f"ttl={session_ttl}",
        )
    except Exception:
        try:
            sel().log_api_access(
                caller=user_id,
                operation="slack.dashboard_token",
                outcome="error",
                resources=f"ttl={session_ttl}",
            )
        except Exception:
            pass
        logger.exception("Failed to DM dashboard link to %s", user_id)
        return ""

    return url


# NOTE: `send_channel_challenge()` was DELIBERATELY REMOVED. It generated a
# presigned dashboard-session link for EVERY inbound Slack message (the
# "challenge-and-redirect" posture) — an enterprise-only control not
# wanted for external/open-source usage, where Slack messages reach the agent
# inline. `send_dashboard_link()` above (the explicit `/kirocrew dashboard`
# command) is retained. Do NOT restore send_channel_challenge during an
# upstream sync.


# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------


def _update_config_list(
    section_key: str,
    id_field: str,
    target_id: str,
    *,
    remove: bool = False,
    name: str = "",
) -> None:
    """Add or remove an entry in a ``config.json → slack.<section_key>`` list.

    Each entry is a dict with at least *id_field*.  Idempotent — adding
    a duplicate or removing a missing entry is a no-op (no write at all).
    Goes through :func:`update_config_locked` so the read-modify-write holds
    the sidecar advisory lock: a concurrent config writer (dashboard PATCH,
    CLI, the boot-time meta refresh) cannot land inside this write's window
    and be silently reverted, and manual edits are still respected because
    the read happens fresh inside the lock hold.
    """
    changed = ""

    def _apply(data: dict) -> dict | None:
        nonlocal changed
        slack_cfg = data.setdefault("slack", {})
        entries: list[dict] = slack_cfg.setdefault(section_key, [])
        if remove:
            filtered = [e for e in entries if e.get(id_field) != target_id]
            if len(filtered) == len(entries):
                return None  # wasn't there — no-op, no write
            slack_cfg[section_key] = filtered
            changed = "Removed"
            return data
        if any(e.get(id_field) == target_id for e in entries):
            return None  # already present — no-op, no write
        entry: dict[str, str] = {id_field: target_id}
        if name:
            entry["name"] = name
        entries.append(entry)
        changed = "Added"
        return data

    update_config_locked(config_path(), mutate=_apply)
    if changed == "Removed":
        logger.info("Removed %s=%s from config slack.%s", id_field, target_id, section_key)
    elif changed == "Added":
        logger.info("Added %s=%s (%s) to config slack.%s", id_field, target_id, name, section_key)


def persist_allowed_user(user_id: str, name: str = "", *, remove: bool = False) -> None:
    """Add or remove *user_id* in ``config.json → slack.allowed_users``."""
    _update_config_list("allowed_users", "slack_id", user_id, remove=remove, name=name)


def persist_tracking_channel(channel_id: str, name: str = "", *, remove: bool = False) -> None:
    """Add or remove *channel_id* in ``config.json → slack.tracking_channels``."""
    _update_config_list("tracking_channels", "channel_id", channel_id, remove=remove, name=name)
