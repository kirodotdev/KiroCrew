"""Tests for the same-worktree index (Signal 2) and collision notify decisions."""

from __future__ import annotations

from kiro_crew.dashboard.collision_notify import (
    NotifyOnce,
    notification_body,
    samefile_should_notify,
    sameworktree_should_notify,
)
from kiro_crew.dashboard.worktree_index import WorktreeIndex

_WT = "/repo/worktree-a"


class TestWorktreeIndex:
    def test_two_sessions_sharing_tree_collide(self):
        idx = WorktreeIndex()
        idx.set_worktree("sess-a", _WT)
        idx.set_worktree("sess-b", _WT)
        assert idx.cotenants(_WT, live_sessions={"sess-a", "sess-b"}) == frozenset(
            {"sess-a", "sess-b"}
        )

    def test_single_session_is_not_a_collision(self):
        idx = WorktreeIndex()
        idx.set_worktree("sess-a", _WT)
        assert idx.cotenants(_WT, live_sessions={"sess-a"}) == frozenset()

    def test_different_trees_do_not_collide(self):
        idx = WorktreeIndex()
        idx.set_worktree("sess-a", "/repo/wt-a")
        idx.set_worktree("sess-b", "/repo/wt-b")
        # Neither tree is shared, so neither has cotenants.
        assert idx.cotenants("/repo/wt-a", live_sessions={"sess-a", "sess-b"}) == frozenset()
        assert idx.cotenants("/repo/wt-b", live_sessions={"sess-a", "sess-b"}) == frozenset()

    def test_closed_session_not_counted(self):
        idx = WorktreeIndex()
        idx.set_worktree("sess-a", _WT)
        idx.set_worktree("sess-b", _WT)
        # sess-b closed -> only a live -> no collision.
        assert idx.cotenants(_WT, live_sessions={"sess-a"}) == frozenset()

    def test_empty_root_drops_session(self):
        idx = WorktreeIndex()
        idx.set_worktree("sess-a", _WT)
        idx.set_worktree("sess-a", "")  # non-repo cwd now
        assert idx.cotenants(_WT, live_sessions={"sess-a"}) == frozenset()

    def test_fork_pair_excluded_but_third_stranger_collides(self):
        idx = WorktreeIndex()
        idx.set_worktree("parent", _WT)
        idx.set_worktree("child", _WT)
        pair = lambda a, b: {a, b} == {"parent", "child"}  # noqa: E731
        assert (
            idx.cotenants(_WT, live_sessions={"parent", "child"}, is_fork_pair=pair) == frozenset()
        )
        idx.set_worktree("stranger", _WT)
        assert idx.cotenants(
            _WT, live_sessions={"parent", "child", "stranger"}, is_fork_pair=pair
        ) == frozenset({"parent", "child", "stranger"})

    def test_prune_drops_dead_sessions(self):
        idx = WorktreeIndex()
        idx.set_worktree("sess-a", _WT)
        idx.set_worktree("sess-b", _WT)
        idx.prune(live_sessions={"sess-a"})  # b died
        assert idx.cotenants(_WT, live_sessions={"sess-a", "sess-b"}) == frozenset()


class TestNotifyOnce:
    def test_notifies_once_per_signature(self):
        n = NotifyOnce()
        assert n.should_notify("sig-1", frozenset({"a", "b"})) is True
        assert n.should_notify("sig-1", frozenset({"a", "b"})) is False  # already sent
        assert n.should_notify("sig-2", frozenset({"a", "c"})) is True  # distinct

    def test_prune_drops_signature_when_any_participant_dead(self):
        n = NotifyOnce()
        n.should_notify("sig-ab", frozenset({"a", "b"}))
        n.should_notify("sig-ac", frozenset({"a", "c"}))
        # a stays live, b dies. sig-ab (needs a AND b) is dropped; sig-ac (a,c)
        # kept only if BOTH a,c live -> c also dies -> dropped too. Keep only a.
        n.prune(live_sessions={"a"})
        # Both signatures had a dead participant -> both dropped -> both re-notify.
        assert n.should_notify("sig-ab", frozenset({"a", "b"})) is True
        assert n.should_notify("sig-ac", frozenset({"a", "c"})) is True

    def test_prune_keeps_signature_while_all_participants_live(self):
        n = NotifyOnce()
        n.should_notify("sig-ab", frozenset({"a", "b"}))
        n.prune(live_sessions={"a", "b", "c"})  # both still live
        assert n.should_notify("sig-ab", frozenset({"a", "b"})) is False  # not re-notified

    def test_seen_store_is_count_bounded_even_without_prune(self):
        # A long-lived process whose prune is starved must not grow _seen without
        # end: at the ceiling, the oldest entry is evicted (FIFO) to admit a new
        # one, so retention is capped at _SEEN_MAX_ENTRIES.
        from kiro_crew.dashboard.collision_notify import _SEEN_MAX_ENTRIES

        n = NotifyOnce()
        for i in range(_SEEN_MAX_ENTRIES + 50):
            assert n.should_notify(f"sig-{i}", frozenset({f"s{i}"})) is True
        assert len(n._seen) == _SEEN_MAX_ENTRIES
        # the very first signatures were evicted, so they notify again
        assert n.should_notify("sig-0", frozenset({"s0"})) is True

    def test_signature_components_are_length_capped(self):
        # An agent-chosen contended path (or a huge session set) cannot make a
        # retained signature string unbounded: each component is truncated.
        from kiro_crew.dashboard.collision_notify import _SIG_COMPONENT_MAX, _sig

        huge = "x" * (_SIG_COMPONENT_MAX * 4)
        s = _sig("same-file", huge, frozenset({huge}))
        for component in s.split("|"):
            assert len(component) <= _SIG_COMPONENT_MAX


