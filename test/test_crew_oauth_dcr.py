"""Crew-owned OAuth for DCR cards: discovery, registration, tokens, revoke, callback.

Every vendor answer comes from an in-memory table: ``_post`` and the discovery
fetch are replaced, so nothing here reaches the network.
"""

from __future__ import annotations

import asyncio
import base64
import urllib.parse

import pytest

from kiro_crew.connections import crew_oauth, crew_oauth_dcr, registry
from kiro_crew.connections.crew_oauth import CrewOAuthError, Tokens, TokenStore
from kiro_crew.connections.crew_oauth_dcr import ClientStore
from kiro_crew.testing.clock import ManualClock

STRIPE_MCP = "https://mcp.stripe.com"
STRIPE_ISS = "https://access.stripe.com/mcp"
ATL_MCP = "https://mcp.atlassian.com/v2/mcp"
ATL_ISS = "https://auth.atlassian.com/VCeDsk8ZHncYF1g234fKtc4lNipbBhu3"


def _as_doc(issuer: str, base: str, **over):
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "revocation_endpoint": f"{base}/revoke",
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        **over,
    }


class Vendor:
    """GET documents and POST answers by URL; records every call."""

    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}
        self.posts: dict[str, list] = {}
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

    def card(self, mcp, issuer, base, as_path, **as_over):
        rm = f"{urllib.parse.urlsplit(mcp).scheme}://{urllib.parse.urlsplit(mcp).netloc}/rm"
        self.posts[mcp] = [(401, {}, f'Bearer resource_metadata="{rm}", error="invalid_token"')]
        self.docs[rm] = {"resource": mcp, "authorization_servers": [issuer]}
        self.docs[as_path] = _as_doc(issuer, base, **as_over)
        self.posts[f"{base}/register"] = [(201, {"client_id": "cid-1"}, "")]


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
    # A local gateway: no public URL configured.
    monkeypatch.setattr(crew_oauth_dcr, "_public_origin", lambda: "")
    v.card(
        STRIPE_MCP,
        STRIPE_ISS,
        "https://access.stripe.com/mcp/oauth2",
        "https://access.stripe.com/.well-known/oauth-authorization-server/mcp",
    )
    return v


def _registered(vendor):
    return [c for c in vendor.calls if c[0] == "POST" and c[1].endswith("/register")]


# --- discovery and registration ----------------------------------------------


def test_a_public_card_registers_kiro_crew_once_and_asks_for_resource(vendor):
    plan = asyncio.run(crew_oauth_dcr.prepare("stripe"))
    (call,) = _registered(vendor)
    body = call[3]
    assert body["client_name"] == "Kiro Crew"
    assert body["redirect_uris"] == ["http://127.0.0.1:48100/callback"]
    assert body["token_endpoint_auth_method"] == "none"
    assert plan.client.client_id == "cid-1" and plan.client.client_secret is None
    query = urllib.parse.parse_qs(
        urllib.parse.urlsplit(crew_oauth_dcr.authorize_url(plan, "st", "ch")).query
    )
    assert query["resource"] == [STRIPE_MCP]
    assert query["code_challenge_method"] == ["S256"] and query["state"] == ["st"]
    # A second sign-in reuses the stored client.
    asyncio.run(crew_oauth_dcr.prepare("stripe"))
    assert len(_registered(vendor)) == 1


def test_a_path_issuer_is_discovered_and_offline_access_is_asked_when_listed(vendor):
    vendor.card(
        ATL_MCP,
        ATL_ISS,
        "https://auth.atlassian.com",
        "https://auth.atlassian.com/.well-known/oauth-authorization-server/VCeDsk8ZHncYF1g234fKtc4lNipbBhu3",
        scopes_supported=["read", "offline_access"],
    )
    plan = asyncio.run(crew_oauth_dcr.prepare("atlassian"))
    assert plan.client.issuer == ATL_ISS
    assert "offline_access" in plan.scope.split()


