"""Quick asks inside iframes: a cross-origin (out-of-process) frame and a same-origin one.

The Apple shape: the sign-in form is an ``idmsa.apple.com`` iframe inside
``secure9.store.apple.com``. The ask binds to the FRAME's committed origin, its
id and loader, and its ``<iframe>`` owner, and re-checks all of them, plus the top
frame, before the gateway writes through the frame's own session. Refusals:
a frame that cannot be attached or verified, nested, hidden, tiny, see-through,
covered at the field, or scaled. Also: the sign-in takeover (credentials with no
targets), the op-side guards that now look into frames, and the relay keeping a
frame's child session private.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from test_browser_live_hub import SESSION
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

from kiro_crew.browser_harness import frames as fr
from kiro_crew.browser_harness import live_hub as lh
from kiro_crew.browser_harness import ops
from kiro_crew.browser_harness import quick_ask as qa
from kiro_crew.browser_harness.relay import CdpRelay
from kiro_crew.dashboard.handlers import browser_ask
from kiro_crew.mcp_tools import browser as tool

pytestmark = pytest.mark.asyncio

TOP_URL = "https://secure9.store.apple.com/shop/signIn"
IDMSA = "https://idmsa.apple.com/appleauth/auth/signin"
CHILD = "CHILD-SESSION"
CREDS = {
    "kind": "credentials",
    "targets": ["username=f1e3", "password=f1e4"],
    "reason": "Sign in to the Apple Store",
}


def _apple() -> FakePage:
    """The store page with Apple's sign-in iframe at (100, 200), out of process."""
    page = FakePage()
    page.frame.update(url=TOP_URL)
    page.add_frame("F2", IDMSA, session=CHILD, content=(100, 200, 400, 300))
    page.add("e3", frame="F2", type="email", rect=[20, 30, 300, 30])
    page.add("e4", frame="F2", type="password", rect=[20, 80, 300, 30])
    return page


def _registry(hub: lh.LiveHub, page: FakePage) -> fr.FrameRegistry:
    """The stream's frame registry, fed the attach events Chrome sends."""
    reg = fr.FrameRegistry()
    reg.parent = SESSION
    for fid, f in page.frames.items():
        if f["session"]:
            reg.handle(
                {
                    "method": "Target.attachedToTarget",
                    "sessionId": SESSION,
                    "params": {
                        "sessionId": f["session"],
                        "targetInfo": {"targetId": fid, "type": "iframe", "url": ""},
                    },
                }
            )
    hub.get(KEY).stream.frames = reg  # type: ignore[union-attr]
    return reg


async def _apple_ask(page: FakePage, args: dict[str, Any] | None = None) -> Any:
    hub, rec = await _live(page)
    _registry(hub, page)
    out = await browser_ask.run_ask(hub, KEY, dict(args or CREDS))
    return hub, rec, out


# ── binding ─────────────────────────────────────────────────────────────────


async def test_an_ask_in_a_cross_origin_frame_binds_to_the_frame_and_shows_both_origins() -> None:
    page = _apple()
    hub, _rec, out = await _apple_ask(page)
    assert out["ok"] is True and "ask_id" in out, out
    ask = hub.get(KEY).ask  # type: ignore[union-attr]
    assert (ask.origin, ask.frame_id, ask.loader_id) == ("https://idmsa.apple.com", "F2", "LF2")
    assert (ask.top_origin, ask.top_frame_id) == ("https://secure9.store.apple.com", "F1")
    assert ask.owner == page.frames["F2"]["owner"] and ask.frame_session == CHILD
    wire = lh.live_event(hub.get(KEY))["ask"]  # type: ignore[arg-type]
    assert wire["origin"] == "https://idmsa.apple.com"
    assert wire["top_origin"] == "https://secure9.store.apple.com"
    # Field rects are page pixels: the frame's own plus the owner's content corner.
    assert [f["rect"] for f in wire["fields"]] == [
        {"x": 120, "y": 230, "w": 300, "h": 30},
        {"x": 120, "y": 280, "w": 300, "h": 30},
    ]
    # The occlusion test ran in the TOP document at each field's mapped centre.
    hits = [
        c
        for c in page.calls
        if c[0] == "Runtime.callFunctionOn" and c[1]["functionDeclaration"] == qa._HIT_JS
    ]
    assert [[a["value"] for a in c[1]["arguments"]] for c in hits] == [
        [270.0, 245.0],
        [270.0, 295.0],
    ]


