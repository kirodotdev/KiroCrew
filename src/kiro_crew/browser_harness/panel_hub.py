"""Panel tabs: the dashboard's own pages, live, on panels in the owner's XR page.

The owner's XR page asks over its dashboard socket for a panel of one of the
dashboard's pages. The gateway opens that page in a tab of its own browser
(``panel_browser.py``), signed in as the owner, sends the tab's pictures back
over that same socket and to no other, and turns the hand's pointer, wheel,
text and keys into real browser input.

What holds, and where:

* **Off unless set.** ``browser.xr_pages`` (default off). With it off
  :meth:`PanelHub.open` refuses before any browser starts and the input routes
  refuse. ``capabilities.browse`` denied refuses the same way.
* **Scope.** A panel belongs to the socket that opened it: its pictures go to
  that socket's sender only. Input names a panel by the handle the hub gave
  and is taken only when the caller presents the same sign-in the socket was
  opened with (:meth:`PanelSocket.digest`); the handle is an address, never a
  credential.
* **The agent is not here.** The browser's relay serves no listener, and
  nothing in this module is reachable from an MCP tool, the agent's ``browser``
  ops or a route the internal secret is accepted on.
* **Addresses.** A panel opens a path of one fixed shape on this gateway's own
  origin, or the Bots page with the name of one of the owner's Bots and
  nothing else in its query (``panel_page.allowed_open``). The tab's main-frame document requests
  are paused and any other address, a redirect included, is failed; so is a
  document, of the tab or of a frame, whose address carries a parameter that
  makes the dashboard act with no press, or asks Settings by its query for a
  tab that the list leaves out (``panel_page.query_refused``). A
  move inside the page to an unlisted path, or to such an address, is stopped
  before it happens, and undone when it is seen all the same; a window the
  page opens is closed. The panel is told the path its tab shows now and, on
  the Bots page, the one Bot it names (``panel_page.address_of``): nothing
  else of a query.
* **Pages for looking at.** While the tab stands at a path of
  ``panel_page.LOOK_ONLY_SHAPES`` (the Task Runner, Schedule), or at a path
  the hub does not know, no press, key or text is sent: only the wheel and a
  pointer's move.
* **The guard is a setting.** The rules here that take something from the
  dashboard's own use (the list of pages, the pages for looking at, the grant
  controls, and the acting parameters but for ``token``) hold while
  ``browser.xr_pages_guarded`` is on. It is off by default: a panel
  then shows any page of the gateway's own origin but the few in
  ``panel_page.SHUT_SHAPES``, and takes a press, keys and text wherever the
  dashboard's page takes them. A panel keeps the way it was opened in; when
  the setting changes, every panel is closed.
* **Sign-in.** The owner's verified access token goes into the socket's own
  incognito context as a session cookie, over the debugging pipe. It is never
  logged, never part of an address and never written to a file, and the context
  is disposed when the socket closes. A response that carries the gate's own
  refusal marks the panel ``signed-out`` and stops its pictures.
* **Grant controls.** ``panel_page.GUARD_JS`` stops a press on a control that
  grants an agent permission inside the page; the hub also refuses to send such
  a press and says ``guarded``. Text and keys go to the page's focus, so the hub
  sends neither while the focus is inside a guarded surface, on a field as much
  as on a button. Both are the hub's own check: when the page cannot be asked,
  a press, a key and text are not sent (``unread``).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import itertools
import json
import logging
import math
import re
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import governance as browse_governance
from . import panel_cover, panel_fence, panel_page, panel_window
from .panel_browser import PanelBrowser, PanelUnavailable
from .panel_stream import PanelStream, css_size, texel_step
from .relay import CdpError, CdpRelay, RelayClosed
from .takeover import HeldInput, InputEvent, InputEventError, dispatch_input

logger = logging.getLogger(__name__)

#: A send to the page that its connection cut: it closed, answered with an error, or did not answer in time.
_SEND_LOST = (CdpError, RelayClosed, asyncio.TimeoutError)


class _Landed(HeldInput):
    """The commands of one input call that the page acknowledged, counted as they land.

    ``dispatch_input`` notes each acknowledged command on its ``held``; this one counts
    it and passes it on to the panel's own held input (``held``), so a send that the
    connection cuts part way still answers how many commands reached the page.
    """

    def __init__(self) -> None:
        super().__init__()
        self.count = 0
        self.held: HeldInput | None = None

    def note_call(self, event: InputEvent, method: str, params: dict[str, Any]) -> None:
        self.count += 1
        if self.held is not None:
            self.held.note_call(event, method, params)


#: How far a control may stand from where the picture that he aimed at showed it, and be the same one (CSS px).
STALE_PX = 4
#: The longest label that a press's ``aimed`` carries beside the box; a longer label of the control now is cut so.
AIMED_LABEL_MAX = 80

#: ``type`` of every panel message on the owner's socket, both ways.
PANEL_MESSAGE_TYPE = "xr_panel"

#: Panels one socket may hold, and panels this gateway shows at once. Each is a
#: whole page in the host's browser. One person can fill the gateway: two rows
#: of six pages stand round a head with none crossing another, and no more.
MAX_PANELS_PER_SOCKET = 12
MAX_PANELS = 12
#: Longest text one input may type.
MAX_TEXT_CHARS = 2000
#: The takeover mapping takes text in pieces of this size (``takeover.MAX_TEXT_CHARS``).
_TEXT_PIECE = 64

#: A panel id is the XR page's own name for it, bounded so it carries no free text.
_PANEL_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

#: Keys a panel may press. No modifier is taken: a chord is not a hand's press.
PANEL_KEYS = frozenset(
    {
        "Enter",
        "Backspace",
        "Delete",
        "Tab",
        "Escape",
        "ArrowUp",
        "ArrowDown",
        "ArrowLeft",
        "ArrowRight",
        "Home",
        "End",
        "PageUp",
        "PageDown",
    }
)

LOADING, LIVE, SIGNED_OUT, FAILED, CLOSED = "loading", "live", "signed-out", "failed", "closed"

#: The header the dashboard's sign-in gate puts on its own refusal.
_AUTH_REQUIRED_HEADER = "x-auth-required"

#: The page could not be asked: no isolated world yet, its reader is not there,
#: or the call failed. Not the same as an answer of "nothing here".
_UNREAD: Any = object()

SendJson = Callable[[dict[str, Any]], Awaitable[None]]
SendBytes = Callable[[bytes], Awaitable[None]]


class PanelRefused(Exception):
    """An input or a question the hub does not take; ``code`` names why."""

    def __init__(self, code: str, status: int = 409) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def token_digest(token: str) -> bytes:
    """What a sign-in is compared by: never the token itself."""
    return hashlib.sha256(token.encode("utf-8")).digest()


#: The claims of an access token that say when it was made and how long it
#: lasts. Every other claim says who it is for and what it is bound to.
_TIME_CLAIMS = frozenset({"exp", "session_exp", "iat", "nonce", "gen"})


def _claims(token: str) -> dict[str, Any] | None:
    try:
        raw = token.split(".", 1)[0]
        data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def renews(old: str, new: str) -> bool:
    """Whether *new* is the same sign-in as *old*, renewed.

    The dashboard page renews its access cookie while it is open, and the
    renewed token is another string. Both were verified by the sign-in gate
    (the one at the socket's upgrade, the other on the request that carries
    it); this only compares what they say. A renewal carries every claim of the
    session forward: the same person, the same kind, the same bounds. So a
    token with the same claims but for its times, and not an older one, is the
    same sign-in; one with any other claim is not, and does not take the panel.
    An access token does not name the chain it was renewed in, so another
    sign-in of the same person with the very same bounds reads as a renewal
    too. It gains nothing by it: it can already do on the dashboard everything
    the tab can, and the panel's pictures still go to the opener's socket only.
    """
    was, now = _claims(old), _claims(new)
    if was is None or now is None or now.get("kind") != "session":
        return False
    if {k: v for k, v in was.items() if k not in _TIME_CLAIMS} != {
        k: v for k, v in now.items() if k not in _TIME_CLAIMS
    }:
        return False
    made_was, made_now = was.get("iat"), now.get("iat")
    if not isinstance(made_was, (int, float)) or not isinstance(made_now, (int, float)):
        return False
    return made_now >= made_was


@dataclass
class PanelSocket:
    """One owner socket, as the hub needs it. Built by the dashboard's handler.

    ``token`` is the access token the sign-in gate verified for the socket's
    upgrade request. It is held in memory for the socket's life, to sign the
    socket's own tabs in, and is not part of ``repr``.
    """

    key: int
    origin: str
    cookie_name: str
    send_json: SendJson
    send_bytes: SendBytes
    token: str = field(repr=False, default="")

    @property
    def digest(self) -> bytes:
        return token_digest(self.token)


@dataclass
class Panel:
    """One open panel: a tab, its stream, and who may drive it."""

    id: str
    handle: str
    socket: PanelSocket
    target_id: str
    stream: PanelStream
    path: str
    status: str = LOADING
    main_frame: str = ""
    world: int | None = None
    pointer_on: bool = False
    #: The XR page wants pictures (the panel is in view); the tab's page may not be up yet.
    shown: bool = True
    #: An empty second tab in the panel's own window, or ``""`` when the browser gave none.
    curtain_id: str = ""
    #: The curtain is in front: the page is a background tab and is drawn about once a second.
    curtained: bool = False
    last_url: str = ""
    #: ``browser.xr_pages_guarded`` as it was when the panel was opened: its
    #: in-page scripts were built for that way, and the hub's checks follow it.
    guarded: bool = True
    held: HeldInput = field(default_factory=HeldInput)
    #: Held while the panel is shown or hidden: the curtain and the pictures change together.
    view: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class _SocketState:
    socket: PanelSocket
    context_id: str = ""
    panels: dict[str, Panel] = field(default_factory=dict)


class _Sink:
    """Where a panel's pictures go: the one socket that opened it."""

    def __init__(self, send_bytes: SendBytes) -> None:
        self._send = send_bytes

    async def frame(self, header: Mapping[str, Any], blob: bytes) -> None:
        await self._send(blob)


