"""The untracked-discard contract of Dev Fleet worktree removal.

This module pins ``_dirty_split``'s tracked and untracked classification, the dirt
fields on refusal payloads and prune verdicts, the consent-by-path-list rules a
discard must meet (the exact listed set, the sample cap, an identity redaction), the
ordering that keeps a refused removal, a locked tree or an unrenewed lease from
deleting anything, which prune refusals a discard-only approval may override in
``_prune_run`` (only ``_DISCARD_OVERRIDABLE_CODES`` reach the removal), the handler
validation of ``discard_untracked_paths``, and what a removal reports after a
discard ran. Every helper the removal touches is stubbed. The per-file unlink walk
runs against a real filesystem in ``test_dev_fleet_server_coverage_discard_files.py``;
the base module is ``test_dev_fleet_server_coverage.py``.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import ExitStack, contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from dev_fleet_server_coverage_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _json_request,
    _pin_module_state,
    _sel_capture,
)

from kiro_crew.apps.builtins.dev_fleet import (
    fleet_state,
    http_api,
    live,
    repository,
    runtime,
    worktree_ops,
)

# ==========================================================================
# Untracked-discard contract: _dirty_split classification, the refusal
# payload builders, and the late-executed `git clean` discard path.
#
# Every helper the removal touches is stubbed with AsyncMock; no real git and
# no real subprocess. `_git` is driven by a per-subcommand fake so a test can
# say "status -uno is clean, ls-files lists two files" without parsing argv by
# position.
# ==========================================================================


def _git_by_subcommand(mapping, default=""):
    """Build an async `_git` double keyed on the git subcommand.

    `mapping` maps a subcommand token (``"status"``, ``"ls-files"``,
    ``"rev-parse"``, ``"clean"``) to the string (or None) `_git` should return.
    The first positional arg is the worktree path; the next is the subcommand.
    """

    async def _fake_git(path, *args, **kw):
        sub = args[0] if args else ""
        if sub in mapping:
            return mapping[sub]
        return default

    return _fake_git


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status_out, lsfiles_out, expect_tracked, expect_untracked",
    [
        # tracked-only: status prints a change, ls-files finds nothing
        (" M src/a.py\n", "", True, []),
        # untracked-only: status is empty, ls-files lists paths (NUL-joined)
        ("", "note.txt\0probe.sh\0", False, ["note.txt", "probe.sh"]),
        # both: a tracked change AND untracked files
        (" M src/a.py\n", "scratch.log\0", True, ["scratch.log"]),
        # status could not answer -> None (unverifiable, never "clean")
        (None, "", None, []),
    ],
)
async def test_dirty_split_classifies_tracked_vs_untracked(
    status_out, lsfiles_out, expect_tracked, expect_untracked
):
    # The tracked half still goes through `_git status`; the untracked half now
    # goes through `_run_cmd ls-files` directly (returning (rc, stdout, stderr))
    # so a leading/trailing space on a filename survives the `_git` strip.
    git = _git_by_subcommand({"status": status_out})

    async def run_cmd(cmd, timeout=None, **kw):
        assert "ls-files" in cmd, cmd
        return (0, lsfiles_out, "")

    with (
        patch.object(repository, "_git", side_effect=git),
        patch.object(runtime, "_run_cmd", side_effect=run_cmd),
    ):
        tracked, untracked = await repository._dirty_split("/wt/x")
    assert tracked is expect_tracked
    assert untracked == expect_untracked


@pytest.mark.asyncio
async def test_dirty_split_lsfiles_failure_is_fail_closed_empty():
    """`ls-files` returning nonzero rc must yield an EMPTY untracked list -- an
    empty list is "nothing approved to discard", never a promise the tree is
    clean. tracked_dirty stays authoritative from the status query."""
    git = _git_by_subcommand({"status": " M a.py\n"})

    async def run_cmd(cmd, timeout=None, **kw):
        assert "ls-files" in cmd, cmd
        return (1, "", "fatal: not a git repo")

    with (
        patch.object(repository, "_git", side_effect=git),
        patch.object(runtime, "_run_cmd", side_effect=run_cmd),
    ):
        tracked, untracked = await repository._dirty_split("/wt/x")
    assert tracked is True
    assert untracked == []


@contextmanager
def _remove_stubs(
    *,
    git,
    run_cmd,
    pr_state,
    own=1,
    contained=True,
    target=None,
):
    """Stub every helper `_worktree_remove_locked` calls on the happy path up to
    the removal, leaving `_git` and `_run_cmd` to the caller so ordering and
    argv can be asserted.

    ``target`` overrides the discovered worktree record, for cases that turn on
    a flag git reports there (a `locked` tree, for instance).
    """
    stack = ExitStack()
    stack.enter_context(
        patch.object(
            repository,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=(
                target or {"path": "/wt/feat", "branch": "feat/x", "is_main": False},
                None,
            ),
        )
    )
    stack.enter_context(
        patch.object(live, "_live_worktree_path", new_callable=AsyncMock, return_value=None)
    )
    stack.enter_context(patch.object(live, "_own_checkout_path", return_value=None))
    stack.enter_context(
        patch.object(repository, "_real_dirty", new_callable=AsyncMock, return_value=True)
    )
    stack.enter_context(
        patch.object(
            fleet_state,
            "_pr_status_cached",
            new_callable=AsyncMock,
            return_value={"state": pr_state},
        )
    )
    stack.enter_context(
        patch.object(repository, "_own_commits_count", new_callable=AsyncMock, return_value=own)
    )
    stack.enter_context(
        patch.object(
            fleet_state, "_fetch_pr_head_oid", new_callable=AsyncMock, return_value="a" * 40
        )
    )
    stack.enter_context(
        patch.object(
            fleet_state, "_head_contained_in_pr", new_callable=AsyncMock, return_value=contained
        )
    )
    stack.enter_context(patch.object(runtime, "_load_cfg", return_value=None))
    stack.enter_context(patch.object(runtime, "_POD_AVAILABLE", False))
    stack.enter_context(patch.object(repository, "_git", side_effect=git))
    stack.enter_context(patch.object(runtime, "_run_cmd", side_effect=run_cmd))
    try:
        yield
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_discard_refused_when_a_tracked_file_is_modified():
    """discard_untracked_paths set but a TRACKED file is modified -> still
    refused; the refusal carries dirty_tracked True. A discard authorizes
    destroying ONLY untracked files, never a tracked edit."""
    # status -uno prints a tracked change, so _dirty_split -> (True, [...]);
    # pending_discard stays None (tracked_dirty is not False), _dirty_now falls
    # back to _real_dirty (True), and the not-force gate refuses.
    git = _git_by_subcommand({"status": " M src/a.py\n"})

    async def run_cmd(cmd, timeout=None, **kw):
        if "ls-files" in cmd:
            return (0, "scratch.log\0", "")
        # a `worktree remove` here would mean the refusal was bypassed
        raise AssertionError("removal must not run for a refused discard")

    with _remove_stubs(git=git, run_cmd=run_cmd, pr_state="OPEN"):
        res = await worktree_ops._worktree_remove_locked(
            "feat", discard_untracked_paths=["scratch.log"]
        )
    assert res["ok"] is False
    assert res["dirty_tracked"] is True
    assert "uncommitted changes" in res["error"]


@pytest.mark.asyncio
async def test_discard_untracked_forced_unmerged_runs_clean_before_removal(monkeypatch):
    """untracked-only + discard_untracked_paths + force=True on an UNMERGED
    branch proceeds. `_discard_untracked_files` runs BEFORE the `worktree
    remove` argv, and `--force` is NOT in the removal argv (the discard path
    always removes without git --force)."""
    order: list[str] = []
    remove_argv: list[list[str]] = []
    discarded: list[tuple] = []

    def discard_spy(worktree, rel_paths):
        order.append("discard")
        discarded.append((worktree, list(rel_paths)))
        return None  # success

    monkeypatch.setattr(repository, "_discard_untracked_files", discard_spy)

    async def git(path, *args, **kw):
        sub = args[0] if args else ""
        if sub == "status":
            # clean of tracked changes both before and after the discard
            return ""
        if sub == "rev-parse":
            return "a" * 40
        return "a" * 40

    async def run_cmd(cmd, timeout=None, **kw):
        if "ls-files" in cmd:
            # untracked before the discard; empty after the discard ran
            return (0, "note.txt\0" if not discarded else "", "")
        if "worktree" in cmd and "remove" in cmd:
            order.append("remove")
            remove_argv.append(list(cmd))
        return (0, "", "")

    with _remove_stubs(git=git, run_cmd=run_cmd, pr_state="OPEN", own=1):
        res = await worktree_ops._worktree_remove_locked(
            "feat", force=True, discard_untracked_paths=["note.txt"]
        )
    assert res.get("ok") is True, res
    assert order == ["discard", "remove"], order
    assert remove_argv, "worktree remove never ran"
    assert "--force" not in remove_argv[0]


@pytest.mark.asyncio
async def test_discard_ordering_refused_request_destroys_nothing(monkeypatch):
    """ORDERING INVARIANT: discard_untracked_paths set, untracked-only, PR NOT
    merged, force=False. The PR-not-merged gate refuses AND the discard never
    ran. A refused request must destroy nothing."""
    cleaned = {"ran": False}

    def discard_spy(worktree, rel_paths):
        cleaned["ran"] = True  # pragma: no cover - must never run
        return None

    monkeypatch.setattr(repository, "_discard_untracked_files", discard_spy)

    async def git(path, *args, **kw):
        sub = args[0] if args else ""
        if sub == "status":
            return ""  # tracked clean
        if sub == "rev-parse":
            return "a" * 40
        return "a" * 40

    async def run_cmd(cmd, timeout=None, **kw):
        if "ls-files" in cmd:
            return (0, "note.txt\0", "")  # untracked-only -> discard approved
        if "worktree" in cmd and "remove" in cmd:
            raise AssertionError("removal must not run when the PR-not-merged gate refuses")
        return (0, "", "")

    # force=False + unmerged (OPEN) + own>0 -> the PR-not-merged gate returns
    # before the discard/removal block is ever reached.
    with _remove_stubs(git=git, run_cmd=run_cmd, pr_state="OPEN", own=1):
        res = await worktree_ops._worktree_remove_locked(
            "feat", force=False, discard_untracked_paths=["note.txt"]
        )
    assert res["ok"] is False
    assert "PR not merged" in res["error"]
    assert cleaned["ran"] is False, "the discard ran on a request that was refused"


@pytest.mark.asyncio
async def test_discard_aborts_removal_when_clean_fails():
    """discard requested but `git clean` fails (or dirt remains after it) ->
    error contains 'removal aborted' and `git worktree remove` never ran."""
    ran_remove = {"v": False}

    async def git(path, *args, **kw):
        sub = args[0] if args else ""
        if sub == "status":
            return ""  # tracked clean throughout
        if sub == "clean":
            return None  # git clean FAILED
        if sub == "rev-parse":
            return "a" * 40
        return "a" * 40

    async def run_cmd(cmd, timeout=None, **kw):
        if "ls-files" in cmd:
            return (0, "note.txt\0", "")  # still untracked even after failed clean
        if "worktree" in cmd and "remove" in cmd:
            ran_remove["v"] = True  # pragma: no cover - must not run
        return (0, "", "")

    with _remove_stubs(git=git, run_cmd=run_cmd, pr_state="OPEN", own=1):
        res = await worktree_ops._worktree_remove_locked(
            "feat", force=True, discard_untracked_paths=["note.txt"]
        )
    assert res["ok"] is False
    assert "removal aborted" in res["error"]
    assert ran_remove["v"] is False


@pytest.mark.asyncio
async def test_refusal_payload_carries_dirt_fields_and_legacy_substring():
    """A refusal payload carries dirty_tracked/dirty_untracked/
    dirty_untracked_paths, and the message still contains 'uncommitted
    changes' (other tests and the client depend on that substring)."""
    git = _git_by_subcommand({"status": " M a.py\n", "rev-parse": "a" * 40})

    async def run_cmd(cmd, timeout=None, **kw):
        if "ls-files" in cmd:
            return (0, "s1\0s2\0", "")
        return (0, "", "")

    with _remove_stubs(git=git, run_cmd=run_cmd, pr_state="OPEN"):
        res = await worktree_ops._worktree_remove_locked("feat", force=False)
    assert res["ok"] is False
    assert res["dirty_tracked"] is True
    assert res["dirty_untracked"] == 2
    assert res["dirty_untracked_paths"] == ["s1", "s2"]
    assert "uncommitted changes" in res["error"]


