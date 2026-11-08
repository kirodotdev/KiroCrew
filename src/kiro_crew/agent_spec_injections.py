"""Edition-registered filters for entries another tool injects into Kiro Crew's own specs.

Some tools rewrite every agent spec in the shared agents directory on each of their
launches, adding their own MCP server. Kiro Crew re-derives a spec it owns, so it would
rewrite the file back, and the two would take turns on it forever. A downstream edition
that knows such a tool registers a filter here, at boot, that removes exactly that
tool's injection from a spec. The public core registers none, so with nothing
registered every caller behaves as it did before.

A filter is a judgement about one exact shape, never a grant. Callers accept its
answer only when the filtered spec is strictly equal to the spec Kiro Crew would
write (:func:`only_registered_injection`), and then use their own template, not the
filter's output, so a filter cannot add anything to what runs.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from kiro_crew.user_json import copy_json_document, strict_json_equal

logger = logging.getLogger(__name__)

#: Takes a copy of a parsed spec and returns it with the filter's injection removed,
#: or ``None`` when the spec does not carry that injection in exactly its shape.
SpecInjectionFilter = Callable[[dict[str, Any]], "dict[str, Any] | None"]

_FILTERS: dict[str, SpecInjectionFilter] = {}
_LOCK = threading.Lock()


def register_spec_injection_filter(name: str, spec_filter: SpecInjectionFilter) -> None:
    """Register *spec_filter* under *name*; a second call with the same name replaces it."""
    if not isinstance(name, str) or not name:
        raise ValueError("a spec injection filter needs a non-empty name")
    if not callable(spec_filter):
        raise TypeError(f"spec injection filter {name!r} is not callable")
    with _LOCK:
        _FILTERS[name] = spec_filter


def unregister_spec_injection_filter(name: str) -> None:
    """Remove the filter registered under *name*, if any."""
    with _LOCK:
        _FILTERS.pop(name, None)


def registered_spec_injection_filters() -> tuple[tuple[str, SpecInjectionFilter], ...]:
    """The registered filters, in registration order."""
    with _LOCK:
        return tuple(_FILTERS.items())


def only_registered_injection(spec: object, expected: dict[str, Any]) -> str | None:
    """The name of a filter that turns *spec* back into exactly *expected*, or ``None``.

    ``None`` when *spec* already equals *expected* (there is nothing to tolerate),
    when nothing is registered, or when no filter's output is strictly equal to
    *expected*. A filter that raises counts as no match.
    """
    if not isinstance(spec, dict) or strict_json_equal(spec, expected):
        return None
    for name, spec_filter in registered_spec_injection_filters():
        try:
            filtered = spec_filter(copy_json_document(spec))
        except Exception:
            logger.debug("spec injection filter %r raised; treating it as no match", name)
            continue
        if filtered is not None and strict_json_equal(filtered, expected):
            return name
    return None
