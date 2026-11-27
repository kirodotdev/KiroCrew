"""Shared fixtures and helpers for the code-channel view-tab tests.

Imported by ``test_slack_code_channel_view_tabs*.py``: the bound channel, the fake Slack
views API, one controlled clock, the Workers-tab Stop world and the board rows.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from slack_code_channels_helpers import OWNER, make_env

from kiro_crew.slack.code_channels import (
    actions,
    commands,
    surface,
    tabs,
    tools,
    views,
    worker_stop,
    wrapup,
)
from kiro_crew.slack.code_channels.board import model, publisher, render, source
from kiro_crew.slack.code_channels.store import Binding
from kiro_crew.slack.code_channels.surface_store import SurfaceStore
from kiro_crew.slack.code_channels.surface_store import set_store as set_surface_store
from kiro_crew.slack.code_channels.surface_store import store as surface_store
from kiro_crew.testing.synthetic_credentials import github_classic

CH = "C0CODE"


OTHER_CH = "C0OTHER"


ORIGIN = "C0ORIGIN"


SLOT = "chat-1-1"


SESSION = "dashboard:chat-1-1"


STRANGER = "USTRANGER"


WORKERS_VIEW = "VWORKERS1"


PR_URL = "https://github.com/acme/widgets/pull/{n}"


_FAKE_KEY = github_classic(seed=61)  # a checksummed token the redactor removes


_NOW = datetime(2026, 10, 21, 13, 30, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _clear_board_timers() -> None:
    from kiro_crew.slack.code_channels import board

    for _due, handle in board._timers.values():
        handle.cancel()
    board._timers.clear()


@pytest.fixture(name="no_host_dns", autouse=True)
def no_host_dns_fixture(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """The board scrubber's host-name read (``getfqdn``) may reverse-resolve over
    DNS on first use: stubbed for every test that imports this fixture, so the
    module's cached answer is neither read nor filled here."""
    _floor_monkeypatch.setattr(model, "_host_names", lambda: ("box.example.internal", "box"))


@pytest.fixture(name="env")
def env_fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    gen = make_env(monkeypatch, tmp_path)
    e = next(gen)
    set_surface_store(SurfaceStore(tmp_path / "surface.json"))
    for mod in (tools, commands, wrapup, actions):
        monkeypatch.setattr(mod, "live_config", e.cfg)
    monkeypatch.setattr(worker_stop, "live_config", e.cfg)
    monkeypatch.setattr(surface, "sel", lambda: e.sel)
    surface.set_slack_provider(lambda: e.slack)
    monkeypatch.setattr(surface, "dashboard_state", lambda: e.state)
    monkeypatch.setattr(surface, "caller_slot_key", lambda _s, key: SLOT if key == SESSION else "")
    # These channels are in the ledger's scope; the scope check itself has its own
    # tests (test_slack_code_channel_board.py), as board_live pins it too.
    monkeypatch.setattr(source, "scope_admits", lambda state, binding: True)
    e.allowed = {OWNER}
    from kiro_crew.slack.handler_runtime import access

    monkeypatch.setattr(access, "is_allowed_user", lambda u: u in e.allowed)
    monkeypatch.setattr("kiro_crew.slack.handler.is_allowed_user", lambda u: u in e.allowed)
    # The Stop's own user check (session_events._may_use_bot) reads these two
    # through kiro_crew.slack.events: pinned per test, so another suite's
    # import-time setup can never decide who may stop here.
    monkeypatch.setattr("kiro_crew.slack.events.is_allowed_user", lambda u: u in e.allowed)
    monkeypatch.setattr(
        "kiro_crew.slack.events.is_owner", lambda u: u == OWNER and OWNER in e.allowed
    )
    from kiro_crew.messaging import identity

    e.inbound_ok = True

    async def inbound(_channel: str) -> bool:
        return e.inbound_ok

    async def outbound(_channel: str) -> bool:
        return True

    monkeypatch.setattr(identity, "channel_inbound_permitted", inbound)
    monkeypatch.setattr(identity, "channel_outbound_permitted", outbound)
    actions._seen = None
    worker_stop.reset_for_tests()
    tabs.reset_for_tests()
    from kiro_crew.slack.code_channels.board import stops

    stops.reset_for_tests()
    # Per-process tables other suites may have filled: locks bound to their
    # event loops, and the board publisher's per-channel throttles.
    views._locks.clear()
    views._record_locks.clear()
    for channel in (CH, OTHER_CH):
        publisher.forget(channel)
    clock = Clock()
    monkeypatch.setattr(tabs, "_clock", clock)
    # One controlled clock for the tab timers AND the views' recorded stamps and
    # relist: no live wall-clock tick decides what a reconcile keeps.
    monkeypatch.setattr(views, "_wall", clock)
    e.clock = clock
    surface._archived_here.clear()
    from kiro_crew.slack.agent_sessions import fence

    monkeypatch.setattr(fence, "_LIVE", type(fence._LIVE)())
    # A tab's hold arms the board's per-channel timer (one per channel, earliest
    # wins): none may carry over from another test, or outlive this one.
    _clear_board_timers()
    yield e
    _clear_board_timers()
    set_surface_store(None)
    surface.set_slack_provider(None)
    source.set_scope_predicate(None)
    source.set_records_reader(None)
    try:
        next(gen)
    except StopIteration:
        pass


