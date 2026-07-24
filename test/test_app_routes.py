"""Tests for kiro_crew.apps.routes — REST API endpoints."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, install_app
from kiro_crew.apps.routes import register_app_routes
from kiro_crew.cron import CronStoreBusy

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app_source(tmp_path, name="api-test-app"):
    src = tmp_path / "source" / name
    src.mkdir(parents=True)
    manifest = {
        "name": name,
        "version": "1.0.0",
        "displayName": "API Test App",
        "description": "App for API testing",
        "author": "tester",
    }
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2))
    return src


def _setup_env(tmp_path, monkeypatch):
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    kiro_agents = tmp_path / "kiro-agents"
    kiro_agents.mkdir()
    import kiro_crew.apps.bridges as bridges_mod
    monkeypatch.setattr(bridges_mod, "KIRO_AGENTS_DIR", kiro_agents)
    import kiro_crew.apps.backend as bmod
    bmod._processes.clear()
    bmod._allocated_ports.clear()
    return home


def _make_app():
    app = web.Application()
    register_app_routes(app)
    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_empty(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get("/api/apps")
        assert resp.status == 200
        data = await resp.json()
        assert data == []


@pytest.mark.asyncio
async def test_install_and_list(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    src = _make_app_source(tmp_path)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/api/apps/install", json={"source": str(src)})
        assert resp.status == 201
        data = await resp.json()
        assert data["ok"] is True

        resp = await client.get("/api/apps")
        data = await resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "api-test-app"


@pytest.mark.asyncio
async def test_install_missing_source(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/api/apps/install", json={"source": ""})
        assert resp.status == 400


@pytest.mark.asyncio
async def test_get_app(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    src = _make_app_source(tmp_path)
    install_app(src)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get("/api/apps/api-test-app")
        assert resp.status == 200
        data = await resp.json()
        assert data["name"] == "api-test-app"


@pytest.mark.asyncio
async def test_get_app_not_found(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get("/api/apps/nonexistent")
        assert resp.status == 404


@pytest.mark.asyncio
async def test_get_manifest(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    src = _make_app_source(tmp_path)
    install_app(src)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get("/api/apps/api-test-app/manifest")
        assert resp.status == 200
        data = await resp.json()
        assert data["name"] == "api-test-app"


@pytest.mark.asyncio
async def test_enable_disable(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    src = _make_app_source(tmp_path)
    install_app(src)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/api/apps/api-test-app/enable")
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True

        resp = await client.post("/api/apps/api-test-app/disable")
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True


@pytest.mark.asyncio
async def test_uninstall(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    src = _make_app_source(tmp_path)
    install_app(src)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/api/apps/api-test-app/uninstall")
        assert resp.status == 200

        resp = await client.get("/api/apps/api-test-app")
        assert resp.status == 404


@pytest.mark.asyncio
async def test_uninstall_aborts_409_when_cron_cleanup_busy(tmp_path, monkeypatch):
    """Uninstall must ABORT (retryable 409) when app-cron cleanup cannot
    complete, instead of logging and proceeding.

    Uninstall is irreversible: past this point the per-app cron manifest is
    dropped and the app directory is deleted. If owned jobs are still persisted
    and ENABLED then, they become permanent orphans that keep firing their
    command/script/agent payload with no owning app left to clean them up. So
    the app must stay installed and the uninstall be retryable.
    """
    _setup_env(tmp_path, monkeypatch)
    src = _make_app_source(tmp_path)
    install_app(src)

    import kiro_crew.apps.routes as routes_mod

    calls = {"n": 0}

    async def _busy(name, cron_service):
        calls["n"] += 1
        raise CronStoreBusy("store busy")

    monkeypatch.setattr(routes_mod, "deregister_app_crons_from_service", _busy)
    monkeypatch.setattr(routes_mod, "_CRON_CLEANUP_BACKOFF_SECS", 0)

    app = _make_app()
    app["state"] = SimpleNamespace(crons=object())
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/apps/api-test-app/uninstall")
        assert resp.status == 409
        body = await resp.json()
        assert body["retryable"] is True
        assert "cron" in body["error"].lower()

        # The app is STILL INSTALLED — nothing was torn down, so a retry can
        # complete the cleanup rather than leaving orphans behind.
        resp = await client.get("/api/apps/api-test-app")
        assert resp.status == 200

    # Transient contention is retried before the abort is surfaced.
    assert calls["n"] == routes_mod._CRON_CLEANUP_ATTEMPTS


@pytest.mark.asyncio
async def test_uninstall_retries_then_succeeds_on_transient_cron_busy(
    tmp_path, monkeypatch
):
    """A single unlucky lock collision must not fail the user's uninstall."""
    _setup_env(tmp_path, monkeypatch)
    src = _make_app_source(tmp_path)
    install_app(src)

    import kiro_crew.apps.routes as routes_mod

    calls = {"n": 0}

    async def _busy_once(name, cron_service):
        calls["n"] += 1
        if calls["n"] == 1:
            raise CronStoreBusy("store busy")
        return 2

    monkeypatch.setattr(routes_mod, "deregister_app_crons_from_service", _busy_once)
    monkeypatch.setattr(routes_mod, "_CRON_CLEANUP_BACKOFF_SECS", 0)

    app = _make_app()
    app["state"] = SimpleNamespace(crons=object())
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/apps/api-test-app/uninstall")
        assert resp.status == 200
        resp = await client.get("/api/apps/api-test-app")
        assert resp.status == 404
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_uninstall_cron_busy_runs_no_destructive_step_before_abort(
    tmp_path, monkeypatch
):
    """ORDERING regression: when cron cleanup fails, the uninstall aborts
    (retryable 409) BEFORE anything destructive runs.

    Cron cleanup is the FIRST precondition, so on a contended store neither the
    (possibly destructive, non-idempotent) onUninstall script NOR the backend
    stop may have executed, and the app must still be installed. If either ran
    before the abort, the retryable 409's "app is still installed; retry"
    message would be false in spirit and the retry would re-run a
    non-idempotent teardown. We spy both and assert zero calls.
    """
    _setup_env(tmp_path, monkeypatch)

    # App source WITH an onUninstall script declared, so a wrong ordering
    # (cleanup after the script) would actually invoke it — making this test
    # non-vacuous.
    src = tmp_path / "source" / "api-test-app"
    src.mkdir(parents=True)
    manifest = {
        "name": "api-test-app",
        "version": "1.0.0",
        "displayName": "API Test App",
        "description": "App for API testing",
        "author": "tester",
        "setup": {"onUninstall": "echo tearing-down"},
    }
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2))
    install_app(src)

    import kiro_crew.apps.routes as routes_mod

    async def _busy(name, cron_service):
        raise CronStoreBusy("store busy")

    script_calls = {"n": 0}
    stop_calls = {"n": 0}

    async def _spy_script(*args, **kwargs):
        script_calls["n"] += 1
        return {"output": "", "failed": False}

    def _spy_stop(name):
        stop_calls["n"] += 1

    monkeypatch.setattr(routes_mod, "deregister_app_crons_from_service", _busy)
    monkeypatch.setattr(routes_mod, "_CRON_CLEANUP_BACKOFF_SECS", 0)
    monkeypatch.setattr(routes_mod, "_run_lifecycle_script", _spy_script)
    monkeypatch.setattr(routes_mod, "stop_app_backend", _spy_stop)

    app = _make_app()
    app["state"] = SimpleNamespace(crons=object())
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/apps/api-test-app/uninstall")
        assert resp.status == 409
        body = await resp.json()
        assert body["retryable"] is True

        # Nothing destructive ran before the abort.
        assert script_calls["n"] == 0, "onUninstall must NOT run before cron cleanup"
        assert stop_calls["n"] == 0, "backend must NOT be stopped before cron cleanup"

        # And the app is still installed, so the retry is safe.
        resp = await client.get("/api/apps/api-test-app")
        assert resp.status == 200