async def test_fill_lands_in_the_frame_through_its_own_session_and_leaks_nowhere(
    sel_rows: list[dict[str, Any]], caplog: pytest.LogCaptureFixture  # noqa: F811
) -> None:
    caplog.set_level(logging.DEBUG)
    page = _apple()
    hub, rec, _out = await _apple_ask(page)
    ask = hub.get(KEY).ask  # type: ignore[union-attr]
    status, out, wire = await _answer(
        {
            "ask_id": ask.id,
            "action": "fill",
            "values": {"username": "a@b.example", "password": CANARY},
        }
    )
    assert (status, out) == (200, {"ok": True, "outcome": "filled"})
    assert page.inserts == [
        (page.child_refs[CHILD]["e3"], "a@b.example"),
        (page.child_refs[CHILD]["e4"], CANARY),
    ]
    writes = [
        c
        for c in page.calls
        if c[0] == "DOM.focus" or c[1].get("functionDeclaration") == qa._INSERT_JS
    ]
    assert writes and all(c[1]["_sid"] == CHILD for c in writes)
    polled = await _poll(ask.id)
    assert polled == {"done": True, "outcome": "filled"}
    for surface in (
        json.dumps(polled),
        json.dumps(out),
        caplog.text,
        json.dumps(sel_rows, default=str),
        json.dumps([e for _k, e in rec.events]),
        json.dumps(wire.events, default=str),
    ):
        assert CANARY not in surface and "a@b.example" not in surface


@pytest.mark.parametrize(
    "change, code",
    [
        ("owner_hidden", "hidden"),
        ("owner_tiny", "hidden"),
        ("owner_transparent", "transparent"),
        ("owner_covered", "covered"),
        ("owner_decoy_no_pointer_events", "covered"),
        ("owner_blend_mode", "transparent"),
        ("owner_filter_opacity", "transparent"),
        ("owner_scaled", "frame_transformed"),
        ("owner_scaled_y", "frame_transformed"),
        ("owner_nested", "nested_frame"),
        ("frame_blank", "not_web_page"),
        ("not_attached", "ref_stale"),
        ("two_frames", "cross_frame"),
        ("no_owner", "frame_unverified"),
    ],
)
async def test_an_unfit_frame_is_refused_at_ask_time(change: str, code: str) -> None:
    page = _apple()
    owner = page.nodes[page.frames["F2"]["owner"]]
    args = dict(CREDS)
    if change == "owner_hidden":
        owner["visible"] = False
    elif change == "owner_tiny":
        # _INSPECT_JS reads a 1 x 1 box as not visible (the < 2 x 2 rule).
        owner.update(rect=[0, 0, 1, 1], visible=False)
    elif change == "owner_transparent":
        owner["op"] = 0.2
    elif change == "owner_covered":
        owner["hit"] = False
    elif change == "owner_decoy_no_pointer_events":
        owner["decoy"] = True  # elementFromPoint still sees the frame
    elif change == "owner_blend_mode":
        owner["blend"] = True
    elif change == "owner_filter_opacity":
        owner["op"] = 0.3  # opacity(0.3) in a filter folds into the product
    elif change == "owner_scaled":
        # Drawn at half size: a frame pixel is not a page pixel.
        owner["content"] = [100, 200, 200, 150]
        page.frames["F2"]["vw"] = 400
    elif change == "owner_scaled_y":
        # scaleY only: axis-aligned and as wide as its viewport, but half as tall.
        owner["content"] = [100, 200, 400, 150]
        page.frames["F2"]["vh"] = 300
    elif change == "owner_nested":
        owner["sameDoc"] = False
    elif change == "frame_blank":
        page.frames["F2"]["url"] = "about:blank"
    elif change == "not_attached":
        args["targets"] = ["username=f7e3", "password=f7e4"]
    elif change == "two_frames":
        page.add("e9", type="password")
        args["targets"] = ["username=f1e3", "password=e9"]
    elif change == "no_owner":
        page.frames["F2"]["hide_owner"] = True
    hub, _rec, out = await _apple_ask(page, args)
    assert out == {"ok": True, "outcome": "refused", "code": code}, out
    assert hub.get(KEY).ask is None  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "change",
    [
        "iframe_navigated_away",
        "iframe_same_origin_reload",
        "top_navigated",
        "owner_swapped",
        "decoy_laid_over",
        "frame_turned_transparent",
        "child_session_gone",
    ],
)
async def test_a_frame_that_moved_gets_origin_changed_and_nothing_written(change: str) -> None:
    page = _apple()
    hub, _rec, _out = await _apple_ask(page)
    ask = hub.get(KEY).ask  # type: ignore[union-attr]
    owner = page.nodes[page.frames["F2"]["owner"]]
    if change == "iframe_navigated_away":
        page.frames["F2"].update(url="https://evil.example/login", loaderId="LX")
    elif change == "iframe_same_origin_reload":
        page.frames["F2"]["loaderId"] = "LF2b"
    elif change == "top_navigated":
        page.frame.update(url="https://secure9.store.apple.com/other", loaderId="L2")
    elif change == "owner_swapped":
        page.frames["F2"]["owner"] = page.add_frame("F9", IDMSA)
    elif change == "decoy_laid_over":
        owner["hit"] = False
    elif change == "frame_turned_transparent":
        owner["op"] = 0.01
    else:
        page.frames["F2"]["session"] = "GONE"
    status, out, _wire = await _answer(
        {"ask_id": ask.id, "action": "fill", "values": {"username": "u", "password": CANARY}}
    )
    assert (status, out) == (
        409,
        {"ok": False, "error": "origin_changed", "code": "origin_changed"},
    )
    assert page.inserts == []
    assert (await _poll(ask.id))["outcome"] == "origin_changed"


