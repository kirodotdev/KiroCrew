"""Tests for ``kiro_crew.frontend.ensure_dev_dist_symlink``.

Covers the runtime dist-resolution contract described:

* pre-bundled real directory is left alone (packaged install / prior build)
* valid symlink is kept
* dangling / empty symlink is replaced
* sibling ``KiroCrewWebsite/dist`` is resolved and symlinked
* nothing-found returns ``None`` (caller logs warning and serves legacy UI)
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from kiro_crew import frontend, platform_compat


def _fake_kiro_crew_package(root: Path) -> Path:
    """Build the minimal directory shape the resolver walks."""
    pkg = root / "src" / "KiroCrew" / "src" / "kiro_crew"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    return pkg


def _make_dist(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "index.html").write_text("<!doctype html><html></html>")
    return path


@pytest.fixture
def fake_pkg(tmp_path, monkeypatch):
    """Patch ``frontend.__file__`` to a throwaway filesystem layout.

    Returns the ``kiro_crew`` package dir (``<ws>/src/KiroCrew/src/kiro_crew``).
    The resolver uses ``Path(__file__)`` from ``kiro_crew.frontend`` to locate
    the package; monkeypatching that attribute redirects every probe to the
    temp-dir tree we build in each test.
    """
    pkg = _fake_kiro_crew_package(tmp_path)
    monkeypatch.setattr(frontend, "__file__", str(pkg / "frontend.py"))
    return pkg


def _no_brazil_path(*a, **kw):
    raise FileNotFoundError("brazil-path not installed")


# ── Case 1: pre-bundled real directory ─────────────────────────────────────


def test_prebundled_real_dir_left_untouched(fake_pkg, monkeypatch):
    """Toolbox / manual install — real dir with index.html is a no-op."""
    tree_dist = fake_pkg / "static" / "dist"
    _make_dist(tree_dist)
    sentinel = tree_dist / "prebundled.marker"
    sentinel.write_text("toolbox")

    monkeypatch.setattr(subprocess, "run", _no_brazil_path)

    result = frontend.ensure_dev_dist_symlink()

    assert result == tree_dist
    assert not tree_dist.is_symlink()
    assert sentinel.read_text(encoding="utf-8") == "toolbox"


# ── Case 2: existing symlinks ──────────────────────────────────────────────


def test_valid_symlink_is_kept(fake_pkg, tmp_path, monkeypatch):
    """A symlink pointing at a valid dist stays as-is."""
    real_dist = _make_dist(tmp_path / "real-dist")
    tree_dist = fake_pkg / "static" / "dist"
    tree_dist.parent.mkdir(parents=True)
    # symlink on POSIX, directory junction on non-admin Windows.
    platform_compat.symlink_or_junction(str(real_dist), str(tree_dist))

    monkeypatch.setattr(subprocess, "run", _no_brazil_path)

    result = frontend.ensure_dev_dist_symlink()

    assert result == real_dist.resolve()
    assert platform_compat.is_link_or_junction(tree_dist)
    assert tree_dist.resolve() == real_dist.resolve()


def test_dangling_symlink_is_replaced_when_candidate_exists(fake_pkg, tmp_path, monkeypatch):
    """Stale link (target gone) gets repointed at a freshly-resolved dist."""
    dead_target = tmp_path / "gone"
    tree_dist = fake_pkg / "static" / "dist"
    tree_dist.parent.mkdir(parents=True)
    dead_target.mkdir()  # junction needs an existing target dir; removed next
    platform_compat.symlink_or_junction(str(dead_target), str(tree_dist))
    shutil.rmtree(dead_target)  # now dangling on both POSIX and Windows

    # Sibling checkout has a fresh dist — resolver should pick it up.
    sibling_dist = _make_dist(fake_pkg.parent.parent.parent / "KiroCrewWebsite" / "dist")

    monkeypatch.setattr(subprocess, "run", _no_brazil_path)

    result = frontend.ensure_dev_dist_symlink()

    assert result == sibling_dist.resolve()
    assert platform_compat.is_link_or_junction(tree_dist)
    assert tree_dist.resolve() == sibling_dist.resolve()


def test_dangling_symlink_with_no_candidate_returns_none(fake_pkg, tmp_path, monkeypatch):
    """Stale link + nothing to resolve → clean up and warn (returns None)."""
    tree_dist = fake_pkg / "static" / "dist"
    tree_dist.parent.mkdir(parents=True)
    gone = tmp_path / "also-gone"
    gone.mkdir()  # junction needs an existing target; removed to make it dangling
    platform_compat.symlink_or_junction(str(gone), str(tree_dist))
    shutil.rmtree(gone)

    monkeypatch.setattr(subprocess, "run", _no_brazil_path)

    assert frontend.ensure_dev_dist_symlink() is None
    # stale link was removed (both a POSIX symlink and a Windows junction).
    assert not platform_compat.is_link_or_junction(tree_dist)


def test_symlink_to_empty_dir_is_replaced(fake_pkg, tmp_path, monkeypatch):
    """Symlink target exists but has no index.html — treat as unusable."""
    empty_target = tmp_path / "empty-target"
    empty_target.mkdir()
    tree_dist = fake_pkg / "static" / "dist"
    tree_dist.parent.mkdir(parents=True)
    platform_compat.symlink_or_junction(str(empty_target), str(tree_dist))

    sibling_dist = _make_dist(fake_pkg.parent.parent.parent / "KiroCrewWebsite" / "dist")
    monkeypatch.setattr(subprocess, "run", _no_brazil_path)

    result = frontend.ensure_dev_dist_symlink()

    assert result == sibling_dist.resolve()
    assert tree_dist.resolve() == sibling_dist.resolve()


# ── Case 3: fresh resolution ───────────────────────────────────────────────


def test_sibling_checkout_is_symlinked(fake_pkg, monkeypatch):
    """Sibling KiroCrewWebsite/dist wins even when brazil-path is available."""
    sibling_dist = _make_dist(fake_pkg.parent.parent.parent / "KiroCrewWebsite" / "dist")

    # Should not be reached — sibling wins first.
    def _should_not_run(*a, **kw):
        raise AssertionError("brazil-path called despite sibling presence")

    monkeypatch.setattr(subprocess, "run", _should_not_run)

    result = frontend.ensure_dev_dist_symlink()
    tree_dist = fake_pkg / "static" / "dist"

    assert result == sibling_dist.resolve()
    assert platform_compat.is_link_or_junction(tree_dist)
    assert tree_dist.resolve() == sibling_dist.resolve()


def test_brazil_path_without_dist_subdir_is_skipped(fake_pkg, tmp_path, monkeypatch):
    """brazil-path returns a valid path but no dist/ inside → falls to None."""
    run_src = tmp_path / "run-src"
    run_src.mkdir()  # no dist/ child

    def _brazil_run(cmd, **kw):
        return subprocess.CompletedProcess(
            cmd, returncode=0, stdout=(str(run_src) + "\n").encode(), stderr=b""
        )

    monkeypatch.setattr(subprocess, "run", _brazil_run)

    assert frontend.ensure_dev_dist_symlink() is None


def test_brazil_path_timeout_is_swallowed(fake_pkg, monkeypatch):
    """A hung brazil-path shouldn't block gateway startup."""

    def _timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="brazil-path", timeout=10)

    monkeypatch.setattr(subprocess, "run", _timeout)

    assert frontend.ensure_dev_dist_symlink() is None


