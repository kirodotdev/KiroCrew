"""Teams' half of session resume: an Adaptive Card picker over the shared core.

Every decision — which sessions are offerable, the picker nonce/TTL/owner scoping, the
binding-conflict rules, and the inbound routing + settlement state machine — lives in
:mod:`kiro_crew.messaging.session_resume`, shared with Discord. This module is only what
is Teams-shaped:

* the picker is an Adaptive Card whose ``Action.Submit`` returns as an ordinary
  ``message`` activity (the same mechanism tool approval and ``[OPTIONS:]`` chips use,
  so it needs no ``invoke`` handler the fast-ack ingress has no room for);
* Teams' own display redaction, and its ``/sessions`` / ``/unlink`` spellings;
* the conversation address, bound at construction so no id type reaches the core.

**Owner-only, and stricter than Discord's rule for a reason.** Discord requires exactly
one configured user id. Teams' allow-list routinely holds several people, and a
dashboard session is the OPERATOR's whole working transcript — so this is gated on the
allow-list having exactly one entry too. With more than one, listing is refused rather
than letting any allow-listed member enumerate and attach to the operator's chats.
"""

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
from kiro_crew.teams.cards import resolved_card, session_picker_card
from kiro_crew.teams.client import TEAMS_MAX_TEXT, TeamsSendError
from kiro_crew.teams.renderer import _display_safe

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.history import ConversationLog
    from kiro_crew.session import SessionManager
    from kiro_crew.teams.client import TeamsClient

logger = logging.getLogger(__name__)

#: How Teams spells the commands the shared refusals point at. "conversation" (not
#: "channel") because a Teams personal chat is not a channel.
_TEAMS_COPY = ResumeCopy(sessions_command="/sessions", unlink_command="/unlink")

#: Transcript entries replayed after a resume, so the user sees where they left off.
_REPLAY_MESSAGES = 5
#: Per-entry budget. Well under the Teams text cap so the icon line and the truncation
#: marker still fit, and small enough that five entries do not bury the picker.
_REPLAY_TEXT_LIMIT = 1800
_REPLAY_TRUNCATED = "\n… (truncated)"


def _safe_teams_text(text: str, max_chars: int) -> str:
    """Redact for Teams' rendering, then budget. Redaction FIRST, always.

    Truncating first can split a credential into a form the scanner no longer matches,
    which is the one ordering that turns a display-safety helper into a leak.
    """
    return _display_safe(text)[:max_chars]


class _TeamsResumeSurface:
    owner_refusal = (
        "🔒 `/sessions` requires exactly one configured `teams.allowed_emails` "
        "entry — a dashboard session is the operator's own transcript, so it is "
        "not listable on a shared allow-list."
    )
    choice_owner_refusal = "session resume is owner-only"
    choice_expired = "this list expired — run `/sessions` again"
    choice_missing = "that session is no longer available"
    choice_expectation_failed = (
        "couldn't save which session this conversation is linked to, so it "
        "was NOT resumed — run `/sessions` to try again"
    )
    choice_claim_lost = (
        "another session just connected here — run `/unlink`, then "
        "`/sessions` to resume this one"
    )
    choice_binding_failed = "couldn't resume that session — run `/sessions` to try again"

    def __init__(
        self,
        client: "TeamsClient",
        conversation_id: str,
        service_url: str,
    ) -> None:
        self.client = client
        self.conversation_id = conversation_id
        self.expectation_id = conversation_id
        self.service_url = service_url

    def display_safe(self, text: str, max_chars: int) -> str:
        return _safe_teams_text(text, max_chars)

    def no_choices(self, query: str) -> str:
        normalized = " ".join(query.casefold().split())
        if normalized:
            label = _safe_teams_text(" ".join(query.split()), 100)
            return (
                f"No dashboard sessions matched “{label}”. Try fewer words, or run "
                f"`/sessions` to see up to {PICKER_LIMIT} recent sessions."
            )
        return "No recent dashboard sessions."

    def picker_heading(self, query: str, total: int) -> str:
        return _picker_heading(query, total, " ".join(query.casefold().split()))

    def choice_success(self, choice: SessionChoice) -> str:
        return f"resumed “{choice.title}” — `/unlink` to come back"

    async def post_picker(
        self,
        heading: str,
        nonce: str,
        choices: tuple[SessionChoice, ...],
    ) -> str:
        try:
            message_id = await self.client.send_card(
                self.conversation_id,
                session_picker_card(prompt=heading, choices=choices, nonce=nonce),
                self.service_url,
            )
            return message_id or ""
        except TeamsSendError:
            logger.warning("Teams resume: picker card delivery failed", exc_info=True)
            await self.say("⚠️ Couldn't show the session list. Try `/sessions` again.")
            return ""

    async def settle_picker(self, message_id: str, text: str) -> bool:
        try:
            return bool(
                await self.client.update_card(
                    self.conversation_id,
                    message_id,
                    resolved_card(title="Sessions", outcome=text),
                    self.service_url,
                )
            )
        except TeamsSendError:
            logger.debug("Teams resume: picker settle failed", exc_info=True)
            return False

    async def say(self, text: str) -> None:
        try:
            await self.client.send_message(self.conversation_id, text, self.service_url)
        except TeamsSendError:
            logger.warning("Teams resume: notice delivery failed", exc_info=True)


