"""Every facade that serves a ``kiro_crew._facade_mirror`` mirror holds to one contract.

The facades are found by importing the ``src/kiro_crew`` modules that mention the mirror
and keeping those that carry an index, so a facade that adopts it is covered on the
commit that adopts it, and a census of ``install(`` calls, from a walk of its own, checks
that discovery. Each facade must bind one object per indexed name, declare in its
``TYPE_CHECKING`` manifest every owner-defined name it forwards, keep its owners from
importing it, rebind no shared global at run time (nor, in its own code, a name it
serves), keep any module-level ``__getattr__`` of its own off the names the mirror holds,
and leave no stray module in its owner package; and every name the test trees patch
through it must be one the mirror routes and reach every owner that binds it.

Three patch shapes are refused across the test trees. ``mock.patch(..., create=True)`` on
a name the mirror forwards deletes it from every holder on exit and never writes it
back; a direct patch on one owner of a name several members bind lets the facade's undo
leak that owner's value into the others; and a write to a facade's namespace dict skips
the mirror altogether. The patches are read by ``facade_mirror_scan``. Every check and
both of its readers run on the synthetic family in ``facade_mirror_helpers`` first, both
ways, so a check that stopped measuring reddens there.
"""

from __future__ import annotations

import ast
import builtins
import dataclasses
import functools
import importlib
import importlib.util
import pkgutil
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from types import ModuleType
from unittest import mock

import facade_mirror_scan
import pytest
import source_corpus
from facade_mirror_helpers import Family, build_family, second_facade
from facade_mirror_scan import (
    _DYNAMIC,
    PatchSite,
    _admitted,
    _Bindings,
    _mentions,
    create_true_patches,
    create_true_sites,
    global_reads,
    may_create,
    names_by_target,
    needs_syntax_tree,
    patch_events,
    patched_names,
    text_patched_names,
)

from kiro_crew import _facade_mirror

# The checks and both guards parse the test trees; keep them on one worker.
pytestmark = pytest.mark.xdist_group(name="tree_scan_facade_mirror_guards")


@pytest.fixture
def family(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Family:
    return build_family(tmp_path, monkeypatch, request)


# --------------------------------------------------------------------------- #
# Every facade holds to the contract
# --------------------------------------------------------------------------- #


def manifest_mismatches(index: _facade_mirror.MirrorIndex, sources: Mapping[str, str]) -> list[str]:
    """How the facade's ``TYPE_CHECKING`` manifest differs from what it forwards.

    *sources* maps the facade and each owner to its text. The manifest must import every
    name an owner defines and the facade forwards, from that owner, and nothing else.
    """
    manifest: dict[str, str] = {}
    for node in ast.parse(sources[index.facade]).body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for sub in node.body:
                if isinstance(sub, ast.ImportFrom) and sub.module in index.owners:
                    manifest.update({alias.name: sub.module for alias in sub.names})
    expected = {
        name: owner
        for owner in index.owners
        for name in _facade_mirror._defined_names(ast.parse(sources[owner]))
        if name in index.exports
    }
    problems = [
        f"{name}: forwarded from {owner}, manifest has {manifest.get(name)}"
        for name, owner in sorted(expected.items())
        if manifest.get(name) != owner
    ]
    problems += [
        f"{name}: in the manifest, not forwarded"
        for name in sorted(set(manifest) - set(expected) - set(index.forwards))
    ]
    return problems


def owner_imports_of_facade(
    index: _facade_mirror.MirrorIndex, sources: Mapping[str, str]
) -> list[str]:
    """The owners whose source imports their facade outside ``if TYPE_CHECKING:``."""
    head, _, leaf = index.facade.rpartition(".")
    found = set()
    for owner in index.owners:
        tree = ast.parse(sources[owner])
        typing_only = {
            id(sub)
            for node in ast.walk(tree)
            if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING"
            for stmt in node.body
            for sub in ast.walk(stmt)
        }
        for node in ast.walk(tree):
            if id(node) in typing_only:
                continue  # an annotation-only import, as of a class the facade still holds
            if isinstance(node, ast.ImportFrom) and (
                node.module == index.facade
                or (node.module == head and leaf in {alias.name for alias in node.names})
            ):
                found.add(owner)
            elif isinstance(node, ast.Import) and index.facade in {a.name for a in node.names}:
                found.add(owner)
    return sorted(found)


def shared_global_rebinds(
    index: _facade_mirror.MirrorIndex, sources: Mapping[str, str]
) -> list[str]:
    """``global`` statements in the family that rebind a name another member also binds."""
    shared = {
        name
        for name, holders in index.holders.items()
        if len(holders) > 1 or name not in index.exports
    }
    found = []
    for module_name, text in sorted(sources.items()):
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Global):
                found += [f"{module_name}: global {name}" for name in node.names if name in shared]
    return sorted(found)


def facade_global_shadows(
    index: _facade_mirror.MirrorIndex, sources: Mapping[str, str]
) -> list[str]:
    """``global`` statements in the facade's own code naming a name it serves unbound.

    Such a write binds the name on the facade, where it shadows the owners for every read
    through the facade, while the owners' own code keeps reading theirs.
    """
    served = set(index.served)
    found = []
    for node in ast.walk(ast.parse(sources[index.facade])):
        if isinstance(node, ast.Global):
            found += [f"{index.facade}: global {name}" for name in node.names if name in served]
    return sorted(found)


def resolver_overlaps(facade: ModuleType) -> list[str]:
    """Names the mirror holds that the facade's own module-level ``__getattr__`` answers.

    :func:`~kiro_crew._facade_mirror.install` refuses such a resolver when the facade
    defines it first; this catches one defined after the install.
    """
    index: _facade_mirror.MirrorIndex = vars(facade)[_facade_mirror.INDEX_ATTRIBUTE]
    resolver = vars(facade).get("__getattr__")
    if not callable(resolver):
        return []
    held = sorted({*index.holders, *index.forwards})
    return [name for name in held if _facade_mirror._answers(facade, resolver, name)]


def unrouted_patches(index: _facade_mirror.MirrorIndex, names: Iterable[str]) -> list[str]:
    """Patched names an owner binds that the mirror does not route to the owners.

    An excluded name, or one outside the facade's surface: a patch of it through the
    facade lands on the facade alone, so every owner reading it keeps the original.
    """
    owners = [importlib.import_module(owner) for owner in index.owners]
    problems = []
    for name in sorted(set(names)):
        if name in index.holders or name in index.forwards or _facade_mirror._is_dunder(name):
            continue
        binding = [owner.__name__ for owner in owners if name in vars(owner)]
        if binding:
            why = "excluded" if name in index.excluded else "outside the index"
            problems.append(f"{name}: {why}, so a patch through the facade misses {binding}")
    return problems


