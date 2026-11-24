"""The panel hub: who may open a panel, what its tab may show, where its pictures go.

Each test names the rule it holds. The browser is ``FakeBrowser`` (nothing starts),
the tab is ``PanelRelay``, and the stream is the real ``PanelStream`` on a fake clock.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from browser_live_helpers import frame_event, make_jpeg
from browser_panel_helpers import MAIN_FRAME, ORIGIN, Client, make_hub, open_live

from kiro_crew.browser_harness import panel_hub as ph
from kiro_crew.browser_harness import panel_page, panel_window
from kiro_crew.browser_harness import screencast as sc
from kiro_crew.browser_harness.relay import CdpError, RelayClosed
from kiro_crew.browser_harness.takeover import InputEventError

pytestmark = pytest.mark.asyncio

TOKEN_A = "owner-session-token-A"
TOKEN_B = "owner-session-token-B"
CLICK = {"type": "mouse", "action": "click", "x": 500, "y": 300}


async def _picture(browser: Any, panel: ph.Panel, payload: int = 512) -> None:
    event = frame_event(make_jpeg(2048, 1440, payload), device_w=1024, device_h=720)
    await browser.relay.push(panel.target_id, event["method"], event["params"])


# ── off by default, and fail closed ─────────────────────────────────────────


async def test_with_the_setting_off_no_browser_starts_and_input_is_refused() -> None:
    hub, browser, _clock = make_hub(enabled=False)
    client = Client(1, TOKEN_A)
    await hub.open(client.socket, "a", path="/chat", width=1024, height=720)
    assert client.told == [{"op": "status", "id": "a", "status": "failed", "why": "off"}]
    assert browser.ensures == 0 and browser.contexts == []
    with pytest.raises(ph.PanelRefused) as refused:
        await hub.input(client.socket.token, "p1", CLICK)
    assert refused.value.code == "off" and refused.value.status == 403


async def test_the_default_reader_of_the_setting_is_off_when_config_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.config import live as live_config

    def boom() -> Any:
        raise RuntimeError("no config")

    monkeypatch.setattr(live_config, "snapshot", boom)
    assert ph._default_enabled() is False
    monkeypatch.setattr(live_config, "snapshot", lambda: None)
    assert ph._default_enabled() is False


async def test_a_browse_deny_starts_nothing() -> None:
    hub, browser, _clock = make_hub(denied=True)
    client = Client(1, TOKEN_A)
    await hub.open(client.socket, "a", path="/chat", width=1024, height=720)
    assert client.last("status")["why"] == "policy_denied"
    assert browser.ensures == 0


async def test_a_socket_with_no_verified_sign_in_gets_no_panel() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, "")
    await hub.open(client.socket, "a", path="/chat", width=1024, height=720)
    assert client.last("status") == {
        "op": "status",
        "id": "a",
        "status": "failed",
        "why": "not_signed_in",
    }
    assert browser.ensures == 0


async def test_the_gates_own_refusal_ends_the_panel_as_signed_out() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    await browser.relay.push(
        panel.target_id,
        "Network.responseReceived",
        {"response": {"status": 403, "headers": {"X-Auth-Required": "true"}}},
    )
    assert client.last("status")["status"] == "signed-out"
    assert not panel.stream.running
    before = len(client.pictures)
    await _picture(browser, panel)
    assert len(client.pictures) == before
    with pytest.raises(ph.PanelRefused) as refused:
        await hub.input(client.socket.token, panel.handle, CLICK)
    assert refused.value.code == "unknown_panel"


# ── addresses ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/xr",
        "/chat?token=abc",
        "/chat?autoSend=1",
        # Pages that publish.
        "/deploy",
        "/artifacts/deploy",
        "/artifacts/remote/github/42",
        # Every app's page, and a built-in app's own route.
        "/apps",
        "/apps/library",
        "/apps/detail/notes",
        "/apps/notes",
        "/aws-control",
        "/aws-control/usage",
        # Pages that set up who or what reaches the gateway or the agent.
        "/channels",
        "/channels/slack",
        "/connections",
        "/instances",
        "/mcp",
        "/hooks",
        "/webhooks",
        "/agents",
        "/mc-agents",
        "/capabilities",
        "/developer",
        "/developer/layout-editor",
        # It only leads on to /capabilities.
        "/knowledge",
        # Opening it admits the people who accepted an invite.
        "/settings/people",
        "/settings/people/invites",
        # Opening it starts the enabled MCP servers and a sign-in helper; /connections leads here.
        "/settings/connections",
        "/settings/connections/github",
        # /instances leads here.
        "/settings/instances",
        # Pop-out and embed routes.
        "/popout/chat",
        "/popout/app/notes",
        "/embed/chat",
        # A listed page has no page under it unless its shape says so.
        "/members/mate",
        "/sessions/x",
        "/notifications/1",
        "/logs/today",
        "/members/",
        "/orchestrated/x",
        "/chat/../members",
        "/chat/..",
        "/artifacts/..",
        "/settings/.",
        "//evil.example/chat",
        "https://evil.example/chat",
        "/api/config",
        "/settings/" + "a" * 80,
        "",
        None,
        7,
    ],
)
async def test_an_address_outside_the_list_is_refused_before_any_browser(path: Any) -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    await hub.open(client.socket, "a", path=path, width=1024, height=720)
    assert client.last("status")["why"] == "address"
    assert browser.ensures == 0


@pytest.mark.parametrize(
    "path",
    [
        "/chat",
        "/chat/dashboard-ab12",
        "/members",
        "/notifications",
        "/sessions",
        "/session-dashboards",
        "/overview",
        "/crew-board",
        "/schedule",
        "/projects",
        "/projects/p1",
        "/projects/p1/tasks/t-2",
        "/tasks",
        "/artifacts",
        "/artifacts/x-1",
        "/artifacts/remote",
        "/logs",
        "/settings",
        "/settings/browser",
        "/settings/agent/models/list",
        "/settings/peoples",
    ],
)
async def test_a_listed_page_opens_on_the_gateways_own_origin(path: str) -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    await hub.open(client.socket, "a", path=path, width=1024, height=720)
    assert client.last("opened")["css"] == [1024, 720]
    (navigate,) = browser.relay.methods("Page.navigate")
    assert navigate[1] == {"url": ORIGIN + path}


async def test_a_redirect_off_the_origin_or_off_the_list_is_failed() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    relay = browser.relay
    for n, url in enumerate(
        ["https://evil.example/chat", ORIGIN + "/capabilities", "http://127.0.0.1:1234/chat"]
    ):
        await relay.push(
            panel.target_id,
            "Fetch.requestPaused",
            {"requestId": f"r{n}", "frameId": MAIN_FRAME, "request": {"url": url}},
        )
    assert [p["requestId"] for _, p, _ in relay.methods("Fetch.failRequest")] == ["r0", "r1", "r2"]
    assert relay.methods("Fetch.continueRequest") == []
    assert client.last("refused") == {"op": "refused", "id": "a", "why": "address"}
    await relay.push(
        panel.target_id,
        "Fetch.requestPaused",
        {"requestId": "ok", "frameId": MAIN_FRAME, "request": {"url": ORIGIN + "/settings"}},
    )
    assert [p["requestId"] for _, p, _ in relay.methods("Fetch.continueRequest")] == ["ok"]


async def test_a_move_inside_the_page_to_an_unlisted_path_is_undone() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    await browser.relay.push(
        panel.target_id,
        "Page.navigatedWithinDocument",
        {"frameId": MAIN_FRAME, "url": ORIGIN + "/capabilities?tab=knowledge"},
    )
    backs = [
        p
        for _, p, _ in browser.relay.methods("Runtime.evaluate")
        if "history.back" in p["expression"]
    ]
    assert len(backs) == 1
    assert client.last("refused")["why"] == "address"
    assert panel.last_url == ORIGIN + "/chat"


async def test_a_link_from_an_artifact_to_the_list_of_artifacts_goes_through() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client, path="/artifacts/x-1")
    assert client.last("path") == {"op": "path", "id": "a", "path": "/artifacts/x-1"}
    await browser.relay.push(
        panel.target_id,
        "Page.navigatedWithinDocument",
        {"frameId": MAIN_FRAME, "url": ORIGIN + "/artifacts"},
    )
    assert panel.last_url == ORIGIN + "/artifacts" and panel.status == ph.LIVE
    assert client.last("path") == {"op": "path", "id": "a", "path": "/artifacts"}
    assert [m for m in client.told if m.get("op") == "refused"] == []
    assert not [
        p
        for _, p, _ in browser.relay.methods("Runtime.evaluate")
        if "history.back" in p["expression"]
    ]


async def test_the_panel_is_told_the_path_its_tab_shows_and_nothing_else_of_the_address() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    relay = browser.relay
    await relay.push(
        panel.target_id,
        "Page.navigatedWithinDocument",
        {"frameId": MAIN_FRAME, "url": ORIGIN + "/chat/dashboard-ab12?sid=secret-1#row-9"},
    )
    assert client.last("path") == {"op": "path", "id": "a", "path": "/chat/dashboard-ab12"}
    told = len([m for m in client.told if m.get("op") == "path"])
    # The same path with another query is the same page: nothing is told again.
    await relay.push(
        panel.target_id,
        "Page.navigatedWithinDocument",
        {"frameId": MAIN_FRAME, "url": ORIGIN + "/chat/dashboard-ab12?sid=secret-2"},
    )
    assert len([m for m in client.told if m.get("op") == "path"]) == told
    # A load of another listed page (a redirect, a link that is no in-page move) is told too.
    await relay.push(
        panel.target_id,
        "Page.frameNavigated",
        {"frame": {"id": MAIN_FRAME, "url": ORIGIN + "/settings/overview"}},
    )
    assert client.last("path") == {"op": "path", "id": "a", "path": "/settings/overview"}
    assert not any("secret" in str(message) for message in client.told)


LAUNCHES = [
    "/chat/withtoken?token=abc",
    "/chat?autoSend=1&newSession=1",
    "/chat?x=1&AutoSend=1",
    "/projects?applied=t-1&autoRun=true",
    "/projects?applied=t-1&auto%52un=true",
    "/tasks?autoRun=false&autoRun=true",
]

# The Settings page draws the tab its query names when its path names none, so a
# tab that the list leaves out is asked for by the bare path too.
TABS_LEFT_OUT = [
    "/settings?tab=connections",
    "/settings?tab=people",
    "/settings?tab=instances",
    "/settings?tab=%63onnections",
    "/settings?tab=peopl%65",
    "/settings?x=1&tab=inst%61nces",
    "/settings?tab=browser&tab=connections",
    "/settings?tab=people&tab=browser",
    "/settings?tab=Connections",
    "/settings?tab=",
]
REFUSED = LAUNCHES + TABS_LEFT_OUT


@pytest.mark.parametrize("address", REFUSED)
async def test_the_tab_loads_no_address_that_makes_the_dashboard_act_as_it_loads(
    address: str,
) -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    relay = browser.relay
    # A link that was pressed in a reply, a redirect, a script of the page: each
    # is a request for the tab's own document, and each is paused.
    await relay.push(
        panel.target_id,
        "Fetch.requestPaused",
        {"requestId": "launch", "frameId": MAIN_FRAME, "request": {"url": ORIGIN + address}},
    )
    assert [p["requestId"] for _, p, _ in relay.methods("Fetch.failRequest")] == ["launch"]
    assert relay.methods("Fetch.continueRequest") == []
    assert client.last("refused") == {"op": "refused", "id": "a", "why": "address"}
    assert panel.status == ph.LIVE and panel.last_url == ORIGIN + "/chat"


async def test_any_other_query_of_a_listed_page_loads() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    relay = browser.relay
    for n, address in enumerate(
        [
            "/chat?new=1",
            "/settings?q=tokens",
            "/chat/a?tokens=1&auto=1",
            "/projects?applied=t-1",
            "/settings?tab=browser",
            "/settings?tab=agent&tab=browser",
            # The path's tab wins, and one that Settings does not have draws the first pane.
            "/settings/x?tab=connections",
            # Only Settings takes a tab from its query in place of a path.
            "/chat?tab=connections",
        ]
    ):
        await relay.push(
            panel.target_id,
            "Fetch.requestPaused",
            {"requestId": f"q{n}", "frameId": MAIN_FRAME, "request": {"url": ORIGIN + address}},
        )
    assert [p["requestId"] for _, p, _ in relay.methods("Fetch.continueRequest")] == [
        "q0",
        "q1",
        "q2",
        "q3",
        "q4",
        "q5",
        "q6",
        "q7",
    ]
    assert [m for m in client.told if m.get("op") == "refused"] == []


@pytest.mark.parametrize("address", REFUSED)
async def test_a_move_inside_the_page_to_an_address_that_acts_is_undone(address: str) -> None:
    # The second net: the tab's own guard stops such a move before it happens.
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client, path="/notifications")
    await browser.relay.push(
        panel.target_id,
        "Page.navigatedWithinDocument",
        {"frameId": MAIN_FRAME, "url": ORIGIN + address},
    )
    backs = [
        p
        for _, p, _ in browser.relay.methods("Runtime.evaluate")
        if "history.back" in p["expression"]
    ]
    assert len(backs) == 1
    assert client.last("refused") == {"op": "refused", "id": "a", "why": "address"}
    assert panel.last_url == ORIGIN + "/notifications" and panel.status == ph.LIVE
    assert client.last("path")["path"] == "/notifications"


@pytest.mark.parametrize(
    "address", ["/projects?applied=t-1", "/settings?tab=browser", "/settings/x?tab=connections"]
)
async def test_a_move_inside_the_page_with_a_query_that_starts_nothing_goes_through(
    address: str,
) -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    await browser.relay.push(
        panel.target_id,
        "Page.navigatedWithinDocument",
        {"frameId": MAIN_FRAME, "url": ORIGIN + address},
    )
    assert panel.last_url == ORIGIN + address
    assert client.last("path") == {"op": "path", "id": "a", "path": address.split("?")[0]}
    assert [m for m in client.told if m.get("op") == "refused"] == []


@pytest.mark.parametrize("address", REFUSED)
async def test_a_frame_loads_no_address_that_makes_the_dashboard_act_as_it_loads(
    address: str,
) -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    relay = browser.relay
    await relay.push(
        panel.target_id,
        "Fetch.requestPaused",
        {"requestId": "framed", "frameId": "F2", "request": {"url": ORIGIN + address}},
    )
    assert [p["requestId"] for _, p, _ in relay.methods("Fetch.failRequest")] == ["framed"]
    assert client.last("refused") == {"op": "refused", "id": "a", "why": "address"}
    # A frame of the gateway's own with any other query, and one of the page's own making, load.
    for n, url in enumerate(
        [
            ORIGIN + "/api/sandbox-doc/7/abc?theme=dark",
            "data:text/html,<p>?token=1</p>",
            "about:blank",
        ]
    ):
        await relay.push(
            panel.target_id,
            "Fetch.requestPaused",
            {"requestId": f"f{n}", "frameId": "F2", "request": {"url": url}},
        )
    assert [p["requestId"] for _, p, _ in relay.methods("Fetch.continueRequest")] == [
        "f0",
        "f1",
        "f2",
    ]
    assert len([m for m in client.told if m.get("op") == "refused"]) == 1


async def test_a_move_inside_the_page_that_its_guard_stopped_gives_the_panel_its_line() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client, path="/settings/browser")
    # The tab got the move guard with the gateway's own list in front of it, in the isolated world.
    scripts = [p for _, p, _ in browser.relay.methods("Page.addScriptToEvaluateOnNewDocument")]
    (guard,) = [p for p in scripts if panel_page.MOVE_JS in p["source"]]
    assert guard["worldName"] == panel_page.PANEL_WORLD and guard["runImmediately"] is True
    assert guard["source"] == panel_page.move_guard_script()
    await browser.relay.push(
        panel.target_id,
        "Runtime.bindingCalled",
        {"name": panel_page.GUARD_BINDING, "payload": panel_page.MOVE_STOPPED},
    )
    assert client.last("refused") == {"op": "refused", "id": "a", "why": "address"}
    assert panel.status == ph.LIVE and panel.last_url == ORIGIN + "/settings/browser"
    # A press that the guard stopped still says "guarded".
    await browser.relay.push(
        panel.target_id,
        "Runtime.bindingCalled",
        {"name": panel_page.GUARD_BINDING, "payload": "click"},
    )
    assert client.told[-1] == {"op": "guarded", "id": "a"}


async def test_a_window_that_the_page_asks_for_is_handed_to_the_xr_page_or_refused() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    # The tab asks for windows instead of opening them: the page's own window.open in its main world, and a link
    # that names another window heard in the panel's world.
    scripts = [p for _, p, _ in browser.relay.methods("Page.addScriptToEvaluateOnNewDocument")]
    (opener,) = [p for p in scripts if p["source"] == panel_window.OPEN_JS]
    (asker,) = [p for p in scripts if p["source"] == panel_window.ASK_JS]
    assert "worldName" not in opener and asker["worldName"] == panel_page.PANEL_WORLD

    async def ask(payload: str) -> None:
        await browser.relay.push(
            panel.target_id,
            "Runtime.bindingCalled",
            {"name": panel_page.GUARD_BINDING, "payload": payload},
        )

    # A page of the gateway's own origin that the panel may show: the XR page opens it.
    await ask(panel_window.WINDOW_PREFIX + ORIGIN + "/settings/browser")
    assert client.told[-1] == {"op": "window", "id": "a", "path": "/settings/browser"}
    # Another origin, a path that stays shut, a page of the page's own making: nothing opens, and the panel says
    # its line.
    for url in ("https://example.com/x", ORIGIN + "/deploy", "about:blank"):
        client.told.clear()
        await ask(panel_window.WINDOW_PREFIX + url)
        assert client.told == [{"op": "refused", "id": "a", "why": "window"}], url
    assert panel_window.window_url(panel_window.WINDOW_PREFIX) is None
    assert panel_window.window_url("x" * 9) is None
    assert panel_window.window_url(panel_window.WINDOW_PREFIX + "y" * 4000) is None


async def test_a_main_document_that_lands_on_a_launch_address_ends_the_panel() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    await browser.relay.push(
        panel.target_id,
        "Page.frameNavigated",
        {"frame": {"id": MAIN_FRAME, "url": ORIGIN + "/chat?token=abc"}},
    )
    assert client.last("status")["status"] == "failed" and client.last("status")["why"] == "address"


async def test_a_main_document_that_lands_off_the_list_ends_the_panel() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    await browser.relay.push(
        panel.target_id,
        "Page.frameNavigated",
        {"frame": {"id": MAIN_FRAME, "url": "https://evil.example/"}},
    )
    assert client.last("status") == {
        "op": "status",
        "id": "a",
        "status": "failed",
        "why": "address",
    }
    assert not panel.stream.running


# ── the sign-in ─────────────────────────────────────────────────────────────


async def test_the_sign_in_reaches_the_browser_as_a_session_cookie_and_nowhere_else() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    await open_live(hub, browser, client)
    relay = browser.relay
    (cookies,) = relay.methods("Storage.setCookies")
    assert cookies[1]["browserContextId"] == "ctx1"
    (cookie,) = cookies[1]["cookies"]
    assert cookie["value"] == TOKEN_A and cookie["name"] == "mc_token_6777"
    assert cookie["httpOnly"] is True and cookie["url"] == ORIGIN + "/"
    assert "domain" not in cookie
    # No expiry: a session cookie, never written to a profile.
    assert "expires" not in cookie
    others = [c for c in relay.calls if c[0] != "Storage.setCookies"]
    assert TOKEN_A not in json.dumps(others)
    assert TOKEN_A not in json.dumps(client.told)
    assert TOKEN_A not in repr(client.socket)


async def test_closing_the_socket_drops_its_tabs_its_context_and_its_sign_in() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    digest = client.socket.token
    panel = await open_live(hub, browser, client)
    await hub.close_socket(client.socket.key)
    assert browser.disposed == ["ctx1"] and browser.stops == 1
    assert client.socket.token == ""
    assert not panel.stream.running and hub.panel_count() == 0
    # The tab, and the empty second tab of its window.
    assert [p["targetId"] for _, p, _ in browser.relay.methods("Target.closeTarget")] == [
        "T1",
        "C1",
    ]
    with pytest.raises(ph.PanelRefused) as refused:
        await hub.input(digest, panel.handle, CLICK)
    assert refused.value.code == "unknown_panel"


async def test_closing_a_sockets_last_panel_drops_the_context_and_the_browser() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    await open_live(hub, browser, client, "a")
    await open_live(hub, browser, client, "b")
    await hub.close(client.socket.key, "a")
    assert browser.disposed == [] and browser.stops == 0
    await hub.close(client.socket.key, "b")
    assert browser.disposed == ["ctx1"] and browser.stops == 1
    # The next panel of the same socket signs a fresh context in.
    await open_live(hub, browser, client, "c")
    assert browser.contexts == ["ctx1", "ctx2"]
    assert len(browser.relay.methods("Storage.setCookies")) == 2


# ── scope ───────────────────────────────────────────────────────────────────


async def test_pictures_go_only_to_the_socket_that_opened_the_panel() -> None:
    hub, browser, _clock = make_hub()
    one, two = Client(1, TOKEN_A), Client(2, TOKEN_B)
    panel_one = await open_live(hub, browser, one, "a")
    panel_two = await open_live(hub, browser, two, "a")
    assert browser.contexts == ["ctx1", "ctx2"]
    await _picture(browser, panel_one)
    assert [p["panel"] for p in one.pictures] == ["a"] and two.pictures == []
    await _picture(browser, panel_two, payload=600)
    assert len(one.pictures) == 1 and len(two.pictures) == 1
    assert one.pictures[0]["type"] == "xr_panel_frame"


async def test_a_panels_socket_flight_counts_every_panel_of_that_socket_only() -> None:
    hub, browser, _clock = make_hub()
    one, two = Client(1, TOKEN_A), Client(2, TOKEN_B)
    a = await open_live(hub, browser, one, "a")
    b = await open_live(hub, browser, one, "b")
    other = await open_live(hub, browser, two, "a")
    now = a.stream._clock()
    a.stream.flight.sent(1, now, 1000, measured=True)
    b.stream.flight.sent(1, now, 300, measured=True)
    other.stream.flight.sent(1, now, 50_000, measured=True)
    assert a.stream.socket_flight() == 1300 and b.stream.socket_flight() == 1300
    assert other.stream.socket_flight() == 50_000
    # A picture no ack came for in CARD_ACK_TIMEOUT_S counts nothing on the socket.
    b.stream.flight.sent(2, now - sc.CARD_ACK_TIMEOUT_S, 9000, measured=True)
    assert a.stream.socket_flight() == 1300 and sorted(b.stream.flight.pictures) == [1]


async def test_input_for_a_panel_is_taken_only_from_the_sign_in_that_opened_it() -> None:
    hub, browser, _clock = make_hub()
    one, two = Client(1, TOKEN_A), Client(2, TOKEN_B)
    panel = await open_live(hub, browser, one)
    await open_live(hub, browser, two, "b")
    for question in (None, "state"):
        with pytest.raises(ph.PanelRefused) as refused:
            if question:
                await hub.ask(two.socket.token, panel.handle, question)
            else:
                await hub.input(two.socket.token, panel.handle, CLICK)
        assert refused.value.code == "not_your_panel" and refused.value.status == 403
    assert browser.relay.dispatched() == []
    assert (await hub.input(one.socket.token, panel.handle, CLICK))["sent"] == 2


@pytest.mark.parametrize(
    "cut",
    [
        RelayClosed("relay closed"),
        asyncio.TimeoutError(),
        CdpError("Input.dispatchMouseEvent", {"message": "Invalid parameters"}),
    ],
)
async def test_a_send_that_the_pages_connection_cuts_says_what_reached_the_page(
    cut: Exception, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    hub, browser, _clock = make_hub()
    one = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, one)
    relay, send = browser.relay, browser.relay.send

    async def cut_input(method: str, *args: Any, **kwargs: Any) -> Any:
        if method.startswith("Input."):
            raise cut
        return await send(method, *args, **kwargs)

    monkeypatch.setattr(relay, "send", cut_input)
    with caplog.at_level("WARNING", logger=ph.__name__):
        for event in (CLICK, {"type": "key", "key": "Enter"}, {"type": "text", "text": "hi"}):
            assert await hub.input(one.socket.token, panel.handle, event) == {
                "sent": 0,
                "lost": True,
            }
    # A refused command is said at warning, by its method and CDP's message alone; a cut connection is not.
    said = [
        r.getMessage() for r in caplog.records if r.name == ph.__name__ and r.levelname == "WARNING"
    ]
    if isinstance(cut, CdpError):
        assert said and all(
            m == "xr panel: the page refused Input.dispatchMouseEvent: Invalid parameters"
            for m in said
        )
        assert not any("hi" in m or "500" in m for m in said)
    else:
        assert not any("refused" in m for m in said)
    # A bad event is still refused as one, before anything is sent.
    with pytest.raises(InputEventError):
        await hub.input(one.socket.token, panel.handle, {"type": "x"})


async def test_a_socket_cannot_steer_another_sockets_panel_by_its_id() -> None:
    hub, browser, _clock = make_hub()
    one, two = Client(1, TOKEN_A), Client(2, TOKEN_B)
    panel = await open_live(hub, browser, one, "a")
    await hub.resize(two.socket.key, "a", 1400, 900)
    await hub.close(two.socket.key, "a")
    await hub.set_visible(two.socket.key, "a", False)
    assert panel.stream.running and panel.stream.css_w == 1024 and panel.stream.visible


# ── grant controls ──────────────────────────────────────────────────────────


LOOKED_AT = [
    "/projects",
    "/projects/p1",
    "/projects/p1/tasks/t-2",
    "/projects?applied=t-1",
    "/tasks",
    "/schedule",
    "/schedule?job=nightly",
]


@pytest.mark.parametrize("address", LOOKED_AT)
async def test_a_page_for_looking_at_takes_no_press_key_or_text_and_scrolls(address: str) -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    relay = browser.relay
    # The tab moves on inside the page, as a press on the dashboard's own rail moves it.
    await relay.push(
        panel.target_id,
        "Page.navigatedWithinDocument",
        {"frameId": MAIN_FRAME, "url": ORIGIN + address},
    )
    assert panel.last_url == ORIGIN + address
    token, handle = client.socket.token, panel.handle
    before = len(relay.methods("Input.dispatchMouseEvent"))
    refused = {"sent": 0, "guarded": True}
    for action in ("down", "click", "up"):
        assert await hub.input(token, handle, {**CLICK, "action": action}) == refused
    assert await hub.input(token, handle, {"type": "text", "text": "run it"}) == refused
    for key in ("Enter", "Tab", "Escape"):
        assert await hub.input(token, handle, {"type": "key", "key": key}) == refused
    assert len(relay.methods("Input.dispatchMouseEvent")) == before
    assert relay.methods("Input.dispatchKeyEvent") == [] and relay.methods("Input.insertText") == []
    # The person reads and scrolls: the wheel and a pointer's move pass.
    out = await hub.input(token, handle, {"type": "wheel", "x": 300, "y": 300, "dx": 0, "dy": 120})
    assert out["sent"] == 1
    out = await hub.input(token, handle, {**CLICK, "action": "move"})
    assert out["sent"] == 1
    kinds = [p["type"] for _, p, _ in relay.methods("Input.dispatchMouseEvent")[before:]]
    assert kinds == ["mouseWheel", "mouseMoved"]
    # What is not an event is still refused as such.
    with pytest.raises(ph.InputEventError):
        await hub.input(token, handle, {"type": "key", "key": "F5"})
    with pytest.raises(ph.InputEventError):
        await hub.input(token, handle, {**CLICK, "action": "smash"})


async def test_a_button_that_was_down_when_the_tab_moved_to_such_a_page_is_let_go() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    relay = browser.relay
    token, handle = client.socket.token, panel.handle
    assert (await hub.input(token, handle, {**CLICK, "action": "down"}))["sent"] == 1
    await relay.push(
        panel.target_id,
        "Page.navigatedWithinDocument",
        {"frameId": MAIN_FRAME, "url": ORIGIN + "/schedule"},
    )
    assert (await hub.input(token, handle, {**CLICK, "action": "up"}))["sent"] == 1
    assert not panel.held
    assert await hub.input(token, handle, {**CLICK, "action": "up"}) == {"sent": 0, "guarded": True}


async def test_a_tab_whose_path_the_hub_does_not_know_takes_no_press() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    panel.last_url = ""
    out = await hub.input(client.socket.token, panel.handle, CLICK)
    assert out == {"sent": 0, "guarded": True}
    out = await hub.input(client.socket.token, panel.handle, {"type": "text", "text": "x"})
    assert out == {"sent": 0, "guarded": True}


async def test_a_plain_press_on_a_grant_control_is_not_sent() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    relay = browser.relay
    relay.target_answer = {
        "rect": [480, 290, 90, 24],
        "kind": "button",
        "label": "Allow once",
        "guarded": True,
    }
    for action in ("click", "down"):
        out = await hub.input(client.socket.token, panel.handle, {**CLICK, "action": action})
        assert out["guarded"] is True and out["sent"] == 0
    assert relay.dispatched() == []
    relay.target_answer = {
        "rect": [600, 290, 60, 24],
        "kind": "button",
        "label": "Reject",
        "guarded": False,
    }
    out = await hub.input(client.socket.token, panel.handle, CLICK)
    assert "guarded" not in out and out["sent"] == 2


async def test_a_press_while_the_browsers_own_picker_is_open_is_held_and_the_picker_closed() -> (
    None
):
    # A colour picker, a date picker, or a select's list that is not drawn in the page: the
    # browser draws it by itself, outside the page. A press where a picture shows it would be a
    # press on what lies behind it.
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    relay = browser.relay
    relay.target_answer = {
        "rect": [0, 0, 0, 0],
        "kind": "",
        "label": "",
        "guarded": False,
        "picker": True,
    }
    escape = {"type": "rawKeyDown", "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27}
    for count, action in enumerate(("click", "down"), start=1):
        out = await hub.input(client.socket.token, panel.handle, {**CLICK, "action": action})
        assert out == {"sent": 0, "picker": True}
        # No press reached the page. The picker got one key down of Escape, and no key up:
        # the page would hear that one.
        assert relay.dispatched() == [("Input.dispatchKeyEvent", escape)] * count
    # A move is sent on, and names no target: the picker is not a thing of the page.
    out = await hub.input(client.socket.token, panel.handle, {**CLICK, "action": "move"})
    assert out["target"] is None and out["sent"] == 1
    # With the picker gone the next press is a press.
    relay.target_answer = {
        "rect": [600, 290, 60, 24],
        "kind": "button",
        "label": "OK",
        "guarded": False,
    }
    out = await hub.input(client.socket.token, panel.handle, CLICK)
    assert "picker" not in out and out["sent"] == 2


#: Where the page's focus can be, as its reader answers (``panel_page.GUARD_JS``, ``focus``).
#: The reader walks up from the focused element by the guard's own rule, so each row is one
#: place on the dashboard; ``website/src/test/panelGuardFocus.test.tsx`` renders those places
#: and holds the reader to these answers.
_BUTTON = {"editable": False, "kind": "", "rect": [480, 290, 90, 24]}
_FIELD = {"editable": True, "kind": "text", "rect": [300, 400, 320, 36]}
FOCUS_GUARDED = {
    "Allow once in the approval prompt": {**_BUTTON, "guarded": True},
    "a quick ask's sign-in field": {**_FIELD, "kind": "password", "guarded": True},
    "a quick ask's code field": {**_FIELD, "guarded": True},
    "a change card's secret field": {**_FIELD, "kind": "password", "guarded": True},
    "a field of a guarded Settings tab": {**_FIELD, "guarded": True},
    "a text area of a guarded Settings tab": {**_FIELD, "kind": "textarea", "guarded": True},
    "a widget frame inside a guarded surface": {**_FIELD, "kind": "frame", "guarded": True},
}
FOCUS_PASSED = {
    "nothing (the page's body)": {"editable": False, "kind": "", "rect": None, "guarded": False},
    "Cancel in a change card": {**_BUTTON, "guarded": False},
    "the message box while an approval prompt is shown": {
        **_FIELD,
        "kind": "textarea",
        "guarded": False,
    },
    "a sandboxed widget frame": {**_FIELD, "kind": "frame", "guarded": False},
    "a field of a Settings tab that grants nothing": {**_FIELD, "guarded": False},
}
#: Answers that say nothing about the guard: the page could not be asked, or an older reader.
FOCUS_UNREAD = {
    "no answer": None,
    "the reader is not in the page": False,
    "an answer with no word on the guard": {**_FIELD},
    "a word on the guard that is not a yes or a no": {**_FIELD, "guarded": "no"},
}
_KEYS = ("Enter", "Tab", "Escape", "Backspace", "ArrowDown")


@pytest.mark.parametrize("where", sorted(FOCUS_GUARDED))
async def test_text_and_keys_are_not_sent_while_the_focus_is_in_a_guarded_surface(
    where: str,
) -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    relay = browser.relay
    relay.focus_answer = FOCUS_GUARDED[where]
    # What is under the middle of the focus is never asked: the answer rests on the focus alone.
    relay.target_answer = {"rect": [0, 0, 1, 1], "kind": "button", "label": "", "guarded": False}
    asked = len(relay.methods("Runtime.evaluate"))
    out = await hub.input(client.socket.token, panel.handle, {"type": "text", "text": "hunter2"})
    assert out == {"sent": 0, "guarded": True}
    for key in _KEYS:
        out = await hub.input(client.socket.token, panel.handle, {"type": "key", "key": key})
        assert out == {"sent": 0, "guarded": True}
    assert relay.dispatched() == []
    questions = [p["expression"] for _, p, _ in relay.methods("Runtime.evaluate")[asked:]]
    assert len(questions) == 1 + len(_KEYS) and all(".focus()" in q for q in questions)


@pytest.mark.parametrize("where", sorted(FOCUS_PASSED))
async def test_text_and_keys_are_sent_while_the_focus_is_outside_every_guarded_surface(
    where: str,
) -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    relay = browser.relay
    relay.focus_answer = FOCUS_PASSED[where]
    # A guarded control under the middle of the focus changes nothing: the focus decides.
    relay.target_answer = {"rect": [0, 0, 1, 1], "kind": "button", "label": "", "guarded": True}
    out = await hub.input(client.socket.token, panel.handle, {"type": "text", "text": "hello"})
    assert out == {"sent": 1}
    out = await hub.input(client.socket.token, panel.handle, {"type": "key", "key": "Enter"})
    assert "guarded" not in out and out["sent"] >= 1
    sent = [m for m, _ in relay.dispatched()]
    assert sent[0] == "Input.insertText" and "Input.dispatchKeyEvent" in sent


async def test_a_tab_from_a_passed_control_into_a_guarded_surface_stops_what_follows() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    relay = browser.relay
    relay.focus_answer = FOCUS_PASSED["Cancel in a change card"]
    out = await hub.input(client.socket.token, panel.handle, {"type": "key", "key": "Tab"})
    assert "guarded" not in out and out["sent"] >= 1
    # The Tab moved the page's focus on to the card's secret field.
    relay.focus_answer = FOCUS_GUARDED["a change card's secret field"]
    sent_before = relay.dispatched()
    for event in (
        {"type": "text", "text": "hunter2"},
        {"type": "key", "key": "Enter"},
        {"type": "key", "key": "Tab"},
    ):
        assert await hub.input(client.socket.token, panel.handle, event) == {
            "sent": 0,
            "guarded": True,
        }
    assert relay.dispatched() == sent_before
    # A press on a control that grants nothing moves the focus out again, and keys go on.
    relay.focus_answer = FOCUS_PASSED["Cancel in a change card"]
    out = await hub.input(client.socket.token, panel.handle, {"type": "key", "key": "Enter"})
    assert "guarded" not in out and out["sent"] >= 1


async def test_text_stops_at_the_piece_before_which_a_guarded_surface_took_the_focus() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    relay = browser.relay
    box = FOCUS_PASSED["the message box while an approval prompt is shown"]
    ask_field = FOCUS_GUARDED["a quick ask's sign-in field"]
    # The focus is in the message box for two pieces; then a quick ask opens and takes it.
    relay.focus_answer = [box, box, ask_field]
    text = "".join(str(n % 10) for n in range(64 * 5))
    asked = len(relay.methods("Runtime.evaluate"))
    out = await hub.input(client.socket.token, panel.handle, {"type": "text", "text": text})
    assert out == {"sent": 2, "guarded": True}
    typed = "".join(p["text"] for m, p in relay.dispatched() if m == "Input.insertText")
    assert typed == text[: 64 * 2]
    # One question before each piece that was sent, and one before the piece that was not.
    assert len(relay.methods("Runtime.evaluate")) - asked == 3


async def test_every_piece_of_a_long_text_is_sent_while_the_focus_stays_outside() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    relay = browser.relay
    text = "x" * ph.MAX_TEXT_CHARS
    asked = len(relay.methods("Runtime.evaluate"))
    out = await hub.input(client.socket.token, panel.handle, {"type": "text", "text": text})
    pieces = [p["text"] for m, p in relay.dispatched() if m == "Input.insertText"]
    assert out == {"sent": len(pieces)} and "".join(pieces) == text
    assert len(relay.methods("Runtime.evaluate")) - asked == len(pieces)


@pytest.mark.parametrize("why", sorted(FOCUS_UNREAD))
async def test_text_and_keys_are_not_sent_when_the_page_cannot_say_where_its_focus_is(
    why: str,
) -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    browser.relay.focus_answer = FOCUS_UNREAD[why]
    for event in ({"type": "text", "text": "hello"}, {"type": "key", "key": "Enter"}):
        assert await hub.input(client.socket.token, panel.handle, event) == {
            "sent": 0,
            "unread": True,
        }
    assert browser.relay.dispatched() == []


@pytest.mark.parametrize("how", ["the call fails", "no isolated world", "no reader in the page"])
async def test_nothing_is_pressed_or_typed_blind(how: str) -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    relay = browser.relay
    if how == "the call fails":
        relay.evaluate_fails = True
    elif how == "no isolated world":
        panel.world = None
    else:
        relay.target_answer = False
        relay.focus_answer = False
    for event in (
        CLICK,
        {**CLICK, "action": "down"},
        {"type": "text", "text": "hello"},
        {"type": "key", "key": "Enter"},
    ):
        assert await hub.input(client.socket.token, panel.handle, event) == {
            "sent": 0,
            "unread": True,
        }
    assert [m for m, _ in relay.dispatched() if m != "Input.dispatchMouseEvent"] == []
    assert not any(
        p.get("type") in ("mousePressed", "mouseReleased") for _, p in relay.dispatched()
    )
    # A move presses nothing, so it still goes: the hand's ray stays on the page.
    out = await hub.input(client.socket.token, panel.handle, {**CLICK, "action": "move"})
    assert out["target"] is None and "unread" not in out
    # The route's own question says "nothing", as it does for an empty point.
    assert await hub.ask(client.socket.token, panel.handle, "focus") is None


async def test_the_pages_own_guard_report_reaches_the_panel() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    await browser.relay.push(
        panel.target_id, "Runtime.bindingCalled", {"name": "__kcPanelGuarded", "payload": "click"}
    )
    assert client.last("guarded") == {"op": "guarded", "id": "a"}


async def test_the_guard_is_installed_before_any_page_script() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    await open_live(hub, browser, client)
    calls = [c[0] for c in browser.relay.calls]
    scripts = browser.relay.methods("Page.addScriptToEvaluateOnNewDocument")
    assert (
        scripts[0][1]["worldName"] == "kirocrew_panel"
        and "data-agent-grant" in scripts[0][1]["source"]
    )
    assert calls.index("Page.addScriptToEvaluateOnNewDocument") < calls.index("Page.navigate")
    assert calls.index("Fetch.enable") < calls.index("Page.navigate")


# ── what an input may be ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "event",
    [
        {"type": "key", "key": "a"},
        {"type": "key", "key": "Enter", "modifiers": 2},
        {"type": "key", "key": "F12"},
        {"type": "text", "text": "x" * (ph.MAX_TEXT_CHARS + 1)},
        {"type": "text", "text": ""},
        {"type": "mouse", "action": "click", "x": 2000, "y": 10},
        {"type": "mouse", "action": "click", "x": 10, "y": -1},
        {"type": "mouse", "action": "click", "x": float("nan"), "y": 10},
        {"type": "mouse", "action": "drag", "x": 10, "y": 10},
        {"type": "eval", "expression": "1"},
        "click",
    ],
)
async def test_an_input_outside_the_contract_is_refused_and_nothing_is_sent(event: Any) -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    with pytest.raises(InputEventError):
        await hub.input(client.socket.token, panel.handle, event)
    assert browser.relay.dispatched() == []


async def test_text_is_typed_into_the_page_as_real_input() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    text = "hello " * 20
    await hub.input(client.socket.token, panel.handle, {"type": "text", "text": text})
    typed = "".join(p["text"] for m, p in browser.relay.dispatched() if m == "Input.insertText")
    assert typed == text


async def test_only_the_three_fixed_questions_are_answered() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    assert (await hub.ask(client.socket.token, panel.handle, "state"))["path"] == "/chat"
    with pytest.raises(ph.PanelRefused) as refused:
        await hub.ask(client.socket.token, panel.handle, "document.cookie")
    assert refused.value.code == "unknown_question"
    asked = [p["expression"] for _, p, _ in browser.relay.methods("Runtime.evaluate")]
    assert not any("cookie" in e for e in asked)


# ── the pixel budget's two calls ────────────────────────────────────────────


async def test_a_panel_out_of_view_sends_nothing_and_comes_back() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    assert panel.stream.casting
    await hub.set_visible(client.socket.key, "a", False)
    assert not panel.stream.casting and browser.relay.methods("Page.stopScreencast")
    await hub.set_visible(client.socket.key, "a", True)
    assert panel.stream.casting


def _windows_made(relay: Any) -> list[dict[str, Any]]:
    """The tabs the hub asked for that are a window of their own: a panel's page each."""
    return [p for _, p, _ in relay.methods("Target.createTarget") if p.get("newWindow") is True]


