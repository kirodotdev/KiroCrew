"""The relay viewer's gateway routes: the Crew list and the desktop app's token mint.

On with no setting, off (404 ``RELAY_VIEWER_OFF``) only in a build that locks the
``tunnel`` control; the dev settings count only with the dev switch on. The list is owner-only and carries no
token, the mint needs loopback plus the local secret (a session alone is refused),
neither route is reachable through the relay, and every answer is ``no-store``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from token_auth_helpers import _make_request, _ok_handler, clear_nonces  # noqa: F401

from kiro_crew.auth import bridge
from kiro_crew.dashboard.handlers import relay_viewer as rv
from kiro_crew.dashboard.kiro_relay import limits
from kiro_crew.dashboard.kiro_relay.caller import RELAY_CALLER_KEY, RelayCaller, RelayCallerKind
from kiro_crew.dashboard.token_auth import token_auth_middleware
from kiro_crew.member_memory_auth import LocalOwnerProvenance
from kiro_crew.relay_viewer import cps

SECRET = "local-secret-value"
BEARER = "bearer-sign-in-9f8e7d6c5b4a"
ACCESS = "access-token-1a2b3c4d5e6f"
EP = "a" * 24
OTHER = "b" * 24
CREW = cps.CrewEndpoint(
    endpoint_id=EP,
    crew_url=f"https://{EP}.relay.example.test",
    name="Build box",
    crew_type="PERSONAL",
    presence="CONNECTED",
    relay_connection_status="CONNECTED",
)
OWNER = RelayCaller(kiro_user_id="kiro-user.owner-0001", kind=RelayCallerKind.OWNER)
#: A made-up profile ARN in the shape a sign-in stores.
PROFILE = "arn:aws:codewhisperer:us-east-1:000000000000:profile/EXAMPLEPROFILE"
TEAM_CREW = dataclasses.replace(CREW, crew_type="TEAM", name="Team box")
FP_A = "fingerprint-of-account-a"
FP_B = "fingerprint-of-account-b"
START = "https://example-portal.awsapps.example.test/start"
IDC = {"identity": "identity_center", "provider": "Enterprise", "start_url": START}
#: The real vault read, kept before the fixture replaces it.
_REAL_VAULT_CLAIMS = bridge.vault_identity_claims


def _marker(fingerprint: str) -> str:
    return hashlib.sha256(b"relay-viewer-sign-in\n" + fingerprint.encode()).hexdigest()[:16]


@pytest.fixture
def viewer(monkeypatch):
    """The dev settings on, CPS faked, the owner and host-process gates passing."""
    state = SimpleNamespace(
        enabled=True,
        calls=[],
        owner=True,
        provenance=LocalOwnerProvenance(),
        fingerprint=FP_A,
        locked=False,
    )
    tunnel = SimpleNamespace(
        relay_viewer_cps_url="https://cps.example.test",
        relay_viewer_credential="",
        relay_viewer_relay_domain="relay.example.test",
    )
    state.tunnel = tunnel

    def _tunnel():
        tunnel.relay_viewer_enabled = state.enabled
        return tunnel

    state.token_type = ""
    state.profile_arn = ""
    state.token_types = []
    state.profile_arns = []
    state.list_profile_arns = []
    state.crew_domains = []
    state.off_domain = 0

    state.refs = []

    async def _bearer(ref, rejected=""):
        state.refs.append(ref)
        return BEARER, state.token_type, state.profile_arn

    async def _list(url, bearer, *, crew_domain, token_type="", profile_arn=""):
        state.crew_domains.append(crew_domain)
        state.calls.append(("list", bearer))
        state.token_types.append(("list", token_type))
        state.list_profile_arns.append(profile_arn)
        return cps.CrewList((CREW,), state.off_domain)

    async def _mint(url, bearer, endpoint_id, *, token_type="", profile_arn=""):
        state.calls.append(("mint", endpoint_id))
        state.token_types.append(("mint", token_type))
        state.profile_arns.append(profile_arn)
        return cps.CrewAccessToken(ACCESS, 2000.0, 240.0, 1700.0)

    async def _owner(request, operation):
        return None if state.owner else web.json_response({"code": "owner_only"}, status=403)

    monkeypatch.setattr(rv, "_load_tunnel", _tunnel)
    monkeypatch.setattr(rv, "local_enable_locked", lambda control: state.locked)
    monkeypatch.setattr(rv, "_bearer", _bearer)
    monkeypatch.setattr(cps, "list_crew_endpoints", _list)
    monkeypatch.setattr(cps, "create_crew_access_token", _mint)
    monkeypatch.setattr(rv, "require_owner_dashboard_request", _owner)
    monkeypatch.setattr(
        "kiro_crew.member_memory_auth.local_owner_bootstrap_provenance",
        lambda _r: state.provenance,
    )
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: MagicMock())
    # Never the host's real vault.
    monkeypatch.setattr(
        "kiro_crew.auth.bridge.vault_identity_fingerprint", lambda: state.fingerprint
    )
    state.claims = {"identity": "builder_id", "provider": "BuilderId"}
    state.claim_reads = 0

    def _claims():
        state.claim_reads += 1
        return (state.fingerprint, dict(state.claims)) if state.fingerprint else ("", {})

    monkeypatch.setattr("kiro_crew.auth.bridge.vault_identity_claims", _claims)
    return state


def _client(relayed: bool = False) -> TestClient:
    @web.middleware
    async def _relay(request, handler):
        if relayed:
            request[RELAY_CALLER_KEY] = OWNER
        return await handler(request)

    app = web.Application(middlewares=[_relay])
    app["local_secret"] = SECRET
    app.router.add_get("/api/relay-viewer/crews", rv.api_relay_viewer_crews)
    app.router.add_post("/api/relay-viewer/token", rv.api_relay_viewer_token)
    return TestClient(TestServer(app))


async def _mint(client, headers=None, body=None):
    resp = await client.post(
        "/api/relay-viewer/token",
        json={"endpointId": EP} if body is None else body,
        headers={"X-Local-Secret": SECRET} if headers is None else headers,
    )
    await resp.read()  # kept, so the body is readable after the client closes
    return resp


@pytest.mark.asyncio
async def test_both_routes_answer_404_in_a_build_that_locks_the_tunnel_control(viewer) -> None:
    viewer.locked = True
    async with _client() as client:
        for resp in (await client.get("/api/relay-viewer/crews"), await _mint(client)):
            assert resp.status == 404
            assert (await resp.json())["code"] == "RELAY_VIEWER_OFF"
            assert resp.headers["Cache-Control"] == "no-store"
    assert viewer.calls == []


@pytest.mark.asyncio
async def test_with_the_dev_switch_off_leftover_dev_settings_are_ignored(
    viewer, monkeypatch
) -> None:
    """No setting lists; a leftover URL, credential or domain alone is never used."""
    urls = _recording_urls(monkeypatch)
    viewer.enabled = False
    viewer.tunnel.relay_viewer_credential = "secret://RELAY_VIEWER_TEST"
    async with _client() as client:
        assert (await client.get("/api/relay-viewer/crews")).status == 200
        assert (await _mint(client)).status == 200
    assert urls == [("list", US_HOST), ("list", US_HOST), ("mint", US_HOST)]
    assert viewer.refs == ["", ""]
    assert viewer.crew_domains == ["", ""]


@pytest.mark.asyncio
async def test_a_dev_relay_domain_counts_only_with_a_dev_url(viewer) -> None:
    viewer.tunnel.relay_viewer_cps_url = ""
    async with _client() as client:
        assert (await client.get("/api/relay-viewer/crews")).status == 200
    assert viewer.crew_domains == [""]


@pytest.mark.asyncio
async def test_crews_lists_for_the_owner_with_no_token(viewer, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    async with _client() as client:
        resp = await client.get("/api/relay-viewer/crews")
        text = await resp.text()
    assert resp.status == 200
    assert resp.headers["Cache-Control"] == "no-store"
    assert (await resp.json())["crews"] == [
        {
            "endpointId": EP,
            "name": "Build box",
            "crewType": "PERSONAL",
            "presence": "CONNECTED",
            "relayConnectionStatus": "CONNECTED",
            "crewUrl": CREW.crew_url,
            "lastSeenAt": None,
            "unseen": False,
        }
    ]
    assert BEARER not in text and BEARER not in caplog.text


@pytest.mark.asyncio
async def test_crews_gives_last_seen_in_whole_epoch_seconds(viewer, monkeypatch) -> None:
    seen = dataclasses.replace(CREW, presence="NOT_CONNECTED", last_seen_at=1762470000.75)

    async def _list(url, bearer, *, crew_domain, token_type="", profile_arn=""):
        return cps.CrewList((seen, dataclasses.replace(CREW, endpoint_id=OTHER)))

    monkeypatch.setattr(cps, "list_crew_endpoints", _list)
    async with _client() as client:
        resp = await client.get("/api/relay-viewer/crews")
        crews = (await resp.json())["crews"]
    assert [c["lastSeenAt"] for c in crews] == [1762470000, None]


@pytest.mark.asyncio
async def test_crews_needs_the_owner(viewer) -> None:
    viewer.owner = False
    async with _client() as client:
        resp = await client.get("/api/relay-viewer/crews")
    assert resp.status == 403
    assert resp.headers["Cache-Control"] == "no-store"
    assert viewer.calls == []


@pytest.mark.asyncio
async def test_a_cps_refusal_passes_its_code_through(viewer, monkeypatch) -> None:
    async def _throttled(url, bearer, *, crew_domain, token_type="", profile_arn=""):
        raise cps.CpsRefused(cps.THROTTLED, "slow down", status=429, retry_after=7.0)

    monkeypatch.setattr(cps, "list_crew_endpoints", _throttled)
    async with _client() as client:
        resp = await client.get("/api/relay-viewer/crews")
        body = await resp.json()
    assert (resp.status, body["code"], body["retryAfter"]) == (429, "THROTTLED", 7.0)
    assert resp.headers["Retry-After"] == "7"
    assert "requestId" not in body


@pytest.mark.parametrize(
    ("code", "status", "retry_after"),
    [(cps.SERVICE_ERROR, 502, None), (cps.THROTTLED, 429, 3.0)],
)
@pytest.mark.asyncio
async def test_a_crews_refusal_carries_the_cps_request_id(
    viewer, monkeypatch, code, status, retry_after
) -> None:
    async def _refused(url, bearer, *, crew_domain, token_type="", profile_arn=""):
        raise cps.CpsRefused(
            code, "refused", status=503, retry_after=retry_after, request_id="req-1"
        )

    monkeypatch.setattr(cps, "list_crew_endpoints", _refused)
    async with _client() as client:
        resp = await client.get("/api/relay-viewer/crews")
        body = await resp.json()
    assert (resp.status, body["code"], body["requestId"]) == (status, code, "req-1")


@pytest.mark.asyncio
async def test_a_crews_refusal_with_no_request_id_leaves_the_field_out(viewer, monkeypatch) -> None:
    async def _refused(url, bearer, *, crew_domain, token_type="", profile_arn=""):
        raise cps.CpsRefused(cps.SERVICE_ERROR, "refused", status=503)

    monkeypatch.setattr(cps, "list_crew_endpoints", _refused)
    async with _client() as client:
        resp = await client.get("/api/relay-viewer/crews")
        body = await resp.json()
    assert body == {"error": "refused", "code": cps.SERVICE_ERROR}


@pytest.mark.asyncio
async def test_a_token_refusal_carries_the_cps_request_id(viewer, monkeypatch) -> None:
    async def _refused(url, bearer, endpoint_id, *, token_type="", profile_arn=""):
        raise cps.CpsRefused(cps.CREW_OFFLINE, "refused", status=409, request_id="req-2")

    monkeypatch.setattr(cps, "create_crew_access_token", _refused)
    async with _client() as client:
        resp = await _mint(client)
        body = await resp.json()
    assert (resp.status, body["code"], body["requestId"]) == (403, cps.CREW_OFFLINE, "req-2")


@pytest.mark.asyncio
async def test_token_mints_for_main_with_the_local_secret(viewer, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    async with _client() as client:
        resp = await _mint(client)
        body = await resp.json()
    assert resp.status == 200
    assert resp.headers["Cache-Control"] == "no-store"
    assert body == {
        "accessToken": ACCESS,
        "expiresAt": 2000.0,
        "renewInSeconds": 240.0,
        "serverDate": 1700.0,
        "crewUrl": CREW.crew_url,
        "relayDomain": "relay.example.test",
        "name": "Build box",
        "signInMarker": _marker(FP_A),
    }
    assert viewer.calls == [("list", BEARER), ("mint", EP)]
    assert ACCESS not in caplog.text and BEARER not in caplog.text


@pytest.mark.parametrize(
    "headers,code",
    [
        ({}, "invalid_secret"),
        ({"X-Local-Secret": "wrong"}, "invalid_secret"),
        # A dashboard session alone is not the local secret.
        ({"Cookie": "mc_token_5476=session-value"}, "invalid_secret"),
    ],
)
@pytest.mark.asyncio
async def test_token_needs_the_local_secret(viewer, headers, code) -> None:
    async with _client() as client:
        resp = await _mint(client, headers=headers)
    assert resp.status == 403
    assert (await resp.json())["code"] == code
    assert resp.headers["Cache-Control"] == "no-store"
    assert viewer.calls == []


@pytest.mark.asyncio
async def test_token_needs_loopback(viewer, monkeypatch) -> None:
    monkeypatch.setattr(rv, "is_loopback", lambda _h: False)
    monkeypatch.setattr(rv, "_unix_peer_is_self", lambda _r: False)
    async with _client() as client:
        resp = await _mint(client)
    assert resp.status == 403
    assert (await resp.json())["code"] == "loopback_only"
    assert viewer.calls == []


@pytest.mark.parametrize("provenance", [None, LocalOwnerProvenance(app="some-app")])
@pytest.mark.asyncio
async def test_token_needs_a_host_process_not_an_app(viewer, provenance) -> None:
    viewer.provenance = provenance
    async with _client() as client:
        resp = await _mint(client)
    assert resp.status == 403
    assert (await resp.json())["code"] == "member_owner_token_refused"
    assert viewer.calls == []


@pytest.mark.parametrize("body", [{}, {"endpointId": "NOT-AN-ID"}, ["x"]])
@pytest.mark.asyncio
async def test_token_refuses_a_bad_endpoint_without_calling_cps(viewer, body) -> None:
    async with _client() as client:
        resp = await _mint(client, body=body)
    assert resp.status == 400
    assert (await resp.json())["code"] == "INVALID_REQUEST"
    assert viewer.calls == []


@pytest.mark.asyncio
async def test_token_refuses_a_crew_not_in_the_list(viewer) -> None:
    async with _client() as client:
        resp = await _mint(client, body={"endpointId": OTHER})
    assert resp.status == 403
    assert (await resp.json())["code"] == "CREW_ACCESS_NOT_GRANTED"
    assert viewer.calls == [("list", BEARER)]


@pytest.mark.asyncio
async def test_crews_refuses_a_relayed_caller(viewer) -> None:
    async with _client(relayed=True) as client:
        resp = await client.get("/api/relay-viewer/crews")
    assert resp.status == 403
    assert resp.headers["Cache-Control"] == "no-store"
    assert viewer.calls == []


@pytest.mark.parametrize("path", ["/api/relay-viewer/crews", "/api/relay-viewer/token"])
@pytest.mark.parametrize("kind", list(RelayCallerKind))
def test_the_relay_limits_refuse_both_routes(path, kind) -> None:
    caller = RelayCaller(kiro_user_id="kiro-user.someone-0003", kind=kind)
    got = limits.remote_limit_for(path, {}, caller, strict_internal_paths=())
    assert got is limits.LOCAL_CREDENTIAL


@pytest.mark.asyncio
async def test_token_auth_bypass_is_post_only_and_crews_is_gated() -> None:
    mw = token_auth_middleware()
    post = await mw(_make_request(path="/api/relay-viewer/token", method="POST"), _ok_handler)
    assert post.status == 200
    for method, path in (("GET", "/api/relay-viewer/token"), ("GET", "/api/relay-viewer/crews")):
        resp = await mw(_make_request(path=path, method=method), _ok_handler)
        assert resp.status == 403


@pytest.mark.asyncio
async def test_sign_in_missing_is_signed_out(monkeypatch) -> None:
    from kiro_crew.auth.provider import KasAuthProvider, NotAuthenticated

    async def _none(self):
        raise NotAuthenticated("no stored Kiro credential")

    monkeypatch.setattr(KasAuthProvider, "current", _none)
    with pytest.raises(cps.CpsRefused) as exc:
        await rv._bearer("")
    assert exc.value.code == cps.SIGNED_OUT


@pytest.mark.asyncio
async def test_the_token_type_reaches_both_the_list_and_the_mint(viewer) -> None:
    viewer.token_type = "EXTERNAL_IDP"
    async with _client() as client:
        assert (await client.get("/api/relay-viewer/crews")).status == 200
        assert (await _mint(client)).status == 200
    assert viewer.token_types == [
        ("list", "EXTERNAL_IDP"),
        ("list", "EXTERNAL_IDP"),
        ("mint", "EXTERNAL_IDP"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("auth_method", "token_type"),
    [("external_idp", "EXTERNAL_IDP"), ("identity_center", ""), (None, "")],
)
async def test_the_sign_in_names_its_token_type(monkeypatch, auth_method, token_type) -> None:
    from kiro_crew.auth.provider import KasAuthProvider

    async def _current(self):
        return SimpleNamespace(access_token=BEARER, auth_method=auth_method, profile_arn=None)

    monkeypatch.setattr(KasAuthProvider, "current", _current)
    assert await rv._bearer("") == (BEARER, token_type, "")


@pytest.mark.asyncio
async def test_the_sign_in_gives_its_profile_arn(monkeypatch) -> None:
    from kiro_crew.auth.provider import KasAuthProvider

    async def _current(self):
        return SimpleNamespace(access_token=BEARER, auth_method=None, profile_arn=PROFILE)

    monkeypatch.setattr(KasAuthProvider, "current", _current)
    assert await rv._bearer("") == (BEARER, "", PROFILE)


@pytest.mark.asyncio
async def test_the_test_credential_sends_no_token_type(monkeypatch) -> None:
    monkeypatch.setattr(
        "kiro_crew.relay_client.lease.read_lease_credential", lambda ref: "test-credential-value"
    )
    assert await rv._bearer("file:/somewhere") == ("test-credential-value", "", "")


FRESH = "bearer-sign-in-refreshed-0a1b2c"


def _refusing_list(viewer, monkeypatch, refusals):
    """``list_crew_endpoints`` answers each scripted HTTP status as a refusal, then lists."""
    seen = []

    async def _bearer(ref, rejected=""):
        seen.append(rejected)
        return (FRESH if rejected else BEARER), "", ""

    async def _list(url, bearer, *, crew_domain, token_type="", profile_arn=""):
        viewer.calls.append(("list", bearer))
        if refusals:
            status = refusals.pop(0)
            code = cps.SIGNED_OUT if status == 401 else cps.ACCESS_DENIED
            raise cps.CpsRefused(code, "refused", status=status)
        return cps.CrewList((CREW,))

    monkeypatch.setattr(rv, "_bearer", _bearer)
    monkeypatch.setattr(cps, "list_crew_endpoints", _list)
    return seen


@pytest.mark.asyncio
async def test_a_cps_401_refreshes_the_sign_in_and_resends_once(viewer, monkeypatch) -> None:
    seen = _refusing_list(viewer, monkeypatch, [401])
    async with _client() as client:
        resp = await _mint(client)
    assert resp.status == 200
    assert seen == ["", BEARER]
    assert viewer.calls == [("list", BEARER), ("list", FRESH), ("mint", EP)]


@pytest.mark.asyncio
async def test_a_second_cps_401_is_signed_out_after_one_refresh(viewer, monkeypatch) -> None:
    seen = _refusing_list(viewer, monkeypatch, [401, 401])
    async with _client() as client:
        resp = await client.get("/api/relay-viewer/crews")
        body = await resp.json()
    assert (resp.status, body["code"]) == (409, "SIGNED_OUT")
    assert seen == ["", BEARER]
    assert viewer.calls == [("list", BEARER), ("list", FRESH)]


@pytest.mark.asyncio
async def test_a_cps_403_does_not_refresh(viewer, monkeypatch) -> None:
    seen = _refusing_list(viewer, monkeypatch, [403])
    async with _client() as client:
        resp = await client.get("/api/relay-viewer/crews")
    assert resp.status == 403
    assert seen == [""]


@pytest.mark.asyncio
async def test_the_forced_refresh_waits_out_its_cooldown(monkeypatch, caplog) -> None:
    from kiro_crew.auth.provider import KasAuthProvider

    caplog.set_level(logging.DEBUG)
    forced = []

    async def _current(self, *, rejected_access_token=None):
        forced.append(rejected_access_token)
        return SimpleNamespace(
            access_token=FRESH if rejected_access_token else BEARER,
            auth_method=None,
            identity="identity_center",
            profile_arn=None,
        )

    monkeypatch.setattr(KasAuthProvider, "current", _current)
    monkeypatch.setattr(rv, "_FORCED_AT", {})
    monkeypatch.setattr(rv, "_FORCE_LOCKS", {})
    now = [1000.0]
    monkeypatch.setattr(rv, "time", SimpleNamespace(monotonic=lambda: now[0]))
    assert await rv._bearer("", rejected=BEARER) == (FRESH, "", "")
    now[0] += rv.FORCE_COOLDOWN_SECONDS - 1
    # Inside the cooldown: read again, not forced, so the caller sees the same sign-in.
    assert await rv._bearer("", rejected=BEARER) == (BEARER, "", "")
    now[0] += 1
    assert await rv._bearer("", rejected=BEARER) == (FRESH, "", "")
    # Each call reads once, then again under the lock, and forces only outside the cooldown.
    assert forced == [None, None, BEARER, None, None, None, None, BEARER]
    for secret in (BEARER, FRESH):
        assert secret not in caplog.text


@pytest.mark.asyncio
async def test_a_peer_refreshed_sign_in_is_not_forced(monkeypatch) -> None:
    from kiro_crew.auth.provider import KasAuthProvider

    forced = []

    async def _current(self, *, rejected_access_token=None):
        forced.append(rejected_access_token)
        return SimpleNamespace(
            access_token=FRESH, auth_method=None, identity="identity_center", profile_arn=None
        )

    monkeypatch.setattr(KasAuthProvider, "current", _current)
    monkeypatch.setattr(rv, "_FORCED_AT", {})
    assert await rv._bearer("", rejected=BEARER) == (FRESH, "", "")
    assert forced == [None]


@pytest.mark.asyncio
async def test_two_concurrent_401s_share_one_refresh_and_both_resend(monkeypatch) -> None:
    import asyncio

    from kiro_crew.auth.provider import KasAuthProvider
    from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

    stored = [BEARER]
    refreshes = []
    sent = []
    both_refused = asyncio.Event()

    async def _current(self, *, rejected_access_token=None):
        if rejected_access_token is not None and stored[0] == rejected_access_token:
            refreshes.append(1)
            # The refresh is still in flight when the second request is refused.
            await asyncio.wait_for(both_refused.wait(), FALLBACK_TIMEOUT_SECS)
            stored[0] = FRESH
        return SimpleNamespace(
            access_token=stored[0], auth_method=None, identity="identity_center", profile_arn=None
        )

    async def _call(bearer, token_type, profile_arn):
        sent.append(bearer)
        if bearer == BEARER:
            if sent.count(BEARER) == 2:
                both_refused.set()
            raise cps.CpsRefused(cps.SIGNED_OUT, "refused", status=401)
        return bearer

    monkeypatch.setattr(KasAuthProvider, "current", _current)
    monkeypatch.setattr(rv, "_FORCED_AT", {})
    monkeypatch.setattr(rv, "_FORCE_LOCKS", {})
    assert await asyncio.gather(rv._with_sign_in("", _call), rv._with_sign_in("", _call)) == [
        FRESH,
        FRESH,
    ]
    assert refreshes == [1]
    assert sorted(sent) == sorted([BEARER, BEARER, FRESH, FRESH])


@pytest.mark.asyncio
async def test_the_sign_in_marker_follows_the_account_and_is_never_logged(viewer, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    async with _client() as client:
        first = await (await _mint(client)).json()
        again = await (await _mint(client)).json()
        viewer.fingerprint = FP_B
        switched = await (await _mint(client)).json()
        viewer.fingerprint = ""
        unread = await (await _mint(client)).json()
    assert first["signInMarker"] == again["signInMarker"] == _marker(FP_A)
    assert len(first["signInMarker"]) == 16
    assert switched["signInMarker"] == _marker(FP_B) != first["signInMarker"]
    assert unread["signInMarker"] is None
    for value in (_marker(FP_A), _marker(FP_B), FP_A, FP_B):
        assert value not in caplog.text


@pytest.mark.asyncio
async def test_the_test_credential_has_no_sign_in_marker(monkeypatch) -> None:
    read = []
    monkeypatch.setattr(
        "kiro_crew.auth.bridge.vault_identity_fingerprint", lambda: read.append(1) or FP_A
    )
    assert await rv._sign_in_marker("relay-viewer-test-credential") is None
    assert read == []


@pytest.mark.asyncio
async def test_a_refusal_carries_no_sign_in_marker(viewer, monkeypatch) -> None:
    async def _refuse(url, bearer, endpoint_id, *, token_type="", profile_arn=""):
        raise cps.CpsRefused(cps.CREW_OFFLINE, "the Crew is offline")

    monkeypatch.setattr(cps, "create_crew_access_token", _refuse)
    async with _client() as client:
        resp = await _mint(client)
        body = await resp.json()
    assert resp.status == 403
    assert "signInMarker" not in body
    assert _marker(FP_A) not in str(body)


def _switching(viewer, monkeypatch, *, at_bearer=None, at_mint=None):
    """Move the vault fingerprint during the bearer read and/or the mint."""

    async def _bearer(ref, rejected=""):
        if at_bearer is not None:
            viewer.fingerprint = at_bearer
        return BEARER, "", ""

    async def _mint(url, bearer, endpoint_id, *, token_type="", profile_arn=""):
        viewer.calls.append(("mint", endpoint_id))
        if at_mint is not None:
            viewer.fingerprint = at_mint
        return cps.CrewAccessToken(ACCESS, 2000.0, 240.0, 1700.0)

    monkeypatch.setattr(rv, "_bearer", _bearer)
    monkeypatch.setattr(cps, "create_crew_access_token", _mint)


async def _assert_switch_refused(viewer, caplog, fingerprints) -> None:
    caplog.set_level(logging.DEBUG)
    async with _client() as client:
        resp = await _mint(client)
        body = await resp.json()
    assert (resp.status, body["code"]) == (502, "UNREACHABLE")
    assert "sign-in changed" in body["error"]
    assert "accessToken" not in body and "signInMarker" not in body
    assert ACCESS not in str(body) and ACCESS not in caplog.text
    assert f"relay viewer: mint {EP} -> UNREACHABLE" in caplog.text
    for fp in fingerprints:
        assert fp not in caplog.text and _marker(fp) not in caplog.text
        assert _marker(fp) not in str(body)


@pytest.mark.asyncio
async def test_a_switch_before_the_bearer_read_refuses_the_grant(viewer, monkeypatch, caplog):
    _switching(viewer, monkeypatch, at_bearer=FP_B)
    await _assert_switch_refused(viewer, caplog, (FP_A, FP_B))


@pytest.mark.asyncio
async def test_a_switch_after_the_mint_refuses_the_grant(viewer, monkeypatch, caplog):
    _switching(viewer, monkeypatch, at_mint=FP_B)
    await _assert_switch_refused(viewer, caplog, (FP_A, FP_B))
    assert viewer.calls[-1] == ("mint", EP), "the grant was minted, then discarded"


@pytest.mark.asyncio
async def test_a_switch_away_and_back_still_refuses_the_grant(viewer, monkeypatch, caplog):
    # A to B to A: every sign-in writes a new generation, so A's second sign-in
    # does not fingerprint like its first.
    from kiro_crew.auth.bridge import _identity_digest

    account_a = SimpleNamespace(identity="social", provider="Google", profile_arn="arn:a")
    account_b = SimpleNamespace(identity="social", provider="Google", profile_arn="arn:b")
    a1 = _identity_digest(account_a, "generation-1")
    b = _identity_digest(account_b, "generation-2")
    a2 = _identity_digest(account_a, "generation-3")
    assert a1 != a2
    viewer.fingerprint = a1
    _switching(viewer, monkeypatch, at_bearer=b, at_mint=a2)
    await _assert_switch_refused(viewer, caplog, (a1, b, a2))


@pytest.mark.asyncio
async def test_a_forced_refresh_with_no_sign_in_change_still_answers_the_marker(
    viewer, monkeypatch
) -> None:
    # A refresh saves no new generation (see test_kas_login_signout_generation's
    # test_a_refresh_write_does_not_move_the_fingerprint), so the marker holds.
    seen = _refusing_list(viewer, monkeypatch, [401])
    async with _client() as client:
        resp = await _mint(client)
        body = await resp.json()
    assert resp.status == 200
    assert seen == ["", BEARER]
    assert body["signInMarker"] == _marker(FP_A)
    assert body["accessToken"] == ACCESS


# A renewal sends the marker its window was opened under.


async def _renew(window_marker):
    async with _client() as client:
        resp = await _mint(client, body={"endpointId": EP, "signInMarker": window_marker})
        body = await resp.json()
    return resp, body


def _no_grant(viewer, monkeypatch, *, switch_to=None):
    """``list_crew_endpoints`` refuses CREW_ACCESS_NOT_GRANTED, as for an account without
    this Crew. *switch_to* moves the vault during the bearer read."""

    async def _bearer(ref, rejected=""):
        if switch_to is not None:
            viewer.fingerprint = switch_to
        return BEARER, "", ""

    async def _list(url, bearer, *, crew_domain, token_type="", profile_arn=""):
        viewer.calls.append(("list", bearer))
        raise cps.CpsRefused(cps.CREW_ACCESS_NOT_GRANTED, "this Crew isn't granted")

    monkeypatch.setattr(rv, "_bearer", _bearer)
    monkeypatch.setattr(cps, "list_crew_endpoints", _list)


def _assert_no_marker_leak(body, caplog, fingerprints) -> None:
    assert "accessToken" not in body and "signInMarker" not in body
    for fp in fingerprints:
        assert fp not in caplog.text and _marker(fp) not in caplog.text
        assert _marker(fp) not in str(body)


@pytest.mark.asyncio
async def test_a_renewal_with_its_own_marker_answers_the_grant(viewer) -> None:
    resp, body = await _renew(_marker(FP_A))
    assert resp.status == 200
    assert body["accessToken"] == ACCESS and body["signInMarker"] == _marker(FP_A)


@pytest.mark.asyncio
async def test_a_switch_refuses_sign_in_changed_before_any_cps_call(viewer, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _no_grant(viewer, monkeypatch)
    viewer.fingerprint = FP_B  # the vault now holds an account without this Crew
    resp, body = await _renew(_marker(FP_A))
    assert (resp.status, body["code"]) == (409, "SIGN_IN_CHANGED")
    assert viewer.calls == [], "no CPS call"
    assert f"relay viewer: mint {EP} -> SIGN_IN_CHANGED" in caplog.text
    _assert_no_marker_leak(body, caplog, (FP_A, FP_B))


@pytest.mark.asyncio
async def test_a_sign_out_under_a_window_marker_is_signed_out(viewer, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)

    async def _signed_out(ref, rejected=""):
        raise cps.CpsRefused(cps.SIGNED_OUT, "sign in to Kiro")

    monkeypatch.setattr(rv, "_bearer", _signed_out)
    viewer.fingerprint = ""
    resp, body = await _renew(_marker(FP_A))
    assert (resp.status, body["code"]) == (409, "SIGNED_OUT")
    _assert_no_marker_leak(body, caplog, (FP_A,))


@pytest.mark.asyncio
async def test_a_switch_between_the_reads_beats_the_no_grant_refusal(viewer, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _no_grant(viewer, monkeypatch, switch_to=FP_B)
    resp, body = await _renew(_marker(FP_A))
    assert (resp.status, body["code"]) == (409, "SIGN_IN_CHANGED")
    assert viewer.calls == [("list", BEARER)]
    _assert_no_marker_leak(body, caplog, (FP_A, FP_B))


@pytest.mark.asyncio
async def test_a_refusal_with_no_sign_in_change_passes_through(viewer, monkeypatch) -> None:
    _no_grant(viewer, monkeypatch)
    resp, body = await _renew(_marker(FP_A))
    assert (resp.status, body["code"]) == (403, "CREW_ACCESS_NOT_GRANTED")


@pytest.mark.asyncio
async def test_a_switch_after_the_mint_refuses_the_window_its_grant(viewer, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _switching(viewer, monkeypatch, at_mint=FP_B)
    resp, body = await _renew(_marker(FP_A))
    assert (resp.status, body["code"]) == (409, "SIGN_IN_CHANGED")
    assert viewer.calls[-1] == ("mint", EP), "minted, then discarded"
    _assert_no_marker_leak(body, caplog, (FP_A, FP_B))


@pytest.mark.asyncio
async def test_an_empty_read_after_the_mint_retries(viewer, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _switching(viewer, monkeypatch, at_mint="")
    resp, body = await _renew(_marker(FP_A))
    assert (resp.status, body["code"]) == (502, "UNREACHABLE")
    _assert_no_marker_leak(body, caplog, (FP_A,))


@pytest.mark.asyncio
async def test_a_transient_empty_first_read_of_the_same_account_answers(viewer, monkeypatch):
    viewer.fingerprint = ""  # the first read failed
    _switching(viewer, monkeypatch, at_bearer=FP_A)
    resp, body = await _renew(_marker(FP_A))
    assert resp.status == 200
    assert body["signInMarker"] == _marker(FP_A)


@pytest.mark.parametrize("bad", ["", "ABCDEFABCDEFABCD", "abc", "g" * 16, 7, ["x"], "a" * 17])
@pytest.mark.asyncio
async def test_a_malformed_marker_is_a_bad_request(viewer, bad) -> None:
    resp, body = await _renew(bad)
    assert (resp.status, body["code"]) == (400, "INVALID_REQUEST")
    assert viewer.calls == []


@pytest.mark.asyncio
async def test_both_routes_pass_the_relay_domain_to_the_list(viewer) -> None:
    async with _client() as client:
        assert (await client.get("/api/relay-viewer/crews")).status == 200
        assert (await _mint(client)).status == 200
    assert viewer.crew_domains == ["relay.example.test", "relay.example.test"]


@pytest.mark.asyncio
async def test_with_no_domain_set_the_mint_pins_the_window_to_the_listed_host(viewer) -> None:
    viewer.tunnel.relay_viewer_relay_domain = ""
    async with _client() as client:
        resp = await _mint(client)
        body = await resp.json()
    assert resp.status == 200 and viewer.crew_domains == [""]
    assert (body["crewUrl"], body["relayDomain"]) == (CREW.crew_url, "relay.example.test")
    assert ("mint", EP) in viewer.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "crew_url",
    [
        f"https://{EP}.relay.example.test",
        f"https://{EP}.evil.example.test",
        f"https://{EP}.relay.example.test:8443",
        f"https://{EP}.us-east-1.relay.kiro.dev",
        f"https://{EP}.kiro-relay.eu-central-1.on.aws",
        f"https://{EP}.kiro-relay.eu-central-1.on.aws.evil.example.test",
    ],
)
@pytest.mark.parametrize(
    "domain",
    [
        "",
        "   ",
        "relay.example.test",
        "https://relay.example.test",
        "relay.example.test:443",
        "relay.example.test/",
        "relaý.example.test",
    ],
)
async def test_a_crew_is_kept_only_under_the_set_domain_or_at_its_own_name(
    viewer, monkeypatch, crew_url, domain
) -> None:
    on_domain = crew_url == f"https://{EP}.relay.example.test"
    # With no domain set, the address CPS lists is trusted when it has no port.
    in_zone = ":8443" not in crew_url
    kept = (not domain.strip() and in_zone) or (domain == "relay.example.test" and on_domain)
    viewer.tunnel.relay_viewer_relay_domain = domain
    raw = {"endpointId": EP, "crewUrl": crew_url, "name": "Build box", "crewType": "PERSONAL"}

    async def _list(url, bearer, *, crew_domain="", token_type="", profile_arn=""):
        viewer.calls.append(("list", bearer))
        crew = cps._crew_endpoint(raw, crew_domain)
        return cps.CrewList(() if crew is None else (crew,))

    monkeypatch.setattr(cps, "list_crew_endpoints", _list)
    async with _client() as client:
        listed = await client.get("/api/relay-viewer/crews")
        listed_body = await listed.json()
        minted = await _mint(client)
        minted_body = await minted.json()
    assert listed.status == 200
    assert [c["endpointId"] for c in listed_body["crews"]] == ([EP] if kept else [])
    if kept:
        # With no domain set, the window is pinned to the host the list gave.
        assert minted.status == 200 and minted_body["crewUrl"] == crew_url
        assert minted_body["relayDomain"] == crew_url.split(f"{EP}.", 1)[1]
    else:
        assert (minted.status, minted_body["code"]) == (403, "CREW_ACCESS_NOT_GRANTED")
        assert ("mint", EP) not in viewer.calls
    assert listed_body.get("code") != "NOT_SET_UP"
    assert minted_body.get("code") != "NOT_SET_UP"


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", [PROFILE, ""])
async def test_both_routes_list_with_the_sign_ins_profile(viewer, profile) -> None:
    viewer.profile_arn = profile
    async with _client() as client:
        assert (await client.get("/api/relay-viewer/crews")).status == 200
        assert (await _mint(client)).status == 200
    assert viewer.list_profile_arns == [profile, profile]


@pytest.mark.asyncio
async def test_a_personal_crew_is_minted_with_the_sign_ins_profile(viewer) -> None:
    viewer.profile_arn = PROFILE
    async with _client() as client:
        assert (await _mint(client)).status == 200
    assert viewer.profile_arns == [PROFILE]


@pytest.mark.asyncio
async def test_a_team_crew_is_minted_with_no_profile_even_when_signed_in_with_one(
    viewer, monkeypatch
) -> None:
    async def _list(url, bearer, *, crew_domain, token_type="", profile_arn=""):
        return cps.CrewList((TEAM_CREW,))

    monkeypatch.setattr(cps, "list_crew_endpoints", _list)
    viewer.profile_arn = PROFILE
    async with _client() as client:
        assert (await _mint(client)).status == 200
    assert viewer.profile_arns == [""]


@pytest.mark.asyncio
async def test_a_personal_crew_with_no_profile_is_minted_as_before_and_logs_no_value(
    viewer, caplog
) -> None:
    caplog.set_level(logging.INFO)
    async with _client() as client:
        assert (await _mint(client)).status == 200
    assert viewer.profile_arns == [""]
    assert "has no profile" in caplog.text
    for value in (BEARER, ACCESS, PROFILE):
        assert value not in caplog.text


#: A made-up profile ARN in a second region.
EU_PROFILE = "arn:aws:codewhisperer:eu-central-1:000000000000:profile/EXAMPLEPROFILE"
US_HOST = "https://management.us-east-1.kiro.dev/"


def _recording_urls(monkeypatch) -> list[tuple[str, str]]:
    """The fixture's list and mint fakes, recording the control plane URL each gets."""
    urls: list[tuple[str, str]] = []
    listed, minted = cps.list_crew_endpoints, cps.create_crew_access_token

    async def _list(url, bearer, *, crew_domain, token_type="", profile_arn=""):
        urls.append(("list", url))
        return await listed(url, bearer, crew_domain=crew_domain, token_type=token_type)

    async def _mint(url, bearer, endpoint_id, *, token_type="", profile_arn=""):
        urls.append(("mint", url))
        return await minted(
            url, bearer, endpoint_id, token_type=token_type, profile_arn=profile_arn
        )

    monkeypatch.setattr(cps, "list_crew_endpoints", _list)
    monkeypatch.setattr(cps, "create_crew_access_token", _mint)
    return urls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("profile", "credential", "host"),
    [
        (EU_PROFILE, "", "https://management.eu-central-1.kiro.dev/"),
        (PROFILE, "", US_HOST),
        # The test credential, and a sign-in with no profile of its own.
        ("", "secret://RELAY_VIEWER_TEST", US_HOST),
        ("", "", US_HOST),
        ("not-a-profile-arn", "", US_HOST),
    ],
)
async def test_an_empty_url_uses_the_built_in_control_plane_for_the_sign_ins_region(
    viewer, monkeypatch, profile, credential, host
) -> None:
    urls = _recording_urls(monkeypatch)
    viewer.tunnel.relay_viewer_cps_url = ""
    viewer.tunnel.relay_viewer_credential = credential
    viewer.profile_arn = profile
    async with _client() as client:
        listed = await client.get("/api/relay-viewer/crews")
        assert listed.status == 200
        assert (await _mint(client)).status == 200
    assert urls == [("list", host), ("list", host), ("mint", host)]