def _bind(env: Any, *, channel: str = CH, session: str = SLOT) -> Binding:
    binding = Binding(
        channel_id=channel,
        team_id="T1",
        session_key=session,
        anchor_ts=f"100.{channel[-4:]}",
        origin_channel_id=ORIGIN,
        origin_ts="1700000000.000100",
        title="Fix flaky login",
        channel_name="fix-login",
    )
    env.store.put_binding(binding)
    env.state.linked[binding.anchor_ts] = SimpleNamespace(key=session, running=True)
    return binding


def _calls(env: Any, method: str) -> list[dict[str, Any]]:
    return [body for m, body in env.api.calls if m == method]


def _set_views(
    env: Any, listed: list[dict[str, Any]] | None = None, refuse: str = "", times: int = 1
) -> None:
    """setView answers ``V<view_key>`` (or *refuse* for its first *times* calls).

    listViews answers *listed* plus every view setView opened and removeView did
    not remove, the way Slack would.
    """
    state = {"n": 0}
    live: dict[str, str] = {}
    extra = list(listed or [])

    def set_view(body: dict[str, Any]) -> dict[str, Any]:
        state["n"] += 1
        if refuse and state["n"] <= times:
            return {"ok": False, "error": refuse}
        view_id = "V" + re.sub(r"[^A-Za-z0-9]", "", body.get("view_key", ""))
        live[view_id] = body.get("view_key", "")
        return {"ok": True, "view_id": view_id}

    def remove_view(body: dict[str, Any]) -> dict[str, Any]:
        live.pop(body.get("view_id", ""), None)
        for view in list(extra):
            if view.get("view_id") == body.get("view_id"):
                extra.remove(view)
        return {"ok": True}

    def list_views(_body: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "views": extra + [{"view_id": v, "view_key": k} for v, k in live.items()],
        }

    env.api.responses["agents.conversations.setView"] = set_view
    env.api.responses["agents.conversations.removeView"] = remove_view
    env.api.responses["agents.conversations.listViews"] = list_views


def _row(item_id: str = "it_00000001", state: str = model.WORKING, **kw: Any) -> model.WorkerRow:
    return model.WorkerRow(
        item_id=item_id, title=kw.pop("title", f"Task {item_id}"), state=state, **kw
    )


def _link(key: str) -> str:
    return f"https://dash.example/chat?sid={key}" if key else ""


def _v(key: str, type: str, at: str) -> dict[str, str]:
    return {"key": key, "view_id": f"V{key.replace('-', '')}", "type": type, "at": at}


_OURS = [
    _v("plan", "canvas", "0"),
    _v("workers", "block_kit", "1"),
    _v("pr-12", "pull_request", "2"),
    _v("pr-30", "pull_request", "3"),
    _v("report", "html", "9"),
    _v("chart", "block_kit", "5"),
]


_TABS = render.Tabs(channel_id=CH, workers=WORKERS_VIEW, code=True, prs=((12, "VPR12"),))


def _snapshot() -> model.BoardSnapshot:
    return model.BoardSnapshot(
        rows=(
            _row(
                "it_00000001",
                title="Fix login",
                pr_number=12,
                pr_url=PR_URL.format(n=12),
                pr_state="open",
                pr_checks=(3, 5, 2),
                session_key="chat-w1",
                step="Running pytest -q",
                turn_started_at=(_NOW - timedelta(minutes=4)).isoformat(),
            ),
            _row(
                "it_00000002",
                title="Docs",
                pr_number=13,
                pr_url=PR_URL.format(n=13),
                pr_state="open",
                session_key="chat-w2",
            ),
        )
    )


