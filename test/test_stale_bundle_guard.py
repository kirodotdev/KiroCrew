"""Tests for the dashboard bundle-freshness guard.

The guard is the FRESHNESS counterpart to the stale-asset (vanish) watchdog: it
warns when the served ``dist`` was built from a different commit than the running
backend, and — unlike the vanish watchdog — never shuts down. These tests pin
that warn-only contract and the conservative "cannot verify → skip silently"
behaviour on every unknown side of the comparison.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import patch


def _write_build_id(tmp_path: Path, commit: str, build_id: str = "1.0.0-abc1234") -> Path:
    dist = tmp_path / "dist"
    dist.mkdir(exist_ok=True)
    path = dist / "build-id.json"
    path.write_text(
        json.dumps({"buildId": build_id, "commit": commit, "builtAt": "2026-08-13T00:00:00Z"}),
        encoding="utf-8",
    )
    return path


def test_warns_on_commit_mismatch(tmp_path: Path, caplog):
    """A dist built from a different commit than the backend → WARNING."""
    from kiro_crew.dashboard import stale_bundle_guard as mod

    build_id_path = _write_build_id(tmp_path, commit="a" * 40, build_id="1.0.0-aaaaaaa")

    caplog.set_level(logging.DEBUG, logger="kiro_crew.dashboard.stale_bundle_guard")
    with (
        patch.object(mod, "_BUILD_ID_PATH", build_id_path),
        patch.object(mod, "_backend_commit", return_value="b" * 40),
    ):
        mod.check_bundle_freshness()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert "STALE" in msg
    # Names both the served build-id and the backend commit so an operator can
    # tell which side is old.
    assert "1.0.0-aaaaaaa" in msg
    assert "bbbbbbb" in msg


def test_silent_when_commits_match(tmp_path: Path, caplog):
    """Matching commits → no warning (the healthy case)."""
    from kiro_crew.dashboard import stale_bundle_guard as mod

    build_id_path = _write_build_id(tmp_path, commit="c" * 40)

    caplog.set_level(logging.DEBUG, logger="kiro_crew.dashboard.stale_bundle_guard")
    with (
        patch.object(mod, "_BUILD_ID_PATH", build_id_path),
        patch.object(mod, "_backend_commit", return_value="c" * 40),
    ):
        mod.check_bundle_freshness()

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_skips_when_build_id_missing(tmp_path: Path, caplog):
    """No dist/build-id.json (dist predates this feature) → skip silently."""
    from kiro_crew.dashboard import stale_bundle_guard as mod

    missing = tmp_path / "dist" / "build-id.json"  # never created

    caplog.set_level(logging.DEBUG, logger="kiro_crew.dashboard.stale_bundle_guard")
    with (
        patch.object(mod, "_BUILD_ID_PATH", missing),
        patch.object(mod, "_backend_commit", return_value="d" * 40),
    ):
        mod.check_bundle_freshness()

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_skips_when_backend_commit_unknown(tmp_path: Path, caplog):
    """Backend commit unknown (source build, no git) → skip silently."""
    from kiro_crew.dashboard import stale_bundle_guard as mod

    build_id_path = _write_build_id(tmp_path, commit="e" * 40)

    caplog.set_level(logging.DEBUG, logger="kiro_crew.dashboard.stale_bundle_guard")
    with (
        patch.object(mod, "_BUILD_ID_PATH", build_id_path),
        patch.object(mod, "_backend_commit", return_value=""),
    ):
        mod.check_bundle_freshness()

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_skips_when_dist_commit_empty(tmp_path: Path, caplog):
    """build-id.json present but commit is "" (git-less frontend build) → skip.

    The stamp is written but the git SHA was unavailable at build time, so there
    is no identity to compare — treat as "cannot verify", not a mismatch.
    """
    from kiro_crew.dashboard import stale_bundle_guard as mod

    build_id_path = _write_build_id(tmp_path, commit="", build_id="1.0.0")

    caplog.set_level(logging.DEBUG, logger="kiro_crew.dashboard.stale_bundle_guard")
    with (
        patch.object(mod, "_BUILD_ID_PATH", build_id_path),
        patch.object(mod, "_backend_commit", return_value="f" * 40),
    ):
        mod.check_bundle_freshness()

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_skips_on_malformed_build_id_json(tmp_path: Path, caplog):
    """A corrupt/truncated build-id.json must not warn or raise."""
    from kiro_crew.dashboard import stale_bundle_guard as mod

    dist = tmp_path / "dist"
    dist.mkdir()
    bad = dist / "build-id.json"
    bad.write_text("{not valid json", encoding="utf-8")

    caplog.set_level(logging.DEBUG, logger="kiro_crew.dashboard.stale_bundle_guard")
    with (
        patch.object(mod, "_BUILD_ID_PATH", bad),
        patch.object(mod, "_backend_commit", return_value="a" * 40),
    ):
        mod.check_bundle_freshness()

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_check_never_raises(tmp_path: Path):
    """Any internal failure is swallowed — a freshness advisory can't break startup."""
    from kiro_crew.dashboard import stale_bundle_guard as mod

    build_id_path = _write_build_id(tmp_path, commit="a" * 40)

    def _boom() -> str:
        raise RuntimeError("backend commit lookup exploded")

    with (
        patch.object(mod, "_BUILD_ID_PATH", build_id_path),
        patch.object(mod, "_backend_commit", side_effect=_boom),
    ):
        # Must not propagate.
        mod.check_bundle_freshness()


