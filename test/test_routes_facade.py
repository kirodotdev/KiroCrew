"""``kiro_crew.apps.routes`` stays the one import path and patch surface of its owners.

The app management route module was split into sibling owners (``routes_<role>.py``) behind a
:mod:`kiro_crew._facade_mirror` facade. These tests pin what the split must keep:

* the facade serves a mirror over exactly the ``routes_*`` modules beside it, and every
  module-level name the unsplit file bound still resolves through it, to the object each
  owner holds, or to the facade's own code;
* the handlers whose blocking file-system calls the sync-io ``fs`` baseline counts against
  ``apps/routes.py``, and the units that must stay beside them, are still defined here;
* every import the facade binds is accounted for, since flake8's F401 is off for its
  verbatim import header;
* every owner logs as ``kiro_crew.apps.routes``, the logger name log filters and
  ``caplog`` assertions use;
* every name the facade serves is named in a ``TYPE_CHECKING`` group, so
  ``from kiro_crew.apps.routes import <name>`` stays visible to type checkers;
* nothing outside the family imports an owner: callers reach every app handler through the facade.

Each structural check has a self-test that shows it can fail.
"""

from __future__ import annotations

import ast
import importlib
import logging
from collections.abc import Iterable
from pathlib import Path

import pytest
import source_corpus

import kiro_crew
from kiro_crew import _facade_mirror
from kiro_crew.apps import routes as facade

# Walks src/kiro_crew for owner imports; keep it on one worker.
pytestmark = pytest.mark.xdist_group(name="tree_scan_routes_facade")

_SRC = Path(kiro_crew.__file__).resolve().parent
_FACADE = "kiro_crew.apps.routes"
_OWNER_PREFIX = "kiro_crew.apps.routes_"

