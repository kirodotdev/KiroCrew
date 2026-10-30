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

    async def sync(binding: Any, wanted: Any, settled: Any = None) -> dict[int, str]:
        seen.append(([p.number for p in wanted], settled))
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
    assert seen == [([33, 10], {7})]


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
        pr_checks="12/12/0",
    )
    shipped = _row(
        "it_00000008",
        model.DONE,
        title="Fix login",
        pr_number=12,
        pr_url=PR_URL.format(n=12),
        pr_state="open",
        pr_checks="41/47/6",
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

    async def make_room(client: Any, channel: str, *, for_key: str) -> str:
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
    stops.note(slot, OWNER)
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
        for mod in (worker_stop, actions):
            monkeypatch.setattr(mod, "live_config", loader("live_config"))
        monkeypatch.setattr(platform_context, "current_context", loader("current_context"))
        monkeypatch.setattr(platform_pkg, "current_context", loader("current_context"))
        return snapshot

    monkeypatch.setattr(source, "read_board", read_board)
    assert (await worker_stop._handle(env.orch, _stop_payload())).startswith("stop:")
    assert reached == [] and len(stop_world.stopped) == 1
