"""Behaviour coverage for ``kiro_crew.apps.registry``'s manifests: the manifest cache and
its garbage collection, subdirectory containment, the manifest fetch, and resolving,
merging and enriching a manifest with its install status and trust fields.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from apps_registry_coverage_helpers import _explicit_registry_execution_admission  # noqa: F401
from apps_registry_coverage_helpers import cache_dir_fixture  # noqa: F401
from apps_registry_coverage_helpers import _fake_sandbox, _FakeProc

from conftest import requires_symlinks
from kiro_crew.apps import registry

# ---------------------------------------------------------------------------
# Manifest cache
# ---------------------------------------------------------------------------


class TestManifestCache:
    _DEMO = {"name": "demo", "repo": "https://github.com/o/demo.git", "branch": "main"}

    def test_missing_cache_reads_none(self, cache_dir):
        assert registry._read_manifest_cache({"name": "nope"}) is None

    def test_round_trip(self, cache_dir):
        registry._write_manifest_cache(self._DEMO, {"name": "demo", "version": "1.0.0"})
        assert registry._read_manifest_cache(self._DEMO) == {"name": "demo", "version": "1.0.0"}

    def test_stale_cache_reads_none(self, cache_dir):
        registry._write_manifest_cache(self._DEMO, {"name": "demo"})
        path = registry._manifest_cache_path(self._DEMO)
        past = time.time() - registry._MANIFEST_CACHE_TTL - 3600
        os.utime(path, (past, past))
        assert registry._read_manifest_cache(self._DEMO) is None

    def test_corrupt_cache_reads_none(self, cache_dir):
        path = registry._manifest_cache_path(self._DEMO)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json", encoding="utf-8")
        assert registry._read_manifest_cache(self._DEMO) is None

    def test_write_failure_is_swallowed(self, cache_dir, monkeypatch):
        def _boom(path, data):
            raise OSError("disk full")

        monkeypatch.setattr(registry, "atomic_write", _boom)
        registry._write_manifest_cache(self._DEMO, {"name": "demo"})  # must not raise
        assert registry._read_manifest_cache(self._DEMO) is None

    def test_traversing_name_is_confined_to_the_cache_dir(self, cache_dir):
        path = registry._manifest_cache_path({"name": "../../escape"})
        assert path.parent == cache_dir / registry._MANIFEST_SOURCE_SUBDIR
        assert cache_dir.resolve() in path.resolve().parents

    def test_branch_change_is_a_cache_miss(self, cache_dir):
        # The cache identity folds the effective branch in, so flipping the
        # configured branch can never reuse metadata resolved from another one.
        main_row = {"name": "demo", "repo": "https://github.com/o/demo.git", "branch": "main"}
        dev_row = {"name": "demo", "repo": "https://github.com/o/demo.git", "branch": "dev"}
        assert registry._manifest_cache_path(main_row) != registry._manifest_cache_path(dev_row)
        registry._write_manifest_cache(main_row, {"name": "demo", "version": "1.0.0"})
        assert registry._read_manifest_cache(dev_row) is None
        assert registry._read_manifest_cache(main_row) is not None

    def test_same_name_different_repos_do_not_share_cache(self, cache_dir):
        one = {"name": "demo", "repo": "https://github.com/one/demo.git", "branch": "main"}
        two = {"name": "demo", "repo": "https://github.com/two/demo.git", "branch": "main"}
        assert registry._manifest_cache_path(one) != registry._manifest_cache_path(two)
        registry._write_manifest_cache(one, {"name": "demo", "description": "repo one"})
        assert registry._read_manifest_cache(two) is None

    def test_subdirectory_and_commit_pin_scope_the_identity(self, cache_dir):
        base = {"name": "demo", "repo": "https://github.com/o/demo.git", "branch": "main"}
        subdir = dict(base, subdirectory="apps/demo")
        pinned = dict(base, commit="a" * 40)
        paths = {
            registry._manifest_cache_path(base),
            registry._manifest_cache_path(subdir),
            registry._manifest_cache_path(pinned),
        }
        assert len(paths) == 3

    def test_branch_change_is_a_miss_even_under_an_unchanged_pin(self, cache_dir):
        # Non-catalog pins are data fidelity, not what the listing fetch
        # resolves — the fetch follows the BRANCH. A ref that kept only the
        # commit would hold the cache path fixed across an operator's branch
        # change, serving another branch's metadata for every pinned row.
        pin = "a" * 40
        main_row = {
            "name": "demo",
            "repo": "https://github.com/o/demo.git",
            "branch": "main",
            "commit": pin,
        }
        dev_row = dict(main_row, branch="dev")
        assert registry._manifest_cache_path(main_row) != registry._manifest_cache_path(dev_row)
        registry._write_manifest_cache(main_row, {"name": "demo", "version": "1.0.0"})
        assert registry._read_manifest_cache(dev_row) is None

    def test_credential_in_url_never_reaches_the_cache_identity(self, cache_dir):
        # Normalization strips userinfo, so the same repo with and without an
        # embedded credential is ONE cache identity and the secret is not in
        # the file name.
        plain = {"name": "demo", "gitUrl": "https://git.example.com/o/demo.git"}
        credentialed = {"name": "demo", "gitUrl": "https://user:secret@git.example.com/o/demo.git"}
        assert registry._manifest_cache_path(plain) == registry._manifest_cache_path(credentialed)
        assert "secret" not in registry._manifest_cache_path(credentialed).name

    def test_external_cache_write_failure_is_swallowed(self, cache_dir, monkeypatch):
        def _boom(path, data):
            raise OSError("disk full")

        monkeypatch.setattr(registry, "atomic_write", _boom)
        registry._write_external_registry_cache("mine", [{"name": "a"}])
        assert registry._read_external_registry_cache("mine") is None


class TestSafeCacheStem:
    def test_pure_name_is_byte_identical(self):
        assert registry._safe_cache_stem("my-app_1.0") == "my-app_1.0"

    def test_traversal_is_slugified_and_disambiguated(self):
        stem = registry._safe_cache_stem("../../config")
        assert "/" not in stem and ".." not in stem
        # Distinct originals must not collide after slugification.
        assert stem != registry._safe_cache_stem("..-..-config")

    def test_all_disallowed_characters_still_yield_a_stem(self):
        assert registry._safe_cache_stem("///").startswith("app-")


class TestExpireCacheFile:
    _DEMO = {"name": "demo", "repo": "https://github.com/o/demo.git", "branch": "main"}

    def test_backdates_instead_of_unlinking(self, cache_dir):
        registry._write_manifest_cache(self._DEMO, {"name": "demo"})
        path = registry._manifest_cache_path(self._DEMO)
        registry._expire_cache_file(path)
        assert path.is_file()  # data survives as stale fallback
        assert registry._read_manifest_cache(self._DEMO) is None

    def test_missing_file_is_a_no_op(self, cache_dir):
        registry._expire_cache_file(cache_dir / "absent.json")

    def test_path_outside_the_cache_dir_is_refused(self, cache_dir, tmp_path):
        outside = tmp_path / "victim.json"
        outside.write_text("{}", encoding="utf-8")
        before = outside.stat().st_mtime
        registry._expire_cache_file(outside)
        assert outside.stat().st_mtime == before

    def test_utime_failure_is_swallowed(self, cache_dir, monkeypatch):
        registry._write_manifest_cache(self._DEMO, {"name": "demo"})

        def _boom(path, times):
            raise OSError("read-only fs")

        monkeypatch.setattr(registry.os, "utime", _boom)
        registry._expire_cache_file(registry._manifest_cache_path(self._DEMO))


class TestManifestCacheGc:
    _DEMO = {"name": "demo", "repo": "https://github.com/o/demo.git", "branch": "main"}

    def _age(self, path, extra=0):
        past = (
            time.time()
            - max(registry._MANIFEST_CACHE_TTL, registry._EXTERNAL_REGISTRY_CACHE_TTL)
            - registry._MANIFEST_CACHE_GC_GRACE
            - 3600
            - extra
        )
        os.utime(path, (past, past))

    def test_write_reclaims_orphans_past_every_ttl_plus_grace(self, cache_dir):
        # A coordinate change orphans the old file (no reader derives its path
        # again); once it is older than every TTL plus the grace window, the
        # next write sweeps it, so churned coordinates cannot grow the dir
        # without bound.
        old_row = dict(self._DEMO, branch="dead-branch")
        registry._write_manifest_cache(old_row, {"name": "demo"})
        orphan = registry._manifest_cache_path(old_row)
        self._age(orphan)
        registry._write_manifest_cache(self._DEMO, {"name": "demo"})
        assert not orphan.exists()
        assert registry._manifest_cache_path(self._DEMO).is_file()

    def test_fresh_and_recently_expired_files_survive(self, cache_dir):
        registry._write_manifest_cache(self._DEMO, {"name": "demo"})
        kept = registry._manifest_cache_path(self._DEMO)
        # A file _expire_cache_file just backdated is expired but NOT yet
        # GC-eligible: expiry preserves it on purpose.
        registry._expire_cache_file(kept)
        other = dict(self._DEMO, name="other")
        registry._write_manifest_cache(other, {"name": "other"})
        assert kept.is_file()

    def test_registry_index_caches_are_never_swept(self, cache_dir):
        index_file = cache_dir / "_registry_acme.json"
        index_file.write_text("[]", encoding="utf-8")
        self._age(index_file)
        registry._write_manifest_cache(self._DEMO, {"name": "demo"})
        assert index_file.is_file()

    def test_a_registry_prefixed_app_name_cannot_escape_the_gc(self, cache_dir):
        # _safe_cache_stem returns plain names byte-identical, so an external
        # index can name an app `_registry_evil`. The GC boundary is the
        # by-source subdirectory, not a name prefix, so such a file is
        # reclaimed like any other manifest instead of accumulating forever.
        evil = {"name": "_registry_evil", "repo": "https://github.com/o/x.git", "branch": "main"}
        registry._write_manifest_cache(evil, {"name": "_registry_evil"})
        orphan = registry._manifest_cache_path(evil)
        assert orphan.parent.name == registry._MANIFEST_SOURCE_SUBDIR
        self._age(orphan)
        registry._write_manifest_cache(self._DEMO, {"name": "demo"})
        assert not orphan.exists()

    def test_gc_errors_are_swallowed(self, cache_dir, monkeypatch):
        registry._write_manifest_cache(self._DEMO, {"name": "demo"})

        def _boom(self_path):
            raise OSError("no listdir for you")

        monkeypatch.setattr(registry.Path, "iterdir", _boom)
        registry._write_manifest_cache(self._DEMO, {"name": "demo"})  # must not raise


# ---------------------------------------------------------------------------
# Subdirectory containment
# ---------------------------------------------------------------------------


class TestSubdirGates:
    @pytest.mark.parametrize(
        "subdir,expected",
        [
            (None, True),
            ("", True),
            ("apps/demo", True),
            ("..", False),
            ("apps/../..", False),
            ("./apps", False),
            ("/etc", False),
            ("C:/Windows", False),
            ("apps\\demo", False),
            ("apps/\x00demo", False),
            (7, False),
            (["apps"], False),
        ],
    )
    def test_is_safe_registry_subdir(self, subdir, expected):
        assert registry._is_safe_registry_subdir(subdir) is expected

    def test_contained_join_returns_root_for_empty_subdir(self, tmp_path):
        assert registry._contained_join(tmp_path, "") == tmp_path

    def test_contained_join_resolves_inside(self, tmp_path):
        (tmp_path / "apps" / "demo").mkdir(parents=True)
        joined = registry._contained_join(tmp_path, "apps/demo")
        assert joined is not None
        assert os.path.realpath(joined) == os.path.realpath(tmp_path / "apps" / "demo")

    def test_contained_join_rejects_traversal(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        assert registry._contained_join(root, "../outside") is None

    @requires_symlinks
    def test_contained_join_rejects_symlink_escape(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        os.symlink(str(outside), str(root / "link"))
        assert registry._contained_join(root, "link") is None

    def test_contained_join_degrades_to_none_on_os_error(self, tmp_path, monkeypatch):
        def _boom(self, strict=False):
            raise OSError("too many levels")

        monkeypatch.setattr(Path, "resolve", _boom)
        assert registry._contained_join(tmp_path, "sub") is None

    @requires_symlinks
    def test_contained_join_degrades_to_none_on_a_symlink_loop(self, tmp_path):
        # A real loop, not a faked OSError. The callers that re-check containment
        # after a third-party script wrote to the checkout need a value, never a
        # raise, and never a path that a later read/write would follow THROUGH the
        # loop. A self-pointing directory link must therefore fail closed to None,
        # for the link itself and for anything named beneath it, on every
        # platform. POSIX gets this for free: non-strict `Path.resolve` walks the
        # link and raises ELOOP as RuntimeError. Windows does NOT -- non-strict
        # resolve lexically collapses the reparse point and hands back a
        # contained-LOOKING path -- so `_contained_join` resolves the target
        # strictly, which forces the OS to walk it and raise on the loop on
        # Windows too. This asserts that fail-closed result on both.
        root = tmp_path / "root"
        root.mkdir()
        os.symlink("pkg", str(root / "pkg"))
        for subdir in ("pkg", "pkg/app.json"):
            assert registry._contained_join(root, subdir) is None


# ---------------------------------------------------------------------------
# Manifest fetch
# ---------------------------------------------------------------------------


def _tmp_clone_dir(monkeypatch, tmp_path, name: str = "clone") -> Path:
    """Point ``tempfile.mkdtemp`` at a directory under *tmp_path*.

    Keeps the throwaway manifest clone inside the test sandbox, so the module's
    own ``shutil.rmtree`` in its ``finally`` block leaves nothing behind.
    """
    import tempfile

    target = tmp_path / name
    target.mkdir()
    monkeypatch.setattr(tempfile, "mkdtemp", lambda *a, **k: str(target))
    return target


class TestFetchAppManifest:
    @pytest.mark.asyncio
    async def test_local_checkout_is_used_when_origin_and_branch_match(self, monkeypatch, tmp_path):
        src = tmp_path / "app-sources" / "demo"
        src.mkdir(parents=True)
        (src / "app.json").write_text(json.dumps({"name": "demo"}), encoding="utf-8")
        monkeypatch.setattr(registry, "app_source_dir", lambda n: src)

        async def _origin_matches(dest, git_url):
            return True

        async def _branch_matches(dest, branch):
            return True

        monkeypatch.setattr(registry, "_clone_origin_matches", _origin_matches)
        monkeypatch.setattr(registry, "_clone_branch_matches", _branch_matches)

        got = await registry._fetch_app_manifest(
            "o/demo", "main", app_name="demo", git_url="https://github.com/o/demo.git"
        )
        assert got == {"name": "demo"}

    @pytest.mark.asyncio
    async def test_corrupt_local_manifest_falls_through(self, monkeypatch, tmp_path):
        src = tmp_path / "app-sources" / "demo"
        src.mkdir(parents=True)
        (src / "app.json").write_text("not json", encoding="utf-8")
        monkeypatch.setattr(registry, "app_source_dir", lambda n: src)

        async def _true(*a, **k):
            return True

        monkeypatch.setattr(registry, "_clone_origin_matches", _true)
        monkeypatch.setattr(registry, "_clone_branch_matches", _true)
        # Not cloneable, so the fall-through path ends in None rather than a clone.
        assert (
            await registry._fetch_app_manifest(
                "o/demo", "main", app_name="demo", git_url="bare-name"
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_non_cloneable_url_returns_none(self):
        assert await registry._fetch_app_manifest("bare", "main") is None

    @pytest.mark.asyncio
    async def test_untrusted_host_is_refused(self, monkeypatch):
        monkeypatch.setattr(registry, "is_clone_host_trusted", lambda url: False)
        got = await registry._fetch_app_manifest(
            "o/demo", "main", git_url="https://127.0.0.1:8443/x.git"
        )
        assert got is None

    @pytest.mark.asyncio
    async def test_failed_clone_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(registry, "is_clone_host_trusted", lambda url: True)
        _tmp_clone_dir(monkeypatch, tmp_path)
        _fake_sandbox(monkeypatch, [_FakeProc(returncode=128)])
        got = await registry._fetch_app_manifest(
            "o/demo", "main", git_url="https://github.com/o/demo.git"
        )
        assert got is None

    @pytest.mark.asyncio
    async def test_missing_manifest_in_clone_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(registry, "is_clone_host_trusted", lambda url: True)
        _tmp_clone_dir(monkeypatch, tmp_path)
        _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        got = await registry._fetch_app_manifest(
            "o/demo", "main", git_url="https://github.com/o/demo.git"
        )
        assert got is None

    @pytest.mark.asyncio
    async def test_successful_clone_reads_the_manifest(self, monkeypatch, tmp_path):
        monkeypatch.setattr(registry, "is_clone_host_trusted", lambda url: True)
        clone = _tmp_clone_dir(monkeypatch, tmp_path)
        (clone / "app.json").write_text(
            json.dumps({"name": "demo", "version": "2.0.0"}), encoding="utf-8"
        )
        _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        got = await registry._fetch_app_manifest(
            "o/demo", "main", git_url="https://github.com/o/demo.git"
        )
        assert got == {"name": "demo", "version": "2.0.0"}

    @pytest.mark.asyncio
    async def test_subdirectory_escape_after_clone_is_refused(self, monkeypatch, tmp_path):
        monkeypatch.setattr(registry, "is_clone_host_trusted", lambda url: True)
        _tmp_clone_dir(monkeypatch, tmp_path)
        _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])
        monkeypatch.setattr(registry, "_contained_join", lambda root, sub: None)
        got = await registry._fetch_app_manifest(
            "o/demo", "main", "evil", git_url="https://github.com/o/demo.git"
        )
        assert got is None

    @pytest.mark.asyncio
    async def test_owner_designated_clone_uses_owner_credentials(self, monkeypatch, tmp_path):
        """The same-repo carve-out must flip env AND audit the grant."""
        monkeypatch.setattr(registry, "is_clone_host_trusted", lambda url: True)
        clone = _tmp_clone_dir(monkeypatch, tmp_path)
        (clone / "app.json").write_text(json.dumps({"name": "demo"}), encoding="utf-8")

        grants: list[str] = []
        monkeypatch.setattr(registry, "_sel_credential_grant", lambda op, url: grants.append(op))
        monkeypatch.setattr(registry, "_context_clone_sandbox_mode", lambda url: "standard")
        monkeypatch.setattr(registry, "minimal_env", lambda **kw: {"SENTINEL": "owner"})

        seen_env: list[dict[str, str]] = []

        async def _spawn(*argv, **kwargs):
            seen_env.append(kwargs["env"])
            return _FakeProc(returncode=0)

        monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="", **_kw: (list(cmd), None))
        monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: list(cmd))
        monkeypatch.setattr(registry, "create_subprocess_limited", _spawn)

        got = await registry._fetch_app_manifest(
            "o/demo",
            "main",
            git_url="https://github.com/o/demo.git",
            owner_designated=True,
        )
        assert got == {"name": "demo"}
        assert seen_env == [{"SENTINEL": "owner"}]
        assert grants == ["fetch_app_manifest"]

    @pytest.mark.asyncio
    async def test_anonymous_clone_is_the_default_posture(self, monkeypatch, tmp_path):
        monkeypatch.setattr(registry, "is_clone_host_trusted", lambda url: True)
        clone = _tmp_clone_dir(monkeypatch, tmp_path)
        (clone / "app.json").write_text(json.dumps({"name": "demo"}), encoding="utf-8")
        monkeypatch.setattr(registry, "anonymous_git_env", lambda **kw: {"SENTINEL": "anon"})

        modes: list[str] = []
        seen_env: list[dict[str, str]] = []

        async def _spawn(*argv, **kwargs):
            seen_env.append(kwargs["env"])
            return _FakeProc(returncode=0)

        def _wrap(cmd, mode=""):
            modes.append(mode)
            return list(cmd), None

        monkeypatch.setattr(registry, "wrap_argv", _wrap)
        monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: list(cmd))
        monkeypatch.setattr(registry, "create_subprocess_limited", _spawn)

        await registry._fetch_app_manifest(
            "o/demo", "main", git_url="https://github.com/o/demo.git"
        )
        assert seen_env == [{"SENTINEL": "anon"}]
        assert modes == ["strict"]


# ---------------------------------------------------------------------------
# Manifest resolution / merge / enrichment
# ---------------------------------------------------------------------------


class TestResolveManifest:
    @pytest.mark.asyncio
    async def test_entry_without_url_is_returned_unchanged(self):
        entry = {"name": "demo"}
        assert await registry._resolve_manifest(entry) is entry

    @pytest.mark.asyncio
    async def test_cached_manifest_short_circuits_the_fetch(self, monkeypatch):
        monkeypatch.setattr(
            registry, "_read_manifest_cache", lambda entry: {"description": "cached"}
        )

        async def _never(*a, **k):
            raise AssertionError("fetch must not run when a fresh cache exists")

        monkeypatch.setattr(registry, "_fetch_app_manifest", _never)
        got = await registry._resolve_manifest(
            {"name": "demo", "gitUrl": "https://github.com/o/demo.git"}
        )
        assert got["description"] == "cached"

    @pytest.mark.asyncio
    async def test_fetched_manifest_is_cached_and_merged(self, monkeypatch):
        monkeypatch.setattr(registry, "_read_manifest_cache", lambda entry: None)
        monkeypatch.setattr(registry, "_is_owner_designated_repo", lambda entry: False)
        written: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            registry,
            "_write_manifest_cache",
            lambda entry, data: written.append((entry["name"], data)),
        )

        async def _fetch(*a, **k):
            return {"description": "fresh"}

        monkeypatch.setattr(registry, "_fetch_app_manifest", _fetch)
        got = await registry._resolve_manifest(
            {"name": "demo", "gitUrl": "https://github.com/o/demo.git"}
        )
        assert got["description"] == "fresh"
        assert written == [("demo", {"description": "fresh"})]

    @pytest.mark.asyncio
    async def test_unavailable_manifest_leaves_a_minimal_row(self, monkeypatch):
        monkeypatch.setattr(registry, "_read_manifest_cache", lambda entry: None)
        monkeypatch.setattr(registry, "_is_owner_designated_repo", lambda entry: False)

        async def _fetch(*a, **k):
            return None

        monkeypatch.setattr(registry, "_fetch_app_manifest", _fetch)
        entry = {"name": "demo", "gitUrl": "https://github.com/o/demo.git"}
        assert await registry._resolve_manifest(entry) == entry

    @pytest.mark.asyncio
    async def test_failed_fetch_never_attaches_another_sources_manifest(
        self, cache_dir, monkeypatch
    ):
        # A manifest cached for branch `main` must not be attached to the same
        # app configured for branch `dev` when the dev fetch fails: the row
        # comes back minimal, never wearing another branch's metadata.
        main_row = {"name": "demo", "gitUrl": "https://github.com/o/demo.git", "branch": "main"}
        registry._write_manifest_cache(main_row, {"description": "from main", "version": "9.9.9"})
        monkeypatch.setattr(registry, "_owner_designated_repo_target", lambda entry: "")

        async def _fetch(*a, **k):
            return None

        monkeypatch.setattr(registry, "_fetch_app_manifest", _fetch)
        dev_row = {"name": "demo", "gitUrl": "https://github.com/o/demo.git", "branch": "dev"}
        got = await registry._resolve_manifest(dict(dev_row))
        assert "description" not in got
        assert got.get("version") is None

    @pytest.mark.asyncio
    async def test_refresh_expiry_updates_available_version_for_not_installed_app(
        self, cache_dir, monkeypatch
    ):
        # After the refresh path expires the coordinates' cache, the next
        # listing resolve refetches and surfaces the NEW available version;
        # install-status enrichment keeps the row not-installed.
        row = {"name": "demo", "gitUrl": "https://github.com/o/demo.git", "branch": "main"}
        registry._write_manifest_cache(row, {"version": "1.0.0"})
        registry._expire_cache_file(registry._manifest_cache_path(row))
        monkeypatch.setattr(registry, "_owner_designated_repo_target", lambda entry: "")

        async def _fetch(*a, **k):
            return {"version": "2.0.0"}

        monkeypatch.setattr(registry, "_fetch_app_manifest", _fetch)
        got = await registry._resolve_manifest(dict(row))
        assert got["version"] == "2.0.0"
        enriched = registry._enrich_with_install_status([got], installed_map={})
        assert enriched[0]["installed"] is False
        assert "installedVersion" not in enriched[0]

    @pytest.mark.asyncio
    async def test_installed_and_available_versions_stay_distinct(self, cache_dir, monkeypatch):
        row = {"name": "demo", "gitUrl": "https://github.com/o/demo.git", "branch": "main"}
        registry._write_manifest_cache(row, {"version": "2.0.0"})
        got = await registry._resolve_manifest(dict(row))
        assert got["version"] == "2.0.0"
        enriched = registry._enrich_with_install_status(
            [got], installed_map={"demo": {"version": "1.0.0", "enabled": True}}
        )
        assert enriched[0]["installedVersion"] == "1.0.0"
        assert enriched[0]["version"] == "2.0.0"
        assert enriched[0]["updateAvailable"] is True


class TestMergeManifest:
    def test_display_fields_come_from_the_manifest(self):
        merged = registry._merge_manifest(
            {"name": "demo", "repo": "o/demo", "branch": "main"},
            {
                "displayName": "Demo",
                "description": "d",
                "version": "1.2.3",
                "author": "someone",
                "tags": ["a"],
                "highlights": ["h"],
                "useCases": ["u"],
                "configuration": ["c"],
                "license": "MIT",
                "minKiroCrewVersion": "0.1.0",
            },
        )
        assert merged["displayName"] == "Demo"
        assert merged["tags"] == ["a"]
        assert merged["useCases"] == ["u"]
        assert merged["configuration"] == ["c"]
        assert merged["minKiroCrewVersion"] == "0.1.0"
        # Registry-only fields survive.
        assert merged["name"] == "demo" and merged["branch"] == "main"

    def test_runtime_fields_are_nested_under_manifest(self):
        merged = registry._merge_manifest(
            {"name": "demo", "repo": "o/demo"},
            {
                "agents": ["a"],
                "skills": ["s"],
                "crons": [],
                "mcpServers": {},
                "permissions": {"x": 1},
                "setup": {"onInstall": "echo hi"},
                "ui": {"panel": True},
                "openCommand": "open",
            },
        )
        assert merged["manifest"]["setup"] == {"onInstall": "echo hi"}
        assert merged["manifest"]["openCommand"] == "open"

    def test_no_manifest_key_means_no_manifest_block(self):
        merged = registry._merge_manifest({"name": "demo", "repo": "o/demo"}, {})
        assert "manifest" not in merged

    def test_platform_config_is_carried_over(self):
        merged = registry._merge_manifest(
            {"name": "demo", "repo": "o/demo"}, {"platform": {"os": ["macos"]}}
        )
        assert merged["platform"] == {"os": ["macos"]}

    def test_image_paths_become_blob_proxy_urls(self):
        merged = registry._merge_manifest(
            {"name": "demo", "repo": "o/demo"},
            {
                "iconPath": "assets/icon.png",
                "icon": "sparkles",
                "screenshots": ["a.png", "b.png"],
                "screenshotsDark": ["a-dark.png"],
                "heroImage": "hero.png",
                "heroImageDark": "hero-dark.png",
                "heroImageDetail": "detail.png",
                "heroImageDetailDark": "detail-dark.png",
            },
        )
        assert merged["iconUrl"] == "/api/apps/blob?repo=o/demo&path=assets/icon.png"
        assert merged["icon"] == "sparkles"
        assert merged["screenshots"] == [
            "/api/apps/blob?repo=o/demo&path=a.png",
            "/api/apps/blob?repo=o/demo&path=b.png",
        ]
        assert merged["screenshotsDark"] == ["/api/apps/blob?repo=o/demo&path=a-dark.png"]
        assert merged["heroImage"] == "/api/apps/blob?repo=o/demo&path=hero.png"
        assert merged["heroImageDark"] == "/api/apps/blob?repo=o/demo&path=hero-dark.png"
        assert merged["heroImageDetail"] == "/api/apps/blob?repo=o/demo&path=detail.png"
        assert merged["heroImageDetailDark"] == "/api/apps/blob?repo=o/demo&path=detail-dark.png"

    def test_blob_urls_never_embed_registry_clone_credentials(self):
        secret = "BlobProxySecret"
        merged = registry._merge_manifest(
            {"name": "demo", "repo": f"https://user:{secret}@example.com/o/demo.git"},
            {"iconPath": "assets/icon.png", "screenshots": ["shot.png"]},
        )

        wire = json.dumps(merged)
        assert secret not in wire
        assert "user:" not in wire
        assert "repo=https://example.com/o/demo.git" in wire

    def test_without_a_repo_no_blob_urls_are_minted(self):
        merged = registry._merge_manifest(
            {"name": "demo"},
            {"iconPath": "icon.png", "screenshots": ["a.png"], "heroImage": "h.png"},
        )
        assert "iconUrl" not in merged
        assert "screenshots" not in merged
        assert "heroImage" not in merged

    def test_the_entry_is_not_mutated(self):
        entry = {"name": "demo", "repo": "o/demo"}
        registry._merge_manifest(entry, {"description": "d"})
        assert entry == {"name": "demo", "repo": "o/demo"}


class TestEnrichWithInstallStatus:
    def test_installed_app_carries_manager_state(self):
        rows = registry._enrich_with_install_status(
            [{"name": "demo", "version": "2.0.0"}],
            {
                "demo": {
                    "version": "1.0.0",
                    "enabled": True,
                    "origin": "registry",
                    "resources": "app",
                    "lifecycle": "app",
                }
            },
        )
        row = rows[0]
        assert row["installed"] is True
        assert row["installedVersion"] == "1.0.0"
        assert row["enabled"] is True
        assert row["resources"] == "app"
        assert row["updateAvailable"] is True

    def test_externally_detected_app_is_marked_external(self):
        rows = registry._enrich_with_install_status(
            [{"name": "demo", "version": "2.0.0"}], {}, detected={"demo"}
        )
        row = rows[0]
        assert row["installed"] is True
        assert row["installedVersion"] == "unknown"
        assert row["origin"] == "external"
        assert row["updateAvailable"] is False

    def test_not_installed_app_reports_no_update(self):
        rows = registry._enrich_with_install_status([{"name": "demo"}], {})
        assert rows[0]["installed"] is False
        assert rows[0]["updateAvailable"] is False

    def test_external_row_never_inherits_origin_from_a_same_named_install(self):
        """Regression: an external registry row named after an installed
        built-in must not get that app's ``origin`` cross-stamped by name —
        the wire value would contradict the ``provenance: "external"`` stamped
        beside it."""
        rows = registry._enrich_with_install_status(
            [{"name": "meetings", "_registry": "third-party", "version": "9.0.0"}],
            {"meetings": {"version": "1.0.0", "enabled": True, "origin": "builtin"}},
        )
        row = rows[0]
        assert "origin" not in row
        # Install-state facts about the machine still flow: only the
        # trust-adjacent field is withheld.
        assert row["installed"] is True
        assert row["installedVersion"] == "1.0.0"

    def test_provenance_external_row_is_also_refused_the_origin_copy(self):
        rows = registry._enrich_with_install_status(
            [{"name": "demo", "provenance": "external"}],
            {"demo": {"version": "1.0.0", "origin": "builtin"}},
        )
        assert "origin" not in rows[0]

    def test_non_external_row_still_receives_origin(self):
        rows = registry._enrich_with_install_status(
            [{"name": "demo"}],
            {"demo": {"version": "1.0.0", "origin": "builtin"}},
        )
        assert rows[0]["origin"] == "builtin"


class TestApplyTrustFields:
    def test_external_row_can_never_self_verify_or_self_feature(self):
        rows = registry._apply_trust_fields(
            [
                {
                    "name": "demo",
                    "_registry": "third-party",
                    "_index_author": "kirocrew",
                    "verified": True,
                    "provenance": "official",
                    "featured": True,
                }
            ]
        )
        assert rows[0]["provenance"] == "external"
        assert rows[0]["verified"] is False
        assert "featured" not in rows[0]
        assert "_index_author" not in rows[0]

    def test_builtin_row_is_verified(self):
        rows = registry._apply_trust_fields([{"name": "demo", "origin": "builtin"}])
        assert rows[0]["provenance"] == "builtin"
        assert rows[0]["verified"] is True

    def test_core_row_is_verified_only_from_the_index_author(self):
        rows = registry._apply_trust_fields(
            [
                {"name": "a", "_index_author": "KiroCrew"},  # brand-ok: see catalog._fold_author
                {"name": "b", "_index_author": "someone-else"},
                {"name": "c", "_index_author": {"name": "kirocrew"}},
            ]
        )
        assert [r["verified"] for r in rows] == [True, False, False]
        assert all(r["provenance"] == "official" for r in rows)

    def test_external_row_origin_is_scrubbed_at_the_trust_boundary(self):
        """Regression: an index-published (or name-collision-inherited)
        ``origin`` on an external row is dropped, so the wire never carries
        ``origin: "builtin"`` beside ``provenance: "external"``."""
        rows = registry._apply_trust_fields(
            [{"name": "demo", "_registry": "third-party", "origin": "builtin"}]
        )
        assert rows[0]["provenance"] == "external"
        assert "origin" not in rows[0]

    def test_external_row_keeps_the_server_stamped_external_origin(self):
        rows = registry._apply_trust_fields(
            [{"name": "demo", "_registry": "third-party", "origin": "external"}]
        )
        assert rows[0]["origin"] == "external"
        assert rows[0]["provenance"] == "external"


class TestVersionNewer:
    @pytest.mark.parametrize(
        "registry_ver,installed_ver,expected",
        [
            ("2.0.0", "1.0.0", True),
            ("1.0.1", "1.0.0", True),
            ("1.0.0", "1.0.0", False),
            ("1.0.0", "2.0.0", False),
            ("1.1", "1.0.9", True),
            ("2", "1.9.9", True),
            ("2.0.0-beta.1", "1.9.9", True),
            ("1.0.0+build.9", "1.0.0", False),
            ("not-a-version", "1.0.0", False),
            ("1.0.0", "", False),
            ("", "", False),
        ],
    )
    def test_version_newer(self, registry_ver, installed_ver, expected):
        assert registry._version_newer(registry_ver, installed_ver) is expected

    def test_non_string_input_is_conservative(self):
        assert registry._version_newer(None, "1.0.0") is False


class TestExternalRegistryAvailableVersion:
    """An installed external-registry app's available version comes from the index.

    The listing reads an installed app's ``app.json`` from its persistent checkout,
    which is the tree that was INSTALLED, so the manifest's ``version`` there always
    equals the installed one. The index row is the only listing input that has seen
    the registry's newer release.
    """

    _ROW = {
        "name": "demo",
        "gitUrl": "https://git.example.com/team/demo.git",
        "branch": "main",
        "_registry": "team-registry",
    }

    @pytest.fixture()
    def installed_checkout(self, cache_dir, monkeypatch, tmp_path):
        src = tmp_path / "app-sources" / "demo"
        src.mkdir(parents=True)
        monkeypatch.setattr(registry, "app_source_dir", lambda n: src)

        async def _true(*a, **k):
            return True

        monkeypatch.setattr(registry, "_clone_origin_matches", _true)
        monkeypatch.setattr(registry, "_clone_branch_matches", _true)
        monkeypatch.setattr(registry, "_owner_designated_repo_target", lambda entry: "")

        def _write(version: str) -> None:
            (src / "app.json").write_text(
                json.dumps({"name": "demo", "version": version}), encoding="utf-8"
            )

        return _write

    async def _list(self, monkeypatch, index_row, installed_version="0.3.0"):
        async def _external():
            return [dict(index_row)]

        monkeypatch.setattr(registry, "_load_external_registries", _external)
        installed_map = {"demo": {"version": installed_version, "enabled": True}}
        rows, detected = await registry._append_external_registry_apps([], set(), installed_map)
        return registry._apply_trust_fields(
            registry._enrich_with_install_status(rows, installed_map, detected)
        )

    @pytest.mark.asyncio
    async def test_index_bump_shows_an_update_for_the_installed_checkout(
        self, installed_checkout, monkeypatch
    ):
        installed_checkout("0.3.0")
        rows = await self._list(monkeypatch, {**self._ROW, "version": "0.3.1"})
        row = rows[0]
        assert row["installedVersion"] == "0.3.0"
        assert row["updateAvailable"] is True
        assert row["version"] == "0.3.1"

    @pytest.mark.asyncio
    async def test_unchanged_index_reports_no_update(self, installed_checkout, monkeypatch):
        installed_checkout("0.3.0")
        rows = await self._list(monkeypatch, {**self._ROW, "version": "0.3.0"})
        assert rows[0]["updateAvailable"] is False
        assert rows[0]["version"] == "0.3.0"

    @pytest.mark.asyncio
    async def test_index_without_a_version_keeps_the_manifest_answer(
        self, installed_checkout, monkeypatch
    ):
        installed_checkout("0.3.0")
        rows = await self._list(monkeypatch, dict(self._ROW))
        assert rows[0]["updateAvailable"] is False
        assert rows[0]["version"] == "0.3.0"

    @pytest.mark.asyncio
    async def test_an_older_index_version_never_lowers_a_newer_manifest(
        self, cache_dir, monkeypatch
    ):
        # A manifest fetched from the remote tip (cached) can be ahead of a lagging index.
        registry._write_manifest_cache(self._ROW, {"name": "demo", "version": "0.4.0"})
        rows = await self._list(monkeypatch, {**self._ROW, "version": "0.3.1"})
        assert rows[0]["updateAvailable"] is True
        assert rows[0]["version"] == "0.4.0"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["not-a-version", "1.0.0 <b>", "", 7, None, ["1.0.0"]])
    async def test_a_malformed_index_version_is_ignored(self, installed_checkout, monkeypatch, bad):
        installed_checkout("0.3.0")
        rows = await self._list(monkeypatch, {**self._ROW, "version": bad})
        assert rows[0]["updateAvailable"] is False
        assert rows[0]["version"] == "0.3.0"
