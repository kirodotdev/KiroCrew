"""The gateway-owned lifecycle of browser, relay and Harness daemon.

One :class:`HarnessSupervisor` per gateway (:func:`get_supervisor`). :meth:`ensure`
brings the three up in order -- pick the browser (:mod:`.discover`), reach its CDP
endpoint (the user's Chrome through its ``DevToolsActivePort`` file; Kiro's Chromium by
launching it), start the relay (:mod:`.relay`) and connect it, then start Harness's
daemon in the fenced environment (:mod:`.env`) pointed at the relay -- and
:meth:`stop` takes them down in reverse. :meth:`status` is what Settings and the
live card read, and :meth:`call` is the ONE way a helper reaches the daemon.

Helpers run in a worker process the venv's interpreter executes
(:mod:`.helper_worker`), because Harness's helpers are synchronous functions that
talk to the daemon over its IPC: in the gateway's own process they would block the
loop, and importing them there would mean importing an unpinned copy. The worker is
given an allowlist and so is this module (:data:`HELPERS`); a name outside it is
refused before anything is sent.

Why there is no deadline on connecting to the user's Chrome: Chrome 144+ shows an
"Allow remote debugging?" sheet per connection, and the connection that raised the
sheet is the only thing keeping it on screen. A timeout would drop the sheet and the
retry would raise another. ``waiting_allow`` therefore lasts until the person answers,
Chrome closes, or the gateway stops. Approval took 215 s in one measured run.

Everything the daemon writes -- its socket, pid file, log, screenshots -- lives in
``<home>/browser-harness-ipc``, a ``HIDDEN`` crew-home leaf, and Kiro's Chromium
profile in ``<home>/browser-profile``, another. The sandbox never sees the socket
path, the Windows IPC token, or a cookie a takeover login left behind. A data home
too deep for the socket path is refused (:meth:`HarnessSupervisor.runtime_dir`)
rather than served from an unmasked directory.

Kiro's Chromium is launched on ``--remote-debugging-pipe`` wherever Python can hand a
child fds 3 and 4 (macOS, Linux): the browser then has no debugging port at all, and
the relay's own listener is the only CDP endpoint, behind a per-start secret. On
Windows it keeps a port (see ``_CHROMIUM_PORT_ARG``).
"""

from __future__ import annotations

import asyncio
import dataclasses
import itertools
import json
import logging
import os
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.browser_harness import HARNESS_VERSION, budgets, credentials, discover
from kiro_crew.browser_harness import governance as browse_governance
from kiro_crew.browser_harness import install
from kiro_crew.browser_harness.env import daemon_env
from kiro_crew.browser_harness.relay import (
    CdpRelay,
    PipeUpstream,
    RelayClosed,
    Upstream,
    pipe_upstream_from_fds,
)
from kiro_crew.loop_lock import LoopBoundLock

logger = logging.getLogger(__name__)

#: Crew-home leaves this module owns. Spelled identically in ``sandbox_leaves`` and
#: ``security.paths_lists``; ``test_browser_harness_supervisor`` pins them equal.
IPC_LEAF = "browser-harness-ipc"
PROFILE_LEAF = "browser-profile"

#: Connection states ``status()`` reports.
CONNECTED = "connected"
WAITING_ALLOW = "waiting_allow"
DISCONNECTED = "disconnected"
ERROR = "error"

#: The helpers :meth:`HarnessSupervisor.call` will forward, with the keyword
#: arguments each accepts. Mirrors ``helper_worker._ALLOWLIST``; ``js`` exists only
#: for the gateway's own accessibility walker (``browser_harness/snapshot_walker.js``).
HELPERS: Mapping[str, tuple[str, ...]] = {
    "goto": ("url",),
    "click": ("x", "y", "button", "clicks"),
    "mouse_move": ("x", "y"),
    "drag": ("x", "y", "to_x", "to_y"),
    "type_text": ("text",),
    "press_key": ("key", "modifiers"),
    "scroll": ("x", "y", "dy", "dx"),
    "screenshot": ("path", "full", "max_dim"),
    "page_info": (),
    "wait_for_load": ("timeout",),
    "new_tab": ("url",),
    "switch_tab": ("target", "activate"),
    "list_tabs": ("include_chrome",),
    "close_tab": ("target",),
    "current_tab": (),
    "js": ("expression", "target_id"),
    "page_clock": ("state",),
}

#: The daemon's instance name. Not ``default``: a named daemon creates its own tab
#: instead of attaching to the first page it finds, which on the user's Chrome would
#: be one of THEIR tabs.
DAEMON_NAME = "kirocrew"

#: Harness's runtime file stems with ``BH_RUNTIME_DIR`` set (``_ipc._runtime_stem``).
_SOCK_FILE = "bu.sock"
_PORT_FILE = "bu.port"

#: AF_UNIX ``sun_path`` is 104 bytes on macOS (108 on Linux). A data home whose socket
#: path would exceed this is REFUSED rather than relocated: the only other place a
#: short real path exists is the system temp dir, which no sandbox leaf masks, and the
#: daemon resolves symlinks before binding, so an alias inside the leaf cannot help.
#: Windows uses loopback TCP for the IPC and has no such limit.
_MAX_SOCKET_PATH = 100

#: Budgets. The daemon needs only a loopback websocket to the relay, so its start is
#: fast; the worker imports Pillow and friends once.
DAEMON_START_TIMEOUT_SECS = 60.0
WORKER_START_TIMEOUT_SECS = 60.0
#: A helper round trip. Screenshots and ``wait_for_load`` are the long ones.
CALL_TIMEOUT_SECS = 90.0
#: Kiro's Chromium writing ``DevToolsActivePort`` after launch.
CHROMIUM_START_TIMEOUT_SECS = 30.0
_POLL_SECS = 0.1
_IPC_TIMEOUT_SECS = 3.0
_STOP_GRACE_SECS = 5.0