#: Every module-level name ``apps/routes.py`` bound before the split.
_BASE_MODULE_NAMES = frozenset("""
    Any BUILTIN_NAMES ConfigReadError CronStoreBusy CronStoreUnreadable
    DEPLOY_WEB_PROVIDER_ID Dependencies KiroCrewConfig PRINCIPAL_AGENT_TOOL
    PRINCIPAL_APP_TOKEN PRINCIPAL_NONE PRINCIPAL_OWNER_SESSION PROXY_PRINCIPAL_HEADER Path
    PinnedPathRefusal PlatformConfig RegistrationResult _ALLOWED_EXTENSIONS
    _ART_IMAGE_EXTENSIONS _ART_MANIFEST_FIELDS _ART_MANIFEST_LIST_FIELDS _ART_MAX_BYTES
    _BLOB_FETCH_SEMAPHORE _BLOB_FETCH_TIMEOUT _BUILTIN_SERVICE_APPS _CONTENT_TYPES
    _CRON_CLEANUP_ATTEMPTS _CRON_CLEANUP_BACKOFF_SECS _HOOKS_NEUTRALIZER_ARGV
    _OPEN_COMMAND_DESKTOP_ENV_KEYS _PROXY_HOP_HEADERS _PROXY_IDLE_TIMEOUT
    _PROXY_STRIP_HEADERS _PROXY_TIMEOUT _REGISTRY_TRUST_TIERS _SAFE_HTTPS_URL_RE
    _SAFE_PATH_RE _SAFE_REF_RE _SAFE_REPO_RE _SAFE_SCP_URL_RE _SAFE_SSH_URL_RE _TRUST_INDEX
    _TRUST_OWNER _UI_MAX_BYTES _UI_STREAM_CHUNK _UI_STREAM_SEMAPHORE
    _UI_STREAM_TIMEOUT_DEFAULT _app_may_run_after_install _app_secret_cache _blob_cache_dir
    _blob_cache_key _check_min_version _check_min_version_str _client_install_manifest
    _context_clone_sandbox_mode _credential_free_source_metadata _declared_art_paths
    _deregister_app_off_loop _deregister_crons_with_retry _derive_registry_name
    _end_relay_midbody _entry_git_url _fetch_git_blob _get_app_secret _git_fetch_branch
    _git_target_is_unsupported _git_url_host _hmac _is_event_stream _is_safe_repo_identifier
    _listed_apps_with_trust _loggable_git_transport_output _notify_builtin_service
    _open_ui_file _owner_designated_repo_target _pinned_registries _provider_is_configured
    _proxy_principal _read_declared_art _redact_warning _refuse_while_startup_hook_runs
    _register_app_off_loop _registry_identity_key _registry_trust_tier_of
    _repo_key_owner_count _resolve_app_backend_url _resolve_deps
    _restore_app_after_failed_update _resync_app_crons_after_update _run_lifecycle_script
    _run_uninstall _same_git_target _sel_credential_grant _stamp_installed_trust_repository
    _start_backend_after_install _stop_backend_and_observe _strip_git_target_userinfo
    _suspend_app_for_session_approval_reconsent _sync_builtin_config _touch_served_blob
    _ui_stream_timeout_secs _unregister_notification_channels _write_registries_config
    aiohttp anonymous_git_env app_conversation_keys app_enabled_state app_endpoint_allowed
    app_execution_denied app_lifecycle_lock apps_dir asyncio build_broadcast_fn
    build_spawn_impl canonical_dep_key cgroup_scope_argv classify_and_clean_for_uninstall
    classify_for_uninstall clean_dependencies collect_publish_providers conditional_response
    config_path create_subprocess_limited declared_capability_keys deregister_app
    deregister_app_crons_reporting_failures dev_mode_granted_root disable_app enable_app
    fd_real_path forget_app_hooks forget_category_order_cache forget_editorial_cache
    formatdate functools get_all_hook_health get_app get_app_backend_port get_app_manifest
    get_registry_app_by_repo get_server_platform handle_app_api_proxy handle_app_art_file
    handle_app_config handle_app_dev_mode handle_app_ui_file handle_blob_proxy
    handle_disable_app handle_enable_app handle_get_app handle_get_manifest
    handle_install_app handle_list_apps handle_migrate_cleanup handle_open_app
    handle_publish_providers handle_register_external handle_registries
    handle_registries_refresh handle_registry handle_registry_install
    handle_registry_install_stream handle_registry_refresh handle_uninstall_app
    handle_uninstall_preview handle_update_app hashlib importlib install_app
    install_from_registry invalidate_app_secret_cache is_app_enabled is_dev_mode_cached
    is_not_modified is_registry_source is_reparse_point json known_registry_repos
    list_app_processes list_apps list_catalog_apps list_registry load_category_order
    load_sections logger logging mimetypes minimal_env official_catalog on_app_enable
    open_in_pinned_parent os partial platform_compat publish_denied_reason re
    recorded_backend_port register_app register_app_crons_with_service register_app_routes
    register_external_app registry_name_from_source resolve_installed_trust_repository
    resolve_mcp_backend_url scrub_env sel sign_proxy_principal_claim start_app_backend stat
    stop_app_backend stop_retained_startup_hooks subprocess_executor supports_pinned_walk
    sys teardown_app_runtime time trust_grant_removal_blocked uninstall_app
    unstopped_backend_port update_app update_config_locked web wrap_argv wrap_argv_async
    yarl
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
    on_disk = sorted(f"kiro_crew.apps.{path.stem}" for path in (_SRC / "apps").glob("routes_*.py"))
    assert sorted(index.owners) == on_disk
    assert index.excluded == ()
    assert index.divergent() == ()


def test_every_base_module_name_resolves_through_the_facade() -> None:
    index = _index()
    missing = sorted(name for name in _BASE_MODULE_NAMES if not hasattr(facade, name))
    assert missing == []
    own = vars(facade)
    for name in sorted(_BASE_MODULE_NAMES):
        holders = index.holders.get(name, ())
        if not holders:
            # only the facade binds it: one of the handlers kept here, or its own glue
            assert name in own, f"{name} is neither an owner's name nor the facade's own"
            continue
        value = getattr(facade, name)
        assert all(
            getattr(importlib.import_module(holder), name) is value for holder in holders
        ), name
        assert own.get(name, value) is value, name


#: The async defs whose blocking file-system calls ``.github/sync-io-in-async-fs-baseline.txt``
#: counts against ``apps/routes.py``, and the units that must stay beside them: the callers of
#: ``_run_uninstall``, the art route that shares the blob proxy's extension screen, and the
#: route table that names them all.
_KEPT_HERE = frozenset("""
    handle_install_app handle_uninstall_app _run_uninstall handle_app_config handle_app_art_file
    _fetch_git_blob handle_blob_proxy handle_migrate_cleanup register_app_routes
    """.split())


def test_the_handlers_the_fs_baseline_pins_stay_in_the_facade() -> None:
    tree = ast.parse(_source(_FACADE))
    defined = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert sorted(_KEPT_HERE - defined) == []
    assert all(getattr(facade, name).__module__ == _FACADE for name in _KEPT_HERE)


#: Names the facade's own glue imports.
_GLUE = frozenset({"annotations", "_importlib", "_sys", "TYPE_CHECKING", "_facade_mirror"})


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
    assert _unaccounted_imports(_source(_FACADE), owners) == []


def test_the_import_census_finds_a_planted_import() -> None:
    owners = [_source(owner) for owner in _index().owners]
    head = "from __future__ import annotations\n"
    planted = _source(_FACADE).replace(head, head + "\nimport json as _s51_planted\n", 1)
    assert planted != _source(_FACADE)
    assert _unaccounted_imports(planted, owners) == ["_s51_planted"]


def test_every_owner_logs_as_the_facade() -> None:
    shared = logging.getLogger(_FACADE)
    assert facade.logger is shared
    loggers = {
        owner: vars(importlib.import_module(owner)).get("logger") for owner in _index().owners
    }
    assert {owner: log for owner, log in loggers.items() if log not in (None, shared)} == {}


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
    ``from kiro_crew.apps.routes import collect_publish_providers`` visible to mypy.
    """
    served = set(_index().exports)
    named = _typing_names(_source(_FACADE))
    assert "collect_publish_providers" in served, "the census found nothing to check"
    assert sorted(served - named) == []


def test_the_type_checker_census_finds_a_dropped_name() -> None:
    source = _source(_FACADE)
    assert "collect_publish_providers" in _typing_names(source)
    pruned = source.replace("        collect_publish_providers,\n", "", 1)
    assert pruned != source
    assert "collect_publish_providers" not in _typing_names(pruned)


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
    sources = source_corpus.iter_candidate_sources(require_all=("routes_",))
    importers = _owner_importers(
        ((path, text) for path, text in sources if path.is_relative_to(_SRC)), _SRC
    )
    assert "apps/routes_registry.py" in importers  # owners import the owners below them
    assert importers - family == set()


def test_the_owner_import_scan_finds_an_outside_importer(tmp_path: Path) -> None:
    root = tmp_path / "kiro_crew"
    sources = {
        root / "pkg" / "caller.py": "from kiro_crew.apps.routes_lifecycle import _redact_warning\n",
        root / "other.py": "from kiro_crew.apps import routes_lifecycle as low\n",
        root / "apps" / "relative.py": "from .routes_lifecycle import _redact_warning\n",
        root / "apps" / "deep" / "up.py": "from .. import routes_lifecycle\n",
        root / "facade_user.py": "from kiro_crew.apps.routes import _redact_warning\n",
    }
    assert _owner_importers(sources.items(), root) == {
        "pkg/caller.py",
        "other.py",
        "apps/relative.py",
        "apps/deep/up.py",
    }
