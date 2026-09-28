#!/usr/bin/env python3
"""Deterministic blast-radius signals (design §3, component 2).

Token-free, repeatable objective signals over a ReviewTarget's files:
sensitive-glob hits, LOC added/removed, guard-clause removals, and import
fan-out. Used twice (design §3): as an input to the Phase 1 design gate, and as
a primary fact in the final Focus Report.

``rate()`` maps signals → SMALL / MEDIUM / LARGE. The thresholds are module
constants so they stay tunable in one place.
"""
from __future__ import annotations

import re
from typing import Any

# --- tunable thresholds (one place) ----------------------------------------
LARGE_LOC = 300
LARGE_GUARDS = 3
LARGE_SENSITIVE = 3
LARGE_SENSITIVE_LOC = 30          # sensitive touch + this much churn => LARGE
MEDIUM_LOC = 80
MEDIUM_FANOUT = 6

# A removed line looks like a guard if it carries control-flow / nullness checks.
_GUARD_RE = re.compile(
    r"\b(if|elif|return|raise|assert|try|except|continue|break|guard)\b"
    r"|is\s+None|is\s+not\s+None|!=\s*None|==\s*None|!==?\s*null|===?\s*null",
    re.I,
)
# An added line that imports/requires another module (fan-out signal).
_IMPORT_RE = re.compile(
    r"^\s*(import\s+[\w.]|from\s+\S+\s+import\b|#\s*include|require\s*\(|use\s+\w|import\s*\{)"
)


def _glob_to_regex(pattern: str) -> re.Pattern:
    """Translate a glob (with ``**`` crossing ``/``) into a full-match regex."""
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        if pattern[i:i + 3] == "**/":
            out.append("(?:.*/)?")
            i += 3
        elif pattern[i:i + 2] == "**":
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def glob_match(path: str, pattern: str) -> bool:
    return _glob_to_regex(pattern).match(path) is not None


def match_sensitive(path: str, sensitive_globs: list[str]) -> list[str]:
    """Return the list of globs the path matched (empty if none)."""
    return [g for g in sensitive_globs if glob_match(path, g)]


def _diff_lines(diff: str):
    """Yield (sign, content) for added/removed lines, skipping diff headers."""
    for line in (diff or "").splitlines():
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            continue
        if line.startswith("+"):
            yield "+", line[1:]
        elif line.startswith("-"):
            yield "-", line[1:]


def file_signals(diff: str) -> dict:
    """Compute per-file signals from a unified diff."""
    added = removed = guard_removals = import_fanout = 0
    for sign, content in _diff_lines(diff):
        if sign == "+":
            added += 1
            if _IMPORT_RE.match(content):
                import_fanout += 1
        else:
            removed += 1
            if _GUARD_RE.search(content):
                guard_removals += 1
    return {
        "loc_added": added,
        "loc_removed": removed,
        "guard_removals": guard_removals,
        "import_fanout": import_fanout,
    }


def extract_signals(files: list[dict], sensitive_globs: list[str]) -> dict:
    """Aggregate signals across all files in a ReviewTarget."""
    agg: dict[str, Any] = {"sensitive_hits": [], "loc_added": 0, "loc_removed": 0,
                           "guard_removals": 0, "import_fanout": 0}
    for f in files or []:
        path = f.get("path", "")
        globs = match_sensitive(path, sensitive_globs or [])
        if globs:
            agg["sensitive_hits"].append({"path": path, "globs": globs})
        fs = file_signals(f.get("diff", ""))
        agg["loc_added"] += fs["loc_added"]
        agg["loc_removed"] += fs["loc_removed"]
        agg["guard_removals"] += fs["guard_removals"]
        agg["import_fanout"] += fs["import_fanout"]
    return agg


def rate(signals: dict) -> str:
    """Map aggregated signals to SMALL / MEDIUM / LARGE (deterministic)."""
    sensitive = len(signals.get("sensitive_hits", []))
    loc = signals.get("loc_added", 0) + signals.get("loc_removed", 0)
    guards = signals.get("guard_removals", 0)
    fanout = signals.get("import_fanout", 0)

    if ((sensitive >= 1 and (loc >= LARGE_SENSITIVE_LOC or guards >= 1))
            or loc >= LARGE_LOC or guards >= LARGE_GUARDS or sensitive >= LARGE_SENSITIVE):
        return "LARGE"
    if sensitive >= 1 or loc >= MEDIUM_LOC or guards >= 1 or fanout >= MEDIUM_FANOUT:
        return "MEDIUM"
    return "SMALL"


def analyze(files: list[dict], sensitive_globs: list[str]) -> dict:
    """Convenience: signals + rating in one call (the result-record shape)."""
    signals = extract_signals(files, sensitive_globs)
    return {"rating": rate(signals), "signals": signals}
