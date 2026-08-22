"""Tests for kiro_crew.apps.registry — External (federated) registry support."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.apps.registry import (
    _clone_sandbox_mode,
    _external_registry_cache_path,
    _external_registry_repos,
    _fetch_external_registry_index,
    _git_url_host,
    _is_ssh_git_url,
    _load_external_registries,
    _manifest_cache_path,
    _read_external_registry_cache,
    _safe_cache_stem,
    _write_external_registry_cache,
    get_registry_app,
    get_registry_app_by_repo,
    is_clone_host_trusted,
    known_registry_repos,
    refresh_registries,
)

# Bound every gated-executor worker wait in this module. A test that gates a
# move-aside worker on a threading.Event and fails before signalling the
# release would otherwise leave that worker blocked against tmp_path past
# teardown (hanging xdist worker shutdown or mutating a directory a later test
# already deleted). Generous enough never to false-fire on a loaded shared
# runner.
_WORKER_RELEASE_TIMEOUT = 30.0


async def _bounded_off_loop(loop: asyncio.AbstractEventLoop, event: threading.Event) -> None:
    """Await *event* off the event loop, bounded by ``_WORKER_RELEASE_TIMEOUT``.

    Waiting for a worker's signal on a bare ``threading.Event`` via
    ``run_in_executor`` blocks an executor thread; if the worker never signals
    (an assertion failed before it ran), an unbounded wait would hang the test
    process. The bound turns that into a fast, loud failure instead.
    """
    signalled = await loop.run_in_executor(None, event.wait, _WORKER_RELEASE_TIMEOUT)
    assert signalled, "worker never signalled within the release timeout"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _explicit_registry_execution_admission(monkeypatch):
    """Registry tests exercise admitted installs unless they say otherwise."""
    monkeypatch.setattr("kiro_crew.apps.execution.third_party_execution_allowed", lambda: True)


@pytest.fixture(autouse=True)
def _catalog_absent(monkeypatch):
    """Pin "official catalog reachable, app absent" for the whole module.

    This module is about SEED and EXTERNAL-registry resolution; the official
    catalog is upstream of both in ``_resolve_registry_row``, and its real
    lookup performs a fresh uncached HTTPS fetch on every call (#4236).
    Before the suite-wide network guard these tests silently depended on the
    runner's live network being up. A test that wants a different catalog
    answer overrides this by monkeypatching the same seam itself.
    """
    monkeypatch.setattr(
        "kiro_crew.apps.official_catalog.inventory_for_install",
        lambda name: None,
    )


@pytest.fixture()
def cache_dir(tmp_path, monkeypatch):
    """Redirect manifest cache to a temp directory."""
    cache = tmp_path / "cache" / "app-manifests"
    cache.mkdir(parents=True)
    monkeypatch.setattr(
        "kiro_crew.apps.registry._manifest_cache_dir",
        lambda: cache,
    )
    return cache


@pytest.fixture()
def sample_entries():
    return [
        {"name": "my-app", "repo": "MyAppRepo", "branch": "mainline"},
        {"name": "other-app", "repo": "OtherRepo", "branch": "mainline"},
    ]


# ---------------------------------------------------------------------------
# _read_external_registry_cache / _write_external_registry_cache
# ---------------------------------------------------------------------------


class TestExternalRegistryCache:
    def test_read_returns_none_when_no_file(self, cache_dir):
        assert _read_external_registry_cache("nonexistent") is None

    def test_write_then_read(self, cache_dir, sample_entries):
        _write_external_registry_cache("myorg", sample_entries)
        result = _read_external_registry_cache("myorg")
        assert result == sample_entries

    def test_read_returns_none_when_stale(self, cache_dir, sample_entries):
        _write_external_registry_cache("myorg", sample_entries)
        # Backdate the file to make it stale
        path = _external_registry_cache_path("myorg")
        old_time = time.time() - 7200  # 2 hours ago
        os.utime(path, (old_time, old_time))
        assert _read_external_registry_cache("myorg") is None

    def test_read_with_ignore_ttl_returns_stale_data(self, cache_dir, sample_entries):
        _write_external_registry_cache("myorg", sample_entries)
        path = _external_registry_cache_path("myorg")
        old_time = time.time() - 7200
        os.utime(path, (old_time, old_time))
        result = _read_external_registry_cache("myorg", ignore_ttl=True)
        assert result == sample_entries

    def test_read_returns_none_for_invalid_json(self, cache_dir):
        path = _external_registry_cache_path("bad")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json", encoding="utf-8")
        assert _read_external_registry_cache("bad") is None

    def test_read_returns_none_for_non_list_json(self, cache_dir):
        path = _external_registry_cache_path("obj")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"not": "a list"}', encoding="utf-8")
        assert _read_external_registry_cache("obj") is None

    def test_read_drops_entries_with_traversal_name(self, cache_dir):
        # A cache written by an older build (before the KEBAB_RE gate) — or
        # tampered on disk — may contain a path-traversing name. Every read
        # (fresh or stale) must drop it so it can never reach app_source_dir().
        path = _external_registry_cache_path("evil")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                [
                    {"name": "../../victim", "repo": "R", "branch": "main"},
                    {"name": "/tmp/victim", "repo": "R", "branch": "main"},
                    {"name": "Bad_Name", "repo": "R", "branch": "main"},
                    {"name": "good-app", "repo": "R", "branch": "main"},
                    {"repo": "R", "branch": "main"},  # missing name
                    "not-a-dict",
                ]
            ),
            encoding="utf-8",
        )
        result = _read_external_registry_cache("evil")
        assert result == [{"name": "good-app", "repo": "R", "branch": "main"}]

    def test_read_stale_also_drops_traversal_name(self, cache_dir):
        # The stale-fallback read path (ignore_ttl=True) must gate names too.
        path = _external_registry_cache_path("evil")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                [
                    {"name": "../../victim", "repo": "R", "branch": "main"},
                    {"name": "good-app", "repo": "R", "branch": "main"},
                ]
            ),
            encoding="utf-8",
        )
        old_time = time.time() - 7200
        os.utime(path, (old_time, old_time))
        result = _read_external_registry_cache("evil", ignore_ttl=True)
        assert result == [{"name": "good-app", "repo": "R", "branch": "main"}]


# ---------------------------------------------------------------------------
# _fetch_external_registry_index — input validation
# ---------------------------------------------------------------------------


class TestFetchExternalRegistryValidation:
    @pytest.fixture(autouse=True)
    def mock_sel(self, monkeypatch):
        """Patch _sel_fn so tests don't abort on SEL unavailability."""
        mock_sel_instance = MagicMock()
        monkeypatch.setattr(
            "kiro_crew.apps.registry._sel_fn",
            mock_sel_instance,
        )
        # Bypass OS-sandbox wrap — macOS 26 has no sandbox backend.
        monkeypatch.setattr(
            "kiro_crew.apps.registry.wrap_argv", lambda argv, **k: (list(argv), None)
        )

    @pytest.mark.asyncio
    async def test_rejects_repo_with_path_traversal(self):
        result = await _fetch_external_registry_index("../evil", "mainline")
        assert result is None

    @pytest.mark.asyncio
    async def test_rejects_repo_with_spaces(self):
        result = await _fetch_external_registry_index("my repo", "mainline")
        assert result is None

    @pytest.mark.asyncio
    async def test_rejects_repo_with_slashes(self):
        result = await _fetch_external_registry_index("pkg/sub", "mainline")
        assert result is None

    @pytest.mark.asyncio
    async def test_rejects_branch_with_double_dots(self):
        result = await _fetch_external_registry_index("ValidRepo", "main/../evil")
        assert result is None

    @pytest.mark.asyncio
    async def test_rejects_branch_with_shell_chars(self):
        result = await _fetch_external_registry_index("ValidRepo", "main;rm -rf /")
        assert result is None

    @pytest.mark.asyncio
    async def test_accepts_valid_repo_and_branch(self):
        """Valid inputs pass validation but fail on git (no network in tests)."""
        # External registries are now cloned via generic ``git clone``, so the
        # repo must be a cloneable URL (https/ssh/git). This passes validation
        # but fails on the actual git command (no network in unit tests). We
        # just verify it doesn't return None from validation alone.
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b"error"))
            mock_proc.returncode = 128
            mock_exec.return_value = mock_proc
            result = await _fetch_external_registry_index(
                "https://github.com/example/ValidRepo-123.git", "mainline"
            )
            # Should have attempted git clone (passed validation)
            assert mock_exec.called
            assert result is None  # git failed but validation passed

    @pytest.mark.asyncio
    async def test_accepts_branch_with_slashes(self):
        """Branch names like 'feature/foo' are valid git refs."""
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b"error"))
            mock_proc.returncode = 128
            mock_exec.return_value = mock_proc
            await _fetch_external_registry_index(
                "https://github.com/example/MyRepo.git", "feature/branch-name"
            )
            assert mock_exec.called


# ---------------------------------------------------------------------------
# _fetch_external_registry_index — app-registry.json parsing
# ---------------------------------------------------------------------------


class TestFetchExternalRegistryParsing:
    @pytest.fixture(autouse=True)
    def mock_sel(self, monkeypatch):
        """Patch _sel_fn so tests don't abort on SEL unavailability."""
        mock_sel_instance = MagicMock()
        monkeypatch.setattr(
            "kiro_crew.apps.registry._sel_fn",
            mock_sel_instance,
        )
        # Bypass OS-sandbox wrap — macOS 26 has no sandbox backend.
        monkeypatch.setattr(
            "kiro_crew.apps.registry.wrap_argv", lambda argv, **k: (list(argv), None)
        )

    @pytest.mark.asyncio
    async def test_parses_app_registry_json_from_clone(self, tmp_path):
        """Simulates a successful git clone whose checkout has app-registry.json."""
        registry_data = [{"name": "cool-app", "repo": "CoolApp", "branch": "mainline"}]
        repo_url = "https://github.com/example/CoolApp.git"

        clone_dir = tmp_path / "clone"

        # ``git clone`` is mocked: instead of cloning, populate the checkout
        # directory with the files the function reads back from disk.
        async def mock_exec_side_effect(*args, **kwargs):
            clone_dir.mkdir(parents=True, exist_ok=True)
            (clone_dir / "app-registry.json").write_text(
                json.dumps(registry_data), encoding="utf-8"
            )
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_proc.returncode = 0
            return mock_proc

        with (
            patch("tempfile.mkdtemp", return_value=str(clone_dir)),
            patch("asyncio.create_subprocess_exec", side_effect=mock_exec_side_effect),
        ):
            result = await _fetch_external_registry_index(repo_url, "mainline")
            assert result == registry_data

    @pytest.mark.asyncio
    async def test_falls_back_to_apps_dir_scan(self, tmp_path):
        """When app-registry.json is absent, scans apps/*/app.json in the clone."""
        repo_url = "https://github.com/example/MyRepo.git"
        clone_dir = tmp_path / "clone"

        # ``git clone`` is mocked: populate the checkout with an apps/ tree but
        # no app-registry.json, exercising the fallback scan.
        async def mock_exec_side_effect(*args, **kwargs):
            app_dir = clone_dir / "apps" / "my-tool"
            app_dir.mkdir(parents=True, exist_ok=True)
            (app_dir / "app.json").write_text('{"name": "my-tool"}', encoding="utf-8")
            # A non-matching file that should be ignored.
            (clone_dir / "apps" / "README.md").write_text("hello", encoding="utf-8")
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_proc.returncode = 0
            return mock_proc

        with (
            patch("tempfile.mkdtemp", return_value=str(clone_dir)),
            patch("asyncio.create_subprocess_exec", side_effect=mock_exec_side_effect),
        ):
            result = await _fetch_external_registry_index(repo_url, "mainline")
            assert result is not None
            assert len(result) == 1
            assert result[0]["name"] == "my-tool"
            assert result[0]["subdirectory"] == "apps/my-tool"


# ---------------------------------------------------------------------------
# _load_external_registries
# ---------------------------------------------------------------------------


class TestLoadExternalRegistries:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_registries_configured(self, monkeypatch):
        mock_config = MagicMock()
        mock_config.registries = []
        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            lambda: mock_config,
        )
        result = await _load_external_registries()
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_cached_entries(self, cache_dir, monkeypatch):
        entries = [{"name": "cached-app", "repo": "R", "branch": "mainline"}]
        _write_external_registry_cache("myorg", entries)

        mock_reg = MagicMock()
        mock_reg.name = "myorg"
        mock_reg.repo = "MyOrgRepo"
        mock_reg.branch = "mainline"

        mock_config = MagicMock()
        mock_config.registries = [mock_reg]
        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            lambda: mock_config,
        )

        result = await _load_external_registries()
        assert len(result) == 1
        assert result[0]["name"] == "cached-app"
        assert result[0]["_registry"] == "myorg"

    @pytest.mark.asyncio
    async def test_tags_entries_with_registry_name(self, cache_dir, monkeypatch):
        entries = [{"name": "app1"}, {"name": "app2"}]
        _write_external_registry_cache("identity", entries)

        mock_reg = MagicMock()
        mock_reg.name = "identity"
        mock_reg.repo = "IdentityApps"
        mock_reg.branch = "mainline"

        mock_config = MagicMock()
        mock_config.registries = [mock_reg]
        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            lambda: mock_config,
        )

        result = await _load_external_registries()
        assert all(e["_registry"] == "identity" for e in result)


# ---------------------------------------------------------------------------
# get_registry_app — external cache lookup
# ---------------------------------------------------------------------------


class TestGetRegistryAppExternal:
    def test_finds_app_in_external_cache(self, cache_dir, monkeypatch):
        entries = [
            {"name": "ext-app", "repo": "ExtRepo", "branch": "mainline"},
        ]
        _write_external_registry_cache("myorg", entries)

        # Mock config to have one registry
        mock_reg = MagicMock()
        mock_reg.name = "myorg"
        mock_reg.repo = "MyOrgRepo"
        mock_reg.branch = "mainline"

        mock_config = MagicMock()
        mock_config.registries = [mock_reg]

        monkeypatch.setattr(
            "kiro_crew.apps.registry._load_registry_file",
            lambda: [],  # empty core registry
        )
        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            lambda: mock_config,
        )

        result = get_registry_app("ext-app")
        assert result is not None
        assert result["name"] == "ext-app"
        assert result["_registry"] == "myorg"

    def test_returns_none_when_not_found(self, cache_dir, monkeypatch):
        mock_config = MagicMock()
        mock_config.registries = []
        monkeypatch.setattr(
            "kiro_crew.apps.registry._load_registry_file",
            lambda: [],
        )
        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            lambda: mock_config,
        )

        result = get_registry_app("nonexistent")
        assert result is None

    def test_prefers_core_registry_over_external(self, cache_dir, monkeypatch):
        core_entry = {"name": "shared-app", "repo": "CoreRepo", "branch": "mainline"}
        ext_entries = [{"name": "shared-app", "repo": "ExtRepo", "branch": "mainline"}]
        _write_external_registry_cache("myorg", ext_entries)

        mock_reg = MagicMock()
        mock_reg.name = "myorg"
        mock_reg.repo = "MyOrgRepo"
        mock_reg.branch = "mainline"

        mock_config = MagicMock()
        mock_config.registries = [mock_reg]

        monkeypatch.setattr(
            "kiro_crew.apps.registry._load_registry_file",
            lambda: [core_entry],
        )
        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            lambda: mock_config,
        )

        result = get_registry_app("shared-app")
        assert result["repo"] == "CoreRepo"  # core wins


# ---------------------------------------------------------------------------
# get_registry_app_by_repo — blob-proxy branch resolution (bundled + external)
# ---------------------------------------------------------------------------


class TestGetRegistryAppByRepoExternal:
    def test_resolves_external_repo_branch(self, cache_dir, monkeypatch):
        # Regression: the /api/apps/blob branch fallback must resolve the
        # configured branch for external-registry apps, not silently use "main"
        # (which 403s the icon for repos pinned to another branch).
        entries = [{"name": "ext-app", "repo": "ExtRepo", "branch": "release"}]
        _write_external_registry_cache("myorg", entries)

        mock_reg = MagicMock()
        mock_reg.name = "myorg"
        mock_reg.repo = "MyOrgRepo"
        mock_reg.branch = "release"
        mock_config = MagicMock()
        mock_config.registries = [mock_reg]

        monkeypatch.setattr(
            "kiro_crew.apps.registry._load_registry_file",
            lambda: [],  # empty core registry
        )
        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            lambda: mock_config,
        )

        entry = get_registry_app_by_repo("ExtRepo")
        assert entry is not None
        assert entry.get("branch") == "release"

    def test_prefers_bundled_over_external(self, cache_dir, monkeypatch):
        core_entry = {"name": "shared", "repo": "SharedRepo", "branch": "main"}
        ext_entries = [{"name": "shared", "repo": "SharedRepo", "branch": "other"}]
        _write_external_registry_cache("myorg", ext_entries)

        mock_reg = MagicMock()
        mock_reg.name = "myorg"
        mock_reg.repo = "MyOrgRepo"
        mock_config = MagicMock()
        mock_config.registries = [mock_reg]

        monkeypatch.setattr(
            "kiro_crew.apps.registry._load_registry_file",
            lambda: [core_entry],
        )
        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            lambda: mock_config,
        )

        entry = get_registry_app_by_repo("SharedRepo")
        assert entry["branch"] == "main"  # bundled wins

    def test_returns_none_when_not_in_any_registry(self, cache_dir, monkeypatch):
        mock_config = MagicMock()
        mock_config.registries = []
        monkeypatch.setattr(
            "kiro_crew.apps.registry._load_registry_file",
            lambda: [],
        )
        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            lambda: mock_config,
        )

        assert get_registry_app_by_repo("Nope") is None


# ---------------------------------------------------------------------------
# Clone sandbox-mode gating (trusted-host SSH exposure)
# ---------------------------------------------------------------------------


class TestGitUrlHost:
    def test_ssh_scheme_with_user_and_port(self):
        assert _git_url_host("ssh://git@example.com:2222/org/app.git") == "example.com"

    def test_scp_style(self):
        assert _git_url_host("git@github.com:org/app.git") == "github.com"

    def test_https(self):
        assert _git_url_host("https://gitlab.com/org/app") == "gitlab.com"

    def test_host_is_lowercased(self):
        assert _git_url_host("ssh://GitHub.COM/org/app") == "github.com"

    def test_unparseable_returns_empty(self):
        assert _git_url_host("not a url") == ""
        assert _git_url_host("") == ""


class TestIsSshGitUrl:
    def test_ssh_scheme(self):
        assert _is_ssh_git_url("ssh://git@host/p") is True

    def test_git_ssh_scheme(self):
        assert _is_ssh_git_url("git+ssh://host/p") is True

    def test_scp_style(self):
        assert _is_ssh_git_url("git@github.com:org/app.git") is True

    def test_https_is_not_ssh(self):
        assert _is_ssh_git_url("https://github.com/org/app") is False

    def test_empty(self):
        assert _is_ssh_git_url("") is False


class TestCloneSandboxMode:
    """The fix: only SSH remotes on trusted hosts get ~/.ssh-exposing standard mode."""

    def test_https_always_strict(self):
        # https never needs SSH keys, regardless of host.
        assert _clone_sandbox_mode("https://github.com/org/app") == "strict"

    def test_ssh_public_forge_is_standard(self):
        assert _clone_sandbox_mode("git@github.com:org/app.git") == "standard"
        assert _clone_sandbox_mode("ssh://git@gitlab.com/org/app") == "standard"

    def test_ssh_untrusted_host_stays_strict(self):
        # The core of finding B: a hostile/typo'd SSH host must NOT be offered
        # the owner's ~/.ssh keys — it fails closed under strict.
        assert _clone_sandbox_mode("ssh://evil.example.com/x") == "strict"
        assert _clone_sandbox_mode("git@evil.example:apps.git") == "strict"

    def test_configured_registry_host_is_trusted(self):
        # A self-hosted registry the user explicitly configured is trusted.
        trusted = frozenset({"git.internal.example"})
        assert _clone_sandbox_mode("git@git.internal.example:apps.git", trusted) == "standard"
        # ...but only that host, not arbitrary ones.
        assert _clone_sandbox_mode("git@other.example:apps.git", trusted) == "strict"

    def test_unparseable_ssh_url_stays_strict(self):
        assert _clone_sandbox_mode("ssh://") == "strict"

    def test_no_trusted_hosts_defaults_to_public_only(self):
        assert _clone_sandbox_mode("git@bitbucket.org:org/app.git") == "standard"
        assert _clone_sandbox_mode("git@selfhosted.example:org/app.git") == "strict"


# ---------------------------------------------------------------------------
# known_registry_repos — blob-proxy SSRF allowlist (bundled + external union)
# ---------------------------------------------------------------------------


class TestKnownRegistryRepos:
    def test_includes_bundled_repos_when_no_external(self, cache_dir, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.apps.registry._load_registry_file",
            lambda: [{"name": "core", "repo": "CoreRepo"}],
        )
        mock_config = MagicMock()
        mock_config.registries = []
        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            lambda: mock_config,
        )
        assert known_registry_repos() == {"CoreRepo"}

    def test_unions_external_registry_app_repos(self, cache_dir, monkeypatch):
        # External registry "PCN" lists app pcn-radar whose repo is PCNRadar.
        _write_external_registry_cache(
            "PCN", [{"name": "pcn-radar", "repo": "PCNRadar", "branch": "mainline"}]
        )
        mock_reg = MagicMock()
        mock_reg.name = "PCN"
        mock_reg.repo = "PCNAppRegistry"
        mock_reg.branch = "mainline"
        mock_config = MagicMock()
        mock_config.registries = [mock_reg]
        monkeypatch.setattr(
            "kiro_crew.apps.registry._load_registry_file",
            lambda: [{"name": "core", "repo": "CoreRepo"}],
        )
        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            lambda: mock_config,
        )
        repos = known_registry_repos()
        assert "CoreRepo" in repos  # bundled repos preserved
        assert "PCNRadar" in repos  # external-registry app repo now trusted

    def test_trusts_stale_cache_via_ignore_ttl(self, cache_dir, monkeypatch):
        # Age the cache past the 1h TTL; ignore_ttl must still trust the repo
        # so icons don't 403 between list_registry refreshes.
        _write_external_registry_cache(
            "PCN", [{"name": "pcn-radar", "repo": "PCNRadar", "branch": "mainline"}]
        )
        stale = time.time() - 7200
        os.utime(_external_registry_cache_path("PCN"), (stale, stale))
        mock_reg = MagicMock()
        mock_reg.name = "PCN"
        mock_reg.repo = "PCNAppRegistry"
        mock_config = MagicMock()
        mock_config.registries = [mock_reg]
        monkeypatch.setattr(
            "kiro_crew.apps.registry._load_registry_file",
            lambda: [],
        )
        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            lambda: mock_config,
        )
        assert "PCNRadar" in known_registry_repos()

    def test_fails_open_to_bundled_when_config_raises(self, cache_dir, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.apps.registry._load_registry_file",
            lambda: [{"name": "core", "repo": "CoreRepo"}],
        )

        def _boom():
            raise RuntimeError("config blew up")

        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            _boom,
        )
        # Must not raise — the allowlist falls open to the bundled set.
        assert known_registry_repos() == {"CoreRepo"}


# ---------------------------------------------------------------------------
# _external_registry_repos — external-only set; fails open to EMPTY (not bundled)
# ---------------------------------------------------------------------------


class TestExternalRegistryRepos:
    def test_returns_external_repos_only(self, cache_dir, monkeypatch):
        _write_external_registry_cache(
            "PCN", [{"name": "pcn-radar", "repo": "PCNRadar", "branch": "mainline"}]
        )
        mock_reg = MagicMock()
        mock_reg.name = "PCN"
        mock_reg.repo = "PCNAppRegistry"
        mock_config = MagicMock()
        mock_config.registries = [mock_reg]
        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            lambda: mock_config,
        )
        # No bundled lookup here — helper returns ONLY external repos.
        assert _external_registry_repos() == {"PCNRadar"}

    def test_fails_open_to_empty_set(self, cache_dir, monkeypatch):
        def _boom():
            raise RuntimeError("config blew up")

        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            _boom,
        )
        # Distinct from known_registry_repos: the helper falls open to EMPTY,
        # leaving the bundled set as the caller's sole source of truth.
        assert _external_registry_repos() == set()


# ---------------------------------------------------------------------------
# install_from_registry admission — the signed manifest is now passed to the
# gate (fetched read-only BEFORE clone), so require_signature no longer denies
# every registry install of a correctly-signed app.
# ---------------------------------------------------------------------------


class TestRegistryInstallAdmission:
    def _write_policy(self, home, policy):
        (home / "app_admission.json").write_text(json.dumps(policy))

    @pytest.fixture()
    def reg_home(self, tmp_path, monkeypatch):
        home = tmp_path / "kirocrew-home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        return home

    def _signed_manifest(self, name, secret, signer="acme"):
        import hashlib
        import hmac

        from kiro_crew.apps.manifest import AppManifest

        data = {
            "name": name,
            "version": "1.0.0",
            "displayName": name,
            "description": "d",
            "author": "tester",
            "signer": signer,
        }
        m = AppManifest.from_dict(data)
        data["signature"] = hmac.new(
            secret.encode(), m.signing_payload(), hashlib.sha256
        ).hexdigest()
        return data

    @pytest.mark.asyncio
    async def test_signed_app_admitted_under_require_signature(self, reg_home):
        from kiro_crew.apps.registry import install_from_registry

        secret = "s3cr3t"
        self._write_policy(
            reg_home,
            {
                "mode": "enforce",
                "require_signature": True,
                "approved": ["signed-reg"],
                "trust_keys": {"acme": secret},
            },
        )
        manifest = self._signed_manifest("signed-reg", secret)
        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={
                    "name": "signed-reg",
                    "repo": "https://example.com/SignedRepo.git",
                    "branch": "mainline",
                },
            ),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value=manifest),
            ),
            patch(
                "kiro_crew.apps.registry._clone_build_app",
                new=AsyncMock(return_value={"ok": False, "error": "stop-after-admission"}),
            ) as mock_build,
        ):
            result = await install_from_registry("signed-reg")
        # Admission passed (signed manifest verified) — flow proceeded to the
        # clone/build step, which we stub to stop right after admission.
        assert "blocked by admission policy" not in (result.get("error") or "")
        mock_build.assert_awaited()

    @pytest.mark.asyncio
    async def test_unsigned_app_denied_under_require_signature(self, reg_home):
        from kiro_crew.apps.registry import install_from_registry

        self._write_policy(
            reg_home,
            {
                "mode": "enforce",
                "require_signature": True,
                "approved": ["unsigned-reg"],
                "trust_keys": {"acme": "s3cr3t"},
            },
        )
        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={
                    "name": "unsigned-reg",
                    "repo": "https://example.com/UnsignedRepo.git",
                    "branch": "mainline",
                },
            ),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value={"name": "unsigned-reg", "version": "1.0.0"}),
            ),
            patch(
                "kiro_crew.apps.registry._clone_build_app",
                new=AsyncMock(return_value={"ok": True, "pkg_dir": reg_home}),
            ) as mock_build,
        ):
            result = await install_from_registry("unsigned-reg")
        # Denied at the gate — the app is never cloned/built.
        assert not result["ok"]
        assert "blocked by admission policy" in result["error"]
        mock_build.assert_not_awaited()


# ---------------------------------------------------------------------------
# _external_registry_cache_path — collision fix for URL-derived names
# ---------------------------------------------------------------------------


class TestExternalRegistryCachePath:
    def test_pure_safe_name_path_unchanged(self, cache_dir):
        # Legacy safe names keep the historical byte-identical path (no hash).
        path = _external_registry_cache_path("myorg")
        assert path.name == "_registry_myorg.json"

    def test_two_url_names_produce_distinct_paths(self, cache_dir):
        # Both names fail the safe-name regex; without the hash suffix they used
        # to collapse to "_registry_invalid.json". They must now be distinct.
        a = _external_registry_cache_path("https://github.com/acme/apps")
        b = _external_registry_cache_path("https://gitlab.com/acme/apps")
        assert a != b
        assert a.name != "_registry_invalid.json"
        assert b.name != "_registry_invalid.json"

    def test_url_name_is_stable(self, cache_dir):
        # Same original name always maps to the same path (deterministic hash).
        name = "https://github.com/acme/apps"
        assert _external_registry_cache_path(name) == _external_registry_cache_path(name)


# ---------------------------------------------------------------------------
# refresh_registries — cache busting + contract shape
# ---------------------------------------------------------------------------


class TestRefreshRegistries:
    @pytest.mark.asyncio
    async def test_success_swaps_cache_and_expires_manifests(self, cache_dir, monkeypatch):
        # Seed a stale index cache for registry "acme" listing one app, plus
        # that app's manifest cache.
        _write_external_registry_cache(
            "acme", [{"name": "cool-app", "repo": "R", "branch": "main"}]
        )
        manifest_path = _manifest_cache_path("cool-app")
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text('{"name": "cool-app"}', encoding="utf-8")
        index_path = _external_registry_cache_path("acme")

        mock_reg = MagicMock()
        mock_reg.name = "acme"
        mock_reg.repo = "https://github.com/acme/apps"
        mock_reg.branch = "main"
        mock_config = MagicMock()
        mock_config.registries = [mock_reg]
        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            lambda: mock_config,
        )
        # Successful refetch returns a fresh index.
        monkeypatch.setattr(
            "kiro_crew.apps.registry._fetch_external_registry_index",
            AsyncMock(return_value=[{"name": "cool-app", "repo": "R"}]),
        )
        monkeypatch.setattr(
            "kiro_crew.apps.registry.list_registry",
            AsyncMock(return_value=[{"name": "cool-app"}]),
        )

        result = await refresh_registries()

        # Fetch-then-swap: index cache is overwritten (still present), and the
        # manifest cache is EXPIRED (mtime backdated) rather than deleted, so a
        # failed manifest refetch can still fall back to it.
        assert index_path.is_file()
        assert manifest_path.is_file()
        assert time.time() - manifest_path.stat().st_mtime > 86400
        # Contract shape.
        assert result["ok"] is True
        assert result["refreshed"] == ["acme"]
        assert result["failed"] == []
        assert result["results"] == [{"name": "acme", "ok": True}]
        assert result["apps"] == 1
        assert isinstance(result["lastSyncedAt"], str)

    @pytest.mark.asyncio
    async def test_fetch_failure_preserves_stale_and_reports_failed(self, cache_dir, monkeypatch):
        # Seed a stale index cache; the refetch will fail.
        _write_external_registry_cache(
            "acme", [{"name": "cool-app", "repo": "R", "branch": "main"}]
        )
        index_path = _external_registry_cache_path("acme")

        mock_reg = MagicMock()
        mock_reg.name = "acme"
        mock_reg.repo = "https://github.com/acme/apps"
        mock_reg.branch = "main"
        mock_config = MagicMock()
        mock_config.registries = [mock_reg]
        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            lambda: mock_config,
        )
        # Refetch fails (unreachable forge / network blip).
        monkeypatch.setattr(
            "kiro_crew.apps.registry._fetch_external_registry_index",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            "kiro_crew.apps.registry.list_registry",
            AsyncMock(return_value=[{"name": "cool-app"}]),
        )

        result = await refresh_registries()

        # The prior cache is PRESERVED (not dropped) so apps don't vanish, and
        # the failure is surfaced instead of being reported as a sync.
        assert index_path.is_file()
        assert _read_external_registry_cache("acme", ignore_ttl=True) == [
            {"name": "cool-app", "repo": "R", "branch": "main"}
        ]
        assert result["ok"] is False
        assert result["refreshed"] == []
        assert result["failed"] == ["acme"]
        assert result["results"] == [{"name": "acme", "ok": False}]

    @pytest.mark.asyncio
    async def test_single_repo_only_refreshes_matching(self, cache_dir, monkeypatch):
        _write_external_registry_cache("acme", [{"name": "a1", "repo": "R"}])
        _write_external_registry_cache("other", [{"name": "b1", "repo": "R"}])
        other_path = _external_registry_cache_path("other")
        other_mtime = other_path.stat().st_mtime

        reg_a = MagicMock()
        reg_a.name = "acme"
        reg_a.repo = "https://github.com/acme/apps"
        reg_a.branch = "main"
        reg_b = MagicMock()
        reg_b.name = "other"
        reg_b.repo = "https://github.com/other/apps"
        reg_b.branch = "main"
        mock_config = MagicMock()
        mock_config.registries = [reg_a, reg_b]
        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            lambda: mock_config,
        )
        monkeypatch.setattr(
            "kiro_crew.apps.registry._fetch_external_registry_index",
            AsyncMock(return_value=[{"name": "a1", "repo": "R"}]),
        )
        monkeypatch.setattr(
            "kiro_crew.apps.registry.list_registry",
            AsyncMock(return_value=[]),
        )

        result = await refresh_registries(repo="https://github.com/acme/apps")

        assert result["refreshed"] == ["acme"]
        # The non-matching registry's cache is left completely untouched.
        assert other_path.stat().st_mtime == other_mtime

    @pytest.mark.asyncio
    async def test_no_registries_configured(self, cache_dir, monkeypatch):
        mock_config = MagicMock()
        mock_config.registries = []
        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            lambda: mock_config,
        )
        monkeypatch.setattr(
            "kiro_crew.apps.registry.list_registry",
            AsyncMock(return_value=[]),
        )
        result = await refresh_registries()
        assert result["ok"] is True
        assert result["refreshed"] == []
        assert result["failed"] == []
        assert result["apps"] == 0

    @pytest.mark.asyncio
    async def test_malformed_index_item_does_not_crash(self, cache_dir, monkeypatch):
        # A registry index containing a non-object item (e.g. ["oops"]) must not
        # crash normalization → HTTP 500; malformed items are dropped.
        mock_reg = MagicMock()
        mock_reg.name = "acme"
        mock_reg.repo = "https://github.com/acme/apps"
        mock_reg.branch = "main"
        mock_config = MagicMock()
        mock_config.registries = [mock_reg]
        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            lambda: mock_config,
        )
        monkeypatch.setattr(
            "kiro_crew.apps.registry._fetch_external_registry_index",
            AsyncMock(return_value=["oops", {"name": "good", "repo": "R"}, 42]),
        )
        monkeypatch.setattr(
            "kiro_crew.apps.registry.list_registry",
            AsyncMock(return_value=[{"name": "good"}]),
        )

        result = await refresh_registries()

        assert result["ok"] is True
        assert result["refreshed"] == ["acme"]
        # Only the well-formed object entry was cached.
        cached = _read_external_registry_cache("acme", ignore_ttl=True)
        assert cached == [
            {
                "name": "good",
                "repo": "R",
                "gitUrl": "https://github.com/acme/apps",
                "branch": "main",
                "_registry": "acme",
            }
        ]

    @pytest.mark.asyncio
    async def test_rejects_entries_with_unsafe_names(self, cache_dir, monkeypatch):
        # GPT 5.6 HIGH: an external registry index is untrusted. Entry names
        # that aren't valid kebab-case app names (path separators, ``..``
        # traversal, or an absolute path) must be dropped BEFORE caching, so a
        # hostile name can never reach ``app_source_dir(name)`` /
        # ``shutil.rmtree(dest)`` on the install path.
        mock_reg = MagicMock()
        mock_reg.name = "acme"
        mock_reg.repo = "https://github.com/acme/apps"
        mock_reg.branch = "main"
        mock_config = MagicMock()
        mock_config.registries = [mock_reg]
        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            lambda: mock_config,
        )
        monkeypatch.setattr(
            "kiro_crew.apps.registry._fetch_external_registry_index",
            AsyncMock(
                return_value=[
                    {"name": "good-app", "repo": "R"},
                    {"name": "../../victim", "repo": "R"},
                    {"name": "/tmp/victim", "repo": "R"},
                    {"name": "Has Spaces", "repo": "R"},
                    {"name": "UPPER", "repo": "R"},
                    {"name": "", "repo": "R"},
                    {"repo": "R"},  # missing name entirely
                ]
            ),
        )
        monkeypatch.setattr(
            "kiro_crew.apps.registry.list_registry",
            AsyncMock(return_value=[{"name": "good-app"}]),
        )

        result = await refresh_registries()

        assert result["ok"] is True
        cached = _read_external_registry_cache("acme", ignore_ttl=True)
        # Only the single kebab-case-valid entry survived; every unsafe name
        # was dropped before it could be cached or listed.
        assert [e["name"] for e in cached] == ["good-app"]

    @pytest.mark.asyncio
    async def test_single_repo_no_match_returns_not_found(self, cache_dir, monkeypatch):
        # GPT 5.6 MEDIUM: a caller-supplied repo that matches no configured
        # registry is a client error — refreshing nothing and returning
        # ``ok: true`` would mislead the client. Signal not_found (route -> 404).
        mock_reg = MagicMock()
        mock_reg.name = "acme"
        mock_reg.repo = "https://github.com/acme/apps"
        mock_reg.branch = "main"
        mock_config = MagicMock()
        mock_config.registries = [mock_reg]
        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            lambda: mock_config,
        )
        # list_registry must NOT be invoked on the not-found short-circuit.
        list_mock = AsyncMock(return_value=[])
        monkeypatch.setattr("kiro_crew.apps.registry.list_registry", list_mock)

        result = await refresh_registries(repo="https://github.com/nope/absent")

        assert result["ok"] is False
        assert result["not_found"] is True
        assert result["refreshed"] == []
        assert result["failed"] == []
        list_mock.assert_not_awaited()

    def test_manifest_cache_path_is_traversal_proof(self, cache_dir):
        # A hostile external-registry entry name must never resolve outside the
        # manifest cache dir (GPT 5.6 HIGH: `../../config` -> config.json unlink).
        import kiro_crew.apps.registry as _reg  # module attr = the patched dir

        cache_root = _reg._manifest_cache_dir().resolve()
        for hostile in ("../../config", "../../../etc/passwd", "a/b/c", "..%2F..%2Fconfig"):
            resolved = _manifest_cache_path(hostile).resolve()
            assert cache_root in resolved.parents, f"{hostile!r} escaped to {resolved}"

    def test_safe_cache_stem_preserves_plain_names(self):
        # Plain names stay byte-identical (no hash suffix) so caches persist.
        assert _safe_cache_stem("cool-app") == "cool-app"
        assert _safe_cache_stem("my_app.v2") == "my_app.v2"
        # Traversal / separator names are slugified AND hashed for uniqueness.
        assert _safe_cache_stem("../../config") != "../../config"
        assert "/" not in _safe_cache_stem("a/b")
        assert ".." not in _safe_cache_stem("../x")


