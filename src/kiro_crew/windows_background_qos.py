"""Mark a spawned Windows agent tree as background work (opt-in).

A sub-agent's ``git`` or test run otherwise competes on equal terms with the
turn a person is watching: every process Crew starts runs at Normal priority
with no scheduling hint. When ``agent.windows_background_qos`` is on, a runtime
whose role is background work (the caller passes ``background=True`` to
``sandbox.apply_windows_resource_ceiling``) gets three hints at spawn, while it is
still suspended:

* a Job object CPU weight below the Windows default (5), set beside the Job's
  memory and process ceilings, which lowers the tree's share only under
  contention and covers every descendant;
* the ``BELOW_NORMAL`` priority class, which its children inherit;
* EcoQoS (``SetProcessInformation(ProcessPowerThrottling)``), the hint Task
  Manager's Efficiency mode sets, on the process Crew starts.

None is a cap. The priority class and the Job weight matter only when the CPU
is contended; EcoQoS asks Windows to lower clocks and prefer efficiency cores
whether or not it is, so it can slow the work on an idle machine. Every call
fails soft, so a Windows build without the API logs once and the spawn carries
on. Nothing here runs off Windows.

This module sits below ``platform_compat_winproc`` (which calls
:func:`set_job_cpu_weight`), so it imports only ``platform_compat_native``.
"""

from __future__ import annotations

import ctypes
import logging
from typing import Any

from kiro_crew.platform_compat_native import IS_WINDOWS, _windows_last_error

logger = logging.getLogger(__name__)

#: Job CPU weight for a background tree. Windows weights run 1-9 with 5 the
#: default a job without rate control is treated as having.
BACKGROUND_JOB_CPU_WEIGHT = 3

_PROCESS_SET_INFORMATION = 0x0200
_BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
# PROCESS_INFORMATION_CLASS.ProcessPowerThrottling
_PROCESS_POWER_THROTTLING_CLASS = 4
_PROCESS_POWER_THROTTLING_CURRENT_VERSION = 1
_PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1
# JOBOBJECTINFOCLASS.JobObjectCpuRateControlInformation
_JOB_OBJECT_CPU_RATE_CONTROL_CLASS = 15
_JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x1
_JOB_OBJECT_CPU_RATE_CONTROL_WEIGHT_BASED = 0x2


class _ProcessPowerThrottlingState(ctypes.Structure):
    """``PROCESS_POWER_THROTTLING_STATE``: three ULONGs (32-bit on Windows)."""

    _fields_ = [
        ("Version", ctypes.c_uint32),
        ("ControlMask", ctypes.c_uint32),
        ("StateMask", ctypes.c_uint32),
    ]


class _JobCpuRateControlInformation(ctypes.Structure):
    """``JOBOBJECT_CPU_RATE_CONTROL_INFORMATION`` with its union read as ``Weight``."""

    _fields_ = [("ControlFlags", ctypes.c_uint32), ("Weight", ctypes.c_uint32)]


_ecoqos_unavailable_logged = False


def enabled_in_config() -> bool:
    """``agent.windows_background_qos``, ``config.local.json`` over ``config.json``.

    Only a real JSON ``true`` turns it on, as ``_safe_bool`` reads the key; False
    when unreadable. The overlay wins, as it does for the loader and Settings.
    """
    try:
        # Lazy: config.loader imports far above this module.
        from kiro_crew.config.loader import (
            config_local_path,
            config_path,
            read_merged_config_files,
        )

        agent = read_merged_config_files(config_path(), config_local_path()).get("agent")
    except Exception:
        logger.debug("windows_background_qos: config unavailable")
        return False
    return isinstance(agent, dict) and agent.get("windows_background_qos") is True


def set_job_cpu_weight(kernel32: Any, job: Any, weight: int) -> bool:
    """Give *job* a weight-based CPU share of *weight* (1-9). Never raises.

    *kernel32* is the caller's already-bound ``kernel32`` (its
    ``SetInformationJobObject`` argtypes set), so a test can pass a fake.
    """
    if not 1 <= weight <= 9:
        logger.debug("set_job_cpu_weight: weight %s outside 1-9, not set", weight)
        return False
    info = _JobCpuRateControlInformation(
        ControlFlags=(
            _JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | _JOB_OBJECT_CPU_RATE_CONTROL_WEIGHT_BASED
        ),
        Weight=weight,
    )
    try:
        ok = bool(
            kernel32.SetInformationJobObject(
                job,
                _JOB_OBJECT_CPU_RATE_CONTROL_CLASS,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
        )
    except Exception:
        logger.warning("Job CPU weight could not be set", exc_info=True)
        return False
    if not ok:
        logger.warning("Job CPU weight could not be set (err=%s)", _windows_last_error())
    return ok


def _bound_kernel32() -> Any:  # pragma: no cover - Windows-only ctypes binding
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.SetPriorityClass.restype = wintypes.BOOL
    kernel32.SetProcessInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.SetProcessInformation.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def apply_background_qos(pid: int, *, kernel32: Any = None) -> bool:
    """Set *pid* to ``BELOW_NORMAL`` and turn EcoQoS on. Never raises.

    Returns whether the priority class was set; EcoQoS is best effort on top of
    it (``SetProcessInformation`` is absent before Windows 10 1709 and rejects
    the class on some builds), logged once when it fails. False off Windows.
    *kernel32* is injectable for tests; the real one is bound here otherwise.
    """
    global _ecoqos_unavailable_logged
    if kernel32 is None:
        if not IS_WINDOWS:
            return False
        try:  # pragma: no cover - Windows-only
            kernel32 = _bound_kernel32()
        except Exception:  # pragma: no cover - Windows-only
            logger.warning("background QoS: kernel32 unavailable", exc_info=True)
            return False
    try:
        handle = kernel32.OpenProcess(_PROCESS_SET_INFORMATION, False, pid)
    except Exception:
        logger.warning("background QoS: OpenProcess failed for pid %d", pid, exc_info=True)
        return False
    if not handle:
        logger.warning(
            "background QoS: OpenProcess failed for pid %d (err=%s)", pid, _windows_last_error()
        )
        return False
    try:
        priority_set = bool(kernel32.SetPriorityClass(handle, _BELOW_NORMAL_PRIORITY_CLASS))
        if not priority_set:
            logger.warning(
                "background QoS: SetPriorityClass failed for pid %d (err=%s)",
                pid,
                _windows_last_error(),
            )
        state = _ProcessPowerThrottlingState(
            Version=_PROCESS_POWER_THROTTLING_CURRENT_VERSION,
            ControlMask=_PROCESS_POWER_THROTTLING_EXECUTION_SPEED,
            StateMask=_PROCESS_POWER_THROTTLING_EXECUTION_SPEED,
        )
        if (
            not kernel32.SetProcessInformation(
                handle,
                _PROCESS_POWER_THROTTLING_CLASS,
                ctypes.byref(state),
                ctypes.sizeof(state),
            )
            and not _ecoqos_unavailable_logged
        ):
            _ecoqos_unavailable_logged = True
            logger.info(
                "background QoS: EcoQoS unavailable on this Windows build (err=%s); "
                "priority and Job CPU weight still apply",
                _windows_last_error(),
            )
        if priority_set:
            logger.info("background QoS applied to pid %d", pid)
        return priority_set
    except Exception:
        logger.warning("background QoS failed for pid %d", pid, exc_info=True)
        return False
    finally:
        try:
            kernel32.CloseHandle(handle)
        except Exception:
            logger.debug("CloseHandle failed", exc_info=True)
