"""A Settings MCP sign-in on KAS runs through a KAS engine into the owner's vault.

The engine is a scripted stand-in, but the handshake decision is the real one
(``runtime_spawn_launch._with_mcp_sign_in_storage``) and the vault is the real
per-user grant store, so these tests pin what the design depends on: a Settings
sign-in is readable by a later owner KAS session and by no other user's; with
persistence off nothing is spawned; a process that cannot keep the grant never
reports it granted; and the consent URL takes the same credential gate as the
kiro-cli mint.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import connections_mint_helpers
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from connections_mint_helpers import _AUTHORIZE, _FakeClient, _state_only
from dashboard_owner_helpers import as_owner

from kiro_crew import mcp_grant_principals as principals
from kiro_crew.acp import kas_secret_storage as kss
from kiro_crew.acp import runtime_spawn_launch
from kiro_crew.acp.types import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.agent_sdk import mcp_sign_in
from kiro_crew.connections import mint, mint_kas
from kiro_crew.dashboard.handlers import connections
from kiro_crew.testing.wait import async_wait_until

# The shared fixtures, bound here by name so pytest registers them for this module.
_isolated_mint = connections_mint_helpers._isolated_mint
#: The real backend check, held before ``_isolated_mint`` pins it off for each test.
_kas_mint_applies = mint_kas.kas_mint_applies

_SLUG = "notion"
_TOKENS_KEY = f"kiro.mcp.{'cd' * 32}.tokens"
_TOKEN_VALUE = '{"access_token":"tok-SECRET-71b0","refresh_token":"rt-SECRET-0c3d"}'
_CREDENTIAL_URL = (
    "https://auth.example.com/authorize?client_id=abc&access_token=AKIAIOSFODNN7EXAMPLE"
)


class _Report:
    def __init__(self, buckets: dict[str, list[str]]) -> None:
        self.buckets = buckets

    def payload(self) -> dict[str, Any]:
        return {name: list(names) for name, names in self.buckets.items()}


class _FakeSession:
    def __init__(self, runtime: "_FakeRuntime") -> None:
        self.runtime = runtime
        self.session_id = "sess-mint"
        self.report = _Report({"ready": [], "failed": [], "awaiting_auth": []})
        self.oauth: list[dict[str, str]] = []

    async def drain_init(self, **_kw: Any) -> None:
        await asyncio.sleep(0)

    def pop_pending_oauth_requests(self) -> list[dict[str, str]]:
        out, self.oauth = self.oauth, []
        return out

    def mcp_session_report(self) -> _Report:
        return self.report


class _Harness:
    opens_external_urls = True
    client_capabilities: dict[str, Any] = {}


class _FakeRuntime:
    """A KAS engine: real storage decision at spawn, scripted MCP frames after."""

    instances: list["_FakeRuntime"] = []
    #: The status the server reports when the session opens.
    initial = "awaiting_auth"
    consent_url = _AUTHORIZE

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self._mcp_grant_key = kwargs["mcp_grant_key"]
        self._harness = _Harness()
        self._mcp_secret_server: kss.SecretStorageServer | None = None
        self.pid = 515150 + len(_FakeRuntime.instances)
        self.alive = True
        self.kills: list[str] = []
        self.sign_ins: list[tuple[str, str]] = []
        self.session: _FakeSession | None = None
        _FakeRuntime.instances.append(self)

    async def spawn(self) -> None:
        await runtime_spawn_launch._with_mcp_sign_in_storage(self, {})

    async def create_session(self, **kwargs: Any) -> _FakeSession:
        self.session = _FakeSession(self)
        self.session.report.buckets[_FakeRuntime.initial].append(_SLUG)
        return self.session

    def begin_mcp_sign_in(self, session_id: str, server: str) -> bool:
        self.sign_ins.append((session_id, server))
        assert self.session is not None
        self.session.oauth.append({"serverName": server, "oauthUrl": _FakeRuntime.consent_url})
        return True

    async def consent(self) -> None:
        """What the engine does once the user consents: store tokens, connect."""
        assert self.session is not None
        if self._mcp_secret_server is not None:
            await self._mcp_secret_server.answer(
                kss.METHOD_SECRET_STORE, {"key": _TOKENS_KEY, "value": _TOKEN_VALUE}
            )
        self.session.report.buckets["awaiting_auth"].remove(_SLUG)
        self.session.report.buckets["ready"].append(_SLUG)

    def is_alive(self) -> bool:
        return self.alive

    async def kill(self, *, expected: bool = False, reason: str = "") -> None:
        self.kills.append(reason)
        self.alive = False


@pytest.fixture(autouse=True)
def _kas_engine(_floor_monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Persistence on, a scratch data home, the scripted engine, short read slices."""
    monkeypatch = _floor_monkeypatch
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    (home / "config.json").write_text(
        json.dumps({"agent": {"kas_persist_mcp_sign_in": True}}), encoding="utf-8"
    )
    principals.reset_for_tests()
    _FakeRuntime.instances.clear()
    _FakeRuntime.initial = "awaiting_auth"
    _FakeRuntime.consent_url = _AUTHORIZE
    monkeypatch.setattr(mcp_sign_in, "_runtime_factory", lambda: _FakeRuntime)
    monkeypatch.setattr(mcp_sign_in, "_DRAIN_SLICE_SECONDS", 0.01)
    yield home
    # A row a test leaves waiting still has its watcher; it must not outlive the test.
    for entry in list(mint._mints.values()):
        watcher = entry.get("watcher")
        if watcher is not None:
            watcher.cancel()
    principals.reset_for_tests()


