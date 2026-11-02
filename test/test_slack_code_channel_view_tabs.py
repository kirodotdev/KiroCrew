"""Code-channel view tabs: native PR tabs, tab deep links, the interactive Workers tab.

Recorded Slack payloads in, recorded calls out (``slack_code_channels_helpers``),
the way the code-channel API-gap tests run. Each behaviour has a test that fails
when it is reverted; the security ones (whose view a click came from, whose
worker it stops, who may press it) were each revert-verified.
"""

from __future__ import annotations

import asyncio
import json
import os
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
    status,
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

CH = "C0CODE"
OTHER_CH = "C0OTHER"
ORIGIN = "C0ORIGIN"
SLOT = "chat-1-1"
SESSION = "dashboard:chat-1-1"
STRANGER = "USTRANGER"
WORKERS_VIEW = "VWORKERS1"
PR_URL = "https://github.com/acme/widgets/pull/{n}"
_FAKE_KEY = "AKIA" + "IOSFODNN7" + "EXAMPLE"
_NOW = datetime(2026, 10, 21, 13, 30, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
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
    yield e
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


# ── 1. The tab budget ─────────────────────────────────────────────────────


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


def test_agent_tabs_go_first_then_the_oldest_pr_and_never_plan_or_workers() -> None:
    for protected in ("workers", "plan", "diff"):
        order = [v["key"] for v in views._candidates(list(_OURS), protected)]
        assert order == ["chart", "report", "pr-12", "pr-30"]
    # A PR tab never displaces another PR tab, and an agent's view never displaces one.
    assert [v["key"] for v in views._candidates(list(_OURS), "pr-44")] == ["chart", "report"]
    assert [v["key"] for v in views._candidates(list(_OURS), "notes")] == ["chart", "report"]


def test_a_view_under_a_pr_key_is_a_pr_tab_only_when_recorded_as_one() -> None:
    assert views.is_pr_tab({"key": "pr-12", "type": "pull_request"})
    assert not views.is_pr_tab({"key": "pr-12", "type": "html"})
    assert not views.is_pr_tab({"key": "pr-x", "type": "pull_request"})


@pytest.mark.asyncio
async def test_at_the_cap_the_workers_tab_removes_an_agent_tab_before_a_pr_tab(env: Any) -> None:
    _bind(env)
    surface_store().update(CH, views=[_v("pr-12", "pull_request", "1"), _v("report", "html", "9")])
    listed = [{"view_id": "Vpr12"}, {"view_id": "Vreport"}, {"view_id": "Vtheirs"}]
    _set_views(env, listed, refuse="too_many_views")
    snap = model.BoardSnapshot(rows=(_row(session_key="chat-w1"),))
    assert await tabs.publish_workers(_bind(env), snap, link_for=_link) == "written"
    assert _calls(env, "agents.conversations.removeView") == [
        {"channel_id": CH, "view_id": "Vreport"}
    ]
    assert views.recorded(surface_store().get(CH), "workers")["view_id"] == "Vworkers"


@pytest.mark.asyncio
async def test_a_pr_tab_the_workers_tab_removed_is_unlinked_by_an_armed_pass(env: Any) -> None:
    """The Workers tab linking a PR tab it removed to fit is rewritten without it.

    No other trigger fires on a finished board, so the removal itself arms the
    pass (through the board's re-arm, :func:`tabs.take_retry`).
    """
    binding = _bind(env)
    surface_store().update(CH, views=[_v("pr-12", "pull_request", "1")])
    listed = [{"view_id": "Vpr12"}] + [{"view_id": f"Vtheirs{n}"} for n in range(4)]
    _set_views(env, listed, refuse="too_many_views")
    row = _row(state=model.DONE, session_key="chat-w1", pr_number=12, pr_url=PR_URL.format(n=12))
    snap = model.BoardSnapshot(rows=(row,))
    assert await tabs.publish_workers(binding, snap, link_for=_link) == "written"
    assert _calls(env, "agents.conversations.removeView") == [
        {"channel_id": CH, "view_id": "Vpr12"}
    ]
    assert "Vpr12" in json.dumps(_calls(env, "agents.conversations.setView")[-1])
    assert tabs.take_retry(CH) == tabs.WORKERS_MIN_GAP_SECS
    env.clock.now += tabs.WORKERS_MIN_GAP_SECS
    assert await tabs.publish_workers(binding, snap, link_for=_link) == "written"
    assert "Vpr12" not in json.dumps(_calls(env, "agents.conversations.setView")[-1])
    assert tabs.take_retry(CH) == 0.0  # settled: nothing more is armed


@pytest.mark.asyncio
async def test_a_pr_tab_the_plan_tab_removed_refreshes_the_board(
    env: Any, monkeypatch: Any
) -> None:
    from kiro_crew.slack.code_channels import board

    _bind(env)
    surface_store().update(CH, views=[_v("pr-12", "pull_request", "1")])
    listed = [{"view_id": "Vpr12"}] + [{"view_id": f"Vtheirs{n}"} for n in range(4)]
    _set_views(env, listed, refuse="too_many_views")
    env.api.responses["conversations.canvases.create"] = {"ok": True, "canvas_id": "F9"}
    refreshed: list[str] = []
    monkeypatch.setattr(board, "_schedule", lambda ch, delay=None: refreshed.append(ch) or True)
    out = await tools.run_tool(env.state, SESSION, tools.PUBLISH_PLAN_CANVAS, {"markdown": "# P"})
    assert out.ok, out.text
    assert "pull request tab 'pr-12' was removed" in out.text
    assert refreshed == [CH]  # the board and the Workers tab drop the link


@pytest.mark.asyncio
async def test_a_plan_tab_that_removed_an_agent_tab_does_not_refresh_the_board(
    env: Any, monkeypatch: Any
) -> None:
    from kiro_crew.slack.code_channels import board

    _bind(env)
    surface_store().update(CH, views=[_v("report", "html", "1")])
    listed = [{"view_id": "Vreport"}] + [{"view_id": f"Vtheirs{n}"} for n in range(4)]
    _set_views(env, listed, refuse="too_many_views")
    env.api.responses["conversations.canvases.create"] = {"ok": True, "canvas_id": "F9"}
    refreshed: list[str] = []
    monkeypatch.setattr(board, "_schedule", lambda ch, delay=None: refreshed.append(ch) or True)
    out = await tools.run_tool(env.state, SESSION, tools.PUBLISH_PLAN_CANVAS, {"markdown": "# P"})
    assert out.ok, out.text
    assert "older view 'report' was removed" in out.text
    assert refreshed == []


@pytest.mark.asyncio
async def test_a_pr_tab_never_removes_another_pr_tab_to_fit(env: Any) -> None:
    binding = _bind(env)
    surface_store().update(
        CH, views=[_v("pr-12", "pull_request", "1") | {"url": PR_URL.format(n=12)}]
    )
    listed = [{"view_id": "Vpr12"}] + [{"view_id": f"Vtheirs{n}"} for n in range(4)]
    _set_views(env, listed, refuse="too_many_views", times=99)
    wanted = [
        tabs.PrRef(12, PR_URL.format(n=12), "open"),
        tabs.PrRef(40, PR_URL.format(n=40), "open"),
    ]
    held = await tabs.sync_pr_tabs(binding, wanted)
    assert _calls(env, "agents.conversations.removeView") == []
    assert set(held) == {12}


@pytest.mark.parametrize("key", ["workers", "pr-12", "diff", "plan"])
@pytest.mark.asyncio
async def test_the_agent_may_not_write_under_the_channels_own_tab_keys(env: Any, key: str) -> None:
    _bind(env)
    _set_views(env)
    out = await tools.run_tool(
        env.state, SESSION, tools.SET_VIEW, {"type": "html", "view_key": key, "content": "<html/>"}
    )
    assert not out.ok
    assert _calls(env, "agents.conversations.setView") == []


@pytest.mark.asyncio
async def test_the_set_view_tool_no_longer_says_pull_request_tabs_publish_themselves_unasked(
    env: Any,
) -> None:
    _bind(env)
    out = await tools.run_tool(
        env.state, SESSION, tools.SET_VIEW, {"type": "pull_request", "view_key": "x"}
    )
    assert not out.ok and "newest open pull requests" in out.text
    assert "published from the worktree" not in out.text


# ── 2. Pull-request tabs ──────────────────────────────────────────────────


def test_wanted_prs_are_the_two_newest_open_confirmed_prs() -> None:
    own = tabs.PrRef(10, PR_URL.format(n=10), "open")
    rows = [
        _row("it_00000001", pr_number=31, pr_url=PR_URL.format(n=31), pr_state="draft"),
        _row("it_00000002", pr_number=45, pr_url="", pr_state=""),  # unconfirmed
        _row("it_00000003", pr_number=40, pr_url=PR_URL.format(n=40), pr_state="merged"),
        _row("it_00000004", pr_number=10, pr_url=PR_URL.format(n=10), pr_state="open"),
        _row("it_00000005", pr_number=22, pr_url=PR_URL.format(n=22), pr_state="open"),
    ]
    assert [p.number for p in tabs.wanted_prs(own, rows)] == [31, 22]
    assert [p.number for p in tabs.wanted_prs(own, rows[:1])] == [31, 10]


@pytest.mark.asyncio
async def test_a_pr_tab_is_opened_once_and_named_for_its_pr(env: Any) -> None:
    binding = _bind(env)
    _set_views(env)
    wanted = [tabs.PrRef(42, PR_URL.format(n=42), "open")]
    assert await tabs.sync_pr_tabs(binding, wanted) == {42: "Vpr42"}
    assert _calls(env, "agents.conversations.setView") == [
        {
            "channel_id": CH,
            "type": "pull_request",
            "view_key": "pr-42",
            "name": "PR #42",
            "pr_url": PR_URL.format(n=42),
        }
    ]
    entry = views.recorded(surface_store().get(CH), "pr-42")
    assert entry["type"] == "pull_request" and entry["url"] == PR_URL.format(n=42)
    await tabs.sync_pr_tabs(binding, wanted)
    assert len(_calls(env, "agents.conversations.setView")) == 1  # unchanged: nothing sent


@pytest.mark.asyncio
async def test_a_merged_or_displaced_pr_loses_its_tab_and_only_ours_are_removed(env: Any) -> None:
    binding = _bind(env)
    _set_views(env)
    first = [
        tabs.PrRef(42, PR_URL.format(n=42), "open"),
        tabs.PrRef(12, PR_URL.format(n=12), "open"),
    ]
    await tabs.sync_pr_tabs(binding, first)
    # PR 12 merged and a newer PR 50 opened: 12's tab goes, 50's comes.
    second = [
        tabs.PrRef(50, PR_URL.format(n=50), "open"),
        tabs.PrRef(42, PR_URL.format(n=42), "open"),
    ]
    held = await tabs.sync_pr_tabs(binding, second, settled={12})
    assert _calls(env, "agents.conversations.removeView") == [
        {"channel_id": CH, "view_id": "Vpr12"}
    ]
    assert held == {50: "Vpr50", 42: "Vpr42"}
    # Nothing wanted and both SEEN merged: every PR tab of ours goes, an agent's stays.
    surface_store().update(CH, views=[*surface_store().get(CH).views, _v("report", "html", "1")])
    await tabs.sync_pr_tabs(binding, [], settled={50, 42})
    removed = [c["view_id"] for c in _calls(env, "agents.conversations.removeView")]
    assert removed[0] == "Vpr12" and sorted(removed[1:]) == ["Vpr42", "Vpr50"]
    assert [v["key"] for v in surface_store().get(CH).views] == ["report"]


@pytest.mark.asyncio
async def test_a_pass_that_could_not_read_a_pr_leaves_its_tab_alone(env: Any) -> None:
    binding = _bind(env)
    _set_views(env)
    both = [
        tabs.PrRef(42, PR_URL.format(n=42), "open"),
        tabs.PrRef(12, PR_URL.format(n=12), "open"),
    ]
    await tabs.sync_pr_tabs(binding, both)
    # The board read failed (or the forge did not answer): only the channel's own
    # PR is known this pass. The workers' PR tab is kept, not torn down and reopened.
    held = await tabs.sync_pr_tabs(binding, [tabs.PrRef(42, PR_URL.format(n=42), "open")])
    assert _calls(env, "agents.conversations.removeView") == []
    assert held == {42: "Vpr42", 12: "Vpr12"}
    # Nothing readable at all: still nothing removed.
    assert await tabs.sync_pr_tabs(binding, []) == {42: "Vpr42", 12: "Vpr12"}
    # Two newer PRs seen open: the unseen one gives up its slot.
    newer = [
        tabs.PrRef(60, PR_URL.format(n=60), "open"),
        tabs.PrRef(42, PR_URL.format(n=42), "open"),
    ]
    held = await tabs.sync_pr_tabs(binding, newer)
    assert _calls(env, "agents.conversations.removeView") == [
        {"channel_id": CH, "view_id": "Vpr12"}
    ]
    assert held == {60: "Vpr60", 42: "Vpr42"}


@pytest.mark.asyncio
async def test_the_service_hands_its_own_pr_and_its_workers_prs_to_the_tabs(
    env: Any, monkeypatch: Any
) -> None:
    from kiro_crew.slack.code_channels.workspace import service as svc

    seen: list[Any] = []

    async def sync(binding: Any, wanted: Any, settled: Any = None, own: int = 0) -> dict[int, str]:
        seen.append(([p.number for p in wanted], settled, own))
        return {}

    monkeypatch.setattr(tabs, "sync_pr_tabs", sync)
    record = SimpleNamespace(
        slack_off="", pr_number=10, pr_url=PR_URL.format(n=10), pr_state="open"
    )
    board = model.BoardSnapshot(
        rows=(
            _row(pr_number=33, pr_url=PR_URL.format(n=33), pr_state="open"),
            _row("it_00000002", pr_number=7, pr_url=PR_URL.format(n=7), pr_state="merged"),
        )
    )
    service = svc.WorkspaceService(lambda: None)
    await service._publish_pr_tabs(_bind(env), record, board)
    assert seen == [([33, 10], {7}, 10)]


@pytest.mark.parametrize("code", ["invalid_view_type", "invalid_pr_url"])
@pytest.mark.asyncio
async def test_a_pr_tab_slack_will_not_take_is_logged_once_and_not_asked_again(
    env: Any, code: str, caplog: Any
) -> None:
    binding = _bind(env)
    env.api.responses["agents.conversations.setView"] = {"ok": False, "error": code}
    wanted = [tabs.PrRef(42, PR_URL.format(n=42), "open")]
    for _ in range(3):
        assert await tabs.sync_pr_tabs(binding, wanted) == {}
    assert len(_calls(env, "agents.conversations.setView")) == 1
    assert sum(f"not published ({code})" in r.getMessage() for r in caplog.records) == 1
    if code == "invalid_view_type":
        # The type itself is refused: no OTHER PR is asked for in this channel either.
        await tabs.sync_pr_tabs(binding, [tabs.PrRef(43, PR_URL.format(n=43), "open")])
        assert len(_calls(env, "agents.conversations.setView")) == 1


@pytest.mark.asyncio
async def test_a_full_channel_is_asked_again_only_after_the_retry_window(env: Any) -> None:
    binding = _bind(env)
    listed = [{"view_id": f"Vtheirs{n}"} for n in range(5)]
    env.api.responses["agents.conversations.setView"] = {"ok": False, "error": "too_many_views"}
    env.api.responses["agents.conversations.listViews"] = {"ok": True, "views": listed}
    wanted = [tabs.PrRef(42, PR_URL.format(n=42), "open")]
    await tabs.sync_pr_tabs(binding, wanted)
    await tabs.sync_pr_tabs(binding, wanted)
    assert len(_calls(env, "agents.conversations.setView")) == 1
    env.clock.now += tabs.FULL_RETRY_SECS + 1
    await tabs.sync_pr_tabs(binding, wanted)
    assert len(_calls(env, "agents.conversations.setView")) == 2


@pytest.mark.asyncio
async def test_a_tab_someone_removed_is_reopened_after_the_relist(env: Any) -> None:
    binding = _bind(env)
    _set_views(env)
    wanted = [tabs.PrRef(42, PR_URL.format(n=42), "open")]
    await tabs.sync_pr_tabs(binding, wanted)
    env.api.responses["agents.conversations.listViews"] = {"ok": True, "views": []}
    env.clock.now += tabs.RELIST_SECS + 1
    await tabs.sync_pr_tabs(binding, wanted)
    assert len(_calls(env, "agents.conversations.setView")) == 2


@pytest.mark.asyncio
async def test_no_tab_is_written_to_an_archived_or_stopped_channel(env: Any) -> None:
    binding = _bind(env)
    _set_views(env)
    surface.fence_archived(CH)
    wanted = [tabs.PrRef(42, PR_URL.format(n=42), "open")]
    assert await tabs.sync_pr_tabs(binding, wanted) == {}
    snap = model.BoardSnapshot(rows=(_row(session_key="chat-w1"),))
    assert await tabs.publish_workers(binding, snap, link_for=_link) == "fenced"
    assert _calls(env, "agents.conversations.setView") == []


# ── 3. Tab deep links ─────────────────────────────────────────────────────


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


def test_the_board_links_the_workers_and_code_tabs_and_each_pr_to_its_tab() -> None:
    blocks = render.board_blocks(_snapshot(), _link, tabs=_TABS, now=_NOW)
    header = blocks[1]  # right under the header; the plan's title is the summary
    assert header["type"] == "rich_text"
    elements = header["elements"][0]["elements"]
    assert elements[1] == {"type": "channel", "channel_id": CH, "tab_id": WORKERS_VIEW}
    assert elements[3] == {"type": "channel", "channel_id": CH, "tab_id": "code"}
    plan = blocks[2]
    assert plan["type"] == "plan"
    first, second = plan["tasks"]
    assert first["output"]["elements"][0]["elements"][0] == {
        "type": "channel",
        "channel_id": CH,
        "tab_id": "VPR12",
    }
    # A PR without a tab of its own still links the forge.
    assert second["output"]["elements"][0]["elements"][0]["type"] == "link"


def test_without_tabs_the_board_is_what_it_was_and_the_rows_look_never_names_a_tab() -> None:
    assert _channel_elements(render.board_blocks(_snapshot(), _link, now=_NOW)) == []
    rows = render.board_blocks(_snapshot(), _link, tabs=_TABS, look="rows", now=_NOW)
    assert _channel_elements(rows) == []


def test_a_tab_id_that_is_not_a_slack_id_is_never_linked() -> None:
    bad = render.Tabs(channel_id=CH, workers="V1|<x>", prs=((12, "../x"),))
    assert _channel_elements(render.board_blocks(_snapshot(), _link, tabs=bad, now=_NOW)) == []


def test_the_board_digest_moves_when_a_tab_appears() -> None:
    links: dict[str, str] = {}
    assert publisher.digest(_snapshot(), links, "plan", None) != publisher.digest(
        _snapshot(), links, "plan", _TABS
    )


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


@pytest.mark.asyncio
async def test_a_board_refused_with_tab_links_is_sent_as_a_plan_without_them(
    env: Any, monkeypatch: Any
) -> None:
    binding = _bind(env)
    fake = _Refusing(refuse=1)
    monkeypatch.setattr(surface, "slack", lambda: fake)
    monkeypatch.setattr(surface, "client", lambda: None)
    publisher.forget(CH)
    out = await publisher.publish(binding, _snapshot(), link_for=_link, tabs=_TABS)
    assert out == "posted"
    assert _channel_elements(fake.sent[0]) and not _channel_elements(fake.sent[1])
    assert any(b.get("type") == "plan" for b in fake.sent[1])  # still the plan look
    assert CH not in publisher._plan_refused
    publisher.forget(CH)


def test_the_wrap_up_links_the_code_tab_and_the_pr_tabs() -> None:
    facts = wrapup.Facts(branch="kc/x", code_tab=True, pr_tabs=((42, "VPR42"),))
    blocks = wrapup.wrap_up_blocks("Done.", facts, CH)
    found = _channel_elements(blocks)
    assert found == [
        {"type": "channel", "channel_id": CH, "tab_id": "code"},
        {"type": "channel", "channel_id": CH, "tab_id": "VPR42"},
    ]
    assert blocks[-1]["type"] == "actions"  # the Archive button stays last
    assert _channel_elements(wrapup.wrap_up_blocks("Done.", wrapup.Facts(), CH)) == []


@pytest.mark.asyncio
async def test_a_wrap_up_refused_for_its_tab_links_still_goes_out(
    env: Any, monkeypatch: Any
) -> None:
    binding = _bind(env)
    fake = _Refusing(refuse=1)
    monkeypatch.setattr(surface, "slack", lambda: fake)
    monkeypatch.setattr(surface, "client", lambda: None)

    async def facts(_b: Any) -> Any:
        return wrapup.Facts(code_tab=True)

    monkeypatch.setattr(wrapup, "gather_facts", facts)
    assert await wrapup.post_wrap_up(binding, "All done.", caller=OWNER) == "100.1"
    assert _channel_elements(fake.sent[0]) and not _channel_elements(fake.sent[1])
    assert fake.sent[1][-1]["type"] == "actions"


def test_gather_facts_reads_the_pr_tabs_this_gateway_opened(env: Any) -> None:
    surface_store().update(
        CH,
        views=[
            _v("pr-12", "pull_request", "1"),
            _v("pr-30", "pull_request", "2"),
            _v("pr-9", "html", "3"),
        ],
    )
    # PR 12 merged: its tab is about to go, so the lasting wrap-up does not link it.
    assert wrapup._pr_tabs(CH, {30}) == ((30, "Vpr30"),)
    assert wrapup._pr_tabs(CH, {30, 12, 9}) == ((30, "Vpr30"), (12, "Vpr12"))


# ── 4. The Workers tab ────────────────────────────────────────────────────


def _texts(block: dict[str, Any]) -> str:
    return "".join(
        e.get("text", "") if e.get("type") != "channel" else f"<#{e['tab_id']}>"
        for e in block["elements"][0]["elements"]
    )


def test_each_worker_shows_title_status_pr_and_its_buttons() -> None:
    waiting = _row(
        "it_00000003",
        model.WAITING_ON_YOU,
        title="Ship",
        waiting_on="approval",
        step="Running git push origin x",
        session_key="chat-w3",
    )
    stuck = _row("it_00000004", model.BLOCKED, title="Icons", summary="needs a token")
    lost = _row("it_00000005", model.LOST, title="Gone")
    snap = model.BoardSnapshot(rows=(*_snapshot().rows, waiting, stuck, lost))
    blocks = render.workers_view_blocks(snap, _link, tabs=_TABS, updated="13:30 UTC", now=_NOW)
    assert blocks[0]["text"]["text"] == f"*{render._escape(snap.aggregate)}*"
    assert blocks[1]["elements"][0]["text"] == "Updated 13:30 UTC"
    by_id = {b.get("block_id"): b for b in blocks}
    first = by_id["worker-it_00000001-text"]["elements"][0]["elements"]
    assert first[0] == {"type": "text", "text": "Fix login", "style": {"bold": True}}
    assert _texts(by_id["worker-it_00000001-text"]) == (
        "Fix login\nWorking · Running pytest -q · 4m\n<#VPR12> · CI 3/5 passing · 2 running"
    )
    assert {"type": "text", "text": "pytest -q", "style": {"code": True}} in first
    assert _texts(by_id["worker-it_00000003-text"]) == (
        "Ship\nWaiting on you: approve git push origin x"
    )
    assert _texts(by_id["worker-it_00000004-text"]) == "Icons\nStuck · needs a token"
    assert _texts(by_id["worker-it_00000005-text"]) == "Gone\nLost"
    buttons = by_id["worker-it_00000001"]["elements"]
    assert buttons[0]["url"] == _link("chat-w1") and buttons[0]["action_id"] == "kc_cc_worker_open"
    stop = buttons[1]
    assert stop["action_id"] == "kc_cc_worker_stop" and stop["value"] == "it_00000001"
    assert "style" not in stop  # outlined, never primary
    assert stop["confirm"]["title"]["text"] == "Stop Fix login?"
    # Stop only while working.
    assert [e["action_id"] for e in by_id["worker-it_00000003"]["elements"]] == [
        "kc_cc_worker_open"
    ]
    assert "worker-it_00000004" not in by_id  # no session, nothing to press


def test_done_workers_fold_into_one_line_each_at_the_bottom() -> None:
    done = _row(
        "it_00000006",
        model.DONE,
        title="Old fix",
        pr_number=7,
        pr_url=PR_URL.format(n=7),
        pr_state="merged",
        pr_checks=(12, 12, 0),
    )
    shipped = _row(
        "it_00000008",
        model.DONE,
        title="Fix login",
        pr_number=12,
        pr_url=PR_URL.format(n=12),
        pr_state="open",
        pr_checks=(41, 47, 6),
    )
    closed = _row("it_00000007", model.CLOSED, title="Dropped", closed_as="rejected")
    snap = model.BoardSnapshot(rows=(done, *_snapshot().rows[1:], shipped, closed))
    blocks = render.workers_view_blocks(snap, _link, tabs=_TABS, now=_NOW)
    assert blocks[-1]["block_id"] == "workers-settled"
    assert _texts(blocks[-1]).split("\n") == [
        "Rejected: Dropped",
        "Done: Fix login · <#VPR12> · CI 41/47",  # its PR opens its own tab
        "Done: Old fix · PR #7 · CI 12/12",  # merged, no tab: the forge link
    ]
    elements = blocks[-1]["elements"][0]["elements"]
    assert {"type": "link", "url": PR_URL.format(n=7), "text": "PR #7"} in elements
    assert not any(str(b.get("block_id", "")).startswith("worker-it_00000006") for b in blocks)


def test_the_workers_tab_stays_within_slacks_block_limit() -> None:
    rows = tuple(_row(f"it_{n:08x}", session_key=f"chat-w{n}") for n in range(40))
    blocks = render.workers_view_blocks(model.BoardSnapshot(rows=rows), _link, now=_NOW)
    assert len(blocks) <= 50
    assert blocks[-1]["elements"][0]["text"] == "+26 more, listed in the dashboard"


def test_the_workers_tab_carries_the_boards_scrubbing() -> None:
    home = os.path.expanduser("~")
    item = SimpleNamespace(
        item_id="it_0000000a",
        title=f"Fix <!channel> {_FAKE_KEY}",
        state="open",
        status="progress",
        reason=None,
        summary="",
        artifacts={},
        pr=None,
        worker_session_key="chat-w1",
        last_report_at="",
        created_at="",
        round=1,
    )
    activity = model.Activity(
        turn_started_at=_NOW.isoformat(),
        project="",
        tool_kind="execute",
        tool_input=json.dumps(
            {"command": f"curl -H 'x: {_FAKE_KEY}' ip-10-0-0-1.ec2.internal {home}/secret.txt"}
        ),
    )
    snap = source.project(
        SimpleNamespace(goal="g", round=1),
        [item],
        lambda key: model.LIVE_RUNNING,
        activity_of=lambda key: activity,
    )
    text = json.dumps(render.workers_view_blocks(snap, _link, now=_NOW))
    assert _FAKE_KEY not in text and "<!channel>" not in text
    assert home not in text and "ip-10-0-0-1" not in text
    assert '"text": "Running "' in text and '"text": "curl' in text  # the call still shows


@pytest.mark.asyncio
async def test_the_workers_tab_is_written_only_when_it_changed_and_at_most_once_a_gap(
    env: Any,
) -> None:
    binding = _bind(env)
    _set_views(env)
    snap = _snapshot()
    assert await tabs.publish_workers(binding, snap, link_for=_link) == "written"
    sent = _calls(env, "agents.conversations.setView")
    assert sent[0]["type"] == "block_kit" and sent[0]["view_key"] == "workers"
    assert sent[0]["name"] == "Workers" and "csp" not in sent[0]
    env.clock.now += 60
    assert await tabs.publish_workers(binding, snap, link_for=_link) == ""  # unchanged
    moved = model.BoardSnapshot(rows=snap.rows[:1])
    env.clock.now += 1
    assert await tabs.publish_workers(binding, moved, link_for=_link) == "written"
    env.clock.now += 1
    later = model.BoardSnapshot(rows=snap.rows)
    assert await tabs.publish_workers(binding, later, link_for=_link) == "deferred"
    assert 0 < tabs.take_retry(CH) <= tabs.WORKERS_MIN_GAP_SECS
    env.clock.now += tabs.WORKERS_MIN_GAP_SECS
    assert await tabs.publish_workers(binding, later, link_for=_link) == "written"
    assert len(_calls(env, "agents.conversations.setView")) == 3


@pytest.mark.asyncio
async def test_a_failed_workers_write_is_retried_with_backoff(env: Any) -> None:
    binding = _bind(env)
    env.api.responses["agents.conversations.setView"] = {"ok": False, "error": "internal_error"}
    assert await tabs.publish_workers(binding, _snapshot(), link_for=_link) == "failed"
    first = tabs.take_retry(CH)
    assert first > 0
    # Inside the backoff the next refresh sends nothing, it re-arms.
    assert await tabs.publish_workers(binding, _snapshot(), link_for=_link) == "backoff"
    assert len(_calls(env, "agents.conversations.setView")) == 1
    assert 0 < tabs.take_retry(CH) <= first
    env.clock.now += first + 1
    assert await tabs.publish_workers(binding, _snapshot(), link_for=_link) == "failed"
    assert len(_calls(env, "agents.conversations.setView")) == 2
    assert tabs.take_retry(CH) > first


def _rate_limited(seconds: str) -> Exception:
    """What slack_sdk raises for a 429: the body, the status and Slack's Retry-After."""
    from slack_sdk.errors import SlackApiError

    class _Response(dict):
        status_code = 429
        headers = {"Retry-After": seconds}

    return SlackApiError("ratelimited", _Response(ok=False, error="ratelimited"))


@pytest.mark.asyncio
async def test_a_rate_limited_workers_write_waits_out_slacks_retry_after(env: Any) -> None:
    binding = _bind(env)
    env.api.responses["agents.conversations.setView"] = _rate_limited("30")
    assert await tabs.publish_workers(binding, _snapshot(), link_for=_link) == "failed"
    assert tabs.take_retry(CH) == 30.0  # not the 5 s first backoff
    env.clock.now += 29
    assert await tabs.publish_workers(binding, _snapshot(), link_for=_link) == "backoff"
    assert len(_calls(env, "agents.conversations.setView")) == 1  # nothing inside the cooldown


@pytest.mark.asyncio
async def test_a_rate_limited_pr_tab_write_waits_out_slacks_retry_after(env: Any) -> None:
    binding = _bind(env)
    env.api.responses["agents.conversations.setView"] = _rate_limited("40")
    await tabs.sync_pr_tabs(binding, [tabs.PrRef(12, PR_URL.format(n=12), "open")])
    env.clock.now += 39
    await tabs.sync_pr_tabs(binding, [tabs.PrRef(12, PR_URL.format(n=12), "open")])
    assert len(_calls(env, "agents.conversations.setView")) == 1


@pytest.mark.asyncio
async def test_a_rate_limited_pr_tab_removal_waits_out_slacks_retry_after(
    env: Any, monkeypatch: Any
) -> None:
    binding = _bind(env)
    _set_views(env)
    _scope_flag(monkeypatch)
    await tabs.sync_pr_tabs(binding, [tabs.PrRef(12, PR_URL.format(n=12), "open")])
    env.api.responses["agents.conversations.removeView"] = _rate_limited("50")
    merged = [tabs.PrRef(12, PR_URL.format(n=12), "merged")]
    await tabs.sync_pr_tabs(binding, [], settled={p.number for p in merged})
    env.clock.now += 49
    await tabs.sync_pr_tabs(binding, [], settled={12})
    assert len(_calls(env, "agents.conversations.removeView")) == 1


def test_an_api_error_carries_slacks_retry_after() -> None:
    from kiro_crew.slack.code_channels.client import CodeChannelsClient
    from kiro_crew.slack.code_channels.models import CodeChannelApiError

    async def api_call(method: str, **_k: Any) -> Any:
        raise _rate_limited("12")

    client = CodeChannelsClient(api_call)
    with pytest.raises(CodeChannelApiError) as caught:
        asyncio.run(client._call("agents.conversations.setView", {"channel_id": CH}))
    assert caught.value.code == "ratelimited" and caught.value.retry_after == 12.0


@pytest.mark.asyncio
async def test_time_passing_alone_rewrites_the_workers_tab_only_every_few_minutes(
    env: Any, monkeypatch: Any
) -> None:
    binding = _bind(env)
    _set_views(env)
    wall = {"now": _NOW}

    class _Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return wall["now"]

    monkeypatch.setattr(render, "datetime", _Clock)
    assert await tabs.publish_workers(binding, _snapshot(), link_for=_link) == "written"
    for _ in range(3):  # three minutes of the elapsed count ticking
        wall["now"] += timedelta(minutes=1)
        env.clock.now += 60
        assert await tabs.publish_workers(binding, _snapshot(), link_for=_link) == ""
    wall["now"] += timedelta(minutes=3)
    env.clock.now += tabs.ELAPSED_REFRESH_SECS
    assert await tabs.publish_workers(binding, _snapshot(), link_for=_link) == "written"
    assert len(_calls(env, "agents.conversations.setView")) == 2


@pytest.mark.asyncio
async def test_a_board_with_no_workers_left_empties_the_tab_once(env: Any) -> None:
    binding = _bind(env)
    _set_views(env)
    assert await tabs.publish_workers(binding, model.BoardSnapshot(), link_for=_link) == ""
    assert _calls(env, "agents.conversations.setView") == []  # no tab yet: none opened
    await tabs.publish_workers(binding, _snapshot(), link_for=_link)
    env.clock.now += 60
    assert await tabs.publish_workers(binding, model.BoardSnapshot(), link_for=_link) == "written"
    sent = _calls(env, "agents.conversations.setView")[-1]
    assert "kc_cc_worker_stop" not in json.dumps(sent["blocks"])
    env.clock.now += 60
    assert await tabs.publish_workers(binding, model.BoardSnapshot(), link_for=_link) == ""


@pytest.mark.asyncio
async def test_a_stop_fence_landing_mid_publish_stops_the_write(env: Any, monkeypatch: Any) -> None:
    binding = _bind(env)
    _set_views(env)
    checks = {"n": 0}

    def stopped(channel: str) -> bool:
        checks["n"] += 1
        return checks["n"] > 1  # Slack's Stop lands after the first fence check

    monkeypatch.setattr(surface, "channel_stopped", stopped)
    assert await tabs.publish_workers(binding, _snapshot(), link_for=_link) == "fenced"
    held = await tabs.sync_pr_tabs(binding, [tabs.PrRef(42, PR_URL.format(n=42), "open")])
    assert held == {} and _calls(env, "agents.conversations.setView") == []


@pytest.mark.asyncio
async def test_a_view_recorded_while_the_list_was_read_is_not_pruned(env: Any) -> None:
    _bind(env)

    async def list_views(channel: str) -> list[dict[str, Any]]:
        # A tool write lands while listViews is in flight.
        await views.record_view(CH, key="report", view_id="Vreport", type="html")
        return []

    client = SimpleNamespace(list_views=list_views)
    assert await views.reconcile(client, CH) == []
    assert views.recorded(surface_store().get(CH), "report") is not None


@pytest.mark.asyncio
async def test_a_refused_workers_tab_never_stops_the_board(env: Any, monkeypatch: Any) -> None:
    from kiro_crew.slack.code_channels import board

    binding = _bind(env)
    env.api.responses["agents.conversations.setView"] = {"ok": False, "error": "invalid_view_type"}
    fake = _Refusing(refuse=0)
    monkeypatch.setattr(surface, "slack", lambda: fake)
    real = SimpleNamespace(**{"client": surface.SurfaceClient(env.api.api_call)})
    monkeypatch.setattr(surface, "client", lambda: real.client)
    publisher.forget(CH)
    assert await board.publish(binding, _snapshot()) == "posted"
    assert len(fake.sent) == 1 and _calls(env, "agents.conversations.setView")
    assert views.recorded(surface_store().get(CH), "workers") is None
    publisher.forget(CH)


# ── 5. Stop from the Workers tab ──────────────────────────────────────────


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


@pytest.fixture
def stop_world(env: Any, monkeypatch: Any) -> Any:
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


@pytest.mark.asyncio
async def test_stop_stops_only_that_workers_turn(env: Any, stop_world: Any) -> None:
    assert await worker_stop._handle(env.orch, _stop_payload()) == "stop:stopping"
    assert stop_world.stopped == [("chat-w1", _STOP_KW)]
    assert stop_world.reads == [CH]  # resolved on THIS channel's own board
    assert stop_world.noted == ["chat-w1"]
    row = env.sel.ops(worker_stop.OPERATION)[-1]
    assert row["outcome"] == "allowed" and "slot=chat-w1" in row["resources"]
    assert env.slack.ephemerals[-1] == (
        CH,
        OWNER,
        "Stopping *Fix login*. The conductor keeps going.",
    )


@pytest.mark.parametrize("view_id", ["VSOMEONEELSE", "Vreport", ""])
@pytest.mark.asyncio
async def test_a_click_from_any_other_view_stops_nothing(
    env: Any, stop_world: Any, view_id: str
) -> None:
    assert await worker_stop._handle(env.orch, _stop_payload(view_id=view_id)) == "denied"
    assert stop_world.stopped == []
    assert env.slack.ephemerals[-1] == (CH, OWNER, worker_stop.NOT_THIS_TAB_TEXT)
    assert env.sel.ops(worker_stop.OPERATION)[-1]["outcome"] == "denied"


@pytest.mark.asyncio
async def test_a_channel_with_no_workers_tab_of_ours_takes_no_stop(
    env: Any, stop_world: Any
) -> None:
    payload = _stop_payload(channel=OTHER_CH, value="it_00000009", view_id=WORKERS_VIEW)
    assert await worker_stop._handle(env.orch, payload) == "denied"
    assert stop_world.stopped == []


@pytest.mark.parametrize(
    "value", ["it_00000009", "chat-w9", "dashboard:chat-w9", "it_0000000c", "it_deadbeef"]
)
@pytest.mark.asyncio
async def test_the_target_must_be_a_worker_on_this_channels_own_board(
    env: Any, stop_world: Any, value: str
) -> None:
    # Another channel's item, a raw session key, the conductor itself, an unknown item.
    assert await worker_stop._handle(env.orch, _stop_payload(value=value)) == "denied"
    assert stop_world.stopped == []
    assert env.slack.ephemerals[-1] == (CH, OWNER, worker_stop.NOT_ON_BOARD_TEXT)


@pytest.mark.asyncio
async def test_a_payload_session_key_is_never_read(env: Any, stop_world: Any) -> None:
    payload = _stop_payload()
    payload["actions"][0]["session_key"] = "chat-w9"
    payload["session_key"] = "chat-w9"
    payload["container"]["session_key"] = "chat-w9"
    await worker_stop._handle(env.orch, payload)
    assert [key for key, _kw in stop_world.stopped] == ["chat-w1"]


@pytest.mark.asyncio
async def test_a_person_the_context_bar_stop_would_refuse_stops_nothing(
    env: Any, stop_world: Any
) -> None:
    assert await worker_stop._handle(env.orch, _stop_payload(user=STRANGER)) == "denied"
    assert stop_world.stopped == []
    assert env.slack.ephemerals[-1] == (CH, STRANGER, actions.NOT_AUTHORIZED_TEXT)


@pytest.mark.asyncio
async def test_the_stop_uses_the_context_bar_stops_own_rule(
    env: Any, stop_world: Any, monkeypatch: Any
) -> None:
    asked: list[tuple[str, str]] = []

    def may_stop(orch: Any, binding: Binding, user: str) -> bool:
        asked.append((binding.channel_id, user))
        return False

    monkeypatch.setattr(actions, "_may_stop", may_stop)
    assert await worker_stop._handle(env.orch, _stop_payload()) == "denied"
    assert asked == [(CH, OWNER)] and stop_world.stopped == []


@pytest.mark.asyncio
async def test_a_stop_in_a_channel_the_policy_refuses_stops_nothing(
    env: Any, stop_world: Any, monkeypatch: Any
) -> None:
    from slack_code_channel_policy_helpers import allow_only, policy_refusals

    rows = allow_only(monkeypatch, OTHER_CH)  # allowed_channel_ids leaves this channel out
    assert await worker_stop._handle(env.orch, _stop_payload()) == "denied"
    assert stop_world.stopped == [] and stop_world.reads == []  # refused at the entry gate
    assert env.slack.ephemerals == []  # dropped without a reply
    assert policy_refusals(rows, worker_stop.OPERATION)  # the policy seam's audit row


@pytest.mark.asyncio
async def test_a_channel_refused_during_the_board_read_stops_nothing(
    env: Any, stop_world: Any, monkeypatch: Any
) -> None:
    from slack_code_channel_policy_helpers import allow_only, policy_refusals

    real_read = source.read_board
    rows: dict[str, Any] = {}

    async def read_board(state: Any, b: Binding) -> Any:
        rows["sel"] = allow_only(monkeypatch, OTHER_CH)  # the ceiling changes meanwhile
        return await real_read(state, b)

    monkeypatch.setattr(source, "read_board", read_board)
    assert await worker_stop._handle(env.orch, _stop_payload()) == "denied"
    assert stop_world.stopped == [] and env.slack.ephemerals == []  # no reply either
    assert policy_refusals(rows["sel"], worker_stop.OPERATION)


@pytest.mark.asyncio
async def test_the_final_gate_asks_the_channel_ceiling_again(
    env: Any, stop_world: Any, monkeypatch: Any
) -> None:
    from slack_code_channel_policy_helpers import allow_only

    # The workspace re-check reads the installed context, which a click's entry
    # gate installs; this test calls the gate alone.
    monkeypatch.setattr(worker_stop, "_workspace_still_admitted", lambda team: True)
    from kiro_crew.platform.context import current_context

    current_context()  # installed, as the click's entry gate leaves it
    click, view_id, _value = worker_stop.parse(_stop_payload())
    cfg = worker_stop.live_config(env.orch)
    gate = lambda: worker_stop._final_gate(  # noqa: E731
        env.orch, env.state, stop_world.binding, click, view_id, "chat-w1", cfg=cfg
    )
    from kiro_crew.slack.code_channels.surface_store import get_async

    await get_async(CH)
    assert not isinstance(gate(), str)  # admitted while the ceiling names the channel
    rows = allow_only(monkeypatch, OTHER_CH)
    assert gate() == worker_stop._CHANNEL_REFUSED
    assert rows == []  # re-read unaudited: the entry gate wrote this decision's row


@pytest.mark.asyncio
async def test_a_context_bar_click_in_a_channel_the_policy_refuses_is_not_admitted(
    env: Any, monkeypatch: Any
) -> None:
    from slack_code_channel_policy_helpers import allow_only, policy_refusals

    _bind(env)
    rows = allow_only(monkeypatch, OTHER_CH)
    click, _view, _value = worker_stop.parse(_stop_payload())
    replies: list[str] = []

    async def respond(text: str) -> None:
        replies.append(text)

    assert await actions.admit(click, respond, f"channel={CH}") is None
    assert policy_refusals(rows, actions.OPERATION) and replies == []


@pytest.mark.parametrize("variant", ["team", "inbound", "unbound"])
@pytest.mark.asyncio
async def test_a_stop_past_any_channel_gate_does_nothing(
    env: Any, stop_world: Any, monkeypatch: Any, variant: str
) -> None:
    payload = _stop_payload()
    if variant == "team":
        monkeypatch.setattr(surface, "workspace_admitted", lambda team: False)
    elif variant == "inbound":
        env.inbound_ok = False
    else:
        payload["channel"] = {"id": "C0UNBOUND"}
    assert await worker_stop._handle(env.orch, payload) == "denied"
    assert stop_world.stopped == []


@pytest.mark.asyncio
async def test_a_double_click_and_a_redelivery_stop_once(env: Any, stop_world: Any) -> None:
    assert (await worker_stop._handle(env.orch, _stop_payload(ts="1.1"))).startswith("stop:")
    assert await worker_stop._handle(env.orch, _stop_payload(ts="1.1")) == "duplicate"
    assert await worker_stop._handle(env.orch, _stop_payload(ts="1.2")) == "repeat"
    assert len(stop_world.stopped) == 1


@pytest.mark.asyncio
async def test_a_worker_that_is_not_running_is_said_so_and_not_stopped(
    env: Any, stop_world: Any
) -> None:
    assert await worker_stop._handle(env.orch, _stop_payload(value="it_00000002")) == "idle"
    assert stop_world.stopped == []
    assert env.slack.ephemerals[-1] == (CH, OWNER, "*Idle one* isn't running.")


@pytest.mark.asyncio
async def test_with_code_channels_off_the_stop_is_not_handled(env: Any, stop_world: Any) -> None:
    env.on = False
    assert await worker_stop.handle_stop_click(env.orch, _stop_payload()) is False
    assert stop_world.stopped == []


@pytest.mark.asyncio
async def test_the_interactive_router_answers_the_workers_tab_buttons(
    env: Any, monkeypatch: Any
) -> None:
    from kiro_crew.slack import interactions

    got: list[Any] = []

    async def stop_click(orch: Any, payload: dict[str, Any]) -> bool:
        got.append(payload["actions"][0]["action_id"])
        return False  # even "not ours" must not fall through

    async def approval(*a: Any, **k: Any) -> None:
        got.append("fell-through")

    monkeypatch.setattr(worker_stop, "handle_stop_click", stop_click)
    monkeypatch.setattr(interactions, "_handle_tool_approval", approval)
    monkeypatch.setattr(interactions, "is_allowed_user", lambda u: True)
    stop_click_payload = _stop_payload()
    # With a message ts, a fall-through would reach the tool-approval handler.
    stop_click_payload["message"] = {"ts": "1.0"}
    await interactions.dispatch(stop_click_payload)
    open_click = _stop_payload()
    open_click["message"] = {"ts": "1.0"}
    open_click["actions"][0]["action_id"] = "kc_cc_worker_open"
    await interactions.dispatch(open_click)
    assert got == ["kc_cc_worker_stop"]


# ── 6. The final gate: nothing decided before the board read is trusted after it ──


@pytest.mark.asyncio
async def test_a_worker_slot_whose_running_turn_is_the_conductors_is_never_stopped(
    env: Any, stop_world: Any
) -> None:
    # The worker's slot was rebound mid-turn: the turn it would cancel is the
    # conductor's own session.
    stop_world.slots["chat-w1"]._active_turn_session_key = f"dashboard_{SLOT}"
    assert await worker_stop._handle(env.orch, _stop_payload()) == "denied"
    assert stop_world.stopped == []
    row = env.sel.ops(worker_stop.OPERATION)[-1]
    assert row["outcome"] == "denied" and row["error"] == "the cancel target is not that worker"


@pytest.mark.asyncio
async def test_a_running_turn_of_another_slot_is_never_stopped(env: Any, stop_world: Any) -> None:
    stop_world.slots["chat-w1"]._active_turn_session_key = "dashboard_chat-w9"
    assert await worker_stop._handle(env.orch, _stop_payload()) == "denied"
    assert stop_world.stopped == []


@pytest.mark.parametrize(
    "change",
    ["revoked", "rebound", "dropped", "archived", "view", "code_off", "team", "inbound"],
)
@pytest.mark.asyncio
async def test_what_changes_while_the_board_is_read_is_judged_again(
    env: Any, stop_world: Any, monkeypatch: Any, change: str
) -> None:
    real = source.read_board
    # The gateway's one config object, which a reload updates in place.
    shared = env.cfg()
    for mod in (worker_stop, actions):
        monkeypatch.setattr(mod, "live_config", lambda _orch=None: shared)

    async def read_board(state: Any, b: Binding) -> Any:
        # Every gate actions.admit passed is revoked mid-read in one variant.
        if change == "code_off":
            shared.slack.code_channels = False
        elif change == "team":
            from kiro_crew.platform.context import installed_context

            gate = installed_context().slack_gate
            monkeypatch.setattr(gate, "check_message_origin", lambda team: False)
        elif change == "inbound":
            env.inbound_ok = False
        elif change == "revoked":
            env.allowed.discard(OWNER)  # the presser loses access meanwhile
        elif change == "rebound":
            env.store.replace_binding(Binding(**{**b.__dict__, "session_key": "chat-new-1"}))
        elif change == "dropped":
            env.store.drop_binding(CH)
        elif change == "archived":
            surface.fence_archived(CH)
        else:
            surface_store().update(CH, views=[_v("workers", "block_kit", "2")])
        return await real(state, b)

    monkeypatch.setattr(source, "read_board", read_board)
    assert await worker_stop._handle(env.orch, _stop_payload()) == "denied"
    assert stop_world.stopped == []


@pytest.mark.asyncio
async def test_a_worker_the_store_says_another_conductor_made_is_not_stopped(
    env: Any, stop_world: Any
) -> None:
    env.store.record_worker("chat-w1", "chat-9-1")
    assert await worker_stop._handle(env.orch, _stop_payload()) == "denied"
    assert stop_world.stopped == []


@pytest.mark.asyncio
async def test_a_container_without_its_own_view_id_is_refused(env: Any, stop_world: Any) -> None:
    payload = _stop_payload()
    payload["container"] = {"type": "view", "tab_id": WORKERS_VIEW}
    assert await worker_stop._handle(env.orch, payload) == "denied"
    assert stop_world.stopped == []


@pytest.mark.asyncio
async def test_a_declined_stop_is_said_as_such_and_can_be_pressed_again(
    env: Any, stop_world: Any
) -> None:
    stop_world.answers.append({"ok": True, "info": "compacting", "compacting": True})
    assert await worker_stop._handle(env.orch, _stop_payload(ts="1.1")) == "stop:compacting"
    assert "compacting" in env.slack.ephemerals[-1][2]
    # Not "Already stopping": the next press stops it.
    assert await worker_stop._handle(env.orch, _stop_payload(ts="1.2")) == "stop:stopping"
    stop_world.answers.append({"ok": True, "info": "no active turn"})
    assert await worker_stop._handle(env.orch, _stop_payload(ts="9.1")) == "repeat"


@pytest.mark.asyncio
async def test_a_stop_that_errors_is_audited_and_told(
    env: Any, stop_world: Any, monkeypatch: Any
) -> None:
    async def boom(*a: Any, **k: Any) -> dict[str, Any]:
        raise RuntimeError("provider gone")

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", boom)
    assert await worker_stop.handle_stop_click(env.orch, _stop_payload()) is True
    row = env.sel.ops(worker_stop.OPERATION)[-1]
    assert row["outcome"] == "error" and row["caller"] == OWNER
    assert env.slack.ephemerals[-1] == (CH, OWNER, worker_stop.FAILED_TEXT)


@pytest.mark.asyncio
async def test_a_repeat_and_a_missing_dashboard_are_audited(
    env: Any, stop_world: Any, monkeypatch: Any
) -> None:
    await worker_stop._handle(env.orch, _stop_payload(ts="1.1"))
    await worker_stop._handle(env.orch, _stop_payload(ts="1.2"))
    assert "result=repeat" in env.sel.ops(worker_stop.OPERATION)[-1]["resources"]
    monkeypatch.setattr(surface, "dashboard_state", lambda: None)
    assert await worker_stop._handle(env.orch, _stop_payload(ts="2.1")) == "denied"
    assert env.sel.ops(worker_stop.OPERATION)[-1]["error"] == "no dashboard"


@pytest.mark.asyncio
async def test_a_pr_tab_that_gives_way_to_the_diff_reruns_the_refresh(monkeypatch: Any) -> None:
    from kiro_crew.slack.code_channels.models import CodeChannelApiError
    from kiro_crew.slack.code_channels.workspace import service as svc

    calls = {"n": 0}

    class _Client:
        async def set_diff_view(self, channel: str, *a: Any, **k: Any) -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise CodeChannelApiError("agents.conversations.setView", "too_many_views")
            return "Vdiff"

    async def make_room(client: Any, channel: str, *, for_key: str, **_kw: Any) -> str:
        assert for_key == "diff"
        return "pr-12"

    monkeypatch.setattr(views, "make_room", make_room)
    service = svc.WorkspaceService(lambda: _Client())
    record = SimpleNamespace(channel_id=CH, key=f"T1:{CH}", session_key=SLOT, slack_off="")
    assert await service._call("set_diff_view", record, "diff --git", room_for="diff") == "Vdiff"
    assert record.key in service._again


# ── 7. Stopped by @user, until the worker's next turn ─────────────────────


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


@pytest.mark.asyncio
async def test_a_workers_tab_stop_shows_who_stopped_it_on_the_board_and_the_tab(
    env: Any, stop_world: Any
) -> None:
    stop_world.slots["chat-w1"]._turn_generation = 7
    assert await worker_stop._handle(env.orch, _stop_payload()) == "stop:stopping"
    # Still unwinding (the turn reads as running) or already idle: either way it
    # reads as stopped, and offers no Stop.
    for live in (model.LIVE_RUNNING, model.LIVE_IDLE):
        row = _project(env.state, live).rows[0]
        assert (
            row.stopped and row.stopped_by == OWNER and re.fullmatch(r"\d\d:\d\d", row.stopped_at)
        )
        blocks = render.workers_view_blocks(
            model.BoardSnapshot(rows=(row,)), _link, tabs=_TABS, now=_NOW
        )
        by_id = {b.get("block_id"): b for b in blocks}
        line = by_id["worker-it_00000001-text"]["elements"][0]["elements"]
        assert {"type": "user", "user_id": OWNER} in line
        assert _texts(by_id["worker-it_00000001-text"]).startswith("Fix login\nStopped by ")
        assert [e["action_id"] for e in by_id["worker-it_00000001"]["elements"]] == [
            "kc_cc_worker_open"
        ]
        board = render.board_blocks(model.BoardSnapshot(rows=(row,)), _link, now=_NOW)
        details = board[-1]["tasks"][0]["details"]["elements"][0]["elements"]
        assert details[:2] == [
            {"type": "text", "text": "Stopped by "},
            {"type": "user", "user_id": OWNER},
        ]
        rows_look = render.board_blocks(
            model.BoardSnapshot(rows=(row,)), _link, look="rows", now=_NOW
        )
        assert f"Stopped by <@{OWNER}> · {row.stopped_at}" in json.dumps(
            rows_look, ensure_ascii=False
        )


@pytest.mark.asyncio
async def test_the_stopped_line_clears_when_the_workers_next_turn_starts(
    env: Any, stop_world: Any
) -> None:
    stop_world.slots["chat-w1"]._turn_generation = 7
    await worker_stop._handle(env.orch, _stop_payload())
    assert _project(env.state).rows[0].stopped
    stop_world.slots["chat-w1"]._turn_generation = 8  # its next turn started
    row = _project(env.state, model.LIVE_RUNNING).rows[0]
    assert not row.stopped and row.stopped_by == ""
    stop_world.slots["chat-w1"]._turn_generation = 7  # never comes back
    assert not _project(env.state).rows[0].stopped


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


@pytest.mark.parametrize("closed_first", [False, True])
@pytest.mark.asyncio
async def test_a_resumed_worker_on_a_new_slot_inherits_no_stop(
    env: Any, stop_world: Any, closed_first: bool
) -> None:
    from kiro_crew.slack.code_channels.board import stops

    stop_world.slots["chat-w1"]._turn_generation = 1
    assert await worker_stop._handle(env.orch, _stop_payload()) == "stop:stopping"
    assert _project(env.state).rows[0].stopped
    if closed_first:
        # The slot closed and a board read saw it gone before the resume.
        old = stop_world.slots.pop("chat-w1")
        assert stops.current(env.state, "chat-w1") is None
        stop_world.slots["chat-w1"] = old
    # Resumed: a NEW slot, whose first turn is generation 1 again, is running.
    _resumed(stop_world, 1)
    row = _project(env.state, model.LIVE_RUNNING).rows[0]
    assert not row.stopped and row.stopped_by == "" and row.state == model.WORKING
    blocks = render.workers_view_blocks(model.BoardSnapshot(rows=(row,)), _link, now=_NOW)
    by_id = {b.get("block_id"): b for b in blocks}
    assert "kc_cc_worker_stop" in [e["action_id"] for e in by_id["worker-it_00000001"]["elements"]]
    # A snapshot read while the old stop showed is reconciled to the new slot's truth.
    stale = model.BoardSnapshot(
        rows=(_row(session_key="chat-w1", stopped_by=OWNER, stopped_at="13:31"),)
    )
    assert stops.reconcile(stale, env.state).rows[0].stopped_by == ""
    assert stops.take_urgent(["chat-w1"]) is False


@pytest.mark.asyncio
async def test_a_stop_of_the_resumed_slot_is_not_answered_as_a_repeat(
    env: Any, stop_world: Any
) -> None:
    stop_world.slots["chat-w1"]._turn_generation = 1
    assert await worker_stop._handle(env.orch, _stop_payload(ts="1.1")) == "stop:stopping"
    _resumed(stop_world, 1)  # within the repeat window, on a new slot's first turn
    assert await worker_stop._handle(env.orch, _stop_payload(ts="1.2")) == "stop:stopping"
    assert len(stop_world.stopped) == 2


@pytest.mark.asyncio
async def test_an_expired_repeat_mark_does_not_keep_its_slot(
    env: Any, stop_world: Any, monkeypatch: Any
) -> None:
    from kiro_crew.testing.clock import ManualClock

    # On worker_stop's own `time` binding only: the event loop keeps its clock.
    clock = ManualClock()
    clock.install(monkeypatch, worker_stop)
    assert await worker_stop._handle(env.orch, _stop_payload(ts="1.1")) == "stop:stopping"
    old = stop_world.slots["chat-w1"]
    assert any(v[2] is old for v in worker_stop._recent.values())
    clock.advance(worker_stop._REPEAT_SECS)
    stop_world.slots["chat-w2"].running = True  # a Stop of ANOTHER worker, later
    payload = _stop_payload(ts="2.1", value="it_00000002")
    assert await worker_stop._handle(env.orch, payload) == "stop:stopping"
    assert not any(v[2] is old for v in worker_stop._recent.values())


def test_a_stop_is_forgotten_once_its_slot_is_gone() -> None:
    from kiro_crew.slack.code_channels.board import stops

    stops.reset_for_tests()
    slots: dict[str, Any] = {"chat-w1": SimpleNamespace(key="chat-w1", _turn_generation=3)}
    state = SimpleNamespace(get_slot=slots.get)
    stops.note(slots["chat-w1"], OWNER, generation=3)
    assert stops.current(state, "chat-w1") is not None
    old = slots.pop("chat-w1")
    assert stops.current(state, "chat-w1") is None
    slots["chat-w1"] = old  # even the same object coming back: the note is gone
    assert stops.current(state, "chat-w1") is None and stops.take_urgent(["chat-w1"]) is False


@pytest.mark.asyncio
async def test_a_refused_or_declined_stop_marks_nobody(env: Any, stop_world: Any) -> None:
    await worker_stop._handle(env.orch, _stop_payload(user=STRANGER))
    stop_world.answers.append({"ok": True, "info": "compacting", "compacting": True})
    await worker_stop._handle(env.orch, _stop_payload(ts="2.1"))
    assert not _project(env.state).rows[0].stopped


@pytest.mark.asyncio
async def test_a_fresh_stop_skips_both_edit_gaps_once(env: Any, monkeypatch: Any) -> None:
    from kiro_crew.slack.code_channels import board
    from kiro_crew.slack.code_channels.board import stops

    binding = _bind(env)
    _set_views(env)
    slot = SimpleNamespace(key="chat-w1", _turn_generation=3)
    env.state.get_slot = lambda key: slot if key == "chat-w1" else None
    fake = _Refusing(refuse=0)
    fake.updates: list[Any] = []  # type: ignore[misc]

    async def update_message(channel: str, ts: str, text: str = "", blocks: Any = None) -> None:
        fake.updates.append(blocks)

    fake.update_message = update_message  # type: ignore[method-assign]
    monkeypatch.setattr(surface, "slack", lambda: fake)
    real = surface.SurfaceClient(env.api.api_call)
    monkeypatch.setattr(surface, "client", lambda: real)
    clock = {"now": 5000.0}
    monkeypatch.setattr(publisher, "_clock", lambda: clock["now"])
    publisher.forget(CH)
    working = model.BoardSnapshot(rows=(_row(session_key="chat-w1"),))
    assert await board.publish(binding, working) == "posted"
    clock["now"] += 1
    env.clock.now += 1
    stops.note(slot, OWNER, generation=3)
    stopped = model.BoardSnapshot(
        rows=(_row(session_key="chat-w1", stopped_by=OWNER, stopped_at="13:31"),)
    )
    # One second after the last edit, well inside both gaps: still published now.
    assert await board.publish(binding, stopped) == "updated"
    sent = _calls(env, "agents.conversations.setView")
    assert len(sent) == 2 and "Stopped by" in json.dumps(sent[-1]["blocks"])
    # The mark is spent: the next change inside the gap waits as usual.
    clock["now"] += 1
    env.clock.now += 1
    later = model.BoardSnapshot(
        rows=(_row(session_key="chat-w1", stopped_by=OWNER, stopped_at="13:31", title="x"),)
    )
    assert await board.publish(binding, later) == "deferred"
    publisher.forget(CH)


@pytest.mark.asyncio
async def test_stop_then_refresh_reads_stopped_on_board_and_tab_until_the_next_turn(
    env: Any, stop_world: Any, monkeypatch: Any
) -> None:
    """Stop -> the refresh it schedules -> "Stopped by" on the board and the tab ->
    the worker's next turn clears it, through the production board publisher."""
    from kiro_crew.slack.code_channels import board

    stop_world.slots["chat-w1"]._turn_generation = 4
    fake = _Refusing(refuse=0)
    posted: list[Any] = []

    async def update_message(channel: str, ts: str, text: str = "", blocks: Any = None) -> None:
        posted.append(blocks)

    fake.update_message = update_message  # type: ignore[method-assign]
    monkeypatch.setattr(surface, "slack", lambda: fake)
    real = surface.SurfaceClient(env.api.api_call)
    monkeypatch.setattr(surface, "client", lambda: real)
    _set_views(env)
    clock = {"now": 9000.0}
    monkeypatch.setattr(publisher, "_clock", lambda: clock["now"])
    publisher.forget(CH)

    def refresh(live: str) -> Any:
        # What the scheduled refresh reads: the board's own projection.
        return board.publish(stop_world.binding, _project(env.state, live))

    assert await refresh(model.LIVE_RUNNING) == "posted"
    clock["now"] += 1
    env.clock.now += 1
    # The click comes from the Workers tab as the gateway recorded it just now.
    payload = _stop_payload(view_id=worker_stop._workers_view(CH))
    assert await worker_stop._handle(env.orch, payload) == "stop:stopping"
    assert stop_world.noted == ["chat-w1"]  # the refresh path was armed
    # The refresh lands inside both edit gaps and is still published at once.
    assert await refresh(model.LIVE_IDLE) == "updated"
    board_text = json.dumps(posted[-1])
    tab = _calls(env, "agents.conversations.setView")[-1]
    for text in (board_text, json.dumps(tab["blocks"])):
        assert '"text": "Stopped by "' in text and f'"user_id": "{OWNER}"' in text
    assert "kc_cc_worker_stop" not in json.dumps(tab["blocks"])
    # The worker's next turn starts: the next refresh omits Stopped.
    stop_world.slots["chat-w1"]._turn_generation = 5
    clock["now"] += publisher.MIN_EDIT_GAP_SECS + 1
    env.clock.now += tabs.WORKERS_MIN_GAP_SECS + 1
    assert await refresh(model.LIVE_RUNNING) == "updated"
    assert "Stopped by" not in json.dumps(posted[-1])
    tab = _calls(env, "agents.conversations.setView")[-1]
    assert "Stopped by" not in json.dumps(tab["blocks"])
    assert "kc_cc_worker_stop" in json.dumps(tab["blocks"])  # Stop is offered again
    publisher.forget(CH)


@pytest.mark.asyncio
async def test_newer_unseen_pr_tabs_are_not_removed_for_older_confirmed_prs(env: Any) -> None:
    binding = _bind(env)
    _set_views(env)
    newer = [
        tabs.PrRef(100, PR_URL.format(n=100), "open"),
        tabs.PrRef(90, PR_URL.format(n=90), "open"),
    ]
    await tabs.sync_pr_tabs(binding, newer)
    # This pass only confirmed two OLDER open PRs; #100 and #90 were not read.
    older = [
        tabs.PrRef(20, PR_URL.format(n=20), "open"),
        tabs.PrRef(10, PR_URL.format(n=10), "open"),
    ]
    held = await tabs.sync_pr_tabs(binding, older)
    assert _calls(env, "agents.conversations.removeView") == []
    assert held == {100: "Vpr100", 90: "Vpr90"}
    assert len(_calls(env, "agents.conversations.setView")) == 2  # no third PR tab


_TRAILING = ["\n", "\r\n", " ", "\t", "\x00", "é"]


@pytest.mark.parametrize("tail", _TRAILING)
def test_slack_ids_are_refused_with_anything_trailing(tail: str) -> None:
    assert render.tab_element(CH, "VPR12") is not None
    assert render.tab_element(CH + tail, "VPR12") is None
    assert render.tab_element(CH, "VPR12" + tail) is None


@pytest.mark.parametrize("tail", _TRAILING)
def test_a_stoppers_user_id_is_refused_with_anything_trailing(tail: str) -> None:
    from kiro_crew.slack.code_channels.board import stops

    stops.reset_for_tests()
    slot = SimpleNamespace(key="chat-w1", _turn_generation=1)
    state = SimpleNamespace(get_slot=lambda key: slot if key == "chat-w1" else None)
    stops.note(slot, "U123ABC" + tail, generation=1)
    assert stops.current(state, "chat-w1") is None
    stops.note(slot, "U123ABC", generation=1)
    assert stops.current(state, "chat-w1") is not None


@pytest.mark.parametrize("tail", _TRAILING)
@pytest.mark.asyncio
async def test_a_stop_item_id_is_refused_with_anything_trailing(
    env: Any, stop_world: Any, tail: str
) -> None:
    payload = _stop_payload(value="it_00000001" + tail)
    assert await worker_stop._handle(env.orch, payload) == "denied"
    assert stop_world.stopped == [] and stop_world.reads == []  # refused before the board read
    assert "not a work item" in env.sel.ops(worker_stop.OPERATION)[-1].values()


@pytest.mark.parametrize(
    "value, ok",
    [
        ("U123ABC", True),
        ("W12", True),
        ("U1", False),  # too short
        ("u123ABC", False),
        ("U123abc", False),
        ("X123ABC", False),
        ("U" + "A" * 31, False),  # too long
        ("U123ABC\n", False),
        ("", False),
    ],
)
def test_user_ids_have_the_exact_shape(value: str, ok: bool) -> None:
    from kiro_crew.slack.code_channels.board import stops

    assert stops._is_user_id(value) is ok


@pytest.mark.parametrize(
    "value, ok",
    [
        ("it_00000001", True),
        ("it_" + "a" * 32, True),
        ("it_0000001", False),  # too short
        ("it_" + "a" * 33, False),
        ("it_0000000G", False),
        ("it_0000000A", False),  # upper-case hex is not minted
        ("IT_00000001", False),
        ("it_00000001\n", False),
        ("", False),
    ],
)
def test_item_ids_have_the_exact_shape(value: str, ok: bool) -> None:
    assert worker_stop._is_item_id(value) is ok


@pytest.mark.asyncio
async def test_a_pass_whose_board_read_failed_removes_no_workers_pr_tab(
    env: Any, monkeypatch: Any
) -> None:
    binding = _bind(env)
    _set_views(env)
    _scope_flag(monkeypatch)
    workers = [
        tabs.PrRef(11, PR_URL.format(n=11), "open"),
        tabs.PrRef(10, PR_URL.format(n=10), "open"),
    ]
    await tabs.sync_pr_tabs(binding, workers)
    # The board read fails: no rows, so only the channel's own (newer) PR is seen.
    own = tabs.PrRef(12, PR_URL.format(n=12), "open")
    held = await tabs.sync_pr_tabs(
        binding, tabs.wanted_prs(own, ()), settled=tabs.settled_prs(own, ()), own=12
    )
    assert _calls(env, "agents.conversations.removeView") == []
    assert held == {11: "Vpr11", 10: "Vpr10"}  # #12 waits for a free slot
    # Two NEWER PRs seen open do take the slots: the unseen tabs give way then.
    seen = [
        tabs.PrRef(13, PR_URL.format(n=13), "open"),
        tabs.PrRef(12, PR_URL.format(n=12), "open"),
    ]
    held = await tabs.sync_pr_tabs(binding, seen, own=12)
    removed = sorted(c["view_id"] for c in _calls(env, "agents.conversations.removeView"))
    assert removed == ["Vpr10", "Vpr11"] and set(held) == {13, 12}


@pytest.mark.asyncio
async def test_the_stop_reads_the_surface_store_only_off_the_loop_or_from_memory(
    env: Any, stop_world: Any, monkeypatch: Any
) -> None:
    from kiro_crew.slack.code_channels import surface_store as ss

    def on_the_loop(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a blocking SurfaceStore.get on the event loop")

    real_get = ss.SurfaceStore.get

    async def get_async(channel_id: str) -> Any:  # what the coroutine path uses
        import asyncio

        return await asyncio.to_thread(real_get, ss.store(), channel_id)

    monkeypatch.setattr(ss, "get_async", get_async)
    monkeypatch.setattr(ss.SurfaceStore, "get", on_the_loop)
    assert await worker_stop._handle(env.orch, _stop_payload()) == "stop:stopping"


def test_an_unloaded_surface_store_answers_nothing_from_memory(tmp_path: Path) -> None:
    fresh = SurfaceStore(tmp_path / "cold.json")
    assert fresh.loaded_get(CH) is None  # never reads the disk to answer


# ── Green-round review fixes ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_press_while_a_stop_is_already_in_flight_credits_nobody(
    env: Any, stop_world: Any
) -> None:
    stop_world.slots["chat-w1"]._turn_generation = 7
    stop_world.answers.append({"ok": True, "info": "already stopping", "already_stopping": True})
    assert await worker_stop._handle(env.orch, _stop_payload()) == "stop:already"
    row = _project(env.state).rows[0]
    assert not row.stopped and row.stopped_by == ""
    blocks = render.workers_view_blocks(
        model.BoardSnapshot(rows=(row,)), _link, tabs=_TABS, now=_NOW
    )
    assert "Stopped by" not in json.dumps(blocks, ensure_ascii=False)


@pytest.mark.asyncio
async def test_a_refused_pr_view_type_stops_the_rest_of_the_same_pass(env: Any) -> None:
    binding = _bind(env)
    env.api.responses["agents.conversations.setView"] = {
        "ok": False,
        "error": "invalid_view_type",
    }
    wanted = [
        tabs.PrRef(42, PR_URL.format(n=42), "open"),
        tabs.PrRef(12, PR_URL.format(n=12), "open"),
    ]
    assert await tabs.sync_pr_tabs(binding, wanted) == {}
    assert len(_calls(env, "agents.conversations.setView")) == 1


@pytest.mark.asyncio
async def test_a_failed_removal_never_lets_a_third_pr_tab_open(env: Any) -> None:
    binding = _bind(env)
    _set_views(env)
    await tabs.sync_pr_tabs(
        binding,
        [
            tabs.PrRef(100, PR_URL.format(n=100), "open"),
            tabs.PrRef(90, PR_URL.format(n=90), "open"),
        ],
    )
    env.api.responses["agents.conversations.removeView"] = {"ok": False, "error": "internal_error"}
    held = await tabs.sync_pr_tabs(
        binding,
        [
            tabs.PrRef(110, PR_URL.format(n=110), "open"),
            tabs.PrRef(100, PR_URL.format(n=100), "open"),
        ],
    )
    assert len(_calls(env, "agents.conversations.removeView")) == 1
    assert len(_calls(env, "agents.conversations.setView")) == 2  # #110 waits for room
    assert held == {100: "Vpr100", 90: "Vpr90"}


def test_only_pr_digit_keys_are_reserved_for_pr_tabs() -> None:
    assert views.reserved_key("pr-12")
    assert not views.reserved_key("pr-summary")
    assert not views.reserved_key("pr-")
    assert not views.reserved_key("pr-²")


@pytest.mark.asyncio
async def test_the_agent_may_write_a_view_under_a_pr_word_key(env: Any) -> None:
    _bind(env)
    _set_views(env)
    out = await tools.run_tool(
        env.state,
        SESSION,
        tools.SET_VIEW,
        {"type": "html", "view_key": "pr-summary", "content": "<html/>"},
    )
    assert out.ok, out.text
    assert [c["view_key"] for c in _calls(env, "agents.conversations.setView")] == ["pr-summary"]
    refused = await tools.run_tool(
        env.state, SESSION, tools.SET_VIEW, {"type": "html", "view_key": "pr-7", "content": "x"}
    )
    assert not refused.ok and "'pr-<number>'" in refused.text


# ── Fences asked again right before every removal ──────────────────────────


@pytest.mark.asyncio
async def test_a_fence_landing_after_the_relist_removes_no_pr_tab(
    env: Any, monkeypatch: Any
) -> None:
    binding = _bind(env)
    _set_views(env)
    both = [
        tabs.PrRef(42, PR_URL.format(n=42), "open"),
        tabs.PrRef(12, PR_URL.format(n=12), "open"),
    ]
    await tabs.sync_pr_tabs(binding, both)

    async def relist(client: Any, channel: str, record: Any) -> Any:
        surface.fence_archived(CH)  # archived while Slack's list was read
        return record

    monkeypatch.setattr(tabs, "_relist", relist)
    await tabs.sync_pr_tabs(binding, [tabs.PrRef(42, PR_URL.format(n=42), "open")], settled={12})
    assert _calls(env, "agents.conversations.removeView") == []
    assert views.recorded(surface_store().get(CH), "pr-12") is not None


@pytest.mark.asyncio
async def test_a_fence_landing_while_making_room_removes_nothing(env: Any) -> None:
    _bind(env)
    surface_store().update(CH, views=[_v("report", "html", "9")])
    listed = [{"view_id": "Vreport"}] + [{"view_id": f"Vtheirs{n}"} for n in range(4)]
    _set_views(env, listed, refuse="too_many_views")
    real_list = env.api.responses["agents.conversations.listViews"]

    def list_views(body: dict[str, Any]) -> dict[str, Any]:
        surface.fence_archived(CH)  # archived between the refusal and the removal
        return real_list(body)

    env.api.responses["agents.conversations.listViews"] = list_views
    snap = model.BoardSnapshot(rows=(_row(session_key="chat-w1"),))
    assert await tabs.publish_workers(_bind(env), snap, link_for=_link) != "written"
    assert _calls(env, "agents.conversations.removeView") == []
    assert views.recorded(surface_store().get(CH), "report") is not None


@pytest.mark.asyncio
async def test_the_final_gate_resolves_no_config_and_composes_no_context(
    env: Any, stop_world: Any, monkeypatch: Any
) -> None:
    import kiro_crew.platform as platform_pkg
    from kiro_crew.platform import context as platform_context

    real = source.read_board
    reached: list[str] = []

    def loader(name: str) -> Any:
        def _reached(*_a: Any, **_k: Any) -> Any:
            reached.append(name)
            raise AssertionError(f"{name} reached on the way to the stop")

        return _reached

    async def read_board(state: Any, b: Binding) -> Any:
        snapshot = await real(state, b)
        # From here to the stop, nothing may resolve config or compose a context.
        # The channel ceiling's re-reads may read the context the entry gate
        # installed (an attribute read); composing one is what is refused.
        installed = platform_context.installed_context()
        assert installed is not None
        composing = loader("current_context")

        def current_context() -> Any:
            return platform_context._ACTIVE or composing()

        for mod in (worker_stop, actions):
            monkeypatch.setattr(mod, "live_config", loader("live_config"))
        monkeypatch.setattr(platform_context, "current_context", current_context)
        monkeypatch.setattr(platform_pkg, "current_context", current_context)
        return snapshot

    monkeypatch.setattr(source, "read_board", read_board)
    assert (await worker_stop._handle(env.orch, _stop_payload())).startswith("stop:")
    assert reached == [] and len(stop_world.stopped) == 1


@pytest.mark.parametrize("when", ["during_stop", "during_reply"])
@pytest.mark.asyncio
async def test_a_queued_turn_that_starts_after_the_stop_is_never_marked_stopped(
    env: Any, stop_world: Any, monkeypatch: Any, when: str
) -> None:
    slot = stop_world.slots["chat-w1"]
    slot._turn_generation = 7
    if when == "during_stop":

        async def stop_slot_turn(state: Any, s: Any, **kw: Any) -> dict[str, Any]:
            stop_world.stopped.append((s.key, kw))
            s._turn_generation += 1  # the cancelled turn ended; a queued one started
            return {"info": ""}

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", stop_slot_turn)
    else:
        real_responder = worker_stop._responder

        def responder(click: Any, admitted: Any) -> Any:
            send = real_responder(click, admitted)

            async def _send(text: str) -> None:
                slot._turn_generation += 1  # the queued turn starts mid-reply
                await send(text)

            return _send

        monkeypatch.setattr(worker_stop, "_responder", responder)
    assert (await worker_stop._handle(env.orch, _stop_payload())).startswith("stop:")
    assert len(stop_world.stopped) == 1
    # The running turn is the queued follow-up: nobody stopped it.
    row = _project(env.state, model.LIVE_RUNNING).rows[0]
    assert not row.stopped and row.stopped_by == ""
    blocks = render.workers_view_blocks(
        model.BoardSnapshot(rows=(row,)), _link, tabs=_TABS, now=_NOW
    )
    by_id = {b.get("block_id"): b for b in blocks}
    assert "kc_cc_worker_stop" in [e["action_id"] for e in by_id["worker-it_00000001"]["elements"]]


@pytest.mark.asyncio
async def test_a_stop_aimed_at_the_next_queued_turn_is_a_new_stop(
    env: Any, stop_world: Any
) -> None:
    slot = stop_world.slots["chat-w1"]
    slot._turn_generation = 7
    assert (await worker_stop._handle(env.orch, _stop_payload(ts="3.1"))).startswith("stop:")
    slot._turn_generation = 8  # its queued follow-up started, and shows Stop
    assert (await worker_stop._handle(env.orch, _stop_payload(ts="3.2"))).startswith("stop:")
    assert len(stop_world.stopped) == 2
    # The same turn pressed again inside the window is still only answered.
    assert await worker_stop._handle(env.orch, _stop_payload(ts="3.3")) == "repeat"
    assert len(stop_world.stopped) == 2


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
@pytest.mark.asyncio
async def test_a_stop_that_raises_leaves_no_repeat_mark(
    env: Any, stop_world: Any, monkeypatch: Any, failure: type[BaseException]
) -> None:
    calls: list[str] = []

    async def stop_slot_turn(state: Any, s: Any, **kw: Any) -> dict[str, Any]:
        calls.append(s.key)
        if len(calls) == 1:
            raise failure()
        stop_world.stopped.append((s.key, kw))
        return {"info": ""}

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", stop_slot_turn)
    with pytest.raises(failure):
        await worker_stop._handle(env.orch, _stop_payload(ts="4.1"))
    # Pressed again at once: a fresh stop, not "Already stopping".
    assert (await worker_stop._handle(env.orch, _stop_payload(ts="4.2"))).startswith("stop:")
    assert len(stop_world.stopped) == 1


def test_a_late_note_for_an_earlier_turn_keeps_the_later_turns_stop() -> None:
    from kiro_crew.slack.code_channels.board import stops

    stops.reset_for_tests()
    slot = SimpleNamespace(key="chat-w1", _turn_generation=8)
    stops.note(slot, OWNER, generation=8)  # stop B, on turn 8
    stops.note(slot, OWNER, generation=7)  # stop A resumes late, for turn 7
    state = SimpleNamespace(get_slot=lambda key: slot if key == "chat-w1" else None)
    held = stops.current(state, "chat-w1")
    assert held is not None and held.generation == 8


@pytest.mark.asyncio
async def test_a_snapshot_read_before_the_next_turn_publishes_no_stopped_line(
    env: Any, monkeypatch: Any
) -> None:
    from kiro_crew.slack.code_channels import board
    from kiro_crew.slack.code_channels.board import stops

    binding = _bind(env)
    _set_views(env)
    slot = SimpleNamespace(key="chat-w1", _turn_generation=3)
    env.state.get_slot = lambda key: slot if key == "chat-w1" else None
    fake = _Refusing(refuse=0)
    fake.updates: list[Any] = []  # type: ignore[misc]

    async def update_message(channel: str, ts: str, text: str = "", blocks: Any = None) -> None:
        fake.updates.append(blocks)

    fake.update_message = update_message  # type: ignore[method-assign]
    monkeypatch.setattr(surface, "slack", lambda: fake)
    real = surface.SurfaceClient(env.api.api_call)
    monkeypatch.setattr(surface, "client", lambda: real)
    monkeypatch.setattr(surface, "dashboard_state", lambda: env.state)
    publisher.forget(CH)
    stops.note(slot, OWNER, generation=3)
    # Read while turn 3 was the stopped one...
    stale = model.BoardSnapshot(
        rows=(_row(session_key="chat-w1", stopped_by=OWNER, stopped_at="13:31"),)
    )
    slot._turn_generation = 4  # ...then the queued turn 4 started before the publish.
    await board.publish(binding, stale)
    sent = _calls(env, "agents.conversations.setView")
    assert sent and "Stopped by" not in json.dumps(sent[-1]["blocks"])
    assert all("Stopped by" not in json.dumps(b) for b in fake.sent + fake.updates)
    assert stops.take_urgent(["chat-w1"]) is False  # no edit-gap skip left over


ALICE = "U0ALICE001"


@pytest.mark.asyncio
async def test_a_newer_stop_landing_before_the_publish_shows_its_own_attribution(
    env: Any, monkeypatch: Any
) -> None:
    from kiro_crew.slack.code_channels import board
    from kiro_crew.slack.code_channels.board import stops

    binding = _bind(env)
    _set_views(env)
    slot = SimpleNamespace(key="chat-w1", _turn_generation=3)
    env.state.get_slot = lambda key: slot if key == "chat-w1" else None
    fake = _Refusing(refuse=0)
    fake.updates: list[Any] = []  # type: ignore[misc]

    async def update_message(channel: str, ts: str, text: str = "", blocks: Any = None) -> None:
        fake.updates.append(blocks)

    fake.update_message = update_message  # type: ignore[method-assign]
    monkeypatch.setattr(surface, "slack", lambda: fake)
    real = surface.SurfaceClient(env.api.api_call)
    monkeypatch.setattr(surface, "client", lambda: real)
    monkeypatch.setattr(surface, "dashboard_state", lambda: env.state)
    publisher.forget(CH)
    # Read while Alice's stop of turn 3 was showing...
    stale = model.BoardSnapshot(
        rows=(_row(session_key="chat-w1", stopped_by=ALICE, stopped_at="13:31"),)
    )
    slot._turn_generation = 4  # ...then the queued turn 4 started, and Bob stopped it.
    stops.note(slot, OWNER, generation=4)
    bob = stops.current(env.state, "chat-w1")
    assert bob is not None
    await board.publish(binding, stale)
    workers = json.dumps(_calls(env, "agents.conversations.setView")[-1]["blocks"])
    boards = [json.dumps(b) for b in fake.sent + fake.updates]
    assert ALICE not in workers and all(ALICE not in b for b in boards)
    assert OWNER in workers and any(OWNER in b for b in boards)


def test_reconcile_sets_each_overlay_to_the_stop_showing_now() -> None:
    from kiro_crew.slack.code_channels.board import stops

    stops.reset_for_tests()
    slots = {
        "chat-w1": SimpleNamespace(key="chat-w1", _turn_generation=4),
        "chat-w2": SimpleNamespace(key="chat-w2", _turn_generation=2),
        "chat-w3": SimpleNamespace(key="chat-w3", _turn_generation=9),
    }
    state = SimpleNamespace(get_slot=slots.get)
    stops.note(slots["chat-w1"], OWNER, generation=4)  # replaces Alice's older stop
    stops.note(slots["chat-w2"], OWNER, generation=2)  # landed after the read
    snap = model.BoardSnapshot(
        rows=(
            _row("it_00000001", session_key="chat-w1", stopped_by=ALICE, stopped_at="13:31"),
            _row("it_00000002", session_key="chat-w2"),
            _row("it_00000003", session_key="chat-w3", stopped_by=ALICE, stopped_at="13:31"),
        )
    )
    rows = stops.reconcile(snap, state).rows
    at1 = stops.current(state, "chat-w1").at  # type: ignore[union-attr]
    at2 = stops.current(state, "chat-w2").at  # type: ignore[union-attr]
    assert [(r.stopped_by, r.stopped_at) for r in rows] == [(OWNER, at1), (OWNER, at2), ("", "")]


@pytest.mark.parametrize("path", ["workers_tab", "board"])
@pytest.mark.asyncio
async def test_a_turn_starting_during_a_publishers_last_read_shows_no_stopped_line(
    env: Any, monkeypatch: Any, path: str
) -> None:
    from kiro_crew.slack.code_channels.board import stops

    binding = _bind(env)
    _set_views(env)
    slot = SimpleNamespace(key="chat-w1", _turn_generation=3)
    env.state.get_slot = lambda key: slot if key == "chat-w1" else None
    monkeypatch.setattr(surface, "dashboard_state", lambda: env.state)
    fake = _Refusing(refuse=0)
    monkeypatch.setattr(surface, "slack", lambda: fake)
    monkeypatch.setattr(surface, "client", lambda: surface.SurfaceClient(env.api.api_call))
    publisher.forget(CH)
    stops.note(slot, OWNER, generation=3)
    stale = model.BoardSnapshot(
        rows=(_row(session_key="chat-w1", stopped_by=OWNER, stopped_at="13:31"),)
    )
    mod = tabs if path == "workers_tab" else publisher
    real_get = mod.get_async

    async def get_async(channel: str) -> Any:
        slot._turn_generation = 4  # the queued turn starts during the publisher's read
        return await real_get(channel)

    monkeypatch.setattr(mod, "get_async", get_async)
    if path == "workers_tab":
        await tabs.publish_workers(binding, stale, link_for=_link, urgent=True)
        sent = [c["blocks"] for c in _calls(env, "agents.conversations.setView")]
    else:
        await publisher.publish(binding, stale, link_for=_link, urgent=True)
        sent = list(fake.sent)
    assert sent and all("Stopped by" not in json.dumps(b) for b in sent)
    assert stops.take_urgent(["chat-w1"]) is False  # the stale mark is gone too


# ── The board's scope, asked again across every await of the tab paths ────


def _scope_flag(monkeypatch: Any) -> dict[str, bool]:
    flag = {"in": True}
    monkeypatch.setattr(source, "scope_admits", lambda state, binding: flag["in"])
    return flag


@pytest.mark.parametrize("when", ["code_tab", "tab_read", "board_read"])
@pytest.mark.asyncio
async def test_a_channel_leaving_the_scope_mid_publish_gets_no_tab_and_no_board(
    env: Any, monkeypatch: Any, when: str
) -> None:
    from kiro_crew.slack.code_channels import board

    binding = _bind(env)
    _set_views(env)
    flag = _scope_flag(monkeypatch)
    fake = _Refusing(refuse=0)
    monkeypatch.setattr(surface, "slack", lambda: fake)
    monkeypatch.setattr(surface, "client", lambda: surface.SurfaceClient(env.api.api_call))
    publisher.forget(CH)
    if when == "code_tab":

        async def code_tab(_b: Any) -> bool:
            flag["in"] = False  # the channel turned shared meanwhile
            return False

        monkeypatch.setattr(board, "_code_tab_shown", code_tab)
    elif when == "board_read":
        real_board_get = publisher.get_async

        async def board_get(channel: str) -> Any:
            flag["in"] = False  # ...or while the board message read its record
            return await real_board_get(channel)

        monkeypatch.setattr(publisher, "get_async", board_get)
    else:
        real_get = tabs.get_async

        async def get_async(channel: str) -> Any:
            flag["in"] = False  # ...or while the Workers tab read its record
            return await real_get(channel)

        monkeypatch.setattr(tabs, "get_async", get_async)
    snap = model.BoardSnapshot(rows=(_row(session_key="chat-w1"),))
    assert await board.publish(binding, snap) == "out_of_scope"
    if when != "board_read":  # (the Workers tab went out before the board's read)
        assert _calls(env, "agents.conversations.setView") == []
    assert fake.sent == []


@pytest.mark.asyncio
async def test_a_channel_leaving_the_scope_mid_sync_gets_no_pr_tab_write_or_removal(
    env: Any, monkeypatch: Any
) -> None:
    binding = _bind(env)
    _set_views(env)
    flag = _scope_flag(monkeypatch)
    both = [
        tabs.PrRef(42, PR_URL.format(n=42), "open"),
        tabs.PrRef(12, PR_URL.format(n=12), "open"),
    ]
    await tabs.sync_pr_tabs(binding, both)
    writes = len(_calls(env, "agents.conversations.setView"))

    async def relist(client: Any, channel: str, record: Any) -> Any:
        flag["in"] = False  # the scope goes while Slack's list is read
        return record

    monkeypatch.setattr(tabs, "_relist", relist)
    newer = [tabs.PrRef(60, PR_URL.format(n=60), "open")]
    await tabs.sync_pr_tabs(binding, newer, settled={12, 42})
    assert len(_calls(env, "agents.conversations.setView")) == writes
    assert _calls(env, "agents.conversations.removeView") == []


@pytest.mark.parametrize("when", ["before_read", "before_post"])
@pytest.mark.asyncio
async def test_a_wrap_up_out_of_scope_links_no_worker_pr_tab_and_no_outcomes(
    env: Any, monkeypatch: Any, when: str
) -> None:
    from kiro_crew.slack.code_channels import board

    binding = _bind(env)
    flag = _scope_flag(monkeypatch)
    fake = _Refusing(refuse=0)
    monkeypatch.setattr(surface, "slack", lambda: fake)
    monkeypatch.setattr(surface, "client", lambda: None)
    surface_store().update(
        CH, views=[_v("pr-10", "pull_request", "1"), _v("pr-33", "pull_request", "2")]
    )
    # The channel's own PR 10, and worker PR 33 from the cached board.
    monkeypatch.setattr(
        wrapup,
        "_gather_facts_sync",
        lambda b: wrapup.Facts(
            pr_number=10,
            pr_state="open",
            code_tab=True,
            pr_tabs=wrapup._pr_tabs(CH, {10, 33}),
        ),
    )

    async def lines(_b: Any) -> list[str]:
        return ["• Fix login: PR #33 open"]

    monkeypatch.setattr(board, "outcome_lines", lines)
    if when == "before_read":
        flag["in"] = False
    else:
        real = surface.is_archived

        async def is_archived(b: Any) -> bool:
            flag["in"] = False  # the scope goes between the facts and the post
            return await real(b)

        monkeypatch.setattr(surface, "is_archived", is_archived)
    assert await wrapup.post_wrap_up(binding, "All done.", caller=OWNER)
    posted = json.dumps(fake.sent[-1])
    assert "Vpr33" not in posted and "Fix login" not in posted
    assert "Vpr10" in posted  # the channel's own PR tab stays


@pytest.mark.asyncio
async def test_a_stop_out_of_scope_after_the_board_read_stops_nothing(
    env: Any, stop_world: Any, monkeypatch: Any
) -> None:
    flag = _scope_flag(monkeypatch)
    real = source.read_board

    async def read_board(state: Any, b: Binding) -> Any:
        snapshot = await real(state, b)
        flag["in"] = False  # the channel turned shared after the read
        return snapshot

    monkeypatch.setattr(source, "read_board", read_board)
    assert await worker_stop._handle(env.orch, _stop_payload()) == "denied"
    assert stop_world.stopped == []
    assert all("Fix login" not in text for *_ignored, text in env.slack.ephemerals)


@pytest.mark.asyncio
async def test_a_stop_whose_channel_leaves_the_scope_meanwhile_names_no_worker(
    env: Any, stop_world: Any, monkeypatch: Any
) -> None:
    from kiro_crew.slack.code_channels.board import stops

    flag = _scope_flag(monkeypatch)

    async def stop_slot_turn(state: Any, s: Any, **kw: Any) -> dict[str, Any]:
        stop_world.stopped.append((s.key, kw))
        flag["in"] = False  # shared while the stop ran
        return {"info": ""}

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", stop_slot_turn)
    assert (await worker_stop._handle(env.orch, _stop_payload())).startswith("stop:")
    assert len(stop_world.stopped) == 1  # the stop itself was admitted, and ran
    # Out of scope by the reply: no reply at all, let alone one naming the worker.
    assert all("Fix login" not in text for *_ignored, text in env.slack.ephemerals)
    assert stops.current(env.state, "chat-w1") is None and stop_world.noted == []


@pytest.mark.parametrize("when", ["before_send", "before_alert", "control"])
@pytest.mark.asyncio
async def test_a_board_publish_out_of_scope_before_a_send_sends_nothing(
    env: Any, monkeypatch: Any, when: str
) -> None:
    binding = _bind(env)
    flag = _scope_flag(monkeypatch)
    fake = _Refusing(refuse=0)
    alerts: list[Any] = []

    async def post_message(*a: Any, **k: Any) -> str:
        alerts.append((a, k))
        return "100.2"

    fake.post_message = post_message  # type: ignore[method-assign]
    monkeypatch.setattr(surface, "slack", lambda: fake)
    publisher.forget(CH)
    real_get = publisher.get_async
    reads = {"n": 0}

    async def get_async(channel: str) -> Any:
        reads["n"] += 1
        if when == "before_send":
            flag["in"] = False  # during the publish's own record read
        return await real_get(channel)

    monkeypatch.setattr(publisher, "get_async", get_async)
    if when == "before_alert":
        real_summary = publisher._record_summary

        async def record_summary(b: Any, ts: str, admits: Any = None) -> None:
            flag["in"] = False  # after the board went out, before its alert
            await real_summary(b, ts, admits)

        monkeypatch.setattr(publisher, "_record_summary", record_summary)
    waiting = model.BoardSnapshot(
        rows=(
            _row(
                state=model.WAITING_ON_YOU,
                session_key="chat-w1",
                report_stamp="r1",
                waiting_on="report",
            ),
        )
    )
    out = await publisher.publish(
        binding,
        waiting,
        link_for=_link,
        owner_id=OWNER,
        admits=lambda: source.scope_admits(None, binding),
    )
    if when == "before_send":
        assert out == "out_of_scope" and fake.sent == []
    elif when == "before_alert":
        assert fake.sent and alerts == []
    else:  # in scope throughout: the alert does go out
        assert fake.sent and alerts


@pytest.mark.asyncio
async def test_a_wrap_up_retried_without_tab_links_is_scoped_again(
    env: Any, monkeypatch: Any
) -> None:
    from kiro_crew.slack.code_channels import board

    binding = _bind(env)
    flag = _scope_flag(monkeypatch)
    fake = _Refusing(refuse=1)
    real_post = fake.post_blocks

    async def post_blocks(channel: str, blocks: Any, text: str, thread_ts: Any = None) -> str:
        if len(fake.sent) == 0:
            flag["in"] = False  # shared while the first (refused) post was out
        return await real_post(channel, blocks, text, thread_ts)

    fake.post_blocks = post_blocks  # type: ignore[method-assign]
    monkeypatch.setattr(surface, "slack", lambda: fake)
    monkeypatch.setattr(surface, "client", lambda: None)
    monkeypatch.setattr(
        wrapup, "_gather_facts_sync", lambda b: wrapup.Facts(code_tab=True, branch="x")
    )

    async def lines(_b: Any) -> list[str]:
        return ["• Fix login: PR #33 open"]

    monkeypatch.setattr(board, "outcome_lines", lines)
    assert await wrapup.post_wrap_up(binding, "All done.", caller=OWNER)
    assert "Fix login" in json.dumps(fake.sent[0])  # in scope when first built
    assert "Fix login" not in json.dumps(fake.sent[1])  # the retry is scoped again


@pytest.mark.parametrize("when", ["alert_read", "between_alerts"])
@pytest.mark.asyncio
async def test_a_channel_leaving_the_scope_during_the_alerts_gets_no_further_mention(
    env: Any, monkeypatch: Any, when: str
) -> None:
    binding = _bind(env)
    flag = _scope_flag(monkeypatch)
    fake = _Refusing(refuse=0)
    alerts: list[Any] = []

    async def post_message(*a: Any, **k: Any) -> str:
        alerts.append((a, k))
        if when == "between_alerts":
            flag["in"] = False  # shared right after the first mention went out
        return "100.2"

    fake.post_message = post_message  # type: ignore[method-assign]
    monkeypatch.setattr(surface, "slack", lambda: fake)
    publisher.forget(CH)
    if when == "alert_read":
        real_alert = publisher._alert
        real_get = publisher.get_async

        async def alert(*a: Any, **k: Any) -> None:
            async def get_async(channel: str) -> Any:
                flag["in"] = False  # shared during the alert's own record read
                return await real_get(channel)

            monkeypatch.setattr(publisher, "get_async", get_async)
            await real_alert(*a, **k)

        monkeypatch.setattr(publisher, "_alert", alert)
    waiting = model.BoardSnapshot(
        rows=tuple(
            _row(
                f"it_0000000{n}",
                state=model.WAITING_ON_YOU,
                session_key=f"chat-w{n}",
                report_stamp="r1",
                waiting_on="report",
            )
            for n in (1, 2)
        )
    )
    await publisher.publish(
        binding,
        waiting,
        link_for=_link,
        owner_id=OWNER,
        admits=lambda: source.scope_admits(None, binding),
    )
    assert fake.sent  # the board itself went out while in scope
    assert len(alerts) == (0 if when == "alert_read" else 1)


@pytest.mark.asyncio
async def test_the_channels_own_pr_tab_needs_no_worker_hand_off(env: Any, monkeypatch: Any) -> None:
    binding = _bind(env)
    _set_views(env)
    flag = _scope_flag(monkeypatch)
    await tabs.sync_pr_tabs(binding, [tabs.PrRef(42, PR_URL.format(n=42), "open")], own=42)
    writes = len(_calls(env, "agents.conversations.setView"))
    # Hand-off off (the default), or the channel turned shared: out of the
    # board's scope. The channel's own new PR 50 still gets its tab; the worker
    # PR 60 is a ledger fact and is not used.
    flag["in"] = False
    newer = [
        tabs.PrRef(60, PR_URL.format(n=60), "open"),
        tabs.PrRef(50, PR_URL.format(n=50), "open"),
    ]
    held = await tabs.sync_pr_tabs(binding, newer, own=50)
    sent = _calls(env, "agents.conversations.setView")[writes:]
    assert [c["view_key"] for c in sent] == ["pr-50"]
    assert 50 in held and 60 not in held


@pytest.mark.asyncio
async def test_out_of_scope_the_workers_pr_tabs_are_neither_removed_nor_replaced(
    env: Any, monkeypatch: Any
) -> None:
    binding = _bind(env)
    _set_views(env)
    flag = _scope_flag(monkeypatch)
    workers = [
        tabs.PrRef(42, PR_URL.format(n=42), "open"),
        tabs.PrRef(12, PR_URL.format(n=12), "open"),
    ]
    await tabs.sync_pr_tabs(binding, workers)  # two workers' tabs fill both slots
    writes = len(_calls(env, "agents.conversations.setView"))
    flag["in"] = False
    held = await tabs.sync_pr_tabs(
        binding, [tabs.PrRef(50, PR_URL.format(n=50), "open")], settled={12}, own=50
    )
    assert _calls(env, "agents.conversations.removeView") == []
    assert len(_calls(env, "agents.conversations.setView")) == writes
    assert set(held) == {42, 12}


@pytest.mark.asyncio
async def test_with_hand_off_off_a_fresh_channel_gets_its_own_pr_tab(
    env: Any, monkeypatch: Any
) -> None:
    binding = _bind(env)
    _set_views(env)
    flag = _scope_flag(monkeypatch)
    flag["in"] = False
    held = await tabs.sync_pr_tabs(binding, [tabs.PrRef(7, PR_URL.format(n=7), "open")], own=7)
    assert held == {7: "Vpr7"}


@pytest.mark.parametrize("when", ["before_sync", "during_relist"])
@pytest.mark.asyncio
async def test_a_channel_set_off_gets_no_own_pr_tab_write_or_removal(
    env: Any, monkeypatch: Any, when: str
) -> None:
    binding = _bind(env)
    _set_views(env)
    flag = _scope_flag(monkeypatch)
    flag["in"] = False  # worker hand-off off: only the own PR is synced
    await tabs.sync_pr_tabs(binding, [tabs.PrRef(42, PR_URL.format(n=42), "open")], own=42)
    writes = len(_calls(env, "agents.conversations.setView"))
    admits = {"on": True}
    monkeypatch.setattr(surface, "channel_admits", lambda cfg, b: admits["on"])
    if when == "before_sync":
        admits["on"] = False  # the operator set the channel to Off or Review
    else:

        async def relist(client: Any, channel: str, record: Any) -> Any:
            admits["on"] = False  # ...while the refresh was reading Slack's list
            return record

        monkeypatch.setattr(tabs, "_relist", relist)
    await tabs.sync_pr_tabs(
        binding, [tabs.PrRef(50, PR_URL.format(n=50), "open")], settled={42}, own=50
    )
    assert len(_calls(env, "agents.conversations.setView")) == writes
    assert _calls(env, "agents.conversations.removeView") == []


@pytest.mark.asyncio
async def test_a_stop_landing_while_room_is_made_holds_no_backoff(
    env: Any, monkeypatch: Any
) -> None:
    _bind(env)
    surface_store().update(CH, views=[_v("report", "html", "9")])
    listed = [{"view_id": "Vreport"}] + [{"view_id": f"Vtheirs{n}"} for n in range(4)]
    _set_views(env, listed, refuse="too_many_views")
    real_remove = env.api.responses["agents.conversations.removeView"]

    def remove_view(body: dict[str, Any]) -> dict[str, Any]:
        monkeypatch.setattr(surface, "channel_stopped", lambda channel: True)  # Slack's Stop
        return real_remove(body)

    env.api.responses["agents.conversations.removeView"] = remove_view
    snap = model.BoardSnapshot(rows=(_row(session_key="chat-w1"),))
    assert await tabs.publish_workers(_bind(env), snap, link_for=_link) == "fenced"
    assert tabs._held(CH, views.WORKERS_KEY) == 0  # a fence, not a failure to back off


@pytest.mark.parametrize("what", ["stop", "set_off"])
@pytest.mark.asyncio
async def test_a_fence_at_the_cap_is_no_full_channel_hold(
    env: Any, monkeypatch: Any, what: str
) -> None:
    _bind(env)
    flag = _scope_flag(monkeypatch)
    surface_store().update(CH, views=[_v("report", "html", "9")])
    listed = [{"view_id": "Vreport"}] + [{"view_id": f"Vtheirs{n}"} for n in range(4)]
    _set_views(env, listed, refuse="too_many_views")
    real_list = env.api.responses["agents.conversations.listViews"]

    def list_views(body: dict[str, Any]) -> dict[str, Any]:
        if what == "stop":
            monkeypatch.setattr(surface, "channel_stopped", lambda channel: True)  # Slack's Stop
        else:  # Off / Review: the board's scope (which asks activation) refuses
            flag["in"] = False
        return real_list(body)

    env.api.responses["agents.conversations.listViews"] = list_views
    snap = model.BoardSnapshot(rows=(_row(session_key="chat-w1"),))
    assert await tabs.publish_workers(_bind(env), snap, link_for=_link) == "fenced"
    assert _calls(env, "agents.conversations.removeView") == []
    assert tabs._held(CH, views.WORKERS_KEY) == 0  # not a 10-minute "channel full"


@pytest.mark.asyncio
async def test_a_fenced_pr_tab_removal_holds_no_backoff(env: Any, monkeypatch: Any) -> None:
    binding = _bind(env)
    _set_views(env)
    await tabs.sync_pr_tabs(binding, [tabs.PrRef(42, PR_URL.format(n=42), "open")], own=42)
    admits = {"on": True}
    monkeypatch.setattr(surface, "channel_admits", lambda cfg, b: admits["on"])
    real_get = views.get_async

    async def get_async(channel: str) -> Any:
        admits["on"] = False  # set to Off right as the removal reads its record
        return await real_get(channel)

    monkeypatch.setattr(views, "get_async", get_async)
    await tabs.sync_pr_tabs(binding, [], settled={42}, own=42)
    assert _calls(env, "agents.conversations.removeView") == []
    assert tabs._held(CH, "remove:pr-42") == 0


def test_a_channels_view_locks_are_never_replaced_however_many_channels_there_are() -> None:
    first, record = views._lock(CH), views._record_lock(CH)
    for n in range(5000):
        views._lock(f"C{n:08d}")
        views._record_lock(f"C{n:08d}")
    # Unlocked but possibly awaited (a released lock's waiter has not woken yet):
    # a fresh lock for the same channel would let a second writer in beside it.
    assert views._lock(CH) is first and views._record_lock(CH) is record


@pytest.mark.parametrize("what", ["policy", "stop", "set_off", "channel_ceiling"])
@pytest.mark.asyncio
async def test_a_wrap_up_retry_is_fenced_again_after_the_failed_post(
    env: Any, monkeypatch: Any, what: str
) -> None:
    binding = _bind(env)
    _scope_flag(monkeypatch)
    fake = _Refusing(refuse=1)
    real_post = fake.post_blocks
    allowed = {"out": True, "on": True, "ceiling": ""}

    async def outbound() -> bool:
        return allowed["out"]

    monkeypatch.setattr(surface, "outbound_permitted", outbound)
    monkeypatch.setattr(surface, "channel_admits", lambda cfg, b: allowed["on"])
    monkeypatch.setattr(surface, "channel_policy_refusal", lambda *a, **k: allowed["ceiling"])

    async def post_blocks(channel: str, blocks: Any, text: str, thread_ts: Any = None) -> str:
        if not fake.sent:  # while the first (refused) post was out
            if what == "policy":
                allowed["out"] = False  # the channels policy revoked
            elif what == "stop":
                status.quiet_after_stop(CH)  # Slack's Stop
            elif what == "channel_ceiling":
                allowed["ceiling"] = "not in allowed_channel_ids"  # the channel ceiling
            else:
                allowed["on"] = False  # set to Off or Review
        return await real_post(channel, blocks, text, thread_ts)

    fake.post_blocks = post_blocks  # type: ignore[method-assign]
    monkeypatch.setattr(surface, "slack", lambda: fake)
    monkeypatch.setattr(surface, "client", lambda: None)
    monkeypatch.setattr(wrapup, "_gather_facts_sync", lambda b: wrapup.Facts(code_tab=True))
    try:
        assert await wrapup.post_wrap_up(binding, "All done.", caller=OWNER) == ""
    finally:
        status._quiet.discard(CH)
    assert len(fake.sent) == 1  # the refused post only: no retry went out


@pytest.mark.asyncio
async def test_a_stop_during_the_policy_read_fences_the_own_pr_tab(
    env: Any, monkeypatch: Any
) -> None:
    binding = _bind(env)
    _set_views(env)
    flag = _scope_flag(monkeypatch)
    flag["in"] = False  # hand-off off: only the own PR, the channel fences alone

    reads = {"n": 0}

    async def outbound() -> bool:
        reads["n"] += 1
        if reads["n"] == 2:  # the read right before the setView itself
            status.quiet_after_stop(CH)  # Slack's Stop lands during the policy read
        return True

    monkeypatch.setattr(surface, "outbound_permitted", outbound)
    try:
        held = await tabs.sync_pr_tabs(binding, [tabs.PrRef(7, PR_URL.format(n=7), "open")], own=7)
    finally:
        status._quiet.discard(CH)
    assert held == {} and _calls(env, "agents.conversations.setView") == []


@pytest.mark.parametrize("when", ["policy_read", "refused_post"])
@pytest.mark.asyncio
async def test_a_fallback_whose_request_is_replaced_before_the_send_posts_nothing(
    env: Any, monkeypatch: Any, when: str
) -> None:
    """The facts-only fallback answers its /wrap-up only while that request is live.

    A newer /wrap-up can replace it during any await before a send: the policy
    read before the post, or the refused first post before the retry.
    """
    binding = _bind(env)
    _scope_flag(monkeypatch)
    fake = _Refusing(refuse=1 if when == "refused_post" else 0)
    real_post = fake.post_blocks
    read = {"facts": False}

    async def outbound() -> bool:
        if when == "policy_read" and read["facts"]:
            wrapup._requests[CH] = "newer"  # a newer /wrap-up during the last read
        return True

    def facts(_b: Any) -> Any:
        read["facts"] = True
        return wrapup.Facts(code_tab=True)

    async def post_blocks(channel: str, blocks: Any, text: str, thread_ts: Any = None) -> str:
        if when == "refused_post" and not fake.sent:
            wrapup._requests[CH] = "newer"  # while the refused first post was out
        return await real_post(channel, blocks, text, thread_ts)

    fake.post_blocks = post_blocks  # type: ignore[method-assign]
    monkeypatch.setattr(surface, "outbound_permitted", outbound)
    monkeypatch.setattr(surface, "slack", lambda: fake)
    monkeypatch.setattr(surface, "client", lambda: None)
    monkeypatch.setattr(wrapup, "_gather_facts_sync", facts)
    wrapup._requests[CH] = "tag1"
    try:
        out = await wrapup.post_wrap_up(
            binding, "", caller=OWNER, settles="tag1", only_if_live=True
        )
    finally:
        wrapup._requests.pop(CH, None)
    assert out == ""
    assert len(fake.sent) == (1 if when == "refused_post" else 0)  # no (re)send went out


@pytest.mark.asyncio
async def test_a_policy_revoked_while_the_wrap_up_facts_are_read_posts_nothing(
    env: Any, monkeypatch: Any
) -> None:
    binding = _bind(env)
    _scope_flag(monkeypatch)
    fake = _Refusing(refuse=0)
    allowed = {"out": True}

    async def outbound() -> bool:
        return allowed["out"]

    monkeypatch.setattr(surface, "outbound_permitted", outbound)
    monkeypatch.setattr(surface, "slack", lambda: fake)
    monkeypatch.setattr(surface, "client", lambda: None)

    def facts(_b: Any) -> Any:
        allowed["out"] = False  # the channels policy revoked during the fact reads
        return wrapup.Facts(branch="x")

    monkeypatch.setattr(wrapup, "_gather_facts_sync", facts)
    assert await wrapup.post_wrap_up(binding, "All done.", caller=OWNER) == ""
    assert fake.sent == []


@pytest.mark.asyncio
async def test_a_wrap_up_retry_fenced_by_a_stop_is_dropped_not_left_pending(
    env: Any, monkeypatch: Any
) -> None:
    binding = _bind(env)
    _scope_flag(monkeypatch)
    fake = _Refusing(refuse=1)
    real_post = fake.post_blocks

    async def post_blocks(channel: str, blocks: Any, text: str, thread_ts: Any = None) -> str:
        if not fake.sent:
            status.quiet_after_stop(CH)  # Slack's Stop while the first post was out
        return await real_post(channel, blocks, text, thread_ts)

    fake.post_blocks = post_blocks  # type: ignore[method-assign]
    monkeypatch.setattr(surface, "slack", lambda: fake)
    monkeypatch.setattr(surface, "client", lambda: None)
    monkeypatch.setattr(wrapup, "_gather_facts_sync", lambda b: wrapup.Facts(code_tab=True))
    surface_store().update(CH, wrapup_pending_since=1.0, wrapup_requested_by=OWNER)
    wrapup._requests[CH] = "tag1"
    try:
        assert await wrapup.post_wrap_up(binding, "All done.", caller=OWNER, settles="tag1") == ""
    finally:
        status._quiet.discard(CH)
    assert not surface_store().get(CH).wrapup_pending_since
    assert CH not in wrapup._requests  # the stopped request is forgotten, not left armed


@pytest.mark.asyncio
async def test_a_stop_fenced_wrap_up_leaves_a_newer_request_live(
    env: Any, monkeypatch: Any
) -> None:
    binding = _bind(env)
    _scope_flag(monkeypatch)
    fake = _Refusing(refuse=1)
    real_post = fake.post_blocks

    async def post_blocks(channel: str, blocks: Any, text: str, thread_ts: Any = None) -> str:
        if not fake.sent:
            wrapup._requests[CH] = "newer"  # a newer /wrap-up...
            status.quiet_after_stop(CH)  # ...and Slack's Stop while the first post was out
        return await real_post(channel, blocks, text, thread_ts)

    fake.post_blocks = post_blocks  # type: ignore[method-assign]
    monkeypatch.setattr(surface, "slack", lambda: fake)
    monkeypatch.setattr(surface, "client", lambda: None)
    monkeypatch.setattr(wrapup, "_gather_facts_sync", lambda b: wrapup.Facts(code_tab=True))
    surface_store().update(CH, wrapup_pending_since=1.0, wrapup_requested_by=OWNER)
    wrapup._requests[CH] = "tag1"
    try:
        assert await wrapup.post_wrap_up(binding, "All done.", caller=OWNER, settles="tag1") == ""
    finally:
        status._quiet.discard(CH)
    try:
        assert wrapup._requests.get(CH) == "newer"  # the newer request is not dropped
        assert surface_store().get(CH).wrapup_pending_since
    finally:
        wrapup._requests.pop(CH, None)


@pytest.mark.asyncio
async def test_a_channel_found_shared_during_the_wrap_up_policy_read_gets_no_worker_facts(
    env: Any, monkeypatch: Any
) -> None:
    from kiro_crew.slack.code_channels import board

    binding = _bind(env)
    flag = _scope_flag(monkeypatch)
    fake = _Refusing(refuse=0)
    reads = {"n": 0}

    async def outbound() -> bool:
        reads["n"] += 1
        if reads["n"] == 2:  # the read inside the lock, right before the post
            flag["in"] = False  # Slack Connect sharing recorded meanwhile
        return True

    monkeypatch.setattr(surface, "outbound_permitted", outbound)
    monkeypatch.setattr(surface, "slack", lambda: fake)
    monkeypatch.setattr(surface, "client", lambda: None)
    monkeypatch.setattr(wrapup, "_gather_facts_sync", lambda b: wrapup.Facts(branch="x"))

    async def lines(_b: Any) -> list[str]:
        return ["• Fix login: PR #33 open"]

    monkeypatch.setattr(board, "outcome_lines", lines)
    assert await wrapup.post_wrap_up(binding, "All done.", caller=OWNER)
    assert "Fix login" not in json.dumps(fake.sent[-1])


@pytest.mark.asyncio
async def test_former_own_pr_tabs_give_way_to_the_channels_newer_own_pr(
    env: Any, monkeypatch: Any
) -> None:
    binding = _bind(env)
    _set_views(env)
    flag = _scope_flag(monkeypatch)
    flag["in"] = False  # worker hand-off off: only the channel's own PRs
    for n in (10, 20, 30):  # the worktree moves between branches with open PRs
        await tabs.sync_pr_tabs(binding, [tabs.PrRef(n, PR_URL.format(n=n), "open")], own=n)
    keys = [c["view_key"] for c in _calls(env, "agents.conversations.setView")]
    assert keys == ["pr-10", "pr-20", "pr-30"]  # #30 is not blocked by #10 and #20
    removed = [c["view_id"] for c in _calls(env, "agents.conversations.removeView")]
    assert removed == ["Vpr10"]  # the oldest of the channel's own tabs gives way
    recorded = {v["key"]: v.get("origin") for v in surface_store().get(CH).views}
    assert recorded == {"pr-20": "own", "pr-30": "own"}


@pytest.mark.asyncio
async def test_a_worker_pr_that_becomes_the_channels_own_is_relabelled(
    env: Any, monkeypatch: Any
) -> None:
    binding = _bind(env)
    _set_views(env)
    _scope_flag(monkeypatch)
    await tabs.sync_pr_tabs(binding, [tabs.PrRef(42, PR_URL.format(n=42), "open")])
    assert views.recorded(surface_store().get(CH), "pr-42")["origin"] == "worker"
    await tabs.sync_pr_tabs(binding, [tabs.PrRef(42, PR_URL.format(n=42), "open")], own=42)
    assert views.recorded(surface_store().get(CH), "pr-42")["origin"] == "own"
    assert len(_calls(env, "agents.conversations.setView")) == 1  # no second write


@pytest.mark.asyncio
async def test_out_of_scope_a_worker_tab_is_never_relabelled_own(
    env: Any, monkeypatch: Any
) -> None:
    binding = _bind(env)
    _set_views(env)
    flag = _scope_flag(monkeypatch)
    await tabs.sync_pr_tabs(binding, [tabs.PrRef(42, PR_URL.format(n=42), "open")])
    flag["in"] = False  # shared (or hand-off off) before the channel's own PR is 42
    await tabs.sync_pr_tabs(binding, [tabs.PrRef(42, PR_URL.format(n=42), "open")], own=42)
    assert views.recorded(surface_store().get(CH), "pr-42")["origin"] == "worker"


def test_a_pr_tabs_origin_survives_the_surface_store_on_disk(tmp_path: Path) -> None:
    path = tmp_path / "surface.json"
    SurfaceStore(path).update(
        CH,
        views=[
            _v("pr-1", "pull_request", "1") | {"origin": "own"},
            _v("pr-2", "pull_request", "2") | {"origin": "worker"},
            _v("pr-3", "pull_request", "3"),
            _v("pr-4", "pull_request", "4") | {"origin": "bogus"},
        ],
    )
    loaded = {v["key"]: v for v in SurfaceStore(path).get(CH).views}
    assert loaded["pr-1"]["origin"] == "own" and loaded["pr-2"]["origin"] == "worker"
    assert [views.from_ledger(loaded[f"pr-{n}"]) for n in (1, 2, 3, 4)] == [
        False,
        True,
        True,  # no recorded origin: a worker's (fails closed)
        True,  # an unknown one too
    ]


@pytest.mark.parametrize("what", ["set_off", "shared"])
@pytest.mark.asyncio
async def test_a_channel_refused_while_the_stop_ran_gets_no_reply_and_no_overlay(
    env: Any, stop_world: Any, monkeypatch: Any, what: str
) -> None:
    from kiro_crew.slack.code_channels.board import stops

    flag = _scope_flag(monkeypatch)
    admits = {"on": True}
    monkeypatch.setattr(surface, "channel_admits", lambda cfg, b: admits["on"])

    async def stop_slot_turn(state: Any, s: Any, **kw: Any) -> dict[str, Any]:
        stop_world.stopped.append((s.key, kw))
        if what == "set_off":
            admits["on"] = False  # set to Off or Review while the stop ran
        else:
            flag["in"] = False  # found shared while the stop ran
        return {"info": ""}

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", stop_slot_turn)
    replies = len(env.slack.ephemerals)
    assert (await worker_stop._handle(env.orch, _stop_payload())).startswith("stop:")
    assert len(stop_world.stopped) == 1
    assert len(env.slack.ephemerals) == replies  # no reply to a refused channel
    assert stops.current(env.state, "chat-w1") is None and stop_world.noted == []
    assert worker_stop._recent == {}  # nothing of the refused press is kept


@pytest.mark.asyncio
async def test_a_stop_that_errors_after_admission_replies_through_the_full_gate(
    env: Any, stop_world: Any, monkeypatch: Any
) -> None:
    admits = {"on": True}
    monkeypatch.setattr(surface, "channel_admits", lambda cfg, b: admits["on"])

    async def stop_slot_turn(state: Any, s: Any, **kw: Any) -> dict[str, Any]:
        admits["on"] = False  # set to Off or Review, then the stop blows up
        raise RuntimeError("boom")

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", stop_slot_turn)
    replies = len(env.slack.ephemerals)
    assert await worker_stop.handle_stop_click(env.orch, _stop_payload()) is True
    assert len(env.slack.ephemerals) == replies  # the error reply was gated too


def test_a_huge_failure_count_never_overflows_the_backoff(env: Any) -> None:
    tabs._failures[(CH, views.WORKERS_KEY)] = 10**6  # a tab failing for a very long time
    delay = tabs._backoff(CH, views.WORKERS_KEY, "internal_error")
    assert delay == tabs._RETRY_CAP_SECS
    assert tabs._failures[(CH, views.WORKERS_KEY)] <= tabs._RETRY_MAX_EXPONENT + 1
    assert 0 < tabs._held(CH, views.WORKERS_KEY) <= tabs._RETRY_CAP_SECS
    # A bogus Retry-After never strands the tab either.
    assert tabs._backoff(CH, "pr-1", "ratelimited", retry_after=1e300) == (
        tabs._RETRY_AFTER_CAP_SECS
    )
    for bad in (float("nan"), float("inf"), -5.0):
        tabs._succeeded(CH, "pr-2")
        delay = tabs._backoff(CH, "pr-2", "ratelimited", retry_after=bad)
        assert 0 < delay <= tabs._RETRY_AFTER_CAP_SECS


def test_a_views_stamp_and_its_relist_read_one_injected_clock(env: Any) -> None:
    import asyncio as _asyncio

    env.clock.now = 5000.0
    _asyncio.run(views.record_view(CH, key="report", view_id="Vreport", type="html"))
    assert views.recorded(surface_store().get(CH), "report")["at"] == "5000.000000"

    class Client:
        async def list_views(self, channel_id: str) -> list[dict[str, str]]:
            return []  # someone removed it in Slack

    # Same instant as the record: newer than the listing can know, so kept.
    _asyncio.run(views.reconcile(Client(), CH))
    assert views.recorded(surface_store().get(CH), "report") is not None
    env.clock.now += 1  # a later listing: the view is gone, pruned
    _asyncio.run(views.reconcile(Client(), CH))
    assert views.recorded(surface_store().get(CH), "report") is None
