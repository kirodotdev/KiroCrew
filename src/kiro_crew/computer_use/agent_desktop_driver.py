"""A :class:`ComputerUseBackend` over the agent-desktop CLI.

agent-desktop (vercel-labs/agent-desktop, Apache-2.0) reads and drives the native
accessibility tree on macOS, Windows and Linux. Every command prints one JSON
envelope (``{"ok": true, "data": ...}`` or ``{"ok": false, "error": {...}}``), and
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
  keys. ``--headed`` is passed ONLY for a request whose ``moves_pointer`` is True.
* **What agent-desktop cannot do** is a coordinate gesture that leaves the pointer
  alone (``app_post``, ``sky_click``): its ``mouse-click`` and ``drag`` require
  ``--headed``. Those, the window inventory (bundle ids, which its own
  ``list-apps`` cannot read inside the gateway sandbox because it shells out to
  ``ps``), the launch catalog and the screenshot are taken from the platform's
  native driver when one is supplied. Without one they refuse, or for the
  screenshot degrade to tree-only as the seam contract allows.

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
from dataclasses import replace
from typing import Any, Callable, Iterable, Optional

from kiro_crew.computer_use import agent_desktop_locate, render
from kiro_crew.computer_use.backend import ComputerUseBackend
from kiro_crew.computer_use.types import (
    CLICK_METHOD_ACCESSIBILITY,
    ERR_INDEX_DRIFT,
    MOUSE_BUTTON_LEFT,
    MOUSE_BUTTON_RIGHT,
    PERMISSION_GRANTED,
    PERMISSION_MISSING,
    PERMISSION_UNKNOWN,
    REFUSAL_SECURE_TARGET,
    TOOL_GET_STATE,
    TOOL_LIST_APPS,
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

#: Error codes meaning "the tree moved": the remedy is a fresh computer_get_state.
_STALE_CODES = frozenset({"STALE_REF", "SNAPSHOT_NOT_FOUND", "AMBIGUOUS_TARGET"})

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
        native: "ComputerUseBackend | None" = None,
        capture: "Callable[[Snapshot, int, int], Snapshot] | None" = None,
        runner: "Runner | None" = None,
        state_home: "str | None" = None,
    ) -> None:
        self._binary = binary
        self._platform_id = platform_id
        self._native = native
        self._capture = capture
        self._run = runner or _subprocess_runner
        self._state_home = state_home
        self._lock = threading.Lock()
        #: window_key -> {our index: (agent-desktop ref, fingerprint)} of the latest walk.
        self._refs: dict[str, dict[int, tuple[str, str]]] = {}
        self._status: "BackendStatus | None" = None

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
        if self._status is None:
            self._status = self._probe_status()
        return self._status

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
        return BackendStatus(supported=True, platform_id=self._platform_id)

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
        """Launch through the native driver's vetted catalog, or refuse.

        agent-desktop's ``launch`` accepts arguments, environment and a working
        directory, and resolves names its own way; the seam requires an OS catalog
        that refuses a path and is re-checked against the operator's policy before
        spawning. The native drivers implement exactly that, so they keep the verb.
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
        apps: list[AppRef] = []
        for win in data if isinstance(data, list) else []:
            if not isinstance(win, dict) or not win.get("visible") or not win.get("accessible"):
                continue
            window_id = _window_number(win.get("id"))
            if window_id is None:
                continue
            apps.append(
                AppRef(
                    name=str(win.get("app_name") or ""),
                    pid=int(win.get("pid") or 0),
                    window_id=window_id,
                    window_title=_clean(win.get("title"), 500),
                )
            )
        return tuple(apps)

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
            return DriverResult(ok=True, app=app, snapshot=snap)

        return _guarded("snapshot", run)

    def _target(self, app: AppRef) -> list[str]:
        if app.window_id:
            return ["--window-id", f"w-{app.window_id}"]
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

    def _refuse_if_secure_now(self, app: AppRef, rec: ElementRec, ref: str) -> None:
        """Re-read the target's states and refuse input to a field that is secure NOW."""
        data = self._call("get", "--property", "states", args=(ref,)) or {}
        states = data.get("value") if isinstance(data, dict) else None
        role = self._call("get", "--property", "role", args=(ref,)) or {}
        role_value = str(role.get("value") or "") if isinstance(role, dict) else ""
        if _is_secure(role_value, states if isinstance(states, list) else []):
            raise ComputerUseError(
                REFUSAL_SECURE_TARGET.format(
                    index=rec.index, app=app.bundle_id or app.name, subrole=role_value or "secure"
                )
            )

    # ── input ──

    def click(self, app: AppRef, rec: "ElementRec | None", req: ClickRequest) -> DriverResult:
        def run() -> DriverResult:
            if rec is not None:
                return DriverResult(ok=True, text=self._click_element(app, rec, req))
            if req.point is None:
                raise ComputerUseError("a click needs an element or a point")
            if req.moves_pointer:
                x, y = req.point
                self._call(
                    "mouse-click",
                    f"--xy={x:g},{y:g}",
                    "--button",
                    req.button,
                    "--count",
                    str(req.count),
                    headed=True,
                    session=False,
                )
                return DriverResult(
                    ok=True, text=f"clicked at ({x:g}, {y:g}) with the real pointer"
                )
            if self._native_ok():
                return self._native.click(app, None, req)  # type: ignore[union-attr]
            raise ComputerUseError(
                f"click_method '{req.method}' clicks a point without moving the pointer, "
                "which the agent-desktop driver cannot do on this platform; pass an "
                "element_index, or name click_method 'global' to accept the cursor move"
            )

        return _guarded("click", run)

    def _click_element(self, app: AppRef, rec: ElementRec, req: ClickRequest) -> str:
        ref = self._ref(app, rec)
        headed = req.moves_pointer
        if req.method != CLICK_METHOD_ACCESSIBILITY and not headed:
            raise ComputerUseError(
                f"click_method '{req.method}' addresses a point; with an element_index use "
                "'accessibility'"
            )
        if req.button == MOUSE_BUTTON_RIGHT and req.count == 1:
            command = "right-click"
        elif req.button == MOUSE_BUTTON_LEFT and req.count == 1:
            command = "click"
        elif req.button == MOUSE_BUTTON_LEFT and req.count in (2, 3) and headed:
            command = "double-click" if req.count == 2 else "triple-click"
        else:
            raise ComputerUseError(
                f"a {req.button} click x{req.count} on an element needs physical input; "
                "use click_method 'global' with x and y"
            )
        self._call(command, args=(ref,), headed=headed)
        return f"{command}ed element {rec.index}"

    def drag(self, app: AppRef, req: DragRequest) -> DriverResult:
        def run() -> DriverResult:
            if self._native_ok():
                return self._native.drag(app, req)  # type: ignore[union-attr]
            if not req.moves_pointer:
                raise ComputerUseError(
                    "a drag that leaves the pointer alone is not available with the "
                    "agent-desktop driver on this platform; name click_method 'global'"
                )
            if req.steps > 1:
                raise ComputerUseError(
                    "the agent-desktop driver draws straight drags only; pass steps 1"
                )
            (x0, y0), (x1, y1) = req.start, req.end
            self._call(
                "drag",
                f"--from-xy={x0:g},{y0:g}",
                f"--to-xy={x1:g},{y1:g}",
                headed=True,
                session=False,
            )
            return DriverResult(ok=True, text=f"dragged from ({x0:g}, {y0:g}) to ({x1:g}, {y1:g})")

        return _guarded("drag", run)

    def type_text(self, app: AppRef, rec: "ElementRec | None", text: str) -> DriverResult:
        def run() -> DriverResult:
            if rec is None:
                if self._native_ok():
                    return self._native.type_text(app, None, text)  # type: ignore[union-attr]
                raise ComputerUseError("pass the element_index of the field to type into")
            ref = self._ref(app, rec)
            self._refuse_if_secure_now(app, rec, ref)
            self._call("type", args=(ref, text))
            return DriverResult(
                ok=True, text=f"typed {len(text)} characters into element {rec.index}"
            )

        return _guarded("type_text", run)

    def press_key(self, app: AppRef, rec: "ElementRec | None", key: str) -> DriverResult:
        def run() -> DriverResult:
            # The native driver posts keys to the process directly. agent-desktop's
            # ``press`` resolves its target through an app inventory that shells out
            # to ``ps``, which the gateway's macOS sandbox denies.
            if self._native_ok():
                return self._native.press_key(app, rec, key)  # type: ignore[union-attr]
            if rec is not None:
                self._call("focus", args=(self._ref(app, rec),))
            self._call("press", *self._target(app), args=(normalize_key(key),), session=False)
            return DriverResult(ok=True, text=f"pressed {key}")

        return _guarded("press_key", run)

    def set_value(self, app: AppRef, rec: ElementRec, value: str) -> DriverResult:
        def run() -> DriverResult:
            ref = self._ref(app, rec)
            self._refuse_if_secure_now(app, rec, ref)
            self._call("set-value", args=(ref, value))
            return DriverResult(ok=True, text=f"set the value of element {rec.index}")

        return _guarded("set_value", run)

    def scroll(self, app: AppRef, rec: ElementRec, direction: str, pages: float) -> DriverResult:
        def run() -> DriverResult:
            amount = max(1, min(1000, round(pages)))
            self._call(
                "scroll",
                "--direction",
                direction,
                "--amount",
                str(amount),
                args=(self._ref(app, rec),),
            )
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
    if code in _STALE_CODES:
        text = f"the window changed since the last {TOOL_GET_STATE}; call it again ({code})"
    elif code == "PERM_DENIED":
        text = f"agent-desktop lacks an OS permission for {command}: {message}"
    else:
        text = f"agent-desktop {command} failed ({code}): {message}"
    if delivered:
        text += (
            f"; the action may have been delivered, so check with {TOOL_GET_STATE} before retrying"
        )
    return AgentDesktopError(text, code=code, delivered=delivered)


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