def test_a_confidential_only_card_sends_basic_and_keeps_the_secret_out_of_the_form(vendor):
    vendor.docs["https://access.stripe.com/.well-known/oauth-authorization-server/mcp"][
        "token_endpoint_auth_methods_supported"
    ] = ["client_secret_basic", "client_secret_post"]
    vendor.posts["https://access.stripe.com/mcp/oauth2/register"] = [
        (201, {"client_id": "cid-2", "client_secret": "s3cr:t"}, "")
    ]
    vendor.posts["https://access.stripe.com/mcp/oauth2/token"] = [
        (200, {"access_token": "at", "refresh_token": "rt", "expires_in": 60}, "")
    ]
    plan = asyncio.run(crew_oauth_dcr.prepare("stripe"))
    assert _registered(vendor)[0][3]["token_endpoint_auth_method"] == "client_secret_basic"
    asyncio.run(crew_oauth_dcr.exchange(plan.client, "code", "verifier"))
    _, _, form, _, auth = vendor.calls[-1]
    assert "client_secret" not in form and form["resource"] == STRIPE_MCP
    assert auth is not None and base64.b64decode(auth.encode().split()[1]) == b"cid-2:s3cr%3At"


def test_a_secret_method_without_a_secret_is_refused(vendor):
    vendor.docs["https://access.stripe.com/.well-known/oauth-authorization-server/mcp"][
        "token_endpoint_auth_methods_supported"
    ] = ["client_secret_post"]
    with pytest.raises(CrewOAuthError) as err:
        asyncio.run(crew_oauth_dcr.prepare("stripe"))
    assert err.value.code == "registration_refused"


def test_a_registry_client_name_overrides_the_default(vendor, monkeypatch):
    real = crew_oauth_dcr.get_provider
    monkeypatch.setattr(
        crew_oauth_dcr, "get_provider", lambda s: {**real(s), "dcr_client_name": "kiro"}
    )
    asyncio.run(crew_oauth_dcr.prepare("stripe"))
    assert _registered(vendor)[0][3]["client_name"] == "kiro"


def test_a_moved_issuer_fails_closed_and_the_new_host_is_never_fetched(vendor):
    vendor.docs["https://mcp.stripe.com/rm"]["authorization_servers"] = ["https://evil.example"]
    with pytest.raises(CrewOAuthError) as err:
        asyncio.run(crew_oauth_dcr.prepare("stripe"))
    assert err.value.code == "issuer_changed"
    assert not any("evil.example" in c[1] for c in vendor.calls)
    assert _registered(vendor) == []


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"code_challenge_methods_supported": ["plain"]}, "no_s256"),
        ({"code_challenge_methods_supported": None}, "no_s256"),
        ({"registration_endpoint": None}, "dcr_unsupported"),
        ({"issuer": "https://access.stripe.com/other"}, "issuer_changed"),
    ],
)
def test_metadata_without_what_crew_needs_fails_closed(vendor, over, code):
    doc = vendor.docs["https://access.stripe.com/.well-known/oauth-authorization-server/mcp"]
    doc.update(over)
    with pytest.raises(CrewOAuthError) as err:
        asyncio.run(crew_oauth_dcr.prepare("stripe"))
    assert err.value.code == code and _registered(vendor) == []


def test_no_metadata_never_falls_back_to_guessed_endpoints(vendor):
    vendor.docs.clear()
    with pytest.raises(CrewOAuthError) as err:
        asyncio.run(crew_oauth_dcr.prepare("stripe"))
    assert err.value.code == "discovery_failed"
    assert not any(c[1].endswith(("/authorize", "/token", "/register")) for c in vendor.calls)


def test_a_moved_redirect_registers_again_and_drops_the_old_pair(vendor, tmp_path, monkeypatch):
    asyncio.run(crew_oauth_dcr.prepare("stripe"))
    TokenStore().replace("stripe", Tokens("a", "r", None, ""))
    monkeypatch.setattr(crew_oauth_dcr, "DCR_CALLBACK_PORT", 48199)
    plan = asyncio.run(crew_oauth_dcr.prepare("stripe"))
    assert len(_registered(vendor)) == 2
    assert plan.client.redirect_uri == "http://127.0.0.1:48199/callback"
    assert TokenStore().read("stripe") is None


# --- refresh, revoke, iss ------------------------------------------------------


def _signed_in(vendor):
    asyncio.run(crew_oauth_dcr.prepare("stripe"))
    TokenStore().replace("stripe", Tokens("a", "r", 0.0, ""))


