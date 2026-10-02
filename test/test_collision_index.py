"""Tests for the same-file collision index (Signal 1 index math)."""

from __future__ import annotations

from kiro_crew.dashboard.collision_index import (
    RECENCY_WINDOW_SECS,
    CollisionIndex,
    FileKey,
)

_R = "git@host:org/repo.git"
_F = "src/app.py"


def _rec(idx, session, ts, *, repo=_R, path=_F):
    idx.record_edit(repo_id=repo, repo_rel_path=path, session=session, ts=ts)


class TestRetentionBounds:
    # Fixed boundary values asserted independently of the production constants:
    # importing the constant here would let these assertions stay green if a
    # limit were loosened. These literals pin the contract (GPT fork-review F3).
    _KEYS_CAP = 8192
    _PATH_CAP = 512
    _IDENT_CAP = 512

    def test_key_count_is_capped_with_fifo_eviction(self):
        from kiro_crew.dashboard.collision_index import _MAX_KEYS

        # Guard: the production constant must still equal the pinned contract.
        assert _MAX_KEYS == self._KEYS_CAP
        idx = CollisionIndex()
        now = 1000.0
        # A stream of distinct paths within the recency window would otherwise
        # grow _by_key without bound; the cap evicts the oldest key.
        for i in range(self._KEYS_CAP + 100):
            _rec(idx, "s", now, path=f"src/f{i}.py")
        assert len(idx._by_key) == self._KEYS_CAP

    def test_repo_rel_path_is_length_capped(self):
        from kiro_crew.dashboard.collision_index import _MAX_REPO_REL_PATH_LEN

        assert _MAX_REPO_REL_PATH_LEN == self._PATH_CAP
        idx = CollisionIndex()
        huge = "a/" * (self._PATH_CAP * 2)
        _rec(idx, "s", 1000.0, path=huge)
        key = next(iter(idx._by_key))
        assert len(key.repo_rel_path) <= self._PATH_CAP

    def test_repo_identifier_is_length_capped(self):
        from kiro_crew.dashboard.collision_index import _MAX_IDENTIFIER_LEN

        assert _MAX_IDENTIFIER_LEN == self._IDENT_CAP
        idx = CollisionIndex()
        huge_repo = "r" * (self._IDENT_CAP * 2)
        _rec(idx, "sess", 1000.0, repo=huge_repo)
        key = next(iter(idx._by_key))
        assert len(key.repo_id) <= self._IDENT_CAP

    def test_key_eviction_overflow_is_counted_and_drained(self):
        from kiro_crew.dashboard.collision_index import _MAX_KEYS

        idx = CollisionIndex()
        now = 1000.0
        for i in range(_MAX_KEYS + 7):
            _rec(idx, "s", now, path=f"src/f{i}.py")
        # 7 keys evicted past the ceiling; the count is drained once (not silent).
        assert idx.drain_evicted() == 7
        assert idx.drain_evicted() == 0  # reset after draining


