"""The relay viewer's control plane client against an in-process fake.

Answers come from ``backend/list-crew-endpoints.response``,
``backend/create-crew-access-token.response`` and
``backend/create-crew-access-token.refusals``.
"""

from __future__ import annotations

import json
import logging
import time
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
#: The relay domain the fixture's usable entries sit under.
DOMAIN = "crew.example.invalid"
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
    crews = await list_crew_endpoints(fake.url, BEARER, crew_domain=DOMAIN)
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
    assert await list_crew_endpoints(fake.url, BEARER, crew_domain=DOMAIN) == ()
    assert len(fake.requests) == cps.MAX_PAGES
    assert "more than" in caplog.text


@pytest.mark.parametrize("case", LIST["unusableEntries"], ids=lambda c: c["name"])
async def test_an_unusable_entry_is_left_out(fake: FakeCps, case: Dict[str, Any]) -> None:
    body = page(1)
    body["crewEndpoints"].insert(0, case["entry"])
    fake.answer(200, body)
    crews = await list_crew_endpoints(fake.url, BEARER, crew_domain=DOMAIN)
    assert [c.endpoint_id for c in crews] == [PERSONAL]


async def test_a_repeated_endpoint_keeps_the_first_and_odd_presence_is_unknown(
    fake: FakeCps,
) -> None:
    body = page(0)
    first = body["crewEndpoints"][0]
    first["presence"], first["relayConnectionStatus"] = 7, ["CONNECTED"]
    body["crewEndpoints"].append(dict(first, name="Second copy"))
    del body["nextToken"]
    fake.answer(200, body)
    (crew,) = await list_crew_endpoints(fake.url, BEARER, crew_domain=DOMAIN)
    assert crew.name == "Build team Crew"
    assert (crew.presence, crew.relay_connection_status) == ("UNKNOWN", "UNKNOWN")


async def test_last_seen_is_read_from_the_list(fake: FakeCps) -> None:
    fake.answer(200, page(0))
    fake.answer(200, page(1))
    crews = await list_crew_endpoints(fake.url, BEARER, crew_domain=DOMAIN)
    assert [c.last_seen_at for c in crews] == [1762470000.0, None]


@pytest.mark.parametrize(
    "value",
    [
        "missing",
        None,
        [1762470000],
        {"at": 1762470000},
        True,
        "not a time",
        "2026-11-08T10:00:00",  # no zone
        -1,
        1_000_000_000,  # 2001, before any Crew existed
        4_102_444_800,  # 2100
        1e400,  # inf once parsed
    ],
)
async def test_a_bad_last_seen_is_dropped_and_keeps_the_entry(fake: FakeCps, value: Any) -> None:
    body = page(0)
    del body["nextToken"]
    if value == "missing":
        del body["crewEndpoints"][0]["lastSeenAt"]
    else:
        body["crewEndpoints"][0]["lastSeenAt"] = value
    fake.answer(200, body)
    (crew,) = await list_crew_endpoints(fake.url, BEARER, crew_domain=DOMAIN)
    assert crew.endpoint_id == TEAM and crew.last_seen_at is None


async def test_last_seen_a_little_ahead_of_this_clock_is_kept(fake: FakeCps) -> None:
    ahead = int(time.time()) + 120
    body = page(0)
    del body["nextToken"]
    body["crewEndpoints"][0]["lastSeenAt"] = ahead
    fake.answer(200, body)
    (crew,) = await list_crew_endpoints(fake.url, BEARER, crew_domain=DOMAIN)
    assert crew.last_seen_at == float(ahead)


async def test_remote_access_off_in_the_list_is_typed(fake: FakeCps) -> None:
    fake.answer(200, page(2))
    with pytest.raises(CpsRefused) as refused:
        await list_crew_endpoints(fake.url, BEARER, crew_domain=DOMAIN)
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
        await list_crew_endpoints(fake.url, BEARER, crew_domain=DOMAIN)
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
        await list_crew_endpoints(fake.url, BEARER, crew_domain=DOMAIN)
    assert refused.value.code == cps.BAD_ANSWER


