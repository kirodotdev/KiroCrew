"""Owner-only Discord session picker and persisted resume binding."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from kiro_crew.messaging.driver import sanitize_channel_replay_text
from kiro_crew.messaging.link import ChannelLink
from kiro_crew.messaging.resume_expectation import ExpectationStoreError, ResumeExpectation
from kiro_crew.messaging.session_resume import ResumeReleaseError  # noqa: F401  (re-export)
from kiro_crew.messaging.session_resume import (
    PICKER_LIMIT,
    TITLE_LIMIT,
    InboundResolution,
    PickerRegistry,
    ResumeCopy,
    RoutingDecision,
    SessionBinder,
    SessionChoice,
    resolve_session_choices,
)
from kiro_crew.messaging.split import split_markdown_safe
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.session_map import ConversationOwnershipConflict

if TYPE_CHECKING:
    from kiro_crew.discord.client import DiscordClient, DiscordInteraction
    from kiro_crew.history import ConversationLog
    from kiro_crew.session import SessionManager

logger = logging.getLogger(__name__)

_REPLAY_MESSAGES = 5
_REPLAY_TEXT_LIMIT = 1900
#: Appended when a replayed message carried more than the preview shows, so the
#: user can tell a shortened transcript entry from a complete one.
_REPLAY_TRUNCATED = "\n… (truncated)"
#: Budget held back per replayed message for the two-character role-icon prefix
#: and the marker above, so a preview plus its scaffolding still fits the limit.
_REPLAY_RESERVE = len(_REPLAY_TRUNCATED) + 2
#: How Discord spells the two commands the shared refusals point at, and the noun for
#: the place the user is in. The ONLY per-channel words in those messages.
_DISCORD_COPY = ResumeCopy(
    sessions_command="!sessions",
    unlink_command="!unlink",
    conversation_noun="Discord conversation",
)


def _picker_owner(user_id: str, channel_id: str) -> str:
    """The identity a Discord picker is scoped to.

    One registry serves the whole gateway, so the channel has to be part of it: without
    that, a press in one channel could resolve a list posted in another.
    """
    return f"{user_id}:{channel_id}"


def _redact_discord_text(text: str) -> str:
    """Redact credentials and exfiltration URLs, then defuse Discord mentions.

    Mention defusing lengthens the text, so it runs before any budgeting: a
    caller measuring the pre-defused string would under-count the payload.
    """
    clean, _ = redact_exfiltration_urls(text or "")
    clean, _ = redact_credentials(clean)
    return clean.replace("@", "@\u200b")


def _safe_discord_text(text: str, max_chars: int) -> str:
    """Redact full text, suppress Discord mentions, then truncate.

    For a short label (a session title, an error string). A Markdown BODY goes
    through :func:`_replay_preview` instead, which shortens without cutting a
    fenced code block in half.
    """
    clean = _redact_discord_text(text)
    if len(clean) <= max_chars:
        return clean
    return clean[: max_chars - 1] + "…"


async def _replay_preview(text: str, limit: int, *, reserve: int) -> str:
    """A fence-safe preview of *text*, marked when it left content behind.

    The replay is a bounded context preview, so a long transcript message is
    still shortened — but a blind character slice can land inside a fenced code
    block, and Discord then renders everything after it as literal backticks
    instead of code. Taking the shared splitter's FIRST chunk shortens at a
    boundary the fence grammar accepts: every chunk but the last is sealed, so
    that chunk closes any block the cut opened and stands on its own.

    The splitter is prefix-stable, so only a bounded prefix is needed to derive
    its first sealed chunk. The bounded split still runs off the asyncio thread:
    pathological delimiter input is finite but CPU-intensive, and transcript
    replay must not pause unrelated Discord traffic while deriving a preview.

    ``reserve`` is the caller's own scaffolding — the role icon, and the marker
    below — held out of the splitter's budget so the assembled message still fits
    ``limit``. A pathological opener can need more fence scaffolding than that
    budget can hold. In that case the marker is safer than a blind API truncation
    that would expose an unterminated fence.
    """
    redacted = _redact_discord_text(text)
    probe = redacted[: max(1, limit) * 2]
    probe_left_content = len(probe) < len(redacted)
    chunks = await asyncio.to_thread(split_markdown_safe, probe, limit, reserve=reserve)
    if not chunks:
        return ""

    first = chunks[0]
    prefix_reserve = max(0, reserve - len(_REPLAY_TRUNCATED))
    if len(chunks) == 1 and not probe_left_content:
        return first if len(first) <= limit - prefix_reserve else _REPLAY_TRUNCATED
    if len(first) > limit - reserve:
        return _REPLAY_TRUNCATED
    return first + _REPLAY_TRUNCATED


def _history_dashboard_key(raw_key: object) -> str | None:
    """Restore the canonical dashboard session key from a JSONL file stem."""
    key = str(raw_key or "")
    if key.startswith("dashboard:"):
        return key
    if key.startswith("dashboard_"):
        while key.startswith("dashboard_"):
            key = key[len("dashboard_") :]
        return f"dashboard:{key}" if key else None
    return None


def _picker_components(nonce: str, choices: tuple[SessionChoice, ...]) -> list[dict]:
    rows: list[dict] = []
    buttons: list[dict] = []
    for index, choice in enumerate(choices):
        buttons.append(
            {
                "type": 2,
                "style": 2,
                "label": f"{index + 1}. {choice.title}"[:80],
                "custom_id": f"s:{nonce}:{index}",
            }
        )
        if len(buttons) == 5:
            rows.append({"type": 1, "components": buttons})
            buttons = []
    if buttons:
        rows.append({"type": 1, "components": buttons})
    return rows


class DiscordSessionResume:
    """Lists dashboard sessions and binds one bidirectionally to Discord."""

    def __init__(
        self,
        sessions: "SessionManager",
        conv_log: "ConversationLog | None",
        allowed_user_ids: set[str],
    ) -> None:
        self.sessions = sessions
        self.conv_log = conv_log
        self.owner_id = next(iter(allowed_user_ids)) if len(allowed_user_ids) == 1 else ""
        self.pickers = PickerRegistry()
        # Set by the gateway after construction (same pattern as ``client`` on
        # the dispatcher). Binding a session from Discord changes what the
        # dashboard must display -- without a push, an already-open dashboard
        # shows no "driven from" chip until unrelated activity happens to
        # refresh slots, which is exactly the window the chip exists to cover.
        self.dashboard_state: object | None = None
        # The shared binder owns the conflict rules, the routing/settlement state
        # machine and the durable record store -- every decision that, made wrongly,
        # routes somebody's transcript into someone else's chat. Discord keeps only its
        # widget and its wording. `_expectations` and `_bind_lock` stay as attributes
        # because they ARE the binder's; two stores or two locks would be two answers.
        self._binder = SessionBinder(sessions, channel_type="discord", copy=_DISCORD_COPY)
        self._binder.title_display = _safe_discord_text
        self._expectations = self._binder.expectations
        self._bind_lock = self._binder.lock

    def _push_slots(self) -> None:
        """Nudge the dashboard so the two-way chip appears/disappears at once."""
        state = self.dashboard_state
        if state is None:
            return
        try:
            push = getattr(state, "push_slots_update", None)
            if callable(push):
                push()
        except Exception:
            logger.debug("discord: slots push after binding change failed", exc_info=True)

    def is_owner(self, user_id: str) -> bool:
        return bool(self.owner_id) and user_id == self.owner_id

    @staticmethod
    def link_for(channel_id: str) -> ChannelLink:
        return ChannelLink(channel_type="discord", channel_id=channel_id)

    def resolve_inbound(self, channel_id: str) -> InboundResolution:
        """Resolve this channel's inbound owner, keeping "none" and "many" apart."""
        return self._binder.resolve_inbound(self.link_for(channel_id))

    def resumed_session(self, channel_id: str) -> str | None:
        """Exactly one inbound-enabled binding, failing closed on duplicates."""
        return self._binder.resumed_session(self.link_for(channel_id))

    async def leave_resumed_session(self, channel_id: str) -> str | None:
        """Drop this channel's resumed binding; raise ResumeReleaseError if not durable."""
        released = await self._binder.release(channel_id, self.link_for(channel_id), self._title_of)
        if released is not None:
            self._push_slots()
        return released

    async def route(self, channel_id: str) -> RoutingDecision:
        """Decide where one inbound message runs, or why it does not run at all."""
        return await self._binder.route(channel_id, self.link_for(channel_id), self._title_of)

    async def settle(self, channel_id: str, decision: RoutingDecision) -> None:
        """Apply a delivered refusal, with version and live-owner guards."""
        await self._binder.settle(channel_id, self.link_for(channel_id), decision)

    def _display(self, expected: ResumeExpectation) -> str:
        """A title safe to post: redacted, mention-neutered, length-capped, because
        a title read back from the store or history is conversation text."""
        if expected.retired:
            return "your own Discord conversation"
        return _safe_discord_text(expected.title or expected.key, TITLE_LIMIT)

    async def _title_of(self, session_key: str) -> str:
        """The stored title for *session_key*, read off-loop, with a stable fallback."""
        title = ""
        if self.conv_log is not None:
            try:
                meta = await asyncio.to_thread(self.conv_log.get_metadata, session_key)
                title = str((meta or {}).get("title") or "")
            except Exception:
                logger.debug("discord resume: title lookup failed", exc_info=True)
        # The picker's fallback for an untitled session, so a bootstrapped record
        # names the conversation the way the user saw it listed.
        return title or session_key.removeprefix("dashboard:")

    async def show_picker(
        self,
        client: "DiscordClient",
        user_id: str,
        channel_id: str,
        query: str = "",
    ) -> None:
        if not self.is_owner(user_id):
            sel().log_api_access(
                caller=user_id,
                operation="discord.sessions_data_access",
                outcome="denied",
                source="discord",
            )
            await client.send_message(
                channel_id,
                "🔒 `!sessions` requires exactly one configured "
                "`discord.allowed_user_ids` entry.",
            )
            return

        if self.conv_log is None:
            await client.send_message(channel_id, "⚠️ Recent sessions are unavailable.")
            return

        normalized_query = " ".join(query.casefold().split())

        try:
            # The eligibility + ranking half is shared (messaging/session_resume.py):
            # same dashboard search, same incognito exclusion, same key normalization.
            eligible, total_choices = await resolve_session_choices(
                self.conv_log, query, _safe_discord_text
            )
        except Exception as exc:
            safe_error = _safe_discord_text(str(exc), 200)
            sel().log_api_access(
                caller=user_id,
                operation="discord.sessions_data_access",
                outcome="error",
                source="discord",
                resources="0 sessions read",
                error=safe_error,
            )
            logger.exception("discord sessions: history listing failed")
            await client.send_message(channel_id, "⚠️ Recent sessions are unavailable.")
            return

        choices = eligible

        sel().log_api_access(
            caller=user_id,
            operation="discord.sessions_data_access",
            outcome="allowed",
            source="discord",
            resources=f"{len(choices)} sessions read",
        )
        if not choices:
            if normalized_query:
                query_label = _safe_discord_text(" ".join(query.split()), 100).replace("`", "ˋ")
                await client.send_message(
                    channel_id,
                    f"No dashboard sessions matched `{query_label}`. Try fewer words, "
                    f"or run `!sessions` to see up to {PICKER_LIMIT} recent sessions.",
                )
            else:
                await client.send_message(channel_id, "No recent dashboard sessions.")
            return

        self.pickers.purge()
        self.pickers.drop_for(_picker_owner(user_id, channel_id))

        nonce = self.pickers.mint()
        frozen = tuple(choices)
        if normalized_query:
            query_label = _safe_discord_text(" ".join(query.split()), 100).replace("`", "ˋ")
            if total_choices > PICKER_LIMIT:
                summary = f"Showing {PICKER_LIMIT} of {total_choices} matching sessions"
            else:
                summary = (
                    f"Showing {total_choices} matching session"
                    f"{'s' if total_choices != 1 else ''} (maximum {PICKER_LIMIT})"
                )
            heading = (
                "🔎 **Dashboard session search**\n"
                f"{summary} for `{query_label}`, ranked over titles and message content."
            )
        else:
            if total_choices > PICKER_LIMIT:
                summary = (
                    f"Showing {PICKER_LIMIT} of {total_choices} most recent dashboard sessions."
                )
            else:
                summary = (
                    f"Showing {total_choices} most recent dashboard session"
                    f"{'s' if total_choices != 1 else ''} (maximum {PICKER_LIMIT})."
                )
            heading = f"🧵 **Recent dashboard sessions**\n{summary}"
        message_id = await client.send_message(
            channel_id,
            f"{heading}\n"
            "Choose one to continue here. Use `!unlink` to return to your "
            "Discord conversation.",
            components=_picker_components(nonce, frozen),
        )
        if message_id:
            # Owner is (user, channel): one registry serves the whole gateway, so the
            # channel has to be part of the identity or a press in one channel could
            # resolve a list posted in another.
            self.pickers.register(nonce, _picker_owner(user_id, channel_id), message_id, frozen)

    def _binding_conflict(
        self,
        key: str,
        title: str,
        target: ChannelLink,
    ) -> str | None:
        """Return a refusal message when *key* must not be bound to *target*.

        Evaluated twice per resume: once for fast feedback, and again
        immediately before the write. The second call is load-bearing.
        ``_bind_lock`` only serialises Discord's own picker, while a dashboard
        mirror link (``chat_mirror``) or another channel's ``!link`` takes no
        such lock -- either can bind this session, or this conversation, during
        the awaited header edit. Without the re-check those newer bindings are
        silently overwritten and the conflict rules below are bypassed.
        """
        existing = self.sessions.get_mirror_link(key)
        if existing is not None and existing != target:
            return (
                f"🧵 This session is already active on "
                f"{existing.channel_type.title()}. Unlink it there first."
            )

        inbound = self.sessions.find_mirror_sessions(target, inbound_only=True)
        if key in inbound:
            return f"🧵 Already active here: {title}"

        occupants = [
            candidate
            for candidate in self.sessions.find_mirror_sessions(target)
            if candidate != key
        ]
        if occupants:
            # `!unlink` clears every binding at this location by value —
            # resumed sessions and outbound dashboard mirrors alike — so one
            # instruction is always followable from inside the conversation.
            return (
                "⚠️ This Discord conversation is already attached to another "
                "session. Run `!unlink` first."
            )
        return None

    async def choose(
        self,
        client: "DiscordClient",
        interaction: "DiscordInteraction",
        custom_id: str,
    ) -> None:
        if not self.is_owner(interaction.user_id):
            sel().log_api_access(
                caller=interaction.user_id,
                operation="discord.session_resume_choice",
                outcome="denied",
                source="discord",
            )
            await client.edit_message(
                interaction.channel_id,
                interaction.message_id,
                "🔒 Session resume is owner-only.",
                components=[],
            )
            return

        choice = self._take_choice(interaction, custom_id)
        if choice is None:
            await client.edit_message(
                interaction.channel_id,
                interaction.message_id,
                "⌛ This session picker expired. Run `!sessions` again.",
                components=[],
            )
            return

        if self.conv_log is None or not await asyncio.to_thread(
            self.conv_log.has_log,
            choice.key,
        ):
            await client.edit_message(
                interaction.channel_id,
                interaction.message_id,
                "That session is no longer available. Run `!sessions` again.",
                components=[],
            )
            return

        target = self.link_for(interaction.channel_id)
        async with self._bind_lock:
            conflict = self._binding_conflict(choice.key, choice.title, target)
            if conflict is not None:
                await client.edit_message(
                    interaction.channel_id,
                    interaction.message_id,
                    conflict,
                    components=[],
                )
                return

            try:
                # Record BEFORE the success banner and the binding, all under the bind
                # lock: once Discord shows "Resumed", only durable evidence survives a
                # crash, and a rollback is unsafe (no map revision; the racing dashboard
                # rebind skips the lock). A lost banner or bind fails toward one notice.
                await self._expectations.record(interaction.channel_id, choice.key, choice.title)
            except ExpectationStoreError:
                logger.warning(
                    "discord resume: the pick of %s did not take effect",
                    choice.key,
                    exc_info=True,
                )
                await client.edit_message(
                    interaction.channel_id,
                    interaction.message_id,
                    "⚠️ Couldn't save which conversation this channel is linked to, "
                    "so the session was NOT resumed. Run `!sessions` to try again.",
                    components=[],
                )
                return
            header_ok = await client.edit_message(
                interaction.channel_id,
                interaction.message_id,
                f"🔄 Resumed: {choice.title}\n"
                "Continue here. Send `!unlink` to return to your Discord conversation.",
                components=[],
            )
            if not header_ok:
                return

            # The edit above awaited a Discord round-trip. Re-check before
            # writing: a dashboard mirror or another channel's !link may have
            # claimed this session or this conversation in that window.
            conflict = self._binding_conflict(choice.key, choice.title, target)
            if conflict is not None:
                await client.edit_message(
                    interaction.channel_id,
                    interaction.message_id,
                    conflict,
                    components=[],
                )
                return

            try:
                self.sessions.set_mirror_link(
                    choice.key,
                    target,
                    accepts_inbound=True,
                )
                self._push_slots()
            except ConversationOwnershipConflict:
                # `_binding_conflict` already checked, twice — but it and the
                # dashboard connect endpoint evaluate under different locks, so
                # this precheck can lose the race. The atomic claim inside
                # `set_mirror_link` is what catches it. This is an ordinary
                # conflict, not a fault: say so in the precheck's own words
                # instead of the generic failure text below, which would send the
                # user off to retry a command that is working.
                logger.debug("discord resume: lost the claim race for this conversation")
                await client.edit_message(
                    interaction.channel_id,
                    interaction.message_id,
                    "🧵 Another session just connected here. "
                    "Run `!unlink`, then `!sessions` to resume this one.",
                    components=[],
                )
                return
            except Exception:
                logger.exception("discord resume: failed to persist binding")
                await client.edit_message(
                    interaction.channel_id,
                    interaction.message_id,
                    "⚠️ Couldn't resume that session. Run `!sessions` to try again.",
                    components=[],
                )
                return

        sel().log_api_access(
            caller=interaction.user_id,
            operation="discord.session_resume",
            outcome="allowed",
            source="discord",
            resources=choice.key,
        )
        await self._replay(client, interaction.channel_id, choice.key)

    def _take_choice(
        self,
        interaction: "DiscordInteraction",
        custom_id: str,
    ) -> SessionChoice | None:
        """Resolve ``s:<nonce>:<index>`` against a live picker, or None."""
        parts = custom_id.split(":")
        if len(parts) != 3 or parts[0] != "s" or not parts[2].isdigit():
            return None
        return self.pickers.take(
            parts[1],
            int(parts[2]),
            _picker_owner(interaction.user_id, interaction.channel_id),
            interaction.message_id,
        )

    async def _replay(
        self,
        client: "DiscordClient",
        channel_id: str,
        session_key: str,
    ) -> None:
        if self.conv_log is None:
            return
        try:
            messages = await asyncio.to_thread(
                self.conv_log.recent,
                session_key,
                _REPLAY_MESSAGES,
                {"user", "assistant"},
            )
        except Exception:
            logger.exception("discord resume: failed to read transcript context")
            return

        for message in messages:
            role = message.get("role", "")
            raw_content = str(message.get("content") or "")
            if role == "assistant":
                raw_content = sanitize_channel_replay_text(raw_content)
            content = await _replay_preview(
                raw_content, _REPLAY_TEXT_LIMIT, reserve=_REPLAY_RESERVE
            )
            if role not in {"user", "assistant"} or not content:
                continue
            icon = "🧑" if role == "user" else "🤖"
            try:
                # The icon gets its OWN line: on the body's first line it would
                # sit in front of a fence opener, which must start the line, so a
                # replayed code block would render as literal backticks however
                # carefully the body was split.
                await client.send_message(channel_id, f"{icon}\n{content}")
            except Exception:
                logger.debug("discord resume: context replay failed", exc_info=True)
