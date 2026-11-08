"""The ``desktop`` block of ``app.json``: windows and shortcuts the desktop shell hosts.

An app may ask the Electron shell for a small, closed set of window shapes and for
global shortcuts. The shell, not the app, owns every window: it creates it with fixed
web preferences, loads the host's own ``/popout/desktop/<app>/<id>`` route from the
gateway, and that route mounts the app's ``entry`` ESM module the way ``AppHost``
mounts a panel tab. No app code runs in the Electron main process.

This block is the whole of what an app can ask for. Anything it does not declare
here the shell refuses (``website/electron/app-window-host-rules.js`` re-checks the
same rules, because the shell must not trust an older or newer gateway's parse).

Parsed by :class:`kiro_crew.apps.manifest.AppManifest`; this module never imports it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: Window and shortcut ids: lowercase slugs, short enough for a log line.
_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
#: One relative POSIX path of plain segments ending in a JS module. Traversal and
#: containment are checked again in ``AppManifest.validate`` against the app root.
_ENTRY_RE = re.compile(r"^(?:[A-Za-z0-9_-][A-Za-z0-9._-]*/)*[A-Za-z0-9_-][A-Za-z0-9._-]*\.m?js$")
_MAX_ENTRY_LEN = 200

MAX_DESKTOP_WINDOWS = 4
MAX_DESKTOP_SHORTCUTS = 4

WINDOW_KINDS = ("overlay", "panel")
PANEL_POSITIONS = ("bottom-right", "top-right", "center")
PANEL_WIDTH_RANGE = (160, 1600)
PANEL_HEIGHT_RANGE = (120, 1200)
PANEL_DEFAULT_SIZE = (400, 560)

_WINDOW_KEYS = frozenset(
    {"id", "entry", "kind", "openOnEnable", "width", "height", "position", "transparent"}
)
_PANEL_ONLY_KEYS = ("width", "height", "position", "transparent")
_SHORTCUT_KEYS = frozenset({"id", "accelerator", "window"})
_BLOCK_KEYS = frozenset({"windows", "shortcuts"})

#: Electron accelerator modifiers, case-insensitive, mapped to one canonical spelling
#: so two spellings of one chord count as the same chord.
_MODIFIERS = {
    "commandorcontrol": "CommandOrControl",
    "cmdorctrl": "CommandOrControl",
    "command": "Command",
    "cmd": "Command",
    "control": "Control",
    "ctrl": "Control",
    "alt": "Alt",
    "option": "Alt",
    "altgr": "AltGr",
    "shift": "Shift",
    "super": "Super",
    "meta": "Super",
}
#: The modifier each spelling presses on macOS, then on Windows and Linux.
_SAME_KEY = (
    {"CommandOrControl": "Cmd", "Command": "Cmd", "Super": "Cmd", "Control": "Ctrl"},
    {"CommandOrControl": "Ctrl", "Control": "Ctrl", "Command": "Cmd", "Super": "Super"},
)
_NAMED_KEYS = {"space": "Space", "up": "Up", "down": "Down", "left": "Left", "right": "Right"}
_FUNCTION_KEY_RE = re.compile(r"^f([1-9]|1[0-9]|2[0-4])$")


def _str(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    return value if isinstance(value, str) else ""


def canonical_accelerator(accelerator: str) -> tuple[str, str]:
    """``(canonical chord, "")`` for an accelerator an app may claim, else ``("", reason)``.

    A global shortcut is exclusive: while the shell holds it, the focused program never
    sees the key. So an app may claim only a chord of two different modifier keys on
    every platform (so at least one is not Shift), plus one letter, digit, function,
    arrow or Space key. That keeps every single-modifier chord (copy, paste, save,
    undo) out of reach.
    """
    if not isinstance(accelerator, str) or not accelerator or len(accelerator) > 64:
        return "", "must be a non-empty string of at most 64 characters"
    parts = accelerator.split("+")
    if any(not p.strip() for p in parts):
        return "", "has an empty part"
    *mods_raw, key_raw = (p.strip() for p in parts)
    mods: list[str] = []
    for raw in mods_raw:
        mod = _MODIFIERS.get(raw.lower())
        if mod is None:
            return "", f"has an unknown modifier {raw!r}"
        if mod in mods:
            return "", f"repeats the modifier {raw!r}"
        mods.append(mod)
    # Counted per platform, because spellings overlap there: on macOS
    # CommandOrControl, Command and Super are all Cmd, elsewhere CommandOrControl is
    # Ctrl. Two spellings of one key are one modifier.
    if any(len({table.get(m, m) for m in mods}) < 2 for table in _SAME_KEY):
        return "", "needs at least two different modifier keys on every platform"
    low = key_raw.lower()
    if len(key_raw) == 1 and key_raw.isascii() and key_raw.isalnum():
        key = key_raw.upper()
    elif _FUNCTION_KEY_RE.fullmatch(low):
        key = low.upper()
    elif low in _NAMED_KEYS:
        key = _NAMED_KEYS[low]
    else:
        return "", f"has an unsupported key {key_raw!r}"
    return "+".join([*sorted(mods), key]), ""


@dataclass
class DesktopWindow:
    """One window the shell hosts for the app."""

    id: str = ""
    # ESM module mounted in the window, resolved against the app's ``ui`` directory
    # like ``contributes.panelTabs[].entry`` (``/apps/<name>/ui/<entry>``).
    entry: str = ""
    kind: str = ""
    openOnEnable: bool = False  # noqa: N815
    width: int = 0
    height: int = 0
    position: str = ""
    transparent: bool = False

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "entry": self.entry, "kind": self.kind}
        if self.openOnEnable:
            d["openOnEnable"] = True
        if self.kind == "panel":
            d["width"] = self.width
            d["height"] = self.height
            d["position"] = self.position
            if self.transparent:
                d["transparent"] = True
        return d


@dataclass
class DesktopShortcut:
    """One global shortcut the shell registers and delivers to one of the app's windows."""

    id: str = ""
    accelerator: str = ""
    window: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "accelerator": self.accelerator, "window": self.window}


