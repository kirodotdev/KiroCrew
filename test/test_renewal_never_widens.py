"""A renewed session never holds more than the credential that opened it.

An app token exchanged through ``?token=`` gets no refresh chain, so a rotation cannot
turn it into a session without its ``app`` claim. A rotation carries every bounding
claim its chain has onto both halves of the new pair. A link, the sessions it opened
and their rotations share one credential family, which ``revoke_token_family``
(``kirocrew logout --link``) ends together without touching any other session. A child
link a session lends a phone is a family of its own that descends from the lender's, so
revoking the lender ends it too, and revoking it ends only what it opened.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from dashboard_handlers_core_coverage_helpers import fake_sel_fixture  # noqa: F401
from dashboard_handlers_core_coverage_helpers import _req
from token_auth_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _make_request,
    _ok_handler,
    clear_nonces,
)

from kiro_crew.dashboard import refresh_tokens as rt
from kiro_crew.dashboard.boot_id import current_boot_id
from kiro_crew.dashboard.handlers import auth_refresh as h
from kiro_crew.dashboard.handlers import core as core_mod
from kiro_crew.dashboard.handlers._shared_access import _caller_bounds
from kiro_crew.dashboard.refresh_tokens import (
    REFRESH_COOKIE_PREFIX,
    RefreshStateManager,
    generate_refresh_token,
    refresh_cookie_name,
    validate_refresh_token,
)
from kiro_crew.dashboard.token_auth import (
    MAX_SESSION_TTL_SECS,
    RevokedNonceStore,
    _b64url_decode,
    generate_token,
    revoke_access_cookie,
    token_auth_middleware,
    validate_token,
)

PORT = "5476"


def revoke_token_family(token: str) -> bool:
    # Imported per call, so on a tree without it each test fails on its own.
    from kiro_crew.dashboard.token_auth_tokens import revoke_token_family as revoke

    return revoke(token)


@pytest.fixture(autouse=True)
def refresh_state(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch) -> RefreshStateManager:
    """A refresh store of this test's own, and a clean refresh rate limiter."""
    mgr = RefreshStateManager(state_path=tmp_path / "refresh_chains.json")
    _floor_monkeypatch.setattr(rt, "_state_singleton", mgr)
    _floor_monkeypatch.setattr(h, "_refresh_rate_buckets", {})
    _floor_monkeypatch.setattr(h, "_refresh_rate_last_sweep", float("-inf"))
    return mgr


def _lend(carried: dict, ttl_seconds: int) -> str:
    """The child link a session lends a phone, as the mobile-link mint builds it."""
    return generate_token("owner", ttl_seconds=ttl_seconds, extra=carried)


def _use_revoked_store(monkeypatch: pytest.MonkeyPatch, path: Path) -> RevokedNonceStore:
    """Point the revoked-nonce singleton at a store backed by *path*."""
    import kiro_crew.dashboard.token_auth as _ta

    store = RevokedNonceStore(state_path=path)
    monkeypatch.setattr(_ta, "_revoked_store_singleton", store)
    return store


def _claims(token: str) -> dict:
    return json.loads(_b64url_decode(token.split(".", 1)[0]))


def _live_refresh_cookies(resp: web.StreamResponse) -> list[str]:
    return [
        name
        for name in resp.cookies
        if name.startswith(REFRESH_COOKIE_PREFIX) and int(resp.cookies[name]["max-age"] or 0) > 0
    ]


async def _exchange(link: str, remote: str = "10.0.0.7") -> tuple[str, str]:
    """Open *link* as ``?token=``; return the access and refresh cookies it set."""
    resp = await token_auth_middleware()(
        _make_request(query={"token": link}, remote=remote), _ok_handler
    )
    assert resp.status == 200
    refresh = resp.cookies.get(refresh_cookie_name(PORT))
    return resp.cookies[f"mc_token_{PORT}"].value, refresh.value if refresh else ""


