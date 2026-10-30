"""The sign-in walk: credentials with no targets, typed into Kiro's sheet, filled by the gateway.

A multi-screen sign-in (Google: the email on one screen, the password on the
next) cannot be bound to fields up front. The person types both into the sheet;
the gateway finds each field on the BOUND origin as its screen shows it, writes
it through the same focus-proving insert, presses Enter (or the screen's Next
once), and stops the moment the tab leaves that site. Nothing typed reaches a
field on another origin, a hidden or covered one, the agent, a log, the SEL or
the replay.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Any

import pytest
from test_browser_quick_ask import sel_rows  # noqa: F401 - a fixture
from test_browser_quick_ask import (  # noqa: F401 - the autouse fixture
    CANARY,
    KEY,
    FakePage,
    _answer,
    _home,
    _live,
    _poll,
)
from test_browser_quick_ask_frames import CHILD, _registry

from kiro_crew.browser_harness import live_hub as lh
from kiro_crew.browser_harness import quick_ask as qa
from kiro_crew.dashboard.handlers import browser_ask
from kiro_crew.mcp_tools import browser as tool

pytestmark = pytest.mark.asyncio

EMAIL = "person@example.org"
SIGN_IN = {"kind": "credentials", "reason": "Sign in to read the inbox"}


@pytest.fixture(autouse=True)
def _fast(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr(qa, "STEP_POLL_S", 0.0)
    _floor_monkeypatch.setattr(qa, "STEP_WAIT_S", 0.3)


class LoginPage(FakePage):
    """:class:`FakePage` plus the walk's finder: what each frame shows, per role."""

    def __init__(self, url: str = "https://accounts.example.com/signin") -> None:
        super().__init__()
        self.frame.update(url=url)
        #: frame id -> role -> backendNodeId the finder names there.
        self.login: dict[str, dict[str, int]] = {}
        self.on_enter: Callable[[int | None], None] | None = None
        self.on_click: Callable[[], None] | None = None
        self.enters = 0
        self.on_find: Callable[[str], None] | None = None
        #: One stale top-frame read (the old document), then the truth again.
        self.stale_tree: dict[str, Any] | None = None

    def show(self, frame: str = "F1", **roles: int) -> None:
        self.login[frame] = roles

    async def send(
        self, method: str, params: dict[str, Any] | None = None, session_id: str | None = None
    ) -> dict[str, Any]:
        p = dict(params or {})
        if (
            method == "Page.getFrameTree"
            and self.stale_tree
            and not session_id_is_child(self, session_id)
        ):
            # The race: the tree still names the old document after another committed.
            tree, self.stale_tree = self.stale_tree, None
            return {"frameTree": {"frame": tree, "childFrames": []}}
        if method == "Runtime.evaluate" and str(p.get("expression", "")).startswith(qa._FIND_JS):
            self.calls.append((method, {**p, "_sid": session_id}))
            role = re.search(r"\('(\w+)'\)$", p["expression"]).group(1)  # type: ignore[union-attr]
            node = self.login.get(self._ctx[p["contextId"]], {}).get(role)
            if node and self.on_find:
                self.on_find(role)
            if node and self.nodes[node].get("connected", True):
                return {"result": {"objectId": f"m:{node}"}}
            return {"result": {"type": "object", "subtype": "null"}}
        if method == "Runtime.callFunctionOn" and p["functionDeclaration"] == qa._FOCUSED_JS:
            node = int(p["objectId"].split(":")[1])
            return {"result": {"value": self.focused == node}}
        if method == "Input.dispatchKeyEvent":
            self.calls.append((method, {**p, "_sid": session_id}))
            if p["type"] == "keyDown" and p["key"] == "Enter":
                self.enters += 1
                if self.on_enter:
                    self.on_enter(self.focused)
            return {}
        out = await super().send(method, params, session_id)
        if method == "Input.dispatchMouseEvent" and p["type"] == "mouseReleased" and self.on_click:
            self.on_click()
        return out


def session_id_is_child(page: LoginPage, sid: str | None) -> bool:
    return bool(sid) and any(f["session"] == sid for f in page.frames.values())


