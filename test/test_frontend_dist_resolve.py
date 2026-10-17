"""Tests for ``kiro_crew.frontend.ensure_dev_dist_symlink``.

Covers the runtime dist-resolution contract described:

* pre-bundled real directory is left alone (packaged install / prior build)
* valid symlink is kept
* a dangling / index-less symlink is replaced, unless it points at this
  checkout's ``website/dist`` outside an edition (kept until the build lands)
* sibling ``KiroCrewWebsite/dist`` is resolved and symlinked
* nothing-found returns ``None`` (caller logs warning and serves legacy UI); a
  stock checkout still links its unbuilt ``website/dist``
"""

from __future__ import annotations

import contextlib
import errno
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from conftest import make_dir_link
from kiro_crew import atomic_write, frontend, platform_compat


def _fake_kiro_crew_package(root: Path) -> Path:
    """Build the minimal directory shape the resolver walks."""
    pkg = root / "src" / "KiroCrew" / "src" / "kiro_crew"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    return pkg


def _index_html(chunk: str) -> str:
    # Mirror a real Vite index: a hashed chunk under /assets plus a
    # route-served reference (/manifest.js) that is NOT a file in the bundle.
    return (
        "<!doctype html><html><head>"
        '<script type="module" src="/manifest.js"></script>'
        f'<script type="module" src="/assets/{chunk}"></script>'
        "</head><body></body></html>"
    )


def _make_dist(path: Path, chunk: str = "main-abc123.js") -> Path:
    path.mkdir(parents=True)
    (path / "index.html").write_text(_index_html(chunk))
    (path / "assets").mkdir(exist_ok=True)
    (path / "assets" / chunk).write_text("console.log(1)")
    return path


def _rebuild_in_place(built: Path, chunk: str, emptied=lambda: None) -> None:
    """Empty ``built`` and write a new build there, as a Vite build in place does.

    ``emptied`` runs while ``built`` is gone, where a request mid-build lands.

    The new ``index.html`` is stamped a second past the old one. A real build
    takes far longer than a filesystem timestamp tick, but this one takes
    microseconds, and the deleted index's inode may be handed straight back, so
    without the stamp the build's ``(st_mtime_ns, st_ino)`` can equal the old
    one's and read as "published nothing".
    """
    before = (built / "index.html").stat().st_mtime_ns
    shutil.rmtree(built)
    emptied()
    _make_dist(built, chunk=chunk)
    later = before + 1_000_000_000
    os.utime(built / "index.html", ns=(later, later))


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


@pytest.mark.parametrize("left_behind", [False, True], ids=["nothing", "an-empty-real-dir"])
def test_a_gateway_started_before_the_first_build_serves_it_once_it_lands(
    fake_pkg, monkeypatch, left_behind
):
    """A stock checkout with nothing built links its website/dist anyway.

    Start still answers ``None`` (nothing to serve yet, so the caller warns), but
    the link is in place, so the first build is served without a restart. An
    empty real ``static/dist`` left by an earlier run is replaced the same way.
    Listed in ``test/requires-real-symlinks.txt``: a junction needs its target.
    """
    website = _unbuilt_checkout(fake_pkg, monkeypatch)
    served = fake_pkg / "static" / "dist"
    if left_behind:
        served.mkdir(parents=True)

    assert frontend.ensure_dev_dist_symlink() is None

    assert frontend._links_to_website_dist(served, website.parent), "no link to website/dist"
    _make_dist(website / "dist")
    assert frontend._incomplete_bundle_reason(served) == ""
    assert frontend.ensure_dev_dist_symlink() == (website / "dist").resolve()


def test_an_unbuilt_checkout_that_cannot_be_linked_starts_as_before(fake_pkg, monkeypatch):
    """A platform that cannot link a missing directory leaves static/dist absent."""
    _unbuilt_checkout(fake_pkg, monkeypatch)

    def _refuse(_target, _link):
        raise OSError(errno.ENOENT, "a junction needs an existing target")

    monkeypatch.setattr(platform_compat, "symlink_or_junction", _refuse)

    assert frontend.ensure_dev_dist_symlink() is None
    assert not os.path.lexists(fake_pkg / "static" / "dist")


def test_an_edition_does_not_link_an_unbuilt_website_dist(fake_pkg, monkeypatch):
    """Under an edition a later stock build must never be served, so no link is made."""
    _unbuilt_checkout(fake_pkg, monkeypatch)
    monkeypatch.setenv("KIROCREW_EDITION_DIR", str(fake_pkg / "edition"))

    assert frontend.ensure_dev_dist_symlink() is None
    assert not os.path.lexists(fake_pkg / "static" / "dist")


def _unbuilt_checkout(fake_pkg: Path, monkeypatch) -> Path:
    """A checkout with a ``website/`` that has never been built; returns it."""
    website = fake_pkg.parent.parent / "website"
    website.mkdir()
    monkeypatch.setattr(subprocess, "run", _no_brazil_path)
    return website


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

        def __init__(self, cmd, **kw):
            calls.append(cmd)

        def communicate(self, timeout=None):
            return (b"", b"")

        def wait(self, timeout=None):
            return 0

    # The install moved from subprocess.run to Popen -- run never exposes a pid,
    # so only the direct child could be signalled on timeout, leaving npm's
    # descendants writing into the dependency tree while it is restored. Both the
    # install and the build are now recorded through this one seam.
    monkeypatch.setattr(frontend.subprocess, "Popen", _Result)
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


# ── stage_built_dist: re-point static/dist at the new bundle ────────────────
#
# Staging only ever re-points the static/dist link: it keeps (or makes) the dev
# link to website/dist for a stock build that publishes atomically, and copies
# any other build (an edition bundle, an older target revision that builds in
# place) into a fresh static/.dist.<id> for the link to name.


def _repo_with_build(root: Path) -> Path:
    """Minimal repo shape: src/kiro_crew/ plus a built website/dist."""
    (root / "src" / "kiro_crew" / "static").mkdir(parents=True)
    built = _make_dist(root / "website" / "dist")
    (built / "assets" / "app-abc123.js").write_text("console.log(1)")
    return built


def test_stage_built_dist_refreshes_an_existing_real_dir(tmp_path):
    """Re-staging overwrites an already-staged tree instead of merging it."""
    _repo_with_build(tmp_path)
    static_dist = tmp_path / "src" / "kiro_crew" / "static" / "dist"
    static_dist.mkdir()
    (static_dist / "index.html").write_text("stale")
    (static_dist / "old-hashed-chunk.js").write_text("stale")

    assert frontend._stage_dist(tmp_path / "website" / "dist", tmp_path) is True

    assert (static_dist / "index.html").read_text() != "stale"
    # Vite emits content-hashed names; a merge would keep serving stale chunks.
    assert not (static_dist / "old-hashed-chunk.js").exists()


def test_stage_built_dist_reports_failure_when_build_missing(tmp_path):
    """No build output → False, so Dev Fleet's strict step fails the sync."""
    (tmp_path / "src" / "kiro_crew" / "static").mkdir(parents=True)
    assert frontend._stage_dist(tmp_path / "website" / "dist", tmp_path) is False


