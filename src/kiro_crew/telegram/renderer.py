"""Layer 2b -- Telegram ``Renderer`` + interactive approval decider.

``TelegramRenderer`` maps the channel-neutral ``OutputEvent`` stream (routed by
the base :class:`Renderer`'s ``dispatch``) onto Telegram's Bot API:

* ``on_turn_start`` -- typing indicator + a "🤔 …" placeholder message.
* ``on_text_chunk`` -- throttled ``editMessageText`` streaming (typewriter),
  with any trailing ``[OPTIONS:]`` markup held back from the visible stream.
* ``on_tool_call`` -- a transient ``🔧 {tool}…`` footer.
* ``on_prompt_choice`` -- inline Approve/Deny buttons as a SEPARATE message
  (so streaming edits don't clobber them); byte-safe ``callback_data``.
* ``on_compaction`` -- a lightweight "compacting…" note.
* ``on_done`` -- the final edit, splitting long output at the capability's
  char cap and attaching the ``[OPTIONS:]`` inline keyboard to the last chunk.

``TelegramApprovalDecider`` is the interactive ladder's awaiter: ``__call__``
registers a Future keyed by ``session:request_id`` and awaits a button press,
denying by default on timeout; the callback handler resolves it via
``resolve_global``.

Dependency direction is ``telegram -> messaging`` (allowed).
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from kiro_crew.messaging.renderer import Renderer
from kiro_crew.messaging.transport import TransportCapabilities

if TYPE_CHECKING:
    from kiro_crew.telegram.client import TelegramClient

logger = logging.getLogger(__name__)

# Telegram has no native token streaming: "streaming" meant editing one message
# on every chunk, and each edit is a full HTTP round-trip + a whole-bubble
# re-render, which reads as a stutter (WeCom streams frames over a persistent
# WebSocket, so it stays smooth). Instead we do "block streaming": hold a live
# "typing…" indicator while the answer forms, then post the finished answer as
# one clean block. This kills the edit-jank entirely and only touches this
# renderer -- the shared messaging event stream (and Slack/WeCom) is untouched.
#
# Telegram's "typing" chat action lasts ~5s, so refresh it just under that
# for the duration of a turn (shown while we accumulate before posting).
_TYPING_REFRESH_S = 4.0

# Min seconds between live streaming edits to one message. Telegram rate-limits
# edits (~this cadence), so we coalesce chunks and edit the message in place at
# most this often. The final formatted edit always lands regardless of throttle.
_EDIT_THROTTLE_S = 1.0

# Interactive approval wait; deny-by-default when it elapses with no press.
_APPROVAL_TIMEOUT_S = 300.0

# Trailing "[OPTIONS: a | b | c]" -- extracted for inline-keyboard rendering.
_OPTIONS_RE = re.compile(r"\[OPTIONS:\s*(.*?)\]\s*\Z", re.DOTALL)


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


# kiro-cli emits an inline "[STEERING steer-<id>: …]" ack marker when it folds a
# mid-turn steer at a boundary. The dashboard parses it into a chip; Telegram has
# no parser, so strip it — the user's own steer message already shows the
# instruction (and gets a steer-ack reaction), so the raw inline marker is just
# redundant noise in the bubble.
_STEER_MARKER_RE = re.compile(r"\[STEERING\b[^\]]*\]", re.IGNORECASE)
# Same marker, capturing the ack SUMMARY kiro-cli embeds after "steer-<id>:".
# The dashboard renders this summary as its "Steered — …" chip; we prefer it
# for the Telegram chip too (the user's own words are already on screen as
# their message — the summary is the only NEW information).
_STEER_SUMMARY_RE = re.compile(
    r"\[STEERING\s+steer-[0-9a-f]+\s*:\s*([^\]]*)\]", re.IGNORECASE
)


def _strip_steering(text: str) -> str:
    """Remove kiro-cli's inline ``[STEERING …]`` steer-ack marker from output.

    Also strips an UNCLOSED trailing ``[STEERING …`` (still streaming, no closing
    ``]`` yet): otherwise the marker's long rephrase streams into the live draft
    and then vanishes when ``on_done`` finally strips the completed marker — a
    jarring show-then-vanish. Stripping the partial marker keeps the draft in
    sync with the final message.
    """
    cleaned = _STEER_MARKER_RE.sub("", text)  # complete markers anywhere
    cleaned = re.sub(r"\[STEERING\b[^\]]*$", "", cleaned)  # unclosed, still streaming
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)  # collapse gaps left behind
    return cleaned.strip()


def _neutralize_md(raw: str) -> str:
    """Collapse whitespace, cap length, and strip Markdown control chars from a
    steer's text so the chip renders literally (inside a blockquote) and can't
    perturb surrounding formatting."""
    t = " ".join((raw or "").split())[:120]
    return re.sub(r"[*_`\[\]()]", "", t)


def _strip_hr(text: str) -> str:
    """Drop Markdown horizontal rules (``---`` / ``***`` / ``___`` on their own
    line) — they render as literal dashes on Telegram and just add noise.
    Fenced code blocks are stashed first so a standalone ``---`` INSIDE a code
    block (e.g. a YAML document separator) is never touched. An unclosed fence
    mid-stream isn't stashed, but live frames are throttled previews — the
    final seal sees the closed fence and preserves its content."""
    stash: list[str] = []

    def _keep(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00H{len(stash) - 1}\x00"

    text = _FENCE_RE.sub(lambda m: _keep(m.group(0)), text)
    # Max 3 leading spaces (markdown HR rule) — a 4-space-indented "---" is
    # indented CODE (e.g. a YAML separator) and must survive.
    out = re.sub(r"(?m)^[ ]{0,3}([-*_])\1{2,}[ \t]*$", "", text)
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = re.sub(r"\x00H(\d+)\x00", lambda m: stash[int(m.group(1))], out)
    return out.strip()


def build_inline_keyboard(options: list[str]) -> dict | None:
    """Build an InlineKeyboardMarkup from ``[OPTIONS:]`` labels.

    ``callback_data`` is the index only (``opt:<i>``) -- Telegram caps it at
    64 BYTES, so a multi-byte (CJK/emoji) label there could overflow and make
    the whole send fail. The label is recovered from the button text at
    callback time. Two buttons per row (mobile friendly).
    """
    if not options:
        return None
    buttons: list[list[dict]] = []
    row: list[dict] = []
    for i, opt in enumerate(options):
        row.append({"text": opt[:64], "callback_data": f"opt:{i}"})
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return {"inline_keyboard": buttons}


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


def _split_markdown(text: str, limit: int) -> list[str]:
    """Split markdown into <=``limit`` chunks, keeping fenced code blocks balanced.

    ``_split_text`` can cut inside a ``` fence (a long code block has internal
    newlines it splits on). An unbalanced fence in a chunk means the per-chunk
    HTML pass never matches it -- the literal ``` shows and, worse, the code body
    is sent unescaped and 400s the HTML request. Rebalance by closing a dangling
    fence at a chunk's end and reopening it at the next chunk's start, so every
    chunk is self-contained markdown.
    """
    chunks = _split_text(text, limit)
    if len(chunks) <= 1:
        return chunks
    out: list[str] = []
    carry_open = False
    for ch in chunks:
        if carry_open:
            ch = "```\n" + ch  # reopen the fence carried from the previous chunk
        if ch.count("```") % 2 == 1:
            ch = ch.rstrip() + "\n```"  # close the fence left dangling here
            carry_open = True
        else:
            carry_open = False
        out.append(ch)
    return out


# Telegram renders a small HTML subset (<b>/<i>/<code>/<pre>/<a>) far more
# reliably than MarkdownV2 (which needs every '.', '-', '!', '(' escaped). The
# agent emits generic Markdown, so we translate it to Telegram HTML for the
# final message. Code spans are stashed first so their contents are never
# treated as markup, then the remaining text is HTML-escaped before any tags
# are introduced -- so raw '<', '>' and '&' in the answer can't break the parse.
_FENCE_RE = re.compile(r"```[^\n]*\n?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$", re.MULTILINE)
_BOLD_STAR_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_BOLD_USCORE_RE = re.compile(r"__(.+?)__", re.DOTALL)
_ITALIC_STAR_RE = re.compile(r"(?<!\w)\*(?!\s)([^*\n]+?)(?<!\s)\*(?!\w)")
_ITALIC_USCORE_RE = re.compile(r"(?<!\w)_(?!\s)([^_\n]+?)(?<!\s)_(?!\w)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+", re.MULTILINE)


def _md_to_telegram_html(text: str) -> str:
    """Translate the agent's Markdown into Telegram's supported HTML subset."""
    stash: list[str] = []

    def _keep(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    text = _FENCE_RE.sub(
        lambda m: _keep(f"<pre>{html.escape(m.group(1).rstrip(chr(10)))}</pre>"), text
    )
    text = _INLINE_CODE_RE.sub(
        lambda m: _keep(f"<code>{html.escape(m.group(1))}</code>"), text
    )
    text = html.escape(text)
    text = _HEADING_RE.sub(lambda m: f"<b>{m.group(1).strip()}</b>", text)
    text = _BOLD_STAR_RE.sub(lambda m: f"<b>{m.group(1)}</b>", text)
    text = _BOLD_USCORE_RE.sub(lambda m: f"<b>{m.group(1)}</b>", text)
    text = _ITALIC_STAR_RE.sub(lambda m: f"<i>{m.group(1)}</i>", text)
    text = _ITALIC_USCORE_RE.sub(lambda m: f"<i>{m.group(1)}</i>", text)
    text = _LINK_RE.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)
    text = _BULLET_RE.sub(lambda m: f"{m.group(1)}\u2022 ", text)
    # Group consecutive "> " lines (escaped to "&gt; ") into a native Telegram
    # <blockquote> — the ▎ quote bar. Runs after inline formatting so bold/italic
    # inside a quote still work; before un-stashing so code/pre placeholders ride
    # through untouched.
    text = re.sub(
        r"(?m)^&gt;[ \t]?.*(?:\n&gt;[ \t]?.*)*",
        lambda m: "<blockquote>"
        + re.sub(r"(?m)^&gt;[ \t]?", "", m.group(0)).rstrip("\n")
        + "</blockquote>",
        text,
    )
    text = re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)
    return text


def _strip_md(text: str) -> str:
    """Flatten Markdown to clean plaintext for the streaming typewriter frames
    (and as the safe fallback if an HTML final edit is ever rejected) -- avoids
    showing raw ``**``/``##``/``[x](url)`` noise while the answer is forming."""
    text = _FENCE_RE.sub(lambda m: m.group(1), text)
    text = _INLINE_CODE_RE.sub(lambda m: m.group(1), text)
    text = _HEADING_RE.sub(lambda m: m.group(1).strip(), text)
    text = _BOLD_STAR_RE.sub(lambda m: m.group(1), text)
    text = _BOLD_USCORE_RE.sub(lambda m: m.group(1), text)
    text = _LINK_RE.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text)
    text = _BULLET_RE.sub(lambda m: f"{m.group(1)}\u2022 ", text)
    return text


