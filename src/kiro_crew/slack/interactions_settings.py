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
from typing import TYPE_CHECKING

import aiohttp

from kiro_crew.config.loader import ConfigReadError, config_path, update_config_locked
from kiro_crew.dashboard.chat_utils import run_config_write
from kiro_crew.http_proxy import resolve_proxy
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

if TYPE_CHECKING:
    from kiro_crew.slack.gateway import GatewayOrchestrator

logger = logging.getLogger("kiro_crew.slack.interactions")


async def _save_then_apply(
    orch: GatewayOrchestrator,
    target_id: str,
    *,
    channel: bool,
    remove: bool = False,
    name: str = "",
) -> str | None:
    """Save one allowlist or tracked-channel change, then apply it to the running set.

    Returns ``None`` once ``config.json`` holds the change, else why it was refused.
    An add reaches the running set only after its save. A remove applies at once and
    again after its save, and until that save returns ``is_tracked_channel`` refuses
    the id whatever puts it back in the set (a config reload, the modal, a select).
    ``run_config_write`` runs saves one at a time in click order, so an add whose
    save returns while a remove of the same id is in flight was clicked before it:
    it leaves the id out, as that remove will.
    """
    from kiro_crew.slack.handler import _removes_in_flight

    save = persist_tracking_channel if channel else persist_allowed_user
    publish = set_tracking_channels if channel else set_allowed_users

    def live() -> set[str]:
        # Read on every use: the select handlers replace the set object.
        return orch._tracking_channels if channel else orch._allowed_users

    async def attempt() -> str | None:
        try:
            await run_config_write(save, target_id, name=name, remove=remove)
        except (ValueError, ConfigReadError, OSError) as e:
            logger.warning("Slack access change for %s not saved: %s", target_id, e)
            return str(e) or type(e).__name__
        return None

    if not remove:
        refusal = await attempt()
        if refusal is None and target_id not in _removes_in_flight:
            live().add(target_id)
            publish(live())
        return refusal
    _removes_in_flight[target_id] = _removes_in_flight.get(target_id, 0) + 1
    try:
        live().discard(target_id)
        publish(live())
        return await attempt()
    finally:
        live().discard(target_id)
        publish(live())
        left = _removes_in_flight.pop(target_id) - 1
        if left:
            _removes_in_flight[target_id] = left


# ---------------------------------------------------------------------------
# Channel modal helpers
# ---------------------------------------------------------------------------


async def _refresh_channels_modal(view_id: str, notice: str = "") -> None:
    """Rebuild and push the channels modal with current state, *notice* on top."""
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
    if notice:
        modal["blocks"].insert(
            0, {"type": "context", "elements": [{"type": "mrkdwn", "text": notice}]}
        )
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

    try:
        await run_config_write(_persist_channel_config, cid, activation=new_mode)
    except ValueError as e:
        logger.warning("Channel %s activation not saved: %s", cid, e)
        return
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

    try:
        await run_config_write(_persist_channel_config, cid, agent=new_agent)
    except ValueError as e:
        logger.warning("Channel %s agent not saved: %s", cid, e)
        return
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

    notice = ""
    refusal = await _save_then_apply(_orch, cid, channel=True, remove=True)
    if refusal is None:
        logger.info("Channel %s removed from tracking", cid)
        sel().log_api_access(
            caller=caller,
            operation="slack.channel_remove",
            outcome="allowed",
            source="slack",
            resources=cid,
        )
    else:
        notice = f"⚠️ <#{cid}> removed for now; not saved: {refusal}"

    view_id = payload.get("view", {}).get("id", "")
    if view_id:
        await _refresh_channels_modal(view_id, notice)


