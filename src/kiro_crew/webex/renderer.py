"""Layer 2b -- Webex ``Renderer``.

Maps the channel-neutral ``OutputEvent`` stream (routed by the base
:class:`Renderer`'s ``dispatch``) onto Webex REST calls.

**The 10-edit budget is the whole design.** Webex caps a message at 10 edits
and returns 400 past that, so a typewriter edit-stream is structurally
impossible, not merely unimplemented. Worse, progress signalling and answer
fidelity are the SAME scarce resource here: Webex has no typing indicator and no
reactions API, so every "I am working" signal is spent from the same 10 edits.
The budget is therefore split once, explicitly: ``_STATUS_EDIT_BUDGET`` frames
of progress and ``_RESERVED_FINAL_EDITS`` held back so the final answer always
has an edit left.

* ``on_turn_start`` -- posts a "🤔 Thinking…" placeholder message.
* ``on_text_chunk`` -- buffered, and the buffer's tail rides the next status
  frame so a long agentic turn shows the answer forming rather than a bare tool
  name followed minutes later by everything at once.
* ``on_tool_call`` -- refreshes the status frame (throttled AND budgeted).
* ``on_prompt_choice`` -- posts its OWN message with an Approve/Deny Adaptive
  Card AND the typed-1/2 text. Never an edit: the placeholder is mid-turn and its
  budget belongs to the answer, and Webex refuses to edit a message once it
  carries an attachment, so the card could not be retired in place either --
  which is why each card carries a nonce instead.
* ``on_steer_consumed`` -- records the fold so ``on_done`` can mark it.
* ``on_compaction`` -- logged only; the dispatcher surfaces threshold notices as
  separate messages post-turn.
* ``on_done`` -- edits the placeholder into the final answer (one edit), falling
  back to a fresh message if the edit fails; overflow past the message-size cap
  goes out as follow-up messages.

Dependency direction is ``webex -> messaging`` (allowed).
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Callable

from kiro_crew.messaging.display_safety import redact_for_display
from kiro_crew.messaging.outbound_files import (
    ExtractLimits,
    extract_local_refs_off_loop,
    hide_local_refs,
    upload_filename,
)
from kiro_crew.messaging.renderer import (
    Renderer,
    apply_options_cap,
    new_approval_nonce,
    split_options_trailer,
)
from kiro_crew.messaging.split import chunk_utf8_bytes
from kiro_crew.messaging.tables import TABLE_POLICY_CARDS
from kiro_crew.messaging.transport import TransportCapabilities
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.webex.cards import approval_card, options_card, usable_choices
from kiro_crew.webex.client import WEBEX_MAX_TEXT

if TYPE_CHECKING:
    from kiro_crew.webex.client import WebexClient

logger = logging.getLogger(__name__)

# Webex allows 10 edits per message. Split that budget ONCE, here, so the
# arithmetic is readable rather than implied by a single constant: status frames
# get the first slice and the rest is held back for the final answer. A status
# frame that FAILS burns the whole remaining status budget (an edit-cap or
# rate-limit 400 means further status edits will fail too), so the final edit
# can never race the cap.
_WEBEX_EDIT_CAP = 10
# One edit for the answer itself, plus one of margin: a follow-up chunk is a new
# message rather than an edit, so the answer path spends exactly one.
_RESERVED_FINAL_EDITS = 2
_STATUS_EDIT_BUDGET = _WEBEX_EDIT_CAP - _RESERVED_FINAL_EDITS

# Status frames are paced by a DOUBLING interval, not a flat one. The budget is
# a handful of edits against a turn that can run for minutes, so a flat throttle
# spends every frame in the first few seconds and then the indicator is frozen
# for the rest of the turn — the exact window where the user most needs to know
# something is still happening. Doubling from _STATUS_THROTTLE_S spreads the
# budget across roughly eight minutes, dense early (when a turn usually
# resolves) and sparse later (when it has not). The growth needs no ceiling of
# its own: _STATUS_EDIT_BUDGET bounds how many times it can double.
_STATUS_THROTTLE_S = 2.0

# How much of the forming answer rides a status frame. Small on purpose: the
# frame is a progress signal, and a near-cap frame would leave no room for the
# tool label and risk overshooting the byte limit.
_STATUS_TAIL_CHARS = 600

# Outbound upload budgets. Webex allows 100 MB per attachment, far above what a
# chat reply should ship: the per-file ceiling is what stops one generated image
# from becoming a 100 MB post, and the aggregate bounds the memory a single
# message can hold while its uploads are in flight.
_UPLOAD_LIMITS = ExtractLimits(max_file_bytes=8 * 1024 * 1024, max_total_bytes=32 * 1024 * 1024)


def _redact_all(text: str) -> str:
    """Both redactors as one callable, for the display-form pass.

    Defined here rather than imported from the shared renderer's private helper:
    Discord does the same, and a channel's outbound redactor is a decision that
    belongs to the channel.
    """
    out, _ = redact_exfiltration_urls(text)
    out, _ = redact_credentials(out)
    return out


def webex_display_safe(text: str) -> str:
    """Redact *text* against what WEBEX will show, not against its bytes.

    The driver's ``StreamRedactor`` already scans the stream byte-for-byte. This
    is the second pass every other rendering channel runs (``discord`` and
    ``imessage`` do the same), and it exists because a credential split by
    markdown delimiters — ``AKIA**IOSF**ODNN7EXAMPLE`` — survives a byte scan and
    is then reassembled by the platform's own renderer into the whole key.
    ``redact_for_display`` canonicalizes to the DISPLAYED form first, so the scan
    sees what the user will.

    Deliberately WITHOUT the mention defang that ``messaging.renderer.display_safe``
    adds: that inserts a zero-width space after every ``@`` to neutralize
    ``@everyone``-style broadcast grammars, and Webex has none — while its
    allow-list is email addresses, so defanging would mangle every address the
    agent legitimately prints.
    """
    out, _ = redact_for_display(text or "", _redact_all)
    return out


# Placeholder shown immediately while the agent is still generating.
_THINKING = "🤔 Thinking…"

_ERROR_TEXT = "⚠️ Something went wrong — please try again."

_STEER_NOTE = "↪️ _folded in your mid-turn message._"

# Shown when a follow-up chunk of a multi-message answer could not be delivered.
# Without it the answer simply stops mid-sentence and anything posted after it (an
# upload, a choice card) reads as attached to a reply the user believes is whole.
_TRUNCATED_NOTE = "⚠️ _The rest of this answer could not be delivered — ask me to continue._"

# Header for the reference-restoring message after a failed upload. Extraction has
# already taken the markup out of the answer, so without this the file is missing
# AND unmentioned.
_UPLOAD_FAILED_HEADER = "⚠️ Couldn't upload these — they're on the machine at:"

# Lead-in on the choice card's message. Webex REQUIRES text or markdown alongside
# a card, and that text is what a client which cannot render one receives — so the
# numbered choices are appended to it rather than living only on the buttons.
_CHOICE_PROMPT = "Pick one, or just reply:"

# How much of a tool title the approval prompt shows. The message itself is
# byte-capped and truncated by the send path, so an uncapped title would push
# "Reply 1 to approve or 2 to deny" off the end of the security prompt.
_TOOL_LABEL_MAX = 120

# Markdown and Adaptive Card control characters. A tool title comes from the
# TOOL_CALL event, which the driver only credential-redacts, so it is
# model-influenced text about to be interpolated into a genuine markdown body —
# and into a card TextBlock, which renders a markdown SUBSET including
# ``[text](url)`` with no per-block toggle. A backtick closes the code span the
# prompt wraps it in, a link plants a clickable target inside the one message the
# user is being asked to trust, and ``{}`` is card templating syntax.
#
# ``_`` is deliberately NOT in the set: real tool names are ``fs_write``,
# ``execute_bash``, ``mcp__server__tool``, and stripping it renders the label
# ``fswrite`` — so the user can no longer tell WHICH tool they are approving,
# which is the entire job of this string. Emphasis is cosmetic anyway: it cannot
# remove text or create a target, and inside the body's code span an underscore
# is literal.
_MD_CONTROL_RE = re.compile(r"[*`\[\]()<>{}]")


def _safe_tool_label(raw: str) -> str:
    """A tool title that cannot style, link, or overflow the approval prompt.

    Collapse whitespace, strip markdown/card control characters, then cap. In that
    order: stripping after the cap could leave a dangling bracket at the boundary.

    Then redacted, and that ORDER is the point: the strip is itself the
    reassembling transformation the display redactor exists for. A title of
    ``AKIA**IOSF**ODNN7EXAMPLE`` does not match a credential pattern as the driver
    streamed it, and removing the ``*`` characters — which this function does, on
    purpose — hands the room the intact key. So the scan runs on the form that
    actually ships.
    """
    stripped = _MD_CONTROL_RE.sub("", " ".join((raw or "").split()))[:_TOOL_LABEL_MAX]
    return webex_display_safe(stripped)


class WebexRenderer(Renderer):
    """Renders a turn to a Webex room: placeholder -> status frames -> answer."""

    channel_type = "webex"

    def __init__(
        self,
        client: "WebexClient",
        room_id: str,
        capabilities: TransportCapabilities,
        *,
        thread_id: str = "",
        uploads_allowed: bool = True,
        mint_approval_nonce: Callable[[str], str] | None = None,
        publish_choices: Callable[[str, list[str]], None] | None = None,
    ) -> None:
        super().__init__(capabilities)
        self._client = client
        self._room_id = room_id
        # Thread root to reply under, when the inbound message had one. Webex
        # threads are flat, so this is the root rather than the immediate parent.
        self._thread_id = thread_id or None
        self._buf: list[str] = []
        self._placeholder_id: str | None = None
        self._edits_used = 0
        self._last_status = 0.0
        self._throttle = _STATUS_THROTTLE_S
        self._last_frame = ""
        self._tool = ""
        self._steered = False
        # ``(nonce, choices) -> None``, supplied by the dispatcher: an options
        # card is the last thing a turn sends, so the store that resolves its
        # press has to outlive this renderer. None means no card is rendered.
        self._publish_choices = publish_choices
        # ``(request_id) -> nonce``, supplied by the dispatcher so the approval
        # REGISTRY owns the nonce lifetime: it is retired by the same window that
        # retires the decision, where a renderer-owned nonce could outlive it.
        # None means no card is attached to an approval prompt.
        self._mint_approval_nonce = mint_approval_nonce
        # Uploads need all three: the declared capability, a room where a local
        # file is not disclosed beyond the allow-list, and a trusted root.
        self._uploads_allowed = uploads_allowed
        self._upload_root = ""
        self._started = False
        self._finalized = False

    # -- lifecycle ----------------------------------------------------------
    async def on_turn_start(self) -> None:
        if self._started:  # idempotent (dispatch + driver both call it)
            return
        self._started = True
        self._placeholder_id = await self._client.send_message(
            self._room_id, _THINKING, parent_id=self._thread_id
        )
        self._last_status = time.monotonic()

    async def on_text_chunk(self, text: str) -> None:
        # Buffered: the 10-edit cap rules out typewriter streaming. The buffer's
        # tail rides the next status frame instead.
        self._buf.append(text)
        await self._status_frame()

    async def on_thinking(self, text: str) -> None:
        # Webex does not surface reasoning inline: the 10-edit cap is spent on
        # tool progress and the final answer, so a reasoning edit would cost one of
        # those. Not a platform limit -- WeCom streams reasoning because its
        # <think> block costs it no extra frame.
        return None

    async def on_tool_call(
        self, tool_call_id: str, title: str, tool_kind: str = "", tool_purpose: str = ""
    ) -> None:
        """Refresh the status frame with the running tool."""
        self._tool = title or tool_kind or "tool"
        await self._status_frame()

    async def on_prompt_choice(
        self,
        options: list[dict[str, Any]],
        request_id: str | int,
        tool_title: str = "",
        tool_purpose: str = "",
        tool_input: str = "",
    ) -> None:
        """Ask for the approval decision as a typed reply.

        Its OWN message, for two reasons: the placeholder is mid-turn and its
        remaining edits belong to the answer, and a prompt has to survive as
        readable history after the turn ends.

        The PRIMARY affordance is a typed ``1``/``2`` — a plain message on every
        channel that needs nothing from the platform — with an Adaptive Card
        riding alongside it when the transport declares ``rich_blocks``. The
        dispatcher intercepts either while the session is busy; see
        ``WebexDispatcher``.
        """
        # The request's OWN tool title first, per the Renderer contract: a
        # permission is not always immediately preceded by its own titled tool
        # call, so ``self._tool`` (the last ``on_tool_call`` seen) can name the
        # PREVIOUS tool and the operator would consent to the wrong one. Fall back
        # to it only when the ask carries no title. Both are LLM-authored, so both
        # go through ``_safe_tool_label``.
        tool = _safe_tool_label(tool_title or self._tool) or "this tool"
        rid = str(request_id)
        # The text prompt is NOT a fallback for a missing card — it is the primary
        # affordance, and it always ships. The card rides alongside it because the
        # inbound half of a press travels over the undocumented device websocket:
        # if that frame never arrives, a typed 1/2 still resolves the prompt.
        text = f"🔐 Approve `{tool}`?\n\nReply **1** to approve or **2** to deny."
        attachments = None
        if self.capabilities.rich_blocks and self._mint_approval_nonce is not None:
            # The nonce is minted by the approval REGISTRY, against the pending
            # entry, so it is retired by the same window that retires the decision
            # and is validated inside ``resolve()`` — before anything is approved.
            # A renderer-owned nonce could outlive the decision it guards, and a
            # renderer-owned CHECK necessarily runs after the dispatcher has
            # already looked the decision up.
            attachments = [
                approval_card(tool, nonce=self._mint_approval_nonce(rid), request_id=rid)
            ]
        await self._client.send_message(
            self._room_id, text, attachments=attachments, parent_id=self._thread_id
        )

    async def on_steer_consumed(self, text: str = "") -> None:
        # Recorded rather than posted: a separate message per fold would bury
        # the answer, and the note costs nothing riding on the final edit.
        self._steered = True

    async def on_compaction(self, context_usage_pct: float) -> None:
        # The dispatcher surfaces threshold notices as separate messages.
        logger.debug("Webex: compaction status %.0f%%", context_usage_pct)

    async def on_done(self, stop_reason: str = "") -> None:
        if self._finalized:
            return
        self._finalized = True
        ok = stop_reason != "error"
        # The BUFFERED default: this renderer sends the answer once, so an
        # unfinished ``[OPTIONS`` tail here is the assistant's own prose and
        # cutting it would be permanent -- a reply ending ``see the [OPTIONS
        # section`` keeps its last four words. The status frame above trades the
        # other way, because a frame is transient.
        body, choices = split_options_trailer("".join(self._buf).strip())
        # Cap the choices for the widget and degrade the remainder to numbered
        # text through the SHARED helper, so the cap is enforced in one place and
        # a choice past it is still visible rather than silently dropped.
        body, kept = apply_options_cap(body, choices, self.capabilities)
        card = self._options_card(kept)
        if card is None:
            # No card will be rendered, so every choice belongs in the text.
            body = self._numbered_text(body, choices)
            kept = []
        # Tables render on the BODY, after the trailer is split off and before
        # anything transforms the text further: the grid is what the reader sees,
        # so it has to exist before the byte split measures it.
        rendered = self.render_tables_for_target(body)
        if rendered != body and len(rendered.encode("utf-8")) > WEBEX_MAX_TEXT:
            # Generated grids must stay in one message; cards and display-safe
            # raw text can use lossless byte chunks. An unrepresentable card run
            # reports its grid so the raw form wins.
            rendered, generated_grid = self.render_tables_for_target_with_metadata(
                body,
                policy=TABLE_POLICY_CARDS,
            )
            if generated_grid and len(rendered.encode("utf-8")) > WEBEX_MAX_TEXT:
                safe_raw = self.safe_raw_table_fallback(body, policy=TABLE_POLICY_CARDS)
                if safe_raw is not None:
                    rendered = safe_raw
        body = rendered
        uploads: list[Any] = []
        if self._uploads_enabled():
            body, uploads = await self._extract_uploads(body)
        content = webex_display_safe(body) or ("…" if ok else _ERROR_TEXT)
        if self._steered:
            content = f"{content}\n\n{_STEER_NOTE}"
        # Byte-aware and LOSSLESS: Webex caps messages in UTF-8 BYTES, so the
        # neutral character-based splitter could hand the client an oversized
        # chunk that gets tail-truncated (silent data loss). Losslessness is the
        # property the table path needs — an oversized safe-raw grid is chunked
        # here and must reassemble exactly, which a line-oriented splitter cannot
        # promise because it consumes the boundary whitespace. Pinned by
        # test_channel_table_rendering.py::TestDeliveryFraming.
        #
        # The SHARED primitive, not a local copy: it carries two termination
        # guards (a non-positive budget, and a single code point wider than the
        # budget) that a hand-rolled copy of this loop spins forever on. And
        # deliberately the FENCE-BLIND one, not ``split_markdown_bytes``: the
        # answer path above re-seals its own fences.
        chunks = chunk_utf8_bytes(content, WEBEX_MAX_TEXT) or ["…"]
        first, rest = chunks[0], chunks[1:]
        delivered = False
        if self._placeholder_id is not None:
            delivered = await self._client.edit_message(self._placeholder_id, self._room_id, first)
        if not delivered:
            sent = await self._client.send_message(self._room_id, first, parent_id=self._thread_id)
            delivered = sent is not None
            # Don't leave a stale "🔧 Running…" placeholder above the answer.
            if delivered and self._placeholder_id is not None:
                await self._client.delete_message(self._placeholder_id)
        if not delivered:
            # Both the edit and the fallback send failed (transient Webex
            # outage). Posting the follow-up chunks anyway would deliver a
            # response that starts mid-answer — abort instead so the failure
            # is all-or-prefix, never a headless tail.
            logger.warning(
                "Webex: final answer delivery failed (edit + send); "
                "suppressing %d follow-up chunk(s)",
                len(rest),
            )
            return
        # The ANSWER completes before anything else goes out. An upload or a card
        # posted between two chunks of one answer reads as an interruption: the
        # user sees part 1, then images, then "pick one of the options above", then
        # part 2 resuming mid-sentence — a card asking for a choice before the
        # question has finished arriving.
        for chunk in rest:
            sent = await self._client.send_message(self._room_id, chunk, parent_id=self._thread_id)
            if sent is None:
                # Stop at the first failed follow-up: the delivered prefix is
                # coherent, and skipping ahead would splice a gap into the middle
                # of the answer. BREAK rather than return, because the uploads and
                # the card below belong to the prefix that DID land — and the card
                # holds the only copy of the kept choices, since the answer text
                # had them stripped precisely so widget and text are not one
                # duplicated list. Returning here would leave the user reading a
                # question with no visible answers anywhere.
                #
                # The truncation is ANNOUNCED for the same reason it is not fatal:
                # what makes a trailing card incoherent is not that it arrives, but
                # that it would otherwise attach to an answer the user believes is
                # complete. Best-effort — this path is already a send failure, so a
                # second failure changes nothing.
                logger.warning("Webex: follow-up chunk delivery failed; stopping")
                await self._client.send_message(
                    self._room_id, _TRUNCATED_NOTE, parent_id=self._thread_id
                )
                break
        if uploads:
            await self._report_failed_uploads(await self._send_uploads(uploads))
        if card is not None:
            # Its OWN message: Webex refuses to edit a message once it carries an
            # attachment, so folding the card into the answer would forfeit the
            # answer's final edit AND leave the card unretireable. Threaded with
            # the answer so a choice does not appear detached in the room root.
            # The numbered choices ride the card message's own markdown, which is
            # the FALLBACK Webex requires alongside an attachment — and cards.py's
            # stated invariant is that a card is an addition, never the only
            # affordance. Without them a client that cannot render Adaptive Cards
            # receives "pick one of the options above" with no options anywhere:
            # the answer text had them stripped precisely because the card was
            # carrying them. A card-capable client sees the buttons and the same
            # list numbered beneath, which is coherent because the numbers ARE the
            # button order.
            sent = await self._client.send_message(
                self._room_id,
                self._numbered_text(_CHOICE_PROMPT, kept),
                attachments=[card],
                parent_id=self._thread_id,
            )
            if sent is None:
                # The card carried the ONLY copy of the kept choices — the answer
                # text has them stripped precisely so widget and text do not
                # duplicate one list. A failed card would therefore leave the user
                # reading a question with no visible answers, so the numbered form
                # is posted as the fallback it always was. Sent only on failure:
                # posting both unconditionally is the duplication the shared
                # apply_options_cap contract exists to prevent.
                logger.warning("Webex: options card send failed; posting the choices as text")
                await self._client.send_message(
                    self._room_id,
                    self._numbered_text("", kept).lstrip("\n"),
                    parent_id=self._thread_id,
                )

    async def close(self) -> None:
        """Idempotent teardown: finalize the turn if it never reached on_done."""
        if not self._finalized:
            await self.on_done(stop_reason="error")

    # -- helpers ------------------------------------------------------------
    async def _status_frame(self) -> None:
        """Edit the placeholder to the current tool plus the forming answer.

        Gated on three things, in this order: a live placeholder, the status
        budget, and the throttle. Then on monotonic progress — an identical
        frame would spend an edit to change nothing, and edits are the scarcest
        resource on this channel.

        The throttle window closes as soon as it OPENS, before the frame is even
        built, and that ordering is load-bearing. Closing it only on a successful
        edit leaves the window open for every later chunk whenever the frame stops
        changing — and the frame stops changing often, because it shows only the
        last ``_STATUS_TAIL_CHARS`` of the answer. Each of those chunks would then
        rebuild the whole buffer and re-run the trailer regex, so a long stream of
        repetitive output (a wide markdown table, a run of blank lines, an
        unterminated ``[OPTIONS`` trailer) turns a per-turn cost into a
        per-chunk one. Deferring an unchanged frame to the next window loses
        nothing: by definition there was nothing new to show.
        """
        if self._placeholder_id is None or self._edits_used >= _STATUS_EDIT_BUDGET:
            return
        now = time.monotonic()
        if now - self._last_status < self._throttle:
            return
        self._last_status = now
        frame = self._frame_text()
        if not frame or frame == self._last_frame:
            return
        # Scanned like the final answer: the frame carries the forming answer's
        # tail, so the same delimiter-split credential the byte scan cannot see
        # reaches the room here FIRST — minutes before on_done would have caught it.
        frame = webex_display_safe(frame)
        edited = await self._client.edit_message(self._placeholder_id, self._room_id, frame)
        if edited:
            self._edits_used += 1
            self._last_frame = frame
            # The budget bounds this: doubling from _STATUS_THROTTLE_S over
            # _STATUS_EDIT_BUDGET edits spreads the frames across ~8 minutes.
            self._throttle *= 2
        else:
            # An edit failure (rate limit / edit cap) burns the whole status
            # budget so we never race the final-answer edit against the cap.
            self._edits_used = _STATUS_EDIT_BUDGET

    def _frame_text(self) -> str:
        """The status frame: the running tool, then the answer's tail so far.

        The tail is already credential-redacted — it arrives through the
        driver's ``StreamRedactor`` — and its trailer is stripped so a partial
        ``[OPTIONS…`` never flashes up as raw text.
        """
        parts = []
        if self._tool:
            parts.append(f"🔧 Running: {self._tool}…")
        tail = self.text()
        if tail and self._uploads_enabled():
            # Hide a local path the final answer will turn into an upload, so it
            # never flashes as raw filesystem text in a status frame.
            tail = hide_local_refs(tail)
        if tail:
            if len(tail) > _STATUS_TAIL_CHARS:
                tail = "…" + tail[-_STATUS_TAIL_CHARS:]
            parts.append(tail)
        return "\n\n".join(parts)

    def text(self) -> str:
        """The answer body so far, with any ``[OPTIONS:]`` trailer removed.

        Used for the status frame's tail. The trailer is NOT rendered here:
        ``on_done`` decides between a card and numbered text, and a status frame
        showing choices the user cannot yet act on would be noise.

        ``hide_partial=True``: a frame renders text still arriving, so a partial
        marker really may be a marker mid-flight, and showing reserved protocol as
        raw text -- even for one frame -- is what this hides. Safe here and not on
        the answer path because the next frame re-renders from the same buffer.
        """
        return split_options_trailer("".join(self._buf).strip(), hide_partial=True)[0]

    def authorize_upload_root(self, root: str) -> None:
        """Authorize the provider's resolved cwd as the upload root.

        A non-absolute root disables uploads rather than widening to the whole
        filesystem: extraction confines reads to this directory, so an unusable
        value must fail closed.
        """
        self._upload_root = root if os.path.isabs(root) else ""

    def _uploads_enabled(self) -> bool:
        """Whether local files in the reply may be uploaded.

        Positive membership on all three inputs. The room check is Webex's own
        version of the disclosure rule that keeps this channel DM-first: a file
        posted into a space is readable by every member of it, including people
        the email allow-list excludes, so a group turn keeps printing the markdown
        path (the honest degradation) instead of shipping bytes.
        """
        return (
            bool(self.capabilities.files_outbound)
            and self._uploads_allowed
            and bool(self._upload_root)
        )

    async def _extract_uploads(self, text: str) -> tuple[str, list[Any]]:
        """Pull local image references out of *text* into upload payloads.

        Off-loop and fail-soft: extraction stats and opens files, and a failure
        must cost the pictures, never the answer.
        """
        try:
            result = await extract_local_refs_off_loop(
                text, within_root=self._upload_root, limits=_UPLOAD_LIMITS
            )
        except Exception:
            logger.warning("Webex: outbound file extraction failed", exc_info=True)
            return text, []
        if result.rejections:
            logger.info(
                "Webex: %d outbound file reference(s) not uploaded: %s",
                len(result.rejections),
                ", ".join(sorted({r.reason for r in result.rejections})),
            )
        return result.rewritten_text, list(result.files)

    async def _send_uploads(self, files: list[Any]) -> list[Any]:
        """Upload each extracted file as its own message; return the failures.

        Webex takes one file per message, so a reply with three images becomes
        three follow-up messages. The bytes come from the extractor; the path
        contributes nothing but a name, and that name is DERIVED rather than
        trusted.

        The failures are RETURNED rather than only logged because extraction
        already removed each reference from the answer text — so a silent failure
        leaves the user with neither the image nor any hint that one was meant to
        be there. The caller restores the references, which is the same recovery
        Discord performs by re-posting its segment's markup.
        """
        failed: list[Any] = []
        for index, item in enumerate(files):
            # Both strings leaving here originate in model-authored reply text, so
            # neither is passed through as written.
            #
            # The NAME enters the multipart ``Content-Disposition`` and Webex
            # echoes it back to the whole room as the attachment's label, so it
            # goes through the shared ``upload_filename`` that every other
            # uploading channel uses (Discord, Slack, Telegram) instead of the raw
            # basename: the path is LLM-authored, so "it is only a basename" is
            # not by itself a reason to trust it. Two things that buys, in the
            # order they are actually reachable:
            #
            # * The extension follows the SNIFFED mime, never the written path's
            #   suffix. Extraction admits a file on its byte signature alone and
            #   never reconciles the two, so a raster whose path says ``.html``
            #   would ship as ``report.html`` on a part typed ``image/gif`` — the
            #   sent name disagreeing with the bytes every gate actually checked.
            # * Only a header-safe basename survives and the RESULT is re-scanned,
            #   so a name still shaped like a credential or a beacon URL is
            #   replaced outright rather than echoed to the room.
            #
            # The CAPTION becomes the upload message's own text, and ``item.alt``
            # is the model-authored alt text of the markdown image reference. It
            # bypasses the answer body's redaction (that ran on the text these
            # files were extracted OUT of), so a delimiter-split credential in the
            # alt (`AKIA**…**`, whole once Webex renders the markup away) would
            # otherwise ship; it gets the same display-form floor every other
            # ``_reply``/frame passes through.
            name = upload_filename(item, index)
            caption = webex_display_safe(item.alt or name)
            sent = await self._client.send_file(
                self._room_id,
                caption,
                data=item.data,
                filename=name,
                mimetype=item.mime or "application/octet-stream",
                parent_id=self._thread_id,
            )
            if sent is None:
                logger.warning("Webex: upload failed for %s", name)
                failed.append(item)
        return failed

    async def _report_failed_uploads(self, failed: list[Any]) -> None:
        """Put back the references whose upload did not land.

        The path is what the answer would have shown if uploads were off at all —
        which is exactly what a group turn does — so restoring it discloses
        nothing new, and it is the difference between "here is where the chart is"
        and a reply that silently mentions no chart.
        """
        if not failed:
            return
        lines = [_UPLOAD_FAILED_HEADER]
        lines += [f"- `{item.path}`" for item in failed]
        await self._client.send_message(
            self._room_id, webex_display_safe("\n".join(lines)), parent_id=self._thread_id
        )

    def _numbered_text(self, body: str, choices: list[str]) -> str:
        """*choices* as a numbered list on *body*, through the SHARED sink.

        Reaches ``apply_options_cap`` with the widget disabled, which is that
        helper's own framing of a button-less surface: zero slots makes every
        choice overflow, and overflow is the numbered-list sink that does the
        display-form credential redaction and the mention defanging. So the
        sanitising is not re-derived here and cannot drift from the widget path's.

        Used only where no card will carry the choices -- a card this client
        cannot render, or a card whose send failed.
        """
        return apply_options_cap(body, choices, replace(self.capabilities, max_buttons=0))[0]

    def _options_card(self, choices: list[str]) -> dict[str, Any] | None:
        """An Adaptive Card for *choices*, or ``None`` to keep the text form.

        Gated on POSITIVE capability membership (``rich_blocks``) rather than a
        channel check, so a transport that declares no rich blocks cannot be
        handed a card by a code path that forgot to ask, and on a publisher: a
        card whose press cannot be resolved would be a row of inert buttons, which
        is strictly worse than the numbered text it replaced.

        What the card offered is published to a store the DISPATCHER owns, because
        this card is the last thing a turn sends and the press arrives after this
        renderer is gone. Both sides read the button order from
        ``cards.usable_choices`` so an index can never drift.
        """
        if not choices or not self.capabilities.rich_blocks or self._publish_choices is None:
            return None
        usable = usable_choices(choices)
        if not usable:
            return None
        # The SHARED minter, for the reason stated at ``new_approval_nonce``: an
        # options press and an approval press are the same hazard (a control left
        # in a chat from an earlier turn naming indexes that are live again), so a
        # second generator with its own alphabet is what eventually diverges. The
        # approval card on this same renderer already reaches it through
        # ``PendingApprovals.reserve``.
        nonce = new_approval_nonce()
        self._publish_choices(nonce, usable)
        return options_card(usable, nonce=nonce)
