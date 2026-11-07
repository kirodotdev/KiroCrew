"""The relay viewer's control plane client against an in-process fake.

Answers come from ``backend/list-crew-endpoints.response``,
``backend/create-crew-access-token.response`` and
``backend/create-crew-access-token.refusals``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Dict, List, Optional

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer
from relay_client_helpers import fixture

from kiro_crew.relay_viewer import cps
from kiro_crew.relay_viewer.cps import CpsRefused, create_crew_access_token, list_crew_endpoints

pytestmark = pytest.mark.asyncio

BEARER = "person-sign-in-not-a-credential-7f3a"
TEAM = "teamaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
PERSONAL = "personalbbbbbbbbbbbbbbbbbbbbbbbb"
LIST = fixture("backend/list-crew-endpoints.response")
MINT = fixture("backend/create-crew-access-token.response")
REFUSALS = fixture("backend/create-crew-access-token.refusals")["refusals"]


class FakeCps:
    """Answers each POST with the next scripted (status, body, headers)."""

    def __init__(self) -> None:
        self.script: List[tuple[int, Any, Dict[str, str]]] = []
        self.requests: List[Dict[str, Any]] = []
        self.server: Optional[TestServer] = None

    def answer(self, status: int, body: Any, headers: Optional[Dict[str, str]] = None) -> None:
        self.script.append((status, body, headers or {}))

    @property
    def url(self) -> str:
        assert self.server is not None
        return str(self.server.make_url("/"))

    async def handle(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(
            {"path": request.path, "headers": dict(request.headers), "body": await request.json()}
        )
        status, body, headers = self.script.pop(0)
        if isinstance(body, bytes):
            return web.Response(status=status, body=body, headers=headers)
        return web.json_response(body, status=status, headers=headers)


@pytest_asyncio.fixture
async def fake() -> AsyncIterator[FakeCps]:
    fake = FakeCps()
    app = web.Application()
    app.router.add_post("/{tail:.*}", fake.handle)
    fake.server = TestServer(app, host="127.0.0.1", port=0)
    await fake.server.start_server()
    try:
        yield fake
    finally:
        await fake.server.close()


def page(index: int) -> Dict[str, Any]:
    return json.loads(json.dumps(LIST["examples"][index]["body"]))


async def test_the_list_follows_pages_and_sends_the_awsjson_shape(fake: FakeCps) -> None:
    fake.answer(200, page(0))
    fake.answer(200, page(1))
    crews = await list_crew_endpoints(fake.url, BEARER)
    assert [c.endpoint_id for c in crews] == [TEAM, PERSONAL]
    assert crews[0].crew_type == "TEAM" and crews[0].presence == "CONNECTED"
    assert crews[1].crew_url == f"https://{PERSONAL}.crew.example.invalid"
    assert crews[1].relay_connection_status == "NOT_CONNECTED"
    assert [r["body"] for r in fake.requests] == [{}, {"nextToken": "page-2-opaque"}]
    for request in fake.requests:
        headers = {k.lower(): v for k, v in request["headers"].items()}
        assert request["path"] == "/"
        assert headers["x-amz-target"] == "KiroControlPlaneBearerService.ListCrewEndpoints"
        assert headers["content-type"] == "application/x-amz-json-1.0"
        assert headers["authorization"] == f"Bearer {BEARER}"
        assert "tokentype" not in headers


async def test_pages_stop_at_the_cap(fake: FakeCps, caplog: pytest.LogCaptureFixture) -> None:
    for _ in range(cps.MAX_PAGES):
        fake.answer(200, {"crewEndpoints": [], "nextToken": "more"})
    assert await list_crew_endpoints(fake.url, BEARER) == ()
    assert len(fake.requests) == cps.MAX_PAGES
    assert "more than" in caplog.text


@pytest.mark.parametrize("case", LIST["unusableEntries"], ids=lambda c: c["name"])
async def test_an_unusable_entry_is_left_out(fake: FakeCps, case: Dict[str, Any]) -> None:
    body = page(1)
    body["crewEndpoints"].insert(0, case["entry"])
    fake.answer(200, body)
    assert [c.endpoint_id for c in await list_crew_endpoints(fake.url, BEARER)] == [PERSONAL]


async def test_a_repeated_endpoint_keeps_the_first_and_odd_presence_is_unknown(
    fake: FakeCps,
) -> None:
    body = page(0)
    first = body["crewEndpoints"][0]
    first["presence"], first["relayConnectionStatus"] = 7, ["CONNECTED"]
    body["crewEndpoints"].append(dict(first, name="Second copy"))
    del body["nextToken"]
    fake.answer(200, body)
    (crew,) = await list_crew_endpoints(fake.url, BEARER)
    assert crew.name == "Build team Crew"
    assert (crew.presence, crew.relay_connection_status) == ("UNKNOWN", "UNKNOWN")


async def test_remote_access_off_in_the_list_is_typed(fake: FakeCps) -> None:
    fake.answer(200, page(2))
    with pytest.raises(CpsRefused) as refused:
        await list_crew_endpoints(fake.url, BEARER)
    assert refused.value.code == cps.REMOTE_ACCESS_DISABLED


async def test_the_mint_sends_only_the_endpoint_and_reads_the_token(fake: FakeCps) -> None:
    example = MINT["examples"][0]
    fake.answer(200, example["body"], example["headers"])
    token = await create_crew_access_token(fake.url, BEARER, TEAM)
    assert token.access_token == "example-access-token-not-a-credential"
    assert token.expires_at == 1762470300.0
    assert token.renew_in_seconds == 236.0
    assert token.server_date == 1762470000.0
    assert "example-access-token" not in repr(token)
    (request,) = fake.requests
    headers = {k.lower(): v for k, v in request["headers"].items()}
    assert request["body"] == MINT["request"]["body"] == {"endpointId": TEAM}
    assert headers["x-amz-target"] == MINT["request"]["headers"]["X-Amz-Target"]
    assert headers["content-type"] == "application/x-amz-json-1.0"
    assert "tokentype" not in headers


async def test_an_iso_expiry_without_renew_in_is_accepted(fake: FakeCps) -> None:
    fake.answer(200, MINT["examples"][1]["body"])
    token = await create_crew_access_token(fake.url, BEARER, TEAM)
    assert (token.expires_at, token.renew_in_seconds) == (1762470300.0, None)


@pytest.mark.parametrize(
    "body",
    [
        {"expiresAt": 1762470300},
        {"accessToken": "", "expiresAt": 1762470300},
        {"accessToken": "t", "expiresAt": "soon"},
        {"accessToken": "t", "expiresAt": 1762470300, "renewInSeconds": 0},
        {"accessToken": "t", "expiresAt": 1762470300, "renewInSeconds": True},
    ],
)
async def test_a_mint_answer_missing_a_field_is_a_bad_answer(fake: FakeCps, body: Any) -> None:
    fake.answer(200, body)
    with pytest.raises(CpsRefused) as refused:
        await create_crew_access_token(fake.url, BEARER, TEAM)
    assert refused.value.code == cps.BAD_ANSWER


@pytest.mark.parametrize("case", REFUSALS, ids=lambda c: f"{c['code']}-{c.get('reason')}")
async def test_each_refusal_maps_to_its_typed_code(fake: FakeCps, case: Dict[str, Any]) -> None:
    fake.answer(case["httpStatus"], case["example"], case.get("exampleHeaders"))
    fake.answer(case["httpStatus"], case["example"], case.get("exampleHeaders"))
    with pytest.raises(CpsRefused) as minted:
        await create_crew_access_token(fake.url, BEARER, TEAM)
    with pytest.raises(CpsRefused) as listed:
        await list_crew_endpoints(fake.url, BEARER)
    for refused in (minted.value, listed.value):
        assert refused.code == case["code"] and refused.status == case["httpStatus"]
        assert case["example"]["message"] not in str(refused)
    assert minted.value.retry_after == (7.0 if case["code"] == cps.THROTTLED else None)


async def test_the_error_type_header_names_a_refusal_with_no_body_type(fake: FakeCps) -> None:
    fake.answer(409, {}, {"X-Amzn-ErrorType": "CrewOfflineException:http://example.invalid/"})
    with pytest.raises(CpsRefused) as refused:
        await create_crew_access_token(fake.url, BEARER, TEAM)
    assert refused.value.code == cps.CREW_OFFLINE


@pytest.mark.parametrize(
    ("status", "headers", "code", "wait"),
    [
        (429, {"Retry-After": "600"}, cps.THROTTLED, 60.0),
        (429, {"Retry-After": "nan"}, cps.THROTTLED, None),
        (503, {}, cps.SERVICE_ERROR, None),
        (403, {}, cps.ACCESS_DENIED, None),
    ],
)
async def test_a_status_with_no_type(
    fake: FakeCps, status: int, headers: Dict[str, str], code: str, wait: Optional[float]
) -> None:
    fake.answer(status, b"", headers)
    with pytest.raises(CpsRefused) as refused:
        await create_crew_access_token(fake.url, BEARER, TEAM)
    assert (refused.value.code, refused.value.retry_after) == (code, wait)


async def test_a_redirect_is_never_followed(fake: FakeCps) -> None:
    fake.answer(307, b"", {"Location": "https://elsewhere.example.invalid/"})
    with pytest.raises(CpsRefused) as refused:
        await create_crew_access_token(fake.url, BEARER, TEAM)
    assert refused.value.code == cps.UNREACHABLE
    assert len(fake.requests) == 1 and not fake.script


async def test_an_answer_over_the_cap_is_refused(fake: FakeCps) -> None:
    fake.answer(200, b"{" + b" " * cps.ANSWER_MAX_BYTES + b"}")
    with pytest.raises(CpsRefused) as refused:
        await list_crew_endpoints(fake.url, BEARER)
    assert refused.value.code == cps.BAD_ANSWER


async def test_a_body_that_is_not_json_is_a_bad_answer(fake: FakeCps) -> None:
    fake.answer(200, b"not json")
    with pytest.raises(CpsRefused) as refused:
        await list_crew_endpoints(fake.url, BEARER)
    assert refused.value.code == cps.BAD_ANSWER


@pytest.mark.parametrize(
    ("url", "bearer", "endpoint", "code"),
    [
        ("https://cps.example.invalid/", "", TEAM, cps.SIGNED_OUT),
        ("", BEARER, TEAM, cps.NOT_SET_UP),
        ("http://cps.example.invalid/", BEARER, TEAM, cps.NOT_SET_UP),
        ("https://cps.example.invalid/", BEARER, "Not-An-Endpoint", cps.INVALID_REQUEST),
    ],
)
async def test_nothing_is_sent_before_the_inputs_check(
    url: str, bearer: str, endpoint: str, code: str
) -> None:
    with pytest.raises(CpsRefused) as refused:
        await create_crew_access_token(url, bearer, endpoint)
    assert refused.value.code == code and refused.value.status is None


async def test_a_service_it_cannot_reach_is_unreachable() -> None:
    with pytest.raises(CpsRefused) as refused:
        await list_crew_endpoints("http://127.0.0.1:9/", BEARER)
    assert refused.value.code == cps.UNREACHABLE


async def test_no_sign_in_or_token_reaches_the_log(
    fake: FakeCps, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    fake.answer(200, MINT["examples"][0]["body"])
    fake.answer(403, REFUSALS[0]["example"])
    fake.answer(200, page(1))
    await create_crew_access_token(fake.url, BEARER, TEAM)
    with pytest.raises(CpsRefused):
        await create_crew_access_token(fake.url, BEARER, TEAM)
    await list_crew_endpoints(fake.url, BEARER)
    assert TEAM in caplog.text
    for secret in (BEARER, "example-access-token-not-a-credential"):
        assert secret not in caplog.text
