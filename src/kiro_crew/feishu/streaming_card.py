"""Layer 2c -- Feishu streaming CARD session (CardKit lifecycle).

A live reply is not an edited text message. Feishu's streaming primitive is a
*card entity*: create the card, send ONE ordinary message that merely references
its ``card_id``, then repeatedly push the **cumulative** answer into one element
of that card. The server diffs successive pushes and animates the difference, so
the typewriter effect is server-side -- this module only decides how often to
push.

Lifecycle (each mutating step consumes one sequence number):

    1. POST   /open-apis/cardkit/v1/cards                      -> card_id
    2. POST   /open-apis/im/v1/messages/{message_id}/reply      msg_type=interactive
    3. PUT    /open-apis/cardkit/v1/cards/{id}/elements/{el}/content   (repeated)
    4. PATCH  /open-apis/cardkit/v1/cards/{id}/settings         streaming_mode=false
    5. PUT    /open-apis/cardkit/v1/cards/{id}                  final full replace

Step 4 MUST precede step 5. Step 5 is what repairs any intermediate frame that
was dropped, which is why a dropped frame needs no retry of its own.

Ported from two MIT-licensed OpenClaw Feishu plugins, attributed in ``NOTICE``
with their licence text reproduced in ``THIRD-PARTY-NOTICES``:

* https://github.com/larksuite/openclaw-lark -- the official Lark/Feishu plugin,
  maintained by the Lark/Feishu Open Platform team. Source of the five-step
  lifecycle, the sequence protocol, the throttle constants and the error
  taxonomy.
* https://github.com/m1heng/clawdbot-feishu -- community plugin. Source of the
  ``streaming_config`` pacing parameters, the schema-2.0 markdown card shape and
  the link/fence normalisation workarounds.

Dependency direction is ``feishu.renderer -> feishu.streaming_card ->
feishu.client``; nothing here imports the renderer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from kiro_crew.feishu.client import CardApiError

if TYPE_CHECKING:
    from kiro_crew.feishu.client import LarkClient

logger = logging.getLogger(__name__)

# -- Tunables (values taken from the reference implementations) --------------

#: Minimum gap between two content pushes. Both plugins independently settled on
#: 100 ms, which is the strongest signal available that this is the right value.
CARDKIT_THROTTLE_S = 0.10

#: A gap longer than this means the model went quiet (cold start, long tool
#: call). The official plugin then defers the next flush briefly so the first
#: visible frame carries real text instead of one or two characters.
LONG_GAP_THRESHOLD_S = 2.0
BATCH_AFTER_GAP_S = 0.30

#: The deferral above applies only to a fragment shorter than this. Above it the
#: text is worth showing immediately, because nothing schedules a retry.
_ANTI_STUTTER_MAX_CHARS = 24

#: Server-side typewriter pacing. Set explicitly rather than inheriting Feishu's
#: defaults so the behaviour does not drift when the platform changes them.
STREAM_PRINT_FREQUENCY_MS = 50
STREAM_PRINT_STEP = 2

#: The one element every content push targets.
STREAMING_ELEMENT_ID = "streaming_content"

#: A card renders at most this many native markdown tables; beyond it Feishu
#: rejects the whole card (230099 / sub-code 11310). Excess tables are demoted
#: to fenced code so the answer still arrives.
FEISHU_CARD_TABLE_LIMIT = 3

# -- Feishu error codes we treat specially ----------------------------------

#: Rate limited. Policy: DROP THIS FRAME. No retry, no backoff, stay streaming.
#: Safe because every push carries the cumulative answer, so the next scheduled
#: flush supersedes the lost one.
ERR_RATE_LIMITED = 230020
#: Card constraint violated -- in practice the table-count ceiling.
ERR_CARD_CONSTRAINT = 230099
#: The anchor message was recalled / deleted by the user. Nothing can be
#: delivered to it, including a plain-text fallback.
ERR_MSG_RECALLED = 230011
ERR_MSG_DELETED = 231003

_GONE_CODES = frozenset({ERR_MSG_RECALLED, ERR_MSG_DELETED})

# -- Markdown normalisation --------------------------------------------------

_FENCE_RE = re.compile(r"```[\s\S]*?```", re.MULTILINE)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_TABLE_RE = re.compile(r"^\|.+\|[ \t]*\r?\n\|[ \t:\-|]+\|[ \t]*\r?$", re.MULTILINE)
_H1_RE = re.compile(r"^# ", re.MULTILINE)
_H2_6_RE = re.compile(r"^#{2,6} ", re.MULTILINE)
_ANY_H1_3_RE = re.compile(r"^#{1,3} ", re.MULTILINE)
_BARE_URL_RE = re.compile(r"(?<![(\[<`])\bhttps?://[^\s<>\[\]`]+")
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]*)\)")
_MANY_NEWLINES_RE = re.compile(r"\n{3,}")


def _protect(text: str) -> tuple[str, list[str]]:
    """Replace fenced blocks and inline code with placeholders.

    Every rewrite below is a *rendering* workaround; applying one inside a code
    block would corrupt content the user asked to see verbatim.
    """
    stash: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        stash.append(match.group(0))
        return f"\x00KC{len(stash) - 1}\x00"

    return _INLINE_CODE_RE.sub(_stash, _FENCE_RE.sub(_stash, text)), stash


def _restore(text: str, stash: list[str]) -> str:
    for i, original in enumerate(stash):
        text = text.replace(f"\x00KC{i}\x00", original)
    return text


def normalize_card_links(text: str) -> str:
    """Work around two Feishu card renderer bugs (from the community plugin).

    * A fence whose opening ``\u0060\u0060\u0060`` is indented is not recognised, so the
      block renders as literal text -- dedent the fence markers.
    * A bare URL gets re-tokenized and is visually split or truncated around
      ``_`` and long query strings -- make it an explicit ``[url](url)`` link
      and percent-encode the characters that break tokenisation.
    """
    try:
        # Dedent fence markers BEFORE protecting, otherwise an indented fence is
        # not recognised as a fence here either. Bound to a NEW name so the
        # ``except`` below returns the caller's original text rather than this
        # half-transformed version, which is what the docstring promises.
        dedented = re.sub(r"^[ \t]+(```)", r"\1", text, flags=re.MULTILINE)
        body, stash = _protect(dedented)

        def _linkify(match: re.Match[str]) -> str:
            url = match.group(0)
            trailing = ""
            # Trailing sentence punctuation is not part of the URL.
            while url and url[-1] in ".,;:!?":
                trailing = url[-1] + trailing
                url = url[:-1]
            # Balance parens conservatively. The pattern ADMITS parentheses so a
            # URL that legitimately contains them -- the Wikipedia
            # ``..._(programming_language)`` shape is the common one -- is not cut
            # at the first ``(``; this loop then gives back only the closing
            # parens that were never opened, which is prose punctuation such as
            # "(see https://example.com)".
            while url.endswith(")") and url.count("(") < url.count(")"):
                trailing = ")" + trailing
                url = url[:-1]
            if not url:
                return match.group(0)
            safe = url.replace("_", "%5F").replace("(", "%28").replace(")", "%29")
            return f"[{url}]({safe}){trailing}"

        body = _BARE_URL_RE.sub(_linkify, body)
        return _restore(body, stash)
    except Exception:  # pragma: no cover - a rendering nicety must never break a reply
        logger.debug("Feishu: link normalisation failed; sending text as-is", exc_info=True)
        return text


def optimize_card_markdown(text: str) -> str:
    """Adapt markdown to what a Feishu card actually renders.

    From the official plugin's ``optimizeMarkdownStyle``:

    * ``#`` / ``##`` render enormous inside a card, so headings are clamped.
    * Blank lines produce no vertical space, so an explicit ``<br>`` is inserted
      around tables and code blocks.
    * An image must reference a Feishu ``img_*`` key; an http(s) or local target
      makes the whole card fail with 200570, so those are stripped.
    """
    try:
        body, stash = _protect(text)

        if _ANY_H1_3_RE.search(body):
            body = _H2_6_RE.sub("##### ", body)
            body = _H1_RE.sub("#### ", body)

        # Drop images Feishu cannot resolve rather than losing the whole card.
        def _image(match: re.Match[str]) -> str:
            target = (match.group(1) or "").strip()
            return match.group(0) if target.startswith("img_") else ""

        body = _MD_IMAGE_RE.sub(_image, body)
        body = _MANY_NEWLINES_RE.sub("\n\n", body)
        body = _restore(body, stash)

        # <br> around block constructs, after restore so fences are real again.
        #
        # Only the OUTER edges of a block get a spacer. A ``` line TOGGLES in/out
        # of a fence, so a regex over every ``` would also insert one just after
        # the opening fence and just before the closing one -- literal <br> lines
        # in the middle of the verbatim content the reader asked to see, which is
        # the very corruption ``_protect`` exists to prevent. An UNTERMINATED
        # fence (the normal case for a mid-stream frame) correctly gets no
        # trailing spacer, because the block has not ended yet.
        lines = body.split("\n")
        spaced: list[str] = []
        in_fence = False
        for idx, line in enumerate(lines):
            if not line.lstrip().startswith("```"):
                spaced.append(line)
                continue
            if not in_fence:
                if spaced and spaced[-1].strip():
                    spaced.append("<br>")
                spaced.append(line)
                in_fence = True
            else:
                spaced.append(line)
                nxt = lines[idx + 1] if idx + 1 < len(lines) else ""
                if nxt.strip():
                    spaced.append("<br>")
                in_fence = False
        body = "\n".join(spaced)
        return body
    except Exception:  # pragma: no cover
        logger.debug("Feishu: markdown optimisation failed; sending text as-is", exc_info=True)
        return text


def fence_excess_tables(text: str, limit: int = FEISHU_CARD_TABLE_LIMIT) -> str:
    """Demote tables beyond *limit* to fenced code blocks.

    A card carrying more native tables than Feishu allows is rejected outright,
    so the answer would be lost entirely. A fenced table is ugly but readable.
    """
    try:
        starts = [m.start() for m in _TABLE_RE.finditer(text)]
        if len(starts) <= limit:
            return text
        lines = text.splitlines(keepends=True)
        # Recompute table spans line-wise so a whole table (header + delimiter +
        # body rows) is wrapped, not just the two matched lines.
        out: list[str] = []
        seen = 0
        i = 0
        while i < len(lines):
            is_head = lines[i].lstrip().startswith("|") and i + 1 < len(lines)
            is_delim = is_head and re.match(r"^\s*\|[\s:\-|]+\|\s*$", lines[i + 1] or "")
            if is_head and is_delim:
                j = i
                while j < len(lines) and lines[j].lstrip().startswith("|"):
                    j += 1
                seen += 1
                block = "".join(lines[i:j])
                if seen > limit:
                    if not block.endswith("\n"):
                        block += "\n"
                    out.append("```\n" + block + "```\n")
                else:
                    out.append(block)
                i = j
                continue
            out.append(lines[i])
            i += 1
        return "".join(out)
    except Exception:  # pragma: no cover
        logger.debug("Feishu: table demotion failed; sending text as-is", exc_info=True)
        return text


def prepare_card_text(text: str, *, demote_tables: bool = False) -> str:
    """The full text pipeline for a card body."""
    out = normalize_card_links(text)
    if demote_tables:
        out = fence_excess_tables(out)
    return optimize_card_markdown(out)


# -- Card payloads -----------------------------------------------------------


def build_streaming_card(summary: str = "[正在生成…]") -> dict[str, Any]:
    """The card created at step 1: one empty markdown element, streaming on."""
    return {
        "schema": "2.0",
        "config": {
            "streaming_mode": True,
            "streaming_config": {
                "print_frequency_ms": {"default": STREAM_PRINT_FREQUENCY_MS},
                "print_step": {"default": STREAM_PRINT_STEP},
            },
            "summary": {"content": summary[:50]},
            "wide_screen_mode": True,
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": "",
                    "text_align": "left",
                    "text_size": "normal_v2",
                    "element_id": STREAMING_ELEMENT_ID,
                }
            ]
        },
    }


def build_final_card(text: str) -> dict[str, Any]:
    """The card sent at step 5, replacing the streaming one wholesale."""
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "body": {"elements": [{"tag": "markdown", "content": text}]},
    }


# -- The session -------------------------------------------------------------


class StreamingCardSession:
    """One live card for one turn.

    Failure policy, in one place because it is the whole design:

    * Anything wrong during ``start`` -> return False and let the caller send an
      ordinary text reply. Nothing has been shown to the user yet.
    * ``ERR_RATE_LIMITED`` on a push -> drop the frame. The next push carries the
      newer cumulative text, so the loss self-heals.
    * ``ERR_CARD_CONSTRAINT`` -> demote excess tables and keep going; a second
      occurrence retires the session.
    * The anchor message was recalled/deleted -> retire, and tell the caller NOT
      to fall back, because a text reply to that anchor fails too.
    * Any other error -> retire and let the caller fall back to text.

    ``delivered`` is the property the caller acts on: True means the user has
    the complete answer on screen and a text fallback would duplicate it.
    """

    def __init__(self, client: "LarkClient", message_id: str) -> None:
        self._client = client
        self._message_id = message_id
        self._card_id = ""
        self._seq = 1
        self._lock = asyncio.Lock()
        self._pending = ""
        self._shown = ""
        self._last_flush = 0.0
        self._batch_until = 0.0
        self._demote_tables = False
        self._retired = False
        self._anchor_gone = False
        self._delivered = False

    # -- State ---------------------------------------------------------------

    @property
    def live(self) -> bool:
        """True while the card can still be pushed to."""
        return bool(self._card_id) and not self._retired

    @property
    def anchor_gone(self) -> bool:
        """True when the inbound message is gone -- do NOT fall back to text."""
        return self._anchor_gone

    @property
    def delivered(self) -> bool:
        """True once the user has the full answer in the card."""
        return self._delivered

    def _next_seq(self) -> int:
        # Deliberately NOT rolled back on failure: gaps are tolerated by Feishu,
        # only monotonicity matters, and rolling back could reuse a number the
        # server already consumed.
        self._seq += 1
        return self._seq

    def _classify(self, exc: BaseException, where: str) -> None:
        """Apply the failure policy to *exc*."""
        code = getattr(exc, "code", None)
        if code == ERR_RATE_LIMITED:
            logger.debug("Feishu card: rate limited at %s; dropping frame", where)
            return
        if code in _GONE_CODES:
            logger.info("Feishu card: anchor message is gone (code=%s); retiring", code)
            self._anchor_gone = True
            self._retired = True
            return
        if code == ERR_CARD_CONSTRAINT and not self._demote_tables:
            logger.info("Feishu card: card constraint hit; demoting excess tables")
            self._demote_tables = True
            return
        logger.warning("Feishu card: %s failed (%s); falling back to text", where, exc)
        self._retired = True

    # -- Lifecycle -----------------------------------------------------------

    async def start(self) -> bool:
        """Steps 1-2. False means nothing was shown; caller should send text."""
        try:
            card_json = json.dumps(build_streaming_card(), ensure_ascii=False)
            data = await self._client.card_api(
                "POST",
                "/open-apis/cardkit/v1/cards",
                {"type": "card_json", "data": card_json},
            )
            card_id = str(data.get("card_id") or "")
            if not card_id:
                logger.warning("Feishu card: create returned no card_id; falling back to text")
                return False
            await self._client.send_card_reply(self._message_id, card_id)
        except CardApiError as exc:
            if getattr(exc, "code", None) in _GONE_CODES:
                self._anchor_gone = True
            logger.warning("Feishu card: could not open a streaming card (%s)", exc)
            return False
        except Exception as exc:
            logger.warning("Feishu card: could not open a streaming card (%s)", exc)
            return False
        self._card_id = card_id
        self._last_flush = time.monotonic()
        return True

    async def push(self, text: str, *, force: bool = False) -> None:
        """Step 3, throttled. *text* is the cumulative answer so far.

        *force* bypasses the throttle. Used for a frame that may be followed by
        a long silence (a tool call), where waiting for the next chunk would
        leave the user staring at stale text.
        """
        self._pending = text
        await self._flush(force=force)

    async def _flush(self, *, force: bool = False) -> None:
        if not self.live:
            return
        async with self._lock:
            if not self.live:
                return
            now = time.monotonic()
            gap = now - self._last_flush
            if not force:
                # Anti-stutter: after a long quiet period the first frame would
                # carry only the handful of characters that just arrived, which
                # reads as a stutter. Defer briefly so it carries real text.
                #
                # Only for a TINY fragment: nothing schedules a retry, so
                # deferring a substantial buffer would hide it until the next
                # event, and if the model then goes quiet the user sees nothing.
                # A fragment this short is worth that risk; a paragraph is not.
                tiny = len(self._pending) - len(self._shown) < _ANTI_STUTTER_MAX_CHARS
                if tiny and gap > LONG_GAP_THRESHOLD_S and self._batch_until <= self._last_flush:
                    self._batch_until = now + BATCH_AFTER_GAP_S
                    return
                if now < self._batch_until:
                    return
                if gap < CARDKIT_THROTTLE_S:
                    return
            body = prepare_card_text(self._pending, demote_tables=self._demote_tables)
            if not body or body == self._shown:
                return
            # Stamp BEFORE the call so a slow request cannot let a second flush
            # through on the old timestamp.
            self._last_flush = now
            try:
                await self._client.card_api(
                    "PUT",
                    f"/open-apis/cardkit/v1/cards/{self._card_id}"
                    f"/elements/{STREAMING_ELEMENT_ID}/content",
                    {"content": body, "sequence": self._next_seq()},
                )
            except Exception as exc:
                self._classify(exc, "content push")
                return
            self._shown = body

    async def finish(self, text: str) -> bool:
        """Steps 3-5. Returns ``delivered``.

        The final content push happens FIRST, so the user has the whole answer
        even if closing streaming mode or the full replace then fails.
        """
        if not self.live:
            return self._delivered
        self._pending = text
        await self._flush(force=True)
        if self._shown:
            self._delivered = True
        if not self.live:
            return self._delivered

        body = prepare_card_text(text, demote_tables=self._demote_tables)
        try:
            await self._client.card_api(
                "PATCH",
                f"/open-apis/cardkit/v1/cards/{self._card_id}/settings",
                {
                    "settings": json.dumps({"streaming_mode": False}, ensure_ascii=False),
                    "sequence": self._next_seq(),
                },
            )
        except Exception as exc:
            # Cosmetic only -- the text is already on screen.
            logger.debug("Feishu card: could not close streaming mode (%s)", exc)
        try:
            await self._client.card_api(
                "PUT",
                f"/open-apis/cardkit/v1/cards/{self._card_id}",
                {
                    "card": {
                        "type": "card_json",
                        "data": json.dumps(build_final_card(body), ensure_ascii=False),
                    },
                    "sequence": self._next_seq(),
                },
            )
        except Exception as exc:
            logger.debug("Feishu card: final replace failed (%s)", exc)
        return self._delivered
