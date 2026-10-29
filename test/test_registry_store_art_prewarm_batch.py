"""The owner-tier store-art prewarm's batch: how one prewarm run is driven and bounded.

The two call sites (the listing's cache miss and the explicit refresh, after its
manifest-cache expiry), the caps on rows, tasks, retained row fields and screenshots,
the row cap counting every row it examines before any per-row read, art published only
for a manifest that reached the cache, the bound on post-budget cleanup, and a batch
that settles however it ends: a copy cancelled mid-flight or an outer cancellation.
"""

from __future__ import annotations

import contextlib
import dataclasses
import threading
from pathlib import Path
from typing import Any

import pytest
from registry_store_art_prewarm_helpers import (
    _BARRIER_CEILING_SECS,
    ART,
    FORGE,
    MANIFEST,
    OTHER_HOST,
    SIBLING,
    _await_barrier,
    _entry,
    _fake_fetch,
    _Loader,
    _pin_registry,
    _settled,
)

from kiro_crew.apps import registry as reg_mod
from kiro_crew.apps.registry import (
    _TRUST_OWNER,
    _blob_cache_dir,
    _copy_declared_art,
    _effective_registries,
    _fetch_owner_tier_store_assets,
    _prewarm_owner_tier_store_assets,
    _read_manifest_cache,
    _store_art_cache_path,
)
from kiro_crew.config import loader as loader_mod
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.testing.wait import LoopHop, await_until, until_parked

# ---------------------------------------------------------------------------
# Cancellation settles the copy without recording a partial run
# ---------------------------------------------------------------------------


class TestCopyCancellation:
    """A budget cancellation must not leave the copy worker writing a partial result:
    no ``.unobtainable`` sidecar and no not-yet-copied path may be recorded when the
    copy is cancelled, and the caller settles the worker before the checkout is
    removed. A completed row still lands."""

    def test_a_preset_cancel_records_nothing_and_writes_nothing(self, monkeypatch, tmp_path):
        clone = tmp_path / "clone"
        (clone / "ui").mkdir(parents=True)
        for rel, data in ART.items():
            (clone / rel).parent.mkdir(parents=True, exist_ok=True)
            (clone / rel).write_bytes(data)
        entry = _entry()
        cancel = threading.Event()
        cancel.set()  # cancelled before the first asset

        written, unobtainable = _copy_declared_art(entry, clone, MANIFEST, cancel)

        assert written == 0
        assert unobtainable == frozenset()
        # No sidecar written on a cancelled run.
        assert not reg_mod._unobtainable_art_path(entry).exists()
        # No art landed.
        assert not any(p.is_file() for p in _blob_cache_dir().rglob("*"))

    def test_cancellation_mid_run_records_nothing_but_a_completed_row_lands(
        self, monkeypatch, tmp_path
    ):
        clone = tmp_path / "clone"
        (clone / "ui").mkdir(parents=True)
        for rel, data in ART.items():
            (clone / rel).parent.mkdir(parents=True, exist_ok=True)
            (clone / rel).write_bytes(data)
        # A manifest declaring a missing path: a full run WOULD record it. Cancelling
        # partway must NOT record it.
        manifest = dict(MANIFEST, screenshots=["ui/shot-1.png", "ui/missing.png"])
        entry = _entry()

        cancel = threading.Event()
        real_write = reg_mod.atomic_write
        seen: list[str] = []

        def _write_then_cancel(path, data, *args, **kwargs):
            seen.append(str(path))
            real_write(path, data, *args, **kwargs)
            cancel.set()  # after the first successful write, request cancellation

        monkeypatch.setattr(reg_mod, "atomic_write", _write_then_cancel)
        written, unobtainable = _copy_declared_art(entry, clone, manifest, cancel)

        # The run stopped early and recorded nothing (no sidecar), even though a path
        # was missing.
        assert unobtainable == frozenset()
        assert not reg_mod._unobtainable_art_path(entry).exists()

        # A completed (uncancelled) run of the same fixture records the missing path.
        monkeypatch.setattr(reg_mod, "atomic_write", real_write)
        written2, unobtainable2 = _copy_declared_art(entry, clone, manifest, threading.Event())
        assert "ui/missing.png" in unobtainable2

    @pytest.mark.asyncio
    async def test_a_budget_cancellation_settles_the_copy_before_cleanup(self, monkeypatch):
        """End to end: the batch budget cancels an in-flight copy. The worker settles
        (records nothing) and the scratch clone is removed after it returns -- no
        sidecar and no partial art land for the cancelled row."""
        import asyncio

        reg = _pin_registry(monkeypatch, _TRUST_OWNER)

        started = asyncio.Event()

        # A copy that blocks until cancelled: the cancel event stops it at the first
        # per-asset check, so it returns without recording.
        real_copy = reg_mod._copy_declared_art

        def _slow_copy(entry, mdir, manifest, cancel=None):
            started.set()
            # Wait on the cooperative cancel flag, then defer to the real copy so
            # the cancelled-path early-return is exercised.
            if cancel is not None:
                cancel.wait()
            return real_copy(entry, mdir, manifest, cancel)

        _fake_fetch(
            monkeypatch, ART, dict(MANIFEST, screenshots=["ui/shot-1.png", "ui/missing.png"])
        )
        monkeypatch.setattr(reg_mod, "_copy_declared_art", _slow_copy)
        monkeypatch.setattr(reg_mod, "_PREWARM_BATCH_BUDGET", 0.2)
        entry = _entry()

        fetched = await await_until(
            asyncio.ensure_future(_prewarm_owner_tier_store_assets(reg, [entry]))
        )

        assert fetched == 0, "the cancelled row is not counted as fetched"
        # No sidecar and no partial art for the cancelled row.
        assert not reg_mod._unobtainable_art_path(entry).exists()
        assert not any(p.is_file() for p in _blob_cache_dir().rglob("*"))

    @pytest.mark.asyncio
    async def test_the_checkout_is_removed_only_after_the_worker_returned(self, monkeypatch):
        """The ordering the settle exists for: when a row is cancelled while its copy
        is in flight, the worker has RETURNED before the scratch checkout is removed.
        Observed at the removal call itself, so a cancellation that merely propagated
        out (leaving the thread live) is what this test turns red on.

        The cancel is sent once the copy worker is OBSERVED running, never by a short
        batch budget: the thread hops before the copy race any budget, and a budget
        that wins cancels a row with no worker to settle. The budget path and an outer
        cancel reach the same settle."""
        import asyncio

        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        loop = asyncio.get_running_loop()
        hop = LoopHop(loop)
        copy_entered = asyncio.Event()
        returned = threading.Event()
        removal_saw_worker_returned: list[bool] = []

        async def _one_loop_turn() -> None:
            return None

        real_copy = reg_mod._copy_declared_art
        real_rmtree = reg_mod._rmtree_force_settled

        def _slow_copy(entry, mdir, manifest, cancel=None):
            loop.call_soon_threadsafe(copy_entered.set)
            assert cancel is not None and cancel.wait(
                timeout=_BARRIER_CEILING_SECS
            ), "the copy worker never saw the cancel flag"
            # Linger past the flag until the loop has finished the step that set
            # it. That step either parks the caller on the worker (the settle) or,
            # for a caller that does not wait for the return, runs its ``finally``
            # straight into the removal, which records this thread as still here.
            hop.run(_one_loop_turn(), "a loop turn after the cancel flag was set")
            result = real_copy(entry, mdir, manifest, cancel)
            returned.set()
            return result

        async def _rmtree(path):
            removal_saw_worker_returned.append(returned.is_set())
            await real_rmtree(path)

        _fake_fetch(monkeypatch, ART, dict(MANIFEST, screenshots=["ui/shot-1.png"]))
        monkeypatch.setattr(reg_mod, "_copy_declared_art", _slow_copy)
        monkeypatch.setattr(reg_mod, "_rmtree_force_settled", _rmtree)
        # A batch budget that never fires: the only cancellation is the one below.
        monkeypatch.setattr(reg_mod, "_PREWARM_BATCH_BUDGET", 3600.0)

        task = asyncio.ensure_future(_prewarm_owner_tier_store_assets(reg, [_entry()]))
        try:
            await _await_barrier(
                copy_entered, "the copy worker", lambda: f"task.done()={task.done()}"
            )
        finally:
            # On every path: a cancel is what sets the copy's flag and lets it return.
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await _settled(task, "the cancelled prewarm")

        hop.check()
        assert removal_saw_worker_returned == [True], removal_saw_worker_returned


