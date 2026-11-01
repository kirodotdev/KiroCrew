"""The egress facade keeps the patch seams the one module had.

``kiro_crew.security.exfil`` is a mirror facade (:mod:`kiro_crew._facade_mirror`) over the
owners ``exfil_url_rules``, ``exfil_oauth`` and ``exfil_bash``; the URL classifier, the
blocked-link records, the URL redactor, the OAuth credential diagnosis and the IMDS check stay
in the facade itself. The moved code reads its names as plain module globals, so a patch reaches
it only because the mirror writes the patch into every module holding the name. Two spellings
reach the facade: a write on ``kiro_crew.security.exfil`` itself, and a write on the package
``kiro_crew.security`` of a name its ``_EXPORTS`` table lists (each row naming ``exfil``), which
the package forwards here. Every other owner-defined name is reached through
``kiro_crew.security.exfil`` only.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules, and its
corpus counts patches spelled through the facade's own name only. These tests pin what is
particular to this facade: every name the test trees patch through either spelling reaches every
module holding it; the owners are exactly the modules the facade lists, loaded lowest layer
first, none importing the facade by any spelling; every member that logs holds the facade's
logger; and each project name served from outside the family stays visible to type checkers.
"""

from __future__ import annotations

import ast
import functools
import importlib
import re
import sys
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus

import kiro_crew.security as security
from kiro_crew import _facade_mirror
from kiro_crew.security import exfil

pytestmark = pytest.mark.xdist_group(name="tree_scan_exfil_facade")

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

FACADE = "kiro_crew.security.exfil"
PACKAGE = "kiro_crew.security"
LEAF = "exfil"
SECURITY_DIR = Path(exfil.__file__).resolve().parent
#: The owner modules, in the layer order ``_OWNERS`` must list them.
OWNERS = ("exfil_url_rules", "exfil_scan_span", "exfil_oauth", "exfil_bash")
_THIS_FILE = Path(__file__).resolve()
#: Text every file that reaches the facade by name holds: a dotted spelling (an import,
#: a string target, a ``sys.modules`` key, an attribute chain off the package) or an import
#: of it from the package, on one line or parenthesized.
_SPELLS_THE_FACADE = re.compile(
    rf"security\.{LEAF}\b|security import[^\n]*\b{LEAF}\b|security import \("
)

#: Names the test trees are known to patch, by the spelling they use: the SEL writer the OAuth
#: audit events use, the discovered endpoint set, the exempt-host lookups, the operator file
#: loader, the endpoint approval lookup, the extension memo and audit set, the extension audit
#: emitter, the Slack manifest memo and the URL redactor. The scan must find all of them, so a
#: scanner that stops matching goes red instead of passing over nothing.
_KNOWN_SEAMS: dict[str, frozenset[str]] = {
    FACADE: frozenset(
        {
            "SecurityEventLog",
            "_OAUTH_DISCOVERED_ENDPOINTS",
            "_exempt_exact_hosts",
            "_exfil_exempt_hosts",
            "_load_operator_oauth_endpoints",
        }
    ),
    PACKAGE: frozenset(
        {
            "_approved_oauth_authorization_endpoint",
            "_OAUTH_EXTENSION_AUDITED",
            "_OAUTH_EXTENSION_MEMO",
            "_emit_oauth_extension_used_event",
            "_slack_manifest_re_slot",
        }
    ),
}


def _index() -> _facade_mirror.MirrorIndex:
    return vars(exfil)[_facade_mirror.INDEX_ATTRIBUTE]


def _family() -> list[ModuleType]:
    return [exfil, *(sys.modules[name] for name in _index().owners)]


def _owner_paths() -> dict[str, Path]:
    return {f"{PACKAGE}.{stem}": SECURITY_DIR / f"{stem}.py" for stem in OWNERS}


def _family_sources() -> dict[str, str]:
    files = {FACADE: Path(exfil.__file__), **_owner_paths()}
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

    ``for name in ("a", "b"): monkeypatch.setattr(exfil, name, ...)`` names the attribute
    through a loop variable, which the corpus scan reads as dynamic, so the constants are
    read here.
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
    # A package write of a name the family only imports lands in that name's own module;
    # the names the family defines are the ones it must route.
    found[PACKAGE] &= defined
    return {target: frozenset(names) for target, names in found.items()}


