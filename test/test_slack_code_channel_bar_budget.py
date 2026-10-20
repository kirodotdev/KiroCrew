"""The code channel's context bar, budgeted to Slack's five items.

Slack keeps the FIRST five items of a longer ``context_bar_items`` list and only
warns (``too_many_context_bar_items``,
https://docs.slack.dev/reference/methods/agents.conversations.setProperties), so
the bar is budgeted before it is sent: a Workers item for the board, repo,
branch, Stop while the channel's session runs, and ONE pull-request slot.

Pure parts are tested directly; the service runs real git against throwaway
repositories with the real board publisher over a recorded fake Slack client.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.dashboard.handlers import worktree as wt
from kiro_crew.slack import features
from kiro_crew.slack.code_channels import agent_bar
from kiro_crew.slack.code_channels import binding as cc_binding
from kiro_crew.slack.code_channels import board as board_pkg
from kiro_crew.slack.code_channels import surface, surface_store
from kiro_crew.slack.code_channels.board import model, publisher, source
from kiro_crew.slack.code_channels.workspace import context_bar, git_ops, service
from kiro_crew.slack.code_channels.workspace.context_bar import (
    ACTION_CREATE_PR,
    ACTION_STOP,
    BarState,
    build_items,
    conforms,
    prs_label,
    with_dashboard,
)
from kiro_crew.slack.code_channels.workspace.pr import PrLookup, PullRequest
from kiro_crew.slack.code_channels.workspace.remote import RepoRef
from kiro_crew.slack.code_channels.workspace.store import WorkspaceRecord, WorkspaceStore

#: The icon values Slack documents for a context-bar item, copied from
#: https://docs.slack.dev/ai/slack-code/code-channel-properties#context-bar-schema
DOCUMENTED_ICONS = frozenset(
    {
        "branch",
        "folder",
        "hierarchy",
        "life-ring",
        "link",
        "globe",
        "terminal",
        "code",
        "search",
        "lock",
    }
)
BOARD_URL = "https://acme.slack.com/archives/C1/p1000000000000001"
DASH = "https://dash.example/chat/chat-cond"


def _row(item_id: str, state: str = model.WORKING, **kw: Any) -> model.WorkerRow:
    return model.WorkerRow(
        item_id=item_id, title=kw.pop("title", f"Task {item_id}"), state=state, **kw
    )


def _pr_row(n: int, state: str = model.WORKING, pr_state: str = "open") -> model.WorkerRow:
    return _row(
        f"it_0000000{n}",
        state,
        branch=f"feat/w{n}",
        pr_number=100 + n,
        pr_url=f"https://github.com/acme/widgets/pull/{100 + n}",
        pr_state=pr_state,
        pr_ci="running",
    )


def _three_workers() -> model.BoardSnapshot:
    return model.BoardSnapshot(rows=(_pr_row(1), _pr_row(2), _pr_row(3, model.WAITING_ON_YOU)))


def _state(**kw: Any) -> BarState:
    base: dict[str, Any] = dict(
        repo=RepoRef("github.com", "acme", "widgets"),
        branch="kc/fix-login",
        branch_pushed=True,
        dashboard_url=DASH,
    )
    base.update(kw)
    return BarState(**base)


def _keys(items: list[dict[str, str]]) -> list[str]:
    return [i["key"] for i in items]


# ── Slack's documented cap ─────────────────────────────────────────────────


def _slack_keeps(items: list[dict[str, str]]) -> tuple[list[dict[str, str]], list[str]]:
    """What setProperties does with the list, per its usage notes: >5 succeeds,
    warns ``too_many_context_bar_items`` and keeps only the first five."""
    warnings = ["too_many_context_bar_items"] if len(items) > 5 else []
    return list(items[:5]), warnings


def test_slack_silently_drops_the_sixth_item_so_the_bar_is_budgeted_here() -> None:
    # Build 6's shape: repo, branch, one item per worker PR, then the actions.
    naive = [
        {"key": "repo", "label": "acme/widgets", "icon": "folder"},
        {"key": "branch", "label": "kc/fix-login", "icon": "branch"},
        *(
            {"key": f"worker-{n}", "label": f"PR #{n}", "icon": "hierarchy"}
            for n in (101, 102, 103)
        ),
        {"key": ACTION_STOP, "label": "Stop", "icon": "terminal", "item_type": "action"},
        {"key": "dashboard", "label": "Open in dashboard", "icon": "link", "url": DASH},
    ]
    kept, warnings = _slack_keeps(naive)
    assert warnings == ["too_many_context_bar_items"]
    assert ACTION_STOP not in _keys(kept)  # the call "succeeds" and Stop is gone

    # Every slot filled and more besides: the builder still sends five, Stop kept.
    full = _state(
        board=_three_workers(),
        board_url=BOARD_URL,
        stop=True,
        can_open_pr=True,
        branch_has_commits=True,
    )
    agent = [{"key": "notes", "label": "Notes", "icon": "link", "url": "https://n.example"}]
    items = with_dashboard(
        agent_bar.merge(build_items(replace(full, dashboard_url="")), agent)[0], DASH
    )
    assert len(items) == 5
    assert _slack_keeps(items) == (items, [])
    assert ACTION_STOP in _keys(items)


def test_the_client_refuses_six_items_rather_than_let_slack_drop_one() -> None:
    from kiro_crew.slack.code_channels.client import CodeChannelsClient
    from kiro_crew.slack.code_channels.models import CodeChannelRequestError

    sent: list[str] = []

    async def api_call(method: str, **kw: Any) -> Any:
        sent.append(method)
        return {"ok": True}

    six = [{"key": f"k{n}", "label": "X", "icon": "lock"} for n in range(6)]
    with pytest.raises(CodeChannelRequestError):
        asyncio.run(CodeChannelsClient(api_call).set_context_bar("C1", six))
    assert sent == []


# ── priority order ─────────────────────────────────────────────────────────


def test_priority_is_workers_repo_branch_stop_then_one_pr_slot() -> None:
    full = _state(board=_three_workers(), board_url=BOARD_URL, stop=True)
    assert _keys(build_items(full)) == ["workers", "repo", "branch", ACTION_STOP, "prs"]
    # The dashboard link only ever takes a slot nothing above wanted.
    assert _keys(build_items(replace(full, stop=False))) == [
        "workers",
        "repo",
        "branch",
        "prs",
        "dashboard",
    ]
    assert _keys(build_items(replace(full, board=None))) == [
        "repo",
        "branch",
        ACTION_STOP,
        "dashboard",
    ]


def test_the_pr_slot_prefers_the_channels_own_pr_then_one_worker_pr_then_a_count() -> None:
    own = PullRequest(42, "https://github.com/acme/widgets/pull/42", "open", "passed")
    board = _three_workers()
    slot = build_items(_state(pr=own, board=board))[3]
    assert slot["key"] == "pr" and slot["url"].endswith("/pull/42")

    one = model.BoardSnapshot(rows=(_pr_row(1), _row("it_00000009")))
    slot = build_items(_state(board=one))[3]
    assert slot == {
        "key": "pr",
        "label": "PR #101 is open · tests running",
        "icon": "hierarchy",
        "url": "https://github.com/acme/widgets/pull/101",
    }

    slot = build_items(_state(board=board, board_url=BOARD_URL))[3]
    assert slot == {"key": "prs", "label": "3 PRs open", "icon": "hierarchy", "url": BOARD_URL}
    # No per-worker item reaches the bar, ever.
    assert not [k for k in _keys(build_items(_state(board=board))) if k.startswith("worker-")]


def test_create_pr_needs_no_pr_at_all_and_commits_on_the_branch() -> None:
    able = dict(can_open_pr=True, branch_has_commits=True)
    assert ACTION_CREATE_PR in _keys(build_items(_state(**able)))
    assert ACTION_CREATE_PR not in _keys(build_items(_state(can_open_pr=True)))
    assert ACTION_CREATE_PR not in _keys(build_items(_state(branch_has_commits=True)))
    # A worker's PR is a PR: Create PR does not compete with it for the slot.
    assert ACTION_CREATE_PR not in _keys(build_items(_state(board=_three_workers(), **able)))
    assert _keys(build_items(_state(stop=True, **able))) == [
        "repo",
        "branch",
        ACTION_STOP,
        ACTION_CREATE_PR,
        "dashboard",
    ]


@pytest.mark.parametrize(
    ("states", "label"),
    [
        (["open", "open", "open"], "3 PRs open"),
        (["open", "draft", "merged"], "3 PRs: 2 open · 1 merged"),
        (["merged", "merged"], "2 PRs merged"),
        (["open", "closed", ""], "3 PRs: 1 open · 1 closed"),
    ],
)
def test_several_worker_prs_read_as_one_count(states: list[str], label: str) -> None:
    prs = [PullRequest(n, "", s) for n, s in enumerate(states, 1)]
    assert prs_label(prs) == label


# ── the Workers label: the board's own counts ──────────────────────────────


@pytest.mark.parametrize(
    ("rows", "label"),
    [
        ([model.WORKING, model.WORKING, model.WAITING_ON_YOU], "2 working · 1 needs you"),
        ([model.DONE, model.DONE, model.DONE], "3 of 3 done"),
        ([model.WAITING_ON_YOU, model.WAITING_ON_YOU], "2 need you"),
        ([model.WORKING, model.DONE], "1 working · 1 of 2 done"),
        ([model.BLOCKED, model.LOST, model.IDLE], "1 blocked · 1 lost · 1 idle"),
        ([model.DONE, model.DONE, model.CLOSED], "2 of 3 done · 1 closed"),
        ([model.CLOSED], "0 of 1 done · 1 closed"),
    ],
)
def test_the_workers_label_is_the_boards_tally(rows: list[str], label: str) -> None:
    snap = model.BoardSnapshot(rows=tuple(_row(f"it_{n:08x}", s) for n, s in enumerate(rows)))
    assert model.bar_label(snap) == label
    item = build_items(BarState(board=snap, board_url=BOARD_URL))[0]
    assert item == {"key": "workers", "label": label, "icon": "hierarchy", "url": BOARD_URL}


def test_the_workers_label_ignores_what_a_worker_last_said() -> None:
    a = model.BoardSnapshot(rows=(_row("it_1", summary="reading the code"),))
    b = model.BoardSnapshot(rows=(_row("it_1", summary="writing the test"),))
    assert build_items(BarState(board=a)) == build_items(BarState(board=b))


def test_the_workers_item_links_only_once_the_board_permalink_is_known() -> None:
    item = build_items(BarState(board=_three_workers()))[0]
    assert item["key"] == "workers" and "url" not in item


# ── documented limits: labels, icons ───────────────────────────────────────


def test_every_icon_the_bar_can_use_is_one_slack_documents() -> None:
    assert context_bar.ICONS == DOCUMENTED_ICONS


def _every_bar() -> list[list[dict[str, str]]]:
    long = "x" * 400
    rows = tuple(_row(f"it_{n:08x}", s, title=long) for n, s in enumerate(model.STATES * 30))
    huge = model.BoardSnapshot(rows=rows + tuple(_pr_row(n) for n in range(1, 10)))
    states = [
        _state(board=huge, board_url=BOARD_URL, stop=True),
        _state(board=huge, branch=long, stop=True, board_url="https://x/" + "y" * 3000),
        _state(
            repo=RepoRef("github.com", "o" * 200, "r" * 200),
            branch=long,
            can_open_pr=True,
            branch_has_commits=True,
        ),
        _state(pr=PullRequest(1, "https://github.com/a/b/pull/1", "draft", "failed"), stop=True),
        BarState(folder="/x/" + long, stop=True, dashboard_url=DASH),
        BarState(repo_name=long, branch=long),
    ]
    return [build_items(s) for s in states]


def test_every_bar_is_within_the_documented_limits() -> None:
    for items in _every_bar():
        assert len(items) <= 5, items
        assert len({i["key"] for i in items}) == len(items)
        for item in items:
            assert item["icon"] in DOCUMENTED_ICONS
            assert 0 < len(item["label"]) <= 128 and 0 < len(item["key"]) <= 64
            assert len(item.get("url", "")) <= 2048
            assert conforms(item), item


# ── git: does the branch have commits ──────────────────────────────────────


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


def _passthrough_spawn(argv: Any, mode: str = "standard", **kw: Any) -> Any:
    return list(argv), dict(os.environ), None


@pytest.fixture(autouse=True)
def _no_sandbox(_floor_monkeypatch: Any) -> None:
    _floor_monkeypatch.setattr(wt, "sandboxed_spawn_argv", _passthrough_spawn)
    _floor_monkeypatch.setattr(git_ops, "sandboxed_spawn_argv", _passthrough_spawn)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    _git("init", "-q", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    root = tmp_path / "proj"
    root.mkdir()
    _git("init", "-q", "-b", "main", cwd=root)
    for key, value in (("user.email", "t@example.com"), ("user.name", "T"), ("gc.auto", "0")):
        _git("config", key, value, cwd=root)
    (root / "README.md").write_text("hello\n")
    _git("add", ".", cwd=root)
    _git("commit", "-q", "-m", "init", cwd=root)
    _git("remote", "add", "origin", "https://github.com/acme/widgets.git", cwd=root)
    _git("push", "-q", str(remote), "main:main", cwd=root)
    _git("fetch", "-q", str(remote), "+refs/heads/*:refs/remotes/origin/*", cwd=root)
    _git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main", cwd=root)
    return root


def _commit(tree: Path, name: str) -> None:
    (tree / name).write_text("x\n")
    _git("add", ".", cwd=tree)
    _git("commit", "-q", "-m", name, cwd=tree)


def test_has_commits_since_reads_only_commits_past_the_base(repo: Path) -> None:
    base = _git("rev-parse", "HEAD", cwd=repo)
    assert not git_ops.has_commits_since(str(repo), base)
    assert not git_ops.has_commits_since(str(repo), "")
    _commit(repo, "a.txt")
    assert git_ops.has_commits_since(str(repo), base)


def _rec(**kw: Any) -> WorkspaceRecord:
    return WorkspaceRecord(team_id="T1", channel_id="C1", **kw)


def test_create_pr_counts_commits_from_the_code_tabs_own_base(repo: Path) -> None:
    cut = _git("rev-parse", "HEAD", cwd=repo)
    _git("checkout", "-q", "-b", "kc/chan", cwd=repo)
    record = _rec(base_sha=cut, base_ref="refs/remotes/origin/main")
    assert not service._branch_has_commits(str(repo), record)
    # main moves on and the channel branch is rebased onto it with nothing of
    # its own: still no commits (the base is the fork point, not the cut sha).
    _git("checkout", "-q", "main", cwd=repo)
    _commit(repo, "upstream.txt")
    _git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=repo)
    _git("checkout", "-q", "kc/chan", cwd=repo)
    _git("rebase", "-q", "main", cwd=repo)
    assert not service._branch_has_commits(str(repo), record)
    _commit(repo, "mine.txt")
    assert service._branch_has_commits(str(repo), record)
    # No base_ref recorded (cut from a local HEAD): the cut commit is the base.
    assert service._branch_has_commits(str(repo), _rec(base_sha=cut))
    assert not service._branch_has_commits(
        str(repo), _rec(base_sha=_git("rev-parse", "HEAD", cwd=repo))
    )


# ── the board's permalink, cached per board message ───────────────────────


class _SlackError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"error": code}


class FakeSlack:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.fail: dict[str, str] = {}
        self._n = 0

    async def post_blocks(self, channel: str, blocks: Any, text: str, thread_ts: Any = None) -> str:
        self._n += 1
        self.calls.append(("post_blocks", channel))
        return f"100.{self._n}"

    async def update_message(
        self, channel: str, ts: str, text: str = "", blocks: Any = None
    ) -> None:
        self.calls.append(("update_message", channel, ts))
        code = self.fail.pop("update_message", "")
        if code:
            raise _SlackError(code)

    async def add_pin(self, channel: str, ts: str) -> None:
        self.calls.append(("add_pin", channel, ts))

    async def post_message(self, channel: str, text: str, thread_ts: Any = None) -> str:
        self.calls.append(("post_message", channel))
        return "200.1"

    async def get_permalink(self, channel: str, ts: str) -> str:
        self.calls.append(("get_permalink", channel, ts))
        code = self.fail.pop("get_permalink", "")
        if code:
            raise _SlackError(code)
        return f"https://acme.slack.com/archives/{channel}/p{ts.replace('.', '')}"

    def of(self, method: str) -> list[tuple[Any, ...]]:
        return [c for c in self.calls if c[0] == method]


@pytest.fixture(autouse=True)
def _feature_on(_floor_monkeypatch: Any, tmp_path: Path) -> Any:
    features.reset_capabilities()
    features.record_granted_scopes(["chat:write", "code_channels:manage"])
    _floor_monkeypatch.setattr(
        cc_binding,
        "live_config",
        lambda orch=None: SimpleNamespace(slack=SimpleNamespace(code_channels=True)),
    )
    surface_store.set_store(surface_store.SurfaceStore(tmp_path / "surface.json"))
    yield
    surface_store.set_store(None)
    features.reset_capabilities()
    source.set_scope_predicate(None)
    source.set_records_reader(None)
    board_pkg.reset_for_tests()


@pytest.fixture(autouse=True)
def _publisher_clock(monkeypatch: Any) -> None:
    """Publishes 30s apart: past the board's edit gap, inside its stale-stamp window."""
    now = [0.0]

    def clock() -> float:
        now[0] += 30.0
        return now[0]

    monkeypatch.setattr(publisher, "_clock", clock)


