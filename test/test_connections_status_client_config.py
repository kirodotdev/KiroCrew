"""Tests for the ``needsClientConfig`` flag on the Connections status feed.

A pre-registered provider (registry ``auth.mode``) whose operator has not entered
a usable OAuth client must tell the card so: a Connect would fail at the vendor
with a registration error no user can act on. The flag is derived from the same
view function the Settings tab reads, over an ISOLATED config dir and vault, so
nothing here touches the operator's real home.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew import mcp_grant
from kiro_crew.connections import get_provider, mint, status
from kiro_crew.dashboard.handlers import connections
from kiro_crew.secrets import SecretVault

GITHUB_MCP_URL = "https://api.githubcopilot.com/mcp/"
NOTION_MCP_URL = "https://mcp.notion.com/mcp"
ENV_ID = "KIROCREW_CONNECTIONS_GITHUB_CLIENT_ID"
ENV_SECRET = "KIROCREW_CONNECTIONS_GITHUB_CLIENT_SECRET"
VAULT_NAME = "CONNECTIONS_GITHUB_CLIENT_SECRET"

_REGULAR_FILE_STAT = Path(__file__).stat()

# Mutable facts the fixture reads; reset per test through ``_set_facts``.
_grants: set[str] = set()
_mint_rows: dict[str, dict] = {}


class _FakeArtifact:
    def __init__(self, url: str) -> None:
        self._url = url

    def stat(self):
        if self._url in _grants:
            return _REGULAR_FILE_STAT
        raise FileNotFoundError(self._url)


def _set_facts(granted: set[str] | None = None, rows: dict[str, dict] | None = None) -> None:
    _grants.clear()
    _grants.update(granted or set())
    _mint_rows.clear()
    _mint_rows.update(rows or {})


def _visible() -> list[dict]:
    github = get_provider("github")
    notion = get_provider("notion")
    assert github is not None and notion is not None
    return [github, notion]


@pytest.fixture()
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Scratch home (config + vault), scratch sidecar, GitHub + Notion visible."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.delenv(ENV_ID, raising=False)
    monkeypatch.delenv(ENV_SECRET, raising=False)
    monkeypatch.setattr(status, "_CONNECTION_STATE_PATH", tmp_path / "connected-since.json")
    monkeypatch.setattr(status, "_UNPERSISTED_RECORD", None)
    monkeypatch.setattr(status, "get_visible_providers", _visible)
    monkeypatch.setattr(
        mcp_grant,
        "grant_artifact_paths",
        lambda url, **kw: (_FakeArtifact(url), _FakeArtifact(url)),
    )
    monkeypatch.setattr(mint, "pending_mint_for", lambda slug: _mint_rows.get(slug))
    _set_facts()
    return home


def _by_slug(statuses: list[status.ConnectionStatus]) -> dict[str, status.ConnectionStatus]:
    return {entry["slug"]: entry for entry in statuses}


def _write_config_client_id(home: Path, slug: str, client_id: str) -> None:
    (home / "config.json").write_text(
        json.dumps({"connections": {"oauth_clients": {slug: {"client_id": client_id}}}}),
        encoding="utf-8",
    )


# ── collect_connection_statuses ──


@pytest.mark.asyncio
async def test_an_unconfigured_preregistered_provider_is_flagged(isolated):
    statuses = _by_slug(await status.collect_connection_statuses())

    github = statuses["github"]
    assert github["status"] == status.STATUS_NOT_CONNECTED
    assert github["reason"] == "client_not_configured"
    assert github["needsClientConfig"] is True
    assert github["grantPresent"] is False
    assert "connectedSince" not in github


@pytest.mark.asyncio
async def test_a_dcr_provider_never_carries_the_key(isolated):
    statuses = _by_slug(await status.collect_connection_statuses())

    notion = statuses["notion"]
    assert "needsClientConfig" not in notion
    assert notion["reason"] == "no_grant"


@pytest.mark.asyncio
async def test_a_dcr_provider_with_a_grant_never_carries_the_key(isolated):
    _set_facts(granted={NOTION_MCP_URL})
    statuses = _by_slug(await status.collect_connection_statuses())

    assert statuses["notion"]["status"] == status.STATUS_CONNECTED
    assert "needsClientConfig" not in statuses["notion"]


@pytest.mark.asyncio
async def test_env_configured_client_clears_the_flag(isolated, monkeypatch):
    monkeypatch.setenv(ENV_ID, "Iv1.operator")
    monkeypatch.setenv(ENV_SECRET, "operator-secret")

    statuses = _by_slug(await status.collect_connection_statuses())

    github = statuses["github"]
    assert "needsClientConfig" not in github
    assert github["status"] == status.STATUS_NOT_CONNECTED
    assert github["reason"] == "no_grant"


@pytest.mark.asyncio
async def test_a_confidential_client_with_only_an_id_stays_flagged(isolated, monkeypatch):
    """GitHub is confidential: the id alone cannot reach the token endpoint."""
    monkeypatch.setenv(ENV_ID, "Iv1.operator")

    statuses = _by_slug(await status.collect_connection_statuses())

    assert statuses["github"]["needsClientConfig"] is True
    assert statuses["github"]["reason"] == "client_not_configured"


@pytest.mark.asyncio
async def test_an_unusable_env_value_does_not_count_as_configured(isolated, monkeypatch):
    monkeypatch.setenv(ENV_ID, "has a space")
    monkeypatch.setenv(ENV_SECRET, "operator-secret")

    statuses = _by_slug(await status.collect_connection_statuses())

    assert statuses["github"]["needsClientConfig"] is True


@pytest.mark.asyncio
async def test_config_id_plus_vault_secret_clears_the_flag(isolated):
    """The dashboard-written shape: id in config.json, secret in the vault."""
    _write_config_client_id(isolated, "github", "Iv1.operator")
    SecretVault(isolated).set_sync(VAULT_NAME, "operator-secret")

    statuses = _by_slug(await status.collect_connection_statuses())

    assert "needsClientConfig" not in statuses["github"]
    assert statuses["github"]["reason"] == "no_grant"


@pytest.mark.asyncio
async def test_config_id_without_a_vault_secret_stays_flagged(isolated):
    _write_config_client_id(isolated, "github", "Iv1.operator")

    statuses = _by_slug(await status.collect_connection_statuses())

    assert statuses["github"]["needsClientConfig"] is True


@pytest.mark.asyncio
async def test_a_vault_secret_for_another_provider_does_not_count(isolated):
    _write_config_client_id(isolated, "github", "Iv1.operator")
    SecretVault(isolated).set_sync("CONNECTIONS_ASANA_CLIENT_SECRET", "not-github")

    statuses = _by_slug(await status.collect_connection_statuses())

    assert statuses["github"]["needsClientConfig"] is True


@pytest.mark.asyncio
async def test_a_held_grant_suppresses_the_flag(isolated):
    """A client the operator later removed does not un-authorize a session kiro-cli
    still holds a token for; the card must not tell a connected user to configure."""
    _set_facts(granted={GITHUB_MCP_URL})

    statuses = _by_slug(await status.collect_connection_statuses())

    github = statuses["github"]
    assert github["status"] == status.STATUS_CONNECTED
    assert github["reason"] == "grant_present"
    assert "needsClientConfig" not in github
    assert github.get("connectedSince")


@pytest.mark.asyncio
async def test_the_flag_returns_once_the_grant_is_gone(isolated):
    _set_facts(granted={GITHUB_MCP_URL})
    await status.collect_connection_statuses()
    _set_facts(granted=set())

    statuses = _by_slug(await status.collect_connection_statuses())

    assert statuses["github"]["needsClientConfig"] is True
    assert statuses["github"]["reason"] == "client_not_configured"
    assert "connectedSince" not in statuses["github"]


@pytest.mark.asyncio
async def test_an_in_flight_mint_keeps_its_own_reason_and_is_not_flagged(isolated):
    """A mint could not have started without a client, so an awaiting-consent row
    keeps ``mint_in_flight`` and carries NO configuration flag: the card must keep
    rendering the consent it is waiting on rather than an instruction to go
    configure."""
    _set_facts(rows={"github": {"state": "waiting", "token": "t"}})

    statuses = _by_slug(await status.collect_connection_statuses())

    github = statuses["github"]
    assert github["status"] == status.STATUS_AWAITING_CONSENT
    assert github["reason"] == "mint_in_flight"
    assert "needsClientConfig" not in github


@pytest.mark.asyncio
async def test_an_indeterminate_grant_lookup_still_flags(isolated, monkeypatch):
    """ "Could not look" is not a grant, so the flag stands; the indeterminacy is
    still reported on its own key."""

    class _Unreadable:
        def stat(self):
            raise PermissionError("EACCES")

    monkeypatch.setattr(
        mcp_grant, "grant_artifact_paths", lambda url, **kw: (_Unreadable(), _Unreadable())
    )

    statuses = _by_slug(await status.collect_connection_statuses())

    github = statuses["github"]
    assert github["grantIndeterminate"] is True
    assert github["grantPresent"] is False
    assert github["needsClientConfig"] is True
    # Only a CONFIRMED absence is renamed; "could not look" keeps its own reason
    # so the signal survives next to the flag instead of being overwritten.
    assert github["reason"] == "grant_unreadable"


# ── _client_config_map ──


def test_client_config_map_is_empty_without_preregistered_providers(isolated, monkeypatch):
    """No vault or config read at all for a DCR-only page."""
    import kiro_crew.secrets as secrets_module

    def _never(*a, **kw):  # pragma: no cover - reaching this IS the failure
        raise AssertionError("the vault must not be opened for DCR-only providers")

    monkeypatch.setattr(secrets_module, "SecretVault", _never)
    notion = get_provider("notion")
    assert notion is not None
    assert status._client_config_map([notion]) == {}


def test_client_config_map_reports_only_preregistered_slugs(isolated):
    assert status._client_config_map(_visible()) == {"github": False}


def test_client_config_map_reads_config_and_vault(isolated):
    _write_config_client_id(isolated, "github", "Iv1.operator")
    SecretVault(isolated).set_sync(VAULT_NAME, "operator-secret")
    assert status._client_config_map(_visible()) == {"github": True}


def test_an_unreadable_vault_reads_as_not_configured(isolated, monkeypatch):
    import kiro_crew.secrets as secrets_module

    class _Broken:
        def __init__(self, *a, **kw) -> None:
            pass

        def list_names(self):
            raise OSError("vault unreadable")

    monkeypatch.setattr(secrets_module, "SecretVault", _Broken)
    _write_config_client_id(isolated, "github", "Iv1.operator")
    assert status._client_config_map(_visible()) == {"github": False}


def test_an_unreadable_config_reads_as_not_configured(isolated):
    (isolated / "config.json").write_text("{not json", encoding="utf-8")
    SecretVault(isolated).set_sync(VAULT_NAME, "operator-secret")
    assert status._client_config_map(_visible()) == {"github": False}


def test_the_map_never_holds_a_secret_value(isolated):
    SecretVault(isolated).set_sync(VAULT_NAME, "operator-secret")
    mapping = status._client_config_map(_visible())
    assert all(isinstance(v, bool) for v in mapping.values())


# ── HTTP surface ──


async def _client() -> TestClient:
    app = web.Application()
    app.router.add_get("/api/connections/status", connections.api_connections_status)
    as_owner(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_status_endpoint_serves_the_flag(isolated):
    client = await _client()
    try:
        resp = await client.get("/api/connections/status")
        assert resp.status == 200
        body = await resp.json()
    finally:
        await client.close()

    verdicts = {entry["slug"]: entry for entry in body["connections"]}
    assert verdicts["github"]["needsClientConfig"] is True
    assert verdicts["github"]["reason"] == "client_not_configured"
    assert "needsClientConfig" not in verdicts["notion"]
    assert "operator" not in json.dumps(body)
