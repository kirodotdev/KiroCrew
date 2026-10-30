"""``kiro_crew.dashboard.handlers.mcp`` stays the one import path and patch surface of its owners.

The MCP server management module was split into sibling owners
(``kiro_crew.dashboard.handlers.mcp_<role>``) behind a :mod:`kiro_crew._facade_mirror`
facade. Callers, tests and documents still reach every name as
``kiro_crew.dashboard.handlers.mcp.<name>``, and the moved code reads its names as plain
module globals, so a patch through the facade reaches that code only because the mirror
writes it into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules (one
object per name, the manifest, owners never importing their facade, no shared global
rebound). These tests pin what is particular to this facade:

* every name the test trees patch through it reaches every module holding it;
* the owners are exactly the ``mcp_<role>.py`` siblings the split made, loaded lowest layer
  first, and every module-level name the unsplit file bound still resolves through the
  facade, to the object each owner holds or to the facade's own code;
* the mcp.json file lock whose blocking open the sync-io ``fs`` baseline counts against
  ``handlers/mcp.py``, and the routes that take it, are still defined here;
* every import the facade binds is accounted for, since flake8's F401 is off for its
  import header;
* every name the facade serves is named in a ``TYPE_CHECKING`` group, so
  ``from kiro_crew.dashboard.handlers.mcp import <name>`` stays visible to type checkers;
* the family logs under the facade's name, the moved exception classes keep the facade's
  module, and the facade still runs as ``python -m``;
* nothing outside the family imports an owner.

Each structural check has a self-test that shows it can fail.
"""

from __future__ import annotations

import ast
import functools
import importlib
import logging
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
from kiro_crew.dashboard.handlers import mcp
from kiro_crew.subprocess_utf8 import UTF8_TEXT

