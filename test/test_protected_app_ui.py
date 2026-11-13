"""The dashboard runs an app's UI scripts from the gateway's copy, not the app's tree.

``/apps/{name}/ui/{entry}`` is imported into the dashboard's own origin, and the app's
tree is writable from the sandbox. So a script is served from the copy the gateway
took when it installed the app (``apps.protected_app_ui``), under ``member-records/``,
which no sandboxed process can write.
"""

from __future__ import annotations

import json
import os

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from app_manager_helpers import app_home_fixture  # noqa: F401 - fixture
from app_manager_helpers import _make_app_source
from apps_routes_coverage_helpers import _make_app as _make_routes_app

from kiro_crew.apps import manager
from kiro_crew.apps import routes as app_routes
from kiro_crew.apps.manager import app_dir, apps_dir, install_app, uninstall_app, update_app

INSTALLED = b"export default () => 'installed'\n"
CHANGED = b"export default () => 'changed on disk'\n"


@pytest.fixture(autouse=True)
def _protected_copy_on():  # type: ignore[no-untyped-def]
    """Every test here is about the opt-in protected copy, so it is switched on;
    the default (off) is pinned by the two tests that patch it back."""
    from kiro_crew.apps import protected_app_ui

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(protected_app_ui, "enabled", lambda: True)
        yield


def _source_with_ui(tmp_path, *, name="test-app", version="1.0.0", body=INSTALLED):
    src = _make_app_source(
        tmp_path / version, name=name, version=version, ui={"entry": "index.mjs"}
    )
    (src / "ui" / "chunks").mkdir(parents=True)
    (src / "ui" / "index.mjs").write_bytes(body)
    (src / "ui" / "chunks" / "lazy.js").write_bytes(body)
    (src / "ui" / "style.css").write_bytes(b"body{}")
    return src


