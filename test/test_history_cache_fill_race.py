"""Regression tests for the unlocked ``_msg_cache`` fill's generation guard.

The mtime guard cannot protect a cache FILL against housekeeping rewrites that
restore the pre-write mtime (``_restore_mtime``): a fill spanning one would
park pre-rewrite messages under an mtime the file still has, undetectably. The
locked fill path orders itself against writers via ``_file_lock``; the UNLOCKED
fallback — taken exactly while a writer holds that lock — instead publishes
through a per-key invalidation generation, snapshotted before the fill's stat
and verified around the publish. An invalidation-free fill is kept (sparing the
next reader a full re-parse), while one racing a preserved-mtime rewrite is
discarded so the next read re-parses the file and returns the post-rewrite
messages.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

import kiro_crew.history as history_mod
from kiro_crew.history import ConversationLog


def _line(role: str, content: str) -> str:
    return json.dumps({"role": role, "content": content}) + "\n"


def _preserved_mtime_rewrite(log: ConversationLog, key: str, content: str) -> None:
    """Rewrite *key*'s transcript in place, restoring the pre-write mtime.

    Models the housekeeping rewrites (compaction / rotation / consolidation
    marks) that deliberately keep the file's mtime: write new bytes, put the
    old mtime back, then invalidate — the generation bump inside
    ``_invalidate_cache`` is the only signal a racing fill can see.
    """
    path = log._path(key)
    st = path.stat()
    path.write_text(content, encoding="utf-8")
    os.utime(path, (st.st_atime, st.st_mtime))
    log._invalidate_cache(key)


class _RewriteOnFirstStore:
    """``_msg_cache`` stand-in that runs *hook* once, just before the first store.

    Deterministically lands a rewrite inside the fill's read → publish window:
    by the time the store reaches this wrapper the fill has already read the
    pre-rewrite bytes and passed its generation pre-check, so the caller's
    post-store re-check is the only thing left that can catch the invalidation
    the hook performs.
    """

    def __init__(self, inner: Any, hook: Callable[[], None]) -> None:
        self._inner = inner
        self._hook: Callable[[], None] | None = hook
        #: True once the hook has run — asserted by tests so "the injection
        #: never ran" fails distinctly instead of masquerading as a
        #: stale-cache report.
        self.fired = False

    def __setitem__(self, key: str, value: Any) -> None:
        hook, self._hook = self._hook, None
        if hook is not None:
            self.fired = True
            hook()
        self._inner[key] = value

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _hold_lock(lock: threading.RLock) -> tuple[threading.Thread, threading.Event]:
    """Hold *lock* from a foreign thread until the returned event is set."""
    acquired, release = threading.Event(), threading.Event()

    def run() -> None:
        with lock:
            acquired.set()
            # Bounded even if the test body raises before releasing, so a
            # failure can never strand this thread holding the lock.
            release.wait(5)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert acquired.wait(5)
    return t, release


def _hold_writer_lock(log: ConversationLog, key: str) -> tuple[threading.Thread, threading.Event]:
    """Hold the FULL writer lock (``_locked``: RLock + cross-process flock)
    from a foreign thread until the returned event is set.

    Models a real local writer mid-mutation: readers degrade to the unlocked
    fill (the RLock is busy) while the flock-hold witness proves external
    processes are locked out — the state in which the unlocked fill is allowed
    to publish. A bare RLock hold (``_hold_lock``) models the OTHER state, a
    local writer still waiting on an external process's flock, in which the
    fill must refuse to publish.
    """
    acquired, release = threading.Event(), threading.Event()

    def run() -> None:
        with log._locked(key):
            acquired.set()
            release.wait(5)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert acquired.wait(5)
    return t, release


class TestUnlockedFillGenerationGuard:
    def test_fill_racing_preserved_mtime_rewrite_is_discarded(self, tmp_path: Path) -> None:
        """A preserved-mtime rewrite held across an unlocked on-loop fill must
        not let the pre-rewrite parse survive: the next read returns the
        post-rewrite messages."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "old")
        log._invalidate_cache("k")  # force a cold fill

        # A writer holds the full per-session writer lock (flock included), so
        # the on-loop read degrades to the unlocked fill with a valid
        # flock-hold witness — the generation is what must catch the rewrite.
        holder, release = _hold_writer_lock(log, "k")

        # ...and the rewrite lands exactly inside that fill's read → publish
        # window, replacing the transcript under the same mtime.
        def rewrite() -> None:
            _preserved_mtime_rewrite(log, "k", _line("user", "new"))

        log._msg_cache = _RewriteOnFirstStore(log._msg_cache, rewrite)  # type: ignore[assignment]
        try:

            async def on_loop() -> list[dict]:
                return log._read_messages("k")

            served = asyncio.run(on_loop())
            # The racing fill parsed the pre-rewrite file; serving that one
            # read is the documented tolerance. CACHING it is the bug: the
            # rewrite restored the mtime, so nothing could ever evict it.
            assert [m["content"] for m in served] == ["old"]
            assert log._msg_cache.get("k") is None, (
                "a fill that raced a preserved-mtime rewrite was published; the "
                "entry holds pre-rewrite messages under an mtime the file still "
                "has, so every future read would serve replaced messages"
            )
        finally:
            release.set()
            holder.join(5)

        # The next read re-parses the file and sees the post-rewrite content.
        msgs = log._read_messages("k")
        assert [m["content"] for m in msgs] == ["new"]

    def test_publish_precheck_skips_store_when_generation_already_moved(
        self, tmp_path: Path
    ) -> None:
        """A generation moved BEFORE the publish attempt suppresses the store
        entirely (no transient stale entry), while the read is still served."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "old")
        log._invalidate_cache("k")
        gen = log._cache_gen("k")

        # The rewrite lands after the caller's snapshot; the fill cannot tell
        # whether its read straddled the rewrite, so it must not publish. The
        # flock is held (a valid witness), so the GENERATION clause is what
        # must refuse the store.
        _preserved_mtime_rewrite(log, "k", _line("user", "new"))

        with log._locked("k"):
            messages = log._read_messages_locked(
                "k", gen=gen, flock_witness=log._flock_hold_witness("k")
            )
        assert [m["content"] for m in messages] == ["new"]
        assert log._msg_cache.get("k") is None, (
            "a fill whose pre-stat generation snapshot no longer matches must "
            "discard itself at the publish pre-check"
        )

    def test_locked_fill_still_publishes_unconditionally(self, tmp_path: Path) -> None:
        """The locked path passes ``gen=None``: the writer lock already orders
        the fill against every rewrite, so a generation moving between the
        caller's snapshot and the store must not suppress the publish."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "hello")
        log._invalidate_cache("k")

        real_cache_fill_lock = log._cache_fill_lock

        @contextlib.contextmanager
        def bump_then_lock(key: str) -> Iterator[bool]:
            # Lands after the caller's pre-fill snapshot and before the fill:
            # a locked fill that (wrongly) compared against the snapshot would
            # now discard itself.
            log._bump_cache_gen(key, log._cache_key_identities(key))
            with real_cache_fill_lock(key) as held:
                yield held

        log._cache_fill_lock = bump_then_lock  # type: ignore[method-assign]
        assert len(log._read_messages("k")) == 1
        assert log._msg_cache.get("k") is not None, (
            "a LOCKED fill discarded itself on a moved generation; the writer "
            "lock already orders it against rewrites, so it must publish "
            "unconditionally (gen=None)"
        )


