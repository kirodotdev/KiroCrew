"""A slot-detail render or encode failure answers the retryable 503, with a traceback.

Every read stage of ``GET /api/chat/slots/{slot}`` fails closed to
``history_corpus_unreadable()``, and so do the render and its UTF-8 encode. Unguarded,
a raise there escapes as aiohttp's bare text/plain 500 and the transcript comes up
blank. These tests pin the 503 for that stage and the WARNING that names the slot and
carries the traceback.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.state import _ChatSlot

_LOGGER = "kiro_crew.dashboard.chat_handlers"
_MESSAGE = "slot-detail render/encode failed for chat-1"


@pytest.fixture()
def state(tmp_path: Any) -> Any:
    st = _make_state(tmp_path)
    st.push_slots_update = lambda: None  # type: ignore[method-assign]
    slot = _ChatSlot(key="chat-1")
    slot.messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
    st._slots["chat-1"] = slot
    return st


def _render_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == _LOGGER and r.getMessage() == _MESSAGE]


async def _get(state: Any) -> tuple[int, Any]:
    """Status plus the parsed body, or the raw text when the body is not JSON."""
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.get("/api/chat/slots/chat-1")
        text = await resp.text()
        try:
            return resp.status, json.loads(text)
        except ValueError:
            return resp.status, text


@pytest.mark.asyncio
async def test_a_render_failure_answers_503_and_logs_the_traceback(
    state: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _boom(*_a: Any, **_k: Any) -> list[dict]:
        raise RuntimeError("render broke")

    monkeypatch.setattr(chat_handlers, "_prepare_messages", _boom)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        status, body = await _get(state)

    assert status == 503
    assert body["code"] == "history_corpus_unreadable"
    [record] = _render_warnings(caplog)
    assert record.levelno == logging.WARNING
    assert record.exc_info is not None and isinstance(record.exc_info[1], RuntimeError)


@pytest.mark.asyncio
async def test_an_encode_failure_answers_503_and_logs_the_traceback(
    state: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A lone surrogate in the rendered string makes ``str.encode`` raise."""
    real_to_thread = asyncio.to_thread

    async def _surrogate_render(fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        out = await real_to_thread(fn, *args, **kwargs)
        # Only the slot-detail render gets the unencodable tail.
        return out + "\udc80" if getattr(fn, "__name__", "") == "_render" else out

    monkeypatch.setattr(asyncio, "to_thread", _surrogate_render)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        status, body = await _get(state)

    assert status == 503
    assert body["code"] == "history_corpus_unreadable"
    [record] = _render_warnings(caplog)
    assert record.exc_info is not None and isinstance(record.exc_info[1], UnicodeEncodeError)


@pytest.mark.asyncio
async def test_a_normal_render_still_answers_200_without_a_warning(
    state: Any, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        status, body = await _get(state)

    assert status == 200
    assert [m["content"] for m in body["messages"]] == ["hi", "yo"]
    assert _render_warnings(caplog) == []