#: Each spelling the patch scan must read, and each it must skip, keyed by case id.
_SPELLING_CASES = {
    "alias-setattr": (
        "from kiro_crew.security import exfil as gate\n"
        "def test(monkeypatch):\n"
        "    monkeypatch.setattr(gate, '_load_operator_oauth_endpoints', None)\n",
        {FACADE: {"_load_operator_oauth_endpoints"}},
    ),
    "dotted-string": (
        "from unittest import mock\n"
        "def test():\n"
        "    with mock.patch('kiro_crew.security.exfil._exfil_exempt_hosts'):\n"
        "        pass\n",
        {FACADE: {"_exfil_exempt_hosts"}},
    ),
    "package": (
        "import kiro_crew.security as security\n"
        "def test(monkeypatch):\n"
        "    monkeypatch.setattr(security, '_OAUTH_EXTENSION_MEMO', None)\n"
        "    monkeypatch.setattr(security, 'is_denied', None)\n",
        {PACKAGE: {"_OAUTH_EXTENSION_MEMO", "is_denied"}},
    ),
    "loop": (
        "from kiro_crew.security import exfil\n"
        "def test(monkeypatch):\n"
        "    for name in ('_load_operator_oauth_endpoints', '_exfil_exempt_hosts'):\n"
        "        monkeypatch.setattr(exfil, name, None)\n",
        {FACADE: {"_load_operator_oauth_endpoints", "_exfil_exempt_hosts"}},
    ),
    "owner-not-facade": (
        "from kiro_crew.security import exfil_url_rules\n"
        "def test(monkeypatch):\n"
        "    monkeypatch.setattr(exfil_url_rules, '_load_operator_oauth_endpoints', None)\n",
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
    test trees patch through the package that this family defines must be a row naming
    this facade; one routed elsewhere, or missing from the table, would leave the owner
    that reads it running the unpatched object."""
    names = _corpus_patched_names()[PACKAGE]
    assert names, "no package patch of a name of the exfil family found -- the scan went blind"
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
        original = getattr(exfil, name, None)
        if not callable(original) or isinstance(original, (type, ModuleType)):
            continue
        holders = [module for module in family if name in vars(module)]
        before = {module.__name__: vars(module)[name] for module in holders}

        def stand_in(*args: object, _real: object = original, **kwargs: object) -> object:
            return _real(*args, **kwargs)  # type: ignore[operator]

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(target, name, stand_in)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not stand_in]
            if getattr(exfil, name) is not stand_in:
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


def test_the_owners_are_the_listed_modules_in_layer_order() -> None:
    """``_OWNERS`` lists exactly the owner modules, lowest layer first, and each owner
    imports only owners listed before it and never the facade, by an absolute or a
    relative spelling. No other ``exfil_*`` sibling exists,
    so a new one must join ``_OWNERS``."""
    owners = list(_index().owners)
    assert owners == [f"{PACKAGE}.{stem}" for stem in OWNERS]
    siblings = {path.stem for path in SECURITY_DIR.glob(f"{LEAF}_*.py")}
    assert siblings == set(OWNERS)
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
    assert FACADE in _relative_owner_imports(ast.parse("from . import exfil\n"))
    assert FACADE in _relative_owner_imports(ast.parse("from .exfil import x\n"))
    assert _relative_owner_imports(ast.parse("from .exfil_url_rules import x\n")) == {
        f"{PACKAGE}.exfil_url_rules"
    }


def test_every_member_logs_under_the_facade_name() -> None:
    """Log filters and captures key on the unsplit module's logger name, so every member
    that logs holds that one logger."""
    assert exfil.logger.name == FACADE
    others = [
        module.__name__
        for module in _family()
        if vars(module).get("logger", exfil.logger) is not exfil.logger
    ]
    assert others == []


def _outside_imports() -> dict[str, tuple[str, str]]:
    """Served names an owner imports from a project module outside the family:
    name -> (home module, original name)."""
    owners = set(_index().owners)
    found: dict[str, tuple[str, str]] = {}
    for path in _owner_paths().values():
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("kiro_crew.") and alias.name not in owners:
                        found.setdefault(alias.asname or alias.name, (alias.name, ""))
                continue
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
    """The facade's ``TYPE_CHECKING`` imports of project modules outside its owners:
    name -> (home, original name). The standard-library names the unsplit module already
    imported for its own annotations are not served names."""
    owners = set(_index().owners)
    out: dict[str, tuple[str, str]] = {}
    for node in ast.parse(Path(exfil.__file__).read_text(encoding="utf-8")).body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for stmt in node.body:
                if isinstance(stmt, ast.Import):
                    for alias in stmt.names:
                        if alias.name.startswith("kiro_crew."):
                            out[alias.asname or alias.name] = (alias.name, "")
                elif (
                    isinstance(stmt, ast.ImportFrom)
                    and (stmt.module or "").startswith("kiro_crew")
                    and stmt.module not in owners
                ):
                    for alias in stmt.names:
                        out[alias.asname or alias.name] = (stmt.module or "", alias.name)
    return out


def test_every_name_served_from_outside_the_family_is_visible_to_type_checkers() -> None:
    """A served name an owner imports from a project module outside the family is named
    from its home module under ``TYPE_CHECKING``, so ``from kiro_crew.security.exfil
    import <name>`` type-checks; exactly those names."""
    served = set(_index().exports)
    expected = {name: home for name, home in _outside_imports().items() if name in served}
    assert expected, "no served name comes from outside the family -- the scan went blind"
    assert _home_manifest() == expected