def _package_modules(package_name: str) -> set[str]:
    package = importlib.import_module(package_name)
    return {f"{package_name}.{info.name}" for info in pkgutil.iter_modules(package.__path__)}


def owner_package_strays(
    index: _facade_mirror.MirrorIndex,
    package_modules: Callable[[str], set[str]] = _package_modules,
) -> list[str]:
    """Modules of the owners' own package that the facade does not list as owners.

    When every owner lives in one package of its own, a module added there and left out
    of the owner list is invisible to the mirror: a patch through the facade misses it.
    Owners that sit beside the facade, in its own package, are not held to this.
    """
    parents = {owner.rpartition(".")[0] for owner in index.owners}
    if len(parents) != 1 or parents == {index.facade.rpartition(".")[0]}:
        return []
    return sorted(package_modules(parents.pop()) - set(index.owners))


def patch_reach_failures(facade: ModuleType, names: Iterable[str]) -> list[str]:
    """Each name patched through *facade* that does not reach every member binding it."""
    index: _facade_mirror.MirrorIndex = vars(facade)[_facade_mirror.INDEX_ATTRIBUTE]
    owners = [importlib.import_module(owner) for owner in index.owners]
    failures: list[str] = []
    for name in sorted(set(names)):
        if name in index.excluded:
            failures.append(f"{name}: excluded, so a patch through the facade cannot reach it")
            continue
        if name.startswith("__") and name.endswith("__"):
            continue
        if name in index.forwards:
            forward_owner = importlib.import_module(index.forwards[name])
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(facade, name, mock.MagicMock(name=f"reach-{name}"))
                if vars(forward_owner).get(name) is not getattr(facade, name):
                    failures.append(f"{name}: the patch missed {forward_owner.__name__}")
            continue
        before = {module.__name__: name in vars(module) for module in (facade, *owners)}
        nobody = not any(before.values())
        sentinel = mock.MagicMock(name=f"reach-{name}")
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(facade, name, sentinel, raising=False)
            missed = [
                owner.__name__
                for owner in owners
                if (before[owner.__name__] or nobody) and vars(owner).get(name) is not sentinel
            ]
            if getattr(facade, name) is not sentinel:
                missed.append(facade.__name__)
            if missed:
                failures.append(f"{name}: the patch missed {missed}")
        after = {module.__name__: name in vars(module) for module in (facade, *owners)}
        if after != before:
            failures.append(f"{name}: the undo left {after}, not {before}")
    return failures


def _family_sources(index: _facade_mirror.MirrorIndex) -> dict[str, str]:
    sources = {}
    for name in (index.facade, *index.owners):
        path = importlib.import_module(name).__file__
        assert path is not None
        sources[name] = Path(path).read_text(encoding="utf-8")
    return sources


def _module_name(path: Path, root: Path | None = None) -> str:
    root = root or source_corpus.src_root()
    parts = [root.name, *path.relative_to(root).with_suffix("").parts]
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def mirrored_facades() -> dict[str, ModuleType]:
    """Every ``src/kiro_crew`` module that serves a mirror, by importing the candidates.

    Found at run time rather than by spelling: a module mentioning the mirror is imported
    and kept when it carries an index, so any way of calling ``install`` is covered.
    """
    found: dict[str, ModuleType] = {}
    for path, _text in source_corpus.iter_candidate_sources(require_all=("_facade_mirror",)):
        name = _module_name(path)
        if name == _facade_mirror.__name__:
            continue
        module = importlib.import_module(name)
        index = vars(module).get(_facade_mirror.INDEX_ATTRIBUTE)
        if isinstance(index, _facade_mirror.MirrorIndex):
            found[name] = module
    return found


_FACADES = mirrored_facades()


def test_the_contract_checks_measure_the_synthetic_family_both_ways(family: Family) -> None:
    index, sources = family.index, family.sources
    low, facade = family.low.__name__, family.facade.__name__
    assert manifest_mismatches(index, sources) == []
    assert owner_imports_of_facade(index, sources) == []
    assert shared_global_rebinds(index, sources) == []
    assert patch_reach_failures(family.facade, ["LIMIT", "helper", "open", "brand_new"]) == []

    short = dict(
        sources, **{facade: sources[facade].replace(f"from {low} import low_reads_limit", "")}
    )
    assert manifest_mismatches(index, short) == [
        f"low_reads_limit: forwarded from {low}, manifest has None"
    ]
    second, _legacy = second_facade(family)
    assert patch_reach_failures(second, ["LEGACY", "LIMIT"]) == []
    assert unreached_readers(index, sources) == []
    reading = dict(
        sources, **{family.high.__name__: sources[family.high.__name__] + "\nX = low_dumps\n"}
    )
    assert unreached_readers(index, reading) == [
        f"{family.high.__name__} reads low_dumps without binding it"
    ]
    importing = dict(sources, **{low: sources[low] + f"\nfrom {facade} import ONLY_HERE\n"})
    assert owner_imports_of_facade(index, importing) == [low]
    annotating = f"\nif TYPE_CHECKING:\n    from {facade} import ONLY_HERE\n"
    assert owner_imports_of_facade(index, dict(sources, **{low: sources[low] + annotating})) == []
    rebinding = {
        low: "def f():\n    global LIMIT\n    LIMIT = 4\n",
        "x": "def g():\n    global low_dumps\n",
    }
    assert shared_global_rebinds(index, rebinding) == [f"{low}: global LIMIT"]
    assert owner_package_strays(index) == [], "owners beside their facade are not held to it"
    packaged = dataclasses.replace(index, owners=("pkg.parts.low", "pkg.parts.high"))
    listing = {"pkg.parts.low", "pkg.parts.high", "pkg.parts.stray"}
    assert owner_package_strays(packaged, lambda name: listing) == ["pkg.parts.stray"]
    assert patch_reach_failures(family.facade, ["CLASH"]) == [
        "CLASH: excluded, so a patch through the facade cannot reach it"
    ]
    assert facade_global_shadows(index, sources) == []
    shadowing = dict(sources, **{facade: sources[facade] + "\ndef f():\n    global LIMIT\n"})
    assert facade_global_shadows(index, shadowing) == [f"{facade}: global LIMIT"]
    bound_here = dict(sources, **{facade: sources[facade] + "\ndef f():\n    global helper\n"})
    assert facade_global_shadows(index, bound_here) == [], "the facade binds helper itself"
    assert resolver_overlaps(family.facade) == []
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(vars(family.facade), "__getattr__", lambda name: 1)
        assert "LIMIT" in resolver_overlaps(family.facade)
    assert unrouted_patches(index, ["LIMIT", "helper", "open", "ONLY_HERE"]) == []
    assert unrouted_patches(index, ["CLASH"]) == [
        f"CLASH: excluded, so a patch through the facade misses [{low!r}, {family.high.__name__!r}]"
    ]
    surfaced, _legacy = second_facade(family, surface={"LIMIT", "CLASH"})
    surface_index = vars(surfaced)[_facade_mirror.INDEX_ATTRIBUTE]
    assert unrouted_patches(surface_index, ["LIMIT", "LEGACY", "low_dumps"]) == [
        f"low_dumps: outside the index, so a patch through the facade misses [{low!r}]"
    ]
    holders_without_high = dict(index.holders, LIMIT=(low,))
    blind = dataclasses.replace(index, holders=holders_without_high)
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(vars(family.facade), _facade_mirror.INDEX_ATTRIBUTE, blind)
        missed = patch_reach_failures(family.facade, ["LIMIT"])
    assert missed == [f"LIMIT: the patch missed ['{family.high.__name__}']"]