@pytest.fixture
def slack(monkeypatch: Any) -> FakeSlack:
    fake = FakeSlack()
    monkeypatch.setattr(surface, "slack", lambda: fake)

    async def _true() -> bool:
        return True

    async def _not_archived(_binding: Any) -> bool:
        return False

    monkeypatch.setattr(surface, "outbound_permitted", _true)
    monkeypatch.setattr(surface, "is_archived", _not_archived)
    monkeypatch.setattr(surface, "channel_stopped", lambda channel: False)
    monkeypatch.setattr(surface, "audit", lambda *a, **k: None)
    monkeypatch.setattr(surface, "client", lambda: None)  # no live summary writes here
    return fake


@dataclass
class _FakeBinding:
    channel_id: str = "C1"
    team_id: str = "T1"
    session_key: str = "chat-cond"
    project_dir: str | None = None
    title: str = "Fix the widget"


def _publish(snapshot: model.BoardSnapshot) -> str:
    return asyncio.run(publisher.publish(_FakeBinding(), snapshot, link_for=lambda k: ""))


def test_the_board_permalink_is_read_once_per_board_message(slack: FakeSlack) -> None:
    _publish(_three_workers())
    _publish(model.BoardSnapshot(goal="moved", rows=_three_workers().rows))  # an edit
    assert slack.of("get_permalink") == [("get_permalink", "C1", "100.1")]
    record = surface_store.store().get("C1")
    assert record.board_link == "https://acme.slack.com/archives/C1/p1001"
    assert record.board_link_ts == "100.1"
    slack.fail["update_message"] = "message_not_found"
    _publish(model.BoardSnapshot(goal="again", rows=_three_workers().rows))  # reposted
    assert slack.of("get_permalink")[-1] == ("get_permalink", "C1", "100.2")


