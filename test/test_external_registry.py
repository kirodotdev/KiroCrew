"""Tests for kiro_crew.apps.registry — External (federated) registry support.

This module covers the external index: its cache, fetch and parsing, lookups against it,
the clone sandbox mode, install admission, ``refresh_registries`` and the clone-host trust
gates. Each ``test_external_registry_<topic>.py`` sibling holds one install-path topic:
``_credentials`` (clone credential posture), ``_branch`` (branch handling and bounded
``.git/HEAD`` reads), ``_refusal_gates`` (the index ``subdirectory`` gate and what a
refusal's cleanup touches), ``_origin`` (origin verification of a persisted clone),
``_restore``, ``_move_aside``, ``_stale_reporting`` (the previous checkout across a failed
or successful install) and ``_clone_diagnostics``. Shared fixtures live in
``external_registry_helpers.py``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from external_registry_helpers import _catalog_absent  # noqa: F401
from external_registry_helpers import _explicit_registry_execution_admission  # noqa: F401
from external_registry_helpers import cache_dir_fixture  # noqa: F401

from kiro_crew.apps.registry import (
    _EXTERNAL_REGISTRY_CACHE_TTL,
    _clone_sandbox_mode,
    _external_registry_cache_identity,
    _external_registry_cache_path,
    _external_registry_repos,
    _fetch_external_registry_index,
    _git_url_host,
    _is_ssh_git_url,
    _load_external_registries,
    _manifest_cache_path,
    _owner_designated_repo_target,
    _read_external_registry_cache,
    _read_manifest_negative_cache,
    _safe_cache_stem,
    _write_external_registry_cache,
    _write_manifest_negative_cache,
    get_registry_app,
    get_registry_app_by_repo,
    is_clone_host_trusted,
    known_registry_repos,
    refresh_registries,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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

    def test_write_and_read_strip_http_userinfo_recursively(self, cache_dir):
        secret = "RegistryCacheSecret"
        raw = f"https://user:{secret}@git.example.com/org/apps.git"
        query = f"https://git.example.com/org/apps.git?token={secret}#ignored"
        generic = f"s3://user:{secret}@bucket.example/apps?token={secret}#ignored"
        entries = [
            {
                "name": "private-app",
                "repo": raw,
                "gitUrl": raw,
                "_registry": raw,
                "nested": {
                    "sourceUrl": raw,
                    "queryUrl": query,
                    "genericUrl": generic,
                },
            }
        ]

        _write_external_registry_cache(raw, entries)
        path = _external_registry_cache_path(raw)
        result = _read_external_registry_cache(raw)

        assert secret not in path.name
        assert secret not in path.read_text(encoding="utf-8")
        assert secret not in repr(result)
        assert result is not None
        assert result[0]["repo"] == "https://git.example.com/org/apps.git"
        assert result[0]["nested"]["queryUrl"] == "https://git.example.com/org/apps.git"
        assert result[0]["nested"]["genericUrl"] == "s3://bucket.example/apps"

    def test_named_legacy_cache_is_sanitized_and_rewritten_on_read(self, cache_dir):
        secret = "NamedLegacyCacheSecret"
        raw = f"https://user:{secret}@git.example.com/org/apps.git"
        path = _external_registry_cache_path("corp")
        path.write_text(
            json.dumps([{"name": "private-app", "repo": raw, "_registry": raw}]),
            encoding="utf-8",
        )

        result = _read_external_registry_cache("corp")

        assert secret not in repr(result)
        assert secret not in path.read_text(encoding="utf-8")

    def test_stale_named_legacy_cache_is_cleaned_without_becoming_fresh(self, cache_dir):
        secret = "StaleNamedLegacySecret"
        raw = f"https://user:{secret}@git.example.com/org/apps.git?token={secret}"
        path = _external_registry_cache_path("corp")
        path.write_text(
            json.dumps([{"name": "private-app", "repo": raw, "_registry": raw}]),
            encoding="utf-8",
        )
        old_time = time.time() - 7200
        os.utime(path, (old_time, old_time))

        assert _read_external_registry_cache("corp") is None

        assert secret not in path.read_text(encoding="utf-8")
        assert path.stat().st_mtime < time.time() - _EXTERNAL_REGISTRY_CACHE_TTL
        stale = _read_external_registry_cache("corp", ignore_ttl=True)
        assert stale is not None
        assert stale[0]["repo"] == "https://git.example.com/org/apps.git"

    def test_unnamed_raw_legacy_cache_filename_is_removed_without_logging_secret(
        self, cache_dir, caplog
    ):
        from kiro_crew.apps import registry as reg

        secret = "LegacyFilenameSecret"
        raw = f"https://user:{secret}@git.example.com/org/apps.git"
        legacy = reg._legacy_external_registry_cache_path(raw)
        legacy.write_text("[]", encoding="utf-8")
        assert secret in legacy.name

        with caplog.at_level(logging.WARNING):
            assert _read_external_registry_cache(raw, ignore_ttl=True) is None

        assert not legacy.exists()
        assert secret not in caplog.text

    def test_invalid_cached_values_are_not_echoed_to_logs(self, cache_dir, caplog):
        secret = "InvalidCacheValueSecret"
        path = _external_registry_cache_path("corp")
        path.write_text(
            json.dumps(
                [
                    {"name": secret, "repo": "https://example.invalid/repo.git"},
                    {
                        "name": "valid-app",
                        "repo": "https://example.invalid/repo.git",
                        "subdirectory": f"../{secret}",
                    },
                ]
            ),
            encoding="utf-8",
        )

        with caplog.at_level(logging.WARNING, logger="kiro_crew.apps.registry"):
            assert _read_external_registry_cache("corp", ignore_ttl=True) == []

        assert secret not in caplog.text

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
    async def test_credentialed_fresh_index_returns_no_http_userinfo(self, monkeypatch, tmp_path):
        secret = "FreshRegistrySecret"
        raw_registry = f"https://user:{secret}@git.example.com/org/apps.git"

        async def _fake_fetch(git_url, branch, dest, log_lines, **kwargs):
            dest.mkdir(parents=True)
            (dest / "app-registry.json").write_text(
                json.dumps(
                    [
                        {
                            "name": "private-app",
                            "repo": raw_registry,
                            "nested": {"sourceUrl": raw_registry},
                        }
                    ]
                ),
                encoding="utf-8",
            )
            return None

        monkeypatch.setattr("kiro_crew.apps.registry._git_fetch_branch", _fake_fetch)
        result = await _fetch_external_registry_index(raw_registry, "main")

        assert result is not None
        assert secret not in repr(result)
        assert result[0]["repo"] == "https://git.example.com/org/apps.git"

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


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", [False, True])
async def test_unnamed_credentialed_registry_cache_hit_and_stale_rows_are_public(
    cache_dir, monkeypatch, stale
):
    from kiro_crew.apps import registry as reg

    secret = "CachedRegistrySecret"
    raw_registry = f"https://user:{secret}@git.example.com/org/apps.git"
    public_registry = "https://git.example.com/org/apps.git"
    mock_reg = SimpleNamespace(name="", repo=raw_registry, branch="main", trust="index")
    cache_key = _external_registry_cache_identity(mock_reg)
    _write_external_registry_cache(
        cache_key,
        [{"name": "private-app", "repo": public_registry, "branch": "main"}],
    )
    if stale:
        old = time.time() - 7200
        os.utime(_external_registry_cache_path(cache_key), (old, old))
        monkeypatch.setattr(
            reg,
            "_fetch_and_cache_external_registry",
            AsyncMock(return_value=None),
        )
    monkeypatch.setattr(reg, "_effective_registries", lambda: [mock_reg])

    rows = await _load_external_registries()

    assert rows and rows[0]["_registry"] == public_registry
    assert secret not in repr(rows)


@pytest.mark.asyncio
async def test_url_shaped_registry_name_is_credential_free_in_cached_rows(cache_dir, monkeypatch):
    from kiro_crew.apps import registry as reg

    secret = "RegistryNameSecret"
    raw_name = f"https://user:{secret}@registry.example/apps?token={secret}"
    public_name = "https://registry.example/apps"
    mock_reg = SimpleNamespace(
        name=raw_name,
        repo="https://git.example.com/org/apps.git",
        branch="main",
        trust="index",
    )
    _write_external_registry_cache(
        _external_registry_cache_identity(mock_reg),
        [{"name": "private-app", "repo": mock_reg.repo, "branch": "main"}],
    )
    monkeypatch.setattr(reg, "_effective_registries", lambda: [mock_reg])

    rows = await _load_external_registries()

    assert rows and rows[0]["_registry"] == public_name
    assert secret not in repr(rows)


def test_url_shaped_registry_name_still_rehydrates_exact_owner_transport(monkeypatch):
    from kiro_crew.apps import registry as reg

    raw_name = "https://name-user:name-secret@registry.example/apps"
    raw_repo = "https://repo-user:repo-secret@git.example.com/org/apps.git"
    configured = SimpleNamespace(
        name=raw_name,
        repo=raw_repo,
        branch="main",
        trust="owner",
    )
    monkeypatch.setattr(reg, "_effective_registries", lambda: [configured])
    entry = {
        "name": "private-app",
        "gitUrl": "https://git.example.com/org/apps.git",
        "_registry": "https://registry.example/apps",
    }

    assert _owner_designated_repo_target(entry) == raw_repo


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

        mock_reg = MagicMock()
        mock_reg.name = "myorg"
        mock_reg.repo = "MyOrgRepo"
        mock_reg.branch = "mainline"
        _write_external_registry_cache(_external_registry_cache_identity(mock_reg), entries)

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

        mock_reg = MagicMock()
        mock_reg.name = "identity"
        mock_reg.repo = "IdentityApps"
        mock_reg.branch = "mainline"
        _write_external_registry_cache(_external_registry_cache_identity(mock_reg), entries)

        mock_config = MagicMock()
        mock_config.registries = [mock_reg]
        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            lambda: mock_config,
        )

        result = await _load_external_registries()
        assert len(result) == 2
        assert all(e["_registry"] == "identity" for e in result)


# ---------------------------------------------------------------------------
# get_registry_app — external cache lookup
# ---------------------------------------------------------------------------


class TestGetRegistryAppExternal:
    def test_finds_app_in_external_cache(self, cache_dir, monkeypatch):
        entries = [
            {"name": "ext-app", "repo": "ExtRepo", "branch": "mainline"},
        ]

        # Mock config to have one registry
        mock_reg = MagicMock()
        mock_reg.name = "myorg"
        mock_reg.repo = "MyOrgRepo"
        mock_reg.branch = "mainline"
        _write_external_registry_cache(_external_registry_cache_identity(mock_reg), entries)

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

        mock_reg = MagicMock()
        mock_reg.name = "myorg"
        mock_reg.repo = "MyOrgRepo"
        mock_reg.branch = "release"
        _write_external_registry_cache(_external_registry_cache_identity(mock_reg), entries)
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
        mock_reg = MagicMock()
        mock_reg.name = "PCN"
        mock_reg.repo = "PCNAppRegistry"
        mock_reg.branch = "mainline"
        _write_external_registry_cache(
            _external_registry_cache_identity(mock_reg),
            [{"name": "pcn-radar", "repo": "PCNRadar", "branch": "mainline"}],
        )
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
        mock_reg = MagicMock()
        mock_reg.name = "PCN"
        mock_reg.repo = "PCNAppRegistry"
        mock_reg.branch = "mainline"
        cache_key = _external_registry_cache_identity(mock_reg)
        _write_external_registry_cache(
            cache_key, [{"name": "pcn-radar", "repo": "PCNRadar", "branch": "mainline"}]
        )
        stale = time.time() - 7200
        os.utime(_external_registry_cache_path(cache_key), (stale, stale))
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
        mock_reg = MagicMock()
        mock_reg.name = "PCN"
        mock_reg.repo = "PCNAppRegistry"
        mock_reg.branch = "mainline"
        _write_external_registry_cache(
            _external_registry_cache_identity(mock_reg),
            [{"name": "pcn-radar", "repo": "PCNRadar", "branch": "mainline"}],
        )
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
# gate (fetched read-only BEFORE clone), so require_signature does not deny
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

    def test_credential_rotation_does_not_change_public_cache_identity(self, cache_dir):
        first = _external_registry_cache_path("https://user:first-secret@git.example.com/acme/apps")
        second = _external_registry_cache_path(
            "https://user:second-secret@git.example.com/acme/apps"
        )
        assert first == second
        assert "secret" not in first.name


# ---------------------------------------------------------------------------
# refresh_registries — cache busting + contract shape
# ---------------------------------------------------------------------------


class TestRefreshRegistries:
    @pytest.mark.asyncio
    async def test_success_swaps_cache_and_expires_manifests(self, cache_dir, monkeypatch):
        # Seed a stale index cache for registry "acme" listing one app, plus
        # that app's manifest cache.
        mock_reg = MagicMock()
        mock_reg.name = "acme"
        mock_reg.repo = "https://github.com/acme/apps"
        mock_reg.branch = "main"
        cache_key = _external_registry_cache_identity(mock_reg)
        _write_external_registry_cache(
            cache_key, [{"name": "cool-app", "repo": "R", "branch": "main"}]
        )
        manifest_path = _manifest_cache_path({"name": "cool-app", "repo": "R", "branch": "main"})
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text('{"name": "cool-app"}', encoding="utf-8")
        index_path = _external_registry_cache_path(cache_key)

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
    async def test_success_expires_failed_fetch_markers(self, cache_dir, monkeypatch):
        # A refresh must retry a row whose last listing fetch failed, for the
        # prior index's coordinates and the fresh index's alike.
        mock_reg = MagicMock()
        mock_reg.name = "acme"
        mock_reg.repo = "https://github.com/acme/apps"
        mock_reg.branch = "main"
        git_url = "https://github.com/acme/cool-app"
        prior_row = {"name": "cool-app", "repo": "R", "branch": "main", "gitUrl": git_url}
        fresh_row = {"name": "cool-app", "repo": "R", "branch": "dev", "gitUrl": git_url}
        _write_external_registry_cache(_external_registry_cache_identity(mock_reg), [prior_row])
        _write_manifest_negative_cache(prior_row)
        _write_manifest_negative_cache(fresh_row)
        assert _read_manifest_negative_cache(prior_row) is True
        assert _read_manifest_negative_cache(fresh_row) is True

        mock_config = MagicMock()
        mock_config.registries = [mock_reg]
        monkeypatch.setattr("kiro_crew.config.loader.KiroCrewConfig.load", lambda: mock_config)
        monkeypatch.setattr(
            "kiro_crew.apps.registry._fetch_external_registry_index",
            AsyncMock(return_value=[fresh_row]),
        )
        monkeypatch.setattr(
            "kiro_crew.apps.registry.list_registry",
            AsyncMock(return_value=[{"name": "cool-app"}]),
        )

        result = await refresh_registries()

        assert result["refreshed"] == ["acme"]
        assert _read_manifest_negative_cache(prior_row) is False
        assert _read_manifest_negative_cache(fresh_row) is False

    @pytest.mark.asyncio
    async def test_fetch_failure_preserves_stale_and_reports_failed(self, cache_dir, monkeypatch):
        # Seed a stale index cache; the refetch will fail.
        mock_reg = MagicMock()
        mock_reg.name = "acme"
        mock_reg.repo = "https://github.com/acme/apps"
        mock_reg.branch = "main"
        cache_key = _external_registry_cache_identity(mock_reg)
        _write_external_registry_cache(
            cache_key, [{"name": "cool-app", "repo": "R", "branch": "main"}]
        )
        index_path = _external_registry_cache_path(cache_key)

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
        assert _read_external_registry_cache(cache_key, ignore_ttl=True) == [
            {"name": "cool-app", "repo": "R", "branch": "main"}
        ]
        assert result["ok"] is False
        assert result["refreshed"] == []
        assert result["failed"] == ["acme"]
        assert result["results"] == [{"name": "acme", "ok": False}]

    @pytest.mark.asyncio
    async def test_refused_ssh_sign_in_is_reported_as_auth(self, cache_dir, monkeypatch):
        """A clone git refuses for credentials names that cause in the refresh result.

        Without it the dashboard can only say "could not refresh", and the user
        has no way to tell expired credentials (renew them) from an outage.
        """
        mock_reg = MagicMock()
        mock_reg.name = "acme"
        mock_reg.repo = "ssh://git.example.com/acme/apps"
        mock_reg.branch = "main"
        mock_config = MagicMock()
        mock_config.registries = [mock_reg]
        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            lambda: mock_config,
        )
        monkeypatch.setattr(
            "kiro_crew.apps.registry.list_registry",
            AsyncMock(return_value=[]),
        )
        from kiro_crew.apps.registry_pipeline import indexes

        async def _unwrapped(argv, **_kwargs):
            return list(argv), None

        # The OS sandbox is not available on every CI runner.
        monkeypatch.setattr(indexes, "wrap_argv_async", _unwrapped)
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(
                return_value=(
                    b"",
                    b"git@git.example.com: Permission denied (publickey).\r\n"
                    b"fatal: Could not read from remote repository.\n",
                )
            )
            mock_proc.returncode = 128
            mock_exec.return_value = mock_proc
            result = await refresh_registries()

        assert mock_exec.called
        assert result["failed"] == ["acme"]
        assert result["results"] == [
            {"name": "acme", "ok": False, "reason": "auth", "host": "git.example.com"}
        ]

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
        cached = _read_external_registry_cache(
            _external_registry_cache_identity(mock_reg), ignore_ttl=True
        )
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
        cached = _read_external_registry_cache(
            _external_registry_cache_identity(mock_reg), ignore_ttl=True
        )
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
            resolved = _manifest_cache_path({"name": hostile}).resolve()
            assert cache_root in resolved.parents, f"{hostile!r} escaped to {resolved}"

    def test_manifest_cache_identity_is_scoped_to_source_coordinates(self, cache_dir):
        # main vs dev of the same repo, and same-name apps from two different
        # repos, must each get a DISTINCT cache identity: a name-only key
        # would let a dev listing reuse main's cached metadata.
        base = {"name": "cool-app", "repo": "https://github.com/acme/apps", "branch": "main"}
        dev = dict(base, branch="dev")
        other_repo = dict(base, repo="https://github.com/rival/apps")
        paths = {
            _manifest_cache_path(base),
            _manifest_cache_path(dev),
            _manifest_cache_path(other_repo),
        }
        assert len(paths) == 3

    def test_safe_cache_stem_preserves_plain_names(self):
        # Plain names stay byte-identical (no hash suffix) so caches persist.
        assert _safe_cache_stem("cool-app") == "cool-app"
        assert _safe_cache_stem("my_app.v2") == "my_app.v2"
        # Traversal / separator names are slugified AND hashed for uniqueness.
        assert _safe_cache_stem("../../config") != "../../config"
        assert "/" not in _safe_cache_stem("a/b")
        assert ".." not in _safe_cache_stem("../x")


# ---------------------------------------------------------------------------
# Clone-URL resolution for the blob proxy is not a standalone resolver.
# ``handle_blob_proxy`` resolves the clone URL once (from the decided entry via
# ``_entry_git_url`` for a bundled entry, or by an inline in-memory URL-form
# check on the already-validated ``repo`` for the no-entry external/federated
# branch) and threads it into ``_fetch_git_blob``; there is no
# ``routes._registry_git_url`` helper to unit-test in isolation.  The URL-form /
# no-bundled-entry resolution boundary is exercised through the handler in
# test_apps_routes_coverage_blob_proxy.py, not in this section.
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


class TestRecordedCloneFailure:
    """What a failed registry clone records and logs."""

    @staticmethod
    async def _fetch_with_stderr(monkeypatch, stderr: bytes):
        from kiro_crew.apps.registry_pipeline import indexes

        async def _unwrapped(argv, **_kwargs):
            return list(argv), None

        # The OS sandbox is not available on every CI runner; the clone's argv is
        # not what these tests are about.
        monkeypatch.setattr(indexes, "wrap_argv_async", _unwrapped)
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", stderr))
            mock_proc.returncode = 128
            mock_exec.return_value = mock_proc
            result = await indexes._fetch_external_registry_index(
                "ssh://git.example.com/acme/apps", "main"
            )
        assert mock_exec.called
        return result

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("stderr", "refused"),
        [
            (b"git@git.example.com: Permission denied (publickey).\n", True),
            (b"fatal: Authentication failed for 'https://git.example.com/r'\n", True),
            (b"ssh: Could not resolve hostname git.example.com: nodename\n", False),
            (b"fatal: could not create work tree dir 'x': Permission denied\n", False),
        ],
    )
    async def test_only_a_refused_sign_in_is_recorded(self, monkeypatch, stderr, refused):
        from kiro_crew.apps.registry_pipeline import indexes

        assert await self._fetch_with_stderr(monkeypatch, stderr) is None
        assert (
            indexes._registry_sign_in_refused("ssh://git.example.com/acme/apps", "main") is refused
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("stderr", "label", "refused"),
        [
            (
                "git@git.example.com: Permission denied (publickey).",
                "git refused the sign-in",
                True,
            ),
            (
                "ssh: Could not resolve hostname git.example.com: x",
                "host could not be resolved",
                False,
            ),
            ("fatal: something new from git.example.com", "unclassified git failure", False),
        ],
    )
    async def test_a_credentialed_clone_logs_a_fixed_label_only(
        self, monkeypatch, caplog, stderr, label, refused
    ):
        """A credentialed remote's git output never reaches the log; a fixed label does."""
        from kiro_crew.apps.registry_pipeline import indexes
        from kiro_crew.apps.registry_pipeline.git_targets import _loggable_git_transport_output

        async def _failing_fetch(url, branch, dest, log_lines, **_kwargs):
            # What the real credentialed fetch hands back: git's output already
            # replaced by a fixed label.
            log_lines.append(_loggable_git_transport_output(stderr, credentialed=True))
            return "fetch failed"

        monkeypatch.setattr(indexes, "_git_fetch_branch", _failing_fetch)
        repo = "https://user:s3cret@git.example.com/acme/apps.git"
        with caplog.at_level("WARNING"):
            assert await indexes._fetch_external_registry_index(repo, "main") is None
        warning = [r.getMessage() for r in caplog.records if "clone failed" in r.getMessage()]
        assert warning == [f"external registry clone failed: {label}"]
        assert indexes._registry_sign_in_refused(repo, "main") is refused

    @pytest.mark.asyncio
    async def test_a_plain_clone_failure_logs_one_line(self, monkeypatch, caplog):
        """The plain clone keeps its single redacted-tail line; recording adds none."""
        with caplog.at_level("WARNING"):
            await self._fetch_with_stderr(
                monkeypatch, b"git@git.example.com: Permission denied (publickey).\n"
            )
        warning = [r.getMessage() for r in caplog.records if "clone failed" in r.getMessage()]
        assert len(warning) == 1

    @pytest.mark.asyncio
    async def test_a_later_fetch_clears_the_refused_sign_in(self, monkeypatch):
        """The record describes the latest fetch only, not any earlier one."""
        from kiro_crew.apps.registry_pipeline import indexes

        repo = "ssh://git.example.com/acme/apps"
        await self._fetch_with_stderr(
            monkeypatch, b"git@git.example.com: Permission denied (publickey).\n"
        )
        assert indexes._registry_sign_in_refused(repo, "main") is True
        await self._fetch_with_stderr(
            monkeypatch, b"ssh: Could not resolve hostname git.example.com: x\n"
        )
        assert indexes._registry_sign_in_refused(repo, "main") is False
