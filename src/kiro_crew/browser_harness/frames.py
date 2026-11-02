"""Cross-origin iframes of the agent's tab, read through the gateway's own CDP session.

Chrome runs a cross-origin iframe (a sign-in form from ``idmsa.apple.com`` inside
``secure9.store.apple.com``) as an out-of-process iframe (OOPIF): the page's own
script cannot reach into it, so the snapshot walker in the top document stops at
its ``<iframe>`` (a ``slot`` node). The live stream's relay-owned session turns on
``Target.setAutoAttach`` (flat), and every OOPIF directly under the tab gets a child
session (:class:`FrameRegistry`, fed by the stream's event callback; the relay keeps
the child sessions private, ``relay.CdpRelay._track_child``). Through it the gateway:

* walks each slot's frame with the same walker (:class:`FrameOps`), and names its
  refs ``f<tag>e<n>``: ``tag`` is a short number for the frame id, stable for the
  stream's life, and ``e<n>`` the frame's own walker ref;
* resolves those refs to page pixels by adding the frame owner's content box
  (``DOM.getFrameOwner`` + ``DOM.getBoxModel``, measured by the browser, not by the
  page); a scaled or transformed frame cannot be placed and is refused;
* reads the frame's focused field, and counts its password and quick-ask fields,
  so the agent's ``type``/``press_key``/``screenshot`` guards hold inside it too.

Same-origin iframes need none of this: the walker walks into them in place.
A cross-origin iframe of the SAME SITE (``idmsa.apple.com`` in ``store.apple.com``,
``secure.soundcloud.com`` in ``soundcloud.com``) is no OOPIF: Chrome keeps it in the
tab's own process, so no child session ever attaches. Such a frame, directly under
the top frame, is read through the stream's own session instead, in a fixed
isolated world of that frame (:data:`WALKER_WORLD`; Chrome hands back the same
world for the same name until the frame's document changes, so its refs live as
long as that document). Only frames directly under the tab are read; one nested
deeper stays a ``slot`` the outline marks as not read.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

#: A ref into a cross-origin frame: ``f<tag>e<n>``.
FRAME_REF_RE = re.compile(r"^f([1-9][0-9]{0,3})(e[1-9][0-9]{0,6})$")
#: Most cross-origin frames one snapshot reads (the walker hands out as many slots).
FRAME_READ_CAP = 8
#: The most frame tags one stream hands out: the largest ``<tag>`` FRAME_REF_RE
#: accepts. A frame attached after that is still tracked for the secret-field
#: guards, but gets no tag, so it is never read into an outline.
FRAME_TAG_MAX = 9999
#: The isolated world a same-process cross-origin frame's walker runs in.
WALKER_WORLD = "kirocrew_frame_walker"
#: How far the owner's box may differ from the frame's own viewport, in CSS
#: pixels, before the frame counts as scaled (its points would land elsewhere).
SCALE_TOLERANCE_PX = 1.5


def split_frame_ref(ref: Any) -> tuple[int, str] | None:
    """``"f2e14"`` -> ``(2, "e14")``; anything else -> ``None``."""
    if not isinstance(ref, str):
        return None
    m = FRAME_REF_RE.match(ref)
    return (int(m.group(1)), m.group(2)) if m else None


class FrameRegistry:
    """The OOPIF child sessions directly under one stream's session.

    Fed from the stream's event callback (:meth:`handle`). A frame's ``tag`` is
    handed out once per frame id and never reused, so a ref minted before the
    frame went away cannot come to name another frame.
    """

    def __init__(self) -> None:
        self.parent = ""
        self._session: dict[str, str] = {}
        #: Same-process cross-origin frames directly under the tab, read through
        #: the stream's own session (:meth:`FrameOps.adopt`).
        self.local: set[str] = set()
        self._tags: dict[str, int] = {}
        self._by_tag: dict[int, str] = {}

    def handle(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        if msg.get("sessionId") != self.parent or not self.parent:
            return
        params = msg.get("params") or {}
        if method == "Target.attachedToTarget":
            info = params.get("targetInfo") or {}
            frame_id, sid = info.get("targetId"), params.get("sessionId")
            if info.get("type") == "iframe" and isinstance(frame_id, str) and isinstance(sid, str):
                self._session[frame_id] = sid
                self.tag_of(frame_id)
        elif method == "Target.detachedFromTarget":
            sid = params.get("sessionId")
            for frame_id, s in list(self._session.items()):
                if s == sid:
                    del self._session[frame_id]

    def session_of(self, frame_id: str) -> str:
        return self._session.get(frame_id, "")

    def tag_of(self, frame_id: str) -> int:
        """The frame's tag, or 0 once :data:`FRAME_TAG_MAX` tags are handed out.

        Tags are never reused, so a stale ref cannot reach a later frame.
        """
        tag = self._tags.get(frame_id)
        if tag is None:
            if len(self._tags) >= FRAME_TAG_MAX:
                return 0
            tag = len(self._tags) + 1
            self._tags[frame_id] = tag
            self._by_tag[tag] = frame_id
        return tag

    def frame_of(self, tag: int) -> str:
        return self._by_tag.get(tag, "")

    def frames(self) -> list[tuple[str, str]]:
        """``(frame_id, session_id)`` of every attached child frame."""
        return list(self._session.items())


async def enable_auto_attach(relay: Any, session_id: str) -> None:
    """Attach every OOPIF under *session_id*, flat, without pausing it."""
    params: dict[str, Any] = {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True}
    try:
        await relay.send(
            "Target.setAutoAttach", {**params, "filter": [{"type": "iframe"}]}, session_id
        )
    except Exception:  # noqa: BLE001 - an older browser without target filters
        await relay.send("Target.setAutoAttach", params, session_id)


class FrameOps:
    """What ``ops.py`` asks of the cross-origin frames of the stream's tab."""

    def __init__(self, relay: Any, top_session: str, registry: FrameRegistry, walker: str) -> None:
        self.relay = relay
        self.top = top_session
        self.registry = registry
        self.walker = walker

    async def _send(self, method: str, params: dict[str, Any], sid: str) -> dict[str, Any]:
        reply = await self.relay.send(method, params, sid)
        return reply if isinstance(reply, dict) else {}

    async def _eval(self, where: tuple[str, int | None], expression: str) -> Any:
        """Evaluate in *where*: ``(session, None)`` is that session's page world,
        ``(top session, context id)`` a same-process frame's walker world."""
        sid, ctx = where
        params: dict[str, Any] = {"expression": expression, "returnByValue": True}
        if ctx is not None:
            params["contextId"] = ctx
        out = await self._send("Runtime.evaluate", params, sid)
        if out.get("exceptionDetails"):
            return None
        return (out.get("result") or {}).get("value")

    async def _walker(self, where: tuple[str, int | None], method: str, *args: Any) -> Any:
        rendered = ", ".join(json.dumps(a) for a in args)
        return await self._eval(where, f"{self.walker}.{method}({rendered})")

    async def _children(self) -> dict[str, str]:
        """``{frame id: url}`` of the frames directly under the tab's top frame."""
        tree = await self._send("Page.getFrameTree", {}, self.top)
        out: dict[str, str] = {}
        for child in (tree.get("frameTree") or {}).get("childFrames") or []:
            frame = child.get("frame") or {}
            if isinstance(frame.get("id"), str):
                out[frame["id"]] = str(frame.get("url") or "")
        return out

    async def adopt(self, frame_id: str) -> bool:
        """Read *frame_id* through the top session: only a frame directly under the
        tab that no child session holds (a same-process cross-origin frame)."""
        if not frame_id or self.registry.session_of(frame_id):
            return False
        if frame_id not in await self._children():
            return False
        self.registry.local.add(frame_id)
        return True

    async def where(self, frame_id: str) -> tuple[str, int | None] | None:
        """Where *frame_id*'s walker runs, or ``None`` when it cannot be read now.

        A same-process frame that left the tree is forgotten here, so a guard
        counting frames stops counting it as unreadable."""
        sid = self.registry.session_of(frame_id) if frame_id else ""
        if sid:
            return sid, None
        if frame_id not in self.registry.local:
            return None
        try:
            if frame_id not in await self._children():
                self.registry.local.discard(frame_id)
                return None
            made = await self._send(
                "Page.createIsolatedWorld",
                {"frameId": frame_id, "worldName": WALKER_WORLD, "grantUniveralAccess": False},
                self.top,
            )
        except Exception:  # noqa: BLE001 - mid-navigation: unreadable this time
            return None
        ctx = made.get("executionContextId")
        return (self.top, ctx) if isinstance(ctx, int) else None

    async def owner_origin_box(
        self, frame_id: str, where: tuple[str, int | None]
    ) -> tuple[float, float] | None:
        """Where the frame's viewport sits in the page's, or ``None`` when it cannot
        be placed exactly (scaled, rotated, gone)."""
        try:
            owner = await self._send("DOM.getFrameOwner", {"frameId": frame_id}, self.top)
            node = owner.get("backendNodeId")
            if not isinstance(node, int):
                return None
            model = (await self._send("DOM.getBoxModel", {"backendNodeId": node}, self.top)).get(
                "model"
            ) or {}
            q = model.get("content")
            size = await self._eval(where, "[innerWidth, innerHeight]")
        except Exception:  # noqa: BLE001 - the frame is gone or mid-navigation
            return None
        if not isinstance(size, list) or len(size) != 2:
            return None
        return content_origin(q, size[0], size[1])

    async def call(self, tag: int, method: str, local_ref: str, *args: Any) -> Any:
        """Run one ref-taking walker method in the frame *tag*; ``None`` when unreadable.

        A point it answers (``resolve``, ``check``) comes back in page pixels: the
        frame's point plus its owner's box.
        """
        frame_id = self.registry.frame_of(tag)
        where = await self.where(frame_id) if frame_id else None
        if where is None:
            return None
        try:
            value = await self._walker(where, method, local_ref, *args)
        except Exception:  # noqa: BLE001 - the frame went away under the call
            return None
        if (
            isinstance(value, dict)
            and value.get("ok")
            and isinstance(value.get("x"), (int, float))
            and isinstance(value.get("y"), (int, float))
        ):
            at = await self.owner_origin_box(frame_id, where)
            if at is None:
                return {"ok": False, "code": "frame_unplaceable"}
            value = {**value, "x": float(value["x"]) + at[0], "y": float(value["y"]) + at[1]}
        return value

    async def slot_frames(self, count: int) -> list[str]:
        """The frame id behind each of the walker's ``slot`` nodes ('' when unknown)."""
        out: list[str] = []
        for i in range(min(count, FRAME_READ_CAP)):
            frame_id = ""
            try:
                found = await self._send(
                    "Runtime.evaluate",
                    {"expression": f"(window.__kcFrameSlots || [])[{i}] || null"},
                    self.top,
                )
                obj = (found.get("result") or {}).get("objectId")
                if obj:
                    try:
                        desc = await self._send("DOM.describeNode", {"objectId": obj}, self.top)
                        frame_id = str((desc.get("node") or {}).get("frameId") or "")
                    finally:
                        await self._send("Runtime.releaseObject", {"objectId": obj}, self.top)
            except Exception:  # noqa: BLE001 - one unreadable slot stays unread
                frame_id = ""
            out.append(frame_id)
        return out

    async def frame_url(self, frame_id: str, where: tuple[str, int | None]) -> str:
        if where[1] is not None:
            return (await self._children()).get(frame_id, "")
        tree = await self._send("Page.getFrameTree", {}, where[0])
        return str(((tree.get("frameTree") or {}).get("frame") or {}).get("url") or "")

    async def url(self, frame_id: str) -> str:
        """Read only the browser's committed URL, before any frame content is read."""
        if not frame_id:
            return ""
        try:
            sid = self.registry.session_of(frame_id)
            if sid:
                return await self.frame_url(frame_id, (sid, None))
            return (await self._children()).get(frame_id, "")
        except Exception:  # noqa: BLE001 - unknown URLs fail the caller's gate closed
            return ""

    async def url_for_ref(self, tag: int) -> str:
        return await self.url(self.registry.frame_of(tag))

    async def outline(self, frame_id: str, budget: int) -> tuple[int, str, list[Any]] | None:
        """``(tag, url, nodes)`` of one frame, its refs already ``f<tag>``-prefixed."""
        if not frame_id or budget <= 0:
            return None
        try:
            if not self.registry.session_of(frame_id):
                await self.adopt(frame_id)
            where = await self.where(frame_id)
            if where is None:
                return None
            url = await self.frame_url(frame_id, where)
            data = await self._walker(where, "snapshot", budget)
        except Exception:  # noqa: BLE001 - the frame navigated or went away
            return None
        if not isinstance(data, dict) or not isinstance(data.get("nodes"), list):
            return None
        tag = self.registry.tag_of(frame_id)
        if not tag:
            return None  # past the ref grammar's range: read as not readable
        nodes = []
        for n in data["nodes"]:
            if not isinstance(n, dict):
                continue
            n = dict(n)
            if n.get("ref"):
                n["ref"] = f"f{tag}{n['ref']}"
            n.pop("slot", None)  # a cross-origin frame nested in this one is not read
            nodes.append(n)
        return tag, url, nodes

    async def focused(self) -> dict[str, Any] | None:
        """The focused field inside the child frame that holds focus, or ``None``.

        WHICH frame holds focus is read from the browser, not from any frame's
        script: the top document's ``activeElement`` in an isolated world (page
        script cannot patch it there), then ``DOM.describeNode`` for its frame id.
        Only that frame is asked what its field is; ``None`` when it is not one
        we read (a child session, or a same-process frame :meth:`adopt` took).
        """
        try:
            tree = await self._send("Page.getFrameTree", {}, self.top)
            top_id = str(((tree.get("frameTree") or {}).get("frame") or {}).get("id") or "")
            made = await self._send(
                "Page.createIsolatedWorld",
                {"frameId": top_id, "worldName": "kirocrew_frames", "grantUniveralAccess": False},
                self.top,
            )
            found = await self._send(
                "Runtime.evaluate",
                {
                    "expression": "document.activeElement",
                    "contextId": made.get("executionContextId"),
                },
                self.top,
            )
            obj = (found.get("result") or {}).get("objectId")
            if not obj:
                return None
            try:
                desc = await self._send("DOM.describeNode", {"objectId": obj}, self.top)
            finally:
                await self._send("Runtime.releaseObject", {"objectId": obj}, self.top)
            frame_id = str((desc.get("node") or {}).get("frameId") or "")
            where = await self.where(frame_id) if frame_id else None
            if where is None:
                return None
            active = await self._walker(where, "activeField")
        except Exception:  # noqa: BLE001 - unreadable: the caller fails closed
            return None
        if not isinstance(active, dict) or not active.get("focused") or active.get("frame"):
            return None  # nothing focused there, or deeper than we read
        return active

    async def _isolated(self, sid: str) -> int | None:
        """An isolated world on the root frame of child session *sid*, or ``None``."""
        tree = await self._send("Page.getFrameTree", {}, sid)
        frame_id = str(((tree.get("frameTree") or {}).get("frame") or {}).get("id") or "")
        if not frame_id:
            return None
        made = await self._send(
            "Page.createIsolatedWorld",
            {"frameId": frame_id, "worldName": WALKER_WORLD, "grantUniveralAccess": False},
            sid,
        )
        ctx = made.get("executionContextId")
        return ctx if isinstance(ctx, int) else None

    async def count(self, method: str) -> int:
        """Sum of ``askedValues``/``passwordBoxes`` over the child frames; one that
        cannot be read counts as one, so a guard fails closed."""
        total = 0
        for _frame_id, sid in self.registry.frames():
            try:
                # In an isolated world of the child's own frame: a frame script that
                # patches its DOM functions cannot make the count read zero there.
                ctx = await self._isolated(sid)
                n = await self._walker((sid, ctx), method) if ctx is not None else None
            except Exception:  # noqa: BLE001
                n = None
            total += n if isinstance(n, int) and not isinstance(n, bool) else 1
        for frame_id in list(self.registry.local):
            try:
                where = await self.where(frame_id)
                if where is None and frame_id not in self.registry.local:
                    continue  # it left the page: nothing there to count
                n = await self._walker(where, method) if where is not None else None
            except Exception:  # noqa: BLE001
                n = None
            total += n if isinstance(n, int) and not isinstance(n, bool) else 1
        return total


