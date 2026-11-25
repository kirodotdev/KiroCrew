"""Code channels: the channel-wide pull request status, from the channel's own PR.

Sent with ``agents.conversations.setProperties`` in a call of its own: the value
is ``context_bar.pr_status_for`` of the real state of the channel's own PR (the
workspace record's) with its last-read CI. Helper agents' PRs never change it.

The refresh tests reuse ``test_slack_code_channel_workspace``'s harness, imported
by name so its tests are not collected twice.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import test_slack_code_channel_workspace as _ws

from kiro_crew.slack.code_channels.client import CodeChannelsClient
from kiro_crew.slack.code_channels.models import CodeChannelApiError, CodeChannelRequestError
from kiro_crew.slack.code_channels.workspace import context_bar, publishing
from kiro_crew.slack.code_channels.workspace.context_bar import PR_CI_UNREAD
from kiro_crew.slack.code_channels.workspace.pr import PrLookup, PullRequest
from kiro_crew.slack.code_channels.workspace.remote import RepoRef
from kiro_crew.slack.code_channels.workspace.service import WorkspaceService
from kiro_crew.slack.code_channels.workspace.store import WorkspaceRecord

FakeBinding = _ws.FakeBinding
Harness = _ws.Harness
_bound = _ws._bound
# Fixtures, by name: pytest finds a fixture through the module's namespace.
_no_sandbox = _ws._no_sandbox
_feature_on = _ws._feature_on
audits = _ws.audits
_repo_template = _ws._repo_template
repo = _ws.repo

CH = "C0CODE"
_URL = "https://github.com/acme/widgets/pull/42"


@pytest.fixture(autouse=True)
def _no_held_channels() -> Any:
    """A refusal holds ``pr_status`` per channel for the process: never across tests."""
    held = getattr(publishing, "_pr_status_held", None)
    if held is not None:
        held.clear()
    yield
    if held is not None:
        held.clear()


def _record(**kw: Any) -> WorkspaceRecord:
    base: dict[str, Any] = dict(
        team_id="T1",
        channel_id=CH,
        pr_number=42,
        pr_url=_URL,
        pr_state="draft",
        pr_title="Fix",
        pr_ci_read="",  # read and settled: the PR has no checks
    )
    return WorkspaceRecord(**{**base, **kw})


class _Client:
    """The one write this module makes, recorded; *fail* raises Slack's answer."""

    def __init__(self, fail: str = "") -> None:
        self.sent: list[str] = []
        self.fail = fail

    async def set_pr_status(self, channel_id: str, value: str) -> None:
        self.sent.append(value)
        if self.fail:
            raise CodeChannelApiError("agents.conversations.setProperties", self.fail)


def _publish(svc: WorkspaceService, record: WorkspaceRecord, binding: Any = None) -> None:
    asyncio.run(publishing.publish_pr_status(svc, record, binding))


# ── the client ─────────────────────────────────────────────────────────────────


def test_the_client_sends_pr_status_alone_at_its_place_and_refuses_other_values() -> None:
    sent: list[dict[str, Any]] = []

    async def api_call(method: str, **kw: Any) -> Any:
        sent.append({"method": method, **kw.get("json", {})})
        return {"ok": True}

    client = CodeChannelsClient(api_call)
    for value in ("draft", ""):  # "" clears the status
        asyncio.run(client.set_pr_status(CH, value))
        # Its own call: a refused value can take no context bar or resource with it.
        assert sent[-1] == {
            "method": "agents.conversations.setProperties",
            "channel_id": CH,
            "code_channel": {"pr_status": value},
        }
    for bad in ("DRAFT", "draft ", " open", "ready", "not_a_status", None, 1):
        with pytest.raises(CodeChannelRequestError):
            asyncio.run(client.set_pr_status(CH, bad))  # type: ignore[arg-type]
    assert len(sent) == 2


