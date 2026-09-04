"""Refresh chains are bound to the tailnet peer that opened them (issue #2417).

Phase 3 pins ACCESS sessions to a daemon-verified tailnet peer, and PR #2411 made
rotation carry that pin forward onto the replacement access token. What it did NOT
do was bind the refresh CHAIN itself for ordinary sessions: the peer-bound
rotation mechanism (``require_peer`` / ``peer_key``) was armed for exactly one
producer, the persistent QR phone session. Every other Phase-3 session minted an
UNBOUND chain, so a refresh cookie stolen from allowed node A and replayed from
allowed node B rotated cleanly and re-pinned the fresh access token to B — the
theft path this module pins shut.

What is asserted here:

* the exploit, end to end: a chain opened on node A cannot be rotated from node
  B, and the refusal neither consumes the jti nor revokes the chain (a refusal is
  not a theft signal — see ``api_auth_refresh``);
* the mint side: an ordinary Phase-3 exchange now writes the binding onto BOTH
  the signed refresh token and the server-side chain record;
* roaming: ``pin_scope: "login"`` still lets the same identity rotate from a
  different device, which is the operator's roaming knob and the resolution of
  the availability tradeoff raised on the issue;
* the opt-out: ``bind_refresh_chains: false`` restores the pre-change unbound
  chain for an operator who needs cross-node roaming at node scope;
* migration: a chain with neither a signed claim nor a persisted record (every
  chain that exists on an upgraded install) keeps today's unbound semantics;
* the server-side record as an independent authority: a chain recorded as
  peer-bound is refused for another peer even when the presented token carries no
  claim at all, so a future mint path that forgets the claim fails closed rather
  than silently unbound — which is the exact class of bug this issue is.

Non-tailnet installs are untouched: every assertion below that expects a binding
first arranges a daemon-verified, allowlisted peer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard import refresh_tokens as rt
from kiro_crew.dashboard import token_auth as ta
from kiro_crew.dashboard.handlers import auth_refresh as h
from kiro_crew.dashboard.refresh_tokens import (
    RefreshStateManager,
    generate_refresh_token,
    refresh_cookie_name,
    refresh_token_peer_key,
    refresh_token_requires_peer,
)
from kiro_crew.dashboard.tailnet import ForwardedPeer, TailnetTrust
from kiro_crew.dashboard.token_auth import generate_token

PORT = 5476
LAPTOP_KEY = "ts:node:user@example.com|laptop.tail.ts.net"
PHONE_KEY = "ts:node:user@example.com|phone.tail.ts.net"
LOGIN_KEY = "ts:login:user@example.com"


def _peer(node: str) -> ForwardedPeer:
    return ForwardedPeer(login="user@example.com", node=node, address="100.64.0.5")


def _trust(*, pin_scope: str = "node", bind_refresh_chains: bool = True) -> TailnetTrust:
    return TailnetTrust(
        trust_identity=True,
        allowed_logins=("user@example.com",),
        pin_scope=pin_scope,
        bind_refresh_chains=bind_refresh_chains,
    )


@pytest.fixture(autouse=True)
def _isolate_token_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Keep the real middleware's PROCESS-GLOBAL auth state inside this test.

    ``_exchange`` below drives the real ``token_auth_middleware``, not a stub, and
    that is the point -- the mint-side gap this module tests lives in the
    middleware. The cost is that a genuine exchange writes process-global state:
    it binds into the ``ta._state`` singleton (nonces, peer bindings, consumed
    tokens) and revokes the link nonce through ``_get_revoked_store()``, whose
    singleton is built ONCE per process from whatever ``KIROCREW_HOME`` the first
    caller happened to see. Left alone, a store created here outlives the test and
    a LATER test writes through it into this test's already-deleted tmp home,
    while stale peer bindings make a later posture read report sessions that no
    test established.

    Reset on the way in as well as out: the leak can arrive from an earlier module
    just as easily as leave for a later one. ``_state.clear_all()`` rather than
    ``revoke_all_sessions()`` so the revocation generation is not bumped between
    unrelated tests -- a bump would reject every token minted before it.

    Deliberately the same fixture ``test/test_token_auth.py`` already uses for
    this, down to the pinned ``_gen``, rather than a second isolation shape for
    the same singletons.
    """
    import kiro_crew.dashboard.revocation_gen as _rg

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr(_rg, "_gen", 0)
    monkeypatch.setattr(ta, "_revoked_store_singleton", None)
    ta._state.clear_all()
    ta._app_perms_cache.clear()
    yield
    monkeypatch.setattr(_rg, "_gen", 0)
    monkeypatch.setattr(ta, "_revoked_store_singleton", None)
    ta._state.clear_all()
    ta._app_perms_cache.clear()