def test_dirt_detail_never_suggests_force_and_is_empty_when_unverifiable():
    """_dirt_detail never says 'use force to override' (force is refused for
    tracked edits too), and returns '' when tracked_dirty is None."""
    assert repository._dirt_detail(None, []) == ""
    assert repository._dirt_detail(None, ["a", "b"]) == ""
    detail = repository._dirt_detail(True, ["a.py"])
    assert "use force to override" not in detail
    assert "tracked files are modified" in detail


def test_dirt_fields_caps_paths_at_20_but_counts_the_true_total():
    """dirty_untracked_paths is capped at 20 for payload size while
    dirty_untracked reports the real total."""
    untracked = [f"f{i}.txt" for i in range(37)]
    fields = repository._dirt_fields(False, untracked)
    assert fields["dirty_untracked"] == 37
    assert len(fields["dirty_untracked_paths"]) == 20
    assert fields["dirty_untracked_paths"] == untracked[:20]


@pytest.mark.asyncio
async def test_prunable_verdict_carries_dirt_fields_for_dirty_tree():
    """_prunable's verdict for a dirty tree carries the three dirt fields so a
    preview can tell real edits apart from leftover session scratch."""
    git = _git_by_subcommand({"status": " M a.py\n"})

    async def run_cmd(cmd, timeout=None, **kw):
        assert "ls-files" in cmd, cmd
        return (0, "scratch.log\0note.txt\0", "")

    with (
        patch.object(
            fleet_state, "_pr_status_cached", new_callable=AsyncMock, return_value={"state": "OPEN"}
        ),
        patch.object(repository, "_own_commits_count", new_callable=AsyncMock, return_value=2),
        patch.object(repository, "_real_dirty", new_callable=AsyncMock, return_value=True),
        patch.object(runtime, "_run_cmd", side_effect=run_cmd),
        patch.object(repository, "_git", side_effect=git),
    ):
        v = await worktree_ops._prunable("/wt/feat", "feat/x")
    assert v["ok"] is False
    assert v["dirty_tracked"] is True
    assert v["dirty_untracked"] == 2
    assert v["dirty_untracked_paths"] == ["scratch.log", "note.txt"]