def test_every_mirrored_facade_is_installed_and_holds_one_object_per_name() -> None:
    for name, facade in _FACADES.items():
        assert type(facade) is _facade_mirror.MirroredModule, name
        assert vars(facade)[_facade_mirror.INDEX_ATTRIBUTE].divergent() == (), name


def test_every_mirrored_facade_manifest_names_what_it_forwards() -> None:
    for name, facade in _FACADES.items():
        index = vars(facade)[_facade_mirror.INDEX_ATTRIBUTE]
        assert manifest_mismatches(index, _family_sources(index)) == [], name


def test_no_owner_imports_its_facade_or_rebinds_a_shared_global() -> None:
    for name, facade in _FACADES.items():
        index = vars(facade)[_facade_mirror.INDEX_ATTRIBUTE]
        sources = _family_sources(index)
        assert owner_imports_of_facade(index, sources) == [], name
        assert shared_global_rebinds(index, sources) == [], name
        assert facade_global_shadows(index, sources) == [], name
        assert owner_package_strays(index) == [], name
        assert resolver_overlaps(facade) == [], name


def test_no_mirrored_facade_forwards_a_builtin_name() -> None:
    """``mock.patch`` creates a builtin-named module attribute by itself, so a forwarded
    builtin-named name would lose itself on every patch's exit."""
    builtin_names = frozenset(name for name in dir(builtins) if not name.startswith("_"))
    clashing = {}
    for name, facade in sorted(_FACADES.items()):
        served = vars(facade)[_facade_mirror.INDEX_ATTRIBUTE].served
        found = sorted(set(served) & builtin_names)
        if found:
            clashing[name] = found
    assert clashing == {}


# --------------------------------------------------------------------------- #
# Reading patch calls out of test sources
# --------------------------------------------------------------------------- #


def test_the_spelling_prefilter_admits_every_reach_and_not_the_bare_word() -> None:
    target = "pkg.sub.state"
    admitted = (
        "import pkg.sub.state as st\n",
        "from pkg.sub import other, state\n",
        "from pkg.sub import (\n    other,\n    state,\n)\n",
        "from pkg import sub\nsub.state.LIMIT = 1\n",
        "importlib.import_module('.state', 'pkg.sub')\n",
        "mock.patch('pkg.sub.state.f')\n",
    )
    for text in admitted:
        assert _mentions(text, [target]), text
    for text in ("the state of the slot\n", "from pkg.other import state\n"):
        assert not _mentions(text, [target]), text


_FACADE_NAME = "pkg.facade"
_SPELLINGS = {_FACADE_NAME: _FACADE_NAME, "pkg.handler.facade": _FACADE_NAME}
_DETECTOR_CASES: tuple[tuple[str, list[tuple[str, str]], str], ...] = (
    (
        "from unittest import mock\nmock.patch('pkg.facade.f', create=True)\n",
        [(_FACADE_NAME, "f")],
        "a string target",
    ),
    (
        "from unittest.mock import patch\nimport pkg.facade as fa\n"
        "patch.object(fa, 'f', create=True)\n",
        [(_FACADE_NAME, "f")],
        "patch.object through an import alias",
    ),
    (
        "from unittest import mock\nfrom pkg import (\n    other,\n    facade,\n)\n"
        "mock.patch.object(facade, 'f', None, None, True)\n",
        [(_FACADE_NAME, "f")],
        "create by position, through a parenthesised multi-name import",
    ),
    (
        "import importlib\nfrom unittest import mock\nM = 'pkg.facade'\n"
        "fa = importlib.import_module(M)\nmock.patch(f'{M}.f', create=True)\n"
        "mock.patch.multiple(fa, create=True, g=1)\n",
        [(_FACADE_NAME, "f"), (_FACADE_NAME, "g")],
        "an f-string, a module constant and patch.multiple",
    ),
    (
        "def test_x(mocker):\n    mocker.patch('pkg.facade.' + 'f', create=True)\n",
        [(_FACADE_NAME, "f")],
        "pytest-mock's mocker and concatenation",
    ),
    (
        "from unittest import mock\ndef test_x(flag):\n"
        "    mock.patch('pkg.facade.f', create=flag)\n",
        [(_FACADE_NAME, "f")],
        "a create argument read at run time",
    ),
    (
        "from unittest import mock\ndef test_x(target):\n"
        "    mock.patch.object(target, 'f', create=True)\n",
        [(_DYNAMIC, "f")],
        "a target the file cannot spell",
    ),
    (
        "from unittest import mock\nimport pkg.other as mod\ndef test_x(mod):\n"
        "    mock.patch.object(mod, 'f', create=True)\n",
        [(_DYNAMIC, "f")],
        "a parameter shadowing an import",
    ),
    (
        "from unittest import mock\nimport pkg.other as mod\nimport pkg.facade\n"
        "for mod in (pkg.facade,):\n    mock.patch.object(mod, 'f', create=True)\n",
        [(_DYNAMIC, "f")],
        "a loop target shadowing an import",
    ),
    (
        "from unittest import mock\nT = 'pkg.other.x'\nT = 'pkg.facade.f'\n"
        "mock.patch(T, create=True)\n",
        [(_FACADE_NAME, "f")],
        "a constant rebound to the facade",
    ),
    (
        "from unittest import mock\nmock.patch('pkg.handler.facade.f', create=True)\n",
        [(_FACADE_NAME, "f")],
        "the facade reached as another module's attribute",
    ),
    (
        "import importlib\nfrom unittest import mock\n"
        "h = importlib.import_module('.facade', 'pkg')\nmock.patch.object(h, 'f', create=True)\n",
        [(_FACADE_NAME, "f")],
        "a relative import_module",
    ),
    (
        "from unittest import mock\nimport pkg.facade as fa\n"
        "mock.patch.object(fa, *a, 'x', 'y', False)\n",
        [(_FACADE_NAME, _DYNAMIC)],
        "a starred argument before the create slot",
    ),
    (
        "import functools\nfrom unittest import mock\nimport pkg.facade as fa\n"
        "functools.partial(mock.patch.object, fa, create=True)('f')\n"
        "getattr(mock.patch, 'object')(fa, 'g', create=True)\n",
        [(_FACADE_NAME, "f"), (_FACADE_NAME, "g")],
        "functools.partial and getattr spellings of the patcher",
    ),
    ("from unittest import mock\nmock.patch('pkg.facade.f')\n", [], "create left false"),
    (
        "from unittest import mock\nmock.patch('pkg.facade.f', create=False)\n",
        [],
        "create spelled false",
    ),
    (
        "from unittest import mock\nmock.patch('pkg.other.f', create=True)\n",
        [],
        "another module",
    ),
    (
        "from unittest import mock\nimport pkg.other as fa\n"
        "mock.patch.object(fa, 'f', create=True)\n",
        [],
        "an alias of another module",
    ),
    (
        "from unittest import mock\nclass Fake:\n    pass\n"
        "mock.patch.object(Fake, 'f', create=True)\n",
        [],
        "a test-local class",
    ),
)


