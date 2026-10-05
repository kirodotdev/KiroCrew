"""``kiro_crew.sandbox`` stays the sandbox's import and patch surface after the split.

The Linux launcher renderer, the macOS Seatbelt renderer and the stale mount-source sweep
are defined in ``sandbox_launcher``, ``sandbox_seatbelt`` and ``sandbox_mount_sweep``.
The facade keeps every name moved to those owners readable under its old path, and
FORWARDS every one of them, so a patch through the facade lands on the owner, where the
owner's own callers read it -- whether or not a scan could read the patched name off the
test. The path rules moved to ``kiro_crew.sandbox_plan`` are bound on the facade as plain
compatibility aliases instead, pinned by identity below; a patch of one does not reach
the planner. The two
builders are the facade's own functions: they gather the live host here, plan the spawn
(``kiro_crew.sandbox_plan``) and hand the plan to the owner's renderer, so a test that
rebinds a tier list or a host-fact helper on the facade reaches the plan, and no owner
imports the facade at all.

This file also holds what both facades share -- ``kiro_crew.sandbox`` and
``kiro_crew.platform_compat`` -- so ``test_platform_compat_refactor_facade`` runs the same
checks on its facade: the static shape of a facade module, the owner-import scan, and the
census of every facade patch in the test trees, which is read once for both facades.

The seam rows patch ONE name with a stub that raises ``_Reached`` and then drive a
function that must read that name. ``_Reached`` derives from ``BaseException`` on
purpose: several of these helpers are best-effort and swallow ``Exception``, and a stub
such a handler could swallow would let a patch that MISSED read as one that landed.
"""

from __future__ import annotations

import ast
import copy
import functools
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import logging
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Callable
from unittest import mock

import pytest
import test_sandbox_refactor_create_guard as create_guard

from kiro_crew import (
    sandbox,
    sandbox_launcher,
    sandbox_mount_sweep,
    sandbox_plan,
    sandbox_seatbelt,
)

# The patch census parses every test file once; keep it on one worker.
pytestmark = pytest.mark.xdist_group(name="tree_scan_sandbox_refactor_facade")

#: The rows that drive a sandbox backend or its sweep: the OS sandbox is POSIX-only, as
#: the Windows collect-ignore list records for the suites these rows sit beside.
_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="the OS sandbox is POSIX-only")

_SRC = Path(sandbox.__file__).resolve().parent
_REPO = _SRC.parents[1]

_OWNERS: dict[str, ModuleType] = {
    m.__name__: m for m in (sandbox_launcher, sandbox_seatbelt, sandbox_mount_sweep)
}

#: Every module-level name the split moved out of sandbox.py, by its owner. The two
#: builders are not here: they are ``kiro_crew.sandbox``'s own functions again, which
#: plan the spawn and hand the plan to the owners' renderers.
_MOVED: dict[str, tuple[str, ...]] = {
    "kiro_crew.sandbox_launcher": (),
    "kiro_crew.sandbox_seatbelt": ("_SEATBELT_PROFILE",),
    "kiro_crew.sandbox_mount_sweep": (
        "_MOUNT_SOURCE_PREFIX",
        "_MOUNT_SOURCE_MAX_AGE_SECONDS",
        "_PIN_SCAN_MAX_PASSES",
        "_MOUNT_TABLE_CACHE_MAX_ENTRIES",
        "_MOUNT_TABLE_CACHE_MAX_BYTES",
        "_PinScanCoverage",
        "_task_uid",
        "_OVERFLOW_UID_SYSCTL",
        "_overflow_uid",
        "_parse_pid_segment",
        "_mount_source_candidate_roots",
        "_mount_pinned_source_names",
        "_SWEEP_TIME_BUDGET_SECONDS",
        "_SWEEP_BUDGET_CHECK_EVERY",
        "_cleanup_stale_sandbox_mount_sources",
        "_LEGACY_MOUNT_SOURCE_RE",
        "_LEGACY_PILE_THRESHOLD",
        "_LEGACY_RESIDUE_MARKER",
        "_launcher_tmpfs_roots",
        "_bound_source_basenames",
        "_cleanup_legacy_mount_source_residue",
        "_cleanup_retired_acp_snapshot_dir",
    ),
}

#: Every function in an owner that imports from the facade, and what it reads there.
#: None does: the renderers read only the plan they are handed, and the live host the
#: plan is made from is gathered in ``kiro_crew.sandbox`` itself.
_SEAM_IMPORTS: dict[tuple[str, str], tuple[str, tuple[str, ...]]] = {}


class _Reached(BaseException):
    """Raised by a stub to prove the reader read the patched name."""


def _raiser(label: str) -> Callable[..., object]:
    def _stub(*_args: object, **_kwargs: object) -> object:
        raise _Reached(label)

    return _stub


# --------------------------------------------------------------------------- #
# Checks both facades run, each against its own tables.
# --------------------------------------------------------------------------- #


def _moved_rows(moved: dict[str, tuple[str, ...]]) -> list[tuple[str, str]]:
    return [(owner, name) for owner, names in moved.items() for name in names]


def check_moved_name(
    facade: ModuleType, owners: dict[str, ModuleType], owner: str, name: str
) -> None:
    """The owner defines the name, and the facade reads that very object."""
    owner_module = owners[owner]
    assert name in vars(owner_module)
    assert getattr(facade, name) is vars(owner_module)[name]


