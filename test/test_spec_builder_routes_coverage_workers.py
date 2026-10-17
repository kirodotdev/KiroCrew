"""Spec Builder's route module below HTTP: git, worktrees, worker slots, the relay, discovery.

``_git`` runs with ``_prepare_git_spawn`` and ``create_subprocess_limited``
replaced, so neither a real ``git`` nor the OS sandbox is needed. Then worktree
creation, removal and rollback; the worker slot a spec runs in (ensure, tear
down, halt); the relay that serializes a slot's transcript and dispatches a
turn; and the discovery of folder specs and the preparation of a spec directory.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
import spec_builder_routes_coverage_helpers
from spec_builder_routes_coverage_helpers import (
    CRED_NAME,
    _entry,
    _FakeProc,
    _no_rehydrate,
    _Slot,
    _spec_tree,
    _State,
    _write_index,
)

from kiro_crew.apps.builtins.spec_builder.tests.routes_facade import routes as r

# pytest resolves a fixture in the requesting test's module, so each shared
# fixture this module uses is bound here by name.
_no_autonudge = spec_builder_routes_coverage_helpers._no_autonudge
_pin_state_dir = spec_builder_routes_coverage_helpers._pin_state_dir
_quiet_sel = spec_builder_routes_coverage_helpers._quiet_sel


# -- git ----------------------------------------------------------------------


def _git_env(proc=None, *, cleanup: str = ""):
    """Replace the sandbox spawn seam so no subprocess and no real git is needed.

    ``sandboxed_spawn_argv`` probes the OS sandbox host and writes a scrubbed-env
    temp file; a GitHub runner has neither that backend nor a guarantee of git, so
    both this and ``create_subprocess_limited`` are injected.
    """
    spawn = mock.Mock(return_value=(["git", "--version"], {"PATH": "/usr/bin"}, cleanup))
    create = mock.AsyncMock(return_value=proc if proc is not None else _FakeProc())
    return (
        spawn,
        create,
        mock.patch.multiple(r, _prepare_git_spawn=spawn, create_subprocess_limited=create),
    )


class TestGit:
    @pytest.mark.asyncio
    async def test_a_successful_command_returns_trimmed_streams(self, tmp_path):
        proc = _FakeProc(0, b"  /repo/root\n", b"")
        _spawn, _create, patch = _git_env(proc)
        with patch:
            rc, out, err = await r._git(str(tmp_path), "rev-parse", "--show-toplevel")
        assert (rc, out, err) == (0, "/repo/root", "")

    @pytest.mark.asyncio
    async def test_a_nonzero_return_code_is_reported_not_raised(self, tmp_path, _quiet_sel):
        proc = _FakeProc(128, b"", b"fatal: not a git repository")
        _spawn, _create, patch = _git_env(proc)
        with patch:
            rc, _out, err = await r._git(str(tmp_path), "rev-parse")
        assert rc == 128 and "not a git repository" in err
        outcomes = [c.kwargs["outcome"] for c in _quiet_sel.log_tool_invocation.call_args_list]
        assert outcomes == ["invoked", "failure"]

    @pytest.mark.asyncio
    async def test_it_refuses_to_spawn_when_the_invocation_cannot_be_audited(self, tmp_path):
        # Audit-or-deny: a process this app runs on the user's repository must be
        # reconstructable from the audit log, so no record means no spawn.
        _spawn, create, patch = _git_env()
        with patch, mock.patch.object(r, "_audit_tool", return_value=False):
            rc, out, err = await r._git(str(tmp_path), "worktree", "add")
        assert rc == r._GIT_UNAVAILABLE
        assert out == "" and "audit unavailable" in err
        create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_sandbox_that_cannot_build_an_argv_degrades_instead_of_500ing(self, tmp_path):
        with mock.patch.object(r, "_prepare_git_spawn", side_effect=RuntimeError("no backend")):
            rc, _out, err = await r._git(str(tmp_path), "rev-parse")
        assert rc == r._GIT_UNAVAILABLE and "RuntimeError" in err

    @pytest.mark.asyncio
    async def test_no_git_on_the_host_degrades_instead_of_500ing(self, tmp_path):
        # Browsing a folder calls _repo_info, so letting this propagate turned the
        # project picker's first request into a 500 on a machine without git.
        _spawn, create, patch = _git_env()
        create.side_effect = FileNotFoundError("git")
        with patch:
            rc, _out, err = await r._git(str(tmp_path), "rev-parse")
        assert rc == r._GIT_UNAVAILABLE and err == "git is not installed"

    @pytest.mark.asyncio
    async def test_a_cancelled_spawn_kills_the_child_and_re_raises(self, tmp_path):
        proc = _FakeProc()
        _spawn, create, patch = _git_env(proc)
        create.side_effect = KeyboardInterrupt("cancelled")
        with patch, mock.patch.object(r, "_halt_git", mock.AsyncMock()) as halt:
            with pytest.raises(KeyboardInterrupt):
                await r._git(str(tmp_path), "worktree", "add")
        halt.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_the_scrubbed_env_temp_file_is_always_removed(self, tmp_path):
        leftover = tmp_path / "scrubbed-env"
        leftover.write_text("x")
        _spawn, _create, patch = _git_env(cleanup=str(leftover))
        with patch:
            await r._git(str(tmp_path), "rev-parse")
        assert not leftover.exists()

    @pytest.mark.asyncio
    async def test_a_command_with_no_arguments_still_audits_a_blank_subcommand(
        self, tmp_path, _quiet_sel
    ):
        _spawn, _create, patch = _git_env()
        with patch:
            await r._git(str(tmp_path))
        assert _quiet_sel.log_tool_invocation.call_args.kwargs["metadata"]["subcommand"] == ""


class TestHaltGit:
    @pytest.mark.asyncio
    async def test_nothing_to_do_for_a_missing_or_finished_process(self):
        await r._halt_git(None, "worktree")
        done = _FakeProc()
        done.returncode = 0
        await r._halt_git(done, "worktree")
        assert done.killed is False

    @pytest.mark.asyncio
    async def test_a_live_process_is_killed_and_reaped(self):
        proc = _FakeProc()
        await r._halt_git(proc, "worktree")
        assert proc.killed is True
        assert proc.returncode == -9

    @pytest.mark.asyncio
    async def test_a_process_that_is_already_gone_needs_no_reap(self):
        proc = _FakeProc()
        proc.kill = mock.Mock(side_effect=ProcessLookupError())  # type: ignore[method-assign]
        proc.wait = mock.AsyncMock(side_effect=AssertionError("must not reap"))  # type: ignore
        await r._halt_git(proc, "worktree")

    @pytest.mark.asyncio
    async def test_a_process_stuck_after_the_kill_is_logged_rather_than_hung_on(self):
        proc = _FakeProc()

        async def _never():
            # asyncio.TimeoutError, NOT the builtin: `_halt_git` catches
            # `asyncio.TimeoutError`, and on Python 3.10 that is
            # concurrent.futures.TimeoutError -- a DIFFERENT class from
            # builtins.TimeoutError. The two only became the same class in 3.11,
            # so raising the builtin here passes on 3.11/3.12 and escapes the
            # handler uncaught on 3.10, which is exactly what CI caught.
            raise asyncio.TimeoutError

        proc.wait = _never  # type: ignore[method-assign]
        with mock.patch.object(r, "_GIT_HALT_SECS", 0.01):
            await r._halt_git(proc, "worktree")  # must return, not raise
        assert proc.killed is True


class TestRepoInfo:
    @pytest.mark.asyncio
    async def test_a_path_outside_a_repository(self):
        with mock.patch.object(r, "_git", mock.AsyncMock(return_value=(128, "", "fatal"))):
            assert await r._repo_info("/tmp/x") == {"is_git": False}

    @pytest.mark.asyncio
    async def test_a_repository_with_an_origin_main_base(self):
        calls = {
            ("rev-parse", "--show-toplevel"): (0, "/repo", ""),
            ("rev-parse", "--abbrev-ref", "HEAD"): (0, "feature/x", ""),
            ("rev-parse", "--verify", "--quiet", "origin/main"): (0, "sha", ""),
        }

        async def _fake(cwd, *args):
            return calls.get(tuple(args), (1, "", ""))

        with mock.patch.object(r, "_git", _fake):
            assert await r._repo_info("/repo") == {
                "is_git": True,
                "root": "/repo",
                "branch": "feature/x",
                "default_base": "origin/main",
            }

    @pytest.mark.asyncio
    async def test_with_no_known_remote_base_it_falls_back_to_the_current_branch(self):
        async def _fake(cwd, *args):
            if args[:2] == ("rev-parse", "--show-toplevel"):
                return 0, "/repo", ""
            if args == ("rev-parse", "--abbrev-ref", "HEAD"):
                return 0, "trunk", ""
            return 1, "", ""

        with mock.patch.object(r, "_git", _fake):
            info = await r._repo_info("/repo")
        assert info["default_base"] == "trunk"

    @pytest.mark.asyncio
    async def test_an_empty_toplevel_is_not_a_repository(self):
        with mock.patch.object(r, "_git", mock.AsyncMock(return_value=(0, "", ""))):
            assert await r._repo_info("/tmp/x") == {"is_git": False}


class TestWorktreeCreation:
    @pytest.mark.asyncio
    async def test_it_refuses_to_reuse_an_existing_path(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        (tmp_path / "repo-wt-demo").mkdir()
        result = await r._create_worktree(str(root), "demo")
        assert isinstance(result, str) and "already exists" in result

    @pytest.mark.asyncio
    async def test_a_successful_creation_returns_the_sibling_path_and_branch(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        seen: list[tuple] = []

        async def _fake(cwd, *args):
            seen.append(args)
            return 0, "/repo", ""

        with (
            mock.patch.object(r, "_git", _fake),
            mock.patch.object(
                r, "_repo_info", mock.AsyncMock(return_value={"default_base": "origin/main"})
            ),
        ):
            result = await r._create_worktree(str(root), "demo")
        assert result == (str(tmp_path / "repo-wt-demo"), "spec/demo")
        assert seen[-1] == (
            "worktree",
            "add",
            str(tmp_path / "repo-wt-demo"),
            "-b",
            "spec/demo",
            "origin/main",
        )

    @pytest.mark.asyncio
    async def test_a_failed_git_call_reports_its_last_line_redacted(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        with (
            mock.patch.object(
                r, "_git", mock.AsyncMock(return_value=(128, "", f"noise\nfatal: {CRED_NAME}"))
            ),
            mock.patch.object(r, "_repo_info", mock.AsyncMock(return_value={})),
        ):
            result = await r._create_worktree(str(root), "demo")
        assert isinstance(result, str)
        assert result.startswith("fatal: ") and CRED_NAME not in result

    @pytest.mark.asyncio
    async def test_a_failure_with_no_stderr_still_reports_the_return_code(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        with (
            mock.patch.object(r, "_git", mock.AsyncMock(return_value=(3, "", ""))),
            mock.patch.object(r, "_repo_info", mock.AsyncMock(return_value={})),
        ):
            result = await r._create_worktree(str(root), "demo")
        assert result == "git worktree add failed (rc=3)"


class TestWorktreeRemoval:
    @pytest.mark.asyncio
    async def test_it_prunes_before_deleting_the_branch(self):
        seen: list[tuple] = []

        async def _fake(cwd, *args):
            seen.append(args)
            return 0, "", ""

        with mock.patch.object(r, "_git", _fake):
            await r._remove_worktree("/repo", "/repo-wt-demo", "spec/demo")
        # A leftover registration keeps the branch checked-out from git's view.
        assert seen == [
            ("worktree", "remove", "--force", "/repo-wt-demo"),
            ("worktree", "prune"),
            ("branch", "-D", "spec/demo"),
        ]

    @pytest.mark.asyncio
    async def test_without_a_branch_only_the_worktree_is_removed(self):
        seen: list[tuple] = []

        async def _fake(cwd, *args):
            seen.append(args)
            return 0, "", ""

        with mock.patch.object(r, "_git", _fake):
            await r._remove_worktree("/repo", "/repo-wt-demo")
        assert ("branch", "-D", "") not in seen
        assert len(seen) == 2

    @pytest.mark.asyncio
    async def test_missing_arguments_make_it_a_no_op(self):
        git = mock.AsyncMock()
        with mock.patch.object(r, "_git", git):
            await r._remove_worktree("", "/wt")
            await r._remove_worktree("/repo", "")
        git.assert_not_awaited()


class TestRollbackWorktreeIfOurs:
    @pytest.mark.asyncio
    async def test_nothing_was_created_so_nothing_is_removed(self):
        remove = mock.AsyncMock()
        with mock.patch.object(r, "_remove_worktree", remove):
            assert (
                await r._rollback_worktree_if_ours(
                    "demo",
                    was_ours=True,
                    repo_root="/repo",
                    created_worktree="",
                    worktree_branch="spec/demo",
                )
                is False
            )
        remove.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_entry_that_is_no_longer_ours_is_left_alone(self):
        # remove --force plus branch -D would discard the REPLACEMENT spec's
        # uncommitted work; an orphaned worktree is recoverable by hand.
        remove = mock.AsyncMock()
        with mock.patch.object(r, "_remove_worktree", remove):
            assert (
                await r._rollback_worktree_if_ours(
                    "demo",
                    was_ours=False,
                    repo_root="/repo",
                    created_worktree="/repo-wt-demo",
                    worktree_branch="spec/demo",
                )
                is False
            )
        remove.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_our_own_worktree_is_removed(self):
        remove = mock.AsyncMock()
        with mock.patch.object(r, "_remove_worktree", remove):
            assert (
                await r._rollback_worktree_if_ours(
                    "demo",
                    was_ours=True,
                    repo_root="/repo",
                    created_worktree="/repo-wt-demo",
                    worktree_branch="spec/demo",
                )
                is True
            )
        remove.assert_awaited_once_with("/repo", "/repo-wt-demo", "spec/demo")


# -- worker slots -------------------------------------------------------------


class TestEnsureWorkerSlot:
    @pytest.mark.asyncio
    async def test_no_state_yields_no_slot(self, tmp_path):
        assert await r._ensure_worker_slot(None, "demo", _entry(tmp_path)) is None

    @pytest.mark.asyncio
    async def test_a_name_that_fails_the_admission_predicate_is_refused(self, tmp_path, _quiet_sel):
        # The name becomes a slot key and then a history key, so an unbounded
        # value would flow into core's session-key parsing.
        state = _State()
        assert await r._ensure_worker_slot(state, CRED_NAME, _entry(tmp_path)) is None
        assert state._slots == {}
        ops = [c.kwargs["operation"] for c in _quiet_sel.log_api_access.call_args_list]
        assert "spec_slot_name_denied" in ops

    @pytest.mark.asyncio
    async def test_denied_name_audit_redacts_before_truncating(self, tmp_path, _quiet_sel):
        """A credential straddling the 64-char audit cut must not leak.

        The old spelling ``_redact(name[:64])`` sliced first, so a key cut at
        the boundary lost its tail, stopped matching the credential regex, and
        the raw prefix escaped into the SEL audit row.

        The fabricated AKIA-shaped literal is deliberately inlined rather than
        bound to a ``secret``-named variable: CodeQL's name-based sensitive-
        source heuristic would otherwise taint this real call path and flag
        every downstream ``logger.warning(..., name, ...)`` in production code
        as clear-text secret logging (10 false alerts on unchanged lines).
        """
        # fails the grammar; the 64-char cut lands 8 chars into the 20-char key
        name = "x" * 56 + "AKIAIOSFODNN7EXAMPLE" + " tail"
        state = _State()
        assert await r._ensure_worker_slot(state, name, _entry(tmp_path)) is None
        denied = [
            c.kwargs["resources"]
            for c in _quiet_sel.log_api_access.call_args_list
            if c.kwargs["operation"] == "spec_slot_name_denied"
        ]
        assert denied, "the denial must still be audited"
        assert all("AKIA" not in res for res in denied)

    @pytest.mark.asyncio
    async def test_a_cold_slot_is_created_scoped_and_titled(self, tmp_path):
        state = _State()
        with _no_rehydrate():
            slot = await r._ensure_worker_slot(state, "demo", _entry(tmp_path / "spec"))
        assert slot is not None
        assert slot._app == r.APP_NAME
        # cwd for the worker's CLI process; without it every tool pill is cd-noise.
        assert slot.project == str(Path(os.path.realpath(tmp_path)))
        assert slot.title == "Spec: demo"
        assert state.titles == [("spec-builder-demo", "Spec: demo")]

    @pytest.mark.asyncio
    async def test_the_persisted_transcript_is_pulled_back_before_a_slot_exists(self, tmp_path):
        state = _State()
        rehydrate = mock.AsyncMock(return_value=None)
        with mock.patch.object(r, "rehydrate_slot_from_history_async", rehydrate):
            await r._ensure_worker_slot(state, "demo", _entry(tmp_path / "spec"))
        rehydrate.assert_awaited_once()
        assert rehydrate.await_args.kwargs["adopt_closed"] is True

    @pytest.mark.asyncio
    async def test_a_creating_caller_must_not_adopt_a_closed_transcript(self, tmp_path):
        # A delete leaves the old spec's archived transcript on disk under a
        # name-derived key, so a fresh spec must not be handed that conversation.
        state = _State()
        rehydrate = mock.AsyncMock(return_value=None)
        with mock.patch.object(r, "rehydrate_slot_from_history_async", rehydrate):
            await r._ensure_worker_slot(
                state, "demo", _entry(tmp_path / "spec"), adopt_closed=False
            )
        assert rehydrate.await_args.kwargs["adopt_closed"] is False

    @pytest.mark.asyncio
    async def test_a_restored_transcript_is_audited(self, tmp_path, _quiet_sel):
        state = _State()
        restored = _Slot("spec-builder-demo")
        with mock.patch.object(
            r, "rehydrate_slot_from_history_async", mock.AsyncMock(return_value=restored)
        ):
            await r._ensure_worker_slot(state, "demo", _entry(tmp_path / "spec"))
        ops = [c.kwargs["operation"] for c in _quiet_sel.log_api_access.call_args_list]
        assert "spec_transcript_restored" in ops

    @pytest.mark.asyncio
    async def test_a_failing_restore_leaves_the_app_working(self, tmp_path):
        state = _State()
        with mock.patch.object(
            r,
            "rehydrate_slot_from_history_async",
            mock.AsyncMock(side_effect=RuntimeError("bad transcript")),
        ):
            slot = await r._ensure_worker_slot(state, "demo", _entry(tmp_path / "spec"))
        assert slot is not None

    @pytest.mark.asyncio
    async def test_a_slot_owned_by_another_app_is_refused_not_taken_over(self, tmp_path):
        state = _State(**{"spec-builder-demo": _Slot("spec-builder-demo", app="issue-radar")})
        with _no_rehydrate():
            assert await r._ensure_worker_slot(state, "demo", _entry(tmp_path / "spec")) is None
        # Its ownership and project must be untouched.
        assert state._slots["spec-builder-demo"]._app == "issue-radar"
        assert state._slots["spec-builder-demo"].project is None

    @pytest.mark.asyncio
    async def test_an_unscoped_slot_under_our_key_is_somebody_elses_conversation(self, tmp_path):
        state = _State(**{"spec-builder-demo": _Slot("spec-builder-demo", app=None)})
        with _no_rehydrate():
            assert await r._ensure_worker_slot(state, "demo", _entry(tmp_path / "spec")) is None

    @pytest.mark.asyncio
    async def test_our_own_existing_slot_is_adopted_and_rescoped(self, tmp_path):
        existing = _Slot("spec-builder-demo")
        state = _State(**{"spec-builder-demo": existing})
        with _no_rehydrate():
            slot = await r._ensure_worker_slot(state, "demo", _entry(tmp_path / "spec"))
        assert slot is existing and existing.project is not None

    @pytest.mark.asyncio
    async def test_an_already_titled_slot_keeps_its_title(self, tmp_path):
        existing = _Slot("spec-builder-demo")
        existing.title = "Renamed by the user"
        existing._titled = True
        state = _State(**{"spec-builder-demo": existing})
        with _no_rehydrate():
            await r._ensure_worker_slot(state, "demo", _entry(tmp_path / "spec"))
        assert existing.title == "Renamed by the user"
        assert state.titles == []

    @pytest.mark.asyncio
    async def test_an_indexed_working_dir_that_no_longer_validates_refuses_the_slot(
        self, tmp_path, _quiet_sel
    ):
        state = _State()
        meta = _entry(tmp_path / "spec", working_dir=str(tmp_path / "deleted-project"))
        with _no_rehydrate():
            assert await r._ensure_worker_slot(state, "demo", meta) is None
        ops = [c.kwargs["operation"] for c in _quiet_sel.log_api_access.call_args_list]
        assert "spec_working_dir_denied" in ops

    @pytest.mark.asyncio
    async def test_a_missing_working_dir_key_counts_as_unusable(self, tmp_path):
        # An unscoped slot is worse than a mis-scoped one: chat_runner passes
        # cwd=slot.project, so the worker would inherit the GATEWAY's cwd.
        state = _State()
        meta = {"spec_dir": str(tmp_path / "spec")}
        with _no_rehydrate():
            assert await r._ensure_worker_slot(state, "demo", meta) is None

    @pytest.mark.asyncio
    async def test_a_spec_replaced_while_the_slot_was_acquired_is_refused(self, tmp_path):
        state = _State()
        r._SLOT_KEYS["demo"] = "spec-builder-demo-0123abcd"

        async def _swap_identity(*_a, **_kw):
            r._SLOT_KEYS["demo"] = "spec-builder-demo-ffffffff"
            return None

        with mock.patch.object(r, "rehydrate_slot_from_history_async", _swap_identity):
            assert await r._ensure_worker_slot(state, "demo", _entry(tmp_path / "spec")) is None
        assert state._slots == {}

    @pytest.mark.asyncio
    async def test_a_replacement_landing_after_the_working_dir_check_is_refused(self, tmp_path):
        state = _State()
        r._SLOT_KEYS["demo"] = "spec-builder-demo-0123abcd"
        real_safe_dir = r._safe_dir

        def _safe_then_swap(raw, **kw):
            r._SLOT_KEYS["demo"] = "spec-builder-demo-ffffffff"
            return real_safe_dir(raw, **kw)

        with _no_rehydrate(), mock.patch.object(r, "_safe_dir", _safe_then_swap):
            assert await r._ensure_worker_slot(state, "demo", _entry(tmp_path / "spec")) is None

    @pytest.mark.asyncio
    async def test_a_slot_that_rejects_scoping_is_still_returned(self, tmp_path):
        # Scoping failures are logged, not fatal -- a partially-built slot must not
        # take the whole request down.
        class _Frozen(_Slot):
            _ready = False

            def __setattr__(self, name, value):
                if name == "project" and self._ready:
                    raise RuntimeError("slot is frozen")
                super().__setattr__(name, value)

        frozen = _Frozen("spec-builder-demo")
        frozen._ready = True
        state = _State(**{"spec-builder-demo": frozen})
        with _no_rehydrate():
            slot = await r._ensure_worker_slot(state, "demo", _entry(tmp_path / "spec"))
        assert slot is frozen
        assert frozen.title == ""  # the scoping block aborted before titling


class TestSlotIdentityMoved:
    def test_an_unchanged_mapping_has_not_moved(self):
        r._SLOT_KEYS["demo"] = "spec-builder-demo-0123abcd"
        assert r._slot_identity_moved("demo", "spec-builder-demo-0123abcd") is False

    def test_a_rewritten_mapping_has_moved_and_is_audited(self, _quiet_sel):
        r._SLOT_KEYS["demo"] = "spec-builder-demo-ffffffff"
        assert r._slot_identity_moved("demo", "spec-builder-demo-0123abcd") is True
        ops = [c.kwargs["operation"] for c in _quiet_sel.log_api_access.call_args_list]
        assert "spec_slot_replaced_midflight" in ops


class TestTeardownWorkerSlot:
    @pytest.mark.asyncio
    async def test_no_state_is_reported_as_nothing_at_risk(self):
        assert await r._teardown_worker_slot(None, "demo") is True

    @pytest.mark.asyncio
    async def test_a_pinned_caller_that_captured_nothing_tears_nothing_down(self):
        state = _State(**{"spec-builder-demo": _Slot("spec-builder-demo")})
        assert await r._teardown_worker_slot(state, "demo", only_slot=None) is True
        assert "spec-builder-demo" in state._slots

    @pytest.mark.asyncio
    async def test_an_absent_slot_is_nothing_at_risk(self):
        assert await r._teardown_worker_slot(_State(), "demo") is True

    @pytest.mark.asyncio
    async def test_a_slot_replaced_since_capture_is_left_alone(self):
        live = _Slot("spec-builder-demo")
        state = _State(**{"spec-builder-demo": live})
        captured = _Slot("spec-builder-demo")
        assert await r._teardown_worker_slot(state, "demo", only_slot=captured) is True
        assert state._slots["spec-builder-demo"] is live

    @pytest.mark.asyncio
    async def test_a_foreign_slot_is_never_deleted_by_name_collision(self):
        state = _State(**{"spec-builder-demo": _Slot("spec-builder-demo", app="issue-radar")})
        assert await r._teardown_worker_slot(state, "demo") is True
        assert "spec-builder-demo" in state._slots

    @pytest.mark.asyncio
    async def test_a_captured_slot_with_a_malformed_key_falls_back_to_the_name(self):
        slot = _Slot("spec-builder-demo")
        slot.key = "not a slot key"  # type: ignore[assignment]
        state = _State(**{"spec-builder-demo": slot})
        with mock.patch(
            "kiro_crew.dashboard.chat_persistence.save_slot_off_loop", mock.AsyncMock()
        ):
            assert await r._teardown_worker_slot(state, "demo", only_slot=slot) is True
        assert state._slots == {}

    @pytest.mark.asyncio
    async def test_the_slot_is_popped_queued_work_dropped_and_the_turn_cancelled(self):
        slot = _Slot("spec-builder-demo", running=True)
        slot._queue = [{"id": "q1", "content": "next prompt"}]
        slot._pending_synthesis = True
        task = mock.Mock()
        task.cancel = mock.Mock()
        slot.task = task  # type: ignore[assignment]
        state = _State(**{"spec-builder-demo": slot})

        async def _cancelled(*_a, **_kw):
            raise asyncio.CancelledError

        with (
            mock.patch(
                "kiro_crew.dashboard.chat_persistence.save_slot_off_loop", mock.AsyncMock()
            ) as save,
            mock.patch.object(asyncio, "wait_for", _cancelled),
        ):
            assert await r._teardown_worker_slot(state, "demo") is True
        assert state._slots == {}
        # _run_chat's end-of-turn block would otherwise start the next prompt.
        assert slot._queue == [] and slot._pending_synthesis is False
        task.cancel.assert_called_once()
        assert save.await_args.kwargs["closed"] is True

    @pytest.mark.asyncio
    async def test_a_failed_archive_is_tolerated_when_it_was_not_required(self):
        slot = _Slot("spec-builder-demo")
        state = _State(**{"spec-builder-demo": slot})
        with mock.patch(
            "kiro_crew.dashboard.chat_persistence.save_slot_off_loop",
            mock.AsyncMock(side_effect=OSError("disk full")),
        ):
            assert await r._teardown_worker_slot(state, "demo") is True
        assert state._slots == {}

    @pytest.mark.asyncio
    async def test_a_required_archive_that_fails_puts_the_slot_back(self, _quiet_sel):
        # The transcript is the user's data: reporting success would discard a
        # conversation that was never written.
        slot = _Slot("spec-builder-demo")
        state = _State(**{"spec-builder-demo": slot})
        with mock.patch(
            "kiro_crew.dashboard.chat_persistence.save_slot_off_loop",
            mock.AsyncMock(side_effect=OSError("disk full")),
        ):
            assert await r._teardown_worker_slot(state, "demo", require_archive=True) is False
        assert state._slots["spec-builder-demo"] is slot
        ops = [c.kwargs["operation"] for c in _quiet_sel.log_api_access.call_args_list]
        assert "spec_slot_archive_failed" in ops


class TestHaltActiveTurn:
    @pytest.mark.asyncio
    async def test_a_pinned_caller_that_captured_nothing_stops_nothing(self):
        assert await r._halt_active_turn(_State(), "demo", only_slot=None) is False

    @pytest.mark.asyncio
    async def test_no_slot_or_no_running_turn_means_nothing_to_stop(self):
        assert await r._halt_active_turn(_State(), "demo") is False
        state = _State(**{"spec-builder-demo": _Slot("spec-builder-demo", running=False)})
        assert await r._halt_active_turn(state, "demo") is False

    @pytest.mark.asyncio
    async def test_a_slot_replaced_since_capture_is_not_stopped(self):
        live = _Slot("spec-builder-demo", running=True)
        state = _State(**{"spec-builder-demo": live})
        assert await r._halt_active_turn(state, "demo", only_slot=_Slot("x")) is False

    @pytest.mark.asyncio
    async def test_an_unscoped_slot_sharing_our_key_is_not_cancelled(self):
        # A plain POST /api/chat conversation that merely shares the key must not
        # lose its turn to this app's Pause button.
        state = _State(**{"spec-builder-demo": _Slot("spec-builder-demo", app=None, running=True)})
        assert await r._halt_active_turn(state, "demo") is False

    @pytest.mark.asyncio
    async def test_the_turn_is_stopped_cooperatively_then_cancelled(self):
        slot = _Slot("spec-builder-demo", running=True)
        slot._queue = [{"id": "q1", "content": "next"}]
        task = mock.Mock()
        task.done.return_value = False
        slot.task = task  # type: ignore[assignment]
        state = _State(**{"spec-builder-demo": slot})

        async def _cancelled(*_a, **_kw):
            raise asyncio.CancelledError

        with mock.patch.object(asyncio, "wait_for", _cancelled):
            assert await r._halt_active_turn(state, "demo") is True
        state.sessions.stop_turn.assert_awaited_once()
        assert state.sessions.stop_turn.await_args.kwargs["force"] is False
        task.cancel.assert_called_once()
        assert slot._queue == []
        # Pause must LEAVE the slot so the user can resume.
        assert state._slots["spec-builder-demo"] is slot

    @pytest.mark.asyncio
    async def test_a_failing_cooperative_stop_still_cancels(self):
        slot = _Slot("spec-builder-demo", running=True)
        state = _State(**{"spec-builder-demo": slot})
        state.sessions.stop_turn.side_effect = RuntimeError("no session")
        assert await r._halt_active_turn(state, "demo") is True


class TestHaltExecution:
    @pytest.mark.asyncio
    async def test_it_sentinels_then_removes_the_loop_then_stops_the_turn(self, tmp_path):
        order: list[str] = []
        with (
            mock.patch.object(
                r,
                "_write_stop_sentinel_for_spec",
                lambda *a, **k: order.append("sentinel") or True,
            ),
            mock.patch.object(
                r,
                "_remove_nudge_loop",
                mock.AsyncMock(side_effect=lambda *a, **k: order.append("loop")),
            ),
            mock.patch.object(
                r,
                "_halt_active_turn",
                mock.AsyncMock(side_effect=lambda *a, **k: order.append("turn")),
            ),
        ):
            await r._halt_execution(_State(), "demo", tmp_path, reason="user stop")
        assert order == ["sentinel", "loop", "turn"]

    @pytest.mark.asyncio
    async def test_a_missing_sentinel_is_not_fatal_because_the_two_stops_are(self, tmp_path):
        with (
            mock.patch.object(r, "_write_stop_sentinel_for_spec", return_value=False),
            mock.patch.object(r, "_remove_nudge_loop", mock.AsyncMock()) as loop,
            mock.patch.object(r, "_halt_active_turn", mock.AsyncMock()) as turn,
        ):
            await r._halt_execution(_State(), "demo", tmp_path, reason="user stop")
        loop.assert_awaited_once()
        turn.assert_awaited_once()


# -- transcript relay ---------------------------------------------------------


class TestSerializeMessages:
    @pytest.mark.asyncio
    async def test_the_live_slot_wins_and_system_turns_are_hidden(self):
        slot = _Slot("spec-builder-demo")
        slot.messages = [
            {"role": "system", "content": "internal", "ts": "1"},
            {"role": "user", "content": "hello", "ts": "2"},
            {"role": "assistant", "content": "hi", "ts": "3"},
        ]
        state = _State(**{"spec-builder-demo": slot})
        assert await r._serialize_messages(state, "spec-builder-demo") == [
            {"role": "user", "content": "hello", "ts": "2"},
            {"role": "assistant", "content": "hi", "ts": "3"},
        ]

    @pytest.mark.asyncio
    async def test_a_tool_turn_is_compacted_to_its_first_line(self):
        slot = _Slot("spec-builder-demo")
        slot.messages = [{"role": "tool", "content": "fs_write tasks.md\nmore\nlines", "ts": "1"}]
        state = _State(**{"spec-builder-demo": slot})
        out = await r._serialize_messages(state, "spec-builder-demo")
        assert out == [{"role": "tool", "content": "fs_write tasks.md", "ts": "1"}]

    @pytest.mark.asyncio
    async def test_a_tool_line_credential_straddling_the_cut_is_not_leaked(self):
        """A credential straddling the 200-char cut must not leak.

        The old spelling ``_redact(first[:200])`` sliced first, so a key cut at
        the boundary lost its tail, stopped matching the credential regex, and
        the raw prefix escaped into the embedded-chat payload.
        The fabricated AKIA-shaped literal is inlined rather than bound to a
        ``secret``-named variable, which would trip CodeQL's name-based
        sensitive-source heuristic on this real call path.
        """
        # cut lands 8 chars into the fabricated 20-char key
        first = "x" * 192 + "AKIAIOSFODNN7EXAMPLE" + " trailing"
        slot = _Slot("spec-builder-demo")
        slot.messages = [{"role": "tool", "content": first + "\nmore", "ts": "1"}]
        state = _State(**{"spec-builder-demo": slot})
        out = await r._serialize_messages(state, "spec-builder-demo")
        assert "AKIA" not in out[0]["content"]
        assert len(out[0]["content"]) <= 200

    @pytest.mark.asyncio
    async def test_a_plain_tool_line_truncation_unchanged(self):
        """Ordinary path is result-preserving: no secret ⇒ the same 200-char slice."""
        slot = _Slot("spec-builder-demo")
        slot.messages = [{"role": "tool", "content": "t" * 250 + "\nmore", "ts": "1"}]
        state = _State(**{"spec-builder-demo": slot})
        out = await r._serialize_messages(state, "spec-builder-demo")
        assert out[0]["content"] == "t" * 200

    @pytest.mark.asyncio
    async def test_an_empty_tool_turn_does_not_raise_on_the_first_line(self):
        slot = _Slot("spec-builder-demo")
        slot.messages = [{"role": "tool", "content": "", "ts": "1"}]
        state = _State(**{"spec-builder-demo": slot})
        assert await r._serialize_messages(state, "spec-builder-demo") == [
            {"role": "tool", "content": "", "ts": "1"}
        ]

    @pytest.mark.asyncio
    async def test_content_is_redacted_before_it_leaves_the_backend(self):
        slot = _Slot("spec-builder-demo")
        slot.messages = [{"role": "assistant", "content": f"use {CRED_NAME}", "ts": "1"}]
        state = _State(**{"spec-builder-demo": slot})
        out = await r._serialize_messages(state, "spec-builder-demo")
        assert CRED_NAME not in out[0]["content"]

    @pytest.mark.asyncio
    async def test_a_cold_slot_falls_back_to_the_persisted_transcript_off_loop(self):
        state = _State()
        state.conversation_log = SimpleNamespace(  # type: ignore[assignment]
            read_messages=mock.Mock(return_value=[{"role": "user", "content": "old", "ts": "9"}])
        )
        assert await r._serialize_messages(state, "spec-builder-demo") == [
            {"role": "user", "content": "old", "ts": "9"}
        ]

    @pytest.mark.asyncio
    async def test_object_shaped_messages_are_read_by_attribute(self):
        state = _State()
        state.conversation_log = SimpleNamespace(  # type: ignore[assignment]
            read_messages=mock.Mock(
                return_value=[SimpleNamespace(role="user", content="old", ts="9")]
            )
        )
        assert await r._serialize_messages(state, "spec-builder-demo") == [
            {"role": "user", "content": "old", "ts": "9"}
        ]

    @pytest.mark.asyncio
    async def test_a_failing_history_read_yields_an_empty_transcript(self):
        state = _State()
        state.conversation_log = SimpleNamespace(  # type: ignore[assignment]
            read_messages=mock.Mock(side_effect=OSError("gone"))
        )
        assert await r._serialize_messages(state, "spec-builder-demo") == []

    @pytest.mark.asyncio
    async def test_no_conversation_log_yields_an_empty_transcript(self):
        assert await r._serialize_messages(_State(), "spec-builder-demo") == []


class TestDispatchTurn:
    @pytest.mark.asyncio
    async def test_an_idle_slot_starts_a_turn(self):
        slot = _Slot("spec-builder-demo")
        state = _State(**{"spec-builder-demo": slot})
        with mock.patch("kiro_crew.dashboard.chat_runner._run_chat", mock.AsyncMock()):
            r._dispatch_turn(state, slot, "do the thing")
            assert slot.messages == [{"role": "user", "content": "do the thing", "ts": ""}]
            assert slot.task is not None
            assert slot.task in state._background_tasks
            assert state.pushes == 1
            # flake-ok: awaits the turn task _dispatch_turn started above on a mocked _run_chat
            await slot.task

    @pytest.mark.asyncio
    async def test_the_turn_uses_the_owner_aware_bounded_wrapper(self):
        slot = _Slot("spec-builder-demo")
        state = _State(**{"spec-builder-demo": slot})
        seen: dict = {}

        async def _capture(coro, *, state, slot):
            seen["state"] = state
            seen["slot"] = slot
            coro.close()

        with (
            mock.patch("kiro_crew.dashboard.chat_runner._run_chat", mock.AsyncMock()),
            mock.patch(
                "kiro_crew.dashboard.turn_dispatch.bounded_chat_turn",
                new=_capture,
            ),
        ):
            r._dispatch_turn(state, slot, "do the thing")
            # flake-ok: awaits the turn task _dispatch_turn started on the line above; the wrapper is a stub
            await slot.task
        assert seen == {"state": state, "slot": slot}

    def test_a_busy_slot_queues_the_turn_and_shows_it_redacted(self):
        slot = _Slot("spec-builder-demo", running=True)
        state = _State(**{"spec-builder-demo": slot})
        r._dispatch_turn(state, slot, f"use {CRED_NAME}")
        assert slot._queue == [f"use {CRED_NAME}"]
        # "queued" is NOT a role the slot suppresses the global SSE push for, so
        # this text reaches every connected dashboard client.
        assert slot.messages[0]["role"] == "queued"
        assert CRED_NAME not in slot.messages[0]["content"]
        assert state.pushes == 1

    def test_a_slot_that_cannot_queue_or_echo_is_tolerated(self):
        slot = _Slot("spec-builder-demo", running=True)
        slot.queue_append = mock.Mock(side_effect=RuntimeError("full"))  # type: ignore
        slot.append = mock.Mock(side_effect=RuntimeError("closed"))  # type: ignore
        state = _State(**{"spec-builder-demo": slot})
        r._dispatch_turn(state, slot, "text")
        assert state.pushes == 1


# -- discovery + spec dir preparation ----------------------------------------


class TestDiscoverFolderSpecs:
    def test_a_spec_shaped_directory_under_a_known_root_is_adopted(self, tmp_path):
        found = _spec_tree(tmp_path, "from-cli", "requirements.md")
        index = {"seed": _entry(tmp_path / "seed", working_dir=str(tmp_path))}
        assert r._discover_folder_specs(index) is True
        assert index["from-cli"]["spec_dir"] == str(found)
        assert index["from-cli"]["discovered"] is True
        assert r._owns_slot_key("from-cli", index["from-cli"]["slot_key"])

    def test_nothing_to_add_reports_false(self, tmp_path):
        (tmp_path / ".kiro" / "specs").mkdir(parents=True)
        index = {"seed": _entry(tmp_path / "seed", working_dir=str(tmp_path))}
        assert r._discover_folder_specs(index) is False

    def test_a_directory_without_kiro_markdown_is_not_a_spec(self, tmp_path):
        _spec_tree(tmp_path, "just-notes", "notes.txt")
        index = {"seed": _entry(tmp_path / "seed", working_dir=str(tmp_path))}
        assert r._discover_folder_specs(index) is False

    def test_a_deleted_directory_is_not_adopted_back(self, tmp_path):
        found = _spec_tree(tmp_path, "deleted-one", "tasks.md")
        r._remember_deleted(str(found))
        index = {"seed": _entry(tmp_path / "seed", working_dir=str(tmp_path))}
        # Deleting is a decision; the tombstone is what remembers it.
        assert r._discover_folder_specs(index) is False
        assert "deleted-one" not in index

    def test_an_already_indexed_directory_is_not_re_added(self, tmp_path):
        found = _spec_tree(tmp_path, "known", "tasks.md")
        index = {"known": _entry(found, working_dir=str(tmp_path))}
        assert r._discover_folder_specs(index) is False

    def test_a_name_that_fails_the_admission_predicate_is_skipped(self, tmp_path):
        _spec_tree(tmp_path, CRED_NAME, "tasks.md")
        index = {"seed": _entry(tmp_path / "seed", working_dir=str(tmp_path))}
        # Admitting on the grammar alone would re-add an entry the next load
        # drops, rediscovering it on every call.
        assert r._discover_folder_specs(index) is False

    def test_a_file_where_a_spec_directory_would_be_is_skipped(self, tmp_path):
        base = tmp_path / ".kiro" / "specs"
        base.mkdir(parents=True)
        (base / "a-file.md").write_text("x")
        index = {"seed": _entry(tmp_path / "seed", working_dir=str(tmp_path))}
        assert r._discover_folder_specs(index) is False

    def test_an_unusable_indexed_root_is_not_enumerated(self, tmp_path):
        index = {"seed": _entry(tmp_path / "seed", working_dir=str(tmp_path / "gone"))}
        assert r._discover_folder_specs(index) is False

    def test_a_sensitive_indexed_root_is_not_enumerated(self, tmp_path):
        _spec_tree(tmp_path, "inside", "tasks.md")
        index = {"seed": _entry(tmp_path / "seed", working_dir=str(tmp_path))}
        with mock.patch.object(r, "is_sensitive_path", return_value=True):
            assert r._discover_folder_specs(index) is False

    def test_a_root_with_no_specs_directory_is_skipped(self, tmp_path):
        index = {"seed": _entry(tmp_path / "seed", working_dir=str(tmp_path))}
        assert r._discover_folder_specs(index) is False

    def test_an_unreadable_specs_directory_is_skipped(self, tmp_path):
        _spec_tree(tmp_path, "one", "tasks.md")
        index = {"seed": _entry(tmp_path / "seed", working_dir=str(tmp_path))}
        with mock.patch.object(Path, "iterdir", side_effect=OSError("denied")):
            assert r._discover_folder_specs(index) is False

    def test_a_failing_stat_falls_back_to_now(self, tmp_path):
        _spec_tree(tmp_path, "one", "tasks.md")
        index = {"seed": _entry(tmp_path / "seed", working_dir=str(tmp_path))}
        real_stat = Path.stat
        seen: list[str] = []

        def _boom(self, *a, **kw):
            # The product stats the candidate twice: once through is_dir(), then
            # again for created_at. Only the second one is the guarded call.
            if self.name == "one":
                seen.append(str(self))
                if len(seen) > 1:
                    raise OSError("stat failed")
            return real_stat(self, *a, **kw)

        with mock.patch.object(Path, "stat", _boom):
            assert r._discover_folder_specs(index) is True
        assert index["one"]["created_at"] > 0
        assert index["one"]["created_at"] == index["one"]["updated_at"]


class TestLoadIndexWithDiscovery:
    def test_it_returns_the_index_and_every_derived_phase_in_one_hop(self, tmp_path):
        found = _spec_tree(tmp_path, "found", "requirements.md", "design.md")
        _write_index({"seed": _entry(tmp_path / "seed", working_dir=str(tmp_path))})
        index, phases = r._load_index_with_discovery()
        assert set(index) == {"seed", "found"}
        assert phases["found"] == "design"
        assert phases["seed"] == "new"
        # Discovery persists, so the next read does not have to rescan.
        assert "found" in json.loads(r._index_path().read_text())
        assert found.is_dir()


class TestPrepareSpecDir:
    def test_it_creates_the_spec_directory(self, tmp_path):
        spec_dir, refusal = r._prepare_spec_dir(str(tmp_path), tmp_path, "demo", False)
        assert refusal == ""
        assert spec_dir == (tmp_path / ".kiro" / "specs" / "demo").resolve()
        assert spec_dir.is_dir()

    def test_a_resolved_path_outside_the_declared_root_is_refused(self, tmp_path):
        outside = tmp_path.parent / "elsewhere"
        with mock.patch.object(r, "_resolve_spec_dir", return_value=outside / "demo"):
            _spec_dir, refusal = r._prepare_spec_dir(str(tmp_path), tmp_path, "demo", False)
        assert refusal == "escape"

    def test_the_settings_base_path_becomes_the_declared_root(self, tmp_path):
        store = tmp_path / "store"
        store.mkdir()
        r._save_settings({"base_path": str(store)})
        spec_dir, refusal = r._prepare_spec_dir(str(tmp_path), tmp_path, "demo", False)
        assert refusal == ""
        assert spec_dir == (store / "demo").resolve()

    def test_a_destination_that_resolves_somewhere_sensitive_is_refused(self, tmp_path):
        real_sensitive = r.is_sensitive_path
        target = str((tmp_path / ".kiro" / "specs" / "demo").resolve())

        def _sensitive(path: str, *a, **kw) -> bool:
            # Containment only says "under the declared root"; if that root grows a
            # link into a credential tree, BOTH paths resolve through it.
            return path == target or real_sensitive(path, *a, **kw)

        with mock.patch.object(r, "is_sensitive_path", _sensitive):
            _spec_dir, refusal = r._prepare_spec_dir(str(tmp_path), tmp_path, "demo", False)
        assert refusal == "escape"

    def test_existing_kiro_documents_are_not_adopted_by_overwrite(self, tmp_path):
        _spec_tree(tmp_path, "demo", "requirements.md", "tasks.md")
        _spec_dir, refusal = r._prepare_spec_dir(str(tmp_path), tmp_path, "demo", False)
        assert refusal == "existing:requirements.md, tasks.md"

    def test_opting_in_adopts_them(self, tmp_path):
        _spec_tree(tmp_path, "demo", "requirements.md")
        _spec_dir, refusal = r._prepare_spec_dir(str(tmp_path), tmp_path, "demo", True)
        assert refusal == ""

    def test_a_failing_mkdir_is_reported_rather_than_raised(self, tmp_path):
        with mock.patch.object(Path, "mkdir", side_effect=OSError("read-only fs")):
            _spec_dir, refusal = r._prepare_spec_dir(str(tmp_path), tmp_path, "demo", False)
        assert refusal.startswith("mkdir:") and "read-only fs" in refusal
