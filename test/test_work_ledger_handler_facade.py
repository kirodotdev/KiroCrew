"""The work-ledger routes' facade keeps the patch seams the one module had.

``kiro_crew.dashboard.handlers.work_ledger`` is a mirror facade
(:mod:`kiro_crew._facade_mirror`) over the ``kiro_crew.dashboard.handlers.work_ledger_<role>``
owners. Its callers (the deferred route binder, session control and the Crew board reader
among them) and tests still reach every name as
``kiro_crew.dashboard.handlers.work_ledger.<name>``, and the moved code
reads its names as plain module globals, so a patch through the facade reaches that code
only because the mirror writes it into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules.
These tests pin what is particular to this facade: every name the test trees patch
through it reaches every module holding it; the owners are the ``work_ledger_*.py``
siblings except ``work_ledger_board.py`` (the Crew page's read, which imports the facade),
loaded lowest layer first, and only the family imports them; the facade keeps no code of
its own and serves every route the deferred binder names; the handlers package still
loads none of the family at boot; the names it serves from the rest of ``kiro_crew`` stay
visible to type checkers; the family logs under the facade's name; the moved dataclass
resolves through its own module; and the facade still runs as ``python -m``.
"""

from __future__ import annotations

import ast
import functools
import importlib
import inspect
import json
import os
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus

import kiro_crew
from kiro_crew import _facade_mirror
from kiro_crew.dashboard.handlers import work_ledger
from kiro_crew.subprocess_utf8 import UTF8_TEXT

pytestmark = pytest.mark.xdist_group(name="tree_scan_work_ledger_handler_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("known_seams", "corpus_gate")

FACADE = "kiro_crew.dashboard.handlers.work_ledger"
PACKAGE = "kiro_crew.dashboard.handlers"
HANDLERS_DIR = Path(work_ledger.__file__).resolve().parent
_SRC = Path(kiro_crew.__file__).resolve().parent
_THIS_FILE = Path(__file__).resolve()

#: The ``work_ledger_*.py`` sibling that is NOT an owner: the Crew page's masked read of
#: the same store, which imports the facade's gates and liveness helpers by name.
_NOT_AN_OWNER = "work_ledger_board"

#: Names the test trees are known to patch through the facade, one of each seam the
#: routes have: the audit row every owner writes, the gates the caller owner reads from
#: other modules, liveness probes, and functions the store path and session control call.
#: The scan below must find all of them, so a scanner that stops matching goes red.
_KNOWN_SEAMS = frozenset(
    {
        "_audit",
        "_is_restricted_session",
        "_mark_goal_recorded",
        "_recognize_session",
        "_slot_open",
        "_slot_running",
        "contained_channel_caller",
        "holds_ledger",
        "reaches_a_channel",
        "unit_for_session_key",
    }
)

#: Every route the module served before its split; ``server_runtime/mcp_routes.py`` binds
#: each one by name through ``_deferred_work_ledger``.
_ROUTES = frozenset(
    {
        "api_work_brief",
        "api_work_ledger_get",
        "api_work_ledger_rebuild",
        "api_work_ledger_record",
        "api_work_report",
    }
)


def _index() -> _facade_mirror.MirrorIndex:
    return work_ledger.__dict__[_facade_mirror.INDEX_ATTRIBUTE]


def _owner_paths() -> dict[str, Path]:
    return {
        f"{PACKAGE}.{path.stem}": path
        for path in sorted(HANDLERS_DIR.glob("work_ledger_*.py"))
        if path.stem != _NOT_AN_OWNER
    }


def _family() -> list[ModuleType]:
    return [work_ledger, *(sys.modules[name] for name in _index().owners)]


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
        if "handlers" not in text or "work_ledger" not in text:
            continue
        names = facade_mirror_scan.patched_names(text, {FACADE: FACADE})
        found |= names.get(FACADE, set())
    return frozenset(found)


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    """The reach the unsplit module had: a write to ``work_ledger.<name>`` was what every
    reader of that name read. Each module of the family that binds a patched name must see
    the patched object, and the undo must restore all of them."""
    family = _family()
    missed: dict[str, list[str]] = {}
    checked = 0
    for name in sorted(_corpus_patched_names()):
        if not hasattr(work_ledger, name):
            continue
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
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


def test_the_audit_row_is_held_by_every_owner_that_writes_one() -> None:
    """``_audit`` is defined once and called from five other owners; each holds it, so a
    facade patch of it sees every refusal the routes audit."""
    holders = sorted(m.__name__ for m in _family() if "_audit" in vars(m))
    assert holders == [
        f"{PACKAGE}.work_ledger_caller",
        f"{PACKAGE}.work_ledger_conductor",
        f"{PACKAGE}.work_ledger_read",
        f"{PACKAGE}.work_ledger_recording",
        f"{PACKAGE}.work_ledger_refusals",
        f"{PACKAGE}.work_ledger_worker",
    ]


def test_the_owners_are_the_work_ledger_siblings_in_layer_order() -> None:
    """``_OWNERS`` lists every ``work_ledger_*.py`` sibling but the Crew board reader, and
    each owner imports only owners listed before it -- the order the facade loads them and
    a read resolves in."""
    owners = list(_index().owners)
    assert sorted(owners) == sorted(_owner_paths())
    assert len(owners) == 7
    assert f"{PACKAGE}.{_NOT_AN_OWNER}" not in owners
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


def test_the_facade_keeps_the_default_read_path_and_no_code_of_its_own() -> None:
    """``work_ledger.py`` was a plain module, so no reader fetched a name of it through
    ``importlib.import_module``: the facade forwards no legacy table, keeps the default
    ``forward_reads``, defines no function or class, and serves every route from an owner."""
    index = _index()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports)
    tree = ast.parse(Path(work_ledger.__file__).read_text(encoding="utf-8"))
    defined = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    assert defined == []
    assert sorted(_ROUTES - set(index.exports)) == []