class TestSameFileNotifyDecision:
    def test_two_session_file_notifies_once(self):
        n = NotifyOnce()
        sessions = frozenset({"a", "b"})
        sig = samefile_should_notify(
            repo_id="r",
            repo_rel_path="src/app.py",
            sessions=sessions,
            notify_once=n,
        )
        assert sig is not None
        # Same pair, same file -> suppressed second time.
        assert (
            samefile_should_notify(
                repo_id="r",
                repo_rel_path="src/app.py",
                sessions=sessions,
                notify_once=n,
            )
            is None
        )

    def test_high_churn_by_session_count_never_notifies(self):
        n = NotifyOnce()
        # 3 distinct sessions on one file -> over the contested threshold.
        assert (
            samefile_should_notify(
                repo_id="r",
                repo_rel_path="src/app.py",
                sessions=frozenset({"a", "b", "c"}),
                notify_once=n,
            )
            is None
        )

    def test_high_churn_basename_never_notifies(self):
        n = NotifyOnce()
        assert (
            samefile_should_notify(
                repo_id="r",
                repo_rel_path="pkg/__init__.py",
                sessions=frozenset({"a", "b"}),
                notify_once=n,
            )
            is None
        )

    def test_new_third_session_is_a_new_signature(self):
        # A pair notifies; a genuinely different 2-set (a joins c) is distinct
        # and notifies once — but a 3-set is suppressed by churn (above). So the
        # signature-vs-churn interaction: {a,b} notifies, {a,c} notifies.
        n = NotifyOnce()
        assert samefile_should_notify(
            repo_id="r",
            repo_rel_path="f.py",
            sessions=frozenset({"a", "b"}),
            notify_once=n,
        )
        assert samefile_should_notify(
            repo_id="r",
            repo_rel_path="f.py",
            sessions=frozenset({"a", "c"}),
            notify_once=n,
        )

    def test_same_relpath_different_repos_both_notify(self):
        # The SAME repo_rel_path in two DIFFERENT repos is a distinct collision;
        # repo_id in the signature keeps the second from being deduped away.
        n = NotifyOnce()
        s = frozenset({"a", "b"})
        assert samefile_should_notify(
            repo_id="repo-1",
            repo_rel_path="x.py",
            sessions=s,
            notify_once=n,
        )
        assert samefile_should_notify(
            repo_id="repo-2",
            repo_rel_path="x.py",
            sessions=s,
            notify_once=n,
        )


class TestSameWorktreeNotifyDecision:
    def test_notifies_by_default_and_dedupes(self):
        n = NotifyOnce()
        sessions = frozenset({"a", "b"})
        assert sameworktree_should_notify(worktree_root="/wt", sessions=sessions, notify_once=n)
        assert (
            sameworktree_should_notify(worktree_root="/wt", sessions=sessions, notify_once=n)
            is None
        )

    def test_no_churn_suppression_for_worktree(self):
        # Even 3+ sessions on a shared tree still notify (no churn threshold) —
        # a live filesystem race with more sessions is MORE severe, not less.
        n = NotifyOnce()
        assert sameworktree_should_notify(
            worktree_root="/wt",
            sessions=frozenset({"a", "b", "c"}),
            notify_once=n,
        )


class TestNotificationBody:
    def test_body_does_not_leak_an_internal_repo_identity(self):
        # The scope defaults to a repo identity (host/org/repo). It must never
        # reach the body — not raw, and not as a hash of it (a hash of a
        # guessable, enumerable repo name is dictionary-attackable), because the
        # body persists to the global unscoped notifications.jsonl.
        body = notification_body(signal="same-worktree", session_count=2)
        assert "github" not in body and "secret-project" not in body
        # No path separators (no repo path / worktree path leaks).
        assert "/" not in body.replace("filesystem", "")
        assert "worktree" in body  # describes the hazard class, not a path

    def test_body_does_not_leak_a_home_dir_path_scope(self):
        # The no-origin-remote fallback makes the scope an absolute path with the
        # OS username. The body must carry nothing derived from it.
        body = notification_body(signal="same-file", session_count=2)
        assert "devuser" not in body
        assert "/home/" not in body

    def test_body_carries_no_hex_digest_token(self):
        # Belt-and-braces against a future regression that re-introduces a
        # persisted scope hash: the body must contain no long hex run that could
        # be a digest of the scope.
        import re

        body = notification_body(signal="same-worktree", session_count=2)
        assert re.search(r"[0-9a-f]{12,}", body) is None

    def test_samefile_body_wording(self):
        body = notification_body(signal="same-file", session_count=2)
        assert "same file" in body or "merge conflict" in body

    def test_body_count_matches_session_count(self):
        # The body must not hardcode "two" — a 3-session collision says "3".
        body = notification_body(signal="same-worktree", session_count=3)
        assert "3 sessions" in body
        assert "two" not in body.lower()