async def _handle_ch_add(payload: dict, action: dict) -> None:
    """Add a channel to tracking via the modal picker."""
    cid = action.get("selected_conversation") or action.get("selected_channel", "")
    if not cid or not _orch:
        return
    caller = payload.get("user", {}).get("id", "")
    if not is_owner(caller):
        return

    notice = ""
    refusal = await _save_then_apply(_orch, cid, channel=True)
    if refusal is None:
        _probe_tracked_channel_scope({cid})
        logger.info("Channel %s added to tracking", cid)
        sel().log_api_access(
            caller=caller,
            operation="slack.channel_add",
            outcome="allowed",
            source="slack",
            resources=cid,
        )
    else:
        notice = f"❌ <#{cid}> not added: {refusal}"

    view_id = payload.get("view", {}).get("id", "")
    if view_id:
        await _refresh_channels_modal(view_id, notice)


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
    who = display_name or new_user_id
    if action_id == ACTION_ALLOWLIST_APPROVE:
        if not _orch:
            logger.error("Allowlist approve: orchestrator not initialized")
            return
        refusal = await _save_then_apply(_orch, new_user_id, channel=False, name=display_name)
        if refusal is not None and new_user_id in _orch._allowed_users:
            label = f"⚠️ `{who}` stays on the allowlist; not saved: {refusal}"
        elif refusal is not None:
            label = f"❌ `{who}` not added to allowlist: {refusal}"
        else:
            sel().log_api_access(
                caller=approver_id,
                operation="slack.allowlist.approve",
                outcome="allowed",
                source="slack",
                resources=new_user_id,
            )
            label = f"✅ `{who}` added to allowlist"
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
        refusal = await _save_then_apply(_orch, new_user_id, channel=False, remove=True)
        if refusal is not None:
            label = f"⚠️ `{who}` is off the allowlist for now; not saved: {refusal}"
        else:
            sel().log_api_access(
                caller=approver_id,
                operation="slack.allowlist.deny",
                outcome="denied",
                source="slack",
                resources=new_user_id,
            )
            label = f"🚫 `{who}` removed from allowlist"
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

    from kiro_crew.slack.handler import is_tracked_channel

    label = ""
    shown = channel_name or target_channel_id
    if action_id == ACTION_TRACK_APPROVE:
        if not _orch:
            logger.error("Track channel approve: orchestrator not initialized")
            return
        refusal = await _save_then_apply(_orch, target_channel_id, channel=True, name=channel_name)
        if refusal is not None and is_tracked_channel(target_channel_id):
            label = f"⚠️ Still tracking `#{shown}`; not saved: {refusal}"
        elif refusal is not None:
            label = f"❌ Not tracking `#{shown}`: {refusal}"
        else:
            _probe_tracked_channel_scope({target_channel_id})
            sel().log_api_access(
                caller=approver_id,
                operation="slack.track_channel.approve",
                outcome="allowed",
                source="slack",
                resources=target_channel_id,
            )
            label = f"✅ Now tracking `#{shown}`"

    elif action_id == ACTION_TRACK_DENY:
        if not _orch:
            logger.error("Track channel deny: orchestrator not initialized")
            return
        refusal = await _save_then_apply(_orch, target_channel_id, channel=True, remove=True)
        if refusal is not None:
            label = f"⚠️ Not tracking `#{shown}` for now; not saved: {refusal}"
        else:
            sel().log_api_access(
                caller=approver_id,
                operation="slack.track_channel.deny",
                outcome="denied",
                source="slack",
                resources=target_channel_id,
            )
            label = f"🚫 Removed `#{shown}` from tracking"

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
                resp = await sess.post(
                    response_url,
                    proxy=resolve_proxy(response_url),
                    json={"replace_original": True, "text": label, "blocks": blks},
                )
                # An expired or rate-limited url answers 4xx/5xx without raising.
                if 200 <= resp.status < 300:
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

    skipped_not_object = False

    def _apply(data: dict) -> dict | None:
        nonlocal skipped_not_object
        section = data.setdefault("slack", {})
        if not isinstance(section, dict):
            # Keep the person's entry as written and write nothing: `None`
            # tells update_config_locked there is no change to persist.
            skipped_not_object = True
            return None
        section["allowed_users"] = [{"slack_id": uid} for uid in sorted(new_users)]
        return data

    try:
        await run_config_write(update_config_locked, cp, mutate=_apply)
    except ConfigReadError:
        logger.exception("Refusing to persist users from select: config unreadable")
        return
    except OSError:
        logger.exception("Failed to persist users from select")
        return
    if skipped_not_object:
        logger.warning("Refusing to persist users from select: config 'slack' is not an object")
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

    skipped_not_object = False

    def _apply(data: dict) -> dict | None:
        nonlocal skipped_not_object
        section = data.setdefault("slack", {})
        if not isinstance(section, dict):
            # Same as the users-select handler: keep the entry, write nothing.
            skipped_not_object = True
            return None
        section["tracking_channels"] = [{"channel_id": cid} for cid in sorted(new_channels)]
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
    if skipped_not_object:
        logger.warning("Refusing to persist channels from select: config 'slack' is not an object")
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

    refusal = await _save_then_apply(_orch, target_id, channel=False, remove=True)
    note = f"🚫 Removed <@{target_id}>"
    if refusal is not None:
        note = f"⚠️ Removed <@{target_id}> for now; not saved: {refusal}"

    from kiro_crew.slack.blocks import allowlist_list_block

    blks = allowlist_list_block(sorted(_orch._allowed_users))
    blks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": note}]})

    response_url = payload.get("response_url", "")
    if response_url:
        try:
            async with aiohttp.ClientSession() as sess:
                resp = await sess.post(
                    response_url,
                    proxy=resolve_proxy(response_url),
                    json={"replace_original": True, "text": "Allowlist updated", "blocks": blks},
                )
                # An expired or rate-limited url answers 4xx/5xx without raising.
                if 200 <= resp.status < 300:
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

    refusal = await _save_then_apply(_orch, target_id, channel=True, remove=True)
    note = f"🚫 Removed <#{target_id}>"
    if refusal is not None:
        note = f"⚠️ Removed <#{target_id}> for now; not saved: {refusal}"

    from kiro_crew.slack.blocks import channel_list_block

    blks = channel_list_block(sorted(_orch._tracking_channels))
    blks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": note}]})

    response_url = payload.get("response_url", "")
    if response_url:
        try:
            async with aiohttp.ClientSession() as sess:
                resp = await sess.post(
                    response_url,
                    proxy=resolve_proxy(response_url),
                    json={
                        "replace_original": True,
                        "text": "Tracked channels updated",
                        "blocks": blks,
                    },
                )
                # An expired or rate-limited url answers 4xx/5xx without raising.
                if 200 <= resp.status < 300:
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
