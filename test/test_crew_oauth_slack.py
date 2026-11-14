"""Crew-owned OAuth for Slack's hosted MCP server (``mcp.slack.com/mcp``).

Slack is a pre-registered client with a secret: PKCE S256 plus
``client_secret_post``, the one method its MCP authorization server lists. Errors
arrive as a 200 with ``{"ok": false}``, refresh tokens rotate and are single-use,
and ``auth.revoke`` takes the access token and answers in the body.
"""

from __future__ import annotations

import json
import logging
import pathlib
import time
import types
import urllib.parse

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from kas_agents_helpers import _spec

from kiro_crew.acp import kas_agents
from kiro_crew.connections import crew_oauth, crew_oauth_proxy, registry
from kiro_crew.connections.crew_oauth import CrewOAuthError, Tokens, TokenStore
from kiro_crew.secrets import SecretVault

SLUG = "slack"
SESSION = "dashboard_chat-crew-oauth-slack"
REDIRECT = "http://localhost:48106/callback"
SLACK_URL = "https://mcp.slack.com/mcp"


@pytest.fixture(autouse=True)
def _pinned_modules(_floor_monkeypatch):
    """The code under test imports these at call time; pin them to this file's."""
    import sys

    import kiro_crew.connections as connections_pkg

    for name, module in (("crew_oauth", crew_oauth), ("crew_oauth_proxy", crew_oauth_proxy)):
        _floor_monkeypatch.setitem(sys.modules, f"kiro_crew.connections.{name}", module)
        _floor_monkeypatch.setattr(connections_pkg, name, module, raising=False)
    crew_oauth._refresh_locks.clear()
    crew_oauth_proxy._bridges.clear()
    _floor_monkeypatch.setenv("KIROCREW_BOUND_PORT", "8765")
    _floor_monkeypatch.delenv("KIROCREW_CONNECTIONS_SLACK_CLIENT_ID", raising=False)
    _floor_monkeypatch.delenv("KIROCREW_CONNECTIONS_SLACK_CLIENT_SECRET", raising=False)
    yield
    crew_oauth_proxy._bridges.clear()


def _client(client_id="123.456"):
    return types.SimpleNamespace(client_id=client_id, client_secret="sec")


# --- registry and consent URL -----------------------------------------------------


def test_slack_is_a_confidential_client_on_its_localhost_port():
    provider = registry.get_provider(SLUG)
    assert SLUG in crew_oauth.SUPPORTED_SLUGS and SLUG not in crew_oauth.DCR_SLUGS
    assert provider["mcp_url"] == "https://mcp.slack.com/mcp"
    assert provider["auth"]["confidential"] is True
    assert registry.redirect_uri(provider) == REDIRECT


def test_the_consent_url_is_slacks_user_token_page_with_pkce():
    url = crew_oauth.authorize_url(SLUG, "123.456", REDIRECT, "st", "ch")
    parts = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parts.query))
    assert (parts.scheme, parts.netloc, parts.path) == (
        "https",
        "slack.com",
        "/oauth/v2_user/authorize",
    )
    assert query["scope"].split() == list(registry.get_provider(SLUG)["recommended_scopes"])
    assert query["code_challenge_method"] == "S256" and query["code_challenge"] == "ch"
    assert query["redirect_uri"] == REDIRECT and query["response_type"] == "code"
    # Desktop redirects may not ask for bot scopes, and none are sent.
    assert "user_scope" not in query
    from kiro_crew.security import oauth_url_contains_credential

    vetted = crew_oauth.authorize_url(SLUG, "123.456", REDIRECT, "A" * 43, "A" * 43)
    assert oauth_url_contains_credential(vetted) is False


_SLACK_USER_QUERY = (
    "?response_type=code&client_id=3336676.569200954261"
    "&redirect_uri=http%3A%2F%2Flocalhost%3A48106%2Fcallback"
    "&scope=search%3Aread.public+channels%3Ahistory"
    "&code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    "&code_challenge_method=S256"
    "&state=" + ("Kp7mQ2xR" * 12)
)


def test_the_banner_gate_admits_a_high_entropy_state_at_slacks_user_consent_page():
    """Without the exact pair the PKCE values read as a credential and sign-in fails closed."""
    from kiro_crew.security import oauth_url_contains_credential

    url = "https://slack.com/oauth/v2_user/authorize" + _SLACK_USER_QUERY
    assert oauth_url_contains_credential(url) is False


def test_the_banner_gate_still_rejects_it_at_a_neighbouring_path():
    from kiro_crew.security import oauth_url_contains_credential

    url = "https://slack.com/oauth/v2_user/authorize/x" + _SLACK_USER_QUERY
    assert oauth_url_contains_credential(url) is True


# --- the client: id AND secret, from OAuth Apps ------------------------------------


def test_crew_refuses_a_slack_client_without_its_secret(tmp_path, monkeypatch):
    from kiro_crew.config import loader, paths

    config = {"connections": {"oauth_clients": {SLUG: {"client_id": "123.456"}}}}
    monkeypatch.setattr(loader, "read_config_for_update", lambda: config)
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path)
    with pytest.raises(CrewOAuthError) as err:
        crew_oauth._client(SLUG)
    assert err.value.code == "client_not_configured"
    monkeypatch.setenv("KIROCREW_CONNECTIONS_SLACK_CLIENT_SECRET", "sec")
    resolved = crew_oauth._client(SLUG)
    assert (resolved.client_id, resolved.client_secret) == ("123.456", "sec")
    assert resolved.redirect_uri == REDIRECT


