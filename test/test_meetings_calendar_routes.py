"""The calendar credential + OAuth routes.

No real network and no real provider: the OAuth module is driven through its own
public surface and the token endpoint never gets called, because every test here
stops at the handshake boundary.

These tests MUST live in the repo-level ``test/`` tree — ``setup.cfg`` sets
``testpaths = test transfer``, so a test under ``src/kiro_crew/apps/builtins/…``
is never collected.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from meetings_helpers import (  # noqa: F401
    reset_module_state_fixture,
    root_fixture,
)

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend import credentials as creds
from kiro_crew.apps.builtins.meetings.backend import oauth
from kiro_crew.apps.builtins.meetings.backend.routes import calendar as calroutes

BASE = k.API_BASE


@pytest.fixture()
def store(tmp_path: Path):
    creds.set_credentials_home(tmp_path / "creds")
    for provider in (k.CALENDAR_PROVIDER_GOOGLE, k.CALENDAR_PROVIDER_MICROSOFT):
        oauth.forget_pending(provider)
    yield creds
    for provider in (k.CALENDAR_PROVIDER_GOOGLE, k.CALENDAR_PROVIDER_MICROSOFT):
        oauth.forget_pending(provider)
    creds.set_credentials_home(None)


class TestCredentialFieldAllowlist:
    """The allowlist is a control, not documentation.

    Without it a client could PUT ``access_token`` or ``refresh_token`` straight
    into the store and hand the app a credential of its choosing. The OAuth tokens
    are written only by the oauth module, on the way out of a real exchange.
    """

    def test_no_provider_accepts_a_token_field(self):
        for provider, fields in calroutes._CREDENTIAL_FIELDS.items():
            assert "access_token" not in fields, provider
            assert "refresh_token" not in fields, provider
            assert "expires_at" not in fields, provider

    def test_caldav_takes_a_username_and_password(self):
        assert calroutes._CREDENTIAL_FIELDS[k.CALENDAR_PROVIDER_CALDAV] == (
            "username",
            "password",
        )

    def test_the_oauth_providers_take_a_client_registration(self):
        for provider in (k.CALENDAR_PROVIDER_GOOGLE, k.CALENDAR_PROVIDER_MICROSOFT):
            assert calroutes._CREDENTIAL_FIELDS[provider] == ("client_id", "client_secret")

    def test_the_providers_that_need_no_credentials_have_no_entry(self):
        assert k.CALENDAR_PROVIDER_NONE not in calroutes._CREDENTIAL_FIELDS
        assert k.CALENDAR_PROVIDER_ICS not in calroutes._CREDENTIAL_FIELDS

    def test_every_allowlisted_provider_is_a_registered_one(self):
        """A typo'd key here would be a silently dead branch."""
        from kiro_crew.apps.builtins.meetings.backend.providers import calendar as cal

        known = {row["id"] for row in cal.available_calendar_providers()}
        assert set(calroutes._CREDENTIAL_FIELDS) <= known


class TestOAuthClientMap:
    def test_only_the_cloud_providers_use_oauth(self):
        assert set(calroutes._OAUTH_CLIENTS) == {
            k.CALENDAR_PROVIDER_GOOGLE,
            k.CALENDAR_PROVIDER_MICROSOFT,
        }

    def test_each_entry_builds_a_client_for_its_own_provider(self):
        for provider, factory in calroutes._OAUTH_CLIENTS.items():
            assert factory().provider_id == provider


class TestRedirectUri:
    """Derived from the request origin, because the auth cookie is host-scoped.

    A user logged in at ``localhost:5476`` who was sent back to ``127.0.0.1:5476``
    would arrive with no cookie and be rejected -- so the origin is echoed rather
    than normalized.
    """

    @staticmethod
    def _request(url: str):
        from aiohttp.test_utils import make_mocked_request

        split = urlsplit(url)
        return make_mocked_request("GET", split.path or "/", headers={"Host": split.netloc})

    def test_it_keeps_the_host_the_caller_used(self):
        uri = calroutes._redirect_uri(self._request("http://localhost:5476/x"))
        assert uri.startswith("http://localhost:5476/")
        assert "127.0.0.1" not in uri

    def test_it_keeps_a_non_default_port(self):
        uri = calroutes._redirect_uri(self._request("http://127.0.0.1:9999/x"))
        assert "127.0.0.1:9999" in uri

    def test_it_points_at_the_registered_callback_path(self):
        """The path comes from the shared constant, so the redirect URI and the
        route cannot drift -- a mismatch would fail only at the provider, with a
        message that does not name the cause."""
        uri = calroutes._redirect_uri(self._request("http://127.0.0.1:5476/x"))
        assert uri.endswith(f"{k.API_BASE}{calroutes.OAUTH_CALLBACK_PATH}")
        assert calroutes.OAUTH_CALLBACK_PATH == "/calendar/oauth/callback"


class TestTheCallbackPage:
    def test_it_answers_html_a_person_can_read(self):
        resp = calroutes._callback_page("Connected", "all good")
        assert resp.content_type == "text/html"
        assert "Connected" in resp.text

    def test_it_escapes_the_detail(self):
        """Every current caller passes text this process produced, but the next one
        will pass a provider's error_description -- and that is remote input."""
        resp = calroutes._callback_page("t", "<script>alert(1)</script>")
        assert "<script>" not in resp.text
        assert "&lt;script&gt;" in resp.text

    def test_it_escapes_the_title_too(self):
        resp = calroutes._callback_page("<img src=x onerror=1>", "d")
        assert "<img" not in resp.text