class TelegramApprovalDecider:
    """Awaits an inline-button approval for a tool-permission request.

    Process-global Future registry keyed by ``session_key:request_id`` so
    concurrent turns (and users) never resolve each other's prompts. Denies by
    default when the wait elapses.
    """

    _REGISTRY: dict[str, "asyncio.Future[bool]"] = {}

    def __init__(self, *, session_key: str) -> None:
        self._session_key = session_key

    @staticmethod
    def key(session_key: str, request_id: str | int) -> str:
        return f"{session_key}:{request_id}"

    async def __call__(self, event: Any) -> bool:
        k = self.key(self._session_key, getattr(event, "request_id", ""))
        fut: "asyncio.Future[bool]" = asyncio.get_running_loop().create_future()
        TelegramApprovalDecider._REGISTRY[k] = fut
        try:
            return bool(await asyncio.wait_for(fut, _APPROVAL_TIMEOUT_S))
        except asyncio.TimeoutError:
            return False  # deny-by-default on timeout
        finally:
            TelegramApprovalDecider._REGISTRY.pop(k, None)

    @classmethod
    def resolve_global(cls, key: str, approved: bool) -> bool:
        """Resolve a pending approval by key. Returns True iff one was waiting."""
        fut = cls._REGISTRY.get(key)
        if fut is not None and not fut.done():
            fut.set_result(bool(approved))
            return True
        return False


