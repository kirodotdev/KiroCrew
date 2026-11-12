"""Out-of-process frames nested at any depth attach to the live stream, and a
screenshot is allowed on a page with cross-origin frames only when the frame
inspector proves it checked every one of them (``FrameOps.coverage``).

A web editor such as Word on the web runs inside a cross-origin frame that holds
frames of its own. Before, any frame the top page could not read refused the
screenshot even with the live view open. Now each such frame, nested ones
included, is walked in its own session, and the refusal stands whenever the proof
falls short: a frame with no session, a count that cannot be taken, a field or
frame in a closed shadow root, or a same-site frame with no session of its own.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from browser_live_helpers import SESSION, make_stream

from kiro_crew.browser_harness import frames as fr
from kiro_crew.browser_harness import ops

pytestmark = pytest.mark.asyncio

TOP = "top-session"


def _attach(source: str, frame_id: str, sid: str) -> dict[str, Any]:
    return {
        "method": "Target.attachedToTarget",
        "sessionId": source,
        "params": {"sessionId": sid, "targetInfo": {"targetId": frame_id, "type": "iframe"}},
    }


def _detach(source: str, sid: str) -> dict[str, Any]:
    return {"method": "Target.detachedFromTarget", "sessionId": source, "params": {"sessionId": sid}}


def _el(name: str, **kw: Any) -> dict[str, Any]:
    node: dict[str, Any] = {"nodeName": name}
    attrs = kw.pop("attrs", None)
    if attrs:
        node["attributes"] = [x for kv in attrs.items() for x in kv]
    node.update(kw)
    return node


def _doc(*kids: dict[str, Any]) -> dict[str, Any]:
    return _el("#document", children=[_el("HTML", children=list(kids))])


def _closed(*kids: dict[str, Any]) -> dict[str, Any]:
    return _el(
        "DIV",
        shadowRoots=[
            {"nodeName": "#document-fragment", "shadowRootType": "closed", "children": list(kids)}
        ],
    )


class Relay:
    """Answers the CDP calls each frame session makes, from a table per session."""

    def __init__(self, sessions: dict[str, dict[str, Any]]) -> None:
        self.sessions = sessions
        self.calls: list[tuple[str, str]] = []

    async def send(self, method: str, params: dict[str, Any], sid: str) -> dict[str, Any]:
        self.calls.append((method, sid))
        s = self.sessions[sid]
        if method in s.get("fail", ()):
            raise RuntimeError(f"{method}: gone")
        if method == "Page.getFrameTree":
            kids = [{"frame": {"id": c}} for c in s.get("children", ())]
            return {
                "frameTree": {
                    "frame": {"id": "F-" + sid, "url": s.get("url", "")},
                    "childFrames": kids,
                }
            }
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": 7}
        if method == "Runtime.evaluate":
            for name, value in s.get("walker", {}).items():
                if f".{name}(" in params["expression"]:
                    return {"result": {"value": value}}
            return {"result": {"value": 0}}
        if method == "DOM.getDocument":
            return {"root": s["root"]}
        if method == "DOM.disable":
            return {}
        if method == "DOM.getFrameOwner":
            return {"backendNodeId": 9}
        if method == "DOM.getBoxModel":
            return {"model": {"content": s["quad"]}}
        raise AssertionError(method)


def _word_page(**over: Any) -> tuple[fr.FrameOps, fr.FrameRegistry]:
    """The tab holds the editor frame (one session); the editor holds one nested
    cross-origin frame of its own (a second session); both read clean."""
    editor: dict[str, Any] = {
        "url": "https://usc-word-edit.officeapps.live.com/we/wordeditorframe.aspx?token=s3cret",
        "walker": {"unreadableFrames": 1, "framesSeen": 1},
        "root": _doc(_el("BODY", children=[_el("IFRAME")])),
    }
    proxy: dict[str, Any] = {
        "url": "https://ext.example/proxy",
        "walker": {"unreadableFrames": 0, "framesSeen": 0},
        "root": _doc(_el("BODY")),
    }
    editor.update(over.pop("editor", {}))
    proxy.update(over.pop("proxy", {}))
    reg = fr.FrameRegistry()
    reg.parent = TOP
    reg.handle(_attach(TOP, "editor", "s-editor"))
    reg.handle(_attach("s-editor", "proxy", "s-proxy"))
    top: dict[str, Any] = {"children": ["editor"]}
    top.update(over.pop("top", {}))
    relay = Relay({TOP: top, "s-editor": editor, "s-proxy": proxy})
    return fr.FrameOps(relay, TOP, reg, "W"), reg


# ── the registry tracks frames nested at any depth ─────────────────────────


async def test_a_nested_frame_attaches_for_the_guards_but_gets_no_ref() -> None:
    reg = fr.FrameRegistry()
    reg.parent = TOP
    assert reg.handle(_attach(TOP, "editor", "s-editor")) == "s-editor"
    assert reg.handle(_attach("s-editor", "proxy", "s-proxy")) == "s-proxy"
    assert reg.frames() == [("editor", "s-editor")]
    assert reg.all_sessions() == [
        ("editor", "s-editor", TOP),
        ("proxy", "s-proxy", "s-editor"),
    ]
    assert reg.child_count(TOP) == 1 and reg.child_count("s-editor") == 1
    assert reg.tag_of("editor") == 1 and reg.frame_of(2) == ""
    assert reg.session_of("proxy") == ""


async def test_an_attach_from_a_session_the_stream_does_not_own_is_ignored() -> None:
    reg = fr.FrameRegistry()
    reg.parent = TOP
    assert reg.handle(_attach("stranger", "x", "s-x")) == ""
    assert reg.all_sessions() == []


async def test_a_detached_frame_takes_the_frames_inside_it_along() -> None:
    reg = fr.FrameRegistry()
    reg.parent = TOP
    reg.handle(_attach(TOP, "editor", "s-editor"))
    reg.handle(_attach("s-editor", "proxy", "s-proxy"))
    reg.handle(_attach("s-proxy", "deep", "s-deep"))
    reg.handle(_detach(TOP, "s-editor"))
    assert reg.all_sessions() == []


async def test_the_stream_turns_on_auto_attach_inside_each_attached_frame() -> None:
    stream, relay, _clock, _sink = make_stream()
    await stream.start()
    await relay.on_event(_attach(SESSION, "editor", "s-editor"))
    await asyncio.gather(*stream._attach_tasks)
    sids = [s for m, _p, s in relay.calls if m == "Target.setAutoAttach"]
    assert sids == [SESSION, "s-editor"]
    await stream.stop()


async def test_stop_cancels_an_auto_attach_still_in_flight() -> None:
    stream, relay, _clock, _sink = make_stream()
    await stream.start()
    gate = asyncio.Event()
    send = relay.send

    async def held(method: str, params: Any = None, session_id: Any = None) -> Any:
        if method == "Target.setAutoAttach" and session_id == "s-editor":
            await gate.wait()
        return await send(method, params, session_id)

    relay.send = held  # type: ignore[method-assign]
    await relay.on_event(_attach(SESSION, "editor", "s-editor"))
    (task,) = stream._attach_tasks
    await stream.stop()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert not stream._attach_tasks


async def test_a_nested_frame_with_a_password_masks_its_top_frame_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream, relay, _clock, _sink = make_stream()
    await stream.start()
    stream.frames.handle(_attach(SESSION, "editor", "s-editor"))
    stream.frames.handle(_attach("s-editor", "proxy", "s-proxy"))

    async def mask_query(sid: str) -> Any:
        boxes = [[5.0, 5.0, 40.0, 10.0]] if sid == "s-proxy" else []
        return {"result": {"value": {"boxes": boxes}}}

    async def frame_box(self: Any, frame_id: str, where: Any) -> Any:
        assert (frame_id, where) == ("editor", ("s-editor", None))
        return [100.0, 50.0, 800.0, 600.0]

    monkeypatch.setattr(stream, "_mask_query", mask_query)
    monkeypatch.setattr(fr.FrameOps, "frame_box", frame_box)
    assert await stream._frame_boxes() == [[100.0, 50.0, 800.0, 600.0]]
    await stream.stop()


async def test_a_clean_nested_frame_masks_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    stream, relay, _clock, _sink = make_stream()
    await stream.start()
    stream.frames.handle(_attach(SESSION, "editor", "s-editor"))
    stream.frames.handle(_attach("s-editor", "proxy", "s-proxy"))

    async def mask_query(_sid: str) -> Any:
        return {"result": {"value": {"boxes": []}}}

    monkeypatch.setattr(stream, "_mask_query", mask_query)
    assert await stream._frame_boxes() == []
    await stream.stop()


async def test_the_top_frame_of_a_deeply_nested_frame_is_found() -> None:
    reg = fr.FrameRegistry()
    reg.parent = TOP
    reg.handle(_attach(TOP, "editor", "s-editor"))
    reg.handle(_attach("s-editor", "proxy", "s-proxy"))
    reg.handle(_attach("s-proxy", "deep", "s-deep"))
    assert reg.top_frame_of("s-deep") == "editor"
    assert reg.top_frame_of("s-editor") == "editor"
    assert reg.top_frame_of("unknown") == ""


# ── coverage: the proof that every unreadable frame was checked ────────────


async def test_an_editor_with_a_nested_frame_is_covered() -> None:
    ops_, _reg = _word_page()
    assert await ops_.coverage(1) == ""


async def test_a_nested_frame_with_no_session_of_its_own_is_not_covered() -> None:
    ops_, _reg = _word_page(editor={"walker": {"unreadableFrames": 2, "framesSeen": 1}})
    why = await ops_.coverage(1)
    assert "holds 2 frame(s)" in why and "1 with a frame session" in why


async def test_the_reason_names_the_host_and_never_the_path_or_query() -> None:
    ops_, _reg = _word_page(editor={"walker": {"unreadableFrames": 3, "framesSeen": 1}})
    why = await ops_.coverage(1)
    assert why.startswith("usc-word-edit.officeapps.live.com:")
    assert "token" not in why and "wordeditorframe" not in why


async def test_a_top_count_the_sessions_do_not_match_is_not_covered() -> None:
    ops_, _reg = _word_page()
    assert "2 frame(s)" in await ops_.coverage(2)


async def test_a_password_in_a_closed_shadow_root_inside_a_frame_is_not_covered() -> None:
    root = _doc(_el("IFRAME"), _closed(_el("INPUT", attrs={"type": "password"})))
    ops_, _reg = _word_page(editor={"root": root})
    assert "closed shadow root" in await ops_.coverage(1)


async def test_a_frame_hidden_in_a_closed_shadow_root_inside_a_frame_is_not_covered() -> None:
    ops_, _reg = _word_page(proxy={"root": _doc(_closed(_el("IFRAME")))})
    assert "closed shadow root" in await ops_.coverage(1)


async def test_a_frame_whose_walk_misses_a_frame_owner_is_not_covered() -> None:
    # The browser sees two frame owners in the editor; the walk reached one.
    root = _doc(_el("IFRAME"), _el("IFRAME", contentDocument=_doc()))
    ops_, _reg = _word_page(editor={"root": root})
    assert "holds 2 frame(s)" in await ops_.coverage(1)


@pytest.mark.parametrize(
    "editor",
    [
        {"fail": ("DOM.getDocument",)},
        {"fail": ("Page.createIsolatedWorld",)},
        {"walker": {"unreadableFrames": True, "framesSeen": 1}},
        {"walker": {"unreadableFrames": None, "framesSeen": 1}},
        {"walker": {"unreadableFrames": 1, "framesSeen": -1}},
        {"walker": {"unreadableFrames": 1, "framesSeen": 3}},
    ],
)
async def test_a_frame_that_cannot_be_counted_is_not_covered(editor: dict[str, Any]) -> None:
    ops_, _reg = _word_page(editor=editor)
    assert await ops_.coverage(1) != ""


async def test_a_same_site_frame_with_no_session_is_not_covered() -> None:
    ops_, reg = _word_page(top={"children": ["editor", "same-site"]})
    reg.local.add("same-site")
    assert "same-site" in await ops_.coverage(1)


async def test_a_same_site_frame_that_left_the_tab_no_longer_blocks() -> None:
    # Adopted by an earlier snapshot, then the tab navigated: the frame is gone
    # from the tree, so it must not keep every later screenshot refused.
    ops_, reg = _word_page()
    reg.local.add("gone")
    assert await ops_.coverage(1) == ""
    assert "gone" not in reg.local


async def test_an_unreadable_tree_keeps_a_same_site_frame_blocking() -> None:
    ops_, reg = _word_page(top={"fail": ("Page.getFrameTree",)})
    reg.local.add("same-site")
    assert "same-site" in await ops_.coverage(1)


async def test_every_frame_read_turns_the_dom_domain_off_again() -> None:
    ops_, _reg = _word_page()
    assert await ops_.coverage(1) == ""
    calls = ops_.relay.calls
    for sid in ("s-editor", "s-proxy"):
        got = [m for m, s in calls if s == sid and m.startswith("DOM.")]
        assert got == ["DOM.getDocument", "DOM.disable"], (sid, got)


async def test_the_dom_domain_is_turned_off_even_when_the_read_fails() -> None:
    ops_, _reg = _word_page(editor={"fail": ("DOM.getDocument",)})
    assert await ops_.coverage(1) != ""
    assert ("DOM.disable", "s-editor") in ops_.relay.calls


async def test_a_scaled_frame_is_bounded_by_its_owner_quad() -> None:
    # A frame rotated 90 degrees: the quad's corners are not in reading order.
    quad = [300.0, 10.0, 300.0, 210.0, 100.0, 210.0, 100.0, 10.0]
    ops_, _reg = _word_page(top={"quad": quad})
    assert await ops_.owner_bounds("editor") == [100.0, 10.0, 200.0, 200.0]


@pytest.mark.parametrize("quad", [None, [1, 2, 3], [0, 0, 0, 0, 0, 0, 0, 0], ["a"] * 8])
async def test_an_owner_with_no_usable_quad_has_no_bounds(quad: Any) -> None:
    ops_, _reg = _word_page(top={"quad": quad})
    assert await ops_.owner_bounds("editor") is None


async def test_a_nested_password_in_a_scaled_frame_still_masks_the_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream, relay, _clock, _sink = make_stream()
    await stream.start()
    stream.frames.handle(_attach(SESSION, "editor", "s-editor"))
    stream.frames.handle(_attach("s-editor", "proxy", "s-proxy"))

    async def mask_query(sid: str) -> Any:
        boxes = [[5.0, 5.0, 40.0, 10.0]] if sid == "s-proxy" else []
        return {"result": {"value": {"boxes": boxes}}}

    async def no_box(self: Any, frame_id: str, where: Any) -> Any:
        return None

    async def bounds(self: Any, frame_id: str) -> Any:
        return [90.0, 40.0, 820.0, 620.0]

    monkeypatch.setattr(stream, "_mask_query", mask_query)
    monkeypatch.setattr(fr.FrameOps, "frame_box", no_box)
    monkeypatch.setattr(fr.FrameOps, "owner_bounds", bounds)
    assert await stream._frame_boxes() == [[90.0, 40.0, 820.0, 620.0]]
    await stream.stop()


async def test_a_page_with_more_frames_than_the_cap_is_not_covered() -> None:
    ops_, reg = _word_page()
    for i in range(fr.FRAME_COVERAGE_CAP):
        reg.handle(_attach("s-proxy", f"extra-{i}", f"s-extra-{i}"))
    assert f"more than {fr.FRAME_COVERAGE_CAP}" in await ops_.coverage(1)


@pytest.mark.parametrize(
    "size", [[0, 600], [800, -1], ["800", 600], [True, 600], [float("nan"), 600], None]
)
async def test_a_frame_with_no_usable_size_cannot_be_boxed(size: Any) -> None:
    class Sized(fr.FrameOps):
        async def owner_origin_box(self, frame_id: str, where: Any) -> Any:
            return (10.0, 20.0)

        async def _eval(self, where: Any, expression: str) -> Any:
            return size

    ops_ = Sized(Relay({}), TOP, fr.FrameRegistry(), "W")
    assert await ops_.frame_box("editor", ("s-editor", None)) is None


async def test_a_placed_frame_is_boxed_at_its_viewport_size() -> None:
    class Sized(fr.FrameOps):
        async def owner_origin_box(self, frame_id: str, where: Any) -> Any:
            return (10.0, 20.0)

        async def _eval(self, where: Any, expression: str) -> Any:
            return [800, 600]

    ops_ = Sized(Relay({}), TOP, fr.FrameRegistry(), "W")
    assert await ops_.frame_box("editor", ("s-editor", None)) == [10.0, 20.0, 800.0, 600.0]


# ── the screenshot guard ───────────────────────────────────────────────────


def _ctx(tmp_path: Path, frames: Any, *, unreadable: int = 1, owners: int = 1) -> ops.OpContext:
    async def helper(name: str, **kw: Any) -> Any:
        if name == "closed_secret_count":
            return 0
        if name == "frame_owner_count":
            return owners
        if name == "current_tab":
            return {"url": "https://example.com/"}
        if name == "js_isolated":
            expr = kw["expression"]
            if ".unreadableFrames(" in expr:
                return unreadable
            if ".framesSeen(" in expr:
                return owners
            return 0
        if name == "screenshot":
            Path(kw["path"]).write_bytes(b"png")
        return None

    return ops.OpContext(helper=helper, screenshot_dir=tmp_path, frames=lambda: frames)


async def test_a_page_whose_frames_are_all_covered_gets_its_screenshot(tmp_path: Path) -> None:
    ops_, _reg = _word_page()
    out = await ops.run_op("screenshot", {}, _ctx(tmp_path, ops_))
    assert out.get("ok") is True, out


async def test_a_filled_code_in_a_nested_frame_still_refuses(tmp_path: Path) -> None:
    ops_, _reg = _word_page(proxy={"walker": {"secretValues": 1}})
    out = await ops.run_op("screenshot", {}, _ctx(tmp_path, ops_))
    assert out.get("code") == "secret_value_on_page", out


async def test_an_asked_value_in_a_nested_frame_still_refuses(tmp_path: Path) -> None:
    ops_, _reg = _word_page(proxy={"walker": {"askedValues": 1}})
    out = await ops.run_op("screenshot", {}, _ctx(tmp_path, ops_))
    assert out.get("code") == "asked_value_on_page", out


async def test_the_refusal_says_which_frame_was_not_checked(tmp_path: Path) -> None:
    ops_, _reg = _word_page(editor={"walker": {"unreadableFrames": 2, "framesSeen": 1}})
    out = await ops.run_op("screenshot", {}, _ctx(tmp_path, ops_))
    assert out.get("code") == "unreadable_frame_on_page", out
    assert "usc-word-edit.officeapps.live.com" in out["error"]
    assert not list(tmp_path.glob("shot-*.png"))


async def test_with_no_live_view_the_refusal_says_so(tmp_path: Path) -> None:
    out = await ops.run_op("screenshot", {}, _ctx(tmp_path, None))
    assert out.get("code") == "unreadable_frame_on_page", out
    assert "no live view" in out["error"]


async def test_an_object_with_no_session_still_refuses(tmp_path: Path) -> None:
    # Two unreadable frames on the tab (the editor and an object); one session.
    ops_, _reg = _word_page()
    out = await ops.run_op("screenshot", {}, _ctx(tmp_path, ops_, unreadable=2, owners=2))
    assert out.get("code") == "unreadable_frame_on_page", out
