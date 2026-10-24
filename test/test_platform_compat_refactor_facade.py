"""``kiro_crew.platform_compat`` stays the compatibility layer's import and patch surface.

The cross-process file locks and the owner-only access helpers are defined in
``platform_lock_compat`` and ``platform_owner_compat``. Every name that moved there stays
readable under its old path and is FORWARDED, so a patch through the facade lands on the
owner, where the owner's own callers read it. Those two owners read the platform flags, the
lock modules, the clock, ``ctypes`` and the Win32 struct layouts from the facade when they
run, so a test that rebinds one of those there still reaches them.

The rest of what platform_compat.py defined is carved into the owners in ``_CARVED``, which
import each other lowest layer first (``_CARVED_IMPORTS``) and never the facade. Their code
read one namespace before the carve, so the facade MIRRORS a write through it
(``kiro_crew._facade_mirror``): it lands on every carved owner that binds the same object,
and on the facade when the facade binds the name too. The checks shared with the sandbox
facade, and the one census of facade patches both run on, live in
``test_sandbox_refactor_facade``.
"""

from __future__ import annotations

import enum
import os
import sys
import zlib
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import test_sandbox_refactor_facade as shared

from kiro_crew import (
    platform_compat,
    platform_compat_descendants,
    platform_compat_files,
    platform_compat_identity,
    platform_compat_kill,
    platform_compat_links,
    platform_compat_liveness,
    platform_compat_metrics,
    platform_compat_native,
    platform_compat_ports,
    platform_compat_proctable,
    platform_compat_runtime,
    platform_compat_trust,
    platform_compat_winproc,
    platform_compat_wintree,
    platform_lock_compat,
    platform_owner_compat,
)

# The patch census parses every test file once; keep it on the sandbox file's worker.
pytestmark = pytest.mark.xdist_group(name="tree_scan_sandbox_refactor_facade")

#: The owners carved out of platform_compat.py, lowest layer first, as the facade imports
#: them.
_CARVED: dict[str, ModuleType] = {
    m.__name__: m
    for m in (
        platform_compat_native,
        platform_compat_proctable,
        platform_compat_runtime,
        platform_compat_links,
        platform_compat_files,
        platform_compat_trust,
        platform_compat_liveness,
        platform_compat_identity,
        platform_compat_ports,
        platform_compat_descendants,
        platform_compat_winproc,
        platform_compat_wintree,
        platform_compat_kill,
        platform_compat_metrics,
    )
}

_OWNERS: dict[str, ModuleType] = {
    **{m.__name__: m for m in (platform_lock_compat, platform_owner_compat)},
    **_CARVED,
}