def test_the_sign_in_refuses_a_resolved_client_that_has_no_secret(tmp_path, monkeypatch):
    """The sign-in's own check, apart from the resolver's: no secret, no sign-in."""
    from kiro_crew.config import loader, paths
    from kiro_crew.connections import oauth_clients

    monkeypatch.setattr(loader, "read_config_for_update", lambda: {})
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(
        oauth_clients,
        "resolve_oauth_client",
        lambda *a, **k: oauth_clients.ResolvedOAuthClient(
            SLUG, "123.456", "config", None, None, REDIRECT
        ),
    )
    with pytest.raises(CrewOAuthError) as err:
        crew_oauth._client(SLUG)
    assert err.value.code == "client_not_configured"


# --- token endpoint ---------------------------------------------------------------


def _slack(seen: list, token_answer: dict, revoke_answer: dict | str | None = None):
    async def _token(request):
        seen.append(("token", dict(await request.post())))
        return web.json_response(token_answer)

    async def _revoke(request):
        seen.append(("revoke", dict(await request.post())))
        if isinstance(revoke_answer, str):
            return web.Response(text=revoke_answer)
        return web.json_response(revoke_answer or {"ok": True, "revoked": True})

    app = web.Application()
    app.router.add_post("/api/oauth.v2.user.access", _token)
    app.router.add_post("/api/auth.revoke", _revoke)
    return app


async def _serve(monkeypatch, seen, token_answer=None, revoke_answer=None):
    server = TestServer(_slack(seen, token_answer or {"ok": True}, revoke_answer))
    await server.start_server()
    base = str(server.make_url(""))
    monkeypatch.setitem(
        crew_oauth._ENDPOINTS,
        SLUG,
        {
            **crew_oauth._ENDPOINTS[SLUG],
            "token": f"{base}/api/oauth.v2.user.access",
            "revoke": f"{base}/api/auth.revoke",
        },
    )
    return server


async def _swap(tmp_path, monkeypatch, answer, client):
    seen: list = []
    server = await _serve(monkeypatch, seen, answer)
    store = TokenStore(SecretVault(tmp_path))
    sign_in = crew_oauth.SignIn(SLUG)
    sign_in._client, sign_in._redirect = client, REDIRECT
    sign_in._done.set_result("the-code")
    try:
        await sign_in.finish(store)
    finally:
        await server.close()
    return seen, store, sign_in


@pytest.mark.asyncio
async def test_the_code_swap_sends_the_secret_and_the_verifier(tmp_path, monkeypatch):
    answer = {
        "ok": True,
        "access_token": "xoxe.xoxp-1",
        "refresh_token": "xoxe-1-r1",
        "expires_in": 43200,
        "token_type": "user",
        "authed_user": {"id": "U1", "scope": "search:read.public"},
    }
    seen, store, sign_in = await _swap(tmp_path, monkeypatch, answer, _client())
    form = seen[0][1]
    assert form == {
        "grant_type": "authorization_code",
        "client_id": "123.456",
        "client_secret": "sec",
        "code": "the-code",
        "redirect_uri": REDIRECT,
        "code_verifier": sign_in._verifier,
    }
    held = store.read(SLUG)
    assert held is not None
    assert (held[0].access, held[0].refresh, held[0].client) == (
        "xoxe.xoxp-1",
        "xoxe-1-r1",
        "123.456",
    )
    assert held[0].expires_at is not None and held[0].expires_at > time.time() + 43000


@pytest.mark.asyncio
async def test_slacks_200_ok_false_is_a_failed_swap_that_stores_nothing(tmp_path, monkeypatch):
    with pytest.raises(CrewOAuthError) as err:
        await _swap(tmp_path, monkeypatch, {"ok": False, "error": "invalid_code"}, _client())
    assert err.value.code == "invalid_code"
    assert TokenStore(SecretVault(tmp_path)).read(SLUG) is None


@pytest.mark.asyncio
async def test_a_refresh_rotates_the_single_use_refresh_token(tmp_path, monkeypatch):
    seen: list = []
    answer = {"ok": True, "access_token": "a2", "refresh_token": "r2", "expires_in": 43200}
    server = await _serve(monkeypatch, seen, answer)
    monkeypatch.setattr(crew_oauth, "_client", lambda s: _client())
    store = TokenStore(SecretVault(tmp_path))
    store.replace(SLUG, Tokens("a1", "r1", time.time() - 10, "", None, "123.456"))
    try:
        assert await crew_oauth.access_token(SLUG, store=store) == "a2"
    finally:
        await server.close()
    assert seen[0][1] == {
        "grant_type": "refresh_token",
        "refresh_token": "r1",
        "client_id": "123.456",
        "client_secret": "sec",
    }
    held = store.read(SLUG)
    assert held is not None and (held[0].access, held[0].refresh) == ("a2", "r2")
    assert held[0].client == "123.456"


@pytest.mark.asyncio
async def test_a_dead_refresh_token_asks_for_a_reconnect_and_keeps_the_pair(tmp_path, monkeypatch):
    seen: list = []
    server = await _serve(monkeypatch, seen, {"ok": False, "error": "invalid_refresh_token"})
    monkeypatch.setattr(crew_oauth, "_client", lambda s: _client())
    store = TokenStore(SecretVault(tmp_path))
    store.replace(SLUG, Tokens("a1", "r1", time.time() - 10, ""))
    try:
        with pytest.raises(CrewOAuthError) as err:
            await crew_oauth.access_token(SLUG, store=store)
    finally:
        await server.close()
    assert err.value.code == "needs_auth"
    assert store.read(SLUG) is not None