def check_forwarded_and_bound_are_disjoint(
    facade: ModuleType, moved: dict[str, tuple[str, ...]], bound: frozenset[str]
) -> None:
    assert [n for n in facade._EXPORTS if n in vars(facade)] == []
    names = {n for group in moved.values() for n in group}
    assert {n for n in names if n in vars(facade)} == set(bound)
    assert set(facade._EXPORTS) == names - bound


def check_forwarding_table(facade: ModuleType, owners: dict[str, ModuleType]) -> None:
    assert set(facade._EXPORTS) <= set(dir(facade))
    assert set(facade._EXPORTS.values()) <= set(owners)
    assert all(isinstance(owner, str) for owner in facade._EXPORTS.values())
    assert {
        name: owner for owner, names in facade._EXPORTS_BY_OWNER.items() for name in names
    } == facade._EXPORTS


def check_round_trips(facade: ModuleType, owner: ModuleType, name: str) -> None:
    """monkeypatch, mock.patch by object and by dotted name, nested either way, and a
    delete: each lands on the owner and is undone without a trace on the facade."""
    original = vars(owner)[name]
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(facade, name, "stub")
        assert vars(owner)[name] == "stub" and getattr(facade, name) == "stub"
        assert name not in vars(facade)
    assert vars(owner)[name] is original and name not in vars(facade)
    with mock.patch.object(facade, name, "outer"):
        with mock.patch(f"{facade.__name__}.{name}", "inner"):
            assert vars(owner)[name] == "inner"
        assert vars(owner)[name] == "outer"
    assert vars(owner)[name] is original
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(facade, name, "mp")
        with mock.patch.object(facade, name, "mock"):
            assert vars(owner)[name] == "mock"
        assert vars(owner)[name] == "mp"
    with mock.patch.object(facade, name, "mock"):
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(facade, name, "mp")
            assert vars(owner)[name] == "mp"
        assert vars(owner)[name] == "mock"
    assert vars(owner)[name] is original
    with pytest.MonkeyPatch.context() as patched:
        patched.delattr(facade, name)
        assert name not in vars(owner) and not hasattr(facade, name)
    assert vars(owner)[name] is original and name not in vars(facade)


def check_loaded_owner_needs_no_import(facade: ModuleType, owner: ModuleType, name: str) -> None:
    """A loaded owner answers from ``sys.modules``: reading, writing and undoing a
    forwarded name never calls the import system, so a test that stubs
    ``importlib.import_module`` for its own subject cannot reach these reads."""
    original = vars(owner)[name]
    with mock.patch.object(importlib, "import_module", side_effect=AssertionError("imported")):
        assert getattr(facade, name) is original
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(facade, name, "stub")
            assert getattr(facade, name) == "stub"
        with mock.patch.object(facade, name, "stub"):
            assert vars(owner)[name] == "stub"
    assert vars(owner)[name] is original


def check_a_purged_owner_is_imported_again(facade: ModuleType, name: str) -> None:
    """The miss path: an owner absent from ``sys.modules`` is imported afresh, and the
    facade reads the fresh copy rather than one it kept. *name* must be an object each
    import builds anew, such as a function, or the two copies cannot be told apart."""
    dotted = facade._EXPORTS[name]
    held = sys.modules[dotted]
    try:
        del sys.modules[dotted]
        fresh = getattr(facade, name)
        assert dotted in sys.modules and sys.modules[dotted] is not held
        assert fresh is vars(sys.modules[dotted])[name]
        assert fresh is not vars(held)[name]
    finally:
        sys.modules[dotted] = held
        parent, _, leaf = dotted.rpartition(".")
        setattr(sys.modules[parent], leaf, held)


