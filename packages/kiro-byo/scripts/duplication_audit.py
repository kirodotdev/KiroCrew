#!/usr/bin/env python3
"""Read-only core size and exact duplicate-function inventory (not savings claims).

Usage: python scripts/duplication_audit.py [--json]
Identical-looking snippets are only candidates: vendor wire and host security
semantics must be reviewed before any refactor.
"""
from __future__ import annotations

import argparse
import ast
import collections
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src" / "kiro_byo"


def audit() -> dict:
    sizes = {}
    groups = collections.Counter()
    funcs = collections.defaultdict(list)
    functions_over_100 = 0
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        lines = path.read_text(encoding="utf-8").splitlines()
        sizes[rel] = len(lines)
        if rel.startswith("providers/"):
            group = "providers"
        elif path.name.startswith(("crew", "integration")):
            group = "crew_integration"
        else:
            group = "other_core"
        groups[group] += len(lines)
        for node in ast.walk(ast.parse("\n".join(lines) + "\n")):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            length = node.end_lineno - node.lineno + 1
            if length > 100:
                functions_over_100 += 1
            if length < 7:
                continue
            # A same-body match is evidence of repeated *syntax*, not evidence
            # that two functions can safely share authority or wire semantics.
            body = ast.dump(ast.Module(body=node.body, type_ignores=[]), include_attributes=False)
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
            funcs[digest].append({"file": rel, "function": node.name, "lines": length})
    exact = [entries for entries in funcs.values() if len({x["file"] for x in entries}) >= 2]
    return {
        "scope": "src/kiro_byo/**/*.py only; excludes tests, scripts, assets",
        "python_files": len(sizes), "python_lines": sum(sizes.values()),
        "group_lines": dict(sorted(groups.items())),
        "functions_over_100_lines": functions_over_100,
        "top_files": [{"file": k, "lines": v} for k, v in sorted(sizes.items(), key=lambda item: -item[1])[:20]],
        "exact_cross_file_function_bodies": exact,
        "warning": "Exact duplication is a conservative signal; near-duplicates and safe deletion budgets require manual semantic review.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = audit()
    print(json.dumps(report, indent=2, ensure_ascii=False) if args.json else (
        f"{report['python_files']} core Python files / {report['python_lines']} lines; "
        f"{report['functions_over_100_lines']} functions over 100 lines; "
        f"{len(report['exact_cross_file_function_bodies'])} exact cross-file body groups"
    ))
