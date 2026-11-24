"""A Conductor thread's worker status board (thread-conductor RFC, part C).

The board of a Conductor thread is the code-channel work board posted INTO the
thread: one message, edited in place, one row per worker (title, state, PR and
CI), the worker's details left in its dashboard chat. It needs none of the
code-channel scopes (no pin, no tabs, no ``agents.*`` call) and runs only while
``slack.thread_conductor`` is on. The pure render and the publisher run against a
recorded fake Slack; the scope, the store and the refresh run for real.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from test_session_control_owner_dm import _state

from kiro_crew.dashboard.session_control_parts import code_channel_exemption as cce
from kiro_crew.slack import handler as slack_handler
from kiro_crew.slack.code_channels import audience
from kiro_crew.slack.code_channels import binding as cc_binding
from kiro_crew.slack.code_channels import board as board_pkg
from kiro_crew.slack.code_channels import surface, surface_store
from kiro_crew.slack.code_channels.board import (
    links,
    model,
    publisher,
    render,
    source,
    thread_board,
)
from kiro_crew.slack.code_channels.models import ChannelInfo
from kiro_crew.slack.code_channels.store import (
    ROOM_PRIVATE,
    Binding,
    CodeChannelStore,
    set_store,
    thread_key,
)

#: Part A's own on-check, read before the fixtures replace it.
_REAL_ON = cc_binding.thread_conductor_on
OWNER = "U0OWNER0001"
TEAM = "T0TEAM00001"
CHANNEL = "G0PRIVATE01"
ANCHOR = "1700000000.000100"
OTHER_ANCHOR = "1700000000.000200"
FOLDER = "fold-thread"
#: What a worker said or ran: none of it may reach the thread.
SECRET_SUMMARY = "SECRET-SUMMARY wired the admin password into config"
SECRET_STEP = "cat /srv/secret-notes.txt"
SECRET_BRANCH = "feat/secret-branch"
DASHBOARD = "https://dash.example/chat/"


class _Slack:
    """A recording Slack: every call the board may make."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.fail: dict[str, str] = {}
        self._n = 0

    def _maybe_fail(self, method: str) -> None:
        code = self.fail.pop(method, "")
        if code:
            from test_slack_code_channel_board import _SlackError

            raise _SlackError(code)

    async def post_blocks(self, channel, blocks, text, thread_ts=None):
        self._maybe_fail("post_blocks")
        self._n += 1
        self.calls.append(("post_blocks", channel, thread_ts, blocks, text))
        return f"100.{self._n}"

    async def update_message(self, channel, ts, text="", blocks=None):
        self.calls.append(("update_message", channel, ts, blocks, text))
        self._maybe_fail("update_message")

    async def post_message(self, channel, text, thread_ts=None):
        self.calls.append(("post_message", channel, thread_ts, text))
        return "200.1"

    async def add_pin(self, channel, ts):
        self.calls.append(("add_pin", channel, ts))

    async def get_permalink(self, channel, ts):
        self.calls.append(("get_permalink", channel, ts))
        return f"https://slack.example/{ts}"

    def of(self, method: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == method]

    def wrote(self) -> list[tuple]:
        return [c for c in self.calls if c[0] != "get_permalink"]


@dataclass
class _Item:
    item_id: str
    title: str = "Fix the login"
    state: str = "open"
    status: str | None = "progress"
    reason: str | None = None
    summary: str = SECRET_SUMMARY
    artifacts: dict = field(default_factory=lambda: {"branch": SECRET_BRANCH})
    pr: int | None = None
    worker_session_key: str | None = None
    last_report_at: str | None = "2026-10-18T10:00:00Z"
    created_at: str = ""
    round: int = 1


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        self.now += 30.0
        return self.now


def _cfg() -> Any:
    return SimpleNamespace(
        slack=SimpleNamespace(code_channel_dispatch=False),
        channel_config=lambda channel: SimpleNamespace(activation=""),
    )