def check_owners_load_with_the_facade(facade: ModuleType) -> None:
    """Importing the facade imports every owner it forwards to, before anything reads a
    forwarded name. That is what lets ``_owner`` read a loaded owner from ``sys.modules``
    without waiting on an import lock: no reader can find an owner half-built, because
    the forwarding is installed only once the facade's body -- and so every owner's --
    has finished. Measured in a child process, where nothing else has imported them."""
    owners = sorted(set(facade._EXPORTS.values()))
    code = (
        "import sys\n"
        f"import {facade.__name__}\n"
        f"owners = {owners!r}\n"
        "print(sorted(o for o in owners if o not in sys.modules))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        cwd=str(_REPO),
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "[]"


def check_unknown_name(facade: ModuleType) -> None:
    with pytest.raises(AttributeError, match="no_such_name"):
        facade.no_such_name  # noqa: B018
    assert getattr(facade, "no_such_name", None) is None


def star_import(tmp_path: Path, facade: ModuleType) -> dict[str, object]:
    """Run ``from <facade> import *`` for real, in a probe module the import machinery
    loads from a file, and return what it bound."""
    stem = f"{facade.__name__.rpartition('.')[2]}_star_probe"
    probe = tmp_path / f"{stem}.py"
    probe.write_text(f"from {facade.__name__} import *  # noqa: F401,F403\n", encoding="utf-8")
    spec = importlib.util.spec_from_file_location(stem, probe)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return vars(module)


def check_star_import(tmp_path: Path, facade: ModuleType, machinery: frozenset[str]) -> None:
    """The star import binds every public name the module held before the split: the
    names it binds now plus the forwarded ones, and nothing private. The bindings only
    the forwarding needs -- *machinery*, the owner modules among them -- stay out, so a
    star importer gains no name the split introduced."""
    assert facade._NOT_EXPORTED == machinery
    assert machinery <= set(vars(facade))
    bound = star_import(tmp_path, facade)
    public = {n for n in set(vars(facade)) | set(facade._EXPORTS) if not n.startswith("_")}
    assert facade.__all__ == sorted(public - machinery)
    assert public - machinery <= set(bound)
    assert not machinery & set(bound)
    for name in facade.__all__:
        assert bound[name] is getattr(facade, name)


def tree_of(module: ModuleType) -> ast.Module:
    return ast.parse(Path(module.__file__ or "").read_text(encoding="utf-8"))


def bare_loads(tree: ast.Module, forwarded: set[str]) -> list[tuple[int, str]]:
    """Every bare read of a forwarded name outside an import statement."""
    import_lines = {
        line
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for line in range(node.lineno, (node.end_lineno or node.lineno) + 1)
    }
    return sorted(
        (node.lineno, node.id)
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id in forwarded
        and node.lineno not in import_lines
    )


def check_static_shape(facade: ModuleType, owners: dict[str, ModuleType]) -> None:
    """No bare read of a forwarded name anywhere in the facade, ``__getattr__`` hidden
    from type checkers, and the ``TYPE_CHECKING`` imports exactly the forwarded names."""
    tree = tree_of(facade)
    assert bare_loads(tree, set(facade._EXPORTS)) == []
    module_level = [
        *tree.body,
        *(child for n in tree.body if isinstance(n, ast.If) for child in n.body + n.orelse),
    ]
    defined = [
        n for n in module_level if isinstance(n, ast.FunctionDef) and n.name == "__getattr__"
    ]
    hidden = [
        n
        for n in tree.body
        if isinstance(n, ast.If)
        and ast.unparse(n.test) == "not TYPE_CHECKING"
        and any(child in n.body for child in defined)
    ]
    assert len(defined) == 1 and len(hidden) == 1
    assert callable(vars(facade).get("__getattr__"))  # still the resolver at run time
    blocks = [
        n for n in tree.body if isinstance(n, ast.If) and ast.unparse(n.test) == "TYPE_CHECKING"
    ]
    typed: dict[str, str] = {}
    for block in blocks:
        for node in ast.walk(block):
            if isinstance(node, ast.ImportFrom) and node.module in owners:
                for alias in node.names:
                    typed[alias.asname or alias.name] = node.module
    assert typed == facade._EXPORTS


_MODULE_ITSELF = "<module>"


def facade_imports(
    tree: ast.Module, package: str, facades: set[str]
) -> list[tuple[str, str, tuple[str, ...]]]:
    """``(enclosing function, facade, names)`` for each import of a facade in *tree*;
    ``"<module>"`` for one at module level. An import of the facade module itself names
    ``("<module>",)``. A ``TYPE_CHECKING`` import is not one."""
    found: list[tuple[str, str, tuple[str, ...]]] = []

    def visit(node: ast.AST, where: str) -> None:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            where = node.name if where == "<module>" else f"{where}.{node.name}"
        if isinstance(node, ast.ImportFrom):
            spelled = "." * node.level + (node.module or "")
            module = importlib.util.resolve_name(spelled, package) if node.level else spelled
            if module in facades:
                found.append((where, module, tuple(a.name for a in node.names)))
            found.extend(
                (where, f"{module}.{a.name}", (_MODULE_ITSELF,))
                for a in node.names
                if f"{module}.{a.name}" in facades
            )
        if isinstance(node, ast.Import):
            found.extend(
                (where, a.name, (_MODULE_ITSELF,)) for a in node.names if a.name in facades
            )
        for child in ast.iter_child_nodes(node):
            visit(child, where)

    visit(tree, "<module>")
    return found


def owner_seams(
    owners: dict[str, ModuleType], facade: ModuleType
) -> dict[tuple[str, str], tuple[str, tuple[str, ...]]]:
    """``(owner, function) -> (facade, names)`` for every import of *facade* in its
    owners, the names of several imports in one function merged in sorted order."""
    merged: dict[tuple[str, str], set[str]] = {}
    for owner in owners.values():
        rows = facade_imports(tree_of(owner), owner.__name__.rpartition(".")[0], _FACADE_NAMES)
        for where, imported, names in rows:
            if imported != facade.__name__:
                continue
            assert where != "<module>", f"{owner.__name__} imports its facade at module level"
            merged.setdefault((owner.__name__, where), set()).update(names)
    return {site: (facade.__name__, tuple(sorted(names))) for site, names in merged.items()}


#: The facades by layer: an owner may import a LOWER facade at module level, as any other
#: module does (the mount sweep probes pids through ``platform_compat``), but never its own
#: or a higher one.
_LAYER = {"kiro_crew.platform_compat": 0, "kiro_crew.sandbox": 1}


def check_owner_dependency_direction(owners: dict[str, ModuleType], facade: ModuleType) -> None:
    """Owners are the facade's bottom layer: none imports its own facade, or a higher one,
    at module level, and none imports another owner."""
    for owner in owners.values():
        tree = tree_of(owner)
        rows = facade_imports(tree, owner.__name__.rpartition(".")[0], _FACADE_NAMES)
        upward = [
            row
            for row in rows
            if row[0] == "<module>" and _LAYER[row[1]] >= _LAYER[facade.__name__]
        ]
        assert upward == [], (owner.__name__, upward)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module not in _ALL_OWNERS, (owner.__name__, node.module)
                assert not any(
                    f"{node.module}.{a.name}" in _ALL_OWNERS for a in node.names
                ), owner.__name__


def check_seams_are_facade_bindings(
    facade: ModuleType,
    moved: dict[str, tuple[str, ...]],
    seams: dict[tuple[str, str], tuple[str, tuple[str, ...]]],
    platform_bound: frozenset[str] = frozenset(),
) -> None:
    """Each name an owner imports from its facade is the facade's own binding -- a
    forwarded one would be read back from the owner -- and the reader's code came from
    that facade. *platform_bound* names are bound only on the platforms that have them."""
    for (owner, function), (name_of_facade, names) in seams.items():
        assert name_of_facade == facade.__name__
        assert function.split(".", 1)[0] in moved[owner]
        for name in names:
            assert name not in facade._EXPORTS, name
            assert name in vars(facade) or name in platform_bound, name
            assert name not in vars(_ALL_OWNERS[owner]), name


def check_owner_logger(facade: ModuleType, owners: dict[str, ModuleType]) -> None:
    """An owner that logs does so under the facade's name, which operator log filters,
    level settings and the tests' ``caplog`` scopes key on."""
    for owner in owners.values():
        if "logger" in vars(owner):
            assert owner.logger is logging.getLogger(facade.__name__) is facade.logger


# --------------------------------------------------------------------------- #
# The patch census: every facade patch in the test trees, read once for both facades.
# --------------------------------------------------------------------------- #

_FACADES: dict[str, ModuleType] = dict(create_guard._FACADES)
_FACADE_NAMES = set(_FACADES)
_ALL_OWNERS: dict[str, ModuleType] = {}


def register_owners(owners: dict[str, ModuleType]) -> None:
    _ALL_OWNERS.update(owners)


register_owners(_OWNERS)
register_owners(
    {
        name: importlib.import_module(name)
        for name in ("kiro_crew.platform_lock_compat", "kiro_crew.platform_owner_compat")
    }
)

_SETTERS = frozenset({"setattr", "delattr"})
_DYNAMIC = create_guard._DYNAMIC
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)

