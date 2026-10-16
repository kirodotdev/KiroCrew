"""``kiro_crew.sandbox`` stays the sandbox's import and patch surface after the split.

The Linux launcher renderer, the macOS Seatbelt renderer and the stale mount-source sweep
are defined in ``sandbox_launcher``, ``sandbox_seatbelt`` and ``sandbox_mount_sweep``.
The facade keeps every name moved to those owners readable under its old path, and
FORWARDS every one of them, so a patch through the facade lands on the owner, where the
owner's own callers read it -- whether or not a scan could read the patched name off the
test. The path rules moved to ``kiro_crew.sandbox_plan`` are bound on the facade as plain
compatibility aliases instead, pinned by identity below; a patch of one does not reach
the planner. The two
builders live in ``sandbox_argv``: they gather the live host (``_live_plan_host``), plan the
spawn (``kiro_crew.sandbox_plan``) and hand the plan to the owner's renderer, so a test that
rebinds a tier list or a host-fact helper through the facade reaches the plan through the
mirror, and no owner imports the facade at all.

The rest of what sandbox.py defined is carved into the owners in ``_CARVED``, which import
each other lowest layer first (``_CARVED_IMPORTS``) and never the facade. Their code read
one namespace before the carve, so the facade MIRRORS a write through it
(``kiro_crew._facade_mirror``): it lands on every carved owner that binds the same object,
and on the facade when the facade binds the name too. The rows below pin that the mirror
indexes exactly the carved owners, that a patch reaches every holder and is undone on each,
and that the renderers and the mount sweep stay out of it. ``test/test_facade_mirror_guards.py``
holds every mirrored facade, this one included, to the mirror's own rules: one object per
indexed name, no owner reading an indexed name it does not hold, no member rebinding a
shared global.

This file also holds what both facades share -- ``kiro_crew.sandbox`` and
``kiro_crew.platform_compat`` -- so ``test_platform_compat_refactor_facade`` runs the same
checks on its facade: the static shape of a facade module, the owner-import scan, and the
census of every facade patch in the test trees, which is read once for both facades, in
``CENSUS_PARTS`` size-balanced parts so that no single test walks the whole tree.

The seam rows patch ONE name with a stub that raises ``_Reached`` and then drive a
function that must read that name. ``_Reached`` derives from ``BaseException`` on
purpose: several of these helpers are best-effort and swallow ``Exception``, and a stub
such a handler could swallow would let a patch that MISSED read as one that landed.
"""

from __future__ import annotations

import ast
import copy
import functools
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import logging
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Callable
from unittest import mock

import pytest
import test_sandbox_refactor_create_guard as create_guard

from kiro_crew import (
    _facade_mirror,
    platform_compat,
    sandbox,
    sandbox_aliases,
    sandbox_argv,
    sandbox_cgroup,
    sandbox_delegation,
    sandbox_launcher,
    sandbox_leaves,
    sandbox_mount_sweep,
    sandbox_plan,
    sandbox_precreate,
    sandbox_probe,
    sandbox_rlimit,
    sandbox_seals,
    sandbox_seatbelt,
    sandbox_slice,
    sandbox_spawn,
    sandbox_staging,
    sandbox_tiers,
    sandbox_unavailable,
    sandbox_workspace,
    sandbox_wrap,
)

# The patch census parses every test file once; keep it on one worker.
pytestmark = pytest.mark.xdist_group(name="tree_scan_sandbox_refactor_facade")

#: The rows that drive a sandbox backend or its sweep: the OS sandbox is POSIX-only, as
#: the Windows collect-ignore list records for the suites these rows sit beside.
_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="the OS sandbox is POSIX-only")

_SRC = Path(sandbox.__file__).resolve().parent
_REPO = _SRC.parents[1]

#: The owners carved out of sandbox.py, lowest layer first, as the facade imports them.
_CARVED: dict[str, ModuleType] = {
    m.__name__: m
    for m in (
        sandbox_leaves,
        sandbox_tiers,
        sandbox_precreate,
        sandbox_seals,
        sandbox_aliases,
        sandbox_staging,
        sandbox_workspace,
        sandbox_argv,
        sandbox_delegation,
        sandbox_probe,
        sandbox_unavailable,
        sandbox_wrap,
        sandbox_cgroup,
        sandbox_slice,
        sandbox_rlimit,
        sandbox_spawn,
    )
}

_OWNERS: dict[str, ModuleType] = {
    **{m.__name__: m for m in (sandbox_launcher, sandbox_seatbelt, sandbox_mount_sweep)},
    **_CARVED,
}

