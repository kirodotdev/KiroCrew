"""Registration bridges — wire app resources into KiroCrew's runtime.

When an app is installed or enabled, its agents, skills, and cron jobs need
to be registered with KiroCrew's existing systems.  This module provides
``register_app`` and ``deregister_app`` which handle the namespacing and
symlink/copy operations.

Namespace convention: ``{app_name}/{resource_name}`` to avoid collisions
between apps.
"""

from __future__ import annotations

import importlib as _importlib
import sys as _sys
from typing import TYPE_CHECKING

from kiro_crew import _facade_mirror

# This module is the facade of the app registration bridge owners, and every name is reached
# through it as ``kiro_crew.apps.bridges.<name>``: a read is served by the owner that binds
# the name, and a write -- ``monkeypatch.setattr``, ``mock.patch`` -- reaches every owner
# holding it (:mod:`kiro_crew._facade_mirror`). Owners never import this module, and each
# imports only the owners listed before it in ``_OWNERS``. New work goes to the owner of
# its responsibility:
#
#   ``bridges_sources`` -- Which manifest and root an app registers from, its resource names and target dirs.
#   ``bridges_mcp_config`` -- Kiro Crew's own agent config file: its path, its lock, locked reads and writes.
#   ``bridges_agents`` -- Materializing an app's agents into the kiro-cli agents directory.
#   ``bridges_skills`` -- Linking an app's skills into the Kiro Crew skills directory.
#   ``bridges_crons`` -- An app's cron manifest and its jobs in the running CronService.
#   ``bridges_mcp`` -- Registering an app's MCP servers, and resolving each stdio server's command.
#   ``bridges_register`` -- register_app / deregister_app as a whole, and an app's session pointers.


#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.apps.bridges_sources",
    "kiro_crew.apps.bridges_mcp_config",
    "kiro_crew.apps.bridges_agents",
    "kiro_crew.apps.bridges_skills",
    "kiro_crew.apps.bridges_crons",
    "kiro_crew.apps.bridges_mcp",
    "kiro_crew.apps.bridges_register",
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
    from kiro_crew.apps.bridges_agents import (  # noqa: F401
        _FRAMEWORK_OWNED_AGENT_KEYS,
        _RENDERED_AGENTS_DIRNAME,
        _TEMPLATE_PLACEHOLDER_RE,
        AGENT_MCP_POLICY_FILE,
        _agent_mcp_policy,
        _apply_agent_mcp_policy,
        _apply_agent_prompt,
        _ceiling_filtered_allowed,
        _deregister_agents,
        _global_mcp_specs,
        _is_within,
        _managed_mcp_spec,
        _materialize_managed_refs,
        _may_auto_approve,
        _own_mcp_servers,
        _placeholder_values,
        _preserve_user_agent_edits,
        _read_agent_config,
        _register_agents,
        _render_shipped_agent,
        _strip_ungoverned_auto_approve,
        _unlink_unheld_app_lock,
        _unresolvable_tool_refs,
        purge_uninstalled_agent_residue,
    )
    from kiro_crew.apps.bridges_crons import (  # noqa: F401
        _CREATE_ONLY_CRON_KEYS,
        _CRON_MANIFEST_NAME,
        _FIELD_DIGEST_CHARS,
        _LATE_KEY_DEFAULTS,
        _MANIFEST_OWNED_CRON_KEYS,
        _OVERWRITE_LOG_VALUE_CHARS,
        _UPDATE_KWARGS_FOR_KEY,
        _admitted_app_cron_defs,
        _app_crons_path,
        _changed_manifest_keys,
        _clip,
        _cron_defs_from_manifest,
        _deregister_crons,
        _field_digest,
        _join_stamp,
        _manifest_job_fields,
        _overwritten_fields,
        _register_crons,
        _report_pending_reenable,
        _resolve_and_vet_app_script,
        _resync_app_cron,
        _stamp_digests,
        _stamp_fields,
        _stored_digest,
        _unapplicable_in_place,
        app_cron_manifest_digest,
        deregister_app_crons_from_service,
        deregister_app_crons_reporting_failures,
        disarm_app_crons_for_execution,
        load_app_cron_defs,
        reconcile_app_crons_for_execution,
        register_app_crons_with_service,
        retire_app_crons_for_reload,
    )
    from kiro_crew.apps.bridges_mcp import (  # noqa: F401
        _BARE_PYTHON,
        _DEPS_BOOT_PATH,
        _HOST_CLI_COMMAND,
        _LEGACY_SHARED_MCP_PATH,
        _PY_OPTS_WITH_SEPARATE_VALUE,
        _deregister_mcp_servers,
        _has_python_shebang,
        _live_port_for,
        _maybe_provision_backendless_deps,
        _normalize_attached_m,
        _pin_host_cli_command,
        _py_target_index,
        _python_shebang_interpreter,
        _register_mcp_servers,
        _resolve_app_launcher_arg,
        _resolve_live_mcp_url,
        _schedule_unresolvable_warning,
        _scrub_legacy_shared_mcp,
        _strip_deps_pythonpath,
        _targets_gateway_module,
        _warn_unresolvable_stdio_command,
        _warn_unresolved_secret_refs,
        _zip_has_main,
        kiro_crew_file,
        registered_app_mcp_servers,
        reregister_app_mcp_servers,
        resolve_stdio_command,
        scrub_backend_mcp_url,
    )
    from kiro_crew.apps.bridges_mcp_config import (  # noqa: F401
        _health_reconcile_guard,
        _mcp_json_path,
        _mcp_lock,
        _read_mcp_json,
        _read_mcp_json_unlocked,
        _write_mcp_json_unlocked,
    )
    from kiro_crew.apps.bridges_register import (  # noqa: F401
        RegistrationResult,
        SessionPointerCleanup,
        _prune_stale_app_resources,
        app_conversation_keys,
        deregister_app,
        discard_app_session_pointers,
        logger,
        reconcile_enabled_app_resources,
        refresh_app_agents,
        register_app,
    )
    from kiro_crew.apps.bridges_skills import (  # noqa: F401
        _RESERVED_SKILL_DIRS,
        _deregister_skills,
        _register_skills,
        reconcile_app_skills,
    )
    from kiro_crew.apps.bridges_sources import (  # noqa: F401
        KIRO_AGENTS_DIR,
        SKILLS_DIR_NAME,
        _app_resource_root,
        _kiro_agents_dir,
        _namespace,
        _registration_denied,
        _registration_source,
        _safe_link_name,
        _skills_dir,
        app_skills_dir,
    )

