"""The gateway's own Chromium for panel tabs: one the agent has no way into.

A panel tab is signed in as the dashboard's owner, so nothing an agent holds
may reach it. This browser is therefore NOT the one the supervisor runs for the
agent's ``browser`` tool:

* It is a second process, started by the same launcher
  (``supervisor.launch_chromium_pipe``), and no debugging port is opened. Its
  CDP rides two socket pairs, not two pipes: a pipe of the browser can be
  opened again through ``/proc/<pid>/fd`` by any process of the same user, an
  agent's shell among them, and a socket cannot.
* It is fenced to this gateway (``panel_fence.py``): a request to any other
  address fails inside the browser, whatever asks for it.
* A window that a page opens is held before it runs anything and closed
  (:meth:`PanelBrowser.new_page` makes the only pages that are let go on).
* Its relay serves nothing (``CdpRelay.start(..., serve=False)``): no socket is
  bound and no secret-bearing URL exists, so the Harness daemon, the helper
  worker and every op of the agent's tool have no connection to it. Only this
  module and ``panel_hub.py`` hold the relay object.
* Each owner socket gets its own incognito browser context
  (:meth:`PanelBrowser.new_context`). The owner's session cookie is set there
  with ``Storage.setCookies``, in memory, and disposing the context drops it.
  Nothing of it is written to the profile, which is a temporary directory that
  is removed when the browser stops.

Where the pipe cannot be handed to a child (Windows) Chromium would need a
debugging port any local process can reach, so there the browser refuses to
start (:class:`PanelUnavailable`) and the panel fails closed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import socket
import sys
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat

from . import install, panel_fence
from .relay import CdpError, CdpRelay, RelayClosed
from .supervisor import chromium_argv, launch_chromium_pipe, spawn_child, stop_child

logger = logging.getLogger(__name__)

#: The tab is laid out at twice the CSS size, so a picture can carry two pixels
#: for each CSS pixel. A ratio set on the tab alone does not reach the screencast.
DEVICE_SCALE_ARG = "--force-device-scale-factor=2"
#: A wheel turned sideways past a page's edge is not a step back in the tab's
#: history. Nobody sees the browser's own sign of that step in a panel, and the
#: page that was there would be gone from under the hand.
NO_SWIPE_BACK_ARG = "--disable-features=OverscrollHistoryNavigation"
_OWN_ARGS = (DEVICE_SCALE_ARG, NO_SWIPE_BACK_ARG)
_PROFILE_PREFIX = "kirocrew-panels-"
#: Where the host says whether one process may attach to another of the same user.
PTRACE_SCOPE_FILE = Path("/proc/sys/kernel/yama/ptrace_scope")
#: What the panel is told when the host lets processes attach to each other, and
#: when the host does not say.
HOST_ATTACH_OPEN = "host_attach_open"
HOST_ATTACH_UNKNOWN = "host_attach_unknown"
#: How long the browser may take to say that a page the gateway made is attached.
_ATTACH_TIMEOUT_S = 10.0
#: How long one step of closing a window that a page opened may take.
_STRAY_STEP_S = 3.0


class PanelUnavailable(RuntimeError):
    """The panel browser cannot run here; ``code`` is what the panel is told."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code


def _socket_pair() -> tuple[int, int]:
    """Two connected descriptors, as ``os.pipe`` gives, of a unix socket pair (POSIX)."""
    one, other = socket.socketpair()
    return one.detach(), other.detach()