@pytest.mark.asyncio
async def test_remove_handler_rejects_non_bool_discard_untracked(monkeypatch):
    """discard_untracked_paths='yes' (not a list of non-empty strings) on the
    single-remove handler -> 400, carrying a machine-readable `code`.

    The code is the contract and the prose is advisory: the dashboard renders
    `error` verbatim into a localized UI, so a sentence alone is untranslatable.
    The repo ratchets this (test_error_code_contract), and it caught this very
    response missing its code.
    """
    _sel_capture(monkeypatch)
    monkeypatch.setattr(repository, "_valid_worktree_names", AsyncMock(return_value={"feat"}))
    remove = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(worktree_ops, "_worktree_remove", remove)
    resp = await http_api.api_dev_fleet_worktree_remove(
        _json_request({"name": "feat", "discard_untracked_paths": "yes"})
    )
    assert resp.status == 400
    body = json.loads(resp.text)
    assert body["code"] == "invalid_discard_paths"
    assert "discard_untracked_paths must be a list of non-empty strings" in body["error"]
    remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_prune_run_handler_rejects_malformed_discard_paths(monkeypatch):
    """discard_untracked_paths='x' (not a name->list map) -> 400 with code
    invalid_discard_paths."""
    _sel_capture(monkeypatch)
    resp = await http_api.api_dev_fleet_prune_run(
        _json_request({"names": [], "discard_untracked_paths": "x"})
    )
    assert resp.status == 400
    body = json.loads(resp.text)
    assert body["code"] == "invalid_discard_paths"


@pytest.mark.asyncio
async def test_prune_run_handler_guards_protected_worktree_in_discard_paths(monkeypatch):
    """A protected (live) worktree named ONLY in discard_untracked_paths is
    screened by the same guard as force_names -> 400 protected_worktree."""
    _sel_capture(monkeypatch)
    monkeypatch.setattr(repository, "_valid_worktree_names", AsyncMock(return_value={"live-wt"}))
    monkeypatch.setattr(live, "_live_worktree_path", AsyncMock(return_value="/repo/live-wt"))
    monkeypatch.setattr(live, "_staged_target", lambda: None)
    monkeypatch.setattr(
        repository,
        "_find_worktree",
        AsyncMock(return_value=({"path": "/repo/live-wt", "is_main": False}, None)),
    )
    resp = await http_api.api_dev_fleet_prune_run(
        _json_request({"names": [], "discard_untracked_paths": {"live-wt": ["n.txt"]}})
    )
    assert resp.status == 400
    body = json.loads(resp.text)
    assert body["code"] == "protected_worktree"
    assert "live-wt" in body["error"]


@pytest.mark.asyncio
async def test_prune_run_discard_only_name_reaches_remove_and_skips_recheck(monkeypatch):
    """A name present ONLY in discard_untracked_paths is now RE-CHECKED via
    _prunable (force's blanket bypass is not inherited), but a refusal
    whose code is in _DISCARD_OVERRIDABLE_CODES is overridden, so it still
    reaches _worktree_remove with the caller's consented path list."""
    prunable_calls: list[str] = []
    remove_kwargs: list[dict] = []

    async def fake_find(nm):
        return {"path": f"/wt/{nm}", "branch": f"feat/{nm}"}, None

    async def fake_prunable(path, branch):
        # An untracked-only tree previews as a REFUSAL; merged_dirty is one of
        # the codes a discard approval is allowed to override.
        prunable_calls.append(path)
        return {"ok": False, "code": "merged_dirty"}

    async def fake_remove(
        nm,
        force=False,
        progress=None,
        _caller="handler",
        discard_untracked_paths=None,
    ):
        remove_kwargs.append({"name": nm, "force": force, "discard": discard_untracked_paths})
        return {"ok": True, "removed": True}

    monkeypatch.setattr(worktree_ops, "_PRUNE_LOCK", asyncio.Lock())
    monkeypatch.setattr(
        worktree_ops,
        "_PRUNE_STATE",
        {"running": False, "total": 0, "done": 0, "current": None, "results": [], "items": {}},
    )
    with (
        patch.object(repository, "_find_worktree", side_effect=fake_find),
        patch.object(worktree_ops, "_prunable", side_effect=fake_prunable),
        patch.object(live, "_live_worktree_path", new_callable=AsyncMock, return_value=None),
        patch.object(live, "_staged_target", return_value=None),
        patch.object(worktree_ops, "_worktree_remove", side_effect=fake_remove),
    ):
        r = await worktree_ops._prune_run([], discard_paths={"wt-scratch": ["note.txt"]})
        assert r == {"ok": True, "total": 1}
        for _ in range(500):
            if not worktree_ops._PRUNE_STATE["running"]:
                break
            # flake-ok: pre-existing yield-poll, bounded at 500 turns, for the background prune to finish, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)

    assert worktree_ops._PRUNE_STATE["running"] is False
    # _prunable IS now consulted for a discard-only name...
    assert prunable_calls == ["/wt/wt-scratch"]
    # ...and its overridable refusal did not stop the removal: the name still
    # reached _worktree_remove carrying the exact consented list, force=False.
    assert remove_kwargs == [{"name": "wt-scratch", "force": False, "discard": ["note.txt"]}]


