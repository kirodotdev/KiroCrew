"""``kiro_crew.browser_cli.install`` keeps the patch seams and the surface the one module had.

``kiro_crew.browser_cli.install`` is a mirror facade (:mod:`kiro_crew._facade_mirror`) over
the owners in its ``_OWNERS``, lowest layer first: ``install_runner``, ``install_launcher``,
``install_seams`` and ``install_browsers``. It keeps detection and the install steps itself,
and every name an owner defines still resolves as ``kiro_crew.browser_cli.install.<name>``.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules. These
tests pin what is particular to this one: every name the test trees patch through it reaches
every module holding it; the owner list and its layering (``install_job`` sits beside the
owners and is not one of them); that every public name the module held before the split
still type-checks through it; that the owners log as ``kiro_crew.browser_cli.install``; that
``SeamSupport`` and ``InstallScope`` keep the module name they print with; that nothing
outside the family imports an owner; and the import accounting that stands in for the F401
check ``setup.cfg`` switches off for the facade.
"""

from __future__ import annotations

import ast
import dataclasses
import functools
import importlib
import re
import sys
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus
import test_campaign_facades_base_public_names as base_names

import kiro_crew
from kiro_crew import _facade_mirror
from kiro_crew.browser_cli import install

pytestmark = pytest.mark.xdist_group(name="tree_scan_install_facade")

FACADE = "kiro_crew.browser_cli.install"
OWNERS = (
    "kiro_crew.browser_cli.install_runner",
    "kiro_crew.browser_cli.install_launcher",
    "kiro_crew.browser_cli.install_seams",
    "kiro_crew.browser_cli.install_browsers",
)
_SRC = Path(kiro_crew.__file__).resolve().parent

#: Names the test trees are known to patch through the facade, one or more per kind of
#: seam: the child runner every owner reads, a launcher helper, a launcher's module state, a
#: browser-cache helper, a seam constant, a step the facade's own code reads, and names the
#: family only imports. The scan below must find all of them, so a scanner that stops
#: matching goes red instead of passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "_run",
        "cli_path",
        "cli_command",
        "_managed_cli_root",
        "_warned_cli_refusals",
        "_browsers_cache_dir",
        "_required_revisions",
        "_LIFECYCLE_SOURCE_MAX_BYTES",
        "_step",
        "bootstrap_tool_provenance",
        "augmented_path",
        "node_augmented_path",
        "config_dir",
        "find_node_tool",
    }
)

#: A test file worth parsing spells the facade itself, never only an ``install_*`` sibling.
_SPELLS_THE_FACADE = re.compile(
    r"kiro_crew\.browser_cli\.install(?![\w])"
    r"|from\s+kiro_crew\.browser_cli\s+import\s+"
    r"(?:\([^)]*?\binstall\b(?!_)|[^\n]*?\binstall\b(?!_))"
)

#: Every name the facade's import statements bound before its owners moved out. They stay
#: bound here because callers and tests read and patch them as attributes of this module.
_BASE_IMPORTS = frozenset("""
    Any Callable Iterator Path annotations augmented_path config_dir contextlib enum
    find_node_tool github_runner json logging lru_cache node_augmented_path os os_deps
    platform platform_compat re redact_credentials redact_exfiltration_urls shutil stat
    subprocess tempfile threading
    """.split())

#: The names the facade's own glue imports: the owner loop, the mirror, its manifest guard.
_GLUE_IMPORTS = frozenset({"TYPE_CHECKING", "_facade_mirror", "_importlib", "_sys"})

#: The logger every owner names, so a record reads as the one module's did.
_LOGGER_NAME = FACADE


def _index() -> _facade_mirror.MirrorIndex:
    index = vars(install)[_facade_mirror.INDEX_ATTRIBUTE]
    assert isinstance(index, _facade_mirror.MirrorIndex)
    return index


def _family() -> list[ModuleType]:
    return [install, *(sys.modules[name] for name in _index().owners)]


def _source(module: ModuleType) -> str:
    assert module.__file__ is not None
    return Path(module.__file__).read_text(encoding="utf-8")


def _tree(module: ModuleType) -> ast.Module:
    return ast.parse(_source(module))