#: Every module-level name the split moved out of platform_compat.py, by its owner, plus
#: ``tempfile``: only the persistence probe reads it now, and a test reaches the stdlib
#: module through ``platform_compat.tempfile``, so the binding moved with its reader. The
#: files owner's two libc temporaries are not listed: they exist only where the C library
#: loaded, and the facade binds them under the same condition.
_MOVED: dict[str, tuple[str, ...]] = {
    "kiro_crew.platform_lock_compat": (
        "_LOCK_POLL_SECS",
        "_LOCK_POLL_MAX_SECS",
        "_LOCK_TIMEOUT_SECS",
        "_WIN_LOCK_POLL_SECS",
        "_WIN_LOCK_TIMEOUT_SECS",
        "_on_event_loop",
        "_lock_timeout_message",
        "_posix_acquire_blocking",
        "_win_acquire_blocking",
        "file_lock",
        "flock_exclusive",
        "open_lock_file",
        "open_create_or_existing",
        "acquire_lock",
        "release_lock",
        "try_acquire_lock",
        "try_acquire_lock_or_raise",
        "probe_file_persistence",
        "tempfile",
    ),
    "kiro_crew.platform_owner_compat": (
        "_OWNER_RIGHTS_SID",
        "_TOKEN_QUERY",
        "_TOKEN_USER_CLASS",
        "_process_token_sid",
        "_process_token_sid_unguarded",
        "process_owner_sid",
        "_TOKEN_SID_CACHE",
        "current_user_sid",
        "make_owner_only_dir",
        "local_user_id",
        "stat_owned_by_current_user",
        "stat_writable_by_current_user",
        "path_writable_by_current_user",
        "restrict_to_owner",
        "restrict_dir_to_owner",
        "_apply_owner_only_dacl",
    ),
    "kiro_crew.platform_compat_native": tuple("""
        IS_WINDOWS IS_POSIX IS_LINUX IS_MACOS _WINDOWS_PID_MAX SIGKILL _OWN_PGID SIGTERM SIGHUP
        CREATE_NEW_PROCESS_GROUP DETACHED_PROCESS CREATE_SUSPENDED _SUBPROCESS_NO_WINDOW
        _ProcessEntry32 _ProcessMemoryCounters _MemoryStatusEx _SidAndAttributes _TokenUser
        _IoCounters _JobObjectBasicLimitInformation _JobObjectExtendedLimitInformation
        _ThreadEntry32 _windows_last_error _open_process_query_handle _close_process_handle
        _PROCESS_QUERY_LIMITED_INFORMATION _ACCESS_HONOURS_EFFECTIVE_IDS
        """.split()),
    "kiro_crew.platform_compat_proctable": tuple("""
        _DARWIN_PROC_PIDVNODEPATHINFO _DARWIN_VNODE_INFO_SIZE _DARWIN_MAXPATHLEN
        _DARWIN_VNODE_INFO_PATH_SIZE _DARWIN_PROC_VNODEPATHINFO_SIZE _DARWIN_PROC_PIDTBSDINFO
        _DARWIN_PROC_BSDINFO_SIZE _DARWIN_PBI_PPID_OFFSET _DARWIN_PBI_START_TVSEC_OFFSET
        _DARWIN_PBI_START_TVUSEC_OFFSET ProcessStartIdentity _DARWIN_PROC_PIDTASKINFO
        _DARWIN_PROC_TASKINFO_SIZE _DARWIN_PTI_TOTAL_USER_OFFSET _DARWIN_PTI_TOTAL_SYSTEM_OFFSET
        _DARWIN_RUSAGE_INFO_V2 _DARWIN_RUSAGE_INFO_V2_SIZE _DARWIN_RI_PHYS_FOOTPRINT_OFFSET
        _darwin_libproc _darwin_libproc_loaded _darwin_libproc_handle _darwin_process_cwd
        _darwin_process_start_identity _darwin_process_phys_footprint_bytes
        proc_phys_footprint_bytes_for_pid darwin_libproc_available darwin_footprint_tree_mb
        _darwin_process_start_microtime _darwin_process_cpu_nanos _DARWIN_PBI_STATUS_OFFSET
        _DARWIN_SZOMB _DARWIN_PIDPATH_MAXSIZE _DARWIN_CTL_KERN _DARWIN_KERN_PROCARGS2
        _DARWIN_PROCARGS_BUFSIZE _DARWIN_PROCARGS_ENV_BUFSIZE _DARWIN_KERN_PROC
        _DARWIN_KERN_PROC_PID _DARWIN_KERN_PROC_PGRP _DARWIN_KINFO_PROC_SIZE
        _DARWIN_KP_START_TVSEC_OFFSET _DARWIN_KP_START_TVUSEC_OFFSET _DARWIN_KP_STAT_OFFSET
        _DARWIN_KP_PID_OFFSET _DARWIN_KINFO_PGRP_SLACK _DARWIN_KINFO_PGRP_ATTEMPTS
        _DARWIN_CHILD_LIST_INITIAL DarwinProcessFacts _darwin_libproc_tree_bound
        _darwin_libproc_tree_handle darwin_child_pids darwin_process_facts darwin_process_path
        _DARWIN_PROC_PIDLISTFDS _DARWIN_PROC_FDINFO_SIZE _DARWIN_PROX_FDTYPE_SOCKET
        _DARWIN_PROC_PIDFDSOCKETINFO _DARWIN_SOCKET_FDINFO_SIZE _DARWIN_SOI_KIND_OFFSET
        _DARWIN_TCPSI_STATE_OFFSET _DARWIN_SOCKINFO_TCP _DARWIN_TSI_S_ESTABLISHED
        _DARWIN_INSI_VFLAG_OFFSET _DARWIN_INSI_FADDR_OFFSET _DARWIN_INI_IPV4 _DARWIN_INI_IPV6
        _DARWIN_IN6_LOOPBACK _DARWIN_IN6_V4MAPPED_PREFIX _DARWIN_MAX_FDS
        _darwin_libproc_fd_bound _darwin_libproc_fd_handle _darwin_peer_is_loopback
        darwin_established_tcp_count _darwin_libc_sysctl _darwin_libc_sysctl_loaded
        _darwin_libc_sysctl_lock _darwin_sysctl_handle darwin_process_argv
        darwin_process_environ DarwinKinfoProc _darwin_kinfo_proc_parse
        _darwin_kinfo_size_mismatch_logged _darwin_kinfo_query darwin_kinfo_proc
        darwin_pid_is_zombie darwin_pgroup_members _DARWIN_BSDINFO_SIZE _DARWIN_OFF_START_TVSEC
        _DARWIN_OFF_START_TVUSEC _DarwinLibprocUnavailable _darwin_libproc_start_id
        _file_identity flock_owner_pid _flock_acquirer_row _flock_acquirer_in_proc_locks
        _flock_acquirer_in_fdinfo parent_pid pids_holding_file _descriptors_on
        _proc_status_rss_kb _stat_tokens _STAT_TOKEN_MAX_DIGITS _stat_token_int _parse_ppid
        ProcStat _linux_proc_root read_proc_stat _LINUX_EXITED_STATES linux_pgroup_members
        read_proc_status_int proc_child_map proc_cpu_jiffies_for_pids _proc_children
        _parse_cpu_jiffies _proc_cpu_jiffies
        """.split()),
    "kiro_crew.platform_compat_runtime": tuple("""
        _UTF8_PROCESS_ENV _ensure_utf8_process_environment _KEPT_FOR_REEXEC keep_for_reexec
        kept_for_reexec _WHEEL_APPLY_MODULE cancel_wheel_applies_in_flight hard_exit
        _disarm_process_alarm_before_exec reexec_launcher reexec_python_module
        execv_target_available _POST_DRAIN_EXEC_FAILURE_EXIT _EXIT_FLUSH_DEADLINE_SECS
        exit_after_failed_restart_exec _drain_event_log_for_exit _WINDOWS_ARM_MACHINES
        is_windows_on_arm BUNDLED_BACKEND_DIST_DIRNAME is_bundled_interpreter
        _PYTHON_OPTIONS_WITH_VALUES isolated_python_argv ensure_utf8_console
        ensure_line_buffered_stdout _is_windows_store_python_stub find_python_interpreter
        process_alarm_available arm_process_alarm boottime_now awake_now process_start_boot_secs
        process_age_secs strftime raise_nofile_soft_limit nofile_soft_limit python_launcher_hops
        """.split()),
    "kiro_crew.platform_compat_links": tuple("""
        symlink_or_junction _ISJUNCTION _FILE_ATTRIBUTE_REPARSE_POINT
        _IO_REPARSE_TAG_MOUNT_POINT _is_junction_fallback _IO_REPARSE_TAG_NAME_SURROGATE
        lstat_is_name_surrogate _StrOrPath strip_extended_length_prefix is_link_or_junction
        first_linked_ancestor unlink_link_or_junction _WIN_GENERIC_READ _WIN_GENERIC_WRITE
        _WIN_FILE_SHARE_READ_WRITE _WIN_FILE_SHARE_READ _WIN_OPEN_EXISTING
        _WIN_FILE_FLAG_BACKUP_SEMANTICS _WIN_FILE_FLAG_OPEN_REPARSE_POINT
        _WIN_FILE_ATTRIBUTE_DIRECTORY _WIN_FILE_ATTRIBUTE_REPARSE_POINT pin_directory
        pinned_dir_flags PINNED_TREE_MAX_DEPTH PinnedDirectory pinned_directory
        _win_open_without_following open_file_no_reparse _WIN_FILE_LIST_DIRECTORY
        _WIN_OPEN_ALWAYS _WIN_FILE_ATTRIBUTE_TAG_INFO win_fd_is_link _win_reparse_refused
        win_open_no_reparse open_create_no_reparse
        """.split()),
    "kiro_crew.platform_compat_files": tuple("""
        _RENAME_NOREPLACE_FN _RENAME_NOREPLACE_FLAG _SYS_RENAMEAT2_BY_MACHINE
        _build_renameat2_via_syscall RENAME_NOREPLACE_AVAILABLE TCC_PROTECTED_HOME_DIRS
        TCC_LIBRARY_WALKABLE_CHILDREN _LIBRARY_DIR rename_noreplace publish_dir_noreplace
        tcc_protected_dirs_for_walk tcc_prune_walk_dirs fchmod_safe chmod_safe
        claude_code_managed_settings_dir
        _clear_readonly_and_retry rmtree_force _WIN_FILE_SHARE_READ_WRITE_DELETE
        open_log_file_for_tail open_lock_file_for_sweep path_volume_is_remote
        _WINDOWS_RUNNABLE_HOOK_SUFFIXES is_executable_file is_readonly_filesystem
        ensure_owner_rwx_dirs _SYS_STATX_BY_MACHINE _AT_FDCWD _AT_EMPTY_PATH _STATX_INO
        _STATX_MNT_ID _STATX_SIZE _statx_mnt_id_unreported _statx_call mount_identity
        """.split()),
    "kiro_crew.platform_compat_trust": tuple("""
        _folded_env_allowlist env_key_allowed _TRUSTED_SYSTEM_BIN_DIRS _WINDOWS_BIN_SUFFIXES
        _windows_system_dirs _FOLDERID_DOCUMENTS _POWERSHELL_USER_PROFILES windows_documents_dir
        windows_powershell_profile_paths _UNPINNED_TOOL_PROBED _log_tool_outside_trusted_dirs
        _WINDOWS_GIT_DIRS trusted_git_bin trusted_system_bin _trusted_bin_search
        trusted_system_bin_quiet _LOCAL_SYSTEM_BIN_DIR _UNTRUSTED_AWS_BIN_LOGGED
        _MAX_SYMLINK_HOPS _root_owned_entry traversed_components traversed_components_and_links
        _group_shared_with_another_account _ACL_XATTR_VERSION _ACL_USER _ACL_GROUP _ACL_MASK
        _ACL_WRITE _NO_ACL_ERRNOS _acl_admits_another_account group_write_admits_another_account
        _is_root_owned_path _local_aws_bin_candidate _is_native_program
        _local_aws_bin_is_trusted trusted_aws_bin aws_bin_declined_on_ownership
        trusted_system_path reveal_in_file_manager open_with_default_app _reveal_env
        tool_outside_trusted_dirs short_path_name
        """.split()),
    "kiro_crew.platform_compat_liveness": tuple("""
        PID_DEAD PID_ALIVE PID_UNSIGNALABLE live_thread_group_leaders is_thread_group_leader
        pid_liveness pgroup_of pgroup_of_leader pid_exists pid_is_zombie process_thread_count
        """.split()),
    "kiro_crew.platform_compat_identity": tuple("""
        process_cwd get_ppid _ANCESTRY_MAX_DEPTH _PYTHON_INTERPRETER_NAME
        process_executable_path _is_python_interpreter_path is_exec_supervisor_of_this_process
        get_process_start_identity get_process_start_id process_namespaces_match
        process_is_sandboxed process_can_read_under_sandbox ProcessIdentitySource
        _WINDOWS_EXIT_FILETIME_TIMEOUT_SECS _WINDOWS_EXIT_FILETIME_POLL_SECS
        _windows_process_handle_identity process_matches process_image_name
        _win_process_image_name process_argv_matches_exact process_argv_fingerprint
        linux_process_name process_cgroups_match process_command_line process_owner_uid
        effective_uid _START_TIME_PS_TIMEOUT _process_lstart process_start_time
        process_start_id_for_source _OWN_START_TIME _linux_boot_id _own_identity_token
        process_incarnation_id own_process_start_time
        """.split()),
    "kiro_crew.platform_compat_ports": tuple("""
        _normalized_tcp_endpoint _linux_tcp_peer_pid _macos_tcp_peer_pid _WindowsTcpOwnerRow
        _WINDOWS_TCP_TABLE_OWNER_PID_LISTENER _WINDOWS_TCP_TABLE_OWNER_PID_CONNECTIONS
        _WINDOWS_TCP_STATE_LISTEN _WINDOWS_TCP_STATE_ESTABLISHED _WINDOWS_TCP_TABLE_MAX_BYTES
        _WINDOWS_ERROR_INSUFFICIENT_BUFFER _windows_tcp_owner_rows get_tcp_peer_pid
        listening_pid_tool listening_pid_tool_available PortListener
        _LOOPBACK_COVERING_ADDRESSES _LSOF_TIMEOUT_SECS _normalize_local_address
        address_covers_loopback loopback_owner_pids probe_port_listeners
        _linux_loopback_listener_inodes _windows_loopback_listener_owner_pids
        process_owns_loopback_listener find_port_listeners find_listening_pids
        """.split()),
    "kiro_crew.platform_compat_descendants": tuple("""
        _descendants_from_parent_map _posix_process_parent_map ProcessDescendantIdentity
        _ProcessStartOrder _process_start_order _PosixProcessSnapshotRow
        _linux_direct_child_pids _direct_child_pids_for_identity_walk
        _atomic_process_descendant_identities _posix_process_snapshot
        _posix_process_identity_map _ordered_posix_descendant_identities _windows_chain_to_root
        _windows_process_descendant_identities process_descendant_identities process_descendants
        attributed_descendants created_after _windows_process_parent_map
        _WINDOWS_CLEANUP_ROOT_LIMIT _WINDOWS_CLEANUP_IDENTITY_LIMIT
        _WINDOWS_CLEANUP_SNAPSHOT_LIMIT WindowsCleanupCapacityError _WindowsTreeOverflow
        """.split()),
    "kiro_crew.platform_compat_winproc": tuple("""
        _open_process_termination_handle open_process_termination_handle
        duplicate_asyncio_process_handle _windows_lineage_matches_lifetimes
        _windows_process_query_diagnostic _windows_descendant_failure_details
        _windows_process_query_creation _windows_foreign_descendant_pids
        descendant_termination_handles close_process_handle descendant_termination_handles_async
        _JOB_OBJECT_LIMIT_ACTIVE_PROCESS _JOB_OBJECT_LIMIT_JOB_MEMORY
        _JOBOBJECT_EXTENDED_LIMIT_INFORMATION_CLASS apply_job_limits _TH32CS_SNAPTHREAD
        _THREAD_SUSPEND_RESUME _INVALID_HANDLE_VALUE _RESUME_THREAD_FAILED
        resume_process_main_thread
        """.split()),
    "kiro_crew.platform_compat_wintree": tuple("""
        _WINDOWS_TERMINATE_REFUSAL_WAIT_MS terminate_process_handle process_handle_active
        _WINDOWS_TREE_REAP_TIMEOUT_SECS _WINDOWS_TREE_REAP_POLL_SECS
        _PENDING_WINDOWS_TREE_RETRY_LIMIT _WINDOWS_CAPTURE_MEASURED_VERSIONS
        WindowsTreeDrainPending _PendingWindowsTreeCleanup _PENDING_WINDOWS_TREE_CLEANUPS
        _PENDING_WINDOWS_TREE_CLEANUPS_LOCK _WINDOWS_TREE_ADMISSIONS
        reserve_windows_tree_cleanup release_windows_tree_reservation _manual_windows_tree
        _bind_windows_cleanup_process create_windows_cleanup_owned_process
        finish_windows_cleanup_owned_spawn _drain_windows_process_tree
        _advance_owned_windows_tree terminate_windows_process_tree_owned
        retry_pending_windows_process_trees terminate_windows_asyncio_tree
        windows_tree_cleanup_pending _create_windows_subprocess_owned
        """.split()),
    "kiro_crew.platform_compat_kill": tuple("""
        pgroup_exists _raise_taskkill_error kill_pid kill_process_group kill_process_tree
        kill_process_tree_pinned kill_pid_pinned kill_pid_async kill_process_tree_async
        REAP_TIMEOUT_SECS _shares_own_process_group kill_popen_tree kill_and_reap
        _run_cleanup_shielded _isolated_group_of_live_child _discard_output_until_exit
        terminate_and_reap terminate_and_reap_sync
        """.split()),
    "kiro_crew.platform_compat_metrics": tuple("""
        _MACH_TASK_BASIC_INFO _MachTimeValue _MachTaskBasicInfo _MACH_TASK_BASIC_INFO_COUNT
        _scale_ru_maxrss _ru_maxrss_bytes _LINUX_STATUS_PATH _LINUX_PEAK_RSS_FIELD
        _LINUX_PEAK_RSS_FLOOR _peak_rss_from_status _linux_peak_rss_bytes _posix_peak_rss_bytes
        _linux_current_rss_bytes _macos_current_rss_bytes _windows_memory_counters
        proc_rss_bytes HEAP_TRIM_INTERVAL_SECONDS HEAP_TRIM_RSS_THRESHOLD_BYTES
        HEAP_TRIM_LOG_THRESHOLD_BYTES HEAP_TRIM_TIMEOUT_SECONDS _malloc_trim _M_ARENA_MAX
        GATEWAY_MALLOC_ARENA_MAX MALLOC_ARENA_MAX_ENV _glibc_mallopt MallocArenaCap
        limit_malloc_arenas log_malloc_arena_cap trim_heap_if_needed HeapTrimMaintainer
        proc_peak_rss_bytes _FD_DIRS count_open_fds proc_rss_bytes_for_pid _SUBTREE_MAX_PROCS
        proc_cpu_nanos_for_pid SubtreeSample _proc_pss_kb proc_subtree_sample
        proc_rss_tree_mb_for_pid proc_cpu_seconds _prev_win_sys_cpu system_memory
        system_cpu_percent _MIB_BYTES _NATURAL_T _HOST_VM_INFO64 _VMStatistics64
        _EXTERNAL_PAGE_COUNT_ELEMENTS macos_vm_statistics _macos_available_mib
        _linux_available_mib host_total_mib host_available_mib MEMORY_PRESSURE_NORMAL
        MEMORY_PRESSURE_WARN MEMORY_PRESSURE_CRITICAL _MEMORY_PRESSURE_NAMES
        _MEMORY_PRESSURE_SYSCTL memory_pressure_level memory_pressure_name
        """.split()),
}

