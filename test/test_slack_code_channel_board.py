"""The conductor's work board in a code channel.

Pure parts (state mapping, aggregate line, rendering, grouped diff assembly) are
tested directly. The publisher runs against a recorded fake Slack client and a
throwaway surface store; the combined diff runs real git against throwaway
repositories through the workspace service, the way the workspace's own tests do.
The ledger reader is faked at its one seam (``source.set_records_reader``) and the
dispatch exemption's scope check at its one call site (``source.set_scope_predicate``).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.dashboard.handlers import worktree as wt
from kiro_crew.slack import features
from kiro_crew.slack.code_channels import binding as cc_binding
from kiro_crew.slack.code_channels import board as board_pkg
from kiro_crew.slack.code_channels import surface, surface_store, wrapup
from kiro_crew.slack.code_channels.board import filing, model, publisher, render, source
from kiro_crew.slack.code_channels.store import Binding
from kiro_crew.slack.code_channels.workspace import git_ops, service
from kiro_crew.slack.code_channels.workspace.diff import (
    CLEAN,
    DiffGroup,
    assemble_groups,
    screen_tracked,
)
from kiro_crew.slack.code_channels.workspace.pr import PrLookup, PullRequest
from kiro_crew.slack.code_channels.workspace.store import WorkspaceStore

# ── shared fixtures ─────────────────────────────────────────────────────────

#: A fixed "now" for renders that show elapsed times.
_NOW = datetime(2026, 10, 20, 13, 30, tzinfo=timezone.utc)


def _passthrough_spawn(argv, mode="standard", **kw):
    return list(argv), dict(os.environ), None


@pytest.fixture(autouse=True)
def _no_sandbox(_floor_monkeypatch):
    _floor_monkeypatch.setattr(wt, "sandboxed_spawn_argv", _passthrough_spawn)
    _floor_monkeypatch.setattr(git_ops, "sandboxed_spawn_argv", _passthrough_spawn)


@pytest.fixture(autouse=True)
def _feature_on(_floor_monkeypatch):
    features.reset_capabilities()
    features.record_granted_scopes(["chat:write", "code_channels:manage"])
    _floor_monkeypatch.setattr(
        cc_binding,
        "live_config",
        lambda orch=None: SimpleNamespace(slack=SimpleNamespace(code_channels=True)),
    )
    yield
    features.reset_capabilities()
    source.set_scope_predicate(None)
    source.set_records_reader(None)
    board_pkg.reset_for_tests()


@pytest.fixture(autouse=True)
def _surface_store(tmp_path):
    surface_store.set_store(surface_store.SurfaceStore(tmp_path / "surface.json"))
    yield
    surface_store.set_store(None)


class StepClock:
    """A fake monotonic clock: each read is *step* seconds after the last."""

    def __init__(self, start=1000.0, step=30.0):
        self.now, self.step = start, step

    def __call__(self):
        self.now += self.step
        return self.now


@pytest.fixture(autouse=True)
def publisher_clock(monkeypatch):
    """Publishes 30s apart: past the edit gap, inside the stale-stamp window."""
    clock = StepClock()
    monkeypatch.setattr(publisher, "_clock", clock)
    return clock


def _binding(channel="C1", session="chat-cond") -> Binding:
    return Binding(channel_id=channel, team_id="T1", session_key=session, anchor_ts="1.0")


def _reader(fn):
    """An async ledger reader (the seam's shape) over a plain function."""

    async def _read(key):
        return fn(key)

    return _read


def _row(item_id="it_00000001", state=model.WORKING, **kw) -> model.WorkerRow:
    return model.WorkerRow(
        item_id=item_id, title=kw.pop("title", f"Task {item_id}"), state=state, **kw
    )


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


# ── state mapping ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("item_state", "status", "reason", "live", "want"),
    [
        ("accepted", "progress", None, model.LIVE_RUNNING, model.DONE),
        ("abandoned", None, None, model.LIVE_IDLE, model.CLOSED),
        ("rejected", "done", None, model.LIVE_GONE, model.CLOSED),
        ("open", "progress", None, model.LIVE_APPROVAL, model.WAITING_ON_YOU),
        ("open", "blocked", None, model.LIVE_RUNNING, model.WORKING),
        ("open", None, None, model.LIVE_QUEUED, model.WORKING),
        ("open", "question", "needs_human", model.LIVE_IDLE, model.WAITING_ON_YOU),
        ("open", "blocked", "approval", model.LIVE_IDLE, model.WAITING_ON_YOU),
        ("open", "blocked", None, model.LIVE_IDLE, model.BLOCKED),
        ("open", "question", None, model.LIVE_IDLE, model.BLOCKED),
        ("open", "progress", None, model.LIVE_GONE, model.LOST),
        ("open", None, None, model.LIVE_GONE, model.LOST),
        ("open", "done", None, model.LIVE_GONE, model.DONE),
        ("open", "done", None, model.LIVE_IDLE, model.DONE),
        ("open", "progress", None, model.LIVE_IDLE, model.IDLE),
        ("open", None, None, model.LIVE_UNBOUND, model.IDLE),
    ],
)
def test_row_state_mapping(item_state, status, reason, live, want):
    assert model.row_state(item_state=item_state, status=status, reason=reason, live=live) == want


def test_aggregate_line_and_overall_state():
    rows = [
        _row("it_00000001", model.WORKING),
        _row("it_00000002", model.WORKING),
        _row("it_00000003", model.WORKING),
        _row("it_00000004", model.WAITING_ON_YOU),
        _row("it_00000005", model.DONE),
    ]
    assert model.aggregate_line(rows) == "3 working · 1 waiting on you · 1 of 5 done"
    assert model.overall_state(rows) == model.OVERALL_NEEDS_YOU
    assert model.aggregate_line([_row(state=model.WORKING)]) == "1 working · 0 of 1 done"
    with_pr = [_row(state=model.WORKING, pr_number=3, pr_state="open"), _row("it_2", model.DONE)]
    assert model.aggregate_line(with_pr) == "1 working · 1 of 2 done · 1 PR open"
    assert model.aggregate_line([_row(state=model.CLOSED)]) == "1 closed · 0 of 1 done"
    assert model.overall_state([_row(state=model.DONE), _row(state=model.CLOSED)]) == "done"
    assert model.overall_state([_row(state=model.LOST), _row(state=model.IDLE)]) == "stuck"
    assert model.overall_state([]) == model.OVERALL_EMPTY
    assert model.aggregate_line([]) == "No workers yet"


def test_summary_is_one_redacted_depinged_line_within_the_cap():
    text = "Hi <!channel> and <@U123|bob> <!subteam^S1|devs>\nsecond line never shown"
    out = model.clean_text(text, 40)
    assert "<!" not in out and "<@" not in out and "second" not in out
    assert out.startswith("Hi @channel and @U123 devs")
    long = model.clean_text("x" * 500, model.SUMMARY_CHARS)
    assert len(long) == model.SUMMARY_CHARS and long.endswith("…")
    key = "AKIA" + "ABCDEFGHIJKLMNOP"
    assert key not in model.clean_text(f"leaked {key} here", 140)


# ── source: scope, projection, liveness ─────────────────────────────────────


@dataclass
class _Item:
    item_id: str
    title: str = "Do a thing"
    state: str = "open"
    status: str | None = None
    reason: str | None = None
    summary: str = ""
    artifacts: dict = field(default_factory=dict)
    pr: int | None = None
    worker_session_key: str | None = None
    last_report_at: str | None = None
    created_at: str = ""
    round: int = 1


class _State:
    """The dashboard slot table, as much of it as the board reads."""

    def __init__(self, slots=None, existing=()):
        self.slots = dict(slots or {})
        self.existing = set(existing) | set(self.slots)

    def get_slot(self, key):
        return self.slots.get(key)

    def slot_exists(self, key):
        return key in self.existing


def _slot(key, *, running=False, queue=(), folder=""):
    return SimpleNamespace(
        key=key,
        running=running,
        turn_running=running,
        _queue=list(queue),
        _approval_futures={},
        folder_id=folder,
    )


def test_read_board_refuses_out_of_scope_channels():
    reads: list[str] = []
    source.set_records_reader(
        _reader(lambda key: reads.append(key) or (SimpleNamespace(goal="g"), []))
    )
    source.set_scope_predicate(lambda state, binding: False)
    assert asyncio.run(source.read_board(_State(), _binding())) is None
    assert reads == []  # refused before any ledger read