#: Longest reply line the gateway reads from the worker's stdout. asyncio's default
#: stream limit is 64 KiB, which an ordinary snapshot (hundreds of links with long
#: names) exceeds; a line over THIS limit is a protocol failure that discards the
#: worker. The worker bounds its own replies below it
#: (``helper_worker.REPLY_CAP_BYTES``), so a legitimate reply never reaches it.
WORKER_LINE_LIMIT = 8 * 1024 * 1024

#: How long the re-pull before a navigation waits for a start in flight before it
#: skips (a start can hold the lock without deadline on the person's Allow sheet),
#: and the most the re-pull itself may take. With the ``fingerprint()`` bound they
#: are ``budgets.CREDENTIAL_WORST_S``, which the shim's HTTP timeout includes.
CREDENTIAL_LOCK_WAIT_SECS = budgets.CREDENTIAL_LOCK_WAIT_S
NAV_REFRESH_BUDGET_SECS = budgets.CREDENTIAL_REPULL_S

#: Chromium flags. On POSIX ``--remote-debugging-pipe`` speaks CDP over fds 3 and 4 and
#: opens NO port, so nothing in the agent sandbox can reach the browser except through
#: the relay's secret-bearing URL. Python cannot hand a child fds 3 and 4 on Windows
#: (no CRT fd-table inheritance from ``CreateProcess``), so there Chromium keeps
#: ``--remote-debugging-port=0`` -- a free port written to ``DevToolsActivePort`` and
#: reachable by any local process; browser.md's security table names that residual.
#: The window size is the screencast's native frame.
#:
#: The two keystore flags are what lets a navigation finish at all. Chrome's network
#: service encrypts its cookie store with a key it fetches from the OS keystore the
#: first time a profile is used: on macOS that is a Keychain item named after the
#: BINARY ("Chrome for Testing Safe Storage"), which a fresh Chrome for Testing never
#: has, so Keychain raises its "wants to use your confidential information" sheet --
#: and a headless browser can neither show it nor answer it. Every request, loopback
#: included, then waits behind the blocked network service and ``Page.navigate``
#: never returns (measured on macOS 15: ``data:`` pages load, ``http(s)`` hang
#: forever). ``--use-mock-keychain`` keeps the key in memory; ``--password-store=basic``
#: is the same guard for a Linux desktop keyring (GNOME Keyring / KWallet unlock
#: prompt). The profile is Kiro's hidden ``browser-profile`` leaf, not the person's,
#: so nothing of theirs is left less protected. Playwright and the dashboard page
#: check pass the same pair for the same reason.
_CHROMIUM_PIPE_ARG = "--remote-debugging-pipe"
_CHROMIUM_PORT_ARG = "--remote-debugging-port=0"
_CHROMIUM_ARGS: tuple[str, ...] = (
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-sync",
    "--disable-background-networking",
    "--use-mock-keychain",
    "--password-store=basic",
    "--window-size=1280,800",
)
#: The keystore pair above, by name, so the regression test and the spec can point at it.
_KEYSTORE_ARGS: tuple[str, ...] = ("--use-mock-keychain", "--password-store=basic")
_HEADLESS_ARG = "--headless=new"
#: Only as root: Chromium refuses to start sandboxed as uid 0 (a container), and a
#: sandbox-less Chromium for anyone else would be a widening for no reason.
_NO_SANDBOX_ARG = "--no-sandbox"
_ROOT_UID = 0

#: Chrome writes this file in the user-data directory when its debugging port is on.
DEVTOOLS_PORT_FILE = "DevToolsActivePort"
_LOOPBACK = "127.0.0.1"
#: How long the loopback probe of a ``DevToolsActivePort`` port may take.
_PORT_PROBE_SECS = 0.5

#: The POSIX launcher for Kiro's Chromium: ``/bin/sh`` moves the two pipe ends the
#: gateway passed (``$1`` read end, ``$2`` write end) onto fds 3 and 4, then ``exec``s
#: Chromium in its place. Every token is a constant here or a number the gateway chose.
#: The ends are reopened by path (``/dev/fd/N``) rather than duplicated with
#: ``3<&N``: dash, ``/bin/sh`` on Debian and Ubuntu, reads only a single-digit fd in
#: a redirection, and a gateway's pipe ends are usually 10 or above.
_SH = "/bin/sh"
_SH_REMAP = 'exec 3</dev/fd/"$1" 4>/dev/fd/"$2"; shift 2; exec "$@"'
#: The first fd a pipe end may occupy before the remap, so ``exec 3<... 4>...`` never
#: reads a descriptor it has just overwritten.
_FIRST_SAFE_FD = 5


#: The status detail while a Redownload replaces Kiro's Chromium.
CHROMIUM_REPLACING_DETAIL = "Kiro's Chromium is being redownloaded; try again when it finishes"


class HarnessNotInstalled(RuntimeError):
    """``ensure()`` was called before Settings -> Browser installed the engine."""


class BrowserUnavailable(RuntimeError):
    """No browser can be reached: the detail says what the person has to do."""


class HelperError(RuntimeError):
    """The helper raised in the worker; the message is Harness's own."""


@dataclass
class HarnessHandle:
    """What ``ensure()`` hands back: the live pieces, for the stream and tool lanes."""

    choice: discover.BrowserChoice
    relay: CdpRelay
    daemon: asyncio.subprocess.Process | None
    worker: asyncio.subprocess.Process | None
    browser: asyncio.subprocess.Process | None
    started_at: float = field(default_factory=time.monotonic)


