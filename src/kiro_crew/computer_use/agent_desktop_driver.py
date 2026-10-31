"""The computer-use driver: a :class:`ComputerUseBackend` over the agent-desktop CLI.

agent-desktop (vercel-labs/agent-desktop, Apache-2.0) reads and drives the native
accessibility tree on macOS, Windows and Linux, and is the only driver. Every command
prints one JSON envelope (``{"ok": true, "data": ...}`` or ``{"ok": false, "error": {...}}``), and
interactive elements carry snapshot-qualified refs such as ``@s8f3k2p9:e12``. This
module turns that into the seam the dispatch chokepoint already polices, so every
safety layer above the seam (the keystone enable, the denylist, the secure-field
refusal, redaction, the SEL audit, fingerprint drift) runs unchanged.

What maps to what:

* **Snapshot.** One ``snapshot --window-id`` walk becomes a :class:`Snapshot`. Our
  indices number the walk in preorder, as the native walkers do, so the dispatch
  layer's index cache and fingerprint check work as before. The driver keeps the
  agent-desktop ref for each index of the latest walk per window; an action looks
  the ref up and refuses when the record it was handed fingerprints differently
  from the element that ref belongs to.
* **Secure fields.** agent-desktop reports an ``AXSecureTextField`` (macOS),
  ``IsPassword`` (Windows UIA) or password role (AT-SPI) as ``states: ["secure"]``
  and withholds the value. That state sets ``ElementRec.secure``, and a role that
  names a password field sets it too. Before typing or setting a value the driver
  re-reads the target's states through ``get --property states`` and refuses if the
  field is secure now, so a field that turned secure after the walk is still caught.
* **Headless by default.** Every element action runs agent-desktop's default
  semantic delivery, which does not move the cursor, focus a window or synthesize
  keys. ``--headed`` is passed for the Windows ``focus`` before a keyboard verb,
  which agent-desktop grants only headed, and for the Linux typing retry (``focus``
  checked, then ``type``) when a toolkit ignores accessibility writes. Real-pointer
  gestures go to the native helper, which confines them; with no helper (Linux)
  they refuse. Typing and key chords take focus on Windows;
  the pointer stays put.
* **What agent-desktop cannot do** is a coordinate gesture that leaves the pointer
  alone (``app_post``, ``sky_click``): its ``mouse-click`` and ``drag`` require
  ``--headed``. Those, the window inventory (bundle ids, which its own
  ``list-apps`` cannot read inside the gateway sandbox because it shells out to
  ``ps``), the launch catalog, keystrokes and the screenshot come from the
  platform's :class:`~kiro_crew.computer_use.backend.NativeHelper` and native
  capture on macOS and Windows. On Linux there is neither: those verbs refuse or
  use agent-desktop's own command.

Never raises (the seam contract): every failure becomes ``DriverResult(ok=False)``.
The typed text travels in the CLI's argv, which a same-user process can read; the
secure-field refusal keeps credentials out of it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
import zlib
from dataclasses import replace
from typing import Any, Callable, Iterable, Optional

from kiro_crew.computer_use import agent_desktop_locate, render
from kiro_crew.computer_use.backend import ComputerUseBackend, NativeHelper
from kiro_crew.computer_use.types import (
    CLICK_METHOD_ACCESSIBILITY,
    ERR_INDEX_DRIFT,
    MOUSE_BUTTON_LEFT,
    MOUSE_BUTTON_RIGHT,
    PERMISSION_GRANTED,
    PERMISSION_MISSING,
    PERMISSION_UNKNOWN,
    PLATFORM_WINDOWS,
    REFUSAL_SECURE_TARGET,
    TOOL_GET_STATE,
    TOOL_LIST_APPS,
    TOOL_SET_VALUE,
    TRAIT_EDITABLE,
    TRAIT_EXPANDED,
    TRAIT_SELECTED,
    AppRef,
    BackendStatus,
    ClickRequest,
    ComputerUseError,
    DragRequest,
    DriverResult,
    ElementRec,
    LaunchIdentity,
    PermissionProbe,
    Snapshot,
    SnapshotRequest,
)

logger = logging.getLogger(__name__)

#: agent-desktop's snapshot namespace for this process. Refs are scoped to it, so a
#: person driving the same CLI by hand cannot collide with the sidecar's refs.
SESSION_ID = "kirocrew"

#: Upper bound for one CLI call. agent-desktop's own deadlines are shorter (3 s
#: observation, 5 s action, 30 s launch); this only catches a wedged process.
CALL_TIMEOUT_SECS = 45.0

#: agent-desktop's ``--max-depth`` ceiling (1.0.2 refuses more with INVALID_ARGS).
#: Its default is 10, far shallower than a browser window, so the request's depth is
#: always passed, clamped to this.
_MAX_AD_DEPTH = 50

#: Roles agent-desktop uses for password inputs across its adapters. ``states``
#: carrying ``secure`` is the primary signal; this catches an adapter that reports
#: the role instead.
_SECURE_ROLES = frozenset({"passwordfield", "password_text", "securetextfield", "password"})
_SECURE_STATE = "secure"

#: Structural roles that consume no index when they carry no text, no ref and have
#: children, mirroring the native walkers' elision.
_ELIDABLE_ROLES = frozenset({"group", "unknown", "generic", "none", "section"})

#: Our ``computer_perform_action`` names (AX and agent-desktop spellings, folded)
#: -> the agent-desktop command that performs them.
_ACTION_COMMANDS: dict[str, str] = {
    "click": "click",
    "press": "click",
    "axpress": "click",
    "rightclick": "right-click",
    "showmenu": "right-click",
    "axshowmenu": "right-click",
    "toggle": "toggle",
    "expand": "expand",
    "collapse": "collapse",
    "setfocus": "focus",
    "focus": "focus",
    "scrollto": "scroll-to",
    "axscrolltovisible": "scroll-to",
    "check": "check",
    "uncheck": "uncheck",
}
#: The agent-desktop advertised action each command needs, for the "does the
#: element offer it" check. ``focus`` is offered as ``SetFocus``.
_ADVERTISED: dict[str, str] = {
    "click": "click",
    "right-click": "rightclick",
    "toggle": "toggle",
    "expand": "expand",
    "collapse": "collapse",
    "focus": "setfocus",
    "scroll-to": "scrollto",
    "check": "toggle",
    "uncheck": "toggle",
}

_KEY_ALIASES = {"command": "cmd", "option": "alt", "opt": "alt", "control": "ctrl"}

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")

#: Opaque window ids kept before the table is pruned to the windows listed now.
_MAX_WINDOW_IDS = 256

#: Error codes meaning "the tree moved": the remedy is a fresh computer_get_state.
_STALE_CODES = frozenset({"STALE_REF", "SNAPSHOT_NOT_FOUND", "AMBIGUOUS_TARGET"})

#: ``details.cause`` of a headless scoped ``press`` whose window is not in front
#: (Linux). Reported under ``STALE_REF``, but a re-walk does not fix it.
_FOCUS_UNCONFIRMED = "exact_window_focus_unconfirmed"
_NOT_SUPPORTED = "ACTION_NOT_SUPPORTED"
_POLICY_DENIED = "POLICY_DENIED"

Runner = Callable[[list[str], dict[str, str], float], "tuple[int, str]"]


def _subprocess_runner(argv: list[str], env: dict[str, str], timeout: float) -> "tuple[int, str]":
    """Run one CLI call; return ``(returncode, stdout)``. Never uses a shell."""
    proc = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
        # Keeps a console window from flashing per call on Windows; 0 elsewhere.
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return proc.returncode, proc.stdout


#: A real-pointer gesture with no native helper (Linux). agent-desktop's
#: ``mouse-click`` / ``drag --headed`` land on whatever owns the pixel, and the only
#: window list there is AT-SPI's, which leaves out every window whose app exposes no
#: accessibility (terminals, Qt or Java apps without the bridge), so ownership of the
#: point cannot be checked.
_REFUSE_UNCONFINED_POINTER = (
    "a real-pointer click or drag cannot be confined to the target window on this "
    "platform (the window list misses apps without accessibility), so it is not "
    "sent; address the control by element_index instead"
)


class AgentDesktopError(ComputerUseError):
    """A failed CLI call, with agent-desktop's code and whether it may have landed."""

    def __init__(self, message: str, *, code: str = "", delivered: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.delivered = delivered


class AgentDesktopBackend(ComputerUseBackend):
    """Computer-use driver that shells out to a pinned agent-desktop CLI."""

    def __init__(
        self,
        binary: str,
        platform_id: str,
        *,
        native: "NativeHelper | None" = None,
        capture: "Callable[[Snapshot, int, int], Snapshot] | None" = None,
        runner: "Runner | None" = None,
        state_home: "str | None" = None,
        cli_capture: bool = False,
    ) -> None:
        self._binary = binary
        self._platform_id = platform_id
        self._native = native
        self._capture = capture
        #: Take screenshots with agent-desktop's own ``screenshot`` when *capture*
        #: is None (Linux, which has no native capture).
        self._cli_capture = cli_capture
        self._run = runner or _subprocess_runner
        self._state_home = state_home
        self._lock = threading.Lock()
        #: window_key -> {our index: (agent-desktop ref, fingerprint)} of the latest walk.
        self._refs: dict[str, dict[int, tuple[str, str]]] = {}
        #: Our integer window id -> agent-desktop's opaque one. On Linux a window id
        #: is an AT-SPI path (``w-1.0-org-a11y-atspi-accessible-172``), not a number,
        #: while ``AppRef.window_id`` is an int; the int is derived from the string
        #: and this table maps it back.
        self._window_ids: dict[int, str] = {}
        self._status: "BackendStatus | None" = None
        #: The bus refusal of the last probe, which ``status()`` does not cache.
        self._last_bus_reason = ""

    # ── plumbing ──

    @property
    def platform_id(self) -> str:
        return self._platform_id

    @property
    def binary(self) -> str:
        return self._binary

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self._state_home:
            env["AGENT_DESKTOP_HOME"] = self._state_home
        return env

    def _call(
        self,
        command: str,
        *options: str,
        args: "tuple[str, ...]" = (),
        headed: bool = False,
        session: bool = True,
    ) -> Any:
        """Run ``agent-desktop <command> <options> -- <args>``; return ``data``.

        *options* are flags this module writes. *args* are the positionals, and are
        the only place an agent-supplied value (typed text, a value, a key spec) may
        go: they follow ``--``, so a text such as ``--headed`` or ``--force`` is
        delivered as text and never parsed as a flag that would move the pointer or
        lift agent-desktop's quit-shortcut refusal.

        Raises :class:`AgentDesktopError` for an ``ok: false`` envelope, a timeout or
        unparseable output. Argument values are never logged: they carry typed text.
        """
        argv = [self._binary, command, *options]
        if session:
            argv += ["--session", SESSION_ID]
        if headed:
            argv.append("--headed")
        if args:
            argv += ["--", *args]
        try:
            _rc, out = self._run(argv, self._env(), CALL_TIMEOUT_SECS)
        except subprocess.TimeoutExpired:
            raise AgentDesktopError(f"agent-desktop {command} timed out", delivered=True)
        except OSError as exc:
            raise AgentDesktopError(f"could not run agent-desktop: {exc.strerror or exc}")
        envelope = _parse_envelope(out)
        if envelope is None:
            raise AgentDesktopError(f"agent-desktop {command} returned no JSON envelope")
        if envelope.get("ok"):
            return envelope.get("data")
        raise _envelope_error(command, envelope.get("error") or {})

    def _native_ok(self) -> bool:
        return self._native is not None and self._native.status().supported

    # ── status ──

    def status(self) -> BackendStatus:
        if self._status is not None:
            return self._status
        status, lasting = self._probe_status(), True
        if not status.supported and status.reason == self._last_bus_reason:
            # A desktop session that is not up YET: ask again next time instead of
            # refusing until the gateway restarts.
            lasting = False
        if lasting:
            self._status = status
        return status

    def _probe_status(self) -> BackendStatus:
        reason = agent_desktop_locate.verify(self._binary)
        if reason:
            return BackendStatus(supported=False, platform_id=self._platform_id, reason=reason)
        try:
            _rc, out = self._run([self._binary, "--version"], self._env(), CALL_TIMEOUT_SECS)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return BackendStatus(
                supported=False,
                platform_id=self._platform_id,
                reason=f"agent-desktop could not run on this host ({type(exc).__name__})",
            )
        version = out.strip().split()[-1] if out.strip() else ""
        if version != agent_desktop_locate.PINNED_VERSION:
            return BackendStatus(
                supported=False,
                platform_id=self._platform_id,
                reason=(
                    f"agent-desktop reports version '{version or '?'}', but this driver "
                    f"is written for {agent_desktop_locate.PINNED_VERSION}"
                ),
            )
        if self._native is None:
            reason = self._bus_reason()
            self._last_bus_reason = reason
            if reason:
                return BackendStatus(supported=False, platform_id=self._platform_id, reason=reason)
        return BackendStatus(supported=True, platform_id=self._platform_id)

    def _bus_reason(self) -> str:
        """Why agent-desktop sees no apps here, or ``""``.

        Asked only when there is no native helper underneath (Linux). There an empty
        ``supported_surfaces`` means the AT-SPI bus did not answer: no graphical
        session, or one without ``at-spi2-core``. Reporting it beats a working-looking
        backend that lists no windows.
        """
        try:
            data = self._call("status", session=False) or {}
        except AgentDesktopError as exc:
            return f"agent-desktop could not read the desktop session ({exc.code or 'error'})"
        if isinstance(data, dict) and data.get("supported_surfaces"):
            return ""
        return (
            "agent-desktop cannot reach the accessibility bus: run Kiro Crew inside a "
            "graphical session with at-spi2-core installed"
        )

    def probe_permissions(self) -> PermissionProbe:
        """ADVISORY: agent-desktop's own TCC read, with the native responsible hint."""
        hint = ""
        if self._native is not None:
            try:
                hint = self._native.probe_permissions().responsible_hint
            except Exception:
                hint = ""
        try:
            data = self._call("permissions", session=False) or {}
        except Exception:
            return PermissionProbe(responsible_hint=hint)
        return PermissionProbe(
            accessibility=_permission_state(data.get("accessibility")),
            screen_recording=_permission_state(data.get("screen_recording")),
            responsible_hint=hint,
        )

    # ── observation ──

    def list_apps(self) -> DriverResult:
        if self._native_ok():
            return self._native.list_apps()  # type: ignore[union-attr]
        return _guarded("list_apps", lambda: DriverResult(ok=True, apps=self._windows()))

    def resolve_app(self, query: str) -> DriverResult:
        if self._native_ok():
            return self._native.resolve_app(query)  # type: ignore[union-attr]
        return _guarded("resolve_app", lambda: DriverResult(ok=True, app=self._resolve(query)))

    def launch_app(
        self,
        query: str,
        *,
        permit: "Callable[[LaunchIdentity], str | None] | None" = None,
        refuse_launched: "Callable[[AppRef], str | None] | None" = None,
    ) -> DriverResult:
        """Launch through the native helper's vetted catalog, or refuse.

        agent-desktop's ``launch`` accepts arguments, environment and a working
        directory, and resolves names its own way; the seam requires an OS catalog
        that refuses a path and is re-checked against the operator's policy before
        spawning. The native helpers implement exactly that, so they keep the verb.
        """
        if self._native_ok():
            return self._native.launch_app(  # type: ignore[union-attr]
                query, permit=permit, refuse_launched=refuse_launched
            )
        return DriverResult(
            ok=False,
            text=(
                "launching an application is not available with the agent-desktop driver "
                f"on this platform; start it yourself, then call {TOOL_LIST_APPS}"
            ),
        )

    def _windows(self) -> tuple[AppRef, ...]:
        """Visible, accessible windows from ``list-windows``, one AppRef per window."""
        data = self._call("list-windows", session=False) or []
        bundles = self._bundle_ids()
        apps: list[AppRef] = []
        for win in data if isinstance(data, list) else []:
            if not isinstance(win, dict) or not win.get("visible") or not win.get("accessible"):
                continue
            window_id = self._window_number_for(str(win.get("id") or ""))
            if window_id is None:
                continue
            pid = int(win.get("pid") or 0)
            apps.append(
                AppRef(
                    name=str(win.get("app_name") or ""),
                    pid=pid,
                    bundle_id=bundles.get(pid, ""),
                    window_id=window_id,
                    window_title=_clean(win.get("title"), 500),
                )
            )
        with self._lock:
            if len(self._window_ids) > _MAX_WINDOW_IDS:
                live = {a.window_id for a in apps}
                self._window_ids = {k: v for k, v in self._window_ids.items() if k in live}
        return tuple(apps)

    def _bundle_ids(self) -> dict[int, str]:
        """pid -> ``bundle_id`` from ``list-apps`` (the desktop id on Linux), best effort.

        ``list-windows`` carries no bundle id, and the denylist matches on one. A
        failed ``list-apps`` (macOS inside the gateway sandbox denies its ``ps``)
        leaves the name and window title, which the denylist also matches.
        """
        try:
            data = self._call("list-apps", session=False) or {}
        except AgentDesktopError:
            return {}
        rows = data.get("apps") if isinstance(data, dict) else data
        out: dict[int, str] = {}
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and isinstance(row.get("bundle_id"), str):
                try:
                    out[int(row.get("pid") or 0)] = _clean(row["bundle_id"], 200)
                except (TypeError, ValueError):
                    continue
        return out

    def _window_number_for(self, raw: str) -> "int | None":
        """Our int id for agent-desktop window id *raw*, recording opaque ones."""
        number = _window_number(raw)
        if number is not None or not raw:
            return number
        number = (zlib.crc32(raw.encode("utf-8")) & 0x7FFFFFFF) or 1
        with self._lock:
            # Linear probe on the (rare) crc collision between two live windows.
            while self._window_ids.get(number, raw) != raw:
                number = number % 0x7FFFFFFF + 1
            self._window_ids[number] = raw
        return number

    def _resolve(self, query: str) -> AppRef:
        wanted = query.strip().lower()
        apps = self._windows()
        exact = [a for a in apps if a.name.lower() == wanted]
        pool = exact or [a for a in apps if a.name.lower().startswith(wanted)]
        names = sorted({a.name for a in pool})
        if not pool:
            raise ComputerUseError(f"no application with an on-screen window matches '{query}'")
        if len(names) > 1:
            raise ComputerUseError(f"'{query}' matches several applications: {', '.join(names)}")
        return pool[0]

    def snapshot(self, app: AppRef, req: SnapshotRequest) -> DriverResult:
        def run() -> DriverResult:
            data = self._call(
                "snapshot",
                *self._target(app),
                "--include-bounds",
                "--max-depth",
                str(max(1, min(_MAX_AD_DEPTH, req.max_depth))),
            )
            snap, refs = build_snapshot(app, data or {}, req)
            with self._lock:
                self._refs[app.window_key] = refs
            if req.want_image and self._capture is not None:
                snap = self._capture(snap, req.image_max_px, req.image_quality)
            elif req.want_image and self._cli_capture:
                from kiro_crew.computer_use import capture_agent_desktop

                snap = capture_agent_desktop.capture_snapshot_image(
                    snap,
                    lambda: self._call("screenshot", *self._target(app), session=False),
                    max_px=req.image_max_px,
                    quality=req.image_quality,
                )
            return DriverResult(ok=True, app=app, snapshot=snap)

        return _guarded("snapshot", run)

    def _target(self, app: AppRef) -> list[str]:
        if app.window_id:
            with self._lock:
                opaque = self._window_ids.get(app.window_id)
            return ["--window-id", opaque or f"w-{app.window_id}"]
        # ``=`` form: a name that starts with ``-`` stays this flag's value.
        return [f"--app={app.name}"]

    # ── refs ──

    def _ref(self, app: AppRef, rec: ElementRec) -> str:
        with self._lock:
            entry = self._refs.get(app.window_key, {}).get(rec.index)
        if entry is None:
            raise ComputerUseError(
                f"element {rec.index} ({render.describe_record(rec)}) is not addressable "
                "(static text or a container); act on the control that encloses it"
            )
        ref, fp = entry
        if fp != render.fingerprint(rec):
            raise ComputerUseError(
                ERR_INDEX_DRIFT.format(
                    index=rec.index,
                    tool=TOOL_GET_STATE,
                    before=render.describe_record(rec),
                    after="a different element",
                )
            )
        return ref

    def _refuse_if_secure_now(self, app: AppRef, rec: ElementRec, ref: str) -> list[str]:
        """Re-read the target's states and refuse input to a field that is secure NOW.

        Returns the states it read, so a caller can check focus off the same read.
        """
        data = self._call("get", "--property", "states", args=(ref,)) or {}
        raw = data.get("value") if isinstance(data, dict) else None
        states = [str(s) for s in raw if isinstance(s, str)] if isinstance(raw, list) else []
        role = self._call("get", "--property", "role", args=(ref,)) or {}
        role_value = str(role.get("value") or "") if isinstance(role, dict) else ""
        if _is_secure(role_value, states):
            raise ComputerUseError(
                REFUSAL_SECURE_TARGET.format(
                    index=rec.index, app=app.bundle_id or app.name, subrole=role_value or "secure"
                )
            )
        return states

    # ── input ──

    def click(self, app: AppRef, rec: "ElementRec | None", req: ClickRequest) -> DriverResult:
        def run() -> DriverResult:
            if rec is not None:
                return DriverResult(ok=True, text=self._click_element(app, rec, req))
            if req.point is None:
                raise ComputerUseError("a click needs an element or a point")
            if self._native_ok():
                # Every point click, ``global`` included: the helper confines a
                # real-pointer click to the authorized window (``pid_owns_point`` /
                # ``hwnd_owns_point``); agent-desktop's ``mouse-click`` checks nothing.
                return self._native.click(app, req)  # type: ignore[union-attr]
            if req.moves_pointer:
                raise ComputerUseError(_REFUSE_UNCONFINED_POINTER)
            raise ComputerUseError(
                f"click_method '{req.method}' clicks a point without moving the pointer, "
                "which the agent-desktop driver cannot do on this platform; pass an "
                "element_index, or name click_method 'global' to accept the cursor move"
            )

        return _guarded("click", run)

    def _click_element(self, app: AppRef, rec: ElementRec, req: ClickRequest) -> str:
        ref = self._ref(app, rec)
        if req.moves_pointer:
            if self._native is None:
                raise ComputerUseError(_REFUSE_UNCONFINED_POINTER)
            # agent-desktop would aim the real pointer at the element's centre with no
            # check of which window owns that pixel. The point form is confined.
            raise ComputerUseError(
                f"click_method '{req.method}' moves the real pointer; pass x and y "
                "instead of an element_index, so the point can be checked against the "
                "window it lands on"
            )
        if req.method != CLICK_METHOD_ACCESSIBILITY:
            raise ComputerUseError(
                f"click_method '{req.method}' addresses a point; with an element_index use "
                "'accessibility'"
            )
        if req.button == MOUSE_BUTTON_RIGHT and req.count == 1:
            command = "right-click"
        elif req.button == MOUSE_BUTTON_LEFT and req.count == 1:
            command = "click"
        elif self._native is None:
            raise ComputerUseError(
                f"a {req.button} click x{req.count} on an element needs physical input, "
                "which is not available on this platform; use a single left click or "
                "computer_perform_action"
            )
        else:
            raise ComputerUseError(
                f"a {req.button} click x{req.count} on an element needs physical input; "
                "use click_method 'global' with x and y"
            )
        self._call(command, args=(ref,))
        return f"{command}ed element {rec.index}"

    def drag(self, app: AppRef, req: DragRequest) -> DriverResult:
        def run() -> DriverResult:
            if self._native_ok():
                return self._native.drag(app, req)  # type: ignore[union-attr]
            if not req.moves_pointer:
                raise ComputerUseError(
                    "a drag that leaves the pointer alone is not available with the "
                    "agent-desktop driver on this platform"
                )
            raise ComputerUseError(_REFUSE_UNCONFINED_POINTER)

        return _guarded("drag", run)

    def _focus_for_keys(
        self, app: AppRef, rec: ElementRec, *, headed: "bool | None" = None
    ) -> None:
        """Give *rec* keyboard focus through its ref, and verify it holds it.

        The keys themselves then go to whatever holds focus, so the native helper
        never takes an element. Refuses, sending nothing, when the field is secure before or
        after the focus move, or when focus did not land on it: keystrokes past a
        failed aim reach whatever the app had focused, which can be a password box.

        On Windows agent-desktop moves focus only with ``--headed`` (a headless
        ``focus`` is ``POLICY_DENIED``: it raises the window). ``SendInput`` needs
        that foreground window anyway, and it does not move the pointer.
        """
        ref = self._ref(app, rec)
        self._refuse_if_secure_now(app, rec, ref)
        if headed is None:
            headed = self._platform_id == PLATFORM_WINDOWS
        self._call("focus", args=(ref,), headed=headed)
        states = self._refuse_if_secure_now(app, rec, ref)
        if "focused" not in states:
            raise ComputerUseError(
                f"element {rec.index} ({render.describe_record(rec)}) did not take keyboard "
                "focus, so nothing was sent; call computer_get_state and address the field "
                "that should receive the keys"
            )

    def type_text(self, app: AppRef, rec: "ElementRec | None", text: str) -> DriverResult:
        def run() -> DriverResult:
            if rec is None:
                if self._native_ok():
                    return self._native.type_text(app, text)  # type: ignore[union-attr]
                raise ComputerUseError("pass the element_index of the field to type into")
            if self._platform_id == PLATFORM_WINDOWS:
                # agent-desktop's Windows ``type`` is SendInput and refuses headless,
                # so this verb is the focus move plus the native key route.
                if not self._native_ok():
                    raise ComputerUseError(
                        "typing on Windows needs the native helper, which did not load; "
                        f"use {TOOL_SET_VALUE} on element {rec.index} instead"
                    )
                self._focus_for_keys(app, rec)
                result = self._native.type_text(app, text)  # type: ignore[union-attr]
                return _with_focus_note(result, rec)
            ref = self._ref(app, rec)
            self._refuse_if_secure_now(app, rec, ref)
            try:
                self._call("type", args=(ref, text))
            except AgentDesktopError as exc:
                # Without a native helper (Linux) some toolkits ignore accessibility
                # text writes (Firefox: "accepts EditableText writes and applies none
                # of them"). Retry once as keyboard input: ``--headed`` lets the field
                # take keyboard focus. It sends keys, not pointer events; measured in
                # the Linux container, the pointer did not move. Never with a native
                # driver, so macOS and Windows keep their own delivery.
                if exc.code != _NOT_SUPPORTED or exc.delivered or self._native is not None:
                    raise
                self._focus_for_keys(app, rec, headed=True)
                self._call("type", args=(ref, text), headed=True)
                return DriverResult(
                    ok=True,
                    text=(
                        f"typed {len(text)} characters into element {rec.index} as keyboard "
                        "input (the application ignored the accessibility text write, so "
                        "its window took keyboard focus)"
                    ),
                )
            return DriverResult(
                ok=True, text=f"typed {len(text)} characters into element {rec.index}"
            )

        return _guarded("type_text", run)

    def press_key(self, app: AppRef, rec: "ElementRec | None", key: str) -> DriverResult:
        def run() -> DriverResult:
            native = self._native_ok()
            if rec is not None and native:
                self._focus_for_keys(app, rec)
            elif rec is not None:
                if not rec.focused:
                    # Without a native helper (Linux) agent-desktop's ``focus`` needs
                    # ``--headed``, which moves the application's keyboard focus; a key
                    # press does not get to do that. A key sent anyway would land on
                    # whatever IS focused.
                    raise ComputerUseError(
                        f"element {rec.index} is not focused, and focusing it here would "
                        "move the application's keyboard focus; pass the element marked "
                        f"<focused> in {TOOL_GET_STATE}, or click element {rec.index} first"
                    )
                states = self._refuse_if_secure_now(app, rec, self._ref(app, rec))
                if "focused" not in states:
                    # The snapshot said focused; the live read says otherwise, so the
                    # window-scoped press would reach some other control.
                    raise ComputerUseError(
                        f"element {rec.index} no longer holds keyboard focus, so nothing "
                        f"was sent; call {TOOL_GET_STATE} again"
                    )
            # The native helper posts keys to the process directly. agent-desktop's
            # ``press`` resolves its target through an app inventory that shells out
            # to ``ps``, which the gateway's macOS sandbox denies.
            if native:
                result = self._native.press_key(app, key)  # type: ignore[union-attr]
                return _with_focus_note(result, rec)
            self._call("press", *self._target(app), args=(normalize_key(key),), session=False)
            return DriverResult(ok=True, text=f"pressed {key}")

        return _guarded("press_key", run)

    def set_value(self, app: AppRef, rec: ElementRec, value: str) -> DriverResult:
        def run() -> DriverResult:
            ref = self._ref(app, rec)
            self._refuse_if_secure_now(app, rec, ref)
            try:
                self._call("set-value", args=(ref, value))
            except AgentDesktopError as exc:
                if exc.code != _NOT_SUPPORTED:
                    raise
                raise ComputerUseError(
                    f"{exc}; this application does not take a value set through "
                    "accessibility, use computer_type_text on the field instead"
                ) from exc
            return DriverResult(ok=True, text=f"set the value of element {rec.index}")

        return _guarded("set_value", run)

    def scroll(self, app: AppRef, rec: ElementRec, direction: str, pages: float) -> DriverResult:
        def run() -> DriverResult:
            amount = max(1, min(1000, round(pages)))
            try:
                self._call(
                    "scroll",
                    "--direction",
                    direction,
                    "--amount",
                    str(amount),
                    args=(self._ref(app, rec),),
                )
            except AgentDesktopError as exc:
                # On Linux agent-desktop scrolls only with the physical wheel, which
                # aims the real pointer, so the headless call is POLICY_DENIED. It is
                # never retried with ``--headed``: computer_scroll does not ask to
                # move the pointer.
                if exc.code != _POLICY_DENIED:
                    raise
                raise ComputerUseError(
                    "scrolling here needs the physical mouse wheel, which moves the real "
                    "pointer; use computer_perform_action ScrollTo on the element to bring "
                    "into view, or computer_press_key pagedown on the focused element"
                ) from exc
            return DriverResult(
                ok=True, text=f"scrolled element {rec.index} {direction} by {amount}"
            )

        return _guarded("scroll", run)

    def perform_action(self, app: AppRef, rec: ElementRec, action: str) -> DriverResult:
        def run() -> DriverResult:
            command = _ACTION_COMMANDS.get(_fold(action))
            if command is None:
                raise ComputerUseError(
                    f"'{action}' is not an action this driver can perform; element "
                    f"{rec.index} advertises: {', '.join(rec.actions) or 'none'}"
                )
            offered = {_fold(a) for a in rec.actions}
            if offered and _ADVERTISED[command] not in offered:
                raise ComputerUseError(
                    f"element {rec.index} does not advertise {action}; it advertises: "
                    f"{', '.join(rec.actions)}"
                )
            self._call(command, args=(self._ref(app, rec),))
            return DriverResult(ok=True, text=f"performed {action} on element {rec.index}")

        return _guarded("perform_action", run)

    def close(self) -> None:
        with self._lock:
            self._refs.clear()
        if self._native is not None:
            try:
                self._native.close()
            except Exception:
                logger.debug("native fallback close() failed", exc_info=True)


# ── snapshot translation ──


def build_snapshot(
    app: AppRef, data: dict, req: SnapshotRequest
) -> "tuple[Snapshot, dict[int, tuple[str, str]]]":
    """Translate one ``snapshot`` envelope's ``data`` into a Snapshot plus its ref map.

    Every node is CLASSIFIED (secure or not) even past the node budget, so
    ``has_secure`` means "none present in what agent-desktop walked". An incomplete
    walk (``complete: false``) marks the snapshot truncated, which keeps the
    screenshot capture failing closed exactly as it does for the native walkers.
    """
    tree = data.get("tree") if isinstance(data, dict) else None
    window = data.get("window") if isinstance(data, dict) else None
    window_title = _clean((window or {}).get("title") if isinstance(window, dict) else "", 500)
    origin = _bounds(tree.get("bounds")) if isinstance(tree, dict) else None
    records: list[ElementRec] = []
    refs: dict[int, tuple[str, str]] = {}
    state = {"truncated": not bool(data.get("complete", True)), "depth": False, "secure": False}
    focused_index: list[int] = []

    def walk(node: dict, depth: int) -> None:
        role = str(node.get("role") or "")
        states = [str(s) for s in node.get("states") or [] if isinstance(s, str)]
        secure = _is_secure(role, states)
        state["secure"] = state["secure"] or secure
        children = [c for c in node.get("children") or [] if isinstance(c, dict)]
        name = _clean(node.get("name"), req.text_limit)
        value = "" if secure else _clean(node.get("value"), req.text_limit)
        if not name and not secure:
            name = _clean(node.get("description"), req.text_limit)
        ref = node.get("ref_id") if isinstance(node.get("ref_id"), str) else ""
        actions = tuple(str(a) for a in node.get("available_actions") or [] if isinstance(a, str))
        elide = role in _ELIDABLE_ROLES and not (name or value or ref) and bool(children)
        next_depth = depth
        if not elide:
            if depth >= req.max_depth:
                state["depth"] = state["depth"] or bool(children or node.get("children_count"))
                return
            if len(records) >= req.max_nodes:
                state["truncated"] = True
            else:
                rec = ElementRec(
                    index=len(records),
                    role=role,
                    subrole=_SECURE_STATE if secure else "",
                    title=name,
                    value=value,
                    actions=actions,
                    depth=depth,
                    secure=secure,
                    enabled="disabled" not in states,
                    frame=_local_frame(_bounds(node.get("bounds")), origin),
                    traits=_traits(states, actions),
                )
                records.append(rec)
                if ref:
                    refs[rec.index] = (ref, render.fingerprint(rec))
                if "focused" in states:
                    focused_index.append(rec.index)
            next_depth = depth + 1
        for child in children:
            walk(child, next_depth)

    if isinstance(tree, dict):
        walk(tree, 0)
    if focused_index:
        # agent-desktop marks the focus chain; the focused element is its deepest.
        last = focused_index[-1]
        records[last] = replace(records[last], focused=True)
    snap = Snapshot(
        app=app,
        elements=tuple(records),
        window_title=window_title or app.window_title,
        captured_at=time.monotonic(),
        truncated=state["truncated"],
        depth_truncated=state["depth"],
        has_secure=state["secure"],
        window_bounds=origin,
    )
    return snap, refs


def _is_secure(role: str, states: Iterable[str]) -> bool:
    return _SECURE_STATE in states or role.lower() in _SECURE_ROLES


def _traits(states: list[str], actions: tuple[str, ...]) -> tuple[str, ...]:
    words: list[str] = []
    if "selected" in states or "checked" in states:
        words.append(TRAIT_SELECTED)
    if "expanded" in states:
        words.append(TRAIT_EXPANDED)
    if "SetValue" in actions and "readonly" not in states:
        words.append(TRAIT_EDITABLE)
    return tuple(words)


def _bounds(raw: object) -> "tuple[float, float, float, float] | None":
    if not isinstance(raw, dict):
        return None
    try:
        return (float(raw["x"]), float(raw["y"]), float(raw["width"]), float(raw["height"]))
    except (KeyError, TypeError, ValueError):
        return None


def _local_frame(
    frame: "tuple[float, float, float, float] | None",
    origin: "tuple[float, float, float, float] | None",
) -> "tuple[float, float, float, float] | None":
    """Screen rect -> window-local rect; ``None`` when either side is unknown."""
    if frame is None or origin is None:
        return None
    return (frame[0] - origin[0], frame[1] - origin[1], frame[2], frame[3])


def _clean(raw: object, limit: int) -> str:
    if raw is None or isinstance(raw, (dict, list)):
        return ""
    text = _CONTROL_CHARS.sub(" ", str(raw)).strip()
    return text[:limit]


def _window_number(raw: object) -> "int | None":
    text = str(raw or "")
    if text.startswith("w-"):
        text = text[2:]
    return int(text) if text.isdigit() else None


def _fold(name: str) -> str:
    return re.sub(r"[^a-z]", "", name.lower())


def normalize_key(key: str) -> str:
    """Our key spec (``cmd+shift+a``, ``option+tab``) in agent-desktop's spelling."""
    parts = [p.strip().lower() for p in key.split("+")]
    return "+".join(_KEY_ALIASES.get(p, p) for p in parts if p)


def _permission_state(raw: object) -> str:
    state = raw.get("state") if isinstance(raw, dict) else None
    if state == "granted":
        return PERMISSION_GRANTED
    if state in ("denied", "missing", "not_granted"):
        return PERMISSION_MISSING
    return PERMISSION_UNKNOWN


def _parse_envelope(out: str) -> "dict | None":
    """The last JSON object agent-desktop printed, or ``None``."""
    for line in reversed((out or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                return parsed
    try:
        parsed = json.loads(out)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _envelope_error(command: str, error: dict) -> AgentDesktopError:
    """A model-facing refusal from agent-desktop's error object.

    Branches on ``code`` and ``disposition`` only, as agent-desktop documents; its
    message is appended for detail. A result that may have been delivered says so,
    because repeating it could click or type twice.
    """
    code = str(error.get("code") or "ERROR")
    message = _clean(error.get("message"), 400)
    raw = error.get("disposition")
    disposition: dict = raw if isinstance(raw, dict) else {}
    delivered = str(disposition.get("delivery") or "not_delivered") != "not_delivered"
    raw_details = error.get("details")
    cause = str(raw_details.get("cause") or "") if isinstance(raw_details, dict) else ""
    if cause == _FOCUS_UNCONFIRMED:
        # Not a stale tree: a fresh computer_get_state cannot fix it, so saying so
        # would send the model round a loop.
        text = (
            f"agent-desktop {command} needs the target window in front and could not "
            "confirm it is; the key was not sent. Bring that window to the front first, "
            "or type into one of its fields with an element_index"
        )
    elif code in _STALE_CODES:
        text = f"the window changed since the last {TOOL_GET_STATE}; call it again ({code})"
    elif code == "PERM_DENIED":
        text = f"agent-desktop lacks an OS permission for {command}: {message}"
    elif code == "POLICY_DENIED":
        # On Windows agent-desktop refuses, headless, every element verb it can only
        # deliver as real input (right-click, double-click, focus, type): measured.
        text = (
            f"agent-desktop {command} needs real mouse or keyboard input on this platform, "
            f"which an element action does not use ({message}); pass x and y with "
            "click_method 'global' to accept the cursor move, or use another element action"
        )
    else:
        text = f"agent-desktop {command} failed ({code}): {message}"
    if delivered:
        text += (
            f"; the action may have been delivered, so check with {TOOL_GET_STATE} before retrying"
        )
    return AgentDesktopError(text, code=code, delivered=delivered)


def _with_focus_note(result: DriverResult, rec: "ElementRec | None") -> DriverResult:
    """Say that focus moved, on a success that moved it, so the model knows the caret did."""
    if rec is None or not result.ok:
        return result
    return replace(result, text=f"{result.text} (keyboard focus moved to element {rec.index})")


def _guarded(label: str, run: "Callable[[], DriverResult]") -> DriverResult:
    """Run *run*; convert every failure into ``DriverResult(ok=False)``."""
    try:
        return run()
    except ComputerUseError as exc:
        return DriverResult(ok=False, text=str(exc))
    except Exception as exc:
        logger.warning("agent-desktop %s failed: %s", label, type(exc).__name__, exc_info=True)
        return DriverResult(ok=False, text=f"{label} failed unexpectedly ({type(exc).__name__})")


def state_home_for(prefix: "Optional[str]") -> "Optional[str]":
    """agent-desktop's state dir (refstore, snapshots) beside its install, 0700."""
    if not prefix:
        return None
    path = os.path.join(prefix, "state")
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    except OSError:
        return None
    return path


__all__ = [
    "AgentDesktopBackend",
    "AgentDesktopError",
    "SESSION_ID",
    "build_snapshot",
    "normalize_key",
    "state_home_for",
]