# ---------------------------------------------------------------------------
# Clone-URL resolution for the blob proxy is no longer a standalone resolver.
# ``handle_blob_proxy`` resolves the clone URL once (from the decided entry via
# ``_entry_git_url`` for a bundled entry, or by an inline in-memory URL-form
# check on the already-validated ``repo`` for the no-entry external/federated
# branch) and threads it into ``_fetch_git_blob``; there is no
# ``routes._registry_git_url`` helper to unit-test in isolation.  The URL-form /
# no-bundled-entry resolution boundary this section used to cover is now
# exercised through the handler in test_apps_routes_coverage.py.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# is_clone_host_trusted -- SSRF gate: URL clones only from explicitly-trusted
# hosts (public forges + configured registries), immune to DNS rebinding.
# ---------------------------------------------------------------------------


class TestIsCloneHostTrusted:
    def _no_configured_hosts(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.apps.registry._configured_registry_hosts",
            frozenset,
        )

    def test_public_forge_https_is_trusted(self, monkeypatch):
        self._no_configured_hosts(monkeypatch)
        assert is_clone_host_trusted("https://github.com/org/app") is True
        assert is_clone_host_trusted("git@gitlab.com:org/app.git") is True

    def test_internal_host_injected_by_index_is_rejected(self, monkeypatch):
        # The core SSRF vector: an untrusted external registry index lists an
        # app repo pointing at the loopback/internal network. The host is not a
        # public forge and the owner never configured it, so it is refused.
        self._no_configured_hosts(monkeypatch)
        assert is_clone_host_trusted("https://127.0.0.1:8443/x") is False
        assert is_clone_host_trusted("https://localhost/x") is False
        assert is_clone_host_trusted("https://10.0.0.5/internal/app") is False

    def test_arbitrary_attacker_host_is_rejected(self, monkeypatch):
        self._no_configured_hosts(monkeypatch)
        assert is_clone_host_trusted("https://evil.example.com/x") is False

    def test_owner_configured_host_is_trusted(self, monkeypatch):
        # An internal forge the OWNER explicitly configured as a registry stays
        # trusted -- their deliberate trust decision (rebinding-proof: gated on
        # the hostname, not a resolvable IP).
        monkeypatch.setattr(
            "kiro_crew.apps.registry._configured_registry_hosts",
            lambda: frozenset({"git.internal.example"}),
        )
        assert is_clone_host_trusted("https://git.internal.example/org/app") is True
        assert is_clone_host_trusted("git@git.internal.example:org/app.git") is True
        # ...but only that host -- a sibling internal host is still refused.
        assert is_clone_host_trusted("https://other.internal.example/app") is False

    def test_bare_name_and_unparseable_are_untrusted(self, monkeypatch):
        # Bare legacy names have no URL host, so they are not a URL clone; the
        # bundled allowlist handles them. Unparseable URLs fail closed.
        self._no_configured_hosts(monkeypatch)
        assert is_clone_host_trusted("SomeBareName") is False
        assert is_clone_host_trusted("") is False
        assert is_clone_host_trusted("://nohost") is False


class TestFetchGitBlobSsrfGate:
    """The blob proxy must refuse to clone an index-injected internal host
    BEFORE spawning git -- the SSRF gate short-circuits _fetch_git_blob."""

    @pytest.mark.asyncio
    async def test_untrusted_host_refused_without_spawning_git(self, tmp_path, monkeypatch):
        from kiro_crew.apps import routes

        # A malicious external index resolved this repo to a loopback URL; the
        # caller threads that resolved clone URL in as ``git_url`` (the callee no
        # longer resolves it from ``repo``).
        # Guard: if the gate failed, this would raise instead of returning False.

        def _boom(*a, **k):
            raise AssertionError("git clone must not be spawned for an untrusted host")

        monkeypatch.setattr(routes.asyncio, "create_subprocess_exec", _boom)

        ok = await routes._fetch_git_blob(
            "https://127.0.0.1:9/x",
            "main",
            "icon.png",
            tmp_path / "out.png",
            git_url="https://127.0.0.1:9/x",
        )
        assert ok is False


# ---------------------------------------------------------------------------
# Install-path confused-deputy defense: credential-free clone for entries whose
# repo URL originates from an owner-configured EXTERNAL registry index.
# ---------------------------------------------------------------------------


class TestInstallPathCredentialPosture:
    """An app entry that came from an external index carries ``_registry`` and
    its ``repo`` URL is index-controlled; installing it must clone
    credential-free (anonymous_git_env + strict sandbox), while a bundled
    (curated) entry keeps the owner's ambient git identity via minimal_env."""

    @pytest.mark.asyncio
    async def test_index_originated_install_propagates_credential_free_flag(self):
        from kiro_crew.apps.registry import install_from_registry

        captured = {}

        async def _fake_clone_build(
            git_url, name, log_lines, branch="main", *, index_originated=False, **kwargs
        ):
            captured["index_originated"] = index_originated
            return {"ok": False, "error": "stop-after-clone-dispatch"}

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                # Entry carries the external-index provenance marker.
                return_value={
                    "name": "acme-app",
                    "repo": "https://github.com/acme/private-sibling.git",
                    "branch": "main",
                    "_registry": "acme",
                },
            ),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value={"name": "acme-app", "version": "1.0.0"}),
            ),
            patch(
                "kiro_crew.apps.registry._clone_build_app",
                new=_fake_clone_build,
            ),
        ):
            await install_from_registry("acme-app")

        assert captured.get("index_originated") is True

    @pytest.mark.asyncio
    async def test_bundled_install_keeps_owner_credentials(self):
        from kiro_crew.apps.registry import install_from_registry

        captured = {}

        async def _fake_clone_build(
            git_url, name, log_lines, branch="main", *, index_originated=False, **kwargs
        ):
            captured["index_originated"] = index_originated
            return {"ok": False, "error": "stop-after-clone-dispatch"}

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                # Bundled/curated entry — no ``_registry`` marker.
                return_value={
                    "name": "bundled-app",
                    "repo": "https://github.com/kirodotdev/bundled-app.git",
                    "branch": "main",
                },
            ),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value={"name": "bundled-app", "version": "1.0.0"}),
            ),
            patch(
                "kiro_crew.apps.registry._clone_build_app",
                new=_fake_clone_build,
            ),
        ):
            await install_from_registry("bundled-app")

        assert captured.get("index_originated") is False

    @pytest.mark.asyncio
    async def test_git_clone_or_pull_index_originated_uses_anonymous_env(self, tmp_path):
        import asyncio as _asyncio

        from kiro_crew.apps import registry as reg

        captured = {}

        def _fake_wrap_argv(argv, mode="standard"):
            captured["mode"] = mode
            return argv, None

        class _FakeProc:
            returncode = 0

            async def communicate(self):
                return (b"", None)

        async def _fake_exec(*args, **kwargs):
            captured["env"] = kwargs.get("env")
            return _FakeProc()

        dest = tmp_path / "clone-dest"  # does not exist → fresh-clone path
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch.object(_asyncio, "create_subprocess_exec", new=_fake_exec),
        ):
            err = await reg._git_clone_or_pull(
                "https://github.com/acme/private-sibling.git",
                "main",
                dest,
                [],
                index_originated=True,
            )

        assert err is None
        env = captured["env"]
        # Anonymous / credential-free markers must be present.
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert "GIT_CONFIG_GLOBAL" in env
        assert "BatchMode=yes" in env["GIT_SSH_COMMAND"]
        # No ambient SSH agent handed through.
        assert "SSH_AUTH_SOCK" not in env
        # Strict OS sandbox (~/.ssh hidden) is forced.
        assert captured["mode"] == "strict"

    @pytest.mark.asyncio
    async def test_git_clone_or_pull_owner_designated_uses_minimal_env(self, tmp_path):
        import asyncio as _asyncio

        from kiro_crew.apps import registry as reg

        captured = {}

        def _fake_wrap_argv(argv, mode="standard"):
            captured["mode"] = mode
            return argv, None

        class _FakeProc:
            returncode = 0

            async def communicate(self):
                return (b"", None)

        async def _fake_exec(*args, **kwargs):
            captured["env"] = kwargs.get("env")
            return _FakeProc()

        url = "https://github.com/kirodotdev/bundled-app.git"
        dest = tmp_path / "clone-dest"
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch.object(_asyncio, "create_subprocess_exec", new=_fake_exec),
        ):
            err = await reg._git_clone_or_pull(url, "main", dest, [], index_originated=False)

        assert err is None
        env = captured["env"]
        # minimal_env carries NONE of the anonymous credential-suppression keys.
        assert "GIT_CONFIG_NOSYSTEM" not in env
        assert "GIT_CONFIG_GLOBAL" not in env
        # Sandbox mode is the host-derived context decision, not forced strict.
        assert captured["mode"] == reg._context_clone_sandbox_mode(url)


# ---------------------------------------------------------------------------
# Same-repo credential carve-out (companion to confused-deputy defense).
# When an index entry's effective clone URL is byte-identical to the owner-
# configured registry repo URL, the clone uses owner credentials instead of
# the anonymous+strict posture. Sibling repos on the same host stay anonymous.
# ---------------------------------------------------------------------------


class TestSameRepoCredentialCarveOut:
    """The same-repo carve-out: owner-configured registry URL gets credentials;
    sibling repos on the same host remain anonymous+strict; bundled unchanged."""

    @pytest.mark.asyncio
    async def test_same_repo_install_uses_owner_credentials(self):
        """Entry whose clone URL == registry config repo → credentialed install."""
        from kiro_crew.apps.registry import install_from_registry

        captured = {}

        async def _fake_clone_build(
            git_url, name, log_lines, branch="main", *, index_originated=False, **kwargs
        ):
            captured["index_originated"] = index_originated
            return {"ok": False, "error": "stop-after-clone-dispatch"}

        mock_config = MagicMock()
        mock_config.registries = [
            SimpleNamespace(
                name="internal", repo="ssh://git.example.com/team/MyRegistry", branch="main"
            )
        ]

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={
                    "name": "my-app",
                    "repo": "ssh://git.example.com/team/MyRegistry",
                    "gitUrl": "ssh://git.example.com/team/MyRegistry",
                    "branch": "main",
                    "subdirectory": "apps/my-app",
                    "_registry": "internal",
                },
            ),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value={"name": "my-app", "version": "1.0.0"}),
            ),
            patch(
                "kiro_crew.apps.registry._clone_build_app",
                new=_fake_clone_build,
            ),
            patch(
                "kiro_crew.config.loader.KiroCrewConfig.load",
                return_value=mock_config,
            ),
        ):
            await install_from_registry("my-app")

        # Same-repo carve-out: index_originated flipped to False → credentialed.
        assert captured.get("index_originated") is False

    @pytest.mark.asyncio
    async def test_sibling_repo_same_host_stays_anonymous(self):
        """Entry pointing at a DIFFERENT repo on the same host → still anonymous."""
        from kiro_crew.apps.registry import install_from_registry

        captured = {}

        async def _fake_clone_build(
            git_url, name, log_lines, branch="main", *, index_originated=False, **kwargs
        ):
            captured["index_originated"] = index_originated
            return {"ok": False, "error": "stop-after-clone-dispatch"}

        mock_config = MagicMock()
        mock_config.registries = [
            SimpleNamespace(
                name="internal", repo="ssh://git.example.com/team/MyRegistry", branch="main"
            )
        ]

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                # repo points at a DIFFERENT package on the same host — the exact
                # confused-deputy scenario the defense exists for.
                return_value={
                    "name": "sibling-app",
                    "repo": "ssh://git.example.com/team/OtherRepo",
                    "gitUrl": "ssh://git.example.com/team/OtherRepo",
                    "branch": "main",
                    "_registry": "internal",
                },
            ),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value={"name": "sibling-app", "version": "1.0.0"}),
            ),
            patch(
                "kiro_crew.apps.registry._clone_build_app",
                new=_fake_clone_build,
            ),
            patch(
                "kiro_crew.config.loader.KiroCrewConfig.load",
                return_value=mock_config,
            ),
        ):
            await install_from_registry("sibling-app")

        # Sibling repo on the same host: confused-deputy defense applies.
        assert captured.get("index_originated") is True

    @pytest.mark.asyncio
    async def test_bundled_entry_unchanged_by_carve_out(self):
        """Bundled entry (no _registry marker) → still owner-designated."""
        from kiro_crew.apps.registry import install_from_registry

        captured = {}

        async def _fake_clone_build(
            git_url, name, log_lines, branch="main", *, index_originated=False, **kwargs
        ):
            captured["index_originated"] = index_originated
            return {"ok": False, "error": "stop-after-clone-dispatch"}

        mock_config = MagicMock()
        mock_config.registries = []

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={
                    "name": "bundled-app",
                    "repo": "https://github.com/kirodotdev/bundled-app.git",
                    "branch": "main",
                },
            ),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value={"name": "bundled-app", "version": "1.0.0"}),
            ),
            patch(
                "kiro_crew.apps.registry._clone_build_app",
                new=_fake_clone_build,
            ),
            patch(
                "kiro_crew.config.loader.KiroCrewConfig.load",
                return_value=mock_config,
            ),
        ):
            await install_from_registry("bundled-app")

        # Bundled entries have no _registry → index_originated stays False.
        assert captured.get("index_originated") is False

    @pytest.mark.asyncio
    async def test_same_repo_manifest_fetch_uses_owner_credentials(self):
        """_fetch_app_manifest with owner_designated=True uses minimal_env."""
        import asyncio as _asyncio

        from kiro_crew.apps import registry as reg

        captured = {}

        def _fake_wrap_argv(argv, mode="standard"):
            captured["mode"] = mode
            return argv, None

        class _FakeProc:
            returncode = 0

            async def communicate(self):
                return (b"", b"")

        async def _fake_exec(*args, **kwargs):
            captured["env"] = kwargs.get("env")
            return _FakeProc()

        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch.object(_asyncio, "create_subprocess_exec", new=_fake_exec),
            patch(
                "kiro_crew.apps.registry.app_source_dir",
                return_value=MagicMock(is_file=MagicMock(return_value=False)),
            ),
        ):
            await reg._fetch_app_manifest(
                "ssh://git.example.com/team/MyRegistry",
                "main",
                "",
                app_name="",
                git_url="ssh://git.example.com/team/MyRegistry",
                owner_designated=True,
            )

        # Owner-designated: minimal_env (no credential suppression) + context sandbox.
        env = captured["env"]
        assert "GIT_CONFIG_NOSYSTEM" not in env
        assert "GIT_CONFIG_GLOBAL" not in env
        # SSH host is trusted → context mode is standard (not forced strict).
        assert captured["mode"] == reg._context_clone_sandbox_mode(
            "ssh://git.example.com/team/MyRegistry"
        )

    @pytest.mark.asyncio
    async def test_default_manifest_fetch_stays_anonymous(self):
        """_fetch_app_manifest without owner_designated uses anonymous+strict."""
        import asyncio as _asyncio

        from kiro_crew.apps import registry as reg

        captured = {}

        def _fake_wrap_argv(argv, mode="standard"):
            captured["mode"] = mode
            return argv, None

        class _FakeProc:
            returncode = 0

            async def communicate(self):
                return (b"", b"")

        async def _fake_exec(*args, **kwargs):
            captured["env"] = kwargs.get("env")
            return _FakeProc()

        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch.object(_asyncio, "create_subprocess_exec", new=_fake_exec),
            patch(
                "kiro_crew.apps.registry.app_source_dir",
                return_value=MagicMock(is_file=MagicMock(return_value=False)),
            ),
        ):
            await reg._fetch_app_manifest(
                "ssh://git.example.com/team/SiblingRepo",
                "main",
                "",
                app_name="",
                git_url="ssh://git.example.com/team/SiblingRepo",
                owner_designated=False,
            )

        # Default (not owner-designated): anonymous env + strict sandbox.
        env = captured["env"]
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert "GIT_CONFIG_GLOBAL" in env
        assert "BatchMode=yes" in env["GIT_SSH_COMMAND"]
        assert "SSH_AUTH_SOCK" not in env
        assert captured["mode"] == "strict"

    @pytest.mark.asyncio
    async def test_same_repo_clone_or_pull_uses_owner_credentials(self, tmp_path):
        """_git_clone_or_pull with index_originated=False (same-repo carve-out)
        uses minimal_env + context sandbox — matching the existing
        test_git_clone_or_pull_owner_designated_uses_minimal_env test."""
        import asyncio as _asyncio

        from kiro_crew.apps import registry as reg

        captured = {}

        def _fake_wrap_argv(argv, mode="standard"):
            captured["mode"] = mode
            return argv, None

        class _FakeProc:
            returncode = 0

            async def communicate(self):
                return (b"", None)

        async def _fake_exec(*args, **kwargs):
            captured["env"] = kwargs.get("env")
            return _FakeProc()

        url = "ssh://git.example.com/team/MyRegistry"
        dest = tmp_path / "clone-dest"
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch.object(_asyncio, "create_subprocess_exec", new=_fake_exec),
        ):
            err = await reg._git_clone_or_pull(url, "main", dest, [], index_originated=False)

        assert err is None
        env = captured["env"]
        # Owner-designated: minimal_env without credential suppression.
        assert "GIT_CONFIG_NOSYSTEM" not in env
        assert "GIT_CONFIG_GLOBAL" not in env
        # Context sandbox mode for trusted SSH host → standard.
        assert captured["mode"] == reg._context_clone_sandbox_mode(url)

    def test_credential_grant_is_sel_audited(self):
        """The owner-designated credential escalation leaves an SEL record.

        Escalating from anonymous+strict to owner credentials is a
        permission decision; it must be auditable like the index fetch.
        """
        from kiro_crew.apps import registry as reg

        sel = MagicMock()
        with patch.object(reg, "_sel_fn", return_value=sel):
            reg._sel_credential_grant("fetch_app_manifest", "ssh://git.example.com/team/MyRegistry")

        sel.log_api_access.assert_called_once()
        kwargs = sel.log_api_access.call_args.kwargs
        assert kwargs["caller"] == "registry"
        assert kwargs["operation"] == "fetch_app_manifest"
        assert kwargs["outcome"] == "granted"
        assert "ssh://git.example.com/team/MyRegistry" in kwargs["resources"]

    def test_credential_grant_audit_is_best_effort(self):
        """A failing (or absent) SEL backend never breaks the clone path."""
        from kiro_crew.apps import registry as reg

        broken = MagicMock()
        broken.log_api_access.side_effect = RuntimeError("sel down")
        with patch.object(reg, "_sel_fn", return_value=broken):
            reg._sel_credential_grant("install_from_registry", "ssh://git.example.com/team/X")
        with patch.object(reg, "_sel_fn", None):
            reg._sel_credential_grant("install_from_registry", "ssh://git.example.com/team/X")

    def test_is_owner_designated_repo_exact_match(self):
        """Predicate returns True only for byte-identical match to config repo."""
        from kiro_crew.apps.registry import _is_owner_designated_repo

        mock_config = MagicMock()
        mock_config.registries = [
            SimpleNamespace(
                name="internal", repo="ssh://git.example.com/team/MyRegistry", branch="main"
            )
        ]

        entry_same = {
            "name": "app",
            "repo": "ssh://git.example.com/team/MyRegistry",
            "gitUrl": "ssh://git.example.com/team/MyRegistry",
            "_registry": "internal",
        }
        entry_sibling = {
            "name": "app",
            "repo": "ssh://git.example.com/team/OtherRepo",
            "gitUrl": "ssh://git.example.com/team/OtherRepo",
            "_registry": "internal",
        }
        entry_bundled = {
            "name": "app",
            "repo": "https://github.com/kirodotdev/app.git",
        }

        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=mock_config):
            assert _is_owner_designated_repo(entry_same) is True
            # Different repo on the same host → NOT owner-designated.
            assert _is_owner_designated_repo(entry_sibling) is False
            # Bundled (no _registry) → returns False (but irrelevant since
            # bundled entries never set index_originated=True).
            assert _is_owner_designated_repo(entry_bundled) is False

    def test_is_owner_designated_repo_no_normalization(self):
        """No URL normalization — trailing slash difference is NOT a match."""
        from kiro_crew.apps.registry import _is_owner_designated_repo

        mock_config = MagicMock()
        mock_config.registries = [
            SimpleNamespace(name="r", repo="ssh://git.example.com/team/Repo", branch="main")
        ]

        # Trailing slash → not byte-identical → not owner-designated.
        entry = {
            "name": "app",
            "gitUrl": "ssh://git.example.com/team/Repo/",
            "_registry": "r",
        }
        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=mock_config):
            assert _is_owner_designated_repo(entry) is False


# ---------------------------------------------------------------------------
# Operator-configured registry branch overrides per-app declarations (#3330)
# ---------------------------------------------------------------------------


class TestConfiguredBranchOverride:
    @pytest.mark.asyncio
    async def test_configured_branch_overrides_declared_branch(
        self, cache_dir, monkeypatch, caplog
    ):
        # A same-repo entry declaring its own branch (e.g. "main", written in
        # anticipation of an eventual merge) must NOT win over the branch the
        # operator configured — the index was read from the configured branch,
        # so the declared one describes a state that does not exist there yet.
        import kiro_crew.apps.registry as reg

        async def _fake_index(repo, branch):
            return [{"name": "eager-app", "subdirectory": "apps/eager", "branch": "main"}]

        monkeypatch.setattr(reg, "_fetch_external_registry_index", _fake_index)

        class _Reg:
            name = "acme"
            repo = "https://github.com/acme/apps"
            branch = "develop"

        with caplog.at_level(logging.WARNING, logger="kiro_crew.apps.registry"):
            entries = await reg._fetch_and_cache_external_registry(_Reg())
        assert entries[0]["branch"] == "develop"
        # The cached copy carries the override too — install reads the cache.
        cached = reg._read_external_registry_cache("acme", ignore_ttl=True)
        assert cached[0]["branch"] == "develop"
        # The divergence is logged, naming both branches and the entry.
        divergence_logs = [r for r in caplog.records if "declares branch" in r.getMessage()]
        assert len(divergence_logs) == 1
        msg = divergence_logs[0].getMessage()
        assert "eager-app" in msg and "'main'" in msg and "'develop'" in msg

    @pytest.mark.asyncio
    async def test_entry_without_branch_inherits_configured_branch(
        self, cache_dir, monkeypatch, caplog
    ):
        # An entry omitting a branch still inherits the configured one, and no
        # divergence warning fires for it.
        import kiro_crew.apps.registry as reg

        async def _fake_index(repo, branch):
            return [{"name": "plain-app", "subdirectory": "apps/plain"}]

        monkeypatch.setattr(reg, "_fetch_external_registry_index", _fake_index)

        class _Reg:
            name = "acme"
            repo = "https://github.com/acme/apps"
            branch = "develop"

        with caplog.at_level(logging.WARNING, logger="kiro_crew.apps.registry"):
            entries = await reg._fetch_and_cache_external_registry(_Reg())
        assert entries[0]["branch"] == "develop"
        assert not [r for r in caplog.records if "declares branch" in r.getMessage()]

    @pytest.mark.asyncio
    async def test_matching_declared_branch_does_not_warn(self, cache_dir, monkeypatch, caplog):
        # A declaration that AGREES with the configured branch is not a
        # divergence — the warning must fire only on a genuine mismatch.
        import kiro_crew.apps.registry as reg

        async def _fake_index(repo, branch):
            return [{"name": "same-app", "subdirectory": "apps/same", "branch": "develop"}]

        monkeypatch.setattr(reg, "_fetch_external_registry_index", _fake_index)

        class _Reg:
            name = "acme"
            repo = "https://github.com/acme/apps"
            branch = "develop"

        with caplog.at_level(logging.WARNING, logger="kiro_crew.apps.registry"):
            entries = await reg._fetch_and_cache_external_registry(_Reg())
        assert entries[0]["branch"] == "develop"
        assert not [r for r in caplog.records if "declares branch" in r.getMessage()]

    @pytest.mark.asyncio
    async def test_cross_repo_entry_keeps_declared_branch(self, cache_dir, monkeypatch, caplog):
        # A cross-repo entry's declared branch names a ref in ANOTHER
        # repository, about which the configured registry branch carries no
        # information. The override must not touch it (and must not warn) —
        # forcing reg.branch there would clone a ref the app repo may not have.
        import kiro_crew.apps.registry as reg

        async def _fake_index(repo, branch):
            return [
                {
                    "name": "sibling-app",
                    "gitUrl": "https://github.com/acme/other-repo",
                    "subdirectory": "apps/sibling",
                    "branch": "main",
                }
            ]

        monkeypatch.setattr(reg, "_fetch_external_registry_index", _fake_index)

        class _Reg:
            name = "acme"
            repo = "https://github.com/acme/apps"
            branch = "develop"

        with caplog.at_level(logging.WARNING, logger="kiro_crew.apps.registry"):
            entries = await reg._fetch_and_cache_external_registry(_Reg())
        assert entries[0]["branch"] == "main"
        assert not [r for r in caplog.records if "declares branch" in r.getMessage()]

    @pytest.mark.asyncio
    async def test_cross_repo_entry_without_branch_inherits(self, cache_dir, monkeypatch):
        # A cross-repo entry with no usable declared branch (absent or an
        # explicit JSON null) still inherits the configured branch, so None
        # can never flow to the clone coordinates.
        import kiro_crew.apps.registry as reg

        async def _fake_index(repo, branch):
            return [
                {
                    "name": "bare-app",
                    "gitUrl": "https://github.com/acme/other-repo",
                    "subdirectory": "apps/bare",
                    "branch": None,
                }
            ]

        monkeypatch.setattr(reg, "_fetch_external_registry_index", _fake_index)

        class _Reg:
            name = "acme"
            repo = "https://github.com/acme/apps"
            branch = "develop"

        entries = await reg._fetch_and_cache_external_registry(_Reg())
        assert entries[0]["branch"] == "develop"

    def test_stale_cache_branch_repaired_on_direct_lookup(self, cache_dir, monkeypatch):
        # A cache written before this policy existed (or before the operator
        # changed the registry's configured branch) still carries the old
        # per-app branch. The direct install lookup reads the cache without a
        # listing refresh (ignore_ttl), so the repair must happen at read time.
        from types import SimpleNamespace

        import kiro_crew.apps.registry as reg

        reg._write_external_registry_cache(
            "acme",
            [
                {
                    "name": "legacy-app",
                    "gitUrl": "https://github.com/acme/apps",
                    "repo": "https://github.com/acme/apps",
                    "subdirectory": "apps/legacy",
                    "branch": "main",
                }
            ],
        )
        mock_config = MagicMock()
        mock_config.registries = [
            SimpleNamespace(name="acme", repo="https://github.com/acme/apps", branch="develop")
        ]
        monkeypatch.setattr(
            "kiro_crew.apps.registry._load_registry_file",
            lambda: [],
        )
        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            lambda: mock_config,
        )

        result = get_registry_app("legacy-app")
        assert result is not None
        assert result["branch"] == "develop"


# ---------------------------------------------------------------------------
# Untrusted index `subdirectory` path-traversal gate (CWE-22 → RCE).
# ---------------------------------------------------------------------------


class TestRegistrySubdirTraversalGate:
    def test_safe_subdirs_accepted(self):
        from kiro_crew.apps.registry import _is_safe_registry_subdir

        for ok in ["", None, "apps", "apps/widget", "a/b/c", ".config", "v2.0"]:
            assert _is_safe_registry_subdir(ok) is True, ok

    def test_unsafe_subdirs_rejected(self):
        from kiro_crew.apps.registry import _is_safe_registry_subdir

        for bad in [
            "/etc",
            "/tmp/victim",
            "../../victim",
            "apps/../../etc",
            "..",
            ".",
            "a/./b",
            "C:\\Windows",
            "a\\b",
            "with\x00nul",
            123,
            ["not", "a", "str"],
        ]:
            assert _is_safe_registry_subdir(bad) is False, bad

    def test_contained_join_blocks_symlink_escape(self, tmp_path):
        import os

        from kiro_crew.apps.registry import _contained_join

        root = tmp_path / "clone"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        # A hostile clone ships a symlink pointing outside the clone root.
        link = root / "sub"
        os.symlink(outside, link)
        assert _contained_join(root, "sub") is None
        # A genuine contained subdir resolves fine.
        (root / "real").mkdir()
        assert _contained_join(root, "real") == (root / "real").resolve()
        # Empty subdir returns the root unchanged.
        assert _contained_join(root, "") == root

    @pytest.mark.asyncio
    async def test_fresh_fetch_drops_unsafe_subdir_entry(self, cache_dir, monkeypatch):
        # An index that lists an app with a traversing subdirectory must have
        # that entry dropped before it is cached or listed.
        import kiro_crew.apps.registry as reg

        async def _fake_index(repo, branch):
            return [
                {"name": "good-app", "repo": repo, "subdirectory": "apps/good"},
                {"name": "evil-app", "repo": repo, "subdirectory": "../../etc"},
            ]

        monkeypatch.setattr(reg, "_fetch_external_registry_index", _fake_index)

        class _Reg:
            name = "acme"
            repo = "https://github.com/acme/apps"
            branch = "main"

        entries = await reg._fetch_and_cache_external_registry(_Reg())
        names = {e["name"] for e in entries}
        assert "good-app" in names
        assert "evil-app" not in names

    def test_cache_read_drops_unsafe_subdir_entry(self, cache_dir):
        # Even a hand-tampered cache file with an absolute subdirectory is
        # filtered on read (the single read chokepoint).
        from kiro_crew.apps.registry import (
            _read_external_registry_cache,
            _write_external_registry_cache,
        )

        _write_external_registry_cache(
            "acme",
            [
                {"name": "good-app", "repo": "r", "subdirectory": "sub"},
                {"name": "evil-app", "repo": "r", "subdirectory": "/etc"},
            ],
        )
        got = _read_external_registry_cache("acme", ignore_ttl=True)
        names = {e["name"] for e in got}
        assert names == {"good-app"}

    @pytest.mark.asyncio
    async def test_install_refuses_symlink_escape_subdir(self, tmp_path):
        # Defense-in-depth: even if a traversing subdir reached install (e.g. a
        # symlink inside the clone), _contained_join refuses it at use time.
        import os

        from kiro_crew.apps.registry import install_from_registry

        pkg_dir = tmp_path / "src"
        pkg_dir.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "app.json").write_text('{"name": "evil"}', encoding="utf-8")
        os.symlink(outside, pkg_dir / "sub")

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={
                    "name": "evil-app",
                    "repo": "https://github.com/acme/apps.git",
                    "branch": "main",
                    "subdirectory": "sub",
                    "_registry": "acme",
                },
            ),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value={"name": "evil-app", "version": "1.0.0"}),
            ),
            patch(
                "kiro_crew.apps.registry._clone_build_app",
                new=AsyncMock(return_value={"ok": True, "pkg_dir": pkg_dir}),
            ),
        ):
            result = await install_from_registry("evil-app")

        assert not result["ok"]
        assert "unsafe subdirectory" in result["error"]


