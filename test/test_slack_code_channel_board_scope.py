"""The workspace publishes only what the channel admits, up to the write itself.

This PR's own board tests, kept apart from ``test_slack_code_channel_board_live``
(at the file-line cap): the bar, the Code tab and the resource re-check the
board's scope and the channel's activation after every await, right before each
write. The two live-suite tests this PR changes moved here whole. Shares that
module's fixtures and helpers (imported, so they apply here too).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import test_slack_code_channel_board as suite
import test_slack_code_channel_board_live as live

from kiro_crew.slack.code_channels import binding as cc_binding
from kiro_crew.slack.code_channels import surface
from kiro_crew.slack.code_channels.board import model, source
from kiro_crew.slack.code_channels.workspace import service

# The board suite's fixtures (autouse ones included) and helpers, bound here so
# they apply to this module too.
_no_sandbox = suite._no_sandbox
_feature_on = suite._feature_on
_surface_store = suite._surface_store
publisher_clock = suite.publisher_clock
slack = suite.slack
StepClock = suite.StepClock
_Harness = suite._Harness
_Item = suite._Item
_SlackError = suite._SlackError
_NOW = suite._NOW
_binding = suite._binding
_link = suite._link
_publish = suite._publish
_reader = suite._reader
_row = suite._row
_snapshot = suite._snapshot


# The live module's fixtures and helpers, bound here so they apply here too.
_Slots = live._Slots
_bar_labels = live._bar_labels
_scoped_binding = live._scoped_binding
_live_slot = live._live_slot
_watched = live._watched
lineage = live.lineage
repo = live.repo
_waiting_board = live._waiting_board


# ── This PR's own: the bar, the Code tab and the resource re-check the
# board's scope and the channel's activation right before each write ──


def _bar_keys(h):
    return [item["key"] for item in h.client.last("set_context_bar")["items"]]


def test_the_bar_asks_the_scope_after_its_last_await_before_sending(tmp_path, repo, monkeypatch):
    """The agent-item read is a thread hop: a refusal landing in it still keeps
    the Workers item off the bar that is sent."""
    from kiro_crew.slack.code_channels import agent_bar

    binding = _scoped_binding(repo)
    conductor = SimpleNamespace(key="chat-cond", _slack_channel="C1")
    monkeypatch.setattr(cc_binding, "bound_slot", lambda state, b: conductor)
    monkeypatch.setattr(surface, "dashboard_state", lambda: object())
    refusal = {"why": ""}
    monkeypatch.setattr(source, "_publish_refusal", lambda state, b: refusal["why"])
    h = _Harness(tmp_path)
    source.set_scope_predicate(None)  # the production scope check
    rec = asyncio.run(h.svc.ensure(binding))
    h.svc._boards[rec.key] = model.BoardSnapshot(
        rows=(_row(title="Fix login", session_key="chat-w1"),)
    )
    real_items = agent_bar.agent_items

    def read_then_refuse(channel_id):
        refusal["why"] = "the channel is shared with another organization"
        return real_items(channel_id)

    monkeypatch.setattr(agent_bar, "agent_items", read_then_refuse)
    asyncio.run(h.svc.republish_bar(binding))
    assert "workers" not in _bar_keys(h)
    assert rec.key not in h.svc._boards


def test_a_turn_starts_bar_never_re_sends_a_refused_cached_board(
    tmp_path, repo, monkeypatch, lineage
):
    """The turn start rebuilds the bar from its last state: the same scope check."""
    binding = _scoped_binding(repo)
    lineage.put_binding(binding)  # the bar reads the channel's binding as stored
    conductor = SimpleNamespace(key="chat-cond", _slack_channel="C1")
    monkeypatch.setattr(cc_binding, "bound_slot", lambda state, b: conductor)
    monkeypatch.setattr(surface, "dashboard_state", lambda: object())
    refusal = {"why": ""}
    monkeypatch.setattr(source, "_publish_refusal", lambda state, b: refusal["why"])
    h = _Harness(tmp_path)
    source.set_scope_predicate(None)  # the production scope check
    rec = asyncio.run(h.svc.ensure(binding))
    url = "https://github.com/acme/widgets/pull/7"
    snap = model.BoardSnapshot(
        rows=(
            _row(
                title="Fix login",
                session_key="chat-w1",
                pr_number=7,
                pr_url=url,
                pr_state="open",
            ),
        )
    )
    h.svc._boards[rec.key] = snap
    assert asyncio.run(h.svc.republish_bar(binding)) == ""
    items = h.client.last("set_context_bar")["items"]
    assert "workers" in _bar_keys(h) and any(i.get("url") == url for i in items)
    refusal["why"] = "the channel is shared with another organization"
    asyncio.run(h.svc.on_turn_start(binding))  # Stop changes the set: a real send
    items = h.client.last("set_context_bar")["items"]
    assert "stop" in _bar_keys(h)
    assert "workers" not in _bar_keys(h) and not any(i.get("url") == url for i in items)
    assert rec.key not in h.svc._boards


def _worker_tree(tmp_path, repo):
    tree = tmp_path / "wt-w"
    suite._git("worktree", "add", "-q", "-b", "feat/w", str(tree), "main", cwd=repo)
    (tree / "w.txt").write_text("worker change\n")
    suite._git("add", "w.txt", cwd=tree)
    suite._git("commit", "-q", "-m", "w", cwd=tree)
    return model.BoardSnapshot(rows=(_row(title="Fix login", branch="feat/w"),))


def _flip_at_the_write(h, admitted):
    """The scope changes the moment a write reaches for the client: after every
    check that ran before it, and before Slack sees anything."""
    client = h.client

    def at_write():
        admitted["now"] = False
        return client

    h.svc._client = at_write


def test_a_bar_whose_scope_flips_at_the_write_goes_out_without_workers(tmp_path, repo):
    board = model.BoardSnapshot(rows=(_row(title="Fix login", session_key="chat-w1"),))
    h = _Harness(tmp_path, snapshot=board)
    binding = suite._FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    h.svc._boards[rec.key] = board
    admitted = {"now": True}
    source.set_scope_predicate(lambda state, b: admitted["now"])
    _flip_at_the_write(h, admitted)
    stored = h.svc.store.get("T1", "C1")
    stored.bar_hash = ""
    h.svc._sent_bar.pop(rec.key, None)  # nothing sent yet: the resend is visible
    before = len([m for m, _ in h.client.calls if m == "set_context_bar"])
    asyncio.run(h.svc._publish_bar(stored, binding))
    sent = [kw["items"] for m, kw in h.client.calls if m == "set_context_bar"][before:]
    assert sent and all("workers" not in [i["key"] for i in items] for items in sent)
    assert rec.key not in h.svc._boards


def test_a_diff_whose_scope_flips_at_the_write_sends_only_the_channels_own(tmp_path, repo):
    board = _worker_tree(tmp_path, repo)
    h = _Harness(tmp_path, snapshot=board)
    binding = suite._FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    rec = h.svc.store.get("T1", "C1")
    (Path(rec.worktree) / "README.md").write_text("hello\nchannel edit\n")
    h.svc._boards[rec.key] = board
    admitted = {"now": True}
    source.set_scope_predicate(lambda state, b: admitted["now"])
    _flip_at_the_write(h, admitted)
    asyncio.run(h.svc._publish_diff(rec, binding))
    sent = [kw["content"] for m, kw in h.client.calls if m == "set_diff_view"]
    assert sent and all("worker change" not in c and "Fix login" not in c for c in sent)
    assert "+channel edit" in sent[-1]


def test_a_diff_resent_after_making_room_asks_the_scope_again(tmp_path, repo, monkeypatch):
    from kiro_crew.slack.code_channels import views
    from kiro_crew.slack.code_channels.models import CodeChannelApiError

    board = _worker_tree(tmp_path, repo)
    h = _Harness(tmp_path, snapshot=board)
    binding = suite._FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    rec = h.svc.store.get("T1", "C1")
    h.svc._boards[rec.key] = board
    admitted = {"now": True}
    source.set_scope_predicate(lambda state, b: admitted["now"])
    real_set = h.client.set_diff_view
    tries = {"n": 0}

    async def capped(channel_id, content, **kw):
        tries["n"] += 1
        if tries["n"] == 1:
            raise CodeChannelApiError("agents.conversations.setView", "too_many_views")
        return await real_set(channel_id, content, **kw)

    removal_admitted: list = []

    async def make_room(client, channel_id, *, for_key, admits=None):
        admitted["now"] = False  # the listViews round trip outlasts a change
        # The removal asks the write's fence too: it refuses now.
        removal_admitted.append((await admits()) if admits is not None else None)
        return "old"

    h.client.set_diff_view = capped
    monkeypatch.setattr(views, "make_room", make_room)
    asyncio.run(h.svc._publish_diff(rec, binding))
    sent = [kw["content"] for m, kw in h.client.calls if m == "set_diff_view"]
    assert tries["n"] == 1 and sent == []  # the resend with workers never went out
    assert removal_admitted == ["out_of_scope"]  # make_room was handed the fence
    assert rec.view_id == "" and rec.diff_hash == ""  # no channel diff of its own


def test_a_turn_starting_during_the_bars_last_await_keeps_its_stop(tmp_path, repo, monkeypatch):
    """GPT on 5c0d70574e: an older refresh's bar must not drop the Stop a turn that
    started during its last await needs (Stop is decided after that await)."""
    from kiro_crew.slack.code_channels import agent_bar

    h = _Harness(tmp_path)
    binding = suite._FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    real_items = agent_bar.agent_items

    def read_while_a_turn_starts(channel_id):
        h.svc._turning.add(rec.key)  # on_turn_start lands during the read
        return real_items(channel_id)

    monkeypatch.setattr(agent_bar, "agent_items", read_while_a_turn_starts)
    stored = h.svc.store.get("T1", "C1")
    asyncio.run(h.svc._publish_bar(stored, binding))
    assert "stop" in _bar_keys(h)


def test_slacks_stop_during_a_refresh_stops_every_workspace_write(tmp_path, repo, monkeypatch):
    """GPT on 5c0d70574e: Slack's own Stop lands while a refresh awaits its PR and
    git reads. No agents.* call may follow for the channel until its next turn --
    not the bar, the diff, the resource, nor a tab removal."""
    from kiro_crew.slack.code_channels import status

    h = _Harness(tmp_path)
    binding = suite._FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    (Path(rec.worktree) / "README.md").write_text("hello\nchannel edit\n")
    stored = h.svc.store.get("T1", "C1")
    stored.bar_hash, stored.view_id, stored.diff_hash = "", "V1", "old"
    stored.pr_number, stored.pr_url, stored.pr_state = 7, "https://github.com/a/b/pull/7", "open"
    h.svc.store.put(stored)
    h.svc._sent_bar.pop(rec.key, None)
    before = len(h.client.calls)

    async def pr_read_meanwhile_stopped(record):
        status.quiet_after_stop("C1")  # Slack's Stop arrives during the reads

    monkeypatch.setattr(h.svc, "_refresh_pr", pr_read_meanwhile_stopped)
    try:
        asyncio.run(h.svc.refresh(binding))
        assert h.client.calls[before:] == []
        # A removal is an agents.* call too.
        asyncio.run(h.svc._clear_diff(h.svc.store.get("T1", "C1")))
        assert h.client.calls[before:] == []
    finally:
        status._quiet.discard("C1")


def test_a_worker_tab_comes_down_when_the_channel_is_set_off_at_the_write(
    tmp_path, repo, monkeypatch
):
    """Shared AND Off at the last moment: not even the channel's own diff may go
    out, and the tab already up (it lists workers) is taken down."""
    board = _worker_tree(tmp_path, repo)
    h = _Harness(tmp_path, snapshot=board)
    binding = suite._FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    rec = h.svc.store.get("T1", "C1")
    (Path(rec.worktree) / "README.md").write_text("hello\nchannel edit\n")
    rec.diff_hash, rec.view_id = "old", "V1"  # a combined tab is up
    h.svc._boards[rec.key] = board
    admitted = {"now": True}
    source.set_scope_predicate(lambda state, b: admitted["now"])
    monkeypatch.setattr(service, "_activation_admits", lambda b: admitted["now"])
    _flip_at_the_write(h, admitted)
    asyncio.run(h.svc._publish_diff(rec, binding))
    assert [m for m, _ in h.client.calls if m == "set_diff_view"] == []
    assert h.client.last("remove_view") == {"view_id": "V1"}
    assert rec.view_id == "" and rec.diff_hash == ""


def test_a_bar_held_back_by_slacks_stop_answers_stopped(tmp_path, repo):
    from kiro_crew.slack.code_channels import status

    h = _Harness(tmp_path)
    binding = suite._FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    stored = h.svc.store.get("T1", "C1")
    stored.bar_hash = ""
    h.svc.store.put(stored)
    h.svc._sent_bar.pop(rec.key, None)
    client = h.client

    def at_write():
        status.quiet_after_stop("C1")  # Slack's Stop while the bar was built
        return client

    h.svc._client = at_write
    try:
        assert asyncio.run(h.svc.republish_bar(binding)) == "stopped"
    finally:
        status._quiet.discard("C1")


def test_a_bar_held_back_by_the_channel_going_off_answers_channel_off(tmp_path, repo, monkeypatch):
    h = _Harness(tmp_path)
    binding = suite._FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    stored = h.svc.store.get("T1", "C1")
    stored.bar_hash = ""
    h.svc.store.put(stored)
    h.svc._sent_bar.pop(rec.key, None)
    admitted = {"now": True}
    monkeypatch.setattr(service, "_activation_admits", lambda b: admitted["now"])
    _flip_at_the_write(h, admitted)
    assert asyncio.run(h.svc.republish_bar(binding)) == "channel_off"


def test_a_bar_sent_outside_a_refresh_never_re_sends_a_refused_cached_board(
    tmp_path, repo, monkeypatch, lineage
):
    """The context bar's Workers item comes from the cached board: re-checked like
    the wrap-up (the bar names no worker; their tally is the board's)."""
    binding = _scoped_binding(repo)
    lineage.put_binding(binding)
    conductor = SimpleNamespace(key="chat-cond", _slack_channel="C1")
    monkeypatch.setattr(cc_binding, "bound_slot", lambda state, b: conductor)
    monkeypatch.setattr(surface, "dashboard_state", lambda: object())
    refusal = {"why": ""}
    monkeypatch.setattr(source, "_publish_refusal", lambda state, b: refusal["why"])
    h = _Harness(tmp_path)
    source.set_scope_predicate(None)  # the production scope check
    rec = asyncio.run(h.svc.ensure(binding))
    snap = model.BoardSnapshot(rows=(_row(title="Fix login", session_key="chat-w1"),))
    h.svc._boards[rec.key] = snap
    assert asyncio.run(h.svc.republish_bar(binding)) == ""
    assert "workers" in _bar_keys(h)  # admitted: shown
    refusal["why"] = "the channel is shared with another organization"
    asyncio.run(h.svc.republish_bar(binding))
    assert rec.key not in h.svc._boards
    assert "workers" not in _bar_keys(h)
    # A turn start that re-sends the bar (no bar stored yet) checks the same scope.
    h.svc._boards[rec.key] = snap
    stored = h.svc.store.get("T1", "C1")
    stored.bar_hash = ""
    h.svc.store.put(stored)
    asyncio.run(h.svc.ensure(binding))
    assert rec.key not in h.svc._boards
    assert "workers" not in _bar_keys(h)


def test_a_diff_refusal_after_the_bar_went_out_resends_the_bar_without_workers(
    tmp_path, repo, monkeypatch
):
    tree = tmp_path / "wt-w"
    suite._git("worktree", "add", "-q", "-b", "feat/w", str(tree), "main", cwd=repo)
    (tree / "w.txt").write_text("w\n")
    suite._git("add", "w.txt", cwd=tree)
    suite._git("commit", "-q", "-m", "w", cwd=tree)
    board = model.BoardSnapshot(rows=(_row(title="Fix login", branch="feat/w"),))
    h = _Harness(tmp_path, snapshot=board)
    binding = suite._FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    real_build = h.svc._build_diff
    admitted = {"now": True}

    def build_then_share(record, board_arg=None):
        out = real_build(record, board_arg)
        if board_arg is not None:
            admitted["now"] = False
        return out

    monkeypatch.setattr(h.svc, "_build_diff", build_then_share)
    source.set_scope_predicate(lambda state, b: admitted["now"])
    published_admitted: list[bool] = []
    real_publish = h.svc._publish_board

    async def publish_board(b, snap):
        published_admitted.append(admitted["now"])
        return await real_publish(b, snap)

    h.svc._publish_board = publish_board
    asyncio.run(h.svc.refresh(binding))
    assert "workers" not in _bar_keys(h)  # the bar re-sent after the refusal
    # The board is published first (the bar links it), while still admitted;
    # nothing goes out for the board after the refusal.
    assert published_admitted in ([], [True])
    assert h.svc.board("T1", "C1") is None
