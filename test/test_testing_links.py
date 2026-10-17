"""Both arms of kiro_crew.testing.links.make_dir_link, on every platform."""

import sys
import types

import pytest

from kiro_crew import platform_compat
from kiro_crew.testing.links import make_dir_link


@pytest.mark.skipif(sys.platform == "win32", reason="a real symlink needs a privilege there")
def test_posix_arm_makes_a_directory_symlink(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    (tmp_path / "target").mkdir()
    make_dir_link(tmp_path / "link", tmp_path / "target")
    assert (tmp_path / "link").is_symlink()
    assert (tmp_path / "link").resolve() == (tmp_path / "target").resolve()


def test_windows_arm_always_calls_create_junction(tmp_path, monkeypatch):
    calls = []
    fake = types.SimpleNamespace(CreateJunction=lambda src, dst: calls.append((src, dst)))
    monkeypatch.setitem(sys.modules, "_winapi", fake)
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    make_dir_link(tmp_path / "link", tmp_path / "target")
    assert calls == [(str(tmp_path / "target"), str(tmp_path / "link"))]
