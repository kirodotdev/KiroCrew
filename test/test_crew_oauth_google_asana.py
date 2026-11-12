"""Crew-owned OAuth phase 2: Asana and the six Google Connections as data entries.

Per provider: the consent URL, the code swap, refresh, Disconnect's revoke call
and the KAS bridge projection. Google's revoke ends the whole grant of its client,
so a Google card revokes only when no other held Google card shares that client.
"""

from __future__ import annotations

import asyncio
import json
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
from kiro_crew.testing.wait import async_wait_until, finished

GOOGLE = (
    "google-drive",
    "google-docs",
    "google-sheets",
    "google-slides",
    "gmail",
    "google-calendar",
)
NEW = ("asana", *GOOGLE)
SESSION = "dashboard_chat-crew-oauth-p2"


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
    yield
    crew_oauth_proxy._bridges.clear()


def _id_token(sub: str) -> str:
    """An unsigned JWT-shaped id_token naming *sub* (only ``sub`` is read)."""
    import base64

    def seg(doc: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(doc).encode()).decode().rstrip("=")

    return f"{seg({'alg': 'none'})}.{seg({'sub': sub})}.sig"


def _client(client_id="cid", secret="sec"):
    return types.SimpleNamespace(client_id=client_id, client_secret=secret)


# --- consent URL --------------------------------------------------------------


@pytest.mark.parametrize("slug", NEW)
def test_the_consent_url_carries_the_registry_scopes_and_provider_extras(slug):
    url = crew_oauth.authorize_url(slug, "cid", "http://127.0.0.1:1/callback", "st", "ch")
    parts = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parts.query))
    host = "accounts.google.com" if slug in GOOGLE else "app.asana.com"
    assert parts.netloc == host
    scopes = list(registry.get_provider(slug)["recommended_scopes"])
    # Google adds ``openid`` so the id_token names the account.
    assert query["scope"].split() == scopes + (["openid"] if slug in GOOGLE else [])
    assert query["code_challenge_method"] == "S256" and query["state"] == "st"
    assert query["response_type"] == "code"
    if slug in GOOGLE:
        # Without both, Google returns no refresh token.
        assert query["access_type"] == "offline" and query["prompt"] == "consent"
    else:
        assert "access_type" not in query and "prompt" not in query
    from kiro_crew.security import oauth_url_contains_credential

    assert oauth_url_contains_credential(url) is False


def test_github_keeps_its_consent_url():
    query = dict(
        urllib.parse.parse_qsl(
            urllib.parse.urlsplit(crew_oauth.authorize_url("github", "c", "r", "s", "x")).query
        )
    )
    assert "access_type" not in query and "prompt" not in query


@pytest.mark.parametrize("slug", NEW)
def test_every_new_slug_is_supported_on_its_registry_port(slug):
    provider = registry.get_provider(slug)
    assert slug in crew_oauth.SUPPORTED_SLUGS
    assert registry.redirect_uri(provider) == (
        f"http://127.0.0.1:{provider['auth']['redirect_port']}/callback"
    )


# --- token endpoint, code swap and refresh -----------------------------------


def _vendor(seen: list, token_answer: tuple[int, str], revoke_status: int = 200):
    async def _token(request):
        seen.append(("token", dict(await request.post())))
        status, body = token_answer
        return web.Response(status=status, text=body, content_type="application/json")

    async def _revoke(request):
        seen.append(("revoke", dict(await request.post())))
        return web.Response(status=revoke_status)

    app = web.Application()
    app.router.add_post("/token", _token)
    app.router.add_post("/revoke", _revoke)
    return app


async def _serve(monkeypatch, slugs, seen, token_answer=(200, "{}"), revoke_status=200):
    server = TestServer(_vendor(seen, token_answer, revoke_status))
    await server.start_server()
    base = str(server.make_url(""))
    for slug in slugs:
        monkeypatch.setitem(
            crew_oauth._ENDPOINTS,
            slug,
            {**crew_oauth._ENDPOINTS[slug], "token": f"{base}/token", "revoke": f"{base}/revoke"},
        )
    return server


