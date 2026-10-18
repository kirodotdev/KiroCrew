"""JSON-RPC envelopes, the MCP tool-result frame, and dashboard API request bodies.

What crosses a transport boundary is checked here: a JSON-RPC 2.0 request or
response envelope, the ``tools/call`` result every tool returns
(:func:`build_tool_response`), and a parsed dashboard request body.

Callers import these names from :mod:`kiro_crew.validation`, which serves them; this
module never imports that facade.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kiro_crew.validation_parts.field_engine import ValidationError
from kiro_crew.validation_parts.input_bounds import MAX_MEDIUM_STRING, MAX_RESPONSE_LEN
from kiro_crew.validation_parts.text_sanitizer import sanitize_response, sanitize_string

# ── JSON-RPC Envelope Validation ──


#: JSON-RPC 2.0 reserved error codes for a request that cannot be served: a
#: frame that does not parse, a malformed envelope, params a method cannot
#: take, and a server-side fault. Here, beside the envelope validator, so a
#: stdio MCP server can answer with them without importing the ACP layer.
JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603


class JsonRpcEnvelopeError(ValidationError):
    """A JSON-RPC request envelope that cannot be served, with what answers it.

    ``req_id`` is the request's own id (``None`` for a notification, which is
    never answered); ``invalid_params`` tells a ``params`` that is not an
    object (JSON-RPC ``-32602``) from a malformed envelope (``-32600``).
    """

    def __init__(
        self, field: str, message: str, *, req_id: Any, method: Any, invalid_params: bool
    ) -> None:
        super().__init__(field, message)
        self.req_id = req_id
        self.method = method
        self.invalid_params = invalid_params


# Named by the path callers import it from: a traceback renders a raised class as
# ``module.qualname``, so the owner's name must not reach it.
JsonRpcEnvelopeError.__module__ = "kiro_crew.validation"


def validate_jsonrpc_request(req: dict[str, Any]) -> tuple[str, Any, dict[str, Any]]:
    """Validate a JSON-RPC 2.0 request envelope.

    Returns (method, id, params). Raises ValidationError on invalid structure:
    :class:`JsonRpcEnvelopeError` for an object envelope, carrying the id the
    refusal is owed to. Absent or ``null`` params read as ``{}``; params of any
    other non-object type are refused rather than read as ``{}``, so a request
    that names no usable arguments is answered as such instead of served as
    one that sent none.
    """
    if not isinstance(req, dict):
        raise ValidationError("request", "must be a JSON object")
    req_id = req.get("id")
    method = req.get("method")
    if req.get("jsonrpc") not in ("2.0", None):
        raise JsonRpcEnvelopeError(
            "jsonrpc", "must be '2.0'", req_id=req_id, method=method, invalid_params=False
        )

    if method is not None and not isinstance(method, str):
        raise JsonRpcEnvelopeError(
            "method", "must be a string", req_id=req_id, method=method, invalid_params=False
        )

    params = req.get("params")
    if params is None:
        params = {}
    elif not isinstance(params, dict):
        raise JsonRpcEnvelopeError(
            "params",
            f"must be an object, not {type(params).__name__}",
            req_id=req_id,
            method=method,
            invalid_params=True,
        )

    return method or "", req_id, params


# ── Response Schemas ──


@dataclass
class McpTextContent:
    """Type-safe MCP TextContent response — the only content type our tools return."""

    type: str  # always "text"
    text: str

    def to_dict(self) -> dict[str, str]:
        return {"type": self.type, "text": self.text}


def build_tool_response(
    text: str, max_len: int = MAX_RESPONSE_LEN, *, is_error: bool = False
) -> dict[str, Any]:
    """Build a validated, sanitized MCP tools/call response.

    Returns the ``result`` payload for a JSON-RPC response:
    ``{"content": [{"type": "text", "text": "..."}]}``

    This is the single exit point for all tool responses — ensures every
    response conforms to the MCP TextContent schema and is sanitized.
    ``is_error`` adds the MCP ``isError`` flag so a client can tell a refusal
    from an answer without pattern-matching the prose.
    """
    text = sanitize_response(text, max_len)
    content = McpTextContent(type="text", text=text)
    frame: dict[str, Any] = {"content": [content.to_dict()]}
    if is_error:
        frame["isError"] = True
    return frame


def validate_jsonrpc_response(resp: dict[str, Any]) -> dict[str, Any]:
    """Validate a JSON-RPC 2.0 response envelope before writing to stdout.

    Ensures: has ``jsonrpc``, ``id``, and either ``result`` or ``error``.
    """
    if not isinstance(resp, dict):
        raise ValidationError("response", "must be a JSON object")
    if "id" not in resp:
        raise ValidationError("response", "missing id")
    if "result" not in resp and "error" not in resp:
        raise ValidationError("response", "must have result or error")
    resp["jsonrpc"] = "2.0"
    return resp


# ── Dashboard API Validation Helpers ──


def validate_api_body(body: Any, max_size: int = 100_000) -> dict[str, Any]:
    """Validate a parsed JSON request body from aiohttp."""
    if not isinstance(body, dict):
        raise ValidationError("body", "must be a JSON object")
    raw = str(body)
    if len(raw) > max_size:
        raise ValidationError("body", f"exceeds max size {max_size}")
    return body


def validate_string_field(
    body: dict[str, Any],
    field_name: str,
    *,
    required: bool = False,
    max_len: int = MAX_MEDIUM_STRING,
    allowed: frozenset[str] | None = None,
) -> str:
    """Extract and validate a string field from a request body."""
    val = body.get(field_name)
    if val is None:
        if required:
            raise ValidationError(field_name, "required")
        return ""
    if not isinstance(val, str):
        raise ValidationError(field_name, "must be a string")
    val = sanitize_string(val)
    if not val and required:
        raise ValidationError(field_name, "required (empty after sanitization)")
    if max_len and len(val) > max_len:
        raise ValidationError(field_name, f"exceeds max length {max_len}")
    if allowed and val not in allowed:
        raise ValidationError(field_name, f"must be one of: {', '.join(sorted(allowed))}")
    return val
