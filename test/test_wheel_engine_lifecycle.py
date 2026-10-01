"""What the shadow-venv engine leaves on disk, and what a restart loads from it.

``test_wheel_engine.py`` pins the engine's refusals. This module pins its life
cycle against real directories, and where it matters real interpreters and real
sockets: which tree a respawned gateway loads its code from after the next
promotion, what an interrupted build or removal leaves for the next attempt,
which trees a sweep or a prune may delete, how long a drip-feeding origin can
hold a download, and that a build child gets the installer path's environment.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import signal
import socket
import ssl
import subprocess
import sys
import textwrap
import threading
import time
import zipfile
from pathlib import Path
from typing import Iterator

import pytest

from kiro_crew.platform import tree_liveness, wheel_engine
from kiro_crew.platform.wheel_engine import ManagedVenvLayout, WheelUpdateError
from kiro_crew.platform_compat import IS_POSIX, trusted_system_bin, try_acquire_lock

pytestmark = pytest.mark.skipif(not IS_POSIX, reason="the shadow-venv engine is POSIX-only")

_FEED_BASE = "https://updates.crew.kiro.dev"
_ARTIFACT_BASE = "https://download.crew.kiro.dev"


def _layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ManagedVenvLayout:
    legacy = tmp_path / "crew-venv"
    (legacy / "bin").mkdir(parents=True)
    layout = ManagedVenvLayout(legacy=legacy, stable_link=tmp_path / "crew-venv-current")
    monkeypatch.setattr(wheel_engine, "managed_venv_layout", lambda: layout)
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(wheel_engine, "_staging_dir", lambda: staging)
    # The process under test runs from none of these trees unless a test says so.
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "elsewhere"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    return layout


def _claim(layout: ManagedVenvLayout, tree: Path, *, sentinel: bool = True) -> None:
    """What the real build writes first."""
    tree.mkdir(mode=0o700, parents=True)
    (tree / tree_liveness.TREE_MARKER).write_text(f"{layout.legacy}\n", encoding="utf-8")
    (tree / tree_liveness.LIVENESS_LOCK).touch()
    if sentinel:
        (tree / wheel_engine._SHADOW_SENTINEL).write_text("", encoding="utf-8")


def _install_locking_release(tree: Path) -> None:
    """The file that tells the prune this tree's release holds its liveness lock."""
    platform_pkg = tree / "lib" / "python3.12" / "site-packages" / "kiro_crew" / "platform"
    platform_pkg.mkdir(parents=True, exist_ok=True)
    (platform_pkg / "tree_liveness.py").write_text("", encoding="utf-8")


def _completed_tree(
    layout: ManagedVenvLayout, version: str, *, installed_at: float, locking: bool = True
) -> Path:
    tree = layout.versioned_tree(version)
    _claim(layout, tree, sentinel=False)
    if locking:
        _install_locking_release(tree)
    (tree / "bin").mkdir()
    for name in ("kirocrew", "python3"):
        path = tree / "bin" / name
        path.write_text("", encoding="utf-8")
        path.chmod(0o755)
        os.utime(path, (installed_at, installed_at))
    return tree


def _versioned(layout: ManagedVenvLayout) -> list[str]:
    return sorted(
        p.name
        for p in layout.legacy.parent.iterdir()
        if p.name.startswith(f"{layout.legacy.name}-")
        and p != layout.stable_link
        and p.is_dir()
        and not p.is_symlink()
    )


class TestRespawnLoadsItsOwnTree:
    """A gateway respawned after a promotion must keep loading the tree it started on."""

    @staticmethod
    def _tree(layout: ManagedVenvLayout, version: str) -> Path:
        tree = layout.versioned_tree(version)
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(tree)],
            check=True,
            capture_output=True,
            cwd=str(tree.parent),
            timeout=120,
        )
        site = next(tree.glob("lib/python*/site-packages"))
        pkg = site / "tornpkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text(f"VERSION = {version!r}\n", encoding="utf-8")
        (pkg / "lazy.py").write_text(f"VERSION = {version!r}\n", encoding="utf-8")
        dist = site / f"tornpkg-{version}.dist-info"
        dist.mkdir()
        (dist / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: tornpkg\nVersion: {version}\n", encoding="utf-8"
        )
        (tree / "bin" / "kirocrew").write_text("", encoding="utf-8")
        (tree / tree_liveness.TREE_MARKER).write_text(f"{layout.legacy}\n", encoding="utf-8")
        return tree

    def test_the_next_promotion_does_not_reach_a_running_gateway(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        layout = _layout(tmp_path, monkeypatch)
        first, second = self._tree(layout, "1.0"), self._tree(layout, "2.0")
        monkeypatch.setattr(wheel_engine, "_respawn_tree_is_managed", lambda _layout: True)
        wheel_engine.promote(first, layout.stable_link)

        exe = wheel_engine.respawn_executable()
        assert Path(exe).parent.parent == first.resolve(), "the restart names the resolved tree"

        # The successor imports eagerly, then waits while the NEXT update flips
        # the link, then does what a running gateway does later: a lazy import,
        # a metadata read, and a child spawned through sys.executable.
        probe = textwrap.dedent("""
            import importlib.metadata, json, subprocess, sys
            import tornpkg
            print("ready", flush=True)
            sys.stdin.readline()
            import tornpkg.lazy
            child = subprocess.run(
                [sys.executable, "-c", "import tornpkg; print(tornpkg.VERSION)"],
                capture_output=True, text=True, encoding="utf-8", timeout=60,
            )
            print(json.dumps({
                "lazy": tornpkg.lazy.VERSION,
                "metadata": importlib.metadata.version("tornpkg"),
                "child": child.stdout.strip(),
            }))
            """)
        proc = subprocess.Popen(
            [exe, "-I", "-c", probe],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(tmp_path),
            start_new_session=True,
        )
        try:
            ready: list[str] = []
            reader = threading.Thread(
                target=lambda: ready.append(proc.stdout.readline() if proc.stdout else ""),
                daemon=True,
            )
            reader.start()
            reader.join(timeout=60)
            assert ready and ready[0].strip() == "ready", "the probe never got ready"
            wheel_engine.promote(second, layout.stable_link)
            out, _err = proc.communicate("go\n", timeout=60)
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=30)
        seen = json.loads(out.strip().splitlines()[-1])
        assert seen == {"lazy": "1.0", "metadata": "1.0", "child": "1.0"}


