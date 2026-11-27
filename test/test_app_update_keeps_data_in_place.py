"""``update_app`` replaces an app's own files and leaves ``data/`` and ``.app_secret`` in place.

The app directory, its ``data/`` and its ``.app_secret`` keep their identity across an
update and an adoption, so a sandbox that is already running keeps its fences on them
(the real-spawn case below). The old root entries leave through ``.<name>-update-out-``
(renamed ``-update-old-`` once all are out), the new ones arrive from
``.<name>-update-new-``, and every restore path -- the in-process rollback and the
recovery of a hard kill -- puts the old entries back the same way. A hard kill is
simulated by a ``BaseException`` from the Nth filesystem step, so no except arm runs.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from app_manager_helpers import _make_app_source, app_home_fixture  # noqa: F401 - fixture

from kiro_crew import pinned_fs, platform_compat, sandbox
from kiro_crew.apps import (
    credential_masks,
    manager,
    manager_adopt,
    manager_records,
)
from kiro_crew.testing.ids import UNALLOCATABLE_PID
from kiro_crew.testing.links import make_dir_link
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

NAME = "test-app"
SECRET = b"s3cret-bytes"


class _HardKill(BaseException):
    """Stands in for the process dying: no ``except Exception`` arm catches it."""


@pytest.fixture(autouse=True)
def _fresh_cache():
    credential_masks.clear_cache()
    yield
    credential_masks.clear_cache()


def _source(tmp_path: Path, version: str, **manifest) -> Path:
    src = _make_app_source(tmp_path / f"src-{version}", version=version, **manifest)
    (src / f"only-{version}.txt").write_text(version, encoding="utf-8")
    (src / "ui").mkdir()
    (src / "ui" / "index.html").write_text(f"ui {version}", encoding="utf-8")
    return src


def _install(tmp_path: Path, **manifest) -> Path:
    result = manager.install_app(_source(tmp_path, "1.0.0", **manifest))
    assert result.ok, result.error
    dest = manager.app_dir(NAME)
    (dest / "data" / "sub").mkdir(parents=True, exist_ok=True)
    (dest / "data" / "store.txt").write_text("kept", encoding="utf-8")
    (dest / "data" / "sub" / "deep.bin").write_bytes(b"\x00\x01user")
    (dest / ".app_secret").write_bytes(SECRET)
    return dest


def _snapshot(root: Path, *, skip: tuple[str, ...] = ()) -> dict[str, bytes | str]:
    """Every path under *root* (minus root entries in *skip*) with its bytes."""
    found: dict[str, bytes | str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if rel.parts[0] in skip:
            continue
        found[rel.as_posix()] = path.read_bytes() if path.is_file() else "<dir>"
    return found


def _identity(path: Path) -> tuple[int, int]:
    st = os.lstat(path)
    return (st.st_dev, st.st_ino)


def _siblings() -> list[str]:
    """The update's sibling trees and marker beside the app directory."""
    return sorted(
        p.name
        for p in manager.apps_dir().iterdir()
        if p.name.startswith(f".{NAME}-update-")
        or (p.name.startswith(f".{NAME}-") and p.name.endswith("-tmp"))
    )


_OWN = (".app_secret", "data", "installed.json")