async def _rotate(refresh_token: str) -> tuple[str, str]:
    """POST ``/api/auth/refresh`` with *refresh_token*; return the rotated pair."""
    resp = await _refresh(refresh_token)
    assert resp.status == 200, resp.text
    return resp.cookies[f"mc_token_{PORT}"].value, resp.cookies[refresh_cookie_name(PORT)].value


async def _refresh(refresh_token: str) -> web.Response:
    """POST ``/api/auth/refresh`` with *refresh_token*; return the handler's response."""
    app = web.Application()
    app["port"] = int(PORT)
    app["allowed_origins"] = {f"http://localhost:{PORT}"}
    transport = MagicMock()
    transport.get_extra_info = lambda key, default=None: (
        ("203.0.113.9", 44444) if key == "peername" else default
    )
    request = make_mocked_request(
        "POST",
        "/api/auth/refresh",
        headers={
            "Host": f"localhost:{PORT}",
            "Cookie": f"{refresh_cookie_name(PORT)}={refresh_token}",
        },
        app=app,
        transport=transport,
    )
    resp = await h.api_auth_refresh(request)
    assert isinstance(resp, web.Response)
    return resp


# -- an app credential is never renewed into a plain session --------------------------


@pytest.mark.asyncio
async def test_an_app_token_exchange_gets_a_session_but_no_refresh_chain(monkeypatch) -> None:
    import kiro_crew.dashboard.token_auth as _ta

    monkeypatch.setattr(_ta, "_app_api_allowlist", lambda name: ())
    token = generate_token("file-explorer", app="file-explorer")
    resp = await token_auth_middleware()(
        _make_request(path="/apps/file-explorer/api/list", query={"token": token}), _ok_handler
    )
    assert resp.status == 200
    assert _claims(resp.cookies[f"mc_token_{PORT}"].value)["app"] == "file-explorer"
    assert _live_refresh_cookies(resp) == []


@pytest.mark.asyncio
async def test_an_app_exchange_leaves_a_refresh_cookie_it_did_not_mint_alone(
    monkeypatch,
) -> None:
    """Unlike ``no_refresh``, an app session does not sign the browser's own chain out."""
    import kiro_crew.dashboard.token_auth as _ta

    monkeypatch.setattr(_ta, "_app_api_allowlist", lambda name: ())
    token = generate_token("file-explorer", app="file-explorer")
    resp = await token_auth_middleware()(
        _make_request(
            path="/apps/file-explorer/api/list",
            query={"token": token},
            cookies={refresh_cookie_name(PORT): "the-owners-chain"},
        ),
        _ok_handler,
    )
    assert resp.status == 200
    assert refresh_cookie_name(PORT) not in resp.cookies


# -- a rotation keeps every bound its chain carries -----------------------------------


@pytest.mark.asyncio
async def test_a_rotation_carries_every_bound_onto_both_halves() -> None:
    refresh, _chain, _jti, _exp = generate_refresh_token(
        "owner",
        boot=current_boot_id(),
        owner_mint=True,
        family="link-nonce-1",
        ancestors="lender-nonce-1",
    )
    access, rotated = await _rotate(refresh)
    for token in (access, rotated):
        claims = _claims(token)
        assert claims["boot"] == current_boot_id()
        assert claims["owner_mint"] == "1"
        assert claims["family"] == "link-nonce-1"
        assert claims["ancestors"] == "lender-nonce-1"
        assert "app" not in claims
    assert _claims(access)["session_exp"] - _claims(access)["iat"] <= MAX_SESSION_TTL_SECS


# -- one revocation ends a link's whole credential family -----------------------------


@pytest.mark.asyncio
async def test_the_exchange_stamps_the_link_family_on_the_session_and_its_chain() -> None:
    link = generate_token("owner")
    access, refresh = await _exchange(link)
    nonce = _claims(link)["nonce"]
    assert _claims(access)["family"] == nonce
    assert _claims(refresh)["family"] == nonce


