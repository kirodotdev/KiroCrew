"""Tests for kiro_crew.apps.dev_mode — app dev-mode live reload."""
from __future__ import annotations

import json
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from conftest import make_dir_link
from kiro_crew.apps.dev_mode import (
    _read_dev_sentinel,
    _reconcile_sentinel_from_installed,
    _scan_installed_dev_apps,
    _scan_ui_mtimes,
    is_dev_mode,
    is_dev_mode_cached,
    set_dev_mode,
)
from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
    _read_installed,
    install_app,
    update_app,
)
from kiro_crew.apps.routes import register_app_routes


@pytest.fixture(autouse=True)
def _reset_dev_cache():
    """Reset the module-global in-memory dev-app cache around every test.

    ``_dev_apps_cache`` persists across tests (module global); without this
    reset a dev app enabled in one test would leak into the next and flip an
    unrelated Cache-Control assertion.
    """
    import kiro_crew.apps.dev_mode as dev_mode
    dev_mode._set_dev_cache(set())
    yield
    dev_mode._set_dev_cache(set())


def _make_app_source(tmp_path, name="dev-mode-app"):
    src = tmp_path / "source" / name
    (src / "ui").mkdir(parents=True)
    (src / "ui" / "index.mjs").write_text("export default () => null\n")
    manifest = {
        "name": name,
        "version": "1.0.0",
        "displayName": "Dev Mode App",
        "description": "App for dev-mode testing",
        "author": "tester",
        "ui": {"entry": "index.mjs", "pages": [{"route": f"/{name}", "label": name}]},
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
    return home


def _make_web_app():
    app = web.Application()
    register_app_routes(app)
    return app


# ---------------------------------------------------------------------------
# set_dev_mode / is_dev_mode
# ---------------------------------------------------------------------------


def test_set_dev_mode_toggles_installed_json(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    install_app(str(_make_app_source(tmp_path)))
    assert is_dev_mode("dev-mode-app") is False

    result = set_dev_mode("dev-mode-app", True)
    assert result == {"name": "dev-mode-app", "dev": True}
    assert is_dev_mode("dev-mode-app") is True
    assert _read_installed("dev-mode-app").dev is True

    result = set_dev_mode("dev-mode-app", False)
    assert result == {"name": "dev-mode-app", "dev": False}
    assert is_dev_mode("dev-mode-app") is False


def test_set_dev_mode_not_installed(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    result = set_dev_mode("nope", True)
    assert "not installed" in result["error"]


def test_set_dev_mode_rejects_traversal(tmp_path, monkeypatch):
    """Path-traversal app names must be rejected before any filesystem op.

    Regression guard for the HIGH finding: unchecked names like ``../../x``
    would escape ~/.kirocrew/apps/ and read/overwrite an external installed.json.
    """
    _setup_env(tmp_path, monkeypatch)
    for bad in ("../../project", "..", "a/b", "a\\b", "../evil"):
        result = set_dev_mode(bad, True)
        assert "invalid app name" in result["error"], bad
    # is_dev_mode / is_dev_mode_cached also refuse traversal names safely.
    assert is_dev_mode("../../project") is False


def test_set_dev_mode_maintains_sentinel_and_cache(tmp_path, monkeypatch):
    """set_dev_mode keeps the dev sentinel file and in-memory cache in sync."""
    _setup_env(tmp_path, monkeypatch)
    install_app(str(_make_app_source(tmp_path)))
    assert _read_dev_sentinel() == set()
    assert is_dev_mode_cached("dev-mode-app") is False

    set_dev_mode("dev-mode-app", True)
    assert _read_dev_sentinel() == {"dev-mode-app"}
    assert is_dev_mode_cached("dev-mode-app") is True

    set_dev_mode("dev-mode-app", False)
    assert _read_dev_sentinel() == set()
    assert is_dev_mode_cached("dev-mode-app") is False


def test_set_dev_mode_rejects_builtin(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    install_app(str(_make_app_source(tmp_path)))
    meta = _read_installed("dev-mode-app")
    meta.origin = "builtin"
    from kiro_crew.apps.manager import _write_installed
    _write_installed("dev-mode-app", meta)
    result = set_dev_mode("dev-mode-app", True)
    assert "builtin" in result["error"]


def test_concurrent_toggles_do_not_clobber_sentinel(tmp_path, monkeypatch):
    """Concurrent set_dev_mode calls must not drop each other's sentinel entries.

    Regression guard for the HIGH finding: without a cross-process lock around
    the sentinel read-modify-write, threads that read the same state, mutate
    private copies, and write back last-writer-wins, silently dropping apps from
    the watched/no-store set. The lock serializes RMW so every enabled app
    survives.
    """
    import threading

    _setup_env(tmp_path, monkeypatch)
    names = [f"dev-app-{i}" for i in range(8)]
    for n in names:
        install_app(str(_make_app_source(tmp_path, name=n)))

    barrier = threading.Barrier(len(names))

    def _enable(app_name):
        barrier.wait()  # maximize overlap on the RMW window
        set_dev_mode(app_name, True)

    threads = [threading.Thread(target=_enable, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert _read_dev_sentinel() == set(names)


def test_set_dev_mode_writes_metadata_under_lock(tmp_path, monkeypatch):
    """set_dev_mode re-reads + writes installed.json INSIDE the sentinel lock.

    Regression guard for the GPT 5.6 MEDIUM / Long-Term Impact BLOCK item: the
    installed.json write used to run before ``with _sentinel_lock():`` so two
    concurrent toggles could interleave (write-meta A, write-meta B, sentinel B,
    sentinel A), leaving installed.json ``dev: true`` while the sentinel excludes
    the app. Moving the read → mutate → write inside the lock also means a
    metadata change that races in while we wait for the lock is preserved,
    because meta is re-read under the lock rather than reusing the stale copy
    read during validation.

    Simulate that race with a lock wrapper that mutates installed.json on lock
    entry (i.e. after set_dev_mode's outer validation read). If the write were
    outside the lock (or reused the stale meta), the concurrent field change
    would be clobbered.
    """
    from contextlib import contextmanager

    import kiro_crew.apps.dev_mode as dev_mode
    from kiro_crew.apps.manager import _write_installed

    _setup_env(tmp_path, monkeypatch)
    install_app(str(_make_app_source(tmp_path)))

    real_lock = dev_mode._sentinel_lock

    @contextmanager
    def _racing_lock():
        # A concurrent writer bumped an unrelated field while we waited for the
        # lock — happens AFTER set_dev_mode's outer validation read.
        m = _read_installed("dev-mode-app")
        m.version = "9.9.9"
        _write_installed("dev-mode-app", m)
        with real_lock():
            yield

    monkeypatch.setattr(dev_mode, "_sentinel_lock", _racing_lock)
    result = set_dev_mode("dev-mode-app", True)
    assert result == {"name": "dev-mode-app", "dev": True}

    final = _read_installed("dev-mode-app")
    assert final.dev is True          # our toggle landed
    assert final.version == "9.9.9"   # concurrent writer's field preserved
    assert _read_dev_sentinel() == {"dev-mode-app"}


def test_uninstall_clears_dev_sentinel(tmp_path, monkeypatch):
    """Uninstalling a dev-mode app removes it from the sentinel (+ cache).

    Regression guard for the MEDIUM finding: a stale sentinel entry would make a
    different app reinstalled under the same name inherit dev-mode serving.
    """
    from kiro_crew.apps.manager import uninstall_app

    _setup_env(tmp_path, monkeypatch)
    install_app(str(_make_app_source(tmp_path)))
    set_dev_mode("dev-mode-app", True)
    assert _read_dev_sentinel() == {"dev-mode-app"}
    assert is_dev_mode_cached("dev-mode-app") is True

    result = uninstall_app("dev-mode-app")
    assert result.ok is True
    assert _read_dev_sentinel() == set()
    assert is_dev_mode_cached("dev-mode-app") is False


def test_load_dev_apps_filters_stale_entries(tmp_path, monkeypatch):
    """_load_dev_apps drops sentinel names not backed by a dev-mode install.

    Defense-in-depth for the MEDIUM finding: even if a stale name survives in
    the sentinel (e.g. crash mid-uninstall), a reinstall under that name with
    dev:false must not be treated as a dev app.
    """
    import kiro_crew.apps.dev_mode as dev_mode

    _setup_env(tmp_path, monkeypatch)
    install_app(str(_make_app_source(tmp_path)))
    # Inject a stale sentinel: a name with no matching install, plus one that is
    # installed but NOT in dev mode.
    dev_mode._write_dev_sentinel({"ghost-app", "dev-mode-app"})
    assert _read_dev_sentinel() == {"ghost-app", "dev-mode-app"}
    # dev-mode-app is installed but dev:false; ghost-app is not installed.
    assert dev_mode._load_dev_apps() == set()

    set_dev_mode("dev-mode-app", True)
    dev_mode._write_dev_sentinel({"ghost-app", "dev-mode-app"})
    assert dev_mode._load_dev_apps() == {"dev-mode-app"}


# ---------------------------------------------------------------------------
# UI serving: dev mode -> no-store
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ui_file_no_store_in_dev_mode(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    install_app(str(_make_app_source(tmp_path)))
    async with TestClient(TestServer(_make_web_app())) as client:
        resp = await client.get("/apps/dev-mode-app/ui/index.mjs")
        assert resp.status == 200
        assert resp.headers.get("Cache-Control") != "no-store"

        set_dev_mode("dev-mode-app", True)
        resp = await client.get("/apps/dev-mode-app/ui/index.mjs")
        assert resp.status == 200
        assert resp.headers.get("Cache-Control") == "no-store"


# ---------------------------------------------------------------------------
# POST /api/apps/{name}/dev endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dev_endpoint_toggles_and_validates(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    install_app(str(_make_app_source(tmp_path)))
    async with TestClient(TestServer(_make_web_app())) as client:
        resp = await client.post("/api/apps/dev-mode-app/dev", json={"enabled": True})
        assert resp.status == 200
        assert (await resp.json()) == {"name": "dev-mode-app", "dev": True}
        assert is_dev_mode("dev-mode-app") is True

        # non-boolean rejected
        resp = await client.post("/api/apps/dev-mode-app/dev", json={"enabled": "yes"})
        assert resp.status == 400

        # unknown app -> 404
        resp = await client.post("/api/apps/ghost/dev", json={"enabled": True})
        assert resp.status == 404


# ---------------------------------------------------------------------------
# Watch scan helper
# ---------------------------------------------------------------------------


def test_scan_ui_mtimes_detects_edit_add_delete(tmp_path):
    ui = tmp_path / "ui"
    sub = ui / "chunks"
    sub.mkdir(parents=True)
    f1 = ui / "index.mjs"
    f1.write_text("a")
    f2 = sub / "x.mjs"
    f2.write_text("b")

    count, digest = _scan_ui_mtimes(ui)
    assert count == 2

    # Edit bumps the digest (mtime + size fold in)
    time.sleep(0.01)
    f1.write_text("aa")
    import os
    os.utime(f1, (time.time() + 5, time.time() + 5))
    count2, digest2 = _scan_ui_mtimes(ui)
    assert (count2, digest2) != (count, digest)

    # Delete changes count
    f2.unlink()
    count3, _ = _scan_ui_mtimes(ui)
    assert count3 == 1

    # Missing dir is a no-change sentinel, not an error
    assert _scan_ui_mtimes(tmp_path / "ghost") == (0, 0)


def test_scan_ui_mtimes_detects_edit_with_older_mtime(tmp_path):
    """A file rewritten with an mtime BELOW the tree's max must still be seen.

    Regression guard for the GPT 5.6 MEDIUM: a ``(count, max-mtime)`` signature
    missed edits when the touched file's new mtime stayed under another file's
    mtime (``cp -p``/``rsync -a`` of an older bundle, clock skew, a future-dated
    pin). The per-file digest folds each file's own mtime + size, so the change
    registers regardless of where it sits relative to the max.
    """
    import os

    ui = tmp_path / "ui"
    ui.mkdir()
    pinned = ui / "pinned.mjs"
    pinned.write_text("x")
    target = ui / "index.mjs"
    target.write_text("v1")
    # Pin one file far in the future so it owns the max mtime.
    os.utime(pinned, (time.time() + 10_000, time.time() + 10_000))
    # Give the target an mtime well BELOW the pinned max.
    os.utime(target, (time.time() - 100, time.time() - 100))

    _, digest = _scan_ui_mtimes(ui)

    # Edit the target but keep its mtime still below the pinned max.
    target.write_text("v2-different-size")
    os.utime(target, (time.time() - 50, time.time() - 50))
    _, digest2 = _scan_ui_mtimes(ui)

    assert digest2 != digest, "edit below the max mtime must change the digest"


def test_scan_ui_mtimes_follows_symlink(tmp_path):
    """The documented dev setup links ``ui/`` at the developer's source tree.

    ``make_dir_link`` so Windows gets a junction: a directory symlink there needs
    SeCreateSymbolicLinkPrivilege, which an unelevated shell lacks, and ``rglob``
    traverses a junction through the same reparse machinery — so the "watch the
    real source through the link" behaviour stays covered on every platform.
    """
    real = tmp_path / "real-ui"
    real.mkdir()
    (real / "index.mjs").write_text("x")
    link = tmp_path / "ui"
    make_dir_link(link, real)
    count, digest = _scan_ui_mtimes(link)
    assert count == 1
    assert digest != 0


# ---------------------------------------------------------------------------
# Watch loop: offloaded filesystem IO still detects edits and broadcasts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watch_loop_broadcasts_reload_on_edit(tmp_path, monkeypatch):
    """_watch_loop must detect a ui/ edit and broadcast app_reload.

    Regression guard for the offloaded filesystem IO: the sentinel stat, the
    sentinel read, and _scan_ui_mtimes() all run via asyncio.to_thread, and the
    path must still seed state on first tick (no reload storm) and fire exactly
    on change.

    Progress is measured by counting completed ui/ scans rather than by sleeping a
    fixed span: one tick is a sentinel stat plus a tree scan, each a separate
    ``to_thread`` hop, and two hops cost well over the 20ms cadence on a loaded
    Windows box (~15.6ms timer granularity, measured 25-35ms per tick). A fixed
    100ms window is therefore not reliably two ticks, and the edit could land
    before the state the watcher compares against had ever been seeded — the
    watcher then treats the edited tree as its own first observation and correctly
    stays silent, so the test failed on a real machine while the product was fine.
    """
    import asyncio

    import kiro_crew.apps.dev_mode as dev_mode

    _setup_env(tmp_path, monkeypatch)
    install_app(str(_make_app_source(tmp_path)))
    set_dev_mode("dev-mode-app", True)
    # Tight cadence so the test runs fast.
    monkeypatch.setattr(dev_mode, "POLL_INTERVAL_SECS", 0.02)

    events: list[tuple[str, dict]] = []
    scans: list[tuple[int, int]] = []

    def _capture(event: str, payload: dict) -> None:
        events.append((event, payload))

    real_scan = dev_mode._scan_ui_mtimes

    def _counting_scan(ui_dir):
        """Delegate to the real scan, recording that a tick completed one."""
        result = real_scan(ui_dir)
        scans.append(result)
        return result

    monkeypatch.setattr(dev_mode, "_scan_ui_mtimes", _counting_scan)

    async def _wait_until(predicate, what: str) -> None:
        """Poll *predicate* on the loop, generous enough for a loaded host."""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"timed out waiting for {what}")

    task = asyncio.get_running_loop().create_task(dev_mode._watch_loop(_capture))
    try:
        # Two scans: the first seeds this app's state, the second compares an
        # unchanged tree against it. Neither may broadcast.
        await _wait_until(lambda: len(scans) >= 2, "the watcher to seed ui/ state")
        assert events == [], "seeding must not broadcast a reload"

        # Edit a ui/ file with a clearly-newer mtime, then wait for detection.
        ui_file = dev_mode.app_dir("dev-mode-app") / "ui" / "index.mjs"
        ui_file.write_text("export default () => 'edited'\n")
        import os
        os.utime(ui_file, (time.time() + 5, time.time() + 5))

        await _wait_until(lambda: bool(events), "an app_reload broadcast")
    finally:
        task.cancel()
        # _watch_loop catches CancelledError and returns cleanly, so awaiting
        # the cancelled task may return normally rather than re-raising.
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert events, "an edit under a dev app's ui/ must broadcast app_reload"
    event, payload = events[0]
    assert event == "app_reload"
    assert payload["app"] == "dev-mode-app"


@pytest.mark.asyncio
async def test_stop_dev_mode_watcher_awaits_cancellation(tmp_path, monkeypatch):
    """stop_dev_mode_watcher cancels the watcher task and awaits its teardown.

    Regression guard for the GPT 5.6 MEDIUM / Long-Term Impact BLOCK item: the
    watcher started at gateway startup was never stopped on shutdown, leaking
    the module-global task (holding a stale broadcast_ws) across an in-process
    restart. stop_dev_mode_watcher is now async — it must cancel the task, await
    it fully unwound, and clear the module global so a fresh init can start
    cleanly.
    """
    import kiro_crew.apps.dev_mode as dev_mode

    _setup_env(tmp_path, monkeypatch)
    monkeypatch.setattr(dev_mode, "POLL_INTERVAL_SECS", 0.02)

    def _noop_broadcast(event: str, payload: dict) -> None:
        pass

    await dev_mode.init_dev_mode_watcher(_noop_broadcast)
    task = dev_mode._watch_task
    assert task is not None and not task.done()

    await dev_mode.stop_dev_mode_watcher()
    assert dev_mode._watch_task is None
    assert task.done(), "watcher task must be fully unwound after stop"

    # Idempotent: a second stop with nothing running is a no-op.
    await dev_mode.stop_dev_mode_watcher()
    assert dev_mode._watch_task is None


@pytest.mark.asyncio
async def test_watch_loop_no_ui_walk_when_no_dev_apps(tmp_path, monkeypatch):
    """Cost model: with zero dev apps the watcher must never walk any ui/ tree.

    Regression guard for the Long-Term Impact finding — the watcher must not
    make every always-on production gateway pay for a dev-only feature. When
    no app is in dev mode the steady state is a single sentinel stat() per
    tick: _scan_ui_mtimes must not be invoked at all.
    """
    import asyncio

    import kiro_crew.apps.dev_mode as dev_mode

    _setup_env(tmp_path, monkeypatch)
    install_app(str(_make_app_source(tmp_path)))  # installed but NOT in dev mode
    monkeypatch.setattr(dev_mode, "POLL_INTERVAL_SECS", 0.02)

    scan_calls: list = []
    real_scan = dev_mode._scan_ui_mtimes
    monkeypatch.setattr(
        dev_mode,
        "_scan_ui_mtimes",
        lambda p: (scan_calls.append(p), real_scan(p))[1],
    )

    task = asyncio.get_running_loop().create_task(dev_mode._watch_loop(lambda *a: None))
    try:
        await asyncio.sleep(0.2)  # ~10 ticks
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert scan_calls == [], "watcher walked a ui/ tree with zero dev apps"


@pytest.mark.asyncio
async def test_watch_loop_picks_up_out_of_process_toggle(tmp_path, monkeypatch):
    """Enabling dev mode mid-run (sentinel change) starts watching that app.

    Simulates the out-of-process `kirocrew app dev` CLI: the sentinel changes,
    the watcher re-reads it off the event loop, seeds state, then fires on edit.
    """
    import asyncio

    import kiro_crew.apps.dev_mode as dev_mode

    _setup_env(tmp_path, monkeypatch)
    install_app(str(_make_app_source(tmp_path)))
    monkeypatch.setattr(dev_mode, "POLL_INTERVAL_SECS", 0.02)

    events: list[tuple[str, dict]] = []
    task = asyncio.get_running_loop().create_task(
        dev_mode._watch_loop(lambda e, p: events.append((e, p)))
    )
    try:
        await asyncio.sleep(0.1)  # running with no dev apps
        assert events == []

        # Out-of-process toggle: write the sentinel + installed.json.
        set_dev_mode("dev-mode-app", True)
        await asyncio.sleep(0.1)  # watcher re-reads sentinel, seeds state
        assert events == [], "seeding a newly-dev app must not broadcast"

        ui_file = dev_mode.app_dir("dev-mode-app") / "ui" / "index.mjs"
        ui_file.write_text("export default () => 'edited'\n")
        import os
        os.utime(ui_file, (time.time() + 5, time.time() + 5))

        for _ in range(100):
            await asyncio.sleep(0.02)
            if events:
                break
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert events and events[0][0] == "app_reload"
    assert events[0][1]["app"] == "dev-mode-app"


# ---------------------------------------------------------------------------
# update_app preserves the dev flag (and all other persisted fields)
# ---------------------------------------------------------------------------


def test_update_app_preserves_dev_flag(tmp_path, monkeypatch):
    """Updating a dev-mode app must NOT silently reset ``dev`` — or ANY other
    persisted field — to its default.

    Regression guard for the GPT 5.6 MEDIUM / Long-Term Impact BLOCK item 1:
    update_app() rebuilt InstalledApp field-by-field and omitted ``dev`` (and
    other newer fields), so an app developer updating the very app they were
    iterating on in dev mode had ``installed.json`` flip to ``dev: false`` —
    and at the next sentinel reconcile/restart the app dropped out of live
    reload. dataclasses.replace now carries every persisted field forward, so
    this asserts the full field set survives, not just ``dev``.
    """
    _setup_env(tmp_path, monkeypatch)
    src = _make_app_source(tmp_path)
    install_app(str(src))
    set_dev_mode("dev-mode-app", True)
    assert _read_installed("dev-mode-app").dev is True

    # Stamp distinctive non-default values on EVERY carried-forward persisted
    # field. update_app() overrides only version/displayName/updatedAt/source;
    # this asserts the dataclasses.replace guard matches the real scope of the
    # fix (all persisted metadata), not merely the ``dev`` flag that surfaced
    # it — so a future field added to InstalledApp can't silently regress.
    from kiro_crew.apps.manager import _write_installed
    meta0 = _read_installed("dev-mode-app")
    meta0.enabled = False
    meta0.installedAt = "2020-01-01T00:00:00Z"
    meta0.origin = "local"
    meta0.resources = "app"
    meta0.lifecycle = "app"
    meta0.schemaVersion = 99
    meta0.migratedTo = "standalone:dev-mode-app"
    _write_installed("dev-mode-app", meta0)

    # Bump the source version and update in place.
    manifest_path = src / APP_MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = "1.1.0"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    result = update_app(str(src))
    assert result.ok is True

    meta = _read_installed("dev-mode-app")
    # Overridden by the update:
    assert meta.version == "1.1.0"
    # Every other persisted field must carry forward untouched:
    assert meta.dev is True, "update_app must preserve the dev flag"
    assert meta.enabled is False
    assert meta.installedAt == "2020-01-01T00:00:00Z"
    assert meta.origin == "local"
    assert meta.resources == "app"
    assert meta.lifecycle == "app"
    assert meta.schemaVersion == 99
    assert meta.migratedTo == "standalone:dev-mode-app"


# ---------------------------------------------------------------------------
# Sentinel reconciliation makes installed.json authoritative at startup
# ---------------------------------------------------------------------------


def test_scan_installed_dev_apps_reads_from_installed_json(tmp_path, monkeypatch):
    """_scan_installed_dev_apps derives the dev set straight from installed.json."""
    _setup_env(tmp_path, monkeypatch)
    install_app(str(_make_app_source(tmp_path, name="app-a")))
    install_app(str(_make_app_source(tmp_path, name="app-b")))
    assert _scan_installed_dev_apps() == set()

    set_dev_mode("app-a", True)
    assert _scan_installed_dev_apps() == {"app-a"}


def test_reconcile_sentinel_adds_missing_entry(tmp_path, monkeypatch):
    """A ``dev: true`` written to installed.json out-of-band is honored on reconcile.

    Regression guard for the Long-Term Impact BLOCK item 2: _load_dev_apps only
    ever FILTERS the sentinel and can never ADD an entry, so a dev flag set via
    snapshot restore / hand-edit / a crash between the metadata and sentinel
    writes left the documented contract field saying ``dev: true`` while the
    watcher never engaged. Reconciling from installed.json at startup makes the
    documented field authoritative.
    """
    _setup_env(tmp_path, monkeypatch)
    install_app(str(_make_app_source(tmp_path)))

    # Simulate an out-of-band dev flag: set installed.json dev=true WITHOUT
    # touching the sentinel (as a snapshot restore or hand-edit would).
    meta = _read_installed("dev-mode-app")
    meta.dev = True
    from kiro_crew.apps.manager import _write_installed
    _write_installed("dev-mode-app", meta)
    assert _read_dev_sentinel() == set(), "sentinel is stale/missing the entry"

    reconciled = _reconcile_sentinel_from_installed()
    assert reconciled == {"dev-mode-app"}
    assert _read_dev_sentinel() == {"dev-mode-app"}, "sentinel rebuilt from installed.json"
    assert is_dev_mode_cached("dev-mode-app") is True


def test_reconcile_sentinel_drops_stale_entry(tmp_path, monkeypatch):
    """Reconcile also removes a sentinel entry not backed by a dev-mode install."""
    import kiro_crew.apps.dev_mode as dev_mode

    _setup_env(tmp_path, monkeypatch)
    install_app(str(_make_app_source(tmp_path)))
    # Sentinel claims dev, but installed.json says dev:false.
    dev_mode._write_dev_sentinel({"dev-mode-app", "ghost"})

    reconciled = _reconcile_sentinel_from_installed()
    assert reconciled == set()
    assert _read_dev_sentinel() == set()


def test_reconcile_scans_inside_lock_preserves_racing_toggle(tmp_path, monkeypatch):
    """Reconcile runs its installed.json scan INSIDE the sentinel lock.

    Regression guard for the GPT 5.6 HIGH: the authoritative scan used to run
    BEFORE ``with _sentinel_lock():``. A concurrent ``set_dev_mode`` toggle
    (which holds the lock) could land between the scan and the lock acquisition
    — the toggle writes installed.json ``dev: true`` and adds the app to the
    sentinel, then reconcile takes the lock with its STALE scan (missing that
    app), sees a divergence, and rewrites the sentinel to exclude it, silently
    dropping the just-toggled app from the watched/no-store set until the next
    restart. Moving the scan inside the lock makes scan → compare → write → cache
    atomic against toggles.

    Simulate the race with a lock wrapper that enables dev mode for the app on
    lock entry (i.e. after any pre-lock scan would have already run). With the
    scan inside the lock the wrapper's dev flag is visible to the scan, so the
    app is kept; if the scan ran before the lock it would see dev:false and drop
    the app.
    """
    from contextlib import contextmanager

    import kiro_crew.apps.dev_mode as dev_mode
    from kiro_crew.apps.manager import _write_installed

    _setup_env(tmp_path, monkeypatch)
    install_app(str(_make_app_source(tmp_path)))

    real_lock = dev_mode._sentinel_lock

    @contextmanager
    def _racing_lock():
        # A concurrent toggle enabled dev mode while we waited for the lock:
        # both installed.json AND the sentinel now say the app is a dev app.
        m = _read_installed("dev-mode-app")
        m.dev = True
        _write_installed("dev-mode-app", m)
        dev_mode._write_dev_sentinel({"dev-mode-app"})
        with real_lock():
            yield

    monkeypatch.setattr(dev_mode, "_sentinel_lock", _racing_lock)
    reconciled = _reconcile_sentinel_from_installed()

    # The racing toggle must survive: scanned inside the lock, dev:true is seen.
    assert reconciled == {"dev-mode-app"}
    assert _read_dev_sentinel() == {"dev-mode-app"}
    assert is_dev_mode_cached("dev-mode-app") is True
