"""Session keys the host bound for the Kiro Space app, and the switch that gates them.

When this machine shares a session it binds the session's key, ``hook:ks-session:<artifactId>``.
Only a webhook source token the operator mapped to the app (``webhooks.APP_KIRO_SPACE``) may
post into a bound key, and only a bound key: ``api_hooks_agent`` refuses every other
``hook:ks-session:*`` key for the app token and refuses the bound key for every other token.
The registry is the allowlist; it holds keys, never credentials or people.

Off by default: :func:`room_enabled` needs Shared Crew's Feature Preview AND the
``dashboard.shared_crew.room`` switch. A config read error reads as off.

This module opens no socket.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from kiro_crew import webhooks

logger = logging.getLogger(__name__)

#: The prefix of a bound key. It sits under the webhook route's ``hook:`` prefix.
BOUND_KEY_PREFIX = "hook:ks-session:"
_BOUND_KEY_RE = re.compile(r"\Ahook:ks-session:[A-Za-z0-9_\-]{1,128}\Z")
MAX_BINDINGS = 1024
_FILENAME = "room_sessions.json"


def is_bound_key_shape(key: str) -> bool:
    """Whether *key* is shaped like a bound key (bound or not)."""
    return bool(_BOUND_KEY_RE.match(key))


#: The plain id alphabet an app-attested user id must use (no whitespace, control or
#: non-ASCII character), at most 128 characters.
AUTHOR_ID_RE = re.compile(r"[A-Za-z0-9_.:@\-]{1,128}")
_ATTRIBUTION_PREFIX = "[Message from "


def attribute_message(label: str, text: str) -> str:
    """*text* behind one header line saying another person wrote it, not the owner.

    *label* is already one plain line (``identity.clean_label``); brackets are removed so
    a name cannot close the header early or open a second one.
    """
    who = " ".join(re.sub(r"[\[\]<>]", " ", label).split())
    header = f"{_ATTRIBUTION_PREFIX}{who}, a collaborator in this shared session. Not the owner.]"
    return f"{header}\n{text}"


def strip_attribution(text: str) -> str:
    """*text* without the header :func:`attribute_message` put first (for the stored row)."""
    if text.startswith(_ATTRIBUTION_PREFIX):
        return text.partition("\n")[2]
    return text


def room_enabled() -> bool:
    """Both Shared Crew's Feature Preview and the room switch are on."""
    try:
        from kiro_crew.config import sections_shared_crew
        from kiro_crew.shared_crew.preview import preview_enabled

        return preview_enabled() and sections_shared_crew.load_shared_crew_config().room is True
    except Exception:
        logger.warning("room switch could not be read; treating it as off", exc_info=True)
        return False


class RoomSessionBindings:
    """The bound keys, in one small JSON file next to the webhook store."""

    def __init__(self, base_dir: Path | str | None = None) -> None:
        self._base = Path(base_dir) if base_dir is not None else webhooks.config_dir()

    @property
    def path(self) -> Path:
        return self._base / _FILENAME

    def _load(self) -> list[str]:
        raw: Any = webhooks._read_json(self.path, {})
        keys = raw.get("keys") if isinstance(raw, dict) else None
        return [k for k in keys if isinstance(k, str)] if isinstance(keys, list) else []

    def is_bound(self, key: str) -> bool:
        return is_bound_key_shape(key) and key in self._load()

    def bind(self, key: str) -> None:
        if not is_bound_key_shape(key):
            raise ValueError("not a bound-session key")
        with webhooks.locked(self.path):
            keys = self._load()
            if key in keys:
                return
            if len(keys) >= MAX_BINDINGS:
                raise ValueError("too many bound sessions")
            webhooks.write_json_atomic(self.path, {"keys": [*keys, key]})

    def unbind(self, key: str) -> None:
        with webhooks.locked(self.path):
            keys = self._load()
            if key in keys:
                webhooks.write_json_atomic(self.path, {"keys": [k for k in keys if k != key]})


def bindings() -> RoomSessionBindings:
    """The registry on the live data home (resolved per call, never at import)."""
    return RoomSessionBindings()