@pytest.fixture(autouse=True)
def _reset_rate_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(h, "_refresh_rate_buckets", {})
    monkeypatch.setattr(h, "_refresh_rate_last_sweep", float("-inf"))


@pytest.fixture()
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RefreshStateManager:
    """Pin the refresh-state singleton at a tmp_path-backed manager."""
    mgr = RefreshStateManager(state_path=tmp_path / "refresh_chains.json")
    monkeypatch.setattr(rt, "_state_singleton", mgr)
    return mgr


@pytest.fixture(autouse=True)
def _quiet_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep SEL out of it — these tests assert on HTTP outcomes, not the trail."""
    monkeypatch.setattr(h, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(ta, "_sel_fn", lambda: MagicMock())


def _refresh_request(
    refresh_cookie: str,
    *,
    trust: TailnetTrust | None = None,
    remote: str = "127.0.0.1",
) -> web.Request:
    """A POST /api/auth/refresh carrying *refresh_cookie*."""
    app = web.Application()
    app["port"] = PORT
    app["allowed_origins"] = {f"http://localhost:{PORT}"}
    if trust is not None:
        app["tailnet_trust"] = trust
    transport = MagicMock()
    transport.get_extra_info = lambda key, default=None: (
        (remote, 44444) if key == "peername" else default
    )
    return make_mocked_request(
        "POST",
        "/api/auth/refresh",
        headers={
            "Host": f"localhost:{PORT}",
            "Cookie": f"{refresh_cookie_name(str(PORT))}={refresh_cookie}",
        },
        app=app,
        transport=transport,
    )


async def _ok_handler(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def _exchange(
    trust: TailnetTrust,
    monkeypatch: pytest.MonkeyPatch,
    *,
    node: str,
    user: str = "user",
) -> web.StreamResponse:
    """Drive a real ``?token=`` link -> session exchange for a verified peer.

    This is the ORDINARY Phase-3 access session — no ``require_peer`` claim on the
    link, no ``no_refresh`` bound. It is the mint path the issue is about.
    """
    monkeypatch.setattr(ta, "resolve_forwarded_peer", AsyncMock(return_value=_peer(node)))
    mw = ta.token_auth_middleware(port=PORT, tailnet_trust=trust)
    link = generate_token(user, ttl_seconds=3600)
    app = web.Application()
    app["port"] = PORT
    transport = MagicMock()
    transport.get_extra_info = lambda key, default=None: (
        ("127.0.0.1", 44444) if key == "peername" else default
    )
    request = make_mocked_request(
        "GET",
        f"/?token={link}",
        headers={"Host": f"localhost:{PORT}"},
        app=app,
        transport=transport,
    )
    return await mw(request, _ok_handler)


def _refresh_cookie_of(response: web.StreamResponse) -> str:
    morsel = response.cookies.get(refresh_cookie_name(str(PORT)))
    assert morsel is not None, "the exchange minted no refresh cookie"
    return morsel.value


def _body(response: web.StreamResponse) -> Any:
    assert isinstance(response, web.Response)
    raw = response.body
    assert isinstance(raw, bytes)
    return json.loads(raw.decode("utf-8"))


# --- the exploit -------------------------------------------------------------


@pytest.mark.asyncio
async def test_stolen_refresh_cookie_cannot_rotate_from_another_allowed_node(
    state: RefreshStateManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The issue, end to end: steal from laptop, replay from phone.

    Before this change the replay returned 200 and pinned the replacement access
    token to the PHONE, laundering a laptop-scoped session onto another device.
    """
    trust = _trust()
    minted = await _exchange(trust, monkeypatch, node="laptop.tail.ts.net")
    stolen = _refresh_cookie_of(minted)

    # The thief's node is allowlisted — same login, different device. That is
    # precisely the case the issue is filed against.
    monkeypatch.setattr(
        h, "resolve_forwarded_peer", AsyncMock(return_value=_peer("phone.tail.ts.net"))
    )
    response = await h.api_auth_refresh(_refresh_request(stolen, trust=trust))

    assert response.status == 401
    assert _body(response)["code"] == "peer_identity_mismatch"
    assert "Set-Cookie" not in response.headers
    # A refusal is not a theft signal: the legitimate laptop must still be able
    # to rotate, so nothing is consumed and the chain is not revoked.
    _valid, _uid, _reason, chain_id, jti, _exp = rt.validate_refresh_token(stolen)
    assert not state.is_consumed(jti)
    assert not state.is_chain_revoked(chain_id)