class _World:
    """The Conductor thread, its dashboard slots and its ledger, with the switch."""

    def __init__(self, tmp_path, monkeypatch, slack: _Slack) -> None:
        self.monkeypatch = monkeypatch
        self.slack = slack
        self.state = _state(tmp_path, monkeypatch)
        self.state._folders = [{"id": FOLDER, "name": "Conductor threads", "parent_id": ""}]
        self.store = CodeChannelStore(path=tmp_path / "slack-code-channels.json")
        self.store.load()
        set_store(self.store)
        self.items: list[_Item] = []
        self.live: dict[str, str] = {}
        self.cfg = _cfg()
        self.switch = True
        self.public = False
        # The gate is part A's on-check (the config key behind it is not read here).
        monkeypatch.setattr(cc_binding, "thread_conductor_on", lambda cfg: self.switch)
        monkeypatch.setattr(cc_binding, "thread_public_rooms_on", lambda cfg: self.public)
        monkeypatch.setattr(cce, "_live_config", lambda: self.cfg)
        monkeypatch.setattr(cc_binding, "live_config", lambda orch=None: self.cfg)
        monkeypatch.setattr(surface, "dashboard_state", lambda: self.state)
        record = SimpleNamespace(goal="Ship the login flow", round=1)

        async def _records(_key):
            return record, list(self.items)

        source.set_records_reader(_records)
        monkeypatch.setattr(
            source, "worker_live", lambda state, key: (self.live.get(key, "running"), "")
        )
        self.conductor, self.binding = self.thread(ANCHOR)

    def thread(self, anchor: str, channel: str = CHANNEL):
        slot = self.state.get_or_create_slot(f"chat-conductor-{anchor[-3:]}")
        slot.folder_id = FOLDER
        self.state.link_slack(slot.key, anchor, channel)
        audience.note_channel_info(
            ChannelInfo(
                channel_id=channel,
                is_code_channel=False,
                is_private=True,
                shared=False,
                archived=False,
            )
        )
        binding = self.store.put_thread_binding(
            Binding(
                channel_id=channel,
                team_id=TEAM,
                session_key=slot.key,
                anchor_ts=anchor,
                thread=True,
                room=ROOM_PRIVATE,
            )
        )
        return slot, binding

    def worker(self, item_id: str, **kw) -> _Item:
        key = f"chat-{item_id}"
        self.state.get_or_create_slot(key)
        item = _Item(item_id, worker_session_key=key, **kw)
        self.items.append(item)
        return item

    def refresh(self, binding=None) -> None:
        asyncio.run(thread_board.refresh(binding or self.binding))


@pytest.fixture(autouse=True)
def _enabled(_floor_monkeypatch, tmp_path):
    """Code channels OFF, the thread runtime built and switched on, Slack recorded."""
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setattr(cc_binding, "code_channels_on", lambda cfg: False)
    _floor_monkeypatch.setattr(cc_binding, "THREAD_CONDUCTOR_RUNTIME_BUILT", True)
    _floor_monkeypatch.setattr(surface, "workspace_admitted", lambda team: team == TEAM)
    _floor_monkeypatch.setattr(slack_handler, "is_owner", lambda user: user == OWNER)
    _floor_monkeypatch.setattr(links, "link_builder", lambda: lambda key: f"{DASHBOARD}{key}")
    clock = _Clock()
    _floor_monkeypatch.setattr(publisher, "_clock", clock)
    _floor_monkeypatch.setattr(publisher, "_page_time", lambda: clock.now)
    surface_store.set_store(surface_store.SurfaceStore(tmp_path / "surface.json"))
    audience.clear()
    cce.forget_senders()
    yield
    source.set_records_reader(None)
    source.set_scope_predicate(None)
    board_pkg.reset_for_tests()
    set_store(None)
    surface_store.set_store(None)
    audience.clear()
    cce.forget_senders()


@pytest.fixture
def slack(_floor_monkeypatch):
    fake = _Slack()
    _floor_monkeypatch.setattr(surface, "slack", lambda: fake)

    async def _true():
        return True

    _floor_monkeypatch.setattr(surface, "outbound_permitted", _true)
    _floor_monkeypatch.setattr(surface, "audit", lambda *a, **k: None)
    return fake


