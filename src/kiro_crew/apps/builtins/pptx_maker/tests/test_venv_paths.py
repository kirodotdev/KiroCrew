"""Windows venv-layout tests for the PPTX Maker engine interpreter path.

Pinned because the POSIX-only literal these cover was a hard stop rather than a
degradation: with ``.venv/bin/python`` hardcoded, the readiness probe reported "no
venv" for a venv that had genuinely been built, and ``uv pip install --python`` was
handed a path that does not exist on Windows — so provisioning could never succeed.
"""

from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.pptx_maker.backend import paths, preview_tools, provision


def _make_windows_interpreter(root: Path) -> Path:
    interpreter = root / "mcp-local" / ".venv" / "Scripts" / "python.exe"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("", encoding="utf-8")
    return interpreter


@pytest.mark.parametrize(
    ("is_windows", "tail"),
    [(False, ("bin", "python")), (True, ("Scripts", "python.exe"))],
)
def test_venv_python_follows_the_platform_venv_layout(monkeypatch, tmp_path, is_windows, tail):
    """uv writes the interpreter under ``bin/`` on POSIX and ``Scripts/`` on Windows."""
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", is_windows)
    assert paths.venv_python(tmp_path) == tmp_path / "mcp-local" / ".venv" / Path(*tail)


def test_engine_python_delegates_to_the_single_authority(monkeypatch, tmp_path):
    """``engine_python`` must not re-derive the layout, or the two answers can drift."""
    monkeypatch.setattr(paths, "engine_root", lambda: tmp_path)
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    assert paths.engine_python() == paths.venv_python(tmp_path)


def test_venv_ready_sees_a_windows_venv(monkeypatch, tmp_path):
    """A genuinely built Windows venv must read as READY, not as absent.

    The empty-tree half is the other direction of the same bug: the probe has to
    still say "not provisioned" when no interpreter exists in this layout.
    """
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    assert provision._venv_ready(tmp_path) is False

    _make_windows_interpreter(tmp_path)
    assert provision._venv_ready(tmp_path) is True


def test_preview_launcher_resolves_the_windows_interpreter(monkeypatch, tmp_path):
    """The launcher's interpreter probe now comes from ``paths``, not a private list."""
    monkeypatch.setattr(paths, "engine_root", lambda: tmp_path)
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    assert preview_tools._engine_python() is None

    interpreter = _make_windows_interpreter(tmp_path)
    assert preview_tools._engine_python() == interpreter