#: Every module-level name either facade holds or forwards. A patch of a name neither
#: has cannot be a facade patch: ``monkeypatch.setattr`` and ``mock.patch`` refuse a
#: missing name unless told to create it, and every moved name is one of these.
_FACADE_ATTRIBUTES = frozenset().union(
    *(set(vars(module)) | set(module._EXPORTS) for module in _FACADES.values())
)

#: What the file's names mean at one patch site: the scope, and the names whose value
#: the file cannot say there -- a parameter, or a loop or comprehension target that
#: takes something other than a literal's elements.
_Binding = tuple[create_guard._Scope, frozenset[str]]


def _reads(node: ast.AST | None, unknown: frozenset[str]) -> bool:
    return node is not None and any(
        isinstance(n, ast.Name) and n.id in unknown for n in ast.walk(node)
    )


def _bind(binding: _Binding, name: str, value: ast.expr, source: _Binding) -> _Binding:
    """*binding* with *name* bound to what *value* names in *source*, or unknown."""
    scope, unknown = binding
    module = source[0].module(value) if not isinstance(value, ast.Constant) else None
    text = source[0].text(value)
    view = scope.without(frozenset({name}))
    if _reads(value, source[1]) or (module is None and text is None):
        return view, unknown | {name}
    view = copy.copy(view)
    view.names = {**view.names}
    view.strings = {**view.strings}
    view._views = {}
    if module is not None:
        view.names[name] = module
    if text is not None:
        view.strings[name] = text
    return view, unknown - {name}


def _unknown(binding: _Binding, names: frozenset[str]) -> _Binding:
    return binding[0].without(names), binding[1] | names