# ── the table (pure) ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "pr,status",
    [
        (("draft", "failed", "changes_requested", "conflicting"), "draft"),
        (("merged", "failed", "", ""), "merged"),
        (("closed", "", "", "conflicting"), "closed"),
        (("open", "failed", "", ""), "blocked"),
        (("open", "passed", "changes_requested", ""), "blocked"),
        (("open", "passed", "approved", "conflicting"), "blocked"),
        (("open", "running", "", ""), "open"),
        (("open", "passed", "approved", "mergeable"), "open"),
        (("open", "", "review_required", ""), "open"),
        (("open", "", "", ""), "open"),  # a PR with no CI, review or merge data
        (("", "", "", ""), None),
        (("weird", "", "", ""), None),
    ],
)
def test_the_pr_maps_from_its_real_state(pr: tuple[str, str, str, str], status: Any) -> None:
    assert context_bar.pr_status_for(*pr) == status
    # Every status the table gives is one the client sends.
    assert status is None or status in context_bar.PR_STATUS_VALUES


def test_the_pr_read_carries_its_review_decision_and_merge_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.dashboard.source_providers import chip_refresh, runner
    from kiro_crew.slack.code_channels.workspace import pr as pr_mod

    argvs: list[tuple[str, ...]] = []
    status: dict[str, str] = {"state": "open", "ci": "passed"}

    async def run_json(*argv: str, host: str = "") -> Any:
        argvs.append(argv)
        row = {"number": 9, "url": "https://github.com/acme/widgets/pull/9", "state": "OPEN"}
        return [{**row, "reviewDecision": "CHANGES_REQUESTED", "mergeable": "CONFLICTING"}]

    async def check_status(url: str) -> Any:
        return dict(status)

    monkeypatch.setattr(runner, "_run_json", run_json)
    monkeypatch.setattr(chip_refresh, "_fetch_check_status", check_status)
    repo_ref = RepoRef("github.com", "acme", "widgets")
    got = asyncio.run(pr_mod.find_pull_request(repo_ref, "slack/x-1")).pr
    assert got is not None and (got.review, got.mergeable) == ("changes_requested", "conflicting")
    assert got.ci_settled
    fields = argvs[0][argvs[0].index("--json") + 1].split(",")
    assert {"reviewDecision", "mergeable"} <= set(fields)
    # The chip read's settled merge state wins over the list read's.
    status["mergeable"] = "mergeable"
    got = asyncio.run(pr_mod.find_pull_request(repo_ref, "slack/x-1")).pr
    assert got is not None and got.mergeable == "mergeable"
    # A checks read that could not answer leaves the CI unsettled.
    status["ciUnavailable"] = "1"
    got = asyncio.run(pr_mod.find_pull_request(repo_ref, "slack/x-1")).pr
    assert got is not None and not got.ci_settled


# ── the workspace's write ─────────────────────────────────────────────────────


def test_the_status_is_sent_once_then_again_only_when_it_changes() -> None:
    client = _Client()
    svc = WorkspaceService(lambda: client, store=None)
    record = _record()
    _publish(svc, record)
    _publish(svc, record)
    assert client.sent == ["draft"] and record.pr_status_hash
    record.pr_title = "Fix the login test"  # not part of the status
    _publish(svc, record)
    record.pr_state, record.pr_ci_read = "open", "running"
    _publish(svc, record)
    record.pr_ci_read = "passed"  # still open: nothing to send
    _publish(svc, record)
    record.pr_review = "changes_requested"
    _publish(svc, record)
    record.pr_review, record.pr_state = "approved", "merged"
    _publish(svc, record)
    record.pr_number, record.pr_url, record.pr_state = 43, _URL[:-2] + "43", "merged"
    _publish(svc, record)  # a new PR on the branch: its status, though the value repeats
    assert client.sent == ["draft", "open", "blocked", "merged", "merged"]


def test_a_status_is_cleared_once_when_the_channel_has_no_pr_any_more() -> None:
    client = _Client()
    svc = WorkspaceService(lambda: client, store=None)
    record = _record()
    _publish(svc, record)
    record.pr_number, record.pr_url, record.pr_state = 0, "", ""  # the branch has no PR now
    _publish(svc, record)
    _publish(svc, record)
    assert client.sent == ["draft", ""]
    # A channel that never had a status sends nothing.
    fresh = _record(channel_id="C0FRESH", pr_number=0, pr_url="", pr_state="")
    _publish(svc, fresh)
    assert client.sent == ["draft", ""]


