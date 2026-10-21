"""The owner-tier store-art prewarm's rewarm window, clone backoff and blob-cache sweep.

The rewarm window refuses a non-positive age and is the checker's return; a clone that
fails outright writes a clone-failure record (and no manifest or unobtainable record)
that backs the row off; a path the proxy would never serve is skipped, not recorded
unobtainable; and the age-based sweep reclaims ``cache/blobs``, by descriptor walk or
by the by-name fallback.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from registry_store_art_prewarm_helpers import ART, MANIFEST, _entry, _fake_fetch, _pin_registry

from conftest import requires_symlinks
from kiro_crew.apps import registry as reg_mod
from kiro_crew.apps.registry import (
    _TRUST_OWNER,
    _blob_cache_dir,
    _copy_declared_art,
    _manifest_cache_path,
    _prewarm_owner_tier_store_assets,
    _read_manifest_cache,
)
from kiro_crew.testing.wait import finished

# ---------------------------------------------------------------------------
# The rewarm-age import guard
# ---------------------------------------------------------------------------


class TestRewarmAgeGuard:
    def test_a_non_positive_window_is_refused(self):
        from kiro_crew.apps.registry_pipeline import store_art

        # A manifest TTL that does not exceed the index TTL is the misconfiguration
        # that turns every fresh fetch into a full credentialed re-clone.
        with pytest.raises(RuntimeError, match="_REWARM_AGE must be positive"):
            store_art._check_rewarm_age(100.0, 100.0)
        with pytest.raises(RuntimeError, match="_REWARM_AGE must be positive"):
            store_art._check_rewarm_age(100.0, 200.0)

    def test_a_positive_window_is_accepted(self):
        from kiro_crew.apps.registry_pipeline import store_art

        # Negative control: a manifest TTL wider than the index TTL passes, so the
        # guard is observing the sign of the difference and not always raising.
        store_art._check_rewarm_age(200.0, 100.0)


# ---------------------------------------------------------------------------
# A clone-LEVEL failure backs the row off instead of stalling every fetch
# ---------------------------------------------------------------------------


def _failing_fetch(monkeypatch, error="git fetch failed (exit 128)"):
    """Stand in for ``_git_fetch_branch`` that always fails; returns the call log.

    A clone-level failure writes no manifest and no unobtainable record, so without
    the ``.clone-failed`` backoff the row would be re-cloned on every fresh fetch."""
    calls: list[str] = []

    async def _fetch(git_url, branch, dest, log_lines, *, clone_env, sandbox_mode, **kw):
        calls.append(git_url)
        return {"ok": False, "name": "app", "error": error}

    monkeypatch.setattr(reg_mod, "_git_fetch_branch", _fetch)
    return calls


class TestCloneFailureBackoff:
    """A clone that fails outright -- deleted repo, revoked credential, a forge that
    hangs to the batch budget -- writes no manifest and no unobtainable record, so a
    warm check that needs a manifest can never mark the row satisfied and the store
    listing that misses the index cache pays the whole clone attempt on every fresh
    index fetch, indefinitely. The prewarm records the failure beside the row's
    manifest cache and its pre-filter backs the row off for ``_CLONE_FAILURE_BACKOFF``."""

    @pytest.mark.asyncio
    async def test_a_failed_clone_records_and_the_next_prewarm_within_backoff_clones_nothing(
        self, monkeypatch
    ):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        calls = _failing_fetch(monkeypatch)
        entry = _entry()

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 0
        assert len(calls) == 1
        record = reg_mod._clone_failure_path(entry)
        assert record.parent == _manifest_cache_path(entry).parent
        recorded = json.loads(record.read_text(encoding="utf-8"))
        assert isinstance(recorded["at"], (int, float))
        assert isinstance(recorded["reason"], str) and recorded["reason"]
        # No manifest and no art landed for a clone-level failure.
        assert _read_manifest_cache(entry) is None

        # Within the backoff window the row is skipped: no second clone.
        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 0
        assert len(calls) == 1

    def test_an_unrepresentable_timestamp_reads_as_absent(self, monkeypatch):
        """A forged ``at`` too large for a float must not crash the pre-filter."""
        _pin_registry(monkeypatch, _TRUST_OWNER)
        entry = _entry()
        record = reg_mod._clone_failure_path(entry)
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text('{"at": 1' + "0" * 400 + ', "reason": "x"}', encoding="utf-8")
        assert reg_mod._read_clone_failure(entry) is None

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs mkfifo")
    def test_a_fifo_record_is_refused_without_blocking(self, monkeypatch):
        """A FIFO planted at the record name has no writer; the open must not block."""
        _pin_registry(monkeypatch, _TRUST_OWNER)
        entry = _entry()
        record = reg_mod._clone_failure_path(entry)
        record.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(record)
        result: list[Any] = []
        worker = threading.Thread(
            target=lambda: result.append(reg_mod._read_clone_failure(entry)), daemon=True
        )
        worker.start()
        worker.join(timeout=5)
        if worker.is_alive():
            # Unblock the stuck open so the session does not hang, then fail.
            fd = os.open(record, os.O_WRONLY | os.O_NONBLOCK)
            os.close(fd)
            worker.join(timeout=5)
            pytest.fail("reading a FIFO clone-failure record blocked")
        assert result == [None]

    @pytest.mark.asyncio
    async def test_the_backoff_can_fail_negative_control(self, monkeypatch):
        """Negative control: with the failure record read as always-absent (the pre-fix
        state), the same failed row IS re-cloned on the next prewarm -- proving the skip
        observes the record and not an accident of the fixture."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        calls = _failing_fetch(monkeypatch)
        entry = _entry()

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 0
        assert len(calls) == 1

        monkeypatch.setattr(reg_mod, "_read_clone_failure", lambda entry: None)
        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 0
        assert len(calls) == 2, "without the backoff the dead row is re-cloned every fetch"

    @pytest.mark.asyncio
    async def test_a_record_older_than_the_backoff_is_ignored_and_the_row_retried(
        self, monkeypatch
    ):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        calls = _failing_fetch(monkeypatch)
        entry = _entry()

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 0
        assert len(calls) == 1
        record = reg_mod._clone_failure_path(entry)
        stale = {"at": time.time() - reg_mod._CLONE_FAILURE_BACKOFF - 60, "reason": "clone failed"}
        record.write_text(json.dumps(stale), encoding="utf-8")

        # The window has lapsed, so the row is retried.
        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 0
        assert len(calls) == 2

    def test_a_record_within_the_backoff_reads_its_timestamp_and_older_reads_none(
        self, monkeypatch
    ):
        entry = _entry()
        record = reg_mod._clone_failure_path(entry)
        record.parent.mkdir(parents=True, exist_ok=True)

        fresh_at = time.time() - 60
        record.write_text(json.dumps({"at": fresh_at, "reason": "clone failed"}), encoding="utf-8")
        assert reg_mod._read_clone_failure(entry) == pytest.approx(fresh_at)

        old_at = time.time() - reg_mod._CLONE_FAILURE_BACKOFF - 1
        record.write_text(json.dumps({"at": old_at, "reason": "clone failed"}), encoding="utf-8")
        assert reg_mod._read_clone_failure(entry) is None

    @pytest.mark.asyncio
    async def test_a_malformed_record_is_ignored_and_the_row_retried(self, monkeypatch):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        calls = _failing_fetch(monkeypatch)
        entry = _entry()

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 0
        record = reg_mod._clone_failure_path(entry)

        for body in ("not json", "[1, 2]", '{"reason": "x"}', '{"at": "soon"}', '{"at": true}'):
            record.write_text(body, encoding="utf-8")
            assert reg_mod._read_clone_failure(entry) is None, body

        # A malformed record leaves the row cold, so the next prewarm re-clones it.
        record.write_text("not json", encoding="utf-8")
        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 0
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_a_successful_clone_removes_an_earlier_record(self, monkeypatch):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        entry = _entry()
        # Plant a record older than the backoff so the pre-filter lets the clone run;
        # a fresh record would (correctly) back the row off before any clone. The
        # clone then succeeds and its success path clears the stale record.
        record = reg_mod._clone_failure_path(entry)
        record.parent.mkdir(parents=True, exist_ok=True)
        stale = {"at": time.time() - reg_mod._CLONE_FAILURE_BACKOFF - 60, "reason": "clone failed"}
        record.write_text(json.dumps(stale), encoding="utf-8")
        calls = _fake_fetch(monkeypatch, ART)

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 1
        assert len(calls) == 1
        assert not record.exists(), "a reaching clone must clear the dead-row record"
        assert _read_manifest_cache(entry) == MANIFEST

    @pytest.mark.asyncio
    async def test_a_budget_cancelled_row_writes_no_record(self, monkeypatch):
        """A row cancelled at the batch budget is not a dead row: its clone never
        returned an error, so no ``.clone-failed`` record is written and the next fresh
        fetch retries it immediately rather than backing it off."""
        import asyncio

        reg = _pin_registry(monkeypatch, _TRUST_OWNER)

        async def _hanging_fetch(
            git_url, branch, dest, log_lines, *, clone_env, sandbox_mode, **kw
        ):
            await asyncio.Event().wait()  # never returns; cancelled at the budget
            return None

        monkeypatch.setattr(reg_mod, "_git_fetch_branch", _hanging_fetch)
        monkeypatch.setattr(reg_mod, "_PREWARM_BATCH_BUDGET", 0.2)
        entry = _entry()

        fetched = await finished(
            _prewarm_owner_tier_store_assets(reg, [entry]), "the prewarm returning at its budget"
        )
        assert fetched == 0
        assert not reg_mod._clone_failure_path(entry).exists()

    @pytest.mark.asyncio
    async def test_the_recorded_reason_carries_no_credential(self, monkeypatch):
        """The record's ``reason`` is a fixed class label, never raw git output: an
        owner-credentialed clone's error text can echo the expanded credential-bearing
        URL, so a secret in the error must not reach the record."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        secret = "hunter2"
        # A hostile error string carrying a credential-bearing URL.
        _failing_fetch(
            monkeypatch,
            error=f"fatal: could not read from https://user:{secret}@forge.example.com/x.git",
        )
        entry = _entry()

        assert await _prewarm_owner_tier_store_assets(reg, [entry]) == 0
        recorded = json.loads(reg_mod._clone_failure_path(entry).read_text(encoding="utf-8"))
        assert secret not in recorded["reason"]
        assert "user:" not in recorded["reason"]
        # It is a constant allowlisted class or the fixed fallback, never a slice.
        assert recorded["reason"] in {
            label for _marker, label in reg_mod._GIT_FAILURE_CLASS_LABELS
        } | {"clone failed"}

    def test_the_reason_reduces_free_text_to_a_constant(self):
        """_clone_failure_reason maps a known class to its label and anything else to
        the fixed fallback -- never a slice of the input."""
        assert reg_mod._clone_failure_reason({"error": "fatal: could not resolve host"}) == (
            "host could not be resolved"
        )
        assert reg_mod._clone_failure_reason({"error": "git fetch failed (exit 128)"}) == (
            "clone failed"
        )
        assert reg_mod._clone_failure_reason("bare string") == "clone failed"

    def test_the_record_lands_where_the_manifest_gc_sweeps(self):
        entry = _entry()
        record = reg_mod._clone_failure_path(entry)
        assert record.parent == _manifest_cache_path(entry).parent
        assert record.name.endswith(".json")
        assert record.name.endswith(reg_mod._CLONE_FAILURE_SUFFIX)
        # Distinct from the unobtainable record and from a manifest cache file.
        assert record != reg_mod._unobtainable_art_path(entry)
        assert record != _manifest_cache_path(entry)


# ---------------------------------------------------------------------------
# _REWARM_AGE is derived from the checker's return, and non-servable is not recorded
# ---------------------------------------------------------------------------


class TestRewarmAgeDerivation:
    def test_the_module_constant_is_the_checkers_return(self):
        from kiro_crew.apps.registry_pipeline import caches, store_art

        assert store_art._REWARM_AGE == store_art._check_rewarm_age(
            caches._MANIFEST_CACHE_TTL, caches._EXTERNAL_REGISTRY_CACHE_TTL
        )
        # It is a positive window, so the derivation is not a coincidence of zero.
        assert store_art._REWARM_AGE > 0

    def test_the_checker_returns_the_window(self):
        from kiro_crew.apps.registry_pipeline import store_art

        assert store_art._check_rewarm_age(200.0, 100.0) == 100.0


class TestNonServablePathIsSkippedNotRecorded:
    """A path ``_store_art_cache_path`` rejects (a non-image, or one outside the
    proxy's grammar) is skipped WITHOUT being recorded unobtainable: the proxy would
    refuse a request for it whatever the clone held, so it never makes the row cold."""

    def test_a_non_image_declared_path_is_not_recorded(self, monkeypatch, tmp_path):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        assert reg is not None
        clone = tmp_path / "clone"
        (clone / "ui").mkdir(parents=True)
        (clone / "ui" / "script.js").write_bytes(b"alert(1)")
        manifest = dict(MANIFEST, iconPath="ui/script.js", heroImage="", screenshots=[])
        entry = _entry()

        written, unobtainable = _copy_declared_art(entry, clone, manifest)

        assert written == 0
        # Non-servable: skipped, not recorded.
        assert "ui/script.js" not in unobtainable
        assert unobtainable == frozenset()


# ---------------------------------------------------------------------------
# The blob cache is swept on the manifest-GC schedule
# ---------------------------------------------------------------------------


def _blob_gc_cutoff() -> float:
    """The instant a blob-cache file's mtime must predate to be GC-eligible."""
    return (
        time.time()
        - max(reg_mod._MANIFEST_CACHE_TTL, reg_mod._EXTERNAL_REGISTRY_CACHE_TTL)
        - reg_mod._BLOB_CACHE_GC_GRACE
    )


def _make_blob_file(rel: str, *, age_past_cutoff: float) -> Path:
    """Create ``<blob root>/<rel>`` and set its mtime *age_past_cutoff* seconds either
    side of the GC cutoff (negative = older = eligible, positive = younger = safe)."""
    path = _blob_cache_dir() / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG blob")
    when = _blob_gc_cutoff() - age_past_cutoff
    os.utime(path, (when, when))
    return path


class TestBlobCacheReclaim:
    """The prewarm fills ``cache/blobs/<repo_key>/<branch>/<path>`` for every declared
    asset of every owner-tier row on each fresh fetch, and an index-driven
    ``branch``/``gitUrl`` change or a delisted row orphans those bytes forever: nothing
    but this age-based sweep reclaims ``cache/blobs``. It runs on the manifest-cache
    write path, on the same schedule as ``_gc_manifest_cache_dir``."""

    def test_an_orphan_older_than_the_grace_is_removed_and_empty_dirs_pruned(self, monkeypatch):
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)  # scopes the cache dir
        assert reg is not None
        orphan = _make_blob_file("deadkey-0000/oldbranch/ui/icon.svg", age_past_cutoff=60)
        assert orphan.is_file()

        reg_mod._gc_blob_cache_dir()

        assert not orphan.exists(), "an orphan older than the grace must be removed"
        # Its now-empty ancestor directories are pruned bottom-up, up to (not
        # including) the blob root.
        assert not (_blob_cache_dir() / "deadkey-0000").exists()
        assert _blob_cache_dir().is_dir(), "the blob root itself is never removed"

    def test_a_young_file_survives_negative_control(self, monkeypatch):
        """Negative control: the SAME sweep leaves a file younger than the cutoff in
        place, proving the removal above observes the age and is not unconditional."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        assert reg is not None
        young = _make_blob_file("livekey-1111/main/ui/icon.svg", age_past_cutoff=-60)

        reg_mod._gc_blob_cache_dir()

        assert young.is_file(), "a file younger than the cutoff must survive"

    @requires_symlinks
    def test_a_symlinked_directory_under_the_root_is_not_descended_or_deleted_through(
        self, monkeypatch, tmp_path
    ):
        """A symlinked directory planted under the blob root is neither followed (so an
        old file the link points AT is never reclaimed through it) nor removed as if it
        were an empty cache directory."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        assert reg is not None
        outside = tmp_path / "outside"
        outside.mkdir()
        target = outside / "secret.svg"
        target.write_bytes(b"\x89PNG SECRET")
        when = _blob_gc_cutoff() - 3600  # older than the cutoff
        os.utime(target, (when, when))
        _blob_cache_dir().mkdir(parents=True, exist_ok=True)
        link = _blob_cache_dir() / "linkkey"
        os.symlink(outside, link, target_is_directory=True)

        reg_mod._gc_blob_cache_dir()

        # The link is untouched and the file it points at, though old, is not reclaimed
        # through it.
        assert link.is_symlink()
        assert target.is_file(), "an old file behind a symlinked dir must not be reclaimed"

    @requires_symlinks
    def test_a_linked_blob_root_is_refused_and_nothing_in_the_target_is_reclaimed(
        self, monkeypatch, tmp_path
    ):
        """F1: the blob root ITSELF is swapped for a symlink to an outside directory
        holding an old file. The sweep must refuse the linked root (open O_NOFOLLOW /
        lstat-refuse) and reclaim nothing in the link target, rather than resolving the
        link and deleting the target's aged files."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        assert reg is not None
        outside = tmp_path / "outside"
        outside.mkdir()
        victim = outside / "old.svg"
        victim.write_bytes(b"\x89PNG VICTIM")
        when = _blob_gc_cutoff() - 3600  # older than the cutoff -> would be eligible
        os.utime(victim, (when, when))
        # Plant cache/blobs as a symlink to the outside dir (the parent exists, the
        # root does not, so the sweep meets a linked root).
        blob_root = _blob_cache_dir()
        blob_root.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(outside, blob_root, target_is_directory=True)

        reg_mod._gc_blob_cache_dir()

        assert victim.is_file(), "a linked blob root must not let the sweep reclaim its target"
        assert blob_root.is_symlink(), "the linked root is left in place, not torn down"

    def test_the_linked_root_refusal_is_load_bearing_negative_control(self, monkeypatch):
        """Negative control: the SAME aged file under a REAL blob root IS reclaimed, so
        the survival above is the linked-root refusal, not the file being safe by age."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        assert reg is not None
        victim = _make_blob_file("realkey-3333/main/old.svg", age_past_cutoff=3600)
        assert victim.is_file()

        reg_mod._gc_blob_cache_dir()

        assert not victim.exists(), "under a real root the same aged file is reclaimed"

    def test_the_sweep_runs_when_the_manifest_cache_is_written(self, monkeypatch):
        """The reclaim is wired to the manifest-cache write path, so a plain
        ``_write_manifest_cache`` sweeps the blob cache too."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        assert reg is not None
        orphan = _make_blob_file("gonekey-2222/dev/ui/hero.png", age_past_cutoff=120)

        reg_mod._write_manifest_cache(_entry(name="unrelated"), {"name": "unrelated"})

        assert not orphan.exists(), "writing a manifest must sweep the blob cache"

    def test_a_bounded_call_stops_after_the_entry_cap(self, monkeypatch):
        """Every entry the sweep examines -- directory OR file -- debits
        ``_BLOB_CACHE_GC_MAX_ENTRIES`` BEFORE it is dispatched on type, so one call
        cannot walk an unbounded tree and a directory with more entries than the
        remaining budget is not fully listed. The remainder drains on the next write."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        assert reg is not None
        # Twenty aged files in ONE directory, reached at depth two
        # (<root>/k/main/). With a small cap the sweep removes only as many as the
        # budget allows after the two directory entries above them are counted, so
        # some survive the first call.
        monkeypatch.setattr(reg_mod, "_BLOB_CACHE_GC_MAX_ENTRIES", 6)
        files = [_make_blob_file(f"k-0000/main/f{i}.png", age_past_cutoff=60) for i in range(20)]

        reg_mod._gc_blob_cache_dir()
        removed = sum(1 for f in files if not f.exists())
        assert 0 < removed < 20, f"a bounded call must sweep some but not all: {removed}"
        assert removed <= reg_mod._BLOB_CACHE_GC_MAX_ENTRIES, "never more than the entry cap"

        # Successive calls drain the rest.
        for _ in range(20):
            if all(not f.exists() for f in files):
                break
            reg_mod._gc_blob_cache_dir()
        assert all(not f.exists() for f in files), "the rest drains over later calls"

    def test_a_streamed_directory_is_not_fully_materialised(self, monkeypatch):
        """The sweep consumes the ``os.scandir`` iterator lazily and stops at the
        budget, so it never touches more entries than the cap even in a directory with
        far more children than that -- proven by counting the ``DirEntry`` objects the
        walk pulls from the iterator."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        assert reg is not None
        monkeypatch.setattr(reg_mod, "_BLOB_CACHE_GC_MAX_ENTRIES", 4)
        # Fifty aged files directly under one key directory; the root holds just that
        # one directory entry, so the budget is spent inside the crowded directory.
        for i in range(50):
            _make_blob_file(f"k-0000/f{i}.png", age_past_cutoff=60)

        touched = 0
        real_scandir = os.scandir

        class _CountingScandir:
            def __init__(self, it):
                self._it = it

            def __iter__(self):
                return self

            def __next__(self):
                nonlocal touched
                entry = next(self._it)
                touched += 1
                return entry

            def close(self):
                self._it.close()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self._it.close()

        def _spy_scandir(arg):
            return _CountingScandir(real_scandir(arg))

        monkeypatch.setattr(os, "scandir", _spy_scandir)
        reg_mod._gc_blob_cache_dir()

        # The crowded directory of 50 was never fully listed: the walk pulled far
        # fewer than its 50 children before the budget stopped it. (The ``for`` loop
        # pulls one entry past the budget before the in-body check fires, so ``touched``
        # sits a hair above the cap, never near the full listing.)
        assert touched < 50, touched
        assert touched <= reg_mod._BLOB_CACHE_GC_MAX_ENTRIES + 2, touched

    def test_a_chain_deeper_than_the_ceiling_is_left_alone(self, monkeypatch, caplog):
        """A subtree nested deeper than ``_BLOB_CACHE_GC_MAX_DEPTH`` is left in place
        (logged once) rather than recursed into without bound, so an agent-writable
        tree cannot drive the sweep into unbounded recursion."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        assert reg is not None
        monkeypatch.setattr(reg_mod, "_BLOB_CACHE_GC_MAX_DEPTH", 3)
        reg_mod._BLOB_CACHE_GC_DEPTH_LOGGED.discard(True)
        # A file six directories below the root, past the depth-3 ceiling.
        deep = _make_blob_file("a/b/c/d/e/deep.png", age_past_cutoff=3600)
        assert deep.is_file()

        with caplog.at_level("WARNING", logger="kiro_crew.apps.registry"):
            reg_mod._gc_blob_cache_dir()

        assert deep.is_file(), "a file past the depth ceiling must not be reclaimed"
        assert any("nested deeper" in r.getMessage() for r in caplog.records)

    def test_the_depth_ceiling_can_be_lifted_negative_control(self, monkeypatch):
        """Negative control: the SAME deep aged file IS reclaimed once the ceiling is
        raised above its depth, proving the survival above is the depth ceiling and not
        the file being safe by age."""
        reg = _pin_registry(monkeypatch, _TRUST_OWNER)
        assert reg is not None
        monkeypatch.setattr(reg_mod, "_BLOB_CACHE_GC_MAX_DEPTH", 16)
        deep = _make_blob_file("a/b/c/d/e/deep.png", age_past_cutoff=3600)

        reg_mod._gc_blob_cache_dir()

        assert not deep.exists(), "within the depth ceiling the same aged file is reclaimed"


