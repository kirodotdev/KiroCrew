"""Dashboard edit mode (feature preview ``dashboard_edit_mode``): the gateway's half.

With the preview on, the person can turn on Edit on a Bot's Dashboard tab, point at a
value and see where it came from, flip a component to read its provider and ask the Bot
to change it, and correct a value the Bot wrote. The host draws all of it; the page
only answers "which field is under this point" and never supplies lineage text.

This module holds what more than one gateway file needs to agree on:

* :func:`edit_mode_on`, the one read of the switch. Off is every page, every write and
  every turn exactly as without the preview.
* the component id a page carries (:data:`BLOCK_ATTRIBUTE`) and its write-gate check
  (:func:`block_id_refusal`).
* the field-path grammar a pick may name (:func:`parse_field_path`).
"""

from __future__ import annotations

import re
from typing import Any, Final, Mapping

#: The feature preview name under ``dashboard.feature_previews``.
PREVIEW: Final[str] = "dashboard_edit_mode"

#: The attribute that names one component (block) of a page.
BLOCK_ATTRIBUTE: Final[str] = "data-dashboard-block"

#: A component id: a short slug, so it is safe as an attribute value and as text.
BLOCK_ID_RE: Final[re.Pattern[str]] = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,47}\Z")

#: Components one page may name. A page is a handful of cards; more is a page that
#: tags every element, which no flip or comment can use.
MAX_BLOCKS: Final[int] = 64

#: Longest field path a pick may name, in characters.
MAX_PATH_LEN: Final[int] = 200

#: A ``data-dashboard-block`` attribute in page markup, in either quote or bare.
_BLOCK_ATTR_RE = re.compile(r"data-dashboard-block\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s\"'>=`]+))")

#: One step of a field path after its root: ``.key``, ``[3]`` or ``[*]``.
_STEP_RE = re.compile(r"\.([A-Za-z0-9_]{1,64})|\[(\d{1,4}|\*)\]")
_ROOT_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")


def edit_mode_on(cfg: Any = None) -> bool:
    """Whether the preview is on: *cfg*, else the live config, else the disk config.

    Fails closed: a config that cannot be read is OFF.
    """
    if cfg is None:
        try:
            from kiro_crew.config import live

            cfg = live.snapshot()
            if cfg is None:
                from kiro_crew.config.loader import KiroCrewConfig

                cfg = KiroCrewConfig.load()
        except Exception:
            return False
    previews = getattr(getattr(cfg, "dashboard", None), "feature_previews", None)
    return isinstance(previews, Mapping) and previews.get(PREVIEW) is True


def block_ids(html: str) -> list[str]:
    """Every component id *html* names, in page order, duplicates kept."""
    return [a or b or c for a, b, c in _BLOCK_ATTR_RE.findall(html)]


def block_id_refusal(html: str) -> str:
    """Why *html*'s component ids are refused at the write gate, or ``""``.

    Checked on WRITE only, never on read: a stored page keeps drawing whatever it
    holds, and the host treats an unusable id as no id.
    """
    ids = block_ids(html)
    if len(ids) > MAX_BLOCKS:
        return f"names {len(ids)} components; at most {MAX_BLOCKS} are allowed"
    seen: set[str] = set()
    for block in ids:
        if BLOCK_ID_RE.fullmatch(block) is None:
            return (
                f"has a {BLOCK_ATTRIBUTE} value that is not a component id: use 1-48 "
                "lowercase letters, digits, '-' or '_', starting with a letter or digit"
            )
        if block in seen:
            return f"names component {block!r} twice; each {BLOCK_ATTRIBUTE} must be unique"
        seen.add(block)
    return ""


def parse_field_path(path: Any) -> tuple[str, list[str | int]] | None:
    """``(root field, steps)`` for a pick's *path*, or ``None`` when it is not one.

    ``accepted`` -> ``("accepted", [])``; ``items[2].accepted`` ->
    ``("items", [2, "accepted"])``; ``items[*]`` -> ``("items", ["*"])``.
    """
    if not isinstance(path, str) or not path or len(path) > MAX_PATH_LEN:
        return None
    root = _ROOT_RE.match(path)
    if root is None:
        return None
    steps: list[str | int] = []
    at = root.end()
    while at < len(path):
        step = _STEP_RE.match(path, at)
        if step is None:
            return None
        key, index = step.group(1), step.group(2)
        if key is not None:
            steps.append(key)
        elif index == "*":
            steps.append("*")
        else:
            steps.append(int(index))
        at = step.end()
    # `[*]` is a count over a list, so it can only be the last step.
    if "*" in steps[:-1]:
        return None
    return root.group(0), steps


def walk(value: Any, steps: list[str | int]) -> tuple[bool, Any]:
    """``(found, value)`` after following *steps* into *value*.

    ``*`` over a list answers the list's length: the count a page draws over every row.
    """
    for step in steps:
        if step == "*":
            if not isinstance(value, list):
                return False, None
            return True, len(value)
        if isinstance(step, int):
            if not isinstance(value, list) or step >= len(value):
                return False, None
            value = value[step]
            continue
        if not isinstance(value, Mapping) or step not in value:
            return False, None
        value = value[step]
    return True, value
