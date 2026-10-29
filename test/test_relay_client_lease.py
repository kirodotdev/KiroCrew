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
    TopRng,
    fixture,
    make_client,
    ready_frame,
    stop,
)

from kiro_crew.platform_compat import IS_POSIX
from kiro_crew.relay_client.lease import (
    INSTANCE_ID_FILE,
    RENEW_PATH,
    LeaseHostTokenSource,
    credential_reference_problem,
    lease_url_problem,
    read_lease_credential,
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


def refusal(code: str) -> Tuple[int, Dict[str, Any]]:
    for entry in fixture("backend/renew-crew-lease.refusals")["refusals"]:
        if entry["code"] == code:
            return entry["httpStatus"], entry["example"]
    raise KeyError(code)


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
    """``POST /RenewCrewLease`` on loopback, answering what the test queued."""

    def __init__(self, credentials: Optional[set] = None) -> None:
        self.credentials = set(credentials or {CREDENTIAL})
        self.answers: List[Tuple[int, Dict[str, Any]]] = []
        self.requests: List[Dict[str, Any]] = []
        self.on_refused_credential: Any = None
        self._server: Optional[TestServer] = None

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.port}"

    def queue(self, status: int, body: Dict[str, Any]) -> None:
        self.answers.append((status, body))

    async def __aenter__(self) -> "FakeLeaseService":
        app = web.Application()
        app.router.add_post(RENEW_PATH, self._renew)
        self._server = TestServer(app, host="127.0.0.1", port=0)
        await self._server.start_server()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._server is not None:
            await self._server.close()

    async def _renew(self, request: web.Request) -> web.Response:
        auth = request.headers.get("Authorization", "")
        self.requests.append({"auth": auth, "body": await request.json()})
        if auth.removeprefix("Bearer ") not in self.credentials:
            if self.on_refused_credential is not None:
                self.on_refused_credential()
            status, body = refusal("SIGN_IN_EXPIRED")
            return web.json_response(body, status=status)
        status, body = self.answers.pop(0) if self.answers else (500, {})
        return web.json_response(body, status=status)


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
        display_name="Work laptop",
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
    assert credential.host_url == answer(0)["hostUrl"]
    assert credential.crew_url == answer(0)["crewUrl"]
    (request,) = lease.requests
    assert request["auth"] == f"Bearer {CREDENTIAL}"
    body = request["body"]
    expected = fixture("backend/renew-crew-lease.request")["examples"][0]["body"]
    assert set(body) == set(expected)
    assert body["relay"] == {"connected": False}
    assert body["hostTokenRequested"] is True
    assert body["connectedUsers"] == []
    assert (body["protocolVersion"], body["hostKind"]) == (1, "crew")
    assert (body["displayName"], body["crewVersion"]) == ("Work laptop", "0.9.1.0")
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
        assert second["relay"] == {"connected": False, "endpointId": answer(0)["endpointId"]}
        assert "hostTokenRequested" not in second
        assert await clock.next_sleep() == answer(1)["renewInSeconds"]
        assert handed == []
        assert await source.current() is first

        clock.elapse()
        await calls(lease, 3)
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
    assert [r["body"]["relay"]["connected"] for r in lease.requests] == [False, True]


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
    assert [r["body"].get("hostTokenRequested") for r in lease.requests] == [True, True]


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
    assert lease.requests[1]["body"]["hostTokenRequested"] is True


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
    assert lease.requests[1]["body"]["hostTokenRequested"] is True


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
    "code",
    [
        "REMOTE_ACCESS_DISABLED",
        "HOSTING_NOT_ALLOWED",
        "CREW_IDENTITY_DISABLED",
        "VERSION_UNSUPPORTED",
    ],
)
async def test_a_definitive_refusal_stops_renewals(
    lease: FakeLeaseService, tmp_path: Path, clock: StepClock, code: str
) -> None:
    lease.queue(*refusal(code))
    source = make_source(lease, tmp_path, clock)
    try:
        with pytest.raises(HostTokenUnavailable, match=code):
            await source.current()
        with pytest.raises(HostTokenUnavailable, match=code):
            await source.renew(host_token_requested=True)
        for _ in range(20):
            await asyncio.sleep(0)
    finally:
        await source.aclose()
    assert len(lease.requests) == 1


@pytest.mark.parametrize("code", ["RATE_LIMITED", "IDENTITY_IN_USE"])
async def test_a_refusal_with_a_wait_is_never_retried_early(
    lease: FakeLeaseService, tmp_path: Path, clock: StepClock, code: str
) -> None:
    status, body = refusal(code)
    lease.queue(status, body)
    lease.queue(200, answer(0))
    source = make_source(lease, tmp_path, clock)
    try:
        with pytest.raises(HostTokenRetryLater, match=code):
            await source.current()
        with pytest.raises(HostTokenRetryLater):
            await source.current()
        assert len(lease.requests) == 1
        wait = await clock.next_sleep()
        assert wait >= body["error"]["retryAfterSeconds"]
        clock.elapse()
        await calls(lease, 2)
    finally:
        await source.aclose()


async def test_an_endpoint_mismatch_renews_at_once_without_the_endpoint(
    lease: FakeLeaseService, tmp_path: Path, clock: StepClock
) -> None:
    lease.queue(200, answer(0))
    lease.queue(*refusal("ENDPOINT_MISMATCH"))
    lease.queue(200, answer(1))
    source = make_source(lease, tmp_path, clock)
    try:
        await source.current()
        await clock.next_sleep()
        clock.elapse()
        await calls(lease, 3)
    finally:
        await source.aclose()
    relays = [r["body"]["relay"] for r in lease.requests]
    assert "endpointId" in relays[1]
    assert "endpointId" not in relays[2]


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
        with pytest.raises(HostTokenUnavailable, match="SIGN_IN_EXPIRED"):
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
    "status,body", [(401, {}), (400, {"error": {"code": "NOT_IN_THE_FIXTURE"}}), (200, {"x": 1})]
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
    lease.queue(*refusal("RATE_LIMITED"))
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
        "relay_region": "us-east-1",
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
    connected = [r["body"]["relay"]["connected"] for r in lease.requests]
    assert connected == [False, True]


async def test_a_static_source_still_serves_the_seam() -> None:
    source = StaticHostTokenSource(HostCredential(make_token()))
    source.track_relay(lambda: True)
    await source.aclose()
    assert (await source.current()).token == make_token()
