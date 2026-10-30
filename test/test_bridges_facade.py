"""``kiro_crew.apps.bridges`` stays the one import path and patch surface of its owners.

The app registration bridge module was split into sibling owners (``bridges_<role>.py``) behind a
:mod:`kiro_crew._facade_mirror` facade. These tests pin what the split must keep:

* the facade serves a mirror over exactly the ``bridges_*`` modules beside it, and every
  module-level name the unsplit file bound still resolves through it, to the object each
  owner holds;
* every owner logs as ``kiro_crew.apps.bridges``, the logger name log filters and
  ``caplog`` assertions use;
* every name the facade serves from outside the family is named in a ``TYPE_CHECKING``
  group, so ``from kiro_crew.apps.bridges import <name>`` stays visible to type checkers;
* nothing outside the family imports an owner: callers register and deregister an app's resources through the facade.

Each structural check has a self-test that shows it can fail.
"""

from __future__ import annotations

import ast
import importlib
from collections.abc import Iterable
from pathlib import Path

import pytest
import source_corpus

import kiro_crew
from kiro_crew import _facade_mirror
from kiro_crew.apps import bridges as facade

# Walks src/kiro_crew for owner imports; keep it on one worker.
pytestmark = pytest.mark.xdist_group(name="tree_scan_bridges_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("base_names_resolve", "owners_log_as_facade")

_SRC = Path(kiro_crew.__file__).resolve().parent
_FACADE = "kiro_crew.apps.bridges"
_OWNER_PREFIX = "kiro_crew.apps.bridges_"

#: Every module-level name ``apps/bridges.py`` bound before the split.
_BASE_MODULE_NAMES = frozenset("""
    AGENT_MCP_POLICY_FILE Any AppManifest Callable Container Containment ConversationLog
    CronPendingMismatch CronSDK CronStoreBusy CronStoreUnreadable ExitStack GatewayLock
    GatewayLockError Iterable Iterator KIRO_AGENTS_DIR LockFileError Optional Path
    RegistrationResult SKILLS_DIR_NAME SUPPRESS_REPLAY_FLAG SessionMap SessionPointerCleanup
    ZipInventoryRejected _BARE_PYTHON _CREATE_ONLY_CRON_KEYS _CRON_MANIFEST_NAME
    _DEPS_BOOT_PATH _FIELD_DIGEST_CHARS _FRAMEWORK_OWNED_AGENT_KEYS _HOST_CLI_COMMAND
    _LEGACY_SHARED_MCP_PATH _MANIFEST_OWNED_CRON_KEYS _OVERWRITE_LOG_VALUE_CHARS
    _PY_OPTS_WITH_SEPARATE_VALUE _RENDERED_AGENTS_DIRNAME _RESERVED_SKILL_DIRS
    _TEMPLATE_PLACEHOLDER_RE _UPDATE_KWARGS_FOR_KEY _admitted_app_cron_defs
    _agent_mcp_policy _app_crons_path _app_resource_root _apply_agent_mcp_policy
    _apply_agent_prompt _ceiling_filtered_allowed _clip _cron_defs_from_manifest
    _deps_boot_module _deregister_agents _deregister_crons _deregister_mcp_servers
    _deregister_skills _field_digest _global_mcp_specs _has_python_shebang
    _health_reconcile_guard _is_within _join_stamp _kiro_agents_dir _live_port_for
    _managed_mcp_spec _manifest_job_fields _materialize_managed_refs _may_auto_approve
    _maybe_provision_backendless_deps _mcp_json_path _mcp_lock _namespace
    _normalize_attached_m _overwritten_fields _own_mcp_servers _pin_host_cli_command
    _placeholder_values _preserve_user_agent_edits _prune_stale_app_resources
    _py_target_index _python_shebang_interpreter _read_agent_config _read_mcp_json
    _read_mcp_json_unlocked _register_agents _register_crons _register_mcp_servers
    _register_skills _registration_denied _registration_source _render_shipped_agent
    _report_pending_reenable _resolve_and_vet_app_script _resolve_live_mcp_url
    _resync_app_cron _safe_link_name _schedule_unresolvable_warning _scrub_legacy_shared_mcp
    _skills_dir _stamp_digests _stamp_fields _strip_deps_pythonpath
    _strip_ungoverned_auto_approve _targets_gateway_module _unlink_unheld_app_lock
    _unresolvable_tool_refs _warn_unresolvable_stdio_command _warn_unresolved_secret_refs
    _write_mcp_json_unlocked _zip_has_main app_conversation_keys app_cron_manifest_digest
    app_data_dir app_deps_dir app_dir app_execution_denied app_skills_dir asyncio
    atomic_write config_dir contained_in_screened_root contextmanager cron_seams dataclass
    deregister_app deregister_app_crons_from_service deregister_app_crons_reporting_failures
    disarm_app_crons_for_execution discard_app_session_pointers emit_env field get_app
    get_app_manifest has_stdio_mcp_server hashlib is_module_style_entry_point
    is_sensitive_path json kiro_agents_dir kiro_crew_file list_apps load_app_cron_defs
    loads_mcp_config loads_user_json logger logging lookup_cron_folder_id
    maintenance_executor may_skip_gate_now os path_command_is_abi_matched pinned_fs
    platform_compat publish_materialized_agents purge_uninstalled_agent_residue re
    reconcile_app_crons_for_execution reconcile_app_skills reconcile_enabled_app_resources
    refresh_app_agents register_app register_app_crons_with_service
    registered_app_mcp_servers reregister_app_mcp_servers resolve_app_python
    resolve_script_path resolve_stdio_command schedule_materialized_agents_refresh
    screen_app_path scrub_backend_mcp_url sel shipped_builtin_app_root shutil stat
    strip_ungoverned_auto_approve sys urlparse urlunparse venv_provided_command
    vet_zip_inventory without_mcp_refs zipfile
    """.split())


def _index() -> _facade_mirror.MirrorIndex:
    index = vars(facade)[_facade_mirror.INDEX_ATTRIBUTE]
    assert isinstance(index, _facade_mirror.MirrorIndex)
    return index


def _source(module_name: str) -> str:
    path = importlib.import_module(module_name).__file__
    assert path is not None
    return Path(path).read_text(encoding="utf-8")


def test_the_facade_serves_a_mirror_over_every_sibling_owner() -> None:
    assert isinstance(facade, _facade_mirror.MirroredModule)
    index = _index()
    on_disk = sorted(f"kiro_crew.apps.{path.stem}" for path in (_SRC / "apps").glob("bridges_*.py"))
    assert sorted(index.owners) == on_disk
    assert index.excluded == ()
    assert index.divergent() == ()


def _served_imports(index: _facade_mirror.MirrorIndex) -> set[str]:
    defined = set().union(
        *(_facade_mirror._defined_names(ast.parse(_source(o))) for o in index.owners)
    )
    family = {*index.owners, _FACADE}
    return {
        name
        for name in index.exports
        if name not in defined
        and name != "TYPE_CHECKING"
        and getattr(getattr(facade, name), "__name__", None) not in family
    }


def _typing_names(source: str, owners: Iterable[str]) -> set[str]:
    """Names the ``TYPE_CHECKING`` groups of *source* import from outside *owners*."""
    named: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for sub in node.body:
                if isinstance(sub, ast.ImportFrom) and sub.module not in owners:
                    named |= {alias.asname or alias.name for alias in sub.names}
                elif isinstance(sub, ast.Import):
                    named |= {alias.asname or alias.name.split(".")[0] for alias in sub.names}
    return named


def test_every_served_import_is_named_for_type_checkers() -> None:
    """A name the family imports and the facade serves is in a ``TYPE_CHECKING`` group.

    The facade binds none of them, so this group is what keeps
    ``from kiro_crew.apps.bridges import get_app_manifest`` visible to mypy.
    """
    index = _index()
    served = _served_imports(index)
    named = _typing_names(_source(_FACADE), index.owners)
    assert "get_app_manifest" in served, "the census found nothing to check"
    assert sorted(served - named) == []
    assert sorted(named - served) == []


def test_the_type_checker_census_finds_a_dropped_name() -> None:
    index = _index()
    source = _source(_FACADE)
    assert "get_app_manifest" in _typing_names(source, index.owners)
    pruned = source.replace("        get_app_manifest,\n", "", 1)
    assert pruned != source
    assert "get_app_manifest" not in _typing_names(pruned, index.owners)


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


def _owner_importers(sources: Iterable[tuple[Path, str]], root: Path) -> set[str]:
    """The files among *sources* (relative to *root*) whose code imports a family owner."""
    found = set()
    for path, text in sources:
        rel = path.relative_to(root)
        module = ".".join([root.name, *rel.with_suffix("").parts])
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.Import, ast.ImportFrom)) and any(
                name.startswith(_OWNER_PREFIX) for name in _imported_modules(node, module)
            ):
                found.add(rel.as_posix())
    return found


def test_only_the_family_imports_an_owner() -> None:
    family = {f"apps/{name.rpartition('.')[2]}.py" for name in (_FACADE, *_index().owners)}
    sources = source_corpus.iter_candidate_sources(require_all=("bridges_",))
    importers = _owner_importers(
        ((path, text) for path, text in sources if path.is_relative_to(_SRC)), _SRC
    )
    assert "apps/bridges_register.py" in importers  # owners import the owners below them
    assert importers - family == set()


def test_the_owner_import_scan_finds_an_outside_importer(tmp_path: Path) -> None:
    root = tmp_path / "kiro_crew"
    sources = {
        root / "pkg" / "caller.py": "from kiro_crew.apps.bridges_sources import _namespace\n",
        root / "other.py": "from kiro_crew.apps import bridges_sources as low\n",
        root / "apps" / "relative.py": "from .bridges_sources import _namespace\n",
        root / "apps" / "deep" / "up.py": "from .. import bridges_sources\n",
        root / "facade_user.py": "from kiro_crew.apps.bridges import _namespace\n",
    }
    assert _owner_importers(sources.items(), root) == {
        "pkg/caller.py",
        "other.py",
        "apps/relative.py",
        "apps/deep/up.py",
    }
