"""The manifest ``contributes.sessionCreateItems`` block: app rows in the Sessions New menu.

An app that must prepare a project before a session exists (mount a filesystem, check
out a worktree) declares a row here. The host renders the row itself, POSTs to the
app's own ``endpoint`` when it is picked, and then creates the session on the project
directory the endpoint answers with. No app code runs in the menu.

What a declaration can and cannot do is decided here, before anything reads it:

- ``id`` is a lowercase kebab slug, unique in the app;
- ``id`` is at most 64 characters and ``endpoint`` at most 512;
- ``label`` (1 to 120 UTF-16 units) and ``icon`` (at most 120) are app literals;
- ``endpoint`` is required; that it routes under ``/api/apps/<app>/`` is checked by
  ``AppManifest.validate``, which knows the app's name;
- an app declares at most :data:`MAX_SESSION_CREATE_ITEMS_PER_APP` rows.

A malformed block is kept with its errors: ``AppManifest.validate`` refuses the
install. ``website/src/apps/sessionCreateContributions.tsx`` mirrors every rule, because
a manifest can reach the dashboard without passing this check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: Mirrored by ``MAX_SESSION_CREATE_ITEMS_PER_APP`` in ``sessionCreateContributions.tsx``.
#: Lower than the file-menu cap: every row lands in one shared, short create menu.
MAX_SESSION_CREATE_ITEMS_PER_APP = 3
#: Same bound as a file-menu row's label (``_MAX_TITLE`` in ``manifest_contributes``).
MAX_SESSION_CREATE_LABEL = 120
#: Bounds on the two fields with no other length cap; mirrored in the dashboard.
MAX_SESSION_CREATE_ID = 64
MAX_SESSION_CREATE_ENDPOINT = 512
#: Same grammar as a contributed command or file-menu row id.
SESSION_CREATE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\Z")
_SESSION_CREATE_KEYS = frozenset({"id", "label", "icon", "endpoint"})


def _session_create_len(text: str) -> int:
    """Length the way JavaScript's ``.length`` counts it, so both sides cap alike."""
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


@dataclass(frozen=True)
class SessionCreateItem:
    id: str
    label: str
    endpoint: str
    icon: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "label": self.label, "endpoint": self.endpoint}
        if self.icon:
            out["icon"] = self.icon
        return out


@dataclass
class SessionCreateItemsConfig:
    items: list[SessionCreateItem] = field(default_factory=list)
    #: Problems found while reading; any one refuses the install. Not serialized.
    errors: list[str] = field(default_factory=list)

    @classmethod
    def from_value(cls, value: object) -> SessionCreateItemsConfig:
        if value is None:
            return cls()
        if not isinstance(value, list):
            return cls(errors=["contributes.sessionCreateItems must be an array"])
        if len(value) > MAX_SESSION_CREATE_ITEMS_PER_APP:
            # Refused before any row is kept, so an over-cap list retains nothing.
            return cls(
                errors=[
                    f"contributes.sessionCreateItems: {len(value)} items exceeds the limit "
                    f"of {MAX_SESSION_CREATE_ITEMS_PER_APP}"
                ]
            )
        errors: list[str] = []
        items: list[SessionCreateItem] = []
        seen: set[str] = set()
        for index, entry in enumerate(value):
            where = f"contributes.sessionCreateItems[{index}]"
            if not isinstance(entry, dict):
                errors.append(f"{where} must be an object")
                continue
            unknown = sorted(set(entry) - _SESSION_CREATE_KEYS)
            if unknown:
                errors.append(f"{where} has unknown keys: {', '.join(unknown)}")
            item_id, label = entry.get("id"), entry.get("label")
            icon, endpoint = entry.get("icon", ""), entry.get("endpoint")
            if (
                not isinstance(item_id, str)
                or len(item_id) > MAX_SESSION_CREATE_ID
                or not SESSION_CREATE_ID_RE.match(item_id)
            ):
                errors.append(
                    f"{where}.id must be a lowercase kebab slug of at most {MAX_SESSION_CREATE_ID}"
                )
                continue
            if item_id in seen:
                errors.append(f"{where}.id {item_id!r} is declared twice")
                continue
            seen.add(item_id)
            if not isinstance(label, str) or not label.strip():
                errors.append(f"{where}.label is required")
                continue
            if _session_create_len(label) > MAX_SESSION_CREATE_LABEL:
                errors.append(f"{where}.label exceeds {MAX_SESSION_CREATE_LABEL} characters")
                continue
            if not isinstance(icon, str) or _session_create_len(icon) > MAX_SESSION_CREATE_LABEL:
                errors.append(
                    f"{where}.icon must be a string of at most {MAX_SESSION_CREATE_LABEL}"
                )
                continue
            if not isinstance(endpoint, str) or not endpoint:
                errors.append(f"{where}.endpoint is required")
                continue
            if len(endpoint) > MAX_SESSION_CREATE_ENDPOINT:
                errors.append(f"{where}.endpoint exceeds {MAX_SESSION_CREATE_ENDPOINT} characters")
                continue
            items.append(SessionCreateItem(id=item_id, label=label, endpoint=endpoint, icon=icon))
        return cls(items=items, errors=errors)

    def to_list(self) -> list[dict[str, Any]]:
        return [i.to_dict() for i in self.items]
