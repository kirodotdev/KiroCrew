"""Which interpreter an App Kit app's own Python runs under.

A bare ``python3`` is resolved through ``PATH`` at spawn time, which need not
hold an interpreter under that name at all, and where it does need not hold one
new enough for the app's venv-installed dependencies. Both failures are silent at
the point they matter, so the resolution happens once rather than at each spawn
site — which is how the backend and the MCP registration drifted apart.
"""

from __future__ import annotations

import sys
from pathlib import Path

from kiro_crew.apps import python_runtime as pr


def _plant(root: Path, *parts: str) -> Path:
    p = root.joinpath(".venv", *parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("", encoding="utf-8")
    return p


class TestAppInterpreterResolution:
    def test_the_posix_venv_interpreter_wins(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pr.platform_compat, "IS_WINDOWS", False)
        expected = _plant(tmp_path, "bin", "python3")
        assert pr.resolve_app_python(tmp_path) == str(expected)

    def test_the_windows_venv_interpreter_wins(self, tmp_path, monkeypatch):
        # A venv exposes Scripts/python.exe there; resolving the POSIX layout
        # finds nothing and the app's dependencies are silently dropped.
        monkeypatch.setattr(pr.platform_compat, "IS_WINDOWS", True)
        expected = _plant(tmp_path, "Scripts", "python.exe")
        assert pr.resolve_app_python(tmp_path) == str(expected)

    def test_the_other_platforms_layout_is_not_accepted(self, tmp_path, monkeypatch):
        # The POSIX layout present while running as Windows must NOT resolve, or
        # the caller is handed a path that cannot execute. Branching on the
        # platform rather than probing both is what makes this hold.
        monkeypatch.setattr(pr.platform_compat, "IS_WINDOWS", True)
        _plant(tmp_path, "bin", "python3")
        assert pr.resolve_app_python(tmp_path) == sys.executable

    def test_no_venv_falls_back_to_the_gateway_interpreter(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pr.platform_compat, "IS_WINDOWS", False)
        assert pr.app_venv_python(tmp_path) is None
        assert pr.resolve_app_python(tmp_path) == sys.executable

    def test_a_directory_at_the_interpreter_path_is_not_a_venv(self, tmp_path, monkeypatch):
        # is_file(), not exists(): a directory there is not runnable, and
        # returning it would replace a PATH lookup with a guaranteed failure.
        monkeypatch.setattr(pr.platform_compat, "IS_WINDOWS", False)
        (tmp_path / ".venv" / "bin" / "python3").mkdir(parents=True)
        assert pr.resolve_app_python(tmp_path) == sys.executable

    def test_the_resolved_interpreter_is_never_a_bare_name(self, tmp_path, monkeypatch):
        # The whole point: whatever comes back must name a file, because the
        # caller writes it where something else will spawn it.
        monkeypatch.setattr(pr.platform_compat, "IS_WINDOWS", False)
        for planted in (True, False):
            if planted:
                _plant(tmp_path, "bin", "python3")
            resolved = pr.resolve_app_python(tmp_path)
            assert resolved not in pr.BARE_PYTHON_COMMANDS
            assert Path(resolved).is_absolute()

    def test_every_bare_launcher_is_recognised(self):
        # `py` is the Windows launcher and is looked up on PATH like the other
        # two, so it belongs to the same class.
        for name in ("python", "python3", "py"):
            assert pr.is_bare_python(name)

    def test_a_bare_name_is_recognised_case_and_space_insensitively(self):
        # A manifest is hand-written JSON: "Python3" spawns the same launcher on
        # a case-insensitive filesystem but would slip past an exact match.
        for name in ("Python3", " python3 ", "PY", "\tpython\n"):
            assert pr.is_bare_python(name), name

    def test_a_resolved_or_deliberate_command_is_not_bare(self):
        for name in ("python3.13", "/usr/bin/python3", "node", "docker", "", "pythonic"):
            assert not pr.is_bare_python(name), name
        assert not pr.is_bare_python(None)

    def test_the_backend_spawn_paths_use_the_shared_resolver(self):
        # Both app-Python branches must go through one policy; a second inline
        # copy is how the two spawn paths drifted apart to begin with.
        import kiro_crew.apps.backend as backend_mod

        source = Path(backend_mod.__file__).read_text(encoding="utf-8")
        assert 'root / ".venv" / "bin"' not in source, "backend re-hardcodes a venv layout"
        assert source.count("resolve_app_python(root)") == 2