@pytest.mark.asyncio
@pytest.mark.parametrize("slug", ["asana", "gmail"])
async def test_the_code_swap_sends_the_authorization_code_grant(tmp_path, monkeypatch, slug):
    seen: list = []
    doc = {"access_token": "a1", "refresh_token": "r1", "expires_in": 3600}
    if slug == "gmail":
        doc["id_token"] = _id_token("acct-1")
    server = await _serve(monkeypatch, [slug], seen, (200, json.dumps(doc)))
    store = TokenStore(SecretVault(tmp_path))
    try:
        sign_in = crew_oauth.SignIn(slug)
        sign_in._client, sign_in._redirect = _client(), "http://127.0.0.1:1/callback"
        sign_in._done.set_result("the-code")
        await sign_in.finish(store)
    finally:
        await server.close()
    form = seen[0][1]
    assert form["grant_type"] == "authorization_code" and form["code"] == "the-code"
    assert form["client_secret"] == "sec" and form["code_verifier"] == sign_in._verifier
    held = store.read(slug)
    assert held is not None and held[0].refresh == "r1"
    assert held[0].account == ("acct-1" if slug == "gmail" else None)
    assert held[0].client == "cid"


@pytest.mark.asyncio
@pytest.mark.parametrize("slug", ["asana", "google-drive"])
async def test_a_refresh_without_a_new_refresh_token_keeps_the_old_one(tmp_path, monkeypatch, slug):
    """Google and Asana refresh tokens are reusable: the answer omits a new one."""
    seen: list = []
    answer = json.dumps({"access_token": "a2", "expires_in": 3600})
    server = await _serve(monkeypatch, [slug], seen, (200, answer))
    monkeypatch.setattr(crew_oauth, "_client", lambda s: _client())
    store = TokenStore(SecretVault(tmp_path))
    store.replace(slug, Tokens("a1", "r1", time.time() - 10, "", "acct-1", "cid"))
    try:
        assert await crew_oauth.access_token(slug, store=store) == "a2"
    finally:
        await server.close()
    assert seen[0][1]["grant_type"] == "refresh_token" and seen[0][1]["refresh_token"] == "r1"
    held = store.read(slug)
    assert held is not None and held[0].access == "a2" and held[0].refresh == "r1"
    # A refresh without an id_token keeps the account and the issuing client.
    assert held[0].account == "acct-1" and held[0].client == "cid"


@pytest.mark.asyncio
@pytest.mark.parametrize("slug", ["asana", "google-sheets"])
async def test_a_400_invalid_grant_asks_the_user_to_reconnect(tmp_path, monkeypatch, slug):
    seen: list = []
    body = json.dumps({"error": "invalid_grant", "error_description": "Token has been revoked."})
    server = await _serve(monkeypatch, [slug], seen, (400, body))
    monkeypatch.setattr(crew_oauth, "_client", lambda s: _client())
    store = TokenStore(SecretVault(tmp_path))
    store.replace(slug, Tokens("a1", "r1", time.time() - 10, ""))
    try:
        with pytest.raises(CrewOAuthError) as err:
            await crew_oauth.access_token(slug, store=store)
    finally:
        await server.close()
    assert err.value.code == "needs_auth"
    assert store.read(slug) is not None  # never deleted on a refresh failure


# --- Disconnect ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_asana_revokes_its_refresh_token_with_the_client(tmp_path, monkeypatch):
    seen: list = []
    server = await _serve(monkeypatch, ["asana"], seen)
    monkeypatch.setattr(crew_oauth, "_client", lambda s: _client())
    store = TokenStore(SecretVault(tmp_path))
    store.replace("asana", Tokens("a1", "r1", None, ""))
    try:
        assert await crew_oauth.disconnect("asana", store=store) == {
            "removed": True,
            "revoked": True,
        }
    finally:
        await server.close()
    assert seen == [("revoke", {"token": "r1", "client_id": "cid", "client_secret": "sec"})]
    assert not store.present("asana")


