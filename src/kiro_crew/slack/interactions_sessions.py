"""Session controls pressed in Slack: stop, resume, end and new.

The Stop confirmation's confirm and Kill Now buttons and the inline Stop button stop a
turn, and its cancel button dismisses the confirmation; the resume button offers the
Thread or DM choice for a session (``_handle_session_resume``; the facade's
``_handle_resume_choice`` handles the choice); the end button ends a session and records
it as dismissed; the new-session button starts a fresh one in a new thread.

Split out of ``kiro_crew.slack.interactions``, which stays the import path and the
patch target: every name here is reached as ``kiro_crew.slack.interactions.<name>``,
and a patch through that facade reaches this module (``kiro_crew._facade_mirror``).
Nothing here imports the facade. New session-control work goes here.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time

import aiohttp

from kiro_crew.messaging.queue_drain import entries_queued_by
from kiro_crew.security import redact_and_truncate, redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.session_lifecycle import (
    STOP_DECLINED_COMPACTING_TEXT,
    compaction_in_flight,
    consume_stop_declined,
    decline_stop,
)
from kiro_crew.slack.handler import is_allowed_user, is_owner
from kiro_crew.slack.interactions_context import _orch, ack_button

logger = logging.getLogger("kiro_crew.slack.interactions")


async def _handle_stop_confirm(payload: dict, channel: str, msg_ts: str, user_id: str) -> None:
    """Stop the current session when user confirms.

    Defense-in-depth: re-checks the allowlist even though dispatch()
    also enforces it, matching the deny-by-default pattern used by
    other privileged handlers. stop_turn() can escalate to a hard kill,
    so handler-level authorization is required.
    """
    if not _orch or not _orch.sessions:
        await ack_button(payload, channel, msg_ts)
        return
    if not is_allowed_user(user_id):
        logger.warning("stop_confirm denied for unauthorized user %s", user_id or "unknown")
        sel().log_api_access(
            caller=user_id or "unknown",
            operation="slack.stop_confirm",
            outcome="denied",
            source="slack",
            resources=channel,
            error="unauthorized user",
        )
        await ack_button(payload, channel, msg_ts)
        return

    # Find the active session in this channel/thread
    thread_ts = payload.get("message", {}).get("thread_ts") or msg_ts
    has_session = _orch.sessions.has_session(thread_ts)
    # READ, not popped: removed below only once the cancel went through.
    active_task = _orch._session_tasks.get(thread_ts)

    if has_session or active_task:
        response_url = payload.get("response_url", "")

        async def _update_ephemeral(blocks: list[dict], text: str) -> bool:
            """Replace the ephemeral card; did the replacement land?

            Reported rather than swallowed because one caller GATES a destructive
            escalation on this text having reached the presser: there is no
            response url on every route, and the post's own error is caught here,
            so "nothing raised" is not the same as "the presser was told".

            The status is read for the same reason. A response url expires, and
            an expired one answers 4xx with a body rather than raising, so the
            send returns normally having delivered nothing.
            """
            if not response_url:
                return False
            try:
                async with aiohttp.ClientSession() as sess:
                    resp = await sess.post(
                        response_url,
                        json={"replace_original": True, "text": text, "blocks": blocks},
                    )
                    return 200 <= int(resp.status) < 300
            except Exception:
                return False

        async def _on_soft() -> None:
            from kiro_crew.slack.blocks import build_stopped_blocks

            await _update_ephemeral(build_stopped_blocks(), "⏹ [Stopped]")
            if _orch and _orch.slack:
                await _orch.slack.post_message(channel, "⏹ Execution stopped.", thread_ts)

        async def _on_hard() -> None:
            from kiro_crew.slack.blocks import build_stop_failed_blocks

            await _update_ephemeral(build_stop_failed_blocks(), "⛔ [Stop Failed, Session Reset]")
            if _orch and _orch.slack:
                await _orch.slack.post_message(
                    channel, "⛔ Execution stopped — session reset.", thread_ts
                )

        # A repeat press within the window by the same presser, while the
        # compaction still holds the session, is the forcing second press the
        # decline reply promised; the marker is spent either way.
        _force = consume_stop_declined(thread_ts, user_id) and compaction_in_flight(
            _orch.sessions, thread_ts
        )
        # ``preserve_queue`` with the force: the hard reset pops the session and
        # its queue, which in a shared thread holds co-tenants' messages;
        # ``stop_turn`` parks them for the successor instead.
        _kw = {"force": True, "preserve_queue": True} if _force else {}
        # Imported here: ``slack.events`` imports this module.
        from kiro_crew.slack.events import _entry_owner, slack_stop_in_flight

        # The PRESSER's queued messages, not the thread's: every member of a
        # thread queues under its one session key, the way ``!stop`` scopes it.
        # The hold keeps the others' kept entries from starting until the stop,
        # and the cancel of the task it ended, have settled.
        async with slack_stop_in_flight(_orch, thread_ts):
            outcome = await _orch.sessions.stop_turn(
                thread_ts,
                owned_by=entries_queued_by(_entry_owner(user_id, channel)),
                on_soft=_on_soft,
                on_hard=_on_hard,
                **_kw,
            )
            if outcome != "compacting":
                # Pop only the task this Stop ended. A successor dispatched from
                # a message admitted mid-await is the user's newer intent: left
                # running and tracked.
                if _orch._session_tasks.get(thread_ts) is active_task:
                    _orch._session_tasks.pop(thread_ts, None)
                if active_task and not active_task.done():
                    active_task.cancel()
        if outcome == "compacting":
            # Nothing was stopped: the task stays tracked and nothing is cancelled.
            # The reply promises that a repeat forces, so arm the marker for this
            # presser -- after the reply landed, since an escalation they were
            # never warned about is a silent reset. Kill Now stays the other route.
            async def _say_declined() -> bool:
                if await _update_ephemeral([], STOP_DECLINED_COMPACTING_TEXT):
                    return True
                # The ephemeral card needs a response url this route does not
                # always carry. The presser still has to be told, because the
                # marker below is what turns their next press into a reset, so
                # fall back to a thread message and report what landed.
                if not (_orch and _orch.slack and channel):
                    return False
                try:
                    return bool(
                        await _orch.slack.post_message(
                            channel, STOP_DECLINED_COMPACTING_TEXT, thread_ts
                        )
                    )
                except Exception:
                    return False

            await decline_stop(thread_ts, user_id, _say_declined)
        # If stop_turn returned "idle" (no active turn), neither callback
        # fired — dismiss the stale ephemeral with a "Nothing running" message.
        if outcome == "idle":
            await _update_ephemeral([], "Nothing running.")
        sel().log_tool_invocation(
            session_key=thread_ts,
            source="slack",
            tool_name="/kirocrew stop",
            tool_kind="command",
            outcome=outcome,
            metadata={"user": user_id, "channel": channel},
        )
    else:
        # Replace buttons with confirmation
        response_url = payload.get("response_url", "")
        label = "Nothing running."
        if response_url:
            try:
                async with aiohttp.ClientSession() as sess:
                    await sess.post(
                        response_url,
                        json={"replace_original": True, "text": label},
                    )
            except Exception:
                pass
        elif _orch.slack:
            try:
                await _orch.slack.update_message(channel, msg_ts, text=label)
            except Exception:
                pass


async def _handle_stop_cancel(payload: dict, channel: str, msg_ts: str) -> None:
    """Delete the ephemeral stop confirmation message on cancel."""
    response_url = payload.get("response_url", "")
    if response_url:
        try:
            async with aiohttp.ClientSession() as sess:
                await sess.post(
                    response_url,
                    json={
                        "delete_original": True,
                    },
                )
        except Exception:
            pass
    elif _orch and _orch.slack:
        try:
            await _orch.slack.delete_message(channel, msg_ts)
        except Exception:
            pass


async def _handle_stop_kill_now(
    payload: dict, action: dict, channel: str, msg_ts: str, user_id: str
) -> None:
    """Force-kill via the ephemeral Kill Now button.

    Defense-in-depth: re-checks the allowlist even though dispatch()
    also enforces it, matching the deny-by-default pattern used by
    other privileged handlers (e.g. ``_handle_allowlist_remove``).
    """
    if not _orch or not _orch.sessions:
        return
    if not is_allowed_user(user_id):
        logger.warning("stop_kill_now denied for unauthorized user %s", user_id or "unknown")
        sel().log_api_access(
            caller=user_id or "unknown",
            operation="slack.stop_kill_now",
            outcome="denied",
            source="slack",
            resources=action.get("value", ""),
            error="unauthorized user",
        )
        return
    session_key = action.get("value", "")
    if not session_key:
        return

    response_url = payload.get("response_url", "")

    async def _on_hard() -> None:
        from kiro_crew.slack.blocks import build_stop_failed_blocks

        if response_url:
            try:
                async with aiohttp.ClientSession() as sess:
                    await sess.post(
                        response_url,
                        json={
                            "replace_original": True,
                            "text": "⛔ [Stop Failed, Session Reset]",
                            "blocks": build_stop_failed_blocks(),
                        },
                    )
            except Exception:
                pass
        if _orch and _orch.slack:
            # Use the ephemeral's thread_ts (falling back to its own ts)
            # rather than session_key: for linked dashboard sessions these
            # differ, and session_key would not be a valid Slack thread.
            thread_ts = payload.get("message", {}).get("thread_ts") or msg_ts
            await _orch.slack.post_message(
                channel, "⛔ Execution stopped — session reset.", thread_ts
            )

    outcome = await _orch.sessions.stop_turn(session_key, force=True, on_hard=_on_hard)
    sel().log_tool_invocation(
        session_key=session_key,
        source="slack",
        tool_name="stop_kill_now",
        tool_kind="command",
        outcome=outcome,
        metadata={"user": user_id, "channel": channel},
    )


async def _handle_session_resume(
    payload: dict, action: dict, channel: str, msg_ts: str, user_id: str
) -> None:
    """Show choice buttons for how to resume a session."""
    if not is_owner(user_id):
        logger.warning("session_resume rejected: non-owner %s", user_id)
        sel().log_api_access(
            caller=user_id, operation="slack.session_resume", outcome="denied", source="slack"
        )
        return
    if not (_orch and _orch.sessions and _orch.slack):
        return

    try:
        val = json.loads(action.get("value", "{}"))
    except ValueError:
        val = {"key": action.get("value", "")}

    session_key = val.get("key", "")
    title = redact_and_truncate(val.get("title", session_key[:20]), max_chars=200)

    if not session_key:
        return

    # Clicking Resume is the user asking for this conversation back, so retract
    # any End they had recorded on it. Done here, before the destination
    # choice, so the row returns whichever way they continue -- and so it
    # returns at all: an active session outranks the flag only while its
    # process lives, and the flag would hide the row again once it exits.
    await _clear_session_dismissed(session_key)

    # Check if session already has a linked thread/channel
    existing_thread, existing_channel = _orch.sessions.get_slack_link(session_key)

    # Home Tab clicks have empty ``channel`` and ``response_url`` because the
    # interaction is a ``view`` payload, not a message payload. Fall back to
    # the user's DM channel so the choice buttons land somewhere visible.
    if not channel:
        try:
            dm_id = await _orch.slack.open_dm(user_id)
        except Exception:
            logger.exception("session_resume: open_dm failed for user %s", user_id)
            dm_id = ""
        if dm_id:
            channel = dm_id

    if existing_thread and existing_channel:
        link = f"https://slack.com/archives/{existing_channel}/p{existing_thread.replace('.', '')}"
        label = f"\U0001f9f5 This session is already active: <{link}|Go to conversation>"
        response_url = payload.get("response_url", "")
        if response_url:
            try:
                async with aiohttp.ClientSession() as sess:
                    await sess.post(response_url, json={"replace_original": False, "text": label})
            except Exception:
                logger.exception("session_resume: response_url POST failed")
        elif channel:
            try:
                await _orch.slack.post_message(channel, label)
            except Exception:
                logger.exception("session_resume: post_message to %s failed", channel)
        return

    # Show choice buttons
    title, _ = redact_exfiltration_urls(title)
    title, _ = redact_credentials(title)
    choice_value = json.dumps({"key": session_key, "title": title, "src_channel": channel})
    short_id = hashlib.sha256(session_key.encode()).hexdigest()[:12]
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"\U0001f504 Resume *{title}*\nWhere would you like to continue?",
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "\U0001f4ce Thread"},
                    "action_id": f"mc_resume_thread_{short_id}",
                    "value": choice_value,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "\U0001f4ac DM"},
                    "action_id": f"mc_resume_dm_{short_id}",
                    "value": choice_value,
                },
            ],
        },
    ]
    response_url = payload.get("response_url", "")
    if response_url:
        try:
            async with aiohttp.ClientSession() as sess:
                await sess.post(
                    response_url,
                    json={
                        "replace_original": False,
                        "text": f"Resume {title} \u2014 choose Thread or DM",
                        "blocks": blocks,
                    },
                )
        except Exception:
            logger.exception("session_resume: response_url POST failed")
    elif channel:
        try:
            await _orch.slack.post_blocks(
                channel, blocks, f"Resume {title} \u2014 choose Thread or DM"  # type: ignore[arg-type]
            )
        except Exception:
            logger.exception("session_resume: post_blocks to %s failed", channel)
    else:
        logger.warning(
            "session_resume: no response_url and no channel — cannot post choice for user %s",
            user_id,
        )


async def _record_session_dismissed(session_key: str) -> None:
    """Stamp ``closed``/``closed_at`` on *session_key*'s transcript.

    The durable half of the End button. ``closed`` is the record that the user
    put this conversation away, read back by
    ``messaging.sessions_view._row_is_ended`` to keep the row out of the
    sessions list, and it is the same field a dashboard tab close writes --
    one dismissal per transcript, because a tab and its channel conversation
    share the file.

    ``closed_at`` is stamped HERE rather than before the teardown above, so
    that consolidation and skill extraction -- which run on the way out and
    can write the file after this handler returns -- cannot look like the user
    coming back. Nothing reads it for the sessions list today; it is written
    because the dashboard's own reader compares against it, and a flag with no
    instant would make every close there permanent.

    Guarded on the metadata already existing: a missing first line means the
    key names no transcript (a stale button, an unresolvable id), and the
    unguarded writer would CREATE one, inventing a session out of a click.
    Best-effort, but logged at warning -- a dismissal that silently fails to
    land is exactly the "End does nothing" report this fixes.
    """
    if not (session_key and _orch and _orch.conv_log):
        return
    conv_log = _orch.conv_log
    fields = {"closed": True, "closed_at": time.time()}
    try:
        recorded = await asyncio.to_thread(
            conv_log.update_metadata_if, session_key, fields, lambda meta: bool(meta)
        )
    except Exception:
        logger.warning("session end: dismissal not recorded for %s", session_key, exc_info=True)
        return
    if not recorded:
        logger.warning(
            "session end: no transcript metadata to dismiss for %s",
            session_key,
        )


async def _clear_session_dismissed(session_key: str) -> None:
    """Drop a ``closed`` record because the user is resuming the conversation.

    Without this the row would only reappear while a process happens to be
    live for the key (``_row_is_ended`` lets an active session outrank the
    flag) and would vanish again the moment that process exits -- so a
    conversation the user deliberately came back to would keep dropping out of
    their own list. Unconditional: the click is the intent, unlike the
    dashboard's resume route, which clears only a close it can prove predates
    its own boundary because there the flag may belong to a different tab.
    """
    if not (session_key and _orch and _orch.conv_log):
        return
    try:
        await asyncio.to_thread(_orch.conv_log.clear_closed, session_key)
    except Exception:
        logger.warning("session resume: dismissal not cleared for %s", session_key, exc_info=True)


async def _handle_session_end(
    payload: dict, action: dict, channel: str, msg_ts: str, user_id: str
) -> None:
    """End a session by removing it from SessionMap and resetting if active."""
    if not is_owner(user_id):
        logger.warning("session_end rejected: non-owner %s", user_id)
        return
    session_id = action.get("value", "")
    if not (session_id and _orch and _orch.sessions):
        return

    sel().log_api_access(
        caller=user_id,
        operation="slack.session_end",
        outcome="allowed",
        source="slack",
        resources=session_id,
    )

    key_to_remove = _orch.sessions.find_key_by_sid(session_id)
    # Also try treating value as a direct session key (from /kirocrew sessions buttons)
    if not key_to_remove and _orch.sessions.has_session(session_id):
        key_to_remove = session_id
    if key_to_remove:
        # Trigger skill extraction before killing the session (fire-and-forget)
        if _orch.consolidator:
            try:
                # Audit the consolidation trigger. Skill write auditing (log_tool_invocation
                # with tool_name="auto_skill_create") is handled inside _process_auto_skills().
                sel().log_api_access(
                    caller=user_id,
                    operation="consolidate_session_slack_end",
                    outcome="allowed",
                    source="slack",
                    resources=key_to_remove,
                )
                _orch.consolidator.consolidate_session(key_to_remove)
            except Exception:
                logger.debug(
                    "consolidate_session (or SEL) failed for %s", key_to_remove, exc_info=True
                )
        # Soft-remove: kill process but preserve session_map for future resume
        try:
            await _orch.sessions.remove(key_to_remove)
        except Exception:
            logger.debug("session end remove failed for %s", key_to_remove, exc_info=True)

    # Record the dismissal on the transcript, which is the half that makes the
    # button do what it says. The removal above only kills a live process; the
    # transcript stays on disk and the sessions list is built from the
    # directory, so without this record the row is back on the next `sessions`
    # call. It runs for a row with NO live session too -- that is the case the
    # user hits most, because a cluttered list is mostly idle rows, and for
    # those the block above resolves no key and does nothing at all.
    await _record_session_dismissed(key_to_remove or session_id)

    response_url = payload.get("response_url", "")
    label = f"🛑 Session `{session_id[:12]}…` ended."
    if response_url:
        try:
            async with aiohttp.ClientSession() as sess:
                await sess.post(response_url, json={"replace_original": True, "text": label})
                return
        except Exception:
            pass
    if _orch.slack and channel and msg_ts:
        try:
            await _orch.slack.update_message(channel, msg_ts, text=label)
        except Exception:
            pass


async def _handle_inline_stop(
    payload: dict, action: dict, channel: str, msg_ts: str, user_id: str
) -> None:
    """Stop the active turn for a session via the inline stop button."""
    if not is_owner(user_id):
        sel().log_api_access(
            caller=user_id,
            operation="slack.inline_stop",
            outcome="denied",
            source="slack",
            resources=action.get("value", ""),
        )
        return
    session_key = action.get("value", "")
    if not (session_key and _orch and _orch.sessions):
        sel().log_api_access(
            caller=user_id,
            operation="slack.inline_stop",
            outcome="invalid",
            source="slack",
            resources=session_key,
        )
        return

    sel().log_api_access(
        caller=user_id,
        operation="slack.inline_stop",
        outcome="allowed",
        source="slack",
        resources=session_key,
    )

    # Immediate feedback — update the working message to show stopping
    if _orch.slack and channel and msg_ts:
        try:
            await _orch.slack.update_message(channel, msg_ts, text="⏹ _Stopping…_")
        except Exception:
            pass

    async def _on_soft() -> None:
        if _orch.slack and channel and msg_ts:
            try:
                await _orch.slack.update_message(channel, msg_ts, text="⏹ Execution stopped.")
            except Exception:
                pass

    async def _on_hard() -> None:
        if _orch.slack and channel and msg_ts:
            try:
                await _orch.slack.update_message(
                    channel, msg_ts, text="⛔ Execution stopped — session reset."
                )
            except Exception:
                pass

    # A repeat press within the window by the same presser, while the compaction
    # still holds the session, is the forcing second press the decline promised.
    _force = consume_stop_declined(session_key, user_id) and compaction_in_flight(
        _orch.sessions, session_key
    )
    # ``preserve_queue`` with the force, for the reason the confirm button gives.
    _kw = {"force": True, "preserve_queue": True} if _force else {}
    from kiro_crew.slack.events import _entry_owner, slack_stop_in_flight

    # The presser's queued messages only, held until the stop settles, for the
    # reasons the confirm button gives.
    async with slack_stop_in_flight(_orch, session_key):
        outcome = await _orch.sessions.stop_turn(
            session_key,
            owned_by=entries_queued_by(_entry_owner(user_id, channel)),
            on_soft=_on_soft,
            on_hard=_on_hard,
            **_kw,
        )
    if outcome == "compacting":
        # The reply promises that a repeat forces, so arm the marker -- and only
        # once the reply landed. This update is conditional on a client and a
        # message id and swallows its own transport error, so arming first would
        # leave the escalation live with the presser never warned, and their
        # retry would hard-reset the session silently.
        async def _say_declined() -> bool:
            if not (_orch.slack and channel and msg_ts):
                return False
            try:
                await _orch.slack.update_message(
                    channel, msg_ts, text=STOP_DECLINED_COMPACTING_TEXT
                )
            except Exception:
                return False
            return True

        await decline_stop(session_key, user_id, _say_declined)
    elif outcome == "idle" and _orch.slack and channel and msg_ts:
        try:
            await _orch.slack.update_message(channel, msg_ts, text="⏹ Nothing running.")
        except Exception:
            pass
    sel().log_tool_invocation(
        session_key=session_key,
        source="slack",
        tool_name="inline_stop",
        tool_kind="command",
        outcome=outcome,
        metadata={"user": user_id, "channel": channel},
    )


async def _handle_session_new(
    payload: dict, action: dict, channel: str, msg_ts: str, user_id: str
) -> None:
    """Create a fresh session by posting a prompt in a new thread."""
    if not is_owner(user_id):
        logger.warning("session_new rejected: non-owner %s", user_id)
        return
    if not (_orch and _orch.slack):
        return
    sel().log_api_access(
        caller=user_id,
        operation="slack.session_new",
        outcome="allowed",
        source="slack",
        resources=channel,
    )

    # Post a new message that starts a fresh thread
    try:
        await _orch.slack.post_message(
            channel, "✨ New session started. Send your first message here."
        )
    except Exception:
        logger.debug("Failed to create new session message", exc_info=True)
        return

    # Ack the button
    response_url = payload.get("response_url", "")
    label = "✨ New session created."
    if response_url:
        try:
            async with aiohttp.ClientSession() as sess:
                await sess.post(response_url, json={"replace_original": False, "text": label})
        except Exception:
            pass
