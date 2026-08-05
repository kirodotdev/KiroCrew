"""Owner-only Discord session picker and persisted resume binding."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from kiro_crew.history import INCOGNITO_MEMORY_MODES
from kiro_crew.messaging.driver import sanitize_channel_replay_text
from kiro_crew.messaging.link import ChannelLink
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

if TYPE_CHECKING:
    from kiro_crew.discord.client import DiscordClient, DiscordInteraction
    from kiro_crew.history import ConversationLog
    from kiro_crew.session import SessionManager

logger = logging.getLogger(__name__)

_PICKER_LIMIT = 10
#: Rows pulled from history before incognito/keying filters. Larger than
#: _PICKER_LIMIT so filtered-out rows cannot starve the 10 button slots.
_SEARCH_FETCH_LIMIT = 50
_PICKER_TTL_SECS = 300
_PICKER_REGISTRY_MAX = 100
_REPLAY_MESSAGES = 5
_REPLAY_TEXT_LIMIT = 1900


@dataclass(frozen=True)
class _SessionChoice:
    key: str
    title: str


@dataclass
class _SessionPicker:
    user_id: str
    channel_id: str
    message_id: str
    created_at: float
    choices: tuple[_SessionChoice, ...]


def _safe_discord_text(text: str, max_chars: int) -> str:
    """Redact full text, suppress Discord mentions, then truncate."""
    clean, _ = redact_exfiltration_urls(text or "")
    clean, _ = redact_credentials(clean)
    clean = clean.replace("@", "@\u200b")
    if len(clean) <= max_chars:
        return clean
    return clean[: max_chars - 1] + "…"


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


def _picker_components(nonce: str, choices: tuple[_SessionChoice, ...]) -> list[dict]:
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
        self.pickers: dict[str, _SessionPicker] = {}
        # Set by the gateway after construction (same pattern as ``client`` on
        # the dispatcher). Binding a session from Discord changes what the
        # dashboard must display -- without a push, an already-open dashboard
        # shows no "driven from" chip until unrelated activity happens to
        # refresh slots, which is exactly the window the chip exists to cover.
        self.dashboard_state: object | None = None
        self._bind_lock = asyncio.Lock()

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

    def resumed_session(self, channel_id: str) -> str | None:
        """Resolve exactly one inbound-enabled binding, failing closed on duplicates."""
        matches = self.sessions.find_mirror_sessions(
            self.link_for(channel_id),
            inbound_only=True,
        )
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            logger.error(
                "discord resume: ambiguous inbound bindings for channel %s; routing denied",
                channel_id,
            )
        return None

    def leave_resumed_session(self, channel_id: str) -> str | None:
        key = self.resumed_session(channel_id)
        if key is not None:
            # Free the LOCATION, not just the resume binding: the dashboard's
            # mirror-link endpoint performs no occupancy check, so an outbound
            # mirror can be laid onto a conversation a resumed session already
            # holds. This early path bypasses the dispatcher's own sweep —
            # clearing only *key* here would leave that mirror occupying the
            # location and reproduce the "already attached" refusal after an
            # apparently successful unlink.
            cleared = self.sessions.clear_mirror_links_at(self.link_for(channel_id))
            logger.info(
                "discord: released resumed session %s (cleared bindings: %s)",
                key,
                ", ".join(cleared) or "none",
            )
            self._push_slots()
        return key

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
            if normalized_query:
                # Reuse the SAME search the dashboard uses
                # (KiroCrewHistory.search_sessions): it matches message CONTENT
                # with a title boost and length-normalised ranking, so a phrase
                # the user remembers from the CONVERSATION finds the session.
                # A title-only filter here would miss exactly that case -- a
                # natural-language phrase rather than a title -- and would be a
                # second search implementation free to drift from the
                # dashboard's ranking. Fetch more than the picker shows
                # so incognito filtering cannot starve the button slots.
                rows = await asyncio.to_thread(
                    self.conv_log.search_sessions, query, _SEARCH_FETCH_LIMIT
                )
                if not rows and len(normalized_query.split()) > 1:
                    # search_sessions matches the query as ONE phrase
                    # (``needle = query.casefold()``), so out-of-order words miss:
                    # "specific link" does not match "Link to a Specific Session".
                    # Fall back to an all-words TITLE match so a remembered-but-
                    # reordered title still resolves. Deliberately last-resort and
                    # only on zero hits, so the shared search stays authoritative
                    # and we are not running two rankers in parallel. The proper
                    # home for multi-word support is search_sessions itself, which
                    # would fix the dashboard too -- tracked as a follow-up.
                    listed = await asyncio.to_thread(self.conv_log.list_sessions)
                    words = normalized_query.split()
                    rows = [
                        row
                        for row in listed
                        if isinstance(row, dict)
                        and all(
                            word in " ".join(str(row.get("title") or "").casefold().split())
                            for word in words
                        )
                    ]
            else:
                rows = await asyncio.to_thread(self.conv_log.list_sessions)
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

        eligible: list[_SessionChoice] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("memory_mode", "persistent")).lower() in INCOGNITO_MEMORY_MODES:
                continue
            key = _history_dashboard_key(row.get("key"))
            if key is None:
                continue
            raw_title = str(row.get("title") or key.removeprefix("dashboard:"))
            title = _safe_discord_text(" ".join(raw_title.split()), 76) or "Untitled session"
            eligible.append(_SessionChoice(key=key, title=title))

        # Order is already meaningful: search_sessions returns best-scored first,
        # list_sessions returns newest first. Do not re-sort.
        total_choices = len(eligible)
        choices = eligible[:_PICKER_LIMIT]

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
                    f"or run `!sessions` to see up to {_PICKER_LIMIT} recent sessions.",
                )
            else:
                await client.send_message(channel_id, "No recent dashboard sessions.")
            return

        self._purge_pickers()
        for nonce, picker in list(self.pickers.items()):
            if picker.user_id == user_id and picker.channel_id == channel_id:
                self.pickers.pop(nonce, None)

        nonce = secrets.token_hex(8)
        frozen = tuple(choices)
        if normalized_query:
            query_label = _safe_discord_text(" ".join(query.split()), 100).replace("`", "ˋ")
            if total_choices > _PICKER_LIMIT:
                summary = f"Showing {_PICKER_LIMIT} of {total_choices} matching sessions"
            else:
                summary = (
                    f"Showing {total_choices} matching session"
                    f"{'s' if total_choices != 1 else ''} (maximum {_PICKER_LIMIT})"
                )
            heading = (
                "🔎 **Dashboard session search**\n"
                f"{summary} for `{query_label}`, ranked over titles and message content."
            )
        else:
            if total_choices > _PICKER_LIMIT:
                summary = f"Showing {_PICKER_LIMIT} of {total_choices} most recent dashboard sessions."
            else:
                summary = (
                    f"Showing {total_choices} most recent dashboard session"
                    f"{'s' if total_choices != 1 else ''} (maximum {_PICKER_LIMIT})."
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
            self.pickers[nonce] = _SessionPicker(
                user_id=user_id,
                channel_id=channel_id,
                message_id=message_id,
                created_at=time.monotonic(),
                choices=frozen,
            )

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

    def _purge_pickers(self) -> None:
        cutoff = time.monotonic() - _PICKER_TTL_SECS
        for nonce, picker in list(self.pickers.items()):
            if picker.created_at < cutoff:
                self.pickers.pop(nonce, None)
        if len(self.pickers) >= _PICKER_REGISTRY_MAX:
            oldest = sorted(self.pickers, key=lambda key: self.pickers[key].created_at)
            for nonce in oldest[: len(self.pickers) - _PICKER_REGISTRY_MAX + 1]:
                self.pickers.pop(nonce, None)

    def _take_choice(
        self,
        interaction: "DiscordInteraction",
        custom_id: str,
    ) -> _SessionChoice | None:
        self._purge_pickers()
        parts = custom_id.split(":")
        if len(parts) != 3 or parts[0] != "s" or not parts[2].isdigit():
            return None
        nonce, index = parts[1], int(parts[2])
        picker = self.pickers.get(nonce)
        if picker is None:
            return None
        if (
            picker.user_id != interaction.user_id
            or picker.channel_id != interaction.channel_id
            or picker.message_id != interaction.message_id
            or index >= len(picker.choices)
        ):
            return None
        self.pickers.pop(nonce, None)
        return picker.choices[index]

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
            content = _safe_discord_text(raw_content, _REPLAY_TEXT_LIMIT)
            if role not in {"user", "assistant"} or not content:
                continue
            icon = "🧑" if role == "user" else "🤖"
            try:
                await client.send_message(channel_id, f"{icon} {content}")
            except Exception:
                logger.debug("discord resume: context replay failed", exc_info=True)
