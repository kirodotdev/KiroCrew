"""The dashboard server's owner import edges, patch reach and boot order.

This file pins that nothing but the server imports an owner and an owner imports
project modules only for type checking (bar one allowlisted parameter default), that
a fresh import of the server loads every
owner and a second import recomposes the owners onto it, the patch census (every
test file that could patch the server, read in parts, with no owner capturing a name
a test rebinds), the middleware chain, lifecycle hooks and server-call sequence of
both entrypoints' boot and teardown, and the MCP and build route tables. The census
skips only this file; the boot tests and the MCP mount test stay here because the
trace helper they share patches the server through a name the census cannot
resolve, and the route-table tests stay beside them with the shared ``_routes``
helper. The suite is described in
``test_dashboard_server_composition_contract.py``.
"""

from __future__ import annotations

import ast
import asyncio
import functools
import hashlib
import importlib
import inspect
import re
import threading
import types
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from dashboard_server_composition_contract_helpers import (
    _BASE_OWNERS,
    _FACADE,
    _FACADE_PATH,
    _OWNER_DIR,
    _OWNER_PACKAGE,
    _PHASE_OWNERS,
    _SRC,
    _import_targets,
    _owner,
    _owner_runtime_edges,
    _owner_sources,
    _run_child,
    _type_checking_nodes,
    _within,
)
from source_corpus import iter_statements, repo_files_named, repo_root

import kiro_crew.dashboard.server as server

_PHASES = frozenset(name for names in _PHASE_OWNERS.values() for name in names)


# ── one edge ──────────────────────────────────────────────────────────────────


def _package_of(path: Path) -> str:
    parts = list(path.resolve().relative_to(_SRC).with_suffix("").parts)
    return ".".join(parts[:-1])


def test_nothing_but_the_facade_imports_an_owner() -> None:
    """The server module is the one import path and the one patch surface."""
    importers = []
    for path in sorted((_SRC / "kiro_crew").rglob("*.py")):
        resolved = path.resolve()
        if _OWNER_DIR in resolved.parents or resolved == _FACADE_PATH or "_vendor" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if "server_runtime" not in text:
            continue
        importers.extend(
            f"{path.relative_to(_SRC)}:{node.lineno}"
            for node, target in _import_targets(ast.parse(text), _package_of(path))
            if _within(target, _OWNER_PACKAGE)
        )
    assert importers == []


#: The one runtime project import an owner keeps, as ``file_api``'s ``git_panel`` keeps
#: ``_GIT_PANEL_STDOUT_CAP``: ``start_api_server`` reads ``_DEFAULT_PORT`` as its
#: ``port`` parameter default, which Python evaluates when the owner loads, before
#: compose.
_RUNTIME_IMPORT_ALLOWLIST = {
    "api_entrypoint": ("kiro_crew.dashboard.state", "_DEFAULT_PORT"),
}


def _allowed_runtime_import_lines(stem: str, source: str) -> set[int]:
    """The line of the owner's one allowlisted import, spelled as exactly that name."""
    if stem not in _RUNTIME_IMPORT_ALLOWLIST:
        return set()
    module, name = _RUNTIME_IMPORT_ALLOWLIST[stem]
    return {
        node.lineno
        for node in ast.parse(source).body
        if isinstance(node, ast.ImportFrom)
        and node.module == module
        and [(alias.name, alias.asname) for alias in node.names] == [(name, None)]
    }


def test_an_owner_imports_project_modules_only_for_type_checking() -> None:
    """An owner's module-level imports are stdlib and third-party only, plus its
    ``TYPE_CHECKING`` names from the server and the one allowlisted parameter default.
    Its project names come from the server's globals, so an owner adds nothing to the
    gateway's boot import graph and cannot close a cycle with the server."""
    sources = _owner_sources()
    offenders = {
        stem: lines
        for stem, source in sources.items()
        if (
            lines := sorted(
                set(_owner_runtime_edges(source, _OWNER_PACKAGE))
                - _allowed_runtime_import_lines(stem, source)
            )
        )
    }
    assert offenders == {}
    for stem, (module, name) in _RUNTIME_IMPORT_ALLOWLIST.items():
        assert len(_allowed_runtime_import_lines(stem, sources[stem])) == 1, stem
        assert vars(_owner(stem))[name] is getattr(importlib.import_module(module), name)
        assert vars(_owner(stem))[name] is getattr(server, name)
    for name in ("start_dashboard", "start_api_server"):
        default = inspect.signature(getattr(server, name)).parameters["port"].default
        assert default is server._DEFAULT_PORT, name
    for source in _owner_sources().values():
        tree = ast.parse(source)
        guarded = [
            node
            for node in ast.walk(tree)
            if id(node) in _type_checking_nodes(tree) and isinstance(node, ast.ImportFrom)
        ]
        assert [node.module for node in guarded] in ([], [_FACADE])


def test_a_fresh_facade_import_loads_every_owner(tmp_path: Path) -> None:
    """Importing the server imports every owner with it: none loads lazily on a later
    call, so the import order stays the one the one-module file had."""
    _run_child(
        tmp_path,
        """
        import sys
        import kiro_crew.dashboard.server
        missing = [n for n in sys.argv[1:]
                   if f"kiro_crew.dashboard.server_runtime.{n}" not in sys.modules]
        assert missing == [], missing
        print("ok")
        """,
        *_BASE_OWNERS,
    )


def test_a_second_facade_import_recomposes_the_owners_onto_it(tmp_path: Path) -> None:
    _run_child(
        tmp_path,
        """
        import sys
        import kiro_crew.dashboard.server as first
        del sys.modules["kiro_crew.dashboard.server"]
        import kiro_crew.dashboard.server as second
        from kiro_crew.dashboard.server_runtime import listener
        assert second is not first
        assert second._bind_once.__globals__ is vars(second)
        assert listener._bind_once is second._bind_once
        print("ok")
        """,
    )


# ── the patch reach ───────────────────────────────────────────────────────────

