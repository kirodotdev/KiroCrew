#!/usr/bin/env python3
"""check_metrics_schema_history.py -- the exported metric schema only grows.

``src/kiro_crew/metrics/schema_manifest.json`` lists every metric a product
destination may receive: its name, kind, unit, scope, temporality, attribute
keys and closed value sets. A downstream reader builds queries on those, so a
row, key or value must never disappear or change meaning. A rename is a removal.

``test/metrics/test_metrics_schema.py`` already keeps the manifest equal to the
code. That test cannot see history: a change that drops a metric from the code
AND regenerates the manifest passes it. This gate is the history half. It reads
the manifest at the base commit with ``git show`` and fails when the manifest
at head takes anything away from it. The rule is ``is_additive`` in
``src/kiro_crew/metrics/schema_manifest.py``, loaded by path (stdlib only, no
package install needed).

The rule is loaded from the BASE commit when the base has it, so one change
cannot weaken the rule and drop a row at the same time. ``--test`` runs the head
copy of the rule over fixed probes first, so a weakened rule fails there.

Usage:

    METRICS_SCHEMA_BASE="$(git merge-base HEAD origin/main)" \\
        python3 scripts/check_metrics_schema_history.py

    python3 scripts/check_metrics_schema_history.py --test

With no ``METRICS_SCHEMA_BASE``, or a base that has no manifest yet, there is
nothing to compare and the gate exits 0 with a note. Pass the merge base, not
the base branch's moving tip.

There is no override. A change that must break the contract adds a new metric
name and marks the old row ``deprecated``.

Exit: 0 pass | 1 something was taken away, or a self-test probe failed | 2 git error
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "src/kiro_crew/metrics/schema_manifest.json"
RULE = "src/kiro_crew/metrics/schema_manifest.py"


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _at(ref: str, path: str) -> Optional[str]:
    """The file's text at ``ref``, or None when the path is not in that tree."""
    if not _git("ls-tree", "--name-only", ref, "--", path).stdout.strip():
        return None
    shown = _git("show", f"{ref}:{path}")
    if shown.returncode != 0:
        raise RuntimeError(f"git show {ref}:{path} failed: {shown.stderr.strip()}")
    return shown.stdout


def _load_rule(source: str, label: str) -> Callable[[Dict[str, Any], Dict[str, Any]], List[str]]:
    """Import ``schema_manifest.py`` source as a throwaway module; return its ``is_additive``."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"schema_manifest_{label}.py"
        path.write_text(source, encoding="utf-8")
        spec = importlib.util.spec_from_file_location(f"_kc_metrics_schema_{label}", path)
        if spec is None or spec.loader is None:  # pragma: no cover - unreachable in-tree
            raise RuntimeError(f"cannot load {RULE} ({label})")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module.is_additive  # type: ignore[attr-defined, no-any-return]


def _head_rule() -> Callable[[Dict[str, Any], Dict[str, Any]], List[str]]:
    return _load_rule((ROOT / RULE).read_text(encoding="utf-8"), "head")


# -- self-test -----------------------------------------------------------------

_PROBE = {
    "metrics": [
        {
            "name": "probe.done",
            "kind": "counter",
            "unit": "1",
            "scope": "kiro_crew",
            "temporality": "delta",
            "status": "active",
            "attributes": {"outcome": {"values": ["error", "ok"]}, "agent": {}},
        }
    ]
}


def _edit(change: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    doc = copy.deepcopy(_PROBE)
    change(doc["metrics"][0])
    return doc


#: (label, edit on the new copy, should it pass?)
_PROBES = [
    ("same document", lambda r: None, True),
    ("new key", lambda r: r["attributes"].update(surface={}), True),
    ("new value", lambda r: r["attributes"]["outcome"]["values"].append("timeout"), True),
    ("deprecated row", lambda r: r.update(status="deprecated"), True),
    ("renamed row", lambda r: r.update(name="probe.finished"), False),
    ("changed unit", lambda r: r.update(unit="ms"), False),
    ("changed scope", lambda r: r.update(scope="kiro.crew"), False),
    ("removed key", lambda r: r["attributes"].pop("agent"), False),
    ("removed value", lambda r: r["attributes"]["outcome"].update(values=["ok"]), False),
]


def _self_test() -> int:
    rule = _head_rule()
    bad = []
    for label, change, passes in _PROBES:
        problems = rule(copy.deepcopy(_PROBE), _edit(change))
        if passes == bool(problems):
            bad.append(f"{label}: expected {'pass' if passes else 'fail'}, got {problems}")
    removed = rule(copy.deepcopy(_PROBE), {"metrics": []})
    if not removed or "probe.done" not in removed[0]:
        bad.append(f"removed row: expected a problem naming probe.done, got {removed}")
    for line in bad:
        print(f"metrics schema self-test FAILED: {line}")
    if not bad:
        print(f"metrics schema self-test: {len(_PROBES) + 1} probes ok")
    return 1 if bad else 0


# -- the gate ------------------------------------------------------------------


def check(base: str) -> int:
    try:
        old_text = _at(base, MANIFEST)
        if old_text is None:
            print(f"{MANIFEST} does not exist at {base}; nothing to compare yet.")
            return 0
        base_rule = _at(base, RULE)
    except RuntimeError as exc:
        print(f"check_metrics_schema_history: {exc}", file=sys.stderr)
        return 2
    rule = _load_rule(base_rule, "base") if base_rule is not None else _head_rule()
    head_path = ROOT / MANIFEST
    if not head_path.is_file():
        print(f"::error::{MANIFEST} was deleted; the exported metric schema is append-only.")
        return 1
    old = json.loads(old_text)
    new = json.loads(head_path.read_text(encoding="utf-8"))
    problems = rule(old, new)
    if not problems:
        added = len(new.get("metrics", [])) - len(old.get("metrics", []))
        print(f"Metric schema only grows against {base} ({added:+d} rows).")
        return 0
    in_ci = os.environ.get("GITHUB_ACTIONS") == "true"
    for problem in problems:
        print(f"::error::Metric schema: {problem}" if in_ci else f"METRIC SCHEMA: {problem}")
    print(
        "The exported metric schema is append-only (docs/guides/telemetry-otlp-export.md,"
        " 'Metric schema contract'). Add a new metric name and mark the old row deprecated."
    )
    return 1


def main(argv: List[str]) -> int:
    if "--test" in argv:
        return _self_test()
    base = os.environ.get("METRICS_SCHEMA_BASE", "").strip()
    if not base:
        print("METRICS_SCHEMA_BASE is not set; nothing to compare against.")
        return 0
    if _git("rev-parse", "--verify", "--quiet", f"{base}^{{commit}}").returncode != 0:
        print(f"check_metrics_schema_history: base {base!r} is not a commit here", file=sys.stderr)
        return 2
    return check(base)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
