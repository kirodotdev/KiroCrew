"""``kiro_crew.dashboard.handlers.sessions`` keeps the patch seams and the surface the one module had.

``handlers/sessions.py`` is a mirror facade (:mod:`kiro_crew._facade_mirror`) over the owner
modules in ``dashboard/handlers/sessions_parts/``. Callers, tests and documents still reach
every name as ``kiro_crew.dashboard.handlers.sessions.<name>``, and the moved code reads its
names as plain module globals, so a patch through the facade reaches that code only because
the mirror writes it into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirror facade to the shared contract.
These tests pin what is particular to this one: every name the test trees patch through it
reaches every module holding it; the owners are exactly the ``sessions_parts`` modules, each
importing only the owners before it; the family logs through one logger named for the
facade; an exception class an owner defines still reports the facade's module; every
``api_session*`` route is wrapped as an owner surface exactly once, apart from the three
that scope their own caller; and every project name the mirror serves from an owner's
import is named for type checkers in the facade.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
import sys
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus

from kiro_crew import _facade_mirror
from kiro_crew.dashboard.handlers import sessions, sessions_parts

pytestmark = pytest.mark.xdist_group(name="tree_scan_sessions_parts_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("known_seams", "corpus_gate")

FACADE = "kiro_crew.dashboard.handlers.sessions"
PARTS = "kiro_crew.dashboard.handlers.sessions_parts"

#: Names the test trees are known to patch through the facade, one per kind of seam: a
#: function the facade and an owner both bind, a project import only the facade binds, a
#: project import only owners bind, a stdlib module several members bind, a global the
#: facade rebinds, an owner's mutable state, a helper several owners read, and owner
#: functions and constants patched by dotted string. The scan must find all of them, so a
#: scanner that stops matching goes red instead of passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "_fetch_whoami",
        "_resolve_kiro_bin_for_spawn",
        "wrap_argv",
        "time",
        "_usage_cache",
        "_TOOL_POLICY_MEMO",
        "_sel",
        "_reset_all_sessions",
        "_SHUTDOWN_TIMEOUT_SECS",
        "_remove_slot_for_history_key",
    }
)

#: The routes that verify and scope their own caller, left unwrapped by every guard.
_MEMBER_SCOPED = frozenset(
    {"api_session_directive", "api_session_keepalive", "api_session_tool_policy"}
)


def _index() -> _facade_mirror.MirrorIndex:
    return sessions.__dict__[_facade_mirror.INDEX_ATTRIBUTE]


def _owners() -> list[ModuleType]:
    return [sys.modules[name] for name in _index().owners]


def _family() -> list[ModuleType]:
    return [sessions, *_owners()]


def _source(module: ModuleType) -> str:
    return Path(module.__file__ or "").read_text(encoding="utf-8")


def _test_tree_files() -> list[Path]:
    """Every test-tree source: ``test/``, the root conftest, and the builtin apps' tests."""
    root = source_corpus.repo_root()
    test_root = root / "test"
    builtins_root = root / "src" / "kiro_crew" / "apps" / "builtins"
    found = []
    for path in source_corpus.repo_files_named(".py"):
        if path == root / "conftest.py" or path.is_relative_to(test_root):
            found.append(path)
        elif path.is_relative_to(builtins_root) and (
            "tests" in path.relative_to(builtins_root).parts[:-1]
            or path.name.startswith("test_")
            or path.name == "conftest.py"
        ):
            found.append(path)
    return found


def _corpus_patched_names() -> set[str]:
    spellings = {FACADE: FACADE}
    names: set[str] = set()
    for path in _test_tree_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if not facade_mirror_scan._mentions(text, spellings):
            continue
        names |= facade_mirror_scan.text_patched_names(text, spellings).get(FACADE, set())
        if facade_mirror_scan.needs_syntax_tree(text, spellings):
            try:
                events = facade_mirror_scan.patch_events(text, spellings)
            except SyntaxError:
                continue
            names |= facade_mirror_scan.names_by_target(events, {FACADE}).get(FACADE, set())
    return names


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    """The reach the one module had: a write to ``sessions.<name>`` was what every reader
    of the name read. Every member of the family that binds a patched name sees the
    patched object, and the undo leaves the family one object per name again."""
    family = _family()
    missed: dict[str, list[str]] = {}
    checked = 0
    for name in sorted(_corpus_patched_names()):
        if not hasattr(sessions, name):
            continue  # a test that creates the name with raising=False
        holders = [module for module in family if name in vars(module)]
        sentinel = object()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(sessions, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(sessions, name) is not sentinel:
                stale.append(FACADE)
        checked += 1
        if stale:
            missed[name] = stale
    assert missed == {}
    assert checked >= len(_KNOWN_SEAMS)
    assert _index().divergent() == ()


def test_the_owners_are_the_sessions_parts_modules_in_layer_order() -> None:
    """``_OWNERS`` names every module of ``sessions_parts``, and each owner imports only
    owners listed before it: the order the facade loads them and a read resolves in."""
    owners = list(_index().owners)
    on_disk = {f"{PARTS}.{info.name}" for info in pkgutil.iter_modules(sessions_parts.__path__)}
    assert sorted(owners) == sorted(on_disk)
    late: dict[str, list[str]] = {}
    for position, module in enumerate(_owners()):
        tree = ast.parse(_source(module))
        imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        later = sorted(m for m in imported if m in owners and owners.index(m) >= position)
        if later:
            late[module.__name__] = later
    assert late == {}


def test_the_facade_keeps_the_default_read_path_and_forwards_no_table() -> None:
    """``sessions.py`` was a plain module: the facade forwards no legacy table and keeps
    the default ``forward_reads``, and every served name comes from the index."""
    index = _index()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports)
    assert index.served, "the facade serves nothing, so the mirror measures nothing"


