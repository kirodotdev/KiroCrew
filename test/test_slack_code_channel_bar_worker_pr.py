"""A helper agent's PR on the code channel's context bar.

With no PR of the channel's own, the bar's one PR slot shows the newest open
PR a worker opened (Slack's partners found it buried in the reply text, the
bar left empty until someone asked the agent). The channel's own PR takes the
slot back; a merged or closed worker PR drops off; a channel the board's scope
refuses gets none of it.

Pure parts are tested on :func:`build_items`; the service runs real git and the
real refresh with the board suite's harness (a fake board reader and forge).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import test_slack_code_channel_board as suite
import test_slack_code_channel_board_live as live

from kiro_crew.slack.code_channels import agent_bar, surface
from kiro_crew.slack.code_channels.board import model, source
from kiro_crew.slack.code_channels.workspace import git_ops, service
from kiro_crew.slack.code_channels.workspace.context_bar import (
    ACTION_CREATE_PR,
    BarState,
    build_items,
)
from kiro_crew.slack.code_channels.workspace.pr import PrLookup, PullRequest
from kiro_crew.slack.code_channels.workspace.remote import RepoRef

# The board suites' fixtures (autouse ones included), bound here so they apply.
_no_sandbox = suite._no_sandbox
_feature_on = suite._feature_on
_surface_store = suite._surface_store
_pinned_identity = suite._pinned_identity
publisher_clock = suite.publisher_clock
repo = suite.repo
_repo_template = suite._repo_template
_Harness = suite._Harness
_FakeBinding = suite._FakeBinding
_row = suite._row

_URL = "https://github.com/acme/widgets/pull/"


def _found(n: int, state: str = "open", ci: str = "passed") -> PrLookup:
    return PrLookup("found", PullRequest(n, _URL + str(n), state, ci))


def _pr_item(items: list[dict[str, str]]) -> dict[str, str] | None:
    return next((i for i in items if i["key"] == "pr"), None)


def _board(*rows: model.WorkerRow) -> model.BoardSnapshot:
    return model.BoardSnapshot(goal="Ship it", rows=rows)


def _with_pr(n: int, state: str, item: str = "it_00000001", **kw: Any) -> model.WorkerRow:
    row = _row(item, kw.pop("row_state", model.WORKING), branch=f"feat/{item}", **kw)
    return row.with_pr(n, _URL + str(n) if state else "", state, "")


def _state(**kw: Any) -> BarState:
    base: dict[str, Any] = dict(repo=RepoRef("github.com", "acme", "widgets"), branch="kc/chan")
    base.update(kw)
    return BarState(**base)


# ── the PR slot ────────────────────────────────────────────────────────────


def test_with_no_own_pr_the_slot_shows_the_newest_open_worker_pr() -> None:
    board = _board(
        _with_pr(7, "open"),
        _with_pr(9, "merged", "it_00000002"),
        _with_pr(8, "draft", "it_00000003"),
    )
    # Two live worker PRs: the newest (highest number, one repository) wins;
    # the merged #9 is newer still but not open.
    assert _pr_item(build_items(_state(board=board))) == {
        "key": "pr",
        "label": "Draft PR #8",
        "icon": "link",
        "url": _URL + "8",
    }


def test_the_channels_own_pr_in_any_state_keeps_the_slot() -> None:
    board = _board(_with_pr(7, "open"))
    for state, label in (("open", "PR #42 is open"), ("merged", "PR #42 merged")):
        own = PullRequest(42, _URL + "42", state, "")
        assert _pr_item(build_items(_state(pr=own, board=board)))["label"] == label


def test_the_channels_own_create_pr_keeps_the_slot_over_a_worker_pr() -> None:
    board = _board(_with_pr(7, "open"))
    keys = [
        i["key"]
        for i in build_items(_state(board=board, can_open_pr=True, branch_has_commits=True))
    ]
    assert ACTION_CREATE_PR in keys and "pr" not in keys


@pytest.mark.parametrize(
    "row",
    [
        _with_pr(7, "merged"),
        _with_pr(7, "closed"),
        # A number the worker reported that the forge could not confirm: no state.
        _with_pr(7, ""),
        # The conductor rejected or abandoned the item: its PR is not put forward.
        _with_pr(7, "open", row_state=model.CLOSED, closed_as="rejected"),
    ],
)
def test_no_live_confirmed_worker_pr_leaves_the_slot_empty(row: model.WorkerRow) -> None:
    assert _pr_item(build_items(_state(board=_board(row)))) is None


# ── the service: a worker's PR reaches the bar on its own ──────────────────


def _bar(h: Any) -> list[dict[str, str]]:
    return h.client.last("set_context_bar")["items"]


def test_a_workers_pr_reaches_the_bar_without_any_agent_call_then_gives_way(
    tmp_path: Any, repo: Any, monkeypatch: Any
) -> None:
    # Every PR read is fresh, so each refresh sees the forge's latest answer.
    monkeypatch.setattr(service, "_PR_TTL_LIVE", 0)
    h = _Harness(tmp_path, snapshot=_board(_row("it_00000001", branch="feat/w1", pr_number=7)))
    h.prs["#7"] = _found(7)
    binding = _FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    asyncio.run(h.svc.refresh(binding))
    # The refresh alone put it there: the agent set no item of its own.
    assert agent_bar.agent_items("C1") == ()
    assert _pr_item(_bar(h)) == {
        "key": "pr",
        "label": "PR #7 is open · tests passing",
        "icon": "link",
        "url": _URL + "7",
    }
    # The channel opens a PR of its own: it takes the slot.
    h.prs[rec.branch] = _found(12, ci="running")
    asyncio.run(h.svc.refresh(binding))
    assert _pr_item(_bar(h))["label"] == "PR #12 is open · tests running"
    # Without it again, a merged worker PR drops off the bar.
    h.prs.pop(rec.branch)
    h.prs["#7"] = _found(7, "merged")
    asyncio.run(h.svc.refresh(binding))
    assert _pr_item(_bar(h)) is None


def test_two_workers_with_prs_show_the_newest(tmp_path: Any, repo: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr(service, "_PR_TTL_LIVE", 0)
    rows = (
        _row("it_00000001", branch="feat/w1", pr_number=7),
        _row("it_00000002", branch="feat/w2", pr_number=8),
    )
    h = _Harness(tmp_path, snapshot=_board(*rows))
    h.prs.update({"#7": _found(7), "#8": _found(8)})
    binding = _FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    asyncio.run(h.svc.refresh(binding))
    assert _pr_item(_bar(h))["url"] == _URL + "8"
    # The newer one merges: the other, still open, takes the slot.
    h.prs["#8"] = _found(8, "merged")
    asyncio.run(h.svc.refresh(binding))
    assert _pr_item(_bar(h))["url"] == _URL + "7"


def test_a_worker_pr_in_another_repository_never_reaches_the_bar(
    tmp_path: Any, repo: Any, monkeypatch: Any
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    w1 = live._live_slot("chat-w1", project=str(other))
    monkeypatch.setattr(surface, "dashboard_state", lambda: live._Slots(**{"chat-w1": w1}))

    def remote(cwd: Any) -> str:
        if str(cwd).startswith(str(other)):
            return "git@github.com:acme/other.git"
        return "git@github.com:acme/widgets"

    monkeypatch.setattr(git_ops, "remote_url", remote)
    snap = _board(_row("it_00000001", branch="feat/b", pr_number=12, session_key="chat-w1"))
    h = _Harness(tmp_path, snapshot=snap)
    h.prs["#12"] = _found(12)
    binding = _FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    asyncio.run(h.svc.refresh(binding))
    assert _pr_item(_bar(h)) is None


def test_a_channel_the_boards_scope_refuses_gets_no_worker_pr(tmp_path: Any, repo: Any) -> None:
    h = _Harness(tmp_path, snapshot=_board(_row("it_00000001", branch="feat/w1", pr_number=7)))
    h.prs["#7"] = _found(7)
    # Shared, archived, Off or Review: the board's scope check says no.
    source.set_scope_predicate(lambda state, binding: False)
    binding = _FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    asyncio.run(h.svc.refresh(binding))
    keys = [i["key"] for i in _bar(h)]
    assert "pr" not in keys and "workers" not in keys