def _default_enabled() -> bool:
    """``browser.xr_pages`` as the gateway holds it now; unreadable is off."""
    try:
        from kiro_crew.config import live as live_config

        snap = live_config.snapshot()
    except Exception:  # noqa: BLE001 - a config that cannot be read turns nothing on
        return False
    return getattr(getattr(snap, "browser", None), "xr_pages", False) is True


def _default_guarded() -> bool:
    """``browser.xr_pages_guarded`` as the gateway holds it now; unreadable is on.

    Only the setting's own ``False`` takes the guard off.
    """
    try:
        from kiro_crew.config import live as live_config

        snap = live_config.snapshot()
    except Exception:  # noqa: BLE001 - a config that cannot be read takes no guard off
        return True
    return getattr(getattr(snap, "browser", None), "xr_pages_guarded", None) is not False


def _default_members() -> frozenset[str] | None:
    """The names of the owner's Bots as the gateway holds them now; ``None`` when they cannot be read."""
    try:
        from kiro_crew.config import live as live_config

        agents = getattr(live_config.snapshot(), "agents", None)
    except Exception:  # noqa: BLE001 - a config that cannot be read names no Bot
        return None
    return frozenset(str(name) for name in agents) if isinstance(agents, Mapping) else None


class PanelHub:
    """Every open panel of this gateway. One per process (:func:`get_panel_hub`)."""

    def __init__(
        self,
        browser: PanelBrowser,
        *,
        enabled: Callable[[], bool] = _default_enabled,
        browse_denied: Callable[[], Awaitable[bool]] | None = None,
        stream_factory: Callable[..., PanelStream] = PanelStream,
        members: Callable[[], Collection[str] | None] = _default_members,
        guarded: Callable[[], bool] = _default_guarded,
    ) -> None:
        self.browser = browser
        self._enabled = enabled
        self._members = members
        self._guarded = guarded
        self._browse_denied = browse_denied or _browse_denied
        self._stream_factory = stream_factory
        self._sockets: dict[int, _SocketState] = {}
        self._by_handle: dict[str, Panel] = {}
        self._handles = itertools.count(1)
        self._lock = asyncio.Lock()
        #: The sockets that asked which way holds, each with the way it was told last.
        self._watchers: dict[int, tuple[PanelSocket, bool]] = {}
        # A window that a page opened was closed before it ran anything: its panel says so.
        browser.on_stray = self._stray
        browser.on_stray_closed = self._stray_closed

    # ── what the hub is asked ──────────────────────────────────────────────

    def enabled(self) -> bool:
        return bool(self._enabled())

    def guarded(self) -> bool:
        """Whether the guard is on now. Anything but the setting's own off is on."""
        return self._guarded() is not False

    def panel_count(self) -> int:
        return len(self._by_handle)

    async def way(self, socket: PanelSocket) -> None:
        """Tell *socket* which way holds: ``{"op": "way", "guarded": ...}``.

        The XR page asks when its socket opens, and before a page comes up by
        itself: with the guard on nothing does. A socket that asked is told
        again when the setting changes while one of its panels stands
        (:meth:`_way_changed`).
        """
        guarded = self.guarded()
        self._watchers[socket.key] = (socket, guarded)
        await self._send(socket, {"op": "way", "guarded": guarded})

    async def _way_changed(self) -> None:
        """The guard was switched: every panel closes, and each socket that asked is told.

        A tab's in-page scripts are of the way it was opened in, so no tab is
        kept. The socket keeps its sign-in: it is the owner's own socket still,
        and its page opens its panels again in the new way with no reload.
        """
        async with self._lock:
            for state in list(self._sockets.values()):
                for panel in list(state.panels.values()):
                    await self._retire(panel, CLOSED, "way")
                state.panels.clear()
                if state.context_id:
                    await self.browser.dispose_context(state.context_id)
            self._sockets.clear()
            await self.browser.stop()
        guarded = self.guarded()
        for key, (socket, told) in list(self._watchers.items()):
            if told == guarded:
                continue
            self._watchers[key] = (socket, guarded)
            await self._send(socket, {"op": "way", "guarded": guarded})

    async def open(
        self,
        socket: PanelSocket,
        panel_id: Any,
        *,
        path: Any,
        width: Any,
        height: Any,
        texels: Any = None,
        prefs: Any = None,
    ) -> None:
        """Open *path* on a new panel of *socket*; every outcome is told to the socket."""
        if not isinstance(panel_id, str) or not _PANEL_ID_RE.fullmatch(panel_id):
            return
        # The address in the gateway's own spelling, read once: what is checked is what is opened.
        guarded = self.guarded()
        clean = panel_page.allowed_open(path, self._members(), guarded=guarded)
        refusal = await self._open_refusal(socket, panel_id, clean, width, height)
        if refusal is not None:
            await self._tell(socket, panel_id, FAILED, refusal)
            return
        size = css_size(width, height)
        assert size is not None and clean is not None
        async with self._lock:
            state = self._sockets.setdefault(socket.key, _SocketState(socket))
            if panel_id in state.panels:
                return
            # Counted again under the lock: opens that come in one burst all pass
            # the count above before any of them has a tab.
            if self._full(state):
                await self._drop_if_empty(state)
                await self._tell(socket, panel_id, FAILED, "too_many")
                return
            try:
                panel = await self._open_tab(state, panel_id, clean, size, texels, prefs, guarded)
            except PanelUnavailable as exc:
                await self._drop_if_empty(state)
                await self._tell(socket, panel_id, FAILED, exc.code)
                return
            except (CdpError, RelayClosed, asyncio.TimeoutError, OSError):
                logger.warning("xr panel: the tab did not open", exc_info=True)
                await self._drop_if_empty(state)
                await self._tell(socket, panel_id, FAILED, "browser_error")
                return
        await self._send(
            socket,
            {
                "op": "opened",
                "id": panel_id,
                "handle": panel.handle,
                "css": [panel.stream.css_w, panel.stream.css_h],
            },
        )

    async def _open_refusal(
        self, socket: PanelSocket, panel_id: str, clean: str | None, width: Any, height: Any
    ) -> str | None:
        if not self.enabled():
            return "off"
        if not socket.token or not socket.origin:
            return "not_signed_in"
        if await self._browse_denied():
            return "policy_denied"
        if clean is None:
            return "address"
        if css_size(width, height) is None:
            return "size"
        if self._full(self._sockets.get(socket.key)):
            return "too_many"
        return None

    def _full(self, state: _SocketState | None) -> bool:
        """Whether one more panel is one too many, for this socket or for the gateway."""
        if state is not None and len(state.panels) >= MAX_PANELS_PER_SOCKET:
            return True
        return len(self._by_handle) >= MAX_PANELS

    async def _open_tab(
        self,
        state: _SocketState,
        panel_id: str,
        path: str,
        size: tuple[int, int],
        texels: Any,
        prefs: Any,
        guarded: bool,
    ) -> Panel:
        socket = state.socket
        relay = await self.browser.ensure(socket.origin)
        if not state.context_id:
            state.context_id = await self.browser.new_context()
            await self._sign_in(relay, state)
        # Its own window: every panel's page is then the active one of a window
        # and keeps painting, however many panels there are.
        target_id = await self.browser.new_page(state.context_id, new_window=True)
        if not target_id:
            raise PanelUnavailable("no_tab", "the browser made no tab")
        panel = Panel(
            id=panel_id,
            handle=f"p{next(self._handles)}",
            socket=socket,
            target_id=target_id,
            stream=None,  # type: ignore[arg-type]  # set on the next line
            path=path,
            curtain_id=await self._hang_curtain(relay, state.context_id, target_id),
            guarded=guarded,
        )
        stream = self._stream_factory(
            relay,
            target_id,
            _Sink(socket.send_bytes),
            panel_id=panel_id,
            css_w=size[0],
            css_h=size[1],
            texels=texel_step(texels) if texels is not None else 2.0,
            on_page_event=lambda msg: self._page_event(panel, msg),
            on_tick=lambda _stats: self._tick(panel),
        )
        panel.stream = stream
        # The pages held for no pointer share one sum of pictures a second (``PanelStream.idle_period``).
        stream.held_count = lambda: self._held_lately(stream)
        # The world a new size asks the page in for its two frames (``PanelStream._let_page_lay_out``).
        stream.world_of = lambda: panel.world
        stream.socket_flight = lambda: self._socket_flight(socket.key)
        # Nothing is cast until the page is the dashboard's: a blank tab has no picture.
        stream.visible = False
        try:
            await stream.start()
            await self._prepare(relay, panel, prefs)
            await relay.send("Page.navigate", {"url": socket.origin + path}, stream.session_id)
        except BaseException:
            await self._close_tab(relay, panel)
            raise
        state.panels[panel_id] = panel
        self._by_handle[panel.handle] = panel
        return panel

    async def _hang_curtain(self, relay: CdpRelay, context_id: str, target_id: str) -> str:
        """Give the panel's window an empty second tab, and return its id (``""`` for none).

        A headless window cannot be hidden: minimized or without focus, its page
        still gets sixty frames a second. A page that is a background tab of its
        own window gets about one. So a panel that is out of view has this tab
        brought in front of it, and the panel's page behind it keeps its socket,
        its timers and its script: it is up to date when it comes back.

        The tab is made right after the panel's own, under the hub's lock, so it
        lands in the panel's window; when the browser says it did not, it is
        closed again and the panel goes without (its pictures still stop).
        """
        curtain = ""
        try:
            curtain = await self.browser.new_page(context_id, new_window=False, background=True)
            if not curtain:
                return ""
            own = await relay.send("Browser.getWindowForTarget", {"targetId": target_id})
            its = await relay.send("Browser.getWindowForTarget", {"targetId": curtain})
            if own.get("windowId") is not None and own.get("windowId") == its.get("windowId"):
                return curtain
        except (CdpError, RelayClosed, asyncio.TimeoutError):
            pass
        if curtain:
            with contextlib.suppress(CdpError, RelayClosed, asyncio.TimeoutError):
                await relay.send("Target.closeTarget", {"targetId": curtain}, quiet=True)
        return ""

    async def _draw_curtain(self, panel: Panel, hidden: bool) -> None:
        """Put the panel's page behind its curtain, or bring it back in front."""
        if not panel.curtain_id or panel.curtained == hidden:
            return
        relay, sid = panel.stream.relay, panel.stream.session_id
        try:
            # A page nobody sees is not told that it has the focus either.
            await relay.send("Emulation.setFocusEmulationEnabled", {"enabled": not hidden}, sid)
            await relay.send(
                "Target.activateTarget",
                {"targetId": panel.curtain_id if hidden else panel.target_id},
            )
            if not hidden:
                # A tab that comes in front gets its window's own size, and the
                # page stays laid out at the panel's: its picture would be the
                # window's shape, with the page in a part of it. The tab is given
                # the page's size again before a picture is asked for.
                await panel.stream.apply_layout()
        except (CdpError, RelayClosed, asyncio.TimeoutError):
            return
        panel.curtained = hidden

    async def _sign_in(self, relay: CdpRelay, state: _SocketState) -> None:
        """Hand the owner's own session to the socket's context, in memory."""
        socket = state.socket
        await relay.send(
            "Storage.setCookies",
            {
                "browserContextId": state.context_id,
                "cookies": [
                    {
                        "name": socket.cookie_name,
                        "value": socket.token,
                        # A host-only cookie of the gateway's own loopback origin.
                        "url": socket.origin + "/",
                        "httpOnly": True,
                        "secure": socket.origin.startswith("https://"),
                        "sameSite": "Lax",
                    }
                ],
            },
        )

    async def _prepare(self, relay: CdpRelay, panel: Panel, prefs: Any) -> None:
        """Everything the tab needs before it loads the dashboard's page."""
        sid = panel.stream.session_id
        tree = await relay.send("Page.getFrameTree", {}, sid)
        panel.main_frame = str(((tree.get("frameTree") or {}).get("frame") or {}).get("id") or "")
        await relay.send("Network.enable", {}, sid)
        await relay.send("Runtime.enable", {}, sid)
        await relay.send(
            "Fetch.enable", {"patterns": [{"urlPattern": "*", "resourceType": "Document"}]}, sid
        )
        # Before the layout: the window's frame is read from a tab that has none yet.
        await panel.stream.fit_window()
        await panel.stream.apply_layout()
        await relay.send("Emulation.setFocusEmulationEnabled", {"enabled": True}, sid)
        # A page that honours it stops its ornaments: a logo that blinks would
        # otherwise cost a whole picture for every step of the blink. And a page
        # that follows the system's colours paints dark: on glasses that add
        # light, dark is clear and a light page is a lamp. The owner's own stored
        # theme, handed over below, still wins.
        await relay.send(
            "Emulation.setEmulatedMedia",
            {
                "features": [
                    {"name": "prefers-reduced-motion", "value": "reduce"},
                    {"name": "prefers-color-scheme", "value": "dark"},
                ]
            },
            sid,
        )
        await relay.send(
            "Runtime.addBinding",
            {"name": panel_page.GUARD_BINDING, "executionContextName": panel_page.PANEL_WORLD},
            sid,
        )
        await relay.send(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": panel_page.guard_script(guarded=panel.guarded),
                "worldName": panel_page.PANEL_WORLD,
                "runImmediately": True,
            },
            sid,
        )
        # A move inside the page to a path off the list is stopped before it happens.
        await relay.send(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": panel_page.move_guard_script(guarded=panel.guarded),
                "worldName": panel_page.PANEL_WORLD,
                "runImmediately": True,
            },
            sid,
        )
        # A window that the page opens is asked of the hub, never opened (``panel_window.py``).
        await relay.send(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": panel_window.OPEN_JS, "runImmediately": True},
            sid,
        )
        await relay.send(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": panel_window.ASK_JS,
                "worldName": panel_page.PANEL_WORLD,
                "runImmediately": True,
            },
            sid,
        )
        # Secret fields are drawn covered in every document of the tab, so that
        # no picture that leaves the browser holds one (``panel_cover.py``).
        await relay.send(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": panel_cover.COVER_JS,
                "worldName": panel_page.PANEL_WORLD,
                "runImmediately": True,
            },
            sid,
        )
        clean = panel_page.clean_prefs(prefs)
        if clean:
            await relay.send(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": panel_page.prefs_script(clean)},
                sid,
            )

    async def close(self, socket_key: int, panel_id: Any) -> None:
        """Close one panel of a socket."""
        async with self._lock:
            state = self._sockets.get(socket_key)
            panel = (
                state.panels.pop(panel_id, None) if state and isinstance(panel_id, str) else None
            )
            if panel is None or state is None:
                return
            await self._retire(panel, CLOSED, "")
            # Its last panel: the context and the sign-in in it go now, not at the socket's end.
            await self._drop_if_empty(state)

    async def close_socket(self, socket_key: int) -> None:
        """The socket is gone: its panels, its context and its sign-in go with it."""
        self._watchers.pop(socket_key, None)
        async with self._lock:
            state = self._sockets.pop(socket_key, None)
            if state is None:
                return
            for panel in list(state.panels.values()):
                await self._retire(panel, CLOSED, "", tell=False)
            state.panels.clear()
            if state.context_id:
                await self.browser.dispose_context(state.context_id)
            # The sign-in was kept for the socket's tabs only.
            state.socket.token = ""
            if not self._sockets:
                await self.browser.stop()

    async def close_all(self) -> None:
        """Gateway shutdown, or the panels were switched off."""
        for key in list(self._sockets):
            await self.close_socket(key)
        await self.browser.stop()

    def ack(self, socket_key: int, panel_id: Any, seq: Any, stream: Any, kind: Any = None) -> bool:
        """The XR page's ack of one picture: ``kind`` is ``decoded`` (the pace) or ``landed`` (seen); none is both."""
        panel = self._panel_of(socket_key, panel_id)
        if panel is None or isinstance(seq, bool) or not isinstance(seq, int):
            return False
        if stream is not None and stream != panel.stream.stream_id:
            return False
        return panel.stream.ack(seq, kind)

    async def set_visible(self, socket_key: int, panel_id: Any, visible: Any) -> None:
        """The panel came into view or left it: a page nobody sees sends no picture."""
        panel = self._panel_of(socket_key, panel_id)
        if panel is None or not isinstance(visible, bool) or panel.status not in (LOADING, LIVE):
            return
        panel.shown = visible
        if visible:
            await self._show(panel)
            return
        # A page out of view is not the one he looks at.
        panel.stream.watched = False
        async with panel.view:
            try:
                await panel.stream.set_visible(False)
            finally:
                # Also when the browser did not answer the stop: the page goes
                # behind its curtain, and the next show asks for a cast anew.
                await self._draw_curtain(panel, True)

    async def _show(self, panel: Panel) -> None:
        """Start the pictures once the tab holds the dashboard's page and the panel is in view.

        A tab that is still swapping in its first document refuses the screencast;
        the next page event tries again, and so does the hub's own look once a
        second (:meth:`_tick`), for a start or a curtain that the browser refused
        after the page's last event.

        One at a time for a panel. A page event shows the panel from its own task,
        and the socket may hide it in the same moment: crossed, the two left a
        curtain drawn back for a page out of view, or a cast with no one to see it.
        """
        stream = panel.stream
        async with panel.view:
            if not panel.shown or not panel.last_url:
                return
            # A cast that is stopped for an idle wait starts again by itself (``PanelStream.holding``).
            if stream.visible and (stream.casting or stream.holding) and not panel.curtained:
                return
            await self._draw_curtain(panel, False)
            if panel.curtained:
                # The browser did not bring the page in front. A cast behind the
                # curtain gets no frame: none is started, and the next look asks.
                return
            stream.visible = False
            try:
                await stream.set_visible(True)
            except (CdpError, RelayClosed, asyncio.TimeoutError):
                stream.visible = False

    async def set_watched(self, socket_key: int, panel_id: Any, on: Any) -> None:
        """The XR page says which page he looks at: that one gets the moving pace with no pointer on it.

        One a socket: watching a page stops the watch on that socket's other pages, so a
        page that names every one costs one fast page. Only a panel of this socket, in
        view and up; anything else does nothing.
        """
        panel = self._panel_of(socket_key, panel_id)
        if panel is None or not isinstance(on, bool) or panel.status not in (LOADING, LIVE):
            return
        if on and not panel.shown:
            return
        if on:
            state = self._sockets.get(socket_key)
            for other in state.panels.values() if state is not None else ():
                if other is not panel:
                    other.stream.watched = False
        if panel.stream.watched == on:
            return
        panel.stream.watched = on
        if on:
            # A held wait that runs now ends at once: the next picture is made at the moving pace.
            panel.stream.hurry()

    async def set_detail(self, socket_key: int, panel_id: Any, texels: Any) -> None:
        panel = self._panel_of(socket_key, panel_id)
        if panel is not None and panel.status in (LOADING, LIVE):
            await panel.stream.set_texels(texels)

    async def resize(self, socket_key: int, panel_id: Any, width: Any, height: Any) -> None:
        panel = self._panel_of(socket_key, panel_id)
        size = css_size(width, height)
        if panel is None or size is None or panel.status not in (LOADING, LIVE):
            return
        if await panel.stream.set_size(*size):
            await self._send(panel.socket, {"op": "resized", "id": panel.id, "css": list(size)})

    # ── input and the three questions (the routes) ─────────────────────────

    async def _owned(self, token: str, handle: Any) -> Panel:
        """The panel *handle* names, when the caller's sign-in is the one that opened it.

        *token* is the access token the sign-in gate verified for this request.
        When it is the opener's own sign-in, renewed (:func:`renews`), the panel
        goes on with it: the socket's tabs are signed in again with the new
        token, in memory, so they do not run out with the old one.
        """
        if not self.enabled():
            raise PanelRefused("off", 403)
        panel = self._by_handle.get(handle) if isinstance(handle, str) else None
        if panel is None:
            raise PanelRefused("unknown_panel", 404)
        socket = panel.socket
        if not hmac.compare_digest(socket.digest, token_digest(token)):
            if not renews(socket.token, token):
                raise PanelRefused("not_your_panel", 403)
            await self._renew(socket, token)
        if panel.status != LIVE:
            raise PanelRefused("not_live", 409)
        return panel

    async def _renew(self, socket: PanelSocket, token: str) -> None:
        """Take the opener's renewed sign-in for *socket* and hand it to its tabs."""
        async with self._lock:
            state = self._sockets.get(socket.key)
            if state is None or hmac.compare_digest(socket.digest, token_digest(token)):
                return
            socket.token = token
            if state.context_id and self.browser.running:
                with contextlib.suppress(CdpError, RelayClosed, asyncio.TimeoutError):
                    await self._sign_in(await self.browser.ensure(socket.origin), state)

    async def input(self, token: str, handle: Any, event: Any) -> dict[str, Any]:
        """One pointer, wheel, key or text event of the owner's hand, in CSS pixels.

        A send that the page's connection cuts (it closed while the tab moved or
        the browser stopped, or did not answer) is answered with what reached the
        page: ``sent`` counts the commands it acknowledged, and ``lost`` says the
        rest was not sent.
        """
        panel = await self._owned(token, handle)
        landed = _Landed()
        try:
            return await self._input(panel, event, landed)
        except _SEND_LOST as exc:
            if isinstance(exc, CdpError):
                # A refused command is most likely a bad command of ours, not a page that went: say so. Only the
                # method and CDP's own message, never the event (input is never logged).
                logger.warning(
                    "xr panel: the page refused %s: %s",
                    exc.method,
                    str(exc.error.get("message", ""))[:200],
                )
            return {"sent": landed.count, "lost": True}

    async def _dispatch(
        self, panel: Panel, event: dict[str, Any], landed: _Landed, *, held: HeldInput | None
    ) -> int:
        """Send one event to the panel's page; the commands that landed are counted on ``landed``."""
        before = landed.count
        landed.held = held
        await dispatch_input(panel.stream.relay, panel.stream.session_id, event, held=landed)
        return landed.count - before

    async def _input(self, panel: Panel, event: Any, landed: _Landed) -> dict[str, Any]:
        if not isinstance(event, dict):
            raise InputEventError("invalid_input_event", "event must be an object")
        kind = event.get("type")
        if kind == "leave":
            await self._set_pointer(panel, False)
            return {"sent": 0}
        # A page for looking at takes no press, key or text; nor does a tab whose
        # path the hub does not know. The answer is the one for a guarded control.
        looking = not panel.last_url or panel_page.look_only(
            panel_page.path_of(panel.last_url), guarded=panel.guarded
        )
        if kind == "text":
            if looking:
                return {"sent": 0, "guarded": True}
            return await self._type(panel, event.get("text"), landed)
        if kind == "key":
            if event.get("key") not in PANEL_KEYS or event.get("modifiers") not in (None, 0):
                raise InputEventError("invalid_input_event", "that key is not a panel key")
            if looking:
                return {"sent": 0, "guarded": True}
            held_back = await self._focus_holds_back(panel)
            if held_back is not None:
                return held_back
            sent = await self._dispatch(
                panel,
                {"type": "key", "action": "press", "key": event["key"]},
                landed,
                held=panel.held,
            )
            panel.stream.note_input()
            return {"sent": sent}
        if kind not in ("mouse", "wheel"):
            raise InputEventError("invalid_input_event", "type must be mouse, wheel, key or text")
        x, y = self._point(panel, event)
        places = self._places(event)
        out: dict[str, Any] = {}
        if kind == "wheel":
            clean: dict[str, Any] = {
                "type": "wheel",
                "x": x,
                "y": y,
                "dx": event.get("dx", 0),
                "dy": event.get("dy", 0),
            }
        else:
            action = event.get("action")
            if action not in ("move", "down", "up", "click"):
                raise InputEventError("invalid_input_event", "unknown mouse action")
            # A release passes only for a button that a press left down before the tab moved here.
            if looking and (action in ("down", "click") or (action == "up" and not panel.held)):
                return {"sent": 0, "guarded": True}
            target = await self._ask(panel, "target", x, y)
            picker = isinstance(target, dict) and target.get("picker") is True
            if action == "move":
                out["target"] = None if target is _UNREAD or picker else target
            elif action != "up" and picker:
                # A list or a picker that the browser draws by itself is open. It is
                # in no moving picture, and a press where a still shows it would be
                # a press on what lies behind it. The press is not sent, and the
                # picker is closed.
                await self._close_picker(panel)
                return {"sent": 0, "picker": True}
            elif action != "up" and target is _UNREAD:
                # The page could not be asked what is under the point: a press is not sent blind.
                return {"sent": 0, "unread": True}
            elif action != "up" and isinstance(target, dict) and target.get("guarded") is True:
                # A control that grants an agent permission: a plain press is not sent.
                return {"sent": 0, "guarded": True, "target": target}
            if action in ("down", "click") and places is not None:
                moved = await self._answers_moved(panel, places)
                if moved is not None:
                    return moved
            if action in ("down", "click") and self._stale(event, target):
                # Another control stands at the press's place, or the same one stands
                # elsewhere, than in the picture he aimed at: the press is not sent. A
                # change anywhere else on the page never voids a press.
                return {"sent": 0, "stale": True, "target": target}
            held = event.get("held") is True
            clean = {"type": "mouse", "action": action, "x": x, "y": y}
            clean["button"] = "left" if action != "move" or held else "none"
        await self._set_pointer(panel, True)
        out["sent"] = await self._dispatch(panel, clean, landed, held=panel.held)
        if kind == "wheel" or clean.get("action") != "move":
            panel.stream.note_input()
        if kind == "mouse" and clean["action"] in ("up", "click"):
            focus = await self._ask(panel, "focus")
            out["focus"] = None if focus is _UNREAD else focus
        return out

    @staticmethod
    def _stale(event: Mapping[str, Any], target: Any) -> bool:
        """Whether a press's ``aimed`` box (what the picture showed at its place) is not what stands there now.

        ``aimed`` missing: the XR page did not see (a finger with no move before it), and the press goes as before.
        ``aimed`` ``None``: no control stood there. A box is four finite numbers, then the control's label when it
        has one (a string of at most ``AIMED_LABEL_MAX``); it is the same control when each side lies within
        ``STALE_PX`` of the one that stands there now and, when both have a label, the labels are the same (two
        controls of one size that swap places). A control with no label is judged by its box.
        """
        if "aimed" not in event:
            return False
        aimed = event.get("aimed")
        if aimed is not None and (
            not isinstance(aimed, list)
            or len(aimed) not in (4, 5)
            or not all(
                isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                for v in aimed[:4]
            )
            or (
                len(aimed) == 5
                and not (isinstance(aimed[4], str) and len(aimed[4]) <= AIMED_LABEL_MAX)
            )
        ):
            raise InputEventError(
                "invalid_input_event", "aimed must be four numbers and an optional label, or null"
            )
        rect = target.get("rect") if isinstance(target, dict) else None
        now = rect if isinstance(rect, list) and len(rect) == 4 else None
        if aimed is None or now is None:
            return (aimed is None) != (now is None)
        if any(abs(float(a) - float(b)) > STALE_PX for a, b in zip(aimed[:4], now)):
            return True
        seen = aimed[4] if len(aimed) == 5 else ""
        label = target.get("label") if isinstance(target, dict) else None
        here = label[:AIMED_LABEL_MAX] if isinstance(label, str) else ""
        return bool(seen) and bool(here) and seen != here

    @staticmethod
    def _places(event: Mapping[str, Any]) -> list[list[float]] | None:
        """Where the XR page last saw the controls that answer an ask, or ``None`` for a press that carries no such word."""
        raw = event.get("grants")
        if raw is None:
            return None
        if not isinstance(raw, list) or len(raw) > 40:
            raise InputEventError(
                "invalid_input_event", "grants must be a list of at most 40 places"
            )
        for place in raw:
            if (
                not isinstance(place, list)
                or len(place) != 4
                or not all(
                    isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                    for v in place
                )
            ):
                raise InputEventError("invalid_input_event", "a place is four numbers")
        return raw

    async def _answers_moved(
        self, panel: Panel, places: list[list[float]]
    ) -> dict[str, Any] | None:
        """``None`` when the page still draws the controls that answer an ask where the press says, else the answer.

        The XR page holds a press on an ask's panel back in the second after those
        controls came, went or moved (`asks.ts`, `pressFate`). The press reaches
        the page one round trip after the XR page last heard where they are, and
        a card that the page drew in between would stand under it: the hub looks
        once more right before it sends. A page that cannot be asked is not sent
        a press blind.
        """
        state = await self._ask(panel, "state")
        now = state.get("grants") if isinstance(state, dict) else None
        if not isinstance(now, list):
            return {"sent": 0, "unread": True}
        if now != places:
            return {"sent": 0, "moved": True, "grants": now}
        return None

    async def _close_picker(self, panel: Panel) -> None:
        """Close a picker that the browser draws by itself, with nothing for the page to hear.

        A key down of Escape goes to the picker and not to the page: the page
        hears no key and no click, so a dialog that the control stands in stays
        open. No key up follows: the picker is gone by then, and the page would
        hear that one. No picture changes: a picker is in none
        (``PanelStream._take_still``).
        """
        with contextlib.suppress(CdpError, RelayClosed, asyncio.TimeoutError):
            await panel.stream.relay.send(
                "Input.dispatchKeyEvent",
                {
                    "type": "rawKeyDown",
                    "key": "Escape",
                    "code": "Escape",
                    "windowsVirtualKeyCode": 27,
                },
                panel.stream.session_id,
            )

    async def ask(self, token: str, handle: Any, question: Any, x: Any = 0, y: Any = 0) -> Any:
        """One of the three fixed questions about the page: target, focus, state."""
        panel = await self._owned(token, handle)
        if question not in ("target", "focus", "state"):
            raise PanelRefused("unknown_question", 400)
        if question == "target":
            x, y = self._point(panel, {"x": x, "y": y})
        answer = await self._ask(panel, question, x, y)
        return None if answer is _UNREAD else answer

    def _point(self, panel: Panel, event: Mapping[str, Any]) -> tuple[int, int]:
        out: list[int] = []
        for name, limit in (("x", panel.stream.css_w), ("y", panel.stream.css_h)):
            value = event.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise InputEventError("invalid_input_event", f"{name} must be a number")
            if not math.isfinite(value) or value < 0 or value > limit:
                raise InputEventError("invalid_input_event", f"{name} is off the page")
            out.append(int(value))
        return out[0], out[1]

    async def _type(self, panel: Panel, text: Any, landed: _Landed) -> dict[str, Any]:
        if not isinstance(text, str) or not text or len(text) > MAX_TEXT_CHARS:
            raise InputEventError("invalid_input_event", "text must be a short string")
        sent = 0
        for start in range(0, len(text), _TEXT_PIECE):
            # Asked before every piece: a surface that an agent raises can take
            # the focus by itself between two pieces of one text.
            held_back = await self._focus_holds_back(panel)
            if held_back is not None:
                if sent:
                    panel.stream.note_input()
                return {**held_back, "sent": sent}
            piece = text[start : start + _TEXT_PIECE]
            sent += await self._dispatch(panel, {"type": "key", "text": piece}, landed, held=None)
        panel.stream.note_input()
        return {"sent": sent}

    async def _focus_holds_back(self, panel: Panel) -> dict[str, Any] | None:
        """The answer when text or a key must not be sent, else ``None``.

        Text and keys go to whatever holds the page's focus. So nothing is sent
        while the focus is inside a guarded surface, on a field as much as on a
        button: text typed into a field of a surface that an agent raised is an
        answer to it, and text fires no key event for the page's own guard to
        stop. The page says whether the focused element is guarded, by the same
        walk as its guard (the nearest mark wins, so a control marked as
        granting nothing takes keys). This is the hub's own check: when the page
        cannot be asked, nothing is sent either.
        """
        focus = await self._ask(panel, "focus")
        if not isinstance(focus, dict) or not isinstance(focus.get("guarded"), bool):
            return {"sent": 0, "unread": True}
        if focus["guarded"]:
            return {"sent": 0, "guarded": True}
        return None

    async def _ask(self, panel: Panel, question: str, x: float = 0, y: float = 0) -> Any:
        """The page's answer to one fixed question, or ``_UNREAD`` when it could not be asked."""
        expression = panel_page.question_js(question, x, y)
        if expression is None or panel.world is None:
            return _UNREAD
        answer = await self._eval(panel, expression)
        # ``False`` is the expression's word for "the reader is not in this page".
        return _UNREAD if answer is False else answer

    async def _eval(self, panel: Panel, expression: str) -> Any:
        try:
            reply = await panel.stream.relay.send(
                "Runtime.evaluate",
                {"expression": expression, "contextId": panel.world, "returnByValue": True},
                panel.stream.session_id,
            )
        except (CdpError, RelayClosed, asyncio.TimeoutError):
            return _UNREAD
        if not isinstance(reply, dict) or reply.get("exceptionDetails") is not None:
            return _UNREAD
        value = (reply.get("result") or {}).get("value")
        return value if isinstance(value, (dict, bool)) else None

    async def _set_pointer(self, panel: Panel, on: bool) -> None:
        """A page no pointer is on holds its text caret still, so it paints nothing."""
        if panel.pointer_on == on:
            return
        panel.pointer_on = on
        panel.stream.idle = not on
        if on:
            panel.stream.hurry()
        if panel.world is not None:
            await self._eval(panel, panel_page.caret_js(not on))

    # ── what the page does ─────────────────────────────────────────────────

    async def _page_event(self, panel: Panel, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        params = msg.get("params") or {}
        sid = panel.stream.session_id
        relay = panel.stream.relay
        if method == "Fetch.requestPaused":
            await self._paused(panel, params, msg.get("sessionId") or sid)
            return
        if method == "Page.javascriptDialogOpening":
            await self._dialog(panel, msg.get("sessionId") or sid)
            return
        if msg.get("sessionId") not in (None, sid):
            return
        if method == "Network.responseReceived":
            headers = (params.get("response") or {}).get("headers") or {}
            refused = any(
                str(name).lower() == _AUTH_REQUIRED_HEADER and str(value).lower() == "true"
                for name, value in headers.items()
            )
            if refused and panel.status in (LOADING, LIVE):
                # The sign-in gate refused the tab: no picture of anything follows.
                await self._fail(panel, SIGNED_OUT, "gate_refused")
        elif method == "Runtime.bindingCalled":
            if params.get("name") == panel_page.GUARD_BINDING:
                asked = panel_window.window_url(params.get("payload"))
                if asked is not None:
                    await self._window(panel, asked)
                elif params.get("payload") == panel_page.MOVE_STOPPED:
                    # A link or a tab of the page led off the list, and the page stayed.
                    await self._send(
                        panel.socket, {"op": "refused", "id": panel.id, "why": "address"}
                    )
                else:
                    await self._send(panel.socket, {"op": "guarded", "id": panel.id})
        elif method == "Runtime.executionContextCreated":
            context = params.get("context") or {}
            aux = context.get("auxData") or {}
            if (
                context.get("name") == panel_page.PANEL_WORLD
                and aux.get("frameId") == panel.main_frame
            ):
                panel.world = context.get("id") if isinstance(context.get("id"), int) else None
                if panel.world is not None:
                    await self._eval(panel, panel_page.caret_js(not panel.pointer_on))
        elif method == "Runtime.executionContextDestroyed":
            if params.get("executionContextId") == panel.world:
                panel.world = None
        elif method == "Runtime.executionContextsCleared":
            panel.world = None
        elif method == "Page.navigatedWithinDocument":
            if params.get("frameId") == panel.main_frame:
                await self._moved(panel, relay, params.get("url"))
        elif method == "Page.frameNavigated":
            frame = params.get("frame") or {}
            if frame.get("id") == panel.main_frame and not frame.get("parentId"):
                url = frame.get("url")
                if self._may_show(panel, url):
                    await self._stands_at(panel, url)
                    await self._show(panel)
                elif panel.status in (LOADING, LIVE):
                    await self._fail(panel, FAILED, "address")
        elif method == "Page.loadEventFired":
            if panel.status == LOADING:
                panel.status = LIVE
                await self._show(panel)
                await self._tell(panel.socket, panel.id, LIVE, "")
        elif method in ("Inspector.targetCrashed", "Inspector.detached"):
            if panel.status in (LOADING, LIVE):
                await self._fail(panel, FAILED, "tab_gone")

    async def _paused(self, panel: Panel, params: Mapping[str, Any], sid: str) -> None:
        """A document request of the tab: continue it only for an address a panel may show.

        The tab's own document must be one of the dashboard's pages. A frame's
        document must be the gateway's own or of the page's own making: the
        browser's fence fails any other request by itself (``panel_fence.py``),
        and this is the second net for a document. Neither loads an address
        whose query is refused (``panel_page.query_refused``): the panel says
        its line.
        """
        request_id = params.get("requestId")
        url = (params.get("request") or {}).get("url")
        main = params.get("frameId") == panel.main_frame and sid == panel.stream.session_id
        relay = panel.stream.relay
        origin = panel.socket.origin
        guarded = panel.guarded
        listed = (
            panel_page.allowed_url(url, origin, guarded=guarded)
            if main
            else panel_fence.passes(url, origin)
        )
        # A page of the page's own making (``about:``, ``blob:``, ``data:``) has no such query.
        launch = (
            listed
            and panel_page.origin_of(url) == origin
            and panel_page.query_refused(url, guarded=guarded)
        )
        if listed and not launch:
            with contextlib.suppress(CdpError, RelayClosed, asyncio.TimeoutError):
                await relay.send("Fetch.continueRequest", {"requestId": request_id}, sid)
            return
        with contextlib.suppress(CdpError, RelayClosed, asyncio.TimeoutError):
            await relay.send(
                "Fetch.failRequest",
                {"requestId": request_id, "errorReason": "BlockedByClient"},
                sid,
            )
        if main or launch:
            await self._send(panel.socket, {"op": "refused", "id": panel.id, "why": "address"})

    async def _dialog(self, panel: Panel, sid: str) -> None:
        """A browser dialog of the tab: answered with a no at once, and the panel says so.

        Nobody is at this browser to answer an ``alert``, a ``confirm``, a
        ``prompt`` or a question before leaving, and a page waits on one for
        ever. A no is the closed answer: a ``confirm`` in front of a delete
        then deletes nothing.
        """
        with contextlib.suppress(CdpError, RelayClosed, asyncio.TimeoutError):
            await panel.stream.relay.send("Page.handleJavaScriptDialog", {"accept": False}, sid)
        await self._send(panel.socket, {"op": "refused", "id": panel.id, "why": "dialog"})

    async def _window(self, panel: Panel, url: str) -> None:
        """A window that the page asked for: the XR page opens it on a panel of its own, by the same rule as an open.

        An address that the panel may not show (another origin, a shut path,
        a refused query) opens nothing, and the panel says its line.
        """
        if self._may_show(panel, url):
            path = panel_page.address_of(url)
            await self._send(panel.socket, {"op": "window", "id": panel.id, "path": path})
        else:
            await self._send(panel.socket, {"op": "refused", "id": panel.id, "why": "window"})

    async def _stray(self, info: Mapping[str, Any]) -> None:
        """A page opened a window; the browser closed it before it ran. Its panel says one line."""
        context_id = info.get("browserContextId")
        for state in self._sockets.values():
            if state.context_id != context_id or not state.panels:
                continue
            panels = list(state.panels.values())
            opener = next((p for p in panels if p.target_id == info.get("openerId")), panels[0])
            await self._send(opener.socket, {"op": "refused", "id": opener.id, "why": "window"})
            return

    async def _stray_closed(self, info: Mapping[str, Any]) -> None:
        """The window that a page opened is closed: its opener's tab is in front again."""
        context_id = info.get("browserContextId")
        for state in self._sockets.values():
            if state.context_id != context_id:
                continue
            panels = list(state.panels.values())
            opener = [p for p in panels if p.target_id == info.get("openerId")]
            await self._lay_out_again(opener or panels)
            return

    async def _lay_out_again(self, panels: Iterable[Panel]) -> None:
        """Give each page that may have come in front again its size back, as after a show.

        A window that a page opens is a tab of the page's own window and comes
        in front of it. When it is closed the page's tab comes in front again,
        and takes its window's own view: the second way to a picture that was
        not made from a view of the page's size (``_draw_curtain`` has the first).
        """
        for panel in panels:
            if panel.curtained or panel.status not in (LOADING, LIVE):
                continue
            with contextlib.suppress(CdpError, RelayClosed, asyncio.TimeoutError, OSError):
                await panel.stream.fit_window()
                await panel.stream.apply_layout()

    async def _moved(self, panel: Panel, relay: Any, url: Any) -> None:
        """The page changed its own address without a load: keep it on the list.

        The second net. The page's own move guard stops a move to a path off
        the list, or to an address whose query is refused, before it happens (``panel_page.MOVE_JS``); one that is seen here all
        the same has happened, and is undone.
        """
        if self._may_show(panel, url):
            await self._stands_at(panel, url)
            return
        with contextlib.suppress(CdpError, RelayClosed, asyncio.TimeoutError):
            await relay.send(
                "Runtime.evaluate", {"expression": "history.back()"}, panel.stream.session_id
            )
        await self._send(panel.socket, {"op": "refused", "id": panel.id, "why": "address"})

    def _may_show(self, panel: Panel, url: Any) -> bool:
        """Whether the panel's tab may stand at *url*, in the way the panel was opened in."""
        return panel_page.allowed_url(
            url, panel.socket.origin, guarded=panel.guarded
        ) and not panel_page.query_refused(url, guarded=panel.guarded)

    async def _stands_at(self, panel: Panel, url: Any) -> None:
        """The tab shows *url* now: tell the panel its path, the Bot a Bots page names, and nothing else of it."""
        was = panel_page.address_of(panel.last_url)
        panel.last_url = str(url)
        now = panel_page.address_of(url)
        if now and now != was:
            await self._send(panel.socket, {"op": "path", "id": panel.id, "path": now})

    async def _tick(self, panel: Panel) -> None:
        """Once a second: the settings still hold, and the tab opened no window of its own."""
        if not self.enabled():
            asyncio.ensure_future(self.close_all())
            return
        if self.guarded() != panel.guarded:
            # The guard changed: a tab's in-page scripts are of the way it was opened in.
            asyncio.ensure_future(self._way_changed())
            return
        if panel.shown and not panel.view.locked():
            # A panel in view whose pictures did not start is asked for again.
            await self._show(panel)
        # A tab that is being opened is in the browser before it is on the hub's
        # list. While one is, this second's look is skipped: the new tab would be
        # closed as a window that a page opened. The look itself holds the lock,
        # so no tab is opened between the browser's answer and the close.
        if self._lock.locked():
            return
        async with self._lock:
            await self._close_windows_of_its_own(panel)

    async def _close_windows_of_its_own(self, panel: Panel) -> None:
        """Close every page of the socket's context that is not one of its panels."""
        state = self._sockets.get(panel.socket.key)

        if state is None or not state.context_id:
            return
        relay = panel.stream.relay
        try:
            listed = await relay.send("Target.getTargets")
        except (CdpError, RelayClosed, asyncio.TimeoutError):
            return
        known = {p.target_id for p in state.panels.values()}
        known |= {p.curtain_id for p in state.panels.values() if p.curtain_id}
        closed = False
        for info in listed.get("targetInfos") or []:
            if info.get("browserContextId") != state.context_id or info.get("type") != "page":
                continue
            if info.get("targetId") in known:
                continue
            with contextlib.suppress(CdpError, RelayClosed, asyncio.TimeoutError):
                await relay.send("Target.closeTarget", {"targetId": info.get("targetId")})
                closed = True
        if closed:
            await self._lay_out_again(state.panels.values())

    # ── plumbing ───────────────────────────────────────────────────────────

    def _held_lately(self, asking: Any) -> int:
        """How many held pages of every socket sent a picture in the last second, the one that asks among them."""
        now = asking._clock()
        held = 0
        for state in self._sockets.values():
            for panel in state.panels.values():
                stream = panel.stream
                if stream is asking or (
                    stream is not None
                    and getattr(stream, "sent_lately", None)
                    and stream.sent_lately(now)
                ):
                    held += 1
        return held

    def _socket_flight(self, socket_key: int) -> int:
        """The picture bytes on their way to the XR page on one owner socket, every panel's."""
        state = self._sockets.get(socket_key)
        if state is None:
            return 0
        total = 0
        for panel in state.panels.values():
            flight_bytes = getattr(panel.stream, "flight_bytes", None)
            if callable(flight_bytes):
                total += flight_bytes()
        return total

    def _panel_of(self, socket_key: int, panel_id: Any) -> Panel | None:
        state = self._sockets.get(socket_key)
        if state is None or not isinstance(panel_id, str):
            return None
        return state.panels.get(panel_id)

    async def _fail(self, panel: Panel, status: str, why: str) -> None:
        """Stop a panel's pictures and input for good, and say why."""
        async with self._lock:
            state = self._sockets.get(panel.socket.key)
            if state is not None:
                state.panels.pop(panel.id, None)
            await self._retire(panel, status, why)
            if state is not None:
                await self._drop_if_empty(state)

    async def _retire(self, panel: Panel, status: str, why: str, *, tell: bool = True) -> None:
        panel.status = status
        panel.stream.watched = False
        self._by_handle.pop(panel.handle, None)
        await self._close_tab(panel.stream.relay, panel)
        if tell:
            await self._tell(panel.socket, panel.id, status, why)

    async def _close_tab(self, relay: Any, panel: Panel) -> None:
        with contextlib.suppress(Exception):
            await panel.stream.stop()
        with contextlib.suppress(Exception):
            await relay.send("Target.closeTarget", {"targetId": panel.target_id}, quiet=True)
        if panel.curtain_id:
            with contextlib.suppress(Exception):
                await relay.send("Target.closeTarget", {"targetId": panel.curtain_id}, quiet=True)

    async def _drop_if_empty(self, state: _SocketState) -> None:
        if state.panels:
            return
        self._sockets.pop(state.socket.key, None)
        if state.context_id:
            await self.browser.dispose_context(state.context_id)
        if not self._sockets:
            await self.browser.stop()

    async def _tell(self, socket: PanelSocket, panel_id: str, status: str, why: str) -> None:
        await self._send(socket, {"op": "status", "id": panel_id, "status": status, "why": why})

    async def _send(self, socket: PanelSocket, data: dict[str, Any]) -> None:
        try:
            await socket.send_json({"type": PANEL_MESSAGE_TYPE, "data": data})
        except Exception:  # noqa: BLE001 - a socket that went away is closed by its own handler
            logger.debug("xr panel: socket send failed", exc_info=True)


async def _browse_denied() -> bool:
    return await browse_governance.browse_denied_async(browse_governance.HOST_KEY)


_HUB: PanelHub | None = None


def get_panel_hub(home: Path | None = None) -> PanelHub:
    """The process-wide hub. Making it starts nothing: the browser starts on a first open."""
    global _HUB
    if _HUB is None:
        if home is None:
            from kiro_crew.config.paths import config_dir

            home = config_dir()
        _HUB = PanelHub(PanelBrowser(home))
    return _HUB


def reset_panel_hub() -> None:
    """Forget the hub (tests)."""
    global _HUB
    _HUB = None


__all__ = [
    "CLOSED",
    "FAILED",
    "LIVE",
    "LOADING",
    "MAX_PANELS",
    "MAX_PANELS_PER_SOCKET",
    "PANEL_KEYS",
    "PANEL_MESSAGE_TYPE",
    "Panel",
    "PanelHub",
    "PanelRefused",
    "PanelSocket",
    "SIGNED_OUT",
    "get_panel_hub",
    "reset_panel_hub",
    "token_digest",
]
