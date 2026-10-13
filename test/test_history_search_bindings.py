"""``kiro_crew.history_search`` stays the import path and the patch surface of its family.

The module keeps ``SessionCatalogProjection`` and is a ``kiro_crew._facade_mirror`` facade
over two owners, lowest layer first: ``history_search_query`` (what a query means: needles,
forge references, the provider spelling slot, the one-string match rule) and
``history_search_memo`` (what every search over one directory shares: the memos, their
byte ceilings, the single-flight table, the memo-gated counter). ``test_facade_mirror_guards``
holds every mirrored facade to the shared contract; this file pins what is particular to
this one: its owners and their layering, the one global it serves, the logger name, every
BASE name bound on the facade, a patch through the facade reaching each reader of the names
the test trees patch, and that nothing outside the family imports an owner.
"""

from __future__ import annotations

import ast
import dis
import importlib
import json
import logging
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
import source_corpus

from kiro_crew import _facade_mirror
from kiro_crew import history_search as facade
from kiro_crew.subprocess_utf8 import UTF8_TEXT

pytestmark = pytest.mark.xdist_group(name="tree_scan_history_search_bindings")

_PACKAGE = "kiro_crew"
_FACADE = "kiro_crew.history_search"
_QUERY = "kiro_crew.history_search_query"
_MEMO = "kiro_crew.history_search_memo"
_OWNERS = (_QUERY, _MEMO)
_INVENTORY = Path(__file__).resolve().parent / "campaign_facade_inventories" / f"{_FACADE}.json"


def _index() -> _facade_mirror.MirrorIndex:
    return vars(facade)[_facade_mirror.INDEX_ATTRIBUTE]


def _source(dotted: str) -> str:
    path = importlib.import_module(dotted).__file__
    assert path is not None
    return Path(path).read_text(encoding="utf-8")


def _tree(dotted: str) -> ast.Module:
    return ast.parse(_source(dotted))


def test_the_facade_mirrors_its_two_owners_lowest_first() -> None:
    assert type(facade) is _facade_mirror.MirroredModule
    assert facade._OWNERS == _OWNERS
    assert _index().owners == _OWNERS
    assert _index().excluded == ()
    assert dict(_index().forwards) == {}
    assert _index().divergent() == ()


def test_the_owners_are_exactly_the_history_search_siblings() -> None:
    here = Path(facade.__file__ or "").parent
    siblings = sorted(f"{_PACKAGE}.{path.stem}" for path in here.glob("history_search_*.py"))
    assert siblings == sorted(_OWNERS)


def test_the_facade_serves_only_the_resolver_slot() -> None:
    """The slot is the one global an owner rebinds at run time; every other name an owner
    defines is bound on the facade itself, the owner's own object."""
    assert dict(_index().exports) == {"_search_ref_resolver": _QUERY}
    assert set(facade._EXPORTS) == set(facade._SERVED_ONLY) == {"_search_ref_resolver"}
    assert "_search_ref_resolver" not in vars(facade)
    for owner in _OWNERS:
        module = importlib.import_module(owner)
        defined = _facade_mirror._defined_names(_tree(owner)) - {"_search_ref_resolver"}
        unbound = sorted(n for n in defined if vars(facade).get(n) is not vars(module)[n])
        assert unbound == [], owner


def test_every_base_name_is_bound_on_the_facade() -> None:
    """Bound, not served, so a type checker reads each one through the facade path."""
    inventory = json.loads(_INVENTORY.read_text(encoding="utf-8"))
    names = inventory["base_public_names"]
    assert names
    assert sorted(n for n in names if n not in vars(facade)) == []


