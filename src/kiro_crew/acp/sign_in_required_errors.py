"""The engine's "sign in to send a prompt" refusal, read from one ACP frame.

KAS answers a ``session/prompt`` it cannot authenticate (the host holds no Kiro
sign-in, so every credential callback was refused) with::

    {"code": -32000,
     "message": "Sign in to Kiro to send a prompt.",
     "data": {"errorType": "SignInRequiredError",
              "retryErrorType": "CLIENT_ERROR"}}

Measured on engine 0.66.104. The frame carries no
401/403 and none of the expiry or "not signed in" wording earlier engines sent,
so the prose vocabulary in ``transport_errors`` does not see it. The structured
``data.errorType`` is the engine's stable name for the refusal, so it is the
one thing read here, compared exactly; the message text is never matched.

Split out of ``transport_errors.py`` to keep that module under the file-line
cap; ``transport_errors`` imports this predicate where it decides the sign-in
wording and the sign-in tags.
"""

from __future__ import annotations

import json

#: The engine's ``data.errorType`` value for this refusal. Compared exactly, so
#: the name echoed in ``message`` or in prose cannot stamp an unrelated error.
_SIGN_IN_REQUIRED = "SignInRequiredError"


def is_sign_in_required(data: object) -> bool:
    """Whether the frame's raw ``data`` (a dict, or one sent as JSON text) is this refusal."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return False
    return isinstance(data, dict) and data.get("errorType") == _SIGN_IN_REQUIRED
