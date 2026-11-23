"""App chat launch: the person's chats an app's page may open, declared in its manifest.

An app page that hands the person a conversation (a follow-up with the agent that did
some work, say) creates a named chat slot for them. The dashboard's own
``POST /api/chat/slots`` does that, but declaring that path in ``permissions.api``
would hand the page every slot route under it. An app instead declares the one shape
of chat it opens, in ``permissions.chatLaunch``::

    "chatLaunch": {
      "keyPrefix": "notes-followup-",
      "agents": ["notes-reviewer"],
      "folder": "Notes",
      "memoryModes": ["persistent"]
    }

and the create carries ``app_launch: <app>``. The gateway then admits it only inside
that declaration (``dashboard/app_chat_launch.py``): a key under ``keyPrefix``, an
agent from ``agents``, a folder only when it is named ``folder``, a memory mode from
``memoryModes`` (``["persistent"]`` when omitted), and no other create option. The
chat is the person's, exactly as one the dashboard creates: it records no owning app
and runs under the person's own approval rules.

A malformed declaration declares nothing (:func:`parse_chat_launch` answers ``None``),
the direction a grant must fail in. A ``keyPrefix`` that starts like a key the core
mints for itself (a person's ``chat-`` tab, a cron or workflow result, a Bot's thread,
a task review) is malformed, so no declaration reaches those slots by name.

A prefix is one app's while it holds it: a launch is refused while any other installed
app, enabled or not, declares a prefix that overlaps it (:func:`overlapping_declarers`),
so no app reaches a chat under another installed app's prefix, including one opened
before that app's page named itself on it. Nothing here names an app.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from kiro_crew.validation_parts.input_bounds import is_registered_agent_name

#: The manifest key under ``permissions``.
MANIFEST_KEY = "chatLaunch"

#: Key prefixes the core mints for slots of its own: a person's chat tab
#: (``session_control.OWNER_CHAT_SLOT_PREFIX``), a cron or workflow result
#: (``slot_ownership.CRON_SLOT_PREFIX`` / ``WORKFLOW_SLOT_PREFIX``), a task review
#: (``slot_ownership.TASK_REVIEW_SLOT_PREFIX``) and a Bot's thread
#: (``members.DM_SLOT_KEY_PREFIX``). Spelled here because importing those modules
#: from manifest parsing would cycle; ``test_app_chat_launch`` pins them equal.
CORE_KEY_PREFIXES = ("chat-", "cron-", "workflow-", "task-review-", "member-")

#: Memory modes a launched chat may take, as ``POST /api/chat/slots`` names them.
MEMORY_MODES = ("persistent", "incognito", "temporary")

#: A declared prefix: lowercase kebab words, ending in ``-``, so the key's own part
#: cannot run on into a word of the prefix.
_PREFIX_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*-")
_PREFIX_MAX = 64
#: The part of a key after the prefix, in the charset a slot key keeps unchanged.
_KEY_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
#: The longest key the slot registry stores unfolded (``_SLOT_KEY_KEEP_CHARS + 1 +
#: _SLOT_KEY_HASH_CHARS``), the bound ``ctx.chat_slots`` keys keep too.
KEY_MAX = 193
_AGENTS_MAX = 16
_FOLDER_MAX = 80


@dataclass(frozen=True)
class ChatLaunch:
    """One app's parsed ``permissions.chatLaunch``."""

    key_prefix: str
    agents: tuple[str, ...]
    folder: str = ""
    memory_modes: tuple[str, ...] = ("persistent",)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"keyPrefix": self.key_prefix, "agents": list(self.agents)}
        if self.folder:
            d["folder"] = self.folder
        d["memoryModes"] = list(self.memory_modes)
        return d

    def covers_key(self, key: str) -> bool:
        """Whether *key* is this declaration's prefix followed by a key id."""
        if len(key) > KEY_MAX or not key.startswith(self.key_prefix):
            return False
        return _KEY_ID_RE.fullmatch(key[len(self.key_prefix) :]) is not None

    def overlaps(self, other: ChatLaunch) -> bool:
        """Whether some key falls under both prefixes: one is the start of the other."""
        return self.key_prefix.startswith(other.key_prefix) or other.key_prefix.startswith(
            self.key_prefix
        )

    def within(self, other: ChatLaunch | None) -> bool:
        """Whether every chat this declaration admits, *other* admits too."""
        return (
            other is not None
            and self.key_prefix == other.key_prefix
            and self.folder == other.folder
            and set(self.agents) <= set(other.agents)
            and set(self.memory_modes) <= set(other.memory_modes)
        )


