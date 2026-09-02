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
    PickerRegistry,
    ResumeCopy,
    RoutingDecision,
    SessionBinder,
    SessionChoice,
    resolve_session_choices,
)
from kiro_crew.messaging.split import split_markdown_safe
from kiro_crew.sel import sel
from kiro_crew.session_map import ConversationOwnershipConflict
from kiro_crew.teams.cards import session_picker_card
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
        self.pickers = PickerRegistry()
        # Set by the gateway after construction (same pattern as ``client`` on the
        # dispatcher). Binding a session from Teams changes what the dashboard must
        # display; without a push an already-open dashboard shows no "driven from" chip
        # until unrelated activity happens to refresh slots.
        self.dashboard_state: object | None = None
        self._binder = SessionBinder(sessions, channel_type="teams", copy=_TEAMS_COPY)
        self._binder.title_display = _safe_teams_text
        self._expectations = self._binder.expectations

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
            logger.debug("Teams: slots push after binding change failed", exc_info=True)

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
            self._push_slots()
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

        async def _say(text: str) -> None:
            try:
                await client.send_message(conversation_id, text, service_url)
            except TeamsSendError:
                logger.warning("Teams resume: notice delivery failed", exc_info=True)

        if not self.is_owner(identity):
            sel().log_api_access(
                caller=identity or "unknown",
                operation="teams.sessions_data_access",
                outcome="denied",
                source="teams",
            )
            await _say(
                "🔒 `/sessions` requires exactly one configured `teams.allowed_emails` "
                "entry — a dashboard session is the operator's own transcript, so it is "
                "not listable on a shared allow-list."
            )
            return
        if self.conv_log is None:
            await _say("⚠️ Recent sessions are unavailable.")
            return

        try:
            choices, total = await resolve_session_choices(self.conv_log, query, _safe_teams_text)
        except Exception as exc:
            sel().log_api_access(
                caller=identity or "unknown",
                operation="teams.sessions_data_access",
                outcome="error",
                source="teams",
                resources="0 sessions read",
                error=_safe_teams_text(str(exc), 200),
            )
            logger.exception("Teams sessions: history listing failed")
            await _say("⚠️ Recent sessions are unavailable.")
            return

        sel().log_api_access(
            caller=identity or "unknown",
            operation="teams.sessions_data_access",
            outcome="allowed",
            source="teams",
            resources=f"{len(choices)} sessions read",
        )
        normalized = " ".join(query.casefold().split())
        if not choices:
            if normalized:
                label = _safe_teams_text(" ".join(query.split()), 100)
                await _say(
                    f"No dashboard sessions matched “{label}”. Try fewer words, or run "
                    f"`/sessions` to see up to {PICKER_LIMIT} recent sessions."
                )
            else:
                await _say("No recent dashboard sessions.")
            return

        self.pickers.purge()
        self.pickers.drop_for(identity)
        nonce = self.pickers.mint()
        frozen = tuple(choices)
        heading = _picker_heading(query, total, normalized)
        try:
            activity_id = (
                await client.send_card(
                    conversation_id,
                    session_picker_card(prompt=heading, choices=frozen, nonce=nonce),
                    service_url,
                )
                or ""
            )
        except TeamsSendError:
            logger.warning("Teams resume: picker card delivery failed", exc_info=True)
            # No nonce is registered, so nothing can resolve against a card that never
            # landed -- and the user is told rather than left with silence.
            await _say("⚠️ Couldn't show the session list. Try `/sessions` again.")
            return
        if activity_id:
            # Owner alone is the scope: a Teams personal chat is 1:1, so the identity
            # already names the conversation.
            self.pickers.register(nonce, identity, activity_id, frozen)

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

        async def _settle(text: str) -> bool:
            """Replace the picker with an outcome, so no row still looks pressable."""
            try:
                from kiro_crew.teams.cards import resolved_card

                return bool(
                    await client.update_card(
                        conversation_id,
                        activity_id,
                        resolved_card(title="Sessions", outcome=text),
                        service_url,
                    )
                )
            except TeamsSendError:
                logger.debug("Teams resume: picker settle failed", exc_info=True)
                return False

        if not self.is_owner(identity):
            sel().log_api_access(
                caller=identity or "unknown",
                operation="teams.session_resume_choice",
                outcome="denied",
                source="teams",
            )
            await _settle("session resume is owner-only")
            return

        choice = self.pickers.take(nonce, index, identity, activity_id)
        if choice is None:
            await _settle("this list expired — run `/sessions` again")
            return
        if self.conv_log is None or not await asyncio.to_thread(self.conv_log.has_log, choice.key):
            await _settle("that session is no longer available")
            return

        link = self.link_for(conversation_id)
        async with self._binder.lock:
            conflict = self._binder.binding_conflict(choice.key, choice.title, link)
            if conflict is not None:
                await _settle(conflict)
                return
            try:
                # Record BEFORE the banner and the binding, all under the lock: once
                # Teams shows "Resumed", only durable evidence survives a crash, and a
                # rollback is unsafe (no map revision; a racing dashboard rebind skips
                # this lock). A lost banner or bind fails toward ONE notice.
                await self._binder.expectations.record(conversation_id, choice.key, choice.title)
            except Exception:
                logger.warning("Teams resume: the pick did not take effect", exc_info=True)
                await _settle(
                    "couldn't save which session this conversation is linked to, so it "
                    "was NOT resumed — run `/sessions` to try again"
                )
                return
            if not await _settle(f"resumed “{choice.title}” — `/unlink` to come back"):
                return
            # The settle above awaited a round trip. Re-check before writing: a
            # dashboard mirror or another channel's link may have claimed this session
            # or this conversation in that window.
            conflict = self._binder.binding_conflict(choice.key, choice.title, link)
            if conflict is not None:
                await _settle(conflict)
                return
            try:
                self.sessions.set_mirror_link(choice.key, link, accepts_inbound=True)
                self._push_slots()
            except ConversationOwnershipConflict:
                # binding_conflict already checked, twice -- but it and the dashboard
                # connect endpoint evaluate under different locks, so the precheck can
                # lose the race. The atomic claim inside set_mirror_link catches it.
                # An ordinary conflict, not a fault: say so in the precheck's words.
                logger.debug("Teams resume: lost the claim race for this conversation")
                await _settle(
                    "another session just connected here — run `/unlink`, then "
                    "`/sessions` to resume this one"
                )
                return
            except Exception:
                logger.exception("Teams resume: failed to persist binding")
                await _settle("couldn't resume that session — run `/sessions` to try again")
                return

        sel().log_api_access(
            caller=identity or "unknown",
            operation="teams.session_resume",
            outcome="allowed",
            source="teams",
            resources=choice.key,
        )
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
