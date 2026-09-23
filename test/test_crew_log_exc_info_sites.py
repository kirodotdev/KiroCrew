"""Pin: no ``exc_info`` log call in ``crew_log`` sits in a frame that holds a ``CrewLog``.

A handle's write lease is released by a finalizer when the handle is dropped. A log record
carrying ``exc_info`` carries the traceback, the traceback carries every frame between
the ``except`` and the raise, and each of those frames reaches its CALLER through
``tb_frame.f_back`` -- so the retained frames are the whole call stack at the time of the
``except``, not just the span the exception crossed. A handler that keeps records
(pytest's ``caplog``, a ``MemoryHandler``) then keeps those frames -- and any ``CrewLog``
bound in them -- for as long as it keeps the record. That is how a lease outlived its
test in the ninth hygiene pass, and it only shows when the test order puts a
record-keeping handler next to the failure. This pin turns that order-dependent flake
into a deterministic one: a NEW ``exc_info`` call inside a ``CrewLog`` method, or in a
function that names a handle anywhere (a parameter, a local, an opener call), fails here
on the first run. A site whose own frame names no handle but whose caller's does (the
``f_back`` case) is not caught statically; it is pinned by a retention test that drives
the real chain, see test_crew_log_session_tree_projection.py. The package's way to log an
exception from such a frame is ``store.log_exception_text``, which renders the traceback
to text.
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

# The vetted sites: (module, enclosing function). Each one is a scan or a sweep over the
# store's directories with no handle in scope; a traceback there pins no lease. Adding a
# site means adding it here AND satisfying the no-handle check below -- the check is what
# keeps the list honest, the list is what makes a new site a deliberate act. The check
# reads the whole function body, so a handle bound as a local later on (``handle =
# CrewLog.open(...)``) fails it too; the list is not the only guard.
VETTED: frozenset[tuple[str, str]] = frozenset(
    {
        ("checkpoint.py", "_save_one"),
        ("emit.py", "_reap_child_origin"),
        ("holders.py", "holders"),
        ("read.py", "dispatch_view"),
        ("session_tree.py", "reading"),
        ("session_tree.py", "chain"),
        # The projection is a pure in-memory fold over records another writer already
        # committed: the module names no handle at all, opens none, and takes none as a
        # parameter, so no frame OF ITS OWN holds one -- which is why the no-handle check
        # skips the file and each site is listed here. What its own frames cannot say is
        # who CALLED them: ``record_opened`` and ``_schedule_checkpoint`` (through
        # ``apply``) run under the emitter's edge recorder, whose ``log`` is a live
        # handle, and a retained traceback reaches that frame through ``f_back``. Those
        # two render to text and are NOT here. The six below are reached only from the
        # dashboard's seed (``ensure_seeded`` and the two it calls), the maintenance
        # pool's debounced write (``_save_checkpoint``), or ``store.remove_unit`` (the two
        # retention doors), none of which hold a handle.
        ("session_tree_projection.py", "_load_checkpoint"),
        ("session_tree_projection.py", "_replay_tail"),
        ("session_tree_projection.py", "_save_checkpoint"),
        ("session_tree_projection.py", "ensure_seeded"),
        ("session_tree_projection.py", "forget_unit"),
        ("session_tree_projection.py", "retract_unit_parent"),
        ("store.py", "remove_unit"),
        ("store.py", "sweep_expired"),
    }
)


# Callables that hand back a handle without naming the class at the call site.
HANDLE_OPENERS: frozenset[str] = frozenset({"open_session_log"})
HANDLE_NAMES: frozenset[str] = frozenset({"CrewLog", *HANDLE_OPENERS})


def _mentions_handle(fn: ast.AST) -> bool:
    """Does anything in *fn* -- a parameter annotation, a local binding, a call -- name a handle?

    Over-approximate on purpose: a method of ``CrewLog`` (``self``), a parameter annotated
    ``CrewLog``, a local bound from ``CrewLog.open(...)`` / ``CrewLog.create(...)`` /
    ``open_session_log(...)``, an ``isinstance(x, CrewLog)`` -- any of these means a frame
    of this function may hold a handle when the traceback is taken, so a bare ``exc_info``
    there is refused. A function that only mentions the class in a docstring is not caught
    (docstrings are constants, not names), which is the right side to err on.
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


def test_every_log_parameter_of_the_emitter_is_annotated_as_a_handle() -> None:
    """The annotation IS the guard, so it is pinned on its own.

    The no-handle check above decides whether a frame can hold a handle by reading names,
    and a parameter annotated ``Any`` is invisible to it: erode ``log: CrewLog`` back to
    ``log: Any`` and a restored ``exc_info=True`` in that function passes the check again
    with nothing else going red. In ``emit.py`` every parameter named ``log`` is the open
    crew log handed down from the writer job, so each one must name the handle type --
    the module-level import is ``TYPE_CHECKING``-only, so this costs the boot path nothing.
    """
    source = (PACKAGE / "emit.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="emit.py")
    untyped: list[tuple[str, int]] = []
    seen = 0
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for arg in [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]:
            if arg.arg != "log":
                continue
            seen += 1
            if arg.annotation is None or not _mentions_handle(arg.annotation):
                untyped.append((fn.name, fn.lineno))
    assert seen >= 5, f"walk found only {seen} 'log' parameters in emit.py; expected five"
    assert not untyped, (
        "a 'log' parameter in emit.py is not annotated as a CrewLog, so the no-handle check "
        f"cannot see that its frame holds a handle: {untyped}"
    )
