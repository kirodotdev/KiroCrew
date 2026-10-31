"""App Manager — install, uninstall, enable, disable lifecycle for KiroCrew apps.

Apps are installed to ``~/.kiro/crew/apps/{name}/``.  Each installed app has an
``installed.json`` metadata file tracking version, timestamp, and enabled state.

The manager validates manifests, copies app files, and delegates resource
registration (agents, skills, crons) to bridge functions.
"""

from __future__ import annotations

import importlib as _importlib
import sys as _sys
from typing import TYPE_CHECKING

from kiro_crew import _facade_mirror

# This module is the facade of the app manager owners, and every name is reached
# through it as ``kiro_crew.apps.manager.<name>``: a read is served by the owner that binds
# the name, and a write -- ``monkeypatch.setattr``, ``mock.patch`` -- reaches every owner
# holding it (:mod:`kiro_crew._facade_mirror`). Owners never import this module, and each
# imports only the owners listed before it in ``_OWNERS``. New work goes to the owner of
# its responsibility:
#
#   ``manager_records`` -- An installed app's on-disk state: paths, installed.json, approved grants, update fencing.
#   ``manager_tree`` -- Validating a source directory and copying it into the tree an install leaves.
#   ``manager_install`` -- install_app and update_app, with the per-app lifecycle lock they share.
#   ``manager_activation`` -- enable_app, disable_app and the governance check every activation passes.
#   ``manager_builtins`` -- Built-in apps: registration, default-on backfill, orphans, migrated cleanup.
#   ``manager_external`` -- register_external_app: the record of a self-managed app.
#   ``manager_uninstall`` -- uninstall_app and the agent.apps_trusted grant it drops and restores.
#   ``manager_lookup`` -- list_apps, get_app, enablement reads, and which installed app owns an agent.


#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.apps.manager_records",
    "kiro_crew.apps.manager_tree",
    "kiro_crew.apps.manager_install",
    "kiro_crew.apps.manager_activation",
    "kiro_crew.apps.manager_builtins",
    "kiro_crew.apps.manager_external",
    "kiro_crew.apps.manager_uninstall",
    "kiro_crew.apps.manager_lookup",
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
    from kiro_crew.apps.manager_activation import (  # noqa: F401
        _app_activation_denied,
        _session_approval_consent_unrecorded_on_disk,
        disable_app,
        enable_app,
        mark_session_approval_consent_pending,
        session_approval_consent_candidates,
    )
    from kiro_crew.apps.manager_builtins import (  # noqa: F401
        _BUILTIN_APPS,
        _DEFAULT_ON_BACKFILL,
        _DEFAULT_ON_BUILTINS,
        _REQUIRED_BUILTIN_FIELDS,
        _app_declares_backend,
        _audit_default_on_backfill,
        _builtin_owns_install,
        _dirfd_ops_supported,
        _edition_builtin_apps,
        _edition_bundled_app_names,
        _effective_migrated_to,
        _orphaned_builtins_cache,
        _rmtree_dirfd,
        _validate_builtin_app,
        backfill_default_on_builtins,
        builtin_owns_installed,
        cleanup_migrated_builtin,
        detect_orphaned_builtins,
        invalidate_orphan_cache,
        migrated_builtin_cleanup_applies,
        register_builtin_apps,
        resolve_mcp_backend_url,
        shipped_builtin_names,
    )
    from kiro_crew.apps.manager_external import (  # noqa: F401
        register_external_app,
    )
    from kiro_crew.apps.manager_install import (  # noqa: F401
        _LIFECYCLE_LOCKS,
        _REGISTRY_SOURCE_REPOSITORY,
        _effective_source_repository,
        app_lifecycle_lock,
        install_app,
        registry_source_repository,
        update_app,
    )
    from kiro_crew.apps.manager_lookup import (  # noqa: F401
        TASK_RUNNER_APP,
        TASK_RUNNER_DISABLED_MESSAGE,
        AgentOwner,
        _absence_is_genuine,
        _app_agent_names,
        _app_agent_registration,
        _app_display_name,
        _app_registered_agent,
        _owner_json,
        _screened_spec_path,
        app_agent_backend_refusal,
        app_agent_launch_refusal,
        app_command_agent_refusal,
        app_disabled,
        app_enabled_state,
        get_app,
        is_app_enabled,
        list_apps,
        logger,
        missing_agent_reason,
        owning_app_for_agent,
        set_app_provenance,
        set_app_source,
    )
    from kiro_crew.apps.manager_records import (  # noqa: F401
        _ALWAYS_STORED_FAMILIES,
        _TREE_SWAP_LAST_CAP,
        _VALID_LIFECYCLE,
        _VALID_ORIGIN,
        _VALID_RESOURCES,
        APP_MANIFEST_FILENAME,
        APPROVED_GRANTS_FILENAME,
        INSTALLED_META_FILENAME,
        SESSION_APPROVAL_PENDING_KEY,
        STAGED_GRANT_FAMILIES,
        AppResult,
        InstalledApp,
        _approved_grants_after_manifest_change,
        _approved_grants_path,
        _approved_grants_row,
        _config_keys_refresh_lock,
        _credential_free_source_metadata,
        _declared_grant_entries,
        _effective_approved_grants,
        _held_back_entries,
        _now_iso,
        _parse_approved_grants,
        _pending_session_approval_after_manifest_change,
        _read_approved_grants,
        _read_enabled,
        _read_installed,
        _record_lock_path,
        _record_lock_state,
        _scan_installed_app_config_keys,
        _session_approval_consent_unrecorded,
        _session_approval_save_rule,
        _SwapState,
        _tree_swap,
        _tree_swap_guard,
        _tree_swap_last,
        _tree_swap_mark,
        _tree_swap_seq,
        _tree_swap_spanned,
        _tree_swap_state,
        _tree_swaps_active,
        _update_in_progress,
        _update_marker_dir,
        _update_retired_tree_prefix,
        _UpdateMarker,
        _write_approved_grants,
        _write_installed,
        app_data_dir,
        app_dir,
        app_record_lock,
        approved_manifest_permissions,
        apps_dir,
        get_app_manifest,
        hold_app_record_lock,
        holds_app_record_locks,
        refresh_app_config_keys,
        staged_app_grants,
    )
    from kiro_crew.apps.manager_tree import (  # noqa: F401
        _COPY_IGNORE,
        _DEPS_STAGING_SWEEP_RE,
        InstalledTreeRefused,
        _check_min_version,
        _check_path_safety,
        _copy_app_tree,
        _is_generated_deps_artifact_name,
        _owned_data_dir,
        _remove_any_shape,
        _reserved_name_code,
        _temp_data_name_obstruction,
        _validate_source_path,
        copy_app_tree_as_installed,
        gateway_data_dir_obstruction,
        preserved_data_awaits,
    )
    from kiro_crew.apps.manager_uninstall import (  # noqa: F401
        _RMTREE_BACKOFF_SECONDS,
        _RMTREE_MAX_ATTEMPTS,
        _SHARING_VIOLATION_WINERRORS,
        _drop_trust_grant,
        _has_trust_grant,
        _restore_trust_grant,
        _rmtree_app_dir,
        _trust_grant_local,
        _trust_grant_repository,
        trust_grant_removal_blocked,
        uninstall_app,
    )

