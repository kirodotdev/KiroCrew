"""``kiro_crew.members`` keeps the patch seams and the surface the one module had.

``kiro_crew.members`` is a mirror facade (:mod:`kiro_crew._facade_mirror`) over the
owners in ``kiro_crew.members_parts``. Callers, the tests and the documents still reach
every name as ``kiro_crew.members.<name>``, and the moved code reads its names as plain
module globals, so a patch through the facade reaches that code only because the mirror
writes it into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirror facade to the shared contract.
These tests pin what is particular to this one: every name the test trees patch through
it reaches every module holding it; the owners are exactly the ``members_parts`` modules,
each importing only the owners before it, and none rebinds a module global; the family
logs through one logger named for the facade; an exception or enum class an owner
defines, and a function an import-origin table pins, still report the facade's module;
every project name the facade imported before its owners moved out stays bound on it;
and every name it serves from outside the family is visible to a type checker through
its ``TYPE_CHECKING`` imports.
"""

from __future__ import annotations

import ast
import importlib
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus

import kiro_crew
from kiro_crew import _facade_mirror, members

pytestmark = pytest.mark.xdist_group(name="tree_scan_members_parts_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("known_seams", "corpus_gate")

FACADE = "kiro_crew.members"
_SRC = Path(kiro_crew.__file__).resolve().parent
_OWNER_PREFIX = "kiro_crew.members_parts"
PARTS_DIR = Path(members.__file__).resolve().parent / "members_parts"
INVENTORY = source_corpus.repo_root() / "test" / "campaign_facade_inventories" / f"{FACADE}.json"

#: Names the test trees are known to patch through the facade, one per kind of seam: the
#: data-home resolver every path reads, a slug resolver one owner defines and others
#: call, a config gate, a binding reader and its path, a project function an owner
#: imports, a member-creation step and the redaction predicate. The scan must find all
#: of them, so a scanner that stops matching goes red instead of passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "data_home",
        "member_slug",
        "crew_panel_enabled",
        "read_dm_binding",
        "dm_binding_path",
        "fsync_dir",
        "mark_welcome_owed",
        "external_text_requires_redaction",
    }
)


def _index() -> _facade_mirror.MirrorIndex:
    return members.__dict__[_facade_mirror.INDEX_ATTRIBUTE]


def _family() -> list[ModuleType]:
    return [members, *(sys.modules[name] for name in _index().owners)]


def _owner_sources() -> dict[str, str]:
    return {
        owner: Path(sys.modules[owner].__file__ or "").read_text(encoding="utf-8")
        for owner in _index().owners
    }