#: Moved names the facade binds again, by import from their owner: code still in
#: platform_compat.py reads them as its own globals, the lock and owner-only modules import
#: them from the facade when they run, or a test reads them from ``vars(platform_compat)``.
#: The mirror holds the facade's binding and the owner's as one name, so a patch through the
#: facade reaches both.
_BOUND_HERE: frozenset[str] = frozenset("""
    IS_LINUX IS_MACOS IS_POSIX IS_WINDOWS _ACCESS_HONOURS_EFFECTIVE_IDS
    _PROCESS_QUERY_LIMITED_INFORMATION _TokenUser is_link_or_junction kill_pid
    publish_dir_noreplace unlink_link_or_junction
    """.split())

#: Carved owner -> the owners it imports. Explicit, so a new edge between owners is a
#: decision; each edge points to an owner earlier in ``platform_compat._CARVED_OWNERS`` (so
#: the graph has no cycle) or to a lock or owner-only module.
_CARVED_IMPORTS: dict[str, frozenset[str]] = {
    "kiro_crew.platform_compat_proctable": frozenset({"kiro_crew.platform_compat_native"}),
    "kiro_crew.platform_compat_runtime": frozenset({"kiro_crew.platform_compat_native"}),
    "kiro_crew.platform_compat_links": frozenset(
        {
            "kiro_crew.platform_compat_native",
            "kiro_crew.platform_lock_compat",
        }
    ),
    "kiro_crew.platform_compat_files": frozenset(
        {
            "kiro_crew.platform_compat_links",
            "kiro_crew.platform_compat_native",
        }
    ),
    "kiro_crew.platform_compat_trust": frozenset({"kiro_crew.platform_compat_native"}),
    "kiro_crew.platform_compat_liveness": frozenset(
        {
            "kiro_crew.platform_compat_native",
            "kiro_crew.platform_compat_proctable",
        }
    ),
    "kiro_crew.platform_compat_identity": frozenset(
        {
            "kiro_crew.platform_compat_native",
            "kiro_crew.platform_compat_proctable",
            "kiro_crew.platform_compat_trust",
        }
    ),
    "kiro_crew.platform_compat_ports": frozenset(
        {
            "kiro_crew.platform_compat_native",
            "kiro_crew.platform_compat_trust",
        }
    ),
    "kiro_crew.platform_compat_descendants": frozenset(
        {
            "kiro_crew.platform_compat_identity",
            "kiro_crew.platform_compat_native",
            "kiro_crew.platform_compat_proctable",
            "kiro_crew.platform_compat_trust",
        }
    ),
    "kiro_crew.platform_compat_winproc": frozenset(
        {
            "kiro_crew.platform_compat_descendants",
            "kiro_crew.platform_compat_identity",
            "kiro_crew.platform_compat_native",
        }
    ),
    "kiro_crew.platform_compat_wintree": frozenset(
        {
            "kiro_crew.platform_compat_descendants",
            "kiro_crew.platform_compat_identity",
            "kiro_crew.platform_compat_native",
            "kiro_crew.platform_compat_winproc",
            "kiro_crew.platform_lock_compat",
        }
    ),
    "kiro_crew.platform_compat_kill": frozenset(
        {
            "kiro_crew.platform_compat_identity",
            "kiro_crew.platform_compat_liveness",
            "kiro_crew.platform_compat_native",
            "kiro_crew.platform_compat_trust",
            "kiro_crew.platform_compat_winproc",
            "kiro_crew.platform_compat_wintree",
        }
    ),
    "kiro_crew.platform_compat_metrics": frozenset(
        {
            "kiro_crew.platform_compat_identity",
            "kiro_crew.platform_compat_liveness",
            "kiro_crew.platform_compat_native",
            "kiro_crew.platform_compat_proctable",
            "kiro_crew.platform_compat_winproc",
        }
    ),
}