def test_stage_built_dist_keeps_serving_when_copy_fails(tmp_path):
    """A failed copy must leave the already-staged tree in place.

    Staging runs against a live gateway, so a mid-stage error may not take the
    served assets down with it.
    """
    built = _repo_with_build(tmp_path)
    static_dist = tmp_path / "src" / "kiro_crew" / "static" / "dist"
    static_dist.mkdir()
    (static_dist / "index.html").write_text("previously staged")

    with patch.object(frontend.shutil, "copytree", side_effect=OSError("ENOSPC")):
        assert frontend._stage_dist(tmp_path / "website" / "dist", tmp_path) is False

    assert (static_dist / "index.html").read_text() == "previously staged"
    assert _staged_entries(static_dist.parent) == [], "a partial copy was left behind"
    assert built.is_dir()


def test_stage_built_dist_sweeps_abandoned_staging_dirs(tmp_path):
    """Residue from a killed run is removed, not left to dirty the checkout.

    An untracked staging tree makes the checkout read as permanently dirty,
    which fail-closes Dev Fleet's prune.
    """
    _repo_with_build(tmp_path)
    static_parent = tmp_path / "src" / "kiro_crew" / "static"
    static_parent.mkdir(parents=True, exist_ok=True)
    orphan = static_parent / ".dist.staging.abandoned"
    orphan.mkdir()
    (orphan / "half-copied.js").write_text("x")

    assert frontend._stage_dist(tmp_path / "website" / "dist", tmp_path) is True

    assert not orphan.exists()
    served = static_parent / "dist"
    assert _staged_entries(static_parent) == [served.resolve().name]


def test_concurrent_staging_does_not_destroy_the_served_tree(tmp_path):
    """Two overlapping stagers must not leave static/dist missing.

    The sweep cannot tell an abandoned tree from one a concurrent run is still
    filling, so sweep/copy/swap is serialized across processes. Without that
    exclusion the second run deletes the first's staging tree, and the first
    then removes the old dist and fails its swap — serving nothing.

    Mutual exclusion is asserted directly rather than inferred from four threads
    happening to overlap: the critical section is instrumented so a scheduler
    that serialized them anyway could not hide a missing lock.
    """
    _repo_with_build(tmp_path)
    static_dist = tmp_path / "src" / "kiro_crew" / "static" / "dist"
    _make_dist(static_dist, chunk="served-0.js")  # a staged copy, as installs have

    workers = 4
    real_file_lock = frontend.platform_compat.file_lock
    real_locked = frontend._stage_dist_locked
    bookkeeping = threading.Lock()
    attempted = 0
    all_attempted = threading.Event()
    inside = 0
    max_inside = 0
    entered = threading.Event()

    @contextlib.contextmanager
    def _counting_lock(fd, **kwargs):
        # Count the ATTEMPT before delegating, so peers blocked on a working
        # lock still register here. This is what lets the first entrant know
        # every peer has reached the acquisition boundary.
        nonlocal attempted
        with bookkeeping:
            attempted += 1
            if attempted >= workers:
                all_attempted.set()
        with real_file_lock(fd, **kwargs):
            yield

    def _instrumented(*args, **kwargs):
        nonlocal inside, max_inside
        with bookkeeping:
            inside += 1
            max_inside = max(max_inside, inside)
        entered.set()
        # Hold the critical section open until every peer has tried to acquire
        # the lock. Without a real lock they are all inside by now, so
        # max_inside records it; elapsed time is never the overlap guarantee.
        all_attempted.wait(timeout=10)
        try:
            return real_locked(*args, **kwargs)
        finally:
            with bookkeeping:
                inside -= 1

    results: list[bool] = []
    errors: list[BaseException] = []

    def _stage() -> None:
        try:
            results.append(frontend._stage_dist(tmp_path / "website" / "dist", tmp_path))
        except BaseException as exc:  # noqa: BLE001 - surfaced via assert below
            errors.append(exc)

    with patch.object(frontend.platform_compat, "file_lock", _counting_lock), \
            patch.object(frontend, "_stage_dist_locked", _instrumented):
        threads = [threading.Thread(target=_stage) for _ in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

    assert not errors, errors
    assert entered.is_set(), "the critical section never ran"
    assert all_attempted.is_set(), f"only {attempted}/{workers} reached the lock"
    assert max_inside == 1, f"{max_inside} stagers were inside the lock at once"
    assert results == [True] * workers
    # The decisive property: a served tree exists and is complete, and the
    # copies the earlier stagers made were swept, not left behind.
    assert frontend._incomplete_bundle_reason(static_dist) == ""
    assert (static_dist / "assets" / "app-abc123.js").is_file()
    assert _staged_entries(static_dist.parent) == [static_dist.resolve().name]


def test_stage_built_dist_refuses_a_source_without_index(tmp_path):
    """An emptied website/dist must not be published over a good bundle.

    An in-place build (an older target revision, ``--watch``, a mount-point
    outDir) empties its outDir before repopulating it, and that build is not
    under the staging lock, so a peer flow's rebuild can be seen mid-flight.
    """
    (tmp_path / "src" / "kiro_crew" / "static").mkdir(parents=True)
    mid_rebuild = tmp_path / "website" / "dist"
    mid_rebuild.mkdir(parents=True)  # exists, but Vite has not written index.html yet
    static_dist = tmp_path / "src" / "kiro_crew" / "static" / "dist"
    static_dist.mkdir()
    (static_dist / "index.html").write_text("last good bundle")

    assert frontend._stage_dist(tmp_path / "website" / "dist", tmp_path) is False

    assert (static_dist / "index.html").read_text() == "last good bundle"


def test_stage_built_dist_refuses_when_source_is_emptied_mid_copy(tmp_path, capsys):
    """A source that loses index.html DURING the copy must not be published.

    The pre-copy check cannot see this: the race is a peer `npm run build`
    emptying the tree while copytree reads it.
    """
    built = _repo_with_build(tmp_path)
    static_dist = tmp_path / "src" / "kiro_crew" / "static" / "dist"
    static_dist.mkdir()
    (static_dist / "index.html").write_text("last good bundle")

    real_copytree = frontend.shutil.copytree

    def _copy_then_lose_index(src, dst, *args, **kwargs):
        result = real_copytree(src, dst, *args, **kwargs)
        # copytree recurses through this same patched name, so only mutate the
        # top-level staging tree, and only once its whole copy has landed.
        idx = Path(dst) / "index.html"
        if Path(dst).name.startswith(".dist.") and idx.is_file():
            idx.unlink()
        return result

    with patch.object(frontend.shutil, "copytree", _copy_then_lose_index):
        assert frontend._stage_dist(tmp_path / "website" / "dist", tmp_path) is False

    # Pin WHICH guard refused: without this a future refactor that failed
    # earlier (e.g. a copy error) would keep the test green.
    assert "Staged copy is incomplete" in capsys.readouterr().out
    assert (static_dist / "index.html").read_text() == "last good bundle"
    assert built.is_dir()
    assert _staged_entries(static_dist.parent) == []


def test_stage_built_dist_refuses_when_a_referenced_chunk_is_missing(tmp_path):
    """index.html alone is not completeness: its /assets chunks must exist.

    Rollup writes the entry document and its hashed chunks separately, so a tree
    copied out from under a concurrent build can carry an index whose chunks are
    absent. Publishing it yields a shell where every chunk 404s.
    """
    built = _repo_with_build(tmp_path)
    (built / "assets" / "main-abc123.js").unlink()  # index still references it
    static_dist = tmp_path / "src" / "kiro_crew" / "static" / "dist"
    static_dist.mkdir()
    (static_dist / "index.html").write_text("last good bundle")

    assert frontend._stage_dist(tmp_path / "website" / "dist", tmp_path) is False

    assert (static_dist / "index.html").read_text() == "last good bundle"


def test_incomplete_bundle_reason_ignores_route_served_references(tmp_path):
    """A reference the GATEWAY serves by route is not a missing bundle file.

    Real index.html carries `/manifest.js`, which is served by a handler rather
    than emitted into dist. Treating it as missing would refuse every stage.
    """
    complete = _make_dist(tmp_path / "dist")
    assert '/manifest.js' in (complete / "index.html").read_text()
    assert not (complete / "manifest.js").exists()

    assert frontend._incomplete_bundle_reason(complete) == ""


def test_incomplete_bundle_reason_checks_sub_path_build_refs(tmp_path):
    """A KIROCREW_BASE_PATH build writes /<base>/assets/...; its chunks still count.

    Without matching the base, such an index has no refs at all and a tree with
    every chunk missing would pass as complete.
    """
    built = tmp_path / "dist"
    built.mkdir()
    (built / "assets").mkdir()
    (built / "index.html").write_text(
        '<script type="module" src="/proxy/dash/assets/main-abc123.js"></script>'
    )
    assert "1 referenced asset(s) missing" in frontend._incomplete_bundle_reason(built)

    (built / "assets" / "main-abc123.js").write_text("console.log(1)")
    assert frontend._incomplete_bundle_reason(built) == ""


def test_stage_built_dist_sweeps_residue_even_when_refusing(tmp_path):
    """Refusing an unusable source must still clear abandoned staging trees.

    The refusal happens under the lock, after the sweep, so a checkout cannot
    accumulate ~30 MB trees that fail-close Dev Fleet's prune just because the
    source was mid-rebuild each time.
    """
    (tmp_path / "src" / "kiro_crew" / "static").mkdir(parents=True)
    (tmp_path / "website" / "dist").mkdir(parents=True)  # no index.html -> refused
    static_parent = tmp_path / "src" / "kiro_crew" / "static"
    orphan = static_parent / ".dist.staging.abandoned"
    orphan.mkdir()
    (orphan / "half-copied.js").write_text("x")

    assert frontend._stage_dist(tmp_path / "website" / "dist", tmp_path) is False

    assert not orphan.exists(), "residue survived a refused stage"


def test_build_and_stage_holds_the_lock_across_the_build(tmp_path):
    """The lock must be held while `npm run build` runs, not just while copying.

    The build swaps a new tree into website/dist, so a peer holding only the
    copy could copy half of each — and lazy chunks are unreachable from
    index.html, so no inspection of the copy detects that reliably.
    """
    (tmp_path / "src" / "kiro_crew" / "static").mkdir(parents=True)
    website = tmp_path / "website"
    website.mkdir()
    lock_held_during_build = {"value": False}

    def _fake_build(*args, **kwargs):
        # A peer process would block here; probe it without blocking ourselves.
        lock_path = tmp_path / "src" / "kiro_crew" / "static" / ".dist.staging.lock"
        with open(lock_path, "a+") as probe:
            lock_held_during_build["value"] = not frontend.platform_compat.try_acquire_lock(
                probe.fileno(), exclusive=True
            )
        _make_dist(website / "dist")  # the build produces the bundle

        class _Done:
            pid = 1234
            returncode = 0

            def wait(self, timeout=None):
                return 0

        return _Done()

    with patch.object(frontend.subprocess, "Popen", _fake_build):
        assert frontend.build_and_stage(tmp_path, npm="/usr/bin/true") is True

    assert lock_held_during_build["value"], "the build ran without the staging lock"
    staged = tmp_path / "src" / "kiro_crew" / "static" / "dist"
    assert (staged / "index.html").is_file()


def test_build_and_stage_reports_a_failed_build(tmp_path):
    """A non-zero build must not publish anything and must return False."""
    (tmp_path / "src" / "kiro_crew" / "static").mkdir(parents=True)
    (tmp_path / "website").mkdir()
    static_dist = tmp_path / "src" / "kiro_crew" / "static" / "dist"
    static_dist.mkdir()
    (static_dist / "index.html").write_text("last good bundle")

    class _Failed:
        pid = 1235
        returncode = 1

        def wait(self, timeout=None):
            return 1

    with patch.object(frontend.subprocess, "Popen", lambda *a, **kw: _Failed()):
        assert frontend.build_and_stage(tmp_path, npm="/usr/bin/false") is False

    assert (static_dist / "index.html").read_text() == "last good bundle"


class _Finished:
    """A build process that already exited 0."""

    returncode = 0

    def wait(self, timeout=None):
        return 0


#: The real publisher, which a checkout carrying it publishes every build with.
_PUBLISH_DIST = Path(__file__).resolve().parents[1] / "website" / "scripts" / "publish-dist.mjs"
#: The completeness cases both gates are checked against.
_COMPLETENESS_CASES = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "dist_completeness_cases.json").read_text(
        encoding="utf-8"
    )
)["cases"]