@pytest.mark.asyncio
async def test_discard_rel_paths_scoped_to_approved_paths(monkeypatch):
    """The discard is not a blanket sweep. `_discard_untracked_files`
    is handed EXACTLY the enumerated untracked paths, and a path that was never
    enumerated (never approved) is never handed to it."""
    discarded: list[tuple] = []

    def discard_spy(worktree, rel_paths):
        discarded.append((worktree, list(rel_paths)))
        return None  # success

    monkeypatch.setattr(repository, "_discard_untracked_files", discard_spy)

    async def git(path, *args, **kw):
        sub = args[0] if args else ""
        if sub == "status":
            return ""  # tracked clean
        if sub == "rev-parse":
            return "a" * 40
        return "a" * 40

    async def run_cmd(cmd, timeout=None, **kw):
        if "ls-files" in cmd:
            # two approved untracked paths before the discard; empty after
            return (0, "" if discarded else "note.txt\0probe.sh\0", "")
        return (0, "", "")

    with _remove_stubs(git=git, run_cmd=run_cmd, pr_state="OPEN", own=1):
        res = await worktree_ops._worktree_remove_locked(
            "feat", force=True, discard_untracked_paths=["note.txt", "probe.sh"]
        )
    assert res.get("ok") is True, res
    assert len(discarded) == 1, discarded
    _worktree, rel_paths = discarded[0]
    assert rel_paths == ["note.txt", "probe.sh"], rel_paths
    # A path the caller never approved is never handed to the discard.
    assert "secret.env" not in rel_paths


@pytest.mark.asyncio
async def test_discard_file_appearing_after_approval_is_not_destroyed():
    """CHANGE 1: a file created AFTER approval is outside the clean pathspec, so
    the post-clean `_dirty_split` still finds it untracked and the removal is
    ABORTED rather than widened to destroy it. `git worktree remove` never
    runs."""
    ran_remove = {"v": False}
    cleaned = {"v": False}

    async def git(path, *args, **kw):
        sub = args[0] if args else ""
        if sub == "status":
            return ""  # tracked clean throughout
        if sub == "clean":
            cleaned["v"] = True
            return ""  # clean itself succeeds
        if sub == "rev-parse":
            return "a" * 40
        return "a" * 40

    async def run_cmd(cmd, timeout=None, **kw):
        if "ls-files" in cmd:
            # BEFORE clean: one approved path. AFTER clean: a DIFFERENT untracked
            # path appears -- one that was never approved and never cleaned.
            return (0, "note.txt\0" if not cleaned["v"] else "appeared-after.txt\0", "")
        if "worktree" in cmd and "remove" in cmd:
            ran_remove["v"] = True  # pragma: no cover - must not run
        return (0, "", "")

    with _remove_stubs(git=git, run_cmd=run_cmd, pr_state="OPEN", own=1):
        res = await worktree_ops._worktree_remove_locked(
            "feat", force=True, discard_untracked_paths=["note.txt"]
        )
    assert res["ok"] is False
    assert "removal aborted" in res["error"]
    assert ran_remove["v"] is False, "worktree remove ran despite a file appearing after approval"


@pytest.mark.asyncio
async def test_dirty_split_untracked_paths_survive_byte_exact():
    """CHANGE 1 regression guard: the untracked half is read through `_run_cmd`
    precisely so filenames survive byte-exact -- a leading space on the first
    entry and a trailing space on the last would be eaten by the `_git` helper's
    `.strip()`. Feed such a payload and assert both survive intact."""
    git = _git_by_subcommand({"status": ""})

    async def run_cmd(cmd, timeout=None, **kw):
        assert "ls-files" in cmd, cmd
        # first entry has a LEADING space, last has a TRAILING space
        return (0, " leading.txt\0middle.txt\0trailing.txt \0", "")

    with (
        patch.object(repository, "_git", side_effect=git),
        patch.object(runtime, "_run_cmd", side_effect=run_cmd),
    ):
        tracked, untracked = await repository._dirty_split("/wt/x")
    assert tracked is False
    assert untracked == [" leading.txt", "middle.txt", "trailing.txt "]
    # The whitespace is preserved, not stripped (which the _git helper would do).
    assert untracked[0].startswith(" ")
    assert untracked[-1].endswith(" ")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code, should_reach_remove",
    [
        # NOT overridable by a discard: the refusal stands, removal never runs.
        ("dirty_check_failed", False),
        ("fresh", False),
        ("merged_unverified", False),
        ("merged_new_commits", False),
        # Overridable: the discard clears the very dirt that withheld it.
        ("merged_dirty", True),
        ("active", True),
    ],
)
async def test_discard_override_respects_overridable_codes(monkeypatch, code, should_reach_remove):
    """CHANGE 2: a discard-only approval overrides a prune refusal ONLY when the
    verdict code is in `_DISCARD_OVERRIDABLE_CODES`. A non-overridable code is
    refused with `not prunable: <code>` and never reaches `_worktree_remove`; an
    overridable code reaches it."""
    remove_kwargs: list[dict] = []

    async def fake_find(nm):
        return {"path": f"/wt/{nm}", "branch": f"feat/{nm}"}, None

    async def fake_prunable(path, branch):
        return {"ok": False, "code": code}

    async def fake_remove(
        nm,
        force=False,
        progress=None,
        _caller="handler",
        discard_untracked_paths=None,
    ):
        remove_kwargs.append({"name": nm, "force": force, "discard": discard_untracked_paths})
        return {"ok": True, "removed": True}

    monkeypatch.setattr(worktree_ops, "_PRUNE_LOCK", asyncio.Lock())
    monkeypatch.setattr(
        worktree_ops,
        "_PRUNE_STATE",
        {"running": False, "total": 0, "done": 0, "current": None, "results": [], "items": {}},
    )
    with (
        patch.object(repository, "_find_worktree", side_effect=fake_find),
        patch.object(worktree_ops, "_prunable", side_effect=fake_prunable),
        patch.object(live, "_live_worktree_path", new_callable=AsyncMock, return_value=None),
        patch.object(live, "_staged_target", return_value=None),
        patch.object(worktree_ops, "_worktree_remove", side_effect=fake_remove),
    ):
        r = await worktree_ops._prune_run([], discard_paths={"wt-x": ["note.txt"]})
        assert r == {"ok": True, "total": 1}
        for _ in range(500):
            if not worktree_ops._PRUNE_STATE["running"]:
                break
            # flake-ok: pre-existing yield-poll, bounded at 500 turns, for the background prune to finish, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)

    if should_reach_remove:
        assert remove_kwargs == [{"name": "wt-x", "force": False, "discard": ["note.txt"]}]
    else:
        assert remove_kwargs == [], f"{code} must not reach _worktree_remove"
        results = worktree_ops._PRUNE_STATE["items"]["wt-x"]
        assert results["status"] == "failed"
        assert results["error"] == f"not prunable: {code}"


