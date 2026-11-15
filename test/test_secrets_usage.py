"""Tests for ``GET /api/secrets/usage`` (kiro_crew.dashboard.handlers.secrets_usage)."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.loader_credentials import CRED_JIRA_API_TOKEN, CREDENTIAL_KEYS
from kiro_crew.dashboard.handlers import secrets_usage
from kiro_crew.dashboard.handlers.secrets import setup_secrets_routes
from kiro_crew.secrets import SecretVault
from kiro_crew.testing.synthetic_credentials import github_classic

_MOD = "kiro_crew.dashboard.handlers.secrets_usage"


class _FakeCrons:
    def __init__(self, jobs: list[SimpleNamespace], *, broken: bool = False) -> None:
        self._jobs = jobs
        self._broken = broken

    async def list_jobs_async(self, include_disabled: bool = False) -> list[SimpleNamespace]:
        assert include_disabled, "a paused job still holds its grant"
        if self._broken:
            raise OSError("store unreadable")
        return self._jobs


def _app(crons: _FakeCrons) -> web.Application:
    @web.middleware
    async def _identity(request, handler):
        request["user"] = request.headers.get("X-Test-User", "local-app")
        request["app"] = request.headers.get("X-Test-App", "")
        return await handler(request)

    app = web.Application(middlewares=[_identity])
    app["state"] = SimpleNamespace(owner_id="", crons=crons)
    setup_secrets_routes(app)
    return app


def _config(deepseek: dict[str, str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        agent=SimpleNamespace(deepseek_env=deepseek or {}, strands_env={}),
        tunnel=SimpleNamespace(relay_lease_credential=""),
        dashboard=SimpleNamespace(bitbucket_server_hosts=[]),
        decisions=SimpleNamespace(provider=SimpleNamespace(api_key="", endpoint="")),
        telemetry=SimpleNamespace(traces_enabled=False),
        session_environments=SimpleNamespace(sprites_profiles=[]),
    )


@pytest.fixture(autouse=True)
def _preview_on(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr(secrets_usage, "_secrets_vault_preview_on", lambda: True)


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    vault = SecretVault(tmp_path)
    for name in ("GH", "JIRA", "CRON_TOKEN", "DEEPSEEK", "UNUSED", "a#b", "a"):
        vault._set_sync(name, "value-" + name)
    agents = tmp_path / "agents"
    agents.mkdir()
    spec = {
        "name": "crew",
        "mcpServers": {
            "github": {"command": "gh-mcp", "env": {"GITHUB_TOKEN": "secret://GH#password"}},
            "legacy": {"command": "x", "env": {"A": "secret://a#b", "PLAIN": "GH"}},
            "remote": {"url": "https://example.test", "headers": {"X": "secret://UNUSED"}},
        },
    }
    (agents / "crew.json").write_text(json.dumps(spec), encoding="utf-8")
    (agents / "other.json").write_text(json.dumps(spec), encoding="utf-8")
    assert CRED_JIRA_API_TOKEN in CREDENTIAL_KEYS
    (tmp_path / ".env").write_text(
        f"{CRED_JIRA_API_TOKEN}=secret://JIRA\nNOT_A_CREDENTIAL=secret://UNUSED\n",
        encoding="utf-8",
    )
    return tmp_path


async def _get(
    app: web.Application,
    home: Path,
    config: SimpleNamespace,
    raw_config_error: Exception | None = None,
    **headers: str,
):
    with (
        patch(f"{_MOD}.config_dir", return_value=str(home)),
        patch(
            "kiro_crew.config.loader.read_config_for_update",
            return_value={},
            side_effect=raw_config_error,
        ),
        patch("kiro_crew.agent.kiro_agents_dir_path", return_value=home / "agents"),
        patch("kiro_crew.config.paths.mate_agents_dir", return_value=home / "mates"),
        # Through the facade: env_path is one name several owners bind.
        patch("kiro_crew.config.loader.env_path", return_value=home / ".env"),
        patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=config),
        patch("kiro_crew.config.paths.data_home", return_value=home),
    ):
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/secrets/usage", headers=headers)
            return resp.status, await resp.json()


_JOBS = [
    SimpleNamespace(
        name="nightly", secret_env={"TOKEN": "CRON_TOKEN"}, secret_env_pending={"X": "UNUSED"}
    ),
    SimpleNamespace(name="no grant", secret_env={}, secret_env_pending={}),
]


@pytest.mark.asyncio
async def test_lists_every_resolving_place_and_nothing_else(home: Path) -> None:
    status, data = await _get(
        _app(_FakeCrons(_JOBS)), home, _config({"DEEPSEEK_API_KEY": "secret://DEEPSEEK"})
    )
    assert status == 200
    assert data["incomplete"] == []
    usage = data["usage"]
    # One row per place, though two agent specs declare the same server.
    assert usage["GH"] == [{"kind": "mcp_server", "name": "github", "key": "GITHUB_TOKEN"}]
    # The whole text wins over its NAME#FIELD reading, as the resolver reads it.
    assert usage["a#b"] == [{"kind": "mcp_server", "name": "legacy", "key": "A"}]
    assert "a" not in usage
    assert usage["JIRA"] == [{"kind": "env_file", "name": ".env", "key": CRED_JIRA_API_TOKEN}]
    assert usage["CRON_TOKEN"] == [{"kind": "cron_job", "name": "nightly", "key": "TOKEN"}]
    assert usage["DEEPSEEK"] == [
        {"kind": "setting", "name": "agent.deepseek_env", "key": "DEEPSEEK_API_KEY"}
    ]
    # Remote headers, non-credential .env keys and pending grants resolve nothing.
    assert "UNUSED" not in usage
    assert "value-" not in json.dumps(data)


@pytest.mark.asyncio
async def test_a_place_that_cannot_be_scanned_is_reported(home: Path) -> None:
    with patch.object(secrets_usage, "_scan_env_file", side_effect=OSError("denied")):
        status, data = await _get(_app(_FakeCrons(_JOBS, broken=True)), home, _config())
    assert status == 200
    assert data["incomplete"] == ["cron_job", "env_file"]
    assert data["usage"]["GH"][0]["kind"] == "mcp_server"


@pytest.mark.asyncio
async def test_non_owner_is_refused(home: Path) -> None:
    status, data = await _get(
        _app(_FakeCrons(_JOBS)), home, _config(), **{"X-Test-App": "some-app"}
    )
    assert status == 403
    assert data["code"] == "owner_only"


@pytest.mark.asyncio
async def test_preview_off_answers_as_before_the_route_and_scans_nothing(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(secrets_usage, "_secrets_vault_preview_on", lambda: False)
    with (
        patch.object(secrets_usage, "_scan_files", side_effect=AssertionError("scanned")),
        patch.object(secrets_usage, "_scan_cron_jobs", side_effect=AssertionError("scanned")),
    ):
        app = _app(_FakeCrons(_JOBS, broken=True))
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/secrets/usage")
            assert resp.status == 405
            assert resp.headers["Allow"] == "DELETE"


def _crew_oauth_usage(stored: set[str], raw_config: object) -> dict:
    usage: dict = {}
    with patch("kiro_crew.config.loader.read_config_for_update", return_value=raw_config):
        secrets_usage._scan_crew_oauth(usage, frozenset(stored))
    return usage


def _owner_record(name: str, url: str) -> dict:
    return {"connections": {"owner_mcp_servers": {name: {"url": url}}}}


def test_an_agent_written_grant_key_and_job_name_are_redacted() -> None:
    # A synthetic GitHub token: a credential under either redaction engine.
    aws_shaped = github_classic()
    job = SimpleNamespace(name=f"job {aws_shaped}", secret_env={aws_shaped: "CRON_TOKEN"})
    usage: dict[str, list[dict[str, str]]] = {}
    secrets_usage._scan_cron_jobs(usage, frozenset({"CRON_TOKEN"}), [job])
    [place] = usage["CRON_TOKEN"]
    assert place["kind"] == "cron_job"
    assert aws_shaped not in place["name"]
    assert aws_shaped not in place["key"]


def test_crew_oauth_entries_of_an_existing_card_are_listed_with_it() -> None:
    from kiro_crew.connections import crew_oauth, crew_oauth_dcr
    from kiro_crew.connections.registry import get_provider

    key = crew_oauth.custom_key("tracker", "https://mcp.example.test/mcp")
    owner = {crew_oauth_dcr.client_entry_name(key), crew_oauth.token_entry_name(key)}
    slug = sorted(crew_oauth.SUPPORTED_SLUGS)[0]
    card = crew_oauth.token_entry_name(slug)
    usage = _crew_oauth_usage(
        owner | {card}, _owner_record("tracker", "https://mcp.example.test/mcp")
    )
    for name in owner:
        assert usage[name] == [{"kind": "mcp_server", "name": "tracker", "key": ""}]
    provider = get_provider(slug)
    assert provider is not None
    assert usage[card] == [{"kind": "connection", "name": str(provider["name"]), "key": ""}]


def test_crew_oauth_entries_of_a_removed_card_or_a_lookalike_name_are_not_listed() -> None:
    from kiro_crew.connections import crew_oauth

    left_behind = crew_oauth.token_entry_name(
        crew_oauth.custom_key("tracker", "https://mcp.example.test/mcp")
    )
    lookalike = "CONNECTIONS_MY_OWN_OAUTH_TOKENS"
    assert _crew_oauth_usage({left_behind, lookalike}, {}) == {}


@pytest.mark.asyncio
async def test_an_unreadable_connections_record_marks_the_scan_incomplete(home: Path) -> None:
    status, data = await _get(
        _app(_FakeCrons(_JOBS)), home, _config(), raw_config_error=OSError("denied")
    )
    assert status == 200
    assert data["incomplete"] == ["connection"]


@pytest.mark.asyncio
async def test_a_server_in_the_global_settings_mcp_json_is_listed(home: Path) -> None:
    settings = home / "settings"
    settings.mkdir()
    doc = {"mcpServers": {"shared": {"command": "x", "env": {"TOKEN": "secret://UNUSED"}}}}
    (settings / "mcp.json").write_text(json.dumps(doc), encoding="utf-8")
    status, data = await _get(_app(_FakeCrons(_JOBS)), home, _config())
    assert status == 200
    assert data["usage"]["UNUSED"] == [{"kind": "mcp_server", "name": "shared", "key": "TOKEN"}]
    assert data["incomplete"] == []


@pytest.mark.asyncio
async def test_an_unreadable_spec_or_settings_file_marks_mcp_incomplete(home: Path) -> None:
    with patch(
        "kiro_crew.agent_discovery.read_agent_spec_strict", side_effect=PermissionError("locked")
    ):
        status, data = await _get(_app(_FakeCrons(_JOBS)), home, _config())
    assert status == 200
    assert "mcp_server" in data["incomplete"]

    settings = home / "settings"
    settings.mkdir()
    (settings / "mcp.json").write_text("{not json", encoding="utf-8")
    status, data = await _get(_app(_FakeCrons(_JOBS)), home, _config())
    assert "mcp_server" in data["incomplete"]


@pytest.mark.asyncio
async def test_the_relay_reference_is_read_trimmed_as_the_relay_reads_it(home: Path) -> None:
    config = _config()
    config.tunnel.relay_lease_credential = "  secret://DEEPSEEK  "
    status, data = await _get(_app(_FakeCrons(_JOBS)), home, config)
    assert status == 200
    assert data["usage"]["DEEPSEEK"] == [
        {"kind": "setting", "name": "tunnel.relay_lease_credential", "key": ""}
    ]
    # A space inside the reference is malformed to the relay, so it reads nothing.
    config.tunnel.relay_lease_credential = "secret:// DEEPSEEK"
    status, data = await _get(_app(_FakeCrons(_JOBS)), home, config)
    assert "DEEPSEEK" not in data["usage"]


def test_a_failed_probe_is_not_read_as_absent(tmp_path: Path) -> None:
    assert secrets_usage._present(tmp_path / "missing", stat.S_ISREG) is False
    (tmp_path / "file").write_text("x", encoding="utf-8")
    assert secrets_usage._present(tmp_path / "file", stat.S_ISREG) is True
    # Any stat error but a missing path is a failed probe, not absence.
    with patch.object(Path, "stat", side_effect=PermissionError("locked")):
        with pytest.raises(PermissionError):
            secrets_usage._present(tmp_path / "file", stat.S_ISREG)


@pytest.mark.asyncio
async def test_settings_that_fell_back_to_defaults_mark_their_kinds_incomplete(
    home: Path,
) -> None:
    # The loader degrades instead of raising; the scan must not read its
    # defaults as "set to nothing".
    for attr, value in (("_overlay_unreadable", True), ("degraded_sections", frozenset({"agent"}))):
        config = _config({"DEEPSEEK_API_KEY": "secret://DEEPSEEK"})
        setattr(config, attr, value)
        status, data = await _get(_app(_FakeCrons(_JOBS)), home, config)
        assert status == 200
        assert {"setting", "integration", "sprites_profile"} <= set(data["incomplete"])
        assert "DEEPSEEK" not in data["usage"]


@pytest.mark.asyncio
async def test_a_server_in_a_crewmate_file_the_rewriter_takes_is_listed(home: Path) -> None:
    from kiro_crew.mcp_gateway.mate_overlays import MateSources

    mates = home / "mates"
    mates.mkdir()
    mate = mates / "scout.json"
    doc = {
        "name": "scout",
        "mcpServers": {"tickets": {"command": "t", "env": {"K": "secret://UNUSED"}}},
    }
    mate.write_text(json.dumps(doc), encoding="utf-8")
    with patch(
        "kiro_crew.mcp_gateway.mate_overlays.list_mate_sources",
        return_value=MateSources(files=(mate,)),
    ):
        status, data = await _get(_app(_FakeCrons(_JOBS)), home, _config())
    assert status == 200
    assert data["usage"]["UNUSED"] == [{"kind": "mcp_server", "name": "tickets", "key": "K"}]
    with patch(
        "kiro_crew.mcp_gateway.mate_overlays.list_mate_sources",
        return_value=MateSources(complete=False),
    ):
        status, data = await _get(_app(_FakeCrons(_JOBS)), home, _config())
    assert "mcp_server" in data["incomplete"]
