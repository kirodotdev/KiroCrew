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
    enabled_fixture,
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

    @staticmethod
    async def _open_flow(store, provider: str = k.CALENDAR_PROVIDER_GOOGLE) -> str:
        """Start a real flow and return the ``state`` the provider will echo back.

        The callback query is built the way a provider actually builds it — the
        registered redirect URI plus ``code``/``state``, and NO ``provider`` — so
        these tests exercise the shape that reaches production. Passing a
        ``provider`` parameter (as they used to) simulated a redirect no provider
        sends, which is why a callback that could never resolve its provider
        still looked green.
        """
        await store.write_for(provider, {"client_id": "cid"})
        await oauth.begin(
            calroutes._OAUTH_CLIENTS[provider](),
            redirect_uri="http://127.0.0.1:5476/cb",
        )
        return oauth._pending[provider].state

    @pytest.mark.asyncio
    async def test_a_callback_that_names_no_flow_is_refused(self, store):
        """No ``state`` at all: nothing identifies a flow, so there is nothing to
        redeem. This is also what a stray visit to the callback URL looks like."""
        resp = await calroutes.handle_oauth_callback(self._callback({}))
        assert resp.status == 200
        assert "expired or was already used" in resp.text

    @pytest.mark.asyncio
    async def test_a_user_denial_is_reported_and_clears_the_flow(self, store):
        state = await self._open_flow(store)
        resp = await calroutes.handle_oauth_callback(
            self._callback({"state": state, "error": "access_denied"})
        )

        assert "access_denied" in resp.text
        # An abandoned flow must not stay redeemable for its whole TTL.
        assert k.CALENDAR_PROVIDER_GOOGLE not in oauth._pending

    @pytest.mark.asyncio
    async def test_a_denial_string_is_escaped_into_the_page(self, store):
        state = await self._open_flow(store)
        resp = await calroutes.handle_oauth_callback(
            self._callback({"state": state, "error": "<script>x</script>"})
        )
        # Reached the denial page (not the refusal), and escaped the provider's text.
        assert "script" in resp.text
        assert "<script>" not in resp.text

    @pytest.mark.asyncio
    async def test_a_forged_callback_with_no_flow_is_refused(self, store, monkeypatch):
        """`state` is the guard: a link a user is tricked into opening has no
        pending flow to match, so it cannot even name a provider. The refusal is
        SEL-audited like every other exit from the handler — a forged callback is
        precisely the event an audit-trail reviewer needs to see."""
        events: list[tuple[str, str, str, str]] = []
        monkeypatch.setattr(
            calroutes,
            "audit",
            lambda op, res, *, outcome, error="": events.append((op, res, outcome, error)),
        )
        resp = await calroutes.handle_oauth_callback(
            self._callback({"code": "attacker-code", "state": "made-up"})
        )
        assert "expired or was already used" in resp.text
        assert "attacker-code" not in resp.text
        assert events == [
            ("meetings.calendar_oauth_callback", "unknown", "error", "invalid_or_expired_state")
        ]

    @pytest.mark.asyncio
    async def test_a_wrong_state_is_refused_before_any_client_is_built(self, store):
        """A state that matches no pending flow is refused at provider resolution.

        Stronger than the check inside ``complete`` that this replaces: because the
        provider is DERIVED from ``state``, a forged one cannot reach the exchange
        at all. ``complete`` still re-verifies it, which is why that check is now
        defence in depth rather than the only guard.
        """
        await self._open_flow(store)
        resp = await calroutes.handle_oauth_callback(
            self._callback({"code": "secret-authorization-code", "state": "wrong"})
        )

        assert "expired or was already used" in resp.text
        assert "secret-authorization-code" not in resp.text
        # The real flow is untouched: a forged callback must not consume it.
        assert k.CALENDAR_PROVIDER_GOOGLE in oauth._pending

    @pytest.mark.asyncio
    async def test_a_real_redirect_resolves_its_provider_from_the_state(
        self, store, monkeypatch: pytest.MonkeyPatch
    ):
        """The end-to-end shape: only ``code`` and ``state`` come back.

        This is the case that was broken — every genuine redirect was rejected as
        an unknown provider — so it is pinned on the exact query a provider sends.
        """
        seen: dict[str, str] = {}

        async def _complete(client, *, code: str, state: str) -> None:
            seen["provider"] = client.provider_id
            seen["state"] = state

        monkeypatch.setattr(oauth, "complete", _complete)
        state = await self._open_flow(store)
        resp = await calroutes.handle_oauth_callback(
            self._callback({"code": "secret-authorization-code", "state": state})
        )

        assert "Calendar connected" in resp.text
        assert "secret-authorization-code" not in resp.text
        # Routed to the right provider's client, carrying the state through.
        assert seen == {"provider": k.CALENDAR_PROVIDER_GOOGLE, "state": state}


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


