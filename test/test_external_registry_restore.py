"""Restoring the previous checkout when a registry install fails after its clone.

A build failure, an install-script failure or an undeletable partial clone leaves
the moved-aside checkout recoverable, and the build-failure restore puts back only
the restorable (same-repository) stale checkouts.

``test_external_registry.py`` holds the index cache, fetch and lookup tests and names
every sibling; shared fixtures live in ``external_registry_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from apps_registry_helpers import _install_script_runs_on_this_host  # noqa: F401 - fixture
from external_registry_helpers import _catalog_absent  # noqa: F401
from external_registry_helpers import _explicit_registry_execution_admission  # noqa: F401


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

        def _fake_wrap_argv(argv, mode="standard", **_kwargs):
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

        def _fake_wrap_argv(argv, mode="standard", **_kwargs):
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

        def _fake_wrap_argv(argv, mode="standard", **_kwargs):
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

        def _fake_wrap_argv(argv, mode="standard", **_kwargs):
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

        def _fake_wrap_argv(argv, mode="standard", **_kwargs):
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
            notice = ""

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
    async def test_self_managed_install_propagates_reconsent_notice(self, tmp_path):
        from kiro_crew.apps.registry import install_from_registry

        pkg_dir = tmp_path / "testapp"
        pkg_dir.mkdir()
        (pkg_dir / "app.json").write_text(
            json.dumps(
                {
                    "name": "testapp",
                    "version": "1.0.0",
                    "resources": "app",
                }
            ),
            encoding="utf-8",
        )

        async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
            return {"ok": True, "pkg_dir": pkg_dir}

        registration = SimpleNamespace(
            ok=True,
            notice="session_approval_reconsent",
        )
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
            patch(
                "kiro_crew.apps.manager.register_external_app",
                return_value=registration,
            ),
            patch("kiro_crew.apps.registry.set_app_provenance"),
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch(
                "kiro_crew.apps.registry._sweep_stale_checkouts",
                new=AsyncMock(),
            ),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await install_from_registry("testapp")

        assert result["ok"] is True
        assert result["notice"] == "session_approval_reconsent"

    @pytest.mark.asyncio
    async def test_self_managed_install_republishes_config_keys_off_the_loop(self, tmp_path):
        """The registration writes a manifest that can declare ``config_keys``, and the
        registry runs it on the loop. The snapshot refresh that follows scans every
        installed app, so it must run in a worker, once, after the registration."""
        import threading

        from kiro_crew.apps.registry import install_from_registry

        pkg_dir = tmp_path / "testapp"
        pkg_dir.mkdir()
        (pkg_dir / "app.json").write_text(
            json.dumps({"name": "testapp", "version": "1.0.0", "resources": "app"}),
            encoding="utf-8",
        )

        async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
            return {"ok": True, "pkg_dir": pkg_dir}

        order: list[str] = []
        refresh_threads: list[int] = []

        def _register(**_kwargs):
            order.append("register")
            return SimpleNamespace(ok=True, notice="")

        def _refresh():
            order.append("refresh")
            refresh_threads.append(threading.get_ident())
            return frozenset()

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
            patch("kiro_crew.apps.manager.register_external_app", side_effect=_register),
            patch("kiro_crew.apps.manager.refresh_app_config_keys", side_effect=_refresh),
            patch("kiro_crew.apps.registry.set_app_provenance"),
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry._sweep_stale_checkouts", new=AsyncMock()),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await install_from_registry("testapp")

        assert result["ok"] is True
        assert order == ["register", "refresh"]
        assert refresh_threads[0] != threading.get_ident()

    @pytest.mark.asyncio
    async def test_self_managed_install_fails_when_registration_is_refused(self, tmp_path):
        """A refused pre-registration fails the install with its error and puts
        the previous checkout back, instead of reporting success."""
        from kiro_crew.apps.manifest import CRON_PERMISSION_REQUIRED_ERROR
        from kiro_crew.apps.registry import install_from_registry

        pkg_dir = tmp_path / "testapp"
        pkg_dir.mkdir()
        (pkg_dir / "app.json").write_text(
            json.dumps({"name": "testapp", "version": "1.1.0", "resources": "app"}),
            encoding="utf-8",
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

        refusal = SimpleNamespace(
            ok=False, error=f"invalid manifest: {CRON_PERMISSION_REQUIRED_ERROR}", notice=""
        )
        provenance = MagicMock()
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
            patch("kiro_crew.apps.manager.register_external_app", return_value=refusal),
            patch("kiro_crew.apps.registry.set_app_provenance", new=provenance),
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry._sweep_stale_checkouts", new=AsyncMock()),
            patch("kiro_crew.apps.registry.app_source_dir", return_value=pkg_dir),
            patch("kiro_crew.apps.registry.sel"),
        ):
            result = await install_from_registry("testapp")

        assert result["ok"] is False
        assert CRON_PERMISSION_REQUIRED_ERROR in result["error"]
        provenance.assert_not_called()
        assert (pkg_dir / "my-work.txt").read_text(encoding="utf-8") == "important"
        assert not (pkg_dir / "replacement.txt").exists()
        assert not stale_dir.exists()

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

        def _fake_wrap_argv(argv, mode="standard", **_kwargs):
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
            result = await _clone_build_app("https://example.com/app.git", "testapp", log_lines)

        assert not result["ok"]
        # The refused-origin checkout was NOT re-seated into the active slot.
        assert stale_dir.exists(), "the origin-mismatched aside must stay .stale-*"
        assert (stale_dir / "someone-elses-repo.txt").read_text(
            encoding="utf-8"
        ) == "refused origin", "the refused checkout must be untouched on disk"
        # It must NOT occupy the active source slot the gate refused to serve.
        if app_source.exists():
            assert not (
                app_source / "someone-elses-repo.txt"
            ).exists(), "the refused-origin checkout must never occupy the active slot"
        # It is reported retained (both in the returned log and the stamp),
        # never silently swept.
        joined = "\n".join(log_lines) + "\n" + result.get("log", "")
        assert "retained at" in joined, "the non-restorable aside must be named retained"
        assert stale_dir in (
            result.get("_pending_stale_cleanup") or []
        ), "the non-restorable aside must remain in pending_cleanup for the reporter"

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
            result = await _clone_build_app("https://example.com/app.git", "testapp", log_lines)

        assert not result["ok"]
        # The restorable aside was put back into the active slot.
        assert not stale_dir.exists(), "the restorable aside is renamed back into pkg_dir"
        assert app_source.exists()
        assert (app_source / "my-work.txt").read_text(encoding="utf-8") == "important local edits"
        joined = "\n".join(log_lines) + "\n" + result.get("log", "")
        assert (
            "previous checkout restored" in joined
        ), "a restored aside keeps the existing restore log line"
        # A restored aside is dropped from pending_cleanup (not retained).
        assert stale_dir not in (result.get("_pending_stale_cleanup") or [])

    @pytest.mark.asyncio
    async def test_restorable_restore_rename_oserror_reports_hint_and_keeps_pending(self, tmp_path):
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
            result = await _clone_build_app("https://example.com/app.git", "testapp", log_lines)

        assert not result["ok"]
        joined = "\n".join(log_lines) + "\n" + result.get("log", "")
        assert (
            "could not restore previous checkout" in joined
        ), "the OSError restore path must surface the recovery hint"
        # A rename that FAILED stays in pending_cleanup so it is still reported.
        assert stale_dir in (result.get("_pending_stale_cleanup") or [])
