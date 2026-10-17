"""Behaviour coverage for where ``kiro_crew.apps.registry`` installs from.

Candidate resolution and the index cache's provenance key, the git-provenance reader
(resolved commit, branch, origin URL), the stale-checkout sweep, and the process-group
kill a timed-out git step ends with.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from apps_registry_coverage_helpers import _explicit_registry_execution_admission  # noqa: F401
from apps_registry_coverage_helpers import cache_dir_fixture  # noqa: F401
from apps_registry_coverage_helpers import _config_with, _fake_sandbox, _FakeProc, _reg

from conftest import requires_symlinks
from kiro_crew.apps import registry

# ---------------------------------------------------------------------------
# Candidate resolution / provenance pinning
# ---------------------------------------------------------------------------


class TestCandidateResolution:
    def test_candidates_span_bundled_and_every_configured_registry(self, monkeypatch, cache_dir):
        # `_registry_app_candidates` consults the official catalog with a fresh
        # uncached HTTPS fetch, and DROPS every candidate when that lookup
        # fails; pin "catalog reachable, app absent" so the assertion
        # exercises the bundled + external span deterministically.
        monkeypatch.setattr(
            "kiro_crew.apps.official_catalog.inventory_for_install",
            lambda name: None,
        )
        monkeypatch.setattr(
            registry,
            "_load_registry_file",
            lambda: [{"name": "demo", "gitUrl": "https://github.com/core/demo.git"}, "junk"],
        )
        reg = _reg("mine", "https://gitea.internal/idx.git")
        _config_with(monkeypatch, [reg])
        registry._write_external_registry_cache(
            registry._external_registry_cache_identity(reg),
            [
                {"name": "demo", "gitUrl": "https://gitea.internal/other/demo.git"},
                {"name": "unrelated"},
            ],
        )
        candidates = registry._registry_app_candidates("demo")
        assert [registry._entry_git_url(c) for c in candidates] == [
            "https://github.com/core/demo.git",
            "https://gitea.internal/other/demo.git",
        ]

    def test_pinned_entry_requires_both_url_and_registry_to_match(self, monkeypatch):
        monkeypatch.setattr(
            registry,
            "_registry_app_candidates",
            lambda name: [
                {"gitUrl": "https://x/other.git", "_registry": "mine"},
                {"gitUrl": "https://x/demo.git", "_registry": "someone-else"},
                {"gitUrl": "https://x/demo.git", "_registry": "mine", "hit": True},
            ],
        )
        entry = registry._pinned_registry_entry(
            "demo", {"sourceUrl": "https://x/demo.git", "sourceRegistry": "mine"}
        )
        assert entry is not None and entry.get("hit") is True

    def test_rotated_credentials_still_match_the_same_pinned_source(self, monkeypatch):
        """Userinfo authenticates transport; it is not repository identity."""
        monkeypatch.setattr(
            registry,
            "_registry_app_candidates",
            lambda name: [
                {
                    "gitUrl": "https://new-user:new-secret@example.com/o/demo.git",
                    "_registry": "https://new-reg:new-token@example.com/o/index.git",
                    "hit": True,
                }
            ],
        )

        entry = registry._pinned_registry_entry(
            "demo",
            {
                "sourceUrl": "https://old-user:old-secret@example.com/o/demo.git",
                "sourceRegistry": "https://old-reg:old-token@example.com/o/index.git",
            },
        )

        assert entry is not None and entry.get("hit") is True

    def test_pinned_entry_returns_none_when_the_source_is_gone(self, monkeypatch):
        monkeypatch.setattr(
            registry,
            "_registry_app_candidates",
            lambda name: [{"gitUrl": "https://x/other.git"}],
        )
        assert registry._pinned_registry_entry("demo", {"sourceUrl": "https://x/demo.git"}) is None

    def test_bundled_candidate_matches_the_bundled_source(self, monkeypatch):
        monkeypatch.setattr(
            registry,
            "_registry_app_candidates",
            lambda name: [{"gitUrl": "https://x/demo.git"}],
        )
        entry = registry._pinned_registry_entry("demo", {"sourceUrl": "https://x/demo.git"})
        assert entry == {"gitUrl": "https://x/demo.git"}


class TestResolveInstallEntry:
    def test_record_without_provenance_keeps_first_match_wins(self, monkeypatch):
        monkeypatch.setattr(registry, "get_app", lambda name: {"name": "demo"})
        monkeypatch.setattr(registry, "get_registry_app", lambda name: {"name": "demo"})
        entry, err = registry._resolve_install_entry("demo")
        assert err == ""
        assert entry == {"name": "demo"}

    def test_fresh_install_uses_the_bare_name_lookup(self, monkeypatch):
        monkeypatch.setattr(registry, "get_app", lambda name: None)
        monkeypatch.setattr(registry, "get_registry_app", lambda name: {"name": "demo"})
        entry, err = registry._resolve_install_entry("demo")
        assert (entry, err) == ({"name": "demo"}, "")

    def test_pinned_record_resolves_to_its_own_source(self, monkeypatch):
        monkeypatch.setattr(registry, "get_app", lambda name: {"sourceUrl": "https://x/demo.git"})
        monkeypatch.setattr(registry, "_pinned_registry_entry", lambda name, meta: {"name": "demo"})
        entry, err = registry._resolve_install_entry("demo")
        assert (entry, err) == ({"name": "demo"}, "")

    def test_missing_pinned_source_refuses_instead_of_falling_back(self, monkeypatch):
        monkeypatch.setattr(registry, "get_app", lambda name: {"sourceUrl": "https://x/demo.git"})
        monkeypatch.setattr(registry, "_pinned_registry_entry", lambda name, meta: None)
        monkeypatch.setattr(
            registry,
            "get_registry_app",
            lambda name: pytest.fail("must not fall back to a bare-name lookup"),
        )
        entry, err = registry._resolve_install_entry("demo")
        assert entry is None
        assert "refusing to update it from a different source" in err

    def test_missing_pin_error_never_returns_embedded_credentials(self, monkeypatch):
        secret = "PinMismatchSecret"
        raw_url = f"https://user:{secret}@example.com/o/demo.git"
        monkeypatch.setattr(registry, "get_app", lambda name: {"sourceUrl": raw_url})
        monkeypatch.setattr(registry, "_pinned_registry_entry", lambda name, meta: None)

        entry, err = registry._resolve_install_entry("demo")

        assert entry is None
        assert secret not in err
        assert raw_url not in err


class TestRepoLookups:
    def test_bundled_repo_wins_before_external(self, monkeypatch):
        monkeypatch.setattr(
            registry, "_load_registry_file", lambda: [{"name": "demo", "repo": "o/demo"}]
        )
        assert registry.get_registry_app_by_repo("o/demo") == {
            "name": "demo",
            "repo": "o/demo",
        }

    def test_external_repo_is_resolved_from_the_sync_cache(self, monkeypatch, cache_dir):
        monkeypatch.setattr(registry, "_load_registry_file", list)
        reg = _reg("mine", "https://gitea.internal/idx.git")
        _config_with(monkeypatch, [reg])
        registry._write_external_registry_cache(
            registry._external_registry_cache_identity(reg),
            [{"name": "demo", "repo": "ext/demo", "branch": "trunk"}],
        )
        assert registry.get_registry_app_by_repo("ext/demo")["branch"] == "trunk"

    def test_unknown_repo_resolves_to_none(self, monkeypatch, cache_dir):
        monkeypatch.setattr(registry, "_load_registry_file", list)
        _config_with(monkeypatch, [])
        assert registry.get_registry_app_by_repo("nope/nope") is None

    def test_external_lookup_fails_open_on_a_config_error(self, monkeypatch):
        def _boom(cls):
            raise RuntimeError("config exploded")

        monkeypatch.setattr("kiro_crew.config.loader.KiroCrewConfig.load", classmethod(_boom))
        assert registry._external_registry_app_by_repo("ext/demo") is None
        assert registry._external_registry_repos() == set()

    def test_known_repos_union_bundled_and_external(self, monkeypatch, cache_dir):
        monkeypatch.setattr(
            registry, "_load_registry_file", lambda: [{"name": "a", "repo": "core/a"}]
        )
        reg = _reg("mine", "https://gitea.internal/idx.git")
        _config_with(monkeypatch, [reg])
        registry._write_external_registry_cache(
            registry._external_registry_cache_identity(reg),
            [{"name": "b", "repo": "ext/b"}, {"name": "c"}],
        )
        assert registry.known_registry_repos() == {"core/a", "ext/b"}


class TestExternalRegistryCacheIdentity:
    """The index cache key is provenance (name|repo|branch), not display name."""

    def test_repointing_repo_or_branch_changes_the_identity(self):
        base = _reg("mine", "https://gitea.internal/idx.git", branch="main")
        other_repo = _reg("mine", "https://gitea.internal/other.git", branch="main")
        other_branch = _reg("mine", "https://gitea.internal/idx.git", branch="dev")
        identities = {
            registry._external_registry_cache_identity(base),
            registry._external_registry_cache_identity(other_repo),
            registry._external_registry_cache_identity(other_branch),
        }
        assert len(identities) == 3

    def test_repointed_registry_stops_serving_the_old_index(self, monkeypatch, cache_dir):
        # The ignore_ttl stale-fallback readers must MISS after a repoint —
        # serving the old repository's index under the same display name is
        # the defect this PR exists to close.
        monkeypatch.setattr(registry, "_load_registry_file", list)
        old = _reg("mine", "https://gitea.internal/old.git")
        registry._write_external_registry_cache(
            registry._external_registry_cache_identity(old),
            [{"name": "demo", "repo": "old/demo"}],
        )
        _config_with(monkeypatch, [_reg("mine", "https://gitea.internal/new.git")])
        assert registry.get_registry_app_by_repo("old/demo") is None
        assert registry.known_registry_repos() == set()

    def test_absent_none_and_empty_branch_share_one_identity(self):
        # Duck-typed registry objects may not carry ``branch`` at all
        # (regression: AttributeError from _registry_app_candidates). Absent,
        # None, and "" all mean "default branch" and must agree — and must
        # never collide with a real branch.
        no_branch = SimpleNamespace(name="mine", repo="https://gitea.internal/idx.git")
        ids = {
            registry._external_registry_cache_identity(no_branch),
            registry._external_registry_cache_identity(
                _reg("mine", "https://gitea.internal/idx.git", branch="")
            ),
            registry._external_registry_cache_identity(
                _reg("mine", "https://gitea.internal/idx.git", branch=None)
            ),
        }
        assert len(ids) == 1
        real = registry._external_registry_cache_identity(
            _reg("mine", "https://gitea.internal/idx.git", branch="main")
        )
        assert real not in ids

    def test_owner_count_reader_agrees_with_the_writer(self, monkeypatch, cache_dir):
        # Regression: _repo_key_owner_count read the cache under the display
        # name while every writer had moved to the coordinate identity, so an
        # external source always counted 0 and the single-owner credential
        # grant was silently disabled.
        from kiro_crew.apps.routes import _repo_key_owner_count

        monkeypatch.setattr(registry, "_load_registry_file", list)
        reg = _reg("mine", "https://gitea.internal/idx.git")
        _config_with(monkeypatch, [reg])
        registry._write_external_registry_cache(
            registry._external_registry_cache_identity(reg),
            [{"name": "demo", "repo": "ext/demo"}],
        )
        assert _repo_key_owner_count("ext/demo") == 1

    @pytest.mark.asyncio
    async def test_fetch_reclaims_the_pre_identity_cache_file(self, monkeypatch, cache_dir):
        # An upgrade orphans the name-keyed file (no reader derives that path
        # any more); the first successful fetch reclaims it.
        reg = _reg("mine", "https://gitea.internal/idx.git")
        legacy_path = registry._external_registry_cache_path("mine")
        registry._write_external_registry_cache("mine", [{"name": "demo"}])
        assert legacy_path.is_file()

        async def _fake_fetch(repo, branch):
            return [{"name": "demo"}]

        monkeypatch.setattr(registry, "_fetch_external_registry_index", _fake_fetch)
        entries = await registry._fetch_and_cache_external_registry(reg)
        assert entries is not None
        assert not legacy_path.is_file()
        assert registry._read_external_registry_cache(
            registry._external_registry_cache_identity(reg), ignore_ttl=True
        )

    @pytest.mark.asyncio
    async def test_failed_fetch_still_reclaims_legacy_files(self, monkeypatch, cache_dir):
        # The legacy name-keyed file can embed URL userinfo in its FILENAME
        # (raw form, written by an older release). It must be reclaimed even
        # when the registry is unreachable — cleanup gated on fetch success
        # would keep a credential-bearing artifact around indefinitely. The
        # name's slug exceeds the current 120-char cap and the expected paths
        # are constructed INLINE with the historical (uncapped) recipe, so a
        # cleanup that switches to the capped derivation misses these files
        # and fails this test.
        import re as _re
        from hashlib import sha256 as _sha256

        secret = "LegacyFileSecret"
        raw_name = (
            f"https://user:{secret}@git.example.com/"
            + "/".join(["deeply-nested-group"] * 8)
            + "/org/apps.git"
        )
        reg = _reg(raw_name, raw_name)

        def _historical_path(name: str):
            slug = _re.sub(r"[^A-Za-z0-9_\-]+", "-", name).strip("-")
            digest = _sha256(name.encode("utf-8")).hexdigest()[:8]
            return cache_dir / f"_registry_{slug}-{digest}.json"

        raw_path = _historical_path(raw_name)
        sanitized_path = _historical_path(
            registry._credential_free_external_registry_value(raw_name)
        )
        assert len(raw_path.name) > 140  # over the cap: only the uncapped derivation finds it
        for p in {raw_path, sanitized_path}:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("[]", encoding="utf-8")
        assert secret in raw_path.name

        async def _fail_fetch(repo, branch):
            return None

        monkeypatch.setattr(registry, "_fetch_external_registry_index", _fail_fetch)
        assert await registry._fetch_and_cache_external_registry(reg) is None
        assert not raw_path.is_file()
        assert not sanitized_path.is_file()

    def test_long_identity_still_yields_a_writable_path(self, cache_dir):
        # An URL-derived name repeats much of the repo URL inside the
        # identity, so an over-long identity must not push the cache filename
        # past the filesystem's ~255-byte component limit (a too-long name
        # makes every write fail with ENAMETOOLONG and silently disables the
        # stale-fallback).
        long_repo = "https://gitlab.example.com/" + "/".join(["group"] * 40) + "/idx.git"
        reg = _reg(long_repo, long_repo, branch="a-rather-long-branch-name")
        identity = registry._external_registry_cache_identity(reg)
        path = registry._external_registry_cache_path(identity)
        assert len(path.name.encode("utf-8")) <= 255
        registry._write_external_registry_cache(identity, [{"name": "demo"}])
        assert registry._read_external_registry_cache(identity, ignore_ttl=True) == [
            {"name": "demo"}
        ]

    def test_truncated_slugs_do_not_collide(self, cache_dir):
        # Identity lives in the digest, not the readable prefix: two long
        # identities sharing their first 120 slug characters still map to
        # distinct cache files.
        prefix = "https://gitlab.example.com/" + "x" * 200
        a = registry._external_registry_cache_path(f"{prefix}/one|{prefix}/one|main")
        b = registry._external_registry_cache_path(f"{prefix}/two|{prefix}/two|main")
        assert a != b

    def test_capped_identity_resists_a_32_bit_digest_collision(self, cache_dir):
        # These branch values collide under the historical eight-hex digest.
        # Because the long readable prefix is capped before the branch, the
        # old current-path recipe mapped both identities to the same file and
        # a repoint could serve the former branch's stale index.
        from hashlib import sha256

        long_repo = "https://gitlab.example.com/" + "/".join(["group"] * 40) + "/idx.git"
        old_identity = registry._external_registry_cache_identity(
            _reg(long_repo, long_repo, branch="b57262")
        )
        new_identity = registry._external_registry_cache_identity(
            _reg(long_repo, long_repo, branch="b166188")
        )
        assert (
            sha256(old_identity.encode("utf-8")).hexdigest()[:8]
            == sha256(new_identity.encode("utf-8")).hexdigest()[:8]
        )

        old_path = registry._external_registry_cache_path(old_identity)
        new_path = registry._external_registry_cache_path(new_identity)
        assert old_path != new_path
        assert len(old_path.name.encode("utf-8")) <= 255
        assert len(new_path.name.encode("utf-8")) <= 255

        registry._write_external_registry_cache(old_identity, [{"name": "old-app"}])
        assert registry._read_external_registry_cache(new_identity, ignore_ttl=True) is None


class TestSourceStrings:
    def test_is_registry_source(self):
        assert registry.is_registry_source("registry:demo") is True
        assert registry.is_registry_source("git:demo") is False

    def test_registry_name_from_source(self):
        assert registry.registry_name_from_source("registry:demo") == "demo"

    def test_app_source_dir_is_under_app_sources(self, monkeypatch, tmp_path):
        monkeypatch.setattr(registry, "config_dir", lambda: tmp_path)
        assert registry.app_source_dir("demo") == tmp_path / "app-sources" / "demo"

    # `...` and `.. ` are ordinary names on POSIX, but Windows drops a trailing dot or
    # space when it opens a path, so there they name the root or its parent.
    @pytest.mark.parametrize("name", ["..", "../", "../x", ".", "", "a/b", "/etc", "...", ".. "])
    def test_app_source_dir_refuses_a_name_that_is_not_one_checkout(
        self, monkeypatch, tmp_path, name
    ):
        monkeypatch.setattr(registry, "config_dir", lambda: tmp_path)
        with pytest.raises(ValueError):
            registry.app_source_dir(name)


class TestServerPlatform:
    def test_reports_os_and_arch(self):
        info = registry.get_server_platform()
        assert set(info) == {"os", "arch"}
        assert info["os"] and info["arch"]


# ---------------------------------------------------------------------------
# Git provenance reader
# ---------------------------------------------------------------------------


class TestResolvedCloneCommit:
    _SHA = "a" * 40

    def test_missing_head_yields_no_commit(self, tmp_path):
        assert registry._resolved_clone_commit(tmp_path) == ""

    def test_detached_head_holds_the_sha_directly(self, tmp_path):
        git = tmp_path / ".git"
        git.mkdir()
        (git / "HEAD").write_text(self._SHA + "\n", encoding="utf-8")
        assert registry._resolved_clone_commit(tmp_path) == self._SHA

    def test_detached_head_with_a_non_sha_is_rejected(self, tmp_path):
        git = tmp_path / ".git"
        git.mkdir()
        (git / "HEAD").write_text("garbage", encoding="utf-8")
        assert registry._resolved_clone_commit(tmp_path) == ""

    def test_loose_ref_is_read(self, tmp_path):
        git = tmp_path / ".git"
        (git / "refs" / "heads").mkdir(parents=True)
        (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git / "refs" / "heads" / "main").write_text(self._SHA + "\n", encoding="utf-8")
        assert registry._resolved_clone_commit(tmp_path) == self._SHA

    def test_packed_refs_fallback_for_a_repacked_clone(self, tmp_path):
        git = tmp_path / ".git"
        git.mkdir()
        (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git / "packed-refs").write_text(
            f"# pack-refs with: peeled\n{self._SHA} refs/heads/main\n", encoding="utf-8"
        )
        assert registry._resolved_clone_commit(tmp_path) == self._SHA

    @pytest.mark.parametrize("ref", ["/etc/passwd", "../../escape", ""])
    def test_a_ref_that_could_escape_the_git_dir_is_refused(self, tmp_path, ref):
        git = tmp_path / ".git"
        git.mkdir()
        (git / "HEAD").write_text(f"ref: {ref}\n", encoding="utf-8")
        assert registry._resolved_clone_commit(tmp_path) == ""

    def test_unresolvable_ref_degrades_to_no_commit(self, tmp_path):
        git = tmp_path / ".git"
        git.mkdir()
        (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        assert registry._resolved_clone_commit(tmp_path) == ""

    def test_short_sha_in_a_loose_ref_is_not_accepted(self, tmp_path):
        git = tmp_path / ".git"
        (git / "refs" / "heads").mkdir(parents=True)
        (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git / "refs" / "heads" / "main").write_text("abc123\n", encoding="utf-8")
        assert registry._resolved_clone_commit(tmp_path) == ""


class TestReadCloneBranch:
    def test_missing_head_yields_none(self, tmp_path):
        assert registry._read_clone_branch(tmp_path) is None

    def test_branch_checkout_is_read(self, tmp_path):
        git = tmp_path / ".git"
        git.mkdir()
        (git / "HEAD").write_text("ref: refs/heads/release/1.x\n", encoding="utf-8")
        assert registry._read_clone_branch(tmp_path) == "release/1.x"

    def test_detached_head_fails_closed(self, tmp_path):
        git = tmp_path / ".git"
        git.mkdir()
        (git / "HEAD").write_text("a" * 40, encoding="utf-8")
        assert registry._read_clone_branch(tmp_path) is None

    def test_undecodable_head_fails_closed(self, tmp_path):
        git = tmp_path / ".git"
        git.mkdir()
        (git / "HEAD").write_bytes(b"\xff\xfe not utf8")
        assert registry._read_clone_branch(tmp_path) is None

    @pytest.mark.asyncio
    async def test_branch_matches_requires_a_non_empty_branch(self, tmp_path):
        assert await registry._clone_branch_matches(tmp_path, "") is False

    @pytest.mark.asyncio
    async def test_branch_matches_compares_exactly(self, tmp_path):
        git = tmp_path / ".git"
        git.mkdir()
        (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        assert await registry._clone_branch_matches(tmp_path, "main") is True
        assert await registry._clone_branch_matches(tmp_path, "mainline") is False

    @pytest.mark.asyncio
    async def test_origin_matches_requires_a_url_to_compare(self, tmp_path):
        assert await registry._clone_origin_matches(tmp_path, "") is False

    @pytest.mark.asyncio
    async def test_origin_matches_uses_credential_free_clone_identity(self, tmp_path, monkeypatch):
        async def _origin(dest):
            return "https://github.com/o/demo.git"

        monkeypatch.setattr(registry, "_clone_origin_url", _origin)
        assert (
            await registry._clone_origin_matches(tmp_path, "https://github.com/o/demo.git") is True
        )
        assert await registry._clone_origin_matches(tmp_path, "https://github.com/o/demo") is True
        assert (
            await registry._clone_origin_matches(
                tmp_path, "https://rotated:new-secret@github.com/o/demo.git"
            )
            is True
        )
        assert (
            await registry._clone_origin_matches(tmp_path, "https://github.com/o/Demo.git") is False
        )


class TestCloneOriginUrl:
    @pytest.mark.asyncio
    async def test_non_git_directory_yields_none(self, tmp_path):
        assert await registry._clone_origin_url(tmp_path) is None

    @pytest.mark.asyncio
    async def test_origin_is_returned_stripped(self, tmp_path, monkeypatch):
        (tmp_path / ".git").mkdir()
        _fake_sandbox(
            monkeypatch, [_FakeProc(returncode=0, output=b"https://github.com/o/demo.git\n")]
        )
        assert await registry._clone_origin_url(tmp_path) == "https://github.com/o/demo.git"

    @pytest.mark.asyncio
    async def test_failed_git_yields_none(self, tmp_path, monkeypatch):
        (tmp_path / ".git").mkdir()
        _fake_sandbox(monkeypatch, [_FakeProc(returncode=1)])
        assert await registry._clone_origin_url(tmp_path) is None

    @pytest.mark.asyncio
    async def test_spawn_failure_yields_none(self, tmp_path, monkeypatch):
        (tmp_path / ".git").mkdir()

        async def _boom(*argv, **kwargs):
            raise OSError("no git binary")

        monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="": (list(cmd), None))
        monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: list(cmd))
        monkeypatch.setattr(registry, "create_subprocess_limited", _boom)
        assert await registry._clone_origin_url(tmp_path) is None

    @pytest.mark.asyncio
    async def test_timeout_kills_the_group_and_yields_none(self, tmp_path, monkeypatch):
        (tmp_path / ".git").mkdir()

        class _Hang(_FakeProc):
            async def communicate(self):
                # flake-ok: pre-existing hang in the fake communicate that the patched wait_for times out at once, moved verbatim; tracked for a de-flake pass
                # nosemgrep: kirocrew.test-sleep-as-barrier -- the fake communicate's hang, moved verbatim; tracked for a de-flake pass
                await asyncio.sleep(30)
                return b"", b""

        _fake_sandbox(monkeypatch, [_Hang()])
        killed: list[int] = []

        async def _kill(proc):
            killed.append(proc.pid)

        monkeypatch.setattr(registry, "_kill_process_group", _kill)
        monkeypatch.setattr(registry.asyncio, "wait_for", _immediate_timeout)
        assert await registry._clone_origin_url(tmp_path) is None
        assert killed == [31337]


async def _immediate_timeout(awaitable, timeout=None):
    """Stand-in for ``asyncio.wait_for`` that times out without waiting.

    The awaitable is closed so no "never awaited" warning escapes.
    """
    awaitable.close()
    raise asyncio.TimeoutError


# ---------------------------------------------------------------------------
# Stale checkout sweep
# ---------------------------------------------------------------------------


class TestStaleCheckoutSweep:
    @staticmethod
    def _aged(path: Path) -> None:
        past = time.time() - (registry._STALE_CHECKOUT_RETENTION_DAYS + 1) * 86400
        os.utime(path, (past, past))

    def test_missing_sources_dir_is_a_no_op(self, tmp_path):
        assert registry._sweep_stale_checkouts_sync(tmp_path / "absent", time.time()) == []

    def test_aged_stale_and_partial_dirs_are_removed(self, tmp_path):
        for name in ("demo.stale-0123abcd", "demo.partial-89abcdef"):
            d = tmp_path / name
            d.mkdir()
            (d / "file.txt").write_text("x", encoding="utf-8")
            self._aged(d)
        removed = registry._sweep_stale_checkouts_sync(tmp_path, time.time())
        assert sorted(removed) == ["demo.partial-89abcdef", "demo.stale-0123abcd"]

    def test_fresh_stale_dir_is_kept(self, tmp_path):
        d = tmp_path / "demo.stale-0123abcd"
        d.mkdir()
        assert registry._sweep_stale_checkouts_sync(tmp_path, time.time()) == []
        assert d.is_dir()

    @pytest.mark.parametrize(
        "name",
        ["demo", "demo.stale-xyz", "demo.stale-0123abc", ".stale-0123abcd"],
    )
    def test_names_outside_the_convention_are_never_touched(self, tmp_path, name):
        d = tmp_path / name
        d.mkdir()
        self._aged(d)
        assert registry._sweep_stale_checkouts_sync(tmp_path, time.time()) == []
        assert d.is_dir()

    def test_unlistable_sources_dir_degrades_to_no_removals(self, tmp_path, monkeypatch):
        def _boom(self):
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "iterdir", _boom)
        assert registry._sweep_stale_checkouts_sync(tmp_path, time.time()) == []

    @requires_symlinks
    def test_symlink_pointing_outside_is_not_followed(self, tmp_path):
        sources = tmp_path / "app-sources"
        sources.mkdir()
        outside = tmp_path / "precious"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep", encoding="utf-8")
        link = sources / "demo.stale-0123abcd"
        os.symlink(str(outside), str(link))
        self._aged(outside)
        assert registry._sweep_stale_checkouts_sync(sources, time.time()) == []
        assert (outside / "keep.txt").is_file()

    @requires_symlinks
    def test_dangling_symlink_is_skipped_rather_than_deleted(self, tmp_path):
        link = tmp_path / "demo.stale-0123abcd"
        os.symlink(str(tmp_path / "gone"), str(link))
        assert registry._sweep_stale_checkouts_sync(tmp_path, time.time()) == []

    @pytest.mark.asyncio
    async def test_async_sweep_reports_removals(self, tmp_path, monkeypatch):
        monkeypatch.setattr(registry, "_app_sources_dir", lambda: tmp_path)
        d = tmp_path / "demo.stale-0123abcd"
        d.mkdir()
        self._aged(d)
        await registry._sweep_stale_checkouts()
        assert not d.exists()

    @pytest.mark.asyncio
    async def test_async_sweep_never_fails_the_install(self, tmp_path, monkeypatch):
        monkeypatch.setattr(registry, "_app_sources_dir", lambda: tmp_path)

        def _boom(sources_dir, now_ts):
            raise RuntimeError("sweep exploded")

        monkeypatch.setattr(registry, "_sweep_stale_checkouts_sync", _boom)
        await registry._sweep_stale_checkouts()  # must not raise

    def test_is_stale_candidate(self, tmp_path):
        assert registry._is_stale_candidate(tmp_path / "a.stale-0123abcd") is True
        assert registry._is_stale_candidate(tmp_path / "a.partial-0123abcd") is True
        assert registry._is_stale_candidate(tmp_path / "a") is False


# ---------------------------------------------------------------------------
# Process-group kill
# ---------------------------------------------------------------------------


class TestKillProcessGroup:
    @pytest.mark.asyncio
    async def test_sigterm_then_reap_is_enough_for_a_cooperative_child(self, monkeypatch):
        signals: list[object] = []

        async def _tree_kill(pid, sig):
            signals.append(sig)
            return True

        monkeypatch.setattr(registry.platform_compat, "kill_process_tree_async", _tree_kill)
        proc = _FakeProc(returncode=0)
        await registry._kill_process_group(proc)
        assert signals == [registry.platform_compat.SIGTERM]
        assert proc.wait_calls == 1
        assert proc.kill_calls == 0

    @pytest.mark.asyncio
    async def test_sigterm_os_error_still_reaps_the_child(self, monkeypatch):
        """A child that already exited makes killpg raise; the reap must still run."""

        async def _tree_kill(pid, sig):
            raise OSError("no such process")

        monkeypatch.setattr(registry.platform_compat, "kill_process_tree_async", _tree_kill)
        proc = _FakeProc(returncode=0)
        await registry._kill_process_group(proc)
        assert proc.wait_calls == 1
        assert proc.kill_calls == 0

    @pytest.mark.asyncio
    async def test_failed_sigkill_falls_back_to_a_pid_scoped_kill(self, monkeypatch):
        """If the group SIGKILL cannot be delivered the child is never left unreaped."""
        sent: list[object] = []

        async def _tree_kill(pid, sig):
            sent.append(sig)
            if sig == registry.platform_compat.SIGKILL:
                raise OSError("not a group leader")
            return True

        monkeypatch.setattr(registry.platform_compat, "kill_process_tree_async", _tree_kill)
        monkeypatch.setattr(registry, "_KILL_GRACE_PERIOD", 0.01)

        class _Stubborn(_FakeProc):
            def __init__(self) -> None:
                super().__init__(returncode=0)
                # A child that ignores SIGTERM has NOT exited yet, so asyncio
                # has recorded no exit code; it is set only once ``wait()``
                # finally returns.
                self.returncode = None
                self._reaped = False

            async def wait(self) -> int:
                self.wait_calls += 1
                if not self._reaped:
                    self._reaped = True
                    # flake-ok: pre-existing hang in the fake process's first wait, which the kill path reaps, moved verbatim; tracked for a de-flake pass
                    # nosemgrep: kirocrew.test-sleep-as-barrier -- the fake process's first-wait hang, moved verbatim; tracked for a de-flake pass
                    await asyncio.sleep(5)
                self.returncode = 0
                return 0

        proc = _Stubborn()
        await registry._kill_process_group(proc)
        assert sent == [
            registry.platform_compat.SIGTERM,
            registry.platform_compat.SIGKILL,
        ]
        assert proc.kill_calls == 1

    @pytest.mark.asyncio
    async def test_an_unresponsive_child_is_escalated_to_sigkill(self, monkeypatch):
        signals: list[object] = []

        async def _tree_kill(pid, sig):
            signals.append(sig)
            return True

        monkeypatch.setattr(registry.platform_compat, "kill_process_tree_async", _tree_kill)
        monkeypatch.setattr(registry, "_KILL_GRACE_PERIOD", 0.01)

        class _Stubborn(_FakeProc):
            def __init__(self) -> None:
                super().__init__(returncode=0)
                # A child that ignores SIGTERM has NOT exited yet, so asyncio
                # has recorded no exit code; it is set only once ``wait()``
                # finally returns.
                self.returncode = None
                self._reaped = False

            async def wait(self) -> int:
                self.wait_calls += 1
                if not self._reaped:
                    self._reaped = True
                    # flake-ok: pre-existing hang in the fake process's first wait, which the kill path reaps, moved verbatim; tracked for a de-flake pass
                    # nosemgrep: kirocrew.test-sleep-as-barrier -- the fake process's first-wait hang, moved verbatim; tracked for a de-flake pass
                    await asyncio.sleep(5)
                self.returncode = 0
                return 0

        proc = _Stubborn()
        await registry._kill_process_group(proc)
        assert signals == [
            registry.platform_compat.SIGTERM,
            registry.platform_compat.SIGKILL,
        ]
