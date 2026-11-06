"""The lease client against a fake lease service answering from the protocol v1 fixtures.

``RenewCrewLease`` requests are checked against ``backend/renew-crew-lease.request``,
answers come from ``backend/renew-crew-lease.response`` and refusals from
``backend/renew-crew-lease.refusals``. Time is a hand-stepped monotonic clock, so a
renewal happens only when a test lets the wait before it run out.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import stat
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer
from relay_client_helpers import (
    OWNER,
    FakeCrew,
    FakeRelay,
    RecordingSleep,
    TopRng,
    fixture,
    make_client,
    ready_frame,
    stop,
    valid_message,
)

from kiro_crew.platform_compat import IS_POSIX
from kiro_crew.relay_client.lease import (
    AWS_JSON_CONTENT_TYPE,
    DEFAULT_DISPLAY_NAME,
    INSTANCE_ID_FILE,
    RENEW_TARGET,
    RETRY_AFTER_MAX_SECONDS,
    TAKEOVER_WAIT_JITTER,
    TAKEOVER_WAIT_MAX_SECONDS,
    LeaseHostTokenSource,
    credential_reference_problem,
    crew_name,
    dial_url,
    error_type,
    lease_url_problem,
    parse_time,
    read_lease_credential,
    refusal_of,
    takeover_wait,
)
from kiro_crew.relay_client.provider import (
    RelaySettings,
    RelaySettingsError,
    public_tunnel_provider,
)
from kiro_crew.relay_client.token_source import (
    HostCredential,
    HostTokenRetryLater,
    HostTokenUnavailable,
    StaticHostTokenSource,
    UnavailableHostTokenSource,
)
from kiro_crew.testing.wait import async_wait_until, await_until

pytestmark = pytest.mark.asyncio

CREDENTIAL = "lease-test-credential-7f3a"
ROTATED = "lease-test-credential-rotated-91c2"


def answer(index: int, **overrides: Any) -> Dict[str, Any]:
    body = dict(fixture("backend/renew-crew-lease.response")["examples"][index]["body"])
    body.update(overrides)
    return body


def refusal(
    exception: str, reason: Optional[str] = None
) -> Tuple[int, Dict[str, Any], Dict[str, str]]:
    """The fixture's example refusal of *exception* (with *reason*): status, body, headers."""
    for entry in fixture("backend/renew-crew-lease.refusals")["refusals"]:
        example = entry.get("example")
        if entry.get("exception") != exception or not example:
            continue
        if reason is None or example.get("reason") == reason:
            return entry["httpStatus"], example, dict(entry.get("exampleHeaders") or {})
    raise KeyError((exception, reason))


def dialed(url: str) -> str:
    """The wss:// URL the client dials for a lease answer's https:// ``hostUrl``."""
    assert url.startswith("https://")
    return "wss://" + url[len("https://") :]


def make_token(*, iat: int = 1793638800, exp: int = 1793639100, sub: str = OWNER) -> str:
    def part(value: Dict[str, Any]) -> str:
        raw = json.dumps(value).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    claims = {"kind": "personal", "sub": sub, "iat": iat, "exp": exp}
    return f"{part({'alg': 'ES256'})}.{part(claims)}.c2ln"


class StepClock:
    """A monotonic clock, and a sleep that waits until the test lets it run out."""

    def __init__(self) -> None:
        self.now = 1000.0
        self._sleeps: asyncio.Queue[Tuple[float, asyncio.Future[None]]] = asyncio.Queue()
        self._pending: Optional[Tuple[float, asyncio.Future[None]]] = None

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        await self._sleeps.put((delay, future))
        await future

    async def next_sleep(self) -> float:
        """The wait the renewal timer is in now, skipping any it abandoned."""
        while True:
            delay, future = await await_until(
                asyncio.ensure_future(self._sleeps.get()), what="a renewal wait"
            )
            for _ in range(20):
                await asyncio.sleep(0)
            if future.cancelled() or not self._sleeps.empty():
                continue
            self._pending = (delay, future)
            return delay

    def elapse(self) -> None:
        """Let the pending wait run out; the clock moves on by its length."""
        assert self._pending is not None
        delay, future = self._pending
        self._pending = None
        self.now += delay
        future.set_result(None)


class FakeLeaseService:
    """The bearer service's awsJson 1.0 ``POST /`` on loopback, answering what the test queued.

    A call that names another operation, or is not ``application/x-amz-json-1.0``,
    is refused as an unknown operation, the way the real service does.
    """

    def __init__(self, credentials: Optional[set] = None) -> None:
        self.credentials = set(credentials or {CREDENTIAL})
        self.answers: List[Tuple[int, Dict[str, Any], Dict[str, str]]] = []
        self.requests: List[Dict[str, Any]] = []
        self.on_refused_credential: Any = None
        self._server: Optional[TestServer] = None

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.port}"

    def queue(
        self, status: int, body: Dict[str, Any], headers: Optional[Dict[str, str]] = None
    ) -> None:
        self.answers.append((status, body, dict(headers or {})))

    async def __aenter__(self) -> "FakeLeaseService":
        app = web.Application()
        app.router.add_post("/", self._renew)
        self._server = TestServer(app, host="127.0.0.1", port=0)
        await self._server.start_server()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._server is not None:
            await self._server.close()

    async def _renew(self, request: web.Request) -> web.Response:
        auth = request.headers.get("Authorization", "")
        self.requests.append(
            {
                "auth": auth,
                "target": request.headers.get("X-Amz-Target"),
                "content_type": request.headers.get("Content-Type"),
                "token_type": request.headers.get("TokenType"),
                "body": json.loads(await request.read()),
            }
        )
        if (
            request.headers.get("X-Amz-Target") != RENEW_TARGET
            or request.headers.get("Content-Type") != AWS_JSON_CONTENT_TYPE
        ):
            return web.json_response({"__type": "UnknownOperationException"}, status=400)
        if auth.removeprefix("Bearer ") not in self.credentials:
            if self.on_refused_credential is not None:
                self.on_refused_credential()
            return web.json_response({"message": "Unauthorized"}, status=401)
        status, body, headers = self.answers.pop(0) if self.answers else (500, {}, {})
        return web.json_response(
            body, status=status, headers=headers, content_type=AWS_JSON_CONTENT_TYPE
        )


