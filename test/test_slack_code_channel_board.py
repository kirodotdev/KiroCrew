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
from slack_sdk.errors import SlackApiError

from kiro_crew.dashboard.handlers import worktree as wt
from kiro_crew.slack import features
from kiro_crew.slack.code_channels import binding as cc_binding
from kiro_crew.slack.code_channels import board as board_pkg
from kiro_crew.slack.code_channels import surface, surface_store
from kiro_crew.slack.code_channels.board import filing, model, publisher, render, source
from kiro_crew.slack.code_channels.store import Binding
from kiro_crew.slack.code_channels.workspace import git_ops, service
from kiro_crew.slack.code_channels.workspace.pr import PrLookup
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


class _LiveConfig:
    """The feature on, every channel at its default activation (tests set ``off``)."""

    def __init__(self, activations: dict[str, str] | None = None) -> None:
        self.slack = SimpleNamespace(code_channels=True)
        self.activations = activations or {}

    def channel_config(self, channel_id: str) -> SimpleNamespace:
        return SimpleNamespace(activation=self.activations.get(channel_id, ""))


@pytest.fixture(autouse=True)
def _feature_on(_floor_monkeypatch):
    features.reset_capabilities()
    features.record_granted_scopes(["chat:write", "code_channels:manage"])
    _floor_monkeypatch.setattr(
        cc_binding,
        "live_config",
        lambda orch=None: _LiveConfig(),
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
def publisher_clock(_floor_monkeypatch):
    """Publishes 30s apart: past the edit gap, inside the stale-stamp window."""
    clock = StepClock()
    _floor_monkeypatch.setattr(publisher, "_clock", clock)
    # A page's own read peeks at this clock without stepping it, so a pass that
    # pages still counts as one 30 s step; a test that installs its own clock
    # gets that clock's reading.
    _floor_monkeypatch.setattr(
        publisher,
        "_page_time",
        lambda: clock.now if publisher._clock is clock else publisher._clock(),
    )
    _floor_monkeypatch.setattr(publisher, "_paged", {})  # page times are on this clock
    _floor_monkeypatch.setattr(publisher, "_not_before", {})  # so are retry deadlines
    _floor_monkeypatch.setattr(board_pkg, "_unsent", {})  # one test's re-arms are its own
    _floor_monkeypatch.setattr(board_pkg, "_timers", {})
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
    assert model.aggregate_line([_row(state=model.CLOSED)]) == "1 closed · 0 of 1 done"
    assert model.overall_state([_row(state=model.DONE), _row(state=model.CLOSED)]) == "done"
    assert model.overall_state([_row(state=model.LOST), _row(state=model.IDLE)]) == "stuck"
    assert model.overall_state([]) == model.OVERALL_EMPTY
    assert model.aggregate_line([]) == "No helper agents yet"


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
    assert first.session_key == "chat-w1"  # the reported PR number is not shown
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
        "session_key",
        "report_stamp",
        "round",
        "waiting_on",
        "approval_tool",
        "closed_as",
        "step",
        "turn_started_at",
        "approval_key",  # a digest only for the alert token, never rendered
    }


def test_read_board_rechecks_scope_after_the_ledger_read():
    calls = iter([True, False])
    source.set_scope_predicate(lambda state, binding: next(calls))
    source.set_records_reader(_reader(lambda key: (SimpleNamespace(goal="g", round=1), [])))
    assert asyncio.run(source.read_board(_State(), _binding())) is None


@pytest.fixture
def bound_store(tmp_path, monkeypatch):
    """A channel store holding the channel's binding, as a bound channel has."""
    from kiro_crew.slack.code_channels import store as cc_store

    real = cc_store.CodeChannelStore(tmp_path / "slack-code-channels-scope.json")
    real.put_binding(_binding())
    monkeypatch.setattr(cc_store, "_STORE", real)
    return real


def test_default_scope_refuses_what_the_dispatch_exemption_would_not_publish(
    monkeypatch, bound_store
):
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


def test_default_scope_requires_the_live_bound_slot_and_no_archive(monkeypatch, bound_store):
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


