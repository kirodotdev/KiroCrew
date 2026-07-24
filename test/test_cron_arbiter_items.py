"""Regression tests for the three Arbiter BLOCK items on PR #331 (fix/cron-locking).

Item 1 — the app-facing ``CronSDK`` mutation API stays **sync-callable** with
         its published contract; a prior revision flipped
         ``add_job``/``remove_job``/``update_job``/``remove_all`` to ``async def``
         with no shim, silently turning ``ctx.cron.add_job(...)`` into an
         un-awaited coroutine (data-losing break). Separately-named ``*_async``
         siblings serve loop-native callers.

Item 2 — ``CronSDK.remove_all`` removes every owned job in ONE atomic
         ``CronService`` batch transaction and PROPAGATES ``CronStoreBusy``: a
         contended store removes them all or none (never a partial removal that
         orphans still-ENABLED jobs) and the failure is surfaced, not masked as
         a successful ``0``. The owned set is SELECTED inside the same locked
         transaction (against the reloaded on-disk state), not from a cache-only
         id snapshot, so a job the app created in another process since the last
         cache refresh is still removed — no cross-process orphan window.

Item 3 — the store lock's loop-safety invariant is MACHINE-ENFORCED:
         ``_file_lock`` raises ``CronLoopSafetyError`` on a running event loop
         under strict mode, while the sanctioned async / offloaded-sync paths
         do NOT trip it. Runs in CI, so a future writer that reintroduces an
         on-loop sync-mutator call on a sanctioned path is caught.
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from kiro_crew.apps import bridges
from kiro_crew.apps.cron_sdk import CronSDK, CronSyncOnLoopError
from kiro_crew.cron import CronLoopSafetyError, CronService, CronStoreBusy

STRICT = "KIROCREW_STRICT_LOOP_SAFETY"


# ── Item 1: the sync SDK contract is preserved ──


class TestSdkSyncContract:
    def test_public_mutators_are_sync_and_async_siblings_exist(self) -> None:
        """The published mutation API is synchronous; the coroutine variants
        live under separate ``*_async`` names. A regression that re-flips the
        public methods to ``async def`` would silently break existing apps."""
        for name in ("add_job", "remove_job", "update_job", "remove_all"):
            assert not inspect.iscoroutinefunction(getattr(CronSDK, name)), (
                f"{name} must stay synchronous (published SDK contract)"
            )
        for name in (
            "add_job_async", "remove_job_async", "update_job_async", "remove_all_async",
        ):
            assert inspect.iscoroutinefunction(getattr(CronSDK, name)), (
                f"{name} must be the async variant"
            )

    def test_sync_add_job_creates_and_persists_loopless(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        sdk = CronSDK("myapp", svc)
        job = sdk.add_job("j", "hello", every_secs=3600)
        # Actually executed synchronously — not an un-awaited coroutine.
        assert not asyncio.iscoroutine(job)
        assert job.created_by == "app:myapp"
        assert [j.id for j in sdk.list_jobs()] == [job.id]
        # A fresh reader confirms it was persisted to disk.
        fresh = CronService(base_dir=tmp_path)
        assert any(j.id == job.id for j in fresh.list_jobs(include_disabled=True))

    def test_sync_update_and_remove_loopless(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        sdk = CronSDK("myapp", svc)
        job = sdk.add_job("j", "hello", every_secs=3600)
        updated = sdk.update_job(job.id, message="changed")
        assert updated is not None and updated.message == "changed"
        assert sdk.remove_job(job.id) is True
        assert sdk.list_jobs() == []

    def test_sync_ownership_is_enforced(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        other = CronSDK("other", svc).add_job("x", "m", every_secs=3600)
        mine = CronSDK("mine", svc)
        with pytest.raises(PermissionError):
            mine.remove_job(other.id)

    @pytest.mark.asyncio
    async def test_async_variants_create_update_remove(self, tmp_path: Path) -> None:
        svc = await CronService.create(base_dir=tmp_path)
        sdk = CronSDK("myapp", svc)
        job = await sdk.add_job_async("j", "hello", every_secs=3600)
        assert job.created_by == "app:myapp"
        assert await sdk.update_job_async(job.id, message="x") is not None
        assert await sdk.remove_job_async(job.id) is True
        assert sdk.list_jobs() == []


# ── Item 2: atomic remove_all + CronStoreBusy propagation ──


class TestRemoveAllAtomic:
    def test_remove_all_uses_one_atomic_batch_not_per_id_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        sdk = CronSDK("app1", svc)
        for i in range(3):
            sdk.add_job(f"j{i}", "m", every_secs=3600)

        calls = {"owner": 0, "batch": 0, "single": 0}
        orig_owner = svc.remove_jobs_by_owner_sync
        orig_batch = svc.remove_jobs_sync
        orig_single = svc.remove_job

        def spy_owner(owner):  # type: ignore[no-untyped-def]
            calls["owner"] += 1
            return orig_owner(owner)

        def spy_batch(ids):  # type: ignore[no-untyped-def]
            calls["batch"] += 1
            return orig_batch(ids)

        def spy_single(jid):  # type: ignore[no-untyped-def]
            calls["single"] += 1
            return orig_single(jid)

        monkeypatch.setattr(svc, "remove_jobs_by_owner_sync", spy_owner)
        monkeypatch.setattr(svc, "remove_jobs_sync", spy_batch)
        monkeypatch.setattr(svc, "remove_job", spy_single)

        assert sdk.remove_all() == 3
        assert calls["owner"] == 1, (
            "remove_all must use ONE atomic owner-scoped batch transaction"
        )
        assert calls["single"] == 0, "remove_all must NOT loop per-id remove_job"
        assert sdk.list_jobs() == []

    def test_remove_all_removes_job_present_on_disk_but_absent_from_cache(
        self, tmp_path: Path
    ) -> None:
        """Cross-process regression: a job this app created in ANOTHER process
        after our cache was populated must still be removed on uninstall.

        Because this PR made ``list_jobs()`` cache-only (no filesystem read),
        computing the owned-id set from the cache before the lock would MISS
        the externally-added job; the locked removal would then delete only the
        stale ids, report success, and uninstall would proceed to delete the
        app — leaving an ENABLED orphan cron with no owning app. The fix selects
        the owned set INSIDE the locked transaction against the reloaded on-disk
        state, so the externally-added job is seen and removed."""
        svc = CronService(base_dir=tmp_path)
        sdk = CronSDK("app1", svc)
        cached = sdk.add_job("cached", "m", every_secs=3600)

        # Populate the in-memory cache/list (simulating a live gateway that has
        # not re-read the store since).
        assert [j.id for j in sdk.list_jobs()] == [cached.id]

        # A SECOND process (another CronService on the same store dir) adds a
        # job owned by the SAME app. Our svc's cache does not know about it.
        other_proc = CronService(base_dir=tmp_path)
        sneaky = CronSDK("app1", other_proc).add_job(
            "sneaky", "m", every_secs=3600
        )
        # Confirm the setup: our svc's cache still reflects only the first job,
        # yet the store on disk now holds BOTH owned jobs.
        assert sneaky.id not in {j.id for j in sdk.list_jobs()}

        # remove_all must remove BOTH — the in-lock reload+select sees the
        # externally-added job. Reported count reflects both.
        assert sdk.remove_all() == 2

        # A fresh reader confirms NO owned jobs (not even an ENABLED orphan)
        # remain on disk.
        fresh = CronService(base_dir=tmp_path)
        assert [
            j for j in fresh.list_jobs(include_disabled=True)
            if getattr(j, "created_by", "") == "app:app1"
        ] == []

    def test_busy_store_removes_nothing_and_surfaces_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A contended store must leave NO enabled orphans and surface the
        failure — not remove a subset and report success/``0``."""
        svc = CronService(base_dir=tmp_path)
        sdk = CronSDK("app1", svc)
        ids = [sdk.add_job(f"j{i}", "m", every_secs=3600).id for i in range(3)]

        def busy(owner):  # type: ignore[no-untyped-def]
            raise CronStoreBusy("store busy")

        monkeypatch.setattr(svc, "remove_jobs_by_owner_sync", busy)
        with pytest.raises(CronStoreBusy):
            sdk.remove_all()

        # All-or-nothing: every job still present AND still enabled — the single
        # atomic transaction raised before mutating anything, so no subset was
        # orphaned in a half-removed/disabled state.
        remaining = sdk.list_jobs()
        assert sorted(j.id for j in remaining) == sorted(ids)
        assert all(j.enabled for j in remaining)

    @pytest.mark.asyncio
    async def test_deregister_propagates_cronstorebusy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``deregister_app_crons_from_service`` must re-raise ``CronStoreBusy``
        so disable/uninstall reports a failed cleanup — not swallow it and
        return ``0`` while owned jobs stay enabled."""
        svc = await CronService.create(base_dir=tmp_path)
        sdk = CronSDK("app1", svc)
        await sdk.add_job_async("j", "m", every_secs=3600)

        async def busy(owner):  # type: ignore[no-untyped-def]
            raise CronStoreBusy("store busy")

        monkeypatch.setattr(svc, "remove_jobs_by_owner", busy)
        with pytest.raises(CronStoreBusy):
            await bridges.deregister_app_crons_from_service("app1", svc)


# ── Item 3: machine-enforced loop-safety guard ──


class TestLoopSafetyGuard:
    def test_file_lock_ok_loopless_even_under_strict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(STRICT, "1")
        svc = CronService(base_dir=tmp_path)
        # No running loop on this thread → the intended sync path → guard passes.
        with svc._file_lock():
            pass

    @pytest.mark.asyncio
    async def test_file_lock_raises_on_running_loop_under_strict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(STRICT, "1")
        svc = CronService(base_dir=tmp_path)
        with pytest.raises(CronLoopSafetyError):
            with svc._file_lock():
                pass  # pragma: no cover

    @pytest.mark.asyncio
    async def test_file_lock_warns_not_raises_by_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(STRICT, raising=False)
        svc = CronService(base_dir=tmp_path)
        # Default (non-strict) degrades to a warning — must NOT raise, so the
        # many existing tests that seed jobs via sync mutators from an async
        # body keep working.
        with svc._file_lock():
            pass

    @pytest.mark.asyncio
    async def test_async_mutator_does_not_trip_guard_under_strict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(STRICT, "1")
        svc = await CronService.create(base_dir=tmp_path)
        sdk = CronSDK("app1", svc)
        # add_job_async offloads the lock to a worker thread (no running loop
        # there) → guard passes even under strict.
        job = await sdk.add_job_async("j", "m", every_secs=3600)
        assert job.id

    @pytest.mark.asyncio
    async def test_sync_sdk_facade_refuses_on_running_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Item 1 × item 3: the synchronous SDK facade REFUSES when called from
        a running loop. Offloading to a worker thread was rejected because the
        caller must still block on the result, parking the loop for the bounded
        lock window and stalling the whole gateway; inline is forbidden by the
        item-3 store guard. So there is no correct on-loop sync answer and the
        SDK fails fast, naming the ``*_async`` sibling."""
        monkeypatch.setenv(STRICT, "1")
        svc = await CronService.create(base_dir=tmp_path)
        sdk = CronSDK("app1", svc)
        # An owned job so remove/update reach the mutator (ownership is vetted
        # first and would otherwise raise PermissionError).
        owned = await sdk.add_job_async("j", "m", every_secs=3600)
        for call, sibling in (
            (lambda: sdk.add_job("j2", "m", every_secs=3600), "add_job_async"),
            (lambda: sdk.remove_job(owned.id), "remove_job_async"),
            (lambda: sdk.update_job(owned.id, message="m2"), "update_job_async"),
        ):
            with pytest.raises(CronSyncOnLoopError) as ei:
                call()
            # The message must name the async sibling so an app author can
            # migrate the call site without reading the source.
            assert sibling in str(ei.value)
        # Refused BEFORE any mutation: the store is untouched.
        assert [j.id for j in sdk.list_jobs()] == [owned.id]
        assert sdk.list_jobs()[0].message == "m"

    @pytest.mark.asyncio
    async def test_sync_remove_all_refuses_on_running_loop_without_mutating(
        self, tmp_path: Path
    ) -> None:
        """``remove_all`` refuses on-loop too, and refuses before removing
        anything — a half-applied cleanup would orphan still-ENABLED jobs."""
        svc = await CronService.create(base_dir=tmp_path)
        sdk = CronSDK("app1", svc)
        job = await sdk.add_job_async("j", "m", every_secs=3600)
        with pytest.raises(CronSyncOnLoopError) as ei:
            sdk.remove_all()
        assert "remove_all_async" in str(ei.value)
        assert [j.id for j in sdk.list_jobs()] == [job.id]

    def test_sync_facade_still_works_loopless(self, tmp_path: Path) -> None:
        """The refusal is scoped to on-loop callers: the published synchronous
        contract still holds off-loop (CLI / MCP process / worker thread), which
        is what third-party App Kit apps overwhelmingly use."""
        svc = CronService(base_dir=tmp_path)
        sdk = CronSDK("app1", svc)
        job = sdk.add_job("j", "m", every_secs=3600)
        assert not asyncio.iscoroutine(job)
        assert job.created_by == "app:app1"
        assert [j.id for j in sdk.list_jobs()] == [job.id]
        assert sdk.remove_all() == 1
