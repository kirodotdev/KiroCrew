"""Telegram's session picker and inbound binding adapter over the shared resume core."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from kiro_crew.messaging.link import ChannelLink, parse_session_key
from kiro_crew.messaging.renderer import display_safe
from kiro_crew.messaging.session_resume import ResumeReleaseError  # noqa: F401  (re-export)
from kiro_crew.messaging.session_resume import (
    PICKER_LIMIT,
    InboundResolution,
    ResumeCopy,
    RoutingDecision,
    SessionChoice,
    SessionResumeController,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.history import ConversationLog
    from kiro_crew.session import SessionManager
    from kiro_crew.telegram.client import TelegramCallback, TelegramClient

logger = logging.getLogger(__name__)

_TELEGRAM_COPY = ResumeCopy(
    sessions_command="/session",
    unlink_command="/unlink",
    conversation_noun="Telegram conversation",
)
_CALLBACK_DATA_MAX_BYTES = 64
_BUTTON_LABEL_MAX_CHARS = 80
_QUERY_LABEL_MAX_CHARS = 100
_ROUTE_OWNER_REFUSAL = (
    "🔒 This Telegram chat cannot resume a dashboard session because session "
    "resume requires exactly one configured operator in a private DM. Your "
    "message was NOT processed."
)


def _safe_telegram_text(text: str, max_chars: int) -> str:
    """Redact the complete rendered string before applying its display budget."""
    return display_safe(text)[:max_chars]


def _picker_keyboard(nonce: str, choices: tuple[SessionChoice, ...]) -> dict:
    """One Telegram button per row, carrying only an ASCII nonce and index."""
    rows: list[list[dict[str, str]]] = []
    for index, choice in enumerate(choices):
        callback_data = f"s:{nonce}:{index}"
        if (
            not callback_data.isascii()
            or len(callback_data.encode("ascii")) > _CALLBACK_DATA_MAX_BYTES
        ):
            raise ValueError("session picker callback_data exceeds Telegram's limit")
        label = _safe_telegram_text(f"{index + 1}. {choice.title}", _BUTTON_LABEL_MAX_CHARS)
        rows.append([{"text": label, "callback_data": callback_data}])
    return {"inline_keyboard": rows}


def _picker_owner(user_id: int, chat_id: int, thread_id: int | None) -> str:
    return f"{user_id}:{TelegramSessionResume.expectation_id(chat_id, thread_id)}"


class _TelegramResumeSurface:
    owner_refusal = (
        "🔒 /session can resume dashboard conversations only from the single "
        "operator's private chat. Configure exactly one telegram.allowed_user_ids entry."
    )
    choice_owner_refusal = "🔒 Session resume is owner-only."
    choice_expired = "⌛ This session picker expired. Run /session again."
    choice_missing = "That session is no longer available. Run /session again."
    choice_expectation_failed = (
        "⚠️ Couldn't save which conversation this chat is linked to, so the session "
        "was NOT resumed. Run /session to try again."
    )
    choice_claim_lost = (
        "🧵 Another session just connected here. Run /session again after freeing "
        "the current binding."
    )
    choice_binding_failed = "⚠️ Couldn't resume that session. Run /session to try again."

    def __init__(
        self,
        client: "TelegramClient",
        chat_id: int,
        thread_id: int | None,
    ) -> None:
        self.client = client
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.expectation_id = TelegramSessionResume.expectation_id(chat_id, thread_id)

    def display_safe(self, text: str, max_chars: int) -> str:
        return _safe_telegram_text(text, max_chars)

    def no_choices(self, query: str) -> str:
        normalized = " ".join(query.casefold().split())
        if normalized:
            label = _safe_telegram_text(" ".join(query.split()), _QUERY_LABEL_MAX_CHARS)
            return (
                f"No dashboard sessions matched “{label}”. Try fewer words, or run "
                f"/session to see up to {PICKER_LIMIT} recent sessions."
            )
        return "No recent dashboard sessions."

    def picker_heading(self, query: str, total: int) -> str:
        shown = min(total, PICKER_LIMIT)
        normalized = " ".join(query.casefold().split())
        if normalized:
            label = _safe_telegram_text(" ".join(query.split()), _QUERY_LABEL_MAX_CHARS)
            if total > PICKER_LIMIT:
                summary = f"Showing {PICKER_LIMIT} of {total} matching sessions"
            else:
                summary = f"Showing {shown} matching session{'s' if shown != 1 else ''}"
            return (
                f"🔎 Dashboard session search\n{summary} for “{label}”, ranked over "
                "titles and message content."
            )
        if total > PICKER_LIMIT:
            summary = f"Showing {PICKER_LIMIT} of {total} most recent dashboard sessions."
        else:
            summary = (
                f"Showing {shown} most recent dashboard session" f"{'s' if shown != 1 else ''}."
            )
        return f"🧵 Recent dashboard sessions\n{summary}"

    def choice_success(self, choice: SessionChoice) -> str:
        return (
            f"🔄 Resumed: {choice.title}\n"
            "Ordinary messages here now continue that dashboard conversation."
        )

    async def post_picker(
        self,
        heading: str,
        nonce: str,
        choices: tuple[SessionChoice, ...],
    ) -> str:
        try:
            keyboard = _picker_keyboard(nonce, choices)
        except ValueError:
            logger.error("Telegram resume: picker callback payload exceeded the platform cap")
            await self.say("⚠️ Couldn't build the session picker. Run /session again.")
            return ""
        message_id = await self.client.send_message(
            self.chat_id,
            f"{heading}\nChoose one to continue here.",
            reply_markup=keyboard,
            message_thread_id=self.thread_id,
        )
        return str(message_id) if message_id is not None else ""

    async def settle_picker(self, message_id: str, text: str) -> bool:
        try:
            numeric_id = int(message_id)
        except (TypeError, ValueError):
            return False
        return bool(
            await self.client.edit_message(
                self.chat_id,
                numeric_id,
                text,
                reply_markup={"inline_keyboard": []},
            )
        )

    async def say(self, text: str) -> None:
        await self.client.send_message(
            self.chat_id,
            text,
            message_thread_id=self.thread_id,
        )


class TelegramSessionResume:
    """List dashboard sessions and bind one to a Telegram chat or Topic."""

    def __init__(
        self,
        sessions: "SessionManager",
        conv_log: "ConversationLog | None",
        allowed_user_ids: set[int],
    ) -> None:
        self.sessions = sessions
        self.conv_log = conv_log
        self.owner_id = next(iter(allowed_user_ids)) if len(allowed_user_ids) == 1 else 0
        self._controller = SessionResumeController(
            sessions,
            conv_log,
            channel_type="telegram",
            copy=_TELEGRAM_COPY,
            title_display=_safe_telegram_text,
        )
        self.pickers = self._controller.pickers
        self._binder = self._controller.binder
        self.expectations = self._binder.expectations

    @property
    def dashboard_state(self) -> object | None:
        return self._controller.dashboard_state

    @dashboard_state.setter
    def dashboard_state(self, state: object | None) -> None:
        self._controller.dashboard_state = state

    @staticmethod
    def expectation_id(chat_id: int, thread_id: int | None) -> str:
        if thread_id is not None:
            return f"topic:{chat_id}:{thread_id}"
        return f"chat:{chat_id}"

    @staticmethod
    def link_for(chat_id: int, thread_id: int | None) -> ChannelLink:
        return ChannelLink(
            channel_type="telegram",
            channel_id=str(chat_id),
            thread_id=str(thread_id) if thread_id is not None else None,
        )

    def is_owner(self, user_id: int, chat_id: int, chat_type: str) -> bool:
        return bool(self.owner_id) and (
            chat_type == "private" and user_id == self.owner_id and chat_id == user_id
        )

    def resolve_inbound(self, chat_id: int, thread_id: int | None) -> InboundResolution:
        return self._binder.resolve_inbound(self.link_for(chat_id, thread_id))

    def resumed_session(self, chat_id: int, thread_id: int | None) -> str | None:
        return self._binder.resumed_session(self.link_for(chat_id, thread_id))

    async def route(
        self,
        user_id: int,
        chat_id: int,
        chat_type: str,
        thread_id: int | None,
    ) -> RoutingDecision:
        link = self.link_for(chat_id, thread_id)
        resolution = self._binder.resolve_inbound(link)
        if (resolution.key is not None or resolution.ambiguous) and not self.is_owner(
            user_id, chat_id, chat_type
        ):
            return RoutingDecision(refusal=_ROUTE_OWNER_REFUSAL, observed=resolution)
        return await self._binder.route(
            self.expectation_id(chat_id, thread_id),
            link,
            self._title_of,
        )

    async def settle(
        self,
        chat_id: int,
        thread_id: int | None,
        decision: RoutingDecision,
    ) -> None:
        await self._binder.settle(
            self.expectation_id(chat_id, thread_id),
            self.link_for(chat_id, thread_id),
            decision,
        )

    async def leave_resumed_session(self, chat_id: int, thread_id: int | None) -> str | None:
        released = await self._binder.release(
            self.expectation_id(chat_id, thread_id),
            self.link_for(chat_id, thread_id),
            self._title_of,
        )
        if released is not None:
            self._controller.push_slots()
        return released

    async def _title_of(self, session_key: str) -> str:
        title = ""
        if self.conv_log is not None:
            try:
                meta = await asyncio.to_thread(self.conv_log.get_metadata, session_key)
                title = str((meta or {}).get("title") or "")
            except Exception:
                logger.debug("Telegram resume: title lookup failed", exc_info=True)
        return title or session_key.removeprefix("dashboard:")

    def _native_origin_keys(self, link: ChannelLink) -> frozenset[str]:
        """Outbound occupants that are native generations of this Telegram DM.

        A dashboard session explicitly mirrored to the same DM is not an origin
        mirror and must remain protected. Legacy origin rows are resolved through
        ``channel_key_for_stem`` before applying the same canonical-key test.
        """
        keys: set[str] = set()
        stem_resolver = getattr(self.sessions, "channel_key_for_stem", None)
        for stored_key in self.sessions.find_mirror_sessions(link):
            candidate = stored_key
            parsed = parse_session_key(candidate)
            if parsed is None and stored_key.startswith("dashboard:") and callable(stem_resolver):
                candidate = str(stem_resolver(stored_key.removeprefix("dashboard:")) or "")
                parsed = parse_session_key(candidate)
            if (
                parsed is not None
                and parsed.surface == "telegram"
                and parsed.chat_type == "direct"
                and parsed.scope == (link.channel_id,)
            ):
                keys.add(stored_key)
        return frozenset(keys)

    async def show_picker(
        self,
        client: "TelegramClient",
        user_id: int,
        chat_id: int,
        chat_type: str,
        thread_id: int | None,
        query: str = "",
    ) -> None:
        await self._controller.show_picker(
            _TelegramResumeSurface(client, chat_id, thread_id),
            caller=str(user_id) or "unknown",
            picker_owner=_picker_owner(user_id, chat_id, thread_id),
            is_owner=self.is_owner(user_id, chat_id, chat_type),
            query=query,
        )

    async def choose(self, client: "TelegramClient", callback: "TelegramCallback") -> None:
        nonce, index = self._parse_choice(callback.data)
        link = self.link_for(callback.chat_id, callback.message_thread_id)
        await self._controller.choose(
            _TelegramResumeSurface(client, callback.chat_id, callback.message_thread_id),
            caller=str(callback.user_id) or "unknown",
            picker_owner=_picker_owner(
                callback.user_id,
                callback.chat_id,
                callback.message_thread_id,
            ),
            is_owner=self.is_owner(callback.user_id, callback.chat_id, callback.chat_type),
            message_id=str(callback.message_id),
            nonce=nonce,
            index=index,
            link=link,
            replace_outbound_keys=self._native_origin_keys(link),
        )

    @staticmethod
    def _parse_choice(data: str) -> tuple[str, int]:
        parts = data.split(":")
        if len(parts) != 3 or parts[0] != "s" or not parts[2].isdigit():
            return "", -1
        return parts[1], int(parts[2])
