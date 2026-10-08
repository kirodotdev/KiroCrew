"""The Git tab's repository discovery: which repos a session's tool calls touched.

Covers ``kiro_crew.dashboard.git_repo_touches`` (candidate extraction, root
resolution, slot bookkeeping, transcript seeding) and the two routes that read
it: ``GET /api/project/git/repos`` lists a slot's repositories, and
``GET /api/project/git/status`` admits a touched root that is not any slot's
project directory.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import git_repo_touches as touches
from kiro_crew.dashboard.handlers import api_project_git_repos, api_project_git_status
from kiro_crew.dashboard.handlers.files import _project_git_branch
from kiro_crew.dashboard.state import _ChatSlot


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
    subprocess.run(
        ["git", "init", "-q", "-b", "trunk"],
        cwd=root,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull},
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

    def test_non_shell_ignores_command_text(self):
        params = {"command": "cd /r/one && ls"}
        assert touches.touch_candidates(params, tool_kind="other") == []

    def test_candidates_are_deduplicated_and_capped(self):
        many = " ; ".join(f"cd /r/{i}" for i in range(40))
        params = {"command": f"cd /r/0 && {many}", "working_dir": "/r/0"}
        found = touches.touch_candidates(params, is_shell=True)
        assert found[0] == "/r/0"
        assert len(found) == len(set(found)) == touches._MAX_CANDIDATES_PER_CALL


# ── resolve_repo_roots ──


class TestResolveRepoRoots:
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

    def test_distinct_roots_in_order(self, tmp_path):
        a = _init_repo(tmp_path / "a")
        b = _init_repo(tmp_path / "b")
        roots = touches.resolve_repo_roots([str(a / "1"), str(b / "2"), str(a / "3")])
        assert roots == [_real(a), _real(b)]


# ── record_repo_roots / transcript seed ──


class TestRecordRepoRoots:
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
        assert slot._git_repos == list(reversed(roots[: touches.MAX_SLOT_GIT_REPOS]))
        assert touches.omitted_repo_count(slot) == 3

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


class TestNoteToolCall:
    @pytest.mark.asyncio
    async def test_records_the_repo_a_write_lands_in(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        slot = _repo_slot()
        await touches.note_tool_call(
            slot, {"command": "create", "path": str(repo / "x.py")}, tool_kind="edit"
        )
        assert slot._git_repos == [_real(repo)]

    @pytest.mark.asyncio
    async def test_never_raises(self, monkeypatch):
        def _boom(*_a, **_k):
            raise RuntimeError("walk failed")

        monkeypatch.setattr(touches, "resolve_repo_roots", _boom)
        slot = _repo_slot()
        await touches.note_tool_call(slot, {"working_dir": "/x"}, is_shell=True)
        assert slot._git_repos == []


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
    if request_app:

        @web.middleware
        async def as_app(request, handler):
            request["app"] = request_app
            return await handler(request)

        app.middlewares.append(as_app)
    return app


class TestReposRoute:
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
                {"meta": {"file_changes": [{"path": str(Path(root) / "f")}]}} for root in roots
            ]
        else:
            touches.record_repo_roots(slot, roots)
        async with TestClient(TestServer(_app(_State(s=slot)))) as client:
            for _ in range(2):
                body = await (await client.get("/api/project/git/repos?slot=s")).json()
                assert body["omitted"] == 2
                assert body["repos"] == [
                    {"path": root, "source": "agent"} for root in reversed(roots[2:])
                ]

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
