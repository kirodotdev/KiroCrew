"""Dependency preflight — reports rather than repairs.

The interesting behaviour is entirely in the failure shapes: ``gh`` present but not
logged in must be distinguishable from ``gh`` absent (the first is a `gh auth login`,
the second an install), and only ``ruff`` may ever be installed — never a system-wide
install and never an authenticated CLI.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.auto_improvement.backend import deps


def _stub_bin(name: str) -> str:
    """An absolute ``which``-style stub path that is valid on every platform.

    Absolute because the PATH-hijack guards refuse a relative binary path, and with a
    directory component because some call sites take ``Path(x).name`` -- a bare name
    would make that assertion vacuous. Rooted at ``tempfile.gettempdir()``, the
    portable root the cross-platform gate recommends, rather than a ``/usr/bin``
    literal that does not exist on Windows. Nothing is created or executed here.
    """
    return str(Path(tempfile.gettempdir()) / "stub-bin" / name)


def _proc(returncode: int, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["x"], returncode=returncode, stdout="", stderr=stderr)


@pytest.fixture
def which(monkeypatch: pytest.MonkeyPatch):
    """Control what is 'on PATH' for one test."""
    table: dict[str, str] = {}

    def _fake(binary: str) -> str | None:
        return table.get(binary)

    monkeypatch.setattr(deps.shutil, "which", _fake)
    return table


class TestWhich:
    def test_a_found_binary_yields_its_path(self, which: dict[str, str]) -> None:
        which["git"] = _stub_bin("git")
        assert deps._which("git") == _stub_bin("git")

    def test_a_missing_binary_yields_the_empty_string_not_none(self, which: dict[str, str]) -> None:
        """Callers do ``bool(path)`` and put the value straight in ``detail``."""
        assert deps._which("nope") == ""


class TestGhAuthenticated:
    def test_gh_absent_is_reported_without_running_anything(
        self, which: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*a: Any, **k: Any) -> Any:  # pragma: no cover - must not be reached
            raise AssertionError("gh must not be executed when it is not on PATH")

        monkeypatch.setattr(deps.subprocess, "run", _boom)
        ok, detail = deps._gh_authenticated()
        assert ok is False
        assert detail == "gh is not on PATH"

    def test_a_live_login_is_authenticated(
        self, which: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        which["gh"] = _stub_bin("gh")
        seen: dict[str, Any] = {}

        def _run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            seen["cmd"] = cmd
            seen["timeout"] = kwargs.get("timeout")
            return _proc(0)

        monkeypatch.setattr(deps.subprocess, "run", _run)
        assert deps._gh_authenticated() == (True, "authenticated")
        assert seen["cmd"] == ["gh", "auth", "status"]
        assert seen["timeout"] == deps._PROBE_TIMEOUT_S

    def test_present_but_logged_out_points_at_gh_auth_login(
        self, which: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Presence on PATH is not enough — this is the case the probe exists for."""
        which["gh"] = _stub_bin("gh")
        monkeypatch.setattr(deps.subprocess, "run", lambda *a, **k: _proc(1))
        ok, detail = deps._gh_authenticated()
        assert ok is False
        assert "gh auth login" in detail

    @pytest.mark.parametrize(
        "exc",
        [OSError("no exec"), subprocess.TimeoutExpired(cmd="gh", timeout=15.0)],
    )
    def test_a_probe_that_cannot_run_degrades_to_not_ok(
        self, which: dict[str, str], monkeypatch: pytest.MonkeyPatch, exc: Exception
    ) -> None:
        which["gh"] = _stub_bin("gh")

        def _run(*a: Any, **k: Any) -> Any:
            raise exc

        monkeypatch.setattr(deps.subprocess, "run", _run)
        ok, detail = deps._gh_authenticated()
        assert ok is False
        assert detail.startswith("could not run gh auth status:")