@pytest.mark.asyncio
async def test_a_google_card_revokes_only_when_it_is_the_last_on_its_client(tmp_path, monkeypatch):
    seen: list = []
    server = await _serve(monkeypatch, ["gmail", "google-drive", "google-docs"], seen)
    monkeypatch.setattr(crew_oauth, "_client", lambda s: _client())
    store = TokenStore(SecretVault(tmp_path))
    # google-docs was issued by another client: it never holds gmail's grant open.
    for slug in ("gmail", "google-drive", "google-docs"):
        issuer = "other" if slug == "google-docs" else "cid"
        store.replace(slug, Tokens(f"a-{slug}", f"r-{slug}", None, "", "acct-1", issuer))
    try:
        first = await crew_oauth.disconnect("gmail", store=store)
        assert first == {"removed": True, "revoked": False, "shared": True}
        assert seen == [] and not store.present("gmail") and store.present("google-drive")
        last = await crew_oauth.disconnect("google-drive", store=store)
        assert last == {"removed": True, "revoked": True}
    finally:
        await server.close()
    # Google's revoke takes the token alone; the client secret is not sent.
    assert seen == [("revoke", {"token": "r-google-drive"})]
    assert store.present("google-docs")


@pytest.mark.asyncio
async def test_a_google_revoke_refused_upstream_still_deletes_the_pair(tmp_path, monkeypatch):
    seen: list = []
    server = await _serve(monkeypatch, ["google-slides"], seen, revoke_status=400)
    monkeypatch.setattr(crew_oauth, "_client", lambda s: _client())
    store = TokenStore(SecretVault(tmp_path))
    store.replace("google-slides", Tokens("a1", None, None, ""))
    try:
        result = await crew_oauth.disconnect("google-slides", store=store)
    finally:
        await server.close()
    assert result == {"removed": True, "revoked": False}
    assert seen == [("revoke", {"token": "a1"})] and not store.present("google-slides")


@pytest.mark.asyncio
async def test_a_google_revoke_needs_no_configured_client(tmp_path, monkeypatch):
    """A removed OAuth Apps record never blocks Google's token-only revoke."""
    seen: list = []
    server = await _serve(monkeypatch, ["gmail"], seen)

    def _gone(slug):
        raise CrewOAuthError("client_not_configured")

    monkeypatch.setattr(crew_oauth, "_client", _gone)
    store = TokenStore(SecretVault(tmp_path))
    store.replace("gmail", Tokens("a1", "r1", None, ""))
    try:
        assert await crew_oauth.disconnect("gmail", store=store) == {
            "removed": True,
            "revoked": True,
        }
    finally:
        await server.close()
    assert seen == [("revoke", {"token": "r1"})]


@pytest.mark.asyncio
async def test_overlapping_google_disconnects_still_revoke_once(tmp_path, monkeypatch):
    """Two tabs disconnect the last two cards of one client: the second one revokes."""
    seen: list = []
    server = await _serve(monkeypatch, ["gmail", "google-drive"], seen)
    monkeypatch.setattr(crew_oauth, "_client", lambda s: _client())
    monkeypatch.setattr(crew_oauth, "_group_locks", {})
    store = TokenStore(SecretVault(tmp_path))
    for slug in ("gmail", "google-drive"):
        store.replace(slug, Tokens(f"a-{slug}", f"r-{slug}", None, "", "acct-1", "cid"))
    try:
        results = await asyncio.gather(
            crew_oauth.disconnect("gmail", store=store),
            crew_oauth.disconnect("google-drive", store=store),
        )
    finally:
        await server.close()
    assert sorted(r["revoked"] for r in results) == [False, True]
    assert len(seen) == 1 and seen[0][0] == "revoke"
    assert not store.present("gmail") and not store.present("google-drive")


@pytest.mark.asyncio
@pytest.mark.parametrize("other_account", ["acct-2", None])
async def test_another_or_unknown_account_never_holds_the_grant_open(
    tmp_path, monkeypatch, other_account
):
    """Same client, different (or unknown) Google account: each Disconnect revokes."""
    seen: list = []
    server = await _serve(monkeypatch, ["gmail", "google-drive"], seen)
    monkeypatch.setattr(crew_oauth, "_client", lambda s: _client())
    store = TokenStore(SecretVault(tmp_path))
    store.replace("gmail", Tokens("a-g", "r-g", None, "", "acct-1", "cid"))
    store.replace("google-drive", Tokens("a-d", "r-d", None, "", other_account, "cid"))
    try:
        first = await crew_oauth.disconnect("gmail", store=store)
        second = await crew_oauth.disconnect("google-drive", store=store)
    finally:
        await server.close()
    assert first["revoked"] is True and second["revoked"] is True
    assert [f["token"] for _, f in seen] == ["r-g", "r-d"]


