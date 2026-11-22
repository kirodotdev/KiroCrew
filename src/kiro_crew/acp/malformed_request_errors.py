"""Refusals of a request the model service could not parse, read from one ACP error frame.

kiro-cli's "Improperly formed request" and the claude adapter's "The request body
is not valid JSON" are each reproduced exactly by re-sending the same
conversation, so ``transport_errors`` reads them as terminal and tags them
structural.

Split out of ``transport_errors.py`` to keep that module under the file-line
cap; ``transport_errors`` re-imports these names, so
``kiro_crew.acp.transport_errors`` and ``kiro_crew.acp.client`` stay the import
paths every caller already uses.
"""

from __future__ import annotations

import re

# kiro-cli's structural rejection of a payload the backend could not parse:
# "Improperly formed request". This string has NO source-side handling and
# passes through the unknown-shape branch verbatim, so the user gets
# the raw provider text with no repair affordance. This is a DETERMINISTIC
# rejection (the payload was rejected for its shape, not a momentary backend
# fault), so retrying the identical payload can only reproduce the same
# rejection: it is TERMINAL (non-retryable). Matched against the provider `data`
# field only (like model-unavailable / generate-failed), so a stray echo of the
# phrase in the JSON-RPC `message` cannot flip an unrelated error into this
# branch. Drives BOTH _format_acp_error (repair guidance) and
# _is_transient_raw_error (terminal verdict) so wording and retry-eligibility
# never drift.
_RE_MALFORMED_REQUEST = re.compile(r"[Ii]mproperly formed request", re.IGNORECASE)

# The claude adapter's report that the model service could not parse the request
# Claude Code built: ``Internal error: API Error: 400 The request body is not
# valid JSON: invalid escape at line 1 column 829692``. The unparsable part is in
# the conversation history Claude Code re-sends on every turn, so the identical
# body is refused on every retry, on every model, and by ``/compact`` too (which
# sends that same history to be summarized). Like the adapter's "Input is too
# long", the sentence rides in the JSON-RPC ``message`` (``data`` carries only
# ``{"errorKind": "unknown"}``), so it is matched there, anchored at the start so
# an echo further into some other error cannot match.
_RE_ADAPTER_INVALID_JSON_BODY = re.compile(
    r"^\s*(?:Internal error:\s*)?(?:API Error:\s*\d{3}\s+)?"
    r"The request body is not valid JSON\b",
    re.IGNORECASE,
)

#: What the user is told. Only a fresh session drops the unparsable history, so
#: no retry and no ``/compact`` is offered; the wording names no surface control,
#: like the formatter's other branches.
ADAPTER_INVALID_JSON_BODY_TEXT = (
    "The model service could not read this chat's request (The request body is "
    "not valid JSON). The same history is sent again on every attempt, so "
    "retrying, compacting or switching models will not help. Start a fresh session."
)


def is_adapter_invalid_json_body(message: str) -> bool:
    """Whether the JSON-RPC ``message`` opens with the adapter's invalid-JSON refusal."""
    return bool(_RE_ADAPTER_INVALID_JSON_BODY.search(message))
