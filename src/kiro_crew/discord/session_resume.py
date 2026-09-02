"""Owner-only Discord session picker and persisted resume binding."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from kiro_crew.messaging.driver import sanitize_channel_replay_text
from kiro_crew.messaging.link import ChannelLink
from kiro_crew.messaging.session_resume import ResumeReleaseError  # noqa: F401  (re-export)
from kiro_crew.messaging.session_resume import (
    PICKER_LIMIT,
    InboundResolution,
    ResumeCopy,
    RoutingDecision,
    SessionChoice,
    SessionResumeController,
)
from kiro_crew.messaging.split import split_markdown_safe
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

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


class _DiscordResumeSurface:
    owner_refusal = (
        "🔒 `!sessions` requires exactly one configured " "`discord.allowed_user_ids` entry."
    )
    choice_owner_refusal = "🔒 Session resume is owner-only."
    choice_expired = "⌛ This session picker expired. Run `!sessions` again."
    choice_missing = "That session is no longer available. Run `!sessions` again."
    choice_expectation_failed = (
        "⚠️ Couldn't save which conversation this channel is linked to, "
        "so the session was NOT resumed. Run `!sessions` to try again."
    )
    choice_claim_lost = (
        "🧵 Another session just connected here. "
        "Run `!unlink`, then `!sessions` to resume this one."
    )
    choice_binding_failed = "⚠️ Couldn't resume that session. Run `!sessions` to try again."

    def __init__(self, client: "DiscordClient", channel_id: str) -> None:
        self.client = client
        self.channel_id = channel_id
        self.expectation_id = channel_id

    def display_safe(self, text: str, max_chars: int) -> str:
        return _safe_discord_text(text, max_chars)

    def no_choices(self, query: str) -> str:
        normalized = " ".join(query.casefold().split())
        if normalized:
            label = _safe_discord_text(" ".join(query.split()), 100).replace("`", "ˋ")
            return (
                f"No dashboard sessions matched `{label}`. Try fewer words, "
                f"or run `!sessions` to see up to {PICKER_LIMIT} recent sessions."
            )
        return "No recent dashboard sessions."

    def picker_heading(self, query: str, total: int) -> str:
        normalized = " ".join(query.casefold().split())
        if normalized:
            label = _safe_discord_text(" ".join(query.split()), 100).replace("`", "ˋ")
            if total > PICKER_LIMIT:
                summary = f"Showing {PICKER_LIMIT} of {total} matching sessions"
            else:
                summary = (
                    f"Showing {total} matching session"
                    f"{'s' if total != 1 else ''} (maximum {PICKER_LIMIT})"
                )
            return (
                "🔎 **Dashboard session search**\n"
                f"{summary} for `{label}`, ranked over titles and message content."
            )
        if total > PICKER_LIMIT:
            summary = f"Showing {PICKER_LIMIT} of {total} most recent dashboard sessions."
        else:
            summary = (
                f"Showing {total} most recent dashboard session"
                f"{'s' if total != 1 else ''} (maximum {PICKER_LIMIT})."
            )
        return f"🧵 **Recent dashboard sessions**\n{summary}"

    def choice_success(self, choice: SessionChoice) -> str:
        return (
            f"🔄 Resumed: {choice.title}\n"
            "Continue here. Send `!unlink` to return to your Discord conversation."
        )

    async def post_picker(
        self,
        heading: str,
        nonce: str,
        choices: tuple[SessionChoice, ...],
    ) -> str:
        message_id = await self.client.send_message(
            self.channel_id,
            f"{heading}\n"
            "Choose one to continue here. Use `!unlink` to return to your "
            "Discord conversation.",
            components=_picker_components(nonce, choices),
        )
        return message_id or ""

    async def settle_picker(self, message_id: str, text: str) -> bool:
        return await self.client.edit_message(
            self.channel_id,
            message_id,
            text,
            components=[],
        )

    async def say(self, text: str) -> None:
        await self.client.send_message(self.channel_id, text)


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
        self._controller = SessionResumeController(
            sessions,
            conv_log,
            channel_type="discord",
            copy=_DISCORD_COPY,
            title_display=_safe_discord_text,
        )
        self.pickers = self._controller.pickers
        self._binder = self._controller.binder
        self._expectations = self._binder.expectations
        self._bind_lock = self._binder.lock

    @property
    def dashboard_state(self) -> object | None:
        return self._controller.dashboard_state

    @dashboard_state.setter
    def dashboard_state(self, state: object | None) -> None:
        self._controller.dashboard_state = state

    def _push_slots(self) -> None:
        self._controller.push_slots()

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
        await self._controller.show_picker(
            _DiscordResumeSurface(client, channel_id),
            caller=user_id,
            picker_owner=_picker_owner(user_id, channel_id),
            is_owner=self.is_owner(user_id),
            query=query,
        )

    async def choose(
        self,
        client: "DiscordClient",
        interaction: "DiscordInteraction",
        custom_id: str,
    ) -> None:
        nonce, index = self._parse_choice(custom_id)
        choice = await self._controller.choose(
            _DiscordResumeSurface(client, interaction.channel_id),
            caller=interaction.user_id,
            picker_owner=_picker_owner(interaction.user_id, interaction.channel_id),
            is_owner=self.is_owner(interaction.user_id),
            message_id=interaction.message_id,
            nonce=nonce,
            index=index,
            link=self.link_for(interaction.channel_id),
        )
        if choice is not None:
            await self._replay(client, interaction.channel_id, choice.key)

    @staticmethod
    def _parse_choice(custom_id: str) -> tuple[str, int]:
        """Parse ``s:<nonce>:<index>`` without resolving the shared picker."""
        parts = custom_id.split(":")
        if len(parts) != 3 or parts[0] != "s" or not parts[2].isdigit():
            return "", -1
        return parts[1], int(parts[2])

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
