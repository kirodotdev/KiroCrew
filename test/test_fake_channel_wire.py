"""Fidelity tests for the wire-level channel fake.

The wire harness exists so a channel's real client, transport and renderer run
against canned vendor bytes. That is only worth anything if the canned response
behaves like ``aiohttp``: a response object written by hand can diverge from
the library in ways that raise no ``AttributeError``, so a suite stays green
while production breaks on the same bytes.

These tests pin the property that removes that risk: the response read side is
``aiohttp``'s own code, bound onto the fake's response object. The identity
assertions are the load-bearing ones -- replacing any of those methods with a
local re-implementation fails here, whatever the replacement happens to return.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiohttp
import pytest
from aiohttp import ClientResponse

from kiro_crew.teams.client import TeamsClient, TeamsSendError
from kiro_crew.testing.channel_fixtures import load_fixture
from kiro_crew.testing.fake_channel_wire import (
    FakeWireSession,
    UnroutedRequestError,
    WireResponse,
    _FakeResponseCM,
)

CHANNEL_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "channels"

_CREDENTIAL = load_fixture("teams", "app_credential", root=CHANNEL_FIXTURES).payload
_ACTIVITY_SENT = load_fixture("teams", "activity_sent", root=CHANNEL_FIXTURES).payload

_SERVICE_URL = "https://smba.trafficmanager.net/teams"


def _read(resp: WireResponse) -> _FakeResponseCM:
    """Drive one canned response through the session, as a client would."""
    wire = FakeWireSession().route("GET", "/probe", resp)
    return wire.get("https://vendor.invalid/probe")


class TestTheReadSideIsAiohttpsOwnCode:
    """The property the whole harness rests on.

    A test asserting only observable behaviour can be satisfied by a local
    re-implementation that happens to agree today and drifts on the next
    ``aiohttp`` release. Asserting object identity cannot.
    """

    @pytest.mark.parametrize(
        "name",
        ["json", "text", "get_encoding", "raise_for_status", "ok"],
    )
    def test_each_read_method_is_the_aiohttp_function_itself(self, name: str) -> None:
        mine = _FakeResponseCM.__dict__.get(name)
        theirs = ClientResponse.__dict__.get(name)
        assert mine is not None, f"{name} must be bound on the fake response"
        assert mine is theirs, (
            f"{name} must BE aiohttp's own implementation, not a copy of it: a "
            f"hand-written version is what lets a wire suite verify the client "
            f"against our model of aiohttp instead of against aiohttp"
        )

    def test_the_mimetype_properties_come_from_aiohttps_mixin(self) -> None:
        """``content_type`` and ``charset`` parse the header aiohttp's way."""
        assert isinstance(_FakeResponseCM.content_type, type(ClientResponse.content_type))
        resp = _read(WireResponse(body={"a": 1}, content_type="application/json; charset=utf-8"))
        # Parameters are stripped, so an equality check against a bare mimetype
        # behaves for a client the way it behaves in production.
        assert resp.content_type == "application/json"
        assert resp.charset == "utf-8"


class TestContentTypeIsEnforced:
    """The divergence with a live production consequence.

    A vendor answering JSON under a non-JSON content type is exactly the iLink
    QR shape: the bytes parse, so a lax fake hands the client a dict and the
    suite passes, while ``aiohttp`` refuses the same response in production.
    """

    def test_a_json_body_under_a_non_json_content_type_raises(self) -> None:
        resp = _read(WireResponse(body={"token": "t"}, content_type="text/html"))
        with pytest.raises(aiohttp.ContentTypeError):
            asyncio.run(resp.json())

    def test_content_type_none_reads_the_body_anyway(self) -> None:
        """Three of the four channel clients pass this, so it must keep working."""
        resp = _read(WireResponse(body={"token": "t"}, content_type="text/html"))
        assert asyncio.run(resp.json(content_type=None)) == {"token": "t"}

    def test_a_json_suffix_mimetype_is_accepted(self) -> None:
        """``application/vnd.api+json`` is JSON; an equality check would refuse it."""
        resp = _read(WireResponse(body={"a": 1}, content_type="application/vnd.api+json"))
        assert asyncio.run(resp.json()) == {"a": 1}

    def test_a_charset_parameter_does_not_defeat_the_match(self) -> None:
        resp = _read(WireResponse(body={"a": 1}, content_type="application/json; charset=utf-8"))
        assert asyncio.run(resp.json()) == {"a": 1}

    def test_the_refusal_names_the_request_that_caused_it(self) -> None:
        """A bare mimetype in the message cannot be traced to a call site."""
        wire = FakeWireSession().route("POST", "/v1/send", WireResponse(content_type="text/html"))
        resp = wire.post("https://vendor.invalid/v1/send", json={"x": 1})
        with pytest.raises(aiohttp.ContentTypeError) as caught:
            asyncio.run(resp.json())
        assert caught.value.request_info.method == "POST"
        assert "/v1/send" in str(caught.value.request_info.url)


class TestBodyDecodingMatchesAiohttp:
    def test_an_empty_body_reads_as_none(self) -> None:
        """``aiohttp`` returns None for an empty body rather than raising."""
        resp = _read(WireResponse(body=""))
        assert asyncio.run(resp.json()) is None

    def test_text_decodes_the_bytes_that_would_be_on_the_wire(self) -> None:
        resp = _read(WireResponse(body="h\u00e9llo", content_type="text/plain"))
        assert asyncio.run(resp.text()) == "h\u00e9llo"

    def test_read_returns_the_raw_bytes(self) -> None:
        resp = _read(WireResponse(body=b"\x89PNG\r\n", content_type="image/png"))
        assert asyncio.run(resp.read()) == b"\x89PNG\r\n"