class HarnessSupervisor:
    """Browser, relay and daemon for one gateway. See the module docstring."""

    def __init__(
        self,
        home: Path,
        *,
        config_reader: Callable[[], Mapping[str, Any]] | None = None,
        base_env: Mapping[str, str] | None = None,
        credential_provider: Callable[[], Any] | None = None,
    ) -> None:
        self.home = Path(home)
        self._home_resolved: Path | None = None
        self._config_reader = config_reader or _default_config_reader
        self._base_env = base_env
        self._credential_provider = credential_provider or _default_credential_provider
        self._credentials = credentials.CredentialStatus()
        #: The relay the last apply wrote through; its outcome is shown only while
        #: that browser is still the connected one.
        self._credentials_relay: CdpRelay | None = None
        #: The provider's ``fingerprint()`` and the settings at the last apply; a
        #: navigation re-pulls when either differs.
        self._applied_key: tuple[str | None, credentials.CredentialSettings] | None = None
        self._handle: HarnessHandle | None = None
        self._connection = DISCONNECTED
        self._detail = ""
        self._choice: discover.BrowserChoice | None = None
        self._ensure_lock = LoopBoundLock()
        self._call_lock = LoopBoundLock()
        self._ensure_task: asyncio.Task[HarnessHandle] | None = None
        #: The stop a ``capabilities.browse`` deny started (:meth:`stop_soon`).
        self._stop_task: asyncio.Task[None] | None = None
        #: Set while a Redownload replaces Kiro's Chromium (:meth:`stop_for_chromium_replace`):
        #: a start that would launch it is refused instead of running from files the
        #: swap is about to move.
        self.replacing_chromium = False
        self._request_ids = itertools.count(1)
        #: A helper round trip; tests shorten it.
        self.call_timeout = CALL_TIMEOUT_SECS

    # ── paths ──

    @property
    def ipc_dir(self) -> Path:
        return self.home / IPC_LEAF

    @property
    def profile_dir(self) -> Path:
        return self.home / PROFILE_LEAF

    def runtime_dir(self) -> Path:
        """Where the daemon's socket lives: ``run/`` inside the hidden IPC leaf.

        Raises :class:`BrowserUnavailable` on POSIX when the socket path would exceed
        the ``sun_path`` limit (see :data:`_MAX_SOCKET_PATH`): a relocated socket
        would sit outside every sandbox mask, so the control is refused, not skipped.
        """
        preferred = self.ipc_dir / "run"
        if platform_compat.IS_WINDOWS:
            return preferred
        # Measured on the RESOLVED path: Harness binds ``Path(BH_RUNTIME_DIR).resolve()``
        # (``_ipc.py``), so a short symlinked home whose real path is deep would pass
        # a check on the spelling and fail at bind with a message that names the
        # wrong cause. Resolved once; the home does not move under a running gateway.
        resolved = self._resolved_home()
        real = resolved / IPC_LEAF / "run" / _SOCK_FILE
        length = len(str(real).encode())
        if length <= _MAX_SOCKET_PATH:
            return preferred
        raise BrowserUnavailable(
            f"the data home is too deep for the browser daemon's socket "
            f"({length} bytes, limit {_MAX_SOCKET_PATH}): {real}"
        )

    def _resolved_home(self) -> Path:
        if self._home_resolved is None:
            try:
                self._home_resolved = self.home.resolve()
            except OSError:
                self._home_resolved = self.home
        return self._home_resolved

    def _runtime_dir_or_none(self) -> Path | None:
        try:
            return self.runtime_dir()
        except BrowserUnavailable:
            return None

    @property
    def relay(self) -> CdpRelay | None:
        return self._handle.relay if self._handle is not None else None

    # ── status ──

    def status(self) -> dict[str, Any]:
        """The Settings/live-card shape: install, browser, connection, detail."""
        state = install.install_state(self.home)
        choice = self._choice
        if choice is None:
            choice = self._choose(state)
        return {
            "installed": state.installed,
            "version": state.version if state.installed else None,
            "wanted_version": HARNESS_VERSION,
            "browser": choice.as_dict() if choice is not None else None,
            "connection": self._connection,
            "detail": self._detail,
            "agent_tabs": len(self.relay.agent_targets()) if self.relay else 0,
            "chromium": state.chromium.as_dict(),
            "credentials": self._credentials_snapshot(),
        }

    def _credentials_snapshot(self) -> dict[str, Any]:
        """Who supplies credentials NOW, plus the live browser's last apply. No value.

        ``provider``, ``enabled`` and ``domains`` are read from the live config on
        every call, so Settings shows a switch flipped while no browser runs. The
        apply's outcome (``state``, count) is kept only while the browser it filled
        is still connected and the settings are the ones it applied; otherwise
        ``state`` says what comes next.
        """
        provider = self._provider()
        name = credentials.provider_name(provider)
        settings = self._credential_settings(provider)
        # The browser the last apply filled is still up: the connected handle's,
        # or a start still in flight (no handle yet) whose relay is open.
        relay, handle = self._credentials_relay, self._handle
        live = (
            relay is not None
            and not relay.closed
            and (handle is None or handle.relay is relay)
            and self._applied_key is not None
            and self._applied_key[1] == settings
        )
        snap = (self._credentials if live else credentials.CredentialStatus()).as_dict()
        snap.update(provider=name, enabled=settings.enabled, domains=list(settings.domains))
        if not live and name is not None:
            if settings.enabled:
                snap["state"] = credentials.STATE_PENDING
            else:
                snap["state"], snap["reason"] = credentials.STATE_DISABLED, "disabled"
        return snap

    def _credential_settings(self, provider: Any) -> credentials.CredentialSettings:
        """``browser.credentials`` from the live config, with the edition's defaults."""
        try:
            section = self._config_reader().get("credentials")
        except Exception:  # noqa: BLE001 - an unreadable config means the defaults
            section = None
        return credentials.effective_settings(section, provider)

    def _provider(self) -> Any:
        from kiro_crew.platform.context import PlatformCompositionError

        try:
            return self._credential_provider()
        except PlatformCompositionError:
            raise
        except Exception:  # noqa: BLE001 - a broken seam reads as no provider
            logger.warning("browser credentials: provider lookup failed; none used")
            return None

    @property
    def connected(self) -> bool:
        handle = self._handle
        return handle is not None and not handle.relay.closed and _alive(handle.daemon)

    @property
    def holds_browser(self) -> bool:
        """Anything is up or coming up: a handle (even one whose daemon died) or a start.

        What a policy stop asks, rather than :attr:`connected`: a browser whose daemon
        exited still runs its pages, and a start parked on Chrome's Allow sheet would
        finish once Allow is clicked.
        """
        task = self._ensure_task
        return self._handle is not None or (task is not None and not task.done())

    # ── lifecycle ──

    async def ensure(self) -> HarnessHandle:
        """Bring browser, relay and daemon up (or return the live handle).

        Serialised: a second caller waits for the first instead of launching a
        second browser. Raises :class:`HarnessNotInstalled` or
        :class:`BrowserUnavailable`; waits without deadline on the Allow sheet.
        """
        async with self._ensure_lock:
            if self.connected and self._handle is not None:
                return self._handle
            await self._teardown()
            return await self._start()

    def ensure_in_background(self) -> asyncio.Task[HarnessHandle]:
        """Start ``ensure()`` as a task the caller does not wait for (the HTTP routes)."""
        task = self._ensure_task
        if task is not None and not task.done():
            return task
        task = asyncio.create_task(self.ensure(), name="browser-harness-ensure")
        self._ensure_task = task
        task.add_done_callback(_log_ensure_outcome)
        return task

    async def stop(self) -> None:
        """Close the agent's tabs, stop daemon, worker, relay and Kiro's Chromium."""
        task = self._ensure_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - stopping anyway
                pass
        async with self._ensure_lock:
            await self._teardown()
        self._set(DISCONNECTED, "")

    @property
    def runs_kiro_chromium(self) -> bool:
        """Kiro's own Chromium is up, or a start that chose it is under way."""
        handle = self._handle
        if handle is not None:
            return handle.choice.kind == "kiro_chromium"
        task = self._ensure_task
        choice = self._choice
        return (
            task is not None
            and not task.done()
            and (choice is None or choice.kind == "kiro_chromium")
        )

    async def stop_for_chromium_replace(self) -> None:
        """Before a Redownload: refuse new starts of Kiro's Chromium, stop a running one.

        The caller sets :attr:`replacing_chromium` back to ``False`` when the install
        ends. A chat whose op was cut short reads why in the status detail.
        """
        self.replacing_chromium = True
        if self.runs_kiro_chromium:
            await self.stop()
            self._set(DISCONNECTED, CHROMIUM_REPLACING_DETAIL)

    def stop_soon(self) -> asyncio.Task[None]:
        """Start :meth:`stop` once, or return the one already running.

        For the policy stops (``browser_harness.governance``): every refused route and
        helper asks for one, and a second ``stop()`` cancelling the start a first one
        is still tearing down would interrupt that cleanup and leak the browser.
        """
        task = self._stop_task
        if task is None or task.done():
            task = asyncio.create_task(self.stop(), name="browser-harness-policy-stop")
            self._stop_task = task
        return task

    async def test(self, wait_secs: float) -> dict[str, Any]:
        """``ensure()`` with a bounded wait, then the status; the Allow sheet outlasts it."""
        task = self.ensure_in_background()
        try:
            await asyncio.wait_for(asyncio.shield(task), wait_secs)
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            # The start was stopped by someone else (Disconnect, a policy stop): report
            # the status. A cancel of THIS call leaves the shielded start running.
            if not task.cancelled():
                raise
        except Exception:  # noqa: BLE001 - the failure is in status()["detail"]
            pass
        # status() reads the config: off the loop.
        report = await asyncio.to_thread(self.status)
        if self.connected:
            try:
                tabs = await self.call("list_tabs", include_chrome=False)
                report["tabs"] = len(tabs) if isinstance(tabs, list) else None
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                report["tabs"] = None
                report["detail"] = str(exc)
        return report

    # ── helpers ──

    async def call(self, helper: str, **kwargs: Any) -> Any:
        """Run ONE allowlisted Harness helper through the worker; return its result.

        Every request carries its own id and the reply is matched to it; a reply the
        worker writes after its request was cancelled or timed out is never read as
        the answer to a later call, because the worker is stopped on either and the
        next call starts a fresh one.
        """
        accepted = HELPERS.get(helper)
        if accepted is None:
            raise HelperError(f"helper not allowed: {helper}")
        unknown = sorted(set(kwargs) - set(accepted))
        if unknown:
            raise HelperError(f"{helper}: unexpected arguments {unknown}")
        # Every helper an op or a reflex loop runs passes here, so a deny that lands
        # mid-op stops it at its next step, and the running browser with it.
        if await browse_governance.browse_denied_async(browse_governance.HOST_KEY, audit=False):
            await asyncio.shield(self.stop_soon())
            raise browse_governance.BrowsingDenied()
        handle = await self.ensure()
        async with self._call_lock:
            if not _alive(handle.worker):
                handle.worker = await self._start_worker(handle)
            assert handle.worker is not None
            request = {"id": next(self._request_ids), "helper": helper, "kwargs": kwargs}
            try:
                reply = await _worker_request(handle.worker, request, self.call_timeout)
            except asyncio.CancelledError:
                await self._discard_worker(handle)
                raise
            if reply.get("fatal"):
                await self._discard_worker(handle)
        if not reply.get("ok"):
            raise HelperError(str(reply.get("error") or "helper failed"))
        return reply.get("result")

    async def _discard_worker(self, handle: HarnessHandle) -> None:
        """Stop a worker whose reply stream cannot be trusted (it owes a reply nobody wants)."""
        worker, handle.worker = handle.worker, None
        await _stop_process(worker)

    # ── internals ──

    def _set(self, connection: str, detail: str) -> None:
        self._connection = connection
        self._detail = detail

    def _choose(self, state: install.InstallState) -> discover.BrowserChoice | None:
        try:
            cfg = self._config_reader()
        except Exception:  # noqa: BLE001 - a config read failure means defaults
            cfg = {}
        try:
            return discover.choose_browser(
                cfg,
                kiro_chromium=state.chromium.executable,
                kiro_profile=self.profile_dir,
            )
        except Exception:  # noqa: BLE001 - a probe failure is "unknown", not a crash
            logger.debug("browser discovery failed", exc_info=True)
            return None

    async def _start(self) -> HarnessHandle:
        # Nothing is launched, and no credential is pulled, under a deny.
        if await browse_governance.browse_denied_async(browse_governance.HOST_KEY):
            self._set(DISCONNECTED, browse_governance.DENIED_TEXT)
            raise browse_governance.BrowsingDenied()
        # Disk reads and the discovery probes (stat, plist, which) run off the loop:
        # ``ensure()`` is awaited from HTTP handlers on the gateway's one loop.
        state = await asyncio.to_thread(install.install_state, self.home)
        if not state.installed or state.python is None:
            self._set(DISCONNECTED, "Browser Harness is not installed")
            raise HarnessNotInstalled(self._detail)
        try:
            await asyncio.to_thread(self.runtime_dir)  # resolves the home once (lstat chain)
        except BrowserUnavailable as exc:
            self._set(ERROR, str(exc))
            raise
        choice = await asyncio.to_thread(self._choose, state)
        if choice is None:
            self._set(ERROR, "could not pick a browser")
            raise BrowserUnavailable(self._detail)
        if choice.kind == "kiro_chromium" and self.replacing_chromium:
            self._set(DISCONNECTED, CHROMIUM_REPLACING_DETAIL)
            raise BrowserUnavailable(self._detail)
        self._choice = choice
        browser_proc: asyncio.subprocess.Process | None = None
        daemon: asyncio.subprocess.Process | None = None
        relay = CdpRelay()
        try:
            upstream: str | Upstream
            if choice.kind == "kiro_chromium":
                browser_proc, upstream = await self._launch_chromium(choice, state)
                self._set(DISCONNECTED, "connecting to Kiro's Chromium")
            else:
                upstream = await self._user_browser_ws_url(choice)
                self._set(WAITING_ALLOW, "click Allow in the browser's remote debugging prompt")
            relay_url = await relay.start(upstream)
            relay.on_upstream_closed = self._on_upstream_closed
            # Again after the Allow wait, which has no deadline: a deny that landed
            # meanwhile must not finish the start.
            if await browse_governance.browse_denied_async(browse_governance.HOST_KEY):
                raise browse_governance.BrowsingDenied()
            # Before the daemon: no agent op can reach the browser until this returns.
            await self._apply_credentials(relay, choice, "start")
            daemon = await self._start_daemon(state.python, relay_url)
            handle = HarnessHandle(choice, relay, daemon, None, browser_proc)
            handle.worker = await self._start_worker(handle)
        except asyncio.CancelledError:
            # The daemon too: started before the worker, it would otherwise outlive
            # a cancelled ensure() holding the hidden socket until a later start
            # happens to reap it as a stray.
            await self._abandon_start(relay, browser_proc, daemon)
            self._set(DISCONNECTED, "")
            raise
        except browse_governance.BrowsingDenied:
            await self._abandon_start(relay, browser_proc, daemon)
            self._set(DISCONNECTED, browse_governance.DENIED_TEXT)
            raise
        except Exception as exc:
            await self._abandon_start(relay, browser_proc, daemon)
            self._set(ERROR, str(exc))
            raise
        self._handle = handle
        self._set(CONNECTED, "")
        return handle

    async def _credential_key(
        self, provider: Any
    ) -> tuple[str | None, credentials.CredentialSettings]:
        """What an apply depends on: the provider's ``fingerprint()`` and the settings."""
        settings = await asyncio.to_thread(self._credential_settings, provider)
        return await credentials.provider_fingerprint(provider), settings

    async def _apply_credentials(
        self, relay: CdpRelay, choice: discover.BrowserChoice, reason: str
    ) -> None:
        """Fill the browser behind *relay* from the edition provider."""
        target = credentials.Target(kind=choice.kind, headless=choice.headless, reason=reason)
        provider = self._provider()
        # Read BEFORE the cookies, so a source that changes during the pull is seen
        # as changed at the next navigation rather than missed.
        key = await self._credential_key(provider)
        self._credentials = await credentials.apply(
            relay, provider=provider, target=target, settings=key[1]
        )
        self._applied_key = key
        self._credentials_relay = relay
        _audit_injection(self._credentials, reason)

    async def refresh_credentials_if_source_changed(self) -> bool:
        """Before an agent navigation: re-pull when the source or the settings moved.

        Cheap when nothing changed (one bounded ``fingerprint()`` call and a config
        read; nothing at all without a provider). The re-pull is bounded by
        :data:`NAV_REFRESH_BUDGET_SECS`, so a slow provider cannot hold the
        navigation: past the bound it goes ahead with what the browser holds, the
        status says ``refresh_timeout``, and the old key is kept so the next
        navigation tries again. A start in flight is not waited on past
        :data:`CREDENTIAL_LOCK_WAIT_SECS`. Returns whether it re-applied. Never
        raises for a provider failure.
        """
        handle = self._handle
        if handle is None or handle.choice.kind != "kiro_chromium" or self._applied_key is None:
            return False
        provider = self._provider()
        if credentials.provider_name(provider) is None:
            return False
        fingerprint, settings = await self._credential_key(provider)
        # A ``None`` fingerprint means "cannot tell" (a slow stat): not a change.
        if (fingerprint or self._applied_key[0], settings) == self._applied_key:
            return False
        if not await self._acquire_briefly():
            return False
        try:
            handle = self._handle
            if handle is None or not self.connected or handle.choice.kind != "kiro_chromium":
                return False
            try:
                # The key is recorded only once the apply finished, so a timeout or
                # a cancellation leaves the old one and the next navigation retries.
                await asyncio.wait_for(
                    self._apply_credentials(handle.relay, handle.choice, "source_changed"),
                    NAV_REFRESH_BUDGET_SECS,
                )
            except asyncio.TimeoutError:
                self._credentials.reason = "refresh_timeout"
                logger.warning("browser credentials: re-pull before navigation timed out")
                return False
            return True
        finally:
            self._ensure_lock.release()

    async def _acquire_briefly(self) -> bool:
        """Take the ensure lock within :data:`CREDENTIAL_LOCK_WAIT_SECS`, or report it busy."""
        try:
            await asyncio.wait_for(self._ensure_lock.acquire(), CREDENTIAL_LOCK_WAIT_SECS)
        except asyncio.TimeoutError:
            return False
        return True

    @staticmethod
    async def _abandon_start(
        relay: CdpRelay,
        browser_proc: asyncio.subprocess.Process | None,
        daemon: asyncio.subprocess.Process | None,
    ) -> None:
        """Undo a ``_start`` that did not reach a handle: nothing it spawned survives."""
        await relay.close()
        await _stop_process(daemon)
        await _stop_process(browser_proc)

    async def _teardown(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            await self._shutdown_stray_daemon()
            return
        if not handle.relay.closed:
            try:
                await handle.relay.close_agent_targets()
            except (RelayClosed, Exception):  # noqa: BLE001 - best effort at teardown
                pass
        await self._ipc_shutdown()
        await _stop_process(handle.worker)
        await _stop_process(handle.daemon)
        await handle.relay.close()
        await _stop_process(handle.browser)

    async def _on_upstream_closed(self) -> None:
        if self._connection == CONNECTED:
            self._set(DISCONNECTED, "the browser closed the connection")

    async def _user_browser_ws_url(self, choice: discover.BrowserChoice) -> str:
        profile = choice.user_data_dir
        name = "Chrome" if choice.kind == "user_chrome" else "Edge"
        if profile is None:
            raise BrowserUnavailable(f"{name} has no profile directory on this machine")
        url = await _devtools_ws_url(profile)
        if url is None:
            self._set(DISCONNECTED, f"turn on {name}'s remote debugging")
            raise BrowserUnavailable(
                f"{name} is not accepting debugging connections: open "
                "chrome://inspect/#remote-debugging and turn on 'Allow remote debugging', "
                "then try again"
            )
        return url

    async def _launch_chromium(
        self, choice: discover.BrowserChoice, state: install.InstallState
    ) -> tuple[asyncio.subprocess.Process, str | PipeUpstream]:
        """Start Kiro's Chromium; return it and the relay's upstream (pipe, or a URL on Windows)."""
        exe = state.chromium.executable
        if exe is None:
            raise BrowserUnavailable("Kiro's Chromium is not downloaded: install it in Settings")
        profile = self.profile_dir
        await asyncio.to_thread(platform_compat.make_owner_only_dir, profile)
        argv = [str(exe), *_CHROMIUM_ARGS, f"--user-data-dir={profile}"]
        if choice.headless:
            argv.append(_HEADLESS_ARG)
        if platform_compat.effective_uid() == _ROOT_UID:
            argv.append(_NO_SANDBOX_ARG)
        if platform_compat.IS_WINDOWS:
            return await self._launch_chromium_port(argv, profile)
        return await self._launch_chromium_pipe(argv)

    async def _launch_chromium_pipe(
        self, argv: list[str]
    ) -> tuple[asyncio.subprocess.Process, PipeUpstream]:
        """POSIX: ``--remote-debugging-pipe`` over two pipes; no port is ever opened."""
        to_browser_r, to_browser_w = os.pipe()
        from_browser_r, from_browser_w = os.pipe()
        try:
            to_browser_r = _fd_at_or_above(to_browser_r, _FIRST_SAFE_FD)
            from_browser_w = _fd_at_or_above(from_browser_w, _FIRST_SAFE_FD)
            sh_argv = [
                _SH,
                "-c",
                _SH_REMAP,
                "kirocrew-chromium",
                str(to_browser_r),
                str(from_browser_w),
                *argv,
                _CHROMIUM_PIPE_ARG,
            ]
            proc = await self._spawn(
                sh_argv,
                env=dict(self._env()),
                pipes=False,
                pass_fds=(to_browser_r, from_browser_w),
            )
        except BaseException:
            for fd in (to_browser_w, from_browser_r):
                os.close(fd)
            raise
        finally:
            # The browser holds its two ends now (or never started); the gateway
            # keeps only its own.
            for fd in (to_browser_r, from_browser_w):
                os.close(fd)
        try:
            upstream = await pipe_upstream_from_fds(from_browser_r, to_browser_w)
        except BaseException:
            # pipe_upstream_from_fds has closed the gateway's two ends already.
            await _stop_unowned(proc)
            raise
        return proc, upstream

    async def _launch_chromium_port(
        self, argv: list[str], profile: Path
    ) -> tuple[asyncio.subprocess.Process, str]:
        """Windows: a free debugging port, read back from ``DevToolsActivePort``."""
        port_file = profile / DEVTOOLS_PORT_FILE
        await asyncio.to_thread(port_file.unlink, missing_ok=True)
        proc = await self._spawn([*argv, _CHROMIUM_PORT_ARG], env=dict(self._env()), pipes=False)
        try:
            deadline = time.monotonic() + CHROMIUM_START_TIMEOUT_SECS
            while time.monotonic() < deadline:
                url = await _devtools_ws_url(profile)
                if url is not None:
                    return proc, url
                if proc.returncode is not None:
                    break
                await asyncio.sleep(_POLL_SECS)
            raise BrowserUnavailable("Kiro's Chromium did not open its debugging port")
        except BaseException:
            await _stop_unowned(proc)
            raise

    def _env(self) -> Mapping[str, str]:
        return self._base_env if self._base_env is not None else os.environ

    def _daemon_env(self, relay_url: str) -> dict[str, str]:
        ipc = self.ipc_dir
        runtime = self.runtime_dir()
        for path in (ipc, ipc / "tmp", ipc / "workspace", runtime):
            platform_compat.make_owner_only_dir(path)
        env = daemon_env(self._env(), relay_url)
        env.update(
            {
                "BU_NAME": DAEMON_NAME,
                "BH_HOME": str(ipc),
                "BH_CONFIG_DIR": str(ipc),
                "BH_RUNTIME_DIR": str(runtime),
                "BH_TMP_DIR": str(ipc / "tmp"),
                "BH_AGENT_WORKSPACE": str(ipc / "workspace"),
                "PYTHONIOENCODING": "utf-8",
            }
        )
        return env

    async def _start_daemon(self, python: Path, relay_url: str) -> asyncio.subprocess.Process:
        await self._shutdown_stray_daemon()
        env = await asyncio.to_thread(self._daemon_env, relay_url)
        # ``-I`` as for the worker: isolated mode keeps the working directory and
        # ``PYTHONPATH`` off ``sys.path``, so ``-m browser_harness.daemon`` resolves to
        # the hash-pinned package in the venv and never to a ``browser_harness/``
        # directory in whatever project the gateway was started from.
        proc = await self._spawn(
            [str(python), "-I", "-m", "browser_harness.daemon"], env=env, pipes=False
        )
        try:
            deadline = time.monotonic() + DAEMON_START_TIMEOUT_SECS
            while time.monotonic() < deadline:
                if await self._ipc_ping():
                    return proc
                if proc.returncode is not None:
                    raise BrowserUnavailable(
                        f"the Harness daemon exited with code {proc.returncode}"
                    )
                await asyncio.sleep(_POLL_SECS)
            raise BrowserUnavailable("the Harness daemon did not start in time")
        except BaseException:
            await _stop_unowned(proc)
            raise

    async def _start_worker(self, handle: HarnessHandle) -> asyncio.subprocess.Process:
        state = await asyncio.to_thread(install.install_state, self.home)
        if state.python is None:
            raise HarnessNotInstalled("Browser Harness is not installed")
        env = await asyncio.to_thread(self._daemon_env, handle.relay.url)
        worker_py = Path(__file__).with_name("helper_worker.py")
        proc = await self._spawn([str(state.python), "-I", str(worker_py)], env=env, pipes=True)
        try:
            reply = await _worker_request(proc, {"id": 0, "ping": True}, WORKER_START_TIMEOUT_SECS)
            if not reply.get("ok"):
                raise BrowserUnavailable(
                    str(reply.get("error") or "the helper worker did not start")
                )
        except BaseException:
            await _stop_unowned(proc)
            raise
        return proc

    async def _spawn(
        self,
        argv: list[str],
        *,
        env: dict[str, str],
        pipes: bool,
        pass_fds: tuple[int, ...] = (),
    ) -> asyncio.subprocess.Process:
        """The supervisor's one spawn: the venv's interpreter or Kiro's Chromium.

        Every argv token is a module constant, a path under the data home or the
        read-only harness prefix, or a pipe fd number the gateway chose; nothing an
        agent says reaches it. Each child gets its own process group so
        :func:`_stop_process` can take the tree.
        """
        kwargs: dict[str, Any] = {"env": env, "stdin": subprocess.DEVNULL}
        if pass_fds:
            kwargs["pass_fds"] = pass_fds
        if pipes:
            kwargs["stdin"] = subprocess.PIPE
            kwargs["stdout"] = subprocess.PIPE
            kwargs["stderr"] = subprocess.DEVNULL
            kwargs["limit"] = WORKER_LINE_LIMIT
        else:
            kwargs["stdout"] = subprocess.DEVNULL
            kwargs["stderr"] = subprocess.DEVNULL
        if platform_compat.IS_WINDOWS:
            kwargs["creationflags"] = (
                platform_compat.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined,unused-ignore]
            )
        else:
            kwargs["start_new_session"] = True
        return await asyncio.create_subprocess_exec(*argv, **kwargs)

    # ── Harness IPC (the daemon's own protocol, client side) ──

    async def _ipc_ping(self) -> bool:
        reply = await self._ipc_request({"meta": "ping"})
        return isinstance(reply, dict) and reply.get("pong") is True

    async def _ipc_shutdown(self) -> None:
        await self._ipc_request({"meta": "shutdown"})

    async def _shutdown_stray_daemon(self) -> None:
        """A daemon from a previous gateway life would make ours exit as 'already running'."""
        runtime = self._runtime_dir_or_none()
        if runtime is None:
            return
        if await self._ipc_ping():
            await self._ipc_shutdown()
            deadline = time.monotonic() + _STOP_GRACE_SECS
            while time.monotonic() < deadline and await self._ipc_ping():
                await asyncio.sleep(_POLL_SECS)
        for name in (_SOCK_FILE, _PORT_FILE):
            try:
                (runtime / name).unlink(missing_ok=True)
            except OSError:
                pass

    async def _ipc_request(self, body: dict[str, Any]) -> Any:
        runtime = self._runtime_dir_or_none()
        if runtime is None:
            return None
        try:
            if platform_compat.IS_WINDOWS:
                port_text = await asyncio.to_thread(
                    (runtime / _PORT_FILE).read_text, encoding="utf-8"
                )
                port_info = json.loads(port_text)
                body = {**body, "token": port_info["token"]}
                opener = asyncio.open_connection(_LOOPBACK, int(port_info["port"]))
            else:
                opener = asyncio.open_unix_connection(str(runtime / _SOCK_FILE))
            reader, writer = await asyncio.wait_for(opener, _IPC_TIMEOUT_SECS)
        except (OSError, ValueError, KeyError, asyncio.TimeoutError):
            return None
        try:
            writer.write((json.dumps(body) + "\n").encode("utf-8"))
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), _IPC_TIMEOUT_SECS)
            return json.loads(line.decode("utf-8")) if line else None
        except (OSError, ValueError, asyncio.TimeoutError):
            return None
        finally:
            writer.close()