#: Every module-level name the split moved out of sandbox.py, by its owner. The two
#: builders that plan a spawn and hand the plan to the renderers live in
#: ``sandbox_argv``.
_MOVED: dict[str, tuple[str, ...]] = {
    "kiro_crew.sandbox_launcher": (),
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
    "kiro_crew.sandbox_leaves": tuple("""
        _POLICY_CACHE_LEAF _VOICE_RUNTIME_LEAF _CREW_HOME_DEFAULT _CREW_HOME_PREFIXES
        _LIVE_TARGET_LEAF _LIVE_TARGET_STAGING_LEAF _AUTH_STORE_STAGING_LEAF MD_NOTEBOOK_APP_NAME
        _MD_NOTEBOOK_STATE_LEAVES _MD_NOTEBOOK_STAGING_LEAF _CREW_HIDDEN_LEAVES
        _CREW_UNREADABLE_MASK_LEAVES _RELOCATED_CREW_HIDDEN_LEAVES _CREW_READONLY_LEAVES
        _CREW_SANDBOX_VISIBLE_LEAVES _crew_home_entries _CREW_HIDDEN_DIRS _CREW_READONLY_TARGETS
        _CREW_CHILD_WITHHELD_LEAVES _CREW_CHILD_READABLE_LEAVES crew_host_runtime_leaves
        """.split()),
    "kiro_crew.sandbox_tiers": tuple("""
        _resolved_kiro_agents_targets _PACKAGED_INSTALL_DIR_NAMES install_root_targets_for
        _install_root_targets_with _install_root_targets_cached _resolved_install_root_targets
        _gateway_package_dir _APP_BACKEND_OWNED_LEAVES app_backend_visible_targets
        crew_home_visible_spellings carveout_chain_has_planted_link
        carveout_shadowed_by_foreign_mask _STRICT_DIRS _STANDARD_DIRS _CC_DIRS _resolves_to
        _crew_home_alias_roots _relocated_crew_targets _POD_OS_HOME_GRANT_STORE_LEAVES
        _POD_OS_HOME_MASKED_SUBLEAVES _pod_os_home_targets _relocated_policy_cache_dirs
        _CC_EXPOSE_FILES _CC_FILES _SENSITIVE_ENV_PREFIXES _PYTHON_ENV_PREFIXES
        _AGENT_DENIED_ENV_KEYS _sandbox_policy _SANDBOX_MODE_FALLBACK configured_sandbox_mode
        _IN_SANDBOX_MARKER _IN_SANDBOX_LEVEL_VAR _TIER_ORDINALS _mode_to_level _SANDBOX_MODE_ALIASES
        effective_sandbox_mode _governance_sandbox_floor _clamp_sandbox_mode _floor_mandates_sandbox
        _clamp_sandbox_mode_to_floor
        """.split()),
    "kiro_crew.sandbox_precreate": tuple("""
        _CREW_PRECREATE_READONLY_DIR_LEAVES _CREW_NOFOLLOW_READONLY_DIR_LEAVES
        _CREW_NOFOLLOW_READONLY_FILE_LEAVES _DELEGATED_OVERLAP_LEAF_REASONS
        _CREW_PRECREATE_READONLY_FILE_LEAVES _CREW_PRECREATE_HIDDEN_DIR_LEAVES
        _MD_NOTEBOOK_PRECREATE_CONTENT _LIVE_TARGET_PRECREATE_CONTENT _EMPTY_CEILING_DOCUMENT
        _CEILING_TEMP_PREFIX
        """.split()),
    "kiro_crew.sandbox_seals": tuple("""
        _sealable_absent_ceilings SandboxCeilingUnsealable _warn_unsealed_ceiling
        _CREW_NO_ALIAS_LEAVES _symlink_target_display _refuse_if_aliased_protected_leaf
        _warn_unsealed_ceiling_parent_missing _warn_if_alias_backed _refuse_if_dangling_symlink
        _refuse_if_symlink_leaf _is_link_or_junction _refuse_linked_models_leaf
        require_real_model_path _require_real_dir_nofollow _require_real_file_nofollow
        _publish_empty_ceiling _materialize_sealable_parent_dirs _note_established
        _mint_gateway_id_before_seal _materialize_sealable_ceilings _warn_aliased_strict_leaves
        require_unaliased_cloud_config _delete_file_command require_unaliased_launch_state
        require_unaliased_model_file
        """.split()),
    "kiro_crew.sandbox_aliases": tuple("""
        _CREW_ALIAS_TOLERATED_LEAVES _CREW_ALIAS_CHAIN_DEGRADE_LEAVES _CREW_HARDLINK_REFUSED_LEAVES
        _first_linked_component_below _masked_crew_home_roots _ALIAS_WALK_MAX_ENTRIES _AliasMask
        _InodeAliases _inode_aliases_under _refuse_multilinked_credential_leaves _referent_identity
        _refuse_aliased_masked_leaves _masked_leaf_pathonly_alias_detail
        _masked_leaf_multilink_detail _masked_leaf_alias_search_hint masked_credential_leaf_aliases
        """.split()),
    "kiro_crew.sandbox_staging": tuple("""
        _materialize_maskable_dirs _materialize_live_target_mask_target
        _refuse_if_live_target_symlink LiveTargetUnfitness _live_target_irregular_detail
        _live_target_multilink_detail _live_target_symlink_detail _refuse_unless_sole_regular_link
        live_target_pointer_unfitness _md_notebook_degraded_mask_dirs
        _sweep_legacy_md_notebook_temps _MD_NOTEBOOK_STATE_COMPONENTS _open_dir_anchored
        _AUTH_STORE_PUBLISHED_KEY_LEAF _AUTH_STORE_LEGACY_TEMP_PREFIX
        _reconcile_auth_store_staging_links _sweep_legacy_auth_store_temps
        _sweep_one_md_notebook_state_dir _materialize_md_notebook_mask_targets
        """.split()),
    "kiro_crew.sandbox_workspace": tuple("""
        _voice_runtime_paths_lock _voice_runtime_paths_cache _ensure_voice_runtime_directory
        _literal_ancestor_guards prime_voice_runtime_sandbox_paths
        _restore_missing_voice_runtime_roots _voice_runtime_sandbox_paths
        _voice_runtime_parent_paths _voice_runtime_ancestor_guards _path_identity
        _identity_within_sealed_parent CANONICAL_TEMP_KEYS DECLARED_TEMP_REFUSAL_REASONS
        DeclaredTempRefusal classify_declared_temp_path classify_declared_temp_env
        _repr_declared_temp_log_value declared_temp_refusal_reasons format_declared_temp_refusals
        _VOICE_GUARD_REMEDY _VoiceGuardRelationship _voice_runtime_guard_message
        _lexical_runtime_overlap voice_runtime_workspace_conflict
        assert_voice_runtime_outside_agent_workspace _open_directory_descriptor
        _directory_ancestor_identities bind_voice_safe_agent_workspace _close_bound_agent_workspace
        release_bound_agent_workspace bind_voice_safe_agent_workspace_async
        _bound_agent_workspace_matches bound_agent_workspace_target BoundWorkspaceMismatch
        resolve_bound_session_workspace
        """.split()),
    "kiro_crew.sandbox_argv": tuple("""
        _LAUNCHER_MAX_AGE_SECONDS _SANDBOX_ARTIFACT_PREFIX _LAUNCHER_SCRIPT_SUFFIX
        _SEATBELT_PROFILE_SUFFIX _PI_GATE_ARTIFACT_PREFIX _PI_GATE_ARTIFACT_SUFFIXES
        _RUN_DIR_ARTIFACTS _DSH_GATE_ARTIFACT_PREFIX _DSH_PATCH_ARTIFACT_PREFIX
        _DSH_GATE_ARTIFACT_SUFFIXES _PI_GATE_DIR_ARTIFACTS _LEGACY_LAUNCHER_DIR
        _resolve_agent_executable _ssh_supports_accept_new namespace_launcher_script_dir
        _ensure_run_dir _LAUNCHER_INTERPRETER_FLAGS _launcher_script_of _log_refusals
        _carveout_probes _plan_cwd _live_plan_host sandbox_may_hide_path _spawn_plan
        _build_launcher_script _build_seatbelt_profile namespace_argv _pinned_env_bin
        sandbox_exec_argv _sandbox_env_scrub_keys _sandbox_env_unset_args
        cleanup_stale_sandbox_profiles _ENV_BINARY_CANDIDATES _unset_env_argv
        """.split()),
    "kiro_crew.sandbox_delegation": tuple("""
        _KIRO_INTERNAL_SETTINGS_PATH _KIRO_INTERNAL_SANDBOX_KEY _kiro_delegation_warned
        kiro_internal_sandbox_enabled kiro_internal_sandbox_switch SANDBOX_LAYER_CREW
        SANDBOX_LAYER_HARNESS wrapped_by_crew_sandbox sandbox_init_remediation
        delegated_workspace_exposes_sealed_target _spawns_kiro_cli
        _delegate_to_kiro_internal_sandbox _command_log_label
        """.split()),
    "kiro_crew.sandbox_probe": tuple("""
        _CLONE_NEWUSER _CLONE_NEWNS _PROBE_MS_REC _PROBE_MS_PRIVATE _TRANSIENT_PROBE_ERRNOS
        _PROBE_TRANSIENT_RETRY_DELAY_SECS _WARM_JOIN_TIMEOUT_SECS _PROBE_STEP_NEWUSER
        _PROBE_STEP_NEWNS _PROBE_STEP_MOUNT_PRIVATE _PROBE_STEP_MULTITHREADED
        _PROBE_MULTITHREADED_REASON _probe_reason_is_multithreaded_collapse
        _PROBE_CHILD_GONE_ERRNOS _PROBE_HANDSHAKE_TIMEOUT_SECS
        _SANDBOX_BACKEND_PROBE_TIMEOUT_SECS REMEDY_APPARMOR_USERNS
        REMEDY_MAX_USER_NAMESPACES REMEDY_NO_USER_NS REMEDY_USERNS_DENIED
        REMEDY_MOUNT_DENIED _remedy_for_step _LINUX_REMEDY_GUIDANCE _linux_remedy_guidance
        _close_probe_fds _PROBE_CHILD_FD_SWEEP_CAP _fd_sweep_ranges _close_fd_ranges
        _probe_failure _probe_harness_failure _probe_child_unshare _probe_child_make_private
        _probe_bind_libc _probe_child_thread_count _probe_write_identity_maps
        _probe_read_step _probe_child_death _probe_reap _probe_child_sequence
        _probe_parent_sequence _probe_unshare_once _PROBE_SHIM_CODE
        _PROBE_SPAWN_TIMEOUT_SECONDS _probe_spawn_unavailable_logged _probe_spawned_death
        _probe_unshare_via_spawn _probe_unshare_via_fork is_wsl is_docker_container
        _probe_sandbox_exec _inside_kirocrew_sandbox _macos_sandbox_state
        _inside_macos_sandbox
        """.split()),
    "kiro_crew.sandbox_unavailable": tuple("""
        _allow_no_isolation UNSANDBOXED_BY_OPERATOR UNSANDBOXED_BY_PLATFORM
        _allow_unsandboxed_exec _unsandboxed_grant_source _unsandboxed_exec_key_declared
        _SSH_VISIBLE_TIERS _ssh_dir_masked _tier_lists_ssh _forward_ssh_auth_sock
        _forward_for_effective_tier unsandboxed_exec_permitted_by _bundled_cli_invocation
        _apparmor_userns_restricted _no_backend_guidance _classify_unavailable
        agent_confinement_evidence _warn_mode_off_unconfined
        _warn_first_party_unconfined_once _first_party_no_backend_passthrough
        CRON_SCRIPT_CHILD_ENV UnauditedSpawnRefused refuse_unaudited_on_dead_fs
        SandboxUnavailableError LAUNCHER_EXIT_PREFIXES _LAUNCHER_REFUSAL_RE launcher_refusal
        _TRUSTED_LAUNCHER_TIMEOUT_SECS
        """.split()),
    "kiro_crew.sandbox_wrap": tuple("""
        _last_unshare_failure unavailable_remedy unavailable_reason remedy_guidance
        _warm_thread _record_probe_failure _background_warm _kick_background_warm
        prewarm_backend warm_backend _probe_unshare userns_available _backend
        unavailable_kind _warn_no_isolation detect_backend reset_backend wrap_argv
        """.split()),
    "kiro_crew.sandbox_cgroup": tuple("""
        _CGROUP_DEFAULT_MAX_PROCESSES _CGROUP_DEFAULT_CPU_WEIGHT _CGROUP_MEMORY_FRACTION
        _CGROUP_FALLBACK_MAX_MEMORY_MB _CGROUP_AGENTS_SLICE _instance_slice_token
        _agents_slice_name _default_max_memory_mb _CGROUP_SCOPE_PROBE
        _CGROUP_SCOPE_PROBE_SESSION _CGROUP_SCOPE_PROBE_AT _CGROUP_SCOPE_PROBE_TTL_SECONDS
        _CGROUP_SCOPE_PROBE_LOCK _CGROUP_WARNED _CGROUP_LINGER_REMEDY
        _warn_cgroup_unavailable _user_slice_controllers_path _user_bus_socket_paths
        _stat_identity _cgroup_scope_session_fingerprint _user_bus_reachable
        _probe_cgroup_scope _log_cgroup_scope_transition _compute_cgroup_scope_probe
        _CPU_DELEGATED _cpu_controller_delegated _cgroup_limits_from_config
        """.split()),
    "kiro_crew.sandbox_slice": tuple("""
        _SLICE_MEMORY_HIGH_FRACTION _SLICE_FALLBACK_MEMORY_HIGH_MB _SLICE_MEMHIGH_APPLIED
        _SLICE_MEMHIGH_DISABLED _default_slice_memory_high_mb
        _ensure_agent_slice_memory_high _SLICE_MEMHIGH_EVENTS_SEEN
        _SLICE_MEMHIGH_CLIMB_WARNED _slice_memory_events_high _check_slice_memory_pressure
        _SLICE_MEMHIGH_MUTEX _reconcile_slice_memory_high_off_thread
        _WINDOWS_INERT_RESOURCE_LIMIT_KEYS _WINDOWS_INERT_LIMITS_WARNED
        _warn_windows_inert_resource_limits_once apply_windows_resource_ceiling
        _SCOPE_UNIT_PREFIX _SCOPE_UNIT_TOKEN_SAFE scope_unit_name cgroup_scope_argv
        name_scope_unit _CGROUP_TOTAL_MEMORY_FRACTION _CGROUP_FALLBACK_MAX_TOTAL_MEMORY_MB
        _CGROUP_DEFAULT_MAX_TOTAL_TASKS _default_max_total_memory_mb
        _slice_limits_from_config _SLICE_LIMITS_APPLIED ensure_agents_slice_limits
        _USER_MANAGER_CGROUP_BASE _agents_slice_cgroup_dir _read_cgroup_counters
        read_cgroup_int _SLICE_THROTTLE_PROBE_SEEN _SLICE_THROTTLE_EDGE_AT
        _SLICE_THROTTLE_EDGE_HOLD_SECS agents_slice_throttling _SLICE_OOM_SEEN
        check_agents_slice_pressure
        """.split()),
    "kiro_crew.sandbox_rlimit": tuple("""
        _UNSET _RESOURCE_PREEXEC resource_limit_preexec _EXTRACTOR_PREEXEC
        extractor_resource_limit_preexec _RESOURCE_SUPERVISOR_ARGV
        resource_limit_supervisor_argv _SESSION_HOST_PREEXEC session_host_preexec
        _BUILD_NOFILE_CEILING _BUILD_RESOURCE_PREEXEC build_resource_limit_preexec
        _SPAWN_SHIM_SOURCE _SPAWN_SHIM_CODE RLIMIT_PROFILE_TOOL RLIMIT_PROFILE_BUILD
        RLIMIT_PROFILE_SESSION_HOST RLIMIT_PROFILE_EXTRACTOR _EXTRACTOR_MAX_AS_BYTES
        _EXTRACTOR_MAX_CPU_SECS _EXTRACTOR_MAX_NOFILE RLIMIT_PROFILE_NONE _PROFILE_OOM_BIAS
        _SHIM_ARGV_SEPARATOR _SHIM_CHDIR_FD_FLAG _SHIM_CTTY_FD_FLAG _SHIM_ARGV_CACHE
        _SHIM_UNAVAILABLE_LOGGED _rlimit_spec spawn_shim_argv _shim_prefix_entering_fd
        _pass_fds_including _preexec_for_profile _resolve_spawn_target _pinned_spawn_path
        _needs_path_search create_subprocess_limited _prepare_limited_spawn run_limited
        _INTERPRETER_ENOENT_DELAYS _is_transient_interpreter_enoent
        _retry_interpreter_enoent popen_limited
        """.split()),
    "kiro_crew.sandbox_spawn": tuple("""
        corroborate_launcher_refusal credential_mask_applies spawn_delegates_masking
        unconfined_live_agent_pid wrap_argv_async _SPAWN_SCRUB_ENV_PREFIXES scrub_env
        agent_env_scrub_prefixes scrub_agent_denied_env scrub_agent_subprocess_env
        sandboxed_spawn_argv shielded_prepare_off_loop sandboxed_spawn_argv_async
        _CGROUP_SCOPE_BUS_ENV_KEYS cgroup_scope_bus_env
        """.split()),
}

