"""GATE F1 — architectural fitness test for the workflows engine layering.

Enforces the layering declared in the module spec + dir-scoped AGENTS.md:

    validate, dsl, __init__   (leaf / contract — no intra-package deps)
        ↑
    context                    (may import: __init__, validate)
        ↑
    runner                     (may import: __init__, validate, dsl, events, context)

    events                     (may import: __init__)

Rules, as a FAILING build instead of prose nobody enforces (research/06 Artifact 1):
  * No backward imports (a lower layer importing a higher one).
  * The engine must NOT import ``kiro_crew.dashboard.state`` internals directly —
    progress goes through ``EventBus`` / a thin port (keeps F1 honest so the UI
    can't reach into dashboard guts).

Pure-stdlib AST scan — no ``import-linter`` dependency (not in the version set).
If this goes RED you introduced a layering violation; fix the import direction,
do not relax the rule (see GATES.md F1).
"""

from __future__ import annotations

import ast
from pathlib import Path

WORKFLOWS_DIR = Path(__file__).resolve().parent.parent / "src" / "kiro_crew" / "workflows"

# Allowed intra-package imports per module (the layering contract). A module may
# import ONLY the siblings listed here (plus the package root ``.`` == __init__).
ALLOWED_SIBLING_IMPORTS: dict[str, set[str]] = {
    "__init__": set(),
    "validate": set(),
    "dsl": set(),
    "schema": set(),  # leaf: structured-output validator, no intra-pkg deps
    "events": set(),  # imports only the package root (__init__)
    "registry": set(),  # leaf: background-run registry, imports only __init__ (store via DI)
    "agent_exec": set(),  # production agent_fn adapter; imports llm_helpers (external),
    #                       no intra-pkg siblings — an OPTIONAL adapter, not engine layer
    "store": set(),  # durable JSON-per-run persistence; imports config.paths + security
    #                  (external), no intra-pkg siblings — an OPTIONAL persistence adapter
    "context": {"validate"},
    "runner": {"validate", "dsl", "events", "context", "schema", "registry"},
    # service is the gateway-side façade ABOVE runner — composes the engine for the
    # live process (chat tools / app / injection). Top of the stack.
    "service": {"validate", "registry", "runner", "agent_exec", "store"},
}

# Modules outside the package the engine must never import directly (F1).
FORBIDDEN_EXTERNAL_PREFIXES = (
    "kiro_crew.dashboard.state",
    "kiro_crew.dashboard.ws",
)


def _module_files() -> list[Path]:
    return sorted(p for p in WORKFLOWS_DIR.glob("*.py"))


def _intra_pkg_siblings(tree: ast.Module) -> set[str]:
    """Sibling module names imported via ``from . import X`` / ``from .sib import Y``.

    ``from . import X`` (level 1, no module) imports names from the package root
    (__init__) — that is always allowed and not a sibling-module edge.
    """
    siblings: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            # ``from .context import ...`` → module == "context"
            siblings.add(node.module.split(".")[0])
    return siblings


def _external_imports(tree: ast.Module) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                out.add(alias.name)
    return out


def test_layering_no_backward_or_unexpected_sibling_imports() -> None:
    for path in _module_files():
        name = path.stem
        assert name in ALLOWED_SIBLING_IMPORTS, (
            f"new workflows module '{name}.py' — add it to ALLOWED_SIBLING_IMPORTS "
            "with its permitted layer (this test is the layering contract)"
        )
        tree = ast.parse(path.read_text())
        actual = _intra_pkg_siblings(tree)
        allowed = ALLOWED_SIBLING_IMPORTS[name]
        violations = actual - allowed
        assert not violations, (
            f"{name}.py imports siblings {sorted(violations)} not permitted by the "
            f"layering (allowed: {sorted(allowed) or 'none'}). Fix the import "
            "direction; do not relax F1."
        )


def test_engine_does_not_import_dashboard_internals() -> None:
    for path in _module_files():
        tree = ast.parse(path.read_text())
        for imported in _external_imports(tree):
            for forbidden in FORBIDDEN_EXTERNAL_PREFIXES:
                assert not imported.startswith(forbidden), (
                    f"{path.name} imports '{imported}' — the engine must not reach "
                    f"into '{forbidden}'. Route progress through EventBus / a port."
                )


def test_every_module_is_covered_by_the_contract() -> None:
    """No workflows module escapes the layering contract unnoticed."""
    on_disk = {p.stem for p in _module_files()}
    assert on_disk == set(ALLOWED_SIBLING_IMPORTS), (
        f"workflows modules on disk {sorted(on_disk)} != layering contract "
        f"{sorted(ALLOWED_SIBLING_IMPORTS)} — update ALLOWED_SIBLING_IMPORTS."
    )