@pytest.mark.asyncio
async def test_revoking_a_link_ends_it_its_sessions_and_their_refresh_chains() -> None:
    link = generate_token("owner")
    access, refresh = await _exchange(link)
    rotated_access, rotated_refresh = await _rotate(refresh)
    other_access, other_refresh = await _exchange(generate_token("owner"))

    assert revoke_token_family(link) is True

    assert validate_token(link)[0] is False
    for session in (access, rotated_access):
        assert validate_token(session, use_session_exp=True)[:3] == (False, "", "session revoked")
    assert validate_refresh_token(rotated_refresh)[2] == "session revoked"
    # Another link's family is untouched.
    assert validate_token(other_access, use_session_exp=True)[0] is True
    assert validate_refresh_token(other_refresh)[0] is True


@pytest.mark.asyncio
async def test_revoking_the_lender_ends_the_child_its_session_and_rotated_chain() -> None:
    link = generate_token("owner")
    access, _refresh = await _exchange(link)
    carried, ttl = _caller_bounds(SimpleNamespace(get={"auth_token": access}.get))
    child = _lend(carried, ttl)
    child_access, child_refresh = await _exchange(child)
    rotated_access, rotated_refresh = await _rotate(child_refresh)
    # A grandchild the child's rotated session lends descends from both.
    grand_carried, grand_ttl = _caller_bounds(
        SimpleNamespace(get={"auth_token": rotated_access}.get)
    )
    grandchild = _lend(grand_carried, grand_ttl)
    nonce, child_nonce = _claims(link)["nonce"], _claims(child)["nonce"]
    assert _claims(child)["ancestors"] == nonce
    assert _claims(grandchild)["ancestors"] == f"{child_nonce},{nonce}"
    for token in (child_access, child_refresh, rotated_access, rotated_refresh):
        assert (_claims(token)["family"], _claims(token)["ancestors"]) == (child_nonce, nonce)
    assert validate_token(child)[0] is True

    assert revoke_token_family(link) is True

    assert validate_token(child)[0] is False
    assert validate_token(grandchild)[0] is False
    for session in (child_access, rotated_access):
        assert validate_token(session, use_session_exp=True)[:3] == (False, "", "session revoked")
    assert validate_refresh_token(rotated_refresh)[2] == "session revoked"


@pytest.mark.asyncio
async def test_revoking_a_child_link_leaves_the_lending_session_and_its_chain() -> None:
    link = generate_token("owner")
    access, refresh = await _exchange(link)
    carried, ttl = _caller_bounds(SimpleNamespace(get={"auth_token": access}.get))
    child = _lend(carried, ttl)
    child_access, child_refresh = await _exchange(child)

    assert revoke_token_family(child) is True

    assert validate_token(child)[0] is False
    assert validate_token(child_access, use_session_exp=True)[0] is False
    assert validate_refresh_token(child_refresh)[2] == "session revoked"
    assert validate_token(access, use_session_exp=True)[0] is True
    assert validate_refresh_token(refresh)[0] is True
    rotated_access, _rotated_refresh = await _rotate(refresh)
    assert validate_token(rotated_access, use_session_exp=True)[0] is True


@pytest.mark.asyncio
async def test_a_rotation_rechecks_every_family_its_chain_descends_from(monkeypatch) -> None:
    """The pre-mint re-check covers an ancestor, not only the chain's own family."""
    link = generate_token("owner")
    access, _parent_refresh = await _exchange(link)
    carried, ttl = _caller_bounds(SimpleNamespace(get={"auth_token": access}.get))
    _child_access, child_refresh = await _exchange(_lend(carried, ttl))
    real_validate = h.validate_refresh_token

    def _validate_then_revoke(token: str):
        result = real_validate(token)
        revoke_token_family(link)
        return result

    monkeypatch.setattr(h, "validate_refresh_token", _validate_then_revoke)
    resp = await _refresh(child_refresh)
    assert (resp.status, json.loads(resp.body)["code"]) == (401, "invalid_refresh")
    assert refresh_cookie_name(PORT) not in resp.cookies


