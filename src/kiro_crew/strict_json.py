"""Keep workflow run records strict JSON: no ``Infinity``/``NaN`` tokens.

Python's ``json`` writes ``float('inf')`` and ``float('nan')`` as the bare
tokens ``Infinity`` and ``NaN`` and reads them back without complaint, but they
are not JSON: the dashboard's ``JSON.parse`` rejects the whole run snapshot. A
script reaches such a value easily -- ``ctx.budget.remaining()`` is ``math.inf``
when the run has no budget -- and so does a model reply parsed by ``json.loads``.

``json_portable`` is applied where script or agent values enter a run record
(event data, the run result, per-call results) and when a stored run is loaded,
so every reader of the record -- HTTP, the run store, the MCP tools -- sees the
same strict JSON. A non-finite float becomes ``None``, the value a browser's
``JSON.stringify`` writes for it. This module has no side effects on import.
"""

from __future__ import annotations

import math
from typing import Any


def json_portable(value: Any) -> Any:
    """Return ``value`` with every non-finite float replaced by ``None``.

    Dicts, lists and tuples are walked recursively. A container holding no
    non-finite float is returned as the same object, so the common case copies
    nothing; a container that does is rebuilt (a tuple as a list, which is how
    ``json`` writes it anyway), and the caller's object is never mutated. Dict
    keys are left alone: ``json`` writes a float key as a quoted string, which
    is valid JSON.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        safe = {key: json_portable(item) for key, item in value.items()}
        if all(safe[key] is item for key, item in value.items()):
            return value
        return safe
    if isinstance(value, (list, tuple)):
        items = [json_portable(item) for item in value]
        if all(new is old for new, old in zip(items, value)):
            return value
        return items
    return value
