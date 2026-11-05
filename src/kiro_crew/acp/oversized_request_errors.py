"""The three refusals of a request too large to send, read from one ACP error frame.

kiro-cli's own "too large to send", the claude adapter's "Input is too long" and
the model service's "Request exceeds the maximum allowed size" are each
reproduced exactly by re-sending the same conversation, so ``transport_errors``
reads them as terminal and tags them structural.

Split out of ``transport_errors.py`` to keep that module under the file-line
cap; ``transport_errors`` re-imports these names, so
``kiro_crew.acp.transport_errors`` and ``kiro_crew.acp.client`` stay the import
paths every caller already uses.
"""

from __future__ import annotations

import re

# kiro-cli's OWN refusal of a request no compaction round can make fit: "This
# message is too large to send, and it contains no text that can be shortened.
# Remove or reduce the attached content and try again." It is raised when the
# context overflowed and the pending message is IRREDUCIBLE (image blocks have no
# truncated form), and kiro-cli deliberately does not compact on the way to
# saying so, nor does it append the failed message to the native history -- so the
# conversation is byte-identical before and after the failure, and re-sending the
# same context reproduces the verdict on every attempt. That makes it a
# STRUCTURAL rejection in the same sense as _RE_MALFORMED_REQUEST (a size verdict
# rather than a shape verdict, but equally deterministic), and a self-driving
# caller that re-inlines the same attachment each cycle must stop rather than
# re-fire it. Matched against the provider `data` field only, like its two
# siblings: the ACP server hands the agent-loop error to `internal_error(text)`,
# so the sentence rides in `data` beside the -32603 boilerplate `message`, and a
# phrase echo carried only by `message` must not stamp an unrelated error. The
# wording ends in "try again", so the terminal verdict is stated explicitly in
# _is_transient_raw_error rather than left to the unknown fall-through: a
# co-occurring retry hint or 5xx wrapper must not rescue a size verdict.
_RE_OVERSIZED_REQUEST = re.compile(r"[Tt]his message is too large to send")

# The claude adapter's spelling of a request larger than the model accepts:
# ``Internal error: API Error: 400 Input is too long.`` (or ``Prompt is too
# long``). Unlike kiro-cli it puts the sentence in the JSON-RPC ``message``, not
# in ``data`` (which carries only ``{"errorKind": "unknown"}``), so this one is
# matched against ``message`` -- anchored at its start, so it reads the adapter's
# own wording and never a phrase echoed further into some other error. The
# overflow is in the conversation history the adapter re-sends on every turn
# (accumulated images are the usual cause), so the identical payload is refused
# on every retry and on every model: a CONTEXT OVERFLOW in the same sense as
# _RE_CONTEXT_WINDOW_OVERFLOW, and tagged the same way at raise time.
_RE_ADAPTER_INPUT_TOO_LONG = re.compile(
    r"^\s*(?:Internal error:\s*)?(?:API Error:\s*\d{3}\s+)?(?:Input|Prompt) is too long\b",
    re.IGNORECASE,
)

# The model service's byte cap on one request, relayed by kiro-cli:
# ``Request exceeds the maximum allowed size (Request ID: ...): {errorType:
# GenericValidationError, retryErrorType: CLIENT_ERROR}``. Like the two
# overflows above, the oversized part is the conversation history (usually
# accumulated full-resolution screenshots) that is re-sent on every turn, so a
# retry, a Resume and a reload all send the same bytes and are refused the same
# way: a CONTEXT OVERFLOW, tagged the same way at raise time. Which JSON-RPC field
# carries the sentence is not captured, so both are read, each ANCHORED at its
# start (after kiro-cli's stream envelope or the ``Internal error:`` prefix): the
# provider's own sentence opens the field, while an echo further in (a tool's
# stderr, a user's own text) does not.
_RE_REQUEST_SIZE_EXCEEDED = re.compile(
    r"^\s*(?:Encountered an error in the response stream:\s*)?(?:Internal error:\s*)?"
    r"(?:API Error:\s*\d{3}\s+)?Request exceeds the maximum allowed size\b",
    re.IGNORECASE,
)


def _is_request_size_exceeded(data: str, message: str) -> bool:
    """Whether the ``data`` or ``message`` field opens with the size-cap refusal."""
    return bool(_RE_REQUEST_SIZE_EXCEEDED.search(data) or _RE_REQUEST_SIZE_EXCEEDED.search(message))