def test_a_failed_permalink_read_is_retried_on_the_next_publish(slack: FakeSlack) -> None:
    slack.fail["get_permalink"] = "ratelimited"
    _publish(_three_workers())
    assert surface_store.store().get("C1").board_link_ts == ""
    _publish(model.BoardSnapshot(goal="moved", rows=_three_workers().rows))
    assert surface_store.store().get("C1").board_link.startswith("https://")


# ── the service: one bar per real change ───────────────────────────────────


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def set_context_bar(self, channel_id: str, items: Any) -> None:
        self.calls.append(("set_context_bar", list(items)))

    async def set_diff_view(self, channel_id: str, content: str, **kw: Any) -> str:
        return "V1"

    async def remove_view(self, channel_id: str, view_id: str) -> None:
        pass

    def bars(self) -> list[list[dict[str, str]]]:
        return [items for m, items in self.calls if m == "set_context_bar"]


class _Harness:
    def __init__(
        self, tmp_path: Path, snapshot: model.BoardSnapshot | None, prs: dict | None = None
    ):
        self.client = _FakeClient()
        self.snapshot = snapshot
        prs = prs or {}

        async def set_project(key: str, path: str) -> bool:
            return True

        async def find_pr(repo: Any, branch: str) -> PrLookup:
            return prs.get(branch, PrLookup("none"))

        async def find_pr_number(repo: Any, number: int) -> PrLookup:
            return PrLookup("unavailable")

        async def read_board(binding: Any) -> model.BoardSnapshot | None:
            return self.snapshot

        async def publish_board(binding: Any, snap: model.BoardSnapshot) -> Any:
            return await publisher.publish(binding, snap, link_for=lambda k: "")

        async def can_open_pr(record: Any) -> bool:
            return True

        self.svc = service.WorkspaceService(
            lambda: self.client,
            store=WorkspaceStore(tmp_path / "ws.json"),
            set_project=set_project,
            find_pr=find_pr,
            find_pr_number=find_pr_number,
            read_board=read_board,
            publish_board=publish_board,
            extras=service.BarExtras(can_open_pr=can_open_pr, dashboard_link=lambda key: DASH),
        )


