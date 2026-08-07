"""The Meetings calendar OAuth client: PKCE, state, exchange, refresh.

No real network. The token endpoint is a local aiohttp server and the SSRF gate
is stubbed to a pass-through so a loopback URL is reachable, exactly as
``test_meetings_providers.py`` does it — everything downstream of the gate (the
form body, the PKCE verifier, the JSON handling, the store writes) runs for real.

These tests MUST live in the repo-level ``test/`` tree: ``setup.cfg`` sets
``testpaths = test transfer``, so a test under ``src/kiro_crew/apps/builtins/…``
is never collected.
"""

from __future__ import annotations

import base64
import hashlib
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from yarl import URL

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend import credentials as creds
from kiro_crew.apps.builtins.meetings.backend import oauth
from kiro_crew.apps.builtins.meetings.backend.providers import calendar as cal

PROVIDER = "test-provider"


def _passthrough_target(url: str) -> cal.VettedTarget:
    """Vet a loopback URL as-is.

    The real gate refuses loopback, so a test that needs a real local server
    stubs it. The stub still returns a genuine ``VettedTarget`` pinned to the
    server's own address, so the fetch runs through the real pinned connector
    rather than around it.
    """
    parsed = URL(url)
    host = parsed.raw_host or ""
    return cal.VettedTarget(url=url, host=host, port=parsed.port or 443, addresses=(host,))


class _FakeTokenEndpoint:
    """A local token endpoint that records the form body it was posted."""

    def __init__(self, response: dict[str, Any] | str, *, status: int = 200) -> None:
        self._response = response
        self._status = status
        self.posts: list[dict[str, list[str]]] = []
        self._runner: Any = None
        self.url = ""

    async def __aenter__(self) -> _FakeTokenEndpoint:
        from aiohttp import web as aioweb

        async def handler(request):
            self.posts.append(parse_qs((await request.read()).decode("utf-8")))
            if isinstance(self._response, str):
                return aioweb.Response(status=self._status, text=self._response)
            return aioweb.json_response(self._response, status=self._status)

        app = aioweb.Application()
        app.router.add_post("/token", handler)
        self._runner = aioweb.AppRunner(app)
        await self._runner.setup()
        site = aioweb.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{self._runner.addresses[0][1]}/token"
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._runner.cleanup()


def _client(token_url: str = "https://example.invalid/token") -> oauth.OAuthClient:
    return oauth.OAuthClient(
        provider_id=PROVIDER,
        authorize_url="https://example.invalid/authorize",
        token_url=token_url,
        scopes=("calendar.readonly", "openid"),
        extra_authorize_params={"access_type": "offline"},
    )


@pytest.fixture()
def store(tmp_path: Path):
    """A temp credential store, so no test touches the real one."""
    creds.set_credentials_home(tmp_path / "creds")
    oauth.forget_pending(PROVIDER)
    yield creds
    oauth.forget_pending(PROVIDER)
    creds.set_credentials_home(None)


class TestPkce:
    """S256 only. ``plain`` gives no protection — the verifier IS the challenge —
    so it is not implemented rather than offered and discouraged."""

    def test_the_verifier_is_within_the_rfc_range(self):
        verifier = oauth.new_verifier()
        # RFC 7636 §4.1: 43..128 characters from the unreserved set.
        assert 43 <= len(verifier) <= 128
        assert all(c.isalnum() or c in "-._~" for c in verifier)

    def test_two_verifiers_differ(self):
        assert oauth.new_verifier() != oauth.new_verifier()

    def test_the_challenge_is_unpadded_base64url_sha256(self):
        verifier = "a" * 64
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        challenge = oauth.challenge_for(verifier)
        assert challenge == expected
        assert "=" not in challenge, "padding must be stripped (RFC 7636 §4.2)"
        assert "+" not in challenge and "/" not in challenge, "must be url-safe"


