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
    # General route tests explicitly admit their synthetic third-party apps.
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
    )
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
async def test_uninstall_preserves_data_by_default(tmp_path, monkeypatch):
    home = _setup_env(tmp_path, monkeypatch)
    src = _make_app_source(tmp_path)
    install_app(src)
    data_file = home / "apps" / "api-test-app" / "data" / "state.json"
    data_file.write_text('{"saved": true}')

    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/api/apps/api-test-app/uninstall")
        assert resp.status == 200

        resp = await client.get("/api/apps/api-test-app")
        assert resp.status == 404

    assert data_file.read_text() == '{"saved": true}'


@pytest.mark.asyncio
async def test_uninstall_purges_data_only_with_explicit_action(tmp_path, monkeypatch):
    home = _setup_env(tmp_path, monkeypatch)
    src = _make_app_source(tmp_path)
    install_app(src)
    app_dir = home / "apps" / "api-test-app"
    (app_dir / "data" / "state.json").write_text('{"saved": true}')

    async with TestClient(TestServer(_make_app())) as client:
        # The legacy destructive field is ignored and fails closed.
        resp = await client.post(
            "/api/apps/api-test-app/uninstall", json={"keep_data": False}
        )
        assert resp.status == 200
    assert (app_dir / "data" / "state.json").is_file()

    # Reinstall over the preserved data, then prove malformed purge intent also
    # fails closed.
    install_app(src)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post(
            "/api/apps/api-test-app/uninstall", json={"purge_data": "true"}
        )
        assert resp.status == 200
    assert (app_dir / "data" / "state.json").is_file()

    # Reinstall again, then invoke the dedicated literal-boolean purge action.
    install_app(src)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post(
            "/api/apps/api-test-app/uninstall", json={"purge_data": True}
        )
        assert resp.status == 200

    assert not app_dir.exists()


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
async def test_uninstall_aborts_non_retryable_when_cron_store_unreadable(tmp_path, monkeypatch):
    """Same abort as the busy case, but reported NON-retryable and never retried.

    An unreadable store degrades the owned-job set to empty, so cleanup reports
    zero removed for a reason unrelated to ownership. Continuing deletes the app
    while its still-ENABLED jobs remain on disk to resume once the store parses.

    Two things differ from `CronStoreBusy` and both come from the code, not from
    symmetry. `handlers/cron.py` documents unreadable as `retryable: False` --
    "an unreadable file does not heal on its own, so a client that retries on busy
    must NOT retry on this" -- so the abort must not tell the caller to retry. And
    `_deregister_crons_with_retry` catches busy IN ORDER TO retry, so unreadable
    must pass through it untouched rather than burn every attempt on a store that
    cannot heal. The call count below is what pins that.
    """
    _setup_env(tmp_path, monkeypatch)
    src = _make_app_source(tmp_path)
    install_app(src)

    import kiro_crew.apps.routes as routes_mod
    from kiro_crew.cron import CronStoreUnreadable

    calls = {"n": 0}

    async def _unreadable(name, cron_service):
        calls["n"] += 1
        raise CronStoreUnreadable("refusing to write cron store: Move the unreadable file aside.")

    monkeypatch.setattr(routes_mod, "deregister_app_crons_from_service", _unreadable)
    monkeypatch.setattr(routes_mod, "_CRON_CLEANUP_BACKOFF_SECS", 0)

    app = _make_app()
    app["state"] = SimpleNamespace(crons=object())
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/apps/api-test-app/uninstall")
        assert resp.status == 409
        body = await resp.json()
        # The distinction handlers/cron.py keeps deliberately.
        assert body["retryable"] is False, body
        assert "Move the unreadable file aside" in body["error"], body

        # THE HARM: the app must still be installed. A test that only asserted
        # the log line or the 409 would pass under the defect too, because the
        # generic catch logs and then deletes the app anyway.
        resp = await client.get("/api/apps/api-test-app")
        assert resp.status == 200

    # NOT retried: an unreadable store does not heal, so burning all three
    # attempts on it would be wrong. This is why the retry wrapper is left alone.
    assert calls["n"] == 1


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
    manifest = json.loads((src / APP_MANIFEST_FILENAME).read_text(encoding="utf-8"))
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
        assert last_modified  # descriptor-derived validator (see _read_ui_file)

        # A revalidation request with the validator must yield 304 (no body).
        resp304 = await client.get(
            "/apps/ui-cache-app/ui/index.mjs",
            headers={"If-Modified-Since": last_modified},
        )
        assert resp304.status == 304