def _test_tree_files() -> list[Path]:
    root = source_corpus.repo_root()
    found = []
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        if rel == "conftest.py" or rel.startswith("test/"):
            found.append(path)
        elif rel.startswith("src/kiro_crew/apps/builtins/") and (
            "/tests/" in rel or path.name.startswith("test_") or path.name == "conftest.py"
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
    """The reach the one module had: a write to ``members.<name>`` was what every
    reader of the name read. Every member of the family that binds a patched name sees
    the patched object, and the undo leaves the family one object per name again."""
    family = _family()
    missed: dict[str, list[str]] = {}
    checked = 0
    for name in sorted(_corpus_patched_names()):
        if not hasattr(members, name):
            continue  # a test that creates the name with raising=False
        holders = [module for module in family if name in vars(module)]
        sentinel = object()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(members, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(members, name) is not sentinel:
                stale.append(FACADE)
        checked += 1
        if stale:
            missed[name] = stale
    assert missed == {}
    assert checked >= len(_KNOWN_SEAMS)
    assert _index().divergent() == ()


def test_no_owner_rebinds_a_module_global() -> None:
    """``members.py`` rebound no global at run time, so no owner may: a ``global`` write
    lands in one namespace alone, which the mirror cannot follow."""
    rebinds = {
        owner: sorted(
            {
                n
                for node in ast.walk(ast.parse(text))
                if isinstance(node, ast.Global)
                for n in node.names
            }
        )
        for owner, text in _owner_sources().items()
    }
    assert {owner: names for owner, names in rebinds.items() if names} == {}


def test_the_owners_are_the_parts_package_in_layer_order() -> None:
    """``_OWNERS`` names every module of ``members_parts``, and each owner imports only
    owners listed before it: the order the facade loads them and a read resolves in."""
    owners = list(_index().owners)
    on_disk = {
        f"kiro_crew.members_parts.{p.stem}": p
        for p in PARTS_DIR.glob("*.py")
        if p.name != "__init__.py"
    }
    assert len(on_disk) == 6
    assert sorted(owners) == sorted(on_disk)
    late: dict[str, list[str]] = {}
    for position, owner in enumerate(owners):
        tree = ast.parse(on_disk[owner].read_text(encoding="utf-8"))
        imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        later = sorted(m for m in imported if m in owners and owners.index(m) >= position)
        if later:
            late[owner] = later
    assert late == {}


def test_the_facade_keeps_the_default_read_path_and_forwards_no_table() -> None:
    """``members.py`` was a plain module: the facade forwards no legacy table and keeps
    the default ``forward_reads``, and every served name comes from the index."""
    index = _index()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports)


def test_the_family_logs_through_one_logger_named_for_the_facade() -> None:
    """``caplog`` filters keyed on ``kiro_crew.members`` keep seeing the moved code."""
    assert members.logger.name == FACADE
    loggers = [m for m in _family() if "logger" in vars(m)]
    assert len(loggers) >= 5
    assert [m.__name__ for m in loggers if vars(m)["logger"] is not members.logger] == []


def test_an_exception_an_owner_defines_keeps_the_facade_module() -> None:
    """A traceback or a log line renders a raised class as ``module.qualname``, so an
    exception class an owner defines reports ``kiro_crew.members``, as it did before
    it moved, and so does the lifecycle enum."""
    found = {
        name: value
        for owner in _index().owners
        for name, value in vars(sys.modules[owner]).items()
        if isinstance(value, type)
        and issubclass(value, BaseException)
        and name in _facade_mirror._defined_names(ast.parse(_owner_sources()[owner]))
    }
    assert {
        "MemberNameError",
        "MemberNameTaken",
        "MemberRulesUnreadable",
        "MemberSlugError",
        "SelfUpdateError",
    } <= set(found), found
    assert {name: cls.__module__ for name, cls in found.items()} == dict.fromkeys(found, FACADE)
    assert members.MemberLifecycle.__module__ == FACADE


def test_a_function_an_import_origin_table_pins_keeps_the_facade_module() -> None:
    """``test_cli_doctor_refactor_facade.py`` pins these functions' ``__module__`` as the path
    ``cli_doctor`` imports them from, so each keeps reporting the facade while its object
    stays the one its owner defines and every holder binds."""
    for name in ("is_dispatchable_member_name",):
        value = getattr(members, name)
        assert value.__module__ == FACADE, name
        holders = _index().holders[name]
        assert holders and all(vars(sys.modules[h])[name] is value for h in holders), name


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
                name.startswith(_OWNER_PREFIX) for name in _imported_modules(node, module)
            ):
                found.add(rel.as_posix())
    return found


def test_only_the_family_imports_an_owner() -> None:
    """Callers reach the moved code through ``kiro_crew.members``: an import of an owner from
    outside the family would hold bindings no facade patch reaches."""
    sources = source_corpus.iter_candidate_sources(require_any=("members_parts",))
    importers = _owner_importers(
        ((path, text) for path, text in sources if path.is_relative_to(_SRC)), _SRC
    )
    assert "members_parts/dm_bindings.py" in importers  # owners import the owners below them
    family = {"members.py", *(f"members_parts/{p.name}" for p in PARTS_DIR.glob("*.py"))}
    assert importers - family == set()


