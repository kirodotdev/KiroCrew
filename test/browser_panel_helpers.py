"""Fakes the panel tests share: a CDP relay that answers a panel tab, a browser, a socket.

``PanelRelay`` is ``browser_live_helpers.FakeRelay`` with one session per target, so
several panels can be open at once, and with answers for the questions a panel tab is
asked inside its page. ``FakeBrowser`` stands where ``PanelBrowser`` does and starts
nothing. Time is the shared ``FakeClock``: no test here waits.
"""

from __future__ import annotations

import asyncio
import json
import struct
from collections.abc import Collection
from typing import Any

from browser_live_helpers import FakeClock, FakeRelay

from kiro_crew.browser_harness import panel_hub as ph
from kiro_crew.browser_harness import panel_page
from kiro_crew.browser_harness.panel_stream import PanelStream
from kiro_crew.browser_harness.relay import CdpError

ORIGIN = "http://127.0.0.1:6777"
COOKIE = "mc_token_6777"
MAIN_FRAME = "F1"
WORLD = 9


class PanelRelay(FakeRelay):
    def __init__(self) -> None:
        super().__init__()
        self.events: dict[str, Any] = {}
        self.targets = 0
        #: What the page says is under a point, where its focus is, and its state.
        self.target_answer: Any = None
        self.focus_answer: Any = {"editable": False, "kind": "", "rect": None, "guarded": False}
        #: Set by a test: every ``Runtime.evaluate`` fails, as when the page cannot be asked.
        self.evaluate_fails = False
        #: Which window each tab is in, as the browser says (``Browser.getWindowForTarget``).
        self.windows: dict[str, int] = {}
        #: Set by a test: the browser says its window's size and the view in it
        #: (as Chromium does: a window of 1280 by 800 has a view of 1280 by 657).
        self.window_bounds: dict[str, Any] | None = None
        self.view: dict[str, Any] | None = None
        #: Set by a test: the browser refuses a new size for a window.
        self.window_refuses = False
        self.window_count = 0
        self.curtain_count = 0
        #: Set by a test: the browser makes no second tab, or puts it in another window.
        self.curtains = True
        self.curtain_strays = False
        self.state_answer: Any = {"path": "/chat", "title": "", "theme": "", "words": 1}
        #: Secret-field boxes the mask query finds, in CSS pixels.
        self.mask_boxes: list[list[float]] = []
        #: The picture a still's own cast gives as its first frame, and the view that frame names.
        self.screenshot = b""
        self.still_view: dict[str, Any] | None = None
        #: The handler of each second session of a tab (the still's own).
        self.second_sessions: dict[str, Any] = {}
        self.closed = False
        #: What the browser lists as its targets (``Target.getTargets``).
        self.listed: list[dict[str, Any]] = []
        #: Awaited while the browser makes a still's frame (a test makes a frame arrive then).
        self.on_screenshot: Any = None
        #: Set by a test to hold a tab half open: ``attach`` waits for it.
        self.attach_gate: Any = None
        self.attach_reached: Any = None
        #: The sessions whose cast runs IN THE BROWSER. Like Chromium, the fake
        #: refuses a start while one runs (``Screencast is already active``).
        self.casts: set[str | None] = set()
        #: Set by a test: this many starts are done by the browser and their
        #: answer never arrives; this many stops never reach the browser.
        self.lose_starts = 0
        self.lose_stops = 0

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        *,
        quiet: bool = False,
    ) -> dict[str, Any]:
        params = dict(params or {})
        if method == "Page.startScreencast" and params.get("format") == "png":
            # The still's own cast, on the tab's second session: its first frame comes at once.
            import base64

            self.calls.append((method, params, session_id))
            self.casts.add(session_id)
            if self.on_screenshot is not None:
                await self.on_screenshot()
            frame: dict[str, Any] = {"data": base64.b64encode(self.screenshot).decode("ascii")}
            if self.still_view is not None:
                frame["metadata"] = dict(self.still_view)
            handler = self.second_sessions.get(str(session_id))
            if handler is not None:
                await handler(
                    {"method": "Page.screencastFrame", "params": frame, "sessionId": session_id}
                )
            return {}
        if method == "Page.startScreencast":
            if session_id in self.casts:
                self.calls.append((method, params, session_id))
                raise CdpError(method, {"message": "Screencast is already active"})
            self.casts.add(session_id)
            if self.lose_starts:
                self.lose_starts -= 1
                self.calls.append((method, params, session_id))
                raise asyncio.TimeoutError
        elif method == "Page.stopScreencast":
            if self.lose_stops:
                self.lose_stops -= 1
                self.calls.append((method, params, session_id))
                raise asyncio.TimeoutError
            self.casts.discard(session_id)
        if method == "Target.createTarget":
            self.calls.append((method, params, session_id))
            if params.get("newWindow") is False:
                # A second tab of a window: it lands in the window that was made last, unless a
                # test says the browser put it elsewhere, or made none.
                if not self.curtains:
                    return {}
                self.curtain_count += 1
                curtain = f"C{self.curtain_count}"
                self.windows[curtain] = self.window_count + (1000 if self.curtain_strays else 0)
                return {"targetId": curtain}
            self.targets += 1
            self.window_count += 1
            self.windows[f"T{self.targets}"] = self.window_count
            return {"targetId": f"T{self.targets}"}
        if method == "Browser.getWindowForTarget":
            self.calls.append((method, params, session_id))
            window: dict[str, Any] = {"windowId": self.windows.get(str(params.get("targetId")))}
            if self.window_bounds is not None:
                window["bounds"] = dict(self.window_bounds)
            return window
        if method == "Page.getLayoutMetrics":
            self.calls.append((method, params, session_id))
            return {"cssLayoutViewport": dict(self.view)} if self.view is not None else {}
        if method == "Browser.setWindowBounds" and self.window_refuses:
            self.calls.append((method, params, session_id))
            raise CdpError(method, {"message": "Browser window not found"})
        if method == "Target.getTargets":
            self.calls.append((method, params, session_id))
            return {"targetInfos": list(self.listed)}
        if method == "Runtime.evaluate":
            self.calls.append((method, params, session_id))
            expression = str(params.get("expression") or "")
            if self.evaluate_fails:
                raise CdpError("Runtime.evaluate", {"message": "the page is gone"})
            if ".target(" in expression:
                value = self.target_answer
            elif ".focus()" in expression:
                # A list is a script: one answer each time the page is asked, the last one kept.
                answers = self.focus_answer
                if isinstance(answers, list):
                    value = answers.pop(0) if len(answers) > 1 else answers[0]
                else:
                    value = answers
            elif ".state()" in expression:
                value = self.state_answer
            elif ".caret(" in expression:
                value = "true" in expression
            elif "history.back" in expression:
                value = None
            else:  # the stream's mask query
                value = {
                    "boxes": list(self.mask_boxes),
                    "frames": [],
                    "named": [],
                    "sx": 0,
                    "sy": 0,
                }
            return {"result": {"value": value}}
        return await super().send(method, params, session_id)

    async def attach(self, target_id: str, on_event: Any) -> str:
        self.attached.append(target_id)
        if target_id in self.events:
            # A second session of a tab that has one: the first keeps the tab's events.
            session = f"S{self.attached.count(target_id)}-{target_id}"
            self.second_sessions[session] = on_event
            return session
        self.events[target_id] = on_event
        if self.attach_gate is not None:
            self.attach_reached.set()
            await self.attach_gate.wait()
        return f"S-{target_id}"

    async def push(self, target_id: str, method: str, params: dict[str, Any]) -> None:
        """One CDP event of *target_id*'s own session."""
        await self.events[target_id](
            {"method": method, "params": params, "sessionId": f"S-{target_id}"}
        )

    def dispatched(self) -> list[tuple[str, dict[str, Any]]]:
        """Every input the page was sent."""
        return [(m, p) for m, p, _ in self.calls if m.startswith("Input.")]