def _real_argv(captured_argv):
    """Strip the spawn shim's own prologue from a captured argv.

    ``create_subprocess_limited`` runs commands through the post-exec shim
    (``python -I -S -c <shim> --rlimits=… -- <real argv>``), so a captured
    spawn no longer starts with the command itself. Return the argv after the
    ``--`` separator, with argv[0] reduced to its basename (git resolves to an
    absolute path, and to ``git.EXE`` on Windows).
    """
    argv = list(captured_argv)
    if "--" in argv:
        argv = argv[argv.index("--") + 1 :]
    if argv:
        argv[0] = os.path.basename(argv[0]).lower().removesuffix(".exe")
    return argv


class TestStaleCloneOriginVerification:
    """A persisted clone's origin must be verified before any pull.

    The credential posture is decided from git_url, but `git pull origin`
    talks to the CLONE's origin. A stale clone (e.g. a registry replaced
    with the same app name) must be discarded, never pulled credentialed.
    """

    @staticmethod
    def _make_fake_exec(captured, origin_url):
        class _FakeProc:
            returncode = 0

            def __init__(self, out=b""):
                self._out = out

            async def communicate(self):
                return (self._out, None)

        async def _fake_exec(*args, **kwargs):
            captured.setdefault("calls", []).append(list(args))
            # The origin read is sandbox-routed + shim-wrapped now, so match on
            # the real argv rather than the raw prefix.
            if _real_argv(args)[:4] == ["git", "remote", "get-url", "origin"]:
                return _FakeProc(origin_url.encode() + b"\n")
            return _FakeProc()

        return _fake_exec

    @pytest.mark.asyncio
    async def test_mismatched_origin_discards_clone_and_reclones(self, tmp_path):
        import asyncio as _asyncio

        from kiro_crew.apps import registry as reg

        dest = tmp_path / "app"
        (dest / ".git").mkdir(parents=True)  # looks like an existing clone

        captured: dict = {}
        vetted_url = "ssh://git.example.com/team/MyRegistry"
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch(
                "kiro_crew.apps.registry.wrap_argv",
                side_effect=lambda a, mode="standard": (a, None),
            ),
            # cgroup_scope_argv wraps the argv on Linux (identity on macOS) —
            # pin it so the captured commands are platform-independent.
            patch("kiro_crew.apps.registry.cgroup_scope_argv", side_effect=lambda a: a),
            patch.object(
                _asyncio,
                "create_subprocess_exec",
                new=self._make_fake_exec(captured, "ssh://git.example.com/team/OldSibling"),
            ),
        ):
            err = await reg._git_clone_or_pull(vetted_url, "main", dest, [], index_originated=False)

        assert err is None
        argvs = [_real_argv(c) for c in captured["calls"]]
        # Stale clone was removed (rmtree) and a FRESH clone from the vetted
        # URL ran — never a pull against the mismatched origin.
        assert not dest.exists()
        assert any(a[:2] == ["git", "clone"] and vetted_url in a for a in argvs)
        assert not any(a[:2] == ["git", "pull"] for a in argvs), argvs

    @pytest.mark.asyncio
    async def test_matching_origin_pulls_in_place(self, tmp_path):
        import asyncio as _asyncio

        from kiro_crew.apps import registry as reg

        dest = tmp_path / "app"
        (dest / ".git").mkdir(parents=True)

        captured: dict = {}
        vetted_url = "ssh://git.example.com/team/MyRegistry"
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch(
                "kiro_crew.apps.registry.wrap_argv",
                side_effect=lambda a, mode="standard": (a, None),
            ),
            # cgroup_scope_argv wraps the argv on Linux (identity on macOS) —
            # pin it so the captured commands are platform-independent.
            patch("kiro_crew.apps.registry.cgroup_scope_argv", side_effect=lambda a: a),
            patch.object(
                _asyncio,
                "create_subprocess_exec",
                new=self._make_fake_exec(captured, vetted_url),
            ),
        ):
            err = await reg._git_clone_or_pull(vetted_url, "main", dest, [], index_originated=False)

        assert err is None
        assert dest.exists()
        argvs = [_real_argv(c) for c in captured["calls"]]
        assert any(a[:2] == ["git", "pull"] for a in argvs), argvs
        assert not any(a[:2] == ["git", "clone"] for a in argvs)


class TestManifestOriginGate:
    """The manifest fed to admission must describe the repo that gets cloned.

    ``_fetch_app_manifest`` shortcuts to the persisted clone's ``app.json``,
    but that clone is keyed on app NAME only. When a registry is replaced, a
    checkout of a different repo can sit under the same name — and
    ``_git_clone_or_pull`` then discards it and re-clones from the new URL.
    Reusing the stale ``app.json`` would admit repo A's manifest and run repo
    B's code, so the shortcut only applies when the clone's origin matches.
    """

    @staticmethod
    def _fake_spawn(captured, origin_url, origin_rc=0):
        """One fake for both spawns this path makes.

        The origin read and the manifest clone both go through
        ``create_subprocess_limited``, so the fake discriminates on the argv:
        an origin read answers with *origin_url* (or fails with *origin_rc*),
        anything else is recorded as a clone attempt.
        """

        class _Proc:
            def __init__(self, out=b"", rc=0):
                self._out = out
                self.returncode = rc

            async def communicate(self):
                return (self._out, b"")

        async def _spawn(*args, **kwargs):
            argv = list(args)
            if argv[:4] == ["git", "remote", "get-url", "origin"]:
                captured.setdefault("origin_reads", []).append(argv)
                if origin_rc != 0:
                    return _Proc(b"", origin_rc)
                return _Proc(origin_url.encode() + b"\n")
            captured.setdefault("clones", []).append(argv)
            return _Proc()

        return _spawn

    def _seed_stale_clone(self, tmp_path, marker):
        clone_dir = tmp_path / "app-sources" / "myapp"
        (clone_dir / ".git").mkdir(parents=True)
        (clone_dir / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (clone_dir / "app.json").write_text(
            json.dumps({"name": "myapp", "version": marker}), encoding="utf-8"
        )
        return clone_dir

    @staticmethod
    def _patches(clone_dir, spawn):
        """Common patch set: identity sandbox wrappers + the single spawn fake.

        wrap_argv / cgroup_scope_argv are pinned to identity so the argv the
        fake sees is the real command, platform-independently.
        """
        return (
            patch("kiro_crew.apps.registry.app_source_dir", return_value=clone_dir),
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch(
                "kiro_crew.apps.registry.wrap_argv",
                side_effect=lambda a, mode="standard": (a, None),
            ),
            patch("kiro_crew.apps.registry.cgroup_scope_argv", side_effect=lambda a: a),
            patch("kiro_crew.apps.registry.create_subprocess_limited", new=spawn),
        )

    @pytest.mark.asyncio
    async def test_mismatched_origin_does_not_reuse_persisted_manifest(self, tmp_path):
        """A stale clone's app.json must never be handed to the admission gate."""
        from kiro_crew.apps import registry as reg

        clone_dir = self._seed_stale_clone(tmp_path, "STALE")
        vetted_url = "ssh://git.example.com/team/NewRepo"
        captured: dict = {}
        # clone left behind by the REPLACED registry
        spawn = self._fake_spawn(captured, "ssh://git.example.com/team/OldRepo")

        p1, p2, p3, p4, p5 = self._patches(clone_dir, spawn)
        with p1, p2, p3, p4, p5:
            manifest = await reg._fetch_app_manifest(
                "team/NewRepo", "main", app_name="myapp", git_url=vetted_url
            )

        # The stale manifest was NOT reused...
        assert manifest is None or manifest.get("version") != "STALE"
        # ...and a fresh clone of the vetted URL was attempted instead.
        assert any(vetted_url in c for c in captured.get("clones", []))

    @pytest.mark.asyncio
    async def test_matching_origin_reuses_persisted_manifest(self, tmp_path):
        """The fast path still works when the clone really is that repo."""
        from kiro_crew.apps import registry as reg

        clone_dir = self._seed_stale_clone(tmp_path, "CURRENT")
        vetted_url = "ssh://git.example.com/team/NewRepo"
        captured: dict = {}
        spawn = self._fake_spawn(captured, vetted_url)

        p1, p2, p3, p4, p5 = self._patches(clone_dir, spawn)
        with p1, p2, p3, p4, p5:
            manifest = await reg._fetch_app_manifest(
                "team/NewRepo", "main", app_name="myapp", git_url=vetted_url
            )

        assert manifest is not None and manifest["version"] == "CURRENT"
        # No network clone needed.
        assert not captured.get("clones")

    @pytest.mark.asyncio
    async def test_unreadable_origin_fails_closed(self, tmp_path):
        """An origin that cannot be read is treated as a mismatch, not a match."""
        from kiro_crew.apps import registry as reg

        clone_dir = self._seed_stale_clone(tmp_path, "STALE")
        vetted_url = "ssh://git.example.com/team/NewRepo"
        captured: dict = {}
        spawn = self._fake_spawn(captured, "", origin_rc=128)

        p1, p2, p3, p4, p5 = self._patches(clone_dir, spawn)
        with p1, p2, p3, p4, p5:
            manifest = await reg._fetch_app_manifest(
                "team/NewRepo", "main", app_name="myapp", git_url=vetted_url
            )

        assert manifest is None or manifest.get("version") != "STALE"


class TestStaleCloneDeletionFailsClosed:
    """A stale clone that cannot be moved aside must abort, never be pulled.

    The move-aside rename can fail (a locked ``.git/index.lock`` on Windows, a
    permission error). If the install continued, the surviving clone would be
    pulled from its OWN unverified origin under the credential posture decided
    for the vetted URL, and built under an admission decision made for a
    different repository.
    """

    @pytest.mark.asyncio
    async def test_surviving_stale_clone_aborts_the_install(self, tmp_path):
        from kiro_crew.apps import registry as reg

        dest = tmp_path / "app"
        (dest / ".git").mkdir(parents=True)

        captured: dict = {}

        class _Proc:
            returncode = 0

            def __init__(self, out=b""):
                self._out = out

            async def communicate(self):
                return (self._out, b"")

        async def _spawn(*args, **kwargs):
            argv = list(args)
            captured.setdefault("calls", []).append(argv)
            if argv[:4] == ["git", "remote", "get-url", "origin"]:
                return _Proc(b"ssh://git.example.com/team/OldRepo\n")
            return _Proc()

        original_rename = Path.rename

        def _failing_rename(self_path, target):
            if ".stale-" in str(target):
                raise OSError("Permission denied: locked files")
            return original_rename(self_path, target)

        log_lines: list = []
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch(
                "kiro_crew.apps.registry.wrap_argv",
                side_effect=lambda a, mode="standard": (a, None),
            ),
            patch("kiro_crew.apps.registry.cgroup_scope_argv", side_effect=lambda a: a),
            patch("kiro_crew.apps.registry.create_subprocess_limited", new=_spawn),
            # rename fails (Windows lock / permissions)
            patch.object(Path, "rename", _failing_rename),
        ):
            err = await reg._git_clone_or_pull(
                "ssh://git.example.com/team/NewRepo", "main", dest, log_lines
            )

        # Fails closed with an error dict...
        assert err is not None and not err["ok"]
        assert err["code"] == "stale_clone_not_removed"
        # ...and never pulls or clones over the surviving stale checkout.
        assert not any(c[:2] == ["git", "pull"] for c in captured["calls"])
        assert not any(c[:2] == ["git", "clone"] for c in captured["calls"])


class TestOriginMismatchDeleteOrder:
    """Regression tests for the delete-order fix: origin-mismatch must move
    aside the old checkout BEFORE cloning, and delete AFTER success.  On clone
    failure/timeout, the old checkout is RESTORED so local changes survive.
    """

    @pytest.mark.asyncio
    async def test_failed_reclone_preserves_old_checkout(self, tmp_path):
        """Origin mismatch + FAILED fresh clone → old checkout still present
        at dest, error dict returned."""
        import kiro_crew.apps.registry as reg

        stale_url = "https://old-origin.example.com/app.git"
        new_url = "https://new-origin.example.com/app.git"

        dest = tmp_path / "app-sources" / "myapp"
        dest.mkdir(parents=True)
        git_dir = dest / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text(
            f'[remote "origin"]\n\turl = {stale_url}\n',
            encoding="utf-8",
        )
        # Marker proving old content survived.
        (dest / "local-changes.txt").write_text("precious", encoding="utf-8")

        def _fake_wrap_argv(argv, mode="standard"):
            return list(argv), None

        class _FailProc:
            returncode = 128  # simulate git clone failure

            async def communicate(self):
                return (b"fatal: remote not found", None)

        async def _fake_create_subprocess(*args, **kwargs):
            # Simulate: dest was moved aside, clone attempted, git fails.
            dest.mkdir(parents=True, exist_ok=True)
            return _FailProc()

        log_lines: list[str] = []
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch(
                "kiro_crew.apps.registry.create_subprocess_limited",
                side_effect=_fake_create_subprocess,
            ),
            patch(
                "kiro_crew.apps.registry._clone_origin_url",
                new=AsyncMock(return_value=stale_url),
            ),
        ):
            err = await reg._git_clone_or_pull(
                new_url,
                "main",
                dest,
                log_lines,
                index_originated=False,
            )

        # Must return error.
        assert err is not None
        assert err["ok"] is False
        assert err["error"] == "git clone failed"
        # The old checkout content MUST still be at dest (restored).
        assert dest.is_dir()
        assert (dest / "local-changes.txt").exists()
        assert (dest / "local-changes.txt").read_text() == "precious"
        # No leftover stale-* dirs visible.
        stale_dirs = [p for p in dest.parent.iterdir() if ".stale-" in p.name]
        assert len(stale_dirs) == 0

    @pytest.mark.asyncio
    async def test_successful_reclone_replaces_checkout(self, tmp_path):
        """Origin mismatch + successful fresh clone → dest contains the new
        clone, moved-aside path deferred to pending_cleanup."""
        import kiro_crew.apps.registry as reg

        stale_url = "https://old-origin.example.com/app.git"
        new_url = "https://new-origin.example.com/app.git"

        dest = tmp_path / "app-sources" / "myapp"
        dest.mkdir(parents=True)
        git_dir = dest / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text(
            f'[remote "origin"]\n\turl = {stale_url}\n',
            encoding="utf-8",
        )
        (dest / "old-file.txt").write_text("old", encoding="utf-8")

        def _fake_wrap_argv(argv, mode="standard"):
            return list(argv), None

        class _SuccessProc:
            returncode = 0

            async def communicate(self):
                return (b"Cloning into...", None)

        async def _fake_create_subprocess(*args, **kwargs):
            # Simulate successful clone: create .git in dest.
            dest.mkdir(parents=True, exist_ok=True)
            new_git = dest / ".git"
            new_git.mkdir(parents=True, exist_ok=True)
            (new_git / "config").write_text(
                f'[remote "origin"]\n\turl = {new_url}\n',
                encoding="utf-8",
            )
            (dest / "new-file.txt").write_text("new", encoding="utf-8")
            return _SuccessProc()

        log_lines: list[str] = []
        pending_cleanup: list[Path] = []
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch(
                "kiro_crew.apps.registry.create_subprocess_limited",
                side_effect=_fake_create_subprocess,
            ),
            patch(
                "kiro_crew.apps.registry._clone_origin_url",
                new=AsyncMock(return_value=stale_url),
            ),
        ):
            err = await reg._git_clone_or_pull(
                new_url,
                "main",
                dest,
                log_lines,
                index_originated=False,
                pending_cleanup=pending_cleanup,
            )

        # Success.
        assert err is None
        # New clone content present.
        assert (dest / "new-file.txt").exists()
        assert (dest / "new-file.txt").read_text() == "new"
        # Old file gone (was in the moved-aside dir).
        assert not (dest / "old-file.txt").exists()
        # moved-aside dir deferred to pending_cleanup (not deleted yet).
        assert len(pending_cleanup) == 1
        assert pending_cleanup[0].exists()
        assert ".stale-" in pending_cleanup[0].name

    @pytest.mark.asyncio
    async def test_move_aside_failure_returns_stale_clone_not_removed(self, tmp_path):
        """Move-aside failure (mock rename to raise) → stale_clone_not_removed
        error, mismatched clone never pulled/built."""
        import kiro_crew.apps.registry as reg

        stale_url = "https://old-origin.example.com/app.git"
        new_url = "https://new-origin.example.com/app.git"

        dest = tmp_path / "app-sources" / "myapp"
        dest.mkdir(parents=True)
        git_dir = dest / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text(
            f'[remote "origin"]\n\turl = {stale_url}\n',
            encoding="utf-8",
        )
        (dest / "intact.txt").write_text("do not touch", encoding="utf-8")

        original_rename = Path.rename

        def _failing_rename(self_path, target):
            if ".stale-" in str(target):
                raise OSError("Permission denied: locked files")
            return original_rename(self_path, target)

        def _fake_wrap_argv(argv, mode="standard"):
            return list(argv), None

        # Mock create_subprocess_limited to simulate `git remote get-url origin`
        # returning the stale_url (used by _clone_origin_url before the rename).
        class _OriginProc:
            returncode = 0

            async def communicate(self):
                return (stale_url.encode() + b"\n", None)

        log_lines: list[str] = []
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch(
                "kiro_crew.apps.registry.create_subprocess_limited",
                side_effect=AsyncMock(return_value=_OriginProc()),
            ),
            patch.object(Path, "rename", _failing_rename),
        ):
            err = await reg._git_clone_or_pull(
                new_url,
                "main",
                dest,
                log_lines,
                index_originated=False,
            )

        # Must return stale_clone_not_removed error.
        assert err is not None
        assert err["ok"] is False
        assert err["code"] == "stale_clone_not_removed"
        # The original clone is UNTOUCHED — no data loss.
        assert (dest / "intact.txt").exists()
        assert (dest / "intact.txt").read_text() == "do not touch"

    @pytest.mark.asyncio
    async def test_timeout_reclone_preserves_old_checkout(self, tmp_path):
        """Origin mismatch + clone TIMEOUT → old checkout still present at
        dest (same preservation guarantee as the failure path)."""
        import asyncio as _asyncio

        import kiro_crew.apps.registry as reg

        stale_url = "https://old-origin.example.com/app.git"
        new_url = "https://new-origin.example.com/app.git"

        dest = tmp_path / "app-sources" / "myapp"
        dest.mkdir(parents=True)
        git_dir = dest / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text(
            f'[remote "origin"]\n\turl = {stale_url}\n',
            encoding="utf-8",
        )
        (dest / "local-changes.txt").write_text("precious", encoding="utf-8")

        def _fake_wrap_argv(argv, mode="standard"):
            return list(argv), None

        class _HangingProc:
            returncode = None
            pid = 99999

            async def communicate(self):
                raise _asyncio.TimeoutError()

            def kill(self):
                pass

            async def wait(self):
                self.returncode = -9

        async def _fake_create_subprocess(*args, **kwargs):
            # Simulate: dest created (partial clone) then timeout.
            dest.mkdir(parents=True, exist_ok=True)
            return _HangingProc()

        log_lines: list[str] = []
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch(
                "kiro_crew.apps.registry.create_subprocess_limited",
                side_effect=_fake_create_subprocess,
            ),
            patch("kiro_crew.apps.registry._kill_process_group", new=AsyncMock()),
            patch(
                "kiro_crew.apps.registry._clone_origin_url",
                new=AsyncMock(return_value=stale_url),
            ),
        ):
            err = await reg._git_clone_or_pull(
                new_url,
                "main",
                dest,
                log_lines,
                index_originated=False,
            )

        # Must return timeout error.
        assert err is not None
        assert err["ok"] is False
        assert "timed out" in err["error"]
        # The old checkout content MUST be restored at dest.
        assert dest.is_dir()
        assert (dest / "local-changes.txt").exists()
        assert (dest / "local-changes.txt").read_text() == "precious"
        # No leftover stale-* dirs.
        stale_dirs = [p for p in dest.parent.iterdir() if ".stale-" in p.name]
        assert len(stale_dirs) == 0

    @pytest.mark.asyncio
    async def test_spawn_exception_restores_old_checkout(self, tmp_path):
        """Origin mismatch + create_subprocess_limited RAISES (spawn failure)
        → old checkout restored at dest, no stale-* leftover.

        Regression: prior to the try/finally guard, a spawn failure after
        move-aside would strand the old checkout under .stale-* permanently.
        """
        import kiro_crew.apps.registry as reg

        stale_url = "https://old-origin.example.com/app.git"
        new_url = "https://new-origin.example.com/app.git"

        dest = tmp_path / "app-sources" / "myapp"
        dest.mkdir(parents=True)
        git_dir = dest / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text(
            f'[remote "origin"]\n\turl = {stale_url}\n',
            encoding="utf-8",
        )
        # Marker proving old content survived.
        (dest / "local-changes.txt").write_text("precious", encoding="utf-8")

        def _fake_wrap_argv(argv, mode="standard"):
            return list(argv), None

        # First call: _clone_origin_url reads the stale origin.
        # Second call: the fresh-clone create_subprocess_limited raises.
        call_count = 0

        class _OriginProc:
            returncode = 0

            async def communicate(self):
                return (stale_url.encode() + b"\n", None)

        async def _subprocess_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # _clone_origin_url: return stale_url
                return _OriginProc()
            # Fresh-clone spawn: simulate OSError (e.g. exec not found)
            raise OSError("No such file or directory: 'git'")

        log_lines: list[str] = []
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch(
                "kiro_crew.apps.registry.create_subprocess_limited",
                side_effect=_subprocess_side_effect,
            ),
        ):
            with pytest.raises(OSError, match="No such file"):
                await reg._git_clone_or_pull(
                    new_url,
                    "main",
                    dest,
                    log_lines,
                    index_originated=False,
                )

        # The old checkout content MUST be restored at dest despite the exception.
        assert dest.is_dir()
        assert (dest / "local-changes.txt").exists()
        assert (dest / "local-changes.txt").read_text() == "precious"
        # No leftover stale-* dirs.
        stale_dirs = [p for p in dest.parent.iterdir() if ".stale-" in p.name]
        assert len(stale_dirs) == 0

    @pytest.mark.asyncio
    async def test_cancellation_restores_old_checkout(self, tmp_path):
        """Origin mismatch + CancelledError during clone → old checkout
        restored at dest.

        Regression: CancelledError must also trigger the try/finally restore
        path (it propagates through the outer try without hitting the timeout
        or returncode handlers).
        """
        import asyncio as _asyncio

        import kiro_crew.apps.registry as reg

        stale_url = "https://old-origin.example.com/app.git"
        new_url = "https://new-origin.example.com/app.git"

        dest = tmp_path / "app-sources" / "myapp"
        dest.mkdir(parents=True)
        git_dir = dest / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text(
            f'[remote "origin"]\n\turl = {stale_url}\n',
            encoding="utf-8",
        )
        (dest / "local-changes.txt").write_text("precious", encoding="utf-8")

        def _fake_wrap_argv(argv, mode="standard"):
            return list(argv), None

        call_count = 0

        class _OriginProc:
            returncode = 0

            async def communicate(self):
                return (stale_url.encode() + b"\n", None)

        async def _subprocess_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _OriginProc()
            # Simulate cancellation during the clone spawn.
            raise _asyncio.CancelledError()

        log_lines: list[str] = []
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch(
                "kiro_crew.apps.registry.create_subprocess_limited",
                side_effect=_subprocess_side_effect,
            ),
        ):
            with pytest.raises(_asyncio.CancelledError):
                await reg._git_clone_or_pull(
                    new_url,
                    "main",
                    dest,
                    log_lines,
                    index_originated=False,
                )

        # The old checkout content MUST be restored at dest.
        assert dest.is_dir()
        assert (dest / "local-changes.txt").exists()
        assert (dest / "local-changes.txt").read_text() == "precious"
        # No leftover stale-* dirs.
        stale_dirs = [p for p in dest.parent.iterdir() if ".stale-" in p.name]
        assert len(stale_dirs) == 0

    @pytest.mark.asyncio
    async def test_cancellation_during_communicate_kills_process(self, tmp_path):
        """Origin mismatch + CancelledError during proc.communicate() → process
        is killed, partial dest removed, old checkout restored.

        Regression test for GPT 5.6 finding: cancellation after the clone
        process starts must not leave the process running or the old checkout
        stranded.
        """
        import asyncio as _asyncio

        import kiro_crew.apps.registry as reg

        stale_url = "https://old-origin.example.com/app.git"
        new_url = "https://new-origin.example.com/app.git"

        dest = tmp_path / "app-sources" / "myapp"
        dest.mkdir(parents=True)
        git_dir = dest / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text(
            f'[remote "origin"]\n\turl = {stale_url}\n',
            encoding="utf-8",
        )
        (dest / "local-changes.txt").write_text("precious", encoding="utf-8")

        def _fake_wrap_argv(argv, mode="standard"):
            return list(argv), None

        call_count = 0
        killed = False

        class _OriginProc:
            returncode = 0

            async def communicate(self):
                return (stale_url.encode() + b"\n", None)

        class _CloneProc:
            """Simulates a running clone process that gets cancelled."""

            pid = 99999
            returncode = None

            async def communicate(self):
                # Simulate the outer task being cancelled during this await.
                raise _asyncio.CancelledError()

            async def wait(self):
                self.returncode = -9
                return -9

            def kill(self):
                nonlocal killed
                killed = True

        async def _subprocess_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                # Call 1: _clone_origin_url (reads the stale origin)
                return _OriginProc()
            # Call 2: the actual fresh-clone spawn
            return _CloneProc()

        async def _fake_kill_process_group(proc):
            nonlocal killed
            killed = True

        log_lines: list[str] = []
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch(
                "kiro_crew.apps.registry.create_subprocess_limited",
                side_effect=_subprocess_side_effect,
            ),
            patch(
                "kiro_crew.apps.registry._kill_process_group",
                side_effect=_fake_kill_process_group,
            ),
        ):
            with pytest.raises(_asyncio.CancelledError):
                await reg._git_clone_or_pull(
                    new_url,
                    "main",
                    dest,
                    log_lines,
                    index_originated=False,
                )

        # The process MUST have been killed.
        assert killed, "Clone process was not killed on cancellation"
        # The old checkout content MUST be restored at dest.
        assert dest.is_dir()
        assert (dest / "local-changes.txt").exists()
        assert (dest / "local-changes.txt").read_text() == "precious"
        # No leftover stale-* dirs.
        stale_dirs = [p for p in dest.parent.iterdir() if ".stale-" in p.name]
        assert len(stale_dirs) == 0


class TestUnreadableOriginAbort:
    """Regression: unreadable origin must NOT enter destructive move-aside path.

    GPT 5.6 finding: a checkout with a corrupt .git/config or missing remote
    previously entered the move-aside → re-clone → delete path, permanently
    losing local edits even though the checkout might be the right repo.
    """

    @pytest.mark.asyncio
    async def test_unreadable_origin_returns_error_without_destroying(self, tmp_path):
        """Checkout with unreadable origin → error, dest untouched."""
        import kiro_crew.apps.registry as reg

        git_url = "https://example.com/app.git"
        dest = tmp_path / "app-sources" / "myapp"
        dest.mkdir(parents=True)
        git_dir = dest / ".git"
        git_dir.mkdir()
        # Corrupt config — _clone_origin_url will return None.
        (git_dir / "config").write_text("garbage", encoding="utf-8")
        (dest / "local-edits.txt").write_text("precious work", encoding="utf-8")

        log_lines: list[str] = []
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch(
                "kiro_crew.apps.registry._clone_origin_url",
                new=AsyncMock(return_value=None),
            ),
        ):
            err = await reg._git_clone_or_pull(
                git_url,
                "main",
                dest,
                log_lines,
                index_originated=False,
            )

        # Must return the unreadable_clone_origin error (slug in `code`,
        # human sentence in `error` — the install banner renders `error`).
        assert err is not None
        assert err["ok"] is False
        assert err["code"] == "unreadable_clone_origin"
        # Dest is UNTOUCHED — no rename, no rmtree, no re-clone.
        assert (dest / "local-edits.txt").exists()
        assert (dest / "local-edits.txt").read_text() == "precious work"
        # No stale-* dirs created.
        stale_dirs = [p for p in dest.parent.iterdir() if ".stale-" in p.name]
        assert len(stale_dirs) == 0

    @pytest.mark.asyncio
    async def test_readable_different_origin_still_reclones(self, tmp_path):
        """Readable but different origin → move-aside/re-clone path (not blocked)."""
        import kiro_crew.apps.registry as reg

        stale_url = "https://old.example.com/app.git"
        new_url = "https://new.example.com/app.git"
        dest = tmp_path / "app-sources" / "myapp"
        dest.mkdir(parents=True)
        git_dir = dest / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text(
            f'[remote "origin"]\n\turl = {stale_url}\n',
            encoding="utf-8",
        )

        def _fake_wrap_argv(argv, mode="standard"):
            return list(argv), None

        class _SuccessProc:
            returncode = 0

            async def communicate(self):
                return (b"Cloning into...", None)

        async def _fake_create_subprocess(*args, **kwargs):
            dest.mkdir(parents=True, exist_ok=True)
            (dest / ".git").mkdir(exist_ok=True)
            return _SuccessProc()

        log_lines: list[str] = []
        pending_cleanup: list[Path] = []
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch(
                "kiro_crew.apps.registry.create_subprocess_limited",
                side_effect=_fake_create_subprocess,
            ),
            patch(
                "kiro_crew.apps.registry._clone_origin_url",
                new=AsyncMock(return_value=stale_url),
            ),
        ):
            err = await reg._git_clone_or_pull(
                new_url,
                "main",
                dest,
                log_lines,
                index_originated=False,
                pending_cleanup=pending_cleanup,
            )

        # Success — re-clone worked.
        assert err is None
        # moved-aside path is deferred.
        assert len(pending_cleanup) == 1


class TestBuildFailureRestoresOldCheckout:
    """Regression: build failure after successful re-clone must NOT lose the
    old checkout permanently.

    GPT 5.6 finding: _git_clone_or_pull deleted moved_aside on clone success
    BEFORE the install transaction completed. Build failure then left the app
    broken with no way to recover the old code.
    """

    @pytest.mark.asyncio
    async def test_build_failure_restores_old_checkout(self, tmp_path):
        """Clone succeeds + build fails → old checkout restored at pkg_dir."""
        import kiro_crew.apps.registry as reg

        stale_url = "https://old.example.com/app.git"
        new_url = "https://new.example.com/app.git"

        # Set up the app-sources dir with an old checkout.
        app_sources = tmp_path / "app-sources"
        app_sources.mkdir()
        pkg_dir = app_sources / "testapp"
        pkg_dir.mkdir()
        git_dir = pkg_dir / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text(
            f'[remote "origin"]\n\turl = {stale_url}\n',
            encoding="utf-8",
        )
        (pkg_dir / "my-local-work.txt").write_text("important", encoding="utf-8")

        def _fake_wrap_argv(argv, mode="standard"):
            return list(argv), None

        class _SuccessProc:
            returncode = 0

            async def communicate(self):
                return (b"Cloning into...", None)

        async def _fake_create_subprocess(*args, **kwargs):
            pkg_dir.mkdir(parents=True, exist_ok=True)
            (pkg_dir / ".git").mkdir(exist_ok=True)
            (pkg_dir / "new-file.txt").write_text("new clone", encoding="utf-8")
            return _SuccessProc()

        # Simulate: clone succeeds, then build fails.
        pending_cleanup: list[Path] = []
        log_lines: list[str] = []
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch(
                "kiro_crew.apps.registry.create_subprocess_limited",
                side_effect=_fake_create_subprocess,
            ),
            patch(
                "kiro_crew.apps.registry._clone_origin_url",
                new=AsyncMock(return_value=stale_url),
            ),
        ):
            clone_err = await reg._git_clone_or_pull(
                new_url,
                "main",
                pkg_dir,
                log_lines,
                index_originated=False,
                pending_cleanup=pending_cleanup,
            )

        # Clone succeeded.
        assert clone_err is None
        assert len(pending_cleanup) == 1
        stale_path = pending_cleanup[0]
        assert stale_path.exists()

        # Simulate build failure: caller restores old checkout.
        # (This mirrors _clone_build_app_locked's failure path.)
        import shutil

        await asyncio.to_thread(shutil.rmtree, pkg_dir, True)
        await asyncio.to_thread(stale_path.rename, pkg_dir)

        # Old checkout content restored.
        assert (pkg_dir / "my-local-work.txt").exists()
        assert (pkg_dir / "my-local-work.txt").read_text() == "important"

    @pytest.mark.asyncio
    async def test_install_success_cleans_up_moved_aside(self, tmp_path):
        """Clone succeeds + build succeeds → moved-aside deleted (no stale
        accumulation on the happy path)."""
        import shutil

        import kiro_crew.apps.registry as reg

        stale_url = "https://old.example.com/app.git"
        new_url = "https://new.example.com/app.git"

        app_sources = tmp_path / "app-sources"
        app_sources.mkdir()
        pkg_dir = app_sources / "testapp"
        pkg_dir.mkdir()
        git_dir = pkg_dir / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text(
            f'[remote "origin"]\n\turl = {stale_url}\n',
            encoding="utf-8",
        )

        def _fake_wrap_argv(argv, mode="standard"):
            return list(argv), None

        class _SuccessProc:
            returncode = 0

            async def communicate(self):
                return (b"Cloning into...", None)

        async def _fake_create_subprocess(*args, **kwargs):
            pkg_dir.mkdir(parents=True, exist_ok=True)
            (pkg_dir / ".git").mkdir(exist_ok=True)
            return _SuccessProc()

        pending_cleanup: list[Path] = []
        log_lines: list[str] = []
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch(
                "kiro_crew.apps.registry.create_subprocess_limited",
                side_effect=_fake_create_subprocess,
            ),
            patch(
                "kiro_crew.apps.registry._clone_origin_url",
                new=AsyncMock(return_value=stale_url),
            ),
        ):
            clone_err = await reg._git_clone_or_pull(
                new_url,
                "main",
                pkg_dir,
                log_lines,
                index_originated=False,
                pending_cleanup=pending_cleanup,
            )

        assert clone_err is None
        assert len(pending_cleanup) == 1
        stale_path = pending_cleanup[0]
        assert stale_path.exists()

        # Simulate install success: caller cleans up.
        await asyncio.to_thread(shutil.rmtree, stale_path, True)

        # No stale dirs left.
        stale_dirs = [p for p in app_sources.iterdir() if ".stale-" in p.name]
        assert len(stale_dirs) == 0


