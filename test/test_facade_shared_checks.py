"""Facade checks whose body is the same for every split module, written once.

Each ``test_*facade*.py`` or ``test_*composition*.py`` file pins one split module's
facade. Many of its checks read only that file's own helpers
(``_corpus_patched_names``, ``_index``, ``_reach_gaps`` ...) and constants
(``_KNOWN_SEAMS``, ``OWNERS`` ...), so the body is the same in every file. Those bodies
live here, parametrized with one id per module.

A file opts in by naming the checks in a module-level ``SHARED_FACADE_CHECKS``
tuple. Discovery reads that tuple from the text of every file matching
:data:`_PATTERNS` at collection, without importing them; each test imports its module by name, which
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
import inspect
import logging
import os
import pkgutil
import re
import subprocess
import sys
import types
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
import source_corpus

from kiro_crew import _facade_mirror
from kiro_crew.subprocess_utf8 import UTF8_TEXT

TEST_DIR = Path(__file__).resolve().parent
_THIS_STEM = Path(__file__).stem
#: The test files discovery reads for an opt-in.
_PATTERNS = ("test_*facade*.py", "test_*composition*.py", "test_*_parts*.py")

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
        # The composed-class contracts (a class bound together from owner functions).
        "the_class_body_scan_can_fail",
        "the_facade_installs_its_mirror_when_run_as_a_script",
        "every_owner_logs_under_the_facades_name",
        "a_moved_member_is_its_owners_function",
        "every_owner_function_of_self_is_bound_on_the_class",
        "no_bound_function_needs_the_class_body",
        "each_guarded_construct_stays_in_the_facade",
        "the_owners_are_every_sibling_module_in_layer_order",
        "an_owner_imports_only_owners_below_it_and_never_its_facade",
        "the_class_keeps_every_base_member",
        "the_class_keeps_every_base_member_and_gains_none",
        "every_base_member_keeps_its_kind_and_signature",
        "only_the_family_imports_an_owner",
        "the_owner_import_scan_finds_an_outside_importer",
        "each_moved_class_is_served_whole_from_its_owner",
        # The mirrored-module contracts (transport dispatch).
        "every_base_name_still_resolves",
        "a_moved_name_is_its_owners_object",
        "the_mirror_serves_exactly_the_moved_names_the_facade_does_not_bind",
        "the_owner_list_is_the_layer_order_and_names_every_owner_on_disk",
        "no_owner_takes_a_sync_io_module_through_a_from_import",
        "no_owner_holds_what_the_facade_guards_read_by_path",
        # The parts packages (a module split into a package of parts).
        "an_owner_imports_the_facade_only_for_type_checking_and_no_project_module",
        "no_name_of_the_family_is_excluded_from_the_mirror",
        "owners_import_only_owners_listed_before_them",
        "the_globstar_matcher_reads_patterns_as_the_reviewers_do",
        "the_owner_set_is_the_package",
        "the_package_init_defines_nothing",
        "the_sweep_reports_a_global_the_namespace_does_not_bind",
        "the_value_check_can_fail",
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
    paths = {path for pattern in _PATTERNS for path in TEST_DIR.glob(pattern)}
    for path in sorted(paths):
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


def _each_contract(check: str) -> Callable:
    """``_each`` under the argument name ``contract``, for bodies that loop over a
    local ``stem`` (an owner's file name)."""
    return pytest.mark.parametrize("contract", _params(check))


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


@_each_contract("the_class_body_scan_can_fail")
def test_the_class_body_scan_can_fail(contract: str) -> None:
    subject = _load(contract)
    planted = "def f(self):\n    super().g()\n    return __class__, self.__secret, self.__dict__\n"
    assert subject._class_body_reads(planted) == ["2: super()", "3: __class__", "3: __secret"]


@_each_contract("the_facade_installs_its_mirror_when_run_as_a_script")
def test_the_facade_installs_its_mirror_when_run_as_a_script(contract: str, tmp_path: Path) -> None:
    """Under ``python -m`` the facade is ``__main__``: one logger by literal name keeps
    the family to one object per name, which the mirror's install requires."""
    subject = _load(contract)
    result = subprocess.run(
        [sys.executable, "-m", subject.FACADE],
        capture_output=True,
        cwd=tmp_path,
        env=dict(os.environ),
        timeout=120,
        **UTF8_TEXT,
    )
    assert result.returncode == 0, result.stderr


@_each_contract("every_owner_logs_under_the_facades_name")
def test_every_owner_logs_under_the_facades_name(contract: str) -> None:
    subject = _load(contract)
    assert subject.FACADE_MODULE.logger is logging.getLogger(subject.FACADE)
    for stem in subject.OWNERS:
        logger = vars(subject._owner(stem)).get("logger")
        if logger is not None:
            assert logger is logging.getLogger(subject.FACADE), stem


@_each_contract("a_moved_member_is_its_owners_function")
def test_a_moved_member_is_its_owners_function(contract: str) -> None:
    subject = _load(contract)
    members = vars(subject.CLS)
    for stem, names in subject.MOVED_MEMBERS.items():
        owner = subject._owner(stem)
        for name in names:
            assert members[name] is getattr(owner, name), f"{stem}.{name}"
            assert members[name].__module__ == owner.__name__, f"{stem}.{name}"
            assert members[name].__qualname__ == name, f"{stem}.{name}"


@_each_contract("every_owner_function_of_self_is_bound_on_the_class")
def test_every_owner_function_of_self_is_bound_on_the_class(contract: str) -> None:
    subject = _load(contract)
    bound = {name for names in subject.MOVED_MEMBERS.values() for name in names}
    for stem in subject.OWNERS:
        owner = subject._owner(stem)
        for name, value in vars(owner).items():
            if not inspect.isfunction(value) or value.__module__ != owner.__name__:
                continue
            if list(inspect.signature(value).parameters)[:1] == ["self"]:
                assert name in bound and vars(subject.CLS)[name] is value, f"{stem}.{name}"


@_each_contract("no_bound_function_needs_the_class_body")
def test_no_bound_function_needs_the_class_body(contract: str) -> None:
    """A function bound into the class from an owner was compiled outside it: it has no
    ``__class__`` cell, so a zero-argument ``super()`` or a ``__class__`` read raises, and
    a ``self.__name`` is never mangled. So no owner spells one; a method that needs one
    stays in the class body."""
    subject = _load(contract)
    assert {
        stem: hits
        for stem in subject.OWNERS
        if (hits := subject._class_body_reads(subject._source(stem)))
    } == {}


@_each_contract("each_guarded_construct_stays_in_the_facade")
def test_each_guarded_construct_stays_in_the_facade(contract: str) -> None:
    """The constructs ``FACADE_PINNED`` names are defined in the facade's own file."""
    subject = _load(contract)
    tree = ast.parse(subject.FACADE_PATH.read_text(encoding="utf-8"))
    cls_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == subject.CLS.__name__
    )
    members = {
        node.name
        for node in cls_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert sorted(subject.FACADE_PINNED - (subject._top_level_names(tree) | members)) == []
    for stem in subject.OWNERS:
        owned = subject._top_level_names(ast.parse(subject._source(stem)))
        assert sorted(subject.FACADE_PINNED & owned) == [], stem


@_each_contract("the_owners_are_every_sibling_module_in_layer_order")
def test_the_owners_are_every_sibling_module_in_layer_order(contract: str) -> None:
    subject = _load(contract)
    package = subject.FACADE.rsplit(".", 1)[0]
    on_disk = {path.stem for path in subject.FACADE_PATH.parent.glob(f"{subject.STEM}_*.py")}
    assert on_disk == set(subject.OWNERS)
    assert subject.FACADE_MODULE._OWNERS == tuple(f"{package}.{stem}" for stem in subject.OWNERS)


@_each_contract("an_owner_imports_only_owners_below_it_and_never_its_facade")
def test_an_owner_imports_only_owners_below_it_and_never_its_facade(contract: str) -> None:
    subject = _load(contract)
    package = subject.FACADE.rsplit(".", 1)[0]
    for index, stem in enumerate(subject.OWNERS):
        loaded = subject._runtime_imports(subject._source(stem))
        assert subject.FACADE not in loaded, stem
        siblings = {
            name.rsplit(".", 1)[-1]
            for name in loaded
            if name.startswith(f"{package}.{subject.STEM}_") and name.count(".") == 2
        }
        assert siblings <= set(subject.OWNERS[:index]), stem


@_each_contract("the_class_keeps_every_base_member")
def test_the_class_keeps_every_base_member(contract: str) -> None:
    subject = _load(contract)
    assert sorted(set(subject.BASE_MEMBERS) - subject._members(subject.CLS)) == []


@_each_contract("every_base_member_keeps_its_kind_and_signature")
def test_every_base_member_keeps_its_kind_and_signature(contract: str) -> None:
    subject = _load(contract)
    members = vars(subject.CLS)
    for name, described in subject.BASE_MEMBERS.items():
        assert subject._describe(members[name], subject.CLS) == described, name


@_each_contract("only_the_family_imports_an_owner")
def test_only_the_family_imports_an_owner(contract: str) -> None:
    """Callers reach the owners' names through the facade, never by importing an owner."""
    subject = _load(contract)
    family = {f"discord/{stem}.py" for stem in (subject.STEM, *subject.OWNERS)}
    sources = source_corpus.iter_candidate_sources(require_any=(f"{subject.STEM}_",))
    importers = subject._owner_importers(
        ((path, text) for path, text in sources if path.is_relative_to(subject.SRC)), subject.SRC
    )
    assert subject.FAMILY_IMPORTER in importers  # owners import the owners below them
    assert importers - family == set()


@_each_contract("the_owner_import_scan_finds_an_outside_importer")
def test_the_owner_import_scan_finds_an_outside_importer(contract: str, tmp_path: Path) -> None:
    subject = _load(contract)
    root = tmp_path / "kiro_crew"
    owner = subject.OWNERS[0]
    sources = {
        root / "pkg" / "caller.py": f"from kiro_crew.discord.{owner} import x\n",
        root / "other.py": f"from kiro_crew.discord import {owner} as low\n",
        root / "discord" / "relative.py": f"from .{owner} import x\n",
        root / "discord" / "deep" / "up.py": f"from .. import {owner}\n",
        root / "facade_user.py": f"from kiro_crew.discord.{subject.STEM} import x\n",
    }
    assert subject._owner_importers(sources.items(), root) == {
        "pkg/caller.py",
        "other.py",
        "discord/relative.py",
        "discord/deep/up.py",
    }


@_each_contract("each_moved_class_is_served_whole_from_its_owner")
def test_each_moved_class_is_served_whole_from_its_owner(contract: str) -> None:
    """A class that moved keeps one object: the facade serves the owner's, whose
    ``__module__`` names the owner. Nothing keys behaviour on that module name."""
    subject = _load(contract)
    for name, stem in subject.MOVED_CLASSES.items():
        owner = subject._owner(stem)
        assert getattr(subject.FACADE_MODULE, name) is getattr(owner, name), name
        assert getattr(owner, name).__module__ == owner.__name__, name


@_each_contract("the_class_keeps_every_base_member_and_gains_none")
def test_the_class_keeps_every_base_member_and_gains_none(contract: str) -> None:
    subject = _load(contract)
    members = {
        n
        for n in vars(subject.CLS)
        if (not n.startswith("__") or n == "__init__") and n != "_abc_impl"
    }
    assert members == set(subject.BASE_MEMBERS)


@_each_contract("every_base_name_still_resolves")
def test_every_base_name_still_resolves(contract: str) -> None:
    subject = _load(contract)
    assert sorted(name for name in subject.BASE_NAMES if not hasattr(subject.td, name)) == []


@_each_contract("a_moved_name_is_its_owners_object")
def test_a_moved_name_is_its_owners_object(contract: str) -> None:
    subject = _load(contract)
    for stem, names in subject.MOVED_NAMES.items():
        owner = subject._owner(stem)
        for name in names:
            assert getattr(subject.td, name) is vars(owner)[name], f"{stem}.{name}"


@_each_contract("the_mirror_serves_exactly_the_moved_names_the_facade_does_not_bind")
def test_the_mirror_serves_exactly_the_moved_names_the_facade_does_not_bind(contract: str) -> None:
    subject = _load(contract)
    index = vars(subject.td)[_facade_mirror.INDEX_ATTRIBUTE]
    assert type(subject.td) is _facade_mirror.MirroredModule
    assert set(index.served) == subject.SERVED
    assert set(subject.td._SERVED) == subject.SERVED
    assert subject.td._EXPORTS == dict(index.served)
    assert index.excluded == ()
    for name in subject.SERVED:
        stem = next(stem for stem, names in subject.MOVED_NAMES.items() if name in names)
        assert index.served[name] == f"{subject.FACADE}_{stem}", name


@_each_contract("the_owner_list_is_the_layer_order_and_names_every_owner_on_disk")
def test_the_owner_list_is_the_layer_order_and_names_every_owner_on_disk(contract: str) -> None:
    subject = _load(contract)
    assert subject.td._OWNERS == tuple(f"{subject.FACADE}_{stem}" for stem in subject.OWNERS)
    on_disk = sorted(p.stem for p in subject.PACKAGE_DIR.glob("transport_dispatch_*.py"))
    assert on_disk == sorted(f"transport_dispatch_{stem}" for stem in subject.OWNERS)
    # A package spelled like the facade would also match the queue-drain guard's
    # ``rglob("transport_dispatch.py")``.
    assert not list(subject.PACKAGE_DIR.glob("transport_dispatch_*/"))


@_each_contract("no_owner_takes_a_sync_io_module_through_a_from_import")
def test_no_owner_takes_a_sync_io_module_through_a_from_import(contract: str) -> None:
    subject = _load(contract)
    assert subject._SYNC_IO_MODULES <= set(subject._sync_io_gate().MODULE_FUNCS)
    blind = {
        stem: sorted(found)
        for stem, source in subject._owner_sources().items()
        if (found := subject._from_imported_sync_io_modules(source))
    }
    assert blind == {}


@_each_contract("no_owner_holds_what_the_facade_guards_read_by_path")
def test_no_owner_holds_what_the_facade_guards_read_by_path(contract: str) -> None:
    subject = _load(contract)
    held = {
        stem: subject._facade_pinned_shapes(source)
        for stem, source in subject._owner_sources().items()
    }
    assert not {stem: labels for stem, labels in held.items() if labels}
    in_facade = set(subject._facade_pinned_shapes(subject.FACADE_PATH.read_text(encoding="utf-8")))
    assert in_facade == subject._FACADE_PINNED.keys() - subject._FORBIDDEN_IN_THE_FACADE_TOO


@_each_contract("the_sweep_reports_a_global_the_namespace_does_not_bind")
def test_the_sweep_reports_a_global_the_namespace_does_not_bind(contract: str) -> None:
    subject = _load(contract)
    module_code = compile(
        "def f():\n    def g():\n        return absent\n    return present, g\n", "probe", "exec"
    )
    code = next(c for c in module_code.co_consts if isinstance(c, types.CodeType))
    probe = types.FunctionType(code, {"__name__": "probe", "present": 1})
    assert subject._unbound_globals(probe) == ["absent"]


@_each_contract("no_name_of_the_family_is_excluded_from_the_mirror")
def test_no_name_of_the_family_is_excluded_from_the_mirror(contract: str) -> None:
    """The family came from one namespace, so no name can mean two things in it."""
    subject = _load(contract)
    assert subject._MIRROR.excluded == ()
    assert subject._MIRROR.divergent() == ()


@_each_contract("owners_import_only_owners_listed_before_them")
def test_owners_import_only_owners_listed_before_them(contract: str) -> None:
    """Lowest layer first: an owner reads a sibling only when the sibling comes earlier."""
    subject = _load(contract)
    order = {name: position for position, name in enumerate(subject._MIRROR.owners)}
    edges = 0
    for owner_name in subject._MIRROR.owners:
        for node in ast.walk(subject._tree(importlib.import_module(owner_name))):
            if isinstance(node, ast.ImportFrom) and node.module in order:
                edges += 1
                assert order[node.module] < order[owner_name], (owner_name, node.module)
    assert edges > 0, "no owner imports a sibling, so this case measures nothing"


@_each_contract("the_globstar_matcher_reads_patterns_as_the_reviewers_do")
def test_the_globstar_matcher_reads_patterns_as_the_reviewers_do(contract: str) -> None:
    subject = _load(contract)
    assert subject._globstar("src/a/**/*.py").match("src/a/b.py")
    assert subject._globstar("src/a/**/*.py").match("src/a/x/y/b.py")
    assert not subject._globstar("src/a/*.py").match("src/a/x/b.py")
    assert subject._globstar("src/a/**").match("src/a/x/b.py")


@_each_contract("the_package_init_defines_nothing")
def test_the_package_init_defines_nothing(contract: str) -> None:
    subject = _load(contract)
    init = subject._PARTS_DIR / "__init__.py"
    assert _facade_mirror._defined_names(subject._tree(init)) == set()
    assert [n for n in subject._tree(init).body if not isinstance(n, ast.Expr)] == []


@_each_contract("an_owner_imports_the_facade_only_for_type_checking_and_no_project_module")
def test_an_owner_imports_the_facade_only_for_type_checking_and_no_project_module(
    contract: str,
) -> None:
    subject = _load(contract)
    bad = {
        stem: found
        for stem, path in subject._owner_paths().items()
        if (found := subject._bad_owner_imports(subject._tree(path)))
    }
    assert bad == {}


@_each_contract("the_value_check_can_fail")
def test_the_value_check_can_fail(contract: str) -> None:
    subject = _load(contract)
    assert subject._module_values(ast.parse('"""doc"""\nimport os\nLIMIT = 3\nclass K: ...\n')) == [
        "3: Assign",
        "4: ClassDef",
    ]


@_each_contract("the_owner_set_is_the_package")
def test_the_owner_set_is_the_package(contract: str) -> None:
    subject = _load(contract)
    assert {info.name for info in pkgutil.iter_modules([str(subject._OWNER_DIR)])} == set(
        subject._BASE_OWNERS
    )