# Walks the test trees and src/kiro_crew; keep it on one worker.
pytestmark = pytest.mark.xdist_group(name="tree_scan_mcp_handler_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("known_seams", "corpus_gate")

FACADE = "kiro_crew.dashboard.handlers.mcp"
HANDLERS_DIR = Path(mcp.__file__).resolve().parent
_SRC = Path(kiro_crew.__file__).resolve().parent
_THIS_FILE = Path(__file__).resolve()

#: The ``mcp_*.py`` handler modules that are route modules of their own, not owners.
_NOT_OWNERS = frozenset({"mcp_apps", "mcp_custom", "mcp_discover"})

#: Every module-level name ``handlers/mcp.py`` bound before the split, except the names
#: bound by ``import <module>`` of a standard-library module.
_BASE_MODULE_NAMES = frozenset("""
    ABSENT AGENT_FILENAME AGENT_SPEC_NOT_REWRITTEN AmbiguousServerKey Any Collection
    DashboardState FORWARD_DECLARED_ENV_DEFAULT INTERNAL_CLIENT_ID_KEY INTERNAL_SCOPES_KEY
    KiroCrewConfig LoopBoundLock MANAGED_STUB_FOLLOWERS MCP_CONFIG_HAS_COMMENTS Mapping
    McpConfigHasComments McpServerInfo Path PreflightKeys SCOPE_KIROCREW SCOPE_KIRO_GLOBAL
    ShareEvidence ShareVerdict SharedAgentHomeRefused SimpleNamespace TYPE_CHECKING
    WITHHELD_KEY _GLOBAL_MCP_JSON _KIROCREW_MCP_JSON _KIRO_GLOBAL_SURFACE _MAX_MCP_NAME_LEN
    _MAX_STUB_BATCH _MCP_APPLY_MAX_BODY_BYTES _MCP_APPLY_MAX_CHANGES
    _MCP_DEFERRED_UNINSTALL_CONCURRENCY _MCP_DEFERRED_UNINSTALL_MAX_BUDGET
    _MCP_GATEWAY_APPLY_LOCK _MCP_LOCK_PATH _MCP_PROBE_CACHE_SECS _MCP_RESOLVE_REFRESH_LOCK
    _McpFileLock _McpFileLockSync _VALID_MCP_NAME_RE _agent_home_not_owned
    _agent_home_not_owned_response _agent_mcp_server_names _annotate_owner_sign_in
    _annotate_quarantine _apply_lock _arm_reprobe _assess_server _atomic_json_write
    _atomic_write _bg_mcp_probe _bg_measure_all _collect_server_rows _config_entry_for
    _config_key_for _declined_foreign_spec_write _do_mcp_apply _edited_since_probe
    _evaluate_shareability _extra_mcp_scopes _find_server_spec_anywhere
    _forget_owner_servers _freeze_stub_servers _get_apply_lock _get_kirocrew_entry
    _get_mcp_lock _get_mcp_lock_sync _has_json_comments_on_disk _invalid_flag_reason
    _is_valid_mcp_name _kirocrew_mcp_json _kirocrew_mcp_servers _launch_specs_for
    _load_json_or_empty _load_shareability_state _local_overlay_section
    _mark_agent_entries_disabled _mcp_hot_reload_active _mcp_probe_cache
    _mcp_probe_in_progress _mcp_probe_task _mcp_probe_ts _mcp_reprobe_armed_for
    _measure_progress _offload_config_write _overlay_shadowed_keys _preflight_change_keys
    _probe_currency _purge_server_config _quarantine_verdicts _read_agent_spec
    _record_probe_verdicts _record_stub_decisions _refuse_commented_config
    _remove_from_agent_file _remove_kirocrew_entry _replace_kirocrew_spec
    _resolve_key_from_read _resolve_stub_overrides _resolve_stub_roster _rmw_remove_entry
    _run_mcp_probe _scope_has_entry _scrub_preregistered_oauth_copies _set_kirocrew_entry
    _set_scope_entry _set_tool_overrides _shared_agent_home_declined _stamp_config_state
    _string_identifier _stub_eligibility _sweep_dangling_uninstalls _sync_mcp_to_agent
    _sync_mcp_to_agent_batch _sync_mcp_to_agent_batch_unlocked _sync_mcp_to_agent_unlocked
    _uninstall_scope_files _user_mcp_config_paths _write_mcp_json api_mcp_active
    api_mcp_apply api_mcp_gateway_enable api_mcp_gateway_metrics
    api_mcp_gateway_server_launch api_mcp_gateway_servers api_mcp_gateway_set_stub
    api_mcp_gateway_status api_mcp_global_scopes api_mcp_measure_progress
    api_mcp_measure_start api_mcp_probe api_mcp_probe_cached api_mcp_quarantine_clear
    api_mcp_remove api_mcp_resolve_refresh api_mcp_server_detail api_mcp_servers
    api_mcp_sync api_mcp_toggle api_mcp_toggle_all api_mcp_toggle_tool
    apply_kiro_oauth_hints approve assess atomic_write cached_probe_is_current data_home
    display_launches emit_env entry_as_enabled has_json_comments hash_command hazards
    is_gateway_supported iter_agent_spec_files kiro_agents_dir kiro_agents_dir_path
    launch_resolve live_sessions_hot_reload load_approvals load_cache loads_mcp_config
    loads_user_json loads_user_jsonc logger managed_server_is_session_bound
    may_skip_gate_now mcp_entry_is_muted mcp_quarantine mcp_server_alias partial
    platform_compat probe_metadata process_shutdown_requested read_bounded_json
    read_config_text rebuild_agent_config_reporting records_dir redact_credentials
    redact_exfiltration_urls redact_mcp_error redact_mcp_headers refused_servers
    resolve_write restore revoke run_to_completion sel stamp target_stem web
    without_mcp_refs
    """.split())

#: Names the test trees are known to patch through the facade, one of each seam: paths
#: the owners read, the lock and writer the facade keeps, owner functions several modules
#: call, names the family only imports, and the probe globals one owner rebinds. The scan
#: below must find all of them, so a scanner that stops matching goes red instead of
#: passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "_GLOBAL_MCP_JSON",
        "_KIROCREW_MCP_JSON",
        "_MCP_LOCK_PATH",
        "_assess_server",
        "_extra_mcp_scopes",
        "_find_server_spec_anywhere",
        "_get_mcp_lock",
        "_mcp_probe_in_progress",
        "_mcp_probe_ts",
        "_sync_mcp_to_agent",
        "_write_mcp_json",
        "is_gateway_supported",
        "rebuild_agent_config_reporting",
        "sel",
    }
)

#: What ``.github/sync-io-in-async-fs-baseline.txt`` pins to ``handlers/mcp.py``: the
#: async lock whose ``__aenter__`` opens the lock file on the loop, and every unit that
#: takes that lock, with the apply route the lock-taking ``_do_mcp_apply`` serves.
_KEPT_HERE = frozenset("""
    _McpFileLock _get_mcp_lock api_mcp_sync api_mcp_toggle api_mcp_toggle_tool
    api_mcp_toggle_all api_mcp_remove api_mcp_server_detail api_mcp_apply _do_mcp_apply
    """.split())

#: The moved exception classes: tracebacks and logs name them by the facade's path.
_FACADE_MODULE_CLASSES = ("AmbiguousServerKey", "McpConfigHasComments")

#: Names the facade's own glue imports.
_GLUE = frozenset({"annotations", "_importlib", "_sys", "TYPE_CHECKING", "_facade_mirror"})


def _index() -> _facade_mirror.MirrorIndex:
    index = vars(mcp)[_facade_mirror.INDEX_ATTRIBUTE]
    assert isinstance(index, _facade_mirror.MirrorIndex)
    return index