_PATCH_CALLS = ("setattr", "patch.object", "delattr")
_MULTIPLE_OPTIONS = frozenset({"spec", "create", "spec_set", "autospec", "new_callable"})
_FUNCTIONS = frozenset({ast.FunctionDef, ast.AsyncFunctionDef})
_ASSIGNMENTS = frozenset({ast.Assign, ast.AugAssign})
#: Nodes with no call inside and no binding: a name, a constant, an expression context.
_LEAVES = frozenset({ast.Name, ast.Constant, ast.Load, ast.Store, ast.Del})
_FACADE_STRING = re.compile(r"""^kiro_crew\.dashboard\.server\.(\w+)$""")

#: The patches whose attribute the scan cannot resolve from the source, keyed by
#: (test file, enclosing function), with the names each one rebinds: the startup
#: harness replaces every step that reaches outside the process through one loop.
_RESOLVED_DYNAMIC_PATCHES: dict[tuple[str, str], frozenset[str]] = {
    ("test_dashboard_server_startup_coverage.py", "_neutralise_outside_process_work"): frozenset(
        {
            "start_enabled_app_backends",
            "start_deferred_app_backends",
            "register_builtin_apps",
            "cleanup_migrated_builtin",
            "on_gateway_startup",
            "on_gateway_shutdown",
        }
    ),
}


def _facade_aliases(tree: ast.Module) -> set[str]:
    """Every expression spelling a test module binds to the server, to a fixed point."""
    aliases = {_FACADE, f"sys.modules[{_FACADE!r}]", f'sys.modules["{_FACADE}"]'}
    assignments: list[tuple[ast.expr, ast.expr]] = []
    # An import and an assignment are statements, so the walk reads statement bodies
    # only and skips the expressions that make up most of a module.
    for node in iter_statements(tree):
        if isinstance(node, ast.Import):
            aliases |= {a.asname for a in node.names if a.name == _FACADE and a.asname}
        elif isinstance(node, ast.ImportFrom) and node.module == "kiro_crew.dashboard":
            aliases |= {a.asname or a.name for a in node.names if a.name == "server"}
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            assignments.append((node.targets[0], node.value))
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            assignments.append((node.target, node.value))
    changed = True
    while changed:
        changed = False
        for target, value in assignments:
            if not isinstance(target, ast.Name) or target.id in aliases:
                continue
            if ast.unparse(value) in aliases or _imports_the_facade(value):
                aliases.add(target.id)
                changed = True
    return aliases


def _imports_the_facade(node: ast.AST) -> bool:
    """``import_module(<server>)``, or ``__import__(<server>, fromlist=...)`` with a
    non-empty fromlist (which returns the server itself, not its root package)."""
    if not (
        isinstance(node, ast.Call)
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == _FACADE
    ):
        return False
    func = ast.unparse(node.func)
    if func.endswith("import_module"):
        return True
    fromlist = {k.arg: k.value for k in node.keywords}.get("fromlist")
    if fromlist is None and len(node.args) >= 4:
        fromlist = node.args[3]
    return (
        func == "__import__" and isinstance(fromlist, (ast.List, ast.Tuple)) and bool(fromlist.elts)
    )


def _parametrized_strings(function: ast.AST) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for decorator in getattr(function, "decorator_list", []):
        if not (
            isinstance(decorator, ast.Call)
            and ast.unparse(decorator.func).endswith("parametrize")
            and len(decorator.args) >= 2
            and isinstance(decorator.args[0], ast.Constant)
            and isinstance(decorator.args[1], (ast.List, ast.Tuple))
        ):
            continue
        names = [n.strip() for n in str(decorator.args[0].value).split(",")]
        if len(names) == 1:
            values = {e.value for e in decorator.args[1].elts if isinstance(e, ast.Constant)}
            if values and all(isinstance(v, str) for v in values):
                found[names[0]] = values
    return found


def _resolve_name(node: ast.AST, params: dict[str, set[str]]) -> set[str] | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Name) and node.id in params:
        return set(params[node.id])
    return None


