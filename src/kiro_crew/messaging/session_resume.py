"""Resuming a dashboard session inside a channel conversation, channel-neutrally.

A user picks one of their dashboard chats from a channel and continues it there. Two
channels grew this independently -- Discord first, and Teams would have been a second
copy of ~600 lines -- so this module is the half that is genuinely shared, split on
the same line ``queue_receipt.py`` uses:

* **HERE** -- the choice list (which sessions are eligible, and the search that ranks
  them), the picker registry with its nonce/TTL/owner scoping, the binding-conflict
  rules, and the inbound routing + settlement state machine. These are decisions, and
  every one of them is a place where a mistake routes somebody's transcript into
  someone else's chat.
* **NOT here** -- posting the widget and the wording around it. A channel supplies a
  :class:`ResumeSurface`, so nothing below ever sees a Discord component array or an
  Adaptive Card, and no address type appears in this module at all.

The routing machine is the subtle part and the reason a second copy was not an option.
Between reading the durable expectation and reading the live session map, a binding can
appear, vanish or move; every ``await`` afterwards can invalidate what was just decided.
So one call returns ONE :class:`RoutingDecision` -- where the message runs, or the
refusal that stops it running, plus the settlement owed once that refusal is delivered.
Splitting it into two resolver calls lets the binding change in the gap and the routing
check fall through to the conversation's own session, which is a silent mis-route.

Dependency direction is ``<channel> -> messaging`` (never the reverse).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

from kiro_crew.history import is_incognito_transcript, needles_match_text, parse_search_query
from kiro_crew.messaging.link import UNBIND_REASON_USER_UNLINK, ChannelLink
from kiro_crew.messaging.renderer import new_approval_nonce
from kiro_crew.messaging.resume_expectation import (
    ExpectationStoreError,
    ResumeExpectation,
    ResumeExpectations,
)
from kiro_crew.sel import sel
from kiro_crew.session_map import ConversationOwnershipConflict

logger = logging.getLogger(__name__)

#: Choices a picker offers. Discord's button rows and a Teams Adaptive Card both hold
#: this comfortably; past it a picker stops being scannable.
PICKER_LIMIT = 10
#: Rows pulled from history BEFORE the incognito/keying filters. Larger than
#: PICKER_LIMIT so filtered-out rows cannot starve the visible slots.
SEARCH_FETCH_LIMIT = 50
#: How long a posted picker stays pressable, and how many are retained. Both bound
#: unbounded growth (one entry per press-less invocation), not UX: an expired or
#: evicted picker says "run it again" rather than acting on a stale list.
PICKER_TTL_SECS = 300
PICKER_REGISTRY_MAX = 100
#: Title budget. The picker label and any notice that quotes a title use the SAME
#: limit, so the string a detach notice names is the one the user picked.
TITLE_LIMIT = 76

#: Settlement owed once a refusal has actually been delivered.
SETTLE_NOTHING = "nothing"  # record persists; refuse again next message
SETTLE_CLEAR = "clear"  # link gone; refuse once, then run natively
SETTLE_ADOPT = "adopt"  # link moved; adopt the new session

#: Re-decide budget for ``route``. A conversation whose owner keeps changing under us
#: is refused rather than routed on a guess.
_MAX_ROUTE_ATTEMPTS = 3


class ResumeReleaseError(RuntimeError):
    """A resumed binding removal could not be made durable."""


@dataclass(frozen=True)
class SessionChoice:
    """One offered session: the dashboard key, and the label the user sees."""

    key: str
    title: str


@dataclass(frozen=True)
class InboundResolution:
    """What the inbound resolver found.

    ``key`` collapses "no owner" and "two owners" into one ``None`` -- right for
    ROUTING, wrong for the user: one means the link is gone, the other that it cannot
    be chosen between. ``ambiguous`` keeps them apart.
    """

    key: str | None
    ambiguous: bool


@dataclass(frozen=True)
class RoutingDecision:
    """Where one inbound message runs, or the refusal that stops it running.

    ONE object for both, computed once. ``described`` is the record a refusal quoted;
    ``observed`` the live state it described, which settlement re-checks -- the version
    alone cannot see a dashboard rebind.
    """

    resumed_key: str | None = None
    refusal: str | None = None
    settle: str = SETTLE_NOTHING
    described: ResumeExpectation | None = None
    observed: InboundResolution | None = None
    adopt_key: str = ""
    adopt_title: str = ""


@dataclass(frozen=True)
class ResumeCopy:
    """The per-channel words in shared refusals.

    Only the command spellings differ between channels (``!sessions`` vs
    ``/sessions``), so they are DATA rather than a per-channel copy of each sentence --
    which is how two channels' wording drifts until one of them stops matching its own
    help text. ``conversation_noun`` names the thing the user is in, because "channel"
    is wrong for a Teams personal chat.
    """

    sessions_command: str
    unlink_command: str
    conversation_noun: str = "conversation"


class ResumeSurface(Protocol):
    """One conversation's picker surface, with its address already bound.

    The address is bound at CONSTRUCTION, which is what keeps every channel id, chat
    id and conversation id out of this module -- the same reason ``ReceiptSurface``
    exists. A channel implements its own rendering and exact wording.
    """

    #: Durable conversation identity for the expectation store. This can be
    #: narrower than ``ChannelLink.channel_id`` (for example a forum Topic).
    expectation_id: str
    owner_refusal: str
    choice_owner_refusal: str
    choice_expired: str
    choice_missing: str
    choice_expectation_failed: str
    choice_claim_lost: str
    choice_binding_failed: str

    def display_safe(self, text: str, max_chars: int) -> str:
        """Redact + neutralize + budget *text* for THIS channel's rendering."""

    def no_choices(self, query: str) -> str:
        """The exact empty-result message for a listing or search."""

    def picker_heading(self, query: str, total: int) -> str:
        """The exact text accompanying a picker with *total* eligible sessions."""

    def choice_success(self, choice: SessionChoice) -> str:
        """The exact successful-resume settlement for *choice*."""

    async def post_picker(
        self, heading: str, nonce: str, choices: tuple[SessionChoice, ...]
    ) -> str:
        """Post the picker; return an opaque message id (empty when unknown)."""

    async def settle_picker(self, message_id: str, text: str) -> bool:
        """Replace a posted picker with *text* and NO controls. False if it failed."""

    async def say(self, text: str) -> None:
        """Post one plain message (a refusal, an empty-result notice)."""


