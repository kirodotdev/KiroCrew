"""The path floor facade keeps the patch seams the one module had.

``kiro_crew.security.paths`` is a mirror facade (:mod:`kiro_crew._facade_mirror`) over the
owners ``paths_lists``, ``paths_resolver`` and ``paths_targets``; the public path gates,
their matchers and the command-line orchestrator stay in the facade itself. The moved code
reads its names as plain module globals, so a patch reaches it only because the mirror
writes the patch into every module holding the name. Two spellings reach the facade: a
write on ``kiro_crew.security.paths`` itself, and a write on the package
``kiro_crew.security`` of a path floor name its ``_EXPORTS`` table lists (each row naming
``paths``), which the package forwards here. Every other owner-defined name is reached
through ``kiro_crew.security.paths`` only.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules, and
its corpus counts patches spelled through the facade's own name only. These tests pin what
is particular to this facade: every name the test trees patch through either spelling
reaches every module holding it; the owners are exactly the ``paths_*.py`` siblings,
loaded lowest layer first, none importing the facade by any spelling; every member logs
under the facade's logger; each name served from outside the family stays visible to
type checkers; and the stall exception keeps the facade's spelling in tracebacks.
"""

from __future__ import annotations

import ast
import functools
import importlib
import re
import sys
import traceback
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus

import kiro_crew.security as security
from kiro_crew import _facade_mirror
from kiro_crew.security import paths

pytestmark = pytest.mark.xdist_group(name="tree_scan_paths_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = (
    "known_seams_by_target",
    "spelling_cases",
    "corpus_gate",
    "test_tree_walk",
    "reach_problems",
    "reach_rule_can_fail",
)

FACADE = "kiro_crew.security.paths"
PACKAGE = "kiro_crew.security"
LEAF = "paths"
SECURITY_DIR = Path(paths.__file__).resolve().parent
_THIS_FILE = Path(__file__).resolve()
#: Text every file that reaches the facade by name holds: a dotted spelling (an import,
#: a string target, a ``sys.modules`` key, an attribute chain off the package) or an import
#: of it from the package, on one line or parenthesized.
_SPELLS_THE_FACADE = re.compile(
    rf"security\.{LEAF}\b|security import[^\n]*\b{LEAF}\b|security import \("
)

#: Names the test trees are known to patch, by the spelling they use: the resolver's clock,
#: platform flag and executor, a child-fault latch the resolver rebinds, the target cache, a
#: matcher and a gate the facade defines, the module-valued ``time``, and the names the
#: package table routes here. The scan must find all of them, so a scanner that stops
#: matching goes red instead of passing over nothing.
_KNOWN_SEAMS: dict[str, frozenset[str]] = {
    FACADE: frozenset(
        {
            "_resolved_spellings",
            "_run_resolution_bounded",
            "_path_resolve_clock",
            "_ON_WINDOWS",
            "path_resolve_executor",
            "_child_fallback_warned",
            "_home_targets_cache",
            "_path_in_home_dirs",
            "is_sensitive_path",
            "time",
        }
    ),
    PACKAGE: frozenset(
        {"_path_resolve_degraded", "_SENSITIVE_HOME_DIRS", "_HOME_TARGETS_TTL_COST_RATIO"}
    ),
}


def _index() -> _facade_mirror.MirrorIndex:
    return vars(paths)[_facade_mirror.INDEX_ATTRIBUTE]


def _family() -> list[ModuleType]:
    return [paths, *(sys.modules[name] for name in _index().owners)]


def _owner_paths() -> dict[str, Path]:
    return {f"{PACKAGE}.{path.stem}": path for path in sorted(SECURITY_DIR.glob(f"{LEAF}_*.py"))}


def _family_sources() -> dict[str, str]:
    files = {FACADE: Path(paths.__file__), **_owner_paths()}
    return {name: path.read_text(encoding="utf-8") for name, path in files.items()}


# --------------------------------------------------------------------------- #
# The patch scan
# --------------------------------------------------------------------------- #


def _test_tree_files() -> list[Path]:
    """Every file pytest collects tests or fixtures from: the root conftest, ``test/``,
    and the test modules and conftests under ``src/kiro_crew/apps/builtins`` -- the same
    walk as ``test_facade_mirror_guards._test_tree_files``."""
    root = source_corpus.repo_root()
    test_root = source_corpus.repo_root() / "test"
    out = []
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        name = path.name
        if rel == "conftest.py" or path.is_relative_to(test_root):
            out.append(path)
        elif rel.startswith("src/kiro_crew/apps/builtins/") and (
            "/tests/" in rel
            or name.startswith("test_")
            or name.endswith("_test.py")
            or name == "conftest.py"
        ):
            out.append(path)
    return out