# ---------------------------------------------------------------------------
# Registration must run off the event loop (blocking KIROCREW_HOME filesystem
# work — manifest reads, skill symlink walks, mcp.json atomic writes — would
# otherwise freeze the gateway on a stalled mount).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_helper_dispatches_off_loop(monkeypatch):
    """_register_app_off_loop runs register_app on an executor thread and
    passes its return value through to the caller."""
    import threading

    import kiro_crew.apps.routes as routes_mod

    loop_thread = threading.current_thread()
    seen: dict[str, object] = {}
    sentinel = SimpleNamespace(ok=True)

    def _spy(name):
        seen["name"] = name
        seen["thread"] = threading.current_thread()
        return sentinel

    monkeypatch.setattr(routes_mod, "register_app", _spy)
    result = await routes_mod._register_app_off_loop("some-app")
    assert result is sentinel  # return value reaches the awaiting caller
    assert seen["name"] == "some-app"
    assert seen["thread"] is not loop_thread  # executor thread, not the loop


@pytest.mark.asyncio
async def test_deregister_helper_dispatches_off_loop(monkeypatch):
    """_deregister_app_off_loop runs deregister_app on an executor thread."""
    import threading

    import kiro_crew.apps.routes as routes_mod

    loop_thread = threading.current_thread()
    seen: dict[str, object] = {}
    sentinel = SimpleNamespace(ok=True)

    def _spy(name):
        seen["name"] = name
        seen["thread"] = threading.current_thread()
        return sentinel

    monkeypatch.setattr(routes_mod, "deregister_app", _spy)
    result = await routes_mod._deregister_app_off_loop("some-app")
    assert result is sentinel
    assert seen["name"] == "some-app"
    assert seen["thread"] is not loop_thread


@pytest.mark.asyncio
async def test_install_route_registers_off_loop(tmp_path, monkeypatch):
    """The install handler reaches register_app via the executor: the real
    registration call must not execute on the event-loop thread."""
    import threading

    import kiro_crew.apps.routes as routes_mod

    _setup_env(tmp_path, monkeypatch)
    src = _make_app_source(tmp_path)
    loop_thread = threading.current_thread()
    seen: dict[str, object] = {}
    real_register = routes_mod.register_app

    def _spy(name):
        seen["thread"] = threading.current_thread()
        return real_register(name)

    monkeypatch.setattr(routes_mod, "register_app", _spy)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/api/apps/install", json={"source": str(src)})
        assert resp.status == 201
        data = await resp.json()
        assert data["ok"] is True
        assert "registration" in data  # helper's return value still surfaces
    assert seen["thread"] is not loop_thread


@pytest.mark.asyncio
async def test_on_enable_gates_backend_start(tmp_path, monkeypatch):
    """onEnable must finish BEFORE the backend is spawned.

    An app whose onEnable installs its backend dependencies (npm install)
    loses any race with the backend's first boot — the backend exits on
    missing node_modules.
    """
    _setup_env(tmp_path, monkeypatch)
    src = _make_app_source(tmp_path)
    manifest = json.loads((src / APP_MANIFEST_FILENAME).read_text())
    manifest["setup"] = {"onEnable": "true"}
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2))
    install_app(src)

    order: list[str] = []

    async def _fake_script(name, script, *, timeout=30, action="lifecycle_script"):
        order.append(f"onEnable:{name}")
        return {"output": "", "failed": False}

    def _fake_backend(app_name):
        order.append(f"backend:{app_name}")
        return None

    monkeypatch.setattr("kiro_crew.apps.routes._run_lifecycle_script", _fake_script)
    monkeypatch.setattr("kiro_crew.apps.routes.start_app_backend", _fake_backend)

    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/api/apps/api-test-app/enable")
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True

    assert order == ["onEnable:api-test-app", "backend:api-test-app"], (
        f"onEnable must complete before start_app_backend, saw {order}"
    )


@pytest.mark.asyncio
async def test_enable_failure_before_backend_start_leaves_no_backend(
    tmp_path, monkeypatch
):
    """A failing onEnable rolls back before any backend process exists."""
    _setup_env(tmp_path, monkeypatch)
    # The failing script is a REAL bash child; allow it regardless of whether
    # this host can build a namespace sandbox (same convention as
    # test_apps_registry.py's unsandboxed_spawn fixture). Sandbox construction
    # itself is covered by test_sandbox_*.py.
    from kiro_crew import sandbox

    monkeypatch.setattr(sandbox, "_allow_unsandboxed_exec", lambda: True)
    src = _make_app_source(tmp_path)
    manifest = json.loads((src / APP_MANIFEST_FILENAME).read_text())
    manifest["setup"] = {"onEnable": "exit 1"}
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2))
    install_app(src)

    backend_started: list[str] = []

    def _fail_backend(app_name):
        backend_started.append(app_name)
        return None

    monkeypatch.setattr("kiro_crew.apps.routes.start_app_backend", _fail_backend)

    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/api/apps/api-test-app/enable")
        assert resp.status == 400
        data = await resp.json()
        assert data["code"] == "on_enable_failed"

    assert backend_started == [], "backend must not start when onEnable fails"

    from kiro_crew.apps.manager import _read_installed

    meta = _read_installed("api-test-app")
    assert meta is not None
    assert meta.enabled is False