def test_read_board_projects_only_the_published_set_with_liveness():
    items = [
        _Item(
            "it_00000001",
            title="Fix <!here> login",
            status="question",
            reason="needs_human",
            summary="Which OAuth app?\nsecret transcript line",
            artifacts={"branch": "feat/login", "worktree": "/abs/path", "notes": "x"},
            pr=12,
            worker_session_key="chat-w1",
            last_report_at="2026-10-18T10:00:00Z",
            created_at="2026-10-18T09:00:00Z",
        ),
        _Item(
            "it_00000002",
            status="progress",
            worker_session_key="chat-w2",
            artifacts={"branch": "--upload-pack=evil"},
            created_at="2026-10-18T09:01:00Z",
        ),
        _Item("it_00000003", created_at="2026-10-18T09:02:00Z"),
        _Item("it_00000004", worker_session_key="chat-w4", created_at="2026-10-18T09:03:00Z"),
    ]
    record = SimpleNamespace(goal="Ship <!channel> the login flow", round=2)
    source.set_records_reader(_reader(lambda key: (record, items)))
    source.set_scope_predicate(lambda state, binding: True)
    state = _State({"chat-w1": _slot("chat-w1"), "chat-w4": _slot("chat-w4", running=True)})
    snap = asyncio.run(source.read_board(state, _binding()))
    assert snap is not None and snap.round == 2 and "<!channel>" not in snap.goal
    first, second, third, fourth = snap.rows
    assert first.state == model.WAITING_ON_YOU and first.branch == "feat/login"
    assert first.summary == "Which OAuth app?" and "<!here>" not in first.title
    assert first.pr_number == 12 and first.session_key == "chat-w1"
    assert second.state == model.LOST  # its session is gone and it never said done
    assert second.branch == ""  # an option-shaped branch is never kept
    assert third.state == model.IDLE and third.session_key == ""  # not dispatched yet
    assert fourth.state == model.WORKING
    # Nothing outside the published set rides on a row.
    assert set(first.__dataclass_fields__) == {
        "item_id",
        "title",
        "state",
        "summary",
        "branch",
        "pr_number",
        "pr_url",
        "pr_state",
        "pr_ci",
        "pr_checks",
        "session_key",
        "report_stamp",
        "round",
        "waiting_on",
        "approval_tool",
        "closed_as",
        "step",
        "turn_started_at",
    }


def test_read_board_rechecks_scope_after_the_ledger_read():
    calls = iter([True, False])
    source.set_scope_predicate(lambda state, binding: next(calls))
    source.set_records_reader(_reader(lambda key: (SimpleNamespace(goal="g", round=1), [])))
    assert asyncio.run(source.read_board(_State(), _binding())) is None


def test_default_scope_refuses_what_the_dispatch_exemption_would_not_publish(monkeypatch):
    from kiro_crew.dashboard.session_control_parts import code_channel_exemption as cce

    live = SimpleNamespace(key="chat-cond", _slack_channel="C1")
    monkeypatch.setattr(cc_binding, "bound_slot", lambda state, b: live)
    judged: list = []

    def clauses(state, slot, binding):
        judged.append((slot, binding.channel_id))
        return verdict, None

    monkeypatch.setattr(cce, "_publish_clauses", clauses)
    verdict = "the code-channel dispatch switch is off"
    assert not source.scope_admits(object(), _binding())
    verdict = ""
    assert source.scope_admits(object(), _binding())
    assert judged == [(live, "C1"), (live, "C1")]


def test_default_scope_requires_the_live_bound_slot_and_no_archive(monkeypatch):
    live = SimpleNamespace(key="chat-cond", _slack_channel="C1")
    monkeypatch.setattr(cc_binding, "bound_slot", lambda state, b: live)
    monkeypatch.setattr(source, "_publish_refusal", lambda state, b: "")
    assert source.scope_admits(object(), _binding())
    live._slack_channel = "C-other"
    assert not source.scope_admits(object(), _binding())
    live._slack_channel = "C1"
    archived = dataclasses.replace(_binding(), archived_at=1.0)
    assert not source.scope_admits(object(), archived)
    monkeypatch.setattr(cc_binding, "bound_slot", lambda state, b: None)
    assert not source.scope_admits(object(), _binding())


def test_store_seam_blanks_a_worker_that_does_not_bind_back(monkeypatch):
    from kiro_crew import work_ledger
    from kiro_crew.dashboard.handlers import work_ledger_board

    mine = work_ledger.WorkItem(item_id="it_0000000a", worker_session_key="chat-mine")
    stolen = work_ledger.WorkItem(item_id="it_0000000b", worker_session_key="chat-other")
    monkeypatch.setattr(work_ledger, "read_conductor", lambda key: work_ledger.ConductorRecord())
    monkeypatch.setattr(work_ledger, "list_work_items", lambda key: [mine, stolen])
    bindings = {"chat-mine": ("cond", "it_0000000a"), "chat-other": ("someone-else", "it_0000000b")}
    monkeypatch.setattr(work_ledger, "read_binding", lambda key: bindings.get(key))
    monkeypatch.setattr(work_ledger, "cache_dirty", lambda key: None)
    _record, items = asyncio.run(work_ledger_board.own_worker_records("cond"))
    assert [i.worker_session_key for i in items] == ["chat-mine", None]
    monkeypatch.setattr(work_ledger, "cache_dirty", lambda key: "undo failed")
    assert asyncio.run(work_ledger_board.own_worker_records("cond")) == (None, [])


# ── render ──────────────────────────────────────────────────────────────────


def _snapshot() -> model.BoardSnapshot:
    return model.BoardSnapshot(
        goal="Ship the login flow",
        round=2,
        rows=(
            _row(
                "it_00000001",
                model.WORKING,
                title="Fix login",
                summary="tests green locally",
                branch="feat/login",
                pr_number=12,
                pr_url="https://github.com/acme/widgets/pull/12",
                pr_state="open",
                pr_ci="passed",
                session_key="chat-w1",
            ),
            _row(
                "it_00000002",
                model.WAITING_ON_YOU,
                title="Pick an icon",
                summary="Which of the two?",
                session_key="chat-w2",
                report_stamp="2026-10-18T10:00:00Z",
            ),
        ),
    )


def _link(key: str) -> str:
    return f"https://dash.example/chat?sid={key}" if key else ""


def test_board_blocks_show_header_rows_and_links():
    blocks = render.board_blocks(
        _snapshot(), _link, conductor_link=_link("chat-cond"), updated="10:00 UTC"
    )
    context, summary, plan = blocks
    assert context["elements"][0]["text"].startswith("Work board · updated 10:00 UTC · <https://")
    assert summary["text"]["text"] == "*1 working · 1 waiting on you · 0 of 2 done · 1 PR open*"
    assert (
        plan["type"] == "plan"
        and plan["title"] == "1 working · 1 waiting on you · 0 of 2 done · 1 PR open"
    )
    first, second = plan["tasks"]
    assert first["task_id"] == "it_00000001" and first["title"] == "Fix login"
    assert first["status"] == "in_progress" and second["status"] == "in_progress"
    output = first["output"]["elements"][0]["elements"]
    assert output[0] == {
        "type": "link",
        "url": "https://github.com/acme/widgets/pull/12",
        "text": "PR #12",
    }
    assert first["sources"] == [
        {"type": "url", "url": "https://github.com/acme/widgets/pull/12", "text": "PR #12"}
    ]
    waiting = second["details"]["elements"][0]["elements"]
    assert waiting[0] == {"type": "text", "text": "Waiting on you:", "style": {"bold": True}}
    assert render.fallback_text(_snapshot()) == (
        "Work board: 1 working · 1 waiting on you · 0 of 2 done · 1 PR open"
    )
    # No custom emoji anywhere: Slack's own status marks are the only ones.
    assert not re.search(r":[a-z_]+:", json.dumps(blocks))
    rows = render.board_blocks(_snapshot(), _link, look="rows", updated="10:00 UTC")
    text = json.dumps(rows)
    assert "*Fix login*" in text and "`feat/login`" in text and "Open in dashboard" in text


def test_board_escapes_markup_in_published_text():
    snap = model.BoardSnapshot(rows=(_row(title="a <b> & c", summary="<https://x|y>"),))
    text = render.row_text(snap.rows[0], _link)
    assert "&lt;b&gt; &amp; c" in text and "<https://x|y>" not in text


def test_needs_you_text_mentions_the_owner_and_links():
    row = _snapshot().rows[1]
    text = render.needs_you_text(row, "U0OWNER", _link)
    assert text.startswith("<@U0OWNER> *Pick an icon* is waiting on you: Which of the two?")
    assert "Open in dashboard" in text
    assert not render.needs_you_text(row, "", _link).startswith("<@")