#: Moved names the facade binds again because code still in sandbox.py reads them as its
#: own globals. None: every unit that read one has moved, so each moved name is
#: forwarded, and the facade keeps only its BASE imports and the ``sandbox_plan`` aliases.
_BOUND_HERE: frozenset[str] = frozenset()

#: Carved owner -> the owners it imports. Explicit, so a new edge between owners is a
#: decision; each edge points to an owner earlier in ``sandbox._CARVED_OWNERS`` (so the
#: graph has no cycle) and is one the owner really uses, both pinned below.
_CARVED_IMPORTS: dict[str, frozenset[str]] = {
    "kiro_crew.sandbox_tiers": frozenset({"kiro_crew.sandbox_leaves"}),
    "kiro_crew.sandbox_precreate": frozenset({"kiro_crew.sandbox_leaves"}),
    "kiro_crew.sandbox_seals": frozenset({"kiro_crew.sandbox_precreate"}),
    "kiro_crew.sandbox_aliases": frozenset(
        {"kiro_crew.sandbox_leaves", "kiro_crew.sandbox_precreate", "kiro_crew.sandbox_seals"}
    ),
    "kiro_crew.sandbox_staging": frozenset(
        {
            "kiro_crew.sandbox_aliases",
            "kiro_crew.sandbox_leaves",
            "kiro_crew.sandbox_precreate",
            "kiro_crew.sandbox_seals",
            "kiro_crew.sandbox_tiers",
        }
    ),
    "kiro_crew.sandbox_workspace": frozenset({"kiro_crew.sandbox_leaves"}),
    "kiro_crew.sandbox_argv": frozenset(
        {
            "kiro_crew.sandbox_aliases",
            "kiro_crew.sandbox_launcher",
            "kiro_crew.sandbox_leaves",
            "kiro_crew.sandbox_mount_sweep",
            "kiro_crew.sandbox_seals",
            "kiro_crew.sandbox_seatbelt",
            "kiro_crew.sandbox_staging",
            "kiro_crew.sandbox_tiers",
            "kiro_crew.sandbox_workspace",
        }
    ),
    "kiro_crew.sandbox_delegation": frozenset(
        {
            "kiro_crew.sandbox_argv",
            "kiro_crew.sandbox_precreate",
            "kiro_crew.sandbox_tiers",
        }
    ),
    "kiro_crew.sandbox_probe": frozenset({"kiro_crew.sandbox_tiers"}),
    "kiro_crew.sandbox_unavailable": frozenset(
        {
            "kiro_crew.sandbox_argv",
            "kiro_crew.sandbox_delegation",
            "kiro_crew.sandbox_probe",
            "kiro_crew.sandbox_tiers",
        }
    ),
    "kiro_crew.sandbox_wrap": frozenset(
        {
            "kiro_crew.sandbox_argv",
            "kiro_crew.sandbox_delegation",
            "kiro_crew.sandbox_probe",
            "kiro_crew.sandbox_seals",
            "kiro_crew.sandbox_tiers",
            "kiro_crew.sandbox_unavailable",
        }
    ),
    "kiro_crew.sandbox_slice": frozenset({"kiro_crew.sandbox_cgroup"}),
    "kiro_crew.sandbox_rlimit": frozenset({"kiro_crew.sandbox_workspace"}),
    "kiro_crew.sandbox_spawn": frozenset(
        {
            "kiro_crew.sandbox_argv",
            "kiro_crew.sandbox_cgroup",
            "kiro_crew.sandbox_delegation",
            "kiro_crew.sandbox_probe",
            "kiro_crew.sandbox_rlimit",
            "kiro_crew.sandbox_slice",
            "kiro_crew.sandbox_tiers",
            "kiro_crew.sandbox_unavailable",
            "kiro_crew.sandbox_wrap",
        }
    ),
}

#: Every function in an owner that imports from the facade, and what it reads there.
#: None does: the renderers read only the plan they are handed, and the live host the
#: plan is made from is gathered in ``sandbox_argv``.
_SEAM_IMPORTS: dict[tuple[str, str], tuple[str, tuple[str, ...]]] = {}


class _Reached(BaseException):
    """Raised by a stub to prove the reader read the patched name."""


def _raiser(label: str) -> Callable[..., object]:
    def _stub(*_args: object, **_kwargs: object) -> object:
        raise _Reached(label)

    return _stub


# --------------------------------------------------------------------------- #
# Checks both facades run, each against its own tables.
# --------------------------------------------------------------------------- #


def _moved_rows(moved: dict[str, tuple[str, ...]]) -> list[tuple[str, str]]:
    return [(owner, name) for owner, names in moved.items() for name in names]


def check_moved_name(
    facade: ModuleType, owners: dict[str, ModuleType], owner: str, name: str
) -> None:
    """The owner defines the name, and the facade reads that very object."""
    owner_module = owners[owner]
    assert name in vars(owner_module)
    assert getattr(facade, name) is vars(owner_module)[name]


def check_forwarded_and_bound_are_disjoint(
    facade: ModuleType, moved: dict[str, tuple[str, ...]], bound: frozenset[str]
) -> None:
    assert [n for n in facade._EXPORTS if n in vars(facade)] == []
    names = {n for group in moved.values() for n in group}
    assert {n for n in names if n in vars(facade)} == set(bound)
    assert set(facade._EXPORTS) == names - bound


def check_forwarding_table(facade: ModuleType, owners: dict[str, ModuleType]) -> None:
    assert set(facade._EXPORTS) <= set(dir(facade))
    assert set(facade._EXPORTS.values()) <= set(owners)
    assert all(isinstance(owner, str) for owner in facade._EXPORTS.values())
    assert {
        name: owner for owner, names in facade._EXPORTS_BY_OWNER.items() for name in names
    } == facade._EXPORTS


def check_round_trips(facade: ModuleType, owner: ModuleType, name: str) -> None:
    """monkeypatch, mock.patch by object and by dotted name, nested either way, and a
    delete: each lands on the owner and is undone without a trace on the facade."""
    original = vars(owner)[name]
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(facade, name, "stub")
        assert vars(owner)[name] == "stub" and getattr(facade, name) == "stub"
        assert name not in vars(facade)
    assert vars(owner)[name] is original and name not in vars(facade)
    with mock.patch.object(facade, name, "outer"):
        with mock.patch(f"{facade.__name__}.{name}", "inner"):
            assert vars(owner)[name] == "inner"
        assert vars(owner)[name] == "outer"
    assert vars(owner)[name] is original
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(facade, name, "mp")
        with mock.patch.object(facade, name, "mock"):
            assert vars(owner)[name] == "mock"
        assert vars(owner)[name] == "mp"
    with mock.patch.object(facade, name, "mock"):
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(facade, name, "mp")
            assert vars(owner)[name] == "mp"
        assert vars(owner)[name] == "mock"
    assert vars(owner)[name] is original
    with pytest.MonkeyPatch.context() as patched:
        patched.delattr(facade, name)
        assert name not in vars(owner) and not hasattr(facade, name)
    assert vars(owner)[name] is original and name not in vars(facade)


def check_loaded_owner_needs_no_import(facade: ModuleType, owner: ModuleType, name: str) -> None:
    """A loaded owner answers from ``sys.modules``: reading, writing and undoing a
    forwarded name never calls the import system, so a test that stubs
    ``importlib.import_module`` for its own subject cannot reach these reads."""
    original = vars(owner)[name]
    with mock.patch.object(importlib, "import_module", side_effect=AssertionError("imported")):
        assert getattr(facade, name) is original
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(facade, name, "stub")
            assert getattr(facade, name) == "stub"
        with mock.patch.object(facade, name, "stub"):
            assert vars(owner)[name] == "stub"
    assert vars(owner)[name] is original


def check_a_purged_owner_is_imported_again(facade: ModuleType, name: str) -> None:
    """The miss path: an owner absent from ``sys.modules`` is imported afresh, and the
    facade reads the fresh copy rather than one it kept. *name* must be an object each
    import builds anew, such as a function, or the two copies cannot be told apart."""
    dotted = facade._EXPORTS[name]
    held = sys.modules[dotted]
    try:
        del sys.modules[dotted]
        fresh = getattr(facade, name)
        assert dotted in sys.modules and sys.modules[dotted] is not held
        assert fresh is vars(sys.modules[dotted])[name]
        assert fresh is not vars(held)[name]
    finally:
        sys.modules[dotted] = held
        parent, _, leaf = dotted.rpartition(".")
        setattr(sys.modules[parent], leaf, held)