def _worker_prs() -> dict[str, PrLookup]:
    return {
        f"feat/w{n}": PrLookup(
            "found",
            PullRequest(
                100 + n, f"https://github.com/acme/widgets/pull/{100 + n}", "open", "running"
            ),
        )
        for n in (1, 2, 3)
    }


def _board(*states: str, summary: str = "") -> model.BoardSnapshot:
    return model.BoardSnapshot(
        goal="Ship it",
        rows=tuple(
            _row(f"it_0000000{n}", s, branch=f"feat/w{n}", summary=summary)
            for n, s in enumerate(states, 1)
        ),
    )


def test_three_workers_fit_one_bar_linked_to_the_pinned_board(
    tmp_path: Path, repo: Path, slack: FakeSlack
) -> None:
    h = _Harness(
        tmp_path, _board(model.WORKING, model.WORKING, model.WAITING_ON_YOU), _worker_prs()
    )
    binding = _FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    asyncio.run(h.svc.refresh(binding))
    bar = h.client.bars()[-1]
    link = "https://acme.slack.com/archives/C1/p1001"
    assert bar == [
        {"key": "workers", "label": "2 working · 1 needs you", "icon": "hierarchy", "url": link},
        {
            "key": "repo",
            "label": "acme/widgets",
            "icon": "folder",
            "url": "https://github.com/acme/widgets",
        },
        {"key": "branch", "label": rec.branch, "icon": "branch"},
        {"key": "prs", "label": "3 PRs open", "icon": "hierarchy", "url": link},
        {"key": "dashboard", "label": "Open in dashboard", "icon": "link", "url": DASH},
    ]