# --- Disconnect -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disconnect_revokes_the_access_token(tmp_path, monkeypatch):
    seen: list = []
    server = await _serve(monkeypatch, seen)

    def _gone(slug):
        raise CrewOAuthError("client_not_configured")

    # The revoke takes the token alone: a removed OAuth Apps record never blocks it.
    monkeypatch.setattr(crew_oauth, "_client", _gone)
    store = TokenStore(SecretVault(tmp_path))
    store.replace(SLUG, Tokens("a1", "r1", None, ""))
    try:
        result = await crew_oauth.disconnect(SLUG, store=store)
    finally:
        await server.close()
    assert result == {"removed": True, "revoked": True}
    assert seen == [("revoke", {"token": "a1"})]
    assert not store.present(SLUG)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [{"ok": False, "error": "token_expired"}, {"ok": True, "revoked": False}, "not json"],
    ids=["ok-false", "revoked-false", "non-json"],
)
async def test_a_200_that_says_not_revoked_is_reported_and_still_deletes(
    tmp_path, monkeypatch, answer
):
    seen: list = []
    server = await _serve(monkeypatch, seen, revoke_answer=answer)
    store = TokenStore(SecretVault(tmp_path))
    store.replace(SLUG, Tokens("a1", "r1", None, ""))
    try:
        result = await crew_oauth.disconnect(SLUG, store=store)
    finally:
        await server.close()
    assert result == {"removed": True, "revoked": False}
    assert not store.present(SLUG)


# --- custody: the vault, never a log, a spec or the session ------------------------