def test_invalid_client_forgets_the_registration_so_the_next_connect_registers(vendor):
    _signed_in(vendor)
    vendor.posts["https://access.stripe.com/mcp/oauth2/token"] = [
        (401, {"error": "invalid_client"}, "")
    ]
    with pytest.raises(CrewOAuthError) as err:
        asyncio.run(crew_oauth.access_token("stripe", force=True))
    assert err.value.code == "needs_auth"
    assert ClientStore().read("stripe") == (None, None)
    assert TokenStore().read("stripe") is None
    asyncio.run(crew_oauth_dcr.prepare("stripe"))
    assert len(_registered(vendor)) == 2


@pytest.mark.parametrize(
    ("error", "forgotten"), [("invalid_client", True), ("invalid_grant", False)]
)
def test_a_code_swap_refused_for_the_client_forgets_it(vendor, monkeypatch, error, forgotten):
    monkeypatch.setattr(crew_oauth_dcr, "DCR_CALLBACK_PORT", 0)
    vendor.posts["https://access.stripe.com/mcp/oauth2/token"] = [(401, {"error": error}, "")]

    async def _run():
        sign_in = crew_oauth.SignIn("stripe")
        await sign_in.start()
        sign_in._done.set_result("code")
        try:
            await sign_in.finish()
        finally:
            await sign_in.shutdown()

    with pytest.raises(CrewOAuthError) as err:
        asyncio.run(_run())
    assert err.value.code == error
    assert (ClientStore().read("stripe")[0] is None) is forgotten


def test_invalid_grant_is_needs_auth_and_keeps_client_and_pair(vendor):
    _signed_in(vendor)
    vendor.posts["https://access.stripe.com/mcp/oauth2/token"] = [
        (400, {"error": "invalid_grant"}, "")
    ]
    with pytest.raises(CrewOAuthError) as err:
        asyncio.run(crew_oauth.access_token("stripe", force=True))
    assert err.value.code == "needs_auth"
    assert ClientStore().read("stripe")[0] is not None
    assert TokenStore().read("stripe") is not None


def test_a_refresh_sends_resource_and_commits_the_new_pair(vendor):
    _signed_in(vendor)
    vendor.posts["https://access.stripe.com/mcp/oauth2/token"] = [
        (200, {"access_token": "a2", "refresh_token": "r2", "expires_in": 3600}, "")
    ]
    assert asyncio.run(crew_oauth.access_token("stripe", force=True)) == "a2"
    form = vendor.calls[-1][2]
    assert form["resource"] == STRIPE_MCP and form["client_id"] == "cid-1"
    assert TokenStore().read("stripe")[0].refresh == "r2"


def test_disconnect_revokes_with_rfc7009_and_keeps_the_client(vendor):
    _signed_in(vendor)
    vendor.posts["https://access.stripe.com/mcp/oauth2/revoke"] = [(200, {}, "")]
    assert asyncio.run(crew_oauth.disconnect("stripe")) == {"removed": True, "revoked": True}
    form = vendor.calls[-1][2]
    assert form["token"] == "r" and form["token_type_hint"] == "refresh_token"
    assert ClientStore().read("stripe")[0] is not None


def test_without_a_revocation_endpoint_disconnect_deletes_locally(vendor):
    vendor.docs["https://access.stripe.com/.well-known/oauth-authorization-server/mcp"][
        "revocation_endpoint"
    ] = None
    _signed_in(vendor)
    assert asyncio.run(crew_oauth.disconnect("stripe")) == {"removed": True, "revoked": False}


TT_MCP = "https://mcp.ticktick.com"
TT_TOKEN = "https://api.ticktick.com/oauth/token"


def _ticktick(vendor, expires_at):
    """TickTick's live shape: revocation_endpoint is the token URL, no refresh token."""
    vendor.card(
        TT_MCP,
        "https://ticktick.com",
        "https://api.ticktick.com/oauth",
        "https://ticktick.com/.well-known/oauth-authorization-server",
        revocation_endpoint=TT_TOKEN,
    )
    asyncio.run(crew_oauth_dcr.prepare("ticktick"))
    TokenStore().replace("ticktick", Tokens("a", None, expires_at, "tasks:read tasks:write"))


def test_a_ticktick_grant_without_refresh_is_served_to_its_expiry(vendor, monkeypatch):
    _ticktick(vendor, 1_000.0)
    # 60 s before expiry, inside the refresh skew: still served, the vendor untouched.
    ManualClock(start=940.0).install(monkeypatch, crew_oauth)
    assert asyncio.run(crew_oauth.access_token("ticktick")) == "a"
    assert not any(c[1] == TT_TOKEN for c in vendor.calls)


