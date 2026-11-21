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
    """Read the registered method table from source without executing it.

    R3 moved the immutable dispatch mapping out of AcpServer. Both the server
    reference and the declared handlers are verified so this audit cannot
    silently accept a table that is no longer used or a non-existent handler.
    """
    tree = ast.parse(server_file.read_text(encoding="utf-8"), filename=str(server_file))
    servers = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "AcpServer"]
    if len(servers) != 1:
        return set()
    server = servers[0]
    handlers = {n.name for n in server.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    # Explicitly-owned ACP control mixins are part of the executable server
    # surface. Audit their methods from source, not an unbounded inheritance
    # walk: a future unrelated mixin must not silently advertise RPC methods.
    mixins = {
        "SessionLifecycleMixin": "session_lifecycle",
        "RunRecoveryMixin": "run_recovery",
    }
    bases = {base.id for base in server.bases if isinstance(base, ast.Name)}
    imports = {
        (node.module, alias.name)
        for node in tree.body if isinstance(node, ast.ImportFrom)
        for alias in node.names if node.level == 1
    }
    for class_name, module_name in mixins.items():
        if class_name not in bases or (module_name, class_name) not in imports:
            return set()
        path = server_file.with_name(module_name + ".py")
        if not path.is_file():
            return set()
        mixin_tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        matches = [node for node in mixin_tree.body
                   if isinstance(node, ast.ClassDef) and node.name == class_name]
        if len(matches) != 1:
            return set()
        handlers.update(node.name for node in matches[0].body
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)))
        mixin_imports = {
            alias.asname or alias.name
            for node in mixin_tree.body if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        for statement in matches[0].body:
            if isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Name):
                if statement.value.id in mixin_imports:
                    handlers.update(target.id for target in statement.targets
                                    if isinstance(target, ast.Name))
    # A few small, explicitly imported request handlers are mounted onto the
    # class as callable aliases. Audit those static aliases too.
    imported = {
        alias.asname or alias.name
        for node in tree.body if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    for statement in server.body:
        if isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Name):
            if statement.value.id in imported:
                handlers.update(
                    target.id for target in statement.targets if isinstance(target, ast.Name)
                )
    handle = next((n for n in server.body if isinstance(n, ast.FunctionDef) and n.name == "handle"), None)
    if handle is None or not any(
        isinstance(n, ast.Name) and n.id == "ACP_REQUEST_HANDLERS" for n in ast.walk(handle)
    ):
        return set()
    registry = server_file.with_name("acp_dispatch.py")
    if not registry.exists():
        return set()
    dispatch_tree = ast.parse(registry.read_text(encoding="utf-8"), filename=str(registry))
    for statement in dispatch_tree.body:
        if not isinstance(statement, ast.AnnAssign):
            continue
        if not isinstance(statement.target, ast.Name) or statement.target.id != "ACP_REQUEST_HANDLERS":
            continue
        value = statement.value
        if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Name) or value.func.id != "MappingProxyType":
            continue
        if len(value.args) != 1 or not isinstance(value.args[0], ast.Dict):
            continue
        mapping = value.args[0]
        if any(
            not isinstance(k, ast.Constant) or not isinstance(k.value, str)
            or not isinstance(v, ast.Constant) or not isinstance(v.value, str)
            or v.value not in handlers
            for k, v in zip(mapping.keys, mapping.values)
        ):
            return set()
        methods = [k.value for k in mapping.keys]
        if len(methods) != len(set(methods)):
            return set()
        return set(methods)
    return set()


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
