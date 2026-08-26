"""Tests for the ``kiro_crew._bootstrap`` console-entry self-heal.

The bootstrap closes the git-pull gap for editable installs: a commit that
adds a runtime dependency must not leave ``kirocrew`` dying on a raw
``ModuleNotFoundError`` when one ``pip install -e .`` fixes it. These tests
stub the import and the pip spawn — no real installs, no network.
"""

from __future__ import annotations

import importlib.util
import subprocess
from configparser import ConfigParser
from pathlib import Path

import pytest

from kiro_crew import _bootstrap

# ── Helpers ──


def _venv_maps(monkeypatch):
    """Answer the install path's foreign-venv guard with "it maps".

    ``dep_sync.sync_or_reinstall`` refuses a venv serving a DIFFERENT checkout
    before it picks a branch, and it answers that by RUNNING the target
    interpreter — which these tests stub. Without this the guard's refusal would
    stand in for the outcome each test is actually asserting, and two of them
    would pass for the wrong reason. The guard itself is covered in
    test/test_dep_sync.py.
    """
    from kiro_crew import dep_sync

    monkeypatch.setattr(dep_sync, "installed_package_origin", lambda target: "<stub>")
    monkeypatch.setattr(dep_sync, "venv_not_mapped_to", lambda origin, repo: None)


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


def test_self_heal_runs_on_windows_through_the_dependency_only_path(monkeypatch, tmp_path):
    """Windows heals now. It used to be the one platform that never did.

    The blanket skip was there because pip cannot replace the running
    ``kirocrew.exe`` — but a dependency install never touches that wrapper, and a
    missing dependency is the only thing that brings us here. Skipping left the
    platform whose users hit this most with nothing but a printed one-liner.
    """
    from kiro_crew import dep_sync

    monkeypatch.setattr(_bootstrap.sys, "platform", "win32")
    monkeypatch.setattr(_bootstrap, "_source_checkout_root", lambda: tmp_path)
    _venv_maps(monkeypatch)
    monkeypatch.setattr(
        dep_sync, "locked_console_scripts", lambda target: [r"C:\v\Scripts\kirocrew.exe"]
    )
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: pytest.fail("the reinstall must not run")
    )
    seen: dict = {}

    def _fake_sync(repo, target_py, emit=None, timeout=None):
        seen["repo"] = repo
        seen["timeout"] = timeout
        return 0

    monkeypatch.setattr(dep_sync, "sync", _fake_sync)
    assert _bootstrap._self_heal("defusedxml") is True
    assert seen["repo"] == tmp_path
    # The substitute is bounded too — an unbounded dependency install would hang
    # the console entry point with no way out.
    assert seen["timeout"] == _bootstrap._PIP_TIMEOUT_SECS


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
    """Where pip CAN rewrite the script, the heal is still the full reinstall."""
    from kiro_crew import dep_sync

    monkeypatch.setattr(_bootstrap.sys, "platform", "linux")  # POSIX heal path
    monkeypatch.setattr(_bootstrap, "_source_checkout_root", lambda: tmp_path)
    _venv_maps(monkeypatch)
    monkeypatch.setattr(dep_sync, "locked_console_scripts", lambda target: [])
    seen: dict = {}

    def _fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["timeout"] = kwargs.get("timeout")

        class _P:
            returncode = 0
            stdout = b""
            stderr = b""

        return _P()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert _bootstrap._self_heal("defusedxml") is True
    assert seen["argv"][1:] == ["-m", "pip", "install", "-e", str(tmp_path), "--quiet"]
    assert seen["timeout"] == _bootstrap._PIP_TIMEOUT_SECS


def test_self_heal_reports_pip_failure(monkeypatch, tmp_path):
    from kiro_crew import dep_sync

    monkeypatch.setattr(_bootstrap.sys, "platform", "linux")  # POSIX heal path
    monkeypatch.setattr(_bootstrap, "_source_checkout_root", lambda: tmp_path)
    _venv_maps(monkeypatch)
    monkeypatch.setattr(dep_sync, "locked_console_scripts", lambda target: [])

    def _fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert _bootstrap._self_heal("defusedxml") is False


def test_self_heal_output_stays_ascii(monkeypatch, tmp_path, capsys):
    """Every line this module prints must survive a cp1252 pipe.

    The heal now relays pip's output and filesystem paths, neither of which is
    ASCII by nature, and it prints before ensure_utf8_console() has run.
    """
    from kiro_crew import dep_sync

    monkeypatch.setattr(_bootstrap.sys, "platform", "linux")
    monkeypatch.setattr(_bootstrap, "_source_checkout_root", lambda: tmp_path)
    _venv_maps(monkeypatch)
    monkeypatch.setattr(dep_sync, "locked_console_scripts", lambda target: [])

    def _fake_run(argv, **kwargs):
        class _P:
            returncode = 1
            stdout = b""
            stderr = "pip a\u00e9choue \u2014 pas de distribution".encode()

        return _P()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert _bootstrap._self_heal("defusedxml") is False
    err = capsys.readouterr().err
    assert err  # the failure was reported, not swallowed
    err.encode("ascii")  # raises UnicodeEncodeError if anything slipped through


# ── _source_checkout_root ──


def test_source_checkout_root_detects_this_repo():
    """Running from the repo's own tree, the root must resolve here."""
    root = _bootstrap._source_checkout_root()
    assert root is not None
    assert (root / "setup.cfg").is_file()
    assert Path(_bootstrap.__file__).is_relative_to(root)


# ── console_scripts targets ──


def test_every_console_script_target_resolves():
    """Every ``console_scripts`` target must name a module that exists.

    Packaging metadata is not imported by the suite, so a target left behind by
    a deleted module passes every other gate and fails only in a user's shell:
    pip still writes the shim onto PATH, and running it raises
    ModuleNotFoundError. ``find_spec`` is used rather than a real import so the
    check stays free of import side effects.
    """
    root = _bootstrap._source_checkout_root()
    assert root is not None
    cfg = ConfigParser()
    cfg.read(root / "setup.cfg", encoding="utf-8")
    entries = [
        line.strip()
        for line in cfg["options.entry_points"]["console_scripts"].splitlines()
        if line.strip()
    ]
    assert entries, "setup.cfg declares no console scripts"
    for entry in entries:
        script, _, target = entry.partition("=")
        module = target.strip().partition(":")[0]
        try:
            spec = importlib.util.find_spec(module)
        except ModuleNotFoundError:
            spec = None
        assert spec is not None, f"console script {script.strip()!r} names missing module {module!r}"