@pytest.mark.asyncio
async def test_a_set_url_still_wins_over_the_built_in_control_plane(viewer, monkeypatch) -> None:
    urls = _recording_urls(monkeypatch)
    viewer.profile_arn = EU_PROFILE
    async with _client() as client:
        assert (await client.get("/api/relay-viewer/crews")).status == 200
        assert (await _mint(client)).status == 200
    set_url = "https://cps.example.test"
    assert urls == [("list", set_url), ("list", set_url), ("mint", set_url)]


# Captured at import, before the fixture fakes them, so a test can use the real client.
_REAL_LIST, _REAL_MINT = cps.list_crew_endpoints, cps.create_crew_access_token


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "code", "posted"),
    [
        ("http://cps.example.invalid/", "NOT_SET_UP", []),
        ("", "UNREACHABLE", [US_HOST]),
    ],
    ids=["rejected", "empty"],
)
async def test_a_rejected_url_is_not_set_up_and_only_an_empty_one_uses_the_built_in_plane(
    viewer, monkeypatch, tmp_path, url, code, posted
) -> None:
    import json

    from kiro_crew.config.loader import KiroCrewConfig

    config_file = tmp_path / "config.json"
    tunnel = {
        "relay_viewer_enabled": True,
        "relay_viewer_cps_url": url,
        "relay_viewer_relay_domain": "relay.example.test",
    }
    config_file.write_text(json.dumps({"tunnel": tunnel}), encoding="utf-8")
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: config_file)
    monkeypatch.setattr(rv, "_load_tunnel", lambda: KiroCrewConfig.load().tunnel)
    monkeypatch.setattr(cps, "list_crew_endpoints", _REAL_LIST)
    monkeypatch.setattr(cps, "create_crew_access_token", _REAL_MINT)
    urls: list[str] = []

    async def _post(post_url, bearer, target, body, token_type):
        urls.append(post_url)
        raise cps.CpsRefused(cps.UNREACHABLE, "stand-in: no network in this test")

    monkeypatch.setattr(cps, "_post", _post)
    async with _client() as client:
        listed = await client.get("/api/relay-viewer/crews")
        minted = await _mint(client)
        assert (await listed.json())["code"] == code
        assert (await minted.json())["code"] == code
    assert urls == posted * 2