async def test_a_same_origin_frame_binds_through_the_stream_session() -> None:
    page = FakePage()
    page.add_frame("F3", "https://github.com/embedded-login")
    page.add("e5", frame="F3", type="password", rect=[10, 10, 200, 30])
    hub, _rec = await _live(page)
    _registry(hub, page)
    out = await browser_ask.run_ask(
        hub, KEY, {"kind": "credentials", "targets": ["password=e5"], "reason": "Sign in"}
    )
    assert out["ok"] is True, out
    ask = hub.get(KEY).ask  # type: ignore[union-attr]
    assert (ask.frame_id, ask.origin, ask.frame_session) == ("F3", "https://github.com", SESSION)
    # Same origin as the top: the card shows one origin line.
    assert "top_origin" not in lh.live_event(hub.get(KEY))["ask"]  # type: ignore[arg-type]
    status, body, _ = await _answer(
        {"ask_id": ask.id, "action": "fill", "values": {"password": CANARY}}
    )
    assert (status, body["outcome"]) == (200, "filled")
    assert page.inserts == [(page.refs["e5"], CANARY)]


@pytest.mark.parametrize(
    "hit, code",
    [("inside_the_frame", ""), ("top_document_decoy", "covered"), ("another_frame", "covered")],
)
async def test_a_frame_in_the_tabs_own_process_is_hit_tested_through(hit: str, code: str) -> None:
    """Same site, another origin (idmsa.apple.com in store.apple.com): Chrome runs the
    frame in the tab's own process, and its hit test answers the field INSIDE the
    frame, not the ``<iframe>``. That passes only when the node it names is in the
    bound frame; a decoy in the top document or in a third frame is still refused."""
    page = FakePage()
    page.frame.update(url=TOP_URL)
    owner = page.nodes[page.add_frame("F3", IDMSA, pierce=True)]
    page.add("e5", frame="F3", type="password", rect=[10, 10, 200, 30])
    if hit == "top_document_decoy":
        owner["decoy"] = True  # elementFromPoint still sees the frame; the browser does not
    elif hit == "another_frame":
        owner["hit_frame"] = "F9"
    hub, _rec = await _live(page)
    _registry(hub, page)
    out = await browser_ask.run_ask(
        hub, KEY, {"kind": "credentials", "targets": ["password=e5"], "reason": "Sign in"}
    )
    if code:
        assert out == {"ok": True, "outcome": "refused", "code": code}, out
        assert hub.get(KEY).ask is None  # type: ignore[union-attr]
        return
    assert out["ok"] is True and "ask_id" in out, out
    ask = hub.get(KEY).ask  # type: ignore[union-attr]
    assert (ask.frame_id, ask.origin, ask.frame_session) == (
        "F3",
        "https://idmsa.apple.com",
        SESSION,
    )
    status, body, _ = await _answer(
        {"ask_id": ask.id, "action": "fill", "values": {"password": CANARY}}
    )
    assert (status, body["outcome"]) == (200, "filled")
    assert page.inserts == [(page.refs["e5"], CANARY)]