def test_the_bar_label_counts_every_worker_in_short_words():
    snap = model.BoardSnapshot(
        rows=tuple(_row(f"it_0000000{i}", branch=f"b{i}") for i in range(1, 6))
    )
    assert model.bar_label(snap) == "5 working"
    assert model.bar_label(model.BoardSnapshot()) == ""


# ── publisher ───────────────────────────────────────────────────────────────


class _SlackError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"error": code}


class FakeSlack:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.fail: dict[str, str] = {}
        self._n = 0

    def _maybe_fail(self, method: str) -> None:
        code = self.fail.pop(method, "")
        if code:
            raise _SlackError(code)

    async def post_blocks(self, channel, blocks, text, thread_ts=None):
        self._maybe_fail("post_blocks")
        self._n += 1
        self.calls.append(("post_blocks", channel, text))
        return f"100.{self._n}"

    async def update_message(self, channel, ts, text="", blocks=None):
        self.calls.append(("update_message", channel, ts, text))
        self._maybe_fail("update_message")

    async def add_pin(self, channel, ts):
        self.calls.append(("add_pin", channel, ts))
        self._maybe_fail("add_pin")

    async def post_message(self, channel, text, thread_ts=None):
        self.calls.append(("post_message", channel, text, thread_ts))
        return "200.1"

    def of(self, method):
        return [c for c in self.calls if c[0] == method]


@pytest.fixture
def slack(monkeypatch):
    fake = FakeSlack()
    monkeypatch.setattr(surface, "slack", lambda: fake)

    async def _true():
        return True

    async def _not_archived(_binding):
        return False

    monkeypatch.setattr(surface, "outbound_permitted", _true)
    monkeypatch.setattr(surface, "is_archived", _not_archived)
    monkeypatch.setattr(surface, "channel_stopped", lambda channel: False)
    monkeypatch.setattr(surface, "audit", lambda *a, **k: None)
    return fake


def _publish(snapshot, owner="U0OWNER"):
    return asyncio.run(publisher.publish(_binding(), snapshot, link_for=_link, owner_id=owner))


def test_first_publish_posts_and_pins_then_edits_in_place_only_on_change(slack):
    assert _publish(_snapshot()) == "posted"
    assert len(slack.of("post_blocks")) == 1 and slack.of("add_pin") == [("add_pin", "C1", "100.1")]
    record = surface_store.store().get("C1")
    assert record.board_ts == "100.1" and record.board_pinned
    assert _publish(_snapshot()) == ""  # unchanged: no Slack write at all
    assert slack.of("update_message") == []
    moved = model.BoardSnapshot(goal="Ship the login flow", round=2, rows=_snapshot().rows[:1])
    assert _publish(moved) == "updated"
    assert slack.of("update_message")[-1][2] == "100.1"
    assert len(slack.of("post_blocks")) == 1 and len(slack.of("add_pin")) == 1


def test_a_deleted_board_is_reposted_and_pinned_again(slack):
    _publish(_snapshot())
    slack.fail["update_message"] = "message_not_found"
    changed = model.BoardSnapshot(rows=_snapshot().rows[:1])
    assert _publish(changed) == "reposted"
    assert surface_store.store().get("C1").board_ts == "100.2"
    assert slack.of("add_pin")[-1] == ("add_pin", "C1", "100.2")


def test_already_pinned_counts_as_pinned(slack):
    slack.fail["add_pin"] = "already_pinned"
    _publish(_snapshot())
    assert surface_store.store().get("C1").board_pinned


def test_a_transient_update_failure_is_retried_next_time(slack):
    _publish(_snapshot())
    slack.fail["update_message"] = "ratelimited"
    changed = model.BoardSnapshot(rows=_snapshot().rows[:1])
    assert _publish(changed) == "failed"
    assert _publish(changed) == "updated"  # the digest did not advance on failure


def test_needs_you_is_threaded_once_per_report_with_the_owner_mentioned(slack):
    _publish(_snapshot())
    replies = slack.of("post_message")
    assert len(replies) == 1
    _, channel, text, thread_ts = replies[0]
    assert channel == "C1" and thread_ts == "100.1" and text.startswith("<@U0OWNER>")
    _publish(model.BoardSnapshot(goal="moved", round=2, rows=_snapshot().rows))
    assert len(slack.of("post_message")) == 1  # an edit never pages twice
    asked_again = list(_snapshot().rows)
    asked_again[1] = model.WorkerRow(
        **{**asked_again[1].__dict__, "report_stamp": "2026-10-18T11:00:00Z"}
    )
    _publish(model.BoardSnapshot(goal="moved", round=2, rows=tuple(asked_again)))
    assert len(slack.of("post_message")) == 2  # a NEW report waiting on you pages again


def test_publisher_respects_policy_archive_and_stop(slack, monkeypatch):
    async def _false():
        return False

    monkeypatch.setattr(surface, "outbound_permitted", _false)
    assert _publish(_snapshot()) == "denied"
    monkeypatch.setattr(surface, "channel_stopped", lambda c: True)
    assert _publish(_snapshot()) == "stopped"

    async def _archived(_b):
        return True

    monkeypatch.setattr(surface, "is_archived", _archived)
    assert _publish(_snapshot()) == "archived"
    assert slack.calls == []
    assert _publish(None) == ""


# ── workspace integration: debounce, combined diff, bar, PRs ────────────────


@pytest.fixture
def repo(tmp_path) -> Path:
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


@dataclass
class _FakeBinding:
    channel_id: str = "C1"
    team_id: str = "T1"
    session_key: str = "chat-cond"
    project_dir: str | None = None
    title: str = "Fix the widget"


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def set_context_bar(self, channel_id, items):
        self.calls.append(("set_context_bar", {"items": items}))

    async def set_diff_view(self, channel_id, content, *, base_branch="", head_branch=""):
        self.calls.append(("set_diff_view", {"content": content}))
        return "V1"

    async def remove_view(self, channel_id, view_id):
        self.calls.append(("remove_view", {"view_id": view_id}))

    def last(self, method):
        return [kw for m, kw in self.calls if m == method][-1]


class _Harness:
    def __init__(self, tmp_path, snapshot=None, prs=None):
        self.client = _FakeClient()
        self.snapshot = snapshot
        self.prs = prs or {}
        self.pr_queries: list[str] = []
        self.published: list[model.BoardSnapshot] = []

        async def set_project(key, path):
            return True

        async def find_pr(repo, branch):
            self.pr_queries.append(branch)
            return self.prs.get(branch, PrLookup("none"))

        async def find_pr_number(repo, number):
            self.pr_queries.append(f"#{number}")
            return self.prs.get(f"#{number}", PrLookup("unavailable"))

        async def read_board(binding):
            return self.snapshot

        async def publish_board(binding, snap):
            self.published.append(snap)

        self.svc = service.WorkspaceService(
            lambda: self.client,
            store=WorkspaceStore(tmp_path / "ws.json"),
            set_project=set_project,
            find_pr=find_pr,
            find_pr_number=find_pr_number,
            read_board=read_board,
            publish_board=publish_board,
        )


def test_board_refreshes_are_debounced_into_one_run(tmp_path, monkeypatch):
    h = _Harness(tmp_path)
    runs: list[str] = []

    async def fake_refresh(binding, *, recheck_pr=True):
        runs.append(binding.channel_id)

    monkeypatch.setattr(h.svc, "refresh", fake_refresh)

    async def go():
        for _ in range(10):
            h.svc.schedule_refresh(_FakeBinding(), delay=0.05)
        await h.svc.drain()

    asyncio.run(go())
    assert runs == ["C1"]


