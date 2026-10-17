"""Tests for the dashboard bundle-freshness guard.

The guard is the FRESHNESS counterpart to the stale-asset (vanish) watchdog: it
warns when the served ``dist`` was built from a different ``website/`` tree than
the running backend, and — unlike the vanish watchdog — never shuts down. These
tests pin that warn-only contract, the ``website/``-tree identity (a
backend-only commit stays silent), and the conservative "cannot verify → skip
silently" behaviour on every unknown side of the comparison.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

_LOGGER = "kiro_crew.dashboard.stale_bundle_guard"


def _write_build_id(tmp_path: Path, tree: str, build_id: str = "1.0.0-abc1234") -> Path:
    dist = tmp_path / "dist"
    dist.mkdir(exist_ok=True)
    path = dist / "build-id.json"
    path.write_text(json.dumps({"buildId": build_id, "websiteTree": tree}), encoding="utf-8")
    return path


def _warnings(caplog) -> list[logging.LogRecord]:
    # Only the guard's own records: a host without a trusted git logs its own
    # platform_compat WARNING, which is not a staleness signal.
    return [r for r in caplog.records if r.name == _LOGGER and r.levelno >= logging.WARNING]


def _run(mod, build_id_path: Path, source_path: Path, backend_tree, caplog) -> None:
    caplog.set_level(logging.DEBUG, logger=_LOGGER)
    backend = backend_tree if callable(backend_tree) else (lambda: backend_tree)
    with (
        patch.object(mod, "_BUILD_ID_PATH", build_id_path),
        patch.object(mod, "_BUILD_SOURCE_PATH", source_path),
        patch.object(mod, "_backend_website_tree", backend),
    ):
        mod.check_bundle_freshness()


def test_warns_on_tree_mismatch(tmp_path: Path, caplog):
    """A dist built from a different website/ tree than the backend → WARNING."""
    from kiro_crew.dashboard import stale_bundle_guard as mod

    path = _write_build_id(tmp_path, tree="a" * 40, build_id="1.0.0-aaaaaaa")
    _run(mod, path, tmp_path / "none.txt", "b" * 40, caplog)

    warnings = _warnings(caplog)
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert "STALE" in msg
    # Names the served build and both trees so an operator can tell which is old.
    assert "1.0.0-aaaaaaa" in msg
    assert "aaaaaaa" in msg
    assert "bbbbbbb" in msg


def test_silent_when_trees_match(tmp_path: Path, caplog):
    """Matching website/ trees → no warning (the healthy case)."""
    from kiro_crew.dashboard import stale_bundle_guard as mod

    path = _write_build_id(tmp_path, tree="c" * 40)
    _run(mod, path, tmp_path / "none.txt", "c" * 40, caplog)
    assert not _warnings(caplog)


def test_fresh_build_id_wins_over_old_fingerprint(tmp_path: Path, caplog):
    """A hand-run rebuild copied over an old dist leaves an old fingerprint.

    The build-id.json every vite build rewrites must win, so a correct rebuild
    is not reported STALE because of the leftover kirocrew-build-source.txt.
    """
    from kiro_crew.dashboard import stale_bundle_guard as mod

    path = _write_build_id(tmp_path, tree="d" * 40)
    source = tmp_path / "dist" / "kirocrew-build-source.txt"
    source.write_text("a" * 40 + "\n", encoding="utf-8")
    _run(mod, path, source, "d" * 40, caplog)
    assert not _warnings(caplog)


def test_dirty_build_id_does_not_fall_back_to_old_fingerprint(tmp_path: Path, caplog):
    """An empty websiteTree (dirty build) skips; the old fingerprint is not used."""
    from kiro_crew.dashboard import stale_bundle_guard as mod

    path = _write_build_id(tmp_path, tree="")
    source = tmp_path / "dist" / "kirocrew-build-source.txt"
    source.write_text("a" * 40, encoding="utf-8")
    _run(mod, path, source, "d" * 40, caplog)
    assert not _warnings(caplog)


def test_build_source_fingerprint_alone_is_enough(tmp_path: Path, caplog):
    """No build-id.json but a build-source fingerprint → still compared."""
    from kiro_crew.dashboard import stale_bundle_guard as mod

    (tmp_path / "dist").mkdir()
    source = tmp_path / "dist" / "kirocrew-build-source.txt"
    source.write_text("e" * 40, encoding="utf-8")
    _run(mod, tmp_path / "dist" / "build-id.json", source, "f" * 40, caplog)
    assert len(_warnings(caplog)) == 1


def test_skips_when_no_stamp(tmp_path: Path, caplog):
    """No stamp at all (dist predates this feature) → skip silently."""
    from kiro_crew.dashboard import stale_bundle_guard as mod

    missing = tmp_path / "dist" / "build-id.json"  # never created
    _run(mod, missing, tmp_path / "none.txt", "d" * 40, caplog)
    assert not _warnings(caplog)


def test_skips_when_backend_tree_unknown(tmp_path: Path, caplog):
    """Backend tree unknown (no baked value, no git) → skip silently."""
    from kiro_crew.dashboard import stale_bundle_guard as mod

    path = _write_build_id(tmp_path, tree="e" * 40)
    _run(mod, path, tmp_path / "none.txt", "", caplog)
    assert not _warnings(caplog)


def test_skips_when_dist_tree_empty(tmp_path: Path, caplog):
    """websiteTree "" (dirty or git-less frontend build) → skip, not a mismatch."""
    from kiro_crew.dashboard import stale_bundle_guard as mod

    path = _write_build_id(tmp_path, tree="", build_id="1.0.0")
    _run(mod, path, tmp_path / "none.txt", "f" * 40, caplog)
    assert not _warnings(caplog)


def test_skips_on_malformed_build_id_json(tmp_path: Path, caplog):
    """A corrupt/truncated build-id.json must not warn or raise."""
    from kiro_crew.dashboard import stale_bundle_guard as mod

    dist = tmp_path / "dist"
    dist.mkdir()
    bad = dist / "build-id.json"
    bad.write_text("{not valid json", encoding="utf-8")
    _run(mod, bad, tmp_path / "none.txt", "a" * 40, caplog)
    assert not _warnings(caplog)


def test_check_never_raises(tmp_path: Path, caplog):
    """Any internal failure is swallowed — a freshness advisory can't break startup."""
    from kiro_crew.dashboard import stale_bundle_guard as mod

    path = _write_build_id(tmp_path, tree="a" * 40)

    def _boom() -> str:
        raise RuntimeError("backend tree lookup exploded")

    _run(mod, path, tmp_path / "none.txt", _boom, caplog)


