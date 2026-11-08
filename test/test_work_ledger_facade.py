"""``kiro_crew.work_ledger`` keeps the patch seams and the namespace the one module had.

``kiro_crew.work_ledger`` is a mirror facade (:mod:`kiro_crew._facade_mirror`) over the
flat ``work_ledger_*.py`` owners. The dashboard routes, the board, the probes, the ledger
sweep and the tests still reach every name as ``kiro_crew.work_ledger.<name>``, and the
moved code reads its names as plain module globals, so a patch through the facade reaches
that code only because the mirror writes it into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirror facade to the shared contract.
These tests pin what is particular to this one: every name the test trees patch through
it reaches every module holding it; the owners are exactly the ``work_ledger_*.py``
siblings, each importing only the owners before it; every name an owner defines is bound
on the facade itself, as that owner's object; the family logs through one logger named for
the facade; the store's exception reports the facade's module; and every project name the
facade imported whose readers are all owners is served from an owner and imported in the
facade's ``TYPE_CHECKING`` group, so ``work_ledger.<name>`` still type-checks.
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus

from kiro_crew import _facade_mirror, work_ledger

pytestmark = pytest.mark.xdist_group(name="tree_scan_work_ledger_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("known_seams",)

FACADE = "kiro_crew.work_ledger"
SRC_DIR = Path(work_ledger.__file__).resolve().parent

#: Names the test trees are known to patch through the facade, one per kind of seam: the
#: store's clock, a lock and the record write several owners call, a size cap read as a
#: global, a stdlib class, a project import, a predicate the facade's own rebuild reads,
#: and a helper the facade keeps. The scan must find all of them, so a scanner that stops
#: matching goes red instead of passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "_now_iso",
        "conductor_lock",
        "_write_record",
        "MAX_RECORD_BYTES",
        "datetime",
        "atomic_write",
        "read_work_item",
        "_restore_files",
    }
)


def _index() -> _facade_mirror.MirrorIndex:
    return work_ledger.__dict__[_facade_mirror.INDEX_ATTRIBUTE]


def _family() -> list[ModuleType]:
    return [work_ledger, *(sys.modules[name] for name in _index().owners)]


def _owner_source(owner: str) -> str:
    return Path(sys.modules[owner].__file__ or "").read_text(encoding="utf-8")


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


def _spellings() -> dict[str, str]:
    """The facade's own name and every src module's module-level binding of it."""
    spellings = {FACADE: FACADE}
    for path, _text, tree in source_corpus.parsed_candidates(require_any=("work_ledger",)):
        rel = path.relative_to(source_corpus.src_root()).with_suffix("")
        module = ".".join(("kiro_crew", *rel.parts)).removesuffix(".__init__")
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == "kiro_crew":
                for alias in node.names:
                    if alias.name == "work_ledger" and module != FACADE:
                        spellings[f"{module}.{alias.asname or alias.name}"] = FACADE
    return spellings


def _corpus_patched_names() -> set[str]:
    spellings = _spellings()
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
    """The reach the one module had: a write to ``work_ledger.<name>`` was what every
    reader of the name read. Every member of the family that binds a patched name sees
    the patched object, and the undo leaves the family one object per name again."""
    family = _family()
    missed: dict[str, list[str]] = {}
    checked = 0
    for name in sorted(_corpus_patched_names()):
        if not hasattr(work_ledger, name):
            continue  # a test that creates the name with raising=False
        holders = [module for module in family if name in vars(module)]
        sentinel = object()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(work_ledger, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(work_ledger, name) is not sentinel:
                stale.append(FACADE)
        checked += 1
        if stale:
            missed[name] = stale
    assert missed == {}
    assert checked >= len(_KNOWN_SEAMS)
    assert _index().divergent() == ()


def test_the_owners_are_the_work_ledger_siblings_in_layer_order() -> None:
    """``_OWNERS`` names every ``work_ledger_*.py`` sibling, and each owner imports only
    owners listed before it: the order the facade loads them and a read resolves in."""
    owners = list(_index().owners)
    on_disk = {f"kiro_crew.{p.stem}": p for p in SRC_DIR.glob("work_ledger_*.py")}
    assert len(on_disk) == 7
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
    """``work_ledger.py`` was a plain module: the facade forwards no legacy table and
    keeps the default ``forward_reads``, and every served name comes from the index."""
    index = _index()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports)


def test_every_name_an_owner_defines_is_bound_on_the_facade() -> None:
    """``vars(work_ledger)`` lists the store's own names as it did before the split (the
    ``CODE_*`` status map is read off it): each name an owner defines is bound on the facade
    as that owner's object, so the mirror serves only imports, never a definition."""
    defined = {
        name: owner
        for owner in _index().owners
        for name in _facade_mirror._defined_names(ast.parse(_owner_source(owner)))
    }
    assert {"CODE_NO_LEDGER", "conductor_lock", "apply_conductor_action"} <= set(defined)
    unbound = sorted(
        name
        for name, owner in defined.items()
        if vars(work_ledger).get(name, _facade_mirror._ABSENT) is not vars(sys.modules[owner])[name]
    )
    assert unbound == []
    assert sorted(set(_index().exports) & set(defined)) == []