@pytest.mark.parametrize("adopted", [True, False])
async def test_a_frame_ref_in_a_same_process_frame_binds_through_the_stream_session(
    adopted: bool,
) -> None:
    """``f1e5`` in a same-process cross-origin frame resolves in that frame's walker
    world on the stream's own session, and only when the snapshot adopted the frame."""
    page = FakePage()
    page.frame.update(url=TOP_URL)
    page.add_frame("F2", IDMSA, pierce=True)
    node = page.add("x5", frame="F2", type="password", rect=[10, 10, 200, 30])
    page.world_refs["e5"] = node
    hub, _rec = await _live(page)
    reg = _registry(hub, page)
    assert reg.tag_of("F2") == 1
    if adopted:
        reg.local.add("F2")
    out = await browser_ask.run_ask(
        hub, KEY, {"kind": "credentials", "targets": ["password=f1e5"], "reason": "Sign in"}
    )
    if not adopted:
        assert out == {"ok": True, "outcome": "refused", "code": "ref_stale"}, out
        return
    assert out["ok"] is True and "ask_id" in out, out
    ask = hub.get(KEY).ask  # type: ignore[union-attr]
    assert (ask.frame_id, ask.origin, ask.frame_session) == (
        "F2",
        "https://idmsa.apple.com",
        SESSION,
    )
    assert ask.top_origin == "https://secure9.store.apple.com"
    status, body, _ = await _answer(
        {"ask_id": ask.id, "action": "fill", "values": {"password": CANARY}}
    )
    assert (status, body["outcome"]) == (200, "filled")
    assert page.inserts == [(node, CANARY)]


class _SameProcessRelay:
    """The top session of a tab whose cross-origin frame F2 shares its process."""

    WORLD = 41

    def __init__(self) -> None:
        self.children = [{"frame": {"id": "F2", "url": IDMSA + "?token=x"}}]
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []

    async def send(self, method: str, params: dict[str, Any], sid: str | None = None) -> Any:
        self.calls.append((method, params, sid))
        assert sid == SESSION, "a same-process frame is read through the top session"
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "F1"}, "childFrames": self.children}}
        if method == "Page.createIsolatedWorld":
            assert params["worldName"] == fr.WALKER_WORLD
            return {"executionContextId": self.WORLD}
        if method == "Runtime.evaluate":
            assert params.get("contextId") == self.WORLD
            expr = params["expression"]
            if expr.endswith(".snapshot(1400)"):
                nodes = [{"depth": 0, "role": "textbox", "name": "Email", "ref": "e1"}]
                return {"result": {"value": {"url": IDMSA, "nodes": nodes}}}
            if '.resolve("e1")' in expr:
                return {"result": {"value": {"ok": True, "x": 10, "y": 20}}}
            if expr == "[innerWidth, innerHeight]":
                return {"result": {"value": [400, 300]}}
            if expr.endswith(".passwordBoxes()"):
                return {"result": {"value": 1}}
        if method == "DOM.getFrameOwner":
            return {"backendNodeId": 99}
        if method == "DOM.getBoxModel":
            return {"model": {"content": [100, 200, 500, 200, 500, 500, 100, 500]}}
        raise AssertionError(f"unexpected {method} {params}")


async def test_a_same_process_cross_origin_frame_is_read_in_its_own_walker_world() -> None:
    relay = _SameProcessRelay()
    reg = fr.FrameRegistry()
    reg.parent = SESSION
    fo = fr.FrameOps(relay, SESSION, reg, "WALKER")
    tag, url, nodes = await fo.outline("F2", 1400)  # type: ignore[misc]
    assert (tag, url, reg.local) == (1, IDMSA + "?token=x", {"F2"})
    assert nodes == [{"depth": 0, "role": "textbox", "name": "Email", "ref": "f1e1"}]
    # Its refs click at page pixels: the frame's point plus its owner's content corner.
    assert await fo.call(1, "resolve", "e1") == {"ok": True, "x": 110.0, "y": 220.0}
    # Its password boxes count toward the guards.
    assert await fo.count("passwordBoxes") == 1
    # Gone from the page: forgotten, not counted as unreadable forever.
    relay.children = []
    assert await fo.count("passwordBoxes") == 0 and reg.local == set()
    assert await fo.call(1, "resolve", "e1") is None


async def test_only_a_frame_directly_under_the_tab_is_adopted() -> None:
    relay = _SameProcessRelay()
    reg = fr.FrameRegistry()
    reg.parent = SESSION
    fo = fr.FrameOps(relay, SESSION, reg, "WALKER")
    assert await fo.outline("F7", 1400) is None  # not a direct child of the top frame
    assert reg.local == set()


# ── the sign-in takeover: credentials with no targets ───────────────────────


