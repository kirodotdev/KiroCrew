"""OS-level sandbox for agent child processes.

Hides sensitive credential paths from the kiro-cli subprocess tree using
platform-native isolation, per tier:

- ``standard`` (what the default ``"auto"`` resolves to) masks ``~/.gnupg``,
  ``~/.docker``, ``~/.azure``, ``~/.config/gcloud``, the crew vault and the
  governance cache -- and DELIBERATELY leaves ``~/.aws``, ``~/.ssh`` and
  ``~/.kube`` visible so the aws CLI, ``credential_process``, git-over-SSH and
  kubectl keep working inside the agent (see ``_STANDARD_DIRS``). The file
  tools still refuse those paths through ``is_sensitive_path``; a spawned
  shell's ``open()`` is not fenced at this tier.
- ``strict`` masks all of the above plus ``~/.aws``, ``~/.kube``,
  ``~/.config/gh`` and ``~/.ssh`` (exposing only ``~/.ssh/known_hosts``).
- ``cc`` is the Claude Code backend's tier: ``strict`` minus ``~/.ssh`` and
  ``~/.config/gh``, with a read-only copy of ``~/.aws/config`` exposed for
  Bedrock auth.

- **Linux**: fork → ``unshare(CLONE_NEWUSER)`` → parent writes identity
  UID/GID map → ``unshare(CLONE_NEWNS)`` → bind-mount empty dirs → exec.
  The child retains the real UID so all toolchains work normally.
- **macOS**: ``sandbox-exec`` with a Seatbelt profile that denies reads

The parent KiroCrew process is completely unaffected — isolation applies
only to the spawned child.  Falls back gracefully to no sandbox when the
OS mechanism is unavailable (logged as warning).

Config: ``"sandbox": "auto" | "strict" | "off"`` in ``~/.kiro/crew/config.json``.
``"auto"`` (default) uses the standard-tier namespace sandbox on Linux and
seatbelt on macOS; ``"strict"`` is the opt-in tier that also hides ``~/.aws``.
"""

from __future__ import annotations

import asyncio  # noqa: F401
import contextlib  # noqa: F401
import ctypes
import ctypes.util  # noqa: F401
import errno  # noqa: F401
import functools  # noqa: F401
import hashlib  # noqa: F401
import importlib
import json  # noqa: F401
import logging
import os  # noqa: F401
import re  # noqa: F401
import select  # noqa: F401
import shlex  # noqa: F401
import shutil  # noqa: F401
import socket  # noqa: F401
import stat  # noqa: F401
import subprocess  # noqa: F401
import sys
import sys as _sys
import tempfile  # noqa: F401
import threading  # noqa: F401
import time  # noqa: F401
import urllib.parse  # noqa: F401
from pathlib import Path  # noqa: F401
from types import ModuleType  # noqa: F401
from typing import TYPE_CHECKING, Literal, NamedTuple  # noqa: F401

import kiro_crew as _kiro_crew_pkg  # noqa: F401
from kiro_crew import (  # noqa: F401
    _facade_mirror,
    platform_compat,
    sandbox_launcher,
    sandbox_mount_sweep,
    sandbox_plan,
    sandbox_seatbelt,
)
from kiro_crew.atomic_write import fsync_dir, refuse_linked_parent  # noqa: F401
from kiro_crew.config.paths import config_dir, kiro_agents_dir  # noqa: F401
from kiro_crew.constants import (  # noqa: F401
    KIROCREW_SANDBOX_TOOL_ENV,
    KIROCREW_SANDBOX_TOOL_VALUE,
    KIROCREW_SPAWNED_ENV,
    KIROCREW_SPAWNED_VALUE,
)
from kiro_crew.identity_stores import AUTH_SQLITE_DB, AUTH_SQLITE_SIDECAR_SUFFIXES  # noqa: F401
from kiro_crew.pinned_fs import fd_real_path  # noqa: F401
from kiro_crew.platform import current_context  # noqa: F401
from kiro_crew.terminal_safe import safe_terminal_line  # noqa: F401

try:
    import resource as _resource_mod
