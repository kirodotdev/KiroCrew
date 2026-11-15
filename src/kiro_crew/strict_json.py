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
``JSON.stringify`` writes for it. ``json_plain`` goes one step further for a
value a script hands back (event data and the run result): what ``json``
cannot write at all becomes its text. This module has no side effects on import.
"""

from __future__ import annotations

import json
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


def json_plain(value: Any) -> Any:
    """Return ``value`` as strict JSON, writing what ``json`` cannot as its text.

    ``json_portable`` first. A value ``json`` then writes as-is comes back as
    that same object, so a JSON value is untouched. Otherwise a set, a date or
    any other object becomes its ``str``, as the run store's ``default=str``
    already wrote it to disk, and a dict key that is not a string, number, bool
    or ``None`` (a tuple) becomes its ``str`` too, so the live snapshot and the
    record reloaded after a restart hold the same value. A mapping whose text
    key would collide with another key becomes its ``str`` whole, so no entry
    is dropped.
    """
    value = json_portable(value)
    try:
        json.dumps(value, allow_nan=False)
        return value
    except (TypeError, ValueError):
        return _plain(value)


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float)):
        return value
    if isinstance(value, dict):
        plain = {
            key if key is None or isinstance(key, (str, int, float)) else str(key): _plain(item)
            for key, item in value.items()
        }
        # A text key that collides with another key, as Python or as the JSON key
        # it is written as (the text "1" and the number 1 are both written "1"),
        # would drop a value, so the whole mapping becomes its text instead.
        written = json.loads(json.dumps(dict.fromkeys(plain, 0)))
        return plain if len(written) == len(value) else str(value)
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return str(value)