class TelegramRenderer(Renderer):
    """Streams a turn to Telegram via ``editMessageText`` + inline keyboards."""

    channel_type = "telegram"

    def __init__(
        self,
        client: "TelegramClient",
        chat_id: int,
        capabilities: TransportCapabilities,
        *,
        session_key: str = "",
        message_thread_id: int | None = None,
    ) -> None:
        super().__init__(capabilities)
        self._client = client
        self._chat_id = chat_id
        # Forum-topic id: when set, every outbound send/typing is threaded into
        # that Topic. None for a 1:1 DM or the supergroup General topic. Edits
        # never carry it (message_id already locates the message in its topic).
        self._thread_id = message_thread_id
        self._session_key = session_key
        self._buf: list[str] = []
        self._last_tool = ""
        # Transient tool-activity footer ("🔧 {tool}…") shown ONLY on live
        # streaming frames — never stored in _buf, so seals/finals stay clean.
        # Set by on_tool_call, cleared when text resumes (on_text_chunk).
        self._tool = ""
        self._finalized = False
        self._closed = False
        self._typing_task: "asyncio.Task[None] | None" = None
        # Live edit-streaming (send one real message, edit it in place as text
        # arrives — no draft, which fails for bots / ghosts). On a steer boundary
        # we STOP editing the current message and open a fresh one for the
        # steered continuation ("rotate"). _stream_mid = the message being edited
        # (None -> next render sends a new one). _shown = last text pushed (skip
        # no-op edits). _last_edit throttles edits. _buf = the CURRENT segment's
        # text; a rotation seals _buf into its message and starts _buf fresh.
        self._stream_mid: int | None = None
        self._shown = ""
        self._last_edit = 0.0
        self._seal_count = 0  # rotations so far == index into _steer_texts for chips
        # Chip pending from the last rotation, NOT yet in _buf. It materializes
        # (prepends to the segment) only when real post-steer text arrives — so
        # an end-of-stream marker (no continuation text) never posts a chip-only
        # ack bubble: the answer already covered the steer and the user's
        # message carries the reaction receipt.
        self._pending_chip = ""
        # User's own mid-turn steer texts (in order), recorded by the dispatcher
        # via note_steer. Each rotation seeds the new segment with that steer's
        # chip; a no-rotation turn shows them as one summary chip at on_done.
        self._steer_texts: list[str] = []

    # -- lifecycle ----------------------------------------------------------
    async def on_turn_start(self) -> None:
        # Typing indicator only — no draft preview. Idempotent (dispatch + driver
        # both call this).
        if self._typing_task is not None or self._closed:
            return
        self._typing_task = asyncio.create_task(self._typing_loop())

    async def _typing_loop(self) -> None:
        """Keep the 'typing…' chat action alive (it expires after ~5s) for the
        duration of the turn. Cancelled by ``_stop_typing``."""
        try:
            while not self._closed:
                try:
                    await self._client.send_typing(
                        self._chat_id, message_thread_id=self._thread_id
                    )
                except Exception:
                    logger.debug("Telegram: typing refresh failed", exc_info=True)
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
        # 1) Rotate to a fresh message at each COMPLETE [STEERING …] marker
        #    (kiro-cli's in-stream steer injection point). Text before the marker
        #    seals into the current message; the steered continuation opens a new
        #    message headed by the steer chip. Splitting at the marker's real
        #    position — not the racy STEER_CONSUMED offset — avoids the
        #    "used.BANANA" leak.
        await self._rotate_at_markers()
        # 1b) Materialize the pending chip once real post-steer text exists.
        self._materialize_chip()
        # 2) Rotate when a segment would exceed one Telegram message.
        await self._rotate_on_length()
        # 3) Live-stream the current segment: edit the message in place (throttled).
        await self._stream_live()

    def _materialize_chip(self) -> None:
        """Prepend the pending steer chip to the segment — but only when the
        segment carries real text. Keeps an end-of-stream marker from ever
        posting a chip-only ack bubble."""
        if self._pending_chip and self._segment_text().strip():
            body = "".join(self._buf).lstrip("\n")
            self._buf = [f"{self._pending_chip}\n\n{body}"]
            self._pending_chip = ""

    async def _rotate_at_markers(self) -> None:
        while True:
            # A chip pending from a PREVIOUS marker materializes now if this
            # segment carries real text — so a single chunk containing two
            # markers can't overwrite the first steer's chip (its continuation
            # seals WITH the chip below). A chip with no continuation text stays
            # pending and is deliberately dropped on overwrite (no-tail design).
            self._materialize_chip()
            raw = "".join(self._buf)
            m = _STEER_MARKER_RE.search(raw)  # first COMPLETE marker only
            if m is None:
                return
            self._buf = [raw[: m.start()]]
            # Length-rotate the pre-marker segment BEFORE sealing: a chunk can
            # deliver oversized text and the marker together, and sealing an
            # over-limit segment would silently truncate it at Telegram's cap.
            await self._rotate_on_length()
            sealed = bool(self._segment_text().strip())
            await self._seal_current()  # freeze the pre-steer message as-is
            # Chip: prefer the SUMMARY embedded in the marker itself (what the
            # dashboard shows as its "Steered — …" chip) — the user's own words
            # are already on screen as their message, so the summary is the only
            # new information. Fall back to the recorded user text.
            sm = _STEER_SUMMARY_RE.match(raw, m.start())
            summary = _neutralize_md(sm.group(1)) if sm else ""
            if summary:
                chip: str | None = f"> ↪️ {summary}"
            else:
                chip = self._chip_for_seal(self._seal_count)
            self._seal_count += 1
            # Hold the chip as PENDING — it prepends only when real post-steer
            # text arrives (see on_text_chunk). An end-of-stream marker thus
            # produces no chip-only ack bubble.
            self._pending_chip = chip or ""
            self._buf = [raw[m.end():]]  # new segment: steered continuation only
            if sealed:
                self._open_new_message()
            # else: the pre-marker segment was empty (e.g. a tool-only live
            # "🔧 …" message) — nothing was sealed, so KEEP _stream_mid and let
            # the steered continuation replace the transient footer in place
            # instead of orphaning it as a permanent tool-footer bubble.

    async def _rotate_on_length(self) -> None:
        """Rotate when the segment exceeds one Telegram message. Uses
        ``_split_markdown`` so a fenced code block spanning a cut is rebalanced
        (fence closed at the seal, reopened in the next segment) instead of
        leaving literal backticks in both messages. A trailing protocol
        directive — a COMPLETE ``[OPTIONS: …]`` block or a still-streaming
        ``[STEERING …`` / ``[OPTIONS …`` fragment — is detached first and
        reattached to the surviving tail, so length splitting can never cut a
        directive in half (which would leak protocol fragments and lose the
        steer rotation / options keyboard)."""
        limit = self._limit()
        raw = "".join(self._buf)
        if len(raw) <= limit:
            return
        partial = ""
        cm = _OPTIONS_RE.search(raw)
        if cm:
            # Complete trailing [OPTIONS: …] — keep it intact on the tail so
            # finalization can extract the inline keyboard (a bare split would
            # leak "NS: A | B]" as raw text and lose the keyboard).
            raw, partial = raw[: cm.start()], raw[cm.start():]
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
        with the steer marker and horizontal-rule noise stripped."""
        return _strip_hr(_strip_steering("".join(self._buf)))

    async def _stream_live(self, *, force: bool = False) -> None:
        """Throttled in-place edit of the current segment (plaintext, so partial
        markdown never 400s). Sends the message on first render. The transient
        ``🔧 {tool}…`` footer is appended ONLY here (live frames) — seals and
        the final render read ``_segment_text`` and never carry it. ``force``
        bypasses the throttle so a tool-call event surfaces immediately."""
        now = time.monotonic()
        if not force and now - self._last_edit < _EDIT_THROTTLE_S:
            return
        # Hold back trailing [OPTIONS:] markup (complete or still-streaming
        # partial) from live frames — it is an internal directive, extracted
        # into the inline keyboard at finalization.
        seg, _ = _extract_options(self._segment_text())
        body = _strip_md(seg)
        footer = f"🔧 {self._tool}…" if self._tool else ""
        if footer:
            # Keep the footer visible even when it must displace body tail chars.
            room = self._limit() - len(footer) - 2
            text = f"{body[:room]}\n\n{footer}".strip() if room > 0 else footer
        else:
            text = body[: self._limit()]
        if not text or text == self._shown:
            return
        self._last_edit = now
        self._shown = text
        if self._stream_mid is None:
            mid = await self._client.send_message(
                self._chat_id, text, message_thread_id=self._thread_id
            )
            if mid is not None:
                self._stream_mid = mid
        else:
            await self._client.edit_message(self._chat_id, self._stream_mid, text)

    async def _seal_current(self, *, keyboard: dict | None = None) -> None:
        """Finalize the current segment: replace its live plaintext with the
        formatted HTML (and optional keyboard). Edits the streamed message in
        place, or sends one if the segment never streamed (e.g. throttled out).
        Empty segments are skipped so a bare steer doesn't post a blank bubble."""
        text = self._segment_text().strip()
        if not text:
            return
        html_text = _md_to_telegram_html(text)
        if self._stream_mid is not None:
            ok = await self._client.edit_message(
                self._chat_id, self._stream_mid, html_text,
                parse_mode="HTML", reply_markup=keyboard, retry_plain=False,
            )
            if not ok:  # malformed HTML -> clean plaintext, never raw tags
                ok = await self._client.edit_message(
                    self._chat_id, self._stream_mid, _strip_md(text),
                    reply_markup=keyboard,
                )
            if ok:
                return
            # Both edits failed — the live message is gone (e.g. the user
            # deleted it mid-turn). Fall through and SEND the final content so
            # the completed answer (and its keyboard) is never silently lost.
            self._stream_mid = None
        mid = await self._client.send_message(
            self._chat_id, html_text, parse_mode="HTML",
            reply_markup=keyboard, retry_plain=False,
            message_thread_id=self._thread_id,
        )
        if mid is None:
            await self._client.send_message(
                self._chat_id, _strip_md(text), reply_markup=keyboard,
                message_thread_id=self._thread_id,
            )

    async def on_thinking(self, text: str) -> None:
        # Telegram does not surface reasoning inline (parity with prior behavior).
        return None

    async def on_tool_call(
        self, tool_call_id: str, title: str, tool_kind: str = "", tool_purpose: str = ""
    ) -> None:
        # Surface mid-turn tool activity as a transient "🔧 {tool}…" footer on
        # the live bubble (force=True so it shows immediately, not throttled).
        # We deliberately do NOT seal a message here: models interleave tool
        # calls mid-sentence, so sealing at a tool boundary chops a sentence
        # into broken bare messages on a channel with no tool cards (proven on
        # Telegram). The footer lives only on live frames — on_text_chunk clears
        # it and seals/finals never carry it.
        self._last_tool = title or tool_kind or "tool"
        self._tool = self._last_tool
        await self._stream_live(force=True)

    async def on_prompt_choice(
        self, options: list[dict[str, Any]], request_id: str | int
    ) -> None:
        # Approve/Deny as a SEPARATE message so ongoing streaming edits to the
        # answer bubble don't clobber the buttons. callback_data stays well
        # under Telegram's 64-byte cap (a:<request_id>:<1|0>); the callback
        # handler resolves the decider Future by reconstructing the key.
        rid = str(request_id)
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve", "callback_data": f"a:{rid}:1"},
                    {"text": "🚫 Deny", "callback_data": f"a:{rid}:0"},
                ]
            ]
        }
        tool = self._last_tool or "this tool"
        await self._client.send_message(
            self._chat_id, f"🔐 Approve `{tool}`?", reply_markup=keyboard,
            message_thread_id=self._thread_id,
        )

    async def on_compaction(self, context_usage_pct: float) -> None:
        try:
            await self._client.send_message(
                self._chat_id, "🗜️ Compacting context…",
                message_thread_id=self._thread_id,
            )
        except Exception:
            logger.debug("Telegram: compaction notice send failed", exc_info=True)

    def note_steer(self, text: str) -> None:
        """Record the user's own mid-turn steer text (their typed words, NOT the
        redacted backend echo). Called by the dispatcher when a steer is actually
        injected; rendered as an inline "↪️ steered: …" chip in on_done. Capped to
        avoid unbounded growth on a pathological steer burst."""
        t = (text or "").strip()
        if t and len(self._steer_texts) < 50:
            self._steer_texts.append(t)

    async def on_done(self, stop_reason: str = "") -> None:
        if self._finalized:
            return
        self._finalized = True
        self._stop_typing()
        ok = stop_reason != "error"
        # Flush any trailing rotation, then finalize the current segment: replace
        # its live plaintext with formatted HTML + the [OPTIONS:] keyboard. Each
        # mid-turn steer already rotated its own message; this seals the last one.
        await self._rotate_at_markers()
        self._materialize_chip()  # chip lands only if real post-steer text exists
        # Extract the trailing [OPTIONS:] BEFORE length rotation: if the body
        # overflows, rotation would otherwise seal the options text into an
        # earlier message and the keyboard would never attach.
        body_raw, opts = _extract_options("".join(self._buf))
        self._buf = [body_raw]
        keyboard = build_inline_keyboard(opts) if opts else None
        # No-rotation fallback: steers were injected but kiro-cli emitted no
        # marker to rotate at — prepend one summary chip so they're still shown.
        # This happens BEFORE length rotation so the summary counts against the
        # transport limit and can never push the final segment past it.
        if self._seal_count == 0 and self._steer_texts:
            quoted = [q for q in (_neutralize_md(t) for t in self._steer_texts) if q]
            if quoted:
                body = self._segment_text().strip()
                summary = "> " + " · ".join(quoted)
                self._buf = [summary + ("\n\n" + body if body else "")]
        await self._rotate_on_length()
        if not self._segment_text().strip():
            # Nothing to post. Earlier rotated segments carried the turn -> stay
            # silent; otherwise show a placeholder. An extracted keyboard (an
            # options-only body) is user-facing content and must ALWAYS reach
            # the user — attach it to the placeholder instead of dropping it.
            if self._seal_count > 0 and keyboard is None:
                return
            placeholder = "…" if ok else "⚠️ Error — please try again"
            if self._stream_mid is not None:
                await self._client.edit_message(
                    self._chat_id, self._stream_mid, placeholder,
                    reply_markup=keyboard,
                )
            else:
                await self._client.send_message(
                    self._chat_id, placeholder, reply_markup=keyboard,
                    message_thread_id=self._thread_id,
                )
            return
        await self._seal_current(keyboard=keyboard)

    def _limit(self) -> int:
        # Leave headroom below the char cap for the HTML tags we add, so a
        # formatted chunk can't overflow Telegram's 4096 limit and get cut
        # mid-tag.
        cap = self.capabilities.max_message_chars or 4000
        return max(500, cap - 256)

    def _chip_for_seal(self, i: int) -> str | None:
        """The steer chip (a "> quote" blockquote of the USER's own words) that
        heads the segment opened by the i-th rotation. None when we have no
        recorded text for that rotation (chip is simply omitted)."""
        if 0 <= i < len(self._steer_texts):
            t = _neutralize_md(self._steer_texts[i])
            return f"> {t}" if t else None
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
