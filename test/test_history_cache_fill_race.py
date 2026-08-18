"""Preserved-mtime cache-fill races in ConversationLog's mtime-keyed memos.

The mtime-keyed caches assume "same mtime == same content". Housekeeping
rewrites break that assumption on purpose: compaction, rotation, metadata
edits and consolidation bookkeeping restore the pre-write mtime
(``_restore_mtime``) so they do not reorder ``list_sessions``. A cache FILL
that stats the file before such a rewrite and publishes after its
``_invalidate_cache`` therefore parks pre-rewrite data under an mtime the
file still has — nothing can ever detect the staleness, so it is served for
the life of the process.

The race tests here inject the rewrite at the exact moment the fill
publishes (a proxy around the cache runs it immediately before delegating
the first store), which is inside the fill's stat → read → publish window by
construction. Note the technique's reach: the proxy fires strictly between
``_publish_if_current``'s generation pre-check and its store, so it
exercises the store-after-full-invalidation and bump-between-check-and-store
arms of the protocol. The remaining arm — a store completing before the
invalidation's bump, which is what makes bump-BEFORE-pop ordering in
``_invalidate_cache`` load-bearing — is unreachable this way and is pinned
separately by the direct ordering test at the bottom; do not treat that
test as redundant with the race tests. Each assertion is on the NEXT read:
it must observe the post-rewrite content, i.e. the racing fill must not have
survived in the cache. On a publish-anyway fill these tests go red — the
stale entry's stored mtime matches the restored file mtime, so every later
read is a cache hit on pre-rewrite data.

``_msg_cache`` (``_read_messages``) is deliberately not covered here: its
fill-vs-rewrite ordering is owned by a separate serialization mechanism.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from kiro_crew.history import ConversationLog


class _RewriteOnFirstStore:
    """Cache proxy that runs *hook* once, just before the first ``__setitem__``.

    Models a housekeeping rewrite landing between a fill's read and its
    publish: by the time the store executes, the file has been rewritten, its
    mtime restored, and ``_invalidate_cache`` has run — so the value being
    stored is provably pre-rewrite. Everything else delegates to the real
    cache, including the re-entrant calls the hook itself triggers (guarded by
    ``fired`` so the rewrite runs exactly once). Tests assert ``fired`` after
    the racing fill so "the injection never ran" fails distinctly instead of
    masquerading as a stale-cache report.

    ``__setitem__`` must be defined explicitly (dunder lookup bypasses
    ``__getattr__``); ``get``/``pop``/``pop_prefix`` reach the inner cache via
    ``__getattr__``.
    """

    def __init__(self, inner: Any, hook: Callable[[], None]) -> None:
        self._inner = inner
        self._hook = hook
        self.fired = False

    def __setitem__(self, key: str, value: Any) -> None:
        if not self.fired:
            self.fired = True
            self._hook()
        self._inner[key] = value

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


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
