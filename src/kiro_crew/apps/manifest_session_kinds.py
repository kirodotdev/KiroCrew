"""``contributes.sessionKinds``: a kind of session whose main pane an app draws.

A kind is a row in the sidebar's create menu. Choosing it creates an ordinary chat
slot bound to ``{app, kind, object}`` (``dashboard/session_kinds.py``), and the
dashboard mounts the kind's ``entry`` in place of the transcript, through the same
in-process ESM host a ``panelTabs`` body uses. So a kind is declarative data for the
same reason a panel tab is: core reads it and mounts a module, and no app code crosses
into a host surface. ``SessionKindConfig`` is one entry; ``session_kinds_errors`` is
the block's own refusals, which ``Contributes.validate`` adds to its own.

Served through :mod:`kiro_crew.apps.manifest`, which callers and tests import and patch;
this module never imports it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from kiro_crew.apps.manifest_labels import _MAX_TITLE, _mirrored_len

#: A kind id is stored in every bound session's metadata line and joined into the
#: create menu's row key, so it is the storage-safe slug a panel-tab id is. Applied
#: with ``.fullmatch()``: ``.match()`` would accept a trailing newline. The create
#: route and the metadata reader re-check a bound kind against this same pattern.
SESSION_KIND_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

#: Most session kinds one app may contribute. Each is a row in the sidebar's create
#: menu beside the built-in New chat rows, a short list a reader scans by eye, so one
#: app may add only a few.
MAX_SESSION_KINDS_PER_APP = 4

#: A kind's ``defaultAgent``: a bare agent config stem, the grammar a contributed
#: command's ``agent`` has. The dashboard passes it to the create as a template pick,
#: so a separator would name a path rather than an agent.
_SESSION_KIND_AGENT_RE = re.compile(r"[A-Za-z0-9._-]{1,120}")


@dataclass
class SessionKindConfig:
    """One kind of session an app contributes."""

    id: str = ""  # stable kind id, unique within the app
    title: str = ""  # the kind's name, shown on a bound session
    menuLabel: str = ""  # create-menu row label  # noqa: N815
    menuDescription: str = ""  # one-line description under the row  # noqa: N815
    icon: str = ""  # lucide icon name, resolved by the reader against lucide-react
    # ESM entry mounted as the session's pane, resolved against the app's ``ui``
    # directory, as a panel tab's ``entry`` is: ``dist/pane.mjs`` is the file at
    # ``<app root>/ui/dist/pane.mjs``.
    entry: str = ""
    # The agent the create menu starts a session of this kind on, or "" for the
    # dashboard default.
    defaultAgent: str = ""  # noqa: N815

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        for key in (
            "id",
            "title",
            "menuLabel",
            "menuDescription",
            "icon",
            "entry",
            "defaultAgent",
        ):
            value = getattr(self, key)
            if value:
                d[key] = value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionKindConfig:
        return cls(
            id=str(data.get("id", "")),
            title=str(data.get("title", "")),
            menuLabel=str(data.get("menuLabel", "")),  # noqa: N815
            menuDescription=str(data.get("menuDescription", "")),  # noqa: N815
            icon=str(data.get("icon", "")),
            entry=str(data.get("entry", "")),
            defaultAgent=str(data.get("defaultAgent", "")),  # noqa: N815
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        where = f"contributes.sessionKinds[{self.id or '?'}]"
        if not self.id:
            errors.append("contributes.sessionKinds: entry missing id")
        elif not SESSION_KIND_ID_RE.fullmatch(self.id):
            errors.append(f"{where}: id must be a storage-safe slug ({self.id!r})")
        for name, value in (
            ("title", self.title),
            ("menuLabel", self.menuLabel),
            ("entry", self.entry),
        ):
            if not value:
                errors.append(f"{where}: missing {name}")
        # Every label the create menu renders is capped, the description included:
        # an uncapped one would be the single field an app could use to blow out the
        # menu's layout.
        for name, value in (
            ("title", self.title),
            ("menuLabel", self.menuLabel),
            ("menuDescription", self.menuDescription),
            ("icon", self.icon),
        ):
            if _mirrored_len(value) > _MAX_TITLE:
                got = _mirrored_len(value)
                errors.append(f"{where}: {name} exceeds {_MAX_TITLE} characters ({got})")
        if self.defaultAgent and not _SESSION_KIND_AGENT_RE.fullmatch(self.defaultAgent):
            errors.append(
                f"{where}: defaultAgent must be a bare agent name ({self.defaultAgent!r})"
            )
        # `entry` traversal is checked in `AppManifest.validate`, which is the only
        # scope that knows the app root.
        return errors


def session_kinds_errors(kinds: list[SessionKindConfig], *, bad: bool, dropped: int) -> list[str]:
    """The block's refusals: its shape, its cap, each entry's own, and duplicate ids.

    *bad* is whether the manifest's ``sessionKinds`` was present but not an array, and
    *dropped* how many entries of a well-formed array were not objects. Either one
    would otherwise install clean with fewer kinds than declared, which reads exactly
    like an app that simply has none.
    """
    errors: list[str] = []
    if bad:
        errors.append(
            "contributes.sessionKinds must be an array -- a non-array value passes as "
            "empty and then disappears from the serialized manifest, so the app "
            "author sees neither an error nor a create-menu row"
        )
    if dropped:
        errors.append(
            f"contributes.sessionKinds: {dropped} entr{'y' if dropped == 1 else 'ies'} "
            "must be an object -- a non-object entry is filtered out before "
            "validation, so the app installs with the remaining kinds and its "
            "author is never told one was dropped"
        )
    if len(kinds) > MAX_SESSION_KINDS_PER_APP:
        errors.append(
            f"contributes.sessionKinds: {len(kinds)} kinds exceeds the "
            f"limit of {MAX_SESSION_KINDS_PER_APP}"
        )
    seen: set[str] = set()
    for kind in kinds:
        errors.extend(kind.validate())
        if kind.id:
            if kind.id in seen:
                # A bound session names its kind by id, so two kinds with one id
                # would leave every such session drawn by whichever came first.
                errors.append(f"contributes.sessionKinds: duplicate id {kind.id!r}")
            seen.add(kind.id)
    return errors