class TestAGatewayStartedThroughTheLinkMovesOffItFirst:
    """An earlier version restarted gateways as ``crew-venv-current/bin/python3``.

    Such a process loads every later import through the link, so the unattended
    apply restarts it onto the link's resolved tree before it builds anything.
    """

    def test_an_interpreter_started_through_the_link_is_recognised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.platform import wheel_apply

        layout = _layout(tmp_path, monkeypatch)
        tree = TestRespawnLoadsItsOwnTree._tree(layout, "1.0")
        wheel_engine.promote(tree, layout.stable_link)
        # What CPython itself reports for an interpreter started through the link.
        prefix = subprocess.run(
            [
                str(layout.stable_link / "bin" / "python3"),
                "-I",
                "-c",
                "import sys; print(sys.prefix)",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        ).stdout.strip()
        if layout.stable_link.name not in Path(prefix).parts:
            # This CPython (macOS) resolves the link into sys.prefix itself, so
            # nothing it imports later goes through the link.
            assert not wheel_engine.runs_through_stable_link(prefix), prefix
            pytest.skip("this interpreter resolves the stable link in sys.prefix")
        assert wheel_engine.runs_through_stable_link(prefix), prefix
        assert not wheel_engine.runs_through_stable_link(str(tree))

        monkeypatch.setattr(sys, "prefix", prefix)
        monkeypatch.setattr(sys, "executable", str(layout.stable_link / "bin" / "python3"))
        assert wheel_apply.relaunch_before_apply()
        monkeypatch.setattr(sys, "prefix", str(tree))
        assert not wheel_apply.relaunch_before_apply()

    def test_a_symlinked_parent_still_matches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        layout = _layout(tmp_path, monkeypatch)
        tree = _completed_tree(layout, "1.0", installed_at=time.time())
        layout.stable_link.symlink_to(tree)
        alias = tmp_path.parent / f"{tmp_path.name}-alias"
        alias.symlink_to(tmp_path)
        try:
            assert wheel_engine.runs_through_stable_link(str(alias / layout.stable_link.name))
            assert not wheel_engine.runs_through_stable_link(str(alias / tree.name))
        finally:
            alias.unlink()

    def test_no_relaunch_when_the_restart_would_land_on_the_link_again(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A respawn that falls back to ``sys.executable`` would repeat every cycle."""
        from kiro_crew.platform import wheel_apply

        layout = _layout(tmp_path, monkeypatch)
        layout.stable_link.symlink_to(_completed_tree(layout, "1.0", installed_at=time.time()))
        through = str(layout.stable_link / "bin" / "python3")
        monkeypatch.setattr(sys, "prefix", str(layout.stable_link))
        monkeypatch.setattr(wheel_engine, "respawn_executable", lambda: through)
        assert not wheel_apply.relaunch_before_apply()


class TestInterruptedWorkIsClearedNextTime:
    def test_an_interrupted_removal_leaves_a_tombstone_not_a_refused_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The tree is renamed before it is deleted, so its name is free at once.

        Built in the engine's own order (the marker first), so a plain ``rmtree``
        would unlink the marker first and leave a tombstone no sweep recognises.
        """
        layout = _layout(tmp_path, monkeypatch)
        debris = layout.versioned_tree("9.9.9")
        _claim(layout, debris, sentinel=False)
        for sub in ("bin", "include", "lib"):
            (debris / sub).mkdir()
            (debris / sub / "f").write_text("", encoding="utf-8")
        (debris / "pyvenv.cfg").write_text("", encoding="utf-8")

        real_rmtree = wheel_engine.shutil.rmtree

        def interrupted(path: object, *a: object, **k: object) -> None:
            raise KeyboardInterrupt  # the process dies mid-removal

        monkeypatch.setattr(wheel_engine.shutil, "rmtree", interrupted)
        with pytest.raises(KeyboardInterrupt):
            wheel_engine._discard_tree(debris)
        monkeypatch.setattr(wheel_engine.shutil, "rmtree", real_rmtree)

        assert not debris.exists(), "the versioned name is free once the rename ran"
        (tombstone,) = [p for p in tmp_path.iterdir() if ".deleting-" in p.name]
        assert (tombstone / tree_liveness.TREE_MARKER).is_file(), "the proof goes last"
        wheel_engine._sweep_layout_debris(layout)
        assert not tombstone.exists()

    def test_a_removal_interrupted_at_any_unlink_stays_sweepable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Wherever the removal stops, what is left is still provably ours."""
        layout = _layout(tmp_path, monkeypatch)
        real_unlink = os.unlink
        stop_after = 0
        while True:
            debris = layout.versioned_tree("9.9.9")
            _claim(layout, debris, sentinel=False)
            for sub in ("bin", "lib"):
                (debris / sub).mkdir()
                (debris / sub / "f").write_text("", encoding="utf-8")
            calls: list[object] = []

            def counting(path: object, *args: object, **kwargs: object) -> None:
                if len(calls) == stop_after:
                    raise KeyboardInterrupt  # the process dies here
                calls.append(path)
                real_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

            monkeypatch.setattr(os, "unlink", counting)
            try:
                wheel_engine._discard_tree(debris)
                finished = True
            except KeyboardInterrupt:
                finished = False
            finally:
                monkeypatch.setattr(os, "unlink", real_unlink)
            left = [p for p in tmp_path.iterdir() if ".deleting-" in p.name]
            if finished:
                assert left == []
                break
            for tombstone in left:
                assert wheel_engine._is_owned_tree(
                    layout, tombstone
                ), f"unrecognisable after {stop_after} unlinks: {list(tombstone.iterdir())}"
            wheel_engine._sweep_layout_debris(layout)
            assert [p for p in tmp_path.iterdir() if ".deleting-" in p.name] == []
            stop_after += 1
        assert stop_after > 4, "every entry's unlink was an interruption point"

    def test_a_sentinel_only_directory_is_ours_and_is_cleared(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A build stopped before ``python -m venv`` wrote pyvenv.cfg is still ours."""
        layout = _layout(tmp_path, monkeypatch)
        shadow = layout.versioned_tree("9.9.9")
        shadow.mkdir()
        (shadow / wheel_engine._SHADOW_SENTINEL).write_text("", encoding="utf-8")
        steps: list[str] = []
        monkeypatch.setattr(
            wheel_engine, "_run", lambda argv, timeout, step, ctx=None: steps.append(step)
        )
        wheel_engine.build_shadow_venv(tmp_path / "w.whl", shadow)
        assert steps[0] == "venv creation"
        marker = (shadow / tree_liveness.TREE_MARKER).read_text(encoding="utf-8").strip()
        assert marker == str(layout.legacy)

    @staticmethod
    def _interrupted_prelock_install(layout: ManagedVenvLayout) -> Path:
        """A pre-lock release installed and promoted, killed before the sentinel came off."""
        tree = layout.versioned_tree("0.1.0")
        _claim(layout, tree)
        package = tree / "lib" / "python3.12" / "site-packages" / "kiro_crew"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        return tree

    def test_the_sweep_keeps_an_installed_prelock_tree_a_sentinel_still_marks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A leftover sentinel does not prove a tree never ran."""
        layout = _layout(tmp_path, monkeypatch)
        tree = self._interrupted_prelock_install(layout)
        wheel_engine._sweep_layout_debris(layout)
        assert tree.exists()
        _install_locking_release(tree)
        wheel_engine._sweep_layout_debris(layout)
        assert not tree.exists(), "an interrupted locking release is still debris"

    def test_a_rebuild_refuses_an_installed_prelock_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        layout = _layout(tmp_path, monkeypatch)
        tree = self._interrupted_prelock_install(layout)
        monkeypatch.setattr(wheel_engine, "_run", lambda *_a, **_kw: None)
        with pytest.raises(WheelUpdateError, match="a running kirocrew process uses it"):
            wheel_engine.build_shadow_venv(tmp_path / "w.whl", tree)
        assert (tree / "lib" / "python3.12" / "site-packages" / "kiro_crew").exists()

    def test_a_set_cancel_leaves_no_new_directory(self, tmp_path: Path) -> None:
        cancel = wheel_engine.ApplyCancel()
        cancel.set()
        shadow = tmp_path / "crew-venv-9.9.9"
        with pytest.raises(wheel_engine.WheelUpdateCancelled):
            wheel_engine.build_shadow_venv(
                tmp_path / "w.whl", shadow, ctx=wheel_engine._BuildContext(cancel=cancel)
            )
        assert not shadow.exists()

    def test_a_cancel_during_the_debris_removal_leaves_no_new_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cancel that lands while a large leftover is removed claims nothing new."""
        shadow = tmp_path / "crew-venv-9.9.9"
        shadow.mkdir()
        (shadow / wheel_engine._SHADOW_SENTINEL).write_text("", encoding="utf-8")
        (shadow / "lib").mkdir()
        cancel = wheel_engine.ApplyCancel()
        real_rmtree = wheel_engine.shutil.rmtree

        def slow_rmtree(path: object, *a: object, **k: object) -> None:
            cancel.set()  # the shutdown arrives mid-removal
            real_rmtree(path, *a, **k)  # type: ignore[arg-type]

        monkeypatch.setattr(wheel_engine.shutil, "rmtree", slow_rmtree)
        with pytest.raises(wheel_engine.WheelUpdateCancelled):
            wheel_engine.build_shadow_venv(
                tmp_path / "w.whl", shadow, ctx=wheel_engine._BuildContext(cancel=cancel)
            )
        assert sorted(p.name for p in tmp_path.iterdir()) == []


class TestSweep:
    def test_the_sweep_removes_only_this_layouts_debris(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        layout = _layout(tmp_path, monkeypatch)
        live = _completed_tree(layout, "1.0", installed_at=time.time())
        layout.stable_link.symlink_to(live)
        debris = layout.versioned_tree("2.0")
        _claim(layout, debris)
        staging = wheel_engine._staging_dir()
        stale = staging / "kirocrew-update-old"
        stale.mkdir()
        old = time.time() - wheel_engine._STAGING_STALE_SECS - 60
        os.utime(stale, (old, old))
        fresh = staging / "kirocrew-update-new"
        fresh.mkdir()

        wheel_engine._sweep_layout_debris(layout)

        assert live.exists() and layout.legacy.exists()
        assert not debris.exists()
        assert not stale.exists() and fresh.exists(), "an apply in flight keeps its staging"

    def test_another_install_sharing_the_parent_is_never_touched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``KIROCREW_VENV=/srv/crew`` beside ``/srv/crew-beta`` and its trees."""
        legacy = tmp_path / "crew"
        legacy.mkdir()
        layout = ManagedVenvLayout(legacy=legacy, stable_link=tmp_path / "crew-current")
        monkeypatch.setattr(wheel_engine, "managed_venv_layout", lambda: layout)
        (tmp_path / "staging").mkdir()
        monkeypatch.setattr(wheel_engine, "_staging_dir", lambda: tmp_path / "staging")
        beta = ManagedVenvLayout(legacy=tmp_path / "crew-beta", stable_link=tmp_path / "x")
        beta.legacy.mkdir()
        beta_build = tmp_path / "crew-beta-2.1"
        _claim(beta, beta_build)
        beta_named_like_ours = tmp_path / "crew-beta2.1"
        _claim(beta, beta_named_like_ours)

        wheel_engine._sweep_layout_debris(layout)
        wheel_engine._prune_superseded_trees(layout)

        assert beta.legacy.exists() and beta_build.exists() and beta_named_like_ours.exists()

    def test_an_unreadable_sibling_is_skipped_not_fatal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        layout = _layout(tmp_path, monkeypatch)
        locked = layout.versioned_tree("3.0")
        locked.mkdir()
        locked.chmod(0)
        try:
            wheel_engine._sweep_layout_debris(layout)
            wheel_engine._prune_superseded_trees(layout)
        finally:
            locked.chmod(0o700)
        assert locked.exists()

    def test_a_held_sentinel_tree_survives_the_sweep(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A tree stranded with its sentinel that a gateway still runs from stays."""
        layout = _layout(tmp_path, monkeypatch)
        stranded = _completed_tree(layout, "1.0", installed_at=time.time() - 100)
        (stranded / wheel_engine._SHADOW_SENTINEL).write_text("", encoding="utf-8")
        current = _completed_tree(layout, "2.0", installed_at=time.time())
        layout.stable_link.symlink_to(current)
        holder = os.open(str(stranded / tree_liveness.LIVENESS_LOCK), os.O_RDWR)
        try:
            assert try_acquire_lock(holder, exclusive=False)
            wheel_engine._sweep_layout_debris(layout)
        finally:
            os.close(holder)
        assert stranded.exists()

    def test_a_crashed_writers_temporary_link_is_removed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        layout = _layout(tmp_path, monkeypatch)
        live = _completed_tree(layout, "1.0", installed_at=time.time())
        layout.stable_link.symlink_to(live)
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait(timeout=30)
        orphan = tmp_path / f"crew-venv-current.{dead.pid}.new"
        orphan.symlink_to(live)
        ours = tmp_path / f"crew-venv-current.{os.getpid()}.new"
        ours.symlink_to(live)

        wheel_engine._sweep_layout_debris(layout)

        assert not orphan.is_symlink(), "a temp link whose writer is gone is swept"
        assert ours.is_symlink(), "a live writer's temp link is left alone"
        assert layout.stable_link.is_symlink()


def _wire_apply(layout: ManagedVenvLayout, monkeypatch: pytest.MonkeyPatch) -> None:
    """The engine's network and build steps as stand-ins that write real trees."""
    clock = [time.time() - 100_000.0]

    def build(wheel: Path, shadow: Path, stable_link: Path | None = None, **_kw: object) -> None:
        _claim(layout, shadow)
        _install_locking_release(shadow)
        (shadow / "bin").mkdir()
        clock[0] += 100
        for name in ("kirocrew", "python3"):
            path = shadow / "bin" / name
            path.write_text("", encoding="utf-8")
            path.chmod(0o755)
            os.utime(path, (clock[0], clock[0]))

    monkeypatch.setattr(
        wheel_engine, "download_verified_wheel", lambda payload, dest, **_kw: dest / "w.whl"
    )
    monkeypatch.setattr(wheel_engine, "build_shadow_venv", build)
    monkeypatch.setattr(wheel_engine, "verify_shadow_venv", lambda *_a, **_kw: None)
    monkeypatch.setattr(wheel_engine, "repoint_launcher_symlink", lambda _layout: True)


def _apply(monkeypatch: pytest.MonkeyPatch, version: str) -> None:
    monkeypatch.setattr(
        wheel_engine,
        "fetch_verified_manifest",
        lambda **_kw: {"version": version, "sha256": "a" * 64, "python_requires": ""},
    )
    wheel_engine.apply_wheel_update(
        channel="stable",
        feed_base=_FEED_BASE,
        artifact_base=_ARTIFACT_BASE,
        expected_version=version,
    )


class TestPrune:
    def test_four_versions_prune_to_current_and_previous(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        layout = _layout(tmp_path, monkeypatch)
        now = time.time()
        trees = [
            _completed_tree(layout, f"{n}.0", installed_at=now - 1000 + n) for n in range(1, 5)
        ]
        layout.stable_link.symlink_to(trees[-1])

        wheel_engine._prune_superseded_trees(layout)

        assert [t.exists() for t in trees] == [False, False, True, True]
        assert layout.legacy.exists()

    def test_a_tree_a_running_process_holds_is_kept(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        layout = _layout(tmp_path, monkeypatch)
        now = time.time()
        trees = [
            _completed_tree(layout, f"{n}.0", installed_at=now - 1000 + n) for n in range(1, 5)
        ]
        layout.stable_link.symlink_to(trees[-1])
        # Another process's shared hold, as tree_liveness takes it at entry.
        holder = os.open(str(trees[0] / tree_liveness.LIVENESS_LOCK), os.O_RDWR)
        try:
            assert try_acquire_lock(holder, exclusive=False)
            wheel_engine._prune_superseded_trees(layout)
        finally:
            os.close(holder)
        assert [t.exists() for t in trees] == [True, False, True, True]

    def test_a_tree_the_persisted_launcher_names_is_kept(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        layout = _layout(tmp_path, monkeypatch)
        now = time.time()
        trees = [
            _completed_tree(layout, f"{n}.0", installed_at=now - 1000 + n) for n in range(1, 5)
        ]
        layout.stable_link.symlink_to(trees[-1])
        launcher = tmp_path / "home" / ".local" / "bin" / "kirocrew"
        launcher.parent.mkdir(parents=True)
        launcher.symlink_to(trees[0] / "bin" / "kirocrew")

        wheel_engine._prune_superseded_trees(layout)

        assert trees[0].exists()

    def test_the_liveness_hold_is_taken_once_at_process_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        layout = _layout(tmp_path, monkeypatch)
        tree = _completed_tree(layout, "1.0", installed_at=time.time())
        monkeypatch.setattr(sys, "prefix", str(tree))
        monkeypatch.setattr(tree_liveness, "_HELD_FD", None)
        tree_liveness.hold_running_tree_lock()
        held = tree_liveness._HELD_FD
        try:
            assert held is not None
            tree_liveness.hold_running_tree_lock()
            assert tree_liveness._HELD_FD == held, "taken once per process"
            probe = os.open(str(tree / tree_liveness.LIVENESS_LOCK), os.O_RDWR)
            try:
                assert not try_acquire_lock(probe, exclusive=True), "a prune may not take it"
            finally:
                os.close(probe)
        finally:
            if held is not None:
                os.close(held)

    def test_sequential_gateway_updates_keep_at_most_two_trees(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The documented flow: each update, then a restart onto the new tree."""
        layout = _layout(tmp_path, monkeypatch)
        _wire_apply(layout, monkeypatch)
        held: int | None = None
        try:
            for version in ("0.2.1", "0.2.2", "0.2.3", "0.2.4", "0.2.5"):
                _apply(monkeypatch, version)
                assert len(_versioned(layout)) <= 2, _versioned(layout)
                # The restart: the old hold dies with the exec, the new tree is held.
                if held is not None:
                    os.close(held)
                tree = Path(os.path.realpath(layout.stable_link))
                monkeypatch.setattr(sys, "prefix", str(tree))
                held = os.open(str(tree / tree_liveness.LIVENESS_LOCK), os.O_RDWR)
                assert try_acquire_lock(held, exclusive=False)
        finally:
            if held is not None:
                os.close(held)

    def test_a_tree_holding_a_release_without_the_liveness_lock_is_kept(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An approved downgrade can install a release that never takes the lock.

        Its gateway runs with the lock free, so a free lock proves nothing there.
        """
        layout = _layout(tmp_path, monkeypatch)
        now = time.time()
        old = _completed_tree(layout, "0.1.0", installed_at=now - 30, locking=False)
        _completed_tree(layout, "0.2.0", installed_at=now - 20)
        _completed_tree(layout, "0.3.0", installed_at=now - 10)
        wheel_engine.promote(_completed_tree(layout, "0.4.0", installed_at=now), layout.stable_link)
        wheel_engine._prune_superseded_trees(layout)
        assert old.exists(), "a pre-lock release's tree must never be pruned"
        assert not layout.versioned_tree("0.2.0").exists(), "a locking release is still pruned"

    def test_cli_only_updates_keep_at_most_two_trees(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No gateway ever runs from a new tree; nothing holds a liveness lock."""
        layout = _layout(tmp_path, monkeypatch)
        _wire_apply(layout, monkeypatch)
        for version in ("0.3.1", "0.3.2", "0.3.3", "0.3.4", "0.3.5"):
            _apply(monkeypatch, version)
            assert len(_versioned(layout)) <= 2, _versioned(layout)


class TestAKeptTreeIsRebuiltOnAMoveBack:
    """The prune keeps the previous tree; an apply of that version must not refuse it.

    A channel move back (insider rc4, then stable 0.4.1, then insider rc4 again)
    aims the engine at the tree the prune kept. The real build runs here, with
    only its children stubbed, so the guard that decides what may be replaced is
    the one under test.
    """

    @staticmethod
    def _wire(layout: ManagedVenvLayout, monkeypatch: pytest.MonkeyPatch, version: str) -> None:
        real_build = wheel_engine.build_shadow_venv
        _wire_apply(layout, monkeypatch)
        monkeypatch.setattr(wheel_engine, "build_shadow_venv", real_build)

        def run(argv: list[str], timeout: float, step: str, *, ctx: object = None) -> None:
            if step == "venv creation":
                bin_dir = Path(argv[-1]) / "bin"
                bin_dir.mkdir(exist_ok=True)
                for name in ("kirocrew", "python3"):
                    (bin_dir / name).write_text("", encoding="utf-8")
                    (bin_dir / name).chmod(0o755)

        monkeypatch.setattr(wheel_engine, "_run", run)
        monkeypatch.setattr(
            wheel_engine,
            "fetch_verified_manifest",
            lambda **_kw: {"version": version, "sha256": "a" * 64, "python_requires": ""},
        )

    @staticmethod
    def _move_back(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[ManagedVenvLayout, Path]:
        layout = _layout(tmp_path, monkeypatch)
        now = time.time()
        kept = _completed_tree(layout, "0.5.0rc4", installed_at=now - 200)
        (kept / "pyvenv.cfg").write_text("", encoding="utf-8")
        (kept / "stale-from-the-first-build").write_text("", encoding="utf-8")
        layout.stable_link.symlink_to(_completed_tree(layout, "0.4.1", installed_at=now - 100))
        TestAKeptTreeIsRebuiltOnAMoveBack._wire(layout, monkeypatch, "0.5.0rc4")
        return layout, kept

    def test_a_refused_prelock_tree_survives_the_failed_apply(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The apply's own failure cleanup must not delete what the rebuild refused."""
        layout = _layout(tmp_path, monkeypatch)
        tree = TestInterruptedWorkIsClearedNextTime._interrupted_prelock_install(layout)
        layout.stable_link.symlink_to(_completed_tree(layout, "0.4.1", installed_at=time.time()))
        self._wire(layout, monkeypatch, "0.1.0")
        with pytest.raises(WheelUpdateError, match="a running kirocrew process uses it"):
            _apply(monkeypatch, "0.1.0")
        assert (tree / "lib" / "python3.12" / "site-packages" / "kiro_crew").exists()

    def test_a_kept_previous_tree_is_rebuilt_and_promoted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        layout, kept = self._move_back(tmp_path, monkeypatch)

        _apply(monkeypatch, "0.5.0rc4")

        assert Path(os.path.realpath(layout.stable_link)) == Path(os.path.realpath(kept))
        assert not (kept / "stale-from-the-first-build").exists(), "rebuilt from the new wheel"
        assert not (kept / wheel_engine._SHADOW_SENTINEL).exists()
        assert [p for p in tmp_path.iterdir() if ".deleting-" in p.name] == []

    def test_a_kept_tree_a_running_process_holds_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        layout, kept = self._move_back(tmp_path, monkeypatch)
        holder = os.open(str(kept / tree_liveness.LIVENESS_LOCK), os.O_RDWR)
        try:
            assert try_acquire_lock(holder, exclusive=False)
            with pytest.raises(WheelUpdateError, match="running kirocrew process uses it"):
                _apply(monkeypatch, "0.5.0rc4")
        finally:
            os.close(holder)
        assert (kept / "stale-from-the-first-build").exists(), "a held tree is never touched"
        assert layout.stable_link.resolve() == layout.versioned_tree("0.4.1").resolve()

    @pytest.mark.parametrize("pin", ["running", "launcher"])
    def test_a_kept_tree_this_process_or_the_launcher_uses_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pin: str
    ) -> None:
        _unused, kept = self._move_back(tmp_path, monkeypatch)
        if pin == "running":
            monkeypatch.setattr(sys, "prefix", str(kept))
        else:
            launcher = tmp_path / "home" / ".local" / "bin" / "kirocrew"
            launcher.parent.mkdir(parents=True)
            launcher.symlink_to(kept / "bin" / "kirocrew")
        with pytest.raises(WheelUpdateError, match="running kirocrew process uses it"):
            _apply(monkeypatch, "0.5.0rc4")
        assert (kept / "stale-from-the-first-build").exists()

    def test_a_tree_another_install_built_is_still_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        layout, kept = self._move_back(tmp_path, monkeypatch)
        (kept / tree_liveness.TREE_MARKER).write_text("/srv/another-crew-venv\n", encoding="utf-8")
        with pytest.raises(WheelUpdateError, match="not built by this install"):
            _apply(monkeypatch, "0.5.0rc4")
        assert (kept / "stale-from-the-first-build").exists()


@pytest.fixture
def drip_server(tmp_path: Path) -> Iterator[str]:
    """A real HTTPS origin that sends a header, then one byte every 0.2 s for 5 s."""
    openssl = trusted_system_bin("openssl")
    if openssl is None:
        pytest.skip("openssl not available in a trusted system directory")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-subj",
            "/CN=127.0.0.1",
            "-days",
            "1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
        timeout=60,
        cwd=str(tmp_path),
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert), str(key))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    port = listener.getsockname()[1]
    stop = threading.Event()

    def handle(conn: socket.socket) -> None:
        try:
            tls = context.wrap_socket(conn, server_side=True)
            buf = b""
            while b"\r\n\r\n" not in buf:
                data = tls.recv(4096)
                if not data:
                    return
                buf += data
            tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\n\r\n")
            end = time.monotonic() + 5.0
            while time.monotonic() < end and not stop.is_set():
                tls.sendall(b"x")
                time.sleep(0.2)
        except OSError:
            pass
        finally:
            conn.close()

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    try:
        yield f"https://127.0.0.1:{port}/w.whl"
    finally:
        stop.set()
        listener.close()


@pytest.fixture
def trust_the_drip_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the certificate check is relaxed; the read path is the real one."""
    insecure = ssl.create_default_context()
    insecure.check_hostname = False
    insecure.verify_mode = ssl.CERT_NONE
    # No proxy: a runner's environment or (on macOS) its system proxy settings
    # would otherwise route the loopback request through a proxy that drops it.
    opener = wheel_engine.urllib.request.build_opener(
        wheel_engine.urllib.request.ProxyHandler({}),
        wheel_engine.urllib.request.HTTPSHandler(context=insecure),
    )
    monkeypatch.setattr(
        wheel_engine.urllib.request,
        "urlopen",
        lambda req, timeout: opener.open(req, timeout=timeout),
    )


@pytest.mark.skipif(
    sys.platform == "darwin",
    reason="the macOS runner closes the loopback TLS drip connection before any "
    "response (RemoteDisconnected); the bounds are platform-neutral and run on Linux",
)
class TestDownloadBounds:
    """Each bound fires at its own deadline, not when the origin stops sending."""

    def test_the_total_bound_holds_against_a_drip(
        self, tmp_path: Path, drip_server: str, trust_the_drip_server: None
    ) -> None:
        with pytest.raises(WheelUpdateError, match="did not finish within"):
            wheel_engine._download_to_file(
                drip_server, tmp_path / "w.whl", 10**9, 2, "a" * 64, total_secs=1.0
            )
        assert not (tmp_path / "w.whl").exists()

    def test_a_cancel_interrupts_a_download_between_drips(
        self, tmp_path: Path, drip_server: str, trust_the_drip_server: None
    ) -> None:
        cancel = wheel_engine.ApplyCancel()
        timer = threading.Timer(0.5, cancel.set)
        timer.start()
        try:
            with pytest.raises(wheel_engine.WheelUpdateCancelled):
                wheel_engine._download_to_file(
                    drip_server, tmp_path / "w.whl", 10**9, 2, "a" * 64, cancel=cancel
                )
        finally:
            timer.cancel()
        assert not (tmp_path / "w.whl").exists()


class TestPersistedLaunchPaths:
    def test_a_path_in_an_older_tree_is_persisted_through_the_link_on_a_newer_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A service installed from tree A after B was promoted must not name A.

        A is the tree the next prune removes, so ExecStart would then fail.
        """
        layout = _layout(tmp_path, monkeypatch)
        older = _completed_tree(layout, "1.0", installed_at=time.time() - 10)
        newer = _completed_tree(layout, "2.0", installed_at=time.time())
        wheel_engine.promote(newer, layout.stable_link)
        persisted = tree_liveness.through_stable_link(str(older / "bin" / "kirocrew"))
        assert persisted == str(layout.stable_link / "bin" / "kirocrew")

    def test_a_file_the_link_tree_lacks_or_a_dangling_link_is_left_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        layout = _layout(tmp_path, monkeypatch)
        older = _completed_tree(layout, "1.0", installed_at=time.time() - 10)
        newer = _completed_tree(layout, "2.0", installed_at=time.time())
        only_in_older = older / "bin" / "kirocrew-old-only"
        only_in_older.write_text("", encoding="utf-8")
        wheel_engine.promote(newer, layout.stable_link)
        assert tree_liveness.through_stable_link(str(only_in_older)) == str(only_in_older)

        layout.stable_link.unlink()
        layout.stable_link.symlink_to(tmp_path / "gone")
        target = str(older / "bin" / "kirocrew")
        assert tree_liveness.through_stable_link(target) == target

    def test_a_path_inside_the_live_tree_is_persisted_through_the_stable_link(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        layout = _layout(tmp_path, monkeypatch)
        tree = _completed_tree(layout, "1.0", installed_at=time.time())
        layout.stable_link.symlink_to(tree)
        persisted = tree_liveness.through_stable_link(str(tree / "bin" / "kirocrew"))
        assert persisted == str(layout.stable_link / "bin" / "kirocrew")

    def test_the_launchd_repairer_names_the_stable_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.service import macos

        layout = _layout(tmp_path, monkeypatch)
        first = _completed_tree(layout, "1.0", installed_at=time.time() - 10)
        layout.stable_link.symlink_to(first)
        monkeypatch.delenv("KIROCREW_SERVICE_BIN", raising=False)
        monkeypatch.setattr(sys, "executable", str(first / "bin" / "python3"))
        persisted = macos._repairer_bin()
        assert persisted == str(layout.stable_link / "bin" / "kirocrew")
        # Two promotions and a prune later the persisted path still resolves.
        for n, version in enumerate(("2.0", "3.0")):
            nxt = _completed_tree(layout, version, installed_at=time.time() + n)
            wheel_engine.promote(nxt, layout.stable_link)
        wheel_engine._prune_superseded_trees(layout)
        assert not first.exists()
        assert Path(persisted).resolve(strict=True)

    def test_the_mcp_command_stays_on_the_running_tree_after_a_promotion(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The servers a gateway starts run its own version until it restarts.

        Through the stable link they would exec a promoted tree's code against
        the not-yet-restarted gateway and its stores, for as long as the restart
        stays deferred.
        """
        from unittest.mock import MagicMock, patch

        import kiro_crew.agent as agent_mod

        layout = _layout(tmp_path, monkeypatch)
        first = _completed_tree(layout, "1.0", installed_at=time.time() - 10)
        (first / "pyvenv.cfg").write_text("", encoding="utf-8")
        package = first / "lib" / "python3.12" / "site-packages" / "kiro_crew"
        package.mkdir(parents=True, exist_ok=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        layout.stable_link.symlink_to(first)
        fake_package = MagicMock()
        fake_package.__file__ = str(package / "__init__.py")
        real_works = agent_mod._launcher_works
        monkeypatch.setattr(
            agent_mod,
            "_launcher_works",
            lambda path: str(path).startswith(str(tmp_path)) and real_works(path),
        )
        monkeypatch.setattr(agent_mod, "_KIROCREW_BIN", None)
        with patch.dict(sys.modules, {"kiro_crew": fake_package}):
            command, args = agent_mod._kirocrew_mcp_invocation("mcp-core")
        assert (command, args) == (str(first / "bin" / "kirocrew"), ["mcp-core"])

        wheel_engine.promote(
            _completed_tree(layout, "2.0", installed_at=time.time()), layout.stable_link
        )
        command, _args = agent_mod._kirocrew_mcp_invocation("mcp-core")
        assert Path(command).resolve().parent.parent == first.resolve()

    def test_the_path_shim_names_the_stable_link(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ``~/.local/bin`` shim outlives the process that writes it."""
        import kiro_crew.agent as agent_mod

        layout = _layout(tmp_path, monkeypatch)
        first = _completed_tree(layout, "1.0", installed_at=time.time())
        layout.stable_link.symlink_to(first)
        monkeypatch.setattr(
            agent_mod, "_resolve_kirocrew_bin", lambda: str(first / "bin" / "kirocrew")
        )
        monkeypatch.setattr(agent_mod, "_in_linked_git_worktree", lambda _path: False)
        monkeypatch.setattr(agent_mod, "_in_ephemeral_tree", lambda _path: False)
        monkeypatch.setattr(agent_mod.shutil, "which", lambda _name: None)
        bin_dir = tmp_path / "localbin"
        assert agent_mod.ensure_kirocrew_on_path(bin_dir=bin_dir) == str(bin_dir / "kirocrew")
        assert os.readlink(bin_dir / "kirocrew") == str(layout.stable_link / "bin" / "kirocrew")

    def test_a_path_outside_any_engine_tree_is_unchanged(self, tmp_path: Path) -> None:
        path = str(tmp_path / "bin" / "kirocrew")
        assert tree_liveness.through_stable_link(path) == path


def _wheel(path: Path, name: str, version: str, files: dict[str, str], metadata: str) -> Path:
    """Write a minimal, valid pure-Python wheel."""
    dist_info = f"{name}-{version}.dist-info"
    entries = dict(files)
    entries[f"{dist_info}/METADATA"] = (
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n{metadata}"
    )
    entries[f"{dist_info}/WHEEL"] = (
        "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    )
    record_lines = []
    for arcname, text in entries.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(text.encode()).digest()).rstrip(b"=")
        record_lines.append(f"{arcname},sha256={digest.decode()},{len(text.encode())}")
    record_lines.append(f"{dist_info}/RECORD,,")
    wheel = path / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for arcname, text in entries.items():
            archive.writestr(arcname, text)
        archive.writestr(f"{dist_info}/RECORD", "\n".join(record_lines) + "\n")
    return wheel


class TestBuildChildEnvironment:
    @pytest.mark.timeout(300)
    @pytest.mark.parametrize("trusted", [True, False])
    def test_an_inherited_pythonpath_cannot_satisfy_the_shadow_dependencies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trusted: bool
    ) -> None:
        """Offline, end to end, on both routes (the gateway's scrubbed environment,
        and ``kirocrew update``'s own shell): the dependency lands IN the tree, and
        a tree without it fails verification.

        The caller's environment carries a ``PYTHONPATH`` whose directory already
        has the dependency installed. A build child that honoured it would report
        the dependency satisfied and install none, and the tree would only fail at
        its next clean start. Interpreter children run ``-I``, so it never does.
        """
        links = tmp_path / "links"
        links.mkdir()
        _wheel(links, "depdummy", "1.0", {"depdummy.py": "VALUE = 1\n"}, "")
        kirocrew = _wheel(
            tmp_path,
            "kirocrew",
            "9.9.9",
            {
                "kiro_crew/__init__.py": "__version__ = '9.9.9'\n",
                "kiro_crew/cli.py": "import depdummy\n\ndef main():\n    pass\n",
                # The console script verification looks for comes from here.
                "kirocrew-9.9.9.dist-info/entry_points.txt": (
                    "[console_scripts]\nkirocrew = kiro_crew.cli:main\n"
                ),
            },
            "Requires-Dist: depdummy\n",
        )
        foreign = tmp_path / "foreign-site"
        foreign.mkdir()
        (foreign / "depdummy.py").write_text("VALUE = 0\n", encoding="utf-8")
        dist = foreign / "depdummy-1.0.dist-info"
        dist.mkdir()
        (dist / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: depdummy\nVersion: 1.0\n", encoding="utf-8"
        )
        monkeypatch.setenv("PYTHONPATH", str(foreign))
        monkeypatch.setenv("PIP_NO_INDEX", "1")
        monkeypatch.setenv("PIP_FIND_LINKS", str(links))
        monkeypatch.setenv("PIP_DISABLE_PIP_VERSION_CHECK", "1")
        ctx = wheel_engine._BuildContext(trusted_env=trusted)

        shadow = tmp_path / "crew-venv-9.9.9"
        wheel_engine.build_shadow_venv(kirocrew, shadow, ctx=ctx)

        site = next(shadow.glob("lib/python*/site-packages"))
        assert (site / "depdummy.py").read_text(encoding="utf-8") == "VALUE = 1\n"
        wheel_engine.verify_shadow_venv(shadow, "9.9.9", ctx=ctx)

        (site / "depdummy.py").unlink()
        for leftover in site.glob("depdummy-*.dist-info"):
            wheel_engine.shutil.rmtree(leftover)
        with pytest.raises(WheelUpdateError, match="unmet dependencies"):
            wheel_engine.verify_shadow_venv(shadow, "9.9.9", ctx=ctx)