def test_an_expired_ticktick_grant_is_needs_auth_and_reads_not_connected(vendor, monkeypatch):
    _ticktick(vendor, 1_000.0)
    ManualClock(start=1_000.0).install(monkeypatch, crew_oauth)
    with pytest.raises(CrewOAuthError) as err:
        asyncio.run(crew_oauth.access_token("ticktick"))
    assert err.value.code == "needs_auth"
    assert not any(c[1] == TT_TOKEN for c in vendor.calls)
    # The card falls back to Connect; the next Connect reuses the client.
    assert crew_oauth.holds_grant("ticktick") is False
    assert ClientStore().read("ticktick")[0] is not None


def test_a_ticktick_disconnect_whose_revoke_is_refused_deletes_locally(vendor):
    _ticktick(vendor, None)
    # Live answer of the advertised revocation_endpoint to an RFC 7009 form: 401.
    vendor.posts[TT_TOKEN] = [(401, {}, "")]
    assert asyncio.run(crew_oauth.disconnect("ticktick")) == {"removed": True, "revoked": False}
    assert vendor.calls[-1][1] == TT_TOKEN
    assert vendor.calls[-1][2]["token_type_hint"] == "access_token"


def test_the_callback_iss_is_checked(vendor):
    client = asyncio.run(crew_oauth_dcr.prepare("stripe")).client
    assert crew_oauth_dcr.iss_ok(client, None) is True
    assert crew_oauth_dcr.iss_ok(client, STRIPE_ISS) is True
    assert crew_oauth_dcr.iss_ok(client, "https://evil.example") is False
    strict = crew_oauth_dcr.replace(client, iss_param=True)
    assert crew_oauth_dcr.iss_ok(strict, None) is False


def test_the_challenge_parser_reads_quoted_and_bare_values():
    params = crew_oauth_dcr._challenge_params('Bearer resource_metadata="https://x/rm", scope=read')
    assert params == {"resource_metadata": "https://x/rm", "scope": "read"}
    assert crew_oauth_dcr._challenge_params('Basic realm="x"') == {}


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        # A quoted value keeps an escaped quote and a comma.
        (
            'Bearer error_description="say \\"hi\\", then go"',
            {"error_description": 'say "hi", then go'},
        ),
        ("Bearer scope=read", {"scope": "read"}),
        ('Bearer  Scope  =  "a b" ,  error = x', {"scope": "a b", "error": "x"}),
        ('Basic realm="x", scope=read', {}),
        ('Bearer scope="read", 9bad=1, error=x', {"scope": "read"}),
        ('Bearer scope="unterminated', {}),
    ],
)
def test_the_challenge_reader_cases(header, expected):
    assert crew_oauth_dcr._challenge_params(header) == expected


# --- shared callback ------------------------------------------------------------


def test_one_port_serves_every_pending_sign_in_by_state(monkeypatch):
    import aiohttp

    monkeypatch.setattr(crew_oauth_dcr, "DCR_CALLBACK_PORT", 0)
    seen: list[str] = []

    def _handler(name):
        async def _h(request):
            from aiohttp import web

            seen.append(name)
            return web.Response(text=name)

        return _h

    async def _run():
        port = await crew_oauth_dcr.listen("state-a", _handler("a"))
        assert await crew_oauth_dcr.listen("state-b", _handler("b")) == port
        async with aiohttp.ClientSession() as s:
            for state, want in (("state-b", 200), ("state-a", 200), ("nope", 400)):
                async with s.get(f"http://127.0.0.1:{port}/callback?state={state}") as r:
                    assert r.status == want
        await crew_oauth_dcr.unlisten("state-a")
        assert "runner" in crew_oauth_dcr._listener
        await crew_oauth_dcr.unlisten("state-b")
        assert "runner" not in crew_oauth_dcr._listener

    asyncio.run(_run())
    assert seen == ["b", "a"]


# --- gate, registry, cleanup --------------------------------------------------------


