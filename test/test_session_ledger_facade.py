"""``kiro_crew.session_ledger`` keeps the patch seams and the surface the one module had.

``kiro_crew.session_ledger`` is a mirror facade (:mod:`kiro_crew._facade_mirror`) over two
owners, lowest layer first: ``session_ledger_layout`` (the vocabulary, the on-disk layout
and the store lock) and ``session_ledger_control`` (the per-slot control files that govern
what a fold reads). It keeps the record API itself -- the fold and read, the write, the
upgrade carry, the legacy-store purge and the snapshot -- and every name an owner defines
still resolves as ``kiro_crew.session_ledger.<name>``.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules. These
tests pin what is particular to this one: every name the test trees patch through it --
directly or through another module's binding of it -- reaches every module holding it and
the undo restores all of them; the owner list and its layering; that the facade binds what
its own code reads and serves the rest, with every served name visible to type checkers;
that every public name the module held still type-checks through it; that the family logs
as ``kiro_crew.session_ledger`` and its three errors keep that module name; and that the
facade imports nothing its own code does not account for, the F401 check ``setup.cfg``
switches off for it.
"""

from __future__ import annotations

import ast
import dis
import functools
import logging
import sys
import traceback
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus
import test_campaign_facades_base_public_names as base_names

from kiro_crew import _facade_mirror, session_ledger

pytestmark = pytest.mark.xdist_group(name="tree_scan_session_ledger_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("known_seams", "spelling_cases", "corpus_gate", "owner_layering")

FACADE = session_ledger.__name__
OWNERS = ("kiro_crew.session_ledger_layout", "kiro_crew.session_ledger_control")
_SRC = Path(session_ledger.__file__).resolve().parent

#: Names the test trees are known to patch through the facade, one or more per kind of
#: seam: the store lock several owners take, a bound only the control owner reads, a
#: control-file helper the facade's carry reaches, a name the facade only imports and an
#: owner reads, the facade's own fold helper, and a name patched through the work
#: projection's binding of the module. The scan below must find all of them, so a scanner
#: that stops matching goes red instead of passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "_MAX_ORDERED_UNITS",
        "_control_file",
        "_locked",
        "_rewrite_lines",
        "_unit_last_seq",
        "exclude_units",
        "fsync_dir",
        "release_lock",
        "work_crew_log_units",
    }
)

#: Every name the facade's import statements bound before its owners moved out. They stay
#: bound here because callers and tests read and patch them as attributes of this module.
_BASE_IMPORTS = frozenset("""
    Any append_line atomic_write contextmanager data_home env_file_display fsync_dir
    hashlib IS_WINDOWS Iterator json logging make_owner_only_dir NamedTuple os Path re
    release_lock shutil strip_extended_length_prefix time try_acquire_lock
    """.split())

#: The names the facade's own glue imports: the owner loop, the mirror, its manifest guard.
_GLUE_IMPORTS = frozenset({"TYPE_CHECKING", "_facade_mirror", "_importlib", "_sys"})


def _index() -> _facade_mirror.MirrorIndex:
    index = vars(session_ledger)[_facade_mirror.INDEX_ATTRIBUTE]
    assert isinstance(index, _facade_mirror.MirrorIndex)
    return index


def _family() -> list[ModuleType]:
    return [session_ledger, *(sys.modules[name] for name in _index().owners)]


def _source(module: ModuleType) -> str:
    assert module.__file__ is not None
    return Path(module.__file__).read_text(encoding="utf-8")


def _tree(module: ModuleType) -> ast.Module:
    return ast.parse(_source(module))


# --------------------------------------------------------------------------- #
# The patch scan and its reach
# --------------------------------------------------------------------------- #


@functools.lru_cache(maxsize=1)
def _spellings() -> dict[str, str]:
    """The facade's dotted name, plus ``<module>.session_ledger`` for each src module that
    binds it at module level (a ``TYPE_CHECKING`` binding a facade serves included) --
    another spelling of the same object a test can patch through."""
    spellings = {FACADE: FACADE}
    for path, text in source_corpus.iter_candidate_sources(require_all=("session_ledger",)):
        if not path.is_relative_to(_SRC):
            continue
        module = ".".join(path.relative_to(_SRC.parent).with_suffix("").parts)
        if module.endswith(".__init__"):
            module = module.rpartition(".")[0]
        for node in _facade_mirror._module_level_statements(ast.parse(text)):
            if isinstance(node, ast.ImportFrom) and node.module == "kiro_crew" and not node.level:
                for alias in node.names:
                    if alias.name == "session_ledger":
                        spellings[f"{module}.{alias.asname or alias.name}"] = FACADE
    return spellings


def patched_names(source: str) -> set[str]:
    return facade_mirror_scan.patched_names(source, _spellings()).get(FACADE, set())