# --------------------------------------------------------------------------- #
# 1. What stays in place, on every OS
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", ["update", "adoption"])
def test_an_update_keeps_the_app_directory_its_data_and_its_secret_in_place(
    app_home, tmp_path, monkeypatch, path
):
    dest = _install(tmp_path)
    if path == "adoption":
        record = manager._read_installed(NAME)
        manager._write_installed(NAME, replace(record, source="builtin", origin="builtin"))
    kept = {leaf: _identity(dest / leaf) for leaf in ("", "data", ".app_secret")}
    data_before = _snapshot(dest / "data")
    tmp_data = dest.parent / f".{NAME}-data-tmp"
    targets: list[str] = []
    test_thread = threading.get_ident()
    for fn in ("replace", "rename"):
        real = getattr(os, fn)

        def spy(src, dst, *a, _real=real, **k):
            if threading.get_ident() == test_thread:  # only the update's own renames
                targets.append(Path(dst).name)
            return _real(src, dst, *a, **k)

        monkeypatch.setattr(os, fn, spy)
    mid: list[dict] = []
    real_write = manager._write_installed

    def look_then_write(name, meta, **kwargs):
        if meta.version == "2.0.0":
            mid.append(
                {
                    "data_tmp": os.path.lexists(tmp_data),
                    "ids": {leaf: _identity(dest / leaf) for leaf in kept},
                }
            )
        return real_write(name, meta, **kwargs)

    monkeypatch.setattr(manager, "_write_installed", look_then_write)
    source = _source(tmp_path, "2.0.0")
    if path == "adoption":
        result = manager.adopt_builtin_record(
            NAME, source, repository="https://example.test/acme/test-app", commit="a" * 40
        )
    else:
        result = manager.update_app(source, expected_name=NAME)
    assert result.ok, result.error

    assert mid == [{"data_tmp": False, "ids": kept}], "mid-swap: nothing moved, nothing aside"
    assert tmp_data.name not in targets
    assert {leaf: _identity(dest / leaf) for leaf in kept} == kept
    assert _snapshot(dest / "data") == data_before
    assert (dest / ".app_secret").read_bytes() == SECRET
    assert not (dest / "only-1.0.0.txt").exists()
    assert (dest / "only-2.0.0.txt").read_text(encoding="utf-8") == "2.0.0"
    assert (dest / "ui" / "index.html").read_text(encoding="utf-8") == "ui 2.0.0"
    assert manager._read_installed(NAME).version == "2.0.0"
    assert _siblings() == []


def test_a_case_folded_data_dir_is_kept_by_identity(app_home, tmp_path):
    probe = tmp_path / "CaseProbe"
    probe.mkdir()
    if not (tmp_path / "caseprobe").exists():
        pytest.skip("this filesystem is case-sensitive: `Data` is not the data directory")
    src = _source(tmp_path, "1.0.0")
    (src / "Data").mkdir()
    (src / "Data" / "store.txt").write_text("kept", encoding="utf-8")
    assert manager.install_app(src).ok
    dest = manager.app_dir(NAME)
    assert "Data" in os.listdir(dest)
    before = _identity(dest / "data")
    assert manager.update_app(_source(tmp_path, "2.0.0"), expected_name=NAME).ok
    assert _identity(dest / "data") == before
    assert (dest / "data" / "store.txt").read_text(encoding="utf-8") == "kept"


# --------------------------------------------------------------------------- #
# 2. A sandbox already running keeps its fences (Linux namespace launcher)
# --------------------------------------------------------------------------- #

_CHILD = r"""
import errno, json, os, sys

notes, secret = sys.argv[1], sys.argv[2]

def write(path):
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("agent")
        return "ok"
    except OSError as exc:
        return errno.errorcode.get(exc.errno, str(exc.errno))

def report(tag):
    with open(secret, encoding="utf-8") as fh:
        seen = fh.read()
    print(tag + " " + json.dumps({
        "write": write(os.path.join(notes, "kept.md")),
        "create": write(os.path.join(notes, "new-" + tag + ".md")),
        "secret": seen,
    }), flush=True)

report("BEFORE")
sys.stdin.readline()
report("AFTER")
"""


@pytest.mark.skipif(sys.platform != "linux", reason="the namespace launcher is Linux-only")
def test_a_running_sandbox_keeps_its_seal_and_mask_across_an_update(tmp_path, monkeypatch):
    from kiro_crew.testing.harness import terminate_pgid

    if not sandbox.userns_available():
        pytest.skip("this host cannot build the namespace sandbox")
    home = (tmp_path / "home").resolve()
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setenv("KIROCREW_ASSUME_GATEWAY_RUNNING", "0")
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
    )
    sandbox.reset_backend()
    fences = {"agentReadOnlyDirs": ["notes"], "credentialMasks": ["secret.json"]}
    assert manager.install_app(_source(tmp_path, "1.0.0", **fences)).ok
    data = home / "apps" / NAME / "data"
    (data / "notes").mkdir(parents=True, exist_ok=True)
    (data / "notes" / "kept.md").write_text("user", encoding="utf-8")
    (data / "secret.json").write_text('{"token":"s3cr3t"}', encoding="utf-8")

    argv = [sys.executable, "-c", _CHILD, str(data / "notes"), str(data / "secret.json")]
    env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SANDBOX_ACTIVE"}
    child = subprocess.Popen(  # noqa: S603 - this interpreter, a literal script
        sandbox.namespace_argv(argv, "standard"),
        cwd=home,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        start_new_session=True,
    )
    lines: queue.Queue[str] = queue.Queue()
    reader = threading.Thread(target=lambda: [lines.put(ln) for ln in child.stdout], daemon=True)
    reader.start()

    def report(tag: str) -> dict:
        while True:
            line = lines.get(timeout=FALLBACK_TIMEOUT_SECS)
            if line.startswith(tag + " "):
                return json.loads(line[len(tag) + 1 :])

    try:
        fenced = {"write": "EROFS", "create": "EROFS", "secret": ""}
        assert report("BEFORE") == fenced
        result = manager.update_app(_source(tmp_path, "2.0.0", **fences), expected_name=NAME)
        assert result.ok, result.error
        child.stdin.write("\n")
        child.stdin.flush()
        assert report("AFTER") == fenced
        assert child.wait(timeout=FALLBACK_TIMEOUT_SECS) == 0
    finally:
        terminate_pgid(child.pid, pgid=child.pid, wait=child.wait)
        reader.join(timeout=FALLBACK_TIMEOUT_SECS)
        sandbox.reset_backend()
    assert (data / "notes" / "kept.md").read_text(encoding="utf-8") == "user"
    assert json.loads((home / "apps" / NAME / "app.json").read_text())["version"] == "2.0.0"


