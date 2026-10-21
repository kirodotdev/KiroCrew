"""The ``!`` text commands and their registered slash forms that reply on their own:
``!link``, ``!unlink``, ``!compact`` and ``!status``, the reply sink a text command
answers through, and the single-reply command router both surfaces share.

Each command's dispatch stays in ``handle_message``, which owns the resume-binding
refusal and the mid-turn ladder; ``!stop``'s handler (``_handle_stop``) and the
soft-threshold notice (``_maybe_notice``) stay in ``transport_dispatch.py``, where the
stop guard and the context-reinjection guard read them. ``_handle_compact``'s direct send
redacts on its own: ``security_posture`` registers this module as the "Discord direct
send" sink.

Each function here whose first parameter is ``self`` is a ``DiscordDispatcher``
method: the class binds it by name, so it reads and writes the dispatcher's own
state on the instance, and a patch of the method on the class or on an instance
reaches every caller.

Instance fields read (a call on one included): ``_session_resume``, ``approval_mode``,
``client``, ``sessions``.

Instance fields assigned, or whose mapping is changed: none.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from kiro_crew.discord.session_resume import ResumeReleaseError
from kiro_crew.messaging.commands import compact_unsupported_backend, compact_unsupported_reply
from kiro_crew.messaging.link import rebind_conversation_location, release_conversation_location
from kiro_crew.safety_override import describe_grant_lifetime, safety_override
from kiro_crew.session_map import ConversationOwnershipConflict
from kiro_crew.stats import Stats

if TYPE_CHECKING:
    from kiro_crew.discord.transport_dispatch import DiscordDispatcher, ReplyFn

#: The dispatcher's one logger, named for the facade module operators filter on.
logger = logging.getLogger("kiro_crew.discord.transport_dispatch")


#: Commands whose whole effect is one reply, so the text and slash surfaces can
#: share a handler and differ only in where that reply goes. Session-scoped
#: commands are deliberately absent: they need the resume-binding refusal and the
#: mid-turn ladder that ``handle_message`` owns.
_REPLY_COMMANDS = frozenset({"status"})

_RELEASE_FAILURE = (
    "⚠️ Couldn't save the session release, so the command was NOT completed. "
    "Fix the gateway's storage problem, then retry."
)


async def _handle_link(
    self: DiscordDispatcher,
    user_id: str,
    channel_id: str,
    thread_id: str,
    resumed_key: str | None,
) -> None:
    """Re-enable mirroring of this conversation's dashboard tab back here.

    The rebind sequence, its batching, its claim-first ordering and its reply
    live in the shared
    :func:`~kiro_crew.messaging.link.rebind_conversation_location`; this
    supplies Discord's spelling of "this conversation" plus the two refusals
    only a resume-capable channel can hit.
    """
    assert self.client is not None
    # Refuse while a resumed session owns this conversation: linking would
    # rebind the same location and silently strand the resumed session. The
    # owner comes from the turn's routing decision, not a fresh resolve — a
    # second resolve is a second answer, and the gap between them is where a
    # concurrent rebind slips through.
    if resumed_key is not None:
        await self.client.send_message(
            channel_id,
            "⚠️ A resumed session is active here. Send `!unlink` first.",
        )
        return
    try:
        reply = rebind_conversation_location(
            self.sessions,
            key=self._session_key(user_id, thread_id),
            location=self._origin_mirror_link(channel_id),
            unlink_command="`!unlink`",
        )
    except ConversationOwnershipConflict:
        # Reachable past the resumed-session check above because that check
        # fails CLOSED on duplicate inbound bindings: with two of them at this
        # conversation `resumed_session` denies routing and returns None,
        # while the claim is still refused. Same instruction either way, and
        # reporting it beats surfacing a traceback as a generic command
        # failure.
        logger.info("discord link refused: conversation already held")
        await self.client.send_message(
            channel_id,
            "⚠️ Another session is already linked here. Send `!unlink` first.",
        )
        return
    await self.client.send_message(channel_id, reply)


async def _handle_unlink(
    self: DiscordDispatcher, user_id: str, channel_id: str, thread_id: str = ""
) -> None:
    assert self.client is not None
    # A resumed session takes precedence: it is what the user is actually
    # talking to, so releasing it is the only way back to their own
    # conversation from Discord.
    try:
        left_resumed = await self._session_resume.leave_resumed_session(channel_id)
    except ResumeReleaseError:
        await self.client.send_message(channel_id, _RELEASE_FAILURE)
        return
    if left_resumed is not None:
        await self.client.send_message(
            channel_id,
            "✅ Left the resumed session. Back to your Discord conversation.",
        )
        return
    key = self._session_key(user_id, thread_id)
    # Persist the refusal BEFORE releasing: mirroring is re-asserted on every
    # inbound turn, so a release alone would be undone by the user's next
    # message. Batched with the release so the pair is one whole-map write.
    with self.sessions.batched_save():
        self.sessions.set_mirror_opt_out(key, True)
        reply, swept = release_conversation_location(
            self.sessions,
            key=key,
            location=self._origin_mirror_link(channel_id),
            channel="discord",
        )
    if swept:
        # A swept binding can belong to a dashboard slot whose link chip is
        # projected at push time — nudge the dashboard like every other
        # binding mutation does.
        self._session_resume._push_slots()
    await self.client.send_message(channel_id, reply)


async def _handle_compact(
    self: DiscordDispatcher,
    user_id: str,
    channel_id: str,
    thread_id: str,
    resumed_key: str | None,
) -> None:
    """In-place ACP ``/compact`` on the conversation's session."""
    assert self.client is not None
    session_key = resumed_key or self._session_key(user_id, thread_id)
    if not await self.sessions.try_acquire(session_key):
        if self.sessions.has_session(session_key):
            await self.client.send_message(
                channel_id,
                "⏳ Still working on your last message — try `!compact` " "once it finishes.",
            )
        else:
            await self.client.send_message(channel_id, "No active session to compact.")
        return
    try:
        provider = self.sessions.get_provider(session_key)
        if provider is None:
            await self.client.send_message(channel_id, "No active session to compact.")
            return

        # Capability gate (mirroring the dashboard's gate): a
        # backend that cannot serve a manual /compact treats the prompt as
        # ordinary text and never answers, so dispatching would strand the
        # 120s wait below. Informational, never an error.
        unsupported = compact_unsupported_backend(provider)
        if unsupported:
            await self.client.send_message(channel_id, compact_unsupported_reply(unsupported))
            return

        status_id = await self.client.send_message(channel_id, "🔄 Compacting context…")
        result_text: str | None = None

        def _safe(text: str) -> str:
            """Redact backend-echoed, LLM-influenced compaction text before
            it reaches the external Discord surface: normal turns get this
            via the shared TurnDriver, but this path sends directly."""
            from kiro_crew.security import redact

            cleaned = redact(text or "")
            return cleaned

        try:

            # Compaction runs over the prompt transport:
            # provider.compact() drives /compact via session/prompt (the
            # commands/execute path does NOT run compaction — it returns
            # with no status). Bound compact()'s prompt
            # turn here, then let wait_for_compaction() own its OWN deadline
            # for a status emitted async after end_turn — it must NOT be
            # nested inside another timeout, or the graceful "timed out"
            # branch is unreachable and a slow-but-healthy session gets
            # destroyed by the outer TimeoutError.
            await asyncio.wait_for(provider.compact(), timeout=120)
            cr = await provider.wait_for_compaction(
                timeout=self.sessions.compact_wait_budget_secs()
            )
            if cr["type"] == "completed":
                # ``summary`` is model-facing compacted context, not a
                # user-facing receipt. Never publish its orchestration text.
                result_text = "✅ Context compacted."
            elif cr["type"] == "failed":
                err = _safe(cr.get("summary", ""))
                result_text = f"❌ Compaction failed: {err}" if err else "❌ Compaction failed."
            else:
                result_text = "⚠️ Compaction timed out."
        except Exception:
            logger.warning("Discord !compact failed for %s", session_key, exc_info=True)
            result_text = "❌ Compaction failed unexpectedly."
            # Drop the wedged native conversation, NOT the session's channel
            # identity: the map entry carries the mirror binding, so a full
            # ``destroy`` would silently unlink a mirrored conversation.
            # Housekeeping never unlinks (see ``SessionMap.prune`` and
            # ``SessionManager._recycle_held``).
            try:
                await self.sessions.discard_conversation(session_key)
            except Exception:
                logger.debug(
                    "Discord: discard after compact failure failed",
                    exc_info=True,
                )

        final = result_text or "✅ Context compacted."
        if status_id:
            await self.client.edit_message(channel_id, status_id, final)
        else:
            await self.client.send_message(channel_id, final)
    finally:
        self.sessions.release(session_key)


