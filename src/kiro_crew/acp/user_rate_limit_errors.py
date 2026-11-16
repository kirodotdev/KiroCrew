"""Kiro's per-user request-rate limit, read from one ACP error frame.

Kiro answers HTTP 429 with the reason ``USER_REQUEST_RATE_EXCEEDED`` when one
user sends requests faster than their rate limit allows. The engine names it a
``ClientThrottleError`` and words it "You're sending requests faster than your
rate limit allows." The limit is per user, not per model, so every model spends
the same allowance: the provider throttle row's "switch to a different model"
advice cannot clear it, and following it only sends more requests.

Only the wording is this module's. The retry verdict and the throttle tag still
come from the frame's throttle tokens, exactly as for any other throttle. The
text keeps "is throttling" so the string classifiers that restored transcripts
use (``llm_helpers`` "throttl", the fleet probe's "is throttling") still match.

Split out of ``transport_errors.py`` to keep that module under the file-line cap.
"""

from __future__ import annotations

import re

#: The reason token, or the engine's own sentence (any apostrophe before it).
_RE_USER_RATE_LIMIT = re.compile(
    r"\bUSER_REQUEST_RATE_EXCEEDED\b|sending requests faster than your rate limit allows",
    re.IGNORECASE,
)

#: What the user is told: which limit, that a model switch will not help, and to wait.
USER_RATE_LIMIT_TEXT = (
    "Kiro is limiting how fast you can send requests, so it is throttling requests "
    "for now. Switching to a different model will not help: every model counts "
    "toward the same limit. Wait a minute, then retry."
)


def is_user_rate_limit(text: str) -> bool:
    """Whether *text* (the frame's ``data`` and ``message``) names the per-user rate limit."""
    return bool(_RE_USER_RATE_LIMIT.search(text))


def error_is_user_rate_limit(exc: BaseException) -> bool:
    """Whether a raised error is Kiro's per-user rate limit.

    Read by the model-fallback walk: the limit counts every model, so a swap
    cannot clear it. The raised message is usually the rewritten
    :data:`USER_RATE_LIMIT_TEXT`; an unformatted one still carries the frame's
    own token or sentence.
    """
    text = str(exc)
    return USER_RATE_LIMIT_TEXT in text or is_user_rate_limit(text)