def patched_names(source: str) -> set[str]:
    """Every name *source* patches through the facade, read by the guards' own reader."""
    return facade_mirror_scan.patched_names(source, {FACADE: FACADE}).get(FACADE, set())


# --------------------------------------------------------------------------- #
# The patch scan
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "from kiro_crew.browser_cli import install as mod\n"
            "def t(monkeypatch):\n    monkeypatch.setattr(mod, 'A', 1)\n",
            {"A"},
        ),
        (
            "import kiro_crew.browser_cli.install as inst\n"
            "from unittest import mock\n"
            "def t():\n    with mock.patch.object(inst, 'B'):\n        pass\n",
            {"B"},
        ),
        (
            "from unittest.mock import patch\n"
            "def t():\n    with patch('kiro_crew.browser_cli.install.C'):\n        pass\n",
            {"C"},
        ),
        (
            "def t(monkeypatch):\n"
            "    monkeypatch.setattr('kiro_crew.browser_cli.install.D', 1)\n",
            {"D"},
        ),
        # An owner is not the facade, even bound under the facade's name.
        (
            "from kiro_crew.browser_cli import install_runner as install\n"
            "def t(monkeypatch):\n    monkeypatch.setattr(install, 'X', 1)\n",
            set(),
        ),
        (
            "from unittest.mock import patch\n"
            "def t():\n    with patch('kiro_crew.browser_cli.install_job.Y'):\n        pass\n",
            set(),
        ),
    ],
    ids=[
        "monkeypatch-from-import",
        "patch-object-import-as",
        "mock-patch-string",
        "dotted-setattr",
        "owner-named-like-the-facade",
        "sibling-string",
    ],
)
def test_the_patch_scan_reads_every_spelling_and_nothing_else(
    source: str, expected: set[str]
) -> None:
    assert patched_names(source) == expected


def _test_tree_files() -> list[Path]:
    root = source_corpus.repo_root()
    test_root = root / "test"
    out = []
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        if rel == "conftest.py" or path.is_relative_to(test_root) or "/tests/" in rel:
            out.append(path)
    return out