async def _settled() -> dict:
    """The row once the start flow has handed off or ended."""
    await async_wait_until(
        lambda: (mint.pending_mint_for(_SLUG) or {}).get("state") not in (None, "minting"),
        timeout=5,
    )
    return mint.pending_mint_for(_SLUG) or {}


def _vault_server(home: Path, principal: str, key: str) -> kss.SecretStorageServer:
    principals.bind(key, principal)
    return kss.SecretStorageServer(principal, key, kss.McpGrantStore(home, principal))


# ── the grant lands where a later owner KAS chat reads it ─────────────────────


@pytest.mark.asyncio
async def test_a_settings_sign_in_is_visible_to_a_new_owner_kas_session(_kas_engine: Path):
    await mint_kas.start_kas_mint(_SLUG, "https://mcp.example.com/mcp")
    row = await _settled()
    assert row["state"] == "waiting"
    assert row["oauth_url"] == _AUTHORIZE
    assert row["store"] == mint_kas.STORE_KAS

    (engine,) = _FakeRuntime.instances
    assert engine.kwargs["acp_backend"] == ACP_BACKEND_KAS
    assert engine.kwargs["mcp_grant_key"] == mint_kas.mint_key(_SLUG)
    assert engine.sign_ins == [("sess-mint", _SLUG)]
    # No kiro-cli process was spawned for this sign-in.
    assert _FakeClient.instances == []

    await engine.consent()
    await async_wait_until(
        lambda: (mint.pending_mint_for(_SLUG) or {}).get("state") == "granted", timeout=5
    )
    # The mint process is released once the grant is kept.
    await async_wait_until(lambda: engine.kills, timeout=5)

    owner_chat = _vault_server(_kas_engine, principals.OWNER_PRINCIPAL, "chat-owner-later")
    got = await owner_chat.answer(kss.METHOD_SECRET_GET, {"key": _TOKENS_KEY})
    assert got == {"value": _TOKEN_VALUE}

    other_user = _vault_server(_kas_engine, "user:someone-else", "chat-other")
    assert await other_user.answer(kss.METHOD_SECRET_GET, {"key": _TOKENS_KEY}) == {}