_LOCK = "kiro_crew.platform_lock_compat"
_OWNER = "kiro_crew.platform_owner_compat"
_PC = "kiro_crew.platform_compat"

#: Every function in an owner that imports from the facade, and what it reads there.
#: The lock modules are imported at the point where their branch first uses them,
#: because the facade binds only the one this platform has.
_SEAM_IMPORTS: dict[tuple[str, str], tuple[str, tuple[str, ...]]] = {
    (_LOCK, "_posix_acquire_blocking"): (_PC, ("fcntl", "time")),
    (_LOCK, "_win_acquire_blocking"): (_PC, ("time",)),
    (_LOCK, "_win_acquire_blocking._try_once"): (_PC, ("msvcrt",)),
    (_LOCK, "acquire_lock"): (_PC, ("IS_POSIX", "fcntl", "time")),
    (_LOCK, "file_lock"): (_PC, ("IS_POSIX", "fcntl", "msvcrt", "time")),
    (_LOCK, "release_lock"): (_PC, ("IS_POSIX", "fcntl", "msvcrt")),
    (_LOCK, "try_acquire_lock_or_raise"): (_PC, ("IS_POSIX", "fcntl", "msvcrt")),
    (_OWNER, "_process_token_sid"): (_PC, ("IS_POSIX",)),
    (_OWNER, "_process_token_sid_unguarded"): (
        _PC,
        ("_PROCESS_QUERY_LIMITED_INFORMATION", "_TokenUser", "ctypes"),
    ),
    (_OWNER, "local_user_id"): (_PC, ("IS_POSIX",)),
    (_OWNER, "path_writable_by_current_user"): (_PC, ("IS_POSIX", "_ACCESS_HONOURS_EFFECTIVE_IDS")),
    (_OWNER, "process_owner_sid"): (_PC, ("IS_POSIX",)),
    (_OWNER, "restrict_dir_to_owner"): (_PC, ("IS_POSIX",)),
    (_OWNER, "restrict_to_owner"): (_PC, ("IS_POSIX",)),
    (_OWNER, "stat_owned_by_current_user"): (_PC, ("IS_POSIX",)),
    (_OWNER, "stat_writable_by_current_user"): (_PC, ("IS_POSIX",)),
}