def test_the_bar_is_resent_only_when_its_items_change(
    tmp_path: Path, repo: Path, slack: FakeSlack
) -> None:
    h = _Harness(tmp_path, _board(model.WORKING, model.WORKING), _worker_prs())
    binding = _FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    asyncio.run(h.svc.refresh(binding))
    sent = len(h.client.bars())
    # A worker's new report line moves the board, not the bar.
    h.snapshot = _board(model.WORKING, model.WORKING, summary="now writing the test")
    asyncio.run(h.svc.refresh(binding, recheck_pr=False))
    asyncio.run(h.svc.refresh(binding, recheck_pr=False))
    assert slack.of("update_message")  # the board was edited
    assert len(h.client.bars()) == sent
    # A tally change is a new label: exactly one more setProperties.
    h.snapshot = _board(model.WORKING, model.DONE)
    asyncio.run(h.svc.refresh(binding, recheck_pr=False))
    asyncio.run(h.svc.refresh(binding, recheck_pr=False))
    assert len(h.client.bars()) == sent + 1
    assert h.client.bars()[-1][0]["label"] == "1 working · 1 of 2 done"


def test_stop_shows_for_the_turn_without_a_git_read(
    tmp_path: Path, repo: Path, slack: FakeSlack, monkeypatch: Any
) -> None:
    h = _Harness(tmp_path, None)
    binding = _FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    asyncio.run(h.svc.refresh(binding))
    assert ACTION_STOP not in _keys(h.client.bars()[-1])
    sent = len(h.client.bars())

    def no_git(*a: Any, **k: Any) -> Any:
        raise AssertionError("a turn start reads no git")

    with monkeypatch.context() as m:
        m.setattr(git_ops, "run_git", no_git)
        asyncio.run(h.svc.on_turn_start(binding))
        asyncio.run(h.svc.on_turn_start(binding))  # the same bar: nothing resent
    assert len(h.client.bars()) == sent + 1
    assert ACTION_STOP in _keys(h.client.bars()[-1])

    async def end() -> None:
        await h.svc.on_turn_end(binding)
        await h.svc.drain()

    asyncio.run(end())
    assert ACTION_STOP not in _keys(h.client.bars()[-1])


