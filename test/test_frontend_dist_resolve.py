"""Tests for ``kiro_crew.frontend.ensure_dev_dist_symlink``.

Covers the runtime dist-resolution contract described:

* pre-bundled real directory is left alone (packaged install / prior build)
* valid symlink is kept
* dangling / empty symlink is replaced
* sibling ``KiroCrewWebsite/dist`` is resolved and symlinked
* nothing-found returns ``None`` (caller logs warning and serves legacy UI)
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from conftest import requires_symlinks
from kiro_crew import frontend, platform_compat


def _fake_kiro_crew_package(root: Path) -> Path:
    """Build the minimal directory shape the resolver walks."""
    pkg = root / "src" / "KiroCrew" / "src" / "kiro_crew"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    return pkg


def _make_dist(path: Path) -> Path:
    path.mkdir(parents=True)
    # Mirror a real Vite index: a hashed chunk under /assets plus a
    # route-served reference (/manifest.js) that is NOT a file in the bundle.
    (path / "index.html").write_text(
        "<!doctype html><html><head>"
        '<script type="module" src="/manifest.js"></script>'
        '<script type="module" src="/assets/main-abc123.js"></script>'
        "</head><body></body></html>"
    )
    (path / "assets").mkdir(exist_ok=True)
    (path / "assets" / "main-abc123.js").write_text("console.log(1)")
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
# ── stage_built_dist: the served tree must not alias the Vite output ────────
#
# ensure_dev_dist_symlink() points static/dist at website/dist, so a running
# gateway serves the directory `npm run build` is about to empty. Staging is
# what breaks that aliasing.


def _repo_with_build(root: Path) -> Path:
    """Minimal repo shape: src/kiro_crew/ plus a built website/dist."""
    (root / "src" / "kiro_crew" / "static").mkdir(parents=True)
    built = _make_dist(root / "website" / "dist")
    (built / "assets" / "app-abc123.js").write_text("console.log(1)")
    return built


@requires_symlinks
def test_stage_built_dist_replaces_symlink_with_real_copy(tmp_path):
    """A static/dist symlinked at website/dist becomes an independent copy.

    This is the live-install state in which Dev Fleet's Pull+Build rewrites the
    directory the running gateway serves.
    """
    built = _repo_with_build(tmp_path)
    static_dist = tmp_path / "src" / "kiro_crew" / "static" / "dist"
    static_dist.symlink_to(built)
    assert static_dist.is_symlink()

    assert frontend._stage_dist(tmp_path / "website" / "dist", tmp_path) is True

    assert not static_dist.is_symlink(), "served dist must not alias website/dist"
    assert static_dist.is_dir()
    assert (static_dist / "index.html").is_file()
    assert (static_dist / "assets" / "app-abc123.js").is_file()

    # The decisive property: wiping the Vite output (what `npm run build` does
    # first) no longer touches what the gateway serves.
    shutil.rmtree(built)
    assert (static_dist / "index.html").is_file()


def test_stage_built_dist_refreshes_an_existing_real_dir(tmp_path):
    """Re-staging overwrites a previously staged tree instead of merging it."""
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
    """A failed copy must leave the previously staged tree in place.

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
    assert not any(
        p.name.startswith(".dist.staging.") and p.is_dir()
        for p in static_dist.parent.iterdir()
    ), "temporary staging dir must not be left behind"
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
    leftovers = [
        p for p in static_parent.iterdir()
        if p.name.startswith(".dist.staging.") and p.is_dir()
    ]
    assert leftovers == []


@requires_symlinks
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
    built = _repo_with_build(tmp_path)
    static_dist = tmp_path / "src" / "kiro_crew" / "static" / "dist"
    static_dist.symlink_to(built)

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
    # The decisive property: a served tree exists and is complete.
    assert static_dist.is_dir() and not static_dist.is_symlink()
    assert (static_dist / "index.html").is_file()
    assert (static_dist / "assets" / "app-abc123.js").is_file()


