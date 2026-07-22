"""Layer 2b -- Discord ``Renderer`` + interactive approval decider.

``DiscordRenderer`` maps the channel-neutral ``OutputEvent`` stream (routed by
the base :class:`Renderer`'s ``dispatch``) onto Discord's REST API:

* ``on_turn_start`` -- typing indicator loop (Discord's lasts ~10s per trigger).
* ``on_text_chunk`` -- throttled in-place ``edit_message`` streaming, with any
  trailing ``[OPTIONS:]`` markup held back from the visible stream.
* ``on_tool_call`` -- a transient ``🔧 {tool}…`` footer on live frames.
* ``on_prompt_choice`` -- Approve/Deny buttons as a SEPARATE message (so
  streaming edits don't clobber them).
* ``on_compaction`` -- a lightweight "compacting…" note.
* ``on_done`` -- the final edit, splitting long output at the capability's
  char cap and attaching the ``[OPTIONS:]`` button rows to the last chunk.

Discord renders standard Markdown natively, so unlike Telegram there is no
HTML translation pass -- the final seal sends the markdown as-is. Steer
rotation (sealing the pre-steer segment and opening a fresh message headed by
a "↪️ steered" chip) mirrors the Telegram renderer.

``DiscordApprovalDecider`` is the interactive ladder's awaiter: ``__call__``
registers a Future keyed by ``session:request_id`` and awaits a button press,
denying by default on timeout; the interaction handler resolves it via
``resolve_global``.

Dependency direction is ``discord -> messaging`` (allowed).
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from typing import TYPE_CHECKING, Any

from kiro_crew.constants import OPTIONS_RE_TRAILER
from kiro_crew.messaging.renderer import Renderer
from kiro_crew.messaging.transport import TransportCapabilities

if TYPE_CHECKING:
    from kiro_crew.discord.client import DiscordClient

logger = logging.getLogger(__name__)

# Discord's typing indicator lasts ~10s per trigger; refresh just under that
# for the duration of a turn.
_TYPING_REFRESH_S = 8.0

# Min seconds between live streaming edits to one message. Discord rate-limits
# message edits (~5/5s per channel), so we coalesce chunks and edit in place at
# most this often. The final edit always lands regardless of throttle.
_EDIT_THROTTLE_S = 1.2

# Interactive approval wait; deny-by-default when it elapses with no press.
_APPROVAL_TIMEOUT_S = 300.0

# Button style constants (Discord component styles).
_STYLE_PRIMARY = 1
_STYLE_SECONDARY = 2
_STYLE_SUCCESS = 3
_STYLE_DANGER = 4

# Trailing "[OPTIONS: a | b | c]" -- extracted for button-row rendering. Matched
# only at the very END of the message, so use the DOTALL/trailer canonical
# parser. Defined once in constants.py (shared with the Slack/dashboard/Telegram/
# WeCom surfaces) so the ReDoS-hardened grammar can never drift; see
# OPTIONS_RE_TRAILER for the full rationale. Per-choice whitespace is stripped by
# the caller.
_OPTIONS_RE = OPTIONS_RE_TRAILER

# kiro-cli's inline "[STEERING steer-<id>: …]" steer-ack marker (see the
# Telegram renderer for the full rationale — Discord likewise has no parser).
_STEER_MARKER_RE = re.compile(r"\[STEERING\b[^\]]*\]", re.IGNORECASE)
_STEER_SUMMARY_RE = re.compile(r"\[STEERING\s+steer-[0-9a-f]+\s*:\s*([^\]]*)\]", re.IGNORECASE)


def _extract_options(text: str) -> tuple[str, list[str]]:
    """Split text into (body, options). Handles the streamed partial too."""
    m = _OPTIONS_RE.search(text)
    if m:
        body = text[: m.start()].rstrip()
        options = [o.strip() for o in m.group(1).split("|") if o.strip()]
        return body, options
    # Hold back an incomplete "[OPTIONS…" fragment mid-stream.
    idx = text.rfind("[OPTIONS")
    if idx != -1 and "]" not in text[idx:]:
        return text[:idx].rstrip(), []
    return text, []


def _strip_steering(text: str) -> str:
    """Remove kiro-cli's inline ``[STEERING …]`` steer-ack marker from output,
    including an UNCLOSED trailing fragment still streaming in (see the
    Telegram renderer for the show-then-vanish rationale)."""
    cleaned = _STEER_MARKER_RE.sub("", text)
    cleaned = re.sub(r"\[STEERING\b[^\]]*$", "", cleaned)  # unclosed, streaming
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _neutralize_md(raw: str) -> str:
    """Collapse whitespace, cap length, and strip Markdown control chars from a
    steer's text so the chip renders literally (inside a blockquote) and can't
    perturb surrounding formatting."""
    t = " ".join((raw or "").split())[:120]
    return re.sub(r"[*_`\[\]()]", "", t)


def build_option_components(options: list[str]) -> list[dict] | None:
    """Build Discord button action rows from ``[OPTIONS:]`` labels.

    ``custom_id`` is the index only (``opt:<i>``) -- Discord caps it at 100
    chars and the label is recovered from the button text at interaction time.
    Up to 5 buttons per action row, max 5 rows (25 options); labels cap at 80
    chars per the component spec.
    """
    if not options:
        return None
    rows: list[dict] = []
    row: list[dict] = []
    for i, opt in enumerate(options[:25]):
        row.append(
            {
                "type": 2,  # button
                "style": _STYLE_SECONDARY,
                "label": opt[:80],
                "custom_id": f"opt:{i}",
            }
        )
        if len(row) == 5:
            rows.append({"type": 1, "components": row})  # action row
            row = []
    if row:
        rows.append({"type": 1, "components": row})
    return rows


def _split_text(text: str, limit: int) -> list[str]:
    """Split text into <=``limit`` chunks, preferring paragraph boundaries."""
    if limit <= 0 or len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        split_at = text.rfind("\n\n", 0, limit)
        if split_at < limit // 2:
            split_at = text.rfind("\n", 0, limit)
        if split_at < limit // 4:
            split_at = limit
        chunks.append(text[:split_at].rstrip())
        text = text[split_at:].lstrip()
    return chunks


_FENCE_RE = re.compile(r"```[^\n]*\n?(.*?)```", re.DOTALL)


def _split_markdown(text: str, limit: int) -> list[str]:
    """Split markdown into <=``limit`` chunks, keeping fenced code blocks
    balanced: close a dangling fence at a chunk's end and reopen it at the next
    chunk's start, so every chunk is self-contained markdown (Discord renders
    an unbalanced fence as literal backticks)."""
    chunks = _split_text(text, limit)
    if len(chunks) <= 1:
        return chunks
    out: list[str] = []
    carry_open = False
    for ch in chunks:
        if carry_open:
            ch = "```\n" + ch
        if ch.count("```") % 2 == 1:
            ch = ch.rstrip() + "\n```"
            carry_open = True
        else:
            carry_open = False
        out.append(ch)
    return out


class DiscordApprovalDecider:
    """Awaits a button approval for a tool-permission request.

    Process-global Future registry keyed by ``session_key:request_id`` so
    concurrent turns (and users) never resolve each other's prompts. Denies by
    default when the wait elapses.

    Nonce guard: ACP request IDs are reusable (a provider or gateway restart
    resets the sequence), so a stale Approve button whose ``custom_id`` carries
    an old request ID could otherwise resolve a NEW pending request for an
    unrelated tool. Each rendered prompt therefore embeds an unpredictable
    per-prompt nonce (``register_nonce``), and ``resolve_global`` only resolves
    when the pressed button's nonce matches the one registered for that key —
    a press from any earlier prompt (or earlier process) fails closed.
    """

    _REGISTRY: dict[str, "asyncio.Future[bool]"] = {}
    #: key -> the per-prompt nonce embedded in that prompt's buttons.
    _NONCES: dict[str, str] = {}

    def __init__(self, *, session_key: str) -> None:
        self._session_key = session_key

    @staticmethod
    def key(session_key: str, request_id: str | int) -> str:
        return f"{session_key}:{request_id}"

    @classmethod
    def register_nonce(cls, key: str) -> str:
        """Mint + register the per-prompt nonce for *key* (renderer-side)."""
        nonce = secrets.token_hex(8)
        cls._NONCES[key] = nonce
        return nonce

    async def __call__(self, event: Any) -> bool:
        k = self.key(self._session_key, getattr(event, "request_id", ""))
        fut: "asyncio.Future[bool]" = asyncio.get_running_loop().create_future()
        DiscordApprovalDecider._REGISTRY[k] = fut
        try:
            return bool(await asyncio.wait_for(fut, _APPROVAL_TIMEOUT_S))
        except asyncio.TimeoutError:
            return False  # deny-by-default on timeout
        finally:
            DiscordApprovalDecider._REGISTRY.pop(k, None)
            # Retire the prompt's nonce with the decision window: a press on
            # the (now stale) buttons can never resolve a future request.
            DiscordApprovalDecider._NONCES.pop(k, None)

    @classmethod
    def resolve_global(cls, key: str, approved: bool, *, nonce: str = "") -> bool:
        """Resolve a pending approval by key. Returns True iff one was waiting
        AND the button's nonce matches the registered per-prompt nonce."""
        expected = cls._NONCES.get(key)
        if not expected or not nonce or not secrets.compare_digest(nonce, expected):
            return False  # stale/foreign button — fail closed
        fut = cls._REGISTRY.get(key)
        if fut is not None and not fut.done():
            fut.set_result(bool(approved))
            return True
        return False