def _channel_elements(blocks: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "channel":
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(blocks)
    return found


class _Refusing:
    def __init__(self, refuse: int) -> None:
        self.refuse = refuse
        self.sent: list[list[dict[str, Any]]] = []

    async def post_blocks(self, channel: str, blocks: Any, text: str, thread_ts: Any = None) -> str:
        self.sent.append(blocks)
        if self.refuse:
            self.refuse -= 1
            exc = Exception("invalid_blocks")
            exc.response = {"error": "invalid_blocks"}  # type: ignore[attr-defined]
            raise exc
        return "100.1"

    async def add_pin(self, channel: str, ts: str) -> None:
        return None

    async def get_permalink(self, channel: str, ts: str) -> str:
        return ""

    async def post_message(self, *a: Any, **k: Any) -> str:
        return "200.1"


def _texts(block: dict[str, Any]) -> str:
    return "".join(
        e.get("text", "") if e.get("type") != "channel" else f"<#{e['tab_id']}>"
        for e in block["elements"][0]["elements"]
    )


def _rate_limited(seconds: str) -> Exception:
    """What slack_sdk raises for a 429: the body, the status and Slack's Retry-After."""
    from slack_sdk.errors import SlackApiError

    class _Response(dict):
        status_code = 429
        headers = {"Retry-After": seconds}

    return SlackApiError("ratelimited", _Response(ok=False, error="ratelimited"))


def _stop_payload(
    *,
    user: str = OWNER,
    view_id: str = WORKERS_VIEW,
    value: str = "it_00000001",
    channel: str = CH,
    ts: str = "1.1",
) -> dict[str, Any]:
    return {
        "type": "block_actions",
        "user": {"id": user, "team_id": "T1"},
        "team": {"id": "T1"},
        "channel": {"id": channel},
        "container": {"type": "view", "view_id": view_id},
        "actions": [{"action_id": "kc_cc_worker_stop", "value": value, "action_ts": ts}],
    }


@pytest.fixture(name="stop_world")
def stop_world_fixture(env: Any, monkeypatch: Any) -> Any:
    """A bound channel whose conductor has two workers, one running."""
    binding = _bind(env)
    other = _bind(env, channel=OTHER_CH, session="chat-9-1")
    surface_store().update(CH, views=[_v("workers", "block_kit", "1") | {"view_id": WORKERS_VIEW}])
    boards = {
        CH: model.BoardSnapshot(
            rows=(
                _row("it_00000001", title="Fix login", session_key="chat-w1"),
                _row("it_00000002", title="Idle one", state=model.IDLE, session_key="chat-w2"),
                _row("it_0000000c", title="Self", session_key=SLOT),
            )
        ),
        OTHER_CH: model.BoardSnapshot(rows=(_row("it_00000009", session_key="chat-w9"),)),
    }
    reads: list[str] = []

    async def read_board(state: Any, b: Binding) -> Any:
        reads.append(b.channel_id)
        return boards.get(b.channel_id)

    monkeypatch.setattr(source, "read_board", read_board)

    def _slot(key: str, running: bool) -> Any:
        # The running turn's own identity, as _cancel_target reads it.
        return SimpleNamespace(
            key=key, running=running, _active_turn_session_key=f"dashboard_{key}"
        )

    slots = {
        "chat-w1": _slot("chat-w1", True),
        "chat-w2": _slot("chat-w2", False),
        "chat-w9": _slot("chat-w9", True),
        SLOT: _slot(SLOT, True),
    }
    env.state.get_slot = lambda key: slots.get(key)
    # The dashboard's session -> slot resolution, over these slots.
    monkeypatch.setattr(
        surface,
        "caller_slot_key",
        lambda _s, key: next((k for k in slots if key in (k, f"dashboard_{k}")), ""),
    )
    stopped: list[Any] = []
    answers: list[dict[str, Any]] = []

    async def stop_slot_turn(state: Any, slot: Any, **kw: Any) -> dict[str, Any]:
        stopped.append((slot.key, kw))
        return answers.pop(0) if answers else {"info": ""}

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", stop_slot_turn)
    noted: list[str] = []
    from kiro_crew.slack.code_channels import board

    monkeypatch.setattr(board, "note_session_turn", noted.append)
    return SimpleNamespace(
        binding=binding,
        other=other,
        stopped=stopped,
        reads=reads,
        noted=noted,
        boards=boards,
        slots=slots,
        answers=answers,
    )


_STOP_KW = {"source": "slack", "escalate": False, "cancel_key": "dashboard_chat-w1"}


def _item(worker: str = "chat-w1") -> Any:
    return SimpleNamespace(
        item_id="it_00000001",
        title="Fix login",
        state="open",
        status="progress",
        reason=None,
        summary="",
        artifacts={},
        pr=None,
        worker_session_key=worker,
        last_report_at="",
        created_at="",
        round=1,
    )


def _project(state: Any, live: str = model.LIVE_IDLE) -> model.BoardSnapshot:
    from kiro_crew.slack.code_channels.board import stops

    return source.project(
        SimpleNamespace(goal="g", round=1),
        [_item()],
        lambda key: live,
        activity_of=lambda key: model.Activity(),
        stopped_of=lambda key: stops.current(state, key),
    )


def _resumed(stop_world: Any, generation: int) -> Any:
    """A new slot under the same key, as a resumed worker gets: its turns count from 1."""
    old = stop_world.slots["chat-w1"]
    new = SimpleNamespace(
        key=old.key,
        running=True,
        _active_turn_session_key=old._active_turn_session_key,
        _turn_generation=generation,
    )
    stop_world.slots["chat-w1"] = new
    return new


_TRAILING = ["\n", "\r\n", " ", "\t", "\x00", "é"]


ALICE = "U0ALICE001"


def _scope_flag(monkeypatch: Any) -> dict[str, bool]:
    flag = {"in": True}
    monkeypatch.setattr(source, "scope_admits", lambda state, binding: flag["in"])
    return flag