# --------------------------------------------------------------------------- #
# 3. The in-process rollback
# --------------------------------------------------------------------------- #


def _fault_at(monkeypatch, stage: str) -> None:
    """Raise ``OSError`` from the first filesystem step of *stage*."""
    real_replace = os.replace
    real_move = manager._move_entry
    prefix = f".{NAME}-update-"

    def faulty(src, dst, *a, **k):
        src, dst = Path(src), Path(dst)
        hit = {
            "out-to-old": dst.name.startswith(prefix + "old-"),
            "put-back": src.name == f".{NAME}-data-tmp",
        }.get(stage, False)
        if hit:
            raise OSError(f"injected at {stage}")
        return real_replace(src, dst, *a, **k)

    def faulty_move(src, entry, dst):
        hit = {
            "move-out": dst.path.name.startswith(prefix + "out-") and entry == "app.json",
            "move-in": src.path.name.startswith(prefix + "new-") and entry == "ui",
        }.get(stage, False)
        if hit:
            raise OSError(f"injected at {stage}")
        return real_move(src, entry, dst)

    monkeypatch.setattr(os, "replace", faulty)
    monkeypatch.setattr(manager, "_move_entry", faulty_move)


@pytest.mark.parametrize("stage", ["move-out", "out-to-old", "move-in", "put-back", "record"])
def test_a_failed_update_puts_the_old_entries_back(app_home, tmp_path, monkeypatch, stage):
    dest = _install(tmp_path)
    home_data = dest / "data"
    if stage == "put-back":
        os.rename(home_data, dest.parent / f".{NAME}-data-tmp")
        home_data = dest.parent / f".{NAME}-data-tmp"
    ids = {"dir": _identity(dest), "data": _identity(home_data)}
    data_before = _snapshot(home_data)
    old = _snapshot(dest, skip=_OWN)
    moved: list[str] = []
    test_thread = threading.get_ident()
    for fn in ("replace", "rename"):
        real = getattr(os, fn)

        def spy(src, dst, *a, _real=real, **k):
            if threading.get_ident() == test_thread:  # only the update's own renames
                moved.append(Path(src).name)
            return _real(src, dst, *a, **k)

        monkeypatch.setattr(os, fn, spy)
    if stage == "record":
        real_write = manager._write_installed

        def failing_write(name, meta, **kwargs):
            if meta.version == "2.0.0":
                raise OSError("injected at record")
            return real_write(name, meta, **kwargs)

        monkeypatch.setattr(manager, "_write_installed", failing_write)
    else:
        _fault_at(monkeypatch, stage)

    result = manager.update_app(_source(tmp_path, "2.0.0"), expected_name=NAME)
    assert not result.ok and f"injected at {stage}" in result.error
    assert "rollback failed" not in result.error
    assert {"dir": _identity(dest), "data": _identity(home_data)} == ids
    assert _snapshot(home_data) == data_before
    assert _snapshot(dest, skip=_OWN) == old
    assert (dest / ".app_secret").read_bytes() == SECRET
    assert manager._read_installed(NAME).version == "1.0.0"
    assert [s for s in _siblings() if "-update-" in s] == []
    if stage != "put-back":
        assert "data" not in moved and ".app_secret" not in moved, "neither moved, even to fail"


