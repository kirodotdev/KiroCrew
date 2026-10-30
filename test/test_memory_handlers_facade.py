"""The memory API handlers' facade keeps the patch seams the one module had.

``kiro_crew.dashboard.handlers.memory`` is a mirror facade (:mod:`kiro_crew._facade_mirror`)
over three owners: ``memory_services`` (the SEL and the vector store), ``memory_embeddings``
(the embedding model) and ``memory_graph`` (building the memory graph). Callers, tests and
documents still reach every name as ``kiro_crew.dashboard.handlers.memory.<name>``, and the
moved code reads its names as plain module globals, so a patch through the facade reaches that
code only because the mirror writes it into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules. These
tests pin what is particular to this facade: every name the test trees patch through it
reaches every module holding it; the owners load lowest layer first; the routes the facade
keeps stay bound there; the embedding setup status lives in one owner only, which rebinds it;
the family logs under the facade's name; and the facade still runs as ``python -m``.
"""

from __future__ import annotations

import ast
import functools
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus

from kiro_crew import _facade_mirror
from kiro_crew.dashboard.handlers import memory
from kiro_crew.subprocess_utf8 import UTF8_TEXT

pytestmark = pytest.mark.xdist_group(name="tree_scan_memory_handlers_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("known_seams", "corpus_gate")

FACADE = "kiro_crew.dashboard.handlers.memory"
HANDLERS_DIR = Path(memory.__file__).resolve().parent
#: The owners, lowest layer first. ``handlers/`` holds other ``memory_*.py`` handler
#: modules of its own (the admin, editor and member routes), so the set is spelled out.
OWNERS = (
    "kiro_crew.dashboard.handlers.memory_services",
    "kiro_crew.dashboard.handlers.memory_embeddings",
    "kiro_crew.dashboard.handlers.memory_graph",
)
_THIS_FILE = Path(__file__).resolve()

#: Names the test trees are known to patch through the facade, one of each seam the
#: handlers have: the SEL and the shared memory the routes read, the write gate and the
#: vector-tier lookup the facade keeps, the embedding model's config write and apply, the
#: model download and the sandboxed pip spawn they drive, and the config path and writer
#: both halves of the family use. The scan below must find all of them.
_KNOWN_SEAMS = frozenset(
    {
        "_sel",
        "_get_memory",
        "_memory_write_gate",
        "_vector_tier_for_request",
        "_write_embed_model_config",
        "_apply_embedding_model",
        "model_download_manager",
        "create_subprocess_limited",
        "get_shared_embedder",
        "config_path",
        "run_config_write",
    }
)


def _index() -> _facade_mirror.MirrorIndex:
    return memory.__dict__[_facade_mirror.INDEX_ATTRIBUTE]


def _owner_path(owner: str) -> Path:
    return HANDLERS_DIR / f"{owner.rsplit('.', 1)[1]}.py"


def _family() -> list[ModuleType]:
    return [memory, *(sys.modules[name] for name in _index().owners)]


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
        if "handlers" not in text or "memory" not in text:
            continue
        names = facade_mirror_scan.patched_names(text, {FACADE: FACADE})
        found |= names.get(FACADE, set())
    return frozenset(found)


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    """The reach the unsplit module had: a write to ``memory.<name>`` was what every
    reader of that name read. Each module of the family that binds a patched name must
    see the patched object, and the undo must restore all of them."""
    family = _family()
    missed: dict[str, list[str]] = {}
    for name in sorted(_corpus_patched_names()):
        if not hasattr(memory, name):
            continue
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(memory, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(memory, name) is not sentinel:
                stale.append(FACADE)
        if stale:
            missed[name] = stale
    assert missed == {}
    assert _index().divergent() == ()


def test_the_owners_load_lowest_layer_first() -> None:
    """``_OWNERS`` is the three owners, and each owner imports only owners listed before
    it -- the order the facade loads them and a read resolves in."""
    owners = list(_index().owners)
    assert owners == list(OWNERS)
    late: dict[str, list[str]] = {}
    for position, owner in enumerate(owners):
        tree = ast.parse(_owner_path(owner).read_text(encoding="utf-8"))
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
    """No reader fetches a name of ``memory.py`` through ``importlib.import_module``, so the
    facade forwards no legacy table and keeps the default ``forward_reads``, and every
    served name comes from the index."""
    index = _index()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports)


def test_the_facade_binds_the_routes_it_keeps_for_itself() -> None:
    """These stay in ``memory.py``, bound there and not served from an owner: the
    document, settings and vector-tier routes whose bounded body reads and persistence
    writes path-keyed inventories name by this file, the write gate they share, and the
    graph route, which logs through ``logging.getLogger(__name__)``."""
    kept = {
        "memory_recall_deadline",
        "_memory_write_gate",
        "_store_unavailable_response",
        "_vector_tier_for_request",
        "_validate_private_profile_update",
        "api_memory_preferences",
        "api_memory_projects",
        "api_memory_history",
        "api_memory_settings",
        "api_memory_semantic",
        "api_memory_semantic_write",
        "api_memory_semantic_delete",
        "api_memory_carve",
        "api_memory_events",
        "_migrate_lock",
        "_set_migrated",
        "api_memory_episodic_search",
        "api_memory_episodic_list",
        "api_memory_episodic_delete",
        "api_memory_stats",
        "api_memory_migrate",
        "api_memory_import",
        "api_memory_context_preview",
        "api_memory_consolidate",
        "api_memory_observability",
        "api_memory_promote",
        "api_memory_graph",
    }
    index = _index()
    assert sorted(kept - set(vars(memory))) == []
    assert sorted(kept & set(index.exports)) == []
    for name in kept:
        code = getattr(getattr(memory, name), "__code__", None)
        if code is not None:
            assert Path(code.co_filename).resolve() == Path(memory.__file__).resolve(), name


def test_the_embedding_setup_status_lives_in_one_owner() -> None:
    """``api_memory_enable_embeddings`` rebinds ``_embedding_setup_status`` with
    ``global``, so the routes that read it share its owner and the facade binds no copy:
    a read through the facade is the owner's current binding."""
    embeddings = sys.modules["kiro_crew.dashboard.handlers.memory_embeddings"]
    assert "_embedding_setup_status" not in vars(memory)
    assert [m.__name__ for m in _family() if "_embedding_setup_status" in vars(m)] == [
        embeddings.__name__
    ]
    for route in ("api_memory_embedding_status", "api_memory_enable_embeddings"):
        assert getattr(memory, route).__globals__ is vars(embeddings), route
    assert memory._embedding_setup_status is vars(embeddings)["_embedding_setup_status"]


def test_the_vector_store_constructor_lives_where_its_plumbing_inventory_names_it() -> None:
    """``test_vector_memory_config_plumbing``'s plumbed-site table keys the dashboard's
    standalone store construction by ``dashboard/handlers/memory_services.py``."""
    services = sys.modules[OWNERS[0]]
    assert memory._get_vector_store is vars(services)["_get_vector_store"]
    assert "VectorMemoryStore(" in _owner_path(OWNERS[0]).read_text(encoding="utf-8")
    assert "VectorMemoryStore(" not in Path(memory.__file__).read_text(encoding="utf-8")


def _home_imports() -> dict[str, tuple[str, str]]:
    """Served names an owner imports from a project module outside the family."""
    family = set(_index().owners)
    found: dict[str, tuple[str, str]] = {}
    for owner in _index().owners:
        for node in ast.parse(_owner_path(owner).read_text(encoding="utf-8")).body:
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
    tree = ast.parse(Path(memory.__file__).read_text(encoding="utf-8"))
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
    under ``TYPE_CHECKING`` and ``from kiro_crew.dashboard.handlers.memory import
    model_download_manager`` type-checks."""
    served = set(_index().exports)
    expected = {name: home for name, home in _home_imports().items() if name in served}
    assert expected, "no served name comes from outside the family -- the scan went blind"
    assert _home_manifest() == expected


def test_the_family_logs_under_the_facade_name() -> None:
    """One logger, still named for the facade module, held by every module that logs:
    ``caplog`` filters keyed on ``kiro_crew.dashboard.handlers.memory`` keep seeing the
    embedding setup's warnings."""
    assert memory.logger.name == FACADE
    others = [
        m.__name__ for m in _family() if vars(m).get("logger", memory.logger) is not memory.logger
    ]
    assert others == []


def test_the_facade_runs_as_a_module(tmp_path: Path) -> None:
    """``python -m kiro_crew.dashboard.handlers.memory`` imports cleanly: the mirror
    installs on ``__main__`` too, because every member binds one logger by name."""
    env = dict(os.environ)
    src = str(Path(memory.__file__).resolve().parents[3])
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
    assert source_corpus.repo_root() / "test" / "test_handlers_memory_coverage.py" in walked
