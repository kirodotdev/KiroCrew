"""File-tool refusal and read-only seals for existing regular app manifests."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from kiro_crew import app_manifest_files
from kiro_crew.app_manifest_files import app_manifest_leaves, app_manifest_targets
from kiro_crew.config.paths import config_dir
from kiro_crew.platform_compat import effective_uid
from kiro_crew.security.paths import sensitive_write_path_refusal

_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="creates symlinks")


def _install(home: Path, name: str) -> Path:
    manifest = home / "apps" / name / "app.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"name": "%s"}' % name, encoding="utf-8")
    return manifest


def _write_fields(info: os.stat_result) -> tuple[int, ...]:
    """What a write, a replace or a chmod changes; ``st_atime`` is left out on
    purpose, because under ``relatime`` the first read after a write moves it."""
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_uid,
        info.st_gid,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


@pytest.mark.parametrize(
    "rel", ["apps/weather/app.json", "apps/weather/APP.JSON", "APPS/Weather/app.json"]
)
def test_an_installed_apps_manifest_is_refused_and_names_request_server(rel):
    path = os.path.join(str(config_dir()), *Path(rel).parts)
    reason = sensitive_write_path_refusal(path)
    assert reason is not None and reason.startswith("Blocked: write to protected path")
    assert "request_server" in reason


def test_the_default_home_spelling_is_refused_too():
    reason = sensitive_write_path_refusal("~/.kiro/crew/apps/weather/app.json")
    assert reason is not None and "request_server" in reason


@pytest.mark.parametrize(
    "rel",
    [
        "apps/weather/data/app.json",
        "apps/.weather-tmp/app.json",
        "apps/weather/ui/app.json",
        "apps/weather/notes.json",
        "workspace/apps/weather/app.json",
    ],
)
def test_other_places_are_not_installed_manifests(tmp_path, rel):
    path = os.path.join(str(config_dir()), *Path(rel).parts)
    assert sensitive_write_path_refusal(path) is None
    assert sensitive_write_path_refusal(os.path.join(str(tmp_path), *Path(rel).parts)) is None


def test_the_seal_lists_every_installed_manifest(caplog):
    home = config_dir()
    _install(home, "weather")
    _install(home, "notes")
    (home / "apps" / "no-manifest").mkdir()
    assert app_manifest_leaves(str(home)) == ("apps/notes/app.json", "apps/weather/app.json")
    targets = app_manifest_targets(str(Path.home()))
    assert str(home / "apps" / "weather" / "app.json") in targets
    assert str(home / "apps" / "notes" / "app.json") in targets
    assert not caplog.records


def test_no_apps_folder_seals_nothing(caplog):
    assert app_manifest_leaves(str(config_dir())) == ()
    assert app_manifest_targets(str(Path.home())) == ()
    assert not caplog.records


def test_a_manifest_without_an_install_record_is_sealed():
    home = config_dir()
    manifest = _install(home, "weather")
    assert not (manifest.parent / "installed.json").exists()
    assert app_manifest_leaves(str(home)) == ("apps/weather/app.json",)
    assert str(manifest) in app_manifest_targets(str(Path.home()))


@_POSIX_ONLY
@pytest.mark.parametrize("layout", ["default", "relocated"])
def test_a_linked_data_home_keeps_its_manifest_sealed(tmp_path, monkeypatch, layout):
    from kiro_crew import sandbox
    from kiro_crew.config import paths

    real_home = config_dir()
    _install(real_home, "weather")
    os_home = tmp_path / "os-home"
    linked_home = os_home / ".kiro" / "crew" if layout == "default" else tmp_path / "linked-home"
    linked_home.parent.mkdir(parents=True, exist_ok=True)
    linked_home.symlink_to(real_home, target_is_directory=True)
    monkeypatch.setattr(paths, "config_dir", lambda: linked_home)
    monkeypatch.setattr(sandbox, "config_dir", lambda: linked_home)

    assert app_manifest_leaves(str(linked_home)) == ("apps/weather/app.json",)
    lexical = str(linked_home / "apps" / "weather" / "app.json")
    resolved = os.path.realpath(lexical)
    targets = app_manifest_targets(str(os_home))
    assert targets == (lexical, resolved)
    assert len(targets) == len(set(targets))
    host, plan = _host(monkeypatch, "seatbelt")
    assert lexical in host.kiro_agents_targets
    assert resolved in plan.readonly
    assert plan.refusals == ()


@_POSIX_ONLY
@pytest.mark.parametrize("backend", ["namespace", "seatbelt"])
@pytest.mark.parametrize("place", ["inside", "outside"])
def test_a_linked_manifest_is_skipped_without_writing(
    tmp_path, monkeypatch, caplog, backend, place
):
    home = config_dir()
    folder = home / "apps" / "weather"
    folder.mkdir(parents=True)
    target = folder / "target.json" if place == "inside" else tmp_path / "target.json"
    target.write_bytes(b'{"name": "weather"}\n')
    if os.name != "nt":
        target.chmod(0o640)
    manifest = folder / "app.json"
    manifest.symlink_to(target)
    before = {p.name: _write_fields(p.lstat()) for p in folder.iterdir()}
    target_before = target.stat()
    assert app_manifest_leaves(str(home)) == ()
    assert len(caplog.records) == 1
    assert caplog.records[0].levelname == "WARNING"
    assert str(manifest) in caplog.records[0].getMessage()
    assert manifest.is_symlink()
    assert target.read_bytes() == b'{"name": "weather"}\n'
    assert target.stat().st_mtime_ns == target_before.st_mtime_ns
    after = {p.name: _write_fields(p.lstat()) for p in folder.iterdir()}
    assert after == before
    host, plan = _host(monkeypatch, backend)
    assert not any(t.endswith("app.json") for t in host.kiro_agents_targets)
    assert plan.refusals == ()


_LINUX_PERMISSIONS = pytest.mark.skipif(
    not sys.platform.startswith("linux") or effective_uid() == 0,
    reason="requires Linux directory permissions without root",
)


@_LINUX_PERMISSIONS
@pytest.mark.parametrize("backend", ["namespace", "seatbelt"])
@pytest.mark.parametrize("place", ["home", "apps", "app"])
@pytest.mark.parametrize("mode", [0o000, 0o100, 0o400, 0o600])
def test_directory_owner_bits_are_restored_before_sealing(
    monkeypatch, caplog, backend, place, mode
):
    home = config_dir()
    manifest = _install(home, "weather")
    folder = home if place == "home" else home / "apps" if place == "apps" else manifest.parent
    original_mode = stat.S_IMODE(folder.stat().st_mode)
    try:
        folder.chmod(mode)
        assert app_manifest_leaves(str(home)) == ("apps/weather/app.json",)
        assert stat.S_IMODE(folder.stat().st_mode) == mode | stat.S_IRWXU
        warnings = [r for r in caplog.records if r.name == "kiro_crew.app_manifest_files"]
        assert len(warnings) == 1 and warnings[0].levelname == "WARNING"
        assert str(folder) in warnings[0].getMessage()
        host, plan = _host(monkeypatch, backend)
        assert str(manifest) in host.kiro_agents_targets
        assert str(manifest) in plan.readonly
        assert plan.refusals == ()
    finally:
        folder.chmod(original_mode)


@_LINUX_PERMISSIONS
def test_a_replaced_directory_is_skipped_without_changing_the_replacement(monkeypatch, caplog):
    from kiro_crew import pinned_fs

    home = config_dir()
    manifest = _install(home, "weather")
    folder = manifest.parent
    moved = folder.parent / ".weather-moved"
    original_mode = stat.S_IMODE(folder.stat().st_mode)
    real_open = os.open
    changed = False

    def swap_before_read(name, flags, *args, **kwargs):
        nonlocal changed
        if name == "weather" and not flags & os.O_PATH:
            changed = True
            folder.rename(moved)
            folder.mkdir(mode=0o500)
        return real_open(name, flags, *args, **kwargs)

    try:
        folder.chmod(0o000)
        monkeypatch.setattr(os, "open", swap_before_read)
        monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: True)
        assert app_manifest_leaves(str(home)) == ()
        assert changed
        assert stat.S_IMODE(folder.stat().st_mode) == 0o500
        assert stat.S_IMODE(moved.stat().st_mode) == stat.S_IRWXU
        assert any("identity changed" in r.getMessage() for r in caplog.records)
    finally:
        (moved if changed else folder).chmod(original_mode)


@_LINUX_PERMISSIONS
def test_another_users_directory_is_skipped_without_changing_its_mode(monkeypatch, caplog):
    home = config_dir()
    manifest = _install(home, "weather")
    folder = manifest.parent
    original_mode = stat.S_IMODE(folder.stat().st_mode)
    try:
        folder.chmod(0o000)
        monkeypatch.setattr(app_manifest_files, "effective_uid", lambda: home.stat().st_uid + 1)
        assert app_manifest_leaves(str(home)) == ()
        assert stat.S_IMODE(folder.stat().st_mode) == 0o000
        assert len(caplog.records) == 1 and caplog.records[0].levelname == "WARNING"
    finally:
        folder.chmod(original_mode)


@_LINUX_PERMISSIONS
@pytest.mark.parametrize("backend", ["namespace", "seatbelt"])
def test_a_failed_descriptor_chmod_skips_and_keeps_other_seals(monkeypatch, caplog, backend):
    home = config_dir()
    manifest = _install(home, "weather")
    other = _install(home, "notes")
    folder = manifest.parent
    original_mode = stat.S_IMODE(folder.stat().st_mode)
    real_chmod = os.chmod

    def fail_proc(path, mode, *args, **kwargs):
        if str(path).startswith("/proc/self/fd/"):
            raise OSError("descriptor chmod unavailable")
        return real_chmod(path, mode, *args, **kwargs)

    try:
        folder.chmod(0o000)
        monkeypatch.setattr(os, "chmod", fail_proc)
        assert app_manifest_leaves(str(home)) == ("apps/notes/app.json",)
        assert stat.S_IMODE(folder.stat().st_mode) == 0o000
        assert len(caplog.records) == 1 and caplog.records[0].levelname == "WARNING"
        host, plan = _host(monkeypatch, backend)
        assert str(other) in plan.readonly
        assert str(manifest) not in host.kiro_agents_targets
        assert plan.refusals == ()
    finally:
        folder.chmod(original_mode)


@_POSIX_ONLY
@pytest.mark.parametrize("backend", ["namespace", "seatbelt"])
@pytest.mark.parametrize(
    "shape", ["linked-folder", "stray-file", "dot-folder", "absent", "nonfile"]
)
def test_skipped_entries_never_stop_the_live_plan(tmp_path, monkeypatch, caplog, backend, shape):
    home = config_dir()
    apps = home / "apps"
    if shape == "linked-folder":
        real = tmp_path / "real"
        _install(real, "weather")
        if os.name != "nt":
            (real / "apps" / "weather").chmod(0o500)
        apps.mkdir()
        (apps / "weather").symlink_to(real / "apps" / "weather", target_is_directory=True)
    elif shape == "stray-file":
        apps.mkdir()
        (apps / "notes.txt").write_text("notes", encoding="utf-8")
    elif shape == "dot-folder":
        _install(home, ".weather-old")
    elif shape == "nonfile":
        (apps / "weather" / "app.json").mkdir(parents=True)
    with caplog.at_level("INFO", logger="kiro_crew.app_manifest_files"):
        assert app_manifest_leaves(str(home)) == ()
    records = [r for r in caplog.records if r.name == "kiro_crew.app_manifest_files"]
    if shape in ("linked-folder", "nonfile"):
        assert len(records) == 1 and records[0].levelname == "WARNING"
    elif shape == "stray-file":
        assert len(records) == 1 and records[0].levelname == "INFO"
    else:
        assert not records
    host, plan = _host(monkeypatch, backend)
    assert not any(t.endswith("app.json") for t in host.kiro_agents_targets)
    assert plan.refusals == ()
    if shape == "linked-folder" and os.name != "nt":
        assert stat.S_IMODE((real / "apps" / "weather").stat().st_mode) == 0o500


def _host(monkeypatch, backend):
    from kiro_crew import sandbox
    from kiro_crew.sandbox_plan import SandboxRequest, plan_confinement

    monkeypatch.setattr(sandbox, "_resolved_kiro_agents_targets", lambda: [])
    monkeypatch.setattr(sandbox, "_resolved_kiro_mcp_registry_targets", lambda: [])
    request = SandboxRequest(tier="standard", backend=backend)
    host = sandbox._live_plan_host(request)
    return host, plan_confinement(request, host)


@_POSIX_ONLY
@pytest.mark.parametrize("backend", ["namespace", "seatbelt"])
def test_an_installed_manifest_is_sealed_on_both_launchers(monkeypatch, backend):
    manifest = _install(config_dir(), "weather")
    host, plan = _host(monkeypatch, backend)
    assert str(manifest) in host.kiro_agents_targets
    assert str(manifest) in plan.readonly
    assert plan.refusals == ()
    if backend == "seatbelt":
        assert str(manifest.parent) in host.readonly_ancestor_guards


@_POSIX_ONLY
def test_an_app_without_a_manifest_adds_no_seal(monkeypatch):
    (config_dir() / "apps" / "half-installed").mkdir(parents=True)
    host, _plan = _host(monkeypatch, "namespace")
    assert not any(t.endswith("app.json") for t in host.kiro_agents_targets)
