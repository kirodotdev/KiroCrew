"""Pre-registered OAuth providers on a chat backend that drops the ``oauth`` block.

KAS takes its agents over a schema with no slot for a remote MCP entry's
``oauth`` block, so a provider whose client is pre-registered can never
finish a sign-in from a KAS chat. The mint itself runs on kiro-cli, so the
Connections page keeps Connect and the status feed carries a caveat flag on every
pre-registered row, read from one capability answer rather than a backend name.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew import mcp_grant
from kiro_crew.agent_sdk import backends
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.agent_sdk.capabilities import UNKNOWN_BACKEND_CAPABILITIES, capabilities_for
from kiro_crew.config import KiroCrewConfig
from kiro_crew.connections import get_provider, mint, status
from kiro_crew.dashboard.handlers import connections

GITHUB_MCP_URL = "https://api.githubcopilot.com/mcp/"
_REGULAR_FILE_STAT = Path(__file__).stat()

_grants: set[str] = set()
_mint_rows: dict[str, dict] = {}


class _FakeArtifact:
    def __init__(self, url: str) -> None:
        self._url = url

    def stat(self):
        if self._url in _grants:
            return _REGULAR_FILE_STAT
        raise FileNotFoundError(self._url)


def _visible() -> list[dict]:
    github = get_provider("github")
    notion = get_provider("notion")
    assert github is not None and notion is not None
    return [github, notion]


def _on_backend(monkeypatch: pytest.MonkeyPatch, backend: str, member: str | None = None) -> None:
    """Make the resolved chat (and crewmate) backend read as *backend*."""
    fake = SimpleNamespace(
        agent=SimpleNamespace(acp_backend=backend, member_acp_backend=member or backend)
    )
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls, *a, **kw: fake))


@pytest.fixture()
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setattr(status, "_CONNECTION_STATE_PATH", tmp_path / "connected-since.json")
    monkeypatch.setattr(status, "_UNPERSISTED_RECORD", None)
    monkeypatch.setattr(status, "get_visible_providers", _visible)
    monkeypatch.setattr(
        mcp_grant,
        "grant_artifact_paths",
        lambda url, **kw: (_FakeArtifact(url), _FakeArtifact(url)),
    )
    monkeypatch.setattr(mint, "pending_mint_for", lambda slug: _mint_rows.get(slug))
    _grants.clear()
    _mint_rows.clear()
    return home


def _by_slug(statuses: list[status.ConnectionStatus]) -> dict[str, status.ConnectionStatus]:
    return {entry["slug"]: entry for entry in statuses}


# ── the capability ──


def test_only_kiro_cli_carries_the_oauth_block() -> None:
    assert backends.ACP_BACKENDS_REMOTE_MCP_OAUTH == frozenset({ACP_BACKEND_KIRO})
    assert capabilities_for(ACP_BACKEND_KIRO).honors_remote_mcp_oauth is True
    assert capabilities_for(ACP_BACKEND_KAS).honors_remote_mcp_oauth is False
    assert UNKNOWN_BACKEND_CAPABILITIES.honors_remote_mcp_oauth is False


def test_the_predicate_reads_the_resolved_chat_backend(monkeypatch) -> None:
    _on_backend(monkeypatch, ACP_BACKEND_KAS)
    assert status.chat_backend_honors_preregistered_oauth() is False
    _on_backend(monkeypatch, ACP_BACKEND_KIRO)
    assert status.chat_backend_honors_preregistered_oauth() is True


def test_the_crewmate_route_does_not_decide(monkeypatch) -> None:
    # The loader resolves member_acp_backend to KAS on every install, so it
    # must not turn the caveat on for an operator whose chats run on kiro-cli.
    _on_backend(monkeypatch, ACP_BACKEND_KIRO, member=ACP_BACKEND_KAS)
    assert status.chat_backend_honors_preregistered_oauth() is True


def test_a_real_config_load_answers_from_the_chat_backend(isolated) -> None:
    resolved = KiroCrewConfig.load().agent.acp_backend
    expected = capabilities_for(resolved).honors_remote_mcp_oauth
    assert status.chat_backend_honors_preregistered_oauth() is expected


def test_an_unreadable_config_makes_no_claim(monkeypatch) -> None:
    def _boom(cls, *a, **kw):
        raise OSError("unreadable")

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_boom))
    assert status.chat_backend_honors_preregistered_oauth() is True


# ── the status feed ──


@pytest.mark.asyncio
async def test_kas_flags_a_preregistered_provider_without_blocking_it(isolated, monkeypatch):
    _on_backend(monkeypatch, ACP_BACKEND_KAS)
    github = _by_slug(await status.collect_connection_statuses())["github"]

    assert github["backendLacksOauth"] is True
    # A caveat, not a state: the client-config flag and reason are unchanged.
    assert github["needsClientConfig"] is True
    assert github["reason"] == "client_not_configured"


@pytest.mark.asyncio
async def test_kas_leaves_a_dcr_provider_alone(isolated, monkeypatch):
    _on_backend(monkeypatch, ACP_BACKEND_KAS)
    notion = _by_slug(await status.collect_connection_statuses())["notion"]

    assert "backendLacksOauth" not in notion
    assert notion["reason"] == "no_grant"


@pytest.mark.asyncio
async def test_kiro_cli_never_carries_the_flag(isolated, monkeypatch):
    _on_backend(monkeypatch, ACP_BACKEND_KIRO)
    github = _by_slug(await status.collect_connection_statuses())["github"]

    assert "backendLacksOauth" not in github


@pytest.mark.asyncio
async def test_a_connected_row_keeps_the_caveat(isolated, monkeypatch):
    _on_backend(monkeypatch, ACP_BACKEND_KAS)
    _grants.add(GITHUB_MCP_URL)
    github = _by_slug(await status.collect_connection_statuses())["github"]

    assert github["status"] == status.STATUS_CONNECTED
    assert github["backendLacksOauth"] is True


# ── the mint route ──


@pytest.mark.asyncio
async def test_mint_is_not_gated_on_the_chat_backend(isolated, monkeypatch):
    """The mint runs on kiro-cli, so KAS must not refuse it: an unconfigured
    client still answers ``client_not_configured``, not a backend refusal."""
    _on_backend(monkeypatch, ACP_BACKEND_KAS)
    app = web.Application()
    app.router.add_post("/api/connections/mint", connections.api_connections_mint)
    as_owner(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post("/api/connections/mint", json={"slug": "github"})
        assert resp.status == 409
        body = await resp.json()
        assert body["code"] == "client_not_configured"
    finally:
        await client.close()