@pytest.mark.parametrize(
    ("source", "expected", "why"), _DETECTOR_CASES, ids=[case[2] for case in _DETECTOR_CASES]
)
def test_the_create_true_detector_answers_both_ways(
    source: str, expected: list[tuple[str, str]], why: str
) -> None:
    sites = create_true_patches(source, _SPELLINGS, {_FACADE_NAME: None})
    assert sorted((site.module, site.name) for site in sites) == sorted(expected), why
    if expected and expected[0][0] != _DYNAMIC:
        assert _admitted(source, _SPELLINGS), f"the text prefilter would skip {why}"


def test_the_detector_counts_only_names_the_facade_forwards() -> None:
    source = "from unittest import mock\nmock.patch('pkg.facade.local', create=True)\n"
    assert create_true_patches(source, _SPELLINGS, {_FACADE_NAME: frozenset({"other"})}) == []
    assert create_true_patches(source, _SPELLINGS, {_FACADE_NAME: frozenset({"local"})})


def _test_tree_files() -> Iterator[Path]:
    """Every file pytest collects tests or fixtures from: the root conftest, ``test/``,
    and the test modules and conftests under ``src/kiro_crew/apps/builtins``."""
    root = source_corpus.repo_root()
    test_root = source_corpus.repo_root() / "test"
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        name = path.name
        if rel == "conftest.py" or path.is_relative_to(test_root):
            yield path
        elif rel.startswith("src/kiro_crew/apps/builtins/") and (
            "/tests/" in rel
            or name.startswith("test_")
            or name.endswith("_test.py")
            or name == "conftest.py"
        ):
            yield path


_THIS_FILE = Path(__file__).resolve()
#: Deliberate ``create=True`` premises, as (file, function).
_ALLOWED = frozenset(
    {(_THIS_FILE.name, "test_the_premise_create_true_on_a_forwarded_name_unbinds_every_holder")}
)


def test_the_premise_create_true_on_a_forwarded_name_unbinds_every_holder(
    family: Family,
) -> None:
    """Why the guard below exists: ``create=True`` skips the write-back on exit."""
    with mock.patch.object(family.facade, "low_reads_limit", create=True):
        pass
    assert "low_reads_limit" not in vars(family.low)
    assert not hasattr(family.facade, "low_reads_limit")


def test_the_test_tree_walk_reaches_every_collected_tree() -> None:
    walked = {path.resolve() for path in _test_tree_files()}
    assert _THIS_FILE in walked
    assert source_corpus.repo_root() / "conftest.py" in walked
    assert any("/container_tests/" in path.as_posix() for path in walked)
    assert len(walked) > 1000