# ── real git: the identity is the website/ tree, not HEAD ──


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    if shutil.which("git") is None:
        pytest.skip("git not available")
    from kiro_crew.platform_compat import trusted_git_bin

    if trusted_git_bin() is None:
        pytest.skip("no git in the trusted system dirs; the guard skips by design")
    root = tmp_path / "repo"
    (root / "website").mkdir(parents=True)
    (root / "src" / "kiro_crew").mkdir(parents=True)
    (root / "website" / "app.ts").write_text("v1\n", encoding="utf-8")
    (root / "src" / "kiro_crew" / "x.py").write_text("v1\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "one")
    return root


def _check_against_repo(mod, repo: Path, built_tree: str, tmp_path: Path, caplog) -> None:
    path = _write_build_id(tmp_path, tree=built_tree)
    caplog.set_level(logging.DEBUG, logger=_LOGGER)
    with (
        patch.object(mod, "_BUILD_ID_PATH", path),
        patch.object(mod, "_BUILD_SOURCE_PATH", tmp_path / "none.txt"),
        patch.object(mod, "_PACKAGE_ROOT", repo / "src" / "kiro_crew"),
        patch("kiro_crew.beacon.baked_website_tree", return_value=""),
    ):
        mod.check_bundle_freshness()


def test_backend_only_commit_after_build_stays_silent(repo: Path, tmp_path: Path, caplog):
    """A commit touching no website/ file after a build must NOT warn."""
    from kiro_crew.dashboard import stale_bundle_guard as mod

    built_tree = _git(repo, "rev-parse", "HEAD:website")
    (repo / "src" / "kiro_crew" / "x.py").write_text("v2\n", encoding="utf-8")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "backend only")

    _check_against_repo(mod, repo, built_tree, tmp_path, caplog)
    assert not _warnings(caplog)