# ── _backend_commit resolution ──


def test_backend_commit_prefers_baked_over_git():
    """A packaged install's baked commit wins over the git fallback."""
    from kiro_crew.dashboard import stale_bundle_guard as mod

    with (
        patch("kiro_crew.beacon.baked_commit", return_value="1" * 40) as baked,
        patch("subprocess.run") as run,
    ):
        assert mod._backend_commit() == "1" * 40
        baked.assert_called_once()
        # Baked value short-circuits before any git subprocess.
        run.assert_not_called()


def test_backend_commit_falls_back_to_git_in_source_checkout():
    """With no baked commit, resolve via `git rev-parse HEAD` (dev/source mode).

    git itself is pinned through ``trusted_system_bin`` — the argv must carry
    the resolved absolute path, never a bare ``git`` for PATH to answer.
    """
    from kiro_crew.dashboard import stale_bundle_guard as mod

    class _Result:
        returncode = 0
        stdout = "9" * 40 + "\n"

    with (
        patch("kiro_crew.beacon.baked_commit", return_value=""),
        patch.object(mod.platform_compat, "trusted_system_bin", return_value="/usr/bin/git"),
        patch("subprocess.run", return_value=_Result()) as run,
    ):
        assert mod._backend_commit() == "9" * 40
        run.assert_called_once()
        assert run.call_args.args[0][0] == "/usr/bin/git"


def test_backend_commit_empty_when_git_not_in_trusted_dirs():
    """git absent from the fixed system dirs → "" (skip), and no spawn at all.

    The PATH may still find a git (mise/homebrew/an agent-planted shim) — the
    guard must not fall back to it.
    """
    from kiro_crew.dashboard import stale_bundle_guard as mod

    with (
        patch("kiro_crew.beacon.baked_commit", return_value=""),
        patch.object(mod.platform_compat, "trusted_system_bin", return_value=None),
        patch("subprocess.run") as run,
    ):
        assert mod._backend_commit() == ""
        run.assert_not_called()


def test_backend_commit_empty_when_git_unavailable():
    """No baked commit and git failing → "" so the guard skips."""
    from kiro_crew.dashboard import stale_bundle_guard as mod

    with (
        patch("kiro_crew.beacon.baked_commit", return_value=""),
        patch.object(mod.platform_compat, "trusted_system_bin", return_value="/usr/bin/git"),
        patch("subprocess.run", side_effect=OSError("git not found")),
    ):
        assert mod._backend_commit() == ""


def test_backend_commit_empty_when_git_returns_nonzero():
    """git rev-parse exiting non-zero (not a repo) → "" so the guard skips."""
    from kiro_crew.dashboard import stale_bundle_guard as mod

    class _Result:
        returncode = 128
        stdout = ""

    with (
        patch("kiro_crew.beacon.baked_commit", return_value=""),
        patch.object(mod.platform_compat, "trusted_system_bin", return_value="/usr/bin/git"),
        patch("subprocess.run", return_value=_Result()),
    ):
        assert mod._backend_commit() == ""


# ── beacon.baked_commit accessor ──


def test_beacon_baked_commit_reads_binding(monkeypatch):
    """baked_commit reflects the module-level _BAKED_COMMIT binding."""
    from kiro_crew import beacon

    monkeypatch.setattr(beacon, "_BAKED_COMMIT", "  " + "a" * 40 + "  ")
    assert beacon.baked_commit() == "a" * 40

    monkeypatch.setattr(beacon, "_BAKED_COMMIT", "")
    assert beacon.baked_commit() == ""
