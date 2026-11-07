"""Readers of a harness's raw JSON-RPC error frame.

:mod:`kiro_crew.acp.transport_errors` turns a frame into an :class:`AcpError`,
and its formatter replaces the provider's text with Kiro Crew's own wording
wherever a curated branch applies. The functions here read the frame itself:
one structured field the classifiers key on, and the redacted, bounded
rendering of the whole frame kept for the log when that wording hides it.
``transport_errors`` imports both, so callers keep reaching them through it.
"""

from __future__ import annotations

import json

from kiro_crew.security import redact_credentials, redact_exfiltration_urls

#: Longest :func:`raw_error_detail` returns, in characters. Enough for a
#: provider's exception name, message and request id; short enough that a frame
#: echoing a large request body adds a bounded line to the log.
RAW_ERROR_DETAIL_MAX_CHARS = 2000


def _error_kind_from_frame(error: object) -> str:
    """Return the structured ``errorKind`` a raw ACP frame's ``data`` carries.

    claude-agent-acp names some provider answers with a machine-readable kind
    alongside the prose (``{"code": -32603, "message": "Internal error",
    "data": {"errorKind": "cloud_credential_error"}}`` -- the same field the
    ``{'errorKind': 'unknown'}`` wrapper note at ``_RE_5XX_HINT`` describes).
    Only a ``data`` that IS a mapping is read: the formatter pastes a
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


#: How deep :func:`raw_error_detail` walks a structured ``data`` before it
#: renders the rest as text. Frames come from parsed JSON a few levels deep;
#: the bound keeps a pathological or self-referencing value finite.
_RAW_ERROR_DETAIL_MAX_DEPTH = 8


def _scrub(text: str) -> str:
    """Exfiltration URLs, then credentials: the order the formatter uses."""
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


def _scrubbed_value(value: object, depth: int = 0) -> object:
    """*value* with every string in it scrubbed, before any JSON escaping.

    Escaping comes later and puts a backslash in front of every quote inside a
    string, which breaks the field-name patterns: a provider string that
    quotes ``"aws_session_token": "..."`` is redacted as written but not once
    escaped. So each string is scrubbed in its own spelling first. Containers
    are walked; anything else, and anything past
    :data:`_RAW_ERROR_DETAIL_MAX_DEPTH`, becomes its scrubbed ``str``.
    """
    if isinstance(value, str):
        return _scrub(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= _RAW_ERROR_DETAIL_MAX_DEPTH:
        return _scrub(str(value))
    if isinstance(value, dict):
        return {_scrub(str(k)): _scrubbed_value(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrubbed_value(item, depth + 1) for item in value]
    return _scrub(str(value))


def raw_error_detail(error: object) -> str:
    """The raw error frame as one redacted, bounded log-safe line.

    Renders ``code``, ``message`` and ``data`` as compact ASCII JSON, so neither
    a newline nor a Unicode line break (U+2028, U+2029, U+0085, which
    ``str.splitlines`` honours) in provider text can start a forged log line. Every string is scrubbed
    (exfiltration URLs, then credentials) in its own spelling before the
    escaping (:func:`_scrubbed_value`), and the rendered whole is scrubbed
    again, which catches a key/value pair in a structured ``data``. Only then
    is it cut to :data:`RAW_ERROR_DETAIL_MAX_CHARS`, with the dropped length
    named: a cut before redaction could sever a secret into a fragment no
    pattern matches. Returns "" for a frame that is not a mapping.
    """
    if not isinstance(error, dict):
        return ""
    frame = {
        key: _scrubbed_value(error.get(key)) for key in ("code", "message", "data") if key in error
    }
    rendered = _scrub(json.dumps(frame, ensure_ascii=True))
    if len(rendered) > RAW_ERROR_DETAIL_MAX_CHARS:
        dropped = len(rendered) - RAW_ERROR_DETAIL_MAX_CHARS
        rendered = f"{rendered[:RAW_ERROR_DETAIL_MAX_CHARS]}... [{dropped} more chars]"
    return rendered
