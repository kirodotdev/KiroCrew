"""The dashboard update-check must skip non-git project dirs (cloud installs)."""

from __future__ import annotations

import asyncio
import json
import subprocess

import pytest

from kiro_crew.dashboard.handlers import updates


def _init_repo(path) -> None:
    """Make *path* the top level of a real git working tree.

    Detection asks git and anchors the answer to this exact directory, so a
    fabricated ``.git`` entry does not stand in for a repository.
    """
    subprocess.run(
        ["git", "init", "-q"], cwd=str(path), check=True, capture_output=True, timeout=30
    )


class TestUpdateCheckGitGuard:
    """A non-git project dir must never invoke git — it takes the feed path instead.

    The guard itself is unchanged (no "not a git repository" spam from the poller);
    what changed is where control goes afterwards. A tarball/wheel install used to
    return early and leave the cache reporting "up to date"; it now compares against
    its release-channel feed, so these tests stub that seam and assert git stayed
    out of it.
    """

    @staticmethod
    def _stub_feed(monkeypatch):
        async def _fake(url: str):
            return 200, b'{"schema": "nope"}'

        monkeypatch.setattr(updates, "_fetch_feed_bytes", _fake)

    @staticmethod
    def _assert_took_the_feed_path():
        # Asserted by BEHAVIOUR, not by the stamp value: every feed-checkable
        # shape reports one capability, and `feed_malformed` proves the feed
        # branch is the one that ran.
        info = updates.get_update_info()
        assert info["error_code"] == "feed_malformed"
        assert info["managed_by"] == "kirocrew"

    def test_skips_git_when_no_dot_git(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        self._stub_feed(monkeypatch)

        def _boom(*a, **k):  # pragma: no cover - must not be called
            raise AssertionError("git must not run without a .git dir")

        monkeypatch.setattr(updates.asyncio, "create_subprocess_exec", _boom)
        asyncio.run(updates._do_update_check())
        self._assert_took_the_feed_path()

    def test_skips_git_when_no_project_dir(self, monkeypatch):
        monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
        self._stub_feed(monkeypatch)

        def _boom(*a, **k):  # pragma: no cover
            raise AssertionError("git must not run without a project dir")

        monkeypatch.setattr(updates.asyncio, "create_subprocess_exec", _boom)
        asyncio.run(updates._do_update_check())
        self._assert_took_the_feed_path()

    def test_apply_rejects_non_git_checkout(self, monkeypatch, tmp_path):
        # POST /api/update on a tarball install must 409 with a clear
        # "redeploy" message instead of running git status/pull and failing.
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))

        def _boom(*a, **k):  # pragma: no cover - must not be called
            raise AssertionError("git must not run without a .git dir")

        monkeypatch.setattr(updates.asyncio, "create_subprocess_exec", _boom)

        class _Req:
            app = {"state": None}

        resp = asyncio.run(updates.api_update_apply(_Req()))
        assert resp.status == 409
        assert b"redeploy" in resp.body

    def test_apply_refuses_a_diverged_checkout_before_pulling(self, monkeypatch, tmp_path):
        # The render-site guards only cover clients that ran a fresh check; a
        # stale client still POSTs here, and the bare ``git pull`` would answer
        # a diverged checkout with an unrequested merge. The endpoint owns the
        # destructive action, so IT enforces the precondition: 409 with the
        # counts, and git pull must never start.
        _init_repo(tmp_path)
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(
            "kiro_crew.platform.update_capability.running_from_checkout",
            lambda root, **kw: True,
        )

        async def _no_provider():
            return None

        monkeypatch.setattr("kiro_crew.platform.update_provider.apply_policy_update", _no_provider)
        monkeypatch.setattr(updates, "update_blocked_reason", lambda url: None)

        calls: list[tuple[str, ...]] = []
        # git status --porcelain (clean), pre-guard git fetch, then rev-list.
        outputs = [(0, b""), (0, b""), (0, b"3\t219\n")]

        class _Proc:
            def __init__(self, rc: int, out: bytes) -> None:
                self.returncode = rc
                self._out = out

            async def communicate(self):
                return (self._out, b"")

        async def _fake_exec(*args, **kwargs):
            calls.append(tuple(str(a) for a in args))
            rc, out = outputs.pop(0) if outputs else (0, b"")
            return _Proc(rc, out)

        monkeypatch.setattr(updates.asyncio, "create_subprocess_exec", _fake_exec)

        class _State:
            def push_refresh(self, kind: str) -> None:
                pass

        class _Req:
            app = {"state": _State()}

        resp = asyncio.run(updates.api_update_apply(_Req()))
        assert resp.status == 409
        body = json.loads(resp.body)
        assert body["code"] == "checkout_diverged"
        assert body["commits_ahead"] == 3
        assert body["commits_behind"] == 219
        assert not any("pull" in c for c in calls)
        # The guard counted against refs it refreshed itself: stale
        # remote-tracking refs from an old fetch can report behind=0 for a
        # checkout whose remote has since moved.
        assert any("fetch" in c for c in calls)

    def test_apply_fails_closed_when_the_preguard_fetch_fails(self, monkeypatch, tmp_path):
        # An unanswerable safety question is not an answer of safe: if the
        # remote cannot be reached, the distance below would be computed from
        # stale refs, so the endpoint refuses instead of pulling blind.
        _init_repo(tmp_path)
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(
            "kiro_crew.platform.update_capability.running_from_checkout",
            lambda root, **kw: True,
        )

        async def _no_provider():
            return None

        monkeypatch.setattr("kiro_crew.platform.update_provider.apply_policy_update", _no_provider)
        monkeypatch.setattr(updates, "update_blocked_reason", lambda url: None)

        calls: list[tuple[str, ...]] = []
        outputs = [(0, b""), (1, b"")]  # clean tree, then the fetch FAILS

        class _Proc:
            def __init__(self, rc: int, out: bytes) -> None:
                self.returncode = rc
                self._out = out

            async def communicate(self):
                return (self._out, b"")

        async def _fake_exec(*args, **kwargs):
            calls.append(tuple(str(a) for a in args))
            rc, out = outputs.pop(0) if outputs else (0, b"")
            return _Proc(rc, out)

        monkeypatch.setattr(updates.asyncio, "create_subprocess_exec", _fake_exec)

        class _State:
            def push_refresh(self, kind: str) -> None:
                pass

        class _Req:
            app = {"state": _State()}

        resp = asyncio.run(updates.api_update_apply(_Req()))
        assert resp.status == 409
        assert json.loads(resp.body)["code"] == "git_fetch_failed"
        assert not any("pull" in c for c in calls)

    @pytest.mark.parametrize(
        "rev_list_result",
        [
            (128, b""),  # rev-list itself failed (e.g. the upstream ref is gone)
            (0, b"garbage\n"),  # output that is not two integer counts
        ],
        ids=["git_failed", "unparseable"],
    )
    def test_apply_fails_closed_when_the_distance_is_unreadable(
        self, monkeypatch, tmp_path, rev_list_result
    ):
        # The diverged refusal above only works if the guard can COUNT. A
        # distance it cannot read must refuse too — never read as "in sync"
        # and wave the pull through.
        _init_repo(tmp_path)
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(
            "kiro_crew.platform.update_capability.running_from_checkout",
            lambda root, **kw: True,
        )

        async def _no_provider():
            return None

        monkeypatch.setattr("kiro_crew.platform.update_provider.apply_policy_update", _no_provider)
        monkeypatch.setattr(updates, "update_blocked_reason", lambda url: None)

        calls: list[tuple[str, ...]] = []
        # git status --porcelain (clean), pre-guard git fetch, then rev-list.
        outputs = [(0, b""), (0, b""), rev_list_result]

        class _Proc:
            def __init__(self, rc: int, out: bytes) -> None:
                self.returncode = rc
                self._out = out

            async def communicate(self):
                return (self._out, b"")

        async def _fake_exec(*args, **kwargs):
            calls.append(tuple(str(a) for a in args))
            rc, out = outputs.pop(0) if outputs else (0, b"")
            return _Proc(rc, out)

        monkeypatch.setattr(updates.asyncio, "create_subprocess_exec", _fake_exec)

        class _State:
            def push_refresh(self, kind: str) -> None:
                pass

        class _Req:
            app = {"state": _State()}

        resp = asyncio.run(updates.api_update_apply(_Req()))
        assert resp.status == 409
        assert json.loads(resp.body)["code"] == "git_read_failed"
        assert not any("pull" in c for c in calls)

    def test_the_apply_pull_is_fast_forward_only(self):
        # The precondition and the pull are not one atomic operation: a remote
        # that moves in between must fail the pull, never mint an unrequested
        # merge commit into the user's branch. Pinned at the source so the
        # flag cannot be dropped in a refactor without a test going red.
        import inspect

        src = inspect.getsource(updates.api_update_apply)
        assert '"--ff-only"' in src

    def test_proceeds_when_dot_git_is_file(self, monkeypatch, tmp_path):
        # Linked git worktrees and submodules have .git as a *file* pointing at
        # the real git dir — update checks must still run there.
        _init_repo(tmp_path)
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(
            "kiro_crew.platform.update_capability.running_from_checkout",
            lambda root, **kw: True,
        )
        called = {"n": 0}

        class _Proc:
            returncode = 128

            async def communicate(self):
                return (b"", b"")

        async def _fake_exec(*a, **k):
            called["n"] += 1
            return _Proc()

        monkeypatch.setattr(updates.asyncio, "create_subprocess_exec", _fake_exec)
        asyncio.run(updates._do_update_check())
        assert called["n"] >= 1

    def test_proceeds_when_dot_git_present(self, monkeypatch, tmp_path):
        _init_repo(tmp_path)
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(
            "kiro_crew.platform.update_capability.running_from_checkout",
            lambda root, **kw: True,
        )
        called = {"n": 0}

        class _Proc:
            returncode = 128

            async def communicate(self):
                return (b"", b"fatal: not a git repository")

        async def _fake_exec(*a, **k):
            called["n"] += 1
            return _Proc()

        monkeypatch.setattr(updates.asyncio, "create_subprocess_exec", _fake_exec)
        asyncio.run(updates._do_update_check())
        assert called["n"] >= 1  # git WAS invoked when .git exists
