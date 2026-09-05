"""Customer stream projection for the backend's OpenAI-compatible turn SSE.

VERIFIED against a real Kiro Crew backend (2026-09-02, isolated home on :8801):
``POST /v1/chat/completions`` with ``stream=true`` emits an **OpenAI** event
stream, NOT the ACP ``sessionUpdate`` vocabulary:

  * ``data: {chat.completion.chunk}`` frames carrying ``choices[].delta.content``
    — assistant text only. The backend already applies credential/exfil-URL
    redaction to this content (``openai_compat._redact``), and its collector
    forwards ONLY ``assistant``/``chunk`` roles, so tool params, agent reasoning
    and usage never appear on this endpoint.
  * ``: keepalive`` comment frames (every ~30s of quiet).
  * a terminal ``data: [DONE]``.
  * on failure, ``data: {"error": {...}}`` then ``data: [DONE]``.
  * NO SSE ``event:`` names anywhere.

The ACP event kinds the design's streaming contract enumerates — and the
undocumented ``tool_result`` leak it warns about — belong to the OWNER control
stream (``GET /sessions/{cid}/stream``), a surface the front process does NOT
serve. An event-NAME allowlist (the earlier build) drops every frame here and
hands the customer an empty turn.

So the projection fails closed at the FRAME-CONTENT level. A frame is relayed
only if it is: a keepalive comment, the ``[DONE]`` sentinel, a well-formed
``chat.completion.chunk``, or an OpenAI ``error`` object. Anything else — a
non-chunk JSON object, non-JSON data, or a frame carrying an SSE ``event:`` name
(which this endpoint never emits) — is dropped. If a future backend change ever
interleaves a named ACP event onto this endpoint, it fails closed rather than
leaking.
"""

from __future__ import annotations

import json
from typing import AsyncIterator

_FRAME_SEP = b"\n\n"


def _frame_lines(frame_text: str) -> list[str]:
    return frame_text.split("\n")


def _is_keepalive(lines: list[str]) -> bool:
    nonblank = [line for line in lines if line != ""]
    return len(nonblank) > 0 and all(line.startswith(":") for line in nonblank)


def _data_payload(lines: list[str]) -> str | None:
    """Concatenated SSE ``data:`` field of a frame, or None if it has none."""
    parts = [line[len("data:") :].lstrip() for line in lines if line.startswith("data:")]
    if not parts:
        return None
    return "\n".join(parts).strip()


def _frame_is_customer_safe(frame_text: str) -> bool:
    lines = _frame_lines(frame_text)
    if _is_keepalive(lines):
        return True
    # This endpoint never names events; a named frame is an anomaly -> drop.
    if any(line.startswith("event:") for line in lines):
        return False
    payload = _data_payload(lines)
    if payload is None:
        return False
    if payload == "[DONE]":
        return True
    try:
        obj = json.loads(payload)
    except (ValueError, TypeError):
        return False  # non-JSON data -> fail closed
    if not isinstance(obj, dict):
        return False
    return obj.get("object") == "chat.completion.chunk" or "error" in obj


def project_frame(frame_bytes: bytes) -> bytes | None:
    """Return the frame verbatim (with separator) if it may reach the customer,
    else None. Fails closed."""
    text = frame_bytes.decode("utf-8", "replace")
    if not text.strip():
        return None
    if _frame_is_customer_safe(text):
        return frame_bytes + _FRAME_SEP
    return None


async def project_sse(chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Reframe a raw backend SSE byte stream, yielding only customer-safe frames.

    Frames can straddle chunk boundaries, so bytes are buffered and split on the
    blank-line separator. CRLF is normalised to LF.
    """
    buffer = b""
    async for chunk in chunks:
        if not chunk:
            continue
        buffer += chunk.replace(b"\r\n", b"\n")
        while _FRAME_SEP in buffer:
            frame, buffer = buffer.split(_FRAME_SEP, 1)
            projected = project_frame(frame)
            if projected is not None:
                yield projected
    if buffer.strip():
        projected = project_frame(buffer)
        if projected is not None:
            yield projected
