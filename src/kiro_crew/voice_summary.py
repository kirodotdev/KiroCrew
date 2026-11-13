"""The spoken summary behind ``voice_reply.speak_mode = "summary"``.

In ``summary`` mode the dashboard's Auto-speak and Read aloud speak a short
conversational summary of a finished reply instead of the reply word for word;
the chat text is unchanged. :func:`validated_speak_mode` is the one validator for
the setting, shared by the config loader (unknown reads as ``full`` with a
warning) and the dashboard PUT (unknown is a 400). :func:`summarize_for_speech`
returns ``None`` whenever the summary cannot be trusted, and the caller then
speaks the full reply: a summary that fails must never leave the user in silence.

The model call is :func:`kiro_crew.llm_helpers.run_bg_oneliner`, the tool-free
background turn the transcript polish uses, with the same redaction on the way
out and again on the way back.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

SPEAK_MODE_FULL = "full"
SPEAK_MODE_SUMMARY = "summary"
#: Every value ``voice_reply.speak_mode`` may hold. ``full`` is the default and
#: today's behaviour: the reply is spoken as written.
VALID_SPEAK_MODES: frozenset[str] = frozenset({SPEAK_MODE_FULL, SPEAK_MODE_SUMMARY})
DEFAULT_SPEAK_MODE = SPEAK_MODE_FULL

#: Wall-clock bound on the summary DRIVE. Auto-speak waits for this before it
#: speaks anything in ``summary`` mode, so it is short; past it the full reply
#: is spoken, which is what the user would have heard without the setting.
SUMMARY_TIMEOUT_SECS = 20.0
#: A summary longer than this share of the stripped reply is not a summary. The
#: prompt asks for two to four sentences; a reply that long is the model
#: echoing the text back, and speaking the original is then the honest answer.
SUMMARY_MAX_RATIO = 0.6
#: Replies shorter than this are spoken as they are: a two-sentence answer has
#: nothing to summarize, and the model call would only add latency.
SUMMARY_MIN_SOURCE_CHARS = 240
#: The most reply text handed to the model. A turn past this is summarized from
#: its ``strip_markdown`` form with the middle cut out rather than refused: a
#: refusal would make the one reply that most needs a summary the one spoken in
#: full. The head says what was done; the tail carries the outcome and any
#: failure, so the tail is the larger share.
SUMMARY_MAX_INPUT_CHARS = 20000
SUMMARY_CLIP_HEAD_CHARS = 4000
_CLIP_MARK = "\n\n[... middle of the reply left out ...]\n\n"

_SUMMARY_PROMPT = (
    "You are the assistant who just wrote the reply below, and it is about to be "
    "read aloud to the user, who is away from the screen. Write what you would say "
    "out loud instead of the full reply: two to four spoken sentences, in plain "
    "words, in the first person, in the same language as the reply. Say what was "
    "done, what the user has to decide or do next, and whether anything failed. "
    "No lists, no code, no file paths, no URLs, no markdown, no headings, no "
    "quotation marks around the whole answer. Output only the sentences.\n\n"
    "Reply:\n{reply}"
)

#: Shapes that mean the model did not return spoken prose: a fence, a heading,
#: a bullet or numbered list, a table row, a markdown link or an option line.
_MARKUP_RE = re.compile(
    r"```|~~~|^\s{0,3}#{1,6}\s|^\s*(?:[-*+]|\d+[.)])\s|^\s*\|.*\|\s*$|\]\(|\[OPTIONS:",
    re.MULTILINE,
)


def validated_speak_mode(value: object) -> str | None:
    """Return *value* as a speak mode, or ``None`` to reject.

    Shared by the config loader, which reads ``None`` as :data:`DEFAULT_SPEAK_MODE`
    with one warning, and the dashboard PUT handler, which answers ``None`` with a
    400. ``in VALID_SPEAK_MODES`` on an unhashable JSON value (list/dict) would
    raise ``TypeError``, so the type is checked first.
    """
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if text not in VALID_SPEAK_MODES:
        return None
    return text


def speak_mode_from_config(value: object) -> str:
    """The speak mode a stored ``voice_reply.speak_mode`` resolves to on read.

    An absent key is ``full``. A value the validator rejects is also ``full``,
    with one warning naming it, so a hand-edited typo degrades to today's
    behaviour instead of refusing to boot.
    """
    if value is None:
        return DEFAULT_SPEAK_MODE
    mode = validated_speak_mode(value)
    if mode is None:
        logger.warning(
            "voice_reply.speak_mode %r is not one of %s; speaking the full reply",
            value,
            sorted(VALID_SPEAK_MODES),
        )
        return DEFAULT_SPEAK_MODE
    return mode


def summary_is_usable(original_spoken: str, candidate: str) -> bool:
    """Whether *candidate* may be spoken in place of *original_spoken*.

    *original_spoken* is the reply after ``strip_markdown``, the text the full
    mode would speak, so the length band compares like with like. Rejects an
    empty answer, one that is not shorter than :data:`SUMMARY_MAX_RATIO` of the
    original, and one that still carries markup.
    """
    text = candidate.strip()
    if not text:
        return False
    base = len(original_spoken.strip())
    if base and len(text) > base * SUMMARY_MAX_RATIO:
        return False
    if _MARKUP_RE.search(text):
        return False
    return True


async def summarize_for_speech(sessions: Any, text: str, *, spoken: str) -> str | None:
    """Return a short spoken summary of *text*, or ``None`` to speak *spoken* instead.

    *text* is the reply as written and *spoken* is its ``strip_markdown`` form,
    the text the full mode speaks; the length check runs against *spoken*. Never
    raises: a model error, a timeout, an empty or over-long answer and an answer
    that still carries markup all return ``None``. The reply is redacted before
    it is sent and the summary is redacted again on return, because the summary
    is new text and the inbound pass says nothing about it.
    """
    if len(spoken.strip()) < SUMMARY_MIN_SOURCE_CHARS:
        return None
    # Imported here, not at module scope: ``llm_helpers`` is large and this module
    # is imported on the gateway boot path by the voice config loader.
    from kiro_crew.llm_helpers import run_bg_oneliner
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    outbound, _ = redact_credentials(redact_exfiltration_urls(text)[0])
    if len(outbound) > SUMMARY_MAX_INPUT_CHARS:
        # The stripped form first (code and tables are already placeholders); if
        # that is still too long, keep its head and its tail and say so, because
        # the sentence that reports a failure sits at the end of a turn. The
        # length check below still runs against the whole spoken reply.
        outbound, _ = redact_credentials(redact_exfiltration_urls(spoken)[0])
        if len(outbound) > SUMMARY_MAX_INPUT_CHARS:
            tail_chars = SUMMARY_MAX_INPUT_CHARS - SUMMARY_CLIP_HEAD_CHARS - len(_CLIP_MARK)
            outbound = outbound[:SUMMARY_CLIP_HEAD_CHARS] + _CLIP_MARK + outbound[-tail_chars:]
    try:
        answer = await run_bg_oneliner(
            sessions,
            _SUMMARY_PROMPT.format(reply=outbound),
            model="auto",
            sel_source="voice_summary",
            timeout=SUMMARY_TIMEOUT_SECS,
        )
    except Exception:
        logger.debug("voice summary failed; speaking the full reply", exc_info=True)
        return None
    candidate = (answer or "").strip()
    if not summary_is_usable(spoken, candidate):
        logger.debug("voice summary discarded (%d chars); speaking the full reply", len(candidate))
        return None
    cleaned, _ = redact_credentials(redact_exfiltration_urls(candidate)[0])
    return cleaned