# The account the list was read for: {accountType, startUrl?}, only when proven.


async def _crews():
    async with _client() as client:
        resp = await client.get("/api/relay-viewer/crews")
        return resp, await resp.json()


@pytest.mark.asyncio
async def test_crews_names_the_account_when_both_reads_match(viewer) -> None:
    resp, body = await _crews()
    assert resp.status == 200 and body.get("account") == {"accountType": "BuilderId"}
    viewer.claims = dict(IDC)
    _, body = await _crews()
    assert body.get("account") == {"accountType": "IamIdentityCenter", "startUrl": START}
    assert viewer.claim_reads == 4, "one vault read before and one after each list"


@pytest.mark.asyncio
async def test_crews_leaves_the_account_out_when_the_sign_in_changes(viewer, monkeypatch) -> None:
    async def _list(url, bearer, *, crew_domain, token_type="", profile_arn=""):
        viewer.fingerprint = FP_B  # a sign-in landed during the list call
        return cps.CrewList((CREW,))

    monkeypatch.setattr(cps, "list_crew_endpoints", _list)
    resp, body = await _crews()
    assert resp.status == 200 and len(body["crews"]) == 1
    assert "account" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fingerprint", "claims"),
    [("", IDC), (FP_A, {"identity": "unknown_slot", "provider": ""})],
    ids=["signed-out", "unlabelled-slot"],
)
async def test_crews_leaves_the_account_out_when_nothing_is_known(
    viewer, fingerprint, claims
) -> None:
    viewer.fingerprint, viewer.claims = fingerprint, dict(claims)
    _, body = await _crews()
    assert "account" not in body


