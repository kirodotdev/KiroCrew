"""The ``traceparent`` round trip through the real routes, served by an aiohttp test server.

``POST /v1/chat/completions`` and ``POST /api/hooks/agent`` are mounted at their real
paths with their real handlers; only the turn itself is a recorder, which reads the
turn id the route bound for it from inside the task the route started. That pins
both halves: the caller's header reaches the turn, and the response header names
the span that turn records (``H(turn, turn_id, 1)``).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import webhooks
from kiro_crew.dashboard import openai_compat
from kiro_crew.dashboard.handlers import hooks as hooks_handlers
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService
from kiro_crew.testing.clock import ManualClock
from kiro_crew.testing.wait import async_wait_until
from kiro_crew.tracing import ids, inbound, provider, runs, turns

NS = "00112233445566778899aabbccddeeff"
TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT = "00f067aa0ba902b7"
VALID = f"00-{TRACE}-{PARENT}-01"
MALFORMED = f"00-{TRACE.upper()}-{PARENT}-01"
TS = 1_700_000_000


class _Ready(KiroPrerequisiteService):
    async def session_ready(self) -> bool:
        return True

    async def verified_ready(self, *, max_age_secs: float) -> bool:
        del max_age_secs
        return True


@pytest.fixture(autouse=True)
def _floor(_floor_monkeypatch):
    return _floor_monkeypatch


@pytest.fixture
def tracing():
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider.install_for_testing(provider.new_state(SimpleSpanProcessor(exporter), NS, b"k" * 32))
    yield
    turns.reset_for_testing()
    runs.reset_for_testing()
    provider.reset_for_testing()


def _expected(turn_id: str) -> str:
    return f"00-{TRACE}-{ids.span_id(NS, ids.KIND_TURN, turn_id, 1)}-01"


# --- POST /v1/chat/completions -------------------------------------------------


def _slot():
    slot = MagicMock()
    slot.key = "trace-slot"
    slot.linked_session_key = ""
    slot.channel_origin = False
    slot.agent = ""
    slot.task = None
    slot.running = False
    slot.turn_running = False
    slot.is_closing = False
    slot.event = asyncio.Event()
    slot._pending = []

    def drain():
        out = slot._pending[:]
        slot._pending.clear()
        slot.event.clear()
        return out

    slot.drain = drain
    return slot


def _completions_app(slot) -> web.Application:
    state = MagicMock()
    state.get_or_create_slot = MagicMock(return_value=slot)
    state._slots = {slot.key: slot}
    state._background_tasks = set()
    state.session_still_loading = AsyncMock(return_value=False)
    app = web.Application()
    app["state"] = state
    app["kiro_prerequisite_service"] = object.__new__(_Ready)
    app.router.add_post("/v1/chat/completions", openai_compat.api_completions)
    return app


async def _complete(monkeypatch, sent: dict[str, str], *, stream: bool):
    slot = _slot()
    claimed: list[str] = []

    async def fake_run_chat(_state, _slot, _prompt, **_kwargs):
        claimed.append(inbound.claim_turn_id())
        slot._pending.append({"role": "assistant", "content": "hi back"})
        slot._pending.append({"cls": "done"})
        slot.event.set()

    monkeypatch.setattr(openai_compat, "_run_chat", fake_run_chat)
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": stream}
    async with TestClient(TestServer(_completions_app(slot))) as http:
        resp = await http.post("/v1/chat/completions", json=body, headers=sent)
        text = await resp.text()
    return resp, text, claimed


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_completions_returns_the_turn_span_in_the_callers_trace(tracing, monkeypatch, stream):
    provider._accept_traceparent = True
    resp, text, claimed = await _complete(monkeypatch, {"traceparent": VALID}, stream=stream)
    assert resp.status == 200
    assert "hi back" in text
    assert claimed and claimed[0]
    assert resp.headers.get("traceparent") == _expected(claimed[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("accept, sent", [(False, VALID), (True, MALFORMED)])
async def test_completions_off_or_malformed_runs_as_before_with_no_header(
    tracing, monkeypatch, accept, sent
):
    provider._accept_traceparent = accept
    resp, text, claimed = await _complete(monkeypatch, {"traceparent": sent}, stream=False)
    assert resp.status == 200
    assert json.loads(text)["choices"][0]["message"]["content"] == "hi back"
    assert claimed == [""]
    assert "traceparent" not in resp.headers


# --- POST /api/hooks/agent -----------------------------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch):
    for var, sub in (("KIROCREW_HOME", "home"), ("KIROCREW_WORKSPACE", "workspace")):
        (tmp_path / sub).mkdir()
        monkeypatch.setenv(var, str(tmp_path / sub))
    monkeypatch.setattr(webhooks, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(hooks_handlers, "_HOOK_STORE_PATH", tmp_path / "hooks.json")
    monkeypatch.setattr(hooks_handlers, "_sel", MagicMock())
    ManualClock(start=float(TS)).install(monkeypatch, webhooks)
    ManualClock(start=float(TS) + 0.25).install(monkeypatch, hooks_handlers)
    webhooks._reset_signature_replay()
    webhooks._reset_auth_throttle()
    inherited = hooks_handlers._hook_semaphore._value
    hooks_handlers._reset_hook_inflight()
    yield tmp_path
    webhooks._reset_signature_replay()
    webhooks._reset_auth_throttle()
    hooks_handlers._reset_hook_inflight()
    while hooks_handlers._hook_semaphore._value < inherited:
        hooks_handlers._hook_semaphore.release()


async def _hook(home, monkeypatch, sent: str):
    claimed: list[str] = []

    async def run(state, session_key, message, *args, **kwargs):  # type: ignore[no-untyped-def]
        try:
            claimed.append(inbound.claim_turn_id())
        finally:
            hooks_handlers._hook_inflight_sessions.discard(session_key)
            hooks_handlers._hook_semaphore.release()

    monkeypatch.setattr(hooks_handlers, "_run_hook_agent", run)
    raw, _secret, _entry = webhooks.WebhookTokenStore(home).create("ci", require_signature=False)
    headers = {"Authorization": f"Bearer {raw}", "traceparent": sent}
    app = web.Application()
    app["state"] = SimpleNamespace(_background_tasks=set())
    app.router.add_post("/api/hooks/agent", hooks_handlers.api_hooks_agent)
    async with TestClient(TestServer(app)) as http:
        resp = await http.post(
            "/api/hooks/agent", json={"message": "build 7 failed"}, headers=headers
        )
        body = await resp.json()
        await async_wait_until(lambda: len(claimed) == 1, describe=lambda: f"claimed={claimed}")
    return resp, body, claimed


@pytest.mark.asyncio
async def test_the_webhook_returns_the_turn_span_in_the_callers_trace(tracing, home, monkeypatch):
    provider._accept_traceparent = True
    resp, body, claimed = await _hook(home, monkeypatch, VALID)
    assert resp.status == 200 and body["status"] == "accepted"
    assert claimed[0]
    assert resp.headers.get("traceparent") == _expected(claimed[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("accept, sent", [(False, VALID), (True, MALFORMED)])
async def test_the_webhook_off_or_malformed_runs_as_before_with_no_header(
    tracing, home, monkeypatch, accept, sent
):
    provider._accept_traceparent = accept
    resp, body, claimed = await _hook(home, monkeypatch, sent)
    assert resp.status == 200 and body["status"] == "accepted"
    assert claimed == [""]
    assert "traceparent" not in resp.headers
