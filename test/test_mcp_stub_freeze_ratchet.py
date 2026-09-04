"""Ratchet: every writer of the ``mcp_gateway`` config section must freeze first.

``_resolve_stub_servers`` is deliberately conditional on ``enabled`` (a legacy
config with ``enabled: false`` must resolve to an EMPTY stub set so an upgrade
never invents a daemon). That makes the resolved value UNSTABLE across a change
to ``enabled``, so a writer that leaves the file still riding the deprecated
``poolable_servers`` alias hands the next read a different stub set than the
operator was looking at.

``_freeze_stub_servers`` closes that, but only for the writers that call it.
These tests fail when a NEW writer appears without the freeze, which is the
failure mode a reviewer flagged as invisible: correctness resting on two call
sites with no guard against a third.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "kiro_crew"
HANDLER = SRC / "dashboard" / "handlers" / "mcp.py"

# Keys whose write makes the legacy alias resolution unstable or authoritative.
#
# ``stub_overrides`` joins them because it is resolved AGAINST the roster: the
# prune that keeps it sparse compares each decision to what ``stub_servers``
# answers, so a write that lands while the file is still riding the deprecated
# alias compares against the wrong base and records every migrated name as an
# operator deviation — pinning them against later roster changes, which is the
# shadowing the override map exists to prevent.
GUARDED_KEYS = {"enabled", "stub_servers", "stub_overrides"}
FREEZE = "_freeze_stub_servers"

# Helpers that take the mcp_gateway section as a PARAMETER and write a guarded key
# on the caller's behalf. They are the primitives, so they are exempt from the
# freeze requirement themselves; what must be sequenced is their CALLER, which is
# why calling one counts as writing the keys it writes.
SECTION_HELPERS = {FREEZE, "_record_stub_decisions"}


def _section_vars(node: ast.AST) -> set[str]:
    """Names bound from ``<data>.setdefault("mcp_gateway", ...)`` inside *node*.

    Provenance, not naming: a future writer that calls the local something other
    than ``section`` is still caught, and unrelated ``d["enabled"] = ...`` on a
    per-server response dict is still ignored.
    """
    names: set[str] = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Assign):
            continue
        call = sub.value
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "setdefault"
            and call.args
            and isinstance(call.args[0], ast.Constant)
            and call.args[0].value == "mcp_gateway"
        ):
            for t in sub.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
    return names


def _functions_writing_guarded_keys(tree: ast.AST) -> dict[str, set[str]]:
    """Map ``function name -> guarded keys it assigns on the mcp_gateway section``.

    A function that DELEGATES the write to one of :data:`SECTION_HELPERS` is
    credited with that helper's keys. Without this the ratchet goes blind exactly
    when a writer is refactored into a helper — the failure mode its own
    self-check warns about — and the sequencing requirement would silently stop
    applying to the handler that still owns the ordering.
    """
    out: dict[str, set[str]] = {}
    helper_keys: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # A section helper receives the section as a PARAMETER rather than via
        # setdefault, so include its parameter names too.
        targets_ok = _section_vars(node)
        if node.name in SECTION_HELPERS:
            targets_ok |= {a.arg for a in node.args.args}
        if not targets_ok:
            continue
        written: set[str] = set()
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Assign):
                continue
            for target in sub.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id in targets_ok
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value in GUARDED_KEYS
                ):
                    written.add(str(target.slice.value))
        if node.name in SECTION_HELPERS:
            helper_keys[node.name] = written
        if written:
            out[node.name] = written

    # Second pass: a caller of a section helper writes what that helper writes.
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in SECTION_HELPERS:
            continue
        for helper, keys in helper_keys.items():
            if keys and _calls(tree, node.name, helper):
                out.setdefault(node.name, set()).update(keys)
    return out


def _calls(tree: ast.AST, func_name: str, callee: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    fn = sub.func
                    if isinstance(fn, ast.Name) and fn.id == callee:
                        return True
                    if isinstance(fn, ast.Attribute) and fn.attr == callee:
                        return True
    return False


def test_every_section_writer_freezes_the_alias_first() -> None:
    tree = ast.parse(HANDLER.read_text(encoding="utf-8"))
    writers = _functions_writing_guarded_keys(tree)

    # Self-check: the two known writers must still be found, or this ratchet has
    # gone blind (e.g. the assignment was refactored into a helper).
    assert "api_mcp_gateway_enable" in writers, (
        "the sharing-toggle writer was not detected — this ratchet is now blind; "
        "update the detector rather than deleting the test"
    )
    assert "api_mcp_gateway_set_stub" in writers, (
        "the per-server writer was not detected — this ratchet is now blind"
    )

    missing = [
        name
        for name in writers
        # The section helpers ARE the primitives: the freeze helper's own
        # assignment is the freeze, and the recorder is documented to run after
        # it. Sequencing is the CALLER's obligation, which the credit pass above
        # is what makes checkable.
        if name not in SECTION_HELPERS and not _calls(tree, name, FREEZE)
    ]
    assert not missing, (
        f"{missing} write mcp_gateway's {sorted(GUARDED_KEYS)} without calling "
        f"{FREEZE} first. A write that leaves the file on the deprecated "
        "poolable_servers alias lets the NEXT read resolve a different stub set — "
        "for the sharing toggle that silently stubs (or unstubs) every alias entry."
    )


def test_the_builtin_app_config_writer_cannot_reach_mcp_gateway() -> None:
    """``apps/routes.py::_sync_builtin_config`` writes ``section["enabled"]`` for
    whatever key ``_BUILTIN_SERVICE_APPS`` maps an app to, and does NOT freeze.

    It is harmless today because that table is empty, but registering
    ``mcp_gateway`` there would create a third writer outside the guarded
    handler — exactly the drift this ratchet exists to catch.
    """
    routes = SRC / "apps" / "routes.py"
    tree = ast.parse(routes.read_text(encoding="utf-8"))

    table: ast.AST | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "_BUILTIN_SERVICE_APPS":
                table = node.value
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "_BUILTIN_SERVICE_APPS":
                    table = node.value

    assert table is not None, "_BUILTIN_SERVICE_APPS not found in apps/routes.py"
    assert isinstance(table, ast.Dict), "_BUILTIN_SERVICE_APPS is no longer a literal dict"

    mapped = [
        v.elts[0].value
        for v in table.values
        if isinstance(v, ast.Tuple) and v.elts and isinstance(v.elts[0], ast.Constant)
    ]
    assert "mcp_gateway" not in mapped, (
        "a builtin app now maps to the mcp_gateway config section, but "
        "_sync_builtin_config writes section['enabled'] WITHOUT freezing the "
        "stub set first. Route it through the dashboard handler (or call "
        f"{FREEZE}) before adding that mapping."
    )