def test_a_read_through_the_facade_follows_the_resolver_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``register_search_ref_resolver`` rebinds the owner's global; a read through the
    facade sees each value it takes. The patch restores whatever the slot held before."""
    query = importlib.import_module(_QUERY)
    monkeypatch.setattr(query, "_search_ref_resolver", None)

    def resolve(token: str) -> None:
        return None

    facade.register_search_ref_resolver(resolve)
    assert query._search_ref_resolver is resolve
    assert facade._search_ref_resolver is resolve
    facade.reset_search_ref_resolver_for_tests()
    assert query._search_ref_resolver is None
    assert facade._search_ref_resolver is None


def test_a_patch_of_the_slot_on_its_owner_reads_through_the_facade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = object()
    monkeypatch.setattr(importlib.import_module(_QUERY), "_search_ref_resolver", sentinel)
    assert facade._search_ref_resolver is sentinel
    assert "_search_ref_resolver" not in vars(facade)


def _logs_under_own_name(source: str) -> bool:
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "getLogger"
            and any(isinstance(arg, ast.Name) and arg.id == "__name__" for arg in node.args)
        ):
            return True
    return False


def test_the_own_name_logger_detector_answers_both_ways() -> None:
    assert _logs_under_own_name("import logging\nlog = logging.getLogger(__name__)\n")
    assert not _logs_under_own_name('import logging\nlog = logging.getLogger("kiro_crew.x")\n')


def test_the_family_logs_under_the_facade_name() -> None:
    """A log filter or ``caplog`` keyed on the module callers import from still applies."""
    logger = logging.getLogger(_FACADE)
    assert facade.logger is logger
    assert vars(importlib.import_module(_QUERY))["logger"] is logger
    for dotted in (_FACADE, *_OWNERS):
        assert not _logs_under_own_name(_source(dotted)), dotted


def _owner_fn(owner: str, name: str) -> Callable[[], object]:
    return lambda: getattr(importlib.import_module(owner), name)


def _catalog_method(name: str) -> Callable[[], object]:
    return lambda: getattr(facade.SessionCatalogProjection, name)


#: Each name the test trees patch through this facade -> the module whose namespace its
#: readers read it from, and the functions reading it.
_PATCHED: dict[str, tuple[str, tuple[Callable[[], object], ...]]] = {
    "_search_ref_resolver": (_QUERY, (_owner_fn(_QUERY, "_provider_search_ref"),)),
    "_SEARCH_COUNT_MEMO_BYTES": (_MEMO, (_owner_fn(_MEMO, "_memos_for"),)),
    "_SEARCH_SNIPPET_MEMO_BYTES": (_MEMO, (_owner_fn(_MEMO, "_memos_for"),)),
    "_Flight": (_FACADE, (_catalog_method("search_sessions"),)),
    "_INDEX_QUIET_WINDOW_SECS": (_FACADE, (_catalog_method("backfill_index"),)),
    "_time": (
        _FACADE,
        (_catalog_method("_search_parsed"), _catalog_method("backfill_index")),
    ),
    "open": (
        _FACADE,
        (
            _catalog_method("_scan_head_title"),
            _catalog_method("_list_sessions_status"),
            _catalog_method("_iter_message_texts"),
        ),
    ),
}


#: Opcodes that read or write a module global by name (``LOAD_NAME`` only at module level).
_GLOBAL_OPS = frozenset({"LOAD_GLOBAL", "LOAD_NAME", "STORE_GLOBAL", "DELETE_GLOBAL"})


def _reads_global(fn: object, name: str) -> bool:
    """Whether *fn*'s code, or a code object nested in it, loads *name* as a global.

    Read from the bytecode, not ``co_names``: that table also holds attribute names, so
    ``self._time`` would pass for a read of the module global ``_time``.
    """
    pending = [getattr(fn, "__code__", None)]
    while pending:
        code = pending.pop()
        if code is None:
            continue
        if any(
            ins.opname in _GLOBAL_OPS and ins.argval == name for ins in dis.get_instructions(code)
        ):
            return True
        pending.extend(const for const in code.co_consts if hasattr(const, "co_code"))
    return False


def test_the_global_read_detector_answers_both_ways() -> None:
    def nested() -> object:
        return lambda: _FACADE

    def attribute_only(obj: object) -> object:
        return getattr(obj, "x", None) or obj._FACADE  # type: ignore[attr-defined]

    def does_not() -> object:
        return None

    assert _reads_global(nested, "_FACADE")
    assert "_FACADE" not in nested.__code__.co_names  # the read sits in the nested lambda
    assert not _reads_global(attribute_only, "_FACADE")
    assert "_FACADE" in attribute_only.__code__.co_names
    assert not _reads_global(does_not, "_FACADE")


@pytest.mark.parametrize("name", sorted(_PATCHED))
def test_a_patch_through_the_facade_reaches_every_reader(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, readers = _PATCHED[name]
    namespace = vars(importlib.import_module(home))
    functions = [reader() for reader in readers]
    assert all(getattr(fn, "__globals__", None) is namespace for fn in functions), name
    assert [fn.__qualname__ for fn in functions if not _reads_global(fn, name)] == []
    sentinel = object()
    monkeypatch.setattr(facade, name, sentinel, raising=False)
    assert getattr(facade, name) is sentinel
    assert [fn.__qualname__ for fn in functions if fn.__globals__[name] is not sentinel] == []


def _resolved(node: ast.ImportFrom, package: str) -> str:
    """The absolute module a ``from ... import`` names, *package* anchoring a relative one."""
    if not node.level:
        return node.module or ""
    parts = package.split(".")
    anchor = ".".join(parts[: len(parts) - node.level + 1])
    return f"{anchor}.{node.module}" if node.module else anchor


def _runtime_facade_imports(tree: ast.Module, package: str = _PACKAGE) -> list[int]:
    """Lines that import the facade outside a ``TYPE_CHECKING`` block."""
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            guarded.update(id(inner) for inner in ast.walk(node))
    lines: list[int] = []
    for node in ast.walk(tree):
        if id(node) in guarded:
            continue
        if isinstance(node, ast.ImportFrom):
            base = _resolved(node, package)
            if base == _FACADE or any(f"{base}.{a.name}" == _FACADE for a in node.names):
                lines.append(node.lineno)
        elif isinstance(node, ast.Import):
            if any(a.name == _FACADE for a in node.names):
                lines.append(node.lineno)
    return lines


def test_the_facade_import_scan_answers_both_ways() -> None:
    tree = ast.parse(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from kiro_crew.history_search import SessionCatalogProjection\n"
        "def f():\n    from kiro_crew.history_search import x\n"
        "import kiro_crew.history_search\n"
        "from kiro_crew import history_search\n"
        "from . import history_search\n"
        "from .history_search import SearchNeedle\n"
    )
    assert sorted(_runtime_facade_imports(tree)) == [5, 6, 7, 8, 9]


@pytest.mark.parametrize("owner", _OWNERS)
def test_no_owner_imports_the_facade_at_run_time(owner: str) -> None:
    assert _runtime_facade_imports(_tree(owner)) == []


def _owner_imports(tree: ast.Module, package: str = _PACKAGE) -> set[str]:
    """The owner modules *tree* imports, by dotted name; *package* anchors relative imports."""
    owners = set(_OWNERS)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = _resolved(node, package)
            if base in owners:
                found.add(base)
            found.update(f"{base}.{a.name}" for a in node.names if f"{base}.{a.name}" in owners)
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name in owners)
    return found


def test_the_owner_import_scan_answers_both_ways() -> None:
    source = (
        "from kiro_crew.history_search_query import parse_search_query\n"
        "from kiro_crew import history_search_memo, platform_compat\n"
        "import kiro_crew.history_search\n"
    )
    assert _owner_imports(ast.parse(source)) == set(_OWNERS)
    relative = (
        "from ..history_search_query import SearchNeedle\nfrom .. import history_search_memo\n"
    )
    assert _owner_imports(ast.parse(relative), "kiro_crew.dashboard") == set(_OWNERS)
    sibling = "from .history_search_query import SearchNeedle\n"
    assert _owner_imports(ast.parse(sibling), "kiro_crew") == {_QUERY}
    assert _owner_imports(ast.parse(sibling), "kiro_crew.dashboard") == set()
    assert _owner_imports(ast.parse("from kiro_crew.history_search import SearchNeedle\n")) == set()


def test_each_owner_imports_only_owners_listed_before_it() -> None:
    """``_OWNERS`` lists the owners lowest layer first: an owner imports only owners
    earlier in the tuple, so the order is the import order."""
    later = {
        owner: sorted(_owner_imports(_tree(owner)) - set(_OWNERS[:i]))
        for i, owner in enumerate(_OWNERS)
    }
    assert later == {owner: [] for owner in _OWNERS}


def test_the_query_owner_imports_no_other_member_of_the_family(tmp_path: Path) -> None:
    """Imported alone in a fresh interpreter, the parse loads neither the memos nor the
    facade, so the layering the owner order states is the one the imports keep."""
    probe = (
        "import json, sys\n"
        "import kiro_crew.history_search_query\n"
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('kiro_crew.history'))))\n"
    )
    env = dict(os.environ)
    src = str(Path(importlib.import_module(_PACKAGE).__file__ or "").resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (src, env.get("PYTHONPATH"))))
    out = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        check=True,
        env=env,
        cwd=tmp_path,
        timeout=60,
        **UTF8_TEXT,
    )
    loaded = json.loads(out.stdout.splitlines()[-1])
    assert _QUERY in loaded
    assert [m for m in loaded if m in (_FACADE, _MEMO)] == []


def test_only_the_family_imports_an_owner() -> None:
    """Callers keep importing ``kiro_crew.history_search``; the owners are its own."""
    family = {"kiro_crew/history_search.py", "kiro_crew/history_search_memo.py"}
    root = source_corpus.src_root().parent
    scanned: list[str] = []
    offenders: list[str] = []
    for path, _text, tree in source_corpus.parsed_candidates(
        require_any=("history_search_query", "history_search_memo")
    ):
        relative = path.relative_to(root).as_posix()
        package = ".".join(Path(relative).with_suffix("").parts[:-1])
        scanned.append(relative)
        if relative not in family and _owner_imports(tree, package):
            offenders.append(relative)
    assert family <= set(scanned), scanned
    assert sorted(offenders) == []