@pytest.mark.asyncio
async def test_a_server_the_vault_already_signs_in_is_granted_without_consent():
    _FakeRuntime.initial = "ready"
    await mint_kas.start_kas_mint(_SLUG, "https://mcp.example.com/mcp")
    row = await _settled()
    assert _state_only(row) == {"state": "granted", "store": "kas"}
    (engine,) = _FakeRuntime.instances
    assert engine.sign_ins == []
    assert engine.kills


# ── fail closed ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_persistence_off_nothing_is_spawned(_kas_engine: Path):
    (_kas_engine / "config.json").write_text(json.dumps({"agent": {}}), encoding="utf-8")
    await mint_kas.start_kas_mint(_SLUG, "https://mcp.example.com/mcp")
    row = await _settled()
    assert _state_only(row) == {"state": "failed", "reason": "kas_sign_in_not_kept", "store": "kas"}
    assert _FakeRuntime.instances == []


@pytest.mark.asyncio
async def test_a_process_that_declared_no_storage_never_signs_in():
    # Another caller drove the reserved key: the handshake finds no single user.
    principals.conflict(mint_kas.mint_key(_SLUG), record_unbound=True)
    await mint_kas.start_kas_mint(_SLUG, "https://mcp.example.com/mcp")
    row = await _settled()
    assert row["state"] == "failed"
    assert row["reason"] == "kas_vault_unavailable"
    (engine,) = _FakeRuntime.instances
    assert engine._mcp_secret_server is None
    assert engine.sign_ins == []
    assert engine.kills


@pytest.mark.asyncio
async def test_a_binding_lost_before_completion_is_not_reported_granted(_kas_engine: Path):
    await mint_kas.start_kas_mint(_SLUG, "https://mcp.example.com/mcp")
    assert (await _settled())["state"] == "waiting"
    (engine,) = _FakeRuntime.instances
    principals.conflict(mint_kas.mint_key(_SLUG))
    await engine.consent()
    await async_wait_until(
        lambda: (mint.pending_mint_for(_SLUG) or {}).get("state") == "failed", timeout=5
    )
    assert (mint.pending_mint_for(_SLUG) or {})["reason"] == "kas_vault_unavailable"
    owner_chat = _vault_server(_kas_engine, principals.OWNER_PRINCIPAL, "chat-owner-later")
    assert await owner_chat.answer(kss.METHOD_SECRET_GET, {"key": _TOKENS_KEY}) == {}


@pytest.mark.asyncio
async def test_a_credential_bearing_consent_url_is_never_published():
    _FakeRuntime.consent_url = _CREDENTIAL_URL
    await mint_kas.start_kas_mint(_SLUG, "https://mcp.example.com/mcp")
    row = await _settled()
    assert row["state"] == "failed"
    assert row["reason"] == "mint_url_rejected"
    assert "oauth_url" not in row
    assert _FakeRuntime.instances[0].kills


@pytest.mark.asyncio
async def test_a_server_the_engine_never_reports_fails_and_releases_the_process():
    _FakeRuntime.initial = "failed"
    await mint_kas.start_kas_mint(_SLUG, "https://mcp.example.com/mcp")
    row = await _settled()
    assert row["reason"] == "mint_server_absent"
    assert _FakeRuntime.instances[0].kills


@pytest.mark.asyncio
async def test_cancel_releases_the_kas_process_and_its_spec(_isolated_mint: Path):
    await mint_kas.start_kas_mint(_SLUG, "https://mcp.example.com/mcp")
    assert (await _settled())["state"] == "waiting"
    assert any(p.name.startswith("kirocrew-mint-") for p in _isolated_mint.iterdir())
    assert await mint.cancel_mint(_SLUG)
    (engine,) = _FakeRuntime.instances
    assert engine.kills
    assert not any(p.name.startswith("kirocrew-mint-") for p in _isolated_mint.iterdir())


# ── the route picks the backend's path ────────────────────────────────────────