async def test_credentials_with_no_targets_hand_the_page_over_and_hand_back_on_navigation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(qa, "STEP_WAIT_S", 0.0)
    page = _apple()
    hub, _rec, out = await _apple_ask(
        page, {"kind": "credentials", "reason": "Sign in to the Apple Store"}
    )
    assert out["ok"] is True, out
    ask = hub.get(KEY).ask  # type: ignore[union-attr]
    wire = lh.live_event(hub.get(KEY))["ask"]  # type: ignore[arg-type]
    assert wire["kind"] == "credentials"
    assert wire["fields"] == [{"role": "region", "rect": {"x": 100, "y": 150, "w": 500, "h": 400}}]
    # The walk finds no field it may type into here (this fake's finder names
    # none): nothing is written, the ask stays open, and taking over is the way on.
    status, body, _ = await _answer(
        {"ask_id": ask.id, "action": "fill", "values": {"password": CANARY}}
    )
    assert (status, body["error"]) == (409, "no_field")
    assert not ask.outcome
    status, body, _ = await _answer({"ask_id": ask.id, "action": "take_over"})
    assert (status, body) == (200, {"ok": True})
    assert hub.in_control(KEY) and ask.phase == "taken_over"
    # The whole page takes the person's input (a submit button may sit outside the form).
    assert hub.get(KEY).input_rect is None  # type: ignore[union-attr]
    assert (await _poll(ask.id))["done"] is False
    page.frame.update(url="https://secure9.store.apple.com/shop/bag", loaderId="L2")
    assert await _poll(ask.id) == {"done": True, "outcome": "passed"}
    assert not hub.in_control(KEY)
    assert page.inserts == []


async def test_the_sign_in_takeover_ends_on_done_and_on_esc() -> None:
    page = _apple()
    hub, _rec, _out = await _apple_ask(page, {"kind": "credentials", "reason": "Sign in"})
    ask = hub.get(KEY).ask  # type: ignore[union-attr]
    await _answer({"ask_id": ask.id, "action": "take_over"})
    assert (await _answer({"ask_id": ask.id, "action": "done"}))[1]["outcome"] == "passed"
    await _poll(ask.id)
    await browser_ask.run_ask(hub, KEY, {"kind": "credentials", "reason": "Sign in"})
    ask2 = hub.get(KEY).ask  # type: ignore[union-attr]
    await _answer({"ask_id": ask2.id, "action": "take_over"})
    await hub.release(KEY)
    assert (await _poll(ask2.id))["outcome"] == "passed"


async def test_take_over_and_done_are_refused_on_a_field_ask() -> None:
    page = _apple()
    hub, _rec, _out = await _apple_ask(page)
    ask = hub.get(KEY).ask  # type: ignore[union-attr]
    assert (await _answer({"ask_id": ask.id, "action": "take_over"}))[0] == 400
    assert not hub.in_control(KEY)


def test_credentials_targets_parse() -> None:
    assert qa.parse_ask({"kind": "credentials", "reason": "r"}).sign_in_takeover
    assert qa.parse_ask({"kind": "credentials", "targets": [], "reason": "r"}).targets == {}
    assert qa.parse_ask(CREDS).targets == {"username": "f1e3", "password": "f1e4"}
    with pytest.raises(qa.AskError):
        qa.parse_ask({"kind": "otp", "reason": "r", "digits": 6})
    with pytest.raises(qa.AskError):
        qa.parse_ask({**CREDS, "targets": ["password=f1x4"]})


# ── the frame plumbing ──────────────────────────────────────────────────────


def test_the_registry_tags_frames_once_and_forgets_detached_sessions() -> None:
    reg = fr.FrameRegistry()
    reg.parent = "P"
    attach = {
        "method": "Target.attachedToTarget",
        "sessionId": "P",
        "params": {"sessionId": "C", "targetInfo": {"targetId": "F2", "type": "iframe"}},
    }
    reg.handle(attach)
    reg.handle({**attach, "sessionId": "OTHER"})  # not ours: a grandchild or another tab
    reg.handle(
        {**attach, "params": {"sessionId": "W", "targetInfo": {"targetId": "X", "type": "worker"}}}
    )
    assert reg.frames() == [("F2", "C")] and reg.tag_of("F2") == 1 and reg.frame_of(1) == "F2"
    reg.handle(
        {"method": "Target.detachedFromTarget", "sessionId": "P", "params": {"sessionId": "C"}}
    )
    assert reg.frames() == [] and reg.frame_of(1) == "F2"  # the tag is never reused
    assert reg.tag_of("F9") == 2


@pytest.mark.parametrize(
    "quad, width, at",
    [
        ([10, 20, 410, 20, 410, 320, 10, 320], 400, (10.0, 20.0)),
        ([10, 20, 210, 20, 210, 170, 10, 170], 400, None),  # scaled
        ([10, 20, 410, 40, 390, 340, -10, 320], 400, None),  # rotated
        (None, 400, None),
    ],
)
def test_content_origin_refuses_a_frame_it_cannot_place(quad: Any, width: Any, at: Any) -> None:
    assert fr.content_origin(quad, width) == at


