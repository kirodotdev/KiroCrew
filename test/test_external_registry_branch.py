"""Branch handling on the registry install path.

An operator-configured registry branch overrides a per-app declaration, the manifest
fast path serves a persisted clone only when both its origin and its branch match, a
detached or unreadable HEAD is never read as a branch mismatch, and the ``.git/HEAD``
reads behind those checks are bounded (a commit-pinned install skips the
reconvergence read).

``test_external_registry.py`` holds the index cache, fetch and lookup tests and names
every sibling; shared fixtures live in ``external_registry_helpers.py``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from external_registry_helpers import _catalog_absent  # noqa: F401
from external_registry_helpers import _explicit_registry_execution_admission  # noqa: F401
from external_registry_helpers import cache_dir_fixture  # noqa: F401

from kiro_crew.apps.registry import get_registry_app

# ---------------------------------------------------------------------------
# Operator-configured registry branch overrides per-app declarations
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
        cached = reg._read_external_registry_cache(
            reg._external_registry_cache_identity(_Reg()), ignore_ttl=True
        )
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

        configured_reg = SimpleNamespace(
            name="acme", repo="https://github.com/acme/apps", branch="develop"
        )
        reg._write_external_registry_cache(
            reg._external_registry_cache_identity(configured_reg),
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
        mock_config.registries = [configured_reg]
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
                side_effect=lambda a, mode="standard", **_k: (a, None),
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

        def _fake_wrap_argv(argv, mode="standard", **_kwargs):
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

        def _fake_wrap_argv(argv, mode="standard", **_kwargs):
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

        def _fake_wrap_argv(argv, mode="standard", **_kwargs):
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
