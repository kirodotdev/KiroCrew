"""Crew-owned OAuth for an owner-added remote MCP server (no Connections card).

Every vendor answer comes from an in-memory table shaped like Slack's live
metadata: ``_post`` and the discovery fetch are replaced, so nothing here reaches
the network.
"""

from __future__ import annotations

import asyncio
import urllib.parse

import pytest

from kiro_crew.connections import crew_oauth, crew_oauth_dcr
from kiro_crew.connections.crew_oauth import CrewOAuthError, TokenStore
from kiro_crew.connections.crew_oauth_dcr import ClientStore

MCP = "https://mcp.slack.com/mcp"
ISS = "https://mcp.slack.com"
RM = "https://mcp.slack.com/.well-known/oauth-protected-resource"
AS_DOC = {
    "issuer": ISS,
    "authorization_endpoint": "https://slack.com/oauth/v2_user/authorize",
    "token_endpoint": "https://slack.com/api/oauth.v2.user.access",
    "token_endpoint_auth_methods_supported": ["client_secret_post"],
    "code_challenge_methods_supported": ["S256"],
}
TOKEN_URL = AS_DOC["token_endpoint"]


class Vendor:
    def __init__(self) -> None:
        self.docs: dict[str, dict] = {
            RM: {"resource": ISS, "authorization_servers": [ISS]},
            f"{ISS}/.well-known/oauth-authorization-server": dict(AS_DOC),
        }
        self.posts: dict[str, list] = {MCP: [(401, {}, f'Bearer resource_metadata="{RM}"')]}
        self.calls: list[tuple] = []

    async def fetch(self, url):
        self.calls.append(("GET", url))
        if url not in self.docs:
            raise ValueError(f"{url} returned HTTP 404")
        return self.docs[url]

    async def post(self, url, *, form=None, body=None, auth=None, headers=None):
        self.calls.append(("POST", url, form, body, auth))
        answers = self.posts.get(url) or [(404, {}, "")]
        return answers.pop(0) if len(answers) > 1 else answers[0]