def history_dashboard_key(raw_key: object) -> str | None:
    """Restore the canonical dashboard session key from a history row, or None.

    A transcript stem is not always the session key: the same session can appear as
    ``dashboard:<slot>`` or as the file-stem form ``dashboard_<slot>``, and a row from
    some other surface is not resumable at all.

    The stem prefix is stripped in a LOOP, not once: a re-archived transcript can carry
    it stacked (``dashboard_dashboard_<slot>``), and stripping one layer would bind
    ``dashboard:dashboard_<slot>`` -- a key no session has, so the resume silently
    attaches to nothing.
    """
    key = str(raw_key or "")
    if key.startswith("dashboard:"):
        return key
    if key.startswith("dashboard_"):
        while key.startswith("dashboard_"):
            key = key[len("dashboard_") :]
        return f"dashboard:{key}" if key else None
    return None


async def resolve_session_choices(
    conv_log: Any,
    query: str,
    display_safe: Any,
    *,
    limit: int = PICKER_LIMIT,
    fetch_limit: int = SEARCH_FETCH_LIMIT,
) -> tuple[list[SessionChoice], int]:
    """The offerable sessions for *query* (newest-first when blank), and the total.

    Reuses the SAME search the dashboard uses (``search_sessions``): it matches message
    CONTENT with a title boost and length-normalised ranking, so a phrase the user
    remembers from the CONVERSATION finds the session. A title-only filter here would
    miss exactly that case, and would be a second search implementation free to drift
    from the dashboard's ranking.

    Order is already meaningful -- ``search_sessions`` returns best-scored first,
    ``list_sessions`` newest first -- so it is never re-sorted.

    Raises whatever the history layer raises; the caller decides what to tell the user.
    """
    normalized = " ".join(query.casefold().split())
    if normalized:
        rows = await asyncio.to_thread(conv_log.search_sessions, query, fetch_limit)
        needles, phrase, floor = parse_search_query(normalized)
        multi_word = [n.text for n in needles if n.required] != [phrase]
        if not rows and multi_word:
            # search_sessions matches multi-word queries needle-wise, so out-of-order
            # words DO resolve. What it cannot reach is a session older than its scan
            # window, so this unbounded TITLE match is the last resort for a long-lived
            # install -- only on zero hits, so the shared search stays authoritative
            # and two rankers never run in parallel. The gate comes from the SAME parse
            # (needles_match_text), not a second whitespace tokenization: a spaceless
            # CJK query has no spaces to split on, so a word-count test never fired for
            # it and the fallback demanded the literal title substring.
            listed = await asyncio.to_thread(conv_log.list_sessions)
            rows = [
                row
                for row in listed
                if isinstance(row, dict)
                and needles_match_text(
                    needles, " ".join(str(row.get("title") or "").casefold().split()), floor
                )
            ]
    else:
        rows = await asyncio.to_thread(conv_log.list_sessions)

    eligible: list[SessionChoice] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        # An incognito transcript is never offered: resuming it would copy a
        # deliberately unpersisted conversation into a channel that does persist.
        if is_incognito_transcript(row.get("memory_mode")):
            continue
        key = history_dashboard_key(row.get("key"))
        if key is None:
            continue
        raw_title = str(row.get("title") or key.removeprefix("dashboard:"))
        title = display_safe(" ".join(raw_title.split()), TITLE_LIMIT) or "Untitled session"
        eligible.append(SessionChoice(key=key, title=title))
    return eligible[:limit], len(eligible)