@pytest.mark.asyncio
async def test_the_pair_lives_only_in_the_vault(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    answer = {"ok": True, "access_token": "xoxp-SECRET-A", "refresh_token": "xoxe-SECRET-R"}
    _, store, _ = await _swap(tmp_path, monkeypatch, answer, _client())
    crew_oauth._audit("connections_crew_oauth_sign_in", "ok", SLUG)
    assert "SECRET" not in caplog.text
    # Encrypted at rest: no file under the data home carries either token.
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert b"SECRET" not in path.read_bytes(), path
    assert store.present(SLUG)


def test_the_kas_projection_carries_the_bridge_not_the_client_or_token(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr(crew_oauth, "enabled", lambda: True)
    monkeypatch.setattr(crew_oauth, "_chat_backend_bridged", lambda: True)
    url = registry.get_provider(SLUG)["mcp_url"]
    spec = _spec(
        tools=[f"@{SLUG}"],
        allowedTools=[f"@{SLUG}"],
        mcpServers={SLUG: {"url": url, "oauth": {"clientId": "123.456"}}},
    )
    out = kas_agents.to_client_custom_agent("kirocrew", spec, "p", session_key=SESSION)
    entry = out["mcpServers"][SLUG]
    assert "url" not in entry and "oauth" not in entry
    assert url not in json.dumps(out) and "123.456" not in json.dumps(out)
    assert crew_oauth_proxy.is_connection_server(SESSION, SLUG)


# --- which entry is Slack's, and what a rebuild keeps -------------------------------


def _url_variants(url: str) -> list[str]:
    """The two spellings the card still owns: one more trailing slash, other letter case."""
    return [url + "/", url.upper()]


def test_no_client_is_bound_and_no_sign_in_is_routed_at_a_variant_url():
    """The card owns a row at a trailing-slash or letter-case variant; nothing else does.

    ``provider_for_server`` binds the operator's client and ``routed_slug`` sends a
    sign-in to Crew, so both refuse every URL that is not the registry's exactly,
    for every pre-registered provider. The card's Connect writes the registry URL
    onto the row first, and only then is the row the provider's.
    """
    from kiro_crew.connections.oauth_clients import provider_for_server
    from kiro_crew.connections.registry import is_preregistered

    slugs = [p["slug"] for p in registry.get_all_registry_providers() if is_preregistered(p)]
    assert SLUG in slugs and "github" in slugs
    for slug in slugs:
        url = registry.get_provider(slug)["mcp_url"]
        assert provider_for_server(slug, {"url": url})["slug"] == slug
        for variant in _url_variants(url):
            assert provider_for_server(slug, {"url": variant}) is None, (slug, variant)
            assert crew_oauth.routed_slug(slug, {"url": variant}, provider_for_server) == "", (
                slug,
                variant,
            )


def _owner_slack(
    monkeypatch, *, app_saved=False, card_grant=False, owner_grant=True, recorded=True
):
    """An owner-added ``slack`` server at Slack's URL, preview on, chats on KAS.

    ``app_saved``: a Slack app under Settings -> Connections. ``card_grant``: Crew
    holds a grant under the card's slug. ``owner_grant``: Crew holds one under the
    owner key. ``recorded``: the sealed owner record names the server.
    """
    import kiro_crew.config.loader as loader
    from kiro_crew.connections.owner_servers import CONFIG_ROOT_KEY, CONFIG_SERVERS_KEY

    owner_key = crew_oauth.custom_key(SLUG, SLACK_URL)
    root: dict = {}
    if recorded:
        root[CONFIG_SERVERS_KEY] = {SLUG: {"url": SLACK_URL}}
    if app_saved:
        root["oauth_clients"] = {SLUG: {"client_id": "123.456"}}
        monkeypatch.setenv("KIROCREW_CONNECTIONS_SLACK_CLIENT_SECRET", "sec")
    held = {owner_key} if owner_grant else set()
    if card_grant:
        held.add(SLUG)
    monkeypatch.setattr(loader, "read_config_for_update", lambda: {CONFIG_ROOT_KEY: root})
    monkeypatch.setattr(crew_oauth, "holds_grant", lambda slug: slug in held)
    monkeypatch.setattr(crew_oauth, "enabled", lambda: True)
    monkeypatch.setattr(crew_oauth, "_chat_backend", lambda: "kas")
    return owner_key


def _owner_sign_in_flag() -> object:
    from kiro_crew.dashboard.handlers import mcp_probe

    rows = [{"name": SLUG, "url": SLACK_URL}]
    mcp_probe._annotate_owner_sign_in(rows)
    return rows[0].get("ownerSignIn")


def test_an_owner_added_slack_server_keeps_its_own_path_while_the_card_has_no_app(monkeypatch):
    """No Slack app saved and no grant under the card: the owner's server is as before.

    Its row keeps the in-place Sign in, that sign-in goes to Crew with the preview
    on, and a session reads the grant Crew holds under the owner key.
    """
    from kiro_crew.connections.oauth_clients import provider_for_server
    from kiro_crew.connections.owner_servers import is_signable_name

    owner_key = _owner_slack(monkeypatch)
    entry = {"url": SLACK_URL}

    assert is_signable_name(SLUG)
    assert _owner_sign_in_flag() is True
    assert crew_oauth.signs_in_custom(entry)
    assert crew_oauth.routed_slug(SLUG, entry, provider_for_server, backend="kas") == owner_key


@pytest.mark.parametrize("state", [{"app_saved": True}, {"card_grant": True}], ids=["app", "grant"])
def test_the_card_owns_an_owner_added_slack_server_once_it_has_an_app_or_a_grant(
    monkeypatch, state
):
    """A saved Slack app, or a grant under the card's slug: the card owns the entry.

    The owner key is not used, the row loses its own Sign in, and the owner-server
    sign-in refuses the registry URL again.
    """
    from kiro_crew.connections.oauth_clients import provider_for_server
    from kiro_crew.connections.owner_servers import is_signable_name

    _owner_slack(monkeypatch, **state)
    entry = {"url": SLACK_URL}

    assert not is_signable_name(SLUG)
    assert _owner_sign_in_flag() is False
    assert not crew_oauth.signs_in_custom(entry)
    assert crew_oauth._custom_slug(SLUG, entry) == ""
    assert crew_oauth.routed_slug(SLUG, entry, provider_for_server, backend="kas") == SLUG


def test_only_the_owners_recorded_slack_server_is_left_to_its_own_path(monkeypatch):
    """A ``slack`` entry the owner did not record never reaches the owner path.

    It has no in-place Sign in and is routed to the card's slug, whatever grant
    sits under the key its name and url would make. The same holds for an entry
    the owner recorded at another url than the one in the spec.
    """
    from kiro_crew.connections.oauth_clients import provider_for_server

    owner_key = _owner_slack(monkeypatch, recorded=False)
    entry = {"url": SLACK_URL}

    assert _owner_sign_in_flag() is False
    assert crew_oauth._custom_slug(SLUG, entry) == ""
    assert crew_oauth.routed_slug(SLUG, entry, provider_for_server, backend="kas") == SLUG

    _owner_slack(monkeypatch)
    moved = {"url": SLACK_URL + "x"}
    monkeypatch.setattr(crew_oauth, "holds_grant", lambda slug: slug != SLUG)
    assert crew_oauth.routed_slug(SLUG, moved, provider_for_server, backend="kas") == ""
    assert owner_key != crew_oauth.custom_key(SLUG, SLACK_URL + "x")


def test_a_card_that_never_keeps_a_users_client_never_yields(monkeypatch):
    """The rule is Slack's: GitHub's card, as unconfigured as Slack's, keeps its name."""
    import kiro_crew.config.loader as loader
    from kiro_crew.connections.owner_servers import card_yields, is_signable_name

    monkeypatch.setattr(loader, "read_config_for_update", lambda: {})
    monkeypatch.setattr(crew_oauth, "holds_grant", lambda slug: False)

    assert card_yields(registry.get_provider(SLUG))
    assert not card_yields(registry.get_provider("github"))
    assert not card_yields(registry.get_provider("notion"))
    assert not is_signable_name("github") and not is_signable_name("notion")

    monkeypatch.setattr(crew_oauth, "holds_grant", lambda slug: None)
    assert not card_yields(registry.get_provider(SLUG))


OP_ID = "123.456"
OP_SECRET = "operator-made-up-secret"
MINE_NO_SECRET = {"clientId": "mine", "redirectUri": "http://localhost:3118/callback"}
MINE_WITH_SECRET = {**MINE_NO_SECRET, "clientSecret": "my-own-secret"}


class _SlackHome:
    """A throwaway data home and Kiro agents folder the real rebuild and copy run on.

    ``store_entry`` is the ``slack`` entry in ``<data home>/mcp.json``, ``None`` for
    a home with no such entry. ``app_saved``
    saves a Slack app as Settings -> Connections does: the Client ID in
    ``config.json``, the Client Secret in the vault. ``recorded`` writes the sealed
    owner record for the entry. ``preview`` is the ``crew_oauth`` preview; chats
    run on KAS.
    """

    def __init__(
        self, tmp_path, monkeypatch, store_entry, *, app_saved=False, recorded=False, preview=False
    ):
        from kiro_crew import agent as agent_mod
        from kiro_crew.apps import bridges
        from kiro_crew.connections.oauth_clients import client_secret_name
        from kiro_crew.dashboard.handlers import mcp as mcp_handlers

        self._agent, self._handlers, self.root = agent_mod, mcp_handlers, tmp_path
        project = tmp_path / "project" / "agents"
        project.mkdir(parents=True)
        (project / "defaults.json").write_text(json.dumps({"name": "kirocrew"}), encoding="utf-8")
        (project / "prompt.md").write_text("prompt", encoding="utf-8")
        self.home = tmp_path / "home"
        self.home.mkdir()
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path / "project"))
        monkeypatch.setenv("KIROCREW_HOME", str(self.home))
        self.store = self.home / "mcp.json"
        servers = {} if store_entry is None else {SLUG: store_entry}
        self.store.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
        connections: dict = {}
        if recorded:
            connections["owner_mcp_servers"] = {SLUG: {"url": SLACK_URL}}
        if app_saved:
            connections["oauth_clients"] = {SLUG: {"client_id": OP_ID}}
            SecretVault(self.home).set_sync(client_secret_name(SLUG), OP_SECRET)
        (self.home / "config.json").write_text(
            json.dumps({"connections": connections}), encoding="utf-8"
        )
        self.agents = tmp_path / ".kiro" / "agents"
        self.agents.mkdir(parents=True)
        self.spec = self.agents / "kirocrew.json"
        monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", self.agents)
        monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", tmp_path / "absent-kiro.json")
        monkeypatch.setattr(agent_mod, "_CC_MCP_JSON", tmp_path / "absent-cc.json")
        monkeypatch.setattr(bridges, "_mcp_json_path", lambda: self.spec)
        monkeypatch.setattr(mcp_handlers, "kiro_agents_dir", lambda: self.agents)
        monkeypatch.setattr(
            mcp_handlers, "_read_agent_spec", lambda p, **_k: json.loads(p.read_text())
        )
        monkeypatch.setattr(mcp_handlers, "_kirocrew_mcp_json", lambda: self.store)
        monkeypatch.setattr(mcp_handlers, "_GLOBAL_MCP_JSON", tmp_path / "absent.json")
        monkeypatch.setattr(mcp_handlers, "_extra_mcp_scopes", lambda: [])
        monkeypatch.setattr(crew_oauth, "enabled", lambda: preview)
        monkeypatch.setattr(crew_oauth, "_chat_backend", lambda: "kas")
        if app_saved:
            # The setup is real: Crew's own sign-in resolves this client from the vault.
            assert crew_oauth._client(SLUG).client_secret == OP_SECRET

    def rebuild(self) -> dict:
        """Run the real rebuild; return the ``slack`` entry it wrote into the spec."""
        self._agent.rebuild_agent_config(refresh_forks=False)
        return json.loads(self.spec.read_text(encoding="utf-8"))["mcpServers"][SLUG]

    def copy_into_store(self) -> dict | None:
        """What Apply does for a row the store lacks: find the spec, write it to the store."""
        found = self._handlers._find_server_spec_anywhere(SLUG)
        self._handlers._set_kirocrew_entry(SLUG, enabled=True, spec=found)
        return found

    def files_holding(self, text: str) -> list[str]:
        """Every file under the data home and the Kiro agents folder that contains *text*."""
        needle = text.encode("utf-8")
        return sorted(
            str(path.relative_to(self.root))
            for path in self.root.rglob("*")
            if path.is_file() and needle in path.read_bytes()
        )

    def files_naming_the_client(self) -> list[str]:
        """The agent specs and ``mcp.json`` files that carry the operator's Client ID.

        ``config.json`` is where Settings -> Connections keeps the public Client ID,
        so that file and the copies Kiro Crew keeps of it are not among them.
        """
        return [
            name
            for name in self.files_holding(OP_ID)
            if not pathlib.PurePath(name).name.startswith("config.json")
        ]