class DiscordRenderer(Renderer):
    """Streams a turn to Discord via in-place message edits + button rows."""

    channel_type = "discord"

    def __init__(
        self,
        client: "DiscordClient",
        channel_id: str,
        capabilities: TransportCapabilities,
        *,
        session_key: str = "",
    ) -> None:
        super().__init__(capabilities)
        self._client = client
        self._channel_id = channel_id
        self._session_key = session_key
        self._buf: list[str] = []
        self._last_tool = ""
        # Transient tool-activity footer ("🔧 {tool}…") shown ONLY on live
        # streaming frames — never stored in _buf, so seals/finals stay clean.
        self._tool = ""
        self._finalized = False
        self._closed = False
        self._typing_task: "asyncio.Task[None] | None" = None
        # Live edit-streaming state (mirrors the Telegram renderer): the
        # message being edited (None -> next render sends a new one), the last
        # text pushed (skip no-op edits), and the edit throttle timestamp.
        self._stream_mid: str | None = None
        self._shown = ""
        self._last_edit = 0.0
        self._seal_count = 0  # rotations so far == index into _steer_texts
        # Chip pending from the last rotation, NOT yet in _buf (materializes
        # only when real post-steer text arrives — see the Telegram renderer).
        self._pending_chip = ""
        # User's own mid-turn steer texts (in order), recorded via note_steer.
        self._steer_texts: list[str] = []

    # -- lifecycle ----------------------------------------------------------
    async def on_turn_start(self) -> None:
        # Typing indicator only — no placeholder bubble. Idempotent (dispatch
        # + driver both call this).
        if self._typing_task is not None or self._closed:
            return
        self._typing_task = asyncio.create_task(self._typing_loop())

    async def _typing_loop(self) -> None:
        """Keep the 'typing…' indicator alive (~10s per trigger) for the
        duration of the turn. Cancelled by ``_stop_typing``."""
        try:
            while not self._closed:
                try:
                    await self._client.send_typing(self._channel_id)
                except Exception:
                    logger.debug("Discord: typing refresh failed", exc_info=True)
                await asyncio.sleep(_TYPING_REFRESH_S)
        except asyncio.CancelledError:
            pass

    def _stop_typing(self) -> None:
        self._closed = True
        task, self._typing_task = self._typing_task, None
        if task is not None and not task.done():
            task.cancel()

    async def on_text_chunk(self, text: str) -> None:
        self._buf.append(text)
        self._tool = ""  # text resumed -> drop the transient tool footer
        # 1) Rotate to a fresh message at each COMPLETE [STEERING …] marker.
        await self._rotate_at_markers()
        # 1b) Materialize the pending chip once real post-steer text exists.
        self._materialize_chip()
        # 2) Rotate when a segment would exceed one Discord message.
        await self._rotate_on_length()
        # 3) Live-stream the current segment (throttled in-place edit).
        await self._stream_live()

    def _materialize_chip(self) -> None:
        """Prepend the pending steer chip to the segment — but only when the
        segment carries real text (an end-of-stream marker never posts a
        chip-only ack bubble)."""
        if self._pending_chip and self._segment_text().strip():
            body = "".join(self._buf).lstrip("\n")
            self._buf = [f"{self._pending_chip}\n\n{body}"]
            self._pending_chip = ""

    async def _rotate_at_markers(self) -> None:
        while True:
            self._materialize_chip()
            raw = "".join(self._buf)
            m = _STEER_MARKER_RE.search(raw)  # first COMPLETE marker only
            if m is None:
                return
            self._buf = [raw[: m.start()]]
            # Length-rotate the pre-marker segment BEFORE sealing (a chunk can
            # deliver oversized text and the marker together).
            await self._rotate_on_length()
            sealed = bool(self._segment_text().strip())
            await self._seal_current()  # freeze the pre-steer message as-is
            sm = _STEER_SUMMARY_RE.match(raw, m.start())
            summary = _neutralize_md(sm.group(1)) if sm else ""
            if summary:
                chip: str | None = f"> ↪️ {summary}"
            else:
                chip = self._chip_for_seal(self._seal_count)
            self._seal_count += 1
            self._pending_chip = chip or ""
            self._buf = [raw[m.end() :]]  # new segment: steered continuation
            if sealed:
                self._open_new_message()
            # else: the pre-marker segment was empty (e.g. a tool-only live
            # "🔧 …" message) — keep _stream_mid so the steered continuation
            # replaces the transient footer in place.

    async def _rotate_on_length(self) -> None:
        """Rotate when the segment exceeds one Discord message, keeping fenced
        code blocks balanced across the cut and never splitting a trailing
        protocol directive (``[OPTIONS:]`` / streaming ``[STEERING`` fragment)
        in half."""
        limit = self._limit()
        raw = "".join(self._buf)
        if len(raw) <= limit:
            return
        partial = ""
        cm = _OPTIONS_RE.search(raw)
        if cm:
            raw, partial = raw[: cm.start()], raw[cm.start() :]
        else:
            idx = max(raw.rfind("[STEERING"), raw.rfind("[OPTIONS"))
            if idx != -1 and "]" not in raw[idx:]:
                raw, partial = raw[:idx], raw[idx:]
        chunks = _split_markdown(raw, limit)
        for ch in chunks[:-1]:
            self._buf = [ch]
            await self._seal_current()
            self._open_new_message()
        self._buf = [(chunks[-1] if chunks else "") + partial]

    def _open_new_message(self) -> None:
        """Next render creates a fresh message instead of editing the old one."""
        self._stream_mid = None
        self._shown = ""

    def _segment_text(self) -> str:
        """Current segment's markdown source (chip already seeded into _buf),
        with the steer marker stripped. Discord renders markdown natively, so
        no further translation is needed."""
        return _strip_steering("".join(self._buf))

    async def _stream_live(self, *, force: bool = False) -> None:
        """Throttled in-place edit of the current segment. Sends the message on
        first render. The transient ``🔧 {tool}…`` footer is appended ONLY here
        (live frames) — seals and the final render never carry it. ``force``
        bypasses the throttle so a tool-call event surfaces immediately."""
        now = time.monotonic()
        if not force and now - self._last_edit < _EDIT_THROTTLE_S:
            return
        # Hold back trailing [OPTIONS:] markup from live frames.
        body, _ = _extract_options(self._segment_text())
        footer = f"-# 🔧 {self._tool}…" if self._tool else ""
        if footer:
            room = self._limit() - len(footer) - 2
            text = f"{body[:room]}\n\n{footer}".strip() if room > 0 else footer
        else:
            text = body[: self._limit()]
        if not text or text == self._shown:
            return
        self._last_edit = now
        self._shown = text
        if self._stream_mid is None:
            mid = await self._client.send_message(self._channel_id, text)
            if mid is not None:
                self._stream_mid = mid
        else:
            await self._client.edit_message(self._channel_id, self._stream_mid, text)

    async def _seal_current(self, *, components: list[dict] | None = None) -> None:
        """Finalize the current segment: land the full markdown text (and
        optional button rows). Edits the streamed message in place, or sends
        one if the segment never streamed (e.g. throttled out). Empty segments
        are skipped so a bare steer doesn't post a blank bubble."""
        text = self._segment_text().strip()
        if not text:
            return
        if self._stream_mid is not None:
            ok = await self._client.edit_message(
                self._channel_id, self._stream_mid, text, components=components
            )
            if ok:
                return
            # Edit failed — the live message is gone (e.g. deleted mid-turn).
            # Fall through and SEND the final content so the completed answer
            # (and its buttons) is never silently lost.
            self._stream_mid = None
        await self._client.send_message(self._channel_id, text, components=components)

    async def on_thinking(self, text: str) -> None:
        # Discord does not surface reasoning inline (parity with Telegram).
        return None

    async def on_tool_call(
        self, tool_call_id: str, title: str, tool_kind: str = "", tool_purpose: str = ""
    ) -> None:
        # Surface mid-turn tool activity as a transient "🔧 {tool}…" footer on
        # the live bubble (force=True so it shows immediately). We deliberately
        # do NOT seal a message here — see the Telegram renderer's rationale.
        self._last_tool = title or tool_kind or "tool"
        self._tool = self._last_tool
        await self._stream_live(force=True)

    async def on_prompt_choice(self, options: list[dict[str, Any]], request_id: str | int) -> None:
        # Approve/Deny as a SEPARATE message so ongoing streaming edits to the
        # answer bubble don't clobber the buttons. custom_id carries a
        # per-prompt nonce (a:<request_id>:<nonce>:<1|0>, well under Discord's
        # 100-char cap) so a stale button from a reused request ID can never
        # resolve a later prompt; the interaction handler validates it via
        # ``resolve_global``.
        rid = str(request_id)
        nonce = DiscordApprovalDecider.register_nonce(
            DiscordApprovalDecider.key(self._session_key, rid)
        )
        components = [
            {
                "type": 1,
                "components": [
                    {
                        "type": 2,
                        "style": _STYLE_SUCCESS,
                        "label": "✅ Approve",
                        "custom_id": f"a:{rid}:{nonce}:1",
                    },
                    {
                        "type": 2,
                        "style": _STYLE_DANGER,
                        "label": "🚫 Deny",
                        "custom_id": f"a:{rid}:{nonce}:0",
                    },
                ],
            }
        ]
        tool = self._last_tool or "this tool"
        await self._client.send_message(
            self._channel_id, f"🔐 Approve `{tool}`?", components=components
        )

    async def on_compaction(self, context_usage_pct: float) -> None:
        try:
            await self._client.send_message(self._channel_id, "🗜️ Compacting context…")
        except Exception:
            logger.debug("Discord: compaction notice send failed", exc_info=True)

    def note_steer(self, text: str) -> None:
        """Record the user's own mid-turn steer text (their typed words, NOT
        the redacted backend echo); rendered as an inline "↪️ steered" chip.
        Capped to avoid unbounded growth on a pathological steer burst."""
        t = (text or "").strip()
        if t and len(self._steer_texts) < 50:
            self._steer_texts.append(t)

    async def on_done(self, stop_reason: str = "") -> None:
        if self._finalized:
            return
        self._finalized = True
        self._stop_typing()
        ok = stop_reason != "error"
        # Flush any trailing rotation, then finalize the current segment with
        # the [OPTIONS:] button rows attached to the last chunk.
        await self._rotate_at_markers()
        self._materialize_chip()
        # Extract the trailing [OPTIONS:] BEFORE length rotation (see the
        # Telegram renderer's rationale).
        body_raw, opts = _extract_options("".join(self._buf))
        self._buf = [body_raw]
        components = build_option_components(opts) if opts else None
        # No-rotation fallback: steers were injected but no marker rotated —
        # prepend one summary chip so they're still shown.
        if self._seal_count == 0 and self._steer_texts:
            quoted = [q for q in (_neutralize_md(t) for t in self._steer_texts) if q]
            if quoted:
                body = self._segment_text().strip()
                summary = "> " + " · ".join(quoted)
                self._buf = [summary + ("\n\n" + body if body else "")]
        await self._rotate_on_length()
        if not self._segment_text().strip():
            # Nothing to post. Earlier rotated segments carried the turn ->
            # stay silent; otherwise show a placeholder. An extracted button
            # row (options-only body) must ALWAYS reach the user.
            if self._seal_count > 0 and components is None:
                return
            placeholder = "…" if ok else "⚠️ Error — please try again"
            if self._stream_mid is not None:
                await self._client.edit_message(
                    self._channel_id,
                    self._stream_mid,
                    placeholder,
                    components=components,
                )
            else:
                await self._client.send_message(
                    self._channel_id, placeholder, components=components
                )
            return
        await self._seal_current(components=components)

    def _limit(self) -> int:
        # Leave headroom below the 2000-char cap for the chip/footer overhead
        # so a chunk can never overflow and get cut mid-word by the API.
        cap = self.capabilities.max_message_chars or 1900
        return max(500, cap - 100)

    def _chip_for_seal(self, i: int) -> str | None:
        """The steer chip (a "> quote" blockquote of the USER's own words) that
        heads the segment opened by the i-th rotation."""
        if 0 <= i < len(self._steer_texts):
            t = _neutralize_md(self._steer_texts[i])
            return f"> ↪️ {t}" if t else None
        return None

    async def close(self) -> None:
        """Idempotent teardown: stop the typing indicator and finalize the turn
        if it never reached on_done."""
        self._stop_typing()
        if not self._finalized:
            await self.on_done(stop_reason="error")

    # -- helpers ------------------------------------------------------------
    def _options(self) -> list[str]:
        raw = "".join(self._buf).strip()
        _, opts = _extract_options(raw)
        return opts