def test_a_dcr_entry_routes_only_at_its_registry_url(monkeypatch):
    monkeypatch.setattr(crew_oauth, "manages", lambda slug: True)
    monkeypatch.setattr(crew_oauth, "custody_on", lambda slug: True)
    assert crew_oauth.routed_slug("stripe", {"url": STRIPE_MCP}, lambda *a: None) == "stripe"
    assert crew_oauth.routed_slug("stripe", {"url": "https://other.example"}, lambda *a: None) == ""
    headers = {"url": STRIPE_MCP, "headers": {"Authorization": "x"}}
    assert crew_oauth.routed_slug("stripe", headers, lambda *a: None) == ""


def test_the_shared_port_is_reserved_in_the_registry():
    raw = {"mode": "preregistered", "confidential": True, "redirect_port": 48100}
    raw["registration_guide"] = "oauth-app-registration/github.md"
    with pytest.raises(registry.RegistryValidationError, match="reserved"):
        registry._validate_auth(raw, 0)
    assert registry.DCR_CALLBACK_PORT == 48100


def test_forget_kas_grant_deletes_only_that_urls_entries(tmp_path):
    from kiro_crew.mcp_grant_kas import forget_kas_grant, kas_connection_hash
    from kiro_crew.secrets import SecretVault

    vault = SecretVault(tmp_path)
    digest = kas_connection_hash(STRIPE_MCP)
    for name in (f"kiro.mcp.{digest}.tokens", f"kiro.mcp.{digest}.client", "kiro.mcp.other.tokens"):
        vault.set_sync(name, "v")
    asyncio.run(forget_kas_grant(STRIPE_MCP, store_root=tmp_path))
    assert vault.list_names() == ["kiro.mcp.other.tokens"]


@pytest.mark.parametrize(
    ("project_dirs", "shared", "unlinked"),
    [(None, (), False), ((), ("x",), False), ((), (), True)],
)
def test_harness_grants_go_only_when_the_full_census_finds_no_sharer(
    monkeypatch, project_dirs, shared, unlinked
):
    import kiro_crew.connections.ownership as ownership
    import kiro_crew.mcp_grant as mcp_grant
    import kiro_crew.mcp_grant_kas as mcp_grant_kas

    forgot: list[str] = []
    gone: list[str] = []
    asked: list = []

    async def _forget(url):
        forgot.append(url)

    async def _sharers(pairs, dirs):
        asked.append(dirs)
        return {
            s: ownership.GrantSharing(shared_with=shared, census_incomplete=False) for s, _ in pairs
        }

    monkeypatch.setattr(mcp_grant_kas, "forget_kas_grant", _forget)
    monkeypatch.setattr(ownership, "grant_sharers_map", _sharers)
    monkeypatch.setattr(mcp_grant, "revoke_local_grant", lambda url: gone.append(url) or [])
    asyncio.run(crew_oauth_dcr.drop_harness_grants("stripe", project_dirs))
    # Both URL-keyed grants (KAS and kiro-cli) follow the same census.
    assert forgot == ([STRIPE_MCP] if unlinked else [])
    assert gone == ([STRIPE_MCP] if unlinked else [])
    assert asked == ([] if project_dirs is None else [project_dirs])


@pytest.mark.parametrize("iss", [None, "https://evil.example"])
def test_a_dcr_sign_in_runs_end_to_end_on_the_shared_port(vendor, monkeypatch, iss):
    import aiohttp

    monkeypatch.setattr(crew_oauth_dcr, "DCR_CALLBACK_PORT", 0)
    dropped: list[str] = []

    async def _drop(slug, project_dirs):
        dropped.append(slug)

    monkeypatch.setattr(crew_oauth_dcr, "drop_harness_grants", _drop)
    vendor.posts["https://access.stripe.com/mcp/oauth2/token"] = [
        (200, {"access_token": "at", "refresh_token": "rt", "expires_in": 60}, "")
    ]

    async def _run():
        sign_in = crew_oauth.SignIn("stripe")
        url = await sign_in.start()
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]
        query = {"state": state, "code": "c", **({"iss": iss} if iss else {})}
        async with aiohttp.ClientSession() as s:
            target = f"http://127.0.0.1:{sign_in.bound_port}/callback"
            async with s.get(target, params=query) as r:
                assert r.status == (400 if iss else 200)
        try:
            await sign_in.finish()
        finally:
            await sign_in.shutdown()
        assert "runner" not in crew_oauth_dcr._listener

    if iss:
        with pytest.raises(CrewOAuthError) as err:
            asyncio.run(_run())
        assert err.value.code == "issuer_mismatch" and dropped == []
        return
    asyncio.run(_run())
    assert TokenStore().read("stripe")[0].access == "at" and dropped == ["stripe"]
    form = vendor.calls[-1][2]
    assert form["grant_type"] == "authorization_code" and form["resource"] == STRIPE_MCP


