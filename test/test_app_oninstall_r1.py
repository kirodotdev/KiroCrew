"""Regression tests for the onInstall script's write window.

These tests run real bash children: the point is what a third-party script can
do to the gateway-owned names around it, not what a mocked runner promises.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import shutil
import stat
import time
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.apps.lifecycle_scripts import run_lifecycle_script
from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
    _remove_readonly_rmtree_error,
    apps_dir,
    install_app,
    uninstall_app,
)
from kiro_crew.apps.manager import _write_installed

pytestmark = pytest.mark.skipif(
    not platform_compat.IS_POSIX,
    reason="these regressions drive real /bin/bash lifecycle scripts",
)


@pytest.fixture()
def app_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
    )
    from kiro_crew import sandbox

    monkeypatch.setattr(sandbox, "_allow_unsandboxed_exec", lambda: True)
    import kiro_crew.apps.bridges as bridges_mod

    kiro_agents = tmp_path / "kiro-agents"
    kiro_agents.mkdir()
    monkeypatch.setattr(bridges_mod, "KIRO_AGENTS_DIR", kiro_agents)
    return home


def _source(
    tmp_path: Path,
    name: str,
    *,
    on_install: str = "",
    on_enable: str = "",
    crons: list[dict[str, object]] | None = None,
) -> Path:
    src = tmp_path / "source" / name
    src.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": name,
        "version": "1.0.0",
        "displayName": name.replace("-", " ").title(),
        "description": "review regression",
        "author": "tester",
    }
    if crons is not None:
        manifest["crons"] = crons
    setup: dict[str, object] = {}
    if on_install:
        setup["onInstall"] = on_install
    if on_enable:
        setup["onEnable"] = on_enable
    if setup:
        manifest["setup"] = setup
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2))
    return src


async def _wait_for_file(path: Path, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not path.is_file():
        if time.monotonic() >= deadline:
            pytest.fail(f"timed out waiting for {path}")
        await asyncio.sleep(0.02)


def _wait_until_dead(pid: int, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while platform_compat.pid_exists(pid):
        if time.monotonic() >= deadline:
            pytest.fail(f"process {pid} outlived cleanup deadline")
        time.sleep(0.02)


def _wait_while_alive(pid: int, timeout: float) -> None:
    """Require *pid* to remain alive for the bounded observation window."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not platform_compat.pid_exists(pid):
            pytest.fail(f"process {pid} exited before the observation window ended")
        time.sleep(0.02)