# ---------------------------------------------------------------------------
# CONSENT-BY-PATH-LIST invariants (the reshaped discard API)
#
# A discard is honoured only when the caller submits the EXACT set it was
# shown: tracked-clean, a non-empty fresh untracked set, at most
# _DIRTY_PATH_SAMPLE of them, every path unchanged by _redact (a lossy
# rendering cannot identify one file, so a rewritten name is refused), and
# set(submitted) == set(fresh). The `git clean` pathspec then names those raw
# paths, each spelled `:(literal)` so no filename is read as pathspec magic.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discard_refused_when_submitted_omits_a_file_now_on_disk():
    """CONSENT MISMATCH: the caller's list omits a file that is now untracked
    on disk. The fresh set is larger than what was consented to, so the discard
    is refused with 'untracked files changed since they were listed'; neither
    `git clean` nor `git worktree remove` runs."""
    cleaned = {"ran": False}
    ran_remove = {"v": False}

    async def git(path, *args, **kw):
        sub = args[0] if args else ""
        if sub == "status":
            return ""  # tracked clean
        if sub == "clean":
            cleaned["ran"] = True  # pragma: no cover - must not run
            return ""
        if sub == "rev-parse":
            return "a" * 40
        return "a" * 40

    async def run_cmd(cmd, timeout=None, **kw):
        if "ls-files" in cmd:
            # disk carries TWO untracked files; the caller consented to one.
            return (0, "note.txt\0appeared.txt\0", "")
        if "worktree" in cmd and "remove" in cmd:
            ran_remove["v"] = True  # pragma: no cover - must not run
        return (0, "", "")

    with _remove_stubs(git=git, run_cmd=run_cmd, pr_state="MERGED", own=0):
        res = await worktree_ops._worktree_remove_locked(
            "feat", force=True, discard_untracked_paths=["note.txt"]
        )
    assert res["ok"] is False
    assert "untracked files changed since they were listed" in res["error"]
    assert cleaned["ran"] is False, "git clean ran on a consent mismatch"
    assert ran_remove["v"] is False, "worktree remove ran on a consent mismatch"


@pytest.mark.asyncio
async def test_discard_refused_when_submitted_names_a_file_no_longer_there():
    """CONSENT MISMATCH (reverse): the caller names a file that is not on
    disk. The submitted set differs from the fresh set, so the discard is
    refused with the same message; nothing is cleaned or removed."""
    cleaned = {"ran": False}
    ran_remove = {"v": False}

    async def git(path, *args, **kw):
        sub = args[0] if args else ""
        if sub == "status":
            return ""  # tracked clean
        if sub == "clean":
            cleaned["ran"] = True  # pragma: no cover - must not run
            return ""
        if sub == "rev-parse":
            return "a" * 40
        return "a" * 40

    async def run_cmd(cmd, timeout=None, **kw):
        if "ls-files" in cmd:
            # only note.txt survives; gone.txt the caller listed is not here
            return (0, "note.txt\0", "")
        if "worktree" in cmd and "remove" in cmd:
            ran_remove["v"] = True  # pragma: no cover - must not run
        return (0, "", "")

    with _remove_stubs(git=git, run_cmd=run_cmd, pr_state="MERGED", own=0):
        res = await worktree_ops._worktree_remove_locked(
            "feat", force=True, discard_untracked_paths=["note.txt", "gone.txt"]
        )
    assert res["ok"] is False
    assert "untracked files changed since they were listed" in res["error"]
    assert cleaned["ran"] is False, "git clean ran on a consent mismatch"
    assert ran_remove["v"] is False, "worktree remove ran on a consent mismatch"


@pytest.mark.asyncio
async def test_discard_refused_when_more_than_sample_untracked_files():
    """TRUNCATION: 21 untracked files exceed _DIRTY_PATH_SAMPLE (20), so the
    caller was handed a truncated list and cannot have consented to the whole
    set. Refuse with 'too many untracked files to confirm individually';
    nothing cleaned or removed."""
    cleaned = {"ran": False}
    ran_remove = {"v": False}
    n = repository._DIRTY_PATH_SAMPLE + 1
    files = [f"f{i}.txt" for i in range(n)]

    async def git(path, *args, **kw):
        sub = args[0] if args else ""
        if sub == "status":
            return ""
        if sub == "clean":
            cleaned["ran"] = True  # pragma: no cover - must not run
            return ""
        if sub == "rev-parse":
            return "a" * 40
        return "a" * 40

    async def run_cmd(cmd, timeout=None, **kw):
        if "ls-files" in cmd:
            return (0, "\0".join(files) + "\0", "")
        if "worktree" in cmd and "remove" in cmd:
            ran_remove["v"] = True  # pragma: no cover - must not run
        return (0, "", "")

    with _remove_stubs(git=git, run_cmd=run_cmd, pr_state="MERGED", own=0):
        res = await worktree_ops._worktree_remove_locked(
            "feat", force=True, discard_untracked_paths=files
        )
    assert res["ok"] is False
    assert "too many untracked files to confirm individually" in res["error"]
    assert res["dirty_untracked"] == n
    assert cleaned["ran"] is False, "git clean ran on an oversized untracked set"
    assert ran_remove["v"] is False, "worktree remove ran on an oversized set"