# --------------------------------------------------------------------------- #
# 4. A hard kill at every step, then recovery and the sweep
# --------------------------------------------------------------------------- #


def _counting_steps(monkeypatch, kill_at: int | None) -> list[str]:
    """Count every rename and removal; raise :class:`_HardKill` at step *kill_at*."""
    steps: list[str] = []
    real_replace = os.replace
    real_remove = manager._remove_any_shape
    real_remove_in = manager._remove_in
    real_remove_tree = manager._remove_tree
    test_thread = threading.get_ident()

    def step(what: str) -> None:
        steps.append(what)
        if len(steps) == kill_at:
            raise _HardKill(what)

    def replace_step(src, dst, *a, **k):
        if threading.get_ident() == test_thread:  # another thread's rename is not a step
            step(f"replace {Path(src).name} -> {Path(dst).name}")
        return real_replace(src, dst, *a, **k)

    def remove_step(path):
        step(f"remove {Path(path).name}")
        return real_remove(path)

    def remove_in_step(pin, entry):
        step(f"remove {entry}")
        return real_remove_in(pin, entry)

    def remove_tree_step(pin):
        step(f"remove {pin.path.name}")
        return real_remove_tree(pin)

    monkeypatch.setattr(os, "replace", replace_step)
    monkeypatch.setattr(manager, "_remove_any_shape", remove_step)
    monkeypatch.setattr(manager, "_remove_in", remove_in_step)
    monkeypatch.setattr(manager, "_remove_tree", remove_tree_step)
    return steps


def _as_dead_owner() -> None:
    """Give this process's leftovers a pid no OS hands out, as a killed process's."""
    mine = f"{os.getpid()}-"
    for root in (manager.apps_dir(), manager._update_marker_dir(NAME)):
        if not root.is_dir():
            continue
        for leftover in root.iterdir():
            if mine in leftover.name:
                leftover.rename(
                    leftover.with_name(leftover.name.replace(mine, f"{UNALLOCATABLE_PID}-"))
                )


def _fresh_home(tmp_path: Path, monkeypatch, label: str) -> Path:
    home = tmp_path / label / "kirocrew-home"
    home.mkdir(parents=True)
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
    )
    return home


class _World:
    """One install, the snapshots a kill's outcome is compared with, and the update."""

    def __init__(self, tmp_path: Path, monkeypatch, label: str, *, held_aside: bool) -> None:
        _fresh_home(tmp_path, monkeypatch, label)
        self.tmp_path = tmp_path / label
        self.dest = _install(self.tmp_path)
        self.data = self.dest / "data"
        self.tmp_data = self.dest.parent / f".{NAME}-data-tmp"
        if held_aside:
            os.rename(self.data, self.tmp_data)
        self.data_id = _identity(self.tmp_data if held_aside else self.data)
        self.data_bytes = _snapshot(self.tmp_data if held_aside else self.data)
        self.old = _snapshot(self.dest, skip=_OWN)
        self.source = _source(self.tmp_path, "2.0.0")

    def update(self) -> None:
        manager.update_app(self.source, expected_name=NAME)

    def assert_whole(self, *, allow_new: bool = True) -> str:
        """The old tree with its record or the new one with its; ``data`` untouched."""
        record = manager._read_installed(NAME)
        assert record is not None, os.listdir(self.dest)
        assert record.version in (("1.0.0", "2.0.0") if allow_new else ("1.0.0",))
        entries = _snapshot(self.dest, skip=_OWN)
        if record.version == "1.0.0":
            assert entries == self.old
        else:
            assert entries == {
                "app.json": entries["app.json"],
                "only-2.0.0.txt": b"2.0.0",
                "ui": "<dir>",
                "ui/index.html": b"ui 2.0.0",
            }
            assert json.loads(entries["app.json"])["version"] == "2.0.0"
        held = [p for p in (self.data, self.tmp_data) if os.path.lexists(p)]
        assert len(held) == 1, held
        if record.version == "2.0.0":
            assert held == [self.data], "the new record is written after the data is back"
        assert _identity(held[0]) == self.data_id
        assert _snapshot(held[0]) == self.data_bytes
        assert (self.dest / ".app_secret").read_bytes() == SECRET
        return record.version


