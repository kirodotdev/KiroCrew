"""Customer stream projection for the backend's OpenAI-compatible turn SSE.

VERIFIED against a real Kiro Crew backend (isolated home on :8801):
``POST /v1/chat/completions`` with ``stream=true`` emits an **OpenAI** event
stream, NOT the ACP ``sessionUpdate`` vocabulary:

  * ``data: {chat.completion.chunk}`` frames carrying ``choices[].delta.content``
    -- assistant text only. The backend already applies credential/exfil-URL
    redaction to this content (``openai_compat._redact``), and its collector
    forwards ONLY ``assistant``/``chunk`` roles, so tool params, agent reasoning
    and usage never appear on this endpoint.
  * ``: keepalive`` comment frames (every ~30s of quiet).
  * a terminal ``data: [DONE]``.
  * on failure, ``data: {"error": {...}}`` then ``data: [DONE]``.
  * NO SSE ``event:`` names anywhere.

The ACP event kinds the design's streaming contract enumerates -- and the
undocumented ``tool_result`` leak it warns about -- belong to the OWNER control
stream (``GET /sessions/{cid}/stream``), a surface the front process does NOT
serve. An event-NAME allowlist (the earlier build) drops every frame here and
hands the customer an empty turn.

So the projection fails closed at the FRAME-CONTENT level. A frame is relayed
only if it is: a keepalive comment, the ``[DONE]`` sentinel, a well-formed
``chat.completion.chunk``, or an OpenAI ``error`` object. Anything else -- a
non-chunk JSON object, non-JSON data, or a frame carrying an SSE ``event:`` name
(which this endpoint never emits) -- is dropped. If a future backend change ever
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


def _frame_verdict(frame_text: str) -> tuple[bool, dict | None]:
    """Whether the frame may reach the customer, and its parsed ``data:`` object.

    The object rides back with the verdict because deciding safety already requires
    parsing it, and the caller needs the same parse to put the crew's own name back
    into it. A second parse would be a second chance to disagree with the decision.
    ``None`` for a frame with no JSON object: a keepalive, the sentinel, or a drop.
    """
    lines = _frame_lines(frame_text)
    if _is_keepalive(lines):
        return True, None
    # This endpoint never names events; a named frame is an anomaly -> drop.
    if any(line.startswith("event:") for line in lines):
        return False, None
    payload = _data_payload(lines)
    if payload is None:
        return False, None
    if payload == "[DONE]":
        return True, None
    try:
        obj = json.loads(payload)
    except (ValueError, TypeError):
        return False, None  # non-JSON data -> fail closed
    if not isinstance(obj, dict):
        return False, None
    safe = obj.get("object") == "chat.completion.chunk" or "error" in obj
    return safe, (obj if safe else None)


def _frame_is_customer_safe(frame_text: str) -> bool:
    return _frame_verdict(frame_text)[0]


def _only_data_lines(frame_text: str) -> bool:
    """True when every non-blank line of the frame is an SSE ``data:`` line.

    A frame this function rejects is relayed byte-for-byte instead of rebuilt.
    Rebuilding emits a single ``data:`` line, so it would silently drop an ``id:``,
    ``retry:`` or comment line sitting beside the payload. This endpoint emits none
    of those, and that is exactly the assumption worth checking rather than trusting.
    """
    return all(line.startswith("data:") for line in _frame_lines(frame_text) if line != "")


def project_frame(frame_bytes: bytes, *, crew_name: str) -> bytes | None:
    """Return the frame (with separator) if it may reach the customer, else None.

    Fails closed on the safety question. Where the frame carries a ``model``, the
    value the customer sees is *crew_name* -- the crew they addressed -- not the
    agent id the backend was asked for. The two differ because a crew's spec is
    installed and dispatched inside the crew namespace, and the backend echoes the
    id it was given; a customer who read that id back and sent it as ``model`` would
    be told this deployment does not serve it. So the namespace is translated on the
    way out by the same process that applied it on the way in.

    A frame whose model already reads as the crew's own name, or which carries no
    string ``model``, is relayed byte-for-byte: rebuilding it would re-serialise a
    payload for no gain.
    """
    text = frame_bytes.decode("utf-8", "replace")
    if not text.strip():
        return None
    safe, obj = _frame_verdict(text)
    if not safe:
        return None
    if (
        obj is not None
        and isinstance(obj.get("model"), str)
        and obj["model"] != crew_name
        and _only_data_lines(text)
    ):
        rebuilt = {**obj, "model": crew_name}
        return b"data: " + json.dumps(rebuilt, ensure_ascii=False).encode("utf-8") + _FRAME_SEP
    return frame_bytes + _FRAME_SEP


async def project_sse(chunks: AsyncIterator[bytes], *, crew_name: str) -> AsyncIterator[bytes]:
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
            projected = project_frame(frame, crew_name=crew_name)
            if projected is not None:
                yield projected
    if buffer.strip():
        projected = project_frame(buffer, crew_name=crew_name)
        if projected is not None:
            yield projected