def test_stage_built_dist_restores_previous_bundle_when_swap_fails(tmp_path):
    """A failed swap must leave the previous bundle serving, not nothing.

    The live tree is moved aside rather than deleted, so a replace error cannot
    publish an empty dashboard — the last good bundle is put back.
    """
    _repo_with_build(tmp_path)
    static_dist = tmp_path / "src" / "kiro_crew" / "static" / "dist"
    static_dist.mkdir()
    (static_dist / "index.html").write_text("last good bundle")
    (static_dist / "assets").mkdir()
    (static_dist / "assets" / "old-chunk.js").write_text("old")

    real_replace = frontend.os.replace

    def _fail_publishing_replace(src, dst):
        # Fail ONLY the publication of the freshly staged tree. The restore
        # targets the same destination, so keying on dst alone would also break
        # the path under test; key on the staging source instead.
        if str(dst) == str(static_dist) and Path(src).name.startswith(".dist.staging."):
            raise OSError("EXDEV")
        return real_replace(src, dst)

    with patch.object(frontend.os, "replace", _fail_publishing_replace):
        assert frontend._stage_dist(tmp_path / "website" / "dist", tmp_path) is False

    assert static_dist.is_dir(), "the previous bundle was not restored"
    assert (static_dist / "index.html").read_text() == "last good bundle"
    assert (static_dist / "assets" / "old-chunk.js").is_file()
    leftovers = [
        p.name for p in static_dist.parent.iterdir()
        if p.is_dir() and p.name.startswith(".dist.staging.")
    ]
    assert leftovers == [], leftovers


@requires_symlinks
def test_stage_built_dist_restores_symlink_when_swap_fails(tmp_path):
    """A failed swap must restore the SYMLINK a source install serves through.

    This is the common shape: `ensure_dev_dist_symlink` leaves static/dist as a
    symlink, so a publication failure that removed it without restoring would
    leave the dashboard with nothing to serve.
    """
    built = _repo_with_build(tmp_path)
    static_dist = tmp_path / "src" / "kiro_crew" / "static" / "dist"
    static_dist.symlink_to(built)

    real_replace = frontend.os.replace

    def _fail_publishing_replace(src, dst):
        if str(dst) == str(static_dist) and Path(src).name.startswith(".dist.staging."):
            raise OSError("EXDEV")
        return real_replace(src, dst)

    with patch.object(frontend.os, "replace", _fail_publishing_replace):
        assert frontend._stage_dist(tmp_path / "website" / "dist", tmp_path) is False

    assert static_dist.is_symlink(), "the served symlink was not restored"
    assert static_dist.resolve() == built.resolve()
    assert (static_dist / "index.html").is_file()
    residue = [
        p.name for p in static_dist.parent.iterdir()
        if p.name.startswith((".dist.staging.", ".dist.previous."))
        and p.name != ".dist.staging.lock"
    ]
    assert residue == [], residue


def test_stage_built_dist_refuses_a_source_without_index(tmp_path):
    """An emptied website/dist must not be published over a good bundle.

    `npm run build` empties its outDir before repopulating it, and that build is
    not under the staging lock, so a peer flow's rebuild can be seen mid-flight.
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
        if Path(dst).name.startswith(".dist.staging.") and idx.is_file():
            idx.unlink()
        return result

    with patch.object(frontend.shutil, "copytree", _copy_then_lose_index):
        assert frontend._stage_dist(tmp_path / "website" / "dist", tmp_path) is False

    # Pin WHICH guard refused: without this a future refactor that failed
    # earlier (e.g. a copy error) would keep the test green.
    assert "Staged copy is incomplete" in capsys.readouterr().out
    assert (static_dist / "index.html").read_text() == "last good bundle"
    assert built.is_dir()
    residue = [
        p.name for p in static_dist.parent.iterdir()
        if p.name.startswith((".dist.staging.", ".dist.previous."))
        and p.name != ".dist.staging.lock"
    ]
    assert residue == [], residue


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

    The build empties website/dist, so a peer holding only the copy could read a
    partially written tree — and lazy chunks are unreachable from index.html, so
    no inspection of the copy detects that reliably.
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


def test_build_and_stage_reaps_the_whole_tree_on_timeout(tmp_path):
    """A timed-out build must have its descendants killed before the lock frees.

    `npm run build` is `tsc -b && vite build`, so killing only npm leaves vite
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

        def wait(self, timeout=None):
            return 0

    with patch.object(frontend.subprocess, "Popen", lambda *a, **kw: _Done()), \
         patch.object(frontend.subprocess, "run", lambda *a, **kw: _Done()):
        # str, exactly as `build_and_stage(sys.argv[1], npm=sys.argv[2])` gets it.
        assert frontend.build_and_stage(
            str(tmp_path), npm="/usr/bin/true", log=lambda _m: None
        ) is True

    assert (tmp_path / "src" / "kiro_crew" / "static" / "dist" / "index.html").is_file()