#: The lock module the facade binds only on the platform that has it.
_PLATFORM_BOUND = frozenset({"msvcrt"} if platform_compat.IS_POSIX else {"fcntl"})


@pytest.mark.parametrize(("owner", "name"), shared._moved_rows(_MOVED))
def test_every_moved_name_is_defined_by_its_owner_and_read_through_the_facade(
    owner: str, name: str
) -> None:
    shared.check_moved_name(platform_compat, _OWNERS, owner, name)


def test_every_moved_name_is_forwarded_unless_code_still_here_reads_it() -> None:
    shared.check_forwarded_and_bound_are_disjoint(platform_compat, _MOVED, _BOUND_HERE)


def test_the_forwarding_table_names_each_owner_by_its_dotted_name() -> None:
    shared.check_forwarding_table(platform_compat, _OWNERS)


@pytest.mark.parametrize(
    ("owner", "name"),
    [
        (platform_lock_compat, "file_lock"),
        (platform_lock_compat, "_LOCK_TIMEOUT_SECS"),
        (platform_owner_compat, "_TOKEN_SID_CACHE"),
        (platform_owner_compat, "restrict_to_owner"),
    ],
)
def test_a_patch_through_the_facade_round_trips_on_the_owner(owner: ModuleType, name: str) -> None:
    shared.check_round_trips(platform_compat, owner, name)