@pytest.mark.parametrize("preview", [False, True], ids=["preview-off", "preview-on"])
def test_a_rebuild_writes_the_operators_slack_client_into_no_file(tmp_path, monkeypatch, preview):
    """A card install with a Slack app saved, through the real rebuild.

    Crew's sign-in reads the client from the vault. The agent spec, every
    ``mcp.json`` and every other file under the data home and the Kiro agents
    folder hold neither the Client Secret nor the Client ID.
    """
    home = _SlackHome(tmp_path, monkeypatch, {"url": SLACK_URL}, app_saved=True, preview=preview)

    written = home.rebuild()

    assert "oauth" not in written
    assert home.files_holding(OP_SECRET) == []
    assert home.files_naming_the_client() == []


def test_the_warm_sign_in_helper_writes_no_slack_client(tmp_path, monkeypatch):
    """The shared kiro-cli helper never lists Slack, so its spec folder holds no client.

    A Slack app is saved and nobody has pressed Connect yet, which is when the
    helper would take the provider's entry from the registry and the vault.
    """
    from kiro_crew.connections import warm

    home = _SlackHome(tmp_path, monkeypatch, None, app_saved=True)

    plan = warm._warm_spec_plan(warm.warm_spec_providers())
    warm._write_warm_mint_specs(plan, tmp_path / "warm" / ".kiro" / "agents")

    assert SLUG not in plan.entries
    assert home.files_holding(OP_SECRET) == []
    assert home.files_naming_the_client() == []


def test_the_cold_connect_spec_holds_no_slack_client(tmp_path, monkeypatch):
    """The one-server spec a kiro-cli Connect writes is a copy of the agent spec's entry."""
    from kiro_crew.connections import mint

    home = _SlackHome(tmp_path, monkeypatch, {"url": SLACK_URL}, app_saved=True)
    home.rebuild()

    _name, path = mint._write_mint_agent_spec(SLUG)

    assert path and "kirocrew-mint-slack-" in path
    assert home.files_holding(OP_SECRET) == []
    assert home.files_naming_the_client() == []