class TestScratchDirCancellation:
    """The scratch dir is made off the loop, so a cancel can land while that thread
    is still making it (a slow runner, where the batch budget fires during the hop).
    The dir the thread then makes must still be removed, however many cancels land."""

    @pytest.mark.asyncio
    async def test_cancels_during_the_scratch_mkdtemp_still_remove_the_dir(self, monkeypatch):
        """Two cancels land while the mkdtemp thread is held, and the thread makes the
        dir only after both were delivered. Each step is queued on the loop behind
        the one before it, so the order is fixed rather than raced against a budget."""
        import asyncio
        import os
        import tempfile

        from kiro_crew.apps.registry_pipeline import store_art

        _pin_registry(monkeypatch, _TRUST_OWNER)
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        finished = asyncio.Event()
        release = threading.Event()
        made: list[str] = []
        real_mkdtemp = tempfile.mkdtemp

        def _held_mkdtemp(*args, **kwargs):
            if kwargs.get("prefix") != "kirocrew-store-art-":
                return real_mkdtemp(*args, **kwargs)
            try:
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(timeout=_BARRIER_CEILING_SECS), "never released"
                path = real_mkdtemp(*args, **kwargs)
                made.append(path)
                return path
            finally:
                loop.call_soon_threadsafe(finished.set)

        _fake_fetch(monkeypatch, ART, dict(MANIFEST, screenshots=["ui/shot-1.png"]))
        monkeypatch.setattr(store_art.tempfile, "mkdtemp", _held_mkdtemp)

        def _second_cancel_then_release() -> None:
            # Runs after the first cancel was delivered; the release is queued
            # behind the second one, so the thread returns only after both.
            task.cancel()
            loop.call_soon(release.set)

        task = asyncio.ensure_future(_fetch_owner_tier_store_assets(_entry(), "test"))
        try:
            try:
                await _await_barrier(entered, "the mkdtemp thread", lambda: f"done={task.done()}")
            finally:
                task.cancel()
                loop.call_soon(_second_cancel_then_release)
            with pytest.raises(asyncio.CancelledError):
                await _settled(task, "the cancelled fetch")
            # Without the settle the thread outlives the task; wait for it to return.
            await _await_barrier(finished, "the mkdtemp thread's return")

            assert made, "the mkdtemp thread never made the scratch dir"
            leaked = [p for p in made if os.path.exists(p)]
            assert not leaked, f"a cancel during mkdtemp leaked the scratch dir: {leaked}"
        finally:
            release.set()
            for path in made:
                with contextlib.suppress(OSError):
                    os.rmdir(path)


# ---------------------------------------------------------------------------
# The two call sites
# ---------------------------------------------------------------------------