@pytest.mark.parametrize("how", ["dropped", "rebound", "re-anchored", "archived"])
def test_default_scope_refuses_a_binding_the_store_no_longer_holds(monkeypatch, bound_store, how):
    """A caller's binding copy, kept across awaits, is checked against the store now.

    The old slot's Slack link is still in place (``bound_slot`` answers it), so
    only the store says the channel was unbound or bound to another session.
    """
    live = SimpleNamespace(key="chat-cond", _slack_channel="C1")
    monkeypatch.setattr(cc_binding, "bound_slot", lambda state, b: live)
    monkeypatch.setattr(source, "_publish_refusal", lambda state, b: "")
    held = _binding()
    assert source.scope_admits(object(), held)
    if how == "dropped":
        bound_store.drop_binding("C1")
    elif how == "rebound":
        bound_store.replace_binding(_binding(session="chat-other"))
    elif how == "re-anchored":
        bound_store.replace_binding(dataclasses.replace(_binding(), anchor_ts="2.0"))
    else:
        bound_store.update_binding("C1", archived_at=1.0)
    assert not source.scope_admits(object(), held)


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
    # The plan's title is the summary line, shown once (no bold section above it).
    context, plan = blocks
    assert context["elements"][0]["text"].startswith("Work board · updated 10:00 UTC · <https://")
    assert "Open the lead session in the dashboard" in context["elements"][0]["text"]
    assert plan["type"] == "plan" and plan["title"] == "1 working · 1 waiting on you · 0 of 2 done"
    first, second = plan["tasks"]
    assert first["task_id"] == "it_00000001" and first["title"] == "Fix login"
    # Working keeps the spinner; the row waiting on the person gets the empty circle.
    assert first["status"] == "in_progress" and second["status"] == "pending"
    assert "output" not in first and "sources" not in first  # no PR on the board
    assert second["title"] == "Waiting on you: Pick an icon"
    waiting = second["details"]["elements"][0]["elements"]
    # The row that needs the owner links to where they act on it; the others don't.
    waiting_row = _snapshot().rows[1]
    assert waiting_row.state == model.WAITING_ON_YOU and waiting_row.session_key
    assert waiting[-1] == {
        "type": "link",
        "url": _link(waiting_row.session_key),
        "text": "Open in dashboard",
    }
    working = first["details"]["elements"][0]["elements"]
    assert not any(e.get("type") == "link" for e in working)
    assert render.fallback_text(_snapshot()) == (
        "Work board: 1 working · 1 waiting on you · 0 of 2 done"
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


# ── publisher ───────────────────────────────────────────────────────────────


class _SlackResponse(dict):
    """What ``slack_sdk`` hands a ``SlackApiError``: the body, a status and headers."""

    def __init__(self, code: str, status: int, headers: dict | None = None) -> None:
        super().__init__(ok=False, error=code)
        self.status_code = status
        self.headers = headers or {}


#: The HTTP status Slack answers each error code with: a rate limit is a 429, an
#: outage a 5xx, and every other refusal an ``ok: false`` body on a 200.
_STATUS = {"ratelimited": 429, "service_unavailable": 503}


class _SlackError(SlackApiError):
    def __init__(self, code: str, *, status: int | None = None, headers: dict | None = None):
        response = _SlackResponse(code, status or _STATUS.get(code, 200), headers)
        super().__init__(code, response)


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


def test_needs_you_is_threaded_once_per_waiting_spell_with_the_owner_mentioned(slack):
    _publish(_snapshot())
    replies = slack.of("post_message")
    assert len(replies) == 1
    _, channel, text, thread_ts = replies[0]
    assert channel == "C1" and thread_ts == "100.1" and text.startswith("<@U0OWNER>")
    _publish(model.BoardSnapshot(goal="moved", round=2, rows=_snapshot().rows))
    assert len(slack.of("post_message")) == 1  # an edit never pages twice
    asked_again = list(_snapshot().rows)
    asked_again[1] = replace(asked_again[1], report_stamp="2026-10-18T11:00:00Z")
    _publish(model.BoardSnapshot(goal="moved", round=2, rows=tuple(asked_again)))
    assert len(slack.of("post_message")) == 1  # a repeated report in the same spell
    answered = list(asked_again)
    answered[1] = replace(answered[1], state=model.WORKING)
    _publish(model.BoardSnapshot(goal="moved", round=2, rows=tuple(answered)))  # spell ends
    _publish(model.BoardSnapshot(goal="moved", round=2, rows=tuple(asked_again)))
    assert len(slack.of("post_message")) == 2  # a NEW waiting spell pages again


def test_an_item_is_paged_no_sooner_than_the_gap_after_its_last_page(slack, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(publisher, "_clock", lambda: now[0])
    waiting = _snapshot().rows
    working = (waiting[0], replace(waiting[1], state=model.WORKING))
    _publish(_snapshot())
    _publish(model.BoardSnapshot(rows=working))  # answered: the spell ends
    now[0] += publisher.PAGE_GAP_SECS - 1
    _publish(model.BoardSnapshot(rows=waiting))  # waiting again, inside the gap
    assert len(slack.of("post_message")) == 1
    assert 0 < publisher.take_retry("C1") <= 1  # the next pass sends it
    now[0] += 1
    _publish(model.BoardSnapshot(rows=waiting))
    assert len(slack.of("post_message")) == 2


def test_the_page_gap_runs_from_the_page_not_from_the_start_of_the_pass(slack, monkeypatch):
    """GPT r54: a 30 s board post counted toward the gap, so two pages came 30 s apart."""
    now = [1000.0]
    monkeypatch.setattr(publisher, "_clock", lambda: now[0])
    real_post = slack.post_blocks

    async def slow_post(*a, **k):
        now[0] += 30  # Slack takes 30 s to take the board
        return await real_post(*a, **k)

    monkeypatch.setattr(slack, "post_blocks", slow_post)
    waiting = _snapshot().rows
    working = (waiting[0], replace(waiting[1], state=model.WORKING))
    _publish(_snapshot())  # paged at 1030
    _publish(model.BoardSnapshot(rows=working))  # answered: the spell ends
    now[0] = 1000.0 + publisher.PAGE_GAP_SECS  # 30 s after the page
    _publish(model.BoardSnapshot(rows=waiting))  # waiting again
    assert len(slack.of("post_message")) == 1


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
    origin_channel_id: str = ""  # a real Binding always carries it


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
        # The fake reader's board was "read under the scope check": the refresh's
        # re-check admits it too. A test of a refusal installs its own predicate.
        source.set_scope_predicate(lambda state, binding: True)
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

        async def read_board(binding):
            return self.snapshot

        async def publish_board(binding, snap):
            self.published.append(snap)
            return "posted"

        self.svc = service.WorkspaceService(
            lambda: self.client,
            store=WorkspaceStore(tmp_path / "ws.json"),
            set_project=set_project,
            find_pr=find_pr,
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


def test_no_board_keeps_the_plain_single_diff(tmp_path, repo):
    h = _Harness(tmp_path, snapshot=None)
    binding = _FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    (Path(rec.worktree) / "README.md").write_text("changed\n")
    asyncio.run(h.svc.refresh(binding))
    content = h.client.last("set_diff_view")["content"]
    assert content.startswith("diff --git") and "====" not in content
    assert h.published == []


# ── wrap-up, filing, triggers ───────────────────────────────────────────────


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


def _store_binding(tmp_path, monkeypatch, binding=None):
    """A real code-channel store holding the channel's binding (the watch reads it)."""
    from kiro_crew.slack.code_channels import store as cc_store

    real = cc_store.CodeChannelStore(tmp_path / "slack-code-channels-bound.json")
    real.load()
    monkeypatch.setattr(cc_store, "_STORE", real)
    real.put_binding(binding or _binding())
    return real


def _watch_scheduled(tmp_path, monkeypatch, readings_list, *, board=True):
    from kiro_crew.slack.code_channels.workspace import folders
    from kiro_crew.slack.code_channels.workspace.store import WorkspaceRecord

    h = _Harness(tmp_path)
    _store_binding(tmp_path, monkeypatch)
    source.set_scope_predicate(lambda state, binding: True)  # the board's channel
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
    monkeypatch.setattr(folders, "sessions_in", lambda rows, ids, worktree="": next(readings))
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


def test_a_rerun_refreshes_the_newest_triggers_binding(tmp_path, repo, monkeypatch):
    """A rebind while a refresh loop runs: the reruns refresh the new binding.

    The scope check refuses the first trigger's binding once the store holds a
    new one, so a loop that kept refreshing it would drop every rerun.
    """
    h = _Harness(tmp_path)
    old = _FakeBinding(project_dir=str(repo))
    new = dataclasses.replace(old, session_key="chat-cond-2")
    refreshed: list[str] = []

    async def fake_refresh(binding, *, recheck_pr=True):
        refreshed.append(binding.session_key)
        if len(refreshed) == 1:
            h.svc.schedule_refresh(new)  # rebound mid-pass: a rerun is queued

    monkeypatch.setattr(h.svc, "refresh", fake_refresh)

    async def go():
        h.svc.schedule_refresh(old)
        await h.svc.drain()

    asyncio.run(go())
    assert refreshed == [old.session_key, "chat-cond-2"]
    assert h.svc._latest_binding == {}


def test_a_refresh_pass_reads_the_channels_binding_from_the_store(
    tmp_path, repo, monkeypatch, bound_store
):
    """A late trigger carrying the old copy (a turn that started before the rebind).

    Each pass refreshes the binding the store holds now, so it is not refused.
    """
    h = _Harness(tmp_path)
    bound_store.replace_binding(_binding(session="chat-cond-2"))
    refreshed: list[str] = []

    async def fake_refresh(binding, *, recheck_pr=True):
        refreshed.append(binding.session_key)

    monkeypatch.setattr(h.svc, "refresh", fake_refresh)

    async def go():
        h.svc.schedule_refresh(_FakeBinding(project_dir=str(repo)))  # the old copy
        await h.svc.drain()

    asyncio.run(go())
    assert refreshed == ["chat-cond-2"]


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


def _approval_board(
    monkeypatch,
    kind,
    *,
    title="Running: deploy --host prod-db.internal",
    project="",
    tool_input="{}",
):
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
    waiting.project = project
    # The slot projection's card fields: the title is the transcript's permission row.
    waiting.to_dict = lambda: {
        "pending_approval": True,
        "pending_approval_info": {"tool": title, "tool_kind": kind, "tool_input": tool_input},
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


def _approval_call(snap) -> str:
    """The pending approval's call as published: the code element of the row's details."""
    (section,) = render.plan_block(snap, _link, now=_NOW)["tasks"][0]["details"]["elements"]
    return next(e["text"] for e in section["elements"] if e.get("style", {}).get("code"))


def test_the_approval_command_is_shown_redacted_and_only_on_the_board(monkeypatch):
    # Kyle's design shows the call a pending approval asks for, redacted first.
    key = "AKIA" + "ABCDEFGHIJKLMNOP"
    home = os.path.expanduser("~")
    snap = _approval_board(
        monkeypatch, "execute", title=f"Running: deploy --key {key} {home}/secrets/x.txt"
    )
    first = snap.rows[0]
    call = _approval_call(snap)
    assert call.startswith("deploy --key ") and key not in call
    assert home not in call and call.endswith(f" {model._home_token()}/…/x.txt")
    # The page never carries the command.
    assert "deploy" not in render.needs_you_text(first, "U1", _link)


def test_a_windows_home_in_the_approval_command_is_shown_as_the_token(monkeypatch):
    # windows-latest CI: the home token's tail was cut again by the POSIX rule.
    win_home = "D:\\Work\\ci-user"
    real = model.os.path.realpath
    monkeypatch.setattr(model.os.path, "expanduser", lambda p: win_home if p == "~" else p)
    monkeypatch.setattr(model.os.path, "realpath", lambda p: p if p == win_home else real(p))
    monkeypatch.setattr(model, "_home_token", lambda os_name=None: "%USERPROFILE%")
    snap = _approval_board(
        monkeypatch, "execute", title=f"Running: deploy {win_home}/secrets/x.txt {win_home}\\a.txt"
    )
    assert _approval_call(snap) == "deploy %USERPROFILE%/…/x.txt %USERPROFILE%\\…\\a.txt"


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
        monkeypatch.setattr(
            loop, "call_later", lambda d, fn, *a, context=None: later.append((d, fn, a, context))
        )
        board_pkg._retry_once_audience_is_read(_binding("C7"))
        board_pkg._retry_once_audience_is_read(_binding("C7"))  # within the window: no second

    asyncio.run(go())
    [(delay, fn, args, context)] = later
    assert (delay, fn, args) == (board_pkg._AUDIENCE_RETRY_SECS, board_pkg._schedule, ("C7",))
    assert context is not None and len(context) == 0  # armed outside the caller's fence


def test_a_known_audience_or_an_archived_channel_does_not_retry(monkeypatch):
    from kiro_crew.slack.code_channels import audience

    later: list = []

    async def go(binding):
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "call_later", lambda *a, **k: later.append(a))
        board_pkg._retry_once_audience_is_read(binding)

    monkeypatch.setattr(audience, "channel_shared", lambda channel: False)
    asyncio.run(go(_binding("C7")))
    monkeypatch.setattr(audience, "channel_shared", lambda channel: None)
    asyncio.run(go(dataclasses.replace(_binding("C8"), archived_at=1.0)))
    assert later == []
