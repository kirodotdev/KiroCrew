"""An app token is accepted only while its app is installed and enabled.

The token is HMAC-signed with the gateway key, so its signature alone stays
valid after the owner disables or uninstalls the app. These tests run a real
app token through the real ``token_auth_middleware`` against a real
``installed.json`` record and pin that:

* a disabled or uninstalled app's token is refused the way an invalid token is;
* a token minted before a disable stays refused after the app is enabled again,
  while a fresh exchange works;
* an enabled app's token and a dashboard token are unaffected.

One injected clock drives both the token mint and the record write, so the
order of "minted" and "disabled" is set by the test, never by timer resolution.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import json
import math
import os
import threading
import time
import types

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps import manager_records, protected_app_state
from kiro_crew.apps.manager_records import InstalledApp, _write_installed, app_dir
from kiro_crew.dashboard import revocation_gen, token_auth

_APP = "token-app"


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def module(self) -> types.ModuleType:
        fake = types.ModuleType("fake_time")
        fake.__dict__.update(time.__dict__)
        fake.time = lambda: self.now  # type: ignore[attr-defined]
        fake.monotonic = lambda: self.now  # type: ignore[attr-defined]
        return fake

    def tick(self) -> None:
        self.now += 1.0


@pytest.fixture
def clock(tmp_path, monkeypatch) -> _Clock:
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.apps.manager_records.config_dir", lambda: tmp_path)
    monkeypatch.setattr(token_auth, "_get_secret", lambda: b"app-token-disabled-key")
    monkeypatch.setattr(token_auth, "_state", token_auth.TokenStateManager())
    monkeypatch.setattr(token_auth, "_app_perms_cache", {})
    monkeypatch.setattr(token_auth, "_revoked_store_singleton", None)
    monkeypatch.setattr(revocation_gen, "_gen", 0)
    c = _Clock()
    fake = c.module()
    # Through the facade: ``time`` is bound by several token_auth owners.
    monkeypatch.setattr("kiro_crew.dashboard.token_auth.time", fake)
    monkeypatch.setattr("kiro_crew.apps.manager_records.time", fake)
    return c


def _set_enabled(clock: _Clock, enabled: bool) -> None:
    clock.tick()
    if enabled:
        # The owner's enable ends their disable, as ``enable_app`` does.
        protected_app_state.set_owner_disabled(_APP, False)
    _write_installed(_APP, InstalledApp(name=_APP, version="1.0.0", enabled=enabled))
    clock.tick()


def _mint(clock: _Clock) -> str:
    clock.tick()
    return token_auth.generate_token(_APP, app=_APP)


def _valid(token: str) -> bool:
    return token_auth.validate_token_with_app(token)[0]


def test_a_disabled_apps_token_is_refused_like_an_invalid_one(clock) -> None:
    _set_enabled(clock, True)
    token = _mint(clock)
    assert _valid(token)

    _set_enabled(clock, False)
    assert token_auth.validate_token_with_app(token) == (False, "", "invalid signature", "")


def test_an_uninstalled_apps_token_is_refused(clock) -> None:
    _set_enabled(clock, True)
    token = _mint(clock)
    (app_dir(_APP) / "installed.json").unlink()

    assert not _valid(token)


def test_a_token_from_before_a_disable_stays_dead_after_reenable(clock) -> None:
    _set_enabled(clock, True)
    old = _mint(clock)
    _set_enabled(clock, False)
    _set_enabled(clock, True)

    assert not _valid(old)
    assert _valid(_mint(clock))


def test_an_enabled_app_and_a_dashboard_token_are_unaffected(clock) -> None:
    _set_enabled(clock, True)
    token = _mint(clock)
    _set_enabled(clock, True)

    assert _valid(token)
    assert token_auth.validate_token_with_app(token_auth.generate_token("owner"))[:2] == (
        True,
        "owner",
    )


def test_a_token_minted_in_the_disables_own_tick_stays_dead(clock) -> None:
    _set_enabled(clock, True)
    clock.tick()
    # A coarse clock gives the mint and the disable the same timestamp.
    same_tick = token_auth.generate_token(_APP, app=_APP)
    _write_installed(_APP, InstalledApp(name=_APP, version="1.0.0", enabled=False))
    _set_enabled(clock, True)

    assert not _valid(same_tick)


def test_a_token_minted_one_tick_after_the_cutoff_is_accepted(clock) -> None:
    _set_enabled(clock, True)
    clock.tick()
    _write_installed(_APP, InstalledApp(name=_APP, version="1.0.0", enabled=False))
    protected_app_state.set_owner_disabled(_APP, False)
    _write_installed(_APP, InstalledApp(name=_APP, version="1.0.0", enabled=True))
    clock.tick()

    assert _valid(token_auth.generate_token(_APP, app=_APP))


def test_a_rewritten_record_does_not_reopen_tokens_the_owner_disabled(clock) -> None:
    _set_enabled(clock, True)
    _set_enabled(clock, False)
    # The app rewrites its own record to enabled; the owner's disable still stands.
    _write_installed(_APP, InstalledApp(name=_APP, version="1.0.0", enabled=True))
    clock.tick()

    assert not _valid(_mint(clock))


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a POSIX FIFO")
def test_a_fifo_in_place_of_the_record_refuses_the_token_without_blocking(clock) -> None:
    _set_enabled(clock, True)
    token = _mint(clock)
    record = app_dir(_APP) / "installed.json"
    record.unlink()
    os.mkfifo(record)

    # A blocking open would never return; the bound turns that into a failure.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        verdict = pool.submit(token_auth.validate_token_with_app, token).result(timeout=30)
    finally:
        # Never wait on a worker stuck in open(): the timeout above is the verdict.
        pool.shutdown(wait=False, cancel_futures=True)
    assert verdict[0] is False


def test_an_integer_cutoff_too_large_for_a_float_refuses_tokens_and_still_disables(
    clock,
) -> None:
    _set_enabled(clock, True)
    token = _mint(clock)
    record = app_dir(_APP) / "installed.json"
    data = json.loads(record.read_text(encoding="utf-8"))
    data["appTokensNotBefore"] = 10**400
    record.write_text(json.dumps(data), encoding="utf-8")

    assert not _valid(token)
    assert InstalledApp.from_dict(data).appTokensNotBefore == math.inf
    _set_enabled(clock, False)
    assert json.loads(record.read_text(encoding="utf-8"))["enabled"] is False


def _count_reads(monkeypatch) -> list[int]:
    reads: list[int] = []
    real = manager_records.app_token_cutoff

    def _counting(name: str):
        reads.append(threading.get_ident())
        return real(name)

    # The check reads it through the ``apps.manager`` facade.
    monkeypatch.setattr("kiro_crew.apps.manager.app_token_cutoff", _counting)
    return reads


def test_a_disable_from_another_process_is_seen_on_the_next_check(clock, monkeypatch) -> None:
    _set_enabled(clock, True)
    token = _mint(clock)
    reads = _count_reads(monkeypatch)
    assert _valid(token) and _valid(token)
    assert len(reads) == 2, "the record is read on every check, nothing is cached"

    # The CLI disables the app by writing the record; no time passes.
    record = app_dir(_APP) / "installed.json"
    data = json.loads(record.read_text(encoding="utf-8"))
    record.write_text(json.dumps({**data, "enabled": False}), encoding="utf-8")
    assert not _valid(token)


def test_an_unreadable_record_refuses_the_token(clock, monkeypatch) -> None:
    _set_enabled(clock, True)
    token = _mint(clock)
    (app_dir(_APP) / "installed.json").write_text("{not json", encoding="utf-8")

    assert not _valid(token)


async def _ok(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


@contextlib.asynccontextmanager
async def _client():
    app = web.Application(middlewares=[token_auth.token_auth_middleware()])
    app.router.add_get("/api/apps/{name}/manifest", _ok)
    async with TestClient(TestServer(app)) as client:
        yield client


@pytest.mark.asyncio
async def test_middleware_refuses_a_disabled_apps_token_on_its_own_route(clock) -> None:
    _set_enabled(clock, True)
    token = _mint(clock)
    async with _client() as client:

        async def status(tok: str) -> int:
            resp = await client.get(f"/api/apps/{_APP}/manifest", params={"token": tok})
            return resp.status

        assert await status(token) == 200
        _set_enabled(clock, False)
        assert await status(token) == 403
        _set_enabled(clock, True)
        assert await status(token) == 403
        assert await status(_mint(clock)) == 200


@pytest.mark.asyncio
async def test_middleware_reads_the_record_off_the_event_loop(clock, monkeypatch) -> None:
    _set_enabled(clock, True)
    token = _mint(clock)
    reads = _count_reads(monkeypatch)
    loop_thread = threading.get_ident()
    async with _client() as client:
        resp = await client.get(f"/api/apps/{_APP}/manifest", params={"token": token})
        assert resp.status == 200

    assert reads and loop_thread not in reads


@pytest.mark.asyncio
async def test_a_cookie_exchanged_before_a_disable_stays_dead_after_reenable(clock) -> None:
    _set_enabled(clock, True)
    token = _mint(clock)

    async with _client() as client:
        resp = await client.get(f"/api/apps/{_APP}/manifest", params={"token": token})
        assert resp.status == 200
        cookie = next(iter(client.session.cookie_jar))
        _set_enabled(clock, False)
        _set_enabled(clock, True)
        resp = await client.get(f"/api/apps/{_APP}/manifest", cookies={cookie.key: cookie.value})
        assert resp.status == 403


@pytest.mark.asyncio
async def test_a_cookie_minted_across_a_disable_stays_dead_after_reenable(
    clock, monkeypatch
) -> None:
    # The token passes the check, the owner's disable lands, then the exchange
    # mints the cookie: the cookie must be judged by the token's issue time.
    _set_enabled(clock, True)
    token = _mint(clock)
    real = token_auth.validate_token_with_app_off_loop
    raced: list[bool] = []

    async def _check_then_disable(tok: str, **kwargs):
        verdict = await real(tok, **kwargs)
        if not raced:
            raced.append(True)
            _set_enabled(clock, False)
        return verdict

    monkeypatch.setattr(token_auth, "validate_token_with_app_off_loop", _check_then_disable)
    async with _client() as client:
        resp = await client.get(f"/api/apps/{_APP}/manifest", params={"token": token})
        assert resp.status == 200
        cookie = next(iter(client.session.cookie_jar))
        monkeypatch.setattr(token_auth, "validate_token_with_app_off_loop", real)
        _set_enabled(clock, True)
        resp = await client.get(f"/api/apps/{_APP}/manifest", cookies={cookie.key: cookie.value})
        assert resp.status == 403


def test_a_token_from_before_an_uninstall_stays_dead_after_a_reinstall(clock) -> None:
    from kiro_crew.apps.manager_uninstall import uninstall_app

    _set_enabled(clock, True)
    old = _mint(clock)
    assert uninstall_app(_APP).ok
    assert not _valid(old)
    _set_enabled(clock, True)

    assert not _valid(old)
    assert _valid(_mint(clock))


def test_an_owner_disable_of_a_record_already_marked_off_moves_the_cutoff(clock) -> None:
    # The app can write enabled:false itself; the owner's disable must still
    # close the tokens minted before it.
    from kiro_crew.apps.manager_activation import disable_app

    _set_enabled(clock, True)
    old = _mint(clock)
    meta_path = app_dir(_APP) / "installed.json"
    record = json.loads(meta_path.read_text(encoding="utf-8"))
    meta_path.write_text(json.dumps({**record, "enabled": False}), encoding="utf-8")
    clock.tick()

    assert disable_app(_APP).ok
    _set_enabled(clock, True)

    assert not _valid(old)
    assert _valid(_mint(clock))


def _forged(app: str) -> str:
    payload = token_auth._b64url_encode(json.dumps({"app": app}).encode())
    return f"{payload}.not-a-signature"


def test_a_forged_tokens_app_claim_reads_nothing(clock, monkeypatch) -> None:
    reads = _count_reads(monkeypatch)

    assert not _valid(_forged(_APP))
    assert not _valid(_forged("../../outside"))
    assert reads == []


def test_a_signed_token_with_an_invalid_app_name_reads_nothing(clock, monkeypatch) -> None:
    reads = _count_reads(monkeypatch)
    token = token_auth.generate_token("x", app="../../outside")

    assert not _valid(token)
    assert reads == []