@dataclass
class _Picker:
    owner_id: str
    message_id: str
    created_at: float
    choices: tuple[SessionChoice, ...]


class PickerRegistry:
    """Posted pickers, keyed by a per-picker nonce.

    The nonce is what makes a stale press unusable: a picker still on screen from a
    previous run names indexes that are live again for a DIFFERENT session list, and
    resuming the wrong session is exactly the mis-route this subsystem exists to
    prevent. Bounded by TTL and by count, so a user who runs the command repeatedly
    without pressing anything cannot grow it without limit.
    """

    def __init__(self) -> None:
        self._pickers: dict[str, _Picker] = {}

    def __len__(self) -> int:
        return len(self._pickers)

    def purge(self) -> None:
        cutoff = time.monotonic() - PICKER_TTL_SECS
        for nonce, picker in list(self._pickers.items()):
            if picker.created_at < cutoff:
                self._pickers.pop(nonce, None)
        if len(self._pickers) >= PICKER_REGISTRY_MAX:
            oldest = sorted(self._pickers, key=lambda key: self._pickers[key].created_at)
            for nonce in oldest[: len(self._pickers) - PICKER_REGISTRY_MAX + 1]:
                self._pickers.pop(nonce, None)

    def drop_for(self, owner: str) -> None:
        """Retire this owner's earlier pickers, so only the newest list is live."""
        for nonce, picker in list(self._pickers.items()):
            if picker.owner_id == owner:
                self._pickers.pop(nonce, None)

    @staticmethod
    def mint() -> str:
        """A nonce for a picker about to be posted.

        Separate from :meth:`register` because the widget has to CARRY the nonce, so it
        must exist before the post whose message id the registration needs. The token
        itself comes from the shared minter -- a picker press and an approval press are
        the same hazard (a control left in a chat from an earlier list), so there is no
        reason for a second generator with its own alphabet and its own entropy.
        """
        return new_approval_nonce()

    def register(
        self, nonce: str, owner: str, message_id: str, choices: tuple[SessionChoice, ...]
    ) -> None:
        """Record a posted picker. ``owner`` is whatever identity the channel scopes by."""
        self._pickers[nonce] = _Picker(
            owner_id=owner,
            message_id=message_id,
            created_at=time.monotonic(),
            choices=choices,
        )

    def take(self, nonce: str, index: int, owner: str, message_id: str) -> SessionChoice | None:
        """Consume one choice, or None when the press must not resolve.

        Every mismatch answers None rather than raising: an expired nonce, a different
        owner, a press on a different message, and an out-of-range index are all "this
        press does nothing", and the caller tells the user so. ``message_id`` is checked
        as well as the nonce, so a press cannot be replayed against a DIFFERENT posting
        of the same list. Consumed on success, so a double-press cannot resume twice.
        """
        self.purge()
        picker = self._pickers.get(nonce)
        if picker is None:
            return None
        if picker.owner_id != owner or picker.message_id != message_id:
            return None
        if index < 0 or index >= len(picker.choices):
            return None
        self._pickers.pop(nonce, None)
        return picker.choices[index]


