"""The relay client checks a relayed request's Kiro access token before it stamps the caller.

A frame that carries ``accessToken`` is handed over only when the token is an
ES256 token from the issuer this Crew's host token names, for this Crew's endpoint,
Region and kind, inside its time window, and for the frame's own user; the stamp
then names the verified ``sub``. Any failed check refuses the request before it
reaches the gateway. A frame with no token is still handed over as the relay named
it, and counted, until the relay sends the token on every frame.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any, Dict, List, Optional

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from kiro_crew.dashboard.kiro_relay.caller import RelayCallerKind, published_relay_caller
from kiro_crew.dashboard.kiro_relay.ingress import (
    RELAY_INGRESS_APP_KEY,
    RelayIngress,
    relay_ingress_middleware,
)
from kiro_crew.dashboard.relay_headers import relay_header_strip_middleware
from kiro_crew.relay_client.access_token import (
    AccessScope,
    AccessTokenRefused,
    AccessTokenVerifier,
    KeySet,
    access_scope,
)
from kiro_crew.relay_client.ingress import (
    IngressForbidden,
    LoopbackIngress,
    RelayedRequest,
    RelayedWebSocket,
)
from kiro_crew.relay_client.protocol import WS_MESSAGE_MAX_BYTES, FrameError, parse_inbound

pytestmark = pytest.mark.asyncio

ISSUER = "https://issuer.example"
REGION = "us-east-1"
ENDPOINT = "k7qm3x2vtzr4hbn6cwapfe5dj3"
OWNER = "u-0001.example-owner"
TEAMMATE = "u-0002.example-teammate"
NOW = 1_800_000_000.0
KID = "key-1"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class _Issuer:
    """Signs access tokens and serves its key set to a counting fetch."""

    def __init__(self) -> None:
        self.keys: Dict[str, ec.EllipticCurvePrivateKey] = {
            KID: ec.generate_private_key(ec.SECP256R1())
        }
        self.fetched: List[str] = []

    def jwks(self) -> bytes:
        entries = []
        for kid, key in self.keys.items():
            numbers = key.public_key().public_numbers()
            entries.append(
                {
                    "kty": "EC",
                    "crv": "P-256",
                    "alg": "ES256",
                    "use": "sig",
                    "kid": kid,
                    "x": _b64(numbers.x.to_bytes(32, "big")),
                    "y": _b64(numbers.y.to_bytes(32, "big")),
                }
            )
        return json.dumps({"keys": entries}).encode()

    async def fetch(self, url: str) -> bytes:
        self.fetched.append(url)
        return self.jwks()

    def token(
        self,
        *,
        header: Optional[Dict[str, Any]] = None,
        signer: Optional[ec.EllipticCurvePrivateKey] = None,
        **claims: Any,
    ) -> str:
        head = {"alg": "ES256", "typ": "kiro-relay-access+jwt", "kid": KID}
        head.update(header or {})
        body: Dict[str, Any] = {
            "iss": ISSUER,
            "aud": "kiro-relay/access",
            "sub": TEAMMATE,
            "kind": "team",
            "host": "crew",
            "ep": ENDPOINT,
            "rgn": REGION,
            "iat": NOW - 10,
            "nbf": NOW - 10,
            "exp": NOW + 290,
            "jti": "j-1",
        }
        body.update(claims)
        body = {k: v for k, v in body.items() if v is not _DROP}
        signing_input = f"{_b64(json.dumps(head).encode())}.{_b64(json.dumps(body).encode())}"
        key = signer or self.keys[str(head.get("kid", KID))]
        r, s = decode_dss_signature(key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
        return f"{signing_input}.{_b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


_DROP = object()
TEAM = AccessScope(issuer=ISSUER, region=REGION, endpoint_id=ENDPOINT, crew_kind="team")


def _verifier(
    issuer: _Issuer, now: float = NOW, clock: Optional[List[float]] = None
) -> AccessTokenVerifier:
    tick = clock if clock is not None else [0.0]
    return AccessTokenVerifier(fetch=issuer.fetch, clock=lambda: now, monotonic=lambda: tick[0])


# ── the token checks ──


async def test_a_valid_token_yields_its_sub() -> None:
    issuer = _Issuer()
    assert await _verifier(issuer).verify(issuer.token(), scope=TEAM, user=TEAMMATE) == TEAMMATE
    assert issuer.fetched == [ISSUER + "/.well-known/jwks.json"]


async def test_the_clock_skew_is_thirty_seconds_each_way() -> None:
    issuer = _Issuer()
    late = issuer.token(nbf=NOW + 30, iat=NOW + 30, exp=NOW + 300)
    assert await _verifier(issuer).verify(late, scope=TEAM, user=TEAMMATE) == TEAMMATE
    just_expired = issuer.token(iat=NOW - 300, nbf=NOW - 300, exp=NOW - 29)
    assert await _verifier(issuer).verify(just_expired, scope=TEAM, user=TEAMMATE) == TEAMMATE


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"iss": "https://other-issuer.example"}, "issuer"),
        ({"aud": "kiro-relay/host"}, "audience"),
        ({"aud": ["kiro-relay/access"]}, "audience"),
        ({"rgn": "eu-central-1"}, "region"),
        ({"ep": "zzzzzzzzzzzzzzzzzzzzzzzzzz"}, "endpoint"),
        ({"kind": "personal"}, "kind"),
        ({"sub": OWNER}, "subject"),
        ({"sub": _DROP}, "subject"),
        ({"nbf": NOW + 31, "iat": NOW + 31, "exp": NOW + 301}, "not_yet_valid"),
        ({"iat": NOW - 300, "nbf": NOW - 300, "exp": NOW - 30}, "expired"),
        ({"exp": NOW + 291}, "lifetime"),
        ({"iat": _DROP}, "times"),
        ({"exp": True}, "times"),
    ],
    ids=lambda v: v if isinstance(v, str) else None,
)
async def test_every_claim_mismatch_refuses(change: Dict[str, Any], code: str) -> None:
    issuer = _Issuer()
    with pytest.raises(AccessTokenRefused) as refused:
        await _verifier(issuer).verify(issuer.token(**change), scope=TEAM, user=TEAMMATE)
    assert refused.value.code == code


@pytest.mark.parametrize(
    ("header", "code"),
    [
        ({"typ": "kiro-relay-host+jwt"}, "type"),
        ({"typ": _DROP}, "type"),
        ({"alg": "HS256"}, "header"),
        ({"alg": "none"}, "header"),
        ({"crit": ["exp"]}, "header"),
    ],
)
async def test_a_bad_header_refuses(header: Dict[str, Any], code: str) -> None:
    issuer = _Issuer()
    token = issuer.token(header={k: v for k, v in header.items() if v is not _DROP})
    if header.get("typ") is _DROP:
        head, rest = token.split(".", 1)
        decoded = json.loads(base64.urlsafe_b64decode(head + "=="))
        decoded.pop("typ")
        token = _b64(json.dumps(decoded).encode()) + "." + rest
    with pytest.raises(AccessTokenRefused) as refused:
        await _verifier(issuer).verify(token, scope=TEAM, user=TEAMMATE)
    assert refused.value.code == code


async def test_a_token_signed_by_another_key_refuses() -> None:
    issuer = _Issuer()
    forged = issuer.token(signer=ec.generate_private_key(ec.SECP256R1()))
    with pytest.raises(AccessTokenRefused) as refused:
        await _verifier(issuer).verify(forged, scope=TEAM, user=TEAMMATE)
    assert refused.value.code == "signature"


async def test_a_changed_claim_breaks_the_signature() -> None:
    issuer = _Issuer()
    head, _body, sig = issuer.token().split(".")
    other = issuer.token(sub=OWNER).split(".")[1]
    with pytest.raises(AccessTokenRefused) as refused:
        await _verifier(issuer).verify(f"{head}.{other}.{sig}", scope=TEAM, user=OWNER)
    assert refused.value.code == "signature"


@pytest.mark.parametrize("token", ["", "a.b", "a.b.c.d", "é.b.c", 7, "x" * 5000])
async def test_a_malformed_token_refuses(token: Any) -> None:
    with pytest.raises(AccessTokenRefused):
        await _verifier(_Issuer()).verify(token, scope=TEAM, user=TEAMMATE)


# ── the key set ──


async def test_an_unknown_key_id_triggers_exactly_one_refetch() -> None:
    issuer = _Issuer()
    clock = [0.0]
    verifier = _verifier(issuer, clock=clock)
    await verifier.verify(issuer.token(), scope=TEAM, user=TEAMMATE)
    assert len(issuer.fetched) == 1
    issuer.keys["key-2"] = ec.generate_private_key(ec.SECP256R1())
    clock[0] = 11.0
    rotated = issuer.token(header={"kid": "key-2"})
    assert await verifier.verify(rotated, scope=TEAM, user=TEAMMATE) == TEAMMATE
    assert len(issuer.fetched) == 2
    # Held now: no further fetch, however often it is named.
    await verifier.verify(rotated, scope=TEAM, user=TEAMMATE)
    assert len(issuer.fetched) == 2


async def test_a_made_up_key_id_costs_one_fetch_per_interval() -> None:
    issuer = _Issuer()
    clock = [0.0]
    verifier = _verifier(issuer, clock=clock)
    await verifier.verify(issuer.token(), scope=TEAM, user=TEAMMATE)
    clock[0] = 11.0
    for _ in range(5):
        with pytest.raises(AccessTokenRefused) as refused:
            bogus = issuer.token(header={"kid": "nope"}, signer=issuer.keys[KID])
            await verifier.verify(bogus, scope=TEAM, user=TEAMMATE)
        assert refused.value.code == "unknown_key"
    assert len(issuer.fetched) == 2


async def test_concurrent_lookups_share_one_fetch() -> None:
    issuer = _Issuer()
    gate = asyncio.Event()

    async def slow(url: str) -> bytes:
        await gate.wait()
        return await issuer.fetch(url)

    key_set = KeySet(ISSUER + "/.well-known/jwks.json", fetch=slow, monotonic=lambda: 0.0)
    lookups = [asyncio.ensure_future(key_set.key(KID)) for _ in range(5)]
    await asyncio.sleep(0)
    gate.set()
    assert all(k is not None for k in await asyncio.gather(*lookups))
    assert key_set.fetches == 1


async def test_a_failed_fetch_keeps_the_keys_held() -> None:
    issuer = _Issuer()
    clock = [0.0]
    calls = [0]

    async def flaky(url: str) -> bytes:
        calls[0] += 1
        if calls[0] > 1:
            raise OSError("down")
        return await issuer.fetch(url)

    key_set = KeySet(ISSUER + "/.well-known/jwks.json", fetch=flaky, monotonic=lambda: clock[0])
    assert await key_set.key(KID) is not None
    clock[0] = 11.0
    assert await key_set.key("other") is None
    assert await key_set.key(KID) is not None


# ── where the expected values come from ──


def test_the_scope_comes_from_the_host_tokens_claims() -> None:
    claims = {"iss": ISSUER, "rgn": REGION, "ep": ENDPOINT, "kind": "team", "sub": "crew-1"}
    assert access_scope(claims) == TEAM
    assert TEAM.key_set_url == "https://issuer.example/.well-known/jwks.json"


@pytest.mark.parametrize(
    "change",
    [
        {"iss": "http://issuer.example"},
        {"iss": "https://issuer.example/"},
        {"iss": "https://u:p@issuer.example"},
        {"iss": "https://issuer.example?x=1"},
        {"iss": None},
        {"rgn": ""},
        {"ep": "Not-An-Endpoint"},
        {"kind": "other"},
    ],
)
def test_a_host_token_that_names_no_usable_scope_yields_none(change: Dict[str, Any]) -> None:
    claims = {"iss": ISSUER, "rgn": REGION, "ep": ENDPOINT, "kind": "team", **change}
    assert access_scope(claims) is None


def test_a_non_string_access_token_is_a_malformed_frame() -> None:
    frame = {
        "type": "ws-upgrade",
        "id": 1,
        "path": "/ws",
        "query": "",
        "headers": [],
        "user": TEAMMATE,
    }
    assert parse_inbound(json.dumps({**frame, "accessToken": "a.b.c"}))["accessToken"] == "a.b.c"
    with pytest.raises(FrameError):
        parse_inbound(json.dumps({**frame, "accessToken": 7}))


# ── through the hand-off, to a gateway with the relay layers ──


class _Gateway:
    """The relay ingress and strip in the gateway's order, recording each caller."""

    def __init__(self) -> None:
        self.ingress = RelayIngress(enabled=True)
        self.seen: List[Any] = []
        self._server: Optional[TestServer] = None

    async def __aenter__(self) -> "_Gateway":
        app = web.Application(
            middlewares=[
                relay_ingress_middleware(strict_internal_paths=frozenset()),
                relay_header_strip_middleware,
            ]
        )
        app[RELAY_INGRESS_APP_KEY] = self.ingress
        app.router.add_get("/api/sessions", self._sessions)
        app.router.add_get("/ws", self._ws)
        self._server = TestServer(app, host="127.0.0.1", port=0)
        await self._server.start_server()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._server is not None:
            await self._server.close()

    async def _sessions(self, request: web.Request) -> web.Response:
        self.seen.append(published_relay_caller(request))
        return web.json_response({"ok": True})

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        self.seen.append(published_relay_caller(request))
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                await ws.send_str(msg.data)
        return ws

    def handoff(self, issuer: _Issuer, *, kind: str, owner: str = "") -> LoopbackIngress:
        assert self._server is not None and self._server.port
        ingress = LoopbackIngress(
            "127.0.0.1",
            self._server.port,
            gateway=self.ingress,
            max_ws_message_bytes=WS_MESSAGE_MAX_BYTES,
            verifier=_verifier(issuer),
        )
        scope = AccessScope(issuer=ISSUER, region=REGION, endpoint_id=ENDPOINT, crew_kind=kind)
        ingress.bind_host(crew_kind=kind, owner_user_id=owner, access_scope=scope)
        return ingress


