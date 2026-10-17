"""The handlers behind Telegram's chat commands.

``TelegramDispatcher.handle_message`` parses a command and dispatches it through
``self``; the handlers here implement ``/kirocrew dashboard``, ``/yolo``, ``/cron``,
``/spawn``, ``/task``, ``/link``, ``/unlink`` and ``/compact``, the direct-message gate
for host-wide listings, and markdown replies. The channel-neutral half of each
command lives in ``messaging/commands.py`` and ``messaging/link.py``. ``/stop`` and
``/title`` stay in ``transport_dispatch.py``, where repository guards read them, and
the bare privacy modifiers stay inline in ``handle_message``; ``/model`` and
``/agent`` are ``dispatch/pickers.py`` and ``/voice`` is ``dispatch/voice.py``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from kiro_crew.messaging.commands import (
    YOLO_PHRASING_PLAIN,
    compact_unsupported_backend,
    compact_unsupported_reply,
    cron_command_reply,
    format_ttl,
    parse_dashboard_ttl,
    run_yolo_command,
    spawn_task_reply,
    task_arg_reply,
)
from kiro_crew.messaging.link import (
    rebind_conversation_location,
    release_conversation_location,
)
from kiro_crew.messaging.session_resume import ResumeReleaseError
from kiro_crew.session_map import ConversationOwnershipConflict
from kiro_crew.telegram.commands import parse_dashboard_argument
from kiro_crew.telegram.renderer import md_to_telegram_html_safe

if TYPE_CHECKING:
    from kiro_crew.telegram.transport_dispatch import TelegramDispatcher

#: The dispatcher's one logger, named for the facade module operators filter on.
logger = logging.getLogger("kiro_crew.telegram.transport_dispatch")

_RELEASE_FAILURE = (
    "⚠️ Could not leave the resumed session safely, so nothing changed. "
    "Try again before sending another message."
)


async def _handle_dashboard(
    self: TelegramDispatcher, route: tuple[str, str], chat_id: int, text: str, user_id: int
) -> None:
    """Generate and send a presigned dashboard login link.

    Mirrors the Slack ``/kirocrew dashboard`` implementation: calls
    ``generate_token`` directly (never via shell) and builds the URL from
    the ``dashboard.url`` config (``KIROCREW_PORT`` overrides the port,
    matching every other link producer).

    DM-only: a presigned link posted into a forum Topic would hand a
    dashboard login to every member of the supergroup, so group requests
    are refused with a pointer to DM — the same token-leak policy as
    Slack's always-DM delivery.
    """
    from kiro_crew.telegram import transport_dispatch as facade

    assert self.client is not None
    from kiro_crew.dashboard.token_auth import (
        MAX_SESSION_TTL_SECS,
        generate_token,
        parse_duration,
    )
    from kiro_crew.dashboard.urls import dashboard_origin, parse_dashboard_url

    thread = self._route_thread(route)
    if not self._is_private_route(route):
        await self._reply(
            chat_id,
            "🔒 Dashboard links are only sent in a direct message — "
            "DM me `/kirocrew dashboard`.",
            thread=thread,
        )
        return
    ttl_secs = min(
        parse_dashboard_ttl(parse_dashboard_argument(text), parse_duration=parse_duration),
        MAX_SESSION_TTL_SECS,
    )
    try:
        token = generate_token(str(user_id), ttl_seconds=ttl_secs)
        origin = dashboard_origin(self.cfg.dashboard.url)
        if not origin:
            # No configured dashboard.url: fall back to the local port
            # (parse_dashboard_url applies the KIROCREW_PORT override).
            _, port = parse_dashboard_url(self.cfg.dashboard.url)
            origin = f"http://localhost:{port}"
        url = f"{origin}/?token={token}"
        ttl_display = format_ttl(ttl_secs)
        # Credential issuance MUST be audited (backend-security-controls):
        # mirrors slack.dashboard_token and telegram.yolo_mode.
        facade.sel().log_api_access(
            caller=str(user_id),
            operation="telegram.dashboard_token",
            outcome="ok",
            source="telegram",
            resources=f"ttl={ttl_secs}",
        )
        await self._reply(
            chat_id,
            f"🔗 Dashboard link (valid {ttl_display}):\n{url}",
            thread=thread,
        )
    except Exception as exc:
        logger.warning("telegram /kirocrew dashboard: token generation failed", exc_info=True)
        try:
            facade.sel().log_api_access(
                caller=str(user_id),
                operation="telegram.dashboard_token",
                outcome="error",
                source="telegram",
                resources=f"ttl={ttl_secs}",
            )
        except Exception:
            # The audit trail must never turn a user-facing failure reply
            # into a crash; the warning above already captured the error.
            pass
        await self._reply(
            chat_id,
            f"⚠️ Could not generate dashboard link: {exc}",
            thread=thread,
        )


async def _handle_yolo(
    self: TelegramDispatcher, chat_id: int, arg: str, user_id: int, *, thread: int | None = None
) -> None:
    """Report or change the global auto-approve grant.

    The ladder, its replies, the off-loop mutators and the SEL row live in
    :func:`~kiro_crew.messaging.commands.run_yolo_command`. Reachable only by
    an allow-listed Telegram user, because ``transport.receive`` is
    deny-by-default and owner-only before dispatch ever runs, which is why
    the user id is trustworthy as the audited caller.
    """
    reply = await run_yolo_command(
        arg,
        source="telegram",
        caller=str(user_id),
        phrasing=YOLO_PHRASING_PLAIN,
    )
    await self._reply(chat_id, reply, thread=thread)


async def _handle_cron(
    self: TelegramDispatcher, chat_id: int, arg: str, *, caller: str = "", thread: int | None = None
) -> None:
    """List / pause / resume / remove scheduled jobs, via the shared layer.

    *caller* is the Telegram user id, threaded through so ``remove all``'s SEL
    audit names the person who issued it rather than only the surface. Same
    attribution the Slack, dashboard, MCP and CLI paths carry.
    """
    if self.cron_service is None:
        await self._reply(chat_id, "Cron is not running on this instance.", thread=thread)
        return
    reply = await cron_command_reply(
        f"cron {arg}".strip(), self.cron_service, source="telegram", caller=caller
    )
    if reply is None:
        await self._reply(
            chat_id,
            "Usage: /cron list | pause <id> | resume <id> | remove <id>|all",
            thread=thread,
        )
        return
    await self._reply_markdown(chat_id, reply, thread=thread)


async def _handle_spawn(
    self: TelegramDispatcher,
    route: tuple[str, str],
    chat_id: int,
    arg: str,
    *,
    thread: int | None = None,
    session_key: str | None = None,
) -> None:
    """Run a task in a background subagent, or list the running ones."""
    if self.subagent_manager is None:
        await self._reply(chat_id, "Subagents are not available on this instance.", thread=thread)
        return
    # Rotated: the subagent's completion arrives later and is routed by this
    # key, so binding it to a generation the next message abandons sends the
    # result to a conversation nobody is reading.
    reply = await spawn_task_reply(
        arg, self.subagent_manager, session_key or self._rotated_session_key(route)
    )
    if reply is None:
        await self._reply(chat_id, "Usage: /spawn <task>  ·  /spawn list", thread=thread)
        return
    await self._reply_markdown(chat_id, reply, thread=thread)


async def _handle_task(
    self: TelegramDispatcher,
    chat_id: int,
    arg: str,
    *,
    route: tuple[str, str],
    thread: int | None = None,
    session_key: str | None = None,
) -> None:
    """Drive the task runner: ``run <spec>`` / ``status`` / ``cancel``.

    The originating session key rides along so a task that later blocks on an
    approval can tell THIS conversation, rather than only the Slack owner's DM
    — which is the whole notice a Telegram-only operator would never see.
    """
    if self.task_runner is None:
        await self._reply(
            chat_id, "The task runner is not available on this instance.", thread=thread
        )
        return
    # Rotated, for the same reason as /spawn: the approval notice comes back
    # later and is routed by this key.
    reply = await task_arg_reply(
        arg,
        self.task_runner,
        session_key=session_key or self._rotated_session_key(route),
    )
    if reply is None:
        await self._reply(
            chat_id,
            "Usage: /task run <spec-path> | /task status | /task cancel",
            thread=thread,
        )
        return
    await self._reply_markdown(chat_id, reply, thread=thread)


async def _require_direct_chat(
    self: TelegramDispatcher,
    cmd: str,
    route: tuple[str, str],
    chat_id: int,
    user_id: int,
    *,
    thread: int | None,
    subject: str,
) -> bool:
    """Refuse a host-wide listing outside a DM. True = the caller may proceed.

    The allow-list gates who may DRIVE a turn, not who can READ the reply, and a
    forum Topic is readable by the whole supergroup. So a command whose answer
    names state belonging to the host rather than to this conversation -- every
    session on the box, every scheduled job -- would disclose it to members who
    were never allow-listed at all. Same rule `/kirocrew dashboard` follows.

    Scoped to the LISTINGS, and per argument rather than per command. It is not
    "host-wide command" as a category: `/spawn <task>` and `/task run <spec>` act
    on THIS conversation's session and report on their own work, and `/stop` and
    `/compact` are how a forum operator drives the Topic they are in. Refusing
    those would break the forum surface to no benefit, since a caller who can
    reach them already had to be allow-listed.

    But the same command changes scope with its argument, which the command name
    does not show: `/spawn list` renders every subagent on the box with its task
    text, and `/task status` reports the one global runner. `lists_host_state`
    (`messaging/commands.py`) is the answer to that, held next to the functions
    that build those replies -- reading `/spawn <task>` and generalizing to
    `/spawn` is precisely how the listing got through the first time.

    Slack's equivalents have no such shape -- their reply lands in a DM or in a
    thread the caller is already in -- so this is the Telegram-specific half of
    the same rule rather than a divergence from parity.
    """
    from kiro_crew.telegram import transport_dispatch as facade

    if self._is_private_route(route):
        # A DM is the right AUDIENCE, and for a host-wide listing it also has to
        # be the right PERSON. `allowed_user_ids` is a list of people permitted
        # to talk to the agent, not a claim that any one of them is the operator,
        # so with several entries a listing of every conversation on the host
        # hands one allow-listed human another's conversation titles -- under the
        # default per-peer dm_scope those are separate sessions belonging to
        # separate people. This is the rule the owner notification already
        # follows for the same reason (messaging.md: "a channel must be able to
        # NAME the owner: exactly one configured target, or nothing", which cites
        # `/sessions`' owner-only rule as its premise); applying it here is that
        # rule reaching the surface it was named after.
        #
        # It costs an operator who lists two of their own accounts, which is the
        # same cost main accepted there: the count is over ALL configured
        # entries, because a two-person allow-list is a guess either way and
        # guessing wrong discloses a third party's titles.
        if len(self._allowed) <= 1:
            return True
        facade.sel().log_api_access(
            caller=str(user_id),
            operation=f"telegram.{cmd}_command",
            outcome="denied",
            source="telegram",
            resources=f"allowed_identities={len(self._allowed)}",
            error="no_unambiguous_owner",
        )
        await self._reply(
            chat_id,
            f"🔒 The {subject} names conversations across this whole install, so "
            "it is only sent when `telegram.allowed_user_ids` holds a single "
            "operator. It currently holds several, and the agent cannot tell "
            "which of them owns the install.",
            thread=thread,
        )
        return False
    facade.sel().log_api_access(
        caller=str(user_id),
        operation=f"telegram.{cmd}_command",
        outcome="denied",
        source="telegram",
        resources=f"chat={chat_id}",
        error="shared_topic_audience",
    )
    await self._reply(
        chat_id,
        f"🔒 The {subject} is only sent in a direct message. DM me `/{cmd}`.",
        thread=thread,
    )
    return False


async def _reply_markdown(
    self: TelegramDispatcher, chat_id: int, text: str, *, thread: int | None = None
) -> int | None:
    """Send a reply whose text carries markdown, rendered rather than literal.

    The shared command replies (``messaging/commands.py``) are written in the
    markdown Slack renders natively — ``*Your cron jobs:*``, backticked job
    ids — and Telegram's ``send_message`` defaults to plaintext, so posting
    them unrendered shows the asterisks and backticks to the user. Converted
    through the renderer's own translator so there is one markdown→Telegram
    grammar, with ``retry_plain`` so a conversion Telegram rejects degrades to
    readable text rather than failing the reply.

    Rendering markup is what makes this a redaction sink, and not all of this
    text is ours: ``/cron list`` and ``/tasks`` echo job and task names an LLM
    wrote, so a credential split by ``**`` survives the byte-level pass and
    the translator would rejoin the halves into one rendered key.
    ``md_to_telegram_html_safe`` redacts against the rendered form first;
    off-loop because that scan is the expensive half.
    """
    return await self._reply(
        chat_id,
        await asyncio.to_thread(md_to_telegram_html_safe, text),
        thread=thread,
        parse_mode="HTML",
    )


async def _handle_link(
    self: TelegramDispatcher,
    route: tuple[str, str],
    chat_id: int,
    *,
    resumed_key: str | None = None,
) -> None:
    """Re-enable mirroring of this conversation's dashboard tab back here.

    The rebind sequence, its batching and its reply live in the shared
    :func:`~kiro_crew.messaging.link.rebind_conversation_location`, the
    counterpart of the ``release_conversation_location`` that ``/unlink``
    uses; this only supplies Telegram's spelling of "this conversation" and
    of the unlink command.
    """
    assert self.client is not None
    thread = self._route_thread(route)
    if resumed_key is not None:
        await self._reply(
            chat_id,
            "⚠️ A resumed session is active here. Send /unlink first.",
            thread=thread,
        )
        return
    # Through the shared helper, which owns the claim-before-withdrawal
    # ordering and the single batched write. The key is ROTATED: a mirror
    # binding is DURABLE and re-read on the next inbound turn, so writing it
    # against a generation the idle window has retired would leave the very
    # next message unlinked again.
    try:
        reply = rebind_conversation_location(
            self.sessions,
            key=self._rotated_session_key(route),
            location=self._origin_mirror_link(route, chat_id),
            unlink_command="/unlink",
        )
    except ConversationOwnershipConflict:
        logger.info("telegram link refused: conversation already held")
        await self._reply(
            chat_id,
            "⚠️ Another session is already linked here. Send /unlink first.",
            thread=thread,
        )
        return
    await self._reply(chat_id, reply, thread=thread)


async def _handle_unlink(self: TelegramDispatcher, route: tuple[str, str], chat_id: int) -> None:
    assert self.client is not None
    thread = self._route_thread(route)
    try:
        left_resumed = await self._session_resume.leave_resumed_session(chat_id, thread)
    except ResumeReleaseError:
        await self._reply(chat_id, _RELEASE_FAILURE, thread=thread)
        return
    if left_resumed is not None:
        await self._reply(
            chat_id,
            "✅ Left the resumed session. Back to your Telegram conversation.",
            thread=thread,
        )
        return
    # Rotated, for the same reason as /link: the opt-out is durable and is
    # re-read per turn, so it has to land on the key the next turn will use.
    key = self._rotated_session_key(route)
    # Persist the refusal BEFORE releasing: mirroring is re-asserted on every
    # inbound turn, so a release alone would be undone by the user's next
    # message. Batched with the release so the pair is one whole-map write
    # instead of four. No dashboard nudge here: a swept slot's link chip is
    # refreshed by the periodic channel_slot_reconciler push.
    with self.sessions.batched_save():
        self.sessions.set_mirror_opt_out(key, True)
        reply, _swept = release_conversation_location(
            self.sessions,
            key=key,
            location=self._origin_mirror_link(route, chat_id),
            channel="telegram",
        )
    await self._reply(chat_id, reply, thread=self._route_thread(route))


async def _handle_compact(
    self: TelegramDispatcher,
    route: tuple[str, str],
    chat_id: int,
    *,
    session_key: str | None = None,
) -> None:
    """In-place ACP ``/compact`` on the user's session (mirrors Slack).

    Holds the per-session semaphore for the WHOLE compaction. Each Telegram
    update is dispatched as its own task, so a bare ``locked()`` check
    followed by ``stream_command`` would race: a normal turn could take the
    semaphore in the window between the check and the stream, and the two
    would then interleave JSON-RPC on one stdio channel and corrupt session
    state. ``try_acquire()`` takes the semaphore atomically (or refuses if a
    turn is already in flight); the ``finally`` always releases it.
    """
    assert self.client is not None
    target_key = session_key or self._session_key(route)
    thread = self._route_thread(route)
    # Atomically take the turn semaphore, or refuse. Distinguish "busy" (a
    # turn is streaming) from "no session yet" for the user-facing note.
    if not await self.sessions.try_acquire(target_key):
        if self.sessions.has_session(target_key):
            await self._reply(
                chat_id,
                "⏳ Still working on your last message — try /compact once it finishes.",
                thread=thread,
            )
        else:
            await self._reply(chat_id, "No active session to compact.", thread=thread)
        return
    try:
        provider = self.sessions.get_provider(target_key)
        if provider is None:
            await self._reply(chat_id, "No active session to compact.", thread=thread)
            return

        # Capability gate (mirrors the dashboard's compact gate): a
        # backend that cannot serve a manual /compact treats the prompt as
        # ordinary text and never answers, so dispatching would strand the
        # 120s wait below. Informational, never an error.
        unsupported = compact_unsupported_backend(provider)
        if unsupported:
            await self._reply(chat_id, compact_unsupported_reply(unsupported), thread=thread)
            return

        status_id = await self._reply(chat_id, "🔄 Compacting context…", thread=thread)
        result_text: str | None = None
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
                err = cr.get("summary", "")
                result_text = f"❌ Compaction failed: {err}" if err else "❌ Compaction failed."
            else:
                result_text = "⚠️ Compaction timed out."
        except Exception:
            logger.warning("Telegram /compact failed for %s", target_key, exc_info=True)
            result_text = "❌ Compaction failed unexpectedly."
            # Drop the wedged native conversation, NOT the session's channel
            # identity: the map entry carries the mirror binding, so a full
            # ``destroy`` would silently unlink a mirrored conversation.
            # Housekeeping never unlinks (see ``SessionMap.prune`` and
            # ``SessionManager._recycle_held``).
            try:
                await self.sessions.discard_conversation(target_key)
            except Exception:
                logger.debug("Telegram: discard after compact failure failed", exc_info=True)

        final = result_text or "✅ Context compacted."
        if status_id:
            await self.client.edit_message(chat_id, status_id, final)
        else:
            await self._reply(chat_id, final, thread=thread)
    finally:
        # Always release the semaphore we took. No-op if the except path
        # already tore the session down (release() looks up by key).
        self.sessions.release(target_key)
