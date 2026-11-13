"""The Mermaid pako verdict: whether one decoded diagram state may keep its link.

:mod:`kiro_crew.security.redaction_entropy` declares the pako route and decodes a
canonical state into its textual views. This module judges those views: each
distinct view, and a check-only variant of it that is never emitted, must come
through the composed URL-and-credential pass unchanged, within the decoded-byte
and view budgets the caller's top-level links share.

:mod:`kiro_crew.security.redaction` is the import path of every name defined
here.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Callable
from urllib.parse import unquote

from .redaction_entropy import _IN_DIAGRAM_SCAN, _MERMAID_PAKO_SOURCE_CAP, _mermaid_pako_views

_PAKO_CHECK_SCHEME_RE = re.compile(r"https?://", re.IGNORECASE)
_PAKO_CHECK_FRAGMENT_RE = re.compile(r"(https?://[^\s)\"'>/?#]+)#")
_PAKO_CHECK_DECODE_ROUNDS = 3


def _pako_check_variant(view: str) -> str | None:
    """Return *view* as the URL pass should see it, or ``None`` if still encoded.

    Only the pako verdict judges this text; it is never emitted. The URL pass
    matches a lowercase scheme and a path or query after the host, so the
    variant decodes percent-escapes to a fixpoint, lowercases each scheme and
    scans a fragment right after the host as a query.
    """
    for _ in range(_PAKO_CHECK_DECODE_ROUNDS):
        decoded = unquote(view)
        if decoded == view:
            break
        view = decoded
    else:
        if unquote(view) != view:
            return None
    view = _PAKO_CHECK_SCHEME_RE.sub(lambda m: m.group().lower(), view)
    return _PAKO_CHECK_FRAGMENT_RE.sub(r"\1?", view)


def _pako_view_unchanged(
    view: str, compose: Callable[[str], tuple[str, list[str], list[str]]]
) -> bool:
    if compose(view)[0] != view:
        return False
    variant = _pako_check_variant(view)
    return variant is not None and (variant == view or compose(variant)[0] == variant)


def _mermaid_pako_verdict(
    payload: str,
    limit: int,
    view_limit: int,
    token_param_prefixes: tuple[str, ...] | None = None,
) -> tuple[bool, int, int]:
    """Keep one state only after bounded composed-redaction scans of its textual views.

    Returns the verdict with the decoded bytes and the distinct views it spent
    from the two budgets the caller's top-level links share.
    ``token_param_prefixes`` is the enclosing call's pass-4 snapshot, as for
    :func:`~kiro_crew.security.redaction._plantuml_verdict`.
    """
    if view_limit <= 0:
        return False, 0, 0
    views, spent = _mermaid_pako_views(payload, min(limit, _MERMAID_PAKO_SOURCE_CAP))
    if views is None:
        return False, spent, 0
    # Each distinct view costs one composed pass and an identical one is judged
    # once. A state that needs more views than are left fails closed and spends
    # the rest, so the links after it fail closed too, as the byte budget does.
    views = tuple(dict.fromkeys(views))
    if len(views) > view_limit:
        return False, spent, view_limit + 1

    # Each view is judged by the composed URL-and-credential pass `redact`
    # applies, so a URL in the diagram is percent-decoded like any other URL.
    # Imported here: `pass_compose` imports the redaction facade, which imports this module.
    from kiro_crew.security.pass_compose import compose

    # A link decoded from a diagram is ordinary text, never another exemption.
    # This matches PlantUML's established no-nested-decoding rule and keeps the
    # validation cost bounded by this call's decoded-byte and view budgets.
    diagram_token = _IN_DIAGRAM_SCAN.set(True)
    try:
        # No exempt host: this call has no host snapshot to judge under, and a live
        # read is one a cached caller's key does not cover, so a diagram URL that
        # only an exemption keeps voids the link (fails closed).
        judge = functools.partial(
            compose, token_param_prefixes=token_param_prefixes, exempt_hosts=frozenset()
        )
        return all(_pako_view_unchanged(view, judge) for view in views), spent, len(views)
    finally:
        _IN_DIAGRAM_SCAN.reset(diagram_token)