@pytest_asyncio.fixture
async def lease():
    async with FakeLeaseService() as service:
        yield service


@pytest.fixture
def clock() -> StepClock:
    return StepClock()


def credential_file(tmp_path: Path, value: str = CREDENTIAL) -> Path:
    path = tmp_path / "lease-credential"
    path.write_text(value + "\n", encoding="ascii")
    return path


def make_source(
    service: FakeLeaseService, tmp_path: Path, clock: StepClock, **kwargs: Any
) -> LeaseHostTokenSource:
    return LeaseHostTokenSource(
        lease_url=service.url,
        credential=str(kwargs.pop("credential", credential_file(tmp_path))),
        crew_version="0.9.1.0",
        home=tmp_path,
        display_name=kwargs.pop("display_name", "Work laptop"),
        rng=TopRng(),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        **kwargs,
    )


async def calls(service: FakeLeaseService, count: int) -> None:
    await async_wait_until(
        lambda: len(service.requests) >= count, describe=lambda: len(service.requests)
    )


# ── the request and the schedule ──


async def test_the_first_call_carries_the_credential_and_asks_for_a_host_token(
    lease: FakeLeaseService, tmp_path: Path, clock: StepClock
) -> None:
    lease.queue(200, answer(0))
    source = make_source(lease, tmp_path, clock)
    try:
        credential = await source.current()
    finally:
        await source.aclose()

    assert credential.token == answer(0)["hostToken"]
    # The model's https:// hostUrl is dialed as wss:// on the same host and path.
    assert credential.host_url == dialed(answer(0)["hostUrl"])
    assert credential.crew_url == answer(0)["crewUrl"]
    (request,) = lease.requests
    assert request["auth"] == f"Bearer {CREDENTIAL}"
    assert request["target"] == "KiroControlPlaneBearerService.RenewCrewLease"
    assert request["content_type"] == "application/x-amz-json-1.0"
    # A credential whose kind the client cannot tell sends no TokenType.
    assert request["token_type"] is None
    body = request["body"]
    expected = fixture("backend/renew-crew-lease.request")["examples"][0]["body"]
    assert set(body) == set(expected)
    assert body["relay"] == {"connectionStatus": "NOT_CONNECTED"}
    # No leaseVersion on the first call after a start: it asks for a host token.
    assert "leaseVersion" not in body
    assert body["connectedCount"] == 0
    assert (body["protocolVersion"], body["hostKind"]) == ("1", "CREW")
    assert (body["name"], body["crewVersion"]) == ("Work laptop", "0.9.1.0")
    # The instance UUID is made once, kept for the owner only, and reused.
    kept = tmp_path / INSTANCE_ID_FILE
    assert body["crewInstanceId"] == str(uuid.UUID(kept.read_text().strip()))
    if IS_POSIX:
        assert stat.S_IMODE(kept.stat().st_mode) == 0o600
    lease.queue(200, answer(0))
    again = make_source(lease, tmp_path, StepClock())
    try:
        await again.current()
    finally:
        await again.aclose()
    assert lease.requests[1]["body"]["crewInstanceId"] == body["crewInstanceId"]


async def test_it_renews_on_the_answers_schedule_and_hands_over_each_new_token(
    lease: FakeLeaseService, tmp_path: Path, clock: StepClock
) -> None:
    lease.queue(200, answer(0))
    lease.queue(200, answer(1))
    lease.queue(200, answer(2))
    source = make_source(lease, tmp_path, clock)
    handed: List[HostCredential] = []

    async def on_new(credential: HostCredential) -> None:
        handed.append(credential)

    source.on_new_token(on_new)
    try:
        first = await source.current()
        # Timed from the answer, never from comparing its times with this clock.
        assert await clock.next_sleep() == answer(0)["renewInSeconds"]
        clock.elapse()
        await calls(lease, 2)
        second = lease.requests[1]["body"]
        assert second["relay"] == {
            "connectionStatus": "NOT_CONNECTED",
            "endpointId": answer(0)["endpointId"],
        }
        # Each renewal sends back the leaseVersion of the answer before it.
        assert second["leaseVersion"] == answer(0)["leaseVersion"]
        assert await clock.next_sleep() == answer(1)["renewInSeconds"]
        assert handed == []
        assert await source.current() is first

        clock.elapse()
        await calls(lease, 3)
        assert lease.requests[2]["body"]["leaseVersion"] == answer(1)["leaseVersion"]
        assert await clock.next_sleep() == answer(2)["renewInSeconds"]
        assert [c.token for c in handed] == [answer(2)["hostToken"]]
        assert (await source.current()).token == answer(2)["hostToken"]
    finally:
        await source.aclose()


async def test_relay_connected_is_what_the_client_reports_at_each_call(
    lease: FakeLeaseService, tmp_path: Path, clock: StepClock
) -> None:
    lease.queue(200, answer(0))
    lease.queue(200, answer(1))
    source = make_source(lease, tmp_path, clock)
    up = {"now": False}
    source.track_relay(lambda: up["now"])
    try:
        await source.current()
        await clock.next_sleep()
        up["now"] = True
        clock.elapse()
        await calls(lease, 2)
    finally:
        await source.aclose()
    assert [r["body"]["relay"]["connectionStatus"] for r in lease.requests] == [
        "NOT_CONNECTED",
        "CONNECTED",
    ]


async def test_the_client_counts_as_connected_only_from_ready_until_the_connection_ends() -> None:
    # Contract section 4.2: relay.connected is true only from ready until that
    # connection ends, never from the upgrade or the hello alone.
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(relay, crew)
        await client.start()
        try:
            leg = await relay.next_leg()
            assert client.relay_connected() is False  # upgraded, no hello yet
            await leg.recv_type("hello")
            assert client.relay_connected() is False  # hello sent, no ready yet
            await leg.send(ready_frame())
            await async_wait_until(client.relay_connected)
            await leg.close(1011)
            await async_wait_until(lambda: not client.relay_connected())
        finally:
            await stop(client, ingress)