def test_post_reads_a_bounded_answer_and_never_follows_a_redirect(monkeypatch):
    import aiohttp
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    async def _answer(request):
        if request.path == "/big":
            return web.Response(text='{"a": "' + "x" * (70 * 1024) + '"}')
        if request.path == "/moved":
            raise web.HTTPFound("https://evil.example/")
        return web.json_response(
            {"error": "invalid_token"}, status=401, headers={"WWW-Authenticate": 'Bearer scope="r"'}
        )

    async def _run():
        app = web.Application()
        app.router.add_post("/{tail:.*}", _answer)
        server = TestServer(app)
        await server.start_server()

        async def _session(url):
            return aiohttp.ClientSession(), str(server.make_url(urllib.parse.urlsplit(url).path))

        monkeypatch.setattr(crew_oauth_dcr, "_session", _session)
        try:
            assert await crew_oauth_dcr._post("https://v.example/mcp", body={}) == (
                401,
                {"error": "invalid_token"},
                'Bearer scope="r"',
            )
            assert (await crew_oauth_dcr._post("https://v.example/big", form={}))[:2] == (200, {})
            assert (await crew_oauth_dcr._post("https://v.example/moved"))[0] == 302
        finally:
            await server.close()

    asyncio.run(_run())


def test_post_reads_an_answer_that_arrives_in_pieces(monkeypatch):
    """A single read() returns only the first piece; the body must be read to EOF."""
    pieces = [b'{"access_token": "a', b't"}']

    class _Content:
        async def read(self, n=-1):
            return pieces[0]

        async def iter_chunked(self, n):
            for piece in pieces:
                yield piece

    class _Resp:
        status, headers, content = 200, {}, _Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _Session:
        def post(self, *a, **k):
            return _Resp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def _session(url):
        return _Session(), url

    monkeypatch.setattr(crew_oauth_dcr, "_session", _session)
    status, doc, _ = asyncio.run(crew_oauth_dcr._post("https://v.example/token", form={}))
    assert (status, doc) == (200, {"access_token": "at"})


def test_an_unreachable_endpoint_is_a_coded_failure(monkeypatch):
    from kiro_crew.link_unfurl import UnfurlRejected

    async def _session(url):
        raise UnfurlRejected("blocked_url")

    monkeypatch.setattr(crew_oauth_dcr, "_session", _session)
    with pytest.raises(CrewOAuthError) as err:
        asyncio.run(crew_oauth_dcr._post("https://10.0.0.1/x"))
    # The code the proxy maps to "try again", not "the provider rejected it".
    assert err.value.code == "token_unreachable"


def test_a_dcr_connect_reaches_waiting_through_the_mint_row(vendor, monkeypatch):
    from kiro_crew.connections import mint

    monkeypatch.setattr(crew_oauth_dcr, "DCR_CALLBACK_PORT", 0)
    monkeypatch.setattr(mint, "_mints", {})

    async def _run():
        await crew_oauth.start_mint_row("stripe", ())
        row = mint._mints["stripe"]
        assert row["state"] == "waiting", row.get("reason")
        assert row["oauth_url"].startswith("https://access.stripe.com/mcp/oauth2/authorize?")
        row["watcher"].cancel()
        await row["client"].shutdown()

    asyncio.run(_run())