def test_combined_diff_groups_each_workers_commits_under_a_header(tmp_path, repo):
    worker_tree = tmp_path / "worker-wt"
    _git("worktree", "add", "-q", "-b", "feat/login", str(worker_tree), "main", cwd=repo)
    (worker_tree / "login.py").write_text("def login():\n    return True\n")
    _git("add", ".", cwd=worker_tree)
    _git("commit", "-q", "-m", "login", cwd=worker_tree)
    _git("branch", "feat/merged", "main", cwd=repo)  # no commits of its own
    snap = model.BoardSnapshot(
        goal="g",
        rows=(
            _row(
                "it_00000001", model.WORKING, title="Fix login", branch="feat/login", pr_number=12
            ),
            _row("it_00000002", model.DONE, title="Nothing new", branch="feat/merged"),
            _row("it_00000003", model.IDLE, title="Bad branch", branch="no/such/branch"),
        ),
    )
    pr = PrLookup(
        "found", PullRequest(12, "https://github.com/acme/widgets/pull/12", "open", "passed")
    )
    h = _Harness(tmp_path, snapshot=snap, prs={"feat/login": pr})
    binding = _FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    (Path(rec.worktree) / "README.md").write_text("hello\nchannel edit\n")
    asyncio.run(h.svc.refresh(binding))
    content = h.client.last("set_diff_view")["content"]
    channel_at = content.index(f"==== Channel branch {rec.branch} (conductor) ====")
    worker_at = content.index(
        "==== Worker 1 of 3: Fix login · branch feat/login · PR #12 open · tests passing · working ===="
    )
    assert channel_at < content.index("+channel edit") < worker_at < content.index("+def login():")
    assert "Nothing new" not in content and "Bad branch" not in content
    # The published board carries the forge's PR reading.
    assert (
        h.published[-1].rows[0].pr_url.endswith("/pull/12")
        and h.published[-1].rows[0].pr_ci == "passed"
    )
    # The context bar: the board's counts first, then the channel's own items,
    # and the one worker PR in the PR slot (its title stays on the board).
    labels = [i["label"] for i in h.client.last("set_context_bar")["items"]]
    assert labels[:3] == ["1 working · 1 idle · 1 of 3 done", "acme/widgets", rec.branch]
    assert labels[-1] == "PR #12 is open · tests passing"


_WORKER_PEM_BODY = ["MIIEowIBAAKCAQEA" + "A" * 48] + [f"{n:02d}" + "Qk" * 31 for n in range(20)]


def _worker_pem(lines: list[str]) -> str:
    return (
        "-----BEGIN RSA PRIVATE KEY-----\n" + "\n".join(lines) + "\n-----END RSA PRIVATE KEY-----\n"
    )


def test_a_worker_commit_inside_a_private_key_never_reaches_the_combined_diff(tmp_path, repo):
    """A worker hunk 10 lines into a PEM carries no marker; the whole-file screen still sees it."""
    (repo / "fixtures").mkdir()
    (repo / "fixtures" / "test_key.txt").write_text(_worker_pem(_WORKER_PEM_BODY))
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "fixture key", cwd=repo)
    worker_tree = tmp_path / "worker-wt"
    _git("worktree", "add", "-q", "-b", "feat/keys", str(worker_tree), "main", cwd=repo)
    edited = list(_WORKER_PEM_BODY)
    edited[10] = "ZZ" + edited[10][2:]
    (worker_tree / "fixtures" / "test_key.txt").write_text(_worker_pem(edited))
    (worker_tree / "visible.py").write_text("VISIBLE = 1\n")
    _git("add", ".", cwd=worker_tree)
    _git("commit", "-q", "-m", "rotate", cwd=worker_tree)
    snap = model.BoardSnapshot(
        rows=(_row("it_00000001", model.WORKING, title="Rotate", branch="feat/keys"),)
    )
    h = _Harness(tmp_path, snapshot=snap)
    binding = _FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    (Path(rec.worktree) / "README.md").write_text("hello\nchannel edit\n")
    asyncio.run(h.svc.refresh(binding))
    content = h.client.last("set_diff_view")["content"]
    assert "+VISIBLE = 1" in content and content.startswith("Diff truncated")
    assert "test_key.txt" not in content
    for line in edited + _WORKER_PEM_BODY:
        assert line not in content and line not in repr(h.client.calls)


def test_a_worker_range_read_hands_the_screen_whole_files_whatever_the_repo_config(tmp_path, repo):
    """Like tracked_diff: whole-file context, and repo diff config cannot reshape the read."""
    lines = [f"line {n}" for n in range(40)]
    (repo / "a.txt").write_text("\n".join(lines) + "\n")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "a", cwd=repo)
    base = _git("rev-parse", "HEAD", cwd=repo)
    lines[20] = "CHANGED"
    (repo / "a.txt").write_text("\n".join(lines) + "\n")
    _git("commit", "-q", "-am", "b", cwd=repo)
    head = _git("rev-parse", "HEAD", cwd=repo)
    for key in ("diff.noprefix", "diff.mnemonicPrefix", "diff.suppressBlankEmpty"):
        _git("config", key, "true", cwd=repo)
    full, cut = git_ops.commit_range_diff(str(repo), base, head, cap=10_000_000)
    seen: list[str] = []
    asked: list[str] = []
    screened, omitted = screen_tracked(
        full,
        truncated=cut,
        classify=lambda s: seen.append(s) or CLEAN,
        mask=str,
        sensitive=lambda p: asked.append(p) or False,
    )
    assert not omitted and b"+CHANGED" in screened
    assert set(asked) == {"a.txt"}
    assert "\n".join(lines) in seen  # the screen classified the whole new version


def _two_worker_branches(tmp_path, repo):
    (repo / "big.txt").write_text("".join(f"line {n}\n" for n in range(400)))
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "big", cwd=repo)
    for name in ("feat/w1", "feat/w2"):
        tree = tmp_path / name.replace("/", "-")
        _git("worktree", "add", "-q", "-b", name, str(tree), "main", cwd=repo)
        (tree / "big.txt").write_text((tree / "big.txt").read_text().replace("line 7\n", "X\n"))
        _git("commit", "-q", "-am", name, cwd=tree)
    return model.BoardSnapshot(
        rows=(
            _row("it_00000001", model.WORKING, title="One", branch="feat/w1"),
            _row("it_00000002", model.WORKING, title="Two", branch="feat/w2"),
        )
    )


def test_worker_reads_share_one_raw_scan_budget_and_say_what_they_left_out(
    tmp_path, repo, monkeypatch
):
    """Whole-file reads screen down to a few lines, so the RAW bytes bound the work."""
    board = _two_worker_branches(tmp_path, repo)
    reads: list[int] = []
    real = git_ops.commit_range_diff

    def counted(cwd, base, head, *, cap):
        reads.append(cap)
        return real(cwd, base, head, cap=cap)

    monkeypatch.setattr(git_ops, "commit_range_diff", counted)
    monkeypatch.setattr(service, "_SCAN_BYTES", 5000)  # big.txt's whole-file read is ~3.4 KB
    groups = service._worker_groups(str(repo), board, "main")
    assert len(reads) == 2 and reads[1] < reads[0] == 5000  # the second read gets what is left
    monkeypatch.setattr(service, "_SCAN_BYTES", 3000)  # one read spends it all
    reads.clear()
    groups = service._worker_groups(str(repo), board, "main")
    assert len(reads) == 1  # the second branch is never read
    assert groups[-1] == DiffGroup("", b"", True)  # the left-out marker, not the cut read
    built = assemble_groups(groups)
    assert built.truncated and built.content.startswith("Diff truncated")


def test_workers_with_nothing_of_their_own_leave_no_truncation_note(tmp_path, repo):
    _git("branch", "feat/merged", "main", cwd=repo)
    board = model.BoardSnapshot(
        rows=(
            _row("it_00000001", model.DONE, title="Merged", branch="feat/merged"),
            _row("it_00000002", model.IDLE, title="Bad", branch="no/such/branch"),
        )
    )
    assert service._worker_groups(str(repo), board, "main") == []


def test_a_worker_whose_range_read_fails_is_reported_as_left_out(tmp_path, repo, monkeypatch):
    board = _two_worker_branches(tmp_path, repo)
    monkeypatch.setattr(git_ops, "commit_range_diff", lambda *a, **k: None)
    built = assemble_groups(service._worker_groups(str(repo), board, "main"))
    assert built.truncated and built.content.startswith("Diff truncated")


def test_no_board_keeps_the_plain_single_diff(tmp_path, repo):
    h = _Harness(tmp_path, snapshot=None)
    binding = _FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    (Path(rec.worktree) / "README.md").write_text("changed\n")
    asyncio.run(h.svc.refresh(binding))
    content = h.client.last("set_diff_view")["content"]
    assert content.startswith("diff --git") and "====" not in content
    assert h.published == []


def test_worker_pr_by_number_fallback_and_cache(tmp_path, repo):
    pr = PrLookup("found", PullRequest(7, "https://github.com/acme/widgets/pull/7", "merged", ""))
    snap = model.BoardSnapshot(rows=(_row("it_00000001", model.DONE, pr_number=7),))
    h = _Harness(tmp_path, snapshot=snap, prs={"#7": pr})
    binding = _FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    asyncio.run(h.svc.refresh(binding))
    asyncio.run(h.svc.refresh(binding))
    assert h.pr_queries.count("#7") == 1  # a merged PR is read once, then cached
    assert h.published[-1].rows[0].pr_state == "merged"