def _iterate(bindings: list[_Binding], target: ast.expr, iterable: ast.expr) -> list[_Binding]:
    """The bindings a loop body sees: one per element of a literal tuple, list or set,
    unpacked into a tuple target of the same length; the target unknown otherwise."""
    names = frozenset(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
    expanded: list[_Binding] = []
    for binding in bindings:
        literal = isinstance(iterable, (ast.Tuple, ast.List, ast.Set))
        if not literal or _reads(iterable, binding[1]):
            expanded.append(_unknown(binding, names))
            continue
        for element in iterable.elts:
            if isinstance(target, ast.Name):
                expanded.append(_bind(binding, target.id, element, binding))
            elif (
                isinstance(target, (ast.Tuple, ast.List))
                and isinstance(element, (ast.Tuple, ast.List))
                and len(element.elts) == len(target.elts)
                and all(isinstance(part, ast.Name) for part in target.elts)
            ):
                unpacked = binding
                for part, value in zip(target.elts, element.elts):
                    assert isinstance(part, ast.Name)
                    unpacked = _bind(unpacked, part.id, value, binding)
                expanded.append(unpacked)
            else:
                expanded.append(_unknown(binding, names))
    return expanded


def _parameters(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> frozenset[str]:
    # pytest-mock's ``mocker`` stays the patch provider it is everywhere.
    return create_guard._parameters(node) - {"mocker"}


def _patches_at(call: ast.Call, binding: _Binding) -> set[tuple[str, str]]:
    """``(facade, name)`` for each facade attribute *call* patches under *binding*: the
    name ``<dynamic>`` when the file does not spell it, and both ``<dynamic>`` when the
    patched object is one the site cannot name but a facade might be."""
    scope, unknown = binding
    form = create_guard._PATCH_FORMS.get(scope.module(call.func) or "")
    if form is None:
        func = call.func
        if not (isinstance(func, ast.Attribute) and func.attr in _SETTERS and call.args):
            return set()
        form = "setattr"
    target = create_guard._argument(call, 0, "target")
    if form == "setattr":
        # ``setattr("pkg.mod.name", value)`` / ``delattr("pkg.mod.name")`` spell the
        # target as one dotted string; the object form passes the name second.
        assert isinstance(call.func, ast.Attribute)
        dotted = len(call.args) == (2 if call.func.attr == "setattr" else 1) and not any(
            k.arg in ("value", "name") for k in call.keywords
        )
        if dotted:
            text = scope.text(target) if target is not None else None
            if text is not None and not _reads(target, unknown):
                facade, _, attribute = text.rpartition(".")
                facade = create_guard._facade_spelling(facade)
                return {(facade, attribute)} if facade in _FACADES else set()
            return {(_DYNAMIC, _DYNAMIC)} if _reads(target, unknown) else set()
        name_node: ast.expr | None = call.args[1]
    else:
        name_node = create_guard._argument(call, 1, "attribute") if form == "object" else None
    name = scope.text(name_node) if name_node is not None else None
    if target is not None and _reads(target, unknown):
        if form == "multiple":
            spelled = {k.arg for k in call.keywords if k.arg} - create_guard._MULTIPLE_PARAMETERS
            possible = not spelled or bool(spelled & _FACADE_ATTRIBUTES)
        else:
            possible = form == "patch" or name is None or name in _FACADE_ATTRIBUTES
        return {(_DYNAMIC, _DYNAMIC)} if possible else set()
    if form != "setattr":
        return {
            (facade, _DYNAMIC if _reads(name_node, unknown) else patched)
            for facade, patched in create_guard._patched_names(call, form, scope)
            if facade in _FACADES
        }
    module = scope.module(target) if target is not None else None
    if module in _FACADES and name_node is not None:
        return {(module, _DYNAMIC if name is None or _reads(name_node, unknown) else name)}
    return set()


def monkeypatch_targets(tree: ast.Module) -> list[tuple[int, str, str, str]]:
    """``(line, enclosing function, facade, name)`` for each ``monkeypatch.setattr`` /
    ``delattr`` and ``mock.patch*`` of a facade attribute.

    A patch runs once per binding of every loop and comprehension around it: a target
    over a literal takes each element, a tuple target unpacks each element. A patch
    whose object reads a name the site cannot resolve -- a parameter, or a target over
    anything else -- is ``(<dynamic>, <dynamic>)`` when a facade could be that object,
    and a facade patch whose name the site cannot spell names ``<dynamic>``. A patch of
    an object the file names as something else -- an instance, a class -- is none."""
    found: list[tuple[int, str, str, str]] = []

    def visit(node: ast.AST, bindings: list[_Binding], where: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            outer: list[ast.AST] = [*getattr(node, "decorator_list", []), *node.args.defaults]
            outer += [d for d in node.args.kw_defaults if d is not None]
            for child in outer:
                visit(child, bindings, where)
            inner = [_unknown(binding, _parameters(node)) for binding in bindings]
            if not isinstance(node, ast.Lambda):
                where = node.name if where == "<module>" else f"{where}.{node.name}"
            for child in node.body if isinstance(node.body, list) else [node.body]:
                visit(child, inner, where)
            return
        if isinstance(node, ast.ClassDef):
            for child in node.decorator_list:
                visit(child, bindings, where)
            where = node.name if where == "<module>" else f"{where}.{node.name}"
            for child in node.body:
                visit(child, bindings, where)
            return
        if isinstance(node, (ast.For, ast.AsyncFor)):
            visit(node.iter, bindings, where)
            for child in node.body:
                visit(child, _iterate(bindings, node.target, node.iter), where)
            for child in node.orelse:
                visit(child, bindings, where)
            return
        if isinstance(node, _COMPREHENSIONS):
            current = bindings
            for generator in node.generators:
                visit(generator.iter, current, where)
                current = _iterate(current, generator.target, generator.iter)
                for condition in generator.ifs:
                    visit(condition, current, where)
            parts = [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
            for part in parts:
                visit(part, current, where)
            return
        if isinstance(node, ast.Call):
            hits: set[tuple[str, str]] = set()
            for binding in bindings:
                hits |= _patches_at(node, binding)
            found.extend((node.lineno, where, facade, name) for facade, name in sorted(hits))
        for child in ast.iter_child_nodes(node):
            visit(child, bindings, where)

    visit(tree, [(create_guard._Scope(tree), frozenset())], "<module>")
    return found


@functools.lru_cache(maxsize=1)
def facade_patches() -> tuple[tuple[str, int, str, str, str], ...]:
    """``(file, line, enclosing function, facade, name)`` for every facade patch under
    ``test/``, each ``src/**/tests`` and the root conftest, read once for both facades."""
    roots = [_REPO / "test", *sorted((_REPO / "src" / "kiro_crew").rglob("tests"))]
    paths = [_REPO / "conftest.py", *(p for root in roots for p in sorted(root.rglob("*.py")))]
    patches: list[tuple[str, int, str, str, str]] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        if not create_guard._worth_parsing(text):
            continue
        relative = path.relative_to(_REPO).as_posix()
        patches.extend(
            (relative, line, where, facade, name)
            for line, where, facade, name in monkeypatch_targets(ast.parse(text))
        )
    return tuple(patches)


def check_shared_bindings(
    facade: ModuleType,
    seams: dict[tuple[str, str], tuple[str, tuple[str, ...]]],
    expected: frozenset[str],
) -> None:
    """A name both the facade and an owner bind takes a patch through the facade in the
    facade's namespace only; each such name a test patches is listed, so a new one cannot
    miss an owner's reader silently. A seam name is not one: the owner imports the
    facade's binding when it runs."""
    seam_names = {name for _facade, names in seams.values() for name in names}
    patches = [row for row in facade_patches() if row[3] == facade.__name__]
    assert len(patches) > 50, f"the scan saw only {len(patches)} patches of {facade.__name__}"
    shared: set[str] = set()
    for _path, _line, _where, _facade, name in patches:
        if name == _DYNAMIC or name in seam_names:
            continue
        if name in facade._EXPORTS or name not in vars(facade):
            continue
        if any(name in vars(owner) for owner in _ALL_OWNERS.values()):
            shared.add(name)
    assert shared == set(expected)


# --------------------------------------------------------------------------- #
# kiro_crew.sandbox
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("owner", "name"), _moved_rows(_MOVED))
def test_every_moved_name_is_defined_by_its_owner_and_read_through_the_facade(
    owner: str, name: str
) -> None:
    check_moved_name(sandbox, _OWNERS, owner, name)


def test_every_moved_name_is_forwarded_and_none_is_bound_here() -> None:
    check_forwarded_and_bound_are_disjoint(sandbox, _MOVED, frozenset())


def test_the_forwarding_table_names_each_owner_by_its_dotted_name() -> None:
    check_forwarding_table(sandbox, _OWNERS)


@pytest.mark.parametrize(
    ("owner", "name"),
    [
        (sandbox_seatbelt, "_SEATBELT_PROFILE"),
        (sandbox_mount_sweep, "_mount_source_candidate_roots"),
        (sandbox_mount_sweep, "_PIN_SCAN_MAX_PASSES"),
    ],
)
def test_a_patch_through_the_facade_round_trips_on_the_owner(owner: ModuleType, name: str) -> None:
    check_round_trips(sandbox, owner, name)


def test_a_write_of_a_facade_name_stays_on_the_facade(monkeypatch: pytest.MonkeyPatch) -> None:
    """A name the facade binds itself -- a table or host fact the live plan host reads,
    a module it imports -- is an ordinary attribute write, which that code then reads."""
    monkeypatch.setattr(sandbox, "_ssh_supports_accept_new", "stub")
    assert vars(sandbox)["_ssh_supports_accept_new"] == "stub"
    assert "_ssh_supports_accept_new" not in vars(sandbox_launcher)
    monkeypatch.setattr(sandbox, "_STANDARD_DIRS", ["stub"])
    assert vars(sandbox)["_STANDARD_DIRS"] == ["stub"]


def test_a_loaded_owner_is_read_without_the_import_system() -> None:
    check_loaded_owner_needs_no_import(sandbox, sandbox_mount_sweep, "_overflow_uid")


def test_a_purged_owner_is_imported_again_and_read_fresh() -> None:
    check_a_purged_owner_is_imported_again(sandbox, "_task_uid")


def test_every_owner_is_loaded_with_the_facade() -> None:
    check_owners_load_with_the_facade(sandbox)


def test_an_unknown_name_is_an_attribute_error() -> None:
    check_unknown_name(sandbox)


def test_the_star_import_binds_the_public_names(tmp_path: Path) -> None:
    check_star_import(
        tmp_path,
        sandbox,
        frozenset(
            {
                "ModuleType",
                "importlib",
                "sandbox_launcher",
                "sandbox_mount_sweep",
                "sandbox_plan",
                "sandbox_seatbelt",
            }
        ),
    )


def test_the_static_shape_of_the_facade() -> None:
    check_static_shape(sandbox, _OWNERS)


def test_the_bare_global_scan_can_fail() -> None:
    tree = ast.parse("from x import y\n\ndef f():\n    return _overflow_uid\n")
    assert bare_loads(tree, {"_overflow_uid"}) == [(4, "_overflow_uid")]
    assert (
        bare_loads(ast.parse("from x import (\n    _overflow_uid,\n)\n"), {"_overflow_uid"}) == []
    )


def test_the_facade_import_scan_reads_every_spelling() -> None:
    source = (
        "from typing import TYPE_CHECKING\n"
        "from kiro_crew import sandbox\n"
        "import kiro_crew.platform_compat\n"
        "from . import sandbox as sb\n"
        "if TYPE_CHECKING:\n"
        "    from kiro_crew.sandbox import _STANDARD_DIRS\n"
        "def f():\n"
        "    from kiro_crew.sandbox import _STANDARD_DIRS, _CC_FILES\n"
        "class C:\n"
        "    def m(self):\n"
        "        from kiro_crew.platform_compat import IS_POSIX\n"
    )
    assert facade_imports(ast.parse(source), "kiro_crew", _FACADE_NAMES) == [
        ("<module>", "kiro_crew.sandbox", ("<module>",)),
        ("<module>", "kiro_crew.platform_compat", ("<module>",)),
        ("<module>", "kiro_crew.sandbox", ("<module>",)),
        ("f", "kiro_crew.sandbox", ("_STANDARD_DIRS", "_CC_FILES")),
        ("C.m", "kiro_crew.platform_compat", ("IS_POSIX",)),
    ]


def test_each_owner_reads_the_facade_only_for_the_listed_seams() -> None:
    assert owner_seams(_OWNERS, sandbox) == _SEAM_IMPORTS


def test_each_seam_is_a_binding_of_the_facade() -> None:
    check_seams_are_facade_bindings(sandbox, _MOVED, _SEAM_IMPORTS)


def test_the_owners_depend_on_nothing_above_them() -> None:
    check_owner_dependency_direction(_OWNERS, sandbox)


def test_the_owners_log_under_the_sandbox_name() -> None:
    check_owner_logger(sandbox, _OWNERS)


def _imported_modules(module: ModuleType) -> set[str]:
    """Every module *module* imports, at any depth of its body."""
    found: set[str] = set()
    for node in ast.walk(tree_of(module)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module)
    return found


#: The layers under the facade: the plan and the launcher program import the standard
#: library and nothing else, and the two renderers import the plan and nothing else of
#: the package. So the plan stays pure, the program stays runnable as a stdlib script,
#: and neither renderer can reach back into ``kiro_crew.sandbox`` for a rule.
_LAYERED: dict[str, frozenset[str]] = {
    "kiro_crew.sandbox_plan": frozenset(),
    "kiro_crew.sandbox_launcher_program": frozenset(),
    "kiro_crew.sandbox_launcher": frozenset({"kiro_crew.sandbox_plan"}),
    "kiro_crew.sandbox_seatbelt": frozenset({"kiro_crew.sandbox_plan"}),
}


@pytest.mark.parametrize("name", sorted(_LAYERED))
def test_the_plan_layers_import_nothing_above_them(name: str) -> None:
    imported = _imported_modules(importlib.import_module(name))
    package = {m for m in imported if m.split(".")[0] == "kiro_crew"}
    assert package == _LAYERED[name], name
    foreign = {
        m
        for m in imported - package
        if m.split(".")[0] not in sys.stdlib_module_names and m != "__future__"
    }
    assert foreign == set(), (name, foreign)


def test_the_layer_check_flags_an_import_of_the_facade(tmp_path: Path) -> None:
    probe = tmp_path / "probe_layer.py"
    probe.write_text("import os\nfrom kiro_crew import sandbox\nimport yaml\n", encoding="utf-8")
    module = ModuleType("probe_layer")
    module.__file__ = str(probe)
    assert _imported_modules(module) == {"os", "kiro_crew", "yaml"}


#: The path rules that moved to ``kiro_crew.sandbox_plan`` and are still bound on the
#: facade under their old names, for the callers and tests that read them there. They
#: are compatibility ALIASES, not forwards: a patch of one does not reach the planner,
#: which calls its own functions -- patch ``sandbox_plan.<rule>`` instead. A moved rule
#: nothing outside the plan reads is not bound here at all.
_PLAN_ALIASES: dict[str, str] = {
    "_is_policy_cache_dir": "is_policy_cache_dir",
    "_hidden_path_contains_visible_path": "hidden_path_contains_visible_path",
    "_path_within": "path_within",
    "_agent_scrub_prefixes": "scrub_prefixes",
}


@pytest.mark.parametrize(("alias", "rule"), sorted(_PLAN_ALIASES.items()))
def test_a_plan_rule_kept_on_the_facade_is_an_alias_not_a_forward(alias: str, rule: str) -> None:
    assert vars(sandbox)[alias] is getattr(sandbox_plan, rule)
    assert alias not in sandbox._EXPORTS


def test_every_plan_object_the_facade_binds_is_a_listed_alias() -> None:
    plan_objects = {id(v) for v in vars(sandbox_plan).values() if callable(v)}
    bound = {
        name
        for name, value in vars(sandbox).items()
        if name.startswith("_")
        and not name.startswith("__")
        and id(value) in plan_objects
        and getattr(value, "__module__", None) == sandbox_plan.__name__
    }
    assert bound == set(_PLAN_ALIASES)


#: Names a test patches through ``kiro_crew.sandbox`` that the facade AND an owner both
#: bind, each importing its own, so the patch reaches only the facade's code. ``time`` is
#: rebound for the agents-slice throttling probe, which stays here; the mount sweep's own
#: clock is never faked through the facade. A new entry is a decision that the patch
#: needs no forwarding.
_SANDBOX_SHARED_BINDINGS_PATCHED: frozenset[str] = frozenset({"time"})


def test_a_patched_name_bound_by_the_facade_and_an_owner_is_a_listed_one() -> None:
    check_shared_bindings(sandbox, _SEAM_IMPORTS, _SANDBOX_SHARED_BINDINGS_PATCHED)


def test_the_patch_scan_reads_every_binding_of_a_patch_site() -> None:
    source = (
        "from unittest import mock\n"
        "from kiro_crew import platform_compat, sandbox\n"
        "def test_x(monkeypatch, attr):\n"
        '    monkeypatch.setattr(sandbox, "_overflow_uid", 1)\n'
        '    monkeypatch.setattr("kiro_crew.platform_compat.file_lock", 1)\n'
        '    monkeypatch.setattr(other, "_overflow_uid", 1)\n'
        "    for module in (platform_compat, sandbox, other):\n"
        '        monkeypatch.setattr(module, "IS_WINDOWS", 1)\n'
        "    for module in MODULES:\n"
        '        mock.patch.object(module, "sys")\n'
        '    for name in ("sys", "time"):\n'
        "        monkeypatch.setattr(platform_compat, name, 1)\n"
        "    monkeypatch.setattr(sandbox, attr, 1)\n"
        '    monkeypatch.setattr(discover.platform_compat, "file_lock", 1)\n'
        "def _helper(monkeypatch, module):\n"
        '    monkeypatch.setattr(module, "IS_WINDOWS", 1)\n'
        "from kiro_crew.dashboard.handlers import discover\n"
    )
    assert monkeypatch_targets(ast.parse(source)) == [
        (4, "test_x", "kiro_crew.sandbox", "_overflow_uid"),
        (5, "test_x", "kiro_crew.platform_compat", "file_lock"),
        (8, "test_x", "<dynamic>", "<dynamic>"),
        (8, "test_x", "kiro_crew.platform_compat", "IS_WINDOWS"),
        (8, "test_x", "kiro_crew.sandbox", "IS_WINDOWS"),
        (10, "test_x", "<dynamic>", "<dynamic>"),
        (12, "test_x", "kiro_crew.platform_compat", "sys"),
        (12, "test_x", "kiro_crew.platform_compat", "time"),
        (13, "test_x", "kiro_crew.sandbox", "<dynamic>"),
        (14, "test_x", "kiro_crew.platform_compat", "file_lock"),
        (16, "_helper", "<dynamic>", "<dynamic>"),
    ]


# --------------------------------------------------------------------------- #
# A patch through the facade reaches the moved code's own reader, and back.
# --------------------------------------------------------------------------- #


@_POSIX_ONLY
def test_a_patched_root_list_reaches_the_keyed_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "_mount_source_candidate_roots", _raiser("roots"))
    with pytest.raises(_Reached, match="roots"):
        sandbox._cleanup_stale_sandbox_mount_sources()


@_POSIX_ONLY
def test_a_patched_pin_scan_reaches_the_legacy_scan(
    monkeypatch: pytest.MonkeyPatch, sandbox_sweep_original: Callable[[str], Callable[..., object]]
) -> None:
    """The test floor stubs ``_bound_source_basenames`` itself, so the real one -- which
    calls the pin scan in its own module -- is read from the floor's stash."""
    bound_source_basenames = sandbox_sweep_original("_bound_source_basenames")
    assert bound_source_basenames.__module__ == sandbox_mount_sweep.__name__
    monkeypatch.setattr(sandbox, "_mount_pinned_source_names", _raiser("pins"))
    with pytest.raises(_Reached, match="pins"):
        bound_source_basenames("/nonexistent-proc-root")


@_POSIX_ONLY
def test_a_patched_tmpfs_root_list_reaches_the_legacy_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "_launcher_tmpfs_roots", _raiser("tmpfs"))
    with pytest.raises(_Reached, match="tmpfs"):
        sandbox._cleanup_legacy_mount_source_residue(tmp_path)


@_POSIX_ONLY
def test_the_facade_s_cleanup_pass_reaches_a_patched_sweep_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``cleanup_stale_sandbox_profiles`` stays here and runs the moved keyed sweep,
    which reads the patched root list from its own module."""
    monkeypatch.setattr(sandbox, "_mount_source_candidate_roots", _raiser("keyed"))
    with pytest.raises(_Reached, match="keyed"):
        sandbox.cleanup_stale_sandbox_profiles(data_home=tmp_path, legacy_dir=str(tmp_path))


@_POSIX_ONLY
def test_a_patched_launcher_builder_reaches_namespace_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(sandbox, "_build_launcher_script", _raiser("launcher"))
    with pytest.raises(_Reached, match="launcher"):
        sandbox.namespace_argv(["/bin/true"], "standard")


@_POSIX_ONLY
def test_a_patched_profile_builder_reaches_sandbox_exec_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(sandbox, "_build_seatbelt_profile", _raiser("profile"))
    with pytest.raises(_Reached, match="profile"):
        sandbox.sandbox_exec_argv(["/bin/true"], "standard")


@pytest.mark.parametrize(
    ("owner", "renderer", "builder"),
    [
        pytest.param(
            sandbox_launcher,
            "render_namespace_launcher",
            "_build_launcher_script",
            marks=_POSIX_ONLY,
            id="namespace",
        ),
        pytest.param(
            sandbox_seatbelt, "render_seatbelt_profile", "_build_seatbelt_profile", id="seatbelt"
        ),
    ],
)
def test_each_builder_renders_through_its_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    owner: ModuleType,
    renderer: str,
    builder: str,
) -> None:
    """The builders stay here and plan the spawn; the owner renders it, read from the
    owner at call time so a patch of the owner's renderer reaches the builder."""
    monkeypatch.setattr(sandbox, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(sandbox, "_ssh_supports_accept_new", lambda: True)
    monkeypatch.setattr(owner, renderer, _raiser(renderer))
    with pytest.raises(_Reached, match=renderer):
        getattr(sandbox, builder)("standard")


def test_every_moved_name_is_forwarded_so_no_patch_needs_resolving() -> None:
    """Why there is no census of the patch sites a scan cannot resolve: every moved name
    is forwarded, so a patch through the facade lands on the owner whatever spelling put
    it there, and a name the facade still binds is one no owner reads."""
    names = {name for group in _MOVED.values() for name in group}
    assert names == set(sandbox._EXPORTS)


def test_the_sweep_s_pin_scan_coverage_class_is_one_class() -> None:
    """``_PinScanCoverage`` is the owner's class, whichever path a caller reads it by, so
    an ``isinstance`` check on a coverage object built through either path holds."""
    coverage = sandbox._PinScanCoverage()
    assert isinstance(coverage, sandbox_mount_sweep._PinScanCoverage)
    assert sandbox._PinScanCoverage is sandbox_mount_sweep._PinScanCoverage


def test_the_forwarding_is_live_for_any_module_that_imported_a_name_early() -> None:
    """``cli_doctor`` imports ``_MOUNT_SOURCE_PREFIX`` from the facade at load: the
    import resolves through ``__getattr__`` to the owner's object."""
    from kiro_crew import cli_doctor

    assert cli_doctor._MOUNT_SOURCE_PREFIX is sandbox_mount_sweep._MOUNT_SOURCE_PREFIX
