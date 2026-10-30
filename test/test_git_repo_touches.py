"""The Git tab's repository discovery: which repos a session's tool calls touched.

Covers ``kiro_crew.dashboard.git_repo_touches`` (candidate extraction, root
resolution, slot bookkeeping, transcript seeding) and the two routes that read
it: ``GET /api/project/git/repos`` lists a slot's repositories, and
``GET /api/project/git/status`` admits a touched root that is not any slot's
project directory.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from git_maintenance_helpers import git_maintenance_off_entries

from kiro_crew.dashboard import git_repo_touches as touches
from kiro_crew.dashboard.chat_runner import _flush_file_changes
from kiro_crew.dashboard.handlers import (
    api_project_git_log,
    api_project_git_repos,
    api_project_git_status,
)
from kiro_crew.dashboard.handlers.files import _project_git_branch
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.platform import tool_paths
from kiro_crew.platform.tool_paths import _PATCH_TEXT_MAX_CHARS
from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_CALL_UPDATE,
    EVENT_TOOL_RESULT,
    LLMEvent,
)
from kiro_crew.testing.wait import async_wait_until, await_until


@pytest.fixture(autouse=True)
def ceiling_at_tmp_path(tmp_path, _floor_monkeypatch) -> None:
    """Keep every upward walk inside the test's tree, whatever ``tmp_path`` sits under."""
    _floor_monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))


@pytest.fixture()
def mock_sel():
    with patch("kiro_crew.dashboard.handlers.sel") as m:
        m.return_value = MagicMock()
        yield m.return_value


def _init_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    env.update(git_maintenance_off_entries(env))
    subprocess.run(
        ["git", "init", "-q", "-b", "trunk"],
        cwd=root,
        check=True,
        capture_output=True,
        env=env,
    )
    return root


def _real(path: Path) -> str:
    return os.path.realpath(path)


def _repo_slot(repos=(), project: str = "") -> _ChatSlot:
    slot = _ChatSlot("repo-touch-test")
    slot._git_repos = list(repos)
    slot.project = project
    return slot


# ── touch_candidates ──


class TestTouchCandidates:
    def test_edit_call_contributes_its_target(self):
        params = {"command": "strReplace", "path": "/r/a.py"}
        assert touches.touch_candidates(params, tool_kind="edit") == ["/r/a.py"]

    def test_diff_block_path_makes_it_an_edit(self):
        assert touches.touch_candidates({}, diff_path="/r/b.py") == ["/r/b.py"]

    def test_read_call_contributes_nothing(self):
        params = {"path": "/r/a.py"}
        assert touches.touch_candidates(params, tool_kind="read") == []

    def test_shell_working_dir(self):
        params = {"command": "git status", "working_dir": "/r/repo"}
        assert touches.touch_candidates(params, is_shell=True) == ["/r/repo"]

    def test_shell_cd_and_git_c(self):
        params = {"command": "cd '/r/one' && make; git -C /r/two commit -m x"}
        assert touches.touch_candidates(params, is_shell=True) == ["/r/one", "/r/two"]

    def test_shell_argv_command(self):
        params = {"command": ["bash", "-lc", "cd ~/proj && ls"]}
        assert touches.touch_candidates(params, is_shell=True) == ["~/proj"]

    def test_relative_cd_is_not_a_candidate(self):
        params = {"command": "cd src && ls"}
        assert touches.touch_candidates(params, is_shell=True) == []

    @pytest.mark.parametrize(
        ("command", "dirs"),
        [
            ("echo hi\n  cd /r/c\nmake", ["/r/c"]),
            ("x &&\n\tcd /r/f", ["/r/f"]),
            ("true ||\tpushd ~/r/d", ["~/r/d"]),
            ("builtin cd /r/e", ["/r/e"]),
            ("echo cd /r/no", []),
        ],
    )
    def test_cd_at_a_line_start_or_after_a_separator(self, command, dirs):
        assert touches._command_dirs(command) == dirs

    def test_a_cd_match_never_spans_a_newline(self):
        """A gap that can cross newlines lets each newline of a long run start a
        match that rescans the rest of it: quadratic work on the event loop for
        a proposed shell call. Matches anchor per line instead."""
        command = "\n" * 64 + " \t\n" * 64 + "  cd /r/a"
        spans = [m.group(0) for m in touches._CD_RE.finditer(command)]
        assert spans == ["  cd /r/a"]

    def test_non_shell_ignores_command_text(self):
        params = {"command": "cd /r/one && ls"}
        assert touches.touch_candidates(params, tool_kind="other") == []

    @pytest.mark.parametrize(
        "source",
        ["working_dir", "cwd", "workdir", "working_directory", "edit", "diff", "cd", "git_c"],
    )
    def test_path_length_bound_keeps_the_limit_and_drops_oversized_candidates(self, source):
        at_limit = "/" + "a" * (touches.MAX_REPO_PATH_CHARS - 1)
        oversized = at_limit + "b"

        def candidates(path):
            if source == "edit":
                return touches.touch_candidates({"path": path}, tool_kind="edit")
            if source == "diff":
                return touches.touch_candidates({}, diff_path=path)
            if source in ("cd", "git_c"):
                command = f"cd '{path}'" if source == "cd" else f"git -C '{path}' status"
                return touches.touch_candidates({"command": command}, is_shell=True)
            return touches.touch_candidates({source: path}, is_shell=True)

        dropped = candidates(oversized)
        assert dropped == []
        assert dropped.truncated is True
        kept = candidates(at_limit)
        assert kept == [at_limit]
        assert kept.truncated is False

    def test_nested_edit_aliases_keep_key_and_document_order(self):
        params = {
            "path": ["/r/a", "/r/a"],
            "operations": [None, {"filePath": "/r/b"}, {"targetFile": "/r/c"}],
        }
        assert touches.touch_candidates(params, tool_kind="edit", diff_path="/r/d") == [
            "/r/a",
            "/r/b",
            "/r/c",
            "/r/d",
        ]

    def test_patch_targets_keep_the_shared_literal_header_contract(self):
        patch = "*** Begin Patch\n*** Update File: /r/a\n+x\n*** Move to: /r/b\n*** End Patch"
        assert touches.touch_candidates({"patchText": patch}, tool_kind="edit") == ["/r/a", "/r/b"]
        invalid = patch.replace("/r/a", "relative")
        assert touches.touch_candidates(
            {"path": "/r/params", "patchText": invalid}, diff_path="/r/diff"
        ) == ["/r/params"]

    def test_command_text_and_argv_work_are_bounded(self):
        oversized = "cd /r/hidden; " + "x" * touches._MAX_COMMAND_CHARS
        assert touches._command_texts(oversized) == []
        assert touches._command_dirs(oversized) == []
        argv = [None] * touches._MAX_CANDIDATES_PER_CALL + ["cd /r/hidden"]
        assert touches._command_texts(argv) == []
        command = ";".join(["cd /r/a"] * touches._MAX_CANDIDATES_PER_CALL + ["cd /r/b"])
        assert touches._command_dirs(command) == ["/r/a", "/r/b"]

    @pytest.mark.parametrize("bound", ["_TARGET_PATH_MAX_NODES", "_TARGET_PATH_MAX_PATHS"])
    def test_edit_container_walk_uses_the_shared_bounds(self, monkeypatch, bound):
        cap = 12
        monkeypatch.setattr(tool_paths, bound, cap)
        visited: list[int] = []

        class Params(dict):
            def get(self, key, default=None):
                if key == "path":
                    visited.append(self["index"])
                return super().get(key, default)

        params = {"operations": [Params(index=i, path=f"/r/{i}") for i in range(cap * 2)]}
        found = touches.touch_candidates(params, tool_kind="edit")
        assert found
        assert len(visited) <= cap
        assert len(found) <= touches._MAX_CANDIDATES_PER_CALL
        assert found.truncated is True
        if bound == "_TARGET_PATH_MAX_NODES":
            cyclic: dict[str, Any] = {"path": "/r/a"}
            cyclic["again"] = cyclic
            found = touches.touch_candidates(cyclic, tool_kind="edit")
            assert found == ["/r/a"]
            assert found.truncated is True

    @pytest.mark.parametrize("patch_text", [None, False, 1, [], {}])
    def test_non_string_patch_text_keeps_params_and_marks_the_scan_incomplete(self, patch_text):
        found = touches.touch_candidates(
            {"path": "/r/params", "patchText": patch_text}, diff_path="/r/diff"
        )
        assert found == ["/r/params"]
        assert found.truncated is True

    def test_windows_absolute_cd_and_git_c_targets_are_kept(self, monkeypatch):
        """A drive-letter or UNC ``cd``/``git -C`` target is absolute on Windows.

        Swap in ``ntpath.isabs`` so the Windows rule runs on any host; relative
        targets stay dropped and POSIX ``/`` and ``~`` keep working.
        """
        import ntpath

        monkeypatch.setattr(touches.os.path, "isabs", ntpath.isabs)
        command = (
            "cd 'C:\\work\\api' && git -C D:/work/web status; "
            "pushd \\\\host\\share\\repo; cd /r/posix; cd ~/r/home; cd relative"
        )
        assert touches._command_dirs(command) == [
            "C:\\work\\api",
            "\\\\host\\share\\repo",
            "/r/posix",
            "~/r/home",
            "D:/work/web",
        ]

    def test_candidates_are_deduplicated_and_capped(self):
        many = " ; ".join(f"cd /r/{i}" for i in range(40))
        params = {"command": f"cd /r/0 && {many}", "working_dir": "/r/0"}
        found = touches.touch_candidates(params, is_shell=True)
        assert found[0] == "/r/0"
        assert len(found) == len(set(found)) == touches._MAX_CANDIDATES_PER_CALL


