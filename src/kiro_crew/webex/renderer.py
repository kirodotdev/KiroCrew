"""Layer 2b -- Webex ``Renderer``.

Maps the channel-neutral ``OutputEvent`` stream (routed by the base
:class:`Renderer`'s ``dispatch``) onto Webex REST calls:

* ``on_turn_start`` -- posts a "🤔 Thinking…" placeholder message.
* ``on_tool_call`` -- edits the placeholder to "🔧 Running: {tool}…",
  throttled AND budgeted: Webex caps a message at 10 edits, so status
  updates spend at most ``_TOOL_EDIT_BUDGET`` edits and the final answer
  always has one left.
* ``on_text_chunk`` -- buffered only (no typewriter streaming; see the
  edit cap), with any trailing ``[OPTIONS:]`` markup stripped (Webex has
  no tappable chips here).
* ``on_prompt_choice`` -- no-op: the driver only dispatches this for
  INTERACTIVE + a decider; Webex runs decider-less (deny-by-default).
* ``on_compaction`` -- logged only; the dispatcher surfaces threshold
  notices as separate messages post-turn.
* ``on_done`` -- edits the placeholder into the final answer (one edit),
  falling back to a fresh message if the edit fails; overflow past the
  message-size cap goes out as follow-up messages.

Dependency direction is ``webex -> messaging`` (allowed).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from kiro_crew.constants import OPTIONS_RE_TRAILER
from kiro_crew.messaging.renderer import Renderer
from kiro_crew.messaging.tables import TABLE_POLICY_CARDS
from kiro_crew.messaging.transport import TransportCapabilities
from kiro_crew.webex.client import WEBEX_MAX_TEXT, chunk_utf8

if TYPE_CHECKING:
    from kiro_crew.webex.client import WebexClient

logger = logging.getLogger(__name__)

# Webex allows 10 edits per message. Spend at most this many on transient
# tool-progress status so the final-answer edit (1) always fits with margin.
_TOOL_EDIT_BUDGET = 6

# Min seconds between tool-status edits: pacing on top of the hard budget.
_STATUS_THROTTLE_S = 2.0

# Placeholder shown immediately while the agent is still generating.
_THINKING = "🤔 Thinking…"

_ERROR_TEXT = "⚠️ Something went wrong — please try again."

# Trailing "[OPTIONS: a | b | c]" chip trailer (a dashboard convention Webex
# can't render as tappable chips). Matched only at the very END of the message,
# so use the DOTALL/trailer canonical parser. Defined once in constants.py
# (shared with the Slack/dashboard/Discord/Telegram/WeCom surfaces) so the
# ReDoS-hardened grammar can never drift; see OPTIONS_RE_TRAILER for the full
# rationale. Per-choice whitespace is stripped by the caller.
_OPTIONS_RE = OPTIONS_RE_TRAILER


def _strip_options(text: str) -> str:
    """Remove a trailing ``[OPTIONS: a | b | c]`` chip trailer.

    Webex has no tappable chips, so we drop the trailer entirely -- the user
    just replies naturally. Also hides a partial ``[OPTIONS…`` fragment (no
    closing ``]``) so it never lands as raw text.
    """
    m = _OPTIONS_RE.search(text)
    if m:
        return text[: m.start()].rstrip()
    idx = text.rfind("[OPTIONS")
    if idx != -1 and "]" not in text[idx:]:
        return text[:idx].rstrip()
    return text


class WebexRenderer(Renderer):
    """Renders a turn to a Webex room: placeholder -> status edits -> answer."""

    channel_type = "webex"

    def __init__(
        self,
        client: "WebexClient",
        room_id: str,
        capabilities: TransportCapabilities,
        *,
        session_key: str = "",
    ) -> None:
        super().__init__(capabilities)
        self._client = client
        self._room_id = room_id
        self._session_key = session_key
        self._buf: list[str] = []
        self._placeholder_id: str | None = None
        self._edits_used = 0
        self._last_status = 0.0
        self._started = False
        self._finalized = False

    # -- lifecycle ----------------------------------------------------------
    async def on_turn_start(self) -> None:
        if self._started:  # idempotent (dispatch + driver both call it)
            return
        self._started = True
        self._placeholder_id = await self._client.send_message(self._room_id, _THINKING)
        self._last_status = time.monotonic()

    async def on_text_chunk(self, text: str) -> None:
        # Buffered only: the 10-edit cap rules out typewriter streaming.
        self._buf.append(text)

    async def on_thinking(self, text: str) -> None:
        # Webex does not surface reasoning inline (parity with WeCom).
        return None

    async def on_tool_call(
        self, tool_call_id: str, title: str, tool_kind: str = "", tool_purpose: str = ""
    ) -> None:
        """Edit the placeholder to show tool progress, throttled + budgeted."""
        if self._placeholder_id is None or self._edits_used >= _TOOL_EDIT_BUDGET:
            return
        now = time.monotonic()
        if now - self._last_status < _STATUS_THROTTLE_S:
            return
        label = title or tool_kind or "tool"
        edited = await self._client.edit_message(
            self._placeholder_id, self._room_id, f"🔧 Running: {label}…"
        )
        if edited:
            self._edits_used += 1
            self._last_status = now
        else:
            # An edit failure (rate limit / edit cap) burns the whole status
            # budget so we never race the final-answer edit against the cap.
            self._edits_used = _TOOL_EDIT_BUDGET

    async def on_prompt_choice(self, options: list[dict[str, Any]], request_id: str | int) -> None:
        # The driver only dispatches prompt_choice for INTERACTIVE + a
        # decider, and Webex runs decider-less (deny-by-default), so this is
        # never reached -- kept as a safe no-op per the Renderer contract.
        logger.debug("Webex: prompt_choice ignored (no interactive buttons)")

    async def on_compaction(self, context_usage_pct: float) -> None:
        # The dispatcher surfaces threshold notices as separate messages.
        logger.debug("Webex: compaction status %.0f%%", context_usage_pct)

    async def on_done(self, stop_reason: str = "") -> None:
        if self._finalized:
            return
        self._finalized = True
        ok = stop_reason != "error"
        raw = self.text()
        content = self.render_tables_for_target(raw)
        if content != raw and len(content.encode("utf-8")) > WEBEX_MAX_TEXT:
            # Generated grids must stay in one message; cards and display-safe
            # raw text can use lossless byte chunks. An unrepresentable card run
            # reports its grid so the raw form wins.
            content, generated_grid = self.render_tables_for_target_with_metadata(
                raw,
                policy=TABLE_POLICY_CARDS,
            )
            if generated_grid and len(content.encode("utf-8")) > WEBEX_MAX_TEXT:
                safe_raw = self.safe_raw_table_fallback(raw, policy=TABLE_POLICY_CARDS)
                if safe_raw is not None:
                    content = safe_raw
        content = content or ("…" if ok else _ERROR_TEXT)
        # Byte-aware, lossless split: Webex caps messages in UTF-8 BYTES, so
        # the neutral character-based chunk_text could hand the client an
        # oversized chunk that gets tail-truncated (silent data loss).
        chunks = chunk_utf8(content) or ["…"]
        first, rest = chunks[0], chunks[1:]
        delivered = False
        if self._placeholder_id is not None:
            delivered = await self._client.edit_message(self._placeholder_id, self._room_id, first)
        if not delivered:
            sent = await self._client.send_message(self._room_id, first)
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
        for chunk in rest:
            sent = await self._client.send_message(self._room_id, chunk)
            if sent is None:
                # Stop at the first failed follow-up: the delivered prefix is
                # coherent, and skipping ahead would splice a gap into the
                # middle of the answer.
                logger.warning("Webex: follow-up chunk delivery failed; stopping")
                return

    async def close(self) -> None:
        """Idempotent teardown: finalize the turn if it never reached on_done."""
        if not self._finalized:
            await self.on_done(stop_reason="error")

    # -- helpers ------------------------------------------------------------
    def text(self) -> str:
        """The turn's visible answer so far (OPTIONS stripped).

        Used both for the final message and to persist the reply to history.
        """
        return _strip_options("".join(self._buf).strip())
