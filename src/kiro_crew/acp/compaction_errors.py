"""Reading a ``failed`` compaction notification: the reason it DISPLAYS and the
verdict that decides whether to RETRY, derived from one walk of the payload so a
backend that moves a field cannot make them disagree.

Split out of ``transport_errors.py`` to keep that module under the file-line
cap; ``transport_errors`` re-imports these three names, so
``kiro_crew.acp.transport_errors`` and ``kiro_crew.acp.client`` stay the import
paths every caller already uses.
"""

from __future__ import annotations

from kiro_crew.acp._dispatch import redact_text

# Cap for the failure detail carried into the compaction notice: it is
# backend-echoed text on a chat row, not a log line.
_COMPACTION_DETAIL_MAX_CHARS = 200


# Keys that may carry a human-readable failure reason, MOST preferred first.
# The rank is what makes extraction deterministic on a payload carrying several
# of them: KAS nests a machine reason (``cause.reason`` =
# ``MODEL_TEMPORARILY_UNAVAILABLE``) beside a sentence written for the user
# (``userFacingSessionErrorMessage``), and the sentence is the one that tells a
# reader what to DO about it.
_COMPACTION_DETAIL_KEYS = (
    "userFacingSessionErrorMessage",
    "reason",
    "message",
    "error",
    "detail",
    "name",
)

# A named reason holding one of these words says nothing the notice does not
# already say. KAS's ``summarization_failed`` frame reported literally "error",
# which rendered as "Compaction failed: error" — no reason, and nothing to grep
# server-side. Rejecting them falls through to the raw shape, which at least
# carries the payload the backend actually sent.
_COMPACTION_DETAIL_PLACEHOLDERS = frozenset(
    {"error", "errored", "failed", "failure", "none", "null", "unknown", "unknown error"}
)

# Reason markers that make a failed compaction RETRYABLE: the summarization
# model call was throttled or 5xx'd, so the same conversation can succeed on a
# later attempt or on another model. Deliberately EXCLUDES the
# context/too-large family — retrying one of those replays the same overflow,
# which is the case the no-retry policy was written for.
_COMPACTION_TRANSIENT_MARKERS = (
    "temporarily unavailable",
    "throttl",
    "too many requests",
    "rate limit",
    "high volume of traffic",
    "service unavailable",
    "internalserverexception",
    "internal server error",
    "timed out",
    "timeout",
)

# Depth bound for the payload walk. The shapes we read are three levels at
# most (``status`` -> ``error`` -> ``cause``); the bound only stops a
# pathological or cyclic frame from walking forever.
_COMPACTION_WALK_MAX_DEPTH = 6


def _walk_compaction_payload(value: object, depth: int = 0):
    """Yield ``(key, leaf)`` pairs from a compaction notification payload.

    One walker serves both readers below, so the reason a notice DISPLAYS and
    the verdict that decides whether to RETRY are derived from the same view of
    the frame — a backend that moves a field cannot make them disagree.
    """
    if depth > _COMPACTION_WALK_MAX_DEPTH:
        return
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(child, (dict, list)):
                yield from _walk_compaction_payload(child, depth + 1)
            else:
                yield str(key), child
    elif isinstance(value, list):
        for child in value:
            yield from _walk_compaction_payload(child, depth + 1)


def compaction_failure_detail(params: dict) -> str:
    """Best-effort reason text from a ``failed`` compaction notification.

    kiro-cli carries no dedicated error field today: ``summary`` is
    populated on success but typically empty on failure, which collapses the
    user-facing notice to "unknown error" with nothing to report or grep.
    Prefer any named reason the payload does carry, else fall
    back to the raw shape so the notice says something concrete. Redacted
    here (not at each call site) because this reaches the dashboard.

    Nested by design: KAS reports the real reason under ``cause``, and the
    previous one-level read saw only the flat sibling — a placeholder word —
    so the notice said "error" while the payload carried the whole throttle.
    """
    best_rank = len(_COMPACTION_DETAIL_KEYS)
    detail = ""
    for key, value in _walk_compaction_payload(params):
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text or text.lower() in _COMPACTION_DETAIL_PLACEHOLDERS:
            continue
        try:
            rank = _COMPACTION_DETAIL_KEYS.index(key)
        except ValueError:
            continue
        if rank < best_rank:
            best_rank, detail = rank, text
    if not detail:
        # No named reason anywhere — the raw params ARE the only evidence.
        detail = f"no reason reported by the agent (raw: {params})"
    # redact_text is the single-source scrub (exfil URLs + credentials) every
    # other LLM-influenced surface uses, including the sibling KAS summary.
    return redact_text(detail)[:_COMPACTION_DETAIL_MAX_CHARS]


def compaction_failure_is_transient(params: dict) -> bool:
    """True when a ``failed`` compaction is worth attempting again.

    Read from the STRUCTURED payload rather than the rendered notice: the
    notice is truncated, redacted and sometimes only a raw ``repr``, so
    matching prose would both miss real throttles and fire on a digit that
    happened to land in a summary. An HTTP status is therefore compared as a
    number under its own key, never as a substring.

    The distinction is load-bearing. A compaction that failed because the
    conversation overflows the window fails again identically, which is why
    the turn is abandoned without a retry; a compaction whose summarization
    call was throttled has nothing wrong with it and succeeds on the next
    attempt. Treating the second as permanent is what dropped the user's
    message with a bare "error" on the row.
    """
    for key, value in _walk_compaction_payload(params):
        if key == "httpStatusCode":
            # ``bool`` is an ``int`` subclass, so a stray True would otherwise
            # compare as 1 and read as a status code.
            if isinstance(value, int) and not isinstance(value, bool):
                if value == 429 or 500 <= value < 600:
                    return True
            continue
        if not isinstance(value, str):
            continue
        # Only REASON-BEARING keys are scanned -- the same set the reason reader
        # ranks. The frame also carries backend-echoed, conversation-derived text
        # (conversationSummary rides in the very payload the KAS branch passes
        # whole), so matching every string leaf would let a summary that merely
        # mentions "timeout" upgrade a permanent context overflow to transient
        # and replay it. A control decision must not be reachable from content
        # the model wrote -- the same reasoning that made this read the payload
        # instead of the rendered notice.
        if key not in _COMPACTION_DETAIL_KEYS:
            continue
        # Separators folded to spaces before matching: the same fault is spelled
        # as prose in the user-facing sentence ("temporarily unavailable") and as
        # a SCREAMING_SNAKE enum in the machine reason
        # ("MODEL_TEMPORARILY_UNAVAILABLE"), and a marker list that matched only
        # one spelling would classify the same failure two different ways
        # depending on which field the backend happened to fill.
        low = value.lower().replace("_", " ").replace("-", " ")
        if any(marker in low for marker in _COMPACTION_TRANSIENT_MARKERS):
            return True
    return False