class SessionBinder:
    """The binding half: conflicts, inbound routing, settlement and release.

    One per channel, holding that channel's durable expectation store. Every method is
    address-shaped only through ``link`` -- a :class:`ChannelLink` the caller builds --
    so no channel id type reaches this class.
    """

    def __init__(self, sessions: Any, *, channel_type: str, copy: ResumeCopy) -> None:
        self.sessions = sessions
        self.channel_type = channel_type
        self.copy = copy
        self._expectations = ResumeExpectations(channel_type)
        #: Optional ``(text, max_chars) -> str`` the channel supplies so a title this
        #: class quotes back is redacted and neutralized the way that channel renders.
        self.title_display: Any | None = None
        # Serialises this channel's OWN picker. It deliberately does not cover a
        # dashboard mirror write or another channel's link command, which is why
        # `binding_conflict` is re-evaluated immediately before every write.
        self.lock = asyncio.Lock()

    # -- reads ---------------------------------------------------------------
    def resolve_inbound(self, link: ChannelLink) -> InboundResolution:
        """Resolve this conversation's inbound owner, keeping "none" and "many" apart."""
        matches = self.sessions.find_mirror_sessions(link, inbound_only=True)
        if len(matches) == 1:
            return InboundResolution(key=matches[0], ambiguous=False)
        if matches:
            logger.error(
                "%s resume: ambiguous inbound bindings for a conversation; routing denied",
                self.channel_type,
            )
            return InboundResolution(key=None, ambiguous=True)
        return InboundResolution(key=None, ambiguous=False)

    def resumed_session(self, link: ChannelLink) -> str | None:
        """Exactly one inbound-enabled binding, failing closed on duplicates."""
        return self.resolve_inbound(link).key

    def binding_conflict(self, key: str, title: str, link: ChannelLink) -> str | None:
        """A refusal when *key* must not be bound to *link*, else None.

        Evaluated TWICE per resume: once for fast feedback, and again immediately
        before the write. The second call is load-bearing -- :attr:`lock` only
        serialises this channel's own picker, while a dashboard mirror link or another
        channel's link command takes no such lock and either can claim this session, or
        this conversation, during the awaited round trip in between. Without the
        re-check those newer bindings are silently overwritten.
        """
        existing = self.sessions.get_mirror_link(key)
        if existing is not None and existing != link:
            return (
                f"🧵 This session is already active on "
                f"{existing.channel_type.title()}. Unlink it there first."
            )
        if key in self.sessions.find_mirror_sessions(link, inbound_only=True):
            return f"🧵 Already active here: {title}"
        occupants = [
            candidate for candidate in self.sessions.find_mirror_sessions(link) if candidate != key
        ]
        if occupants:
            # The unlink command clears every binding at this location by value --
            # resumed sessions and outbound dashboard mirrors alike -- so one
            # instruction is always followable from inside the conversation.
            return (
                f"⚠️ This {self.copy.conversation_noun} is already attached to another "
                f"session. Run `{self.copy.unlink_command}` first."
            )
        return None

    # -- expectations --------------------------------------------------------
    @property
    def expectations(self) -> ResumeExpectations:
        """The durable record store, for the channel's own attach path."""
        return self._expectations

    def _described(self, expected: ResumeExpectation) -> str:
        return self.display_title(expected.title or expected.key)

    def display_title(self, title: str) -> str:
        """A title as a notice should quote it.

        Defaults to a plain budget; a channel whose rendering needs redaction or
        mention-defusing sets :attr:`title_display` to its own ``display_safe``.
        """
        if self.title_display is not None:
            return self.title_display(title, TITLE_LIMIT)
        return title[:TITLE_LIMIT]

    # -- routing -------------------------------------------------------------
    def _storage_refused(
        self, why: str, observed: InboundResolution | None = None
    ) -> RoutingDecision:
        """Refuse rather than route on an unreadable store.

        Routing anyway attaches the turn to a binding whose later loss nothing can
        detect, which splits one conversation's history in two without a report.
        """
        logger.warning(
            "%s resume: %s; refusing rather than routing undetectably",
            self.channel_type,
            why,
            exc_info=True,
        )
        return RoutingDecision(
            refusal=(
                f"🔗 Can't read or save which session this {self.copy.conversation_noun} "
                "is linked to, so your message was NOT processed. This needs an operator "
                f"to repair the gateway's expectation store; `{self.copy.sessions_command}` "
                "still lists sessions, but a reattachment cannot be saved until it is."
            ),
            settle=SETTLE_NOTHING,
            observed=observed,
        )

    async def route(
        self, conversation_id: str, link: ChannelLink, title_of: Any
    ) -> RoutingDecision:
        """Decide where one inbound message runs, or why it does not run at all.

        The store read comes FIRST and the session map AFTER it, so the live binding is
        never older than the record it is compared against; any further await
        revalidates -- a move before the bootstrap record refuses outright, one after it
        restarts, an exhausted budget refuses.
        """
        for _ in range(_MAX_ROUTE_ATTEMPTS):
            try:
                expected = await self._expectations.get(conversation_id)
            except ExpectationStoreError:
                return self._storage_refused("cannot read the store")
            resolution = self.resolve_inbound(link)
            if resolution.ambiguous:
                return RoutingDecision(
                    refusal=(
                        f"🔗 Ambiguous link: this {self.copy.conversation_noun} is claimed "
                        "by more than one session, so it cannot be resumed and your "
                        f"message was NOT processed. Run `{self.copy.unlink_command}` to "
                        f"release them, then `{self.copy.sessions_command}` to reattach."
                    ),
                    settle=SETTLE_NOTHING,
                    observed=resolution,
                )
            if resolution.key is None:
                if expected is None or expected.retired:
                    return RoutingDecision()
                return RoutingDecision(
                    refusal=(
                        f"🔗 Detached: this {self.copy.conversation_noun} is no longer "
                        f'linked to "{self._described(expected)}". Your message was NOT '
                        f"processed. Run `{self.copy.sessions_command}` to reattach, or "
                        "resend to continue in your own conversation."
                    ),
                    settle=SETTLE_CLEAR,
                    described=expected,
                    observed=resolution,
                )
            if expected is not None and not expected.retired and expected.key == resolution.key:
                return RoutingDecision(resumed_key=resolution.key)

            title = await title_of(resolution.key)
            if self.resolve_inbound(link) != resolution:
                break
            if expected is None:
                try:
                    await self._expectations.record(conversation_id, resolution.key, title)
                except ExpectationStoreError:
                    return self._storage_refused("could not record the binding", resolution)
                if self.resolve_inbound(link) != resolution:
                    continue
                return RoutingDecision(resumed_key=resolution.key)
            return RoutingDecision(
                refusal=(
                    f'🔗 Now linked to "{self.display_title(title)}" instead of '
                    f'"{self._described(expected)}". Your message was NOT processed. '
                    "Resend it to continue in the new conversation, or run "
                    f"`{self.copy.unlink_command}` to go back to your own."
                ),
                settle=SETTLE_ADOPT,
                described=expected,
                observed=resolution,
                adopt_key=resolution.key,
                adopt_title=title,
            )
        logger.warning(
            "%s resume: a conversation kept changing owner; refusing this message",
            self.channel_type,
        )
        return RoutingDecision(
            refusal=(
                f"🔗 This {self.copy.conversation_noun}'s link is changing right now, so "
                "your message was NOT processed. Send it again in a moment."
            ),
            settle=SETTLE_NOTHING,
        )

    async def settle(
        self, conversation_id: str, link: ChannelLink, decision: RoutingDecision
    ) -> None:
        """Apply a DELIVERED refusal, with version and live-owner guards.

        A detach becomes one durable retired marker: native routing can resume with no
        owner, while any owner racing that write still meets the retained evidence.
        """
        if decision.settle == SETTLE_NOTHING or decision.described is None:
            return
        if self.resolve_inbound(link) != decision.observed:
            logger.info(
                "%s resume: the conversation moved while the notice was in flight; "
                "leaving the record for the next message to re-decide",
                self.channel_type,
            )
            return
        if decision.settle == SETTLE_CLEAR:
            try:
                await self.sessions.aflush()
            except Exception:
                logger.warning(
                    "%s resume: detach durability failed", self.channel_type, exc_info=True
                )
                return
        try:
            if decision.settle == SETTLE_CLEAR:
                await self._expectations.retire_if(conversation_id, decision.described.version)
            elif decision.settle == SETTLE_ADOPT:
                await self._expectations.record_if(
                    conversation_id,
                    decision.described.version,
                    decision.adopt_key,
                    decision.adopt_title,
                )
        except ExpectationStoreError:
            # Unsettled, so the same refusal is owed again.
            logger.warning(
                "%s resume: could not settle the notice; the next message will be " "refused again",
                self.channel_type,
                exc_info=True,
            )

    async def release(self, conversation_id: str, link: ChannelLink, title_of: Any) -> str | None:
        """Drop this conversation's resumed binding. Returns the released key, or None.

        Raises :class:`ResumeReleaseError` when the removal could not be made durable:
        a cleared owner whose flush then failed would run natively in silence until the
        persisted binding revives on restart, splitting the conversation's history.
        """
        async with self.lock:
            seen = self.resolve_inbound(link)
            releasing = seen.key is not None or seen.ambiguous
            cleared: list[str] = []
            if releasing:
                # Evidence BEFORE mutation, for the reason in the docstring.
                try:
                    expected = await self._expectations.get(conversation_id)
                    owner = seen.key or next(
                        iter(self.sessions.find_mirror_sessions(link, inbound_only=True)), None
                    )
                    if owner and (expected is None or expected.retired):
                        await self._expectations.record(
                            conversation_id, owner, await title_of(owner)
                        )
                except ExpectationStoreError as exc:
                    raise ResumeReleaseError("release evidence write failed") from exc
                # Which of the occupants accepted inbound, captured BEFORE the clear so a
                # rollback restores the shape that was there rather than a wider one:
                # granting inbound to a mirror that never had it would let this
                # conversation drive a session it was only observing.
                inbound_before = set(self.sessions.find_mirror_sessions(link, inbound_only=True))
                # Free every co-located occupant, so unlink cannot leave an ambiguous
                # owner behind.
                cleared = self.sessions.clear_mirror_links_at(
                    link, reason=UNBIND_REASON_USER_UNLINK
                )
            try:
                await self.sessions.aflush()
            except Exception as exc:
                # The in-memory clear already happened, so without this the command
                # reports "not released" while the binding IS gone for this process: the
                # user's next message routes to their own session, which is the opposite
                # of what they were just told. Put it back, then raise -- a release that
                # could not be made durable must change nothing.
                for key in cleared:
                    try:
                        self.sessions.set_mirror_link(
                            key,
                            link,
                            accepts_inbound=key in inbound_before,
                            reason=UNBIND_REASON_USER_UNLINK,
                        )
                    except Exception:
                        # Nothing further to try: the refusal is already being raised, and
                        # a key that cannot be re-claimed is one another writer took in the
                        # meantime, which is a live binding rather than a lost one.
                        logger.warning(
                            "%s resume: could not restore binding %s after a failed flush",
                            self.channel_type,
                            key,
                            exc_info=True,
                        )
                logger.warning(
                    "%s resume: binding release was not durable", self.channel_type, exc_info=True
                )
                raise ResumeReleaseError("session-map flush failed") from exc
            if releasing:
                logger.info(
                    "%s: released resumed session %s (cleared bindings: %s)",
                    self.channel_type,
                    seen.key or "(ambiguous)",
                    ", ".join(cleared) or "none",
                )
            try:
                # Retire on the version CAS alone: an owner racing the flush is a NEW
                # attachment that must meet retired evidence and be announced, while a
                # newer picker record already wins by version. Gating on a live owner
                # would let a same-key rebind ride the stale record in silence.
                expected = await self._expectations.get(conversation_id)
                if expected is not None and not expected.retired:
                    await self._expectations.retire_if(conversation_id, expected.version)
            except ExpectationStoreError:
                logger.warning(
                    "%s resume: could not retire the released record; one stale detach "
                    "notice may follow",
                    self.channel_type,
                    exc_info=True,
                )
        return seen.key or (cleared[0] if cleared else None)


