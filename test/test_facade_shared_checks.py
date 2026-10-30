"""Facade checks whose body is the same for every split module, written once.

Each ``test_*facade*.py`` file pins one split module's facade. Many of its checks
read only that file's own helpers (``_corpus_patched_names``, ``_index``,
``_reach_gaps`` ...) and constants (``_KNOWN_SEAMS``, ``OWNERS`` ...), so the body is
the same in every file. Those bodies live here, parametrized with one id per module.

A file opts in by naming the checks in a module-level ``SHARED_FACADE_CHECKS``
tuple. Discovery reads that tuple from the text of every ``test_*facade*.py`` file
at collection, without importing them; each test imports its module by name, which
under pytest's default import mode is the module object pytest already collected,
so its ``lru_cache``d corpus scan is shared with that file's own tests. Each param
carries its module's ``xdist_group``, so under ``--dist loadgroup`` it runs on the
worker that already paid for the scan.
"""

from __future__ import annotations

import ast
import dataclasses
import functools
import importlib
import logging
import re
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
import source_corpus

from kiro_crew import _facade_mirror

TEST_DIR = Path(__file__).resolve().parent
_THIS_STEM = Path(__file__).stem

#: Every check a module can opt into. A name outside this set is a typo that would
#: otherwise opt the module out silently.
KNOWN_CHECKS = frozenset(
    {
        "known_seams",
        "known_seams_by_target",
        "spelling_cases",
        "corpus_gate",
        "test_tree_walk",
        "reach_routing",
        "reach_scan_can_fail",
        "reach_problems",
        "reach_rule_can_fail",
        "owner_layering",
        "owners_in_layer_order",
        "owners_in_layer_order_each_facade",
        "one_home_per_name",
        "home_manifest",
        "base_names_resolve",
        "owners_log_as_facade",
        "owners_log_under_facade_name",
        "logger_scan_can_fail",
        "class_body_check_can_fail",
    }
)

_OPT_IN = re.compile(r"^SHARED_FACADE_CHECKS\s*=\s*(\([^)]*\))", re.M)
_GROUP = re.compile(r"""xdist_group\(\s*name\s*=\s*["']([^"']+)["']""")


@dataclasses.dataclass(frozen=True)
class _OptIn:
    checks: frozenset[str]
    group: str | None


def _discover() -> tuple[dict[str, _OptIn], list[str]]:
    """Read every facade test file's opt-in from its text: stem -> checks, plus errors."""
    found: dict[str, _OptIn] = {}
    errors: list[str] = []
    for path in sorted(TEST_DIR.glob("test_*facade*.py")):
        if path.stem == _THIS_STEM:
            continue
        text = path.read_text(encoding="utf-8")
        if "SHARED_FACADE_CHECKS" not in text:
            continue
        match = _OPT_IN.search(text)
        if match is None:
            errors.append(f"{path.name}: SHARED_FACADE_CHECKS is not a one-statement tuple")
            continue
        checks = ast.literal_eval(match.group(1))
        mark_at = text.find("\npytestmark")
        group = _GROUP.search(text, mark_at) if mark_at >= 0 else None
        found[path.stem] = _OptIn(frozenset(checks), group.group(1) if group else None)
    return found, errors


_OPTED_IN, _DISCOVERY_ERRORS = _discover()


def _params(check: str) -> list:
    return [
        pytest.param(
            stem,
            id=stem,
            marks=[pytest.mark.xdist_group(name=opt.group)] if opt.group else [],
        )
        for stem, opt in _OPTED_IN.items()
        if check in opt.checks
    ]


def _each(check: str) -> Callable:
    return pytest.mark.parametrize("stem", _params(check))


def _load(stem: str) -> ModuleType:
    return importlib.import_module(stem)


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def test_every_opt_in_parses_and_names_only_known_checks() -> None:
    assert _DISCOVERY_ERRORS == []
    unknown = {s: sorted(o.checks - KNOWN_CHECKS) for s, o in _OPTED_IN.items()}
    assert {s: names for s, names in unknown.items() if names} == {}