class TestCheckDeps:
    def test_everything_present_is_ok_with_nothing_blocking(
        self, which: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        which.update({"git": _stub_bin("git"), "gh": _stub_bin("gh"), "ruff": _stub_bin("ruff")})
        monkeypatch.setattr(deps.subprocess, "run", lambda *a, **k: _proc(0))
        report = deps.check_deps()
        assert report["ok"] is True
        assert report["blocking"] == []
        by_id = {d["id"]: d for d in report["deps"]}
        assert set(by_id) == {"git", "gh", "ruff"}
        assert by_id["git"]["detail"] == _stub_bin("git")
        assert by_id["ruff"]["installable"] is True
        assert by_id["gh"]["installable"] is False

    def test_a_missing_required_binary_blocks_the_run(
        self, which: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        which["gh"] = _stub_bin("gh")
        monkeypatch.setattr(deps.subprocess, "run", lambda *a, **k: _proc(0))
        report = deps.check_deps()
        assert report["ok"] is False
        assert report["blocking"] == ["git"]
        by_id = {d["id"]: d for d in report["deps"]}
        assert by_id["git"]["ok"] is False
        assert by_id["git"]["detail"] == "not found on PATH"

    def test_a_missing_optional_binary_only_narrows_discovery(
        self, which: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        which.update({"git": _stub_bin("git"), "gh": _stub_bin("gh")})
        monkeypatch.setattr(deps.subprocess, "run", lambda *a, **k: _proc(0))
        report = deps.check_deps()
        assert report["ok"] is True
        assert report["blocking"] == []
        ruff = next(d for d in report["deps"] if d["id"] == "ruff")
        assert ruff["ok"] is False
        assert ruff["required"] is False
        assert "compile check" in ruff["detail"]

    def test_both_required_entries_can_block_at_once(
        self, which: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(deps.subprocess, "run", lambda *a, **k: _proc(0))
        report = deps.check_deps()
        assert report["blocking"] == ["git", "gh"]
        assert report["ok"] is False


class TestInstallDeps:
    def test_an_already_present_ruff_is_a_no_op(
        self, which: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        which["ruff"] = _stub_bin("ruff")

        def _boom(*a: Any, **k: Any) -> Any:  # pragma: no cover - must not be reached
            raise AssertionError("pip must not run when ruff is already present")

        monkeypatch.setattr(deps.subprocess, "run", _boom)
        assert deps.install_deps() == {
            "ok": True,
            "installed": [],
            "detail": "ruff already present",
        }

    def test_a_successful_install_uses_this_interpreters_pip_only(
        self, which: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}

        def _run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            seen["cmd"] = cmd
            return _proc(0)

        monkeypatch.setattr(deps.subprocess, "run", _run)
        assert deps.install_deps() == {
            "ok": True,
            "installed": ["ruff"],
            "detail": "ruff installed",
        }
        assert seen["cmd"] == [sys.executable, "-m", "pip", "install", "--quiet", "ruff"]

    def test_a_pip_failure_reports_the_last_stderr_line(
        self, which: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            deps.subprocess,
            "run",
            lambda *a, **k: _proc(1, stderr="warming up\nERROR: no matching distribution\n"),
        )
        out = deps.install_deps()
        assert out["ok"] is False
        assert out["installed"] == []
        assert out["error"] == "pip failed: ERROR: no matching distribution"

    def test_a_pip_failure_with_no_stderr_still_reports_an_error(
        self, which: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ``or [""]`` fallback — an empty stderr must not IndexError."""
        monkeypatch.setattr(deps.subprocess, "run", lambda *a, **k: _proc(2, stderr=""))
        out = deps.install_deps()
        assert out["ok"] is False
        assert out["error"] == "pip failed: "

    def test_a_pip_failure_line_is_truncated(
        self, which: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(deps.subprocess, "run", lambda *a, **k: _proc(1, stderr="x" * 500))
        out = deps.install_deps()
        assert out["error"] == "pip failed: " + "x" * 200

    @pytest.mark.parametrize(
        "exc",
        [OSError("no pip"), subprocess.TimeoutExpired(cmd="pip", timeout=300.0)],
    )
    def test_an_install_that_cannot_run_is_reported_not_raised(
        self, which: dict[str, str], monkeypatch: pytest.MonkeyPatch, exc: Exception
    ) -> None:
        def _run(*a: Any, **k: Any) -> Any:
            raise exc

        monkeypatch.setattr(deps.subprocess, "run", _run)
        out = deps.install_deps()
        assert out["ok"] is False
        assert out["installed"] == []
        assert out["error"].startswith("install failed:")