class TestBegin:
    @pytest.mark.asyncio
    async def test_it_refuses_without_a_client_id(self, store):
        with pytest.raises(oauth.CalendarError, match="OAuth client id"):
            await oauth.begin(_client(), redirect_uri="http://127.0.0.1:5476/cb")

    @pytest.mark.asyncio
    async def test_the_consent_url_carries_everything_the_provider_needs(self, store):
        await store.write_for(PROVIDER, {"client_id": "cid-123"})
        url = await oauth.begin(_client(), redirect_uri="http://127.0.0.1:5476/cb")

        query = parse_qs(urlsplit(url).query)
        assert url.startswith("https://example.invalid/authorize?")
        assert query["client_id"] == ["cid-123"]
        assert query["response_type"] == ["code"]
        assert query["redirect_uri"] == ["http://127.0.0.1:5476/cb"]
        assert query["code_challenge_method"] == ["S256"]
        assert query["scope"] == ["calendar.readonly openid"]
        # Provider-specific extras must survive: without access_type=offline
        # Google returns no refresh token and the calendar stops working in an hour.
        assert query["access_type"] == ["offline"]
        assert len(query["state"][0]) >= 20
        assert len(query["code_challenge"][0]) >= 20

    @pytest.mark.asyncio
    async def test_the_challenge_matches_the_verifier_that_will_be_sent(self, store):
        """The whole point of PKCE: the challenge shown to the provider must be
        the S256 of the verifier this process kept."""
        await store.write_for(PROVIDER, {"client_id": "cid"})
        url = await oauth.begin(_client(), redirect_uri="http://127.0.0.1:5476/cb")
        challenge = parse_qs(urlsplit(url).query)["code_challenge"][0]
        pending = oauth._pending[PROVIDER]
        assert oauth.challenge_for(pending.verifier) == challenge

    @pytest.mark.asyncio
    async def test_starting_again_replaces_the_pending_flow(self, store):
        await store.write_for(PROVIDER, {"client_id": "cid"})
        first = await oauth.begin(_client(), redirect_uri="http://127.0.0.1:5476/cb")
        second = await oauth.begin(_client(), redirect_uri="http://127.0.0.1:5476/cb")
        first_state = parse_qs(urlsplit(first).query)["state"][0]
        second_state = parse_qs(urlsplit(second).query)["state"][0]

        assert first_state != second_state
        # A user who restarts after a mistake must be able to finish the NEW flow.
        assert oauth._pending[PROVIDER].state == second_state