def _settle(monkeypatch) -> None:
    """What the next boot does: recover (twice, for idempotency), then sweep."""
    monkeypatch.setattr(manager_adopt, "pid_exists", lambda pid: pid != UNALLOCATABLE_PID)
    manager.recover_interrupted_adoption(NAME)
    manager.recover_interrupted_adoption(NAME)
    manager.sweep_completed_swap_leftovers(NAME)
    assert _siblings() == [] or _siblings() == [f".{NAME}-data-tmp"]


def _forward_step_count(tmp_path: Path, monkeypatch, held_aside: bool) -> int:
    with monkeypatch.context() as patch:
        world = _World(tmp_path, patch, "count", held_aside=held_aside)
        steps = _counting_steps(patch, None)
        world.update()
        assert world.assert_whole() == "2.0.0"
    return len(steps)


@pytest.mark.parametrize("held_aside", [False, True], ids=["data-in-place", "data-held-aside"])
def test_a_hard_kill_at_any_step_settles_whole(app_home, tmp_path, monkeypatch, held_aside):
    """``data-held-aside``: a crashed sibling left the data at ``.<name>-data-tmp``."""
    total = _forward_step_count(tmp_path, monkeypatch, held_aside)
    outcomes = set()
    for kill_at in range(1, total + 1):
        with monkeypatch.context() as patch:
            world = _World(tmp_path, patch, f"kill-{kill_at}", held_aside=held_aside)
            patch.setattr(manager_records._UpdateMarker, "__exit__", lambda *_a: None)
            with patch.context() as killing:
                steps = _counting_steps(killing, kill_at)
                with pytest.raises(_HardKill):
                    world.update()
            killed_at = steps[-1]
            _as_dead_owner()
            _settle(patch)
            outcomes.add((killed_at, world.assert_whole()))
            assert not manager._update_in_progress(NAME), killed_at
    assert {version for _step, version in outcomes} == {"1.0.0", "2.0.0"}


def _steps_then(monkeypatch, *, fail_at: int | None, kill_at: int) -> list[str]:
    """Count steps; ``OSError`` at *fail_at* (so the rollback runs), a hard kill at *kill_at*."""
    steps = _counting_steps(monkeypatch, kill_at)
    if fail_at is not None:
        counted = os.replace
        test_thread = threading.get_ident()

        def failing(src, dst, *a, **k):
            # Another thread's rename reaches ``counted``, which passes it through uncounted.
            if threading.get_ident() == test_thread and len(steps) + 1 == fail_at:
                steps.append("fail")
                raise OSError("injected")
            return counted(src, dst, *a, **k)

        monkeypatch.setattr(os, "replace", failing)
    return steps


def _restore_kills(tmp_path, monkeypatch, phase: str, kill_at: int) -> int:
    """Kill the restore after forward step *kill_at* at each of its steps; count the runs."""
    runs = 0
    for restore_kill in range(1, 64):
        with monkeypatch.context() as patch:
            world = _World(tmp_path, patch, f"{phase}-{kill_at}-{restore_kill}", held_aside=False)
            patch.setattr(manager_records._UpdateMarker, "__exit__", lambda *_a: None)
            with patch.context() as killing:
                if phase == "rollback":
                    steps = _steps_then(killing, fail_at=kill_at, kill_at=kill_at + restore_kill)
                    reached = kill_at + restore_kill
                else:
                    steps = _counting_steps(killing, kill_at)
                    reached = kill_at
                try:
                    world.update()
                except _HardKill:
                    pass
            _as_dead_owner()
            if phase == "recovery":
                if manager._read_installed(NAME) is not None:
                    return runs  # this kill left a record: no restore to interrupt
                patch.setattr(manager_adopt, "pid_exists", lambda pid: pid != UNALLOCATABLE_PID)
                with patch.context() as killing:
                    steps = _counting_steps(killing, restore_kill)
                    try:
                        manager.recover_interrupted_adoption(NAME)
                    except _HardKill:
                        pass
                reached = restore_kill
            elif manager._read_installed(NAME) is not None and (
                manager._read_installed(NAME).version == "2.0.0"
            ):
                return runs  # the injected fault was past the record: nothing rolled back
            _settle(patch)
            assert world.assert_whole(allow_new=False) == "1.0.0", (phase, kill_at, steps)
            runs += 1
            if len(steps) < reached:
                return runs  # the restore finished before this step
    raise AssertionError("the restore never finished")


