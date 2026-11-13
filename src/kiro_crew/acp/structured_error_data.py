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

#: The ``data`` of the error reply the runtime hands a session in place of a chat
#: prompt reply it dropped as oversize. ``transport_errors`` shows that error's
#: ``message`` as one plain sentence (the byte counts stay in the reader's log),
#: still through its credential redaction, since a backend can send it too.
OVERSIZE_REPLY_DATA = "kirocrew:oversize-reply-dropped"


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


def _error_kind_from_frame(error: object) -> str:
    """Return the structured ``errorKind`` a raw ACP frame's ``data`` carries.

    claude-agent-acp names some provider answers with a machine-readable kind
    alongside the prose (``{"code": -32603, "message": "Internal error",
    "data": {"errorKind": "cloud_credential_error"}}`` -- the same field the
    ``{'errorKind': 'unknown'}`` wrapper note at ``transport_errors._RE_5XX_HINT``
    describes). Only a ``data`` that IS a mapping is read: the formatter pastes a
    non-mapping ``data`` verbatim into the user-visible message, so matching
    the kind out of that string would be the prose path structured readers
    exist to avoid. Returns "" when the frame names no kind.
    """
    if not isinstance(error, dict):
        return ""
    data_field = error.get("data")
    if isinstance(data_field, dict):
        kind = data_field.get("errorKind")
        return kind if isinstance(kind, str) else ""
    return ""