class TestGenerationIdentityClosure:
    """One session file is reachable under several cache-key spellings, and the
    writer and reader do not always use the same one — a caller may invalidate
    under the sanitized file stem while readers pass the logical session key. A
    bump under any spelling must be visible to a snapshot under any other, or
    the guard is blind to exactly the rewrite class it exists to catch."""

    def test_legacy_stem_bump_visible_to_canonical_reader(self, tmp_path: Path) -> None:
        log = ConversationLog(base_dir=tmp_path)
        key = "slack:1234567890.123456"
        gen = log._cache_gen(key)
        # Rotation derives its invalidation key from the file name; a legacy
        # Slack transcript's stem is the bare thread_ts.
        log._invalidate_cache("1234567890.123456")
        assert log._cache_gen(key) != gen

    def test_canonical_key_bump_visible_to_legacy_stem_reader(self, tmp_path: Path) -> None:
        log = ConversationLog(base_dir=tmp_path)
        gen = log._cache_gen("1234567890.123456")
        log._invalidate_cache("slack:1234567890.123456")
        assert log._cache_gen("1234567890.123456") != gen

    def test_logical_key_and_sanitized_stem_share_a_counter(self, tmp_path: Path) -> None:
        log = ConversationLog(base_dir=tmp_path)
        key = "channel:general"
        stem = log._path(key).stem
        assert stem != key  # the sanitization is what creates the split
        gen = log._cache_gen(stem)
        log._invalidate_cache(key)
        assert log._cache_gen(stem) != gen