# Each handler takes a ``reply`` sink instead of a channel id, because the
# same body serves two delivery shapes: a ``!`` text command answers with a
# normal channel message, while a registered slash command must answer its
# own interaction (ephemerally, inside Discord's ~3s callback deadline).
# Sharing the body is the point: a second copy per surface is how the two
# drift, and the slash form is the one an operator will actually discover.
# Each handler replies EXACTLY ONCE — an interaction callback may only be
# used for the first response, and a second would need a followup route.
async def _handle_status(
    self: DiscordDispatcher, reply: "ReplyFn", scope_id: str | None = None
) -> None:
    """Report runtime stats, from the same source Slack's ``/kirocrew status`` uses.

    ``Stats()`` is the process-wide counter set, so the two channels cannot
    report different numbers for the same gateway. The auto-approve line is
    appended because it is the one piece of runtime state that changes what a
    tool call will DO, and a user deciding whether to send a request needs it.

    READ-ONLY, and the only mention of the grant this channel makes: Discord
    can report auto-approve but cannot take, renew, or drop it. Granting is
    the operator's, from the dashboard or the machine running the gateway.

    Nothing here names a path, a token, or a config value: a slash command is
    invocable from an allow-listed guild thread that every member can read.
    """
    so = safety_override()
    yolo = f"ON ({describe_grant_lifetime()})" if so.is_active() else "OFF"
    await reply(
        f"📊 {Stats().summary()}\n"
        f"agent `{self._resolve_agent(scope_id)}` · approval `{self.approval_mode}` · "
        f"YOLO {yolo}"
    )


def _channel_reply(self: DiscordDispatcher, channel_id: str) -> "ReplyFn":
    """A reply sink that posts a normal channel message."""

    async def _send(text: str) -> None:
        assert self.client is not None
        await self.client.send_message(channel_id, text)

    return _send


async def _run_reply_command(
    self: DiscordDispatcher,
    cmd: str,
    reply: "ReplyFn",
    *,
    user_id: str,
    thread_id: str,
    text: str,
) -> None:
    """Dispatch one single-reply command through the given sink.

    The two surfaces share this so a command cannot exist on one and not the
    other: the text path and the slash path differ only in the sink they bind.
    """
    if cmd == "status":
        await self._handle_status(reply, self._scope_id(user_id, thread_id))
