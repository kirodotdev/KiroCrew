"""Every registry install exit reports a retained stale checkout.

A failure after a successful clone and build, a refusal, an exception and a
provenance failure after a durable success each name the retained ``.stale-*``
path; ``install_from_registry`` reports from its ``finally`` alone, and
``_clone_build_app``'s exception path reports before it re-raises; and a refusal's
outcome carries none of the internal move-aside bookkeeping keys.

``test_external_registry.py`` holds the index cache, fetch and lookup tests and names
every sibling; shared fixtures live in ``external_registry_helpers.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from unittest.mock import AsyncMock, patch

import pytest
from external_registry_helpers import _catalog_absent  # noqa: F401
from external_registry_helpers import _explicit_registry_execution_admission  # noqa: F401


class TestInstallFailureReportsStaleCheckout:
    """Every non-ok exit AFTER a successful
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

        def _fake_wrap_argv(argv, mode="standard", **_kwargs):
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
    """A refusal that leaves a non-restorable
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
    """``_clone_build_app`` must stamp
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
    """When ``_clone_build_app``
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
                await reg._clone_build_app("https://new.example.com/app.git", "testapp", log_lines)

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
                await reg._clone_build_app("https://new.example.com/app.git", "testapp", log_lines)

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
                await reg._clone_build_app("https://new.example.com/app.git", "testapp", log_lines)

        # Restored back into the slot, so the .stale-* sibling is gone...
        assert not stale_dir.exists(), "a restorable stale is renamed back into pkg_dir"
        assert (pkg_dir / "my-work.txt").read_text(encoding="utf-8") == "important"
        # ...and it must NOT be reported as still retained at a now-deleted path.
        assert not any(
            line.startswith("Previous checkout retained at:") for line in log_lines
        ), "a restored checkout must not be named as retained"


class TestProvenanceRaiseAfterDurableSuccessReportsRetainedStale:
    """A durable-success install whose provenance write
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
    """Grep-pin: retained-stale reporting is
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
        assert (
            "filter_restorable=not durable_success" in source
        ), "the sole reporter call must use filter_restorable=not durable_success"


class TestRefusalOutcomeCarriesNoInternalTransactionKeys:
    """A build-refusal exit
    does ``outcome = {**build_result}``, so the internal move-aside bookkeeping
    keys ``_pending_stale_cleanup`` / ``_restorable_stale`` (each ``list[Path]``)
    would ride out of ``install_from_registry`` on the returned dict. ``Path``
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
