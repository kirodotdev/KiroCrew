"""Same-worktree collision — Signal 2 of project coordination (Phase 1).

A *same-worktree collision* is two or more live sessions whose ``worktree_root``
(the ``git rev-parse --show-toplevel`` of their cwd) is the SAME physical tree.
This is the STRONG-WARN signal: a live filesystem race, not a merge conflict. A
concurrent session's ``git checkout`` flips the tree out from under an in-flight
edit; the file tool reports a file missing that ``ls`` just showed; uncommitted
work is clobbered. It is invisible to Signal 1 (same-file) because it happens in
the shared bytes on disk AROUND the writes, not in what was written.

Unlike Signal 1 this is NOT recency-windowed: it is about sessions that share a
tree RIGHT NOW. So the state is a simple ``session -> worktree_root`` map of each
live session's current tree; a collision is any tree held by >= 2 distinct live
sessions. Liveness is supplied by the caller at query time (the dashboard owns
the slot registry), so a closed session's stale entry is ignored and swept.

Design (peer-coordination design §4.2 Signal 2, §4.4):

* Key is ``worktree_root`` (realpath'd toplevel). Collision = two distinct live
  sessions sharing it. NOT same-branch: separate worktrees on one branch editing
  different files is harmless and fires nothing — the hazard is the shared TREE.
* ``worktree_root`` is re-derived from the session's cwd on each per-turn flush
  (the caller supplies the derived value here), OFF the loop. It is therefore
  EVENTUALLY consistent: a session contributes only after its first flush, not
  the instant its cwd changes. Eager re-derive on every confirmed ``slot.project``
  commit is a later hardening; Phase 1 uses the per-flush approximation.
* Fork pairs are excluded like Signal 1 (a fork that has not yet moved to its
  own worktree momentarily shares the parent's tree; that is not the hazard).
* This module is a pure, thread-safe leaf: it knows nothing about slots, git, or
  ``send_notification``. The notify decision (default-notify for same-worktree,
  dedupe, opaque-id body) lives in the caller.
"""

from __future__ import annotations

import threading

from kiro_crew.dashboard.collision_index import _all_fork_paired


class WorktreeIndex:
    """Live ``session -> worktree_root`` map. Thread-safe; pure set math.

    The caller updates a session's current tree on each per-turn flush (from its
    cwd, off-loop), lets a closed session's row be swept by ``prune``, and
    queries for trees co-tenanted by >= 2 distinct live sessions. Because the
    update is per-flush, co-tenancy is eventually consistent — a session appears
    only after its first turn.
    """

    def __init__(self) -> None:
        self._by_session: dict[str, str] = {}
        self._lock = threading.Lock()

    def set_worktree(self, session: str, worktree_root: str) -> None:
        """Record that ``session``'s cwd is now under ``worktree_root``. An empty
        root (non-repo cwd / derive miss) drops the session — a session not in a
        git tree cannot be in a shared-tree race."""
        if not session:
            return
        with self._lock:
            if worktree_root:
                self._by_session[session] = worktree_root
            else:
                self._by_session.pop(session, None)

    def cotenants(
        self, worktree_root: str, *, live_sessions: set[str], is_fork_pair=None
    ) -> frozenset[str]:
        """The distinct LIVE sessions currently sharing ``worktree_root``.

        Returns the co-tenant session set (>= 2 members means a collision). A
        session not in ``live_sessions`` is ignored (closed sessions do not
        race). If ``is_fork_pair`` is given and the ONLY co-tenants are a single
        fork pair, returns an empty set (a fork momentarily on the parent's tree
        is not the hazard) — same conservative rule as Signal 1.
        """
        if not worktree_root:
            return frozenset()
        with self._lock:
            members = {
                s
                for s, root in self._by_session.items()
                if root == worktree_root and s in live_sessions
            }
        if len(members) < 2:
            return frozenset()
        if is_fork_pair is not None and _all_fork_paired(members, is_fork_pair):
            return frozenset()
        return frozenset(members)

    def prune(self, *, live_sessions: set[str]) -> None:
        """Drop entries for sessions that are not live. Cheap; call periodically so
        a long-lived process does not accumulate closed-session rows."""
        with self._lock:
            for s in [s for s in self._by_session if s not in live_sessions]:
                self._by_session.pop(s, None)