class TestRestoreCollision:
    """Regression: undeletable partial clone must not prevent old checkout
    restoration.

    GPT 5.6 finding: rmtree(dest, ignore_errors=True) can silently fail
    (e.g. locked files on Windows), then moved_aside.rename(dest) raises
    OSError and the user's checkout is stranded.
    """

    @pytest.mark.asyncio
    async def test_undeletable_dest_moved_aside_before_restore(self, tmp_path):
        """rmtree(dest) fails → dest moved to .partial-*, then moved_aside
        restored to dest."""
        import kiro_crew.apps.registry as reg

        stale_url = "https://old.example.com/app.git"
        new_url = "https://new.example.com/app.git"

        dest = tmp_path / "app-sources" / "myapp"
        dest.mkdir(parents=True)
        git_dir = dest / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text(
            f'[remote "origin"]\n\turl = {stale_url}\n',
            encoding="utf-8",
        )
        (dest / "local-work.txt").write_text("precious", encoding="utf-8")

        def _fake_wrap_argv(argv, mode="standard"):
            return list(argv), None

        class _FailProc:
            returncode = 128

            async def communicate(self):
                return (b"fatal: clone failed", None)

        rmtree_call_count = 0
        original_rmtree = __import__("shutil").rmtree

        # `**kwargs` absorbs the `onexc=`/`onerror=` hook that
        # `platform_compat.rmtree_force` passes: the removal this test pins is
        # the one that CANNOT delete dest, whichever spelling the caller uses.
        def _stubborn_rmtree(path, ignore_errors=False, **kwargs):
            nonlocal rmtree_call_count
            rmtree_call_count += 1
            # ALL rmtree calls on dest silently fail (simulating locked files).
            if Path(str(path)) == dest:
                return  # silently fail — dest remains
            original_rmtree(path, ignore_errors=ignore_errors)

        async def _fake_create_subprocess(*args, **kwargs):
            # Clone creates partial content at dest.
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "partial-clone-marker.txt").write_text("partial", encoding="utf-8")
            return _FailProc()

        log_lines: list[str] = []
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch(
                "kiro_crew.apps.registry.create_subprocess_limited",
                side_effect=_fake_create_subprocess,
            ),
            patch(
                "kiro_crew.apps.registry._clone_origin_url",
                new=AsyncMock(return_value=stale_url),
            ),
            patch("shutil.rmtree", side_effect=_stubborn_rmtree),
        ):
            err = await reg._git_clone_or_pull(
                new_url,
                "main",
                dest,
                log_lines,
                index_originated=False,
            )

        # Error returned.
        assert err is not None
        assert err["ok"] is False
        # Old checkout restored at dest.
        assert dest.is_dir()
        assert (dest / "local-work.txt").exists()
        assert (dest / "local-work.txt").read_text() == "precious"
        # The undeletable partial clone was moved to a .partial-* sibling.
        partial_dirs = [p for p in dest.parent.iterdir() if ".partial-" in p.name]
        assert len(partial_dirs) == 1
        # Log mentions the partial aside.
        assert any("partial" in line.lower() for line in log_lines)


class TestInstallScriptFailurePreservesStaleCheckout:
    """Regression: install script failure after successful clone+build must
    NOT lose the moved-aside old checkout.

    GPT 5.6 finding: _clone_build_app_locked deleted moved_aside immediately
    after build succeeded, but install_from_registry's install script step
    had not yet run.  If the script failed, the user's old (possibly locally
    modified) code was permanently gone.

    After the fix, _clone_build_app_locked surfaces _pending_stale_cleanup
    in the result dict and only install_from_registry's terminal success
    paths delete the stale dirs.
    """

    @pytest.mark.asyncio
    async def test_clone_build_surfaces_pending_stale_cleanup(self, tmp_path):
        """_clone_build_app surfaces _pending_stale_cleanup paths on the result
        (via its single-exit stamp) instead of deleting them (deferring to
        caller). The stamp lives on the wrapper so EVERY dict result carries
        it, refusals included, not only the ok path."""
        import kiro_crew.apps.registry as reg

        stale_url = "https://old.example.com/app.git"
        new_url = "https://new.example.com/app.git"

        app_sources = tmp_path / "app-sources"
        app_sources.mkdir()
        pkg_dir = app_sources / "testapp"
        pkg_dir.mkdir()
        git_dir = pkg_dir / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text(
            f'[remote "origin"]\n\turl = {stale_url}\n',
            encoding="utf-8",
        )
        (pkg_dir / "local-edits.txt").write_text("precious data", encoding="utf-8")

        def _fake_wrap_argv(argv, mode="standard"):
            return list(argv), None

        class _SuccessProc:
            returncode = 0

            async def communicate(self):
                return (b"Cloning into...", None)

        async def _fake_create_subprocess(*args, **kwargs):
            pkg_dir.mkdir(parents=True, exist_ok=True)
            (pkg_dir / ".git").mkdir(exist_ok=True)
            # A real clone materializes the manifest; the identity gate reads
            # it fail-closed before the build, so the fake must provide it.
            (pkg_dir / "app.json").write_text('{"name": "testapp"}', encoding="utf-8")
            return _SuccessProc()

        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch(
                "kiro_crew.apps.registry.create_subprocess_limited",
                side_effect=_fake_create_subprocess,
            ),
            patch(
                "kiro_crew.apps.registry._clone_origin_url",
                new=AsyncMock(return_value=stale_url),
            ),
            patch("kiro_crew.apps.registry.app_source_dir", return_value=pkg_dir),
            patch(
                "kiro_crew.apps.registry._run_app_build",
                new=AsyncMock(return_value={"ok": True}),
            ),
            patch("kiro_crew.apps.registry._looks_like_git_url", return_value=True),
        ):
            result = await reg._clone_build_app(
                new_url, "testapp", [], branch="main", index_originated=False
            )

        # Build succeeded.
        assert result["ok"]
        # Stale paths surfaced for caller cleanup — NOT deleted.
        stale_paths = result.get("_pending_stale_cleanup", [])
        assert len(stale_paths) == 1
        assert stale_paths[0].exists(), (
            "Expected .stale-* dir to still exist after _clone_build_app "
            "success (deferred to caller)"
        )
        assert ".stale-" in stale_paths[0].name
        # The old checkout content is inside the stale dir (user can recover).
        assert (stale_paths[0] / "local-edits.txt").exists()
        assert (stale_paths[0] / "local-edits.txt").read_text() == "precious data"

    @pytest.mark.asyncio
    async def test_same_repo_stale_is_restored_when_install_from_registry_fails(self, tmp_path):
        """Path-level companion to the helper tests: the `finally` in
        install_from_registry must actually fire.

        The helper was unit-tested while the WIRING was not, which is how a
        branch-based restoration that missed the `onInstall` exit shipped. This drives
        the same failure the test above drives, but with a SAME-REPOSITORY move, which
        must be put back rather than retained.
        """
        from kiro_crew.apps.registry import install_from_registry

        pkg_dir = tmp_path / "testapp"
        pkg_dir.mkdir()
        (pkg_dir / "app.json").write_text(
            '{"name": "testapp", "setup": {"onInstall": "exit 1"}}', encoding="utf-8"
        )
        (pkg_dir / "replacement.txt").write_text("freshly fetched", encoding="utf-8")
        stale_dir = tmp_path / "testapp.stale-deadbeef"
        stale_dir.mkdir()
        (stale_dir / "my-work.txt").write_text("important", encoding="utf-8")

        async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
            return {
                "ok": True,
                "pkg_dir": pkg_dir,
                "_pending_stale_cleanup": [stale_dir],
                "_restorable_stale": [stale_dir],
            }

        class _ScriptFailProc:
            returncode = 1

            async def communicate(self):
                return (b"script failed", None)

        def _fake_wrap_argv(argv, mode="standard"):
            return list(argv), None

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={"repo": "https://example.com/app.git", "branch": "main"},
            ),
            patch(
                "kiro_crew.apps.registry._entry_git_url",
                return_value="https://example.com/app.git",
            ),
            patch("kiro_crew.apps.registry._clone_build_app", new=_fake_clone_build),
            patch("kiro_crew.apps.registry.app_admission_denied", return_value=None),
            patch("kiro_crew.apps.registry.app_execution_denied", return_value=None),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value=None),
            ),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch(
                "kiro_crew.apps.registry.create_subprocess_limited",
                new=AsyncMock(return_value=_ScriptFailProc()),
            ),
            # The destination is derived from the app name in production
            # (`pkg_dir = app_source_dir(app_name)` is its only assignment), so the
            # test has to say where that is rather than relying on the result dict.
            patch("kiro_crew.apps.registry.app_source_dir", return_value=pkg_dir),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await install_from_registry("testapp")

        assert not result["ok"]
        assert (pkg_dir / "my-work.txt").read_text(
            encoding="utf-8"
        ) == "important", "the user's edited checkout must be back in place after a failed update"
        assert not (pkg_dir / "replacement.txt").exists(), "the replacement is discarded"
        assert not stale_dir.exists()
        # Regression: `"log": "\n".join(log_lines)` is built at the `return`
        # inside the `try`, which runs BEFORE this `finally`-driven restore. A
        # dict return value is mutable, but the string it briefly held is not
        # -- appending to log_lines from `finally` after that point silently
        # never reached the caller unless the returned dict itself is re-
        # stamped from `finally`.
        assert "Restored the previous checkout after the install did not complete" in result.get(
            "log", ""
        ), (
            "the finally-driven restore actually happened (asserted above) but its "
            "own confirmation message must reach the caller's log too"
        )

    @pytest.mark.asyncio
    async def test_restoration_works_when_the_failure_dict_omits_pkg_dir(
        self, tmp_path, monkeypatch
    ):
        """The exit Design Review found, which four rounds of rollback work missed.

        Every post-clone FAILURE dict omits `pkg_dir`, so reading it raised a KeyError
        that the broad catch swallowed -- the restoration silently did nothing on
        exactly the exits it exists for, and the suite was green because every existing
        test drove a failure that came AFTER an ok result carrying `pkg_dir`.
        """
        from kiro_crew.apps.registry import install_from_registry

        pkg_dir = tmp_path / "testapp"
        pkg_dir.mkdir()
        (pkg_dir / "replacement.txt").write_text("half-installed", encoding="utf-8")
        stale_dir = tmp_path / "testapp.stale-deadbeef"
        stale_dir.mkdir()
        (stale_dir / "my-work.txt").write_text("important", encoding="utf-8")

        async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
            # Shaped like a real post-clone refusal: ok=False, NO pkg_dir, but the
            # rollback state is present.
            return {
                "ok": False,
                "name": app_name,
                "error": "blocked by admission policy: not allowlisted",
                "_restorable_stale": [stale_dir],
            }

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={"repo": "https://example.com/app.git", "branch": "main"},
            ),
            patch(
                "kiro_crew.apps.registry._entry_git_url",
                return_value="https://example.com/app.git",
            ),
            patch("kiro_crew.apps.registry._clone_build_app", new=_fake_clone_build),
            patch("kiro_crew.apps.registry.app_admission_denied", return_value=None),
            patch("kiro_crew.apps.registry.app_execution_denied", return_value=None),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value=None),
            ),
            patch("kiro_crew.apps.registry.app_source_dir", return_value=pkg_dir),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await install_from_registry("testapp")

        assert not result["ok"]
        assert (pkg_dir / "my-work.txt").read_text(
            encoding="utf-8"
        ) == "important", "a failure dict without pkg_dir must still get the checkout restored"
        assert not stale_dir.exists()

    @pytest.mark.asyncio
    async def test_a_failed_provenance_write_does_not_roll_back_the_source(self, tmp_path):
        """`install_app` has already copied the files, so treating a failed receipt as
        "not durable" would leave installed files from the NEW version beside a source
        tree from the OLD one -- worse than either outcome."""
        from kiro_crew.apps.registry import install_from_registry

        pkg_dir = tmp_path / "testapp"
        pkg_dir.mkdir()
        (pkg_dir / "app.json").write_text('{"name": "testapp"}', encoding="utf-8")
        (pkg_dir / "replacement.txt").write_text("the installed version", encoding="utf-8")
        stale_dir = tmp_path / "testapp.stale-deadbeef"
        stale_dir.mkdir()
        (stale_dir / "old.txt").write_text("previous", encoding="utf-8")

        async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
            return {
                "ok": True,
                "pkg_dir": pkg_dir,
                "_pending_stale_cleanup": [stale_dir],
                "_restorable_stale": [stale_dir],
            }

        class _Ok:
            ok = True
            name = "testapp"
            message = "installed"
            error = None

        def _boom(*a, **k):
            raise OSError("provenance store unwritable")

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={"repo": "https://example.com/app.git", "branch": "main"},
            ),
            patch(
                "kiro_crew.apps.registry._entry_git_url",
                return_value="https://example.com/app.git",
            ),
            patch("kiro_crew.apps.registry._clone_build_app", new=_fake_clone_build),
            patch("kiro_crew.apps.registry.app_admission_denied", return_value=None),
            patch("kiro_crew.apps.registry.app_execution_denied", return_value=None),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value=None),
            ),
            patch("kiro_crew.apps.registry.get_app", return_value=None),
            patch("kiro_crew.apps.registry.install_app", return_value=_Ok()),
            patch("kiro_crew.apps.registry.set_app_provenance", side_effect=_boom),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await install_from_registry("testapp")

        assert not result["ok"], "the bookkeeping failure is still reported"
        assert (
            pkg_dir / "replacement.txt"
        ).exists(), "the installed source tree must NOT be rolled back under installed files"
        assert stale_dir.exists(), "the previous checkout is retained, not restored"

    @pytest.mark.asyncio
    async def test_a_successful_install_is_not_rolled_back(self, tmp_path):
        """Scope guard for the `finally`: a durable success must keep the freshly
        fetched tree, and retain the old one as a sibling rather than restoring it."""
        from kiro_crew.apps.registry import install_from_registry

        pkg_dir = tmp_path / "testapp"
        pkg_dir.mkdir()
        (pkg_dir / "app.json").write_text('{"name": "testapp"}', encoding="utf-8")
        (pkg_dir / "replacement.txt").write_text("freshly fetched", encoding="utf-8")
        stale_dir = tmp_path / "testapp.stale-deadbeef"
        stale_dir.mkdir()
        (stale_dir / "my-work.txt").write_text("important", encoding="utf-8")

        async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
            return {
                "ok": True,
                "pkg_dir": pkg_dir,
                "_pending_stale_cleanup": [stale_dir],
                "_restorable_stale": [stale_dir],
            }

        class _Ok:
            ok = True
            name = "testapp"
            message = "installed"
            error = None

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={"repo": "https://example.com/app.git", "branch": "main"},
            ),
            patch(
                "kiro_crew.apps.registry._entry_git_url",
                return_value="https://example.com/app.git",
            ),
            patch("kiro_crew.apps.registry._clone_build_app", new=_fake_clone_build),
            patch("kiro_crew.apps.registry.app_admission_denied", return_value=None),
            patch("kiro_crew.apps.registry.app_execution_denied", return_value=None),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value=None),
            ),
            patch("kiro_crew.apps.registry.get_app", return_value=None),
            patch("kiro_crew.apps.registry.install_app", return_value=_Ok()),
            patch("kiro_crew.apps.registry.set_app_provenance"),
            # Needed for the rollback destination to be observable at all: without it a
            # wrongly-triggered restore would land outside tmp_path and the assertions
            # below would pass for the wrong reason.
            patch("kiro_crew.apps.registry.app_source_dir", return_value=pkg_dir),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await install_from_registry("testapp")

        assert result["ok"], result
        assert (
            pkg_dir / "replacement.txt"
        ).exists(), "a durable success must keep the tree it installed"
        assert stale_dir.exists(), "the old checkout is retained beside it, not restored"

    @pytest.mark.asyncio
    async def test_stale_not_cleaned_when_install_from_registry_fails(self, tmp_path):
        """Full install_from_registry flow: clone+build succeed but install
        script fails → stale checkout NOT deleted."""
        from kiro_crew.apps.registry import install_from_registry

        stale_dir = tmp_path / "stale-checkout"
        stale_dir.mkdir()
        (stale_dir / "my-work.txt").write_text("important", encoding="utf-8")

        # Mock _clone_build_app to return success with a _pending_stale_cleanup
        # entry, simulating the origin-mismatch → move-aside → clone success flow.
        app_source = tmp_path / "app-source"
        app_source.mkdir()
        (app_source / "app.json").write_text(
            '{"name": "testapp", "setup": {"onInstall": "exit 1"}}', encoding="utf-8"
        )

        async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
            return {
                "ok": True,
                "pkg_dir": app_source,
                "_pending_stale_cleanup": [stale_dir],
            }

        class _ScriptFailProc:
            returncode = 1

            async def communicate(self):
                return (b"script failed", None)

        def _fake_wrap_argv(argv, mode="standard"):
            return list(argv), None

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={"repo": "https://example.com/app.git", "branch": "main"},
            ),
            patch(
                "kiro_crew.apps.registry._entry_git_url",
                return_value="https://example.com/app.git",
            ),
            patch(
                "kiro_crew.apps.registry._clone_build_app",
                new=_fake_clone_build,
            ),
            patch(
                "kiro_crew.apps.registry.app_admission_denied",
                return_value=None,
            ),
            patch(
                "kiro_crew.apps.registry.app_execution_denied",
                return_value=None,
            ),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value=None),
            ),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch(
                "kiro_crew.apps.registry.create_subprocess_limited",
                new=AsyncMock(return_value=_ScriptFailProc()),
            ),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await install_from_registry("testapp")

        # Install script failed.
        assert not result["ok"]
        assert "install script failed" in result.get("error", "")
        # The stale checkout was NOT deleted — user can recover.
        assert stale_dir.exists(), "Expected .stale-* dir to survive install script failure"
        assert (stale_dir / "my-work.txt").read_text() == "important"


class TestMoveCheckoutAsideCancellationSafety:
    """Regression: GPT 5.6 round-5 finding — a cancellation delivered while
    awaiting ``_move_checkout_aside``'s rename+mtime-refresh must not leave
    the moved-aside checkout silently unaccounted for.

    The old implementation ran the rename and the mtime refresh as TWO
    separate ``asyncio.to_thread`` calls; a cancellation landing between them
    left ``aside`` on disk with the checkout's ORIGINAL mtime — immediately
    eligible for the age-based sweep — and unrecorded, since the assignment
    ``moved_aside = await _move_checkout_aside(...)`` never completed. Both
    calls now run as one thread function (no mtime gap), and a cancellation
    that arrives after that call already completed synchronously undoes the
    rename so the caller's state is unchanged by the attempt.
    """

    @pytest.mark.asyncio
    async def test_cancellation_after_completed_rename_restores_checkout(self, tmp_path):
        """A deterministic, event-gated fake: the rename+refresh is allowed
        to run to completion, a threading.Event confirms that on-disk state,
        and only THEN is the awaiting task cancelled -- never a wall-clock
        sleep racing the interleave."""
        from kiro_crew.apps import registry as reg

        dest = tmp_path / "testapp"
        dest.mkdir()
        (dest / "marker.txt").write_text("original", encoding="utf-8")

        rename_completed = threading.Event()
        release_thread = threading.Event()
        real_impl = reg._rename_and_refresh_mtime

        def _gated_impl(d, a):
            # Perform the real rename+utime, signal the test that it landed,
            # then hold the worker thread (the underlying concurrent.futures
            # future stays NOT DONE) until the test has issued the
            # cancellation -- this is what pins the interleave to "cancel
            # arrives strictly after the rename, strictly before the thread
            # call reports its result" instead of leaving it to a race. The
            # wait is BOUNDED: an early test failure that never signals must
            # not leave this worker blocked against tmp_path past teardown.
            real_impl(d, a)
            rename_completed.set()
            assert release_thread.wait(_WORKER_RELEASE_TIMEOUT), "worker was never released"

        log_lines: list[str] = []
        loop = asyncio.get_event_loop()
        with patch.object(reg, "_rename_and_refresh_mtime", side_effect=_gated_impl):
            try:
                task = asyncio.ensure_future(reg._move_checkout_aside(dest, log_lines))
                # Block off-loop until the worker thread's rename+utime has
                # actually landed on disk -- deterministic, no wall-clock sleep.
                await _bounded_off_loop(loop, rename_completed)
                # The underlying thread call is still blocked (has not reported
                # its result yet), so this cancellation is guaranteed to be
                # observed before the coroutine would otherwise receive the
                # completed rename's result.
                task.cancel()
                release_thread.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                # Release the worker no matter how the body exits, so a failed
                # assertion above can never strand the executor thread.
                release_thread.set()

        # The rename had already landed when cancellation arrived, so the
        # helper must have undone it: dest is back with its original
        # contents, and no `.stale-*` sibling is left stranded.
        assert dest.exists()
        assert (dest / "marker.txt").read_text(encoding="utf-8") == "original"
        stranded = list(tmp_path.glob("testapp.stale-*"))
        assert stranded == [], f"expected no stranded aside directory, found {stranded}"

    @pytest.mark.asyncio
    async def test_successful_move_aside_has_no_mtime_gap(self, tmp_path):
        """Control: an un-cancelled move-aside still renames and refreshes
        the mtime, and does so without the window a separate utime call used
        to leave open — the aside's mtime must be fresh (close to now), not
        the checkout's original, possibly-old, mtime."""
        from kiro_crew.apps import registry as reg

        dest = tmp_path / "testapp"
        dest.mkdir()
        old_time = time.time() - 3600
        os.utime(dest, (old_time, old_time))

        log_lines: list[str] = []
        aside = await reg._move_checkout_aside(dest, log_lines)

        assert aside is not None
        assert not dest.exists()
        assert aside.exists()
        assert abs(aside.stat().st_mtime - time.time()) < 60, (
            "the aside's mtime must be refreshed to now, not inherited from "
            "the checkout's original (possibly stale) mtime"
        )

    @pytest.mark.asyncio
    async def test_cancellation_before_rename_settles_worker_and_does_not_strand(self, tmp_path):
        """Round-6 regression (GPT 5.6): a cancellation delivered while the
        move-aside worker is dispatched but has NOT yet renamed must still end
        with the checkout accounted for. Cancelling the awaiting task does not
        cancel the executor thread -- it runs on -- so a bare
        ``if aside.exists()`` check races the in-thread rename: a worker past
        dispatch but pre-rename at check time would complete the rename after
        the handler re-raised, stranding the checkout at ``aside`` unrecorded.
        The handler now settles the worker future BEFORE inspecting ``aside``,
        so a rename that lands after the cancellation is undone (or the
        retained path is logged) -- never silently stranded."""
        from kiro_crew.apps import registry as reg

        dest = tmp_path / "testapp"
        dest.mkdir()
        (dest / "marker.txt").write_text("original", encoding="utf-8")

        worker_started = threading.Event()
        release_thread = threading.Event()
        rename_done = threading.Event()
        real_impl = reg._rename_and_refresh_mtime

        def _gated_impl(d, a):
            # Signal that the worker is running, then hold it BEFORE the
            # rename -- so at cancellation time ``aside`` does not yet exist.
            # Only after the test releases the thread does the rename land,
            # reproducing the "worker renames after the cancel" interleave. The
            # wait is BOUNDED so an early failure cannot strand the worker past
            # teardown.
            worker_started.set()
            assert release_thread.wait(_WORKER_RELEASE_TIMEOUT), "worker was never released"
            real_impl(d, a)
            rename_done.set()

        log_lines: list[str] = []
        loop = asyncio.get_event_loop()
        with patch.object(reg, "_rename_and_refresh_mtime", side_effect=_gated_impl):
            try:
                task = asyncio.ensure_future(reg._move_checkout_aside(dest, log_lines))
                # Worker is dispatched and blocked strictly BEFORE the rename.
                await _bounded_off_loop(loop, worker_started)
                task.cancel()
                # Let the handler resume and reach its settle-await; a bare
                # check-then-race shape would instead re-raise here without ever
                # waiting for the worker.
                await asyncio.sleep(0)
                # Now let the worker perform the rename -- AFTER the cancellation.
                release_thread.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
                # Confirm the worker's rename actually landed on disk.
                assert await loop.run_in_executor(
                    None, lambda: rename_done.wait(_WORKER_RELEASE_TIMEOUT)
                )
            finally:
                # Release the worker no matter how the body exits.
                release_thread.set()

        stranded = list(tmp_path.glob("testapp.stale-*"))
        if stranded:
            # If the undo could not run, the retained path MUST be logged so
            # it is never silently unaccounted for.
            assert any(
                "retained at" in line for line in log_lines
            ), f"a stranded aside {stranded} must be logged, not silent"
        else:
            assert dest.exists()
            assert (dest / "marker.txt").read_text(encoding="utf-8") == "original"

    @pytest.mark.asyncio
    async def test_repeated_cancellation_still_settles_and_undoes_or_logs(self, tmp_path):
        """Round-10 regression (GPT 5.6): a SECOND cancellation delivered while
        the handler is awaiting settlement must not skip the undo-or-log.

        The rename lands (aside exists), then the awaiting task is cancelled
        while the worker thread is still held, driving the handler into its
        settle-await. A second cancel is delivered there. The old handler used
        a bare ``await asyncio.wait({worker})`` that re-raised on that second
        cancel and skipped BOTH the aside->dest undo AND the retained-path log
        -- stranding the checkout at an unreported ``.stale-*`` path until the
        sweep deleted it. The handler now absorbs repeated cancels until the
        worker future is done, THEN runs the synchronous undo-or-log, THEN
        re-raises once. So this ends deterministically with either the aside
        restored to dest OR the retained path named in the log -- never a
        silent strand. Event-gated; no wall-clock sleeps."""
        from kiro_crew.apps import registry as reg

        dest = tmp_path / "testapp"
        dest.mkdir()
        (dest / "marker.txt").write_text("original", encoding="utf-8")

        rename_completed = threading.Event()
        release_thread = threading.Event()
        real_impl = reg._rename_and_refresh_mtime

        def _gated_impl(d, a):
            # Perform the real rename+utime so ``aside`` exists on disk, signal
            # the test, then hold the worker thread NOT DONE until the test has
            # delivered BOTH cancels -- pinning the "aside exists, worker still
            # settling, second cancel arrives" interleave the fix must survive.
            # BOUNDED so an early failure cannot strand the worker past teardown.
            real_impl(d, a)
            rename_completed.set()
            assert release_thread.wait(_WORKER_RELEASE_TIMEOUT), "worker was never released"

        log_lines: list[str] = []
        loop = asyncio.get_event_loop()
        with patch.object(reg, "_rename_and_refresh_mtime", side_effect=_gated_impl):
            try:
                task = asyncio.ensure_future(reg._move_checkout_aside(dest, log_lines))
                # Block off-loop until the worker's rename+utime has landed on disk.
                await _bounded_off_loop(loop, rename_completed)
                # First cancel: delivered at ``await asyncio.shield(worker)``, so
                # the coroutine enters its CancelledError handler and reaches the
                # settle-await (the worker future is still NOT DONE -- held below).
                task.cancel()
                await asyncio.sleep(0)
                # Second cancel: delivered while the handler is parked on its
                # settle-await. The fix must absorb this and keep settling; the
                # old bare wait re-raised here and skipped the undo/log.
                task.cancel()
                await asyncio.sleep(0)
                # Now let the worker finish so the future settles.
                release_thread.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                # Release the worker no matter how the body exits.
                release_thread.set()

        # Repeated cancellation must NOT have stranded the checkout silently.
        stranded = list(tmp_path.glob("testapp.stale-*"))
        if stranded:
            assert any("retained at" in line for line in log_lines), (
                f"a stranded aside {stranded} must be logged after repeated "
                f"cancellation, not silently swept"
            )
        else:
            # The undo ran: the checkout is back in place, unchanged.
            assert dest.exists()
            assert (dest / "marker.txt").read_text(encoding="utf-8") == "original"

    @pytest.mark.asyncio
    async def test_cancel_with_failed_undo_logs_retained_path_durably(self, tmp_path, caplog):
        """Round-14 regression (GPT 5.6, BLOCKING): a cancellation whose undo
        FAILS must record the retained ``.stale-*`` path in a DURABLE sink
        (``logger.warning``), not only in the request-local ``log_lines``.

        A gateway shutdown cancels the update after the move-aside rename landed,
        then the aside->dest undo fails. The request never returns, so its
        ``log_lines`` are discarded — a list-only report would leave the
        age-based sweep to later delete the unnamed recovery checkout. The fix
        emits ``logger.warning`` with the exact retained path before re-raising,
        so the path survives the shutdown in the process log. This asserts the
        durable record specifically, not just the (also-discarded) log line."""
        from kiro_crew.apps import registry as reg

        dest = tmp_path / "testapp"
        dest.mkdir()
        (dest / "marker.txt").write_text("original", encoding="utf-8")

        rename_completed = threading.Event()
        release_thread = threading.Event()
        real_impl = reg._rename_and_refresh_mtime

        def _gated_impl(d, a):
            # Land the real rename+utime so ``aside`` exists, signal the test,
            # then hold the worker NOT DONE until the test delivers the cancel.
            real_impl(d, a)
            rename_completed.set()
            assert release_thread.wait(_WORKER_RELEASE_TIMEOUT), "worker was never released"

        real_rename = Path.rename

        def _undo_fails(self, target, *args, **kwargs):
            # Fail ONLY the aside->dest undo (target is the original dest); the
            # dest->aside move-aside already ran inside the worker via real_impl.
            if Path(target) == dest:
                raise OSError("simulated undo failure")
            return real_rename(self, target, *args, **kwargs)

        log_lines: list[str] = []
        loop = asyncio.get_event_loop()
        with caplog.at_level(logging.WARNING, logger=reg.logger.name):
            with patch.object(reg, "_rename_and_refresh_mtime", side_effect=_gated_impl):
                with patch.object(Path, "rename", _undo_fails):
                    try:
                        task = asyncio.ensure_future(reg._move_checkout_aside(dest, log_lines))
                        await _bounded_off_loop(loop, rename_completed)
                        task.cancel()
                        await asyncio.sleep(0)
                        release_thread.set()
                        with pytest.raises(asyncio.CancelledError):
                            await task
                    finally:
                        release_thread.set()

        # The checkout is genuinely stranded at a .stale-* aside (the undo failed).
        stranded = list(tmp_path.glob("testapp.stale-*"))
        assert stranded, "the undo was supposed to fail, leaving a stranded aside"
        aside_name = stranded[0].name
        # The DURABLE record names the exact retained path. This is the point of
        # the fix: caplog captures logger output that outlives the discarded
        # log_lines, so an incident responder can find the recovery copy.
        durable = [
            rec.getMessage()
            for rec in caplog.records
            if "retained at" in rec.getMessage() and aside_name in rec.getMessage()
        ]
        assert durable, (
            "the retained .stale-* path must be logged durably (logger.warning), "
            f"not only into the request-local log_lines; caplog had: "
            f"{[r.getMessage() for r in caplog.records]}"
        )


class TestMoveAsideRefreshesMtimeBeforeRename:
    """Round-14 regression (GPT 5.6): the move-aside must refresh the retention
    clock BEFORE the rename, so the moved-aside dir never appears under its
    sweep-recognized ``.stale-*`` name carrying a stale (possibly expired) mtime.

    The pre-fix ordering renamed first, then ran ``os.utime`` on the aside. For
    a checkout already older than the retention window, that leaves the aside
    visible under the sweepable name with an expired clock in the window between
    the two syscalls — a CONCURRENT install running the age-based sweep in that
    window deletes the user's recovery copy. Refreshing ``dest`` before the
    rename closes the window: the dir is already fresh the instant it becomes
    observable under the ``.stale-*`` name.
    """

    @pytest.mark.asyncio
    async def test_refresh_precedes_rename_in_invocation_trace(self, tmp_path):
        """The mtime refresh (``os.utime`` on ``dest``) is recorded strictly
        before ``dest`` is renamed aside. Fails against the pre-fix
        rename-then-utime ordering; passes with the refresh-then-rename fix."""
        from kiro_crew.apps import registry as reg

        dest = tmp_path / "testapp"
        dest.mkdir()
        # Source is ALREADY older than the retention window — the exact
        # condition under which a stale-clock aside would be swept immediately.
        expired = time.time() - (reg._STALE_CHECKOUT_RETENTION_DAYS + 1) * 86400
        os.utime(dest, (expired, expired))

        trace: list[str] = []
        real_utime = os.utime
        real_rename = Path.rename

        def _traced_utime(path, *args, **kwargs):
            # Only the retention refresh (targeting the live dest) is of
            # interest; record it and delegate to the real implementation.
            if Path(path) == dest:
                trace.append("utime(dest)")
            return real_utime(path, *args, **kwargs)

        def _traced_rename(self, target, *args, **kwargs):
            if Path(self) == dest:
                trace.append("rename(dest)")
            return real_rename(self, target, *args, **kwargs)

        log_lines: list[str] = []
        with (
            patch.object(reg.os, "utime", side_effect=_traced_utime),
            patch.object(reg.Path, "rename", autospec=True, side_effect=_traced_rename),
        ):
            aside = await reg._move_checkout_aside(dest, log_lines)

        assert aside is not None, f"move-aside should succeed; log: {log_lines!r}"
        # The refresh must be recorded BEFORE the rename.
        assert trace == ["utime(dest)", "rename(dest)"], (
            "the retention clock must be refreshed on dest before the rename; "
            f"observed order was {trace!r}"
        )

    @pytest.mark.asyncio
    async def test_expired_source_carries_fresh_clock_the_instant_it_is_swept_eligible(
        self, tmp_path
    ):
        """On-disk proof: an already-expired checkout, once moved aside, sits at
        the ``.stale-*`` name with a FRESH mtime (well inside the retention
        window), so a concurrent age-based sweep cannot delete it. The sweep's
        own age test is applied to the moved-aside dir to make the point
        concrete."""
        from kiro_crew.apps import registry as reg

        dest = tmp_path / "testapp"
        dest.mkdir()
        (dest / "marker.txt").write_text("recovery copy", encoding="utf-8")
        expired = time.time() - (reg._STALE_CHECKOUT_RETENTION_DAYS + 1) * 86400
        os.utime(dest, (expired, expired))

        log_lines: list[str] = []
        aside = await reg._move_checkout_aside(dest, log_lines)

        assert aside is not None
        assert ".stale-" in aside.name, "the aside must use the sweep-recognized name"
        # Fresh clock: well inside the retention window, so the age-based sweep
        # (cutoff = now - retention window) would NOT delete it.
        cutoff = time.time() - reg._STALE_CHECKOUT_RETENTION_DAYS * 86400
        assert aside.stat().st_mtime >= cutoff, (
            "the moved-aside recovery copy must carry a fresh clock so a "
            "concurrent sweep cannot delete it"
        )
        # And the recovery copy's contents survive intact.
        assert (aside / "marker.txt").read_text(encoding="utf-8") == "recovery copy"