@pytest.mark.parametrize(
    "authorize",
    [
        "https://auth.atlassian.com/authorize",
        "https://mcp.notion.com/authorize",
        "https://mcp.linear.app/authorize",
        "https://access.stripe.com/mcp/oauth2/authorize",
        "https://vercel.com/oauth/authorize",
        "https://gitlab.com/oauth/authorize",
        "https://ticktick.com/oauth/authorize",
    ],
)
def test_each_cards_consent_link_passes_the_banner_gate(authorize):
    """The six live ``authorization_endpoint`` values, with ``resource`` and the shared redirect."""
    from kiro_crew.security import oauth_url_contains_credential

    client = crew_oauth_dcr.Client(
        client_id="3f9c2a7e-1b4d-4c8a-9e0f-5a6b7c8d9e0f",
        client_secret=None,
        secret_expires_at=0.0,
        auth_method="none",
        redirect_uri=crew_oauth_dcr.redirect_uri(),
        issuer="https://issuer.example",
        resource="https://mcp.example/mcp",
        authorize=authorize,
        token="https://issuer.example/token",
        revoke=None,
        iss_param=False,
    )
    plan = crew_oauth_dcr.Plan(client, "read offline_access")
    fixed = "A" * 43
    assert oauth_url_contains_credential(crew_oauth_dcr.authorize_url(plan, fixed, fixed)) is False


# --- the gateway's public HTTPS callback (pods) ---------------------------------

POD = "https://pod.example.com"
POD_REDIRECT = f"{POD}/api/connections/oauth/callback"


def _config(monkeypatch, url, tunnel=""):
    from types import SimpleNamespace

    import kiro_crew.config as config
    import kiro_crew.tunnel as tunnel_mod

    cfg = SimpleNamespace(dashboard=SimpleNamespace(url=url))
    monkeypatch.setattr(config.KiroCrewConfig, "load", staticmethod(lambda *a, **k: cfg))
    monkeypatch.setattr(tunnel_mod, "get_tunnel_url", lambda: tunnel)


@pytest.mark.parametrize(
    ("url", "tunnel", "expected"),
    [
        (f"{POD}/some/page", "", POD_REDIRECT),
        # A local gateway keeps the loopback: no URL (a live tunnel is not
        # configuration), plain http, or a loopback host.
        ("", "https://t.example.net/", "http://127.0.0.1:48100/callback"),
        ("http://pod.example.com", "", "http://127.0.0.1:48100/callback"),
        ("https://localhost:5476", "", "http://127.0.0.1:48100/callback"),
    ],
)
def test_the_redirect_host_comes_from_config_never_a_request(monkeypatch, url, tunnel, expected):
    _config(monkeypatch, url, tunnel)
    # redirect_uri takes no request, so a caller's Host header cannot reach it.
    assert crew_oauth_dcr.redirect_uri() == expected


def test_a_gateway_gaining_a_public_url_registers_again_and_drops_the_old_pair(vendor, monkeypatch):
    asyncio.run(crew_oauth_dcr.prepare("stripe"))
    TokenStore().replace("stripe", Tokens("a", "r", None, ""))
    monkeypatch.setattr(crew_oauth_dcr, "_public_origin", lambda: POD)
    plan = asyncio.run(crew_oauth_dcr.prepare("stripe"))
    registered = _registered(vendor)
    assert len(registered) == 2 and registered[1][3]["redirect_uris"] == [POD_REDIRECT]
    assert plan.client.redirect_uri == POD_REDIRECT
    assert TokenStore().read("stripe") is None


def _gateway_app():
    from aiohttp import web

    from kiro_crew.dashboard.handlers.connections import api_connections_oauth_callback

    app = web.Application()
    app.router.add_get(
        "/api/connections/oauth/callback", api_connections_oauth_callback, allow_head=False
    )
    return app


async def _get(client, **query):
    async with client.get("/api/connections/oauth/callback", params=query) as r:
        return r.status, await r.text(), r.headers


def _with_client(body):
    from aiohttp.test_utils import TestClient, TestServer

    async def _run():
        async with TestClient(TestServer(_gateway_app())) as client:
            await body(client)

    asyncio.run(_run())