def test_an_open_pr_whose_checks_no_read_settled_sends_nothing() -> None:
    client = _Client()
    svc = WorkspaceService(lambda: client, store=None)
    record = _record(pr_state="open", pr_ci_read="failed")
    _publish(svc, record)
    record.pr_ci_read = PR_CI_UNREAD  # e.g. a new PR whose checks read failed
    _publish(svc, record)
    assert client.sent == ["blocked"]  # the stored value stays


def test_a_merged_or_closed_pr_counts_whatever_its_ci_read() -> None:
    client = _Client()
    svc = WorkspaceService(lambda: client, store=None)
    record = _record(pr_state="merged", pr_ci_read=PR_CI_UNREAD)
    _publish(svc, record)
    record.pr_state = "closed"
    _publish(svc, record)
    assert client.sent == ["merged", "closed"]


def test_a_write_whose_answer_was_lost_is_sent_again() -> None:
    """Slack may have applied it: the next pass cannot trust its digest, so the
    value it wants (even the one sent before) goes again, and a clear still goes."""

    class Lost(_Client):
        lose = False

        async def set_pr_status(self, channel_id: str, value: str) -> None:
            self.sent.append(value)
            if self.lose:
                raise CodeChannelApiError(
                    "agents.conversations.setProperties", "transport_error", maybe_applied=True
                )

    client = Lost()
    svc = WorkspaceService(lambda: client, store=None)
    record = _record(pr_state="open")
    _publish(svc, record)
    client.lose = True
    record.pr_ci_read = "failed"
    _publish(svc, record)  # "blocked" may have landed
    client.lose = False
    record.pr_ci_read = "passed"
    _publish(svc, record)
    assert client.sent == ["open", "blocked", "open"]
    client.lose = True
    record.pr_state = "merged"
    _publish(svc, record)  # lost again
    client.lose = False
    record.pr_number, record.pr_url, record.pr_state = 0, "", ""
    _publish(svc, record)
    assert client.sent == ["open", "blocked", "open", "merged", ""]


