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

import string
from html.parser import HTMLParser
from typing import Any, Final, Mapping

#: The feature preview name under ``dashboard.feature_previews``.
PREVIEW: Final[str] = "dashboard_edit_mode"

#: The attribute that names one component (block) of a page.
BLOCK_ATTRIBUTE: Final[str] = "data-dashboard-block"

#: Longest component id, in characters.
MAX_BLOCK_ID_LEN: Final[int] = 48

_LOWER_DIGITS: Final[frozenset[str]] = frozenset(string.ascii_lowercase + string.digits)
_BLOCK_CHARS: Final[frozenset[str]] = _LOWER_DIGITS | frozenset("_-")

#: Components one page may name. A page is a handful of cards; more is a page that
#: tags every element, which no flip or comment can use.
MAX_BLOCKS: Final[int] = 64

#: Longest field path a pick may name, in characters.
MAX_PATH_LEN: Final[int] = 200

#: Longest root field or ``.key`` step of a field path, and digits in an ``[n]`` step.
_MAX_NAME_LEN: Final[int] = 64
_MAX_INDEX_DIGITS: Final[int] = 4

_ROOT_CHARS: Final[frozenset[str]] = frozenset(string.ascii_lowercase + string.digits + "_")
_KEY_CHARS: Final[frozenset[str]] = frozenset(string.ascii_letters + string.digits + "_")


def is_block_id(value: Any) -> bool:
    """Whether *value* is a component id: 1-48 of ``a-z 0-9 _ -``, led by ``a-z 0-9``.

    A short ASCII slug, so it is safe as an attribute value and as text.
    """
    return (
        isinstance(value, str)
        and 1 <= len(value) <= MAX_BLOCK_ID_LEN
        and value[0] in _LOWER_DIGITS
        and all(ch in _BLOCK_CHARS for ch in value)
    )


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
    from kiro_crew import edition_locks

    # Through the edition lock, like every preview reader: an LTS build reads it off.
    previews = edition_locks.feature_previews(
        getattr(getattr(cfg, "dashboard", None), "feature_previews", None)
    )
    return isinstance(previews, Mapping) and previews.get(PREVIEW) is True


class _BlockIds(HTMLParser):
    """The ``data-dashboard-block`` values on START TAGS, in page order.

    A selector in a script (``querySelector('[data-dashboard-block="x"]')``), a style
    rule or a comment names no component: the parser reads script and style bodies as
    text and comments as comments, so only an attribute on an element counts.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name == BLOCK_ATTRIBUTE:
                self.ids.append(value or "")


def block_ids(html: str) -> list[str]:
    """Every component id *html* names on an element, in page order, duplicates kept."""
    parser = _BlockIds()
    parser.feed(html)
    parser.close()
    return parser.ids


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
        if not is_block_id(block):
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

    The root is 1-64 of ``a-z 0-9 _`` led by ``a-z``. Each step is ``.key`` (1-64 of
    ``A-Z a-z 0-9 _``), ``[n]`` (1-4 ASCII digits) or ``[*]``. ``parseFieldPath`` in
    ``website/src/pages/members/dashboardEdit.ts`` walks the same grammar, and
    ``test/fixtures/dashboard_edit_paths.json`` holds both to it.
    """
    if not isinstance(path, str) or not path or len(path) > MAX_PATH_LEN:
        return None
    end = len(path)
    if path[0] not in string.ascii_lowercase:
        return None
    at = 1
    while at < end and at < _MAX_NAME_LEN and path[at] in _ROOT_CHARS:
        at += 1
    root = path[:at]
    steps: list[str | int] = []
    while at < end:
        if path[at] == ".":
            start = at = at + 1
            while at < end and at - start < _MAX_NAME_LEN and path[at] in _KEY_CHARS:
                at += 1
            if at == start:
                return None
            steps.append(path[start:at])
        elif path[at] == "[":
            close = path.find("]", at + 1)
            if close < 0:
                return None
            inner = path[at + 1 : close]
            if inner == "*":
                steps.append("*")
            elif 1 <= len(inner) <= _MAX_INDEX_DIGITS and inner.isascii() and inner.isdigit():
                steps.append(int(inner))
            else:
                return None
            at = close + 1
        else:
            return None
    # `[*]` is a count over a list, so it can only be the last step.
    if "*" in steps[:-1]:
        return None
    return root, steps


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