class TestCallSites:
    @pytest.mark.asyncio
    async def test_the_listing_prewarms_on_a_fresh_fetch_and_never_on_a_cache_hit(
        self, monkeypatch
    ):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        rows = [_entry()]
        warmed: list[list[dict[str, Any]]] = []

        async def _prewarm(r, entries):
            assert r == reg
            warmed.append(entries)
            return 0

        async def _fetch(r):
            return rows

        monkeypatch.setattr(reg_mod, "_prewarm_owner_tier_store_assets", _prewarm)
        monkeypatch.setattr(reg_mod, "_fetch_and_cache_external_registry", _fetch)
        cache: dict[str, Any] = {"rows": None}
        monkeypatch.setattr(
            reg_mod,
            "_read_external_registry_cache",
            lambda name, *, ignore_ttl=False: cache["rows"],
        )

        assert await reg_mod._load_external_registries() == rows
        assert warmed == [rows], "a fresh fetch must hand its rows to the prewarm"

        # The cached path: the same rows, read back from disk, must NOT be prewarmed.
        cache["rows"] = [dict(r) for r in rows]
        await reg_mod._load_external_registries()
        assert len(warmed) == 1

    @pytest.mark.asyncio
    async def test_the_prewarm_runs_after_every_sibling_has_written_its_cache(self, monkeypatch):
        """Two registries fetched fresh and concurrently. The prewarm for either runs
        only after BOTH fetches have finished (and so written their caches): the
        provenance gate reads the sibling's cache, and a prewarm run inside the
        per-registry load would find it unwritten on a first load and skip every
        row as ambiguous."""
        import asyncio

        cfg = KiroCrewConfig()
        cfg.registries = []
        monkeypatch.setattr(loader_mod.KiroCrewConfig, "load", staticmethod(lambda: cfg))
        base = build_default_context(KiroCrewConfig())
        ctx = dataclasses.replace(
            base,
            apps_loader=_Loader(
                [
                    {"name": "official", "repo": FORGE, "trust": _TRUST_OWNER},
                    {"name": "second", "repo": OTHER_HOST, "trust": _TRUST_OWNER},
                ]
            ),
        )
        monkeypatch.setattr(reg_mod, "current_context", lambda: ctx)
        regs = _effective_registries()
        assert [r.name for r in regs] == ["official", "second"]

        fetched: set[str] = set()
        seen_at_prewarm: list[frozenset[str]] = []
        official_fetched = asyncio.Event()

        async def _fetch(r):
            # The sibling finishes last, gated on the first fetch: a prewarm inside
            # the per-registry load would run for "official" in the same loop turn
            # its fetch returns, while "second" is still parked here, unfetched.
            if r.name == "second":
                await await_until(official_fetched, what="the official registry's fetch")
            fetched.add(r.name)
            if r.name == "official":
                official_fetched.set()
            return [dict(_entry(), _registry=r.name)]

        async def _prewarm(r, entries):
            seen_at_prewarm.append(frozenset(fetched))
            return 0

        monkeypatch.setattr(reg_mod, "_fetch_and_cache_external_registry", _fetch)
        monkeypatch.setattr(reg_mod, "_prewarm_owner_tier_store_assets", _prewarm)
        monkeypatch.setattr(
            reg_mod,
            "_read_external_registry_cache",
            lambda name, *, ignore_ttl=False: None,
        )

        rows = await reg_mod._load_external_registries()

        assert len(rows) == 2
        assert seen_at_prewarm == [frozenset({"official", "second"})] * 2, seen_at_prewarm

    @pytest.mark.asyncio
    async def test_refresh_prewarms_after_expiring_the_manifest_caches(self, monkeypatch):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        rows = [_entry()]
        order: list[str] = []

        async def _fetch(r):
            return rows

        async def _prewarm(r, entries):
            assert r == reg and entries is rows
            order.append("prewarm")
            return 1

        async def _list():
            return rows

        monkeypatch.setattr(reg_mod, "_fetch_and_cache_external_registry", _fetch)
        monkeypatch.setattr(reg_mod, "_prewarm_owner_tier_store_assets", _prewarm)
        monkeypatch.setattr(reg_mod, "_expire_cache_file", lambda path: order.append("expire"))
        monkeypatch.setattr(
            reg_mod,
            "_read_external_registry_cache",
            lambda name, *, ignore_ttl=False: None,
        )
        monkeypatch.setattr(reg_mod, "list_registry", _list)

        result = await reg_mod.refresh_registries()

        assert result["ok"] is True
        assert "prewarm" in order and "expire" in order
        assert order.index("prewarm") > max(i for i, step in enumerate(order) if step == "expire")

    @pytest.mark.asyncio
    async def test_refresh_prewarms_only_after_every_registry_has_written_its_cache(
        self, monkeypatch
    ):
        """Two registries refreshed in one call. ``refresh_registries`` fetches them
        one after another, so a prewarm run inside the per-registry loop for the
        FIRST registry would read the SECOND's index cache before that refetch had
        written it -- and the provenance gate counts an absent sibling cache as a
        possible claimant, leaving every row cold. Both prewarms must therefore run
        only after both registries have been fetched and had their manifest caches
        expired, in refresh order, each with its own fresh rows."""
        cfg = KiroCrewConfig()
        cfg.registries = []
        monkeypatch.setattr(loader_mod.KiroCrewConfig, "load", staticmethod(lambda: cfg))
        base = build_default_context(KiroCrewConfig())
        ctx = dataclasses.replace(
            base,
            apps_loader=_Loader(
                [
                    {"name": "official", "repo": FORGE, "trust": _TRUST_OWNER},
                    {"name": "second", "repo": OTHER_HOST, "trust": _TRUST_OWNER},
                ]
            ),
        )
        monkeypatch.setattr(reg_mod, "current_context", lambda: ctx)
        regs = _effective_registries()
        assert [r.name for r in regs] == ["official", "second"]

        fetched: list[str] = []
        expired: list[str] = []
        rows_by_name = {r.name: [dict(_entry(), _registry=r.name)] for r in regs}
        seen_at_prewarm: list[tuple[str, frozenset[str], int, list[dict[str, Any]]]] = []

        async def _fetch(r):
            # The index cache is written by this fetch (the real one caches on
            # success); recording the name is the cache-written marker.
            fetched.append(r.name)
            return rows_by_name[r.name]

        async def _prewarm(r, entries):
            seen_at_prewarm.append((r.name, frozenset(fetched), len(expired), entries))
            return 0

        async def _list():
            return []

        monkeypatch.setattr(reg_mod, "_fetch_and_cache_external_registry", _fetch)
        monkeypatch.setattr(reg_mod, "_prewarm_owner_tier_store_assets", _prewarm)
        monkeypatch.setattr(reg_mod, "_expire_cache_file", lambda path: expired.append(str(path)))
        monkeypatch.setattr(
            reg_mod,
            "_read_external_registry_cache",
            lambda name, *, ignore_ttl=False: None,
        )
        monkeypatch.setattr(reg_mod, "list_registry", _list)

        result = await reg_mod.refresh_registries()

        assert result["ok"] is True
        assert result["refreshed"] == ["official", "second"]
        assert fetched == ["official", "second"]
        # Each registry row's manifest file and failed-fetch marker were expired
        # BEFORE either prewarm ran.
        assert len(expired) == 4, expired
        assert sum(p.endswith(".miss.json") for p in expired) == 2, expired
        assert [name for name, _f, _e, _rows in seen_at_prewarm] == ["official", "second"]
        for name, fetched_at, expired_at, rows in seen_at_prewarm:
            assert fetched_at == frozenset({"official", "second"}), (name, fetched_at)
            assert expired_at == 4, (name, expired_at)
            assert rows is rows_by_name[name], "each prewarm gets its own fresh rows"


# ---------------------------------------------------------------------------
# The prewarm batch is bounded in rows, tasks, and retained payload
# ---------------------------------------------------------------------------