@pytest.mark.parametrize("damage", ["broken-by-hand", "entry-removed"])
def test_a_scope_copy_after_the_store_is_damaged_writes_no_operator_client(
    tmp_path, monkeypatch, damage
):
    """The store's ``slack`` entry is lost, then Apply copies the spec back into it.

    ``broken-by-hand``: ``<data home>/mcp.json`` does not parse. ``entry-removed``:
    the entry was deleted and a rebuild ran. Either way the copy has no operator
    client to carry, because the spec never held one.
    """
    home = _SlackHome(tmp_path, monkeypatch, {"url": SLACK_URL}, app_saved=True)
    home.rebuild()
    if damage == "broken-by-hand":
        home.store.write_text('{"mcpServers": {"slack": {"url": ', encoding="utf-8")
    else:
        home.store.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        home.rebuild()

    found = home.copy_into_store()

    assert found == {"url": SLACK_URL}
    # Whatever the write did with the damaged file, no file holds the client.
    assert home.files_holding(OP_SECRET) == []
    assert home.files_naming_the_client() == []


@pytest.mark.parametrize("app_saved", [False, True], ids=["no-app", "app-saved"])
@pytest.mark.parametrize("preview", [False, True], ids=["preview-off", "preview-on"])
@pytest.mark.parametrize("mine", [MINE_NO_SECRET, MINE_WITH_SECRET], ids=["no-secret", "secret"])
def test_a_rebuild_keeps_the_client_the_user_wrote(tmp_path, monkeypatch, mine, preview, app_saved):
    """A hand-written ``slack`` entry with its own client, not in the owner record.

    The spec keeps that client as written, with the preview off or on and with a
    Slack app saved or not, and the user's file is read, never edited.
    """
    entry = {"url": SLACK_URL, "oauth": dict(mine)}
    home = _SlackHome(tmp_path, monkeypatch, entry, app_saved=app_saved, preview=preview)

    written = home.rebuild()

    assert written["oauth"] == mine
    assert json.loads(home.store.read_text(encoding="utf-8"))["mcpServers"][SLUG] == entry
    assert home.files_holding(OP_SECRET) == []


def test_an_owner_added_slack_server_keeps_its_client_its_sign_in_and_its_path(
    tmp_path, monkeypatch
):
    """The owner's own ``slack`` server while the card has no app, preview on.

    The spec keeps the client the owner wrote, the row keeps its Sign in, and a
    KAS session reads the grant held under the owner key.
    """
    from kiro_crew.connections.oauth_clients import provider_for_server

    entry = {"url": SLACK_URL, "oauth": dict(MINE_NO_SECRET)}
    home = _SlackHome(tmp_path, monkeypatch, entry, recorded=True, preview=True)
    owner_key = crew_oauth.custom_key(SLUG, SLACK_URL)
    monkeypatch.setattr(crew_oauth, "holds_grant", lambda slug: slug == owner_key)

    written = home.rebuild()

    assert written["oauth"] == MINE_NO_SECRET
    assert _owner_sign_in_flag() is True
    assert crew_oauth.routed_slug(SLUG, written, provider_for_server, backend="kas") == owner_key


def test_a_scope_copy_keeps_the_client_on_the_rendered_slack_entry(tmp_path, monkeypatch):
    """Copying the rendered ``slack`` entry into a scope carries its client along.

    Every client on a ``slack`` entry is the user's, so the copy drops nothing.
    """
    entry = {"url": SLACK_URL, "oauth": dict(MINE_WITH_SECRET)}
    home = _SlackHome(tmp_path, monkeypatch, entry)
    home.rebuild()
    home.store.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

    found = home.copy_into_store()

    assert found is not None and found["oauth"] == MINE_WITH_SECRET


def test_a_projected_providers_scope_copy_still_drops_the_client():
    """GitHub's client is projected into the spec, so its copy drops the three keys."""
    from kiro_crew.connections.oauth_clients import (
        keeps_user_client,
        strip_preregistered_oauth_client,
    )

    assert keeps_user_client(SLUG) and not keeps_user_client("github")
    rendered = {"url": "u", "oauth": {"clientId": "op", "clientSecret": "s", "redirectUri": "r"}}
    assert strip_preregistered_oauth_client(rendered) == {"url": "u"}


# --- the MCP table: the card claims a slack row only once it can sign in ------------


def _table_rows() -> list[dict]:
    github = registry.get_provider("github")["mcp_url"]
    return [
        {"name": SLUG, "url": SLACK_URL},
        {"name": "github", "url": github},
        {"name": SLUG, "url": "https://slack.example.com/mcp"},
        {"name": "docs", "url": "https://docs.example.com/mcp"},
        *({"name": SLUG, "url": variant} for variant in _url_variants(SLACK_URL)),
    ]


def test_a_slack_row_is_left_to_its_own_sign_in_while_no_oauth_app_is_saved(tmp_path, monkeypatch):
    """No OAuth Apps record: the card could only fail, so the row is not the card's.

    GitHub's row, as unconfigured as Slack's here, keeps its card. A slack row at a
    trailing-slash or letter-case variant is one the card owns too, so it is marked.
    """
    from kiro_crew.dashboard.handlers import mcp_probe

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.delenv("KIROCREW_CONNECTIONS_SLACK_CLIENT_SECRET", raising=False)
    rows = _table_rows()

    mcp_probe._annotate_unconfigured_connection(rows)

    assert [row.get("connectionUnconfigured") for row in rows] == [
        True,
        None,
        None,
        None,
        True,
        True,
    ]


