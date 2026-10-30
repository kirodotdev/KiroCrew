"""Sprites and boxd take their shared pieces from ``session_environments``, not ``coder``.

``coder/`` is Coder's own package. The persistent-VM registry and lifecycle, the
provider routing and move guard, and the VM branches of browse and retention live
in the provider-neutral ``kiro_crew.session_environments`` package, so a VM
provider never reaches into ``coder`` for them.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
VM_PACKAGES = ("sprites_session", "boxd_session")
#: The one ``coder`` module a VM provider may import, and why: the guest worker
#: launch and its remote shell protocol are Coder's on main and run unchanged on
#: every remote provider. Nothing else of ``coder`` is shared.
ALLOWED_CODER_IMPORTS = frozenset({"kiro_crew.coder.remote_worker"})
#: What moved out of ``coder``: none of it may come back there.
MOVED = {
    "kiro_crew.coder.session_launch": (
        "_provider_ids_of",
        "ambiguous_profile_ids",
        "profile_provider",
        "chosen_provider",
        "_vm_environments",
        "_profile_configured",
        "_has_coder_binding",
        "_owned_elsewhere",
        "_dict_profile_ids",
    ),
    "kiro_crew.coder.session_browse": ("_resolve_persistent", "_command_shell_types"),
    "kiro_crew.coder.retention": (
        "_persistent_environments",
        "_sweep_persistent",
        "_last_vm_sweep_error",
        "_SHUTDOWN_HIBERNATE_PARALLEL",
    ),
}


def _imported_modules(path: Path) -> set[str]:
    """Every absolute module a file imports, at any depth (lazy imports included)."""
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            out.add(node.module)
            # ``from kiro_crew.coder import persistent_vm`` names a module too.
            out.update(f"{node.module}.{alias.name}" for alias in node.names)
    return out


@pytest.mark.parametrize("package", VM_PACKAGES)
def test_a_vm_provider_imports_nothing_shared_from_coder(package):
    files = sorted((SRC / package).glob("*.py"))
    assert files, package
    offending = {
        f"{path.name}: {module}"
        for path in files
        for module in _imported_modules(path)
        if (module == "kiro_crew.coder" or module.startswith("kiro_crew.coder."))
        and module not in ALLOWED_CODER_IMPORTS
        # ``from kiro_crew.coder.remote_worker import X`` also yields ``....remote_worker.X``.
        and not any(module.startswith(f"{allowed}.") for allowed in ALLOWED_CODER_IMPORTS)
    }
    assert not offending, sorted(offending)


def test_the_shared_pieces_live_in_session_environments():
    assert importlib.util.find_spec("kiro_crew.coder.persistent_vm") is None
    for module in ("persistent_vm", "routing", "browse", "retention"):
        assert importlib.util.find_spec(f"kiro_crew.session_environments.{module}") is not None
    for module, names in MOVED.items():
        path = Path(importlib.util.find_spec(module).origin)
        defined = set()
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                defined.update(t.id for t in targets if isinstance(t, ast.Name))
        # Imported from the neutral package is fine; defined here again is not.
        assert not sorted(defined & set(names)), module


def test_the_neutral_package_does_not_import_a_vm_provider_at_module_load():
    """The VM providers import the neutral package; it reaches them only lazily,
    inside the functions that dispatch, so neither import order can cycle."""
    for path in sorted((SRC / "session_environments").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        top = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
        names = {
            alias.name if isinstance(node, ast.Import) else (node.module or "")
            for node in top
            for alias in node.names
        }
        providers = {f"kiro_crew.{package}" for package in VM_PACKAGES}
        eager = [n for n in names if any(n == p or n.startswith(f"{p}.") for p in providers)]
        assert not eager, (path.name, eager)