async def _wait_until_dead_async(pid: int, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while platform_compat.pid_exists(pid):
        if time.monotonic() >= deadline:
            pytest.fail(f"process {pid} outlived cleanup deadline")
        await asyncio.sleep(0.02)


def test_install_script_cannot_replant_app_secret_over_a_victim(
    tmp_path: Path, app_home: Path
) -> None:
    victim = tmp_path / "victim" / ".bashrc"
    victim.parent.mkdir()
    victim.write_text("operator data\n", encoding="utf-8")
    src = _source(
        tmp_path,
        "secret-app",
        on_install=f"ln -s '{victim}' .app_secret",
    )

    result = install_app(src)

    assert result.ok, result.error
    assert victim.read_text(encoding="utf-8") == "operator data\n"
    secret = app_home / "apps" / "secret-app" / ".app_secret"
    assert secret.is_file() and not secret.is_symlink()
    assert secret.read_text(encoding="utf-8") != "operator data\n"
    assert stat.S_IMODE(secret.stat().st_mode) & 0o077 == 0


def test_install_sweeps_crash_partial_copies_before_a_retry(tmp_path: Path, app_home: Path) -> None:
    """A hard-killed backup copy is never treated as the authoritative backup."""
    name = "crash-partial-app"
    apps_dir().mkdir(parents=True, exist_ok=True)
    for suffix in (
        "-partial-1234-deadbeef",
        ".partial-1234-deadbeef",
        ".partial-1234-deadbeef-trash-1234-cafebabe",
    ):
        leftover = apps_dir() / f".{name}-data-script-backup{suffix}"
        leftover.mkdir()
        (leftover / "user.db").write_text("stale crash copy\n", encoding="utf-8")

    src = _source(tmp_path, name, on_install="true")
    result = install_app(src)

    assert result.ok, result.error
    survivors = [
        entry.name
        for entry in apps_dir().iterdir()
        if entry.name.startswith(f".{name}-data-script-backup-")
        or entry.name.startswith(f".{name}-data-script-backup.partial-")
    ]
    assert survivors == []


def test_readonly_data_backup_is_fully_removed(tmp_path: Path, app_home: Path) -> None:
    """Removing a preserved-data backup can chmod the failed entry's parent."""
    name = "readonly-data-app"
    src = _source(tmp_path, name, on_install="true")
    dest = apps_dir() / name
    (dest / "data" / "readonly").mkdir(parents=True)
    (dest / "data" / "readonly" / "user.db").write_text("user data\n", encoding="utf-8")
    os.chmod(dest / "data" / "readonly", stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)

    result = install_app(src)

    assert result.ok, result.error
    survivors = [
        entry.name
        for entry in apps_dir().iterdir()
        if entry.name.startswith(f".{name}-data-script-backup")
        and entry.name != f".{name}-data-script-backup"
    ]
    assert survivors == []
    assert (apps_dir() / name / "data" / "readonly" / "user.db").is_file()
    os.chmod(dest / "data" / "readonly", stat.S_IRWXU)


def test_readonly_removal_error_grants_write_to_the_entry_parent() -> None:
    """Deleting a failed entry needs the owner-write bit on its parent."""
    parent = apps_dir() / "readonly-removal-parent"
    parent.mkdir(parents=True)
    entry = parent / "user.db"
    entry.write_text("user data\n", encoding="utf-8")
    os.chmod(parent, stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)

    _remove_readonly_rmtree_error(os.remove, str(entry), None)

    assert not entry.exists()


def test_backup_rename_failure_returns_result_and_preserves_data(
    tmp_path: Path, app_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backup commit has the same cleanup and AppResult contract as copy."""
    name = "rename-failure-app"
    src = _source(tmp_path, name, on_install="true")
    dest = apps_dir() / name
    (dest / "data").mkdir(parents=True)
    (dest / "data" / "user.db").write_text("pristine\n", encoding="utf-8")
    (src / "data").mkdir()
    (src / "data" / "user.db").write_text("pristine\n", encoding="utf-8")
    real_rename = os.rename
    backup = apps_dir() / f".{name}-data-script-backup"

    def fail_backup_commit(src_path: object, dst_path: object, *args: object) -> None:
        if Path(dst_path) == backup:
            raise OSError("forced backup commit failure")
        real_rename(str(src_path), str(dst_path), *args)

    monkeypatch.setattr("kiro_crew.apps.manager.os.rename", fail_backup_commit)
    result = install_app(src)

    assert result.ok is False
    assert result.error_code == "on_install_data_backup_failed"
    assert "forced backup commit failure" in result.error
    assert not backup.exists()
    assert (apps_dir() / name / "data" / "user.db").read_text(encoding="utf-8") == "pristine\n"


def test_purge_uninstall_sweeps_app_script_backup_families(tmp_path: Path, app_home: Path) -> None:
    """Purging an app also removes the install-time crash copies for that app."""
    name = "purge-backup-app"
    src = _source(tmp_path, name)
    assert install_app(src).ok
    readonly = apps_dir() / f".{name}-data-script-backup-replaced-1234-deadbeef"
    readonly.mkdir()
    (readonly / "user.db").write_text("crash copy\n", encoding="utf-8")
    os.chmod(readonly, stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)
    for entry_name in (
        f".{name}-data-script-backup-trash-1234-deadbeef",
        f".{name}-data-script-backup-restore-1234-deadbeef",
        f".{name}-data-script-backup.partial-1234-deadbeef",
        f".{name}-data-script-backup.partial-1234-deadbeef-trash-1234-cafebabe",
    ):
        entry = apps_dir() / entry_name
        entry.mkdir()
        (entry / "user.db").write_text("crash copy\n", encoding="utf-8")

    result = uninstall_app(name, keep_data=False)

    assert result.ok, result.error
    survivors = [
        entry.name
        for entry in apps_dir().iterdir()
        if entry.name.startswith(f".{name}-data-script-backup")
        and entry.name != f".{name}-data-script-backup"
    ]
    assert survivors == []
    assert not (apps_dir() / name).exists()


def test_failed_install_restores_preserved_data_over_a_planted_link(
    tmp_path: Path, app_home: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    dest = app_home / "apps" / "link-restore-app"
    (dest / "data").mkdir(parents=True)
    (dest / "data" / "user.db").write_text("pristine\n", encoding="utf-8")
    src = _source(
        tmp_path,
        "link-restore-app",
        on_install=f"rm -rf data && ln -s '{outside}' data && exit 3",
    )

    result = install_app(src)

    assert result.ok is False
    assert result.error_code == "on_install_failed"
    restored = dest / "data" / "user.db"
    assert restored.is_file(), result.error
    assert restored.read_text(encoding="utf-8") == "pristine\n"
    assert not (outside / ".link-restore-app-data-script-backup").exists()


@pytest.mark.asyncio
async def test_timeout_sigkills_a_term_ignoring_group_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "kirocrew-home"
    (home / "apps" / "timeout-app").mkdir(parents=True)
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
    )
    from kiro_crew import sandbox

    monkeypatch.setattr(sandbox, "_allow_unsandboxed_exec", lambda: True)
    app_dir = home / "apps" / "timeout-app"
    ready = app_dir / "ready"
    member_pid = app_dir / "member.pid"
    growing = app_dir / "growing"
    script = (
        f"(trap '' TERM; while :; do echo x >> '{growing}'; sleep .1; done) & "
        f"member=$!; echo $member > '{member_pid}'; touch '{ready}'; sleep 1000"
    )

    runner_task = asyncio.create_task(
        run_lifecycle_script("timeout-app", script, timeout=5, reap_surviving_group=True)
    )
    await _wait_for_file(ready)
    await _wait_for_file(member_pid)
    result = await runner_task

    assert result["failed"] is True
    assert "timed out" in result["output"]
    assert growing.is_file()
    before = growing.stat().st_size
    await _wait_until_dead_async(int(member_pid.read_text().strip()))
    assert growing.stat().st_size == before, "TERM-ignoring straggler survived timeout"


@pytest.mark.asyncio
async def test_cancellation_sigkills_the_script_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "kirocrew-home"
    (home / "apps" / "cancel-app").mkdir(parents=True)
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
    )
    from kiro_crew import sandbox

    monkeypatch.setattr(sandbox, "_allow_unsandboxed_exec", lambda: True)
    app_dir = home / "apps" / "cancel-app"
    leader_pid = app_dir / "leader.pid"
    member_pid = app_dir / "member.pid"
    script = (
        f"echo $$ > '{leader_pid}'; "
        f"(while :; do sleep .1; done) & echo $! > '{member_pid}'; sleep 1000"
    )
    task = asyncio.create_task(run_lifecycle_script("cancel-app", script, timeout=30))
    for _ in range(100):
        if leader_pid.is_file() and member_pid.is_file():
            break
        await asyncio.sleep(0.02)
    assert leader_pid.is_file() and member_pid.is_file()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    leader = int(leader_pid.read_text().strip())
    member = int(member_pid.read_text().strip())
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        leader_alive = platform_compat.pid_exists(leader)
        member_alive = platform_compat.pid_exists(member)
        if not leader_alive and not member_alive:
            break
        await asyncio.sleep(0.02)
    assert not platform_compat.pid_exists(leader)
    assert not platform_compat.pid_exists(member)


def test_interrupted_install_authoritatively_restores_pristine_data(
    tmp_path: Path, app_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retry must treat the script-window backup, not mutated dest/data, as truth."""
    dest = app_home / "apps" / "interrupted-app"
    src = _source(tmp_path, "interrupted-app", on_install="printf mutated > data/user.db")
    dest.mkdir(parents=True)
    (dest / "data").mkdir()
    (dest / "data" / "user.db").write_text("pristine\n", encoding="utf-8")

    # Run the REAL hook, then model cancellation after it completed by raising
    # from install_app's script-phase boundary. The resulting state is the one a
    # gateway crash leaves behind: a pristine script-window backup and mutated
    # live data with no installed record.
    real_asyncio_run = asyncio.run
    interrupted = False

    def _run_real_script_then_cancel(coroutine):
        nonlocal interrupted
        real_asyncio_run(coroutine)
        interrupted = True
        raise KeyboardInterrupt

    monkeypatch.setattr("kiro_crew.apps.manager.asyncio.run", _run_real_script_then_cancel)
    with pytest.raises(KeyboardInterrupt):
        install_app(src)

    assert interrupted
    backup = app_home / "apps" / ".interrupted-app-data-script-backup"
    assert not backup.exists()
    assert (dest / "data" / "user.db").read_text(encoding="utf-8") == "pristine\n"

    retry = install_app(_source(tmp_path, "interrupted-app"))

    assert retry.ok, retry.error
    assert (dest / "data" / "user.db").read_text(encoding="utf-8") == "pristine\n"
    assert not backup.exists()


def test_install_entry_treats_an_owned_backup_as_authoritative(
    tmp_path: Path, app_home: Path
) -> None:
    """A crash can strand a backup; the next install must not delete it."""
    dest = app_home / "apps" / "stranded-backup-app"
    dest.mkdir(parents=True)
    (dest / "data").mkdir()
    (dest / "data" / "user.db").write_text("mutated\n", encoding="utf-8")
    backup = app_home / "apps" / ".stranded-backup-app-data-script-backup"
    backup.mkdir(parents=True)
    (backup / "user.db").write_text("pristine\n", encoding="utf-8")

    result = install_app(_source(tmp_path, "stranded-backup-app"))

    assert result.ok, result.error
    print("TREE", sorted(str(p) for p in dest.rglob("*")))
    assert (dest / "data" / "user.db").read_text(encoding="utf-8") == "pristine\n"
    assert not backup.exists()


def test_cli_enable_cancellation_rolls_back_to_disabled(
    tmp_path: Path, app_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse

    import kiro_crew.cli_commands as cli_mod
    from kiro_crew.apps.manager import enable_app

    src = _source(tmp_path, "cli-cancel-app", on_enable="true")
    install_app(src)
    monkeypatch.setattr(cli_mod, "_run_app_action_through_gateway", lambda *a, **k: False)
    enable_app("cli-cancel-app")
    assert (_read_app_enabled("cli-cancel-app")) is True

    real_run_lifecycle_script = cli_mod.run_lifecycle_script

    def _real_script_then_interrupt(*args, **kwargs):
        result = asyncio.run(real_run_lifecycle_script(*args, **kwargs))
        assert result["failed"] is False
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_mod, "run_lifecycle_script", _real_script_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        cli_mod._handle_app(argparse.Namespace(app_action="enable", name="cli-cancel-app"))

    assert _read_app_enabled("cli-cancel-app") is False


def _read_app_enabled(name: str) -> bool | None:
    from kiro_crew.apps.manager import app_enabled_state

    return app_enabled_state(name)


def test_cli_enable_reaps_a_manifest_writing_straggler(
    tmp_path: Path, app_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful leader cannot leave a detached manifest writer behind."""
    import argparse

    import kiro_crew.cli_commands as cli_mod

    src = _source(
        tmp_path,
        "cli-straggler-app",
        on_enable=("nohup bash -c 'sleep 2; cp evil.json app.json' " ">/dev/null 2>&1 & disown"),
    )
    evil = json.dumps(
        {
            "name": "cli-straggler-app",
            "version": "9.9.9",
            "displayName": "Evil",
            "description": "x",
            "author": "x",
        },
        separators=(",", ":"),
    )
    (src / "evil.json").write_text(evil)
    install_app(src)
    registered: list[str] = []
    monkeypatch.setattr(
        cli_mod,
        "register_app",
        lambda name: registered.append(name)
        or type("R", (), {"agents": [], "skills": [], "crons": [], "errors": []})(),
    )

    cli_mod._handle_app(argparse.Namespace(app_action="enable", name="cli-straggler-app"))
    marker = app_home / "apps" / "cli-straggler-app" / "app.json"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if json.loads(marker.read_text()).get("version") == "9.9.9":
            break
        time.sleep(0.02)

    assert json.loads(marker.read_text()).get("version") != "9.9.9"
    assert registered == ["cli-straggler-app"]


def test_failed_cli_reenable_runs_full_disable_teardown(
    tmp_path: Path, app_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse

    import kiro_crew.cli_commands as cli_mod
    from kiro_crew.apps.manager import enable_app
    from kiro_crew.cli_commands import _handle_app
    from kiro_crew.config import config_dir
    from kiro_crew.cron import CronService

    crons = [{"name": "poller", "every": 900, "message": "poll", "silent": True}]
    src = _source(tmp_path, "cli-teardown-app", crons=crons)
    install_app(src)
    enable_app("cli-teardown-app")
    assert config_dir().is_dir()
    from kiro_crew.cli_commands import _register_app_crons_to_scheduler

    _register_app_crons_to_scheduler("cli-teardown-app")
    installed = app_home / "apps" / "cli-teardown-app" / APP_MANIFEST_FILENAME
    manifest = json.loads(installed.read_text())
    manifest["setup"] = {"onEnable": "exit 7"}
    installed.write_text(json.dumps(manifest, indent=2))

    monkeypatch.setattr(cli_mod, "_run_app_action_through_gateway", lambda *a, **k: False)
    teardown: list[str] = []
    real_cleanup = cli_mod._cleanup_app_crons_from_scheduler
    real_disable = cli_mod.disable_app
    real_deregister = cli_mod.deregister_app

    def _spy_cleanup(name):
        result = real_cleanup(name)
        teardown.append("crons")
        return result

    def _spy_disable(name):
        result = real_disable(name)
        teardown.append("disable")
        return result

    def _spy_deregister(name):
        result = real_deregister(name)
        teardown.append("deregister")
        return result

    monkeypatch.setattr(cli_mod, "_cleanup_app_crons_from_scheduler", _spy_cleanup)
    monkeypatch.setattr(cli_mod, "disable_app", _spy_disable)
    monkeypatch.setattr(cli_mod, "deregister_app", _spy_deregister)
    with pytest.raises(SystemExit):
        _handle_app(argparse.Namespace(app_action="enable", name="cli-teardown-app"))

    assert teardown == ["crons", "disable", "deregister"]
    svc = CronService(base_dir=config_dir())
    assert svc.list_jobs(include_disabled=True) == []
    assert (app_home / "kiro-agents" / "cli-teardown-app").exists() is False


def test_write_app_secret_refuses_to_follow_a_link(tmp_path: Path, app_home: Path) -> None:
    from kiro_crew.dashboard.token_auth import write_app_secret

    victim = tmp_path / "victim" / ".bashrc"
    victim.parent.mkdir()
    victim.write_text("operator data\n", encoding="utf-8")
    secret_dir = app_home / "apps" / "direct-secret-app"
    secret_dir.mkdir(parents=True)
    (secret_dir / ".app_secret").symlink_to(victim)

    write_app_secret("direct-secret-app", "generated-secret")

    assert victim.read_text(encoding="utf-8") == "operator data\n"
    secret = secret_dir / ".app_secret"
    assert secret.is_file() and not secret.is_symlink()
    assert secret.read_text(encoding="utf-8") == "generated-secret"


@pytest.mark.asyncio
async def test_route_post_enable_denial_stops_fresh_backend_without_http_server(
    tmp_path: Path, app_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Directly pin the route branch: denial stops a fresh auto-started backend."""
    import kiro_crew.apps.routes as routes_mod

    src = _source(tmp_path, "route-denial-app", on_enable="true")
    install_app(src)
    stopped: list[str] = []
    monkeypatch.setattr(routes_mod, "stop_app_backend", lambda name: stopped.append(name))
    monkeypatch.setattr(
        routes_mod,
        "app_admission_denied",
        lambda name, manifest=None, action="install": "denied after rewrite",
    )

    class _State:
        owner_id = "owner"

    class _App:
        def __getitem__(self, key):
            if key == "state":
                return _State()
            raise KeyError(key)

    class _Request:
        app = _App()

        def __init__(self):
            self.match_info = {"name": "route-denial-app"}
            self.can_read_body = False

        def __getitem__(self, key):
            if key == "app":
                return ""
            if key == "user":
                return "owner"
            raise KeyError(key)

        def __contains__(self, key):
            return key in {"app", "user"}

        def get(self, key, default=None):
            return {"app": "", "user": "owner"}.get(key, default)

    response = await routes_mod.handle_enable_app(_Request())

    assert response.status == 400
    assert stopped == ["route-denial-app"]
    from kiro_crew.apps.manager import _read_installed

    meta = _read_installed("route-denial-app")
    assert meta is not None and meta.enabled is False


@pytest.mark.parametrize("error", [OSError(28, "No space left on device"), KeyboardInterrupt])
def test_partial_backup_is_never_treated_as_authoritative(
    tmp_path: Path, app_home: Path, monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    """A partial copy must never replace the complete live data set."""
    name = "partial-backup-app"
    src = _source(tmp_path, name, on_install="true")
    dest = app_home / "apps" / name
    dest.mkdir(parents=True)
    (dest / "data").mkdir()
    for index in range(5):
        (dest / "data" / f"f{index}.db").write_text(f"{index}\n", encoding="utf-8")

    calls = 0
    real_copytree = shutil.copytree

    def _partial_copytree(src: object, dst: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if "-data-script-backup.partial-" not in Path(str(dst)).name:
            return real_copytree(src, dst, **kwargs)  # type: ignore[arg-type]
        destination = Path(str(dst))
        destination.mkdir()
        (destination / "f0.db").write_text("0\n", encoding="utf-8")
        raise error

    monkeypatch.setattr(shutil, "copytree", _partial_copytree)

    if isinstance(error, OSError):
        first = install_app(src)
        assert first.ok is False
        assert first.error_code == "on_install_data_backup_failed"
    else:
        with pytest.raises(KeyboardInterrupt):
            install_app(src)

    assert calls == 2
    assert sorted(p.name for p in (dest / "data").glob("*.db")) == [
        "f0.db",
        "f1.db",
        "f2.db",
        "f3.db",
        "f4.db",
    ]

    retry = install_app(_source(tmp_path, name))
    assert retry.ok, retry.error
    assert sorted(p.name for p in (dest / "data").glob("*.db")) == [
        "f0.db",
        "f1.db",
        "f2.db",
        "f3.db",
        "f4.db",
    ]


def test_readonly_data_is_swapped_without_deleting_authoritative_data(
    tmp_path: Path, app_home: Path
) -> None:
    """Deleting trusted backups in place must not destroy live user data."""
    name = "readonly-data-app"
    src = _source(tmp_path, name, on_install="true")
    dest = app_home / "apps" / name
    dest.mkdir(parents=True)
    (dest / "data" / "zz-readonly").mkdir(parents=True)
    (dest / "data" / "a.db").write_text("important\n", encoding="utf-8")
    (dest / "data" / "zz-readonly" / "blob").write_text("blob\n", encoding="utf-8")
    os.chmod(dest / "data" / "zz-readonly", 0o555)
    try:
        first = install_app(src)
        assert first.ok, first.error
        assert (dest / "data" / "a.db").read_text(encoding="utf-8") == "important\n"
        assert (dest / "data" / "zz-readonly" / "blob").read_text(encoding="utf-8") == "blob\n"
    finally:
        for path in (app_home / "apps").rglob("*"):
            if path.is_dir():
                os.chmod(path, 0o755)


def test_authoritative_backup_restore_never_follows_a_symlinked_dest(
    tmp_path: Path, app_home: Path
) -> None:
    """Entry restore must not follow a script-planted dest link."""
    name = "dest-link-app"
    target = tmp_path / "dest-link-target"
    target.mkdir(parents=True)
    (app_home / "apps").mkdir(parents=True, exist_ok=True)
    (target / "data").mkdir()
    (target / "data" / "outside.db").write_text("do not delete\n", encoding="utf-8")
    dest = app_home / "apps" / name
    dest.symlink_to(target, target_is_directory=True)
    backup = app_home / "apps" / f".{name}-data-script-backup"
    backup.mkdir()
    (backup / "restored.db").write_text("authoritative\n", encoding="utf-8")

    result = install_app(_source(tmp_path, name))

    assert result.ok, result.error
    assert (target / "data" / "outside.db").read_text(encoding="utf-8") == "do not delete\n"
    assert (dest / "data" / "restored.db").read_text(encoding="utf-8") == "authoritative\n"


def test_failed_restore_rolls_back_to_disabled(tmp_path: Path, app_home: Path, monkeypatch):
    """A failed interrupted-install restore must re-raise and name the backup."""
    from kiro_crew.apps.manager import AppResult

    name = "interrupt-restore-app"
    src = _source(tmp_path, name, on_install="true")
    dest = app_home / "apps" / name
    dest.mkdir(parents=True)
    (dest / "data").mkdir()
    (dest / "data" / "user.db").write_text("pristine\n", encoding="utf-8")

    real_asyncio_run = asyncio.run

    def _run_real_script_then_interrupt(coroutine):
        real_asyncio_run(coroutine)
        raise KeyboardInterrupt

    def _failed_restore(*args, **kwargs):
        return AppResult(
            ok=False,
            name=name,
            error="restore failed; authoritative backup retained",
            error_code="on_install_restore_failed",
        )

    monkeypatch.setattr("kiro_crew.apps.manager.asyncio.run", _run_real_script_then_interrupt)
    monkeypatch.setattr(
        "kiro_crew.apps.manager._remove_installed_tree_after_script", _failed_restore
    )
    with pytest.raises(RuntimeError) as caught:
        install_app(src)
    assert "authoritative backup retained" in str(caught.value)
    assert isinstance(caught.value.__cause__, KeyboardInterrupt)


@pytest.mark.asyncio
async def test_timeout_cancellation_during_grace_still_sigkills_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "kirocrew-home"
    (home / "apps" / "grace-cancel-app").mkdir(parents=True)
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
    )
    from kiro_crew import sandbox
    from kiro_crew.apps import lifecycle_scripts

    monkeypatch.setattr(sandbox, "_allow_unsandboxed_exec", lambda: True)
    monkeypatch.setattr(lifecycle_scripts, "_TERM_GRACE_SECS", 10)
    app_dir = home / "apps" / "grace-cancel-app"
    ready = app_dir / "ready"
    leader_pid = app_dir / "leader.pid"
    pid_file = app_dir / "pid"
    script = (
        "(trap '' TERM; while :; do sleep .1; done) & member=$!; "
        "echo $member > pid; echo $$ > leader.pid; touch ready; wait $member"
    )
    task = asyncio.create_task(run_lifecycle_script("grace-cancel-app", script, timeout=10))
    await _wait_for_file(ready)
    await _wait_for_file(pid_file)
    await _wait_for_file(leader_pid)
    leader = int(leader_pid.read_text().strip())
    member = int(pid_file.read_text().strip())

    # The runner's generous timeout is the only timed wait. Once the default
    # TERM kills the leader while the trapping child remains, the runner is
    # deterministically inside the grace wait.
    await _wait_until_dead_async(leader, timeout=10)
    assert platform_compat.pid_exists(member)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _wait_until_dead_async(member)


def test_on_enable_background_process_survives_for_self_managed_app(
    tmp_path: Path, app_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Self-managed hooks may intentionally launch detached work."""
    import argparse

    import kiro_crew.cli_commands as cli_mod
    from kiro_crew.apps.manager import _read_installed

    name = "selfmanaged-background-app"
    app_dir = app_home / "apps" / name
    src = _source(
        tmp_path,
        name,
        on_enable="nohup sleep 2 >/dev/null 2>&1 & echo $! > background.pid",
    )
    install_app(src)
    meta = _read_installed(name)
    assert meta is not None
    meta.resources = "app"
    _write_installed(name, meta)

    monkeypatch.setattr(cli_mod, "_run_app_action_through_gateway", lambda *a, **k: False)
    cli_mod._handle_app(argparse.Namespace(app_action="enable", name=name))
    pid = int((app_dir / "background.pid").read_text().strip())
    _wait_while_alive(pid, 0.5)
    assert platform_compat.pid_exists(pid)
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)


def test_on_enable_background_process_is_reaped_for_gateway_app(
    tmp_path: Path, app_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gateway-managed onEnable may not leave unregistered group members."""
    import argparse

    import kiro_crew.cli_commands as cli_mod

    name = "gateway-background-app"
    app_dir = app_home / "apps" / name
    src = _source(
        tmp_path,
        name,
        on_enable="nohup sleep 2 >/dev/null 2>&1 & echo $! > background.pid",
    )
    install_app(src)
    monkeypatch.setattr(cli_mod, "_run_app_action_through_gateway", lambda *a, **k: False)
    cli_mod._handle_app(argparse.Namespace(app_action="enable", name=name))
    pid = int((app_dir / "background.pid").read_text().strip())
    _wait_until_dead(pid, 2)
