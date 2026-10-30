"""``kiro_crew.apps.manager`` stays the one import path and patch surface of its owners.

The app manager module was split into sibling owners (``manager_<role>.py``) behind a
:mod:`kiro_crew._facade_mirror` facade. These tests pin what the split must keep:

* the facade serves a mirror over exactly the ``manager_*`` modules beside it, and every
  module-level name the unsplit file bound still resolves through it, to the object each
  owner holds;
* every owner logs as ``kiro_crew.apps.manager``, the logger name log filters and
  ``caplog`` assertions use;
* every name the facade serves from outside the family is named in a ``TYPE_CHECKING``
  group, so ``from kiro_crew.apps.manager import <name>`` stays visible to type checkers;
* nothing outside the family imports an owner: callers install, update, uninstall, enable, disable and list apps through the facade.
* ``InstalledTreeRefused`` keeps the facade's ``__module__``: it is an exception, and its
  dotted name is what a traceback or a log line shows.

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
from kiro_crew.apps import manager as facade

# Walks src/kiro_crew for owner imports; keep it on one worker.
pytestmark = pytest.mark.xdist_group(name="tree_scan_manager_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("base_names_resolve", "owners_log_as_facade")

_SRC = Path(kiro_crew.__file__).resolve().parent
_FACADE = "kiro_crew.apps.manager"
_OWNER_PREFIX = "kiro_crew.apps.manager_"

#: Every module-level name ``apps/manager.py`` bound before the split.
_BASE_MODULE_NAMES = frozenset("""
    APPROVED_GRANTS_FILENAME APP_MANIFEST_FILENAME AgentOwner Any AppManifest AppResult
    Callable ConfigReadError ContextVar INSTALLED_META_FILENAME InstalledApp
    InstalledTreeRefused Iterator LoopBoundLock OrderedDict Path RESERVED_APP_NAME_CODE
    STAGED_GRANT_FAMILIES Sequence TASK_RUNNER_APP TASK_RUNNER_DISABLED_MESSAGE
    _BUILTIN_APPS _COPY_IGNORE _DEFAULT_ON_BACKFILL _DEFAULT_ON_BUILTINS
    _DEPS_STAGING_SWEEP_RE _LIFECYCLE_LOCKS _REGISTRY_SOURCE_REPOSITORY
    _REQUIRED_BUILTIN_FIELDS _SwapState _TREE_SWAP_LAST_CAP _UpdateMarker _VALID_LIFECYCLE
    _VALID_ORIGIN _VALID_RESOURCES _absence_is_genuine _app_activation_denied
    _app_agent_names _app_agent_registration _app_declares_backend _app_display_name
    _app_registered_agent _approved_grants_after_manifest_change _approved_grants_path
    _approved_grants_row _audit_default_on_backfill _builtin_owns_install _check_min_version
    _check_path_safety _copy_app_tree _credential_free_source_metadata
    _declared_grant_entries _dirfd_ops_supported _drop_trust_grant _edition_builtin_apps
    _edition_bundled_app_names _effective_approved_grants _effective_migrated_to
    _effective_source_repository _has_trust_grant _held_back_entries
    _is_generated_deps_artifact_name _now_iso _orphaned_builtins_cache _owned_data_dir
    _owner_json _parse_approved_grants _pending_session_approval_after_manifest_change
    _read_approved_grants _read_installed _remove_any_shape _reserved_name_code
    _restore_trust_grant _rmtree_dirfd _screened_spec_path
    _temp_data_name_obstruction _tree_swap _tree_swap_guard _tree_swap_last _tree_swap_mark
    _tree_swap_seq _tree_swap_spanned _tree_swap_state _tree_swaps_active _trust_grant_local
    _trust_grant_repository _update_in_progress _update_marker_dir
    _update_retired_tree_prefix _validate_builtin_app _validate_source_path
    _write_approved_grants _write_installed app_admission_denied app_agent_backend_refusal
    app_command_agent_refusal app_data_dir app_dir app_disabled app_enabled_state
    app_execution_denied app_lifecycle_lock app_name_error approved_manifest_permissions
    apps_dir asdict atomic_write backfill_default_on_builtins builtin_owns_installed
    cleanup_migrated_builtin config_dir config_local_path config_path contain_command_agents
    contextlib contextmanager copy_app_tree_as_installed cron_permission_error
    current_context dataclass detect_orphaned_builtins disable_app discover_builtin_apps
    enable_app ensure_directory functools gateway_data_dir_obstruction get_app
    get_app_manifest install_app invalidate_orphan_cache ipaddress is_app_enabled
    is_link_or_junction is_reserved_app_name json list_apps logger logging
    migrated_builtin_cleanup_applies missing_agent_reason os owning_app_for_agent
    platform_compat preserved_data_awaits re read_config_text register_builtin_apps
    register_external_app registry_source_repository replace repository_bound_grant_denied
    resolve_mcp_backend_url safe_context_call sel set_app_provenance set_app_source
    shipped_builtin_app_root shipped_builtin_names shutil staged_app_grants stat
    supports_pinned_walk threading time trust_grant_removal_blocked uninstall_app update_app
    update_config_locked urlparse
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
    on_disk = sorted(f"kiro_crew.apps.{path.stem}" for path in (_SRC / "apps").glob("manager_*.py"))
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
    ``from kiro_crew.apps.manager import config_dir`` visible to mypy.
    """
    index = _index()
    served = _served_imports(index)
    named = _typing_names(_source(_FACADE), index.owners)
    assert "config_dir" in served, "the census found nothing to check"
    assert sorted(served - named) == []
    assert sorted(named - served) == []


def test_the_type_checker_census_finds_a_dropped_name() -> None:
    index = _index()
    source = _source(_FACADE)
    assert "config_dir" in _typing_names(source, index.owners)
    pruned = source.replace("        config_dir,\n", "", 1)
    assert pruned != source
    assert "config_dir" not in _typing_names(pruned, index.owners)


def test_the_refusal_exception_keeps_the_facade_module() -> None:
    refused = facade.InstalledTreeRefused
    assert refused.__module__ == _FACADE
    assert refused.__qualname__ == "InstalledTreeRefused"
    assert getattr(importlib.import_module(refused.__module__), refused.__qualname__) is refused
    assert (
        f"{refused.__module__}.{refused.__qualname__}"
        == "kiro_crew.apps.manager.InstalledTreeRefused"
    )


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
    sources = source_corpus.iter_candidate_sources(require_all=("manager_",))
    importers = _owner_importers(
        ((path, text) for path, text in sources if path.is_relative_to(_SRC)), _SRC
    )
    assert "apps/manager_lookup.py" in importers  # owners import the owners below them
    assert importers - family == set()


def test_the_owner_import_scan_finds_an_outside_importer(tmp_path: Path) -> None:
    root = tmp_path / "kiro_crew"
    sources = {
        root / "pkg" / "caller.py": "from kiro_crew.apps.manager_records import app_dir\n",
        root / "other.py": "from kiro_crew.apps import manager_records as low\n",
        root / "apps" / "relative.py": "from .manager_records import app_dir\n",
        root / "apps" / "deep" / "up.py": "from .. import manager_records\n",
        root / "facade_user.py": "from kiro_crew.apps.manager import app_dir\n",
    }
    assert _owner_importers(sources.items(), root) == {
        "pkg/caller.py",
        "other.py",
        "apps/relative.py",
        "apps/deep/up.py",
    }
