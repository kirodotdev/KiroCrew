"""Host-independent tests for the worktree LIST and REMOVE paths.

Why a second file next to ``test_worktree_create.py``: that suite drives the real
sandboxed git, so every test in it calls ``_require_sandbox_exec()`` and SKIPS on a
host with no sandbox backend — which is most CI shards. The endpoint logic that
skipping leaves unexecuted is exactly the parsing and refusal reasoning that most
needs pinning, so this file fakes ``_run_git`` instead and therefore runs
everywhere. No real repository, no spawn, no skip.

The seam is deliberate: ``_run_git`` is the single chokepoint every git call in the
module goes through, so replacing it exercises the callers' own logic while leaving
the isolation contract (argv-only, credential-scrubbed, resource-capped) to the
sandbox tests in the sibling file.
"""

from __future__ import annotations

import os
import subprocess
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers.worktree import (
    _active_slot_beneath,
    _active_worktree_slots,
    _list_worktrees_detailed,
    _list_worktrees_sync,
    _norm_path,
    _remove_worktree_sync,
    _sync_result_response,
    _worktree_dirty,
    api_worktree_list,
    api_worktree_remove,
)


def _proc(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["git"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _porcelain(*entries: dict) -> str:
    """Build ``git worktree list --porcelain -z`` output.

    Every attribute is NUL-terminated and an extra NUL separates records, which is
    the shape the parser is written against — building it here rather than pasting a
    literal keeps the test honest about that framing.
    """
    out = []
    for e in entries:
        out.append(f"worktree {e['path']}\0")
        if e.get("head"):
            out.append(f"HEAD {e['head']}\0")
        if e.get("branch"):
            out.append(f"branch {e['branch']}\0")
        for flag in ("bare", "detached", "locked"):
            if e.get(flag):
                out.append(f"{flag}\0")
        out.append("\0")
    return "".join(out)


class _GitStub:
    """Records calls and answers from a queue keyed by the git subcommand."""

    def __init__(self, **answers):
        self.answers = answers
        self.calls: list[tuple[tuple[str, ...], str]] = []

    def __call__(self, args, cwd):
        self.calls.append((tuple(args), cwd))
        for key, val in self.answers.items():
            if key.replace("_", " ") in " ".join(args) or key == args[0]:
                return val(self) if callable(val) else val
        return _proc()

    def argv_for(self, needle: str) -> tuple[str, ...] | None:
        for args, _ in self.calls:
            if needle in " ".join(args):
                return args
        return None


@pytest.fixture
def git(monkeypatch):
    """Install a ``_GitStub`` over ``_run_git`` for the module under test."""

    def install(**answers):
        from kiro_crew.dashboard.handlers import worktree as wt

        stub = _GitStub(**answers)
        monkeypatch.setattr(wt, "_run_git", stub)
        # The dirty probe consults the filter gate first; default it to "no filter
        # driver" so a test that does not care about it still reaches `git status`.
        monkeypatch.setattr(wt, "_checkout_filter", lambda root: "")
        return stub

    return install


class TestListWorktreesDetailedParsing:
    def test_parses_branch_head_and_flags_in_git_order(self, git):
        git(
            worktree_list=_proc(
                _porcelain(
                    {"path": "/repo", "head": "a" * 40, "branch": "refs/heads/main"},
                    {"path": "/repo-wt-x", "head": "b" * 40, "branch": "refs/heads/feat/x"},
                    {"path": "/repo-wt-d", "head": "c" * 40, "detached": True},
                )
            )
        )
        recs = _list_worktrees_detailed("/repo")

        assert [r["path"] for r in recs] == ["/repo", "/repo-wt-x", "/repo-wt-d"]
        # `refs/heads/` is stripped; a detached entry carries no branch at all.
        assert [r["branch"] for r in recs] == ["main", "feat/x", ""]
        assert recs[2]["detached"] is True
        assert recs[0]["head"] == "a" * 40

    def test_locked_with_and_without_a_reason_both_read_as_locked(self, git):
        git(worktree_list=_proc("worktree /a\0locked\0\0worktree /b\0locked being repaired\0\0"))
        recs = _list_worktrees_detailed("/repo")

        assert [r["locked"] for r in recs] == [True, True]

    def test_a_path_containing_a_newline_stays_one_record(self, git):
        # This is the whole reason the query passes `-z`; a line-based parser would
        # split this into two bogus worktrees.
        git(worktree_list=_proc(_porcelain({"path": "/a\nb", "head": "d" * 40})))
        recs = _list_worktrees_detailed("/repo")

        assert [r["path"] for r in recs] == ["/a\nb"]

    def test_git_failure_is_none_not_an_empty_list(self, git):
        # The remove path keys a destructive decision off this answer, so "git could
        # not tell us" must never be indistinguishable from "nothing is registered".
        git(worktree_list=_proc("", returncode=128))

        assert _list_worktrees_detailed("/repo") is None

    def test_attributes_before_any_worktree_line_are_ignored(self, git):
        git(worktree_list=_proc("branch refs/heads/stray\0worktree /a\0\0"))
        recs = _list_worktrees_detailed("/repo")

        assert [r["path"] for r in recs] == ["/a"]
        assert recs[0]["branch"] == ""


class TestWorktreeDirty:
    """Every case here needs a REAL directory.

    The probe refuses to spawn git at all when the worktree path is not a
    directory (git keeps registrations for deleted trees, and a dead cwd raises
    rather than returning non-zero). A fabricated path like ``/wt`` would take
    that early exit and every assertion below would pass for the wrong reason.
    """

    def test_clean_tree_is_false_and_dirty_tree_is_true(self, git, tmp_path):
        git(status=_proc(""))
        assert _worktree_dirty(str(tmp_path)) is False

        git(status=_proc(" M file.py\n?? other\n"))
        assert _worktree_dirty(str(tmp_path)) is True

    def test_status_failure_is_unknown(self, git, tmp_path):
        git(status=_proc("", returncode=128))
        assert _worktree_dirty(str(tmp_path)) is None

    def test_a_missing_worktree_directory_is_unknown_and_spawns_nothing(self, git, tmp_path):
        """A registered tree whose directory is gone must not reach git.

        Both probes below run with the worktree as cwd, which RAISES instead of
        failing softly -- so this early exit is what keeps one stale registration
        from 500-ing the whole list request.
        """
        stub = git(status=_proc(""))
        gone = tmp_path / "vanished"  # deliberately never created

        assert _worktree_dirty(str(gone)) is None
        assert stub.calls == []

    def test_status_runs_against_the_worktree_not_the_repo_root(self, git, tmp_path):
        wt_dir = tmp_path / "some-worktree"
        wt_dir.mkdir()
        stub = git(status=_proc(""))
        _worktree_dirty(str(wt_dir))

        assert stub.calls[-1][1] == str(wt_dir)

    def test_a_filter_driver_short_circuits_before_status_runs(self, monkeypatch, tmp_path):
        from kiro_crew.dashboard.handlers import worktree as wt

        stub = _GitStub(status=_proc(" M f\n"))
        monkeypatch.setattr(wt, "_run_git", stub)
        monkeypatch.setattr(wt, "_checkout_filter", lambda root: "evil")

        # `git status` would run a `filter.<name>.clean` driver for any tracked file
        # carrying that attribute, so the answer is "unknown" and `status` is never
        # invoked at all.
        assert _worktree_dirty(str(tmp_path)) is None
        assert stub.argv_for("status") is None


class TestActiveWorktreeSlots:
    def test_maps_normalized_realpath_to_slot_key(self, tmp_path):
        real = tmp_path / "tree"
        real.mkdir()
        state = MagicMock()
        state._slots = {"chat-7": MagicMock(project=str(real))}

        active = _active_worktree_slots(state)

        assert list(active.values()) == ["chat-7"]

    def test_symlinked_project_resolves_to_the_same_entry(self, tmp_path):
        real = tmp_path / "tree"
        real.mkdir()
        link = tmp_path / "link"
        try:
            link.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError):  # pragma: no cover - needs privilege
            pytest.skip("symlinks unavailable on this host")
        state = MagicMock()
        state._slots = {"chat-1": MagicMock(project=str(link))}

        active = _active_worktree_slots(state)

        assert os.path.realpath(str(real)).lower() in {k.lower() for k in active}

    def test_slots_without_a_project_are_skipped(self):
        state = MagicMock()
        state._slots = {"a": MagicMock(project=""), "b": MagicMock(project="   ")}

        assert _active_worktree_slots(state) == {}

    def test_a_state_with_no_slots_mapping_is_empty_not_an_error(self):
        state = MagicMock()
        state._slots = None

        assert _active_worktree_slots(state) == {}


class TestListWorktreesSync:
    def test_first_entry_is_main_and_active_session_is_attached(self, git, tmp_path):
        main = tmp_path / "repo"
        linked = tmp_path / "repo-wt-x"
        main.mkdir()
        linked.mkdir()
        git(
            worktree_list=_proc(
                _porcelain(
                    {"path": str(main), "head": "a" * 40, "branch": "refs/heads/main"},
                    {"path": str(linked), "head": "b" * 40, "branch": "refs/heads/feat/x"},
                )
            ),
            status=_proc(""),
        )
        state = MagicMock()
        state._slots = {"chat-9": MagicMock(project=str(linked))}

        rows = _list_worktrees_sync(str(main), state)

        assert [r["is_main"] for r in rows] == [True, False]
        assert rows[0]["active_session"] is None
        assert rows[1]["active_session"] == "chat-9"
        # The head sha is shortened for display.
        assert rows[0]["head"] == "a" * 12

    def test_a_bare_worktree_skips_the_dirty_probe(self, git, tmp_path):
        bare = tmp_path / "bare"
        bare.mkdir()
        stub = git(
            worktree_list=_proc(_porcelain({"path": str(bare), "bare": True})),
            status=_proc(" M f\n"),
        )
        state = MagicMock()
        state._slots = {}

        rows = _list_worktrees_sync(str(bare), state)

        # No working tree means nothing to be dirty, so the probe must not run.
        assert rows[0]["dirty"] is None
        assert stub.argv_for("status") is None

    def test_a_git_listing_failure_propagates_as_none(self, git):
        git(worktree_list=_proc("", returncode=128))
        state = MagicMock()
        state._slots = {}

        assert _list_worktrees_sync("/repo", state) is None


class TestRemoveWorktreeSyncRefusals:
    """Every refusal carries a machine-readable ``code`` and the right status."""

    def _state(self, **slots):
        state = MagicMock()
        state._slots = {k: MagicMock(project=v) for k, v in slots.items()}
        return state

    def test_a_listing_failure_refuses_503_without_touching_git_remove(self, git):
        stub = git(worktree_list=_proc("", returncode=128))

        payload, status = _remove_worktree_sync("/repo", "/repo-wt-x", False, self._state())

        assert status == 503
        assert payload["code"] == "worktree_list_unavailable"
        assert stub.argv_for("worktree remove") is None

    def test_a_path_git_does_not_list_is_404(self, git, tmp_path):
        main = tmp_path / "repo"
        main.mkdir()
        git(worktree_list=_proc(_porcelain({"path": str(main), "branch": "refs/heads/main"})))

        payload, status = _remove_worktree_sync(
            str(main), str(tmp_path / "elsewhere"), False, self._state()
        )

        assert (status, payload["code"]) == (404, "worktree_not_found")

    def test_the_main_worktree_is_refused(self, git, tmp_path):
        main = tmp_path / "repo"
        main.mkdir()
        git(worktree_list=_proc(_porcelain({"path": str(main), "branch": "refs/heads/main"})))

        payload, status = _remove_worktree_sync(str(main), str(main), False, self._state())

        assert (status, payload["code"]) == (409, "worktree_main_protected")

    def test_a_worktree_live_in_another_session_is_refused_and_names_it(self, git, tmp_path):
        main, linked = tmp_path / "repo", tmp_path / "repo-wt-x"
        main.mkdir()
        linked.mkdir()
        git(
            worktree_list=_proc(
                _porcelain(
                    {"path": str(main), "branch": "refs/heads/main"},
                    {"path": str(linked), "branch": "refs/heads/feat/x"},
                )
            )
        )

        payload, status = _remove_worktree_sync(
            str(main), str(linked), False, self._state(**{"chat-4": str(linked)})
        )

        assert (status, payload["code"]) == (409, "worktree_in_use")
        assert payload["active_session"] == "chat-4"

    def test_an_unprovable_clean_tree_is_refused_rather_than_removed(self, git, tmp_path):
        main, linked = tmp_path / "repo", tmp_path / "repo-wt-x"
        main.mkdir()
        linked.mkdir()
        stub = git(
            worktree_list=_proc(
                _porcelain(
                    {"path": str(main), "branch": "refs/heads/main"},
                    {"path": str(linked), "branch": "refs/heads/feat/x"},
                )
            ),
            status=_proc("", returncode=128),
        )

        payload, status = _remove_worktree_sync(str(main), str(linked), False, self._state())

        assert (status, payload["code"]) == (409, "worktree_dirty_unknown")
        assert stub.argv_for("worktree remove") is None

    def test_a_dirty_tree_is_refused_and_flagged_for_the_confirm_row(self, git, tmp_path):
        main, linked = tmp_path / "repo", tmp_path / "repo-wt-x"
        main.mkdir()
        linked.mkdir()
        git(
            worktree_list=_proc(
                _porcelain(
                    {"path": str(main), "branch": "refs/heads/main"},
                    {"path": str(linked), "branch": "refs/heads/feat/x"},
                )
            ),
            status=_proc(" M f\n"),
        )

        payload, status = _remove_worktree_sync(str(main), str(linked), False, self._state())

        assert (status, payload["code"]) == (409, "worktree_dirty")
        assert payload["dirty"] is True


class TestRemoveWorktreeSyncSuccess:
    def _listing(self, main, linked):
        return _proc(
            _porcelain(
                {"path": str(main), "branch": "refs/heads/main"},
                {"path": str(linked), "branch": "refs/heads/feat/x"},
            )
        )

    def test_clean_removal_reports_the_branch_without_a_repo_wide_prune(self, git, tmp_path):
        main, linked = tmp_path / "repo", tmp_path / "repo-wt-x"
        main.mkdir()
        linked.mkdir()
        stub = git(worktree_list=self._listing(main, linked), status=_proc(""))

        payload, status = _remove_worktree_sync(str(main), str(linked), False, MagicMock(_slots={}))

        assert (status, payload["ok"], payload["branch"]) == (200, True, "feat/x")
        # git is handed ITS OWN registered path, never the request string.
        assert stub.argv_for("worktree remove")[-1] == str(linked)
        # And NO repo-wide prune: `worktree remove` already deregistered this
        # tree, while prune would also drop the metadata of an unrelated worktree
        # whose volume merely happens to be unreachable right now.
        assert stub.argv_for("worktree prune") is None

    def test_force_skips_the_dirty_probe_and_passes_force_to_git(self, git, tmp_path):
        main, linked = tmp_path / "repo", tmp_path / "repo-wt-x"
        main.mkdir()
        linked.mkdir()
        stub = git(worktree_list=self._listing(main, linked), status=_proc(" M f\n"))

        payload, status = _remove_worktree_sync(str(main), str(linked), True, MagicMock(_slots={}))

        assert status == 200
        assert "--force" in stub.argv_for("worktree remove")
        assert stub.argv_for("status") is None

    def test_a_git_remove_failure_surfaces_as_400(self, git, tmp_path):
        main, linked = tmp_path / "repo", tmp_path / "repo-wt-x"
        main.mkdir()
        linked.mkdir()

        def answer(stub):
            return _proc("", returncode=1, stderr="fatal: cannot remove")

        stub = git(
            worktree_list=self._listing(main, linked), status=_proc(""), worktree_remove=answer
        )

        payload, status = _remove_worktree_sync(str(main), str(linked), False, MagicMock(_slots={}))

        assert (status, payload["code"]) == (400, "worktree_remove_failed")
        assert stub.argv_for("worktree prune") is None


class TestSyncResultResponse:
    def test_status_and_body_ride_through_unchanged(self):
        resp = _sync_result_response({"error": "no", "code": "x"}, 409)
        assert resp.status == 409

    def test_success_is_rendered_as_200(self):
        assert _sync_result_response({"ok": True}, 200).status == 200


def _app(*projects: str, app_claim: str | None = "", user: str = "owner") -> web.Application:
    @web.middleware
    async def claims(request: web.Request, handler):
        if app_claim is not None:
            request["app"] = app_claim
        request["user"] = user
        return await handler(request)

    app = web.Application(middlewares=[claims])
    state = MagicMock()
    state.owner_id = "owner"
    state._slots = {f"chat-{i}": MagicMock(project=str(p)) for i, p in enumerate(projects) if p}
    app["state"] = state
    app.router.add_get("/api/worktree/list", api_worktree_list)
    app.router.add_post("/api/worktree/remove", api_worktree_remove)
    return app


class TestHandlerInputValidation:
    """Argument screening happens BEFORE any path or git work, so these need no repo."""

    @pytest.mark.asyncio
    async def test_list_without_repo_is_400_with_a_code(self):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.get("/api/worktree/list")
            body = await resp.json()

        assert resp.status == 400
        assert body["code"] == "worktree_repo_required"

    @pytest.mark.asyncio
    async def test_remove_with_a_non_json_body_is_400(self):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post(
                "/api/worktree/remove",
                data="not json",
                headers={"Content-Type": "application/json"},
            )
            body = await resp.json()

        assert resp.status == 400
        assert body["code"] == "invalid_json"

    @pytest.mark.asyncio
    async def test_remove_with_a_json_array_body_is_400(self):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/worktree/remove", json=[1, 2, 3])
            body = await resp.json()

        assert resp.status == 400
        assert body["code"] == "invalid_json"

    @pytest.mark.asyncio
    async def test_remove_with_non_string_arguments_is_400(self):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/worktree/remove", json={"repo": 1, "path": 2})
            body = await resp.json()

        assert resp.status == 400
        assert body["code"] == "worktree_invalid_arguments"

    @pytest.mark.asyncio
    async def test_remove_with_blank_arguments_is_400(self):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/worktree/remove", json={"repo": "  ", "path": ""})
            body = await resp.json()

        assert resp.status == 400
        assert body["code"] == "worktree_invalid_arguments"

    @pytest.mark.asyncio
    async def test_an_app_caller_is_denied_on_both_endpoints(self):
        # The allow-list is built from EVERY slot's project, so an app caller
        # reaching here could read or delete inside another session's repository.
        async with TestClient(TestServer(_app(app_claim="some-app"))) as client:
            assert (await client.get("/api/worktree/list?repo=/tmp")).status == 403
            assert (
                await client.post("/api/worktree/remove", json={"repo": "/tmp", "path": "/tmp/x"})
            ).status == 403

    @pytest.mark.asyncio
    async def test_a_repo_outside_every_slot_project_is_refused(self, tmp_path):
        outsider = tmp_path / "not-a-slot-project"
        outsider.mkdir()
        async with TestClient(TestServer(_app())) as client:
            resp = await client.get(f"/api/worktree/list?repo={outsider}")

        assert resp.status == 403


class TestForceMustBeARealBoolean:
    """`bool("false")` is True, and that would hand `--force` to git.

    The picker only ever sends a JSON boolean, but the endpoint is reachable by
    anything holding a dashboard session, and the failure mode here is discarding
    a user's uncommitted work -- so the type is enforced rather than coerced.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["false", "true", 0, 1, "yes", [], {}])
    async def test_a_non_boolean_force_is_refused_before_any_git_work(self, bad):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post(
                "/api/worktree/remove",
                json={"repo": "/tmp/x", "path": "/tmp/x/wt", "force": bad},
            )
            body = await resp.json()

        assert resp.status == 400
        assert body["code"] == "worktree_invalid_arguments"

    @pytest.mark.asyncio
    async def test_an_absent_force_is_accepted_as_not_forced(self, tmp_path):
        # Reaching the allow-list refusal (403) proves argument screening passed;
        # a 400 here would mean the default was rejected as a bad type.
        outsider = tmp_path / "elsewhere"
        outsider.mkdir()
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post(
                "/api/worktree/remove", json={"repo": str(outsider), "path": str(outsider / "wt")}
            )

        assert resp.status == 403


class TestActiveSessionIsRecheckedBeforeRemoval:
    def test_a_session_that_adopts_the_worktree_during_the_dirty_probe_is_honoured(
        self, monkeypatch, tmp_path
    ):
        """The dirty probe can take seconds; the map is re-read after it.

        Without the late re-check, a session that retargeted onto this worktree
        while `git status` was running would be left pointing at a deleted
        directory. Simulated by having the probe itself adopt the worktree.
        """
        from kiro_crew.dashboard.handlers import worktree as wt

        main, linked = tmp_path / "repo", tmp_path / "repo-wt-x"
        main.mkdir()
        linked.mkdir()
        state = MagicMock()
        state._slots = {}

        stub = _GitStub(
            worktree_list=_proc(
                _porcelain(
                    {"path": str(main), "branch": "refs/heads/main"},
                    {"path": str(linked), "branch": "refs/heads/feat/x"},
                )
            )
        )
        monkeypatch.setattr(wt, "_run_git", stub)
        monkeypatch.setattr(wt, "_checkout_filter", lambda root: "")

        def adopt_then_report_clean(worktree_path):
            state._slots = {"chat-late": MagicMock(project=str(linked))}
            return False

        monkeypatch.setattr(wt, "_worktree_dirty", adopt_then_report_clean)

        payload, status = _remove_worktree_sync(str(main), str(linked), False, state)

        assert (status, payload["code"]) == (409, "worktree_in_use")
        assert payload["active_session"] == "chat-late"
        assert stub.argv_for("worktree remove") is None
        assert linked.exists()


class TestSensitiveWorktreePathsAreNotReachable:
    """git having a sensitive path registered is not authority to touch it.

    `_resolve_repo_root` screens the REPO, but per-worktree paths come from git's
    own registration and were never screened -- so a worktree registered under a
    sensitive directory would be status-probed on the list side (a read) and
    deletable on the remove side (a delete).
    """

    def test_a_sensitive_worktree_is_omitted_from_the_list(self, git, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers import worktree as wt

        main, secret = tmp_path / "repo", tmp_path / "dot-ssh"
        main.mkdir()
        secret.mkdir()
        stub = git(
            worktree_list=_proc(
                _porcelain(
                    {"path": str(main), "branch": "refs/heads/main"},
                    {"path": str(secret), "branch": "refs/heads/keys"},
                )
            ),
            status=_proc(""),
        )
        monkeypatch.setattr(wt, "is_sensitive_path", lambda p: os.path.realpath(str(secret)) == p)

        rows = _list_worktrees_sync(str(main), MagicMock(_slots={}))

        assert [r["path"] for r in rows] == [str(main)]
        # And it was never probed, which would itself have been a read of that tree.
        assert all(call[1] != str(secret) for call in stub.calls)

    def test_the_omission_is_audited_not_silent(self, git, tmp_path, monkeypatch):
        """Omitting a sensitive worktree is a denial, so it must reach the SEL.

        The endpoint logs its own `allowed` outcome for the list call; without a
        `denied` event here the refusal to hand back a path would leave no trace
        at all, and the audit trail would read as a clean listing.
        """
        from kiro_crew.dashboard.handlers import worktree as wt

        main, secret = tmp_path / "repo", tmp_path / "dot-ssh"
        main.mkdir()
        secret.mkdir()
        git(
            worktree_list=_proc(
                _porcelain(
                    {"path": str(main), "branch": "refs/heads/main"},
                    {"path": str(secret), "branch": "refs/heads/keys"},
                )
            ),
            status=_proc(""),
        )
        monkeypatch.setattr(wt, "is_sensitive_path", lambda p: os.path.realpath(str(secret)) == p)

        logged: list[dict] = []
        recorder = MagicMock()
        recorder.log_api_access = lambda **kw: logged.append(kw)
        monkeypatch.setattr(wt, "sel", lambda: recorder)

        rows = _list_worktrees_sync(str(main), MagicMock(_slots={}), "owner")

        assert [r["path"] for r in rows] == [str(main)]
        denials = [e for e in logged if e.get("outcome") == "denied"]
        assert len(denials) == 1, logged
        assert denials[0]["caller"] == "owner"
        assert denials[0]["operation"] == "worktree_list"
        assert denials[0]["error"] == "worktree_path_sensitive"
        # The offending path is named, so the trail says WHICH tree was withheld.
        assert os.path.realpath(str(secret)) in denials[0]["resources"]

    def test_a_registered_worktree_whose_directory_is_gone_does_not_500(self, git, tmp_path):
        """git keeps the registration after the directory goes; listing must cope.

        Both dirty probes run with the worktree as cwd, which RAISES (rather than
        returning non-zero) when it no longer exists — so one stale entry would
        take down the whole listing instead of rendering as "unknown".
        """
        main, gone = tmp_path / "repo", tmp_path / "repo-wt-vanished"
        main.mkdir()  # `gone` is deliberately never created
        git(
            worktree_list=_proc(
                _porcelain(
                    {"path": str(main), "branch": "refs/heads/main"},
                    {"path": str(gone), "branch": "refs/heads/feat/x"},
                )
            ),
            status=_proc(""),
        )

        rows = _list_worktrees_sync(str(main), MagicMock(_slots={}))

        assert [r["path"] for r in rows] == [str(main), str(gone)]
        # The vanished tree reports an unknown dirty state, not a crash and not a
        # false "clean" — remove still demands an explicit force for it.
        assert rows[1]["dirty"] is None

    def test_a_clean_listing_logs_no_denial(self, git, tmp_path, monkeypatch):
        """The other half: an ordinary listing must not emit denial noise."""
        from kiro_crew.dashboard.handlers import worktree as wt

        main = tmp_path / "repo"
        main.mkdir()
        git(
            worktree_list=_proc(_porcelain({"path": str(main), "branch": "refs/heads/main"})),
            status=_proc(""),
        )
        monkeypatch.setattr(wt, "is_sensitive_path", lambda p: False)

        logged: list[dict] = []
        recorder = MagicMock()
        recorder.log_api_access = lambda **kw: logged.append(kw)
        monkeypatch.setattr(wt, "sel", lambda: recorder)

        _list_worktrees_sync(str(main), MagicMock(_slots={}), "owner")

        assert [e for e in logged if e.get("outcome") == "denied"] == []

    def test_removing_a_sensitive_worktree_is_refused_403(self, git, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers import worktree as wt

        main, secret = tmp_path / "repo", tmp_path / "dot-ssh"
        main.mkdir()
        secret.mkdir()
        stub = git(
            worktree_list=_proc(
                _porcelain(
                    {"path": str(main), "branch": "refs/heads/main"},
                    {"path": str(secret), "branch": "refs/heads/keys"},
                )
            ),
            status=_proc(""),
        )
        monkeypatch.setattr(wt, "is_sensitive_path", lambda p: os.path.realpath(str(secret)) == p)

        payload, status = _remove_worktree_sync(str(main), str(secret), True, MagicMock(_slots={}))

        assert (status, payload["code"]) == (403, "worktree_path_sensitive")
        # Force must not get it past the gate either.
        assert stub.argv_for("worktree remove") is None
        assert secret.exists()


class TestNestedSessionProjectCountsAsInUse:
    """A session scoped BENEATH a worktree is just as broken by removing it."""

    def test_a_slot_scoped_to_a_subdirectory_blocks_removal(self, git, tmp_path):
        main, linked = tmp_path / "repo", tmp_path / "repo-wt-x"
        main.mkdir()
        linked.mkdir()
        (linked / "src").mkdir()
        git(
            worktree_list=_proc(
                _porcelain(
                    {"path": str(main), "branch": "refs/heads/main"},
                    {"path": str(linked), "branch": "refs/heads/feat/x"},
                )
            ),
            status=_proc(""),
        )
        state = MagicMock()
        # The session's cwd is inside the worktree, not the worktree root.
        state._slots = {"chat-nested": MagicMock(project=str(linked / "src"))}

        payload, status = _remove_worktree_sync(str(main), str(linked), False, state)

        assert (status, payload["code"]) == (409, "worktree_in_use")
        assert payload["active_session"] == "chat-nested"

    def test_a_sibling_with_a_shared_prefix_does_not_count(self, tmp_path):
        # `/repo-wt-other` must not read as inside `/repo-wt`.
        target = _norm_path(os.path.realpath(str(tmp_path / "repo-wt")))
        sibling = _norm_path(os.path.realpath(str(tmp_path / "repo-wt-other")))

        assert _active_slot_beneath({sibling: "chat-1"}, target) is None
        assert _active_slot_beneath({target: "chat-1"}, target) == "chat-1"
