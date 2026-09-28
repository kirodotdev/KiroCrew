"""Mochi's own settings store.

NEW MODULE — no upstream counterpart. The original app kept user settings in
its Electron ``configService`` (a JSON file in the app's own data dir); as a
builtin the gateway owns config, but these are Mochi-scoped preferences that do
not belong in KiroCrew's global ``config.json``, so they live in the app's data
dir under the same atomic-write discipline as the watch list.

Currently one setting: ``petInstance``.

WHY petInstance EXISTS
----------------------
Mochi is a builtin, so it lives inside a gateway and its data is that
gateway's. A user can have several gateways reachable at once — not several on
this machine (port 5476 is a mutex, enforced by the shell's
``instance-guard.js``), but one local "self" plus any number of REMOTE
instances forwarded in over ``ssh -L`` and shown as tabs by
``InstancesViewport``. Each of those gateways may have Mochi enabled, each with
its own watch list and stats.

The pet window, by contrast, is a single machine-wide resource: one pet on the
screen. So something has to choose which instance's Mochi it shows. That choice
is a property of THIS machine, which is why it is stored on the local instance
and read by the shell — not synced or per-remote.

Values:

- ``"self"`` (default) — the pet shows the local gateway's Mochi.
- an instance id from ``GET /api/instances`` — the pet shows that remote
  instance's Mochi, loading its panel through the SSH-forwarded local port.

The id is stored opaquely and NOT validated against the live instance list:
instances come and go (TTL expiry, tunnel down) and a stored id must survive a
temporarily absent instance rather than being silently reset. The consumer
resolves it at open time and falls back to ``"self"`` when it cannot.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from kiro_crew.atomic_write import atomic_write
from kiro_crew.platform_compat import file_lock

logger = logging.getLogger(__name__)

SETTINGS_FILENAME = "mochi-settings.json"

#: Sentinel meaning "the gateway this Mochi lives in".
SELF_INSTANCE = "self"

#: The built-in appearance packs, which are also the two characters a user picks
#: between. There is ONE identity key -- ``activeAppearance`` -- and these are its
#: built-in values; a user-imported pack id is equally valid.
#:
#: This deliberately matches the original, which had a second key (``pet.character``)
#: and MIGRATED IT AWAY into ``activeAppearance`` precisely because the two could
#: disagree. This port briefly re-created that split under the name ``avatar``,
#: which let the pet LOOK like an imported robot while its prompt described a cat.
#: Personality now follows the active pack (see ``soul_loader``), so the art and
#: the persona cannot diverge.
PACK_MOCHI = "default-mochi"
PACK_GHOST = "kiro-ghost"
BUILTIN_PACKS = (PACK_MOCHI, PACK_GHOST)

#: Legacy ``avatar`` values, mapped to the pack that replaced them. Mirrors the
#: original's own ``CHARACTER_TO_PACK`` migration table.
_LEGACY_AVATAR_PACKS = {"mochi": PACK_MOCHI, "ghost": PACK_GHOST}

#: Longest accepted pet name. The name is rendered in a 320px-wide chat panel
#: and in speech bubbles, so an unbounded string would break the layout — and it
#: reaches the agent prompt, where it is also an injection surface.
MAX_PET_NAME_LEN = 40

#: Behavior modes. Default is QUIET: an on-screen companion that interrupts by
#: default is the fastest way to get itself turned off, so the user opts IN to
#: more activity rather than opting out.
MODE_QUIET = "quiet"
MODE_NORMAL = "normal"
MODE_ACTIVE = "active"
MODES = (MODE_QUIET, MODE_NORMAL, MODE_ACTIVE)

# Background-agent spend tiers (contract lives in activity_budget.TIERS —
# this tuple only gates what the settings file may persist).
ACTIVITY_TIERS = ("economy", "balanced", "active", "unlimited")


#: UI language for Mochi's own windows. Validates SHAPE, not membership in the
#: shipped-language list — the same rule as ``dashboard.language``
#: (``dashboard/handlers/core.py::_LANGUAGE_TAG_RE``), and for the same reason:
#: the frontend's ``SUPPORTED_LANGUAGES`` registry is the single source of truth,
#: so adding a language stays a pure frontend data change (registry + one
#: catalog) and never needs a backend edit. A well-formed tag with no catalog is
#: safe because ``resolveLanguage()`` falls back on its own.
#:
#: This replaced a closed ``("", "en", "zh")`` set that predated Mochi's strings
#: living in the core catalog: back then the app shipped exactly two bundles, so
#: the set WAS the truth. It now has ten, and a second list here would drift the
#: moment an eleventh was added.
#:
#: Empty means "follow KiroCrew" — the renderer passes it through to
#: ``initI18n()``, which resolves the language the dashboard persisted and only
#: then falls back to the browser locale.
LANG_FOLLOW_SYSTEM = ""
_LANGUAGE_TAG_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,2}$")

#: Global-shortcut actions Mochi can bind, and their defaults — the original's
#: `config.shortcuts` (src/shared/config.ts). voiceInput is omitted: push-to-talk
#: needs the OS-level hotkey monitor, which is not ported, so a bound key would
#: be dead. Empty string means "unbound".
SHORTCUT_ACTIONS = ("toggleWindow", "hideAll", "screenCapture")


def _default_shortcuts() -> dict[str, str]:
    """Platform-appropriate global accelerators.

    ``CommandOrControl`` already resolves to Cmd on macOS and Ctrl elsewhere, so
    the token is portable. The MODIFIER CHOICE is not: on Windows and Linux,
    ``Ctrl+Shift+<letter>`` is heavily used by applications (VS Code alone takes
    Ctrl+Shift+M and Ctrl+Shift+X), and ``globalShortcut`` registration is
    EXCLUSIVE — while Mochi holds the combo, the focused app never receives it.
    Silently stealing another app's shortcut is worse than failing to register,
    because a failure is reported back to the user and a theft is not.

    ``Alt+Shift`` is used instead. ``Ctrl+Alt`` is deliberately avoided: on
    keyboard layouts with AltGr (German, Polish, and others) Ctrl+Alt+<letter>
    types a character.
    """
    if sys.platform == "darwin":
        return {
            "toggleWindow": "CommandOrControl+Shift+M",
            "hideAll": "CommandOrControl+Shift+H",
            "screenCapture": "CommandOrControl+Shift+X",
        }
    return {
        "toggleWindow": "Alt+Shift+M",
        "hideAll": "Alt+Shift+H",
        "screenCapture": "Alt+Shift+X",
    }


_SHORTCUT_DEFAULTS: dict[str, str] = _default_shortcuts()

#: macOS-only accelerator tokens rewritten to their portable equivalents on the
#: way in. Electron documents ``Option`` as macOS-only, so a binding captured on
#: a Mac and stored as ``Option+Shift+X`` FAILS to register on Windows — and the
#: settings UI then reports it as "already taken", which is misleading. ``Alt``
#: means the same key on macOS and is valid everywhere, so it is the stored form.
_PORTABLE_MODIFIERS: dict[str, str] = {
    "Option": "Alt",
    "Cmd": "Command",
    "CmdOrCtrl": "CommandOrControl",
}

#: Electron accelerator modifier tokens (electron.git docs/api/accelerator.md).
#: `Super`/`Meta` are included because Electron accepts them even though the
#: capture UI does not emit them.
_ACCEL_MODIFIERS = frozenset(
    {
        "Command",
        "Cmd",
        "Control",
        "Ctrl",
        "CommandOrControl",
        "CmdOrCtrl",
        "Alt",
        "Option",
        "AltGr",
        "Shift",
        "Super",
        "Meta",
    }
)


def validate_accelerator(value: str) -> str:
    """Normalise an Electron accelerator, or raise ValueError.

    Shape only — whether the combination is actually AVAILABLE is a question only
    the OS can answer, and Electron reports that at register() time. The main
    process therefore returns a per-action success flag to the settings UI; this
    check just stops obviously-unusable strings (a lone modifier, two letter keys,
    an empty segment) from ever being persisted.
    """
    if not value:
        return ""  # explicitly unbound
    parts = [_PORTABLE_MODIFIERS.get(p, p) for p in value.split("+")]
    if any(not p for p in parts):
        raise ValueError("accelerator has an empty segment")
    mods = [p for p in parts if p in _ACCEL_MODIFIERS]
    keys = [p for p in parts if p not in _ACCEL_MODIFIERS]
    if not mods:
        raise ValueError("accelerator needs at least one modifier")
    if len(keys) != 1:
        raise ValueError("accelerator needs exactly one non-modifier key")
    return "+".join(mods + keys)


#: ``activeAppearance`` defaults to the built-in cat rather than empty.
#:
#: The original opened a first-run window to make the user choose before the pet
#: appeared. As a builtin that is a gate in front of a companion the user just
#: enabled from the App Store, so Mochi Cat is the default and the choice is
#: reversible from two places (the pet's right-click > Avatars, and the dashboard
#: Appearance card). Nothing treats "unset" as "ask the user" any more -- which is
#: also why the default is a real pack id and not "": an empty value would need a
#: second key to say WHICH built-in was meant, and that second key is the split
#: this design removes.
_DEFAULTS: dict[str, Any] = {
    "petInstance": SELF_INSTANCE,
    "mode": MODE_QUIET,
    "catPreset": None,
    "allowMcpServers": False,
    # Empty means "use the active pack's own name" (Kiro / Mochi for the two
    # built-ins) rather than a hardcoded default, so renaming follows the
    # character until the user overrides it.
    "petName": "",
    # Empty = follow the browser locale. The pet overlay ALREADY reads this key
    # (PetWidget's config effect calls setLang on it); it simply had no writer,
    # so the language selector the original shipped had nothing to persist to.
    "language": LANG_FOLLOW_SYSTEM,
    # Suppress the completion notification for background work (planning, watch
    # checks). Results still reach the chat — this only silences the interruption.
    "silentSubagents": False,
    # Background-agent spend. A separate axis from "mode" (personality):
    # neither may branch on the other, so making the pet chattier can
    # never silently cost more. Tiers resolve in activity_budget.py.
    "activityTier": "balanced",
    # Model override for background runs ("" = the agent's default).
    "bgModel": "",
    # The ONE identity key: a built-in pack id (BUILTIN_PACKS) or a user pack in
    # <data_dir>/appearances/ (appearance_store). Drives the art, the persona,
    # and the default pet name together.
    "activeAppearance": PACK_MOCHI,
    # User-editable global accelerators, as the original had them. A nested dict
    # (rather than one flat key per action) mirrors `config.shortcuts` and keeps
    # adding screenCapture / voiceInput a data change once those features land.
    "shortcuts": dict(_SHORTCUT_DEFAULTS),
}

#: Keys the original `config.mochi` carried that the ported renderer edits but
#: the builtin has no separate home for. Table-driven because each is a plain
#: container/scalar with exactly one rule — "the stored value must have the same
#: type as the default" — so adding a key is a data change, not a code change.
#:
#: `extraMcpServers` is the one that matters most: the original let the user
#: configure MCP servers individually (per-agent assignment, auto-approve list,
#: disabled tools), which a single boolean cannot express.
_PASSTHROUGH_DEFAULTS: dict[str, Any] = {
    "extraMcpServers": [],
    "colorMaps": {},
    "customPresets": [],
    #: Re-notify guard window for chat pushes: a notify with pushToChat set
    #: is dropped when a same-or-similar push was accepted within this many
    #: minutes. Enforced by MochiRuntime._push_to_chat (hooks.py). 0 disables
    #: the guard. There is deliberately no Settings UI control; the agent-side
    #: dedup rule (read the activity log first) remains the first line.
    "quietPeriodMins": 5,
    "breakReminderMins": 60,
    "restoreSessions": True,
    "sessionHistoryDays": 7,
    "firstLaunchDone": False,
    "chatAlwaysOnTop": True,
    "activityLogMaxEntries": 500,
}


def _passthrough_ok(value: Any, default: Any) -> bool:
    """Exact-type match, not ``isinstance``.

    ``isinstance(True, int)`` is True, so an isinstance check would let a boolean
    land in an int-valued key (and vice versa) and silently corrupt it.
    """
    return type(value) is type(default)


def _base_defaults() -> dict[str, Any]:
    """A fresh defaults dict, including the passthrough keys.

    Deep-copied because the passthrough defaults contain lists/dicts — a shallow
    copy would hand every caller the same mutable object.
    """
    out = copy.deepcopy(_DEFAULTS)
    out.update(copy.deepcopy(_PASSTHROUGH_DEFAULTS))
    return out


def settings_path(data_dir: Path) -> Path:
    return data_dir / SETTINGS_FILENAME


@contextmanager
def settings_mutation(data_dir: Path) -> Iterator[None]:
    """Cross-process lock for a settings read-modify-write sequence.

    Same defect and remedy as ``watchlist_file.watchlist_mutation``:
    ``atomic_write`` makes each WRITE atomic, but ``save_settings`` does a
    load-modify-write that is not. The settings file has more than one writer —
    e.g. a Settings save and an Avatars/appearance save can run concurrently
    (both offloaded to worker threads via ``to_thread``), each reading the same
    snapshot and the later atomic write discarding the other's update. Serialise
    the whole sequence on a sibling ``.lock`` file (flock follows the inode, so
    locking the data file would guard a vanishing inode after the rename).

    Plain reads stay lock-free: the atomic replace already guarantees a reader
    sees a complete document.
    """
    target = settings_path(data_dir)
    lock_path = target.with_name(target.name + ".lock")
    data_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with file_lock(fd, exclusive=True):
            yield
    finally:
        os.close(fd)


def load_settings(data_dir: Path) -> dict[str, Any]:
    """Read settings, filling defaults for anything absent or unreadable.

    Never raises: a corrupt or unreadable file degrades to defaults, matching
    how the other ported readers treat their files (a broken settings file must
    not take the app down).
    """
    path = settings_path(data_dir)
    out = _base_defaults()
    try:
        # encoding= is mandatory: a pet name or persona may be non-ASCII and
        # Windows would otherwise decode this file as cp1252 and raise.
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return out
    except (OSError, ValueError):
        logger.warning("mochi: unreadable settings at %s — using defaults", path)
        return out
    if not isinstance(raw, dict):
        return out
    stored = raw.get("petInstance")
    # Only a non-empty string is meaningful; anything else means "unset".
    if isinstance(stored, str) and stored:
        out["petInstance"] = stored
    mode = raw.get("mode")
    if mode in MODES:
        out["mode"] = mode
    # catPreset is a Mochi-only cat breed colorway; it is free-form because the
    # preset list lives in the renderer (builtInCatPresets) and user-defined
    # presets are allowed.
    preset = raw.get("catPreset")
    if isinstance(preset, str) and preset:
        out["catPreset"] = preset
    if isinstance(raw.get("allowMcpServers"), bool):
        out["allowMcpServers"] = raw["allowMcpServers"]
    name = raw.get("petName")
    if isinstance(name, str):
        out["petName"] = name[:MAX_PET_NAME_LEN]
    lang = raw.get("language")
    if lang == LANG_FOLLOW_SYSTEM or (isinstance(lang, str) and _LANGUAGE_TAG_RE.match(lang)):
        out["language"] = lang
    if isinstance(raw.get("silentSubagents"), bool):
        out["silentSubagents"] = raw["silentSubagents"]
    tier = raw.get("activityTier")
    if isinstance(tier, str) and tier in ACTIVITY_TIERS:
        out["activityTier"] = tier
    if isinstance(raw.get("bgModel"), str):
        out["bgModel"] = raw["bgModel"]
    # Migration, in the original's spirit (`migrateConfig`): a config written by
    # an older build may carry `avatar`, or an `activeAppearance` of "" meaning
    # "whatever avatar says". Fold both into the single key. An explicit pack id
    # always wins, so a user who picked a custom pack is never dragged back to a
    # built-in by a stale avatar value.
    legacy_avatar = _LEGACY_AVATAR_PACKS.get(raw.get("avatar") or "")
    active = raw.get("activeAppearance")
    if (not isinstance(active, str) or not active) and legacy_avatar:
        active = legacy_avatar
    if isinstance(active, str):
        out["activeAppearance"] = active
    # Per-action merge so a file written before an action existed (or with one
    # bad entry) still yields the defaults for everything else — a single invalid
    # accelerator must not cost the user their other binding.
    stored_shortcuts = raw.get("shortcuts")
    shortcuts = dict(_SHORTCUT_DEFAULTS)
    if isinstance(stored_shortcuts, dict):
        for action in SHORTCUT_ACTIONS:
            value = stored_shortcuts.get(action)
            if not isinstance(value, str):
                continue
            try:
                shortcuts[action] = validate_accelerator(value)
            except ValueError:
                logger.warning("mochi: ignoring invalid %s accelerator %r", action, value)
    out["shortcuts"] = shortcuts
    for key, default in _PASSTHROUGH_DEFAULTS.items():
        value = raw.get(key)
        if _passthrough_ok(value, default):
            out[key] = value
    return out


def save_settings(data_dir: Path, updates: dict[str, Any]) -> dict[str, Any]:
    """Merge ``updates`` into the stored settings and return the new state.

    Unknown keys are dropped rather than persisted, so a stale or hostile client
    cannot grow the file with arbitrary content.
    """
    # Lock the ENTIRE load-modify-write: two concurrent saves (e.g. a Settings
    # save and an Avatars save, both on worker threads) would otherwise read the
    # same snapshot and the later atomic write would drop the earlier update.
    with settings_mutation(data_dir):
        return _save_settings_locked(data_dir, updates)


def _save_settings_locked(data_dir: Path, updates: dict[str, Any]) -> dict[str, Any]:
    current = load_settings(data_dir)
    if "petInstance" in updates:
        value = updates["petInstance"]
        if value is None or value == "":
            current["petInstance"] = SELF_INSTANCE
        elif isinstance(value, str):
            current["petInstance"] = value
        else:
            raise ValueError("petInstance must be a string")
    if "mode" in updates:
        value = updates["mode"]
        if value not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        current["mode"] = value
    if "catPreset" in updates:
        value = updates["catPreset"]
        if value is None or value == "":
            current["catPreset"] = None
        elif isinstance(value, str):
            current["catPreset"] = value
        else:
            raise ValueError("catPreset must be a string or null")
    if "allowMcpServers" in updates:
        value = updates["allowMcpServers"]
        if not isinstance(value, bool):
            raise ValueError("allowMcpServers must be a boolean")
        current["allowMcpServers"] = value
    if "petName" in updates:
        value = updates["petName"]
        if value is None:
            current["petName"] = ""
        elif isinstance(value, str):
            # Trim rather than reject: a name pasted with trailing space is a
            # slip, not an error worth failing the whole save over.
            current["petName"] = value.strip()[:MAX_PET_NAME_LEN]
        else:
            raise ValueError("petName must be a string or null")
    if "language" in updates:
        value = updates["language"]
        # None/"" both mean "follow the system locale" — the same normalisation
        # the other nullable keys use, so a client clearing the field works.
        if value is None or value == LANG_FOLLOW_SYSTEM:
            current["language"] = LANG_FOLLOW_SYSTEM
        elif isinstance(value, str) and _LANGUAGE_TAG_RE.match(value):
            current["language"] = value
        else:
            raise ValueError("language must be a BCP-47 tag such as 'en' or 'zh-CN', or empty")
    if "silentSubagents" in updates:
        value = updates["silentSubagents"]
        if not isinstance(value, bool):
            raise ValueError("silentSubagents must be a boolean")
        current["silentSubagents"] = value
    if "activityTier" in updates:
        value = updates["activityTier"]
        if value not in ACTIVITY_TIERS:
            raise ValueError(f"activityTier must be one of {ACTIVITY_TIERS}")
        current["activityTier"] = value
    if "bgModel" in updates:
        value = updates["bgModel"]
        if value is None:
            current["bgModel"] = ""
        elif isinstance(value, str):
            current["bgModel"] = value.strip()
        else:
            raise ValueError("bgModel must be a string or null")
    if "activeAppearance" in updates:
        value = updates["activeAppearance"]
        # Clearing normalizes to the default PACK, not to "": with one identity
        # key there is no second place for "which built-in" to live, so an empty
        # value has to resolve here rather than at every read site.
        if value is None or value == "":
            current["activeAppearance"] = PACK_MOCHI
        elif isinstance(value, str):
            current["activeAppearance"] = value
        else:
            raise ValueError("activeAppearance must be a string or null")
    if "shortcuts" in updates:
        value = updates["shortcuts"]
        if not isinstance(value, dict):
            raise ValueError("shortcuts must be an object")
        merged = dict(current["shortcuts"])
        for action, accel in value.items():
            if action not in SHORTCUT_ACTIONS:
                # Unknown actions are dropped rather than persisted, same as
                # unknown top-level keys.
                continue
            if accel is None:
                merged[action] = ""
            elif isinstance(accel, str):
                # RAISES on a malformed accelerator: unlike load (which degrades
                # to the default), a WRITE must tell the user their combo was
                # rejected rather than silently keep the old one.
                merged[action] = validate_accelerator(accel)
            else:
                raise ValueError(f"{action} accelerator must be a string or null")
        current["shortcuts"] = merged

    for key, default in _PASSTHROUGH_DEFAULTS.items():
        if key not in updates:
            continue
        value = updates[key]
        if not _passthrough_ok(value, default):
            raise ValueError(f"{key} must be a {type(default).__name__}")
        current[key] = value

    # The builtin gates MCP exposure on a single boolean, while the original
    # expressed the same intent through whether the user configured any servers.
    # Deriving it keeps one source of truth: configuring a server is what turns
    # the capability on, so there is no separate primary switch a user can leave
    # off and then wonder why their server is ignored.
    if "extraMcpServers" in updates:
        current["allowMcpServers"] = len(current["extraMcpServers"]) > 0

    data_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(settings_path(data_dir), json.dumps(current, indent=2), mode=0o600)
    return current