def test_the_owner_import_scan_finds_an_outside_importer(tmp_path: Path) -> None:
    root = tmp_path / "kiro_crew"
    sources = {
        root / "pkg" / "caller.py": "from kiro_crew.members_parts.member_space import x\n",
        root / "other.py": "from kiro_crew.members_parts import member_space as low\n",
        root / "relative.py": "from .members_parts.member_space import x\n",
        root / "deep" / "up.py": "from .. import members_parts\n",
        root / "plain.py": "import kiro_crew.members_parts.member_space\n",
        root / "facade_user.py": "from kiro_crew.members import x\n",
    }
    assert _owner_importers(sources.items(), root) == {
        "pkg/caller.py",
        "other.py",
        "relative.py",
        "deep/up.py",
        "plain.py",
    }


def _static_bindings(path: Path) -> set[str]:
    """Names a module binds at import time with a module-level ``import``, outside
    ``if TYPE_CHECKING:`` -- the bindings a type checker reads off the module."""
    found: set[str] = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            found |= {(a.asname or a.name).split(".")[0] for a in node.names}
    return found


def _type_checking_bindings(path: Path) -> dict[str, tuple[str, str | None]]:
    """Name -> (module, attribute) a top-level ``if TYPE_CHECKING:`` block binds it from:
    the attribute is ``None`` for a plain ``import <module>``."""
    found: dict[str, tuple[str, str | None]] = {}
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for sub in node.body:
                if isinstance(sub, ast.ImportFrom):
                    found.update(
                        {a.asname or a.name: (sub.module or "", a.name) for a in sub.names}
                    )
                elif isinstance(sub, ast.Import):
                    found.update({a.asname or a.name: (a.name, None) for a in sub.names})
    return found


def _home(value: object) -> str:
    if isinstance(value, ModuleType):
        return value.__name__
    return getattr(value, "__module__", "") or ""


def test_every_project_name_the_facade_imported_stays_bound_on_it() -> None:
    """A name the facade imported from the rest of ``kiro_crew`` before its owners moved
    out is bound on it still, statically and as its home module's object, so a reader that
    spells it through ``kiro_crew.members`` keeps type-checking."""
    rebound = json.loads(INVENTORY.read_text(encoding="utf-8"))["rebound"]
    assert "data_home" in rebound, "the inventory went blind"
    bound = _static_bindings(Path(members.__file__))
    assert sorted(name for name in rebound if name not in bound) == []
    for name, home in rebound.items():
        assert vars(members)[name] is getattr(sys.modules[home], name), name


def test_every_name_served_from_outside_the_family_is_visible_to_a_type_checker() -> None:
    """A name the mirror serves that no owner defines is an owner's import: it comes from
    outside the family, so the facade's ``TYPE_CHECKING`` imports name it, from the
    module that holds it, and none of them is a project module the facade stopped
    binding."""
    defined = {
        name
        for text in _owner_sources().values()
        for name in _facade_mirror._defined_names(ast.parse(text))
    }
    imported_by_owners = sorted(set(_index().exports) - defined)
    assert "json" in imported_by_owners, "no served name is an owner's import"
    manifest = _type_checking_bindings(Path(members.__file__))
    assert sorted(name for name in imported_by_owners if name not in manifest) == []
    wrong_home = []
    for name in imported_by_owners:
        module, attribute = manifest[name]
        home = importlib.import_module(module)
        if (home if attribute is None else getattr(home, attribute)) is not getattr(members, name):
            wrong_home.append(name)
    assert wrong_home == []
    family = {FACADE, *_index().owners}
    served_from_outside = {
        name: _home(getattr(members, name))
        for name in imported_by_owners
        if _home(getattr(members, name)).startswith("kiro_crew")
        and _home(getattr(members, name)) not in family
    }
    assert served_from_outside == {}