def test_a_write_of_a_facade_name_stays_on_the_facade(monkeypatch: pytest.MonkeyPatch) -> None:
    """A name the facade binds itself -- a platform flag, a module -- is an ordinary
    attribute write, which the owners then read at call time."""
    monkeypatch.setattr(platform_compat, "IS_POSIX", "stub")
    assert vars(platform_compat)["IS_POSIX"] == "stub"
    assert "IS_POSIX" not in vars(platform_lock_compat)
    assert "IS_POSIX" not in vars(platform_owner_compat)


def test_a_loaded_owner_is_read_without_the_import_system() -> None:
    shared.check_loaded_owner_needs_no_import(
        platform_compat, platform_owner_compat, "current_user_sid"
    )


def test_a_purged_owner_is_imported_again_and_read_fresh() -> None:
    shared.check_a_purged_owner_is_imported_again(platform_compat, "_lock_timeout_message")


def test_every_owner_is_loaded_with_the_facade() -> None:
    shared.check_owners_load_with_the_facade(platform_compat)


def test_an_unknown_name_is_an_attribute_error() -> None:
    shared.check_unknown_name(platform_compat)


def test_the_star_import_binds_the_moved_public_names(tmp_path: Path) -> None:
    shared.check_star_import(
        tmp_path,
        platform_compat,
        frozenset({"TYPE_CHECKING", "platform_lock_compat", "platform_owner_compat"}),
    )
    bound = shared.star_import(tmp_path, platform_compat)
    for name in ("file_lock", "try_acquire_lock", "restrict_to_owner", "make_owner_only_dir"):
        assert bound[name] is getattr(platform_compat, name)


def test_the_static_shape_of_the_facade() -> None:
    shared.check_static_shape(platform_compat, _OWNERS)


def test_each_owner_reads_the_facade_only_for_the_listed_seams() -> None:
    assert shared.owner_seams(_OWNERS, platform_compat) == _SEAM_IMPORTS


def test_each_seam_is_a_binding_of_the_facade() -> None:
    shared.check_seams_are_facade_bindings(platform_compat, _MOVED, _SEAM_IMPORTS, _PLATFORM_BOUND)


def test_the_owners_depend_on_nothing_above_them() -> None:
    shared.check_owner_dependency_direction(_OWNERS, platform_compat, _CARVED_IMPORTS)


def test_the_owners_log_under_the_compatibility_layer_s_name() -> None:
    shared.check_owner_logger(platform_compat, _OWNERS)


