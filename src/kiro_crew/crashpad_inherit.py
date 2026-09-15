"""Stop Electron's Crashpad handler from following us into every child process.

Problem
-------
The desktop shell starts Electron's ``crashReporter`` (``native-logging.js``)
so a crash of the app itself leaves a minidump behind. On macOS Crashpad
registers itself as the task's Mach exception port for ``EXC_CRASH`` and
``EXC_RESOURCE``. Mach exception ports are INHERITED across ``fork``/``exec``,
so every process the gateway launches — kiro-cli, node, an MCP server, and the
``brazil``/``ruby``/``python``/``chrome-headless-shell`` those in turn spawn —
still reports to *our* handler, which faithfully writes *their* dumps into
*our* ``Crashpad/pending/``. Measured on one developer machine: 585 dumps,
zero of them this app, 539 of them a Ruby child aborting on ``fork()`` in its
own ``at_exit`` (``objc_initializeAfterForkError``), a dozen an hour, forever.

That is not a diagnostic; it is a disk leak (Crashpad with ``uploadToServer``
off never prunes ``pending/``), and it makes the crash collector do real work
every launch to prove each dump is somebody else's.

Fix
---
Clear the two inherited exception ports on THIS task (the gateway) before any
child is spawned. ``task_set_exception_ports(mach_task_self(), mask, MACH_PORT_NULL, …)``
resets the entries to the null port, and the null port is what our children then
inherit — so a child crash is handled by the ordinary path (the kernel's
``ReportCrash`` writes an ``.ips`` under the child's OWN name, exactly as if the
process had been launched from a terminal). The Electron parent is untouched:
its port is a per-task setting, and only the gateway's task is edited.

Scope
-----
macOS only; every other platform is a no-op. Best-effort: a failure to load
libSystem or a non-zero ``kern_return_t`` is logged and ignored — inheriting a
crash handler is a nuisance, and refusing to start over it would be worse.
"""

from __future__ import annotations

import ctypes
import logging
import sys

logger = logging.getLogger(__name__)

# <mach/exception_types.h>: EXC_MASK_<type> == 1 << EXC_<type>.
EXC_CRASH = 10
EXC_RESOURCE = 11
EXC_MASK_CRASH = 1 << EXC_CRASH
EXC_MASK_RESOURCE = 1 << EXC_RESOURCE
# The two masks Crashpad claims (crashpad/util/mach/exc_server_variants.h,
# ExcServerSuccessfulReturnValue + CrashpadClient::SetHandlerMachPort). Nothing
# else in the app registers exception ports, so nothing else is disturbed.
CRASHPAD_MASKS = EXC_MASK_CRASH | EXC_MASK_RESOURCE

MACH_PORT_NULL = 0
EXCEPTION_DEFAULT = 1
THREAD_STATE_NONE = 0
_KERN_SUCCESS = 0


def detach_inherited_crash_handler(*, platform: str = sys.platform) -> bool:
    """Reset the inherited Crashpad exception ports on this task.

    Returns True when the ports were cleared, False when nothing was done
    (not macOS, libSystem unavailable, or the Mach call failed). Never raises.
    """
    if platform != "darwin":
        return False
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.dylib", use_errno=True)
    except OSError as exc:
        logger.debug("crashpad detach: libSystem unavailable: %s", exc)
        return False
    try:
        libc.mach_task_self.restype = ctypes.c_uint32
        libc.mach_task_self.argtypes = []
        libc.task_set_exception_ports.restype = ctypes.c_int32
        libc.task_set_exception_ports.argtypes = [
            ctypes.c_uint32,  # task_t
            ctypes.c_uint32,  # exception_mask_t
            ctypes.c_uint32,  # mach_port_t new_port
            ctypes.c_int32,  # exception_behavior_t
            ctypes.c_int32,  # thread_state_flavor_t
        ]
        # mach_task_self() is a name the task owns, not a fresh send right; it
        # must not be deallocated (same rule as _macos_current_rss_bytes).
        kern_return = libc.task_set_exception_ports(
            libc.mach_task_self(),
            CRASHPAD_MASKS,
            MACH_PORT_NULL,
            EXCEPTION_DEFAULT,
            THREAD_STATE_NONE,
        )
    except (AttributeError, OSError, ValueError) as exc:
        logger.debug("crashpad detach: Mach call unavailable: %s", exc)
        return False
    if kern_return != _KERN_SUCCESS:
        logger.debug("crashpad detach: task_set_exception_ports kr=%d", kern_return)
        return False
    logger.debug("crashpad detach: cleared inherited EXC_CRASH|EXC_RESOURCE ports")
    return True
