"""Shared Crew trusted attribution through the real auth middleware.

Every request here goes through ``token_auth_middleware`` with real signed
tokens; the tailnet daemon is mocked at ``tailnet._run_json_detail`` (the same
seam the tailnet tests use), so no test runs a real ``tailscale`` binary. The
handler reports the actor the middleware published, which is all any Shared
Crew handler can see.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from multidict import CIMultiDict

from kiro_crew.dashboard import tailnet
from kiro_crew.dashboard.tailnet import TailnetTrust
from kiro_crew.dashboard.token_auth import generate_token, token_auth_middleware
from kiro_crew.shared_crew import identity, people
from kiro_crew.shared_crew.contracts import (
    ACTOR_REQUEST_KEY,
    ActorContext,
    PrincipalKind,
    Provenance,
    Transport,
)

TRUST = TailnetTrust(
    trust_identity=True,
    allowed_logins=("alice@example.com", "bob@example.com", "tagged-devices"),
    pin_scope="login",
)
ADDRS = {
    "100.64.0.5": ("alice@example.com", "alice-laptop.tail.ts.net."),
    "100.64.0.6": ("alice@example.com", "alice-phone.tail.ts.net."),
    "100.64.0.7": ("bob@example.com", "bob-laptop.tail.ts.net."),
    "100.64.0.8": ("tagged-devices", "ci-runner-1.tail.ts.net."),
    "100.64.0.9": ("tagged-devices", "ci-runner-2.tail.ts.net."),
    "100.64.0.10": ("carol@example.com", "carol.tail.ts.net."),
}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, _floor_monkeypatch):
    import kiro_crew.dashboard.revocation_gen as rg
    import kiro_crew.dashboard.token_auth as ta

    monkeypatch = _floor_monkeypatch
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr(rg, "_gen", 0)
    monkeypatch.setattr(ta, "_revoked_store_singleton", None)
    monkeypatch.setattr(tailnet, "IS_POSIX", True)
    ta._state.clear_all()
    ta._app_perms_cache.clear()
    tailnet._whois_cache.clear()
    people.reset_for_tests()
    calls: list[str] = []

    def whois(argv: list[str]) -> tuple[Any, bool]:
        addr = argv[-1]
        calls.append(addr)
        hit = ADDRS.get(addr)
        if hit is None:
            return None, False
        return {"Node": {"Name": hit[1]}, "UserProfile": {"LoginName": hit[0]}}, False

    monkeypatch.setattr(tailnet, "_run_json_detail", MagicMock(side_effect=whois))
    # The loopback client stands in for the daemon behind ``tailscale serve``.
    monkeypatch.setattr(tailnet, "loopback_client_is_daemon", lambda *_a: True)
    yield calls
    ta._state.clear_all()
    tailnet._whois_cache.clear()
    people.reset_for_tests()


def _request(
    *,
    remote: str = "127.0.0.1",
    token: str = "",
    headers: list[tuple[str, str]] | None = None,
    path: str = "/api/whoami",
) -> Any:
    """A request double that stores items, so the published actor is readable."""
    req = MagicMock(spec=web.Request)
    store: dict[str, Any] = {}
    req.__setitem__.side_effect = store.__setitem__
    req.__getitem__.side_effect = store.__getitem__
    req.__contains__.side_effect = store.__contains__
    req.get.side_effect = store.get
    req.path = path
    req.query = {"token": token} if token else {}
    req.cookies = {}
    req.remote = remote
    req.headers = CIMultiDict(headers or [])
    req.method = "GET"
    req.store = store
    return req


async def _handler(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def _actor_after(mw: Any, req: Any) -> tuple[int, ActorContext | None]:
    resp = await mw(req, _handler)
    return resp.status, req.store.get(ACTOR_REQUEST_KEY)


def _token(user: str = "local-app", **kw: Any) -> str:
    return generate_token(user, ttl_seconds=300, **kw)


# -- Precedence, as a pure function ---------------------------------------- #


def _peer(login: str, node: str) -> tailnet.ForwardedPeer:
    return tailnet.ForwardedPeer(login=login, node=node, address="100.64.0.5")


def test_precedence_app_then_internal_then_serve_then_direct_then_local():
    base = dict(token_subject="local-app", remote="127.0.0.1", forwarded=False)
    alice = _peer("alice@example.com", "a")
    assert (
        identity.derive_principal(
            internal=True, app="notes", serve_peer=alice, direct_peer=None, **base
        ).subject
        == "app:notes"
    )
    assert (
        identity.derive_principal(
            internal=True, app="", serve_peer=alice, direct_peer=None, **base
        ).subject
        == "internal"
    )
    serve = identity.derive_principal(
        internal=False, app="", serve_peer=alice, direct_peer=None, **base
    )
    assert serve.provenance is Provenance.TAILSCALE_SERVE
    direct = identity.derive_principal(
        internal=False, app="", serve_peer=None, direct_peer=alice, **base
    )
    assert direct.provenance is Provenance.TAILSCALE_DIRECT and direct.id == serve.id
    local = identity.derive_principal(
        internal=False, app="", serve_peer=None, direct_peer=None, **base
    )
    assert local.provenance is Provenance.LOCAL_OPERATOR and local.subject == "local-app"


def test_loopback_alone_is_not_the_operator_when_a_forwarding_header_is_present():
    p = identity.derive_principal(
        internal=False,
        app="",
        token_subject="local-app",
        serve_peer=None,
        direct_peer=None,
        remote="127.0.0.1",
        forwarded=True,
    )
    assert p.is_unknown


def test_a_non_loopback_unverified_peer_is_unknown():
    p = identity.derive_principal(
        internal=False,
        app="",
        token_subject="local-app",
        serve_peer=None,
        direct_peer=None,
        remote="192.168.1.20",
        forwarded=False,
    )
    assert p.is_unknown


# -- Through the real middleware ------------------------------------------- #


@pytest.mark.asyncio
async def test_two_people_behind_serve_get_distinct_principals():
    mw = token_auth_middleware(tailnet_trust=TRUST)
    _, a = await _actor_after(
        mw, _request(token=_token(), headers=[("X-Forwarded-For", "100.64.0.5")])
    )
    _, b = await _actor_after(
        mw, _request(token=_token(), headers=[("X-Forwarded-For", "100.64.0.7")])
    )
    assert a is not None and b is not None
    assert a.principal.kind is PrincipalKind.HUMAN and b.principal.kind is PrincipalKind.HUMAN
    assert a.principal.provenance is Provenance.TAILSCALE_SERVE
    assert a.principal.id != b.principal.id
    assert a.principal.display.login == "alice@example.com"
    # The token subject is the same for both: it is not the person.
    assert a.auth.token_subject == b.auth.token_subject == "local-app"


@pytest.mark.asyncio
async def test_one_person_on_two_devices_and_tabs_is_one_principal():
    mw = token_auth_middleware(tailnet_trust=TRUST)
    ids = set()
    for addr in ("100.64.0.5", "100.64.0.6", "100.64.0.5"):
        status, actor = await _actor_after(
            mw, _request(token=_token(), headers=[("X-Forwarded-For", addr)])
        )
        assert status == 200 and actor is not None
        ids.add(actor.principal.id)
    assert len(ids) == 1
    rows = people.people_view(actor.principal, [])["people"]
    assert len(rows) == 1 and rows[0]["devices"] == 2 and rows[0]["is_viewer"] is True


@pytest.mark.asyncio
async def test_tagged_nodes_are_distinct_machines():
    mw = token_auth_middleware(tailnet_trust=TRUST)
    _, one = await _actor_after(
        mw, _request(token=_token(), headers=[("X-Forwarded-For", "100.64.0.8")])
    )
    _, two = await _actor_after(
        mw, _request(token=_token(), headers=[("X-Forwarded-For", "100.64.0.9")])
    )
    assert one.principal.kind is PrincipalKind.MACHINE
    assert two.principal.kind is PrincipalKind.MACHINE
    assert one.principal.id != two.principal.id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        [("Tailscale-User-Login", "bob@example.com"), ("X-Forwarded-For", "100.64.0.5")],
        [("X-Forwarded-For", "100.64.0.7, 100.64.0.5")],
        [("X-Forwarded-For", "100.64.0.7"), ("X-Forwarded-For", "100.64.0.5")],
    ],
    ids=["disagreeing-login-header", "comma-chain", "repeated-xff"],
)
async def test_unattributable_tailnet_forwarding_is_refused_never_a_person(headers):
    """A forwarded tailnet request that names no allowed person is refused."""
    mw = token_auth_middleware(tailnet_trust=TRUST)
    status, actor = await _actor_after(mw, _request(token=_token(), headers=headers))
    assert status == 403
    assert actor is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [[("X-Forwarded-For", "10.0.0.7")], [("Forwarded", "for=100.64.0.7")]],
    ids=["non-tailnet", "rfc7239-only"],
)
async def test_non_tailnet_forwarding_is_unknown_never_a_person(headers):
    mw = token_auth_middleware(tailnet_trust=TRUST)
    status, actor = await _actor_after(mw, _request(token=_token(), headers=headers))
    assert status == 200, "forwarding outside the tailnet is not the allowlist's business"
    assert actor is not None and actor.principal.is_unknown


@pytest.mark.asyncio
async def test_identity_headers_and_query_fields_are_never_read():
    mw = token_auth_middleware(tailnet_trust=TRUST)
    req = _request(
        token=_token(),
        headers=[
            ("X-Shared-Crew-Principal", "p_" + "0" * 32),
            ("X-User", "bob@example.com"),
            ("X-Forwarded-User", "bob@example.com"),
            ("X-Forwarded-Email", "bob@example.com"),
        ],
    )
    req.query = {"token": req.query["token"], "principal": "bob", "creator": "mine"}
    _, actor = await _actor_after(mw, req)
    # A loopback socket with no forwarding header is the token holder, not Bob.
    assert actor.principal.provenance is Provenance.LOCAL_OPERATOR
    assert actor.principal.subject == "local-app"


@pytest.mark.asyncio
async def test_existing_rejections_are_preserved_and_publish_nothing():
    mw = token_auth_middleware(tailnet_trust=TRUST)
    # No credential at all.
    status, actor = await _actor_after(mw, _request())
    assert status in (401, 403) and actor is None
    # A verified login outside the allowlist is still denied.
    status, actor = await _actor_after(
        mw, _request(token=_token(), headers=[("X-Forwarded-For", "100.64.0.10")])
    )
    assert status in (401, 403) and actor is None
    # A tampered token is still denied.
    status, actor = await _actor_after(mw, _request(token=_token() + "x"))
    assert status in (401, 403) and actor is None


@pytest.mark.asyncio
async def test_a_failing_identity_lookup_publishes_unknown_and_admits_only_what_auth_admitted(
    monkeypatch,
):
    async def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("daemon exploded")

    monkeypatch.setattr(identity, "_derive_actor", boom)
    mw = token_auth_middleware(tailnet_trust=TRUST)
    status, actor = await _actor_after(mw, _request(token=_token()))
    assert status == 200 and actor is not None and actor.principal.is_unknown
    status, actor = await _actor_after(mw, _request())
    assert status in (401, 403) and actor is None


@pytest.mark.asyncio
async def test_an_app_token_is_the_apps_system_principal(monkeypatch):
    import kiro_crew.dashboard.token_auth as ta

    monkeypatch.setattr(ta, "_enforce_app_scope", lambda *_a, **_k: None)

    async def _no_prime(_app: str) -> None:
        return None

    monkeypatch.setattr(ta, "_prime_app_api_allowlist", _no_prime)
    mw = token_auth_middleware(tailnet_trust=TRUST)
    _, actor = await _actor_after(mw, _request(token=_token("notes", app="notes")))
    assert actor.principal.kind is PrincipalKind.SYSTEM
    assert actor.principal.subject == "app:notes"


# -- Direct tailnet peer ---------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_direct_tailnet_peer_is_resolved_by_the_daemon_from_the_socket_address():
    req = _request(remote="100.64.0.7")
    req.store["user"] = "local-app"
    actor = await identity.publish_actor(req, trust=TRUST, serve_peer=None)
    assert actor.principal.provenance is Provenance.TAILSCALE_DIRECT
    assert actor.principal.display.login == "bob@example.com"
    assert req.store[ACTOR_REQUEST_KEY] is actor


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "remote,headers,trust",
    [
        ("100.64.0.7", [("X-Forwarded-For", "100.64.0.5")], TRUST),
        ("100.64.0.7", [("Forwarded", "for=100.64.0.5")], TRUST),
        ("100.64.0.7", [("X-Real-IP", "100.64.0.5")], TRUST),
        ("100.64.0.7", [("Tailscale-User-Login", "alice@example.com")], TRUST),
        ("100.64.0.99", [], TRUST),
        ("192.168.1.7", [], TRUST),
        ("100.64.0.7", [], TailnetTrust()),
    ],
    ids=[
        "proxy-on-another-node",
        "proxy-forwarded-header",
        "proxy-real-ip",
        "disagreeing-header",
        "unknown-peer",
        "lan",
        "trust-off",
    ],
)
async def test_a_direct_peer_without_daemon_proof_is_unknown(remote, headers, trust, _isolated):
    req = _request(remote=remote, headers=headers)
    actor = await identity.publish_actor(req, trust=trust, serve_peer=None)
    assert actor.principal.is_unknown
    if trust is not TRUST or remote == "192.168.1.7":
        assert remote not in _isolated, "no daemon call without trust or for a non-tailnet peer"


@pytest.mark.asyncio
async def test_a_direct_peer_outside_the_allowlist_is_attributed_but_not_newly_denied():
    """Direct-peer resolution is attribution only; the allowlist keeps its scope."""
    req = _request(remote="100.64.0.10")
    actor = await identity.publish_actor(req, trust=TRUST, serve_peer=None)
    assert actor.principal.display.login == "carol@example.com"


# -- Actor context fields --------------------------------------------------- #


@pytest.mark.asyncio
async def test_websocket_upgrade_and_correlation_header():
    req = _request(headers=[("Upgrade", "websocket"), ("X-Correlation-Id", "retry-42")])
    req.store["user"] = "local-app"
    actor = await identity.publish_actor(req, trust=None, serve_peer=None)
    assert actor.transport is Transport.WEBSOCKET
    assert actor.correlation_id == "retry-42"
    bad = _request(headers=[("X-Correlation-Id", "a b\nc")])
    actor2 = await identity.publish_actor(bad, trust=None, serve_peer=None)
    assert actor2.correlation_id == actor2.request_id


def test_a_request_without_a_published_actor_reads_as_unknown():
    req = _request()
    assert identity.actor_of(req).principal.is_unknown
    assert identity.viewer_principal(object()).is_unknown


def test_the_public_principal_carries_no_subject_or_issuer():
    p = identity.tailnet_principal("alice@example.com", "n", Provenance.TAILSCALE_SERVE)
    assert set(p.to_public_dict()) == {"id", "kind", "display"}
    assert "alice" not in p.id
