"""A quoted or relative workspace path must never create a folder under the CWD."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.config import loader
from kiro_crew.config.loader import normalize_workspace_path, workspace_root


@pytest.mark.parametrize("raw", ['"~/x y"', "'~/x y'", '  "~/x y"  '])
def test_one_quote_pair_is_stripped_and_home_expanded(raw: str) -> None:
    assert normalize_workspace_path(raw) == Path.home() / "x y"


@pytest.mark.parametrize("raw", ['"abc', "'abc\"", '"'])
def test_unbalanced_quotes_are_left_alone(raw: str) -> None:
    assert normalize_workspace_path(raw) == Path(raw)


def test_whitespace_inside_a_path_is_kept() -> None:
    assert normalize_workspace_path("/ws/name ") == Path("/ws/name ")
    assert normalize_workspace_path('" /ws/name "') == Path(" /ws/name ")


@pytest.mark.skipif(sys.platform == "win32", reason="Windows maps ~name to a sibling profile")
def test_unknown_user_tilde_falls_back_instead_of_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(loader, "_default_workspace_base", lambda: tmp_path / "base")
    monkeypatch.setenv("KIROCREW_WORKSPACE", "~no-such-user-7701/ws")

    assert workspace_root() == (tmp_path / "base" / loader._WORKSPACE_DIR_NAME).resolve()


@pytest.mark.skipif(sys.platform != "win32", reason="drive paths are only absolute on Windows")
def test_quoted_windows_drive_path_stays_absolute() -> None:
    p = normalize_workspace_path('"C:\\Users\\me\\AI prompting"')
    assert p.is_absolute()
    assert p == Path("C:\\Users\\me\\AI prompting")


def test_saved_quoted_root_resolves_to_the_real_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KIROCREW_WORKSPACE", raising=False)
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "AI prompting" / "ws"
    saved = tmp_path / "workspace_dir"
    saved.write_text(f'"{target}"\n', encoding="utf-8")
    monkeypatch.setattr(loader, "_workspace_dir_file", lambda: saved)

    assert workspace_root() == target.resolve()
    assert not list(tmp_path.glob('"*'))


def test_relative_root_falls_back_to_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(loader, "_default_workspace_base", lambda: tmp_path / "base")
    monkeypatch.setenv("KIROCREW_WORKSPACE", "relative-ws")

    root = workspace_root()

    assert root == (tmp_path / "base" / loader._WORKSPACE_DIR_NAME).resolve()
    assert list(cwd.iterdir()) == []


def test_wizard_quoted_answer_writes_the_unquoted_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.cli_setup import _setup_workspace_dir

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "AI prompting"
    ws_file = tmp_path / "cfg" / "workspace_dir"
    monkeypatch.setattr("kiro_crew.cli_setup._workspace_dir_file", lambda: ws_file)
    with patch("builtins.input", return_value=f'"{target}"'):
        _setup_workspace_dir()

    assert target.is_dir()
    assert ws_file.read_text(encoding="utf-8").strip() == str(target)
    assert not list(tmp_path.glob('"*'))


def test_wizard_refuses_a_relative_answer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.cli_setup import _setup_workspace_dir

    monkeypatch.chdir(tmp_path)
    ws_file = tmp_path / "cfg" / "workspace_dir"
    monkeypatch.setattr("kiro_crew.cli_setup._workspace_dir_file", lambda: ws_file)
    with patch("builtins.input", return_value="relative-ws"):
        _setup_workspace_dir()

    assert not (tmp_path / "relative-ws").exists()
    assert not ws_file.exists()


def test_wizard_keeps_a_previously_saved_quoted_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.cli_setup import _setup_workspace_dir

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "AI prompting"
    ws_file = tmp_path / "workspace_dir"
    ws_file.write_text(f'"{target}"\n', encoding="utf-8")
    monkeypatch.setattr("kiro_crew.cli_setup._workspace_dir_file", lambda: ws_file)
    with patch("builtins.input", return_value=""):
        _setup_workspace_dir()

    assert target.is_dir()
    assert ws_file.read_text(encoding="utf-8").strip() == str(target)