def _on_enable_rewritten_app(tmp_path, monkeypatch, on_enable_body):
    """Install an app whose onEnable runs *on_enable_body* as real bash.

    Same unsandboxed convention as the scripted-install tests: these tests
    assert real bash semantics and must not depend on the host's sandbox
    backend.
    """
    from kiro_crew import sandbox

    monkeypatch.setattr(sandbox, "_allow_unsandboxed_exec", lambda: True)
    src = _make_app_source(tmp_path)
    manifest = json.loads((src / APP_MANIFEST_FILENAME).read_text())
    manifest["setup"] = {"onEnable": on_enable_body}
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2))
    install_app(src)


@pytest.mark.asyncio
async def test_on_enable_cannot_swap_manifest_identity(tmp_path, monkeypatch):
    """onEnable rewrites app.json → registration must be re-admitted first.

    A compromised hook with write access to the app directory could swap the
    admitted manifest (e.g. inject cron declarations) between admission and
    registration. The enable route must re-read the manifest, detect the
    identity change, and roll back before anything from the rewritten
    manifest is registered or booted.
    """
    _setup_env(tmp_path, monkeypatch)
    _on_enable_rewritten_app(
        tmp_path,
        monkeypatch,
        on_enable_body=(
            'printf \'{"name":"api-test-app","version":"9.9.9",'
            '"displayName":"Evil"}\' > app.json'
        ),
    )

    registered: list[str] = []
    monkeypatch.setattr(
        "kiro_crew.apps.routes.register_app",
        lambda name: registered.append(name),
    )
    backend_started: list[str] = []
    monkeypatch.setattr(
        "kiro_crew.apps.routes.start_app_backend",
        lambda app_name: backend_started.append(app_name),
    )

    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/api/apps/api-test-app/enable")
        assert resp.status == 400
        data = await resp.json()
        assert data["code"] == "on_enable_admission_denied"
        assert "9.9.9" in data["error"] or "manifest changed" in data["error"]

    assert registered == [], "nothing from the rewritten manifest may register"
    assert backend_started == [], "no backend may boot from a rewritten manifest"

    from kiro_crew.apps.manager import _read_installed

    meta = _read_installed("api-test-app")
    assert meta is not None
    assert meta.enabled is False


@pytest.mark.asyncio
async def test_on_enable_cannot_destroy_the_manifest(tmp_path, monkeypatch):
    """onEnable removes app.json → fail closed, not register from cache."""
    _setup_env(tmp_path, monkeypatch)
    _on_enable_rewritten_app(tmp_path, monkeypatch, on_enable_body="rm -f app.json")

    monkeypatch.setattr("kiro_crew.apps.routes.register_app", lambda name: None)

    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/api/apps/api-test-app/enable")
        assert resp.status == 400
        data = await resp.json()
        assert data["code"] == "on_enable_admission_denied"
        assert "manifest" in data["error"]

    from kiro_crew.apps.manager import _read_installed

    meta = _read_installed("api-test-app")
    assert meta is not None
    assert meta.enabled is False


@pytest.mark.asyncio
async def test_on_enable_admission_policy_denial_rolls_back(tmp_path, monkeypatch):
    """A post-onEnable admission denial rolls the enable back cleanly."""
    _setup_env(tmp_path, monkeypatch)
    _on_enable_rewritten_app(tmp_path, monkeypatch, on_enable_body="true")
    monkeypatch.setattr(
        "kiro_crew.apps.routes.app_admission_denied",
        lambda name, manifest=None, action="install": "unsigned app",
    )
    monkeypatch.setattr("kiro_crew.apps.routes.register_app", lambda name: None)

    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/api/apps/api-test-app/enable")
        assert resp.status == 400
        data = await resp.json()
        assert data["code"] == "on_enable_admission_denied"
        assert "unsigned app" in data["error"]

    from kiro_crew.apps.manager import _read_installed

    meta = _read_installed("api-test-app")
    assert meta is not None
    assert meta.enabled is False


@pytest.mark.asyncio
async def test_benign_on_enable_passes_readmission(tmp_path, monkeypatch):
    """A well-behaved onEnable leaves the manifest alone → enable succeeds."""
    _setup_env(tmp_path, monkeypatch)
    _on_enable_rewritten_app(tmp_path, monkeypatch, on_enable_body="true")
    monkeypatch.setattr(
        "kiro_crew.apps.routes.start_app_backend", lambda app_name: None
    )

    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post("/api/apps/api-test-app/enable")
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True

    from kiro_crew.apps.manager import _read_installed

    meta = _read_installed("api-test-app")
    assert meta is not None
    assert meta.enabled is True
