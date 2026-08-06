"""Layer 2b -- Feishu ``Renderer``.

Maps the channel-neutral ``OutputEvent`` stream onto a single Feishu REST
reply anchored to the inbound message_id:

* ``on_turn_start``   -- no-op (no streaming placeholder in v1).
* ``on_text_chunk``   -- buffers text; trailing ``[OPTIONS:]`` stripped.
* ``on_tool_call``    -- updates a transient tool-footer in the buffer.
* ``on_prompt_choice``-- no-op: Feishu has no interactive buttons in v1
  (the driver only dispatches this for INTERACTIVE + a decider, and
  FeishuDispatcher runs decider-less).
* ``on_compaction``   -- logged only (threshold notices go post-turn).
* ``on_done``         -- sends the complete accumulated text as one reply.
* ``close``           -- idempotent finalisation (sends on error if needed).

Dependency direction is ``feishu -> messaging`` (allowed).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from kiro_crew.constants import OPTIONS_RE_TRAILER
from kiro_crew.messaging.renderer import Renderer
from kiro_crew.messaging.transport import TransportCapabilities

if TYPE_CHECKING:
    from kiro_crew.feishu.client import LarkClient

logger = logging.getLogger(__name__)

# Trailing "[OPTIONS: a | b | c]" chip trailer -- Feishu can't render tappable
# chips, so we strip it entirely.  Shared with every other surface via
# constants.py so the ReDoS-hardened grammar never drifts.
_OPTIONS_RE = OPTIONS_RE_TRAILER


def _strip_options(text: str) -> str:
    """Remove a trailing ``[OPTIONS: …]`` chip trailer."""
    m = _OPTIONS_RE.search(text)
    if m:
        return text[: m.start()].rstrip()
    # Also strip a still-streaming partial ``[OPTIONS…`` fragment so raw
    # markup never flashes to the user.
    idx = text.rfind("[OPTIONS")
    if idx != -1 and "]" not in text[idx:]:
        return text[:idx].rstrip()
    return text


class FeishuRenderer(Renderer):
    """Buffers a complete turn and sends it as one Feishu reply on ``on_done``.

    v1 is intentionally simple: no streaming, no edit-in-place.  A streaming
    variant (send a placeholder, then update with ``lark_client.im.v1.message.
    update``) is a natural follow-up once the core integration is stable.
    """

    channel_type = "feishu"

    def __init__(
        self,
        client: "LarkClient",
        message_id: str,
        capabilities: TransportCapabilities,
        *,
        session_key: str = "",
    ) -> None:
        super().__init__(capabilities)
        self._client = client
        self._message_id = message_id
        self._session_key = session_key
        self._buf: list[str] = []
        self._tool: str = ""
        self._finalized = False

    # -- Output event handlers ----------------------------------------------

    async def on_turn_start(self) -> None:
        # No streaming placeholder in v1.  A "thinking" reaction could be added
        # here via ``lark_client.im.v1.message.reactions.create`` once proven
        # useful.
        pass

    async def on_text_chunk(self, text: str) -> None:
        self._buf.append(text)
        self._tool = ""  # text resumed -> clear transient tool footer

    async def on_thinking(self, text: str) -> None:
        # Feishu v1 does not surface reasoning inline.
        return None

    async def on_tool_call(
        self,
        tool_call_id: str,
        title: str,
        tool_kind: str = "",
        tool_purpose: str = "",
    ) -> None:
        # Record but don't push mid-turn (single-shot reply only).
        self._tool = title or tool_kind or "工具"

    async def on_prompt_choice(
        self, options: list[dict[str, Any]], request_id: str | int
    ) -> None:
        # Feishu has no interactive buttons in v1.  The driver dispatches this
        # only for INTERACTIVE + a decider; FeishuDispatcher runs decider-less,
        # so this is never reached -- kept as a safe no-op to satisfy the
        # Renderer contract.
        logger.debug("Feishu: prompt_choice ignored (no interactive buttons)")

    async def on_compaction(self, context_usage_pct: float) -> None:
        # Threshold notices are surfaced post-turn by the dispatcher; a
        # mid-turn frame would corrupt the single-shot answer bubble.
        logger.debug("Feishu: compaction status %.0f%%", context_usage_pct)

    async def on_done(self, stop_reason: str = "") -> None:
        if self._finalized:
            return
        self._finalized = True
        ok = stop_reason != "error"
        content = self.text() or ("…" if ok else "⚠️ 出错了，请重试")
        await self._client.send_reply(self._message_id, content)

    async def close(self) -> None:
        """Idempotent teardown -- finalise if ``on_done`` was never reached."""
        if not self._finalized:
            await self.on_done(stop_reason="error")

    # -- Helpers ------------------------------------------------------------

    def text(self) -> str:
        """The complete visible answer so far (OPTIONS stripped).

        Used both by ``on_done`` (final send) and by the dispatcher to persist
        the reply to history.
        """
        return _strip_options("".join(self._buf).strip())