class TestRecordAndContest:
    def test_two_distinct_live_sessions_collide(self):
        idx = CollisionIndex()
        _rec(idx, "sess-a", 1000.0)
        _rec(idx, "sess-b", 1001.0)
        hits = idx.contested_files(_R, live_sessions={"sess-a", "sess-b"}, now=1002.0)
        assert len(hits) == 1
        key, sessions = hits[0]
        assert key == FileKey(_R, _F)
        assert sessions == frozenset({"sess-a", "sess-b"})

    def test_single_session_re_editing_does_not_self_collide(self):
        idx = CollisionIndex()
        _rec(idx, "sess-a", 1000.0)
        _rec(idx, "sess-a", 1001.0)
        _rec(idx, "sess-a", 1002.0)
        assert idx.contested_files(_R, live_sessions={"sess-a"}, now=1003.0) == []

    def test_closed_session_not_counted_for_live_collision(self):
        # sess-b edited but is not live -> only sess-a is live -> no collision.
        idx = CollisionIndex()
        _rec(idx, "sess-a", 1000.0)
        _rec(idx, "sess-b", 1001.0)
        assert idx.contested_files(_R, live_sessions={"sess-a"}, now=1002.0) == []

    def test_edit_outside_window_is_ignored(self):
        idx = CollisionIndex()
        _rec(idx, "sess-a", 1000.0)
        # sess-b edited long ago, outside the 30-min window.
        _rec(idx, "sess-b", 1000.0 - RECENCY_WINDOW_SECS - 10)
        assert idx.contested_files(_R, live_sessions={"sess-a", "sess-b"}, now=1001.0) == []

    def test_empty_scope_query_returns_nothing(self):
        idx = CollisionIndex()
        _rec(idx, "sess-a", 1000.0)
        _rec(idx, "sess-b", 1001.0)
        # Querying with an empty repo_id scope is a no-op (the index contract).
        assert idx.contested_files("", live_sessions={"sess-a", "sess-b"}, now=1002.0) == []

    def test_missing_key_component_is_dropped(self):
        idx = CollisionIndex()
        _rec(idx, "sess-a", 1000.0, repo="")  # no repo_id
        _rec(idx, "sess-b", 1001.0, path="")  # no path
        assert idx.contested_files(_R, live_sessions={"sess-a", "sess-b"}, now=1002.0) == []

    def test_different_repo_rel_paths_do_not_collide(self):
        idx = CollisionIndex()
        _rec(idx, "sess-a", 1000.0, path="src/a.py")
        _rec(idx, "sess-b", 1001.0, path="src/b.py")
        assert idx.contested_files(_R, live_sessions={"sess-a", "sess-b"}, now=1002.0) == []

    def test_same_relpath_across_worktrees_collides(self):
        # Two worktrees of one repo: same repo_id + repo_rel_path, different
        # sessions. The whole point of keying on repo-relative path.
        idx = CollisionIndex()
        _rec(idx, "sess-a", 1000.0)
        _rec(idx, "sess-b", 1001.0)
        hits = idx.contested_files(_R, live_sessions={"sess-a", "sess-b"}, now=1002.0)
        assert len(hits) == 1

    def test_other_repos_not_returned(self):
        idx = CollisionIndex()
        _rec(idx, "sess-a", 1000.0, repo="git@host:org/repo-1.git")
        _rec(idx, "sess-b", 1001.0, repo="git@host:org/repo-2.git")
        assert (
            idx.contested_files(
                "git@host:org/repo-1.git", live_sessions={"sess-a", "sess-b"}, now=1002.0
            )
            == []
        )


class TestForkExclusion:
    def test_parent_and_its_fork_do_not_collide(self):
        idx = CollisionIndex()
        _rec(idx, "parent", 1000.0)
        _rec(idx, "child", 1001.0)
        # child is a fork of parent.
        pair = lambda a, b: {a, b} == {"parent", "child"}  # noqa: E731
        assert (
            idx.contested_files(
                _R, live_sessions={"parent", "child"}, is_fork_pair=pair, now=1002.0
            )
            == []
        )

    def test_third_unrelated_session_still_collides_with_fork_pair(self):
        idx = CollisionIndex()
        _rec(idx, "parent", 1000.0)
        _rec(idx, "child", 1001.0)
        _rec(idx, "stranger", 1002.0)
        pair = lambda a, b: {a, b} == {"parent", "child"}  # noqa: E731
        hits = idx.contested_files(
            _R, live_sessions={"parent", "child", "stranger"}, is_fork_pair=pair, now=1003.0
        )
        # stranger vs parent (and vs child) are real pairs -> collision stands.
        assert len(hits) == 1
        assert hits[0][1] == frozenset({"parent", "child", "stranger"})

    def test_all_fork_paired_triple_is_suppressed(self):
        # Three sessions where EVERY pair is a fork relationship (a chain
        # a<-b<-c all sharing lineage) -> no genuine contention -> suppressed.
        idx = CollisionIndex()
        _rec(idx, "a", 1000.0)
        _rec(idx, "b", 1001.0)
        _rec(idx, "c", 1002.0)
        all_pairs = lambda x, y: True  # noqa: E731  every pair is a fork pair
        assert (
            idx.contested_files(
                _R, live_sessions={"a", "b", "c"}, is_fork_pair=all_pairs, now=1003.0
            )
            == []
        )

    def test_fork_pair_predicate_checked_symmetrically(self):
        # The index checks each unordered pair once in sorted order; a symmetric
        # predicate must suppress regardless of which argument is "parent".
        idx = CollisionIndex()
        _rec(idx, "zeta", 1000.0)  # sorts AFTER "alpha"
        _rec(idx, "alpha", 1001.0)
        # Symmetric predicate keyed on the set, so order does not matter.
        pair = lambda a, b: {a, b} == {"alpha", "zeta"}  # noqa: E731
        assert (
            idx.contested_files(_R, live_sessions={"alpha", "zeta"}, is_fork_pair=pair, now=1002.0)
            == []
        )