#: Names a test patches through ``kiro_crew.platform_compat`` that the facade AND an owner
#: both bind, each importing its own, so the patch reaches the facade's and the carved
#: owners' code (the mirror carries it there) but not the lock or owner-only module's.
#: ``Path`` is rebound for the process-identity and trusted-binary helpers, in the carved
#: identity and trust owners; no test fakes it for a lock or an owner-only helper. A new
#: entry is a decision that the patch needs no forwarding.
_PC_SHARED_BINDINGS_PATCHED: frozenset[str] = frozenset({"Path"})


@pytest.mark.parametrize("part", range(shared.CENSUS_PARTS))
def test_a_patched_name_bound_by_the_facade_and_an_owner_is_a_listed_one(part: int) -> None:
    shared.check_shared_bindings(
        platform_compat,
        _SEAM_IMPORTS,
        _PC_SHARED_BINDINGS_PATCHED,
        part,
        mirrored=platform_compat._MIRROR.holders,
        carved=_CARVED,
    )


def test_every_listed_shared_binding_is_still_patched() -> None:
    shared.check_listed_bindings_are_patched(
        platform_compat,
        _SEAM_IMPORTS,
        _PC_SHARED_BINDINGS_PATCHED,
        mirrored=platform_compat._MIRROR.holders,
        carved=_CARVED,
    )


def test_the_mirror_indexes_exactly_the_carved_owners() -> None:
    assert platform_compat._CARVED_OWNERS == tuple(_CARVED)
    assert platform_compat._MIRROR.owners == platform_compat._CARVED_OWNERS
    assert platform_compat._MIRROR.excluded == ()
    assert platform_compat._MIRROR.divergent() == ()
    carved = {name: owner for name, owner in platform_compat._EXPORTS.items() if owner in _CARVED}
    assert carved == dict(platform_compat._MIRROR.exports)


def test_the_lock_and_owner_only_modules_stay_out_of_the_mirror() -> None:
    """They owned their names before the carve, and a write through the facade never
    reached them: patching ``platform_compat.Path`` for the code that stays here and in the
    carved owners leaves the lock and owner-only modules' own ``Path`` alone, as it did."""
    assert all(set(found) <= set(_CARVED) for found in platform_compat._MIRROR.holders.values())
    unmirrored = {name for name, owner in platform_compat._EXPORTS.items() if owner not in _CARVED}
    assert unmirrored and not any(unmirrored & set(vars(m)) for m in _CARVED.values())
    real = platform_lock_compat.Path
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(platform_compat, "Path", "stub")
        assert vars(platform_compat)["Path"] == "stub"
        assert platform_lock_compat.Path is real and platform_owner_compat.Path is real
        for dotted in platform_compat._MIRROR.holders.get("Path", ()):
            assert vars(sys.modules[dotted])["Path"] == "stub"
    assert platform_compat.Path is real


# --------------------------------------------------------------------------- #
# A name rebound on the facade reaches the owner that reads it at call time.
# --------------------------------------------------------------------------- #


class _ReachedModule:
    """Stands in for a module: reading any attribute of it raises ``_Reached`` naming the
    facade binding the helper read."""

    def __init__(self, label: str) -> None:
        self._label = label

    def __getattr__(self, attr: str) -> Any:
        raise shared._Reached(f"{self._label}.{attr}")


def test_a_rebound_lock_module_reaches_the_posix_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(platform_compat, "IS_POSIX", True)
    monkeypatch.setattr(platform_compat, "fcntl", _ReachedModule("fcntl"), raising=False)
    with platform_compat.open_lock_file(tmp_path / "lock") as fd:
        with pytest.raises(shared._Reached, match="fcntl"):
            with platform_compat.file_lock(fd):
                pass


def test_a_rebound_platform_flag_and_lock_module_reach_the_windows_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Windows branch runs from a POSIX host: the flag and ``msvcrt`` are both read
    from the facade, which is what a stubbed ``msvcrt`` has always relied on."""
    taken: list[tuple[int, int]] = []
    fake = SimpleNamespace(
        LK_NBLCK=2, LK_UNLCK=0, locking=lambda fd, mode, _n: taken.append((fd, mode))
    )
    monkeypatch.setattr(platform_compat, "IS_POSIX", False)
    monkeypatch.setattr(platform_compat, "msvcrt", fake, raising=False)
    with platform_compat.open_lock_file(tmp_path / "lock") as fd:
        with platform_compat.file_lock(fd):
            pass
    assert [mode for _fd, mode in taken] == [2, 0]


def test_a_rebound_clock_reaches_the_windows_spin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(platform_compat, "time", _ReachedModule("time"))
    fake = SimpleNamespace(LK_NBLCK=2, locking=lambda *_a: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(platform_compat, "msvcrt", fake, raising=False)
    with platform_compat.open_lock_file(tmp_path / "lock") as fd:
        with pytest.raises(shared._Reached, match="time.monotonic"):
            platform_compat._win_acquire_blocking(fd, timeout=1.0)


def test_a_rebound_clock_reaches_the_lock_s_wait_measurement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal reports the time the acquire really waited, measured on the clock
    the facade binds. A stand-in ``fcntl`` keeps the POSIX branch runnable on any host
    and takes no real lock."""
    fake = SimpleNamespace(LOCK_EX=2, LOCK_SH=1, LOCK_NB=4, LOCK_UN=8, flock=lambda *_a: None)
    monkeypatch.setattr(platform_compat, "IS_POSIX", True)
    monkeypatch.setattr(platform_compat, "fcntl", fake, raising=False)
    monkeypatch.setattr(platform_compat, "time", _ReachedModule("time"))
    with platform_compat.open_lock_file(tmp_path / "lock") as fd:
        with pytest.raises(shared._Reached, match="time.monotonic"):
            with platform_compat.file_lock(fd):
                pass


