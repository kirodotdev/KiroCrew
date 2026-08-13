"""The dashboard update-check must skip non-git project dirs (cloud installs)."""

from __future__ import annotations

import asyncio
import subprocess

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