class TestRotationEvictsThePublishedFill:
    def test_fill_published_inside_the_append_rotate_window_is_evicted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rotation restores the pre-append mtime, and its invalidation runs
        AFTER an unlocked fill's post-check — too late for the generation to
        catch. The identity-wide pops under the LOGICAL key are what evict the
        published entry; invalidating under the sanitized ``path.stem`` alone
        would miss it and serve the pre-rotation parse for the process
        lifetime."""
        log = ConversationLog(base_dir=tmp_path)
        key = "dashboard:abc"  # namespaced: logical spelling != path.stem
        for i in range(40):
            log.append(key, "user", f"m{i:02d}")

        served: list[list[dict]] = []
        real_rotate = log._maybe_rotate

        def rotate_with_reader(path: Path, k: str) -> None:
            # The append's own invalidation has already run; rotation has not.
            # An on-loop reader released here fills UNLOCKED (this thread holds
            # the writer RLock) across an invalidation-free window, so it
            # publishes the pre-rotation parse.
            def read_on_loop() -> None:
                async def go() -> list[dict]:
                    return log._read_messages(key)

                served.append(asyncio.run(go()))

            t = threading.Thread(target=read_on_loop, daemon=True)
            t.start()
            t.join(10)
            assert log._msg_cache.get(key) is not None, (
                "premise broken: the unlocked fill inside the rotate window " "did not publish"
            )
            real_rotate(path, k)

        log._maybe_rotate = rotate_with_reader  # type: ignore[method-assign]
        # Make the NEXT append blow the byte budget so it rotates.
        monkeypatch.setattr(history_mod, "_SESSION_MAX_BYTES", log._path(key).stat().st_size)
        log.append(key, "user", "m40")

        assert len(served) == 1 and len(served[0]) == 41
        assert log._msg_cache.get(key) is None, (
            "rotation could not evict the fill published inside its window; "
            "the entry holds all 41 pre-rotation messages under the restored "
            "mtime, so every future read would serve rotated-away messages"
        )
        after = log._read_messages(key)
        assert 0 < len(after) < 41
        # The retained tail is the newest messages, rotation's contract.
        assert [m["content"] for m in after] == [
            f"m{i:02d}" if i < 40 else "m40" for i in range(41 - len(after), 41)
        ]


class TestGenerationIsProcessWide:
    """The lock whose contention forces a reader onto the unlocked fill is
    class-level (``_file_locks``), so the writer holding it may live on a
    DIFFERENT ``ConversationLog`` instance over the same directory. The
    generation table must have the same scope, or that writer's bump is
    invisible to the reader's snapshot."""

    def test_cross_instance_invalidation_moves_the_generation(self, tmp_path: Path) -> None:
        reader = ConversationLog(base_dir=tmp_path)
        writer = ConversationLog(base_dir=tmp_path)
        gen = reader._cache_gen("k")
        writer._invalidate_cache("k")
        assert reader._cache_gen("k") != gen

    def test_distinct_base_dirs_do_not_share_counters(self, tmp_path: Path) -> None:
        one = ConversationLog(base_dir=tmp_path / "one")
        other = ConversationLog(base_dir=tmp_path / "other")
        gen = one._cache_gen("k")
        other._invalidate_cache("k")
        assert one._cache_gen("k") == gen

    def test_fill_racing_cross_instance_rewrite_is_discarded(self, tmp_path: Path) -> None:
        reader = ConversationLog(base_dir=tmp_path)
        writer = ConversationLog(base_dir=tmp_path)
        reader.append("k", "user", "old")
        reader._invalidate_cache("k")
        # The premise: both instances share one per-path lock, so the writer
        # can force the reader onto the unlocked fill at all.
        assert reader._file_lock("k") is writer._file_lock("k")

        holder, release = _hold_writer_lock(reader, "k")

        def rewrite_via_writer() -> None:
            _preserved_mtime_rewrite(writer, "k", _line("user", "new"))

        reader._msg_cache = _RewriteOnFirstStore(  # type: ignore[assignment]
            reader._msg_cache, rewrite_via_writer
        )
        try:

            async def on_loop() -> list[dict]:
                return reader._read_messages("k")

            served = asyncio.run(on_loop())
            assert [m["content"] for m in served] == ["old"]
            assert reader._msg_cache.get("k") is None, (
                "a rewrite performed through a SECOND ConversationLog instance "
                "was invisible to the reader's generation snapshot; the stale "
                "parse survived under the restored mtime"
            )
        finally:
            release.set()
            holder.join(5)
        assert [m["content"] for m in reader._read_messages("k")] == ["new"]

    def test_entry_published_before_a_cross_instance_rewrite_is_unhit(self, tmp_path: Path) -> None:
        """An already-published entry cannot be popped by another instance's
        ``_invalidate_cache`` (pops are instance-local), so the warm HIT must
        consult the process-wide generation: the entry records the generation
        it was stored under, and the cross-instance bump makes it miss."""
        reader = ConversationLog(base_dir=tmp_path)
        writer = ConversationLog(base_dir=tmp_path)
        reader.append("k", "user", "old")
        reader._invalidate_cache("k")
        assert [m["content"] for m in reader._read_messages("k")] == ["old"]
        assert reader._msg_cache.get("k") is not None  # premise: entry published

        _preserved_mtime_rewrite(writer, "k", _line("user", "new"))

        assert [m["content"] for m in reader._read_messages("k")] == ["new"], (
            "the reader's warm hit served an entry that predates a preserved-"
            "mtime rewrite performed through a second ConversationLog "
            "instance; mtime matches and the pop never reached this cache, so "
            "only the per-entry generation check can unhit it"
        )

    def test_unlocked_publish_then_late_cross_instance_invalidation_is_unhit(
        self, tmp_path: Path
    ) -> None:
        """The writer-holds-lock variant: the reader publishes an unlocked fill
        (valid at publish time), and the lock-holding writer's rewrite +
        invalidation land only AFTER the reader's post-check — too late for
        the fill guard, unreachable by the writer's pops. The next read must
        still see the rewrite via the per-entry generation."""
        reader = ConversationLog(base_dir=tmp_path)
        writer = ConversationLog(base_dir=tmp_path)
        reader.append("k", "user", "old")
        reader._invalidate_cache("k")

        holder, release = _hold_writer_lock(reader, "k")
        try:

            async def on_loop() -> list[dict]:
                return reader._read_messages("k")

            assert [m["content"] for m in asyncio.run(on_loop())] == ["old"]
            assert reader._msg_cache.get("k") is not None  # published, validly
            # The lock holder's rewrite lands strictly after the fill completed.
            _preserved_mtime_rewrite(writer, "k", _line("user", "new"))
        finally:
            release.set()
            holder.join(5)

        assert [m["content"] for m in reader._read_messages("k")] == ["new"], (
            "an unlocked fill published before the lock-holding writer's "
            "rewrite stayed hittable afterwards; the pop cannot cross "
            "instances, so the warm path must reject the entry on its "
            "recorded generation"
        )

    def test_title_fallback_ignores_an_entry_predating_a_cross_instance_rewrite(
        self, tmp_path: Path
    ) -> None:
        """``list_sessions``' title fallback reads ``_msg_cache`` under the
        file stem; its hit must consult the generation too, or a cross-
        instance preserved-mtime rewrite leaves the session titled by a first
        user message the transcript no longer contains."""
        reader = ConversationLog(base_dir=tmp_path)
        writer = ConversationLog(base_dir=tmp_path)
        key = "dashboard:title-probe"
        reader.append(key, "user", "old title text")
        stem = reader._path(key).stem
        # Warm the cache under the STEM spelling — the one the title fallback
        # reads — then rewrite through the second instance.
        assert [m["content"] for m in reader._read_messages(stem)] == ["old title text"]
        assert reader._msg_cache.get(stem) is not None  # premise: stem-keyed hit exists

        _preserved_mtime_rewrite(writer, key, _line("user", "new title text"))

        (row,) = [r for r in reader.list_sessions() if r["key"] == stem]
        assert row["title"] == "new title text", (
            "list_sessions' title fallback served a stem-keyed entry that "
            "predates a cross-instance preserved-mtime rewrite; only the "
            "generation clause on that hit can reject it"
        )


