"""MCP discovery's facade keeps the patch seams the one module had.

``kiro_crew.mcp_discovery`` is a mirror facade (:mod:`kiro_crew._facade_mirror`) over six
sibling owners, and keeps the discovery passes for itself: ``probe_all`` and the
discover-and-sync step. Callers, tests and documents still reach every name as
``kiro_crew.mcp_discovery.<name>``, and the moved code reads its names as plain module
globals, so a patch through the facade reaches that code only because the mirror writes
it into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules. These
tests pin what is particular to this facade: every name the test trees patch through it
reaches every module holding it; the owners load lowest layer first; the passes the facade
keeps stay bound there; ``probe_server`` and the private loop that re-enters it share one
owner; names served from outside the family type-check through the facade; and the family
logs under the facade's name.
"""

from __future__ import annotations

import ast
import functools
import sys
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus

from kiro_crew import _facade_mirror, mcp_discovery

pytestmark = pytest.mark.xdist_group(name="tree_scan_mcp_discovery_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("known_seams", "corpus_gate")

FACADE = "kiro_crew.mcp_discovery"
SRC_DIR = Path(mcp_discovery.__file__).resolve().parent
#: The owners, lowest layer first.
OWNERS = (
    "kiro_crew.mcp_discovery_server_info",
    "kiro_crew.mcp_discovery_probe_state",
    "kiro_crew.mcp_discovery_managed",
    "kiro_crew.mcp_discovery_config",
    "kiro_crew.mcp_discovery_remote",
    "kiro_crew.mcp_discovery_probe",
)
_THIS_FILE = Path(__file__).resolve()

#: Names the test trees are known to patch through the facade, one of each seam the
#: family has: the config sources and their loaders, the cached-verdict lookup, the
#: probe timeout, the remote probe and its grant lookup, the sandboxed spawn, the probe
#: entry and the private loop, the quarantine exclusion, and the sync steps. The scan
#: below must find all of them.
_KNOWN_SEAMS = frozenset(
    {
        "_MCP_JSON_PATHS",
        "_load_agent_config",
        "_load_mcp_json_by_source",
        "_get_probe_timeout",
        "_probe_remote",
        "_runtime_grant_present",
        "sandboxed_spawn_argv",
        "create_subprocess_limited",
        "probe_server",
        "list_servers",
        "_spawn_excluded",
        "discover_servers_to_sync",
        "register_servers_for_cc",
    }
)


def _index() -> _facade_mirror.MirrorIndex:
    return mcp_discovery.__dict__[_facade_mirror.INDEX_ATTRIBUTE]


def _owner_path(owner: str) -> Path:
    return SRC_DIR / f"{owner.rsplit('.', 1)[1]}.py"


def _family() -> list[ModuleType]:
    return [mcp_discovery, *(sys.modules[name] for name in _index().owners)]


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
        if "mcp_discovery" not in text:
            continue
        names = facade_mirror_scan.patched_names(text, {FACADE: FACADE})
        found |= names.get(FACADE, set())
    return frozenset(found)


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    """The reach the unsplit module had: a write to ``mcp_discovery.<name>`` was what every
    reader of that name read. Each module of the family that binds a patched name must
    see the patched object, and the undo must restore all of them."""
    family = _family()
    missed: dict[str, list[str]] = {}
    for name in sorted(_corpus_patched_names()):
        if not hasattr(mcp_discovery, name):
            continue
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(mcp_discovery, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(mcp_discovery, name) is not sentinel:
                stale.append(FACADE)
        if stale:
            missed[name] = stale
    assert missed == {}
    assert _index().divergent() == ()


def test_the_owners_load_lowest_layer_first() -> None:
    """``_OWNERS`` is the six owners, and each owner imports only owners listed before
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
    """``mcp_discovery.py`` was a plain module with no forwarding table, so the facade forwards
    no legacy table and keeps the default ``forward_reads``, and every served name comes
    from the index."""
    index = _index()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports)


def test_the_facade_binds_the_discovery_passes_it_keeps() -> None:
    """These stay in ``mcp_discovery.py``, bound and defined there, not served from an
    owner: ``probe_all`` and the quarantine exclusion it reads, and the discover-and-sync
    step with its divergence checks and the Claude Code sidecar."""
    kept = {
        "_quarantine_warned",
        "_spawn_excluded",
        "PROBE_MAX_CONCURRENCY",
        "probe_all",
        "_note_denied_env",
        "_commands_diverged",
        "_pinned_command_missing",
        "_envs_agree",
        "_names_a_location",
        "_basenames_match",
        "_DEFAULT_PATHEXT",
        "_win_exec_suffixes",
        "_is_abs_any",
        "_basename_any",
        "_declared_source_owned",
        "_source_owned_diverged",
        "discover_servers_to_sync",
        "sync_to_agent_config",
        "_SYNC_MUTEX",
        "sync_discovered_servers",
        "kirocrew_managed_names",
        "register_servers_for_cc",
    }
    index = _index()
    assert sorted(kept - set(vars(mcp_discovery))) == []
    assert sorted(kept & set(index.exports)) == []
    for name in kept:
        code = getattr(getattr(mcp_discovery, name), "__code__", None)
        if code is not None:
            assert Path(code.co_filename).resolve() == Path(mcp_discovery.__file__).resolve(), name


def test_probe_server_and_the_private_loop_that_re_enters_it_share_one_owner() -> None:
    """``probe_server`` hands a local probe to ``_probe_on_private_loop``, which calls
    ``probe_server`` again on its own loop. Owners import in one direction only, so the
    two must be defined in the same owner, and the facade binds neither."""
    probe = sys.modules["kiro_crew.mcp_discovery_probe"]
    for name in ("probe_server", "_probe_on_private_loop"):
        assert getattr(mcp_discovery, name).__globals__ is vars(probe), name
    assert "_probe_on_private_loop" not in vars(mcp_discovery)


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
    tree = ast.parse(Path(mcp_discovery.__file__).read_text(encoding="utf-8"))
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
    under ``TYPE_CHECKING`` and ``from kiro_crew.mcp_discovery import safe_read_file``
    type-checks as it did before the split."""
    served = set(_index().exports)
    expected = {name: home for name, home in _home_imports().items() if name in served}
    assert expected, "no served name comes from outside the family -- the scan went blind"
    assert _home_manifest() == expected


def test_the_family_logs_under_the_facade_name() -> None:
    """One logger, still named for the facade module, held by every module that logs:
    ``caplog`` filters and levels keyed on ``kiro_crew.mcp_discovery`` keep seeing the
    probes' and the config readers' records."""
    assert mcp_discovery.logger.name == FACADE
    loggers = {m.__name__: vars(m)["logger"] for m in _family() if "logger" in vars(m)}
    assert {FACADE, "kiro_crew.mcp_discovery_probe"} <= set(loggers)
    others = sorted(name for name, value in loggers.items() if value is not mcp_discovery.logger)
    assert others == []


def test_the_test_tree_walk_reaches_this_file() -> None:
    walked = {path.resolve() for path in _test_tree_files()}
    assert _THIS_FILE in walked
    assert source_corpus.repo_root() / "test" / "test_mcp_discovery.py" in walked