class FakeBrowser:
    """Stands where ``PanelBrowser`` does; counts what a real one would start."""

    def __init__(self) -> None:
        self.relay = PanelRelay()
        self.ensures = 0
        self.contexts: list[str] = []
        self.disposed: list[str] = []
        self.stops = 0
        self.origins: list[str] = []
        self.on_stray: Any = None
        self.on_stray_closed: Any = None
        #: Each page asked for through the browser's own door: context, new window, background.
        self.pages: list[tuple[str, bool, bool]] = []
        self.running = True

    async def ensure(self, origin: str = "") -> PanelRelay:
        self.ensures += 1
        self.origins.append(origin)
        return self.relay

    async def new_page(self, context_id: str, *, new_window: bool, background: bool = False) -> str:
        """As the real one: the browser makes the page; here nothing holds it at its start."""
        self.pages.append((context_id, new_window, background))
        params: dict[str, Any] = {
            "url": "about:blank",
            "browserContextId": context_id,
            "newWindow": new_window,
        }
        if background:
            params["background"] = True
        made = await self.relay.send("Target.createTarget", params)
        return str(made.get("targetId") or "")

    async def new_context(self) -> str:
        self.contexts.append(f"ctx{len(self.contexts) + 1}")
        return self.contexts[-1]

    async def dispose_context(self, context_id: str) -> None:
        self.disposed.append(context_id)

    async def stop(self) -> None:
        self.stops += 1


