"""The shape of one ``agent.apps_builtin_adoption`` entry, and its validator.

A pure module: the config loader builds the key through :func:`coerce_adoption_markers`
and the adoption state reader validates the base file with the same function, so
the two cannot disagree on what a marker is. A malformed entry is dropped, never
repaired.
"""

from __future__ import annotations

import re
from typing import Literal, TypedDict

#: Marker states. ``granting`` is written with the trust grant, before the tree
#: swap; ``adopted`` after it. An ``adopted`` entry is never removed.
MARKER_STATES = ("granting", "adopted")

#: Bound on the map, like ``execution._MAX_GRANT_ENTRIES``: it is read on demand.
MAX_MARKERS = 512

_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_FLAG_KEYS = ("addedName", "setRepository", "removedLocal")
_KEYS = frozenset({"state", "repository", "commit", *_FLAG_KEYS})


class AdoptionMarker(TypedDict):
    """One adoption marker, keyed by app name in ``agent.apps_builtin_adoption``."""

    state: Literal["granting", "adopted"]
    repository: str
    commit: str
    addedName: bool  # noqa: N815 - the persisted JSON key
    setRepository: bool  # noqa: N815
    removedLocal: bool  # noqa: N815


def marker_is_valid(value: object) -> bool:
    """Whether *value* has exactly the :class:`AdoptionMarker` shape."""
    if not isinstance(value, dict) or set(value) != _KEYS:
        return False
    if value["state"] not in MARKER_STATES:
        return False
    repository = value["repository"]
    if not isinstance(repository, str) or not repository or len(repository) > 512:
        return False
    if not isinstance(value["commit"], str) or not _COMMIT_RE.fullmatch(value["commit"]):
        return False
    return all(isinstance(value[key], bool) for key in _FLAG_KEYS)


def coerce_adoption_markers(raw: object) -> dict[str, AdoptionMarker]:
    """The valid entries of *raw*; anything malformed is dropped. Never raises."""
    if not isinstance(raw, dict):
        return {}
    from kiro_crew.apps.execution import APP_NAME_RE

    out: dict[str, AdoptionMarker] = {}
    for name, value in raw.items():
        if len(out) >= MAX_MARKERS:
            break
        if isinstance(name, str) and APP_NAME_RE.fullmatch(name) and marker_is_valid(value):
            out[name] = AdoptionMarker(
                state=value["state"],
                repository=value["repository"],
                commit=value["commit"],
                addedName=value["addedName"],
                setRepository=value["setRepository"],
                removedLocal=value["removedLocal"],
            )
    return out