@pytest.mark.asyncio
async def test_discard_accepts_exactly_sample_untracked_files(monkeypatch):
    """TRUNCATION boundary: exactly _DIRTY_PATH_SAMPLE (20) files are still
    within the shown list, so a matching consent set is ACCEPTED, the discard
    runs against all 20, and the removal proceeds."""
    discarded: list[tuple] = []
    n = repository._DIRTY_PATH_SAMPLE
    files = [f"f{i}.txt" for i in range(n)]

    def discard_spy(worktree, rel_paths):
        discarded.append((worktree, list(rel_paths)))
        return None  # success

    monkeypatch.setattr(repository, "_discard_untracked_files", discard_spy)

    async def git(path, *args, **kw):
        sub = args[0] if args else ""
        if sub == "status":
            return ""
        if sub == "rev-parse":
            return "a" * 40
        return "a" * 40

    async def run_cmd(cmd, timeout=None, **kw):
        if "ls-files" in cmd:
            # all 20 before the discard; empty afterwards
            return (0, ("\0".join(files) + "\0") if not discarded else "", "")
        return (0, "", "")

    with _remove_stubs(git=git, run_cmd=run_cmd, pr_state="MERGED", own=0):
        res = await worktree_ops._worktree_remove_locked(
            "feat", force=True, discard_untracked_paths=files
        )
    assert res.get("ok") is True, res
    assert len(discarded) == 1, discarded
    _worktree, rel_paths = discarded[0]
    assert rel_paths == files, rel_paths
    assert len(rel_paths) == n


def test_dirt_fields_and_detail_pass_paths_through_redact(monkeypatch):
    """REDACTION: `_dirt_fields` and `_dirt_detail` emit paths passed through
    `_redact`. Patch `_redact` with a recognisable transform and assert the
    emitted values carry it."""
    monkeypatch.setattr(runtime, "_redact", lambda s: f"REDACTED::{s}")
    untracked = ["a.txt", "b.txt", "c.txt", "d.txt"]

    fields = repository._dirt_fields(False, untracked)
    assert fields["dirty_untracked_paths"] == [f"REDACTED::{p}" for p in untracked]
    # the count is the true total, unredacted
    assert fields["dirty_untracked"] == 4

    detail = repository._dirt_detail(False, untracked)
    # _dirt_detail names the first three, each redacted
    assert "REDACTED::a.txt" in detail
    assert "REDACTED::b.txt" in detail
    assert "REDACTED::c.txt" in detail
    # the raw (unredacted) name must not leak into the detail string
    assert "a.txt" in detail  # substring of REDACTED::a.txt, expected
    assert detail.count("REDACTED::") == 3


@pytest.mark.asyncio
async def test_discard_emitted_paths_are_redacted_but_pathspec_is_raw(monkeypatch):
    """REDACTION: with `_redact` patched to a non-identity transform, a refusal
    payload's emitted paths carry the transform. The discard itself is refused
    outright in that case -- a lossy rendering cannot identify the consented
    file -- so `_discard_untracked_files` is never called."""
    monkeypatch.setattr(runtime, "_redact", lambda s: f"R::{s}")
    discarded: list[tuple] = []

    def discard_spy(worktree, rel_paths):
        discarded.append((worktree, list(rel_paths)))  # pragma: no cover - refusal, must not run
        return None

    monkeypatch.setattr(repository, "_discard_untracked_files", discard_spy)

    async def git(path, *args, **kw):
        sub = args[0] if args else ""
        if sub == "status":
            return ""
        if sub == "rev-parse":
            return "a" * 40
        return "a" * 40

    async def run_cmd(cmd, timeout=None, **kw):
        if "ls-files" in cmd:
            return (0, "note.txt\0", "")
        return (0, "", "")

    # Submit the RAW name. Redaction is not the identity here, so the discard is
    # refused BEFORE any set comparison: a rewritten name cannot be pinned to
    # one file. Emitted paths still carry the transform.
    with _remove_stubs(git=git, run_cmd=run_cmd, pr_state="MERGED", own=0):
        res = await worktree_ops._worktree_remove_locked(
            "feat", force=True, discard_untracked_paths=["note.txt"]
        )
    assert res["ok"] is False
    assert "cannot be confirmed safely" in res["error"]
    # emitted dirt paths carry the redaction transform
    assert res["dirty_untracked_paths"] == ["R::note.txt"]
    # and nothing was discarded (the discard was never called)
    assert discarded == [], "the discard ran despite an unconfirmable filename"


@pytest.mark.asyncio
async def test_discard_refused_when_a_filename_is_rewritten_by_redaction(monkeypatch):
    """REDACTION COLLISION: `_redact` is lossy, so two distinct filenames can
    share one displayed rendering. Comparing consent in that space would let a
    file swapped in after display satisfy the equality check and be destroyed
    unapproved. So a discard is refused outright whenever ANY fresh path is
    rewritten on the way out -- even when the client faithfully echoes exactly
    what it was shown. Refusing costs a manual cleanup; the alternative loses a
    file nobody approved."""
    monkeypatch.setattr(runtime, "_redact", lambda s: f"R::{s}")
    discarded: list[tuple] = []

    def discard_spy(worktree, rel_paths):
        discarded.append((worktree, list(rel_paths)))  # pragma: no cover - must never run
        return None

    monkeypatch.setattr(repository, "_discard_untracked_files", discard_spy)

    async def git(path, *args, **kw):
        sub = args[0] if args else ""
        if sub == "status":
            return ""
        if sub == "rev-parse":
            return "a" * 40
        return "a" * 40

    async def run_cmd(cmd, timeout=None, **kw):
        if "ls-files" in cmd:
            return (0, "note.txt\0probe.sh\0", "")
        return (0, "", "")

    # The client echoes exactly the redacted forms `_dirt_fields` showed it.
    # Under the old two-space rule this was ACCEPTED; it is now refused, because
    # `R::note.txt` cannot be traced back to one file on disk.
    with _remove_stubs(git=git, run_cmd=run_cmd, pr_state="MERGED", own=0):
        res = await worktree_ops._worktree_remove_locked(
            "feat",
            force=True,
            discard_untracked_paths=["R::note.txt", "R::probe.sh"],
        )
    assert res["ok"] is False
    assert "cannot be confirmed safely" in res["error"]
    assert discarded == [], "the discard ran on a set that redaction had rewritten"


@pytest.mark.asyncio
async def test_discard_accepted_when_redaction_is_identity(monkeypatch):
    """The control for the guard above: with `_redact` left as the identity on
    these filenames, the echoed set matches the raw set and the discard runs,
    handed exactly those raw literal paths (no `:(literal)` wrapping)."""
    monkeypatch.setattr(runtime, "_redact", lambda s: s)
    discarded: list[tuple] = []

    def discard_spy(worktree, rel_paths):
        discarded.append((worktree, list(rel_paths)))
        return None  # success

    monkeypatch.setattr(repository, "_discard_untracked_files", discard_spy)

    async def git(path, *args, **kw):
        sub = args[0] if args else ""
        if sub == "status":
            return ""
        if sub == "rev-parse":
            return "a" * 40
        return "a" * 40

    async def run_cmd(cmd, timeout=None, **kw):
        if "ls-files" in cmd:
            return (0, ("note.txt\0probe.sh\0") if not discarded else "", "")
        return (0, "", "")

    with _remove_stubs(git=git, run_cmd=run_cmd, pr_state="MERGED", own=0):
        res = await worktree_ops._worktree_remove_locked(
            "feat",
            force=True,
            discard_untracked_paths=["note.txt", "probe.sh"],
        )
    assert res.get("ok") is True, res
    assert len(discarded) == 1, discarded
    _worktree, rel_paths = discarded[0]
    # exactly the raw approved paths, no `:(literal)` wrapping
    assert rel_paths == ["note.txt", "probe.sh"], rel_paths
    assert not any(p.startswith(":(literal)") for p in rel_paths), rel_paths