#: Each spelling the patch scan must read, and each it must skip, keyed by case id.
_SPELLING_CASES = {
    "monkeypatch-alias": (
        "from kiro_crew import session_ledger as sl\n"
        "def t(monkeypatch):\n    monkeypatch.setattr(sl, 'A', 1)\n",
        {"A"},
    ),
    "mock-patch-string": (
        "from unittest.mock import patch\n"
        "def t():\n    with patch('kiro_crew.session_ledger.B'):\n        pass\n",
        {"B"},
    ),
    "projection-binding": (
        "from kiro_crew.crew_log import projection\n"
        "def t(monkeypatch):\n    monkeypatch.setattr(projection.session_ledger, 'C', 1)\n",
        {"C"},
    ),
    "owner-string": (
        "from unittest.mock import patch\n"
        "def t():\n    with patch('kiro_crew.session_ledger_control.D'):\n        pass\n",
        set(),
    ),
}


def _test_tree_files() -> list[Path]:
    root = source_corpus.repo_root()
    test_root = root / "test"
    out = []
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        if rel == "conftest.py" or path.is_relative_to(test_root) or "/tests/" in rel:
            out.append(path)
    return out


@functools.lru_cache(maxsize=1)
def _corpus_patched_names() -> frozenset[str]:
    """Every name the test trees patch through the facade, scanned once per worker."""
    found: set[str] = set()
    for path in _test_tree_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if "session_ledger" not in text:
            continue
        try:
            found |= patched_names(text)
        except SyntaxError:
            continue
    return frozenset(found)


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    """The reach the unsplit module had: a write to ``session_ledger.<name>`` was what every
    reader of that name read. Each module of the family that binds a patched name must see
    the patched object, and the undo must restore all of them."""
    family = _family()
    missed: dict[str, list[str]] = {}
    for name in sorted(_corpus_patched_names()):
        if not hasattr(session_ledger, name):
            continue
        if isinstance(getattr(session_ledger, name), ModuleType):
            continue  # a shared module (os, shutil) is one object in every holder
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        before = {module.__name__: vars(module)[name] for module in holders}
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(session_ledger, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(session_ledger, name) is not sentinel:
                stale.append(FACADE)
        restored = {module.__name__: vars(module).get(name) for module in holders}
        if stale or restored != before:
            missed[name] = stale or ["undo"]
    assert missed == {}
    assert _index().divergent() == ()


def test_a_facade_patch_of_a_layout_bound_reaches_the_control_owner_reading_it() -> None:
    """``_MAX_ORDERED_UNITS`` is defined by the layout owner and read by the control
    owner's unit-order log: one write through the facade lands in both."""
    layout = sys.modules["kiro_crew.session_ledger_layout"]
    control = sys.modules["kiro_crew.session_ledger_control"]
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(session_ledger, "_MAX_ORDERED_UNITS", 3)
        assert (layout._MAX_ORDERED_UNITS, control._MAX_ORDERED_UNITS) == (3, 3)
    assert layout._MAX_ORDERED_UNITS == control._MAX_ORDERED_UNITS != 3


# --------------------------------------------------------------------------- #
# The owners, what the facade binds and serves
# --------------------------------------------------------------------------- #


def test_the_mirror_indexes_exactly_the_owners_with_no_exclusion_and_no_forward() -> None:
    index = _index()
    assert index.owners == OWNERS == session_ledger._OWNERS
    assert index.excluded == ()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports) == session_ledger._EXPORTS
    assert index.divergent() == ()


def test_the_owners_are_every_session_ledger_sibling() -> None:
    siblings = sorted(f"kiro_crew.{path.stem}" for path in _SRC.glob("session_ledger_*.py"))
    assert siblings == sorted(OWNERS)


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


def test_the_layering_check_can_fail() -> None:
    trees = {name: _tree(sys.modules[name]) for name in OWNERS}
    trees[OWNERS[0]] = ast.parse("from kiro_crew.session_ledger_control import _read_lines\n")
    trees[OWNERS[1]] = ast.parse("from kiro_crew import session_ledger\n")
    assert _layering_gaps(trees) == {OWNERS[0]: [OWNERS[1]], OWNERS[1]: [FACADE]}


def _owner_defined() -> dict[str, set[str]]:
    return {name: _facade_mirror._defined_names(_tree(sys.modules[name])) for name in OWNERS}


def test_no_two_owners_define_the_same_name_and_none_redefines_a_facade_name() -> None:
    homes: dict[str, list[str]] = {}
    for owner, names in _owner_defined().items():
        for name in names:
            homes.setdefault(name, []).append(owner)
    assert {name: owners for name, owners in homes.items() if len(owners) > 1} == {}
    facade_defined = _facade_mirror._defined_names(_tree(session_ledger))
    # The control owner names the facade's logger by its literal name: one object.
    assert sorted(set(homes) & facade_defined) == ["logger"]


