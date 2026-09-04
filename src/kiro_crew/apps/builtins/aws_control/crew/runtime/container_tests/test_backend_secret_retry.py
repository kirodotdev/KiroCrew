"""A secret that vanishes between the 403 and the retry must not truncate the stream.

``forward_stream`` guards the FIRST ``_read_secret`` and, until this test existed, not the
one on the 403 retry path. A backend that restarts and answers 403 while its replacement
boot secret is not yet on disk raised ``BackendSecretUnavailable`` from that second read,
and nothing upstream caught it: status 200 is committed before a body streams, so the
response ended truncated with no ``[DONE]`` and a client waiting for the sentinel waited
forever.

Same failure mode as the transport case in ``test_backend_transport_failure``, and it
survived that fix because it is a different exception type. This suite also pins the
context-manager bookkeeping the retry needs, since the first stream is closed before the
second is opened.
"""

from __future__ import annotations

import asyncio

import pytest
from container import common
from container.front import backend as be

from .test_backup_restore import make_settings


class _Stream:
    """One ``client.stream(...)`` context manager that records its own closes."""

    def __init__(self, status: int, closes: list[str], tag: str, body: bytes = b"") -> None:
        self._status = status
        self._closes = closes
        self._tag = tag
        self._body = body

    async def __aenter__(self):
        stream = self

        class _Resp:
            status_code = stream._status

            async def aread(self):
                return stream._body

            async def aiter_bytes(self):
                if stream._body:
                    yield stream._body

        return _Resp()

    async def __aexit__(self, *_):
        self._closes.append(self._tag)
        return False


class _Client:
    """403 first, then whatever the test wants, recording every close."""

    def __init__(self, closes: list[str], second_status: int = 200) -> None:
        self.closes = closes
        self._calls = 0
        self._second = second_status

    def stream(self, *a, **k):
        self._calls += 1
        if self._calls == 1:
            return _Stream(403, self.closes, "first")
        return _Stream(self._second, self.closes, "second", b"data: [DONE]\n\n")


class _NoScope:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *_):
        return False


def _drain(resp) -> str:
    async def go():
        return b"".join([c async for c in resp.body_iterator])

    out = asyncio.run(go())
    return out.decode("utf-8") if isinstance(out, bytes) else str(out)


def test_a_secret_lost_on_the_retry_still_ends_the_stream(tmp_path, monkeypatch):
    calls = {"n": 0}

    def flaky(_settings):
        calls["n"] += 1
        if calls["n"] == 1:
            return "first-secret"
        raise common.BackendSecretUnavailable("rotated away")

    monkeypatch.setattr(be, "_read_secret", flaky)
    closes: list[str] = []
    text = _drain(
        be.forward_stream(_Client(closes), make_settings(tmp_path), {"messages": []}, _NoScope())
    )

    # The sentinel is the property that matters: without it a client hangs.
    assert "[DONE]" in text, f"stream ended without the sentinel: {text[:160]!r}"
    assert '"error"' in text, text[:200]
    assert calls["n"] == 2, "the retry path did not run, so this proves nothing"


def test_the_first_stream_is_closed_exactly_once(tmp_path, monkeypatch):
    """The retry closes the first stream, so the finally must not close it again."""
    calls = {"n": 0}

    def flaky(_settings):
        calls["n"] += 1
        if calls["n"] == 1:
            return "first-secret"
        raise common.BackendSecretUnavailable("rotated away")

    monkeypatch.setattr(be, "_read_secret", flaky)
    closes: list[str] = []
    _drain(
        be.forward_stream(_Client(closes), make_settings(tmp_path), {"messages": []}, _NoScope())
    )
    assert closes == ["first"], f"expected one close of the first stream, got {closes}"


def test_a_successful_retry_closes_only_the_second_stream(tmp_path, monkeypatch):
    monkeypatch.setattr(be, "_read_secret", lambda s: "secret")
    closes: list[str] = []
    text = _drain(
        be.forward_stream(_Client(closes), make_settings(tmp_path), {"messages": []}, _NoScope())
    )
    assert "[DONE]" in text
    # First closed by the retry path, second by the finally. Each exactly once.
    assert closes == ["first", "second"], closes


@pytest.mark.parametrize("second_status", [500, 503])
def test_a_retry_that_still_fails_reports_an_error_frame(tmp_path, monkeypatch, second_status):
    monkeypatch.setattr(be, "_read_secret", lambda s: "secret")
    closes: list[str] = []
    text = _drain(
        be.forward_stream(
            _Client(closes, second_status), make_settings(tmp_path), {"messages": []}, _NoScope()
        )
    )
    assert "[DONE]" in text
    assert '"error"' in text