def _atomic_checkout(root: Path) -> Path:
    """A repo whose website/ publishes atomically, with a built website/dist."""
    built = _repo_with_build(root)
    scripts = root / "website" / "scripts"
    scripts.mkdir()
    shutil.copy2(_PUBLISH_DIST, scripts / "publish-dist.mjs")
    return built


def _dev_linked(root: Path) -> tuple[Path, Path]:
    """An atomic checkout whose static/dist is the dev link to website/dist."""
    built = _atomic_checkout(root)
    served = root / "src" / "kiro_crew" / "static" / "dist"
    make_dir_link(served, built)  # a junction on Windows
    return built, served


def _staged_entries(static_parent: Path) -> list[str]:
    """Every staging entry beside static/dist except the lock."""
    return sorted(
        p.name
        for p in static_parent.iterdir()
        if p.name.startswith(".dist.") and p.name != ".dist.staging.lock"
    )


def _serves_a_staged_copy(served: Path) -> bool:
    """Whether static/dist links to one of the immutable copies beside it."""
    return platform_compat.is_link_or_junction(served) and frontend._is_staged_tree(
        served.resolve(), served
    )


def _npm_build(build):
    """A ``Popen`` that runs ``build`` for the npm build and the real thing for git."""
    real_popen = subprocess.Popen

    def _popen(argv, *args, **kwargs):
        if argv[0] != "/usr/bin/true":  # the fingerprint's git calls
            return real_popen(argv, *args, **kwargs)
        build()
        return _Finished()

    return _popen


def _run_bounded(call, timeout: float = 5.0):
    """Run ``call`` on a thread and fail by name if it does not return in time.

    A lock wait that regressed to the production ceiling would otherwise block
    the worker far past pytest's timeout.
    """
    result: list = []
    worker = threading.Thread(target=lambda: result.append(call()), daemon=True)
    worker.start()
    worker.join(timeout=timeout)
    assert not worker.is_alive(), f"{call} blocked for more than {timeout}s"
    return result[0] if result else None