def _from_imports(source: str, *, type_checking: bool) -> dict[str, tuple[str, str]]:
    """``bound name -> (module, imported name)`` for the module's top-level
    ``from ... import`` statements, or for those in its ``if TYPE_CHECKING:`` block."""
    found: dict[str, tuple[str, str]] = {}
    for node in ast.parse(source).body:
        if type_checking and isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            stmts: list[ast.stmt] = node.body
        elif not type_checking:
            stmts = [node]
        else:
            continue
        for stmt in stmts:
            if isinstance(stmt, ast.ImportFrom) and stmt.level == 0:
                for alias in stmt.names:
                    found[alias.asname or alias.name] = (stmt.module or "", alias.name)
    return found


def test_every_project_name_the_facade_serves_is_imported_for_type_checkers() -> None:
    """A project name the facade imported whose readers all moved to owners is served
    from an owner at run time, and named in the facade's ``TYPE_CHECKING`` group from its
    home module, so ``work_ledger.<name>`` type-checks; the group names nothing else."""
    checking = _from_imports(
        Path(work_ledger.__file__).read_text(encoding="utf-8"), type_checking=True
    )
    assert {"file_lock", "data_home", "work_event_id"} <= set(
        checking
    ), "the static reader went blind"
    family = {FACADE, *_index().owners}
    served_project: dict[str, tuple[str, str]] = {}
    for name, owner in _index().exports.items():
        home = _from_imports(_owner_source(owner), type_checking=False).get(name)
        if home and home[0].split(".")[0] == "kiro_crew" and home[0] not in family:
            served_project[name] = home
    assert served_project, "no served name is a project import, so this case measured nothing"
    assert checking == served_project
    for name, (home, imported) in checking.items():
        assert getattr(importlib.import_module(home), imported) is getattr(work_ledger, name)


def test_the_family_logs_through_one_logger_named_for_the_facade() -> None:
    """``caplog`` filters and log records keyed on ``kiro_crew.work_ledger`` keep seeing
    the moved code: every owner that logs holds the facade's one logger."""
    assert work_ledger.logger.name == FACADE
    loggers = [m for m in _family() if "logger" in vars(m)]
    assert len(loggers) >= 4
    assert [m.__name__ for m in loggers if vars(m)["logger"] is not work_ledger.logger] == []


def test_the_store_error_keeps_the_facade_module() -> None:
    """A traceback or a log line renders a raised class as ``module.qualname``, so the
    store's exception still reads ``kiro_crew.work_ledger.WorkLedgerError``, and it is the
    only exception class the owners define."""
    found = {
        name: value
        for owner in _index().owners
        for name, value in vars(sys.modules[owner]).items()
        if isinstance(value, type)
        and issubclass(value, BaseException)
        and name in _facade_mirror._defined_names(ast.parse(_owner_source(owner)))
    }
    assert sorted(found) == ["WorkLedgerError"]
    assert work_ledger.WorkLedgerError.__module__ == FACADE
    assert f"{found['WorkLedgerError'].__module__}.WorkLedgerError" == f"{FACADE}.WorkLedgerError"


