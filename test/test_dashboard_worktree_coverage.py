"""Coverage for ``dashboard/handlers/worktree.py`` refusal and cleanup branches.

``test_worktree_create.py`` already covers this module end-to-end, but every one
of its git-touching tests goes through ``_require_sandbox_exec()`` and SKIPS on a
host where the OS sandbox cannot establish isolation -- which is most CI runners.
The result is that the module's interesting halves (the config probes, the
cleanup unwind, ``_create_worktree_sync``'s eight refusal returns and the
endpoint's audit-and-refuse paths) are unmeasured there.

So nothing here runs git. The boundary is injected at exactly two seams:
``_run_git`` (a table-driven stand-in returning canned
``subprocess.CompletedProcess`` values) and, for the endpoint tests,
``_git_toplevel`` / ``_create_worktree_sync``. No subprocess is spawned, no
network is touched, and every filesystem write lands under ``tmp_path`` or the
autouse-isolated ``KIROCREW_HOME`` from ``conftest.py``.

Style follows ``test_worktree_create.py`` (aiohttp ``TestClient`` +
``TestServer`` for endpoint work, direct calls for the sync helpers) and
``test_dashboard_handlers_core_coverage.py`` (``monkeypatch``-swapped seams, a
recorder in place of the SEL audit).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import worktree as wt

# ── injected git boundary ────────────────────────────────────────────────


def _proc(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(list(args), returncode, stdout, stderr)


class FakeGit:
    """Table-driven stand-in for ``_run_git``.

    ``table`` maps an argv PREFIX tuple to ``(returncode, stdout, stderr)``.
    First matching prefix wins, so insertion order puts the specific entries
    ahead of the general ones; anything unmatched gets ``default``.
    """

    def __init__(self, table=None, default=(0, "", "")):
        self.table = dict(table or {})
        self.default = default
        self.calls: list[tuple[tuple[str, ...], str]] = []

    def __call__(self, args, cwd):
        self.calls.append((tuple(args), cwd))
        for prefix, result in self.table.items():
            if tuple(args[: len(prefix)]) == tuple(prefix):
                return _proc(args, *result)
        return _proc(args, *self.default)

    def ran(self, *prefix) -> bool:
        return any(call[: len(prefix)] == prefix for call, _ in self.calls)


@pytest.fixture
def git(monkeypatch):
    """Install a :class:`FakeGit` and hand it back for per-test programming."""
    fake = FakeGit()
    monkeypatch.setattr(wt, "_run_git", fake)
    return fake


@pytest.fixture
def audit(monkeypatch):
    """Swap the SEL audit seam for a recorder."""
    recorder = MagicMock()
    monkeypatch.setattr(wt, "sel", lambda: recorder)
    return recorder


def _outcomes(recorder) -> list[str]:
    return [c.kwargs.get("error", "") for c in recorder.log_api_access.call_args_list]


# ── _run_git ─────────────────────────────────────────────────────────────


class TestRunGit:
    """The sandbox chokepoint, the temp-file cleanup, and the launcher probe."""

    @staticmethod
    def _spawn(cleanup=None):
        def fake(argv, mode="standard", **kw):
            return list(argv), {}, cleanup

        return fake

    def test_missing_backend_becomes_sandbox_unavailable(self, monkeypatch):
        monkeypatch.setattr(
            wt, "sandboxed_spawn_argv", MagicMock(side_effect=RuntimeError("no backend"))
        )
        with pytest.raises(wt.SandboxUnavailable, match="no backend"):
            wt._run_git(["--version"], os.getcwd())

    def test_terminal_prompt_is_disabled_and_overrides_are_prepended(self, monkeypatch, tmp_path):
        seen: dict = {}

        def fake_spawn(argv, mode="standard", **kw):
            seen["argv"] = list(argv)
            return list(argv), {}, None

        def fake_run(argv, **kwargs):
            seen["env"] = kwargs.get("env")
            seen["timeout"] = kwargs.get("timeout")
            return _proc(argv)

        monkeypatch.setattr(wt, "sandboxed_spawn_argv", fake_spawn)
        monkeypatch.setattr(wt.subprocess, "run", fake_run)
        wt._run_git(["status"], str(tmp_path))
        assert seen["argv"][0] == "git"
        assert f"core.hooksPath={os.devnull}" in seen["argv"]
        assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert seen["timeout"] == wt._GIT_TIMEOUT

    def test_cleanup_file_is_unlinked(self, monkeypatch, tmp_path):
        scratch = tmp_path / "wrapper.json"
        scratch.write_text("{}", encoding="utf-8", newline="\n")
        monkeypatch.setattr(wt, "sandboxed_spawn_argv", self._spawn(str(scratch)))
        monkeypatch.setattr(wt.subprocess, "run", lambda argv, **kw: _proc(argv))
        wt._run_git(["status"], str(tmp_path))
        assert not scratch.exists()

    def test_missing_cleanup_file_is_suppressed(self, monkeypatch, tmp_path):
        """``os.unlink`` on an already-gone wrapper must not surface as an error."""
        monkeypatch.setattr(
            wt, "sandboxed_spawn_argv", self._spawn(str(tmp_path / "never-created"))
        )
        monkeypatch.setattr(wt.subprocess, "run", lambda argv, **kw: _proc(argv))
        assert wt._run_git(["status"], str(tmp_path)).returncode == 0

    def test_cleanup_runs_even_when_the_spawn_raises(self, monkeypatch, tmp_path):
        scratch = tmp_path / "wrapper.json"
        scratch.write_text("{}", encoding="utf-8", newline="\n")
        monkeypatch.setattr(wt, "sandboxed_spawn_argv", self._spawn(str(scratch)))

        def boom(argv, **kw):
            raise subprocess.TimeoutExpired(argv, wt._GIT_TIMEOUT)

        monkeypatch.setattr(wt.subprocess, "run", boom)
        with pytest.raises(subprocess.TimeoutExpired):
            wt._run_git(["status"], str(tmp_path))
        assert not scratch.exists()

    def test_launcher_stderr_prefix_is_a_refusal(self, monkeypatch, tmp_path):
        monkeypatch.setattr(wt, "sandboxed_spawn_argv", self._spawn())
        monkeypatch.setattr(
            wt.subprocess,
            "run",
            lambda argv, **kw: _proc(argv, 1, "", f"{wt._SANDBOX_LAUNCHER_PREFIX}denied\n"),
        )
        with pytest.raises(wt.SandboxUnavailable, match="denied"):
            wt._run_git(["status"], str(tmp_path))

    def test_none_stderr_is_not_a_refusal(self, monkeypatch, tmp_path):
        """``capture_output=False`` callers leave stderr None; that is a git error."""
        monkeypatch.setattr(wt, "sandboxed_spawn_argv", self._spawn())
        monkeypatch.setattr(wt.subprocess, "run", lambda argv, **kw: _proc(argv, 128, "", None))
        assert wt._run_git(["status"], str(tmp_path)).returncode == 128


# ── small pure helpers ───────────────────────────────────────────────────


class TestDirSlugEdges:
    def test_trailing_slash_is_ignored(self):
        assert wt._dir_slug("feat/thing/") == "thing"

    def test_bare_name_has_no_segments(self):
        assert wt._dir_slug("hotfix") == "hotfix"

    def test_slug_is_bounded(self):
        assert len(wt._dir_slug("feat/" + "z" * 400)) == wt._MAX_DIR_SLUG


class TestGitError:
    def test_empty_output_is_a_generic_message(self):
        assert wt._git_error(_proc(["git"], 1, "", "")) == "git failed"

    def test_blank_leading_lines_are_skipped(self):
        proc = _proc(["git"], 1, "", "\n\n   \nfatal: real reason\nnoise\n")
        assert wt._git_error(proc) == "fatal: real reason"

    def test_stdout_is_used_when_stderr_is_empty(self):
        assert wt._git_error(_proc(["git"], 1, "from stdout", "")) == "from stdout"

    def test_message_is_truncated(self):
        assert len(wt._git_error(_proc(["git"], 1, "", "x" * 900))) == 300

    def test_none_streams_are_tolerated(self):
        assert wt._git_error(_proc(["git"], 1, None, None)) == "git failed"


class TestNormPath:
    def test_normalizes_and_casefolds(self):
        raw = os.path.join("a", "b", "..", "b", "c")
        assert wt._norm_path(raw) == os.path.normcase(os.path.join("a", "b", "c"))


# ── _repo_lock ───────────────────────────────────────────────────────────


class TestRepoLock:
    @pytest.fixture(autouse=True)
    def _fresh_registry(self, monkeypatch):
        monkeypatch.setattr(wt, "_REPO_LOCKS", {})

    def test_same_root_returns_the_same_lock(self):
        first = wt._repo_lock("/srv/repo")
        assert wt._repo_lock("/srv/repo") is first

    def test_different_roots_get_different_locks(self):
        assert wt._repo_lock("/srv/a") is not wt._repo_lock("/srv/b")

    @pytest.mark.asyncio
    async def test_idle_locks_are_evicted_but_a_held_one_survives(self):
        held = wt._repo_lock("held-root")
        await held.acquire()
        try:
            # Fill to exactly the cap: the sweep runs on the call that would
            # exceed it, so one more root than this would evict early.
            for i in range(wt._MAX_REPO_LOCKS - 1):
                wt._repo_lock(f"idle-{i}")
            assert len(wt._REPO_LOCKS) == wt._MAX_REPO_LOCKS
            wt._repo_lock("brand-new")
            assert wt._REPO_LOCKS["held-root"] is held
            assert "brand-new" in wt._REPO_LOCKS
            assert len(wt._REPO_LOCKS) == 2
        finally:
            held.release()


# ── config / listing probes ──────────────────────────────────────────────


class TestResolveBaseRefAndCommit:
    def test_origin_head_is_preferred(self, git):
        git.table = {("rev-parse", "--verify", "--quiet", "origin/HEAD"): (0, "abc123\n", "")}
        assert wt._resolve_base_ref("/srv/repo") == "origin/HEAD"

    def test_empty_output_falls_back_to_head(self, git):
        git.table = {("rev-parse",): (0, "  \n", "")}
        assert wt._resolve_base_ref("/srv/repo") == "HEAD"

    def test_failed_probe_falls_back_to_head(self, git):
        git.table = {("rev-parse",): (128, "", "fatal")}
        assert wt._resolve_base_ref("/srv/repo") == "HEAD"

    def test_resolve_commit_returns_the_sha(self, git):
        git.table = {("rev-parse",): (0, "deadbeef\n", "")}
        assert wt._resolve_commit("/srv/repo", "HEAD") == "deadbeef"

    def test_resolve_commit_is_empty_on_failure(self, git):
        git.table = {("rev-parse",): (1, "", "")}
        assert wt._resolve_commit("/srv/repo", "nope") == ""


class TestGitToplevel:
    def test_non_repo_is_none(self, git):
        git.table = {("rev-parse",): (128, "", "fatal: not a git repository")}
        assert wt._git_toplevel("/srv/plain") is None

    def test_blank_toplevel_is_none(self, git):
        git.table = {("rev-parse",): (0, "\n", "")}
        assert wt._git_toplevel("/srv/plain") is None

    def test_toplevel_is_realpathed(self, git, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        git.table = {("rev-parse",): (0, f"{root}\n", "")}
        assert wt._git_toplevel(str(root)) == os.path.realpath(str(root))


class TestWorktreeBranches:
    def test_listing_failure_is_none_not_empty(self, git):
        git.table = {("worktree", "list"): (128, "", "fatal")}
        assert wt._worktree_branches("/srv/repo") is None

    def test_parses_branch_detached_and_raw_refs(self, git):
        listing = "\0".join(
            [
                "worktree /srv/repo",
                "HEAD aaa",
                "branch refs/heads/main",
                "",
                "worktree /srv/repo-wt-a",
                "branch refs/heads/feat/a",
                "",
                "worktree /srv/repo-wt-detached",
                "HEAD bbb",
                "detached",
                "",
                "worktree /srv/repo-wt-odd",
                "branch refs/tags/v1",
                "",
            ]
        )
        git.table = {("worktree", "list"): (0, listing, "")}
        trees = wt._worktree_branches("/srv/repo")
        assert trees is not None
        assert trees[wt._norm_path("/srv/repo")] == "main"
        assert trees[wt._norm_path("/srv/repo-wt-a")] == "feat/a"
        assert trees[wt._norm_path("/srv/repo-wt-detached")] == ""
        assert trees[wt._norm_path("/srv/repo-wt-odd")] == "refs/tags/v1"

    def test_branch_field_without_a_worktree_is_ignored(self, git):
        git.table = {("worktree", "list"): (0, "branch refs/heads/orphan\0", "")}
        assert wt._worktree_branches("/srv/repo") == {}

    def test_empty_listing_is_an_empty_mapping(self, git):
        git.table = {("worktree", "list"): (0, "", "")}
        assert wt._worktree_branches("/srv/repo") == {}


class TestWorktreeConfigActive:
    def test_extension_probe_failure_is_false(self, git):
        git.table = {("config", "--bool"): (1, "", "")}
        assert wt._worktree_config_active("/srv/repo") is False

    def test_extension_disabled_is_false(self, git):
        git.table = {("config", "--bool"): (0, "false\n", "")}
        assert wt._worktree_config_active("/srv/repo") is False

    def test_unlocatable_git_dir_fails_open_to_true(self, git):
        """Cannot find GIT_DIR: assume the scope is live so the probe fails closed."""
        git.table = {
            ("config", "--bool"): (0, "true\n", ""),
            ("rev-parse", "--absolute-git-dir"): (128, "", "fatal"),
        }
        assert wt._worktree_config_active("/srv/repo") is True

    def test_blank_git_dir_is_also_true(self, git):
        git.table = {
            ("config", "--bool"): (0, "true\n", ""),
            ("rev-parse", "--absolute-git-dir"): (0, "  \n", ""),
        }
        assert wt._worktree_config_active("/srv/repo") is True

    def test_absolute_git_dir_with_the_file_present(self, git, tmp_path):
        gitdir = tmp_path / "dotgit"
        gitdir.mkdir()
        (gitdir / "config.worktree").write_text("", encoding="utf-8", newline="\n")
        git.table = {
            ("config", "--bool"): (0, "true\n", ""),
            ("rev-parse", "--absolute-git-dir"): (0, f"{gitdir}\n", ""),
        }
        assert wt._worktree_config_active(str(tmp_path)) is True

    def test_absolute_git_dir_without_the_file(self, git, tmp_path):
        gitdir = tmp_path / "dotgit"
        gitdir.mkdir()
        git.table = {
            ("config", "--bool"): (0, "true\n", ""),
            ("rev-parse", "--absolute-git-dir"): (0, f"{gitdir}\n", ""),
        }
        assert wt._worktree_config_active(str(tmp_path)) is False

    def test_relative_git_dir_is_joined_to_the_root(self, git, tmp_path):
        root = tmp_path / "proj"
        (root / ".git").mkdir(parents=True)
        (root / ".git" / "config.worktree").write_text("", encoding="utf-8", newline="\n")
        git.table = {
            ("config", "--bool"): (0, "true\n", ""),
            ("rev-parse", "--absolute-git-dir"): (0, ".git\n", ""),
        }
        assert wt._worktree_config_active(str(root)) is True


class TestCheckoutFilter:
    def test_no_filter_keys_is_clean(self, git, monkeypatch):
        monkeypatch.setattr(wt, "_worktree_config_active", lambda root: False)
        git.table = {("config",): (0, "core.bare\nremote.origin.url\n", "")}
        assert wt._checkout_filter("/srv/repo") == ""

    @pytest.mark.parametrize(
        "key",
        [
            "filter.evil.process",
            "filter.evil.smudge",
            "filter.evil.clean",
            "FILTER.Evil.SMUDGE",
            "filter.with.dots.in.name.process",
        ],
    )
    def test_filter_driver_keys_are_reported(self, git, monkeypatch, key):
        monkeypatch.setattr(wt, "_worktree_config_active", lambda root: False)
        git.table = {("config",): (0, f"core.bare\n  {key}  \n", "")}
        assert wt._checkout_filter("/srv/repo") == key

    @pytest.mark.parametrize("key", ["filter.evil.required", "filterfoo.process", "filter.x"])
    def test_non_driver_keys_are_ignored(self, git, monkeypatch, key):
        monkeypatch.setattr(wt, "_worktree_config_active", lambda root: False)
        git.table = {("config",): (0, f"{key}\n", "")}
        assert wt._checkout_filter("/srv/repo") == ""

    def test_reported_key_is_truncated(self, git, monkeypatch):
        monkeypatch.setattr(wt, "_worktree_config_active", lambda root: False)
        git.table = {("config",): (0, "filter." + "n" * 400 + ".smudge\n", "")}
        assert len(wt._checkout_filter("/srv/repo")) == 120

    def test_unreadable_scope_fails_closed(self, git, monkeypatch):
        monkeypatch.setattr(wt, "_worktree_config_active", lambda root: False)
        git.table = {("config",): (128, "", "unable to read config file")}
        assert wt._checkout_filter("/srv/repo") == wt._FILTER_PROBE_FAILED

    def test_worktree_scope_is_probed_when_active(self, git, monkeypatch):
        monkeypatch.setattr(wt, "_worktree_config_active", lambda root: True)
        git.table = {
            ("config", "--worktree"): (0, "filter.sneaky.smudge\n", ""),
            ("config", "--local"): (0, "core.bare\n", ""),
        }
        assert wt._checkout_filter("/srv/repo") == "filter.sneaky.smudge"
        assert git.ran("config", "--local", "--includes", "--name-only", "--list")
        assert git.ran("config", "--worktree", "--includes", "--name-only", "--list")

    def test_worktree_scope_is_skipped_when_inactive(self, git, monkeypatch):
        monkeypatch.setattr(wt, "_worktree_config_active", lambda root: False)
        git.table = {("config",): (0, "", "")}
        assert wt._checkout_filter("/srv/repo") == ""
        assert not git.ran("config", "--worktree", "--includes", "--name-only", "--list")


# ── branch claim / release ───────────────────────────────────────────────


class TestClaimAndDelete:
    def test_claim_uses_the_empty_old_value_sentinel(self, git):
        assert wt._claim_branch("/srv/repo", "feat/x", "abc") is True
        argv, cwd = git.calls[0]
        assert argv == (
            "update-ref",
            "--create-reflog",
            "refs/heads/feat/x",
            "abc",
            "",
        )
        assert cwd == "/srv/repo"

    def test_claim_failure_is_false(self, git):
        git.table = {("update-ref",): (1, "", "fatal: already exists")}
        assert wt._claim_branch("/srv/repo", "feat/x", "abc") is False

    def test_delete_without_a_recorded_sha_refuses(self, git, caplog):
        with caplog.at_level("WARNING", logger=wt.logger.name):
            assert wt._delete_ref_if_unchanged("/srv/repo", "feat/x", "") is False
        assert not git.calls
        assert "no claimed sha" in caplog.text

    def test_delete_is_compare_and_delete(self, git):
        assert wt._delete_ref_if_unchanged("/srv/repo", "feat/x", "abc") is True
        assert git.calls[0][0] == ("update-ref", "-d", "refs/heads/feat/x", "abc")

    def test_delete_of_a_moved_ref_fails(self, git):
        git.table = {("update-ref", "-d"): (1, "", "fatal: ref has changed")}
        assert wt._delete_ref_if_unchanged("/srv/repo", "feat/x", "abc") is False


# ── _cleanup_partial ─────────────────────────────────────────────────────


class TestCleanupPartial:
    """Only what a request can PROVE it created may be removed."""

    @staticmethod
    def _dest(tmp_path, name="proj-wt-x"):
        dest = tmp_path / name
        dest.mkdir()
        (dest / "marker.txt").write_text("x", encoding="utf-8", newline="\n")
        return dest

    def test_untouched_when_nothing_was_created_or_claimed(self, git, tmp_path):
        dest = self._dest(tmp_path)
        wt._cleanup_partial(
            str(tmp_path / "proj"), str(dest), "feat/x", claimed=False, created=False
        )
        assert (dest / "marker.txt").is_file()
        assert git.ran("worktree", "prune")
        assert not git.ran("worktree", "remove")

    def test_foreign_registration_is_spared(self, git, tmp_path, monkeypatch):
        dest = self._dest(tmp_path)
        monkeypatch.setattr(
            wt,
            "_worktree_branches",
            lambda root: {wt._norm_path(str(dest)): "someone/else"},
        )
        wt._cleanup_partial(
            str(tmp_path / "proj"), str(dest), "feat/x", claimed=False, created=True
        )
        assert (dest / "marker.txt").is_file()
        assert not git.ran("worktree", "remove")

    def test_unreadable_listing_still_removes_our_own_mkdir(self, git, tmp_path, monkeypatch):
        dest = self._dest(tmp_path)
        monkeypatch.setattr(wt, "_worktree_branches", lambda root: None)
        wt._cleanup_partial(
            str(tmp_path / "proj"), str(dest), "feat/x", claimed=False, created=True
        )
        assert not dest.exists()

    def test_rmtree_fallback_when_worktree_remove_refuses(self, tmp_path, monkeypatch):
        dest = self._dest(tmp_path)
        fake = FakeGit({("worktree", "remove"): (1, "", "fatal: not a working tree")})
        monkeypatch.setattr(wt, "_run_git", fake)
        monkeypatch.setattr(wt, "_worktree_branches", lambda root: {})
        wt._cleanup_partial(
            str(tmp_path / "proj"), str(dest), "feat/x", claimed=False, created=True
        )
        assert not dest.exists()

    def test_successful_remove_skips_the_rmtree(self, tmp_path, monkeypatch):
        dest = self._dest(tmp_path)

        def remove_it(args, cwd):
            if args[:2] == ["worktree", "remove"]:
                for child in dest.iterdir():
                    child.unlink()
                dest.rmdir()
            return _proc(args)

        monkeypatch.setattr(wt, "_run_git", remove_it)
        monkeypatch.setattr(wt, "_worktree_branches", lambda root: {})
        wt._cleanup_partial(
            str(tmp_path / "proj"), str(dest), "feat/x", claimed=False, created=True
        )
        assert not dest.exists()

    def test_claimed_branch_is_deleted_after_a_prune(self, git, tmp_path, monkeypatch):
        monkeypatch.setattr(wt, "_worktree_branches", lambda root: {})
        wt._cleanup_partial(
            str(tmp_path / "proj"),
            str(tmp_path / "proj-wt-x"),
            "feat/x",
            claimed=True,
            created=False,
            base_sha="abc",
        )
        assert git.ran("worktree", "prune")
        assert git.ran("update-ref", "-d", "refs/heads/feat/x", "abc")

    def test_claimed_branch_survives_when_another_worktree_holds_it(
        self, git, tmp_path, monkeypatch, caplog
    ):
        monkeypatch.setattr(
            wt,
            "_worktree_branches",
            lambda root: {wt._norm_path(str(tmp_path / "theirs")): "feat/x"},
        )
        with caplog.at_level("WARNING", logger=wt.logger.name):
            wt._cleanup_partial(
                str(tmp_path / "proj"),
                str(tmp_path / "proj-wt-x"),
                "feat/x",
                claimed=True,
                created=False,
                base_sha="abc",
            )
        assert not git.ran("update-ref", "-d")
        assert "another worktree" in caplog.text

    def test_claimed_branch_survives_an_unreadable_listing(self, git, tmp_path, monkeypatch):
        monkeypatch.setattr(wt, "_worktree_branches", lambda root: None)
        wt._cleanup_partial(
            str(tmp_path / "proj"),
            str(tmp_path / "proj-wt-x"),
            "feat/x",
            claimed=True,
            created=False,
            base_sha="abc",
        )
        assert not git.ran("update-ref", "-d")

    def test_our_own_destination_does_not_count_as_an_adopter(self, git, tmp_path, monkeypatch):
        dest = tmp_path / "proj-wt-x"
        monkeypatch.setattr(
            wt, "_worktree_branches", lambda root: {wt._norm_path(str(dest)): "feat/x"}
        )
        wt._cleanup_partial(
            str(tmp_path / "proj"),
            str(dest),
            "feat/x",
            claimed=True,
            created=False,
            base_sha="abc",
        )
        assert git.ran("update-ref", "-d", "refs/heads/feat/x", "abc")

    def test_delete_is_retried_once_after_a_second_prune(self, tmp_path, monkeypatch):
        attempts: list[int] = []

        def flaky(root, branch, base_sha):
            attempts.append(1)
            return len(attempts) > 1

        monkeypatch.setattr(wt, "_worktree_branches", lambda root: {})
        monkeypatch.setattr(wt, "_delete_ref_if_unchanged", flaky)
        fake = FakeGit()
        monkeypatch.setattr(wt, "_run_git", fake)
        wt._cleanup_partial(
            str(tmp_path / "proj"),
            str(tmp_path / "proj-wt-x"),
            "feat/x",
            claimed=True,
            created=False,
            base_sha="abc",
        )
        assert len(attempts) == 2
        prunes = [c for c, _ in fake.calls if c[:2] == ("worktree", "prune")]
        assert len(prunes) == 2

    def test_a_branch_surviving_both_attempts_is_logged(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(wt, "_worktree_branches", lambda root: {})
        monkeypatch.setattr(wt, "_delete_ref_if_unchanged", lambda *a: False)
        monkeypatch.setattr(wt, "_run_git", FakeGit())
        with caplog.at_level("WARNING", logger=wt.logger.name):
            wt._cleanup_partial(
                str(tmp_path / "proj"),
                str(tmp_path / "proj-wt-x"),
                "feat/x",
                claimed=True,
                created=False,
                base_sha="abc",
            )
        assert "already existing" in caplog.text


# ── _create_worktree_sync ────────────────────────────────────────────────


@pytest.fixture
def sync_env(monkeypatch, tmp_path):
    """A programmable stand-in for every seam ``_create_worktree_sync`` uses.

    Each test flips ONE field so the branch under test is the only thing that
    differs from the success path.
    """
    root = tmp_path / "proj"
    root.mkdir()
    state = SimpleNamespace(
        root=str(root),
        dest=str(tmp_path / "proj-wt-x"),
        checkout_filter="",
        registered={},
        base="HEAD",
        sha="c0ffee",
        claim=True,
        add=(0, "", ""),
        add_raises=None,
        cleanups=[],
    )
    monkeypatch.setattr(wt, "_checkout_filter", lambda r: state.checkout_filter)
    monkeypatch.setattr(wt, "_worktree_branches", lambda r: state.registered)
    monkeypatch.setattr(wt, "_resolve_base_ref", lambda r: state.base)
    monkeypatch.setattr(wt, "_resolve_commit", lambda r, ref: state.sha)
    monkeypatch.setattr(wt, "_claim_branch", lambda r, b, s: state.claim)

    def fake_run(args, cwd):
        if args[:2] == ["worktree", "add"]:
            if state.add_raises is not None:
                raise state.add_raises
            return _proc(args, *state.add)
        return _proc(args)

    monkeypatch.setattr(wt, "_run_git", fake_run)
    monkeypatch.setattr(
        wt,
        "_cleanup_partial",
        lambda r, dest, branch, **kw: state.cleanups.append((dest, branch, kw)),
    )
    return state


class TestCreateWorktreeSync:
    def test_happy_path_creates_the_sibling_directory(self, sync_env):
        payload, status = wt._create_worktree_sync(sync_env.root, "feat/x")
        assert status == 200
        assert payload == {
            "ok": True,
            "path": sync_env.dest,
            "branch": "feat/x",
            "base": "HEAD",
            "reused": False,
        }
        assert os.path.isdir(sync_env.dest)
        assert sync_env.cleanups == []

    def test_sensitive_destination_is_denied(self, sync_env, monkeypatch):
        monkeypatch.setattr(wt, "is_sensitive_path", lambda p: True)
        assert wt._create_worktree_sync(sync_env.root, "feat/x") == (
            {"error": "Access denied"},
            403,
        )

    def test_checkout_filter_is_a_409_naming_the_key(self, sync_env):
        sync_env.checkout_filter = "filter.evil.smudge"
        payload, status = wt._create_worktree_sync(sync_env.root, "feat/x")
        assert status == 409
        assert "filter.evil.smudge" in payload["error"]
        assert not os.path.exists(sync_env.dest)

    def test_unreadable_worktree_listing_is_a_503(self, sync_env):
        sync_env.registered = None
        assert wt._create_worktree_sync(sync_env.root, "feat/x") == (
            {"error": "git could not list this repository's worktrees"},
            503,
        )

    def test_our_own_worktree_at_the_destination_is_reused(self, sync_env):
        os.mkdir(sync_env.dest)
        sync_env.registered = {wt._norm_path(sync_env.dest): "feat/x"}
        payload, status = wt._create_worktree_sync(sync_env.root, "feat/x")
        assert status == 200
        assert payload["reused"] is True
        assert payload["base"] == ""

    def test_a_different_branch_at_the_destination_is_a_409(self, sync_env):
        os.mkdir(sync_env.dest)
        sync_env.registered = {wt._norm_path(sync_env.dest): "fix/x"}
        payload, status = wt._create_worktree_sync(sync_env.root, "feat/x")
        assert status == 409
        assert "already exists" in payload["error"]

    def test_an_unregistered_directory_at_the_destination_is_a_409(self, sync_env):
        os.mkdir(sync_env.dest)
        payload, status = wt._create_worktree_sync(sync_env.root, "feat/x")
        assert status == 409
        assert os.path.isdir(sync_env.dest), "someone else's directory was removed"

    def test_unresolvable_base_is_a_400_before_anything_is_created(self, sync_env):
        sync_env.sha = ""
        sync_env.base = "origin/HEAD"
        payload, status = wt._create_worktree_sync(sync_env.root, "feat/x")
        assert status == 400
        assert "origin/HEAD" in payload["error"]
        assert not os.path.exists(sync_env.dest)
        assert sync_env.cleanups == []

    def test_a_lost_branch_claim_is_a_409(self, sync_env):
        sync_env.claim = False
        payload, status = wt._create_worktree_sync(sync_env.root, "feat/x")
        assert status == 409
        assert "Branch already exists" in payload["error"]
        assert not os.path.exists(sync_env.dest)

    def test_mkdir_race_unwinds_the_claim_without_owning_the_directory(
        self, sync_env, monkeypatch
    ):
        def racing_mkdir(path, *a, **kw):
            raise FileExistsError(17, "File exists")

        monkeypatch.setattr(wt.os, "mkdir", racing_mkdir)
        payload, status = wt._create_worktree_sync(sync_env.root, "feat/x")
        assert status == 409
        assert sync_env.cleanups == [
            (sync_env.dest, "feat/x", {"claimed": True, "created": False, "base_sha": "c0ffee"})
        ]

    def test_mkdir_oserror_is_a_500_reporting_strerror(self, sync_env, monkeypatch):
        def denied(path, *a, **kw):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(wt.os, "mkdir", denied)
        payload, status = wt._create_worktree_sync(sync_env.root, "feat/x")
        assert status == 500
        assert "Permission denied" in payload["error"]
        assert sync_env.cleanups[0][2]["created"] is False

    def test_mkdir_oserror_without_strerror_still_reports(self, sync_env, monkeypatch):
        monkeypatch.setattr(wt.os, "mkdir", MagicMock(side_effect=OSError("odd failure")))
        payload, status = wt._create_worktree_sync(sync_env.root, "feat/x")
        assert status == 500
        assert "odd failure" in payload["error"]

    def test_a_timeout_cleans_up_and_re_raises(self, sync_env):
        sync_env.add_raises = subprocess.TimeoutExpired(["git"], wt._GIT_TIMEOUT)
        with pytest.raises(subprocess.TimeoutExpired):
            wt._create_worktree_sync(sync_env.root, "feat/x")
        assert sync_env.cleanups[0][2] == {
            "claimed": True,
            "created": True,
            "base_sha": "c0ffee",
        }

    def test_a_failing_add_is_a_400_with_gits_own_message(self, sync_env):
        sync_env.add = (1, "", "fatal: injected failure\n")
        payload, status = wt._create_worktree_sync(sync_env.root, "feat/x")
        assert status == 400
        assert payload["error"] == "fatal: injected failure"
        assert sync_env.cleanups[0][2]["created"] is True

    def test_add_reporting_success_with_no_directory_is_a_500(self, sync_env, monkeypatch):
        """Defensive branch: git exits 0 but the tree is not on disk."""
        monkeypatch.setattr(wt.os, "mkdir", lambda path, *a, **kw: None)
        payload, status = wt._create_worktree_sync(sync_env.root, "feat/x")
        assert status == 500
        assert "no directory was created" in payload["error"]
        assert sync_env.cleanups[0][2]["created"] is True

    def test_destination_name_is_derived_from_the_branch_tail(self, sync_env):
        payload, status = wt._create_worktree_sync(sync_env.root, "feat/deep/upload-limit")
        assert status == 200
        assert os.path.basename(payload["path"]) == "proj-wt-upload-limit"


# ── _allowed_repo_roots ──────────────────────────────────────────────────


class TestAllowedRepoRoots:
    @staticmethod
    def _state(*projects):
        return SimpleNamespace(
            _slots={f"chat-{i}": SimpleNamespace(project=p) for i, p in enumerate(projects)}
        )

    def test_no_state_yields_nothing(self):
        assert wt._allowed_repo_roots(None) == []

    def test_state_without_slots_yields_nothing(self):
        assert wt._allowed_repo_roots(SimpleNamespace()) == []

    def test_empty_and_whitespace_projects_are_skipped(self):
        assert wt._allowed_repo_roots(self._state("", "   ", None)) == []

    def test_missing_directories_are_skipped(self, tmp_path):
        assert wt._allowed_repo_roots(self._state(str(tmp_path / "gone"))) == []

    def test_a_file_is_not_a_root(self, tmp_path):
        target = tmp_path / "afile"
        target.write_text("x", encoding="utf-8", newline="\n")
        assert wt._allowed_repo_roots(self._state(str(target))) == []

    def test_duplicates_are_collapsed(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        roots = wt._allowed_repo_roots(self._state(str(proj), str(proj)))
        assert roots == [os.path.realpath(str(proj))]

    def test_projects_are_realpathed_and_stripped(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        roots = wt._allowed_repo_roots(self._state(f"  {proj}  "))
        assert roots == [os.path.realpath(str(proj))]

    def test_non_string_project_is_coerced_then_skipped(self):
        assert wt._allowed_repo_roots(self._state(12345)) == []


class TestMatchAllowedRootEdges:
    def test_trailing_separator_on_the_root_still_matches_a_child(self):
        root = os.path.join(os.sep + "srv", "repo") + os.sep
        child = os.path.join(os.sep + "srv", "repo", "src")
        assert wt._match_allowed_root(child, [root]) == root

    def test_case_differences_match_where_the_platform_ignores_case(self):
        root = os.path.join(os.sep + "srv", "Repo")
        probe = os.path.join(os.sep + "srv", "repo")
        expected = root if os.path.normcase(probe) == os.path.normcase(root) else None
        assert wt._match_allowed_root(probe, [root]) == expected


# ── api_worktree_create ──────────────────────────────────────────────────


def _make_app(*projects: str, internal: bool = True) -> web.Application:
    """App whose state exposes one slot per allowed project directory.

    ``internal_auth`` is the one caller shape ``deny_non_dashboard_caller``
    admits without an owner identity (it is how every MCP call arrives), so it
    keeps these tests on the handler's own logic rather than on the auth seam --
    which ``test_worktree_create.py::TestCallerIsolation`` already covers.
    """

    @web.middleware
    async def claims(request: web.Request, handler):
        request["user"] = "dashboard"
        if internal:
            request["internal_auth"] = True
        else:
            request["app"] = "some-app"
        return await handler(request)

    app = web.Application(middlewares=[claims])
    state = SimpleNamespace(
        owner_id="owner",
        _slots={f"chat-{i}": SimpleNamespace(project=p) for i, p in enumerate(projects) if p},
    )
    app["state"] = state
    app.router.add_post("/api/worktree/create", wt.api_worktree_create)
    return app


@pytest.fixture
def repo_dir(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    return root


class TestEndpointValidation:
    @pytest.mark.asyncio
    async def test_non_dashboard_caller_is_refused(self, repo_dir, audit):
        app = _make_app(str(repo_dir), internal=False)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/worktree/create", json={"repo": str(repo_dir), "branch": "feat/x"}
            )
            assert resp.status == 403

    @pytest.mark.asyncio
    async def test_malformed_json_body(self, audit):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/worktree/create",
                data="{not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            assert (await resp.json())["error"] == "invalid JSON"

    @pytest.mark.asyncio
    async def test_a_json_list_body_is_refused(self, audit):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/worktree/create", json=["repo", "branch"])
            assert resp.status == 400
            assert (await resp.json())["error"] == "invalid JSON"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            {"repo": 1, "branch": "feat/x"},
            {"repo": "/srv/repo", "branch": None},
            {},
        ],
    )
    async def test_non_string_inputs(self, audit, body):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/worktree/create", json=body)
            assert resp.status == 400
            assert "must be strings" in (await resp.json())["error"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("body", [{"repo": "  ", "branch": "feat/x"}, {"repo": "/r", "branch": " "}])
    async def test_blank_inputs_are_required_errors(self, audit, body):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/worktree/create", json=body)
            assert resp.status == 400
            assert "are required" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_over_long_branch_is_audited_and_refused(self, audit):
        long_branch = "feat/" + "a" * (wt.MAX_FOLLOWUP_BRANCH + 10)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/worktree/create", json={"repo": "/srv/repo", "branch": long_branch}
            )
            assert resp.status == 400
            assert (await resp.json())["error"] == "Invalid branch name"
        assert "invalid branch name" in _outcomes(audit)

    @pytest.mark.asyncio
    async def test_repo_outside_every_slot_project_is_audited_and_refused(self, repo_dir, audit):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/worktree/create", json={"repo": str(repo_dir), "branch": "feat/x"}
            )
            assert resp.status == 403
            assert "project directory of an existing session" in (await resp.json())["error"]
        assert "outside slot project directories" in _outcomes(audit)

    @pytest.mark.asyncio
    async def test_a_granted_path_that_is_not_a_directory_is_a_400(
        self, tmp_path, audit, monkeypatch
    ):
        """The grant is served from slot state, so it can name a path since removed.

        The allow-list seam is replaced rather than ``os.path.isdir``: the
        endpoint and ``_allowed_repo_roots`` share that call, so patching it
        globally empties the allow-list and the request 403s for the wrong
        reason.
        """
        missing = str(tmp_path / "was-here")
        monkeypatch.setattr(wt, "_allowed_repo_roots", lambda state: [missing])
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/worktree/create", json={"repo": missing, "branch": "feat/x"}
            )
            assert resp.status == 400
            assert (await resp.json())["error"] == "repo is not a directory"

    @pytest.mark.asyncio
    async def test_sensitive_repo_is_audited_and_refused(self, repo_dir, audit, monkeypatch):
        monkeypatch.setattr(wt, "is_sensitive_path", lambda p: True)
        async with TestClient(TestServer(_make_app(str(repo_dir)))) as client:
            resp = await client.post(
                "/api/worktree/create", json={"repo": str(repo_dir), "branch": "feat/x"}
            )
            assert resp.status == 403
            assert (await resp.json())["error"] == "Access denied"
        assert "sensitive path" in _outcomes(audit)

    @pytest.mark.asyncio
    async def test_a_tilde_repo_is_expanded_before_matching(self, tmp_path, audit, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        proj = tmp_path / "proj"
        proj.mkdir()
        expanded = os.path.expanduser(os.path.join("~", "proj"))
        if os.path.realpath(expanded) != os.path.realpath(str(proj)):
            pytest.skip("HOME is not honored by expanduser on this host")
        monkeypatch.setattr(wt, "_git_toplevel", lambda p: os.path.realpath(p))
        monkeypatch.setattr(wt, "_create_worktree_sync", lambda root, branch: ({"ok": True}, 200))
        async with TestClient(TestServer(_make_app(str(proj)))) as client:
            resp = await client.post(
                "/api/worktree/create",
                json={"repo": os.path.join("~", "proj"), "branch": "feat/x"},
            )
            assert resp.status == 200, await resp.text()


class TestEndpointGitBoundary:
    @pytest.fixture(autouse=True)
    def _no_real_git(self, monkeypatch):
        monkeypatch.setattr(wt, "_create_worktree_sync", lambda root, branch: ({"ok": True}, 200))

    @pytest.mark.asyncio
    async def test_sandbox_unavailable_on_the_toplevel_probe_is_a_503(
        self, repo_dir, audit, monkeypatch
    ):
        monkeypatch.setattr(
            wt, "_git_toplevel", MagicMock(side_effect=wt.SandboxUnavailable("no backend"))
        )
        async with TestClient(TestServer(_make_app(str(repo_dir)))) as client:
            resp = await client.post(
                "/api/worktree/create", json={"repo": str(repo_dir), "branch": "feat/x"}
            )
            assert resp.status == 503
            assert (await resp.json())["error"] == wt._SANDBOX_REFUSAL
        assert "sandbox backend unavailable" in _outcomes(audit)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc", [OSError("git missing"), subprocess.SubprocessError("spawn failed")]
    )
    async def test_a_spawn_failure_on_the_toplevel_probe_is_a_503(
        self, repo_dir, audit, monkeypatch, exc
    ):
        monkeypatch.setattr(wt, "_git_toplevel", MagicMock(side_effect=exc))
        async with TestClient(TestServer(_make_app(str(repo_dir)))) as client:
            resp = await client.post(
                "/api/worktree/create", json={"repo": str(repo_dir), "branch": "feat/x"}
            )
            assert resp.status == 503
            assert (await resp.json())["error"] == "git is unavailable"

    @pytest.mark.asyncio
    async def test_a_non_repo_is_a_400(self, repo_dir, audit, monkeypatch):
        monkeypatch.setattr(wt, "_git_toplevel", lambda p: None)
        async with TestClient(TestServer(_make_app(str(repo_dir)))) as client:
            resp = await client.post(
                "/api/worktree/create", json={"repo": str(repo_dir), "branch": "feat/x"}
            )
            assert resp.status == 400
            assert (await resp.json())["error"] == "Not a git repository"

    @pytest.mark.asyncio
    async def test_a_toplevel_above_every_grant_is_audited_and_refused(
        self, tmp_path, audit, monkeypatch
    ):
        nested = tmp_path / "proj" / "src"
        nested.mkdir(parents=True)
        monkeypatch.setattr(wt, "_git_toplevel", lambda p: os.path.realpath(str(tmp_path / "proj")))
        async with TestClient(TestServer(_make_app(str(nested)))) as client:
            resp = await client.post(
                "/api/worktree/create", json={"repo": str(nested), "branch": "feat/x"}
            )
            assert resp.status == 403
            body = await resp.json()
        assert body["error"] == (
            "The repository root is outside this session's project directory."
        )
        assert "git toplevel outside slot project directories" in _outcomes(audit)

    @pytest.mark.asyncio
    async def test_a_sensitive_toplevel_is_audited_and_refused(
        self, repo_dir, audit, monkeypatch
    ):
        """The SECOND sensitive-path screen: the grant is clean, the toplevel is not.

        ``is_sensitive_path`` must reject only the resolved toplevel here --
        rejecting the granted root as well short-circuits at the earlier screen,
        which answers the same 403 for a different reason and leaves this branch
        unexercised.
        """
        inner = os.path.realpath(str(repo_dir / "inner"))
        monkeypatch.setattr(wt, "_git_toplevel", lambda p: inner)
        monkeypatch.setattr(wt, "is_sensitive_path", lambda p: os.path.realpath(p) == inner)
        async with TestClient(TestServer(_make_app(str(repo_dir)))) as client:
            resp = await client.post(
                "/api/worktree/create", json={"repo": str(repo_dir), "branch": "feat/x"}
            )
            assert resp.status == 403
            assert (await resp.json())["error"] == "Access denied"
        assert _outcomes(audit) == ["sensitive path"]
        assert audit.log_api_access.call_args.kwargs["resources"] == f"root={inner}"


class TestEndpointCreateOutcomes:
    @pytest.fixture(autouse=True)
    def _toplevel(self, monkeypatch):
        monkeypatch.setattr(wt, "_git_toplevel", lambda p: os.path.realpath(p))

    @pytest.mark.asyncio
    async def test_success_is_audited_as_allowed(self, repo_dir, audit, monkeypatch, caplog):
        created = str(repo_dir.parent / "proj-wt-x")
        monkeypatch.setattr(
            wt,
            "_create_worktree_sync",
            lambda root, branch: (
                {"ok": True, "path": created, "branch": branch, "base": "HEAD", "reused": False},
                200,
            ),
        )
        with caplog.at_level("INFO", logger=wt.logger.name):
            async with TestClient(TestServer(_make_app(str(repo_dir)))) as client:
                resp = await client.post(
                    "/api/worktree/create", json={"repo": str(repo_dir), "branch": "feat/x"}
                )
                assert resp.status == 200, await resp.text()
                assert (await resp.json())["path"] == created
        assert audit.log_api_access.call_args.kwargs["outcome"] == "allowed"
        assert "Created worktree" in caplog.text

    @pytest.mark.asyncio
    async def test_a_refusal_status_is_audited_as_error(self, repo_dir, audit, monkeypatch):
        monkeypatch.setattr(
            wt, "_create_worktree_sync", lambda root, branch: ({"error": "nope"}, 409)
        )
        async with TestClient(TestServer(_make_app(str(repo_dir)))) as client:
            resp = await client.post(
                "/api/worktree/create", json={"repo": str(repo_dir), "branch": "feat/x"}
            )
            assert resp.status == 409
        kwargs = audit.log_api_access.call_args.kwargs
        assert kwargs["outcome"] == "error"
        assert kwargs["error"] == "nope"

    @pytest.mark.asyncio
    async def test_a_git_timeout_is_a_504(self, repo_dir, audit, monkeypatch):
        def timeout(root, branch):
            raise subprocess.TimeoutExpired(["git"], wt._GIT_TIMEOUT)

        monkeypatch.setattr(wt, "_create_worktree_sync", timeout)
        async with TestClient(TestServer(_make_app(str(repo_dir)))) as client:
            resp = await client.post(
                "/api/worktree/create", json={"repo": str(repo_dir), "branch": "feat/x"}
            )
            assert resp.status == 504
            assert (await resp.json())["error"] == "git timed out"
        assert "git timeout" in _outcomes(audit)

    @pytest.mark.asyncio
    async def test_sandbox_unavailable_during_create_is_a_503(self, repo_dir, audit, monkeypatch):
        def refuse(root, branch):
            raise wt.SandboxUnavailable("no backend")

        monkeypatch.setattr(wt, "_create_worktree_sync", refuse)
        async with TestClient(TestServer(_make_app(str(repo_dir)))) as client:
            resp = await client.post(
                "/api/worktree/create", json={"repo": str(repo_dir), "branch": "feat/x"}
            )
            assert resp.status == 503
            assert (await resp.json())["error"] == wt._SANDBOX_REFUSAL
        assert "sandbox backend unavailable" in _outcomes(audit)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc", [OSError("disk full"), subprocess.SubprocessError("git died")]
    )
    async def test_an_unexpected_failure_during_create_is_a_500(
        self, repo_dir, audit, monkeypatch, exc
    ):
        def blow_up(root, branch):
            raise exc

        monkeypatch.setattr(wt, "_create_worktree_sync", blow_up)
        async with TestClient(TestServer(_make_app(str(repo_dir)))) as client:
            resp = await client.post(
                "/api/worktree/create", json={"repo": str(repo_dir), "branch": "feat/x"}
            )
            assert resp.status == 500
            assert (await resp.json())["error"] == "worktree creation failed"

    @pytest.mark.asyncio
    async def test_same_repo_requests_are_serialized_by_the_repo_lock(
        self, repo_dir, audit, monkeypatch
    ):
        """Two concurrent same-root requests must not interleave the create."""
        monkeypatch.setattr(wt, "_REPO_LOCKS", {})
        overlap = {"max": 0, "now": 0}

        def slow_create(root, branch):
            overlap["now"] += 1
            overlap["max"] = max(overlap["max"], overlap["now"])
            try:
                import time

                time.sleep(0.05)
                return ({"ok": True, "path": root, "branch": branch}, 200)
            finally:
                overlap["now"] -= 1

        monkeypatch.setattr(wt, "_create_worktree_sync", slow_create)
        async with TestClient(TestServer(_make_app(str(repo_dir)))) as client:
            responses = await asyncio.gather(
                *[
                    client.post(
                        "/api/worktree/create",
                        json={"repo": str(repo_dir), "branch": f"feat/x{i}"},
                    )
                    for i in range(3)
                ]
            )
        assert [r.status for r in responses] == [200, 200, 200]
        assert overlap["max"] == 1, "the per-repo lock did not serialize the create"


# ── module constants ─────────────────────────────────────────────────────


class TestModuleContract:
    def test_sandbox_mode_and_hook_sink_are_pinned(self):
        assert wt._SANDBOX_MODE == "strict"
        assert wt._HOOKS_SINK == os.devnull
        assert not os.path.isdir(wt._HOOKS_SINK)

    def test_filter_regex_only_matches_driver_keys(self):
        assert wt._FILTER_KEY_RE.match("filter.a.process")
        assert wt._FILTER_KEY_RE.match("filter.a.b.clean")
        assert not wt._FILTER_KEY_RE.match("filter..process")
        assert not wt._FILTER_KEY_RE.match("filter.a.required")

    def test_probe_failure_sentinel_is_not_a_plausible_key(self):
        assert not wt._FILTER_KEY_RE.match(wt._FILTER_PROBE_FAILED)