@pytest.mark.asyncio
async def test_remove_handler_rejects_discard_bool(monkeypatch):
    """HANDLER VALIDATION: discard_untracked_paths as a bool -> 400, remove
    never called."""
    _sel_capture(monkeypatch)
    monkeypatch.setattr(repository, "_valid_worktree_names", AsyncMock(return_value={"feat"}))
    remove = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(worktree_ops, "_worktree_remove", remove)
    resp = await http_api.api_dev_fleet_worktree_remove(
        _json_request({"name": "feat", "discard_untracked_paths": True})
    )
    assert resp.status == 400
    assert (
        "discard_untracked_paths must be a list of non-empty strings"
        in json.loads(resp.text)["error"]
    )
    remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_remove_handler_rejects_discard_list_with_empty_string(monkeypatch):
    """HANDLER VALIDATION: discard_untracked_paths containing an empty string
    -> 400, remove never called."""
    _sel_capture(monkeypatch)
    monkeypatch.setattr(repository, "_valid_worktree_names", AsyncMock(return_value={"feat"}))
    remove = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(worktree_ops, "_worktree_remove", remove)
    resp = await http_api.api_dev_fleet_worktree_remove(
        _json_request({"name": "feat", "discard_untracked_paths": ["ok.txt", ""]})
    )
    assert resp.status == 400
    assert (
        "discard_untracked_paths must be a list of non-empty strings"
        in json.loads(resp.text)["error"]
    )
    remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_prune_run_handler_rejects_discard_list_instead_of_map(monkeypatch):
    """HANDLER VALIDATION: prune's discard_untracked_paths given as a LIST
    instead of a name->list map -> 400 with code invalid_discard_paths."""
    _sel_capture(monkeypatch)
    resp = await http_api.api_dev_fleet_prune_run(
        _json_request({"names": [], "discard_untracked_paths": ["wt-a"]})
    )
    assert resp.status == 400
    body = json.loads(resp.text)
    assert body["code"] == "invalid_discard_paths"


@pytest.mark.asyncio
async def test_prune_run_handler_rejects_discard_map_with_empty_string_entry(monkeypatch):
    """HANDLER VALIDATION: prune's discard_untracked_paths map carrying a list
    with an empty string -> 400 with code invalid_discard_paths."""
    _sel_capture(monkeypatch)
    resp = await http_api.api_dev_fleet_prune_run(
        _json_request({"names": [], "discard_untracked_paths": {"wt-a": ["ok.txt", ""]}})
    )
    assert resp.status == 400
    body = json.loads(resp.text)
    assert body["code"] == "invalid_discard_paths"


@pytest.mark.asyncio
async def test_locked_worktree_refused_before_any_discard_runs(monkeypatch):
    """LOCK ORDERING: git refuses to remove a locked worktree, and it refuses
    LAST -- a discard succeeds on a locked tree, so a discard that ran first
    would already have destroyed the scratch by the time the removal failed.
    The lock is therefore recognised up front and the discard must never run."""
    clean_ran: list[tuple] = []

    def discard_spy(worktree, rel_paths):
        clean_ran.append((worktree, list(rel_paths)))  # pragma: no cover - must never happen
        return None

    monkeypatch.setattr(repository, "_discard_untracked_files", discard_spy)

    async def git(path, *args, **kw):
        sub = args[0] if args else ""
        if sub == "status":
            return ""
        return "a" * 40

    async def run_cmd(cmd, timeout=None, **kw):
        if "ls-files" in cmd:
            return (0, "probe.py\0", "")
        return (0, "", "")

    with _remove_stubs(
        git=git,
        run_cmd=run_cmd,
        pr_state="MERGED",
        own=0,
        target={
            "path": "/wt/held",
            "branch": "held",
            "is_main": False,
            "locked": "keeping this for the repro",
        },
    ):
        res = await worktree_ops._worktree_remove_locked(
            "held", force=True, discard_untracked_paths=["probe.py"]
        )
    assert res["ok"] is False
    assert "locked" in res["error"]
    assert "git worktree unlock" in res["error"]
    assert clean_ran == [], "the discard ran against a locked worktree"


@pytest.mark.asyncio
async def test_removal_failure_after_discard_says_files_were_discarded(monkeypatch):
    """Every removal failure we can name in advance is refused before the
    discard, but the unnameable ones (a permission change, a file held open)
    still land after it. The error must then SAY the files are gone, instead of
    returning git's bare stderr and reading like a no-op."""

    cleaned: list[tuple] = []

    def discard_spy(worktree, rel_paths):
        cleaned.append((worktree, list(rel_paths)))  # discard succeeds
        return None

    monkeypatch.setattr(repository, "_discard_untracked_files", discard_spy)

    async def git(path, *args, **kw):
        sub = args[0] if args else ""
        if sub == "status":
            return ""
        return "a" * 40

    calls: list[list] = []

    async def run_cmd(cmd, timeout=None, **kw):
        if "ls-files" in cmd:
            # Empty once the discard has run, so the post-discard check passes
            # and execution reaches the removal -- which is where this test's
            # failure is injected.
            return (0, "" if cleaned else "probe.py\0", "")
        if "remove" in cmd:
            calls.append(cmd)
            return (1, "", "fatal: could not remove: Permission denied")
        return (0, "", "")

    with _remove_stubs(git=git, run_cmd=run_cmd, pr_state="MERGED", own=0):
        res = await worktree_ops._worktree_remove_locked(
            "feat", force=True, discard_untracked_paths=["probe.py"]
        )
    assert res["ok"] is False
    assert "discarded 1 untracked file(s)" in res["error"]
    assert "could not remove the worktree" in res["error"]
    # git's own reason is preserved, not swallowed by the wrapper text
    assert "Permission denied" in res["error"]