def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It reads the whole test tree, so a leaf-only diff must still run it."""
    root = source_corpus.repo_root()
    monkeypatch.syspath_prepend(str(root / "scripts"))
    leaf_test_scope = importlib.import_module("leaf_test_scope")
    this_file = Path(__file__).resolve().relative_to(root).as_posix()
    assert this_file in leaf_test_scope.corpus_gates(root)


@functools.lru_cache(maxsize=1)
def _corpus_patched_names() -> frozenset[str]:
    """Every name the test trees patch through the facade, scanned once per worker."""
    found: set[str] = set()
    for path in _test_tree_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if _SPELLS_THE_FACADE.search(text):
            found |= patched_names(text)
    return frozenset(found)


def test_the_patch_scan_finds_every_known_seam() -> None:
    """Non-vacuity: a scan that found nothing would pass the reach tests over nothing."""
    assert sorted(_KNOWN_SEAMS - _corpus_patched_names()) == []


# --------------------------------------------------------------------------- #
# Reach: a patch through the facade lands on every module holding the name
# --------------------------------------------------------------------------- #


def _reach_gaps(names: set[str], index: _facade_mirror.MirrorIndex) -> list[str]:
    """``owner.name`` for each patched name an owner binds that the index does not route."""
    return [
        f"{module.__name__}.{name}"
        for name in sorted(names)
        for module in _family()[1:]
        if name in vars(module) and module.__name__ not in index.holders.get(name, ())
    ]


def test_every_scanned_name_is_routed_to_every_owner_holding_it() -> None:
    assert _reach_gaps(set(_corpus_patched_names()), _index()) == []


def test_the_reach_scan_can_fail() -> None:
    """The routing check reads the index: blind one name and the gap shows."""
    index = _index()
    name, holders = next((n, h) for n, h in sorted(index.holders.items()) if h)
    blind = dataclasses.replace(index, holders={**index.holders, name: ()})
    assert _reach_gaps({name}, blind) == [f"{owner}.{name}" for owner in holders]


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    family = _family()
    missed: dict[str, list[str]] = {}
    for name in sorted(_corpus_patched_names()):
        if not hasattr(install, name) or isinstance(getattr(install, name), ModuleType):
            continue  # a shared module (os, subprocess) is one object in every holder
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(install, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(install, name) is not sentinel:
                stale.append(FACADE)
        if stale:
            missed[name] = stale
    assert missed == {}
    assert _index().divergent() == ()


@pytest.mark.parametrize(
    ("name", "readers"),
    [
        # ``cli_env`` builds every child's PATH from these, and moved to the runner.
        ("augmented_path", {FACADE, "kiro_crew.browser_cli.install_runner"}),
        ("node_augmented_path", {FACADE, "kiro_crew.browser_cli.install_runner"}),
        # ``install`` resolves npm and node with it; ``_node_version`` moved to the launcher.
        ("find_node_tool", {FACADE, "kiro_crew.browser_cli.install_launcher"}),
        # The managed tools leaf is placed under it; the launcher builds that path.
        ("config_dir", {FACADE, "kiro_crew.browser_cli.install_launcher"}),
    ],
)
def test_a_facade_write_of_an_imported_name_reaches_the_owner_that_reads_it(
    name: str, readers: set[str]
) -> None:
    """The family only imports these, so the facade's own binding is not the one the moved
    code reads: the write has to land in the owner too."""
    holders = [m for m in _family() if name in vars(m)]
    assert {m.__name__ for m in holders} >= readers
    sentinel = object()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(install, name, sentinel)
        assert [m.__name__ for m in holders if vars(m)[name] is not sentinel] == []
    assert all(vars(m)[name] is not sentinel for m in holders)


# --------------------------------------------------------------------------- #
# The owners and their layering
# --------------------------------------------------------------------------- #


def test_the_mirror_indexes_exactly_the_owners_with_no_exclusion_and_no_forward() -> None:
    index = _index()
    assert index.owners == OWNERS == install._OWNERS
    assert index.excluded == ()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports) == install._EXPORTS
    assert index.divergent() == ()


def test_the_owners_are_every_install_sibling_but_the_install_job() -> None:
    """``install_job`` is the dashboard's job record, a caller of this facade, not an owner."""
    assert install.__file__ is not None
    siblings = sorted(
        f"kiro_crew.browser_cli.{path.stem}"
        for path in Path(install.__file__).parent.glob("install_*.py")
    )
    assert siblings == sorted({*OWNERS, "kiro_crew.browser_cli.install_job"})


