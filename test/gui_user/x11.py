"""X11 backend for the GUI user-test harness.

The model sees screenshots and emits actions; this module turns those actions
into pixels-in / input-events-out on a virtual X display:

* screenshots via Pillow ``ImageGrab.grab(xdisplay=...)``, downscaled to a
  fixed width so the model's pixel estimates stay 1:1 with what it was shown;
* input via ``xdotool`` (mouse move / click / drag, key chords, typed text).

Everything that does not need a display -- coordinate scaling, key-name
normalisation, argv construction, the action vocabulary -- is a pure function
so it is unit-testable on any host. Only :class:`Display` touches X11.

Coordinate system: every coordinate the model gives or receives is in
SCREENSHOT space (``Geometry.shot_w`` wide). ``Geometry.to_real`` scales to the
display and clamps, so an off-by-estimate click lands on the screen edge rather
than raising.

Safety: the backend refuses a display that is not a private virtual one
(``:0`` and friends are a real desktop) and caps typed text, so a prompt
injection read off a screenshot cannot type a novel into the target.
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

#: Actions the backend executes. Mirrors the Anthropic ``computer_2025*`` tool's
#: action names so a native computer-use tool call maps 1:1; the custom-tool
#: fallback uses the same names as tool names.
ACTIONS: frozenset[str] = frozenset(
    {
        "screenshot",
        "left_click",
        "right_click",
        "middle_click",
        "double_click",
        "triple_click",
        "mouse_move",
        "left_click_drag",
        "type",
        "key",
        "scroll",
        "wait",
    }
)

#: Actions the native tool may emit that this backend deliberately does not
#: support. They get a structured error result, never an exception.
UNSUPPORTED_ACTIONS: frozenset[str] = frozenset(
    {"zoom", "hold_key", "left_mouse_down", "left_mouse_up", "cursor_position"}
)

#: Longest string ``type`` will emit in one action. A test scenario types a
#: handful of words; anything longer is a runaway or an injection.
MAX_TYPE_CHARS = 400

#: Longest ``wait`` in seconds. The harness has its own wall-clock gate; this
#: just stops one action from eating the whole budget.
MAX_WAIT_SECONDS = 10.0

#: Wheel clicks per ``scroll`` action, capped so "scroll_amount: 999" is bounded.
MAX_SCROLL_CLICKS = 20

# xdotool button numbers.
_BUTTONS = {"left": 1, "middle": 2, "right": 3}
_SCROLL_BUTTONS = {"up": 4, "down": 5, "left": 6, "right": 7}

# Model-friendly key names -> X keysym names xdotool understands. Anything not
# listed is passed through unchanged (xdotool already accepts ``ctrl+s``,
# ``Return``, ``F5`` ...), so this map only covers the aliases models actually
# produce. Keys are matched case-insensitively.
_KEY_ALIASES: dict[str, str] = {
    "enter": "Return",
    "return": "Return",
    "esc": "Escape",
    "escape": "Escape",
    "tab": "Tab",
    "space": "space",
    "backspace": "BackSpace",
    "delete": "Delete",
    "del": "Delete",
    "insert": "Insert",
    "home": "Home",
    "end": "End",
    "pageup": "Page_Up",
    "page_up": "Page_Up",
    "pgup": "Page_Up",
    "pagedown": "Page_Down",
    "page_down": "Page_Down",
    "pgdn": "Page_Down",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "arrowup": "Up",
    "arrowdown": "Down",
    "arrowleft": "Left",
    "arrowright": "Right",
    "ctrl": "ctrl",
    "control": "ctrl",
    "cmd": "super",
    "command": "super",
    "win": "super",
    "super": "super",
    "meta": "super",
    "alt": "alt",
    "option": "alt",
    "shift": "shift",
}
_FKEY_RE = re.compile(r"^f([1-9]|1[0-2])$")

_MODIFIERS = frozenset({"ctrl", "alt", "shift", "super"})

#: Modifier chords the model may press, after :func:`normalize_key`. Everything
#: else with a modifier is refused: ``ctrl+o`` / ``ctrl+s`` open the runner's
#: file picker, ``ctrl+u`` / ``ctrl+shift+i`` / ``F12`` open source or devtools,
#: ``ctrl+n`` / ``ctrl+t`` open windows the harness does not track, ``super``
#: reaches the window manager. Editing and in-page navigation chords stay, plus
#: ``ctrl+l`` because the harness navigates through the omnibox (and the browser
#: policy in boot.sh pins the reachable URLs to the loopback gateway).
KEY_CHORD_ALLOWLIST: frozenset[str] = frozenset(
    {
        "ctrl+a",
        "ctrl+c",
        "ctrl+v",
        "ctrl+x",
        "ctrl+z",
        "ctrl+y",
        "ctrl+l",
        "ctrl+Return",
        "ctrl+BackSpace",
        "ctrl+Delete",
        "ctrl+Home",
        "ctrl+End",
        "ctrl+Left",
        "ctrl+Right",
        "shift+Return",
        "shift+Tab",
        "shift+Left",
        "shift+Right",
        "shift+Up",
        "shift+Down",
        "shift+Home",
        "shift+End",
    }
)

#: Bare function keys the model may press. ``F5`` reloads; the rest open
#: devtools, fullscreen, menus or help.
FUNCTION_KEY_ALLOWLIST: frozenset[str] = frozenset({"F5"})

# Displays that are a REAL desktop, never a private test display. ``:0`` is the
# X default; ``:1`` is what a second logged-in seat gets. This is the cheap
# first check; :func:`verify_virtual_display` is the one that actually looks.
_REAL_DISPLAY_RE = re.compile(r"^(?:[^:]*)?:[01](?:\.\d+)?$")
_DISPLAY_NUMBER_RE = re.compile(r"^(?:[^:]*)?:(\d+)(?:\.\d+)?$")

#: Process names of X servers that render to memory, never to a monitor. Only a
#: display served by one of these is accepted; a real seat's server (``Xorg``,
#: ``Xwayland``, ``X``) is refused whatever its number.
VIRTUAL_X_SERVERS: frozenset[str] = frozenset({"Xvfb", "Xdcv", "Xvnc", "Xephyr", "Xdummy"})


class X11Error(RuntimeError):
    """The backend could not perform an action (bad input, xdotool failure)."""


def display_number(display: str) -> int:
    m = _DISPLAY_NUMBER_RE.match(display.strip())
    if not m:
        raise X11Error(f"DISPLAY {display!r} is not of the form [host]:N[.S]")
    return int(m.group(1))


def refuse_real_display(display: str) -> None:
    """Raise :class:`X11Error` unless ``display`` looks like a private virtual one.

    The lane must never be pointed at a person's desktop: pixel-level control
    is full control of everything on that screen. ``:0`` / ``:1`` (with or
    without a host prefix or screen suffix) are refused; an empty value too,
    because that would fall back to whatever ``DISPLAY`` the runner inherited.
    A remote host part is refused as well: the backend only ever drives a
    server on this machine, whose owning process it can inspect.
    """
    if not display or not display.strip():
        raise X11Error("DISPLAY is empty; refusing to fall back to the inherited desktop")
    if _REAL_DISPLAY_RE.match(display.strip()):
        raise X11Error(
            f"DISPLAY {display!r} is a real desktop, not a private virtual display; "
            "start an Xvfb (e.g. :99) and point the harness at it"
        )
    host = display.strip().rsplit(":", 1)[0]
    if host not in ("", "localhost", "unix"):
        raise X11Error(
            f"DISPLAY {display!r} names a remote host; only a local virtual display is driven"
        )
    display_number(display)


def server_process_name(
    display: str, *, lock_dir: Path = Path("/tmp"), proc: Path = Path("/proc")
) -> str:
    """Name of the X server process serving ``display`` (``Xvfb``, ``Xorg`` ...).

    An X server writes its pid to ``/tmp/.X<N>-lock``; the process's ``comm``
    is its executable name. Raises :class:`X11Error` when either cannot be
    read -- a display nothing owns is not one to drive.
    """
    n = display_number(display)
    lock = lock_dir / f".X{n}-lock"
    try:
        pid = int(lock.read_text(encoding="ascii").strip())
    except (OSError, ValueError) as exc:
        raise X11Error(
            f"cannot read the X lock file {lock} for DISPLAY {display!r}: {exc}"
        ) from exc
    try:
        return (proc / str(pid) / "comm").read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise X11Error(
            f"X lock {lock} names pid {pid}, which has no readable process: {exc}"
        ) from exc


def verify_virtual_display(
    display: str, *, server_name: Callable[[str], str] = server_process_name
) -> str:
    """Refuse unless ``display`` is served by a memory-only X server.

    Runs the cheap number check first, then reads the owning server's process
    name and requires one of :data:`VIRTUAL_X_SERVERS`. Returns the server
    name so callers can log it.
    """
    refuse_real_display(display)
    name = server_name(display)
    if name not in VIRTUAL_X_SERVERS:
        raise X11Error(
            f"DISPLAY {display!r} is served by {name!r}, not a virtual X server "
            f"({', '.join(sorted(VIRTUAL_X_SERVERS))}); refusing to drive a real screen"
        )
    return name


# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Geometry:
    """Real display size and the screenshot width the model is shown."""

    real_w: int
    real_h: int
    shot_w: int

    def __post_init__(self) -> None:
        if self.real_w <= 0 or self.real_h <= 0 or self.shot_w <= 0:
            raise ValueError("geometry dimensions must be positive")
        if self.shot_w > self.real_w:
            raise ValueError("shot_w must not exceed the real width (never upscale)")

    @property
    def scale(self) -> float:
        """``real = shot * scale``."""
        return self.real_w / self.shot_w

    @property
    def shot_h(self) -> int:
        return max(1, round(self.real_h / self.scale))

    def to_real(self, x: float, y: float) -> tuple[int, int]:
        """Screenshot-space -> real pixels, clamped to the display."""
        rx = int(round(x * self.scale))
        ry = int(round(y * self.scale))
        return (
            max(0, min(self.real_w - 1, rx)),
            max(0, min(self.real_h - 1, ry)),
        )


def parse_screen(spec: str) -> tuple[int, int]:
    """``"1600x1000"`` or ``"1600x1000x24"`` -> ``(1600, 1000)``."""
    m = re.match(r"^\s*(\d+)\s*x\s*(\d+)(?:\s*x\s*\d+)?\s*$", spec)
    if not m:
        raise ValueError(f"bad screen spec {spec!r}; expected WxH or WxHxDEPTH")
    return int(m.group(1)), int(m.group(2))


# --------------------------------------------------------------------------
# Key names
# --------------------------------------------------------------------------


def normalize_key(name: str) -> str:
    """Map a model-supplied key or chord to xdotool syntax.

    ``"ctrl+l"`` -> ``"ctrl+l"``; ``"Enter"`` -> ``"Return"``; ``"cmd+a"`` ->
    ``"super+a"``; ``"F5"`` -> ``"F5"``; ``"Page Down"`` -> ``"Page_Down"``.
    Single printable characters pass through so ``"a"`` and ``"+"`` still work.
    """
    raw = name.strip()
    if not raw:
        raise X11Error("empty key name")
    if raw == "+":
        return "plus"
    parts = [p.strip() for p in re.split(r"(?<!^)\+(?!$)", raw) if p.strip()]
    if not parts:
        raise X11Error(f"unparseable key chord {name!r}")
    out: list[str] = []
    for part in parts:
        token = part.replace(" ", "_")
        low = token.lower()
        if low in _KEY_ALIASES:
            out.append(_KEY_ALIASES[low])
        elif _FKEY_RE.match(low):
            out.append("F" + low[1:])
        else:
            out.append(token)
    return "+".join(out)


def check_key_allowed(chord: str) -> str:
    """Return a normalized chord if the model may press it, else raise :class:`X11Error`.

    Plain keys (letters, digits, punctuation, Return, Tab, arrows, Escape ...)
    are always fine. A chord with a modifier must be on
    :data:`KEY_CHORD_ALLOWLIST`; a function key on :data:`FUNCTION_KEY_ALLOWLIST`.
    Modifier order does not matter (``shift+ctrl+a`` is ``ctrl+shift+a``).
    """
    norm = normalize_key(chord)
    parts = norm.split("+")
    mods = sorted(p for p in parts if p.lower() in _MODIFIERS)
    keys = [p for p in parts if p.lower() not in _MODIFIERS]
    if len(keys) != 1:
        raise X11Error(f"key chord {chord!r} must name exactly one non-modifier key")
    key = keys[0]
    if len(key) == 1 and mods:
        # ``ctrl+L`` is the same chord as ``ctrl+l``; a capital would make xdotool add shift.
        key = key.lower()
    if re.fullmatch(r"F([1-9]|1[0-2])", key) and key not in FUNCTION_KEY_ALLOWLIST:
        raise X11Error(
            f"function key {key!r} is not allowed (only {sorted(FUNCTION_KEY_ALLOWLIST)})"
        )
    if not mods:
        return norm
    canonical = "+".join([*sorted(mods, key=("ctrl", "alt", "shift", "super").index), key])
    if canonical not in KEY_CHORD_ALLOWLIST:
        raise X11Error(
            f"key chord {chord!r} is not on the allowlist; the tester may use editing and "
            f"in-page chords only ({', '.join(sorted(KEY_CHORD_ALLOWLIST))})"
        )
    return canonical


# --------------------------------------------------------------------------
# xdotool argv builders (pure)
# --------------------------------------------------------------------------


def _coord(value: Any) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise X11Error(f"coordinate must be [x, y], got {value!r}")
    try:
        return float(value[0]), float(value[1])
    except (TypeError, ValueError) as exc:
        raise X11Error(f"coordinate must be numeric, got {value!r}") from exc


def _int_in(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        n = int(value) if value is not None else default
    except (TypeError, ValueError):
        n = default
    return max(lo, min(hi, n))


def build_argv(action: str, params: dict[str, Any], geo: Geometry) -> list[list[str]]:
    """Translate one action into zero or more xdotool argv lists.

    ``screenshot`` and ``wait`` produce no argv (the backend handles them
    itself); every other supported action produces one or more commands to run
    in order. Raises :class:`X11Error` for an unsupported action or bad params.
    """
    if action in UNSUPPORTED_ACTIONS:
        raise X11Error(f"action {action!r} is not supported by this backend")
    if action not in ACTIONS:
        raise X11Error(f"unknown action {action!r}")

    if action in ("screenshot", "wait"):
        return []

    if action == "mouse_move":
        x, y = geo.to_real(*_coord(params.get("coordinate")))
        return [["xdotool", "mousemove", "--sync", str(x), str(y)]]

    if action in ("left_click", "right_click", "middle_click", "double_click", "triple_click"):
        x, y = geo.to_real(*_coord(params.get("coordinate")))
        button = {"right_click": 3, "middle_click": 2}.get(action, 1)
        repeat = {"double_click": 2, "triple_click": 3}.get(action, 1)
        argv = ["xdotool", "mousemove", "--sync", str(x), str(y), "click"]
        if repeat > 1:
            argv += ["--repeat", str(repeat), "--delay", "80"]
        argv.append(str(button))
        return [argv]

    if action == "left_click_drag":
        x0, y0 = geo.to_real(*_coord(params.get("start_coordinate")))
        x1, y1 = geo.to_real(*_coord(params.get("coordinate")))
        return [
            ["xdotool", "mousemove", "--sync", str(x0), str(y0), "mousedown", "1"],
            ["xdotool", "mousemove", "--sync", str(x1), str(y1), "mouseup", "1"],
        ]

    if action == "type":
        text = params.get("text")
        if not isinstance(text, str) or not text:
            raise X11Error("type requires a non-empty 'text' string")
        if len(text) > MAX_TYPE_CHARS:
            raise X11Error(f"type text is {len(text)} chars; cap is {MAX_TYPE_CHARS}")
        if "\x00" in text:
            raise X11Error("type text contains NUL")
        return [["xdotool", "type", "--delay", "20", "--", text]]

    if action == "key":
        raw = params.get("text", params.get("key"))
        if not isinstance(raw, str):
            raise X11Error("key requires a 'text' string such as 'Return' or 'ctrl+l'")
        return [["xdotool", "key", "--", check_key_allowed(raw)]]

    if action == "scroll":
        x, y = geo.to_real(*_coord(params.get("coordinate")))
        direction = str(params.get("scroll_direction", "down")).lower()
        if direction not in _SCROLL_BUTTONS:
            raise X11Error(f"scroll_direction must be one of {sorted(_SCROLL_BUTTONS)}")
        clicks = _int_in(params.get("scroll_amount"), 1, MAX_SCROLL_CLICKS, 3)
        return [
            [
                "xdotool",
                "mousemove",
                "--sync",
                str(x),
                str(y),
                "click",
                "--repeat",
                str(clicks),
                "--delay",
                "30",
                str(_SCROLL_BUTTONS[direction]),
            ]
        ]

    raise X11Error(f"unhandled action {action!r}")  # pragma: no cover - ACTIONS is exhaustive


def wait_seconds(params: dict[str, Any]) -> float:
    """Clamp a ``wait`` action's duration."""
    try:
        s = float(params.get("duration", params.get("seconds", 1.0)))
    except (TypeError, ValueError):
        s = 1.0
    return max(0.0, min(MAX_WAIT_SECONDS, s))


