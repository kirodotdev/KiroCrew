"""Where the engine finds the running install and how it respawns into the next one.

The managed-venv layout and its detection, the pipx marker, the respawn executable and the
re-exec parameter, and the launcher repoint.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from kiro_crew.platform import wheel_engine
from kiro_crew.platform.wheel_engine import (
    ManagedVenvLayout,
    WheelUpdateError,
    managed_venv_layout,
    respawn_executable,
    running_from_managed_venv,
    running_from_pipx,
)
from kiro_crew.platform_compat import IS_POSIX

# The engine is POSIX-only by construction: cli.sh is a POSIX installer, and
# running_from_managed_venv() answers False on Windows, so no production path
# reaches this module there. The tests lean on POSIX semantics throughout —
# atomic rename over a symlink (WinError 5 on Windows), 0o600 mode bits, and
# venv bin/ layout — so they are skipped as a module rather than papered over
# with per-test shims that would test nothing real.
pytestmark = pytest.mark.skipif(not IS_POSIX, reason="the shadow-venv engine is POSIX-only")


class TestLayoutAndDetection:
    def test_layout_honours_venv_override(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("KIROCREW_VENV", str(tmp_path / "custom-venv"))
        layout = managed_venv_layout()
        assert layout.legacy == tmp_path / "custom-venv"
        assert layout.stable_link == tmp_path / "custom-venv-current"

    def test_layout_defaults_beside_data_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KIROCREW_VENV", raising=False)
        layout = managed_venv_layout()
        from kiro_crew.config.paths import data_home

        assert layout.legacy == Path(f"{str(data_home()).rstrip('/')}-venv")

    @pytest.mark.parametrize("version", ["9.9/9", "../9", "9.9.9\n", "", "x" * 129])
    def test_a_version_outside_the_release_grammar_names_no_tree(
        self, tmp_path: Path, version: str
    ) -> None:
        """The update paths read the version before the signed manifest, so a
        malformed one is the engine's own refusal, never pathlib's ``ValueError``."""
        layout = ManagedVenvLayout(
            legacy=tmp_path / "crew-venv", stable_link=tmp_path / "crew-venv-current"
        )
        with pytest.raises(WheelUpdateError, match="fails validation"):
            layout.versioned_tree(version)

    def test_a_release_version_names_its_sibling_tree(self, tmp_path: Path) -> None:
        layout = ManagedVenvLayout(
            legacy=tmp_path / "crew-venv", stable_link=tmp_path / "crew-venv-current"
        )
        assert layout.versioned_tree("1.2.3rc1+local") == tmp_path / "crew-venv-1.2.3rc1+local"

    def test_running_from_legacy_tree_detects_symlinked_interpreter(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        legacy = tmp_path / "crew-venv"
        (legacy / "bin").mkdir(parents=True)
        base_python = tmp_path / "base-python" / "python3"
        base_python.parent.mkdir()
        base_python.write_text("")
        exe = legacy / "bin" / "python3"
        exe.symlink_to(base_python)
        layout = ManagedVenvLayout(legacy=legacy, stable_link=tmp_path / "crew-venv-current")
        monkeypatch.setattr(sys, "executable", str(exe))
        assert running_from_managed_venv(layout) is True

    def test_running_from_versioned_tree_detects_symlinked_interpreter(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        tree = tmp_path / "crew-venv-1.2.3"
        (tree / "bin").mkdir(parents=True)
        base_python = tmp_path / "base-python" / "python3"
        base_python.parent.mkdir()
        base_python.write_text("")
        exe = tree / "bin" / "python3"
        exe.symlink_to(base_python)
        # Every real versioned install carries the console script; the
        # positive-identification rule keys on it.
        (tree / "bin" / "kirocrew").write_text("")
        layout = ManagedVenvLayout(
            legacy=tmp_path / "crew-venv", stable_link=tmp_path / "crew-venv-current"
        )
        monkeypatch.setattr(sys, "executable", str(exe))
        assert running_from_managed_venv(layout) is True

    def test_prefix_named_foreign_venv_not_detected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """crew-venv-dev with no engine artifacts is someone else's venv."""
        tree = tmp_path / "crew-venv-dev"
        (tree / "bin").mkdir(parents=True)
        exe = tree / "bin" / "python3"
        exe.write_text("")
        layout = ManagedVenvLayout(
            legacy=tmp_path / "crew-venv", stable_link=tmp_path / "crew-venv-current"
        )
        monkeypatch.setattr(sys, "executable", str(exe))
        assert running_from_managed_venv(layout) is False

    def test_foreign_interpreter_not_detected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        other = tmp_path / "somewhere-else" / "bin"
        other.mkdir(parents=True)
        exe = other / "python3"
        exe.write_text("")
        layout = ManagedVenvLayout(
            legacy=tmp_path / "crew-venv", stable_link=tmp_path / "crew-venv-current"
        )
        monkeypatch.setattr(sys, "executable", str(exe))
        assert running_from_managed_venv(layout) is False