def _prefix(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > _PREFIX_MAX:
        return None
    if not _PREFIX_RE.fullmatch(value):
        return None
    if any(value.startswith(core) or core.startswith(value) for core in CORE_KEY_PREFIXES):
        return None
    return value


def _agents(value: Any) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not value or len(value) > _AGENTS_MAX:
        return None
    if not all(is_registered_agent_name(a) for a in value):
        return None
    return tuple(dict.fromkeys(value))


def _folder(value: Any) -> str | None:
    if value is None:
        return ""
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not name or len(name) > _FOLDER_MAX or not name.isprintable():
        return None
    return name


def _memory_modes(value: Any) -> tuple[str, ...] | None:
    if value is None:
        return ("persistent",)
    if not isinstance(value, list) or not value or not all(m in MEMORY_MODES for m in value):
        return None
    return tuple(m for m in MEMORY_MODES if m in value)


def parse_chat_launch(value: Any) -> ChatLaunch | None:
    """The declaration in a raw ``permissions.chatLaunch`` value, or ``None``.

    Any malformed or unknown part declares nothing at all rather than a narrower
    launch, so a typo never leaves a half-declared grant behind.
    """
    if not isinstance(value, dict):
        return None
    if set(value) - {"keyPrefix", "agents", "folder", "memoryModes"}:
        return None
    prefix = _prefix(value.get("keyPrefix"))
    agents = _agents(value.get("agents"))
    folder = _folder(value.get("folder"))
    modes = _memory_modes(value.get("memoryModes"))
    if prefix is None or agents is None or folder is None or modes is None:
        return None
    return ChatLaunch(key_prefix=prefix, agents=agents, folder=folder, memory_modes=modes)


def declared_chat_launch(app_name: str) -> ChatLaunch | None:
    """*app_name*'s declaration while it is installed and enabled, else ``None``.

    Read from the installed record's ``app.json`` on every call (a built-in's is
    rewritten from the wheel at every start), so a manifest that drops the key, or a
    disabled app, refuses the next launch. Blocking: call it off the event loop.
    """
    from kiro_crew.apps.manager import get_app_manifest, is_app_enabled

    if not is_app_enabled(app_name):
        return None
    manifest = get_app_manifest(app_name)
    return manifest.permissions.chatLaunch if manifest is not None else None


def overlapping_declarers(app_name: str, launch: ChatLaunch) -> tuple[str, ...]:
    """Every other installed app whose ``chatLaunch`` prefix overlaps *launch*'s, sorted.

    Enabled or not: a disabled app's chats keep their keys, so its prefix stays its
    own. Read from each installed tree's ``app.json``, the record
    :func:`declared_chat_launch` reads; a sidecar entry or an unreadable manifest
    declares nothing. Blocking: call it off the event loop.
    """
    from kiro_crew.apps.manager import _is_sidecar_entry, apps_dir, get_app_manifest
    from kiro_crew.apps.manifest_names import app_name_error

    try:
        names = sorted(entry.name for entry in apps_dir().iterdir() if entry.is_dir())
    except FileNotFoundError:
        return ()
    holders: list[str] = []
    for name in names:
        if name == app_name or _is_sidecar_entry(name) or app_name_error(name):
            continue
        manifest = get_app_manifest(name)
        other = manifest.permissions.chatLaunch if manifest is not None else None
        if other is not None and other.overlaps(launch):
            holders.append(name)
    return tuple(holders)


__all__ = [
    "CORE_KEY_PREFIXES",
    "KEY_MAX",
    "MANIFEST_KEY",
    "MEMORY_MODES",
    "ChatLaunch",
    "declared_chat_launch",
    "overlapping_declarers",
    "parse_chat_launch",
]