def _since(relay: Any, mark: int, *names: str) -> list[tuple[str, Any]]:
    """The calls named *names* that the hub made after call number *mark*, in order."""
    out: list[tuple[str, Any]] = []
    for method, params, _ in relay.calls[mark:]:
        if method in names:
            out.append((method, params.get("targetId", params.get("enabled"))))
    return out


async def test_a_panels_window_gets_an_empty_second_tab_in_the_sockets_own_context() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    made = [p for _, p, _ in browser.relay.methods("Target.createTarget")]
    assert made == [
        {"url": "about:blank", "browserContextId": "ctx1", "newWindow": True},
        {"url": "about:blank", "browserContextId": "ctx1", "newWindow": False, "background": True},
    ]
    assert panel.target_id == "T1" and panel.curtain_id == "C1" and not panel.curtained
    # It is never attached to, never navigated and never given a script.
    assert browser.relay.attached == ["T1"]


async def test_a_panel_out_of_view_goes_behind_its_curtain_and_is_brought_back() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    relay = browser.relay
    names = (
        "Page.stopScreencast",
        "Emulation.setFocusEmulationEnabled",
        "Target.activateTarget",
        "Emulation.setDeviceMetricsOverride",
        "Page.startScreencast",
    )
    mark = len(relay.calls)
    await hub.set_visible(client.socket.key, "a", False)
    # The pictures stop first; then the page is told it has no focus and goes behind the curtain.
    assert _since(relay, mark, *names) == [
        ("Page.stopScreencast", None),
        ("Emulation.setFocusEmulationEnabled", False),
        ("Target.activateTarget", "C1"),
    ]
    assert panel.curtained and not panel.stream.casting
    # Told twice, it is done once.
    mark = len(relay.calls)
    await hub.set_visible(client.socket.key, "a", False)
    assert _since(relay, mark, *names) == []
    # In view again: the page comes in front and has the focus, and the tab is given the
    # page's size again (a tab that comes in front gets its window's own), before its
    # pictures start.
    mark = len(relay.calls)
    await hub.set_visible(client.socket.key, "a", True)
    assert _since(relay, mark, *names) == [
        ("Emulation.setFocusEmulationEnabled", True),
        ("Target.activateTarget", "T1"),
        ("Emulation.setDeviceMetricsOverride", None),
        ("Page.startScreencast", None),
    ]
    laid = relay.methods("Emulation.setDeviceMetricsOverride")[-1][1]
    assert (laid["width"], laid["height"]) == (panel.stream.css_w, panel.stream.css_h)
    assert not panel.curtained and panel.stream.casting


