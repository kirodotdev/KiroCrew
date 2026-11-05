"""Pin: no ``exc_info`` log call in ``crew_log`` sits in a frame that holds a ``CrewLog``.

A handle's write lease is released by a finalizer when the handle is dropped. An
``exc_info`` record keeps the traceback, and through ``tb_frame.f_back`` the whole call
stack, so a record-keeping handler (``caplog``, a ``MemoryHandler``) keeps any ``CrewLog``
bound there alive. That flake only shows under some test orders; this pin makes it
deterministic. The caller-only (``f_back``) case is pinned in
test_crew_log_session_tree_projection.py. Use ``store.log_exception_text`` instead.
"""

from __future__ import annotations

import ast
from pathlib import Path

import kiro_crew
import kiro_crew.crew_log as crew_log_pkg

PACKAGE = Path(crew_log_pkg.__file__).parent
# The invariant is process-wide -- a handle dropped anywhere must release its lease -- so the
# no-handle check walks the whole source tree; only the vetted list is package-scoped.
SOURCE_ROOT = Path(kiro_crew.__file__).parent

# The vetted sites: (module, enclosing function), each with no handle in scope. A new site
# must be added here AND pass the no-handle check, which reads the whole function body.
VETTED: frozenset[tuple[str, str]] = frozenset(
    {
        ("emit_runtime.py", "_reap_child_origin"),
        ("holders.py", "holders"),
        ("read.py", "_scanned_view"),
        ("session_tree.py", "reading"),
        ("session_tree.py", "chain"),
        # The projection names no handle, but ``record_opened``, ``record_adopted``,
        # ``record_released`` and ``_schedule_checkpoint`` run under the edge recorder's
        # live ``log`` (reachable via ``f_back``), so those render to text and are not
        # here. The five below are reached only from the seed, ``_save_checkpoint`` or
        # ``store.remove_unit``, none of which hold a handle.
        ("session_tree_projection.py", "forget_unit"),
        ("session_tree_projection.py", "reconcile_unit_edge"),
        ("session_tree_projection.py", "retract_unit_parent"),
        ("session_tree_projection_checkpoint.py", "_load_checkpoint"),
        ("session_tree_projection_checkpoint.py", "_save_checkpoint"),
        ("session_tree_projection_seed.py", "_replay_tail"),
        ("session_tree_projection_seed.py", "ensure_seeded"),
        ("store_removal.py", "remove_unit"),
        ("store_retention.py", "sweep_expired"),
        # The OTel roots gauge reads only the in-memory projection on the SDK's own
        # collection thread, so neither it nor its caller holds a handle.
        ("otel_session_tree_roots.py", "read_roots_with_children"),
    }
)


# Callables that hand back a handle without naming the class at the call site.
HANDLE_OPENERS: frozenset[str] = frozenset({"open_session_log"})
HANDLE_NAMES: frozenset[str] = frozenset({"CrewLog", *HANDLE_OPENERS})


def _mentions_handle(fn: ast.AST) -> bool:
    """Does anything in *fn* -- a parameter annotation, a local binding, a call -- name a handle?

    Over-approximate on purpose: any mention means a frame may hold a handle. A mention
    only in a docstring is not caught, which is the right side to err on.
    """
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and node.id in HANDLE_NAMES:
            return True
        if isinstance(node, ast.Attribute) and node.attr in HANDLE_NAMES:
            return True
        if isinstance(node, ast.Constant) and node.value == "CrewLog":  # a string annotation
            return True
    return False


def _exc_info_sites(
    root: Path, pattern: str, *, handle_files_only: bool = False
) -> list[tuple[str, str, int, bool]]:
    """Every ``exc_info=`` keyword on a call, with its enclosing function and handle check."""
    sites: list[tuple[str, str, int, bool]] = []
    for path in sorted(root.glob(pattern)):
        source = path.read_text(encoding="utf-8")
        if "exc_info" not in source:
            continue  # no site to report; the parse is the expensive part of the walk
        if handle_files_only and not any(n in source for n in HANDLE_NAMES):
            continue  # a file that names no handle has no function that can hold one
        tree = ast.parse(source, filename=str(path))
        # (function node, owning class node or None) for every def, so a site knows both.
        for cls in [None, *[n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]]:
            body = cls.body if cls is not None else tree.body
            for fn in body:
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                holds_handle = (cls is not None and cls.name == "CrewLog") or _mentions_handle(fn)
                for call in ast.walk(fn):
                    if not isinstance(call, ast.Call):
                        continue
                    if any(kw.arg == "exc_info" for kw in call.keywords):
                        rel = path.relative_to(root).as_posix()
                        sites.append((rel, fn.name, call.lineno, holds_handle))
    return sites


def test_no_exc_info_site_anywhere_in_kiro_crew_holds_a_crew_log_handle() -> None:
    sites = _exc_info_sites(SOURCE_ROOT, "**/*.py", handle_files_only=True)
    assert any(f.startswith("crew_log/") for f, _fn, _ln, _h in sites), "walk missed crew_log"
    offenders = [(f, fn, ln) for f, fn, ln, holds in sites if holds]
    assert not offenders, (
        "exc_info inside a frame that holds a CrewLog pins its lease while a handler keeps "
        f"the record; use store.log_exception_text instead: {offenders}"
    )


def test_the_exc_info_sites_left_in_crew_log_are_exactly_the_vetted_ones() -> None:
    found = {(f, fn) for f, fn, _ln, _holds in _exc_info_sites(PACKAGE, "*.py")}
    assert found == VETTED, (
        f"new: {sorted(found - VETTED)}; gone: {sorted(VETTED - found)} -- a new site needs "
        "a look at what its frames hold before it joins VETTED"
    )


def test_every_log_parameter_in_the_package_is_annotated() -> None:
    """The annotation IS the guard, so it is pinned on its own.

    The no-handle check reads names, so ``log: Any`` would hide a handle from it. Every
    ``log`` parameter must be a handle type or a ``logging.Logger``.
    """
    untyped: list[tuple[str, str, int]] = []
    seen: dict[str, int] = {}
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for arg in [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]:
                if arg.arg != "log":
                    continue
                seen[path.name] = seen.get(path.name, 0) + 1
                annotation = "" if arg.annotation is None else ast.unparse(arg.annotation)
                is_logger = annotation in {"logging.Logger", "Logger"}
                if not is_logger and (
                    arg.annotation is None or not _mentions_handle(arg.annotation)
                ):
                    untyped.append((path.name, fn.name, fn.lineno))
    assert (
        sum(n for name, n in seen.items() if name == "emit.py" or name.startswith("emit_")) >= 5
    ), f"walk found only {seen} 'log' parameters; expected five in emit.py and its owners"
    assert not untyped, (
        "a 'log' parameter is annotated neither as a CrewLog nor as a logging.Logger, so the "
        f"no-handle check cannot tell whether its frame holds a handle: {untyped}"
    )
