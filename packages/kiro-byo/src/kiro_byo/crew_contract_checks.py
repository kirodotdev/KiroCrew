"""Fail-closed, read-only Crew source contract verification.

No installer transaction or patch mutation is permitted here. This module
validates the *actual installed source* rather than trusting version strings.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

_PREFIX = "src/kiro_crew/"

def _normalized_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")


def _top_level_bound_names(text: str) -> frozenset[str]:
    """Return executable module bindings, excluding comments/strings/usages.

    Definitions inside a module-level conditional count because Python can bind
    them at import time (for example Crew's PEP 562 ``__getattr__``). Imports
    inside such conditionals deliberately do *not* count: ``if TYPE_CHECKING``
    imports are not runtime bindings.
    """
    tree = ast.parse(text)
    names: set[str] = set()

    def bind_target(node: ast.AST) -> None:
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, (ast.Tuple, ast.List)):
            for item in node.elts:
                bind_target(item)

    def visit_statements(nodes: list[ast.stmt], *, imports: bool) -> None:
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    bind_target(target)
            elif imports and isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    names.add(alias.asname or alias.name.split(".", 1)[0])
            elif isinstance(node, ast.If):
                visit_statements(node.body, imports=False)
                visit_statements(node.orelse, imports=False)
            elif isinstance(node, ast.Try):
                visit_statements(node.body, imports=False)
                visit_statements(node.orelse, imports=False)
                visit_statements(node.finalbody, imports=False)
                for handler in node.handlers:
                    visit_statements(handler.body, imports=False)

    visit_statements(tree.body, imports=True)
    return frozenset(names)


def _literal_strings_bound_to(text: str, binding: str) -> frozenset[str]:
    """Return literal strings contained in one module-level assignment value."""
    tree = ast.parse(text)
    value_node: ast.AST | None = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == binding for t in node.targets):
                value_node = node.value
                break
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == binding:
                value_node = node.value
                break
    if value_node is None:
        return frozenset()
    try:
        value = ast.literal_eval(value_node)
    except (ValueError, TypeError, SyntaxError):
        return frozenset()

    found: set[str] = set()
    def collect(item: object) -> None:
        if isinstance(item, str):
            found.add(item)
        elif isinstance(item, dict):
            for key, val in item.items():
                collect(key)
                collect(val)
        elif isinstance(item, (list, tuple, set, frozenset)):
            for val in item:
                collect(val)
    collect(value)
    return frozenset(found)


def _export_contract_issues(raw_exports: dict[str, Any] | None, crew_package: Path) -> list[str]:
    """Check imported collection exports against actual Crew module bindings.

    This is independent of symbol/marker checks, and can be tested against a
    synthetic source package without an installer transaction.
    """
    issues: list[str] = []
    if raw_exports is None:
        return issues
    for relative, bindings in raw_exports.items():
        rel = str(relative).replace("\\", "/")
        if not rel.startswith(_PREFIX):
            issues.append(f"{rel} (contract path outside KiroCrew source root)")
            continue
        if not isinstance(bindings, dict):
            issues.append(f"{rel} (invalid contract export map)")
            continue
        target = crew_package / rel[len(_PREFIX):]
        if not target.is_file():
            issues.append(f"{rel} (missing overlay dependency)")
            continue
        try:
            text = _normalized_text(target)
        except OSError as exc:
            issues.append(f"{rel} ({exc})")
            continue
        for binding, values in bindings.items():
            if not isinstance(binding, str) or not binding or not isinstance(values, list) or not all(
                isinstance(v, str) and v for v in values
            ):
                issues.append(f"{rel} (invalid contract export list)")
                continue
            try:
                present = _literal_strings_bound_to(text, binding)
            except SyntaxError as exc:
                issues.append(f"{rel} (cannot parse overlay dependency: {exc.msg})")
                break
            missing = [value for value in values if value not in present]
            if missing:
                rendered = ", ".join(repr(v) for v in missing[:3])
                suffix = " ..." if len(missing) > 3 else ""
                issues.append(
                    f"{rel} (overlay export contract moved; {binding} missing {rendered}{suffix})"
                )
    return issues


def compatibility_contract_issues(compat: dict[str, Any], crew_package: Path) -> list[str]:
    """Validate unmodified upstream symbols the owned overlay imports.

    Semantic patch inspection proves the files Kiro BYO *changes*. The created
    HarnessAdapter module also imports a small set of Crew-owned APIs/private
    grant constants that are not patched. Future KiroCrew builds may keep a familiar
    version shape while moving one of those symbols; accepting such a build
    would turn "fail closed" into an import/runtime failure. Compatibility
    metadata therefore pins actual module-level bindings for those dependencies.
    """
    raw_symbols = compat.get("required_contract_symbols")
    raw_exports = compat.get("required_contract_exports")
    raw_markers = compat.get("required_contract_markers")
    if raw_symbols is None and raw_exports is None and raw_markers is None:
        return []
    if raw_symbols is not None and not isinstance(raw_symbols, dict):
        return ["required_contract_symbols (invalid metadata)"]
    if raw_exports is not None and not isinstance(raw_exports, dict):
        return ["required_contract_exports (invalid metadata)"]
    if raw_markers is not None and not isinstance(raw_markers, dict):
        return ["required_contract_markers (invalid metadata)"]

    issues: list[str] = []
    requirements: dict[str, tuple[str, list[str]]] = {}
    for kind, raw in (("symbol", raw_symbols), ("marker", raw_markers)):
        if raw is None:
            continue
        for relative, values in raw.items():
            rel = str(relative).replace("\\", "/")
            if rel in requirements:
                issues.append(f"{rel} (duplicate overlay contract metadata)")
                continue
            if not isinstance(values, list) or not all(isinstance(v, str) and v for v in values):
                issues.append(f"{rel} (invalid contract {kind} list)")
                continue
            requirements[rel] = (kind, list(values))

    for rel, (kind, values) in requirements.items():
        if not rel.startswith(_PREFIX):
            issues.append(f"{rel} (contract path outside KiroCrew source root)")
            continue
        target = crew_package / rel[len(_PREFIX):]
        if not target.is_file():
            issues.append(f"{rel} (missing overlay dependency)")
            continue
        try:
            text = _normalized_text(target)
        except OSError as exc:
            issues.append(f"{rel} ({exc})")
            continue
        if kind == "symbol":
            try:
                present = _top_level_bound_names(text)
            except SyntaxError as exc:
                issues.append(f"{rel} (cannot parse overlay dependency: {exc.msg})")
                continue
            missing = [value for value in values if value not in present]
        else:
            # Backward-compatible metadata support for older adapters. New
            # adapters should prefer required_contract_symbols.
            missing = [value for value in values if value not in text]
        if missing:
            rendered = ", ".join(repr(v) for v in missing[:3])
            suffix = " ..." if len(missing) > 3 else ""
            issues.append(f"{rel} (overlay contract moved; missing {rendered}{suffix})")

    issues.extend(_export_contract_issues(raw_exports, crew_package))
    if "src/kiro_crew/agent_sdk/tool_gate.py" in compat.get("retired_generated_files", ()):
        issues.extend(retired_tool_gate_contract_issues(crew_package))
    return issues


def _version_core(value: str) -> tuple[int, int, int] | None:
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", str(value or ""))
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _crew_seam_is_symlink(crew_package: Path, target: Path) -> bool:
    """Refuse an overlay destination reached through a link inside Crew.

    Only lexical descendants of the Crew package are permitted. This is a
    preflight guard, not a promise of race-free OS-level sandboxing.
    """
    try:
        relative = target.relative_to(crew_package)
    except ValueError:
        return True
    if crew_package.is_symlink() or ".." in relative.parts:
        return True
    candidate = crew_package
    for component in relative.parts:
        candidate = candidate / component
        if candidate.is_symlink():
            return True
    return False




def _install_plan_precommit_issue(plan: dict[str, Any], crew_package: Path, normalized_sha: Any) -> str | None:
    """Recheck Crew file identities after preflight and before commit."""
    dst = plan["dst"]
    if _crew_seam_is_symlink(crew_package, dst):
        return f"Crew seam became symlinked during integration preflight: {plan['rel']}; retry"
    if plan.get("existed_before", True):
        if not dst.is_file() or normalized_sha(dst) != plan["current_hash"]:
            return f"Crew seam changed during integration preflight: {plan['rel']}; retry"
    elif dst.exists():
        return f"created-file path appeared during integration preflight: {plan['rel']}; retry"
    return None


def rc6_contract_issues(crew_package: Path) -> list[str]:
    """Check upstream rc.6 worker-policy and cold-start safety seams, read-only.

    These shapes were inspected in the public rc.6 tag. Matching them is NOT
    desktop certification; an unknown or changed implementation is refused.
    """
    locations = {
        "mate_worker.py": ("resolve_worker_spec", "worker_session_spec"),
        "mate_spec_policy.py": ("applies_spec_policy", "layer_policy", "policy_of"),
        "mcp_gateway/backend_handshake.py": ("_effective_initialize_timeout_secs",),
        "config/validation.py": ("_apply_field_default",),
    }
    trees: dict[str, ast.Module] = {}
    issues: list[str] = []
    for rel, required in locations.items():
        path = crew_package / rel
        try:
            tree = ast.parse(_normalized_text(path))
        except (OSError, SyntaxError, UnicodeError) as exc:
            issues.append(f"rc.6 {rel} (unreadable source contract: {exc})")
            continue
        names = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        if any(name not in names for name in required):
            issues.append(f"rc.6 {rel} (required worker/initialization function missing)")
            continue
        trees[rel] = tree

    worker = trees.get("mate_worker.py")
    if worker is not None:
        methods = {n.name: n for n in worker.body if isinstance(n, ast.FunctionDef)}
        for name in ("resolve_worker_spec", "worker_session_spec"):
            method = methods[name]
            kw = {arg.arg for arg in method.args.kwonlyargs}
            if "refuse_spec_policy" not in kw:
                issues.append(f"rc.6 mate_worker.py ({name} no longer accepts refusal gate)")
        session = methods["worker_session_spec"]
        forwarded = any(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "resolve_worker_spec"
            and any(kw.arg == "refuse_spec_policy" and isinstance(kw.value, ast.Name)
                    and kw.value.id == "refuse_spec_policy" for kw in node.keywords)
            for node in ast.walk(session)
        )
        if not forwarded:
            issues.append("rc.6 mate_worker.py (session refusal is not forwarded)")
        resolver = methods["resolve_worker_spec"]
        checks = [n for n in ast.walk(resolver) if isinstance(n, ast.If)
                  and isinstance(n.test, ast.Name) and n.test.id == "refuse_spec_policy"]
        # A raise in the `else` branch must never be treated as refusal on
        # the True path. Keep source checks conservative: without a raise
        # reachable from the gated True body, reject the upstream seam.
        refusal = any(isinstance(n, ast.Raise) for branch in checks
                      for stmt in branch.body for n in ast.walk(stmt))
        if not refusal:
            issues.append("rc.6 mate_worker.py (worker policy is not refused)")

    policy = trees.get("mate_spec_policy.py")
    if policy is not None:
        method = next(n for n in policy.body if isinstance(n, ast.FunctionDef)
                      and n.name == "applies_spec_policy")
        kas_only = any(isinstance(n, ast.Compare)
                       and len(n.ops) == 1 and isinstance(n.ops[0], ast.Eq)
                       and isinstance(n.left, ast.Name) and n.left.id == "backend"
                       and len(n.comparators) == 1
                       and isinstance(n.comparators[0], ast.Name)
                       and n.comparators[0].id == "ACP_BACKEND_KAS"
                       for n in ast.walk(method))
        if not kas_only:
            issues.append("rc.6 mate_spec_policy.py (KAS-only worker policy gate changed)")

    handshake = trees.get("mcp_gateway/backend_handshake.py")
    if handshake is not None:
        floors = [n.value for node in handshake.body if isinstance(node, ast.Assign)
                  and any(isinstance(t, ast.Name)
                          and t.id == "_INITIALIZE_TIMEOUT_COLD_START_FLOOR_SECS"
                          for t in node.targets)
                  for n in [node.value] if isinstance(n, ast.Constant)
                  and isinstance(n.value, (int, float))]
        if not floors or floors[0] < 30:
            issues.append("rc.6 mcp_gateway/backend_handshake.py (30s cold-start floor missing)")
        method = next(n for n in handshake.body if isinstance(n, ast.FunctionDef)
                      and n.name == "_effective_initialize_timeout_secs")
        if not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id == "max" and any(isinstance(arg, ast.Name)
                   and arg.id == "_INITIALIZE_TIMEOUT_COLD_START_FLOOR_SECS"
                   for arg in n.args) for n in ast.walk(method)):
            issues.append("rc.6 mcp_gateway/backend_handshake.py (floor not applied)")

    validation = trees.get("config/validation.py")
    if validation is not None:
        failclosed = [node for node in validation.body if isinstance(node, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "_FAIL_CLOSED_PATHS"
                              for t in node.targets)]
        if not failclosed or not any(isinstance(n, ast.Constant) and n.value == "agent"
                                     for n in ast.walk(failclosed[0].value)):
            issues.append("rc.6 config/validation.py (malformed agent no longer fails closed)")
    return issues



def retired_tool_gate_contract_issues(crew_package: Path) -> list[str]:
    """Prove the host's *own* unknown-routing fallback is fail-closed.

    R74 no longer rewrites tool_gate.py. Its unmodified 0.12.0 default already
    reports INDETERMINATE, and ``is_enforced`` still refuses to certify any
    new routing. Pin those structural properties before accepting a future
    Crew source; a version match or source comment is not a security contract.

    This is NOT runtime enforcement certification: BYO may still be selectable
    despite the ``False`` result. Worker/MCP E2E authorization is separate.
    """
    rel = "agent_sdk/tool_gate.py"
    path = crew_package / rel
    if path.is_symlink() or not path.is_file():
        return [f"{rel}: missing or unsafe read-only security contract"]
    try:
        tree = ast.parse(_normalized_text(path))
    except (OSError, UnicodeError, SyntaxError) as exc:
        return [f"{rel}: security contract unreadable: {exc}"]
    assignments = [node for node in tree.body if isinstance(node, (ast.Assign, ast.AnnAssign))
                   and any(isinstance(target, ast.Name) and target.id == "ENFORCED_ROUTINGS"
                           for target in (node.targets if isinstance(node, ast.Assign) else (node.target,)))]
    if len(assignments) != 1:
        return [f"{rel}: ENFORCED_ROUTINGS declaration changed"]
    # Require a literal, closed mechanism set, not a computed expression that
    # might hide a new ``ACP_PERMISSION_GATE`` member behind a function call.
    declaration = assignments[0].value
    if not (isinstance(declaration, ast.Call) and isinstance(declaration.func, ast.Name)
            and declaration.func.id == "frozenset" and len(declaration.args) == 1
            and not declaration.keywords and isinstance(declaration.args[0], ast.Set)):
        return [f"{rel}: enforced-routing registry is no longer a closed literal"]
    names = declaration.args[0].elts
    if not all(isinstance(item, ast.Attribute) and isinstance(item.value, ast.Name)
               and item.value.id == "Routing" for item in names):
        return [f"{rel}: enforced-routing set contains a nonliteral mechanism"]
    if any(item.attr == "ACP_PERMISSION_GATE" for item in names):
        return [f"{rel}: unverified ACP_PERMISSION_GATE was promoted to enforced"]
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    verdict = functions.get("routing_verdict")
    enforced = functions.get("is_enforced")
    if verdict is None or enforced is None or not verdict.body:
        return [f"{rel}: routing verdict or enforcement probe absent"]
    # Require the actual function's final path to return INDETERMINATE, not just
    # an unrelated string, a dead branch or an earlier declaration.
    last = verdict.body[-1]
    if not isinstance(last, ast.Return) or not isinstance(last.value, ast.Tuple) or not last.value.elts:
        return [f"{rel}: unknown routing no longer has explicit final fallback"]
    first = last.value.elts[0]
    if not (isinstance(first, ast.Attribute) and isinstance(first.value, ast.Name)
            and first.value.id == "Verdict" and first.attr == "INDETERMINATE"):
        return [f"{rel}: unknown routing fallback no longer reports INDETERMINATE"]
    if len(enforced.body) not in (1, 2) or (len(enforced.body) == 2 and not (
            isinstance(enforced.body[0], ast.Expr)
            and isinstance(enforced.body[0].value, ast.Constant)
            and isinstance(enforced.body[0].value.value, str))):
        return [f"{rel}: enforcement decision shape changed"]
    ret = enforced.body[-1]
    if not (isinstance(ret, ast.Return) and isinstance(ret.value, ast.Compare)
            and len(ret.value.ops) == 1 and isinstance(ret.value.ops[0], ast.In)
            and len(ret.value.comparators) == 1
            and isinstance(ret.value.comparators[0], ast.Name)
            and ret.value.comparators[0].id == "ENFORCED_ROUTINGS"):
        return [f"{rel}: is_enforced no longer uses the audited host routing set"]
    return []
