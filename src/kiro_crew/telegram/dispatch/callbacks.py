"""Route an inline-keyboard press: session picks, approvals, picker presses, ``[OPTIONS:]``.

A press is authorized and gated like an inbound message -- the peer roster, the
shared forum gate, then the operator's ``channels`` governance, with an explicit
approval reject exempt -- before any branch acts on it. An approval press resolves a
``TelegramApprovalDecider`` window by its nonce; an ``[OPTIONS:]`` choice is replayed
as a fresh turn bound to the session that rendered the keyboard.
"""

from __future__ import annotations

import html
import logging
from typing import TYPE_CHECKING

from kiro_crew.telegram.transport import TelegramInboundMessage, forum_gate_outcome

if TYPE_CHECKING:
    from kiro_crew.telegram.client import TelegramCallback
    from kiro_crew.telegram.transport_dispatch import TelegramDispatcher

#: The dispatcher's one logger, named for the facade module operators filter on.
logger = logging.getLogger("kiro_crew.telegram.transport_dispatch")

#: A legacy button carries no proof of which session authored its model-written
#: label, so applying it to the current session would be a cross-session injection.
_UNTAGGED_OPTIONS_REFUSAL = (
    "🔘 These buttons predate a session-safety update, so which conversation "
    "they belong to cannot be verified and your choice was NOT applied. "
    "Type it as a message instead."
)


