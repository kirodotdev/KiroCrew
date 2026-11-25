"""A ``permissions`` block that names a ``policies`` bundle, read as its ``deny`` and ``ask`` rules.

KAS expands a named bundle into rules held on the backend (``policy-parser.ts``
``expandPolicies`` over ``presets.ts``), where Crew's governance ceiling cannot see
them. So the bundle never travels, and neither does an ``allow`` written beside it:
an author who reached for a bundle has not written the grant Crew could check.
The ``deny`` and ``ask`` rules beside it only narrow, so they travel as written.

A leaf: every reader of an authored block (the KAS projection, the restriction
reader, the parent-grant reader, the Bot worker layering) strips the bundle here,
so they agree on what one means.
"""

from __future__ import annotations

from typing import Any

#: The bundle ids KAS ships (``presets.ts``), each mapped to a literal this module
#: owns. A log line names a bundle by reading the literal OUT of this table, never by
#: passing the spec's own string through: CodeQL's clear-text-logging query follows the
#: spec-derived dataflow into the logger, and a table lookup is the barrier it sees.
_SHIPPED_BUNDLE_LOG_NAMES = {
    "allow-all": "allow-all",
    "approve-for-me": "approve-for-me",
    "dev-shell": "dev-shell",
    "edit-workspace": "edit-workspace",
    "read-all": "read-all",
    "read-only-shell": "read-only-shell",
    "read-workspace": "read-workspace",
}


def bundle_log_names(bundles: list[str]) -> str:
    """*bundles* for a log line: shipped ids by name, any other as ``another bundle``."""
    return ", ".join(_SHIPPED_BUNDLE_LOG_NAMES.get(b, "another bundle") for b in bundles)


def bundle_ids(raw: Any) -> list[str]:
    """The bundle ids *raw* names: a non-empty ``policies`` list of strings, else ``[]``."""
    policies = raw.get("policies") if isinstance(raw, dict) else None
    if not isinstance(policies, list) or not all(isinstance(p, str) for p in policies):
        return []
    return list(policies)


def is_allow_rule(rule: Any) -> bool:
    """Whether *rule* is an ``allow``, the kind a named bundle leaves out beside it."""
    return isinstance(rule, dict) and rule.get("effect") == "allow"


def without_policy_bundle(raw: Any) -> Any:
    """*raw* less its ``policies`` bundle and every ``allow`` rule beside it.

    Unchanged when *raw* names no bundle, or names one in a shape that is not a
    list of strings (the parser refuses that block whole). A bundle with no
    ``rules`` beside it reads as ``rules: []``. A ``rules`` value that is not a list
    is kept as it is, for the parser to refuse.
    """
    if not bundle_ids(raw):
        return raw
    out = {key: value for key, value in raw.items() if key != "policies"}
    rules = raw.get("rules", [])
    out["rules"] = [r for r in rules if not is_allow_rule(r)] if isinstance(rules, list) else rules
    return out


def allows_beside_bundle(raw: Any) -> int:
    """How many ``allow`` rules :func:`without_policy_bundle` leaves out of *raw*."""
    if not bundle_ids(raw) or not isinstance(raw.get("rules"), list):
        return 0
    return sum(1 for r in raw["rules"] if is_allow_rule(r))
