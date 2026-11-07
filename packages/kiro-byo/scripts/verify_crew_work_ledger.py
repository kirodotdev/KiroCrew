#!/usr/bin/env python3
"""Read-only KiroCrew Work Ledger source-surface preflight for BYO Phase 5.

This does NOT certify runtime wiring or prove a gateway is installed. It only
checks that the official Work Ledger interface this proposal expects exists
in the exact local source tree. Unsupported layouts fail with actionable
messages; installation and writes are intentionally out of scope.
"""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

REQUIRED = {
    "work_ledger_store.py": ("read_conductor", "read_work_item"),
    "work_ledger_writes.py": ("read_binding", "apply_conductor_action", "apply_worker_report"),
    "work_ledger_records.py": ("ConductorRecord", "WorkItem"),
    "mcp_work.py": (),
}


def inspect(crew_package: Path) -> dict:
    failures: list[str] = []
    for name, definitions in REQUIRED.items():
        path = crew_package / name
        try:
            text = path.read_text(encoding="utf-8-sig")
            tree = ast.parse(text, filename=str(path))
        except (OSError, SyntaxError, UnicodeError) as exc:
            failures.append(f"{name}: cannot inspect: {type(exc).__name__}")
            continue
        present = {
            n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        for symbol in definitions:
            if symbol not in present:
                failures.append(f"{name}: missing {symbol}")
    try:
        mcp = (crew_package / "mcp_work.py").read_text(encoding="utf-8-sig")
        if not all(t in mcp for t in ("WORKER_TOOLS", "CONDUCTOR_TOOLS", "work_brief", "work_report")):
            failures.append("mcp_work.py: authoritative worker/conductor tools changed")
    except (OSError, UnicodeError) as exc:
        failures.append(f"mcp_work.py: cannot inspect: {type(exc).__name__}")
    # A Work Ledger alone is not proof that the Mate Worker is launched through
    # the expected Crew source-defined role or that the BYO backend is selected.
    mate = crew_package / "mate_worker.py"
    try:
        source = mate.read_text(encoding="utf-8-sig")
        if not all(n in source for n in ("MATE_WORKER_AGENT", "session_create", "session_send")):
            failures.append("mate_worker.py: Mate 2.0 dispatch markers changed")
    except (OSError, UnicodeError) as exc:
        failures.append(f"mate_worker.py: cannot inspect: {type(exc).__name__}")
    return {
        "work_ledger_source_surface_checked": not failures,
        "source_files_checked": len(REQUIRED) + 1,
        "crew_host_patch_installed": False,
        "crew_host_byo_work_link_emission_verified": False,
        "windows_gateway_e2e_verified": False,
        "issues": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--crew-package", type=Path, required=True,
                        help="installed Crew package folder, e.g. .../src/kiro_crew")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = inspect(args.crew_package)
    result = json.dumps(report, ensure_ascii=False, indent=2)
    print(result)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(result + "\n", encoding="utf-8")
    return 0 if report["work_ledger_source_surface_checked"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