def _source(module_name: str) -> str:
    path = importlib.import_module(module_name).__file__
    assert path is not None
    return Path(path).read_text(encoding="utf-8")


def _family() -> list[ModuleType]:
    return [mcp, *(sys.modules[name] for name in _index().owners)]


# --------------------------------------------------------------------------- #
# Patch reach
# --------------------------------------------------------------------------- #


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
        if "handlers" not in text or "mcp" not in text:
            continue
        names = facade_mirror_scan.patched_names(text, {FACADE: FACADE})
        found |= names.get(FACADE, set())
    return frozenset(found)


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    """The reach the unsplit module had: a write to ``mcp.<name>`` was what every reader of
    that name read. Each module of the family that binds a patched name must see the
    patched object, and the undo must restore all of them."""
    family = _family()
    missed: dict[str, list[str]] = {}
    for name in sorted(_corpus_patched_names()):
        if not hasattr(mcp, name):
            continue
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        before = {module.__name__: vars(module)[name] for module in holders}
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(mcp, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(mcp, name) is not sentinel:
                stale.append(FACADE)
        stale += [m.__name__ for m in holders if vars(m).get(name) is not before[m.__name__]]
        if stale:
            missed[name] = stale
    assert missed == {}
    assert _index().divergent() == ()


# --------------------------------------------------------------------------- #
# Owners and names
# --------------------------------------------------------------------------- #


def test_the_owners_are_the_split_siblings_in_layer_order() -> None:
    """``_OWNERS`` names every ``mcp_<role>.py`` sibling the split made, and each owner
    imports only owners listed before it -- the order the facade loads them in."""
    owners = list(_index().owners)
    assert owners == list(mcp._OWNERS)
    siblings = {
        f"kiro_crew.dashboard.handlers.{path.stem}"
        for path in HANDLERS_DIR.glob("mcp_*.py")
        if path.stem not in _NOT_OWNERS
    }
    assert sorted(owners) == sorted(siblings)
    late: dict[str, list[str]] = {}
    for position, owner in enumerate(owners):
        tree = ast.parse(_source(owner))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        later = sorted(m for m in imported if m in owners and owners.index(m) >= position)
        if later:
            late[owner] = later
    assert late == {}


def test_every_base_module_name_resolves_through_the_facade() -> None:
    index = _index()
    missing = sorted(name for name in _BASE_MODULE_NAMES if not hasattr(mcp, name))
    assert missing == []
    own = vars(mcp)
    for name in sorted(_BASE_MODULE_NAMES):
        holders = index.holders.get(name, ())
        if not holders:
            # only the facade binds it: one of the units kept here, or its own glue
            assert name in own, f"{name} is neither an owner's name nor the facade's own"
            continue
        value = getattr(mcp, name)
        assert all(
            getattr(importlib.import_module(holder), name) is value for holder in holders
        ), name
        assert own.get(name, value) is value, name


def test_the_handlers_package_registers_the_objects_the_facade_serves() -> None:
    """The route table reads every MCP route as ``handlers.<name>``; the package's re-exports
    are the very objects the facade answers with, wherever the route now lives."""
    package = importlib.import_module("kiro_crew.dashboard.handlers")
    routes = sorted(
        name for name in dir(package) if name.startswith("api_mcp_") and hasattr(mcp, name)
    )
    assert len(routes) >= 20, routes
    assert {name: getattr(package, name) for name in routes} == {
        name: getattr(mcp, name) for name in routes
    }


def test_the_lock_and_the_routes_that_take_it_stay_in_the_facade() -> None:
    tree = ast.parse(_source(FACADE))
    defined = {
        n.name
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    assert sorted(_KEPT_HERE - defined) == []
    assert all(getattr(mcp, name).__module__ == FACADE for name in _KEPT_HERE)


def _unaccounted_imports(facade_source: str, owner_sources: Iterable[str]) -> list[str]:
    """Top-level imports of *facade_source* no rule accounts for.

    An import is accounted for when it is a name the unsplit module bound, the facade's own
    glue, a name an owner defines, or a name the family's own code reads.
    """
    trees = [ast.parse(text) for text in owner_sources]
    owner_defined = set().union(*(_facade_mirror._defined_names(t) for t in trees))
    facade_tree = ast.parse(facade_source)
    read = {
        node.id
        for tree in (facade_tree, *trees)
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    bound = [
        alias.asname or alias.name.split(".")[0]
        for node in facade_tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    ]
    return sorted(
        name
        for name in bound
        if name not in _BASE_MODULE_NAMES
        and name not in _GLUE
        and name not in owner_defined
        and name not in read
    )


def test_every_import_the_facade_binds_is_accounted_for() -> None:
    owners = [_source(owner) for owner in _index().owners]
    assert _unaccounted_imports(_source(FACADE), owners) == []


def test_the_import_census_finds_a_planted_import() -> None:
    owners = [_source(owner) for owner in _index().owners]
    head = "from __future__ import annotations\n"
    planted = _source(FACADE).replace(head, head + "\nimport json as _s52_planted\n", 1)
    assert planted != _source(FACADE)
    assert _unaccounted_imports(planted, owners) == ["_s52_planted"]


def _typing_names(source: str) -> set[str]:
    """Names the ``TYPE_CHECKING`` groups of *source* import."""
    named: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for sub in node.body:
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    named |= {alias.asname or alias.name.split(".")[0] for alias in sub.names}
    return named


def test_every_served_name_is_named_for_type_checkers() -> None:
    """A name the facade serves but does not bind is in a ``TYPE_CHECKING`` group.

    The facade binds none of them, so this group is what keeps
    ``from kiro_crew.dashboard.handlers.mcp import api_mcp_servers`` visible to mypy.
    """
    served = set(_index().exports)
    named = _typing_names(_source(FACADE))
    assert "api_mcp_servers" in served, "the census found nothing to check"
    assert sorted(served - named) == []


def test_the_type_checker_census_finds_a_dropped_name() -> None:
    source = _source(FACADE)
    assert "api_mcp_servers" in _typing_names(source)
    pruned = source.replace("        api_mcp_servers,\n", "", 1)
    assert pruned != source
    assert "api_mcp_servers" not in _typing_names(pruned)


# --------------------------------------------------------------------------- #
# Logger, exception classes, ``python -m``
# --------------------------------------------------------------------------- #


def test_the_family_logs_under_the_facade_name() -> None:
    """One logger, still named for the facade module, held by every module that logs:
    log filters and ``caplog`` assertions name ``kiro_crew.dashboard.handlers.mcp``."""
    shared = logging.getLogger(FACADE)
    assert mcp.logger is shared
    others = [m.__name__ for m in _family() if vars(m).get("logger", shared) is not shared]
    assert others == []


@pytest.mark.parametrize("name", _FACADE_MODULE_CLASSES)
def test_the_moved_exception_keeps_the_facade_module(name: str) -> None:
    cls = getattr(mcp, name)
    assert cls.__module__ == FACADE
    assert getattr(importlib.import_module(cls.__module__), cls.__qualname__) is cls
    holders = _index().holders.get(name, ())
    assert holders and all(getattr(sys.modules[h], name) is cls for h in holders)


def test_the_facade_runs_as_a_module(tmp_path: Path) -> None:
    """``python -m kiro_crew.dashboard.handlers.mcp`` imported cleanly before the split; the
    mirror installs on ``__main__`` too, because every member binds one logger by name."""
    env = dict(os.environ)
    src = str(Path(mcp.__file__).resolve().parents[3])
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


# --------------------------------------------------------------------------- #
# Who imports an owner
# --------------------------------------------------------------------------- #


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
    wanted = frozenset(owners)
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
    owners = _index().owners
    family = {f"dashboard/handlers/{name.rpartition('.')[2]}.py" for name in (FACADE, *owners)}
    leaves = tuple(name.rpartition(".")[2] for name in owners)
    sources = source_corpus.iter_candidate_sources(require_any=leaves)
    importers = _owner_importers(
        ((path, text) for path, text in sources if path.is_relative_to(_SRC)), _SRC, owners
    )
    assert "dashboard/handlers/mcp_probe.py" in importers  # owners import the owners below them
    assert importers - family == set()


def test_the_owner_import_scan_finds_an_outside_importer(tmp_path: Path) -> None:
    root = tmp_path / "kiro_crew"
    owner = "kiro_crew.dashboard.handlers.mcp_config_files"
    sources = {
        root / "pkg" / "caller.py": f"from {owner} import _kirocrew_mcp_json\n",
        root / "other.py": "from kiro_crew.dashboard.handlers import mcp_config_files as low\n",
        root / "dashboard" / "handlers" / "relative.py": "from .mcp_config_files import x\n",
        root / "dashboard" / "handlers" / "deep" / "up.py": "from .. import mcp_config_files\n",
        root / "facade_user.py": "from kiro_crew.dashboard.handlers.mcp import x\n",
        root / "sibling_user.py": "from kiro_crew.dashboard.handlers.mcp_custom import x\n",
    }
    assert _owner_importers(sources.items(), root, [owner]) == {
        "pkg/caller.py",
        "other.py",
        "dashboard/handlers/relative.py",
        "dashboard/handlers/deep/up.py",
    }


# --------------------------------------------------------------------------- #
# The tree walk
# --------------------------------------------------------------------------- #


def test_the_test_tree_walk_reaches_this_file() -> None:
    walked = {path.resolve() for path in _test_tree_files()}
    assert _THIS_FILE in walked
    assert source_corpus.repo_root() / "test" / "test_mcp_apply.py" in walked
