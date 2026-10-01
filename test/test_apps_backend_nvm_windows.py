"""The nvm resolver never shells out on Windows.

``_resolve_nvm_path`` in ``kiro_crew.apps.backend`` sources ``nvm.sh`` through
``bash -c`` to find an nvm-managed node. nvm (nvm-sh) is a POSIX-shell tool, and
on Windows the bare name ``bash`` resolves through ``CreateProcess`` to
``C:\\Windows\\System32\\bash.exe`` -- the WSL launcher -- which cannot source a
Windows path. With an ``nvm.sh`` on disk, every Node-backend app start therefore
detoured through WSL for up to the 10 s subprocess timeout (or opened WSL's
distribution-install prompt on a host with no distro) before falling through to
``node`` on PATH.

These tests simulate Windows by pointing ``platform_compat.IS_WINDOWS`` at True
on every host, the same seam ``test_app_backend_launcher_tree_drain.py`` uses,
and close the process boundary at ``backend.subprocess.run``. Nothing here
spawns a process. The POSIX half pins the existing behaviour so the guard cannot
silently widen into "never use nvm".
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import kiro_crew.apps.backend as bmod


def _nvm_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An ``NVM_DIR`` holding a real ``nvm.sh`` so the on-disk gate passes."""
    nvm = tmp_path / "nvm"
    nvm.mkdir()
    (nvm / "nvm.sh").write_text("# nvm\n", encoding="utf-8")
    monkeypatch.setenv("NVM_DIR", str(nvm))
    return nvm


def _spy_runs(
    monkeypatch: pytest.MonkeyPatch, *, stdout: str = "", returncode: int = 0
) -> list[list[str]]:
    """Record every ``subprocess.run`` argv the resolver issues."""
    calls: list[list[str]] = []

    def _run(argv: Any, **_kwargs: Any) -> Any:
        calls.append(list(argv))
        return SimpleNamespace(returncode=returncode, stdout=stdout)

    monkeypatch.setattr(bmod.subprocess, "run", _run)
    return calls


# ---------------------------------------------------------------------------
# Windows: the guard fires before the filesystem gate and before any spawn
# ---------------------------------------------------------------------------


class TestWindowsNeverSpawnsBash:
    @pytest.fixture(autouse=True)
    def _windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bmod.platform_compat, "IS_WINDOWS", True)

    @pytest.mark.parametrize("binary_name", ["node", "npm"])
    def test_nvm_sh_present_returns_none_without_a_subprocess(
        self, binary_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Red on base: the on-disk gate passes and the resolver runs
        # ``["bash", "-c", ...]`` -- the WSL launcher on a real Windows host.
        _nvm_dir(tmp_path, monkeypatch)
        calls = _spy_runs(monkeypatch, stdout=str(tmp_path / "node") + "\n")
        assert bmod._resolve_nvm_path(binary_name) is None
        assert calls == []

    def test_does_not_consult_the_filesystem(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The guard sits above the ``nvm.sh`` probe: an app start on Windows
        # pays nothing for this resolver, not even a stat.
        _nvm_dir(tmp_path, monkeypatch)
        _spy_runs(monkeypatch)
        monkeypatch.setattr(
            bmod.os.path,
            "isfile",
            lambda _p: pytest.fail("nvm.sh probed on Windows"),
        )
        assert bmod._resolve_nvm_path("node") is None

    def test_node_and_npm_fall_through_to_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The acceptance criterion: with ``nvm.sh`` present, ``node``/``npm``
        # come from PATH (nvm-windows shims, or a plain installer) and no
        # ``bash`` process is spawned along the way.
        _nvm_dir(tmp_path, monkeypatch)
        calls = _spy_runs(monkeypatch, stdout=str(tmp_path / "node") + "\n")
        monkeypatch.setattr(
            bmod.shutil, "which", lambda name: rf"C:\Program Files\nodejs\{name}.CMD"
        )
        assert bmod._find_node_binary() == r"C:\Program Files\nodejs\node.CMD"
        assert bmod._find_npm_binary() == r"C:\Program Files\nodejs\npm.CMD"
        assert calls == []

    def test_no_node_on_path_still_spawns_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # PATH empty: the callers report "not found" (the existing log line
        # upstream) instead of asking WSL.
        _nvm_dir(tmp_path, monkeypatch)
        calls = _spy_runs(monkeypatch)
        monkeypatch.setattr(bmod.shutil, "which", lambda _name: None)
        assert bmod._find_node_binary() is None
        assert bmod._find_npm_binary() is None
        assert calls == []


# ---------------------------------------------------------------------------
# POSIX: unchanged -- the nvm lookup still runs, with the same argv
# ---------------------------------------------------------------------------


class TestPosixStillUsesNvm:
    @pytest.fixture(autouse=True)
    def _posix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bmod.platform_compat, "IS_WINDOWS", False)

    def test_sources_nvm_sh_with_the_same_argv(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Kills the mutant that returns None unconditionally (or keys the guard
        # off the wrong flag): on POSIX the ``bash -c`` lookup must still fire,
        # exactly as before.
        nvm = _nvm_dir(tmp_path, monkeypatch)
        bin_dir = tmp_path / "versions" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "node").write_text("", encoding="utf-8")
        (bin_dir / "npm").write_text("", encoding="utf-8")
        calls = _spy_runs(monkeypatch, stdout=f"{bin_dir / 'node'}\n")
        assert bmod._resolve_nvm_path("npm") == str(bin_dir / "npm")
        assert calls == [
            ["bash", "-c", f'source "{nvm / "nvm.sh"}" --no-use && nvm which current'],
        ]

    def test_missing_nvm_sh_still_returns_none_without_a_subprocess(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NVM_DIR", str(tmp_path / "absent"))
        calls = _spy_runs(monkeypatch)
        assert bmod._resolve_nvm_path("node") is None
        assert calls == []

    def test_nvm_hit_still_wins_over_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _nvm_dir(tmp_path, monkeypatch)
        bin_dir = tmp_path / "versions" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "node").write_text("", encoding="utf-8")
        _spy_runs(monkeypatch, stdout=f"{bin_dir / 'node'}\n")
        monkeypatch.setattr(
            bmod.shutil, "which", lambda _n: pytest.fail("PATH consulted despite nvm hit")
        )
        assert bmod._find_node_binary() == str(bin_dir / "node")
