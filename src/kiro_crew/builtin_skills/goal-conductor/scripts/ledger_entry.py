#!/usr/bin/env python3
"""Ledger item-entry codec - the one code owner of the conductor's entry format.

The goal-conductor's only compaction-surviving state is the per-work-item entry
it writes into the session ledger's ``artifacts`` map. Before this script, that
format existed only as prose plus a worked example in SKILL.md, and the model
re-derived it every patrol cycle - which produced two real defects during
review of PR #5652: an acceptance spec that lived only in model context (lost
on compaction), and an entry written as a nested JSON object, which the ledger
rejects with ``artifacts_not_string_map`` so nothing persisted at all.

This script owns the format. The conductor calls it; it never hand-rolls the
encoding again.

Usage:
    python3 ledger_entry.py {encode|decode|validate|rotate} < input.json

Every mode reads one JSON document on stdin and writes one on stdout. Domain
problems (a value too long, a malformed stored entry) are structured
``{"ok": false, "error": {...}}`` results, never crashes - the conductor must
be able to read WHY and re-derive, not lose the sibling results. Exit code 0
means the operation ran; 2 means stdin was not the JSON the mode needs or the
mode name is unknown.

Modes:

``encode``  fields -> the single-line JSON STRING the ledger accepts.
    stdin:  {"accept": {...}, "session": "<key>", "round": 2,
             "status": "running", "since": "<cursor>"}
    stdout: {"ok": true, "value": "<compact json string>", "chars": 123}
    The value is a STRING (the ledger's artifacts map is string->string; a
    nested object is rejected outright). ``since`` is optional. Output is
    deterministic (sorted keys, compact separators) so rewriting an unchanged
    entry produces an identical value.

``decode``  stored value -> structured fields.
    stdin:  {"value": <the stored artifacts value>}
    stdout: {"ok": true, "entry": {...}, "terminal": bool, "complete": bool}
            or {"ok": false, "error": {"code": ..., "detail": ...}}
    ``terminal`` means the status is a finished verdict; ``complete`` means the
    entry still carries the full patrol contract (accept + session). A decode
    failure means a lost item: re-derive it from the child session, never guess.

``validate``  enforce the ledger's REAL bounds before a write is attempted.
    stdin:  {"artifacts": {"item-1": "...", ...}}   (the map about to be written)
    stdout: {"ok": bool, "violations": [{"key": ..., "code": ..., "detail": ...}]}
    The bounds mirror the ledger's own (see the constants below). Validating
    first matters because the ledger does not reject an oversized write - it
    silently TRUNCATES the value at the cap, which corrupts the JSON payload,
    and silently ages out the oldest entries past the entry cap.

``rotate``  collapse or drop terminal entries so finished items cannot age an
    ACTIVE item out of the entry cap.
    stdin:  {"artifacts": {...}}
    stdout: {"ok": true, "artifacts": {...}, "collapsed": [...], "dropped": [...]}
            or {"ok": false, "error": {"code": "cap_exceeded_all_active", ...}}
    Deterministic rule: terminal entries are first collapsed to their one-line
    outcome ({"round": N, "status": "pass"}), then - only if the map still
    exceeds the cap - dropped oldest-first. An active item is NEVER dropped;
    if the cap cannot be met without dropping one, that is a structured error
    for the conductor to surface, not a silent loss.

Stdlib-only, Python 3.8+.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# The ledger's real bounds, mirrored from kiro_crew/session_ledger.py
# (_MAX_TEXT, key clamp in record(), _MAX_ARTIFACTS). Do NOT tune these here:
# test_conductor_ledger_entry.py asserts they equal the ledger's own constants,
# so a ledger-side change fails that test instead of silently drifting.
# The ledger CLAMPS rather than rejects (a long value is truncated, a full map
# ages out its oldest entries), which is exactly why validation must happen on
# this side, before the write.
# ---------------------------------------------------------------------------
MAX_VALUE_CHARS = 2000
MAX_KEY_CHARS = 128
MAX_ENTRIES = 32

#: The full status vocabulary. ``encode`` REJECTS anything else: a synonym like
#: "passed" or "done" would decode as non-terminal, so rotation would treat the
#: finished item as active forever and the map would fill to an unresolvable
#: cap_exceeded_all_active. Failing at authoring time is the cheap failure.
#:
#: Retry semantics are part of the contract: a cycle-level acceptance failure
#: is NOT terminal — the skill's stop condition allows three failures — so on a
#: failed check the entry stays "running" with ``fails`` incremented, and
#: "fail" is written only when the item is finally given up (retries exhausted
#: or the item abandoned). Collapsing on the first failed check would destroy
#: the acceptance spec, session key and cursor that the retry needs.
ALLOWED_STATUSES = frozenset({"running", "waiting", "pass", "fail"})

#: Statuses that mean the item is FINISHED (see the retry semantics above).
#: Rotation never collapses or drops an entry it is not certain is terminal,
#: so an unknown status can cost capacity but never state.
TERMINAL_STATUSES = frozenset({"pass", "fail"})

#: The full entry contract patrol needs to run a cycle without the
#: conversation. ``since`` (read cursor) and ``fails`` (acceptance-failure
#: count) are optional; a collapsed terminal entry keeps only ``round`` and
#: ``status``.
_FIELD_TYPES = {
    "accept": dict,
    "session": str,
    "round": int,
    "status": str,
    "since": str,
    "fails": int,
}
_REQUIRED_FOR_ENCODE = ("accept", "session", "round", "status")


def _error(code: str, detail: str) -> Dict[str, Any]:
    return {"ok": False, "error": {"code": code, "detail": detail}}


def _check_field(name: str, value: Any) -> Optional[str]:
    """Return a problem description when ``value`` has the wrong type."""
    want = _FIELD_TYPES[name]
    # bool is an int subclass; {"round": true} must not encode as round=True.
    if want is int and (not isinstance(value, int) or isinstance(value, bool)):
        return f"{name!r} must be an integer"
    if want is not int and not isinstance(value, want):
        return f"{name!r} must be a {want.__name__}"
    return None


def _encode_value(entry: Dict[str, Any]) -> str:
    """The one serialization: compact, key-sorted, single line."""
    return json.dumps(entry, sort_keys=True, separators=(",", ":"))


def mode_encode(payload: Dict[str, Any]) -> Dict[str, Any]:
    for name in _REQUIRED_FOR_ENCODE:
        if name not in payload:
            return _error("missing_field", f"encode needs {name!r}")
    unknown = sorted(set(payload) - set(_FIELD_TYPES))
    if unknown:
        # Strict on the authoring side: a typo'd field must not silently
        # vanish from the durable entry. (decode stays tolerant of unknown
        # fields so an older helper can read a newer entry.)
        return _error("unknown_field", f"unknown field(s): {', '.join(unknown)}")
    entry: Dict[str, Any] = {}
    for name in _FIELD_TYPES:
        if name not in payload:
            continue
        problem = _check_field(name, payload[name])
        if problem:
            return _error("bad_field_type", problem)
        entry[name] = payload[name]
    if entry["status"] not in ALLOWED_STATUSES:
        return _error(
            "unknown_status",
            f"status {entry['status']!r} is not one of {sorted(ALLOWED_STATUSES)}. "
            "A synonym would silently read as active and never rotate; note that "
            "a failed acceptance CHECK keeps status 'running' with 'fails' "
            "incremented — 'fail' means finally given up.",
        )
    value = _encode_value(entry)
    if len(value) > MAX_VALUE_CHARS:
        return _error(
            "value_too_long",
            f"encoded entry is {len(value)} chars; the ledger truncates past "
            f"{MAX_VALUE_CHARS}, which would corrupt the JSON. Shrink the "
            "acceptance spec (it is a machine condition, not prose).",
        )
    return {"ok": True, "value": value, "chars": len(value)}


def decode_value(value: Any) -> Dict[str, Any]:
    """Decode one stored artifacts value; structured error on malformed input."""
    if not isinstance(value, str):
        return _error(
            "value_not_string",
            f"stored value must be a string, got {type(value).__name__} - the "
            "ledger rejects non-string values (artifacts_not_string_map), so "
            "this entry was never persisted",
        )
    try:
        entry = json.loads(value)
    except json.JSONDecodeError as exc:
        return _error("not_json", f"stored value is not JSON: {exc}")
    except RecursionError:
        # Deeply-nested JSON within the 2000-char bound can exceed the parser's
        # recursion limit; a damaged entry must come back as a structured error
        # ("re-derive the item"), never a traceback.
        return _error("not_json", "stored value is JSON nested too deeply to parse")
    if not isinstance(entry, dict):
        return _error(
            "not_an_object", f"stored value must decode to an object, got {type(entry).__name__}"
        )
    for name in _FIELD_TYPES:
        if name in entry:
            problem = _check_field(name, entry[name])
            if problem:
                return _error("bad_field_type", problem)
    if "status" not in entry:
        return _error("missing_field", "entry has no 'status'")
    terminal = entry["status"] in TERMINAL_STATUSES
    complete = isinstance(entry.get("accept"), dict) and isinstance(entry.get("session"), str)
    return {"ok": True, "entry": entry, "terminal": terminal, "complete": complete}


def mode_decode(payload: Dict[str, Any]) -> Dict[str, Any]:
    if "value" not in payload:
        return _error("missing_field", "decode needs 'value'")
    return decode_value(payload["value"])


def validate_artifacts(artifacts: Dict[str, Any]) -> List[Dict[str, str]]:
    """Every bound the ledger would silently enforce, reported instead."""
    violations: List[Dict[str, str]] = []
    for key, value in artifacts.items():
        if len(key) > MAX_KEY_CHARS:
            violations.append(
                {
                    "key": key,
                    "code": "key_too_long",
                    "detail": f"key is {len(key)} chars; the ledger truncates keys past "
                    f"{MAX_KEY_CHARS}, which can silently collide two items",
                }
            )
        if not isinstance(value, str):
            violations.append(
                {
                    "key": key,
                    "code": "value_not_string",
                    "detail": f"value is {type(value).__name__}, not a string; the ledger "
                    "rejects the whole write with artifacts_not_string_map and "
                    "nothing persists",
                }
            )
        elif len(value) > MAX_VALUE_CHARS:
            violations.append(
                {
                    "key": key,
                    "code": "value_too_long",
                    "detail": f"value is {len(value)} chars; the ledger truncates past "
                    f"{MAX_VALUE_CHARS}, corrupting the stored JSON",
                }
            )
    if len(artifacts) > MAX_ENTRIES:
        violations.append(
            {
                "key": "",
                "code": "too_many_entries",
                "detail": f"{len(artifacts)} entries; the ledger keeps only the newest "
                f"{MAX_ENTRIES} and silently ages out the oldest - rotate first",
            }
        )
    return violations


def mode_validate(payload: Dict[str, Any]) -> Dict[str, Any]:
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict):
        return _error("bad_field_type", "validate needs 'artifacts' as an object")
    violations = validate_artifacts(artifacts)
    return {"ok": not violations, "violations": violations}


def _collapse(entry: Dict[str, Any]) -> Tuple[str, bool]:
    """Terminal entry -> its one-line outcome. Returns (value, changed)."""
    kept = {k: entry[k] for k in ("round", "status") if k in entry}
    collapsed = _encode_value(kept)
    return collapsed, kept != entry


def mode_rotate(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Collapse/drop terminal entries; the result must be written back WHOLE.

    Two trigger points, and the second is load-bearing: on a terminal verdict,
    and BEFORE any dispatch write that would push the map past the cap — run
    on the COMBINED current-plus-new map, because rotate trims down TO the cap
    (never below), so a map already at the cap plus a new entry would
    otherwise be capped by the ledger itself, whose age-out is blind to status
    and evicts the oldest entry even when it is an active item's only
    surviving state. A cap_exceeded_all_active error at dispatch time means
    there is no capacity: do not dispatch.

    The ledger MERGES artifacts rather than replacing them: a key omitted from
    a ``session_ledger_record`` call is never removed, and every key present is
    re-inserted as newest. So writing back only the changed entries would move
    the collapsed TERMINAL entries to the newest positions and leave the
    untouched ACTIVE entries oldest — inverting the age-out order rotation
    exists to protect, and making ``dropped`` a no-op. Always write the entire
    returned ``artifacts`` map in one call.
    """
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict):
        return _error("bad_field_type", "rotate needs 'artifacts' as an object")
    rotated: Dict[str, Any] = {}
    terminal_keys: List[str] = []
    collapsed: List[str] = []
    # Pass 1: collapse every terminal entry to its outcome. Insertion order is
    # preserved throughout - it is the ledger's own age-out order, so "oldest"
    # here means the same entry the ledger itself would age out first.
    for key, value in artifacts.items():
        decoded = decode_value(value)
        if decoded["ok"] and decoded["terminal"]:
            terminal_keys.append(key)
            new_value, changed = _collapse(decoded["entry"])
            rotated[key] = new_value
            if changed:
                collapsed.append(key)
        else:
            # Active, opaque, or malformed: rotation must never destroy what it
            # cannot prove is finished.
            rotated[key] = value
    # Pass 2: only when over the cap, drop terminal entries oldest-first.
    dropped: List[str] = []
    if len(rotated) > MAX_ENTRIES:
        for key in terminal_keys:
            if len(rotated) <= MAX_ENTRIES:
                break
            del rotated[key]
            dropped.append(key)
    if len(rotated) > MAX_ENTRIES:
        return _error(
            "cap_exceeded_all_active",
            f"{len(rotated)} entries remain after dropping every terminal one; "
            f"the ledger caps at {MAX_ENTRIES} and an active item must never be "
            "dropped. Finish or stop items before dispatching more.",
        )
    return {"ok": True, "artifacts": rotated, "collapsed": collapsed, "dropped": dropped}


_MODES = {
    "encode": mode_encode,
    "decode": mode_decode,
    "validate": mode_validate,
    "rotate": mode_rotate,
}


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in _MODES:
        # stdout, like the malformed-stdin error below and accept_eval.py: the
        # conductor reads one stream, and a wrong mode name is the likeliest
        # invocation error, so its cause must be where the caller looks.
        print(
            json.dumps(
                {"error": f"usage: ledger_entry.py {{{'|'.join(sorted(_MODES))}}} < input.json"}
            )
        )
        return 2
    try:
        payload = json.load(sys.stdin)
    except Exception:
        print(json.dumps({"error": "stdin must be a JSON object"}))
        return 2
    if not isinstance(payload, dict):
        # An explicit check, not an assert: asserts vanish under `python -O`,
        # and a JSON array on stdin would then crash inside the mode handler
        # instead of returning the documented structured exit-2.
        print(json.dumps({"error": "stdin must be a JSON object"}))
        return 2
    print(json.dumps(_MODES[sys.argv[1]](payload), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