def test_unconfirmed_pr_number_is_shown_without_a_link(tmp_path, repo):
    snap = model.BoardSnapshot(rows=(_row("it_00000001", model.WORKING, pr_number=99),))
    h = _Harness(tmp_path, snapshot=snap)
    binding = _FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    asyncio.run(h.svc.refresh(binding))
    row = h.published[-1].rows[0]
    assert row.pr_number == 99 and row.pr_url == "" and row.pr_ci == ""


def test_assemble_groups_shares_one_budget_and_says_when_it_cut():
    big = b"diff --git a/x b/x\n" + b"+" + b"y" * 3000 + b"\n"
    groups = [DiffGroup("Channel", big), DiffGroup("Worker 1", big), DiffGroup("Empty", b"")]
    built = assemble_groups(groups, limit=4500)
    assert built.truncated and built.content.startswith("Diff truncated")
    assert built.content.index("==== Channel ====") < built.content.index("==== Worker 1 ====")
    assert built.content.count("y") < 2 * 3000  # the second group was cut short
    whole = assemble_groups(groups)
    assert not whole.truncated and whole.files == 2 and "Empty" not in whole.content


def test_branch_commit_never_resolves_option_or_namespace_shaped_names(tmp_path, repo):
    assert git_ops.branch_commit(str(repo), "main")
    for bad in ("--all", "HEAD~1", "refs/tags/x", "a..b", ""):
        assert git_ops.branch_commit(str(repo), bad) == ""
    assert git_ops.commit_range_diff(str(repo), "HEAD", "main", cap=100) is None


# ── wrap-up, filing, triggers ───────────────────────────────────────────────


def test_wrap_up_lists_each_workers_outcome():
    rows = [
        _row(
            "it_00000001",
            model.DONE,
            title="Fix login",
            pr_number=12,
            pr_url="https://github.com/a/b/pull/12",
            pr_state="merged",
        ),
        _row(
            "it_00000002",
            model.WORKING,
            title="Docs",
            pr_number=13,
            pr_url="https://github.com/a/b/pull/13",
            pr_state="open",
            pr_ci="failed",
        ),
        _row("it_00000003", model.CLOSED, title="Spike", closed_as="abandoned"),
        _row("it_00000004", model.LOST, title="Flaky test"),
    ]
    lines = [render.outcome_line(r) for r in rows]
    assert lines == [
        "• Fix login: <https://github.com/a/b/pull/12|PR #12> merged",
        "• Docs: <https://github.com/a/b/pull/13|PR #13> open · tests failing",
        "• Spike: abandoned",
        "• Flaky test: lost, no PR",
    ]
    blocks = wrapup.wrap_up_blocks("All done.", wrapup.Facts(workers=tuple(lines)), "C1")
    context = blocks[1]["elements"][0]["text"]
    assert context.startswith("*Workers*\n• Fix login") and "• Spike: abandoned" in context


def test_gather_facts_carries_worker_outcomes(monkeypatch):
    async def lines(binding):
        return ["• A: abandoned"]

    monkeypatch.setattr(board_pkg, "outcome_lines", lines)
    monkeypatch.setattr(wrapup, "_gather_facts_sync", lambda b: wrapup.Facts(branch="x"))
    facts = asyncio.run(wrapup.gather_facts(_binding()))
    assert facts.workers == ("• A: abandoned",) and facts.branch == "x"


def test_unfiled_workers_join_the_conductors_folder_and_filed_ones_stay(monkeypatch):
    state = _State(
        {
            "chat-cond": _slot("chat-cond", folder="F-channel"),
            "chat-w1": _slot("chat-w1"),
            "chat-w2": _slot("chat-w2", folder="F-mine"),
        }
    )
    monkeypatch.setattr(surface, "dashboard_state", lambda: state)
    monkeypatch.setattr(surface, "audit", lambda *a, **k: None)
    placed: list[tuple[str, str]] = []

    async def fake_place(st, slot, folder):
        placed.append((slot.key, folder))
        slot.folder_id = folder
        return True

    monkeypatch.setattr(filing, "_place", fake_place)
    snap = model.BoardSnapshot(
        rows=(
            _row("it_00000001", session_key="chat-w1"),
            _row("it_00000002", session_key="chat-w2"),
        )
    )
    assert asyncio.run(filing.file_unfiled_workers(_binding(), snap)) == 1
    assert placed == [("chat-w1", "F-channel")]
    assert state.slots["chat-w2"].folder_id == "F-mine"


def test_worker_turn_and_ledger_moves_schedule_a_debounced_refresh(monkeypatch):
    scheduled: list[tuple[str, float]] = []

    class _Svc:
        def schedule_refresh(self, binding, *, delay=0.0):
            scheduled.append((binding.channel_id, delay))

    from kiro_crew.slack.code_channels.workspace import core as ws_core

    monkeypatch.setattr(ws_core, "service", lambda: _Svc())
    monkeypatch.setattr(
        cc_binding, "active_binding", lambda channel, thread_ts=None: _binding(channel)
    )
    board_pkg.remember("C1", "chat-cond", ["chat-w1"])
    board_pkg.note_session_turn("chat-unrelated")
    assert scheduled == []
    board_pkg.note_session_turn("chat-w1")
    board_pkg.note_ledger_write("chat-cond")
    assert scheduled == [("C1", service.BOARD_DEBOUNCE_SECS)] * 2


def test_work_fold_events_for_other_boards_are_ignored():
    calls: list = []
    loop = SimpleNamespace(call_soon_threadsafe=lambda fn, *a: calls.append((fn, a)))
    board_pkg._bus["loop"] = loop
    board_pkg.remember("C1", "chat-cond", [])
    # A key just found bound to no channel costs the bus thread one lookup.
    board_pkg._unbound["chat-elsewhere"] = time.monotonic()
    board_pkg._on_work_fold(SimpleNamespace(key="chat-elsewhere"))
    assert calls == []
    board_pkg._on_work_fold(SimpleNamespace(key="chat-cond"))
    assert calls == [(board_pkg.note_ledger_write, ("chat-cond",))]


def test_a_board_not_yet_published_is_hopped_so_its_first_write_is_seen():
    calls: list = []
    board_pkg._bus["loop"] = SimpleNamespace(
        call_soon_threadsafe=lambda fn, *a: calls.append((fn, a))
    )
    board_pkg._on_work_fold(SimpleNamespace(key="chat-new-cond"))
    assert calls == [(board_pkg.note_ledger_write, ("chat-new-cond",))]


def _stub_bindings(monkeypatch, bindings):
    from kiro_crew.slack.code_channels import store as cc_store

    monkeypatch.setattr(
        cc_store, "store", lambda: SimpleNamespace(loaded_bindings=lambda: bindings)
    )


def _recording_service(monkeypatch):
    scheduled: list[tuple[str, float]] = []

    class _Svc:
        def schedule_refresh(self, binding, *, delay=0.0):
            scheduled.append((binding.channel_id, delay))

    from kiro_crew.slack.code_channels.workspace import core as ws_core

    monkeypatch.setattr(ws_core, "service", lambda: _Svc())
    monkeypatch.setattr(
        cc_binding, "active_binding", lambda channel, thread_ts=None: _binding(channel)
    )
    return scheduled


def test_the_first_dispatch_of_a_channel_conductor_refreshes_its_board(monkeypatch):
    # No board yet: the conductor's first ledger write (its bind) is matched to
    # its channel through the code-channel bindings, and the board is refreshed.
    scheduled = _recording_service(monkeypatch)
    _stub_bindings(monkeypatch, [_binding("C7", "chat-cond")])
    board_pkg.note_ledger_write("dashboard_chat-cond")
    assert scheduled == [("C7", service.BOARD_DEBOUNCE_SECS)]
    # ...and is remembered, so the worker's own triggers find the channel too.
    assert board_pkg._conductor_channels["chat-cond"] == "C7"


def test_a_ledger_no_channel_is_bound_to_is_looked_up_once_per_window(monkeypatch):
    scheduled = _recording_service(monkeypatch)
    lookups: list[int] = []
    from kiro_crew.slack.code_channels import store as cc_store

    def _bindings():
        lookups.append(1)
        return [
            _binding("C7", "chat-cond"),
            dataclasses.replace(_binding("C8", "chat-x"), archived_at=1.0),
        ]

    monkeypatch.setattr(cc_store, "store", lambda: SimpleNamespace(loaded_bindings=_bindings))
    board_pkg.note_ledger_write("chat-x")  # archived channel: not its board
    board_pkg.note_ledger_write("chat-x")
    assert scheduled == [] and len(lookups) == 1