@pytest.mark.asyncio
async def test_the_original_node_still_rotates_its_own_chain(
    state: RefreshStateManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The binding must not cost the legitimate device its own session."""
    trust = _trust()
    minted = await _exchange(trust, monkeypatch, node="laptop.tail.ts.net")
    cookie = _refresh_cookie_of(minted)

    monkeypatch.setattr(
        h, "resolve_forwarded_peer", AsyncMock(return_value=_peer("laptop.tail.ts.net"))
    )
    response = await h.api_auth_refresh(_refresh_request(cookie, trust=trust))

    assert response.status == 200
    rotated = _refresh_cookie_of(response)
    # The binding survives the rotation on both halves of the new pair, or the
    # SECOND rotation would silently be unbound again.
    assert refresh_token_requires_peer(rotated) is True
    assert refresh_token_peer_key(rotated) == LAPTOP_KEY
    _v, _u, _r, chain_id, _j, _e = rt.validate_refresh_token(rotated)
    assert state.chain_peer(chain_id) == LAPTOP_KEY


# --- the mint side -----------------------------------------------------------


@pytest.mark.asyncio
async def test_ordinary_phase3_exchange_binds_the_chain_it_opens(
    state: RefreshStateManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gap was at the mint: an ordinary session opened an UNBOUND chain."""
    response = await _exchange(_trust(), monkeypatch, node="laptop.tail.ts.net")
    cookie = _refresh_cookie_of(response)

    assert refresh_token_requires_peer(cookie) is True
    assert refresh_token_peer_key(cookie) == LAPTOP_KEY
    _v, _u, _r, chain_id, _j, _e = rt.validate_refresh_token(cookie)
    assert state.chain_peer(chain_id) == LAPTOP_KEY


@pytest.mark.asyncio
async def test_the_chain_binding_is_persisted_for_the_next_gateway(
    state: RefreshStateManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``refresh_chains.json`` carries the binding, so a restart keeps it."""
    response = await _exchange(_trust(), monkeypatch, node="laptop.tail.ts.net")
    _v, _u, _r, chain_id, _j, _e = rt.validate_refresh_token(_refresh_cookie_of(response))

    on_disk = json.loads((tmp_path / "refresh_chains.json").read_text())
    assert {"chain_id": chain_id, "peer_key": LAPTOP_KEY} == {
        k: v for k, v in on_disk["chain_peers"][0].items() if k != "exp"
    }
    # A fresh manager over the same file reads it back — the restart case.
    reloaded = RefreshStateManager(state_path=tmp_path / "refresh_chains.json")
    assert reloaded.chain_peer(chain_id) == LAPTOP_KEY


@pytest.mark.asyncio
async def test_a_non_tailnet_exchange_still_opens_an_unbound_chain(
    state: RefreshStateManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No identity trust, no binding — the ordinary token+IP install is untouched."""
    monkeypatch.setattr(ta, "resolve_forwarded_peer", AsyncMock(return_value=None))
    mw = ta.token_auth_middleware(port=PORT)
    link = generate_token("alice", ttl_seconds=3600)
    app = web.Application()
    app["port"] = PORT
    transport = MagicMock()
    transport.get_extra_info = lambda key, default=None: (
        ("127.0.0.1", 44444) if key == "peername" else default
    )
    request = make_mocked_request(
        "GET",
        f"/?token={link}",
        headers={"Host": f"localhost:{PORT}"},
        app=app,
        transport=transport,
    )
    response = await mw(request, _ok_handler)

    cookie = _refresh_cookie_of(response)
    assert refresh_token_requires_peer(cookie) is False
    _v, _u, _r, chain_id, _j, _e = rt.validate_refresh_token(cookie)
    assert state.chain_peer(chain_id) == ""


# --- the rotated ACCESS token ------------------------------------------------


@pytest.mark.asyncio
async def test_the_rotated_access_token_carries_the_signed_device(
    state: RefreshStateManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both halves of the rotated pair, not just the refresh cookie.

    The signed claim is what survives a gateway restart: before it, the rotated
    access cookie relied on the in-memory pin map, which is empty after a restart
    and re-pinned to whichever allowed node presented the cookie first. The pair
    is bound together on purpose — a rotation that bound only the chain would
    leave the access cookie with the same laundering shape one credential over.

    The cost of this is stated in the spec and worth pinning here: an access
    cookie carrying ``require_peer`` fails closed when no peer resolves, so
    turning identity trust off later ends these sessions until one re-mint.
    """
    from kiro_crew.dashboard.token_auth import required_peer_key_unverified

    trust = _trust()
    minted = await _exchange(trust, monkeypatch, node="laptop.tail.ts.net")
    monkeypatch.setattr(
        h, "resolve_forwarded_peer", AsyncMock(return_value=_peer("laptop.tail.ts.net"))
    )
    response = await h.api_auth_refresh(_refresh_request(_refresh_cookie_of(minted), trust=trust))

    assert response.status == 200
    access = response.cookies[f"mc_token_{PORT}"].value
    assert required_peer_key_unverified(access) == LAPTOP_KEY


@pytest.mark.asyncio
async def test_a_rotated_access_token_is_refused_for_another_node_after_restart(
    state: RefreshStateManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The signed claim beats an empty pin map — no first-arrival takeover.

    A restart clears the in-memory bindings, which is precisely when a stolen
    access cookie used to be re-pinned to whoever presented it. The claim the
    rotation now signs is what makes the ORIGINAL device the only answer, and it
    must not cost that device its own session.
    """
    from kiro_crew.dashboard.token_auth import _state as token_state

    trust = _trust()
    minted = await _exchange(trust, monkeypatch, node="laptop.tail.ts.net")
    monkeypatch.setattr(
        h, "resolve_forwarded_peer", AsyncMock(return_value=_peer("laptop.tail.ts.net"))
    )
    rotated = await h.api_auth_refresh(_refresh_request(_refresh_cookie_of(minted), trust=trust))
    access = rotated.cookies[f"mc_token_{PORT}"].value

    # The restart: every hot binding is gone, only the signed claims remain.
    token_state._peer_bindings.clear()

    async def _authenticate(node: str) -> int:
        monkeypatch.setattr(ta, "resolve_forwarded_peer", AsyncMock(return_value=_peer(node)))
        mw = ta.token_auth_middleware(port=PORT, tailnet_trust=trust)
        app = web.Application()
        app["port"] = PORT
        transport = MagicMock()
        transport.get_extra_info = lambda key, default=None: (
            ("127.0.0.1", 44444) if key == "peername" else default
        )
        request = make_mocked_request(
            "GET",
            "/api/sessions",
            headers={"Host": f"localhost:{PORT}", "Cookie": f"mc_token_{PORT}={access}"},
            app=app,
            transport=transport,
        )
        return (await mw(request, _ok_handler)).status

    # 403, not 401: the middleware's deny for an authenticated-but-wrong-device
    # caller is a permission refusal, not a missing credential.
    assert await _authenticate("phone.tail.ts.net") == 403
    assert await _authenticate("laptop.tail.ts.net") == 200


# --- roaming and the opt-out -------------------------------------------------


@pytest.mark.asyncio
async def test_login_scope_still_roams_between_the_users_own_devices(
    state: RefreshStateManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``pin_scope: "login"`` is the roaming knob, and the binding respects it.

    This is the resolution of the availability tradeoff raised on the issue: at
    login scope the pin key is the identity, not the device, so the same person's
    other device rotates normally. At node scope the ACCESS token was already
    device-pinned, so the chain being unbound was the only thing making
    cross-node use "work" — by laundering, which is the defect.
    """
    trust = _trust(pin_scope="login")
    minted = await _exchange(trust, monkeypatch, node="laptop.tail.ts.net")
    cookie = _refresh_cookie_of(minted)
    assert refresh_token_peer_key(cookie) == LOGIN_KEY

    monkeypatch.setattr(
        h, "resolve_forwarded_peer", AsyncMock(return_value=_peer("phone.tail.ts.net"))
    )
    response = await h.api_auth_refresh(_refresh_request(cookie, trust=trust))

    assert response.status == 200
    assert refresh_token_peer_key(_refresh_cookie_of(response)) == LOGIN_KEY


@pytest.mark.asyncio
async def test_the_opt_out_restores_the_unbound_chain(
    state: RefreshStateManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``bind_refresh_chains: false`` is the documented availability escape hatch."""
    trust = _trust(bind_refresh_chains=False)
    minted = await _exchange(trust, monkeypatch, node="laptop.tail.ts.net")
    cookie = _refresh_cookie_of(minted)
    assert refresh_token_requires_peer(cookie) is False

    monkeypatch.setattr(
        h, "resolve_forwarded_peer", AsyncMock(return_value=_peer("phone.tail.ts.net"))
    )
    response = await h.api_auth_refresh(_refresh_request(cookie, trust=trust))
    assert response.status == 200


# --- migration and the server-side record ------------------------------------


@pytest.mark.asyncio
async def test_a_pre_upgrade_chain_keeps_todays_unbound_semantics(
    state: RefreshStateManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Migration rule from the issue: absent binding = unbound, as today.

    Every chain that already exists on an upgraded install has neither the signed
    claim nor a persisted record. Refusing those would log the whole 30-day
    window out on upgrade, which is not what a hardening change may do silently.
    """
    legacy, chain_id, _jti, _exp = generate_refresh_token("alice")
    assert state.chain_peer(chain_id) == ""

    monkeypatch.setattr(
        h, "resolve_forwarded_peer", AsyncMock(return_value=_peer("phone.tail.ts.net"))
    )
    response = await h.api_auth_refresh(_refresh_request(legacy, trust=_trust()))
    assert response.status == 200


@pytest.mark.asyncio
async def test_the_persisted_record_alone_refuses_a_different_peer(
    state: RefreshStateManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defense in depth: the record is authority even with no claim on the token.

    This issue exists because ONE mint path carried the claim and the others did
    not. A server-side record the token cannot influence is what makes the next
    forgetful mint path fail closed instead of silently unbound.
    """
    claimless, chain_id, jti, exp = generate_refresh_token("alice")
    state.bind_chain_peer(chain_id, LAPTOP_KEY, exp)

    monkeypatch.setattr(
        h, "resolve_forwarded_peer", AsyncMock(return_value=_peer("phone.tail.ts.net"))
    )
    response = await h.api_auth_refresh(_refresh_request(claimless, trust=_trust()))

    assert response.status == 401
    assert _body(response)["code"] == "peer_identity_mismatch"
    assert not state.is_consumed(jti)
    assert not state.is_chain_revoked(chain_id)


@pytest.mark.asyncio
async def test_a_bound_chain_cannot_rotate_while_identity_is_unverifiable(
    state: RefreshStateManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed on an unresolvable peer, and do NOT revoke over a daemon blip."""
    trust = _trust()
    minted = await _exchange(trust, monkeypatch, node="laptop.tail.ts.net")
    cookie = _refresh_cookie_of(minted)

    monkeypatch.setattr(h, "resolve_forwarded_peer", AsyncMock(return_value=None))
    response = await h.api_auth_refresh(_refresh_request(cookie, trust=trust))

    assert response.status == 401
    assert _body(response)["code"] == "peer_identity_unverified"
    _v, _u, _r, chain_id, jti, _e = rt.validate_refresh_token(cookie)
    assert not state.is_consumed(jti)
    assert not state.is_chain_revoked(chain_id)


def test_revoking_a_chain_drops_its_peer_record(state: RefreshStateManager) -> None:
    """A dead chain must not leave a binding behind for a recycled chain_id."""
    _token, chain_id, _jti, exp = generate_refresh_token("alice")
    state.bind_chain_peer(chain_id, LAPTOP_KEY, exp)
    assert state.chain_peer(chain_id) == LAPTOP_KEY
    state.revoke_chain(chain_id, exp)
    assert state.chain_peer(chain_id) == ""


def test_an_expired_chain_binding_is_evicted(state: RefreshStateManager) -> None:
    """Bindings age out with their chain, so the state file stays bounded."""
    state.bind_chain_peer("deadbeef", LAPTOP_KEY, 1.0)
    state.evict_expired(now=2.0)
    assert state.chain_peer("deadbeef") == ""


@pytest.mark.parametrize(
    "document",
    [
        pytest.param({"chain_peers": None}, id="chain_peers-null"),
        pytest.param({"chain_peers": {"a": "b"}}, id="chain_peers-object"),
        pytest.param({"consumed_jtis": None}, id="consumed_jtis-null"),
        pytest.param({"revoked_chains": None}, id="revoked_chains-null"),
        pytest.param([], id="document-is-a-list"),
        pytest.param("nonsense", id="document-is-a-string"),
        pytest.param(None, id="document-is-null"),
    ],
)
def test_a_wrong_shaped_state_file_neither_crashes_nor_reads_as_empty(
    tmp_path: Path, document: object
) -> None:
    """Both halves of the review argument on this span, in one assertion pair.

    Round 1 (GPT, BLOCKING): iterating a present-but-null key raised inside the
    constructor, which runs from ``_get_state()``, so one malformed byte-range
    500'd EVERY ``/api/auth/refresh``. A ``.get(key, [])`` default does not cover
    it -- the key exists, so the default is never consulted.

    Round 3 (GPT + Opus adjudication, UPHOLD-FENCED): reading it as EMPTY is the
    opposite defect. These lists are security controls, so empty means "nothing
    revoked, nothing consumed, nothing bound" -- i.e. every control satisfied.

    So the store must do neither: construct without raising, and report itself
    degraded rather than clean.
    """
    path = tmp_path / "refresh_chains.json"
    path.write_text(json.dumps(document))

    mgr = RefreshStateManager(state_path=path)

    assert mgr.degraded_reason(), "a wrong-shaped file must not read as clean state"


def test_an_absent_record_key_is_not_corruption(tmp_path: Path) -> None:
    """Absence is fine; only a PRESENT unreadable record degrades the store.

    This is the line that keeps the fail-closed posture from swallowing normal
    installs: a fresh state file has no ``revoked_chains`` because nothing has
    been revoked, and reading that as corruption would refuse every rotation on a
    healthy system.
    """
    path = tmp_path / "refresh_chains.json"
    path.write_text(json.dumps({"consumed_jtis": [], "revoked_chains": []}))
    assert RefreshStateManager(state_path=path).degraded_reason() == ""

    path.write_text(json.dumps({}))
    assert RefreshStateManager(state_path=path).degraded_reason() == ""


def test_an_unparseable_file_still_starts_empty(tmp_path: Path) -> None:
    """The pinned behaviour this change deliberately does NOT touch.

    ``test_tr_i_17_corrupted_state_file_starts_empty`` pins that a file which will
    not parse starts empty rather than crashing, and its docstring records that it
    was settled "per review-bot finding (post 34)". ``atomic_write`` makes a torn
    write impossible, so a file that will not parse at all is better read as "not
    our state" than as "our records, lost". Asserted here so a future round of the
    fail-closed argument cannot quietly widen into it.
    """
    path = tmp_path / "refresh_chains.json"
    path.write_text("not valid json {[")
    mgr = RefreshStateManager(state_path=path)
    assert mgr.degraded_reason() == ""
    assert mgr.is_consumed("anything") is False


def test_a_corrupt_revoked_chains_list_cannot_revive_a_revoked_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact harm the Opus adjudication upheld as non-self-healing.

    Revoke a chain, then corrupt the list that records it. Read as empty,
    ``is_chain_revoked`` answers False and the stolen cookie rotates into fresh
    credentials -- and reuse detection does not save you, because it only re-fires
    on a jti replay the attacker need not cause. So the token must be refused.
    """
    path = tmp_path / "refresh_chains.json"
    live = RefreshStateManager(state_path=path)
    monkeypatch.setattr(rt, "_state_singleton", live)

    token, chain_id, _jti, exp = generate_refresh_token("alice")
    live.revoke_chain(chain_id, exp)
    assert rt.validate_refresh_token(token)[2] == "chain revoked"

    # The record that carried the revocation is now unreadable.
    path.write_text(json.dumps({"revoked_chains": None}))
    monkeypatch.setattr(rt, "_state_singleton", RefreshStateManager(state_path=path))

    valid, _user, reason, _c, _j, _e = rt.validate_refresh_token(token)
    assert valid is False, "a revoked chain came back to life through a corrupt file"
    assert reason.startswith("refresh state unavailable")


@pytest.mark.asyncio
async def test_a_wrong_shaped_state_file_refuses_rotation_without_a_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At the endpoint: 401, not 500 (round 1) and not 200 (round 3)."""
    path = tmp_path / "refresh_chains.json"
    path.write_text(json.dumps({"chain_peers": None, "consumed_jtis": None}))

    token, _chain, _jti, _exp = generate_refresh_token("alice")
    monkeypatch.setattr(rt, "_state_singleton", RefreshStateManager(state_path=path))

    response = await h.api_auth_refresh(_refresh_request(token))

    assert response.status == 401
    assert _body(response)["error"] == "invalid_refresh"


def test_a_degraded_store_does_not_overwrite_the_unreadable_file(tmp_path: Path) -> None:
    """Persisting would destroy the records AND silently self-heal into the bypass.

    An empty write here loses whatever the operator still has on disk, and the
    NEXT start would load a clean empty store and resume rotating -- converting a
    refusal into the very bypass it exists to prevent. So the file is left alone.
    """
    path = tmp_path / "refresh_chains.json"
    original = json.dumps({"revoked_chains": None})
    path.write_text(original)

    mgr = RefreshStateManager(state_path=path)
    assert mgr.degraded_reason()
    mgr.revoke_chain("some-chain", 9e12)  # triggers _persist

    assert path.read_text() == original, "the unreadable file was overwritten"


def test_a_malformed_chain_binding_does_not_brick_the_store(tmp_path: Path) -> None:
    """One bad record must not take every refresh with it (mirrors the exp guard)."""
    path = tmp_path / "refresh_chains.json"
    path.write_text(
        json.dumps(
            {
                "chain_peers": [
                    {"chain_id": "bad", "peer_key": LAPTOP_KEY, "exp": "not-a-number"},
                    {"chain_id": "good", "peer_key": PHONE_KEY, "exp": 9e12},
                    {"chain_id": "nokey", "exp": 9e12},
                ]
            }
        )
    )
    mgr = RefreshStateManager(state_path=path)
    assert mgr.chain_peer("bad") == ""
    assert mgr.chain_peer("nokey") == ""
    assert mgr.chain_peer("good") == PHONE_KEY
