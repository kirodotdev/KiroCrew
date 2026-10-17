"""Identity-pinned dashboard sessions for peers reaching the gateway over a tailnet.

These tests pin the peer-keyed session pin, with whois mocked so no test runs a real
``tailscale`` binary: a verified peer binds its node or login identity instead of the
proxy's loopback address, a node-scoped session replayed from another node or a
login-scoped one from another login is denied with a reason naming the identity, a
tagged node is always pinned per node, the login allowlist is mandatory, a daemon
outage falls back to the address pin except for links that require a verified peer
and sessions already pinned to an identity, a request with no credential or a forged
forwarding header never reaches the daemon, mixed internal paths enforce the same pin,
a cookie that outlives a restart re-pins only for its original device, and a gateway
without tailnet trust keeps the plain address pin. A topic of the ``test_token_auth``
family: ``test_token_auth.py`` keeps the address pin and the middleware paths, and the
other siblings are ``test_token_auth_signing_secret.py``,
``test_token_auth_session_lifecycle.py``, ``test_token_auth_spa_shell.py`` and
``test_token_auth_credential_scope.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from token_auth_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _make_request,
    _ok_handler,
    clear_nonces,
)

from kiro_crew.dashboard.refresh_tokens import refresh_cookie_name, refresh_token_peer_key
from kiro_crew.dashboard.token_auth import (
    bind_token_ip,
    generate_token,
    mark_consumed,
    required_peer_key_unverified,
    token_auth_middleware,
)

# -- Identity-pinned sessions (RFC Phase 3) --
#
# Middleware-level behaviour of the peer-keyed pin: the tailnet branch is new,
# the ip: branch must be byte-for-byte the pre-peer behaviour. Whois is mocked
# at the tailnet._run_json seam — no test invokes a real tailscale binary.


def _tailnet_trust(**kw):
    from kiro_crew.dashboard.tailnet import TailnetTrust

    defaults = dict(trust_identity=True, allowed_logins=("you@example.com",), pin_scope="node")
    defaults.update(kw)
    return TailnetTrust(**defaults)


def _whois_payload(login: str = "you@example.com", node: str = "phone.tail.ts.net"):
    return {"Node": {"Name": node}, "UserProfile": {"LoginName": login}}


@pytest.fixture()
def _tailnet_env(monkeypatch):
    """Clear the whois cache and hand back a patch hook for its result."""
    from kiro_crew.dashboard import tailnet

    monkeypatch.setattr(tailnet, "IS_POSIX", True)
    tailnet._whois_cache.clear()

    def set_whois(result):
        mock = MagicMock(return_value=(result, False))
        monkeypatch.setattr(tailnet, "_run_json_detail", mock)
        return mock

    yield set_whois
    tailnet._whois_cache.clear()


def _peer_request(
    remote: str = "127.0.0.1",
    forwarded: str | None = "100.64.0.5",
    query: dict | None = None,
    cookies: dict | None = None,
):
    from multidict import CIMultiDict

    headers = CIMultiDict()
    if forwarded is not None:
        headers.add("X-Forwarded-For", forwarded)
    return _make_request(query=query, cookies=cookies, remote=remote, headers=headers)


@pytest.mark.asyncio
async def test_verified_peer_session_pins_to_identity_key(_tailnet_env) -> None:
    """A verified tailnet peer's session binds ts:node:<login>|<node>, not the
    tunnel's loopback address — and it is per-client, so posture must not
    report SHARED for it."""
    from kiro_crew.dashboard import token_auth as _ta

    _tailnet_env(_whois_payload())
    mw = token_auth_middleware(tailnet_trust=_tailnet_trust())
    token = generate_token("tsuser", ttl_seconds=300)
    resp = await mw(_peer_request(query={"token": token}), _ok_handler)
    assert resp.status == 200
    cookie = resp.cookies.get("mc_token_5476")
    assert cookie is not None
    key, _exp, proxied = _ta._state._peer_bindings[_ta._token_pin_key(cookie.value)]
    assert key == "ts:node:you@example.com|phone.tail.ts.net"
    assert proxied is False
    from kiro_crew.dashboard.token_auth import proxied_pin_observed

    assert proxied_pin_observed() is False


@pytest.mark.asyncio
async def test_node_scope_replay_from_another_node_denied_with_device_reason(
    _tailnet_env,
) -> None:
    """A node-pinned session replayed from a different node in the same tailnet
    is rejected, and the reason names DEVICE identity — a phone re-enrolling
    Tailscale must not surface as an unexplained 'IP mismatch'."""
    from kiro_crew.dashboard.token_auth import bind_token_peer

    _tailnet_env(_whois_payload(node="other-node.tail.ts.net"))
    mw = token_auth_middleware(tailnet_trust=_tailnet_trust())
    token = generate_token("tsuser", ttl_seconds=300)
    bind_token_peer(token, "ts:node:you@example.com|phone.tail.ts.net")
    mark_consumed(token)
    resp = await mw(_peer_request(cookies={"mc_token_5476": token}), _ok_handler)
    assert resp.status == 403
    assert b"device identity mismatch" in resp.body
    assert b"IP mismatch" not in resp.body


@pytest.mark.asyncio
async def test_login_scope_replay_from_another_node_is_accepted(_tailnet_env) -> None:
    """Under pin_scope login the same replay is accepted — the two scopes are
    separately pinned, so neither silently becomes the other."""
    from kiro_crew.dashboard.token_auth import bind_token_peer

    _tailnet_env(_whois_payload(node="other-node.tail.ts.net"))
    mw = token_auth_middleware(tailnet_trust=_tailnet_trust(pin_scope="login"))
    token = generate_token("tsuser", ttl_seconds=300)
    bind_token_peer(token, "ts:login:you@example.com")
    mark_consumed(token)
    resp = await mw(_peer_request(cookies={"mc_token_5476": token}), _ok_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_login_scope_replay_with_different_login_is_denied(_tailnet_env) -> None:
    from kiro_crew.dashboard.token_auth import bind_token_peer

    _tailnet_env(_whois_payload(login="other@example.com"))
    mw = token_auth_middleware(
        tailnet_trust=_tailnet_trust(
            allowed_logins=("you@example.com", "other@example.com"), pin_scope="login"
        )
    )
    token = generate_token("tsuser", ttl_seconds=300)
    bind_token_peer(token, "ts:login:you@example.com")
    mark_consumed(token)
    resp = await mw(_peer_request(cookies={"mc_token_5476": token}), _ok_handler)
    assert resp.status == 403
    assert b"peer identity mismatch" in resp.body


@pytest.mark.asyncio
async def test_verified_login_outside_allowlist_is_denied(_tailnet_env) -> None:
    """The allowlist is mandatory: a daemon-verified login the operator did not
    list is denied outright, valid token or not."""
    _tailnet_env(_whois_payload(login="mallory@example.com"))
    mw = token_auth_middleware(tailnet_trust=_tailnet_trust())
    token = generate_token("tsuser", ttl_seconds=300)
    resp = await mw(_peer_request(query={"token": token}), _ok_handler)
    assert resp.status == 403
    assert b"tailnet login not allowed" in resp.body


@pytest.mark.asyncio
async def test_tagged_node_session_not_replayable_from_second_tagged_node(
    _tailnet_env,
) -> None:
    """allowed_logins containing 'tagged-devices' under pin_scope login must not
    let one tagged node replay another's session: the pin is forced to node
    scope for tagged nodes."""
    from kiro_crew.dashboard.token_auth import bind_token_peer

    _tailnet_env(_whois_payload(login="tagged-devices", node="ci-b.tail.ts.net"))
    mw = token_auth_middleware(
        tailnet_trust=_tailnet_trust(allowed_logins=("tagged-devices",), pin_scope="login")
    )
    token = generate_token("ci", ttl_seconds=300)
    # Session originally bound on tagged node A (forced node scope).
    bind_token_peer(token, "ts:node:tagged-devices|ci-a.tail.ts.net")
    mark_consumed(token)
    resp = await mw(_peer_request(cookies={"mc_token_5476": token}), _ok_handler)
    assert resp.status == 403
    assert b"device identity mismatch" in resp.body


@pytest.mark.asyncio
async def test_xff_injection_from_non_loopback_peer_gets_ip_pin(_tailnet_env) -> None:
    """A remote client spraying X-Forwarded-For never resolves a peer: the
    session pins to its real address and the daemon is never consulted."""
    from kiro_crew.dashboard import token_auth as _ta

    whois = _tailnet_env(_whois_payload())
    mw = token_auth_middleware(tailnet_trust=_tailnet_trust())
    token = generate_token("attacker", ttl_seconds=300)
    resp = await mw(_peer_request(remote="203.0.113.7", query={"token": token}), _ok_handler)
    assert resp.status == 200
    cookie = resp.cookies.get("mc_token_5476")
    assert _ta._state._peer_bindings[_ta._token_pin_key(cookie.value)][0] == "ip:203.0.113.7"
    whois.assert_not_called()


@pytest.mark.asyncio
async def test_daemon_failure_degrades_to_token_ip_path(_tailnet_env) -> None:
    """Daemon absent/down/timeout → request proceeds on today's token+IP path:
    fail-closed on identity, fail-open on availability."""
    from kiro_crew.dashboard import token_auth as _ta

    _tailnet_env(None)  # every whois outcome collapses to None at this seam
    mw = token_auth_middleware(tailnet_trust=_tailnet_trust())
    token = generate_token("tsuser", ttl_seconds=300)
    resp = await mw(_peer_request(query={"token": token}), _ok_handler)
    assert resp.status == 200
    cookie = resp.cookies.get("mc_token_5476")
    key, _exp, proxied = _ta._state._peer_bindings[_ta._token_pin_key(cookie.value)]
    assert key == "ip:127.0.0.1"
    assert proxied is True  # same-host proxy pin — posture reports SHARED


@pytest.mark.asyncio
async def test_require_peer_link_refuses_daemon_failure_before_cookie_exchange(
    _tailnet_env,
) -> None:
    """A persistent QR link must not become a shared loopback session when
    whois is unavailable during its first exchange."""
    _tailnet_env(None)
    mw = token_auth_middleware(tailnet_trust=_tailnet_trust())
    token = generate_token("tsuser", ttl_seconds=300, extra={"require_peer": "1"})
    resp = await mw(_peer_request(query={"token": token}), _ok_handler)
    assert resp.status == 403
    assert b"tailnet identity unverified" in resp.body
    assert resp.cookies.get("mc_token_5476") is None


@pytest.mark.asyncio
async def test_require_peer_link_exchanges_for_verified_allowed_peer(_tailnet_env) -> None:
    """The fail-closed claim still permits the intended verified peer and the
    exchanged access cookie keeps the identity-required claim."""
    from kiro_crew.dashboard import token_auth as _ta

    _tailnet_env(_whois_payload())
    mw = token_auth_middleware(tailnet_trust=_tailnet_trust())
    token = generate_token("tsuser", ttl_seconds=300, extra={"require_peer": "1"})
    resp = await mw(_peer_request(query={"token": token}), _ok_handler)
    assert resp.status == 200
    cookie = resp.cookies.get("mc_token_5476")
    assert cookie is not None
    assert _ta.requires_verified_peer_unverified(cookie.value) is True
    expected = "ts:node:you@example.com|phone.tail.ts.net"
    assert required_peer_key_unverified(cookie.value) == expected
    assert _ta._state._peer_bindings[_ta._token_pin_key(cookie.value)][0] == expected
    refresh = resp.cookies.get(refresh_cookie_name("5476"))
    assert refresh is not None
    assert refresh_token_peer_key(refresh.value) == expected


@pytest.mark.asyncio
async def test_non_tailscale_tunnel_behaviour_is_unchanged(_tailnet_env) -> None:
    """Loopback peer + XFF with identity trust OFF: byte-for-byte today's
    behaviour — pin ip:127.0.0.1, posture SHARED, no daemon call."""
    from kiro_crew.dashboard import token_auth as _ta
    from kiro_crew.dashboard.token_auth import proxied_pin_observed

    whois = _tailnet_env(_whois_payload())
    mw = token_auth_middleware()  # tailnet_trust=None: every non-Tailscale setup
    token = generate_token("tunneluser", ttl_seconds=300)
    resp = await mw(_peer_request(query={"token": token}), _ok_handler)
    assert resp.status == 200
    cookie = resp.cookies.get("mc_token_5476")
    key, _exp, proxied = _ta._state._peer_bindings[_ta._token_pin_key(cookie.value)]
    assert key == "ip:127.0.0.1"
    assert proxied is True
    assert proxied_pin_observed() is True
    whois.assert_not_called()


@pytest.mark.asyncio
async def test_plain_ip_mismatch_reason_is_preserved(_tailnet_env) -> None:
    """The address-pin denial keeps its historical reason string."""
    mw = token_auth_middleware()
    token = generate_token("user", ttl_seconds=300)
    bind_token_ip(token, "10.0.0.1")
    mark_consumed(token)
    req = _make_request(cookies={"mc_token_5476": token}, remote="192.168.1.9")
    resp = await mw(req, _ok_handler)
    assert resp.status == 403
    assert b"IP mismatch" in resp.body


# -- Review-round hardening (adversarial fleet findings) --


@pytest.mark.asyncio
async def test_credential_less_request_never_reaches_the_daemon(_tailnet_env) -> None:
    """An unauthenticated local caller spraying X-Forwarded-For on a static
    path must not be able to force whois spawns: resolution is gated on the
    request presenting a credential (query token or mc_* cookie)."""
    whois = _tailnet_env(_whois_payload())
    mw = token_auth_middleware(tailnet_trust=_tailnet_trust())
    resp = await mw(_peer_request(query={}, cookies={}), _ok_handler)
    assert resp.status == 403  # no token — denied as today
    whois.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_cookie_alone_still_reaches_the_allowlist_deny(_tailnet_env) -> None:
    """The credential gate keys on PRESENCE (including the refresh cookie), so
    the allowlist deny still covers the /api/auth/refresh middleware bypass."""
    _tailnet_env(_whois_payload(login="mallory@example.com"))
    mw = token_auth_middleware(tailnet_trust=_tailnet_trust())
    req = _peer_request(cookies={"mc_refresh_5476": "whatever"})
    req.path = "/api/auth/refresh"
    req.method = "POST"
    resp = await mw(req, _ok_handler)
    assert resp.status == 403
    assert b"tailnet login not allowed" in resp.body


@pytest.mark.asyncio
async def test_internal_mixed_path_enforces_the_peer_pin(_tailnet_env) -> None:
    """A node-pinned session replayed from another node against a mixed
    internal path (/api/spawn-style cookie auth) is denied — the internal
    branches must not skip the pin the main flow enforces."""
    from kiro_crew.dashboard.token_auth import bind_token_peer

    _tailnet_env(_whois_payload(node="other-node.tail.ts.net"))
    mw = token_auth_middleware(
        mixed_internal_paths=frozenset({"/api/spawn"}),
        tailnet_trust=_tailnet_trust(),
    )
    token = generate_token("tsuser", ttl_seconds=300)
    bind_token_peer(token, "ts:node:you@example.com|phone.tail.ts.net")
    mark_consumed(token)
    req = _peer_request(cookies={"mc_token_5476": token})
    req.path = "/api/spawn"
    resp = await mw(req, _ok_handler)
    assert resp.status == 403
    assert b"device identity mismatch" in resp.body


@pytest.mark.asyncio
async def test_internal_mixed_path_accepts_the_matching_peer(_tailnet_env) -> None:
    from kiro_crew.dashboard.token_auth import bind_token_peer

    _tailnet_env(_whois_payload())
    mw = token_auth_middleware(
        mixed_internal_paths=frozenset({"/api/spawn"}),
        tailnet_trust=_tailnet_trust(),
    )
    token = generate_token("tsuser", ttl_seconds=300)
    bind_token_peer(token, "ts:node:you@example.com|phone.tail.ts.net")
    mark_consumed(token)
    req = _peer_request(cookies={"mc_token_5476": token})
    req.path = "/api/spawn"
    resp = await mw(req, _ok_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_identity_pinned_session_with_daemon_down_names_unavailability(
    _tailnet_env,
) -> None:
    """A ts:-pinned session checked while NO peer resolves is denied with a
    reason naming the unverified identity — not 'device identity mismatch',
    which would tell the user their device changed when the daemon blipped."""
    from kiro_crew.dashboard.token_auth import bind_token_peer

    _tailnet_env(None)  # daemon unreachable
    mw = token_auth_middleware(tailnet_trust=_tailnet_trust())
    token = generate_token("tsuser", ttl_seconds=300)
    bind_token_peer(token, "ts:node:you@example.com|phone.tail.ts.net")
    mark_consumed(token)
    resp = await mw(_peer_request(cookies={"mc_token_5476": token}), _ok_handler)
    assert resp.status == 403
    assert b"tailnet identity unverified" in resp.body
    assert b"device identity mismatch" not in resp.body


@pytest.mark.asyncio
async def test_restart_first_use_repins_verified_peer_cookie(_tailnet_env) -> None:
    """After a gateway restart the in-memory binding map is empty, so a
    surviving cookie is unbound. The first request carrying a VERIFIED peer
    identity re-claims the pin; a replay from another node is denied again."""
    from kiro_crew.dashboard import token_auth as _ta

    set_whois = _tailnet_env
    set_whois(_whois_payload())
    mw = token_auth_middleware(tailnet_trust=_tailnet_trust())
    token = generate_token("tsuser", ttl_seconds=300)
    mark_consumed(token)
    # No bind_token_peer call: simulates the post-restart unbound state.
    resp = await mw(_peer_request(cookies={"mc_token_5476": token}), _ok_handler)
    assert resp.status == 200
    key, _exp, _proxied = _ta._state._peer_bindings[_ta._token_pin_key(token)]
    assert key == "ts:node:you@example.com|phone.tail.ts.net"
    # Same cookie replayed from a different node (different tailnet address,
    # so the whois cache cannot serve the first node's answer) is rejected.
    set_whois(_whois_payload(node="other-node.tail.ts.net"))
    resp2 = await mw(
        _peer_request(forwarded="100.64.0.6", cookies={"mc_token_5476": token}), _ok_handler
    )
    assert resp2.status == 403
    assert b"device identity mismatch" in resp2.body


@pytest.mark.asyncio
async def test_restart_require_peer_cookie_refuses_unverified_first_use(
    _tailnet_env,
) -> None:
    """After restart, an unbound persistent cookie waits for verified whois;
    it cannot claim the proxy's loopback identity during a daemon outage."""
    from kiro_crew.dashboard import token_auth as _ta

    _tailnet_env(None)
    mw = token_auth_middleware(tailnet_trust=_tailnet_trust())
    token = generate_token("tsuser", ttl_seconds=300, extra={"require_peer": "1"})
    mark_consumed(token)
    # No bind_token_peer call: simulates the post-restart in-memory state.
    resp = await mw(_peer_request(cookies={"mc_token_5476": token}), _ok_handler)
    assert resp.status == 403
    assert b"tailnet identity unverified" in resp.body
    assert not _ta._state.has_binding(token)