# ---------------------------------------------------------------------------
# UI file serving — cache policy
# ---------------------------------------------------------------------------

def _make_app_source_with_ui(tmp_path, name="ui-cache-app"):
    src = _make_app_source(tmp_path, name)
    ui = src / "ui"
    ui.mkdir()
    (ui / "index.mjs").write_text("export default function App() { return null }\n")
    manifest = json.loads((src / APP_MANIFEST_FILENAME).read_text())
    manifest["ui"] = {"entry": "index.mjs", "pages": [{"route": f"/{name}", "label": name}]}
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2))
    return src


@pytest.mark.asyncio
async def test_ui_file_no_cache_revalidation(tmp_path, monkeypatch):
    """App UI files are served with Cache-Control: no-cache so browsers
    revalidate every load (app updates / dev edits show on plain refresh),
    and conditional requests get a body-less 304 so unchanged files stay cheap.

    Regression: the previous ``public, max-age=3600`` served every app's UI
    stale for up to an hour after an update.
    """
    _setup_env(tmp_path, monkeypatch)
    install_app(str(_make_app_source_with_ui(tmp_path)))
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get("/apps/ui-cache-app/ui/index.mjs")
        assert resp.status == 200
        assert resp.headers.get("Cache-Control") == "no-cache"
        assert "max-age" not in resp.headers.get("Cache-Control", "")
        last_modified = resp.headers.get("Last-Modified")
        assert last_modified  # FileResponse provides the validator

        # A revalidation request with the validator must yield 304 (no body).
        resp304 = await client.get(
            "/apps/ui-cache-app/ui/index.mjs",
            headers={"If-Modified-Since": last_modified},
        )
        assert resp304.status == 304
