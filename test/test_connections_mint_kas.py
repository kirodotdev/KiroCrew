"""A Settings or Connections sign-in while chats run on KAS lands in Crew's grant vault.

A KAS chat reads a remote server's tokens from the gateway-wide vault
(``acp/kas_secret_storage.py``). The sign-in behind Settings -> MCP -> Sign in and
the Connections card is ``start_oauth_mint``; when it ran kiro-cli its grant went
to kiro-cli's cache, which no KAS chat reads, so the server stayed signed out in
every KAS chat. These tests drive ``start_oauth_mint`` with the engine process
replaced by a fake that plays the engine's half of the sign-in against Crew's
real secret store, and check where the sign-in ends up.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import types
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew import mcp_grant, mcp_grant_kas, session
from kiro_crew.acp import kas_secret_storage as kss
from kiro_crew.config.paths import data_home
from kiro_crew.connections import mint, status
from kiro_crew.connections.registry import get_provider
from kiro_crew.dashboard.handlers import connections
from kiro_crew.secrets import SecretVault
from kiro_crew.testing.wait import async_wait_until

_SERVER = "docsServer"
_URL = "https://mcp.docs.example/mcp"
_AUTHORIZE = (
    "https://auth.docs.example/authorize?client_id=abc"
    "&redirect_uri=http%3A%2F%2F127.0.0.1%3A43123%2Fcallback&state=opaque"
)
_TOKENS = '{"access_token": "at-1", "refresh_token": "rt-1"}'
#: A consent URL with a fixed credential in its query, which no allowlist clears.
_TAINTED = "https://auth.docs.example/authorize?client_id=abc&access_token=AKIAIOSFODNN7EXAMPLE"


class _Handle:
    """The mint session on the fake engine: its MCP report and its URL frames."""

    def __init__(self, engine: "_Engine") -> None:
        self.engine = engine
        self.session_id = "kas-session-1"
        self.buckets: dict[str, list[str]] = {"ready": [], "awaiting_auth": [], "failed": []}
        self.pending: list[dict[str, str]] = []
        self.destroyed = False

    async def drain_init(self, **_kw: Any) -> None:
        await asyncio.sleep(0)

    def mcp_session_report(self) -> "_Handle":
        return self

    def payload(self) -> dict[str, list[str]]:
        return {name: list(names) for name, names in self.buckets.items()}

    def pop_pending_oauth_requests(self) -> list[dict[str, str]]:
        out, self.pending = self.pending, []
        return out

    def place(self, bucket: str) -> None:
        for names in self.buckets.values():
            if _SERVER in names:
                names.remove(_SERVER)
        self.buckets[bucket].append(_SERVER)

    async def destroy(self) -> None:
        self.destroyed = True


class _Engine:
    """Stands in for ``AcpRuntime`` on KAS: spawn, one session, the sign-in.

    ``consent`` is the user's browser reaching the engine's callback: the engine
    swaps the code and keeps the tokens through Crew's secret store, exactly the
    ``_kiro/secret/store`` the handshake's storage server answers.
    """

    instances: list["_Engine"] = []
    starts_connected = False
    #: The engine kept tokens a refresh could not use: the server reports failed.
    starts_failed = False
    #: The consent URL the engine opens as ``_kiro/openExternalUrl``.
    oauth_url = _AUTHORIZE

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.pid: int | None = None
        self.alive = True
        self.handle: _Handle | None = None
        self.signing_in = False
        self.kills = 0
        #: How often the sign-in's state was asked for: once per watcher poll.
        self.sign_in_checks = 0
        _Engine.instances.append(self)

    async def spawn(self, **_kw: Any) -> None:
        self.pid = 515150 + len(_Engine.instances)

    async def create_session(self, **kwargs: Any) -> _Handle:
        self.session_kwargs = kwargs
        self.handle = _Handle(self)
        if _Engine.starts_failed:
            self.handle.place("failed")
        else:
            self.handle.place("ready" if _Engine.starts_connected else "awaiting_auth")
        return self.handle

    def is_alive(self) -> bool:
        return self.alive

    def begin_mcp_sign_in(self, session_id: str, server_name: str) -> bool:
        assert self.handle is not None and session_id == self.handle.session_id
        self.signing_in = True
        self.handle.pending.append({"serverName": server_name, "oauthUrl": self.oauth_url})
        return True

    def mcp_sign_in_holds(self, session_id: str, server_name: str) -> bool:
        self.sign_in_checks += 1
        return self.signing_in

    async def consent(self) -> None:
        await kss.SecretStorageServer().answer(
            kss.METHOD_SECRET_STORE,
            {"key": mcp_grant_kas.kas_tokens_entry_name(_URL), "value": _TOKENS},
        )
        assert self.handle is not None
        self.handle.place("ready")
        self.signing_in = False

    def give_up(self) -> None:
        """The engine's connect attempt timed out with no consent: it reports ``failed``."""
        assert self.handle is not None
        self.handle.place("failed")
        self.signing_in = False

    def answer_reset(self) -> None:
        """The engine answers the reset at its 60 s ceiling while the connect still runs."""
        self.signing_in = False

    async def kill(self, **_kw: Any) -> None:
        self.alive = False
        self.kills += 1


