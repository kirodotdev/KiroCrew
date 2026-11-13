"""What a hard-killed ``update_app`` leaves beside the app directory, and the sweep.

``update_app`` writes the new ``installed.json`` and only then removes the retired
tree (``.<name>-update-old-<pid>-<hex>``) and its token in
``.<name>-update-inflight``. A kill in either gap leaves those behind. Every walk of
``apps_dir()`` skips them, and ``manager.sweep_completed_swap_leftovers`` removes
them once nothing can still need them. The two crash points are driven in a real
child process that exits hard (``os._exit``), so no ``finally`` or ``__exit__`` runs.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from app_manager_helpers import _make_app_source, app_home_fixture  # noqa: F401 - fixture

from kiro_crew.apps import manager, manager_adopt
from kiro_crew.subprocess_utf8 import UTF8_TEXT
from kiro_crew.testing.ids import unallocatable_pids
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

NAME = "test-app"

#: Owners of fabricated leftovers: pids no OS hands out, so never this worker's
#: own, which the sweep always counts as alive whatever ``pid_exists`` says.
PID, OTHER_PID = unallocatable_pids(2)

#: Runs ``update_app`` and exits hard at the named gap. ``retired`` is the call that
#: removes the retired tree (the record is durable); ``marker`` is the update
#: marker's exit (the retired tree is already gone).
_KILLED_UPDATE = """
import os, sys
from pathlib import Path
from kiro_crew.apps import manager, manager_install, manager_records

source, gap = Path(sys.argv[1]), sys.argv[2]
if gap == "retired":
    real = manager_install._remove_any_shape
    def remove(path):
        if "-update-old-" in Path(path).name:
            os._exit(0)
        return real(path)
    manager_install._remove_any_shape = remove
else:
    manager_records._UpdateMarker.__exit__ = lambda *_a: os._exit(0)
manager.update_app(source, expected_name="test-app")
os._exit(3)
"""


def _install(tmp_path: Path) -> None:
    result = manager.install_app(_make_app_source(tmp_path / "v1", version="0.9.0"))
    assert result.ok, result.error
    (manager.app_dir(NAME) / "data").mkdir(exist_ok=True)
    (manager.app_dir(NAME) / "data" / "store.txt").write_text("kept", encoding="utf-8")


def _killed_update(tmp_path: Path, gap: str) -> int:
    """Update to 1.0.0 in a child that exits hard at *gap*; return the child's pid."""
    source = _make_app_source(tmp_path / "v2", version="1.0.0")
    child = subprocess.Popen(  # noqa: S603 - this interpreter, a literal script
        [sys.executable, "-c", _KILLED_UPDATE, str(source), gap],
        env=dict(os.environ),
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **UTF8_TEXT,
    )
    _out, err = child.communicate(timeout=FALLBACK_TIMEOUT_SECS)
    assert child.returncode == 0, err
    return child.pid


def _sidecars() -> list[str]:
    return sorted(p.name for p in manager.apps_dir().iterdir() if p.name.startswith("."))


@pytest.fixture
def dead(monkeypatch):
    """Pids the sweep must read as gone; every other pid reads as alive."""
    gone: set[int] = set()
    monkeypatch.setattr(manager_adopt, "pid_exists", lambda pid: pid not in gone)
    return gone


@pytest.mark.parametrize("gap", ["retired", "marker"])
def test_a_kill_after_the_record_leaves_one_row_and_the_sweep_clears_the_rest(
    app_home, tmp_path, dead, gap
):
    _install(tmp_path)
    dead_pid = _killed_update(tmp_path, gap)
    dead.add(dead_pid)
    retired = f"{manager._update_retired_tree_prefix(NAME)}{dead_pid}-"
    left = _sidecars()
    assert f".{NAME}-update-inflight" in left
    assert any(s.startswith(retired) for s in left) is (gap == "retired")
    assert [(row["name"], row["version"]) for row in manager.list_apps()] == [(NAME, "1.0.0")]
    assert manager._update_in_progress(NAME)

    assert manager.swap_leftover_names() == [NAME]
    assert manager.sweep_completed_swap_leftovers(NAME) is True
    assert _sidecars() == []
    assert not manager._update_in_progress(NAME)
    assert (manager.app_dir(NAME) / "data" / "store.txt").read_text(encoding="utf-8") == "kept"
    assert manager._read_installed(NAME).version == "1.0.0"
    assert manager.sweep_completed_swap_leftovers(NAME) is False, "nothing left the second time"