class TestRunningFromPipx:
    """``running_from_pipx`` keys on the metadata file pipx keeps at the venv root."""

    def test_detects_a_pipx_venv_by_its_metadata_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        venv = tmp_path / "pipx-venvs" / "kirocrew"
        venv.mkdir(parents=True)
        (venv / "pipx_metadata.json").write_text("{}")
        monkeypatch.setattr(sys, "prefix", str(venv))
        assert running_from_pipx() is True

    def test_a_plain_venv_without_the_marker_is_not_pipx(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        venv = tmp_path / "my-venv"
        venv.mkdir()
        monkeypatch.setattr(sys, "prefix", str(venv))
        assert running_from_pipx() is False

    def test_a_directory_named_like_the_marker_is_not_a_match(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The marker must be a FILE — a directory of that name is not pipx's."""
        venv = tmp_path / "odd-venv"
        (venv / "pipx_metadata.json").mkdir(parents=True)
        monkeypatch.setattr(sys, "prefix", str(venv))
        assert running_from_pipx() is False


class TestRespawnExecutable:
    def test_non_managed_install_answers_sys_executable(self) -> None:
        # The test process runs from a dev venv, never a managed tree.
        assert respawn_executable() == sys.executable

    def test_managed_install_routes_through_stable_link(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        legacy = tmp_path / "crew-venv"
        (legacy / "bin").mkdir(parents=True)
        old_exe = legacy / "bin" / "python3"
        old_exe.write_text("")
        new_tree = tmp_path / "crew-venv-2.0.0"
        (new_tree / "bin").mkdir(parents=True)
        new_exe = new_tree / "bin" / "python3"
        new_exe.write_text("")
        new_exe.chmod(0o755)
        # A genuinely promoted tree always carries the console script; the
        # positive-identification rule keys on it.
        (new_tree / "bin" / "kirocrew").write_text("")
        stable = tmp_path / "crew-venv-current"
        stable.symlink_to(new_tree)

        monkeypatch.setenv("KIROCREW_VENV", str(legacy))
        monkeypatch.setattr(sys, "executable", str(old_exe))
        assert respawn_executable() == str(Path(os.path.realpath(stable)) / "bin" / "python3")

    def test_link_repointed_outside_managed_trees_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A stable link aimed outside the layout must never become the exec target."""
        legacy = tmp_path / "crew-venv"
        (legacy / "bin").mkdir(parents=True)
        exe = legacy / "bin" / "python3"
        exe.write_text("")
        outside = tmp_path / "not-ours"
        (outside / "bin").mkdir(parents=True)
        planted = outside / "bin" / "python3"
        planted.write_text("")
        planted.chmod(0o755)
        stable = tmp_path / "crew-venv-current"
        stable.symlink_to(outside)

        monkeypatch.setenv("KIROCREW_VENV", str(legacy))
        monkeypatch.setattr(sys, "executable", str(exe))
        assert respawn_executable() == str(
            exe
        ), "a link outside the managed trees must fall back to sys.executable"

    def test_missing_stable_link_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        legacy = tmp_path / "crew-venv"
        (legacy / "bin").mkdir(parents=True)
        exe = legacy / "bin" / "python3"
        exe.write_text("")
        monkeypatch.setenv("KIROCREW_VENV", str(legacy))
        monkeypatch.setattr(sys, "executable", str(exe))
        assert respawn_executable() == str(exe)

    def test_symlinked_interpreter_still_routes_through_stable_link(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A real venv's bin/python3 is a symlink to the base interpreter.

        Identity must come from the tree the link lives in, not from where it
        points: resolving the file lands outside every venv and would make
        every real managed install answer plain sys.executable.
        """
        base = tmp_path / "base-python" / "bin"
        base.mkdir(parents=True)
        base_exe = base / "python3.12"
        base_exe.write_text("")
        legacy = tmp_path / "crew-venv"
        (legacy / "bin").mkdir(parents=True)
        old_exe = legacy / "bin" / "python3"
        old_exe.symlink_to(base_exe)
        new_tree = tmp_path / "crew-venv-2.0.0"
        (new_tree / "bin").mkdir(parents=True)
        new_exe = new_tree / "bin" / "python3"
        new_exe.write_text("")
        new_exe.chmod(0o755)
        (new_tree / "bin" / "kirocrew").write_text("")
        stable = tmp_path / "crew-venv-current"
        stable.symlink_to(new_tree)

        monkeypatch.setenv("KIROCREW_VENV", str(legacy))
        monkeypatch.setattr(sys, "executable", str(old_exe))
        assert respawn_executable() == str(Path(os.path.realpath(stable)) / "bin" / "python3")

    def _nested_venv_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
        """A data home whose ``venv/`` is the retired in-data-home install."""
        home = tmp_path / "crew"
        nested = home / "venv"
        (nested / "bin").mkdir(parents=True)
        (nested / "bin" / "python3").write_text("")
        (nested / "bin" / "kirocrew").write_text("")
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        monkeypatch.delenv("KIROCREW_VENV", raising=False)
        monkeypatch.setattr(sys, "executable", str(nested / "bin" / "python3"))
        return home

    def test_retired_nested_venv_routes_through_stable_link(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The installer re-run cli.sh performs on migration: it builds the
        new tree BESIDE the data home, repoints the stable link at it, then
        ``rm -rf``s the in-data-home venv this process is running from. The
        restart must exec the stable link's interpreter — sys.executable no
        longer exists."""
        import shutil

        home = self._nested_venv_home(monkeypatch, tmp_path)
        new_tree = tmp_path / "crew-venv"
        (new_tree / "bin").mkdir(parents=True)
        new_exe = new_tree / "bin" / "python3"
        new_exe.write_text("")
        new_exe.chmod(0o755)
        (new_tree / "bin" / "kirocrew").write_text("")
        stable = tmp_path / "crew-venv-current"
        stable.symlink_to(new_tree)
        shutil.rmtree(home / "venv")

        assert not Path(sys.executable).exists()
        assert respawn_executable() == str(Path(os.path.realpath(stable)) / "bin" / "python3")

    def test_nested_venv_before_migration_keeps_sys_executable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """No stable link yet (the installer has not been re-run): the nested
        venv is the only interpreter, and the restart stays on it."""
        home = self._nested_venv_home(monkeypatch, tmp_path)
        assert respawn_executable() == str(home / "venv" / "bin" / "python3")

    def _migrated_legacy_tree(self, tmp_path: Path) -> Path:
        """The tree cli.sh's re-run builds beside the data home and verifies."""
        new_tree = tmp_path / "crew-venv"
        (new_tree / "bin").mkdir(parents=True)
        exe = new_tree / "bin" / "python3"
        exe.write_text("")
        exe.chmod(0o755)
        (new_tree / "bin" / "kirocrew").write_text("")
        return new_tree

    def test_unusable_stable_link_after_migration_uses_legacy_tree(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A real directory at the stable name makes cli.sh SKIP the repoint
        (its guard only writes a symlink or an absent path), but the nested-venv
        delete is gated on the new tree's own import check, so it still runs.
        The cached interpreter is then gone and the stable link cannot be
        trusted — the restart must take the import-verified legacy tree rather
        than exec a path that does not exist."""
        import shutil

        home = self._nested_venv_home(monkeypatch, tmp_path)
        new_tree = self._migrated_legacy_tree(tmp_path)
        (tmp_path / "crew-venv-current").mkdir()  # corrupt: a directory, not a link
        shutil.rmtree(home / "venv")

        assert not Path(sys.executable).exists()
        assert respawn_executable() == str(new_tree / "bin" / "python3")

    def test_failed_stable_link_repoint_after_migration_uses_legacy_tree(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """cli.sh's repoint failure is non-fatal (it warns and continues), so
        the migration can delete the nested venv leaving no stable link at
        all."""
        import shutil

        home = self._nested_venv_home(monkeypatch, tmp_path)
        new_tree = self._migrated_legacy_tree(tmp_path)
        shutil.rmtree(home / "venv")

        assert not (tmp_path / "crew-venv-current").exists()
        assert respawn_executable() == str(new_tree / "bin" / "python3")

    def test_deleted_interpreter_with_no_usable_legacy_tree_keeps_sys_executable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The fallback never invents an interpreter: with the legacy tree
        absent too there is nothing to validate, and the answer stays
        ``sys.executable`` exactly as before."""
        import shutil

        home = self._nested_venv_home(monkeypatch, tmp_path)
        nested_exe = home / "venv" / "bin" / "python3"
        shutil.rmtree(home / "venv")

        assert not (tmp_path / "crew-venv").exists()
        assert respawn_executable() == str(nested_exe)

    def test_live_interpreter_is_never_displaced_by_the_legacy_tree(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The legacy fallback is reached ONLY when the cached path is gone. A
        process whose own interpreter still exists keeps it, so a corrupt
        stable link cannot silently move a healthy gateway onto another
        tree."""
        home = self._nested_venv_home(monkeypatch, tmp_path)
        self._migrated_legacy_tree(tmp_path)
        (tmp_path / "crew-venv-current").mkdir()

        assert Path(sys.executable).exists()
        assert respawn_executable() == str(home / "venv" / "bin" / "python3")


class TestReexecExecutableParameter:
    def test_reexec_uses_supplied_executable(
        self,
        monkeypatch: pytest.MonkeyPatch,
        nonbundled_python_without_user_site,
    ) -> None:
        from kiro_crew import platform_compat

        # The real call mutates os.environ (UTF-8 pinning) before exec; with
        # execv mocked the process KEEPS RUNNING, so the mutation would leak
        # into every later test on this worker. The env step is not what these
        # tests assert, so it is stubbed rather than let loose.
        monkeypatch.setattr(platform_compat, "_ensure_utf8_process_environment", lambda: None)

        captured: dict[str, object] = {}

        def fake_execv(path: str, argv: list[str]) -> None:
            captured["path"] = path
            captured["argv"] = argv

        monkeypatch.setattr(os, "execv", fake_execv)
        platform_compat.reexec_python_module("kiro_crew", ["--flag"], executable="/x/bin/python3")
        assert captured["path"] == "/x/bin/python3"
        argv = captured["argv"]
        assert isinstance(argv, list)
        assert argv[1:5] == ["-s", "-P", "-m", "kiro_crew"]
        assert argv[5:] == ["--flag"]

    def test_reexec_defaults_to_sys_executable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew import platform_compat

        monkeypatch.setattr(platform_compat, "_ensure_utf8_process_environment", lambda: None)
        captured: dict[str, object] = {}
        monkeypatch.setattr(os, "execv", lambda p, a: captured.update(path=p))
        platform_compat.reexec_python_module("kiro_crew", [])
        assert captured["path"] == sys.executable


class TestLauncherRepoint:
    def _layout(self, tmp_path: Path) -> ManagedVenvLayout:
        legacy = tmp_path / "crew-venv"
        (legacy / "bin").mkdir(parents=True)
        (legacy / "bin" / "kirocrew").write_text("")
        return ManagedVenvLayout(legacy=legacy, stable_link=tmp_path / "crew-venv-current")

    def test_repoints_managed_launcher_through_stable_link(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        layout = self._layout(tmp_path)
        home = tmp_path / "home"
        (home / ".local" / "bin").mkdir(parents=True)
        launcher = home / ".local" / "bin" / "kirocrew"
        launcher.symlink_to(layout.legacy / "bin" / "kirocrew")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

        assert wheel_engine.repoint_launcher_symlink(layout) is True
        assert os.readlink(launcher) == str(layout.stable_link / "bin" / "kirocrew")

    def test_leaves_foreign_launcher_alone(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        layout = self._layout(tmp_path)
        home = tmp_path / "home"
        (home / ".local" / "bin").mkdir(parents=True)
        foreign = tmp_path / "pipx-venv" / "bin"
        foreign.mkdir(parents=True)
        (foreign / "kirocrew").write_text("")
        launcher = home / ".local" / "bin" / "kirocrew"
        launcher.symlink_to(foreign / "kirocrew")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

        assert wheel_engine.repoint_launcher_symlink(layout) is False
        assert os.readlink(launcher) == str(foreign / "kirocrew")

    def test_regular_file_launcher_untouched(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        layout = self._layout(tmp_path)
        home = tmp_path / "home"
        (home / ".local" / "bin").mkdir(parents=True)
        launcher = home / ".local" / "bin" / "kirocrew"
        launcher.write_text("#!/bin/sh\n")  # an operator's wrapper script
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

        assert wheel_engine.repoint_launcher_symlink(layout) is False
        assert launcher.is_file() and not launcher.is_symlink()