def test_brazil_path_empty_stdout_is_rejected(fake_pkg, monkeypatch):
    """Empty/whitespace stdout must not degrade to a cwd-relative ``Path('dist')``.

    Without the guard, ``Path("") / "dist" == Path("dist")`` — a relative
    path that ``is_dir()`` checks against the gateway's cwd, which could
    coincidentally match an unrelated local ``dist/`` directory.
    """

    def _empty_out(cmd, **kw):
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=b"   \n", stderr=b"")

    monkeypatch.setattr(subprocess, "run", _empty_out)

    assert frontend.ensure_dev_dist_symlink() is None


def test_brazil_path_relative_stdout_is_rejected(fake_pkg, monkeypatch):
    """Any non-absolute path from brazil-path is treated as untrusted."""

    def _relative(cmd, **kw):
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=b"relative/path\n", stderr=b"")

    monkeypatch.setattr(subprocess, "run", _relative)

    assert frontend.ensure_dev_dist_symlink() is None


def test_no_sibling_no_brazil_returns_none(fake_pkg, monkeypatch):
    """Fresh clone with nothing set up — caller sees None and warns."""
    monkeypatch.setattr(subprocess, "run", _no_brazil_path)

    assert frontend.ensure_dev_dist_symlink() is None
    assert not (fake_pkg / "static" / "dist").exists()