def test_every_known_check_runs_on_some_module() -> None:
    """Non-vacuity: a discovery that went blind would collect zero params and pass."""
    used = set().union(*(opt.checks for opt in _OPTED_IN.values()))
    assert sorted(KNOWN_CHECKS - used) == []


def test_every_opted_in_module_runs_in_its_own_xdist_group() -> None:
    """Each module's tree scan is cached per worker; its group keeps it on one worker."""
    assert sorted(stem for stem, opt in _OPTED_IN.items() if opt.group is None) == []


# --------------------------------------------------------------------------- #
# The patch scan
# --------------------------------------------------------------------------- #


@_each("known_seams")
def test_the_patch_scan_finds_every_known_seam(stem: str) -> None:
    """Non-vacuity: a scan that finds nothing would pass the reach test over nothing."""
    module = _load(stem)
    assert sorted(module._KNOWN_SEAMS - module._corpus_patched_names()) == []


@_each("known_seams_by_target")
def test_the_patch_scan_finds_every_known_seam_of_each_target(stem: str) -> None:
    """The same, for a module patched both through its facade and through its package."""
    module = _load(stem)
    found = module._corpus_patched_names()
    missing = {
        target: sorted(seams - found[target]) for target, seams in module._KNOWN_SEAMS.items()
    }
    assert missing == {module.FACADE: [], module.PACKAGE: []}


@_each("spelling_cases")
def test_the_patch_scan_reads_every_spelling_and_nothing_else(stem: str) -> None:
    module = _load(stem)
    cases = module._SPELLING_CASES
    assert cases
    wrong = {
        case: got
        for case, (source, expected) in cases.items()
        if (got := module.patched_names(source)) != expected
    }
    assert wrong == {}


@functools.lru_cache(maxsize=1)
def _corpus_gates() -> frozenset[str]:
    root = source_corpus.repo_root()
    return frozenset(importlib.import_module("leaf_test_scope").corpus_gates(root))