if TYPE_CHECKING:  # names it serves from outside the family, from their homes
    import asyncio  # noqa: F401
    import hashlib  # noqa: F401
    import json  # noqa: F401
    import logging  # noqa: F401
    import os  # noqa: F401
    import re  # noqa: F401
    import shutil  # noqa: F401
    import stat  # noqa: F401
    import sys  # noqa: F401
    import zipfile  # noqa: F401
    from contextlib import (  # noqa: F401
        ExitStack,
        contextmanager,
    )
    from dataclasses import (  # noqa: F401
        dataclass,
        field,
    )
    from pathlib import (  # noqa: F401
        Path,
    )
    from typing import (  # noqa: F401
        Any,
        Callable,
        Container,
        Iterable,
        Iterator,
        Optional,
    )
    from urllib.parse import (  # noqa: F401
        urlparse,
        urlunparse,
    )

    from kiro_crew import cron as cron_seams  # noqa: F401
    from kiro_crew import (  # noqa: F401
        pinned_fs,
        platform_compat,
    )
    from kiro_crew.apps import deps_boot as _deps_boot_module  # noqa: F401
    from kiro_crew.apps.cron_sdk import (  # noqa: F401
        CronSDK,
    )
    from kiro_crew.apps.execution import (  # noqa: F401
        app_execution_denied,
        shipped_builtin_app_root,
    )
    from kiro_crew.apps.interpreter import (  # noqa: F401
        app_deps_dir,
        path_command_is_abi_matched,
        resolve_app_python,
        venv_provided_command,
    )
    from kiro_crew.apps.manager import (  # noqa: F401
        app_data_dir,
        app_dir,
        get_app,
        get_app_manifest,
        list_apps,
    )
    from kiro_crew.apps.manifest import (  # noqa: F401
        AppManifest,
        Containment,
        contained_in_screened_root,
        has_stdio_mcp_server,
        is_module_style_entry_point,
        screen_app_path,
    )
    from kiro_crew.atomic_write import (  # noqa: F401
        atomic_write,
    )
    from kiro_crew.config.loader import (  # noqa: F401
        config_dir,
        publish_materialized_agents,
        schedule_materialized_agents_refresh,
    )
    from kiro_crew.config.paths import (  # noqa: F401
        kiro_agents_dir,
    )
    from kiro_crew.cron import (  # noqa: F401
        CronPendingMismatch,
        CronStoreBusy,
        CronStoreUnreadable,
        lookup_cron_folder_id,
    )
    from kiro_crew.cron_script import (  # noqa: F401
        resolve_script_path,
    )
    from kiro_crew.env import (  # noqa: F401
        emit_env,
    )
    from kiro_crew.executors import (  # noqa: F401
        maintenance_executor,
    )
    from kiro_crew.gateway_lock import (  # noqa: F401
        GatewayLock,
        GatewayLockError,
        LockFileError,
    )
    from kiro_crew.history import (  # noqa: F401
        ConversationLog,
    )
    from kiro_crew.mcp_utils import (  # noqa: F401
        without_mcp_refs,
    )
    from kiro_crew.platform.governance import (  # noqa: F401
        may_skip_gate_now,
        strip_ungoverned_auto_approve,
    )
    from kiro_crew.security import (  # noqa: F401
        is_sensitive_path,
    )
    from kiro_crew.sel import (  # noqa: F401
        sel,
    )
    from kiro_crew.session_map import (  # noqa: F401
        SUPPRESS_REPLAY_FLAG,
        SessionMap,
    )
    from kiro_crew.user_json import (  # noqa: F401
        loads_mcp_config,
        loads_user_json,
    )
    from kiro_crew.zip_vet import (  # noqa: F401
        ZipInventoryRejected,
        vet_zip_inventory,
    )