async def _client() -> TestClient:
    app = web.Application()
    app.router.add_post("/api/connections/mint", connections.api_connections_mint)
    app.router.add_get("/api/connections/mint", connections.api_connections_mint_state)
    as_owner(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("on_kas", "body", "expected"),
    [
        (True, {"slug": _SLUG, "surface": "settings"}, "kas"),
        (False, {"slug": _SLUG, "surface": "settings"}, "kiro-cli"),
        # A Connections card: its status and Disconnect read kiro-cli's cache.
        (True, {"slug": _SLUG}, "kiro-cli"),
        (False, {"slug": _SLUG}, "kiro-cli"),
    ],
)
async def test_the_mint_route_runs_the_backends_own_sign_in(
    monkeypatch: pytest.MonkeyPatch, on_kas: bool, body: dict, expected: str
):
    calls: list[str] = []

    def _recorder(name: str) -> Any:
        async def _start(slug: str, *_a: Any, **_kw: Any) -> None:
            calls.append(name)

        return _start

    monkeypatch.setattr(mint_kas, "kas_mint_applies", lambda: on_kas)
    monkeypatch.setattr(mint_kas, "start_kas_mint", _recorder("kas"))
    monkeypatch.setattr(mint, "start_oauth_mint", _recorder("kiro-cli"))
    monkeypatch.setattr(connections, "_oauth_client_configured", lambda provider: True)
    client = await _client()
    try:
        resp = await client.post("/api/connections/mint", json=body)
        assert resp.status == 200
        if connections._mint_tasks:
            await asyncio.gather(*list(connections._mint_tasks))
    finally:
        await client.close()
    assert calls == [expected]


@pytest.mark.asyncio
async def test_a_dead_engine_is_expired_and_released_without_a_poll(_isolated_mint: Path):
    await mint_kas.start_kas_mint(_SLUG, "https://mcp.example.com/mcp")
    assert (await _settled())["state"] == "waiting"
    (engine,) = _FakeRuntime.instances
    engine.alive = False
    await async_wait_until(
        lambda: (mint.pending_mint_for(_SLUG) or {}).get("state") == "expired", timeout=5
    )
    assert (mint.pending_mint_for(_SLUG) or {})["reason"] == "mint_process_gone"
    await async_wait_until(
        lambda: not any(p.name.startswith("kirocrew-mint-") for p in _isolated_mint.iterdir()),
        timeout=5,
    )


@pytest.mark.asyncio
async def test_the_state_poll_names_the_kas_store():
    await mint_kas.start_kas_mint(_SLUG, "https://mcp.example.com/mcp")
    await _settled()
    client = await _client()
    try:
        resp = await client.get("/api/connections/mint", params={"slug": _SLUG})
        body = await resp.json()
    finally:
        await client.close()
    assert body["state"] == "waiting"
    assert body["store"] == "kas"


@pytest.mark.parametrize(
    ("backend", "persist", "expected"),
    [
        (ACP_BACKEND_KAS, True, True),
        # Persistence off: a KAS sign-in would die with the mint process, so the
        # button keeps the kiro-cli mint it ran before this path existed.
        (ACP_BACKEND_KAS, False, False),
        (ACP_BACKEND_KIRO, True, False),
    ],
)
def test_only_kas_with_persistence_on_takes_the_kas_path(
    monkeypatch: pytest.MonkeyPatch, backend: str, persist: bool, expected: bool
):
    # The backend is pinned rather than read from a config file: whether this
    # host can serve KAS decides what an empty choice resolves to.
    config = SimpleNamespace(agent=SimpleNamespace(acp_backend=backend))
    monkeypatch.setattr("kiro_crew.config.loader.KiroCrewConfig.load", lambda: config)
    monkeypatch.setattr(kss, "persistence_enabled", lambda: persist)
    assert _kas_mint_applies() is expected


def test_an_unreadable_config_keeps_the_kiro_cli_path(monkeypatch: pytest.MonkeyPatch):
    def _boom() -> Any:
        raise OSError("config unreadable")

    monkeypatch.setattr("kiro_crew.config.loader.KiroCrewConfig.load", _boom)
    assert _kas_mint_applies() is False