async def test_a_panels_window_has_the_pages_size_at_the_open_and_at_each_new_size() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    relay = browser.relay
    # As Chromium's own window: 1280 by 800, with a view of 1280 by 657 in it.
    relay.window_bounds = {"left": 20, "top": 20, "width": 1280, "height": 800}
    relay.view = {"clientWidth": 1280, "clientHeight": 657}
    names = (
        "Page.getLayoutMetrics",
        "Browser.setWindowBounds",
        "Emulation.setDeviceMetricsOverride",
    )
    await open_live(hub, browser, client)
    # The frame is read before the tab's first layout, and the window is sized before it.
    assert [name for name, _ in _since(relay, 0, *names)][:3] == list(names)
    window = relay.windows["T1"]
    assert relay.methods("Browser.setWindowBounds")[-1][1] == {
        "windowId": window,
        "bounds": {"width": 1024, "height": 720 + 143},
    }
    mark = len(relay.calls)
    await hub.resize(client.socket.key, "a", 1400, 900)
    assert [name for name, _ in _since(relay, mark, *names)] == list(names[1:])
    assert relay.methods("Browser.setWindowBounds")[-1][1] == {
        "windowId": window,
        "bounds": {"width": 1400, "height": 900 + 143},
    }


async def test_a_page_event_that_shows_a_panel_and_the_sockets_hide_do_not_cross() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    await hub.open(client.socket, "a", path="/chat", width=1024, height=720)
    panel = hub._by_handle[client.last("opened")["handle"]]
    relay = browser.relay
    answer = relay.send
    reached, go = asyncio.Event(), asyncio.Event()

    async def hold_the_start(
        method: str, params: Any = None, session_id: Any = None, **more: Any
    ) -> Any:
        if method == "Page.startScreencast" and not reached.is_set():
            reached.set()
            await go.wait()
        return await answer(method, params, session_id, **more)

    relay.send = hold_the_start  # type: ignore[method-assign]
    # The page loads: its own event shows the panel, and the browser is asked for the cast...
    shown = asyncio.ensure_future(
        relay.push(
            panel.target_id,
            "Page.frameNavigated",
            {"frame": {"id": MAIN_FRAME, "url": ORIGIN + "/chat"}},
        )
    )
    await asyncio.wait_for(reached.wait(), 5)
    # ...when the socket says that the panel is out of view.
    hidden = asyncio.ensure_future(hub.set_visible(client.socket.key, "a", False))
    go.set()
    await asyncio.wait_for(asyncio.gather(shown, hidden), 5)
    # The hide came last and holds: no cast runs for a page that nobody sees.
    assert panel.curtained and not panel.stream.casting and not panel.stream.visible
    # And the next look at the panel starts its pictures.
    await hub.set_visible(client.socket.key, "a", True)
    assert not panel.curtained and panel.stream.casting and panel.stream.visible