class TestPopWidthMatchesBumpWidth:
    def test_preserved_mtime_rewrite_invalidates_the_stem_keyed_search_fold(
        self, tmp_path: Path
    ) -> None:
        """``search_sessions`` keys its fold by ``path.stem`` (list_sessions'
        ``meta["key"]``) while writers invalidate under the LOGICAL key. The
        identity-wide pops are what connect them; without them a rewrite that
        restores the mtime leaves the fold matching text the file no longer
        has."""
        log = ConversationLog(base_dir=tmp_path)
        key = "dashboard:probe"
        log.append(key, "user", "SECRETNEEDLE alpha")
        log.append(key, "user", "keepme beta")
        assert [h["key"] for h in log.search_sessions("SECRETNEEDLE")] == [log._path(key).stem]

        before = log._path(key).stat().st_mtime
        log.rewrite_session(
            key, [m for m in log.read_messages(key) if "SECRET" not in m["content"]]
        )
        assert log._path(key).stat().st_mtime == before, "premise: the rewrite preserved the mtime"

        assert log.search_sessions("SECRETNEEDLE") == [], (
            "the stem-keyed search fold survived a preserved-mtime rewrite; "
            "search still matches text the transcript no longer contains"
        )


class TestFlockHoldWitness:
    """External processes are the one writer class the in-process generation
    cannot witness: their ``_invalidate_cache`` bumps a table in THEIR
    process. What excludes them is the cross-process flock — an unlocked fill
    may publish only while this process provably held it for the whole fill
    window."""

    def test_unlocked_fill_without_the_flock_does_not_publish(self, tmp_path: Path) -> None:
        """A bare RLock hold with the flock NOT held by this process is
        exactly the state in which a local writer is still waiting on an
        external process's flock — the file is externally rewritable, so the
        fill must serve the read but refuse to publish, generation or no
        generation."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "hello")
        log._invalidate_cache("k")

        holder, release = _hold_lock(log._file_lock("k"))
        try:

            async def on_loop() -> list[dict]:
                return log._read_messages("k")

            assert len(asyncio.run(on_loop())) == 1, "the fill must still serve the read"
            assert log._msg_cache.get("k") is None, (
                "an unlocked fill published without a cross-process flock "
                "hold; an external process's preserved-mtime rewrite in that "
                "window bumps no generation in this process, so the entry "
                "would serve replaced messages for the process lifetime"
            )
        finally:
            release.set()
            holder.join(5)

    def test_witness_present_only_under_a_held_flock(self, tmp_path: Path) -> None:
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "hello")
        assert log._flock_hold_witness("k") is None

        holder, release = _hold_writer_lock(log, "k")
        try:
            assert log._flock_hold_witness("k") is not None
        finally:
            release.set()
            holder.join(5)

    def test_witness_comparison_includes_the_release_epoch(self, tmp_path: Path) -> None:
        """Equal fds at two instants cannot prove a continuous hold (a release
        and re-acquire can recycle the fd number); the epoch is the component
        that moves on every release, so the witness must carry it."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "hello")
        holder, release = _hold_writer_lock(log, "k")
        try:
            first = log._flock_hold_witness("k")
            assert first is not None
            lock_key = str(log._path("k"))
            with ConversationLog._flock_guard:
                ConversationLog._flock_epochs[lock_key] = (
                    ConversationLog._flock_epochs.get(lock_key, 0) + 1
                )
            assert log._flock_hold_witness("k") != first, (
                "the witness ignored the release epoch; a broken hold with a "
                "recycled fd would be indistinguishable from a continuous one"
            )
        finally:
            release.set()
            holder.join(5)


