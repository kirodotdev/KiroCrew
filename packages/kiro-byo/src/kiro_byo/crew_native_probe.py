"""Read-only, fail-closed evidence for KiroCrew third-party ACP registration.

This is deliberately not an installer.  A source declaration is weaker than a
live, permissions-verified Harness selection; no source scan can grant either.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

_BACKENDS = Path("agent_sdk/backends.py")
_ROUTING = Path("agent_sdk/backends_routing.py")
_MAX_BYTES = 2 * 1024 * 1024
_BYO_ID = "kiro-byo"


def _source(path: Path) -> str | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_BYTES:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def _declared_names(tree: ast.AST) -> set[str]:
    return {n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}


def _assignments(tree: ast.Module) -> dict[str, ast.AST]:
    """Only module-level declarations count; comments and dead strings do not."""
    result: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    result[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            result[node.target.id] = node.value
    return result


def _symbol_value(node: ast.AST | None, declarations: dict[str, ast.AST]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        assigned = declarations.get(node.id)
        if isinstance(assigned, ast.Constant) and isinstance(assigned.value, str):
            return assigned.value
    return None


def _elements(node: ast.AST | None) -> list[ast.AST] | None:
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return list(node.elts)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in {"frozenset", "set", "tuple", "list"}
            and len(node.args) == 1 and not node.keywords):
        return _elements(node.args[0])
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        a, b = _elements(node.left), _elements(node.right)
        return a + b if a is not None and b is not None else None
    return None


def _known_member(assignments: dict[str, ast.AST]) -> bool:
    return any(_symbol_value(v, assignments) == _BYO_ID
               for v in (_elements(assignments.get("ACP_BACKENDS_KNOWN")) or []))


def _routed_member(assignments: dict[str, ast.AST]) -> bool:
    node = assignments.get("ACP_BACKEND_ROUTING")
    return isinstance(node, ast.Dict) and any(
        _symbol_value(k, assignments) == _BYO_ID for k in node.keys
    )


def inspect_native_sources(backends: str | None, routing: str | None) -> dict[str, Any]:
    """Return factual source signals, never a claim of Crew E2E compatibility."""
    base = {"native_integration_verified": False}
    if backends is None:
        return {**base, "status": "source_unavailable", "evidence": [],
                "next_step": "inspect a complete installed Crew source tree"}
    try:
        tree = ast.parse(backends, filename=str(_BACKENDS))
        route_tree = ast.parse(routing, filename=str(_ROUTING)) if routing is not None else None
    except SyntaxError:
        return {**base, "status": "source_invalid", "evidence": [],
                "next_step": "do not register or patch an unknown backend source"}

    assignments = _assignments(tree)
    route_assignments = _assignments(route_tree) if route_tree is not None else {}
    has_register = "register_selectable_backend" in _declared_names(tree)
    known_gate = "ACP_BACKENDS_KNOWN" in assignments
    byo_declared = _symbol_value(assignments.get("ACP_BACKEND_KIRO_BYO"), assignments) == _BYO_ID
    known_member = _known_member(assignments)
    routing_source = "ACP_BACKEND_ROUTING" in route_assignments
    routing_member = _routed_member({**assignments, **route_assignments})

    blockers = []
    if not has_register:
        blockers.append("registration API not established in inspected owner")
    if not known_gate or not known_member:
        blockers.append("BYO is not a proven member of ACP_BACKENDS_KNOWN")
    if not routing_member:
        blockers.append("BYO has no proven permission-routing map entry")

    evidence = []
    if has_register:
        evidence.append("register_selectable_backend function declared")
    if known_gate:
        evidence.append("ACP_BACKENDS_KNOWN declared")
    if known_member:
        evidence.append("BYO literal resolved as a known backend member")
    if routing_member:
        evidence.append("BYO literal resolved as a permission-routing map key")

    # Membership is deliberately necessary, NOT sufficient. Even this strong
    # syntactic candidate needs end-to-end host permission and worker tests.
    if has_register and known_member and routing_member:
        status = "core_registration_candidate"
        next_step = "run real Crew selection, worker permissions, and session E2E"
    elif known_gate and not known_member:
        status = "core_known_backend_gate"
        next_step = "request an authorized third-party Harness registration contract upstream"
    else:
        status = "registration_not_demonstrated"
        next_step = "inspect host registration/routing; do not assume App registration"
    return {
        **base, "status": status,
        "has_registry_function": has_register,
        "has_known_backend_gate": known_gate,
        "has_permission_routing_source": routing_source,
        "byo_literal_in_core": byo_declared or known_member,
        "byo_declared": byo_declared,
        "byo_known_member": known_member,
        "byo_routing_member": routing_member,
        "blockers": blockers,
        "evidence": evidence, "next_step": next_step,
    }


def inspect_native_registration(crew_package: Path) -> dict[str, Any]:
    """Inspect local host sources without importing or modifying them."""
    return inspect_native_sources(_source(crew_package / _BACKENDS), _source(crew_package / _ROUTING))