def test_create_pr_appears_once_the_channel_branch_has_a_commit(
    tmp_path: Path, repo: Path, slack: FakeSlack
) -> None:
    h = _Harness(tmp_path, None)
    binding = _FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    asyncio.run(h.svc.refresh(binding))
    assert ACTION_CREATE_PR not in _keys(h.client.bars()[-1])
    _commit(Path(rec.worktree), "work.txt")
    asyncio.run(h.svc.refresh(binding))
    assert _keys(h.client.bars()[-1])[2] == ACTION_CREATE_PR


def test_the_workers_item_link_follows_a_reposted_board() -> None:
    old = "https://acme.slack.com/archives/C1/p1001"
    surface_store.store().update("C1", board_ts="100.1", board_link=old, board_link_ts="100.1")
    assert asyncio.run(service._board_link("C1")) == old
    # The board was reposted and its new link not read yet: the old one is not shown.
    surface_store.store().update("C1", board_ts="100.2")
    assert asyncio.run(service._board_link("C1")) == ""
    assert asyncio.run(service._board_link("C9")) == ""


# ── review fixes ───────────────────────────────────────────────────────────


def test_the_pr_count_covers_only_the_prs_the_board_lists() -> None:
    settled = tuple(_row(f"it_{n:08x}", model.DONE) for n in range(model.MAX_SHOWN))
    hidden = _row("it_ffffffff", model.DONE, pr_number=9, pr_state="merged")
    board = model.BoardSnapshot(rows=(hidden,) + settled)  # oldest settled: not shown
    assert hidden not in board.shown
    keys = _keys(build_items(_state(board=board)))
    assert "pr" not in keys and "prs" not in keys