def _leftovers(pid: int, *, record: str = "0.9.0") -> tuple[Path, Path]:
    """A retired tree holding an older record, and a token, both owned by *pid*."""
    root = manager.apps_dir()
    retired = root / f"{manager._update_retired_tree_prefix(NAME)}{pid}-ab12cd34"
    retired.mkdir()
    (retired / "installed.json").write_text(
        json.dumps({"name": NAME, "version": record, "origin": "local"}), encoding="utf-8"
    )
    (retired / "app.json").write_text(json.dumps({"name": NAME}), encoding="utf-8")
    token = manager._update_marker_dir(NAME) / f"{pid}-ab12cd34"
    token.parent.mkdir(exist_ok=True)
    token.touch()
    return retired, token


def test_no_walk_of_the_apps_dir_reads_a_retired_tree_as_an_app(app_home, tmp_path):
    _install(tmp_path)
    retired, _token = _leftovers(PID)
    (retired / "installed.json").write_text(
        json.dumps({"name": NAME, "version": "0.8.0", "origin": "builtin", "dev": True}),
        encoding="utf-8",
    )
    assert [(row["name"], row["version"]) for row in manager.list_apps()] == [(NAME, "0.9.0")]
    manager.invalidate_orphan_cache()
    assert retired.name not in manager.detect_orphaned_builtins()
    from kiro_crew.apps import dev_mode

    assert retired.name not in dev_mode._scan_installed_dev_apps()


def test_a_live_process_keeps_its_token_and_every_retired_tree(app_home, tmp_path, dead):
    _install(tmp_path)
    retired, token = _leftovers(PID)
    dead.add(OTHER_PID)
    _other_retired, other_token = _leftovers(OTHER_PID)
    assert manager.sweep_completed_swap_leftovers(NAME) is True
    assert token.exists() and retired.is_dir(), "its owner may still be mid-update"
    assert not other_token.exists()
    assert _other_retired.is_dir(), "no tree goes while any update may be in flight"


def test_a_retired_tree_of_a_live_process_is_kept(app_home, tmp_path, dead):
    _install(tmp_path)
    retired, token = _leftovers(PID)
    token.unlink()
    assert manager.sweep_completed_swap_leftovers(NAME) is True, "the empty marker goes"
    assert retired.is_dir()


def test_this_process_and_an_unparseable_owner_count_as_alive(app_home, tmp_path, dead):
    _install(tmp_path)
    retired, token = _leftovers(os.getpid())
    dead.add(os.getpid())
    odd = manager._update_marker_dir(NAME) / "not-a-pid"
    odd.touch()
    assert manager.sweep_completed_swap_leftovers(NAME) is False
    assert retired.is_dir() and token.exists() and odd.exists()


def test_without_a_record_a_killed_swap_is_left_to_recovery(app_home, tmp_path, dead):
    """The old tree is aside and no new record exists: the marker is what guards it."""
    _install(tmp_path)
    dest = manager.app_dir(NAME)
    dead.add(PID)
    retired, token = _leftovers(PID)
    (dest / "installed.json").unlink()
    assert manager.sweep_completed_swap_leftovers(NAME) is False
    assert retired.is_dir() and token.exists()


@pytest.mark.parametrize("aside", ["data-tmp", "secret-tmp"])
def test_without_a_record_the_marker_stays_while_anything_is_set_aside(
    app_home, tmp_path, dead, aside
):
    _install(tmp_path)
    dead.add(PID)
    retired, token = _leftovers(PID)
    manager._remove_any_shape(retired)
    (manager.app_dir(NAME) / "installed.json").unlink()
    (manager.apps_dir() / f".{NAME}-{aside}").write_text("x", encoding="utf-8")
    assert manager.sweep_completed_swap_leftovers(NAME) is False
    assert token.exists()


def test_without_a_record_a_dead_marker_with_nothing_aside_goes(app_home, tmp_path, dead):
    """An uninstall after the kill: the marker alone would keep the name mid-update."""
    _install(tmp_path)
    dead.add(PID)
    retired, _token = _leftovers(PID)
    manager._remove_any_shape(retired)
    assert manager.uninstall_app(NAME).ok
    assert manager._update_in_progress(NAME)
    assert manager.swap_leftover_names() == [NAME]
    assert manager.sweep_completed_swap_leftovers(NAME) is True
    assert not manager._update_in_progress(NAME)


