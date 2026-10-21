"""Code-channel view tabs: native PR tabs, tab deep links, the interactive Workers tab.

Recorded Slack payloads in, recorded calls out (``slack_code_channels_helpers``),
the way the code-channel API-gap tests run. Each behaviour has a test that fails
when it is reverted; the security ones (whose view a click came from, whose
worker it stops, who may press it) were each revert-verified.
"""

from __future__ import annotations

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
    e.allowed = {OWNER}
    from kiro_crew.slack.handler_runtime import access

    monkeypatch.setattr(access, "is_allowed_user", lambda u: u in e.allowed)
    monkeypatch.setattr("kiro_crew.slack.handler.is_allowed_user", lambda u: u in e.allowed)
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
    clock = Clock()
    monkeypatch.setattr(tabs, "_clock", clock)
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
    held = await tabs.sync_pr_tabs(binding, second)
    assert _calls(env, "agents.conversations.removeView") == [
        {"channel_id": CH, "view_id": "Vpr12"}
    ]
    assert held == {50: "Vpr50", 42: "Vpr42"}
    # Nothing wanted: every PR tab of ours goes, an agent's view stays.
    surface_store().update(CH, views=[*surface_store().get(CH).views, _v("report", "html", "1")])
    await tabs.sync_pr_tabs(binding, [])
    removed = [c["view_id"] for c in _calls(env, "agents.conversations.removeView")]
    assert removed[0] == "Vpr12" and sorted(removed[1:]) == ["Vpr42", "Vpr50"]
    assert [v["key"] for v in surface_store().get(CH).views] == ["report"]


@pytest.mark.asyncio
async def test_the_service_hands_its_own_pr_and_its_workers_prs_to_the_tabs(
    env: Any, monkeypatch: Any
) -> None:
    from kiro_crew.slack.code_channels.workspace import service as svc

    seen: list[Any] = []

    async def sync(binding: Any, wanted: Any) -> dict[int, str]:
        seen.append([p.number for p in wanted])
        return {}

    monkeypatch.setattr(tabs, "sync_pr_tabs", sync)
    record = SimpleNamespace(
        slack_off="", pr_number=10, pr_url=PR_URL.format(n=10), pr_state="open"
    )
    board = model.BoardSnapshot(
        rows=(_row(pr_number=33, pr_url=PR_URL.format(n=33), pr_state="open"),)
    )
    service = svc.WorkspaceService(lambda: None)
    await service._publish_pr_tabs(_bind(env), record, board)
    assert seen == [[33, 10]]


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
    assert sum("the pr-42 tab was not published" in r.getMessage() for r in caplog.records) == 1


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
                pr_checks="3/5/2",
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
    header = blocks[2]
    assert header["type"] == "rich_text"
    elements = header["elements"][0]["elements"]
    assert elements[1] == {"type": "channel", "channel_id": CH, "tab_id": WORKERS_VIEW}
    assert elements[3] == {"type": "channel", "channel_id": CH, "tab_id": "code"}
    plan = blocks[3]
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
    assert wrapup._pr_tabs(CH) == ((30, "Vpr30"), (12, "Vpr12"))


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
    )
    closed = _row("it_00000007", model.CLOSED, title="Dropped", closed_as="rejected")
    snap = model.BoardSnapshot(rows=(done, *_snapshot().rows, closed))
    blocks = render.workers_view_blocks(snap, _link, now=_NOW)
    last = blocks[-1]["elements"][0]["text"].splitlines()
    assert last == [
        "Rejected · *Dropped*",
        f"Done · *Old fix* · <{PR_URL.format(n=7)}|PR #7> merged",
    ]
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
    assert await tabs.publish_workers(binding, _snapshot(), link_for=_link) == "failed"
    assert tabs.take_retry(CH) > first > 0


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
    slots = {
        "chat-w1": SimpleNamespace(key="chat-w1", running=True),
        "chat-w2": SimpleNamespace(key="chat-w2", running=False),
        "chat-w9": SimpleNamespace(key="chat-w9", running=True),
        SLOT: SimpleNamespace(key=SLOT, running=True),
    }
    env.state.get_slot = lambda key: slots.get(key)
    stopped: list[Any] = []

    async def stop_slot_turn(state: Any, slot: Any, **kw: Any) -> dict[str, Any]:
        stopped.append((slot.key, kw))
        return {"info": ""}

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", stop_slot_turn)
    noted: list[str] = []
    from kiro_crew.slack.code_channels import board

    monkeypatch.setattr(board, "note_session_turn", noted.append)
    return SimpleNamespace(
        binding=binding, other=other, stopped=stopped, reads=reads, noted=noted, boards=boards
    )


@pytest.mark.asyncio
async def test_stop_stops_only_that_workers_turn(env: Any, stop_world: Any) -> None:
    assert await worker_stop._handle(env.orch, _stop_payload()) == "stop:stopping"
    assert stop_world.stopped == [("chat-w1", {"source": "slack", "escalate": False})]
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