def _two_step(page: LoginPage, frame: str = "F1") -> tuple[int, int]:
    """An email screen whose Enter swaps in a password screen, in *frame*."""
    user = page.add("u1", frame=frame, type="email", rect=[20, 30, 300, 30])
    nxt = page.add("b1", frame=frame, tag="button", type="", text="Next", rect=[20, 80, 90, 30])
    page.show(frame, username=user, action=nxt)
    password = page.add("p1", frame=frame, type="password", rect=[20, 30, 300, 30])

    def advance(focused: int | None) -> None:
        if focused == user:
            page.nodes[user]["connected"] = False
            page.show(frame, password=password)

    page.on_enter = advance
    return user, password


async def _ask(page: LoginPage, oopif: bool = False) -> tuple[lh.LiveHub, Any, Any]:
    hub, rec = await _live(page)
    if oopif:
        _registry(hub, page)
    out = await browser_ask.run_ask(hub, KEY, dict(SIGN_IN))
    assert out["ok"] is True, out
    return hub, rec, hub.get(KEY).ask  # type: ignore[union-attr]


def _fill(ask: Any, **values: str) -> dict[str, Any]:
    return {"ask_id": ask.id, "action": "fill", "values": values}


async def test_a_two_screen_sign_in_is_typed_screen_by_screen_and_leaks_nowhere(
    sel_rows: list[dict[str, Any]],  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    page = LoginPage()
    user, password = _two_step(page)
    hub, rec, ask = await _ask(page)
    wire = lh.live_event(hub.get(KEY))["ask"]  # type: ignore[arg-type]
    assert wire["origin"] == "https://accounts.example.com"
    assert wire["steps"] == {"username": "required"}
    status, out, card = await _answer(_fill(ask, username=EMAIL, password=CANARY))
    assert (status, out) == (200, {"ok": True, "outcome": "submitted"})
    # Each value in its own screen's field, through the focus-proving insert; Enter twice.
    assert page.inserts == [(user, EMAIL), (password, CANARY)]
    assert page.enters == 2
    assert page.nodes[password].get("asked")
    polled = await _poll(ask.id)
    assert polled == {"done": True, "outcome": "submitted"}
    text = tool._ask_text(polled["outcome"])
    assert "submitted" in text
    for surface in (
        json.dumps(polled),
        text,
        json.dumps(out),
        caplog.text,
        json.dumps(sel_rows, default=str),
        json.dumps([e for _k, e in rec.events]),
        json.dumps(card.events, default=str),
        json.dumps(lh.live_event(hub.get(KEY))),  # type: ignore[arg-type]
    ):
        assert CANARY not in surface and EMAIL not in surface


async def test_the_walk_stops_and_drops_the_password_when_the_tab_leaves_the_site() -> None:
    page = LoginPage()
    user, password = _two_step(page)

    def away(focused: int | None) -> None:
        page.frame.update(url="https://accounts-example.evil.test/password", loaderId="L9")
        page.show("F1", password=password)

    page.on_enter = away
    _hub, _rec, ask = await _ask(page)
    status, out, _ = await _answer(_fill(ask, username=EMAIL, password=CANARY))
    assert (status, out["error"]) == (409, "origin_changed")
    assert page.inserts == [(user, EMAIL)]
    assert ask.outcome == "origin_changed"


async def test_a_site_change_racing_the_origin_read_still_gets_nothing() -> None:
    page = LoginPage()
    user, password = _two_step(page)

    def race(focused: int | None) -> None:
        page.stale_tree = dict(page.frame)  # the next read still says accounts.example.com
        page.frame.update(url="https://evil.test/password", loaderId="L9")
        page.nodes[user]["connected"] = False
        page.show("F1", password=password)

    page.on_enter = race
    _hub, _rec, ask = await _ask(page)
    status, out, _ = await _answer(_fill(ask, username=EMAIL, password=CANARY))
    # The insert reads the document's own origin in the same call, and refuses.
    assert (status, out["error"]) == (409, "origin_changed")
    assert page.inserts == [(user, EMAIL)]


async def test_a_same_site_reload_between_screens_is_still_the_bound_site() -> None:
    page = LoginPage()
    user, password = _two_step(page)

    def reload(focused: int | None) -> None:
        page.frame.update(url="https://accounts.example.com/challenge/pwd", loaderId="L2")
        page.nodes[user]["connected"] = False
        page.show("F1", password=password)

    page.on_enter = reload
    _hub, _rec, ask = await _ask(page)
    status, out, _ = await _answer(_fill(ask, username=EMAIL, password=CANARY))
    assert (status, out["outcome"]) == (200, "submitted")
    assert page.inserts == [(user, EMAIL), (password, CANARY)]


async def test_next_is_clicked_once_when_enter_does_not_move_the_screen_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(qa, "STEP_NUDGE_S", 0.0)
    page = LoginPage()
    user, password = _two_step(page)
    page.on_enter = None

    def clicked() -> None:
        page.nodes[user]["connected"] = False
        page.show("F1", password=password)

    page.on_click = clicked
    _hub, _rec, ask = await _ask(page)
    status, out, _ = await _answer(_fill(ask, username=EMAIL, password=CANARY))
    assert (status, out["outcome"]) == (200, "submitted")
    assert len([c for c in page.clicks() if c["type"] == "mousePressed"]) == 1
    # At the button's centre: (20 + 90/2, 80 + 30/2).
    assert (page.clicks()[0]["x"], page.clicks()[0]["y"]) == (65.0, 95.0)
    assert page.inserts == [(user, EMAIL), (password, CANARY)]


async def test_the_walk_stops_typing_when_the_ask_runs_out_mid_walk() -> None:
    page = LoginPage()
    user, password = _two_step(page)
    _hub, _rec, ask = await _ask(page)

    def expire(focused: int | None) -> None:
        ask.deadline = 0.0  # the ask's time is up while the password screen loads
        page.nodes[user]["connected"] = False
        page.show("F1", password=password)

    page.on_enter = expire
    status, _out, _ = await _answer(_fill(ask, username=EMAIL, password=CANARY))
    assert status == 409 and ask.outcome == "timed_out"
    assert page.inserts == [(user, EMAIL)]


async def test_an_ask_that_runs_out_while_the_password_box_is_found_types_nothing() -> None:
    page = LoginPage()
    password = page.add("p1", type="password", rect=[20, 30, 300, 30])
    page.show("F1", password=password)
    _hub, _rec, ask = await _ask(page)

    def expire(role: str) -> None:
        if role == "password":
            ask.deadline = 0.0  # runs out inside discovery, after the loop's own check

    page.on_find = expire
    status, _out, _ = await _answer(_fill(ask, password=CANARY))
    assert status == 409 and ask.outcome == "timed_out"
    assert page.inserts == []


async def test_no_field_in_time_keeps_the_ask_open_for_a_takeover() -> None:
    page = LoginPage()
    user, _password = _two_step(page)
    page.on_enter = None  # a passkey or a puzzle came instead of the password screen
    page.login["F1"].pop("action")
    hub, _rec, ask = await _ask(page)
    status, out, _ = await _answer(_fill(ask, username=EMAIL, password=CANARY))
    assert (status, out["error"]) == (409, "no_field")
    assert page.inserts == [(user, EMAIL)] and not ask.outcome
    status, out, _ = await _answer({"ask_id": ask.id, "action": "take_over"})
    assert (status, out) == (200, {"ok": True})
    assert hub.in_control(KEY) and ask.phase == "taken_over"


@pytest.mark.parametrize("problem", [{"covered": True}, {"visible": False}, {"type": "text"}])
async def test_a_password_box_that_fails_its_checks_is_never_typed_into(
    problem: dict[str, Any],
) -> None:
    page = LoginPage()
    password = page.add("p1", type="password", rect=[20, 30, 300, 30])
    page.show("F1", password=password)
    _hub, _rec, ask = await _ask(page)
    page.nodes[password].update(problem)
    status, out, _ = await _answer(_fill(ask, password=CANARY))
    assert (status, out["error"]) == (409, "no_field")
    assert page.inserts == []


async def test_a_password_only_screen_needs_no_username_and_a_username_screen_does() -> None:
    page = LoginPage()
    password = page.add("p1", type="password", rect=[20, 30, 300, 30])
    page.show("F1", password=password)
    hub, _rec, ask = await _ask(page)
    assert lh.live_event(hub.get(KEY))["ask"]["steps"] == {"username": "none"}  # type: ignore[arg-type]
    status, out, _ = await _answer(_fill(ask, password=CANARY))
    assert (status, out["outcome"]) == (200, "submitted")
    assert page.inserts == [(password, CANARY)]

    lh.reset_live_hub()
    page2 = LoginPage()
    _two_step(page2)
    _hub2, _rec2, ask2 = await _ask(page2)
    for values in ({"password": CANARY}, {"username": EMAIL}, {"password": CANARY, "otp": "1"}):
        status, out, _ = await _answer(_fill(ask2, **values))
        assert (status, out["error"]) == (400, "bad_action"), values
    assert page2.inserts == []


async def test_nothing_on_the_page_yet_makes_the_username_optional() -> None:
    page = LoginPage()
    hub, _rec, _ask_ = await _ask(page)
    wire = lh.live_event(hub.get(KEY))["ask"]  # type: ignore[arg-type]
    assert wire["steps"] == {"username": "optional"}
    assert wire["origin"] == "https://accounts.example.com"


# ── iframes ────────────────────────────────────────────────────────────────


def _store_with_frames(page: LoginPage) -> None:
    page.frame.update(url="https://store.example.com/checkout")
    page.add_frame(
        "F2", "https://id.example.com/signin", session=CHILD, content=(100, 200, 400, 300)
    )


async def test_a_sign_in_iframe_binds_its_origin_and_is_walked_through_its_session() -> None:
    page = LoginPage()
    _store_with_frames(page)
    user, password = _two_step(page, frame="F2")
    hub, _rec, ask = await _ask(page, oopif=True)
    wire = lh.live_event(hub.get(KEY))["ask"]  # type: ignore[arg-type]
    assert (wire["origin"], wire["top_origin"]) == (
        "https://id.example.com",
        "https://store.example.com",
    )
    status, out, _ = await _answer(_fill(ask, username=EMAIL, password=CANARY))
    assert (status, out["outcome"]) == (200, "submitted")
    assert page.inserts == [(user, EMAIL), (password, CANARY)]
    focus_sids = {p["_sid"] for m, p in page.calls if m == "DOM.focus"}
    assert focus_sids == {CHILD}


async def test_a_same_process_sign_in_frame_is_walked_and_enrolled_for_the_guards() -> None:
    """idmsa.apple.com in store.apple.com: same site, so no child session. The walk
    finds and fills it through the top session, and the frame is enrolled first so
    the screenshot and typing guards count its filled fields."""
    page = LoginPage()
    page.frame.update(url="https://store.example.com/checkout")
    page.add_frame("F2", "https://id.example.com/signin", pierce=True)
    user, password = _two_step(page, frame="F2")
    hub, _rec, ask = await _ask(page, oopif=True)
    wire = lh.live_event(hub.get(KEY))["ask"]  # type: ignore[arg-type]
    assert (wire["origin"], wire["steps"]) == ("https://id.example.com", {"username": "required"})
    reg = hub.get(KEY).stream.frames  # type: ignore[union-attr]
    assert reg.local == set()
    status, out, _ = await _answer(_fill(ask, username=EMAIL, password=CANARY))
    assert (status, out["outcome"]) == (200, "submitted")
    assert page.inserts == [(user, EMAIL), (password, CANARY)]
    assert reg.local == {"F2"}


async def test_a_password_box_in_another_origins_frame_is_never_typed_into() -> None:
    page = LoginPage()
    _store_with_frames(page)
    page.add_frame(
        "F3", "https://ads.example.net/promo", session="ADS", content=(100, 520, 400, 200)
    )
    user, _password = _two_step(page, frame="F2")
    decoy = page.add("x1", frame="F3", type="password", rect=[10, 10, 300, 30])

    def decoy_only(focused: int | None) -> None:
        page.nodes[user]["connected"] = False
        page.show("F2")
        page.show("F3", password=decoy)

    page.on_enter = decoy_only
    _hub, _rec, ask = await _ask(page, oopif=True)
    status, out, _ = await _answer(_fill(ask, username=EMAIL, password=CANARY))
    assert (status, out["error"]) == (409, "no_field")
    assert page.inserts == [(user, EMAIL)]


async def test_a_see_through_sign_in_frame_is_not_typed_into() -> None:
    page = LoginPage()
    _store_with_frames(page)
    page.nodes[page.frames["F2"]["owner"]]["op"] = 0.1
    password = page.add("p1", frame="F2", type="password", rect=[20, 30, 300, 30])
    page.show("F2", password=password)
    _hub, _rec, ask = await _ask(page, oopif=True)
    status, out, _ = await _answer(_fill(ask, password=CANARY))
    assert (status, out["error"]) == (409, "no_field")
    assert page.inserts == []