class TestSubdirectoryEscapeRefusalNeverWritesOutsideCheckout:
    """Regression: the subdirectory-containment refusal must never join the
    raw, untrusted ``subdirectory`` onto ``pkg_dir`` for a filesystem write.

    GPT 5.6 finding: on a containment refusal (``_contained_join`` returns
    ``None``), the cleanup call built ``manifest_relpath`` from the raw
    ``subdirectory`` value instead of the safely-contained path. When the
    checkout PRE-existed, ``_unpoison_rejected_checkout`` writes the manifest
    snapshot to ``pkg_dir / manifest_relpath`` — and a build step that
    replaces ``subdirectory`` with a symlink pointing outside the checkout
    made that write escape the sandbox.
    """

    @pytest.mark.asyncio
    async def test_escaping_subdirectory_does_not_write_outside_checkout(self, tmp_path):
        """A symlinked subdirectory that escapes containment must not cause
        any write outside the cloned checkout, even when the checkout
        pre-existed (update path)."""
        from kiro_crew.apps.registry import install_from_registry

        app_source = tmp_path / "app-source"
        app_source.mkdir()
        (app_source / "app.json").write_text('{"name": "testapp"}', encoding="utf-8")

        # A directory OUTSIDE the checkout that a malicious build step wants
        # the manifest-restore write to land in.
        outside = tmp_path / "outside"
        outside.mkdir()

        # subdirectory "evil" is a symlink escaping app_source — this is what
        # a compromised/malicious build step would do.
        evil_link = app_source / "evil"
        evil_link.symlink_to(outside)

        def _fake_wrap_argv(argv, mode="standard"):
            return list(argv), None

        class _GitProc:
            returncode = 1

            async def communicate(self):
                return (b"", None)

        build_result = {
            "ok": True,
            "pkg_dir": app_source,
            # Simulates an UPDATE of a pre-existing app checkout — the branch
            # of _unpoison_rejected_checkout that performs the vulnerable
            # manifest write.
            "_checkout_preexisted": True,
            "_pre_pull_commit": "deadbeef",
            "_pre_update_manifest": b'{"name": "testapp", "pwned": true}',
            "_pending_stale_cleanup": [],
        }

        async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
            return build_result

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={
                    "repo": "https://example.com/app.git",
                    "branch": "main",
                    "subdirectory": "evil",
                },
            ),
            patch(
                "kiro_crew.apps.registry._entry_git_url",
                return_value="https://example.com/app.git",
            ),
            patch("kiro_crew.apps.registry._clone_build_app", new=_fake_clone_build),
            patch("kiro_crew.apps.registry.app_source_dir", return_value=app_source),
            patch("kiro_crew.apps.registry.app_admission_denied", return_value=None),
            patch("kiro_crew.apps.registry.app_execution_denied", return_value=None),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value=None),
            ),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch(
                "kiro_crew.apps.registry.create_subprocess_limited",
                new=AsyncMock(return_value=_GitProc()),
            ),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await install_from_registry("testapp")

        assert not result["ok"]
        assert "unsafe subdirectory" in result.get("error", "")
        # The manifest-restore write must NEVER have followed the symlink out
        # of the checkout — "outside" must remain exactly as it was.
        assert list(outside.iterdir()) == [], (
            "containment refusal must not write into a symlink-escaped "
            "subdirectory outside the checkout"
        )


class TestUnpoisonRejectedCheckoutRevalidatesSubdirectoryAtWriteTime:
    """Regression: every call site that reaches ``_unpoison_rejected_checkout``
    with ``checkout_preexisted=True`` (identity/admission gates before AND
    after the install script runs) built ``manifest_relpath`` from the raw
    ``subdirectory`` and trusted a containment check an EARLIER gate had
    already made. A build step or ``onInstall`` script — which runs with
    write access to the checkout between some of those gates — can replace
    the subdirectory with a symlink escaping the checkout after that earlier
    check passed, and this cleanup runs unsandboxed as the Kiro Crew process.
    ``_unpoison_rejected_checkout`` must re-verify containment itself, at the
    point of the write, rather than relying on every call site to remember.
    """

    @pytest.mark.asyncio
    async def test_manifest_restore_skipped_when_subdirectory_escapes_at_write_time(self, tmp_path):
        from kiro_crew.apps.registry import _unpoison_rejected_checkout

        pkg_dir = tmp_path / "app-source"
        pkg_dir.mkdir()

        outside = tmp_path / "outside"
        outside.mkdir()

        # An earlier gate saw a real directory here and passed containment;
        # by the time this cleanup runs it has been replaced with a symlink
        # escaping pkg_dir — e.g. by onInstall, which ran in between.
        (pkg_dir / "sub").symlink_to(outside)

        log_lines: list[str] = []
        await _unpoison_rejected_checkout(
            "testapp",
            pkg_dir,
            log_lines,
            checkout_preexisted=True,
            pre_pull_commit="",  # skip the git-reset branch; irrelevant here
            manifest_relpath="sub/app.json",
            manifest_snapshot=b'{"name": "testapp", "pwned": true}',
        )

        assert list(outside.iterdir()) == [], (
            "manifest restore must not write through a symlink that escapes "
            "pkg_dir, even when it was planted after an earlier containment check"
        )
        assert any("no longer resolves inside" in line for line in log_lines)

    @pytest.mark.asyncio
    async def test_manifest_restore_still_runs_when_subdirectory_stays_contained(self, tmp_path):
        """Control: a legitimate, still-contained subdirectory must still get
        its manifest restored — the new guard must not break the happy path."""
        from kiro_crew.apps.registry import _unpoison_rejected_checkout

        pkg_dir = tmp_path / "app-source"
        sub = pkg_dir / "sub"
        sub.mkdir(parents=True)
        (sub / "app.json").write_text('{"name": "testapp", "pwned": true}', encoding="utf-8")

        log_lines: list[str] = []
        await _unpoison_rejected_checkout(
            "testapp",
            pkg_dir,
            log_lines,
            checkout_preexisted=True,
            pre_pull_commit="",
            manifest_relpath="sub/app.json",
            manifest_snapshot=b'{"name": "testapp"}',
        )

        assert (sub / "app.json").read_text(encoding="utf-8") == '{"name": "testapp"}'

    @pytest.mark.asyncio
    async def test_manifest_restore_skipped_when_leaf_is_symlinked_inside_subdirectory(
        self, tmp_path
    ):
        """GPT 5.6 round-5 finding: the old guard checked containment of
        ``subdirectory`` (the directory) only, never the manifest LEAF
        (``subdirectory/app.json``). A build step or ``onInstall`` script can
        leave ``subdirectory`` itself an ordinary, correctly-contained
        directory while replacing just its ``app.json`` with a symlink
        escaping ``pkg_dir`` — that passed the old check and the raw write
        then followed the symlink outside the checkout."""
        from kiro_crew.apps.registry import _unpoison_rejected_checkout

        pkg_dir = tmp_path / "app-source"
        sub = pkg_dir / "sub"
        sub.mkdir(parents=True)

        outside = tmp_path / "outside"
        outside.mkdir()
        target = outside / "security_policy.json"
        target.write_text("untouched", encoding="utf-8")

        # `sub` itself is a real, contained directory — only its app.json
        # leaf is a symlink escaping pkg_dir.
        (sub / "app.json").symlink_to(target)

        log_lines: list[str] = []
        await _unpoison_rejected_checkout(
            "testapp",
            pkg_dir,
            log_lines,
            checkout_preexisted=True,
            pre_pull_commit="",
            manifest_relpath="sub/app.json",
            manifest_snapshot=b'{"name": "testapp", "pwned": true}',
        )

        assert target.read_text(encoding="utf-8") == "untouched", (
            "the manifest restore must not follow a symlink planted at the "
            "manifest leaf, even when the containing subdirectory is itself "
            "a real, contained directory"
        )
        assert any("no longer resolves inside" in line for line in log_lines)

    @pytest.mark.asyncio
    async def test_manifest_restore_skipped_when_leaf_is_symlinked_no_subdirectory(self, tmp_path):
        """Same attack, no ``subdirectory`` at all: the old guard was gated on
        ``if subdirectory`` and never ran when subdirectory=="", so a
        symlinked ``app.json`` at the checkout root was never re-checked
        before the write."""
        from kiro_crew.apps.registry import _unpoison_rejected_checkout

        pkg_dir = tmp_path / "app-source"
        pkg_dir.mkdir()

        outside = tmp_path / "outside"
        outside.mkdir()
        target = outside / "security_policy.json"
        target.write_text("untouched", encoding="utf-8")

        (pkg_dir / "app.json").symlink_to(target)

        log_lines: list[str] = []
        await _unpoison_rejected_checkout(
            "testapp",
            pkg_dir,
            log_lines,
            checkout_preexisted=True,
            pre_pull_commit="",
            manifest_relpath="app.json",
            manifest_snapshot=b'{"name": "testapp", "pwned": true}',
        )

        assert target.read_text(encoding="utf-8") == "untouched", (
            "the manifest restore must not follow a symlinked app.json even "
            "with no subdirectory declared"
        )
        assert any("no longer resolves inside" in line for line in log_lines)


class TestContainmentRefusalRestoreFromRespectsRestorableStale:
    """Regression: the subdirectory-containment refusal's cleanup
    (``_checkout_preexisted=False`` branch) must filter ``restore_from`` by
    ``_restorable_stale`` the same way every other restoration site in this
    module does.

    Round-4 security review finding: the refusal passed the first
    ``_pending_stale_cleanup`` entry to ``_unpoison_rejected_checkout``
    unfiltered. When ``_clone_build_app_locked`` forces
    ``_checkout_preexisted=False`` after an origin-mismatch (non-restorable,
    different-repository) move-aside + a successful fresh clone/build, and
    that fresh clone's declared ``subdirectory`` then fails containment, the
    unfiltered ``restore_from`` renamed the origin-mismatched stale checkout
    back into ``pkg_dir`` — exactly the "hand the build the tree the gate
    refused" case ``_restorable_stale`` exists to prevent elsewhere in this
    file.
    """

    @staticmethod
    def _build_result(app_source, stale_dir, *, restorable):
        result = {
            "ok": True,
            "pkg_dir": app_source,
            "_checkout_preexisted": False,
            "_pending_stale_cleanup": [stale_dir],
        }
        if restorable:
            result["_restorable_stale"] = [stale_dir]
        return result

    @pytest.mark.asyncio
    async def test_non_restorable_stale_is_not_restored_on_containment_refusal(self, tmp_path):
        """A non-restorable (origin-mismatch) pending stale must NOT be
        restored into pkg_dir when the freshly cloned repo's declared
        subdirectory fails containment."""
        from kiro_crew.apps.registry import install_from_registry

        app_source = tmp_path / "app-source"
        app_source.mkdir()
        (app_source / "app.json").write_text('{"name": "testapp"}', encoding="utf-8")
        outside = tmp_path / "outside"
        outside.mkdir()
        (app_source / "evil").symlink_to(outside)

        stale_dir = tmp_path / "testapp.stale-deadbeef"
        stale_dir.mkdir()
        (stale_dir / "someone-elses-repo.txt").write_text("not restorable", encoding="utf-8")

        build_result = self._build_result(app_source, stale_dir, restorable=False)

        async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
            return build_result

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={
                    "repo": "https://example.com/app.git",
                    "branch": "main",
                    "subdirectory": "evil",
                },
            ),
            patch(
                "kiro_crew.apps.registry._entry_git_url",
                return_value="https://example.com/app.git",
            ),
            patch("kiro_crew.apps.registry._clone_build_app", new=_fake_clone_build),
            patch("kiro_crew.apps.registry.app_source_dir", return_value=app_source),
            patch("kiro_crew.apps.registry.app_admission_denied", return_value=None),
            patch("kiro_crew.apps.registry.app_execution_denied", return_value=None),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value=None),
            ),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await install_from_registry("testapp")

        assert not result["ok"]
        assert "unsafe subdirectory" in result.get("error", "")
        assert not app_source.exists(), "the rejected fresh clone must be removed"
        assert stale_dir.exists(), "a non-restorable stale must never be restored"
        assert (stale_dir / "someone-elses-repo.txt").read_text(
            encoding="utf-8"
        ) == "not restorable"

    @pytest.mark.asyncio
    async def test_restorable_stale_is_still_restored_on_containment_refusal(self, tmp_path):
        """Control: a restorable (same-repository, branch-drift) pending
        stale IS still restored into pkg_dir on the same containment
        refusal — the fix must narrow, not remove, this path."""
        from kiro_crew.apps.registry import install_from_registry

        app_source = tmp_path / "app-source"
        app_source.mkdir()
        (app_source / "app.json").write_text('{"name": "testapp"}', encoding="utf-8")
        outside = tmp_path / "outside"
        outside.mkdir()
        (app_source / "evil").symlink_to(outside)

        stale_dir = tmp_path / "testapp.stale-deadbeef"
        stale_dir.mkdir()
        (stale_dir / "my-work.txt").write_text("important", encoding="utf-8")

        build_result = self._build_result(app_source, stale_dir, restorable=True)

        async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
            return build_result

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={
                    "repo": "https://example.com/app.git",
                    "branch": "main",
                    "subdirectory": "evil",
                },
            ),
            patch(
                "kiro_crew.apps.registry._entry_git_url",
                return_value="https://example.com/app.git",
            ),
            patch("kiro_crew.apps.registry._clone_build_app", new=_fake_clone_build),
            patch("kiro_crew.apps.registry.app_source_dir", return_value=app_source),
            patch("kiro_crew.apps.registry.app_admission_denied", return_value=None),
            patch("kiro_crew.apps.registry.app_execution_denied", return_value=None),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value=None),
            ),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await install_from_registry("testapp")

        assert not result["ok"]
        assert "unsafe subdirectory" in result.get("error", "")
        assert not stale_dir.exists(), "the restorable stale is renamed back into pkg_dir"
        assert app_source.exists()
        assert (app_source / "my-work.txt").read_text(encoding="utf-8") == "important"


class TestIdentityMismatchPreBuildRestoreFromRespectsRestorableStale:
    """Regression: the pre-build IDENTITY GATE inside ``_clone_build_app_locked``
    must filter ``restore_from`` through the local ``restorable_stale`` list
    before handing it to ``_refuse_identity_mismatch`` — the same class of bug
    fixed at the containment refusal above, just at a different call site.

    Round-4 security review finding (step 10, site 1/3): this gate computed
    ``restore_from`` as ``pending_cleanup[0] if pending_cleanup else None``
    with no ``restorable_stale`` filter, so an origin-mismatched move-aside
    could be restored into ``pkg_dir`` for a repo whose cloned ``app.json``
    this very gate is refusing for declaring the wrong name.
    """

    @staticmethod
    def _make_fake_clone(stale_dir, *, restorable):
        async def _fake_clone(git_url, branch, dest, log_lines, **kwargs):
            pending_cleanup = kwargs.get("pending_cleanup")
            if pending_cleanup is not None:
                pending_cleanup.append(stale_dir)
            restorable_stale = kwargs.get("restorable_stale")
            if restorable and restorable_stale is not None:
                restorable_stale.append(stale_dir)
            dest.mkdir(parents=True, exist_ok=True)
            # Wrong name — trips the identity gate, not the admission gate.
            (dest / "app.json").write_text(json.dumps({"name": "wrong-name"}), encoding="utf-8")
            return None

        return _fake_clone

    @pytest.mark.asyncio
    async def test_non_restorable_stale_is_not_restored_on_identity_mismatch(self, tmp_path):
        from kiro_crew.apps.registry import _clone_build_app

        app_source = tmp_path / "app-sources" / "testapp"
        stale_dir = tmp_path / "app-sources" / "testapp.stale-deadbeef"
        stale_dir.mkdir(parents=True)
        (stale_dir / "someone-elses-repo.txt").write_text("not restorable", encoding="utf-8")

        with (
            patch("kiro_crew.apps.registry.app_source_dir", return_value=app_source),
            patch(
                "kiro_crew.apps.registry._git_clone_or_pull",
                new=self._make_fake_clone(stale_dir, restorable=False),
            ),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await _clone_build_app("https://example.com/app.git", "testapp", [])

        assert not result["ok"]
        assert "declares" in result.get("error", "")
        assert not app_source.exists(), "the rejected fresh clone must be removed"
        assert stale_dir.exists(), "a non-restorable stale must never be restored"
        assert (stale_dir / "someone-elses-repo.txt").read_text(
            encoding="utf-8"
        ) == "not restorable"

    @pytest.mark.asyncio
    async def test_restorable_stale_is_still_restored_on_identity_mismatch(self, tmp_path):
        from kiro_crew.apps.registry import _clone_build_app

        app_source = tmp_path / "app-sources" / "testapp"
        stale_dir = tmp_path / "app-sources" / "testapp.stale-deadbeef"
        stale_dir.mkdir(parents=True)
        (stale_dir / "my-work.txt").write_text("important", encoding="utf-8")

        with (
            patch("kiro_crew.apps.registry.app_source_dir", return_value=app_source),
            patch(
                "kiro_crew.apps.registry._git_clone_or_pull",
                new=self._make_fake_clone(stale_dir, restorable=True),
            ),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await _clone_build_app("https://example.com/app.git", "testapp", [])

        assert not result["ok"]
        assert not stale_dir.exists(), "the restorable stale is renamed back into pkg_dir"
        assert app_source.exists()
        assert (app_source / "my-work.txt").read_text(encoding="utf-8") == "important"


class TestIdentityMismatchPostBuildRestoreFromRespectsRestorableStale:
    """Regression: the post-build IDENTITY GATE inside ``install_from_registry``
    (the re-check that runs right after ``_clone_build_app`` returns
    ``ok=True``) must filter ``restore_from`` through
    ``build_result["_restorable_stale"]`` before handing it to
    ``_refuse_identity_mismatch`` — same class of bug, third call site.
    """

    @staticmethod
    def _build_result(app_source, stale_dir, *, restorable):
        result = {
            "ok": True,
            "pkg_dir": app_source,
            "_checkout_preexisted": False,
            "_pending_stale_cleanup": [stale_dir],
        }
        if restorable:
            result["_restorable_stale"] = [stale_dir]
        return result

    @pytest.mark.asyncio
    async def test_non_restorable_stale_is_not_restored_on_identity_mismatch(self, tmp_path):
        from kiro_crew.apps.registry import install_from_registry

        app_source = tmp_path / "app-source"
        app_source.mkdir()
        # A build step rewrote app.json to a different name than the registry
        # entry declares — the post-build re-check must catch this.
        (app_source / "app.json").write_text('{"name": "wrong-name"}', encoding="utf-8")

        stale_dir = tmp_path / "testapp.stale-deadbeef"
        stale_dir.mkdir()
        (stale_dir / "someone-elses-repo.txt").write_text("not restorable", encoding="utf-8")

        build_result = self._build_result(app_source, stale_dir, restorable=False)

        async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
            return build_result

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={"repo": "https://example.com/app.git", "branch": "main"},
            ),
            patch(
                "kiro_crew.apps.registry._entry_git_url",
                return_value="https://example.com/app.git",
            ),
            patch("kiro_crew.apps.registry._clone_build_app", new=_fake_clone_build),
            patch("kiro_crew.apps.registry.app_source_dir", return_value=app_source),
            patch("kiro_crew.apps.registry.app_admission_denied", return_value=None),
            patch("kiro_crew.apps.registry.app_execution_denied", return_value=None),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value=None),
            ),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await install_from_registry("testapp")

        assert not result["ok"]
        assert "declares" in result.get("error", "")
        assert not app_source.exists(), "the rejected clone must be removed"
        assert stale_dir.exists(), "a non-restorable stale must never be restored"
        assert (stale_dir / "someone-elses-repo.txt").read_text(
            encoding="utf-8"
        ) == "not restorable"

    @pytest.mark.asyncio
    async def test_restorable_stale_is_still_restored_on_identity_mismatch(self, tmp_path):
        from kiro_crew.apps.registry import install_from_registry

        app_source = tmp_path / "app-source"
        app_source.mkdir()
        (app_source / "app.json").write_text('{"name": "wrong-name"}', encoding="utf-8")

        stale_dir = tmp_path / "testapp.stale-deadbeef"
        stale_dir.mkdir()
        (stale_dir / "my-work.txt").write_text("important", encoding="utf-8")

        build_result = self._build_result(app_source, stale_dir, restorable=True)

        async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
            return build_result

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={"repo": "https://example.com/app.git", "branch": "main"},
            ),
            patch(
                "kiro_crew.apps.registry._entry_git_url",
                return_value="https://example.com/app.git",
            ),
            patch("kiro_crew.apps.registry._clone_build_app", new=_fake_clone_build),
            patch("kiro_crew.apps.registry.app_source_dir", return_value=app_source),
            patch("kiro_crew.apps.registry.app_admission_denied", return_value=None),
            patch("kiro_crew.apps.registry.app_execution_denied", return_value=None),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value=None),
            ),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await install_from_registry("testapp")

        assert not result["ok"]
        assert not stale_dir.exists(), "the restorable stale is renamed back into pkg_dir"
        assert app_source.exists()
        assert (app_source / "my-work.txt").read_text(encoding="utf-8") == "important"


class TestAdmissionGatePreBuildRestoreFromRespectsRestorableStale:
    """Regression: the pre-build ADMISSION GATE (second pass, on the cloned
    manifest) inside ``_clone_build_app_locked`` must filter ``restore_from``
    through the local ``restorable_stale`` list before handing it to
    ``_unpoison_rejected_checkout`` — same class of bug, second call site.
    """

    @staticmethod
    def _make_fake_clone(stale_dir, *, restorable):
        async def _fake_clone(git_url, branch, dest, log_lines, **kwargs):
            pending_cleanup = kwargs.get("pending_cleanup")
            if pending_cleanup is not None:
                pending_cleanup.append(stale_dir)
            restorable_stale = kwargs.get("restorable_stale")
            if restorable and restorable_stale is not None:
                restorable_stale.append(stale_dir)
            dest.mkdir(parents=True, exist_ok=True)
            # Correct name — passes the identity gate so the admission gate
            # (patched to deny below) is the one that fires.
            (dest / "app.json").write_text(json.dumps({"name": "testapp"}), encoding="utf-8")
            return None

        return _fake_clone

    @pytest.mark.asyncio
    async def test_non_restorable_stale_is_not_restored_on_admission_denial(self, tmp_path):
        from kiro_crew.apps.registry import _clone_build_app

        app_source = tmp_path / "app-sources" / "testapp"
        stale_dir = tmp_path / "app-sources" / "testapp.stale-deadbeef"
        stale_dir.mkdir(parents=True)
        (stale_dir / "someone-elses-repo.txt").write_text("not restorable", encoding="utf-8")

        with (
            patch("kiro_crew.apps.registry.app_source_dir", return_value=app_source),
            patch(
                "kiro_crew.apps.registry._git_clone_or_pull",
                new=self._make_fake_clone(stale_dir, restorable=False),
            ),
            patch("kiro_crew.apps.registry.app_admission_denied", return_value="unsigned"),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await _clone_build_app("https://example.com/app.git", "testapp", [])

        assert not result["ok"]
        assert "admission policy" in result.get("error", "")
        assert not app_source.exists(), "the rejected fresh clone must be removed"
        assert stale_dir.exists(), "a non-restorable stale must never be restored"
        assert (stale_dir / "someone-elses-repo.txt").read_text(
            encoding="utf-8"
        ) == "not restorable"

    @pytest.mark.asyncio
    async def test_restorable_stale_is_still_restored_on_admission_denial(self, tmp_path):
        from kiro_crew.apps.registry import _clone_build_app

        app_source = tmp_path / "app-sources" / "testapp"
        stale_dir = tmp_path / "app-sources" / "testapp.stale-deadbeef"
        stale_dir.mkdir(parents=True)
        (stale_dir / "my-work.txt").write_text("important", encoding="utf-8")

        with (
            patch("kiro_crew.apps.registry.app_source_dir", return_value=app_source),
            patch(
                "kiro_crew.apps.registry._git_clone_or_pull",
                new=self._make_fake_clone(stale_dir, restorable=True),
            ),
            patch("kiro_crew.apps.registry.app_admission_denied", return_value="unsigned"),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await _clone_build_app("https://example.com/app.git", "testapp", [])

        assert not result["ok"]
        assert not stale_dir.exists(), "the restorable stale is renamed back into pkg_dir"
        assert app_source.exists()
        assert (app_source / "my-work.txt").read_text(encoding="utf-8") == "important"


# ---------------------------------------------------------------------------
# Stale checkout retention on success + aged sweep
# ---------------------------------------------------------------------------


class TestSuccessPathRetainsStaleCheckout:
    """Regression: install success must NOT delete moved-aside checkouts.

    GPT 5.6 round 6 finding: a successful source replacement permanently
    deletes the .stale-* dir, losing user's local edits even when the
    install SUCCEEDED.  After the fix, the stale dir is retained and its
    path is surfaced in the install log.
    """

    @pytest.mark.asyncio
    async def test_success_retains_stale_checkout_and_logs_path(self, tmp_path):
        """A successful install from registry retains the .stale-* dir and
        names its path in the log output."""
        import kiro_crew.apps.registry as reg

        app_sources = tmp_path / "app-sources"
        app_sources.mkdir()
        stale_dir = app_sources / "testapp.stale-abcd1234"
        stale_dir.mkdir()
        (stale_dir / "local-edits.txt").write_text("important work", encoding="utf-8")

        pkg_dir = app_sources / "testapp"
        pkg_dir.mkdir()
        (pkg_dir / "app.json").write_text(
            json.dumps({"name": "testapp", "version": "1.0.0", "resources": "app"}),
            encoding="utf-8",
        )

        async def _fake_clone_build(
            git_url, name, log_lines, *, branch="main", index_originated=False, **kwargs
        ):
            return {
                "ok": True,
                "pkg_dir": pkg_dir,
                "_pending_stale_cleanup": [stale_dir],
            }

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={
                    "repo": "https://example.com/app.git",
                    "branch": "main",
                    "resources": "app",
                },
            ),
            patch(
                "kiro_crew.apps.registry._entry_git_url",
                return_value="https://example.com/app.git",
            ),
            patch(
                "kiro_crew.apps.registry._clone_build_app",
                new=_fake_clone_build,
            ),
            patch(
                "kiro_crew.apps.registry.app_admission_denied",
                return_value=None,
            ),
            patch(
                "kiro_crew.apps.registry.app_execution_denied",
                return_value=None,
            ),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "kiro_crew.apps.registry.is_clone_host_trusted",
                return_value=True,
            ),
            patch(
                "kiro_crew.apps.manager.register_external_app",
            ),
            patch("kiro_crew.apps.registry._sweep_stale_checkouts", new=AsyncMock()),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await reg.install_from_registry("testapp")

        assert result["ok"]
        # The stale directory must still exist — not deleted.
        assert stale_dir.exists(), "Expected .stale-* dir to survive a successful install"
        assert (stale_dir / "local-edits.txt").read_text() == "important work"
        # The log must name the retained path.
        assert str(stale_dir) in result.get("log", "")


class TestRetainedAtReportingSkipsRestorableStale:
    """Regression: a restorable (same-repository) move-aside must never be
    reported as "Previous checkout retained at" — the enclosing `finally`
    restores it after `_report_retained_stale_checkouts` runs, so naming its
    (now-deleted) `.stale-*` path in the log is misleading recovery guidance.

    Opus 4.8 finding (round 3), converged on by Design Review and First
    Principles: a branch-mismatch move-aside is the SAME repository as the
    active checkout (origin already verified identical), so it is marked
    restorable and the `finally` puts it back on a post-build failure. An
    origin-mismatch move-aside is a DIFFERENT repository and is never
    restorable, so it must keep reporting retained-at and stay on disk.
    """

    @pytest.mark.asyncio
    async def test_restorable_stale_is_restored_and_not_reported_as_retained(self, tmp_path):
        """Branch-mismatch move-aside (restorable) + post-build install
        failure -> checkout restored to app_source_dir, and the returned log
        does not claim "Previous checkout retained at:" for it."""
        from kiro_crew.apps.registry import install_from_registry

        pkg_dir = tmp_path / "testapp"
        pkg_dir.mkdir()
        (pkg_dir / "app.json").write_text('{"name": "testapp"}', encoding="utf-8")
        (pkg_dir / "replacement.txt").write_text("freshly fetched", encoding="utf-8")
        stale_dir = tmp_path / "testapp.stale-deadbeef"
        stale_dir.mkdir()
        (stale_dir / "my-work.txt").write_text("important", encoding="utf-8")

        async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
            return {
                "ok": True,
                "pkg_dir": pkg_dir,
                "_pending_stale_cleanup": [stale_dir],
                "_restorable_stale": [stale_dir],
            }

        class _NotOk:
            ok = False
            name = "testapp"
            message = None
            error = "install failed"

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={"repo": "https://example.com/app.git", "branch": "main"},
            ),
            patch(
                "kiro_crew.apps.registry._entry_git_url",
                return_value="https://example.com/app.git",
            ),
            patch("kiro_crew.apps.registry._clone_build_app", new=_fake_clone_build),
            patch("kiro_crew.apps.registry.app_admission_denied", return_value=None),
            patch("kiro_crew.apps.registry.app_execution_denied", return_value=None),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value=None),
            ),
            patch("kiro_crew.apps.registry.get_app", return_value=None),
            patch("kiro_crew.apps.registry.install_app", return_value=_NotOk()),
            # The `finally` restores to `app_source_dir(name)`, not to a key on
            # the failure dict (see _restore_moved_aside call site).
            patch("kiro_crew.apps.registry.app_source_dir", return_value=pkg_dir),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await install_from_registry("testapp")

        assert not result["ok"]
        assert (pkg_dir / "my-work.txt").read_text(encoding="utf-8") == "important", (
            "the branch-mismatch move-aside is the same repository and must be "
            "restored after the failed install"
        )
        assert not (pkg_dir / "replacement.txt").exists(), "the replacement is discarded"
        assert not stale_dir.exists(), "the restored path no longer exists as a stale sibling"
        assert "Previous checkout retained at:" not in result.get("log", ""), (
            "a restored checkout must not be reported as still retained at a "
            "now-deleted .stale-* path"
        )

    @pytest.mark.asyncio
    async def test_non_restorable_stale_still_reports_retained_and_survives(self, tmp_path):
        """Origin-mismatch move-aside (non-restorable, a different repository)
        + the same post-build install failure -> the retained-at line is
        still present and the `.stale-*` dir survives on disk untouched."""
        from kiro_crew.apps.registry import install_from_registry

        pkg_dir = tmp_path / "testapp"
        pkg_dir.mkdir()
        (pkg_dir / "app.json").write_text('{"name": "testapp"}', encoding="utf-8")
        (pkg_dir / "replacement.txt").write_text("freshly fetched", encoding="utf-8")
        stale_dir = tmp_path / "testapp.stale-deadbeef"
        stale_dir.mkdir()
        (stale_dir / "someone-elses-repo.txt").write_text("not restorable", encoding="utf-8")

        async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
            return {
                "ok": True,
                "pkg_dir": pkg_dir,
                "_pending_stale_cleanup": [stale_dir],
                # No _restorable_stale: an origin mismatch is a different
                # repository and is never restorable.
            }

        class _NotOk:
            ok = False
            name = "testapp"
            message = None
            error = "install failed"

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={"repo": "https://example.com/app.git", "branch": "main"},
            ),
            patch(
                "kiro_crew.apps.registry._entry_git_url",
                return_value="https://example.com/app.git",
            ),
            patch("kiro_crew.apps.registry._clone_build_app", new=_fake_clone_build),
            patch("kiro_crew.apps.registry.app_admission_denied", return_value=None),
            patch("kiro_crew.apps.registry.app_execution_denied", return_value=None),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value=None),
            ),
            patch("kiro_crew.apps.registry.get_app", return_value=None),
            patch("kiro_crew.apps.registry.install_app", return_value=_NotOk()),
            patch("kiro_crew.apps.registry.app_source_dir", return_value=pkg_dir),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await install_from_registry("testapp")

        assert not result["ok"]
        assert stale_dir.exists(), "an origin-mismatched checkout must never be restored"
        assert (stale_dir / "someone-elses-repo.txt").exists()
        assert (pkg_dir / "replacement.txt").exists(), "the freshly cloned tree stays in place"
        assert f"Previous checkout retained at: {stale_dir}" in result.get("log", "")