@pytest.mark.parametrize(
    ("raw", "sub"),
    [(None, None), ("not.a", None), ("a.%%%.b", None), ("x.e30.y", None)],
)
def test_a_bad_id_token_names_no_account(raw, sub):
    assert crew_oauth._id_token_subject(raw) == sub
    assert crew_oauth._id_token_subject(_id_token("acct-9")) == "acct-9"


def test_the_account_never_reaches_the_audit_log(monkeypatch, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    tokens = crew_oauth.tokens_from_response(
        {"access_token": "a", "id_token": _id_token("acct-secret-sub")}, 0.0
    )
    assert tokens.account == "acct-secret-sub"
    crew_oauth._audit("connections_crew_oauth_sign_in", "ok", "gmail")
    assert "acct-secret-sub" not in caplog.text and "eyJ" not in caplog.text


@pytest.mark.asyncio
async def test_two_clients_of_one_cloud_project_share_the_grant(tmp_path, monkeypatch):
    """Google revokes project-wide: a sibling on another client of the project holds it open."""
    seen: list = []
    server = await _serve(monkeypatch, ["gmail", "google-drive"], seen)
    store = TokenStore(SecretVault(tmp_path))
    gmail_client = "123-aaa.apps.googleusercontent.com"
    drive_client = "123-bbb.apps.googleusercontent.com"
    store.replace("gmail", Tokens("a-g", "r-g", None, "", "acct-1", gmail_client))
    store.replace("google-drive", Tokens("a-d", "r-d", None, "", "acct-1", drive_client))
    store.replace("google-docs", Tokens("a-o", "r-o", None, "", "acct-1", "456-ccc"))
    assert crew_oauth._shares_upstream_grant("google-docs", store) is False
    try:
        result = await crew_oauth.disconnect("gmail", store=store)
    finally:
        await server.close()
    assert result["shared"] is True and seen == []


@pytest.mark.asyncio
async def test_sharing_follows_the_issuing_client_not_todays_settings(tmp_path, monkeypatch):
    """Replacing the OAuth Apps client without reconnecting changes nothing here."""
    seen: list = []
    server = await _serve(monkeypatch, ["gmail", "google-drive"], seen)
    monkeypatch.setattr(crew_oauth, "_client", lambda s: _client("replaced"))
    store = TokenStore(SecretVault(tmp_path))
    for slug in ("gmail", "google-drive"):
        store.replace(slug, Tokens(f"a-{slug}", f"r-{slug}", None, "", "acct-1", "cid"))
    try:
        result = await crew_oauth.disconnect("gmail", store=store)
    finally:
        await server.close()
    assert result["shared"] is True and seen == []


@pytest.mark.asyncio
async def test_a_sibling_disconnect_waits_for_a_sign_in_swap(tmp_path, monkeypatch):
    """Drive's code swap holds the group lock, so Gmail's Disconnect sees Drive as sharer."""
    seen: list = []
    server = await _serve(monkeypatch, ["gmail", "google-drive"], seen)
    monkeypatch.setattr(crew_oauth, "_client", lambda s: _client())
    monkeypatch.setattr(crew_oauth, "_group_locks", {})
    store = TokenStore(SecretVault(tmp_path))
    store.replace("gmail", Tokens("a-g", "r-g", None, "", "acct-1", "cid"))
    swapping = asyncio.Event()
    release = asyncio.Event()

    async def _slow_post(slug, form):
        swapping.set()
        await release.wait()
        return {"access_token": "a-d", "refresh_token": "r-d", "id_token": _id_token("acct-1")}

    monkeypatch.setattr(crew_oauth, "_post_token", _slow_post)
    sign_in = crew_oauth.SignIn("google-drive")
    sign_in._client, sign_in._redirect = _client(), "http://127.0.0.1:1/callback"
    sign_in._done.set_result("code")
    connecting = asyncio.create_task(sign_in.finish(store))
    leaving: asyncio.Task | None = None
    try:
        await finished(swapping.wait(), "Drive swap started")
        leaving = asyncio.create_task(crew_oauth.disconnect("gmail", store=store))
        lock = crew_oauth._group_locks["google"]
        # Positive signal: Disconnect is queued on the group lock Drive's swap holds.
        await async_wait_until(lambda: bool(getattr(lock, "_waiters", None)))
        assert lock.locked() and not leaving.done()
        release.set()
        await finished(connecting, "Drive sign-in")
        result = await finished(leaving, "Gmail disconnect")
    finally:
        release.set()
        for task in (connecting, leaving):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(connecting, *([leaving] if leaving else []), return_exceptions=True)
        await server.close()
    assert result["shared"] is True and seen == []
    assert store.present("google-drive") and not store.present("gmail")


@pytest.mark.asyncio
@pytest.mark.parametrize("close_while", ["queued", "swapping"])
async def test_a_closing_sign_in_never_drops_a_swapped_pair(tmp_path, monkeypatch, close_while):
    """Queued: the code is never swapped. Swapping: the pair is stored for Disconnect."""
    swaps: list[str] = []
    in_swap = asyncio.Event()
    release = asyncio.Event()

    async def _post(slug, form):
        swaps.append(form["code"])
        in_swap.set()
        await release.wait()
        return {"access_token": "a-new", "refresh_token": "r-new"}

    monkeypatch.setattr(crew_oauth, "_post_token", _post)
    store = TokenStore(SecretVault(tmp_path))
    sign_in = crew_oauth.SignIn("asana")
    sign_in._client, sign_in._redirect = _client(), "http://127.0.0.1:1/callback"
    sign_in._done.set_result("code")
    lock = crew_oauth._refresh_locks.setdefault("asana", asyncio.Lock())
    if close_while == "queued":
        await lock.acquire()  # a refresh holds the slug lock
    finish = asyncio.create_task(sign_in.finish(store))
    try:
        if close_while == "queued":
            await async_wait_until(lambda: sign_in._write is not None)
            sign_in._closing = True
            lock.release()
            with pytest.raises(CrewOAuthError):
                await finished(finish, "sign-in finish")
            assert swaps == [] and not store.present("asana")
        else:
            await finished(in_swap.wait(), "swap started")
            sign_in._closing = True
            release.set()
            await finished(finish, "sign-in finish")
            held = store.read("asana")
            assert held is not None and held[0].access == "a-new"
    finally:
        release.set()
        if lock.locked():
            lock.release()
        if not finish.done():
            finish.cancel()
        await asyncio.gather(finish, return_exceptions=True)


@pytest.mark.parametrize(("account", "client"), [(None, "cid"), ("acct-1", None)])
def test_a_pair_without_its_account_or_client_is_not_shared(tmp_path, account, client):
    """A pair stored before these fields existed always revokes."""
    store = TokenStore(SecretVault(tmp_path))
    store.replace("gmail", Tokens("a", "r", None, "", account, client))
    store.replace("google-drive", Tokens("a", "r", None, "", account, client))
    assert crew_oauth._shares_upstream_grant("gmail", store) is False
    assert crew_oauth._shares_upstream_grant("asana", store) is False


# --- KAS projection --------------------------------------------------------------


@pytest.mark.parametrize("slug", NEW)
def test_the_bridge_projects_for_the_slug_on_kas(tmp_path, monkeypatch, slug):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr(crew_oauth, "enabled", lambda: True)
    monkeypatch.setattr(crew_oauth, "_chat_backend_bridged", lambda: True)
    url = registry.get_provider(slug)["mcp_url"]
    spec = _spec(
        tools=[f"@{slug}"],
        allowedTools=[f"@{slug}"],
        mcpServers={slug: {"url": url, "oauth": {"clientId": "c", "clientSecret": "s"}}},
    )
    out = kas_agents.to_client_custom_agent("kirocrew", spec, "p", session_key=SESSION)
    entry = out["mcpServers"][slug]
    assert "url" not in entry and "oauth" not in entry
    assert url not in json.dumps(out)
    assert crew_oauth_proxy.is_connection_server(SESSION, slug)