def check_owners_load_with_the_facade(facade: ModuleType) -> None:
    """Importing the facade imports every owner it forwards to, before anything reads a
    forwarded name. That is what lets ``_owner`` read a loaded owner from ``sys.modules``
    without waiting on an import lock: no reader can find an owner half-built, because
    the forwarding is installed only once the facade's body -- and so every owner's --
    has finished. Measured in a child process, where nothing else has imported them."""
    owners = sorted(set(facade._EXPORTS.values()))
    code = (
        "import sys\n"
        f"import {facade.__name__}\n"
        f"owners = {owners!r}\n"
        "print(sorted(o for o in owners if o not in sys.modules))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        cwd=str(_REPO),
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "[]"


def check_unknown_name(facade: ModuleType) -> None:
    with pytest.raises(AttributeError, match="no_such_name"):
        facade.no_such_name  # noqa: B018
    assert getattr(facade, "no_such_name", None) is None


def star_import(tmp_path: Path, facade: ModuleType) -> dict[str, object]:
    """Run ``from <facade> import *`` for real, in a probe module the import machinery
    loads from a file, and return what it bound."""
    stem = f"{facade.__name__.rpartition('.')[2]}_star_probe"
    probe = tmp_path / f"{stem}.py"
    probe.write_text(f"from {facade.__name__} import *  # noqa: F401,F403\n", encoding="utf-8")
    spec = importlib.util.spec_from_file_location(stem, probe)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return vars(module)


def check_star_import(tmp_path: Path, facade: ModuleType, machinery: frozenset[str]) -> None:
    """The star import binds every public name the module held before the split: the
    names it binds now plus the forwarded ones, and nothing private. The bindings only
    the forwarding needs -- *machinery*, the owner modules among them -- stay out, so a
    star importer gains no name the split introduced."""
    assert facade._NOT_EXPORTED == machinery
    assert machinery <= set(vars(facade))
    bound = star_import(tmp_path, facade)
    public = {n for n in set(vars(facade)) | set(facade._EXPORTS) if not n.startswith("_")}
    assert facade.__all__ == sorted(public - machinery)
    assert public - machinery <= set(bound)
    assert not machinery & set(bound)
    for name in facade.__all__:
        assert bound[name] is getattr(facade, name)


def tree_of(module: ModuleType) -> ast.Module:
    return ast.parse(Path(module.__file__ or "").read_text(encoding="utf-8"))


def bare_loads(tree: ast.Module, forwarded: set[str]) -> list[tuple[int, str]]:
    """Every bare read of a forwarded name outside an import statement."""
    import_lines = {
        line
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for line in range(node.lineno, (node.end_lineno or node.lineno) + 1)
    }
    return sorted(
        (node.lineno, node.id)
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id in forwarded
        and node.lineno not in import_lines
    )


def check_static_shape(facade: ModuleType, owners: dict[str, ModuleType]) -> None:
    """No bare read of a forwarded name anywhere in the facade, ``__getattr__`` hidden
    from type checkers, and the ``TYPE_CHECKING`` imports exactly the forwarded names.

    A facade that serves through ``kiro_crew._facade_mirror`` defines no module-level
    ``__getattr__`` at all: the mirror's module class answers every forwarded name, and a
    resolver of the facade's own would answer first and shadow it."""
    tree = tree_of(facade)
    assert bare_loads(tree, set(facade._EXPORTS)) == []
    module_level = [
        *tree.body,
        *(child for n in tree.body if isinstance(n, ast.If) for child in n.body + n.orelse),
    ]
    defined = [
        n for n in module_level if isinstance(n, ast.FunctionDef) and n.name == "__getattr__"
    ]
    if _facade_mirror.INDEX_ATTRIBUTE in vars(facade):
        assert defined == [] and "__getattr__" not in vars(facade)
        assert isinstance(facade, _facade_mirror.MirroredModule)  # the resolver at run time
    else:
        hidden = [
            n
            for n in tree.body
            if isinstance(n, ast.If)
            and ast.unparse(n.test) == "not TYPE_CHECKING"
            and any(child in n.body for child in defined)
        ]
        assert len(defined) == 1 and len(hidden) == 1
        assert callable(vars(facade).get("__getattr__"))  # still the resolver at run time
    blocks = [
        n for n in tree.body if isinstance(n, ast.If) and ast.unparse(n.test) == "TYPE_CHECKING"
    ]
    typed: dict[str, str] = {}
    for block in blocks:
        for node in ast.walk(block):
            if isinstance(node, ast.ImportFrom) and node.module in owners:
                for alias in node.names:
                    typed[alias.asname or alias.name] = node.module
    assert typed == facade._EXPORTS


_MODULE_ITSELF = "<module>"


def facade_imports(
    tree: ast.Module, package: str, facades: set[str]
) -> list[tuple[str, str, tuple[str, ...]]]:
    """``(enclosing function, facade, names)`` for each import of a facade in *tree*;
    ``"<module>"`` for one at module level. An import of the facade module itself names
    ``("<module>",)``. A ``TYPE_CHECKING`` import is not one."""
    found: list[tuple[str, str, tuple[str, ...]]] = []

    def visit(node: ast.AST, where: str) -> None:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            where = node.name if where == "<module>" else f"{where}.{node.name}"
        if isinstance(node, ast.ImportFrom):
            spelled = "." * node.level + (node.module or "")
            module = importlib.util.resolve_name(spelled, package) if node.level else spelled
            if module in facades:
                found.append((where, module, tuple(a.name for a in node.names)))
            found.extend(
                (where, f"{module}.{a.name}", (_MODULE_ITSELF,))
                for a in node.names
                if f"{module}.{a.name}" in facades
            )
        if isinstance(node, ast.Import):
            found.extend(
                (where, a.name, (_MODULE_ITSELF,)) for a in node.names if a.name in facades
            )
        for child in ast.iter_child_nodes(node):
            visit(child, where)

    visit(tree, "<module>")
    return found


def owner_seams(
    owners: dict[str, ModuleType], facade: ModuleType
) -> dict[tuple[str, str], tuple[str, tuple[str, ...]]]:
    """``(owner, function) -> (facade, names)`` for every import of *facade* in its
    owners, the names of several imports in one function merged in sorted order."""
    merged: dict[tuple[str, str], set[str]] = {}
    for owner in owners.values():
        rows = facade_imports(tree_of(owner), owner.__name__.rpartition(".")[0], _FACADE_NAMES)
        for where, imported, names in rows:
            if imported != facade.__name__:
                continue
            assert where != "<module>", f"{owner.__name__} imports its facade at module level"
            merged.setdefault((owner.__name__, where), set()).update(names)
    return {site: (facade.__name__, tuple(sorted(names))) for site, names in merged.items()}


#: The facades by layer: an owner may import a LOWER facade at module level, as any other
#: module does (the mount sweep probes pids through ``platform_compat``), but never its own
#: or a higher one. A module carved out of a facade sits on its facade's layer, so an owner
#: of a lower facade cannot reach into it either.
_LAYER = {"kiro_crew.platform_compat": 0, "kiro_crew.sandbox": 1, **dict.fromkeys(_CARVED, 1)}


def owner_imports(module: ModuleType, owners: Mapping[str, ModuleType]) -> set[str]:
    """Every owner -- registered, or one of *owners* -- that *module* imports, by any
    spelling: ``from <owner> import``, ``from <package> import <owner>``, a relative
    ``from . import``, or ``import <owner>``."""
    known = {**_ALL_OWNERS, **owners}
    package = module.__name__.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(tree_of(module)):
        if isinstance(node, ast.ImportFrom):
            spelled = "." * node.level + (node.module or "")
            imported = importlib.util.resolve_name(spelled, package) if node.level else spelled
            if imported in known:
                found.add(imported)
            found.update(
                f"{imported}.{a.name}" for a in node.names if f"{imported}.{a.name}" in known
            )
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name in known)
    return found


def check_owner_dependency_direction(
    owners: dict[str, ModuleType],
    facade: ModuleType,
    allowed: Mapping[str, frozenset[str]] | None = None,
) -> None:
    """Owners are the facade's bottom layer: none imports its own facade, or a higher one,
    at module level, and none imports another owner unless *allowed* lists that edge --
    an owner carved out of the facade importing a lower one."""
    permitted = allowed or {}
    for owner in owners.values():
        tree = tree_of(owner)
        listed = permitted.get(owner.__name__, frozenset())
        rows = facade_imports(tree, owner.__name__.rpartition(".")[0], set(_LAYER))
        upward = [
            row
            for row in rows
            if row[0] == "<module>"
            and _LAYER[row[1]] >= _LAYER[facade.__name__]
            and row[1] not in listed
        ]
        assert upward == [], (owner.__name__, upward)
        unlisted = owner_imports(owner, owners) - listed
        assert unlisted == set(), (owner.__name__, sorted(unlisted))


def check_seams_are_facade_bindings(
    facade: ModuleType,
    moved: dict[str, tuple[str, ...]],
    seams: dict[tuple[str, str], tuple[str, tuple[str, ...]]],
    platform_bound: frozenset[str] = frozenset(),
) -> None:
    """Each name an owner imports from its facade is the facade's own binding -- a
    forwarded one would be read back from the owner -- and the reader's code came from
    that facade. *platform_bound* names are bound only on the platforms that have them."""
    for (owner, function), (name_of_facade, names) in seams.items():
        assert name_of_facade == facade.__name__
        assert function.split(".", 1)[0] in moved[owner]
        for name in names:
            assert name not in facade._EXPORTS, name
            assert name in vars(facade) or name in platform_bound, name
            assert name not in vars(_ALL_OWNERS[owner]), name