class TeamsSessionResume:
    """Lists dashboard sessions and binds one bidirectionally to a Teams chat."""

    def __init__(
        self,
        sessions: "SessionManager",
        conv_log: "ConversationLog | None",
        allowed_emails: set[str],
    ) -> None:
        self.sessions = sessions
        self.conv_log = conv_log
        # Owner-only: exactly one allow-listed identity, for the reason in the module
        # docstring. Empty when the list holds none or several, and `is_owner` then
        # refuses everyone.
        self.owner_id = next(iter(allowed_emails)) if len(allowed_emails) == 1 else ""
        self._controller = SessionResumeController(
            sessions,
            conv_log,
            channel_type="teams",
            copy=_TEAMS_COPY,
            title_display=_safe_teams_text,
        )
        self.pickers = self._controller.pickers
        self._binder = self._controller.binder
        self._expectations = self._binder.expectations

    @property
    def dashboard_state(self) -> object | None:
        return self._controller.dashboard_state

    @dashboard_state.setter
    def dashboard_state(self, state: object | None) -> None:
        self._controller.dashboard_state = state

    # ── identity + addressing ─────────────────────────────────────────────
    def is_owner(self, identity: str) -> bool:
        """Case-insensitively, matching how the allow-list itself compares.

        Azure AD hands the UPN back in whatever case the directory holds, and
        ``teams.allowed_emails`` is lowercased at load -- so an exact compare here
        would refuse the very identity the allow-list just admitted.
        """
        return bool(self.owner_id) and identity.lower() == self.owner_id.lower()

    @staticmethod
    def link_for(conversation_id: str) -> ChannelLink:
        return ChannelLink(channel_type="teams", channel_id=conversation_id)

    # ── shared-core delegation ────────────────────────────────────────────
    def resolve_inbound(self, conversation_id: str) -> InboundResolution:
        return self._binder.resolve_inbound(self.link_for(conversation_id))

    def resumed_session(self, conversation_id: str) -> str | None:
        return self._binder.resumed_session(self.link_for(conversation_id))

    async def route(self, conversation_id: str) -> RoutingDecision:
        """Where one inbound message runs, or the refusal that stops it running."""
        return await self._binder.route(
            conversation_id, self.link_for(conversation_id), self._title_of
        )

    async def settle(self, conversation_id: str, decision: RoutingDecision) -> None:
        await self._binder.settle(conversation_id, self.link_for(conversation_id), decision)

    async def leave_resumed_session(self, conversation_id: str) -> str | None:
        """Drop this conversation's resumed binding, or raise ResumeReleaseError."""
        released = await self._binder.release(
            conversation_id, self.link_for(conversation_id), self._title_of
        )
        if released is not None:
            self._controller.push_slots()
        return released

    async def _title_of(self, session_key: str) -> str:
        """The stored title for *session_key*, read off-loop, with a stable fallback."""
        title = ""
        if self.conv_log is not None:
            try:
                meta = await asyncio.to_thread(self.conv_log.get_metadata, session_key)
                title = str((meta or {}).get("title") or "")
            except Exception:
                logger.debug("Teams resume: title lookup failed", exc_info=True)
        # The picker's own fallback for an untitled session, so a bootstrapped record
        # names the conversation the way the user saw it listed.
        return title or session_key.removeprefix("dashboard:")

    # ── the picker ────────────────────────────────────────────────────────
    async def show_picker(
        self,
        client: "TeamsClient",
        identity: str,
        conversation_id: str,
        service_url: str,
        query: str = "",
    ) -> None:
        """Post the session picker, or say why there is nothing to post."""
        await self._controller.show_picker(
            _TeamsResumeSurface(client, conversation_id, service_url),
            caller=identity or "unknown",
            picker_owner=identity,
            is_owner=self.is_owner(identity),
            query=query,
        )

    async def choose(
        self,
        client: "TeamsClient",
        identity: str,
        conversation_id: str,
        service_url: str,
        activity_id: str,
        nonce: str,
        index: int,
    ) -> None:
        """Resolve a picker press: bind the chosen session, or say why not."""
        choice = await self._controller.choose(
            _TeamsResumeSurface(client, conversation_id, service_url),
            caller=identity or "unknown",
            picker_owner=identity,
            is_owner=self.is_owner(identity),
            message_id=activity_id,
            nonce=nonce,
            index=index,
            link=self.link_for(conversation_id),
        )
        if choice is not None:
            await self._replay(client, conversation_id, service_url, choice.key)

    async def _replay(
        self, client: "TeamsClient", conversation_id: str, service_url: str, session_key: str
    ) -> None:
        """Post the last few transcript entries, so the user sees where they left off."""
        if self.conv_log is None:
            return
        try:
            messages = await asyncio.to_thread(
                self.conv_log.recent, session_key, _REPLAY_MESSAGES, {"user", "assistant"}
            )
        except Exception:
            logger.exception("Teams resume: failed to read transcript context")
            return
        for message in messages:
            role = message.get("role", "")
            if role not in {"user", "assistant"}:
                continue
            raw = str(message.get("content") or "")
            if role == "assistant":
                # Strip the protocol framing a stored assistant turn can carry, so a
                # steering marker is never replayed as if the assistant had said it.
                raw = sanitize_channel_replay_text(raw)
            content = await asyncio.to_thread(_replay_preview, raw)
            if not content:
                continue
            icon = "🧑" if role == "user" else "🤖"
            try:
                # The icon gets its OWN line: on the body's first line it would sit in
                # front of a fence opener, which must start the line, so a replayed code
                # block would render as literal backticks however carefully it was split.
                await client.send_message(conversation_id, f"{icon}\n{content}", service_url)
            except TeamsSendError:
                logger.debug("Teams resume: context replay failed", exc_info=True)