def host_attach_refusal() -> str | None:
    """Why this host is not one to run the panel browser on, or ``None`` when it is.

    The browser is signed in as the owner, and its debugging transport is two
    socket pairs that nothing can open again. A process that may attach to
    another process of the same user can still copy a descriptor out of the
    browser or the gateway (``pidfd_getfd``), and an agent's command is such a
    process where ``kernel.yama.ptrace_scope`` is 0. So the host must say that
    it does not allow that: 0, a file that cannot be read, and a host that is
    not Linux are all refused.
    """
    if not sys.platform.startswith("linux"):
        return HOST_ATTACH_UNKNOWN
    try:
        scope = int(PTRACE_SCOPE_FILE.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return HOST_ATTACH_UNKNOWN
    return HOST_ATTACH_OPEN if scope == 0 else None


def _accepts_attach() -> bool:
    """``browser.xr_pages_accept_same_user_attach`` as the gateway holds it now; unreadable is off."""
    try:
        from kiro_crew.config import live as live_config

        snap = live_config.snapshot()
    except Exception:  # noqa: BLE001 - a config that cannot be read accepts nothing
        return False
    browser = getattr(snap, "browser", None)
    return getattr(browser, "xr_pages_accept_same_user_attach", False) is True


class PanelBrowser:
    """One Chromium for every panel tab of this gateway, started on first use."""

    def __init__(self, home: Path, *, accepts_attach: Callable[[], bool] = _accepts_attach) -> None:
        self.home = home
        #: The owner's own word that the browser may start on a host that lets
        #: processes of one user attach to each other. A setting, off by default.
        self._accepts_attach = accepts_attach
        self._lock = asyncio.Lock()
        self._relay: CdpRelay | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._profile: Path | None = None
        #: The gateway origin the running browser is fenced to.
        self._origin = ""
        #: Told of each window that a page opened and that was closed for it:
        #: the browser's own description of the window (``targetInfo``).
        self.on_stray: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        #: Told of the same window once it is closed: the tab it stood in front
        #: of is in front again by then.
        self.on_stray_closed: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        #: Pages the browser attached and holds at their start: target id to the
        #: session it is held on and the browser's description of it.
        self._held: dict[str, tuple[str, dict[str, Any]]] = {}
        #: The page the gateway is making now, when the browser has not named it yet.
        self._making = 0
        #: The pages the gateway made itself (:meth:`new_page`).
        self._own: set[str] = set()
        self._awaited: dict[str, asyncio.Future[str]] = {}

    @property
    def running(self) -> bool:
        return self._relay is not None and not self._relay.closed

    async def ensure(self, origin: str = "") -> CdpRelay:
        """The relay of the running browser, starting it first when needed.

        *origin* is the gateway's own origin, which the browser is fenced to. A
        browser that runs for another origin is not handed out, and the panel
        fails closed with a reason of its own: a socket that came in on the
        gateway's other loopback address asks for one. Its pages cannot be
        shown at the origin the browser runs for either, since the sign-in
        gate pins its session to the address it came from.
        """
        async with self._lock:
            if self._relay is not None and not self._relay.closed:
                if origin and origin != self._origin:
                    raise PanelUnavailable(
                        "other_address", "the panel browser is fenced to another origin"
                    )
                return self._relay
            await self._teardown()
            return await self._start(origin)

    async def _start(self, origin: str) -> CdpRelay:
        if platform_compat.IS_WINDOWS:
            raise PanelUnavailable("unsupported_host", "panels need the debugging pipe")
        refusal = host_attach_refusal()
        if refusal is not None and not self._accepts_attach():
            raise PanelUnavailable(refusal, "this host lets a process attach to another")
        try:
            fence = panel_fence.launch_flags(origin)
        except panel_fence.FenceError as exc:
            raise PanelUnavailable("address", str(exc)) from exc
        state = await asyncio.to_thread(install.install_state, self.home)
        exe = state.chromium.executable if state.chromium.present else None
        if exe is None:
            raise PanelUnavailable("no_browser", "Kiro's Chromium is not downloaded")
        profile = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix=_PROFILE_PREFIX))
        argv = [*chromium_argv(exe, profile, headless=True), *_OWN_ARGS, *fence]

        async def spawn(sh_argv: list[str], **kwargs: Any) -> asyncio.subprocess.Process:
            return await spawn_child(sh_argv, env=dict(os.environ), **kwargs)

        relay = CdpRelay()
        proc: asyncio.subprocess.Process | None = None
        stderr = bytearray()
        try:
            proc, upstream, stderr = await launch_chromium_pipe(argv, spawn, pair=_socket_pair)
            await relay.start(upstream, serve=False)
            await relay.send("Browser.getVersion")
            # Every new page is attached and held at its start, before it runs
            # anything: one that the gateway did not make is a window a page opened.
            relay.on_browser_event = self._browser_event
            await relay.send(
                "Target.setAutoAttach",
                {
                    "autoAttach": True,
                    "waitForDebuggerOnStart": True,
                    "flatten": True,
                    "filter": [{"type": "page", "exclude": False}],
                },
            )
        except BaseException:
            # The loader's or the browser's own last words; its argv holds no secret.
            logger.debug("panel browser did not start: %s", bytes(stderr[-600:]))
            await relay.close()
            await stop_child(proc)
            await asyncio.to_thread(shutil.rmtree, profile, True)
            raise
        self._relay, self._proc, self._profile, self._origin = relay, proc, profile, origin
        return relay

    async def new_page(self, context_id: str, *, new_window: bool, background: bool = False) -> str:
        """Make an empty page in *context_id* and let it go on; return its target id.

        The only pages of this browser that run are the ones made here. The
        hub makes one page at a time (under its lock), so while this runs a
        page the browser attaches is either this one or a window that a page
        opened in the same moment: both wait, and the stranger is closed after.
        """
        relay = await self.ensure()
        self._making += 1
        try:
            made = await relay.send(
                "Target.createTarget",
                {
                    "url": "about:blank",
                    "browserContextId": context_id,
                    "newWindow": new_window,
                    "background": background,
                },
            )
            target_id = str(made.get("targetId") or "")
            if not target_id:
                return ""
            held = self._held.pop(target_id, None)
            session_id = held[0] if held else ""
            if not session_id:
                waiter: asyncio.Future[str] = asyncio.get_running_loop().create_future()
                self._awaited[target_id] = waiter
                try:
                    session_id = await asyncio.wait_for(waiter, _ATTACH_TIMEOUT_S)
                finally:
                    self._awaited.pop(target_id, None)
            self._own.add(target_id)
            await relay.send("Runtime.runIfWaitingForDebugger", {}, session_id)
            return target_id
        finally:
            self._making -= 1
            if not self._making:
                for session, info in list(self._held.values()):
                    await self._close_stray(relay, info, session)

    async def _browser_event(self, msg: dict[str, Any]) -> None:
        """A page the browser attached by itself: the gateway's own, or a window to close."""
        if msg.get("method") != "Target.attachedToTarget" or msg.get("sessionId"):
            return
        params = msg.get("params") or {}
        info = params.get("targetInfo") or {}
        target_id, session_id = info.get("targetId"), params.get("sessionId")
        if info.get("type") != "page" or not isinstance(target_id, str) or not target_id:
            return
        if not isinstance(session_id, str) or not session_id:
            return
        # A session that the gateway attached itself to one of its own pages is
        # told here too. Any other page is new: it waits at its start when the
        # browser could hold it, and is a stranger either way.
        if target_id in self._own:
            return
        waiter = self._awaited.get(target_id)
        if waiter is not None and not waiter.done():
            waiter.set_result(session_id)
            return
        self._held[target_id] = (session_id, info)
        relay = self._relay
        if not self._making and relay is not None:
            await self._close_stray(relay, info, session_id)

    async def _close_stray(self, relay: CdpRelay, info: dict[str, Any], session_id: str) -> None:
        """Close a window that a page opened. It was held at its start, so it ran nothing.

        A window opened by script shares its opener's process, and while it
        waits at its start the opener waits with it. So it is let go, but only
        after every request it could make has been set to wait for an answer
        that never comes; it is closed in the same breath.
        """
        target_id = info.get("targetId")
        self._held.pop(str(target_id), None)
        if self.on_stray is not None and info.get("browserContextId"):
            try:
                await self.on_stray(info)
            except Exception:  # noqa: BLE001 - telling the panel never undoes the close
                logger.debug("panel browser: on_stray failed", exc_info=True)

        async def step(method: str, params: dict[str, Any], session: str | None = None) -> None:
            # A window that waits at its start may not answer; it is not waited on for long.
            with contextlib.suppress(CdpError, RelayClosed, asyncio.TimeoutError):
                await asyncio.wait_for(
                    relay.send(method, params, session, quiet=True), _STRAY_STEP_S
                )

        await step("Fetch.enable", {"patterns": [{"urlPattern": "*"}]}, session_id)
        closing = asyncio.ensure_future(step("Target.closeTarget", {"targetId": target_id}))
        await step("Runtime.runIfWaitingForDebugger", {}, session_id)
        await closing
        if self.on_stray_closed is not None and info.get("browserContextId"):
            try:
                await self.on_stray_closed(info)
            except Exception:  # noqa: BLE001 - what follows a close never undoes it
                logger.debug("panel browser: on_stray_closed failed", exc_info=True)

    async def new_context(self) -> str:
        """A fresh incognito context: its cookies and storage live in memory only."""
        relay = await self.ensure()
        made = await relay.send("Target.createBrowserContext", {"disposeOnDetach": False})
        context_id = made.get("browserContextId")
        if not isinstance(context_id, str) or not context_id:
            raise PanelUnavailable("no_context", "the browser made no context")
        # A panel never saves a file to the gateway's host.
        await relay.send(
            "Browser.setDownloadBehavior", {"behavior": "deny", "browserContextId": context_id}
        )
        return context_id

    async def dispose_context(self, context_id: str) -> None:
        """Close every tab of *context_id* and forget its cookies. Never raises."""
        relay = self._relay
        if relay is None or relay.closed:
            return
        with contextlib.suppress(Exception):
            await relay.send(
                "Target.disposeBrowserContext", {"browserContextId": context_id}, quiet=True
            )

    async def stop(self) -> None:
        """Stop the browser and remove its profile. Idempotent."""
        async with self._lock:
            await self._teardown()

    async def _teardown(self) -> None:
        relay, proc, profile = self._relay, self._proc, self._profile
        self._relay = self._proc = self._profile = None
        self._origin = ""
        self._held.clear()
        self._own.clear()
        if relay is not None:
            with contextlib.suppress(RelayClosed, Exception):
                await relay.close()
        await stop_child(proc)
        if profile is not None:
            await asyncio.to_thread(shutil.rmtree, profile, True)


__all__ = ["DEVICE_SCALE_ARG", "PanelBrowser", "PanelUnavailable"]