# ── module helpers ──


#: How many domains one injection's SEL row names before it counts the rest.
_AUDIT_DOMAINS = 20


def _audit_injection(status: credentials.CredentialStatus, reason: str) -> None:
    """One SEL row per apply that set a cookie: provider, count, domains. Never a value."""
    if not status.applied:
        return
    domains = status.applied_domains
    shown = ",".join("".join(ch for ch in d if ch.isprintable()) for d in domains[:_AUDIT_DOMAINS])
    if len(domains) > _AUDIT_DOMAINS:
        shown += f",+{len(domains) - _AUDIT_DOMAINS} more"
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller="gateway",
            operation="browser_harness.credentials_injected",
            outcome="applied",
            source="browser_api",
            resources=(
                f"provider={status.provider or '-'} reason={reason} cookies={status.applied} "
                f"domains={shown}"
            ),
        )
    except Exception:  # noqa: BLE001 - an audit failure never stops the browser
        logger.warning("browser credentials: SEL write failed", exc_info=True)


def _default_credential_provider() -> Any:
    """The composed edition provider (the public Default supplies nothing)."""
    from kiro_crew.platform.context import current_context

    return current_context().browser_credentials


def _default_config_reader() -> Mapping[str, Any]:
    """The ``browser`` config section, tolerant of a build that has not shipped it."""
    from kiro_crew.config import KiroCrewConfig

    section = getattr(KiroCrewConfig.load(), "browser", None)
    if section is None:
        return {}
    if dataclasses.is_dataclass(section) and not isinstance(section, type):
        return dataclasses.asdict(section)
    if isinstance(section, Mapping):
        return dict(section)
    return {}


