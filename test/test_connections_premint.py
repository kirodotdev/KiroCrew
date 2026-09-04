"""Premint endpoint tests: the route, the report it makes, and what it must not wait for.

``POST /api/connections/premint`` is the warm engine's only request-path caller. Its
contract is narrow -- ``{"ok": true, "preminting": [<slug>, ...]}`` -- and the whole point
is that the slugs are reported BEFORE any provider process exists, so the tests here pin
the non-blocking property directly rather than trusting the handler's shape.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.connections.registry import Provider
from kiro_crew.dashboard.handlers import connections


def _provider(slug: str) -> Provider:
    return {  # type: ignore[typeddict-item]
        "slug": slug,
        "mcp_url": f"https://{slug}.example/mcp",
        "l0_expectations": {"dcr": True},
    }


async def _client() -> TestClient:
    app = web.Application()
    app.router.add_post("/api/connections/premint", connections.api_connections_premint)
    as_owner(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.fixture
def warm_engine(monkeypatch: pytest.MonkeyPatch):
    """Stub the warm engine's two entry points. NEVER a real provider spawn.

    Patched on the engine module, which is the namespace the handler's
    function-local import resolves against -- patching a name on the handler
    module would miss, and the real activation would spawn kiro-cli.
    """
    from kiro_crew.connections import warm

    calls: dict[str, object] = {"warmed": None, "invocations": 0}
    candidates: list[Provider] = [_provider("linear"), _provider("vercel")]

    async def _warm_mint_all(providers: list[Provider] | None = None) -> list[str]:
        calls["invocations"] = int(calls["invocations"]) + 1
        calls["warmed"] = providers
        return [str(provider["slug"]) for provider in providers or []]

    monkeypatch.setattr(warm, "mintable_providers", lambda: list(candidates))
    monkeypatch.setattr(warm, "warm_mint_all", _warm_mint_all)
    calls["candidates"] = candidates
    return calls


async def _premint(client: TestClient) -> tuple[int, dict]:
    resp = await client.post("/api/connections/premint")
    return resp.status, await resp.json()


# ── the wire contract ──


@pytest.mark.asyncio
async def test_premint_reports_every_mintable_provider(warm_engine):
    client = await _client()
    try:
        status, body = await _premint(client)
    finally:
        await client.close()

    assert status == 200
    assert body == {"ok": True, "preminting": ["linear", "vercel"]}


@pytest.mark.asyncio
async def test_premint_drives_the_warm_engine_with_the_slugs_it_reported(warm_engine):
    """The response and the claim set come from ONE registry scan, not two.

    The handler passes the candidates it just scanned rather than letting the engine
    re-scan: two independent scans can disagree (a consent completing between them
    drops a provider), and the response would then name a slug nothing claimed.
    """
    client = await _client()
    try:
        _status, body = await _premint(client)
    finally:
        await client.close()

    # The task is fired, not awaited, so give the loop one pass to start it.
    await asyncio.sleep(0)
    assert warm_engine["invocations"] == 1
    warmed = warm_engine["warmed"]
    assert warmed is not None
    assert [str(provider["slug"]) for provider in warmed] == body["preminting"]


@pytest.mark.asyncio
async def test_premint_with_no_eligible_provider_warms_nothing(
    monkeypatch: pytest.MonkeyPatch, warm_engine
):
    """An empty candidate set must not activate a process to warm nothing."""
    from kiro_crew.connections import warm

    monkeypatch.setattr(warm, "mintable_providers", lambda: [])
    client = await _client()
    try:
        status, body = await _premint(client)
    finally:
        await client.close()

    assert status == 200
    assert body == {"ok": True, "preminting": []}
    await asyncio.sleep(0)
    assert warm_engine["invocations"] == 0


@pytest.mark.asyncio
async def test_premint_answers_before_the_activation_settles(
    monkeypatch: pytest.MonkeyPatch, warm_engine
):
    """The page fires this on mount; an activation costs seconds, so it cannot be awaited.

    A handler that awaited the engine would hang here for the whole test timeout
    instead of answering, which is exactly the regression this pins.
    """
    from kiro_crew.connections import warm

    released = asyncio.Event()
    started = asyncio.Event()

    async def _never_settles(providers: list[Provider] | None = None) -> list[str]:
        started.set()
        await released.wait()
        return []

    monkeypatch.setattr(warm, "warm_mint_all", _never_settles)
    client = await _client()
    try:
        status, body = await asyncio.wait_for(_premint(client), timeout=5)
        assert status == 200
        assert body["preminting"] == ["linear", "vercel"]
        # The engine really was entered -- the response overtook it rather than the
        # handler having skipped the call altogether.
        await asyncio.wait_for(started.wait(), timeout=5)
    finally:
        released.set()
        await client.close()


# ── the gate ──


@pytest.mark.asyncio
async def test_premint_is_owner_gated(warm_engine):
    """Same gate as the sibling POSTs: warming spawns a kiro-cli process."""
    client = await _client()
    try:
        resp = await client.post(
            "/api/connections/premint", headers={"X-Test-User": "someone-else"}
        )
        assert resp.status in (401, 403)
        body = await resp.json()
    finally:
        await client.close()
    assert body.get("code")
    await asyncio.sleep(0)
    assert warm_engine["invocations"] == 0


# ── the acted-on grant observation is SEL-audited ──


def test_the_premint_read_id_is_registered_with_the_audit_gate():
    """``emit_internal_read_audit`` fail-closes on an unregistered read_id, and the
    audit tests below monkeypatch the hook, so only this un-mocked check can catch a
    registration gap that silently disables the audit."""
    from kiro_crew import hooks

    assert connections._GRANT_PRESENCE_READ_ID in hooks._AUDIT_ONLY_READ_IDS


@pytest.mark.asyncio
async def test_premint_audits_the_grant_observation_it_acts_on(warm_engine, monkeypatch):
    """The scan stats kiro-cli's OAuth artifacts and this handler SPAWNS on the answer.

    One event per activation, not one per candidate: the scan is a single pass that
    yields N candidates and exactly one act decision, so N events would over-count one
    observation.
    """
    from kiro_crew import hooks

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        hooks,
        "emit_internal_read_audit",
        lambda read_id, outcome: (calls.append((read_id, outcome)), True)[1],
    )

    client = await _client()
    try:
        status, body = await _premint(client)
    finally:
        await client.close()

    assert status == 200
    assert body["preminting"] == ["linear", "vercel"]
    assert calls == [(connections._GRANT_PRESENCE_READ_ID, "success")]


@pytest.mark.asyncio
async def test_premint_still_warms_when_the_audit_cannot_be_recorded(
    warm_engine, monkeypatch, caplog
):
    """Best-effort, not fail-closed: the artifacts are stat-ed, never opened.

    Denying here would turn an SEL outage into a page that pays a cold spawn on every
    Connect, so an unaudited boolean is the lesser failure -- and it leaves a warning.
    """
    from kiro_crew import hooks

    monkeypatch.setattr(hooks, "emit_internal_read_audit", lambda read_id, outcome: False)

    client = await _client()
    try:
        with caplog.at_level(logging.WARNING, logger=connections.__name__):
            status, body = await _premint(client)
    finally:
        await client.close()

    assert status == 200
    assert body["preminting"] == ["linear", "vercel"]
    # The activation is not gated on the trail.
    await asyncio.sleep(0)
    assert warm_engine["invocations"] == 1
    assert any(
        "proceeding unaudited" in record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    )


@pytest.mark.asyncio
async def test_premint_with_no_candidates_audits_nothing(warm_engine, monkeypatch):
    """An empty scan is not an acted-on observation, so it owes no trail.

    Same rule as ``connections.status``: the event records the observation a caller
    ACTS on. Here the handler returns early -- no process is spawned, nothing is
    persisted, and no grant answer reaches the user -- so a page mounted against a
    fully-authorized gallery must not write an event on every poll.
    """
    from kiro_crew import hooks
    from kiro_crew.connections import warm

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        hooks,
        "emit_internal_read_audit",
        lambda read_id, outcome: (calls.append((read_id, outcome)), True)[1],
    )
    monkeypatch.setattr(warm, "mintable_providers", lambda: [])

    client = await _client()
    try:
        status, body = await _premint(client)
    finally:
        await client.close()

    assert status == 200
    assert body == {"ok": True, "preminting": []}
    assert calls == []


# ── the route is reachable, not merely defined ──


def test_the_premint_route_is_wired_into_the_dashboard():
    """A handler the route table never registers is unreachable from the page."""
    from kiro_crew.dashboard.routes import agent_config

    app = web.Application()
    agent_config.register(app)
    wired = {
        (route.method, route.resource.canonical)
        for route in app.router.routes()
        if route.resource is not None
    }
    assert ("POST", "/api/connections/premint") in wired


# ── the boot path stays free of the engine ──


def test_the_handlers_package_does_not_import_the_warm_engine():
    """The gateway imports the handlers package at boot; the warm engine must not ride along.

    The warm engine imports the cold mint at module scope and adds the ACP runtime and the
    MCP inventory on top, so a module-scope import in the premint handler would put the
    heaviest half of Connections on every gateway start. Run in a subprocess because this
    test module reaches the engine directly, so an in-process ``sys.modules`` check would
    always find it.
    """
    probe = (
        "import sys; import kiro_crew.dashboard.handlers;"
        " leaked = [m for m in ('kiro_crew.connections.warm', 'kiro_crew.connections.mint')"
        " if m in sys.modules];"
        " print('LEAKED:' + ','.join(leaked) if leaked else 'CLEAN')"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().endswith("CLEAN"), out.stdout