def _facade_strings(tree: ast.Module, aliases: set[str]) -> set[str]:
    """Every name a test module binds to the server's dotted path, to a fixed point."""
    strings: set[str] = set()
    assignments: list[tuple[ast.expr, ast.expr]] = []
    for node in iter_statements(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            assignments.append((node.targets[0], node.value))
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            assignments.append((node.target, node.value))
    changed = True
    while changed:
        changed = False
        for target, value in assignments:
            if isinstance(target, ast.Name) and target.id not in strings:
                if _string_text(value, strings, aliases) == _FACADE:
                    strings.add(target.id)
                    changed = True
    return strings


def _string_text(node: ast.AST, strings: set[str], aliases: set[str]) -> str | None:
    """The text one piece of a string spells: a constant, a name bound to the server's
    dotted path, or ``<server alias>.__name__``; None for anything else."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in strings:
        return _FACADE
    if (
        isinstance(node, ast.Attribute)
        and node.attr == "__name__"
        and ast.unparse(node.value) in aliases
    ):
        return _FACADE
    return None


def _string_pieces(node: ast.AST) -> list[ast.AST]:
    if isinstance(node, ast.JoinedStr):
        return [v.value if isinstance(v, ast.FormattedValue) else v for v in node.values]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _string_pieces(node.left) + _string_pieces(node.right)
    return [node]


def _resolve_target(
    node: ast.AST, params: dict[str, set[str]], strings: set[str], aliases: set[str]
) -> set[str] | None:
    """The names a string patch target rebinds on the server: an empty set when it
    names another module or an attribute of a server global, None when it names the
    server but the scan cannot tell which attribute."""
    pieces = _string_pieces(node)
    head = ""
    for index, piece in enumerate(pieces):
        text = _string_text(piece, strings, aliases)
        if text is None:
            break
        head += text
    else:
        match = _FACADE_STRING.match(head)
        return {match.group(1)} if match else set()
    if not head.startswith(f"{_FACADE}."):
        return set()
    if head == f"{_FACADE}." and index == len(pieces) - 1:
        return _resolve_name(pieces[index], params)
    return None


def _may_patch_the_facade(text: str) -> bool:
    """Every spelling of a server patch names the server's dotted path or imports
    ``server`` from the dashboard package, so a source with neither holds none."""
    return "dashboard.server" in text or (
        "kiro_crew.dashboard import" in text and re.search(r"\bserver\b", text) is not None
    )


def _patched_names_in(text: str) -> tuple[set[str], set[str]]:
    """``(names, dynamic)``: first-level names one test source rebinds on the server,
    and the enclosing functions of each patch whose name the scan cannot resolve --
    which fails the reach test closed unless it is a resolved one."""
    if not _may_patch_the_facade(text):
        return set(), set()
    tree = ast.parse(text)
    aliases = _facade_aliases(tree)
    strings = _facade_strings(tree, aliases)
    found: set[str] = set()
    dynamic: set[str] = set()

    def read_call(node: ast.Call, params: dict[str, set[str]], label: str) -> None:
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        target = node.args[0] if node.args else kw.get("target")
        if target is None:
            return
        func = ast.unparse(node.func)
        if ast.unparse(target) in aliases or _imports_the_facade(target):
            if func.endswith(_PATCH_CALLS):
                name = node.args[1] if len(node.args) >= 2 else kw.get("attribute", kw.get("name"))
                resolved = _resolve_name(name, params) if name is not None else None
                if resolved is None:
                    dynamic.add(label)
                else:
                    found.update(resolved)
            elif func.endswith("patch.multiple"):
                if any(k.arg is None for k in node.keywords):
                    dynamic.add(label)
                found.update(k for k in kw if k not in _MULTIPLE_OPTIONS)
        elif func.split(".")[-1] in ("patch", "setattr", "delattr"):
            resolved = _resolve_target(target, params, strings, aliases)
            if resolved is None:
                dynamic.add(label)
            else:
                found.update(resolved)

    def visit(node: ast.AST, params: dict[str, set[str]], label: str) -> None:
        # One walk, carrying the innermost enclosing ``def`` -- whose parametrize
        # strings resolve a patched name and whose name labels an unresolved one --
        # down to each call, decorators and defaults included; a lambda opens no
        # scope. Names, constants and expression contexts hold no call and no
        # assignment and are not entered: they are most of a module's nodes, and
        # this walk runs over every test file that could patch the facade.
        kind = type(node)
        if kind in _LEAVES:
            return
        if kind in _FUNCTIONS:
            assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            params, label = _parametrized_strings(node), node.name
        elif kind is ast.Call:
            assert isinstance(node, ast.Call)
            read_call(node, params, label)
        elif kind in _ASSIGNMENTS:
            assert isinstance(node, (ast.Assign, ast.AugAssign))
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and ast.unparse(target.value) in aliases:
                    found.add(target.attr)
        for field in node._fields:
            child = getattr(node, field, None)
            if isinstance(child, ast.AST):
                visit(child, params, label)
            elif isinstance(child, list):
                for item in child:
                    if isinstance(item, ast.AST):
                        visit(item, params, label)

    visit(tree, {}, "<module>")
    found |= set(re.findall(r"""["']kiro_crew\.dashboard\.server\.(\w+)["']""", text))
    return found, dynamic


#: How many parts the patch census is read in. The per-test timeout bounds ONE test,
#: and the whole census -- every test file that could patch the server parsed and
#: walked, under coverage, on a shared runner -- is more than one test's budget. Each
#: part is one test of the capture check, so the budget bounds a quarter of the test
#: tree; the parts hold about the same amount of source, and the sum of the work does
#: not change.
CENSUS_PARTS = 4


@functools.lru_cache(maxsize=1)
def _census_parts() -> tuple[tuple[Path, ...], ...]:
    """The test files the census parses -- under ``test/`` and each ``src/**/tests``,
    this file excepted, whose text could spell a server patch -- dealt into
    ``CENSUS_PARTS`` parts of about equal source size: each file, largest first, joins
    the lightest part so far. Only the paths are kept; the text is read again by the
    part that parses it."""
    root = repo_root()
    here = Path(__file__).resolve()
    sizes: list[tuple[int, Path]] = []
    for path in repo_files_named(".py"):
        parts = path.relative_to(root).parts
        in_tests = parts[0] == "test" or (parts[0] == "src" and "tests" in parts)
        if in_tests and path.resolve() != here:
            text = path.read_text(encoding="utf-8", errors="replace")
            if _may_patch_the_facade(text):
                sizes.append((len(text), path))
    dealt: list[list[Path]] = [[] for _ in range(CENSUS_PARTS)]
    loads = [0] * CENSUS_PARTS
    for size, path in sorted(sizes, key=lambda item: (-item[0], item[1])):
        lightest = loads.index(min(loads))
        dealt[lightest].append(path)
        loads[lightest] += size
    return tuple(tuple(sorted(part)) for part in dealt)


@functools.lru_cache(maxsize=CENSUS_PARTS)
def _census_part(index: int) -> frozenset[str]:
    """The first-level names the tests of census part *index* rebind on the server, each
    unresolvable patch replaced by its listed resolution."""
    found: set[str] = set()
    unresolved = []
    for path in _census_parts()[index]:
        names, dynamic = _patched_names_in(path.read_text(encoding="utf-8", errors="replace"))
        found |= names
        for label in dynamic:
            key = (path.name, label)
            if key in _RESOLVED_DYNAMIC_PATCHES:
                found |= _RESOLVED_DYNAMIC_PATCHES[key]
            else:
                unresolved.append(key)
    assert unresolved == [], "a test patches a server name the scan cannot resolve"
    return frozenset(found)


def _facade_patched_names() -> set[str]:
    """Every first-level name the test trees rebind on the server: the union of the
    census parts, each read on its first use."""
    return set().union(*(_census_part(index) for index in range(CENSUS_PARTS)))


def _captured_names(source: str) -> set[str]:
    """Names an owner module binds or evaluates when it LOADS, outside
    ``TYPE_CHECKING``: everything a later patch of the server cannot reach."""
    tree = ast.parse(source)
    guarded = _type_checking_nodes(tree)
    found: set[str] = set()

    def loads(node: ast.AST) -> set[str]:
        return {
            n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        }

    def visit(statements: list[ast.stmt]) -> None:
        for node in statements:
            if id(node) in guarded:
                continue
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                found.update((a.asname or a.name).split(".")[0] for a in node.names)
                found.update(a.name.split(".")[-1] for a in node.names)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for part in node.decorator_list + node.args.defaults:
                    found.update(loads(part))
                for part in node.args.kw_defaults:
                    if part is not None:
                        found.update(loads(part))
            elif isinstance(node, ast.ClassDef):
                for part in node.decorator_list + node.bases + [k.value for k in node.keywords]:
                    found.update(loads(part))
                for stmt in node.body:
                    if isinstance(stmt, (ast.AnnAssign, ast.Assign)) and stmt.value is not None:
                        found.update(loads(stmt.value))
            elif isinstance(node, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                for field in ("test", "iter", "items"):
                    value = getattr(node, field, None)
                    if isinstance(value, ast.AST):
                        found.update(loads(value))
                    elif isinstance(value, list):
                        for item in value:
                            found.update(loads(item))
                for block in ("body", "orelse", "finalbody"):
                    visit(getattr(node, block, []))
                for handler in getattr(node, "handlers", []):
                    if handler.type is not None:
                        found.update(loads(handler.type))
                    visit(handler.body)
            elif not (
                node is tree.body[0]
                and isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                found.update(loads(node))

    visit(tree.body)
    return found


def test_the_patch_scan_reads_every_spelling() -> None:
    planted = (
        "import importlib, sys\n"
        "import kiro_crew.dashboard.server as srv\n"
        "from kiro_crew.dashboard import server as sv\n"
        "facade = importlib.import_module('kiro_crew.dashboard.server')\n"
        "alias = facade\n"
        "held = sys.modules['kiro_crew.dashboard.server']\n"
        "def test(monkeypatch):\n"
        "    monkeypatch.setattr(srv, 'first', 1)\n"
        "    monkeypatch.setattr(sv, 'second', 2)\n"
        "    patch.object(alias, 'third')\n"
        "    sv.fourth = 4\n"
        "    monkeypatch.setattr('kiro_crew.dashboard.server.fifth', 5)\n"
        "    monkeypatch.setattr(sv.web, 'attr', 7)\n"
        "    monkeypatch.setattr(other, 'not_the_facade', 8)\n"
        "    monkeypatch.delattr(sv, 'sixth')\n"
        "    patch.object(target=alias, attribute='seventh')\n"
        "    patch.multiple(held, eighth=1, create=True)\n"
        "    patch('kiro_crew.dashboard.server.KiroCrewConfig.load')\n"
        "@pytest.mark.parametrize('which', ['ninth', 'tenth'])\n"
        "def test_param(which):\n"
        "    patch(f'kiro_crew.dashboard.server.{which}')\n"
        "FACADE = 'kiro_crew.dashboard.server'\n"
        "other = 'kiro_crew.dashboard.state'\n"
        "def test_strings(monkeypatch):\n"
        "    path = FACADE\n"
        "    patch(f'{path}.eleventh')\n"
        "    patch(FACADE + '.twelfth')\n"
        "    monkeypatch.setattr(f'{sv.__name__}.thirteenth', 13)\n"
        "    patch(f'{other}.not_the_facade')\n"
        "    patch(f'{FACADE}_runtime.not_the_facade')\n"
    )
    names, dynamic = _patched_names_in(planted)
    assert names == {
        "first",
        "second",
        "third",
        "fourth",
        "fifth",
        "sixth",
        "seventh",
        "eighth",
        "ninth",
        "tenth",
        "eleventh",
        "twelfth",
        "thirteenth",
    }
    assert dynamic == set()
    unresolvable = (
        "from kiro_crew.dashboard import server as sv\n"
        "def _drive(monkeypatch, name):\n"
        "    monkeypatch.setattr(sv, name, 1)\n"
    )
    assert _patched_names_in(unresolvable) == (set(), {"_drive"})


def test_the_capture_scan_flags_what_an_owner_evaluates_when_it_loads() -> None:
    planted = (
        '"""An owner."""\n'
        "from typing import TYPE_CHECKING\n"
        "import asyncio as aio\n"
        "if TYPE_CHECKING:\n"
        "    from kiro_crew.dashboard.server import logger\n"
        "LIMIT = _CAP * 2\n"
        "def f(x=_DEFAULT, *, y=_KW):\n"
        "    return logger, sel\n"
        "class C(_Base):\n"
        "    attr: int = _CLASS_BODY\n"
    )
    captured = _captured_names(planted)
    assert {"aio", "asyncio", "_CAP", "_DEFAULT", "_KW", "_Base", "_CLASS_BODY"} <= captured
    assert {"logger", "sel"} & captured == set()


@pytest.mark.parametrize("part", range(CENSUS_PARTS))
def test_no_owner_captures_a_name_tests_rebind_on_the_facade(part: int) -> None:
    """An owner that imported, defaulted or evaluated a rebound name when it loaded
    would keep that object, and a patch of the server would silently stop applying
    there. One census part per test keeps each test's work a bounded share of the test
    tree: a captured name is visible in the part that holds the test rebinding it."""
    patched = _census_part(part)
    for stem, source in _owner_sources().items():
        assert _captured_names(source) & patched == set(), stem


def test_the_whole_census_holds_the_known_rebinds_and_an_owner_s_definitions() -> None:
    """The whole census, read from the parts' caches: it saw the rebinds this suite is
    known to make, and enough of them to have measured anything. An owner may DEFINE a
    rebound name: the server's binding of it is the composed copy, and every caller
    reads it through the server's globals. In file order this test follows the per-part
    checks and reads their cached parts."""
    patched = _facade_patched_names()
    assert {
        "sel",
        "sel_is_warm",
        "data_home",
        "current_context",
        "KiroCrewConfig",
        "DashboardState",
        "_bind_once",
        "_start_site",
        "_reserve_dashboard_port",
        "_start_unix_site",
        "_extra_frame_ancestors",
        "_write_secret_file",
        "logger",
        "start_enabled_app_backends",
    } <= patched
    assert len(patched) >= 50
    for stem in _owner_sources():
        defined = {name for name in vars(_owner(stem)) if name in patched}
        assert all(getattr(server, name) is vars(_owner(stem))[name] for name in defined), stem


# ── the boot ──────────────────────────────────────────────────────────────────

#: The full middleware chain of each entrypoint, outermost first, by the name of each
#: layer and the factory that built it. The order is a security contract: latency is
#: outermost, the relay-path header rule is next so it reads a verified handover before
#: any inner layer, the client-sent relay-header strip sits just inside it so no inner
#: layer reads a client-sent relay header, the
#: deny-audit boundary is outer to every barrier that can refuse, the SEL request
#: audit is inner to them, the per-slot ownership checkpoint is inner to token auth
#: and the audit record, and the workflow and crewmate-prune gates, the Shared Crew
#: team-config gate and the change-card hook (innermost, after auth has classified
#: the caller) are appended after the explicit list.
_DASHBOARD_CHAIN = (
    ("route_latency_middleware", "make_route_latency_middleware.<locals>."),
    ("_relay_ingress", "relay_ingress_middleware.<locals>."),
    ("relay_header_strip_middleware", "relay_header_strip_middleware"),
    ("deny_audit_middleware", "_make_deny_audit_middleware.<locals>."),
    ("host_canonical_redirect", "build_host_canonical_redirect.<locals>."),
    ("host_validation_middleware", "_make_host_validation_middleware.<locals>."),
    ("reject_compressed_body_middleware", "reject_compressed_body_middleware"),
    ("no_cache_middleware", "_install_dashboard_middlewares.<locals>."),
    ("csrf_middleware", "_make_csrf_middleware.<locals>."),
    ("middleware", "token_auth_middleware.<locals>."),
    ("sel_audit_middleware", "_install_dashboard_middlewares.<locals>."),
    ("slot_ownership_middleware", "slot_ownership_middleware"),
    ("spa_fallback", "_install_dashboard_middlewares.<locals>."),
    ("_workflow_ready", "_register_workflow_lifecycle.<locals>."),
    ("_crewmate_prune_gate", "_register_crewmate_prune_gate.<locals>."),
    ("team_config_middleware", "team_config_middleware"),
    ("_change_card_hook", "_register_change_card_hook.<locals>."),
)
_API_CHAIN = (
    ("route_latency_middleware", "make_route_latency_middleware.<locals>."),
    ("_relay_ingress", "relay_ingress_middleware.<locals>."),
    ("relay_header_strip_middleware", "relay_header_strip_middleware"),
    ("deny_audit_middleware", "_make_deny_audit_middleware.<locals>."),
    ("host_validation_middleware", "_make_host_validation_middleware.<locals>."),
    ("reject_compressed_body_middleware", "reject_compressed_body_middleware"),
    ("csrf_middleware", "_make_csrf_middleware.<locals>."),
    ("middleware", "token_auth_middleware.<locals>."),
    ("sel_audit_middleware", "_install_api_middlewares.<locals>."),
    ("slot_ownership_middleware", "slot_ownership_middleware"),
    ("team_config_middleware", "team_config_middleware"),
    ("_workflow_ready", "_register_workflow_lifecycle.<locals>."),
)

#: The lifecycle hooks the server registers, in registration order, by name; hooks
#: route slices and handler modules register are not the server's and are left out.
_DASHBOARD_HOOKS = {
    "on_startup": (
        "_hooks_startup",
        "_contrib_startup",
        "_stt_startup",
        "_own_host_warm",
        "_instances_startup",
        "_browser_sessions_startup",
    ),
    "on_cleanup": (
        "_tunnel_shutdown",
        "_status_sink_shutdown",
        "_hooks_shutdown",
        "_contrib_shutdown",
        "_watchdog_shutdown",
        "_diag_recorder_shutdown",
        "_prevent_sleep_shutdown",
        "_listener_guard_shutdown",
        "_kiro_prerequisite_shutdown",
        "_kas_login_shutdown",
        "_stt_shutdown",
        "_local_decision_model_shutdown",
        "_decision_http_shutdown",
        "_config_watch_shutdown",
        "_instances_shutdown",
        "_crew_log_drain",
        "_browser_install_shutdown",
        "_browser_view_shutdown",
        "_connections_warm_shutdown",
        "_workflow_shutdown",
        "_unlink_unix_socket",
    ),
    "on_shutdown": ("_workflow_stop_publication",),
    "on_response_prepare": ("_finalize_asset_cache_control",),
}
_API_HOOKS = {
    "on_startup": ("_stt_startup", "_own_host_warm"),
    "on_cleanup": (
        "_kiro_prerequisite_shutdown",
        "_kas_login_shutdown",
        "_stt_shutdown",
        "_local_decision_model_shutdown",
        "_decision_http_shutdown",
        "_config_watch_shutdown",
        "_prevent_sleep_shutdown",
        "_listener_guard_shutdown",
        "_browser_install_shutdown",
        "_connections_warm_shutdown",
        "_workflow_shutdown",
        "_unlink_unix_socket",
    ),
    "on_shutdown": ("_workflow_stop_publication",),
    "on_response_prepare": (),
}

#: The server calls each boot makes from its own task, in order, by server global name;
#: the ones its teardown makes; and, as a multiset, the ones made ELSEWHERE -- on a
#: worker thread (``T:``) or by another task on the loop (``S:``) -- over boot and
#: teardown together. Captured from the one-module file and pinned so the split could
#: not reorder a step, drop one or move one across the bind; the boot phases the
#: entrypoints now delegate to are names the base did not have, so only the calls
#: they make count. A boot step added on purpose updates these with it.
_DASHBOARD_BOOT = tuple("""
    consume_managed_service_launch_environment set_pending_staged_hook
    set_pending_consumed_hook set_global_hook_store register_skill_read_observer
    wire_session_subagent_probe _wire_tunnel_shutdown _wire_status_delta_sink
    register_status_delta_sink _precompute_telemetry current_context
    _register_mcp_routes _deferred _deferred setup_spawn_resume_routes _deferred
    _deferred_work_ledger _deferred_work_ledger _deferred_work_ledger
    _deferred_work_ledger _deferred_work_ledger _deferred _deferred _deferred _deferred _deferred _deferred
    _deferred _deferred _deferred _deferred _deferred _deferred
    _deferred _deferred _deferred _deferred _deferred _deferred _deferred _deferred
    _deferred _deferred _deferred _deferred _deferred _deferred _deferred _deferred
    _deferred _deferred _deferred _deferred _deferred _deferred _deferred
    _deferred _deferred _deferred _deferred _deferred
    _deferred _deferred _deferred _deferred _deferred _deferred _deferred _deferred
    _deferred _deferred _deferred _deferred _deferred _deferred _deferred _deferred
    _deferred _deferred _deferred _deferred _deferred _deferred _deferred _deferred
    _deferred _deferred _deferred _deferred _deferred _deferred _deferred _deferred
    _deferred _deferred _deferred _deferred _deferred _deferred _deferred _deferred _deferred _deferred _deferred _deferred _deferred
    _deferred _deferred _deferred _deferred _deferred _deferred
    _deferred _deferred _deferred _deferred _deferred _deferred _deferred
    register_all subprocess_executor subprocess_executor subprocess_executor
    subprocess_executor subprocess_executor subprocess_executor _register_deploy_routes
    setup_knowledge_routes setup_weixin_routes setup_feedback_routes
    setup_secrets_routes setup_whatsapp_routes setup_link_meta_routes bind_address_for
    _reserve_dashboard_port subprocess_executor init_hooks_system safe_context_call
    current_context _register_dist_static_routes _dist_file_handler _dist_file_handler
    _dist_file_handler _dist_file_handler _dist_file_handler discover_app_window_entries
    register_app_window_paths _install_asset_cache_control_finalizer
    tailnet_effective_allowed_logins tailnet_identity_unknown degraded_config_files
    build_allowed_origins _make_host_validation_middleware
    _make_csrf_middleware
    _make_deny_audit_middleware resolve_dashboard_host build_host_canonical_redirect
    warm_auth_singletons warm_sel_singleton make_route_latency_middleware
    relay_ingress_middleware
    _mixed_internal_api_paths safe_context_call current_context token_auth_middleware
    _insert_enterprise_gate _register_prevent_sleep_shutdown _register_listener_guard_shutdown
    _register_stt_hooks _register_own_host_warm _register_config_watch
    _register_instances_hooks _register_browser_install_cleanup
    _register_browser_view_cleanup _register_connections_warm_lifecycle
    _register_workflow_lifecycle _register_crewmate_prune_gate _register_change_card_hook
    _register_unix_socket_cleanup build_hardened_runner on_gateway_startup
    init_hook_reconciler async_safe_context_call current_context register_pre_exec_stop
    _arm_listener_guard
    _kick_crewmate_prune _kick_pinned_order_load subprocess_executor _kick_default_app_installs _start_unix_site _resolved_bound_port
    _start_secondary_loopback_site subprocess_executor _note_listener_sidecar
    _reconcile_listener_publication _kick_workflow_initialization
    _kick_connections_warm_scavenge _kick_session_search_index _kick_config_watch
    _kick_local_decision_model _kick_knowledge_orphan_reclaim _kick_owner_only_sweep
    _kick_display_verdicts _kick_otel_crew_log_subscriber
    load_loop_stall_exit_after
    _arm_prevent_sleep_poll register_pre_exec_stop safety_override safety_override
    safety_override
    await_crewmate_prune_settled current_context record_boot_to_ready
    _dispatch_healthy_boot_marker
    """.split())
_DASHBOARD_TEARDOWN = tuple("""
    current_context unregister_status_delta_sink stop_hook_reconciler
    on_gateway_shutdown async_safe_context_call current_context _release_sleep_block
    """.split())
_DASHBOARD_ELSEWHERE = tuple("""
    S:_initialize_workflow_service S:_prebuild_memory_after_preparation
    S:_prune_browser_snapshots_loop S:_stt_idle_sweep
    S:_stt_startup_prewarm T:_apply_startup_yolo T:_claimed_dashboard_slots
    T:_live_sibling_port T:_register_deploy_skills
    T:_take_prior_dropped_grant
    T:_write_instance_credentials T:_write_secret_file T:_write_secret_file
    T:_write_secret_file T:apply_config_duration T:cleanup_migrated_builtin
    T:cleanup_migrated_builtin T:cleanup_migrated_builtin T:cleanup_migrated_builtin
    T:migrate_channel_transcripts T:migrate_existing_crewmates T:newest_dump_with_stacks T:open_dump_file
    T:prune_synced_crewmates T:record_healthy_boot T:refresh_config_meta_stamp
    T:refresh_materialized_agents
    T:register_builtin_apps T:rotate_dumps T:start_deferred_app_backends
    T:start_enabled_app_backends T:sweep_stale_dumps T:take_dropped_grant
    T:tighten_data_home T:warm_own_host_names
    """.split())
_API_BOOT = tuple("""
    set_global_hook_store register_skill_read_observer wire_session_subagent_probe
    _precompute_telemetry current_context tailnet_effective_allowed_logins
    tailnet_identity_unknown degraded_config_files build_allowed_origins
    _make_host_validation_middleware _make_csrf_middleware _make_deny_audit_middleware
    warm_auth_singletons warm_sel_singleton make_route_latency_middleware
    relay_ingress_middleware
    _mixed_internal_api_paths safe_context_call current_context token_auth_middleware
    _insert_enterprise_gate _register_mcp_routes _deferred _deferred setup_spawn_resume_routes
    _deferred
    _deferred_work_ledger _deferred_work_ledger _deferred_work_ledger
    _deferred_work_ledger _deferred_work_ledger _deferred _deferred _deferred _deferred _deferred _deferred _deferred _deferred _deferred
    _deferred _deferred _deferred _deferred _deferred _deferred
    _deferred _deferred _deferred _deferred _deferred _deferred _deferred _deferred
    _deferred _deferred _deferred _deferred _deferred _deferred _deferred _deferred
    _deferred _deferred _deferred _deferred _deferred _deferred _deferred
    _deferred _deferred _deferred _deferred _deferred
    _deferred _deferred _deferred _deferred _deferred _deferred _deferred _deferred
    _deferred _deferred _deferred _deferred _deferred _deferred _deferred _deferred
    _deferred _deferred _deferred _deferred _deferred _deferred _deferred _deferred
    _deferred _deferred _deferred _deferred _deferred _deferred _deferred _deferred
    _deferred _deferred _deferred _deferred _deferred _deferred _deferred _deferred _deferred _deferred _deferred _deferred _deferred
    _deferred _deferred _deferred _deferred _deferred _deferred
    _deferred _deferred _deferred _deferred
    _register_deploy_routes _register_stt_hooks _register_own_host_warm
    _register_config_watch _register_prevent_sleep_shutdown
    _register_listener_guard_shutdown _register_browser_install_cleanup
    _register_connections_warm_lifecycle _register_workflow_lifecycle
    _register_unix_socket_cleanup build_hardened_runner bind_address_for _start_site
    _arm_listener_guard _kick_pinned_order_load _export_bound_port _resolved_bound_port
    _start_unix_site _resolved_bound_port _resolved_bound_host _start_secondary_loopback_site
    subprocess_executor _resolved_bound_host _resolved_bound_host _note_listener_sidecar
    _resolved_bound_host _reconcile_listener_publication _kick_workflow_initialization
    _kick_connections_warm_scavenge _kick_session_search_index _kick_config_watch
    _kick_local_decision_model _kick_owner_only_sweep _kick_display_verdicts
    _arm_prevent_sleep_poll
    register_pre_exec_stop record_boot_to_ready _dispatch_healthy_boot_marker
    """.split())
_API_TEARDOWN: tuple[str, ...] = ("_release_sleep_block",)
_API_ELSEWHERE = tuple("""
    S:_initialize_workflow_service S:_stt_idle_sweep S:_stt_startup_prewarm
    T:_live_sibling_port T:_write_instance_credentials T:_write_secret_file
    T:_write_secret_file T:_write_secret_file T:record_healthy_boot T:tighten_data_home
    T:warm_own_host_names
    """.split())

#: Done callbacks of tasks the boot creates, whose position depends on when a worker
#: thread finishes rather than on the boot's own order.
_TIMING_DEPENDENT = frozenset({"_own_host_warm_done", "_log_prewarm_outcome"})


def _install_trace(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Wrap every server global that is a project function or a test double, so each
    call records its name: bare from the calling task, ``S:<name>`` from another task
    or a loop callback, ``T:<name>`` from a worker thread."""
    trace: list[str] = []
    own = asyncio.current_task()

    def _label(name: str) -> str:
        if threading.current_thread() is not threading.main_thread():
            return f"T:{name}"
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        return name if task is own else f"S:{name}"

    for name, value in list(vars(server).items()):
        if name.startswith("__") or isinstance(value, (type, types.ModuleType)):
            continue
        if isinstance(value, (MagicMock, AsyncMock)):

            def _double(*a: Any, __name: str = name, __target: Any = value, **k: Any) -> Any:
                trace.append(_label(__name))
                return __target(*a, **k)

            monkeypatch.setattr(server, name, _double)
            continue
        if not isinstance(value, types.FunctionType):
            continue
        if not (getattr(value, "__module__", "") or "").startswith("kiro_crew"):
            continue
        if inspect.iscoroutinefunction(value):

            async def _async(*a: Any, __name: str = name, __fn: Any = value, **k: Any) -> Any:
                trace.append(_label(__name))
                return await __fn(*a, **k)

            wrapped: Any = functools.wraps(value)(_async)
        else:

            def _sync(*a: Any, __name: str = name, __fn: Any = value, **k: Any) -> Any:
                trace.append(_label(__name))
                return __fn(*a, **k)

            wrapped = functools.wraps(value)(_sync)
        monkeypatch.setattr(server, name, wrapped)
    return trace


def _own(trace: list[str]) -> tuple[str, ...]:
    return tuple(t for t in trace if ":" not in t and t not in _PHASES)


def _elsewhere(trace: list[str]) -> tuple[str, ...]:
    return tuple(
        sorted(
            t for t in trace if ":" in t and t.split(":", 1)[1] not in _PHASES | _TIMING_DEPENDENT
        )
    )


def _server_hooks(app: web.Application) -> dict[str, tuple[str, ...]]:
    return {
        signal: tuple(
            hook.__name__
            for hook in getattr(app, signal)
            if getattr(hook, "__module__", "") == _FACADE
        )
        for signal in ("on_startup", "on_cleanup", "on_shutdown", "on_response_prepare")
    }


def _chain_matches(app: web.Application, expected: tuple[tuple[str, str], ...]) -> None:
    middlewares = list(app.middlewares)
    assert [mw.__name__ for mw in middlewares] == [name for name, _ in expected]
    for mw, (name, built_by) in zip(middlewares, expected, strict=True):
        assert mw.__qualname__.startswith(built_by), (name, mw.__qualname__)


async def _traced_dashboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[web.AppRunner, Any, list[str], list[str]]:
    """Boot the real dashboard through the startup-coverage harness, traced, with the
    second loopback family left unbound so the boot takes one path on every host."""
    import test_dashboard_server_startup_coverage as harness

    monkeypatch.setattr(server, "_start_secondary_loopback_site", AsyncMock(return_value=None))
    # The crewmate prune is kicked onto a worker thread a few steps before the boot
    # merges channel transcripts, and the merge kicks a deferred removal when the
    # prune has not settled yet. Which branch runs depends on the worker thread's
    # speed, so hold the merge until the prune has settled: the recorded order is
    # then the settled one on every runner, however slow.
    converge = server._converge_channel_transcripts

    @functools.wraps(converge)
    async def _converge_once_settled(state: Any) -> None:
        await asyncio.wait_for(state.crewmate_prune_settled.wait(), timeout=30)
        await converge(state)

    monkeypatch.setattr(server, "_converge_channel_transcripts", _converge_once_settled)
    holder: dict[str, list[str]] = {}
    real = server.start_dashboard

    async def _start(*args: Any, **kwargs: Any) -> Any:
        holder["trace"] = _install_trace(monkeypatch)
        return await real(*args, **kwargs)

    monkeypatch.setattr(server, "start_dashboard", _start)
    runner, state, _spies = await harness._start_dashboard(tmp_path, monkeypatch)
    await _knowledge_reclaim_finished(state)
    boot = list(holder["trace"])
    del holder["trace"][:]
    return runner, state, boot, holder["trace"]


#: How long the boot's knowledge-reclaim task may run before the test gives up on it.
_RECLAIM_WAIT_SECONDS = 30


async def _knowledge_reclaim_finished(state: Any) -> None:
    """Wait for the boot's knowledge reclaim task, which then calls the memory prebuild.

    ``_kick_knowledge_orphan_reclaim`` reaches ``_prebuild_memory_after_preparation``
    only after its worker thread returns. The teardown cancels every stray task, so
    on a slow runner the prebuild is never called and drops out of the trace.
    This one task is finite, unlike the boot's other background loops, so waiting
    for it alone records the prebuild on every runner.
    """
    reclaim = [
        task
        for task in state._background_tasks
        if task.get_coro().__qualname__.endswith("._knowledge_orphan_reclaim")
    ]
    if reclaim:
        await asyncio.wait_for(asyncio.gather(*reclaim), timeout=_RECLAIM_WAIT_SECONDS)


async def _traced_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[web.AppRunner, Any, list[str], list[str]]:
    import test_dashboard_server_coverage as harness

    monkeypatch.setattr(server, "_start_unix_site", AsyncMock(return_value=None))
    monkeypatch.setattr(server, "_start_secondary_loopback_site", AsyncMock(return_value=None))
    holder: dict[str, list[str]] = {}
    real = server.start_api_server

    async def _start(*args: Any, **kwargs: Any) -> Any:
        holder["trace"] = _install_trace(monkeypatch)
        return await real(*args, **kwargs)

    monkeypatch.setattr(server, "start_api_server", _start)
    runner, state = await harness._start_api(tmp_path, monkeypatch)
    boot = list(holder["trace"])
    del holder["trace"][:]
    return runner, state, boot, holder["trace"]


@pytest.mark.asyncio
async def test_the_dashboard_boot_keeps_its_chain_hooks_and_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import test_dashboard_server_startup_coverage as harness

    runner, state, boot, teardown = await _traced_dashboard(tmp_path, monkeypatch)
    try:
        app = runner.app
        _chain_matches(app, _DASHBOARD_CHAIN)
        token_auth = [mw for mw in app.middlewares if getattr(mw, "_is_token_auth", False)]
        assert [mw.__name__ for mw in token_auth] == ["middleware"]
        assert _server_hooks(app) == _DASHBOARD_HOOKS
        assert _own(boot) == _DASHBOARD_BOOT
    finally:
        await runner.cleanup()
        await harness._cancel_stray_tasks()
        harness._release_process_handles(state)
    assert _own(teardown) == _DASHBOARD_TEARDOWN
    assert _elsewhere(boot + teardown) == _DASHBOARD_ELSEWHERE


@pytest.mark.asyncio
async def test_the_headless_boot_keeps_its_chain_hooks_and_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import test_dashboard_server_startup_coverage as harness

    runner, _state, boot, teardown = await _traced_api(tmp_path, monkeypatch)
    try:
        app = runner.app
        _chain_matches(app, _API_CHAIN)
        assert _server_hooks(app) == _API_HOOKS
        assert _own(boot) == _API_BOOT
    finally:
        await runner.cleanup()
        await harness._cancel_stray_tasks()
    assert _own(teardown) == _API_TEARDOWN
    assert _elsewhere(boot + teardown) == _API_ELSEWHERE


def _routes(app: web.Application) -> list[tuple[str, str, str]]:
    return [
        (route.method, route.resource.canonical, getattr(route.handler, "__name__", ""))
        for route in app.router.routes()
        if route.resource is not None
    ]


#: SHA-256 of the MCP route table's ``"<method> <path> <handler>"`` rows in
#: registration order, and their count. The table is shared by both entrypoints, so a
#: route added to it on purpose updates these with it.
_MCP_TABLE_ROWS = 324
_MCP_TABLE_DIGEST = "8dd185f753c62320e0ebc8918f1c007480efcb11489dec57d5772ff8315f1048"


def test_the_mcp_route_table_keeps_its_rows_and_order() -> None:
    app = web.Application()
    server._register_mcp_routes(app)
    rows = [" ".join(row) for row in _routes(app)]
    digest = hashlib.sha256("\n".join(rows).encode()).hexdigest()
    assert (len(rows), digest) == (_MCP_TABLE_ROWS, _MCP_TABLE_DIGEST), "\n".join(rows)


def test_a_literal_mcp_route_precedes_the_pattern_that_would_swallow_it() -> None:
    """aiohttp resolves a request in REGISTRATION order, so a literal path registered
    after a pattern route that matches it is unreachable for that method."""
    app = web.Application()
    server._register_mcp_routes(app)
    shadowed = []
    seen: list[tuple[str, Any]] = []
    for route in app.router.routes():
        resource = route.resource
        if resource is None:
            continue
        info = resource.get_info()
        if "path" in info:
            for method, pattern in seen:
                if method in (route.method, "*") and pattern.fullmatch(info["path"]):
                    shadowed.append((route.method, info["path"]))
        elif "pattern" in info:
            seen.append((route.method, info["pattern"]))
    assert shadowed == []


@pytest.mark.asyncio
async def test_both_entrypoints_mount_the_one_mcp_table_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import test_dashboard_server_startup_coverage as harness

    table = web.Application()
    server._register_mcp_routes(table)
    expected = _routes(table)
    runner, state, _spies = await harness._start_dashboard(tmp_path / "dash", monkeypatch)
    try:
        assert _routes(runner.app)[: len(expected)] == expected
    finally:
        await runner.cleanup()
        await harness._cancel_stray_tasks()
        harness._release_process_handles(state)
    api_runner, _state, _boot, _teardown = await _traced_api(tmp_path / "api", monkeypatch)
    try:
        api_routes = _routes(api_runner.app)
        assert api_routes[: len(expected)] == expected
        probes = [path for _, path, _ in api_routes[len(expected) : len(expected) + 6]]
        assert probes == ["/api/health", "/api/health", "/api/live", "/api/live"] + [
            "/api/ready",
            "/api/ready",
        ]
    finally:
        await api_runner.cleanup()
        await harness._cancel_stray_tasks()


def test_the_build_routes_register_every_prefix_then_the_vendor_preflight(tmp_path: Path) -> None:
    app = web.Application()
    server._register_dist_static_routes(app, tmp_path / "dist")
    rows = [(m, p) for m, p, _ in _routes(app)]
    prefixes = ("assets", "sprites", "fonts", "vendor", "app-assets")
    assert rows == [
        (method, f"/{prefix}/{{tail}}") for prefix in prefixes for method in ("HEAD", "GET")
    ] + [("OPTIONS", "/vendor/{tail}")]