def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It reads the whole test tree, so a leaf-only diff must still run it."""
    root = source_corpus.repo_root()
    monkeypatch.syspath_prepend(str(root / "scripts"))
    leaf_test_scope = importlib.import_module("leaf_test_scope")
    assert _THIS_FILE.relative_to(root).as_posix() in leaf_test_scope.corpus_gates(root)


def facade_spellings() -> dict[str, str]:
    """Each spelling of every mirrored facade: its own name, and every src module's
    module-level binding of it (``<module>.<bound name>``)."""
    spellings = {name: name for name in _FACADES}
    if not _FACADES:
        return spellings
    leaves = tuple({name.rpartition(".")[2] for name in _FACADES})
    for path, _text, tree in source_corpus.parsed_candidates(require_any=leaves):
        module = _module_name(path)
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                for alias in node.names:
                    bound = f"{node.module}.{alias.name}"
                    if bound in _FACADES and module != bound:
                        spellings[f"{module}.{alias.asname or alias.name}"] = bound
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in _FACADES and alias.asname:
                        spellings[f"{module}.{alias.asname}"] = alias.name
    return spellings


_SPELLINGS_FOUND = facade_spellings()
_OWNERS_FOUND = {
    owner: name
    for name, facade in _FACADES.items()
    for owner in vars(facade)[_facade_mirror.INDEX_ATTRIBUTE].owners
}


@functools.lru_cache(maxsize=1)
def _family_test_texts() -> tuple[tuple[str, str], ...]:
    """``(path, text)`` of each test-tree file that spells a mirrored facade or an owner,
    read once for the three scans below."""
    names = [*_SPELLINGS_FOUND, *_OWNERS_FOUND]
    root = source_corpus.repo_root()
    found = []
    for path in _test_tree_files() if names else ():
        text = path.read_text(encoding="utf-8", errors="replace")
        if _mentions(text, names):
            found.append((path.relative_to(root).as_posix(), text))
    return tuple(found)


#: One spelling table for every corpus scan -- each facade's spellings and each owner's
#: own name -- so each test file is parsed once for all of them.
_SCAN_SPELLINGS = tuple(sorted({**_SPELLINGS_FOUND, **{o: o for o in _OWNERS_FOUND}}.items()))


@functools.lru_cache(maxsize=None)
def _events_of(path: str, spellings: tuple[tuple[str, str], ...]) -> tuple[PatchSite, ...]:
    """:func:`patch_events` of one corpus file, parsed once per spelling table."""
    text = dict(_family_test_texts())[path]
    try:
        return tuple(patch_events(text, dict(spellings)))
    except SyntaxError:
        return ()


def install_census(root: Path) -> tuple[set[str], set[str]]:
    """``(modules naming the mirror, modules calling _facade_mirror.install()``)`` under
    *root*, read as raw text from a walk of its own, so it shares nothing with discovery."""
    mentions, calls = set(), set()
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "_facade_mirror" in text:
            mentions.add(_module_name(path, root))
            if "_facade_mirror.install(" in text:
                calls.add(_module_name(path, root))
    return mentions, calls


def test_discovery_agrees_with_an_independent_census_of_install_calls(tmp_path: Path) -> None:
    """A count of its own, so a discovery that went blind would read as a mismatch."""
    mentions, calls = install_census(source_corpus.src_root())
    assert _facade_mirror.__name__ in mentions, "the census walk reaches the mirror itself"
    assert set(_FACADES) == calls - {_facade_mirror.__name__}
    synthetic = tmp_path / "kiro_crew"
    (synthetic / "parts").mkdir(parents=True)
    call = "from kiro_crew import _facade_mirror\n_facade_mirror.install(facade, owners)\n"
    (synthetic / "parts" / "facade.py").write_text(call, encoding="utf-8")
    (synthetic / "plain.py").write_text("from kiro_crew import _facade_mirror\n", encoding="utf-8")
    (synthetic / "other.py").write_text("x = 1\n", encoding="utf-8")
    found = install_census(synthetic)
    assert found == ({"kiro_crew.parts.facade", "kiro_crew.plain"}, {"kiro_crew.parts.facade"})


#: Facade -> the test file holding the facade's own ``create=True`` guard over its
#: ``forwards`` table (a facade that adopted the mirror over a forwarding shape it had,
#: and kept that shape's guard). The census here leaves those names to that guard; a
#: facade not listed has every name it serves censused here, its forwards included.
_OWN_CREATE_GUARDS: dict[str, str] = {
    "kiro_crew.acp.client": "test/test_acp_refactor_create_guard.py",
    "kiro_crew.sandbox": "test/test_sandbox_refactor_create_guard.py",
}


def census_names(index: _facade_mirror.MirrorIndex, own_guard: str | None = None) -> frozenset[str]:
    """The names the create=True census guards: every name the mirror serves, minus the
    ``forwards`` table only when *own_guard* names the facade's own create guard."""
    served = set(index.served)
    return frozenset(served - set(index.forwards) if own_guard else served)


def own_guard_problems(guards: Mapping[str, str], root: Path) -> list[str]:
    """Each listed guard that is not a test file naming its facade and ``create=True``."""
    problems = []
    for facade, guard in sorted(guards.items()):
        path = root / guard
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        if facade not in text or "create=True" not in text:
            problems.append(f"{facade}: {guard} is not a create=True guard naming it")
    return problems


def test_the_create_true_census_counts_forwarded_names_unless_a_guard_of_their_own_does(
    family: Family, tmp_path: Path
) -> None:
    second, _legacy = second_facade(family)
    index = vars(second)[_facade_mirror.INDEX_ATTRIBUTE]
    assert {"LIMIT", "LEGACY"} <= census_names(index)
    names = census_names(index, own_guard="test/test_own_guard.py")
    assert "LIMIT" in names and "LEGACY" not in names
    spellings = {second.__name__: second.__name__}
    source = (
        "from unittest import mock\n"
        f"mock.patch('{second.__name__}.LEGACY', create=True)\n"
        f"mock.patch('{second.__name__}.LIMIT', create=True)\n"
    )
    sites = create_true_patches(source, spellings, {second.__name__: names})
    assert [site.name for site in sites] == ["LIMIT"]
    guard = tmp_path / "test" / "test_own_guard.py"
    guard.parent.mkdir()
    guard.write_text(f"# refuses create=True on {second.__name__}\n", encoding="utf-8")
    listed = {second.__name__: "test/test_own_guard.py"}
    assert own_guard_problems(listed, tmp_path) == []
    assert own_guard_problems({second.__name__: "test/missing.py"}, tmp_path) == [
        f"{second.__name__}: test/missing.py is not a create=True guard naming it"
    ]


def test_every_listed_create_guard_is_a_guard_naming_its_facade() -> None:
    assert set(_OWN_CREATE_GUARDS) <= set(_FACADES), "a listed facade no longer serves a mirror"
    assert own_guard_problems(_OWN_CREATE_GUARDS, source_corpus.repo_root()) == []


def test_no_test_patches_a_mirrored_facade_name_with_create_true() -> None:
    forwarded: dict[str, frozenset[str] | None] = {
        name: census_names(
            vars(module)[_facade_mirror.INDEX_ATTRIBUTE], _OWN_CREATE_GUARDS.get(name)
        )
        for name, module in _FACADES.items()
    }
    offenders = []
    scanned = 0
    for path, text in _family_test_texts():
        if not _admitted(text, _SPELLINGS_FOUND):
            continue
        scanned += 1
        if not may_create(text):
            continue
        for site in create_true_sites(_events_of(path, _SCAN_SPELLINGS), forwarded):
            if (Path(path).name, site.function) in _ALLOWED:
                continue
            offenders.append(f"{path}:{site.line} {site.module}.{site.name}")
    assert offenders == [], (
        "mock.patch(..., create=True) on a name a mirrored facade forwards deletes it from "
        "every owner on exit and never writes it back; patch it without create=True:\n"
        + "\n".join(offenders)
    )
    if _FACADES:
        assert scanned > 0, "no test file names a mirrored facade, so the scan measured nothing"


def test_the_create_candidate_check_answers_both_ways() -> None:
    for text in (
        "mock.patch('a.b', create=True)\n",
        "mock.patch.object(a, 'b', **options)\n",
        "mock.patch.object(a, 'b', None, None, True)\n",
        "mock.patch('a.b', None, None, flag)\n",
        "patch.multiple(a, None, True)\n",
    ):
        assert may_create(text), text
    for text in (
        "mock.patch.object(a, 'b', f(1, 2), spec)\n",
        "mock.patch('a.b', new)\n",
        "create_session(state)\n",
    ):
        assert not may_create(text), text