class TestRetainedAtReportingOnDurableSuccess:
    """Regression: a restorable stale must still be reported as retained on a
    DURABLE SUCCESS exit, where the enclosing ``finally`` never restores it.

    GPT 5.6 round-5 finding: ``_report_retained_stale_checkouts`` filtered out
    every ``_restorable_stale`` entry unconditionally, on the assumption that
    "the enclosing finally restores it" — true only on a FAILURE exit
    (``durable_success`` stays False). On a durable success (a completed
    branch-drift or pinned reinstall), the same filtering silently dropped
    the "Previous checkout retained at" line for the one stale that is
    genuinely never restored on that path — it sits at ``.stale-*`` until
    the age-based sweep deletes it, with neither the user nor the log ever
    told about it. ``filter_restorable=False`` on the success call sites
    fixes this.
    """

    @pytest.mark.asyncio
    async def test_successful_reinstall_reports_retained_restorable_stale(self, tmp_path):
        """A durably successful kirocrew-managed install whose build carried
        a restorable stale (branch drift, same repository) must still log
        "Previous checkout retained at" for it."""
        from kiro_crew.apps.registry import install_from_registry

        pkg_dir = tmp_path / "testapp"
        pkg_dir.mkdir()
        (pkg_dir / "app.json").write_text(
            json.dumps({"name": "testapp", "version": "1.0.0", "resources": "app"}),
            encoding="utf-8",
        )
        stale_dir = tmp_path / "testapp.stale-deadbeef"
        stale_dir.mkdir()
        (stale_dir / "my-work.txt").write_text("important", encoding="utf-8")

        async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
            return {
                "ok": True,
                "pkg_dir": pkg_dir,
                "_pending_stale_cleanup": [stale_dir],
                # Restorable, but this success path's `finally` never reaches
                # the restore branch (durable_success is set True below) —
                # the stale genuinely stays at `.stale-*` and must be named.
                "_restorable_stale": [stale_dir],
            }

        class _Ok:
            ok = True
            name = "testapp"
            message = "installed"
            error = None

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={
                    "repo": "https://example.com/app.git",
                    "branch": "main",
                    "resources": "app",
                },
            ),
            patch(
                "kiro_crew.apps.registry._entry_git_url",
                return_value="https://example.com/app.git",
            ),
            patch("kiro_crew.apps.registry._clone_build_app", new=_fake_clone_build),
            patch("kiro_crew.apps.registry.app_admission_denied", return_value=None),
            patch("kiro_crew.apps.registry.app_execution_denied", return_value=None),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value=None),
            ),
            patch("kiro_crew.apps.registry.get_app", return_value=None),
            patch("kiro_crew.apps.registry.install_app", return_value=_Ok()),
            patch("kiro_crew.apps.registry.app_source_dir", return_value=pkg_dir),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await install_from_registry("testapp")

        assert result["ok"]
        assert stale_dir.exists(), "a durable success never restores the stale"
        assert f"Previous checkout retained at: {stale_dir}" in result.get("log", ""), (
            "a restorable stale must still be reported as retained on a "
            "durable-success exit, where it is genuinely never restored"
        )


class TestStaleCheckoutSweep:
    """Tests for _sweep_stale_checkouts_sync — the aged sweep mechanism."""

    def test_removes_aged_stale_dirs(self, tmp_path):
        """Dirs matching .stale-* older than retention are removed."""
        from kiro_crew.apps.registry import _sweep_stale_checkouts_sync

        sources = tmp_path / "app-sources"
        sources.mkdir()
        # Create an old .stale-* dir (mtime 30 days ago)
        stale = sources / "myapp.stale-aabbccdd"
        stale.mkdir()
        (stale / "file.txt").write_text("old", encoding="utf-8")
        old_mtime = time.time() - (30 * 86400)
        os.utime(stale, (old_mtime, old_mtime))

        removed = _sweep_stale_checkouts_sync(sources, time.time())
        assert "myapp.stale-aabbccdd" in removed
        assert not stale.exists()

    def test_removes_aged_partial_dirs(self, tmp_path):
        """Dirs matching .partial-* older than retention are removed."""
        from kiro_crew.apps.registry import _sweep_stale_checkouts_sync

        sources = tmp_path / "app-sources"
        sources.mkdir()
        partial = sources / "myapp.partial-11223344"
        partial.mkdir()
        old_mtime = time.time() - (30 * 86400)
        os.utime(partial, (old_mtime, old_mtime))

        removed = _sweep_stale_checkouts_sync(sources, time.time())
        assert "myapp.partial-11223344" in removed
        assert not partial.exists()

    def test_keeps_fresh_stale_dirs(self, tmp_path):
        """Dirs within the retention window are NOT removed."""
        from kiro_crew.apps.registry import _sweep_stale_checkouts_sync

        sources = tmp_path / "app-sources"
        sources.mkdir()
        stale = sources / "myapp.stale-freshone1"
        stale.mkdir()
        # mtime = now (just created) — well within retention

        removed = _sweep_stale_checkouts_sync(sources, time.time())
        assert removed == []
        assert stale.exists()

    def test_ignores_non_matching_siblings(self, tmp_path):
        """Normal app dirs and unrelated names are never touched."""
        from kiro_crew.apps.registry import _sweep_stale_checkouts_sync

        sources = tmp_path / "app-sources"
        sources.mkdir()
        # A normal app directory
        app_dir = sources / "myapp"
        app_dir.mkdir()
        (app_dir / "app.json").write_text("{}", encoding="utf-8")
        # A dir with 'stale' in the name but not matching the pattern
        oddname = sources / "stale-notes"
        oddname.mkdir()
        # Set both old so they'd be swept IF they matched the pattern
        old_mtime = time.time() - (30 * 86400)
        os.utime(app_dir, (old_mtime, old_mtime))
        os.utime(oddname, (old_mtime, old_mtime))

        removed = _sweep_stale_checkouts_sync(sources, time.time())
        assert removed == []
        assert app_dir.exists()
        assert oddname.exists()

    def test_symlink_outside_sources_not_followed(self, tmp_path):
        """A symlink pointing outside app-sources is NOT followed/deleted."""
        from kiro_crew.apps.registry import _sweep_stale_checkouts_sync

        sources = tmp_path / "app-sources"
        sources.mkdir()
        # Create a target outside sources
        outside = tmp_path / "outside-precious"
        outside.mkdir()
        (outside / "secret.txt").write_text("do not delete", encoding="utf-8")
        # Create a symlink inside sources that looks like a stale checkout
        link = sources / "myapp.stale-symlink1"
        link.symlink_to(outside)
        # Make it old — os.utime(follow_symlinks=False) is unavailable on
        # Windows, so use os.lstat + os.utime on platforms that support it,
        # otherwise skip the mtime aging (the containment check rejects
        # symlinks before the age check anyway).
        old_mtime = time.time() - (30 * 86400)
        if os.utime in os.supports_follow_symlinks:
            os.utime(link, (old_mtime, old_mtime), follow_symlinks=False)
        # On Windows: the symlink resolves outside sources_dir, so the
        # containment check alone is sufficient to prove safety.

        removed = _sweep_stale_checkouts_sync(sources, time.time())
        # The symlink target must NOT be deleted
        assert outside.exists()
        assert (outside / "secret.txt").read_text() == "do not delete"
        # The symlink itself should not appear in removed
        assert "myapp.stale-symlink1" not in removed

    def test_nonexistent_sources_dir(self, tmp_path):
        """A missing app-sources directory returns empty (no crash)."""
        from kiro_crew.apps.registry import _sweep_stale_checkouts_sync

        removed = _sweep_stale_checkouts_sync(tmp_path / "does-not-exist", time.time())
        assert removed == []

    @pytest.mark.asyncio
    async def test_async_sweep_called_at_install(self, tmp_path):
        """_sweep_stale_checkouts is called at the start of
        install_from_registry (integration coherence check)."""
        import kiro_crew.apps.registry as reg

        sweep_called = []

        async def _tracking_sweep():
            sweep_called.append(True)

        async def _fake_clone_build(
            git_url, name, log_lines, *, branch="main", index_originated=False, **kwargs
        ):
            return {"ok": False, "error": "deliberate failure for test"}

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={
                    "repo": "https://example.com/app.git",
                    "branch": "main",
                },
            ),
            patch(
                "kiro_crew.apps.registry._entry_git_url",
                return_value="https://example.com/app.git",
            ),
            patch(
                "kiro_crew.apps.registry.app_admission_denied",
                return_value=None,
            ),
            patch(
                "kiro_crew.apps.registry.app_execution_denied",
                return_value=None,
            ),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "kiro_crew.apps.registry.is_clone_host_trusted",
                return_value=True,
            ),
            patch(
                "kiro_crew.apps.registry._clone_build_app",
                new=_fake_clone_build,
            ),
            patch(
                "kiro_crew.apps.registry._sweep_stale_checkouts",
                new=_tracking_sweep,
            ),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await reg.install_from_registry("testapp")

        # Clone failed but sweep must have been called before it.
        assert sweep_called, "Expected _sweep_stale_checkouts to be invoked"
        assert not result["ok"]

    def test_move_aside_with_mtime_refresh_survives_sweep(self, tmp_path):
        """Regression: a checkout moved aside with refreshed mtime is NOT
        immediately swept, even if the original checkout was older than the
        retention window.

        This reproduces the bug where rename() preserves the directory's mtime,
        so a 30-day-old checkout renamed to .stale-* would be sweep-eligible
        on the very next install — defeating the retention promise.
        """
        from kiro_crew.apps.registry import (
            _STALE_CHECKOUT_RETENTION_DAYS,
            _sweep_stale_checkouts_sync,
        )

        sources = tmp_path / "app-sources"
        sources.mkdir()

        # Simulate a checkout that was last modified 30 days ago.
        old_checkout = sources / "myapp"
        old_checkout.mkdir()
        (old_checkout / "user-edits.txt").write_text("precious", encoding="utf-8")
        old_time = time.time() - (30 * 86400)
        os.utime(old_checkout, (old_time, old_time))

        # Simulate the move-aside: rename preserves mtime…
        moved = sources / "myapp.stale-aabb0011"
        old_checkout.rename(moved)
        # …then the fix refreshes mtime to now.
        os.utime(moved)

        # Also create a genuinely aged stale dir (no refresh).
        genuinely_old = sources / "other.stale-cc001122"
        genuinely_old.mkdir()
        very_old = time.time() - ((_STALE_CHECKOUT_RETENTION_DAYS + 1) * 86400)
        os.utime(genuinely_old, (very_old, very_old))

        # Run sweep.
        removed = _sweep_stale_checkouts_sync(sources, time.time())

        # The refreshed dir must survive — its mtime is now < retention.
        assert moved.exists(), "Move-aside dir with refreshed mtime should NOT be swept"
        assert "myapp.stale-aabb0011" not in removed

        # The genuinely old one must still be swept.
        assert not genuinely_old.exists()
        assert "other.stale-cc001122" in removed


# ---------------------------------------------------------------------------
# Branch-aware manifest fast path: persistent clone on branch A + entry
# branch B must NOT serve the stale (branch-A) manifest through the fast path.
# (registry-admission-branch-consistency)
# ---------------------------------------------------------------------------


class TestManifestBranchGate:
    """Regression: the fast path must require BOTH origin AND branch to match.

    The pre-fix chain:
      1. _fetch_app_manifest served app.json from persistent clone (branch A)
      2. Admission gated on branch-A's manifest
      3. _git_clone_or_pull fast-forwarded onto branch B
      4. Branch-B code ran with admission decided on branch-A's manifest

    Post-fix: the fast path also checks ``_clone_branch_matches`` — it only
    serves the local manifest when clone branch == requested branch. Mismatch
    falls through to the throwaway clone that fetches the correct branch.
    """

    @pytest.mark.asyncio
    async def test_branch_mismatch_skips_fast_path(self, tmp_path):
        """Persistent clone on branch A must NOT supply its manifest when
        the entry requests branch B."""
        from kiro_crew.apps import registry as reg

        clone_url = "https://example.com/target-app.git"

        # Set up a fake persistent clone on "old-branch" with a stale manifest.
        clone_dir = tmp_path / "app-sources" / "target-app"
        clone_dir.mkdir(parents=True)
        git_dir = clone_dir / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text(
            f'[remote "origin"]\n\turl = {clone_url}\n',
            encoding="utf-8",
        )
        (git_dir / "HEAD").write_text(
            "ref: refs/heads/old-branch\n",
            encoding="utf-8",
        )
        (clone_dir / "app.json").write_text(
            '{"name": "target-app", "version": "1.0.0", "stale": true}',
            encoding="utf-8",
        )

        # The throwaway clone (new branch) returns a different manifest.
        new_manifest = {"name": "target-app", "version": "2.0.0", "stale": False}

        async def _fake_exec(*args, **kwargs):
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_proc.returncode = 0
            return mock_proc

        with (
            patch(
                "kiro_crew.apps.registry.app_source_dir",
                return_value=clone_dir,
            ),
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch(
                "kiro_crew.apps.registry.wrap_argv",
                lambda argv, **k: (list(argv), None),
            ),
            patch(
                "kiro_crew.apps.registry.cgroup_scope_argv",
                side_effect=lambda a: a,
            ),
            patch(
                "kiro_crew.apps.registry.create_subprocess_limited",
                side_effect=_fake_exec,
            ),
            patch("tempfile.mkdtemp", return_value=str(tmp_path / "throwaway")),
        ):
            # Ensure the throwaway dir has app.json from the new branch.
            throwaway = tmp_path / "throwaway"
            throwaway.mkdir(parents=True, exist_ok=True)
            (throwaway / "app.json").write_text(json.dumps(new_manifest), encoding="utf-8")

            result = await reg._fetch_app_manifest(
                clone_url,
                "new-branch",  # entry wants branch B
                "",
                app_name="target-app",
                git_url=clone_url,
                owner_designated=False,
            )

        # The stale manifest (branch-A, version 1.0.0) must NOT be returned.
        assert result is not None
        assert result.get("stale") is not True
        assert result.get("version") == "2.0.0"

    @pytest.mark.asyncio
    async def test_same_branch_serves_fast_path(self, tmp_path):
        """Persistent clone with matching origin AND branch still serves its
        local manifest (fast path preserved)."""
        from kiro_crew.apps import registry as reg

        matching_url = "https://example.com/matching-app.git"

        clone_dir = tmp_path / "app-sources" / "matching-app"
        clone_dir.mkdir(parents=True)
        git_dir = clone_dir / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text(
            f'[remote "origin"]\n\turl = {matching_url}\n',
            encoding="utf-8",
        )
        (git_dir / "HEAD").write_text(
            "ref: refs/heads/main\n",
            encoding="utf-8",
        )
        (clone_dir / "app.json").write_text(
            '{"name": "matching-app", "version": "3.0.0"}',
            encoding="utf-8",
        )

        captured: dict = {}

        async def _fake_exec(*args, **kwargs):
            captured.setdefault("calls", []).append(list(args))
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(matching_url.encode() + b"\n", b""))
            mock_proc.returncode = 0
            return mock_proc

        with (
            patch(
                "kiro_crew.apps.registry.app_source_dir",
                return_value=clone_dir,
            ),
            patch(
                "kiro_crew.apps.registry.wrap_argv",
                side_effect=lambda a, mode="standard": (a, None),
            ),
            patch(
                "kiro_crew.apps.registry.cgroup_scope_argv",
                side_effect=lambda a: a,
            ),
            patch(
                "kiro_crew.apps.registry.create_subprocess_limited",
                side_effect=_fake_exec,
            ),
        ):
            result = await reg._fetch_app_manifest(
                matching_url,
                "main",
                "",
                app_name="matching-app",
                git_url=matching_url,
                owner_designated=False,
            )

        # Origin + branch match → persistent clone manifest served directly.
        assert result is not None
        assert result["version"] == "3.0.0"

    @pytest.mark.asyncio
    async def test_unreadable_branch_fails_closed(self, tmp_path):
        """If .git/HEAD is missing (detached or corrupt), the fast path is NOT
        used — fail closed to throwaway clone."""
        from kiro_crew.apps import registry as reg

        matching_url = "https://example.com/target-app.git"

        # Set up persistent clone with matching origin but NO .git/HEAD.
        clone_dir = tmp_path / "app-sources" / "target-app"
        clone_dir.mkdir(parents=True)
        git_dir = clone_dir / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text(
            f'[remote "origin"]\n\turl = {matching_url}\n',
            encoding="utf-8",
        )
        # No HEAD file — _read_clone_branch returns None.
        (clone_dir / "app.json").write_text(
            '{"name": "target-app", "version": "1.0.0", "should-not-serve": true}',
            encoding="utf-8",
        )

        new_manifest = {"name": "target-app", "version": "2.0.0"}

        async def _fake_exec(*args, **kwargs):
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_proc.returncode = 0
            return mock_proc

        with (
            patch(
                "kiro_crew.apps.registry.app_source_dir",
                return_value=clone_dir,
            ),
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch(
                "kiro_crew.apps.registry.wrap_argv",
                lambda argv, **k: (list(argv), None),
            ),
            patch(
                "kiro_crew.apps.registry.cgroup_scope_argv",
                side_effect=lambda a: a,
            ),
            patch(
                "kiro_crew.apps.registry.create_subprocess_limited",
                side_effect=_fake_exec,
            ),
            patch("tempfile.mkdtemp", return_value=str(tmp_path / "throwaway")),
        ):
            throwaway = tmp_path / "throwaway"
            throwaway.mkdir(parents=True, exist_ok=True)
            (throwaway / "app.json").write_text(json.dumps(new_manifest), encoding="utf-8")

            result = await reg._fetch_app_manifest(
                matching_url,
                "main",
                "",
                app_name="target-app",
                git_url=matching_url,
                owner_designated=False,
            )

        # Unreadable branch → fail closed → throwaway clone manifest served.
        assert result is not None
        assert result.get("should-not-serve") is not True
        assert result.get("version") == "2.0.0"

    @pytest.mark.asyncio
    async def test_detached_head_fails_closed(self, tmp_path):
        """Detached HEAD (raw SHA in .git/HEAD) → fail closed, throwaway clone
        used even though origin matches."""
        from kiro_crew.apps import registry as reg

        matching_url = "https://example.com/target-app.git"

        clone_dir = tmp_path / "app-sources" / "target-app"
        clone_dir.mkdir(parents=True)
        git_dir = clone_dir / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text(
            f'[remote "origin"]\n\turl = {matching_url}\n',
            encoding="utf-8",
        )
        # Detached HEAD — raw SHA, not a branch ref.
        (git_dir / "HEAD").write_text(
            "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2\n",
            encoding="utf-8",
        )
        (clone_dir / "app.json").write_text(
            '{"name": "target-app", "version": "1.0.0", "stale": true}',
            encoding="utf-8",
        )

        new_manifest = {"name": "target-app", "version": "2.0.0"}

        async def _fake_exec(*args, **kwargs):
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_proc.returncode = 0
            return mock_proc

        with (
            patch(
                "kiro_crew.apps.registry.app_source_dir",
                return_value=clone_dir,
            ),
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch(
                "kiro_crew.apps.registry.wrap_argv",
                lambda argv, **k: (list(argv), None),
            ),
            patch(
                "kiro_crew.apps.registry.cgroup_scope_argv",
                side_effect=lambda a: a,
            ),
            patch(
                "kiro_crew.apps.registry.create_subprocess_limited",
                side_effect=_fake_exec,
            ),
            patch("tempfile.mkdtemp", return_value=str(tmp_path / "throwaway")),
        ):
            throwaway = tmp_path / "throwaway"
            throwaway.mkdir(parents=True, exist_ok=True)
            (throwaway / "app.json").write_text(json.dumps(new_manifest), encoding="utf-8")

            result = await reg._fetch_app_manifest(
                matching_url,
                "main",
                "",
                app_name="target-app",
                git_url=matching_url,
                owner_designated=False,
            )

        # Detached HEAD → _read_clone_branch returns None → fail closed.
        assert result is not None
        assert result.get("stale") is not True
        assert result.get("version") == "2.0.0"

    @pytest.mark.asyncio
    async def test_non_utf8_head_fails_closed(self, tmp_path):
        """Non-UTF-8 bytes in .git/HEAD → _read_clone_branch returns None,
        _clone_branch_matches returns False, no UnicodeDecodeError propagates.

        Regression: before the fix, read_text("utf-8") raised
        UnicodeDecodeError which was not caught by the except-OSError handler.
        """
        from kiro_crew.apps import registry as reg

        matching_url = "https://example.com/target-app.git"

        clone_dir = tmp_path / "app-sources" / "target-app"
        clone_dir.mkdir(parents=True)
        git_dir = clone_dir / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text(
            f'[remote "origin"]\n\turl = {matching_url}\n',
            encoding="utf-8",
        )
        # Write non-UTF-8 bytes into HEAD — triggers UnicodeDecodeError on read.
        (git_dir / "HEAD").write_bytes(b"ref: refs/heads/\xff\xfe\n")
        (clone_dir / "app.json").write_text(
            '{"name": "target-app", "version": "1.0.0", "stale": true}',
            encoding="utf-8",
        )

        # Unit-level: _read_clone_branch must return None, no exception.
        assert reg._read_clone_branch(clone_dir) is None

        # Unit-level: _clone_branch_matches must return False, no exception.
        assert await reg._clone_branch_matches(clone_dir, "main") is False

        # Integration: fetch_app_manifest fails closed → throwaway clone used.
        new_manifest = {"name": "target-app", "version": "2.0.0"}

        async def _fake_exec(*args, **kwargs):
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_proc.returncode = 0
            return mock_proc

        with (
            patch(
                "kiro_crew.apps.registry.app_source_dir",
                return_value=clone_dir,
            ),
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch(
                "kiro_crew.apps.registry.wrap_argv",
                lambda argv, **k: (list(argv), None),
            ),
            patch(
                "kiro_crew.apps.registry.cgroup_scope_argv",
                side_effect=lambda a: a,
            ),
            patch(
                "kiro_crew.apps.registry.create_subprocess_limited",
                side_effect=_fake_exec,
            ),
            patch("tempfile.mkdtemp", return_value=str(tmp_path / "throwaway")),
        ):
            throwaway = tmp_path / "throwaway"
            throwaway.mkdir(parents=True, exist_ok=True)
            (throwaway / "app.json").write_text(json.dumps(new_manifest), encoding="utf-8")

            result = await reg._fetch_app_manifest(
                matching_url,
                "main",
                "",
                app_name="target-app",
                git_url=matching_url,
                owner_designated=False,
            )

        # Non-UTF-8 HEAD → fail closed → throwaway clone manifest served.
        assert result is not None
        assert result.get("stale") is not True
        assert result.get("version") == "2.0.0"


class TestCloneFailureDiagnostics:
    """A failed clone must tell an index-originated installer WHY: the clone
    ran credential-free because the repo URL differs from the registry URL, so
    a private sibling repo cannot be read. Owner-designated installs keep the
    ambient identity, so their failure must NOT carry the misleading hint.

    The human sentence lives in ``error`` and the machine slug in ``code``: the
    App Store install banner renders ``result.error`` and never
    ``result.message`` (AppDetailPage.tsx), so a slug in ``error`` would be
    shown to the user verbatim.
    """

    @staticmethod
    def _fake_wrap_argv(argv, mode="standard"):
        return list(argv), None

    class _FailProc:
        returncode = 128  # git clone failure

        async def communicate(self):
            return (b"fatal: could not read Username (no credentials)", None)

    @pytest.mark.asyncio
    async def test_index_originated_failure_explains_credential_posture(self, tmp_path):
        """index_originated=True + failed fresh clone → error carries the
        credential-posture explanation and the monorepo-recipe pointer; the
        machine slug is in ``code``."""
        import kiro_crew.apps.registry as reg

        git_url = "https://forge.example.com/owner/private-sibling.git"
        dest = tmp_path / "app-sources" / "myapp"

        async def _fake_create_subprocess(*args, **kwargs):
            # Fresh clone: dest is created by git, then git exits nonzero.
            dest.mkdir(parents=True, exist_ok=True)
            return self._FailProc()

        log_lines: list[str] = []
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=self._fake_wrap_argv),
            patch("kiro_crew.apps.registry.cgroup_scope_argv", side_effect=lambda a: a),
            patch(
                "kiro_crew.apps.registry.create_subprocess_limited",
                side_effect=_fake_create_subprocess,
            ),
        ):
            err = await reg._git_clone_or_pull(
                git_url,
                "main",
                dest,
                log_lines,
                index_originated=True,
            )

        assert err is not None
        assert err["ok"] is False
        # Machine-stable slug is in `code`, NOT `error`.
        assert err["code"] == "git_clone_failed_no_credentials"
        human = err["error"]
        # The human text must NOT be the bare slug (which the banner would show).
        assert human != "git_clone_failed_no_credentials"
        assert human != "git clone failed"
        # It explains the cause (credential-free / withheld) and the remedy.
        low = human.lower()
        assert "credential" in low
        assert "registry" in low
        assert "docs/app-kit/publishing-guide.md" in human
        # No secret/identity material: only the owner's own registry/repo posture
        # is referenced, never the git URL, a token, or ssh identity.
        assert git_url not in human
        assert "ssh" not in low
        assert "token" not in low

    @pytest.mark.asyncio
    async def test_owner_designated_failure_has_no_misleading_hint(self, tmp_path):
        """index_originated=False + failed fresh clone → the bare
        ``git clone failed`` error, with NO credential-posture hint (the clone
        kept the ambient identity, so the hint would be wrong)."""
        import kiro_crew.apps.registry as reg

        git_url = "https://forge.example.com/owner/app.git"
        dest = tmp_path / "app-sources" / "myapp"

        async def _fake_create_subprocess(*args, **kwargs):
            dest.mkdir(parents=True, exist_ok=True)
            return self._FailProc()

        log_lines: list[str] = []
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=self._fake_wrap_argv),
            patch("kiro_crew.apps.registry.cgroup_scope_argv", side_effect=lambda a: a),
            patch(
                "kiro_crew.apps.registry.create_subprocess_limited",
                side_effect=_fake_create_subprocess,
            ),
        ):
            err = await reg._git_clone_or_pull(
                git_url,
                "main",
                dest,
                log_lines,
                index_originated=False,
            )

        assert err is not None
        assert err["ok"] is False
        # Unchanged bare failure — no credential-posture code, no hint.
        assert err["error"] == "git clone failed"
        assert "code" not in err
        assert "credential" not in err["error"].lower()


class TestDetachedHeadNeverMovedAside:
    """Regression: the install-path branch gate must not treat an unreadable
    or detached-HEAD branch state as a confirmed mismatch.

    GPT 5.6 finding (registry.py, install-path branch gate): a None return
    from ``_read_clone_branch`` (detached HEAD, unreadable ``.git/HEAD``,
    gitfile layout) was treated identically to a confirmed branch mismatch,
    destructively moving the checkout aside for re-clone. This punished
    tag-pinned entries (whose detached HEAD is their normal healthy state) on
    every update cycle, and any checkout with a momentarily unreadable
    ``.git/HEAD``. Post-fix: a None read falls through to the non-destructive
    pull path with a log line, and only a CONCRETELY read differing branch
    name triggers the move-aside.
    """

    @staticmethod
    def _make_detached_checkout(dest: Path, origin_url: str) -> None:
        dest.mkdir(parents=True)
        git_dir = dest / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text(
            f'[remote "origin"]\n\turl = {origin_url}\n',
            encoding="utf-8",
        )
        # Detached HEAD: raw SHA, not a "ref: refs/heads/..." line.
        (git_dir / "HEAD").write_text(
            "e83c5163316f89bfbde7d9ab23ca2e25604af290\n",
            encoding="utf-8",
        )

    @pytest.mark.asyncio
    async def test_detached_head_does_not_move_aside(self, tmp_path):
        """A tag-pinned (detached HEAD) checkout must not be moved aside just
        because the requested branch differs from the (unreadable) current
        state — it must fall through to the pull path instead."""
        import kiro_crew.apps.registry as reg

        origin_url = "https://example.com/tag-pinned-app.git"
        dest = tmp_path / "app-sources" / "tag-pinned-app"
        self._make_detached_checkout(dest, origin_url)

        def _fake_wrap_argv(argv, mode="standard"):
            return list(argv), None

        class _PullProc:
            returncode = 0

            async def communicate(self):
                return (b"Already up to date.", None)

        pull_calls: list[list[str]] = []

        async def _fake_create_subprocess(*args, **kwargs):
            pull_calls.append(list(args))
            return _PullProc()

        log_lines: list[str] = []
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch(
                "kiro_crew.apps.registry.cgroup_scope_argv",
                side_effect=lambda a: a,
            ),
            patch(
                "kiro_crew.apps.registry.create_subprocess_limited",
                side_effect=_fake_create_subprocess,
            ),
            patch(
                "kiro_crew.apps.registry._clone_origin_url",
                new=AsyncMock(return_value=origin_url),
            ),
        ):
            err = await reg._git_clone_or_pull(
                origin_url,
                "v2.0.0",  # requested branch differs from the (unknown) current state
                dest,
                log_lines,
                index_originated=False,
            )

        # No error — the fall-through pull path ran instead of a destructive
        # move-aside + re-clone.
        assert err is None
        # No .stale-* sibling was created.
        stale_dirs = [p for p in dest.parent.iterdir() if ".stale-" in p.name]
        assert stale_dirs == []
        # The original checkout (still the same directory) survived.
        assert dest.is_dir()
        assert (dest / ".git").is_dir()
        # A log line explains why re-convergence was skipped.
        assert any("skipping" in line and "branch re-convergence" in line for line in log_lines)
        # The pull path actually ran (git pull spawned).
        assert pull_calls

    @pytest.mark.asyncio
    async def test_repeated_installs_detached_head_no_stale_accumulation(self, tmp_path):
        """Opus 4.8 tag-pinned case: repeated installs of a detached-HEAD
        checkout must never accumulate .stale-* siblings."""
        import kiro_crew.apps.registry as reg

        origin_url = "https://example.com/tag-pinned-app.git"
        dest = tmp_path / "app-sources" / "tag-pinned-app"
        self._make_detached_checkout(dest, origin_url)

        def _fake_wrap_argv(argv, mode="standard"):
            return list(argv), None

        class _PullProc:
            returncode = 0

            async def communicate(self):
                return (b"Already up to date.", None)

        async def _fake_create_subprocess(*args, **kwargs):
            return _PullProc()

        for _ in range(2):
            log_lines: list[str] = []
            with (
                patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
                patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
                patch(
                    "kiro_crew.apps.registry.cgroup_scope_argv",
                    side_effect=lambda a: a,
                ),
                patch(
                    "kiro_crew.apps.registry.create_subprocess_limited",
                    side_effect=_fake_create_subprocess,
                ),
                patch(
                    "kiro_crew.apps.registry._clone_origin_url",
                    new=AsyncMock(return_value=origin_url),
                ),
            ):
                err = await reg._git_clone_or_pull(
                    origin_url,
                    "v2.0.0",
                    dest,
                    log_lines,
                    index_originated=False,
                )
            assert err is None

        stale_dirs = [p for p in dest.parent.iterdir() if ".stale-" in p.name]
        assert stale_dirs == [], "detached-HEAD installs must never accumulate .stale-* dirs"

    @pytest.mark.asyncio
    async def test_known_branch_mismatch_moves_aside_with_fresh_mtime(self, tmp_path):
        """Control: a CONCRETELY read differing branch still moves aside for
        re-clone with a refreshed mtime (existing PR behavior, unchanged)."""
        import kiro_crew.apps.registry as reg

        origin_url = "https://example.com/branched-app.git"
        dest = tmp_path / "app-sources" / "branched-app"
        dest.mkdir(parents=True)
        git_dir = dest / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text(
            f'[remote "origin"]\n\turl = {origin_url}\n',
            encoding="utf-8",
        )
        (git_dir / "HEAD").write_text("ref: refs/heads/old-branch\n", encoding="utf-8")

        def _fake_wrap_argv(argv, mode="standard"):
            return list(argv), None

        class _SuccessProc:
            returncode = 0

            async def communicate(self):
                return (b"Cloning into...", None)

        async def _fake_create_subprocess(*args, **kwargs):
            dest.mkdir(parents=True, exist_ok=True)
            new_git = dest / ".git"
            new_git.mkdir(parents=True, exist_ok=True)
            (new_git / "config").write_text(
                f'[remote "origin"]\n\turl = {origin_url}\n',
                encoding="utf-8",
            )
            return _SuccessProc()

        log_lines: list[str] = []
        pending_cleanup: list[Path] = []
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch(
                "kiro_crew.apps.registry.cgroup_scope_argv",
                side_effect=lambda a: a,
            ),
            patch(
                "kiro_crew.apps.registry.create_subprocess_limited",
                side_effect=_fake_create_subprocess,
            ),
            patch(
                "kiro_crew.apps.registry._clone_origin_url",
                new=AsyncMock(return_value=origin_url),
            ),
        ):
            before = time.time() - 1000
            os.utime(dest, (before, before))
            err = await reg._git_clone_or_pull(
                origin_url,
                "new-branch",
                dest,
                log_lines,
                index_originated=False,
                pending_cleanup=pending_cleanup,
            )

        assert err is None
        assert len(pending_cleanup) == 1
        moved_aside = pending_cleanup[0]
        assert ".stale-" in moved_aside.name
        assert moved_aside.exists()
        # mtime was refreshed (not the far-past value set above).
        assert moved_aside.stat().st_mtime > before + 500
        assert any("moving aside for re-clone" in line for line in log_lines)