except ImportError:  # non-POSIX (Windows)
    _resource_mod = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence  # noqa: F401
    from concurrent.futures import ThreadPoolExecutor  # noqa: F401
    from typing import Any  # noqa: F401

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Compatibility facade. What a spawn masks is decided once by
# ``kiro_crew.sandbox_plan``; the Linux namespace launcher and the macOS Seatbelt
# profile are rendered from that plan by ``kiro_crew.sandbox_launcher`` and
# ``kiro_crew.sandbox_seatbelt``, and the reclaim of the launcher's stale bind-mount
# sources lives in ``kiro_crew.sandbox_mount_sweep``. The rest of what this module
# defined is carved into the owner modules in ``_CARVED_OWNERS``. Every moved name stays
# readable as ``kiro_crew.sandbox.<name>``: a name this module binds is read here, and
# every other one (``_EXPORTS_BY_OWNER``) is FORWARDED -- the mirror's module class reads
# it from its owner, resolved from :data:`sys.modules` on each use. A write or delete through this module reaches every module that holds the
# name: a name moved to the renderers or the mount sweep lands on that owner alone, as
# it did before the carve, and any other name lands on this module (when it binds the
# name) and on every carved owner that binds the same object (``kiro_crew._facade_mirror``),
# so a patch of ``kiro_crew.sandbox.<name>`` reaches every reader it reached when this
# was one module, however the test spells it. A forwarded name is absent from this
# module's namespace on purpose -- a binding here would shadow the owner for every later
# read. The path rules that moved to ``sandbox_plan`` are not forwarded: the few this
# module still binds under their old names are compatibility aliases of the plan's
# functions, and patching an alias reaches nothing the planner reads -- patch
# ``sandbox_plan.<rule>``, or build a ``PlanHost``.
#
# The imports at the top stay bound even where this module's own code does not read
# them (``# noqa: F401``): they are its surface, which callers read and tests patch.
#
# No owner imports this module: the renderers read only the plan they are handed, and
# the carved owners import each other lowest layer first.
# ``test/test_sandbox_refactor_facade.py`` pins the forwarding, the mirror and that
# direction.
# --------------------------------------------------------------------------- #
#: The modules carved out of this one, lowest layer first: the first one that binds a
#: name answers a forwarded read of it. They are imported here, before the mirror
#: indexes them, so no reader can find one half-built.
_CARVED_OWNERS: tuple[str, ...] = (
    "kiro_crew.sandbox_leaves",
    "kiro_crew.sandbox_tiers",
    "kiro_crew.sandbox_precreate",
    "kiro_crew.sandbox_seals",
    "kiro_crew.sandbox_aliases",
    "kiro_crew.sandbox_staging",
    "kiro_crew.sandbox_workspace",
    "kiro_crew.sandbox_argv",
    "kiro_crew.sandbox_delegation",
    "kiro_crew.sandbox_probe",
    "kiro_crew.sandbox_unavailable",
    "kiro_crew.sandbox_wrap",
    "kiro_crew.sandbox_cgroup",
    "kiro_crew.sandbox_slice",
    "kiro_crew.sandbox_rlimit",
    "kiro_crew.sandbox_spawn",
)


def _import_carved_owners() -> None:
    for dotted in _CARVED_OWNERS:
        importlib.import_module(dotted)


_import_carved_owners()

#: The names moved to the renderers and the mount sweep, by owner. They owned these
#: names before the carve, so each name is read from, written to and deleted on its one
#: owner, and a write through this module never reaches another module.
_FORWARDS_BY_OWNER: dict[str, tuple[str, ...]] = {
    "kiro_crew.sandbox_seatbelt": ("_SEATBELT_PROFILE",),
    "kiro_crew.sandbox_mount_sweep": (
        "_MOUNT_SOURCE_PREFIX",
        "_MOUNT_SOURCE_MAX_AGE_SECONDS",
        "_PIN_SCAN_MAX_PASSES",
        "_MOUNT_TABLE_CACHE_MAX_ENTRIES",
        "_MOUNT_TABLE_CACHE_MAX_BYTES",
        "_PinScanCoverage",
        "_task_uid",
        "_OVERFLOW_UID_SYSCTL",
        "_overflow_uid",
        "_parse_pid_segment",
        "_mount_source_candidate_roots",
        "_mount_pinned_source_names",
        "_SWEEP_TIME_BUDGET_SECONDS",
        "_SWEEP_BUDGET_CHECK_EVERY",
        "_cleanup_stale_sandbox_mount_sources",
        "_LEGACY_MOUNT_SOURCE_RE",
        "_LEGACY_PILE_THRESHOLD",
        "_LEGACY_RESIDUE_MARKER",
        "_launcher_tmpfs_roots",
        "_bound_source_basenames",
        "_cleanup_legacy_mount_source_residue",
        "_cleanup_retired_acp_snapshot_dir",
    ),
}