# --------------------------------------------------------------------------- #
# Every patch through a facade reaches its readers
# --------------------------------------------------------------------------- #


def test_the_patch_name_scan_answers_both_ways() -> None:
    source = (
        "from unittest import mock\nimport pkg.facade as fa\nfrom pkg import facade\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(fa, 'a', 1)\n"
        "    fa.b = 2\n"
        "    mock.patch.object(facade, 'c')\n"
        "    monkeypatch.setattr('pkg.facade.d', 3)\n"
        "    monkeypatch.setattr('pkg.handler.facade.e.attr', 4)\n"
        "    setattr(fa, 'f', 5)\n"
        "    monkeypatch.setattr(other, 'g', 6)\n"
        "    monkeypatch.setattr('pkg.other.h', 7)\n"
    )
    assert patched_names(source, _SPELLINGS) == {_FACADE_NAME: {"a", "b", "c", "d", "e", "f"}}
    assert text_patched_names(source, _SPELLINGS) == patched_names(source, _SPELLINGS)
    parenthesised = "from pkg import (\n    other,\n    facade as fx,\n)\nfx.z = 1\n"
    assert text_patched_names(parenthesised, _SPELLINGS) == {_FACADE_NAME: {"z"}}


def test_both_readers_see_namespace_dict_writes_and_sys_modules_subscripts() -> None:
    source = (
        "import sys\nfrom unittest import mock\nimport pkg.facade as fa\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setitem(vars(fa), 'a', 1)\n"
        "    monkeypatch.delitem(fa.__dict__, 'b')\n"
        "    fa.__dict__['c'] = 3\n"
        "    del vars(fa)['d']\n"
        "    mock.patch.dict(fa.__dict__, {'e': 5})\n"
        "    mock.patch.dict(vars(fa), {'f': 6})\n"
        "    monkeypatch.setattr(sys.modules['pkg.facade'], 'g', 7)\n"
        "    sys.modules['pkg.facade'].h = 8\n"
        "    held = sys.modules['pkg.facade']\n"
        "    del held.i\n"
        "    vars(other)['j'] = 10\n"
        "    other.__dict__['k'] = 11\n"
        "    mock.patch.dict(os.environ, {'l': '12'})\n"
    )
    expected = {_FACADE_NAME: set("abcdefghi")}
    assert patched_names(source, _SPELLINGS) == expected
    assert text_patched_names(source, _SPELLINGS) == expected
    events = patch_events(source, _SPELLINGS)
    on_namespace = {e.name for e in events if e.kind == "namespace" and e.module == _FACADE_NAME}
    assert on_namespace == set("abcdef")
    methods = (
        "import pkg.facade as fa\nfa.__dict__.update({'u': 1}, v=2)\nvars(fa).pop('w')\n"
        "vars(fa).clear()\nfa.__dict__.setdefault('x', 1)\n"
        "mock.patch.dict('pkg.facade.__dict__', {'y': 1})\n"
    )
    found = sorted(e.name for e in patch_events(methods, _SPELLINGS) if e.kind == "namespace")
    assert found == sorted([_DYNAMIC, "u", "v", "w", "x"])
    imported = "from unittest import mock\n" + methods.split("\n", 1)[1]
    assert {e.name for e in patch_events(imported, _SPELLINGS)} >= {"y"}


def namespace_writes(events: Iterable[PatchSite], facades: Iterable[str]) -> list[PatchSite]:
    """The writes among *events* to a facade's namespace dict, which bypass its class."""
    wanted = set(facades)
    return [event for event in events if event.kind == "namespace" and event.module in wanted]


def test_the_namespace_write_scan_answers_both_ways(family: Family) -> None:
    facade, low = family.facade.__name__, family.low.__name__
    source = (
        f"import {facade} as fa\nimport {low} as lo\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setitem(vars(fa), 'helper', 1)\n"
        "    monkeypatch.setattr(fa, 'helper', 2)\n"
        "    lo.__dict__['LIMIT'] = 3\n"
        "    lo.__dict__['low_dumps'] = 4\n"
    )
    spellings = {facade: facade, low: low}
    events = patch_events(source, spellings)
    assert [(e.module, e.name) for e in namespace_writes(events, [facade])] == [(facade, "helper")]
    owners = {low: facade}
    shared = {facade: shared_names(family.index)}
    assert direct_owner_patches(source, owners, shared) == [f"{low}.LIMIT"]


