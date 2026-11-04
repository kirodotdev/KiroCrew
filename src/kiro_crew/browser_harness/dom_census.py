"""Counts read from the browser's own view of a tab's DOM (``DOM.getDocument`` with
``pierce``), for the guards that page script cannot fool or hide from.

The helper worker carries its own copy of this logic (``helper_worker.py`` imports
nothing from ``kiro_crew``); ``test/test_browser_guard_isolated_count.py`` keeps
the two equal.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

#: ``autocomplete`` tokens that name a secret (the replay mask's rule).
SECRET_AUTOCOMPLETE = ("current-password", "new-password", "one-time-code", "cc-csc")
#: The walker's ``SECRET_NAME_RE`` (snapshot_walker.js), kept equal by a test.
SECRET_NAME_RE = re.compile(
    r"(^|[^a-z])(password|passwd|pwd|otp|totp|mfa|cvv|cvc|secret|token|apikey)([^a-z]|$)",
    re.IGNORECASE,
)


def split_camel(text: str) -> str:
    """*text* with camelCase words split (``otpCode`` -> ``otp Code``), as the
    walker's ``splitCamel`` splits them before the name rule is tested."""
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)


class MalformedDocument(ValueError):
    """The browser's DOM reply is not the shape a census can trust."""


def walk(root: Any) -> Iterator[tuple[dict[str, Any], bool]]:
    """Every node under *root*, with whether it sits inside a closed shadow root."""
    pending: list[tuple[Any, bool]] = [(root, False)]
    while pending:
        node, closed = pending.pop()
        if not isinstance(node, dict) or not isinstance(node.get("nodeName"), str):
            raise MalformedDocument("malformed node")
        yield node, closed
        kids = node.get("children")
        if kids is not None:
            if not isinstance(kids, list):
                raise MalformedDocument("malformed node")
            pending.extend((k, closed) for k in kids)
        roots = node.get("shadowRoots")
        if roots is not None:
            if not isinstance(roots, list):
                raise MalformedDocument("malformed node")
            for r in roots:
                inner = closed or (isinstance(r, dict) and r.get("shadowRootType") == "closed")
                pending.append((r, inner))
        doc = node.get("contentDocument")
        if doc is not None:
            pending.append((doc, closed))


def is_secret_field(node: dict[str, Any]) -> bool:
    """Whether a DOM node is a field the walker's ``secretish`` names a secret."""
    if node["nodeName"].upper() not in ("INPUT", "TEXTAREA"):
        return False
    flat = node.get("attributes") or []
    if not isinstance(flat, list):
        raise MalformedDocument("malformed node")
    attrs = {str(flat[i]).lower(): str(flat[i + 1]) for i in range(0, len(flat) - 1, 2)}
    if attrs.get("type", "").lower() == "password" or "data-kc-asked" in attrs:
        return True
    auto = attrs.get("autocomplete", "").lower()
    if any(word in auto.split() for word in SECRET_AUTOCOMPLETE):
        return True
    named = " ".join((attrs.get("name", ""), attrs.get("id", ""), attrs.get("autocomplete", "")))
    # Both spellings: ``apiKey`` matches as written, ``otpCode`` only once split.
    return bool(SECRET_NAME_RE.search(named) or SECRET_NAME_RE.search(split_camel(named)))


#: The elements that can hold a document of their own.
FRAME_OWNERS = frozenset({"IFRAME", "FRAME", "OBJECT", "EMBED"})


def uninspected_frame(node: dict[str, Any]) -> bool:
    """Whether *node* owns a document the browser did not hand back inline."""
    return node["nodeName"].upper() in FRAME_OWNERS and node.get("contentDocument") is None


def closed_secret_count(root: Any) -> int:
    """How many secret-looking fields sit inside closed shadow roots under *root*.

    A frame owner in a closed root with no document handed back inline runs out of
    process and nothing looked inside it, so it counts as one secret field.
    """
    return sum(
        1
        for node, closed in walk(root)
        if closed and (is_secret_field(node) or uninspected_frame(node))
    )
