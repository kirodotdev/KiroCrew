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
# (used only as the fallback when native draft streaming is unavailable).
_TYPING_REFRESH_S = 4.0

# How often to push an animated draft update while streaming. Drafts animate in
# place and are cheap, so a brisk cadence stays smooth; the client's 429
# backstop covers the rare long turn.
_DRAFT_THROTTLE_S = 0.5

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
# no parser, so strip it — the user's own steer message (which the steered reply
# threads under, see on_steer_consumed) already shows the instruction, so the raw
# inline marker is redundant noise in the bubble.
_STEER_MARKER_RE = re.compile(r"\[STEERING\b[^\]]*\]", re.IGNORECASE)


def _strip_steering(text: str) -> str:
    """Remove kiro-cli's inline ``[STEERING …]`` steer-ack marker from output."""
    cleaned = _STEER_MARKER_RE.sub("", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)  # collapse gaps left behind
    return cleaned.strip()


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
    ) -> None:
        super().__init__(capabilities)
        self._client = client
        self._chat_id = chat_id
        self._session_key = session_key
        self._buf: list[str] = []
        self._last_tool = ""
        self._finalized = False
        self._closed = False
        self._typing_task: "asyncio.Task[None] | None" = None
        # Native draft streaming (smooth, no editMessageText reflow). draft_id
        # must be non-zero and stable across the turn so updates animate in
        # place. _draft_ok: None=untried, True=streaming, False=fell back.
        self._draft_id = abs(id(self)) % 1_000_000_000 + 1
        self._draft_ok: bool | None = None
        self._last_draft = 0.0
        # Mid-turn steer (M1): the user's steer message id (reply target) and the
        # pre/post-steer split offset in _buf, so on_done renders the steered
        # continuation as its own message threaded under the user's message.
        self._steer_reply_to: int | None = None
        self._steer_split_at: int | None = None

    def set_steer_reply_to(self, message_id: int) -> None:
        """Dispatcher hook: record the user's steer message id so the post-steer
        continuation threads under it (M1 reply-linkage).

        First-steer-wins: on a rapid multi-steer burst the dispatcher calls this
        for every steer, but ``on_steer_consumed`` records the split only at the
        first consumed boundary. Keeping the *first* steer's id here aligns the
        reply target with that split so the cause->effect link stays consistent.
        """
        if self._steer_reply_to is None:
            self._steer_reply_to = message_id or None

    # -- lifecycle ----------------------------------------------------------
    async def on_turn_start(self) -> None:
        # Prefer native draft streaming (smooth, animated, no edit reflow). An
        # empty draft renders a "Thinking…" placeholder. If drafts are rejected
        # (e.g. Forum Topic Mode off), fall back to a live "typing…" indicator.
        # Idempotent (dispatch + driver both call this).
        if self._draft_ok is not None or self._closed:
            return
        self._draft_ok = await self._client.send_message_draft(
            self._chat_id, self._draft_id, ""
        )
        self._last_draft = time.monotonic()
        if not self._draft_ok and self._typing_task is None:
            self._typing_task = asyncio.create_task(self._typing_loop())

    async def _typing_loop(self) -> None:
        """Keep the 'typing…' chat action alive (it expires after ~5s) for the
        duration of the turn. Cancelled by ``_stop_typing``."""
        try:
            while not self._closed:
                try:
                    await self._client.send_typing(self._chat_id)
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
        # Stream the growing answer as an animated draft (plaintext, so partial
        # markdown never 400s). The finished, formatted message is posted in
        # on_done. If a draft update is rejected mid-turn, fall back to typing.
        if not self._draft_ok:
            return
        now = time.monotonic()
        if now - self._last_draft < _DRAFT_THROTTLE_S:
            return
        self._last_draft = now
        body = _strip_md(self._text())
        if not body:
            return
        ok = await self._client.send_message_draft(self._chat_id, self._draft_id, body)
        if not ok:
            self._draft_ok = False
            if self._typing_task is None and not self._closed:
                self._typing_task = asyncio.create_task(self._typing_loop())

    async def on_thinking(self, text: str) -> None:
        # Telegram does not surface reasoning inline (parity with prior behavior).
        return None

    async def on_tool_call(
        self, tool_call_id: str, title: str, tool_kind: str = "", tool_purpose: str = ""
    ) -> None:
        # Remember the tool for a possible approval prompt; the live typing
        # indicator already signals "working", so nothing is posted here.
        self._last_tool = title or tool_kind or "tool"

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
            self._chat_id, f"🔐 Approve `{tool}`?", reply_markup=keyboard
        )

    async def on_compaction(self, context_usage_pct: float) -> None:
        try:
            await self._client.send_message(self._chat_id, "🗜️ Compacting context…")
        except Exception:
            logger.debug("Telegram: compaction notice send failed", exc_info=True)

    async def on_done(self, stop_reason: str = "") -> None:
        if self._finalized:
            return
        self._finalized = True
        self._stop_typing()
        ok = stop_reason != "error"
        # Leave headroom below the char cap for the HTML tags we add, so a
        # formatted chunk can't overflow Telegram's 4096 limit and get cut
        # mid-tag.
        cap = self.capabilities.max_message_chars or 4000
        limit = max(500, cap - 256)

        # Mid-turn steer split (M1): render the pre-steer output and the steered
        # continuation as SEPARATE messages, with the continuation threaded under
        # the user's steer message. Split the RAW buffer at the boundary recorded
        # by on_steer_consumed (real messages are only posted here, in on_done).
        raw = "".join(self._buf)
        split = self._steer_split_at
        if split is not None and 0 < split < len(raw.rstrip()):
            pre = _strip_steering(_extract_options(raw[:split].strip())[0])
            post_body, opts = _extract_options(raw[split:].strip())
            post = _strip_steering(post_body)
            keyboard = build_inline_keyboard(opts) if opts else None
            if pre:
                await self._send_blocks(pre, limit, keyboard=None, reply_to=None)
            await self._send_blocks(
                post or "…", limit, keyboard=keyboard, reply_to=self._steer_reply_to
            )
            return

        body = self._text()
        full = body or ("…" if ok else "⚠️ Error — please try again")
        opts = self._options()
        keyboard = build_inline_keyboard(opts) if opts else None
        await self._send_blocks(full, limit, keyboard=keyboard, reply_to=None)

    async def _send_blocks(
        self, text: str, limit: int, *, keyboard: dict | None, reply_to: int | None
    ) -> None:
        """Post ``text`` as one or more Telegram HTML blocks. The RAW markdown is
        split into <=limit chunks with fenced code kept balanced (_split_markdown)
        then each chunk is formatted -- so a fence never straddles a chunk edge and
        leaks a literal ``` / unescaped body. Keyboard on the last chunk only;
        reply threading on the first chunk only."""
        chunks = _split_markdown(text, limit) or [text]
        last = len(chunks) - 1
        for i, chunk in enumerate(chunks):
            kb = keyboard if i == last else None
            rt = reply_to if i == 0 else None
            html_chunk = _md_to_telegram_html(chunk)
            mid = await self._client.send_message(
                self._chat_id, html_chunk, parse_mode="HTML",
                reply_markup=kb, reply_to_message_id=rt, retry_plain=False,
            )
            if mid is None:  # malformed HTML -> clean plaintext, never raw tags
                await self._client.send_message(
                    self._chat_id, _strip_md(chunk),
                    reply_markup=kb, reply_to_message_id=rt,
                )

    async def on_steer_consumed(self) -> None:
        """Record the pre/post-steer boundary in _buf so on_done renders the
        steered continuation as its own message, threaded under the user's steer.
        (Block streaming: real messages are only posted in on_done, so we mark
        the split offset here rather than sealing a live message.)"""
        if self._steer_split_at is None:  # first steer marks the split
            self._steer_split_at = len("".join(self._buf))

    async def close(self) -> None:
        """Idempotent teardown: stop the typing indicator and finalize the turn
        if it never reached on_done."""
        self._stop_typing()
        if not self._finalized:
            await self.on_done(stop_reason="error")

    # -- helpers ------------------------------------------------------------
    def _text(self) -> str:
        raw = "".join(self._buf).strip()
        body, _ = _extract_options(raw)
        return _strip_steering(body)

    def _options(self) -> list[str]:
        raw = "".join(self._buf).strip()
        _, opts = _extract_options(raw)
        return opts
