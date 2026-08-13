"""Owner-only Discord session picker and persisted resume binding."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from kiro_crew.discord.resume_expectation import (
    ExpectationStoreError,
    ResumeExpectation,
    ResumeExpectations,
)
from kiro_crew.history import is_incognito_transcript, needles_match_text, parse_search_query
from kiro_crew.messaging.driver import sanitize_channel_replay_text
from kiro_crew.messaging.link import UNBIND_REASON_USER_UNLINK, ChannelLink
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.session_map import ConversationOwnershipConflict

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
#: Matches the picker's own label budget, so the title a detach notice names is
#: the same string the user picked.
_TITLE_LIMIT = 76


@dataclass(frozen=True)
class _SessionChoice:
    key: str
    title: str


@dataclass(frozen=True)
class InboundResolution:
    """What the inbound resolver found. ``resumed_session`` collapses "no owner" and
    "two owners" into one ``None`` — right for ROUTING, wrong for the user: one means
    the link is gone, the other that it cannot be chosen between."""

    key: str | None
    ambiguous: bool


_SETTLE_NOTHING = "nothing"  # record persists; refuse again next message
_SETTLE_CLEAR = "clear"  # link gone; refuse once, then run natively
_SETTLE_ADOPT = "adopt"  # link moved; adopt the new session

_MAX_ROUTE_ATTEMPTS = 3
#: Refusal when the attachment cannot be made durable or read back: the turn would
#: otherwise use a link whose later loss nothing can detect.
_STORAGE_REFUSAL = ("🔗 Can't read or save which conversation this channel is linked to, so your "
                    "message was NOT processed. This needs an operator to repair the gateway's "
                    "expectation store; `!sessions` still lists sessions, but a reattachment "
                    "cannot be saved until it is.")


class ResumeReleaseError(RuntimeError):
    """A resumed binding removal could not be made durable."""


@dataclass(frozen=True)
class RoutingDecision:
    """Where one inbound message runs, or the refusal that stops it running — ONE object
    for both, computed once, because two resolver calls with an await between them let
    the binding vanish in the gap and the routing check fall through to the DM's own
    session. ``described`` is the record the refusal quoted; ``observed`` the live state
    it described, which settlement re-checks: the version cannot see a dashboard rebind."""

    resumed_key: str | None = None
    refusal: str | None = None
    settle: str = _SETTLE_NOTHING
    described: ResumeExpectation | None = None
    observed: InboundResolution | None = None
    adopt_key: str = ""
    adopt_title: str = ""


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


def _storage_refused(channel_id: str, why: str,
                     observed: "InboundResolution | None" = None) -> "RoutingDecision":
    """Log and refuse: routing on an unreadable store attaches the turn to a binding whose later loss nothing can detect."""
    logger.warning("discord resume: %s at channel %s; refusing rather than routing "
                   "undetectably", why, channel_id, exc_info=True)
    return RoutingDecision(refusal=_STORAGE_REFUSAL, settle=_SETTLE_NOTHING, observed=observed)


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
        # Survives the bound session's map entry, which is what makes a
        # binding destroyed out-of-band reportable at all -- see
        # discord/resume_expectation.py.
        self._expectations = ResumeExpectations()

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
        """Resolve this conversation's inbound owner, keeping "none" and "many" apart."""
        matches = self.sessions.find_mirror_sessions(
            self.link_for(channel_id),
            inbound_only=True,
        )
        if len(matches) == 1:
            return InboundResolution(key=matches[0], ambiguous=False)
        if matches:
            logger.error(
                "discord resume: ambiguous inbound bindings for channel %s; routing denied",
                channel_id,
            )
            return InboundResolution(key=None, ambiguous=True)
        return InboundResolution(key=None, ambiguous=False)

    def resumed_session(self, channel_id: str) -> str | None:
        """Resolve exactly one inbound-enabled binding, failing closed on duplicates."""
        return self.resolve_inbound(channel_id).key

    async def leave_resumed_session(self, channel_id: str) -> str | None:
        # The bind lock closes the picker gap; live-owner checks cover dashboard writers.
        async with self._bind_lock:
            seen = self.resolve_inbound(channel_id)
            releasing = seen.key is not None or seen.ambiguous
            cleared: list[str] = []
            if releasing:
                # Evidence before mutation: a cleared owner whose flush then fails
                # would otherwise run natively in silence until the persisted
                # binding revives on restart, splitting the conversation history.
                try:
                    expected = await self._expectations.get(channel_id)
                    owner = seen.key or next(iter(self.sessions.find_mirror_sessions(
                        self.link_for(channel_id), inbound_only=True)), None)
                    if owner and (expected is None or expected.retired):
                        await self._expectations.record(channel_id, owner, await self._title_of(owner))
                except ExpectationStoreError as exc:
                    raise ResumeReleaseError("release evidence write failed") from exc
                # Free every co-located occupant so unlink cannot leave an ambiguous owner.
                cleared = self.sessions.clear_mirror_links_at(
                    self.link_for(channel_id), reason=UNBIND_REASON_USER_UNLINK)
            # A retry must land a failed in-memory removal before record retirement.
            try:
                await self.sessions.aflush()
            except Exception as exc:
                logger.warning("discord resume: binding release was not durable for channel %s",
                               channel_id, exc_info=True)
                raise ResumeReleaseError("session-map flush failed") from exc
            if releasing:
                logger.info("discord: released resumed session %s (cleared bindings: %s)",
                            seen.key or "(ambiguous)", ", ".join(cleared) or "none")
                self._push_slots()
            try:
                # Retire on the version CAS alone: an owner racing the flush is a NEW
                # attachment that must meet retired evidence and be announced, while a
                # newer picker record already wins by version. Gating on a live owner
                # would let a same-key rebind ride the stale record in silence.
                expected = await self._expectations.get(channel_id)
                if expected is not None and not expected.retired:
                    await self._expectations.retire_if(channel_id, expected.version)
            except ExpectationStoreError:
                logger.warning(
                    "discord resume: could not retire the released record at channel %s; "
                    "one stale detach notice may follow", channel_id, exc_info=True)
        return seen.key or (cleared[0] if cleared else None)

    async def route(self, channel_id: str) -> RoutingDecision:
        """Decide where one inbound message runs, or why it does not run at all. The
        store read comes FIRST and the session map AFTER it, so the live binding is
        never older than the record it is compared against; any further await
        revalidates — a move before the bootstrap record refuses outright, one after
        it restarts, an exhausted budget refuses. The spec's state table is authoritative."""
        for _ in range(_MAX_ROUTE_ATTEMPTS):
            try:
                expected = await self._expectations.get(channel_id)
            except ExpectationStoreError:
                return _storage_refused(channel_id, "cannot read the store")
            resolution = self.resolve_inbound(channel_id)
            if resolution.ambiguous:
                return RoutingDecision(refusal=(
                        "🔗 Ambiguous link: this conversation is claimed by more than "
                        "one session, so it cannot be resumed and your message was NOT "
                        "processed. Run `!unlink` to release them, then `!sessions` to " "reattach."
                    ), settle=_SETTLE_NOTHING, observed=resolution,
                )
            if resolution.key is None:
                if expected is None or expected.retired:
                    return RoutingDecision()
                return RoutingDecision(
                    refusal=(
                        "🔗 Detached: this conversation is no longer linked to "
                        f'"{self._display(expected)}". Your message was NOT processed. '
                        "Run `!sessions` to reattach, or resend to continue in your own "
                        "conversation."
                    ),
                    settle=_SETTLE_CLEAR,
                    described=expected,
                    observed=resolution,
                )
            if (expected is not None and not expected.retired and expected.key == resolution.key):
                return RoutingDecision(resumed_key=resolution.key)

            title = await self._title_of(resolution.key)
            if self.resolve_inbound(channel_id) != resolution:
                break
            if expected is None:
                try:
                    await self._expectations.record(channel_id, resolution.key, title)
                except ExpectationStoreError:
                    return _storage_refused(channel_id, "could not record the binding", resolution)
                if self.resolve_inbound(channel_id) != resolution:
                    continue
                return RoutingDecision(resumed_key=resolution.key)
            return RoutingDecision(
                refusal=(
                    f'🔗 Now linked to "{_safe_discord_text(title, _TITLE_LIMIT)}" '
                    f'instead of "{self._display(expected)}". Your message was NOT '
                    "processed. Resend it to continue in the new conversation, or run "
                    "`!unlink` to go back to your own."
                ),
                settle=_SETTLE_ADOPT,
                described=expected,
                observed=resolution,
                adopt_key=resolution.key,
                adopt_title=title,
            )
        logger.warning(
            "discord resume: channel %s kept changing owner; refusing this message",
            channel_id,
        )
        return RoutingDecision(
            refusal=(
                "🔗 This conversation's link is changing right now, so your message was "
                "NOT processed. Send it again in a moment."
            ),
            settle=_SETTLE_NOTHING,
        )

    async def settle(self, channel_id: str, decision: RoutingDecision) -> None:
        """Apply a delivered refusal with version and live-owner guards. A detach
        becomes one durable retired marker: native routing can resume with no owner,
        while any owner racing that write still encounters the retained evidence."""
        if decision.settle == _SETTLE_NOTHING or decision.described is None:
            return
        if self.resolve_inbound(channel_id) != decision.observed:
            logger.info(
                "discord resume: channel %s moved while the notice was in flight; "
                "leaving the record for the next message to re-decide",
                channel_id,
            )
            return
        if decision.settle == _SETTLE_CLEAR:
            try:
                await self.sessions.aflush()
            except Exception:
                logger.warning(
                    "discord resume: detach durability failed at %s",
                    channel_id, exc_info=True)
                return
        try:
            if decision.settle == _SETTLE_CLEAR:
                await self._expectations.retire_if(channel_id, decision.described.version)
            elif decision.settle == _SETTLE_ADOPT:
                await self._expectations.record_if(
                    channel_id,
                    decision.described.version,
                    decision.adopt_key,
                    decision.adopt_title,
                )
        except ExpectationStoreError:
            # Unsettled, so the same refusal is owed again.
            logger.warning(
                "discord resume: could not settle the notice at channel %s; the "
                "next message will be refused again", channel_id, exc_info=True,
            )

    def _display(self, expected: ResumeExpectation) -> str:
        """A title safe to post: redacted, mention-neutered, length-capped, because
        a title read back from the store or history is conversation text."""
        if expected.retired:
            return "your own Discord conversation"
        return _safe_discord_text(expected.title or expected.key, _TITLE_LIMIT)

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
                fallback_needles, fallback_phrase, fallback_floor = parse_search_query(
                    normalized_query
                )
                fallback_multi = [
                    n.text for n in fallback_needles if n.required
                ] != [fallback_phrase]
                if not rows and fallback_multi:
                    # search_sessions matches multi-word queries needle-wise (all
                    # required needles must appear, in the title or the content),
                    # so out-of-order words like "specific link" DO resolve
                    # "Link to a Specific Session". What it still cannot reach is
                    # a session older than its _SEARCH_SCAN_WINDOW most-recent
                    # cap, so keep this unbounded TITLE match as the last resort
                    # for a long-lived install. Only on zero hits, so the shared
                    # search stays authoritative and we are not running two
                    # rankers in parallel. The gate comes from the SAME parse as
                    # search_sessions (needles_match_text), not a second
                    # whitespace tokenization — a spaceless CJK query has no
                    # spaces to split on, so a word-count test never fired for
                    # it and the fallback demanded the literal title substring.
                    listed = await asyncio.to_thread(self.conv_log.list_sessions)
                    rows = [
                        row
                        for row in listed
                        if isinstance(row, dict)
                        and needles_match_text(
                            fallback_needles,
                            " ".join(str(row.get("title") or "").casefold().split()),
                            fallback_floor,
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
            if is_incognito_transcript(row.get("memory_mode")):
                continue
            key = _history_dashboard_key(row.get("key"))
            if key is None:
                continue
            raw_title = str(row.get("title") or key.removeprefix("dashboard:"))
            clean = " ".join(raw_title.split())
            title = _safe_discord_text(clean, _TITLE_LIMIT) or "Untitled session"
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

            try:
                # Record BEFORE the success banner and the binding, all under the bind
                # lock: once Discord shows "Resumed", only durable evidence survives a
                # crash, and a rollback is unsafe (no map revision; the racing dashboard
                # rebind skips the lock). A lost banner or bind fails toward one notice.
                await self._expectations.record(interaction.channel_id, choice.key, choice.title)
            except ExpectationStoreError:
                logger.warning(
                    "discord resume: the pick of %s did not take effect",
                    choice.key, exc_info=True,
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