@pytest.mark.parametrize(
    "role, problem",
    [("password", ""), ("username", ""), ("otp", ""), ("action", "covered"), ("widget", "covered")],
)
def test_only_a_filled_field_may_sit_under_its_own_label(role: str, problem: str) -> None:
    """The gateway writes into a filled field; it CLICKS a confirm button or a
    widget, and a label over those would take the click."""
    base = {"type": "password" if role == "password" else "text"}
    if role == "action":
        base = {"tag": "button"}
    if role == "widget":
        base = {"tag": "iframe"}
    info = {
        **base,
        "connected": True,
        "sameDoc": True,
        "visible": True,
        "covered": True,
        "labelCovered": True,
        "rect": [0, 0, 100, 30],
        "tag": base.get("tag", "input"),
    }
    assert qa.target_problem(role, info) == problem
    assert qa.target_problem(role, {**info, "labelCovered": False}) == "covered"


def test_frame_refs_split() -> None:
    assert fr.split_frame_ref("f2e14") == (2, "e14")
    assert fr.split_frame_ref("e14") is None and fr.split_frame_ref("f0e1") is None


async def test_the_relay_keeps_a_frame_child_session_private() -> None:
    relay = CdpRelay()
    got: list[str] = []

    async def on_event(msg: dict[str, Any]) -> None:
        got.append(str(msg.get("method")))

    relay._own_sessions["P"] = on_event
    attach = {
        "method": "Target.attachedToTarget",
        "sessionId": "P",
        "params": {"sessionId": "C", "targetInfo": {"targetId": "F2", "type": "iframe"}},
    }
    await relay._dispatch_upstream(json.dumps(attach))
    assert relay.owns_session("C")
    await relay._dispatch_upstream(
        json.dumps({"method": "Runtime.consoleAPICalled", "sessionId": "C"})
    )
    assert relay.forwarded_down == 0  # never reached a daemon client
    await relay._dispatch_upstream(
        json.dumps(
            {"method": "Target.detachedFromTarget", "sessionId": "P", "params": {"sessionId": "C"}}
        )
    )
    assert not relay.owns_session("C")
    await relay._dispatch_upstream(json.dumps(attach))
    relay._drop_children("P")
    assert not relay.owns_session("C")


# ── the agent's ops look into frames ────────────────────────────────────────


class FakeFrames:
    """``FrameOps`` as the ops see it."""

    def __init__(self, *, focused: dict[str, Any] | None = None, counts: int = 0) -> None:
        self._focused = focused
        self._counts = counts
        self.calls: list[tuple[Any, ...]] = []

    async def call(self, tag: int, method: str, local: str, *args: Any) -> Any:
        self.calls.append((tag, method, local, *args))
        if method == "resolve":
            return {"ok": True, "x": 270.0, "y": 245.0, "tag": "input"}
        if method == "focus":
            return {"ok": True, "focused": True}
        return None

    async def focused(self) -> dict[str, Any] | None:
        return self._focused

    async def count(self, _method: str) -> int:
        return self._counts

    async def url(self, frame_id: str) -> str:
        return IDMSA if frame_id == "F2" else ""

    async def url_for_ref(self, tag: int) -> str:
        return IDMSA if tag == 1 else ""

    async def slot_frames(self, n: int) -> list[str]:
        return ["F2"][:n]

    async def outline(self, frame_id: str, budget: int) -> Any:
        if frame_id != "F2":
            return None
        return (
            1,
            IDMSA,
            [{"depth": 0, "role": "textbox", "name": "Email", "ref": "f1e3", "state": {}}],
        )


def _ctx(
    frames: Any, page: dict[str, Any], seen: list[tuple[str, dict[str, Any]]], tmp: Path
) -> ops.OpContext:
    async def helper(name: str, **kw: Any) -> Any:
        seen.append((name, kw))
        if name == "js":
            for method, value in page.items():
                if f".{method}(" in kw["expression"]:
                    return value
            return None
        if name == "screenshot":
            Path(kw["path"]).write_bytes(b"png")
        return None

    return ops.OpContext(helper=helper, frames=lambda: frames, screenshot_dir=tmp)