def test_the_status_asks_the_channels_activation_right_before_its_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Set to Off or Review after the value was built: nothing goes out."""
    from kiro_crew.slack.code_channels.workspace import service as service_mod

    admitted = {"now": True}
    client = _Client()

    def make_client() -> _Client:
        admitted["now"] = False  # the operator turns the channel Off meanwhile
        return client

    monkeypatch.setattr(service_mod, "_activation_admits", lambda b: admitted["now"])
    svc = WorkspaceService(make_client, store=None)
    record = _record()
    _publish(svc, record, SimpleNamespace(channel_id=CH))
    assert client.sent == [] and record.pr_status_hash == ""  # sent once it is on again


@pytest.mark.parametrize("code", ["invalid_pr_status", "invalid_arguments", "invalid_arg_name"])
def test_a_refused_value_is_held_for_the_channel(code: str) -> None:
    refused = _Client(fail=code)
    svc = WorkspaceService(lambda: refused, store=None)
    record = _record()
    _publish(svc, record)
    record.pr_state = "open"
    _publish(svc, record)
    # Not asked again in this process, and the channel itself stays on.
    assert refused.sent == ["draft"] and record.pr_status_hash == "" and record.slack_off == ""


@pytest.mark.parametrize(
    "code",
    [
        "ratelimited",
        "internal_error",
        "is_archived",
        "pr_link_failed",
        "stopped_by_person",
        "channel_not_allowed_by_policy",
    ],
)
def test_any_other_failure_is_sent_again(code: str) -> None:
    """Slack's ``pr_link_failed`` saved the value but its routing did not follow:
    repeated. An outage, an archive racing the pass, a Stop or the channel policy
    says nothing about the value: none of them holds the channel."""
    client = _Client(fail=code)
    svc = WorkspaceService(lambda: client, store=None)
    record = _record()
    _publish(svc, record)
    _publish(svc, record)
    assert client.sent == ["draft", "draft"] and record.slack_off == ""
    assert record.pr_status_hash == ""
    client.fail = ""
    _publish(svc, record)
    assert client.sent == ["draft"] * 3 and record.pr_status_hash


def test_the_suite_floor_forgets_held_channels() -> None:
    """A hold one test planted is gone by the next: the shared per-test reset."""
    import conftest_resets

    publishing._pr_status_held.add("T1:C0PLANTED")
    guard = conftest_resets._reset_code_channel_owner_only_notices
    gen = getattr(guard, "__wrapped__", guard)()
    next(gen)
    assert "T1:C0PLANTED" not in publishing._pr_status_held


# ── through a whole refresh ──────────────────────────────────────────────────


class _StatusClient(_ws.FakeClient):
    async def set_pr_status(self, channel_id: str, value: str) -> None:
        return await self._record("set_pr_status", channel_id, value=value)


def test_a_refresh_sets_it_from_the_own_pr_alone_in_its_own_call(
    tmp_path: Path, repo: Path, audits: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.slack.code_channels.board.model import BoardSnapshot, WorkerRow

    h = Harness(tmp_path, PrLookup("found", PullRequest(42, _URL, "open", "passed")))
    h.client = _StatusClient()
    # A helper agent's failing PR on the board: it does not change the status.
    worker = WorkerRow(
        "it_1", "Worker", "working", pr_number=7, pr_url=_URL[:-2] + "7", pr_state="open"
    ).with_pr(7, _URL[:-2] + "7", "open", "failed")

    async def read_board(_binding: Any) -> BoardSnapshot:
        return BoardSnapshot(rows=(worker,))

    async def as_read(_record: Any, board: BoardSnapshot) -> BoardSnapshot:
        return board

    h.svc._read_board = read_board
    monkeypatch.setattr(h.svc, "_fill_worker_prs", as_read)
    monkeypatch.setattr(h.svc, "_board_still_admitted", lambda binding: True)
    binding, _rec = _bound(h, repo)
    h.run(h.svc.refresh(binding))
    assert h.client.of("set_pr_status") == [{"value": "open"}]
    h.pr = PrLookup("found", PullRequest(42, _URL, "open", "failed"))
    h.run(h.svc.refresh(binding))
    assert h.client.of("set_pr_status")[-1] == {"value": "blocked"}
    assert h.store.get("T1", "C1").pr_status_hash  # written back with the refresh
    # Never batched: the bar and the resource carry no status.
    assert "pr_status" not in repr(h.client.of("set_context_bar"))
    for resource in h.client.of("set_agent_resource"):
        assert set(resource) == {"url", "resource_type", "title", "provider"}


def test_a_channel_whose_board_is_refused_follows_its_own_pr(
    tmp_path: Path, repo: Path, audits: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shared, or its conductor's slot gone: no board, and the board's scope
    refuses. The status still follows the channel's own PR, behind the channel's
    activation, and clears when the PR goes."""
    h = Harness(tmp_path, PrLookup("found", PullRequest(42, _URL, "open", "failed")))
    h.client = _StatusClient()

    async def refused(_binding: Any) -> None:
        return None

    h.svc._read_board = refused
    monkeypatch.setattr(h.svc, "_board_still_admitted", lambda binding: False)
    binding, _rec = _bound(h, repo)
    h.run(h.svc.refresh(binding))
    h.pr = PrLookup("found", PullRequest(42, _URL, "merged", "passed"))
    h.run(h.svc.refresh(binding))
    h.pr = PrLookup("none")
    h.run(h.svc.refresh(binding))
    assert h.client.of("set_pr_status") == [
        {"value": "blocked"},
        {"value": "merged"},
        {"value": ""},
    ]


@pytest.mark.parametrize("failure", ["raise", "invalid_pr_status"])
def test_a_failing_status_write_never_breaks_the_refresh(
    tmp_path: Path, repo: Path, audits: Any, failure: str
) -> None:
    h = Harness(tmp_path, PrLookup("found", PullRequest(42, _URL, "draft", "running")))

    class Broken(_StatusClient):
        async def set_pr_status(self, channel_id: str, value: str) -> None:
            await super().set_pr_status(channel_id, value)
            if failure == "raise":
                raise RuntimeError("boom")
            raise CodeChannelApiError("agents.conversations.setProperties", failure)

    h.client = Broken()
    binding, rec = _bound(h, repo)
    (Path(rec.worktree) / "README.md").write_text("changed\n", newline="\n")
    h.run(h.svc.refresh(binding))
    assert h.client.of("set_pr_status") == [{"value": "draft"}]
    # The bar, the resource and the Code tab all still went out.
    assert "pr" in {i["key"] for i in _ws._bar(h.client)}
    assert h.client.of("set_agent_resource") and h.client.of("set_diff_view")
    stored = h.store.get("T1", "C1")
    assert stored.pr_status_hash == "" and stored.slack_off == ""