class TestRowCap:
    def test_hammering_session_never_evicts_another_distinct_session(self):
        # The false-negative GPT caught: session B edits once, session A edits
        # the same file many times. With one-row-per-session, B's row is never
        # evicted, so the collision still fires (kills the newest-N-rows cap).
        idx = CollisionIndex()
        _rec(idx, "sess-b", 1000.0)
        for i in range(500):  # A hammers far past any old row cap
            _rec(idx, "sess-a", 1001.0 + i)
        hits = idx.contested_files(_R, live_sessions={"sess-a", "sess-b"}, now=1002.0 + 500)
        assert len(hits) == 1
        assert hits[0][1] == frozenset({"sess-a", "sess-b"})

    def test_one_row_per_session_regardless_of_edit_count(self):
        # Structure is bounded by distinct sessions, not edits: a session
        # editing N times leaves exactly one row (its newest ts). A lone session
        # is never a collision, however many times it edits.
        idx = CollisionIndex()
        for i in range(100):
            _rec(idx, "solo", 1000.0 + i)
        hits = idx.contested_files(_R, live_sessions={"solo"}, now=1100.0)
        assert hits == []


class TestPrune:
    def test_prune_drops_stale_rows_and_empty_keys(self):
        idx = CollisionIndex()
        _rec(idx, "sess-a", 1000.0)
        idx.prune(now=1000.0 + RECENCY_WINDOW_SECS + 10)
        # All rows stale -> key removed -> no collision possible.
        assert (
            idx.contested_files(_R, live_sessions={"sess-a"}, now=1000.0 + RECENCY_WINDOW_SECS + 11)
            == []
        )

    def test_record_prunes_touched_key(self):
        idx = CollisionIndex()
        _rec(idx, "sess-a", 1000.0)
        # A much later edit by b prunes a's stale row on the same key.
        _rec(idx, "sess-b", 1000.0 + RECENCY_WINDOW_SECS + 10)
        # a's row is now stale/pruned; only b remains -> no 2-session collision.
        assert (
            idx.contested_files(
                _R,
                live_sessions={"sess-a", "sess-b"},
                now=1000.0 + RECENCY_WINDOW_SECS + 11,
            )
            == []
        )

    def test_prune_physically_evicts_stale_rows_from_the_store(self):
        # GAP the query-result tests miss: contested_files filters stale rows at
        # query time REGARDLESS of pruning, so a prune() that stopped evicting
        # would still show empty results while _by_key grew unbounded. Assert on
        # the store itself so the memory-bounding guarantee is pinned at the unit
        # that provides it, not only via a downstream query.
        idx = CollisionIndex()
        _rec(idx, "sess-a", 1000.0)
        key = FileKey(_R, _F)
        assert idx._by_key[key]  # row present before prune
        idx.prune(now=1000.0 + RECENCY_WINDOW_SECS + 10)
        # The stale row is physically gone and the now-empty key is removed.
        assert key not in idx._by_key

    def test_record_edit_physically_evicts_a_stale_row_on_the_touched_key(self):
        # Same guarantee via the record path: a fresh edit prunes the touched
        # key's stale rows in place, so the store holds only the live row.
        idx = CollisionIndex()
        _rec(idx, "sess-a", 1000.0)
        _rec(idx, "sess-b", 1000.0 + RECENCY_WINDOW_SECS + 10)
        rows = idx._by_key[FileKey(_R, _F)]
        assert {r.session for r in rows} == {"sess-b"}  # a's stale row evicted