def _module_aliases(tree: ast.Module) -> dict[str, str]:
    """Spelling -> the target module it names in one test file's syntax tree."""
    aliases = {FACADE: FACADE, PACKAGE: PACKAGE}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in (FACADE, PACKAGE) and alias.asname:
                    aliases[alias.asname] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module in ("kiro_crew", PACKAGE):
            for alias in node.names:
                dotted = f"{node.module}.{alias.name}"
                if dotted in (FACADE, PACKAGE):
                    aliases[alias.asname or alias.name] = dotted
    return aliases


def loop_patched_names(source: str | ast.Module) -> dict[str, set[str]]:
    """Target -> names a loop over string constants patches through it.

    ``for name in ("a", "b"): monkeypatch.setattr(paths, name, ...)`` names the
    attribute through a loop variable, which the corpus scan reads as dynamic, so the
    constants are read here.
    """
    tree = ast.parse(source) if isinstance(source, str) else source
    aliases = _module_aliases(tree)
    found: dict[str, set[str]] = {}
    for loop in ast.walk(tree):
        if not (
            isinstance(loop, ast.For)
            and isinstance(loop.target, ast.Name)
            and isinstance(loop.iter, (ast.Tuple, ast.List))
            and loop.iter.elts
            and all(
                isinstance(e, ast.Constant) and isinstance(e.value, str) for e in loop.iter.elts
            )
        ):
            continue
        constants = {e.value for e in loop.iter.elts if isinstance(e, ast.Constant)}
        for node in ast.walk(loop):
            if not (
                isinstance(node, ast.Call)
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Name)
                and node.args[1].id == loop.target.id
            ):
                continue
            func = node.func
            verb = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            target = aliases.get(ast.unparse(node.args[0]))
            if verb in ("setattr", "delattr", "object") and target is not None:
                found.setdefault(target, set()).update(constants)
    return found


def patched_names(source: str | ast.Module) -> dict[str, set[str]]:
    """Target -> names *source* patches through the facade or the package."""
    tree = ast.parse(source) if isinstance(source, str) else source
    found = facade_mirror_scan.patched_names(tree, {FACADE: FACADE, PACKAGE: PACKAGE})
    for target, names in loop_patched_names(tree).items():
        found.setdefault(target, set()).update(names)
    return {target: names for target, names in found.items() if names}


@functools.lru_cache(maxsize=1)
def _family_defined_names() -> frozenset[str]:
    """Every name a module of the family defines (``_facade_mirror``'s own notion of it):
    a package write of one of these must land on the facade to reach the owner."""
    defined: set[str] = set()
    for source in _family_sources().values():
        defined |= _facade_mirror._defined_names(ast.parse(source))
    return frozenset(defined)


@functools.lru_cache(maxsize=1)
def _corpus_patched_names() -> dict[str, frozenset[str]]:
    """Target -> every name the test trees patch through it, scanned once per worker."""
    found: dict[str, set[str]] = {FACADE: set(), PACKAGE: set()}
    defined = _family_defined_names()
    names_defined = re.compile(r"\b(?:" + "|".join(map(re.escape, sorted(defined))) + r")\b")
    for path in _test_tree_files():
        if path.resolve() == _THIS_FILE:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        # Only a file that spells the facade, or the package and a name the family defines,
        # can patch one of them through either spelling; the rest are never parsed.
        if not (
            _SPELLS_THE_FACADE.search(text) or ("security" in text and names_defined.search(text))
        ):
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for target, names in patched_names(tree).items():
            found[target] |= names
    # A package write of a name the family only imports lands in that name's own module,
    # as it did before the split; the names the family defines are the ones it must route.
    found[PACKAGE] &= defined
    return {target: frozenset(names) for target, names in found.items()}