@pytest.mark.asyncio
async def test_a_signed_out_refusal_carries_no_account(viewer, monkeypatch) -> None:
    _refusing_list(viewer, monkeypatch, [401, 401])
    resp, body = await _crews()
    assert (resp.status, body["code"]) == (409, "SIGNED_OUT")
    assert "account" not in body


@pytest.mark.asyncio
async def test_the_test_credential_names_no_account(viewer) -> None:
    viewer.tunnel.relay_viewer_credential = "relay-viewer-test-credential"
    _, body = await _crews()
    assert "account" not in body
    assert viewer.claim_reads == 0, "the vault is never read for the test credential"


@pytest.mark.asyncio
async def test_the_start_url_is_capped_and_only_for_identity_center(viewer) -> None:
    viewer.claims = {**IDC, "start_url": "https://" + "a" * 300}
    _, body = await _crews()
    assert len((body.get("account") or {}).get("startUrl", "")) == 200
    viewer.claims = {"identity": "builder_id", "provider": "BuilderId", "start_url": START}
    _, body = await _crews()
    assert body.get("account") == {"accountType": "BuilderId"}


@pytest.mark.asyncio
async def test_the_account_never_reaches_the_log(viewer, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    viewer.claims = dict(IDC)
    _, body = await _crews()
    assert (body.get("account") or {}).get("startUrl") == START
    assert "relay viewer: crews -> 1 listed" in caplog.text
    for value in (START, "example-portal", "IamIdentityCenter", FP_A):
        assert value not in caplog.text


@pytest.mark.asyncio
async def test_a_forced_refresh_keeps_the_fingerprint_and_the_account(
    viewer, monkeypatch, tmp_path
) -> None:
    # The real vault read over a real store: a CPS 401, then one forced refresh
    # whose result is built by the real SSO-OIDC refresh and saved, as the
    # provider does. The refresh completes no sign-in, so the fingerprint holds.
    from datetime import datetime, timedelta, timezone

    from kiro_crew.auth import refresh
    from kiro_crew.auth.store import KasToken, TokenStore

    monkeypatch.setattr(bridge, "vault_identity_claims", _REAL_VAULT_CLAIMS)
    monkeypatch.setattr(bridge, "data_home", lambda: tmp_path)
    store = TokenStore(tmp_path)
    stored = KasToken(
        access_token=BEARER,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        provider="Enterprise",
        identity="identity_center",
        refresh_token="rt-1",
        profile_arn="arn:aws:kiro:us-east-1:1:profile/p1",
        region="us-east-1",
        client_id="client-1",
        client_secret="client-secret-1",
        extra={"start_url": START},
    )
    store.save(stored)
    store.bump_signin_generation()
    first = _REAL_VAULT_CLAIMS()[0]

    class _Answer:
        status = 200

        async def json(self):
            return {"accessToken": FRESH, "expiresIn": 3600, "refreshToken": "rt-2"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    session: Any = SimpleNamespace(post=lambda *a, **k: _Answer())
    seen = _refusing_list(viewer, monkeypatch, [401])

    async def _bearer(ref, rejected=""):
        seen.append(rejected)
        if not rejected:
            return BEARER, "", ""
        current = store.resolve()
        assert current is not None
        store.save(await refresh._refresh_sso_oidc(current, session=session))
        return FRESH, "", ""

    monkeypatch.setattr(rv, "_bearer", _bearer)
    resp, body = await _crews()
    assert resp.status == 200
    assert seen == ["", BEARER] and viewer.calls == [("list", BEARER), ("list", FRESH)]
    refreshed = store.resolve()
    assert refreshed is not None and refreshed.access_token == FRESH
    assert _REAL_VAULT_CLAIMS()[0] == first != ""
    assert body.get("account") == {"accountType": "IamIdentityCenter", "startUrl": START}


@pytest.mark.asyncio
async def test_a_workload_host_reads_no_person_sign_in(monkeypatch) -> None:
    """With KIROCREW_WORKLOAD_IDENTITY set, the viewer never builds the person's auth provider."""
    from kiro_crew import workload_identity
    from kiro_crew.auth import provider

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a person sign-in was read")

    monkeypatch.setenv(workload_identity.ENV_MODE, "spiffe")
    monkeypatch.setattr(provider, "KasAuthProvider", boom)
    monkeypatch.setattr(bridge, "default_token_store", boom)
    monkeypatch.setattr(bridge, "vault_identity_fingerprint", boom)
    with pytest.raises(cps.CpsRefused) as refused:
        await rv._bearer("")
    assert refused.value.code == cps.SIGNED_OUT
    assert await rv._sign_in_marker("") is None


@pytest.mark.asyncio
async def test_the_list_says_how_many_crews_the_relay_domain_alone_left_out(viewer) -> None:
    """Row 65: the dashboard is told, so it names the setting instead of showing none."""
    async with _client() as client:
        assert "offDomain" not in await (await client.get("/api/relay-viewer/crews")).json()
        viewer.off_domain = 3
        body = await (await client.get("/api/relay-viewer/crews")).json()
    assert body["offDomain"] == 3
    assert [c["endpointId"] for c in body["crews"]] == [EP]
