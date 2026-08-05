"""Layer 2b -- Microsoft Teams ``Renderer``.

Maps the channel-neutral ``OutputEvent`` stream (routed by the base
:class:`Renderer`'s ``dispatch``) onto Bot Framework REST calls:

* ``on_turn_start`` -- posts a ``typing`` activity for immediate feedback
  (Teams has no in-place edit stream here, so no placeholder message).
* ``on_tool_call`` -- re-posts ``typing`` (throttled) so the indicator does
  not lapse during a long turn.
* ``on_text_chunk`` -- buffered only (no typewriter streaming), with any
  trailing ``[OPTIONS:]`` markup stripped (Teams MVP has no tappable chips).
* ``on_prompt_choice`` -- no-op: the driver only dispatches this for
  INTERACTIVE + a decider; Teams runs decider-less (deny-by-default).
* ``on_compaction`` -- logged only; the dispatcher surfaces threshold notices
  as separate messages post-turn.
* ``on_done`` -- sends the final answer as one message (chunked to the Teams
  size cap if needed).

Dependency direction is ``teams -> messaging`` (allowed).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from kiro_crew.constants import OPTIONS_RE_TRAILER
from kiro_crew.messaging.renderer import Renderer, chunk_text
from kiro_crew.messaging.transport import TransportCapabilities

if TYPE_CHECKING:
    from kiro_crew.teams.client import TeamsClient

logger = logging.getLogger(__name__)

# Min seconds between typing-indicator refreshes during a long turn.
_TYPING_THROTTLE_S = 3.0

_ERROR_TEXT = "⚠️ Something went wrong — please try again."

# Trailing "[OPTIONS: a | b | c]" chip trailer (a dashboard convention Teams
# can't render as tappable chips in the MVP). Defined once in constants.py
# (shared across surfaces) so the ReDoS-hardened grammar can't drift.
_OPTIONS_RE = OPTIONS_RE_TRAILER


def _strip_options(text: str) -> str:
    """Remove a trailing ``[OPTIONS: a | b | c]`` chip trailer.

    Teams (MVP) has no tappable chips, so we drop the trailer entirely -- the
    user just replies naturally. Also hides a partial ``[OPTIONS…`` fragment
    (no closing ``]``) so it never lands as raw text.
    """
    m = _OPTIONS_RE.search(text)
    if m:
        return text[: m.start()].rstrip()
    idx = text.rfind("[OPTIONS")
    if idx != -1 and "]" not in text[idx:]:
        return text[:idx].rstrip()
    return text


class TeamsRenderer(Renderer):
    """Renders a turn to a Teams conversation: typing -> single final answer."""

    channel_type = "teams"

    def __init__(
        self,
        client: "TeamsClient",
        conversation_id: str,
        service_url: str,
        capabilities: TransportCapabilities,
        *,
        session_key: str = "",
    ) -> None:
        super().__init__(capabilities)
        self._client = client
        self._conversation_id = conversation_id
        self._service_url = service_url
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
        await self._client.send_typing(self._conversation_id, self._service_url)
        self._last_typing = time.monotonic()

    async def on_text_chunk(self, text: str) -> None:
        # Buffered only: Bot Framework has no typewriter edit stream here.
        self._buf.append(text)

    async def on_thinking(self, text: str) -> None:
        # Teams does not surface reasoning inline (parity with WeCom/Webex).
        return None

    async def on_tool_call(
        self, tool_call_id: str, title: str, tool_kind: str = "", tool_purpose: str = ""
    ) -> None:
        """Refresh the typing indicator during a long turn (throttled)."""
        now = time.monotonic()
        if now - self._last_typing < _TYPING_THROTTLE_S:
            return
        await self._client.send_typing(self._conversation_id, self._service_url)
        self._last_typing = now

    async def on_prompt_choice(self, options: list[dict[str, Any]], request_id: str | int) -> None:
        # Driver only dispatches prompt_choice for INTERACTIVE + a decider, and
        # Teams runs decider-less (deny-by-default) -- safe no-op.
        logger.debug("Teams: prompt_choice ignored (no interactive buttons)")

    async def on_compaction(self, context_usage_pct: float) -> None:
        logger.debug("Teams: compaction status %.0f%%", context_usage_pct)

    async def on_done(self, stop_reason: str = "") -> None:
        if self._finalized:
            return
        self._finalized = True
        ok = stop_reason != "error"
        content = self.text() or ("…" if ok else _ERROR_TEXT)
        chunks = chunk_text(content, self.capabilities.max_message_chars) or ["…"]
        for chunk in chunks:
            sent = await self._client.send_message(
                self._conversation_id, chunk, self._service_url
            )
            if sent is None:
                # Stop at the first failed chunk: the delivered prefix is
                # coherent, and skipping ahead would splice a gap into the
                # middle of the answer.
                logger.warning("Teams: answer chunk delivery failed; stopping")
                return

    async def close(self) -> None:
        """Idempotent teardown: finalize the turn if it never reached on_done."""
        if not self._finalized:
            await self.on_done(stop_reason="error")

    # -- helpers ------------------------------------------------------------
    def text(self) -> str:
        """The turn's visible answer so far (OPTIONS stripped)."""
        return _strip_options("".join(self._buf).strip())
