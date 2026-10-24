"""Cross-platform compatibility shims for POSIX-only APIs.

All helpers are safe no-ops (or best-effort fallbacks) on Windows where
the underlying syscall does not exist. Callers should use these instead
of raw ``os.*`` / ``fcntl`` / ``signal`` calls for anything that is
POSIX-only.
"""

from __future__ import annotations

import asyncio  # noqa: F401
import contextlib
import ctypes.util  # noqa: F401
import enum  # noqa: F401
import errno  # noqa: F401
import functools  # noqa: F401
import hashlib  # noqa: F401
import importlib
import io  # noqa: F401
import ipaddress  # noqa: F401
import logging
import ntpath  # noqa: F401
import os  # noqa: F401
import pathlib  # noqa: F401
import platform  # noqa: F401
import re  # noqa: F401
import shutil  # noqa: F401
import signal  # noqa: F401
import site  # noqa: F401
import stat  # noqa: F401
import struct  # noqa: F401
import subprocess  # noqa: F401
import sys
import sys as _sys
import threading  # noqa: F401
import time  # noqa: F401
import types  # noqa: F401
from asyncio import subprocess as aio_subprocess  # noqa: F401
from ctypes import wintypes  # type aliases only; imports cleanly on every platform  # noqa: F401
from pathlib import Path  # noqa: F401
from typing import (  # noqa: F401
    TYPE_CHECKING,
    Any,
    Callable,
    Coroutine,
    Iterator,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    TypeVar,
)

# Loaded with this module, so each owner is complete before the mirror at the end of
# this file indexes the family and before any reader can reach it.
from kiro_crew import platform_lock_compat, platform_owner_compat, windows_acl  # noqa: F401
from kiro_crew.executors import subprocess_executor  # noqa: F401
from kiro_crew.subprocess_utf8 import UTF8_TEXT  # noqa: F401

logger = logging.getLogger(__name__)


# The carved owners' names this module binds itself: the ones its own code reads, the
# ones the lock and owner-only modules import from here when they run, and the few a
# test reads from this module's namespace or patches with unpacked keyword arguments.
# One per line, so a line diff of the split keeps the code around them as unchanged
# context.
from kiro_crew.platform_compat_files import publish_dir_noreplace  # noqa: E402,F401
from kiro_crew.platform_compat_kill import kill_pid  # noqa: E402,F401
from kiro_crew.platform_compat_links import is_link_or_junction  # noqa: E402,F401
from kiro_crew.platform_compat_links import unlink_link_or_junction  # noqa: E402,F401
from kiro_crew.platform_compat_native import _ACCESS_HONOURS_EFFECTIVE_IDS  # noqa: E402,F401
from kiro_crew.platform_compat_native import _PROCESS_QUERY_LIMITED_INFORMATION  # noqa: E402,F401
from kiro_crew.platform_compat_native import IS_LINUX  # noqa: E402,F401
from kiro_crew.platform_compat_native import IS_MACOS  # noqa: E402,F401
from kiro_crew.platform_compat_native import IS_POSIX  # noqa: E402,F401
from kiro_crew.platform_compat_native import IS_WINDOWS  # noqa: E402,F401
from kiro_crew.platform_compat_native import _TokenUser  # noqa: E402,F401

# --------------------------------------------------------------------------- #
# Compatibility facade. What this module defined is carved into the owner modules in
# ``_CARVED_OWNERS``; the cross-process file locks live in
# ``kiro_crew.platform_lock_compat`` and the owner-only access helpers (the process
# token's SID, the writability checks, ``restrict_to_owner`` and its directory form) in
# ``kiro_crew.platform_owner_compat``. Every moved name stays readable as
# ``kiro_crew.platform_compat.<name>``: a name this module binds is read here, and every
# other one (``_EXPORTS_BY_OWNER``) is FORWARDED -- the mirror's module class reads it
# from its owner, resolved from :data:`sys.modules` on each use. A write or delete
# through this module reaches every module that holds the name: a lock or owner-only
# name lands on that owner alone, as it did before the carve, and any other name lands on
# this module (when it binds the name) and on every carved owner that binds the same
# object (``kiro_crew._facade_mirror``), so a patch of ``kiro_crew.platform_compat.<name>``
# reaches every reader it reached when this was one module, however the test spells it. A
# forwarded name is absent from this module's namespace on purpose -- a binding here
# would shadow the owner for every later read.
#
# The lock and owner-only modules read what they need from this module -- the platform
# flags, the lock modules, the clock, ``ctypes`` and the Win32 struct layouts -- through
# a function-local import when they run, so a test that rebinds one of those here still
# reaches them; this module keeps binding each of those names. No carved owner imports
# this module: the carved owners import each other lowest layer first.
# ``test/test_platform_compat_refactor_facade.py`` pins the forwarding, the mirror and
# that direction.
# --------------------------------------------------------------------------- #
# The lock modules the lock helpers read from here, bound where the platform has them.
if IS_POSIX:
    import fcntl  # noqa: F401 — re-exported
    import resource  # noqa: F401 — POSIX-only; a patch here reaches the runtime and metrics owners