async def test_a_frame_ref_clicks_at_its_page_point(tmp_path: Path) -> None:
    seen: list[Any] = []
    frames = FakeFrames()
    out = await ops.run_op("click", {"ref": "f1e3"}, _ctx(frames, {}, seen, tmp_path))
    assert out["ok"] and (out["x"], out["y"]) == (270.0, 245.0)
    assert frames.calls[0] == (1, "resolve", "e3")
    assert ("click", {"x": 270.0, "y": 245.0, "button": "left", "clicks": 1}) in seen
    # No stream: a frame ref is stale, never resolved in the top page.
    out = await ops.run_op("click", {"ref": "f1e3"}, _ctx(None, {}, [], tmp_path))
    assert out["code"] == "ref_stale"


@pytest.mark.parametrize(
    "inner, code",
    [
        (None, "focus_in_frame"),
        ({"focused": True, "password": True, "asked": False, "hasFocus": True}, "password_field"),
        ({"focused": True, "password": False, "asked": True, "hasFocus": True}, "asked_field"),
        ({"focused": True, "password": False, "asked": False, "hasFocus": True}, None),
    ],
)
async def test_typing_into_a_cross_origin_frame_is_judged_by_that_frame(
    inner: Any, code: str | None, tmp_path: Path
) -> None:
    top = {"activeField": {"focused": True, "frame": True, "password": False, "asked": False}}
    seen: list[Any] = []
    ctx = _ctx(FakeFrames(focused=inner), top, seen, tmp_path)
    out = await ops.run_op("type", {"text": CANARY}, ctx)
    assert out.get("code") == code
    assert any(n == "type_text" for n, _ in seen) is (code is None)
    key = await ops.run_op("press_key", {"key": "a"}, ctx)
    assert key.get("code") == code


async def test_frame_secrets_count_toward_the_screenshot_guard(tmp_path: Path) -> None:
    ctx = _ctx(FakeFrames(counts=1), {"askedValues": 0, "passwordBoxes": 0}, [], tmp_path)
    assert (await ops.run_op("screenshot", {}, ctx))["code"] == "asked_value_on_page"
    ctx = _ctx(FakeFrames(counts=0), {"askedValues": 0, "passwordBoxes": 0}, [], tmp_path)
    assert (await ops.run_op("screenshot", {}, ctx))["ok"] is True


async def test_the_snapshot_reads_cross_origin_frames_into_the_outline(tmp_path: Path) -> None:
    nodes = [
        {"depth": 0, "role": "heading", "name": "Sign in"},
        {"depth": 0, "role": "iframe", "name": "Sign In with Apple", "slot": 0},
        {"depth": 0, "role": "iframe", "name": "ad", "slot": 1},
    ]
    page = {"snapshot": {"url": TOP_URL, "title": "Bag", "nodes": nodes}}
    out = await ops.run_op("snapshot", {}, _ctx(FakeFrames(), page, [], tmp_path))
    lines = out["snapshot"].splitlines()
    assert lines[1] == f'- iframe "Sign In with Apple" [frame f1 {IDMSA}]'
    assert lines[2] == '  - textbox "Email" [ref=f1e3]'
    assert lines[3] == '- iframe "ad" [cross-origin, not read]'
    assert "ask` kind credentials with no targets" in out["note"]
    text = tool._result_text("snapshot", out)
    assert "(note: 1 cross-origin iframe(s)" in text


def test_the_tool_tells_the_agent_about_frame_refs_and_the_no_target_sign_in() -> None:
    desc = tool.schemas()[0]["description"]
    assert "f1e3" in desc and "NO targets" in desc


class _FocusRelay:
    """The top session and two child frames; the browser says F2 holds focus."""

    def __init__(self, focused_frame: str) -> None:
        self.focused_frame = focused_frame

    async def send(self, method: str, params: dict[str, Any], sid: str) -> dict[str, Any]:
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "F1"}}}
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": 3}
        if method == "Runtime.evaluate" and sid == "TOP":
            return {"result": {"objectId": "o1"}}
        if method == "DOM.describeNode":
            return {"node": {"backendNodeId": 5, "frameId": self.focused_frame}}
        if method == "Runtime.evaluate":
            # Each child's own script: the AD frame lies that it holds a plain field.
            if sid == "AD":
                value = {"focused": True, "hasFocus": True, "password": False, "asked": False}
            else:
                value = {"focused": True, "hasFocus": True, "password": False, "asked": True}
            return {"result": {"value": value}}
        return {}


async def test_the_focused_frame_is_named_by_the_browser_not_by_a_frame_that_lies() -> None:
    reg = fr.FrameRegistry()
    reg.parent = "TOP"
    for fid, sid in (("FAD", "AD"), ("F2", "LOGIN")):
        reg.handle(
            {
                "method": "Target.attachedToTarget",
                "sessionId": "TOP",
                "params": {"sessionId": sid, "targetInfo": {"targetId": fid, "type": "iframe"}},
            }
        )
    ops_ = fr.FrameOps(_FocusRelay("F2"), "TOP", reg, "walker")
    assert (await ops_.focused() or {}).get("asked") is True
    unknown = fr.FrameOps(_FocusRelay("F9"), "TOP", reg, "walker")
    assert await unknown.focused() is None