@pytest.mark.asyncio
async def test_restart_signed_require_peer_cookie_rehydrates_only_for_original_device(
    _tailnet_env,
) -> None:
    """The signed original-device claim, not first arrival, rebuilds the hot pin."""
    from kiro_crew.dashboard import token_auth as _ta

    expected = "ts:node:you@example.com|phone.tail.ts.net"
    set_whois = _tailnet_env
    set_whois(_whois_payload())
    mw = token_auth_middleware(tailnet_trust=_tailnet_trust())
    token = generate_token(
        "tsuser",
        ttl_seconds=300,
        peer_key=expected,
        extra={"require_peer": "1"},
        register_nonce=False,
    )

    assert not _ta._state.has_binding(token)
    response = await mw(_peer_request(cookies={"mc_token_5476": token}), _ok_handler)
    assert response.status == 200
    assert _ta._state._peer_bindings[_ta._token_pin_key(token)][0] == expected

    # Simulate another restart, then let a different but still allowlisted node
    # arrive first.  It cannot claim the empty in-memory map.
    _ta._state._peer_bindings.pop(_ta._token_pin_key(token), None)
    set_whois(_whois_payload(node="other-node.tail.ts.net"))
    replay = await mw(
        _peer_request(forwarded="100.64.0.6", cookies={"mc_token_5476": token}),
        _ok_handler,
    )
    assert replay.status == 403
    assert b"device identity mismatch" in replay.body
    assert not _ta._state.has_binding(token)