def test_frontend_commit_after_build_warns(repo: Path, tmp_path: Path, caplog):
    """A commit that changes website/ after a build → WARNING."""
    from kiro_crew.dashboard import stale_bundle_guard as mod

    built_tree = _git(repo, "rev-parse", "HEAD:website")
    (repo / "website" / "app.ts").write_text("v2\n", encoding="utf-8")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "frontend")

    _check_against_repo(mod, repo, built_tree, tmp_path, caplog)
    assert len(_warnings(caplog)) == 1


# ── _backend_website_tree resolution ──


def test_backend_tree_prefers_baked_over_git():
    """A packaged install's baked tree wins over the git fallback."""
    from kiro_crew.dashboard import stale_bundle_guard as mod

    with (
        patch("kiro_crew.beacon.baked_website_tree", return_value="1" * 40) as baked,
        patch("subprocess.run") as run,
    ):
        assert mod._backend_website_tree() == "1" * 40
        baked.assert_called_once()
        run.assert_not_called()


def test_backend_tree_uses_trusted_hardened_git():
    """Source checkout: `git rev-parse HEAD:website` via trusted_git_bin + hardened env."""
    from kiro_crew.dashboard import stale_bundle_guard as mod

    class _Result:
        returncode = 0
        stdout = "9" * 40 + "\n"

    with (
        patch("kiro_crew.beacon.baked_website_tree", return_value=""),
        patch.object(mod, "trusted_git_bin", return_value="/usr/bin/git"),
        patch("subprocess.run", return_value=_Result()) as run,
    ):
        assert mod._backend_website_tree() == "9" * 40
        run.assert_called_once()
        argv = run.call_args.args[0]
        assert argv[0] == "/usr/bin/git"
        assert argv[-1] == "HEAD:website"
        env = run.call_args.kwargs["env"]
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"


def test_backend_tree_empty_when_git_not_trusted():
    """No trusted git → "" (skip), and no spawn at all."""
    from kiro_crew.dashboard import stale_bundle_guard as mod

    with (
        patch("kiro_crew.beacon.baked_website_tree", return_value=""),
        patch.object(mod, "trusted_git_bin", return_value=None),
        patch("subprocess.run") as run,
    ):
        assert mod._backend_website_tree() == ""
        run.assert_not_called()


def test_backend_tree_empty_when_git_fails():
    """git raising or exiting non-zero → "" so the guard skips."""
    from kiro_crew.dashboard import stale_bundle_guard as mod

    class _Result:
        returncode = 128
        stdout = ""

    with (
        patch("kiro_crew.beacon.baked_website_tree", return_value=""),
        patch.object(mod, "trusted_git_bin", return_value="/usr/bin/git"),
    ):
        with patch("subprocess.run", side_effect=OSError("git not found")):
            assert mod._backend_website_tree() == ""
        with patch("subprocess.run", return_value=_Result()):
            assert mod._backend_website_tree() == ""


# ── beacon.baked_website_tree accessor ──


def test_beacon_baked_website_tree_reads_binding(monkeypatch):
    """baked_website_tree reflects the module-level _BAKED_WEBSITE_TREE binding."""
    from kiro_crew import beacon

    monkeypatch.setattr(beacon, "_BAKED_WEBSITE_TREE", "  " + "a" * 40 + "  ")
    assert beacon.baked_website_tree() == "a" * 40

    monkeypatch.setattr(beacon, "_BAKED_WEBSITE_TREE", "")
    assert beacon.baked_website_tree() == ""