def test_an_unloaded_store_is_not_remembered_as_unbound(monkeypatch):
    scheduled = _recording_service(monkeypatch)
    _stub_bindings(monkeypatch, None)
    board_pkg.note_ledger_write("chat-cond")
    assert scheduled == [] and "chat-cond" not in board_pkg._unbound
    _stub_bindings(monkeypatch, [_binding("C7", "chat-cond")])
    board_pkg.note_ledger_write("chat-cond")
    assert scheduled == [("C7", service.BOARD_DEBOUNCE_SECS)]


def test_the_workspace_watcher_subscribes_the_ledger_trigger(monkeypatch):
    from kiro_crew.slack.code_channels.workspace import core as ws_core

    subscribed: list[int] = []
    monkeypatch.setattr(board_pkg, "subscribe", lambda: subscribed.append(1))
    monkeypatch.setattr(ws_core, "_SERVICE", SimpleNamespace(start_watch=lambda state: None))
    ws_core.start_watch()
    assert subscribed == [1]


def _watch_scheduled(tmp_path, monkeypatch, readings_list, *, board=True):
    from kiro_crew.slack.code_channels.workspace import folders
    from kiro_crew.slack.code_channels.workspace.store import WorkspaceRecord

    h = _Harness(tmp_path)
    rec = WorkspaceRecord(
        team_id="T1",
        channel_id="C1",
        session_key="chat-cond",
        worktree=str(tmp_path),
        folder_id="F",
    )
    h.svc.store.put(rec)
    if board:
        h.svc._boards[rec.key] = model.BoardSnapshot(rows=(_row(session_key="chat-w1"),))
        h.svc._board_refreshed[rec.key] = h.svc._clock()
    readings = iter(readings_list)
    monkeypatch.setattr(folders, "subtree_ids", lambda state, fid: {fid})
    monkeypatch.setattr(folders, "sessions_in", lambda state, ids: next(readings))
    scheduled: list[float] = []
    monkeypatch.setattr(
        h.svc, "schedule_refresh", lambda binding, *, delay=0.0: scheduled.append(delay)
    )

    async def no_status(record, now):
        return None

    monkeypatch.setattr(h.svc, "_worker_status", no_status)
    for _ in readings_list:
        asyncio.run(h.svc.watch_tick(object()))
    return scheduled


def test_the_folder_watcher_refreshes_a_board_channel_when_a_worker_starts(tmp_path, monkeypatch):
    scheduled = _watch_scheduled(
        tmp_path, monkeypatch, [{"chat-w1": (False, 0)}, {"chat-w1": (True, 1)}]
    )
    assert scheduled == [service.BOARD_DEBOUNCE_SECS]


def test_the_folder_watcher_reads_a_new_generation_while_running_as_a_start(tmp_path, monkeypatch):
    # The last turn ended and the next began between two passes: still running,
    # but a later turn, which the board shows.
    scheduled = _watch_scheduled(
        tmp_path, monkeypatch, [{"chat-w1": (True, 1)}, {"chat-w1": (True, 2)}]
    )
    assert scheduled == [service.BOARD_DEBOUNCE_SECS]


def test_the_folder_watchers_first_pass_and_a_boardless_channel_schedule_no_start(
    tmp_path, monkeypatch
):
    assert _watch_scheduled(tmp_path, monkeypatch, [{"chat-w1": (True, 1)}]) == []
    assert (
        _watch_scheduled(
            tmp_path,
            monkeypatch,
            [{"chat-w1": (False, 0)}, {"chat-w1": (True, 1)}],
            board=False,
        )
        == []
    )


# ── review fixes ────────────────────────────────────────────────────────────


def test_a_goal_with_a_secret_or_a_ping_is_cleaned_in_the_snapshot():
    # The signed-off plan look shows no goal; the snapshot still keeps it clean.
    key = "AKIA" + "ABCDEFGHIJKLMNOP"
    record = SimpleNamespace(goal=f"Ship it <!channel> with {key}\nsecond line", round=1)
    source.set_records_reader(_reader(lambda k: (record, [])))
    source.set_scope_predicate(lambda state, binding: True)
    snap = asyncio.run(source.read_board(_State(), _binding()))
    assert key not in snap.goal and "<!channel>" not in snap.goal
    assert "second line" not in snap.goal and snap.goal.startswith("Ship it @channel")
    assert key not in json.dumps(render.board_blocks(snap, _link))


def test_a_credential_split_by_slack_emphasis_is_redacted():
    out = model.clean_text("token AKIA*ABCDEFGHIJKLMNOP* here", 140)
    assert "ABCDEFGHIJKLMNOP" not in out


def test_a_failed_verdict_or_an_answered_report_hands_the_move_back():
    assert (
        model.row_state(
            item_state="open", status="done", reason=None, live=model.LIVE_IDLE, verdict="fail"
        )
        == model.IDLE
    )
    assert (
        model.row_state(
            item_state="open",
            status="question",
            reason="needs_human",
            live=model.LIVE_IDLE,
            handed_back=True,
        )
        == model.IDLE
    )
    item = _Item(
        "it_0000000c",
        status="blocked",
        worker_session_key="chat-w",
        last_report_at="2026-10-18T10:00:00Z",
    )
    item.decided_at = "2026-10-18T10:05:00Z"
    snap = source.project(SimpleNamespace(goal="", round=1), [item], lambda k: model.LIVE_IDLE)
    assert snap.rows[0].state == model.IDLE


def test_totals_count_every_item_and_open_rows_are_always_shown():
    rows = [_row(f"it_{i:08x}", model.DONE) for i in range(25)]
    rows.append(_row("it_000000ff", model.WAITING_ON_YOU, title="Late question"))
    snap = model.BoardSnapshot(rows=tuple(rows))
    assert snap.total == 26 and snap.done == 25 and snap.overall == model.OVERALL_NEEDS_YOU
    assert len(snap.shown) == model.MAX_SHOWN and snap.omitted == 6
    assert snap.shown[-1].title == "Late question"
    rows_look = render.board_blocks(snap, _link, look="rows")
    assert "+6 more items" in str(rows_look[-1])
    # The plan shows every row up to Slack's 50 tasks.
    plan = render.board_blocks(snap, _link)[-1]
    assert len(plan["tasks"]) == 26


def test_an_answered_approval_re_arms_the_next_one(slack):
    row = _row(
        "it_00000009",
        model.WAITING_ON_YOU,
        title="Deploy",
        waiting_on="approval",
        session_key="chat-w9",
    )
    waiting = model.BoardSnapshot(goal="g", rows=(row,))
    quiet = model.BoardSnapshot(goal="g", rows=(replace(row, state=model.WORKING, waiting_on=""),))
    _publish(waiting)
    assert "has a tool approval waiting on you" in slack.of("post_message")[-1][2]
    _publish(waiting)
    assert len(slack.of("post_message")) == 1
    _publish(quiet)  # approved: the alert is forgotten
    _publish(waiting)  # the next approval pages again
    assert len(slack.of("post_message")) == 2


def test_a_worker_is_filed_once_and_a_person_moving_it_out_wins(monkeypatch):
    state = _State({"chat-cond": _slot("chat-cond", folder="F"), "chat-w1": _slot("chat-w1")})
    monkeypatch.setattr(surface, "dashboard_state", lambda: state)
    monkeypatch.setattr(surface, "audit", lambda *a, **k: None)

    async def fake_place(st, slot, folder):
        slot.folder_id = folder
        return True

    monkeypatch.setattr(filing, "_place", fake_place)
    snap = model.BoardSnapshot(rows=(_row("it_00000001", session_key="chat-w1"),))
    assert asyncio.run(filing.file_unfiled_workers(_binding(), snap)) == 1
    state.slots["chat-w1"].folder_id = ""  # the person drags it back out
    assert asyncio.run(filing.file_unfiled_workers(_binding(), snap)) == 0
    assert state.slots["chat-w1"].folder_id == ""