def _node_that_runs_publish_dist() -> str:
    """A ``node`` new enough for website/ (engines.node), or skip by name."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    floor = json.loads((_PUBLISH_DIST.parents[1] / "package.json").read_text())["engines"]["node"]
    want = tuple(int(n) for n in re.findall(r"\d+", floor)[:3])
    out = subprocess.run([node, "--version"], capture_output=True, encoding="utf-8", timeout=30)
    have = tuple(int(n) for n in re.findall(r"\d+", out.stdout)[:3])
    if have < want:
        pytest.skip(f"node {out.stdout.strip()} is older than website/'s {floor}")
    return node


@pytest.fixture(autouse=True)
def _clean_edition_env(monkeypatch):
    """An edition env changes which path staging takes; tests opt in explicitly."""
    monkeypatch.delenv("KIROCREW_EDITION_DIR", raising=False)
    monkeypatch.delenv("KIROCREW_ALLOW_EDITION", raising=False)


def test_staging_keeps_the_dev_link(tmp_path):
    """A static/dist linked at an atomically published build: nothing to copy."""
    built, served = _dev_linked(tmp_path)

    assert frontend._stage_dist(built, tmp_path, lambda _m: None) is True

    assert served.resolve() == built.resolve(), "the dev link was replaced"
    assert _staged_entries(served.parent) == []


def test_a_stage_with_nothing_to_copy_never_waits_on_the_lock(tmp_path):
    """The dev-link check needs no lock, so a held lock cannot fail or stall it."""
    built, served = _dev_linked(tmp_path)

    with frontend._staging_lock(served.parent):
        assert _run_bounded(lambda: frontend._stage_dist(built, tmp_path, lambda _m: None))


def test_a_dev_link_stage_sweeps_residue_when_the_lock_is_free(tmp_path):
    """make and the CLI take the lock-free return; residue there is still swept."""
    built, served = _dev_linked(tmp_path)
    orphan = served.parent / ".dist.1-killed"
    _make_dist(orphan)

    assert frontend._stage_dist(built, tmp_path, lambda _m: None) is True

    assert _staged_entries(served.parent) == []


def test_staging_links_a_fresh_checkout_instead_of_copying(tmp_path):
    """Nothing served yet: link, so hand builds go live."""
    built = _atomic_checkout(tmp_path)
    served = tmp_path / "src" / "kiro_crew" / "static" / "dist"

    assert frontend._stage_dist(built, tmp_path, lambda _m: None) is True

    assert platform_compat.is_link_or_junction(served)
    assert served.resolve() == built.resolve()


@pytest.mark.parametrize("occupant", ["real copy", "link elsewhere", "dangling link"])
def test_staging_re_points_any_occupant_at_an_atomic_build(tmp_path, occupant):
    """A copy staged by an older revision, a link to another tree, a dead link: all become the dev link.

    Safe for a running gateway because every route resolves static/dist per
    request; whatever was there before is swept.
    """
    built = _atomic_checkout(tmp_path)
    served = tmp_path / "src" / "kiro_crew" / "static" / "dist"
    if occupant == "real copy":
        _make_dist(served, chunk="old-1.js")
    elif occupant == "link elsewhere":
        make_dir_link(served, _make_dist(tmp_path / "KiroCrewWebsite" / "dist", chunk="other-9.js"))
    else:
        gone = tmp_path / "gone"
        gone.mkdir()
        make_dir_link(served, gone)
        gone.rmdir()

    assert frontend._stage_dist(built, tmp_path, lambda _m: None) is True

    assert served.resolve() == built.resolve()
    assert _staged_entries(served.parent) == []


def test_staging_copies_where_the_build_does_not_publish_atomically(tmp_path):
    """An older target revision builds in place, so it is served from an immutable copy."""
    built = _repo_with_build(tmp_path)  # no website/scripts/publish-dist.mjs
    served = tmp_path / "src" / "kiro_crew" / "static" / "dist"
    make_dir_link(served, built)

    assert frontend._stage_dist(built, tmp_path, lambda _m: None) is True

    assert _serves_a_staged_copy(served)
    assert frontend._incomplete_bundle_reason(served) == ""
    shutil.rmtree(built)  # the next in-place build empties website/dist
    assert frontend._incomplete_bundle_reason(served) == ""


def test_restaging_a_copy_sweeps_the_copy_it_replaced(tmp_path):
    """Each copy is immutable and fresh; the one not served is removed."""
    built = _repo_with_build(tmp_path)
    served = tmp_path / "src" / "kiro_crew" / "static" / "dist"

    assert frontend._stage_dist(built, tmp_path, lambda _m: None) is True
    first = served.resolve()
    (built / "assets" / "app-abc123.js").write_text("console.log(2)")
    assert frontend._stage_dist(built, tmp_path, lambda _m: None) is True

    assert served.resolve() != first
    assert _staged_entries(served.parent) == [served.resolve().name]
    assert (served / "assets" / "app-abc123.js").read_text() == "console.log(2)"


def test_restaging_an_in_place_target_says_to_restart_the_gateway_that_pinned_the_copy(tmp_path):
    """That target's gateway resolved the copy once at start, and the sweep just removed it."""
    built = _repo_with_build(tmp_path)  # no website/scripts/publish-dist.mjs
    served = tmp_path / "src" / "kiro_crew" / "static" / "dist"
    make_dir_link(served, built)
    lines: list[str] = []

    assert frontend._stage_dist(built, tmp_path, lines.append) is True
    assert not any("restart a gateway" in line for line in lines), lines

    lines.clear()
    assert frontend._stage_dist(built, tmp_path, lines.append) is True
    assert any("restart a gateway" in line for line in lines), lines


def test_an_edition_restage_of_an_atomic_revision_needs_no_restart(tmp_path, monkeypatch):
    """Its own gateway resolves static/dist per request, so a swept copy costs it nothing."""
    built, _served = _dev_linked(tmp_path)
    monkeypatch.setenv("KIROCREW_EDITION_DIR", str(tmp_path / "edition"))
    lines: list[str] = []

    assert frontend._stage_dist(built, tmp_path, lambda _m: None) is True
    assert frontend._stage_dist(built, tmp_path, lines.append) is True

    assert not any("restart a gateway" in line for line in lines), lines