def _replay_preview(raw: str) -> str:
    """One transcript entry, display-safe and budgeted at a fence boundary.

    Split with the shared fence-safe splitter rather than sliced, so a preview cannot
    end inside a code fence and leave the rest of the message rendering as code.
    """
    safe = _display_safe(raw).strip()
    if not safe:
        return ""
    budget = min(_REPLAY_TEXT_LIMIT, TEAMS_MAX_TEXT) - len(_REPLAY_TRUNCATED)
    chunks = split_markdown_safe(safe, budget)
    if not chunks:
        return ""
    return chunks[0] + (_REPLAY_TRUNCATED if len(chunks) > 1 else "")


def _picker_heading(query: str, total: int, normalized: str) -> str:
    """The line above the choices, saying what was searched and how much was cut."""
    shown = min(total, PICKER_LIMIT)
    if normalized:
        label = _safe_teams_text(" ".join(query.split()), 100)
        if total > PICKER_LIMIT:
            summary = f"Showing {PICKER_LIMIT} of {total} matching sessions"
        else:
            summary = f"Showing {shown} matching session{'s' if shown != 1 else ''}"
        return (
            f"🔎 **Dashboard session search**\n{summary} for “{label}”, ranked over "
            "titles and message content.\nChoose one to continue here; `/unlink` comes back."
        )
    if total > PICKER_LIMIT:
        summary = f"Showing {PICKER_LIMIT} of {total} most recent dashboard sessions."
    else:
        summary = f"Showing {shown} most recent dashboard session{'s' if shown != 1 else ''}."
    return (
        f"🧵 **Recent dashboard sessions**\n{summary}\nChoose one to continue here; "
        "`/unlink` comes back."
    )


__all__ = ["ResumeReleaseError", "RoutingDecision", "SessionChoice", "TeamsSessionResume"]
