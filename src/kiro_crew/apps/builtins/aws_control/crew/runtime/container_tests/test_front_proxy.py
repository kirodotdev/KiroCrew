"""Tests for the S1 front process against a FAKE backend HTTP surface.

No AWS, no real Kiro Crew boot. The fake backend is a small FastAPI app on real
loopback that records what it received and models the three backend behaviours
the front must respect: the ``X-Internal-Secret`` grant, the per-slot 409, and an
SSE turn stream. The front app is driven in-process over ``httpx.ASGITransport``.

What these tests CANNOT prove is stated in the track report: the real backend's
exact SSE event vocabulary, the CSRF/Origin behaviour of the real gateway, and
anything about the deployed network path are not exercised here.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
import uvicorn
from container import common
from container.front.app import build_app
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


# --------------------------------------------------------------------------- #
# Fake backend
# --------------------------------------------------------------------------- #
@dataclass
class FakeState:
    secret_file: Path
    accept: str = "SECRET"
    heal_on_bad: bool = False
    turn_delay: float = 0.05
    stream_chunks: list[bytes] = field(default_factory=list)
    requests: list[dict] = field(default_factory=list)
    inflight: set[str] = field(default_factory=set)
    current: int = 0
    max_concurrent: int = 0
    saw_409: int = 0
    lock: asyncio.Lock | None = None


def build_fake_backend(fake: FakeState) -> FastAPI:
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def turn(request: Request):
        if fake.lock is None:
            fake.lock = asyncio.Lock()
        headers = {k.lower(): v for k, v in request.headers.items()}
        raw = await request.body()
        body = json.loads(raw) if raw else {}
        fake.requests.append({"headers": headers, "body": body})

        presented = headers.get(common.HEADER.lower())
        if presented != fake.accept:
            # Model a backend that has (re)booted: the fresh secret is now the
            # value on disk, so a re-read by the caller will pick it up.
            if fake.heal_on_bad:
                fake.secret_file.write_text(fake.accept, encoding="utf-8")
            return JSONResponse({"detail": "forbidden"}, status_code=403)

        # A missing id means the backend mints one; model that so id-less turns
        # do not collide on a shared inflight key.
        sid = body.get("id") or f"__mint_{len(fake.requests)}"

        async with fake.lock:
            if sid in fake.inflight:
                fake.saw_409 += 1
                # Real backend shape (verified 2026-09-02): OpenAI-style error.
                return JSONResponse(
                    {"error": {"message": f"slot '{sid}' is busy", "type": "slot_busy"}},
                    status_code=409,
                )
            fake.inflight.add(sid)
            fake.current += 1
            fake.max_concurrent = max(fake.max_concurrent, fake.current)

        if body.get("stream"):

            async def gen():
                try:
                    await asyncio.sleep(fake.turn_delay)
                    for chunk in fake.stream_chunks:
                        yield chunk
                finally:
                    # Created by the route above before it ever reaches here.
                    assert fake.lock is not None
                    async with fake.lock:
                        fake.inflight.discard(sid)
                        fake.current -= 1

            return StreamingResponse(gen(), media_type="text/event-stream")

        try:
            await asyncio.sleep(fake.turn_delay)
            return JSONResponse(
                {
                    "id": sid,
                    "choices": [{"message": {"role": "assistant", "content": "hi"}}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                }
            )
        finally:
            async with fake.lock:
                fake.inflight.discard(sid)
                fake.current -= 1

    return app


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def backend(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("front-backend")
    run_dir = tmp / "run"
    run_dir.mkdir()
    port = _free_port()
    secret_file = run_dir / f"gateway-{port}.secret"
    secret_file.write_text("SECRET", encoding="utf-8")

    fake = FakeState(secret_file=secret_file)
    server = uvicorn.Server(
        uvicorn.Config(build_fake_backend(fake), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.02)
    else:
        raise RuntimeError("fake backend did not start")

    yield {"fake": fake, "port": port, "run_dir": run_dir, "secret_file": secret_file}

    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def env(backend):
    """Reset the fake to a clean, authenticating state before each test."""
    fake: FakeState = backend["fake"]
    fake.requests.clear()
    fake.inflight.clear()
    fake.current = 0
    fake.max_concurrent = 0
    fake.saw_409 = 0
    fake.heal_on_bad = False
    fake.turn_delay = 0.05
    fake.accept = "SECRET"
    fake.stream_chunks = []
    backend["secret_file"].write_text("SECRET", encoding="utf-8")
    return backend


def make_settings(backend, *, route_prefix: str = "/c/crew", control_secret: str | None = None):
    run_dir = backend["run_dir"]
    data_home = run_dir.parent / "data"
    return common.Settings(
        backend_port=backend["port"],
        backend_run_dir=run_dir,
        front_port=8080,
        route_prefix=route_prefix,
        control_secret=control_secret,
        data_home=data_home,
        config_dir=data_home / "config",
        crew_name="crew",
        backup_bucket=None,
        backup_prefix="",
        backup_interval_secs=30,
    )


@asynccontextmanager
async def run_front(settings):
    app = build_app(settings)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://front")
    try:
        yield client, app
    finally:
        await client.aclose()
        backend_client = getattr(app.state, "backend_client", None)
        if backend_client is not None:
            await backend_client.aclose()


TURN = "/c/crew/v1/chat/completions"


# --------------------------------------------------------------------------- #
# Forwarding + auth
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_customer_turn_forwards_and_authenticates(env):
    async with run_front(make_settings(env)) as (client, _app):
        resp = await client.post(TURN, json={"model": "crew", "messages": [], "id": "slot-1"})
    assert resp.status_code == 200
    assert resp.json()["id"] == "slot-1"
    assert len(env["fake"].requests) == 1
    sent = env["fake"].requests[0]
    assert sent["headers"][common.HEADER.lower()] == "SECRET"
    # Only the contract fields were forwarded.
    assert set(sent["body"]) == {"model", "messages", "id", "stream"}


@pytest.mark.asyncio
async def test_foreign_origin_and_forwarded_headers_are_not_forwarded(env):
    """A request arriving with a foreign Origin and X-Forwarded-* must reach the
    backend WITHOUT them: loopback-with-no-Origin is what the backend's CSRF
    check trusts, and a forwarded foreign Origin trips it silently."""
    hostile = {
        "Origin": "https://evil.example.com",
        "X-Forwarded-For": "203.0.113.9",
        "X-Forwarded-Host": "evil.example.com",
        "X-Forwarded-Proto": "https",
    }
    async with run_front(make_settings(env)) as (client, _app):
        resp = await client.post(
            TURN, json={"model": "crew", "messages": [], "id": "s"}, headers=hostile
        )
    assert resp.status_code == 200
    assert len(env["fake"].requests) == 1  # it reached the backend
    got = env["fake"].requests[0]["headers"]
    assert "origin" not in got
    assert not any(k.startswith("x-forwarded") for k in got)


# --------------------------------------------------------------------------- #
# Prefix stripping + health
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_bare_and_prefixed_health(env):
    async with run_front(make_settings(env)) as (client, _app):
        bare = await client.get("/health")
        prefixed = await client.get("/c/crew/health")
    assert bare.status_code == 200 and bare.json() == {"status": "ok"}
    assert prefixed.status_code == 200 and prefixed.json() == {"status": "ok"}
    assert env["fake"].requests == []  # health never touches the backend


@pytest.mark.asyncio
async def test_turn_recognized_only_after_prefix_strip(env):
    async with run_front(make_settings(env, route_prefix="/c/crew")) as (client, _app):
        ok = await client.post(
            "/c/crew/v1/chat/completions", json={"model": "crew", "id": "s", "messages": []}
        )
    assert ok.status_code == 200


# --------------------------------------------------------------------------- #
# Per-slot serialization
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_same_slot_requests_are_serialized_never_409(env):
    env["fake"].turn_delay = 0.2
    async with run_front(make_settings(env)) as (client, _app):
        r1, r2 = await asyncio.gather(
            client.post(TURN, json={"model": "crew", "id": "A", "messages": []}),
            client.post(TURN, json={"model": "crew", "id": "A", "messages": []}),
        )
    assert r1.status_code == 200 and r2.status_code == 200
    assert env["fake"].saw_409 == 0  # the caller never saw a 409
    assert env["fake"].max_concurrent == 1  # the front queued them


@pytest.mark.asyncio
async def test_different_slots_run_concurrently(env):
    env["fake"].turn_delay = 0.2
    async with run_front(make_settings(env)) as (client, _app):
        r1, r2 = await asyncio.gather(
            client.post(TURN, json={"model": "crew", "id": "A", "messages": []}),
            client.post(TURN, json={"model": "crew", "id": "B", "messages": []}),
        )
    assert r1.status_code == 200 and r2.status_code == 200
    assert env["fake"].max_concurrent >= 2  # a global lock would make this 1


# --------------------------------------------------------------------------- #
# Customer / control separation
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_control_route_requires_the_secret(env):
    settings = make_settings(env, control_secret="CTRL")
    async with run_front(settings) as (client, _app):
        no_secret = await client.get("/c/crew/crews")
        wrong = await client.get("/c/crew/crews", headers={"X-SMC-Control-Secret": "nope"})
        right = await client.get("/c/crew/crews", headers={"X-SMC-Control-Secret": "CTRL"})
    assert no_secret.status_code == 403 and no_secret.json()["code"] == "control_forbidden"
    assert wrong.status_code == 403
    # Correct secret passes the gate; the front serves no control operation.
    assert right.status_code == 404 and right.json()["code"] == "control_not_implemented"
    assert env["fake"].requests == []  # control is never forwarded to the backend


@pytest.mark.asyncio
async def test_control_denied_when_no_secret_configured(env):
    settings = make_settings(env, control_secret=None)
    async with run_front(settings) as (client, _app):
        resp = await client.get("/c/crew/crews", headers={"X-SMC-Control-Secret": "anything"})
    assert resp.status_code == 403  # fail closed


@pytest.mark.asyncio
async def test_client_control_header_cannot_reach_control_and_is_not_forwarded(env):
    """A customer-supplied control header on the customer path stays a customer
    turn, and the header is never forwarded to the backend."""
    settings = make_settings(env, control_secret="CTRL")
    async with run_front(settings) as (client, _app):
        resp = await client.post(
            TURN,
            json={"model": "crew", "id": "s", "messages": []},
            headers={"X-SMC-Control-Secret": "CTRL", "Origin": "https://evil.example.com"},
        )
    assert resp.status_code == 200
    assert len(env["fake"].requests) == 1
    got = env["fake"].requests[0]["headers"]
    assert "x-smc-control-secret" not in got
    assert "origin" not in got


# --------------------------------------------------------------------------- #
# Boot secret re-read + single retry
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_secret_reread_and_retry_succeeds_on_403(env):
    fake: FakeState = env["fake"]
    fake.accept = "NEW"
    fake.heal_on_bad = True  # first 403 writes NEW to disk
    env["secret_file"].write_text("OLD", encoding="utf-8")
    async with run_front(make_settings(env)) as (client, _app):
        resp = await client.post(TURN, json={"model": "crew", "id": "s", "messages": []})
    assert resp.status_code == 200
    assert len(fake.requests) == 2  # one 403, one retried success
    assert fake.requests[0]["headers"][common.HEADER.lower()] == "OLD"
    assert fake.requests[1]["headers"][common.HEADER.lower()] == "NEW"


@pytest.mark.asyncio
async def test_secret_retry_gives_up_after_one_retry(env):
    fake: FakeState = env["fake"]
    fake.accept = "NEW"
    fake.heal_on_bad = False  # disk never gets the good secret
    env["secret_file"].write_text("OLD", encoding="utf-8")
    async with run_front(make_settings(env)) as (client, _app):
        resp = await client.post(TURN, json={"model": "crew", "id": "s", "messages": []})
    assert resp.status_code == 403  # relayed to the caller
    assert len(fake.requests) == 2  # exactly one retry, then give up


# --------------------------------------------------------------------------- #
# Stream projection — OpenAI-format frames (VERIFIED against a real backend on
# 2026-09-02: /v1/chat/completions streams `data: {chat.completion.chunk}` +
# `data: [DONE]` + `: keepalive`, with NO SSE event: names; the ACP event
# vocabulary and its tool_result leak live on the control /sessions/{id}/stream,
# not on this endpoint). The projection is a fail-closed frame-CONTENT allowlist.
# --------------------------------------------------------------------------- #
def _chunk(content):
    return (
        b'data: {"id": "cmpl-1", "object": "chat.completion.chunk", '
        b'"choices": [{"index": 0, "delta": {"role": "assistant", "content": "'
        + content.encode()
        + b'"}, "finish_reason": null}]}\n\n'
    )


@pytest.mark.asyncio
async def test_stream_forwards_openai_chunks_done_and_keepalive(env):
    fake: FakeState = env["fake"]
    fake.stream_chunks = [
        _chunk("Hel"),
        b": keepalive\n\n",
        _chunk("lo"),  # split across two frames to exercise buffering
        b'data: {"id": "cmpl-1", "object": "chat.completion.chunk", '
        b'"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    body = b""
    async with run_front(make_settings(env)) as (client, _app):
        async with client.stream(
            "POST", TURN, json={"model": "crew", "id": "s", "messages": [], "stream": True}
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            async for chunk in resp.aiter_bytes():
                body += chunk
    text = body.decode("utf-8")
    assert '"content": "Hel"' in text and '"content": "lo"' in text
    assert '"finish_reason": "stop"' in text
    assert "data: [DONE]" in text
    assert ": keepalive" in text


@pytest.mark.asyncio
async def test_stream_drops_named_acp_event_fail_closed(env):
    """The design's tool_result-leak concern, encoded at the correct layer: if a
    named ACP event ever appears on this endpoint, it is dropped, not relayed."""
    fake: FakeState = env["fake"]
    fake.stream_chunks = [
        _chunk("safe text"),
        b'event: tool_result\ndata: {"output": "SENSITIVE-TOOL-OUTPUT"}\n\n',
        b'event: agent_thought_chunk\ndata: {"delta": "private reasoning"}\n\n',
        b"data: [DONE]\n\n",
    ]
    body = b""
    async with run_front(make_settings(env)) as (client, _app):
        async with client.stream(
            "POST", TURN, json={"model": "crew", "id": "s", "messages": [], "stream": True}
        ) as resp:
            async for chunk in resp.aiter_bytes():
                body += chunk
    text = body.decode("utf-8")
    assert '"content": "safe text"' in text
    assert "data: [DONE]" in text
    assert "SENSITIVE-TOOL-OUTPUT" not in text
    assert "tool_result" not in text
    assert "private reasoning" not in text


@pytest.mark.asyncio
async def test_stream_drops_non_chunk_json(env):
    """A data frame that is neither a chat.completion.chunk nor an error object
    (nor [DONE]) is dropped."""
    fake: FakeState = env["fake"]
    fake.stream_chunks = [
        b'data: {"object": "something_internal", "secret": "LEAK"}\n\n',
        _chunk("ok"),
        b"data: [DONE]\n\n",
    ]
    body = b""
    async with run_front(make_settings(env)) as (client, _app):
        async with client.stream(
            "POST", TURN, json={"model": "crew", "id": "s", "messages": [], "stream": True}
        ) as resp:
            async for chunk in resp.aiter_bytes():
                body += chunk
    text = body.decode("utf-8")
    assert '"content": "ok"' in text
    assert "data: [DONE]" in text
    assert "LEAK" not in text
    assert "something_internal" not in text


@pytest.mark.asyncio
async def test_stream_backend_403_becomes_openai_error_frame(env):
    fake: FakeState = env["fake"]
    fake.accept = "NEW"
    fake.heal_on_bad = False
    env["secret_file"].write_text("OLD", encoding="utf-8")
    body = b""
    async with run_front(make_settings(env)) as (client, _app):
        async with client.stream(
            "POST", TURN, json={"model": "crew", "id": "s", "messages": [], "stream": True}
        ) as resp:
            assert resp.status_code == 200  # SSE commits 200, error is a frame
            async for chunk in resp.aiter_bytes():
                body += chunk
    assert b'"error"' in body and b"data: [DONE]" in body
    assert b"event:" not in body  # OpenAI shape, not a named event
    assert len(fake.requests) == 2  # retried once, then gave up