def check_owner_logger(facade: ModuleType, owners: dict[str, ModuleType]) -> None:
    """An owner that logs does so under the facade's name, which operator log filters,
    level settings and the tests' ``caplog`` scopes key on."""
    for owner in owners.values():
        if "logger" in vars(owner):
            assert owner.logger is logging.getLogger(facade.__name__) is facade.logger


# --------------------------------------------------------------------------- #
# The patch census: every facade patch in the test trees, read once for both facades.
# --------------------------------------------------------------------------- #

_FACADES: dict[str, ModuleType] = dict(create_guard._FACADES)
_FACADE_NAMES = set(_FACADES)
_ALL_OWNERS: dict[str, ModuleType] = {}


def register_owners(owners: dict[str, ModuleType]) -> None:
    _ALL_OWNERS.update(owners)


register_owners({name: m for name, m in _OWNERS.items() if name not in _CARVED})
register_owners(
    {
        name: importlib.import_module(name)
        for name in ("kiro_crew.platform_lock_compat", "kiro_crew.platform_owner_compat")
    }
)

_SETTERS = frozenset({"setattr", "delattr"})
_DYNAMIC = create_guard._DYNAMIC
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)
_FUNCTIONS = frozenset({ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda})
_LOOPS = frozenset({ast.For, ast.AsyncFor})
#: Nodes with no call inside and no binding: a name, a constant, an expression context.
_LEAVES = frozenset({ast.Name, ast.Constant, ast.Load, ast.Store, ast.Del})

#: Every module-level name either facade holds or forwards. A patch of a name neither
#: has cannot be a facade patch: ``monkeypatch.setattr`` and ``mock.patch`` refuse a
#: missing name unless told to create it, and every moved name is one of these.
_FACADE_ATTRIBUTES = frozenset().union(
    *(set(vars(module)) | set(module._EXPORTS) for module in _FACADES.values())
)

#: What the file's names mean at one patch site: the scope, and the names whose value
#: the file cannot say there -- a parameter, or a loop or comprehension target that
#: takes something other than a literal's elements.
_Binding = tuple[create_guard._Scope, frozenset[str]]


def _reads(node: ast.AST | None, unknown: frozenset[str]) -> bool:
    return node is not None and any(
        isinstance(n, ast.Name) and n.id in unknown for n in ast.walk(node)
    )


def _bind(binding: _Binding, name: str, value: ast.expr, source: _Binding) -> _Binding:
    """*binding* with *name* bound to what *value* names in *source*, or unknown."""
    scope, unknown = binding
    module = source[0].module(value) if not isinstance(value, ast.Constant) else None
    text = source[0].text(value)
    view = scope.without(frozenset({name}))
    if _reads(value, source[1]) or (module is None and text is None):
        return view, unknown | {name}
    view = copy.copy(view)
    view.names = {**view.names}
    view.strings = {**view.strings}
    view._views = {}
    if module is not None:
        view.names[name] = module
    if text is not None:
        view.strings[name] = text
    return view, unknown - {name}


def _unknown(binding: _Binding, names: frozenset[str]) -> _Binding:
    return binding[0].without(names), binding[1] | names


