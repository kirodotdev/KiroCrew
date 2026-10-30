"""The Crew Members routes' facade keeps the patch seams the one module had.

``kiro_crew.dashboard.handlers.members`` is a mirror facade (:mod:`kiro_crew._facade_mirror`)
over the ``kiro_crew.dashboard.handlers.members_<role>`` owners. Callers, tests and documents
still reach every name as ``kiro_crew.dashboard.handlers.members.<name>``, and the moved code
reads its names as plain module globals, so a patch through the facade reaches that code
only because the mirror writes it into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules
(one object per name, the manifest, owners never importing their facade, no shared
global rebound). These tests pin what is particular to this facade: every name the test
trees patch through it reaches every module holding it; the owners are exactly the
``members_*.py`` siblings, loaded lowest layer first, and only the family imports them;
the facade keeps no code of its own and serves every route the handlers package
re-exports; the names it serves from the rest of ``kiro_crew`` stay visible to type
checkers; the family logs under the facade's name; and the facade still runs as
``python -m``.
"""

from __future__ import annotations

import ast
import functools
import importlib
import inspect
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
from kiro_crew.dashboard.handlers import members
from kiro_crew.subprocess_utf8 import UTF8_TEXT

pytestmark = pytest.mark.xdist_group(name="tree_scan_members_handler_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("known_seams", "corpus_gate")

FACADE = "kiro_crew.dashboard.handlers.members"
OWNER_PREFIX = "kiro_crew.dashboard.handlers.members_"
HANDLERS_DIR = Path(members.__file__).resolve().parent
_SRC = Path(kiro_crew.__file__).resolve().parent
_THIS_FILE = Path(__file__).resolve()

#: Names the test trees are known to patch through the facade, one of each seam the
#: routes have: a constant an owner reads, functions several owners call, names the
#: family only imports, and the late-bound SEL accessor. The scan below must find all of
#: them, so a scanner that stops matching goes red instead of passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "_FRESH_START_STOP_WAIT_SECS",
        "_logged_slugs",
        "_member_names_for_slug",
        "_sel",
        "default_project_dir",
        "load_config_with_content_stamp",
        "pin_private_agent_store",
        "require_owner_dashboard_request",
    }
)

#: Every route the module served before its split; the handlers package re-exports each.
_ROUTES = frozenset(
    {
        "api_member_activity",
        "api_member_briefing",
        "api_member_fresh_start",
        "api_member_greet",
        "api_member_projections",
        "api_member_rules_get",
        "api_member_rules_put",
        "api_member_thread",
        "api_members",
    }
)


def _index() -> _facade_mirror.MirrorIndex:
    return members.__dict__[_facade_mirror.INDEX_ATTRIBUTE]


def _owner_paths() -> dict[str, Path]:
    return {
        f"{OWNER_PREFIX}{path.stem[len('members_'):]}": path
        for path in sorted(HANDLERS_DIR.glob("members_*.py"))
    }


def _family() -> list[ModuleType]:
    return [members, *(sys.modules[name] for name in _index().owners)]


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
        if "handlers" not in text or "members" not in text:
            continue
        names = facade_mirror_scan.patched_names(text, {FACADE: FACADE})
        found |= names.get(FACADE, set())
    return frozenset(found)


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    """The reach the unsplit module had: a write to ``members.<name>`` was what every reader
    of that name read. Each module of the family that binds a patched name must see the
    patched object, and the undo must restore all of them."""
    family = _family()
    missed: dict[str, list[str]] = {}
    checked = 0
    for name in sorted(_corpus_patched_names()):
        if not hasattr(members, name):
            continue
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
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


def test_a_shared_seam_is_held_by_every_owner_that_reads_it() -> None:
    """``_member_names_for_slug`` is defined once and read by three route owners; each
    holds it, so a facade patch of it reaches every route that asks the question."""
    holders = sorted(m.__name__ for m in _family() if "_member_names_for_slug" in vars(m))
    assert holders == [
        f"{OWNER_PREFIX}access",
        f"{OWNER_PREFIX}drawer",
        f"{OWNER_PREFIX}rules",
        f"{OWNER_PREFIX}thread",
    ]


def test_the_owners_are_every_members_sibling_in_layer_order() -> None:
    """``_OWNERS`` lists every ``members_*.py`` sibling, and each owner imports only owners
    listed before it -- the order the facade loads them and a read resolves in."""
    owners = list(_index().owners)
    assert sorted(owners) == sorted(_owner_paths())
    assert len(owners) == 6
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
    """``members.py`` was a plain module, so no reader fetched a name of it through
    ``importlib.import_module``: the facade forwards no legacy table, keeps the default
    ``forward_reads``, defines no function or class, and serves every route from an owner."""
    index = _index()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports)
    tree = ast.parse(Path(members.__file__).read_text(encoding="utf-8"))
    defined = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    assert defined == []
    assert sorted(_ROUTES - set(index.exports)) == []