async def _devtools_ws_url(profile: Path) -> str | None:
    """The websocket URL Chrome wrote to ``<profile>/DevToolsActivePort``, if live.

    The file read and the loopback probe both leave the event loop: a port whose
    listener neither answers nor refuses would otherwise hold every other task
    for the probe's wait.
    """
    try:
        text = await asyncio.to_thread((profile / DEVTOOLS_PORT_FILE).read_text, encoding="utf-8")
        lines = text.splitlines()
        port = int(lines[0].strip())
        path = lines[1].strip()
    except (OSError, ValueError, IndexError):
        return None
    if not path.startswith("/"):
        return None
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(_LOOPBACK, port), _PORT_PROBE_SECS
        )
    except (OSError, asyncio.TimeoutError):
        return None
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return f"ws://{_LOOPBACK}:{port}{path}"


def _fd_at_or_above(fd: int, floor: int) -> int:
    """Return an fd for the same file at or above *floor*; *fd* itself is closed.

    ``F_DUPFD`` picks the lowest free fd at or above *floor* in one call. A loop of
    ``os.dup`` cannot: with two free slots below *floor* it bounces between them.
    POSIX only; the pipe launcher is its one caller.
    """
    if fd >= floor:
        os.set_inheritable(fd, True)
        return fd
    higher = platform_compat.dup_fd_at_or_above(fd, floor)
    os.close(fd)
    os.set_inheritable(higher, True)
    return higher


