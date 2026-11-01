#!/usr/bin/env python3
"""Incremental architecture size guard, not a code-line minimizer.

Reports Python hot spots and compares them with explicit ratchet budgets. A
refactor may reduce a budget; raising one should be an intentional, reviewed
architecture decision. This guard cannot prove runtime semantic equivalence.
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUDGET = ROOT / "config" / "architecture_budget.json"


def measure() -> dict[str, object]:
    functions: list[dict[str, object]] = []
    file_lines: dict[str, int] = {}
    for base in ("src/kiro_byo", "backend", "scripts"):
        for path in sorted((ROOT / base).rglob("*.py")):
            if any(part in {"__pycache__", "build", "dist"} for part in path.parts):
                continue
            relative = path.relative_to(ROOT).as_posix()
            source = path.read_text(encoding="utf-8")
            file_lines[relative] = len(source.splitlines())
            tree = ast.parse(source, filename=relative)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # Same counting semantics as the audited preflight baseline.
                    functions.append({
                        "file": relative,
                        "name": node.name,
                        "lines": node.end_lineno - node.lineno + 1,
                    })
    functions.sort(key=lambda row: (-int(row["lines"]), str(row["file"]), str(row["name"])))
    return {
        "python_files": len(file_lines),
        "python_functions": len(functions),
        "functions_over_100_lines": sum(int(row["lines"]) > 100 for row in functions),
        "functions_over_200_lines": sum(int(row["lines"]) > 200 for row in functions),
        "file_lines": file_lines,
        "function_lines": {
            f"{row['file']}::{row['name']}": row['lines'] for row in functions
        },
        "longest_functions": functions[:10],
    }


def violations(metrics: dict[str, object], budget: dict[str, object]) -> list[str]:
    failures: list[str] = []
    for name in ("functions_over_100_lines", "functions_over_200_lines"):
        actual, cap = int(metrics[name]), int(budget[name])
        if actual > cap:
            failures.append(f"{name}: {actual} > budget {cap}")
    lines = metrics["file_lines"]
    assert isinstance(lines, dict)
    for path, cap in budget["hotspot_file_lines"].items():
        if path not in lines:
            failures.append(f"hotspot disappeared or moved without reviewed budget update: {path}")
        elif int(lines[path]) > int(cap):
            failures.append(f"{path}: {lines[path]} lines > budget {cap}")
    function_lines = metrics["function_lines"]
    assert isinstance(function_lines, dict)
    for name, cap in budget.get("hotspot_function_lines", {}).items():
        if name not in function_lines:
            failures.append(f"tracked function disappeared or was renamed without review: {name}")
        elif int(function_lines[name]) > int(cap):
            failures.append(f"{name}: {function_lines[name]} lines > budget {cap}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail on architecture budget regression")
    args = parser.parse_args()
    metrics = measure()
    for field in ("python_files", "python_functions", "functions_over_100_lines", "functions_over_200_lines"):
        print(f"{field}: {metrics[field]}")
    print("longest Python functions:")
    for row in metrics["longest_functions"]:
        print(f"  {row['lines']:>4} {row['file']}::{row['name']}")
    if args.check:
        budget = json.loads(BUDGET.read_text(encoding="utf-8"))
        failures = violations(metrics, budget)
        if failures:
            for failure in failures:
                print(f"FAIL: {failure}", file=sys.stderr)
            return 1
        print("architecture budget: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