@pytest.mark.parametrize("phase", ["recovery", "rollback"])
def test_a_hard_kill_during_a_restore_settles_on_the_old_tree(
    app_home, tmp_path, monkeypatch, phase
):
    """Every forward step, then a kill at every step of the restore that follows it."""
    total = _forward_step_count(tmp_path, monkeypatch, False)
    runs = [_restore_kills(tmp_path, monkeypatch, phase, at) for at in range(1, total + 1)]
    assert sum(runs) > total, runs


def _killed_during_move_out(tmp_path: Path, monkeypatch) -> _World:
    world = _World(tmp_path, monkeypatch, "s1", held_aside=False)
    monkeypatch.setattr(manager_records._UpdateMarker, "__exit__", lambda *_a: None)
    real_move = manager._move_entry

    def kill_after_the_record(src, entry, dst):
        if dst.path.name.startswith(f".{NAME}-update-out-") and entry == "app.json":
            raise _HardKill
        return real_move(src, entry, dst)

    with monkeypatch.context() as killing:
        killing.setattr(manager, "_move_entry", kill_after_the_record)
        with pytest.raises(_HardKill):
            world.update()
    _as_dead_owner()
    monkeypatch.setattr(manager_adopt, "pid_exists", lambda pid: pid != UNALLOCATABLE_PID)
    return world


def test_a_name_taken_in_the_app_directory_leaves_the_old_entries_untouched(
    app_home, tmp_path, monkeypatch, caplog
):
    world = _killed_during_move_out(tmp_path, monkeypatch)
    (out,) = manager._retired_trees(NAME, ("out",))
    held = _snapshot(out)
    assert set(held) == {"installed.json"}
    (world.dest / "installed.json").mkdir()
    with caplog.at_level(logging.WARNING, logger="kiro_crew.apps.manager"):
        assert manager.recover_interrupted_adoption(NAME) is False
    assert "recovery_ambiguous" in caplog.text
    assert _snapshot(out) == held
    assert (world.dest / "app.json").is_file(), "nothing more was touched"
    assert manager._update_in_progress(NAME)


def test_without_a_record_the_sweep_removes_no_tree_and_keeps_the_marker(
    app_home, tmp_path, monkeypatch
):
    world = _killed_during_move_out(tmp_path, monkeypatch)
    stem = f"{UNALLOCATABLE_PID}-ab12cd34"
    staging = manager.apps_dir() / f".{NAME}-update-new-{stem}"
    staging.mkdir()
    before = _siblings()
    assert manager.swap_leftover_names() == [NAME]
    assert manager.sweep_completed_swap_leftovers(NAME) is False
    assert _siblings() == before
    assert manager.recover_interrupted_adoption(NAME) is True
    assert world.assert_whole(allow_new=False) == "1.0.0"
    assert staging.is_dir(), "another stem's staging tree is the sweep's"
    assert manager.sweep_completed_swap_leftovers(NAME) is True
    assert _siblings() == []


def test_a_swap_of_the_whole_tree_is_recovered_as_before(app_home, tmp_path, monkeypatch):
    """A kill in a swap that moved the tree and set ``data/`` aside: today's branch."""
    dest = _install(tmp_path)
    old = _snapshot(dest, skip=_OWN)
    data_id = _identity(dest / "data")
    stem = f"{UNALLOCATABLE_PID}-deadbeef"
    os.rename(dest / "data", dest.parent / f".{NAME}-data-tmp")
    os.rename(dest / ".app_secret", dest.parent / f".{NAME}-secret-tmp")
    os.rename(dest, dest.parent / f".{NAME}-update-old-{stem}")
    dest.mkdir()
    (dest / "app.json").write_text("{}", encoding="utf-8")
    marker = manager._update_marker_dir(NAME)
    marker.mkdir()
    (marker / stem).touch()
    assert manager.recover_interrupted_adoption(NAME) is True
    assert _snapshot(dest, skip=_OWN) == old
    assert _identity(dest / "data") == data_id
    assert (dest / ".app_secret").read_bytes() == SECRET
    assert manager._read_installed(NAME).version == "1.0.0"
    assert _siblings() == []


# --------------------------------------------------------------------------- #
# 5. A link planted in apps/ while the update runs
# --------------------------------------------------------------------------- #

_needs_dir_fd = pytest.mark.skipif(
    not pinned_fs.supports_pinned_walk(), reason="needs dir_fd renames to refuse a mid-update swap"
)


class _SelSpy:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def log_api_access(self, **record) -> None:
        self.records.append(record)