def test_an_own_pr_whose_ci_read_fails_keeps_its_last_read_ci(
    tmp_path: Path, repo: Path, audits: Any
) -> None:
    h = Harness(tmp_path, PrLookup("found", PullRequest(42, _URL, "open", "", ci_settled=False)))
    h.client = _StatusClient()
    binding, _rec = _bound(h, repo)
    h.run(h.svc.refresh(binding))
    # Open, and no read has settled its CI yet: nothing goes out this pass.
    assert h.client.of("set_pr_status") == []
    h.pr = PrLookup("found", PullRequest(42, _URL, "open", "failed"))
    h.run(h.svc.refresh(binding))
    # The checks read fails (rate limit), then gh is unavailable: still blocked.
    h.pr = PrLookup("found", PullRequest(42, _URL, "open", "", ci_settled=False))
    h.run(h.svc.refresh(binding))
    h.pr = PrLookup("unavailable", reason="gh")
    h.run(h.svc.refresh(binding))
    assert h.client.of("set_pr_status") == [{"value": "blocked"}]
    h.pr = PrLookup("found", PullRequest(42, _URL, "open", "passed"))
    h.run(h.svc.refresh(binding))
    assert h.client.of("set_pr_status")[-1] == {"value": "open"}


def test_review_and_merge_state_reach_the_status_from_the_gh_read(
    tmp_path: Path, repo: Path, audits: Any
) -> None:
    h = Harness(tmp_path)
    h.client = _StatusClient()
    h.pr = PrLookup("found", PullRequest(42, _URL, "open", "passed", review="changes_requested"))
    binding, _rec = _bound(h, repo)
    h.run(h.svc.refresh(binding))
    assert h.client.of("set_pr_status") == [{"value": "blocked"}]  # the review
    # A board-only pass reads the stored record: the review survived the store.
    h.run(h.svc.refresh(binding, recheck_pr=False))
    assert h.client.of("set_pr_status") == [{"value": "blocked"}]
    h.pr = PrLookup(
        "found", PullRequest(42, _URL, "open", "passed", review="approved", mergeable="conflicting")
    )
    h.run(h.svc.refresh(binding))
    stored = h.store.get("T1", "C1")
    assert (stored.pr_review, stored.pr_mergeable) == ("approved", "conflicting")
    h.run(h.svc.refresh(binding, recheck_pr=False))
    assert h.client.of("set_pr_status") == [{"value": "blocked"}]  # now the conflict
    h.pr = PrLookup("found", PullRequest(42, _URL, "open", "passed", mergeable="mergeable"))
    h.run(h.svc.refresh(binding))
    assert h.client.of("set_pr_status")[-1] == {"value": "open"}


# ── the record across a rebind ────────────────────────────────────────────────


def test_a_rebind_to_a_folder_with_no_pr_clears_the_status_it_showed(
    tmp_path: Path, repo: Path, audits: Any
) -> None:
    """The status is the channel's, not the tree's: a new record keeps what was
    sent, so the next pass can clear it."""
    h = Harness(tmp_path, PrLookup("found", PullRequest(42, _URL, "draft", "running")))
    h.client = _StatusClient()
    binding, _rec = _bound(h, repo)
    h.run(h.svc.refresh(binding))
    assert h.client.of("set_pr_status") == [{"value": "draft"}]
    notes = tmp_path / "notes"
    notes.mkdir()
    moved = FakeBinding("C1", project_dir=str(notes))
    h.run(h.svc.ensure(moved))
    assert h.store.get("T1", "C1").pr_status_hash  # carried onto the new record
    h.run(h.svc.refresh(moved))
    assert h.client.of("set_pr_status") == [{"value": "draft"}, {"value": ""}]


