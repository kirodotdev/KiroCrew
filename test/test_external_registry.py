"""Tests for kiro_crew.apps.registry — External (federated) registry support."""

from __future__ import annotations

import asyncio
import json
import os
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

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _explicit_registry_execution_admission(monkeypatch):
    """Registry tests exercise admitted installs unless they say otherwise."""
    monkeypatch.setattr("kiro_crew.apps.execution.third_party_execution_allowed", lambda: True)


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
# _registry_git_url — clone-URL resolution for the blob proxy
# ---------------------------------------------------------------------------


class TestRegistryGitUrl:
    """`_registry_git_url` must resolve URL-form repos even when the bundled
    lookup (`get_registry_app_by_repo`) finds no entry.

    Regression for the asymmetric-lookup boundary (GPT 5.6 MEDIUM): the PR
    widened `_is_safe_repo_identifier` to admit full git URLs, but the resolver
    used to early-return `None` whenever the bundled registry had no matching
    entry — making external-registry blobs (whose `repo` IS a full git URL)
    unreachable.
    """

    def test_url_repo_resolves_without_bundled_entry(self, monkeypatch):
        from kiro_crew.apps import routes

        # No bundled entry for this URL-form repo.
        monkeypatch.setattr(routes, "get_registry_app_by_repo", lambda repo: None)

        for url in (
            "https://github.com/acme/apps",
            "git@github.com:acme/apps.git",
            "ssh://git@example.com:2222/org/app.git",
            "https://gitlab.com/org/app.git",
        ):
            assert routes._registry_git_url(url) == url, url

    def test_bare_name_without_entry_returns_none(self, monkeypatch):
        from kiro_crew.apps import routes

        monkeypatch.setattr(routes, "get_registry_app_by_repo", lambda repo: None)
        # A bare (non-URL) token with no registry entry has no resolvable URL.
        assert routes._registry_git_url("SomeBundledRepoName") is None

    def test_entry_git_url_field_takes_precedence(self, monkeypatch):
        from kiro_crew.apps import routes

        monkeypatch.setattr(
            routes,
            "get_registry_app_by_repo",
            lambda repo: {"repo": repo, "gitUrl": "https://github.com/acme/canonical"},
        )
        # Explicit gitUrl wins over treating the (URL-form) repo as the clone URL.
        assert (
            routes._registry_git_url("https://github.com/acme/apps")
            == "https://github.com/acme/canonical"
        )


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

        # A malicious external index resolved this repo to a loopback URL.
        monkeypatch.setattr(routes, "_registry_git_url", lambda repo: "https://127.0.0.1:9/x")
        # Guard: if the gate failed, this would raise instead of returning False.

        def _boom(*a, **k):
            raise AssertionError("git clone must not be spawned for an untrusted host")

        monkeypatch.setattr(routes.asyncio, "create_subprocess_exec", _boom)

        ok = await routes._fetch_git_blob(
            "https://127.0.0.1:9/x", "main", "icon.png", tmp_path / "out.png"
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
        assert err["error"] == "stale_clone_not_removed"
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
        assert err["error"] == "stale_clone_not_removed"
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

        # Must return the unreadable_clone_origin error.
        assert err is not None
        assert err["ok"] is False
        assert err["error"] == "unreadable_clone_origin"
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

        def _stubborn_rmtree(path, ignore_errors=False):
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
        """_clone_build_app_locked returns _pending_stale_cleanup paths on
        success instead of deleting them (deferring to caller)."""
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
            result = await reg._clone_build_app_locked(
                new_url, "testapp", [], branch="main", index_originated=False
            )

        # Build succeeded.
        assert result["ok"]
        # Stale paths surfaced for caller cleanup — NOT deleted.
        stale_paths = result.get("_pending_stale_cleanup", [])
        assert len(stale_paths) == 1
        assert stale_paths[0].exists(), (
            "Expected .stale-* dir to still exist after _clone_build_app_locked "
            "success (deferred to caller)"
        )
        assert ".stale-" in stale_paths[0].name
        # The old checkout content is inside the stale dir (user can recover).
        assert (stale_paths[0] / "local-edits.txt").exists()
        assert (stale_paths[0] / "local-edits.txt").read_text() == "precious data"

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
