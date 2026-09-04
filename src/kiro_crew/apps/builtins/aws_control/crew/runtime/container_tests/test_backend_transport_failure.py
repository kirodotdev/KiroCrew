"""A backend that goes away mid-turn must produce a usable answer, not a hang.

The backend is a sibling process in the same task and ECS restarts it, so a turn in
flight when that happens is an expected event rather than a bug. Neither transport was
guarded:

* the non-streamed path let ``httpx.RequestError`` escape, which FastAPI rendered as a
  500 -- "this service is broken" when the truth is "try again";
* the streamed path had already committed status 200, so the exception ended the body
  TRUNCATED with no ``[DONE]``, and a client waiting for that sentinel waits forever.
"""

from __future__ import annotations

import json

import httpx
import pytest
from container.front import backend as be

from .test_backup_restore import make_settings


class _BoomClient:
    """An httpx-shaped client whose every call fails the way a restart does."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def post(self, *a, **k):
        raise self._exc

    def stream(self, *a, **k):
        exc = self._exc

        class _CM:
            async def __aenter__(self):
                raise exc

            async def __aexit__(self, *_):
                return False

        return _CM()


class _NoScope:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *_):
        return False


def _settings(tmp_path):
    s = make_settings(tmp_path)
    s.__class__  # keep the fixture's own construction; nothing to override here
    return s


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("connection refused"),
        httpx.ReadError("connection reset"),
        httpx.RemoteProtocolError("server disconnected"),
    ],
)
def test_a_non_streamed_turn_returns_503(tmp_path, exc, monkeypatch):
    import asyncio

    monkeypatch.setattr(be, "_read_secret", lambda s: "secret")
    resp = asyncio.run(
        be.forward_completion(_BoomClient(exc), _settings(tmp_path), {"messages": []})
    )
    assert resp.status_code == 503, f"{type(exc).__name__} did not become a 503"
    payload = json.loads(bytes(resp.body).decode("utf-8"))
    assert payload["error"]["code"] == "backend_unreachable"


def test_a_streamed_turn_ends_with_a_complete_error_frame(tmp_path, monkeypatch):
    import asyncio

    monkeypatch.setattr(be, "_read_secret", lambda s: "secret")
    resp = be.forward_stream(
        _BoomClient(httpx.ConnectError("connection refused")),
        _settings(tmp_path),
        {"messages": []},
        _NoScope(),
    )

    async def drain():
        return b"".join([chunk async for chunk in resp.body_iterator])

    body = asyncio.run(drain())
    text = body.decode("utf-8") if isinstance(body, bytes) else str(body)
    # The sentinel is the property that matters: without it a client hangs.
    assert "[DONE]" in text, f"stream ended without the sentinel: {text[:120]!r}"
    assert "backend_unreachable" in text, text[:200]


def test_the_stream_says_it_failed_rather_than_returning_nothing(tmp_path, monkeypatch):
    """An empty-but-well-formed stream would look like a crew with nothing to say."""
    import asyncio

    monkeypatch.setattr(be, "_read_secret", lambda s: "secret")
    resp = be.forward_stream(
        _BoomClient(httpx.ReadError("reset")), _settings(tmp_path), {"messages": []}, _NoScope()
    )

    async def drain():
        return b"".join([chunk async for chunk in resp.body_iterator])

    text = asyncio.run(drain()).decode("utf-8")
    assert '"error"' in text, text[:200]
