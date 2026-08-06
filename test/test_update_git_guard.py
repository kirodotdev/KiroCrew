"""The dashboard update-check must skip non-git project dirs (cloud installs)."""

from __future__ import annotations

import asyncio

from kiro_crew.dashboard.handlers import updates


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
        # Asserted by BEHAVIOUR, not by the stamp value: `install_kind` echoes
        # `beacon.distribution()`, which is `source` in a checkout and `wheel` in an
        # installed artifact. `feed_malformed` proves the feed branch ran.
        info = updates.get_update_info()
        assert info["error"] == "feed_malformed"
        assert info["install_kind"] in ("wheel", "source")

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
        (tmp_path / ".git").write_text("gitdir: /somewhere/.git/worktrees/x\n")
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
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
        (tmp_path / ".git").mkdir()
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
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