@pytest.mark.asyncio
async def test_discard_waits_for_a_fresh_lease_proof_and_deletes_nothing_when_refused(
    monkeypatch,
):
    """When a discard is pending, the approved deletion is the point of no return — so
    the lease is proven FRESH (a renewal through the gateway) immediately before it. A
    gateway that will not renew must leave every file in place and say so."""
    cleaned: list[tuple] = []

    def discard_spy(worktree, rel_paths):
        cleaned.append((worktree, list(rel_paths)))
        return None

    monkeypatch.setattr(repository, "_discard_untracked_files", discard_spy)

    async def git(path, *args, **kw):
        sub = args[0] if args else ""
        if sub == "status":
            return ""
        return "a" * 40

    git_ran: list[list] = []

    async def run_cmd(cmd, timeout=None, **kw):
        if "ls-files" in cmd:
            return (0, "probe.py\0", "")
        if "remove" in cmd:
            git_ran.append(cmd)
            return (0, "", "")
        return (0, "", "")

    async def acquire(path: str):
        return "cap-x"

    async def renew(token: str) -> bool:
        return False  # the gateway forgot the lease before the discard

    async def release(token: str) -> None:
        pass

    live.install_removal_lease_client((acquire, renew, release))
    try:
        with _remove_stubs(git=git, run_cmd=run_cmd, pr_state="MERGED", own=0):
            res = await worktree_ops._worktree_remove_locked(
                "feat", force=True, discard_untracked_paths=["probe.py"]
            )
    finally:
        live.install_removal_lease_client(None)
    assert res["ok"] is False
    assert "left in place" in res["error"] and "nothing was deleted" in res["error"]
    assert cleaned == [], "the discard must not run without a fresh lease proof"
    assert git_ran == []


@pytest.mark.asyncio
async def test_lease_refusal_after_the_discard_says_the_files_are_gone(monkeypatch):
    """If the gateway stops renewing between the discard and the git spawn, the
    pre-spawn gate refuses — and the report must say the discard already happened,
    never a bland 'retry'."""
    cleaned: list[tuple] = []

    def discard_spy(worktree, rel_paths):
        cleaned.append((worktree, list(rel_paths)))
        return None

    monkeypatch.setattr(repository, "_discard_untracked_files", discard_spy)

    async def git(path, *args, **kw):
        sub = args[0] if args else ""
        if sub == "status":
            return ""
        return "a" * 40

    async def run_cmd(cmd, timeout=None, **kw):
        if "ls-files" in cmd:
            return (0, "" if cleaned else "probe.py\0", "")
        if "remove" in cmd:
            gate = kw.get("pre_spawn")
            refusal = await gate() if gate else None
            if refusal is not None:
                return (-1, "", refusal)
            raise AssertionError("git must not run after a refused gate")
        return (0, "", "")

    answers = [True, False]  # first proof (pre-discard) ok; pre-spawn proof refused

    async def acquire(path: str):
        return "cap-y"

    async def renew(token: str) -> bool:
        return answers.pop(0) if answers else False

    async def release(token: str) -> None:
        pass

    live.install_removal_lease_client((acquire, renew, release))
    try:
        with _remove_stubs(git=git, run_cmd=run_cmd, pr_state="MERGED", own=0):
            res = await worktree_ops._worktree_remove_locked(
                "feat", force=True, discard_untracked_paths=["probe.py"]
            )
    finally:
        live.install_removal_lease_client(None)
    assert res["ok"] is False
    assert cleaned, "the discard ran under a fresh proof"
    assert "discarded 1 untracked file(s)" in res["error"]
    assert "could not confirm that no cutover overlaps this removal" in res["error"]


@pytest.mark.asyncio
async def test_partial_discard_failure_reports_the_files_already_deleted(monkeypatch, tmp_path):
    """The per-file helper can delete N-1 approved files and then fail on the Nth.
    That refusal is not a no-op and must say how many are already gone."""
    (tmp_path / "keep.txt").write_text("x")  # the one the helper "failed" on

    ran = {"v": False}

    def discard_partial(worktree, rel_paths):
        ran["v"] = True
        return "could not discard 'keep.txt': Permission denied"

    monkeypatch.setattr(repository, "_discard_untracked_files", discard_partial)

    async def git(path, *args, **kw):
        sub = args[0] if args else ""
        if sub == "status":
            return ""
        return "a" * 40

    async def run_cmd(cmd, timeout=None, **kw):
        if "ls-files" in cmd:
            listing = "keep.txt\0" if ran["v"] else "a.txt\0b.txt\0keep.txt\0"
            return (0, listing, "")
        if "worktree" in cmd and "remove" in cmd:
            raise AssertionError("removal must not run after a refused discard")
        return (0, "", "")

    with _remove_stubs(
        git=git,
        run_cmd=run_cmd,
        pr_state="MERGED",
        own=0,
        target={"path": str(tmp_path), "branch": "feat/x", "is_main": False},
    ):
        res = await worktree_ops._worktree_remove_locked(
            "feat", force=True, discard_untracked_paths=["a.txt", "b.txt", "keep.txt"]
        )
    assert res["ok"] is False
    assert "Permission denied" in res["error"]
    assert "2 of 3 approved untracked file(s) were already deleted" in res["error"]
    assert "removal aborted" in res["error"]


@pytest.mark.asyncio
async def test_post_discard_dirt_reports_the_approved_files_as_gone(monkeypatch, tmp_path):
    """Every approved file was deleted, then a new untracked file is found: the
    refusal must not read as if nothing happened."""

    ran = {"v": False}

    def discard_ok(worktree, rel_paths):
        ran["v"] = True
        return None

    monkeypatch.setattr(repository, "_discard_untracked_files", discard_ok)

    async def git(path, *args, **kw):
        sub = args[0] if args else ""
        if sub == "status":
            return ""
        return "a" * 40

    async def run_cmd(cmd, timeout=None, **kw):
        if "ls-files" in cmd:
            return (0, "fresh.log\0" if ran["v"] else "a.txt\0", "")  # appeared after approval
        if "worktree" in cmd and "remove" in cmd:
            raise AssertionError("removal must not run after a refused discard")
        return (0, "", "")

    with _remove_stubs(
        git=git,
        run_cmd=run_cmd,
        pr_state="MERGED",
        own=0,
        target={"path": str(tmp_path), "branch": "feat/x", "is_main": False},
    ):
        res = await worktree_ops._worktree_remove_locked(
            "feat", force=True, discard_untracked_paths=["a.txt"]
        )
    assert res["ok"] is False
    assert "1 untracked file(s) remain" in res["error"]
    assert "1 of 1 approved untracked file(s) were already deleted" in res["error"]