def test_every_route_the_deferred_binder_names_is_served_by_an_owner() -> None:
    """The binder resolves each handler BY NAME on the facade at the first request, so each
    name it binds must resolve to a coroutine function an owner defines."""
    mcp_routes = importlib.import_module("kiro_crew.dashboard.server_runtime.mcp_routes")
    bound = {
        node.args[0].value
        for node in ast.walk(ast.parse(inspect.getsource(mcp_routes)))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_deferred_work_ledger"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }
    assert bound == _ROUTES
    owners = set(_index().owners)
    for name in sorted(bound):
        handler = getattr(work_ledger, name)
        assert inspect.iscoroutinefunction(handler), name
        assert handler.__module__ in owners, name


def test_the_handlers_package_loads_no_member_of_the_family(tmp_path: Path) -> None:
    """``kirocrew-work`` is an opt-in MCP server, so the handlers package -- the gateway's
    boot path -- imports neither the facade nor any owner; the deferred binder does, on the
    first request."""
    env = dict(os.environ)
    src = str(Path(work_ledger.__file__).resolve().parents[3])
    env["PYTHONPATH"] = os.pathsep.join(p for p in (src, env.get("PYTHONPATH", "")) if p)
    probe = (
        "import json, sys\n"
        "import kiro_crew.dashboard.handlers  # noqa: F401\n"
        "print(json.dumps(sorted(m for m in sys.modules\n"
        f"    if m.startswith({FACADE!r}))))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        check=False,
        cwd=tmp_path,
        env=env,
        timeout=90,
        **UTF8_TEXT,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert json.loads(out.stdout.strip().splitlines()[-1]) == []


def _home_imports() -> dict[str, tuple[str, str]]:
    """Served names an owner binds by importing a project module outside the family:
    ``from home import name`` and ``import home as alias`` alike."""
    family = set(_index().owners)
    found: dict[str, tuple[str, str]] = {}
    for path in _owner_paths().values():
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname and alias.name.startswith("kiro_crew"):
                        found.setdefault(alias.asname, (alias.name, ""))
                continue
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            home = f"{PACKAGE}.{node.module}" if node.level == 1 else node.module
            if not home.startswith("kiro_crew") or home in family:
                continue
            for alias in node.names:
                if f"{home}.{alias.name}" not in family:
                    found.setdefault(alias.asname or alias.name, (home, alias.name))
    return found


def _home_manifest() -> dict[str, tuple[str, str]]:
    """The facade's ``TYPE_CHECKING`` imports from outside the family: name -> (home, original)."""
    family = set(_index().owners)
    tree = ast.parse(Path(work_ledger.__file__).read_text(encoding="utf-8"))
    out: dict[str, tuple[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for stmt in node.body:
                if isinstance(stmt, ast.Import):
                    for alias in stmt.names:
                        if alias.asname:
                            out[alias.asname] = (alias.name, "")
                if isinstance(stmt, ast.ImportFrom) and stmt.module not in family:
                    for alias in stmt.names:
                        out[alias.asname or alias.name] = (stmt.module or "", alias.name)
    return out


def test_every_name_served_from_outside_the_family_is_visible_to_type_checkers() -> None:
    """At BASE the module bound these by import, so ``from
    kiro_crew.dashboard.handlers.work_ledger import WorkLedgerError`` type-checked. Now an
    owner holds them, so the facade names each one from its home module under
    ``TYPE_CHECKING``, and nothing else."""
    served = set(_index().exports)
    expected = {name: home for name, home in _home_imports().items() if name in served}
    assert expected, "no served name comes from outside the family -- the scan went blind"
    assert _home_manifest() == expected


def test_the_family_logs_under_the_facade_name() -> None:
    """One logger, still named for the facade module, held by every module that logs."""
    assert work_ledger.logger.name == FACADE
    loggers = [m.__name__ for m in _family() if "logger" in vars(m)]
    assert len(loggers) >= 5
    others = [
        m.__name__
        for m in _family()
        if vars(m).get("logger", work_ledger.logger) is not work_ledger.logger
    ]
    assert others == []


def test_the_moved_dataclass_resolves_through_its_own_module_and_the_facade() -> None:
    """``SendHandback`` is a value session control carries from the read to the stamp; no
    log, message or pickle names its module, so it reports the owner it lives in, and the
    facade still hands out the same class."""
    cls = work_ledger.SendHandback
    assert cls.__module__ == f"{PACKAGE}.work_ledger_handback"
    assert getattr(importlib.import_module(cls.__module__), cls.__qualname__) is cls
    assert repr(cls("c", "w", "it_1", ())).startswith("SendHandback(")


def test_the_mutable_board_lock_registry_is_one_object() -> None:
    """``_BOARD_LOCKS`` is module state every write and rebuild serialises on, and tests
    reset it through the facade: one registry, bound by its one owner, served as is."""
    holders = [m.__name__ for m in _family() if "_BOARD_LOCKS" in vars(m)]
    assert holders == [f"{PACKAGE}.work_ledger_recording"]
    assert work_ledger._BOARD_LOCKS is vars(sys.modules[holders[0]])["_BOARD_LOCKS"]


def _imported_modules(node: ast.Import | ast.ImportFrom, module: str) -> list[str]:
    """Dotted names an import statement in *module* reaches, relative spellings resolved."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    base = node.module or ""
    if node.level:
        package = module.split(".")[: -node.level]
        base = ".".join([*package, *([node.module] if node.module else [])])
    return [base, *(f"{base}.{alias.name}" for alias in node.names)]


def _owner_importers(
    sources: Iterable[tuple[Path, str]], root: Path, owners: Iterable[str]
) -> set[str]:
    """The files among *sources* (relative to *root*) whose code imports one of *owners*."""
    wanted = set(owners)
    found = set()
    for path, text in sources:
        rel = path.relative_to(root)
        module = ".".join([root.name, *rel.with_suffix("").parts])
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.Import, ast.ImportFrom)) and any(
                name in wanted for name in _imported_modules(node, module)
            ):
                found.add(rel.as_posix())
    return found


def test_only_the_family_imports_an_owner() -> None:
    """A module outside the family that imported an owner would hold bindings no facade
    patch reaches; every caller -- the Crew board reader included -- goes through the
    facade instead."""
    owners = _index().owners
    family = {f"dashboard/handlers/{name.rpartition('.')[2]}.py" for name in (FACADE, *owners)}
    sources = source_corpus.iter_candidate_sources(require_all=("work_ledger_",))
    importers = _owner_importers(
        ((path, text) for path, text in sources if path.is_relative_to(_SRC)), _SRC, owners
    )
    assert "dashboard/handlers/work_ledger_handback.py" in importers  # lower-owner imports
    assert importers - family == set()


def test_the_owner_import_scan_finds_an_outside_importer(tmp_path: Path) -> None:
    root = tmp_path / "kiro_crew"
    owners = (f"kiro_crew.dashboard.handlers.work_ledger_{r}" for r in ("caller", "read"))
    sources = {
        root
        / "pkg"
        / "caller.py": (
            "from kiro_crew.dashboard.handlers.work_ledger_caller import _caller_key\n"
        ),
        root / "other.py": "from kiro_crew.dashboard.handlers import work_ledger_read as r\n",
        root / "dashboard" / "handlers" / "relative.py": "from .work_ledger_read import x\n",
        root / "board_user.py": "from kiro_crew.dashboard.handlers import work_ledger_board\n",
        root
        / "facade_user.py": ("from kiro_crew.dashboard.handlers.work_ledger import _caller_key\n"),
    }
    assert _owner_importers(sources.items(), root, owners) == {
        "pkg/caller.py",
        "other.py",
        "dashboard/handlers/relative.py",
    }


def test_the_facade_runs_as_a_module(tmp_path: Path) -> None:
    """``python -m kiro_crew.dashboard.handlers.work_ledger`` imported cleanly before the
    split; the mirror installs on ``__main__`` too, because every member binds one logger
    by name."""
    env = dict(os.environ)
    src = str(Path(work_ledger.__file__).resolve().parents[3])
    env["PYTHONPATH"] = os.pathsep.join(p for p in (src, env.get("PYTHONPATH", "")) if p)
    out = subprocess.run(
        [sys.executable, "-m", FACADE],
        capture_output=True,
        check=False,
        cwd=tmp_path,
        env=env,
        timeout=90,
        **UTF8_TEXT,
    )
    assert out.returncode == 0, out.stdout + out.stderr


def test_the_test_tree_walk_reaches_this_file() -> None:
    walked = {path.resolve() for path in _test_tree_files()}
    assert _THIS_FILE in walked
    assert source_corpus.repo_root() / "test" / "test_work_ledger_tools.py" in walked