class TestPrewarmBatchBounds:
    """A large owner-tier index must not exhaust memory. The prewarm caps how many
    rows one batch CONSIDERS (`_PREWARM_MAX_ROWS`), drives them through a FIXED pool
    of `_PREWARM_CONCURRENCY` workers pulling from a queue (never one task per row),
    and retains only the slim fields a fetch needs per candidate."""

    @pytest.mark.asyncio
    async def test_an_oversized_index_attempts_at_most_the_cap_and_logs_overflow(
        self, monkeypatch, caplog
    ):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        monkeypatch.setattr(reg_mod, "_PREWARM_MAX_ROWS", 5)
        attempted: list[str] = []

        async def _fetch(entry, registry_name):
            attempted.append(entry["name"])
            return True

        monkeypatch.setattr(reg_mod, "_fetch_owner_tier_store_assets", _fetch)
        rows = [
            _entry(
                name=f"app-{i}", gitUrl=f"{SIBLING[:-4]}-{i}.git", repo=f"{SIBLING[:-4]}-{i}.git"
            )
            for i in range(1000)
        ]

        with caplog.at_level("WARNING", logger="kiro_crew.apps.registry"):
            fetched = await _prewarm_owner_tier_store_assets(reg, rows)

        assert fetched == 5
        assert len(attempted) == 5, "no more than the cap is ever fetched"
        assert any("more rows than one batch considers" in r.getMessage() for r in caplog.records)
        # The warning reports the INDEX size and the cap -- not the candidate count
        # plus overflow, which is neither.
        assert any("(1000 rows, capped at 5)" in r.getMessage() for r in caplog.records), [
            r.getMessage() for r in caplog.records
        ]
        assert any("the 995 past the cap stay cold" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_the_task_count_never_exceeds_the_concurrency(self, monkeypatch):
        import asyncio

        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        monkeypatch.setattr(reg_mod, "_PREWARM_CONCURRENCY", 3)
        monkeypatch.setattr(reg_mod, "_PREWARM_MAX_ROWS", 1000)
        live = 0
        peak = 0
        release = asyncio.Event()

        async def _fetch(entry, registry_name):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            # Every fetch stays in flight until the test has seen the pool full.
            await release.wait()
            live -= 1
            return True

        monkeypatch.setattr(reg_mod, "_fetch_owner_tier_store_assets", _fetch)
        rows = [
            _entry(
                name=f"app-{i}", gitUrl=f"{SIBLING[:-4]}-{i}.git", repo=f"{SIBLING[:-4]}-{i}.git"
            )
            for i in range(40)
        ]

        prewarm = asyncio.ensure_future(_prewarm_owner_tier_store_assets(reg, rows))
        try:
            # The pool is full once three fetches are parked; a task per row would
            # have parked all forty by the time this poll first reads.
            await until_parked(release, count=3, describe=lambda: f"live={live} peak={peak}")
            assert peak == 3, peak
        finally:
            release.set()
        fetched = await _settled(prewarm, "the prewarm batch")

        assert fetched == 40
        assert peak == 3, peak

    @pytest.mark.asyncio
    async def test_the_retained_row_drops_fields_a_fetch_does_not_read(self, monkeypatch):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        seen: list[dict] = []

        async def _fetch(entry, registry_name):
            seen.append(entry)
            return True

        monkeypatch.setattr(reg_mod, "_fetch_owner_tier_store_assets", _fetch)
        # A row carrying a large description and unrelated display fields.
        row = _entry(description="x" * 100_000, screenshotsDark=["a.png"], tags=["t"] * 50)

        await _prewarm_owner_tier_store_assets(reg, [row])

        (retained,) = seen
        assert "description" not in retained
        assert "tags" not in retained
        assert "screenshotsDark" not in retained
        # The fields a fetch and its caches DO read are kept.
        assert retained["name"] == "app"
        assert retained["repo"] == SIBLING
        assert retained["branch"] == "main"
        assert retained["_registry"] == "official"

    def test_the_slim_projection_keeps_only_the_fetch_fields(self):
        slim = reg_mod._slim_prewarm_row(
            {
                "name": "app",
                "gitUrl": "g",
                "repo": "r",
                "branch": "b",
                "subdirectory": "s",
                "commit": "c",
                "_registry": "official",
                "description": "x" * 1000,
                "iconPath": "ui/icon.svg",
            }
        )
        assert set(slim) == {
            "name",
            "gitUrl",
            "repo",
            "branch",
            "subdirectory",
            "commit",
            "_registry",
        }


class TestPrewarmRowFieldCap:
    """A retained prewarm-row field is a scalar string coordinate; an owner-tier index
    is external input and can pad one to an arbitrary length or make it a non-string.
    ``_slim_prewarm_row`` rejects such a row (returns None) so it never earns an
    owner-credentialed clone and the padded string never sits in memory for the batch's
    life. The reject is per-field and bounded by ``_PREWARM_FIELD_MAX_LEN``."""

    def test_an_over_length_field_rejects_the_row(self, monkeypatch):
        # Cap above the normal-length coordinates (SIBLING is ~50 chars) so ONLY the
        # padded branch exceeds it and drives the reject.
        monkeypatch.setattr(reg_mod, "_PREWARM_FIELD_MAX_LEN", 60)
        row = _entry(branch="b" * 100)
        assert reg_mod._slim_prewarm_row(row) is None

    def test_a_non_string_field_rejects_the_row(self, monkeypatch):
        row = _entry()
        row["branch"] = 7  # a non-string retained field
        assert reg_mod._slim_prewarm_row(row) is None

    def test_a_within_cap_string_row_is_kept_negative_control(self, monkeypatch):
        """Negative control: the same shape with every retained field a short string
        is kept, proving the rejects above observe the field and not the fixture. Uses
        the default cap so the normal-length coordinates in ``_entry`` all pass."""
        slim = reg_mod._slim_prewarm_row(_entry(branch="main"))
        assert slim is not None and slim["branch"] == "main"

    def test_a_missing_field_is_not_a_reject(self):
        """An ABSENT retained field is omitted from the projection, not a rejection --
        only a PRESENT non-string or over-length one rejects the row."""
        row = _entry()
        row.pop("subdirectory", None)  # absent, not present-and-bad
        slim = reg_mod._slim_prewarm_row(row)
        assert slim is not None and "subdirectory" not in slim

    @pytest.mark.asyncio
    async def test_a_row_with_an_over_length_field_is_skipped_and_not_cloned(
        self, monkeypatch, caplog
    ):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        monkeypatch.setattr(reg_mod, "_PREWARM_FIELD_MAX_LEN", 60)
        calls = _fake_fetch(monkeypatch, ART)
        # A valid repo key (so it passes the provenance gate) but an over-length branch.
        entry = _entry(branch="b" * 100)

        with caplog.at_level("WARNING", logger="kiro_crew.apps.registry"):
            assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 0

        assert calls == [], "a field-rejected row must not be cloned"
        assert _read_manifest_cache(entry) is None
        assert any("over-length retained field" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_the_field_cap_can_be_lifted_negative_control(self, monkeypatch):
        """Negative control: with the cap raised above the same branch the row IS
        cloned, proving the skip observes the cap and not an accident of the fixture."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        monkeypatch.setattr(reg_mod, "_PREWARM_FIELD_MAX_LEN", 4096)
        calls = _fake_fetch(monkeypatch, ART)
        # A servable branch name under the raised cap.
        entry = _entry(branch="b" * 100)

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 1
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# One row cannot fill the blob cache with screenshots
# ---------------------------------------------------------------------------


class TestPerRowScreenshotCap:
    """An owner-tier index is external input and a row's ``screenshots`` list can be
    unbounded; each accepted path is a clone read and a blob-cache file up to
    ``_ART_MAX_BYTES``. ``_declared_store_art`` caps each list field at
    ``_MAX_LIST_ART_PATHS_PER_FIELD`` so one row cannot fill the cache. The cap is
    applied identically to the copy and the warm check (both call it), so a dropped
    path can never keep the row cold."""

    def test_a_list_field_contributes_at_most_the_cap(self):
        cap = reg_mod._MAX_LIST_ART_PATHS_PER_FIELD
        manifest = dict(MANIFEST)
        manifest.pop("iconPath", None)
        manifest.pop("heroImage", None)
        manifest["screenshots"] = [f"ui/shot-{i}.png" for i in range(cap + 10)]
        manifest["screenshotsDark"] = [f"ui/dark-{i}.png" for i in range(cap + 10)]

        declared = reg_mod._declared_store_art(manifest)

        assert declared[:cap] == [f"ui/shot-{i}.png" for i in range(cap)]
        assert declared[cap : 2 * cap] == [f"ui/dark-{i}.png" for i in range(cap)]
        # Each list field contributed exactly the cap; nothing past it leaked in.
        assert len(declared) == 2 * cap
        assert "ui/shot-%d.png" % cap not in declared

    @pytest.mark.asyncio
    async def test_only_the_capped_screenshots_are_copied(self, monkeypatch, caplog):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        cap = reg_mod._MAX_LIST_ART_PATHS_PER_FIELD
        shots = [f"ui/shot-{i}.png" for i in range(cap + 5)]
        manifest = dict(MANIFEST, iconPath="ui/icon.svg", heroImage="", screenshots=shots)
        tree = {"ui/icon.svg": b"<svg/>", **{s: b"\x89PNG" for s in shots}}
        _fake_fetch(monkeypatch, tree, manifest)
        entry = _entry()

        with caplog.at_level("WARNING", logger="kiro_crew.apps.registry"):
            assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 1

            # The first `cap` screenshots landed; the extras were never read or cached.
            assert _store_art_cache_path(entry, shots[cap - 1]).is_file()
            assert not _store_art_cache_path(entry, shots[cap]).exists()
            # The row is warm despite declaring more than the cap: the warm check uses
            # the same capped list, so the dropped paths do not keep it cold.
            assert reg_mod._store_assets_warm(entry, _read_manifest_cache(entry))

        # The overflow is counted and said out loud ONCE for this manifest snapshot;
        # the warm check re-derives the capped list without logging again.
        overflow = [r for r in caplog.records if "more screenshots than one row" in r.getMessage()]
        assert len(overflow) == 1
        assert "screenshots=5" in overflow[0].getMessage()

    def test_no_overflow_is_reported_at_or_under_the_cap(self):
        cap = reg_mod._MAX_LIST_ART_PATHS_PER_FIELD
        manifest = dict(MANIFEST, screenshots=[f"ui/s-{i}.png" for i in range(cap)])
        assert reg_mod._dropped_list_art_paths(manifest) == {}
        manifest["screenshotsDark"] = [f"ui/d-{i}.png" for i in range(cap + 2)]
        assert reg_mod._dropped_list_art_paths(manifest) == {"screenshotsDark": 2}

    def test_the_cap_can_be_lifted_negative_control(self, monkeypatch):
        """Negative control: raising the cap lets the same manifest declare more,
        proving the truncation above observes the constant and not the fixture."""
        monkeypatch.setattr(reg_mod, "_MAX_LIST_ART_PATHS_PER_FIELD", 100)
        manifest = dict(MANIFEST)
        manifest.pop("iconPath", None)
        manifest.pop("heroImage", None)
        manifest["screenshots"] = [f"ui/shot-{i}.png" for i in range(30)]
        manifest["screenshotsDark"] = []

        declared = reg_mod._declared_store_art(manifest)

        assert len([d for d in declared if d.startswith("ui/shot-")]) == 30


# ---------------------------------------------------------------------------
# The post-budget cleanup is bounded, and an over-budget cleanup is detached
# ---------------------------------------------------------------------------


class TestBoundedPostBudgetCleanup:
    """``_PREWARM_BATCH_BUDGET`` is the clone-cancellation deadline; after it fires the
    listing request otherwise awaits each cancelled clone's process-group kill and
    scratch ``rmtree`` with no bound, so a hanging forge on a slow filesystem stalls a
    cold App Store load for tens of seconds. The batch's cancellation/cleanup phase is
    wrapped in ``_PREWARM_CLEANUP_BUDGET``, and a cleanup still running at that deadline
    is detached to finish in the background so the request returns."""

    @pytest.mark.asyncio
    async def test_a_hanging_cleanup_is_detached_and_the_request_returns_before_it_finishes(
        self, monkeypatch, caplog
    ):
        import asyncio

        reg = _pin_registry(monkeypatch, _TRUST_OWNER)

        release = threading.Event()
        cleanup_began = threading.Event()
        cleanup_finished = threading.Event()
        loop = asyncio.get_running_loop()

        async def _fetch(entry, registry_name):
            # The row's fetch, parked on its first step: a worker reaches it in the
            # same loop turn the batch starts, so the budget always lands here, never
            # in the thread hops a real fetch makes first. Cancelled at the budget,
            # its cleanup blocks until the test releases it -- standing in for a slow
            # filesystem / process kill. It can only finish AFTER the prewarm has
            # returned, because the test releases it only then.
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleanup_began.set()
                await loop.run_in_executor(None, release.wait)
                cleanup_finished.set()
                raise
            return True

        from kiro_crew.apps.registry_pipeline import store_art

        class _WaitSpy:
            """``asyncio`` for the prewarm's owner, recording each ``asyncio.wait``
            timeout so the test reads which budget bounds each phase, not how long
            the phase took."""

            def __init__(self) -> None:
                self.timeouts: list[float | None] = []

            def __getattr__(self, name: str) -> Any:
                return getattr(asyncio, name)

            async def wait(self, fs, *, timeout=None, **kwargs):
                self.timeouts.append(timeout)
                return await asyncio.wait(fs, timeout=timeout, **kwargs)

        # Distinct budgets, so a cleanup wait bounded by the batch budget (or by any
        # value but the cleanup budget) shows up in the recorded order.
        batch_budget, cleanup_budget = 0.2, 0.3
        spy = _WaitSpy()
        monkeypatch.setattr(reg_mod, "_fetch_owner_tier_store_assets", _fetch)
        monkeypatch.setattr(reg_mod, "_PREWARM_BATCH_BUDGET", batch_budget)
        monkeypatch.setattr(reg_mod, "_PREWARM_CLEANUP_BUDGET", cleanup_budget)
        monkeypatch.setattr(store_art, "asyncio", spy)
        entry = _entry()
        # Everything from the first ``await`` to the last assertion that precedes the
        # release runs under ``try/finally``: the fake cleanup parks a default-executor
        # worker thread on ``release.wait``, and an assertion that fired before
        # ``release.set()`` would strand that (non-daemon) thread and hang the test
        # session at interpreter exit. The finally releases it and drains the detached
        # cleanup regardless of how the body ended.
        prewarm: asyncio.Future[int] | None = None
        try:
            # The bound is proved by ORDER, not by wall clock: the cleanup cannot finish
            # until ``release`` is set, and ``release`` is set only after the prewarm
            # returns, so a prewarm that waits on the cleanup never returns at all. The
            # ceiling is only the hang guard that turns that into a named failure; how
            # long a loaded runner takes to schedule the two budgets is not under test.
            # ``asyncio.wait`` rather than ``wait_for``: on a hang, ``wait_for`` would
            # cancel the prewarm and then wait for it to unwind, which a prewarm stuck
            # on the unreleased cleanup never does.
            with caplog.at_level("WARNING", logger="kiro_crew.apps.registry"):
                prewarm = asyncio.ensure_future(_prewarm_owner_tier_store_assets(reg, [entry]))
                done, _ = await asyncio.wait({prewarm}, timeout=_BARRIER_CEILING_SECS)
            if prewarm not in done:
                pytest.fail(
                    "the budgeted prewarm did not return within the "
                    f"{_BARRIER_CEILING_SECS:.0f}s hang guard; it waited on the cleanup"
                )
            fetched = prewarm.result()

            assert fetched == 0
            assert cleanup_began.is_set(), "the batch budget never cancelled the row"
            # The batch phase waits the batch budget and the cleanup phase the cleanup
            # budget, each read from its own constant.
            assert spy.timeouts == [batch_budget, cleanup_budget], (
                "expected waits bounded by the batch then the cleanup budget, "
                f"got {spy.timeouts}"
            )
            # A request that waited on the cleanup is caught by the hang guard above:
            # ``release`` is still unset here, so the cleanup cannot have finished.
            messages = [r.getMessage() for r in caplog.records]
            assert any("cleanup exceeded its" in m for m in messages)
            assert any("exceeded its" in m and "batch budget" in m for m in messages)
            # The detached cleanup is still pending; releasing it lets it finish, and
            # the pending set drains.
            assert reg_mod._PENDING_PREWARM_CLEANUPS
        finally:
            # Always unblock the executor worker, then settle the detached cleanup so
            # no task or thread outlives the test.
            release.set()
            pending: set[asyncio.Future[Any]] = set(reg_mod._PENDING_PREWARM_CLEANUPS)
            if prewarm is not None and not prewarm.done():
                pending.add(prewarm)
            if pending:
                await asyncio.wait(pending, timeout=_BARRIER_CEILING_SECS)
        assert cleanup_finished.is_set(), "the detached cleanup must still complete"
        assert not reg_mod._PENDING_PREWARM_CLEANUPS, "a finished cleanup removes itself"

    @pytest.mark.asyncio
    async def test_an_unbounded_cleanup_would_block_past_the_deadline_negative_control(
        self, monkeypatch
    ):
        """Negative control: emulate the pre-fix single ``wait_for`` (which awaits the
        cancelled clones' cleanup inline) and observe it blocks past the cleanup
        deadline -- proving the bound above is what returns the request promptly."""
        import asyncio

        release = asyncio.Event()
        deadline_fired = asyncio.Event()

        async def _worker():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # The pre-fix shape awaits this cleanup inline, unbounded.
                deadline_fired.set()
                await release.wait()
                raise

        async def _prefix_wait():
            # The old code: one wait_for over the gather, whose timeout cancels the
            # worker and then awaits its (hanging) cleanup before raising.
            try:
                await asyncio.wait_for(
                    asyncio.gather(_worker()), timeout=0.2
                )  # flake-ok: negative control simulating pre-fix bug; the timeout IS the assertion proving the old code blocked
            except asyncio.TimeoutError:
                return 0

        task = asyncio.ensure_future(_prefix_wait())
        try:
            # The deadline has fired and cancelled the worker, which is now parked in
            # its cleanup. Nothing but ``release`` can end that cleanup, so a pre-fix
            # shape still pending here is blocked past its deadline by construction,
            # not by how long this test chose to look.
            await _await_barrier(deadline_fired, "the pre-fix deadline")
            await until_parked(release)
            assert not task.done(), "the pre-fix shape should still be blocked on cleanup"
        finally:
            release.set()
        assert await _settled(task, "the pre-fix wait") == 0

    @pytest.mark.asyncio
    async def test_a_quick_cleanup_settles_within_the_budget_without_detaching(self, monkeypatch):
        """When the cancelled clones' cleanup settles within ``_PREWARM_CLEANUP_BUDGET``,
        nothing is detached: the pending set stays empty."""
        import asyncio

        reg = _pin_registry(monkeypatch, _TRUST_OWNER)

        async def _hang_fetch(git_url, branch, dest, log_lines, *, clone_env, sandbox_mode, **kw):
            Path(dest).mkdir(parents=True, exist_ok=True)
            await asyncio.Event().wait()
            return None

        monkeypatch.setattr(reg_mod, "_git_fetch_branch", _hang_fetch)
        monkeypatch.setattr(reg_mod, "_PREWARM_BATCH_BUDGET", 0.2)
        monkeypatch.setattr(reg_mod, "_PREWARM_CLEANUP_BUDGET", 2.0)
        assert not reg_mod._PENDING_PREWARM_CLEANUPS

        fetched = await await_until(
            asyncio.ensure_future(_prewarm_owner_tier_store_assets(reg, [_entry()]))
        )

        assert fetched == 0
        assert not reg_mod._PENDING_PREWARM_CLEANUPS, "a cleanup within budget is not detached"

    def test_the_cleanup_budget_is_at_least_the_process_kill_grace(self):
        """The cleanup budget's floor is ``checkout._KILL_GRACE_PERIOD`` (one clone's
        kill grace); a value below it would abandon cleanups about to finish."""
        assert reg_mod._PREWARM_CLEANUP_BUDGET >= reg_mod._KILL_GRACE_PERIOD


# ---------------------------------------------------------------------------
# An OUTER cancellation (gateway shutdown) settles the batch, not leaks it
# ---------------------------------------------------------------------------


class TestOuterCancellationSettlesTheBatch:
    """``_PREWARM_BATCH_BUDGET`` bounds the batch, but the whole prewarm can also be
    cancelled from OUTSIDE — a gateway shutdown mid cold load. ``asyncio.wait`` does
    NOT cancel the futures it waits on when it is itself cancelled, so without a guard
    the batch and its up-to-``_PREWARM_CONCURRENCY`` owner-credentialed clones would be
    left running detached with their scratch dirs. The prewarm cancels the batch and
    settles its cleanup before propagating the cancellation."""

    @pytest.mark.asyncio
    async def test_cancelling_the_prewarm_cancels_the_workers_and_settles_cleanup(
        self, monkeypatch
    ):
        import asyncio

        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        cancelled: list[str] = []
        cleaned: list[str] = []
        started = asyncio.Event()

        async def _hang_fetch(git_url, branch, dest, log_lines, *, clone_env, sandbox_mode, **kw):
            Path(dest).mkdir(parents=True, exist_ok=True)
            started.set()
            try:
                await asyncio.Event().wait()  # never returns on its own
            except asyncio.CancelledError:
                cancelled.append(git_url)
                raise
            return None

        real_rmtree = reg_mod._rmtree_force_settled

        async def _rmtree(path):
            cleaned.append(str(path))
            await real_rmtree(path)

        monkeypatch.setattr(reg_mod, "_git_fetch_branch", _hang_fetch)
        monkeypatch.setattr(reg_mod, "_rmtree_force_settled", _rmtree)
        # A long batch budget so the deadline never fires: the ONLY thing that ends
        # this run is the outer cancellation below.
        monkeypatch.setattr(reg_mod, "_PREWARM_BATCH_BUDGET", 3600.0)
        entry = _entry()

        task = asyncio.ensure_future(_prewarm_owner_tier_store_assets(reg, [entry]))
        await await_until(started)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await await_until(task)

        # The in-flight clone was cancelled (not abandoned) and its scratch dir removed.
        assert cancelled, "the worker clone was not cancelled on outer cancellation"
        assert cleaned, "the cancelled clone's scratch dir was not cleaned up"

    @pytest.mark.asyncio
    async def test_a_bare_wait_would_leak_the_batch_negative_control(self, monkeypatch):
        """Negative control: the pre-fix shape (a bare ``asyncio.wait`` with no
        cancellation guard) leaves the inner batch RUNNING when the outer wait is
        cancelled — proving the guard above is what settles it. Reproduced inline so
        the control does not depend on reverting the production code."""
        import asyncio

        worker_cancelled: list[str] = []
        leaked: list[asyncio.Future] = []
        worker_parked = asyncio.Event()

        async def _worker():
            try:
                worker_parked.set()
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                worker_cancelled.append("w")
                raise

        async def _prefix_prewarm():
            batch = asyncio.ensure_future(asyncio.gather(_worker()))
            leaked.append(batch)
            # The pre-fix code: no try/except around the wait, so an outer
            # cancellation raises straight out and never cancels ``batch``.
            await asyncio.wait({batch}, timeout=3600.0)
            return batch

        task = asyncio.ensure_future(_prefix_prewarm())
        # The worker runs inside the batch, and the control task created the batch
        # and went straight into its bare wait in the same turn: both are parked
        # where the cancel must find them.
        await _await_barrier(worker_parked, "the batch's worker")
        assert leaked and not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await _settled(task, "the cancelled control")
        # The batch was NOT cancelled by the bare wait: the worker still runs.
        assert worker_cancelled == [], "the bare-wait shape should leak the running batch"
        # Clean up the leaked batch this control deliberately created.
        for fut in leaked:
            fut.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await fut

    @pytest.mark.asyncio
    async def test_an_outer_cancel_during_the_cleanup_window_propagates(self, monkeypatch):
        """The budget-elapsed path cancels the workers and then waits the cleanup
        budget with ``asyncio.wait({batch}, timeout=...)``. If an OUTER cancellation
        (gateway shutdown) lands during that cleanup wait it must PROPAGATE -- the
        prewarm coroutine raises ``CancelledError`` and does NOT return a count. A
        ``wait_for(shield(batch))`` + ``except CancelledError: pass`` would swallow it
        as though it were the settling batch's own re-raised cancellation."""
        import asyncio

        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        cleanup_entered = asyncio.Event()
        release = asyncio.Event()
        held: list[Any] = []

        async def _fetch(entry, registry_name):
            # The row's fetch, parked on its first step: a worker reaches it in the
            # same loop turn the batch starts, so the short budget always lands
            # here, never in the thread hops a real fetch makes first. Its cleanup
            # blocks, holding the prewarm in the cleanup wait for the outer cancel
            # below to land there.
            held.append(asyncio.current_task())
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleanup_entered.set()
                await release.wait()
                raise
            return True

        monkeypatch.setattr(reg_mod, "_fetch_owner_tier_store_assets", _fetch)
        # Short batch budget -> the run reaches the cleanup wait quickly; a long
        # cleanup budget -> the cleanup wait is where the outer cancel lands.
        monkeypatch.setattr(reg_mod, "_PREWARM_BATCH_BUDGET", 0.1)
        monkeypatch.setattr(reg_mod, "_PREWARM_CLEANUP_BUDGET", 3600.0)

        task = asyncio.ensure_future(_prewarm_owner_tier_store_assets(reg, [_entry()]))
        try:
            await _await_barrier(cleanup_entered, "the cancelled row's cleanup")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await _settled(task, "the prewarm cancelled in its cleanup window")
        finally:
            # Let the blocked cleanup drain, even on a failed assertion, so the
            # test leaves nothing running behind it.
            release.set()
            if held:
                await asyncio.wait(held, timeout=_BARRIER_CEILING_SECS)


class TestCancelledBatchLeavesNoUnretrievedException:
    """A cancelled batch is an outcome the prewarm handles, so it must not reach the
    loop's exception handler. ``asyncio.gather`` records a requested cancellation with
    ``set_exception(CancelledError())``; left unread, GC logs ``_GatheringFuture
    exception was never retrieved`` and ``crash_guard`` writes a false ``ASYNCIO
    UNHANDLED`` record to ``crash.log``. Both ways a batch is cancelled --
    the budget and an outer cancel -- are covered."""

    @staticmethod
    async def _handler_reports(run) -> list[str]:
        import asyncio
        import gc

        loop = asyncio.get_running_loop()
        seen: list[str] = []
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, ctx: seen.append(ctx.get("message", "")))
        try:
            await run()
            # The batch is unreachable once the prewarm returned; collect it so an
            # unread exception is reported now, inside this handler.
            gc.collect()
            await asyncio.sleep(0)
        finally:
            loop.set_exception_handler(previous)
        return seen

    @staticmethod
    def _hang_on_fetch(monkeypatch, started):
        import asyncio

        async def _hang_fetch(git_url, branch, dest, log_lines, *, clone_env, sandbox_mode, **kw):
            Path(dest).mkdir(parents=True, exist_ok=True)
            started.set()
            await asyncio.Event().wait()  # a git step that never finishes on its own

        monkeypatch.setattr(reg_mod, "_git_fetch_branch", _hang_fetch)

    @pytest.mark.asyncio
    async def test_a_budget_cancelled_batch_reports_nothing(self, monkeypatch):
        import asyncio

        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        self._hang_on_fetch(monkeypatch, asyncio.Event())
        monkeypatch.setattr(reg_mod, "_PREWARM_BATCH_BUDGET", 0.2)

        async def _run() -> None:
            fetched = await await_until(
                asyncio.ensure_future(_prewarm_owner_tier_store_assets(reg, [_entry()]))
            )
            assert fetched == 0

        assert await self._handler_reports(_run) == []

    @pytest.mark.asyncio
    async def test_an_outer_cancelled_batch_reports_nothing(self, monkeypatch):
        import asyncio

        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        started = asyncio.Event()
        self._hang_on_fetch(monkeypatch, started)
        monkeypatch.setattr(reg_mod, "_PREWARM_BATCH_BUDGET", 3600.0)

        async def _run() -> None:
            task = asyncio.ensure_future(_prewarm_owner_tier_store_assets(reg, [_entry()]))
            await await_until(started)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await await_until(task)

        assert await self._handler_reports(_run) == []


# ---------------------------------------------------------------------------
# The row cap bounds preprocessing, not just task creation
# ---------------------------------------------------------------------------


class TestRowCapBoundsPreprocessing:
    """The ``_PREWARM_MAX_ROWS`` cap is checked at the TOP of the per-row loop, before
    any per-row I/O (the provenance/cache/backoff reads), so an oversized index cannot
    make preprocessing unbounded: once the batch holds the cap's worth of candidates,
    every remaining row is counted as overflow and skipped without a read."""

    @pytest.mark.asyncio
    async def test_at_most_the_cap_many_manifest_cache_reads_for_a_3n_index(self, monkeypatch):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        cap = 5
        monkeypatch.setattr(reg_mod, "_PREWARM_MAX_ROWS", cap)
        reads: list[str] = []
        real_read = reg_mod._read_manifest_cache

        def _spy_read(entry):
            reads.append(entry.get("name", ""))
            return real_read(entry)

        monkeypatch.setattr(reg_mod, "_read_manifest_cache", _spy_read)

        async def _fetch(entry, registry_name):
            return True

        monkeypatch.setattr(reg_mod, "_fetch_owner_tier_store_assets", _fetch)
        rows = [
            _entry(
                name=f"app-{i}", gitUrl=f"{SIBLING[:-4]}-{i}.git", repo=f"{SIBLING[:-4]}-{i}.git"
            )
            for i in range(3 * cap)
        ]

        fetched = await _prewarm_owner_tier_store_assets(reg, rows)

        assert fetched == cap
        # The rows past the cap were never read: at most `cap` per-row cache reads for
        # a 3*cap-row index. (Warm rows would read then skip, so this is <= cap; here
        # every row is cold so it is exactly cap.)
        assert len(reads) == cap, f"expected at most {cap} reads, saw {len(reads)}: {reads}"

    @pytest.mark.asyncio
    async def test_the_cap_can_be_lifted_negative_control(self, monkeypatch):
        """Negative control: raising the cap past the index size lets every row be
        read, proving the bound above observes the cap and not the fixture size."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        monkeypatch.setattr(reg_mod, "_PREWARM_MAX_ROWS", 1000)
        reads: list[str] = []
        real_read = reg_mod._read_manifest_cache

        def _spy_read(entry):
            reads.append(entry.get("name", ""))
            return real_read(entry)

        monkeypatch.setattr(reg_mod, "_read_manifest_cache", _spy_read)

        async def _fetch(entry, registry_name):
            return True

        monkeypatch.setattr(reg_mod, "_fetch_owner_tier_store_assets", _fetch)
        rows = [
            _entry(
                name=f"app-{i}", gitUrl=f"{SIBLING[:-4]}-{i}.git", repo=f"{SIBLING[:-4]}-{i}.git"
            )
            for i in range(15)
        ]

        await _prewarm_owner_tier_store_assets(reg, rows)

        assert len(reads) == 15, "with the cap lifted every row is read"


# ---------------------------------------------------------------------------
# Persist before publish, and the row cap counts rows EXAMINED
# ---------------------------------------------------------------------------


class TestPersistBeforePublish:
    """Art is published only for a manifest that reached the cache: a refused manifest
    write ends the row cold, so it never reads warm beside a bare listing."""

    @pytest.mark.asyncio
    async def test_a_refused_manifest_write_publishes_no_art(self, monkeypatch, caplog):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        _fake_fetch(monkeypatch, ART, dict(MANIFEST, screenshots=["ui/shot-1.png"]))
        monkeypatch.setattr(reg_mod, "_write_manifest_cache", lambda entry, data: False)
        published: list[Path] = []
        real_publish = reg_mod._publish_pinned_asset
        monkeypatch.setattr(
            reg_mod,
            "_publish_pinned_asset",
            lambda p, d: (published.append(p), real_publish(p, d)),
        )
        fetched = await _prewarm_owner_tier_store_assets(reg, [_entry()])
        assert fetched == 0
        assert published == []

    @pytest.mark.asyncio
    async def test_a_persisted_manifest_publishes_art_negative_control(self, monkeypatch):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        _fake_fetch(monkeypatch, ART, dict(MANIFEST, screenshots=["ui/shot-1.png"]))
        published: list[Path] = []
        real_publish = reg_mod._publish_pinned_asset
        monkeypatch.setattr(
            reg_mod,
            "_publish_pinned_asset",
            lambda p, d: (published.append(p), real_publish(p, d)),
        )
        fetched = await _prewarm_owner_tier_store_assets(reg, [_entry()])
        assert fetched == 1
        assert published


class TestTheRowCapCountsExaminedRows:
    """``_PREWARM_MAX_ROWS`` bounds the rows a batch EXAMINES, not only the cold ones it
    keeps, so an index of many warm rows cannot spend unbounded cache reads."""

    @pytest.mark.asyncio
    async def test_warm_rows_past_the_cap_are_not_read(self, monkeypatch):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        monkeypatch.setattr(reg_mod, "_PREWARM_MAX_ROWS", 2)
        looked: list[str] = []
        monkeypatch.setattr(
            reg_mod,
            "_store_assets_warm",
            lambda entry, manifest: looked.append(entry["name"]) or True,
        )
        monkeypatch.setattr(reg_mod, "_read_manifest_cache", lambda entry: {"name": entry["name"]})
        rows = [
            _entry(name=f"warm-{i}", repo=f"{SIBLING}{i}", gitUrl=f"{SIBLING}{i}") for i in range(6)
        ]
        fetched = await _prewarm_owner_tier_store_assets(reg, rows)
        assert fetched == 0
        assert len(looked) <= 2, looked

    @pytest.mark.asyncio
    async def test_within_the_cap_every_row_is_read_negative_control(self, monkeypatch):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        monkeypatch.setattr(reg_mod, "_PREWARM_MAX_ROWS", 10)
        looked: list[str] = []
        monkeypatch.setattr(
            reg_mod,
            "_store_assets_warm",
            lambda entry, manifest: looked.append(entry["name"]) or True,
        )
        monkeypatch.setattr(reg_mod, "_read_manifest_cache", lambda entry: {"name": entry["name"]})
        rows = [
            _entry(name=f"warm-{i}", repo=f"{SIBLING}{i}", gitUrl=f"{SIBLING}{i}") for i in range(6)
        ]
        await _prewarm_owner_tier_store_assets(reg, rows)
        assert len(looked) == 6