async def test_a_frame_outline_is_clamped_and_its_url_loses_query_and_fragment(
    tmp_path: Path,
) -> None:
    class Hostile(FakeFrames):
        async def outline(self, frame_id: str, budget: int) -> Any:
            return (
                1,
                IDMSA + "?token=SECRET#frag",
                [
                    {"depth": 10**9, "role": "textbox", "name": "x"},
                    {"depth": "x", "role": "button", "name": "y"},
                ],
            )

    nodes = [{"depth": [1], "role": "iframe", "name": "f", "slot": 0}]
    page = {"snapshot": {"url": TOP_URL, "title": "t", "nodes": nodes}}
    out = await ops.run_op("snapshot", {}, _ctx(Hostile(), page, [], tmp_path))
    assert "SECRET" not in out["snapshot"] and "frag" not in out["snapshot"]
    assert max(len(line) for line in out["snapshot"].splitlines()) < 300


async def test_the_relay_swallows_late_detaches_of_a_dropped_parents_children() -> None:
    relay = CdpRelay()

    async def on_event(_msg: dict[str, Any]) -> None:
        return None

    relay._own_sessions["P"] = on_event
    attach = {
        "method": "Target.attachedToTarget",
        "sessionId": "P",
        "params": {"sessionId": "C", "targetInfo": {"targetId": "F2", "type": "iframe"}},
    }
    await relay._dispatch_upstream(json.dumps(attach))
    relay._own_sessions.pop("P")
    relay._drop_children("P")
    await relay._dispatch_upstream(
        json.dumps({"method": "Target.detachedFromTarget", "params": {"sessionId": "C"}})
    )
    assert relay.forwarded_down == 0 and "C" not in relay._closing


class _ChildCountRelay:
    """A child-session frame whose PAGE world was patched to report no secret field."""

    def __init__(self, world: bool = True) -> None:
        self.world = world
        self.evaluated: list[dict[str, Any]] = []

    async def send(self, method: str, params: dict[str, Any], sid: str) -> dict[str, Any]:
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "C1"}}}
        if method == "Page.createIsolatedWorld":
            assert sid == "CHILD" and params["frameId"] == "C1"
            return {"executionContextId": 41} if self.world else {}
        if method == "Runtime.evaluate":
            self.evaluated.append(dict(params))
            # The page world answers 0 (patched querySelectorAll); the isolated one 1.
            return {"result": {"value": 1 if params.get("contextId") == 41 else 0}}
        raise AssertionError(f"unexpected {method} {params}")


async def test_a_child_frame_counts_its_secret_fields_in_an_isolated_world() -> None:
    relay = _ChildCountRelay()
    reg = fr.FrameRegistry()
    reg.parent = SESSION
    reg.handle(
        {
            "method": "Target.attachedToTarget",
            "sessionId": SESSION,
            "params": {"sessionId": "CHILD", "targetInfo": {"targetId": "F9", "type": "iframe"}},
        }
    )
    fo = fr.FrameOps(relay, SESSION, reg, "WALKER")
    assert await fo.count("askedValues") == 1
    assert all(p.get("contextId") == 41 for p in relay.evaluated)


async def test_a_child_frame_with_no_isolated_world_counts_as_one() -> None:
    relay = _ChildCountRelay(world=False)
    reg = fr.FrameRegistry()
    reg.parent = SESSION
    reg.handle(
        {
            "method": "Target.attachedToTarget",
            "sessionId": SESSION,
            "params": {"sessionId": "CHILD", "targetInfo": {"targetId": "F9", "type": "iframe"}},
        }
    )
    fo = fr.FrameOps(relay, SESSION, reg, "WALKER")
    assert await fo.count("askedValues") == 1 and relay.evaluated == []


def test_frame_tags_stop_at_the_ref_grammar_and_are_never_reused(monkeypatch) -> None:
    monkeypatch.setattr(fr, "FRAME_TAG_MAX", 3)
    reg = fr.FrameRegistry()
    assert [reg.tag_of(f"F{i}") for i in range(5)] == [1, 2, 3, 0, 0]
    assert reg.tag_of("F1") == 2 and reg.frame_of(3) == "F2"
    assert fr.FRAME_REF_RE.match(f"f{9999}e1") and not fr.FRAME_REF_RE.match("f10000e1")