def _same_tree(h: Any) -> Any:
    async def bind(_binding: Any) -> WorkspaceRecord:
        fresh = h.store.get("T1", "C1")
        return WorkspaceRecord(
            team_id="T1",
            channel_id="C1",
            worktree=fresh.worktree,
            branch=fresh.branch,
            repo_root=fresh.repo_root,
            project_dir=fresh.project_dir,
            session_key=fresh.session_key,
        )

    return bind


def test_a_re_adopted_tree_keeps_the_status_and_sends_no_spurious_clear(
    tmp_path: Path, repo: Path, audits: Any
) -> None:
    h = Harness(tmp_path, PrLookup("found", PullRequest(42, _URL, "draft", "running")))
    h.client = _StatusClient()
    binding, _rec = _bound(h, repo)
    h.run(h.svc.refresh(binding))
    sent = h.store.get("T1", "C1").pr_status_hash
    h.svc._bind = _same_tree(h)
    h.run(h.svc._bind_and_store(binding, "T1:C1"))
    assert h.store.get("T1", "C1").pr_status_hash == sent  # carried onto the new record
    # A board-only pass right after: no "" for a PR that still applies.
    h.run(h.svc.refresh(binding, recheck_pr=False))
    assert h.client.of("set_pr_status") == [{"value": "draft"}]


def test_a_re_adopt_while_github_is_unavailable_keeps_the_status(
    tmp_path: Path, repo: Path, audits: Any
) -> None:
    """The re-adopted record keeps its PR until a read answers: an unavailable
    read is no evidence the PR went, so nothing is cleared."""
    h = Harness(tmp_path, PrLookup("found", PullRequest(42, _URL, "open", "failed")))
    h.client = _StatusClient()
    binding, _rec = _bound(h, repo)
    h.run(h.svc.refresh(binding))
    assert h.client.of("set_pr_status") == [{"value": "blocked"}]
    h.svc._bind = _same_tree(h)
    h.run(h.svc._bind_and_store(binding, "T1:C1"))
    assert h.store.get("T1", "C1").pr_number == 42
    h.pr = PrLookup("unavailable", reason="gh")
    h.run(h.svc.refresh(binding))
    assert h.client.of("set_pr_status") == [{"value": "blocked"}]


def test_a_rebind_racing_the_pass_keeps_the_status_the_pass_sent(
    tmp_path: Path, repo: Path, audits: Any
) -> None:
    h = Harness(tmp_path)
    h.client = _StatusClient()
    binding, rec = _bound(h, repo)

    async def find_pr(repo_ref: RepoRef, branch: str) -> PrLookup:
        # A rebind lands while the pass runs: the stored record moves to another branch.
        moved = h.store.get("T1", "C1")
        moved.branch = "other-branch"
        h.store.put(moved)
        return PrLookup("found", PullRequest(42, _URL, "draft", "running"))

    h.svc._find_pr = find_pr
    h.run(h.svc.refresh(binding))
    assert h.client.of("set_pr_status") == [{"value": "draft"}]
    assert h.store.get("T1", "C1").pr_status_hash  # what Slack now shows


def test_an_unsettled_merge_state_keeps_the_last_settled_one(
    tmp_path: Path, repo: Path, audits: Any
) -> None:
    """GitHub answers ``UNKNOWN`` while it recomputes mergeability: a known
    conflict still blocks, until a settled read says otherwise."""
    h = Harness(
        tmp_path,
        PrLookup("found", PullRequest(42, _URL, "open", "passed", mergeable="conflicting")),
    )
    h.client = _StatusClient()
    binding, _rec = _bound(h, repo)
    h.run(h.svc.refresh(binding))
    h.pr = PrLookup("found", PullRequest(42, _URL, "open", "passed"))  # mergeable unknown
    h.run(h.svc.refresh(binding))
    assert h.client.of("set_pr_status") == [{"value": "blocked"}]
    h.pr = PrLookup("found", PullRequest(42, _URL, "open", "passed", mergeable="mergeable"))
    h.run(h.svc.refresh(binding))
    assert h.client.of("set_pr_status")[-1] == {"value": "open"}