class TestOriginMismatchLogsBeforeMoveAside:
    """Regression (First Principles round-6 Watch): the origin-mismatch
    move-aside path must announce WHY it is replacing the checkout -- naming
    the mismatched origin -- for parity with the branch-mismatch path. A
    branch rebuild had dropped that log line, leaving the origin-mismatch
    re-clone silent while the branch-mismatch path still logged its reason.
    """

    @pytest.mark.asyncio
    async def test_origin_mismatch_logs_the_mismatched_origin(self, tmp_path):
        import kiro_crew.apps.registry as reg

        existing_origin = "https://example.com/old-owner/app.git"
        requested_url = "https://example.com/new-owner/app.git"
        dest = tmp_path / "app-sources" / "app"
        dest.mkdir(parents=True)
        git_dir = dest / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text(
            f'[remote "origin"]\n\turl = {existing_origin}\n',
            encoding="utf-8",
        )
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

        def _fake_wrap_argv(argv, mode="standard"):
            return list(argv), None

        class _SuccessProc:
            returncode = 0

            async def communicate(self):
                return (b"Cloning into...", None)

        async def _fake_create_subprocess(*args, **kwargs):
            # The re-clone recreates the checkout at the requested origin.
            dest.mkdir(parents=True, exist_ok=True)
            new_git = dest / ".git"
            new_git.mkdir(parents=True, exist_ok=True)
            (new_git / "config").write_text(
                f'[remote "origin"]\n\turl = {requested_url}\n',
                encoding="utf-8",
            )
            return _SuccessProc()

        log_lines: list[str] = []
        pending_cleanup: list[Path] = []
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch("kiro_crew.apps.registry.cgroup_scope_argv", side_effect=lambda a: a),
            patch(
                "kiro_crew.apps.registry.create_subprocess_limited",
                side_effect=_fake_create_subprocess,
            ),
            patch(
                "kiro_crew.apps.registry._clone_origin_url",
                new=AsyncMock(return_value=existing_origin),
            ),
        ):
            err = await reg._git_clone_or_pull(
                requested_url,
                "main",
                dest,
                log_lines,
                index_originated=False,
                pending_cleanup=pending_cleanup,
            )

        assert err is None
        # The stale clone was moved aside for re-clone.
        assert len(pending_cleanup) == 1
        assert ".stale-" in pending_cleanup[0].name
        # The reason is logged, naming the mismatched origin, for parity with
        # the branch-mismatch path. Assert on log_lines, never raw git argv.
        origin_log = [
            line
            for line in log_lines
            if "Existing clone origin" in line and "does not match" in line
        ]
        assert origin_log, f"expected an origin-mismatch log line, got {log_lines!r}"
        assert existing_origin in origin_log[0]
        assert "moving aside stale clone for re-clone" in origin_log[0]


class TestInstallFailureReportsStaleCheckout:
    """Regression (GPT 5.6 round 2): every non-ok exit AFTER a successful
    clone+build that carries ``_pending_stale_cleanup`` must report the
    retained checkout path — never strand a .stale-* silently.
    """

    @pytest.mark.asyncio
    async def test_install_app_not_ok_reports_stale_path(self, tmp_path):
        """install_app returning not-ok after a successful branch-mismatch
        move-aside + clone + build must still report the retained checkout."""
        import kiro_crew.apps.registry as reg
        from kiro_crew.apps.manager import AppResult

        app_sources = tmp_path / "app-sources"
        app_sources.mkdir()
        stale_dir = app_sources / "testapp.stale-abcd1234"
        stale_dir.mkdir()
        (stale_dir / "local-edits.txt").write_text("important work", encoding="utf-8")

        pkg_dir = app_sources / "testapp"
        pkg_dir.mkdir()
        (pkg_dir / "app.json").write_text(
            json.dumps({"name": "testapp", "version": "1.0.0"}),
            encoding="utf-8",
        )

        async def _fake_clone_build(
            git_url, name, log_lines, *, branch="main", index_originated=False, **kwargs
        ):
            return {
                "ok": True,
                "pkg_dir": pkg_dir,
                "_pending_stale_cleanup": [stale_dir],
            }

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={
                    "repo": "https://example.com/app.git",
                    "branch": "main",
                },
            ),
            patch(
                "kiro_crew.apps.registry._entry_git_url",
                return_value="https://example.com/app.git",
            ),
            patch(
                "kiro_crew.apps.registry._clone_build_app",
                new=_fake_clone_build,
            ),
            patch(
                "kiro_crew.apps.registry.app_admission_denied",
                return_value=None,
            ),
            patch(
                "kiro_crew.apps.registry.app_execution_denied",
                return_value=None,
            ),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "kiro_crew.apps.registry.is_clone_host_trusted",
                return_value=True,
            ),
            patch("kiro_crew.apps.registry.get_app", return_value=None),
            patch(
                "kiro_crew.apps.registry.install_app",
                return_value=AppResult(ok=False, name="testapp", error="install script refused"),
            ),
            patch("kiro_crew.apps.registry._sweep_stale_checkouts", new=AsyncMock()),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await reg.install_from_registry("testapp")

        assert result["ok"] is False
        # The stale directory must still exist — not silently stranded.
        assert stale_dir.exists()
        assert (stale_dir / "local-edits.txt").read_text() == "important work"
        # The log must name the retained path (same line the success paths use).
        assert f"Previous checkout retained at: {stale_dir}" in result.get("log", "")

    @pytest.mark.asyncio
    async def test_outer_exception_reports_stale_path(self, tmp_path):
        """An exception raised AFTER a successful clone+build (e.g. during
        the install_app/update_app call) must still report the retained
        checkout via the outer except handler."""
        import kiro_crew.apps.registry as reg

        app_sources = tmp_path / "app-sources"
        app_sources.mkdir()
        stale_dir = app_sources / "testapp.stale-deadbeef"
        stale_dir.mkdir()
        (stale_dir / "local-edits.txt").write_text("important work", encoding="utf-8")

        pkg_dir = app_sources / "testapp"
        pkg_dir.mkdir()
        (pkg_dir / "app.json").write_text(
            json.dumps({"name": "testapp", "version": "1.0.0"}),
            encoding="utf-8",
        )

        async def _fake_clone_build(
            git_url, name, log_lines, *, branch="main", index_originated=False, **kwargs
        ):
            return {
                "ok": True,
                "pkg_dir": pkg_dir,
                "_pending_stale_cleanup": [stale_dir],
            }

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={
                    "repo": "https://example.com/app.git",
                    "branch": "main",
                },
            ),
            patch(
                "kiro_crew.apps.registry._entry_git_url",
                return_value="https://example.com/app.git",
            ),
            patch(
                "kiro_crew.apps.registry._clone_build_app",
                new=_fake_clone_build,
            ),
            patch(
                "kiro_crew.apps.registry.app_admission_denied",
                return_value=None,
            ),
            patch(
                "kiro_crew.apps.registry.app_execution_denied",
                return_value=None,
            ),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "kiro_crew.apps.registry.is_clone_host_trusted",
                return_value=True,
            ),
            patch("kiro_crew.apps.registry.get_app", return_value=None),
            patch(
                "kiro_crew.apps.registry.install_app",
                side_effect=RuntimeError("boom"),
            ),
            patch("kiro_crew.apps.registry._sweep_stale_checkouts", new=AsyncMock()),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await reg.install_from_registry("testapp")

        assert result["ok"] is False
        assert stale_dir.exists()
        assert f"Previous checkout retained at: {stale_dir}" in result.get("log", "")

    @pytest.mark.asyncio
    async def test_install_script_timeout_reports_stale_path(self, tmp_path):
        """The install-script TIMEOUT exit (proc killed via
        _kill_process_group) must also report the retained checkout — the
        same "Previous checkout retained at:" line the other post-build exits
        use. Distinct from test_outer_exception_reports_stale_path and
        test_install_app_not_ok_reports_stale_path, which both bypass the
        script-execution code path entirely (their manifests carry no
        ``setup.onInstall``)."""
        import kiro_crew.apps.registry as reg

        app_sources = tmp_path / "app-sources"
        app_sources.mkdir()
        stale_dir = app_sources / "testapp.stale-f00dcafe"
        stale_dir.mkdir()
        (stale_dir / "local-edits.txt").write_text("important work", encoding="utf-8")

        pkg_dir = app_sources / "testapp"
        pkg_dir.mkdir()
        (pkg_dir / "app.json").write_text(
            json.dumps({"name": "testapp", "setup": {"onInstall": "sleep 999"}}),
            encoding="utf-8",
        )

        async def _fake_clone_build(
            git_url, name, log_lines, *, branch="main", index_originated=False, **kwargs
        ):
            return {
                "ok": True,
                "pkg_dir": pkg_dir,
                "_pending_stale_cleanup": [stale_dir],
            }

        class _TimeoutProc:
            returncode: int | None = None

            async def communicate(self):
                raise asyncio.TimeoutError

        def _fake_wrap_argv(argv, mode="standard"):
            return list(argv), None

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={
                    "repo": "https://example.com/app.git",
                    "branch": "main",
                },
            ),
            patch(
                "kiro_crew.apps.registry._entry_git_url",
                return_value="https://example.com/app.git",
            ),
            patch(
                "kiro_crew.apps.registry._clone_build_app",
                new=_fake_clone_build,
            ),
            patch(
                "kiro_crew.apps.registry.app_admission_denied",
                return_value=None,
            ),
            patch(
                "kiro_crew.apps.registry.app_execution_denied",
                return_value=None,
            ),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "kiro_crew.apps.registry.is_clone_host_trusted",
                return_value=True,
            ),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch(
                "kiro_crew.apps.registry.create_subprocess_limited",
                new=AsyncMock(return_value=_TimeoutProc()),
            ),
            patch("kiro_crew.apps.registry._kill_process_group", new=AsyncMock()),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await reg.install_from_registry("testapp")

        assert result["ok"] is False
        assert "timed out" in result.get("error", "")
        # The stale directory must still exist — not silently stranded.
        assert stale_dir.exists()
        assert (stale_dir / "local-edits.txt").read_text() == "important work"
        # The log must name the retained path (same line the other post-build
        # exits use).
        assert f"Previous checkout retained at: {stale_dir}" in result.get("log", "")

    @pytest.mark.asyncio
    async def test_traversing_subdirectory_restores_moved_aside_checkout(self, tmp_path):
        """Self-review finding: a traversing ``subdirectory`` refused AFTER a
        successful branch-mismatch move-aside + clone + build must restore the
        moved-aside checkout, not strand it.

        This refusal fires right after ``build_result`` is obtained, before
        any of the identity/admission gates that already call
        ``_unpoison_rejected_checkout`` — it must do the same.
        """
        import kiro_crew.apps.registry as reg

        app_sources = tmp_path / "app-sources"
        app_sources.mkdir()
        stale_dir = app_sources / "testapp.stale-abcd1234"
        stale_dir.mkdir()
        (stale_dir / "local-edits.txt").write_text("important work", encoding="utf-8")

        pkg_dir = app_sources / "testapp"
        pkg_dir.mkdir()

        async def _fake_clone_build(
            git_url, name, log_lines, *, branch="main", index_originated=False, **kwargs
        ):
            return {
                "ok": True,
                "pkg_dir": pkg_dir,
                "_checkout_preexisted": False,
                "_pre_pull_commit": "",
                "_pre_update_manifest": None,
                "_pending_stale_cleanup": [stale_dir],
                # A branch-mismatch move-aside is the same repository as the
                # active checkout, so `_clone_build_app_locked` marks it
                # restorable — the containment-refusal cleanup must only
                # restore a pending stale when it is also restorable (see
                # TestContainmentRefusalRestoreFromRespectsRestorableStale).
                "_restorable_stale": [stale_dir],
            }

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={
                    "repo": "https://example.com/app.git",
                    "branch": "main",
                    "subdirectory": "../../etc",
                },
            ),
            patch(
                "kiro_crew.apps.registry._entry_git_url",
                return_value="https://example.com/app.git",
            ),
            patch(
                "kiro_crew.apps.registry._clone_build_app",
                new=_fake_clone_build,
            ),
            patch(
                "kiro_crew.apps.registry.app_execution_denied",
                return_value=None,
            ),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "kiro_crew.apps.registry.is_clone_host_trusted",
                return_value=True,
            ),
            patch("kiro_crew.apps.registry.app_source_dir", return_value=pkg_dir),
            patch("kiro_crew.apps.registry.get_app", return_value=None),
            patch("kiro_crew.apps.registry._sweep_stale_checkouts", new=AsyncMock()),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await reg.install_from_registry("testapp")

        assert result["ok"] is False
        assert "unsafe subdirectory" in result["error"]
        # The moved-aside checkout must be restored into pkg_dir's slot, not
        # left stranded as an unreported .stale-* sibling.
        assert not stale_dir.exists()
        assert pkg_dir.exists()
        assert (pkg_dir / "local-edits.txt").read_text() == "important work"


class TestRefusalExitsReportRetainedStale:
    """Regression (GPT 5.6 round 8): a refusal that leaves a non-restorable
    (origin-mismatch) checkout moved aside must REPORT the retained ``.stale-*``
    path, not strand it silently until the age-based sweep deletes it.

    Two halves of the fix are pinned here:

    * ``_clone_build_app`` stamps ``_pending_stale_cleanup`` on EVERY dict
      result at its single exit — refusals included, not only the ok path — so
      the caller's reporter has the move-aside state to work from.
    * ``install_from_registry`` runs ``_report_retained_stale_checkouts`` at
      every refusal exit reachable after a move-aside, filtering the restorable
      subset the enclosing ``finally`` puts back.

    The end-to-end cases drive the REAL ``_clone_build_app`` through a faked
    ``_git_clone_or_pull`` so the wrapper's stamping is exercised, not
    hand-supplied.
    """

    @staticmethod
    def _make_fake_git_clone(stale_dir, *, restorable, cloned_name):
        async def _fake_clone(git_url, branch, dest, log_lines, **kwargs):
            # Mirror the origin-mismatch move-aside: record the aside path on
            # the caller-owned lists exactly as the real gate does.
            pending_cleanup = kwargs.get("pending_cleanup")
            if pending_cleanup is not None:
                pending_cleanup.append(stale_dir)
            restorable_stale = kwargs.get("restorable_stale")
            if restorable and restorable_stale is not None:
                restorable_stale.append(stale_dir)
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "app.json").write_text(json.dumps({"name": cloned_name}), encoding="utf-8")
            return None

        return _fake_clone

    @staticmethod
    def _install_patches(pkg_dir, *, admission_denied=None):
        # Deny only once a MANIFEST is present (the cloned / post-build gates),
        # never on the manifest=None prefetch gate that runs before the clone —
        # otherwise the prefetch short-circuits before any move-aside happens.
        def _admission(_name, *, manifest=None, action=None, **_kw):
            return admission_denied if (admission_denied and manifest is not None) else None

        return (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={"repo": "https://new.example.com/app.git", "branch": "main"},
            ),
            patch(
                "kiro_crew.apps.registry._entry_git_url",
                return_value="https://new.example.com/app.git",
            ),
            patch("kiro_crew.apps.registry.app_source_dir", return_value=pkg_dir),
            patch("kiro_crew.apps.registry.app_admission_denied", side_effect=_admission),
            patch("kiro_crew.apps.registry.app_execution_denied", return_value=None),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value=None),
            ),
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry._looks_like_git_url", return_value=True),
            patch("kiro_crew.apps.registry._sweep_stale_checkouts", new=AsyncMock()),
            patch("kiro_crew.apps.registry.sel"),
        )

    async def _run_install(self, pkg_dir, first_patch, *, admission_denied=None):
        """Enter *first_patch* plus the shared install patches and run
        ``install_from_registry("testapp")``, returning its result."""
        import kiro_crew.apps.registry as reg

        with contextlib.ExitStack() as stack:
            stack.enter_context(first_patch)
            for cm in self._install_patches(pkg_dir, admission_denied=admission_denied):
                stack.enter_context(cm)
            return await reg.install_from_registry("testapp")

    @pytest.mark.asyncio
    async def test_identity_refusal_reports_non_restorable_retained_stale(self, tmp_path):
        """Origin-mismatch move-aside + a pre-build identity refusal (cloned
        app.json declares a different name): the log names the retained
        ``.stale-*`` path and the dir survives on disk."""
        app_sources = tmp_path / "app-sources"
        app_sources.mkdir()
        pkg_dir = app_sources / "testapp"
        stale_dir = app_sources / "testapp.stale-deadbeef"
        stale_dir.mkdir()
        (stale_dir / "someone-elses-repo.txt").write_text("not restorable", encoding="utf-8")

        result = await self._run_install(
            pkg_dir,
            patch(
                "kiro_crew.apps.registry._git_clone_or_pull",
                new=self._make_fake_git_clone(stale_dir, restorable=False, cloned_name="wrong"),
            ),
        )

        assert result["ok"] is False
        assert f"Previous checkout retained at: {stale_dir}" in result.get("log", "")
        assert stale_dir.exists(), "a non-restorable stale must never be swept unreported"
        assert (stale_dir / "someone-elses-repo.txt").read_text(
            encoding="utf-8"
        ) == "not restorable"

    @pytest.mark.asyncio
    async def test_cloned_admission_refusal_reports_non_restorable_retained_stale(self, tmp_path):
        """Origin-mismatch move-aside + the cloned-admission rejection
        (app_admission_denied denies): the retained ``.stale-*`` is reported and
        survives — same seam as the identity refusal, different gate."""
        app_sources = tmp_path / "app-sources"
        app_sources.mkdir()
        pkg_dir = app_sources / "testapp"
        stale_dir = app_sources / "testapp.stale-deadbeef"
        stale_dir.mkdir()
        (stale_dir / "someone-elses-repo.txt").write_text("not restorable", encoding="utf-8")

        result = await self._run_install(
            pkg_dir,
            patch(
                "kiro_crew.apps.registry._git_clone_or_pull",
                # Correct name → identity gate passes → the admission gate is
                # the one that refuses.
                new=self._make_fake_git_clone(stale_dir, restorable=False, cloned_name="testapp"),
            ),
            admission_denied="signature required",
        )

        assert result["ok"] is False
        assert "admission policy" in result.get("error", "")
        assert f"Previous checkout retained at: {stale_dir}" in result.get("log", "")
        assert stale_dir.exists(), "a non-restorable stale must never be swept unreported"

    @pytest.mark.asyncio
    async def test_restorable_stale_is_restored_and_not_reported_on_identity_refusal(
        self, tmp_path
    ):
        """Control: a same-origin (branch-drift) move-aside + an identity
        refusal is RESTORED into the slot (existing behavior) and the log does
        NOT claim it was retained."""
        app_sources = tmp_path / "app-sources"
        app_sources.mkdir()
        pkg_dir = app_sources / "testapp"
        stale_dir = app_sources / "testapp.stale-deadbeef"
        stale_dir.mkdir()
        (stale_dir / "my-work.txt").write_text("important", encoding="utf-8")

        result = await self._run_install(
            pkg_dir,
            patch(
                "kiro_crew.apps.registry._git_clone_or_pull",
                new=self._make_fake_git_clone(stale_dir, restorable=True, cloned_name="wrong"),
            ),
        )

        assert result["ok"] is False
        assert "Previous checkout retained at:" not in result.get("log", "")
        assert not stale_dir.exists(), "a restorable stale is renamed back into the slot"
        assert pkg_dir.exists()
        assert (pkg_dir / "my-work.txt").read_text(encoding="utf-8") == "important"

    @pytest.mark.asyncio
    async def test_post_build_identity_refusal_reports_non_restorable_retained_stale(
        self, tmp_path
    ):
        """A build step that rewrites app.json to a different name trips the
        POST-build identity gate inside install_from_registry; a non-restorable
        move-aside carried on the (ok) build result must be reported there."""
        app_sources = tmp_path / "app-sources"
        app_sources.mkdir()
        pkg_dir = app_sources / "testapp"
        pkg_dir.mkdir()
        (pkg_dir / "app.json").write_text('{"name": "wrong-name"}', encoding="utf-8")
        stale_dir = app_sources / "testapp.stale-deadbeef"
        stale_dir.mkdir()
        (stale_dir / "someone-elses-repo.txt").write_text("not restorable", encoding="utf-8")

        async def _fake_clone_build(git_url, name, log_lines, *, branch="main", **kwargs):
            return {
                "ok": True,
                "pkg_dir": pkg_dir,
                "_checkout_preexisted": False,
                "_pending_stale_cleanup": [stale_dir],
            }

        result = await self._run_install(
            pkg_dir, patch("kiro_crew.apps.registry._clone_build_app", new=_fake_clone_build)
        )

        assert result["ok"] is False
        assert "declares" in result.get("error", "")
        assert f"Previous checkout retained at: {stale_dir}" in result.get("log", "")
        assert stale_dir.exists()

    @pytest.mark.asyncio
    async def test_post_build_admission_refusal_reports_non_restorable_retained_stale(
        self, tmp_path
    ):
        """The POST-build admission gate inside install_from_registry must also
        report a non-restorable move-aside it strands."""
        app_sources = tmp_path / "app-sources"
        app_sources.mkdir()
        pkg_dir = app_sources / "testapp"
        pkg_dir.mkdir()
        (pkg_dir / "app.json").write_text('{"name": "testapp"}', encoding="utf-8")
        stale_dir = app_sources / "testapp.stale-deadbeef"
        stale_dir.mkdir()
        (stale_dir / "someone-elses-repo.txt").write_text("not restorable", encoding="utf-8")

        async def _fake_clone_build(git_url, name, log_lines, *, branch="main", **kwargs):
            return {
                "ok": True,
                "pkg_dir": pkg_dir,
                "_checkout_preexisted": False,
                "_pending_stale_cleanup": [stale_dir],
            }

        result = await self._run_install(
            pkg_dir,
            patch("kiro_crew.apps.registry._clone_build_app", new=_fake_clone_build),
            admission_denied="signature required",
        )

        assert result["ok"] is False
        assert "admission policy" in result.get("error", "")
        assert f"Previous checkout retained at: {stale_dir}" in result.get("log", "")
        assert stale_dir.exists()


class TestCloneBuildStampsPendingOnRefusal:
    """Regression (GPT 5.6 round 8): ``_clone_build_app`` must stamp
    ``_pending_stale_cleanup`` onto a REFUSAL dict, not only the ok path — the
    single-exit invariant that keeps a new exit from silently dropping the
    move-aside state the caller's reporter needs.
    """

    @staticmethod
    def _make_fake_clone(stale_dir, *, restorable):
        async def _fake_clone(git_url, branch, dest, log_lines, **kwargs):
            pending_cleanup = kwargs.get("pending_cleanup")
            if pending_cleanup is not None:
                pending_cleanup.append(stale_dir)
            restorable_stale = kwargs.get("restorable_stale")
            if restorable and restorable_stale is not None:
                restorable_stale.append(stale_dir)
            dest.mkdir(parents=True, exist_ok=True)
            # Wrong name trips the pre-build identity gate → refusal dict.
            (dest / "app.json").write_text(json.dumps({"name": "wrong-name"}), encoding="utf-8")
            return None

        return _fake_clone

    @pytest.mark.asyncio
    async def test_non_restorable_refusal_dict_carries_pending_only(self, tmp_path):
        from kiro_crew.apps.registry import _clone_build_app

        app_source = tmp_path / "app-sources" / "testapp"
        stale_dir = tmp_path / "app-sources" / "testapp.stale-deadbeef"
        stale_dir.mkdir(parents=True)

        with (
            patch("kiro_crew.apps.registry.app_source_dir", return_value=app_source),
            patch(
                "kiro_crew.apps.registry._git_clone_or_pull",
                new=self._make_fake_clone(stale_dir, restorable=False),
            ),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await _clone_build_app("https://new.example.com/app.git", "testapp", [])

        assert result["ok"] is False
        assert result.get("_pending_stale_cleanup") == [stale_dir]
        # Non-restorable: it must NOT appear in the restorable subset.
        assert result.get("_restorable_stale") is None

    @pytest.mark.asyncio
    async def test_restorable_refusal_dict_carries_both_lists(self, tmp_path):
        from kiro_crew.apps.registry import _clone_build_app

        app_source = tmp_path / "app-sources" / "testapp"
        stale_dir = tmp_path / "app-sources" / "testapp.stale-deadbeef"
        stale_dir.mkdir(parents=True)

        with (
            patch("kiro_crew.apps.registry.app_source_dir", return_value=app_source),
            patch(
                "kiro_crew.apps.registry._git_clone_or_pull",
                new=self._make_fake_clone(stale_dir, restorable=True),
            ),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await _clone_build_app("https://new.example.com/app.git", "testapp", [])

        assert result["ok"] is False
        assert result.get("_pending_stale_cleanup") == [stale_dir]
        assert result.get("_restorable_stale") == [stale_dir]


class TestCloneBuildExceptionPathReportsRetainedStale:
    """Regression (GPT 5.6 round 9, registry.py:~4005): when ``_clone_build_app``
    raises AFTER an origin-mismatch move-aside, its ``except BaseException``
    handler must NAME each retained non-restorable ``.stale-*`` path (the same
    "Previous checkout retained at:" wording the finally-owned reporter uses)
    before re-raising.

    On this path there is no result dict, so the caller's finally-owned reporter
    never learns of the move-aside; without the handler's logging the age-based
    sweep would later delete a checkout the user was never told about. The
    restorable (same-origin) subset is instead RESTORED here and must NOT be
    reported as retained.
    """

    @staticmethod
    def _make_raising_locked(stale_dir, *, restorable, exc):
        """Fake ``_clone_build_app_locked`` that records *stale_dir* on the
        caller-owned lists exactly as the real move-aside gate does, then raises
        *exc* before returning any result dict."""

        async def _fake_locked(git_url, app_name, log_lines, **kwargs):
            pending_cleanup = kwargs.get("pending_cleanup")
            if pending_cleanup is not None:
                pending_cleanup.append(stale_dir)
            restorable_stale = kwargs.get("restorable_stale")
            if restorable and restorable_stale is not None:
                restorable_stale.append(stale_dir)
            raise exc

        return _fake_locked

    @pytest.mark.asyncio
    async def test_non_restorable_move_aside_is_logged_before_reraise(self, tmp_path):
        """Origin-mismatch (non-restorable) move-aside + a build-step exception:
        the retained ``.stale-*`` path is named in ``log_lines`` before the
        exception propagates, and the dir survives on disk untouched."""
        import kiro_crew.apps.registry as reg

        app_sources = tmp_path / "app-sources"
        app_sources.mkdir()
        pkg_dir = app_sources / "testapp"
        stale_dir = app_sources / "testapp.stale-deadbeef"
        stale_dir.mkdir()
        (stale_dir / "someone-elses-repo.txt").write_text("not restorable", encoding="utf-8")

        log_lines: list[str] = []
        with (
            patch("kiro_crew.apps.registry.app_source_dir", return_value=pkg_dir),
            patch(
                "kiro_crew.apps.registry._clone_build_app_locked",
                new=self._make_raising_locked(
                    stale_dir, restorable=False, exc=RuntimeError("build blew up")
                ),
            ),
        ):
            with pytest.raises(RuntimeError, match="build blew up"):
                await reg._clone_build_app(
                    "https://new.example.com/app.git", "testapp", log_lines
                )

        # The handler named the retained path before re-raising.
        assert f"Previous checkout retained at: {stale_dir}" in log_lines
        # A non-restorable origin-mismatch stale is deliberately left on disk.
        assert stale_dir.exists(), "a non-restorable stale must never be swept unreported"
        assert (stale_dir / "someone-elses-repo.txt").read_text(
            encoding="utf-8"
        ) == "not restorable"

    @pytest.mark.asyncio
    async def test_cancellation_logs_retained_non_restorable_path(self, tmp_path):
        """``CancelledError`` (the reported case) on the same path also names the
        retained non-restorable stale before propagating — the handler catches
        ``BaseException`` on purpose."""
        import kiro_crew.apps.registry as reg

        app_sources = tmp_path / "app-sources"
        app_sources.mkdir()
        pkg_dir = app_sources / "testapp"
        stale_dir = app_sources / "testapp.stale-deadbeef"
        stale_dir.mkdir()
        (stale_dir / "someone-elses-repo.txt").write_text("not restorable", encoding="utf-8")

        log_lines: list[str] = []
        with (
            patch("kiro_crew.apps.registry.app_source_dir", return_value=pkg_dir),
            patch(
                "kiro_crew.apps.registry._clone_build_app_locked",
                new=self._make_raising_locked(
                    stale_dir, restorable=False, exc=asyncio.CancelledError()
                ),
            ),
        ):
            with pytest.raises(asyncio.CancelledError):
                await reg._clone_build_app(
                    "https://new.example.com/app.git", "testapp", log_lines
                )

        assert f"Previous checkout retained at: {stale_dir}" in log_lines
        assert stale_dir.exists()

    @pytest.mark.asyncio
    async def test_restorable_move_aside_is_restored_not_reported_on_reraise(self, tmp_path):
        """A restorable (same-origin) move-aside on the exception path is put
        back at ``pkg_dir`` and must NOT be reported as retained — restoring it
        deletes the ``.stale-*`` sibling, so naming it would be misleading."""
        import kiro_crew.apps.registry as reg

        app_sources = tmp_path / "app-sources"
        app_sources.mkdir()
        pkg_dir = app_sources / "testapp"
        stale_dir = app_sources / "testapp.stale-deadbeef"
        stale_dir.mkdir()
        (stale_dir / "my-work.txt").write_text("important", encoding="utf-8")

        log_lines: list[str] = []
        with (
            patch("kiro_crew.apps.registry.app_source_dir", return_value=pkg_dir),
            patch(
                "kiro_crew.apps.registry._clone_build_app_locked",
                new=self._make_raising_locked(
                    stale_dir, restorable=True, exc=RuntimeError("build blew up")
                ),
            ),
        ):
            with pytest.raises(RuntimeError, match="build blew up"):
                await reg._clone_build_app(
                    "https://new.example.com/app.git", "testapp", log_lines
                )

        # Restored back into the slot, so the .stale-* sibling is gone...
        assert not stale_dir.exists(), "a restorable stale is renamed back into pkg_dir"
        assert (pkg_dir / "my-work.txt").read_text(encoding="utf-8") == "important"
        # ...and it must NOT be reported as still retained at a now-deleted path.
        assert not any(
            line.startswith("Previous checkout retained at:") for line in log_lines
        ), "a restored checkout must not be named as retained"


class TestProvenanceRaiseAfterDurableSuccessReportsRetainedStale:
    """Regression (GPT 5.6 round 9, registry.py:~5418 / the consolidated
    finally-owned reporter): a durable-success install whose provenance write
    then RAISES must still NAME the retained restorable stale in the outcome log
    and leave it on disk (never restored — the install durably succeeded).

    The generic ``except`` catches the provenance failure, but the ``finally``
    still sees ``durable_success`` True, so it restores nothing and reports with
    ``filter_restorable=not durable_success`` == ``False`` — naming the stale
    that genuinely stays at ``.stale-*`` rather than letting it sit unlogged
    until the age-based sweep. With the pre-fix ``filter_restorable=True`` mirror
    this path filtered the stale out and reported nothing.
    """

    @pytest.mark.asyncio
    async def test_provenance_raise_reports_retained_restorable_stale(self, tmp_path):
        from kiro_crew.apps.registry import install_from_registry

        pkg_dir = tmp_path / "testapp"
        pkg_dir.mkdir()
        (pkg_dir / "app.json").write_text('{"name": "testapp"}', encoding="utf-8")
        (pkg_dir / "installed.txt").write_text("the installed version", encoding="utf-8")
        stale_dir = tmp_path / "testapp.stale-deadbeef"
        stale_dir.mkdir()
        (stale_dir / "my-work.txt").write_text("important", encoding="utf-8")

        async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
            return {
                "ok": True,
                "pkg_dir": pkg_dir,
                "_pending_stale_cleanup": [stale_dir],
                # Restorable (branch drift, same repository) — but this
                # durable-success path's `finally` never restores it, so it
                # genuinely stays at `.stale-*` and must be named.
                "_restorable_stale": [stale_dir],
            }

        class _Ok:
            ok = True
            name = "testapp"
            message = "installed"
            error = None

        def _boom(*a, **k):
            raise OSError("provenance store unwritable")

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={"repo": "https://example.com/app.git", "branch": "main"},
            ),
            patch(
                "kiro_crew.apps.registry._entry_git_url",
                return_value="https://example.com/app.git",
            ),
            patch("kiro_crew.apps.registry._clone_build_app", new=_fake_clone_build),
            patch("kiro_crew.apps.registry.app_admission_denied", return_value=None),
            patch("kiro_crew.apps.registry.app_execution_denied", return_value=None),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value=None),
            ),
            patch("kiro_crew.apps.registry.get_app", return_value=None),
            patch("kiro_crew.apps.registry.install_app", return_value=_Ok()),
            patch("kiro_crew.apps.registry.app_source_dir", return_value=pkg_dir),
            patch("kiro_crew.apps.registry.set_app_provenance", side_effect=_boom),
            patch("kiro_crew.apps.registry._sweep_stale_checkouts", new=AsyncMock()),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await install_from_registry("testapp")

        # The bookkeeping failure is surfaced, but the install is NOT rolled back.
        assert result["ok"] is False
        assert (pkg_dir / "installed.txt").read_text(encoding="utf-8") == "the installed version"
        # The restorable stale is RETAINED (durable success never restores it)...
        assert stale_dir.exists(), "a durable success never restores the stale"
        assert (stale_dir / "my-work.txt").read_text(encoding="utf-8") == "important"
        # ...and it is NAMED in the outcome log, not stranded until the sweep.
        assert f"Previous checkout retained at: {stale_dir}" in result.get("log", ""), (
            "a provenance raise after durable success must still report the "
            "genuinely-retained restorable stale"
        )


class TestFinallyOwnedReporterIsTheSoleSite:
    """Grep-pin (Design + First Principles round 9): retained-stale reporting is
    owned by exactly ONE site — the ``finally`` of ``install_from_registry``,
    with ``filter_restorable=not durable_success``. The 12 per-exit calls this
    consolidation deleted were the scattered-per-exit stranding class; a new exit
    re-introducing a per-exit call (and its own hand-mirrored flag) would
    reopen it, so pin the single site textually."""

    def test_exactly_one_reporter_call_in_install_from_registry(self):
        import inspect

        from kiro_crew.apps import registry as reg

        source = inspect.getsource(reg.install_from_registry)
        calls = source.count("_report_retained_stale_checkouts(")
        assert calls == 1, (
            "install_from_registry must call _report_retained_stale_checkouts "
            f"exactly once (the finally-owned site); found {calls}"
        )
        # The single call must derive its flag from durable_success, never
        # hand-mirror a literal True/False at a per-exit site.
        assert "filter_restorable=not durable_success" in source, (
            "the sole reporter call must use filter_restorable=not durable_success"
        )


