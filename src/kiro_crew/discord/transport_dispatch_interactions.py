"""Every Discord interaction: a button press (a tool approval, a session pick, a model
pick or an ``[OPTIONS:]`` choice) and a registered slash command.

``on_interaction`` authorizes, acknowledges and routes each press, and re-reads the
governance ceiling and the rosters before anything but an explicit reject resolves;
``_interaction_actionable``
is the destination check it shares with the spawn-approval timeout path.
``_on_command_interaction`` rebuilds a slash command's text form and runs it, and
``_interaction_reply`` is the ephemeral reply sink it answers through. Every
interaction response this dispatcher issues is in this module.

Each function here whose first parameter is ``self`` is a ``DiscordDispatcher``
method: the class binds it by name, so it reads and writes the dispatcher's own
state on the instance, and a patch of the method on the class or on an instance
reaches every caller.

Instance fields read (a call on one included): ``_allowed_threads``, ``_model_pickers``,
``_session_resume``, ``client``, ``transport``.

Instance fields assigned, or whose mapping is changed: ``_model_pickers``.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from kiro_crew.discord.commands import build_help_text
from kiro_crew.discord.renderer import DiscordApprovalDecider
from kiro_crew.discord.transport_dispatch_commands import _REPLY_COMMANDS
from kiro_crew.discord.transport_dispatch_model_picker import _MODEL_PICKER_TTL_SECS
from kiro_crew.messaging.identity import channel_inbound_permitted, channel_outbound_permitted
from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.sel import sel

if TYPE_CHECKING:
    from kiro_crew.discord.client import DiscordInteraction
    from kiro_crew.discord.transport_dispatch import DiscordDispatcher, ReplyFn

#: The dispatcher's one logger, named for the facade module operators filter on.
logger = logging.getLogger("kiro_crew.discord.transport_dispatch")


#: Refusal for a pre-provenance button (bare ``opt:<i>``): Discord replays old
#: components indefinitely, so an untagged press can never prove which session
#: it belongs to — fail closed rather than route it by current binding.
_UNTAGGED_OPTIONS_REFUSAL = (
    "🔘 These buttons predate a session-safety update, so which conversation "
    "they belong to can no longer be verified and your choice was NOT applied. "
    "Type it as a message instead."
)


async def on_interaction(self: DiscordDispatcher, itx: "DiscordInteraction") -> None:
    """Route an interaction: a slash command, an approval, or a choice."""
    assert self.client is not None
    # Auth first (deny-by-default short-circuit).
    if not self._authorized(itx.user_id):
        return
    thread_id = itx.channel_id if itx.guild_id else ""
    # The same actionable-interaction decision is re-used by the spawn
    # approval timeout path. If a condition here can drop every press, an
    # unpressed spawn wait must observe it before reporting a user denial.
    interaction_actionable = await self._interaction_actionable(itx.user_id, thread_id)
    if not itx.guild_id:
        # A DM interaction names its peer, and every callback below answers
        # that SAME channel without ever opening it, so this is where the
        # pairing can be learned for the interaction direction. Recorded
        # before any callback is issued, because the mid-send re-check runs
        # inside the first one; and on the authorized path only, so a denied
        # presser cannot plant a pairing. Mirrors transport.receive().
        self.client.remember_dm_recipient(itx.channel_id, itx.user_id)
    # Guild interactions are accepted only in an allow-listed channel that
    # Discord confirms is a thread. This mirrors transport.receive().
    if itx.guild_id and not interaction_actionable:
        # A COMMAND gets an ephemeral explanation rather than silence. A
        # dropped interaction is not invisible to the user: Discord shows its
        # own red "did not respond" with no reason, which reads as the bot
        # being broken. The reply is ephemeral, so naming the rule discloses
        # nothing to the rest of the channel. Stateless commands are still
        # refused HERE rather than answered, because a shared channel is a
        # wider disclosure boundary than the thread allow-list grants and
        # turns are deliberately never run in one.
        if itx.is_command:
            # No destination, deliberately: this notice exists BECAUSE the
            # channel is not on the roster, so passing it would have the
            # re-check refuse the explanation for the very reason it is being
            # given, and the presser would see Discord's red "did not respond"
            # instead. It is ephemeral -- visible to the presser alone, never
            # posted into the channel -- so it discloses nothing the ceiling
            # governs.
            await self.client.respond_interaction(
                itx.interaction_id,
                itx.interaction_token,
                "🔒 Commands run in a direct message or an approved thread. "
                "Post here and I will open a thread, or DM me.",
                ephemeral=True,
            )
        return
    # A slash command is answered by its OWN callback, so it must not be
    # pre-acked: DEFERRED_UPDATE_MESSAGE is a component-only callback type,
    # and spending the one permitted first response on it would leave the
    # command's actual reply with no route. It also runs the governance gate
    # BEFORE responding rather than after, unlike the button path below.
    # The order matters and the trade-off is deliberate: a governance check
    # slower than Discord's ~3s callback window makes the command visibly
    # fail, where acking first would have let a policy-denied command run.
    # Failing visibly is the correct direction for a fail-closed gate.
    if itx.is_command:
        if not await channel_inbound_permitted("discord"):
            logger.info("discord command dropped: denied by channels governance policy")
            # Named, not silent, for the same reason as the guild refusal
            # above. The wording stays generic: the governance profile is the
            # operator's ceiling and its contents are not the user's to read.
            # No destination, for the same reason as well, and more sharply: the
            # outbound ceiling reads the SAME `channels` allowlist that just
            # denied this command, so a re-check would refuse the notice in
            # exactly the case it is written for. Ephemeral, so nothing the
            # ceiling governs is disclosed.
            await self.client.respond_interaction(
                itx.interaction_id,
                itx.interaction_token,
                "🔒 The Discord channel is currently disabled by policy.",
                ephemeral=True,
            )
            return
        await self._on_command_interaction(itx)
        return

    # Ack FIRST (after auth) to dismiss Discord's "interaction failed" state —
    # the governance check below does off-loop profile-store I/O that can, on a
    # slow FS, exceed Discord's ~3s interaction-ack deadline. Acking is a no-op
    # UI dismissal; it does NOT resolve the approval or start a turn.
    await self.client.ack_component_interaction(
        itx.interaction_id, itx.interaction_token, destination=itx.channel_id
    )

    data = itx.custom_id or ""

    # Inbound channels-governance gate (off-loop) — a button press RESOLVES a
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
    if not _is_reject_press:
        # The rosters are read ONCE before the ack, and the ack itself serves the
        # REST ladder's waits while the governance read is deliberately off-loop,
        # so authorization can be withdrawn across that window. Re-read the same
        # things the pre-ack gate established before anything resolves: without
        # this a stale Approve press executes the governed tool after the operator
        # has already withdrawn it. The channel TYPE is immutable and so is not
        # re-resolved -- only membership moves.
        #
        # ORDER: the governance ceiling first, the rosters LAST. The ceiling read
        # is an `await` doing profile-store I/O, so a roster reading taken before
        # it describes a state that can have changed by the time anything
        # resolves; reading the rosters last makes them the final word, which is
        # the same contract the client's own mid-send predicate states.
        if not await channel_inbound_permitted("discord"):
            logger.info("discord interaction dropped: denied by channels governance policy")
            return
        if not self._authorized(itx.user_id) or (
            itx.guild_id and thread_id not in self._allowed_threads
        ):
            logger.info(
                "discord interaction dropped: authorization withdrawn during the " "acknowledgement"
            )
            return

    # Session picker: "s:<nonce>:<index>". The controller binds the nonce
    # to the owner, channel, message, TTL, and exact server-side choice list.
    # Guild-side refusal is defence in depth: show_picker already refuses
    # outside a DM, so a guild `s:` press means a stale or forged button —
    # honouring it would replay private transcript into a shared thread.
    if data.startswith("s:"):
        if itx.guild_id:
            return
        await self._session_resume.choose(
            self.client,
            itx,
            data,
            native_key=self._session_key(itx.user_id, thread_id),
        )
        return

    # Tool-approval decision: "a:<request_id>:<nonce>:<1|0>". The nonce is
    # validated by resolve_global — a stale button (reused request ID from
    # before a restart, or an earlier prompt) fails closed.
    if data.startswith("a:"):
        body = data[2:]
        head, _, flag = body.rpartition(":")
        rid, _, nonce = head.rpartition(":")
        approved = flag == "1"
        key = DiscordApprovalDecider.key(
            self._inbound_session_key(itx.user_id, itx.channel_id, thread_id),
            rid,
        )
        resolved = DiscordApprovalDecider.resolve_global(key, approved, nonce=nonce)
        if resolved:
            verdict = "✅ Approved" if approved else "🚫 Denied"
        else:
            # No pending decision — already timed out (deny-by-default) or
            # answered. Don't imply the press took effect.
            verdict = "⌛ This approval already expired."
        # The confirmation is an outbound write, and a reject press reaches here
        # without the re-read above: resolving a denial is what a withdrawal
        # wants, but writing into the channel afterwards is not. The edit may
        # also serve no wait, in which case the ladder's own re-check never runs
        # and nothing else judges it. So both authorities are read once more.
        #
        # A channel the ceiling refuses gets NO edit at all, whichever
        # verdict it would have carried. The card is an ordinary channel message,
        # visible to everyone who can read that channel, and it was posted while
        # the channel was still permitted; leaving it exactly as the operator last
        # allowed it writes nothing new, while replacing its text names a tool and
        # its outcome into a channel the ceiling now refuses as a destination.
        # The pending approval is resolved before this point, so a withheld edit
        # costs the presser a confirmation and never the decision.
        #
        # ORDER: the ceiling first, the rosters LAST -- the ceiling read is an
        # `await`, so a roster reading taken before it can be stale by the time the
        # edit is issued, which is the same contract the client's own mid-send
        # predicate states. Read here even on the arm that already read it above,
        # rather than carrying that answer forward: a value taken before a
        # suspension can be stale by now, and on an
        # ungoverned install the read permits without writing a row, so the second
        # reading costs a row only where an operator asked for the audit trail.
        #
        # The OUTBOUND authority decides it, because what is gated here is a write
        # this process is about to make. It reads the same `channels` allowlist the
        # inbound gate above reads, so the verdict is the same; the difference is
        # the name the refusal is filed under, and an egress refusal recorded as an
        # ingress one is unreadable to whoever later asks why a message did not go
        # out.
        if not await channel_outbound_permitted("discord"):
            logger.info(
                "discord approval confirmation withheld: denied by channels " "governance policy"
            )
            return
        if not self._authorized(itx.user_id) or (
            itx.guild_id and thread_id not in self._allowed_threads
        ):
            logger.info(
                "discord approval confirmation withheld: authorization withdrawn "
                "before the verdict could be written"
            )
            return
        await self.client.edit_message(itx.channel_id, itx.message_id, verdict, components=[])
        return

    # Model pick: "m:<index>" into the picker posted on this message. The
    # index resolves against the exact choice list that picker recorded, so a
    # button Discord replays after the advertised set changed cannot apply a
    # model from a stale list.
    if data.startswith("m:"):
        token = f"{itx.channel_id}:{itx.message_id}"
        picker = self._model_pickers.get(token)
        expired = picker is not None and (time.time() - picker.created_at > _MODEL_PICKER_TTL_SECS)
        try:
            index = int(data[2:])
        except ValueError:
            index = -1
        if picker is None or expired or not (0 <= index < len(picker.choices)):
            # Covers expired, evicted and already-consumed alike — the
            # wording must not claim "expired" for a picker that was simply
            # used, which is what a double-press hits.
            self._model_pickers.pop(token, None)
            await self.client.edit_message(
                itx.channel_id,
                itx.message_id,
                "⌛ This model list is no longer active — send `!model` again.",
                components=[],
            )
            return
        # Consume the picker BEFORE applying: the switch takes a round-trip,
        # and a second press in that window would otherwise apply twice.
        self._model_pickers.pop(token, None)
        model_id, label = picker.choices[index]
        outcome = await self._apply_model(
            picker.scope_id,
            self._inbound_session_key(itx.user_id, itx.channel_id, thread_id),
            model_id,
        )
        sel().log_api_access(
            caller=itx.user_id or "unknown",
            operation="discord.set_model",
            outcome="allowed",
            source="discord",
            resources=f"model={label}",
        )
        # One edit carries both the result text and the retired buttons, so
        # they never outlive the choice they represent.
        await self.client.edit_message(itx.channel_id, itx.message_id, outcome, components=[])
        return

    # [OPTIONS:] choice: "opt:<i>:<origin-tag>" — label recovered from the
    # button text. A bare "opt:<i>" is a pre-provenance button; Discord
    # replays old components indefinitely, so its originating session can
    # never be verified — refuse it here, fail closed, before any echo
    # suggests the choice was sent.
    if data.startswith("opt:"):
        parts = data.split(":", 2)
        origin_tag = parts[2] if len(parts) == 3 else ""
        choice_text = itx.label
        # Retire the buttons but KEEP the original answer text intact —
        # a components-only PATCH leaves the content unchanged.
        await self.client.edit_message_components(itx.channel_id, itx.message_id, [])
        if not origin_tag:
            await self.client.send_message(itx.channel_id, _UNTAGGED_OPTIONS_REFUSAL)
            return
        if not choice_text:
            await self.client.send_message(
                itx.channel_id,
                "⚠️ Couldn't read that choice — please type it instead.",
            )
            return
        # Echo the picked option as a quoted line (a button tap can't
        # render as a real user message), then re-dispatch as a fresh turn.
        await self.client.send_message(itx.channel_id, f"> {choice_text}")
        synthetic = InboundMessage(
            channel_type="discord",
            user_id=itx.user_id,
            conversation_id=itx.channel_id,
            text=choice_text,
            thread_id=thread_id or None,
            # A person's own message, re-dispatched (kiro_crew.start_priority).
            person_origin=True,
        )
        # An option label is MODEL-AUTHORED: the agent chose the text of the
        # button, and the press only says which one the user picked. So the
        # payload is turn content, never a command. Interpreting it would let
        # a prompt-injected agent offer `!new` as a choice and have one click
        # discard the conversation, or `!stop` and have it cancel the reply the
        # user was waiting on. Same rule and same reason as the queue drain, which
        # replays with commands off so a queued `!new` reaches the model as
        # literal text instead of executing.
        # UNLIKE the drain, the press still resolves the resumed binding: the
        # buttons sit on the bound session's own reply, so the choice is that
        # session's turn content — its non-empty `origin_tag` is what routes
        # it (see ``handle_message``). Without routing the press ran in the
        # NATIVE DM session — the click answered a question nobody asked
        # there, while the bound session kept waiting (and if the binding died
        # in between, routing now surfaces the Detached refusal instead of a
        # silent native turn). The tag also closes the remaining window: the
        # resolved target must BE the session that posted these buttons.
        await self.handle_message(
            synthetic,
            interpret_commands=False,
            origin_tag=origin_tag,
        )


async def _interaction_actionable(self: DiscordDispatcher, user_id: str, thread_id: str) -> bool:
    """Whether this user could resolve an interaction at this destination."""
    if not self._authorized(user_id):
        return False
    if not thread_id:
        return True
    client = self.client
    if client is None:
        return False
    if thread_id not in self._allowed_threads:
        # A thread `auto_thread` opened before a restart is re-derived from
        # Discord by the transport, which also pushes it into this set.
        transport = self.transport
        if transport is None or not await transport.readmit_own_thread(thread_id, user_id):
            return False
    try:
        return bool(await client.is_thread_channel(thread_id))
    except Exception:
        logger.warning(
            "Discord: thread classification failed for an interaction; "
            "treating the destination as unactionable",
            exc_info=True,
        )
        return False


def _interaction_reply(self: DiscordDispatcher, itx: "DiscordInteraction") -> "ReplyFn":
    """A reply sink that answers the interaction itself, ephemerally.

    Ephemeral because a slash command is invocable from an allow-listed guild
    thread that every member can read, and these replies carry runtime state
    or a login link. Only the FIRST response may use the callback route, which
    is why every handler behind this replies exactly once.
    """

    async def _respond(text: str) -> None:
        assert self.client is not None
        await self.client.respond_interaction(
            itx.interaction_id,
            itx.interaction_token,
            text,
            ephemeral=True,
            destination=itx.channel_id,
        )

    return _respond


async def _on_command_interaction(self: DiscordDispatcher, itx: "DiscordInteraction") -> None:
    """Run a registered slash command.

    Reconstructs the ``!``-form text from the command name and its options so
    the SAME parsers and handlers serve both surfaces; the alternative is a
    second argument grammar per command, which is how the two drift.

    Commands whose reply is not a single message are handled separately:
    ``model`` posts a real channel message because its buttons must be
    editable (an ephemeral response is not), and the session-scoped commands
    route through ``handle_message`` so they keep the resume-binding refusal
    and mid-turn checks that path owns.
    """
    assert self.client is not None
    name = itx.command_name
    thread_id = itx.channel_id if itx.guild_id else ""
    if name in _REPLY_COMMANDS:
        await self._run_reply_command(
            name,
            self._interaction_reply(itx),
            user_id=itx.user_id,
            thread_id=thread_id,
            # Rebuild the text form so the shared argument parsers apply
            # unchanged. Option order does not matter: every command here
            # takes at most one.
            text=" ".join([f"!{name}", *itx.options.values()]).strip(),
        )
        return
    if name == "help":
        await self.client.respond_interaction(
            itx.interaction_id,
            itx.interaction_token,
            build_help_text(),
            ephemeral=True,
            destination=itx.channel_id,
        )
        return
    if name == "model" and thread_id:
        # `model` is the one command whose output CANNOT be ephemeral: its
        # buttons have to live on an editable channel message, and an
        # ephemeral response is not editable. In a guild thread that would
        # publish the account's advertised model list to every member, after
        # the slash surface promised a private reply. Refusing is the honest
        # resolution: `!model` in the thread still works for anyone who
        # accepts that it posts, and a DM has no such tension.
        await self.client.respond_interaction(
            itx.interaction_id,
            itx.interaction_token,
            "🔒 `/model` needs a message it can edit, so its reply cannot be "
            "private here. DM me `/model`, or send `!model` if you are happy "
            "for the list to be visible in this thread.",
            ephemeral=True,
            destination=itx.channel_id,
        )
        return
    # Everything else is session-scoped. Acknowledge the interaction first so
    # Discord does not show "interaction failed" while the turn or command
    # runs, then replay it through the text path, which owns the resume
    # refusal, the governance recheck and the mid-turn ladder.
    await self.client.respond_interaction(
        itx.interaction_id,
        itx.interaction_token,
        f"Running `/{name}`…",
        ephemeral=True,
        destination=itx.channel_id,
    )
    argument = " ".join(itx.options.values()).strip()
    synthetic = InboundMessage(
        channel_type="discord",
        user_id=itx.user_id,
        conversation_id=itx.channel_id,
        text=f"!{name} {argument}".strip(),
        thread_id=thread_id or None,
        # A person's own message, re-dispatched (kiro_crew.start_priority).
        person_origin=True,
    )
    await self.handle_message(synthetic)
