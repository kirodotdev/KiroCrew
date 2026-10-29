"""Layer 2b -- Signal ``Renderer``.

Maps the channel-neutral ``OutputEvent`` stream onto ``signal-cli`` sends. The
shape is the iMessage renderer's, for the same reason: this channel does not
edit what it sent, so a "Thinking..." placeholder would be stranded above every
reply. The progress signal is Signal's typing indicator (refreshed while tools
run; it lapses on its own after 15 seconds), the sender gets a read receipt,
and the answer goes out once at the end, flattened to plain text and split
into Signal-sized messages.

Intermediate reasoning and tool activity stay in the gateway. The one
exception is a tool-approval prompt: with no buttons it is sent as its own
numbered plain-text message the operator answers by typing ``1``/``2``/``3``
(the shared ``messaging.approval`` ladder, WhatsApp's shape).

Two more things ride on the turn, both mirroring WhatsApp:

* **Status reactions** on the person's own message: an hourglass while the
  turn runs, replaced (Signal keeps one reaction per author) by a check mark
  when the answer was delivered or a warning sign when the turn failed. A
  muted conversation never reaches this renderer (``drive_turn`` swaps in a
  silent one), so a mute stays silent.
* **Outbound files**: picture references in the answer
  (``![chart](/abs/path.png)``) are planned through the shared extractor
  BEFORE the text is flattened and split (:mod:`kiro_crew.signal_cli.files`),
  sent as attachments after the text, and every refusal is named.

Every text this channel puts on the wire passes :func:`display_safe_text` (or
:meth:`SignalRenderer._flatten`, which is that screen after flattening): the
answer, the approval prompt, a picture's caption, the refusal note and the
``file_send`` caption alike. A refusal note quotes the model's own image
destination and a reason, so a screen on the answer alone would leave the note
as the way round it.

Dependency direction is ``signal_cli -> messaging`` (allowed).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Callable

from kiro_crew.messaging.approval import (
    TIMEOUT_NOTICE,
    abandon_approval,
    build_approval_prompt,
    open_approval,
)
from kiro_crew.messaging.display_safety import redact_for_display
from kiro_crew.messaging.outbound_files import Rejection, upload_filename
from kiro_crew.messaging.renderer import (
    Renderer,
    count_redaction_tags,
    redaction_notice,
    render_options_as_text,
)
from kiro_crew.messaging.transport import TransportCapabilities
from kiro_crew.security import (
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.signal_cli.client import redact_identifier
from kiro_crew.signal_cli.files import (
    REASON_UPLOAD_FAILED,
    UploadPlan,
    data_uri,
    plan_uploads,
    rejection_note,
)
from kiro_crew.signal_cli.plaintext import chunk_plaintext, to_plaintext
from kiro_crew.signal_cli.rpc import RpcError, RpcTransportError

if TYPE_CHECKING:
    from kiro_crew.signal_cli.client import SignalClient

logger = logging.getLogger(__name__)

#: Min seconds between typing-indicator refreshes (Signal's lapses after 15s).
_TYPING_THROTTLE_S = 10.0

_ERROR_TEXT = "⚠️ Something went wrong — please try again."

#: Phase reactions on the person's own message -- WhatsApp's set, so the two
#: phone channels read the same. Chat content, not dashboard UI.
REACTION_WORKING = "\N{HOURGLASS WITH FLOWING SAND}"
REACTION_DONE = "\N{WHITE HEAVY CHECK MARK}"
REACTION_FAILED = "\N{WARNING SIGN}"


def _default_redactor(text: str) -> str:
    """The same pair ``TurnDriver`` streams provider text through."""
    out, _ = redact_exfiltration_urls(text or "")
    out, _ = redact_credentials(out)
    return out


def display_safe_text(text: str) -> str:
    """*text* screened for what the reader is shown, without flattening it.

    For a sink whose text is already plain: the file-refusal note and the
    ``file_send`` caption. Re-flattening those would rewrite characters inside
    a path (an ``_`` or ``*`` in a filename), but they still need the display
    screen, because both quote agent-authored text -- an image destination, a
    refusal reason, a description -- and the screen is what finds a credential
    split by ``**``, ``_`` or a backtick. WhatsApp's ``display_safe_text`` is
    the same rule.
    """
    return redact_for_display(text or "", _default_redactor)[0]


class SignalRenderer(Renderer):
    """Renders a turn to one Signal conversation: typing indicator, then the answer."""

    channel_type = "signal"

    def __init__(
        self,
        client: "SignalClient",
        recipient: str,
        capabilities: TransportCapabilities,
        *,
        reply_timestamp: int = 0,
        session_key: str = "",
        approval_session_key: str | None = None,
        react_author: str = "",
        react_timestamp: int = 0,
        upload_root: "Callable[[], str] | None" = None,
    ) -> None:
        super().__init__(capabilities)
        self._client = client
        self._handle = recipient
        self._reply_timestamp = reply_timestamp
        self._session_key = session_key
        #: Set by the dispatcher only when it installed a typed-reply decider,
        #: i.e. for a sender allowed to approve. ``None`` keeps
        #: ``on_prompt_choice`` silent rather than posting a prompt whose answer
        #: nothing would read.
        self._approval_session_key = approval_session_key
        #: Fallback naming for a prompt whose event carries no title of its own.
        self._last_tool = ""
        self._last_purpose = ""
        #: The person's message the status reactions go on: its author (as
        #: signal-cli reported it) and its timestamp. Unset means no reactions.
        self._react_author = react_author
        self._react_timestamp = react_timestamp
        #: Resolves the only tree an outbound file reference may name (the
        #: provider's cwd), at delivery time: the provider is acquired inside
        #: the turn. ``None`` disables uploads.
        self._upload_root = upload_root
        self._buf: list[str] = []
        self._last_typing = 0.0
        self._started = False
        self._finalized = False

    # -- lifecycle ----------------------------------------------------------
    async def on_turn_start(self) -> None:
        if self._started:  # idempotent (dispatch + driver both call it)
            return
        self._started = True
        # Acknowledge receipt before the work starts. Both are no-ops for Note
        # to Self and degrade silently when signal-cli rejects them.
        await self._client.mark_read(self._handle, self._reply_timestamp)
        await self._react(REACTION_WORKING)
        await self._poke_typing(force=True)

    async def on_text_chunk(self, text: str) -> None:
        # Buffered only: with no edit and no streaming, the answer goes out
        # once, at the end.
        self._buf.append(text)

    async def on_thinking(self, text: str) -> None:
        # Reasoning is not surfaced inline (parity with Webex/WeCom).
        return None

    async def on_tool_call(
        self, tool_call_id: str, title: str, tool_kind: str = "", tool_purpose: str = ""
    ) -> None:
        """Keep the typing indicator alive while tools run.

        The tool's identity is deliberately not sent: naming it would need a
        message, and a message here cannot be taken back. It is remembered,
        only as the fallback name for an approval prompt whose own event
        carries no title.
        """
        self._last_tool = title or self._last_tool
        self._last_purpose = tool_purpose or ""
        await self._poke_typing()

    async def on_prompt_choice(
        self,
        options: list[dict[str, Any]],
        request_id: str | int,
        tool_title: str = "",
        tool_purpose: str = "",
        tool_input: str = "",
    ) -> None:
        """Ask for approval as a numbered message the operator answers by typing.

        Only reached when the dispatcher supplied a decider (the driver renders
        no prompt otherwise). The entry is opened HERE, before the send, because
        the driver dispatches this event before it awaits the decider and the
        decider reads "no entry" as "no prompt reached anybody" (deny now).

        The prompt goes through ``client.send`` like every other message, so in
        Note to Self its echo lands in the client's own-send ledger and is
        dropped by the transport instead of arriving as a new turn. It is never a
        verdict either way: :func:`parse_approval_reply` accepts only a message
        that is ENTIRELY an answer.
        """
        if self._approval_session_key is None:
            logger.debug("signal: prompt_choice with no approval route; ignoring")
            return
        title = tool_title or self._last_tool
        purpose = tool_purpose if tool_title else self._last_purpose
        open_approval(
            self._approval_session_key,
            request_id,
            tool_title=title,
            conversation_id=self._handle,
            on_timeout=self._announce_approval_timeout,
        )
        try:
            # ``build_approval_prompt`` screens the model-authored title and
            # purpose; the display re-scan matches what ``delivery_text`` does
            # to the answer, since this is another message reaching the phone.
            prompt = display_safe_text(build_approval_prompt(title, purpose))
            await self._client.send(self._handle, prompt)
        except BaseException:
            # Opened before the send because the send is what can fail. Left
            # behind, the NEXT "1" the operator types would answer a prompt
            # they never saw, for a turn that already unwound.
            abandon_approval(self._approval_session_key, request_id)
            raise

    async def _announce_approval_timeout(self) -> None:
        """Say the window closed. Best-effort: the tool is already denied.

        Signal messages are not edited here, so the notice is a new message;
        without it the stale prompt still looks answerable, and the operator
        would believe a late "1" approved a tool that was already refused.
        """
        try:
            await self._client.send(self._handle, TIMEOUT_NOTICE)
        except (RpcError, RpcTransportError) as exc:
            logger.warning(
                "signal: could not deliver the approval timeout to %s: %s",
                redact_identifier(self._handle),
                exc,
            )

    async def on_compaction(self, context_usage_pct: float) -> None:
        # The dispatcher surfaces threshold notices as separate messages.
        logger.debug("signal: compaction status %.0f%%", context_usage_pct)

    async def on_done(self, stop_reason: str = "") -> None:
        if self._finalized:
            return
        self._finalized = True
        ok = stop_reason != "error"
        try:
            await self._deliver(ok)
        except Exception:
            # Undelivered: the warning sign, then the driver records the failure.
            await self._react(REACTION_FAILED)
            raise
        await self._react(REACTION_DONE if ok else REACTION_FAILED)

    async def _deliver(self, ok: bool) -> None:
        """Send the answer, then its files, then any redaction notice."""
        # Planned BEFORE flattening and splitting: a reference cut by the
        # splitter is half a link in each chunk, which no later pass recognises.
        plan = await self._plan_uploads() if ok else UploadPlan(text=self.text())
        content = self._flatten(plan.text) or ("…" if ok else _ERROR_TEXT)
        chunks = chunk_plaintext(content, self.capabilities.max_message_chars) or ["…"]
        for index, chunk in enumerate(chunks):
            try:
                await self._client.send(self._handle, chunk)
            except (RpcError, RpcTransportError) as exc:
                # Log which chunk died -- the driver's own error will not know --
                # then RE-RAISE. Swallowing here is what made an undelivered
                # answer look like a completed turn: `on_done` is the terminal
                # hook, so a clean return tells `drive_turn` the reply was
                # delivered and the turn is persisted as successful. Catching in
                # `client.send` and then catching again here would have moved the
                # defect up a layer rather than fixing it.
                #
                # Remaining chunks are abandoned deliberately: signal-cli is down
                # or rejecting, so they would fail too.
                logger.error(
                    "signal: delivery to %s failed on chunk %d of %d: %s",
                    redact_identifier(self._handle),
                    index + 1,
                    len(chunks),
                    exc,
                )
                raise

        note = await self._send_files(plan)

        # The answer shipped. If either redactor rewrote it -- a credential, or a
        # suspicious URL `_default_redactor` also strips -- the reader is holding
        # a command that will not run when pasted. This channel does not
        # edit what it sent and the transport carries no annotation channel, so -- unlike
        # the dashboard, which appends a notice row to the same segment -- the only
        # way to say so is an ADDITIONAL follow-up message.
        #
        # Count from the TAG in the delivered text rather than from the redactor's
        # warnings list, which `_default_redactor` drops: each chunk is redacted on
        # the way out, so re-redacting the assembled answer reports nothing while
        # the placeholders are plainly visible. Sum every tag the redactor can emit
        # (`CREDENTIAL_REDACTION_TAGS`) so an encoded-credential-only answer is not
        # missed, and count the URL tag by `EXFILTRATION_REDACTION_TAG_PREFIX`
        # prefix (it interpolates the domain, so it has no constant form). The two
        # stay separate because the notice is worded by kind: the remedies differ.
        #
        # Best-effort AFTER the answer succeeded: a failure to deliver the notice
        # must NOT re-raise and convert an already-delivered answer into a failed
        # turn. That trade is deliberate -- the answer is out; losing the notice
        # is a degraded warning, losing the turn would discard a delivered reply.
        _cred_redactions, _url_redactions = count_redaction_tags(f"{content}\n{note}")
        if _cred_redactions > 0 or _url_redactions > 0:
            try:
                await self._client.send(
                    self._handle, redaction_notice(_cred_redactions, _url_redactions)
                )
            except (RpcError, RpcTransportError) as exc:
                logger.warning(
                    "signal: could not deliver the redaction notice to %s "
                    "(answer already sent): %s",
                    redact_identifier(self._handle),
                    exc,
                )

    async def close(self) -> None:
        """Idempotent teardown: finalize the turn if it never reached on_done.

        This is the ONE place a delivery failure is contained, because it is
        already the failure path: `close` runs during teardown, often after the
        turn has errored, and letting a send failure escape here would replace
        the original error with a delivery error and skip the rest of teardown.
        The turn's own outcome is already recorded by the time we get here.
        """
        if not self._finalized:
            try:
                await self.on_done(stop_reason="error")
            except (RpcError, RpcTransportError) as exc:
                logger.warning(
                    "signal: teardown could not deliver the error notice to %s: %s",
                    redact_identifier(self._handle),
                    exc,
                )

    # -- files and reactions ------------------------------------------------
    async def _plan_uploads(self) -> UploadPlan:
        """The answer's file plan; uploads nothing when files are off or no root."""
        text = self.text()
        if not self.capabilities.files_outbound or self._upload_root is None:
            return UploadPlan(text=text)
        try:
            root = self._upload_root()
        except Exception:  # noqa: BLE001: no root means no uploads, never a failure
            logger.debug("signal: could not resolve the upload root", exc_info=True)
            return UploadPlan(text=text)
        return await plan_uploads(text, within_root=root, session_key=self._session_key)

    async def _send_files(self, plan: UploadPlan) -> str:
        """Send each planned picture as an attachment, then name any refusal.

        After the text, so the picture lands under the words that introduced
        it. Best-effort AFTER the answer: a failed upload becomes a rejection
        the note names, never a failed turn -- the answer is already out.
        Returns the note as delivered (``""`` when none went out), so the
        redaction notice can count a placeholder the screen wrote into it.
        """
        rejections = list(plan.rejections)
        for index, outbound in enumerate(plan.files):
            try:
                await self._client.send(
                    self._handle,
                    # The caption is model-authored alt text: screened like a body.
                    self._flatten(outbound.alt or ""),
                    attachments=[
                        data_uri(outbound.data, outbound.mime, upload_filename(outbound, index))
                    ],
                )
            except (RpcError, RpcTransportError):
                logger.warning("signal: file upload failed", exc_info=True)
                rejections.append(Rejection(outbound.path, REASON_UPLOAD_FAILED, ""))
        # Screened like the answer: the note quotes the model's image
        # destination and the refusal reason, either of which can carry a
        # credential the reader would see reassembled.
        note = display_safe_text(rejection_note(rejections))
        if not note:
            return ""
        try:
            await self._client.send(self._handle, note)
        except (RpcError, RpcTransportError) as exc:
            logger.warning("signal: could not deliver the file note (answer already sent): %s", exc)
            return ""
        return note

    async def _react(self, emoji: str) -> None:
        """Set the status reaction on the person's message. Best-effort."""
        if not self.capabilities.reactions or not self._react_author:
            return
        try:
            await self._client.send_reaction(
                self._handle, emoji, self._react_author, self._react_timestamp
            )
        except Exception:  # noqa: BLE001: decoration must never fail the turn
            logger.debug("signal: status reaction failed", exc_info=True)

    # -- helpers ------------------------------------------------------------
    def text(self) -> str:
        """The turn's answer as markdown, with ``[OPTIONS:]`` as numbered text."""
        return render_options_as_text("".join(self._buf).strip(), self.capabilities)

    def delivery_text(self) -> str:
        """The answer flattened for a surface that renders no markup.

        The flattening is itself the hazard ``display_safety`` exists to close.
        ``TurnDriver`` scans the provider stream as literal bytes, so a
        credential split by markup -- ``**AKIA**IOSFODNN7EXAMPLE``, a link whose
        target is broken by emphasis -- matches no pattern as written and
        survives that pass. :func:`to_plaintext` then strips the delimiters and
        hands the reader an intact secret: the transformation happens AFTER the
        scan, which is why the scan has to be repeated on the form that actually
        ships.

        Unlike every other channel this one collapses the markup in code rather
        than leaving it to a platform renderer, so the re-scan cannot be skipped
        on the grounds that the driver already redacted the stream.
        """
        return self._flatten(self.text())

    @staticmethod
    def _flatten(text: str) -> str:
        return redact_for_display(to_plaintext(text), _default_redactor)[0]

    async def _poke_typing(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_typing < _TYPING_THROTTLE_S:
            return
        self._last_typing = now
        await self._client.send_typing(self._handle)