def test_every_route_is_the_one_the_handlers_package_re_exports() -> None:
    """The route table reaches these handlers through the package re-export, so the
    package and the facade hand out the same function objects."""
    routes = {
        name: getattr(members, name)
        for name in sorted(set(dir(members)))
        if name.startswith("api_") and inspect.iscoroutinefunction(getattr(members, name))
    }
    assert set(routes) == _ROUTES
    package = importlib.import_module("kiro_crew.dashboard.handlers")
    assert {name: getattr(package, name) for name in routes} == routes
    assert sorted(fn.__module__ for fn in routes.values() if fn.__module__ == FACADE) == []


def _home_imports() -> dict[str, tuple[str, str]]:
    """Served names an owner binds by importing a project module outside the family:
    ``from home import name`` and ``import home as alias`` alike."""
    family = set(_index().owners)
    package = FACADE.rpartition(".")[0]
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
            home = f"{package}.{node.module}" if node.level == 1 else node.module
            if not home.startswith("kiro_crew") or home in family:
                continue
            for alias in node.names:
                if f"{home}.{alias.name}" not in family:
                    found.setdefault(alias.asname or alias.name, (home, alias.name))
    return found


def _home_manifest() -> dict[str, tuple[str, str]]:
    """The facade's ``TYPE_CHECKING`` imports from outside the family: name -> (home, original)."""
    family = set(_index().owners)
    tree = ast.parse(Path(members.__file__).read_text(encoding="utf-8"))
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
    """At BASE the module bound these by import, so ``from kiro_crew.dashboard.handlers.members
    import KiroCrewConfig`` type-checked. Now an owner holds them, so the facade names each
    one from its home module under ``TYPE_CHECKING``, and nothing else."""
    served = set(_index().exports)
    expected = {name: home for name, home in _home_imports().items() if name in served}
    assert expected, "no served name comes from outside the family -- the scan went blind"
    assert _home_manifest() == expected


def test_the_family_logs_under_the_facade_name() -> None:
    """One logger, still named for the facade module, held by every module that logs."""
    assert members.logger.name == FACADE
    loggers = [m.__name__ for m in _family() if "logger" in vars(m)]
    assert len(loggers) >= 5
    others = [
        m.__name__ for m in _family() if vars(m).get("logger", members.logger) is not members.logger
    ]
    assert others == []


def test_the_facade_runs_as_a_module(tmp_path: Path) -> None:
    """``python -m kiro_crew.dashboard.handlers.members`` imported cleanly before the split;
    the mirror installs on ``__main__`` too, because every member binds one logger by name."""
    env = dict(os.environ)
    src = str(Path(members.__file__).resolve().parents[3])
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
                name.startswith(OWNER_PREFIX) for name in _imported_modules(node, module)
            ):
                found.add(rel.as_posix())
    return found


def test_only_the_family_imports_an_owner() -> None:
    """A module outside the family that imported an owner would hold bindings no facade
    patch reaches; every caller goes through the facade instead."""
    family = {
        f"dashboard/handlers/{name.rpartition('.')[2]}.py" for name in (FACADE, *_index().owners)
    }
    sources = source_corpus.iter_candidate_sources(require_all=("members_",))
    importers = _owner_importers(
        ((path, text) for path, text in sources if path.is_relative_to(_SRC)), _SRC
    )
    assert "dashboard/handlers/members_drawer.py" in importers  # owners import lower owners
    assert importers - family == set()


def test_the_owner_import_scan_finds_an_outside_importer(tmp_path: Path) -> None:
    root = tmp_path / "kiro_crew"
    sources = {
        root
        / "pkg"
        / "caller.py": (
            "from kiro_crew.dashboard.handlers.members_access import _deny_app_caller\n"
        ),
        root / "other.py": "from kiro_crew.dashboard.handlers import members_roster as low\n",
        root / "dashboard" / "handlers" / "relative.py": "from .members_thread import x\n",
        root / "dashboard" / "deep" / "up.py": "from ..handlers import members_rules\n",
        root
        / "facade_user.py": ("from kiro_crew.dashboard.handlers.members import _deny_app_caller\n"),
    }
    assert _owner_importers(sources.items(), root) == {
        "pkg/caller.py",
        "other.py",
        "dashboard/handlers/relative.py",
        "dashboard/deep/up.py",
    }


def test_the_test_tree_walk_reaches_this_file() -> None:
    walked = {path.resolve() for path in _test_tree_files()}
    assert _THIS_FILE in walked
    assert source_corpus.repo_root() / "test" / "test_member_fresh_start.py" in walked