def test_the_family_logs_through_one_logger_named_for_the_facade() -> None:
    """``caplog`` filters keyed on ``kiro_crew.dashboard.handlers.sessions`` keep seeing the
    moved code, at DEBUG too: a sibling module's own logger would not be a child of it."""
    assert sessions.logger.name == FACADE
    loggers = [m for m in _family() if "logger" in vars(m)]
    assert len(loggers) >= 9
    assert [m.__name__ for m in loggers if vars(m)["logger"] is not sessions.logger] == []


def test_an_exception_an_owner_defines_keeps_the_facade_module() -> None:
    """A traceback or a log line renders a raised class as ``module.qualname``, so an
    exception class an owner defines reports the facade, as it did before it moved."""
    found = {
        name: value
        for module in _owners()
        for name, value in vars(module).items()
        if isinstance(value, type)
        and issubclass(value, BaseException)
        and name in _facade_mirror._defined_names(ast.parse(_source(module)))
    }
    assert "ManagedToolPolicyUnreadable" in found, "no owner-defined exception was found"
    assert {name: cls.__module__ for name, cls in found.items()} == dict.fromkeys(found, FACADE)


def _routes() -> dict[str, object]:
    """Every ``api_session*`` coroutine route the facade answers, bound or served."""
    names = {n for n in dir(sessions) if n.startswith("api_session")}
    return {n: getattr(sessions, n) for n in sorted(names)}


def test_every_api_session_route_is_an_owner_surface_exactly_once() -> None:
    """The one module wrapped every ``api_session*`` coroutine in its namespace with
    ``guard_owner_surface_routes`` at its bottom, leaving out the three that verify and
    scope their own caller. Each owner now wraps its own, so a route is wrapped once,
    the three stay bare, and the object every holder binds is the one callers reach."""
    routes = _routes()
    assert len(routes) >= 18, sorted(routes)
    bare = sorted(n for n, r in routes.items() if not hasattr(r, "__wrapped__"))
    assert bare == sorted(_MEMBER_SCOPED)
    twice = sorted(
        n for n, r in routes.items() if hasattr(getattr(r, "__wrapped__", None), "__wrapped__")
    )
    assert twice == []
    # ``api_approvals*`` never matched the ``api_session`` prefix, so they stay bare too.
    approvals = {n: getattr(sessions, n) for n in ("api_approvals", "api_approval_resolve")}
    assert [n for n, r in approvals.items() if hasattr(r, "__wrapped__")] == []
    for name, route in routes.items():
        target = inspect.unwrap(route)  # type: ignore[arg-type]
        assert inspect.iscoroutinefunction(target), name
        holders = [m for m in _family() if name in vars(m)]
        assert holders, name
        assert [m.__name__ for m in holders if vars(m)[name] is not route] == [], name


def _typing_imports(source: str) -> dict[str, str]:
    """Name -> home module of each import under ``if TYPE_CHECKING:`` in *source*."""
    found: dict[str, str] = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for sub in node.body:
                if isinstance(sub, ast.ImportFrom) and sub.module:
                    found.update({a.asname or a.name: sub.module for a in sub.names})
                elif isinstance(sub, ast.Import):
                    found.update({a.asname or a.name: a.name for a in sub.names})
    return found


def _project_imports(source: str, family: set[str]) -> dict[str, tuple[str, str | None]]:
    """Name -> (home module, imported attribute or None for a module) of each run-time
    import of a ``kiro_crew`` module outside the family in *source*."""
    found: dict[str, tuple[str, str | None]] = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("kiro_crew") and node.module not in family:
                found.update({a.asname or a.name: (node.module, a.name) for a in node.names})
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("kiro_crew") and a.asname:
                    found[a.asname] = (a.name, None)
    return found


def test_every_project_name_an_owner_serves_is_named_for_type_checkers() -> None:
    """A project name the one module imported is served now from the owner that imports
    it, which a type checker cannot see through the mirror; the facade names each one
    under ``TYPE_CHECKING`` from the module it comes from, so a reader of
    ``sessions.<name>`` keeps type-checking."""
    family = {FACADE, *_index().owners}
    served = set(_index().served)
    imported: dict[str, tuple[str, str | None]] = {}
    for module in _owners():
        imported.update(_project_imports(_source(module), family))
    served_imports = {name: home for name, home in imported.items() if name in served}
    assert served_imports, "no served name is an owner's project import, so this measured nothing"
    named = _typing_imports(_source(sessions))
    assert {name: named.get(name) for name in served_imports} == {
        name: home for name, (home, _attr) in served_imports.items()
    }
    for name, (home, attr) in served_imports.items():
        expected = (
            sys.modules[home] if attr is None else getattr(importlib.import_module(home), attr)
        )
        assert getattr(sessions, name) is expected, name