def _plant_folder(name: str, body: bytes = CHANGED) -> None:
    """An app folder no install put there: what any writer of ``apps/`` can make."""
    folder = apps_dir() / name
    (folder / "ui").mkdir(parents=True)
    (folder / "app.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    (folder / "installed.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    (folder / "ui" / "index.mjs").write_bytes(body)


async def _get(path: str) -> tuple[int, bytes]:
    app = web.Application()
    app.router.add_get("/apps/{name}/ui/{path:.*}", app_routes.handle_app_ui_file)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(path)
        return resp.status, await resp.read()


@pytest.mark.asyncio
async def test_a_script_changed_in_the_app_tree_is_served_as_installed(tmp_path, app_home):
    assert install_app(_source_with_ui(tmp_path)).ok
    ui = app_dir("test-app") / "ui"
    (ui / "index.mjs").write_bytes(CHANGED)
    (ui / "chunks" / "lazy.js").write_bytes(CHANGED)

    assert await _get("/apps/test-app/ui/index.mjs") == (200, INSTALLED)
    assert await _get("/apps/test-app/ui/chunks/lazy.js") == (200, INSTALLED)


@pytest.mark.asyncio
async def test_an_app_folder_planted_after_the_start_up_pass_has_no_scripts_served(app_home):
    """No request has to come first: the gateway's own pass at start closes the window."""
    from kiro_crew.apps import protected_app_ui

    protected_app_ui.capture_existing(apps_dir())  # what the gateway runs after its bind
    _plant_folder("forged")

    status, body = await _get("/apps/forged/ui/index.mjs")
    assert status == 404
    assert CHANGED not in body


@pytest.mark.asyncio
async def test_files_that_are_not_scripts_are_still_read_from_the_app_tree(tmp_path, app_home):
    assert install_app(_source_with_ui(tmp_path)).ok
    (app_dir("test-app") / "ui" / "style.css").write_bytes(b"body{color:red}")
    assert await _get("/apps/test-app/ui/style.css") == (200, b"body{color:red}")


@pytest.mark.asyncio
async def test_an_update_replaces_the_copy_and_an_uninstall_removes_it(tmp_path, app_home):
    from kiro_crew.apps import protected_app_ui

    assert install_app(_source_with_ui(tmp_path)).ok
    newer = b"export default () => 'v2'\n"
    assert update_app(_source_with_ui(tmp_path, version="1.1.0", body=newer)).ok
    assert await _get("/apps/test-app/ui/index.mjs") == (200, newer)

    assert uninstall_app("test-app").ok
    assert not protected_app_ui.copy_root("test-app").exists()


@pytest.mark.asyncio
async def test_a_failed_update_keeps_the_copy_of_the_version_left_installed(
    tmp_path, app_home, monkeypatch
):
    assert install_app(_source_with_ui(tmp_path)).ok

    def _fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(manager, "_write_installed", _fail)
    newer = b"export default () => 'v2'\n"
    result = update_app(_source_with_ui(tmp_path, version="1.1.0", body=newer))
    assert not result.ok
    assert await _get("/apps/test-app/ui/index.mjs") == (200, INSTALLED)


@pytest.mark.asyncio
async def test_an_app_an_older_build_installed_gets_its_copy_once(app_home):
    """Upgrading must not break installed apps: the start-up pass copies every one."""
    from kiro_crew.apps import protected_app_ui

    _plant_folder("older-app", INSTALLED)
    protected_app_ui.capture_existing(apps_dir())

    (apps_dir() / "older-app" / "ui" / "index.mjs").write_bytes(CHANGED)
    assert await _get("/apps/older-app/ui/index.mjs") == (200, INSTALLED)


def test_a_failed_start_up_copy_is_not_retried_from_the_app_tree(app_home, monkeypatch):
    """After the one pass, nothing is taken from an app tree: an update brings it back."""
    from kiro_crew.apps import protected_app_ui

    _plant_folder("older-app", INSTALLED)
    real_open = protected_app_ui.os.open

    def _locked(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(path).endswith("index.mjs"):
            raise PermissionError("held open by another process")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(protected_app_ui.os, "open", _locked)
    protected_app_ui.capture_existing(apps_dir())
    assert not protected_app_ui.copy_root("older-app").exists()

    monkeypatch.setattr(protected_app_ui.os, "open", real_open)
    (apps_dir() / "older-app" / "ui" / "index.mjs").write_bytes(CHANGED)
    _plant_folder("planted-later")
    assert protected_app_ui.served_root(apps_dir(), "older-app") is None
    assert protected_app_ui.served_root(apps_dir(), "planted-later") is None


def test_a_made_up_name_leaves_no_lock_file(app_home):
    from kiro_crew.apps import protected_app_ui
    from kiro_crew.config.paths import config_dir

    protected_app_ui.capture_existing(apps_dir())
    assert protected_app_ui.served_root(apps_dir(), "no-such-app") is None
    assert not (config_dir() / "app-record-locks" / "no-such-app.lock").exists()


@pytest.mark.asyncio
async def test_an_owner_registration_tells_open_dashboards_to_reload(app_home):
    from types import SimpleNamespace

    sent: list[tuple[str, dict]] = []
    app = _make_routes_app(dashboard_user="owner")
    app["state"] = SimpleNamespace(
        owner_id="owner", broadcast_ws=lambda kind, data: sent.append((kind, data))
    )
    (apps_dir() / "self-app" / "ui").mkdir(parents=True)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/apps/register",
            json={"name": "self-app", "version": "1.0.0", "displayName": "Self App"},
        )
        assert resp.status == 201
    assert ("app_reload", "self-app") in [(k, d.get("app")) for k, d in sent]


@pytest.mark.asyncio
async def test_an_owner_registration_takes_the_copy_of_a_self_managed_app(app_home):
    ui = apps_dir() / "self-app" / "ui"
    ui.mkdir(parents=True)
    (ui / "index.mjs").write_bytes(INSTALLED)
    async with TestClient(TestServer(_make_routes_app(dashboard_user="owner"))) as client:
        resp = await client.post(
            "/api/apps/register",
            json={"name": "self-app", "version": "1.0.0", "displayName": "Self App"},
        )
        assert resp.status == 201
        (ui / "index.mjs").write_bytes(CHANGED)
        resp = await client.get("/apps/self-app/ui/index.mjs")
        assert resp.status == 200
        assert await resp.read() == INSTALLED


@pytest.mark.asyncio
async def test_a_copy_set_aside_by_a_hard_stop_is_put_back(tmp_path, app_home):
    """A stop between the two renames leaves only the set-aside copy; it is served."""
    from kiro_crew.apps import protected_app_ui

    assert install_app(_source_with_ui(tmp_path)).ok
    target = protected_app_ui.copy_root("test-app")
    target.rename(target.parent / ".old-test-app-0123abcd")
    (app_dir("test-app") / "ui" / "index.mjs").write_bytes(CHANGED)

    assert await _get("/apps/test-app/ui/index.mjs") == (200, INSTALLED)


@pytest.mark.asyncio
async def test_an_uninstall_drops_set_aside_copies_too(tmp_path, app_home):
    """Nothing left behind by a hard stop can bring back an uninstalled app's scripts."""
    from kiro_crew.apps import protected_app_ui

    assert install_app(_source_with_ui(tmp_path)).ok
    protected_app_ui.capture_existing(apps_dir())  # what the gateway runs after its bind
    target = protected_app_ui.copy_root("test-app")
    aside = target.parent / ".old-test-app-0123abcd"
    aside.mkdir()
    (aside / "index.mjs").write_bytes(INSTALLED)
    assert uninstall_app("test-app").ok
    assert not aside.exists()

    _plant_folder("test-app")
    status, body = await _get("/apps/test-app/ui/index.mjs")
    assert status == 404
    assert INSTALLED not in body


def test_the_copy_leaves_out_what_an_install_leaves_out(tmp_path, app_home):
    from kiro_crew.apps import protected_app_ui

    src = _source_with_ui(tmp_path)
    (src / "ui" / "node_modules" / "dep").mkdir(parents=True)
    (src / "ui" / "node_modules" / "dep" / "index.js").write_bytes(b"dep")
    assert install_app(src).ok
    copy = protected_app_ui.copy_root("test-app")
    assert (copy / "index.mjs").is_file()
    assert not (copy / "node_modules").exists()


@pytest.mark.asyncio
async def test_a_copy_an_uninstall_could_not_delete_is_not_served(tmp_path, app_home):
    """A copy held open (Windows) survives the removal; the withdrawal still holds."""
    from types import SimpleNamespace

    from kiro_crew.apps import protected_app_ui

    assert install_app(_source_with_ui(tmp_path)).ok
    protected_app_ui.capture_existing(apps_dir())

    def _held(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("file in use")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(protected_app_ui, "shutil", SimpleNamespace(rmtree=_held))
        assert uninstall_app("test-app").ok
    assert protected_app_ui.copy_root("test-app").exists()

    _plant_folder("test-app")
    assert (await _get("/apps/test-app/ui/index.mjs"))[0] == 404


def test_a_hard_linked_install_source_keeps_its_scripts(tmp_path, app_home):
    from kiro_crew.apps import protected_app_ui

    src = _source_with_ui(tmp_path)
    os.link(src / "ui" / "index.mjs", tmp_path / "second-name.mjs")
    assert install_app(src).ok
    assert (protected_app_ui.copy_root("test-app") / "index.mjs").read_bytes() == INSTALLED


def test_the_start_up_boundary_is_written_before_any_tree_is_read(app_home, monkeypatch):
    """No boundary, no copy: a pass that cannot record it reads no app tree."""
    from kiro_crew.apps import protected_app_ui

    _plant_folder("older-app", INSTALLED)

    def _full_disk(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(protected_app_ui, "atomic_write", _full_disk)
        with pytest.raises(OSError):
            protected_app_ui.capture_existing(apps_dir())
    assert not protected_app_ui.copy_root("older-app").exists()


def test_an_uninstall_that_cannot_withdraw_the_copy_deletes_nothing(tmp_path, app_home):
    from kiro_crew.apps import protected_app_ui

    assert install_app(_source_with_ui(tmp_path)).ok

    def _full_disk(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(protected_app_ui, "withdraw", _full_disk)
        assert not uninstall_app("test-app").ok
    assert (app_dir("test-app") / "ui" / "index.mjs").is_file()
    assert protected_app_ui.served_root(apps_dir(), "test-app") is not None


def test_a_script_read_waits_for_a_running_start_up_pass(app_home):
    """A pass that has started holds the lock; a reader waits instead of a 404."""
    import threading

    from kiro_crew.apps import protected_app_ui

    _plant_folder("older-app", INSTALLED)
    entered = threading.Event()
    release = threading.Event()
    real_record_folder = protected_app_ui.record_folder

    def _slow(apps_root, name):  # type: ignore[no-untyped-def]
        entered.set()
        assert release.wait(10)
        real_record_folder(apps_root, name)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(protected_app_ui, "record_folder", _slow)
        runner = threading.Thread(target=protected_app_ui.capture_existing, args=(apps_dir(),))
        runner.start()
        assert entered.wait(10)
        result: list[object] = []
        reader = threading.Thread(
            target=lambda: result.append(protected_app_ui.served_root(apps_dir(), "older-app"))
        )
        reader.start()
        release.set()
        runner.join(10)
        reader.join(10)
    assert result and result[0] is not None


def test_one_unreadable_app_folder_does_not_skip_the_others(app_home):
    from pathlib import Path

    from kiro_crew.apps import protected_app_ui

    _plant_folder("bad-app", INSTALLED)
    _plant_folder("good-app", INSTALLED)
    real_is_file = Path.is_file

    def _probe(self):  # type: ignore[no-untyped-def]
        if self.parent.name == "bad-app" and self.name == "installed.json":
            raise PermissionError("no search permission")
        return real_is_file(self)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Path, "is_file", _probe)
        protected_app_ui.capture_existing(apps_dir())
    assert protected_app_ui.copy_root("good-app").is_dir()


@pytest.mark.asyncio
async def test_a_failed_ui_copy_still_answers_the_committed_registration(app_home):
    """The record and secret are committed, so the route answers 201 with a warning."""
    from kiro_crew.apps import protected_app_ui

    def _full_disk(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    (apps_dir() / "self-app" / "ui").mkdir(parents=True)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(protected_app_ui, "record_folder", _full_disk)
        async with TestClient(TestServer(_make_routes_app(dashboard_user="owner"))) as client:
            resp = await client.post(
                "/api/apps/register",
                json={"name": "self-app", "version": "1.0.0", "displayName": "Self App"},
            )
            body = await resp.json()
    assert resp.status == 201
    assert body["secret"]
    assert body["code"] == "app_ui_copy_failed"


@pytest.mark.asyncio
async def test_off_by_default_scripts_are_served_from_the_app_tree_as_on_main(tmp_path, app_home):
    from kiro_crew.apps import protected_app_ui

    assert install_app(_source_with_ui(tmp_path)).ok
    (app_dir("test-app") / "ui" / "index.mjs").write_bytes(CHANGED)
    _plant_folder("forged")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(protected_app_ui, "enabled", lambda: False)
        assert await _get("/apps/test-app/ui/index.mjs") == (200, CHANGED)
        assert await _get("/apps/forged/ui/index.mjs") == (200, CHANGED)


def test_off_by_default_an_uninstall_goes_ahead_when_the_mark_cannot_be_written(tmp_path, app_home):
    from kiro_crew.apps import protected_app_ui

    assert install_app(_source_with_ui(tmp_path)).ok

    def _full_disk(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(protected_app_ui, "enabled", lambda: False)
        mp.setattr(protected_app_ui, "withdraw", _full_disk)
        assert uninstall_app("test-app").ok
    assert not (app_dir("test-app") / "installed.json").exists()


def test_the_config_setting_is_off_by_default(app_home):
    from kiro_crew.config.loader import KiroCrewConfig

    assert KiroCrewConfig.load().agent.apps_ui_protected_copy is False