@pytest.mark.asyncio
async def test_restart_legacy_claimless_require_peer_cookie_cannot_claim_device(
    _tailnet_env,
) -> None:
    """Pre-fix cookies must re-scan instead of accepting the first allowed node."""
    from kiro_crew.dashboard import token_auth as _ta

    _tailnet_env(_whois_payload())
    mw = token_auth_middleware(tailnet_trust=_tailnet_trust())
    token = generate_token(
        "tsuser", ttl_seconds=300, extra={"require_peer": "1"}, register_nonce=False
    )
    response = await mw(_peer_request(cookies={"mc_token_5476": token}), _ok_handler)
    assert response.status == 403
    assert b"tailnet session device binding missing" in response.body
    assert not _ta._state.has_binding(token)


@pytest.mark.asyncio
async def test_claimless_require_peer_link_cannot_enroll_on_mixed_internal_path(
    _tailnet_env,
) -> None:
    """Only the main QR exchange path may turn a claimless link into a session."""
    from kiro_crew.dashboard import token_auth as _ta

    _tailnet_env(_whois_payload())
    mw = token_auth_middleware(
        mixed_internal_paths=frozenset({"/api/spawn"}),
        tailnet_trust=_tailnet_trust(),
    )
    token = generate_token("tsuser", ttl_seconds=300, extra={"require_peer": "1"})
    request = _peer_request(query={"token": token})
    request.path = "/api/spawn"
    response = await mw(request, _ok_handler)
    assert response.status == 403
    assert b"tailnet session device binding missing" in response.body
    assert not response.cookies
    assert not _ta._state.has_binding(token)