class TestStatusRulesMatchAiohttp:
    @pytest.mark.parametrize(
        "status, expected",
        [(100, True), (200, True), (302, True), (399, True), (400, False), (503, False)],
    )
    def test_ok_is_aiohttps_under_400_rule(self, status: int, expected: bool) -> None:
        """An informational status is ok to aiohttp; a 4xx is not."""
        assert _read(WireResponse(status=status)).ok is expected

    def test_raise_for_status_raises_what_production_raises(self) -> None:
        resp = _read(WireResponse(status=404))
        with pytest.raises(aiohttp.ClientResponseError) as caught:
            resp.raise_for_status()
        assert caught.value.status == 404
        # The reason phrase is derived from the status, so a fixture that sets
        # only a status still produces the error a real server would.
        assert caught.value.message == "Not Found"

    def test_raise_for_status_is_silent_on_success(self) -> None:
        assert _read(WireResponse(status=200)).raise_for_status() is None


class TestHeadersAreMatchedCaseInsensitively:
    """A fixture header is one field however it is spelled.

    A plain dict would make ``content-type`` and ``Content-Type`` two separate
    entries. ``HeadersMixin`` asks for aiohttp's spelling, so the lowercase
    form would be looked past and the default answered instead -- letting
    ``.json()`` accept a body production refuses, which is the divergence this
    module exists to remove.
    """

    @pytest.mark.parametrize("spelling", ["Content-Type", "content-type", "CONTENT-TYPE"])
    def test_any_spelling_of_content_type_refuses_a_non_json_read(self, spelling: str) -> None:
        resp = _read(WireResponse(body={"token": "t"}, headers={spelling: "text/html"}))
        assert resp.content_type == "text/html"
        with pytest.raises(aiohttp.ContentTypeError):
            asyncio.run(resp.json())

    def test_a_lowercase_fixture_header_overrides_the_content_type_field(self) -> None:
        resp = _read(
            WireResponse(content_type="application/json", headers={"content-type": "text/plain"})
        )
        assert resp.content_type == "text/plain"

    def test_the_header_mapping_itself_is_case_insensitive_to_callers(self) -> None:
        resp = _read(WireResponse(headers={"X-Rate-Limit": "9"}))
        assert resp.headers["x-rate-limit"] == "9"
        assert resp.request_info.headers["X-RATE-LIMIT"] == "9"

    def test_one_content_type_field_is_carried_not_two(self) -> None:
        resp = _read(
            WireResponse(content_type="application/json", headers={"content-type": "text/csv"})
        )
        assert [v for k, v in resp.headers.items() if k.lower() == "content-type"] == ["text/csv"]


class TestTheStrictRoutingContractSurvives:
    """The fail-closed behaviour four Teams tests depend on is unchanged."""

    def test_an_unrouted_endpoint_still_fails_closed(self) -> None:
        wire = FakeWireSession()
        with pytest.raises(UnroutedRequestError):
            wire.get("https://vendor.invalid/unpinned")

    def test_an_exhausted_script_still_fails_closed(self) -> None:
        wire = FakeWireSession().route("GET", "/poll", [WireResponse(body={"n": 1})])
        assert asyncio.run(wire.get("https://vendor.invalid/poll").json()) == {"n": 1}
        with pytest.raises(UnroutedRequestError):
            wire.get("https://vendor.invalid/poll")


class TestTheFullStackSeesTheProductionFailure:
    """End to end: the enforcement reaches a real client's own ``.json()`` call.

    The Teams credential exchange reads its token with a bare ``.json()``. A
    token endpoint answering JSON under ``text/html`` therefore fails, and the
    send is abandoned instead of proceeding with a token the client would never
    have obtained against the real endpoint.
    """

    def test_a_token_endpoint_answering_html_abandons_the_send(self) -> None:
        wire = (
            FakeWireSession()
            .route(
                "POST",
                "/oauth2/v2.0/token",
                WireResponse(body=_CREDENTIAL, content_type="text/html"),
            )
            .route("POST", "/v3/conversations/", _ACTIVITY_SENT)
        )
        client = TeamsClient(app_id="app-1", app_password="secret-pw", tenant_id="tenant-1")
        client._session = wire

        with pytest.raises(TeamsSendError):
            asyncio.run(client.send_message("conv-1", "hi", _SERVICE_URL))

        assert not [
            r for r in wire.requests if "/v3/conversations/" in r.path
        ], "a refused token exchange must not be followed by an activity post"

    def test_the_same_exchange_under_a_json_content_type_succeeds(self) -> None:
        """The control: only the content type differs from the failing case."""
        wire = (
            FakeWireSession()
            .route(
                "POST",
                "/oauth2/v2.0/token",
                WireResponse(body=_CREDENTIAL, content_type="application/json"),
            )
            .route("POST", "/v3/conversations/", _ACTIVITY_SENT)
        )
        client = TeamsClient(app_id="app-1", app_password="secret-pw", tenant_id="tenant-1")
        client._session = wire

        asyncio.run(client.send_message("conv-1", "hi", _SERVICE_URL))

        assert [r for r in wire.requests if "/v3/conversations/" in r.path]
