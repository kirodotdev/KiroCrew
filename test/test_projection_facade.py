"""The crew-log fold facade keeps the patch seams the one module had.

``kiro_crew.crew_log.projection`` is a mirror facade (:mod:`kiro_crew._facade_mirror`)
over the ``kiro_crew.crew_log.projection_<role>`` owners. Callers, tests and documents
still reach every name as ``kiro_crew.crew_log.projection.<name>``, and the moved code
reads its names as plain module globals, so a patch through the facade reaches that
code only because the mirror writes it into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules
(one object per name, the manifest, owners never importing their facade, no shared
global rebound). These tests pin what is particular to this facade: every name the test
trees patch through it, by any spelling they use, reaches every module holding it; the
owners are exactly the ``projection_*.py`` siblings, loaded lowest layer first; the folds
keep logging under the facade's name; and a facade write of the revision counter still
moves the counter.
"""

from __future__ import annotations

import ast
import functools
import importlib
import inspect
import sys
from pathlib import Path
from types import ModuleType

import pytest
import source_corpus

from kiro_crew import _facade_mirror
from kiro_crew.crew_log import projection

pytestmark = pytest.mark.xdist_group(name="tree_scan_projection_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("known_seams", "spelling_cases")

FACADE = "kiro_crew.crew_log.projection"
OWNER_PREFIX = "kiro_crew.crew_log.projection_"
CREW_LOG_DIR = Path(projection.__file__).resolve().parent

#: Names the test trees are known to patch through the facade, from every kind of seam
#: the folds have: a limit an owner reads, an owner's mutable state, a function several
#: owners call, and names the facade only imports. The scan below must find all of them,
#: so a scanner that stops matching goes red instead of passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "RADAR_ITEM_LIMIT",
        "RADAR_SKIP_LIMIT",
        "open_session_log",
        "units_in_succession",
        "session_units_for_slot",
        "unit_header_created_at",
        "unit_opened_previous",
        "WORK_TOMBSTONE_LIMIT",
        "read_slot_projection",
        "read_tree_projection",
        "fold_session",
        "_log_identity",
        "_slot_memos",
        "_unit_mark",
        "read_projection",
    }
)


def _index() -> _facade_mirror.MirrorIndex:
    return projection.__dict__[_facade_mirror.INDEX_ATTRIBUTE]


def _owner_paths() -> dict[str, Path]:
    return {
        f"{OWNER_PREFIX}{path.stem[len('projection_'):]}": path
        for path in sorted(CREW_LOG_DIR.glob("projection_*.py"))
    }


def _family() -> list[ModuleType]:
    return [projection, *(sys.modules[name] for name in _index().owners)]


# --------------------------------------------------------------------------- #
# The patch scan
# --------------------------------------------------------------------------- #


def _dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        head = _dotted(node.value)
        return None if head is None else f"{head}.{node.attr}"
    return None


def _returns_the_facade(module_name: str, function_name: str) -> bool:
    """Whether ``<module>.<function>()`` is a zero-argument helper that returns the facade,
    the lazy-import shape (``_projection()``, ``_crew_log()``) some modules use."""
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return False
    function = getattr(module, function_name, None)
    if not inspect.isfunction(function):
        return False
    required = [
        p
        for p in inspect.signature(function).parameters.values()
        if p.default is inspect.Parameter.empty
        and p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    ]
    if required or "crew_log import projection" not in inspect.getsource(function):
        return False
    return function() is projection


class _Receivers:
    """Which expressions in one test file name the facade module."""

    def __init__(self, tree: ast.Module) -> None:
        self.aliases: set[str] = set()
        self.modules: dict[str, str] = {}
        assigns: list[ast.Assign] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                for alias in node.names:
                    full = f"{node.module}.{alias.name}"
                    local = alias.asname or alias.name
                    if full == FACADE:
                        self.aliases.add(local)
                    elif full.startswith("kiro_crew."):
                        self.modules[local] = full
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == FACADE:
                        self.aliases.add(alias.asname or FACADE)
                    elif alias.name.startswith("kiro_crew.") and alias.asname:
                        self.modules[alias.asname] = alias.name
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                assigns.append(node)
        for node in assigns:
            call = node.value
            assert isinstance(call, ast.Call)
            imports_facade = (
                _dotted(call.func) in {"importlib.import_module", "import_module"}
                and bool(call.args)
                and isinstance(call.args[0], ast.Constant)
                and call.args[0].value == FACADE
            )
            if imports_facade or self.names_facade(call):
                self.aliases.update(t.id for t in node.targets if isinstance(t, ast.Name))

    def names_facade(self, node: ast.expr) -> bool:
        """Whether *node* is the facade: an alias of it, ``<module>.<attr>`` bound to it,
        or a zero-argument ``<module>.<helper>()`` that returns it."""
        if isinstance(node, ast.Call):
            if node.args or node.keywords:
                return False
            dotted = _dotted(node.func)
            if dotted is None:
                return False
            head, _, attr = dotted.rpartition(".")
            module_name = self.modules.get(head)
            return module_name is not None and _returns_the_facade(module_name, attr)
        dotted = _dotted(node)
        if dotted is None:
            return False
        if dotted in self.aliases:
            return True
        head, _, attr = dotted.rpartition(".")
        module_name = self.modules.get(head)
        if module_name is None or not attr:
            return False
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            return False
        return getattr(module, attr, None) is projection


