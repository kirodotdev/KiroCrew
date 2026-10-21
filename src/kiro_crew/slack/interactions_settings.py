"""Owner settings changed from Slack: channels, users, the default agent and approvals.

The channels modal's activation, agent, add and remove controls (``_handle_ch_*``; add
and remove rebuild it through ``_refresh_channels_modal``); the users and channels
pickers and the allowlist and channel remove buttons; the default-agent selector; and the
allowlist and track-channel approve and deny buttons.

Split out of ``kiro_crew.slack.interactions``, which stays the import path and the
patch target: every name here is reached as ``kiro_crew.slack.interactions.<name>``,
and a patch through that facade reaches this module (``kiro_crew._facade_mirror``).
Nothing here imports the facade. New settings-control work goes here.
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp

from kiro_crew.config.loader import ConfigReadError, config_path, update_config_locked
from kiro_crew.dashboard.chat_utils import run_config_write
from kiro_crew.sel import sel
from kiro_crew.slack.allowlist import (
    ACTION_ALLOWLIST_APPROVE,
    ACTION_ALLOWLIST_DENY,
    ACTION_TRACK_APPROVE,
    ACTION_TRACK_DENY,
    persist_allowed_user,
    persist_tracking_channel,
)
from kiro_crew.slack.handler import is_owner, set_allowed_users, set_tracking_channels
from kiro_crew.slack.interactions_context import _orch, _probe_tracked_channel_scope

logger = logging.getLogger("kiro_crew.slack.interactions")


# ---------------------------------------------------------------------------
# Channel modal helpers
# ---------------------------------------------------------------------------


async def _refresh_channels_modal(view_id: str) -> None:
    """Rebuild and push the channels modal with current state."""
    if not _orch or not _orch.slack:
        return
    from kiro_crew.slack.blocks import channels_modal
    from kiro_crew.slack.handler import slack_cfg

    current_ids = sorted(_orch._tracking_channels)
    channels = [
        {
            "channel_id": cid,
            "activation": slack_cfg(_orch).channel_config(cid).activation,
            "agent": slack_cfg(_orch).channel_config(cid).agent,
        }
        for cid in current_ids
    ]
    from kiro_crew.slack.events import _get_agent_names

    agent_names = await asyncio.to_thread(_get_agent_names)
    modal = channels_modal(channels, agent_names=agent_names)
    try:
        await _orch.slack.views_update(view_id=view_id, view=modal)
    except Exception:
        logger.exception("Failed to refresh channels modal")


async def _handle_ch_activation(payload: dict, action: dict) -> None:
    """Change activation mode for a channel from the modal dropdown."""
    caller = payload.get("user", {}).get("id", "")
    if not is_owner(caller):
        return
    action_id = action.get("action_id", "")
    cid = action_id.removeprefix("mc_ch_activation_")
    new_mode = (action.get("selected_option") or {}).get("value", "mention")

    from kiro_crew.slack.handler import _persist_channel_config

    await run_config_write(_persist_channel_config, cid, activation=new_mode)
    if _orch:
        # In place, never a rebind: ``_orch._cfg`` is the object the handler
        # module and every dispatcher hold, so rebinding it here would leave
        # them on the stale one.
        from kiro_crew.slack.handler import _reload_orch_cfg

        _reload_orch_cfg()
    sel().log_api_access(
        caller=caller,
        operation="slack.channel_activation_change",
        outcome="allowed",
        source="slack",
        resources=f"{cid}={new_mode}",
    )
    logger.info("Channel %s activation changed to %s", cid, new_mode)


async def _handle_ch_agent(payload: dict, action: dict) -> None:
    """Change agent override for a channel from the modal dropdown."""
    caller = payload.get("user", {}).get("id", "")
    if not is_owner(caller):
        return
    action_id = action.get("action_id", "")
    cid = action_id.removeprefix("mc_ch_agent_")
    new_agent = (action.get("selected_option") or {}).get("value", "")
    if new_agent == "__default__":
        new_agent = ""

    from kiro_crew.slack.handler import _persist_channel_config

    await run_config_write(_persist_channel_config, cid, agent=new_agent)
    if _orch:
        # In place, never a rebind: ``_orch._cfg`` is the object the handler
        # module and every dispatcher hold, so rebinding it here would leave
        # them on the stale one.
        from kiro_crew.slack.handler import _reload_orch_cfg

        _reload_orch_cfg()
    logger.info("Channel %s agent changed to %s", cid, new_agent or "default")
    sel().log_api_access(
        caller=caller,
        operation="slack.channel_agent_change",
        outcome="allowed",
        source="slack",
        resources=f"{cid}={new_agent or 'default'}",
    )


async def _handle_ch_remove(payload: dict, action: dict) -> None:
    """Remove a channel from tracking via the modal button."""
    cid = action.get("value", "")
    if not cid or not _orch:
        return
    caller = payload.get("user", {}).get("id", "")
    if not is_owner(caller):
        return

    from kiro_crew.slack.allowlist import persist_tracking_channel

    _orch._tracking_channels.discard(cid)
    set_tracking_channels(_orch._tracking_channels)
    await run_config_write(persist_tracking_channel, cid, remove=True)
    logger.info("Channel %s removed from tracking", cid)
    sel().log_api_access(
        caller=caller,
        operation="slack.channel_remove",
        outcome="allowed",
        source="slack",
        resources=cid,
    )

    view_id = payload.get("view", {}).get("id", "")
    if view_id:
        await _refresh_channels_modal(view_id)


async def _handle_ch_add(payload: dict, action: dict) -> None:
    """Add a channel to tracking via the modal picker."""
    cid = action.get("selected_conversation") or action.get("selected_channel", "")
    if not cid or not _orch:
        return
    caller = payload.get("user", {}).get("id", "")
    if not is_owner(caller):
        return

    from kiro_crew.slack.allowlist import persist_tracking_channel

    _orch._tracking_channels.add(cid)
    set_tracking_channels(_orch._tracking_channels)
    _probe_tracked_channel_scope({cid})
    await run_config_write(persist_tracking_channel, cid)
    logger.info("Channel %s added to tracking", cid)
    sel().log_api_access(
        caller=caller,
        operation="slack.channel_add",
        outcome="allowed",
        source="slack",
        resources=cid,
    )

    view_id = payload.get("view", {}).get("id", "")
    if view_id:
        await _refresh_channels_modal(view_id)


async def _handle_allowlist(
    payload: dict,
    action: dict,
    action_id: str,
    channel: str,
    msg_ts: str,
    approver_id: str,
) -> None:
    """Process an allowlist approve or deny button click."""
    raw_value = action.get("value", "")
    new_user_id, _, display_name = raw_value.partition(":")
    if not new_user_id:
        logger.warning("Allowlist button missing user_id in value=%r", raw_value)
        return

    label = ""
    if action_id == ACTION_ALLOWLIST_APPROVE:
        if not _orch:
            logger.error("Allowlist approve: orchestrator not initialized")
            return
        _orch._allowed_users.add(new_user_id)
        set_allowed_users(_orch._allowed_users)
        await run_config_write(persist_allowed_user, new_user_id, name=display_name)
        sel().log_api_access(
            caller=approver_id,
            operation="slack.allowlist.approve",
            outcome="allowed",
            source="slack",
            resources=new_user_id,
        )
        label = f"✅ `{display_name or new_user_id}` added to allowlist"
        # Notify the approved user. Multi-user access is disabled: the gateway
        # keeps the allowlist owner-only and prunes non-owner entries at startup,
        # so a non-owner still cannot message the bot. Tell them that plainly
        # rather than claiming "you can now message me", which would be false.
        if _orch.slack:
            try:
                dm = await _orch.slack.open_dm(new_user_id)
                await _orch.slack.post_message(
                    dm,
                    "✅ Your allowlist request was approved by the owner. "
                    "Note: this bot is currently owner-only, so you still "
                    "cannot message it directly yet.",
                )
            except Exception:
                logger.debug("Failed to DM approved user %s", new_user_id, exc_info=True)

    elif action_id == ACTION_ALLOWLIST_DENY:
        if not _orch:
            logger.error("Allowlist deny: orchestrator not initialized")
            return
        # Remove from in-memory set and persisted config
        _orch._allowed_users.discard(new_user_id)
        set_allowed_users(_orch._allowed_users)
        await run_config_write(persist_allowed_user, new_user_id, remove=True)
        sel().log_api_access(
            caller=approver_id,
            operation="slack.allowlist.deny",
            outcome="denied",
            source="slack",
            resources=new_user_id,
        )
        label = f"🚫 `{display_name or new_user_id}` removed from allowlist"
        if _orch.slack and new_user_id:
            try:
                dm = await _orch.slack.open_dm(new_user_id)
                await _orch.slack.post_message(
                    dm, "🚫 Your access request was denied by the owner."
                )
            except Exception:
                logger.debug("Failed to DM denied user %s", new_user_id, exc_info=True)

    # Replace the buttons message with the outcome
    if label and _orch and _orch.slack and channel and msg_ts:
        try:
            await _orch.slack.update_message(channel, msg_ts, text=label)
        except Exception:
            pass


async def _handle_track_channel(
    payload: dict,
    action: dict,
    action_id: str,
    channel: str,
    msg_ts: str,
    approver_id: str,
) -> None:
    """Process a tracking-channel approve or deny button click."""
    raw_value = action.get("value", "")
    target_channel_id, _, channel_name = raw_value.partition(":")
    if not target_channel_id:
        logger.warning("Track channel button missing channel_id in value=%r", raw_value)
        return

    label = ""
    if action_id == ACTION_TRACK_APPROVE:
        if not _orch:
            logger.error("Track channel approve: orchestrator not initialized")
            return
        _orch._tracking_channels.add(target_channel_id)
        set_tracking_channels(_orch._tracking_channels)
        _probe_tracked_channel_scope({target_channel_id})
        await run_config_write(persist_tracking_channel, target_channel_id, name=channel_name)
        sel().log_api_access(
            caller=approver_id,
            operation="slack.track_channel.approve",
            outcome="allowed",
            source="slack",
            resources=target_channel_id,
        )
        label = f"✅ Now tracking `#{channel_name or target_channel_id}`"

    elif action_id == ACTION_TRACK_DENY:
        if not _orch:
            logger.error("Track channel deny: orchestrator not initialized")
            return
        # Remove from in-memory set and persisted config
        _orch._tracking_channels.discard(target_channel_id)
        set_tracking_channels(_orch._tracking_channels)
        await run_config_write(persist_tracking_channel, target_channel_id, remove=True)
        sel().log_api_access(
            caller=approver_id,
            operation="slack.track_channel.deny",
            outcome="denied",
            source="slack",
            resources=target_channel_id,
        )
        label = f"🚫 Removed `#{channel_name or target_channel_id}` from tracking"

    # Replace the buttons message with the outcome
    if label and _orch and _orch.slack and channel and msg_ts:
        try:
            await _orch.slack.update_message(channel, msg_ts, text=label)
        except Exception:
            pass


async def _handle_agent_select(
    payload: dict, action: dict, channel: str, msg_ts: str, user_id: str
) -> None:
    """Handle agent static_select — switch agent and collapse message."""
    from kiro_crew.slack.handler import (
        _resolve_agent_name,
        _set_default_agent,
        is_owner,
    )

    if not is_owner(user_id):
        return

    selected = action.get("selected_option", {})
    agent_name = selected.get("value", "")
    if not agent_name:
        return

    if agent_name.lower() in ("off", "default"):
        try:
            await run_config_write(_set_default_agent, "")
        except ValueError:
            return
        label = "🔄 Reset to default agent."
    else:
        # The resolver lists the agents directory and reads the matching spec:
        # filesystem work, off the loop like the other async callers of it.
        resolved = await asyncio.to_thread(_resolve_agent_name, agent_name)
        if not resolved:
            return
        try:
            await run_config_write(_set_default_agent, resolved)
        except ValueError:
            return
        label = f"🔄 Switched to agent: *{resolved}*"

    blks = [{"type": "section", "text": {"type": "mrkdwn", "text": label}}]

    response_url = payload.get("response_url", "")
    if response_url:
        try:
            async with aiohttp.ClientSession() as sess:
                await sess.post(
                    response_url,
                    json={"replace_original": True, "text": label, "blocks": blks},
                )
                return
        except Exception:
            pass

    if _orch and _orch.slack and channel and msg_ts:
        try:
            await _orch.slack.update_message(channel, msg_ts, text=label, blocks=blks)
        except Exception:
            pass


async def _handle_users_select(
    payload: dict, action: dict, channel: str, msg_ts: str, user_id: str
) -> None:
    """Handle multi_users_select — update allowlist."""
    # Imported at call time on purpose: tests patch ``handler.is_owner`` to drive
    # the non-owner rejection, and only a call-time rebind observes that patch.
    from kiro_crew.slack.handler import is_owner, set_allowed_users

    if not is_owner(user_id):
        return

    new_users = set(action.get("selected_users") or [])

    # Persist through the locked read-modify-write BEFORE mutating runtime
    # state. Fail closed on an unreadable config: writing back a {} baseline
    # would drop every other setting. Order matters — applying the in-memory
    # change first would make a refused save look applied, then silently
    # revert on restart.
    cp = config_path()

    def _apply(data: dict) -> dict:
        data.setdefault("slack", {})["allowed_users"] = [
            {"slack_id": uid} for uid in sorted(new_users)
        ]
        return data

    try:
        await run_config_write(update_config_locked, cp, mutate=_apply)
    except ConfigReadError:
        logger.exception("Refusing to persist users from select: config unreadable")
        return
    except OSError:
        logger.exception("Failed to persist users from select")
        return

    if _orch:
        _orch._allowed_users = new_users
        set_allowed_users(new_users)

    logger.info("Allowlist updated via select: %d users", len(new_users))
    sel().log_api_access(
        caller=user_id,
        operation="slack.allowlist_update",
        outcome="allowed",
        source="slack",
        resources=f"users={len(new_users)}",
    )


async def _handle_channels_select(
    payload: dict, action: dict, channel: str, msg_ts: str, user_id: str
) -> None:
    """Handle multi_channels_select — update tracked channels."""
    # Imported at call time on purpose: tests patch ``handler.is_owner`` to drive
    # the non-owner rejection, and only a call-time rebind observes that patch.
    from kiro_crew.slack.handler import is_owner, set_tracking_channels

    if not is_owner(user_id):
        return

    new_channels = set(action.get("selected_channels") or [])

    # Read BEFORE mutating runtime state (see the users-select handler above for
    # why the order is load-bearing).
    cp = config_path()

    def _apply(data: dict) -> dict:
        data.setdefault("slack", {})["tracking_channels"] = [
            {"channel_id": cid} for cid in sorted(new_channels)
        ]
        return data

    # Persist FIRST (locked read-modify-write), then mutate runtime state
    # (see the users-select handler).
    try:
        await run_config_write(update_config_locked, cp, mutate=_apply)
    except ConfigReadError:
        logger.exception("Refusing to persist channels from select: config unreadable")
        return
    except OSError:
        logger.exception("Failed to persist channels from select")
        return

    if _orch:
        added = new_channels - _orch._tracking_channels
        _orch._tracking_channels = new_channels
        set_tracking_channels(new_channels)
        _probe_tracked_channel_scope(added)

    logger.info("Tracked channels updated via select: %d channels", len(new_channels))
    sel().log_api_access(
        caller=user_id,
        operation="slack.channels_update",
        outcome="allowed",
        source="slack",
        resources=f"channels={len(new_channels)}",
    )


async def _handle_allowlist_remove(
    payload: dict, action: dict, channel: str, msg_ts: str, user_id: str
) -> None:
    """Remove a user from the allowlist via the remove button."""
    if not is_owner(user_id) or not _orch:
        return
    target_id = action.get("value", "")
    if not target_id:
        return

    _orch._allowed_users.discard(target_id)
    set_allowed_users(_orch._allowed_users)
    await run_config_write(persist_allowed_user, target_id, remove=True)

    from kiro_crew.slack.blocks import allowlist_list_block

    blks = allowlist_list_block(sorted(_orch._allowed_users))
    blks.append(
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"🚫 Removed <@{target_id}>"}]}
    )

    response_url = payload.get("response_url", "")
    if response_url:
        try:
            async with aiohttp.ClientSession() as sess:
                await sess.post(
                    response_url,
                    json={"replace_original": True, "text": "Allowlist updated", "blocks": blks},
                )
                return
        except Exception:
            pass
    if _orch.slack and channel and msg_ts:
        try:
            await _orch.slack.update_message(channel, msg_ts, text="Allowlist updated", blocks=blks)
        except Exception:
            pass


async def _handle_channel_remove(
    payload: dict, action: dict, channel: str, msg_ts: str, user_id: str
) -> None:
    """Remove a channel from tracking via the remove button."""
    if not is_owner(user_id) or not _orch:
        return
    target_id = action.get("value", "")
    if not target_id:
        return

    _orch._tracking_channels.discard(target_id)
    set_tracking_channels(_orch._tracking_channels)
    await run_config_write(persist_tracking_channel, target_id, remove=True)

    from kiro_crew.slack.blocks import channel_list_block

    blks = channel_list_block(sorted(_orch._tracking_channels))
    blks.append(
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"🚫 Removed <#{target_id}>"}]}
    )

    response_url = payload.get("response_url", "")
    if response_url:
        try:
            async with aiohttp.ClientSession() as sess:
                await sess.post(
                    response_url,
                    json={
                        "replace_original": True,
                        "text": "Tracked channels updated",
                        "blocks": blks,
                    },
                )
                return
        except Exception:
            pass
    if _orch.slack and channel and msg_ts:
        try:
            await _orch.slack.update_message(
                channel, msg_ts, text="Tracked channels updated", blocks=blks
            )
        except Exception:
            pass