def test_bar_shows_the_board_in_one_item_and_keeps_worker_prs_on_the_board():
    from kiro_crew.slack.code_channels.workspace.context_bar import BarState, build_items

    snap = model.BoardSnapshot(
        rows=(
            _row("it_00000001", model.DONE, title="Merged"),
            _row("it_00000002", model.WAITING_ON_YOU, title="Asks you"),
            _row("it_00000003", model.WORKING, title="Busy"),
        )
    )
    assert model.bar_label(snap) == "1 working · 1 needs you · 1 of 3 done"
    items = build_items(BarState(board=snap, board_url="https://acme.slack.com/archives/C1/p1"))
    assert items == [
        {
            "key": "workers",
            "label": "1 working · 1 needs you · 1 of 3 done",
            "icon": "hierarchy",
            "url": "https://acme.slack.com/archives/C1/p1",
        }
    ]
    long = _row("it_00000004", title="x" * 120, pr_number=5, pr_state="open", pr_ci="failed")
    items = build_items(BarState(board=model.BoardSnapshot(rows=(long,))))
    assert [i["label"] for i in items] == ["1 working", "PR #5 is open · tests failing"]
    assert not any("x" * 20 in i["label"] for i in items)  # titles stay on the board


def test_worker_pr_cache_is_scoped_to_the_forge_host(tmp_path):
    from kiro_crew.slack.code_channels.workspace.remote import RepoRef

    h = _Harness(
        tmp_path,
        prs={"b": PrLookup("found", PullRequest(1, "https://github.com/a/r/pull/1", "open"))},
    )
    public = RepoRef(host="github.com", owner="a", name="r")
    enterprise = RepoRef(host="ghe.example.com", owner="a", name="r")
    asyncio.run(h.svc._worker_pr(public, "b", 0))
    asyncio.run(h.svc._worker_pr(enterprise, "b", 0))
    assert h.pr_queries == ["b", "b"]


def test_wrap_up_outcomes_keep_rejected_apart_and_fit_slacks_text_cap():
    rejected = _row(
        "it_00000001",
        model.CLOSED,
        title="Spike",
        closed_as="rejected",
        pr_number=9,
        pr_url="https://github.com/a/b/pull/9",
        pr_state="closed",
    )
    assert (
        render.outcome_line(rejected)
        == "• Spike: rejected, <https://github.com/a/b/pull/9|PR #9> closed"
    )
    many = tuple(f"• {'w' * 140} {i}" for i in range(40))
    context = wrapup.wrap_up_blocks("s", wrapup.Facts(workers=many), "C1")[1]["elements"][0]["text"]
    assert len(context) < 3000 and context.rstrip().endswith("more")


def test_a_coalesced_rerun_keeps_the_debounce(tmp_path):
    h = _Harness(tmp_path)
    started: list[bool] = []
    waits: list[float] = []

    class _Delays(dict):
        """The rerun debounce store, recording what each rerun is told to wait."""

        def pop(self, key, default=None):
            value = super().pop(key, default)
            waits.append(value)
            return value

    h.svc._again_delay = _Delays()

    async def slow_refresh(binding, *, recheck_pr=True):
        started.append(recheck_pr)
        if len(started) == 1:
            h.svc.schedule_refresh(binding, delay=0.01)  # a ledger move mid-refresh

    h.svc.refresh = slow_refresh

    async def go():
        h.svc.schedule_refresh(_FakeBinding())
        await h.svc.drain()

    asyncio.run(go())
    assert started == [True, False] and 0.01 in waits


def test_a_decide_after_done_hands_nothing_back_and_same_second_answers_a_question():
    done = _Item(
        "it_0000000d", status="done", worker_session_key="w", last_report_at="2026-10-18T10:00:00Z"
    )
    done.decided_at = "2026-10-18T10:05:00Z"
    asked = _Item(
        "it_0000000e",
        status="question",
        reason="needs_human",
        worker_session_key="w",
        last_report_at="2026-10-18T10:00:00Z",
    )
    asked.decided_at = "2026-10-18T10:00:00Z"
    snap = source.project(
        SimpleNamespace(goal="", round=1), [done, asked], lambda k: model.LIVE_GONE
    )
    assert [r.state for r in snap.rows] == [model.DONE, model.LOST]


def test_the_combined_diff_channel_header_passes_the_outbound_redaction(
    tmp_path, repo, monkeypatch
):
    worker_tree = tmp_path / "wt2"
    _git("worktree", "add", "-q", "-b", "feat/x", str(worker_tree), "main", cwd=repo)
    (worker_tree / "x.py").write_text("x = 1\n")
    _git("add", ".", cwd=worker_tree)
    _git("commit", "-q", "-m", "x", cwd=worker_tree)
    real = service.redact_via_context
    monkeypatch.setattr(
        service, "redact_via_context", lambda text: real(text).replace("Channel branch", "[R]")
    )
    snap = model.BoardSnapshot(rows=(_row("it_00000001", model.WORKING, branch="feat/x"),))
    h = _Harness(tmp_path, snapshot=snap)
    binding = _FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    (Path(rec.worktree) / "README.md").write_text("edit\n")
    asyncio.run(h.svc.refresh(binding))
    assert f"==== [R] {rec.branch} (conductor) ====" in h.client.last("set_diff_view")["content"]


def test_raising_and_answering_an_approval_refreshes_the_board(monkeypatch):
    from kiro_crew.dashboard.state_parts import slot_turns

    noted: list[str] = []
    monkeypatch.setattr(board_pkg, "note_session_turn", lambda key: noted.append(key))

    async def go():
        fake = SimpleNamespace(key="chat-w1", _approval_futures={}, _approval_instances={})
        future = asyncio.get_running_loop().create_future()
        slot_turns.register_approval(fake, "r1", future, {})
        assert noted == ["chat-w1"]
        future.set_result("allow")
        await asyncio.sleep(0)

    asyncio.run(go())
    assert noted == ["chat-w1", "chat-w1"]


def test_a_failed_filing_is_not_remembered(monkeypatch):
    state = _State({"chat-cond": _slot("chat-cond", folder="F"), "chat-w1": _slot("chat-w1")})
    monkeypatch.setattr(surface, "dashboard_state", lambda: state)
    monkeypatch.setattr(surface, "audit", lambda *a, **k: None)
    outcomes = iter([False, True])

    async def flaky_place(st, slot, folder):
        ok = next(outcomes)
        if ok:
            slot.folder_id = folder
        return ok

    monkeypatch.setattr(filing, "_place", flaky_place)
    snap = model.BoardSnapshot(rows=(_row("it_00000001", session_key="chat-w1"),))
    assert asyncio.run(filing.file_unfiled_workers(_binding(), snap)) == 0
    assert asyncio.run(filing.file_unfiled_workers(_binding(), snap)) == 1
    assert surface_store.store().get("C1").board_filed == ("chat-w1",)


def test_the_board_is_read_and_published_without_the_channel_lock(tmp_path, repo):
    # A turn start's ensure() takes the channel lock: the board's ledger read, its
    # worker PR lookups and its Slack message must not hold it.
    snap = model.BoardSnapshot(rows=(_row("it_00000001", model.WORKING, branch="feat/x"),))
    h = _Harness(tmp_path, snapshot=snap)
    binding = _FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    held: list[bool] = []
    lock = h.svc._lock("T1:C1")

    async def read_board(b):
        held.append(lock.locked())
        return snap

    async def publish_board(b, s):
        held.append(lock.locked())

    h.svc._read_board, h.svc._publish_board = read_board, publish_board
    asyncio.run(h.svc.refresh(binding))
    assert held == [False, False]


def test_the_worker_pr_lookups_run_without_the_channel_lock(tmp_path, repo):
    snap = model.BoardSnapshot(rows=(_row("it_00000001", model.WORKING, branch="feat/x"),))
    h = _Harness(tmp_path, snapshot=snap)
    binding = _FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    lock = h.svc._lock("T1:C1")
    held: list[bool] = []
    inner = h.svc._find_pr

    async def find_pr(repo_, branch):
        held.append(lock.locked())
        return await inner(repo_, branch)

    h.svc._find_pr = find_pr
    asyncio.run(h.svc.refresh(binding))
    # The channel's own PR and the worker's: both read off the lock.
    assert "feat/x" in h.pr_queries and held and not any(held)


def test_a_rebind_mid_refresh_re_reads_the_channel_pr_on_the_rerun(tmp_path, repo):
    snap = model.BoardSnapshot(rows=(_row("it_00000001", model.WORKING),))
    h = _Harness(tmp_path, snapshot=snap)
    binding = _FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))

    async def publish_board(b, s):
        current = h.svc.store.get("T1", "C1")
        current.branch = "kirocrew/rebound"
        h.svc.store.put(current)

    h.svc._publish_board = publish_board
    asyncio.run(h.svc.refresh(binding))
    # The PR this pass read was the old branch's: the rerun, even a board-only
    # one, reads the new branch's instead of reusing it for a minute.
    assert "T1:C1" not in h.svc._channel_pr_read and "T1:C1" in h.svc._again


