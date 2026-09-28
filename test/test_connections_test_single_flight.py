# testpaths-ok: tests the single-flight guard on POST /api/connections/test.
"""Connections Test runs at most one provider at a time.

Two concurrent tests must never both reach ``test_connection_tools``: the second
POST is refused with 409 ``test_in_flight`` naming the slug already running,
rather than starting a second concurrent kiro-cli session or silently queuing
behind the first.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.connections import tool_test
from kiro_crew.dashboard.handlers import connections


async def _client() -> TestClient:
    app = web.Application()
    app.router.add_post("/api/connections/test", connections.api_connections_test)
    as_owner(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.fixture(autouse=True)
def _clean_guard():
    """Reset the module-global holder so one test's crash cannot wedge the next."""
    connections._testing_slug = None
    yield
    connections._testing_slug = None


@pytest.fixture
def controlled_test(monkeypatch: pytest.MonkeyPatch):
    """Replace ``test_connection_tools`` with one call this test can hold open.

    Returns ``(await_running, release, calls)``: ``await_running`` resolves once
    the fake has actually been ENTERED (so the endpoint has committed its
    holder), ``release`` unblocks the currently in-flight call (and every future
    call resolves immediately once set), ``calls`` is the ordered list of
    provider slugs the fake was actually invoked with -- the thing a
    single-flight bug would let grow past length 1 while a call is held.

    ``await_running`` exists because ``asyncio.sleep(0)`` is NOT a happens-before
    edge: it yields one scheduling turn, which on Linux happened to be enough for
    the test client's first POST to reach the handler, while on Windows's
    Proactor loop the connect/write/accept/dispatch sequence needs several more.
    There the second request then also passed the guard, both handlers parked on
    ``gate``, and the test deadlocked hard enough to take the xdist worker down
    ("node down: Not properly terminated") instead of failing an assertion. The
    fake's own entry is the only real ordering signal, so the tests wait on that.
    """
    started = asyncio.Event()
    gate = asyncio.Event()
    calls: list[str] = []

    async def fake_test(provider: dict[str, Any]) -> dict[str, Any]:
        calls.append(str(provider["slug"]))
        started.set()
        await gate.wait()
        return {
            "schema_version": 1,
            "slug": provider["slug"],
            "verdict": "usable",
            "code": "tools_available",
            "toolCount": 1,
        }

    monkeypatch.setattr(tool_test, "test_connection_tools", fake_test)

    async def await_running() -> None:
        # Bounded: a hang here must fail this test rather than park the worker.
        await asyncio.wait_for(started.wait(), timeout=10.0)

    async def release() -> None:
        await asyncio.wait_for(started.wait(), timeout=10.0)
        gate.set()

    return await_running, release, calls


@pytest.mark.asyncio
async def test_a_second_concurrent_test_is_refused_naming_the_running_slug(controlled_test):
    await_running, release, calls = controlled_test
    client = await _client()
    try:
        first = asyncio.ensure_future(client.post("/api/connections/test", json={"slug": "linear"}))
        await await_running()  # the holder is committed once the fake is entered
        second_response = await client.post("/api/connections/test", json={"slug": "stripe"})
        second_body = await second_response.json()

        assert second_response.status == 409
        assert second_body == {
            "error": "a connection test for linear is already running",
            "code": "test_in_flight",
            "slug": "linear",
        }
        # The refused caller never reached the engine: exactly one call, for the
        # provider that was already running, not two.
        assert calls == ["linear"]

        await release()
        first_response = await first
        first_body = await first_response.json()
        assert first_response.status == 200
        assert first_body["slug"] == "linear"
        assert first_body["verdict"] == "usable"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_duplicate_test_for_the_same_provider_also_refuses(controlled_test):
    await_running, release, calls = controlled_test
    client = await _client()
    try:
        first = asyncio.ensure_future(client.post("/api/connections/test", json={"slug": "linear"}))
        await await_running()
        second_response = await client.post("/api/connections/test", json={"slug": "linear"})
        second_body = await second_response.json()

        assert second_response.status == 409
        assert second_body["code"] == "test_in_flight"
        assert second_body["slug"] == "linear"
        assert calls == ["linear"]

        await release()
        first_response = await first
        assert first_response.status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_new_test_succeeds_once_the_running_one_completes(controlled_test):
    await_running, release, calls = controlled_test
    client = await _client()
    try:
        first = asyncio.ensure_future(client.post("/api/connections/test", json={"slug": "linear"}))
        await await_running()
        await release()
        first_response = await first
        assert first_response.status == 200

        third_response = await client.post("/api/connections/test", json={"slug": "stripe"})
        third_body = await third_response.json()
        assert third_response.status == 200
        assert third_body["slug"] == "stripe"
        assert calls == ["linear", "stripe"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_the_holder_clears_even_when_the_engine_raises(monkeypatch: pytest.MonkeyPatch):
    async def failing_test(provider: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("boom")

    monkeypatch.setattr(tool_test, "test_connection_tools", failing_test)
    client = await _client()
    try:
        response = await client.post("/api/connections/test", json={"slug": "linear"})
        # aiohttp's server turns an unhandled exception into a 500 rather than
        # propagating it to the test client -- the behavior under test is that
        # the `finally` clears the holder either way, not the exception's shape.
        assert response.status == 500
    finally:
        await client.close()

    assert connections._testing_slug is None
    # A fresh test now runs rather than being wedged behind the crashed one.
    monkeypatch.setattr(
        tool_test,
        "test_connection_tools",
        lambda provider: asyncio.sleep(
            0,
            result={
                "schema_version": 1,
                "slug": provider["slug"],
                "verdict": "usable",
                "code": "tools_available",
                "toolCount": 1,
            },
        ),
    )
    client2 = await _client()
    try:
        response = await client2.post("/api/connections/test", json={"slug": "linear"})
        assert response.status == 200
    finally:
        await client2.close()