# ── never a spent token ──


async def test_a_token_with_no_lifetime_left_is_never_handed_over(
    lease: FakeLeaseService, tmp_path: Path, clock: StepClock
) -> None:
    lease.queue(200, answer(0, hostToken=make_token(iat=1793638800, exp=1793638800)))
    lease.queue(200, answer(0, hostToken=make_token(iat=1793638800, exp=1793638802)))
    source = make_source(lease, tmp_path, clock)
    try:
        for _ in range(2):
            with pytest.raises(HostTokenRetryLater):
                await source.current()
    finally:
        await source.aclose()
    # Still no usable token, so the second call asks again by sending no leaseVersion.
    assert [r["body"].get("leaseVersion") for r in lease.requests] == [None, None]


async def test_an_aged_out_token_is_renewed_instead_of_returned(
    lease: FakeLeaseService, tmp_path: Path, clock: StepClock
) -> None:
    lease.queue(200, answer(0))
    fresh = make_token(iat=1793639000, exp=1793639300)
    lease.queue(200, answer(0, hostToken=fresh))
    source = make_source(lease, tmp_path, clock)
    try:
        await source.current()
        # The token's own lifetime (exp - iat = 300 s), counted from its arrival.
        clock.now += 300
        assert (await source.current()).token == fresh
    finally:
        await source.aclose()
    assert "leaseVersion" not in lease.requests[1]["body"]


async def test_a_client_that_lost_its_token_gets_a_new_one_or_none(
    lease: FakeLeaseService, tmp_path: Path, clock: StepClock
) -> None:
    lease.queue(200, answer(0))
    lease.queue(200, answer(1))
    source = make_source(lease, tmp_path, clock)
    try:
        await source.current()
        # The relay refused the held token: an answer with none is not the old one.
        with pytest.raises(HostTokenRetryLater):
            await source.renew(host_token_requested=True)
    finally:
        await source.aclose()
    assert "leaseVersion" not in lease.requests[1]["body"]


async def test_a_4401_close_leaves_out_the_lease_version_then_the_next_renewal_sends_it(
    lease: FakeLeaseService, tmp_path: Path, clock: StepClock
) -> None:
    fresh = make_token(iat=1793638900, exp=1793639200)
    lease.queue(200, answer(0))
    lease.queue(200, answer(2, hostToken=fresh, leaseVersion=9))
    lease.queue(200, answer(1, leaseVersion=10))
    source = make_source(lease, tmp_path, clock)
    try:
        await source.current()
        await clock.next_sleep()
        # The relay closed the host leg with 4401: the client asks for a new token.
        assert (await source.renew(host_token_requested=True)).token == fresh
        assert "leaseVersion" not in lease.requests[1]["body"]
        assert await clock.next_sleep() == answer(2)["renewInSeconds"]
        clock.elapse()
        await calls(lease, 3)
        assert lease.requests[2]["body"]["leaseVersion"] == 9
    finally:
        await source.aclose()
    # A restart of Remote Control forgets the version, so its first call has none.
    lease.queue(200, answer(0))
    try:
        await source.current()
    finally:
        await source.aclose()
    assert "leaseVersion" not in lease.requests[3]["body"]


# ── failures and refusals ──


async def test_failed_calls_back_off_on_the_reconnect_steps_then_reset(
    lease: FakeLeaseService, tmp_path: Path, clock: StepClock
) -> None:
    lease.queue(503, {})
    lease.queue(502, {})
    lease.queue(200, answer(0))
    source = make_source(lease, tmp_path, clock)
    handed: List[HostCredential] = []

    async def on_new(credential: HostCredential) -> None:
        handed.append(credential)

    source.on_new_token(on_new)
    try:
        with pytest.raises(HostTokenRetryLater):
            await source.current()
        # A client asking again inside the wait gets no extra call.
        with pytest.raises(HostTokenRetryLater):
            await source.current()
        assert len(lease.requests) == 1
        assert await clock.next_sleep() == 2.0
        clock.elapse()
        await calls(lease, 2)
        assert await clock.next_sleep() == 5.0
        clock.elapse()
        await calls(lease, 3)
        assert await clock.next_sleep() == answer(0)["renewInSeconds"]
        assert [c.token for c in handed] == [answer(0)["hostToken"]]
    finally:
        await source.aclose()