def test_the_premise_a_namespace_write_bypasses_the_mirror(family: Family) -> None:
    """Why namespace writes on a facade are refused: no owner sees them."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(vars(family.facade), "LIMIT", 9)
        assert family.facade.LIMIT == 9
        assert family.high.high_reads_limit() == 3


def test_no_test_writes_a_mirrored_facade_namespace_directly() -> None:
    offenders = [
        f"{path}:{event.line} {event.module}.{event.name}"
        for path, event in _corpus_events()
        if event in namespace_writes([event], _FACADES)
    ]
    assert offenders == [], (
        "a write to a mirrored facade's namespace dict (vars(facade)[...], "
        "facade.__dict__[...], mock.patch.dict on either) bypasses the mirror, so no owner "
        "sees it; patch the name through the facade instead:\n" + "\n".join(offenders)
    )


def test_a_patch_through_the_synthetic_facade_reaches_every_reader(family: Family) -> None:
    facade = family.facade.__name__
    package, _, leaf = facade.rpartition(".")
    source = (
        f"from {package} import {leaf}\n"
        "def test_x(monkeypatch):\n"
        f"    monkeypatch.setattr({leaf}, 'helper', 1)\n"
        f"    monkeypatch.setattr({leaf}, 'open', 2, raising=False)\n"
    )
    names = patched_names(source, {facade: facade})[facade]
    assert names == {"helper", "open"}
    assert patch_reach_failures(family.facade, names) == []


def parsed_for_the_corpus(text: str, spellings: Mapping[str, str], owners: Iterable[str]) -> bool:
    """Whether a corpus file is read by :func:`patch_events` as well as by regular expressions.

    When the regular expressions cannot fully read its patches of a facade
    (:func:`needs_syntax_tree`), when it writes a namespace dict or names an owner (the two
    scans that need each site's line), or when it may pass ``create`` to a patch. Every
    other file is read by :func:`text_patched_names` alone, which is where the cost of
    these guards goes when a facade is spelled in hundreds of test files.
    """
    return (
        needs_syntax_tree(text, spellings)
        or (("__dict__" in text or "vars(" in text) and _mentions(text, spellings))
        or _mentions(text, owners)
        or (_admitted(text, spellings) and may_create(text))
    )


def routed_patched_names(
    texts: Iterable[tuple[str, str]],
    spellings: Mapping[str, str],
    events_of: Callable[[str, str], Iterable[PatchSite]],
) -> dict[str, set[str]]:
    """Facade -> every name *texts* patch through it: the regular expressions' reading of
    every file, and the syntax tree's (*events_of*) of each file routed to it."""
    found: dict[str, set[str]] = {}
    targets = set(spellings.values())
    for path, text in texts:
        for facade, names in text_patched_names(text, spellings).items():
            found.setdefault(facade, set()).update(names)
        if needs_syntax_tree(text, spellings):
            for facade, names in names_by_target(events_of(path, text), targets).items():
                found.setdefault(facade, set()).update(names)
    return found


@functools.lru_cache(maxsize=1)
def _corpus_events() -> tuple[tuple[str, PatchSite], ...]:
    """``(path, event)`` for every patch a routed test-tree file makes on a family member."""
    return tuple(
        (path, event)
        for path, text in _family_test_texts()
        if parsed_for_the_corpus(text, _SPELLINGS_FOUND, _OWNERS_FOUND)
        for event in _events_of(path, _SCAN_SPELLINGS)
    )


def _corpus_patched_names() -> dict[str, set[str]]:
    """Facade -> every name the test trees patch through it (:func:`routed_patched_names`)."""
    return routed_patched_names(
        _family_test_texts(),
        _SPELLINGS_FOUND,
        lambda path, _text: _events_of(path, _SCAN_SPELLINGS),
    )


def one_facade_patched_names(
    text: str, facade: str, spellings: Mapping[str, str], table: Mapping[str, str]
) -> set[str]:
    """The names *text* patches through the one facade *facade*, read by both readers.

    *spellings* holds *facade*'s own spellings and *table* the whole spelling table.
    :func:`text_patched_names` reads each spelling on its own, so the regular expressions
    run with *spellings* alone. A file holding none of their leaves spells nothing either
    reader resolves to *facade*; every other file is also parsed with *table*, the
    spellings the corpus scan parses with, so no routing decides what the tree sees.
    """
    if not any(spelling.rpartition(".")[2] in text for spelling in spellings):
        return set()
    names = text_patched_names(text, spellings).get(facade, set())
    try:
        events = patch_events(text, table)
    except SyntaxError:
        return names
    return names | names_by_target(events, [facade]).get(facade, set())


def facade_patched_names(facade: str) -> set[str]:
    """Every name the test trees patch through *facade* (:func:`one_facade_patched_names`
    of each test-tree file): one facade's share of :func:`_corpus_patched_names`, without
    reading every file for every facade's spellings."""
    spellings = {
        spelling: target for spelling, target in _SPELLINGS_FOUND.items() if target == facade
    }
    table = dict(_SCAN_SPELLINGS)
    found: set[str] = set()
    for path in _test_tree_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        found |= one_facade_patched_names(text, facade, spellings, table)
    return found


def test_the_one_facade_read_answers_both_ways() -> None:
    """Both readers run on a file holding the facade's leaf: the regular expressions read
    the alias patch, the tree reads the attribute chain they cannot see, and another
    module's patch adds nothing."""
    table = {**_SPELLINGS, "pkg.other": "pkg.other"}
    source = (
        "import pkg.handler as h\nimport pkg.other as o\nfrom pkg import facade as fa\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(fa, 'a', 1)\n"
        "    h.facade.b = 2\n"
        "    o.c = 3\n"
    )
    assert "b" not in text_patched_names(source, _SPELLINGS).get(_FACADE_NAME, set())
    assert one_facade_patched_names(source, _FACADE_NAME, _SPELLINGS, table) == {"a", "b"}
    other_only = "import pkg.other as o\no.c = 3\n"
    assert one_facade_patched_names(other_only, _FACADE_NAME, _SPELLINGS, table) == set()


def unreached_readers(index: _facade_mirror.MirrorIndex, sources: Mapping[str, str]) -> list[str]:
    """Owners whose code reads an indexed name as a global without binding it.

    The structural half of patch reach: a write through the facade reaches exactly the
    holders, so an owner that reads a name it does not hold -- falling through to a
    builtin of the same name -- is the one place such a patch would miss. Read from the
    owners' bytecode, so it covers every name at once, patched by a test or not.
    """
    problems = []
    for owner in index.owners:
        for name in sorted(global_reads(sources[owner], owner)):
            holders = index.holders.get(name)
            if holders is not None and owner not in holders:
                problems.append(f"{owner} reads {name} without binding it")
    return problems


def shared_names(index: _facade_mirror.MirrorIndex) -> frozenset[str]:
    """Names more than one member of the family binds: patched through the facade only."""
    return frozenset(
        name
        for name, holders in index.holders.items()
        if len(holders) > 1 or name not in index.exports
    )


def direct_owner_patches(
    source: str | ast.Module, owners: Mapping[str, str], shared: Mapping[str, frozenset[str]]
) -> list[str]:
    """``owner.name`` for each shared name *source* patches on an owner directly.

    *owners* maps each owner to its facade, and *shared* each facade to its shared names.
    """
    events = patch_events(source, {owner: owner for owner in owners})
    found = []
    for owner, names in names_by_target(events, owners).items():
        found += [f"{owner}.{name}" for name in sorted(names & shared[owners[owner]])]
    return found


def test_the_direct_owner_patch_scan_answers_both_ways(family: Family) -> None:
    low, high = family.low.__name__, family.high.__name__
    owners = {low: family.facade.__name__, high: family.facade.__name__}
    shared = {family.facade.__name__: shared_names(family.index)}
    source = (
        f"import {low} as lo\nfrom {high.rpartition('.')[0]} import high\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(lo, 'LIMIT', 1)\n"
        "    monkeypatch.setattr(lo, 'low_dumps', 2)\n"
        f"    monkeypatch.setattr('{high}.helper', 3)\n"
        "    monkeypatch.setattr(high, 'high_dumps', 4)\n"
    )
    assert direct_owner_patches(source, owners, shared) == [f"{low}.LIMIT", f"{high}.helper"]


def test_no_test_patches_a_shared_name_on_an_owner_directly() -> None:
    shared = {
        name: shared_names(vars(facade)[_facade_mirror.INDEX_ATTRIBUTE])
        for name, facade in _FACADES.items()
    }
    offenders = [
        f"{path}:{event.line} {event.module}.{event.name}"
        for path, event in _corpus_events()
        if event.module in _OWNERS_FOUND and event.name in shared[_OWNERS_FOUND[event.module]]
    ]
    assert offenders == [], (
        "a name more than one member of a mirrored family binds is patched through the "
        "facade, never on one owner: the facade's undo would restore every holder to that "
        "owner's patched value:\n" + "\n".join(offenders)
    )


#: How many patched names per facade are proven by executing the patch and its undo;
#: the rest are proven structurally, by :func:`unreached_readers`.
_SAMPLE = 3


def reach_sample(names: Iterable[str], index: _facade_mirror.MirrorIndex) -> list[str]:
    """The first :data:`_SAMPLE` patched names the mirror routes, sorted."""
    routed = sorted(set(names) & (set(index.holders) | set(index.forwards)))
    return routed[:_SAMPLE]


def test_every_name_the_test_trees_patch_through_a_facade_reaches_its_readers() -> None:
    corpus = _corpus_patched_names()
    for name, facade in _FACADES.items():
        index = vars(facade)[_facade_mirror.INDEX_ATTRIBUTE]
        names = corpus.get(name, set())
        assert names, f"no test patches {name}, so this case measured nothing"
        assert unrouted_patches(index, names) == [], name
        assert unreached_readers(index, _family_sources(index)) == [], name
        assert patch_reach_failures(facade, reach_sample(names, index)) == [], name


#: Patches only the syntax tree reads, one per blind spot of the regular expressions:
#: (source, the name it patches through ``pkg.facade``).
_SYNTAX_ONLY_CASES = (
    ("import pkg.facade\npkg.facade.a = 1\n", "a"),
    ("from pkg import facade as fa\nalias = fa\nalias.b = 2\n", "b"),
    ("from unittest import mock\nM = 'pkg.facade'\nmock.patch(f'{M}.c')\n", "c"),
    ("from unittest import mock\nmock.patch('pkg.facade.' + 'd')\n", "d"),
    ("from unittest import mock\nimport pkg.facade as fa\nmock.patch.multiple(fa, e=1)\n", "e"),
    (
        "import functools\nfrom unittest import mock\nimport pkg.facade as fa\n"
        "functools.partial(mock.patch.object, fa)('f')\n",
        "f",
    ),
    ("import importlib\nh = importlib.import_module('.facade', 'pkg')\nh.g = 1\n", "g"),
    ("import sys\nm = sys.modules.get('pkg.facade')\nm.h = 1\n", "h"),
    ("import pkg.facade as fa\nvars(fa).update({'i': 1})\n", "i"),
    ("from unittest import mock\nmock.patch.dict('pkg.facade.__dict__', {'j': 1})\n", "j"),
    ("import pytest\nfa = pytest.importorskip('pkg.facade')\nfa.k = 1\n", "k"),
)


@pytest.mark.parametrize(("source", "name"), _SYNTAX_ONLY_CASES)
def test_a_patch_only_the_syntax_tree_reads_is_routed_to_it_and_caught(
    source: str, name: str
) -> None:
    """The regular expressions miss it, so the router must send the file to the tree."""
    assert name not in text_patched_names(source, _SPELLINGS).get(_FACADE_NAME, set())
    assert name in patched_names(source, _SPELLINGS)[_FACADE_NAME]
    assert needs_syntax_tree(source, _SPELLINGS)
    parsed: list[str] = []

    def events_of(path: str, text: str) -> list[PatchSite]:
        parsed.append(path)
        return patch_events(text, _SPELLINGS)

    found = routed_patched_names([("case.py", source)], _SPELLINGS, events_of)
    assert name in found[_FACADE_NAME] and parsed == ["case.py"]


def test_a_file_the_regular_expressions_fully_read_is_never_parsed() -> None:
    readable = (
        "from unittest import mock\nimport pkg.facade as fa\nfrom pkg import facade\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(fa, 'a', 1)\n"
        "    fa.b = 2\n"
        "    mock.patch.object(facade, 'c')\n"
        "    monkeypatch.setattr('pkg.facade.d', 3)\n"
        "    vars(fa)['e'] = 5\n"
        "    assert fa.helper() == facade.LIMIT\n"
    )
    assert not needs_syntax_tree(readable, _SPELLINGS)
    assert not needs_syntax_tree("x = 1\n", _SPELLINGS), "a file naming no facade"

    def never(path: str, text: str) -> list[PatchSite]:
        raise AssertionError(f"{path} was parsed")

    found = routed_patched_names([("readable.py", readable)], _SPELLINGS, never)
    assert found == {_FACADE_NAME: set("abcde")} == patched_names(readable, _SPELLINGS)
    for source, _name in _SYNTAX_ONLY_CASES:
        assert parsed_for_the_corpus(source, _SPELLINGS, ())
    assert not parsed_for_the_corpus(readable.replace("vars(fa)['e'] = 5\n", ""), _SPELLINGS, ())
    assert parsed_for_the_corpus(readable, _SPELLINGS, ()), "a namespace write needs its line"
    assert parsed_for_the_corpus("import pkg.parts.low\n", _SPELLINGS, ["pkg.parts.low"])


def test_the_corpus_scans_parse_once_and_execute_a_bounded_sample(family: Family) -> None:
    """A work budget, pinned by count rather than by clock: a 2,000-site file is read in
    one pass, and however many names a facade's tests patch, only a fixed sample of them
    is executed; the rest are proven from the owners' bytecode."""
    built = []

    class _CountedBindings(_Bindings):
        def __init__(self, *args: object, **kwargs: object) -> None:
            built.append(1)
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    lines = ["from unittest import mock", "import pkg.facade as fa", "def test_x(monkeypatch):"]
    lines += [f"    monkeypatch.setattr(fa, 'n{i}', {i})" for i in range(1000)]
    lines += [f"    mock.patch('pkg.facade.m{i}')" for i in range(1000)]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(facade_mirror_scan, "_Bindings", _CountedBindings)
        events = patch_events("\n".join(lines) + "\n", _SPELLINGS)
    assert len(events) == 2000 and len(built) == 1
    names = {f"n{i}" for i in range(1000)} | set(family.index.holders)
    sample = reach_sample(names, family.index)
    assert len(sample) == _SAMPLE and set(sample) <= set(family.index.holders)
