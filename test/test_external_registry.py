"""Tests for kiro_crew.apps.registry — External (federated) registry support."""

from __future__ import annotations

import json
import os
import time
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
            git_url, name, log_lines, branch="main", *, index_originated=False, **_extra
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
            git_url, name, log_lines, branch="main", *, index_originated=False, **_extra
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