def _decoy(tmp_path: Path, label: str) -> Path:
    """A directory outside ``apps/`` holding an app-shaped tree."""
    decoy = tmp_path / "outside" / label
    (decoy / "ui").mkdir(parents=True)
    (decoy / "app.json").write_text("decoy", encoding="utf-8")
    (decoy / "ui" / "index.html").write_text("decoy", encoding="utf-8")
    (decoy / "installed.json").write_text("{}", encoding="utf-8")
    return decoy


def _swap_for_link(path: Path, target: Path) -> Path:
    """Move the directory at *path* to a stash in ``apps/`` and plant a link to *target*."""
    stash = manager.apps_dir() / f"stash-{path.name}"
    os.rename(path, stash)
    os.symlink(target, path)
    return stash


def test_a_link_at_the_app_directory_refuses_the_update_before_anything_moves(
    app_home, tmp_path, monkeypatch
):
    dest = _install(tmp_path)
    real = tmp_path / "outside" / "real-app"
    real.parent.mkdir()
    os.rename(dest, real)
    make_dir_link(dest, real)
    before = _snapshot(real)
    spy = _SelSpy()
    monkeypatch.setattr(manager, "sel", lambda: spy)

    result = manager.update_app(_source(tmp_path, "2.0.0"), expected_name=NAME)

    assert not result.ok and "cannot be held" in result.error
    assert [r["operation"] for r in spy.records] == ["app_dir_pin"]
    assert platform_compat.is_link_or_junction(dest)
    assert _snapshot(real) == before
    assert json.loads((real / "installed.json").read_text())["version"] == "1.0.0"
    assert [s for s in _siblings() if "-update-" in s] == []


def _attack_while_moving_out(monkeypatch, attack) -> None:
    """Run *attack* once, at the first rename made while the swap's ``out`` tree exists."""
    real_replace = os.replace
    done: list[bool] = []
    test_thread = threading.get_ident()

    def replace(src, dst, *a, **k):
        mine = threading.get_ident() == test_thread  # only the update's own renames attack
        if mine and not done and any(manager.apps_dir().glob(f".{NAME}-update-out-*")):
            done.append(True)
            attack()
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", replace)


def _attack_after_out_to_old(monkeypatch, attack) -> None:
    """Run *attack* once, right after ``out`` is renamed to ``old``; the record write then fails."""
    real_replace = os.replace
    real_write = manager._write_installed

    def replace(src, dst, *a, **k):
        result = real_replace(src, dst, *a, **k)
        if Path(dst).name.startswith(f".{NAME}-update-old-"):
            attack()
        return result

    def failing_write(name, meta, **kwargs):
        if meta.version == "2.0.0":
            raise OSError("injected at record")
        return real_write(name, meta, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(manager, "_write_installed", failing_write)


def _only(pattern: str) -> Path:
    (found,) = manager.apps_dir().glob(pattern)
    return found


@_needs_dir_fd
@pytest.mark.parametrize("point", ["dest", "out", "new", "old"])
def test_a_directory_swapped_for_a_link_mid_update_is_never_followed(
    app_home, tmp_path, monkeypatch, point
):
    dest = _install(tmp_path)
    decoy = _decoy(tmp_path, point)
    outside = _snapshot(tmp_path / "outside")
    old = _snapshot(dest, skip=_OWN)
    data_id, data_bytes = _identity(dest / "data"), _snapshot(dest / "data")
    stashed: list[Path] = []

    def attack() -> None:
        target = {
            "dest": lambda: dest,
            "out": lambda: _only(f".{NAME}-update-out-*"),
            "new": lambda: _only(f".{NAME}-update-new-*"),
            "old": lambda: _only(f".{NAME}-update-old-*"),
        }[point]()
        stashed.append(_swap_for_link(target, decoy))

    if point == "old":
        _attack_after_out_to_old(monkeypatch, attack)
    else:
        _attack_while_moving_out(monkeypatch, attack)

    result = manager.update_app(_source(tmp_path, "2.0.0"), expected_name=NAME)

    assert stashed, "the attack ran"
    assert not result.ok
    assert _snapshot(tmp_path / "outside") == outside, "nothing outside apps/ changed"
    app = stashed[0] if point == "dest" else dest
    assert _snapshot(app, skip=_OWN) == old
    assert json.loads((app / "installed.json").read_text())["version"] == "1.0.0"
    assert (_identity(app / "data"), _snapshot(app / "data")) == (data_id, data_bytes)
    assert (app / ".app_secret").read_bytes() == SECRET