async def test_a_body_that_is_not_json_is_a_bad_answer(fake: FakeCps) -> None:
    fake.answer(200, b"not json")
    with pytest.raises(CpsRefused) as refused:
        await list_crew_endpoints(fake.url, BEARER, crew_domain=DOMAIN)
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
        await list_crew_endpoints("http://127.0.0.1:9/", BEARER, crew_domain=DOMAIN)
    assert refused.value.code == cps.UNREACHABLE
    assert refused.value.request_id is None


REQUEST_ID = "3f1c2b9e-0d4a-4c7e-9b51-6a2f8e7d1c00"


@pytest.mark.parametrize(
    ("value", "kept"),
    [
        (REQUEST_ID, REQUEST_ID),
        ("ABC-123", "ABC-123"),
        ("a" * 64, "a" * 64),
        ("a" * 65, None),
        ("", None),
        (None, None),
        ("id with space", None),
        ("id\x00nul", None),
        ("id\ttab", None),
        ("id\x7fdel", None),
        ("id;semi", None),
        ("id\u00e9", None),
    ],
)
def test_a_request_id_is_kept_only_when_short_and_plain(
    value: Optional[str], kept: Optional[str]
) -> None:
    assert cps._request_id(value) == kept


async def test_a_refusal_carries_the_request_id_and_logs_it(
    fake: FakeCps, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    fake.answer(503, b"", {"x-amzn-RequestId": REQUEST_ID})
    with pytest.raises(CpsRefused) as refused:
        await list_crew_endpoints(fake.url, BEARER, crew_domain=DOMAIN)
    assert (refused.value.code, refused.value.request_id) == (cps.SERVICE_ERROR, REQUEST_ID)
    assert f"request {REQUEST_ID}" in caplog.text


async def test_a_refusal_with_no_request_id_header_has_none(
    fake: FakeCps, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    fake.answer(429, b"", {"Retry-After": "5"})
    with pytest.raises(CpsRefused) as refused:
        await create_crew_access_token(fake.url, BEARER, TEAM)
    assert (refused.value.code, refused.value.request_id) == (cps.THROTTLED, None)
    assert "request -)" in caplog.text


async def test_a_request_id_over_the_cap_is_dropped(fake: FakeCps) -> None:
    fake.answer(503, b"", {"x-amzn-RequestId": "a" * 65})
    with pytest.raises(CpsRefused) as refused:
        await create_crew_access_token(fake.url, BEARER, TEAM)
    assert refused.value.request_id is None


async def test_a_refusal_in_a_200_answer_carries_the_request_id(fake: FakeCps) -> None:
    fake.answer(200, page(2), {"x-amzn-RequestId": REQUEST_ID})
    with pytest.raises(CpsRefused) as refused:
        await list_crew_endpoints(fake.url, BEARER, crew_domain=DOMAIN)
    assert (refused.value.code, refused.value.request_id) == (
        cps.REMOTE_ACCESS_DISABLED,
        REQUEST_ID,
    )


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
    await list_crew_endpoints(fake.url, BEARER, crew_domain=DOMAIN)
    assert TEAM in caplog.text
    for secret in (BEARER, "example-access-token-not-a-credential"):
        assert secret not in caplog.text


async def test_an_external_idp_sign_in_sends_the_token_type_on_both_calls(fake: FakeCps) -> None:
    fake.answer(200, page(1))
    fake.answer(200, MINT["examples"][0]["body"])
    await list_crew_endpoints(fake.url, BEARER, crew_domain=DOMAIN, token_type="EXTERNAL_IDP")
    await create_crew_access_token(fake.url, BEARER, TEAM, token_type="EXTERNAL_IDP")
    for request in fake.requests:
        headers = {k.lower(): v for k, v in request["headers"].items()}
        assert headers["tokentype"] == "EXTERNAL_IDP"
        assert headers["authorization"] == f"Bearer {BEARER}"
        assert headers["content-type"] == "application/x-amz-json-1.0"


@pytest.mark.parametrize(
    "crew_url",
    [
        f"https://{TEAM}.evil.example.test",
        f"https://{TEAM}.{DOMAIN}.evil.example.test",
        f"https://{TEAM}.sub.{DOMAIN}",
        f"https://{TEAM}.{DOMAIN}:443",
        f"https://{TEAM}.{DOMAIN}:8443/",
    ],
)
async def test_a_crew_outside_the_relay_domain_or_on_a_port_is_left_out(
    fake: FakeCps, crew_url: str
) -> None:
    body = page(1)
    body["crewEndpoints"].insert(0, dict(page(0)["crewEndpoints"][0], crewUrl=crew_url))
    fake.answer(200, body)
    crews = await list_crew_endpoints(fake.url, BEARER, crew_domain=DOMAIN)
    assert [c.endpoint_id for c in crews] == [PERSONAL]


@pytest.mark.parametrize(
    "crew_url", [f"https://{TEAM}.{DOMAIN}", f"HTTPS://{TEAM.upper()}.CREW.Example.Invalid/"]
)
async def test_a_crew_exactly_under_the_relay_domain_is_kept(fake: FakeCps, crew_url: str) -> None:
    body = page(1)
    body["crewEndpoints"].insert(0, dict(page(0)["crewEndpoints"][0], crewUrl=crew_url))
    fake.answer(200, body)
    crews = await list_crew_endpoints(fake.url, BEARER, crew_domain=DOMAIN)
    assert [c.endpoint_id for c in crews] == [TEAM, PERSONAL]


@pytest.mark.parametrize(
    "crew_url", [f"https://{TEAM}.evil.example.test", f"https://{TEAM}.{DOMAIN}:8443/"]
)
async def test_with_no_relay_domain_the_endpoint_prefix_check_still_applies(
    fake: FakeCps, crew_url: str
) -> None:
    body = page(1)
    other = dict(page(0)["crewEndpoints"][0], crewUrl=crew_url)
    wrong = dict(other, endpointId="otherccccccccccccccccccccccccccc")
    body["crewEndpoints"][0:0] = [other, wrong]
    fake.answer(200, body)
    crews = await list_crew_endpoints(fake.url, BEARER)
    assert [c.endpoint_id for c in crews] == [TEAM, PERSONAL]


@pytest.mark.parametrize(
    "domain",
    [
        "https://crew.example.invalid",
        "crew.example.invalid:443",
        "crew.example.invalid/",
        "crewý.example",
    ],
)
async def test_a_set_value_that_is_not_a_domain_lists_nothing_and_says_why(
    fake: FakeCps, caplog: pytest.LogCaptureFixture, domain: str
) -> None:
    body = page(1)
    body["crewEndpoints"].insert(0, page(0)["crewEndpoints"][0])
    fake.answer(200, body)
    with caplog.at_level(logging.WARNING, logger="kiro_crew"):
        assert await list_crew_endpoints(fake.url, BEARER, crew_domain=domain) == ()
    warnings = [r.getMessage() for r in caplog.records if "unusable Crew list" in r.getMessage()]
    assert warnings and "tunnel.relay_viewer_relay_domain is set" in warnings[0]
    assert domain not in caplog.text and "example" not in warnings[0]


async def test_with_no_domain_the_dropped_row_warning_names_no_setting(
    fake: FakeCps, caplog: pytest.LogCaptureFixture
) -> None:
    body = page(1)
    body["crewEndpoints"].insert(0, LIST["unusableEntries"][0]["entry"])
    fake.answer(200, body)
    with caplog.at_level(logging.WARNING, logger="kiro_crew"):
        await list_crew_endpoints(fake.url, BEARER)
    assert "unusable Crew list" in caplog.text
    assert "relay_viewer_relay_domain" not in caplog.text