#: Each spelling the patch scan must read, and each it must skip, keyed by case id.
_SPELLING_CASES = {
    "alias-setattr": (
        "from kiro_crew.security import paths as gate\n"
        "def test(monkeypatch):\n"
        "    monkeypatch.setattr(gate, '_home_targets_cache', {})\n",
        {FACADE: {"_home_targets_cache"}},
    ),
    "dotted-string": (
        "from unittest import mock\n"
        "def test():\n"
        "    with mock.patch('kiro_crew.security.paths._resolved_spellings'):\n"
        "        pass\n",
        {FACADE: {"_resolved_spellings"}},
    ),
    "package": (
        "import kiro_crew.security as security\n"
        "def test(monkeypatch):\n"
        "    monkeypatch.setattr(security, '_path_resolve_degraded', {})\n"
        "    monkeypatch.setattr(security, 'is_denied', None)\n",
        {PACKAGE: {"_path_resolve_degraded", "is_denied"}},
    ),
    "loop": (
        "from kiro_crew.security import paths\n"
        "def test(monkeypatch):\n"
        "    for name in ('_path_resolve_load_probes', '_path_resolve_thread_waits'):\n"
        "        monkeypatch.setattr(paths, name, {})\n",
        {FACADE: {"_path_resolve_load_probes", "_path_resolve_thread_waits"}},
    ),
    "owner-not-facade": (
        "from kiro_crew.security import paths_resolver\n"
        "def test(monkeypatch):\n"
        "    monkeypatch.setattr(paths_resolver, '_ON_WINDOWS', True)\n",
        {},
    ),
}


# --------------------------------------------------------------------------- #
# Reach
# --------------------------------------------------------------------------- #


def reach_problems(
    names: frozenset[str] | set[str],
    index: _facade_mirror.MirrorIndex,
    sources: dict[str, str],
    namespaces: dict[str, dict[str, object]],
) -> list[str]:
    """Why a patch of each of *names* through the facade would miss a reader.

    A name an owner binds must be indexed (an excluded name, or one outside the index, is
    written on the facade alone); a name only the facade binds is the facade's own and is
    written there. Either way every member whose code reads the name as a module global
    must bind it, since the mirror writes exactly the members holding it.
    """
    problems = []
    for name in sorted(names):
        binding = sorted(module for module, ns in namespaces.items() if name in ns)
        if not binding:
            continue  # a probe a test creates and removes, or a builtin it shadows
        facade_only = binding == [index.facade]
        if not facade_only and (name in index.excluded or name not in index.holders):
            problems.append(f"{name}: bound by {binding} but not routed by the mirror")
            continue
        for module, source in sorted(sources.items()):
            if name in facade_mirror_scan.global_reads(source, module) and module not in binding:
                problems.append(f"{name}: {module} reads it without binding it")
    return problems


def test_every_package_routed_name_lands_on_this_facade() -> None:
    """A package write forwards to the module the package table names, so every name the
    test trees patch through the package that the path floor defines must be a row naming
    this facade; one routed elsewhere, or missing from the table, would leave the owner
    that reads it running the unpatched object."""
    names = _corpus_patched_names()[PACKAGE]
    assert names, "no package patch of a path floor name found -- the scan went blind"
    misrouted = {name: security._EXPORTS.get(name) for name in names}
    assert {name: leaf for name, leaf in misrouted.items() if leaf != LEAF} == {}


@pytest.mark.parametrize("spelling", [FACADE, PACKAGE])
def test_a_patch_of_every_scanned_function_reaches_every_holder_and_undoes(
    spelling: str,
) -> None:
    """The executed half, over the patched names that hold functions: each is replaced by
    a delegating stand-in, so a thread still running a gate keeps its behaviour, and
    every holder must see the stand-in and get the original back on undo."""
    target = importlib.import_module(spelling)
    family = _family()
    missed: dict[str, list[str]] = {}
    for name in sorted(_corpus_patched_names()[spelling]):
        original = getattr(paths, name, None)
        if not callable(original) or isinstance(original, (type, ModuleType)):
            continue
        holders = [module for module in family if name in vars(module)]
        before = {module.__name__: vars(module)[name] for module in holders}

        def stand_in(*args: object, _real: object = original, **kwargs: object) -> object:
            return _real(*args, **kwargs)  # type: ignore[operator]

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(target, name, stand_in)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not stand_in]
            if getattr(paths, name) is not stand_in:
                stale.append(FACADE)
        stale += [m.__name__ for m in holders if vars(m).get(name) is not before[m.__name__]]
        if stale:
            missed[name] = stale
    assert missed == {}
    assert _index().divergent() == ()


# --------------------------------------------------------------------------- #
# The family
# --------------------------------------------------------------------------- #