def test_a_rebind_mid_refresh_re_reads_the_pr_on_a_board_only_rerun(tmp_path, repo):
    snap = model.BoardSnapshot(rows=(_row("it_00000001", model.WORKING),))
    h = _Harness(tmp_path, snapshot=snap)
    binding = _FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    rebound = {"done": False}

    async def publish_board(b, s):
        if not rebound["done"]:
            rebound["done"] = True
            current = h.svc.store.get("T1", "C1")
            current.branch = "kirocrew/rebound"
            h.svc.store.put(current)

    h.svc._publish_board = publish_board

    async def go():
        h.svc.schedule_refresh(binding, delay=0.01)  # board-only: no forced PR read
        await h.svc.drain()

    asyncio.run(go())
    # The PR is read off the checkout's branch: the board-only rerun reads it
    # again instead of reusing the old binding's read for a minute.
    assert len(h.pr_queries) == 2 and rebound["done"]


def test_a_pass_absorbs_the_debounce_of_triggers_that_arrived_before_it(tmp_path, repo):
    h = _Harness(tmp_path, snapshot=model.BoardSnapshot(rows=(_row("it_00000001"),)))
    binding = _FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    in_body = {"seen": False}

    class _Delays(dict):
        """The rerun debounce store, recording what each rerun after the body waits."""

        after_body: list[float] = []

        def pop(self, key, default=None):
            value = super().pop(key, default)
            if in_body["seen"]:
                self.after_body.append(value)
            return value

    h.svc._again_delay = _Delays()

    async def publish_board(b, s):
        if not in_body["seen"]:
            in_body["seen"] = True
            h.svc.schedule_refresh(binding)  # a conductor turn ends mid-pass: no debounce

    h.svc._publish_board = publish_board

    async def go():
        h.svc.schedule_refresh(binding, delay=0.01)
        h.svc.schedule_refresh(binding, delay=service.BOARD_DEBOUNCE_SECS)  # in the sleep
        await h.svc.drain()

    asyncio.run(go())
    # The first pass already read what the second trigger asked for, so the rerun
    # the mid-pass turn end queued does not wait out that debounce.
    assert in_body["seen"] and service.BOARD_DEBOUNCE_SECS not in _Delays.after_body


def test_the_folder_watcher_reads_a_session_first_seen_running_as_a_start(tmp_path, monkeypatch):
    # Board filing moves a running worker into an already-watched folder.
    scheduled = _watch_scheduled(tmp_path, monkeypatch, [{}, {"chat-w1": (True, 1)}])
    assert scheduled == [service.BOARD_DEBOUNCE_SECS]


def test_an_end_and_a_start_in_one_pass_schedule_one_full_refresh(tmp_path, monkeypatch):
    scheduled = _watch_scheduled(
        tmp_path,
        monkeypatch,
        [{"a": (True, 1), "b": (False, 0)}, {"a": (False, 1), "b": (True, 1)}],
    )
    assert scheduled == [0.0]


# ── "waiting on you: approve <tool>" ────────────────────────────────────────


def _approval_board(monkeypatch, kind, *, title="Running: deploy --host prod-db.internal"):
    from kiro_crew.dashboard.session_control_parts import session_status

    def fields(state, slot):
        if slot.key != "chat-w1":
            return {}
        return {"pending_approval": True, "pending_approval_tool": title}

    monkeypatch.setattr(session_status, "_pending_approval_fields", fields)
    items = [
        _Item("it_00000001", title="Fix #21964", status="progress", worker_session_key="chat-w1"),
        _Item("it_00000002", status="progress", worker_session_key="chat-w2"),
    ]
    source.set_records_reader(_reader(lambda key: (SimpleNamespace(goal="g", round=1), items)))
    source.set_scope_predicate(lambda state, binding: True)
    waiting = _slot("chat-w1", running=True)
    # The slot projection's card fields: the title is the transcript's permission row.
    waiting.to_dict = lambda: {
        "pending_approval": True,
        "pending_approval_info": {"tool": title, "tool_kind": kind, "tool_input": "{}"},
    }
    state = _State({"chat-w1": waiting, "chat-w2": _slot("chat-w2", running=True)})
    snap = asyncio.run(source.read_board(state, _binding()))
    assert snap is not None
    return snap


def test_a_pending_approval_row_says_what_it_asks_and_counts_as_waiting_on_you(monkeypatch):
    snap = _approval_board(monkeypatch, "execute")
    first, second = snap.rows
    assert first.state == model.WAITING_ON_YOU and first.approval_tool == "a command"
    assert second.approval_tool == ""  # no approval pending: nothing on the row
    assert "*Waiting on you:* approve" in render.row_text(first, _link)
    assert "1 waiting on you" in snap.aggregate and "1 working" in snap.aggregate
    assert render.needs_you_text(first, "U1", _link).count("approve a command") == 1


def test_the_approval_command_is_shown_redacted_and_only_on_the_board(monkeypatch):
    # Kyle's design shows the call a pending approval asks for, redacted first.
    key = "AKIA" + "ABCDEFGHIJKLMNOP"
    home = os.path.expanduser("~")
    snap = _approval_board(
        monkeypatch, "execute", title=f"Running: deploy --key {key} {home}/secrets/x.txt"
    )
    first = snap.rows[0]
    detail = json.dumps(render.plan_block(snap, _link, now=_NOW)["tasks"][0]["details"])
    assert "approve " in detail and "deploy --key" in detail and key not in detail
    assert home not in detail and "~/secrets/x.txt" in detail
    # The page, the outcome and the bar never carry the command.
    others = [
        render.needs_you_text(first, "U1", _link),
        render.outcome_line(first),
        " ".join(i["label"] for i in _bar_of(snap)),
    ]
    assert not any("deploy" in text for text in others)


def _bar_of(snap):
    from kiro_crew.slack.code_channels.workspace.context_bar import BarState, build_items

    return build_items(BarState(board=snap))


@pytest.mark.parametrize(
    ("kind", "words"),
    [("edit", "a file edit"), ("fetch", "a web fetch"), ("spawn", "a sub-agent"), ("other", "")],
)
def test_the_approval_phrase_is_a_closed_vocabulary(monkeypatch, kind, words):
    snap = _approval_board(monkeypatch, kind)
    assert snap.rows[0].approval_tool == words


def test_an_approval_of_an_unknown_kind_still_reads_as_waiting_on_you(monkeypatch):
    snap = _approval_board(monkeypatch, "rm -rf / <!here>")
    assert snap.rows[0].approval_tool == ""
    assert model.row_label(snap.rows[0]) == "waiting on you: approve a tool"


# ── first-dispatch edges ────────────────────────────────────────────────────


def test_a_new_binding_forgets_the_unbound_ledger_keys(monkeypatch):
    from kiro_crew.slack.code_channels.workspace import core as ws_core

    board_pkg._unbound["chat-cond"] = time.monotonic()

    async def ensure(binding):
        return None

    monkeypatch.setattr(ws_core, "_SERVICE", SimpleNamespace(ensure=ensure))
    monkeypatch.setattr(ws_core, "start_watch", lambda: None)
    asyncio.run(ws_core.prepare(_binding()))
    assert board_pkg._unbound == {}


def test_a_scope_refused_for_an_unread_audience_retries_once(monkeypatch):
    from kiro_crew.slack.code_channels import audience

    later: list[tuple[float, object, tuple]] = []
    monkeypatch.setattr(audience, "channel_shared", lambda channel: None)

    async def go():
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "call_later", lambda d, fn, *a: later.append((d, fn, a)))
        board_pkg._retry_once_audience_is_read(_binding("C7"))
        board_pkg._retry_once_audience_is_read(_binding("C7"))  # within the window: no second

    asyncio.run(go())
    assert later == [(board_pkg._AUDIENCE_RETRY_SECS, board_pkg._schedule, ("C7",))]


def test_a_known_audience_or_an_archived_channel_does_not_retry(monkeypatch):
    from kiro_crew.slack.code_channels import audience

    later: list = []

    async def go(binding):
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "call_later", lambda *a: later.append(a))
        board_pkg._retry_once_audience_is_read(binding)

    monkeypatch.setattr(audience, "channel_shared", lambda channel: False)
    asyncio.run(go(_binding("C7")))
    monkeypatch.setattr(audience, "channel_shared", lambda channel: None)
    asyncio.run(go(dataclasses.replace(_binding("C8"), archived_at=1.0)))
    assert later == []