else:
    import msvcrt  # type: ignore[import-not-found]  # noqa: F401

# The no-replace rename's libc handle and symbol name, bound where the files owner binds
# them: on Linux and macOS, when the C library loaded.
if IS_LINUX or IS_MACOS:
    with contextlib.suppress(ImportError):
        from kiro_crew.platform_compat_files import _rename_libc, _rename_symbol  # noqa: F401

from kiro_crew import _facade_mirror  # noqa: E402

#: The modules carved out of this one, lowest layer first: the first one that binds a
#: name answers a forwarded read of it. They are imported here, before the mirror
#: indexes them, so no reader can find one half-built.
_CARVED_OWNERS: tuple[str, ...] = (
    "kiro_crew.platform_compat_native",
    "kiro_crew.platform_compat_proctable",
    "kiro_crew.platform_compat_runtime",
    "kiro_crew.platform_compat_links",
    "kiro_crew.platform_compat_files",
    "kiro_crew.platform_compat_trust",
    "kiro_crew.platform_compat_liveness",
    "kiro_crew.platform_compat_identity",
    "kiro_crew.platform_compat_ports",
    "kiro_crew.platform_compat_descendants",
    "kiro_crew.platform_compat_winproc",
    "kiro_crew.platform_compat_wintree",
    "kiro_crew.platform_compat_kill",
    "kiro_crew.platform_compat_metrics",
)


def _import_carved_owners() -> None:
    for dotted in _CARVED_OWNERS:
        importlib.import_module(dotted)


_import_carved_owners()

#: The names moved to the lock and owner-only modules, by owner. They owned these names
#: before the carve, so each name is read from, written to and deleted on its one owner,
#: and a write through this module never reaches another module.
_FORWARDS_BY_OWNER: dict[str, tuple[str, ...]] = {
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
}


def _index_forwards() -> dict[str, str]:
    """Invert the lock and owner-only table into forwarded name -> owner."""
    return {name: owner for owner, names in _FORWARDS_BY_OWNER.items() for name in names}


#: Forwarded name -> the dotted NAME of its owner, never the module object: the owner
#: is read from :data:`sys.modules` on each use, so a module purged and imported again
#: is seen at once instead of this table forwarding to the old copy. The carved names
#: come from the mirror's index, the lock and owner-only names from their table.
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

# ``from kiro_crew.platform_compat import *`` binds only the names this list holds:
# nothing can enumerate what the mirror would serve, so a forwarded name reaches a star
# importer only by being listed here. The list is DERIVED from what this module binds
# plus the forwarding table, so it is not a third list of names to keep in step. The
# bindings only the forwarding needs stay out of it, so a star import binds what it
# bound before the owners existed.
_NOT_EXPORTED = frozenset({"TYPE_CHECKING", "platform_lock_compat", "platform_owner_compat"})
__all__ = sorted(
    name
    for name in set(globals()) | set(_EXPORTS)
    if not name.startswith("_") and name not in _NOT_EXPORTED
)