def _owner_imports(tree: ast.Module, owners: set[str], package: str = "kiro_crew") -> set[str]:
    """The owners a module's syntax tree imports, at any depth, in every spelling: an
    absolute or a relative ``import``/``from ... import`` (resolved against *package*, the
    importing module's package), and a constant ``importlib.import_module(...)`` or
    ``__import__(...)`` call."""
    found: set[str] = set()

    def note(dotted: str) -> None:
        if dotted in owners:
            found.add(dotted)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                note(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")
                anchor = ".".join(parts[: len(parts) - node.level + 1])
                base = f"{anchor}.{base}" if base else anchor
            note(base)
            for alias in node.names:
                note(f"{base}.{alias.name}")
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if called in ("import_module", "__import__"):
                note(str(node.args[0].value))
    return found


def _package_of(path: Path) -> str:
    """The package a source file under ``src/`` belongs to, for resolving its relative imports."""
    rel = path.resolve().relative_to(SRC_DIR.parent).with_suffix("")
    parts = rel.parts if rel.name == "__init__" else rel.parts[:-1]
    return ".".join(p for p in parts if p != "__init__")


@pytest.mark.parametrize(
    ("source", "package"),
    [
        ("from kiro_crew.work_ledger_writes import apply_conductor_action", "kiro_crew"),
        ("from kiro_crew import work_ledger_writes", "kiro_crew"),
        ("import kiro_crew.work_ledger_writes", "kiro_crew"),
        ("from .work_ledger_writes import apply_conductor_action", "kiro_crew"),
        ("from . import work_ledger_writes", "kiro_crew"),
        ("from ..work_ledger_writes import apply_conductor_action", "kiro_crew.dashboard"),
        ("def f():\n    from .work_ledger_writes import apply_conductor_action", "kiro_crew"),
        ("import importlib\nimportlib.import_module('kiro_crew.work_ledger_writes')", "kiro_crew"),
        (
            "from importlib import import_module\nimport_module('kiro_crew.work_ledger_writes')",
            "kiro_crew",
        ),
        ("__import__('kiro_crew.work_ledger_writes')", "kiro_crew"),
    ],
)
def test_the_owner_import_reader_sees_every_spelling(source: str, package: str) -> None:
    """Non-vacuity for the importer guard below: each spelling of an import of an owner is
    caught, and the facade's own import path is not."""
    owners = set(_index().owners)
    assert _owner_imports(ast.parse(source), owners, package) == {"kiro_crew.work_ledger_writes"}
    assert _owner_imports(ast.parse("from kiro_crew import work_ledger"), owners) == set()


def test_no_module_outside_the_family_imports_an_owner() -> None:
    """The product reaches the owners only through ``kiro_crew.work_ledger``, so the checks
    and allowlists keyed on that import path still see every caller, and moving a name
    between owners stays invisible to the rest of the package. Any spelling counts:
    absolute, relative, or a constant ``importlib.import_module``/``__import__`` call."""
    owners = set(_index().owners)
    facade_tree = ast.parse(Path(work_ledger.__file__).read_text(encoding="utf-8"))
    assert _owner_imports(facade_tree, owners) == owners, "the import reader went blind"
    family = {Path(work_ledger.__file__).resolve()} | {
        Path(sys.modules[owner].__file__ or "").resolve() for owner in owners
    }
    leaves = tuple(owner.rpartition(".")[2] for owner in owners)
    offenders = sorted(
        path.relative_to(SRC_DIR).as_posix()
        for path, _text, tree in source_corpus.parsed_candidates(require_any=leaves)
        if path.resolve() not in family and _owner_imports(tree, owners, _package_of(path))
    )
    assert any(
        path.resolve() in family
        for path, _t, _tree in source_corpus.parsed_candidates(require_any=leaves)
    ), "the corpus reader went blind"
    assert offenders == []
