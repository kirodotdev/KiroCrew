"""The engine's "could not load the available models" refusal, read from one ACP frame.

KAS settles a session's model before it runs a prompt. When every attempt to
fetch the model list fails for a reason other than a rejected or denied
credential, ``session/prompt`` answers::

    {"code": -32000,
     "message": "Kiro could not load the available models. Please try again.",
     "data": {"errorType": "ModelRegistryUnavailableError",
              "retryErrorType": "SERVER_ERROR"}}

The frame names no HTTP status and keeps none of the service's own answer.
Its cause can be a network blip, or a request the service refuses every time:
a sign-in that sends no profile ARN gets HTTP 400 ``ValidationException``
"profileArn is required for this request" on each model-list call, and KAS
folds that into this same frame. Read as generic 5xx prose (its "Please try
again" matches ``_RE_5XX_HINT``), it told the user the backend had a momentary
server error, which was false in the persistent case. It gets its own wording
instead. Only the wording is this module's: the retry verdict and the
classifier kind still come from the 5xx retry hint, which matches the engine's
own ``SERVER_ERROR``, and the bounded ladder caps the cost when the cause persists.

Split out of ``transport_errors.py`` to keep that module under the file-line
cap; ``transport_errors`` imports these names, so ``kiro_crew.acp.transport_errors``
and ``kiro_crew.acp.client`` stay the import paths every caller already uses.
"""

from __future__ import annotations

import json

#: The engine's stable ``data.errorType`` value for this refusal. Compared exactly,
#: so the name echoed in ``message`` or in prose cannot stamp an unrelated error.
_MODEL_REGISTRY_UNAVAILABLE = "ModelRegistryUnavailableError"

#: What the user is told. It names what failed (the model list), keeps both
#: causes open, and points at the sign-in, the one fix when the failure repeats.
MODEL_REGISTRY_UNAVAILABLE_TEXT = (
    "Kiro could not load the list of models for this account, so the turn did "
    "not start. Retry in a moment. If it keeps failing, sign out and sign in "
    "again under Settings → Agent Harness → Kiro Agent."
)


def is_model_registry_unavailable(data: object) -> bool:
    """Whether the frame's raw ``data`` (a dict, or one sent as JSON text) is this refusal."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return False
    return isinstance(data, dict) and data.get("errorType") == _MODEL_REGISTRY_UNAVAILABLE
