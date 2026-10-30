"""The Knowledge Library handlers' facade keeps the patch seams the one module had.

``kiro_crew.dashboard.handlers.knowledge`` is a mirror facade (:mod:`kiro_crew._facade_mirror`)
over the ``kiro_crew.dashboard.handlers.knowledge_<role>`` owners and keeps no code of its own.
Callers, tests and documents still reach every name as
``kiro_crew.dashboard.handlers.knowledge.<name>``, and the moved code reads its names as plain
module globals, so a patch through the facade reaches that code only because the mirror writes
it into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules (one
object per name, the manifest, owners never importing their facade, no shared global
rebound). These tests pin what is particular to this facade: every name the test trees patch
through it reaches every module holding it; the owners are exactly the ``knowledge_*.py``
siblings, loaded lowest layer first; the route table still registers the handlers the facade
serves; the family logs under the facade's name; and the facade still runs as ``python -m``.
"""

from __future__ import annotations

import ast
import functools
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import facade_mirror_scan
import pytest
import source_corpus
from aiohttp import web

from kiro_crew import _facade_mirror
from kiro_crew.dashboard.handlers import knowledge
from kiro_crew.subprocess_utf8 import UTF8_TEXT

pytestmark = pytest.mark.xdist_group(name="tree_scan_knowledge_handlers_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("known_seams", "corpus_gate")

FACADE = "kiro_crew.dashboard.handlers.knowledge"
OWNER_PREFIX = "kiro_crew.dashboard.handlers.knowledge_"
HANDLERS_DIR = Path(knowledge.__file__).resolve().parent
_THIS_FILE = Path(__file__).resolve()

#: Names the test trees are known to patch through the facade, one of each seam the
#: handlers have: the SEL audit and its sink, the app's store, the retriever and the embed
#: pool the searches use, the sync status write and claim, the web-page source's fetch,
#: the sensitive-path fence, the embedding rebuild, the embedder factory, the folder dialog
#: and an upload archive cap. The scan below must find all of them, so a scanner that stops
#: matching goes red instead of passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "_sel_log",
        "sel",
        "_store",
        "HybridRetriever",
        "run_in_embed_pool",
        "_set_sync_status",
        "_claim_sync",
        "fetch_page",
        "is_sensitive_path",
        "rebuild_embeddings",
        "_create_embedder",
        "_run_folder_dialog",
        "_MAX_INGEST_ARCHIVE_MEMBERS",
    }
)


def _index() -> _facade_mirror.MirrorIndex:
    return knowledge.__dict__[_facade_mirror.INDEX_ATTRIBUTE]


def _owner_paths() -> dict[str, Path]:
    return {
        f"{OWNER_PREFIX}{path.stem[len('knowledge_'):]}": path
        for path in sorted(HANDLERS_DIR.glob("knowledge_*.py"))
    }


def _family() -> list[ModuleType]:
    return [knowledge, *(sys.modules[name] for name in _index().owners)]


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
        if "handlers" not in text or "knowledge" not in text:
            continue
        names = facade_mirror_scan.patched_names(text, {FACADE: FACADE})
        found |= names.get(FACADE, set())
    return frozenset(found)


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    """The reach the unsplit module had: a write to ``knowledge.<name>`` was what every
    reader of that name read. Each module of the family that binds a patched name must
    see the patched object, and the undo must restore all of them."""
    family = _family()
    missed: dict[str, list[str]] = {}
    for name in sorted(_corpus_patched_names()):
        if not hasattr(knowledge, name):
            continue
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(knowledge, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(knowledge, name) is not sentinel:
                stale.append(FACADE)
        if stale:
            missed[name] = stale
    assert missed == {}
    assert _index().divergent() == ()


def test_the_owners_are_every_knowledge_sibling_in_layer_order() -> None:
    """``_OWNERS`` lists every ``knowledge_*.py`` sibling, and each owner imports only
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


def test_the_facade_keeps_the_default_read_path_and_no_code_of_its_own() -> None:
    """Every name is served from the index, through the default read path, and the facade
    defines no function or class: new Knowledge Library code goes to the owner of its
    responsibility."""
    index = _index()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports)
    tree = ast.parse(Path(knowledge.__file__).read_text(encoding="utf-8"))
    defined = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    assert defined == []


def test_the_route_table_registers_the_handlers_the_facade_serves() -> None:
    """``setup_knowledge_routes`` runs in its owner and registers the very handler objects
    a read through the facade returns, so a route and a test name one function."""
    app = web.Application()
    app["state"] = MagicMock()
    app["knowledge_pipeline"] = MagicMock()
    knowledge.setup_knowledge_routes(app)
    registered = {
        route.handler
        for route in app.router.routes()
        if route.resource is not None and route.resource.canonical.startswith("/api/knowledge/")
    }
    assert registered
    unserved = sorted(
        handler.__name__
        for handler in registered
        if getattr(knowledge, handler.__name__, None) is not handler
    )
    assert unserved == []


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
    tree = ast.parse(Path(knowledge.__file__).read_text(encoding="utf-8"))
    out: dict[str, tuple[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for stmt in node.body:
                if isinstance(stmt, ast.ImportFrom) and stmt.module not in family:
                    for alias in stmt.names:
                        out[alias.asname or alias.name] = (stmt.module or "", alias.name)
    return out


def test_every_name_served_from_outside_the_family_is_visible_to_type_checkers() -> None:
    """An owner binds these by import, so the facade names each one from its home module
    under ``TYPE_CHECKING`` and ``from kiro_crew.dashboard.handlers.knowledge import
    HybridRetriever`` type-checks."""
    served = set(_index().exports)
    expected = {name: home for name, home in _home_imports().items() if name in served}
    assert expected, "no served name comes from outside the family -- the scan went blind"
    assert _home_manifest() == expected


def test_the_family_logs_under_the_facade_name() -> None:
    """One logger, still named for the facade module, held by every module that logs."""
    assert knowledge.logger.name == FACADE
    others = [
        m.__name__
        for m in _family()
        if vars(m).get("logger", knowledge.logger) is not knowledge.logger
    ]
    assert others == []


def test_the_facade_runs_as_a_module(tmp_path: Path) -> None:
    """``python -m kiro_crew.dashboard.handlers.knowledge`` imports cleanly: the mirror
    installs on ``__main__`` too, because every member binds one logger by name."""
    env = dict(os.environ)
    src = str(Path(knowledge.__file__).resolve().parents[3])
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
    assert source_corpus.repo_root() / "test" / "test_knowledge_handlers_coverage.py" in walked
