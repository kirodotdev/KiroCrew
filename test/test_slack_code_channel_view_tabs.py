"""Code-channel view tabs: native PR tabs, tab deep links, the interactive Workers tab.

The tab budget, PR tabs, tab deep links and the Workers tab.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from slack_code_channel_view_tabs_helpers import (  # noqa: F401 - fixtures by name
    _FAKE_KEY,
    _NOW,
    _OURS,
    _TABS,
    CH,
    PR_URL,
    SESSION,
    WORKERS_VIEW,
    _bind,
    _calls,
    _channel_elements,
    _link,
    _rate_limited,
    _Refusing,
    _row,
    _scope_flag,
    _set_views,
    _snapshot,
    _texts,
    _v,
    env_fixture,
    no_host_dns_fixture,
)
from slack_code_channels_helpers import OWNER

from kiro_crew.slack.code_channels import surface, tabs, tools, views, wrapup
from kiro_crew.slack.code_channels.board import model, publisher, render, source
from kiro_crew.slack.code_channels.surface_store import store as surface_store


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
async def test_a_pr_tab_removed_for_a_write_that_then_fails_still_refreshes_the_board(
    env: Any, monkeypatch: Any
) -> None:
    from kiro_crew.slack.code_channels import board
    from kiro_crew.slack.code_channels.models import CodeChannelApiError

    _bind(env)
    surface_store().update(CH, views=[_v("pr-12", "pull_request", "1")])
    listed = [{"view_id": "Vpr12"}] + [{"view_id": f"Vtheirs{n}"} for n in range(4)]
    _set_views(env, listed)
    refreshed: list[str] = []
    monkeypatch.setattr(board, "_schedule", lambda ch, delay=None: refreshed.append(ch) or True)
    answers = ["too_many_views", "internal_error"]  # full, then the retry fails

    async def write() -> Any:
        raise CodeChannelApiError("agents.conversations.setView", answers.pop(0))

    with pytest.raises(CodeChannelApiError):
        await views.write_view(surface.client(), CH, key="plan", type="canvas", write=write)
    assert _calls(env, "agents.conversations.removeView") == [
        {"channel_id": CH, "view_id": "Vpr12"}
    ]
    assert refreshed == [CH]  # the board and the Workers tab drop the link anyway


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


def test_the_wrap_up_links_no_pr_tab_of_a_replaced_conductors_workers(
    env: Any, monkeypatch: Any
) -> None:
    from kiro_crew.slack.code_channels.workspace import core as workspace_core

    asked: list[str] = []
    theirs = model.BoardSnapshot(
        rows=(_row(pr_number=33, pr_url=PR_URL.format(n=33), pr_state="open"),)
    )

    class _Service:
        def board(self, team_id: str, channel_id: str, session_key: str = "") -> Any:
            asked.append(session_key)
            return None if session_key else theirs  # cached for another conductor

    monkeypatch.setattr(workspace_core, "service", lambda: _Service())
    binding = _bind(env)
    assert wrapup._open_prs(binding, SimpleNamespace(pr_number=0, pr_state="")) == set()
    assert asked == [binding.session_key]