class Client:
    """One owner socket as the hub sees it, and everything the hub sent it."""

    def __init__(self, key: int, token: str) -> None:
        self.told: list[dict[str, Any]] = []
        self.pictures: list[dict[str, Any]] = []
        self.socket = ph.PanelSocket(
            key=key,
            origin=ORIGIN,
            cookie_name=COOKIE,
            send_json=self._json,
            send_bytes=self._bytes,
            token=token,
        )

    async def _json(self, data: dict[str, Any]) -> None:
        assert data["type"] == ph.PANEL_MESSAGE_TYPE
        self.told.append(data["data"])

    async def _bytes(self, blob: bytes) -> None:
        (length,) = struct.unpack_from(">I", blob, 0)
        self.pictures.append(json.loads(blob[4 : 4 + length]))

    def last(self, op: str) -> dict[str, Any]:
        return [m for m in self.told if m.get("op") == op][-1]


def make_hub(
    *,
    enabled: bool = True,
    denied: bool = False,
    members: Collection[str] | None = None,
    guarded: bool = True,
) -> tuple[ph.PanelHub, FakeBrowser, FakeClock]:
    """A hub over fakes. *members* is the owner's Bots as it reads them; ``None``: they cannot be read."""
    browser = FakeBrowser()
    clock = FakeClock()

    def factory(*args: Any, **kwargs: Any) -> PanelStream:
        return PanelStream(*args, **kwargs, auto_control=False, clock=clock, sleep=clock.sleep)

    async def browse_denied() -> bool:
        return denied

    hub = ph.PanelHub(
        browser,  # type: ignore[arg-type]
        enabled=lambda: enabled,
        browse_denied=browse_denied,
        stream_factory=factory,
        members=lambda: members,
        guarded=lambda: guarded,
    )
    return hub, browser, clock


async def open_live(
    hub: ph.PanelHub, browser: FakeBrowser, client: Client, panel_id: str = "a", path: str = "/chat"
) -> ph.Panel:
    """Open a panel and walk its tab to the loaded dashboard page."""
    await hub.open(client.socket, panel_id, path=path, width=1024, height=720)
    handle = client.last("opened")["handle"]
    panel = hub._by_handle[handle]
    relay = browser.relay
    await relay.push(
        panel.target_id, "Page.frameNavigated", {"frame": {"id": MAIN_FRAME, "url": ORIGIN + path}}
    )
    await relay.push(
        panel.target_id,
        "Runtime.executionContextCreated",
        {
            "context": {
                "id": WORLD,
                "name": panel_page.PANEL_WORLD,
                "auxData": {"frameId": MAIN_FRAME},
            }
        },
    )
    await relay.push(panel.target_id, "Page.loadEventFired", {})
    assert panel.status == ph.LIVE
    return panel
