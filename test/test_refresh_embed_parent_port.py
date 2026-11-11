"""An Instances pane session keeps its ``embed_parent_port`` across refresh.

A remote crew embedded in the hub's Instances viewport authorizes the hub as a
CSP frame ancestor only through the signed ``embed_parent_port`` claim on its
``mc_token_<port>`` access cookie. ``/api/auth/refresh`` re-mints that cookie,
so the claim has to travel on the refresh chain: if a rotation drops it, the
remote answers the pane's next framed document load with bare
``frame-ancestors 'self'`` and the pane goes blank until the app restarts.

Asserted here, through the real link -> session exchange and the real refresh
handler:

* the exchange writes the claim onto the refresh token it mints;
* a rotation carries it onto BOTH halves of the new pair, and a second rotation
  still has it;
* a session opened without the claim stays without it;
* a malformed claim on the refresh token is dropped rather than signed into the
  rotated access token.
"""

from __future__ import annotations

import json
from pathlib import Path
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
)
from kiro_crew.dashboard.token_auth import generate_token
from kiro_crew.dashboard.token_auth_tokens import token_embed_parent_port

PORT = 7778
PARENT_PORT = 5476


@pytest.fixture(autouse=True)
def _isolate_token_state(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch):
    """Keep the real middleware's process-global auth state inside this test.

    Same isolation as ``test_refresh_peer_binding.py``: the exchange below runs
    the real ``token_auth_middleware``, which writes the ``ta._state`` singleton
    and the revoked-nonce store.
    """
    import kiro_crew.dashboard.revocation_gen as _rg

    mp = _floor_monkeypatch
    mp.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    mp.setattr(_rg, "_gen", 0)
    mp.setattr(ta, "_revoked_store_singleton", None)
    ta._state.clear_all()
    ta._app_perms_cache.clear()
    yield
    mp.setattr(_rg, "_gen", 0)
    mp.setattr(ta, "_revoked_store_singleton", None)
    ta._state.clear_all()
    ta._app_perms_cache.clear()


@pytest.fixture(autouse=True)
def _quiet(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    mp = _floor_monkeypatch
    mp.setattr(h, "_refresh_rate_buckets", {})
    mp.setattr(h, "_refresh_rate_last_sweep", float("-inf"))
    mp.setattr(h, "_audit", lambda *a, **k: None)
    mp.setattr(ta, "_sel_fn", lambda: MagicMock())
    mp.setattr(ta, "resolve_forwarded_peer", AsyncMock(return_value=None))
    mp.setattr(h, "resolve_forwarded_peer", AsyncMock(return_value=None))


@pytest.fixture(autouse=True)
def state(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch) -> RefreshStateManager:
    mgr = RefreshStateManager(state_path=tmp_path / "refresh_chains.json")
    _floor_monkeypatch.setattr(rt, "_state_singleton", mgr)
    return mgr


def _transport() -> MagicMock:
    transport = MagicMock()
    transport.get_extra_info = lambda key, default=None: (
        ("127.0.0.1", 44444) if key == "peername" else default
    )
    return transport


async def _ok_handler(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def _exchange(extra: dict[str, str] | None) -> web.StreamResponse:
    """Drive a real ``?token=`` link -> session exchange, as a pane's first load does."""
    mw = ta.token_auth_middleware(port=PORT)
    link = generate_token(
        "alice", ttl_seconds=3600, extra=extra
    )  # flake-ok: token lifetime, no wait
    app = web.Application()
    app["port"] = PORT
    request = make_mocked_request(
        "GET",
        f"/?token={link}",
        headers={"Host": f"localhost:{PORT}"},
        app=app,
        transport=_transport(),
    )
    return await mw(request, _ok_handler)


async def _rotate(refresh_cookie: str) -> web.StreamResponse:
    app = web.Application()
    app["port"] = PORT
    app["allowed_origins"] = {f"http://localhost:{PORT}"}
    request = make_mocked_request(
        "POST",
        "/api/auth/refresh",
        headers={
            "Host": f"localhost:{PORT}",
            "Cookie": f"{refresh_cookie_name(str(PORT))}={refresh_cookie}",
        },
        app=app,
        transport=_transport(),
    )
    response = await h.api_auth_refresh(request)
    assert response.status == 200
    return response


def _cookie(response: web.StreamResponse, name: str) -> str:
    morsel = response.cookies.get(name)
    assert morsel is not None, f"no {name} cookie was set"
    return morsel.value


def _refresh_claim(refresh_token: str) -> object:
    """The raw ``embed_parent_port`` a refresh token's signed payload carries.

    Read straight from the payload rather than through the module's accessor, so
    the end-to-end assertions observe what was signed, not what a reader admits.
    """
    payload = json.loads(rt._b64url_decode(refresh_token.split(".", 1)[0]))
    return payload.get("embed_parent_port")


ACCESS = f"mc_token_{PORT}"
REFRESH = refresh_cookie_name(str(PORT))


@pytest.mark.asyncio
async def test_a_pane_session_keeps_its_parent_port_across_rotations() -> None:
    minted = await _exchange({"embed_parent_port": str(PARENT_PORT)})
    assert token_embed_parent_port(_cookie(minted, ACCESS)) == PARENT_PORT
    refresh = _cookie(minted, REFRESH)
    assert _refresh_claim(refresh) == str(PARENT_PORT)

    first = await _rotate(refresh)
    assert token_embed_parent_port(_cookie(first, ACCESS)) == PARENT_PORT
    assert _refresh_claim(_cookie(first, REFRESH)) == str(PARENT_PORT)

    second = await _rotate(_cookie(first, REFRESH))
    assert token_embed_parent_port(_cookie(second, ACCESS)) == PARENT_PORT


@pytest.mark.asyncio
async def test_a_session_opened_without_the_claim_stays_without_it() -> None:
    minted = await _exchange(None)
    refresh = _cookie(minted, REFRESH)
    assert _refresh_claim(refresh) is None

    rotated = await _rotate(refresh)
    assert token_embed_parent_port(_cookie(rotated, ACCESS)) is None
    assert _refresh_claim(_cookie(rotated, REFRESH)) is None


@pytest.mark.parametrize("bad", ["0", "70000", "54x6", "\u00b2", "-1"])
@pytest.mark.asyncio
async def test_a_malformed_claim_is_dropped_not_carried(bad: str) -> None:
    refresh, *_ = generate_refresh_token("alice", embed_parent_port=bad)
    assert rt.refresh_token_embed_parent_port(refresh) == ""

    rotated = await _rotate(refresh)
    assert token_embed_parent_port(_cookie(rotated, ACCESS)) is None
    assert _refresh_claim(_cookie(rotated, REFRESH)) is None


def test_the_accessor_reads_nothing_from_an_undecodable_token() -> None:
    assert rt.refresh_token_embed_parent_port("not-a-token") == ""
    unmarked, *_ = generate_refresh_token("alice")
    assert rt.refresh_token_embed_parent_port(unmarked) == ""
