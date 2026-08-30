"""Class-level invariants for the Project modules.

Five review rounds each found one instance of the same defect class: a file or
Git coordinate the agent can write was trusted by a privileged reader -- an
unbounded, link-following read of an install-level JSON file, or a fetch whose
URL/branch came from inside an agent-writable checkout. Instance fixes do not
close a class; these guards do. They fail the build when a new raw read or a
checkout-derived coordinate appears in the modules that own Project state.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import kiro_crew

_SRC = Path(kiro_crew.__file__).resolve().parent
_MODULES = (
    "project_capabilities.py",
    "project_git.py",
    "project_registry.py",
    "project_sessions.py",
)

# The ONLY functions allowed to open or decode an install-level / derived file.
# Everything else must call one of them. Adding a name here is a review event.
_READER_ALLOWLIST = {
    "project_capabilities.py": {
        "_read_install_json",  # open_file_no_reparse + fstat + byte cap
        "_read_project_file_bytes",  # descriptor-pinned bundle read
        "_decode_project_json_object",  # json.loads of an already-bounded payload
        "_lock",  # os.open of the lock fd, never read
        "_scan",  # pinned directory traversal: O_DIRECTORY|O_NOFOLLOW via dir_fd
        "_open_file",  # O_NOFOLLOW via dir_fd, the leaf of that traversal
    },
    "project_git.py": {
        "_checkout_matches",  # O_NOFOLLOW + fstat + byte cap on the provenance record
        "_lock",
    },
    "project_registry.py": {
        "_read_registry_bytes",  # O_NOFOLLOW + PROJECT_REGISTRY_MAX_BYTES
        "_load_unlocked",  # json.loads of _read_registry_bytes()
        "_lock",
    },
    "project_sessions.py": set(),
}

_RAW_READ_CALLS = {"read_text", "read_bytes", "load", "loads", "safe_load", "open"}

# Git coordinates a checkout controls. A `_run_git` argv naming any of these
# is trusting agent-writable state; coordinates come from the registration.
_CHECKOUT_COORDINATES = {"origin", "symbolic-ref", "get-url"}


def _enclosing_functions(tree: ast.AST) -> dict[int, str]:
    """Map every line to the innermost def that contains it."""
    owner: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for line in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                # Inner defs are visited too; the LAST writer for a line is the
                # innermost because ast.walk is breadth-first from the module.
                owner[line] = node.name
    return owner


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


@pytest.mark.parametrize("module", _MODULES)
def test_every_install_or_derived_read_goes_through_a_hardened_reader(module: str) -> None:
    source = (_SRC / module).read_text(encoding="utf-8")
    tree = ast.parse(source)
    owners = _enclosing_functions(tree)
    allowed = _READER_ALLOWLIST[module]
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name in _RAW_READ_CALLS or (name == "open" and isinstance(node.func, ast.Attribute)):
            if owners.get(node.lineno, "<module>") not in allowed:
                offenders.append(
                    f"{module}:{node.lineno} {name}() in {owners.get(node.lineno, '<module>')}"
                )
    assert not offenders, (
        "raw file read outside the hardened readers -- route it through "
        "_read_install_json / _read_project_file_bytes (or the registry reader):\n  "
        + "\n  ".join(offenders)
    )


def test_git_store_never_takes_a_coordinate_from_inside_a_checkout() -> None:
    source = (_SRC / "project_git.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    owners = _enclosing_functions(tree)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node) != "_run_git":
            continue
        literals = {
            arg.value
            for arg in node.args
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
        }
        bad = sorted(
            lit for lit in literals if lit in _CHECKOUT_COORDINATES or lit.startswith("origin/")
        )
        # `add()` reads HEAD from the STAGING clone it just made, before publish;
        # that is the one sanctioned symbolic-ref, and it pins the branch.
        if bad == ["symbolic-ref"] and owners.get(node.lineno) == "add":
            continue
        if bad:
            offenders.append(f"project_git.py:{node.lineno} {bad} in {owners.get(node.lineno)}")
    assert not offenders, (
        "git coordinate taken from inside a checkout -- use the registration's pinned "
        "remote/default_branch and FETCH_HEAD:\n  " + "\n  ".join(offenders)
    )