@pytest.fixture()
def vendor(tmp_path, monkeypatch):
    import kiro_crew.config.paths as paths
    import kiro_crew.mcp_oauth_discovery as disc

    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path)
    v = Vendor()
    monkeypatch.setattr(crew_oauth_dcr, "_post", v.post)
    monkeypatch.setattr(disc, "_fetch_json", v.fetch)
    monkeypatch.setattr(crew_oauth_dcr, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(crew_oauth, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(crew_oauth_dcr, "_public_origin", lambda: "")
    return v


def test_the_key_binds_name_and_url_and_never_collides_with_a_card():
    key = crew_oauth.custom_key("slack", MCP)
    assert crew_oauth.is_custom_key(key) and key == crew_oauth.custom_key("slack", MCP)
    assert key != crew_oauth.custom_key("slack", MCP + "/other")
    assert key != crew_oauth.custom_key("slack2", MCP)
    assert not any(crew_oauth.is_custom_key(s) for s in crew_oauth.SUPPORTED_SLUGS)
    assert not crew_oauth.is_custom_key("custom-xyz")
    assert crew_oauth.token_entry_name(key).startswith("CONNECTIONS_CUSTOM_")


def test_an_owner_public_client_is_used_as_given_with_discovered_endpoints(vendor):
    key = crew_oauth.custom_key("slack", MCP)
    redirect = "http://localhost:48106/callback"
    oauth = {"clientId": "123.456", "redirectUri": redirect, "oauthScopes": ["search:read.public"]}
    plan = asyncio.run(crew_oauth_dcr.prepare_custom(key, MCP, oauth))
    client = plan.client
    assert (client.client_id, client.client_secret, client.auth_method) == ("123.456", None, "none")
    assert client.redirect_uri == redirect and client.issuer == ISS
    assert client.authorize == AS_DOC["authorization_endpoint"] and client.token == TOKEN_URL
    assert plan.scope == "search:read.public"
    # The owner's scopes win over a challenge's.
    vendor.posts[MCP] = [(401, {}, f'Bearer resource_metadata="{RM}", scope="chat:write"')]
    again = asyncio.run(crew_oauth_dcr.prepare_custom(key, MCP, oauth))
    assert again.scope == "search:read.public"
    # No DCR at an AS that offers none; the client waits for its code swap.
    assert not any(c[0] == "POST" and "register" in c[1] for c in vendor.calls)
    assert ClientStore().read(key)[0] is None
    vendor.posts[TOKEN_URL] = [(200, {"access_token": "at", "refresh_token": "rt"}, "")]
    asyncio.run(crew_oauth_dcr.exchange(client, "code", "verifier"))
    _, _, form, _, auth = vendor.calls[-1]
    assert form["client_id"] == "123.456" and "client_secret" not in form and auth is None


def test_an_owner_secret_goes_to_the_vault_and_the_form_post(vendor):
    key = crew_oauth.custom_key("gh", MCP)
    oauth = {"clientId": "Iv1.abc", "clientSecret": "s3cr3t"}
    client = asyncio.run(crew_oauth_dcr.prepare_custom(key, MCP, oauth)).client
    assert client.auth_method == "client_secret_post"
    assert client.redirect_uri == "http://127.0.0.1:48100/callback"
    crew_oauth_dcr.keep_client(key, client, crew_oauth.Tokens("at", "rt", None, ""))
    assert ClientStore().read(key)[0].client_secret == "s3cr3t"
    assert TokenStore().read(key)[0].access == "at"
    del vendor.docs[f"{ISS}/.well-known/oauth-authorization-server"][
        "token_endpoint_auth_methods_supported"
    ]
    # An AS that lists no method (GitHub) gets the form post.
    assert crew_oauth_dcr._secret_method(
        vendor.docs[f"{ISS}/.well-known/oauth-authorization-server"]
    ) == ("client_secret_post")


def test_without_a_client_id_crew_registers_by_dcr(vendor):
    doc = vendor.docs[f"{ISS}/.well-known/oauth-authorization-server"]
    doc["registration_endpoint"] = "https://slack.com/register"
    doc["token_endpoint_auth_methods_supported"] = ["none"]
    vendor.posts["https://slack.com/register"] = [(201, {"client_id": "dcr-1"}, "")]
    key = crew_oauth.custom_key("x", MCP)
    plan = asyncio.run(crew_oauth_dcr.prepare_custom(key, MCP, {}))
    assert plan.client.client_id == "dcr-1"


def test_without_a_client_id_or_dcr_the_sign_in_fails_closed(vendor):
    with pytest.raises(CrewOAuthError) as err:
        asyncio.run(crew_oauth_dcr.prepare_custom(crew_oauth.custom_key("x", MCP), MCP, None))
    assert err.value.code == "dcr_unsupported"


@pytest.mark.parametrize(
    "redirect",
    ["https://example.com/callback", "http://evil.example:8080/cb", "http://localhost/cb"],
)
def test_a_redirect_crew_cannot_receive_is_refused(vendor, redirect):
    oauth = {"clientId": "c", "redirectUri": redirect}
    with pytest.raises(CrewOAuthError) as err:
        asyncio.run(crew_oauth_dcr.prepare_custom(crew_oauth.custom_key("x", MCP), MCP, oauth))
    assert err.value.code == "redirect_unsupported"


@pytest.mark.parametrize(
    "redirect, expected",
    [
        ("http://127.0.0.1:48100/callback", None),
        ("https://crew.example/api/connections/oauth/callback", None),
        ("http://localhost:48106/callback", (48106, "/callback")),
        ("http://127.0.0.1:3118/oauth/cb", (3118, "/oauth/cb")),
    ],
)
def test_only_an_owner_registered_loopback_gets_its_own_listener(redirect, expected):
    assert crew_oauth_dcr.own_listener(redirect) == expected


def test_a_custom_sign_in_runs_end_to_end_on_the_owners_port_and_refreshes(vendor, monkeypatch):
    import aiohttp

    port = 48107
    asked: list[tuple[int, str]] = []
    real_bind = crew_oauth.SignIn._bind

    async def _bind(self, wanted, path):
        # The owner's registered port is asserted; the socket takes any free one.
        asked.append((wanted, path))
        await real_bind(self, 0, path)

    monkeypatch.setattr(crew_oauth.SignIn, "_bind", _bind)
    name = "slack"
    key = crew_oauth.custom_key(name, MCP)
    custom = {
        "slug": name,
        "mcp_url": MCP,
        "entry": {
            "url": MCP,
            "oauth": {"clientId": "c1", "redirectUri": f"http://localhost:{port}/callback"},
        },
    }
    vendor.posts[TOKEN_URL] = [
        (200, {"access_token": "at", "refresh_token": "rt", "expires_in": 60}, ""),
        (200, {"access_token": "at2", "refresh_token": "rt2", "expires_in": 3600}, ""),
    ]

    async def _run():
        sign_in = crew_oauth.SignIn(key, (), custom)
        url = await sign_in.start()
        assert asked == [(port, "/callback")] and "runner" not in crew_oauth_dcr._listener
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        assert query["redirect_uri"] == [f"http://localhost:{port}/callback"]
        async with aiohttp.ClientSession() as s:
            params = {"state": query["state"][0], "code": "c"}
            target = f"http://127.0.0.1:{sign_in.bound_port}/callback"
            async with s.get(target, params=params) as r:
                assert r.status == 200
        try:
            await sign_in.finish()
        finally:
            await sign_in.shutdown()
        return await crew_oauth.access_token(key, force=True)

    assert asyncio.run(_run()) == "at2"
    held = TokenStore().read(key)[0]
    assert (held.access, held.refresh, held.client) == ("at2", "rt2", "c1")
    form = vendor.calls[-1][2]
    assert form["grant_type"] == "refresh_token" and form["client_id"] == "c1"


def test_a_custom_connect_is_keyed_by_name_and_its_consent_host_is_the_discovered_one(
    vendor, monkeypatch
):
    from kiro_crew.connections import mint

    monkeypatch.setattr(crew_oauth_dcr, "DCR_CALLBACK_PORT", 0)
    monkeypatch.setattr(mint, "_mints", {})
    # The real banner gate: slack.com's user consent page is on no builtin list.
    custom = {"slug": "slack", "mcp_url": MCP, "entry": {"url": MCP, "oauth": {"clientId": "c1"}}}

    async def _run():
        await crew_oauth.start_mint_row(crew_oauth.custom_key("slack", MCP), (), custom)
        row = mint._mints["slack"]
        assert row["state"] == "waiting", row.get("reason")
        assert row["oauth_url"].startswith(AS_DOC["authorization_endpoint"] + "?")
        row["watcher"].cancel()
        await row["client"].shutdown()

    asyncio.run(_run())


def test_a_query_string_never_reaches_the_resource_or_the_audit(vendor, monkeypatch):
    from kiro_crew.connections import mint

    monkeypatch.setattr(crew_oauth_dcr, "DCR_CALLBACK_PORT", 0)
    monkeypatch.setattr(mint, "_mints", {})
    audited: list[str] = []
    import kiro_crew.security.exfil_oauth as exfil_oauth

    monkeypatch.setattr(
        exfil_oauth, "_emit_oauth_discovered_endpoint_event", lambda h, p, u: audited.append(u)
    )
    exfil_oauth._OAUTH_DISCOVERED_ENDPOINTS.clear()
    raw = MCP + "?api_key=SECRET"
    vendor.posts[raw] = vendor.posts[MCP]
    custom = {"slug": "slack", "mcp_url": raw, "entry": {"url": raw, "oauth": {"clientId": "c1"}}}

    async def _run():
        await crew_oauth.start_mint_row(crew_oauth.custom_key("slack", raw), (), custom)
        row = mint._mints["slack"]
        assert row["state"] == "waiting", row.get("reason")
        assert "SECRET" not in row["oauth_url"]
        assert row["client"]._dcr.client.resource == MCP
        row["watcher"].cancel()
        await row["client"].shutdown()

    asyncio.run(_run())
    assert audited == [MCP]
    assert not any("SECRET" in str(c) for c in vendor.calls)


@pytest.fixture()
def owner(monkeypatch):
    """An owner-recorded ``slack`` at :data:`MCP`, flag on, KAS chats."""
    import kiro_crew.config.loader as loader
    from kiro_crew.connections.owner_servers import CONFIG_ROOT_KEY, CONFIG_SERVERS_KEY

    held: set[str] = set()
    config = {CONFIG_ROOT_KEY: {CONFIG_SERVERS_KEY: {"slack": {"url": MCP}}}}
    monkeypatch.setattr(loader, "read_config_for_update", lambda: config)
    monkeypatch.setattr(crew_oauth, "holds_grant", lambda slug: slug in held)
    monkeypatch.setattr(crew_oauth, "enabled", lambda: True)
    monkeypatch.setattr(crew_oauth, "_chat_backend", lambda: "kas")
    return held


def test_an_owner_server_is_bridged_only_once_crew_holds_its_grant(owner):
    entry = {"url": MCP}
    assert crew_oauth.routed_slug("slack", entry, None, backend="kas") == ""
    owner.add(crew_oauth.custom_key("slack", MCP))
    assert crew_oauth.routed_slug("slack", entry, None, backend="kas") == crew_oauth.custom_key(
        "slack", MCP
    )
    # kiro-cli keeps its native path.
    assert crew_oauth.routed_slug("slack", entry, None, backend="kiro") == ""
    # Its own credential header keeps its own path.
    assert (
        crew_oauth.routed_slug("slack", {"url": MCP, "headers": {"Authorization": "x"}}, None) == ""
    )


def test_a_re_pointed_or_unrecorded_server_is_never_bridged(owner):
    owner.add(crew_oauth.custom_key("slack", MCP + "x"))
    owner.add(crew_oauth.custom_key("other", MCP))
    assert crew_oauth.routed_slug("slack", {"url": MCP + "x"}, None, backend="kas") == ""
    assert crew_oauth.routed_slug("other", {"url": MCP}, None, backend="kas") == ""


def test_the_dashboard_sign_in_goes_to_crew_only_with_the_flag_and_a_bridged_chat(
    owner, monkeypatch
):
    assert crew_oauth.signs_in_custom({"url": MCP})
    assert not crew_oauth.signs_in_custom({"url": MCP, "headers": {"X-Api-Key": "k"}})
    monkeypatch.setattr(crew_oauth, "_chat_backend", lambda: "kiro")
    assert not crew_oauth.signs_in_custom({"url": MCP})
    monkeypatch.setattr(crew_oauth, "_chat_backend", lambda: "kas")
    monkeypatch.setattr(crew_oauth, "enabled", lambda: False)
    assert not crew_oauth.signs_in_custom({"url": MCP})


def test_a_legacy_sse_server_keeps_its_own_path(owner):
    owner.add(crew_oauth.custom_key("slack", MCP))
    entry = {"url": MCP, "type": "sse"}
    assert crew_oauth.routed_slug("slack", entry, None, backend="kas") == ""
    assert not crew_oauth.signs_in_custom(entry)
    assert crew_oauth.routed_slug("slack", {"url": MCP, "type": "http"}, None, backend="kas")


def test_a_declined_switch_keeps_the_client_that_issued_the_pair(vendor):
    key = crew_oauth.custom_key("slack", MCP)
    first = asyncio.run(crew_oauth_dcr.prepare_custom(key, MCP, {"clientId": "A"})).client
    crew_oauth_dcr.keep_client(key, first, crew_oauth.Tokens("at", "rt", None, "", client="A"))
    # The owner switches to client B, then declines consent: nothing is stored.
    asyncio.run(crew_oauth_dcr.prepare_custom(key, MCP, {"clientId": "B"}))
    assert ClientStore().read(key)[0].client_id == "A"


def test_a_grant_runs_the_post_grant_hook_once_and_a_failure_does_not(monkeypatch):
    from kiro_crew.connections import mint

    calls: list[str] = []

    class _SignIn:
        slug = crew_oauth.custom_key("slack", MCP)

        def __init__(self, error=None):
            self.error = error

        async def finish(self):
            if self.error:
                raise CrewOAuthError(self.error)

        async def shutdown(self):
            pass

    monkeypatch.setattr(mint, "_mints", {"slack": {"token": "t"}})
    asyncio.run(crew_oauth._await_consent("slack", "t", _SignIn(), lambda: calls.append("x")))
    assert calls == ["x"] and mint._mints["slack"]["state"] == "granted"
    asyncio.run(
        crew_oauth._await_consent(
            "slack", "t", _SignIn("consent_declined"), lambda: calls.append("y")
        )
    )
    assert calls == ["x"] and mint._mints["slack"]["state"] == "failed"


def test_the_post_grant_hook_marks_every_live_session_without_a_reset():
    from kiro_crew.dashboard.handlers.connections_custom_oauth import (
        _mark_live_sessions_for_new_bridge,
    )

    class _Provider:
        mcp_config_wire_reinject = True
        marked = 0

        def mark_mcp_config_changed(self):
            self.marked += 1

    live, warm = _Provider(), _Provider()

    class _Sessions:
        fenced = 0

        def active_providers(self):
            return [live]

        def warm_providers(self):
            return [warm]

        def mark_mcp_config_changed(self):
            self.fenced += 1

    sessions = _Sessions()

    class _State:
        pass

    state = _State()
    state.sessions = sessions
    _mark_live_sessions_for_new_bridge({"state": state})
    assert (live.marked, warm.marked, sessions.fenced) == (1, 1, 1)


def test_an_entry_at_a_registry_providers_url_keeps_the_provider_path(owner, monkeypatch):
    """A card is a preset over this path: its url is never claimed twice, whatever the name."""
    import kiro_crew.config.loader as loader
    from kiro_crew.connections.owner_servers import CONFIG_ROOT_KEY, CONFIG_SERVERS_KEY
    from kiro_crew.connections.registry import get_provider

    url = str(get_provider("github")["mcp_url"])
    config = {CONFIG_ROOT_KEY: {CONFIG_SERVERS_KEY: {"my-github": {"url": url}}}}
    monkeypatch.setattr(loader, "read_config_for_update", lambda: config)
    owner.add(crew_oauth.custom_key("my-github", url))
    for spelled in (url, url.rstrip("/")):
        assert crew_oauth.routed_slug("my-github", {"url": spelled}, None, backend="kas") == ""
        assert not crew_oauth.signs_in_custom({"url": spelled})


def test_the_editors_top_level_scopes_are_the_ones_asked_for(vendor, monkeypatch):
    """The custom-server editor stores ``scopes`` beside ``oauth``, not inside it."""
    monkeypatch.setattr(crew_oauth_dcr, "DCR_CALLBACK_PORT", 0)
    vendor.posts[MCP] = [(401, {}, f'Bearer resource_metadata="{RM}", scope="chat:write"')]
    entry = {"url": MCP, "scopes": ["search:read.public"], "oauth": {"clientId": "c1"}}
    custom = {"slug": "slack", "mcp_url": MCP, "entry": entry}

    async def _run():
        sign_in = crew_oauth.SignIn(crew_oauth.custom_key("slack", MCP), (), custom)
        url = await sign_in.start()
        await sign_in.shutdown()
        return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["scope"]

    assert asyncio.run(_run()) == ["search:read.public"]


def _signed_in_with(vendor, client_id, token_answer):
    """One full owner-client sign-in on the shared port; the raised code or ``None``."""
    import aiohttp

    custom = {
        "slug": "slack",
        "mcp_url": MCP,
        "entry": {"url": MCP, "oauth": {"clientId": client_id}},
    }
    vendor.posts[TOKEN_URL] = [token_answer]

    async def _run():
        sign_in = crew_oauth.SignIn(crew_oauth.custom_key("slack", MCP), (), custom)
        url = await sign_in.start()
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]
        async with aiohttp.ClientSession() as s:
            target = f"http://127.0.0.1:{sign_in.bound_port}/callback"
            async with s.get(target, params={"state": state, "code": "c"}) as r:
                assert r.status == 200
        try:
            await sign_in.finish()
        except CrewOAuthError as exc:
            return exc.code
        finally:
            await sign_in.shutdown()
        return None

    return asyncio.run(_run())


def test_a_refused_replacement_client_keeps_the_working_one(vendor, monkeypatch):
    monkeypatch.setattr(crew_oauth_dcr, "DCR_CALLBACK_PORT", 0)
    key = crew_oauth.custom_key("slack", MCP)
    ok = (200, {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}, "")
    assert _signed_in_with(vendor, "A", ok) is None
    refused = (400, {"error": "invalid_client"}, "")
    assert _signed_in_with(vendor, "B", refused) == "invalid_client"
    assert ClientStore().read(key)[0].client_id == "A"
    assert TokenStore().read(key)[0].client == "A"
    # The client that issued the pair is forgotten when it is the one refused.
    assert _signed_in_with(vendor, "A", refused) == "invalid_client"
    assert ClientStore().read(key)[0] is None


def test_a_failed_write_keeps_both_previous_entries(vendor, monkeypatch):
    from kiro_crew.secrets import SecretVault

    monkeypatch.setattr(crew_oauth_dcr, "DCR_CALLBACK_PORT", 0)
    key = crew_oauth.custom_key("slack", MCP)
    ok = (200, {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}, "")
    assert _signed_in_with(vendor, "A", ok) is None
    real = SecretVault._write_store

    def _full_disk(self, mutate):
        mutate(self._load_entries())  # the replacement is built, then the write fails
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(SecretVault, "_write_store", _full_disk)
    with pytest.raises(OSError):
        _signed_in_with(vendor, "B", (200, {"access_token": "at2", "refresh_token": "rt2"}, ""))
    monkeypatch.setattr(SecretVault, "_write_store", real)
    assert ClientStore().read(key)[0].client_id == "A"
    assert TokenStore().read(key)[0].access == "at"


def test_a_moved_dcr_redirect_stages_the_new_client_and_keeps_the_grant(vendor, monkeypatch):
    """Registering again is staged: the working client and pair stay until a swap succeeds."""
    doc = vendor.docs[f"{ISS}/.well-known/oauth-authorization-server"]
    doc["registration_endpoint"] = "https://slack.com/register"
    doc["token_endpoint_auth_methods_supported"] = ["none"]
    vendor.posts["https://slack.com/register"] = [
        (201, {"client_id": "old"}, ""),
        (201, {"client_id": "new"}, ""),
    ]
    key = crew_oauth.custom_key("x", MCP)
    first = asyncio.run(crew_oauth_dcr.prepare_custom(key, MCP, {})).client
    assert ClientStore().read(key)[0] is None  # staged, not stored
    crew_oauth_dcr.keep_client(key, first, crew_oauth.Tokens("at", "rt", None, "", client="old"))
    moved = {"redirectUri": "http://localhost:3999/callback"}
    plan = asyncio.run(crew_oauth_dcr.prepare_custom(key, MCP, moved))
    assert plan.client.client_id == "new"
    assert ClientStore().read(key)[0].client_id == "old"
    assert TokenStore().read(key)[0].access == "at"
