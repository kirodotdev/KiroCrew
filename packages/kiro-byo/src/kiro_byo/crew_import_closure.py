"""Import-closure gate for BYO-generated Crew source (no module execution).

This prevents an installed Gateway boot failure caused by a BYO-owned import
referring to a symbol the target module does not export. It is checked both
before the installer writes *any* Crew file and after all writes commit.

This is not a substitute for a Gateway run or Worker/MCP permission testing.
"""
from __future__ import annotations

import ast
from collections.abc import Mapping

IDENTITY = "ACP_BACKEND_KIRO_BYO"
OWNER = "src/kiro_crew/agent_sdk/backends.py"
TYPES = "src/kiro_crew/acp/types.py"
FACADE = "kiro_crew.acp_backends"
REGISTRY = "src/kiro_crew/providers/mirrors/registry.py"
MEMBER_LOADER = "src/kiro_crew/config/loader_builders.py"
SLOT_EFFORT = "src/kiro_crew/dashboard/chat_api/slot_effort.py"


def _direct_local_imports(node: ast.AsyncFunctionDef) -> dict[str, str]:
    """Return function-scope import owners; conditional imports are insufficient."""
    imports: dict[str, str] = {}
    for stmt in node.body:
        if isinstance(stmt, ast.ImportFrom) and stmt.level == 0:
            for alias in stmt.names:
                if alias.asname is None:
                    imports[alias.name] = stmt.module or ""
    return imports


def _slot_facade_binding_issues(tree: ast.Module) -> list[str]:
    """Audit endpoint locals after Crew's owner→chat_handlers composition.

    Module-level imports in slot_effort are NOT visible after the upstream
    facade rebinds every owner function onto chat_handlers globals. Without
    explicit local imports the selection-capabilities GET can fail with a
    NameError, even though the Python module imported and the install passed.
    """
    issues: list[str] = []
    endpoints = {
        "api_chat_slot_selection_capabilities": (
            "ACP_BACKEND_KIRO_BYO", "_kiro_byo_cold_selection_capabilities",
            "_kiro_byo_type_selection_active", "_kiro_byo_reconcile_slot_routing",
        ),
        "api_chat_slot_reasoning_effort": (
            "ACP_BACKEND_KIRO_BYO", "_kiro_byo_type_selection_active",
        ),
        "_kiro_byo_cold_selection_capabilities": ("ACP_BACKEND_KIRO_BYO",),
        "_kiro_byo_reconcile_slot_routing": ("ACP_BACKEND_KIRO_BYO",),
    }
    for name, required in endpoints.items():
        funcs = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                 and node.name == name]
        if len(funcs) != 1:
            issues.append(f"{SLOT_EFFORT}: composed BYO function {name} missing or ambiguous")
            continue
        bound = _direct_local_imports(funcs[0])
        for symbol in required:
            owner = ("kiro_crew.agent_sdk.backends" if symbol == IDENTITY
                     else "kiro_crew.dashboard.chat_api.slot_effort")
            if bound.get(symbol) != owner:
                issues.append(
                    f"{SLOT_EFFORT}: {name} must locally import {symbol} from {owner} "
                    "(Crew facade rebinding)"
                )
    return issues


def _bound_at_module_scope(tree: ast.Module, name: str) -> bool:
    return any(
        (isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets))
        or (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
            and node.target.id == name)
        or (isinstance(node, ast.ImportFrom) and any(
            alias.name == name and alias.asname is None for alias in node.names))
        for node in tree.body
    )


def byo_import_closure_issues(sources: Mapping[str, str]) -> list[str]:
    """Audit the *complete generated-source set*, including nested imports.

    A declaration in a comment, method body or `TYPE_CHECKING` does not count
    as a module export. Unknown aliasing/import-owner layouts fail closed.
    """
    parsed: dict[str, ast.Module] = {}
    issues: list[str] = []
    for path, source in sorted(sources.items()):
        if not path.endswith(".py"):
            continue
        try:
            parsed[path] = ast.parse(source, filename=path)
        except SyntaxError as exc:
            issues.append(f"{path}: invalid generated Python ({exc.msg})")
    if OWNER not in parsed or not _bound_at_module_scope(parsed[OWNER], IDENTITY):
        issues.append(f"{OWNER}: canonical {IDENTITY} declaration missing")
    if TYPES not in parsed or not _bound_at_module_scope(parsed[TYPES], IDENTITY):
        issues.append(f"{TYPES}: canonical {IDENTITY} re-export missing")

    imports: dict[str, list[ast.ImportFrom]] = {}
    allowed = {"kiro_crew.agent_sdk.backends", "kiro_crew.acp.types"}
    for path, tree in sorted(parsed.items()):
        imports[path] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not any(
                alias.name == IDENTITY for alias in node.names
            ):
                continue
            imports[path].append(node)
            if node.level or node.module not in allowed:
                issues.append(
                    f"{path}:{node.lineno}: {IDENTITY} imported from "
                    f"{node.module or '<relative>'}, not its verified owner"
                )
            if any(alias.name == IDENTITY and alias.asname for alias in node.names):
                issues.append(f"{path}:{node.lineno}: aliased BYO backend identity")

    # Crew 0.12 composes slot_effort functions into chat_handlers' globals.
    # A normal module import check misses this cross-module runtime NameError.
    if SLOT_EFFORT in parsed:
        issues.extend(_slot_facade_binding_issues(parsed[SLOT_EFFORT]))

    # These two files caused actual failures after their old facade re-export
    # stopped being patched. A binding must exist in the correct lexical scope.
    for path in (REGISTRY, MEMBER_LOADER):
        tree = parsed.get(path)
        if tree is None:
            issues.append(f"{path}: required generated Crew seam missing")
            continue
        if path == REGISTRY:
            direct = [n for n in tree.body if isinstance(n, ast.ImportFrom)]
        else:
            funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                     and n.name == "_read_backend_choice"]
            direct = [n for n in funcs[0].body if isinstance(n, ast.ImportFrom)] if len(funcs) == 1 else []
        bindings = [n for n in direct if n.module == "kiro_crew.agent_sdk.backends"
                    and n.level == 0 and any(
                        a.name == IDENTITY and a.asname is None for a in n.names)]
        if len(bindings) != 1:
            issues.append(f"{path}: BYO identity must have one canonical in-scope import")
    return issues