class TestBlobCacheLstatSweep:
    """``_gc_blob_cache_sweep_lstat`` is the by-name sweep a platform that cannot pin
    a directory fd (Windows) falls back to. It is driven here directly, so its rules
    hold on every runner: only aged regular files go, a link is never followed or
    removed, an emptied subdirectory is pruned, and the entry budget and depth
    ceiling both stop the walk."""

    @staticmethod
    def _age(path: Path, seconds: float = 3600) -> None:
        old = time.time() - seconds
        os.utime(path, (old, old))

    def test_aged_files_go_and_fresh_files_stay(self, tmp_path):
        old = tmp_path / "repo" / "main" / "old.png"
        fresh = tmp_path / "repo" / "main" / "fresh.png"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"old")
        fresh.write_bytes(b"fresh")
        self._age(old)

        reg_mod._gc_blob_cache_sweep_lstat(tmp_path, time.time() - 60, [100], depth=0)

        assert not old.exists()
        assert fresh.is_file()

    def test_an_emptied_subdirectory_is_pruned(self, tmp_path):
        old = tmp_path / "repo" / "main" / "old.png"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"old")
        self._age(old)

        reg_mod._gc_blob_cache_sweep_lstat(tmp_path, time.time() - 60, [100], depth=0)

        assert not (tmp_path / "repo").exists()
        assert tmp_path.is_dir()

    @requires_symlinks
    def test_a_link_is_never_followed_or_removed(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        target = outside / "victim.png"
        target.write_bytes(b"keep")
        self._age(target)
        root = tmp_path / "blobs"
        root.mkdir()
        (root / "dir-link").symlink_to(outside, target_is_directory=True)
        (root / "file-link.png").symlink_to(target)

        reg_mod._gc_blob_cache_sweep_lstat(root, time.time() - 60, [100], depth=0)

        assert target.is_file(), "a file reached through a link must survive"
        assert (root / "dir-link").is_symlink()
        assert (root / "file-link.png").is_symlink()

    def test_a_non_regular_entry_is_left_alone(self, tmp_path):
        if not hasattr(os, "mkfifo"):
            pytest.skip("needs a FIFO to stand in for a special file")
        fifo = tmp_path / "pipe"
        os.mkfifo(fifo)
        self._age(fifo)

        reg_mod._gc_blob_cache_sweep_lstat(tmp_path, time.time() - 60, [100], depth=0)

        assert fifo.exists()

    def test_the_entry_budget_stops_the_walk(self, tmp_path):
        for i in range(5):
            aged = tmp_path / f"f{i}.png"
            aged.write_bytes(b"x")
            self._age(aged)
        budget = [2]

        reg_mod._gc_blob_cache_sweep_lstat(tmp_path, time.time() - 60, budget, depth=0)

        assert budget == [0]
        assert len(list(tmp_path.iterdir())) == 3

    def test_a_subtree_past_the_depth_ceiling_is_left(self, monkeypatch, tmp_path):
        monkeypatch.setattr(reg_mod, "_BLOB_CACHE_GC_MAX_DEPTH", 2)
        reg_mod._BLOB_CACHE_GC_DEPTH_LOGGED.discard(True)
        deep = tmp_path / "a" / "b" / "deep.png"
        deep.parent.mkdir(parents=True)
        deep.write_bytes(b"x")
        self._age(deep)

        reg_mod._gc_blob_cache_sweep_lstat(tmp_path, time.time() - 60, [100], depth=0)

        assert deep.is_file()

    def test_a_missing_directory_is_a_no_op(self, tmp_path):
        reg_mod._gc_blob_cache_sweep_lstat(tmp_path / "absent", time.time() - 60, [100], depth=0)