# --------------------------------------------------------------------------
# Live backend
# --------------------------------------------------------------------------

Runner = Callable[[Sequence[str], dict[str, str]], None]


def _run_xdotool(argv: Sequence[str], env: dict[str, str]) -> None:
    try:
        subprocess.run(list(argv), env=env, check=True, timeout=20, capture_output=True)
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or b"").decode("utf-8", "replace").strip()
        raise X11Error(f"xdotool failed ({exc.returncode}): {err or ' '.join(argv[:3])}") from exc
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise X11Error(f"xdotool did not complete: {exc}") from exc


class Display:
    """A live X display: grabs screenshots and executes actions.

    ``runner`` is injectable so the whole class can be exercised in tests with
    a fake xdotool; ``grabber`` likewise returns a PIL image for a display, and
    ``server_name`` resolves the display's owning X server (see
    :func:`verify_virtual_display`).
    """

    def __init__(
        self,
        display: str,
        geo: Geometry,
        shots_dir: Path,
        *,
        runner: Runner = _run_xdotool,
        grabber: Optional[Callable[[str], Any]] = None,
        settle_seconds: float = 1.0,
        server_name: Callable[[str], str] = server_process_name,
    ) -> None:
        # Refuses before anything else can happen: a real seat's server name,
        # a display nothing owns, or a remote host all stop construction.
        self.server = verify_virtual_display(display, server_name=server_name)
        self.display = display
        self.geo = geo
        self.shots_dir = Path(shots_dir)
        self.shots_dir.mkdir(parents=True, exist_ok=True)
        self._runner = runner
        self._grabber = grabber or _grab
        self._settle = settle_seconds
        self._env = {**os.environ, "DISPLAY": display}
        self._n = 0

    # -- screenshots -------------------------------------------------------

    def reset(self, shots_dir: Path) -> None:
        """Start a new archive directory with numbering from 01."""
        self.shots_dir = Path(shots_dir)
        self.shots_dir.mkdir(parents=True, exist_ok=True)
        self._n = 0

    def screenshot(self, label: str = "shot") -> tuple[bytes, Path]:
        """Grab the display, downscale to ``geo.shot_w``, archive, return PNG bytes."""
        img = self._grabber(self.display)
        if img.size != (self.geo.real_w, self.geo.real_h):
            # The X server may have come up at a different size than configured;
            # trust the pixels we actually got rather than mis-scaling every click.
            self.geo = Geometry(img.size[0], img.size[1], self.geo.shot_w)
        if self.geo.scale != 1:
            img = img.resize((self.geo.shot_w, self.geo.shot_h))
        self._n += 1
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-")[:48] or "shot"
        path = self.shots_dir / f"{self._n:02d}-{safe}.png"
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG", optimize=True)
        data = buf.getvalue()
        path.write_bytes(data)
        return data, path

    # -- actions -----------------------------------------------------------

    def perform(self, action: str, params: dict[str, Any]) -> str:
        """Execute one action and return a one-line human summary.

        Raises :class:`X11Error` on bad input; the harness turns that into an
        error ``tool_result`` so the model can correct itself.
        """
        if action == "wait":
            s = wait_seconds(params)
            time.sleep(s)
            return f"waited {s:g}s"
        for argv in build_argv(action, params, self.geo):
            self._runner(argv, self._env)
        if action == "screenshot":
            return "screenshot"
        if action in ("type", "key"):
            what = params.get("text", params.get("key"))
            return f"{action} {str(what)[:60]!r}"
        if action == "left_click_drag":
            return f"drag {params.get('start_coordinate')} -> {params.get('coordinate')}"
        return f"{action} at {params.get('coordinate')}"

    def settle(self) -> None:
        """Let the UI catch up after an input event before the next screenshot."""
        if self._settle > 0:
            time.sleep(self._settle)


def _grab(display: str) -> Any:
    # Imported lazily so the pure helpers above import without Pillow.
    from PIL import ImageGrab

    return ImageGrab.grab(xdisplay=display)
