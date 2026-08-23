"""Layer 2b -- iMessage ``Renderer``.

Maps the channel-neutral ``OutputEvent`` stream (routed by the base
:class:`Renderer`'s ``dispatch``) onto bridge sends.

The shape is dictated by one hard constraint: **a sent iMessage cannot be
edited**. Every other channel opens with a placeholder it later rewrites into
the answer; doing that here would leave a permanent "Thinking..." message
stranded above every reply. So the turn's only progress signal is the typing
indicator, refreshed while tools run, and the answer is delivered as one
message (plus continuations when it is long).

Intermediate reasoning and tool activity stay in the gateway: a phone is a
poor place to read a tool log, and each line would be its own undeletable
message.

Dependency direction is ``imessage -> messaging`` (allowed).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from kiro_crew.constants import OPTIONS_RE_TRAILER
from kiro_crew.imessage.client import redact_handle
from kiro_crew.imessage.plaintext import chunk_plaintext, to_plaintext
from kiro_crew.imessage.rpc import RpcError, RpcTransportError
from kiro_crew.messaging.display_safety import redact_for_display
from kiro_crew.messaging.renderer import Renderer
from kiro_crew.messaging.transport import TransportCapabilities
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:
    from kiro_crew.imessage.client import IMessageClient

logger = logging.getLogger(__name__)

#: Min seconds between typing-indicator refreshes. The indicator expires on its
#: own after a few seconds, so a long tool run needs re-poking -- but one call
#: per tool event would be a burst on the bridge's single mutation worker.
_TYPING_THROTTLE_S = 4.0

_ERROR_TEXT = "⚠️ Something went wrong — please try again."

#: Trailing "[OPTIONS: a | b | c]" chip trailer (a dashboard convention with no
#: iMessage equivalent -- there are no tappable choices). Matched only at the
#: very END of the message, so use the DOTALL/trailer canonical parser defined
#: once in constants.py; see OPTIONS_RE_TRAILER for the ReDoS-hardening
#: rationale.
_OPTIONS_RE = OPTIONS_RE_TRAILER


def _default_redactor(text: str) -> str:
    """The same pair ``TurnDriver`` streams provider text through.

    Spelled out here rather than imported from ``messaging.renderer``, whose
    equivalent is private to that module. ``security`` is a pure-regex module
    with no vendor dependencies, so this adds no import-time cost and nothing
    that could touch an event loop.
    """
    out, _ = redact_exfiltration_urls(text or "")
    out, _ = redact_credentials(out)
    return out


def _strip_options(text: str) -> str:
    """Remove a trailing ``[OPTIONS: a | b | c]`` chip trailer.

    iMessage has no tappable chips, so the trailer is dropped entirely -- the
    user just replies naturally. Also hides a partial ``[OPTIONS...`` fragment
    (no closing ``]``) so it never lands as raw text.
    """
    m = _OPTIONS_RE.search(text)
    if m:
        return text[: m.start()].rstrip()
    idx = text.rfind("[OPTIONS")
    if idx != -1 and "]" not in text[idx:]:
        return text[:idx].rstrip()
    return text


class IMessageRenderer(Renderer):
    """Renders a turn to one iMessage chat: typing indicator, then the answer."""

    channel_type = "imessage"

    def __init__(
        self,
        client: "IMessageClient",
        handle: str,
        capabilities: TransportCapabilities,
        *,
        chat_selector: dict[str, Any] | None = None,
        session_key: str = "",
    ) -> None:
        super().__init__(capabilities)
        self._client = client
        self._handle = handle
        self._selector = dict(chat_selector or {})
        self._session_key = session_key
        self._buf: list[str] = []
        self._last_typing = 0.0
        self._started = False
        self._finalized = False

    # -- lifecycle ----------------------------------------------------------
    async def on_turn_start(self) -> None:
        if self._started:  # idempotent (dispatch + driver both call it)
            return
        self._started = True
        # Acknowledge receipt before the work starts. Both degrade to no-ops
        # when the bridge does not offer them.
        await self._client.mark_read(self._selector)
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
        message, and a message here cannot be taken back.
        """
        await self._poke_typing()

    async def on_prompt_choice(
        self,
        options: list[dict[str, Any]],
        request_id: str | int,
        tool_title: str = "",
        tool_purpose: str = "",
    ) -> None:
        # The driver only dispatches prompt_choice for INTERACTIVE + a decider,
        # and iMessage runs decider-less (deny-by-default), so this is never
        # reached -- kept as a safe no-op per the Renderer contract.
        logger.debug("imessage: prompt_choice ignored (no interactive buttons)")

    async def on_compaction(self, context_usage_pct: float) -> None:
        # The dispatcher surfaces threshold notices as separate messages.
        logger.debug("imessage: compaction status %.0f%%", context_usage_pct)

    async def on_done(self, stop_reason: str = "") -> None:
        if self._finalized:
            return
        self._finalized = True
        ok = stop_reason != "error"
        content = self.delivery_text() or ("…" if ok else _ERROR_TEXT)
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
                # Remaining chunks are abandoned deliberately: the bridge is down
                # or rejecting, so they would fail too.
                logger.error(
                    "imessage: delivery to %s failed on chunk %d of %d: %s",
                    redact_handle(self._handle),
                    index + 1,
                    len(chunks),
                    exc,
                )
                raise

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
                    "imessage: teardown could not deliver the error notice to %s: %s",
                    redact_handle(self._handle),
                    exc,
                )

    # -- helpers ------------------------------------------------------------
    def text(self) -> str:
        """The turn's visible answer so far, as markdown with OPTIONS stripped."""
        return _strip_options("".join(self._buf).strip())

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
        return redact_for_display(to_plaintext(self.text()), _default_redactor)[0]

    async def _poke_typing(self, *, force: bool = False) -> None:
        if not self._selector:
            return
        now = time.monotonic()
        if not force and now - self._last_typing < _TYPING_THROTTLE_S:
            return
        self._last_typing = now
        await self._client.send_typing(self._selector)