def test_a_landed_own_pr_yields_the_slot_to_open_worker_prs() -> None:
    merged = PullRequest(42, "https://github.com/acme/widgets/pull/42", "merged", "")
    slot = build_items(_state(pr=merged, board=_three_workers(), board_url=BOARD_URL))[3]
    assert slot["key"] == "prs" and slot["label"] == "3 PRs open"
    done = model.BoardSnapshot(rows=(_pr_row(1, model.DONE, "merged"),))
    slot = build_items(_state(pr=merged, board=done))[3]
    assert slot["key"] == "pr" and slot["url"].endswith("/pull/42")


def test_an_unusable_permalink_is_not_cached(slack: FakeSlack, monkeypatch: Any) -> None:
    async def nothing(channel: str, ts: str) -> str:
        slack.calls.append(("get_permalink", channel, ts))
        return "http://not-https.example/x"

    monkeypatch.setattr(slack, "get_permalink", nothing)
    _publish(_three_workers())
    _publish(model.BoardSnapshot(goal="moved", rows=_three_workers().rows))
    assert len(slack.of("get_permalink")) == 2  # asked again, nothing stored
    assert surface_store.store().get("C1").board_link_ts == ""


def test_stop_survives_a_turn_starting_while_the_last_turns_refresh_runs(
    tmp_path: Path, repo: Path, slack: FakeSlack
) -> None:
    h = _Harness(tmp_path, None)
    binding = _FakeBinding(project_dir=str(repo))

    async def go() -> None:
        await h.svc.on_turn_start(binding)  # turn N: Stop shown
        assert ACTION_STOP in _keys(h.client.bars()[-1])
        paused, release = asyncio.Event(), asyncio.Event()
        real = h.svc._publish_diff

        async def slow_diff(record: Any) -> None:
            paused.set()
            await release.wait()
            await real(record)

        h.svc._publish_diff = slow_diff  # type: ignore[method-assign]
        await h.svc.on_turn_end(binding)  # its refresh drops Stop, then stalls
        await paused.wait()
        assert ACTION_STOP not in _keys(h.client.bars()[-1])
        await h.svc.on_turn_start(binding)  # turn N+1 starts mid-refresh
        release.set()
        await h.svc.drain()

    asyncio.run(go())
    assert ACTION_STOP in _keys(h.client.bars()[-1])
    stored = h.svc.store.get("T1", "C1")
    assert stored.bar_hash == h.svc._sent_bar["T1:C1"]  # the store tracks what Slack shows


