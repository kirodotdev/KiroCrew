"""The provider text of an ACP error frame whose ``data`` is a structured object.

Most backends put the provider's own error string in ``data``::

    {"code": -32603, "message": "Internal error", "data": "<provider text>"}

Kiro CLI 3.0.0 (and the KAS engine behind it) moves that string to ``message``
and sends a machine-readable object in ``data`` instead::

    {"code": -32000,
     "message": "The monthly usage limit has been reached ...",
     "data": {"errorType": "MonthlyLimitError", "retryErrorType": "..."}}

The readers in ``transport_errors`` match several provider patterns against the
``data`` field only, so a phrase echo in JSON-RPC boilerplate cannot flip a
verdict. For this shape the field held only the object's ``repr``, so the
provider sentence was lost from the usage-limit row and the "temporarily
unavailable" retry never matched. :func:`provider_data_text` returns the text
those readers should treat as the provider field.

Only this shape is read differently: a ``data`` mapping that names a string
``errorType``, next to a ``message`` that is not the fixed ``Internal error``
boilerplate. A string ``data``, an ``errorKind`` mapping (claude-agent-acp) and
a frame with no ``data`` read exactly as before.

Split out of ``transport_errors.py`` to keep that module under the file-line cap.
"""

from __future__ import annotations

from collections.abc import Mapping


def structured_data_prose(error: object) -> str:
    """The provider sentence of a structured-``data`` frame, or "" for any other shape."""
    if not isinstance(error, dict):
        return ""
    data = error.get("data")
    if not isinstance(data, Mapping) or not isinstance(data.get("errorType"), str):
        return ""
    message = error.get("message")
    if not isinstance(message, str):
        return ""
    prose = message.strip()
    # -32603's canonical message carries no provider text; keep the old reading.
    if prose.lower() == "internal error":
        return ""
    return prose


def provider_data_text(error: object) -> str:
    """The text the ``data``-scoped provider patterns are matched against.

    ``str(error["data"])`` as before, with the provider sentence put in front when
    the frame is the structured-``data`` shape :func:`structured_data_prose` reads.
    The ``repr`` is kept after it so the readers that key on ``errorType`` tokens
    (``MonthlyLimitError``) still see them.
    """
    data = str(error.get("data", "") or "") if isinstance(error, dict) else ""
    prose = structured_data_prose(error)
    return f"{prose} {data}" if prose else data