def test_a_marker_of_an_unexpected_shape_is_left_alone(app_home, tmp_path, dead):
    _install(tmp_path)
    dead.add(PID)
    retired, _token = _leftovers(PID)
    marker = manager._update_marker_dir(NAME)
    manager._remove_any_shape(marker)
    marker.write_text("", encoding="utf-8")
    assert manager.sweep_completed_swap_leftovers(NAME) is False
    assert retired.is_dir()


def test_another_apps_sidecars_are_never_read_as_this_apps_leftovers(app_home, tmp_path, dead):
    """``<name>-update-old-<digits>`` is an admissible app name of its own."""
    _install(tmp_path)
    other = f"{NAME}-update-old-2026"
    root = manager.apps_dir()
    dead.add(2026)
    kept = [root / f".{other}-data-tmp", root / f".{other}-secret-tmp"]
    kept[0].mkdir()
    (kept[0] / "store.txt").write_text("theirs", encoding="utf-8")
    kept[1].write_text("secret", encoding="utf-8")
    other_marker = root / f".{other}-update-inflight"
    other_marker.mkdir()
    (other_marker / f"{PID}-ab12cd34").touch()
    assert manager.swap_leftover_names() == [other]
    assert manager._retired_trees(NAME) == []
    assert manager.sweep_completed_swap_leftovers(NAME) is False
    assert (kept[0] / "store.txt").read_text(encoding="utf-8") == "theirs"
    assert kept[1].is_file() and other_marker.is_dir()


@pytest.mark.parametrize("stem", [f"{PID}-ab12", f"{PID}-ab12cd34-x", f"{PID}-AB12CD34"])
def test_a_stem_update_app_never_writes_counts_as_alive(app_home, tmp_path, dead, stem):
    _install(tmp_path)
    dead.add(PID)
    odd = manager.apps_dir() / f"{manager._update_retired_tree_prefix(NAME)}{stem}"
    odd.mkdir()
    assert manager._retired_trees(NAME) == []
    assert manager.swap_leftover_names() == []
    assert manager.sweep_completed_swap_leftovers(NAME) is False
    assert odd.is_dir()


@pytest.mark.asyncio
async def test_the_sweep_after_the_listener_survives_one_failing_app(
    app_home, tmp_path, dead, monkeypatch
):
    _install(tmp_path)
    dead.add(PID)
    retired, token = _leftovers(PID)
    calls: list[str] = []
    real = manager_adopt.sweep_completed_swap_leftovers

    def sweep(name):
        calls.append(name)
        if name == "broken-app":
            raise RuntimeError("one app's sweep fails")
        return real(name)

    monkeypatch.setattr(manager, "swap_leftover_names", lambda: ["broken-app", NAME])
    monkeypatch.setattr(manager, "sweep_completed_swap_leftovers", sweep)
    await manager.sweep_interrupted_updates()
    assert calls == ["broken-app", NAME]
    assert not retired.exists() and not token.exists()


@pytest.mark.asyncio
async def test_the_boot_leaves_the_sweep_to_a_task_after_the_listener(
    app_home, tmp_path, dead, monkeypatch
):
    """Before APPLY nothing lists the apps directory; the kick only schedules the sweep."""
    from types import SimpleNamespace

    from kiro_crew.apps import builtin_adoption
    from kiro_crew.dashboard.server_runtime import app_platform

    _install(tmp_path)
    dead.add(PID)
    retired, token = _leftovers(PID)

    def listed():
        raise AssertionError("the apps directory was listed before the listener served")

    with monkeypatch.context() as boot:
        boot.setattr(manager, "swap_leftover_names", listed)

        async def apply():
            return {}

        boot.setattr(builtin_adoption, "apply_builtin_adoptions", apply)
        boot.setattr(builtin_adoption, "released_builtin_packages", lambda _names: frozenset())
        await app_platform._apply_builtin_adoptions({})
    assert retired.is_dir() and token.exists()

    state = SimpleNamespace(_background_tasks=set())
    app_platform._kick_interrupted_update_sweep(state)
    (task,) = state._background_tasks
    await asyncio.wait_for(task, FALLBACK_TIMEOUT_SECS)
    assert not retired.exists() and not token.exists()
    assert state._background_tasks == set()