def patched_names(source: str) -> set[str]:
    """Every name *source* patches on the facade, by any spelling a test uses."""
    tree = ast.parse(source)
    receivers = _Receivers(tree)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.Delete)):
            targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and receivers.names_facade(target.value):
                    found.add(target.attr)
            continue
        if not isinstance(node, ast.Call):
            continue
        func = _dotted(node.func) or ""
        leaf = func.rpartition(".")[2]
        args = node.args
        if leaf in {"setattr", "delattr", "object"} and len(args) >= 2:
            name = args[1]
            if isinstance(name, ast.Constant) and isinstance(name.value, str):
                if receivers.names_facade(args[0]):
                    found.add(name.value)
                continue
        if leaf == "multiple" and args and receivers.names_facade(args[0]):
            found.update(k.arg for k in node.keywords if k.arg)
            continue
        if leaf in {"setattr", "delattr", "patch"} and args:
            target = args[0]
            if isinstance(target, ast.Constant) and isinstance(target.value, str):
                head, _, name = target.value.rpartition(".")
                if head == FACADE:
                    found.add(name)
    return found


def _test_tree_files() -> list[Path]:
    root = source_corpus.repo_root()
    out = []
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        if rel == "conftest.py" or rel.startswith("test/") or "/tests/" in rel:
            out.append(path)
    return out


@functools.lru_cache(maxsize=1)
def _corpus_patched_names() -> frozenset[str]:
    """Every name the test trees patch through the facade, scanned once per worker."""
    found: set[str] = set()
    for path in _test_tree_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if "crew_log" not in text or "projection" not in text:
            continue
        found |= patched_names(text)
    return frozenset(found)


#: Each spelling the patch scan must read, and each it must skip, keyed by case id.
_SPELLING_CASES = {
    "monkeypatch-alias": (
        "from kiro_crew.crew_log import projection\n"
        "def t(monkeypatch):\n    monkeypatch.setattr(projection, 'A', 1)\n",
        {"A"},
    ),
    "delattr-renamed-alias": (
        "from kiro_crew.crew_log import projection as crew_log\n"
        "def t(monkeypatch):\n    monkeypatch.delattr(crew_log, 'B')\n",
        {"B"},
    ),
    "patch-object-import-as": (
        "import kiro_crew.crew_log.projection as P\n"
        "from unittest import mock\n"
        "def t():\n    with mock.patch.object(P, 'C'):\n        pass\n",
        {"C"},
    ),
    "dotted-setattr": (
        "def t(monkeypatch):\n" "    monkeypatch.setattr('kiro_crew.crew_log.projection.D', 1)\n",
        {"D"},
    ),
    "mock-patch-string": (
        "from unittest.mock import patch\n"
        "def t():\n    with patch('kiro_crew.crew_log.projection.E'):\n        pass\n",
        {"E"},
    ),
    "import-module": (
        "import importlib\n"
        "P = importlib.import_module('kiro_crew.crew_log.projection')\n"
        "def t(monkeypatch):\n    monkeypatch.setattr(P, 'F', 1)\n",
        {"F"},
    ),
    "module-attribute-receiver": (
        "from kiro_crew.crew_log import read as crew_log_read\n"
        "def t(monkeypatch):\n    monkeypatch.setattr(crew_log_read.projections, 'G', 1)\n",
        {"G"},
    ),
    "patch-multiple": (
        "from kiro_crew.crew_log import projection\n"
        "from unittest import mock\n"
        "def t():\n    with mock.patch.multiple(projection, H=1):\n        pass\n",
        {"H"},
    ),
    "plain-assignment-and-del": (
        "from kiro_crew.crew_log import projection as crew_log\n"
        "def t():\n    crew_log.I = 1\n    del crew_log.J\n",
        {"I", "J"},
    ),
    "helper-call-receiver": (
        "from kiro_crew.dashboard.handlers import crew_log as routes\n"
        "def t(monkeypatch):\n    monkeypatch.setattr(routes._crew_log(), 'K', 1)\n",
        {"K"},
    ),
    "local-bound-from-helper": (
        "from kiro_crew.apps.builtins.issue_radar.backend import crew_store as cs\n"
        "def t(monkeypatch):\n"
        "    projection = cs._projection()\n"
        "    monkeypatch.setattr(projection, 'L', 1)\n",
        {"L"},
    ),
    # Another module's patch, and a facade patch of a name chosen at run time.
    "other-module": (
        "from kiro_crew.crew_log import store\n"
        "def t(monkeypatch):\n    monkeypatch.setattr(store, 'X', 1)\n",
        set(),
    ),
    "name-chosen-at-run-time": (
        "from kiro_crew.crew_log import projection\n"
        "def t(monkeypatch, name):\n    monkeypatch.setattr(projection, name, 1)\n",
        set(),
    ),
    "an-owner-path-is-not-the-facade": (
        "def t(monkeypatch):\n"
        "    monkeypatch.setattr('kiro_crew.crew_log.projection_usage.Y', 1)\n",
        set(),
    ),
}


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    """The reach the unsplit module had: a write to ``projection.<name>`` was what
    every reader of that name read. Each module of the family that binds a patched name
    must see the patched object, and the undo must restore all of them."""
    family = _family()
    missed: dict[str, list[str]] = {}
    for name in sorted(_corpus_patched_names()):
        if not hasattr(projection, name):
            continue
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(projection, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(projection, name) is not sentinel:
                stale.append(FACADE)
        if stale:
            missed[name] = stale
    assert missed == {}
    assert _index().divergent() == ()


def test_the_owners_are_every_projection_module_in_layer_order() -> None:
    """``_OWNERS`` lists every ``projection_*.py`` sibling, and each owner imports only
    owners listed before it -- the order the facade loads them and a read resolves in."""
    owners = list(_index().owners)
    assert sorted(owners) == sorted(_owner_paths())
    late: dict[str, list[str]] = {}
    for position, owner in enumerate(owners):
        tree = ast.parse(_owner_paths()[owner].read_text(encoding="utf-8"))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        later = sorted(m for m in imported if m in owners and owners.index(m) >= position)
        if later:
            late[owner] = later
    assert late == {}


def test_the_facade_keeps_the_default_read_path_and_forwards_no_table() -> None:
    """``projection.py`` was a plain module, so no reader ever fetched a name of it
    through ``importlib.import_module``: the facade forwards no legacy table and keeps
    the default ``forward_reads``, and every served name comes from the index."""
    index = _index()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports)