@pytest.fixture
def world(tmp_path, monkeypatch, slack):
    return _World(tmp_path, monkeypatch, slack)


def _text(calls: list[tuple]) -> str:
    """Everything the board sent, as one string (blocks and text)."""
    return json.dumps([c[3:] for c in calls], default=str)


# ── posts in the thread, updates in place ─────────────────────────────────


def test_two_workers_post_one_message_in_the_thread_with_a_row_each(world, slack):
    world.worker("it_00000001", title="Fix the login")
    world.worker("it_00000002", title="Write the docs", pr=12)
    world.refresh()

    posts = slack.of("post_blocks")
    assert len(posts) == 1 and slack.wrote() == posts
    _, channel, thread_ts, blocks, _text_ = posts[0]
    assert (channel, thread_ts) == (CHANNEL, ANCHOR)  # INTO the thread, not top level
    plan = next(b for b in blocks if b["type"] == "plan")
    assert [t["title"] for t in plan["tasks"]] == ["Fix the login", "Write the docs"]
    # The board's pointer is kept under the thread's own key.
    record = surface_store.store().get(thread_key(CHANNEL, ANCHOR))
    assert record is not None and record.board_ts == "100.1"
    assert surface_store.store().get(CHANNEL) is None


def test_a_state_change_edits_the_same_message_and_posts_nothing_new(world, slack):
    world.worker("it_00000001")
    world.refresh()
    world.live["chat-it_00000001"] = "idle"
    world.items[0].status = "done"
    world.refresh()

    assert len(slack.of("post_blocks")) == 1
    edits = slack.of("update_message")
    assert [(c[1], c[2]) for c in edits] == [(CHANNEL, "100.1")]
    assert slack.of("post_message") == []


def test_an_unchanged_board_writes_nothing(world, slack):
    world.worker("it_00000001")
    world.refresh()
    before = list(slack.calls)
    world.refresh()
    assert slack.calls == before


def test_the_board_is_never_pinned_and_asks_for_no_channel_surface(world, slack):
    world.worker("it_00000001")
    world.refresh()
    world.refresh()
    assert slack.of("add_pin") == []  # pins:write is a code-channel scope
    assert slack.of("get_permalink") == []


def test_two_threads_of_one_channel_keep_their_own_message(world, slack):
    world.worker("it_00000001")
    world.refresh()
    _slot, other = world.thread(OTHER_ANCHOR)
    world.refresh(other)

    posts = slack.of("post_blocks")
    assert [c[2] for c in posts] == [ANCHOR, OTHER_ANCHOR]  # one message each
    store = surface_store.store()
    assert store.get(thread_key(CHANNEL, ANCHOR)).board_ts == "100.1"
    assert store.get(thread_key(CHANNEL, OTHER_ANCHOR)).board_ts == "100.2"


# ── details stay in the dashboard ─────────────────────────────────────────


def _worker_with_secrets(world) -> None:
    world.worker("it_00000001", title="Fix the login", pr=7)
    world.monkeypatch.setattr(
        source,
        "worker_activity",
        lambda state, key: model.Activity(
            turn_started_at="2026-10-18T10:00:00Z",
            tool_title=f"Running: {SECRET_STEP}",
            tool_kind="execute",
        ),
    )


def test_a_row_shows_title_state_and_pr_and_hides_what_the_worker_said_or_ran(world, slack):
    _worker_with_secrets(world)
    world.refresh()

    # The precondition: the snapshot the board read DOES hold the details.
    snapshot = asyncio.run(source.read_board(world.state, world.binding))
    assert snapshot.rows[0].summary == model.safe_line(SECRET_SUMMARY, first_line=True)
    assert snapshot.rows[0].step and snapshot.rows[0].branch == SECRET_BRANCH
    sent = _text(slack.of("post_blocks"))
    for secret in ("SECRET-SUMMARY", "admin password", "secret-notes", SECRET_BRANCH):
        assert secret not in sent
    assert "Fix the login" in sent and "Working" in sent and "PR #7" in sent
    # ...and each row leads to the worker's dashboard chat, where the details are.
    assert f"{DASHBOARD}chat-it_00000001" in sent