def test_a_forged_or_foreign_token_revokes_nothing() -> None:
    link = generate_token("owner")
    assert revoke_token_family(link + "x") is False
    assert revoke_token_family("not-a-token") is False
    assert validate_token(link)[0] is True


def _unwritable_store_path(tmp_path: Path) -> Path:
    """A store path whose parent is a regular file: reads find nothing, writes fail."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    return blocker / "token_revoked_nonces.json"


def test_a_revocation_the_store_cannot_write_is_not_reported_done(tmp_path, monkeypatch) -> None:
    _use_revoked_store(monkeypatch, _unwritable_store_path(tmp_path))
    with pytest.raises(OSError):
        revoke_token_family(generate_token("owner"))


def test_a_per_session_logout_the_store_cannot_write_is_not_reported_done(
    tmp_path, monkeypatch
) -> None:
    """Same store, same failure: the single-cookie logout says so too."""
    session = generate_token("owner", session=True, register_nonce=False)
    _use_revoked_store(monkeypatch, _unwritable_store_path(tmp_path))
    with pytest.raises(OSError):
        revoke_access_cookie(session)


@pytest.mark.asyncio
async def test_an_unreadable_denylist_refuses_a_family_chain(tmp_path, monkeypatch) -> None:
    """A store file that exists but cannot be read must not read as "nothing revoked"."""
    _access, refresh = await _exchange(generate_token("owner"))
    unreadable = tmp_path / "unreadable-store"
    unreadable.mkdir()
    _use_revoked_store(monkeypatch, unreadable)
    assert validate_refresh_token(refresh)[2] == "session revocation state unavailable"


def test_the_family_check_never_reads_the_store_file_on_the_calling_thread(
    tmp_path, monkeypatch
) -> None:
    """An unreadable denylist is retried on a background thread; the caller (the event
    loop) is refused at once and admitted once that read lands."""
    import threading

    from kiro_crew.testing.wait import wait_until

    unreadable = tmp_path / "unreadable-store"
    unreadable.mkdir()
    store = _use_revoked_store(monkeypatch, unreadable)
    readers: list[str] = []
    real_load = store._load

    def _recording_load() -> bool:
        readers.append(threading.current_thread().name)
        return real_load()

    monkeypatch.setattr(store, "_load", _recording_load)
    from kiro_crew.dashboard.token_auth_tokens import credential_family_refusal

    assert credential_family_refusal("fam") == "session revocation state unavailable"
    wait_until(lambda: readers and not store._reloading, describe=lambda: readers)
    assert threading.current_thread().name not in readers
    unreadable.rmdir()
    wait_until(lambda: credential_family_refusal("fam") == "", describe=lambda: readers)
    assert threading.current_thread().name not in readers


@pytest.mark.asyncio
async def test_logout_revokes_the_chain_while_the_family_denylist_is_unreadable(
    tmp_path, monkeypatch, refresh_state
) -> None:
    """The chain is authentic when only the family check failed, so logout still ends it."""
    _access, refresh = await _exchange(generate_token("owner"))
    chain_id = json.loads(_b64url_decode(refresh.split(".", 1)[0]))["chain_id"]
    unreadable = tmp_path / "unreadable-store"
    unreadable.mkdir()
    _use_revoked_store(monkeypatch, unreadable)
    app = web.Application()
    app["port"] = int(PORT)
    app["allowed_origins"] = {f"http://localhost:{PORT}"}
    transport = MagicMock()
    transport.get_extra_info = lambda key, default=None: (
        ("203.0.113.9", 44444) if key == "peername" else default
    )
    request = make_mocked_request(
        "POST",
        "/api/auth/logout",
        headers={"Host": f"localhost:{PORT}", "Cookie": f"{refresh_cookie_name(PORT)}={refresh}"},
        app=app,
        transport=transport,
    )
    await h.api_auth_logout(request)
    assert refresh_state.is_chain_revoked(chain_id)


@pytest.mark.asyncio
async def test_a_revocation_during_a_rotation_mints_no_pair(monkeypatch) -> None:
    """The owner revokes after the chain validated and before the pair is minted."""
    link = generate_token("owner")
    _access, refresh = await _exchange(link)
    real_validate = h.validate_refresh_token

    def _validate_then_revoke(token: str):
        result = real_validate(token)
        revoke_token_family(link)
        return result

    monkeypatch.setattr(h, "validate_refresh_token", _validate_then_revoke)
    resp = await _refresh(refresh)
    assert resp.status == 401
    assert refresh_cookie_name(PORT) not in resp.cookies


@pytest.mark.asyncio
async def test_logout_with_a_link_ends_that_family_only(monkeypatch, fake_sel) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: True)
    revoke_all = AsyncMock()
    monkeypatch.setattr("kiro_crew.dashboard.token_auth.revoke_all_sessions_async", revoke_all)
    link = generate_token("owner")
    access, _refresh = await _exchange(link)
    other_access, _ = await _exchange(generate_token("owner"))

    request = _req(app={"local_secret": "right"}, headers={"X-Local-Secret": "right"})
    request.can_read_body = True

    async def _body() -> dict:
        return {"link": f"http://127.0.0.1:{PORT}/?token={link}"}

    request.json = _body
    resp = await core_mod.api_logout_link(request)

    assert (resp.status, json.loads(resp.body)) == (200, {"ok": True, "scope": "link"})
    revoke_all.assert_not_called()
    assert validate_token(access, use_session_exp=True)[0] is False
    assert validate_token(other_access, use_session_exp=True)[0] is True


@pytest.mark.asyncio
async def test_logout_refuses_a_link_this_gateway_did_not_sign(monkeypatch, fake_sel) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: True)
    request = _req(app={"local_secret": "right"}, headers={"X-Local-Secret": "right"})
    request.can_read_body = True

    async def _body() -> dict:
        return {"link": "forged.token"}

    request.json = _body
    resp = await core_mod.api_logout_link(request)
    assert (resp.status, json.loads(resp.body)["code"]) == (400, "invalid_link")


def _legacy_link() -> str:
    """A signed link shaped like one minted before families: no ``fam_v`` marker."""
    from kiro_crew.dashboard.token_auth_tokens import FAMILY_MARKER_CLAIM, _b64url_encode, _sign

    claims = _claims(generate_token("owner"))
    claims.pop(FAMILY_MARKER_CLAIM)
    raw = json.dumps(claims, separators=(",", ":")).encode()
    return f"{_b64url_encode(raw)}.{_sign(raw)}"


@pytest.mark.asyncio
async def test_revoking_a_link_minted_before_families_is_refused(monkeypatch, fake_sel) -> None:
    """Its sessions carry no family, so the route must not report them revoked."""
    monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: True)
    link = _legacy_link()
    request = _req(app={"local_secret": "right"}, headers={"X-Local-Secret": "right"})
    request.can_read_body = True

    async def _body() -> dict:
        return {"link": link}

    request.json = _body
    resp = await core_mod.api_logout_link(request)
    body = json.loads(resp.body)
    assert (resp.status, body["code"]) == (400, "legacy_link")
    assert "kirocrew logout" in body["error"]
    assert validate_token(link)[0] is True


@pytest.mark.asyncio
async def test_revoking_a_session_rotated_from_a_chain_before_families_is_refused(
    monkeypatch, fake_sel
) -> None:
    """After an upgrade such a session carries the marker but no family, so a record on
    its own nonce would end it alone while its chain mints the next one."""
    monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: True)
    pre_family_chain, _chain, _jti, _exp = generate_refresh_token("owner", boot=current_boot_id())
    session, chain = await _rotate(pre_family_chain)
    request = _req(app={"local_secret": "right"}, headers={"X-Local-Secret": "right"})
    request.can_read_body = True

    async def _body() -> dict:
        return {"link": session}

    request.json = _body
    resp = await core_mod.api_logout_link(request)
    assert (resp.status, json.loads(resp.body)["code"]) == (400, "legacy_link")
    assert validate_token(session, use_session_exp=True)[0] is True
    assert (await _refresh(chain)).status == 200