# ── resolve_repo_roots ──


class TestResolveRepoRoots:
    def test_path_length_bound_screens_candidates_before_walking(self, tmp_path, monkeypatch):
        prefix = tmp_path.anchor
        at_limit = prefix + "a" * (touches.MAX_REPO_PATH_CHARS - len(prefix))
        oversized = at_limit + "b"
        root = _real(tmp_path / "repo")
        walked: list[str] = []

        def find(path):
            walked.append(path)
            return root

        monkeypatch.setattr(touches, "find_git_root", find)
        assert touches.resolve_repo_roots([oversized, at_limit]) == [root]
        assert walked == [at_limit]

    def test_path_length_bound_screens_resolved_roots(self, tmp_path, monkeypatch):
        at_limit = "/" + "a" * (touches.MAX_REPO_PATH_CHARS - 1)
        oversized = at_limit + "b"
        monkeypatch.setattr(touches, "find_git_root", lambda path: path)
        monkeypatch.setattr(touches, "is_sensitive_path", lambda path: False)
        realpath = touches.os.path.realpath
        first = str(tmp_path / "first")
        second = str(tmp_path / "second")

        def resolve(path, *args, **kwargs):
            if path == first:
                return oversized
            if path == second:
                return at_limit
            return realpath(path, *args, **kwargs)

        monkeypatch.setattr(touches.os.path, "realpath", resolve)
        assert touches.resolve_repo_roots([first, second]) == [at_limit]

    def test_file_in_subdirectory_resolves_to_repo_root(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        (repo / "pkg").mkdir()
        roots = touches.resolve_repo_roots([str(repo / "pkg" / "new_file.py")])
        assert roots == [_real(repo)]

    def test_path_that_does_not_exist_yet_still_resolves(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        roots = touches.resolve_repo_roots([str(repo / "a" / "b" / "c.txt")])
        assert roots == [_real(repo)]

    def test_linked_worktree_dot_git_file_counts(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n")
        assert touches.resolve_repo_roots([str(wt / "x.py")]) == [_real(wt)]

    def test_outside_any_repo_is_dropped(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        assert touches.resolve_repo_roots([str(plain / "x.py")]) == []

    def test_ceiling_stops_the_walk(self, tmp_path, monkeypatch):
        outer = _init_repo(tmp_path / "outer")
        inner = outer / "inner"
        inner.mkdir()
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(inner))
        assert touches.resolve_repo_roots([str(inner / "deep" / "x.py")]) == []

    def test_branch_probe_shares_the_walk_and_its_ceiling(self, tmp_path, monkeypatch):
        """``GET /api/project/git`` discovers through the same walk as the list.

        One walk, so a project under a ``GIT_CEILING_DIRECTORIES`` entry reads as
        a repository on both surfaces or on neither -- and on neither matches the
        status and log routes, whose ``git rev-parse --git-dir`` stops at the
        ceiling too. A second walk without the ceiling labelled a branch the
        status route then said was not a repository.
        """
        outer = _init_repo(tmp_path / "outer")
        deep = outer / "inner" / "deep"
        deep.mkdir(parents=True)
        assert _project_git_branch(_real(deep))["branch"] == "trunk"
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(outer / "inner"))
        assert _project_git_branch(_real(deep)) == {"repo": False}

    def test_relative_path_needs_an_anchor(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        assert touches.resolve_repo_roots(["src/x.py"]) == []
        assert touches.resolve_repo_roots(["src/x.py"], str(repo)) == [_real(repo)]

    def test_home_directory_is_never_listed(self, tmp_path, monkeypatch):
        home = _init_repo(tmp_path / "home")
        # expanduser reads HOME on POSIX and USERPROFILE on Windows.
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        assert touches.resolve_repo_roots([str(home / "notes.txt")]) == []

    def test_sensitive_root_is_dropped(self, tmp_path, monkeypatch):
        repo = _init_repo(tmp_path / "repo")
        monkeypatch.setattr(touches, "is_sensitive_path", lambda p: p == _real(repo))
        assert touches.resolve_repo_roots([str(repo / "x")]) == []

    def test_distinct_roots_take_their_latest_position(self, tmp_path):
        """Candidates arrive oldest to newest: A, B, A makes A the newest root."""
        a = _init_repo(tmp_path / "a")
        b = _init_repo(tmp_path / "b")
        roots = touches.resolve_repo_roots([str(a / "1"), str(b / "2"), str(a / "3")])
        assert roots == [_real(b), _real(a)]
        slot = _repo_slot()
        touches.record_repo_roots(slot, roots)
        assert slot._git_repos[-1] == _real(a), "the last-touched repository is the newest"


# ── record_repo_roots / transcript seed ──


class TestRecordRepoRoots:
    @pytest.mark.parametrize("newest", [False, True])
    def test_path_length_bound_covers_held_and_evicted_roots(self, newest):
        at_limit = "/" + "a" * (touches.MAX_REPO_PATH_CHARS - 1)
        oversized = at_limit + "b"
        slot = _repo_slot()
        touches.record_repo_roots(slot, [oversized, at_limit], newest=newest)
        assert slot._git_repos == [at_limit]
        assert slot._git_repos_evicted == []
        assert touches.omitted_repo_count(slot) == 0
        touches.record_repo_roots(slot, [f"/r{i}" for i in range(touches.MAX_SLOT_GIT_REPOS)])
        assert slot._git_repos_evicted == [at_limit]
        assert oversized not in slot._git_repos
        assert touches.omitted_repo_count(slot) == 1

    def test_path_length_bound_covers_transcript_seeds(self):
        at_limit = "/" + "a" * (touches.MAX_REPO_PATH_CHARS - 1)
        oversized = at_limit + "b"
        messages = [
            {
                "role": "assistant",
                "meta": {"file_changes": [{"path": oversized}, {"path": at_limit}]},
            }
        ]
        assert touches.transcript_change_paths(messages) == [at_limit]

    def test_live_touch_moves_root_to_newest(self):
        slot = _repo_slot(["/a", "/b"])
        assert touches.record_repo_roots(slot, ["/a"]) is True
        assert slot._git_repos == ["/b", "/a"]

    def test_unchanged_reports_false(self):
        slot = _repo_slot(["/a", "/b"])
        assert touches.record_repo_roots(slot, ["/b"]) is False

    def test_seed_adds_only_unknown_roots_at_the_oldest_end(self):
        slot = _repo_slot(["/live"])
        touches.record_repo_roots(slot, ["/live", "/old"], newest=False)
        assert slot._git_repos == ["/old", "/live"]

    def test_cap_drops_least_recent(self):
        slot = _repo_slot([])
        n = touches.MAX_SLOT_GIT_REPOS + 3
        touches.record_repo_roots(slot, [f"/r{i}" for i in range(n)])
        assert len(slot._git_repos) == touches.MAX_SLOT_GIT_REPOS
        assert slot._git_repos[-1] == f"/r{n - 1}"
        assert "/r0" not in slot._git_repos
        assert slot._git_repos_evicted == ["/r0", "/r1", "/r2"]
        assert touches.omitted_repo_count(slot) == 3

    def test_revisiting_two_repos_past_the_cap_hides_exactly_one(self):
        slot = _repo_slot([])
        roots = [f"/r{i}" for i in range(touches.MAX_SLOT_GIT_REPOS + 1)]
        touches.record_repo_roots(slot, roots)
        assert touches.omitted_repo_count(slot) == 1
        for _ in range(50):
            touches.record_repo_roots(slot, [roots[-1]])
            touches.record_repo_roots(slot, [roots[0]])
        assert touches.omitted_repo_count(slot) == 1
        assert len(slot._git_repos) == touches.MAX_SLOT_GIT_REPOS

    def test_a_retouched_evicted_root_leaves_the_count(self):
        slot = _repo_slot([])
        roots = [f"/r{i}" for i in range(touches.MAX_SLOT_GIT_REPOS + 1)]
        touches.record_repo_roots(slot, roots)
        assert slot._git_repos_evicted == [roots[0]]
        touches.record_repo_roots(slot, [roots[0]])
        assert roots[0] in slot._git_repos
        assert slot._git_repos_evicted == [roots[1]]
        assert touches.omitted_repo_count(slot) == 1
        assert touches.record_repo_roots(slot, [roots[0]]) is False

    def test_seed_eviction_counts_even_when_the_held_list_does_not_change(self):
        roots = [f"/r{i}" for i in range(touches.MAX_SLOT_GIT_REPOS)]
        slot = _repo_slot(roots)
        assert touches.record_repo_roots(slot, ["/old"], newest=False) is True
        assert slot._git_repos == roots
        assert slot._git_repos_evicted == ["/old"]
        touches.record_repo_roots(slot, [*roots, "/old"], newest=False)
        assert touches.omitted_repo_count(slot) == 1

    def test_seed_counts_every_root_beyond_the_cap(self):
        slot = _repo_slot([])
        roots = [f"/r{i}" for i in range(touches.MAX_SLOT_GIT_REPOS + 3)]
        touches.record_repo_roots(slot, roots, newest=False)
        assert slot._git_repos == roots[3:]
        assert slot._git_repos_evicted == roots[:3]
        assert touches.omitted_repo_count(slot) == 3

    def test_seed_keeps_its_order_ahead_of_live_roots(self):
        slot = _repo_slot(["/live"])
        touches.record_repo_roots(slot, ["/b", "/live", "/a"], newest=False)
        assert slot._git_repos == ["/b", "/a", "/live"]

    def test_evicted_roots_past_their_bound_are_kept_as_a_count(self):
        slot = _repo_slot([])
        extra = touches.MAX_SLOT_EVICTED_GIT_REPOS + 5
        roots = [f"/r{i}" for i in range(touches.MAX_SLOT_GIT_REPOS + extra)]
        touches.record_repo_roots(slot, roots)
        assert len(slot._git_repos_evicted) == touches.MAX_SLOT_EVICTED_GIT_REPOS
        assert slot._git_repos_evicted[0] == "/r5"
        assert slot._git_repos_evicted_overflow == 5
        assert touches.omitted_repo_count(slot) == extra

    def test_real_slot_initializes_its_fields_independently(self):
        first = _ChatSlot("git-overflow-first")
        second = _ChatSlot("git-overflow-second")
        assert touches.omitted_repo_count(first) == touches.omitted_repo_count(second) == 0
        roots = [f"/r{i}" for i in range(touches.MAX_SLOT_GIT_REPOS + 1)]
        touches.record_repo_roots(first, roots)
        assert touches.omitted_repo_count(first) == 1
        assert second._git_repos_evicted == []
        assert second._git_repos_evicted_overflow == 0

    def test_transcript_paths_newest_first(self):
        messages: list[dict[str, Any]] = [
            {"role": "assistant", "meta": {"file_changes": [{"path": "/a/1"}, {"path": "/b/1"}]}},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "meta": {"file_changes": [{"path": "/c/1"}, {"path": "/a/1"}]}},
            {"role": "assistant", "meta": {"file_changes": "garbage"}},
        ]
        assert touches.transcript_change_paths(messages) == ["/c/1", "/a/1", "/b/1"]

    @pytest.mark.parametrize("role", ["user", "tool", "system", "error", None])
    def test_only_assistant_rows_seed_paths(self, role):
        messages: list[dict[str, Any]] = [
            {"role": "assistant", "meta": {"file_changes": [{"path": "/agent/f"}]}},
            {"meta": {"file_changes": [{"path": "/forged/f"}]}},
        ]
        if role is not None:
            messages[-1]["role"] = role
        assert touches.transcript_change_paths(messages) == ["/agent/f"]

    def test_unverified_write_rows_do_not_seed_paths(self):
        # A failed or refused write whose outcome cannot be proven keeps only its
        # path in the flushed row (omitted_reason="unverified_write"). Live
        # capture records nothing for such a call, so the seed must not either.
        messages: list[dict[str, Any]] = [
            {
                "role": "assistant",
                "meta": {
                    "file_changes": [
                        {"path": "/done/f"},
                        {"path": "/refused/f", "omitted_reason": "unverified_write"},
                    ]
                },
            },
        ]
        assert touches.transcript_change_paths(messages) == ["/done/f"]


def _executed_touch(slot, params, *, background_tasks=None, **kwargs) -> None:
    """The walk tests supply a call and its successful terminal result together."""
    slot._git_repo_touch_terminal.pop("executed", None)
    touches.note_tool_call(slot, params, tool_call_id="executed", **kwargs)
    touches.finish_tool_call(slot, "executed", executed=True, background_tasks=background_tasks)


class TestNoteToolCall:
    """The runner's seam: queue on the loop, walk on the slot's walker task."""

    @staticmethod
    async def _drained(slot: _ChatSlot) -> None:
        task = slot._git_repo_walk_task
        assert task is not None
        await await_until(task, what="the slot's repo walker draining")

    @pytest.mark.asyncio
    async def test_path_length_bound_covers_a_stalled_walkers_backlog(self, monkeypatch):
        at_limit = "/" + "a" * (touches.MAX_REPO_PATH_CHARS - 1)
        oversized = at_limit + "b"
        entered = asyncio.Event()
        release = asyncio.Event()
        walked: list[list[str]] = []

        async def stalled_walk(slot):
            entered.set()
            await await_until(release, what="the stalled walker being released")
            walked.append(list(slot._git_repo_walk_pending))
            slot._git_repo_walk_pending = []

        monkeypatch.setattr(touches, "_drain_walks", stalled_walk)
        slot = _repo_slot()
        try:
            _executed_touch(slot, {"working_dir": "/first"}, is_shell=True)
            await await_until(entered, what="the repo walker starting")
            walker = slot._git_repo_walk_task
            assert walker is not None
            for i in range(touches._MAX_PENDING_CANDIDATES_PER_SLOT):
                _executed_touch(slot, {"working_dir": oversized + str(i)}, is_shell=True)
            assert slot._git_repo_walk_pending == ["/first"]
            _executed_touch(slot, {"working_dir": at_limit}, is_shell=True)
            assert slot._git_repo_walk_pending == ["/first", at_limit]
            assert slot._git_repo_walk_task is walker
        finally:
            release.set()
            await self._drained(slot)
        assert walked == [["/first", at_limit]]

    @pytest.mark.asyncio
    async def test_records_the_repo_a_write_lands_in(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        slot = _repo_slot()
        _executed_touch(slot, {"command": "create", "path": str(repo / "x.py")}, tool_kind="edit")
        await self._drained(slot)
        assert slot._git_repos == [_real(repo)]

    @pytest.mark.asyncio
    async def test_never_raises(self, monkeypatch):
        def _boom(*_a, **_k):
            raise RuntimeError("walk failed")

        monkeypatch.setattr(touches, "resolve_repo_roots", _boom)
        slot = _repo_slot()
        _executed_touch(slot, {"working_dir": "/x"}, is_shell=True)
        await self._drained(slot)
        assert slot._git_repos == []
        assert slot._git_repo_walk_pending == []

    @pytest.mark.asyncio
    async def test_a_call_with_no_candidates_starts_no_walker(self):
        slot = _repo_slot()
        _executed_touch(slot, {"path": "/r/a.py"}, tool_kind="read")
        assert slot._git_repo_walk_task is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("params_path", [False, True])
    async def test_edit_user_home_expansion_runs_only_on_the_walker_thread(
        self, tmp_path, monkeypatch, params_path
    ):
        repo = _init_repo(tmp_path / "repo")
        root = _real(repo)
        loop_thread = threading.get_ident()
        calls: list[tuple[str, int]] = []
        real_expanduser = os.path.expanduser
        target = "~repo-touch-user/x.py"

        def expanduser(path):
            if isinstance(path, str) and path.startswith("~"):
                calls.append((path, threading.get_ident()))
            if path == target:
                return str(repo / "x.py")
            return real_expanduser(path)

        monkeypatch.setattr(os.path, "expanduser", expanduser)
        slot = _repo_slot()
        params = {"path": target} if params_path else {}
        _executed_touch(slot, params, tool_kind="edit", diff_path=target)
        await self._drained(slot)
        assert slot._git_repos == [root]
        assert any(path == target for path, _ in calls), "the named home was not expanded"
        assert all(thread != loop_thread for _, thread in calls), calls

    @pytest.mark.asyncio
    async def test_raw_edit_snapshot_is_bounded_and_does_not_retain_params(self, monkeypatch):
        monkeypatch.setattr(touches, "_MAX_PENDING_CANDIDATES_PER_SLOT", 3)
        slot = _repo_slot()

        async def parked() -> None:
            await await_until(asyncio.Event(), what="the snapshot walker being cancelled")

        task = asyncio.create_task(parked())
        slot._git_repo_walk_task = task
        at_limit = "/" + "a" * (touches.MAX_REPO_PATH_CHARS - 1)
        try:
            params = {"path": [at_limit, at_limit + "x"], "content": "large document"}
            _executed_touch(slot, params, tool_kind="edit", diff_path=at_limit + "x")
            params["path"] = ["/changed"]
            assert slot._git_repo_walk_pending == [at_limit]
            for i in range(touches._MAX_PENDING_CANDIDATES_PER_SLOT + 1):
                _executed_touch(slot, {}, diff_path=f"~repo-touch-user/{i}")
            assert slot._git_repo_walk_pending == [
                "~repo-touch-user/1",
                "~repo-touch-user/2",
                "~repo-touch-user/3",
            ]
            assert all(
                len(path) <= touches.MAX_REPO_PATH_CHARS for path in slot._git_repo_walk_pending
            )
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await await_until(task, what="the parked snapshot walker cancelling")

    @pytest.mark.asyncio
    async def test_relative_diff_is_withheld_but_relative_params_use_the_project(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        slot = _repo_slot(project=str(repo))
        _executed_touch(slot, {}, diff_path="relative.py")
        await self._drained(slot)
        assert slot._git_repos == []
        _executed_touch(slot, {"path": "relative.py"}, tool_kind="edit")
        _executed_touch(slot, {}, diff_path="relative.py")
        await self._drained(slot)
        assert slot._git_repos == [_real(repo)]

    @pytest.mark.asyncio
    async def test_a_hanging_walk_does_not_delay_the_next_event(self, tmp_path, monkeypatch):
        """The Design review's case: a ``stat`` that hangs on a dead mount.

        ``find_git_root`` is held on a ``threading.Event`` the test owns. While
        it is held the runner's next ``note_tool_call`` returns at once and the
        loop keeps serving other coroutines; nothing is recorded yet, and the
        second call queues behind the one walker rather than starting a second
        walk. Released, the walker records both roots in call order.
        """
        repo_a = _init_repo(tmp_path / "a")
        repo_b = _init_repo(tmp_path / "b")
        entered = threading.Event()
        release = threading.Event()
        real_find = touches.find_git_root
        walks: list[str] = []

        def _hanging_find(path: str) -> str | None:
            walks.append(path)
            entered.set()
            assert release.wait(10), "the test never released the walk"
            return real_find(path)

        monkeypatch.setattr(touches, "find_git_root", _hanging_find)
        slot = _repo_slot()
        try:
            first = _executed_touch(  # type: ignore[func-returns-value]
                slot, {"command": "create", "path": str(repo_a / "x.py")}, tool_kind="edit"
            )
            # Done, not a coroutine: the inline shape handed the runner a
            # coroutine that returned only once the walk did.
            assert first is None, "note_tool_call must not hand the runner the walk to await"
            walker = slot._git_repo_walk_task
            assert walker is not None
            await async_wait_until(
                entered.is_set, describe=lambda: "the walker thread never started"
            )
            # The walker is now blocked in its thread. The runner's next event:
            _executed_touch(slot, {"working_dir": str(repo_b)}, is_shell=True)
            # ...and the loop is free to run anything else meanwhile.
            await asyncio.sleep(0)
            assert slot._git_repos == []
            assert walks == [os.path.normpath(str(repo_a / "x.py"))]
            assert slot._git_repo_walk_pending == [str(repo_b)]
            assert slot._git_repo_walk_task is walker  # one walker, not two
        finally:
            release.set()
        await await_until(walker, what="the released walker recording both roots")
        assert slot._git_repos == [_real(repo_a), _real(repo_b)]
        assert slot._git_repo_walk_pending == []

    @pytest.mark.asyncio
    async def test_the_walker_is_held_in_the_owners_task_set_until_done(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        background: set[asyncio.Task[Any]] = set()
        slot = _repo_slot()
        _executed_touch(
            slot, {"working_dir": str(repo)}, is_shell=True, background_tasks=background
        )
        walker = slot._git_repo_walk_task
        assert walker is not None and background == {walker}
        await self._drained(slot)
        assert background == set()
        assert slot._git_repos == [_real(repo)]

    @pytest.mark.asyncio
    async def test_calls_queued_before_the_walk_run_as_one_walk_in_call_order(
        self, tmp_path, monkeypatch
    ):
        """A burst of calls is one thread hop, and a retouched path is newest."""
        repo_a = _init_repo(tmp_path / "a")
        repo_b = _init_repo(tmp_path / "b")
        real_resolve = touches.resolve_repo_roots
        walks: list[list[str]] = []

        def _spy(candidates, anchor=""):
            walks.append(list(candidates))
            return real_resolve(candidates, anchor)

        monkeypatch.setattr(touches, "resolve_repo_roots", _spy)
        slot = _repo_slot()
        for path in (repo_a / "1", repo_b / "2", repo_a / "1"):
            _executed_touch(slot, {"command": "create", "path": str(path)}, tool_kind="edit")
        await self._drained(slot)
        assert walks == [[str(repo_b / "2"), str(repo_a / "1")]]
        assert slot._git_repos == [_real(repo_b), _real(repo_a)]

    @pytest.mark.asyncio
    async def test_the_backlog_is_bounded_oldest_first(self, monkeypatch):
        entered = threading.Event()
        release = threading.Event()

        def _hanging_resolve(candidates, anchor=""):
            entered.set()
            assert release.wait(10), "the test never released the walk"
            return []

        monkeypatch.setattr(touches, "resolve_repo_roots", _hanging_resolve)
        monkeypatch.setattr(touches, "_MAX_PENDING_CANDIDATES_PER_SLOT", 3)
        slot = _repo_slot()
        try:
            _executed_touch(slot, {"working_dir": "/r0"}, is_shell=True)
            await async_wait_until(
                entered.is_set, describe=lambda: "the walker thread never started"
            )
            for i in range(1, 6):
                _executed_touch(slot, {"working_dir": f"/r{i}"}, is_shell=True)
            assert slot._git_repo_walk_pending == ["/r3", "/r4", "/r5"]
        finally:
            release.set()
        await self._drained(slot)

    @pytest.mark.asyncio
    async def test_a_cancelled_walker_leaves_nothing_behind(self, monkeypatch):
        """Loop shutdown cancels pending tasks; the walker must go quietly."""
        entered = threading.Event()
        release = threading.Event()

        def _hanging_resolve(candidates, anchor=""):
            entered.set()
            release.wait(10)
            return ["/late"]

        monkeypatch.setattr(touches, "resolve_repo_roots", _hanging_resolve)
        background: set[asyncio.Task[Any]] = set()
        slot = _repo_slot()
        try:
            _executed_touch(slot, {"working_dir": "/r"}, is_shell=True, background_tasks=background)
            walker = slot._git_repo_walk_task
            assert walker is not None
            await async_wait_until(
                entered.is_set, describe=lambda: "the walker thread never started"
            )
            walker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await walker
        finally:
            release.set()
        assert walker.cancelled()
        assert walker not in background
        # The thread's late result lands nowhere: the await it fed was cancelled.
        assert slot._git_repos == []
        # A later call starts a fresh walker rather than reusing the dead one.
        monkeypatch.setattr(touches, "resolve_repo_roots", lambda c, a="": ["/fresh"])
        _executed_touch(slot, {"working_dir": "/r"}, is_shell=True)
        assert slot._git_repo_walk_task is not walker
        await self._drained(slot)
        assert slot._git_repos == ["/fresh"]


# ── routes ──


class _Slot(_ChatSlot):
    def __init__(
        self, project: str = "", repos=(), messages=(), app: str = "", key: str = "s"
    ) -> None:
        super().__init__(key)
        self.key = key
        self.linked_session_key = ""
        self.project = project
        self._git_repos = list(repos)
        self._git_repos_seeded = False
        self.messages = list(messages)
        self._app = app


class _State:
    def __init__(self, **slots: _Slot) -> None:
        self._slots = dict(slots)

    def get_slot(self, key: str):
        return self._slots.get(key)


def _app(state: _State, request_app: str = "") -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/project/git/repos", api_project_git_repos)
    app.router.add_get("/api/project/git/status", api_project_git_status)
    app.router.add_get("/api/project/git/log", api_project_git_log)
    if request_app:

        @web.middleware
        async def as_app(request, handler):
            request["app"] = request_app
            return await handler(request)

        app.middlewares.append(as_app)
    return app


class TestReposRoute:
    @pytest.mark.asyncio
    async def test_unloaded_restored_slot_reads_off_loop_and_replays_on_loop(
        self, tmp_path, mock_sel, monkeypatch
    ):
        repo = _init_repo(tmp_path / "repo")
        messages = [{"role": "assistant", "meta": {"file_changes": [{"path": str(repo / "f")}]}}]
        loop_thread = threading.get_ident()
        reads: list[int] = []
        replays: list[int] = []

        class PendingWindow:
            def read(self, slot):
                thread = threading.get_ident()
                reads.append(thread)
                assert thread != loop_thread, "transcript disk read ran on the event loop"
                return (messages,)

            def __call__(self, slot, rows):
                thread = threading.get_ident()
                replays.append(thread)
                assert thread == loop_thread, "transcript replay ran off the event loop"
                slot._messages.extend(rows)

        slot = _Slot()
        monkeypatch.setattr(slot, "_lazy_window", PendingWindow())
        assert slot.window_pending
        async with TestClient(TestServer(_app(_State(s=slot)))) as client:
            response = await await_until(
                asyncio.ensure_future(client.get("/api/project/git/repos?slot=s")),
                what="the restored slot's repos listing",
            )
            assert response.status == 200
            body = await response.json()
        assert len(reads) == 1 and reads[0] != loop_thread
        assert replays == [loop_thread]
        assert not slot.window_pending
        assert slot._git_repos_seeded is True
        assert body["repos"] == [{"path": _real(repo), "source": "agent"}]

    @pytest.mark.asyncio
    async def test_project_first_then_touched_newest_first(self, tmp_path, mock_sel):
        proj = _init_repo(tmp_path / "proj")
        a = _init_repo(tmp_path / "a")
        b = _init_repo(tmp_path / "b")
        state = _State(s=_Slot(project=str(proj), repos=[_real(a), _real(proj), _real(b)]))
        async with TestClient(TestServer(_app(state))) as client:
            resp = await client.get("/api/project/git/repos?slot=s")
            assert resp.status == 200
            body = await resp.json()
        assert body["repos"] == [
            {"path": str(proj), "source": "project"},
            {"path": _real(b), "source": "agent"},
            {"path": _real(a), "source": "agent"},
        ]

    @pytest.mark.asyncio
    async def test_a_remote_session_lists_no_gateway_repository(
        self, tmp_path, mock_sel, monkeypatch
    ):
        from kiro_crew.dashboard.handlers import files

        proj = _init_repo(tmp_path / "proj")
        state = _State(s=_Slot(project=str(proj), repos=[_real(proj)]))
        monkeypatch.setattr(files, "session_runs_remotely", lambda key: True)
        async with TestClient(TestServer(_app(state))) as client:
            resp = await client.get("/api/project/git/repos?slot=s")
            assert resp.status == 200
            body = await resp.json()
        assert body["repos"] == []

    @pytest.mark.asyncio
    async def test_the_placement_read_runs_off_the_event_loop(
        self, tmp_path, mock_sel, monkeypatch
    ):
        from kiro_crew.dashboard.handlers import files

        loop_thread = threading.get_ident()
        threads: list[int] = []

        def placement(key):
            threads.append(threading.get_ident())
            return False

        monkeypatch.setattr(files, "session_runs_remotely", placement)
        state = _State(s=_Slot(project="", repos=[]))
        async with TestClient(TestServer(_app(state))) as client:
            assert (await client.get("/api/project/git/repos?slot=s")).status == 200
        assert threads and loop_thread not in threads

    @pytest.mark.asyncio
    async def test_project_outside_a_repo_is_not_listed(self, tmp_path, mock_sel):
        plain = tmp_path / "workspace"
        plain.mkdir()
        repo = _init_repo(tmp_path / "repo")
        state = _State(s=_Slot(project=str(plain), repos=[_real(repo)]))
        async with TestClient(TestServer(_app(state))) as client:
            body = await (await client.get("/api/project/git/repos?slot=s")).json()
        assert body["repos"] == [{"path": _real(repo), "source": "agent"}]

    @pytest.mark.asyncio
    async def test_deleted_repo_drops_out(self, tmp_path, mock_sel):
        gone = tmp_path / "gone"
        state = _State(s=_Slot(repos=[str(gone)]))
        async with TestClient(TestServer(_app(state))) as client:
            body = await (await client.get("/api/project/git/repos?slot=s")).json()
        assert body["repos"] == []
        assert body["omitted"] == 0
        assert body["listed"] == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("touched_project", [False, True])
    async def test_listed_counts_only_the_touched_repos_shown(
        self, tmp_path, mock_sel, touched_project
    ):
        """``listed`` feeds the capacity notice's numbers. An untouched project
        row is not an agent repository and a held root whose ``.git`` is gone is
        not shown, so neither counts; the project counts once the agent worked
        in it."""
        proj = _init_repo(tmp_path / "proj")
        a = _init_repo(tmp_path / "a")
        gone = tmp_path / "gone"
        held = [_real(a), str(gone)] + ([_real(proj)] if touched_project else [])
        state = _State(s=_Slot(project=str(proj), repos=held))
        async with TestClient(TestServer(_app(state))) as client:
            body = await (await client.get("/api/project/git/repos?slot=s")).json()
        assert [row["source"] for row in body["repos"]] == ["project", "agent"]
        assert body["listed"] == (2 if touched_project else 1)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("masked", ["agent", "project"])
    async def test_a_path_the_redactor_masks_is_refused_and_announced(
        self, tmp_path, mock_sel, masked
    ):
        """A masked path is not one the status and log routes accept, so its row
        would only ever show an error. It is left out, out of ``listed``, and the
        listing says it is incomplete."""
        # Shaped like an access key id, so the credential redactor masks it.
        credential_shaped = "AKIA" + "IOSFODNN7" + "EXAMPLE"
        plain = _init_repo(tmp_path / "plain")
        secretish = _init_repo(tmp_path / credential_shaped)
        if masked == "agent":
            slot = _Slot(project=str(plain), repos=[_real(plain), _real(secretish)])
            expected = [{"path": _real(plain), "source": "project"}]
        else:
            slot = _Slot(project=_real(secretish), repos=[_real(secretish), _real(plain)])
            expected = [{"path": _real(plain), "source": "agent"}]
        async with TestClient(TestServer(_app(_State(s=slot)))) as client:
            body = await (await client.get("/api/project/git/repos?slot=s")).json()
        assert body["repos"] == expected
        assert credential_shaped not in json.dumps(body)
        assert body["listed"] == 1
        assert body["incomplete"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("seeded", [False, True])
    async def test_reports_capacity_evictions_on_every_listing(self, tmp_path, mock_sel, seeded):
        roots = []
        for i in range(touches.MAX_SLOT_GIT_REPOS + 2):
            root = tmp_path / f"r{i}"
            (root / ".git").mkdir(parents=True)
            roots.append(_real(root))
        slot = _Slot()
        if seeded:
            slot.messages = [
                {"role": "assistant", "meta": {"file_changes": [{"path": str(Path(root) / "f")}]}}
                for root in roots
            ]
        else:
            touches.record_repo_roots(slot, roots)
        async with TestClient(TestServer(_app(_State(s=slot)))) as client:
            for _ in range(2):
                body = await (await client.get("/api/project/git/repos?slot=s")).json()
                assert body["omitted"] == 2
                # The notice reads both of its numbers from the response.
                assert body["listed"] == touches.MAX_SLOT_GIT_REPOS
                assert "limit" not in body
                assert body["repos"] == [
                    {"path": root, "source": "agent"} for root in reversed(roots[2:])
                ]

    @pytest.mark.asyncio
    async def test_restored_slot_lists_its_newest_repeated_root_first(self, tmp_path, mock_sel):
        a = _init_repo(tmp_path / "a")
        b = _init_repo(tmp_path / "b")
        # Oldest row first on disk; the seed reads it newest first:
        # a/file2, b/file, a/file1.
        messages = [
            {"role": "assistant", "meta": {"file_changes": [{"path": str(a / "file1")}]}},
            {"role": "assistant", "meta": {"file_changes": [{"path": str(b / "file")}]}},
            {"role": "assistant", "meta": {"file_changes": [{"path": str(a / "file2")}]}},
        ]
        slot = _Slot(messages=messages)
        async with TestClient(TestServer(_app(_State(s=slot)))) as client:
            for _ in range(2):
                body = await (await client.get("/api/project/git/repos?slot=s")).json()
                assert body["repos"] == [
                    {"path": _real(a), "source": "agent"},
                    {"path": _real(b), "source": "agent"},
                ]
        assert slot._git_repos == [_real(b), _real(a)]

    @pytest.mark.asyncio
    async def test_restored_slot_evicts_its_oldest_root_not_the_newest(
        self, tmp_path, mock_sel, monkeypatch
    ):
        monkeypatch.setattr(touches, "MAX_SLOT_GIT_REPOS", 2)
        a, b, c = (_init_repo(tmp_path / name) for name in ("a", "b", "c"))
        # Newest first: a/new, b/f, c/f, a/old. ``c`` is the oldest repository.
        messages = [
            {"role": "assistant", "meta": {"file_changes": [{"path": str(path)}]}}
            for path in (a / "old", c / "f", b / "f", a / "new")
        ]
        slot = _Slot(messages=messages)
        async with TestClient(TestServer(_app(_State(s=slot)))) as client:
            body = await (await client.get("/api/project/git/repos?slot=s")).json()
        assert body["repos"] == [
            {"path": _real(a), "source": "agent"},
            {"path": _real(b), "source": "agent"},
        ]
        assert body["omitted"] == 1
        assert slot._git_repos_evicted == [_real(c)]

    @pytest.mark.asyncio
    async def test_transcript_seeds_a_restored_slot_once(self, tmp_path, mock_sel):
        repo = _init_repo(tmp_path / "repo")
        messages = [{"role": "assistant", "meta": {"file_changes": [{"path": str(repo / "f")}]}}]
        slot = _Slot(messages=messages)
        state = _State(s=slot)
        async with TestClient(TestServer(_app(state))) as client:
            body = await (await client.get("/api/project/git/repos?slot=s")).json()
            assert body["repos"] == [{"path": _real(repo), "source": "agent"}]
            assert slot._git_repos == [_real(repo)]
            assert slot._git_repos_seeded is True
            slot.messages = []  # a second call must not re-read the transcript
            body = await (await client.get("/api/project/git/repos?slot=s")).json()
            assert body["repos"] == [{"path": _real(repo), "source": "agent"}]

    @pytest.mark.asyncio
    async def test_unknown_slot_404(self, mock_sel):
        async with TestClient(TestServer(_app(_State()))) as client:
            resp = await client.get("/api/project/git/repos?slot=nope")
            assert resp.status == 404
            assert (await resp.json())["code"] == "slot_not_found"

    @pytest.mark.asyncio
    async def test_missing_slot_param_400(self, mock_sel):
        async with TestClient(TestServer(_app(_State()))) as client:
            resp = await client.get("/api/project/git/repos")
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_app_caller_cannot_read_a_foreign_slot(self, tmp_path, mock_sel):
        app = _app(_State(s=_Slot(app="other-app")), request_app="some-app")
        with patch("kiro_crew.dashboard.slot_ownership.sel") as iso_sel:
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/project/git/repos?slot=s")
                assert resp.status == 404
                assert (await resp.json())["code"] == "slot_not_found"
        # The refusal of a slot that exists is an audited permission decision.
        denial = iso_sel.return_value.log_api_access.call_args.kwargs
        assert (denial["outcome"], denial["operation"]) == ("denied", "project_git_repos")

    @pytest.mark.asyncio
    async def test_app_caller_reads_its_own_slot(self, tmp_path, mock_sel):
        repo = _init_repo(tmp_path / "repo")
        app = _app(_State(s=_Slot(app="some-app", repos=[_real(repo)])), request_app="some-app")
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/project/git/repos?slot=s")
            assert resp.status == 200
            assert (await resp.json())["repos"] == [{"path": _real(repo), "source": "agent"}]


class TestStatusAdmitsTouchedRoots:
    @pytest.fixture(autouse=True)
    def passthrough_sandbox(self, _floor_monkeypatch):
        from kiro_crew.dashboard.handlers import files as files_mod

        _floor_monkeypatch.setattr(
            files_mod,
            "sandboxed_spawn_argv",
            lambda argv, mode="standard", **kw: (
                list(argv),
                dict(kw.get("env") or os.environ),
                None,
            ),
        )

    @pytest.mark.asyncio
    async def test_touched_root_is_readable(self, tmp_path, mock_sel):
        repo = _init_repo(tmp_path / "repo")
        (repo / "new.txt").write_text("x\n")
        state = _State(s=_Slot(project=str(tmp_path), repos=[_real(repo)]))
        async with TestClient(TestServer(_app(state))) as client:
            resp = await client.get(f"/api/project/git/status?path={_real(repo)}")
            assert resp.status == 200
            body = await resp.json()
        assert body["repo"] is True
        assert [f["path"] for f in body["files"]] == ["new.txt"]

    @pytest.mark.asyncio
    async def test_user_file_changes_cannot_list_or_admit_an_untouched_repo(
        self, tmp_path, mock_sel
    ):
        agent_repo = _init_repo(tmp_path / "agent")
        forged_repo = _init_repo(tmp_path / "forged")
        slot = _Slot(project=str(tmp_path))
        slot._file_changes = [{"path": str(agent_repo / "f"), "content": ""}]
        _flush_file_changes(slot)
        assert slot.messages[-1]["role"] == "assistant"
        assert slot.messages[-1]["meta"]["file_changes"][0]["path"] == str(agent_repo / "f")
        slot.append(
            "user",
            "hi",
            broadcast=False,
            meta={"file_changes": [{"path": str(forged_repo / "f")}]},
        )
        async with TestClient(TestServer(_app(_State(s=slot)))) as client:
            for route in ("status", "log"):
                resp = await client.get(f"/api/project/git/{route}?path={_real(forged_repo)}")
                assert resp.status == 403
            body = await (await client.get("/api/project/git/repos?slot=s")).json()
            assert body["repos"] == [{"path": _real(agent_repo), "source": "agent"}]
            assert slot._git_repos == [_real(agent_repo)]
            for route in ("status", "log"):
                resp = await client.get(f"/api/project/git/{route}?path={_real(forged_repo)}")
                assert resp.status == 403
                assert (await resp.json())["code"] == "unknown_project_dir"
                resp = await client.get(f"/api/project/git/{route}?path={_real(agent_repo)}")
                assert resp.status == 200
                assert (await resp.json())["repo"] is True

    @pytest.mark.asyncio
    async def test_untouched_repo_is_still_refused(self, tmp_path, mock_sel):
        repo = _init_repo(tmp_path / "repo")
        state = _State(s=_Slot(project=str(tmp_path)))
        async with TestClient(TestServer(_app(state))) as client:
            resp = await client.get(f"/api/project/git/status?path={_real(repo)}")
            assert resp.status == 403

    @pytest.mark.asyncio
    async def test_app_caller_cannot_read_a_root_another_slot_touched(self, tmp_path, mock_sel):
        repo = _init_repo(tmp_path / "repo")
        state = _State(s=_Slot(app="other-app", repos=[_real(repo)]))
        async with TestClient(TestServer(_app(state, request_app="some-app"))) as client:
            resp = await client.get(f"/api/project/git/status?path={_real(repo)}")
            assert resp.status == 403

    @pytest.mark.asyncio
    async def test_app_caller_reads_a_root_its_own_slot_touched(self, tmp_path, mock_sel):
        repo = _init_repo(tmp_path / "repo")
        state = _State(s=_Slot(app="some-app", repos=[_real(repo)]))
        async with TestClient(TestServer(_app(state, request_app="some-app"))) as client:
            resp = await client.get(f"/api/project/git/status?path={_real(repo)}")
            assert resp.status == 200


class TestExecutedToolTouches:
    """Drive the runner's real proposal/refinement/result seams and route boundary."""

    @staticmethod
    async def _walked(slot):
        task = slot._git_repo_walk_task
        if task is not None:
            await await_until(task, what="the runner's repository walker draining")

    @staticmethod
    async def _run(tmp_path, monkeypatch, stream):
        from kiro_crew.dashboard.chat import _run_chat

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.context_builder = None
        state.consolidator = None
        state._hook_store = None
        state._yolo = False
        slot = state.get_or_create_slot("s")
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=0.0)
        client.context_window_tokens = MagicMock(return_value=0)
        client.context_used_tokens = MagicMock(return_value=0)
        client.mcp_session_report = MagicMock(return_value=None)
        client.available_models = MagicMock(return_value=[])
        client.client.pop_pending_oauth_requests = MagicMock(return_value=[])

        async def run_stream(msg):
            async for event in stream(slot):
                if event.kind == EVENT_COMPLETE:
                    yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Done.")
                yield event

        client.stream = run_stream
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        await await_until(
            asyncio.ensure_future(_run_chat(state, slot, "hello")),
            what="the tool-touch regression turn completing",
        )
        await TestExecutedToolTouches._walked(slot)
        return slot

    @staticmethod
    def _proposal(call_id, repo, *, refinement=False):
        return LLMEvent(
            kind=EVENT_TOOL_CALL_UPDATE if refinement else EVENT_TOOL_CALL,
            tool_call_id=call_id,
            title="Terminal",
            tool_kind="execute",
            is_shell=True,
            raw_tool_params={"command": f"cd '{repo}' && git status"},
        )

    @staticmethod
    def _result(call_id, status):
        return LLMEvent(
            kind=EVENT_TOOL_RESULT,
            tool_call_id=call_id,
            tool_status=status,
            tool_final=status == "completed",
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["refused", "cancelled", "canceled", "failed", ""])
    async def test_denied_or_unexecuted_call_never_lists_or_admits_repo(
        self, tmp_path, monkeypatch, mock_sel, status
    ):
        repo = _init_repo(tmp_path / "other")

        async def stream(slot):
            yield self._proposal("denied", repo)
            if status:
                yield self._result("denied", status)
            yield LLMEvent(kind=EVENT_COMPLETE)

        slot = await self._run(tmp_path, monkeypatch, stream)
        assert slot._git_repos == []
        slot._app = "touch-app"
        async with TestClient(TestServer(_app(_State(s=slot), request_app="touch-app"))) as client:
            body = await (await client.get("/api/project/git/repos?slot=s")).json()
            assert body["repos"] == []
            for route in ("status", "log"):
                response = await client.get(f"/api/project/git/{route}?path={_real(repo)}")
                assert response.status == 403
                assert (await response.json())["code"] == "unknown_project_dir"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("refinement", [False, True])
    async def test_completed_call_records_only_after_execution(
        self, tmp_path, monkeypatch, refinement
    ):
        repo = _init_repo(tmp_path / "completed")

        async def stream(slot):
            if refinement:
                yield LLMEvent(kind=EVENT_TOOL_CALL, tool_call_id="done", title="Terminal")
            yield self._proposal("done", repo, refinement=refinement)
            await self._walked(slot)
            assert slot._git_repos == [], "a proposal is not execution"
            yield self._result("done", "in_progress")
            assert slot._git_repos == [], "partial output is not a completed call"
            yield self._result("done", "completed")
            await self._walked(slot)
            assert slot._git_repos == [_real(repo)]
            assert slot._git_repo_touch_pending == {}
            yield LLMEvent(kind=EVENT_COMPLETE)

        slot = await self._run(tmp_path, monkeypatch, stream)
        assert slot._git_repos == [_real(repo)]

    @pytest.mark.asyncio
    async def test_two_completed_calls_preserve_completion_order(self, tmp_path, monkeypatch):
        a = _init_repo(tmp_path / "a")
        b = _init_repo(tmp_path / "b")

        async def stream(slot):
            yield self._proposal("a", a)
            yield self._proposal("b", b)
            await self._walked(slot)
            assert slot._git_repos == []
            yield self._result("b", "completed")
            yield self._result("a", "completed")
            await self._walked(slot)
            assert slot._git_repos == [_real(b), _real(a)]
            yield LLMEvent(kind=EVENT_COMPLETE)

        await self._run(tmp_path, monkeypatch, stream)

    @pytest.mark.asyncio
    async def test_pending_call_ids_are_bounded_oldest_first(self, tmp_path, monkeypatch):
        async def stream(slot):
            cap = getattr(touches, "MAX_PENDING_TOOL_CALLS", 64)
            for i in range(cap + 3):
                yield self._proposal(f"call-{i}", tmp_path / f"repo-{i}")
                assert len(slot._git_repo_touch_pending) <= cap
            assert list(slot._git_repo_touch_pending) == [f"call-{i}" for i in range(3, cap + 3)]
            assert slot._git_repo_walk_task is None
            yield self._result("call-3", "cancelled")
            assert "call-3" not in slot._git_repo_touch_pending
            yield LLMEvent(kind=EVENT_COMPLETE)

        slot = await self._run(tmp_path, monkeypatch, stream)
        assert slot._git_repo_touch_pending == {}, "turn end drops calls that never ran"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["completed", "refused", "cancelled"])
    async def test_terminal_update_can_supply_the_first_path(self, tmp_path, monkeypatch, status):
        from kiro_crew.acp._dispatch import parse_session_update

        repo = _init_repo(tmp_path / "late-input")
        events = parse_session_update(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "late",
                "status": status,
                "title": "bash",
                "kind": "execute",
                "rawInput": {"command": f"cd '{repo}' && git status"},
            }
        )
        assert [event.kind for event in events] == [EVENT_TOOL_RESULT, EVENT_TOOL_CALL_UPDATE]

        async def stream(slot):
            yield LLMEvent(kind=EVENT_TOOL_CALL, tool_call_id="late", title="Terminal")
            yield events[0]
            assert slot._git_repo_touch_terminal["late"] == (
                "completed" if status == "completed" else "refused"
            )
            yield events[1]
            await self._walked(slot)
            assert slot._git_repos == ([_real(repo)] if status == "completed" else [])
            assert slot._git_repo_touch_pending == {}
            yield LLMEvent(kind=EVENT_COMPLETE)

        await self._run(tmp_path, monkeypatch, stream)

    def test_staging_keeps_bounded_strings_without_filesystem_work(self, monkeypatch):
        slot = _repo_slot()
        params: dict[str, Any] = {
            "path": [f"~repo-touch-user/{i}" for i in range(100)],
            "content": "body",
        }

        def forbidden(*args, **kwargs):
            raise AssertionError("filesystem work on the event loop")

        with monkeypatch.context() as guard:
            guard.setattr(touches.os.path, "expanduser", forbidden)
            guard.setattr(touches.os.path, "realpath", forbidden)
            guard.setattr(touches.os.path, "exists", forbidden)
            touches.note_tool_call(slot, params, tool_call_id="strings", tool_kind="edit")
            touches.note_tool_call(
                slot, {"path": "/another"}, tool_call_id="strings", tool_kind="edit"
            )
        params["path"].clear()
        assert slot._git_repo_touch_pending["strings"] == [
            f"~repo-touch-user/{i}" for i in range(touches._MAX_CANDIDATES_PER_CALL)
        ]
        assert all(isinstance(p, str) for p in slot._git_repo_touch_pending["strings"])
        assert slot._git_repo_walk_task is None

    def test_terminal_receipts_are_bounded_and_missing_ids_record_nothing(self):
        slot = _repo_slot()
        for i in range(touches.MAX_PENDING_TOOL_CALLS + 3):
            touches.finish_tool_call(slot, f"done-{i}", executed=False)
        assert list(slot._git_repo_touch_terminal) == [
            f"done-{i}" for i in range(3, touches.MAX_PENDING_TOOL_CALLS + 3)
        ]
        for call_id in ("", "x" * (touches.MAX_REPO_PATH_CHARS + 1)):
            touches.note_tool_call(slot, {"path": "/r/f"}, tool_call_id=call_id, tool_kind="edit")
            touches.finish_tool_call(slot, call_id, executed=True)
        assert slot._git_repo_touch_pending == {}
        assert slot._git_repo_walk_task is None


class TestIncompleteTracking:
    @pytest.mark.asyncio
    async def test_patch_with_sixteen_paths_in_a_and_one_in_b_warns(self, tmp_path, mock_sel):
        a = _init_repo(tmp_path / "a")
        b = _init_repo(tmp_path / "b")
        paths = [str(a / f"f{i}") for i in range(touches._MAX_CANDIDATES_PER_CALL)]
        paths.append(str(b / "f"))
        patch_text = "\n".join(
            ["*** Begin Patch", *[f"*** Add File: {path}\n+x" for path in paths], "*** End Patch"]
        )
        slot = _Slot()
        _executed_touch(slot, {"patchText": patch_text}, tool_kind="edit")
        await TestNoteToolCall._drained(slot)
        async with TestClient(TestServer(_app(_State(s=slot)))) as client:
            for _ in range(2):
                body = await (await client.get("/api/project/git/repos?slot=s")).json()
                assert body["repos"] == [{"path": _real(a), "source": "agent"}]
                assert body["omitted"] == 0
                assert body["incomplete"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["refused", "failed", "cancelled", ""])
    async def test_cut_unexecuted_calls_do_not_warn(self, tmp_path, monkeypatch, mock_sel, status):
        async def stream(slot):
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id="cut",
                title="Write",
                tool_kind="edit",
                raw_tool_params={
                    "path": [
                        str(tmp_path / f"f{i}") for i in range(touches._MAX_CANDIDATES_PER_CALL + 1)
                    ]
                },
            )
            if status:
                yield TestExecutedToolTouches._result("cut", status)
            yield LLMEvent(kind=EVENT_COMPLETE)

        slot = await TestExecutedToolTouches._run(tmp_path, monkeypatch, stream)
        assert slot._git_repo_touch_pending == {}
        assert slot._git_repo_touch_overflow is False
        async with TestClient(TestServer(_app(_State(s=slot)))) as client:
            body = await (await client.get("/api/project/git/repos?slot=s")).json()
        assert body["repos"] == []
        assert body["incomplete"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("empty", [False, True])
    @pytest.mark.parametrize("incomplete", [False, True])
    async def test_response_always_has_incomplete(self, tmp_path, mock_sel, empty, incomplete):
        repo = _init_repo(tmp_path / "repo")
        slot = _Slot(repos=[] if empty else [_real(repo)])
        slot._git_repos_incomplete = incomplete
        async with TestClient(TestServer(_app(_State(s=slot)))) as client:
            body = await (await client.get("/api/project/git/repos?slot=s")).json()
        assert body["incomplete"] is incomplete

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "source",
        [
            "edit",
            "edit_length",
            "diff",
            "cwd",
            "dirs",
            "text",
            "argv",
            "entries",
            "paths",
            "patch",
            "patch_type",
        ],
    )
    async def test_every_extraction_bound_carries_loss_until_execution(self, monkeypatch, source):
        cap = touches._MAX_CANDIDATES_PER_CALL
        oversized = "/" + "x" * touches.MAX_REPO_PATH_CHARS
        kwargs: dict[str, Any] = {"tool_kind": "edit"}
        params: dict[str, Any] = {}
        if source == "edit":
            params = {"path": [f"/r/{i}" for i in range(cap + 1)]}
        elif source == "edit_length":
            params = {"path": oversized}
        elif source == "diff":
            kwargs["diff_path"] = oversized
        elif source == "cwd":
            params = {"working_dir": oversized}
            kwargs = {"is_shell": True}
        elif source == "dirs":
            params = {"command": ";".join(f"cd /r/{i}" for i in range(cap + 1))}
            kwargs = {"is_shell": True}
        elif source == "text":
            params = {"command": "x" * (touches._MAX_COMMAND_CHARS + 1)}
            kwargs = {"is_shell": True}
        elif source == "argv":
            params = {"command": ["bash"] * cap + ["cd /r/hidden"]}
            kwargs = {"is_shell": True}
        elif source == "entries":
            monkeypatch.setattr(tool_paths, "_TARGET_PATH_MAX_NODES", 8)
            params = {"items": [None] * 20 + [{"path": "/r/hidden"}]}
        elif source == "paths":
            monkeypatch.setattr(tool_paths, "_TARGET_PATH_MAX_PATHS", 2)
            params = {"path": ["/r/first", "/r/second", "/r/hidden"]}
        elif source == "patch_type":
            params = {"patchText": None}
        else:
            params = {"patchText": "x" * (_PATCH_TEXT_MAX_CHARS + 1)}
        slot = _repo_slot()
        touches.note_tool_call(slot, params, tool_call_id="cut", **kwargs)
        staged = slot._git_repo_touch_pending["cut"]
        assert staged.truncated is True
        assert len(staged) <= cap
        assert slot._git_repos_incomplete is False
        touches.finish_tool_call(slot, "cut", executed=False)
        assert slot._git_repos_incomplete is False
        # A completed receipt can precede its first refined input.
        touches.finish_tool_call(slot, "cut", executed=True)
        touches.note_tool_call(slot, params, tool_call_id="cut", **kwargs)
        assert slot._git_repos_incomplete is True
        await TestExecutedToolTouches._walked(slot)

    def test_exact_candidate_cap_and_non_bound_exclusions_do_not_warn(self, tmp_path):
        params = {
            "path": [str(tmp_path / f"f{i}") for i in range(touches._MAX_CANDIDATES_PER_CALL)]
        }
        candidates = touches.touch_candidates(params, tool_kind="edit")
        assert candidates.truncated is False
        assert (
            touches.touch_candidates({"command": "cd relative"}, is_shell=True).truncated is False
        )
        assert touches.resolve_repo_roots(["relative"]).truncated is False
        invalid = "*** Begin Patch\n*** Add File: relative\n+x\n*** End Patch"
        candidates = touches.touch_candidates({"patchText": invalid}, diff_path="/r/diff")
        assert candidates == []
        assert candidates.truncated is False

    def test_refinement_merge_keeps_the_loss_bit_even_without_paths(self):
        slot = _repo_slot()
        oversized = "/" + "x" * touches.MAX_REPO_PATH_CHARS
        touches.note_tool_call(slot, {}, tool_call_id="merge", diff_path=oversized)
        touches.note_tool_call(slot, {"path": "/r/f"}, tool_call_id="merge", tool_kind="edit")
        assert slot._git_repo_touch_pending["merge"].truncated is True
        slot = _repo_slot()
        touches.note_tool_call(
            slot,
            {"path": [f"/r/{i}" for i in range(touches._MAX_CANDIDATES_PER_CALL)]},
            tool_call_id="merge",
            tool_kind="edit",
        )
        touches.note_tool_call(slot, {"path": "/another/f"}, tool_call_id="merge", tool_kind="edit")
        assert slot._git_repo_touch_pending["merge"].truncated is True
        assert slot._git_repos_incomplete is False

    @pytest.mark.parametrize("executed", [False, True])
    def test_evicted_proposal_warns_only_when_executed(self, monkeypatch, executed):
        monkeypatch.setattr(touches, "MAX_PENDING_TOOL_CALLS", 2)
        slot = _repo_slot()
        for i in range(3):
            touches.note_tool_call(slot, {"path": f"/r/{i}"}, tool_call_id=str(i), tool_kind="edit")
        assert list(slot._git_repo_touch_pending) == ["1", "2"]
        assert slot._git_repos_incomplete is False
        touches.finish_tool_call(slot, "0", executed=executed)
        assert slot._git_repos_incomplete is executed

    @pytest.mark.asyncio
    async def test_executed_backlog_eviction_warns(self, monkeypatch):
        monkeypatch.setattr(touches, "_MAX_PENDING_CANDIDATES_PER_SLOT", 2)
        slot = _repo_slot()
        for i in range(3):
            _executed_touch(slot, {"working_dir": f"/r/{i}"}, is_shell=True)
        assert slot._git_repo_walk_pending == ["/r/1", "/r/2"]
        assert slot._git_repos_incomplete is True
        await TestNoteToolCall._drained(slot)

    @pytest.mark.parametrize("executed", [False, True])
    def test_terminal_receipt_eviction_is_not_a_loss(self, monkeypatch, executed):
        monkeypatch.setattr(touches, "MAX_PENDING_TOOL_CALLS", 2)
        slot = _repo_slot()
        touches.finish_tool_call(slot, "old", executed=executed)
        for call_id in ("next", "last"):
            touches.finish_tool_call(slot, call_id, executed=False)
        assert "old" not in slot._git_repo_touch_terminal
        assert slot._git_repos_incomplete is False

    def test_long_turn_of_pathless_calls_stays_complete(self):
        # An ordinary long turn: many completed calls that name no path (a
        # search, a think step). Their receipts roll over the bound without
        # any path ever being lost, so the listing must not read as incomplete.
        slot = _repo_slot()
        for i in range(touches.MAX_PENDING_TOOL_CALLS * 2 + 1):
            touches.note_tool_call(slot, {"query": "x"}, tool_call_id=f"c{i}", tool_kind="search")
            touches.finish_tool_call(slot, f"c{i}", executed=True)
        assert len(slot._git_repo_touch_terminal) == touches.MAX_PENDING_TOOL_CALLS
        assert slot._git_repos_incomplete is False

    @pytest.mark.parametrize("source", ["raw", "expanded", "root", "depth", "seed"])
    def test_worker_and_seed_bounds_keep_loss(self, tmp_path, monkeypatch, source):
        oversized = "/" + "x" * touches.MAX_REPO_PATH_CHARS
        candidates = [oversized] if source == "raw" else ["f"]
        if source == "expanded":
            monkeypatch.setattr(touches, "MAX_REPO_PATH_CHARS", len(str(tmp_path)))
        elif source == "root":
            monkeypatch.setattr(touches, "find_git_root", lambda p: oversized)
        elif source == "depth":
            monkeypatch.setattr(touches, "_GIT_ROOT_WALK_LIMIT", 0)
        elif source == "seed":
            monkeypatch.setattr(touches, "_MAX_SEED_PATHS", 1)
            candidates = touches.transcript_change_paths(
                [
                    {
                        "role": "assistant",
                        "meta": {
                            "file_changes": [
                                {"path": str(tmp_path / "first")},
                                {"path": str(tmp_path / "second")},
                            ]
                        },
                    }
                ]
            )
        roots = touches.resolve_repo_roots(candidates, str(tmp_path))
        assert roots.truncated is True
        slot = _repo_slot()
        touches.record_repo_roots(slot, roots)
        assert slot._git_repos_incomplete is True

    @pytest.mark.asyncio
    async def test_turn_cleanup_discards_evicted_unexecuted_proposals(self, tmp_path, monkeypatch):
        monkeypatch.setattr(touches, "MAX_PENDING_TOOL_CALLS", 2)

        async def stream(slot):
            for i in range(3):
                yield TestExecutedToolTouches._proposal(str(i), tmp_path / f"repo-{i}")
            assert slot._git_repo_touch_overflow is True
            assert slot._git_repos_incomplete is False
            yield LLMEvent(kind=EVENT_COMPLETE)

        slot = await TestExecutedToolTouches._run(tmp_path, monkeypatch, stream)
        assert slot._git_repo_touch_overflow is False
        assert slot._git_repo_touch_pending == {}
        assert slot._git_repos_incomplete is False

    def test_oversized_seed_paths_are_incomplete_even_without_retained_paths(self):
        paths = touches.transcript_change_paths(
            [
                {
                    "role": "assistant",
                    "meta": {"file_changes": [{"path": "/" + "x" * touches.MAX_REPO_PATH_CHARS}]},
                }
            ]
        )
        assert paths == []
        assert paths.truncated is True