class TestOAuthCallbackHandler:
    """The callback always answers 200 HTML: a person is reading it, and a browser
    error page would hide the reason."""

    @staticmethod
    def _callback(query: dict[str, str]):
        from urllib.parse import urlencode

        from aiohttp.test_utils import make_mocked_request

        path = f"{BASE}{calroutes.OAUTH_CALLBACK_PATH}?{urlencode(query)}"
        return make_mocked_request("GET", path, headers={"Host": "127.0.0.1:5476"})

    @pytest.mark.asyncio
    async def test_an_unknown_provider_is_reported_not_crashed(self, store):
        resp = await calroutes.handle_oauth_callback(self._callback({"provider": "nope"}))
        assert resp.status == 200
        assert "not one this app can connect to" in resp.text

    @pytest.mark.asyncio
    async def test_a_user_denial_is_reported_and_clears_the_flow(self, store):
        await store.write_for(k.CALENDAR_PROVIDER_GOOGLE, {"client_id": "cid"})
        await oauth.begin(
            calroutes._OAUTH_CLIENTS[k.CALENDAR_PROVIDER_GOOGLE](),
            redirect_uri="http://127.0.0.1:5476/cb",
        )
        resp = await calroutes.handle_oauth_callback(
            self._callback({"provider": k.CALENDAR_PROVIDER_GOOGLE, "error": "access_denied"})
        )

        assert "access_denied" in resp.text
        # An abandoned flow must not stay redeemable for its whole TTL.
        assert k.CALENDAR_PROVIDER_GOOGLE not in oauth._pending

    @pytest.mark.asyncio
    async def test_a_denial_string_is_escaped_into_the_page(self, store):
        resp = await calroutes.handle_oauth_callback(
            self._callback(
                {"provider": k.CALENDAR_PROVIDER_GOOGLE, "error": "<script>x</script>"}
            )
        )
        assert "<script>" not in resp.text

    @pytest.mark.asyncio
    async def test_a_forged_callback_with_no_flow_is_refused(self, store):
        """`state` is the guard: a link a user is tricked into opening has no
        pending flow to match."""
        resp = await calroutes.handle_oauth_callback(
            self._callback(
                {
                    "provider": k.CALENDAR_PROVIDER_GOOGLE,
                    "code": "attacker-code",
                    "state": "made-up",
                }
            )
        )
        assert "no calendar authorization is in progress" in resp.text

    @pytest.mark.asyncio
    async def test_a_wrong_state_is_refused_and_the_code_is_not_echoed(self, store):
        await store.write_for(k.CALENDAR_PROVIDER_GOOGLE, {"client_id": "cid"})
        await oauth.begin(
            calroutes._OAUTH_CLIENTS[k.CALENDAR_PROVIDER_GOOGLE](),
            redirect_uri="http://127.0.0.1:5476/cb",
        )
        resp = await calroutes.handle_oauth_callback(
            self._callback(
                {
                    "provider": k.CALENDAR_PROVIDER_GOOGLE,
                    "code": "secret-authorization-code",
                    "state": "wrong",
                }
            )
        )

        assert "did not match" in resp.text
        assert "secret-authorization-code" not in resp.text

    @pytest.mark.asyncio
    async def test_the_authorization_code_never_reaches_the_page_on_success_either(
        self, store, monkeypatch: pytest.MonkeyPatch
    ):
        async def _complete(client, *, code: str, state: str) -> None:
            return None

        monkeypatch.setattr(oauth, "complete", _complete)
        resp = await calroutes.handle_oauth_callback(
            self._callback(
                {
                    "provider": k.CALENDAR_PROVIDER_GOOGLE,
                    "code": "secret-authorization-code",
                    "state": "s",
                }
            )
        )

        assert "Calendar connected" in resp.text
        assert "secret-authorization-code" not in resp.text


class TestForgettingAProviderClearsItsPendingFlow:
    @pytest.mark.asyncio
    async def test_disconnect_drops_the_flow(self, store):
        await store.write_for(k.CALENDAR_PROVIDER_GOOGLE, {"client_id": "cid"})
        await oauth.begin(
            calroutes._OAUTH_CLIENTS[k.CALENDAR_PROVIDER_GOOGLE](),
            redirect_uri="http://127.0.0.1:5476/cb",
        )
        assert k.CALENDAR_PROVIDER_GOOGLE in oauth._pending

        oauth.forget_pending(k.CALENDAR_PROVIDER_GOOGLE)
        assert k.CALENDAR_PROVIDER_GOOGLE not in oauth._pending


class TestTheConsentUrlUsesTheDerivedRedirect:
    @pytest.mark.asyncio
    async def test_the_redirect_uri_in_the_url_is_the_callback_route(self, store):
        await store.write_for(k.CALENDAR_PROVIDER_GOOGLE, {"client_id": "cid"})
        redirect = f"http://127.0.0.1:5476{k.API_BASE}{calroutes.OAUTH_CALLBACK_PATH}"
        url = await oauth.begin(
            calroutes._OAUTH_CLIENTS[k.CALENDAR_PROVIDER_GOOGLE](),
            redirect_uri=redirect,
        )
        assert parse_qs(urlsplit(url).query)["redirect_uri"] == [redirect]
