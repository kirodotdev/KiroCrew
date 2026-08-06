"""Tests for the ``kiro_crew._bootstrap`` console-entry self-heal.

The bootstrap closes the git-pull gap for editable installs: a commit that
adds a runtime dependency must not leave ``kirocrew`` dying on a raw
``ModuleNotFoundError`` when one ``pip install -e .`` fixes it. These tests
stub the import and the pip spawn — no real installs, no network.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kiro_crew import _bootstrap

# ── Helpers ──


def _fail_then_succeed(calls: list[str], sentinel):
    """Import stub failing with ModuleNotFoundError once, then succeeding."""

    def _import():
        calls.append("import")
        if len([c for c in calls if c == "import"]) == 1:
            raise ModuleNotFoundError("No module named 'defusedxml'", name="defusedxml")
        return sentinel

    return _import


# ── main() flow ──


def test_happy_path_never_spawns_pip(monkeypatch):
    ran: list[str] = []
    monkeypatch.setattr(_bootstrap, "_import_cli", lambda: lambda: ran.append("cli"))
    monkeypatch.setattr(
        _bootstrap, "_self_heal", lambda missing: pytest.fail("heal must not run")
    )
    _bootstrap.main()
    assert ran == ["cli"]


def test_missing_dep_heals_once_and_retries(monkeypatch):
    calls: list[str] = []
    ran: list[str] = []
    monkeypatch.setattr(
        _bootstrap, "_import_cli", _fail_then_succeed(calls, lambda: ran.append("cli"))
    )
    monkeypatch.setattr(
        _bootstrap, "_self_heal", lambda missing: calls.append(f"heal:{missing}") or True
    )
    _bootstrap.main()
    assert calls == ["import", "heal:defusedxml", "import"]
    assert ran == ["cli"]


def test_heal_failure_exits_with_guidance(monkeypatch, capsys):
    def _always_fail():
        raise ModuleNotFoundError("No module named 'defusedxml'", name="defusedxml")

    monkeypatch.setattr(_bootstrap, "_import_cli", _always_fail)
    monkeypatch.setattr(_bootstrap, "_self_heal", lambda missing: False)
    with pytest.raises(SystemExit) as exc_info:
        _bootstrap.main()
    assert exc_info.value.code == 1
    assert "pip install -e" in capsys.readouterr().err


def test_still_missing_after_heal_exits_without_looping(monkeypatch, capsys):
    """The heal is attempted exactly once — no retry loop."""
    imports: list[str] = []
    heals: list[str] = []

    def _always_fail():
        imports.append("import")
        raise ModuleNotFoundError("No module named 'defusedxml'", name="defusedxml")

    monkeypatch.setattr(_bootstrap, "_import_cli", _always_fail)
    monkeypatch.setattr(_bootstrap, "_self_heal", lambda missing: heals.append("heal") or True)
    with pytest.raises(SystemExit) as exc_info:
        _bootstrap.main()
    assert exc_info.value.code == 1
    assert imports == ["import", "import"]
    assert heals == ["heal"]
    assert "still failing" in capsys.readouterr().err


# ── _self_heal ──


def test_self_heal_refuses_outside_source_checkout(monkeypatch):
    monkeypatch.setattr(_bootstrap, "_source_checkout_root", lambda: None)
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: pytest.fail("pip must not run")
    )
    assert _bootstrap._self_heal("defusedxml") is False


def test_self_heal_skips_windows(monkeypatch, tmp_path):
    """A running console launcher cannot be replaced on Windows -> no heal."""
    monkeypatch.setattr(_bootstrap.sys, "platform", "win32")
    monkeypatch.setattr(_bootstrap, "_source_checkout_root", lambda: tmp_path)
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: pytest.fail("pip must not run")
    )
    assert _bootstrap._self_heal("defusedxml") is False


def test_retry_invalidates_import_caches(monkeypatch):
    """The just-installed package must be visible to the retry import."""
    calls: list[str] = []

    def _sentinel_cli() -> None:
        return None

    def _import():
        calls.append("import")
        if calls.count("import") == 1:
            raise ModuleNotFoundError("No module named 'defusedxml'", name="defusedxml")
        return _sentinel_cli

    monkeypatch.setattr(_bootstrap, "_import_cli", _import)
    monkeypatch.setattr(_bootstrap, "_self_heal", lambda missing: True)
    monkeypatch.setattr(
        _bootstrap.importlib, "invalidate_caches", lambda: calls.append("invalidate")
    )
    _bootstrap.main()
    assert calls == ["import", "invalidate", "import"]


def test_self_heal_runs_fixed_pip_argv(monkeypatch, tmp_path):
    monkeypatch.setattr(_bootstrap.sys, "platform", "linux")  # POSIX heal path
    monkeypatch.setattr(_bootstrap, "_source_checkout_root", lambda: tmp_path)
    seen: dict = {}

    def _fake_run(argv, timeout):
        seen["argv"] = argv
        seen["timeout"] = timeout

        class _P:
            returncode = 0

        return _P()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert _bootstrap._self_heal("defusedxml") is True
    assert seen["argv"][1:] == ["-m", "pip", "install", "-e", str(tmp_path), "--quiet"]
    assert seen["timeout"] == _bootstrap._PIP_TIMEOUT_SECS


def test_self_heal_reports_pip_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(_bootstrap.sys, "platform", "linux")  # POSIX heal path
    monkeypatch.setattr(_bootstrap, "_source_checkout_root", lambda: tmp_path)

    def _fake_run(argv, timeout):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert _bootstrap._self_heal("defusedxml") is False


# ── _source_checkout_root ──


def test_source_checkout_root_detects_this_repo():
    """Running from the repo's own tree, the root must resolve here."""
    root = _bootstrap._source_checkout_root()
    assert root is not None
    assert (root / "setup.cfg").is_file()
    assert Path(_bootstrap.__file__).is_relative_to(root)
