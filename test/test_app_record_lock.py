"""One writer of an app's ``installed.json`` at a time (``app_record_lock``).

Every record writer reads the record, changes it and replaces the file. Two
writers that overlap lose one change: a dev-mode save that read the record
before a widening update can land after it and undo its consent-pending mark.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading

import pytest
from app_manager_helpers import app_home_fixture  # noqa: F401 - fixture
from app_manager_helpers import _make_app_source

from kiro_crew import platform_compat
from kiro_crew.apps import manager as manager_mod
from kiro_crew.apps.dev_mode import set_dev_mode
from kiro_crew.apps.manager import (
    INSTALLED_META_FILENAME,
    app_dir,
    app_record_lock,
    enable_app,
    hold_app_record_lock,
    install_app,
    update_app,
)
from kiro_crew.apps.permissions import app_can_manage_session_approvals
from kiro_crew.testing.wait import wait_until

_KEY = "sessionApprovalConsentPending"


def _raw_record(name: str) -> dict:
    return json.loads((app_dir(name) / INSTALLED_META_FILENAME).read_text(encoding="utf-8"))


def test_the_lock_is_reentrant_in_one_thread(app_home):
    held = manager_mod._record_lock_state
    with app_record_lock("test-app"):
        assert held.held["test-app"] == 1
        with app_record_lock("test-app"):
            assert held.held["test-app"] == 2
        with app_record_lock("test-app"):
            assert held.held["test-app"] == 2
        assert held.held["test-app"] == 1
    assert "test-app" not in held.held


def test_holding_outside_a_locked_writer_refuses(app_home):
    with pytest.raises(RuntimeError):
        hold_app_record_lock("test-app")


def test_another_process_cannot_take_a_held_lock(app_home, tmp_path):
    lock_path = manager_mod._record_lock_path("test-app")
    probe = (
        "import os, sys\n"
        "from kiro_crew import platform_compat\n"
        "fd = platform_compat.open_create_or_existing(sys.argv[1], os.O_RDWR)\n"
        "try:\n"
        "    with platform_compat.file_lock(fd, exclusive=True, wait=False):\n"
        "        sys.exit(0)\n"
        "except BlockingIOError:\n"
        "    sys.exit(3)\n"
    )
    env = {**os.environ, "KIROCREW_HOME": str(app_home)}
    with app_record_lock("test-app"):
        held = subprocess.run(
            [sys.executable, "-c", probe, str(lock_path)], cwd=tmp_path, env=env, timeout=120
        )
    free = subprocess.run(
        [sys.executable, "-c", probe, str(lock_path)], cwd=tmp_path, env=env, timeout=120
    )
    assert held.returncode == 3
    assert free.returncode == 0


def test_the_lock_lives_outside_the_app_tree(app_home):
    assert not manager_mod._record_lock_path("test-app").is_relative_to(manager_mod.apps_dir())


def test_a_dev_mode_save_cannot_undo_a_widening_update(tmp_path, app_home, monkeypatch):
    assert install_app(_make_app_source(tmp_path)).ok
    assert enable_app("test-app").ok
    v2 = _make_app_source(tmp_path / "v2", version="2.0.0", permissions={"sessionApproval": True})

    lock_fds: set[int] = set()
    real_open = platform_compat.open_create_or_existing
    real_lock = platform_compat.file_lock
    real_write = manager_mod.atomic_write
    dev_at_write, release_dev, update_waits = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    threads: dict[str, threading.Thread] = {}

    def _open(path, *args, **kwargs):
        fd = real_open(path, *args, **kwargs)
        if str(path).endswith(os.path.join("app-record-locks", "test-app.lock")):
            lock_fds.add(fd)
        return fd

    def _lock(fd, *args, **kwargs):
        if fd in lock_fds and threading.current_thread() is threads.get("update"):
            update_waits.set()
        return real_lock(fd, *args, **kwargs)

    def _write(path, *args, **kwargs):
        dev_thread = threading.current_thread() is threads.get("dev")
        if dev_thread and str(path).endswith(INSTALLED_META_FILENAME) and not dev_at_write.is_set():
            dev_at_write.set()
            release_dev.wait()
        return real_write(path, *args, **kwargs)

    monkeypatch.setattr(platform_compat, "open_create_or_existing", _open)
    monkeypatch.setattr(platform_compat, "file_lock", _lock)
    monkeypatch.setattr(manager_mod, "atomic_write", _write)
    results: dict[str, object] = {}
    threads["dev"] = threading.Thread(
        target=lambda: results.update(dev=set_dev_mode("test-app", False))
    )
    threads["update"] = threading.Thread(target=lambda: results.update(update=update_app(v2)))
    try:
        threads["dev"].start()
        wait_until(dev_at_write.is_set, describe=lambda: "dev-mode save never reached its write")
        threads["update"].start()
        # The update must wait on the record lock the dev-mode save holds.
        wait_until(update_waits.is_set, describe=lambda: "update did not wait on the record lock")
    finally:
        release_dev.set()
        for t in threads.values():
            if t.ident is not None:
                wait_until(lambda t=t: not t.is_alive(), describe=lambda: f"{t.name} hung")

    assert results["update"].ok, results["update"]
    record = _raw_record("test-app")
    assert record["enabled"] is False
    assert record[_KEY] is True
    assert app_can_manage_session_approvals("test-app") is False


def test_every_record_writer_takes_the_lock_before_it_reads():
    import ast
    from pathlib import Path

    import kiro_crew.apps as apps_pkg

    missing = []
    for path in sorted(Path(apps_pkg.__file__).parent.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef) or fn.name.startswith("_write_installed"):
                continue
            calls = [
                c for c in ast.walk(fn) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
            ]
            writes = [c for c in calls if c.func.id == "_write_installed"]
            if not writes:
                continue
            holds = [c.lineno for c in calls if c.func.id == "hold_app_record_lock"]
            reads = [c.lineno for c in calls if c.func.id == "_read_installed"]
            first = min(reads + [w.lineno for w in writes])
            decorated = any(
                isinstance(d, ast.Name) and d.id == "holds_app_record_locks"
                for d in fn.decorator_list
            )
            if not (decorated and holds and min(holds) < first):
                missing.append(f"{path.name}:{fn.name}")
    assert missing == []