async def test_a_hide_does_not_cross_a_page_events_show_that_draws_the_curtain_back() -> None:
    """The crossing that only the panel's own lock in the hub keeps apart.

    Held at the cast's start, the stream's lock already keeps a hide back. Here
    the browser is held one step earlier, at the curtain.
    """
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    relay = browser.relay
    key = client.socket.key
    await hub.set_visible(key, "a", False)
    assert panel.curtained and not panel.stream.casting
    answer = relay.send
    gates = [(asyncio.Event(), asyncio.Event()), (asyncio.Event(), asyncio.Event())]
    held: list[bool] = []

    async def hold_the_curtain(
        method: str, params: Any = None, session_id: Any = None, **more: Any
    ) -> Any:
        back = method == "Emulation.setFocusEmulationEnabled" and (params or {}).get("enabled")
        if back and len(held) < 2:
            reached, go = gates[len(held)]
            held.append(True)
            reached.set()
            await go.wait()
        return await answer(method, params, session_id, **more)

    relay.send = hold_the_curtain  # type: ignore[method-assign]
    # The socket says: in view. Its show begins to draw the curtain back...
    show = asyncio.ensure_future(hub.set_visible(key, "a", True))
    await asyncio.wait_for(gates[0][0].wait(), 5)
    # ...and the page reloads itself: its event shows the panel too, from its own task.
    event = asyncio.ensure_future(
        relay.push(
            panel.target_id,
            "Page.frameNavigated",
            {"frame": {"id": MAIN_FRAME, "url": ORIGIN + "/chat"}},
        )
    )
    for _ in range(8):
        await asyncio.sleep(0)
    gates[0][1].set()
    await asyncio.wait_for(show, 5)
    # The socket says: out of view. In the socket's chain this comes after its own show.
    hide = asyncio.ensure_future(hub.set_visible(key, "a", False))
    for _ in range(8):
        await asyncio.sleep(0)
    # The page event's show goes on now, if it was let in at all.
    gates[1][1].set()
    await asyncio.wait_for(asyncio.gather(event, hide), 5)
    assert panel.shown is False
    assert panel.curtained and not panel.stream.casting and not panel.stream.visible