def _alive(proc: asyncio.subprocess.Process | None) -> bool:
    return proc is not None and proc.returncode is None


async def _stop_process(proc: asyncio.subprocess.Process | None) -> None:
    if proc is None or proc.returncode is not None:
        return
    if proc.stdin is not None:
        try:
            proc.stdin.close()
        except Exception:  # noqa: BLE001 - the pipe may already be gone
            pass
    try:
        await platform_compat.terminate_and_reap(proc, grace=_STOP_GRACE_SECS)
    except Exception:  # noqa: BLE001 - a child that will not die is logged, not raised
        logger.debug("browser harness child did not stop cleanly", exc_info=True)


async def _stop_unowned(proc: asyncio.subprocess.Process) -> None:
    """Stop a child its launcher spawned but never handed to its caller.

    Runs from an ``except BaseException`` while a cancellation (a disconnect, a
    gateway shutdown) may be unwinding the launcher, so the stop is shielded: a
    second cancellation cannot leave the child running with its profile lock, its
    window and, on Windows, its debugging endpoint. The stop always runs to the
    end before this returns, and a cancellation that arrived meanwhile is raised
    afterwards rather than dropped.
    """
    task = asyncio.ensure_future(_stop_process(proc))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError


async def _worker_request(
    proc: asyncio.subprocess.Process, body: dict[str, Any], timeout: float
) -> dict[str, Any]:
    """One request to the worker; the reply whose ``id`` matches, within *timeout*.

    A reply with another id is a leftover from a request that was abandoned and
    is skipped. A failure the worker cannot recover from (timeout, exit, garbage)
    is returned with ``fatal`` set so the caller stops the worker.
    """
    assert proc.stdin is not None and proc.stdout is not None
    deadline = time.monotonic() + timeout
    try:
        proc.stdin.write((json.dumps(body) + "\n").encode("utf-8"))
        await proc.stdin.drain()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), remaining)
            except ValueError as exc:
                # ``readline`` past the stream limit: the rest of the line is still in
                # the pipe, so nothing read after it would frame correctly. Fatal.
                return {"ok": False, "error": f"helper reply too long: {exc}", "fatal": True}
            if not line:
                return {"ok": False, "error": "helper worker exited", "fatal": True}
            try:
                reply = json.loads(line.decode("utf-8"))
            except ValueError:
                return {"ok": False, "error": "helper worker answered garbage", "fatal": True}
            if not isinstance(reply, dict):
                return {"ok": False, "error": "bad reply", "fatal": True}
            # ``id: null`` is the worker's own protocol failure (import failed,
            # invalid JSON): about this request, since nothing else is in flight.
            if reply.get("id") == body.get("id") or reply.get("id") is None:
                return reply
            logger.debug("browser harness: dropping stale worker reply id=%r", reply.get("id"))
    except asyncio.TimeoutError:
        return {"ok": False, "error": f"helper timed out after {timeout:.0f}s", "fatal": True}
    except (OSError, ConnectionError, BrokenPipeError) as exc:
        return {"ok": False, "error": f"helper worker is gone: {exc}", "fatal": True}


def _log_ensure_outcome(task: asyncio.Task[HarnessHandle]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.info("browser harness not connected: %s", exc)


_SUPERVISORS: dict[Path, HarnessSupervisor] = {}


def get_supervisor(home: Path) -> HarnessSupervisor:
    """The one supervisor for *home*; created on first use."""
    key = Path(home)
    sup = _SUPERVISORS.get(key)
    if sup is None:
        sup = HarnessSupervisor(key)
        _SUPERVISORS[key] = sup
    return sup


def _reset_supervisors_for_tests() -> None:
    _SUPERVISORS.clear()


__all__ = [
    "BrowserUnavailable",
    "CONNECTED",
    "DISCONNECTED",
    "ERROR",
    "HELPERS",
    "HarnessHandle",
    "HarnessNotInstalled",
    "HarnessSupervisor",
    "HelperError",
    "IPC_LEAF",
    "PROFILE_LEAF",
    "WAITING_ALLOW",
    "get_supervisor",
]
