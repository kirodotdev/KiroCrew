"""Routes for installing an app from Kiro Space: prepare, then confirm.

``POST /api/apps/market/install`` verifies and unpacks into staging and returns
the confirm data plus an opaque, server-issued staging id.
``POST /api/apps/market/install/confirm`` installs what that prepare step
verified (never a new download) or, with ``confirmed: false``, discards it.

The listing client is injected through ``app["market_client_factory"]``, so these
tests drive the routes with an in-memory fake and no network.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from app_manager_helpers import app_home_fixture  # noqa: F401 - fixture
from test_apps_marketplace_install import REF, FakeMarketClient, _app_files, _zip

from kiro_crew.apps.marketplace_install import MarketInstallError, market_staging_root
from kiro_crew.apps.routes import register_app_routes

PREPARE = "/api/apps/market/install"
CONFIRM = "/api/apps/market/install/confirm"


@pytest.fixture(name="app_home")
def _app_home(app_home_fixture: Path) -> Path:  # noqa: F811
    return app_home_fixture


def _make_app(
    client: Any = None,
    *,
    user: str | None = "owner",
    app_token: str = "",
    factory: Callable[[], Any] | None = None,
) -> web.Application:
    @web.middleware
    async def _identity(request: web.Request, handler: Any) -> web.StreamResponse:
        request["app"] = app_token
        if user is not None:
            request["user"] = user
        return await handler(request)

    app = web.Application(middlewares=[_identity])
    app["state"] = SimpleNamespace(owner_id="owner", broadcast_ws=lambda *a, **k: None)
    if factory is not None:
        app["market_client_factory"] = factory
    elif client is not None:
        app["market_client_factory"] = lambda: client
    register_app_routes(app)
    return app


def _client() -> FakeMarketClient:
    return FakeMarketClient(payload=_zip(_app_files()))


def _staged() -> list[Path]:
    root = market_staging_root()
    return sorted(root.iterdir()) if root.exists() else []


async def _prepare(http: TestClient, **body: Any) -> dict[str, Any]:
    resp = await http.post(PREPARE, json={"ref": REF, **body})
    assert resp.status == 200, await resp.text()
    return await resp.json()


# ---------------------------------------------------------------------------
# Prepare
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prepare_returns_confirm_data_and_opaque_id(app_home: Path) -> None:
    fake = _client()
    async with TestClient(TestServer(_make_app(fake))) as http:
        body = await _prepare(http)
    assert body["confirm"]["app"]["ref"] == REF
    assert body["confirm"]["trustLabel"]
    staging_id = body["stagingId"]
    assert isinstance(staging_id, str) and staging_id
    assert "/" not in staging_id and ".." not in staging_id
    # The id is not the folder name, and no filesystem path is exposed.
    assert staging_id not in {p.name for p in _staged()}
    assert str(app_home) not in json.dumps(body)


@pytest.mark.asyncio
async def test_prepare_installs_nothing(app_home: Path) -> None:
    from kiro_crew.apps.manager import get_app

    async with TestClient(TestServer(_make_app(_client()))) as http:
        await _prepare(http)
    assert get_app("ticket-digest") is None


@pytest.mark.asyncio
async def test_prepare_refusal_maps_code_and_leaves_no_staging(app_home: Path) -> None:
    fake = FakeMarketClient(payload=_zip(_app_files()), download_sha="0" * 64)
    async with TestClient(TestServer(_make_app(fake))) as http:
        resp = await http.post(PREPARE, json={"ref": REF})
        assert resp.status == 400
        assert (await resp.json())["code"] == "market_sha256_mismatch"
    assert _staged() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"ref": 7},
        {"ref": "not a ref"},
        {"ref": REF, "version": True},
        {"ref": REF, "version": "3"},
        {"ref": REF, "version": 0},
        [],
    ],
)
async def test_prepare_rejects_malformed_body(app_home: Path, body: Any) -> None:
    fake = _client()
    async with TestClient(TestServer(_make_app(fake))) as http:
        resp = await http.post(PREPARE, json=body)
        assert resp.status == 400
    assert fake.calls == []
    assert _staged() == []


@pytest.mark.asyncio
async def test_prepare_client_failure_is_502_without_detail(app_home: Path) -> None:
    class Boom:
        def resolve_install(self, ref: str, version: int | None) -> Any:
            raise RuntimeError("secret-host.internal exploded")

    async with TestClient(TestServer(_make_app(Boom()))) as http:
        resp = await http.post(PREPARE, json={"ref": REF})
        assert resp.status == 502
        assert "secret-host" not in await resp.text()
    assert _staged() == []


@pytest.mark.asyncio
async def test_prepare_without_a_configured_client_is_503(app_home: Path) -> None:
    async with TestClient(TestServer(_make_app())) as http:
        resp = await http.post(PREPARE, json={"ref": REF})
        assert resp.status == 503
        assert (await resp.json())["code"] == "market_client_unavailable"


@pytest.mark.asyncio
async def test_factory_error_is_surfaced_as_its_code(app_home: Path) -> None:
    def factory() -> Any:
        raise MarketInstallError("market_not_signed_in", "sign in first")

    async with TestClient(TestServer(_make_app(factory=factory))) as http:
        resp = await http.post(PREPARE, json={"ref": REF})
        assert resp.status == 503
        assert (await resp.json())["code"] == "market_not_signed_in"


# ---------------------------------------------------------------------------
# Owner-only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user", "app_token"),
    [("channel-user", ""), (None, ""), ("owner", "some-app"), ("some-app", "some-app")],
    ids=["non_owner", "unauthenticated", "app_token_as_owner", "app_token"],
)
@pytest.mark.parametrize("path", [PREPARE, CONFIRM])
async def test_non_owner_is_refused_before_any_side_effect(
    app_home: Path, user: str | None, app_token: str, path: str
) -> None:
    fake = _client()
    async with TestClient(TestServer(_make_app(fake, user=user, app_token=app_token))) as http:
        resp = await http.post(path, json={"ref": REF, "stagingId": "a" * 22, "confirmed": True})
        assert resp.status == 403
        assert (await resp.json())["code"] == "owner_only"
    assert fake.calls == []
    assert _staged() == []


@pytest.mark.asyncio
async def test_non_owner_cannot_confirm_or_discard_an_owner_staging(app_home: Path) -> None:
    from kiro_crew.apps.manager import get_app

    fake = _client()
    app = _make_app(fake)
    async with TestClient(TestServer(app)) as http:
        staging_id = (await _prepare(http))["stagingId"]
        staged = _staged()
        assert len(staged) == 1

        # Same registry, different caller.
        other = _make_app(fake, user="channel-user")
        other["market_staging"] = app["market_staging"]
        async with TestClient(TestServer(other)) as other_http:
            for confirmed in (True, False):
                resp = await other_http.post(
                    CONFIRM, json={"stagingId": staging_id, "confirmed": confirmed}
                )
                assert resp.status == 403
        assert _staged() == staged
    assert get_app("ticket-digest") is None


# ---------------------------------------------------------------------------
# Staging id validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_staging_id_is_refused(app_home: Path) -> None:
    from kiro_crew.apps.manager import get_app

    async with TestClient(TestServer(_make_app(_client()))) as http:
        resp = await http.post(CONFIRM, json={"stagingId": "A" * 22, "confirmed": True})
        assert resp.status == 404
        assert (await resp.json())["code"] == "market_staging_unknown"
    assert get_app("ticket-digest") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_id",
    [
        "",
        "../../sentinel",
        "..",
        "a/b",
        "a\\b",
        "/etc/passwd",
        "install-abc123",
        "a" * 200,
        "short",
        "ok-but-has-space-in-it-1234",
        "id\x00nul-aaaaaaaaaaaaaaaaa",
        7,
        None,
        ["a" * 22],
    ],
)
@pytest.mark.parametrize("confirmed", [True, False])
async def test_malformed_staging_id_is_refused_without_filesystem_access(
    app_home: Path, monkeypatch: pytest.MonkeyPatch, bad_id: Any, confirmed: bool
) -> None:
    import shutil

    sentinel = app_home / "sentinel"
    sentinel.mkdir()
    (sentinel / "keep").write_text("x")
    removed: list[Any] = []
    real = shutil.rmtree
    monkeypatch.setattr(shutil, "rmtree", lambda p, *a, **k: (removed.append(p), real(p, *a, **k)))

    async with TestClient(TestServer(_make_app(_client()))) as http:
        resp = await http.post(CONFIRM, json={"stagingId": bad_id, "confirmed": confirmed})
        assert resp.status == 400
        assert (await resp.json())["code"] == "market_staging_id_invalid"
    assert removed == []
    assert (sentinel / "keep").read_text() == "x"


@pytest.mark.asyncio
async def test_confirm_rejects_missing_or_non_boolean_confirmed(app_home: Path) -> None:
    async with TestClient(TestServer(_make_app(_client()))) as http:
        staging_id = (await _prepare(http))["stagingId"]
        for body in ({"stagingId": staging_id}, {"stagingId": staging_id, "confirmed": "yes"}):
            resp = await http.post(CONFIRM, json=body)
            assert resp.status == 400
        # Still staged and still confirmable.
        assert len(_staged()) == 1
        resp = await http.post(CONFIRM, json={"stagingId": staging_id, "confirmed": False})
        assert resp.status == 200


# ---------------------------------------------------------------------------
# Confirm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_installs_disabled_without_downloading_again(app_home: Path) -> None:
    from kiro_crew.apps.manager import get_app

    fake = _client()
    async with TestClient(TestServer(_make_app(fake))) as http:
        staging_id = (await _prepare(http))["stagingId"]
        calls_after_prepare = list(fake.calls)
        resp = await http.post(CONFIRM, json={"stagingId": staging_id, "confirmed": True})
        assert resp.status == 201, await resp.text()
        body = await resp.json()
    assert body["ok"] is True
    assert fake.calls == calls_after_prepare  # nothing was fetched at confirm
    info = get_app("ticket-digest")
    assert info is not None
    assert not info.get("enabled")
    assert info["source"].startswith("market:")
    assert info["sourceCommit"] == __import__("hashlib").sha256(fake.payload).hexdigest()
    assert _staged() == []


@pytest.mark.asyncio
async def test_confirm_is_single_use(app_home: Path) -> None:
    async with TestClient(TestServer(_make_app(_client()))) as http:
        staging_id = (await _prepare(http))["stagingId"]
        first = await http.post(CONFIRM, json={"stagingId": staging_id, "confirmed": True})
        assert first.status == 201
        second = await http.post(CONFIRM, json={"stagingId": staging_id, "confirmed": True})
        assert second.status == 404


@pytest.mark.asyncio
async def test_install_failure_still_removes_staging(
    app_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.apps import marketplace_install
    from kiro_crew.apps.manager import AppResult

    monkeypatch.setattr(
        marketplace_install,
        "install_app",
        lambda *a, **k: AppResult(ok=False, error="nope", error_code="x"),
    )
    async with TestClient(TestServer(_make_app(_client()))) as http:
        staging_id = (await _prepare(http))["stagingId"]
        resp = await http.post(CONFIRM, json={"stagingId": staging_id, "confirmed": True})
        assert resp.status == 400
    assert _staged() == []


# ---------------------------------------------------------------------------
# Discard and timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discard_removes_staging_and_is_idempotent(app_home: Path) -> None:
    from kiro_crew.apps.manager import get_app

    async with TestClient(TestServer(_make_app(_client()))) as http:
        staging_id = (await _prepare(http))["stagingId"]
        assert len(_staged()) == 1
        first = await http.post(CONFIRM, json={"stagingId": staging_id, "confirmed": False})
        assert first.status == 200
        assert (await first.json())["discarded"] is True
        assert _staged() == []
        again = await http.post(CONFIRM, json={"stagingId": staging_id, "confirmed": False})
        assert again.status == 200
        assert (await again.json())["discarded"] is False
        # A discarded id cannot be confirmed.
        gone = await http.post(CONFIRM, json={"stagingId": staging_id, "confirmed": True})
        assert gone.status == 404
    assert get_app("ticket-digest") is None


@pytest.mark.asyncio
async def test_staging_is_removed_after_the_timeout(
    app_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.apps import routes_market_install

    monkeypatch.setattr(routes_market_install, "STAGING_TTL_SECONDS", 0.2)
    async with TestClient(TestServer(_make_app(_client()))) as http:
        staging_id = (await _prepare(http))["stagingId"]
        assert len(_staged()) == 1
        for _ in range(50):
            await asyncio.sleep(0.1)
            if not _staged():
                break
        assert _staged() == []
        resp = await http.post(CONFIRM, json={"stagingId": staging_id, "confirmed": True})
        assert resp.status == 404


@pytest.mark.asyncio
async def test_expired_entry_is_never_confirmed_even_if_the_timer_has_not_run(
    app_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.apps import routes_market_install
    from kiro_crew.apps.manager import get_app

    clock = [1000.0]
    monkeypatch.setattr(routes_market_install, "_now", lambda: clock[0])
    async with TestClient(TestServer(_make_app(_client()))) as http:
        staging_id = (await _prepare(http))["stagingId"]
        clock[0] += routes_market_install.STAGING_TTL_SECONDS + 1
        resp = await http.post(CONFIRM, json={"stagingId": staging_id, "confirmed": True})
        assert resp.status == 404
    assert get_app("ticket-digest") is None
    assert _staged() == []


@pytest.mark.asyncio
async def test_shutdown_removes_pending_staging(app_home: Path) -> None:
    async with TestClient(TestServer(_make_app(_client()))) as http:
        await _prepare(http)
        assert len(_staged()) == 1
    assert _staged() == []


@pytest.mark.asyncio
async def test_pending_staging_is_bounded(app_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.apps import routes_market_install

    monkeypatch.setattr(routes_market_install, "MAX_PENDING", 2)
    async with TestClient(TestServer(_make_app(_client()))) as http:
        await _prepare(http)
        await _prepare(http)
        resp = await http.post(PREPARE, json={"ref": REF})
        assert resp.status == 429
        assert (await resp.json())["code"] == "market_install_too_many_pending"
        assert len(_staged()) == 2


# ---------------------------------------------------------------------------
# Route table: the new first segment is reserved
# ---------------------------------------------------------------------------


def test_market_is_a_reserved_first_segment_everywhere() -> None:
    from kiro_crew.apps.manifest import RESERVED_APP_PATH_SEGMENTS as manifest_segments
    from kiro_crew.dashboard.token_auth import RESERVED_APP_PATH_SEGMENTS as token_segments
    from kiro_crew.dashboard.token_auth import _app_owns_path

    assert "market" in manifest_segments
    assert "market" in token_segments
    assert not _app_owns_path("market", "/api/apps/market/install")
    assert not _app_owns_path("market", "/api/apps/market/install/confirm")
    mirror = (
        Path(__file__).resolve().parent.parent / "website" / "src" / "apps" / "coreAppRoutes.ts"
    ).read_text(encoding="utf-8")
    assert "'market'" in mirror


def test_routes_are_registered_before_the_name_catch_all() -> None:
    app = _make_app()
    resources = [r.canonical for r in app.router.resources()]
    assert PREPARE in resources and CONFIRM in resources
    assert resources.index(PREPARE) < resources.index("/api/apps/{name}")
    assert resources.index(CONFIRM) < resources.index("/api/apps/{name}")
