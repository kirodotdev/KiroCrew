"""Wire-level tests for the Microsoft Teams channel.

Runs the REAL TeamsClient against a fake aiohttp session. Teams is the most
worthwhile channel to test at this layer because its outbound path carries an
app bearer credential to a **server-supplied** URL (``serviceUrl`` arrives on
the inbound activity), so the request-construction code is a security boundary,
not just a serialization detail.

The credential exchange posts FORM-encoded data (``data={...}``), not JSON --
inspected via ``RecordedRequest.form``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from kiro_crew.teams.client import TeamsClient
from kiro_crew.testing.channel_fixtures import load_fixture
from kiro_crew.testing.fake_channel_wire import FakeWireSession, WireResponse

CHANNEL_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "channels"

_CREDENTIAL = load_fixture("teams", "app_credential", root=CHANNEL_FIXTURES).payload
_ACTIVITY_SENT = load_fixture("teams", "activity_sent", root=CHANNEL_FIXTURES).payload

_SERVICE_URL = "https://smba.trafficmanager.net/teams"


def _client(wire: FakeWireSession) -> TeamsClient:
    client = TeamsClient(app_id="app-1", app_password="secret-pw", tenant_id="tenant-1")
    client._session = wire
    return client


def _wired() -> FakeWireSession:
    return (
        FakeWireSession()
        .route("POST", "/oauth2/v2.0/token", _CREDENTIAL)
        .route("POST", "/v3/conversations/", _ACTIVITY_SENT)
    )


class TestOutboundCredentialIsNeverSentInPlaintext:
    """The refusal that makes serviceUrl safe to trust.

    ``serviceUrl`` comes from the inbound activity -- i.e. from the network. If
    it were honoured verbatim, an ``http://`` value would put the app bearer
    credential on the wire in plaintext. The client refuses; this asserts the
    refusal by observing that NO request is made at all.
    """

    def test_an_http_service_url_sends_nothing(self) -> None:
        wire = _wired()
        client = _client(wire)

        out = asyncio.run(
            client.send_message("conv-1", "hi", "http://smba.trafficmanager.net/teams")
        )

        assert out is None
        assert wire.requests == [], (
            "a non-https serviceUrl must produce NO request -- not even the "
            "credential exchange, which would itself leak nothing but proves the "
            "guard runs before any network work"
        )

    def test_a_scheme_relative_or_bare_host_sends_nothing(self) -> None:
        wire = _wired()
        client = _client(wire)

        assert asyncio.run(client.send_message("conv-1", "hi", "//evil.example")) is None
        assert asyncio.run(client.send_message("conv-1", "hi", "smba.example")) is None
        assert wire.requests == []

    def test_an_empty_service_url_sends_nothing(self) -> None:
        wire = _wired()
        client = _client(wire)

        assert asyncio.run(client.send_message("conv-1", "hi", "")) is None
        assert wire.requests == []

    def test_an_https_service_url_is_accepted(self) -> None:
        wire = _wired()
        client = _client(wire)

        out = asyncio.run(client.send_message("conv-1", "hello", _SERVICE_URL))

        assert out == _ACTIVITY_SENT["id"]
        activity = [r for r in wire.requests if "/v3/conversations/" in r.path][0]
        assert activity.url.startswith("https://")


class TestActivityRequestShape:
    def test_the_activity_url_and_body_match_the_connector_contract(self) -> None:
        wire = _wired()
        client = _client(wire)

        asyncio.run(client.send_message("conv-1", "hello", _SERVICE_URL))

        activity = [r for r in wire.requests if "/v3/conversations/" in r.path][0]
        assert activity.path == "/teams/v3/conversations/conv-1/activities"
        assert activity.json_body == {"type": "message", "text": "hello"}
        assert activity.headers["Authorization"] == "Bearer " + _CREDENTIAL["access_token"]

    def test_typing_posts_a_typing_activity(self) -> None:
        wire = _wired()
        client = _client(wire)

        asyncio.run(client.send_typing("conv-1", _SERVICE_URL))

        activity = [r for r in wire.requests if "/v3/conversations/" in r.path][0]
        assert activity.json_body == {"type": "typing"}

    def test_empty_text_is_replaced_so_the_connector_never_400s(self) -> None:
        wire = _wired()
        client = _client(wire)

        asyncio.run(client.send_message("conv-1", "", _SERVICE_URL))

        activity = [r for r in wire.requests if "/v3/conversations/" in r.path][0]
        assert activity.json_body["text"], "an empty activity text is rejected by the API"


class TestCredentialExchange:
    def test_the_exchange_uses_the_client_credentials_grant(self) -> None:
        wire = _wired()
        client = _client(wire)

        asyncio.run(client.send_message("conv-1", "hi", _SERVICE_URL))

        auth = [r for r in wire.requests if "/oauth2/v2.0/token" in r.path][0]
        form = auth.form
        assert form["grant_type"] == "client_credentials"
        assert form["client_id"] == "app-1"
        assert form["scope"].endswith("/.default")
        # The tenant is templated into the path, so a wrong tenant is a wrong URL.
        assert "/tenant-1/" in auth.path

    def test_the_credential_is_cached_across_sends(self) -> None:
        """A per-message exchange would triple outbound latency and hit throttling."""
        wire = _wired()
        client = _client(wire)

        asyncio.run(client.send_message("conv-1", "one", _SERVICE_URL))
        asyncio.run(client.send_message("conv-1", "two", _SERVICE_URL))

        exchanges = [r for r in wire.requests if "/oauth2/v2.0/token" in r.path]
        assert len(exchanges) == 1, "the cached credential must be reused"

    def test_a_failed_exchange_does_not_post_an_activity(self) -> None:
        wire = (
            FakeWireSession()
            .route("POST", "/oauth2/v2.0/token", WireResponse(body={}, status=401))
            .route("POST", "/v3/conversations/", _ACTIVITY_SENT)
        )
        client = _client(wire)

        out = asyncio.run(client.send_message("conv-1", "hi", _SERVICE_URL))

        assert out is None
        assert not [r for r in wire.requests if "/v3/conversations/" in r.path], (
            "an unauthenticated activity post would be a wasted 401 round trip"
        )
        assert client.last_error


class TestConnectorErrors:
    def test_a_4xx_activity_is_reported_not_raised(self) -> None:
        wire = (
            FakeWireSession()
            .route("POST", "/oauth2/v2.0/token", _CREDENTIAL)
            .route("POST", "/v3/conversations/", WireResponse(body="forbidden", status=403))
        )
        client = _client(wire)

        out = asyncio.run(client.send_message("conv-1", "hi", _SERVICE_URL))

        assert out is None
        assert client.last_error.startswith("send failed"), (
            "a connector failure must surface on last_error for the status badge"
        )
