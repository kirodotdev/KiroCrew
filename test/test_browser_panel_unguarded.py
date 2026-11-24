"""The panel hub with its guard off (``browser.xr_pages_guarded``, the default).

A panel then does what the dashboard's own page does: every control takes a
press, no page is for looking at only, the dashboard's own sends pass, and any
page of the gateway's own origin opens but the few that stay shut. Each test
names the rule it holds, and what stays out in both ways.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from browser_panel_helpers import MAIN_FRAME, ORIGIN, Client, make_hub, open_live

from kiro_crew.browser_harness import panel_hub as ph
from kiro_crew.browser_harness import panel_page

TOKEN = "owner-session-token-A"
CLICK = {"type": "mouse", "action": "click", "x": 500, "y": 300}

#: What stays out with the guard off, in the spellings the dashboard's router takes for the same page.
SHUT = [
    "/xr",
    "/XR",
    "/xr/",
    "//xr",
    "/%78r",
    "/xr/anything",
    "/deploy",
    "/Deploy/",
    "/artifacts/deploy",
    "/artifacts/remote/s3/abc",
    "/settings/people",
    "/settings/People/invite",
    "/settings%2Fpeople",
    "/settings/instances",
    "/instances",
    "/instances/",
]

#: Pages that the guarded list leaves out and that open with the guard off.
OPENED = [
    "/settings/connections",
    "/connections",
    "/capabilities",
    "/knowledge",
    "/apps/notes",
    "/apps/notes/page/7",
    "/xrays",
]


# ── the rules, as functions ─────────────────────────────────────────────────


@pytest.mark.parametrize("path", SHUT)
def test_a_page_that_reaches_other_people_or_machines_stays_shut_in_both_ways(path: str) -> None:
    assert panel_page.allowed_path(path, guarded=False) is None
    assert panel_page.allowed_path(path) is None
    assert panel_page.look_only(path, guarded=False) is True
    assert panel_page.allowed_url(ORIGIN + path, ORIGIN, guarded=False) is False


@pytest.mark.parametrize("path", OPENED)
def test_any_other_page_of_the_dashboard_opens_with_the_guard_off(path: str) -> None:
    assert panel_page.allowed_path(path, guarded=False) == path
    assert panel_page.allowed_url(ORIGIN + path, ORIGIN, guarded=False) is True
    assert panel_page.look_only(path, guarded=False) is False
    # With the guard on the list decides, as before.
    assert panel_page.allowed_path(path) is None


@pytest.mark.parametrize(
    "path", ["", "xr", "chat", "/a/../xr", "/a/./b", "/a/%2e%2e/xr", "/" + "a" * 300, None, 7]
)
def test_what_is_not_a_plain_path_opens_in_neither_way(path: Any) -> None:
    assert panel_page.allowed_path(path, guarded=False) is None
    assert panel_page.allowed_path(path) is None


def test_a_name_that_only_begins_like_a_shut_page_opens() -> None:
    for path in ("/artifacts/remote-notes", "/artifacts/deployed", "/settings/people-search"):
        assert panel_page.allowed_path(path, guarded=False) == path


def test_another_origin_stays_out_with_the_guard_off() -> None:
    assert panel_page.allowed_url("https://evil.example/chat", ORIGIN, guarded=False) is False
    assert panel_page.allowed_url("http://127.0.0.1:6778/chat", ORIGIN, guarded=False) is False


@pytest.mark.parametrize("path", ["/projects", "/projects/p1/tasks/t-2", "/tasks", "/schedule"])
def test_no_page_is_for_looking_at_only_with_the_guard_off(path: str) -> None:
    assert panel_page.look_only(path) is True
    assert panel_page.look_only(path, guarded=False) is False


def test_a_path_the_hub_does_not_know_takes_no_press_in_either_way() -> None:
    assert panel_page.look_only("", guarded=False) is True
    assert panel_page.look_only(None, guarded=False) is True


@pytest.mark.parametrize(
    "address",
    [
        "/chat?autoSend=1",
        "/chat/abc?AUTOSEND=true",
        "/projects?applied=t-1&autoRun=1",
        "/settings?tab=connections",
        "/settings?tab=browser",
        "/chat?tab=people",
        "/members?create=1&name=x&goal=y",
    ],
)
def test_the_dashboards_own_sends_and_every_open_tab_pass_with_the_guard_off(address: str) -> None:
    assert panel_page.query_refused(ORIGIN + address, guarded=False) is False
    assert panel_page.allowed_open(address, {"mate"}, guarded=False) == address


@pytest.mark.parametrize(
    "address",
    [
        "/chat?token=abc",
        "/chat?TOKEN=abc",
        "/chat?autoSend=1&token=abc",
        "/settings?tab=people",
        "/settings?tab=instances",
        "/settings?tab=People",
        "/Settings/?tab=people",
        "/settings?tab=browser&tab=instances",
    ],
)
def test_a_token_and_a_shut_settings_tab_stay_refused_with_the_guard_off(address: str) -> None:
    assert panel_page.query_refused(ORIGIN + address, guarded=False) is True
    assert panel_page.allowed_open(address, {"mate"}, guarded=False) is None


def test_a_bot_named_to_the_bots_page_is_one_of_the_owners_in_both_ways() -> None:
    known = {"mate", "Ada Lovelace"}
    for guarded in (True, False):
        assert (
            panel_page.allowed_open("/members?member=mate", known, guarded=guarded)
            == "/members?member=mate"
        )
        assert panel_page.allowed_open("/members?member=nobody", known, guarded=guarded) is None
        assert panel_page.allowed_open("/members?member=mate", None, guarded=guarded) is None
    # With the guard off the Bots page takes other words too, for a Bot that is the owner's.
    assert (
        panel_page.allowed_open("/members?member=mate&view=goals", known, guarded=False)
        == "/members?member=mate&view=goals"
    )
    assert (
        panel_page.allowed_open("/members?member=nobody&view=goals", known, guarded=False) is None
    )
    assert panel_page.allowed_open("/Members/?member=nobody&x=1", known, guarded=False) is None


@pytest.mark.parametrize("address", ["/chat#top", "/chat x", "/chat\n", "/chät", "chat", ""])
def test_an_address_that_a_browser_would_not_write_is_not_opened(address: str) -> None:
    assert panel_page.allowed_open(address, {"mate"}, guarded=False) is None


def test_the_in_page_guard_answers_no_for_every_element_with_the_guard_off() -> None:
    assert panel_page.guard_script() == panel_page.GUARD_JS
    script = panel_page.guard_script(guarded=False)
    assert script == "globalThis.__kcPanelUnguarded=true;" + panel_page.GUARD_JS
    # The word is read once, in the isolated world, before the walk for a mark.
    assert "const OFF=globalThis.__kcPanelUnguarded===true;" in panel_page.GUARD_JS
    assert "const guarded=el=>{if(OFF)return false;" in panel_page.GUARD_JS


def test_the_move_guard_carries_what_stays_shut_with_the_guard_off() -> None:
    script = panel_page.move_guard_script(guarded=False)
    assert script.endswith(panel_page.MOVE_JS)
    assert json.dumps([shape.pattern for shape in panel_page.SHUT_SHAPES]) in script
    assert "globalThis.__kcPanelLaunch=" + json.dumps(["token"]) + ";" in script
    assert "__kcPanelShapes=" not in script
    # With the guard on nothing names what is shut, and the script reads the list.
    assert "__kcPanelShut=" not in panel_page.move_guard_script()
    assert "(SHUT?open(p):SHAPES.some(r=>r.test(p)))" in panel_page.MOVE_JS


# ── the hub ─────────────────────────────────────────────────────────────────

on_loop = pytest.mark.asyncio


@on_loop
async def test_the_tab_gets_the_scripts_of_the_way_it_was_opened_in() -> None:
    hub, browser, _clock = make_hub(guarded=False)
    client = Client(1, TOKEN)
    panel = await open_live(hub, browser, client)
    assert panel.guarded is False
    sources = [
        p["source"] for _, p, _ in browser.relay.methods("Page.addScriptToEvaluateOnNewDocument")
    ]
    assert sources[0] == panel_page.guard_script(guarded=False)
    assert sources[1] == panel_page.move_guard_script(guarded=False)


@on_loop
async def test_every_control_takes_a_press_keys_and_text() -> None:
    hub, browser, _clock = make_hub(guarded=False)
    client = Client(1, TOKEN)
    panel = await open_live(hub, browser, client)
    relay = browser.relay
    # The page's reader says of an Approve button what it says with the guard off.
    relay.target_answer = {
        "rect": [480, 290, 90, 24],
        "kind": "button",
        "label": "Approve",
        "guarded": False,
    }
    token, handle = client.socket.token, panel.handle
    out = await hub.input(token, handle, CLICK)
    assert out["sent"] == 2 and "guarded" not in out
    assert (await hub.input(token, handle, {"type": "text", "text": "yes"}))["sent"] >= 1
    assert (await hub.input(token, handle, {"type": "key", "key": "Enter"}))["sent"] >= 1


@on_loop
@pytest.mark.parametrize("address", ["/projects/p1", "/tasks", "/schedule?job=nightly"])
async def test_the_task_runner_and_schedule_take_a_press_keys_and_text(address: str) -> None:
    hub, browser, _clock = make_hub(guarded=False)
    client = Client(1, TOKEN)
    panel = await open_live(hub, browser, client)
    relay = browser.relay
    await relay.push(
        panel.target_id,
        "Page.navigatedWithinDocument",
        {"frameId": MAIN_FRAME, "url": ORIGIN + address},
    )
    token, handle = client.socket.token, panel.handle
    for action, sent in (("down", 1), ("up", 1), ("click", 2)):
        out = await hub.input(token, handle, {**CLICK, "action": action})
        assert out["sent"] == sent and "guarded" not in out
    assert (await hub.input(token, handle, {"type": "text", "text": "run it"}))["sent"] >= 1
    assert (await hub.input(token, handle, {"type": "key", "key": "Enter"}))["sent"] >= 1
    assert relay.methods("Input.insertText") or relay.methods("Input.dispatchKeyEvent")


@on_loop
@pytest.mark.parametrize(
    "address", ["/chat?autoSend=1", "/projects?applied=t-1&autoRun=1", "/settings?tab=connections"]
)
async def test_a_send_of_the_dashboards_own_passes_for_a_load_and_for_a_move(address: str) -> None:
    hub, browser, _clock = make_hub(guarded=False)
    client = Client(1, TOKEN)
    panel = await open_live(hub, browser, client)
    relay = browser.relay
    await relay.push(
        panel.target_id,
        "Fetch.requestPaused",
        {"requestId": "main", "frameId": MAIN_FRAME, "request": {"url": ORIGIN + address}},
    )
    await relay.push(
        panel.target_id,
        "Fetch.requestPaused",
        {"requestId": "framed", "frameId": "F2", "request": {"url": ORIGIN + address}},
    )
    assert [p["requestId"] for _, p, _ in relay.methods("Fetch.continueRequest")] == [
        "main",
        "framed",
    ]
    await relay.push(
        panel.target_id,
        "Page.frameNavigated",
        {"frame": {"id": MAIN_FRAME, "url": ORIGIN + address}},
    )
    await relay.push(
        panel.target_id,
        "Page.navigatedWithinDocument",
        {"frameId": MAIN_FRAME, "url": ORIGIN + address},
    )
    assert panel.status == ph.LIVE and panel.last_url == ORIGIN + address
    assert relay.methods("Fetch.failRequest") == []
    assert [m for m in client.told if m.get("op") == "refused"] == []
    # What the panel is told of it is the path, and nothing of the query.
    assert panel_page.address_of(panel.last_url) == address.split("?")[0]


@on_loop
@pytest.mark.parametrize("path", OPENED)
async def test_a_page_off_the_guarded_list_opens_and_a_move_to_it_arrives(path: str) -> None:
    hub, browser, _clock = make_hub(guarded=False)
    client = Client(1, TOKEN)
    panel = await open_live(hub, browser, client, path=path)
    assert panel.status == ph.LIVE and panel.last_url == ORIGIN + path
    other = await open_live(hub, browser, client, panel_id="b")
    await browser.relay.push(
        other.target_id,
        "Page.navigatedWithinDocument",
        {"frameId": MAIN_FRAME, "url": ORIGIN + path},
    )
    assert other.last_url == ORIGIN + path
    assert client.last("path") == {"op": "path", "id": "b", "path": path}
    assert [m for m in client.told if m.get("op") == "refused"] == []


STAYS_OUT = [*SHUT, "/chat?token=abc", "/settings?tab=people", "/settings?tab=instances"]


@on_loop
@pytest.mark.parametrize("address", STAYS_OUT)
async def test_what_stays_out_is_refused_by_an_open(address: str) -> None:
    hub, browser, _clock = make_hub(guarded=False)
    client = Client(1, TOKEN)
    await hub.open(client.socket, "a", path=address, width=1024, height=720)
    assert client.told == [{"op": "status", "id": "a", "status": "failed", "why": "address"}]
    assert browser.ensures == 0 and browser.contexts == []


@on_loop
@pytest.mark.parametrize("address", STAYS_OUT)
async def test_what_stays_out_is_refused_by_a_load_and_undone_after_a_move(address: str) -> None:
    hub, browser, _clock = make_hub(guarded=False)
    client = Client(1, TOKEN)
    panel = await open_live(hub, browser, client)
    relay = browser.relay
    await relay.push(
        panel.target_id,
        "Fetch.requestPaused",
        {"requestId": "main", "frameId": MAIN_FRAME, "request": {"url": ORIGIN + address}},
    )
    assert [p["requestId"] for _, p, _ in relay.methods("Fetch.failRequest")] == ["main"]
    assert client.last("refused") == {"op": "refused", "id": "a", "why": "address"}
    told = len(client.told)
    await relay.push(
        panel.target_id,
        "Page.navigatedWithinDocument",
        {"frameId": MAIN_FRAME, "url": ORIGIN + address},
    )
    backs = [
        p for _, p, _ in relay.methods("Runtime.evaluate") if "history.back" in p["expression"]
    ]
    assert len(backs) == 1 and panel.last_url == ORIGIN + "/chat"
    assert client.told[told:] == [{"op": "refused", "id": "a", "why": "address"}]
    # A document that lands there all the same ends the panel.
    await relay.push(
        panel.target_id,
        "Page.frameNavigated",
        {"frame": {"id": MAIN_FRAME, "url": ORIGIN + address}},
    )
    assert client.last("status")["status"] == "failed" and client.last("status")["why"] == "address"


@on_loop
async def test_a_load_of_another_origin_and_a_frame_with_a_token_are_refused() -> None:
    hub, browser, _clock = make_hub(guarded=False)
    client = Client(1, TOKEN)
    panel = await open_live(hub, browser, client)
    relay = browser.relay
    for request_id, frame, url in (
        ("away", MAIN_FRAME, "https://evil.example/chat"),
        ("framed", "F2", ORIGIN + "/chat?token=abc"),
        ("framed-away", "F2", "https://evil.example/"),
    ):
        await relay.push(
            panel.target_id,
            "Fetch.requestPaused",
            {"requestId": request_id, "frameId": frame, "request": {"url": url}},
        )
    assert [p["requestId"] for _, p, _ in relay.methods("Fetch.failRequest")] == [
        "away",
        "framed",
        "framed-away",
    ]
    assert relay.methods("Fetch.continueRequest") == []


@on_loop
async def test_a_tab_whose_path_the_hub_does_not_know_takes_no_press() -> None:
    hub, browser, _clock = make_hub(guarded=False)
    client = Client(1, TOKEN)
    panel = await open_live(hub, browser, client)
    panel.last_url = ""
    out = await hub.input(client.socket.token, panel.handle, CLICK)
    assert out == {"sent": 0, "guarded": True}


@on_loop
async def test_a_press_is_not_sent_blind_when_the_page_cannot_be_asked() -> None:
    hub, browser, _clock = make_hub(guarded=False)
    client = Client(1, TOKEN)
    panel = await open_live(hub, browser, client)
    browser.relay.evaluate_fails = True
    out = await hub.input(client.socket.token, panel.handle, CLICK)
    assert out == {"sent": 0, "unread": True}


@on_loop
@pytest.mark.parametrize("opened_guarded", [True, False])
async def test_a_change_of_the_guard_closes_every_panel(opened_guarded: bool) -> None:
    hub, browser, _clock = make_hub(guarded=opened_guarded)
    client = Client(1, TOKEN)
    panel = await open_live(hub, browser, client)
    await hub._tick(panel)
    assert hub.panel_count() == 1
    closed = asyncio.Event()
    changed = hub._way_changed

    async def change_and_say() -> None:
        await changed()
        closed.set()

    hub._way_changed = change_and_say  # type: ignore[method-assign]
    hub._guarded = lambda: not opened_guarded
    await hub._tick(panel)
    await asyncio.wait_for(closed.wait(), 5)
    assert hub.panel_count() == 0
    # The panel is told that it closed, and why: its page opens it again in the new way.
    assert client.last("status") == {
        "op": "status",
        "id": panel.id,
        "status": ph.CLOSED,
        "why": "way",
    }


@on_loop
@pytest.mark.parametrize("guarded", [True, False])
async def test_a_socket_that_asks_is_told_which_way_holds(guarded: bool) -> None:
    hub, _browser, _clock = make_hub(guarded=guarded)
    client = Client(1, TOKEN)
    await hub.way(client.socket)
    assert client.told == [{"op": "way", "guarded": guarded}]
    # Asked again, it is told again: the page asks before a page comes up by itself.
    await hub.way(client.socket)
    assert client.told == [{"op": "way", "guarded": guarded}] * 2


@on_loop
async def test_a_socket_that_asked_is_told_when_the_guard_changes_and_keeps_its_sign_in() -> None:
    hub, browser, _clock = make_hub(guarded=True)
    asked, silent = Client(1, TOKEN), Client(2, TOKEN)
    await hub.way(asked.socket)
    first = await open_live(hub, browser, asked)
    await open_live(hub, browser, silent)
    assert first.guarded is True
    hub._guarded = lambda: False
    await hub._way_changed()
    assert hub.panel_count() == 0
    assert asked.last("way") == {"op": "way", "guarded": False}
    # A socket that never asked is told nothing of the way: only that its panel closed.
    assert [m["op"] for m in silent.told if m["op"] == "way"] == []
    assert silent.last("status")["why"] == "way"
    # The sign-in is the socket's own still: its page opens the panel again, and the tab is of the new way.
    assert asked.socket.token == TOKEN
    again = await open_live(hub, browser, asked)
    assert again.guarded is False
    assert (await hub.input(asked.socket.token, again.handle, CLICK))["sent"] > 0
    # No change, no second word.
    told = len(asked.told)
    await hub._way_changed()
    assert [m for m in asked.told[told:] if m["op"] == "way"] == []


@on_loop
async def test_a_socket_that_is_gone_is_told_nothing_more() -> None:
    hub, _browser, _clock = make_hub(guarded=True)
    client = Client(1, TOKEN)
    await hub.way(client.socket)
    await hub.close_socket(client.socket.key)
    hub._guarded = lambda: False
    await hub._way_changed()
    assert client.told == [{"op": "way", "guarded": True}]


def test_only_the_settings_own_off_takes_the_guard_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.config import live as live_config
    from kiro_crew.config.loader import KiroCrewConfig

    def boom() -> Any:
        raise RuntimeError("no config")

    monkeypatch.setattr(live_config, "snapshot", boom)
    assert ph._default_guarded() is True
    for unread in (None, SimpleNamespace(), SimpleNamespace(browser=SimpleNamespace())):
        monkeypatch.setattr(live_config, "snapshot", lambda value=unread: value)
        assert ph._default_guarded() is True
    for value, guarded in ((True, True), (False, False), ("false", True), (0, True)):
        snap = SimpleNamespace(browser=SimpleNamespace(xr_pages_guarded=value))
        monkeypatch.setattr(live_config, "snapshot", lambda value=snap: value)
        assert ph._default_guarded() is guarded
    # The product's own default: off.
    monkeypatch.setattr(live_config, "snapshot", lambda: KiroCrewConfig())
    assert ph._default_guarded() is False
    # A reader that answers anything but its own off leaves the guard on.
    hub, _browser, _clock = make_hub()
    hub._guarded = lambda: None  # type: ignore[assignment,return-value]
    assert hub.guarded() is True