class TestCredentialRoutes:
    """The credential HTTP surface, end to end through the real router.

    Values go in and never come back out: every assertion on a response is
    field NAMES and booleans, and the tests fail if a stored value ever appears
    in a response body.
    """

    @staticmethod
    def _client(root):
        from meetings_helpers import client_for, make_app

        return client_for(make_app(root))

    @pytest.mark.asyncio
    async def test_get_answers_empty_when_nothing_is_stored(self, root, enabled, store):
        async with self._client(root) as client:
            resp = await client.get(f"{BASE}/calendar/credentials")
            assert resp.status == 200
            assert (await resp.json())["status"] == {}

    @pytest.mark.asyncio
    async def test_put_stores_and_reports_names_never_values(self, root, enabled, store):
        async with self._client(root) as client:
            resp = await client.put(
                f"{BASE}/calendar/credentials",
                json={
                    "provider": k.CALENDAR_PROVIDER_CALDAV,
                    "values": {"username": "u@example.com", "password": "hunter2"},
                },
            )
            assert resp.status == 200
            body = await resp.text()
            assert "hunter2" not in body
            assert "u@example.com" not in body
            payload = await resp.json()
            assert payload["status"][k.CALENDAR_PROVIDER_CALDAV]["fields"] == [
                "password",
                "username",
            ]
        assert (await store.read_for(k.CALENDAR_PROVIDER_CALDAV))["password"] == "hunter2"

    @pytest.mark.asyncio
    async def test_put_rejects_an_unknown_provider(self, root, enabled, store):
        async with self._client(root) as client:
            resp = await client.put(
                f"{BASE}/calendar/credentials",
                json={"provider": "nonesuch", "values": {"username": "x"}},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "unknown_provider"

    @pytest.mark.asyncio
    async def test_put_rejects_a_provider_that_takes_no_credentials(self, root, enabled, store):
        async with self._client(root) as client:
            resp = await client.put(
                f"{BASE}/calendar/credentials",
                json={"provider": k.CALENDAR_PROVIDER_NONE, "values": {"x": "y"}},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "no_credentials_needed"

    @pytest.mark.asyncio
    async def test_put_rejects_a_non_object_values(self, root, enabled, store):
        async with self._client(root) as client:
            resp = await client.put(
                f"{BASE}/calendar/credentials",
                json={"provider": k.CALENDAR_PROVIDER_CALDAV, "values": "not-a-dict"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_put_rejects_a_non_string_value(self, root, enabled, store):
        """`field_str` would silently treat a non-string as missing; this route
        hand-validates so a malformed request cannot answer 200 while skipping a
        field the user thinks they set."""
        async with self._client(root) as client:
            resp = await client.put(
                f"{BASE}/calendar/credentials",
                json={"provider": k.CALENDAR_PROVIDER_CALDAV, "values": {"username": 5}},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_put_rejects_an_oversized_value(self, root, enabled, store):
        async with self._client(root) as client:
            resp = await client.put(
                f"{BASE}/calendar/credentials",
                json={
                    "provider": k.CALENDAR_PROVIDER_CALDAV,
                    "values": {"password": "x" * (k.MAX_CREDENTIAL_VALUE_CHARS + 1)},
                },
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_put_rejects_when_no_known_field_is_supplied(self, root, enabled, store):
        """Unknown fields are ignored (the allowlist is the control), and a body
        carrying only unknown fields is an error, not a silent 200 no-op."""
        async with self._client(root) as client:
            resp = await client.put(
                f"{BASE}/calendar/credentials",
                json={
                    "provider": k.CALENDAR_PROVIDER_CALDAV,
                    "values": {"access_token": "attacker-controlled"},
                },
            )
            assert resp.status == 400
        assert await store.read_for(k.CALENDAR_PROVIDER_CALDAV) == {}

    @pytest.mark.asyncio
    async def test_put_with_null_clears_one_field(self, root, enabled, store):
        await store.write_for(k.CALENDAR_PROVIDER_CALDAV, {"username": "u", "password": "p"})
        async with self._client(root) as client:
            resp = await client.put(
                f"{BASE}/calendar/credentials",
                json={"provider": k.CALENDAR_PROVIDER_CALDAV, "values": {"password": None}},
            )
            assert resp.status == 200
        assert await store.read_for(k.CALENDAR_PROVIDER_CALDAV) == {"username": "u"}

    @pytest.mark.asyncio
    async def test_forget_clears_credentials_and_any_pending_flow(self, root, enabled, store):
        await store.write_for(k.CALENDAR_PROVIDER_GOOGLE, {"client_id": "cid"})
        await oauth.begin(
            calroutes._OAUTH_CLIENTS[k.CALENDAR_PROVIDER_GOOGLE](),
            redirect_uri="http://127.0.0.1:5476/cb",
        )
        assert k.CALENDAR_PROVIDER_GOOGLE in oauth._pending
        async with self._client(root) as client:
            resp = await client.post(
                f"{BASE}/calendar/credentials/forget",
                json={"provider": k.CALENDAR_PROVIDER_GOOGLE},
            )
            assert resp.status == 200
            assert (await resp.json())["status"] == {}
        assert await store.read_for(k.CALENDAR_PROVIDER_GOOGLE) == {}
        assert k.CALENDAR_PROVIDER_GOOGLE not in oauth._pending


class TestOAuthStartRoute:
    @staticmethod
    def _client(root):
        from meetings_helpers import client_for, make_app

        return client_for(make_app(root))

    @pytest.mark.asyncio
    async def test_start_answers_the_consent_url(self, root, enabled, store):
        await store.write_for(k.CALENDAR_PROVIDER_GOOGLE, {"client_id": "cid"})
        async with self._client(root) as client:
            resp = await client.post(
                f"{BASE}/calendar/oauth/start",
                json={"provider": k.CALENDAR_PROVIDER_GOOGLE},
            )
            assert resp.status == 200
            payload = await resp.json()
            assert payload["ok"] is True
            # The consent URL carries the redirect derived from THIS request's
            # origin — the host-scoped-cookie property the module docstring names.
            redirect = parse_qs(urlsplit(payload["authorize_url"]).query)["redirect_uri"][0]
            assert redirect.endswith(f"{k.API_BASE}{calroutes.OAUTH_CALLBACK_PATH}")

    @pytest.mark.asyncio
    async def test_start_refuses_a_non_oauth_provider(self, root, enabled, store):
        async with self._client(root) as client:
            resp = await client.post(
                f"{BASE}/calendar/oauth/start",
                json={"provider": k.CALENDAR_PROVIDER_CALDAV},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "not_an_oauth_provider"

    @pytest.mark.asyncio
    async def test_start_maps_a_begin_failure_to_502(self, root, enabled, store):
        """No client_id stored: `begin` raises CalendarError, and the route answers
        a 502 with the reason instead of a 500 with a stack trace."""
        async with self._client(root) as client:
            resp = await client.post(
                f"{BASE}/calendar/oauth/start",
                json={"provider": k.CALENDAR_PROVIDER_GOOGLE},
            )
            assert resp.status == 502
            payload = await resp.json()
            assert payload["ok"] is False
            assert payload["code"] == "calendar_oauth_failed"


class TestOAuthCallbackSuccess:
    @pytest.mark.asyncio
    async def test_a_completed_exchange_renders_the_connected_page(self, store, monkeypatch):
        """The success exit of the callback: `complete` is stubbed AT the handler's
        boundary (the exchange itself is `oauth` module territory, tested there),
        so this locks the route's own behaviour — the audit record and the page."""
        from urllib.parse import urlencode

        from aiohttp.test_utils import make_mocked_request

        await store.write_for(k.CALENDAR_PROVIDER_GOOGLE, {"client_id": "cid"})
        await oauth.begin(
            calroutes._OAUTH_CLIENTS[k.CALENDAR_PROVIDER_GOOGLE](),
            redirect_uri="http://127.0.0.1:5476/cb",
        )
        state = oauth._pending[k.CALENDAR_PROVIDER_GOOGLE].state

        async def _complete(client, *, code, state):
            return None

        monkeypatch.setattr(oauth, "complete", _complete)
        events: list[tuple[str, str, str]] = []
        monkeypatch.setattr(
            calroutes,
            "audit",
            lambda op, res, *, outcome, error="": events.append((op, res, outcome)),
        )

        path = (
            f"{BASE}{calroutes.OAUTH_CALLBACK_PATH}?"
            f"{urlencode({'code': 'auth-code', 'state': state})}"
        )
        resp = await calroutes.handle_oauth_callback(
            make_mocked_request("GET", path, headers={"Host": "127.0.0.1:5476"})
        )
        assert resp.status == 200
        assert "Calendar connected" in resp.text
        assert events == [("meetings.calendar_oauth_callback", k.CALENDAR_PROVIDER_GOOGLE, "ok")]


class TestCredentialStoreEdges:
    """The store's failure-path contracts, driven through its public surface."""

    @pytest.mark.asyncio
    async def test_write_for_refuses_an_empty_provider_id(self, store):
        with pytest.raises(ValueError):
            await store.write_for("  ", {"username": "u"})

    @pytest.mark.asyncio
    async def test_clearing_every_field_removes_the_provider_entry(self, store):
        await store.write_for(k.CALENDAR_PROVIDER_CALDAV, {"username": "u"})
        await store.write_for(k.CALENDAR_PROVIDER_CALDAV, {"username": ""})
        assert await store.read_all() == {}

    @pytest.mark.asyncio
    async def test_clear_for_is_a_no_op_when_nothing_is_stored(self, store):
        await store.clear_for(k.CALENDAR_PROVIDER_CALDAV)
        assert not store.credentials_file().exists()

    @pytest.mark.asyncio
    async def test_clear_for_removes_only_the_named_provider(self, store):
        await store.write_for(k.CALENDAR_PROVIDER_CALDAV, {"username": "u"})
        await store.write_for(k.CALENDAR_PROVIDER_GOOGLE, {"client_id": "c"})
        await store.clear_for(k.CALENDAR_PROVIDER_CALDAV)
        assert set(await store.read_all()) == {k.CALENDAR_PROVIDER_GOOGLE}

    @pytest.mark.asyncio
    async def test_a_non_object_store_reads_as_empty_but_refuses_writes(self, store):
        """Schema-invalid root: DISPLAY degrades to empty, a WRITE refuses.

        The split is the whole point of :class:`_StoreUnreadable`: a settings page
        can render "not connected", but a read-modify-write that treated this as
        empty would rebuild the file from nothing and permanently destroy whatever
        a human could still recover from the malformed content.
        """
        store.credentials_home().mkdir(parents=True, exist_ok=True)
        store.credentials_file().write_text('["not", "a", "dict"]', encoding="utf-8")
        assert await store.read_all() == {}
        with pytest.raises(store._StoreUnreadable):
            await store.write_for(k.CALENDAR_PROVIDER_CALDAV, {"username": "u"})
        # The malformed content is untouched, not rewritten away.
        assert store.credentials_file().read_text(encoding="utf-8") == '["not", "a", "dict"]'

    @pytest.mark.asyncio
    async def test_schema_invalid_entries_poison_the_whole_read(self, store):
        """A skipped entry would be persisted-away by the next write; refuse instead."""
        store.credentials_home().mkdir(parents=True, exist_ok=True)
        store.credentials_file().write_text(
            '{"caldav": {"username": "u"}, "bad": "not-a-dict"}', encoding="utf-8"
        )
        assert await store.read_all() == {}
        with pytest.raises(store._StoreUnreadable):
            await store.write_for(k.CALENDAR_PROVIDER_GOOGLE, {"client_id": "c"})
        # The recoverable entry is still in the file for a human to salvage.
        assert "username" in store.credentials_file().read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_a_failed_owner_lockdown_warns_but_does_not_lose_the_read(
        self, store, monkeypatch
    ):
        await store.write_for(k.CALENDAR_PROVIDER_CALDAV, {"username": "u"})
        monkeypatch.setattr(
            creds, "restrict_to_owner", lambda _p: (_ for _ in ()).throw(OSError("acl"))
        )
        assert (await store.read_for(k.CALENDAR_PROVIDER_CALDAV))["username"] == "u"

    @pytest.mark.asyncio
    async def test_a_failed_owner_lockdown_on_the_temp_refuses_the_write(self, store, monkeypatch):
        """Fail-closed: a credential the module cannot protect is not published.
        The refused write leaves the previous file intact, so the existing
        connection keeps working — the opposite trade from the read path's
        warn-and-continue, deliberately."""
        from kiro_crew import atomic_write as aw

        await store.write_for(k.CALENDAR_PROVIDER_CALDAV, {"username": "before"})
        monkeypatch.setattr(
            aw.platform_compat,
            "restrict_to_owner",
            lambda _p: (_ for _ in ()).throw(OSError("acl")),
        )
        with pytest.raises(OSError):
            await store.write_for(k.CALENDAR_PROVIDER_CALDAV, {"username": "after"})
        monkeypatch.undo()
        assert (await store.read_for(k.CALENDAR_PROVIDER_CALDAV))["username"] == "before"
        assert list(store.credentials_home().glob("*.tmp")) == []

    @pytest.mark.asyncio
    async def test_a_failed_replace_cleans_up_the_temp_and_raises(self, store, monkeypatch):
        """The atomic-write contract: a failure between temp-write and replace
        must not leave a credential-bearing temp file behind."""
        import os as _os

        from kiro_crew import atomic_write as aw

        def _boom(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(aw, "replace_with_retry", _boom)
        with pytest.raises(OSError):
            await store.write_for(k.CALENDAR_PROVIDER_CALDAV, {"username": "u"})
        monkeypatch.undo()
        leftovers = (
            list(store.credentials_home().glob("*.tmp"))
            if (store.credentials_home().exists())
            else []
        )
        assert leftovers == []
        assert _os.path.exists(store.credentials_file()) is False

    def test_the_production_path_lands_under_workspace_meetings(self, monkeypatch):
        """The path the sensitive-file gate protects and the path the store writes
        must be the same one — this pins the store's side of that contract."""
        creds.set_credentials_home(None)
        try:
            monkeypatch.setattr(creds, "config_dir", lambda: Path("/tmp/crew-home"))
            assert creds.credentials_home() == Path("/tmp/crew-home/workspace/meetings")
        finally:
            creds.set_credentials_home(None)

    def test_a_broken_resolver_degrades_to_the_documented_default(self, monkeypatch):
        creds.set_credentials_home(None)
        try:
            monkeypatch.setattr(
                creds, "config_dir", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
            )
            monkeypatch.setenv("KIROCREW_HOME", "/tmp/override-home")
            assert creds.credentials_home() == Path("/tmp/override-home/workspace/meetings")
            monkeypatch.delenv("KIROCREW_HOME")
            assert (
                creds.credentials_home()
                == Path.home() / ".kiro" / "crew" / "workspace" / "meetings"
            )
        finally:
            creds.set_credentials_home(None)