def test_a_patched_ceiling_reaches_the_lock_s_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_LOCK_TIMEOUT_SECS`` is the owner's: a patch through the facade is the ceiling
    the refusal message names."""
    monkeypatch.setattr(platform_compat, "IS_POSIX", False)
    monkeypatch.setattr(platform_compat, "_win_acquire_blocking", lambda _fd, **_kw: False)
    monkeypatch.setattr(platform_compat, "_LOCK_TIMEOUT_SECS", 1234.0)
    with platform_compat.open_lock_file(tmp_path / "lock") as fd:
        with pytest.raises(OSError, match="1234"):
            with platform_compat.file_lock(fd):
                pass


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="test/conftest.py stubs restrict_to_owner on Windows outside the exempt modules",
)
def test_a_rebound_platform_flag_reaches_restrict_to_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Windows branch of the owner-only lockdown, and the DACL writer it calls in
    its own module, each reached through a patch on the facade."""
    target = tmp_path / "secret"
    target.write_text("x", encoding="utf-8")
    monkeypatch.setattr(platform_compat, "IS_POSIX", False)
    monkeypatch.setattr(platform_compat, "_apply_owner_only_dacl", shared._raiser("dacl"))
    with pytest.raises(shared._Reached, match="dacl"):
        platform_compat.restrict_to_owner(target)


def test_a_patched_directory_lockdown_reaches_make_owner_only_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The conftest's Windows stub relies on this: ``make_owner_only_dir`` calls
    ``restrict_dir_to_owner`` in its own module, where a patch through the facade lands."""
    reached: list[str] = []
    monkeypatch.setattr(platform_compat, "restrict_dir_to_owner", lambda p: reached.append(str(p)))
    platform_compat.make_owner_only_dir(tmp_path / "owned")
    assert reached == [str(tmp_path / "owned")]


def test_a_rebound_ctypes_reaches_the_token_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform_compat, "ctypes", _ReachedModule("ctypes"))
    with pytest.raises(shared._Reached, match="ctypes"):
        platform_compat._process_token_sid_unguarded()


def test_a_rebound_platform_flag_reaches_local_user_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Off POSIX the id is derived from the token's SID, read through the facade too."""
    monkeypatch.setattr(platform_compat, "IS_POSIX", False)
    monkeypatch.setattr(platform_compat, "current_user_sid", lambda: "S-1-5-21-1-2-3-1001")
    assert platform_compat.local_user_id() == zlib.crc32(b"S-1-5-21-1-2-3-1001")


@pytest.mark.parametrize("honoured", [False, True])
def test_a_patched_effective_ids_flag_reaches_the_writability_check(
    honoured: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_ACCESS_HONOURS_EFFECTIVE_IDS`` stays on the facade, shared with the trusted-binary
    walk; the owner's writability check reads it there at call time, and reads the
    patched mode-bit check in its own module."""
    target = tmp_path / "file"
    target.write_text("x", encoding="utf-8")
    asked: list[object] = []

    def spy(path: object, mode: int, **kwargs: object) -> bool:
        asked.append(kwargs.get("effective_ids"))
        return True

    monkeypatch.setattr(platform_compat, "IS_POSIX", True)
    monkeypatch.setattr(platform_compat, "stat_writable_by_current_user", lambda _st: False)
    monkeypatch.setattr(os, "access", spy)
    monkeypatch.setattr(platform_compat, "_ACCESS_HONOURS_EFFECTIVE_IDS", honoured)
    assert platform_compat.path_writable_by_current_user(target) is honoured
    assert asked == ([True] if honoured else [])


#: The moved exception and enum classes. A traceback, a log line, a pickle and every
#: ``module.qualname`` rendering read their module, so each keeps the facade's: the
#: rendered path is one callers import, and it resolves to the very class.
_FACADE_NAMED_CLASSES: dict[str, tuple[str, ...]] = {
    "kiro_crew.platform_compat_proctable": ("_DarwinLibprocUnavailable",),
    "kiro_crew.platform_compat_identity": ("ProcessIdentitySource",),
    "kiro_crew.platform_compat_descendants": (
        "_ProcessStartOrder",
        "WindowsCleanupCapacityError",
        "_WindowsTreeOverflow",
    ),
    "kiro_crew.platform_compat_wintree": ("WindowsTreeDrainPending",),
}


def test_every_moved_exception_and_enum_class_is_listed() -> None:
    found = {
        owner: tuple(
            name
            for name in _MOVED[owner]
            if isinstance(value := vars(module).get(name), type)
            and issubclass(value, (BaseException, enum.Enum))
        )
        for owner, module in _CARVED.items()
    }
    assert {k: v for k, v in found.items() if v} == _FACADE_NAMED_CLASSES


@pytest.mark.parametrize(
    ("owner", "name"),
    [(owner, name) for owner, names in _FACADE_NAMED_CLASSES.items() for name in names],
)
def test_a_moved_exception_or_enum_class_keeps_the_facade_s_module(owner: str, name: str) -> None:
    cls = vars(_CARVED[owner])[name]
    assert cls.__module__ == platform_compat.__name__
    assert getattr(sys.modules[cls.__module__], cls.__qualname__) is cls
