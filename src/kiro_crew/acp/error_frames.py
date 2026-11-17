"""Readers of a harness's raw JSON-RPC error frame.

:mod:`kiro_crew.acp.transport_errors` turns a frame into an :class:`AcpError`,
and its formatter replaces the provider's text with Kiro Crew's own wording
wherever a curated branch applies. :func:`raw_error_detail` renders the frame
itself, redacted and bounded, for the log when that wording hides it.
``transport_errors`` imports it, so callers keep reaching it through it.
"""

from __future__ import annotations

import json

from kiro_crew.security import redact_credentials, redact_exfiltration_urls

#: Longest :func:`raw_error_detail` returns, in characters. Enough for a
#: provider's exception name, message and request id; short enough that a frame
#: echoing a large request body adds a bounded line to the log.
RAW_ERROR_DETAIL_MAX_CHARS = 2000


#: How deep :func:`raw_error_detail` walks a structured ``data`` before it
#: renders the rest as text. Frames come from parsed JSON a few levels deep;
#: the bound keeps a pathological or self-referencing value finite.
_RAW_ERROR_DETAIL_MAX_DEPTH = 8

#: What a container past :data:`_RAW_ERROR_DETAIL_MAX_DEPTH` becomes. Its
#: ``str`` would escape the newlines in the strings inside it before any scrub,
#: and an escaped newline defeats the truncated-PEM pattern.
_RAW_ERROR_DETAIL_TOO_DEEP = "[nested too deep]"


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
    are walked down to :data:`_RAW_ERROR_DETAIL_MAX_DEPTH`; a container past it
    becomes :data:`_RAW_ERROR_DETAIL_TOO_DEEP`, never its unscrubbed ``str``.
    Any other value becomes its scrubbed ``str``.
    """
    if isinstance(value, str):
        return _scrub(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= _RAW_ERROR_DETAIL_MAX_DEPTH and isinstance(value, (dict, list, tuple)):
        return _RAW_ERROR_DETAIL_TOO_DEEP
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

    :func:`~kiro_crew.acp.transport_errors._raise_acp_error` stores the result as
    ``AcpError.raw_detail`` when the message is Kiro Crew's curated wording. That
    wording replaces the provider's text, so this is the one place the
    underlying cause survives for the log; ``raw_detail`` stays empty when the
    message already carries it.
    """
    if not isinstance(error, dict):
        return ""
    frame = {
        key: _scrubbed_value(error.get(key)) for key in ("code", "message", "data") if key in error
    }
    # ensure_ascii keeps the record on one line, which
    # session_health.scan_log_for_stalls relies on to skip RAW_FRAME_LOG_LABEL.
    rendered = _scrub(json.dumps(frame, ensure_ascii=True))
    if len(rendered) > RAW_ERROR_DETAIL_MAX_CHARS:
        dropped = len(rendered) - RAW_ERROR_DETAIL_MAX_CHARS
        rendered = f"{rendered[:RAW_ERROR_DETAIL_MAX_CHARS]}... [{dropped} more chars]"
    return rendered