def content_origin(
    quad: Any, frame_width: Any, frame_height: Any = None
) -> tuple[float, float] | None:
    """The top-left of an axis-aligned, unscaled content quad, else ``None``.

    Unscaled on both axes: a ``scaleY`` frame is still axis-aligned and as wide as
    its viewport, but its points land elsewhere. *frame_height* ``None`` checks the
    width only (a caller that has just one side).
    """
    if not isinstance(quad, list) or len(quad) != 8:
        return None
    try:
        x1, y1, x2, y2, x3, y3, x4, y4 = (float(v) for v in quad)
    except (TypeError, ValueError):
        return None
    if abs(y1 - y2) > 0.5 or abs(x2 - x3) > 0.5 or abs(y3 - y4) > 0.5 or abs(x4 - x1) > 0.5:
        return None  # rotated or skewed
    if isinstance(frame_width, bool) or not isinstance(frame_width, (int, float)):
        return None
    if abs((x2 - x1) - float(frame_width)) > SCALE_TOLERANCE_PX:
        return None  # scaled: a frame pixel is not a page pixel
    if frame_height is not None:
        if isinstance(frame_height, bool) or not isinstance(frame_height, (int, float)):
            return None
        if abs((y4 - y1) - float(frame_height)) > SCALE_TOLERANCE_PX:
            return None
    return x1, y1


__all__ = [
    "FRAME_READ_CAP",
    "FRAME_REF_RE",
    "FrameOps",
    "FrameRegistry",
    "content_origin",
    "enable_auto_attach",
    "split_frame_ref",
]
