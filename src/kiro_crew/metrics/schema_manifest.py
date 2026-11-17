"""The rules for ``schema_manifest.json``, the exported metric schema.

``schema_manifest.json`` (next to this file) lists every metric a product
destination may receive. It is append-only: a downstream reader builds queries
on its names, keys and values, so none of them may disappear or change meaning.
:func:`is_additive` states that rule over two manifest documents.

Standard library only, and no ``kiro_crew`` import: CI's
``scripts/check_metrics_schema_history.py`` loads this file by path in a job that
does not install the package, and ``test/metrics_schema_helpers.py`` imports it
for the code check. One copy of the rule serves both.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

#: Row fields that never change once a row is in the manifest.
FIXED_FIELDS = ("kind", "unit", "scope", "temporality")


def value_tokens(spec: Dict[str, Any], field: str = "values") -> Optional[set]:
    """A key's value list as comparable tokens (``True`` and ``1`` stay apart)."""
    if field not in spec:
        return None
    return {json.dumps(v) for v in spec[field]}


def manifest_rows(document: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {row["name"]: row for row in document.get("metrics", [])}


def is_additive(old: Dict[str, Any], new: Dict[str, Any]) -> List[str]:
    """What *new* takes away from *old*, one line per problem; empty when it only grows.

    Allowed: new rows, keys and values; a row, key or value moving from active
    to deprecated. Everything else that differs is a problem.
    """
    problems: List[str] = []
    old_rows, new_rows = manifest_rows(old), manifest_rows(new)
    for name, before in sorted(old_rows.items()):
        after = new_rows.get(name)
        if after is None:
            problems.append(f"{name}: row removed (a removal or a rename); deprecate it instead")
            continue
        for field in FIXED_FIELDS:
            if before.get(field) != after.get(field):
                problems.append(
                    f"{name}: {field} changed from {before.get(field)!r} to {after.get(field)!r}"
                )
        if before.get("status") == "deprecated" and after.get("status") != "deprecated":
            problems.append(f"{name}: a deprecated row was made active again")
        old_keys, new_keys = before.get("attributes", {}), after.get("attributes", {})
        for key, old_spec in sorted(old_keys.items()):
            new_spec = new_keys.get(key)
            where = f"{name}: attribute {key!r}"
            if new_spec is None:
                problems.append(f"{where} removed; deprecate it instead")
                continue
            if old_spec.get("status") == "deprecated" and new_spec.get("status") != "deprecated":
                problems.append(f"{where} was deprecated and is active again")
            old_values, new_values = value_tokens(old_spec), value_tokens(new_spec)
            if (old_values is None) != (new_values is None):
                problems.append(f"{where} changed between an open and a closed value set")
                continue
            if old_values is None or new_values is None:
                continue
            old_gone = value_tokens(old_spec, "deprecated_values") or set()
            new_gone = value_tokens(new_spec, "deprecated_values") or set()
            for value in sorted(old_values - new_values - new_gone):
                problems.append(f"{where} value {value} removed; deprecate it instead")
            for value in sorted(old_gone - new_gone):
                problems.append(f"{where} deprecated value {value} was dropped or reused")
    return problems
