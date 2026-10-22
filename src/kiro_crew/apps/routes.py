"""App management REST API endpoints for the KiroCrew dashboard.

All endpoints are registered under ``/api/apps`` by the dashboard handler
setup. These are aiohttp-compatible handler functions.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import hmac as _hmac
import importlib
import importlib as _importlib
import json
import logging
import mimetypes
import os
import re
import stat
import sys
import sys as _sys
import time
from email.utils import formatdate
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
import yarl
from aiohttp import web

import kiro_crew._facade_mirror as _facade_mirror
from kiro_crew import platform_compat
from kiro_crew.apps import official_catalog
from kiro_crew.apps.backend import (
    get_app_backend_port,
    list_app_processes,
    recorded_backend_port,
    start_app_backend,
    stop_app_backend,
    unstopped_backend_port,
)
from kiro_crew.apps.bridges import (
    RegistrationResult,
    app_conversation_keys,
    deregister_app,
    deregister_app_crons_reporting_failures,
    register_app,
    register_app_crons_with_service,
)
from kiro_crew.apps.builtins import BUILTIN_NAMES
from kiro_crew.apps.dependencies import clean_dependencies
from kiro_crew.apps.dependencies import resolve_dependencies as _resolve_deps
from kiro_crew.apps.dependency_ledger import (
    canonical_dep_key,
    classify_and_clean_for_uninstall,
    classify_for_uninstall,
    declared_capability_keys,
)
from kiro_crew.apps.dev_mode import dev_mode_granted_root, is_dev_mode_cached
from kiro_crew.apps.event_bus import build_broadcast_fn
from kiro_crew.apps.execution import app_execution_denied
from kiro_crew.apps.hooks_integration import (
    get_all_hook_health,
    on_app_enable,
    stop_retained_startup_hooks,
)

# Aliased to keep `routes._run_lifecycle_script` patchable, which several tests rely on.
from kiro_crew.apps.lifecycle_scripts import run_lifecycle_script as _run_lifecycle_script
from kiro_crew.apps.manager import (
    _credential_free_source_metadata,
    app_enabled_state,
    app_lifecycle_lock,
    apps_dir,
    disable_app,
    enable_app,
    get_app,
    get_app_manifest,
    install_app,
    is_app_enabled,
    list_apps,
    refresh_app_config_keys,
    register_external_app,
    resolve_mcp_backend_url,
    trust_grant_removal_blocked,
    uninstall_app,
    update_app,
)
from kiro_crew.apps.manifest import Dependencies, PlatformConfig, app_endpoint_allowed
from kiro_crew.apps.official_category_order import forget_cache as forget_category_order_cache
from kiro_crew.apps.official_category_order import load_category_order
from kiro_crew.apps.official_editorial import forget_cache as forget_editorial_cache
from kiro_crew.apps.official_editorial import load_sections
from kiro_crew.apps.proxy_auth import (
    PRINCIPAL_AGENT_TOOL,
    PRINCIPAL_APP_TOKEN,
    PRINCIPAL_NONE,
    PRINCIPAL_OWNER_SESSION,
    PROXY_PRINCIPAL_HEADER,
    sign_proxy_principal_claim,
)
from kiro_crew.apps.registry import (
    _ART_IMAGE_EXTENSIONS,
    _ART_MANIFEST_FIELDS,
    _ART_MANIFEST_LIST_FIELDS,
    _ART_MAX_BYTES,
    _REGISTRY_TRUST_TIERS,
    _SAFE_PATH_RE,
    _TRUST_INDEX,
    _TRUST_OWNER,
    _blob_cache_dir,
    _blob_cache_key,
    _context_clone_sandbox_mode,
    _entry_git_url,
    _git_fetch_branch,
    _git_target_is_unsupported,
    _git_url_host,
    _loggable_git_transport_output,
    _owner_designated_repo_target,
    _pinned_registries,
    _registry_identity_key,
    _registry_trust_tier_of,
    _repo_key_owner_count,
    _same_git_target,
    _sel_credential_grant,
    _strip_git_target_userinfo,
    anonymous_git_env,
    get_registry_app_by_repo,
    get_server_platform,
    install_from_registry,
    is_registry_source,
    known_registry_repos,
    list_catalog_apps,
    list_registry,
    minimal_env,
    registry_name_from_source,
    resolve_installed_trust_repository,
)
from kiro_crew.apps.registry_pipeline.checkout import _HOOKS_NEUTRALIZER_ARGV
from kiro_crew.apps.routes_art import handle_app_art_file, handle_blob_proxy
from kiro_crew.apps.routes_enable import handle_disable_app, handle_enable_app, handle_open_app
from kiro_crew.apps.routes_install import (
    handle_install_app,
    handle_register_external,
    handle_update_app,
)
from kiro_crew.apps.routes_listing import (
    handle_app_config,
    handle_get_app,
    handle_get_manifest,
    handle_list_apps,
    handle_publish_providers,
)
from kiro_crew.apps.routes_proxy import handle_app_api_proxy
from kiro_crew.apps.routes_registry import (
    handle_registries,
    handle_registries_refresh,
    handle_registry,
    handle_registry_install,
    handle_registry_install_stream,
    handle_registry_refresh,
)
from kiro_crew.apps.routes_static import handle_app_dev_mode, handle_app_ui_file
from kiro_crew.apps.routes_uninstall import (
    handle_migrate_cleanup,
    handle_uninstall_app,
    handle_uninstall_preview,
)
from kiro_crew.apps.spawn_sdk import build_spawn_impl
from kiro_crew.apps.teardown import forget_app_hooks, teardown_app_runtime
from kiro_crew.apps.update_notify import (
    deliver_app_update_notes,
    prepare_app_update_notes,
)
from kiro_crew.apps.update_status import attach_update, attach_updates
from kiro_crew.apps.version import check_min_version as _check_min_version_str
from kiro_crew.config.loader import (
    ConfigReadError,
    KiroCrewConfig,
    config_path,
    update_config_locked,
)
from kiro_crew.cron import CronStoreBusy, CronStoreUnreadable
from kiro_crew.dashboard.conditional_get import conditional_response, is_not_modified
from kiro_crew.executors import subprocess_executor
from kiro_crew.pinned_fs import (
    PinnedPathRefusal,
    fd_real_path,
    is_reparse_point,
    open_in_pinned_parent,
    supports_pinned_walk,
)
from kiro_crew.publish_governance import DEPLOY_WEB_PROVIDER_ID, publish_denied_reason
from kiro_crew.sandbox import (
    cgroup_scope_argv,
    create_subprocess_limited,
    scrub_env,
    wrap_argv,
    wrap_argv_async,
)
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)


def register_app_routes(app: web.Application) -> None:
    """Register all app management routes on an aiohttp Application."""

    async def _start_proxy_session(app: web.Application) -> None:
        app["_proxy_session"] = aiohttp.ClientSession()

    async def _close_proxy_session(app: web.Application) -> None:
        session = app.get("_proxy_session")
        if session and not session.closed:
            await session.close()

    app.on_startup.append(_start_proxy_session)
    app.on_cleanup.append(_close_proxy_session)

    app.router.add_get("/api/apps", handle_list_apps)
    app.router.add_get("/api/publish-providers", handle_publish_providers)
    app.router.add_get("/api/apps/registry", handle_registry)
    app.router.add_get("/api/apps/registries", handle_registries)
    app.router.add_put("/api/apps/registries", handle_registries)
    app.router.add_post("/api/apps/registries/refresh", handle_registries_refresh)
    app.router.add_get("/api/apps/blob", handle_blob_proxy)
    # Outside /api/apps/ on purpose: _app_owns_path would grant an app named
    # `registry` implicit ownership of /api/apps/registry/* -- see the
    # handler's docstring.
    app.router.add_post("/api/app-store/refresh", handle_registry_refresh)
    app.router.add_post("/api/apps/registry/install", handle_registry_install)
    app.router.add_post("/api/apps/registry/install-stream", handle_registry_install_stream)
    app.router.add_post("/api/apps/install", handle_install_app)
    app.router.add_post("/api/apps/register", handle_register_external)
    app.router.add_get("/api/apps/{name}", handle_get_app)
    app.router.add_get("/api/apps/{name}/manifest", handle_get_manifest)
    app.router.add_get("/api/apps/{name}/config", handle_app_config)
    app.router.add_put("/api/apps/{name}/config", handle_app_config)
    app.router.add_get("/api/apps/{name}/uninstall/preview", handle_uninstall_preview)
    app.router.add_post("/api/apps/{name}/uninstall", handle_uninstall_app)
    app.router.add_post("/api/apps/{name}/update", handle_update_app)
    app.router.add_post("/api/apps/{name}/enable", handle_enable_app)
    app.router.add_post("/api/apps/{name}/disable", handle_disable_app)
    app.router.add_post("/api/apps/{name}/open", handle_open_app)
    app.router.add_post("/api/apps/{name}/dev", handle_app_dev_mode)
    app.router.add_delete("/api/apps/{name}/migrate-cleanup", handle_migrate_cleanup)
    app.router.add_get("/apps/{name}/ui/{path:.*}", handle_app_ui_file)
    app.router.add_get("/apps/{name}/art/{path:.*}", handle_app_art_file)
    # Reverse proxy: dashboard app UI → app backend (same-origin, avoids CORS)
    app.router.add_route("*", "/apps/{name}/api/{path:.*}", handle_app_api_proxy)


# This module keeps only the route table. It is the facade of the app management route
# owners: every name is reached through it as ``kiro_crew.apps.routes.<name>``, a read is served by
# the owner that binds the name, and a write -- ``monkeypatch.setattr``, ``mock.patch``
# -- reaches every owner holding it (:mod:`kiro_crew._facade_mirror`). Owners never
# import this module, and each imports only the owners listed before it in ``_OWNERS``.
# New work goes to the owner of its responsibility:
#
#   ``routes_lifecycle`` -- Steps install, update, uninstall, enable and disable share.
#   ``routes_proxy`` -- The reverse proxy from an app's UI to its backend, and its secret cache.
#   ``routes_static`` -- An installed app's UI bundle and the dev-mode toggle.
#   ``routes_art`` -- The installed-art route, and the git blob proxy with its validators.
#   ``routes_listing`` -- The app list, one app, its manifest and config, the publish providers.
#   ``routes_install`` -- Installing from a local path, updating, and registering an external app.
#   ``routes_uninstall`` -- The uninstall, its preview and cron retry, and migrated-app cleanup.
#   ``routes_enable`` -- Enable, disable and open an installed app.
#   ``routes_registry`` -- Browsing, refreshing and installing from the registry; its configured sources.
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.apps.routes_lifecycle",
    "kiro_crew.apps.routes_proxy",
    "kiro_crew.apps.routes_static",
    "kiro_crew.apps.routes_art",
    "kiro_crew.apps.routes_listing",
    "kiro_crew.apps.routes_install",
    "kiro_crew.apps.routes_uninstall",
    "kiro_crew.apps.routes_enable",
    "kiro_crew.apps.routes_registry",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.apps.routes_art import (  # noqa: F401
        _BLOB_FETCH_SEMAPHORE,
        _BLOB_FETCH_TIMEOUT,
        _SAFE_HTTPS_URL_RE,
        _SAFE_REF_RE,
        _SAFE_REPO_RE,
        _SAFE_SCP_URL_RE,
        _SAFE_SSH_URL_RE,
        _declared_art_paths,
        _fetch_git_blob,
        _is_safe_repo_identifier,
        _read_declared_art,
        _touch_served_blob,
    )
    from kiro_crew.apps.routes_enable import (  # noqa: F401
        _BUILTIN_SERVICE_APPS,
        _OPEN_COMMAND_DESKTOP_ENV_KEYS,
        _client_install_manifest,
        _notify_builtin_service,
        _sync_builtin_config,
    )
    from kiro_crew.apps.routes_install import (  # noqa: F401
        _resync_app_crons_after_update,
    )
    from kiro_crew.apps.routes_lifecycle import (  # noqa: F401
        _app_may_run_after_install,
        _check_min_version,
        _deregister_app_off_loop,
        _redact_warning,
        _refuse_while_startup_hook_runs,
        _register_app_off_loop,
        _restore_app_after_failed_update,
        _start_backend_after_install,
        _stop_backend_and_observe,
        _suspend_app_for_session_approval_reconsent,
        _unregister_notification_channels,
    )
    from kiro_crew.apps.routes_listing import (  # noqa: F401
        _installed_app_detail,
        _listed_apps_with_trust,
        _provider_is_configured,
        _stamp_installed_trust_repository,
        collect_publish_providers,
    )
    from kiro_crew.apps.routes_proxy import (  # noqa: F401
        _PROXY_HOP_HEADERS,
        _PROXY_IDLE_TIMEOUT,
        _PROXY_STRIP_HEADERS,
        _PROXY_TIMEOUT,
        _app_secret_cache,
        _end_relay_midbody,
        _get_app_secret,
        _is_event_stream,
        _proxy_principal,
        _resolve_app_backend_url,
        invalidate_app_secret_cache,
    )
    from kiro_crew.apps.routes_registry import (  # noqa: F401
        _announce_app_updates,
        _derive_registry_name,
        _write_registries_config,
    )
    from kiro_crew.apps.routes_static import (  # noqa: F401
        _ALLOWED_EXTENSIONS,
        _CONTENT_TYPES,
        _UI_MAX_BYTES,
        _UI_STREAM_CHUNK,
        _UI_STREAM_SEMAPHORE,
        _UI_STREAM_TIMEOUT_DEFAULT,
        _open_ui_file,
        _ui_stream_timeout_secs,
    )
    from kiro_crew.apps.routes_uninstall import (  # noqa: F401
        _CRON_CLEANUP_ATTEMPTS,
        _CRON_CLEANUP_BACKOFF_SECS,
        _deregister_crons_with_retry,
        _run_uninstall,
    )
