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
    _read_external_registry_cache,
    _write_external_registry_cache,
    get_registry_app,
    known_registry_repos,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
            "name": name, "version": "1.0.0", "displayName": name,
            "description": "d", "author": "tester", "signer": signer,
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
        self._write_policy(reg_home, {
            "mode": "enforce", "require_signature": True,
            "approved": ["signed-reg"], "trust_keys": {"acme": secret},
        })
        manifest = self._signed_manifest("signed-reg", secret)
        with patch(
            "kiro_crew.apps.registry.get_registry_app",
            return_value={"name": "signed-reg", "repo": "https://example.com/SignedRepo.git",
                          "branch": "mainline"},
        ), patch(
            "kiro_crew.apps.registry._fetch_app_manifest",
            new=AsyncMock(return_value=manifest),
        ), patch(
            "kiro_crew.apps.registry._clone_build_app",
            new=AsyncMock(return_value={"ok": False, "error": "stop-after-admission"}),
        ) as mock_build:
            result = await install_from_registry("signed-reg")
        # Admission passed (signed manifest verified) — flow proceeded to the
        # clone/build step, which we stub to stop right after admission.
        assert "blocked by admission policy" not in (result.get("error") or "")
        mock_build.assert_awaited()

    @pytest.mark.asyncio
    async def test_unsigned_app_denied_under_require_signature(self, reg_home):
        from kiro_crew.apps.registry import install_from_registry

        self._write_policy(reg_home, {
            "mode": "enforce", "require_signature": True,
            "approved": ["unsigned-reg"], "trust_keys": {"acme": "s3cr3t"},
        })
        with patch(
            "kiro_crew.apps.registry.get_registry_app",
            return_value={"name": "unsigned-reg", "repo": "https://example.com/UnsignedRepo.git",
                          "branch": "mainline"},
        ), patch(
            "kiro_crew.apps.registry._fetch_app_manifest",
            new=AsyncMock(return_value={"name": "unsigned-reg", "version": "1.0.0"}),
        ), patch(
            "kiro_crew.apps.registry._clone_build_app",
            new=AsyncMock(return_value={"ok": True, "pkg_dir": reg_home}),
        ) as mock_build:
            result = await install_from_registry("unsigned-reg")
        # Denied at the gate — the app is never cloned/built.
        assert not result["ok"]
        assert "blocked by admission policy" in result["error"]
        mock_build.assert_not_awaited()
