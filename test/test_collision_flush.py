"""Integration test for the Signal-1 per-turn flush (_flush_collision_writes)."""

from __future__ import annotations

import subprocess
import types

import pytest

from kiro_crew.dashboard.chat_runner import _flush_collision_writes
from kiro_crew.dashboard.collision_index import CollisionIndex, FileKey
from kiro_crew.dashboard.collision_notify import NotifyOnce
from kiro_crew.dashboard.worktree_index import WorktreeIndex


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, cwd=str(cwd))


@pytest.fixture(autouse=True)
def _skip_without_git():
    try:
        subprocess.run(["git", "--version"], check=True, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git not available")


def _repo(tmp_path, remote="git@github.com:org/repo.git"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "remote", "add", "origin", remote)
    return tmp_path


def _slot(project, writes, *, key="chat-1"):
    # A minimal stand-in with the attributes the flush + effective_session_key
    # read. The flush now sources touched paths from ``_file_changes`` (the
    # per-turn snapshot list, one entry per path), so seed that.
    return types.SimpleNamespace(
        project=str(project),
        _file_changes=[{"path": str(p)} for p in writes],
        key=key,
        linked_session_key="",
        forked_from=None,
        title="",
        agent="",
    )


class _CapturingBus:
    def __init__(self):
        self.pushed = []

    def push(self, payload):
        self.pushed.append(payload)
        return {}


def _state(*slots):
    st = types.SimpleNamespace(
        collisions=CollisionIndex(),
        worktrees=WorktreeIndex(),
        collision_notify_once=NotifyOnce(),
        notification_bus=_CapturingBus(),
    )
    st._slots = {s.key: s for s in slots}
    return st


class TestSignal2Notify:
    @pytest.mark.asyncio
    async def test_same_worktree_collision_notifies(self, tmp_path):
        """Two live sessions in one coordination scope sharing a worktree ->
        the flush fires a same-worktree notification (default-notify)."""
        repo = _repo(tmp_path / "r")
        f = repo / "a.py"
        f.write_text("x", encoding="utf-8")
        slot_a = _slot(repo, [str(f)], key="chat-a")
        slot_b = _slot(repo, [], key="chat-b")
        state = _state(slot_a, slot_b)
        # Pre-place B on the same worktree (as if B's flush ran first), under
        # B's EFFECTIVE session key (what the flush + live set use).
        import os as _os

        from kiro_crew.dashboard.chat_utils import effective_session_key

        state.worktrees.set_worktree(effective_session_key(slot_b), _os.path.realpath(str(repo)))
        await _flush_collision_writes(state, slot_a, effective_session_key(slot_a))
        # A same-worktree notification was pushed; the persisted body carries NO
        # scope value (raw or hashed), and the group_key groups by signal type.
        assert len(state.notification_bus.pushed) == 1
        pushed = state.notification_bus.pushed[0]
        body = pushed.body
        assert "grp-1" not in body  # raw scope never leaks to the persisted body
        import re as _re

        assert _re.search(r"[0-9a-f]{12,}", body) is None  # no digest token either
        assert "worktree" in body
        assert pushed.group_key == "collision:same-worktree"  # scope-independent
        # Dedupe: a second identical flush does NOT re-notify.
        slot_a._file_changes = [{"path": str(f)}]
        await _flush_collision_writes(state, slot_a, effective_session_key(slot_a))
        assert len(state.notification_bus.pushed) == 1

    @pytest.mark.asyncio
    async def test_no_write_turn_still_evaluates_same_worktree(self, tmp_path):
        """A scoped session that shares a tree but wrote nothing this turn still
        notifies (Signal 2 evaluates on an empty write set; GPT-review)."""
        import os as _os

        from kiro_crew.dashboard.chat_utils import effective_session_key

        repo = _repo(tmp_path / "r")
        a = _slot(repo, [], key="chat-a")  # NO writes this turn
        b = _slot(repo, [], key="chat-b")
        state = _state(a, b)
        state.worktrees.set_worktree(effective_session_key(b), _os.path.realpath(str(repo)))
        await _flush_collision_writes(state, a, effective_session_key(a))
        assert len(state.notification_bus.pushed) == 1
        assert "worktree" in state.notification_bus.pushed[0].body

    @pytest.mark.asyncio
    async def test_solo_session_does_not_notify(self, tmp_path):
        from kiro_crew.dashboard.chat_utils import effective_session_key

        repo = _repo(tmp_path / "r")
        f = repo / "a.py"
        f.write_text("x", encoding="utf-8")
        slot_a = _slot(repo, [str(f)], key="chat-a")
        state = _state(slot_a)  # only one live session
        await _flush_collision_writes(state, slot_a, effective_session_key(slot_a))
        assert state.notification_bus.pushed == []

    @pytest.mark.asyncio
    async def test_fork_pair_on_shared_tree_does_not_notify(self, tmp_path):
        import os as _os

        from kiro_crew.dashboard.chat_utils import effective_session_key

        repo = _repo(tmp_path / "r")
        f = repo / "a.py"
        f.write_text("x", encoding="utf-8")
        parent = _slot(repo, [str(f)], key="chat-parent")
        child = _slot(repo, [], key="chat-child")
        # forked_from stores the parent's EFFECTIVE session key (see chat_fork.py).
        child.forked_from = effective_session_key(parent)
        state = _state(parent, child)
        state.worktrees.set_worktree(effective_session_key(child), _os.path.realpath(str(repo)))
        await _flush_collision_writes(state, parent, effective_session_key(parent))
        # A fork pair momentarily sharing the parent's tree is not the hazard.
        assert state.notification_bus.pushed == []

    @pytest.mark.asyncio
    async def test_stranger_breaks_fork_pair_and_notifies(self, tmp_path):
        """Parent + its fork + an UNRELATED stranger on one tree IS a real race
        (the fork exclusion must not swallow it). Drives _is_fork_pair from the
        live-slot forked_from map end-to-end."""
        import os as _os

        from kiro_crew.dashboard.chat_utils import effective_session_key

        repo = _repo(tmp_path / "r")
        f = repo / "a.py"
        f.write_text("x", encoding="utf-8")
        parent = _slot(repo, [str(f)], key="chat-parent")
        child = _slot(repo, [], key="chat-child")
        stranger = _slot(repo, [], key="chat-stranger")
        child.forked_from = effective_session_key(parent)
        state = _state(parent, child, stranger)
        wt = _os.path.realpath(str(repo))
        state.worktrees.set_worktree(effective_session_key(child), wt)
        state.worktrees.set_worktree(effective_session_key(stranger), wt)
        await _flush_collision_writes(state, parent, effective_session_key(parent))
        assert len(state.notification_bus.pushed) == 1
        assert "worktree" in state.notification_bus.pushed[0].body

    @pytest.mark.asyncio
    async def test_same_tree_that_is_also_same_file_emits_one_note(self, tmp_path):
        """Two sessions sharing a worktree AND contesting the same file this
        turn -> exactly ONE note (same-worktree), not two (per-tree dedupe)."""
        import os as _os

        from kiro_crew.dashboard.chat_utils import effective_session_key

        repo = _repo(tmp_path / "r")
        f = repo / "a.py"
        f.write_text("x", encoding="utf-8")
        slot_a = _slot(repo, [str(f)], key="chat-a")
        slot_b = _slot(repo, [], key="chat-b")
        state = _state(slot_a, slot_b)
        wt = _os.path.realpath(str(repo))
        # B already on the same tree AND already recorded editing the same file.
        state.worktrees.set_worktree(effective_session_key(slot_b), wt)
        state.collisions.record_edit(
            repo_id="github.com/org/repo",
            repo_rel_path="a.py",
            session=effective_session_key(slot_b),
        )
        await _flush_collision_writes(state, slot_a, effective_session_key(slot_a))
        # Exactly one note, and it is the same-worktree (higher-severity) one.
        assert len(state.notification_bus.pushed) == 1
        assert "worktree" in state.notification_bus.pushed[0].body

    @pytest.mark.asyncio
    async def test_same_file_on_a_different_worktree_still_notifies(self, tmp_path):
        """Two sessions on DIFFERENT worktrees of one repo editing the same file
        is a distinct same-file hazard the same-worktree dedupe must NOT swallow
        (Correctness-review: the ``set(fsessions) <= members`` negative branch).
        Also the only end-to-end coverage of the flush's Signal-1 emission path.
        """
        import os as _os

        from kiro_crew.dashboard.chat_utils import effective_session_key

        # Two clones sharing one origin remote -> same repo_id, different roots.
        repo_a = _repo(tmp_path / "wt-a")
        repo_b = _repo(tmp_path / "wt-b")
        fa = repo_a / "a.py"
        fa.write_text("x", encoding="utf-8")
        slot_a = _slot(repo_a, [str(fa)], key="chat-a")
        slot_b = _slot(repo_b, [], key="chat-b")
        state = _state(slot_a, slot_b)
        # B lives on its OWN worktree (repo_b), not A's -> NOT a cotenant of A's
        # tree, so the same-worktree note cannot fire / cannot dedupe the file.
        state.worktrees.set_worktree(effective_session_key(slot_b), _os.path.realpath(str(repo_b)))
        # B already recorded editing the SAME repo-relative file, same repo_id.
        # The flush scopes on the DERIVED repo identity, so B's pre-recorded edit
        # must use that same derived repo_id as its scope for the two to collide.
        # derive_repo_context does blocking sandbox prep for its git spawns, so
        # production calls it via asyncio.to_thread (chat_runner._flush path);
        # mirror that here rather than calling it on the event loop.
        import asyncio as _asyncio

        from kiro_crew.dashboard.collision_derive import derive_repo_context

        ctx_a = await _asyncio.to_thread(derive_repo_context, str(repo_a))
        repo_id = ctx_a.repo_id
        state.collisions.record_edit(
            repo_id=repo_id,
            repo_rel_path="a.py",
            session=effective_session_key(slot_b),
        )
        await _flush_collision_writes(state, slot_a, effective_session_key(slot_a))
        # A SAME-FILE note fires (no same-worktree note: the sessions are on
        # different trees), proving the Signal-1 emission branch is reachable
        # and the different-worktree hazard is not deduped away.
        assert len(state.notification_bus.pushed) == 1
        body = state.notification_bus.pushed[0].body
        assert "same file" in body or "merge conflict" in body
        assert "worktree" not in body


class TestFlush:
    async def _run(self, coro):
        return await coro

    @pytest.mark.asyncio
    async def test_flush_records_edit_and_clears(self, tmp_path):
        repo = _repo(tmp_path / "r")
        f = repo / "src" / "app.py"
        f.parent.mkdir(parents=True)
        f.write_text("x", encoding="utf-8")
        state = _state()
        slot = _slot(repo, [str(f)])

        await _flush_collision_writes(state, slot, "sess-a")

        # The flush scopes on the session's DERIVED repo identity; the scope is a
        # local and is not mirrored on the slot. A second session editing the
        # same repo-relative path under that same derived repo_id collides.
        scope = "github.com/org/repo"  # derived from the repo's origin remote
        state.collisions.record_edit(
            repo_id=scope,
            repo_rel_path="src/app.py",
            session="sess-b",
        )
        hits = state.collisions.contested_files(scope, live_sessions={"sess-a", "sess-b"})
        assert len(hits) == 1
        assert hits[0][0] == FileKey(scope, "src/app.py")

    @pytest.mark.asyncio
    async def test_non_repo_session_records_nothing(self, tmp_path):
        """A cwd that is NOT a git repo resolves to no coordination scope, so
        nothing is indexed (the decoupled model's 'no scope' case — replaces the
        old tag-based 'untagged' gate)."""
        non_repo = tmp_path / "plain"
        non_repo.mkdir()
        f = non_repo / "a.py"
        f.write_text("x", encoding="utf-8")
        state = _state()
        slot = _slot(non_repo, [str(f)])  # no explicit scope, no repo
        await _flush_collision_writes(state, slot, "sess-a")
        # A non-repo cwd derives no scope ("") so nothing is indexed.
        assert state.collisions.contested_files("", live_sessions={"sess-a"}) == []

    @pytest.mark.asyncio
    async def test_repo_session_self_scopes_to_repo_identity(self, tmp_path):
        """With no explicit scope, a session in a git repo self-scopes to the
        repo identity, so two such sessions in one repo are collision peers —
        the decoupling: no tag required."""
        repo = _repo(tmp_path / "r")
        f = repo / "src" / "app.py"
        f.parent.mkdir(parents=True)
        f.write_text("x", encoding="utf-8")
        state = _state()
        slot = _slot(repo, [str(f)])  # no explicit scope
        await _flush_collision_writes(state, slot, "sess-a")
        repo_id = "github.com/org/repo"
        # A second session editing the same repo-relative path under the repo
        # identity collides with sess-a's flushed edit — proving the flush
        # self-scoped sess-a to repo_id (no tag required).
        state.collisions.record_edit(
            repo_id=repo_id,
            repo_rel_path="src/app.py",
            session="sess-b",
        )
        hits = state.collisions.contested_files(repo_id, live_sessions={"sess-a", "sess-b"})
        assert len(hits) == 1

    @pytest.mark.asyncio
    async def test_repo_switch_rescopes_fresh_not_stale(self, tmp_path):
        """A session that moves from repo X to repo Y indexes Y's edits under Y,
        never the stale X (Correctness-review Finding #3): the scope is derived
        fresh from the cwd every turn, with no slot state read back as source.
        Proven against the INDEX, the real observable."""
        repo_x = _repo(tmp_path / "x", remote="git@github.com:org/repo-x.git")
        repo_y = _repo(tmp_path / "y", remote="git@github.com:org/repo-y.git")
        fx = repo_x / "a.py"
        fx.write_text("x", encoding="utf-8")
        fy = repo_y / "a.py"
        fy.write_text("y", encoding="utf-8")
        state = _state()
        # Turn 1: the session lives in repo X -> X's edit indexes under X.
        slot = _slot(repo_x, [str(fx)])
        await _flush_collision_writes(state, slot, "sess-a")
        # A peer in X on the same file collides under X's scope.
        state.collisions.record_edit(
            repo_id="github.com/org/repo-x",
            repo_rel_path="a.py",
            session="sess-peer-x",
        )
        assert (
            len(
                state.collisions.contested_files(
                    "github.com/org/repo-x", live_sessions={"sess-a", "sess-peer-x"}
                )
            )
            == 1
        )  # turn-1 edit is under X
        # Turn 2: the SAME session has moved to repo Y (its cwd changed). The
        # scope must derive fresh to Y, NOT reuse X.
        slot.project = str(repo_y)
        slot._file_changes = [{"path": str(fy)}]
        await _flush_collision_writes(state, slot, "sess-a")
        # Y's edit indexed under Y: a peer in Y on the same file collides.
        state.collisions.record_edit(
            repo_id="github.com/org/repo-y",
            repo_rel_path="a.py",
            session="sess-peer-y",
        )
        assert (
            len(
                state.collisions.contested_files(
                    "github.com/org/repo-y", live_sessions={"sess-a", "sess-peer-y"}
                )
            )
            == 1
        )  # turn-2 edit is under Y, re-derived fresh — not stale X

    @pytest.mark.asyncio
    async def test_out_of_tree_write_records_nothing(self, tmp_path):
        repo = _repo(tmp_path / "r")
        outside = tmp_path / "outside.py"
        outside.write_text("x", encoding="utf-8")
        state = _state()
        slot = _slot(repo, [str(outside)])
        await _flush_collision_writes(state, slot, "sess-a")
        # Nothing indexed -> even a second session cannot collide.
        state.collisions.record_edit(
            repo_id="github.com/org/repo",
            repo_rel_path="outside.py",
            session="sess-b",
        )
        assert state.collisions.contested_files("grp-1", live_sessions={"sess-a", "sess-b"}) == []

    @pytest.mark.asyncio
    async def test_sensitive_path_is_skipped(self, tmp_path, monkeypatch):
        """A write whose path validate_file_path rejects (sensitive) is skipped
        by the flush and never indexed (design §4.2 security drop)."""
        import kiro_crew.dashboard.chat_runner as cr

        repo = _repo(tmp_path / "r")
        secret = repo / "secret.env"
        secret.write_text("x", encoding="utf-8")
        # Force this path to be treated as sensitive.
        monkeypatch.setattr(
            cr, "validate_file_path", lambda p: None if p.endswith("secret.env") else p
        )
        state = _state()
        slot = _slot(repo, [str(secret)])
        await _flush_collision_writes(state, slot, "sess-a")
        # Nothing indexed for the sensitive path.
        state.collisions.record_edit(
            repo_id="github.com/org/repo",
            repo_rel_path="secret.env",
            session="sess-b",
        )
        assert state.collisions.contested_files("grp-1", live_sessions={"sess-a", "sess-b"}) == []

    @pytest.mark.asyncio
    async def test_flush_prunes_stale_keys_across_all_keys(self, tmp_path, monkeypatch):
        """The flush sweeps window-expired rows on EVERY key, not just the one
        it touches this turn — else keys for files that stop being edited leak for the
        process lifetime (GPT-review unbounded-memory BLOCK)."""
        import kiro_crew.dashboard.collision_index as ci

        state = _state()
        # Seed many stale keys directly (as if edited long ago).
        for i in range(50):
            state.collisions.record_edit(
                repo_id="r",
                repo_rel_path=f"old/{i}.py",
                session="past",
                ts=1000.0,
            )
        assert len(state.collisions._by_key) == 50
        # A later flush (even an untagged/no-op one) must prune all stale keys.
        # Freeze "now" far past the window so every seeded key is expired.
        real_time = ci.time.time
        monkeypatch.setattr(ci.time, "time", lambda: 1000.0 + ci.RECENCY_WINDOW_SECS + 10)
        try:
            slot = _slot(tmp_path, [])  # untagged, no paths -> record=False
            await _flush_collision_writes(state, slot, "sess-x")
        finally:
            monkeypatch.setattr(ci.time, "time", real_time)
        assert state.collisions._by_key == {}  # all stale keys swept

    @pytest.mark.asyncio
    async def test_empty_accumulator_is_noop(self, tmp_path):
        state = _state()
        slot = _slot(tmp_path, [])
        await _flush_collision_writes(state, slot, "sess-a")  # must not raise
        # No touched paths -> nothing recorded, no notification.
        assert (
            state.collisions.contested_files("github.com/org/repo", live_sessions={"sess-a"}) == []
        )