async def test_a_page_event_starts_no_pictures_for_a_panel_out_of_view() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    relay = browser.relay
    await hub.set_visible(client.socket.key, "a", False)
    mark = len(relay.calls)
    # The page behind its curtain reloads itself, and loads.
    await relay.push(
        panel.target_id,
        "Page.frameNavigated",
        {"frame": {"id": MAIN_FRAME, "url": ORIGIN + "/chat"}},
    )
    await relay.push(panel.target_id, "Page.loadEventFired", {})
    asked = [m for m, _, _ in relay.calls[mark:]]
    assert "Page.startScreencast" not in asked and "Target.activateTarget" not in asked
    assert panel.curtained and not panel.stream.casting and not panel.stream.visible


async def test_no_cast_is_started_behind_a_curtain_that_did_not_come_back() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    relay = browser.relay
    await hub.set_visible(client.socket.key, "a", False)
    answer = relay.send
    lost: list[bool] = []

    async def lose_one_activate(
        method: str, params: Any = None, session_id: Any = None, **more: Any
    ) -> Any:
        if method == "Target.activateTarget" and not lost:
            lost.append(True)
            raise ph.CdpError(method, {"message": "the browser did not do it"})
        return await answer(method, params, session_id, **more)

    relay.send = lose_one_activate  # type: ignore[method-assign]
    await hub.set_visible(client.socket.key, "a", True)
    # The page is still behind its curtain, where a cast gets no frame: none was started.
    assert lost and panel.shown and panel.curtained
    assert not panel.stream.casting and not panel.stream.visible
    # The hub's own look, once a second, asks again.
    await hub._tick(panel)
    assert not panel.curtained and panel.stream.casting and panel.stream.visible