def _project_imports(tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def _layering_gaps(trees: dict[str, ast.Module]) -> dict[str, list[str]]:
    """Owner -> the family modules it imports that it may not: the facade, or an owner
    listed at or after it."""
    gaps: dict[str, list[str]] = {}
    for position, owner in enumerate(OWNERS):
        allowed = set(OWNERS[:position])
        reached = _project_imports(trees[owner]) & {FACADE, *OWNERS}
        bad = sorted(reached - allowed)
        if bad:
            gaps[owner] = bad
    return gaps


def test_each_owner_imports_only_owners_listed_before_it_and_never_the_facade() -> None:
    trees = {name: _tree(sys.modules[name]) for name in OWNERS}
    assert _layering_gaps(trees) == {}


def test_the_layering_check_can_fail() -> None:
    trees = {name: _tree(sys.modules[name]) for name in OWNERS}
    trees[OWNERS[0]] = ast.parse(
        "from kiro_crew.browser_cli.install_browsers import browser_status\n"
    )
    trees[OWNERS[1]] = ast.parse("import kiro_crew.browser_cli.install\n")
    assert _layering_gaps(trees) == {
        OWNERS[0]: [OWNERS[3]],
        OWNERS[1]: [FACADE],
    }


# --------------------------------------------------------------------------- #
# Who imports an owner
# --------------------------------------------------------------------------- #


def _imported_modules(node: ast.Import | ast.ImportFrom, module: str) -> list[str]:
    """Dotted names an import statement in *module* reaches, relative spellings resolved."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    base = node.module or ""
    if node.level:
        package = module.split(".")[: -node.level]
        base = ".".join([*package, *([node.module] if node.module else [])])
    return [base, *(f"{base}.{alias.name}" for alias in node.names)]


def _owner_importers(sources: Iterable[tuple[Path, str]], root: Path) -> set[str]:
    """The files among *sources* (relative to *root*) whose code imports a family owner."""
    found = set()
    for path, text in sources:
        rel = path.relative_to(root)
        module = ".".join([root.name, *rel.with_suffix("").parts])
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.Import, ast.ImportFrom)) and any(
                name in OWNERS for name in _imported_modules(node, module)
            ):
                found.add(rel.as_posix())
    return found


def test_only_the_family_imports_an_owner() -> None:
    """Callers detect, install and probe the CLI through the facade, so a patch through it
    reaches what they run; an outside module importing an owner would hold bindings no
    facade patch reaches."""
    family = {f"browser_cli/{name.rpartition('.')[2]}.py" for name in (FACADE, *OWNERS)}
    sources = source_corpus.iter_candidate_sources(require_all=("install_",))
    importers = _owner_importers(
        ((path, text) for path, text in sources if path.is_relative_to(_SRC)), _SRC
    )
    assert "browser_cli/install_seams.py" in importers  # owners import the owners below them
    assert importers - family == set()


def test_the_owner_import_scan_finds_an_outside_importer(tmp_path: Path) -> None:
    root = tmp_path / "kiro_crew"
    sources = {
        root / "pkg" / "caller.py": "from kiro_crew.browser_cli.install_runner import _run\n",
        root / "other.py": "from kiro_crew.browser_cli import install_launcher as low\n",
        root / "browser_cli" / "relative.py": "from .install_seams import SeamSupport\n",
        root / "browser_cli" / "deep" / "up.py": "from .. import install_browsers\n",
        root / "facade_user.py": "from kiro_crew.browser_cli.install import cli_path\n",
        root / "job_user.py": "from kiro_crew.browser_cli import install_job\n",
    }
    assert _owner_importers(sources.items(), root) == {
        "pkg/caller.py",
        "other.py",
        "browser_cli/relative.py",
        "browser_cli/deep/up.py",
    }


# --------------------------------------------------------------------------- #
# Static visibility
# --------------------------------------------------------------------------- #


def _home_imports() -> dict[str, tuple[str, str]]:
    """Names an owner imports from a project module outside the family."""
    family = set(OWNERS)
    found: dict[str, tuple[str, str]] = {}
    for owner in OWNERS:
        for node in _tree(sys.modules[owner]).body:
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                if not node.module.startswith("kiro_crew") or node.module in family:
                    continue
                for alias in node.names:
                    if f"{node.module}.{alias.name}" not in family:
                        found.setdefault(alias.asname or alias.name, (node.module, alias.name))
    return found


def _home_manifest() -> dict[str, tuple[str, str]]:
    """The facade's ``TYPE_CHECKING`` imports from outside the family."""
    out: dict[str, tuple[str, str]] = {}
    for node in _tree(install).body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for stmt in node.body:
                if isinstance(stmt, ast.ImportFrom) and stmt.module not in set(OWNERS):
                    for alias in stmt.names:
                        out[alias.asname or alias.name] = (stmt.module or "", alias.name)
    return out


def test_every_name_served_from_outside_the_family_is_visible_to_type_checkers() -> None:
    """A name an owner imports from a project module and the facade serves without
    binding is named from its home under ``TYPE_CHECKING`` (none today: the facade
    keeps binding every project import it had)."""
    assert _home_imports(), "the owners import nothing from the project -- the scan went blind"
    served = set(_index().exports)
    assert _home_manifest() == {n: h for n, h in _home_imports().items() if n in served}


def _statically_bound(tree: ast.Module) -> set[str]:
    """Names a type checker sees on the module: every module-level binding, the
    ``TYPE_CHECKING`` imports included."""
    names = set(_facade_mirror._defined_names(tree))
    for stmt in _facade_mirror._module_level_statements(tree):
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            names |= {alias.asname or alias.name.split(".")[0] for alias in stmt.names}
    return names


def test_every_base_public_name_type_checks_through_the_facade() -> None:
    missing = base_names._BASE_PUBLIC_NAMES[FACADE] - _statically_bound(_tree(install))
    assert sorted(missing) == []


def test_every_served_name_type_checks_through_the_facade() -> None:
    """Callers import moved names from here (``CLI_NOT_INSTALLED_REASON``,
    ``legacy_launcher_refused``, ``SeamSupport``, ...), so mypy must see each one."""
    assert sorted(set(_index().exports) - _statically_bound(_tree(install))) == []


# --------------------------------------------------------------------------- #
# The logger, the moved classes' module, and the import accounting
# --------------------------------------------------------------------------- #


def _logger_names(tree: ast.Module) -> list[str]:
    """The argument of every ``logging.getLogger(...)`` call, unparsed."""
    return [
        ast.unparse(node.args[0]) if node.args else ""
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "logging.getLogger"
    ]


def test_the_family_logs_under_the_facade_name() -> None:
    """The moved code logs as the one module did: an owner's ``__name__`` would be a
    logger no filter, level or capture names (``caplog.at_level(logger=...)``). The
    facade spells it too, so a run as ``__main__`` still binds one logger object."""
    names = [n for module in _family() for n in _logger_names(_tree(module))]
    assert len(names) >= 2, "the scan found too few logger calls -- it went blind"
    assert set(names) == {repr(_LOGGER_NAME)}
    assert {vars(m).get("logger", install.logger) for m in _family()} == {install.logger}


def test_the_logger_scan_can_fail() -> None:
    tree = ast.parse("import logging\nlogging.getLogger(__name__).warning('x')\n")
    assert _logger_names(tree) == ["__name__"]


@pytest.mark.parametrize(
    ("name", "owner"),
    [
        ("SeamSupport", "kiro_crew.browser_cli.install_seams"),
        ("InstallScope", "kiro_crew.browser_cli.install_runner"),
    ],
)
def test_the_moved_classes_keep_the_facade_module(name: str, owner: str) -> None:
    """``repr`` and pickle render a class as ``module.qualname``: both name the one import
    path, and that path resolves back to the class itself, which is what a pickle load does."""
    value = vars(sys.modules[owner])[name]
    assert getattr(install, name) is value
    assert value.__module__ == FACADE
    assert value.__qualname__ == name
    assert getattr(importlib.import_module(value.__module__), value.__qualname__) is value


def test_an_install_scope_reprs_under_the_facade_name() -> None:
    assert repr(install.InstallScope()).startswith(f"<{FACADE}.InstallScope object at ")


def unaccounted_imports(source: str, owner_names: dict[str, set[str]], read: set[str]) -> list[str]:
    """Names *source* binds by a runtime import that nothing accounts for.

    An import is accounted for when it is one the module had before its split, one the
    glue needs, a name its owner defines, or a name the family's own code reads. Imports
    under ``if TYPE_CHECKING:`` bind nothing at run time and are the mirror guard's
    business, so they are skipped.
    """
    found: list[str] = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            continue
        if isinstance(node, ast.Import):
            bound = [(alias.asname or alias.name.split(".")[0], None) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            bound = [(alias.asname or alias.name, node.module) for alias in node.names]
        else:
            continue
        for name, module in bound:
            if module in owner_names:
                if name not in owner_names[module]:
                    found.append(f"{module}.{name}")
            elif name not in _BASE_IMPORTS | _GLUE_IMPORTS | read:
                found.append(name)
    return found


def _owner_names() -> dict[str, set[str]]:
    return {owner: _facade_mirror._defined_names(_tree(sys.modules[owner])) for owner in OWNERS}


def _family_reads() -> set[str]:
    """Every name the family's code reads: a ``Name`` load anywhere in the facade or an
    owner, the ``TYPE_CHECKING`` manifest left out."""
    reads: set[str] = set()
    for module in _family():
        tree = _tree(module)
        manifest = {
            id(sub)
            for node in tree.body
            if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING"
            for sub in ast.walk(node)
        }
        reads |= {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and id(node) not in manifest
        }
    return reads


def test_every_facade_import_is_accounted_for() -> None:
    assert unaccounted_imports(_source(install), _owner_names(), _family_reads()) == []


def test_the_import_accounting_reports_a_planted_import() -> None:
    """The F401 stand-in must be able to fail: a stray import nothing reads, and an owner
    import of a name that owner does not define, are both reported."""
    planted = (
        _source(install)
        + "\nimport pickle\nfrom kiro_crew.browser_cli.install_runner import cli_path\n"
    )
    assert unaccounted_imports(planted, _owner_names(), _family_reads()) == [
        "pickle",
        "kiro_crew.browser_cli.install_runner.cli_path",
    ]