class SessionResumeController:
    """Own the session picker and bind transaction for every resume surface."""

    def __init__(
        self,
        sessions: Any,
        conv_log: Any | None,
        *,
        channel_type: str,
        copy: ResumeCopy,
        title_display: Any,
    ) -> None:
        self.sessions = sessions
        self.conv_log = conv_log
        self.channel_type = channel_type
        self.pickers = PickerRegistry()
        self.binder = SessionBinder(sessions, channel_type=channel_type, copy=copy)
        self.binder.title_display = title_display
        self.dashboard_state: object | None = None

    def push_slots(self) -> None:
        state = self.dashboard_state
        if state is None:
            return
        try:
            push = getattr(state, "push_slots_update", None)
            if callable(push):
                push()
        except Exception:
            logger.debug(
                "%s: slots push after binding change failed",
                self.channel_type,
                exc_info=True,
            )

    async def show_picker(
        self,
        surface: ResumeSurface,
        *,
        caller: str,
        picker_owner: str,
        is_owner: bool,
        query: str = "",
    ) -> None:
        """List eligible sessions and register only a picker that reached the surface."""
        operation = f"{self.channel_type}.sessions_data_access"
        if not is_owner:
            sel().log_api_access(
                caller=caller,
                operation=operation,
                outcome="denied",
                source=self.channel_type,
            )
            await surface.say(surface.owner_refusal)
            return
        if self.conv_log is None:
            await surface.say("⚠️ Recent sessions are unavailable.")
            return

        try:
            choices, total = await resolve_session_choices(
                self.conv_log,
                query,
                surface.display_safe,
            )
        except Exception as exc:
            sel().log_api_access(
                caller=caller,
                operation=operation,
                outcome="error",
                source=self.channel_type,
                resources="0 sessions read",
                error=surface.display_safe(str(exc), 200),
            )
            logger.exception("%s sessions: history listing failed", self.channel_type)
            await surface.say("⚠️ Recent sessions are unavailable.")
            return

        sel().log_api_access(
            caller=caller,
            operation=operation,
            outcome="allowed",
            source=self.channel_type,
            resources=f"{len(choices)} sessions read",
        )
        if not choices:
            await surface.say(surface.no_choices(query))
            return

        self.pickers.purge()
        self.pickers.drop_for(picker_owner)
        nonce = self.pickers.mint()
        frozen = tuple(choices)
        message_id = await surface.post_picker(
            surface.picker_heading(query, total),
            nonce,
            frozen,
        )
        if message_id:
            self.pickers.register(nonce, picker_owner, message_id, frozen)

    async def choose(
        self,
        surface: ResumeSurface,
        *,
        caller: str,
        picker_owner: str,
        is_owner: bool,
        message_id: str,
        nonce: str,
        index: int,
        link: ChannelLink,
    ) -> SessionChoice | None:
        """Consume one picker press and atomically claim its inbound binding."""
        if not is_owner:
            sel().log_api_access(
                caller=caller,
                operation=f"{self.channel_type}.session_resume_choice",
                outcome="denied",
                source=self.channel_type,
            )
            await surface.settle_picker(message_id, surface.choice_owner_refusal)
            return None

        choice = self.pickers.take(nonce, index, picker_owner, message_id)
        if choice is None:
            await surface.settle_picker(message_id, surface.choice_expired)
            return None
        if self.conv_log is None or not await asyncio.to_thread(
            self.conv_log.has_log,
            choice.key,
        ):
            await surface.settle_picker(message_id, surface.choice_missing)
            return None

        async with self.binder.lock:
            conflict = self.binder.binding_conflict(choice.key, choice.title, link)
            if conflict is not None:
                await surface.settle_picker(message_id, conflict)
                return None

            try:
                await self.binder.expectations.record(
                    surface.expectation_id,
                    choice.key,
                    choice.title,
                )
            except Exception:
                # Path resolution can fail before the store normalizes an OSError.
                # Every persistence failure must settle fail-closed rather than leave
                # a live-looking picker whose binding never took effect.
                logger.warning(
                    "%s resume: the pick of %s did not take effect",
                    self.channel_type,
                    choice.key,
                    exc_info=True,
                )
                await surface.settle_picker(message_id, surface.choice_expectation_failed)
                return None

            if not await surface.settle_picker(message_id, surface.choice_success(choice)):
                return None

            conflict = self.binder.binding_conflict(choice.key, choice.title, link)
            if conflict is not None:
                await surface.settle_picker(message_id, conflict)
                return None

            try:
                self.sessions.set_mirror_link(choice.key, link, accepts_inbound=True)
            except ConversationOwnershipConflict:
                logger.debug(
                    "%s resume: lost the claim race for this conversation",
                    self.channel_type,
                )
                await surface.settle_picker(message_id, surface.choice_claim_lost)
                return None
            except Exception:
                logger.exception("%s resume: failed to persist binding", self.channel_type)
                await surface.settle_picker(message_id, surface.choice_binding_failed)
                return None
            self.push_slots()

        sel().log_api_access(
            caller=caller,
            operation=f"{self.channel_type}.session_resume",
            outcome="allowed",
            source=self.channel_type,
            resources=choice.key,
        )
        return choice