@pytest.mark.xdist_group(name="tree_scan_facade_shared_checks")
@pytest.mark.parametrize("stem", [*_params("corpus_gate"), _THIS_STEM])
def test_leaf_test_scope_derives_the_file_as_a_corpus_gate(
    stem: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file reads the whole test tree, so a leaf-only diff must still run it."""
    root = source_corpus.repo_root()
    monkeypatch.syspath_prepend(str(root / "scripts"))
    assert (TEST_DIR / f"{stem}.py").relative_to(root).as_posix() in _corpus_gates()


@_each("test_tree_walk")
def test_the_test_tree_walk_reaches_every_collected_tree(stem: str) -> None:
    walked = _load(stem)._test_tree_files()
    assert TEST_DIR / f"{stem}.py" in walked
    assert source_corpus.repo_root() / "conftest.py" in walked
    assert any("/container_tests/" in path.as_posix() for path in walked)
    assert len(walked) > 1000


# --------------------------------------------------------------------------- #
# Reach: a patch through the facade lands on every module holding the name
# --------------------------------------------------------------------------- #


@_each("reach_routing")
def test_every_scanned_name_is_routed_to_every_owner_holding_it(stem: str) -> None:
    module = _load(stem)
    assert module._reach_gaps(set(module._corpus_patched_names()), module._index()) == []


@_each("reach_scan_can_fail")
def test_the_reach_scan_can_fail(stem: str) -> None:
    """The routing check reads the index: blind one name and the gap shows."""
    module = _load(stem)
    index = module._index()
    name, holders = next((n, h) for n, h in sorted(index.holders.items()) if h)
    blind = dataclasses.replace(index, holders={**index.holders, name: ()})
    assert module._reach_gaps({name}, blind) == [f"{owner}.{name}" for owner in holders]


@_each("reach_problems")
def test_every_scanned_name_reaches_every_module_reading_it(stem: str) -> None:
    """The structural half of the reach the unsplit module had, for every patched name
    by either spelling: the mirror routes the name, and nothing reads it unbound."""
    module = _load(stem)
    corpus = module._corpus_patched_names()
    names = corpus[module.FACADE] | corpus[module.PACKAGE]
    namespaces = {m.__name__: vars(m) for m in module._family()}
    problems = module.reach_problems(names, module._index(), module._family_sources(), namespaces)
    assert problems == []


@_each("reach_rule_can_fail")
def test_the_reach_rule_flags_an_unbound_reader_and_an_unrouted_name(stem: str) -> None:
    """A facade-only name read by the facade alone passes; an owner reading it does not."""
    index = _facade_mirror.MirrorIndex(
        facade="pkg.facade",
        owners=("pkg.low", "pkg.high"),
        holders={"LIMIT": ("pkg.low",)},
        exports={"LIMIT": "pkg.low"},
        excluded=("CLASH",),
    )
    sources = {
        "pkg.low": "LIMIT = 3\ndef g():\n    return GATE\n",
        "pkg.high": "def f():\n    return LIMIT\n",
        "pkg.facade": "def GATE():\n    return 1\ndef h():\n    return GATE\n",
    }
    namespaces = {
        "pkg.low": {"LIMIT": 3, "CLASH": 1},
        "pkg.high": {"CLASH": 2},
        "pkg.facade": {"GATE": object()},
    }
    problems = _load(stem).reach_problems(
        {"LIMIT", "CLASH", "GATE", "open"}, index, sources, namespaces
    )
    assert problems == [
        "CLASH: bound by ['pkg.high', 'pkg.low'] but not routed by the mirror",
        "GATE: pkg.low reads it without binding it",
        "LIMIT: pkg.high reads it without binding it",
    ]


# --------------------------------------------------------------------------- #
# The family's shape: owners, layering, one home per name
# --------------------------------------------------------------------------- #


@_each("owner_layering")
def test_each_owner_imports_only_owners_listed_before_it_and_never_the_facade(stem: str) -> None:
    module = _load(stem)
    trees = {name: module._tree(sys.modules[name]) for name in module.OWNERS}
    assert module._layering_gaps(trees) == {}


def _late_imports(owners: list[str], path_of: Callable[[str], Path], package: str | None) -> dict:
    """Owners that import an owner listed at or after their own position."""
    late: dict[str, list[str]] = {}
    for position, owner in enumerate(owners):
        tree = ast.parse(path_of(owner).read_text(encoding="utf-8"))
        imported = {
            f"{package}.{node.module}" if node.level and package else node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        later = sorted(m for m in imported if m in owners and owners.index(m) >= position)
        if later:
            late[owner] = later
    return late


@_each("owners_in_layer_order")
def test_the_owners_are_the_sibling_modules_in_layer_order(stem: str) -> None:
    """``_OWNERS`` lists exactly the sibling owners, and each imports only owners listed
    before it -- the order the facade loads them and a read resolves in."""
    module = _load(stem)
    owners = list(module._index().owners)
    assert owners == list(module.OWNERS)
    assert _late_imports(owners, module._owner_path, None) == {}


@_each("owners_in_layer_order_each_facade")
def test_the_owners_of_each_facade_are_every_sibling_module_in_layer_order(stem: str) -> None:
    """The same for a file pinning several facades: ``_OWNERS`` lists every
    ``<stem>_*.py`` sibling, and each owner imports only owners listed before it."""
    module = _load(stem)
    unlisted: dict[str, tuple[list[str], list[str]]] = {}
    late: dict[str, dict] = {}
    for facade in module.FACADES:
        owners = list(facade.index.owners)
        paths = facade.owner_paths()
        if sorted(owners) != sorted(paths):
            unlisted[facade.stem] = (sorted(owners), sorted(paths))
        if gaps := _late_imports(owners, paths.__getitem__, module.PKG):
            late[facade.stem] = gaps
    assert module.FACADES
    assert unlisted == {}
    assert late == {}


@_each("one_home_per_name")
def test_no_two_members_of_the_family_define_the_same_name(stem: str) -> None:
    """Each moved definition has one home: the facade keeps what it kept, an owner the rest."""
    module = _load(stem)
    seen: dict[str, str] = {}
    clashes = []
    for member in module._family():
        for name in sorted(_facade_mirror._defined_names(module._tree(member))):
            if name in {"__all__"}:
                continue
            if name in seen:
                clashes.append(f"{name}: {seen[name]} and {member.__name__}")
            seen[name] = member.__name__
    assert clashes == []


@_each("home_manifest")
def test_every_name_served_from_outside_the_family_is_visible_to_type_checkers(stem: str) -> None:
    """A name an owner imports from a project module and the facade serves without
    binding is named from its home under ``TYPE_CHECKING``."""
    module = _load(stem)
    home_imports = module._home_imports()
    assert home_imports, "the owners import nothing from the project -- the scan went blind"
    served = set(module._index().exports)
    assert module._home_manifest() == {n: h for n, h in home_imports.items() if n in served}


@_each("base_names_resolve")
def test_every_base_module_name_resolves_through_the_facade(stem: str) -> None:
    module = _load(stem)
    facade, index = module.facade, module._index()
    missing = sorted(name for name in module._BASE_MODULE_NAMES if not hasattr(facade, name))
    assert missing == []
    for name in sorted(module._BASE_MODULE_NAMES):
        holders = index.holders.get(name, ())
        assert holders, f"{name} is no owner's name"
        assert all(
            getattr(importlib.import_module(holder), name) is getattr(facade, name)
            for holder in holders
        ), name


# --------------------------------------------------------------------------- #
# Logging: the family logs under the facade's name
# --------------------------------------------------------------------------- #


@_each("owners_log_as_facade")
def test_every_owner_logs_as_the_facade(stem: str) -> None:
    module = _load(stem)
    shared = logging.getLogger(module._FACADE)
    assert module.facade.logger is shared
    loggers = {
        owner: vars(importlib.import_module(owner)).get("logger")
        for owner in module._index().owners
    }
    assert {owner: log for owner, log in loggers.items() if log not in (None, shared)} == {}


@_each("owners_log_under_facade_name")
def test_the_owners_log_under_the_facade_name(stem: str) -> None:
    """The moved code logs as the one module did: an owner's ``__name__`` would be a
    logger no filter, level or capture names (``caplog.at_level(logger=...)``)."""
    module = _load(stem)
    names = [
        n for owner in module.OWNERS for n in module._logger_names(module._tree(sys.modules[owner]))
    ]
    assert names == [repr(module.FACADE)], names
    holders = [m for m in module._family() if "logger" in vars(m)]
    assert len(holders) > 2, "too few modules hold the logger -- the scan went blind"
    assert all(vars(m)["logger"] is logging.getLogger(module.FACADE) for m in holders)


# --------------------------------------------------------------------------- #
# The per-file scanners can fail
# --------------------------------------------------------------------------- #


@_each("logger_scan_can_fail")
def test_the_logger_scan_can_fail(stem: str) -> None:
    tree = ast.parse("import logging\nlogging.getLogger(__name__).warning('x')\n")
    assert _load(stem)._logger_names(tree) == ["__name__"]


@_each("class_body_check_can_fail")
def test_the_class_body_check_can_fail(stem: str) -> None:
    source = "def f(self):\n    super().f()\n    print(__class__)\n    return self.__hidden\n"
    found = _load(stem)._class_body_dependencies(source)
    assert sorted(found) == ["__class__", "mangled __hidden", "super()"]