class TestComplete:
    @pytest.mark.asyncio
    async def test_no_pending_flow_is_refused(self, store):
        with pytest.raises(oauth.CalendarError, match="no calendar authorization"):
            await oauth.complete(_client(), code="c", state="s")

    @pytest.mark.asyncio
    async def test_a_mismatched_state_is_refused(self, store):
        await store.write_for(PROVIDER, {"client_id": "cid"})
        await oauth.begin(_client(), redirect_uri="http://127.0.0.1:5476/cb")
        with pytest.raises(oauth.CalendarError, match="did not match"):
            await oauth.complete(_client(), code="c", state="not-the-state")

    @pytest.mark.asyncio
    async def test_a_mismatched_state_consumes_the_flow(self, store):
        """Replay protection: a verifier that survived a failed attempt could be
        presented again with a stolen code."""
        await store.write_for(PROVIDER, {"client_id": "cid"})
        await oauth.begin(_client(), redirect_uri="http://127.0.0.1:5476/cb")
        with pytest.raises(oauth.CalendarError):
            await oauth.complete(_client(), code="c", state="wrong")
        assert PROVIDER not in oauth._pending

    @pytest.mark.asyncio
    async def test_an_expired_flow_is_refused(
        self, store, monkeypatch: pytest.MonkeyPatch
    ):
        await store.write_for(PROVIDER, {"client_id": "cid"})
        url = await oauth.begin(_client(), redirect_uri="http://127.0.0.1:5476/cb")
        state = parse_qs(urlsplit(url).query)["state"][0]

        started = oauth._pending[PROVIDER].created_at
        monkeypatch.setattr(
            oauth, "_now", lambda: started + k.OAUTH_FLOW_TTL_SECS + 1
        )
        with pytest.raises(oauth.CalendarError, match="expired"):
            await oauth.complete(_client(), code="c", state=state)

    @pytest.mark.asyncio
    async def test_the_state_check_is_constant_time(self):
        """Structural: an early-returning comparison leaks a prefix of the
        anti-forgery value."""
        import inspect

        assert "compare_digest" in inspect.getsource(oauth.complete)

    @pytest.mark.asyncio
    async def test_a_successful_exchange_sends_the_verifier_and_stores_the_tokens(
        self, store, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(cal, "_normalize_url", _passthrough_target)
        async with _FakeTokenEndpoint(
            {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600}
        ) as endpoint:
            client = _client(endpoint.url)
            await store.write_for(PROVIDER, {"client_id": "cid", "client_secret": "sek"})
            url = await oauth.begin(client, redirect_uri="http://127.0.0.1:5476/cb")
            state = parse_qs(urlsplit(url).query)["state"][0]
            verifier = oauth._pending[PROVIDER].verifier

            await oauth.complete(client, code="the-code", state=state)
            posted = endpoint.posts[0]

        assert posted["grant_type"] == ["authorization_code"]
        assert posted["code"] == ["the-code"]
        assert posted["code_verifier"] == [verifier], "PKCE verifier must be sent"
        assert posted["client_id"] == ["cid"]
        # A registration that has a secret still sends it; PKCE is what protects
        # the exchange, but some registrations require both.
        assert posted["client_secret"] == ["sek"]

        stored = await store.read_for(PROVIDER)
        assert stored["access_token"] == "at-1"
        assert stored["refresh_token"] == "rt-1"
        assert float(stored["expires_at"]) > time.time()
        # The flow is consumed on success too, so a captured code cannot be
        # redeemed twice.
        assert PROVIDER not in oauth._pending

    @pytest.mark.asyncio
    async def test_a_registration_without_a_secret_sends_none(
        self, store, monkeypatch: pytest.MonkeyPatch
    ):
        """A public client has no usable secret; sending an empty one would be
        rejected by some providers as a malformed request."""
        monkeypatch.setattr(cal, "_normalize_url", _passthrough_target)
        async with _FakeTokenEndpoint({"access_token": "at", "expires_in": 60}) as endpoint:
            client = _client(endpoint.url)
            await store.write_for(PROVIDER, {"client_id": "cid"})
            url = await oauth.begin(client, redirect_uri="http://127.0.0.1:5476/cb")
            state = parse_qs(urlsplit(url).query)["state"][0]
            await oauth.complete(client, code="c", state=state)
            posted = endpoint.posts[0]

        assert "client_secret" not in posted

    @pytest.mark.asyncio
    async def test_a_provider_error_is_reported_with_its_description(
        self, store, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(cal, "_normalize_url", _passthrough_target)
        async with _FakeTokenEndpoint(
            {"error": "invalid_grant", "error_description": "Code was already redeemed"},
            status=200,
        ) as endpoint:
            client = _client(endpoint.url)
            await store.write_for(PROVIDER, {"client_id": "cid"})
            url = await oauth.begin(client, redirect_uri="http://127.0.0.1:5476/cb")
            state = parse_qs(urlsplit(url).query)["state"][0]
            with pytest.raises(oauth.CalendarError, match="already redeemed"):
                await oauth.complete(client, code="c", state=state)

    @pytest.mark.asyncio
    async def test_a_non_json_token_response_is_a_calendar_error(
        self, store, monkeypatch: pytest.MonkeyPatch
    ):
        """The route turns ``CalendarError`` into a 502; a bare ``ValueError``
        would escape as a 500."""
        monkeypatch.setattr(cal, "_normalize_url", _passthrough_target)
        async with _FakeTokenEndpoint("<html>gateway error</html>") as endpoint:
            client = _client(endpoint.url)
            await store.write_for(PROVIDER, {"client_id": "cid"})
            url = await oauth.begin(client, redirect_uri="http://127.0.0.1:5476/cb")
            state = parse_qs(urlsplit(url).query)["state"][0]
            with pytest.raises(oauth.CalendarError, match="not JSON"):
                await oauth.complete(client, code="c", state=state)


class TestAccessToken:
    @pytest.mark.asyncio
    async def test_an_unconnected_provider_says_so(self, store):
        with pytest.raises(oauth.CalendarError, match="not connected"):
            await oauth.access_token(_client())

    @pytest.mark.asyncio
    async def test_a_fresh_token_is_returned_without_a_round_trip(
        self, store, monkeypatch: pytest.MonkeyPatch
    ):
        await store.write_for(
            PROVIDER,
            {
                "client_id": "cid",
                "refresh_token": "rt",
                "access_token": "still-good",
                "expires_at": str(time.time() + 3600),
            },
        )

        async def explode(*args: object, **kwargs: object) -> bytes:
            raise AssertionError("a fresh token must not hit the token endpoint")

        monkeypatch.setattr(cal, "fetch_vetted", explode)
        monkeypatch.setattr(oauth, "fetch_vetted", explode)
        assert await oauth.access_token(_client()) == "still-good"

    @pytest.mark.asyncio
    async def test_an_expired_token_is_refreshed(
        self, store, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(cal, "_normalize_url", _passthrough_target)
        async with _FakeTokenEndpoint(
            {"access_token": "at-2", "expires_in": 3600}
        ) as endpoint:
            await store.write_for(
                PROVIDER,
                {
                    "client_id": "cid",
                    "refresh_token": "rt-keep",
                    "access_token": "expired",
                    "expires_at": str(time.time() - 10),
                },
            )
            token = await oauth.access_token(_client(endpoint.url))
            posted = endpoint.posts[0]

        assert token == "at-2"
        assert posted["grant_type"] == ["refresh_token"]
        assert posted["refresh_token"] == ["rt-keep"]

        stored = await store.read_for(PROVIDER)
        # The refresh response omitted refresh_token, and the old one must
        # SURVIVE -- clearing it would break the connection permanently.
        assert stored["refresh_token"] == "rt-keep"
        assert stored["access_token"] == "at-2"

    @pytest.mark.asyncio
    async def test_a_token_expiring_inside_the_skew_is_refreshed_early(
        self, store, monkeypatch: pytest.MonkeyPatch
    ):
        """A token that passes the check and then dies in flight is a 401 halfway
        through a sync, which is worse than one extra refresh."""
        monkeypatch.setattr(cal, "_normalize_url", _passthrough_target)
        async with _FakeTokenEndpoint(
            {"access_token": "refreshed-early", "expires_in": 3600}
        ) as endpoint:
            await store.write_for(
                PROVIDER,
                {
                    "client_id": "cid",
                    "refresh_token": "rt",
                    "access_token": "about-to-die",
                    # Still valid, but inside the skew window.
                    "expires_at": str(time.time() + k.OAUTH_REFRESH_SKEW_SECS - 5),
                },
            )
            assert await oauth.access_token(_client(endpoint.url)) == "refreshed-early"

    @pytest.mark.asyncio
    async def test_a_rotated_refresh_token_replaces_the_old_one(
        self, store, monkeypatch: pytest.MonkeyPatch
    ):
        """Some providers rotate the refresh token on every use; keeping the old
        one would break the next refresh."""
        monkeypatch.setattr(cal, "_normalize_url", _passthrough_target)
        async with _FakeTokenEndpoint(
            {"access_token": "at", "refresh_token": "rt-new", "expires_in": 60}
        ) as endpoint:
            await store.write_for(
                PROVIDER,
                {
                    "client_id": "cid",
                    "refresh_token": "rt-old",
                    "expires_at": "0",
                },
            )
            await oauth.access_token(_client(endpoint.url))

        assert (await store.read_for(PROVIDER))["refresh_token"] == "rt-new"


class TestTheTokenEndpointGoesThroughTheSharedGate:
    def test_it_does_not_open_its_own_session(self):
        """A bare aiohttp call here would skip https-only, DNS pinning, per-hop
        redirect validation and the size cap -- every property the calendar fetch
        is careful about, for a request that carries a credential."""
        import inspect

        src = inspect.getsource(oauth._post_token)
        assert "fetch_vetted(" in src
        assert "ClientSession" not in src