if TYPE_CHECKING:  # the forwarded names, visible to type checkers and IDEs
    from kiro_crew.platform_compat_descendants import (  # noqa: F401
        _WINDOWS_CLEANUP_IDENTITY_LIMIT,
        _WINDOWS_CLEANUP_ROOT_LIMIT,
        _WINDOWS_CLEANUP_SNAPSHOT_LIMIT,
        ProcessDescendantIdentity,
        WindowsCleanupCapacityError,
        _atomic_process_descendant_identities,
        _descendants_from_parent_map,
        _direct_child_pids_for_identity_walk,
        _linux_direct_child_pids,
        _ordered_posix_descendant_identities,
        _posix_process_identity_map,
        _posix_process_parent_map,
        _posix_process_snapshot,
        _PosixProcessSnapshotRow,
        _process_start_order,
        _ProcessStartOrder,
        _windows_chain_to_root,
        _windows_process_descendant_identities,
        _windows_process_parent_map,
        _WindowsTreeOverflow,
        attributed_descendants,
        created_after,
        process_descendant_identities,
        process_descendants,
    )
    from kiro_crew.platform_compat_files import (  # noqa: F401
        _AT_EMPTY_PATH,
        _AT_FDCWD,
        _LIBRARY_DIR,
        _RENAME_NOREPLACE_FLAG,
        _RENAME_NOREPLACE_FN,
        _STATX_INO,
        _STATX_MNT_ID,
        _STATX_SIZE,
        _SYS_RENAMEAT2_BY_MACHINE,
        _SYS_STATX_BY_MACHINE,
        _WIN_FILE_SHARE_READ_WRITE_DELETE,
        _WINDOWS_RUNNABLE_HOOK_SUFFIXES,
        RENAME_NOREPLACE_AVAILABLE,
        TCC_LIBRARY_WALKABLE_CHILDREN,
        TCC_PROTECTED_HOME_DIRS,
        _build_renameat2_via_syscall,
        _clear_readonly_and_retry,
        _statx_call,
        _statx_mnt_id_unreported,
        chmod_safe,
        claude_code_managed_settings_dir,
        ensure_owner_rwx_dirs,
        fchmod_safe,
        is_executable_file,
        is_readonly_filesystem,
        mount_identity,
        open_lock_file_for_sweep,
        open_log_file_for_tail,
        path_volume_is_remote,
        rename_noreplace,
        rmtree_force,
        tcc_protected_dirs_for_walk,
        tcc_prune_walk_dirs,
    )
    from kiro_crew.platform_compat_identity import (  # noqa: F401
        _ANCESTRY_MAX_DEPTH,
        _OWN_START_TIME,
        _PYTHON_INTERPRETER_NAME,
        _START_TIME_PS_TIMEOUT,
        _WINDOWS_EXIT_FILETIME_POLL_SECS,
        _WINDOWS_EXIT_FILETIME_TIMEOUT_SECS,
        ProcessIdentitySource,
        _is_python_interpreter_path,
        _linux_boot_id,
        _own_identity_token,
        _process_lstart,
        _win_process_image_name,
        _windows_process_handle_identity,
        effective_uid,
        get_ppid,
        get_process_start_id,
        get_process_start_identity,
        is_exec_supervisor_of_this_process,
        linux_process_name,
        own_process_start_time,
        process_argv_fingerprint,
        process_argv_matches_exact,
        process_can_read_under_sandbox,
        process_cgroups_match,
        process_command_line,
        process_cwd,
        process_executable_path,
        process_image_name,
        process_incarnation_id,
        process_is_sandboxed,
        process_matches,
        process_namespaces_match,
        process_owner_uid,
        process_start_id_for_source,
        process_start_time,
    )
    from kiro_crew.platform_compat_kill import (  # noqa: F401
        REAP_TIMEOUT_SECS,
        _discard_output_until_exit,
        _isolated_group_of_live_child,
        _raise_taskkill_error,
        _run_cleanup_shielded,
        _shares_own_process_group,
        kill_and_reap,
        kill_pid_async,
        kill_pid_pinned,
        kill_popen_tree,
        kill_process_group,
        kill_process_tree,
        kill_process_tree_async,
        kill_process_tree_pinned,
        pgroup_exists,
        terminate_and_reap,
        terminate_and_reap_sync,
    )
    from kiro_crew.platform_compat_links import (  # noqa: F401
        _FILE_ATTRIBUTE_REPARSE_POINT,
        _IO_REPARSE_TAG_MOUNT_POINT,
        _IO_REPARSE_TAG_NAME_SURROGATE,
        _ISJUNCTION,
        _WIN_FILE_ATTRIBUTE_DIRECTORY,
        _WIN_FILE_ATTRIBUTE_REPARSE_POINT,
        _WIN_FILE_ATTRIBUTE_TAG_INFO,
        _WIN_FILE_FLAG_BACKUP_SEMANTICS,
        _WIN_FILE_FLAG_OPEN_REPARSE_POINT,
        _WIN_FILE_LIST_DIRECTORY,
        _WIN_FILE_SHARE_READ,
        _WIN_FILE_SHARE_READ_WRITE,
        _WIN_GENERIC_READ,
        _WIN_GENERIC_WRITE,
        _WIN_OPEN_ALWAYS,
        _WIN_OPEN_EXISTING,
        PINNED_TREE_MAX_DEPTH,
        PinnedDirectory,
        _is_junction_fallback,
        _StrOrPath,
        _win_open_without_following,
        _win_reparse_refused,
        first_linked_ancestor,
        lstat_is_name_surrogate,
        open_create_no_reparse,
        open_file_no_reparse,
        pin_directory,
        pinned_dir_flags,
        pinned_directory,
        strip_extended_length_prefix,
        symlink_or_junction,
        win_fd_is_link,
        win_open_no_reparse,
    )
    from kiro_crew.platform_compat_liveness import (  # noqa: F401
        PID_ALIVE,
        PID_DEAD,
        PID_UNSIGNALABLE,
        is_thread_group_leader,
        live_thread_group_leaders,
        pgroup_of,
        pgroup_of_leader,
        pid_exists,
        pid_is_zombie,
        pid_liveness,
        process_thread_count,
    )
    from kiro_crew.platform_compat_metrics import (  # noqa: F401
        _EXTERNAL_PAGE_COUNT_ELEMENTS,
        _FD_DIRS,
        _HOST_VM_INFO64,
        _LINUX_PEAK_RSS_FIELD,
        _LINUX_PEAK_RSS_FLOOR,
        _LINUX_STATUS_PATH,
        _M_ARENA_MAX,
        _MACH_TASK_BASIC_INFO,
        _MACH_TASK_BASIC_INFO_COUNT,
        _MEMORY_PRESSURE_NAMES,
        _MEMORY_PRESSURE_SYSCTL,
        _MIB_BYTES,
        _NATURAL_T,
        _SUBTREE_MAX_PROCS,
        GATEWAY_MALLOC_ARENA_MAX,
        HEAP_TRIM_INTERVAL_SECONDS,
        HEAP_TRIM_LOG_THRESHOLD_BYTES,
        HEAP_TRIM_RSS_THRESHOLD_BYTES,
        HEAP_TRIM_TIMEOUT_SECONDS,
        MALLOC_ARENA_MAX_ENV,
        MEMORY_PRESSURE_CRITICAL,
        MEMORY_PRESSURE_NORMAL,
        MEMORY_PRESSURE_WARN,
        HeapTrimMaintainer,
        MallocArenaCap,
        SubtreeSample,
        _glibc_mallopt,
        _linux_available_mib,
        _linux_current_rss_bytes,
        _linux_peak_rss_bytes,
        _MachTaskBasicInfo,
        _MachTimeValue,
        _macos_available_mib,
        _macos_current_rss_bytes,
        _malloc_trim,
        _peak_rss_from_status,
        _posix_peak_rss_bytes,
        _prev_win_sys_cpu,
        _proc_pss_kb,
        _ru_maxrss_bytes,
        _scale_ru_maxrss,
        _VMStatistics64,
        _windows_memory_counters,
        count_open_fds,
        host_available_mib,
        host_total_mib,
        limit_malloc_arenas,
        log_malloc_arena_cap,
        macos_vm_statistics,
        memory_pressure_level,
        memory_pressure_name,
        proc_cpu_nanos_for_pid,
        proc_cpu_seconds,
        proc_peak_rss_bytes,
        proc_rss_bytes,
        proc_rss_bytes_for_pid,
        proc_rss_tree_mb_for_pid,
        proc_subtree_sample,
        system_cpu_percent,
        system_memory,
        trim_heap_if_needed,
    )
    from kiro_crew.platform_compat_native import (  # noqa: F401
        _OWN_PGID,
        _SUBPROCESS_NO_WINDOW,
        _WINDOWS_PID_MAX,
        CREATE_NEW_PROCESS_GROUP,
        CREATE_SUSPENDED,
        DETACHED_PROCESS,
        SIGHUP,
        SIGKILL,
        SIGTERM,
        _close_process_handle,
        _IoCounters,
        _JobObjectBasicLimitInformation,
        _JobObjectExtendedLimitInformation,
        _MemoryStatusEx,
        _open_process_query_handle,
        _ProcessEntry32,
        _ProcessMemoryCounters,
        _SidAndAttributes,
        _ThreadEntry32,
        _windows_last_error,
    )
    from kiro_crew.platform_compat_ports import (  # noqa: F401
        _LOOPBACK_COVERING_ADDRESSES,
        _LSOF_TIMEOUT_SECS,
        _WINDOWS_ERROR_INSUFFICIENT_BUFFER,
        _WINDOWS_TCP_STATE_ESTABLISHED,
        _WINDOWS_TCP_STATE_LISTEN,
        _WINDOWS_TCP_TABLE_MAX_BYTES,
        _WINDOWS_TCP_TABLE_OWNER_PID_CONNECTIONS,
        _WINDOWS_TCP_TABLE_OWNER_PID_LISTENER,
        PortListener,
        _linux_loopback_listener_inodes,
        _linux_tcp_peer_pid,
        _macos_tcp_peer_pid,
        _normalize_local_address,
        _normalized_tcp_endpoint,
        _windows_loopback_listener_owner_pids,
        _windows_tcp_owner_rows,
        _WindowsTcpOwnerRow,
        address_covers_loopback,
        find_listening_pids,
        find_port_listeners,
        get_tcp_peer_pid,
        listening_pid_tool,
        listening_pid_tool_available,
        loopback_owner_pids,
        probe_port_listeners,
        process_owns_loopback_listener,
    )
    from kiro_crew.platform_compat_proctable import (  # noqa: F401
        _DARWIN_BSDINFO_SIZE,
        _DARWIN_CHILD_LIST_INITIAL,
        _DARWIN_CTL_KERN,
        _DARWIN_IN6_LOOPBACK,
        _DARWIN_IN6_V4MAPPED_PREFIX,
        _DARWIN_INI_IPV4,
        _DARWIN_INI_IPV6,
        _DARWIN_INSI_FADDR_OFFSET,
        _DARWIN_INSI_VFLAG_OFFSET,
        _DARWIN_KERN_PROC,
        _DARWIN_KERN_PROC_PGRP,
        _DARWIN_KERN_PROC_PID,
        _DARWIN_KERN_PROCARGS2,
        _DARWIN_KINFO_PGRP_ATTEMPTS,
        _DARWIN_KINFO_PGRP_SLACK,
        _DARWIN_KINFO_PROC_SIZE,
        _DARWIN_KP_PID_OFFSET,
        _DARWIN_KP_START_TVSEC_OFFSET,
        _DARWIN_KP_START_TVUSEC_OFFSET,
        _DARWIN_KP_STAT_OFFSET,
        _DARWIN_MAX_FDS,
        _DARWIN_MAXPATHLEN,
        _DARWIN_OFF_START_TVSEC,
        _DARWIN_OFF_START_TVUSEC,
        _DARWIN_PBI_PPID_OFFSET,
        _DARWIN_PBI_START_TVSEC_OFFSET,
        _DARWIN_PBI_START_TVUSEC_OFFSET,
        _DARWIN_PBI_STATUS_OFFSET,
        _DARWIN_PIDPATH_MAXSIZE,
        _DARWIN_PROC_BSDINFO_SIZE,
        _DARWIN_PROC_FDINFO_SIZE,
        _DARWIN_PROC_PIDFDSOCKETINFO,
        _DARWIN_PROC_PIDLISTFDS,
        _DARWIN_PROC_PIDTASKINFO,
        _DARWIN_PROC_PIDTBSDINFO,
        _DARWIN_PROC_PIDVNODEPATHINFO,
        _DARWIN_PROC_TASKINFO_SIZE,
        _DARWIN_PROC_VNODEPATHINFO_SIZE,
        _DARWIN_PROCARGS_BUFSIZE,
        _DARWIN_PROCARGS_ENV_BUFSIZE,
        _DARWIN_PROX_FDTYPE_SOCKET,
        _DARWIN_PTI_TOTAL_SYSTEM_OFFSET,
        _DARWIN_PTI_TOTAL_USER_OFFSET,
        _DARWIN_RI_PHYS_FOOTPRINT_OFFSET,
        _DARWIN_RUSAGE_INFO_V2,
        _DARWIN_RUSAGE_INFO_V2_SIZE,
        _DARWIN_SOCKET_FDINFO_SIZE,
        _DARWIN_SOCKINFO_TCP,
        _DARWIN_SOI_KIND_OFFSET,
        _DARWIN_SZOMB,
        _DARWIN_TCPSI_STATE_OFFSET,
        _DARWIN_TSI_S_ESTABLISHED,
        _DARWIN_VNODE_INFO_PATH_SIZE,
        _DARWIN_VNODE_INFO_SIZE,
        _LINUX_EXITED_STATES,
        _STAT_TOKEN_MAX_DIGITS,
        DarwinKinfoProc,
        DarwinProcessFacts,
        ProcessStartIdentity,
        ProcStat,
        _darwin_kinfo_proc_parse,
        _darwin_kinfo_query,
        _darwin_kinfo_size_mismatch_logged,
        _darwin_libc_sysctl,
        _darwin_libc_sysctl_loaded,
        _darwin_libc_sysctl_lock,
        _darwin_libproc,
        _darwin_libproc_fd_bound,
        _darwin_libproc_fd_handle,
        _darwin_libproc_handle,
        _darwin_libproc_loaded,
        _darwin_libproc_start_id,
        _darwin_libproc_tree_bound,
        _darwin_libproc_tree_handle,
        _darwin_peer_is_loopback,
        _darwin_process_cpu_nanos,
        _darwin_process_cwd,
        _darwin_process_phys_footprint_bytes,
        _darwin_process_start_identity,
        _darwin_process_start_microtime,
        _darwin_sysctl_handle,
        _DarwinLibprocUnavailable,
        _descriptors_on,
        _file_identity,
        _flock_acquirer_in_fdinfo,
        _flock_acquirer_in_proc_locks,
        _flock_acquirer_row,
        _linux_proc_root,
        _parse_cpu_jiffies,
        _parse_ppid,
        _proc_children,
        _proc_cpu_jiffies,
        _proc_status_rss_kb,
        _stat_token_int,
        _stat_tokens,
        darwin_child_pids,
        darwin_established_tcp_count,
        darwin_footprint_tree_mb,
        darwin_kinfo_proc,
        darwin_libproc_available,
        darwin_pgroup_members,
        darwin_pid_is_zombie,
        darwin_process_argv,
        darwin_process_environ,
        darwin_process_facts,
        darwin_process_path,
        flock_owner_pid,
        linux_pgroup_members,
        parent_pid,
        pids_holding_file,
        proc_child_map,
        proc_cpu_jiffies_for_pids,
        proc_phys_footprint_bytes_for_pid,
        read_proc_stat,
        read_proc_status_int,
    )
    from kiro_crew.platform_compat_runtime import (  # noqa: F401
        _EXIT_FLUSH_DEADLINE_SECS,
        _KEPT_FOR_REEXEC,
        _POST_DRAIN_EXEC_FAILURE_EXIT,
        _PYTHON_OPTIONS_WITH_VALUES,
        _UTF8_PROCESS_ENV,
        _WHEEL_APPLY_MODULE,
        _WINDOWS_ARM_MACHINES,
        BUNDLED_BACKEND_DIST_DIRNAME,
        _disarm_process_alarm_before_exec,
        _drain_event_log_for_exit,
        _ensure_utf8_process_environment,
        _is_windows_store_python_stub,
        arm_process_alarm,
        awake_now,
        boottime_now,
        cancel_wheel_applies_in_flight,
        ensure_line_buffered_stdout,
        ensure_utf8_console,
        execv_target_available,
        exit_after_failed_restart_exec,
        find_python_interpreter,
        hard_exit,
        is_bundled_interpreter,
        is_windows_on_arm,
        isolated_python_argv,
        keep_for_reexec,
        kept_for_reexec,
        nofile_soft_limit,
        process_age_secs,
        process_alarm_available,
        process_start_boot_secs,
        python_launcher_hops,
        raise_nofile_soft_limit,
        reexec_launcher,
        reexec_python_module,
        strftime,
    )
    from kiro_crew.platform_compat_trust import (  # noqa: F401
        _ACL_GROUP,
        _ACL_MASK,
        _ACL_USER,
        _ACL_WRITE,
        _ACL_XATTR_VERSION,
        _FOLDERID_DOCUMENTS,
        _LOCAL_SYSTEM_BIN_DIR,
        _MAX_SYMLINK_HOPS,
        _NO_ACL_ERRNOS,
        _POWERSHELL_USER_PROFILES,
        _TRUSTED_SYSTEM_BIN_DIRS,
        _UNPINNED_TOOL_PROBED,
        _UNTRUSTED_AWS_BIN_LOGGED,
        _WINDOWS_BIN_SUFFIXES,
        _WINDOWS_GIT_DIRS,
        _acl_admits_another_account,
        _folded_env_allowlist,
        _group_shared_with_another_account,
        _is_native_program,
        _is_root_owned_path,
        _local_aws_bin_candidate,
        _local_aws_bin_is_trusted,
        _log_tool_outside_trusted_dirs,
        _reveal_env,
        _root_owned_entry,
        _trusted_bin_search,
        _windows_system_dirs,
        aws_bin_declined_on_ownership,
        env_key_allowed,
        group_write_admits_another_account,
        open_with_default_app,
        reveal_in_file_manager,
        short_path_name,
        tool_outside_trusted_dirs,
        traversed_components,
        traversed_components_and_links,
        trusted_aws_bin,
        trusted_git_bin,
        trusted_system_bin,
        trusted_system_bin_quiet,
        trusted_system_path,
        windows_documents_dir,
        windows_powershell_profile_paths,
    )
    from kiro_crew.platform_compat_winproc import (  # noqa: F401
        _INVALID_HANDLE_VALUE,
        _JOB_OBJECT_LIMIT_ACTIVE_PROCESS,
        _JOB_OBJECT_LIMIT_JOB_MEMORY,
        _JOBOBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
        _RESUME_THREAD_FAILED,
        _TH32CS_SNAPTHREAD,
        _THREAD_SUSPEND_RESUME,
        _open_process_termination_handle,
        _windows_descendant_failure_details,
        _windows_foreign_descendant_pids,
        _windows_lineage_matches_lifetimes,
        _windows_process_query_creation,
        _windows_process_query_diagnostic,
        apply_job_limits,
        close_process_handle,
        descendant_termination_handles,
        descendant_termination_handles_async,
        duplicate_asyncio_process_handle,
        open_process_termination_handle,
        resume_process_main_thread,
    )
    from kiro_crew.platform_compat_wintree import (  # noqa: F401
        _PENDING_WINDOWS_TREE_CLEANUPS,
        _PENDING_WINDOWS_TREE_CLEANUPS_LOCK,
        _PENDING_WINDOWS_TREE_RETRY_LIMIT,
        _WINDOWS_CAPTURE_MEASURED_VERSIONS,
        _WINDOWS_TERMINATE_REFUSAL_WAIT_MS,
        _WINDOWS_TREE_ADMISSIONS,
        _WINDOWS_TREE_REAP_POLL_SECS,
        _WINDOWS_TREE_REAP_TIMEOUT_SECS,
        WindowsTreeDrainPending,
        _advance_owned_windows_tree,
        _bind_windows_cleanup_process,
        _create_windows_subprocess_owned,
        _drain_windows_process_tree,
        _manual_windows_tree,
        _PendingWindowsTreeCleanup,
        create_windows_cleanup_owned_process,
        finish_windows_cleanup_owned_spawn,
        process_handle_active,
        release_windows_tree_reservation,
        reserve_windows_tree_cleanup,
        retry_pending_windows_process_trees,
        terminate_process_handle,
        terminate_windows_asyncio_tree,
        terminate_windows_process_tree_owned,
        windows_tree_cleanup_pending,
    )
    from kiro_crew.platform_lock_compat import (  # noqa: F401
        _LOCK_POLL_MAX_SECS,
        _LOCK_POLL_SECS,
        _LOCK_TIMEOUT_SECS,
        _WIN_LOCK_POLL_SECS,
        _WIN_LOCK_TIMEOUT_SECS,
        _lock_timeout_message,
        _on_event_loop,
        _posix_acquire_blocking,
        _win_acquire_blocking,
        acquire_lock,
        file_lock,
        flock_exclusive,
        open_create_or_existing,
        open_lock_file,
        probe_file_persistence,
        release_lock,
        tempfile,
        try_acquire_lock,
        try_acquire_lock_or_raise,
    )
    from kiro_crew.platform_owner_compat import (  # noqa: F401
        _OWNER_RIGHTS_SID,
        _TOKEN_QUERY,
        _TOKEN_SID_CACHE,
        _TOKEN_USER_CLASS,
        _apply_owner_only_dacl,
        _process_token_sid,
        _process_token_sid_unguarded,
        current_user_sid,
        local_user_id,
        make_owner_only_dir,
        path_writable_by_current_user,
        process_owner_sid,
        restrict_dir_to_owner,
        restrict_to_owner,
        stat_owned_by_current_user,
        stat_writable_by_current_user,
    )