@pytest.mark.asyncio
async def test_signed_login_scope_survives_operator_pin_scope_change(_tailnet_env) -> None:
    """An issued login-scoped session keeps its signed scope after config changes."""
    from kiro_crew.dashboard import token_auth as _ta

    expected = "ts:login:you@example.com"
    _tailnet_env(_whois_payload(node="replacement-phone.tail.ts.net"))
    # Today's config is node-scoped, but the signed credential was issued with
    # login scope and therefore remains usable after a phone re-enrolment.
    mw = token_auth_middleware(tailnet_trust=_tailnet_trust(pin_scope="node"))
    token = generate_token(
        "tsuser",
        ttl_seconds=300,
        peer_key=expected,
        extra={"require_peer": "1"},
        register_nonce=False,
    )
    response = await mw(_peer_request(cookies={"mc_token_5476": token}), _ok_handler)
    assert response.status == 200
    assert _ta._state._peer_bindings[_ta._token_pin_key(token)][0] == expected


@pytest.mark.asyncio
async def test_restart_repin_covers_internal_mixed_paths(_tailnet_env) -> None:
    """The first-use re-pin lives in the shared check, so an unbound cookie
    presented on a mixed internal route also claims the pin — a later replay
    from another node is denied there too."""
    from kiro_crew.dashboard import token_auth as _ta

    set_whois = _tailnet_env
    set_whois(_whois_payload())
    mw = token_auth_middleware(
        mixed_internal_paths=frozenset({"/api/spawn"}), tailnet_trust=_tailnet_trust()
    )
    token = generate_token("tsuser", ttl_seconds=300)
    mark_consumed(token)
    req = _peer_request(cookies={"mc_token_5476": token})
    req.path = "/api/spawn"
    resp = await mw(req, _ok_handler)
    assert resp.status == 200
    assert (
        _ta._state._peer_bindings[_ta._token_pin_key(token)][0]
        == "ts:node:you@example.com|phone.tail.ts.net"
    )
    set_whois(_whois_payload(node="other-node.tail.ts.net"))
    req2 = _peer_request(forwarded="100.64.0.6", cookies={"mc_token_5476": token})
    req2.path = "/api/spawn"
    resp2 = await mw(req2, _ok_handler)
    assert resp2.status == 403
    assert b"device identity mismatch" in resp2.body


@pytest.mark.asyncio
async def test_restart_unbound_cookie_without_peer_keeps_todays_semantics(
    _tailnet_env,
) -> None:
    """No verified peer (trust off): an unbound cookie stays unbound — the
    pre-identity restart behaviour is byte-for-byte preserved."""
    from kiro_crew.dashboard import token_auth as _ta

    _tailnet_env(_whois_payload())
    mw = token_auth_middleware()  # trust off
    token = generate_token("user", ttl_seconds=300)
    mark_consumed(token)
    resp = await mw(_make_request(cookies={"mc_token_5476": token}), _ok_handler)
    assert resp.status == 200
    assert not _ta._state.has_binding(token)
