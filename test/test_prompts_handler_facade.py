"""The prompt and skill routes' facade keeps the patch seams the one module had.

``kiro_crew.dashboard.handlers.prompts`` is a mirror facade (:mod:`kiro_crew._facade_mirror`)
over the ``kiro_crew.dashboard.handlers.prompts_<role>`` owners. Callers, tests and documents
still reach every name as ``kiro_crew.dashboard.handlers.prompts.<name>``, and the moved code
reads its names as plain module globals, so a patch through the facade reaches that code
only because the mirror writes it into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules
(one object per name, the manifest, owners never importing their facade, no shared
global rebound). These tests pin what is particular to this facade: every name the test
trees patch through it reaches every module holding it; the owners are exactly the
``prompts_*.py`` siblings, loaded lowest layer first; the facade keeps no code of its own;
the routes stay the plain coroutines they were, each checking its own caller; the family
logs under the facade's name; and the facade still runs as ``python -m``.
"""

from __future__ import annotations

import ast
import functools
import importlib
import inspect
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus

from kiro_crew import _facade_mirror
from kiro_crew.dashboard.handlers import prompts
from kiro_crew.subprocess_utf8 import UTF8_TEXT

pytestmark = pytest.mark.xdist_group(name="tree_scan_prompts_handler_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("known_seams", "corpus_gate")

FACADE = "kiro_crew.dashboard.handlers.prompts"
OWNER_PREFIX = "kiro_crew.dashboard.handlers.prompts_"
HANDLERS_DIR = Path(prompts.__file__).resolve().parent
_THIS_FILE = Path(__file__).resolve()

#: Names the test trees are known to patch through the facade, one of each seam the
#: routes have: the platform capability constants the pinned create reads, caller gates
#: several owners call, and names the family only imports. The scan below must find all
#: of them, so a scanner that stops matching goes red instead of passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "_DIR_FD_SUPPORTED",
        "_IS_WINDOWS",
        "_UNNAMED_CREATE_SUPPORTED",
        "_assemble_skills_catalog_uncached",
        "_capability_manager",
        "_deny_foreign_app_skill_slot",
        "_find_prompt",
        "_get_skills",
        "_sel",
        "is_owner_dashboard_request",
        "requesting_slot_project",
        "safe_read_file_bytes_nolink",
        "session_skill_globs",
        "verified_replace_file_nolink",
    }
)

#: Every route the module served before its split.
_ROUTES = frozenset(
    {
        "api_prompt_detail",
        "api_prompts",
        "api_prompts_create",
        "api_skill_detail",
        "api_skill_file",
        "api_skill_inject_on_trigger",
        "api_skill_pending_approve",
        "api_skill_pending_detail",
        "api_skill_pending_dismiss",
        "api_skill_pin",
        "api_skill_tree",
        "api_skills",
        "api_skills_create",
        "api_skills_pending",
        "api_skills_pending_dismiss_all",
        "api_skills_trust",
        "api_skills_trust_grant",
        "api_skills_trust_revoke",
    }
)


def _index() -> _facade_mirror.MirrorIndex:
    return prompts.__dict__[_facade_mirror.INDEX_ATTRIBUTE]


def _owner_paths() -> dict[str, Path]:
    return {
        f"{OWNER_PREFIX}{path.stem[len('prompts_'):]}": path
        for path in sorted(HANDLERS_DIR.glob("prompts_*.py"))
    }


def _family() -> list[ModuleType]:
    return [prompts, *(sys.modules[name] for name in _index().owners)]


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
        if "handlers" not in text or "prompts" not in text:
            continue
        names = facade_mirror_scan.patched_names(text, {FACADE: FACADE})
        found |= names.get(FACADE, set())
    return frozenset(found)


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    """The reach the unsplit module had: a write to ``prompts.<name>`` was what every reader
    of that name read. Each module of the family that binds a patched name must see the
    patched object, and the undo must restore all of them."""
    family = _family()
    missed: dict[str, list[str]] = {}
    for name in sorted(_corpus_patched_names()):
        if not hasattr(prompts, name):
            continue
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(prompts, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(prompts, name) is not sentinel:
                stale.append(FACADE)
        if stale:
            missed[name] = stale
    assert missed == {}
    assert _index().divergent() == ()


def test_the_owners_are_every_prompts_sibling_in_layer_order() -> None:
    """``_OWNERS`` lists every ``prompts_*.py`` sibling, and each owner imports only owners
    listed before it -- the order the facade loads them and a read resolves in."""
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


def test_the_facade_keeps_the_default_read_path_and_no_code_of_its_own() -> None:
    """``prompts.py`` was a plain module, so no reader fetched a name of it through
    ``importlib.import_module``: the facade forwards no legacy table, keeps the default
    ``forward_reads``, defines no function or class, and serves every route from an owner."""
    index = _index()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports)
    tree = ast.parse(Path(prompts.__file__).read_text(encoding="utf-8"))
    defined = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    assert defined == []
    assert sorted(_ROUTES - set(index.exports)) == []


def test_every_route_stays_the_coroutine_it_was() -> None:
    """At BASE no route guard wrapped these routes; each checks its own caller
    (``_deny_non_owner_skill_operation`` and its siblings). The split adds no wrapper."""
    routes = {
        name: getattr(prompts, name)
        for name in sorted(set(dir(prompts)))
        if name.startswith("api_") and inspect.iscoroutinefunction(getattr(prompts, name))
    }
    assert set(routes) == _ROUTES
    assert sorted(name for name, fn in routes.items() if hasattr(fn, "__wrapped__")) == []
    owners = [importlib.import_module(name) for name in _index().owners]
    assert all(fn.__module__ in {owner.__name__ for owner in owners} for fn in routes.values())


def _home_imports() -> dict[str, tuple[str, str]]:
    """Served names an owner imports from a project module outside the family."""
    family = set(_index().owners)
    package = FACADE.rpartition(".")[0]
    found: dict[str, tuple[str, str]] = {}
    for path in _owner_paths().values():
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
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
    tree = ast.parse(Path(prompts.__file__).read_text(encoding="utf-8"))
    out: dict[str, tuple[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for stmt in node.body:
                if isinstance(stmt, ast.ImportFrom) and stmt.module not in family:
                    for alias in stmt.names:
                        out[alias.asname or alias.name] = (stmt.module or "", alias.name)
    return out


def test_every_name_served_from_outside_the_family_is_visible_to_type_checkers() -> None:
    """At BASE the module bound these by import, so ``from kiro_crew.dashboard.handlers.prompts
    import safe_read_file_bytes_nolink`` type-checked. Now an owner holds them, so the facade names each one
    from its home module under ``TYPE_CHECKING``, and nothing else."""
    served = set(_index().exports)
    expected = {name: home for name, home in _home_imports().items() if name in served}
    assert expected, "no served name comes from outside the family -- the scan went blind"
    assert _home_manifest() == expected


def test_the_family_logs_under_the_facade_name() -> None:
    """One logger, still named for the facade module, held by every module that logs."""
    assert prompts.logger.name == FACADE
    others = [
        m.__name__ for m in _family() if vars(m).get("logger", prompts.logger) is not prompts.logger
    ]
    assert others == []


def test_the_facade_runs_as_a_module(tmp_path: Path) -> None:
    """``python -m kiro_crew.dashboard.handlers.prompts`` imported cleanly before the split; the
    mirror installs on ``__main__`` too, because every member binds one logger by name."""
    env = dict(os.environ)
    src = str(Path(prompts.__file__).resolve().parents[3])
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
    assert source_corpus.repo_root() / "test" / "test_prompts_authoring.py" in walked