@pytest.fixture()
def audits(monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(crew_oauth_dcr, "_audit", lambda *a: seen.append(a))
    monkeypatch.setattr(crew_oauth_dcr, "_pending", {})
    return seen


def _ok_handler(calls):
    async def _h(request):
        from aiohttp import web

        calls.append(request.query.get("code"))
        return web.Response(text="Signed in. You can close this tab and return to Kiro Crew.")

    return _h


def test_the_gateway_route_answers_its_state_with_a_fixed_page(audits):
    calls: list = []

    async def body(client):
        assert (
            await crew_oauth_dcr.listen("s1", _ok_handler(calls), slug="stripe", loopback=False)
            == 0
        )
        assert "runner" not in crew_oauth_dcr._listener  # no loopback port on a pod
        status, text, headers = await _get(client, state="s0", code="c")
        assert status == 400 and calls == []
        status, text, headers = await _get(client, state="s1", code="c")
        assert status == 200 and calls == ["c"] and "Signed in." in text
        assert headers["Cache-Control"] == "no-store"
        assert headers["Referrer-Policy"] == "no-referrer"
        assert headers["Content-Type"].startswith("text/html")
        for method in ("HEAD", "POST"):
            async with client.request(method, "/api/connections/oauth/callback?state=x") as r:
                assert r.status == 405

    _with_client(body)
    assert [a[:3] for a in audits] == [
        ("connections_crew_oauth_callback", "refused", "unknown"),
        ("connections_crew_oauth_callback", "ok", "stripe"),
    ]


def test_a_pod_sign_in_state_is_gone_after_the_sign_in_window(vendor, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    monkeypatch.setattr(crew_oauth_dcr, "_public_origin", lambda: POD)
    # 0: the wait gives up at once, with no real clock in the test.
    monkeypatch.setattr(crew_oauth, "SIGN_IN_TTL_SECS", 0)

    async def _run():
        sign_in = crew_oauth.SignIn("stripe")
        url = await sign_in.start()
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]
        with pytest.raises(CrewOAuthError) as err:
            await sign_in.finish()
        await sign_in.shutdown()
        assert err.value.code == "consent_expired" and crew_oauth_dcr._pending == {}
        async with TestClient(TestServer(_gateway_app())) as client:
            status, _, _ = await _get(client, state=state, code="c")
            assert status == 400

    asyncio.run(_run())


def test_the_gateway_route_never_reflects_input(audits):
    probe = "<script>x</script>"

    async def body(client):
        await crew_oauth_dcr.listen(probe, _ok_handler([]), loopback=False)
        for query in ({"state": probe, "code": probe}, {"state": "z", "error": probe}):
            _, text, _ = await _get(client, **query)
            assert probe not in text and "script" not in text

    _with_client(body)


def test_the_gateway_route_is_token_free_for_get_only():
    from aiohttp import web

    from kiro_crew.dashboard.routes import agent_config
    from kiro_crew.dashboard.token_auth_paths import (
        _BYPASS_EXACT,
        _BYPASS_EXACT_METHODS,
        CREW_OAUTH_CALLBACK_PATH,
    )

    assert _BYPASS_EXACT_METHODS[CREW_OAUTH_CALLBACK_PATH] == frozenset({"GET"})
    assert CREW_OAUTH_CALLBACK_PATH not in _BYPASS_EXACT
    app = web.Application()
    agent_config.register(app)
    methods = {
        r.method
        for r in app.router.routes()
        if r.resource is not None and r.resource.canonical == CREW_OAUTH_CALLBACK_PATH
    }
    assert methods == {"GET"}


def test_a_dcr_sign_in_on_a_pod_completes_through_the_gateway_route(vendor, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    monkeypatch.setattr(crew_oauth_dcr, "_public_origin", lambda: POD)
    monkeypatch.setattr(crew_oauth_dcr, "drop_harness_grants", lambda *a: asyncio.sleep(0))
    vendor.posts["https://access.stripe.com/mcp/oauth2/token"] = [
        (200, {"access_token": "at", "refresh_token": "rt", "expires_in": 60}, "")
    ]

    async def _run():
        sign_in = crew_oauth.SignIn("stripe")
        url = await sign_in.start()
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        assert query["redirect_uri"] == [POD_REDIRECT] and sign_in.bound_port == 0
        assert "runner" not in crew_oauth_dcr._listener
        async with TestClient(TestServer(_gateway_app())) as client:
            status, _, _ = await _get(client, state=query["state"][0], code="c")
            assert status == 200
            # Replay: the sign-in answers once.
            status, _, _ = await _get(client, state=query["state"][0], code="c")
            assert status == 400
        try:
            await sign_in.finish()
        finally:
            await sign_in.shutdown()
        assert crew_oauth_dcr._pending == {}

    asyncio.run(_run())
    assert TokenStore().read("stripe")[0].access == "at"
    form = vendor.calls[-1][2]
    assert form["redirect_uri"] == POD_REDIRECT and form["code"] == "c"