def _relative_owner_imports(tree: ast.Module) -> set[str]:
    """Dotted names of the package modules a source imports, by any spelling."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 1 and node.module:
                found.add(f"{PACKAGE}.{node.module}")
            elif node.level == 1:
                found |= {f"{PACKAGE}.{alias.name}" for alias in node.names}
            elif node.module == PACKAGE:
                found |= {f"{PACKAGE}.{alias.name}" for alias in node.names}
            elif node.module:
                found.add(node.module)
        elif isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
    return found


def test_the_owners_are_every_paths_module_in_layer_order() -> None:
    """``_OWNERS`` lists every ``paths_*.py`` sibling, and each owner imports only
    owners listed before it and never the facade, by an absolute or a relative spelling."""
    owners = list(_index().owners)
    assert sorted(owners) == sorted(_owner_paths())
    problems: dict[str, list[str]] = {}
    for position, owner in enumerate(owners):
        imported = _relative_owner_imports(
            ast.parse(_owner_paths()[owner].read_text(encoding="utf-8"))
        )
        bad = sorted(m for m in imported if m in owners and owners.index(m) >= position)
        if FACADE in imported:
            bad.append(FACADE)
        if bad:
            problems[owner] = bad
    assert problems == {}


def test_the_layer_rule_flags_a_relative_import_of_the_facade() -> None:
    assert FACADE in _relative_owner_imports(ast.parse("from . import paths\n"))
    assert FACADE in _relative_owner_imports(ast.parse("from .paths import x\n"))
    assert _relative_owner_imports(ast.parse("from .paths_lists import x\n")) == {
        f"{PACKAGE}.paths_lists"
    }


def test_every_member_logs_under_the_facade_name() -> None:
    """Log filters and captures key on the unsplit module's logger name, so every member
    that logs holds that one logger."""
    assert paths.logger.name == FACADE
    others = [
        module.__name__
        for module in _family()
        if vars(module).get("logger", paths.logger) is not paths.logger
    ]
    assert others == []


def _outside_imports() -> dict[str, tuple[str, str]]:
    """Served names an owner imports from a project module outside the family:
    name -> (home module, original name)."""
    owners = set(_index().owners)
    found: dict[str, tuple[str, str]] = {}
    for path in _owner_paths().values():
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.level == 1:
                home = f"{PACKAGE}.{node.module}" if node.module else PACKAGE
            elif node.module and node.module.startswith("kiro_crew"):
                home = node.module
            else:
                continue
            if home in owners:
                continue
            for alias in node.names:
                found.setdefault(alias.asname or alias.name, (home, alias.name))
    return found


def _home_manifest() -> dict[str, tuple[str, str]]:
    """The facade's ``TYPE_CHECKING`` imports from outside its owners: name -> (home,
    original name)."""
    owners = set(_index().owners)
    out: dict[str, tuple[str, str]] = {}
    for node in ast.parse(Path(paths.__file__).read_text(encoding="utf-8")).body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for stmt in node.body:
                if isinstance(stmt, ast.ImportFrom) and stmt.module not in owners:
                    for alias in stmt.names:
                        out[alias.asname or alias.name] = (stmt.module or "", alias.name)
    return out


def test_every_name_served_from_outside_the_family_is_visible_to_type_checkers() -> None:
    """At BASE the module bound these by import, so ``from kiro_crew.security.paths
    import path_resolve_executor`` type-checked. Now an owner holds them, so the facade names each
    one from its home module under ``TYPE_CHECKING``, and exactly those."""
    served = set(_index().exports)
    expected = {name: home for name, home in _outside_imports().items() if name in served}
    assert expected, "no served name comes from outside the family -- the scan went blind"
    assert _home_manifest() == expected


def test_the_stall_exception_keeps_the_facade_spelling() -> None:
    """``PathResolutionStalled`` names a refusal in tracebacks and logs, so it keeps the
    module path callers import it from, while the class itself lives in its owner."""
    exc = paths.PathResolutionStalled("/srv/wedged/x", "/srv/wedged")
    assert type(exc).__module__ == FACADE
    assert type(exc).__qualname__ == "PathResolutionStalled"
    line = traceback.format_exception_only(exc)[-1]
    assert line.startswith(f"{FACADE}.PathResolutionStalled: ")
    assert (
        paths.PathResolutionStalled
        is sys.modules[f"{PACKAGE}.paths_resolver"].PathResolutionStalled
    )