@pytest.mark.parametrize("look", ["plan", "rows"])
def test_both_looks_hide_the_details_and_link_each_worker(look):
    row = model.WorkerRow(
        item_id="it_00000001",
        title="Fix the login",
        state=model.WORKING,
        summary=SECRET_SUMMARY,
        step=f"Running {SECRET_STEP}",
        branch=SECRET_BRANCH,
        session_key="chat-w1",
        pr_number=9,
        pr_url="https://github.com/acme/widgets/pull/9",
        pr_state="open",
        pr_ci="passing",
        pr_checks=(4, 6, 2),
    )
    snapshot = render.brief_snapshot(model.BoardSnapshot(goal="a goal", rows=(row,)))
    blocks = render.board_blocks(
        snapshot, lambda key: f"{DASHBOARD}{key}", look=look, brief=True, block_seed="x"
    )
    sent = json.dumps(blocks)
    for secret in ("SECRET-SUMMARY", "secret-notes", SECRET_BRANCH, "a goal"):
        assert secret not in sent
    assert f"{DASHBOARD}chat-w1" in sent and "PR #9" in sent and "CI 4/6 passing" in sent
    assert render.BRIEF_FOOTER in sent


def test_the_rows_look_is_one_line_with_the_title_linking_the_workers_chat():
    row = model.WorkerRow(
        item_id="it_00000001",
        title="Fix <the> login",
        state=model.WAITING_ON_YOU,
        session_key="chat-w1",
        summary=SECRET_SUMMARY,
        waiting_on="approval",
        approval_tool="run a command",
    )
    text = render.row_text(row, lambda key: f"{DASHBOARD}{key}", brief=True)
    assert "\n" not in text
    assert text.startswith(f"*<{DASHBOARD}chat-w1|Fix &lt;the&gt; login>*")
    assert "Waiting on you" in text and "SECRET" not in text and "run a command" not in text


def test_a_plan_block_slack_refuses_falls_back_to_the_rows_in_the_thread(world, slack):
    world.worker("it_00000001")
    slack.fail["post_blocks"] = "invalid_blocks"
    world.refresh()

    posts = slack.of("post_blocks")
    assert [c[2] for c in posts] == [ANCHOR]  # the retry is also in the thread
    assert not any(b["type"] == "plan" for b in posts[0][3])
    assert "Fix the login" in _text(posts)


def test_a_worker_waiting_on_the_owner_pages_them_in_the_thread_without_its_report(world, slack):
    world.worker("it_00000001", status="question", reason="needs_human")
    world.live["chat-it_00000001"] = "idle"
    world.state.owner_id = OWNER
    world.refresh()

    pages = slack.of("post_message")
    assert len(pages) == 1
    _, channel, thread_ts, text = pages[0]
    assert (channel, thread_ts) == (CHANNEL, ANCHOR)  # the thread, not the board message
    assert f"<@{OWNER}>" in text and "is waiting on you" in text
    assert "SECRET" not in text and f"{DASHBOARD}chat-it_00000001" in text
    world.refresh()
    assert len(slack.of("post_message")) == 1  # once per waiting spell


# ── only while the switch is on ───────────────────────────────────────────


def test_nothing_is_posted_while_the_switch_is_off(world, slack):
    world.worker("it_00000001")
    world.switch = False
    world.refresh()
    assert slack.calls == []
    assert surface_store.store().get(thread_key(CHANNEL, ANCHOR)) is None

    world.switch = True
    world.refresh()
    assert len(slack.of("post_blocks")) == 1


def test_nothing_is_posted_before_the_thread_runtime_is_built(world, slack, monkeypatch):
    world.worker("it_00000001")
    monkeypatch.setattr(cc_binding, "THREAD_CONDUCTOR_RUNTIME_BUILT", False)
    monkeypatch.setattr(cc_binding, "thread_conductor_on", _REAL_ON)  # the shipped check
    world.refresh()
    assert slack.calls == []