async def _status(ingress: LoopbackIngress, request: RelayedRequest) -> int:
    response = await ingress.open_http(request, None)
    try:
        return response.status
    finally:
        await response.aclose()


def _get(user: str, token: Optional[str] = None) -> RelayedRequest:
    return RelayedRequest(
        method="GET", path="/api/sessions", query="", headers=(), user=user, access_token=token
    )


async def test_a_verified_teammate_is_a_teammate_and_the_owner_is_the_owner() -> None:
    issuer = _Issuer()
    async with _Gateway() as gateway:
        team = gateway.handoff(issuer, kind="team")
        try:
            assert await _status(team, _get(TEAMMATE, issuer.token())) == 200
        finally:
            await team.aclose()
        personal = gateway.handoff(issuer, kind="personal", owner=OWNER)
        try:
            token = issuer.token(sub=OWNER, kind="personal")
            assert await _status(personal, _get(OWNER, token)) == 200
        finally:
            await personal.aclose()
    assert [(c.kiro_user_id, c.kind) for c in gateway.seen] == [
        (TEAMMATE, RelayCallerKind.TEAMMATE),
        (OWNER, RelayCallerKind.OWNER),
    ]
    assert team.tokenless_frames == personal.tokenless_frames == 0


async def test_a_tampered_token_never_reaches_the_gateway() -> None:
    issuer = _Issuer()
    head, _body, sig = issuer.token().split(".")
    forged_body = issuer.token(sub=OWNER).split(".")[1]
    async with _Gateway() as gateway:
        ingress = gateway.handoff(issuer, kind="team")
        try:
            with pytest.raises(IngressForbidden):
                await ingress.open_http(_get(OWNER, f"{head}.{forged_body}.{sig}"), None)
            with pytest.raises(IngressForbidden):
                await ingress.open_websocket(
                    RelayedWebSocket(
                        path="/ws",
                        query="",
                        protocols=(),
                        headers=(),
                        user=TEAMMATE,
                        access_token=issuer.token(ep="zzzzzzzzzzzzzzzzzzzzzzzzzz"),
                    )
                )
        finally:
            await ingress.aclose()
    assert gateway.seen == []
    assert ingress.refused_tokens == 2


