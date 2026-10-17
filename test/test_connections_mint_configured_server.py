"""The ``server`` form of ``/api/connections/mint``: sign in to an owner-added server.

A remote MCP server the owner added from the dashboard is signed in to on the
same mint engine as a provider. The caller names the server; the URL the mint
connects to comes from the main agent spec, never the request, and must equal
the URL the owner's add recorded in the sealed ``config.json``. A server an agent
or a session added has no such record and is refused. These tests pin that
bound and the routes' shape. The engine itself is stubbed: its behaviour is
covered in ``test_connections_mint.py``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.connections import mint
from kiro_crew.dashboard.handlers import connections

_DOCS_URL = "https://docs.example.com/mcp"
NON_OWNER = {"X-Test-User": "someone-else"}


def _write_owner_record(home: Path, servers: dict[str, Any]) -> None:
    (home / "config.json").write_text(
        json.dumps({"connections": {"owner_mcp_servers": servers}}), encoding="utf-8"
    )


def _add_spec_entry(agents_dir: Path, name: str, entry: dict[str, Any]) -> None:
    path = agents_dir / "kirocrew.json"
    spec = json.loads(path.read_text(encoding="utf-8"))
    spec["mcpServers"][name] = entry
    path.write_text(json.dumps(spec), encoding="utf-8")


@pytest.fixture()
def agents_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A scratch main agent spec holding one remote, one local, one registry entry."""
    directory = tmp_path / "agents"
    directory.mkdir()
    (directory / "kirocrew.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "docsServer": {"url": _DOCS_URL},
                    "local-tool": {"command": "local-tool-bin", "args": []},
                    "plain-http": {"url": "ftp://files.example.com/mcp"},
                    "notion": {"url": "https://self-hosted.example.com/mcp"},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("kiro_crew.agent.kiro_agents_dir_path", lambda: directory)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    # The owner added docsServer from the dashboard. The other entries were not
    # recorded: plain-http and local-tool would be refused on shape alone, and
    # notion is a registry name.
    _write_owner_record(home, {"docsServer": {"url": _DOCS_URL}})
    monkeypatch.setattr(mint, "_mints", {})
    monkeypatch.setattr(mint, "_mints_lock", asyncio.Lock())
    return directory


@pytest.fixture()
def started(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Record the mints the route schedules instead of spawning kiro-cli."""
    calls: list[tuple[str, str]] = []

    async def _fake_start(
        slug: str, mcp_url: str, token: Any = None, prior: Any = None, pinned: Any = None
    ) -> None:
        # An owner-added server always mints with the entry that was validated.
        assert pinned is not None and pinned.get("url") == mcp_url
        calls.append((slug, mcp_url))

    monkeypatch.setattr(mint, "start_oauth_mint", _fake_start)
    return calls


async def _client() -> TestClient:
    app = web.Application()
    app.router.add_post("/api/connections/mint", connections.api_connections_mint)
    app.router.add_get("/api/connections/mint", connections.api_connections_mint_state)
    as_owner(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def _drain_mint_tasks() -> None:
    if connections._mint_tasks:
        await asyncio.gather(*list(connections._mint_tasks))


@pytest.mark.asyncio
async def test_a_configured_remote_server_mints_against_the_url_in_the_owner_spec(
    agents_dir: Path, started: list[tuple[str, str]]
):
    client = await _client()
    try:
        resp = await client.post("/api/connections/mint", json={"server": "docsServer"})
        assert resp.status == 200
        body = await resp.json()
        assert body["slug"] == "docsServer"
        assert body["state"] == "minting"
        await _drain_mint_tasks()
    finally:
        await client.close()
    assert started == [("docsServer", _DOCS_URL)]


@pytest.mark.asyncio
async def test_a_url_in_the_request_is_never_what_the_mint_connects_to(
    agents_dir: Path, started: list[tuple[str, str]]
):
    client = await _client()
    try:
        resp = await client.post(
            "/api/connections/mint",
            json={"server": "docsServer", "mcp_url": "https://elsewhere.example.com/mcp"},
        )
        assert resp.status == 200
        await _drain_mint_tasks()
    finally:
        await client.close()
    assert started == [("docsServer", _DOCS_URL)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "server",
    [
        "not-configured",
        "local-tool",
        "plain-http",
        # A registry name stays on the provider path, even when the owner's entry
        # under it points somewhere else.
        "notion",
        "../escape",
        "",
        42,
    ],
)
async def test_a_server_that_is_not_a_configured_remote_server_is_refused(
    agents_dir: Path, started: list[tuple[str, str]], server: Any
):
    client = await _client()
    try:
        resp = await client.post("/api/connections/mint", json={"server": server})
        assert resp.status == 400
        assert (await resp.json())["code"] == "unknown_server"
    finally:
        await client.close()
    assert started == []


@pytest.mark.asyncio
async def test_the_configured_server_mint_is_owner_only(
    agents_dir: Path, started: list[tuple[str, str]]
):
    client = await _client()
    try:
        resp = await client.post(
            "/api/connections/mint", json={"server": "docsServer"}, headers=NON_OWNER
        )
        assert resp.status == 403
    finally:
        await client.close()
    assert started == []


@pytest.mark.asyncio
async def test_an_unreadable_main_spec_refuses_rather_than_minting(
    agents_dir: Path, started: list[tuple[str, str]]
):
    (agents_dir / "kirocrew.json").write_text("[not an object]", encoding="utf-8")
    client = await _client()
    try:
        resp = await client.post("/api/connections/mint", json={"server": "docsServer"})
        assert resp.status == 400
    finally:
        await client.close()
    assert started == []


@pytest.mark.asyncio
async def test_the_state_poll_reads_the_configured_server_row_by_its_exact_name(
    agents_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    asked: list[str] = []

    async def _no_expiry(slug: str) -> None:
        asked.append(slug)

    def _row(slug: str) -> dict[str, Any] | None:
        asked.append(slug)
        return {
            "state": "waiting",
            "oauth_url": "https://docs.example.com/authorize",
            "token": "t1",
        }

    monkeypatch.setattr(mint, "expire_dead_holder", _no_expiry)
    monkeypatch.setattr(mint, "pending_mint_for", _row)
    client = await _client()
    try:
        resp = await client.get("/api/connections/mint", params={"server": "docsServer"})
        assert resp.status == 200
        body = await resp.json()
        assert body["slug"] == "docsServer"
        assert body["oauth_url"] == "https://docs.example.com/authorize"

        refused = await client.get("/api/connections/mint", params={"server": "local-tool"})
        assert refused.status == 400
        assert (await refused.json())["code"] == "unknown_server"
    finally:
        await client.close()
    # Keyed by the exact configured name, not the registry's lowercase slug.
    assert asked == ["docsServer", "docsServer"]


@pytest.mark.asyncio
async def test_a_provider_mint_without_a_server_field_keeps_the_registry_bound(
    agents_dir: Path, started: list[tuple[str, str]]
):
    client = await _client()
    try:
        resp = await client.post("/api/connections/mint", json={"slug": "docsServer"})
        assert resp.status == 400
        assert (await resp.json())["code"] == "unknown_provider"
    finally:
        await client.close()
    assert started == []


@pytest.mark.asyncio
async def test_a_remote_server_an_agent_added_is_refused(
    agents_dir: Path, started: list[tuple[str, str]]
):
    # An agent appended a remote entry to the config the spec is rebuilt from. It
    # reaches the spec, but no owner add recorded it.
    _add_spec_entry(agents_dir, "agentServer", {"url": "https://agent.example.com/mcp"})
    client = await _client()
    try:
        resp = await client.post("/api/connections/mint", json={"server": "agentServer"})
        assert resp.status == 400
        assert (await resp.json())["code"] == "unknown_server"
        poll = await client.get("/api/connections/mint", params={"server": "agentServer"})
        assert poll.status == 400
    finally:
        await client.close()
    assert started == []


@pytest.mark.asyncio
async def test_an_owner_server_an_agent_repointed_is_refused(
    agents_dir: Path, started: list[tuple[str, str]]
):
    # The name is the owner's, but the spec's URL differs from the recorded one.
    _add_spec_entry(agents_dir, "docsServer", {"url": "https://elsewhere.example.com/mcp"})
    client = await _client()
    try:
        resp = await client.post("/api/connections/mint", json={"server": "docsServer"})
        assert resp.status == 400
        assert (await resp.json())["code"] == "unknown_server"
    finally:
        await client.close()
    assert started == []


@pytest.mark.asyncio
async def test_a_server_only_a_session_spec_declares_is_refused(
    agents_dir: Path, started: list[tuple[str, str]], tmp_path: Path
):
    # A session or a crew member may start with its own spec. Even with an owner
    # record under the same name, the mint reads only the main spec.
    (agents_dir / "session-agent.json").write_text(
        json.dumps({"name": "session-agent", "mcpServers": {"sessionServer": {"url": _DOCS_URL}}}),
        encoding="utf-8",
    )
    _write_owner_record(
        tmp_path / "home",
        {"docsServer": {"url": _DOCS_URL}, "sessionServer": {"url": _DOCS_URL}},
    )
    client = await _client()
    try:
        resp = await client.post("/api/connections/mint", json={"server": "sessionServer"})
        assert resp.status == 400
    finally:
        await client.close()
    assert started == []


@pytest.mark.asyncio
async def test_an_unreadable_owner_record_refuses_rather_than_minting(
    agents_dir: Path, started: list[tuple[str, str]], tmp_path: Path
):
    (tmp_path / "home" / "config.json").write_text("{not json", encoding="utf-8")
    client = await _client()
    try:
        resp = await client.post("/api/connections/mint", json={"server": "docsServer"})
        assert resp.status == 400
    finally:
        await client.close()
    assert started == []


def test_the_mint_spec_is_written_from_the_validated_entry_not_a_reread(agents_dir: Path):
    # The main spec changes between the owner check and the mint spec write. The
    # mint still connects to the entry that was checked.
    _add_spec_entry(agents_dir, "docsServer", {"url": "https://elsewhere.example.com/mcp"})

    name, path = mint._write_mint_agent_spec("docsServer", {"url": _DOCS_URL})
    try:
        written = json.loads(Path(path).read_text(encoding="utf-8"))
        assert written["mcpServers"] == {"docsServer": {"url": _DOCS_URL}}
    finally:
        mint._remove_mint_agent_spec(path)