async def test_a_start_whose_answer_was_lost_is_put_right_at_the_next_look() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    relay = browser.relay
    key = client.socket.key
    sid = panel.stream.session_id
    await hub.set_visible(key, "a", False)
    assert relay.casts == set()
    # The browser starts the cast, and its answer never arrives.
    relay.lose_starts = 1
    await hub.set_visible(key, "a", True)
    assert relay.casts == {sid} and panel.shown and not panel.curtained
    assert not panel.stream.casting and not panel.stream.visible
    # Neither the page nor the socket says more. The hub's look once a second does:
    # the browser refuses a second cast, so the one that runs is stopped and asked for anew.
    await hub._tick(panel)
    assert panel.stream.casting and panel.stream.visible and relay.casts == {sid}
    before = len(client.pictures)
    await _picture(browser, panel)
    assert len(client.pictures) == before + 1


async def test_a_stop_that_fails_still_puts_the_page_behind_its_curtain() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    relay = browser.relay
    key = client.socket.key
    sid = panel.stream.session_id
    relay.lose_stops = 1
    with pytest.raises(asyncio.TimeoutError):
        await hub.set_visible(key, "a", False)
    # The cast still runs in the browser, and the stream's book says so. The page
    # is behind its curtain all the same, told that it has no focus, and no
    # picture of it goes anywhere.
    assert panel.shown is False and panel.curtained and not panel.view.locked()
    assert panel.stream.casting and not panel.stream.visible and relay.casts == {sid}
    before = len(client.pictures)
    await _picture(browser, panel)
    assert len(client.pictures) == before
    # Back in view: the curtain is drawn back and the cast is started anew.
    await hub.set_visible(key, "a", True)
    assert not panel.curtained and panel.stream.casting and panel.stream.visible
    await _picture(browser, panel, payload=640)
    assert len(client.pictures) == before + 1


async def test_a_page_behind_its_curtain_still_answers_and_still_takes_input() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    await hub.set_visible(client.socket.key, "a", False)
    assert (await hub.ask(client.socket.token, panel.handle, "state"))["path"] == "/chat"
    out = await hub.input(client.socket.token, panel.handle, {"type": "text", "text": "hello"})
    assert out == {"sent": 1}


@pytest.mark.parametrize("how", ["the browser makes no second tab", "it lands in another window"])
async def test_a_panel_goes_without_a_curtain_when_the_browser_gives_none(how: str) -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    await browser.ensure()
    relay = browser.relay
    if how == "the browser makes no second tab":
        relay.curtains = False
    else:
        relay.curtain_strays = True
    panel = await open_live(hub, browser, client)
    assert panel.curtain_id == "" and client.last("opened")["id"] == "a"
    closed = [p["targetId"] for _, p, _ in relay.methods("Target.closeTarget")]
    # A tab that landed elsewhere is closed again: it would hide nothing, and nothing would close it later.
    assert closed == ([] if how == "the browser makes no second tab" else ["C1"])
    # Out of view its pictures still stop; nothing is brought in front of it.
    await hub.set_visible(client.socket.key, "a", False)
    assert not panel.stream.casting and not panel.curtained
    assert relay.methods("Target.activateTarget") == []
    await hub.set_visible(client.socket.key, "a", True)
    assert panel.stream.casting


async def test_closing_a_panel_closes_its_curtain_too() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    await open_live(hub, browser, client)
    await hub.open(client.socket, "b", path="/settings", width=1024, height=720)
    await hub.close(client.socket.key, "a")
    closed = [p["targetId"] for _, p, _ in browser.relay.methods("Target.closeTarget")]
    assert closed == ["T1", "C1"] and hub.panel_count() == 1


