"""The dashboard update-check must skip non-git project dirs (cloud installs)."""

from __future__ import annotations

import asyncio

from kiro_crew.dashboard.handlers import updates


class TestUpdateCheckGitGuard:
    def test_skips_when_no_dot_git(self, monkeypatch, tmp_path):
        # A tarball-shipped cloud install has no .git — the check must return
        # early without ever invoking git (no "not a git repository" spam).
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))

        def _boom(*a, **k):  # pragma: no cover - must not be called
            raise AssertionError("git must not run without a .git dir")

        monkeypatch.setattr(updates.asyncio, "create_subprocess_exec", _boom)
        asyncio.run(updates._do_update_check())  # returns cleanly, no git call

    def test_skips_when_no_project_dir(self, monkeypatch):
        monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)

        def _boom(*a, **k):  # pragma: no cover
            raise AssertionError("git must not run without a project dir")

        monkeypatch.setattr(updates.asyncio, "create_subprocess_exec", _boom)
        asyncio.run(updates._do_update_check())

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