async def test_a_service_it_cannot_reach_is_retried_later(tmp_path: Path, clock: StepClock) -> None:
    async with FakeLeaseService() as gone:
        url = gone.url
    source = LeaseHostTokenSource(
        lease_url=url,
        credential=str(credential_file(tmp_path)),
        crew_version="0.9.1.0",
        home=tmp_path,
        rng=TopRng(),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    try:
        with pytest.raises(HostTokenRetryLater, match="could not be reached"):
            await source.current()
        assert await clock.next_sleep() == 2.0
    finally:
        await source.aclose()


@pytest.mark.parametrize(
    "exception,reason",
    [
        ("ResourceNotFoundException", None),
        ("ValidationException", None),
    ],
)
async def test_a_definitive_refusal_stops_renewals(
    lease: FakeLeaseService,
    tmp_path: Path,
    clock: StepClock,
    exception: str,
    reason: Optional[str],
) -> None:
    lease.queue(*refusal(exception, reason))
    source = make_source(lease, tmp_path, clock)
    try:
        with pytest.raises(HostTokenUnavailable, match=exception):
            await source.current()
        with pytest.raises(HostTokenUnavailable, match=exception):
            await source.renew(host_token_requested=True)
        for _ in range(20):
            await asyncio.sleep(0)
    finally:
        await source.aclose()
    assert len(lease.requests) == 1


@pytest.mark.parametrize(
    "reason,why",
    [
        ("RemoteAccessDisabled", "does not allow remote access"),
        ("CrewAccessNotGranted", "has no access to the Crew"),
        (None, "cannot host this Crew"),
    ],
)
async def test_an_access_refusal_tries_again_on_its_own_after_15_minutes(
    lease: FakeLeaseService,
    tmp_path: Path,
    clock: StepClock,
    reason: Optional[str],
    why: str,
) -> None:
    # The refusals fixture: try again at most every 15 minutes, never stop for good.
    if reason is None:
        lease.queue(403, {"__type": "AccessDeniedException", "message": "Access denied."})
    else:
        lease.queue(*refusal("AccessDeniedException", reason))
    lease.queue(200, answer(0))
    source = make_source(lease, tmp_path, clock)
    try:
        with pytest.raises(HostTokenRetryLater, match=why):
            await source.current()
        # Asking again inside the wait makes no call.
        with pytest.raises(HostTokenRetryLater, match=why):
            await source.renew(host_token_requested=True)
        assert len(lease.requests) == 1
        assert await clock.next_sleep() == RETRY_AFTER_MAX_SECONDS == 900.0
        clock.elapse()
        await calls(lease, 2)
        assert (await source.current()).token == answer(0)["hostToken"]
    finally:
        await source.aclose()


async def test_identity_in_use_waits_until_takeover_after_by_the_answers_own_date(
    lease: FakeLeaseService, tmp_path: Path, clock: StepClock
) -> None:
    status, body, headers = refusal("IdentityInUseException")
    lease.queue(status, body, headers)
    lease.queue(200, answer(0))
    source = make_source(lease, tmp_path, clock)
    try:
        with pytest.raises(HostTokenRetryLater, match="IdentityInUseException"):
            await source.current()
        with pytest.raises(HostTokenRetryLater):
            await source.current()
        assert len(lease.requests) == 1
        wait = await clock.next_sleep()
        # takeoverAfter 1793638864 minus Date 17:00:00 GMT (1793638800): 64 s, plus
        # a jitter that only ever lengthens it.
        assert 64 <= wait <= 64 * (1 + TAKEOVER_WAIT_JITTER)
        clock.elapse()
        await calls(lease, 2)
    finally:
        await source.aclose()


async def test_identity_in_use_with_no_usable_date_waits_the_longest_wait(
    lease: FakeLeaseService, tmp_path: Path, clock: StepClock
) -> None:
    status, body, _ = refusal("IdentityInUseException")
    lease.queue(status, body, {"Date": "not a date"})
    source = make_source(lease, tmp_path, clock)
    try:
        with pytest.raises(HostTokenRetryLater):
            await source.current()
        # The whole takeover time, and the jitter never takes it past that.
        assert await clock.next_sleep() == TAKEOVER_WAIT_MAX_SECONDS == 120.0
    finally:
        await source.aclose()


class _NoClock:
    """Stands in for the lease module's ``time``: reading a clock fails the test."""

    def __init__(self, real: Any) -> None:
        self._real = real

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def time(self) -> float:
        raise AssertionError("the takeover wait read this machine's clock")

    def monotonic(self) -> float:
        raise AssertionError("the takeover wait read this machine's clock")


async def test_the_takeover_wait_never_reads_this_machines_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.relay_client import lease as lease_module

    # Only the lease module's own binding is replaced, never the stdlib clock.
    monkeypatch.setattr(lease_module, "time", _NoClock(lease_module.time))
    date = "Mon, 02 Nov 2026 17:00:00 GMT"
    assert takeover_wait({"takeoverAfter": 1793638864}, date) == 64.0
    assert takeover_wait({"takeoverAfter": "2026-11-02T17:01:04Z"}, date) == 64.0
    # Clamped to 1 s (no tight loop) up to the 120 s takeover time.
    assert takeover_wait({"takeoverAfter": 1793638700}, date) == 1.0
    assert takeover_wait({"takeoverAfter": 1793638800 + 5000}, date) == 120.0
    # Jitter is applied before the clamp, so it never passes the takeover time.
    assert takeover_wait({"takeoverAfter": 1793638800 + 110}, date, 1.2) == 120.0
    assert takeover_wait({"takeoverAfter": 1793638864}, date, 1.1) == pytest.approx(70.4)
    # No Date, an unparseable one, or no takeoverAfter: the whole takeover time.
    assert takeover_wait({"takeoverAfter": 1793638864}, None) == TAKEOVER_WAIT_MAX_SECONDS
    assert takeover_wait({"takeoverAfter": 1793638864}, "garbage") == TAKEOVER_WAIT_MAX_SECONDS
    assert takeover_wait({}, date) == TAKEOVER_WAIT_MAX_SECONDS


@pytest.mark.parametrize(
    "body",
    [
        {"__type": "UnauthorizedException", "message": "Unauthorized"},
        {"__type": "AccessDeniedException", "reason": "RemoteAccessDisabled"},
        {},
    ],
    ids=["unauthorized", "named-access-denied", "no-body"],
)
async def test_any_http_401_is_a_sign_in_failure_whatever_its_body(
    lease: FakeLeaseService, tmp_path: Path, clock: StepClock, body: Dict[str, Any]
) -> None:
    # The credential is reread and the call retried once, then it recovers.
    path = credential_file(tmp_path, CREDENTIAL)
    lease.queue(401, body)
    lease.queue(200, answer(0))
    source = make_source(lease, tmp_path, clock, credential=path)
    reads: List[str] = []
    original = source._read_credential

    def counting(ref: str) -> str:
        reads.append(ref)
        return original(ref)

    source._read_credential = counting  # type: ignore[method-assign]
    try:
        assert (await source.current()).token == answer(0)["hostToken"]
    finally:
        await source.aclose()
    assert len(lease.requests) == 2
    assert len(reads) == 2


async def test_a_second_401_with_a_name_stops_until_restart(
    lease: FakeLeaseService, tmp_path: Path, clock: StepClock
) -> None:
    lease.queue(401, {"__type": "UnauthorizedException"})
    lease.queue(401, {"__type": "UnauthorizedException"})
    source = make_source(lease, tmp_path, clock)
    try:
        with pytest.raises(HostTokenUnavailable, match="HTTP 401"):
            await source.current()
    finally:
        await source.aclose()
    assert len(lease.requests) == 2


async def test_a_403_with_no_exception_name_backs_off(
    lease: FakeLeaseService, tmp_path: Path, clock: StepClock
) -> None:
    # Apart from a 401, status alone never decides: a 403 with no name is not an
    # access refusal.
    lease.queue(403, {"message": "Forbidden"})
    source = make_source(lease, tmp_path, clock)
    try:
        with pytest.raises(HostTokenRetryLater):
            await source.current()
        assert await clock.next_sleep() == 2.0
    finally:
        await source.aclose()
    assert len(lease.requests) == 1


async def test_a_validation_refusal_with_an_endpoint_renews_once_without_it_then_stops(
    lease: FakeLeaseService, tmp_path: Path, clock: StepClock
) -> None:
    lease.queue(200, answer(0))
    lease.queue(*refusal("ValidationException"))
    lease.queue(200, answer(1))
    lease.queue(*refusal("ValidationException"))
    lease.queue(*refusal("ValidationException"))
    source = make_source(lease, tmp_path, clock)
    try:
        await source.current()
        await clock.next_sleep()
        clock.elapse()
        await calls(lease, 3)
        relays = [r["body"]["relay"] for r in lease.requests]
        assert "endpointId" in relays[1]
        assert "endpointId" not in relays[2]
        # A refusal of the retry without the endpoint is not retried again.
        await clock.next_sleep()
        clock.elapse()
        await calls(lease, 5)
        await async_wait_until(lambda: bool(source.stopped_reason))
        assert "ValidationException" in source.stopped_reason
    finally:
        await source.aclose()
    assert len(lease.requests) == 5


async def test_an_expired_sign_in_rereads_the_credential_and_recovers_on_the_one_retry(
    lease: FakeLeaseService, tmp_path: Path, clock: StepClock
) -> None:
    path = credential_file(tmp_path, CREDENTIAL)
    lease.credentials = {ROTATED}
    lease.on_refused_credential = lambda: path.write_text(ROTATED + "\n", encoding="ascii")
    lease.queue(200, answer(0))
    source = make_source(lease, tmp_path, clock, credential=path)
    try:
        assert (await source.current()).token == answer(0)["hostToken"]
    finally:
        await source.aclose()
    assert [r["auth"] for r in lease.requests] == [f"Bearer {CREDENTIAL}", f"Bearer {ROTATED}"]


async def test_a_second_expired_sign_in_stops_until_restart(
    lease: FakeLeaseService, tmp_path: Path, clock: StepClock
) -> None:
    lease.credentials = {ROTATED}
    source = make_source(lease, tmp_path, clock)
    try:
        with pytest.raises(HostTokenUnavailable, match="HTTP 401"):
            await source.current()
        with pytest.raises(HostTokenUnavailable):
            await source.current()
        assert len(lease.requests) == 2
        # A restart of Remote Control starts again.
        await source.aclose()
        lease.credentials = {CREDENTIAL}
        lease.queue(200, answer(0))
        assert (await source.current()).token == answer(0)["hostToken"]
    finally:
        await source.aclose()


@pytest.mark.parametrize(
    "status,body",
    [
        refusal("ThrottlingException")[:2],
        refusal("InternalServerException")[:2],
        (400, {"__type": "NotInTheModelException"}),
        (503, {}),
        (200, {"x": 1}),
        (200, {k: v for k, v in answer(1).items() if k != "leaseVersion"}),
        (200, answer(1, hostUrl="ftp://host-us-east-1.relay.example/v1/host")),
        (200, answer(1, leaseExpiresAt="tomorrow")),
    ],
    ids=[
        "throttling",
        "internal",
        "unknown-type",
        "no-body",
        "not-an-answer",
        "no-lease-version",
        "host-url-scheme",
        "lease-expires-at",
    ],
)
async def test_an_unlisted_refusal_or_an_unusable_answer_backs_off(
    lease: FakeLeaseService,
    tmp_path: Path,
    clock: StepClock,
    status: int,
    body: Dict[str, Any],
) -> None:
    lease.queue(status, body)
    source = make_source(lease, tmp_path, clock)
    try:
        with pytest.raises(HostTokenRetryLater):
            await source.current()
        assert await clock.next_sleep() == 2.0
    finally:
        await source.aclose()


# ── the credential ──


async def test_the_credential_and_the_tokens_stay_out_of_the_logs(
    lease: FakeLeaseService,
    tmp_path: Path,
    clock: StepClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    lease.queue(503, {})
    lease.queue(200, answer(0))
    lease.queue(*refusal("ThrottlingException"))
    source = make_source(lease, tmp_path, clock)
    try:
        with pytest.raises(HostTokenRetryLater):
            await source.current()
        await clock.next_sleep()
        clock.elapse()
        await calls(lease, 2)
        await clock.next_sleep()
        clock.elapse()
        await calls(lease, 3)
        await clock.next_sleep()
        text = caplog.text + repr(source)
    finally:
        await source.aclose()
    settings = RelaySettings.from_config(_cfg(lease_credential="secret://lease"))
    assert "secret://lease" not in repr(settings)
    assert CREDENTIAL not in text
    assert answer(0)["hostToken"] not in text
    assert "lease renewed" in text


async def test_the_credential_setting_names_a_place_never_the_credential(tmp_path: Path) -> None:
    assert credential_reference_problem("secret://relay-lease") is None
    assert credential_reference_problem(str(tmp_path / "cred")) is None
    assert credential_reference_problem("~/.lease-credential") is None
    for bad in ("", CREDENTIAL, "relative/path", "secret://"):
        assert credential_reference_problem(bad) is not None, bad
    assert CREDENTIAL not in str(credential_reference_problem(CREDENTIAL))


async def test_the_credential_is_read_from_a_file_or_the_vault(tmp_path: Path) -> None:
    from kiro_crew.secrets import SecretVault

    assert read_lease_credential(str(credential_file(tmp_path)), home=tmp_path) == CREDENTIAL
    SecretVault(tmp_path).set_sync("relay-lease", ROTATED)
    assert read_lease_credential("secret://relay-lease", home=tmp_path) == ROTATED
    (tmp_path / "empty").write_text("\n")
    with pytest.raises(Exception, match="printable ASCII"):
        read_lease_credential(str(tmp_path / "empty"), home=tmp_path)
    with pytest.raises(Exception, match="no secret"):
        read_lease_credential("secret://absent", home=tmp_path)


@pytest.mark.parametrize(
    "encoded",
    [
        b"\xef\xbb\xbf" + CREDENTIAL.encode("ascii") + b"\r\n",
        (CREDENTIAL + "\r\n").encode("utf-16"),
        b"\xfe\xff" + (CREDENTIAL + "\r\n").encode("utf-16-be"),
    ],
    ids=["utf-8-bom", "utf-16-le-bom", "utf-16-be-bom"],
)
async def test_a_credential_file_a_windows_editor_saved_is_read(
    tmp_path: Path, encoded: bytes
) -> None:
    # Notepad's UTF-8 with a mark, and PowerShell's `echo x > file` (UTF-16).
    path = tmp_path / "lease-credential"
    path.write_bytes(encoded)
    assert read_lease_credential(str(path), home=tmp_path) == CREDENTIAL


async def test_a_credential_that_is_not_ascii_after_its_mark_is_still_refused(
    tmp_path: Path,
) -> None:
    path = tmp_path / "lease-credential"
    path.write_bytes("caf\u00e9".encode("utf-16"))
    with pytest.raises(Exception, match="not ASCII"):
        read_lease_credential(str(path), home=tmp_path)


async def test_a_credential_file_missing_for_a_moment_is_read_again_later(
    lease: FakeLeaseService, tmp_path: Path, clock: StepClock
) -> None:
    # A rotation can leave the file missing or half written for a moment: that
    # backs off and reads again, rather than stopping until a restart.
    path = tmp_path / "rotated-credential"
    lease.queue(200, answer(0))
    source = make_source(lease, tmp_path, clock, credential=path)
    try:
        with pytest.raises(HostTokenRetryLater, match="tunnel.relay_lease_credential"):
            await source.current()
        assert lease.requests == []
        assert await clock.next_sleep() == 2.0
        path.write_text(CREDENTIAL + "\n", encoding="ascii")
        clock.elapse()
        await calls(lease, 1)
        assert (await source.current()).token == answer(0)["hostToken"]
    finally:
        await source.aclose()


async def test_a_setting_that_names_no_credential_stops_and_says_which_setting(
    lease: FakeLeaseService, tmp_path: Path, clock: StepClock
) -> None:
    source = make_source(lease, tmp_path, clock, credential="relative/path")
    try:
        with pytest.raises(HostTokenUnavailable, match="tunnel.relay_lease_credential"):
            await source.current()
        assert lease.requests == []
    finally:
        await source.aclose()


# ── settings and selection ──


async def test_the_lease_url_is_https_or_a_loopback_port_forward() -> None:
    assert lease_url_problem("https://lease.example.com") is None
    assert lease_url_problem("http://127.0.0.1:8790") is None
    assert lease_url_problem("http://[::1]:8790/") is None
    for bad in (
        "http://lease.example.com",
        "https://user:pw@lease.example.com",
        "https://lease.example.com/?x=1",
        "ftp://127.0.0.1",
        "https://",
    ):
        assert lease_url_problem(bad) is not None, bad


def _cfg(**tunnel: Any) -> SimpleNamespace:
    values = {
        "relay_enabled": True,
        "relay_host_url": "",
        "relay_crew_domain": "",
        "relay_move_grace_seconds": 1.0,
        "relay_lease_url": "",
        "relay_lease_credential": "",
    }
    values.update({f"relay_{k}": v for k, v in tunnel.items()})
    return SimpleNamespace(tunnel=SimpleNamespace(**values))


async def test_the_lease_client_is_the_source_only_when_a_lease_service_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("kiro_crew.edition_locks.local_enable_locked", lambda _section: False)
    assert isinstance(public_tunnel_provider(_cfg())._tokens, UnavailableHostTokenSource)
    provider = public_tunnel_provider(
        _cfg(lease_url="http://127.0.0.1:8790", lease_credential="/run/lease-credential")
    )
    assert isinstance(provider._tokens, LeaseHostTokenSource)


async def test_lease_settings_the_client_must_not_use_are_refused_without_echo() -> None:
    def settings(**tunnel: Any) -> RelaySettings:
        return RelaySettings.from_config(_cfg(**tunnel))

    settings(lease_url="https://lease.example.com", lease_credential="secret://x").validate()
    # No lease service and no names: nothing names the relay, so it is refused.
    with pytest.raises(RelaySettingsError, match="tunnel.relay_host_url"):
        settings().validate()
    with pytest.raises(RelaySettingsError, match="relay_lease_url"):
        settings(lease_url="http://lease.example.com", lease_credential="secret://x").validate()
    with pytest.raises(RelaySettingsError) as refused:
        settings(lease_url="https://lease.example.com", lease_credential=CREDENTIAL).validate()
    assert CREDENTIAL not in str(refused.value)


# ── the round trip ──


async def test_a_renewal_token_goes_down_the_live_host_connection(
    lease: FakeLeaseService, tmp_path: Path, clock: StepClock
) -> None:
    first, second = answer(0)["hostToken"], answer(2)["hostToken"]
    async with FakeRelay(tokens={first, second}) as relay, FakeCrew() as crew:
        lease.queue(200, answer(0, hostUrl=relay.url))
        lease.queue(200, answer(2, hostUrl=relay.url))
        source = make_source(lease, tmp_path, clock)
        client, _, ingress = make_client(relay, crew, source=source)  # type: ignore[arg-type]
        await client.start()
        try:
            leg = await relay.next_leg()
            assert leg.token == first
            await leg.handshake()
            await async_wait_until(client.relay_connected)
            await clock.next_sleep()
            clock.elapse()
            frame = await leg.recv_type("token")
            assert frame["token"] == second
        finally:
            await stop(client, ingress)
            await source.aclose()
    connected = [r["body"]["relay"]["connectionStatus"] for r in lease.requests]
    assert connected == ["NOT_CONNECTED", "CONNECTED"]


async def test_a_static_source_still_serves_the_seam() -> None:
    source = StaticHostTokenSource(HostCredential(make_token()))
    source.track_relay(lambda: True)
    await source.aclose()
    assert (await source.current()).token == make_token()


# ── the model's shapes ──


async def test_an_https_host_url_is_dialed_as_wss_on_the_same_host_port_and_path() -> None:
    assert dial_url("https://host-us-east-1.relay.example/v1/host") == (
        "wss://host-us-east-1.relay.example/v1/host"
    )
    assert dial_url("https://relay.example.com:8443/a/b") == "wss://relay.example.com:8443/a/b"
    assert dial_url("wss://relay.example.com/v1/host") == "wss://relay.example.com/v1/host"
    # A plaintext loopback test relay, only when the lease service is on loopback too.
    local = {"local_test": True}
    assert dial_url("http://127.0.0.1:9/v1/host", **local) == "ws://127.0.0.1:9/v1/host"
    assert dial_url("ws://127.0.0.1:9/v1/host", **local) == "ws://127.0.0.1:9/v1/host"
    assert dial_url("ws://relay.example.com/v1/host", **local) is None
    assert dial_url("http://127.0.0.1:9/v1/host") is None
    assert dial_url("ws://127.0.0.1:9/v1/host") is None
    for bad in (
        "http://relay.example.com/v1/host",
        "ws://relay.example.com/v1/host",
        "ftp://relay.example.com/v1/host",
        "relay.example.com/v1/host",
        "https://",
    ):
        assert dial_url(bad) is None, bad


@pytest.mark.parametrize(
    "host_url", ["ws://127.0.0.1:9/v1/host", "http://127.0.0.1:9/v1/host", "ws://[::1]:9/v1/host"]
)
async def test_a_real_lease_service_never_points_the_host_token_at_a_plaintext_port(
    tmp_path: Path, clock: StepClock, monkeypatch: pytest.MonkeyPatch, host_url: str
) -> None:
    # The host token rides the dial as a Bearer header, so a lease service that is
    # not on loopback must never steer it to a plaintext loopback port.
    source = LeaseHostTokenSource(
        lease_url="https://lease.example.com",
        credential=str(credential_file(tmp_path)),
        crew_version="0.9.1.0",
        home=tmp_path,
        rng=TopRng(),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    posted: List[Dict[str, Any]] = []

    async def fake_post(credential: str, body: Dict[str, Any]) -> Any:
        posted.append(body)
        return SimpleNamespace(
            status=200, payload=answer(0, hostUrl=host_url), date=None, error_type=None
        )

    monkeypatch.setattr(source, "_post", fake_post)
    try:
        with pytest.raises(HostTokenRetryLater, match="hostUrl"):
            await source.current()
    finally:
        await source.aclose()
    assert len(posted) == 1


async def test_times_are_epoch_seconds_or_iso_8601_with_a_zone() -> None:
    assert parse_time(1793639100) == 1793639100.0
    assert parse_time(1793639100.5) == 1793639100.5
    assert parse_time("2026-11-02T17:05:00Z") == 1793639100.0
    assert parse_time("2026-11-02T17:05:00+00:00") == 1793639100.0
    for bad in (None, True, -1, "2026-11-02T17:05:00", "tomorrow", float("nan"), {}):
        assert parse_time(bad) is None, bad


async def test_the_iso_answer_example_is_accepted(
    lease: FakeLeaseService, tmp_path: Path, clock: StepClock
) -> None:
    lease.queue(200, answer(0))
    lease.queue(200, answer(3))
    source = make_source(lease, tmp_path, clock)
    try:
        await source.current()
        await clock.next_sleep()
        clock.elapse()
        await calls(lease, 2)
        # A usable answer: the next wait is its renewInSeconds, not a backoff step.
        assert await clock.next_sleep() == answer(3)["renewInSeconds"]
    finally:
        await source.aclose()


async def test_the_name_always_fits_the_models_crew_name() -> None:
    assert crew_name("Work laptop") == "Work laptop"
    assert crew_name("dev-dsk_01.corp") == "dev-dsk_01.corp"
    assert crew_name("_hidden") == "hidden"
    assert crew_name("matt's mac (2)") == "matts mac 2"
    assert crew_name("x" * 100) == "x" * 64
    for empty in ("", None, "日本語", "--", "   "):
        assert crew_name(empty) == DEFAULT_DISPLAY_NAME
    assert DEFAULT_DISPLAY_NAME == crew_name(DEFAULT_DISPLAY_NAME)


async def test_a_host_name_that_does_not_fit_is_cleaned_before_it_is_sent(
    lease: FakeLeaseService, tmp_path: Path, clock: StepClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket

    monkeypatch.setattr(socket, "gethostname", lambda: "Matt's MacBook (Pro).local")
    lease.queue(200, answer(0))
    source = make_source(lease, tmp_path, clock, display_name=None)
    try:
        await source.current()
    finally:
        await source.aclose()
    assert lease.requests[0]["body"]["name"] == "Matts MacBook Pro"


async def test_smithy_error_types_are_read_from_the_body_or_the_header() -> None:
    assert error_type("ValidationException") == "ValidationException"
    assert error_type("com.example.service#ValidationException") == "ValidationException"
    assert error_type("ValidationException:http://errors.example.com/x/") == ("ValidationException")
    assert error_type("not a type!") is None
    assert error_type(None) is None
    assert refusal_of({"__type": "AccessDeniedException", "reason": "RemoteAccessDisabled"}) == (
        "AccessDeniedException",
        "RemoteAccessDisabled",
    )
    assert refusal_of({}, "com.example.service#ThrottlingException") == (
        "ThrottlingException",
        None,
    )
    assert refusal_of({"__type": "AccessDeniedException", "reason": "has spaces"}) == (
        "AccessDeniedException",
        None,
    )
    assert refusal_of(None) == (None, None)


async def test_an_external_idp_credential_sends_its_token_type(
    lease: FakeLeaseService, tmp_path: Path, clock: StepClock
) -> None:
    lease.queue(200, answer(0))
    source = make_source(lease, tmp_path, clock, token_type="EXTERNAL_IDP")
    try:
        await source.current()
    finally:
        await source.aclose()
    assert lease.requests[0]["token_type"] == "EXTERNAL_IDP"
    for refused in ("API_KEY", "SSO_OIDC", "external_idp", ""):
        with pytest.raises(ValueError):
            make_source(lease, tmp_path, clock, token_type=refused)


# ── a spent host token is never dialed again ──


def _count_lease_calls(source: LeaseHostTokenSource) -> List[int]:
    """Count every call the source makes to the lease service, reached or not."""
    calls: List[int] = []
    post = source._post

    async def counted(credential: str, body: dict):
        calls.append(1)
        return await post(credential, body)

    source._post = counted  # type: ignore[method-assign]
    return calls


async def _connected_then_unreachable(lease: FakeLeaseService, relay: FakeRelay, client, clock):
    """Connect on the lease's first token, then spend it and take the lease service away."""
    leg = await relay.next_leg()
    await leg.handshake()
    await async_wait_until(client.relay_connected)
    # The token's own lifetime passes, the relay stops taking it, and the lease
    # service goes away.
    clock.now += 600
    relay.tokens.clear()
    await lease.__aexit__()
    return leg


@pytest.mark.parametrize("end", ["4403", "1006"])
async def test_a_spent_host_token_is_not_dialed_while_renewals_fail(
    lease: FakeLeaseService, tmp_path: Path, clock: StepClock, end: str
) -> None:
    first = answer(0)["hostToken"]
    async with FakeRelay(tokens={first}) as relay, FakeCrew() as crew:
        lease.queue(200, answer(0, hostUrl=relay.url))
        source = make_source(lease, tmp_path, clock)
        calls = _count_lease_calls(source)
        # The client's own history is bounded; the sleep keeps every cycle.
        sleep = RecordingSleep()
        client, _, ingress = make_client(
            relay, crew, source=source, sleep=sleep  # type: ignore[arg-type]
        )
        await client.start()
        try:
            leg = await _connected_then_unreachable(lease, relay, client, clock)
            assert len(calls) == 1
            if end == "4403":
                await leg.close(4403)
            else:
                leg.abort()
            # Many backoff cycles pass on the client's own clock while the lease
            # service's hold runs on the source's: no dial, and one lease call.
            await async_wait_until(lambda: len(sleep.delays) >= 8)
            assert relay.dials == 1 and relay.refused == []
            assert len(calls) == 2
            snapshot = client.snapshot()
            assert snapshot["state"] == "reconnecting"
            assert "lease service could not be reached" in snapshot["error"]

            # Once the hold runs out, the next cycle asks once more, and only once.
            clock.now += 3600
            await async_wait_until(lambda: len(calls) == 3)
            seen = len(sleep.delays)
            await async_wait_until(lambda: len(sleep.delays) >= seen + 8)
            assert len(calls) == 3
            assert relay.dials == 1 and relay.refused == []
        finally:
            await stop(client, ingress)
            await source.aclose()


async def test_a_move_with_no_usable_host_token_keeps_the_old_connection(
    lease: FakeLeaseService, tmp_path: Path, clock: StepClock
) -> None:
    first = answer(0)["hostToken"]
    async with FakeRelay(tokens={first}) as relay, FakeCrew() as crew:
        lease.queue(200, answer(0, hostUrl=relay.url))
        source = make_source(lease, tmp_path, clock)
        calls = _count_lease_calls(source)
        client, _, ingress = make_client(
            relay, crew, source=source, sleep=RecordingSleep()  # type: ignore[arg-type]
        )
        await client.start()
        try:
            leg = await _connected_then_unreachable(lease, relay, client, clock)
            (conn,) = list(client._live)
            await leg.send(valid_message("frames/reconnect", 0))
            # No dial with the old connection's spent token: the old one serves on.
            await async_wait_until(lambda: len(calls) == 2)
            await async_wait_until(lambda: not conn.move_requested.is_set())
            assert relay.dials == 1 and not leg.closed.is_set()
            # The relay's 1012 ends it; the loop then waits on the source, not a dial.
            await leg.close(1012)
            await async_wait_until(lambda: len(client.delays) >= 8)
            assert relay.dials == 1 and relay.refused == []
            assert len(calls) == 2
            assert "lease service could not be reached" in client.snapshot()["error"]
        finally:
            await stop(client, ingress)
            await source.aclose()


async def test_a_client_stopped_by_its_lease_gives_up_its_relay_url(
    lease: FakeLeaseService, tmp_path: Path, clock: StepClock
) -> None:
    # Connected, then a backoff close keeps the URL while the client redials. The
    # lease service then refuses the credential twice, so the source stops: the
    # stopped client must drop the URL, or links and origins keep a dead endpoint.
    first = answer(0)["hostToken"]
    async with FakeRelay(tokens={first}) as relay, FakeCrew() as crew:
        lease.queue(200, answer(0, hostUrl=relay.url))
        source = make_source(lease, tmp_path, clock)
        client, _, ingress = make_client(
            relay, crew, source=source, sleep=RecordingSleep()  # type: ignore[arg-type]
        )
        lost: List[bool] = []

        async def on_disconnect() -> None:
            lost.append(True)

        client.register_callbacks(on_connect=None, on_disconnect=on_disconnect)
        await client.start()
        try:
            leg = await relay.next_leg()
            await leg.handshake()
            await async_wait_until(lambda: client.public_url != "")
            clock.now += 600
            lease.credentials.clear()
            await leg.close(1013)
            await async_wait_until(lambda: client.state == "error")
            assert "HTTP 401" in client.snapshot()["error"]
            assert lost == [True]
            assert client.public_url == ""
        finally:
            await stop(client, ingress)
            await source.aclose()