def test_a_staged_copy_is_ignored_under_an_older_revision_s_gitignore(tmp_path):
    """An older target's .gitignore does not name .dist.<id>; the copy must not dirty it."""
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not on PATH")
    built = _repo_with_build(tmp_path)  # an older target: builds in place
    (tmp_path / ".gitignore").write_text(  # the rules a revision before .dist.<id> carries
        "website/dist/\nsrc/kiro_crew/static/dist\n"
        "src/kiro_crew/static/.dist.staging.*\nsrc/kiro_crew/static/.dist.previous.*\n",
        encoding="utf-8",
    )
    subprocess.run([git, "init", "-q"], cwd=str(tmp_path), check=True)

    assert frontend._stage_dist(built, tmp_path, lambda _m: None) is True
    served = tmp_path / "src" / "kiro_crew" / "static" / "dist"
    assert _serves_a_staged_copy(served)

    untracked = subprocess.run(
        [git, "-C", str(tmp_path), "ls-files", "--others", "--exclude-standard"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.split()
    assert untracked == [".gitignore"], untracked


def test_an_edition_bundle_is_staged_as_a_private_copy(tmp_path, monkeypatch):
    """A later stock build in the same checkout must not replace an edition dashboard."""
    built, served = _dev_linked(tmp_path)
    monkeypatch.setenv("KIROCREW_EDITION_DIR", str(tmp_path / "edition"))

    assert frontend._stage_dist(built, tmp_path, lambda _m: None) is True
    shutil.rmtree(built)
    _make_dist(built, chunk="stock-7.js")  # a stock build published afterwards

    assert _serves_a_staged_copy(served)
    assert (served / "assets" / "app-abc123.js").is_file(), "the edition bundle was replaced"


def test_a_failed_re_point_leaves_the_served_tree_in_place(tmp_path):
    """A real static/dist retired for the link is put back if the link cannot land."""
    built = _repo_with_build(tmp_path)
    served = _make_dist(tmp_path / "src" / "kiro_crew" / "static" / "dist", chunk="old-1.js")
    real_replace = frontend.replace_with_retry

    def _refuse_the_link(src, dst):
        if Path(src).name.startswith(".dist.link-"):
            raise OSError(errno.EXDEV, "refused", str(dst))
        return real_replace(src, dst)

    with patch.object(frontend, "replace_with_retry", _refuse_the_link):
        assert frontend._stage_dist(built, tmp_path, lambda _m: None) is False

    assert not platform_compat.is_link_or_junction(served)
    assert (served / "assets" / "old-1.js").is_file(), "the served bundle was not restored"
    assert _staged_entries(served.parent) == []


def test_a_failed_re_point_retries_the_rollback_rename(tmp_path, monkeypatch):
    """The rollback rides the same Windows rename-window retry as the forward moves.

    ``os.replace`` on Windows refuses a path another handle holds (an indexer or
    AV scanner on the just-retired tree). The forward moves absorb that through
    ``replace_with_retry``; a bare ``os.replace`` on the rollback was suppressed,
    leaving ``static/dist`` absent and the retired bundle to the next sweep.
    """
    built = _repo_with_build(tmp_path)
    served = _make_dist(tmp_path / "src" / "kiro_crew" / "static" / "dist", chunk="old-1.js")
    real_replace = os.replace
    contended: list[str] = []

    class _AsOnWindows:
        IS_WINDOWS = True

        def __getattr__(self, name):
            return getattr(platform_compat, name)

    def _replace(src, dst):
        if Path(src).name.startswith(".dist.link-"):
            raise OSError(errno.EXDEV, "refused", str(dst))
        if Path(dst) == served and not contended:  # the rollback, held once
            contended.append(str(src))
            raise PermissionError(errno.EACCES, "sharing violation", str(dst))
        return real_replace(src, dst)

    monkeypatch.setattr(atomic_write, "platform_compat", _AsOnWindows())
    monkeypatch.setattr(atomic_write, "_REPLACE_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(os, "replace", _replace)
    assert frontend._stage_dist(built, tmp_path, lambda _m: None) is False

    assert len(contended) == 1 and Path(contended[0]).name.startswith(".dist.")
    assert not platform_compat.is_link_or_junction(served)
    assert (served / "assets" / "old-1.js").is_file(), "the served bundle was not restored"
    assert _staged_entries(served.parent) == []


def test_a_failed_re_point_leaves_the_link_in_place(tmp_path):
    """A link that cannot be replaced still serves what it served."""
    built = _repo_with_build(tmp_path)
    served = tmp_path / "src" / "kiro_crew" / "static" / "dist"
    make_dir_link(served, built)
    real_replace = frontend.replace_with_retry

    def _refuse_the_link(src, dst):
        if Path(src).name.startswith(".dist.link-"):
            raise OSError(errno.EXDEV, "refused", str(dst))
        return real_replace(src, dst)

    with patch.object(frontend, "replace_with_retry", _refuse_the_link):
        assert frontend._stage_dist(built, tmp_path, lambda _m: None) is False

    assert served.resolve() == built.resolve()
    assert _staged_entries(served.parent) == []


def test_a_dev_link_another_starter_made_first_is_kept(tmp_path):
    """Gateway start links without the lock; losing that race is success, never a copy."""
    built = _atomic_checkout(tmp_path)
    served = tmp_path / "src" / "kiro_crew" / "static" / "dist"
    logged: list[str] = []

    def _raced(static_dist, target):
        make_dir_link(static_dist, target)
        raise FileExistsError(errno.EEXIST, "exists", str(static_dist))

    with patch.object(frontend, "_point_static_dist_at", _raced):
        assert frontend._stage_dist(built, tmp_path, logged.append) is True

    assert served.resolve() == built.resolve()
    assert not any("copying instead" in m for m in logged), logged
    assert _staged_entries(served.parent) == []


def test_a_build_through_the_dev_link_keeps_the_served_page_whole(tmp_path, monkeypatch):
    """A gateway serving through the dev link sees a whole bundle across a real publish.

    The fake build writes a scratch sibling and runs the REAL publish-dist.mjs, as
    the Vite plugin does, and the watchdog's probe holds before and after.
    """
    node = _node_that_runs_publish_dist()
    from kiro_crew.dashboard import stale_asset_watchdog

    built, served = _dev_linked(tmp_path)
    monkeypatch.setattr(stale_asset_watchdog, "_DIST_INDEX", served / "index.html")
    seen: list[tuple[bool, str]] = []

    def _probe() -> None:
        seen.append((stale_asset_watchdog.assets_present(), frontend._incomplete_bundle_reason(served)))

    real_popen = subprocess.Popen
    module_url = (tmp_path / "website" / "scripts" / "publish-dist.mjs").as_uri()
    script = (
        f"import {{ publishDist }} from {module_url!r};"
        "publishDist({ next: process.env.NEXT, live: process.env.LIVE })"
    )

    def _popen(argv, *args, **kwargs):
        if argv[0] != "/usr/bin/true":  # the fingerprint's git calls
            return real_popen(argv, *args, **kwargs)
        scratch = _make_dist(tmp_path / "website" / ".dist.next-1-test", chunk="app-new456.js")
        _probe()
        env = {**os.environ, "NEXT": str(scratch), "LIVE": str(built)}
        node_run = subprocess.run(
            [node, "--input-type=module", "-e", script],
            env=env,
            cwd=str(tmp_path),
            capture_output=True,
            timeout=60,
        )
        assert node_run.returncode == 0, node_run.stderr
        return _Finished()

    with patch.object(frontend.subprocess, "Popen", _popen):
        published = frontend.build_and_stage(tmp_path, npm="/usr/bin/true", log=lambda _m: None)
    _probe()

    assert published is True
    assert seen == [(True, ""), (True, "")], seen
    assert served.resolve() == built.resolve(), "a stager replaced the dev link"
    assert (served / "assets" / "app-new456.js").is_file()


def test_a_build_that_writes_in_place_is_served_from_a_copy_meanwhile(tmp_path):
    """A revision whose build empties website/dist must not do it under the dev link.

    The served bundle moves to a copy before the build starts, so a request
    during the build still finds a whole bundle.
    """
    built = _repo_with_build(tmp_path)  # no publish-dist.mjs: builds in place
    served = tmp_path / "src" / "kiro_crew" / "static" / "dist"
    make_dir_link(served, built)
    during: list[str] = []

    def _in_place_build():
        _rebuild_in_place(
            built,
            chunk="app-new456.js",
            emptied=lambda: during.append(frontend._incomplete_bundle_reason(served)),
        )

    with patch.object(frontend.subprocess, "Popen", _npm_build(_in_place_build)):
        assert frontend.build_and_stage(tmp_path, npm="/usr/bin/true", log=lambda _m: None)

    assert during == [""], "the build emptied the served bundle"
    assert _serves_a_staged_copy(served)
    assert (served / "assets" / "app-new456.js").is_file()


def test_a_build_that_exits_0_without_publishing_is_not_staged(tmp_path):
    """Exit 0 alone is no proof: an untouched website/dist must not be reported as new."""
    _repo_with_build(tmp_path)
    logged: list[str] = []

    with patch.object(frontend.subprocess, "Popen", _npm_build(lambda: None)):
        assert frontend.build_and_stage(tmp_path, npm="/usr/bin/true", log=logged.append) is False

    assert any("published no new" in m for m in logged), logged


def test_a_published_build_counts_whatever_its_timestamp(tmp_path):
    """No clock is compared: a new index.html stamped an hour back (a skewed share) is new."""
    built = _repo_with_build(tmp_path)

    def _build():
        fresh = built.with_name(".dist.next-1-test")
        _make_dist(fresh, chunk="app-new456.js")
        old = time.time() - 3600
        os.utime(fresh / "index.html", (old, old))
        shutil.rmtree(built)
        fresh.rename(built)

    with patch.object(frontend.subprocess, "Popen", _npm_build(_build)):
        assert frontend.build_and_stage(tmp_path, npm="/usr/bin/true", log=lambda _m: None)


def test_gateway_start_does_not_wait_on_the_staging_lock(fake_pkg, monkeypatch):
    """Start only checks or creates the link: a build holding the lock costs it nothing."""
    _make_dist(fake_pkg.parent.parent / "website" / "dist")
    monkeypatch.setattr(subprocess, "run", _no_brazil_path)
    with frontend._staging_lock(fake_pkg / "static"):
        assert _run_bounded(frontend.ensure_dev_dist_symlink) is not None


def test_gateway_start_keeps_a_dangling_link_to_this_checkout_s_build(fake_pkg, monkeypatch):
    """Mid-publish or not built yet: the link stays, and serves the build once it lands."""
    built = _make_dist(fake_pkg.parent.parent / "website" / "dist")
    served = fake_pkg / "static" / "dist"
    served.parent.mkdir(parents=True)
    make_dir_link(served, built)
    aside = built.with_name(".dist.prev-test")
    built.rename(aside)
    monkeypatch.setattr(subprocess, "run", _no_brazil_path)

    assert frontend.ensure_dev_dist_symlink() is None

    assert platform_compat.is_link_or_junction(served), "the link was dropped"
    aside.rename(built)
    assert frontend._incomplete_bundle_reason(served) == ""


def test_an_edition_gateway_drops_a_dangling_link_to_this_checkout_s_build(fake_pkg, monkeypatch):
    """Under an edition no website/dist link survives start, so a later stock build is never served."""
    built = _make_dist(fake_pkg.parent.parent / "website" / "dist")
    served = fake_pkg / "static" / "dist"
    served.parent.mkdir(parents=True)
    make_dir_link(served, built)
    shutil.rmtree(built)
    monkeypatch.setenv("KIROCREW_EDITION_DIR", str(fake_pkg / "edition"))
    monkeypatch.setattr(subprocess, "run", _no_brazil_path)

    assert frontend.ensure_dev_dist_symlink() is None

    assert not os.path.lexists(served), "the link to website/dist was kept"
    _make_dist(built, chunk="stock-7.js")
    assert not (served / "index.html").exists()


def test_gateway_start_survives_a_looping_link(fake_pkg, monkeypatch):
    """A self-looping static/dist is replaced, not a crash (RuntimeError before 3.13).

    Listed in ``test/requires-real-symlinks.txt``: only a symlink can loop.
    """
    served = fake_pkg / "static" / "dist"
    served.parent.mkdir(parents=True)
    served.symlink_to(served)
    sibling = _make_dist(fake_pkg.parent.parent.parent / "KiroCrewWebsite" / "dist")
    monkeypatch.setattr(subprocess, "run", _no_brazil_path)

    assert frontend.ensure_dev_dist_symlink() == sibling.resolve()


def test_an_edition_gateway_never_serves_through_the_dev_link(fake_pkg, monkeypatch):
    """Under an edition, start stages a private copy rather than keeping the link."""
    built = _make_dist(fake_pkg.parent.parent / "website" / "dist")
    served = fake_pkg / "static" / "dist"
    served.parent.mkdir(parents=True)
    make_dir_link(served, built)
    monkeypatch.setenv("KIROCREW_EDITION_DIR", str(fake_pkg / "edition"))
    monkeypatch.setattr(subprocess, "run", _no_brazil_path)

    assert frontend.ensure_dev_dist_symlink() == served

    assert _serves_a_staged_copy(served)
    staged = served.resolve()
    # The next start keeps that copy rather than copying it again.
    assert frontend.ensure_dev_dist_symlink() == staged
    assert _staged_entries(served.parent) == [staged.name]


@pytest.mark.parametrize("linked", [True, False])
def test_an_edition_gateway_serves_the_build_it_found_when_staging_fails(fake_pkg, monkeypatch, linked):
    """A held lock or a failed copy degrades to the link, with a warning, never to no dashboard."""
    built = _make_dist(fake_pkg.parent.parent / "website" / "dist")
    served = fake_pkg / "static" / "dist"
    served.parent.mkdir(parents=True)
    if linked:
        make_dir_link(served, built)
    monkeypatch.setenv("KIROCREW_EDITION_DIR", str(fake_pkg / "edition"))
    monkeypatch.setattr(subprocess, "run", _no_brazil_path)
    monkeypatch.setattr(frontend, "_stage_dist", lambda *_a, **_k: False)

    assert frontend.ensure_dev_dist_symlink() == built.resolve()

    assert served.resolve() == built.resolve()


def test_an_edition_gateway_waits_for_the_lock_for_its_own_budget(fake_pkg, monkeypatch):
    """Start passes its short lock budget, which only binds off the event loop (cli_server)."""
    _make_dist(fake_pkg.parent.parent / "website" / "dist")
    monkeypatch.setenv("KIROCREW_EDITION_DIR", str(fake_pkg / "edition"))
    monkeypatch.setattr(subprocess, "run", _no_brazil_path)
    budgets: list = []
    monkeypatch.setattr(
        frontend, "_stage_dist", lambda *_a, lock_timeout=None, **_k: budgets.append(lock_timeout)
    )

    frontend.ensure_dev_dist_symlink()

    assert budgets == [frontend._GATEWAY_START_LOCK_TIMEOUT]
    assert 0 < frontend._GATEWAY_START_LOCK_TIMEOUT < frontend._CLI_STAGE_LOCK_TIMEOUT


def test_a_build_publishes_over_a_looping_link(tmp_path):
    """A static/dist that points at itself is replaced, not resolved into a crash.

    Listed in ``test/requires-real-symlinks.txt``: only a symlink can loop.
    """
    (tmp_path / "src" / "kiro_crew" / "static").mkdir(parents=True)
    built = _make_dist(tmp_path / "website" / "dist")
    served = tmp_path / "src" / "kiro_crew" / "static" / "dist"
    served.symlink_to(served)

    assert frontend._stage_dist(built, tmp_path, log=lambda _m: None) is True

    assert _serves_a_staged_copy(served)
    assert frontend._incomplete_bundle_reason(served) == ""


def test_the_completeness_rule_is_the_one_publish_dist_applies():
    """Both gates must accept and refuse the same trees: one pattern, pinned equal."""
    source = _PUBLISH_DIST.read_text(encoding="utf-8")
    match = re.search(r"ASSET_REF_PATTERN = String\.raw`([^`]+)`", source)
    assert match, "publish-dist.mjs no longer exports ASSET_REF_PATTERN"
    assert match.group(1) == frontend._ASSET_REF


@pytest.mark.parametrize("case", _COMPLETENESS_CASES, ids=lambda c: c["ref"])
def test_completeness_fixtures_shared_with_publish_dist(tmp_path, case):
    """test/fixtures/dist_completeness_cases.json, which publishDist.test.ts reads too."""
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "x.js").write_text("")
    (tmp_path / "index.html").write_text(f"<script {case['ref']}></script>")
    assert frontend._incomplete_bundle_reason(tmp_path) == case["reason"]


def test_the_in_gap_budget_stays_under_the_watchdog_s_re_check():
    """A publish that lands inside its in-gap budget is never read as a vanished install."""
    from kiro_crew.dashboard import stale_asset_watchdog

    source = _PUBLISH_DIST.read_text(encoding="utf-8")
    match = re.search(r"export const IN_GAP_BUDGET_MS = ([\d_]+)", source)
    assert match, "publish-dist.mjs no longer exports IN_GAP_BUDGET_MS"
    assert int(match.group(1).replace("_", "")) / 1000 < stale_asset_watchdog._CONFIRM_DELAY_SECS


def test_a_stage_log_line_cannot_fail_on_a_narrow_console(tmp_path, monkeypatch):
    """A latin-1 stdout must not turn a completed stage into a failure."""
    _repo_with_build(tmp_path)
    buffer = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(buffer, encoding="latin-1"))

    frontend.stage_built_dist(tmp_path)

    assert b"Staged static/dist" in buffer.getvalue()


@pytest.mark.parametrize("stream", ["none", "closed"])
def test_a_stage_log_line_cannot_fail_without_a_console(tmp_path, monkeypatch, stream):
    """No stdout (pythonw) or a closed one: the stage still completes and reports it."""
    _repo_with_build(tmp_path)
    closed = io.StringIO()
    closed.close()
    monkeypatch.setattr(sys, "stdout", None if stream == "none" else closed)

    frontend.stage_built_dist(tmp_path)

    assert _serves_a_staged_copy(tmp_path / "src" / "kiro_crew" / "static" / "dist")


def test_the_stage_command_reports_a_held_lock_plainly(tmp_path, monkeypatch, capsys):
    """`python -m kiro_crew.frontend stage` fails fast, by name, behind a build."""
    _repo_with_build(tmp_path)
    monkeypatch.setattr(frontend, "_CLI_STAGE_LOCK_TIMEOUT", 0)
    static_parent = tmp_path / "src" / "kiro_crew" / "static"

    with frontend._staging_lock(static_parent):
        code = _run_bounded(lambda: frontend.main(["stage", str(tmp_path)]))

    assert code == 1
    assert "a frontend build may hold it" in capsys.readouterr().out
    assert frontend.main(["stage", str(tmp_path)]) == 0


def test_the_stage_command_refuses_a_directory_that_is_not_a_checkout(tmp_path, capsys):
    """Run from website/ by mistake: refused before anything is created in it."""
    _repo_with_build(tmp_path)
    website = tmp_path / "website"
    before = sorted(p.relative_to(website) for p in website.rglob("*"))

    assert frontend.main(["stage", str(website)]) == 1

    assert "is not a Kiro Crew checkout" in capsys.readouterr().out
    assert sorted(p.relative_to(website) for p in website.rglob("*")) == before


@pytest.mark.parametrize("edition", [False, True])
def test_the_stage_command_stages_a_relative_repo(tmp_path, monkeypatch, edition):
    """`stage .` from the repo root, as make does: the copy is linked by an absolute path.

    A relative link text is read against ``static/``, so the link would dangle
    and the sweep after it would discard the copy just made.
    """
    _repo_with_build(tmp_path)  # no publish-dist.mjs: the copy path
    if edition:
        monkeypatch.setenv("KIROCREW_EDITION_DIR", str(tmp_path / "edition"))
    monkeypatch.chdir(tmp_path)
    served = tmp_path / "src" / "kiro_crew" / "static" / "dist"

    assert frontend.main(["stage", "."]) == 0

    assert not platform_compat.IS_POSIX or os.path.isabs(os.readlink(served))
    assert _serves_a_staged_copy(served)
    assert frontend._incomplete_bundle_reason(served) == ""
    assert _staged_entries(served.parent) == [served.resolve().name]


def test_a_sweep_keeps_everything_while_static_dist_resolves_nowhere(tmp_path):
    """Which copy a dangling link meant cannot be told, so none is deleted."""
    (tmp_path / "src" / "kiro_crew" / "static").mkdir(parents=True)
    served = tmp_path / "src" / "kiro_crew" / "static" / "dist"
    copy = _make_dist(served.parent / ".dist.1-copy")
    gone = tmp_path / "gone"
    gone.mkdir()
    make_dir_link(served, gone)
    gone.rmdir()

    frontend._sweep_staged_locked(served)

    assert (copy / "index.html").is_file()


def test_the_served_copy_is_recognised_through_a_second_spelling(tmp_path, monkeypatch):
    """A bind mount or a case-insensitive volume reaches one copy by two paths: never sweep it."""
    real = tmp_path / "real"
    real.mkdir()
    _repo_with_build(real)
    assert frontend._stage_dist(real / "website" / "dist", real, lambda _m: None) is True
    served = real / "src" / "kiro_crew" / "static" / "dist"
    copy = served.resolve()
    alias = tmp_path / "alias"
    make_dir_link(alias, real)
    # The link text names the copy through the alias, which resolve() keeps.
    alias_copy = alias / copy.relative_to(real)
    monkeypatch.setattr(frontend, "_live_link_target", lambda _p: alias_copy)

    assert frontend._is_staged_tree(alias_copy, served)
    frontend._sweep_staged_locked(served)

    assert (copy / "index.html").is_file(), "the served copy was swept"


def test_replacing_a_real_static_dist_says_to_restart_a_running_gateway(tmp_path):
    """A gateway that resolved the directory at start cannot follow the link that replaces it."""
    built = _atomic_checkout(tmp_path)
    _make_dist(tmp_path / "src" / "kiro_crew" / "static" / "dist", chunk="old-1.js")
    lines: list[str] = []

    assert frontend._stage_dist(built, tmp_path, lines.append) is True
    assert any("restart a gateway" in line for line in lines), lines

    lines.clear()
    assert frontend._stage_dist(built, tmp_path, lines.append) is True
    assert not any("restart a gateway" in line for line in lines), lines


def test_build_and_stage_reaps_the_whole_tree_on_timeout(tmp_path):
    """A timed-out build must have its descendants killed before the lock frees.

    `npm run build` is `tsc -p tsconfig.app.json && vite build`, so killing only npm leaves vite
    writing website/dist after the lock releases — and a surviving writer makes
    the lock's exclusion meaningless.
    """
    (tmp_path / "src" / "kiro_crew" / "static").mkdir(parents=True)
    (tmp_path / "website").mkdir()
    killed: list[tuple[int, int]] = []

    class _HangingProc:
        pid = 424242
        returncode = None

        def wait(self, timeout=None):
            if not killed:
                raise subprocess.TimeoutExpired(cmd="npm", timeout=timeout or 0)
            return -9

    with patch.object(frontend.subprocess, "Popen", lambda *a, **kw: _HangingProc()), \
         patch.object(
             frontend.platform_compat, "kill_process_tree",
             lambda pid, sig: killed.append((pid, sig)) or True,
         ):
        assert frontend.build_and_stage(tmp_path, npm="/usr/bin/true") is False

    assert killed == [(424242, frontend.platform_compat.SIGKILL)], (
        "the build tree was not reaped as a group on timeout"
    )


def test_build_timeout_reaps_a_descendant_that_escaped_the_group(tmp_path):
    """A descendant in its OWN session is outside the group a killpg reaches.

    Such an escapee keeps rewriting website/dist after this holder releases the
    staging lock, so the reap must enumerate descendants and kill them too --
    and enumerate BEFORE killing, because the kill reparents survivors to init
    and erases the PPID links that identify them.
    """
    (tmp_path / "src" / "kiro_crew" / "static").mkdir(parents=True)
    (tmp_path / "website").mkdir()
    events: list[str] = []
    killed: list[int] = []

    class _HangingProc:
        pid = 424242
        returncode = None

        def wait(self, timeout=None):
            if not killed:
                raise subprocess.TimeoutExpired(cmd="npm", timeout=timeout or 0)
            return -9

    def _descendants(pid):
        events.append("enumerate")
        return [515151]

    def _kill(pid, sig):
        events.append(f"kill{pid}")
        killed.append(pid)
        return True

    with patch.object(frontend.subprocess, "Popen", lambda *a, **kw: _HangingProc()), \
         patch.object(frontend.platform_compat, "process_descendants", _descendants), \
         patch.object(frontend.platform_compat, "kill_process_tree", _kill):
        assert frontend.build_and_stage(tmp_path, npm="/usr/bin/true") is False

    assert 515151 in killed, "the escaped descendant was left writing website/dist"
    assert events[0] == "enumerate", f"enumeration must precede any kill: {events}"
    assert events == ["enumerate", "kill424242", "kill515151"], events


def test_build_timeout_clears_a_measured_cold_build():
    """The build budget must clear the SLOWEST healthy build, not the fastest.

    It sat at 300s from the first commit while the frontend grew to ~50 runtime
    dependencies. `npm run build` is `tsc -p tsconfig.app.json` then a production bundle; on a
    developer machine that took 75-98s as a repeat build but 328s and 420s on the
    first build after `npm ci` -- and the slow case is the one Dev Fleet's
    Pull+Build always runs, so every sync was SIGKILLed mid-build and the
    dashboard silently kept serving the previous bundle. The floor here is what
    stops the constant drifting back under the work it has to cover.
    """
    assert frontend._BUILD_TIMEOUT >= 600, (
        "the build budget no longer clears a measured cold build (~420s) on a busy "
        "machine; a build that overruns it is killed and the dashboard goes stale"
    )


def test_build_budget_leaves_room_under_dev_fleet_s_run_deadline():
    """A budget that never fires cannot report anything.

    dev_fleet's stream watchdog kills the whole sync run at ``_RUN_DEADLINE_S``,
    counted from fetch -- before preflight, merge, pip and `npm ci` have reached
    the build. A build budget at or near that deadline is dead code on the very
    caller this change exists for: the watchdog kills the tree first, so the
    actionable timeout warning is never emitted and the stale bundle stays served
    with no reason given. The build may therefore claim at most HALF the run,
    leaving the other half for everything before it.
    """
    runtime = pytest.importorskip(
        "kiro_crew.apps.builtins.dev_fleet.runtime",
        reason="dev_fleet app backend not importable in this environment",
    )
    assert frontend._BUILD_TIMEOUT * 2 <= runtime._RUN_DEADLINE_S, (
        f"the build budget ({frontend._BUILD_TIMEOUT}s) leaves under half of "
        f"dev_fleet's {runtime._RUN_DEADLINE_S}s run deadline for fetch, preflight, "
        "merge, pip and npm ci -- the watchdog pre-empts the build and its warning "
        "is never emitted"
    )


def test_build_timeout_message_names_the_budget_that_expired(tmp_path):
    """The warning has to say what expired.

    "Frontend build timed out -- dashboard may be stale" told the operator
    nothing they could act on: not which budget was hit, not how long it waited.
    This line is the ONLY trace the failure leaves, so it carries the number.
    """
    (tmp_path / "src" / "kiro_crew" / "static").mkdir(parents=True)
    (tmp_path / "website").mkdir()
    killed: list[int] = []
    logged: list[str] = []

    class _HangingProc:
        pid = 424242
        returncode = None

        def wait(self, timeout=None):
            if not killed:
                raise subprocess.TimeoutExpired(cmd="npm", timeout=timeout or 0)
            return -9

    with patch.object(frontend.subprocess, "Popen", lambda *a, **kw: _HangingProc()), \
         patch.object(frontend.platform_compat, "process_descendants", lambda pid: []), \
         patch.object(
             frontend.platform_compat, "kill_process_tree",
             lambda pid, sig: killed.append(pid) or True,
         ):
        assert frontend.build_and_stage(
            tmp_path, npm="/usr/bin/true", log=logged.append
        ) is False

    timeout_line = next((m for m in logged if "timed out" in m), "")
    assert f"{frontend._BUILD_TIMEOUT}s" in timeout_line, logged


def test_stage_built_dist_accepts_an_explicit_source_dir(tmp_path):
    """A caller may stage from a build directory other than website/dist."""
    (tmp_path / "src" / "kiro_crew" / "static").mkdir(parents=True)
    scratch = _make_dist(tmp_path / "website" / "dist-scratch")

    assert frontend._stage_dist(scratch, tmp_path) is True

    assert (tmp_path / "src" / "kiro_crew" / "static" / "dist" / "index.html").is_file()


def test_build_and_stage_accepts_a_string_repo_path(tmp_path):
    """Dev Fleet's sync step passes the repo through argv, so it arrives a str.

    Asserting the step's argv is not enough: the repo arrives as a str, so the
    path must be normalised before any `/` is applied to it — `str / str` raises
    TypeError and would fail every stock Pull+Build before it builds anything.
    """
    (tmp_path / "src" / "kiro_crew" / "static").mkdir(parents=True)
    built = _make_dist(tmp_path / "website" / "dist")
    assert built.is_dir()

    class _Done:
        returncode = 0
        # build_and_stage now also runs read-only `git status`/`rev-parse` to
        # fingerprint the built source; stubbed git returns empty output, read as
        # a clean tree with no resolvable id, so no fingerprint is written and
        # the staged-bundle assertion below is unaffected.
        stdout = b""
        stderr = b""

        def wait(self, timeout=None):
            return 0

    def _build(*_a, **_kw):
        _rebuild_in_place(built, chunk="app-new456.js")  # the build publishes a new tree
        return _Done()

    with patch.object(frontend.subprocess, "Popen", _build), \
         patch.object(frontend.subprocess, "run", lambda *a, **kw: _Done()):
        # str, exactly as `build_and_stage(sys.argv[1], npm=sys.argv[2])` gets it.
        assert frontend.build_and_stage(
            str(tmp_path), npm="/usr/bin/true", log=lambda _m: None
        ) is True

    assert (tmp_path / "src" / "kiro_crew" / "static" / "dist" / "index.html").is_file()