if TYPE_CHECKING:  # names it serves from outside the family, from their homes
    import contextlib  # noqa: F401
    import functools  # noqa: F401
    import ipaddress  # noqa: F401
    import json  # noqa: F401
    import logging  # noqa: F401
    import os  # noqa: F401
    import re  # noqa: F401
    import shutil  # noqa: F401
    import stat  # noqa: F401
    import threading  # noqa: F401
    import time  # noqa: F401
    from collections import (  # noqa: F401
        OrderedDict,
    )
    from contextlib import (  # noqa: F401
        contextmanager,
    )
    from contextvars import (  # noqa: F401
        ContextVar,
    )
    from dataclasses import (  # noqa: F401
        asdict,
        dataclass,
        replace,
    )
    from pathlib import (  # noqa: F401
        Path,
    )
    from typing import (  # noqa: F401
        Any,
        Callable,
        Iterator,
        Sequence,
    )
    from urllib.parse import (  # noqa: F401
        urlparse,
    )

    from kiro_crew import (  # noqa: F401
        platform_compat,
    )
    from kiro_crew.apps.admission import (  # noqa: F401
        app_admission_denied,
    )
    from kiro_crew.apps.discovery import (  # noqa: F401
        discover_builtin_apps,
    )
    from kiro_crew.apps.execution import (  # noqa: F401
        app_execution_denied,
        repository_bound_grant_denied,
        shipped_builtin_app_root,
    )
    from kiro_crew.apps.link_preview_grants import (  # noqa: F401
        link_preview_grant_row,
    )
    from kiro_crew.apps.manifest import (  # noqa: F401
        RESERVED_APP_NAME_CODE,
        AppManifest,
        app_name_error,
        contain_command_agents,
        cron_permission_error,
        is_reserved_app_name,
    )
    from kiro_crew.apps.point_grants import (  # noqa: F401
        grant_source,
        point_grant_families,
    )
    from kiro_crew.atomic_write import (  # noqa: F401
        atomic_write,
        on_event_loop,
    )
    from kiro_crew.config.loader import (  # noqa: F401
        ConfigReadError,
        config_dir,
        config_local_path,
        config_path,
        read_config_text,
        update_config_locked,
    )
    from kiro_crew.loop_lock import (  # noqa: F401
        LoopBoundLock,
    )
    from kiro_crew.owner_only_files import (  # noqa: F401
        ensure_directory,
    )
    from kiro_crew.pinned_fs import (  # noqa: F401
        supports_pinned_walk,
    )
    from kiro_crew.platform import (  # noqa: F401
        current_context,
        safe_context_call,
    )
    from kiro_crew.platform_compat import (  # noqa: F401
        is_link_or_junction,
    )
    from kiro_crew.sel import (  # noqa: F401
        sel,
    )