async def on_callback(self: TelegramDispatcher, cb: "TelegramCallback") -> None:
    """Route an inline-keyboard press: approval decisions or [OPTIONS:]."""
    from kiro_crew.telegram import transport_dispatch as facade

    assert self.client is not None
    # Auth first (deny-by-default short-circuit): don't even ack an
    # unauthorized user's press — avoids a wasted Bot API round-trip.
    if not self._authorized(cb.user_id):
        return
    # Chat-type gate — an authZ boundary that MUST mirror
    # ``transport.receive`` EXACTLY: buttons live on messages the bot sent,
    # so a press can originate from a private DM or an allow-listed
    # supergroup forum Topic. Uses the SHARED ``forum_gate_outcome`` predicate
    # so this fail-closed decision can never drift from the inbound path.
    # NEVER honor a callback from an ordinary group, a non-allow-listed
    # supergroup, or the supergroup General chat (no thread). This gate is
    # ADDITIONAL to the owner/user authorization above, not a replacement.
    # Both sides now follow the SAME reloaded config: this site reads it at
    # point of use, and the transport's frozen copy is replaced wholesale by
    # ``reconfigure`` from the config applier. The transport still freezes
    # rather than reading live so one inbound decision cannot see the set
    # change under it; the two can differ only for the instant between a
    # reload and the applier's push.
    forum_cfg = self._live_cfg().telegram
    outcome = forum_gate_outcome(
        cb.chat_type,
        cb.chat_id,
        getattr(cb, "message_thread_id", None),
        allow_forum=bool(forum_cfg.allow_forum),
        allowed_forum_chat_ids=forum_cfg.allowed_forum_chat_ids,
    )
    if outcome is not None:
        facade.sel().log_api_access(
            caller=str(cb.user_id) or "unknown",
            operation="telegram_transport.on_callback",
            outcome=outcome,
            source="telegram",
        )
        return
    # Answer FIRST (after auth) to dismiss the button spinner — the governance
    # check below does off-loop profile-store I/O that could otherwise delay
    # the callback answer past Telegram's expectation. Answering is a no-op UI
    # dismissal; it does NOT resolve the approval or start a turn.
    await self.client.answer_callback(cb.callback_query_id)

    data = cb.data or ""

    # Inbound channels-governance gate (off-loop) — a callback press RESOLVES a
    # tool approval (executes the governed tool) or injects an [OPTIONS:]
    # choice (starts a turn), so it must pass the SAME gate as a message BEFORE
    # any resolution. Without it, an admin deny added after connect could still
    # execute a governed tool via a stale approval button.
    # EXCEPTION: an explicit REJECT of a tool approval ("a:...:0") is a DENIAL —
    # exactly what a channels-deny wants — so let it resolve the pending future
    # as refused rather than silently dropping it (which would strand the
    # kiro-cli approval until timeout, ~300s). Approve presses and [OPTIONS:]
    # turns stay blocked.
    _is_reject_press = data.startswith("a:") and data.rpartition(":")[2] == "0"
    if not _is_reject_press and not await facade.channel_inbound_permitted("telegram"):
        logger.info("telegram callback dropped: denied by channels governance policy")
        return

    if data.startswith("s:"):
        # The native session key is supplied by the DISPATCHER because only it
        # knows this DM's dm_scope. Under ``dm_scope="unified"`` the native
        # bucket is a ``unified:`` key, not a ``telegram:`` one, so the resume
        # adapter cannot recognise its own outbound mirror by namespace and
        # would demand a preparatory /unlink for one-click takeover.
        press_route = self._route_key(
            chat_type=cb.chat_type,
            user_id=cb.user_id,
            chat_id=cb.chat_id,
            thread=cb.message_thread_id,
        )
        # Under the SAME per-route lock a message takes for its routing
        # decision. A press and the message that follows it are independent
        # Telegram tasks, so without this the message can resolve its session
        # before the press has committed the binding — and its turn, with its
        # transcript, lands in the native session the user just left.
        async with self._routing_turn(
            self._session_resume.expectation_id(cb.chat_id, self._route_thread(press_route))
        ):
            await self._session_resume.choose(
                self.client, cb, native_key=self._session_key(press_route)
            )
        return

    # Route the callback to the same conversation identity its turn used so
    # an approval/[OPTIONS:] press resolves against the correct session key:
    # a private press -> (direct, user_id); an allow-listed forum press ->
    # the per-Topic forum key (chat_type + message_thread_id carried through).
    route = self._route_key(
        chat_type=cb.chat_type,
        user_id=cb.user_id,
        chat_id=cb.chat_id,
        thread=getattr(cb, "message_thread_id", None),
    )
    # Topic id to thread the [OPTIONS:] echo sends back into (None for a DM).
    cb_thread = self._route_thread(route)

    # Tool-approval decision: "a:<request_id>:<nonce>:<1|0>".
    if data.startswith("a:"):
        # "a:<request_id>:<nonce>:<flag>". Parsed from the RIGHT so a request id
        # containing a colon cannot shift the fields: the flag and the nonce are
        # the last two segments and the id is whatever precedes them. A button
        # rendered before the nonce existed leaves `nonce` holding part of the id
        # and fails the constant-time compare, which is the correct answer — it
        # is a press from an earlier process.
        body = data[2:]
        rest, _, flag = body.rpartition(":")
        rid, _, nonce = rest.rpartition(":")
        trust = flag == "t"
        approved = flag in ("1", "t")
        session_key = self._callback_session_key(
            route,
            cb.chat_id,
            cb_thread,
            cb.user_id,
            cb.chat_type,
        )
        key = facade.TelegramApprovalDecider.key(session_key, rid)
        # Asked BEFORE the grant, because Trust is the one press with a side
        # effect that OUTLIVES the prompt: it auto-approves every later tool in
        # this conversation and writes the session's approval policy to `auto`
        # so subagents inherit it. The registry is empty after a gateway
        # restart, so without this every Trust button still in the chat's
        # scrollback would silently re-grant standing authority while the reply
        # said the approval had expired.
        pending = facade.TelegramApprovalDecider.is_pending(key, nonce)
        if trust and pending:
            # Granted BEFORE resolving, so the tool this very prompt is asking
            # about is covered by the grant the press just made — resolving
            # first would approve this one by the button and then let the NEXT
            # tool race the write.
            facade.add_trusted_session(session_key, self.sessions)
            facade.sel().log_api_access(
                caller=str(cb.user_id) or "unknown",
                operation="telegram.trust_session",
                outcome="allowed",
                source="telegram",
                resources=f"session={session_key}",
            )
        elif trust:
            # Audited as a refusal rather than dropped: an operator pressing
            # Trust and getting nothing needs the reason to be findable.
            facade.sel().log_api_access(
                caller=str(cb.user_id) or "unknown",
                operation="telegram.trust_session",
                outcome="denied",
                source="telegram",
                resources=f"session={session_key}",
                error="no_pending_approval",
            )
        resolved = facade.TelegramApprovalDecider.resolve_global(key, approved, nonce=nonce)
        if resolved:
            if trust:
                verdict = "🤝 Trusted — this conversation's tools auto-approve."
            else:
                verdict = "✅ Approved" if approved else "🚫 Denied"
        else:
            # No pending decision to resolve — the request already timed out
            # (decider denies by default and pops the key), was answered, or the
            # press came from a STALE keyboard whose nonce does not match
            # (request ids restart at 1 per provider process, so an old button can
            # name an id that is live again for a different tool).
            # Don't imply the press took effect: a post-timeout "Approve" on
            # an already-denied tool must not display "Approved".
            verdict = "⌛ This approval already expired."
        await self.client.edit_message(
            cb.chat_id, cb.message_id, verdict, reply_markup={"inline_keyboard": []}
        )
        return

    # Model pick: "m:<index>" into the picker posted on this message.
    # A picker press: "m:<index>" for a model, "g:<index>" for an agent. Both
    # resolve through one helper — the staleness contract (consume before
    # applying, and the wording that must not claim "expired" for a picker that
    # was simply used) is one decision, and two copies of it means a fix to
    # double-press or eviction handling reaches one picker.
    for prefix, table, noun, command, operation, resource in (
        (
            "m:",
            self._model_pickers,
            "model",
            "/model",
            "telegram.set_model",
            "model",
        ),
        (
            "g:",
            self._agent_pickers,
            "agent",
            "/agent",
            "telegram.set_agent",
            "agent",
        ),
    ):
        if not data.startswith(prefix):
            continue
        taken = await self._consume_picker(cb, data, table, noun=noun, command=command)
        if taken is None:
            return
        picker, value, label = taken
        if prefix == "m:":
            current_target = self._callback_session_key(
                route,
                cb.chat_id,
                cb_thread,
                cb.user_id,
                cb.chat_type,
            )
            if not current_target or current_target != picker.session_key:
                facade.sel().log_api_access(
                    caller=str(cb.user_id) or "unknown",
                    operation=operation,
                    outcome="denied",
                    source="telegram",
                    resources=f"{resource}={label}",
                    error="session_binding_changed",
                )
                await self.client.edit_message(
                    cb.chat_id,
                    cb.message_id,
                    "⌛ This model list belongs to a session this chat no longer "
                    "controls. Send /model again.",
                    reply_markup={"inline_keyboard": []},
                )
                return
            outcome = await self._apply_model(
                picker.route,
                value,
                picker.session_key,
                store_route_preference=picker.store_route_preference,
            )
        else:
            outcome = await self._apply_agent(picker.route, value)
        facade.sel().log_api_access(
            caller=str(cb.user_id) or "unknown",
            operation=operation,
            outcome="allowed",
            source="telegram",
            resources=f"{resource}={label}",
        )
        # One edit carries both the result text and the retired keyboard, so
        # the buttons never outlive the choice they represent.
        await self.client.edit_message(
            cb.chat_id, cb.message_id, outcome, reply_markup={"inline_keyboard": []}
        )
        return

    # [OPTIONS:] choice: ``opt:<index>:<origin-tag>``. The label is
    # recovered from the button text; the tag binds it to the session that
    # authored the keyboard.
    if data.startswith("opt:"):
        parts = data.split(":", 2)
        origin_tag = parts[2] if len(parts) == 3 else ""
        choice_text = cb.label
        # Retire the keyboard but KEEP the original answer text intact --
        # tapping an option must not overwrite the answer bubble. The choice
        # is handled as a fresh turn whose reply arrives as a NEW message.
        await self.client.edit_message_reply_markup(
            cb.chat_id, cb.message_id, {"inline_keyboard": []}
        )
        if not origin_tag:
            # A button created before provenance existed cannot prove which
            # session its model-authored text belongs to. Never infer that
            # from whatever session happens to be current now.
            await self._reply(
                cb.chat_id,
                _UNTAGGED_OPTIONS_REFUSAL,
                thread=cb_thread,
            )
            return
        if not choice_text:
            await self._reply(
                cb.chat_id,
                "⚠️ Couldn't read that choice — please type it instead.",
                thread=cb_thread,
            )
            return
        # Echo the picked option as its own block (a button tap can't
        # render as a real user message), then re-dispatch as a fresh turn.
        echoed = await self._reply(
            cb.chat_id,
            f"<blockquote>{html.escape(choice_text)}</blockquote>",
            thread=cb_thread,
            parse_mode="HTML",
            retry_plain=False,
        )
        if echoed is None:  # malformed HTML -> plain fallback
            await self._reply(cb.chat_id, f"» {choice_text}", thread=cb_thread)
        # Re-inject the choice with the callback's ORIGINAL route so a forum
        # press stays under the same Topic. ``from_widget`` bypasses forum
        # activation because tapping this bot's own keyboard addresses it.
        synthetic = TelegramInboundMessage(
            channel_type="telegram",
            user_id=str(cb.user_id),
            conversation_id=str(cb.chat_id),
            text=choice_text,
            thread_id=(
                str(cb.message_thread_id) if getattr(cb, "message_thread_id", None) else None
            ),
            chat_type=cb.chat_type,
            from_widget=True,
        )
        # The label is MODEL-AUTHORED. A leading command token is ordinary
        # turn content, never permission for the model to execute `/new`,
        # `/dashboard`, `/yolo`, or any future command. The non-empty tag
        # still asks handle_message to validate current and final affinity.
        await self.handle_message(
            synthetic,
            interpret_commands=False,
            origin_tag=origin_tag,
        )
