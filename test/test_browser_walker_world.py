"""The snapshot walker reads the page from an isolated world, not the page's own.

A page script can replace ``Element.prototype.getAttribute``, a property getter or
``document.querySelectorAll`` in its own world. The walker decides which fields are
secret with those calls, so it runs in an isolated world: the DOM is shared, the
page's replacements are not. Only the console hook and the navigation stamp, which
need the page's own ``console`` and ``window``, stay in the page's world.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.browser_harness import frames, helper_worker, ops, quick_ask


def _ctx(seen: list[str]) -> ops.OpContext:
    async def helper(name: str, **_kw: Any) -> Any:
        seen.append(name)
        if name == "current_tab":
            return {"url": "https://example.com/"}
        if name in ("closed_secret_count", "frame_owner_count"):
            return 0
        return {"ok": True}

    return ops.OpContext(helper=helper)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method",
    [
        "snapshot",
        "observe",
        "cells",
        "resolve",
        "focus",
        "activeField",
        "selectOption",
        "textPresent",
        "check",
        "back",
    ],
)
async def test_every_read_of_the_page_runs_in_the_isolated_world(method: str) -> None:
    seen: list[str] = []
    await ops._page(_ctx(seen), method, "e1")
    assert seen == ["current_tab", "js_isolated"], method


@pytest.mark.asyncio
@pytest.mark.parametrize("method", sorted(ops.PAGE_WORLD_METHODS))
async def test_only_the_console_hook_and_the_stamp_stay_in_the_page_world(method: str) -> None:
    seen: list[str] = []
    await ops._page(_ctx(seen), method, "x")
    assert "js" in seen and "js_isolated" not in seen, method


def test_the_page_world_methods_are_the_ones_that_need_the_pages_own_window() -> None:
    assert ops.PAGE_WORLD_METHODS == {"stamp", "state", "consoleMessages"}


def test_the_stream_side_reaches_the_helpers_walker_world_by_name() -> None:
    # Chrome gives every session the same world for the same name, so this one name
    # is what lets the stream's session read the refs the helper's walker made.
    assert frames.TOP_WALKER_WORLD == helper_worker._GUARD_WORLD
    assert frames.TOP_WALKER_WORLD != frames.WALKER_WORLD


class _Relay:
    """A relay that records every call and answers like a tab with one child frame."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], str]] = []
        self.worlds: dict[int, tuple[str, str, str]] = {}

    async def send(self, method: str, params: dict[str, Any], sid: str) -> Any:
        self.calls.append((method, dict(params), sid))
        if method == "Page.getFrameTree":
            if sid == "CHILD":
                return {"frameTree": {"frame": {"id": "child", "url": "https://other.test/"}}}
            return {"frameTree": {"frame": {"id": "top", "url": "https://example.com/"}}}
        if method == "Page.createIsolatedWorld":
            # Chrome hands back the same world for the same frame and name.
            key = (sid, params["frameId"], params["worldName"])
            ctx = next((c for c, k in self.worlds.items() if k == key), 100 + len(self.worlds))
            self.worlds[ctx] = key
            return {"executionContextId": ctx}
        if method == "Runtime.evaluate":
            if "contextId" in params and "__kcFrameSlots" in params["expression"]:
                return {"result": {"objectId": "slot"}}
            if "contextId" in params:
                return {"result": {"value": {"nodes": []}}}
            return {"result": {"value": None}}
        if method == "DOM.describeNode":
            return {"node": {"frameId": "child"}}
        if method == "Runtime.releaseObject":
            return {}
        raise AssertionError(method)


def _frame_ops(relay: _Relay) -> frames.FrameOps:
    registry = frames.FrameRegistry()
    registry.parent = "TOP"
    registry.tag_of("child")
    registry.handle(
        {
            "sessionId": "TOP",
            "method": "Target.attachedToTarget",
            "params": {"sessionId": "CHILD", "targetInfo": {"type": "iframe", "targetId": "child"}},
        }
    )
    return frames.FrameOps(relay, "TOP", registry, "WALKER")


@pytest.mark.asyncio
async def test_a_child_session_frame_is_read_in_its_own_isolated_world() -> None:
    relay = _Relay()
    ops_ = _frame_ops(relay)
    where = await ops_.where("child")
    assert where is not None and where[0] == "CHILD" and where[1] is not None
    assert relay.worlds[where[1]] == ("CHILD", "child", frames.WALKER_WORLD)
    await ops_.call(1, "resolve", "e1")
    (read,) = [p for m, p, _s in relay.calls if m == "Runtime.evaluate"]
    assert read["contextId"] == where[1]


@pytest.mark.asyncio
async def test_a_child_frame_url_is_read_from_its_own_session_not_the_tops_children() -> None:
    relay = _Relay()
    ops_ = _frame_ops(relay)
    where = await ops_.where("child")
    assert where is not None
    assert await ops_.frame_url("child", where) == "https://other.test/"


@pytest.mark.asyncio
async def test_frame_slots_are_read_in_the_walker_world_not_the_pages() -> None:
    relay = _Relay()
    ids = await _frame_ops(relay).slot_frames(1)
    assert ids == ["child"]
    ((sid, frame_id, name),) = relay.worlds.values()
    assert (sid, frame_id, name) == ("TOP", "top", frames.TOP_WALKER_WORLD)
    (read,) = [p for m, p, _s in relay.calls if m == "Runtime.evaluate"]
    assert read["contextId"] == 100 and "__kcFrameSlots" in read["expression"]


@pytest.mark.asyncio
async def test_frame_slots_are_unread_when_the_walker_world_cannot_be_made() -> None:
    class Broken(_Relay):
        async def send(self, method: str, params: dict[str, Any], sid: str) -> Any:
            if method == "Page.createIsolatedWorld":
                raise RuntimeError("detached")
            return await super().send(method, params, sid)

    assert await _frame_ops(Broken()).slot_frames(2) == ["", ""]


@pytest.mark.asyncio
async def test_a_quick_ask_target_ref_is_looked_up_in_the_walker_world() -> None:
    relay = _Relay()
    probe = quick_ask.PageProbe(relay, "TOP")
    try:
        await probe.ref_node("e3")
    except quick_ask.AskError:
        pass  # the fake holds no element: only WHERE the ref is read matters here
    ((sid, frame_id, name),) = relay.worlds.values()
    assert (sid, frame_id, name) == ("TOP", "top", frames.TOP_WALKER_WORLD)
    (read,) = [p for m, p, _s in relay.calls if m == "Runtime.evaluate"]
    assert read["contextId"] == 100 and "__kcRefs" in read["expression"]