def test_the_card_claims_the_slack_row_once_its_oauth_app_is_saved(tmp_path, monkeypatch):
    """Client ID and Secret saved: no flag, and a stale one from a cached row goes."""
    from kiro_crew.dashboard.handlers import mcp_probe

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(
        json.dumps({"connections": {"oauth_clients": {SLUG: {"client_id": "123.456"}}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("KIROCREW_CONNECTIONS_SLACK_CLIENT_SECRET", "sec")
    rows = _table_rows()
    rows[0]["connectionUnconfigured"] = True
    rows[0]["connectionNeedsApp"] = True

    mcp_probe._annotate_unconfigured_connection(rows)

    assert all("connectionUnconfigured" not in row for row in rows)
    assert all("connectionNeedsApp" not in row for row in rows)


@pytest.mark.parametrize("preview,backend", [(False, "kas"), (True, "kiro"), (True, "kas")])
@pytest.mark.parametrize("cached", [False, True])
def test_configured_slack_row_needs_crew_sign_in_only_while_unavailable(
    tmp_path, monkeypatch, preview, backend, cached
):
    from kiro_crew.dashboard.handlers import mcp_probe

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(
        json.dumps({"connections": {"oauth_clients": {SLUG: {"client_id": "123.456"}}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("KIROCREW_CONNECTIONS_SLACK_CLIENT_SECRET", "sec")
    monkeypatch.setattr(crew_oauth, "enabled", lambda: preview)
    monkeypatch.setattr(crew_oauth, "_chat_backend", lambda: backend)
    rows = _table_rows()
    if cached:
        for row in rows:
            row["connectionNeedsCrewSignIn"] = True

    mcp_probe._annotate_unconfigured_connection(rows)

    needed = True if not preview or backend == "kiro" else None
    assert [row.get("connectionNeedsCrewSignIn") for row in rows] == [
        needed,
        None,
        None,
        None,
        needed,
        needed,
    ]
    assert all("connectionUnconfigured" not in row for row in rows)
    assert all("connectionNeedsApp" not in row for row in rows)


def test_a_marked_slack_row_names_the_missing_app_when_chat_cannot_sign_in(tmp_path, monkeypatch):
    """No OAuth app saved: which marked rows the table must not send to chat.

    A row with no client of its own cannot sign in anywhere. A row with its own
    client signs in from chat on the harness's own OAuth, until Crew's sign-in
    serves the entry (preview on, chats on KAS) and has no client to present.
    """
    from kiro_crew.dashboard.handlers import mcp_probe

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.delenv("KIROCREW_CONNECTIONS_SLACK_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(crew_oauth, "_chat_backend", lambda: "kas")

    def needs_app(preview: bool) -> list[object]:
        monkeypatch.setattr(crew_oauth, "enabled", lambda: preview)
        rows = [
            {"name": SLUG, "url": SLACK_URL},
            {"name": SLUG, "url": SLACK_URL, "clientId": "mine"},
            {"name": "github", "url": registry.get_provider("github")["mcp_url"]},
        ]
        mcp_probe._annotate_unconfigured_connection(rows)
        return [row.get("connectionNeedsApp") for row in rows]

    assert needs_app(False) == [True, None, None]
    assert needs_app(True) == [True, True, None]


# --- an entry Crew's sign-in does not own: no Connect, and no steps to turn it on ---

_OWN_HEADER = {"name": SLUG, "url": SLACK_URL, "headers": {"Authorization": "Bearer x"}}
_OTHER_URL = {"name": SLUG, "url": "https://slack.example.com/mcp", "headers": {}}
_PLAIN = {"name": SLUG, "url": SLACK_URL, "headers": {}}


def _configured_slack(tmp_path, monkeypatch, entry: dict, *, preview: bool = True) -> None:
    """A Slack app saved, chats on KAS, and *entry* as the one configured ``slack`` server."""
    import kiro_crew.mcp_discovery as mcp_discovery
    from kiro_crew import mcp_grant
    from kiro_crew.connections import mint, status

    class _NoGrant:
        def stat(self):
            raise FileNotFoundError(SLUG)

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    (home / "config.json").write_text(
        json.dumps({"connections": {"oauth_clients": {SLUG: {"client_id": "123.456"}}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("KIROCREW_CONNECTIONS_SLACK_CLIENT_SECRET", "sec")
    monkeypatch.setattr(crew_oauth, "enabled", lambda: preview)
    monkeypatch.setattr(crew_oauth, "_chat_backend", lambda: "kas")
    server = types.SimpleNamespace(**entry)
    monkeypatch.setattr(mcp_discovery, "list_servers", lambda: [server])
    monkeypatch.setattr(status, "_CONNECTION_STATE_PATH", tmp_path / "connected-since.json")
    monkeypatch.setattr(status, "_UNPERSISTED_RECORD", None)
    monkeypatch.setattr(status, "get_visible_providers", lambda: [registry.get_provider(SLUG)])
    monkeypatch.setattr(
        mcp_grant, "grant_artifact_paths", lambda url, **kw: (_NoGrant(), _NoGrant())
    )
    monkeypatch.setattr(mint, "pending_mint_for", lambda slug: None)


async def _mint_slack(monkeypatch) -> tuple[int, dict, list[str]]:
    """POST the card's Connect for Slack; nothing real starts behind it."""
    from aiohttp.test_utils import TestClient
    from dashboard_owner_helpers import as_owner

    import kiro_crew.connections.warm as warm_mod
    from kiro_crew.dashboard.handlers import connections

    started: list[str] = []

    async def _cold(slug: str, *args: object, **kwargs: object) -> web.Response:
        started.append(f"cold:{slug}")
        return web.json_response({"ok": True})

    async def _adopt(slug: str, *args: object, **kwargs: object) -> None:
        started.append(f"warm:{slug}")

    async def _crew(slug: str, *args: object) -> str:
        started.append(f"crew:{slug}")
        return "token"

    monkeypatch.setattr(connections, "_start_cold_mint", _cold)
    monkeypatch.setattr(warm_mod, "adopt_shared_mint", _adopt)
    monkeypatch.setattr(crew_oauth, "start_mint_row", _crew)
    app = web.Application()
    app.router.add_post("/api/connections/mint", connections.api_connections_mint)
    as_owner(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post("/api/connections/mint", json={"slug": SLUG})
        return resp.status, await resp.json(), started
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", [_OWN_HEADER, _OTHER_URL], ids=["credential-header", "other-url"])
async def test_an_entry_crew_does_not_own_hides_connect_without_the_turn_on_steps(
    tmp_path, monkeypatch, entry
):
    """Crew's sign-in is on but the configured entry keeps its own path.

    The card hides Connect (``crewSignInNotOwned``) but does not tell the user to
    turn on a sign-in that is already on (no ``needsCrewSignIn``), and asks for no
    OAuth app. The mint refuses as before. The MCP row keeps what it showed for
    such an entry: an entry with its own header gets no mark, and an entry at
    another url is not the card's row at all, so it gets no mark either.
    """
    from kiro_crew.connections import status
    from kiro_crew.dashboard.handlers import mcp_probe

    _configured_slack(tmp_path, monkeypatch, entry)
    assert crew_oauth.manages(SLUG) and not crew_oauth.owns(SLUG)

    slack = {s["slug"]: s for s in await status.collect_connection_statuses()}[SLUG]
    assert slack.get("crewSignInNotOwned") is True
    assert "needsCrewSignIn" not in slack
    assert "needsClientConfig" not in slack

    rows = [dict(entry)]
    mcp_probe._annotate_unconfigured_connection(rows)
    assert "connectionNeedsCrewSignIn" not in rows[0]
    assert "connectionUnconfigured" not in rows[0]

    code, body, started = await _mint_slack(monkeypatch)
    assert (code, body.get("code"), started) == (409, "crew_sign_in_required", [])


@pytest.mark.asyncio
async def test_the_plain_official_entry_is_crews_and_carries_no_mark(tmp_path, monkeypatch):
    """Control: the official url with no header is Crew's to sign in, so the card connects."""
    from kiro_crew.connections import status
    from kiro_crew.dashboard.handlers import mcp_probe

    _configured_slack(tmp_path, monkeypatch, _PLAIN)
    assert crew_oauth.owns(SLUG)

    slack = {s["slug"]: s for s in await status.collect_connection_statuses()}[SLUG]
    for key in ("crewSignInNotOwned", "needsCrewSignIn", "needsClientConfig"):
        assert key not in slack
    rows = [dict(_PLAIN)]
    mcp_probe._annotate_unconfigured_connection(rows)
    assert "connectionNeedsCrewSignIn" not in rows[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", _url_variants(SLACK_URL), ids=["trailing-slash", "upper-case"])
async def test_a_url_variant_the_card_owns_keeps_connect(tmp_path, monkeypatch, variant):
    """A trailing-slash or letter-case spelling is the card's row: Connect stays.

    ``owns`` still compares the exact url, so the mint would refuse this entry, but
    the card's Connect writes the registry url onto it before minting.
    """
    from kiro_crew.connections import status

    _configured_slack(tmp_path, monkeypatch, {"name": SLUG, "url": variant, "headers": {}})
    assert crew_oauth.manages(SLUG) and not crew_oauth.owns(SLUG)

    slack = {s["slug"]: s for s in await status.collect_connection_statuses()}[SLUG]
    for key in ("crewSignInNotOwned", "needsCrewSignIn", "needsClientConfig"):
        assert key not in slack


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", [_PLAIN, _OWN_HEADER], ids=["plain", "credential-header"])
async def test_crews_sign_in_off_still_shows_the_turn_on_steps(tmp_path, monkeypatch, entry):
    """Preview off: the card shows the steps, never the other mark.

    The MCP row shows them only for an entry that would sign in through Crew. An
    entry with its own credential header signs in by itself, so its row keeps its
    probe result and tools with no mark.
    """
    from kiro_crew.connections import status
    from kiro_crew.dashboard.handlers import mcp_probe

    _configured_slack(tmp_path, monkeypatch, entry, preview=False)

    slack = {s["slug"]: s for s in await status.collect_connection_statuses()}[SLUG]
    assert slack["needsCrewSignIn"] is True
    assert "crewSignInNotOwned" not in slack
    rows = [dict(entry)]
    mcp_probe._annotate_unconfigured_connection(rows)
    assert rows[0].get("connectionNeedsCrewSignIn") is (True if entry is _PLAIN else None)


@pytest.mark.parametrize("header", ["Authorization", "x-api-key"])
def test_a_row_with_its_own_credential_header_never_needs_crew_sign_in(
    tmp_path, monkeypatch, header
):
    """App saved, Crew's sign-in off: a row with its own credential gets no mark.

    A stale mark from a cached row goes too.
    """
    from kiro_crew.dashboard.handlers import mcp_probe

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(
        json.dumps({"connections": {"oauth_clients": {SLUG: {"client_id": "123.456"}}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("KIROCREW_CONNECTIONS_SLACK_CLIENT_SECRET", "sec")
    monkeypatch.setattr(crew_oauth, "enabled", lambda: False)
    rows = [
        {"name": SLUG, "url": url, "headers": {header: "x"}, "connectionNeedsCrewSignIn": True}
        for url in [SLACK_URL, *_url_variants(SLACK_URL)]
    ]
    rows.append({"name": SLUG, "url": SLACK_URL, "headers": {"X-Region": "us"}})

    mcp_probe._annotate_unconfigured_connection(rows)

    assert [row.get("connectionNeedsCrewSignIn") for row in rows] == [None, None, None, True]