async def test_a_token_for_another_user_than_the_frame_names_is_refused() -> None:
    issuer = _Issuer()
    async with _Gateway() as gateway:
        ingress = gateway.handoff(issuer, kind="team")
        try:
            with pytest.raises(IngressForbidden):
                await ingress.open_http(_get(OWNER, issuer.token(sub=TEAMMATE)), None)
        finally:
            await ingress.aclose()
    assert gateway.seen == []


async def test_a_token_with_no_scope_to_check_it_against_is_refused() -> None:
    issuer = _Issuer()
    async with _Gateway() as gateway:
        ingress = gateway.handoff(issuer, kind="team")
        ingress.bind_host(crew_kind="team", owner_user_id="")
        try:
            with pytest.raises(IngressForbidden):
                await ingress.open_http(_get(TEAMMATE, issuer.token()), None)
        finally:
            await ingress.aclose()
    assert gateway.seen == []


async def test_a_tokenless_frame_passes_as_named_and_is_counted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    issuer = _Issuer()
    caplog.set_level(logging.WARNING, logger="kiro_crew.relay_client.ingress")
    async with _Gateway() as gateway:
        ingress = gateway.handoff(issuer, kind="team")
        try:
            for _ in range(3):
                assert await _status(ingress, _get(TEAMMATE)) == 200
            ws = await ingress.open_websocket(
                RelayedWebSocket(path="/ws", query="", protocols=(), headers=(), user=TEAMMATE)
            )
            await ws.send("ping")
            assert await ws.receive() == "ping"
            await ws.close(1000)
        finally:
            await ingress.aclose()
    assert [(c.kiro_user_id, c.kind) for c in gateway.seen] == [
        (TEAMMATE, RelayCallerKind.TEAMMATE)
    ] * 4
    assert ingress.tokenless_frames == 4
    assert issuer.fetched == []
    warnings = [r for r in caplog.records if "relay_access_check=absent" in r.getMessage()]
    assert len(warnings) == 1


async def test_a_tokenless_frame_for_another_user_on_a_personal_crew_is_still_refused() -> None:
    issuer = _Issuer()
    async with _Gateway() as gateway:
        ingress = gateway.handoff(issuer, kind="personal", owner=OWNER)
        try:
            assert await _status(ingress, _get(TEAMMATE)) == 403
        finally:
            await ingress.aclose()
    assert gateway.seen == []