def _iterate(bindings: list[_Binding], target: ast.expr, iterable: ast.expr) -> list[_Binding]:
    """The bindings a loop body sees: one per element of a literal tuple, list or set,
    unpacked into a tuple target of the same length; the target unknown otherwise."""
    names = frozenset(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
    expanded: list[_Binding] = []
    for binding in bindings:
        literal = isinstance(iterable, (ast.Tuple, ast.List, ast.Set))
        if not literal or _reads(iterable, binding[1]):
            expanded.append(_unknown(binding, names))
            continue
        for element in iterable.elts:
            if isinstance(target, ast.Name):
                expanded.append(_bind(binding, target.id, element, binding))
            elif (
                isinstance(target, (ast.Tuple, ast.List))
                and isinstance(element, (ast.Tuple, ast.List))
                and len(element.elts) == len(target.elts)
                and all(isinstance(part, ast.Name) for part in target.elts)
            ):
                unpacked = binding
                for part, value in zip(target.elts, element.elts):
                    assert isinstance(part, ast.Name)
                    unpacked = _bind(unpacked, part.id, value, binding)
                expanded.append(unpacked)
            else:
                expanded.append(_unknown(binding, names))
    return expanded


def _parameters(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> frozenset[str]:
    # pytest-mock's ``mocker`` stays the patch provider it is everywhere.
    return create_guard._parameters(node) - {"mocker"}


def _patches_at(call: ast.Call, binding: _Binding) -> set[tuple[str, str]]:
    """``(facade, name)`` for each facade attribute *call* patches under *binding*: the
    name ``<dynamic>`` when the file does not spell it, and both ``<dynamic>`` when the
    patched object is one the site cannot name but a facade might be."""
    scope, unknown = binding
    form = create_guard._PATCH_FORMS.get(scope.module(call.func) or "")
    if form is None:
        func = call.func
        if not (isinstance(func, ast.Attribute) and func.attr in _SETTERS and call.args):
            return set()
        form = "setattr"
    target = create_guard._argument(call, 0, "target")
    if form == "setattr":
        # ``setattr("pkg.mod.name", value)`` / ``delattr("pkg.mod.name")`` spell the
        # target as one dotted string; the object form passes the name second.
        assert isinstance(call.func, ast.Attribute)
        dotted = len(call.args) == (2 if call.func.attr == "setattr" else 1) and not any(
            k.arg in ("value", "name") for k in call.keywords
        )
        if dotted:
            text = scope.text(target) if target is not None else None
            if text is not None and not _reads(target, unknown):
                facade, _, attribute = text.rpartition(".")
                facade = create_guard._facade_spelling(facade)
                return {(facade, attribute)} if facade in _FACADES else set()
            return {(_DYNAMIC, _DYNAMIC)} if _reads(target, unknown) else set()
        name_node: ast.expr | None = call.args[1]
    else:
        name_node = create_guard._argument(call, 1, "attribute") if form == "object" else None
    name = scope.text(name_node) if name_node is not None else None
    if target is not None and _reads(target, unknown):
        if form == "multiple":
            spelled = {k.arg for k in call.keywords if k.arg} - create_guard._MULTIPLE_PARAMETERS
            possible = not spelled or bool(spelled & _FACADE_ATTRIBUTES)
        else:
            possible = form == "patch" or name is None or name in _FACADE_ATTRIBUTES
        return {(_DYNAMIC, _DYNAMIC)} if possible else set()
    if form != "setattr":
        return {
            (facade, _DYNAMIC if _reads(name_node, unknown) else patched)
            for facade, patched in create_guard._patched_names(call, form, scope)
            if facade in _FACADES
        }
    module = scope.module(target) if target is not None else None
    if module in _FACADES and name_node is not None:
        return {(module, _DYNAMIC if name is None or _reads(name_node, unknown) else name)}
    return set()


def monkeypatch_targets(
    tree: ast.Module, *, source: str | None = None
) -> list[tuple[int, str, str, str]]:
    """``(line, enclosing function, facade, name)`` for each ``monkeypatch.setattr`` /
    ``delattr`` and ``mock.patch*`` of a facade attribute.

    A patch runs once per binding of every loop and comprehension around it: a target
    over a literal takes each element, a tuple target unpacks each element. A patch
    whose object reads a name the site cannot resolve -- a parameter, or a target over
    anything else -- is ``(<dynamic>, <dynamic>)`` when a facade could be that object,
    and a facade patch whose name the site cannot spell names ``<dynamic>``. A patch of
    an object the file names as something else -- an instance, a class -- is none.

    *source* is the text *tree* came from, when the caller has it; it lets the scope
    read bindings from statements alone (``create_guard._Scope``). The walk dispatches
    on the node's exact type and never enters a name, a constant or an expression
    context, which hold no call and bind nothing: those are most of a module's nodes,
    and this walk runs over every test file in the tree."""
    found: list[tuple[int, str, str, str]] = []

    def visit(node: ast.AST, bindings: list[_Binding], where: str) -> None:
        kind = type(node)
        if kind in _LEAVES:
            return
        if kind in _FUNCTIONS:
            assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            outer: list[ast.AST] = [*getattr(node, "decorator_list", []), *node.args.defaults]
            outer += [d for d in node.args.kw_defaults if d is not None]
            for child in outer:
                visit(child, bindings, where)
            inner = [_unknown(binding, _parameters(node)) for binding in bindings]
            if not isinstance(node, ast.Lambda):
                where = node.name if where == "<module>" else f"{where}.{node.name}"
            for child in node.body if isinstance(node.body, list) else [node.body]:
                visit(child, inner, where)
            return
        if kind is ast.ClassDef:
            assert isinstance(node, ast.ClassDef)
            for child in node.decorator_list:
                visit(child, bindings, where)
            where = node.name if where == "<module>" else f"{where}.{node.name}"
            for child in node.body:
                visit(child, bindings, where)
            return
        if kind in _LOOPS:
            assert isinstance(node, (ast.For, ast.AsyncFor))
            visit(node.iter, bindings, where)
            for child in node.body:
                visit(child, _iterate(bindings, node.target, node.iter), where)
            for child in node.orelse:
                visit(child, bindings, where)
            return
        if kind in _COMPREHENSIONS:
            assert isinstance(node, _COMPREHENSIONS)
            current = bindings
            for generator in node.generators:
                visit(generator.iter, current, where)
                current = _iterate(current, generator.target, generator.iter)
                for condition in generator.ifs:
                    visit(condition, current, where)
            parts = [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
            for part in parts:
                visit(part, current, where)
            return
        if kind is ast.Call:
            assert isinstance(node, ast.Call)
            hits: set[tuple[str, str]] = set()
            for binding in bindings:
                hits |= _patches_at(node, binding)
            found.extend((node.lineno, where, facade, name) for facade, name in sorted(hits))
        for field in node._fields:
            child = getattr(node, field, None)
            if isinstance(child, ast.AST):
                visit(child, bindings, where)
            elif isinstance(child, list):
                for item in child:
                    if isinstance(item, ast.AST):
                        visit(item, bindings, where)

    scope = create_guard._Scope(tree, source=source)
    visit(tree, [(scope, frozenset())], "<module>")
    return found


_Patch = tuple[str, int, str, str, str]

#: How many parts the census is read in. The per-test timeout bounds ONE test, and the
#: whole census -- every test file parsed and walked, under coverage, on a shared
#: runner -- is more than one test's budget. Each part is one test of
#: ``check_shared_bindings``, so the budget bounds a quarter of the tree; the parts
#: hold about the same amount of source, and the sum of the work does not change.
CENSUS_PARTS = 4


@functools.lru_cache(maxsize=1)
def census_parts() -> tuple[tuple[Path, ...], ...]:
    """The files the census parses, dealt into ``CENSUS_PARTS`` parts of about equal
    source size: each file, largest first, joins the lightest part so far. Only the
    paths are kept; the text is read again by the part that parses it, so nothing here
    retains the tree's source for the process lifetime."""
    roots = [_REPO / "test", *sorted((_REPO / "src" / "kiro_crew").rglob("tests"))]
    paths = [_REPO / "conftest.py", *(p for root in roots for p in sorted(root.rglob("*.py")))]
    sizes: list[tuple[int, Path]] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        if create_guard._worth_parsing(text):
            sizes.append((len(text), path))
    parts: list[list[Path]] = [[] for _ in range(CENSUS_PARTS)]
    loads = [0] * CENSUS_PARTS
    for size, path in sorted(sizes, key=lambda item: (-item[0], item[1])):
        lightest = loads.index(min(loads))
        parts[lightest].append(path)
        loads[lightest] += size
    return tuple(tuple(sorted(part)) for part in parts)


@functools.lru_cache(maxsize=CENSUS_PARTS)
def census_part(index: int) -> tuple[_Patch, ...]:
    """``(file, line, enclosing function, facade, name)`` for every facade patch in the
    files of census part *index*, read once for both facades."""
    patches: list[_Patch] = []
    for path in census_parts()[index]:
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(_REPO).as_posix()
        patches.extend(
            (relative, line, where, facade, name)
            for line, where, facade, name in monkeypatch_targets(ast.parse(text), source=text)
        )
    return tuple(patches)


def facade_patches() -> tuple[_Patch, ...]:
    """Every facade patch under ``test/``, each ``src/**/tests`` and the root conftest,
    in file then line order: the union of the parts, each read on its first use."""
    rows = (row for index in range(CENSUS_PARTS) for row in census_part(index))
    return tuple(sorted(rows, key=lambda row: (row[0], row[1])))


def shared_bindings(
    facade: ModuleType,
    seams: dict[tuple[str, str], tuple[str, tuple[str, ...]]],
    patches: tuple[_Patch, ...],
    *,
    mirrored: Mapping[str, tuple[str, ...]] | None = None,
    carved: Mapping[str, ModuleType] | None = None,
) -> set[str]:
    """The names *patches* rebind through *facade* that the facade AND an owner both
    bind: such a patch lands in the facade's namespace only. A forwarded name is not
    one, nor a name the facade alone binds, nor a seam name, which the owner imports
    from the facade when it runs. The owners read are the registered ones plus this
    facade's *carved* owners, and a binding the facade's mirror holds for that name
    (*mirrored*: name -> holders) is not one either: the mirror repeats the write there."""
    seam_names = {name for _facade, names in seams.values() for name in names}
    holders = mirrored or {}
    shared: set[str] = set()
    for _path, _line, _where, patched, name in patches:
        if patched != facade.__name__ or name == _DYNAMIC or name in seam_names:
            continue
        if name in facade._EXPORTS or name not in vars(facade):
            continue
        if any(
            name in vars(owner) and dotted not in holders.get(name, ())
            for dotted, owner in {**_ALL_OWNERS, **(carved or {})}.items()
        ):
            shared.add(name)
    return shared


def check_shared_bindings(
    facade: ModuleType,
    seams: dict[tuple[str, str], tuple[str, tuple[str, ...]]],
    expected: frozenset[str],
    part: int,
    *,
    mirrored: Mapping[str, tuple[str, ...]] | None = None,
    carved: Mapping[str, ModuleType] | None = None,
) -> None:
    """Every shared name the tests of census *part* patch through *facade* is listed, so
    a new one cannot miss an owner's reader silently. One part per test keeps each
    test's work a bounded share of the tree; ``check_listed_bindings_are_patched``
    reads the whole."""
    found = shared_bindings(facade, seams, census_part(part), mirrored=mirrored, carved=carved)
    assert found <= set(expected)


def check_listed_bindings_are_patched(
    facade: ModuleType,
    seams: dict[tuple[str, str], tuple[str, tuple[str, ...]]],
    expected: frozenset[str],
    *,
    mirrored: Mapping[str, tuple[str, ...]] | None = None,
    carved: Mapping[str, ModuleType] | None = None,
) -> None:
    """The whole census, read from the parts: the listed names are exactly the shared
    names some test patches, so a stale entry is refused too, and the scan saw enough
    patches of *facade* to have measured anything. In file order this test follows the
    per-part checks and reads their cached parts; shuffled, it pays for the parts not yet
    read, which costs time and nothing else."""
    patches = facade_patches()
    seen = sum(1 for row in patches if row[3] == facade.__name__)
    assert seen > 50, f"the scan saw only {seen} patches of {facade.__name__}"
    found = shared_bindings(facade, seams, patches, mirrored=mirrored, carved=carved)
    assert found == set(expected)


# --------------------------------------------------------------------------- #
# kiro_crew.sandbox
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("owner", "name"), _moved_rows(_MOVED))
def test_every_moved_name_is_defined_by_its_owner_and_read_through_the_facade(
    owner: str, name: str
) -> None:
    check_moved_name(sandbox, _OWNERS, owner, name)


def test_every_moved_name_is_forwarded_unless_code_still_here_reads_it() -> None:
    check_forwarded_and_bound_are_disjoint(sandbox, _MOVED, _BOUND_HERE)


def test_the_forwarding_table_names_each_owner_by_its_dotted_name() -> None:
    check_forwarding_table(sandbox, _OWNERS)


@pytest.mark.parametrize(
    ("owner", "name"),
    [
        (sandbox_seatbelt, "_SEATBELT_PROFILE"),
        (sandbox_mount_sweep, "_mount_source_candidate_roots"),
        (sandbox_mount_sweep, "_PIN_SCAN_MAX_PASSES"),
    ],
)
def test_a_patch_through_the_facade_round_trips_on_the_owner(owner: ModuleType, name: str) -> None:
    check_round_trips(sandbox, owner, name)


def test_a_write_of_a_facade_name_stays_on_the_facade(monkeypatch: pytest.MonkeyPatch) -> None:
    """A name only the facade binds -- a ``sandbox_plan`` alias no owner reads -- is an
    ordinary attribute write. A forwarded name (a tier table) is written on the owners
    that hold it and not bound on the facade, and neither is ever written on a renderer."""
    monkeypatch.setattr(sandbox, "_is_policy_cache_dir", "stub")
    assert vars(sandbox)["_is_policy_cache_dir"] == "stub"
    assert "_is_policy_cache_dir" not in vars(sandbox_launcher)
    assert not any("_is_policy_cache_dir" in vars(m) for m in _CARVED.values())
    monkeypatch.setattr(sandbox, "_STANDARD_DIRS", ["stub"])
    assert "_STANDARD_DIRS" not in vars(sandbox)
    assert vars(sandbox_tiers)["_STANDARD_DIRS"] == ["stub"]
    assert "_STANDARD_DIRS" not in vars(sandbox_launcher)


def test_a_loaded_owner_is_read_without_the_import_system() -> None:
    check_loaded_owner_needs_no_import(sandbox, sandbox_mount_sweep, "_overflow_uid")


def test_a_purged_owner_is_imported_again_and_read_fresh() -> None:
    check_a_purged_owner_is_imported_again(sandbox, "_task_uid")


def test_every_owner_is_loaded_with_the_facade() -> None:
    check_owners_load_with_the_facade(sandbox)


def test_an_unknown_name_is_an_attribute_error() -> None:
    check_unknown_name(sandbox)


def test_the_star_import_binds_the_public_names(tmp_path: Path) -> None:
    check_star_import(
        tmp_path,
        sandbox,
        frozenset(
            {
                "ModuleType",
                "importlib",
                "sandbox_launcher",
                "sandbox_mount_sweep",
                "sandbox_plan",
                "sandbox_seatbelt",
            }
        ),
    )


def test_the_static_shape_of_the_facade() -> None:
    check_static_shape(sandbox, _OWNERS)


def test_the_bare_global_scan_can_fail() -> None:
    tree = ast.parse("from x import y\n\ndef f():\n    return _overflow_uid\n")
    assert bare_loads(tree, {"_overflow_uid"}) == [(4, "_overflow_uid")]
    assert (
        bare_loads(ast.parse("from x import (\n    _overflow_uid,\n)\n"), {"_overflow_uid"}) == []
    )


def test_the_facade_import_scan_reads_every_spelling() -> None:
    source = (
        "from typing import TYPE_CHECKING\n"
        "from kiro_crew import sandbox\n"
        "import kiro_crew.platform_compat\n"
        "from . import sandbox as sb\n"
        "if TYPE_CHECKING:\n"
        "    from kiro_crew.sandbox import _STANDARD_DIRS\n"
        "def f():\n"
        "    from kiro_crew.sandbox import _STANDARD_DIRS, _CC_FILES\n"
        "class C:\n"
        "    def m(self):\n"
        "        from kiro_crew.platform_compat import IS_POSIX\n"
    )
    assert facade_imports(ast.parse(source), "kiro_crew", _FACADE_NAMES) == [
        ("<module>", "kiro_crew.sandbox", ("<module>",)),
        ("<module>", "kiro_crew.platform_compat", ("<module>",)),
        ("<module>", "kiro_crew.sandbox", ("<module>",)),
        ("f", "kiro_crew.sandbox", ("_STANDARD_DIRS", "_CC_FILES")),
        ("C.m", "kiro_crew.platform_compat", ("IS_POSIX",)),
    ]


def test_each_owner_reads_the_facade_only_for_the_listed_seams() -> None:
    assert owner_seams(_OWNERS, sandbox) == _SEAM_IMPORTS


def test_each_seam_is_a_binding_of_the_facade() -> None:
    check_seams_are_facade_bindings(sandbox, _MOVED, _SEAM_IMPORTS)


def test_the_owners_depend_on_nothing_above_them() -> None:
    check_owner_dependency_direction(_OWNERS, sandbox, _CARVED_IMPORTS)


def test_the_carved_import_table_has_no_cycle_and_no_unused_edge() -> None:
    order = {owner: rank for rank, owner in enumerate(sandbox._CARVED_OWNERS)}
    for owner, imported in _CARVED_IMPORTS.items():
        assert owner in _CARVED, owner
        for other in imported:
            assert other in _OWNERS, (owner, other)
            if other in _CARVED:
                assert order[other] < order[owner], (owner, other)
    used = {owner: owner_imports(module, _OWNERS) for owner, module in _CARVED.items()}
    assert {owner: edges for owner, edges in used.items() if edges} == _CARVED_IMPORTS


def test_the_owner_import_check_refuses_an_unlisted_edge(tmp_path: Path) -> None:
    probe = tmp_path / "probe_carved_owner.py"
    probe.write_text(
        "from kiro_crew.sandbox_leaves import _CREW_HIDDEN_LEAVES\n"
        "from kiro_crew import sandbox_tiers\n",
        encoding="utf-8",
    )
    module = ModuleType("probe_carved_owner")
    module.__file__ = str(probe)
    owners = {**_CARVED, module.__name__: module}
    leaves_only = {**_CARVED_IMPORTS, module.__name__: frozenset({"kiro_crew.sandbox_leaves"})}
    with pytest.raises(AssertionError, match="sandbox_leaves"):
        check_owner_dependency_direction(owners, sandbox, _CARVED_IMPORTS)
    with pytest.raises(AssertionError, match="sandbox_tiers"):
        check_owner_dependency_direction(owners, sandbox, leaves_only)
    both = frozenset({"kiro_crew.sandbox_leaves", "kiro_crew.sandbox_tiers"})
    check_owner_dependency_direction(owners, sandbox, {**_CARVED_IMPORTS, module.__name__: both})


@pytest.mark.parametrize(
    "spelling",
    [
        "from . import sandbox_seals\n",
        "from .sandbox_seals import SandboxCeilingUnsealable\n",
        "import kiro_crew.sandbox_seals\n",
    ],
)
def test_the_owner_import_scan_reads_every_spelling(tmp_path: Path, spelling: str) -> None:
    probe = tmp_path / "probe_spelling.py"
    probe.write_text(spelling, encoding="utf-8")
    module = ModuleType("kiro_crew.probe_spelling")
    module.__file__ = str(probe)
    assert owner_imports(module, _CARVED) == {"kiro_crew.sandbox_seals"}


def test_a_lower_facade_s_owner_cannot_reach_into_a_carved_owner(tmp_path: Path) -> None:
    """A carved owner sits on its facade's layer: an owner of ``platform_compat`` may no
    more import ``kiro_crew.sandbox_seals`` than ``kiro_crew.sandbox`` itself."""
    probe = tmp_path / "probe_lower_owner.py"
    probe.write_text(
        "from kiro_crew.sandbox_seals import SandboxCeilingUnsealable\n", encoding="utf-8"
    )
    module = ModuleType("kiro_crew.probe_lower_owner")
    module.__file__ = str(probe)
    with pytest.raises(AssertionError, match="sandbox_seals"):
        check_owner_dependency_direction({module.__name__: module}, platform_compat)


def test_the_owners_log_under_the_sandbox_name() -> None:
    check_owner_logger(sandbox, _OWNERS)


#: Moved exception classes and their owners. Each keeps the facade's ``__module__``: an
#: error chain renders a raised class as ``module.qualname`` (``subagent._describe_exception``)
#: in tracebacks and the Subagents panel, so the owner's name must not leak into that text.
_FACADE_NAMED_EXCEPTIONS: dict[str, ModuleType] = {
    "SandboxCeilingUnsealable": sandbox_seals,
    "BoundWorkspaceMismatch": sandbox_workspace,
    "UnauditedSpawnRefused": sandbox_unavailable,
    "SandboxUnavailableError": sandbox_unavailable,
}


@pytest.mark.parametrize("name", sorted(_FACADE_NAMED_EXCEPTIONS))
def test_a_moved_exception_class_keeps_the_facade_module(name: str) -> None:
    owner = _FACADE_NAMED_EXCEPTIONS[name]
    cls = vars(owner)[name]
    assert getattr(sandbox, name) is cls
    assert cls.__module__ == sandbox.__name__
    assert cls.__qualname__ == name


def test_an_error_chain_names_the_class_by_the_facade_path() -> None:
    from kiro_crew.subagent import _describe_exception

    error = sandbox.SandboxUnavailableError("no backend confines", "none", "detail")
    rendered = _describe_exception(error)
    assert rendered == "kiro_crew.sandbox.SandboxUnavailableError: no backend confines"


def _imported_modules(module: ModuleType) -> set[str]:
    """Every module *module* imports, at any depth of its body."""
    found: set[str] = set()
    for node in ast.walk(tree_of(module)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module)
    return found


#: The layers under the facade: the plan and the launcher program import the standard
#: library and nothing else, and the two renderers import the plan and nothing else of
#: the package. So the plan stays pure, the program stays runnable as a stdlib script,
#: and neither renderer can reach back into ``kiro_crew.sandbox`` for a rule.
_LAYERED: dict[str, frozenset[str]] = {
    "kiro_crew.sandbox_plan": frozenset(),
    "kiro_crew.sandbox_launcher_program": frozenset(),
    "kiro_crew.sandbox_launcher": frozenset({"kiro_crew.sandbox_plan"}),
    "kiro_crew.sandbox_seatbelt": frozenset({"kiro_crew.sandbox_plan"}),
}


@pytest.mark.parametrize("name", sorted(_LAYERED))
def test_the_plan_layers_import_nothing_above_them(name: str) -> None:
    imported = _imported_modules(importlib.import_module(name))
    package = {m for m in imported if m.split(".")[0] == "kiro_crew"}
    assert package == _LAYERED[name], name
    foreign = {
        m
        for m in imported - package
        if m.split(".")[0] not in sys.stdlib_module_names and m != "__future__"
    }
    assert foreign == set(), (name, foreign)


def test_the_layer_check_flags_an_import_of_the_facade(tmp_path: Path) -> None:
    probe = tmp_path / "probe_layer.py"
    probe.write_text("import os\nfrom kiro_crew import sandbox\nimport yaml\n", encoding="utf-8")
    module = ModuleType("probe_layer")
    module.__file__ = str(probe)
    assert _imported_modules(module) == {"os", "kiro_crew", "yaml"}


#: The path rules that moved to ``kiro_crew.sandbox_plan`` and are still bound on the
#: facade under their old names, for the callers and tests that read them there. They
#: are compatibility ALIASES, not forwards: a patch of one does not reach the planner,
#: which calls its own functions -- patch ``sandbox_plan.<rule>`` instead. A moved rule
#: nothing outside the plan reads is not bound here at all.
_PLAN_ALIASES: dict[str, str] = {
    "_is_policy_cache_dir": "is_policy_cache_dir",
    "_hidden_path_contains_visible_path": "hidden_path_contains_visible_path",
    "_path_within": "path_within",
    "_agent_scrub_prefixes": "scrub_prefixes",
}


@pytest.mark.parametrize(("alias", "rule"), sorted(_PLAN_ALIASES.items()))
def test_a_plan_rule_kept_on_the_facade_is_an_alias_not_a_forward(alias: str, rule: str) -> None:
    assert vars(sandbox)[alias] is getattr(sandbox_plan, rule)
    assert alias not in sandbox._EXPORTS


def test_every_plan_object_the_facade_binds_is_a_listed_alias() -> None:
    plan_objects = {id(v) for v in vars(sandbox_plan).values() if callable(v)}
    bound = {
        name
        for name, value in vars(sandbox).items()
        if name.startswith("_")
        and not name.startswith("__")
        and id(value) in plan_objects
        and getattr(value, "__module__", None) == sandbox_plan.__name__
    }
    assert bound == set(_PLAN_ALIASES)


#: Names a test patches through ``kiro_crew.sandbox`` that the facade AND an unmirrored
#: owner both bind, each importing its own, so the patch misses that owner. ``time`` is
#: rebound for the agents-slice throttling probe in ``sandbox_slice``, which the mirror
#: reaches; it is listed because ``sandbox_mount_sweep`` binds its own ``time``, and the
#: sweep's clock is never faked through the facade. A new entry is a decision that the
#: patch needs no forwarding.
_SANDBOX_SHARED_BINDINGS_PATCHED: frozenset[str] = frozenset({"time"})


@pytest.mark.parametrize("part", range(CENSUS_PARTS))
def test_a_patched_name_bound_by_the_facade_and_an_owner_is_a_listed_one(part: int) -> None:
    check_shared_bindings(
        sandbox,
        _SEAM_IMPORTS,
        _SANDBOX_SHARED_BINDINGS_PATCHED,
        part,
        mirrored=sandbox._MIRROR.holders,
        carved=_CARVED,
    )


def test_every_listed_shared_binding_is_still_patched() -> None:
    check_listed_bindings_are_patched(
        sandbox,
        _SEAM_IMPORTS,
        _SANDBOX_SHARED_BINDINGS_PATCHED,
        mirrored=sandbox._MIRROR.holders,
        carved=_CARVED,
    )


def test_the_patch_scan_reads_every_binding_of_a_patch_site() -> None:
    source = (
        "from unittest import mock\n"
        "from kiro_crew import platform_compat, sandbox\n"
        "def test_x(monkeypatch, attr):\n"
        '    monkeypatch.setattr(sandbox, "_overflow_uid", 1)\n'
        '    monkeypatch.setattr("kiro_crew.platform_compat.file_lock", 1)\n'
        '    monkeypatch.setattr(other, "_overflow_uid", 1)\n'
        "    for module in (platform_compat, sandbox, other):\n"
        '        monkeypatch.setattr(module, "IS_WINDOWS", 1)\n'
        "    for module in MODULES:\n"
        '        mock.patch.object(module, "sys")\n'
        '    for name in ("sys", "time"):\n'
        "        monkeypatch.setattr(platform_compat, name, 1)\n"
        "    monkeypatch.setattr(sandbox, attr, 1)\n"
        '    monkeypatch.setattr(discover.platform_compat, "file_lock", 1)\n'
        "def _helper(monkeypatch, module):\n"
        '    monkeypatch.setattr(module, "IS_WINDOWS", 1)\n'
        "from kiro_crew.dashboard.handlers import discover\n"
    )
    assert monkeypatch_targets(ast.parse(source)) == [
        (4, "test_x", "kiro_crew.sandbox", "_overflow_uid"),
        (5, "test_x", "kiro_crew.platform_compat", "file_lock"),
        (8, "test_x", "<dynamic>", "<dynamic>"),
        (8, "test_x", "kiro_crew.platform_compat", "IS_WINDOWS"),
        (8, "test_x", "kiro_crew.sandbox", "IS_WINDOWS"),
        (10, "test_x", "<dynamic>", "<dynamic>"),
        (12, "test_x", "kiro_crew.platform_compat", "sys"),
        (12, "test_x", "kiro_crew.platform_compat", "time"),
        (13, "test_x", "kiro_crew.sandbox", "<dynamic>"),
        (14, "test_x", "kiro_crew.platform_compat", "file_lock"),
        (16, "_helper", "<dynamic>", "<dynamic>"),
    ]


# --------------------------------------------------------------------------- #
# A patch through the facade reaches the moved code's own reader, and back.
# --------------------------------------------------------------------------- #


@_POSIX_ONLY
def test_a_patched_root_list_reaches_the_keyed_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "_mount_source_candidate_roots", _raiser("roots"))
    with pytest.raises(_Reached, match="roots"):
        sandbox._cleanup_stale_sandbox_mount_sources()


@_POSIX_ONLY
def test_a_patched_pin_scan_reaches_the_legacy_scan(
    monkeypatch: pytest.MonkeyPatch, sandbox_sweep_original: Callable[[str], Callable[..., object]]
) -> None:
    """The test floor stubs ``_bound_source_basenames`` itself, so the real one -- which
    calls the pin scan in its own module -- is read from the floor's stash."""
    bound_source_basenames = sandbox_sweep_original("_bound_source_basenames")
    assert bound_source_basenames.__module__ == sandbox_mount_sweep.__name__
    monkeypatch.setattr(sandbox, "_mount_pinned_source_names", _raiser("pins"))
    with pytest.raises(_Reached, match="pins"):
        bound_source_basenames("/nonexistent-proc-root")


@_POSIX_ONLY
def test_a_patched_tmpfs_root_list_reaches_the_legacy_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "_launcher_tmpfs_roots", _raiser("tmpfs"))
    with pytest.raises(_Reached, match="tmpfs"):
        sandbox._cleanup_legacy_mount_source_residue(tmp_path)