async def test_the_newest_size_and_detail_reach_the_tab() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    await hub.resize(client.socket.key, "a", 1400, 900)
    assert client.last("resized") == {"op": "resized", "id": "a", "css": [1400, 900]}
    metrics = browser.relay.methods("Emulation.setDeviceMetricsOverride")[-1][1]
    assert (metrics["width"], metrics["height"]) == (1400, 900)
    await hub.set_detail(client.socket.key, "a", 0.9)
    cast = browser.relay.methods("Page.startScreencast")[-1][1]
    assert (cast["maxWidth"], cast["maxHeight"]) == (1400, 900)
    await hub.resize(client.socket.key, "a", 99999, 900)
    assert panel.stream.css_w == 1400


async def test_a_socket_holds_at_most_its_share_of_panels() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    for n in range(ph.MAX_PANELS_PER_SOCKET):
        await hub.open(client.socket, f"p{n}", path="/chat", width=1024, height=720)
    await hub.open(client.socket, "extra", path="/chat", width=1024, height=720)
    assert client.last("status") == {
        "op": "status",
        "id": "extra",
        "status": "failed",
        "why": "too_many",
    }
    assert hub.panel_count() == ph.MAX_PANELS_PER_SOCKET


async def test_one_person_can_have_twelve_pages_and_the_thirteenth_is_refused() -> None:
    assert ph.MAX_PANELS_PER_SOCKET == 12 and ph.MAX_PANELS == 12
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    for n in range(12):
        await hub.open(client.socket, f"p{n}", path="/chat", width=1024, height=720)
    assert [m["id"] for m in client.told if m.get("op") == "opened"] == [f"p{n}" for n in range(12)]
    await hub.open(client.socket, "p12", path="/chat", width=1024, height=720)
    assert client.last("status") == {
        "op": "status",
        "id": "p12",
        "status": "failed",
        "why": "too_many",
    }
    assert len(_windows_made(browser.relay)) == 12
    # One closed makes room for one more.
    await hub.close(client.socket.key, "p3")
    await hub.open(client.socket, "p12", path="/chat", width=1024, height=720)
    assert client.last("opened")["id"] == "p12" and hub.panel_count() == 12


async def test_opens_that_come_in_one_burst_cannot_go_past_the_limit() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    await asyncio.gather(
        *(
            hub.open(client.socket, f"p{n}", path="/chat", width=1024, height=720)
            for n in range(ph.MAX_PANELS_PER_SOCKET + 4)
        )
    )
    opened = [m["id"] for m in client.told if m.get("op") == "opened"]
    refused = [m["id"] for m in client.told if m.get("why") == "too_many"]
    assert len(opened) == ph.MAX_PANELS_PER_SOCKET and len(refused) == 4
    assert set(opened).isdisjoint(refused)
    assert hub.panel_count() == ph.MAX_PANELS_PER_SOCKET
    assert len(_windows_made(browser.relay)) == ph.MAX_PANELS_PER_SOCKET


async def test_two_people_share_the_gateways_twelve_pages() -> None:
    hub, _browser, _clock = make_hub()
    one, two = Client(1, TOKEN_A), Client(2, TOKEN_B)
    for n in range(8):
        await hub.open(one.socket, f"a{n}", path="/chat", width=1024, height=720)
    for n in range(4):
        await hub.open(two.socket, f"b{n}", path="/chat", width=1024, height=720)
    assert hub.panel_count() == ph.MAX_PANELS
    await hub.open(two.socket, "b4", path="/chat", width=1024, height=720)
    assert two.last("status")["why"] == "too_many"
    await hub.open(one.socket, "a8", path="/chat", width=1024, height=720)
    assert one.last("status")["why"] == "too_many"
    assert hub.panel_count() == ph.MAX_PANELS


def _page(target_id: str, context_id: str = "ctx1") -> dict[str, Any]:
    return {"targetId": target_id, "type": "page", "browserContextId": context_id}


async def test_a_window_that_a_page_opens_itself_is_closed() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    # The browser lists the panel's tab, the empty second tab of its window, a window that the
    # page opened, and a tab of another socket's context.
    browser.relay.listed = [
        _page("T1"),
        _page("C1"),
        _page("POPUP"),
        _page("OTHER", "ctx-of-another-socket"),
    ]
    mark = len(browser.relay.calls)
    await hub._tick(panel)
    closed = [p["targetId"] for _, p, _ in browser.relay.methods("Target.closeTarget")]
    assert closed == ["POPUP"]
    # The window stood in front of a page of this socket: the page is laid out again after it.
    after = [m for m, _, _ in browser.relay.calls[mark:]]
    assert after.index("Target.closeTarget") < after.index("Emulation.setDeviceMetricsOverride")
    # A look that closes nothing lays nothing out.
    browser.relay.listed = [_page("T1"), _page("C1")]
    mark = len(browser.relay.calls)
    await hub._tick(panel)
    assert "Emulation.setDeviceMetricsOverride" not in [m for m, _, _ in browser.relay.calls[mark:]]


async def test_a_tab_that_is_still_being_opened_is_not_closed_as_a_stray_window() -> None:
    # The browser lists a new tab before the hub has it on its list. The look that
    # runs once a second must not take that tab for a window a page opened.
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    first = await open_live(hub, browser, client)
    relay = browser.relay
    relay.attach_gate, relay.attach_reached = asyncio.Event(), asyncio.Event()
    opening = asyncio.ensure_future(
        hub.open(client.socket, "b", path="/settings", width=1024, height=720)
    )
    await asyncio.wait_for(relay.attach_reached.wait(), 5)
    relay.listed = [_page("T1"), _page("T2")]
    await hub._tick(first)
    assert relay.methods("Target.closeTarget") == []
    relay.attach_gate.set()
    await asyncio.wait_for(opening, 5)
    assert client.last("opened")["id"] == "b" and hub.panel_count() == 2
    # Once it is on the list, the look runs again and still closes nothing.
    await hub._tick(first)
    assert relay.methods("Target.closeTarget") == []


# ── an answer of the browser that does not arrive ─────────────────────────────


def _in_front(relay: Any, rule: Any) -> None:
    """Put *rule* in front of the fake browser. It may raise, or return ``None`` to let the fake answer."""
    answer = relay.send

    async def send(method: str, params: Any = None, session_id: Any = None, **more: Any) -> Any:
        got = await rule(method, params, session_id, answer)
        if got is not None:
            return got
        return await answer(method, params, session_id, **more)

    relay.send = send


def _done_and_lost(relay: Any, method_name: str) -> list[bool]:
    """The browser does the next *method_name*, and its answer never arrives.

    The fake's own ``lose_stops`` loses a stop by not doing it: the browser's
    cast then still runs and the stream's book is right. This is the other
    state, the one a real browser can leave: done, and not answered.
    """
    lost: list[bool] = []

    async def rule(method: str, params: Any, session_id: Any, answer: Any) -> Any:
        if method == method_name and not lost:
            lost.append(True)
            await answer(method, params, session_id)
            raise asyncio.TimeoutError
        return None

    _in_front(relay, rule)
    return lost


async def test_a_restart_whose_stop_was_done_and_not_answered_is_put_right_at_the_next_look() -> (
    None
):
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    relay = browser.relay
    sid = panel.stream.session_id
    assert relay.casts == {sid}
    lost = _done_and_lost(relay, "Page.stopScreencast")
    # A resize restarts the cast: a stop, and then a start that never comes.
    with pytest.raises(asyncio.TimeoutError):
        await hub.resize(client.socket.key, "a", 800, 600)
    assert lost and relay.casts == set()
    # In view, and the book says "no cast": that is what the browser runs.
    assert panel.shown and not panel.stream.casting
    await hub._tick(panel)
    assert relay.casts == {sid} and panel.stream.casting and panel.stream.visible
    assert not panel.curtained


async def test_a_show_asks_for_its_cast_even_when_the_book_says_that_one_runs() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    relay = browser.relay
    key = client.socket.key
    sid = panel.stream.session_id
    _done_and_lost(relay, "Page.stopScreencast")
    with pytest.raises(asyncio.TimeoutError):
        await hub.set_visible(key, "a", False)
    # Out of view. The browser runs no cast, and the book says that one runs.
    assert not panel.stream.visible and panel.stream.casting
    assert relay.casts == set() and panel.curtained
    await hub.set_visible(key, "a", True)
    assert relay.casts == {sid} and panel.stream.casting and panel.stream.visible
    assert not panel.curtained


async def test_the_look_holds_the_panels_lock_while_the_browser_is_slow_and_a_second_look_passes() -> (
    None
):
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    relay = browser.relay
    key = client.socket.key
    await hub.set_visible(key, "a", False)
    relay.lose_starts = 1
    await hub.set_visible(key, "a", True)
    assert panel.shown and not panel.stream.casting
    reached, go = asyncio.Event(), asyncio.Event()

    async def rule(method: str, _params: Any, _session_id: Any, _answer: Any) -> Any:
        if method == "Page.startScreencast" and not reached.is_set():
            reached.set()
            await go.wait()
        return None

    _in_front(relay, rule)
    look = asyncio.ensure_future(hub._tick(panel))
    await asyncio.wait_for(reached.wait(), 5)
    assert panel.view.locked()
    # A second look does not wait behind the first.
    await asyncio.wait_for(hub._tick(panel), 5)
    # The socket's hide does: it is said at once, and done when the look has ended.
    hide = asyncio.ensure_future(hub.set_visible(key, "a", False))
    for _ in range(8):
        await asyncio.sleep(0)
    assert panel.shown is False and not hide.done()
    go.set()
    await asyncio.wait_for(asyncio.gather(look, hide), 5)
    assert panel.curtained and not panel.stream.casting and not panel.stream.visible
    assert relay.casts == set()