class _KiroCli:
    """Stands in for the kiro-cli mint client; its consent writes kiro-cli's cache pair."""

    instances: list["_KiroCli"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self._pid = 616160 + len(_KiroCli.instances)
        self.requests = [{"serverName": _SERVER, "oauthUrl": _AUTHORIZE}]
        _KiroCli.instances.append(self)

    async def ensure_ready(self) -> None:
        return None

    def pop_pending_oauth_requests(self) -> list[dict[str, str]]:
        out, self.requests = self.requests, []
        return out

    def is_process_alive(self) -> bool:
        return True

    async def consent(self) -> None:
        _write_kiro_cli_grant(_URL)

    async def shutdown(self) -> None:
        return None


def _write_kiro_cli_grant(url: str) -> None:
    """kiro-cli's cache pair for *url*, as its mint writes it on consent."""
    cache = mcp_grant.kiro_oauth_cache_dir()
    key = mcp_grant.grant_key(url)
    (cache / f"{key}.token.json").write_text("{}", encoding="utf-8")
    (cache / f"{key}.registration.json").write_text("{}", encoding="utf-8")


@pytest.fixture(autouse=True)
def _kas_chats(_floor_monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Chats on KAS with sign-in persistence on, an empty mint table, no real spawn.

    Patched through the floor's own ``MonkeyPatch`` (D11), so a test that calls
    ``monkeypatch.undo()`` cannot lift it and reach the real agents dir or a real
    engine. A test's own ``monkeypatch`` still overrides any of these.
    """
    mp = _floor_monkeypatch
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "kirocrew.json").write_text(
        json.dumps({"mcpServers": {_SERVER: {"url": _URL}}}), encoding="utf-8"
    )
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    mp.setattr("kiro_crew.agent.kiro_agents_dir_path", lambda: agents_dir)
    mp.setattr(mcp_grant, "kiro_oauth_cache_dir", lambda **kw: cache_dir)
    mp.setattr(mint, "_mint_manifest_path", lambda: tmp_path / "mint-specs.json")
    mp.setattr(mint, "_mints", {})
    mp.setattr(mint, "_mints_lock", asyncio.Lock())
    mp.setattr(mint, "_MINT_GRANT_POLL_SECONDS", 0.0)
    live_pids: set[int] = set()
    mp.setattr(mint, "register_protected_pid", live_pids.add)
    mp.setattr(mint, "unregister_protected_pid", live_pids.discard)
    mp.setattr(status, "chat_backend_is_kas", lambda: True)
    mp.setattr(mcp_grant_kas, "persistence_enabled", lambda: True)
    _Engine.instances.clear()
    _Engine.starts_connected = False
    _Engine.starts_failed = False
    _KiroCli.instances.clear()
    mp.setattr(session, "_load_bg_runtime_types", lambda: (_Engine, RuntimeError))
    mp.setattr(mint, "_acp_client_factory", lambda: _KiroCli)
    return agents_dir


def _row() -> dict[str, Any]:
    return mint.pending_mint_for(_SERVER) or {}


async def _until_state(*states: str) -> dict[str, Any]:
    await async_wait_until(lambda: _row().get("state") in states, describe=_row)
    return _row()


async def _sign_in() -> None:
    """Start the Settings sign-in for the owner server, as the dashboard does."""
    await mint.start_oauth_mint(_SERVER, _URL, pinned={"url": _URL})


async def _consent() -> None:
    """The user consents on whichever process published the URL."""
    holder = (_Engine.instances or _KiroCli.instances)[-1]
    await holder.consent()


def _saved_tokens() -> str | None:
    found = kss.default_grant_store().get(mcp_grant_kas.kas_tokens_entry_name(_URL))
    return None if found is None else found[0]


@pytest.mark.asyncio
async def test_a_kas_sign_in_saves_its_tokens_where_a_kas_chat_reads_them():
    await _sign_in()
    waiting = await _until_state("waiting", "failed")
    assert waiting.get("oauth_url") == _AUTHORIZE, waiting

    await _consent()
    granted = await _until_state("granted", "expired", "failed")

    assert granted["state"] == "granted"
    # In the gateway-wide vault the KAS secret store answers from, under the key
    # the engine derives from the url.
    assert await asyncio.to_thread(mcp_grant_kas.kas_grant_presence, _URL) is True
    # A later KAS chat's engine asks for it with ``_kiro/secret/get`` and gets it.
    answer = await kss.SecretStorageServer().answer(
        kss.METHOD_SECRET_GET, {"key": mcp_grant_kas.kas_tokens_entry_name(_URL)}
    )
    assert answer == {"value": _TOKENS}
    # Nothing landed in kiro-cli's cache, which a KAS chat never reads.
    assert await asyncio.to_thread(mcp_grant.grant_presence, _URL) is False


@pytest.mark.asyncio
async def test_the_sign_in_runs_on_one_kas_engine_mounting_only_its_server():
    await _sign_in()
    await _until_state("waiting")

    [engine] = _Engine.instances
    assert engine.kwargs["acp_backend"] == "kas"
    assert engine.session_kwargs["mcp_servers"] == []
    spec = json.loads(Path(_row_spec_path()).read_text(encoding="utf-8"))
    assert spec["mcpServers"] == {_SERVER: {"url": _URL}}
    assert _KiroCli.instances == []

    await _consent()
    await _until_state("granted")
    # The process held for the consent is released once the sign-in is saved.
    assert engine.kills == 1 and engine.handle is not None and engine.handle.destroyed


def _row_spec_path() -> str:
    return str(mint._mints[_SERVER].get("spec_path") or "")


@pytest.mark.asyncio
async def test_a_server_the_vault_already_signs_in_is_granted_with_no_url():
    _Engine.starts_connected = True

    await _sign_in()
    row = await _until_state("granted", "waiting", "failed")

    assert row["state"] == "granted" and "oauth_url" not in row
    assert _Engine.instances[0].kills == 1


@pytest.mark.asyncio
async def test_a_sign_in_the_engine_ends_without_consent_expires_the_row():
    await _sign_in()
    await _until_state("waiting")

    _Engine.instances[0].give_up()
    row = await _until_state("expired", "granted")

    # No reason: the card reads a reasonless expiry as the sign-in window running
    # out ("Sign-in timed out"); the URL it showed was real.
    assert row == {"state": "expired", "token": row["token"]}
    assert _saved_tokens() is None


@pytest.mark.asyncio
async def test_a_reset_answered_before_consent_keeps_the_sign_in_open():
    # The engine answers the reset 60 s in while its callback listener runs on to
    # the server's timeout; the consent URL keeps the server ``awaiting_auth``.
    await _sign_in()
    await _until_state("waiting")
    [engine] = _Engine.instances
    engine.answer_reset()
    polled = engine.sign_in_checks
    await async_wait_until(
        lambda: engine.sign_in_checks >= polled + 5 or _row().get("state") != "waiting",
        describe=_row,
    )

    assert _row()["state"] == "waiting", _row()
    assert engine.kills == 0 and engine.handle is not None and not engine.handle.destroyed

    await _consent()
    granted = await _until_state("granted", "expired", "failed")

    assert granted["state"] == "granted"
    assert _saved_tokens() == _TOKENS


@pytest.mark.asyncio
async def test_a_consent_url_carrying_a_credential_is_never_published(monkeypatch, caplog):
    # The gate a kiro-cli URL passes: one fresh engine, then the rejection is final.
    monkeypatch.setattr(_Engine, "oauth_url", _TAINTED)
    logged: list[str] = []
    monkeypatch.setattr(
        mint,
        "_log_mint_outcome",
        lambda slug, outcome, detail: logged.append(f"{outcome} {detail}"),
    )

    with caplog.at_level("WARNING"):
        await _sign_in()
        row = await _until_state("failed", "waiting", "granted", "expired")

    # A fixed credential is a rejection no allowlist clears, so no endpoint is named.
    assert row == {"state": "failed", "reason": "mint_url_rejected", "token": row["token"]}
    assert "oauth_url" not in mint._mints[_SERVER]
    assert len(_Engine.instances) == 2
    assert [engine.kills for engine in _Engine.instances] == [1, 1]
    assert logged == ["error reason=mint_url_rejected route=kas"]
    assert "AKIAIOSFODNN7EXAMPLE" not in caplog.text


@pytest.mark.asyncio
async def test_stale_vault_tokens_do_not_grant_a_sign_in_before_consent():
    # Tokens the engine could not use are already saved, so their presence proves
    # nothing: the row waits until the session reports the server connected.
    await kss.McpGrantStore(data_home()).store(mcp_grant_kas.kas_tokens_entry_name(_URL), "{}")

    await _sign_in()
    [engine] = _Engine.instances
    polled = engine.sign_in_checks
    # Five watcher polls with the user not yet consented, or an early verdict.
    await async_wait_until(
        lambda: engine.sign_in_checks >= polled + 5 or _row().get("state") != "waiting",
        describe=_row,
    )

    row = _row()
    assert row["state"] == "waiting" and row["oauth_url"] == _AUTHORIZE, row
    # The engine holding the callback listener is still up for the consent.
    assert engine.kills == 0 and engine.handle is not None and not engine.handle.destroyed

    await _consent()
    granted = await _until_state("granted", "expired", "failed")

    assert granted["state"] == "granted"
    assert _saved_tokens() == _TOKENS
    assert engine.kills == 1


@pytest.mark.asyncio
async def test_connect_replaces_saved_tokens_the_engine_failed_to_refresh():
    # The vendor refused the refresh with an error the engine keeps the tokens on,
    # so the server reports failed. Connect must sign in again, not fail the row.
    _Engine.starts_failed = True
    await kss.McpGrantStore(data_home()).store(mcp_grant_kas.kas_tokens_entry_name(_URL), "{}")

    await _sign_in()
    row = await _until_state("waiting", "failed", "granted")

    assert row["state"] == "waiting" and row["oauth_url"] == _AUTHORIZE, row
    await _consent()
    granted = await _until_state("granted", "expired", "failed")
    assert granted["state"] == "granted"
    assert _saved_tokens() == _TOKENS


@pytest.mark.asyncio
async def test_a_failed_server_with_no_saved_tokens_fails_the_row():
    _Engine.starts_failed = True

    await _sign_in()
    row = await _until_state("failed", "waiting", "granted")

    assert row["state"] == "failed" and row.get("reason") == "mint_server_failed", row
    assert not _Engine.instances[0].signing_in


@pytest.mark.asyncio
async def test_on_kiro_cli_chats_the_sign_in_stays_on_kiro_cli(monkeypatch):
    monkeypatch.setattr(status, "chat_backend_is_kas", lambda: False)

    await _sign_in()
    await _until_state("waiting")
    await _consent()
    row = await _until_state("granted")

    assert row["state"] == "granted"
    assert _Engine.instances == [] and len(_KiroCli.instances) == 1
    assert await asyncio.to_thread(mcp_grant.grant_presence, _URL) is True
    assert _saved_tokens() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entry",
    [
        {"url": _URL, "oauth": {"clientId": "operator-app"}},
        {"url": _URL, "headers": {"X-Tenant": "a"}},
    ],
)
async def test_an_entry_the_engine_is_not_sent_bare_keeps_the_kiro_cli_mint(entry):
    await mint.start_oauth_mint(_SERVER, _URL, pinned=entry)
    await _until_state("waiting")

    assert _Engine.instances == [] and len(_KiroCli.instances) == 1


@pytest.mark.asyncio
async def test_with_persistence_off_the_sign_in_stays_on_kiro_cli(monkeypatch):
    monkeypatch.setattr(mcp_grant_kas, "persistence_enabled", lambda: False)

    await _sign_in()
    await _until_state("waiting")

    assert _Engine.instances == [] and len(_KiroCli.instances) == 1


# ── the Connections card ──


def _notion() -> dict[str, Any]:
    provider = get_provider("notion")
    assert provider is not None
    return dict(provider)


async def _connect_notion(monkeypatch: pytest.MonkeyPatch) -> tuple[int, dict, list, list]:
    """POST the card's Connect for notion with a warm URL on offer.

    Returns the status, the body, the slugs that adopted the warm URL and the slugs
    whose cold mint started.
    """
    from kiro_crew.connections import warm

    adopted: list[str] = []
    started: list[str] = []

    async def _adopt(slug: str, url: str) -> str:
        adopted.append(slug)
        return "warm-token"

    async def _start(slug, mcp_url, token=None, prior=None, pinned=None) -> None:
        started.append(slug)

    monkeypatch.setattr(connections, "_oauth_client_configured", lambda provider: True)
    monkeypatch.setattr(warm, "adopt_shared_mint", _adopt)
    monkeypatch.setattr(mint, "start_oauth_mint", _start)
    app = web.Application()
    app.router.add_post("/api/connections/mint", connections.api_connections_mint)
    as_owner(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post("/api/connections/mint", json={"slug": "notion"})
        body = await resp.json()
    finally:
        await client.close()
    return resp.status, body, adopted, started


@pytest.mark.asyncio
async def test_connect_on_kas_never_adopts_a_kiro_cli_warm_url(monkeypatch, _kas_chats):
    # The card writes the provider's entry before it asks for the mint.
    _install_notion(_kas_chats)

    status_code, body, adopted, started = await _connect_notion(monkeypatch)

    assert status_code == 200 and body["state"] == "minting", body
    assert adopted == []
    await async_wait_until(lambda: started == ["notion"], describe=lambda: started)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entry",
    [
        pytest.param({"headers": {"X-Tenant": "a"}}, id="entry-with-headers"),
        pytest.param(None, id="no-main-spec-entry"),
    ],
)
async def test_a_connect_whose_sign_in_stays_on_kiro_cli_still_adopts_its_warm_url(
    monkeypatch, _kas_chats, entry
):
    # The mint runs such a sign-in on kiro-cli (kas_sign_in_entry), so the warm URL
    # kiro-cli already holds for it is the one to hand the card.
    if entry is not None:
        (_kas_chats / "kirocrew.json").write_text(
            json.dumps({"mcpServers": {"notion": {"url": _notion()["mcp_url"], **entry}}}),
            encoding="utf-8",
        )

    status_code, body, adopted, started = await _connect_notion(monkeypatch)

    assert status_code == 200, body
    assert (body["state"], body["token"]) == ("waiting", "warm-token")
    assert adopted == ["notion"] and started == []


def test_the_premint_warms_only_the_providers_whose_connect_stays_on_kiro_cli(
    monkeypatch, _kas_chats
):
    from kiro_crew.connections import mint_kas

    (_kas_chats / "kirocrew.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "notion": {"url": _notion()["mcp_url"], "headers": {"X-Tenant": "a"}},
                    "linear": {"url": str(get_provider("linear")["mcp_url"])},
                }
            }
        ),
        encoding="utf-8",
    )
    # notion keeps kiro-cli through its headers and github is pre-registered.
    # linear's entry is sent bare, and figma's Connect writes its bare url first.
    providers = [dict(get_provider(slug)) for slug in ("notion", "linear", "github", "figma")]

    kept = mint_kas.kiro_cli_sign_in_providers(providers)

    assert [p["slug"] for p in kept] == ["notion", "github"]
    monkeypatch.setattr(status, "chat_backend_is_kas", lambda: False)
    assert mint_kas.kiro_cli_sign_in_providers(providers) == providers


@pytest.mark.asyncio
async def test_the_card_reads_a_vault_sign_in_as_connected_with_no_kas_caveat(monkeypatch):
    monkeypatch.setattr(status, "get_visible_providers", lambda: [_notion()])
    monkeypatch.setattr(status, "reconcile_connected_since", lambda statuses, now: {})
    await kss.McpGrantStore(data_home()).store(
        mcp_grant_kas.kas_tokens_entry_name(_notion()["mcp_url"]), _TOKENS
    )

    [notion] = await status.collect_connection_statuses()

    assert notion["status"] == status.STATUS_CONNECTED and notion["grantPresent"] is True
    assert "signInInChat" not in notion


@pytest.mark.asyncio
async def test_on_kiro_cli_chats_a_grant_only_the_kas_vault_holds_is_not_connected(monkeypatch):
    # A Kiro CLI chat's handshake declares no secret storage, so it never reads the
    # vault a Connect made while chats ran on KAS saved its sign-in in. The card
    # answers for the chats there are, so it offers Connect, which signs in on
    # kiro-cli, rather than a Connected badge no chat can use.
    monkeypatch.setattr(status, "chat_backend_is_kas", lambda: False)
    monkeypatch.setattr(status, "get_visible_providers", lambda: [_notion()])
    monkeypatch.setattr(status, "reconcile_connected_since", lambda statuses, now: {})
    await kss.McpGrantStore(data_home()).store(
        mcp_grant_kas.kas_tokens_entry_name(_notion()["mcp_url"]), _TOKENS
    )

    [notion] = await status.collect_connection_statuses()

    assert notion["status"] == status.STATUS_NOT_CONNECTED, notion
    assert notion["grantPresent"] is False


def _install_notion(agents_dir: Path) -> None:
    """The main-spec entry the card's Connect installs for notion."""
    (agents_dir / "kirocrew.json").write_text(
        json.dumps({"mcpServers": {"notion": {"url": _notion()["mcp_url"]}}}), encoding="utf-8"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("on_kas", "crew_held"), [(True, True), (False, None)])
async def test_a_provider_that_signs_in_on_kas_is_offered_no_test(
    monkeypatch, _kas_chats, on_kas, crew_held
):
    # Test enumerates through kiro-cli's grant, which a Connect on the KAS engine
    # never writes; the card offers Test only on a row without crewHeld. Each row
    # holds the grant its backend's Connect writes: the vault on KAS chats,
    # kiro-cli's cache pair on Kiro CLI chats.
    _install_notion(_kas_chats)
    monkeypatch.setattr(status, "chat_backend_is_kas", lambda: on_kas)
    monkeypatch.setattr(status, "get_visible_providers", lambda: [_notion()])
    monkeypatch.setattr(status, "reconcile_connected_since", lambda statuses, now: {})
    if on_kas:
        await kss.McpGrantStore(data_home()).store(
            mcp_grant_kas.kas_tokens_entry_name(_notion()["mcp_url"]), _TOKENS
        )
    else:
        _write_kiro_cli_grant(_notion()["mcp_url"])

    [notion] = await status.collect_connection_statuses()

    assert notion["status"] == status.STATUS_CONNECTED
    assert notion.get("crewHeld") is crew_held


@pytest.mark.asyncio
async def test_test_is_refused_for_a_provider_that_signs_in_on_kas(monkeypatch, _kas_chats):
    from kiro_crew.connections import tool_test

    enumerated: list[str] = []

    async def _enumerate(provider: dict[str, Any]) -> dict[str, Any]:
        enumerated.append(str(provider["slug"]))
        return {"schema_version": 1, "slug": provider["slug"], "verdict": "usable"}

    _install_notion(_kas_chats)
    monkeypatch.setattr(tool_test, "test_connection_tools", _enumerate)
    app = web.Application()
    app.router.add_post("/api/connections/test", connections.api_connections_test)
    as_owner(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        refused = await client.post("/api/connections/test", json={"slug": "notion"})
        refused_body = await refused.json()
        # On kiro-cli chats the same Connect signs in on kiro-cli, so Test runs.
        monkeypatch.setattr(status, "chat_backend_is_kas", lambda: False)
        ran = await client.post("/api/connections/test", json={"slug": "notion"})
        ran_body = await ran.json()
    finally:
        await client.close()

    assert refused.status == 409, refused_body
    assert refused_body["code"] == "kas_sign_in_test_unsupported"
    assert ran.status == 200 and ran_body["verdict"] == "usable", ran_body
    assert enumerated == ["notion"]


@pytest.mark.asyncio
async def test_disconnect_removes_the_saved_kas_sign_in(monkeypatch):
    from kiro_crew.connections.ownership import DisconnectScope

    url = _notion()["mcp_url"]

    async def _remove(slug, mcp_url, project_dirs):
        return DisconnectScope(
            entry_removed=True,
            grant_removed=(),
            grant_shared_with=(),
            census_incomplete=False,
            census_unreadable=(),
            attempted_urls=(url,),
            agent_spec_rewritten=False,
            kas_sign_in_urls=(url,),
            kas_sign_ins_removed=1,
        )

    monkeypatch.setattr(connections, "remove_provider_entry", _remove)
    app = web.Application()
    app.router.add_post("/api/connections/disconnect", connections.api_connections_disconnect)
    as_owner(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post("/api/connections/disconnect", json={"slug": "notion"})
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 200, body
    assert body["grantRemoved"] is True and body["grantSurviving"] == []


@contextlib.asynccontextmanager
async def _no_mcp_lock():
    yield


async def _vault_names() -> set[str]:
    root = mcp_grant_kas.grant_store_root(data_home())
    return await asyncio.to_thread(lambda: set(SecretVault(root).list_names()))


async def _disconnect_notion_beside(
    monkeypatch: pytest.MonkeyPatch,
    other: str,
    other_url: str,
    *,
    label: str = "kirocrew",
    notion_url: str | None = None,
    saved: bool = True,
    unreadable: tuple[str, ...] = (),
    refuse_purge: bool = False,
    lock: Any = None,
) -> dict[str, Any]:
    """Disconnect notion while *other* stays configured at *other_url*; the response body.

    The real census and judge (``remove_provider_entry``) run over notion's scope
    (its entry at *notion_url*, the registry's by default), *other* under *label* and
    the *unreadable* sources, with the MCP lock, the purge (refused as a foreign data
    home's when *refuse_purge*), the rebuild and the warm re-arm stubbed. The vault
    holds the saved sign-in of notion's entry when *saved*.
    """
    from kiro_crew import agent as agent_mod
    from kiro_crew import mcp_discovery
    from kiro_crew.connections import ownership, warm
    from kiro_crew.dashboard.handlers import mcp as mcp_handlers

    url = notion_url or _notion()["mcp_url"]
    store = kss.McpGrantStore(data_home())
    for name in mcp_grant_kas.kas_saved_sign_in_names(url) if saved else ():
        await store.store(name, "{}")
    inventory = [
        types.SimpleNamespace(name="notion", url=url),
        types.SimpleNamespace(name=other, url=other_url),
    ]
    census: dict[str, dict[str, Any]] = {"kirocrew": {"notion": {"url": url}}}
    census.setdefault(label, {})[other] = {"url": other_url}

    async def _no_mint(_slug: str, _token: object) -> bool:
        return False

    def _purge(_name: str, *, scopes: object = None) -> dict:
        if refuse_purge:
            raise agent_mod.SharedAgentHomeRefused("foreign data home owns the shared agent specs")
        return {}

    monkeypatch.setattr(mint, "cancel_mint", _no_mint)
    monkeypatch.setattr(mcp_discovery, "list_servers", lambda: list(inventory))
    monkeypatch.setattr(ownership, "spec_census", lambda _dirs=(): (census, unreadable))
    monkeypatch.setattr(mcp_handlers, "_get_mcp_lock", lock or _no_mcp_lock)
    monkeypatch.setattr(mcp_handlers, "_purge_server_config", _purge)
    monkeypatch.setattr(
        agent_mod, "rebuild_agent_config_reporting", lambda: (Path("/dev/null"), True)
    )
    monkeypatch.setattr(warm, "rearm_invalidated_provider", lambda _slug: None)
    app = web.Application()
    app.router.add_post("/api/connections/disconnect", connections.api_connections_disconnect)
    as_owner(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post("/api/connections/disconnect", json={"slug": "notion"})
        body = await resp.json()
    finally:
        await client.close()
    assert resp.status == 200, body
    return body


@pytest.mark.asyncio
async def test_disconnect_keeps_a_kas_sign_in_another_configured_entry_still_holds(monkeypatch):
    # kiro-cli keys its pair by the url with the path's case kept
    # (mcp_grant.grant_key), the KAS vault by the whole url lowercased
    # (mcp_grant_kas.kas_connection_hash). An entry at notion's url with an upper-case
    # path shares no kiro-cli pair with notion, so the census attempts notion's own
    # pair, yet both entries' KAS chats read the one saved sign-in.
    url = _notion()["mcp_url"]
    assert url == "https://mcp.notion.com/mcp"
    variant = "https://mcp.notion.com/MCP"
    assert mcp_grant.grant_key(variant) != mcp_grant.grant_key(url)
    assert mcp_grant_kas.kas_saved_sign_in_names(variant) == (
        mcp_grant_kas.kas_saved_sign_in_names(url)
    )

    body = await _disconnect_notion_beside(monkeypatch, "notion-team", variant)

    names = await _vault_names()
    # notion-team stays configured, so the sign-in its KAS chats read stays saved.
    assert set(mcp_grant_kas.kas_saved_sign_in_names(variant)) <= names, (body, names)
    # A deliberate keep: named as a kiro-cli sharer is, never a failed removal.
    assert body["grantSharedWith"] == ["notion-team"], body
    assert body["grantSurviving"] == [], body


@pytest.mark.asyncio
async def test_disconnect_removes_a_kas_sign_in_an_entry_sharing_only_kiro_clis_pair_never_reads(
    monkeypatch,
):
    # grant_key drops the query, so an entry at notion's url with a query shares
    # notion's kiro-cli pair and keeps it. The vault keys the whole url, query kept,
    # so that entry's KAS chats read a sign-in of their own and never notion's.
    url = _notion()["mcp_url"]
    variant = f"{url}?workspace=x"
    assert mcp_grant.grant_key(variant) == mcp_grant.grant_key(url)
    assert mcp_grant_kas.kas_connection_hash(variant) != mcp_grant_kas.kas_connection_hash(url)
    _write_kiro_cli_grant(url)

    body = await _disconnect_notion_beside(monkeypatch, "notion-x", variant)

    names = await _vault_names()
    assert names.isdisjoint(mcp_grant_kas.kas_saved_sign_in_names(url)), (body, names)
    assert mcp_grant.grant_presence(url) is True
    assert body["grantSharedWith"] == ["notion-x"], body
    assert body["grantRemoved"] is True and body["grantSurviving"] == [], body


@pytest.mark.asyncio
async def test_disconnect_names_no_kas_holder_while_the_vault_holds_no_sign_in(monkeypatch):
    # Nothing was kept for notion-team, so the card must not say a sign-in was.
    body = await _disconnect_notion_beside(
        monkeypatch, "notion-team", "https://mcp.notion.com/MCP", saved=False
    )

    assert body["grantSharedWith"] == [] and body["grantSurviving"] == [], body


@pytest.mark.asyncio
async def test_disconnect_keeps_every_kas_sign_in_on_a_census_gap(monkeypatch):
    # An unreadable source could hide an entry that reads notion's saved sign-in.
    body = await _disconnect_notion_beside(
        monkeypatch, "linear", str(get_provider("linear")["mcp_url"]), unreadable=("agent:x.json",)
    )

    names = await _vault_names()
    assert set(mcp_grant_kas.kas_saved_sign_in_names(_notion()["mcp_url"])) <= names, body
    assert body["grantCensusIncomplete"] is True and body["grantSurviving"] == [], body


@pytest.mark.asyncio
async def test_a_refused_purge_keeps_the_kas_sign_in_its_surviving_mirror_reads(monkeypatch):
    # The purge would strip the mirrored notion entry; refused, it survives and its
    # KAS chats still read notion's saved sign-in.
    url = _notion()["mcp_url"]
    body = await _disconnect_notion_beside(
        monkeypatch, "notion", url, label="mirror:kirocrew.json", refuse_purge=True
    )

    names = await _vault_names()
    assert set(mcp_grant_kas.kas_saved_sign_in_names(url)) <= names, body
    assert body["entryRemoved"] is False and body["grantSurviving"] == [], body


@pytest.mark.asyncio
async def test_disconnect_removes_the_kas_sign_in_of_an_owned_spelling_kiro_cli_folds(monkeypatch):
    # grant_key folds the explicit default port into notion's own pair, but the vault
    # keys the url as written, so that entry's KAS chats saved a sign-in of their own.
    spelled = "https://mcp.notion.com:443/mcp"
    assert mcp_grant.grant_key(spelled) == mcp_grant.grant_key(_notion()["mcp_url"])
    body = await _disconnect_notion_beside(
        monkeypatch, "linear", str(get_provider("linear")["mcp_url"]), notion_url=spelled
    )

    names = await _vault_names()
    assert names.isdisjoint(mcp_grant_kas.kas_saved_sign_in_names(spelled)), (body, names)
    assert body["grantRemoved"] is True and body["grantSurviving"] == [], body


def test_a_holder_url_the_engine_hash_is_not_mirrored_for_holds_every_kas_sign_in():
    from kiro_crew.connections.ownership import _kas_sign_in_split

    url = _notion()["mcp_url"]
    other = "https://mcp.example/mcp"
    # An entry with no url (a stdio server) reads no saved sign-in.
    assert _kas_sign_in_split((url, other), (("local", None),)) == ((url, other), {})
    # One spelling per identity: the upper-case path is the same saved sign-in.
    assert _kas_sign_in_split((url, url.upper()), ()) == ((url,), {})
    for odd in ("https://mcp.notion.com/mcp?ws=\u00e9", 42):
        assert _kas_sign_in_split((url, other), (("odd", odd),)) == (
            (),
            {url: {"odd"}, other: {"odd"}},
        )


@pytest.mark.asyncio
async def test_a_vault_sign_in_an_entry_with_headers_cannot_use_does_not_read_connected(
    monkeypatch, _kas_chats
):
    # An entry carrying headers reaches a KAS chat through Crew's stdio HTTP bridge
    # (kas_agents, kas_env_launch.relay_withheld_headers), which forwards the
    # configured headers and never reads the vault's tokens; kiro-cli holds no pair.
    url = _notion()["mcp_url"]
    (_kas_chats / "kirocrew.json").write_text(
        json.dumps(
            {"mcpServers": {"notion": {"url": url, "headers": {"X-Notion-Workspace": "team"}}}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(status, "get_visible_providers", lambda: [_notion()])
    monkeypatch.setattr(status, "reconcile_connected_since", lambda statuses, now: {})
    await kss.McpGrantStore(data_home()).store(mcp_grant_kas.kas_tokens_entry_name(url), _TOKENS)

    [notion] = await status.collect_connection_statuses()

    assert notion["status"] == status.STATUS_NOT_CONNECTED, notion
    assert notion["grantPresent"] is False, notion


# ── the MCP Servers row ──

_HEADERS_URL = "https://mcp.tenant.example/mcp"
_OAUTH_URL = "https://mcp.operator.example/mcp"
_GITHUB_URL = "https://api.githubcopilot.com/mcp/"


def _mcp_row(name: str, url: str = _URL, **fields: Any) -> dict[str, Any]:
    """A served MCP row that met an OAuth challenge, as ``McpServerInfo.to_dict`` writes it."""
    return {"name": name, "url": url, "status": "needs_auth", "authChallenge": True, **fields}


async def _caveats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    from kiro_crew.dashboard.handlers import mcp as mcp_handlers

    await mcp_handlers._annotate_kas_chat_sign_in(rows)
    return {row["name"]: row.get("kasChatSignIn") for row in rows}


@pytest.mark.asyncio
async def test_a_signed_in_row_whose_grant_kas_chats_lack_says_to_sign_in_again():
    # Signed in through kiro-cli's cache only, as every earlier Settings sign-in is.
    rows = [_mcp_row(_SERVER, authGrantPresent=True)]

    assert await _caveats(rows) == {_SERVER: "again"}

    await kss.McpGrantStore(data_home()).store(mcp_grant_kas.kas_tokens_entry_name(_URL), _TOKENS)
    assert await _caveats(rows) == {_SERVER: None}
    assert "kasChatSignIn" not in rows[0]


@pytest.mark.asyncio
async def test_a_row_whose_sign_in_runs_on_kiro_cli_says_the_chat_signs_in(_kas_chats):
    (_kas_chats / "kirocrew.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    _SERVER: {"url": _URL},
                    "tenantServer": {"url": _HEADERS_URL, "headers": {"X-Tenant": "a"}},
                    "operatorServer": {"url": _OAUTH_URL, "oauth": {"clientId": "operator-app"}},
                    "github": {"url": _GITHUB_URL},
                }
            }
        ),
        encoding="utf-8",
    )
    rows = [
        _mcp_row(_SERVER, authGrantPresent=False),
        _mcp_row("tenantServer", _HEADERS_URL, authGrantPresent=True),
        _mcp_row("operatorServer", _OAUTH_URL, authGrantPresent=False),
        # A pre-registered provider signs in with the operator's client on kiro-cli.
        _mcp_row("github", _GITHUB_URL, authGrantPresent=True),
        # Crew serves this one to KAS chats itself; that row says nothing here.
        _mcp_row("crewHeld", authGrantPresent=True, authCrewHeld=True),
        {"name": "plain", "url": _URL, "status": "ok"},
    ]

    assert await _caveats(rows) == {
        _SERVER: None,
        "tenantServer": "chat",
        "operatorServer": "chat",
        "github": "chat",
        "crewHeld": None,
        "plain": None,
    }


@pytest.mark.asyncio
async def test_with_persistence_off_every_sign_in_row_says_the_chat_signs_in(monkeypatch):
    monkeypatch.setattr(mcp_grant_kas, "persistence_enabled", lambda: False)

    rows = [_mcp_row(_SERVER, authGrantPresent=False), _mcp_row("wiki", authGrantPresent=True)]

    assert await _caveats(rows) == {_SERVER: "chat", "wiki": "chat"}


@pytest.mark.asyncio
async def test_on_kiro_cli_chats_no_row_carries_a_kas_caveat(monkeypatch):
    monkeypatch.setattr(status, "chat_backend_is_kas", lambda: False)

    rows = [_mcp_row(_SERVER, authGrantPresent=True, kasChatSignIn="again")]

    assert await _caveats(rows) == {_SERVER: None}
    assert "kasChatSignIn" not in rows[0]


@pytest.mark.asyncio
async def test_the_mcp_list_serves_each_row_its_caveat(monkeypatch):
    from kiro_crew import mcp_discovery
    from kiro_crew.dashboard.handlers import mcp as mcp_handlers

    monkeypatch.setattr(
        mcp_handlers, "_mcp_probe_cache", [_mcp_row(_SERVER, authGrantPresent=True)]
    )
    monkeypatch.setattr(mcp_handlers, "_mcp_probe_ts", time.time())
    monkeypatch.setattr(mcp_discovery, "list_servers", lambda: [])
    for name in ("_annotate_quarantine", "_annotate_owner_sign_in", "_annotate_agent_drops"):
        monkeypatch.setattr(mcp_handlers, name, lambda rows: None)
    app = web.Application()
    app.router.add_get("/api/mcp/probe", mcp_handlers.api_mcp_probe_cached)
    client = TestClient(TestServer(as_owner(app)))
    await client.start_server()
    try:
        resp = await client.get("/api/mcp/probe")
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 200, body
    assert [row.get("kasChatSignIn") for row in body] == ["again"]


@pytest.mark.asyncio
async def test_a_sign_in_saved_under_the_mounted_urls_trailing_slash_reads_granted():
    # The provider matcher ignores a trailing slash, so the registry URL reaches the
    # watcher slashless while the entry the engine mounts keeps the slash, and the
    # engine stores its tokens under the MOUNTED url's key.
    mounted = _URL + "/"
    assert mcp_grant_kas.kas_tokens_entry_name(mounted) != mcp_grant_kas.kas_tokens_entry_name(_URL)
    await mint.start_oauth_mint(_SERVER, _URL, pinned={"url": mounted})
    await _until_state("waiting", "failed")

    await kss.SecretStorageServer().answer(
        kss.METHOD_SECRET_STORE,
        {"key": mcp_grant_kas.kas_tokens_entry_name(mounted), "value": _TOKENS},
    )
    _Engine.instances[-1].handle.place("ready")
    _Engine.instances[-1].signing_in = False

    granted = await _until_state("granted", "expired", "failed")
    assert granted["state"] == "granted", granted


@pytest.mark.asyncio
async def test_disconnect_deletes_the_kas_sign_in_before_the_mcp_lock_is_released(monkeypatch):
    # A holder added the moment the lock is free must find the vault already
    # settled, not a sign-in the earlier census is about to delete from under it.
    url = _notion()["mcp_url"]
    seen: list[set[str]] = []

    @contextlib.asynccontextmanager
    async def _recording_lock():
        yield
        seen.append(await _vault_names())

    await _disconnect_notion_beside(
        monkeypatch, "other", "https://other.example/mcp", lock=_recording_lock
    )

    [at_release] = seen
    assert at_release.isdisjoint(mcp_grant_kas.kas_saved_sign_in_names(url)), at_release