class _GenAtPop:
    """``_msg_cache`` stand-in recording the key's generation at each pop."""

    def __init__(self, inner: Any, gen_of: Callable[[], int]) -> None:
        self._inner = inner
        self._gen_of = gen_of
        self.gens_at_pop: list[int] = []

    def pop(self, key: str, default: Any = None) -> Any:
        self.gens_at_pop.append(self._gen_of())
        return self._inner.pop(key, default)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class TestInvalidateOrdering:
    def test_invalidate_bumps_before_it_pops(self, tmp_path: Path) -> None:
        """The bump must precede the pops: a fill storing between a pop and a
        later bump would pass its re-check and resurrect the dropped entry."""
        log = ConversationLog(base_dir=tmp_path)
        before = log._cache_gen("k")
        recorder = _GenAtPop(log._msg_cache, lambda: log._cache_gen("k"))
        log._msg_cache = recorder  # type: ignore[assignment]
        log._invalidate_cache("k")
        assert recorder.gens_at_pop, "no pop observed"
        assert all(g > before for g in recorder.gens_at_pop), (
            "_invalidate_cache popped before bumping; a concurrent fill "
            "storing in that gap passes its generation re-check and "
            "resurrects the entry the pop just removed"
        )


# ── Fill-race pins for the mtime-keyed memo caches (meta / list / recent) ──
class TestPreservedMtimeFillRace:
    def test_read_metadata_fill_discarded_after_racing_metadata_rewrite(
        self, tmp_path: Path
    ) -> None:
        """A metadata fill spanning an mtime-restoring edit must not stick."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "hello")
        log.update_metadata("k", {"title": "old"})
        log._invalidate_cache("k")  # cold cache so the read takes the fill path

        def rewrite() -> None:
            # Metadata edits restore the pre-write mtime (_restore_mtime), so
            # the racing fill's stored mtime will match the file's — only the
            # generation re-check can catch it.
            log.update_metadata("k", {"title": "new"})

        proxy = _RewriteOnFirstStore(log._meta_cache, rewrite)
        log._meta_cache = proxy  # type: ignore[assignment]

        meta, readable = log._read_metadata_status("k")
        assert readable
        assert proxy.fired, "the racing rewrite was never injected"
        # The racing fill itself may legitimately return the pre-rewrite view
        # (it was true at read time). What must NOT happen is that view being
        # memoized past the rewrite: the next read has to see the new title.
        assert log.get_metadata("k").get("title") == "new"

    def test_list_sessions_fill_discarded_after_racing_metadata_rewrite(
        self, tmp_path: Path
    ) -> None:
        """list_sessions' first-line metadata fill must not outlive a rewrite.

        Uses a punctuated logical key: ``list_sessions`` keys its fill by the
        sanitized ``path.stem`` while the racing writer invalidates under the
        logical key, so this pins the identity normalization as well as the
        publish guard.
        """
        log = ConversationLog(base_dir=tmp_path)
        key = "slack:123.456"  # sanitizes to stem "slack_123.456"
        log.append(key, "user", "hello")
        log.update_metadata(key, {"title": "old"})
        log._invalidate_cache(key)

        def rewrite() -> None:
            log.update_metadata(key, {"title": "new"})

        proxy = _RewriteOnFirstStore(log._meta_cache, rewrite)
        log._meta_cache = proxy  # type: ignore[assignment]

        log.list_sessions()  # the racing fill (publishes under the stem)
        assert proxy.fired, "the racing rewrite was never injected"
        # Both consumers of _meta_cache must observe the post-rewrite title.
        assert log.get_metadata(key).get("title") == "new"
        rows = {s["key"]: s for s in log.list_sessions()}
        assert rows["slack_123.456"]["title"] == "new"

    def test_recent_tail_fill_discarded_after_racing_session_rewrite(self, tmp_path: Path) -> None:
        """A recent() tail memo spanning a compaction rewrite must not stick.

        The worst site of the class: the memo feeds recent(), the per-turn
        model-context path, so a stale window would be injected every turn.
        """
        log = ConversationLog(base_dir=tmp_path)
        for i in range(5):
            log.append("k", "user", f"m{i}")
        log._invalidate_cache("k")  # force the tail fill (no fresh full cache)

        def rewrite() -> None:
            # rewrite_session is compaction housekeeping: it rewrites the file
            # to the given messages and restores the pre-write mtime.
            log.rewrite_session("k", [{"role": "user", "content": "rewritten"}])

        proxy = _RewriteOnFirstStore(log._recent_cache, rewrite)
        log._recent_cache = proxy  # type: ignore[assignment]

        log.recent("k", max_messages=3)  # the racing fill
        assert proxy.fired, "the racing rewrite was never injected"
        # The next call must re-read the rewritten tail, not serve the memo.
        assert log.recent("k", max_messages=3) == [{"role": "user", "content": "rewritten"}]

    def test_stem_keyed_meta_entry_invalidated_by_logical_key_write(self, tmp_path: Path) -> None:
        """No race needed: a plain stem-keyed entry must not survive a rewrite.

        ``list_sessions`` caches metadata under the sanitized ``path.stem``;
        a later mtime-restoring edit invalidates under the logical key. If the
        invalidation does not also drop the stem spelling, the stale entry
        keeps its matching mtime and ``list_sessions`` serves the old title
        for the life of the process.
        """
        log = ConversationLog(base_dir=tmp_path)
        key = "slack:123.456"
        log.append(key, "user", "hello")
        log.update_metadata(key, {"title": "old"})
        log._invalidate_cache(key)
        log.list_sessions()  # warm the stem-keyed _meta_cache entry, no race
        log.update_metadata(key, {"title": "new"})  # restores mtime
        rows = {s["key"]: s for s in log.list_sessions()}
        assert rows["slack_123.456"]["title"] == "new"

    def test_recent_fill_discarded_after_racing_legacy_rotation(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """Rotation on a legacy bare-``thread_ts`` file must reach canonical readers.

        ``_maybe_rotate`` invalidates under the key ITS caller holds, and for a
        pre-migration Slack transcript that is the bare ``thread_ts`` — a
        different spelling from the canonical ``slack:<ts>`` key readers pass.
        The identity closure must be bidirectional: the legacy-keyed writer has
        to move the canonical reader's generation, or a racing ``recent()``
        fill permanently serves the pre-rotation window.
        """
        log = ConversationLog(base_dir=tmp_path)
        bare = "123.456"
        canonical = "slack:123.456"
        # Legacy layout: the transcript lives under the bare thread_ts stem.
        for i in range(6):
            log.append(bare, "user", f"m{i}")
        assert log._path(canonical).stem == bare  # canonical resolves to the legacy file
        log._invalidate_cache(canonical)  # cold cache so recent() takes the tail fill
        # Any positive size below the file's forces the rotation to actually run.
        monkeypatch.setattr("kiro_crew.history._SESSION_MAX_BYTES", 1)

        def rotate() -> None:
            # The real rotation writer: rewrites the file, restores the
            # pre-write mtime, and invalidates under the spelling ITS caller
            # knows. Here that is the legacy bare ``thread_ts`` — a different
            # spelling from the canonical key the racing reader uses, which is
            # what the identity closure has to bridge.
            log._maybe_rotate(log._path(canonical), bare)

        proxy = _RewriteOnFirstStore(log._recent_cache, rotate)
        log._recent_cache = proxy  # type: ignore[assignment]

        log.recent(canonical, max_messages=3)  # the racing fill
        assert proxy.fired, "the racing rotation was never injected"
        # Rotation at this byte cap keeps only the newest message; a stale memo
        # would keep answering with the pre-rotation three-message window.
        assert log.recent(canonical, max_messages=3) == [{"role": "user", "content": "m5"}]

    def test_rotation_invalidates_logical_keyed_entries_on_canonical_file(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """Rotation must invalidate under the caller's logical key, not the stem.

        Cache entries live under the spelling the caller used, and the
        sanitized stem is lossy (``slack:<ts>`` cannot be recovered from
        ``slack_<ts>``), so a rotation that invalidates by file stem can never
        pop logically-keyed entries. Rotation restores the pre-write mtime, so
        a surviving entry keeps matching the file and serves the pre-rotation
        window indefinitely — this is the interleaving arm whose safety rests
        entirely on the invalidation's pop reaching the entry.
        """
        log = ConversationLog(base_dir=tmp_path)
        key = "slack:999.888"
        for i in range(6):
            log.append(key, "user", f"m{i}")
        log._invalidate_cache(key)
        # Warm the logically-keyed recent() memo at the file's current mtime.
        assert log.recent(key, max_messages=3) == [
            {"role": "user", "content": f"m{i}"} for i in (3, 4, 5)
        ]
        # Any positive size below the file's forces the rotation to actually run.
        monkeypatch.setattr("kiro_crew.history._SESSION_MAX_BYTES", 1)
        with log._locked(key):
            log._maybe_rotate(log._path(key), key)
        # Rotation at this byte cap keeps only the newest message; a surviving
        # memo would keep answering with the pre-rotation three-message window.
        assert log.recent(key, max_messages=3) == [{"role": "user", "content": "m5"}]

    def test_invalidate_bumps_generation_before_dropping_entries(self, tmp_path: Path) -> None:
        """Bump-before-pop ordering: a fill storing between the pop and a
        later bump would pass its re-check and resurrect the dropped entry, so
        the bump must already be visible when the pops run. This is the one
        protocol arm the ``_RewriteOnFirstStore`` race tests cannot reach (the
        proxy always completes the whole invalidation before the store), so
        this direct ordering test is not redundant with them."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "hello")

        observed: list[int] = []
        real_pop = log._meta_cache.pop

        def spying_pop(key: str, default: Any = None) -> Any:
            observed.append(log._cache_gen("k"))
            return real_pop(key, default)

        log._meta_cache.pop = spying_pop  # type: ignore[method-assign]
        before = log._cache_gen("k")
        log._invalidate_cache("k")
        assert observed and all(g == before + 1 for g in observed)

    def test_folded_fill_discarded_after_racing_session_rewrite(self, tmp_path: Path) -> None:
        """A search fold spanning a preserved-mtime rewrite must not stick.

        A surviving stale fold is worse than a stale preview: the fold decides
        whether a session MATCHES at all, so text removed by the rewrite would
        keep matching — and text added by it would never match — for the life
        of the process.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "the old needle")
        log._invalidate_cache("k")  # cold cache so the search takes the fold fill

        def rewrite() -> None:
            # rewrite_session is compaction housekeeping: it rewrites the file
            # to the given messages and restores the pre-write mtime, so only
            # the generation re-check can catch the racing fold.
            log.rewrite_session("k", [{"role": "user", "content": "the new needle"}])

        proxy = _RewriteOnFirstStore(log._folded_cache, rewrite)
        log._folded_cache = proxy  # type: ignore[assignment]

        log.search_sessions("needle")  # the racing fill
        assert proxy.fired, "the racing rewrite was never injected"
        # The next search must be answered from the rewritten file, not the memo.
        assert log.search_sessions("old needle") == []
        hits = log.search_sessions("new needle")
        assert len(hits) == 1

    def test_snippet_fill_discarded_after_racing_session_rewrite(self, tmp_path: Path) -> None:
        """The snippet memo (filled by the same fold) must not outlive a rewrite.

        A stale surviving entry here shows the user a preview line quoting text
        the transcript no longer contains. The snippet store happens inside
        ``_build_folded`` BEFORE the folded store, so the proxy injects the
        rewrite at the snippet publish — strictly inside the fold's
        stat → read → publish window.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "preview of the old text")
        log._invalidate_cache("k")

        def rewrite() -> None:
            log.rewrite_session("k", [{"role": "user", "content": "preview of the new text"}])

        proxy = _RewriteOnFirstStore(log._snippet_cache, rewrite)
        log._snippet_cache = proxy  # type: ignore[assignment]

        log.search_sessions("preview")  # the racing fill (fold stores the snippet memo)
        assert proxy.fired, "the racing rewrite was never injected"
        # The next snippet must come from the rewritten file, not the memo.
        assert "new text" in log._content_snippet("k", "preview")

    def test_folded_and_snippet_hits_require_matching_generation(self, tmp_path: Path) -> None:
        """A warm fold/snippet entry must miss when only the generation moved.

        Models the cross-instance preserved-mtime rewrite: the writer's
        ``_invalidate_cache`` pops ITS instance's caches, so all this reader
        ever observes is the generation bump — the entry is still present and
        its stored mtime still matches the file. An mtime-only hit check
        serves the stale fold forever; the generation clause must force the
        re-fold.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "needle in the haystack")
        log.search_sessions("needle")  # warm both memos
        assert log._folded_cache.get("k") is not None
        stored = log._snippet_cache.get("k")
        assert stored is not None
        # The bump alone (no pop) is exactly what a cross-instance writer's
        # invalidation looks like from this instance: the process-wide table
        # moves but this instance's entries are never popped, surviving with
        # matching mtimes.
        log._bump_cache_gen("k", log._cache_key_identities("k"))
        # Pin the snippet HIT clause in isolation first: poison the memo's
        # payload keeping its (still matching) mtime and now-stale generation.
        # The fold's own re-fold would republish this memo and mask a missing
        # generation clause here, so this must be asserted before the search.
        log._snippet_cache["k"] = (stored[0], stored[1], ["poisoned"])
        snippet = log._content_snippet("k", "needle")
        assert "poisoned" not in snippet, "a stale generation must not be trusted"
        assert "needle" in snippet
        folds = 0
        real_build = log._build_folded

        def counting_build(key: str, mtime: float, gen: int) -> Any:
            nonlocal folds
            folds += 1
            return real_build(key, mtime, gen)

        log._build_folded = counting_build  # type: ignore[method-assign]
        hits = log.search_sessions("needle")
        assert len(hits) == 1
        assert folds == 1, "bumped generation must force a re-fold despite the matching mtime"
        # And the re-published entries carry the CURRENT generation, so the
        # next query is a warm hit again.
        folds = 0
        log.search_sessions("needle")
        assert folds == 0

    def test_cross_instance_preserved_mtime_rewrite_unhits_search_memos(
        self, tmp_path: Path
    ) -> None:
        """The end-to-end issue #4414 scenario, no injection required.

        A long-lived reader instance holds warm fold/snippet memos; a
        short-lived instance over the same directory performs a
        preserved-mtime rewrite. The writer's ``_invalidate_cache`` pops only
        its own instance's caches, and ``_restore_mtime`` keeps the file's
        mtime matching the reader's entries — so before the generation clause
        the reader served pre-rewrite search results and previews for the life
        of the process. The process-wide generation table is what carries the
        writer's bump across instances; the per-entry clause is what acts on
        it.
        """
        reader = ConversationLog(base_dir=tmp_path)
        writer = ConversationLog(base_dir=tmp_path)
        reader.append("k", "user", "the old needle")
        assert [h["key"] for h in reader.search_sessions("old needle")] == ["k"]  # warm memos

        writer.rewrite_session("k", [{"role": "user", "content": "the new needle"}])

        assert (
            reader.search_sessions("old needle") == []
        ), "reader must stop matching text the rewrite removed"
        hits = reader.search_sessions("new needle")
        assert [h["key"] for h in hits] == ["k"], "reader must match the rewritten text"
        assert "new needle" in reader._content_snippet(
            "k", "needle"
        ), "the preview must quote the rewritten transcript"

    def test_cross_instance_preserved_mtime_metadata_edit_unhits_meta_cache(
        self, tmp_path: Path
    ) -> None:
        """Same two-instance scenario for the metadata memo.

        ``update_metadata`` restores the pre-write mtime, and the writer's
        pops cannot reach the reader's ``_meta_cache`` — before the generation
        clause the reader served the stale title (and agent / folder / pin
        state) for the life of the process.
        """
        reader = ConversationLog(base_dir=tmp_path)
        writer = ConversationLog(base_dir=tmp_path)
        reader.append("k", "user", "hello")
        reader.update_metadata("k", {"title": "old"})
        assert reader.get_metadata("k").get("title") == "old"  # warm the memo

        writer.update_metadata("k", {"title": "new"})

        assert (
            reader.get_metadata("k").get("title") == "new"
        ), "reader must observe the other instance's preserved-mtime edit"