# ---------------------------------------------------------------------------
# The Bots page of one Bot: the one query a panel may be opened with.
# ---------------------------------------------------------------------------

BOTS = ("Atlas", "Juno", "R&D / 100% Ops", "mate")


@pytest.mark.parametrize(
    ("asked", "opened"),
    [
        ("/members?member=Atlas", "/members?member=Atlas"),
        ("/members?member=mate", "/members?member=mate"),
        # A space, a slash, an ampersand and a per cent sign: each goes in encoded, however it came.
        (
            "/members?member=R%26D%20%2F%20100%25%20Ops",
            "/members?member=R%26D%20%2F%20100%25%20Ops",
        ),
        ("/members?member=R%26D+%2F+100%25+Ops", "/members?member=R%26D%20%2F%20100%25%20Ops"),
        ("/members?member=R%26D%20/%20100%25%20Ops", "/members?member=R%26D%20%2F%20100%25%20Ops"),
    ],
)
async def test_a_panel_opens_the_page_of_one_of_the_owners_bots(asked: str, opened: str) -> None:
    hub, browser, _clock = make_hub(members=BOTS)
    client = Client(1, TOKEN_A)
    await hub.open(client.socket, "a", path=asked, width=1024, height=720)
    assert client.last("opened")["id"] == "a"
    went = [params["url"] for _method, params, _session in browser.relay.methods("Page.navigate")]
    assert went[-1] == ORIGIN + opened


@pytest.mark.parametrize(
    "asked",
    [
        # Another key, with the Bot's name or without it: the Bots page acts on these as it opens.
        "/members?member=Atlas&create=1",
        "/members?create=1",
        "/members?create=1&name=x&goal=y",
        "/members?member=Atlas&dashboard=preview",
        "/members?dashboard=preview",
        "/members?member=Atlas&member=Juno",
        "/members?Member=Atlas",
        "/members?member=",
        "/members?member",
        "/members?",
        "/members?member=Atlas#x",
        "/members?member=Atlas%26create%3D1",
        # A name that is none of the owner's Bots, and one that cannot be read.
        "/members?member=Nobody",
        "/members?member=%FF%FE",
        "/members?member=Atlas%0Acreate",
        # A query on any other page of the list.
        "/chat?member=Atlas",
        "/notifications?member=Atlas",
        "/members/x?member=Atlas",
        "/members?member=" + "a" * 2100,
    ],
)
async def test_any_other_query_is_refused_before_any_browser(asked: str) -> None:
    hub, browser, _clock = make_hub(members=BOTS)
    client = Client(1, TOKEN_A)
    await hub.open(client.socket, "a", path=asked, width=1024, height=720)
    assert client.last("status")["why"] == "address"
    assert browser.ensures == 0


@pytest.mark.parametrize(
    "asked",
    [
        "/members?member=Atlas",
        "/members?member=mate",
        "/members?member=Any%20Name",
        "/members?member=a&create=1",
    ],
)
async def test_with_the_bots_unreadable_no_bots_page_is_opened_for_any_name(asked: str) -> None:
    hub, browser, _clock = make_hub(members=None)
    client = Client(1, TOKEN_A)
    await hub.open(client.socket, "a", path=asked, width=1024, height=720)
    assert client.last("status") == {
        "op": "status",
        "id": "a",
        "status": "failed",
        "why": "address",
    }
    assert browser.ensures == 0


async def test_with_the_bots_unreadable_the_pages_of_the_list_open_as_before() -> None:
    hub, _browser, _clock = make_hub(members=None)
    client = Client(1, TOKEN_A)
    await hub.open(client.socket, "a", path="/members", width=1024, height=720)
    assert client.last("opened")["id"] == "a"
    assert panel_page.allowed_open("/chat", None) == "/chat"
    assert panel_page.allowed_open("/members?member=Atlas", None) is None


async def test_the_panel_is_told_which_bot_its_bots_page_names_and_nothing_else_of_the_query() -> (
    None
):
    hub, browser, _clock = make_hub(members=BOTS)
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client, path="/members?member=Atlas")
    assert client.last("path") == {"op": "path", "id": "a", "path": "/members?member=Atlas"}

    async def moves(url: str) -> None:
        await browser.relay.push(
            panel.target_id,
            "Page.navigatedWithinDocument",
            {"frameId": MAIN_FRAME, "url": ORIGIN + url},
        )

    # The person picks another Bot in the page: the panel is told that one, with the page's other keys left out.
    await moves("/members?member=R%26D+%2F+100%25+Ops&tab=goals")
    assert client.last("path")["path"] == "/members?member=R%26D%20%2F%20100%25%20Ops"
    # A Bots page that names no Bot, or two, is the bare path.
    await moves("/members?tab=goals")
    assert client.last("path")["path"] == "/members"
    await moves("/members?member=Atlas&member=Juno")
    assert client.last("path")["path"] == "/members"
    # The pages of the list are told as before: a path, and no query.
    await moves("/chat/dashboard-ab12?x=1")
    assert client.last("path")["path"] == "/chat/dashboard-ab12"


def test_the_address_a_panel_opens_is_built_from_the_name_and_never_copied() -> None:
    known = frozenset(BOTS)
    assert panel_page.allowed_open("/chat", known) == "/chat"
    assert panel_page.allowed_open("/members", known) == "/members"
    assert panel_page.allowed_open("/members?member=Atlas", known) == "/members?member=Atlas"
    # However the name was encoded, one spelling comes back.
    for asked in ("/members?member=%41tlas", "/members?member=Atl%61s"):
        assert panel_page.allowed_open(asked, known) == "/members?member=Atlas"
    assert panel_page.allowed_open("/members?member=Atlas;create=1", known) is None
    assert panel_page.allowed_open("/members?member=Atlas&create=1", known) is None
    assert panel_page.allowed_open("/members?create=1", known) is None
    assert panel_page.allowed_open("/chat?x=1", known) is None
    assert panel_page.allowed_open("/api/config?member=Atlas", known) is None
    assert panel_page.allowed_open(None, known) is None
    # What is told of a tab: its path, and the one Bot a Bots page names.
    assert panel_page.address_of(ORIGIN + "/members?member=Atlas&x=1") == "/members?member=Atlas"
    assert panel_page.address_of(ORIGIN + "/members?x=1") == "/members"
    assert panel_page.address_of(ORIGIN + "/chat?member=Atlas") == "/chat"
    assert panel_page.address_of(7) == ""


def test_a_press_is_stale_only_when_the_control_at_its_place_is_another_or_moved() -> None:
    button = {"rect": [100, 200, 80, 30], "kind": "button", "label": "Allow once", "guarded": False}
    # The XR page did not see what stood there (a finger with no move before it): the press goes as before.
    assert ph.PanelHub._stale({"type": "mouse", "action": "click"}, button) is False
    # The same control, within 4 px: the press goes, whatever else changed on the page.
    assert ph.PanelHub._stale({"aimed": [100, 202, 80, 30]}, button) is False
    # It moved, or another one stands there now, or one came where he saw none, or it went.
    assert ph.PanelHub._stale({"aimed": [100, 240, 80, 30]}, button) is True
    assert ph.PanelHub._stale({"aimed": None}, button) is True
    assert ph.PanelHub._stale({"aimed": [100, 200, 80, 30]}, None) is True
    assert ph.PanelHub._stale({"aimed": None}, None) is False
    with pytest.raises(InputEventError):
        ph.PanelHub._stale({"aimed": [1, 2, 3]}, button)
    with pytest.raises(InputEventError):
        ph.PanelHub._stale({"aimed": [1, 2, 3, float("nan")]}, button)


def test_a_press_is_stale_when_a_control_of_the_same_size_with_another_label_stands_there() -> None:
    allow = {"rect": [100, 200, 80, 30], "kind": "button", "label": "Allow once", "guarded": False}
    # Two buttons of one size swapped places between the picture and the press: one box, another label.
    assert ph.PanelHub._stale({"aimed": [100, 200, 80, 30, "Reject"]}, allow) is True
    assert ph.PanelHub._stale({"aimed": [100, 200, 80, 30, "Allow once"]}, allow) is False
    # A control with no label, on either side, is judged by its box, as before.
    assert ph.PanelHub._stale({"aimed": [100, 200, 80, 30]}, allow) is False
    assert ph.PanelHub._stale({"aimed": [100, 200, 80, 30, ""]}, allow) is False
    assert (
        ph.PanelHub._stale({"aimed": [100, 200, 80, 30, "Reject"]}, {**allow, "label": ""}) is False
    )
    # The control's label now is compared as far as the press carries one.
    long = "x" * 120
    assert (
        ph.PanelHub._stale({"aimed": [100, 200, 80, 30, long[:80]]}, {**allow, "label": long})
        is False
    )
    with pytest.raises(InputEventError):
        ph.PanelHub._stale({"aimed": [100, 200, 80, 30, 7]}, allow)
    with pytest.raises(InputEventError):
        ph.PanelHub._stale({"aimed": [100, 200, 80, 30, "y" * 81]}, allow)
