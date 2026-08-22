"""Layer 2b -- Microsoft Teams ``Renderer``.

Maps the channel-neutral ``OutputEvent`` stream (routed by the base
:class:`Renderer`'s ``dispatch``) onto Bot Framework REST calls:

* ``on_turn_start`` -- posts a ``typing`` activity for immediate feedback.
* ``on_tool_call`` -- lazily opens ONE progress message and edits it in place as
  tools run, so a long agentic turn is not silent. Teams supports updating a
  bot's own activity (``PUT .../activities/{id}``), which is what makes this
  possible here where WeCom and Weixin -- whose reply is bound to the inbound
  request -- cannot have it.
* ``on_text_chunk`` -- buffered (no typewriter streaming: Teams' native token
  streaming is 1 req/s and capped at two minutes, which a long turn exceeds).
* ``on_prompt_choice`` -- posts the Approve / Trust session / Deny Adaptive Card
  and arms its nonce on the decider, so a click resolves the awaiting prompt and
  a card from a previous run cannot.
* ``on_done`` -- delivers the final answer, reusing the progress message for the
  first chunk so no "Working…" bubble is stranded above the reply, then any local
  image the reply referenced, then any ``[OPTIONS:]`` choices as a chip card.

Splitting goes through the shared fence-safe ``split_markdown_safe`` rather than
blind fixed-width slicing, so a long reply cannot be cut through the middle of a
code fence. Text is re-scanned in its DISPLAY form before delivery, because
removing or rendering markup can reassemble a credential the driver's scan saw
as broken.

``on_done`` is also the single OUTBOUND FILE seal: an agent that writes
``![chart](/tmp/chart.png)`` gets the picture in the conversation instead of a
filesystem path. The reference scanning, the security floor and the byte budgets
belong to ``messaging/outbound_files.py``; this renderer supplies only Teams'
half -- one inline ``data:`` URI attachment per image, the narrower format
allow-list, and a visible refusal for anything that cannot go that way. Teams does
not stream, so there are no live frames that could flash the markup before the
seal replaces it.

Dependency direction is ``teams -> messaging`` (allowed).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING, Any

from kiro_crew.constants import OPTIONS_RE_TRAILER
from kiro_crew.messaging.display_safety import redact_for_display
from kiro_crew.messaging.outbound_files import (
    OutboundFile,
    Rejection,
    extract_local_refs_off_loop,
)
from kiro_crew.messaging.renderer import (
    Renderer,
    _default_redactor,
    apply_options_cap,
    new_approval_nonce,
)
from kiro_crew.messaging.split import split_markdown_safe
from kiro_crew.messaging.tables import TABLE_POLICY_CARDS
from kiro_crew.messaging.transport import TransportCapabilities
from kiro_crew.sel import sel
from kiro_crew.teams.attachments import (
    REASON_INLINE_UNDELIVERED,
    TEAMS_INLINE_IMAGE_MIMES,
    TEAMS_UPLOAD_LIMITS,
    inline_image_attachment,
    inline_image_name,
    undeliverable_rejection,
    unsupported_inline_rejection,
)
from kiro_crew.teams.cards import approval_card, options_card, resolved_card
from kiro_crew.teams.client import TeamsSendError

if TYPE_CHECKING:
    from kiro_crew.teams.approvals import TeamsApprovalDecider
    from kiro_crew.teams.client import TeamsClient

logger = logging.getLogger(__name__)

# Min seconds between outbound progress writes during a long turn. Teams'
# per-thread limit is 7 requests/second and 1800/hour, so a throttle here is a
# rate-limit guard, not just cosmetic pacing.
_PROGRESS_THROTTLE_S = 3.0

#: How often the typing indicator is re-posted while a turn runs. A Teams typing
#: activity expires after a few seconds; the reference middleware refreshes every
#: 2 s. Teams' per-thread budget is 7 requests/second, so this spends ~1% of it.
_TYPING_REFRESH_S = 4.0

_ERROR_TEXT = "⚠️ Something went wrong — please try again."

# Refusal lines appended to one answer before they are summarized. A reply that
# referenced a dozen unusable paths must explain itself without burying the answer.
_MAX_REJECTION_LINES = 3

#: What a settled card SHOWS in place of its buttons. Three outcomes, three
#: replacements, so a card in a Teams chat always states its own fate.
_OUTCOME_EXPIRED = "expired · nobody answered, so it was denied"
#: Past participle, in register with its siblings ("approved", "denied") -- a settled
#: card reads `` `label` — <outcome> ``, so a second-person phrasing breaks the pattern.
_OUTCOME_PICKED = "picked"

#: Session-key namespace of a dashboard conversation mirrored into Teams. Uploads
#: are refused for one: a dashboard slot can be incognito/restricted, and the
#: signal that says so is not resolvable from the renderer. Teams' own turns are
#: always keyed on their own namespace, so this denies nothing that works today.
_DASHBOARD_KEY_PREFIX = "dashboard:"

#: Opening of markdown image markup. A reply without it has no local reference to
#: extract, which is the whole reason a bare answer never touches the filesystem.
_IMAGE_MARKER = "!["


def _persisted_upload_root(session_key: str) -> str:
    """This session's provider working directory, or empty (blocking; offloaded).

    Read from the session map rather than from configuration: the working
    directory is per-session, recorded from the live provider's own ``cwd`` when
    the session is created, so it is the same value a dispatcher-side
    ``authorize_upload_root(provider.cwd)`` would pass. A throwaway ``SessionMap()``
    is the established read-only access pattern for it.

    This resolver is the PRIMARY path for Teams, not a fallback, because Teams
    rides the shared ``drive_turn`` pipeline: the renderer is constructed before
    the provider exists, so the dispatcher has no ``provider.cwd`` to hand over.
    Discord can use the setter only because it owns its own turn loop and acquires
    the provider itself. ``authorize_upload_root`` stays available for a caller
    that does have the provider, and takes precedence when set.

    Fails closed in every uncertain case -- no row, a relative path, an unreadable
    map -- because an unknown root means extraction has no approved boundary to
    check a reference against.
    """
    from kiro_crew.session_map import SessionMap

    root = SessionMap().get_cwd(session_key)
    return root if root and os.path.isabs(root) else ""


# Trailing "[OPTIONS: a | b | c]" chip trailer. Defined once in constants.py
# (shared across surfaces) so the ReDoS-hardened grammar can't drift.
_OPTIONS_RE = OPTIONS_RE_TRAILER


def _display_safe(text: str) -> str:
    """Redact *text* against the form Teams will RENDER (blocking; offloaded).

    The single helper behind every LLM-authored string this renderer sends —
    answer body, chip labels, the progress bubble, and a card's tool title and
    purpose. Markup the platform renders away can reassemble a credential the
    driver's byte-level scan saw as broken, so the check belongs at each sink.
    """
    safe, _ = redact_for_display(text or "", _default_redactor)
    return safe


def _extract_options(text: str) -> tuple[str, list[str]]:
    """Split text into ``(body, options)`` for a trailing ``[OPTIONS:]`` chip list.

    Teams renders the choices as Adaptive Card actions, so the trailer is parsed
    rather than dropped. A partial ``[OPTIONS…`` fragment (no closing ``]``) is
    held back so it never lands as raw text.
    """
    m = _OPTIONS_RE.search(text)
    if m:
        body = text[: m.start()].rstrip()
        return body, [o.strip() for o in m.group(1).split("|") if o.strip()]
    idx = text.rfind("[OPTIONS")
    if idx != -1 and "]" not in text[idx:]:
        return text[:idx].rstrip(), []
    return text, []


def _strip_options(text: str) -> str:
    """The body of *text* with any ``[OPTIONS:]`` trailer removed."""
    return _extract_options(text)[0]


class TeamsRenderer(Renderer):
    """Renders a turn to a Teams conversation: typing -> progress -> answer."""

    channel_type = "teams"

    def __init__(
        self,
        client: "TeamsClient",
        conversation_id: str,
        service_url: str,
        capabilities: TransportCapabilities,
        *,
        session_key: str = "",
        decider: "TeamsApprovalDecider | None" = None,
        upload_root: str = "",
    ) -> None:
        super().__init__(capabilities)
        self._client = client
        self._conversation_id = conversation_id
        self._service_url = service_url
        self._session_key = session_key
        self._decider = decider
        if decider is not None:
            # The decider knows a prompt expired; only the renderer knows where its
            # card is. This is the wire between them.
            decider.on_expired = self._settle_expired
        #: request id -> (card activity id, tool title), so an answered prompt's
        #: card can be replaced with its outcome instead of staying clickable.
        self._pending_prompts: dict[str, tuple[str, str]] = {}
        #: Nonce + labels of the options card this turn posted, so a chip click is
        #: resolved against what was actually offered rather than its own payload.
        self._option_nonce = ""
        self._option_labels: list[str] = []
        #: Activity id of the chips card, so a pick can replace the buttons with the
        #: choice instead of leaving every chip looking live.
        self._option_card_id = ""
        self._buf: list[str] = []
        self._last_progress = 0.0
        self._started = False
        self._finalized = False
        # Holds the typing-refresh task so it is not garbage collected mid-flight.
        # Cancelled when the turn finalizes.
        self._typing_task: asyncio.Task[None] | None = None
        # Whether the LAST _post_card reached the conversation, regardless of
        # whether Teams returned an editable id.
        self._card_posted = False
        # Activity id of the reusable progress message, when one was opened and
        # Teams returned an id we can edit.
        self._progress_id: str = ""
        # Whether opening the progress message has been ATTEMPTED. Separate from
        # ``_progress_id`` because Teams withholds the id when it splits an
        # activity: without this flag a failed open would be retried on every
        # subsequent tool call, posting a fresh bubble each time.
        self._progress_opened = False
        self._tool_title = ""
        # Approved root for outbound file extraction: the provider's working
        # directory. Absolute or nothing -- a relative root is no boundary at all.
        self._upload_root = upload_root if os.path.isabs(upload_root) else ""
        self._upload_root_resolved = bool(self._upload_root)

    def authorize_upload_root(self, root: str) -> None:
        """Authorize the provider's resolved cwd as the extraction boundary.

        A caller holding the live provider should use this; it takes precedence
        over the persisted lookup below and is the same contract Discord's renderer
        exposes. A non-absolute root leaves uploads disabled.
        """
        self._upload_root = root if os.path.isabs(root) else ""
        self._upload_root_resolved = True

    # -- lifecycle ----------------------------------------------------------
    async def on_turn_start(self) -> None:
        if self._started:  # idempotent (dispatch + driver both call it)
            return
        self._started = True
        await self._client.send_typing(self._conversation_id, self._service_url)
        # A Teams typing indicator lasts a few seconds, so ONE activity leaves a
        # turn that runs for minutes showing dots briefly and then a silent chat --
        # indistinguishable from a dead bot. Refreshed on a timer for the same reason
        # the reference middleware does (every 2 s there; the per-thread budget is 7
        # sends/second, so this costs ~1% of it). The progress bubble only covers a
        # turn that CALLS a tool; a long pure generation or a /compact has nothing
        # else to show.
        self._typing_task = asyncio.create_task(self._keep_typing())

    async def _keep_typing(self) -> None:
        """Re-post the typing indicator until the turn ends. Never raises."""
        try:
            while not self._finalized:
                await asyncio.sleep(_TYPING_REFRESH_S)
                if self._finalized:
                    return
                try:
                    await self._client.send_typing(self._conversation_id, self._service_url)
                except TeamsSendError:
                    # A transient failure costs one missed refresh, not the turn.
                    logger.debug("Teams: typing refresh failed", exc_info=True)
        except asyncio.CancelledError:
            raise

    async def on_text_chunk(self, text: str) -> None:
        # Buffered: Teams' native streaming is throttled to 1 request/second and
        # dies at a hard two-minute ceiling, which an agentic turn routinely
        # exceeds -- a stream that dies mid-answer is worse than one message.
        self._buf.append(text)

    async def on_thinking(self, text: str) -> None:
        # Teams does not surface reasoning inline: the edit budget is spent on tool
        # progress and the answer, which is Webex's reason too. Discord, Telegram and
        # iMessage also no-op here; WeCom is the one channel that does surface it,
        # because its native <think> block costs no extra message.
        return None

    async def on_tool_call(
        self, tool_call_id: str, title: str, tool_kind: str = "", tool_purpose: str = ""
    ) -> None:
        """Surface tool progress in one message, edited in place.

        The FIRST tool call always writes: a tool call means real work started, so
        suppressing it would leave a long turn showing nothing between the typing
        indicator and the answer. Later calls are throttled, because Teams allows
        only 7 requests/second per thread.
        """
        self._tool_title = title or self._tool_title
        now = time.monotonic()
        if self._progress_opened and now - self._last_progress < _PROGRESS_THROTTLE_S:
            return
        self._last_progress = now
        await self._write_progress(f"🔧 {self._tool_title}…")

    async def on_prompt_choice(
        self,
        options: list[dict[str, Any]],
        request_id: str | int,
        tool_title: str = "",
        tool_purpose: str = "",
        tool_input: str = "",
    ) -> None:
        """Post the Approve / Trust session / Deny card for one tool request.

        ``tool_input`` is accepted and not rendered: the card already carries the
        tool's title and purpose, and adding its arguments is a card-layout change
        rather than something this signature widening should decide.

        The nonce is armed on the decider BEFORE the card is posted: a click can
        arrive as soon as the card renders, and a resolve against an un-armed
        prompt is refused as stale.
        """
        if self._decider is None:
            # The driver only dispatches this with a decider, so reaching here
            # means the turn is running deny-by-default and buttons would be dead
            # controls.
            logger.debug("Teams: prompt_choice with no decider; nothing to render")
            return
        rid = str(request_id)
        title = ""
        purpose = ""
        for option in options:
            if isinstance(option, dict):
                title = title or str(option.get("title") or option.get("name") or "")
                purpose = purpose or str(option.get("purpose") or option.get("description") or "")
        # The request's OWN title outranks both: `_tool_title` is the last
        # tool_call seen and is never cleared, so it names the PREVIOUS tool for
        # any permission that arrives without one of its own. The options list is
        # kept as the next fallback since it can carry a per-option label.
        title = tool_title or title or self._tool_title or "this tool"
        purpose = purpose or tool_purpose
        # A tool title and purpose are LLM-authored and land in a card TextBlock
        # that Teams markdown-renders, so they go through the same display-form
        # scan as the answer and the progress bubble. The driver's byte-level pass
        # sees `AKIA**…**` as broken; the rendered card would show it whole.
        title = await asyncio.to_thread(_display_safe, title)
        purpose = await asyncio.to_thread(_display_safe, purpose) if purpose else ""
        nonce = new_approval_nonce()
        self._decider.arm(rid, nonce)
        card = approval_card(title=title, purpose=purpose, request_id=rid, nonce=nonce)
        activity_id = await self._post_card(card)
        if not activity_id and not self._card_posted:
            # The card never reached the conversation, so nobody can click it. Refuse
            # NOW rather than parking the turn for the full approval window behind a
            # prompt the user never saw, then denying with no explanation.
            self._decider.abandon(rid)
            await self._say(
                "⚠️ Could not show the approval prompt for "
                f"`{title}`, so the tool was not run. Try again."
            )
            return
        # Remember where the card landed so the answered outcome can REPLACE it.
        # A prompt whose buttons stay live after it was answered invites a second
        # click that resolves nothing.
        self._pending_prompts[rid] = (activity_id, title)

    async def on_compaction(self, context_usage_pct: float) -> None:
        logger.debug("Teams: compaction status %.0f%%", context_usage_pct)

    async def on_done(self, stop_reason: str = "") -> None:
        if self._finalized:
            return
        self._finalized = True
        # Before the first await below: the flag is what stops the keepalive loop,
        # and cancelling here means no typing activity races the answer.
        self._stop_typing()
        ok = stop_reason != "error"
        content, choices, files = await self._display_text()
        if not content and not choices and not files:
            content = "…" if ok else _ERROR_TEXT
        # Overflow past max_buttons is appended to the body as a numbered list by
        # the shared cap, so the user still learns those choices exist.
        content, kept = apply_options_cap(content, choices, self.capabilities)
        chunks = await asyncio.to_thread(
            split_markdown_safe, content, self.capabilities.max_message_chars
        ) or ([] if (kept or files) else ["…"])
        for index, chunk in enumerate(chunks):
            # Reuse the progress bubble for the first chunk so a turn that showed
            # "🔧 …" does not leave it stranded above the answer.
            if index == 0 and self._progress_id:
                if await self._client.update_message(
                    self._conversation_id, self._progress_id, chunk, self._service_url
                ):
                    continue
            try:
                await self._client.send_message(self._conversation_id, chunk, self._service_url)
            except TeamsSendError:
                # Stop at the first failed chunk -- skipping ahead would splice a gap
                # into the middle of the answer, and a conversation that cannot take
                # the text will not take an attachment either -- and then RAISE.
                #
                # Returning here is what made the gateway record an answer the user
                # never received: `drive_turn` treats a clean return from the renderer
                # as delivery, so it ran `record_success` and persisted the FULL text
                # while the Connector had refused part or all of it. Raising skips both
                # and records a failure, which is the honest outcome and the one the
                # user can retry. It costs the delivered prefix its transcript entry;
                # a turn silently recorded as complete costs the ability to notice at
                # all.
                logger.warning("Teams: answer chunk delivery failed; stopping", exc_info=True)
                raise
        if files:
            # After the text, so an image lands next to the prose that introduced
            # it rather than above the answer it belongs to.
            await self._send_inline_images(files)
        if kept:
            # Chips ride their own card AFTER the answer, so a failed answer
            # delivery above never leaves buttons floating with nothing to act on.
            nonce = new_approval_nonce()
            self._option_nonce = nonce
            self._option_labels = list(kept)
            self._option_card_id = await self._post_card(
                options_card(prompt="", options=kept, nonce=nonce)
            )
            if not self._card_posted:
                # The chips never landed AND the trailer was already stripped from
                # the body, so without this the choices are simply gone. Degrade to
                # the same numbered list the shared options cap uses for overflow,
                # and drop the nonce so no later click resolves against a card that
                # does not exist.
                self._option_nonce = ""
                self._option_labels = []
                listed = "\n".join(f"{n}. {label}" for n, label in enumerate(kept, 1))
                await self._say(f"Pick one by replying with its text:\n{listed}")

    @property
    def has_pending_choices(self) -> bool:
        """Whether this turn left tappable choices a later click must resolve.

        An approval card resolves DURING its turn -- the turn is blocked on the
        decider -- but an ``[OPTIONS:]`` chip is posted at ``on_done`` and answered
        whenever the user gets to it, which is after the turn has finished. The
        dispatcher reads this to decide whether the renderer must outlive the turn;
        retiring it immediately is what makes an advertised chip unclickable.
        """
        return bool(self._option_nonce)

    def option_label(self, nonce: str, index: str) -> str:
        """The label an options click refers to, or empty when it is stale.

        The label is resolved from state THIS process holds rather than trusted
        from the payload, so a fabricated or replayed submit cannot inject text
        into the conversation as if the user had typed it.
        """
        if not self._option_nonce or nonce != self._option_nonce:
            return ""
        position = int(index)
        return self._option_labels[position] if position < len(self._option_labels) else ""

    def _stop_typing(self) -> None:
        """Cancel the typing keepalive. Idempotent."""
        task, self._typing_task = self._typing_task, None
        if task is not None and not task.done():
            task.cancel()

    async def close(self) -> None:
        """Idempotent teardown: finalize the turn if it never reached on_done."""
        if not self._finalized:
            await self.on_done(stop_reason="error")
        # Belt and braces: on_done stops it, but close is the ONE call every path
        # reaches, and an orphaned refresh loop would keep posting into a finished
        # conversation for the process lifetime.
        self._stop_typing()

    async def settle_prompt(self, request_id: str, outcome: str) -> None:
        """Replace an answered prompt's card with its outcome.

        Called by the dispatcher once a click has been resolved. Best-effort: the
        decision is already recorded, so a failed edit costs a stale-looking card
        rather than the answer.
        """
        entry = self._pending_prompts.pop(str(request_id), None)
        if entry is None:
            return
        activity_id, title = entry
        if not activity_id:
            return
        try:
            await self._client.update_card(
                self._conversation_id,
                activity_id,
                resolved_card(title=title, outcome=outcome),
                self._service_url,
            )
        except TeamsSendError:
            logger.debug("Teams: prompt card settle failed", exc_info=True)

    async def _settle_expired(self, request_id: str) -> None:
        """Replace a prompt whose click window closed. Wired onto the decider."""
        await self.settle_prompt(request_id, _OUTCOME_EXPIRED)

    async def settle_options(self, label: str) -> None:
        """Replace the chips card with the choice, so no chip still looks live.

        Also clears the nonce, which is what retires this renderer: the dispatcher
        keeps it alive only while chips are outstanding.
        """
        activity_id, self._option_card_id = self._option_card_id, ""
        self._option_nonce = ""
        self._option_labels = []
        if not activity_id:
            return
        safe = await asyncio.to_thread(_display_safe, label)
        try:
            await self._client.update_card(
                self._conversation_id,
                activity_id,
                resolved_card(title=safe, outcome=_OUTCOME_PICKED),
                self._service_url,
            )
        except TeamsSendError:
            logger.debug("Teams: options card settle failed", exc_info=True)

    # -- helpers ------------------------------------------------------------
    async def _post_card(self, card: dict[str, Any]) -> str:
        """Post an Adaptive Card; return its activity id (empty when unknown).

        Sets :attr:`_card_posted` so a caller can tell "delivered, but Teams withheld
        the id" from "not delivered" -- the two need opposite handling and both come
        back as an empty string.
        """
        self._card_posted = False
        try:
            activity_id = (
                await self._client.send_card(self._conversation_id, card, self._service_url) or ""
            )
        except TeamsSendError:
            logger.warning("Teams: card delivery failed", exc_info=True)
            return ""
        self._card_posted = True
        return activity_id

    async def _say(self, body: str) -> None:
        """Send one short out-of-band notice. Best effort, never raises."""
        try:
            await self._client.send_message(self._conversation_id, body, self._service_url)
        except TeamsSendError:
            logger.debug("Teams: notice delivery failed", exc_info=True)

    async def _write_progress(self, body: str) -> None:
        """Open or update the single progress message. Never fails a turn.

        The body carries an LLM-authored tool title and is sent with
        ``textFormat: markdown``, so it goes through the SAME display-form
        redaction as the answer. Without it a credential split by emphasis markup
        (``AKIA**…**``) is broken to the byte-level scanner and whole on screen --
        the progress bubble would be the one hole in the chokepoint.
        """
        body, redacted = await asyncio.to_thread(redact_for_display, body, _default_redactor)
        if redacted:
            logger.warning("Teams: redacted credential material from a progress update")
        try:
            if self._progress_id:
                await self._client.update_message(
                    self._conversation_id, self._progress_id, body, self._service_url
                )
                return
            if self._progress_opened:
                # The open already ran and produced no editable id; posting again
                # would add a fresh bubble on every later tool call.
                return
            self._progress_opened = True
            sent = await self._client.send_message(self._conversation_id, body, self._service_url)
            # Teams withholds the id when it splits an activity; without one the
            # message cannot be edited, so leave _progress_id empty and let the
            # answer arrive as its own message rather than editing the wrong one.
            self._progress_id = sent or ""
        except TeamsSendError:
            logger.debug("Teams: progress write failed", exc_info=True)

    # -- outbound files -----------------------------------------------------
    async def _upload_root_for_turn(self) -> str:
        """The approved extraction root, resolved once per renderer.

        Resolved LAZILY: only a reply that actually references a local file pays
        for it, and by the time one does the session exists, so the persisted
        working directory is present.
        """
        if self._upload_root_resolved:
            return self._upload_root
        self._upload_root_resolved = True
        if not self._session_key or self._session_key.startswith(_DASHBOARD_KEY_PREFIX):
            return ""
        try:
            self._upload_root = await asyncio.to_thread(_persisted_upload_root, self._session_key)
        except Exception:
            logger.warning("Teams: could not resolve the upload root", exc_info=True)
            self._upload_root = ""
        return self._upload_root

    async def _extract_uploads(self, text: str) -> tuple[str, list[OutboundFile]]:
        """Pull inlinable images out of one sealed answer. Never raises.

        Runs off the event loop (extraction stats and reads files) and fails soft:
        a reply must still go out when extraction cannot, and leaving the markup in
        place is the degradation that keeps the path visible.

        Two refusal sources are folded together here so the answer carries them
        both: the neutral module's (sensitive path, symlink, not-a-raster, over the
        per-file ceiling -- all of which keep their markdown) and this channel's own
        (a real raster Teams cannot render inline).
        """
        root = await self._upload_root_for_turn()
        if not root:
            # Fail closed: with no approved boundary there is nothing to check a
            # reference against, so the reply keeps its markup.
            return text, []
        try:
            result = await extract_local_refs_off_loop(
                text, within_root=root, limits=TEAMS_UPLOAD_LIMITS
            )
        except Exception:
            logger.warning("Teams: outbound file extraction failed", exc_info=True)
            return text, []
        files: list[OutboundFile] = []
        rejections = list(result.rejections)
        for candidate in result.files:
            if candidate.mime in TEAMS_INLINE_IMAGE_MIMES:
                files.append(candidate)
            else:
                rejections.append(unsupported_inline_rejection(candidate))
        self._audit_uploads(rejections, files)
        body = result.rewritten_text.strip()
        if not body and not files:
            # Nothing was extracted and nothing is left: send the original.
            body = text
        return self._append_rejections(body, rejections), files

    def _audit_uploads(self, rejections: list[Rejection], files: list[OutboundFile]) -> None:
        """Record the outcome with COUNTS and reason codes only.

        Never the destination or the file name: the destination is LLM-authored and
        the name is user-adjacent, and an audit log is not the place to persist
        either.
        """
        if rejections:
            sel().log_api_access(
                caller=self._session_key or "teams",
                operation="teams_renderer.upload_files",
                outcome="denied",
                source="teams",
                resources=f"{len(rejections)} rejection(s)",
                error=",".join(sorted({item.reason for item in rejections})),
            )
        if files:
            sel().log_api_access(
                caller=self._session_key or "teams",
                operation="teams_renderer.upload_files",
                outcome="allowed",
                source="teams",
                resources=f"{len(files)} file(s)",
            )

    def _append_rejections(self, body: str, rejections: list[Rejection]) -> str:
        """Append refusal reasons, summarizing past the line budget.

        Appended BEFORE display redaction, so a refusal line that quotes an
        LLM-authored destination is scanned like the rest of the answer.

        Deliberately NOT budgeted against ``max_message_chars``: the image markdown
        this note explains has already been cut out of the answer, so dropping the
        note to stay inside one message is the one outcome that leaves the user with
        neither the picture nor a reason. Every caller chunks, so an over-cap body
        costs an extra message rather than a lost line.
        """
        if not rejections:
            return body
        for rejection in rejections:
            logger.info("Teams: local image not sent (%s)", rejection.reason)
        lines = [f"⚠️ {rejection}" for rejection in rejections[:_MAX_REJECTION_LINES]]
        if len(rejections) > _MAX_REJECTION_LINES:
            lines.append(f"⚠️ …and {len(rejections) - _MAX_REJECTION_LINES} more")
        note = "\n".join(lines)
        return f"{body}\n\n{note}" if body else note

    async def _send_inline_images(self, files: list[OutboundFile]) -> None:
        """Deliver each image as its own activity, reporting any that did not land.

        One activity per image because Teams SPLITS an activity carrying both text
        and an attachment and withholds its id, and its own guidance is to send
        separate activities rather than rely on that split.

        A failed image is NOT silent: its markdown was already cut out of the
        answer, so the failure is reported in a short follow-up naming the path.
        """
        failures: list[Rejection] = []
        for file in files:
            caption, _ = await asyncio.to_thread(redact_for_display, file.alt, _default_redactor)
            attachment = await asyncio.to_thread(
                inline_image_attachment, file, inline_image_name(caption, file)
            )
            try:
                await self._client.send_inline_image(
                    self._conversation_id, attachment, self._service_url
                )
            except TeamsSendError:
                logger.warning("Teams: inline image delivery failed", exc_info=True)
                failures.append(undeliverable_rejection(file))
        if not failures:
            return
        sel().log_api_access(
            caller=self._session_key or "teams",
            operation="teams_renderer.upload_files",
            outcome="denied",
            source="teams",
            resources=f"{len(failures)} file(s)",
            error=REASON_INLINE_UNDELIVERED,
        )
        note = self._append_rejections("", failures)
        safe, _ = await asyncio.to_thread(redact_for_display, note, _default_redactor)
        # Chunked like any other text: a refusal line quotes an LLM-authored path,
        # so the note has no bound of its own and a single over-cap activity would
        # be refused whole -- losing the only record that the image went missing.
        chunks = await asyncio.to_thread(
            split_markdown_safe, safe, self.capabilities.max_message_chars
        )
        for chunk in chunks:
            try:
                await self._client.send_message(self._conversation_id, chunk, self._service_url)
            except TeamsSendError:
                logger.warning("Teams: could not report an undelivered image", exc_info=True)
                return

    async def _display_text(self) -> tuple[str, list[str], list[OutboundFile]]:
        """The turn's answer body, its ``[OPTIONS:]`` choices, and its images.

        Everything delivered is display-form redacted. The driver already redacted
        the stream, but removing the trailer, cutting image markup and letting Teams
        render markdown can each reassemble a credential the scan saw as broken, so
        the delivered form is re-scanned at this single chokepoint -- the choices
        too, because a chip label is rendered text like any other.

        Extraction runs BEFORE that scan, so the transformed body and every appended
        refusal are covered by it.
        """
        body, choices = _extract_options("".join(self._buf).strip())
        if not body and not choices:
            return "", [], []
        body = self._adapt_tables(body)
        files: list[OutboundFile] = []
        # ``_IMAGE_MARKER`` is the cheapest sound pre-check: extraction only ever
        # acts on ``![…](…)``, so a reply without the marker needs neither the root
        # lookup nor the scan. Wrong only in the safe direction.
        if body and self.capabilities.files_outbound and _IMAGE_MARKER in body:
            body, files = await self._extract_uploads(body)
        safe, redacted = await asyncio.to_thread(redact_for_display, body, _default_redactor)
        safe_choices: list[str] = []
        for choice in choices:
            clean, hit = await asyncio.to_thread(redact_for_display, choice, _default_redactor)
            redacted = redacted or hit
            if clean.strip():
                safe_choices.append(clean.strip())
        if redacted:
            logger.warning("Teams: redacted credential material from an outbound answer")
        return safe, safe_choices, files

    def _adapt_tables(self, body: str) -> str:
        """Render markdown tables into the form Teams actually displays.

        Runs on the extracted body BEFORE display redaction, so a generated grid is
        scanned like any other delivered text, and before upload extraction, so image
        markup a table carried is still there to find.

        The cap logic is a one-message question, not a splitting one: a generated grid
        has to arrive whole, so one that would not fit is retried under the cards policy
        and then degraded to display-safe raw text, which the fence-safe splitter below
        may chunk freely.
        """
        content = self.render_tables_for_target(body)
        cap = self.capabilities.max_message_chars
        if cap > 0 and content != body and len(content) > cap:
            content, generated_grid = self.render_tables_for_target_with_metadata(
                body,
                policy=TABLE_POLICY_CARDS,
            )
            if generated_grid and len(content) > cap:
                safe_raw = self.safe_raw_table_fallback(body, policy=TABLE_POLICY_CARDS)
                if safe_raw is not None:
                    content = safe_raw
        return content

    def text(self) -> str:
        """The turn's visible answer so far (OPTIONS stripped)."""
        return _strip_options("".join(self._buf).strip())