def _index_forwards() -> dict[str, str]:
    """Invert the renderers' and the sweep's table into forwarded name -> owner."""
    return {name: owner for owner, names in _FORWARDS_BY_OWNER.items() for name in names}


_is_policy_cache_dir = sandbox_plan.is_policy_cache_dir


_hidden_path_contains_visible_path = sandbox_plan.hidden_path_contains_visible_path


_path_within = sandbox_plan.path_within


_agent_scrub_prefixes = sandbox_plan.scrub_prefixes


#: Forwarded name -> the dotted NAME of its owner, never the module object: the owner
#: is read from :data:`sys.modules` on each use, so a module purged and imported again
#: is seen at once instead of this table forwarding to the old copy. The carved names
#: come from the mirror's index, the renderers' and the sweep's from their table.
_EXPORTS: dict[str, str] = {}
_MIRROR = _facade_mirror.install(
    _sys.modules[__name__], _CARVED_OWNERS, excluded=(), forwards=_index_forwards()
)
_EXPORTS.update(_MIRROR.served)


def _index_by_owner() -> dict[str, tuple[str, ...]]:
    """Group the forwarded names by their owner."""
    grouped: dict[str, list[str]] = {}
    for name, owner in _EXPORTS.items():
        grouped.setdefault(owner, []).append(name)
    return {owner: tuple(sorted(names)) for owner, names in grouped.items()}


#: Owner module -> every name this module forwards to it.
_EXPORTS_BY_OWNER: dict[str, tuple[str, ...]] = _index_by_owner()


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))


# Installed last, so the forwarding is live for every caller but never runs while this
# module is still binding its own names.
sys.modules[__name__].__class__ = _facade_mirror.MirroredModule

# ``from kiro_crew.sandbox import *`` binds only the names this list holds: nothing can
# enumerate what ``__getattr__`` would serve, so a forwarded name reaches a star
# importer only by being listed here. The list is DERIVED from what this module binds
# plus the forwarding table, so it is not a third list of names to keep in step. The
# bindings only the forwarding needs stay out of it, so a star import binds what it
# bound before the owners existed.
_NOT_EXPORTED = frozenset(
    {
        "ModuleType",
        "importlib",
        "sandbox_launcher",
        "sandbox_mount_sweep",
        "sandbox_plan",
        "sandbox_seatbelt",
    }
)
__all__ = sorted(
    name
    for name in set(globals()) | set(_EXPORTS)
    if not name.startswith("_") and name not in _NOT_EXPORTED
)