def test_the_facade_serves_every_owner_name_its_own_code_does_not_bind() -> None:
    """The facade binds what its own code reads and serves every other owner-defined
    name from the owner that defines it, and every name it serves is one an owner binds."""
    defined = _owner_defined()
    served = set(_index().exports)
    assert served, "the facade serves nothing -- the purge probe would measure nothing"
    unserved = {
        name
        for names in defined.values()
        for name in names
        if name not in vars(session_ledger) and name not in served
    }
    assert sorted(unserved) == []
    owners = [sys.modules[name] for name in OWNERS]
    assert sorted(n for n in served if not any(n in vars(owner) for owner in owners)) == []


def _statically_bound(tree: ast.Module) -> set[str]:
    """Names a type checker sees on the module: every module-level binding, the
    ``TYPE_CHECKING`` imports included."""
    names = set(_facade_mirror._defined_names(tree))
    for stmt in _facade_mirror._module_level_statements(tree):
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            names |= {alias.asname or alias.name.split(".")[0] for alias in stmt.names}
    return names


def test_every_base_public_name_type_checks_through_the_facade() -> None:
    missing = base_names._BASE_PUBLIC_NAMES[FACADE] - _statically_bound(_tree(session_ledger))
    assert sorted(missing) == []


def test_every_served_name_type_checks_through_the_facade() -> None:
    """``crew_log``, the dashboard handlers and the sweep import moved names from here."""
    assert sorted(set(_index().exports) - _statically_bound(_tree(session_ledger))) == []


# --------------------------------------------------------------------------- #
# The logger, the errors and the import inventory
# --------------------------------------------------------------------------- #


def test_the_family_logs_under_the_facade_name() -> None:
    """One logger, still named for the facade module, held by every module that logs, so a
    ``caplog.at_level(logger="kiro_crew.session_ledger")`` still hears the moved code."""
    assert session_ledger.logger is logging.getLogger(FACADE)
    holders = [m.__name__ for m in _family() if "logger" in vars(m)]
    assert {FACADE, "kiro_crew.session_ledger_control"} <= set(holders)
    assert all(vars(sys.modules[h])["logger"] is session_ledger.logger for h in holders)


@pytest.mark.parametrize(
    "name", ["LedgerUnavailable", "LedgerEntryTooLarge", "LedgerExclusionError"]
)
def test_an_error_is_named_by_the_facade_in_a_traceback(name: str) -> None:
    """A traceback names an exception by its class's dotted name. All three moved to owners
    and still read as ``kiro_crew.session_ledger.<name>``."""
    error_type = getattr(session_ledger, name)
    assert error_type.__module__ == FACADE
    shown = "".join(traceback.format_exception_only(error_type("refused")))
    assert shown.startswith(f"{FACADE}.{name}: refused")


def unexpected_imports(source: str, owner_names: dict[str, set[str]], read: set[str]) -> list[str]:
    """Names *source* binds by a runtime import that nothing accounts for: the F401 check
    ``setup.cfg`` switches off for this module, kept as a test.

    A name is accounted for when the module bound it before its owners moved out (BASE),
    the mirror glue imports it, an owner module defines it (for an import from that
    owner), or the facade's own code reads it. Imports under ``if TYPE_CHECKING:`` bind
    nothing at run time and are the mirror guard's business, so they are skipped.
    """
    found: list[str] = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            continue
        if isinstance(node, ast.Import):
            bound = [(alias.asname or alias.name.split(".")[0], None) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
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


def _facade_reads() -> set[str]:
    """Every global the facade's module code and its functions read."""
    names: set[str] = set()
    pending = [compile(_source(session_ledger), "session_ledger", "exec")]
    while pending:
        code = pending.pop()
        names |= {
            i.argval for i in dis.get_instructions(code) if i.opname in ("LOAD_GLOBAL", "LOAD_NAME")
        }
        pending += [c for c in code.co_consts if isinstance(c, type(code))]
    return names


def test_the_facade_imports_nothing_its_code_does_not_account_for() -> None:
    assert unexpected_imports(_source(session_ledger), _owner_defined(), _facade_reads()) == []


def test_the_inventory_check_reports_a_planted_import() -> None:
    """The F401 stand-in must be able to fail: an import nothing reads, and an owner import
    of a name that owner does not define, are both reported."""
    planted = (
        _source(session_ledger)
        + "\nimport pickle\nfrom kiro_crew.session_ledger_layout import _read_lines\n"
    )
    assert unexpected_imports(planted, _owner_defined(), _facade_reads()) == [
        "pickle",
        "kiro_crew.session_ledger_layout._read_lines",
    ]