@dataclass
class DesktopConfig:
    """The parsed ``desktop`` block, with every refusal found while parsing it."""

    windows: list[DesktopWindow] = field(default_factory=list)
    shortcuts: list[DesktopShortcut] = field(default_factory=list)
    errors: list[str] = field(default_factory=list, repr=False)

    def to_dict(self) -> dict[str, Any]:
        if not self.windows:
            return {}
        d: dict[str, Any] = {"windows": [w.to_dict() for w in self.windows]}
        if self.shortcuts:
            d["shortcuts"] = [s.to_dict() for s in self.shortcuts]
        return d

    def validate(self) -> list[str]:
        return list(self.errors)

    @classmethod
    def from_value(cls, raw: Any) -> DesktopConfig:
        """Parse ``app.json``'s ``desktop`` value; an absent block is an empty config."""
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            return cls(errors=["desktop must be an object"])
        errors: list[str] = []
        unknown = sorted(str(k) for k in raw if k not in _BLOCK_KEYS)
        if unknown:
            errors.append(f"desktop: unknown keys {unknown}")
        windows = _parse_windows(raw.get("windows"), errors)
        ids = {w.id for w in windows}
        shortcuts = _parse_shortcuts(raw.get("shortcuts", []), ids, errors)
        return cls(windows=windows, shortcuts=shortcuts, errors=errors)