if TYPE_CHECKING:  # the forwarded names, visible to type checkers and IDEs
    from kiro_crew.sandbox_aliases import (  # noqa: F401
        _ALIAS_WALK_MAX_ENTRIES,
        _CREW_ALIAS_CHAIN_DEGRADE_LEAVES,
        _CREW_ALIAS_TOLERATED_LEAVES,
        _CREW_HARDLINK_REFUSED_LEAVES,
        _AliasMask,
        _first_linked_component_below,
        _inode_aliases_under,
        _InodeAliases,
        _masked_crew_home_roots,
        _masked_leaf_alias_search_hint,
        _masked_leaf_multilink_detail,
        _masked_leaf_pathonly_alias_detail,
        _referent_identity,
        _refuse_aliased_masked_leaves,
        _refuse_multilinked_credential_leaves,
        masked_credential_leaf_aliases,
    )
    from kiro_crew.sandbox_argv import (  # noqa: F401
        _DSH_GATE_ARTIFACT_PREFIX,
        _DSH_GATE_ARTIFACT_SUFFIXES,
        _DSH_PATCH_ARTIFACT_PREFIX,
        _ENV_BINARY_CANDIDATES,
        _LAUNCHER_INTERPRETER_FLAGS,
        _LAUNCHER_MAX_AGE_SECONDS,
        _LAUNCHER_SCRIPT_SUFFIX,
        _LEGACY_LAUNCHER_DIR,
        _PI_GATE_ARTIFACT_PREFIX,
        _PI_GATE_ARTIFACT_SUFFIXES,
        _PI_GATE_DIR_ARTIFACTS,
        _RUN_DIR_ARTIFACTS,
        _SANDBOX_ARTIFACT_PREFIX,
        _SEATBELT_PROFILE_SUFFIX,
        _build_launcher_script,
        _build_seatbelt_profile,
        _carveout_probes,
        _ensure_run_dir,
        _launcher_script_of,
        _live_plan_host,
        _log_refusals,
        _pinned_env_bin,
        _plan_cwd,
        _resolve_agent_executable,
        _sandbox_env_scrub_keys,
        _sandbox_env_unset_args,
        _spawn_plan,
        _ssh_supports_accept_new,
        _unset_env_argv,
        cleanup_stale_sandbox_profiles,
        namespace_argv,
        namespace_launcher_script_dir,
        sandbox_exec_argv,
        sandbox_may_hide_path,
    )
    from kiro_crew.sandbox_cgroup import (  # noqa: F401
        _CGROUP_AGENTS_SLICE,
        _CGROUP_DEFAULT_CPU_WEIGHT,
        _CGROUP_DEFAULT_MAX_PROCESSES,
        _CGROUP_FALLBACK_MAX_MEMORY_MB,
        _CGROUP_LINGER_REMEDY,
        _CGROUP_MEMORY_FRACTION,
        _CGROUP_SCOPE_PROBE,
        _CGROUP_SCOPE_PROBE_AT,
        _CGROUP_SCOPE_PROBE_LOCK,
        _CGROUP_SCOPE_PROBE_SESSION,
        _CGROUP_SCOPE_PROBE_TTL_SECONDS,
        _CGROUP_WARNED,
        _CPU_DELEGATED,
        _agents_slice_name,
        _cgroup_limits_from_config,
        _cgroup_scope_session_fingerprint,
        _compute_cgroup_scope_probe,
        _cpu_controller_delegated,
        _default_max_memory_mb,
        _instance_slice_token,
        _log_cgroup_scope_transition,
        _probe_cgroup_scope,
        _stat_identity,
        _user_bus_reachable,
        _user_bus_socket_paths,
        _user_slice_controllers_path,
        _warn_cgroup_unavailable,
    )
    from kiro_crew.sandbox_delegation import (  # noqa: F401
        _KIRO_INTERNAL_SANDBOX_KEY,
        _KIRO_INTERNAL_SETTINGS_PATH,
        SANDBOX_LAYER_CREW,
        SANDBOX_LAYER_HARNESS,
        _command_log_label,
        _delegate_to_kiro_internal_sandbox,
        _kiro_delegation_warned,
        _spawns_kiro_cli,
        delegated_workspace_exposes_sealed_target,
        kiro_internal_sandbox_enabled,
        kiro_internal_sandbox_switch,
        sandbox_init_remediation,
        wrapped_by_crew_sandbox,
    )
    from kiro_crew.sandbox_leaves import (  # noqa: F401
        _AUTH_STORE_STAGING_LEAF,
        _CREW_CHILD_READABLE_LEAVES,
        _CREW_CHILD_WITHHELD_LEAVES,
        _CREW_HIDDEN_DIRS,
        _CREW_HIDDEN_LEAVES,
        _CREW_HOME_DEFAULT,
        _CREW_HOME_PREFIXES,
        _CREW_READONLY_LEAVES,
        _CREW_READONLY_TARGETS,
        _CREW_SANDBOX_VISIBLE_LEAVES,
        _CREW_UNREADABLE_MASK_LEAVES,
        _LIVE_TARGET_LEAF,
        _LIVE_TARGET_STAGING_LEAF,
        _MD_NOTEBOOK_STAGING_LEAF,
        _MD_NOTEBOOK_STATE_LEAVES,
        _POLICY_CACHE_LEAF,
        _RELOCATED_CREW_HIDDEN_LEAVES,
        _VOICE_RUNTIME_LEAF,
        MD_NOTEBOOK_APP_NAME,
        _crew_home_entries,
        crew_host_runtime_leaves,
    )
    from kiro_crew.sandbox_mount_sweep import (  # noqa: F401
        _LEGACY_MOUNT_SOURCE_RE,
        _LEGACY_PILE_THRESHOLD,
        _LEGACY_RESIDUE_MARKER,
        _MOUNT_SOURCE_MAX_AGE_SECONDS,
        _MOUNT_SOURCE_PREFIX,
        _MOUNT_TABLE_CACHE_MAX_BYTES,
        _MOUNT_TABLE_CACHE_MAX_ENTRIES,
        _OVERFLOW_UID_SYSCTL,
        _PIN_SCAN_MAX_PASSES,
        _SWEEP_BUDGET_CHECK_EVERY,
        _SWEEP_TIME_BUDGET_SECONDS,
        _bound_source_basenames,
        _cleanup_legacy_mount_source_residue,
        _cleanup_retired_acp_snapshot_dir,
        _cleanup_stale_sandbox_mount_sources,
        _launcher_tmpfs_roots,
        _mount_pinned_source_names,
        _mount_source_candidate_roots,
        _overflow_uid,
        _parse_pid_segment,
        _PinScanCoverage,
        _task_uid,
    )
    from kiro_crew.sandbox_precreate import (  # noqa: F401
        _CEILING_TEMP_PREFIX,
        _CREW_NOFOLLOW_READONLY_DIR_LEAVES,
        _CREW_NOFOLLOW_READONLY_FILE_LEAVES,
        _CREW_PRECREATE_HIDDEN_DIR_LEAVES,
        _CREW_PRECREATE_READONLY_DIR_LEAVES,
        _CREW_PRECREATE_READONLY_FILE_LEAVES,
        _DELEGATED_OVERLAP_LEAF_REASONS,
        _EMPTY_CEILING_DOCUMENT,
        _LIVE_TARGET_PRECREATE_CONTENT,
        _MD_NOTEBOOK_PRECREATE_CONTENT,
    )
    from kiro_crew.sandbox_probe import (  # noqa: F401
        _CLONE_NEWNS,
        _CLONE_NEWUSER,
        _LINUX_REMEDY_GUIDANCE,
        _PROBE_CHILD_FD_SWEEP_CAP,
        _PROBE_CHILD_GONE_ERRNOS,
        _PROBE_HANDSHAKE_TIMEOUT_SECS,
        _PROBE_MS_PRIVATE,
        _PROBE_MS_REC,
        _PROBE_MULTITHREADED_REASON,
        _PROBE_SHIM_CODE,
        _PROBE_SPAWN_TIMEOUT_SECONDS,
        _PROBE_STEP_MOUNT_PRIVATE,
        _PROBE_STEP_MULTITHREADED,
        _PROBE_STEP_NEWNS,
        _PROBE_STEP_NEWUSER,
        _PROBE_TRANSIENT_RETRY_DELAY_SECS,
        _SANDBOX_BACKEND_PROBE_TIMEOUT_SECS,
        _TRANSIENT_PROBE_ERRNOS,
        _WARM_JOIN_TIMEOUT_SECS,
        REMEDY_APPARMOR_USERNS,
        REMEDY_MAX_USER_NAMESPACES,
        REMEDY_MOUNT_DENIED,
        REMEDY_NO_USER_NS,
        REMEDY_USERNS_DENIED,
        _close_fd_ranges,
        _close_probe_fds,
        _fd_sweep_ranges,
        _inside_kirocrew_sandbox,
        _inside_macos_sandbox,
        _linux_remedy_guidance,
        _macos_sandbox_state,
        _probe_bind_libc,
        _probe_child_death,
        _probe_child_make_private,
        _probe_child_sequence,
        _probe_child_thread_count,
        _probe_child_unshare,
        _probe_failure,
        _probe_harness_failure,
        _probe_parent_sequence,
        _probe_read_step,
        _probe_reap,
        _probe_reason_is_multithreaded_collapse,
        _probe_sandbox_exec,
        _probe_spawn_unavailable_logged,
        _probe_spawned_death,
        _probe_unshare_once,
        _probe_unshare_via_fork,
        _probe_unshare_via_spawn,
        _probe_write_identity_maps,
        _remedy_for_step,
        is_docker_container,
        is_wsl,
    )
    from kiro_crew.sandbox_rlimit import (  # noqa: F401
        _BUILD_NOFILE_CEILING,
        _BUILD_RESOURCE_PREEXEC,
        _EXTRACTOR_MAX_AS_BYTES,
        _EXTRACTOR_MAX_CPU_SECS,
        _EXTRACTOR_MAX_NOFILE,
        _EXTRACTOR_PREEXEC,
        _INTERPRETER_ENOENT_DELAYS,
        _PROFILE_OOM_BIAS,
        _RESOURCE_PREEXEC,
        _RESOURCE_SUPERVISOR_ARGV,
        _SESSION_HOST_PREEXEC,
        _SHIM_ARGV_CACHE,
        _SHIM_ARGV_SEPARATOR,
        _SHIM_CHDIR_FD_FLAG,
        _SHIM_CTTY_FD_FLAG,
        _SHIM_UNAVAILABLE_LOGGED,
        _SPAWN_SHIM_CODE,
        _SPAWN_SHIM_SOURCE,
        _UNSET,
        RLIMIT_PROFILE_BUILD,
        RLIMIT_PROFILE_EXTRACTOR,
        RLIMIT_PROFILE_NONE,
        RLIMIT_PROFILE_SESSION_HOST,
        RLIMIT_PROFILE_TOOL,
        _is_transient_interpreter_enoent,
        _needs_path_search,
        _pass_fds_including,
        _pinned_spawn_path,
        _preexec_for_profile,
        _prepare_limited_spawn,
        _resolve_spawn_target,
        _retry_interpreter_enoent,
        _rlimit_spec,
        _shim_prefix_entering_fd,
        build_resource_limit_preexec,
        create_subprocess_limited,
        extractor_resource_limit_preexec,
        popen_limited,
        resource_limit_preexec,
        resource_limit_supervisor_argv,
        run_limited,
        session_host_preexec,
        spawn_shim_argv,
    )
    from kiro_crew.sandbox_seals import (  # noqa: F401
        _CREW_NO_ALIAS_LEAVES,
        SandboxCeilingUnsealable,
        _delete_file_command,
        _is_link_or_junction,
        _materialize_sealable_ceilings,
        _materialize_sealable_parent_dirs,
        _mint_gateway_id_before_seal,
        _note_established,
        _publish_empty_ceiling,
        _refuse_if_aliased_protected_leaf,
        _refuse_if_dangling_symlink,
        _refuse_if_symlink_leaf,
        _refuse_linked_models_leaf,
        _require_real_dir_nofollow,
        _require_real_file_nofollow,
        _sealable_absent_ceilings,
        _symlink_target_display,
        _warn_aliased_strict_leaves,
        _warn_if_alias_backed,
        _warn_unsealed_ceiling,
        _warn_unsealed_ceiling_parent_missing,
        require_real_model_path,
        require_unaliased_cloud_config,
        require_unaliased_launch_state,
        require_unaliased_model_file,
    )
    from kiro_crew.sandbox_seatbelt import _SEATBELT_PROFILE  # noqa: F401
    from kiro_crew.sandbox_slice import (  # noqa: F401
        _CGROUP_DEFAULT_MAX_TOTAL_TASKS,
        _CGROUP_FALLBACK_MAX_TOTAL_MEMORY_MB,
        _CGROUP_TOTAL_MEMORY_FRACTION,
        _SCOPE_UNIT_PREFIX,
        _SCOPE_UNIT_TOKEN_SAFE,
        _SLICE_FALLBACK_MEMORY_HIGH_MB,
        _SLICE_LIMITS_APPLIED,
        _SLICE_MEMHIGH_APPLIED,
        _SLICE_MEMHIGH_CLIMB_WARNED,
        _SLICE_MEMHIGH_DISABLED,
        _SLICE_MEMHIGH_EVENTS_SEEN,
        _SLICE_MEMHIGH_MUTEX,
        _SLICE_MEMORY_HIGH_FRACTION,
        _SLICE_OOM_SEEN,
        _SLICE_THROTTLE_EDGE_AT,
        _SLICE_THROTTLE_EDGE_HOLD_SECS,
        _SLICE_THROTTLE_PROBE_SEEN,
        _USER_MANAGER_CGROUP_BASE,
        _WINDOWS_INERT_LIMITS_WARNED,
        _WINDOWS_INERT_RESOURCE_LIMIT_KEYS,
        _agents_slice_cgroup_dir,
        _check_slice_memory_pressure,
        _default_max_total_memory_mb,
        _default_slice_memory_high_mb,
        _ensure_agent_slice_memory_high,
        _read_cgroup_counters,
        _reconcile_slice_memory_high_off_thread,
        _slice_limits_from_config,
        _slice_memory_events_high,
        _warn_windows_inert_resource_limits_once,
        agents_slice_throttling,
        apply_windows_resource_ceiling,
        cgroup_scope_argv,
        check_agents_slice_pressure,
        ensure_agents_slice_limits,
        name_scope_unit,
        read_cgroup_int,
        scope_unit_name,
    )
    from kiro_crew.sandbox_spawn import (  # noqa: F401
        _CGROUP_SCOPE_BUS_ENV_KEYS,
        _SPAWN_SCRUB_ENV_PREFIXES,
        agent_env_scrub_prefixes,
        cgroup_scope_bus_env,
        corroborate_launcher_refusal,
        credential_mask_applies,
        sandboxed_spawn_argv,
        sandboxed_spawn_argv_async,
        scrub_agent_denied_env,
        scrub_agent_subprocess_env,
        scrub_env,
        shielded_prepare_off_loop,
        spawn_delegates_masking,
        unconfined_live_agent_pid,
        wrap_argv_async,
    )
    from kiro_crew.sandbox_staging import (  # noqa: F401
        _AUTH_STORE_LEGACY_TEMP_PREFIX,
        _AUTH_STORE_PUBLISHED_KEY_LEAF,
        _MD_NOTEBOOK_STATE_COMPONENTS,
        LiveTargetUnfitness,
        _live_target_irregular_detail,
        _live_target_multilink_detail,
        _live_target_symlink_detail,
        _materialize_live_target_mask_target,
        _materialize_maskable_dirs,
        _materialize_md_notebook_mask_targets,
        _md_notebook_degraded_mask_dirs,
        _open_dir_anchored,
        _reconcile_auth_store_staging_links,
        _refuse_if_live_target_symlink,
        _refuse_unless_sole_regular_link,
        _sweep_legacy_auth_store_temps,
        _sweep_legacy_md_notebook_temps,
        _sweep_one_md_notebook_state_dir,
        live_target_pointer_unfitness,
    )
    from kiro_crew.sandbox_tiers import (  # noqa: F401
        _AGENT_DENIED_ENV_KEYS,
        _APP_BACKEND_OWNED_LEAVES,
        _CC_DIRS,
        _CC_EXPOSE_FILES,
        _CC_FILES,
        _IN_SANDBOX_LEVEL_VAR,
        _IN_SANDBOX_MARKER,
        _PACKAGED_INSTALL_DIR_NAMES,
        _POD_OS_HOME_GRANT_STORE_LEAVES,
        _POD_OS_HOME_MASKED_SUBLEAVES,
        _PYTHON_ENV_PREFIXES,
        _SANDBOX_MODE_ALIASES,
        _SANDBOX_MODE_FALLBACK,
        _SENSITIVE_ENV_PREFIXES,
        _STANDARD_DIRS,
        _STRICT_DIRS,
        _TIER_ORDINALS,
        _clamp_sandbox_mode,
        _clamp_sandbox_mode_to_floor,
        _crew_home_alias_roots,
        _floor_mandates_sandbox,
        _gateway_package_dir,
        _governance_sandbox_floor,
        _install_root_targets_cached,
        _install_root_targets_with,
        _mode_to_level,
        _pod_os_home_targets,
        _relocated_crew_targets,
        _relocated_policy_cache_dirs,
        _resolved_install_root_targets,
        _resolved_kiro_agents_targets,
        _resolves_to,
        _sandbox_policy,
        app_backend_visible_targets,
        carveout_chain_has_planted_link,
        carveout_shadowed_by_foreign_mask,
        configured_sandbox_mode,
        crew_home_visible_spellings,
        effective_sandbox_mode,
        install_root_targets_for,
    )
    from kiro_crew.sandbox_unavailable import (  # noqa: F401
        _LAUNCHER_REFUSAL_RE,
        _SSH_VISIBLE_TIERS,
        _TRUSTED_LAUNCHER_TIMEOUT_SECS,
        CRON_SCRIPT_CHILD_ENV,
        LAUNCHER_EXIT_PREFIXES,
        UNSANDBOXED_BY_OPERATOR,
        UNSANDBOXED_BY_PLATFORM,
        SandboxUnavailableError,
        UnauditedSpawnRefused,
        _allow_no_isolation,
        _allow_unsandboxed_exec,
        _apparmor_userns_restricted,
        _bundled_cli_invocation,
        _classify_unavailable,
        _first_party_no_backend_passthrough,
        _forward_for_effective_tier,
        _forward_ssh_auth_sock,
        _no_backend_guidance,
        _ssh_dir_masked,
        _tier_lists_ssh,
        _unsandboxed_exec_key_declared,
        _unsandboxed_grant_source,
        _warn_first_party_unconfined_once,
        _warn_mode_off_unconfined,
        agent_confinement_evidence,
        launcher_refusal,
        refuse_unaudited_on_dead_fs,
        unsandboxed_exec_permitted_by,
    )
    from kiro_crew.sandbox_workspace import (  # noqa: F401
        _VOICE_GUARD_REMEDY,
        CANONICAL_TEMP_KEYS,
        DECLARED_TEMP_REFUSAL_REASONS,
        BoundWorkspaceMismatch,
        DeclaredTempRefusal,
        _bound_agent_workspace_matches,
        _close_bound_agent_workspace,
        _directory_ancestor_identities,
        _ensure_voice_runtime_directory,
        _identity_within_sealed_parent,
        _lexical_runtime_overlap,
        _literal_ancestor_guards,
        _open_directory_descriptor,
        _path_identity,
        _repr_declared_temp_log_value,
        _restore_missing_voice_runtime_roots,
        _voice_runtime_ancestor_guards,
        _voice_runtime_guard_message,
        _voice_runtime_parent_paths,
        _voice_runtime_paths_cache,
        _voice_runtime_paths_lock,
        _voice_runtime_sandbox_paths,
        _VoiceGuardRelationship,
        assert_voice_runtime_outside_agent_workspace,
        bind_voice_safe_agent_workspace,
        bind_voice_safe_agent_workspace_async,
        bound_agent_workspace_target,
        classify_declared_temp_env,
        classify_declared_temp_path,
        declared_temp_refusal_reasons,
        format_declared_temp_refusals,
        prime_voice_runtime_sandbox_paths,
        release_bound_agent_workspace,
        resolve_bound_session_workspace,
        voice_runtime_workspace_conflict,
    )
    from kiro_crew.sandbox_wrap import (  # noqa: F401
        _backend,
        _background_warm,
        _kick_background_warm,
        _last_unshare_failure,
        _probe_unshare,
        _record_probe_failure,
        _warm_thread,
        _warn_no_isolation,
        detect_backend,
        prewarm_backend,
        remedy_guidance,
        reset_backend,
        unavailable_kind,
        unavailable_reason,
        unavailable_remedy,
        userns_available,
        warm_backend,
        wrap_argv,
    )
