"""Layer 2b -- WeCom ``Renderer``.

Maps the channel-neutral ``OutputEvent`` stream (routed by the base
:class:`Renderer`'s ``dispatch``) onto WeCom AI-bot ``aibot_respond_msg`` WS
frames:

* ``on_turn_start`` -- a "🤔 …" placeholder stream frame.
* ``on_text_chunk`` -- throttled full-text stream frames (each frame REPLACES
  the bubble content), with any trailing ``[OPTIONS:]`` markup stripped (WeCom
  can't render tappable chips).
* ``on_tool_call`` -- a transient ``🔧 正在运行：{tool}…`` footer.
* ``on_prompt_choice`` -- no-op: WeCom has no interactive buttons, and the
  driver only dispatches this for INTERACTIVE + a decider; WeCom runs
  decider-less (deny-by-default), so this never fires.
* ``on_compaction`` -- logged only (a mid-turn out-of-band frame would pollute
  the single answer bubble; the dispatcher surfaces threshold notices).
* ``on_done`` -- the final ``finish=true`` frame (locks the bubble), falling
  back to a one-shot ``response_url`` POST if the WS stream is
  unavailable/expired.

Dependency direction is ``wecom -> messaging`` (allowed).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from kiro_crew.constants import OPTIONS_RE_TRAILER
from kiro_crew.messaging.renderer import Renderer
from kiro_crew.messaging.transport import TransportCapabilities
from kiro_crew.wecom.client import new_stream_id

if TYPE_CHECKING:
    from kiro_crew.wecom.client import WeComClient

logger = logging.getLogger(__name__)

# Min seconds between intermediate stream frames: paces the typewriter effect
# and avoids hammering the stream API. The final finish frame ignores it.
_STREAM_THROTTLE_S = 0.7

# Placeholder shown immediately while the agent is still generating.
_THINKING = "🤔 …"

# Trailing "[OPTIONS: a | b | c]" chip trailer (a dashboard convention WeCom
# can't render as tappable chips). Matched only at the very END of the message,
# so use the DOTALL/trailer canonical parser. Defined once in constants.py
# (shared with the Slack/dashboard/Discord/Telegram surfaces) so the
# ReDoS-hardened grammar can never drift; see OPTIONS_RE_TRAILER for the full
# rationale. Per-choice whitespace is stripped by the caller.
_OPTIONS_RE = OPTIONS_RE_TRAILER


def _strip_options(text: str) -> str:
    """Remove a trailing ``[OPTIONS: a | b | c]`` chip trailer.

    WeCom can't render tappable chips, so we drop the trailer entirely -- the
    user just replies naturally. Also hides a still-streaming partial
    ``[OPTIONS…`` fragment (no closing ``]``) so it never flashes as raw text.
    """
    m = _OPTIONS_RE.search(text)
    if m:
        return text[: m.start()].rstrip()
    idx = text.rfind("[OPTIONS")
    if idx != -1 and "]" not in text[idx:]:
        return text[:idx].rstrip()
    return text


class WeComRenderer(Renderer):
    """Streams a turn to WeCom via WS ``aibot_respond_msg`` frames + fallback.

    Sends a "thinking" placeholder on ``on_turn_start``, throttled full-text
    updates (each frame carries the full accumulated text, replacing the
    bubble) on ``on_text_chunk``, and a final ``finish=true`` frame on
    ``on_done``. If the WS stream path is unavailable/expired it falls back to a
    single one-shot POST to the inbound message's ``response_url``.
    """

    channel_type = "wecom"

    def __init__(
        self,
        client: "WeComClient",
        req_id: str,
        response_url: str,
        capabilities: TransportCapabilities,
        *,
        session_key: str = "",
    ) -> None:
        super().__init__(capabilities)
        self._client = client
        self._req_id = req_id
        self._response_url = response_url
        self._session_key = session_key
        self._stream_id = new_stream_id()
        self._buf: list[str] = []
        self._last_send = 0.0
        # Stream only if we have a req_id to correlate; else go straight to the
        # one-shot response_url fallback at on_done.
        self._stream_ok = bool(req_id)
        self._tool = ""
        self._started = False
        self._finalized = False
        # How much of the answer earlier bubbles already carry. A WeCom stream
        # frame REPLACES its bubble with the full text it is given, so after
        # rolling to a fresh bubble the continuation must send only what comes
        # after this offset — otherwise the reader sees the whole answer twice.
        self._carried = 0
        # Absolute answer lengths carried by the last two frames put on the wire.
        # Two, not one, because a refusal is only observed on a LATER push, so the
        # newest frame is the one that may have been rejected — see
        # ``_roll_if_sealed``.
        self._sent_abs = 0
        self._prev_sent_abs = 0

    # -- lifecycle ----------------------------------------------------------
    async def on_turn_start(self) -> None:
        if self._started:  # idempotent (dispatch + driver both call it)
            return
        self._started = True
        if self._stream_ok:
            self._stream_ok = await self._client.send_stream(
                self._req_id, self._stream_id, _THINKING, finish=False
            )
        self._last_send = time.monotonic()

    async def on_text_chunk(self, text: str) -> None:
        self._buf.append(text)
        self._tool = ""  # text resumed -> drop the transient tool footer
        await self._push(force=False)

    async def on_thinking(self, text: str) -> None:
        # WeCom does not surface reasoning inline.
        return None

    async def on_tool_call(
        self, tool_call_id: str, title: str, tool_kind: str = "", tool_purpose: str = ""
    ) -> None:
        self._tool = title or tool_kind or "工具"
        await self._push(force=True)

    async def on_prompt_choice(self, options: list[dict[str, Any]], request_id: str | int) -> None:
        # WeCom has no interactive buttons. The driver only dispatches
        # prompt_choice for INTERACTIVE + a decider, and WeCom runs decider-less
        # (deny-by-default), so this is never reached -- kept as a safe no-op to
        # satisfy the Renderer contract.
        logger.debug("WeCom: prompt_choice ignored (no interactive buttons)")

    async def on_compaction(self, context_usage_pct: float) -> None:
        # A mid-turn out-of-band frame would pollute the single answer bubble;
        # the dispatcher surfaces soft/hard threshold notices post-turn instead.
        logger.debug("WeCom: compaction status %.0f%%", context_usage_pct)

    async def on_done(self, stop_reason: str = "") -> None:
        if self._finalized:
            return
        self._finalized = True
        self._tool = ""  # never lock a tool footer into the final answer
        ok = stop_reason != "error"
        answer = self.text()
        # The final frame is the one that MUST land, so give it a live bubble: a
        # turn that outran the platform's stream lifetime has a sealed one, and
        # writing the answer there delivers it nowhere.
        #
        # Roll only when there is something left to deliver. If the sealed bubble
        # already carries the whole answer, opening a fresh one just to seal it
        # would post a bubble with no content in it — the seal is cosmetic
        # (it locks the message), and an unsealed bubble holding the full answer
        # is strictly better than a spurious empty one.
        if self._client.stream_is_dead(self._stream_id) and len(answer) > self._prev_sent_abs:
            self._roll_if_sealed()
        remainder = answer[self._carried :]
        if answer:
            content = remainder
        else:
            content = "…" if ok else "⚠️ 出错了，请重试"
        if self._stream_ok and content:
            sent = await self._client.send_stream(
                self._req_id, self._stream_id, content, finish=True
            )
            if sent:
                return
        if not content:
            return
        # Fallback: stream never started or died mid-turn -- one-shot POST.
        await self._client.send_reply(self._response_url, content)

    async def close(self) -> None:
        """Idempotent teardown: finalize the turn if it never reached on_done."""
        if not self._finalized:
            await self.on_done(stop_reason="error")

    # -- helpers ------------------------------------------------------------
    def text(self) -> str:
        """The turn's visible answer so far (OPTIONS stripped, no tool footer).

        Used both for the final frame and to persist the reply to history.
        """
        return _strip_options("".join(self._buf).strip())

    def _compose(self) -> str:
        """Streaming content = answer text + optional transient tool footer.

        Only the part of the answer this bubble owns (see ``_carried``).
        """
        body = self.text()[self._carried :]
        if self._tool:
            footer = f"🔧 正在运行：{self._tool}…"
            return f"{body}\n\n{footer}" if body else footer
        return body

    def _roll_if_sealed(self) -> None:
        """Move to a fresh bubble when WeCom has sealed the current one.

        A bubble stops accepting writes after the platform's 10-minute stream
        lifetime (errcode 846608), or if its req_id becomes unroutable (846605).
        An agentic turn doing tool work routinely runs past ten minutes, and the
        refusal is asynchronous — ``send_stream`` reports only that the frame
        reached the socket — so without this the renderer keeps "succeeding" into
        a sealed bubble and the rest of the answer, including the final frame, is
        never seen.

        Rolling keeps what the sealed bubble already shows and continues after
        it, so the answer is delivered in two bubbles rather than lost.

        Where the continuation RESUMES from is deliberately conservative. The
        refusal is only observed on a later push, so the most recent frame is
        exactly the one that may have been rejected — resuming after it would drop
        text the reader never received. The continuation therefore resumes from
        the frame BEFORE it, which was acknowledged by being followed by another
        accepted send. The cost is that one frame's worth of text can appear
        twice; the alternative is a silent hole in the answer, and a visible
        repeat is the better failure.
        """
        if not self._client.stream_is_dead(self._stream_id):
            return
        self._carried = self._prev_sent_abs
        self._stream_id = new_stream_id()
        logger.info("WeCom: stream bubble sealed by the platform, continuing in a new one")

    async def _push(self, *, force: bool) -> None:
        if not self._stream_ok:
            return
        now = time.monotonic()
        if not force and now - self._last_send < _STREAM_THROTTLE_S:
            return
        self._roll_if_sealed()
        content = self._compose()
        if not content:
            return
        cap = self.capabilities.max_message_chars
        if cap > 0:
            content = content[:cap]
        answer_len = len(self.text())
        self._stream_ok = await self._client.send_stream(
            self._req_id, self._stream_id, content, finish=False
        )
        if self._stream_ok:
            self._prev_sent_abs, self._sent_abs = self._sent_abs, answer_len
        self._last_send = now