class TestRefusalOutcomeCarriesNoInternalTransactionKeys:
    """Regression (GPT 5.6 round 10, registry.py:~4925): a build-refusal exit
    does ``outcome = {**build_result}``, so the internal move-aside bookkeeping
    keys ``_pending_stale_cleanup`` / ``_restorable_stale`` (each ``list[Path]``)
    used to ride out of ``install_from_registry`` on the returned dict. ``Path``
    is not JSON-serializable, so the API/SSE layer raised ``TypeError`` when it
    serialized the refusal.

    The finally now scrubs every ``_``-prefixed key from ``outcome`` AFTER the
    restore/report have consumed them off ``build_result``, so the returned
    outcome is JSON-serializable and carries no internal transaction key — while
    the retained-path log line the reporter produced still reaches the caller.
    """

    @pytest.mark.asyncio
    async def test_build_refusal_outcome_is_json_serializable_and_scrubbed(self, tmp_path):
        from kiro_crew.apps.registry import install_from_registry

        app_sources = tmp_path / "app-sources"
        app_sources.mkdir()
        pkg_dir = app_sources / "testapp"
        stale_dir = app_sources / "testapp.stale-deadbeef"
        stale_dir.mkdir()
        (stale_dir / "someone-elses-repo.txt").write_text("not restorable", encoding="utf-8")

        async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
            # A pre-build refusal (failed clone/build or an in-callee gate):
            # ok=False, carrying the origin-mismatch move-aside state as the
            # real single-exit stamping does. `_restorable_stale` empty → the
            # pending path is a genuinely-retained non-restorable checkout.
            return {
                "ok": False,
                "name": app_name,
                "error": "build failed",
                "_pending_stale_cleanup": [stale_dir],
                "_restorable_stale": [],
            }

        with (
            patch(
                "kiro_crew.apps.registry.get_registry_app",
                return_value={"repo": "https://example.com/app.git", "branch": "main"},
            ),
            patch(
                "kiro_crew.apps.registry._entry_git_url",
                return_value="https://example.com/app.git",
            ),
            patch("kiro_crew.apps.registry._clone_build_app", new=_fake_clone_build),
            patch("kiro_crew.apps.registry.app_source_dir", return_value=pkg_dir),
            patch("kiro_crew.apps.registry.app_admission_denied", return_value=None),
            patch("kiro_crew.apps.registry.app_execution_denied", return_value=None),
            patch(
                "kiro_crew.apps.registry._fetch_app_manifest",
                new=AsyncMock(return_value=None),
            ),
            patch("kiro_crew.apps.registry._sweep_stale_checkouts", new=AsyncMock()),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await install_from_registry("testapp")

        assert result["ok"] is False
        # The whole point of the round-10 fix: the refusal serializes cleanly.
        json.dumps(result)
        # No internal transaction key rides out on the returned outcome.
        assert "_pending_stale_cleanup" not in result
        assert "_restorable_stale" not in result
        # Scrub the CLASS, not the two names: no leaked `_`-prefixed key at all.
        leaked = [k for k in result if k.startswith("_")]
        assert leaked == [], f"internal underscore keys leaked into the outcome: {leaked}"
        # The retained non-restorable path is still named in the log the caller
        # receives — the scrub strips the keys, not the report.
        assert f"Previous checkout retained at: {stale_dir}" in result.get("log", "")
        assert stale_dir.exists(), "a non-restorable stale must never be swept unreported"


class TestReadCloneBranchBoundedRead:
    """Round-11 regression (GPT 5.6): the ``.git/HEAD`` read must be BOUNDED.

    ``.git/HEAD`` lives inside a checkout an app's own build script can rewrite,
    so its size is attacker-controlled. Reading it whole let an app replace the
    file with (or symlink it to) a multi-gigabyte / sparse file and exhaust
    gateway memory on the next update. The read is now capped at
    ``_HEAD_READ_LIMIT`` bytes, and content that fills the bound fails closed.
    """

    def test_oversized_head_is_not_read_past_the_bound(self, tmp_path):
        """An oversized ``.git/HEAD`` never pulls more than the bound into
        memory: record the byte count passed to the bounded reader and confirm
        it is capped at ``_HEAD_READ_LIMIT``.

        The bounded read now runs through ``hooks.safe_read_prefix`` (the
        symlink-containing primitive), so the boundedness proof is the ``n``
        argument that reader receives, not a ``builtins.open`` read size."""
        from kiro_crew.apps import registry as reg

        clone_dir = tmp_path / "clone"
        (clone_dir / ".git").mkdir(parents=True)
        head_file = clone_dir / ".git" / "HEAD"
        # A `ref:` line followed by megabytes of padding — a real HEAD is a
        # single short line, so anything this large is hostile.
        head_file.write_text(
            "ref: refs/heads/main\n" + ("A" * (5 * 1024 * 1024)),
            encoding="utf-8",
        )

        from kiro_crew import hooks

        real_prefix = hooks.safe_read_prefix
        limit_args: list[int] = []

        def _recording_prefix(raw, n):
            # A concrete positive bound is the only legal shape; the unbounded
            # read this test forbids would be a whole-file read() with no cap.
            limit_args.append(n)
            return real_prefix(raw, n)

        with patch.object(hooks, "safe_read_prefix", side_effect=_recording_prefix):
            branch = reg._read_clone_branch(clone_dir)

        # The read was bounded: every bounded-reader call was capped at the
        # limit, never an unbounded read that would slurp the whole 5 MiB file.
        assert limit_args, "the HEAD read never happened"
        assert all(
            0 <= n <= reg._HEAD_READ_LIMIT for n in limit_args
        ), f"HEAD was read past the bound: read sizes {limit_args}"
        # Content that fills the bound (this hostile file does) fails closed.
        assert branch is None, "an oversized HEAD must fail closed, not return a branch"

    def test_head_content_filling_the_bound_fails_closed(self, tmp_path):
        """A ``ref:`` line padded exactly to the byte bound is treated as
        malformed (possible mid-token truncation) and returns None."""
        from kiro_crew.apps import registry as reg

        clone_dir = tmp_path / "clone"
        (clone_dir / ".git").mkdir(parents=True)
        head_file = clone_dir / ".git" / "HEAD"
        # A branch name long enough that the whole line meets/exceeds the bound.
        long_branch = "b" * (reg._HEAD_READ_LIMIT + 100)
        head_file.write_text(f"ref: refs/heads/{long_branch}\n", encoding="utf-8")

        assert reg._read_clone_branch(clone_dir) is None

    def test_normal_head_still_reads_the_branch(self, tmp_path):
        """Control: a well-formed short HEAD still resolves to its branch —
        the bound rejects only oversized/hostile content."""
        from kiro_crew.apps import registry as reg

        clone_dir = tmp_path / "clone"
        (clone_dir / ".git").mkdir(parents=True)
        (clone_dir / ".git" / "HEAD").write_text(
            "ref: refs/heads/feature/some-branch\n", encoding="utf-8"
        )

        assert reg._read_clone_branch(clone_dir) == "feature/some-branch"

    def test_head_symlinked_to_oversized_file_fails_closed(self, tmp_path):
        """A ``.git/HEAD`` symlinked to a huge file is still bounded — the read
        follows the link but stops at the limit, and the oversized content
        fails closed."""
        from kiro_crew.apps import registry as reg

        clone_dir = tmp_path / "clone"
        (clone_dir / ".git").mkdir(parents=True)
        big = tmp_path / "big-file"
        big.write_text("ref: refs/heads/main\n" + ("A" * (3 * 1024 * 1024)), encoding="utf-8")
        head_file = clone_dir / ".git" / "HEAD"
        try:
            head_file.symlink_to(big)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        # Fails closed (oversized), and — the point — without loading 3 MiB:
        # the bounded read is what makes the symlink target's size irrelevant.
        assert reg._read_clone_branch(clone_dir) is None

    def test_head_symlinked_to_sensitive_path_is_refused(self, tmp_path):
        """Round-14 regression (GPT 5.6, BLOCKING): a checkout-controlled
        ``.git/HEAD`` symlinked to a protected file must NOT be read.

        A malicious app can replace ``.git/HEAD`` with a symlink to a secret
        (``~/.aws/credentials``, an SSH key). The bounded read now routes
        through ``hooks.safe_read_prefix``, which canonicalizes via ``realpath``
        and refuses a resolved target ``is_sensitive_path`` flags before any
        read, so the branch read fails closed to None and the secret bytes never
        leave the sensitive-path ceiling — even though a valid ``ref:`` line at
        the target would otherwise parse to a branch."""
        from kiro_crew import hooks
        from kiro_crew.apps import registry as reg

        # A secret file that resolves to a *sensitive* path. Its content is a
        # perfectly valid HEAD line, so if the read followed the link it would
        # return a branch — the refusal, not malformed content, is the defense.
        secret = tmp_path / "credentials"
        secret.write_text("ref: refs/heads/leaked\n", encoding="utf-8")

        clone_dir = tmp_path / "clone"
        (clone_dir / ".git").mkdir(parents=True)
        head_file = clone_dir / ".git" / "HEAD"
        try:
            head_file.symlink_to(secret)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        resolved_secret = os.path.realpath(secret)
        real_is_sensitive = hooks.is_sensitive_path

        def _sensitive(path, base_dir=None):
            # Flag only the symlink's resolved target as sensitive, so the test
            # does not depend on the host actually having ~/.aws etc.
            if os.path.realpath(path) == resolved_secret:
                return True
            return real_is_sensitive(path, base_dir)

        with patch.object(hooks, "is_sensitive_path", side_effect=_sensitive):
            branch = reg._read_clone_branch(clone_dir)

        assert branch is None, (
            "a .git/HEAD symlinked to a sensitive path must be refused, not "
            "followed through the sensitive-path ceiling"
        )

    def test_bounded_read_of_sensitive_symlink_returns_none(self, tmp_path):
        """Unit-level twin of the above at the primitive: the shared
        ``_read_git_metadata_bounded`` returns None (not the target's bytes) for
        a path whose resolved target is sensitive, so EVERY call site (loose
        ref, ``packed-refs``, provenance HEAD) is contained, not just the
        branch read."""
        from kiro_crew import hooks
        from kiro_crew.apps import registry as reg

        secret = tmp_path / "id_rsa"
        secret.write_text("PRIVATE KEY MATERIAL\n", encoding="utf-8")
        link = tmp_path / "clone" / ".git" / "some-ref"
        link.parent.mkdir(parents=True)
        try:
            link.symlink_to(secret)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        resolved_secret = os.path.realpath(secret)
        real_is_sensitive = hooks.is_sensitive_path

        def _sensitive(path, base_dir=None):
            if os.path.realpath(path) == resolved_secret:
                return True
            return real_is_sensitive(path, base_dir)

        with patch.object(hooks, "is_sensitive_path", side_effect=_sensitive):
            result = reg._read_git_metadata_bounded(link, reg._HEAD_READ_LIMIT)

        assert result is None, "the bounded reader must refuse a sensitive symlink target"


class TestResolvedCloneCommitBoundedRead:
    """Round-11 class closure: :func:`_resolved_clone_commit` reads the SAME
    ``.git/HEAD`` primitive on the install (provenance) path, so it must be
    bounded too — otherwise the memory-exhaustion class stays open from the
    next call site even after :func:`_read_clone_branch` is fixed.
    """

    def test_oversized_head_fails_closed_without_unbounded_read(self, tmp_path):
        from kiro_crew.apps import registry as reg

        clone = tmp_path / "clone"
        (clone / ".git").mkdir(parents=True)
        head_file = clone / ".git" / "HEAD"
        # Detached-HEAD shape padded past the bound — a real detached HEAD is a
        # single 40/64-char SHA line.
        head_file.write_text("f" * (2 * 1024 * 1024), encoding="utf-8")

        from kiro_crew import hooks

        real_prefix = hooks.safe_read_prefix
        limit_args: list[int] = []

        def _recording_prefix(raw, n):
            limit_args.append(n)
            return real_prefix(raw, n)

        with patch.object(hooks, "safe_read_prefix", side_effect=_recording_prefix):
            sha = reg._resolved_clone_commit(clone)

        # Provenance degrades to "" (unknown) rather than loading 2 MiB.
        assert sha == ""
        assert limit_args, "HEAD was never read"
        assert all(
            0 <= n <= reg._HEAD_READ_LIMIT for n in limit_args
        ), f"HEAD read past the bound: {limit_args}"

    def test_packed_refs_read_is_bounded(self, tmp_path):
        """``packed-refs`` is checkout-resident, so its read is bounded — an
        oversized file is not slurped whole. Asserted by counting the largest
        ``read(n)`` on that file: the unbounded original reads it all."""
        from kiro_crew.apps import registry as reg

        clone = tmp_path / "clone"
        git_dir = clone / ".git"
        git_dir.mkdir(parents=True)
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        packed = git_dir / "packed-refs"
        # A multi-MiB packed-refs — far past the bound, no matching ref line.
        packed.write_text("x" * (4 * 1024 * 1024), encoding="utf-8")

        from kiro_crew import hooks

        real_prefix = hooks.safe_read_prefix
        packed_read_args: list[int] = []

        def _recording_prefix(raw, n):
            # Record only the packed-refs read; HEAD is read through the same
            # primitive but with the smaller HEAD bound.
            if Path(raw) == packed:
                packed_read_args.append(n)
            return real_prefix(raw, n)

        with patch.object(hooks, "safe_read_prefix", side_effect=_recording_prefix):
            sha = reg._resolved_clone_commit(clone)

        assert sha == ""  # no matching ref → unknown provenance
        assert packed_read_args, "packed-refs was never read through the bounded reader"
        # The bounded read passes a concrete positive size; an unbounded read
        # would slurp the whole 4 MiB file. Use the file size as the ceiling so
        # this fails behaviorally on an unbounded read regardless of the exact
        # bound constant.
        assert all(
            0 <= n <= 4 * 1024 * 1024 for n in packed_read_args
        ), f"packed-refs read unbounded (read sizes {packed_read_args})"

    def test_normal_detached_head_still_resolves(self, tmp_path):
        """Control: a well-formed detached HEAD still yields its SHA — the
        bound rejects only oversized content."""
        from kiro_crew.apps import registry as reg

        clone = tmp_path / "clone"
        (clone / ".git").mkdir(parents=True)
        sha = "a" * 40
        (clone / ".git" / "HEAD").write_text(sha + "\n", encoding="utf-8")

        assert reg._resolved_clone_commit(clone) == sha


class TestCommitPinnedSkipsReconvergenceRead:
    """Round-11 regression (GPT 5.6): a commit-pinned install must NOT perform
    the branch-reconvergence ``.git/HEAD`` read.

    A pinned install never reuses the existing tree (it is moved aside and the
    commit is re-fetched into a fresh checkout regardless), so reading the old
    tree's branch is pointless work — and pointless attack surface on a path
    that runs during every pinned update against attacker-writable content. The
    read is now gated behind ``not commit``.
    """

    @pytest.mark.asyncio
    async def test_pinned_install_never_calls_read_clone_branch(self, tmp_path):
        """With ``commit`` set, ``_read_clone_branch`` is never invoked even
        though a ``.git`` checkout on a DIFFERENT branch is present."""
        from kiro_crew.apps import registry as reg

        dest = tmp_path / "app-sources" / "pinned-app"
        (dest / ".git").mkdir(parents=True)
        # A concrete, different branch — if the reconvergence read ran it would
        # try to move this aside. The gate must skip the read entirely.
        (dest / ".git" / "HEAD").write_text("ref: refs/heads/old-branch\n", encoding="utf-8")

        read_calls: list[Path] = []
        real_read = reg._read_clone_branch

        def _counting_read(clone_dir):
            read_calls.append(clone_dir)
            return real_read(clone_dir)

        async def _fake_move_aside(d, log_lines):
            # Behave like a successful move-aside without touching disk.
            return d.with_name(f"{d.name}.stale-test")

        # The pinned fetch returns a failure dict so the function returns before
        # doing any real work after the reconvergence gate.
        fetch_result = {"ok": False, "name": "pinned-app", "error": "fetch stub"}
        git_url = "https://example.com/pinned-app.git"

        with (
            patch.object(reg, "_read_clone_branch", side_effect=_counting_read),
            patch.object(reg, "_move_checkout_aside", side_effect=_fake_move_aside),
            patch.object(reg, "_git_fetch_commit", new=AsyncMock(return_value=fetch_result)),
            # Origin verified identical so the origin gate passes and control
            # reaches the (now commit-gated) reconvergence block.
            patch.object(reg, "_clone_origin_url", new=AsyncMock(return_value=git_url)),
            patch("kiro_crew.apps.registry.app_source_dir", return_value=dest),
            patch(
                "kiro_crew.apps.registry.is_clone_host_trusted",
                return_value=True,
            ),
        ):
            result = await reg._clone_build_app_locked(
                git_url,
                "pinned-app",
                [],
                branch="main",
                commit="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                pending_cleanup=[],
                restorable_stale=[],
            )

        assert result == fetch_result
        assert read_calls == [], (
            "a commit-pinned install must skip the branch-reconvergence "
            f"read, but _read_clone_branch was called with {read_calls}"
        )

    @pytest.mark.asyncio
    async def test_branch_tracking_install_does_call_read_clone_branch(self, tmp_path):
        """Control: with ``commit`` unset, the reconvergence read DOES run —
        proving the skip is specific to the pinned path, not a dead gate."""
        from kiro_crew.apps import registry as reg

        dest = tmp_path / "app-sources" / "tracking-app"
        (dest / ".git").mkdir(parents=True)
        (dest / ".git" / "HEAD").write_text("ref: refs/heads/old-branch\n", encoding="utf-8")

        git_url = "https://example.com/tracking-app.git"

        def _tripwire_read(clone_dir):
            raise RuntimeError("reconvergence read reached")

        with (
            patch.object(reg, "_read_clone_branch", side_effect=_tripwire_read),
            # Origin verified identical (the check that precedes the
            # reconvergence read) so the branch path is actually reached.
            patch.object(reg, "_clone_origin_url", new=AsyncMock(return_value=git_url)),
            patch("kiro_crew.apps.registry.app_source_dir", return_value=dest),
            patch(
                "kiro_crew.apps.registry.is_clone_host_trusted",
                return_value=True,
            ),
        ):
            # The read runs (inside asyncio.to_thread), so its exception
            # propagates — that is the proof it was reached on the branch path.
            with pytest.raises(RuntimeError, match="reconvergence read reached"):
                await reg._clone_build_app_locked(
                    git_url,
                    "tracking-app",
                    [],
                    branch="main",
                    commit="",
                    pending_cleanup=[],
                    restorable_stale=[],
                )


class TestMoveAsideUtimeFailureUndoesRename:
    """Round-11 regression (GPT 5.6): a failed mtime refresh must NOT forfeit
    stale-checkout retention.

    ``_rename_and_refresh_mtime`` refreshes the checkout's mtime FIRST, then
    renames it aside, so the moved-aside dir gets the full retention window and
    never appears under its ``.stale-*`` name carrying a stale clock. If the
    ``utime`` fails and the failure is swallowed, the checkout would end up
    aside with its original (possibly already-expired) mtime and the next
    install's age-based sweep would delete the user's recovery copy. The refresh
    is not best-effort: it fails CLOSED before anything moves — the utime error
    re-raises while ``dest`` is still at its original path, so
    ``_move_checkout_aside`` returns None and the caller fails closed with
    ``stale_clone_not_removed``. The checkout stays in place, never stranded
    with an expired clock.
    """

    @pytest.mark.asyncio
    async def test_utime_failure_undoes_the_move_aside(self, tmp_path):
        """``os.utime`` raising OSError leaves ``dest`` in place with no
        ``.stale-*`` sibling, and the move-aside returns None (fail closed).

        With the pre-rename refresh the failure fires before ``dest`` is moved,
        so there is nothing to undo — but the observable contract is unchanged:
        the checkout stays put and no aside is handed back."""
        from kiro_crew.apps import registry as reg

        dest = tmp_path / "testapp"
        dest.mkdir()
        (dest / "marker.txt").write_text("original", encoding="utf-8")

        def _boom(*args, **kwargs):
            raise OSError("timestamps not permitted on this filesystem")

        log_lines: list[str] = []
        with patch("kiro_crew.apps.registry.os.utime", side_effect=_boom):
            aside = await reg._move_checkout_aside(dest, log_lines)

        # Fail closed: no aside path handed back.
        assert aside is None
        # The refresh failed before the rename, so dest never moved: it is
        # still in place with its original contents.
        assert dest.exists()
        assert (dest / "marker.txt").read_text(encoding="utf-8") == "original"
        # No stranded .stale-* sibling holding an expired clock.
        stranded = list(tmp_path.glob("testapp.stale-*"))
        assert stranded == [], f"a .stale-* sibling was stranded: {stranded}"

    @pytest.mark.asyncio
    async def test_utime_failure_makes_install_fail_closed(self, tmp_path):
        """End to end: a branch-drift install whose ``os.utime`` fails during
        move-aside refuses with ``stale_clone_not_removed`` and leaves the old
        checkout in place, rather than moving it aside with an expired mtime."""
        from kiro_crew.apps import registry as reg

        dest = tmp_path / "app-sources" / "drift-app"
        (dest / ".git").mkdir(parents=True)
        # Different branch than requested → triggers the reconvergence move-aside.
        (dest / ".git" / "HEAD").write_text("ref: refs/heads/old-branch\n", encoding="utf-8")

        git_url = "https://example.com/drift-app.git"

        def _boom(*args, **kwargs):
            raise OSError("timestamps not permitted on this filesystem")

        with (
            patch("kiro_crew.apps.registry.os.utime", side_effect=_boom),
            # Origin verified identical → the origin gate does not move aside;
            # only the branch-drift reconvergence below does, which is where the
            # utime failure must fail the whole install closed.
            patch.object(reg, "_clone_origin_url", new=AsyncMock(return_value=git_url)),
            patch("kiro_crew.apps.registry.app_source_dir", return_value=dest),
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
        ):
            result = await reg._clone_build_app_locked(
                git_url,
                "drift-app",
                [],
                branch="main",
                commit="",
                pending_cleanup=[],
                restorable_stale=[],
            )

        assert result["ok"] is False
        assert result["error"] == "stale_clone_not_removed"
        # The old checkout is left in place (fail closed), not stranded aside.
        assert (dest / ".git").is_dir()
        stranded = list((tmp_path / "app-sources").glob("drift-app.stale-*"))
        assert stranded == [], f"a .stale-* sibling was stranded with an expired clock: {stranded}"


class TestBuildFailureRestoreRespectsRestorableStale:
    """Round-12 regression (GPT 5.6): the build-failure restore loop inside
    ``_clone_build_app_locked`` must restore ONLY ``restorable_stale`` members.

    ``pending_cleanup`` carries every move-aside a run made — both
    same-origin/branch-drift asides (restorable) AND origin-mismatch asides the
    identity gate deliberately refused to serve. The old loop iterated ALL of
    ``pending_cleanup`` and renamed each aside back into ``pkg_dir`` whenever the
    fresh clone's build failed. Chain: an entry repointed to a different origin
    → old checkout moved aside (NON-restorable) → fresh clone BUILDS but the
    build fails → the loop renamed the origin-mismatched old checkout back into
    the active source slot, re-seating a repository the gate had just refused.

    After the fix only ``restorable_stale`` members are put back; a
    non-restorable aside stays a ``.stale-*`` sibling and is named "retained at:"
    in the returned log.
    """

    @staticmethod
    def _make_fake_clone(stale_dir, *, restorable):
        async def _fake_clone(git_url, branch, dest, log_lines, **kwargs):
            # Simulate the origin-mismatch (or branch-drift) move-aside having
            # happened during the clone: the old checkout is a .stale-* sibling
            # and a FRESH clone now sits at dest with a CORRECT-name manifest so
            # the identity gate passes and control reaches the build step.
            pending_cleanup = kwargs.get("pending_cleanup")
            if pending_cleanup is not None:
                pending_cleanup.append(stale_dir)
            restorable_stale = kwargs.get("restorable_stale")
            if restorable and restorable_stale is not None:
                restorable_stale.append(stale_dir)
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "app.json").write_text(json.dumps({"name": "testapp"}), encoding="utf-8")
            return None

        return _fake_clone

    @pytest.mark.asyncio
    async def test_origin_mismatch_stale_not_restored_on_build_failure(self, tmp_path):
        """Origin-mismatch aside + successful fresh clone + FAILED build → the
        origin-mismatched old checkout is NOT renamed back into the active slot
        (it stays a ``.stale-*`` sibling) and the returned log names it retained.

        Fails at head 17372a70 (the loop renamed every aside back); passes
        after the restorable-membership gate.
        """
        from kiro_crew.apps.registry import _clone_build_app

        app_source = tmp_path / "app-sources" / "testapp"
        stale_dir = tmp_path / "app-sources" / "testapp.stale-deadbeef"
        stale_dir.mkdir(parents=True)
        (stale_dir / "someone-elses-repo.txt").write_text("refused origin", encoding="utf-8")

        with (
            patch("kiro_crew.apps.registry.app_source_dir", return_value=app_source),
            patch(
                "kiro_crew.apps.registry._git_clone_or_pull",
                new=self._make_fake_clone(stale_dir, restorable=False),
            ),
            patch(
                "kiro_crew.apps.registry._run_app_build",
                new=AsyncMock(return_value={"ok": False, "name": "testapp"}),
            ),
            patch("kiro_crew.apps.registry.sel"),
        ):
            log_lines: list[str] = []
            result = await _clone_build_app(
                "https://example.com/app.git", "testapp", log_lines
            )

        assert not result["ok"]
        # The refused-origin checkout was NOT re-seated into the active slot.
        assert stale_dir.exists(), "the origin-mismatched aside must stay .stale-*"
        assert (stale_dir / "someone-elses-repo.txt").read_text(
            encoding="utf-8"
        ) == "refused origin", "the refused checkout must be untouched on disk"
        # It must NOT occupy the active source slot the gate refused to serve.
        if app_source.exists():
            assert not (app_source / "someone-elses-repo.txt").exists(), (
                "the refused-origin checkout must never occupy the active slot"
            )
        # It is reported retained (both in the returned log and the stamp),
        # never silently swept.
        joined = "\n".join(log_lines) + "\n" + result.get("log", "")
        assert "retained at" in joined, "the non-restorable aside must be named retained"
        assert stale_dir in (result.get("_pending_stale_cleanup") or []), (
            "the non-restorable aside must remain in pending_cleanup for the reporter"
        )

    @pytest.mark.asyncio
    async def test_branch_drift_stale_is_restored_on_build_failure(self, tmp_path):
        """Control: a same-origin branch-drift aside (RESTORABLE) + failed build
        → the old checkout IS renamed back into the slot, with the existing
        "previous checkout restored" log line. Existing behaviour unchanged.
        """
        from kiro_crew.apps.registry import _clone_build_app

        app_source = tmp_path / "app-sources" / "testapp"
        stale_dir = tmp_path / "app-sources" / "testapp.stale-cafef00d"
        stale_dir.mkdir(parents=True)
        (stale_dir / "my-work.txt").write_text("important local edits", encoding="utf-8")

        with (
            patch("kiro_crew.apps.registry.app_source_dir", return_value=app_source),
            patch(
                "kiro_crew.apps.registry._git_clone_or_pull",
                new=self._make_fake_clone(stale_dir, restorable=True),
            ),
            patch(
                "kiro_crew.apps.registry._run_app_build",
                new=AsyncMock(return_value={"ok": False, "name": "testapp"}),
            ),
            patch("kiro_crew.apps.registry.sel"),
        ):
            log_lines: list[str] = []
            result = await _clone_build_app(
                "https://example.com/app.git", "testapp", log_lines
            )

        assert not result["ok"]
        # The restorable aside was put back into the active slot.
        assert not stale_dir.exists(), "the restorable aside is renamed back into pkg_dir"
        assert app_source.exists()
        assert (app_source / "my-work.txt").read_text(
            encoding="utf-8"
        ) == "important local edits"
        joined = "\n".join(log_lines) + "\n" + result.get("log", "")
        assert "previous checkout restored" in joined, (
            "a restored aside keeps the existing restore log line"
        )
        # A restored aside is dropped from pending_cleanup (no longer retained).
        assert stale_dir not in (result.get("_pending_stale_cleanup") or [])

    @pytest.mark.asyncio
    async def test_restorable_restore_rename_oserror_reports_hint_and_keeps_pending(
        self, tmp_path
    ):
        """Control: a RESTORABLE aside whose restore-rename raises OSError still
        reports the recovery hint and keeps the path in pending_cleanup so it is
        reported stranded (existing failure-path behaviour unchanged)."""
        from kiro_crew.apps import registry as reg
        from kiro_crew.apps.registry import _clone_build_app

        app_source = tmp_path / "app-sources" / "testapp"
        stale_dir = tmp_path / "app-sources" / "testapp.stale-beefbeef"
        stale_dir.mkdir(parents=True)
        (stale_dir / "my-work.txt").write_text("important", encoding="utf-8")

        real_to_thread = asyncio.to_thread

        async def _to_thread_fail_rename(fn, *args, **kwargs):
            # Fail only the restore rename (stale_dir.rename), pass everything
            # else (the rmtree of the failed fresh clone) through.
            if getattr(fn, "__name__", "") == "rename":
                raise OSError("simulated restore rename failure")
            return await real_to_thread(fn, *args, **kwargs)

        with (
            patch("kiro_crew.apps.registry.app_source_dir", return_value=app_source),
            patch(
                "kiro_crew.apps.registry._git_clone_or_pull",
                new=self._make_fake_clone(stale_dir, restorable=True),
            ),
            patch(
                "kiro_crew.apps.registry._run_app_build",
                new=AsyncMock(return_value={"ok": False, "name": "testapp"}),
            ),
            patch.object(reg.asyncio, "to_thread", side_effect=_to_thread_fail_rename),
            patch("kiro_crew.apps.registry.sel"),
        ):
            log_lines: list[str] = []
            result = await _clone_build_app(
                "https://example.com/app.git", "testapp", log_lines
            )

        assert not result["ok"]
        joined = "\n".join(log_lines) + "\n" + result.get("log", "")
        assert "could not restore previous checkout" in joined, (
            "the OSError restore path must surface the recovery hint"
        )
        # A rename that FAILED stays in pending_cleanup so it is still reported.
        assert stale_dir in (result.get("_pending_stale_cleanup") or [])


class TestMoveAsideUndoFailureReportsRetainedPath:
    """Round-12b + round-14 regression (GPT 5.6): a checkout ever left stranded
    at a ``.stale-*`` aside path MUST have that exact path named in the log,
    rather than left for the age-based sweep to delete an unreported recovery
    copy.

    Round 14 moved the mtime refresh BEFORE the rename (so the moved-aside dir
    never appears under its sweepable name with a stale clock), which means a
    ``utime`` failure now fails closed while ``dest`` is still at its original
    path — there is no rename to undo, nothing is stranded, and the honest
    report names ``dest`` (see :class:`TestMoveAsideUtimeFailureUndoesRename`).
    The :class:`_MoveAsideUndoFailed` retained-path contract is preserved as
    the caller's handler for any residual stranding path: when it fires, the
    exact ``aside`` path it carries is what the log names.
    """

    @pytest.mark.asyncio
    async def test_move_aside_undo_failed_handler_names_retained_aside(self, tmp_path):
        """The caller's ``_MoveAsideUndoFailed`` handler names the exact
        stranded ``.stale-*`` path. Drive the contract directly: the worker
        raises ``_MoveAsideUndoFailed`` carrying an on-disk aside (the residual
        strand shape), and ``_move_checkout_aside`` must fail closed (return
        None) and log a "Previous checkout retained at: <aside>" line naming
        that exact path — never a generic dest-only line that would leave the
        strand unreported."""
        from kiro_crew.apps import registry as reg

        dest = tmp_path / "testapp"
        dest.mkdir()
        # A stranded aside actually present on disk, as the contract describes.
        aside = dest.with_name("testapp.stale-deadbeef")
        aside.mkdir()
        (aside / "marker.txt").write_text("original", encoding="utf-8")

        def _raise_undo_failed(d, a):
            raise reg._MoveAsideUndoFailed(aside, OSError("undo could not land"))

        log_lines: list[str] = []
        with patch.object(reg, "_rename_and_refresh_mtime", side_effect=_raise_undo_failed):
            result = await reg._move_checkout_aside(dest, log_lines)

        # Failed closed.
        assert result is None
        # The exact retained path is named, not swept unreported.
        retained_lines = [ln for ln in log_lines if "Previous checkout retained at" in ln]
        assert retained_lines, f"the retained aside must be named; got {log_lines!r}"
        assert str(aside) in retained_lines[0], "the log must name the true on-disk aside path"

    @pytest.mark.asyncio
    async def test_utime_failure_before_rename_names_dest_not_retained(self, tmp_path):
        """Round-14 ordering: ``utime`` runs on ``dest`` BEFORE the rename, so a
        failure fires while the checkout is still at ``dest`` — nothing is moved,
        nothing is stranded, and the honest report is the generic
        "Could not move aside ... at <dest>" line with no "retained at" line."""
        from kiro_crew.apps import registry as reg

        dest = tmp_path / "testapp"
        dest.mkdir()
        (dest / "marker.txt").write_text("original", encoding="utf-8")
        old_time = time.time() - 3600
        os.utime(dest, (old_time, old_time))

        real_utime = os.utime

        def _fake_utime(path, *args, **kwargs):
            # The refresh now targets dest (pre-rename); fail it there.
            if Path(path) == dest:
                raise OSError("simulated utime failure")
            return real_utime(path, *args, **kwargs)

        log_lines: list[str] = []
        with patch.object(reg.os, "utime", side_effect=_fake_utime):
            aside = await reg._move_checkout_aside(dest, log_lines)

        assert aside is None
        # dest is still in place with its original contents; nothing stranded.
        assert dest.exists()
        assert (dest / "marker.txt").read_text(encoding="utf-8") == "original"
        assert list(tmp_path.glob("testapp.stale-*")) == []
        assert any("Could not move aside" in ln for ln in log_lines)
        assert not any(
            "retained at" in ln for ln in log_lines
        ), "a fail-closed pre-rename refresh strands nothing, so no retained line is owed"