@_POSIX_ONLY
def test_the_facade_s_cleanup_pass_reaches_a_patched_sweep_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``cleanup_stale_sandbox_profiles`` (in ``sandbox_argv``) runs the moved keyed sweep,
    which reads the patched root list from its own module."""
    monkeypatch.setattr(sandbox, "_mount_source_candidate_roots", _raiser("keyed"))
    with pytest.raises(_Reached, match="keyed"):
        sandbox.cleanup_stale_sandbox_profiles(data_home=tmp_path, legacy_dir=str(tmp_path))


@_POSIX_ONLY
def test_a_patched_launcher_builder_reaches_namespace_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(sandbox, "_build_launcher_script", _raiser("launcher"))
    with pytest.raises(_Reached, match="launcher"):
        sandbox.namespace_argv(["/bin/true"], "standard")


@_POSIX_ONLY
def test_a_patched_profile_builder_reaches_sandbox_exec_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(sandbox, "_build_seatbelt_profile", _raiser("profile"))
    with pytest.raises(_Reached, match="profile"):
        sandbox.sandbox_exec_argv(["/bin/true"], "standard")


@pytest.mark.parametrize(
    ("owner", "renderer", "builder"),
    [
        pytest.param(
            sandbox_launcher,
            "render_namespace_launcher",
            "_build_launcher_script",
            marks=_POSIX_ONLY,
            id="namespace",
        ),
        pytest.param(
            sandbox_seatbelt, "render_seatbelt_profile", "_build_seatbelt_profile", id="seatbelt"
        ),
    ],
)
def test_each_builder_renders_through_its_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    owner: ModuleType,
    renderer: str,
    builder: str,
) -> None:
    """The builders stay here and plan the spawn; the owner renders it, read from the
    owner at call time so a patch of the owner's renderer reaches the builder."""
    monkeypatch.setattr(sandbox, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(sandbox, "_ssh_supports_accept_new", lambda: True)
    monkeypatch.setattr(owner, renderer, _raiser(renderer))
    with pytest.raises(_Reached, match=renderer):
        getattr(sandbox, builder)("standard")


def test_every_moved_name_is_forwarded_so_no_patch_needs_resolving() -> None:
    """Why there is no census of the patch sites a scan cannot resolve: every moved name
    is forwarded or mirrored, so a patch through the facade lands on every module that
    holds it whatever spelling put it there, and a name the facade alone binds is one no
    owner reads."""
    names = {name for group in _MOVED.values() for name in group}
    assert names - _BOUND_HERE == set(sandbox._EXPORTS)
    assert _BOUND_HERE <= set(sandbox._MIRROR.holders)


def test_the_sweep_s_pin_scan_coverage_class_is_one_class() -> None:
    """``_PinScanCoverage`` is the owner's class, whichever path a caller reads it by, so
    an ``isinstance`` check on a coverage object built through either path holds."""
    coverage = sandbox._PinScanCoverage()
    assert isinstance(coverage, sandbox_mount_sweep._PinScanCoverage)
    assert sandbox._PinScanCoverage is sandbox_mount_sweep._PinScanCoverage


def test_the_forwarding_is_live_for_any_module_that_imported_a_name_early() -> None:
    """``cli_doctor`` imports ``_MOUNT_SOURCE_PREFIX`` from the facade at load: the
    import resolves through ``__getattr__`` to the owner's object."""
    from kiro_crew import cli_doctor

    assert cli_doctor._MOUNT_SOURCE_PREFIX is sandbox_mount_sweep._MOUNT_SOURCE_PREFIX


# --------------------------------------------------------------------------- #
# The mirror: a write through the facade reaches every carved owner that reads it.
# --------------------------------------------------------------------------- #


def check_mirror_round_trips(facade: ModuleType, name: str) -> None:
    """monkeypatch, mock.patch by object and by dotted name, nested either way, and a
    delete: each lands on every module that holds *name* and is undone on each."""
    holders = [sys.modules[dotted] for dotted in facade._MIRROR.holders[name]]
    if name not in facade._MIRROR.exports:
        holders.append(facade)
    original = vars(holders[0])[name]

    def seen() -> set[object]:
        return {id(vars(module).get(name, _Reached)) for module in holders}

    assert len(holders) > 1 and seen() == {id(original)}
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(facade, name, "stub")
        assert all(vars(module)[name] == "stub" for module in holders)
    assert seen() == {id(original)}
    with mock.patch.object(facade, name, "outer"):
        with mock.patch(f"{facade.__name__}.{name}", "inner"):
            assert all(vars(module)[name] == "inner" for module in holders)
        assert all(vars(module)[name] == "outer" for module in holders)
    assert seen() == {id(original)}
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(facade, name, "mp")
        with mock.patch.object(facade, name, "mock"):
            assert all(vars(module)[name] == "mock" for module in holders)
        assert all(vars(module)[name] == "mp" for module in holders)
    assert seen() == {id(original)}
    with pytest.MonkeyPatch.context() as patched:
        patched.delattr(facade, name)
        assert not any(name in vars(module) for module in holders)
        assert not hasattr(facade, name)
    assert seen() == {id(original)}
    assert facade._MIRROR.divergent() == ()


def test_the_mirror_indexes_exactly_the_carved_owners() -> None:
    assert sandbox._CARVED_OWNERS == tuple(_CARVED)
    assert sandbox._MIRROR.owners == sandbox._CARVED_OWNERS
    assert sandbox._MIRROR.excluded == ()
    assert sandbox._MIRROR.divergent() == ()
    carved = {name: owner for name, owner in sandbox._EXPORTS.items() if owner in _CARVED}
    assert carved == dict(sandbox._MIRROR.exports)


def test_the_renderers_and_the_sweep_stay_out_of_the_mirror() -> None:
    """They owned their names before the carve, and a write through the facade never
    reached them: patching ``sandbox.time`` for the agents-slice probe leaves the mount
    sweep's clock alone, as it did."""
    assert all(set(found) <= set(_CARVED) for found in sandbox._MIRROR.holders.values())
    unmirrored = {name for name, owner in sandbox._EXPORTS.items() if owner not in _CARVED}
    assert unmirrored and not any(unmirrored & set(vars(m)) for m in _CARVED.values())
    real = sandbox_mount_sweep.time
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(sandbox, "time", "stub")
        assert vars(sandbox)["time"] == "stub" and sandbox_mount_sweep.time is real
        for dotted in sandbox._MIRROR.holders.get("time", ()):
            assert vars(sys.modules[dotted])["time"] == "stub"
    assert sandbox.time is real


@pytest.mark.parametrize(
    "name",
    [
        "config_dir",  # an import the facade and many owners bind
        "_CREW_HIDDEN_LEAVES",  # moved, imported by other owners and by the facade
        "_publish_empty_ceiling",  # moved and forwarded, imported by another owner
    ],
)
def test_a_patch_through_the_facade_reaches_every_holder(name: str) -> None:
    check_mirror_round_trips(sandbox, name)
