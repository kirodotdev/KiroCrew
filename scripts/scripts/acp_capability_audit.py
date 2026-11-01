#!/usr/bin/env python3
"""Audit the *advertised* ACP surface against the actual server dispatcher.

This check is deliberately read-only.  Internal Crew integration extensions
must not be published to arbitrary ACP clients simply because handlers exist.
It is a static compatibility guard, not a claim of Windows/Crew E2E parity.
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _dispatch_names(server_file: Path) -> set[str]:
    tree = ast.parse(server_file.read_text(encoding="utf-8"), filename=str(server_file))
    found: set[str] = set()
    for cls in (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AcpServer"):
        for fn in cls.body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or fn.name != "handle":
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
                    continue
                if not any(isinstance(t, ast.Name) and t.id == "handlers" for t in node.targets):
                    continue
                found.update(k.value for k in node.value.keys if isinstance(k, ast.Constant) and isinstance(k.value, str))
    return found


def audit_contract(root: Path = ROOT) -> tuple[dict[str, Any], list[str]]:
    src = str(root / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from kiro_byo.protocol import initialize_result, runtime_capability_report

    advertised = initialize_result()["agentCapabilities"]
    declared_extensions = advertised.get("_meta", {}).get("kiro", {}).get("extensionMethods", [])
    runtime = runtime_capability_report(native_tools=[], mcp_transports=[])
    dispatcher = _dispatch_names(root / "src/kiro_byo/acp_server.py")
    errors: list[str] = []
    if not isinstance(declared_extensions, list) or any(not isinstance(x, str) for x in declared_extensions):
        errors.append("extensionMethods must be a list of strings")
        declared_extensions = []
    if len(declared_extensions) != len(set(declared_extensions)):
        errors.append("duplicate extensionMethods")
    for method in declared_extensions:
        if method not in dispatcher:
            errors.append(f"advertised extension missing an ACP handler: {method}")
        if method.startswith("_kiro.dev/"):
            errors.append(f"retired CLI v2 extension must not be advertised: {method}")
    # These methods bypass the dict dispatcher by design. session/cancel is an
    # ACP notification handled in _handle_notification, not an RPC request.
    special = {"session/prompt", "session/load", "session/resume", "session/cancel"}
    standard = set(runtime.get("standard_methods", []))
    if not standard - special <= dispatcher:
        errors.append(f"reported standard methods missing handlers: {sorted((standard-special)-dispatcher)}")
    if not {"initialize", "session/new", "session/prompt", "session/cancel"} <= standard:
        errors.append("runtime report omitted a required ACP lifecycle method")
    session_methods = set(advertised.get("sessionCapabilities", {}))
    for cap in session_methods - {"additionalDirectories"}:
        if f"session/{cap}" not in dispatcher | special:
            errors.append(f"advertised session capability has no handler: {cap}")
    if "session/fork" in dispatcher | standard | set(declared_extensions):
        errors.append("fork is not supported and must not be reported as implemented")
    if set(runtime.get("kiro_extensions", [])) != set(declared_extensions):
        errors.append("runtime report extensions disagree with initialize")
    # These are Crew integration/legacy handlers, intentionally *not*
    # advertised to generic V3 ACP clients; retain their wire contracts.
    private = sorted(x for x in dispatcher if x.startswith("_") and x not in declared_extensions)
    report = {
        "protocol_version": initialize_result()["protocolVersion"],
        "advertised_extensions": list(declared_extensions),
        "standard_methods": sorted(standard),
        "unadvertised_crew_and_legacy_extensions": private,
        "unadvertised_extension_count": len(private),
        "fork_supported": False,
        "mcp_interactive_input_supported": False,
        "windows_e2e_verified": False,
    }
    return report, errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    opts = parser.parse_args()
    report, errors = audit_contract()
    if opts.json:
        print(json.dumps({"report": report, "errors": errors}, indent=2, ensure_ascii=False))
    else:
        print(f"ACP contract: {len(report['standard_methods'])} standard methods; "
              f"{len(report['advertised_extensions'])} public Kiro extensions; "
              f"{report['unadvertised_extension_count']} unadvertised Crew/legacy extensions")
        for error in errors:
            print("FAIL:", error, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
