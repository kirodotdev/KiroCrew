"""Tests for kiro_crew.signal_cli.bridge_path (who chooses the spawned binary)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from kiro_crew.signal_cli import bridge_path as bp

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX executable bits")


def _executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture(autouse=True)
def _no_override(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.delenv(bp.ENV_OVERRIDE, raising=False)


def test_the_first_listed_executable_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    second = _executable(tmp_path / "b")
    monkeypatch.setattr(bp, "TRUSTED_BRIDGE_PATHS", (str(tmp_path / "missing"), str(second)))
    assert bp.resolve_bridge_path() == str(second)


def test_nothing_installed_is_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bp, "TRUSTED_BRIDGE_PATHS", (str(tmp_path / "missing"),))
    assert bp.resolve_bridge_path() == ""
    assert bp.ENV_OVERRIDE in bp.describe_lookup()


def test_a_non_executable_file_is_not_a_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plain = tmp_path / "plain"
    plain.write_text("x", encoding="utf-8")
    plain.chmod(0o644)
    monkeypatch.setattr(bp, "TRUSTED_BRIDGE_PATHS", (str(plain),))
    assert bp.resolve_bridge_path() == ""


def test_the_operator_override_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    listed = _executable(tmp_path / "listed")
    chosen = _executable(tmp_path / "chosen")
    monkeypatch.setattr(bp, "TRUSTED_BRIDGE_PATHS", (str(listed),))
    monkeypatch.setenv(bp.ENV_OVERRIDE, str(chosen))
    assert bp.resolve_bridge_path() == str(chosen)


@pytest.mark.parametrize("value", ["signal-cli", "relative/signal-cli", "/nonexistent/signal-cli"])
def test_an_unusable_override_is_not_replaced_by_a_listed_binary(
    value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listed = _executable(tmp_path / "listed")
    monkeypatch.setattr(bp, "TRUSTED_BRIDGE_PATHS", (str(listed),))
    monkeypatch.setenv(bp.ENV_OVERRIDE, value)
    assert bp.resolve_bridge_path() == ""
    assert "is not an absolute path" in bp.describe_lookup()


def test_path_is_never_consulted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _executable(tmp_path / "signal-cli")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(bp, "TRUSTED_BRIDGE_PATHS", ())
    assert bp.resolve_bridge_path() == ""


def test_the_list_is_absolute() -> None:
    assert bp.TRUSTED_BRIDGE_PATHS
    assert all(p.startswith("/") and p.endswith("/signal-cli") for p in bp.TRUSTED_BRIDGE_PATHS)