def _parse_windows(raw: Any, errors: list[str]) -> list[DesktopWindow]:
    if not isinstance(raw, list) or not raw:
        errors.append("desktop.windows must be a non-empty list")
        return []
    if len(raw) > MAX_DESKTOP_WINDOWS:
        errors.append(f"desktop.windows: at most {MAX_DESKTOP_WINDOWS} windows ({len(raw)})")
    windows: list[DesktopWindow] = []
    seen: set[str] = set()
    for index, item in enumerate(raw[:MAX_DESKTOP_WINDOWS]):
        where = f"desktop.windows[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{where} must be an object")
            continue
        unknown = sorted(str(k) for k in item if k not in _WINDOW_KEYS)
        if unknown:
            errors.append(f"{where}: unknown keys {unknown}")
        win = DesktopWindow(
            id=_str(item, "id"),
            entry=_str(item, "entry"),
            kind=_str(item, "kind"),
        )
        if not _ID_RE.fullmatch(win.id):
            errors.append(f"{where}: id must match {_ID_RE.pattern}")
        elif win.id in seen:
            errors.append(f"{where}: duplicate id {win.id!r}")
        seen.add(win.id)
        if len(win.entry) > _MAX_ENTRY_LEN or not _ENTRY_RE.fullmatch(win.entry):
            errors.append(f"{where}: entry must be a relative .js or .mjs path under ui/")
        if win.kind not in WINDOW_KINDS:
            errors.append(f"{where}: kind must be one of {list(WINDOW_KINDS)}")
        open_raw = item.get("openOnEnable", False)
        if not isinstance(open_raw, bool):
            errors.append(f"{where}: openOnEnable must be a boolean")
        win.openOnEnable = open_raw is True
        if win.kind == "panel":
            _parse_panel_fields(item, win, where, errors)
        else:
            present = [k for k in _PANEL_ONLY_KEYS if k in item]
            if present:
                errors.append(f"{where}: {present} apply to a panel only")
        windows.append(win)
    return windows


def _parse_panel_fields(
    item: dict[str, Any], win: DesktopWindow, where: str, errors: list[str]
) -> None:
    for name, (low, high), default in (
        ("width", PANEL_WIDTH_RANGE, PANEL_DEFAULT_SIZE[0]),
        ("height", PANEL_HEIGHT_RANGE, PANEL_DEFAULT_SIZE[1]),
    ):
        value = item.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            errors.append(f"{where}: {name} must be an integer from {low} to {high}")
            value = default
        setattr(win, name, value)
    position = item.get("position", PANEL_POSITIONS[0])
    if position not in PANEL_POSITIONS:
        errors.append(f"{where}: position must be one of {list(PANEL_POSITIONS)}")
        position = PANEL_POSITIONS[0]
    win.position = position
    transparent = item.get("transparent", False)
    if not isinstance(transparent, bool):
        errors.append(f"{where}: transparent must be a boolean")
    win.transparent = transparent is True


def _parse_shortcuts(raw: Any, window_ids: set[str], errors: list[str]) -> list[DesktopShortcut]:
    if not isinstance(raw, list):
        errors.append("desktop.shortcuts must be a list")
        return []
    if len(raw) > MAX_DESKTOP_SHORTCUTS:
        errors.append(f"desktop.shortcuts: at most {MAX_DESKTOP_SHORTCUTS} shortcuts ({len(raw)})")
    shortcuts: list[DesktopShortcut] = []
    seen_ids: set[str] = set()
    seen_chords: set[str] = set()
    for index, item in enumerate(raw[:MAX_DESKTOP_SHORTCUTS]):
        where = f"desktop.shortcuts[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{where} must be an object")
            continue
        unknown = sorted(str(k) for k in item if k not in _SHORTCUT_KEYS)
        if unknown:
            errors.append(f"{where}: unknown keys {unknown}")
        shortcut = DesktopShortcut(
            id=_str(item, "id"),
            accelerator=_str(item, "accelerator"),
            window=_str(item, "window"),
        )
        if not _ID_RE.fullmatch(shortcut.id):
            errors.append(f"{where}: id must match {_ID_RE.pattern}")
        elif shortcut.id in seen_ids:
            errors.append(f"{where}: duplicate id {shortcut.id!r}")
        seen_ids.add(shortcut.id)
        chord, reason = canonical_accelerator(shortcut.accelerator)
        if reason:
            errors.append(f"{where}: accelerator {reason}")
        elif chord in seen_chords:
            errors.append(f"{where}: accelerator repeats another shortcut's chord")
        seen_chords.add(chord)
        if shortcut.window not in window_ids:
            errors.append(f"{where}: window must name one of the app's desktop.windows")
        shortcuts.append(shortcut)
    return shortcuts