def _home_imports() -> dict[str, tuple[str, str]]:
    """Served names an owner imports from a project module outside the family."""
    family = set(_index().owners)
    found: dict[str, tuple[str, str]] = {}
    for path in _owner_paths().values():
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                if not node.module.startswith("kiro_crew") or node.module in family:
                    continue
                for alias in node.names:
                    if f"{node.module}.{alias.name}" not in family:
                        found.setdefault(alias.asname or alias.name, (node.module, alias.name))
    return found


def _home_manifest() -> dict[str, tuple[str, str]]:
    """The facade's ``TYPE_CHECKING`` imports from outside the family: name -> (home, original)."""
    family = set(_index().owners)
    tree = ast.parse(Path(projection.__file__).read_text(encoding="utf-8"))
    out: dict[str, tuple[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.If) and _dotted(node.test) == "TYPE_CHECKING":
            for stmt in node.body:
                if isinstance(stmt, ast.ImportFrom) and stmt.module not in family:
                    if stmt.module == "kiro_crew.crew_log.checkpoint":
                        continue  # the facade's own type-only imports for its annotations
                    for alias in stmt.names:
                        out[alias.asname or alias.name] = (stmt.module or "", alias.name)
    return out


def test_every_name_served_from_outside_the_family_is_visible_to_type_checkers() -> None:
    """At BASE the facade re-bound these by import, so ``from kiro_crew.crew_log.projection
    import RADAR_PHASES`` type-checked. Now an owner holds them, so the facade names each one
    from its home module under ``TYPE_CHECKING``, and exactly those."""
    served = set(_index().exports)
    expected = {name: home for name, home in _home_imports().items() if name in served}
    assert expected, "no served name comes from outside the family -- the scan went blind"
    assert _home_manifest() == expected


def test_the_folds_log_under_the_facade_name() -> None:
    """One logger, still named for the facade module, held by every module that logs."""
    assert projection.logger.name == FACADE
    others = [
        m.__name__
        for m in _family()
        if vars(m).get("logger", projection.logger) is not projection.logger
    ]
    assert others == []


def test_a_facade_write_of_the_revision_counter_moves_the_counter() -> None:
    """``projection._slot_revision = n`` set the counter the next revision is minted
    from, and still does, wherever ``_next_slot_revision`` lives."""
    # Under the guard every minting caller holds, so no other thread mints between the
    # patch and the read, and the undo restores the counter before anyone mints again.
    with projection._slot_memo_guard, pytest.MonkeyPatch.context() as patch:
        patch.setattr(projection, "_slot_revision", 41)
        assert projection._next_slot_revision() == 42
        assert projection._slot_revision == 42