def test_an_ended_thread_gets_no_more_board(world, slack):
    world.worker("it_00000001")
    world.refresh()
    world.store.end_thread_binding(CHANNEL, ANCHOR)
    world.items[0].status = "done"
    world.refresh()
    assert len(slack.wrote()) == 1  # the one post; no edit after the end


def test_a_public_room_is_refused_without_the_opt_in(world, slack):
    world.worker("it_00000001")
    audience.note_channel_info(
        ChannelInfo(
            channel_id=CHANNEL,
            is_code_channel=False,
            is_private=False,
            shared=False,
            archived=False,
        )
    )
    world.refresh()
    assert slack.calls == []


# ── triggers ──────────────────────────────────────────────────────────────


def test_a_ledger_write_finds_the_thread_and_schedules_its_board(world, monkeypatch):
    scheduled: list[Any] = []
    monkeypatch.setattr(
        thread_board, "schedule", lambda binding, **kw: scheduled.append(binding) or True
    )
    board_pkg.note_ledger_write(world.conductor.key)
    assert [thread_key(b.channel_id, b.anchor_ts) for b in scheduled] == [
        thread_key(CHANNEL, ANCHOR)
    ]
    # A worker's own trigger finds it through the remembered room.
    assert board_pkg._conductor_channels[world.conductor.key] == thread_key(CHANNEL, ANCHOR)


def test_a_worker_turn_finds_its_conductors_thread_through_the_store(world, monkeypatch):
    scheduled: list[Any] = []
    monkeypatch.setattr(
        thread_board, "schedule", lambda binding, **kw: scheduled.append(binding) or True
    )
    worker = world.state.get_or_create_slot("chat-w-lineage")
    world.store.record_worker(worker.key, world.conductor.key)
    board_pkg.note_session_turn(worker.key)
    assert [b.anchor_ts for b in scheduled] == [ANCHOR]


def test_an_ended_thread_is_not_scheduled(world, monkeypatch):
    scheduled: list[Any] = []
    monkeypatch.setattr(
        thread_board, "schedule", lambda binding, **kw: scheduled.append(binding) or True
    )
    world.store.end_thread_binding(CHANNEL, ANCHOR)
    board_pkg.note_ledger_write(world.conductor.key)
    assert scheduled == []


def test_the_schedule_coalesces_a_burst_and_reruns_once_for_a_trigger_mid_pass(world, monkeypatch):
    world.worker("it_00000001")
    passes: list[int] = []

    async def counting(binding):
        passes.append(1)
        if len(passes) == 1:
            thread_board.schedule(binding, delay=0.01)  # a trigger while the pass runs
            thread_board.schedule(binding, delay=0.01)

    monkeypatch.setattr(thread_board, "refresh", counting)
    monkeypatch.setattr("kiro_crew.slack.code_channels.workspace.service.BOARD_DEBOUNCE_SECS", 0.01)

    async def burst():
        for _ in range(5):
            thread_board.schedule(world.binding, delay=0.01)
        await thread_board.drain()

    asyncio.run(burst())
    assert len(passes) == 2  # five triggers cost one pass; the ones mid-pass, one rerun


# ── a code channel's own board is unchanged ───────────────────────────────


def test_a_code_channels_board_is_still_posted_top_level_and_pinned(slack, monkeypatch):
    binding = Binding(channel_id="C1", team_id=TEAM, session_key="chat-cond", anchor_ts="1.0")
    snapshot = model.BoardSnapshot(
        rows=(model.WorkerRow(item_id="it_00000001", title="Task", state=model.WORKING),)
    )

    async def _not_archived(_binding):
        return False

    monkeypatch.setattr(surface, "is_archived", _not_archived)
    done = asyncio.run(
        publisher.publish(binding, snapshot, link_for=lambda key: "", owner_id=OWNER)
    )
    assert done == "posted"
    assert slack.of("post_blocks")[0][2] is None  # no thread
    assert len(slack.of("add_pin")) == 1
    assert surface_store.store().get("C1").board_ts == "100.1"  # keyed by the channel, as before