def test_a_refresh_that_read_its_state_before_a_turn_started_sends_stop(
    tmp_path: Path, repo: Path, slack: FakeSlack
) -> None:
    h = _Harness(tmp_path, None)
    binding = _FakeBinding(project_dir=str(repo))

    async def go() -> None:
        await h.svc.ensure(binding)
        await h.svc.refresh(binding)
        paused, release = asyncio.Event(), asyncio.Event()
        real = h.svc._bar_state

        async def read_then_wait(record: Any) -> BarState:
            state = await real(record)  # read while nothing runs: no Stop
            paused.set()
            await release.wait()
            return state

        h.svc._bar_state = read_then_wait  # type: ignore[method-assign]
        task = asyncio.ensure_future(h.svc.refresh(binding))
        await paused.wait()
        await h.svc.on_turn_start(binding)
        release.set()
        await task

    asyncio.run(go())
    assert ACTION_STOP in _keys(h.client.bars()[-1])


def test_a_failing_board_never_costs_the_bar(tmp_path: Path, repo: Path, slack: FakeSlack) -> None:
    h = _Harness(tmp_path, _board(model.WORKING))

    async def broken(binding: Any, snap: Any) -> None:
        raise RuntimeError("store unreadable")

    h.svc._publish_board = broken
    binding = _FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    sent = len(h.client.bars())
    asyncio.run(h.svc.refresh(binding))
    assert len(h.client.bars()) == sent + 1
    assert h.client.bars()[-1][0]["label"] == "1 working"


def test_the_first_turn_after_a_restart_shows_stop(
    tmp_path: Path, repo: Path, slack: FakeSlack
) -> None:
    h = _Harness(tmp_path, None)
    binding = _FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    asyncio.run(h.svc.refresh(binding))
    restarted = _Harness(tmp_path, None)  # same store on disk, nothing in memory
    asyncio.run(restarted.svc.on_turn_start(binding))
    assert ACTION_STOP in _keys(restarted.client.bars()[-1])


def test_a_turn_start_links_the_board_message_that_is_current_now(
    tmp_path: Path, repo: Path, slack: FakeSlack
) -> None:
    h = _Harness(tmp_path, _board(model.WORKING))
    binding = _FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    asyncio.run(h.svc.refresh(binding))
    assert h.client.bars()[-1][0]["url"].startswith("https://")
    surface_store.store().update("C1", board_ts="100.9")  # reposted, link not read yet
    asyncio.run(h.svc.on_turn_start(binding))
    assert "url" not in h.client.bars()[-1][0]


def test_a_turn_start_keeps_a_channel_disabling_refusal(
    tmp_path: Path, repo: Path, slack: FakeSlack
) -> None:
    from kiro_crew.slack.code_channels.models import CodeChannelApiError

    h = _Harness(tmp_path, None)
    binding = _FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    asyncio.run(h.svc.refresh(binding))

    async def refuse(channel_id: str, items: Any) -> None:
        raise CodeChannelApiError("agents.conversations.setProperties", "not_agent_channel")

    h.client.set_context_bar = refuse  # type: ignore[method-assign]
    asyncio.run(h.svc.on_turn_start(binding))
    assert h.svc.store.get("T1", "C1").slack_off == "not_agent_channel"