# ── Case 4: empty real directory fallback ──────────────────────────────────


def test_empty_real_dir_is_replaced_when_candidate_exists(fake_pkg, monkeypatch):
    """A real dir with no index.html is unusable — replace with a link."""
    tree_dist = fake_pkg / "static" / "dist"
    tree_dist.mkdir(parents=True)  # empty — no index.html

    sibling_dist = _make_dist(fake_pkg.parent.parent.parent / "KiroCrewWebsite" / "dist")
    monkeypatch.setattr(subprocess, "run", _no_brazil_path)

    result = frontend.ensure_dev_dist_symlink()

    assert result == sibling_dist.resolve()
    assert platform_compat.is_link_or_junction(tree_dist)


# ── Regression: the existing pwa_file symlink test still passes ────────────


def test_resolver_produces_a_symlink_the_pwa_guard_accepts(fake_pkg, tmp_path, monkeypatch):
    """The pwa_file handler (dashboard/handlers/core.py) rejects paths whose
    resolved target lies outside ``_DIST_DIR.resolve()``. This test verifies
    the new resolver still produces the symlink shape that test already
    guarantees — a symlink where ``resolve()`` on both sides yields equal
    prefixes.
    """
    _ = tmp_path  # unused — fake_pkg is the layout we need
    sibling_dist = _make_dist(fake_pkg.parent.parent.parent / "KiroCrewWebsite" / "dist")
    (sibling_dist / "pcm-worklet.js").write_text("// worklet")
    monkeypatch.setattr(subprocess, "run", _no_brazil_path)

    result = frontend.ensure_dev_dist_symlink()
    assert result is not None

    tree_dist = fake_pkg / "static" / "dist"
    asset = tree_dist / "pcm-worklet.js"

    assert asset.is_file()  # walked through the symlink
    assert tree_dist.resolve() in asset.resolve().parents


# ── npm resolution on Windows (npm.CMD) ────────────────────────────────────


def test_build_frontend_sync_spawns_resolved_npm_path(tmp_path, monkeypatch):
    """Regression: on Windows npm is ``npm.CMD``; CreateProcess cannot spawn the
    bare name "npm". build_frontend_sync must spawn the RESOLVED path.
    """
    website = tmp_path / "website"
    website.mkdir()
    (website / "package.json").write_text("{}")
    fake_npm = r"C:\node\npm.CMD"

    monkeypatch.setattr(frontend.shutil, "which", lambda name: fake_npm)
    monkeypatch.setattr(frontend, "_stage_dist", lambda *a, **k: None)

    calls: list[list[str]] = []

    class _Result:
        returncode = 0

    def _fake_run(cmd, **kw):
        calls.append(cmd)
        return _Result()

    monkeypatch.setattr(frontend.subprocess, "run", _fake_run)
    frontend.build_frontend_sync(tmp_path, log=lambda *a: None)

    assert calls, "no subprocess was spawned"
    # Every spawned command uses the resolved npm path as argv[0], never "npm".
    for cmd in calls:
        assert cmd[0] == fake_npm
        assert cmd[0] != "npm"


@pytest.mark.asyncio
async def test_build_frontend_async_spawns_resolved_npm_path(tmp_path, monkeypatch):
    """Async sibling of the sync npm-resolution regression."""
    website = tmp_path / "website"
    website.mkdir()
    (website / "package.json").write_text("{}")
    fake_npm = r"C:\node\npm.CMD"

    monkeypatch.setattr(frontend.shutil, "which", lambda name: fake_npm)
    monkeypatch.setattr(frontend, "_stage_dist", lambda *a, **k: None)

    calls: list[str] = []

    class _Proc:
        returncode = 0

        async def wait(self):
            return 0

    async def _fake_exec(program, *args, **kw):
        calls.append(program)
        return _Proc()

    monkeypatch.setattr(frontend.asyncio, "create_subprocess_exec", _fake_exec)
    await frontend.build_frontend_async(str(tmp_path))

    assert calls, "no subprocess was spawned"
    for program in calls:
        assert program == fake_npm
        assert program != "npm"
