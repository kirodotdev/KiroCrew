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

import asyncio
import contextlib
import ctypes
import ctypes.util
import errno
import functools
import hashlib
import importlib
import json
import logging
import os
import re
import select
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import sys as _sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path
from types import ModuleType  # noqa: F401
from typing import TYPE_CHECKING, Literal, NamedTuple  # noqa: F401

import kiro_crew as _kiro_crew_pkg  # noqa: F401
from kiro_crew import (
    _facade_mirror,
    platform_compat,
    sandbox_launcher,
    sandbox_mount_sweep,
    sandbox_plan,
    sandbox_seatbelt,
)
from kiro_crew.atomic_write import fsync_dir, refuse_linked_parent  # noqa: F401
from kiro_crew.config.paths import config_dir, kiro_agents_dir  # noqa: F401
from kiro_crew.constants import (
    KIROCREW_SANDBOX_TOOL_ENV,
    KIROCREW_SANDBOX_TOOL_VALUE,
    KIROCREW_SPAWNED_ENV,
    KIROCREW_SPAWNED_VALUE,
)
from kiro_crew.identity_stores import AUTH_SQLITE_DB, AUTH_SQLITE_SIDECAR_SUFFIXES  # noqa: F401
from kiro_crew.pinned_fs import fd_real_path  # noqa: F401
from kiro_crew.platform import current_context
from kiro_crew.terminal_safe import safe_terminal_line

try:
    import resource as _resource_mod
except ImportError:  # non-POSIX (Windows)
    _resource_mod = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from concurrent.futures import ThreadPoolExecutor
    from typing import Any

logger = logging.getLogger(__name__)

# Launcher scripts and seatbelt profiles are read exactly once at child exec.
# Any file older than this threshold is garbage regardless of PID liveness.
_LAUNCHER_MAX_AGE_SECONDS = 3600

#: Artifact families the sweep reclaims, by filename prefix -> accepted
#: suffixes. Every family tags the writing process's PID after the prefix.
_SANDBOX_ARTIFACT_PREFIX = "kirocrew_sandbox_"
#: The two artifacts written under that prefix, each named once. Three readers depend on
#: these agreeing: the ``mkstemp`` calls that write them, the run-dir sweep's accepted
#: suffixes below, and ``session_pid``'s managed-agent gate, which imports both to tell
#: the Linux launcher apart from the macOS seatbelt profile sitting beside it under the
#: same prefix.
_LAUNCHER_SCRIPT_SUFFIX = ".py"
_SEATBELT_PROFILE_SUFFIX = ".sb"
_PI_GATE_ARTIFACT_PREFIX = "kirocrew_pi_gate_"
# Named ONCE because two sweeps accept this family: the run dir still holds artifacts
# written before they moved, and the gate dir holds the current ones. Two spellings could
# drift and leave one of those directories unswept. ``.tmp`` is the mkstemp stage both pi
# artifacts pass through before publication. ``.json`` is the per-session MCP handoff
# (``kirocrew_pi_gate_<pid>_mcp_<nonce>.json``), removed once its session is established;
# the sweep reclaims one a dead gateway left behind.
_PI_GATE_ARTIFACT_SUFFIXES: tuple[str, ...] = (".sh", ".cmd", ".ts", ".tmp", ".json")
_RUN_DIR_ARTIFACTS: dict[str, tuple[str, ...]] = {
    _SANDBOX_ARTIFACT_PREFIX: (_SEATBELT_PROFILE_SUFFIX, _LAUNCHER_SCRIPT_SUFFIX),
    # The run sweep accepts pi artifacts as well as sandbox launchers.
    _PI_GATE_ARTIFACT_PREFIX: _PI_GATE_ARTIFACT_SUFFIXES,
}
# The DeepSeek Harness gate shares the pi-gate leaf: the sealed plugin
# (``kirocrew_dsh_gate_<pid>.mjs``), its per-launch patch
# (``kirocrew_dsh_gate_<pid>.patch.yml``) and the mkstemp stages both are
# renamed from (``kirocrew_dsh_gate_<pid>_*.tmp`` / ``kirocrew_dsh_patch_<pid>_*.tmp``).
# Like the pi artifacts they are written once per gateway process and reused, so
# the PID in the name is the owner's own and liveness, not age, decides staleness.
_DSH_GATE_ARTIFACT_PREFIX = "kirocrew_dsh_gate_"
_DSH_PATCH_ARTIFACT_PREFIX = "kirocrew_dsh_patch_"
_DSH_GATE_ARTIFACT_SUFFIXES: tuple[str, ...] = (".mjs", ".patch.yml", ".tmp")
_PI_GATE_DIR_ARTIFACTS: dict[str, tuple[str, ...]] = {
    _PI_GATE_ARTIFACT_PREFIX: _PI_GATE_ARTIFACT_SUFFIXES,
    _DSH_GATE_ARTIFACT_PREFIX: _DSH_GATE_ARTIFACT_SUFFIXES,
    _DSH_PATCH_ARTIFACT_PREFIX: (".tmp",),
}

# Legacy sandbox launcher directory (before migration to <config_dir>/run/).
_LEGACY_LAUNCHER_DIR = "/tmp"

# The carved owners' names this module's own code reads. They are imported one per
# line, below the module constants, so a line diff of the split keeps those
# constants as unchanged context.
from kiro_crew.sandbox_aliases import _referent_identity  # noqa: E402
from kiro_crew.sandbox_aliases import _refuse_aliased_masked_leaves  # noqa: E402
from kiro_crew.sandbox_aliases import _refuse_multilinked_credential_leaves  # noqa: E402
from kiro_crew.sandbox_leaves import _CREW_HIDDEN_DIRS  # noqa: E402
from kiro_crew.sandbox_leaves import _CREW_HIDDEN_LEAVES  # noqa: E402
from kiro_crew.sandbox_leaves import _CREW_READONLY_LEAVES  # noqa: E402
from kiro_crew.sandbox_leaves import _CREW_READONLY_TARGETS  # noqa: E402
from kiro_crew.sandbox_leaves import _CREW_UNREADABLE_MASK_LEAVES  # noqa: E402
from kiro_crew.sandbox_leaves import _RELOCATED_CREW_HIDDEN_LEAVES  # noqa: E402
from kiro_crew.sandbox_precreate import _DELEGATED_OVERLAP_LEAF_REASONS  # noqa: E402
from kiro_crew.sandbox_seals import SandboxCeilingUnsealable  # noqa: E402
from kiro_crew.sandbox_seals import _materialize_sealable_ceilings  # noqa: E402
from kiro_crew.sandbox_seals import _refuse_linked_models_leaf  # noqa: E402
from kiro_crew.sandbox_seals import _warn_aliased_strict_leaves  # noqa: E402
from kiro_crew.sandbox_staging import _materialize_live_target_mask_target  # noqa: E402
from kiro_crew.sandbox_staging import _materialize_maskable_dirs  # noqa: E402
from kiro_crew.sandbox_staging import _materialize_md_notebook_mask_targets  # noqa: E402
from kiro_crew.sandbox_staging import _md_notebook_degraded_mask_dirs  # noqa: E402
from kiro_crew.sandbox_staging import _reconcile_auth_store_staging_links  # noqa: E402
from kiro_crew.sandbox_staging import _sweep_legacy_auth_store_temps  # noqa: E402
from kiro_crew.sandbox_staging import _sweep_legacy_md_notebook_temps  # noqa: E402
from kiro_crew.sandbox_tiers import _AGENT_DENIED_ENV_KEYS  # noqa: E402
from kiro_crew.sandbox_tiers import _CC_EXPOSE_FILES  # noqa: E402
from kiro_crew.sandbox_tiers import _CC_FILES  # noqa: E402
from kiro_crew.sandbox_tiers import _IN_SANDBOX_LEVEL_VAR  # noqa: E402
from kiro_crew.sandbox_tiers import _IN_SANDBOX_MARKER  # noqa: E402
from kiro_crew.sandbox_tiers import _POD_OS_HOME_GRANT_STORE_LEAVES  # noqa: E402
from kiro_crew.sandbox_tiers import _POD_OS_HOME_MASKED_SUBLEAVES  # noqa: E402
from kiro_crew.sandbox_tiers import _PYTHON_ENV_PREFIXES  # noqa: E402
from kiro_crew.sandbox_tiers import _SANDBOX_MODE_ALIASES  # noqa: E402
from kiro_crew.sandbox_tiers import _SENSITIVE_ENV_PREFIXES  # noqa: E402
from kiro_crew.sandbox_tiers import _STANDARD_DIRS  # noqa: E402
from kiro_crew.sandbox_tiers import _TIER_ORDINALS  # noqa: E402
from kiro_crew.sandbox_tiers import _clamp_sandbox_mode_to_floor  # noqa: E402
from kiro_crew.sandbox_tiers import _crew_home_alias_roots  # noqa: E402
from kiro_crew.sandbox_tiers import _floor_mandates_sandbox  # noqa: E402
from kiro_crew.sandbox_tiers import _governance_sandbox_floor  # noqa: E402
from kiro_crew.sandbox_tiers import _mode_to_level  # noqa: E402
from kiro_crew.sandbox_tiers import _relocated_crew_targets  # noqa: E402
from kiro_crew.sandbox_tiers import _relocated_policy_cache_dirs  # noqa: E402
from kiro_crew.sandbox_tiers import _resolved_install_root_targets  # noqa: E402
from kiro_crew.sandbox_tiers import _resolved_kiro_agents_targets  # noqa: E402
from kiro_crew.sandbox_tiers import _sandbox_policy  # noqa: E402
from kiro_crew.sandbox_tiers import effective_sandbox_mode  # noqa: E402
from kiro_crew.sandbox_workspace import _directory_ancestor_identities  # noqa: E402
from kiro_crew.sandbox_workspace import _literal_ancestor_guards  # noqa: E402
from kiro_crew.sandbox_workspace import _open_directory_descriptor  # noqa: E402
from kiro_crew.sandbox_workspace import _voice_runtime_ancestor_guards  # noqa: E402
from kiro_crew.sandbox_workspace import _voice_runtime_parent_paths  # noqa: E402
from kiro_crew.sandbox_workspace import _voice_runtime_sandbox_paths  # noqa: E402

_is_policy_cache_dir = sandbox_plan.is_policy_cache_dir


_hidden_path_contains_visible_path = sandbox_plan.hidden_path_contains_visible_path


# ── Availability probes ──


# unshare(2) flags for the userns probe.
_CLONE_NEWUSER = 0x10000000
_CLONE_NEWNS = 0x00020000
# mount(2) flags for the probe's propagation step: the launcher's first mount is
# ``mount(NULL, "/", NULL, MS_REC|MS_PRIVATE, NULL)`` inside the fresh mount
# namespace, so the probe performs exactly that call and nothing else.
_PROBE_MS_REC = 0x4000
_PROBE_MS_PRIVATE = 1 << 18

# Errnos that indicate a TRANSIENT resource failure (fork/CDLL under momentary
# pressure) — the kernel supports user namespaces, we just couldn't verify it
# right now. These must never be treated as "this host has no sandbox backend"
# (one EAGAIN during a cron spawn burst would otherwise fail-close every
# subsequent spawn because the failed probe result was cached).
_TRANSIENT_PROBE_ERRNOS = frozenset(
    {errno.EAGAIN, errno.ENOMEM, errno.EMFILE, errno.ENFILE, errno.ENOSPC}
)

# Delay before the single in-probe retry on a transient failure.
_PROBE_TRANSIENT_RETRY_DELAY_SECS = 0.05

# Ceiling on how long a blocking ``warm_backend`` waits for the probe to land.
# The probe itself is a fork + unshare (sub-millisecond) and the warm thread
# makes at most two attempts separated by one _PROBE_TRANSIENT_RETRY_DELAY_SECS
# sleep, so this is orders of magnitude of slack rather than a tuned value — it
# exists so a wedged probe cannot stall boot indefinitely. Exceeding it is not
# an error: the cache stays cold and the self-healing transient path applies.
_WARM_JOIN_TIMEOUT_SECS = 2.0

# Steps of the launcher's namespace handshake, named in probe failure reasons so
# a caller can tell the host mechanisms apart instead of seeing a bare errno: a
# NEWNS denial is Ubuntu's AppArmor userns restriction, while NEWUSER with
# ENOSPC/EUSERS is a hardened user.max_user_namespaces=0. ENOSPC is ALSO what
# momentary fd/disk pressure looks like, so the cap verdict stays TRANSIENT and
# is never cached; the remedy travels with it so a host at a cap of 0 — which is
# reported transient forever — still gets told which sysctl to raise.
#
# The third step is the launcher's FIRST mount: making propagation on ``/``
# private inside the new mount namespace. Both unshare calls can succeed while
# that mount is refused — a container whose runtime applies its default AppArmor
# profile (``deny mount``) but no seccomp filter, the Kubernetes default on an
# AppArmor node — and a probe that stopped at the unshares reported such a host
# sandbox-capable, so every real spawn then died in the launcher with
# ``sandbox: BLOCKED -- making mount propagation private on / failed: errno 13``
# and was misread as a broken CLI. The probe therefore performs exactly that
# mount, in a namespace that dies with the probe child, and nothing more.
_PROBE_STEP_NEWUSER = "unshare(CLONE_NEWUSER)"
_PROBE_STEP_NEWNS = "unshare(CLONE_NEWNS)"
_PROBE_STEP_MOUNT_PRIVATE = "mount(MS_REC|MS_PRIVATE) on /"
#: Wire step the probe child sends INSTEAD of "U" when its ``CLONE_NEWUSER`` EINVAL
#: is explained by the child having been multithreaded, carrying the thread count.
#: The parent classifies it exactly as it classifies a plain EINVAL -- the step
#: exists to carry the explanation, not to change the verdict. See
#: ``_probe_child_thread_count``.
_PROBE_STEP_MULTITHREADED = "M"

#: Trailing clause of the reason `_probe_parent_sequence` emits for the
#: `_PROBE_STEP_MULTITHREADED` collapse. A single shared spelling, because callers
#: that must RECOGNIZE the collapse (a fork child whose verdict is unobtainable is
#: an unknown reading, not a disagreement) match against the reason text -- a
#: hand-copied substring would drift the moment the wording changes.
_PROBE_MULTITHREADED_REASON = (
    "an os.register_at_fork hook started one, so the kernel's own verdict is "
    "unobtainable from this child"
)


def _probe_reason_is_multithreaded_collapse(reason: str) -> bool:
    """Whether a probe reason reports the multithreaded-fork-child collapse.

    True only for the fork path: the collapse text is emitted by
    `_probe_parent_sequence` when the probe child counted more than one thread,
    which happens when an ``os.register_at_fork`` hook armed earlier in the
    calling process starts a thread inside every fork child. The verdict such a
    child returns is the hook's artifact, not the kernel's answer.
    """
    return _PROBE_MULTITHREADED_REASON in (reason or "")


# A probe child that vanished mid-handshake is a harness failure, not a kernel
# verdict, so it must not be cached as "this host has no sandbox". Kept separate
# from _TRANSIENT_PROBE_ERRNOS so that set's cache semantics stay untouched.
# ESRCH/ENOENT surface when opening /proc/<pid>/... for a dead child; EPIPE
# surfaces when releasing a child that died after the maps were written.
_PROBE_CHILD_GONE_ERRNOS = frozenset({errno.ESRCH, errno.ENOENT, errno.EPIPE})

# Upper bound on the probe's pipe handshake. The real exchange is
# sub-millisecond; this only stops a pathological child from wedging the
# background warm thread forever.
_PROBE_HANDSHAKE_TIMEOUT_SECS = 5.0

# Upper bound on the macOS sandbox-backend probe (`sandbox-exec` running
# /usr/bin/true under an allow-default profile) that backend detection runs.
_SANDBOX_BACKEND_PROBE_TIMEOUT_SECS: float = 5.0

# Detail of the most recent failed userns probe: (transient, reason, remedy).
# ``None`` means the last probe succeeded (or none has run yet). Consumed by
# detect_backend() for cache policy and by wrap_argv() for error reporting.
#
# One value, swapped atomically, so a reader always gets a reason and the remedy
# from the SAME probe. Holding the remedy in a second global would let a
# concurrent re-probe land between the two reads and pair one probe's failure
# with another's mechanism, which is the wrong fix presented as the right one.
_last_unshare_failure: tuple[bool, str, str] | None = None

# ── Remedy tokens for a Linux user-namespace denial ──
# The probe already knows WHICH step failed and with which errno, and those two
# facts identify the host mechanism (see the table in docs/guides/install.md).
# A reason string alone would leave every presentation layer showing a bare
# ``errno 1 (EPERM)`` and no way forward.
# These tokens carry the mechanism out to callers machine-readably, so the
# dashboard, doctor and logs can each render their own remedy copy instead of
# pattern-matching English prose out of the detail.
#
# A token travels IN-BAND: every probe step returns it alongside its verdict, so
# it is never module state that a second probe could overwrite. ``""`` means the
# failure identifies no mechanism — a harness failure, a non-Linux host, or a
# deferred on-loop probe.
REMEDY_APPARMOR_USERNS = "apparmor_userns"  # Ubuntu >= 23.10 restricted profile
REMEDY_MAX_USER_NAMESPACES = "max_user_namespaces"  # user.max_user_namespaces=0
REMEDY_NO_USER_NS = "no_user_ns"  # kernel built without CONFIG_USER_NS
REMEDY_USERNS_DENIED = "userns_denied"  # userns creation refused outright
REMEDY_MOUNT_DENIED = "mount_denied"  # namespaces work; mount(2) refused inside them


def _remedy_for_step(label: str, err: int) -> str:
    """Name the host mechanism behind one failed probe step.

    ``label`` is one of the ``_PROBE_STEP_*`` constants for a real kernel
    verdict; any other label is a harness failure (fork/pipe under pressure)
    which says nothing about the host and therefore has no remedy.

    A NEWNS denial is only reachable AFTER NEWUSER succeeded, which is the
    signature of Ubuntu's restricted-profile restriction rather than of userns
    being unavailable — the distinction that decides whether the fix is an
    AppArmor profile or a sysctl.

    A refused propagation mount is only reachable after BOTH unshares succeeded:
    the process holds every capability inside its new user namespace, so the
    kernel itself never refuses this call — a policy layer did. EACCES is
    AppArmor's answer (the container runtimes' default profile carries ``deny
    mount``); EPERM is a seccomp filter's. Both are permanent for the life of
    the container and share one remedy: relax that policy, or accept the
    container as the only isolation boundary.
    """
    if label == _PROBE_STEP_MOUNT_PRIVATE:
        return REMEDY_MOUNT_DENIED if err in (errno.EACCES, errno.EPERM) else ""
    if label == _PROBE_STEP_NEWNS:
        return REMEDY_APPARMOR_USERNS if err == errno.EPERM else ""
    if label != _PROBE_STEP_NEWUSER:
        return ""
    if err in (errno.ENOSPC, errno.EUSERS):
        return REMEDY_MAX_USER_NAMESPACES
    if err in (errno.EINVAL, errno.ENOSYS):
        return REMEDY_NO_USER_NS
    if err == errno.EPERM:
        return REMEDY_USERNS_DENIED
    return ""


# Concrete, mechanism-specific first line for the ``no_backend`` guidance in a
# SandboxUnavailableError message. Kept as prose here (rather than only as a
# token) because logs, doctor and the Slack surface all read the message text —
# only the dashboard consumes the token and renders its own translated copy.
_LINUX_REMEDY_GUIDANCE = {
    REMEDY_APPARMOR_USERNS: (
        "This host looks like Ubuntu 23.10 or newer with "
        "kernel.apparmor_restrict_unprivileged_userns=1: the user namespace was "
        "created, then the mount namespace was denied because the restricted "
        "AppArmor profile carries no CAP_SYS_ADMIN. Run `kirocrew service "
        "install` to install the narrow kirocrew-userns AppArmor profile (it "
        "grants only `userns`). The profile is attached by path to the kirocrew "
        "launcher, so any launch through that launcher is confined, a foreground "
        "`kirocrew gateway` included; other entry points stay unconfined, and "
        "`aa-exec -p` cannot fix that for an unprivileged user because entering "
        "a named profile needs privilege and aa-exec execs unconfined rather "
        "than failing. The desktop app reuses a gateway already listening on the "
        "port, so installing the service covers that install too. Do NOT set the "
        "sysctl to 0 — that removes a kernel-wide protection to satisfy one app. "
    ),
    REMEDY_MAX_USER_NAMESPACES: (
        "User namespace creation hit the per-user cap, which usually means "
        "user.max_user_namespaces=0 (a CIS-hardened default). Raise that sysctl. "
    ),
    REMEDY_NO_USER_NS: (
        "The kernel rejected the user namespace outright, which means it was "
        "built without CONFIG_USER_NS. There is no host-level fix short of a "
        "different kernel. "
    ),
    REMEDY_USERNS_DENIED: (
        "User namespace creation was refused. On Debian-family hosts check "
        "kernel.unprivileged_userns_clone (it must be 1); inside a container "
        "this is usually the container's own seccomp filter denying unshare, "
        "which is fixed with container run flags rather than host config. "
    ),
    REMEDY_MOUNT_DENIED: (
        "The user and mount namespaces were created, but making mount "
        "propagation private on / inside them was refused, so the sandbox "
        "cannot hide anything. Nothing in the kernel refuses this to a process "
        "that owns the namespace: a policy layer did. Inside a container that is "
        "the runtime's default AppArmor profile (it carries `deny mount`; "
        "EACCES) or a seccomp filter without the mount family (EPERM). Run the "
        "container with AppArmor unconfined (docker: --security-opt "
        "apparmor=unconfined; Kubernetes 1.30+: securityContext.appArmorProfile.type "
        "Unconfined, earlier versions the "
        "container.apparmor.security.beta.kubernetes.io/<container>: unconfined "
        "annotation) and a seccomp profile that permits unshare and mount — no "
        "root or CAP_SYS_ADMIN is needed — or accept the container as the only "
        "isolation boundary with agent.sandbox_allow_unsandboxed_exec=true. "
    ),
}


def _linux_remedy_guidance(remedy: str) -> str:
    """Mechanism-specific guidance prefix for a remedy token (``""`` if none)."""
    return _LINUX_REMEDY_GUIDANCE.get(remedy, "")


def unavailable_remedy() -> str:
    """Public: remedy token for the most recent sandbox probe failure.

    ``""`` when the last probe succeeded, when none has run, or when the failure
    identifies no host mechanism. Pair it with :func:`unavailable_kind` — a
    ``"transient"`` failure is momentary resource pressure and must never be
    presented as something the operator should reconfigure.
    """
    if _last_unshare_failure is None:
        return ""
    return _last_unshare_failure[2]


def unavailable_reason() -> str:
    """Public: technical reason for the most recent sandbox probe failure.

    Names the failing step verbatim (e.g. ``"unshare(CLONE_NEWNS) failed with
    errno 1 (EPERM)"``), so a diagnostic surface can show the kernel's answer
    rather than a paraphrase. ``""`` when the last probe succeeded or none has
    run. Sibling accessor to :func:`unavailable_remedy`, reading the same
    recorded failure so the two can never describe different probes.
    """
    if _last_unshare_failure is None:
        return ""
    return _last_unshare_failure[1]


def remedy_guidance(remedy: str) -> str:
    """Public: mechanism-specific guidance for a ``REMEDY_*`` token (``""`` if none).

    The stable cross-module entry point for :data:`_LINUX_REMEDY_GUIDANCE`, so
    diagnostic surfaces (doctor, dashboard) render the one shared remedy text
    for a mechanism instead of maintaining a drifting copy.
    """
    return _linux_remedy_guidance(remedy)


def _close_probe_fds(*fds: int) -> None:
    """Close probe pipe fds, tolerating an already-closed one. Never raises.

    ``os.close`` blocks, but every probe path runs off the event loop
    (``_probe_unshare`` defers to the background warm thread when a loop is
    running), so this does not breach the no-blocking-call-on-event-loop rule.
    """
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


_PROBE_CHILD_FD_SWEEP_CAP = 4096
"""Fallback bound for the probe child's inherited-fd close sweep.

Used only when ``SC_OPEN_MAX`` cannot be read or answers nonsense. When
sysconf answers, its value (the soft ``RLIMIT_NOFILE``) is trusted as the
bound: ``os.closerange`` delegates to ``close_range(2)`` on Linux >= 5.9, so
a wide span costs one syscall rather than a walk, and silently clamping the
bound would leave a high-numbered lock fd open with no diagnostic that the
sweep came up short.
"""


def _fd_sweep_ranges(keep: frozenset[int], limit: int | None = None) -> tuple[tuple[int, int], ...]:
    """Precompute the ``os.closerange`` spans covering ``[0, bound)`` minus *keep*.

    Runs in the PARENT, before ``os.fork()``. The probe child of a threaded
    process must not allocate or take locks — another thread may own the
    allocator lock at fork time and vanish, leaving it held forever in the
    child — so everything that sorts, boxes, or asks ``sysconf`` happens here,
    and the child is left executing bare ``closerange`` syscalls over the
    returned pairs (:func:`_close_fd_ranges`).

    The bound is ``SC_OPEN_MAX`` (the soft ``RLIMIT_NOFILE``);
    :data:`_PROBE_CHILD_FD_SWEEP_CAP` applies only when sysconf cannot answer.
    ``limit`` exists for tests. Never raises.
    """
    if limit is None:
        try:
            limit = int(os.sysconf("SC_OPEN_MAX"))
        except (AttributeError, OSError, ValueError):
            # AttributeError: os.sysconf does not exist off-POSIX (Windows);
            # the sweep only runs on Linux, but this helper must keep its
            # never-raises contract everywhere the tests exercise it.
            limit = _PROBE_CHILD_FD_SWEEP_CAP
    if limit <= 0:
        limit = _PROBE_CHILD_FD_SWEEP_CAP
    ranges: list[tuple[int, int]] = []
    low = 0
    for fd in sorted(k for k in keep if k >= 0):
        if fd >= limit:
            break
        if fd > low:
            ranges.append((low, fd))
        low = fd + 1
    if low < limit:
        ranges.append((low, limit))
    return tuple(ranges)


def _close_fd_ranges(ranges: tuple[tuple[int, int], ...]) -> None:
    """Close the precomputed fd spans: the probe child's half of the sweep.

    Runs between ``os.fork()`` and ``os._exit`` in a child that never execs,
    so ``O_CLOEXEC`` never fires and every inherited descriptor — the
    ``gateway.lock`` flock fd and the dashboard listen socket included — is
    still open. Without the sweep, a probe child orphaned by its parent's
    death (gateway OOM-killed between fork and reap) keeps the lock fd open
    and pins the data home until someone reclaims it.

    Only ``os.closerange`` is invoked here: the spans were computed pre-fork
    by :func:`_fd_sweep_ranges` precisely so this post-fork path does no
    allocation-bearing work beyond iterating a ready tuple. ``closerange``
    ignores bad fds, so this never raises.
    """
    for low, high in ranges:
        os.closerange(low, high)


def _probe_failure(label: str, err: int) -> tuple[bool, bool, str, str]:
    """Shape one failed probe step into ``(ok, transient, reason)``.

    EPERM stays PERMANENT: an AppArmor userns denial, or a kernel built without
    CONFIG_USER_NS, will not clear on a retry, and caching that verdict is what
    makes ``detect_backend()`` honest. Only the momentary-resource errnos are
    transient — widening that set caused incident 2026-07-18, where one EAGAIN
    was cached as "this host has no sandbox" for an hour.

    Returns the step's remedy token IN-BAND with the verdict. Carrying it in a
    module global instead would let a second, concurrent probe interleave between
    one probe staging its token and the caller reading it, recording a reason with
    the wrong mechanism.
    """
    name = errno.errorcode.get(err, "?")
    return (
        False,
        err in _TRANSIENT_PROBE_ERRNOS,
        f"{label} failed with errno {err} ({name})",
        _remedy_for_step(label, err),
    )


def _probe_harness_failure(label: str, err: int) -> tuple[bool, bool, str, str]:
    """Classify a probe-scaffolding failure, treating a vanished child as transient.

    A child that dies mid-handshake reaches the parent as ESRCH/ENOENT on a
    ``/proc`` map write, or EPIPE on the write that releases it. None of those is
    a kernel verdict about user namespaces, so caching them permanently would
    strand every later spawn until restart — the incident-2026-07-18 shape.
    """
    if err in _PROBE_CHILD_GONE_ERRNOS:
        name = errno.errorcode.get(err, "?")
        return (False, True, f"{label} failed with errno {err} ({name})", "")
    return _probe_failure(label, err)


def _probe_child_unshare(libc: ctypes.CDLL, flags: int) -> int:
    """Run one ``unshare(2)`` in the probe child; return 0 or the errno.

    A module-level seam so a test can simulate the Ubuntu >= 23.10 shape
    ("NEWUSER ok, NEWNS EPERM") without needing a restricted kernel.
    """
    ctypes.set_errno(0)
    if libc.unshare(flags) == 0:
        return 0
    return ctypes.get_errno() or errno.EPERM


def _probe_child_make_private(libc: ctypes.CDLL) -> int:
    """Make ``/`` recursively private in the probe child's new mount namespace.

    Returns 0 or the errno, like :func:`_probe_child_unshare`, and is the same
    seam for tests: the container shape ("both unshares ok, mount EACCES") is
    simulated here rather than by running under a ``deny mount`` profile.

    Runs ONLY after ``unshare(CLONE_NEWNS)`` succeeded, so the propagation
    change is confined to a namespace no other process shares and that ends
    with the child. Nothing is bind-mounted and no path is hidden: this is the
    one mount the launcher performs before any of that, and the one a
    container's policy refuses first.
    """
    ctypes.set_errno(0)
    if libc.mount(None, b"/", None, _PROBE_MS_REC | _PROBE_MS_PRIVATE, None) == 0:
        return 0
    return ctypes.get_errno() or errno.EPERM


def _probe_bind_libc(libc: ctypes.CDLL) -> None:
    """Declare the two libc calls the probe child makes, for either probe path."""
    libc.unshare.argtypes = [ctypes.c_int]
    libc.unshare.restype = ctypes.c_int
    libc.mount.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
    ]
    libc.mount.restype = ctypes.c_int


def _probe_child_thread_count() -> int:
    """Live threads in the probe child, or 0 when it cannot be determined.

    ``unshare(CLONE_NEWUSER)`` implies ``CLONE_THREAD``, which the kernel refuses
    with **EINVAL** unless the caller's thread group holds exactly one task. A
    ``fork()`` child is single-threaded by construction, so this normally reads 1 --
    but ``os.register_at_fork`` handlers run INSIDE ``os.fork()``, before it
    returns, and a library can start a thread there. OpenTelemetry's metric SDK does
    exactly that: its ``PeriodicExportingMetricReader`` registers an
    ``after_in_child`` hook that restarts its exporter thread in every child.

    Used ONLY to explain an EINVAL, never to reclassify one. EINVAL is genuinely
    ambiguous here -- a kernel built without ``CONFIG_USER_NS`` returns it too, and a
    multithreaded child cannot tell the two apart, because it never gets far enough to
    ask. Calling it transient would be just as wrong as calling it permanent, and it
    would additionally withhold the ``no_backend`` opt-in (``sandbox_allow_unsandboxed_exec``)
    from a host that really has no user namespaces. So the classification stays exactly
    as it was and the REASON names the thread, which is the part a reader cannot infer:
    a bare "errno 22 (EINVAL)" sends them to check their kernel config, which is the
    wrong place. Making such a process probe successfully needs a single-threaded
    child, i.e. a different spawn mechanism, and that is its own change.

    ``st_nlink`` of ``/proc/self/task`` is ``2 + threads`` (each thread is a
    subdirectory), so this is one ``stat`` and no list: the probe child of a threaded
    process must not allocate, because another thread may have owned the allocator lock
    at fork time and does not exist in the child to release it. Linux-only, like the
    rest of the probe.
    """
    try:
        return max(0, os.stat("/proc/self/task").st_nlink - 2)
    except OSError:
        return 0


def _probe_write_identity_maps(pid: int, uid: int, gid: int) -> tuple[str, int] | None:
    """Write the probe child's identity maps, exactly as the launcher's parent does.

    Returns ``None`` on success, else ``(label, errno)`` for the first
    ``/proc/<pid>/`` file that could not be written. A child that died between
    the fork and this write surfaces here as ESRCH/ENOENT instead of raising.
    """
    for name, payload in (
        ("setgroups", "deny"),
        ("uid_map", f"{uid} {uid} 1\n"),
        ("gid_map", f"{gid} {gid} 1\n"),
    ):
        try:
            with open(f"/proc/{pid}/{name}", "w") as handle:
                handle.write(payload)
        except OSError as exc:
            return (f"/proc/<pid>/{name} write", exc.errno or 0)
    return None


def _probe_read_step(fd: int) -> tuple[str, int] | None:
    """Read one ``<step>:<errno>`` report from the probe child.

    ``None`` means the child closed the pipe without reporting, sent junk, or
    stayed silent past the handshake deadline — the deadline being what stops a
    pathological child from wedging the background warm thread forever.

    Uses ``poll`` rather than ``select``: ``select`` raises ``ValueError`` once a
    descriptor reaches FD_SETSIZE (1024), and a long-lived gateway can easily
    hand the probe a pipe fd past that. Raising there would kill the warm thread
    and leave ``wrap_argv`` rejecting every sandboxed spawn.
    """
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    deadline = time.monotonic() + _PROBE_HANDSHAKE_TIMEOUT_SECS
    buf = b""
    try:
        while b"\n" not in buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                if not poller.poll(max(1, int(remaining * 1000))):
                    return None
                chunk = os.read(fd, 32)
            except OSError:
                return None
            if not chunk:
                return None  # writer closed (POLLHUP) without a full report
            buf += chunk
    finally:
        try:
            poller.unregister(fd)
        except (KeyError, OSError):
            pass
    step, _, value = buf.split(b"\n", 1)[0].decode("ascii", "replace").partition(":")
    try:
        return (step, int(value))
    except ValueError:
        return None


def _probe_child_death(pid: int) -> str:
    """Describe how a silent probe child ended, for the failure reason.

    A child killed by a signal (the OOM killer, a stray SIGKILL) is a momentary
    environmental failure rather than a kernel verdict, so naming the signal
    preserves the diagnostic the child's exit status carries instead of reporting a
    bare failure. Non-blocking, so a child wedged past the handshake
    deadline is described instead of waited on.
    """
    try:
        reaped, status = os.waitpid(pid, os.WNOHANG)
    except OSError:
        return "exited without reporting"
    if reaped != pid:
        return f"stayed silent for {_PROBE_HANDSHAKE_TIMEOUT_SECS:g}s"
    if os.WIFSIGNALED(status):
        return f"killed by signal {os.WTERMSIG(status)}"
    if os.WIFEXITED(status):
        return f"exited with status {os.WEXITSTATUS(status)}"
    return "exited without reporting"


def _probe_reap(pid: int) -> None:
    """Reap the probe child on every exit path so no zombie or stuck child leaks.

    Reaps a child that already exited without signalling it — the common case,
    and the one that must never send SIGKILL at a pid the kernel could have
    recycled. Only a child still running after the handshake ended (it cannot
    make progress: its pipes are closed) is killed, which bounds the reap
    without spinning. ``platform_compat.kill_pid`` deliberately propagates
    ``ProcessLookupError``, so an exit in that race is caught here.
    """
    try:
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return
    except OSError:
        return  # not our child, or already reaped
    try:
        platform_compat.kill_pid(pid, platform_compat.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        os.waitpid(pid, 0)
    except OSError:
        pass


def _probe_child_sequence(
    libc: ctypes.CDLL,
    c2p_r: int,
    c2p_w: int,
    p2c_r: int,
    p2c_w: int,
    sweep_ranges: tuple[tuple[int, int], ...],
) -> None:
    """Probe child: run the launcher's namespace handshake, reporting each step.

    Three steps, in the launcher's order: ``unshare(CLONE_NEWUSER)``, then —
    once the parent has written the identity maps — ``unshare(CLONE_NEWNS)``,
    then — once the parent has read that verdict — the propagation mount on
    ``/``. Each step is one line on the pipe and the child waits for the parent
    between them: a report and its successor written back to back could land in
    the same read and the second would be lost.

    Never returns. It reports raw errnos and classifies nothing, so the entire
    verdict lives in the parent where a test can drive it without forking.
    ``sweep_ranges`` was computed pre-fork by :func:`_fd_sweep_ranges` so this
    path performs no allocation-bearing bookkeeping of its own.
    """
    try:
        _close_probe_fds(c2p_r, p2c_w)
        # Drop every other inherited descriptor before touching namespaces:
        # an orphaned probe child must not keep the gateway.lock fd (or the
        # dashboard listen socket) open and pin the home. Only the handshake
        # ends and the standard streams survive.
        _close_fd_ranges(sweep_ranges)
        # Read BEFORE the unshare: it is the only moment the count is the one the
        # kernel judged. Reported only alongside an EINVAL, and only to explain it --
        # see _probe_child_thread_count for why it must not change the verdict.
        threads = _probe_child_thread_count()
        err = _probe_child_unshare(libc, _CLONE_NEWUSER)
        if err == errno.EINVAL and threads > 1:
            os.write(c2p_w, b"M:%d\n" % threads)
            os._exit(0)
        os.write(c2p_w, b"U:%d\n" % err)
        if err:
            os._exit(0)
        # NEWNS needs a mapped UID, so wait for the parent's maps first. This
        # ordering is the entire point of the probe.
        if not os.read(p2c_r, 1):
            os._exit(0)  # parent abandoned the handshake; it already has a verdict
        err = _probe_child_unshare(libc, _CLONE_NEWNS)
        os.write(c2p_w, b"N:%d\n" % err)
        if err:
            os._exit(0)
        if not os.read(p2c_r, 1):
            os._exit(0)
        os.write(c2p_w, b"P:%d\n" % _probe_child_make_private(libc))
        os._exit(0)
    except BaseException:
        os._exit(1)


def _probe_parent_sequence(
    pid: int,
    c2p_r: int,
    p2c_w: int,
    uid: int,
    gid: int,
    death: Callable[[int], str] = _probe_child_death,
) -> tuple[bool, bool, str, str]:
    """Parent half of the probe: drive the handshake and decide the verdict.

    ``death`` describes a child that stopped reporting. It is injected because the
    spawned probe's child is owned by a ``Popen`` -- calling ``waitpid`` on it here
    would race that object's own bookkeeping -- while the forked probe's child is
    reaped by this module. The verdict logic is identical for both.
    """
    report = _probe_read_step(c2p_r)
    if report is None:
        return (False, True, f"probe child {death(pid)}; no {_PROBE_STEP_NEWUSER} result", "")
    step, err = report
    if step == _PROBE_STEP_MULTITHREADED:
        # Same classification and same remedy as a plain EINVAL -- deliberately, see
        # _probe_child_thread_count. Only the reason gains the thread count, because
        # that is the one part a reader cannot infer from the errno.
        ok, transient, reason, remedy = _probe_failure(_PROBE_STEP_NEWUSER, errno.EINVAL)
        return (
            ok,
            transient,
            f"{reason}; the probe child had {err} threads, which alone makes it "
            "return EINVAL (CLONE_NEWUSER implies CLONE_THREAD) -- "
            f"{_PROBE_MULTITHREADED_REASON}",
            remedy,
        )
    if step != "U":
        return (False, True, f"probe child sent unexpected step {step!r}", "")
    if err:
        return _probe_failure(_PROBE_STEP_NEWUSER, err)

    failed_map = _probe_write_identity_maps(pid, uid, gid)
    if failed_map is not None:
        label, map_errno = failed_map
        return _probe_harness_failure(label, map_errno)

    try:
        os.write(p2c_w, b"x")
    except OSError as exc:
        return _probe_harness_failure("probe handshake write", exc.errno or 0)

    report = _probe_read_step(c2p_r)
    if report is None:
        return (False, True, f"probe child {death(pid)}; no {_PROBE_STEP_NEWNS} result", "")
    step, err = report
    if step != "N":
        return (False, True, f"probe child sent unexpected step {step!r}", "")
    if err:
        return _probe_failure(_PROBE_STEP_NEWNS, err)

    # Release the child for the propagation mount only after its NEWNS report
    # has been read: the two verdicts share one pipe and must not share a read.
    try:
        os.write(p2c_w, b"m")
    except OSError as exc:
        return _probe_harness_failure("probe handshake write", exc.errno or 0)

    report = _probe_read_step(c2p_r)
    if report is None:
        return (
            False,
            True,
            f"probe child {death(pid)}; no {_PROBE_STEP_MOUNT_PRIVATE} result",
            "",
        )
    step, err = report
    if step != "P":
        return (False, True, f"probe child sent unexpected step {step!r}", "")
    if err:
        return _probe_failure(_PROBE_STEP_MOUNT_PRIVATE, err)
    return (True, False, "ok", "")


def _probe_unshare_once() -> tuple[bool, bool, str, str]:
    """One launcher-shaped namespace probe: ``(ok, transient, reason)``.

    Runs in a FRESH interpreter when one can be spawned, and falls back to
    :func:`_probe_unshare_via_fork` otherwise. The two produce the same verdict
    tuple through the same classifier; only the process the child half runs in
    differs. See :data:`_PROBE_SHIM_CODE` for why that difference is the whole
    point.
    """
    spawned = _probe_unshare_via_spawn()
    if spawned is not None:
        return spawned
    return _probe_unshare_via_fork()


#: Child half of the probe, run in a FRESH interpreter rather than a fork of the
#: caller. Same wire protocol as :func:`_probe_child_sequence`, so the reviewed
#: parent half drives either one unchanged.
#:
#: WHY A FRESH PROCESS. ``unshare(CLONE_NEWUSER)`` implies ``CLONE_THREAD`` and the
#: kernel refuses it with EINVAL unless the caller's thread group holds exactly one
#: task. A fork child inherits that condition: ``os.register_at_fork`` handlers run
#: INSIDE ``os.fork()`` before it returns, so a dependency that restarts a thread in
#: every child -- OpenTelemetry's ``PeriodicExportingMetricReader`` does exactly
#: this -- makes the child multithreaded before the probe can measure anything. The
#: verdict is then EINVAL, which is classified permanent and cached, and every later
#: sandboxed spawn on that process fails closed. A release gate lost 40 tests to one
#: such probe: all of them pass alone, none of them is a metrics test.
#:
#: A fresh interpreter starts single-threaded and runs no after-in-child fork hooks,
#: so its thread count at ``unshare()`` time is 1 regardless of the caller. This does
#: NOT soften the fail-closed rule: a genuinely single-threaded process that still
#: gets EINVAL means the host lacks ``CONFIG_USER_NS``, which stays permanent. It
#: removes the FALSE EINVAL, not the real one.
#:
#: It is also the more faithful probe. The real launcher is already a fresh
#: interpreter -- ``wrap_argv`` returns ``[sys.executable, launcher_path, ...]`` --
#: which then forks and unshares. So the fork-based probe was strictly MORE
#: pessimistic than the spawn it predicts, and this makes the two agree.
#:
#: Kept deliberately free of ``kiro_crew`` imports and run under ``-I -S``, like
#: ``_SPAWN_SHIM_CODE``: no site directory, no ``PYTHON*`` environment influence,
#: nothing to shadow. It writes ONLY wire steps on fd 1.
_PROBE_SHIM_CODE = r"""
import ctypes, errno, os

CLONE_NEWUSER = 0x10000000
CLONE_NEWNS = 0x00020000
MS_REC = 0x4000
MS_PRIVATE = 1 << 18


def threads():
    # st_nlink of /proc/self/task is the thread count plus the two dir entries.
    try:
        return max(1, os.stat("/proc/self/task").st_nlink - 2)
    except OSError:
        return 1


def unshare(libc, flags):
    ctypes.set_errno(0)
    if libc.unshare(flags) == 0:
        return 0
    return ctypes.get_errno() or errno.EPERM


def make_private(libc):
    # The launcher's first mount, inside the namespace this child just created:
    # nothing is bind-mounted or hidden, and the namespace ends with the child.
    ctypes.set_errno(0)
    if libc.mount(None, b"/", None, MS_REC | MS_PRIVATE, None) == 0:
        return 0
    return ctypes.get_errno() or errno.EPERM


def main():
    try:
        # dlopen(NULL): resolve unshare() from the libc ALREADY loaded into this
        # interpreter. Never ctypes.util.find_library here -- on Linux it EXECUTES
        # helper processes (ldconfig, then a PATH-resolved gcc/cc/objdump on musl
        # hosts) to locate libc, and this probe runs before any confinement, so a
        # workspace-controlled `gcc` on PATH would be same-user code execution.
        libc = ctypes.CDLL(None, use_errno=True)
        libc.unshare.argtypes = [ctypes.c_int]
        libc.unshare.restype = ctypes.c_int
        libc.mount.argtypes = [
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_void_p,
        ]
        libc.mount.restype = ctypes.c_int
    except BaseException:
        os._exit(1)
    n = threads()
    err = unshare(libc, CLONE_NEWUSER)
    if err == errno.EINVAL and n > 1:
        os.write(1, b"M:%d\n" % n)
        os._exit(0)
    os.write(1, b"U:%d\n" % err)
    if err:
        os._exit(0)
    if not os.read(0, 1):
        os._exit(0)
    err = unshare(libc, CLONE_NEWNS)
    os.write(1, b"N:%d\n" % err)
    if err:
        os._exit(0)
    if not os.read(0, 1):
        os._exit(0)
    os.write(1, b"P:%d\n" % make_private(libc))
    os._exit(0)


main()
"""

#: Ceiling on the spawned probe. The child does three syscalls and two blocking
#: reads whose writer is this process, so anything near this is a wedged
#: interpreter, not slow work. Exceeding it is reported TRANSIENT: a host that
#: cannot start a Python in 20 seconds is under momentary pressure, not
#: permanently sandbox-less.
_PROBE_SPAWN_TIMEOUT_SECONDS = 20.0

_probe_spawn_unavailable_logged = False


def _probe_spawned_death(proc: "subprocess.Popen[bytes]") -> str:
    """Describe how the spawned probe child ended, for a transient reason string."""
    code = proc.poll()
    if code is None:
        return "did not report"
    if code < 0:
        return f"was killed by signal {-code}"
    return f"exited with status {code}"


def _probe_unshare_via_spawn() -> tuple[bool, bool, str, str] | None:
    """Probe in a fresh interpreter. ``None`` means "cannot spawn, use the fork path".

    Returning ``None`` rather than a verdict is deliberate: an interpreter this
    process cannot start says nothing about the host's namespaces, so it must not
    become a sandbox verdict.
    """
    global _probe_spawn_unavailable_logged
    if not sys.executable:
        if not _probe_spawn_unavailable_logged:
            _probe_spawn_unavailable_logged = True
            logger.warning(
                "namespace probe cannot spawn a fresh interpreter (sys.executable is "
                "empty); falling back to a fork-based probe, which reports EINVAL on a "
                "multithreaded caller even where the sandbox works"
            )
        return None

    uid, gid = os.getuid(), os.getgid()
    try:
        # close_fds is subprocess's default and does the job the fork path has to do
        # by hand: the child gets only its standard streams, so an orphaned probe
        # cannot hold the gateway lock fd or the dashboard listen socket open.
        proc = subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", _PROBE_SHIM_CODE],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as exc:
        return _probe_failure("probe spawn", exc.errno or 0)
    except Exception as exc:  # pragma: no cover - defensive
        return (False, True, f"probe spawn failed: {exc}", "")

    assert proc.stdin is not None and proc.stdout is not None
    try:
        return _probe_parent_sequence(
            proc.pid,
            proc.stdout.fileno(),
            proc.stdin.fileno(),
            uid,
            gid,
            death=lambda _pid: _probe_spawned_death(proc),
        )
    finally:
        # Closing stdin releases a child still waiting on the maps; the wait then
        # reaps it. Popen owns the pid, so _probe_reap must NOT run here.
        for stream in (proc.stdin, proc.stdout):
            try:
                stream.close()
            except OSError:
                pass
        try:
            proc.wait(timeout=_PROBE_SPAWN_TIMEOUT_SECONDS)
        except Exception:  # pragma: no cover - a wedged interpreter
            proc.kill()
            try:
                proc.wait(timeout=_PROBE_SPAWN_TIMEOUT_SECONDS)
            except Exception:
                pass


def _probe_unshare_via_fork() -> tuple[bool, bool, str, str]:
    """The fork-based probe: same verdict, but the child inherits fork hooks.

    Retained as the fallback for a process that cannot spawn an interpreter at all.
    Its child can be made multithreaded by an ``os.register_at_fork`` handler, which
    is why :func:`_probe_unshare_via_spawn` is preferred whenever it is available.

    Mirrors the sequence ``_build_launcher_script()`` actually performs — fork,
    child ``unshare(CLONE_NEWUSER)``, parent writes the identity UID/GID map,
    child ``unshare(CLONE_NEWNS)``, child ``mount(MS_REC|MS_PRIVATE)`` on ``/``
    — because the steps do NOT behave the same way when combined or skipped. A
    single ``unshare(CLONE_NEWUSER | CLONE_NEWNS)`` is satisfied atomically and
    therefore SUCCEEDS on hosts where the split sequence fails: with Ubuntu's
    ``kernel.apparmor_restrict_unprivileged_userns = 1`` (the default since
    23.10), creating a user namespace moves the process into a restricted
    AppArmor profile carrying no CAP_SYS_ADMIN, so the *second* unshare returns
    EPERM. The previous combined probe reported those hosts as sandbox-capable
    and every real spawn then died with ``sandbox: unshare(NEWNS) failed: errno
    1``. The propagation mount is the same story one step later: a container
    under its runtime's default AppArmor profile passes both unshares and
    refuses the mount with EACCES, and a probe that stopped at the unshares
    reported it sandbox-capable too.

    ``reason`` names the failing step so a caller can tell the mechanisms apart
    — a NEWNS denial is the AppArmor userns restriction, NEWUSER with
    ENOSPC/EUSERS is ``user.max_user_namespaces=0``, and a mount denial is the
    container runtime's policy — rather than reporting a bare errno that fits
    all of them.

    Linux-only and off-loop only: ``_probe_unshare()`` guards the platform and
    defers to the background warm thread when a loop is running, so the fork,
    pipe reads and ``waitpid`` here never block the event loop.
    """
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        _probe_bind_libc(libc)
    except OSError as exc:
        return (False, exc.errno in _TRANSIENT_PROBE_ERRNOS, f"libc load failed: {exc}", "")
    except Exception as exc:  # find_library returning junk, ABI issues, ...
        return (False, False, f"libc load failed: {exc}", "")

    uid, gid = os.getuid(), os.getgid()
    try:
        c2p_r, c2p_w = os.pipe()
    except OSError as exc:
        return _probe_failure("probe pipe", exc.errno or 0)
    try:
        p2c_r, p2c_w = os.pipe()
    except OSError as exc:
        _close_probe_fds(c2p_r, c2p_w)
        return _probe_failure("probe pipe", exc.errno or 0)

    # Compute the child's fd sweep BEFORE forking: sorting, sysconf, and tuple
    # building all allocate, and post-fork the allocator lock may be held by a
    # thread that does not exist in the child.
    sweep_ranges = _fd_sweep_ranges(frozenset({0, 1, 2, c2p_w, p2c_r}))

    try:
        pid = os.fork()
    except OSError as exc:
        _close_probe_fds(c2p_r, c2p_w, p2c_r, p2c_w)
        return _probe_failure("fork", exc.errno or 0)

    if pid == 0:
        _probe_child_sequence(libc, c2p_r, c2p_w, p2c_r, p2c_w, sweep_ranges)  # never returns
        os._exit(1)  # pragma: no cover - defensive

    _close_probe_fds(c2p_w, p2c_r)
    try:
        return _probe_parent_sequence(pid, c2p_r, p2c_w, uid, gid)
    finally:
        # Closing p2c_w also releases a child still waiting on the maps.
        _close_probe_fds(c2p_r, p2c_w)
        _probe_reap(pid)


# ── Background warm thread (never-block-on-loop policy) ──
# The event loop NEVER executes fork/waitpid/sleep for the probe. On-loop
# callers with a cold cache get an immediate transient "none" (fail-closed,
# self-heals in ms) and fire a background daemon thread that populates the
# cache off-loop. Boot sites call warm_backend() (via asyncio.to_thread) to fill
# the cache before any on-loop caller ever reaches detect_backend(), so the
# transient path is typically never hit in production.

_warm_thread: threading.Thread | None = None


def _record_probe_failure(transient: bool, reason: str, remedy: str = "") -> None:
    """Record a probe failure and its remedy token together.

    Sole writer of the pair, so a token can never outlive the failure it
    describes: a caller that records a failure without probing omits `remedy` and
    thereby clears any earlier one, instead of having to remember a separate line.
    That matters because the token is surfaced to the user even for a transient
    verdict, so a stale one would name the wrong host mechanism.
    """
    global _last_unshare_failure
    _last_unshare_failure = (transient, reason, remedy)


def _background_warm() -> None:
    """Run the probe off-loop and populate the cache. Thread target."""
    global _backend, _last_unshare_failure
    for attempt in (1, 2):
        ok, transient, reason, remedy = _probe_unshare_once()
        if ok:
            _last_unshare_failure = None
            _backend = "namespace"
            logger.info("Background warm: sandbox backend = namespace")
            return
        _record_probe_failure(transient, reason, remedy)
        if not transient:
            logger.warning("Background warm: probe permanent failure: %s", reason)
            _backend = "none"
            return
        logger.warning("Background warm: probe transient (attempt %d/2): %s", attempt, reason)
        if attempt == 1:
            time.sleep(_PROBE_TRANSIENT_RETRY_DELAY_SECS)
    # Both attempts transient — leave cache uncached (None) so next call re-tries
    logger.warning("Background warm: both attempts transient, cache stays cold")


def _kick_background_warm() -> None:
    """Start the background warm thread if not already running.

    Thread-start failure (RuntimeError under thread exhaustion) is swallowed:
    the cache stays cold and the pre-existing self-healing transient path
    applies on the next spawn.  This keeps gateway boot stable even when the
    host is resource-constrained.
    """
    global _warm_thread
    if _warm_thread is not None and _warm_thread.is_alive():
        return  # dedupe: warm already in progress
    _warm_thread = threading.Thread(target=_background_warm, name="sandbox-probe-warm", daemon=True)
    try:
        _warm_thread.start()
    except RuntimeError:
        _warm_thread = None
        logger.debug("sandbox warm thread start failed; cache stays cold")


def prewarm_backend() -> None:
    """Fire-and-forget boot hook: start background probe to fill the cache.

    The gateway boot sites (slack/gateway.py, mcp_gateway/daemon/cli.py::_amain)
    call the blocking ``warm_backend`` instead, which waits for the probe so the
    cache is warm before any on-loop spawn path reaches detect_backend().
    """
    if sys.platform != "linux":
        return  # probes are Linux-only
    _kick_background_warm()


def warm_backend(timeout: float = _WARM_JOIN_TIMEOUT_SECS) -> None:
    """Blocking boot hook: fill the probe cache BEFORE returning.

    ``prewarm_backend`` only *starts* the probe, so a caller that reaches
    ``detect_backend`` in the same tick still races the warm thread and gets the
    synthetic-transient answer — a cold-cache false negative that reads exactly
    like "this host has no sandbox backend". This variant waits for the probe to
    land, so the next ``detect_backend`` sees a warm cache and the transient path
    is not reachable from a warmed boot.

    The wait is bounded: ``_background_warm`` makes at most two attempts with a
    single short delay between them, and a join timeout caps the total. A timeout
    is not an error — the cache simply stays cold and the pre-existing
    self-healing transient path applies, exactly as with ``prewarm_backend``.

    **Never-block-on-loop invariant**: this blocks on a thread join, so it MUST
    NOT be called from a running event loop. On-loop callers use
    ``await asyncio.to_thread(warm_backend)``; synchronous callers (CLI paths)
    may call it directly.
    """
    if sys.platform != "linux":
        return  # probes are Linux-only
    _kick_background_warm()
    thread = _warm_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout)


def _probe_unshare() -> bool:
    """Return True if user + mount namespaces work (Linux).

    Failures are logged with their errno and classified transient vs
    permanent in :data:`_last_unshare_failure`; a transient failure gets one
    immediate retry (off-loop only).

    **Never-block-on-loop invariant**: when called from a running asyncio
    event loop with a cold cache, this function does NOT probe — it fires
    ``_kick_background_warm()`` and returns False with a transient reason.
    The background thread populates the cache in ms; the next spawn re-checks
    and finds a warm cache. Boot prewarm ensures this path is rarely hit.

    Callers deciding cache policy (detect_backend) MUST consult the
    classification — a transient result is not evidence that the host lacks
    a sandbox backend.
    """
    global _last_unshare_failure
    if sys.platform != "linux":
        _record_probe_failure(False, "not Linux")
        return False

    # Fast path: the cache already proved user namespaces work -- no probe
    # needed. Keeps on-loop callers correct after prewarm instead of
    # deferring and returning False.
    if _backend == "namespace":
        return True

    # Detect running event loop — governs whether we probe directly or defer.
    on_loop = False
    try:
        asyncio.get_running_loop()
        on_loop = True
    except RuntimeError:
        pass

    if on_loop:
        # NEVER probe on the event loop. Kick background warm and fail transient.
        _kick_background_warm()
        # No probe ran, so the omitted remedy clears any older failure's token.
        _record_probe_failure(
            True,
            "probe deferred to background thread (cold cache on event loop); "
            "cache warms in ms — retry",
        )
        return False

    # Off-loop: direct probe with one retry on transient failure.
    for attempt in (1, 2):
        ok, transient, reason, remedy = _probe_unshare_once()
        if ok:
            _last_unshare_failure = None
            return True
        _record_probe_failure(transient, reason, remedy)
        if not transient:
            logger.warning("userns probe failed (permanent): %s", reason)
            return False
        logger.warning("userns probe failed (transient, attempt %d/2): %s", attempt, reason)
        if attempt == 1:
            time.sleep(_PROBE_TRANSIENT_RETRY_DELAY_SECS)
    return False


def userns_available() -> bool:
    """Public: True if this host can build the launcher's namespace sandbox.

    "Build" means the whole handshake the launcher performs before it hides
    anything: an unprivileged user namespace, a mount namespace inside it, and
    a private propagation mount on ``/`` inside that. A host that grants the
    namespaces but refuses the mount (a container under its runtime's default
    AppArmor profile) is NOT sandbox-capable, and reporting it as such made
    every real spawn die in the launcher instead.

    Stable cross-module entry point for the namespace-support probe, shared by
    the OS-level sandbox here and the JailProvider extension point
    (``platform/interfaces.py``), so consumers do not depend on the private
    ``_probe_unshare`` name.
    """
    return _probe_unshare()


@functools.lru_cache(maxsize=1)
def is_wsl() -> bool:
    """Public: True if this Linux host is running under Windows Subsystem for Linux.

    Centralized host probe (parallel to :func:`userns_available`) so consumers
    never re-implement WSL detection. WSL2 *does* expose working user
    namespaces, so :func:`userns_available` returns True there — but WSL's
    networking is a NAT'd virtual interface, and rootless-namespace jails
    (slirp4netns) make agentic command networking unreachable. A jail backend
    (JailProvider) uses this to opt WSL out of jailing.

    Detection (cheap, in order): the ``WSL_DISTRO_NAME`` / ``WSL_INTEROP`` env
    vars WSL injects into every login shell, then the ``microsoft`` marker the
    WSL kernel stamps into ``/proc/version`` (covers WSL1 + WSL2, both Microsoft
    and -microsoft-standard builds). Result is cached — the host's WSL-ness does
    not change within a process. Always False off Linux.
    """
    if sys.platform != "linux":
        return False
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        with open("/proc/version", encoding="utf-8", errors="replace") as fh:
            return "microsoft" in fh.read().lower()
    except OSError:
        return False


@functools.lru_cache(maxsize=1)
def is_docker_container() -> bool:
    """Public: True if this process is running inside a Docker/OCI container.

    Centralized host probe (parallel to :func:`is_wsl`) so consumers never
    re-implement container detection.  Used by :func:`wrap_argv` to produce an
    actionable error message when ``unshare(CLONE_NEWUSER)`` is blocked by the
    container runtime's seccomp/AppArmor policy instead of a kernel-level
    user-namespace restriction — the two cases warrant different remedies.

    Detection order (cheap, no I/O on fast paths):

    1. ``/.dockerenv`` — Docker daemon creates this in every container.
    2. ``/run/.containerenv`` — Podman's equivalent OCI marker.
    3. ``CONTAINER=oci`` env var — set by Podman rootless and some runtimes.
    4. ``/proc/1/cgroup`` — contains ``docker``, ``containerd``, or
       ``kubepods`` in container-managed cgroups; also fires in nested
       Docker-in-Docker setups.

    Result is cached — the container context does not change within a process.
    Always False off Linux.
    """
    if sys.platform != "linux":
        return False
    # Fast path: Docker always creates /.dockerenv; Podman creates /run/.containerenv.
    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        return True
    # Podman rootless and some OCI runtimes export CONTAINER=oci.
    if os.environ.get("CONTAINER") == "oci":
        return True
    # Fallback: inspect the cgroup hierarchy for well-known runtime markers.
    try:
        with open("/proc/1/cgroup", encoding="utf-8", errors="replace") as fh:
            content = fh.read().lower()
        return "docker" in content or "containerd" in content or "kubepods" in content
    except OSError:
        return False


def _probe_sandbox_exec() -> bool:
    """Return True if macOS ``sandbox-exec`` actually works.

    Uses a file-based profile with fixed system paths for both
    ``sandbox-exec`` and its ``/usr/bin/true`` target. The probe tests with an
    ``(allow default)`` profile to detect kernel-level rejection, not merely
    executable presence.
    """
    if sys.platform != "darwin":
        return False
    # Decide empirically — do NOT hard-code a macOS version cutoff. An earlier
    # `major >= 26 → return False` gate was wrong: sandbox-exec + the Seatbelt
    # kernel subsystem still work on macOS 26 (Tahoe) — verified that the real
    # generated profile compiles, runs kiro-cli, AND enforces (a strict profile
    # denies `cat ~/.aws/config`). The gate disabled a working sandbox and forced
    # the agent onto the fail-closed no-isolation path. The probe below already
    # detects a genuinely-broken sandbox-exec on any host/version, so trust it.
    # Note: sandbox-exec / sandbox_init() are marked "deprecated" in headers
    # since macOS 10.8, but the Seatbelt kernel subsystem they use is NOT
    # deprecated — it's the same enforcement layer that backs App Sandbox and
    # iOS. All major AI CLIs (Claude Code, Codex, Gemini) rely on it.
    # Rather than hard-coding version checks, we probe empirically below.
    sb = "/usr/bin/sandbox-exec"
    if not os.path.exists(sb):
        return False
    # Probe with a file-based (allow default) profile against a TRUSTED, fixed
    # system binary. We deliberately do NOT probe the (user-writable) kiro-cli
    # binary: the probe runs under (allow default) with KiroCrew's credentials,
    # so exec'ing a user-writable target here could run a planted payload
    # effectively unsandboxed. The probe only needs to confirm the kernel
    # accepts sandbox_apply, which /usr/bin/true validates safely.
    target = "/usr/bin/true"
    if not os.path.exists(target):
        return False
    fd, profile_path = tempfile.mkstemp(suffix=".sb", prefix="kirocrew_probe_")
    try:
        os.write(fd, b"(version 1)(allow default)")
        os.close(fd)
        r = subprocess.run(
            [sb, "-f", profile_path, target],
            capture_output=True,
            timeout=_SANDBOX_BACKEND_PROBE_TIMEOUT_SECS,
            # Pinned to the profile's own temp dir: the probe runs /usr/bin/true
            # and reads nothing, so it has no claim on the caller's cwd -- and a
            # spawn with cwd=None is indistinguishable, to a per-spawn audit,
            # from one that ran in the checkout under test (the one class-7
            # descriptor a per-spawn sweep reads, charged to an arbitrary
            # first test on every worker).
            cwd=os.path.dirname(profile_path),
        )
        if r.returncode != 0:
            detail = r.stderr.decode(errors="replace").strip()
            # A nested probe ALWAYS fails: Seatbelt cannot nest, so from inside a
            # sandbox `sandbox_apply` returns EPERM even under an (allow default)
            # profile. Reporting that at WARNING as a probe failure sent operators
            # hunting for a broken sandbox-exec on hosts where it works perfectly
            # unnested, so say what actually happened instead.
            if _macos_sandbox_state() is True:
                logger.info(
                    "sandbox-exec probe failed inside an existing Seatbelt "
                    "sandbox (exit %d: %s) — nesting is impossible; this host's "
                    "sandbox-exec is NOT broken",
                    r.returncode,
                    detail,
                )
            else:
                logger.warning(
                    "sandbox-exec probe failed (exit %d): %s",
                    r.returncode,
                    detail,
                )
        return r.returncode == 0
    except Exception as exc:
        logger.debug("sandbox-exec probe failed: %s", exc)
        return False
    finally:
        try:
            os.unlink(profile_path)
        except OSError:
            pass


# ── Backend: Linux namespace sandbox ──


def _resolve_agent_executable(executable: str) -> str:
    """Resolve *executable* through the active edition before sandboxing.

    The public adapter is identity. An edition companion may replace a managed
    launcher with the direct executable it ultimately invokes so KiroCrew can
    apply exactly one OS-level sandbox. A transient adapter failure degrades to
    the original executable, which preserves the secure behavior: the outer
    sandbox still applies and a launcher that cannot run nested fails closed.
    Platform composition failures always propagate through ``safe_context_call``.
    """
    from kiro_crew.platform import safe_context_call

    return safe_context_call(
        lambda: current_context().agent_executable.resolve_executable(executable),
        fallback=executable,
        log_message="Agent executable resolver failed; using the original executable",
    )


@functools.lru_cache(maxsize=None)
def _ssh_supports_accept_new() -> bool:
    """Return True if the installed ssh supports StrictHostKeyChecking=accept-new (OpenSSH >= 7.6)."""
    try:
        r = subprocess.run(["ssh", "-V"], capture_output=True, timeout=5)
        m = re.search(r"OpenSSH_(\d+)\.(\d+)", r.stderr.decode())
        if m:
            return (int(m.group(1)), int(m.group(2))) >= (7, 6)
    except Exception:
        pass
    return False


def namespace_launcher_script_dir() -> str:
    """The directory :func:`namespace_argv` writes its launcher script into.

    ``<config_dir>/run``, normalized, and named here because two readers need the one
    answer: :func:`_ensure_run_dir`, which creates it, and ``session_pid``'s
    managed-agent gate, which has to recognise a launcher path it did not build. Normalized
    HERE so both sides of that comparison see one spelling: ``mkstemp`` returns an
    ``abspath`` of its ``dir`` on 3.12+, so an uncollapsed path reaches ``/proc``
    collapsed anyway. The case that needs it is the DEFAULT home under a ``HOME``
    containing ``..``; an explicit ``KIROCREW_HOME`` is already ``.resolve()``d by
    ``config.paths``.

    A function rather than a constant because it reads the live data home, and a NAMED one
    rather than two spellings of ``config_dir() / "run"`` because the writer and the
    recogniser have to agree about it exactly. The launcher's other three shape values are
    plain module constants, so the gate imports those directly instead.

    Deliberately only the PREFERRED spelling. ``_ensure_run_dir`` degrades to the system
    tmpdir when this directory cannot be created or chmod'd, and that directory is shared
    with every other user of the host -- accepting it as a launcher location on a KILL
    path would let a path anyone can write decide which process trees are reclaimable.
    RESIDUAL, stated rather than closed: on a host where the crew ``run/`` directory
    cannot be created, a sandboxed agent root is not recognised and its tracking entry is
    retained instead of reaped, exactly as it was before the gate learned this shape.
    Closing that needs identity the tmpdir path cannot supply.

    This is also the only part of the launcher's shape that touches the filesystem:
    ``config_dir()`` creates the data home if it is missing and can raise, so the gate
    asks for it LAST, after every free test on the command line has already matched.
    """
    return os.path.normpath(os.path.abspath(str(config_dir() / "run")))


def _ensure_run_dir() -> str:
    """Create ``<config_dir>/run/`` with mode 0o700, falling back to tmpdir on failure."""
    run_dir = namespace_launcher_script_dir()
    try:
        os.makedirs(run_dir, mode=0o700, exist_ok=True)
        # exist_ok does not re-apply mode on existing dirs — enforce explicitly.
        # 0o700 (owner-only rwx) is deliberately restrictive: this dir holds
        # per-session sandbox launcher scripts and sockets that must NOT be
        # world-readable. Semgrep's 0o644 suggestion is wrong for a directory
        # (needs the execute/traverse bit) and would loosen, not tighten, access.
        os.chmod(run_dir, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501  # fmt: skip
    except OSError:
        # ``gettempdir()`` probes candidate directories for writability, so it is called
        # HERE and not beside the preferred spelling: on a read-only-root container with
        # no writable tmp it raises, and an eager call would have turned every namespace
        # and seatbelt spawn into a failure instead of a degraded one.
        logger.warning("Cannot create %s; falling back to system tmpdir", run_dir)
        run_dir = tempfile.gettempdir()
    return run_dir


# Interpreter flags for the namespace launcher. These matter for CONFINEMENT
# ORDERING, not tidiness: the launcher IS a Python process, and everything the
# interpreter does at startup happens BEFORE the script reaches ``unshare``. With
# site processing enabled, ``site`` executes code from env-derived paths at startup
# -- user-site ``.pth`` files (whose location comes from ``PYTHONUSERBASE``, else
# ``HOME``) and ``sitecustomize`` (from ``PYTHONPATH``). For a config-declared
# server ``env`` block that is externally authorable text, so it is arbitrary code
# running unconfined. No argv[0] pin helps: the interpreter is the pinned binary.
#   -I (isolated) ignores PYTHON* startup vars and drops the script dir from
#      sys.path; implies -E and -s.
#   -S skips ``site`` altogether, which is what closes the class rather than
#      individual keys -- no .pth and no sitecustomize run at all.
# Safe because the generated launcher imports stdlib only (sys, os, stat, struct,
# tempfile, platform, ctypes) and never needs site-packages. The namespace probe
# and spawn shims already start their interpreters with these exact flags; this
# launcher was the one Python entrypoint that did not.
_LAUNCHER_INTERPRETER_FLAGS: tuple[str, ...] = ("-I", "-S")


def _launcher_script_of(launcher_argv: list[str]) -> str:
    """The generated launcher script inside a ``namespace_argv`` result.

    Derived from the flag count rather than hardcoded, so adding a flag cannot
    silently return a flag token where a path is expected — which would both leak
    the tempfile and hand the caller ``"-I"`` to ``unlink``.
    """
    return launcher_argv[1 + len(_LAUNCHER_INTERPRETER_FLAGS)]


def _log_refusals(refusals: tuple[sandbox_plan.Refusal, ...] | list[sandbox_plan.Refusal]) -> None:
    """Log each refusal a plan made, in the order the plan made them."""
    for refusal in refusals:
        logger.warning(refusal.message, *refusal.args)


def _carveout_probes(candidates: tuple[str, ...]) -> dict[str, sandbox_plan.CarveoutProbe]:
    """What this host says about each absolute write carve-out candidate.

    ``realpath`` and ``isdir`` are the only filesystem calls a carve-out decision needs;
    taking them here keeps :func:`kiro_crew.sandbox_plan.plan_confinement` pure. Safe on
    the spawn path: every async caller reaches the builders through
    ``shielded_prepare_off_loop``'s worker thread.
    """
    probes: dict[str, sandbox_plan.CarveoutProbe] = {}
    for raw in candidates:
        if not raw or not os.path.isabs(raw) or raw in probes:
            continue
        lexical = os.path.normpath(os.path.abspath(raw))
        canonical = os.path.realpath(lexical)
        probes[raw] = sandbox_plan.CarveoutProbe(
            raw=raw, lexical=lexical, canonical=canonical, is_dir=os.path.isdir(canonical)
        )
    return probes


def _plan_cwd(request: sandbox_plan.SandboxRequest, host_paths: list[str]) -> str:
    """The working directory a relative path in the plan resolves against.

    Read only when a path the planner joins or compares is relative -- one the request
    names, or one the host supplies (``$HOME``, the pod's home, the relocated data-home
    spellings) -- as ``os.path.abspath`` would: a process whose working directory was
    deleted keeps planning an all-absolute layout.
    """
    paths = (
        *request.extra_hidden_dirs,
        *request.extra_visible_dirs,
        *request.extra_private_dirs,
        *request.extra_expose_files,
        *host_paths,
    )
    return os.getcwd() if any(not os.path.isabs(path) for path in paths) else os.sep


def _live_plan_host(request: sandbox_plan.SandboxRequest) -> sandbox_plan.PlanHost:
    """The production :class:`~kiro_crew.sandbox_plan.PlanHost`: this host, read now.

    Every fact is read from this module at call time -- the tier tables, the platform
    policy, the relocated data-home spellings, the voice-runtime cache, the pod
    environment -- so a test that rebinds one of them here reaches the plan. Only the
    facts the request's backend renders are read, in the order the builders always read
    them: the namespace backend alone asks the process for its uid and gid and the host
    ssh for ``accept-new`` support.
    """
    namespace = request.backend == sandbox_plan.BACKEND_NAMESPACE
    home = str(Path.home())
    uid = os.getuid() if namespace else 0
    gid = os.getgid() if namespace else 0
    # The tier lists come from the active PlatformContext, so an internal companion can
    # extend them; the Default adapter returns ``_STRICT_DIRS`` / ``_CC_DIRS``.
    # ``_STANDARD_DIRS`` is not an extension point and stays on the module global.
    if request.tier == "standard":
        tier_dirs = tuple(_STANDARD_DIRS)
    elif request.tier == "cc":
        tier_dirs = tuple(_sandbox_policy().cc_dirs())
    else:
        tier_dirs = tuple(_sandbox_policy().strict_dirs())
    relocated_crew_hidden = tuple(
        _relocated_crew_targets((*_CREW_HIDDEN_LEAVES, *_RELOCATED_CREW_HIDDEN_LEAVES))
    )
    # The pod's remapped home, gated on ``KIROCREW_POD == "1"`` exactly as
    # ``config.paths`` gates the resolver, so a non-pod session's mask is unchanged.
    pod_os_home = (
        os.environ.get("KIROCREW_OS_HOME") if os.environ.get("KIROCREW_POD") == "1" else None
    )
    relocated_policy_cache_dirs = tuple(_relocated_policy_cache_dirs())
    md_notebook_degraded_dirs = tuple(_md_notebook_degraded_mask_dirs())
    voice_runtime_roots = tuple(_voice_runtime_sandbox_paths())
    voice_runtime_parents = tuple(_voice_runtime_parent_paths())
    voice_runtime_ancestor_guards = () if namespace else tuple(_voice_runtime_ancestor_guards())
    relocated_crew_readonly = tuple(_relocated_crew_targets(_CREW_READONLY_LEAVES))
    kiro_agents_targets = tuple(_resolved_kiro_agents_targets())
    install_root_targets = tuple(_resolved_install_root_targets())
    # The namespace launcher binds the package directory itself, so a renamed parent
    # carries the read-only mount with it; Seatbelt matches paths and needs the guards.
    install_root_ancestor_guards = (
        ()
        if namespace
        else _literal_ancestor_guards(tuple(os.path.dirname(t) for t in install_root_targets))
    )
    carveout_probes = tuple(_carveout_probes(sandbox_plan.carveout_candidates(request)).values())
    host_paths = [
        home,
        *([pod_os_home] if pod_os_home else []),
        *relocated_policy_cache_dirs,
        *relocated_crew_hidden,
        *relocated_crew_readonly,
        *md_notebook_degraded_dirs,
        *voice_runtime_roots,
        *voice_runtime_parents,
        *voice_runtime_ancestor_guards,
        *kiro_agents_targets,
        *install_root_targets,
        *install_root_ancestor_guards,
    ]
    return sandbox_plan.PlanHost(
        home=home,
        cwd=_plan_cwd(request, host_paths),
        tier_dirs=tier_dirs,
        cc_files=tuple(_CC_FILES),
        cc_expose_files=tuple(_CC_EXPOSE_FILES),
        crew_readonly_targets=tuple(_CREW_READONLY_TARGETS),
        crew_hidden_dirs=tuple(_CREW_HIDDEN_DIRS),
        unreadable_mask_leaves=tuple(_CREW_UNREADABLE_MASK_LEAVES),
        sensitive_env_prefixes=tuple(_SENSITIVE_ENV_PREFIXES),
        agent_denied_env_keys=tuple(_AGENT_DENIED_ENV_KEYS),
        python_env_prefixes=tuple(_PYTHON_ENV_PREFIXES),
        pod_os_home=pod_os_home,
        pod_grant_store_leaves=frozenset(_POD_OS_HOME_GRANT_STORE_LEAVES),
        pod_masked_subleaves=tuple(_POD_OS_HOME_MASKED_SUBLEAVES),
        relocated_policy_cache_dirs=relocated_policy_cache_dirs,
        relocated_crew_hidden=relocated_crew_hidden,
        relocated_crew_readonly=relocated_crew_readonly,
        md_notebook_degraded_dirs=md_notebook_degraded_dirs,
        voice_runtime_roots=voice_runtime_roots,
        voice_runtime_parents=voice_runtime_parents,
        voice_runtime_ancestor_guards=voice_runtime_ancestor_guards,
        kiro_agents_targets=kiro_agents_targets,
        install_root_targets=install_root_targets,
        install_root_ancestor_guards=install_root_ancestor_guards,
        carveout_probes=carveout_probes,
        uid=uid,
        gid=gid,
        ssh_accept_new=_ssh_supports_accept_new() if namespace else False,
    )


def _spawn_plan(
    backend: str,
    sandbox_level: str = "strict",
    *,
    strip_python_env: bool = False,
    forward_ssh_auth_sock: bool = False,
    extra_hidden_dirs: tuple[str, ...] = (),
    extra_hidden_dir_ids: tuple[tuple[str, int, int], ...] = (),
    extra_alias_credential_ids: tuple[tuple[int, int], ...] = (),
    extra_visible_dirs: tuple[str, ...] = (),
    extra_private_dirs: tuple[str, ...] = (),
    extra_private_dir_ids: tuple[tuple[str, int, int], ...] = (),
    extra_writable_dirs: tuple[str, ...] = (),
    extra_expose_files: tuple[str, ...] = (),
    fail_closed_file_masks: tuple[tuple[str, int, int], ...] = (),
    required_mask_targets: tuple[str, ...] = (),
    mask_occupants: "Mapping[str, tuple[int, ...]] | None" = None,
    crew_home_aliases: tuple[tuple[str, str, int, int], ...] = (),
) -> sandbox_plan.ConfinementPlan:
    """Plan one spawn for *backend* against this host, logging what the plan refused.

    The keyword arguments are the ones the builders below and their callers already
    take, gathered into one :class:`~kiro_crew.sandbox_plan.SandboxRequest`.
    """
    request = sandbox_plan.SandboxRequest(
        tier=sandbox_level,
        backend=backend,
        strip_python_env=strip_python_env,
        forward_ssh_auth_sock=forward_ssh_auth_sock,
        extra_hidden_dirs=tuple(extra_hidden_dirs),
        extra_hidden_dir_ids=tuple(extra_hidden_dir_ids),
        extra_alias_credential_ids=tuple(extra_alias_credential_ids),
        extra_visible_dirs=tuple(extra_visible_dirs),
        extra_private_dirs=tuple(extra_private_dirs),
        extra_private_dir_ids=tuple(extra_private_dir_ids),
        extra_writable_dirs=tuple(extra_writable_dirs),
        extra_expose_files=tuple(extra_expose_files),
        fail_closed_file_masks=tuple(fail_closed_file_masks),
        required_mask_targets=tuple(required_mask_targets),
        mask_occupants=tuple((mask_occupants or {}).items()),
        crew_home_aliases=tuple(crew_home_aliases),
    )
    plan = sandbox_plan.plan_confinement(request, _live_plan_host(request))
    _log_refusals(plan.refusals)
    return plan


def _build_launcher_script(
    sandbox_level: str = "strict",
    *,
    strip_python_env: bool = False,
    forward_ssh_auth_sock: bool = False,
    extra_hidden_dirs: tuple[str, ...] = (),
    extra_hidden_dir_ids: tuple[tuple[str, int, int], ...] = (),
    extra_alias_credential_ids: tuple[tuple[int, int], ...] = (),
    extra_visible_dirs: tuple[str, ...] = (),
    extra_private_dirs: tuple[str, ...] = (),
    extra_private_dir_ids: tuple[tuple[str, int, int], ...] = (),
    extra_writable_dirs: tuple[str, ...] = (),
    extra_expose_files: tuple[str, ...] = (),
    fail_closed_file_masks: tuple[tuple[str, int, int], ...] = (),
    required_mask_targets: tuple[str, ...] = (),
    mask_occupants: "Mapping[str, tuple[int, ...]] | None" = None,
    crew_home_aliases: tuple[tuple[str, str, int, int], ...] = (),
) -> str:
    """The Linux namespace launcher program for one spawn, as Python source text.

    Plans the spawn (:func:`_spawn_plan`) and renders the plan with
    :func:`kiro_crew.sandbox_launcher.render_namespace_launcher`. No filesystem probe of a
    masked path happens here: every identity the launcher checks was observed by the
    pre-spawn passes in :func:`namespace_argv` and arrives as data.
    """
    return sandbox_launcher.render_namespace_launcher(
        _spawn_plan(
            sandbox_plan.BACKEND_NAMESPACE,
            sandbox_level,
            strip_python_env=strip_python_env,
            forward_ssh_auth_sock=forward_ssh_auth_sock,
            extra_hidden_dirs=extra_hidden_dirs,
            extra_hidden_dir_ids=extra_hidden_dir_ids,
            extra_alias_credential_ids=extra_alias_credential_ids,
            extra_visible_dirs=extra_visible_dirs,
            extra_private_dirs=extra_private_dirs,
            extra_private_dir_ids=extra_private_dir_ids,
            extra_writable_dirs=extra_writable_dirs,
            extra_expose_files=extra_expose_files,
            fail_closed_file_masks=fail_closed_file_masks,
            required_mask_targets=required_mask_targets,
            mask_occupants=mask_occupants,
            crew_home_aliases=crew_home_aliases,
        )
    )


def _build_seatbelt_profile(
    sandbox_level: str = "strict",
    *,
    extra_hidden_dirs: tuple[str, ...] = (),
    extra_visible_dirs: tuple[str, ...] = (),
    extra_private_dirs: tuple[str, ...] = (),
    extra_writable_dirs: tuple[str, ...] = (),
    extra_expose_files: tuple[str, ...] = (),
) -> str:
    """The macOS Seatbelt profile for one spawn, as ``sandbox-exec`` profile text.

    Plans the spawn (:func:`_spawn_plan`) and renders the plan with
    :func:`kiro_crew.sandbox_seatbelt.render_seatbelt_profile`.
    """
    return sandbox_seatbelt.render_seatbelt_profile(
        _spawn_plan(
            sandbox_plan.BACKEND_SEATBELT,
            sandbox_level,
            extra_hidden_dirs=extra_hidden_dirs,
            extra_visible_dirs=extra_visible_dirs,
            extra_private_dirs=extra_private_dirs,
            extra_writable_dirs=extra_writable_dirs,
            extra_expose_files=extra_expose_files,
        )
    )


def namespace_argv(
    argv: list[str],
    sandbox_level: str = "strict",
    *,
    strip_python_env: bool = False,
    forward_ssh_auth_sock: bool = False,
    extra_hidden_dirs: tuple[str, ...] = (),
    extra_hidden_dir_ids: tuple[tuple[str, int, int], ...] = (),
    extra_alias_credential_ids: tuple[tuple[int, int], ...] = (),
    extra_visible_dirs: tuple[str, ...] = (),
    extra_private_dirs: tuple[str, ...] = (),
    extra_private_dir_ids: tuple[tuple[str, int, int], ...] = (),
    extra_writable_dirs: tuple[str, ...] = (),
    extra_expose_files: tuple[str, ...] = (),
) -> list[str]:
    """Wrap *argv* via the Python namespace launcher.

    The launcher forks, the parent writes identity UID/GID maps, and the
    child bind-mounts empty dirs over credential paths before exec.
    The child retains the real UID/GID.
    """
    resolved_argv = list(argv)
    if resolved_argv:
        resolved_argv[0] = _resolve_agent_executable(resolved_argv[0])

    # Give the seal something to mount ON, or refuse the spawn. ``readonly_dirs`` is
    # guarded on
    # ``os.path.exists`` in the launcher (a ceiling may be a plain file, so the guard
    # cannot be ``isdir``), and an absent ceiling therefore gets no bind + remount pair
    # at all — leaving the data home writable at that name for the whole sandbox.
    # Materialising the sealable subset first is what makes the seal non-vacuous on a
    # default install. Runs before the script is built so the paths exist by the time
    # the child mounts, and raises ``SandboxCeilingUnsealable`` rather than launching
    # with a keystone the seal could not cover.
    _required_targets: list[str] = []
    _materialize_sealable_ceilings(_required_targets)
    # The mask loop has the same guard (``isdir``), so the on-demand hidden
    # directories get the same treatment for the same reason.
    _materialize_maskable_dirs(_required_targets)
    # And the ``sensitive_files`` loop is guarded on ``isfile``, so md-notebook's state
    # leaves — creatable on a sandboxed host now that the backend carve-out exists —
    # need a mount target too.
    _materialize_md_notebook_mask_targets(_required_targets)
    # The live-target pointer needs one for a stronger reason than a leak: it names the
    # checkout the gateway execs into, and there is no carve-out for it at all — it is
    # creatable from any sandbox simply because the data-home ROOT is writable there and
    # an absent name has no mask. Publishing the stub first makes the mask non-vacuous.
    _materialize_live_target_mask_target(_required_targets)
    # CLEANUP BEFORE THE REFUSAL, and this order is a contract rather than a preference.
    # Both sweeps and the reconciliation remove names that are themselves hard links to a
    # masked credential leaf -- a pre-upgrade orphan is a link to the signing key by
    # construction, and so is a staging name the publisher kept when its directory sync
    # failed. The refusal below reads ``st_nlink``, so running it first would refuse on a
    # link count that the very next statement was about to fix, and refuse again on every
    # later spawn: the cleanup written for that state would sit permanently behind the gate
    # it is supposed to open. Neither sweep needs the refusal to have run, because each
    # descends by pinned descriptor and skips a root it cannot open.
    _sweep_legacy_md_notebook_temps()
    _sweep_legacy_auth_store_temps()
    _reconcile_auth_store_staging_links()
    # LAST of the pre-spawn checks, and last on purpose: every masked leaf's NAME must be
    # the name the mask binds, and the leaves above have already answered for themselves
    # with sentences tailored to what they hold. This pass covers the rest -- the masked
    # leaves nothing materialises, whose alias went unreported entirely -- and creates
    # nothing, so an unused store stays absent.
    # The identities the passes above SAW, carried into the launcher so the child can
    # require a match before it follows a link at one of these names. The alias pass
    # records them from the ``lstat`` it already performs, so the common case costs no
    # syscall; the established targets are added here because a materialiser that just
    # created one has not classified it, and they are warm. NOT every masked target: the
    # launcher masks hundreds, and statting them all here would put the loop-blocking
    # probe that ``test_the_builder_does_not_stat_the_hidden_paths`` documents back on
    # the gateway's single loop. A name with no entry keeps its previous behaviour, and
    # the module spec records that boundary.
    #
    # The same pass ALSO returns the path-only aliases this spawn must hide (the hard-link
    # alias defence): one call fills ``_mask_occupants`` for the launcher's
    # no-follow occupant check AND returns ``alias_masks`` for ``fail_closed_file_masks``.
    _mask_occupants: dict[str, tuple[int, ...]] = {}
    alias_masks = _refuse_aliased_masked_leaves(_mask_occupants)
    for _target in dict.fromkeys(_required_targets):
        if _target in _mask_occupants:
            continue
        try:
            _info = os.lstat(_target)
        except OSError as exc:
            # FAIL CLOSED. ``_required_targets`` are names a pre-spawn materialiser just
            # established as PRESENT; an ``lstat`` that now fails means the name changed
            # between then and here -- renamed aside, or its permissions revoked -- which
            # is the very substitution window this identity is carried to catch. Skipping
            # it would carry NO identity, so ``_carried_occupant`` returns ``None`` in the
            # launcher, the mismatch refusal never fires, and a decoy left at the name is
            # sealed while the original stays writable at whatever name it moved to. Refuse
            # the spawn instead, the same way an unsealable ceiling refuses.
            raise SandboxCeilingUnsealable(
                f"cannot re-stat the established mask target {safe_terminal_line(_target)} "
                f"to carry its identity into the launcher: {safe_terminal_line(str(exc))}. "
                "A pre-spawn pass saw it present, so a stat that now fails means the name "
                "changed under this launch; refusing rather than masking whatever took its "
                "place."
            ) from exc
        _mask_occupants[_target] = (
            _info.st_dev,
            _info.st_ino,
            int(stat.S_ISLNK(_info.st_mode)),
            *_referent_identity(_target, _info),
        )
    # ``~/.ssh`` is not a crew leaf, so no pass above sees it, and the strict tier masks
    # it. One named ``lstat`` here is what lets the launcher require a match instead of
    # taking its own first look after the fork -- a look that arrives on the far side of
    # script build, ``mkstemp`` and ``unshare``, which is the window being closed.
    if sandbox_level == "strict":
        _ssh_target = os.path.join(os.path.expanduser("~"), ".ssh")
        try:
            _ssh_info = os.lstat(_ssh_target)
        except OSError:
            pass
        else:
            _mask_occupants[_ssh_target] = (
                _ssh_info.st_dev,
                _ssh_info.st_ino,
                int(stat.S_ISLNK(_ssh_info.st_mode)),
                *_referent_identity(_ssh_target, _ssh_info),
            )

    # Carry the alias-mask identities into the occupant map too. The alias pass
    # already discovered each hard-linked credential alias and its device/inode
    # (``_AliasMask``); handing those paths to the launcher only via
    # ``fail_closed_file_masks``/``extra_hidden_dirs`` leaves ``_carried_occupant``
    # with nothing to enforce for them, so a rename BETWEEN the alias check and the
    # bind would let the pin mask a decoy left at the old name while the credential
    # stays readable under its moved name. An alias is a regular-file hard link, so
    # its recorded link flag is not-a-link (0), what the name reaches is a regular
    # file (2), and the referent is the entry itself; the pin then refuses if the object at the name is not the one the
    # pass saw, the same fail-closed rule every other masked target already gets.
    for _am in alias_masks:
        _mask_occupants.setdefault(_am.path, (_am.dev, _am.ino, 0, 2, _am.dev, _am.ino))

    script = _build_launcher_script(
        sandbox_level,
        strip_python_env=strip_python_env,
        forward_ssh_auth_sock=forward_ssh_auth_sock,
        extra_hidden_dirs=extra_hidden_dirs + tuple(m.path for m in alias_masks),
        extra_hidden_dir_ids=extra_hidden_dir_ids,
        extra_alias_credential_ids=extra_alias_credential_ids,
        # The same paths again WITH the inode each one was at discovery, as the set whose
        # absence or changed identity at mask time is a fault. Discovery and the bind are two
        # separate acts: the mask loop's ordinary policy is to skip a target that is absent,
        # which for a credential alias would mean a rename between the two silently leaves the
        # bytes readable under the new name -- and a decoy left at the old name is one ``ln``
        # from two links, so only the identity tells it apart.
        fail_closed_file_masks=tuple((m.path, m.dev, m.ino) for m in alias_masks),
        extra_visible_dirs=extra_visible_dirs,
        extra_private_dirs=extra_private_dirs,
        extra_private_dir_ids=extra_private_dir_ids,
        extra_writable_dirs=extra_writable_dirs,
        extra_expose_files=extra_expose_files,
        required_mask_targets=tuple(_required_targets),
        mask_occupants=_mask_occupants,
        # Decided here, beside the passes that already stat the crew roots, so the
        # builder stays free of filesystem calls and still hands the launcher one
        # name per crew directory on a symlinked home.
        crew_home_aliases=_crew_home_alias_roots(),
    )
    run_dir = _ensure_run_dir()
    # Prefix and suffix from the module constants, not a second spelling of them: the
    # run-dir sweep reclaims this family by prefix and ``session_pid``'s managed-agent
    # gate RECOGNISES the launcher by prefix and suffix, so a literal here that drifted
    # from either would leak the tempfile and cost every sandboxed root its reclaim.
    fd, path = tempfile.mkstemp(
        suffix=_LAUNCHER_SCRIPT_SUFFIX,
        prefix=f"{_SANDBOX_ARTIFACT_PREFIX}{os.getpid()}_",
        dir=run_dir,
    )
    os.write(fd, script.encode())
    os.close(fd)
    platform_compat.chmod_safe(path, 0o700)

    return [sys.executable, *_LAUNCHER_INTERPRETER_FLAGS, path, *resolved_argv]


_path_within = sandbox_plan.path_within


# ── Backend: macOS sandbox-exec ──

# kiro-cli >= 2.13 ships its own internal agent sandbox, toggled by the
# "sandbox" key in this settings file. On macOS its in-process seatbelt init
# cannot nest inside KiroCrew's sandbox-exec wrap — the kernel returns EPERM
# even under an (allow default) outer profile. Exactly one sandbox layer can be
# active per spawn, so on macOS the layers are mutually exclusive:
# kiro's internal sandbox ON  -> KiroCrew's seatbelt OFF for kiro-cli spawns
# kiro's internal sandbox OFF -> KiroCrew's seatbelt ON (unchanged default)
# (``~/.kiro/settings`` is the kiro-cli backend's own directory, distinct from
# KiroCrew's data home ``~/.kiro/crew``; the filename is the literal kiro-cli ships.)
_KIRO_INTERNAL_SETTINGS_PATH = "~/.kiro/settings/amazon-internal.json"
_KIRO_INTERNAL_SANDBOX_KEY = "sandbox"

# One loud warning per process for the delegation decision (per-spawn logs
# would spam warm-pool refills); every delegated spawn is still SEL-audited.
_kiro_delegation_warned = False


def _pinned_env_bin() -> str:
    """Absolute path to ``env``, resolved WITHOUT consulting PATH.

    ``env`` is the process that applies the credential scrub (``env -u KEY ...``)
    on the delegation paths, where no Seatbelt/namespace layer wraps the child.
    A bare ``"env"`` token is resolved by the OS through the PATH in the
    environment we hand ``Popen`` -- and on the script-cron MCP path that PATH can
    come from a config-declared server ``env`` block. Redirecting ``env`` does not
    merely run an attacker binary: it means the scrub NEVER RUNS, so the child
    receives the very credentials (Slack tokens, owner id) the ``-u`` flags exist
    to strip, and can exfiltrate them. Pinning is therefore load-bearing on these
    paths even though they intentionally apply no OS confinement of our own.

    ``trusted_system_bin`` ignores ``os.environ`` PATH entirely (fixed system dirs
    only); the ``/usr/bin/env`` fallback matches the idiom already used by
    ``sandbox_exec_argv`` and ``cgroup_scope_argv`` so an unusual host layout
    still yields an absolute path rather than a redirectable bare name.

    Deliberately does NOT pin the inner command: that is the operator's agent or
    server binary (``kiro-cli``, ``npx``, ...), which legitimately must be found on
    PATH, and it carries the same config-file trust level as the ``env`` block
    itself -- an author who can set PATH can already set ``command`` directly, so
    pinning it would buy nothing while breaking normal installs.
    """
    return platform_compat.trusted_system_bin("env") or "/usr/bin/env"


def kiro_internal_sandbox_enabled() -> bool:
    """True when kiro-cli's own internal agent sandbox is enabled.

    Reads the ``"sandbox"`` key from ``~/.kiro/settings/amazon-internal.json``
    (kiro-cli >= 2.13). Absent file, missing key, or parse failure all return
    False, which keeps KiroCrew's own sandbox engaged — failure resolves
    toward our audited isolation layer, never toward no isolation.

    Deliberately uncached: it is one small-file read per spawn, and caching
    would make a settings flip require a gateway restart (mirrors the
    uncached ``_resolve_kiro_bin`` rationale).
    """
    # Deferred import: sandbox.py is a low-level leaf that deliberately avoids a
    # top-level dependency on hooks (hooks imports sandbox at call time). The
    # read is routed through hooks.safe_read_file (security-controls): the file
    # is user-writable, so the read gets is_sensitive_path() on the RESOLVED
    # target (a symlink into ~/.aws etc. is refused through the link) plus
    # O_NOFOLLOW against a TOCTOU swap of the final component.
    from kiro_crew.hooks import safe_read_file

    try:
        data = json.loads(safe_read_file(_KIRO_INTERNAL_SETTINGS_PATH))
        if not isinstance(data, dict):
            # Valid-but-non-object JSON ([], "str", null, 123) must also
            # resolve toward KiroCrew's own sandbox, not raise.
            return False
        return bool(data.get(_KIRO_INTERNAL_SANDBOX_KEY, False))
    except (OSError, ValueError, RuntimeError):
        # OSError covers missing file / EACCES / PermissionError (sensitive
        # or symlinked target refused by hooks); ValueError covers JSON
        # decode; RuntimeError covers home-directory resolution failure.
        # Every failure resolves toward KiroCrew's own sandbox.
        return False


def kiro_internal_sandbox_switch() -> tuple[str, str]:
    """The settings file and key that toggle kiro-cli's internal sandbox.

    Returns ``(path, key)`` so a diagnostic can name the exact switch an
    operator has to edit — ``kiro_internal_sandbox_enabled`` answers *whether*
    delegation is on but not *where* it was decided, and a caller that spelled
    either half itself would drift silently the day kiro-cli renames one.

    Reads the module globals at call time, so a test that repoints
    :data:`_KIRO_INTERNAL_SETTINGS_PATH` gets its own path back here too.
    """
    return _KIRO_INTERNAL_SETTINGS_PATH, _KIRO_INTERNAL_SANDBOX_KEY


#: The two isolation layers an agent spawn can be wrapped by, as the names a
#: diagnostic prints. Constants rather than literals at each raise site: the
#: layer travels into an exception type, a log line and an operator-facing
#: remedy, and three spellings of the same layer is how a remedy ends up naming
#: the wrong switch.
SANDBOX_LAYER_CREW = "kirocrew"
SANDBOX_LAYER_HARNESS = "harness-internal"


def wrapped_by_crew_sandbox(argv: "Sequence[str]") -> bool:
    """Whether *argv* -- as returned by :func:`wrap_argv` -- runs the child
    through Kiro Crew's OWN sandbox layer.

    Read off the wrapped argv rather than re-deriving the decision from mode +
    platform + settings, because that decision is not a single expression: the
    delegated branch still falls back to Crew's seatbelt when the caller asks for
    path masks a delegated sandbox cannot enforce, the governance floor can clamp
    a requested ``off`` back up, and the audit-or-deny step can refuse a
    delegation after it was chosen. A second copy of that reasoning would answer
    differently from the wrap on exactly the hosts where the answer matters. The
    argv is the wrap's own record of what it did.

    Keys on the two things only Crew's wrappers put in an argv -- the
    ``KIROCREW_SANDBOX_ACTIVE`` env assignment the macOS seatbelt wrap prepends,
    and the generated launcher script the Linux namespace wrap execs. The
    delegated and unconfined paths add neither (they prepend at most ``env -u``
    scrub flags), so this is False for both, which is the point: on those paths
    the only sandbox left in the chain belongs to the harness.
    """
    marker = f"{_IN_SANDBOX_MARKER}="
    for token in argv:
        if not isinstance(token, str):
            continue
        if token.startswith(marker):
            return True
        if os.path.basename(token).startswith(_SANDBOX_ARTIFACT_PREFIX):
            return True
    return False


def sandbox_init_remediation(layer: str, *, corroborated: bool) -> str:
    """What an operator must change to get past a sandbox that will not initialize.

    **The switch that turns a layer OFF is emitted only on a CORROBORATED
    verdict**, and that is the whole shape of this function. The signature that
    reaches the caller is the dead child's own stderr, and that child is the
    unverified binary the sandbox exists to contain: a planted one can print any
    line it likes. A message that answered it with "run
    ``kirocrew config set agent.sandbox off``" would let that binary talk the
    operator into removing the isolation it is running under -- the same hazard
    :func:`launcher_refusal` states for its own callers, answered the same way it
    prescribes. *corroborated* must therefore come from
    :func:`corroborate_launcher_refusal` (a real launcher run around a trusted
    no-op, whose stderr no child wrote), never from the child's text.

    Uncorroborated, the message still names the layer -- that comes from the argv
    Kiro Crew itself built, not from the child -- and routes the operator to the
    check that can reach a verdict, which is where the switch lives.

    Corroboration exists for Crew's Linux launcher only. On macOS the probe
    validates an ``(allow default)`` profile against a fixed system binary while
    the real wrap applies the strict generated one, so a passing probe is not
    evidence that the real wrap works and the uncorroborated branch is the honest
    answer there. Closing that gap is the real-wrap self-test, tracked separately.

    It follows that the HARNESS layer never gets a switch from here at all, whatever
    *corroborated* says: the only trusted run available speaks to Crew's launcher,
    so treating its verdict as evidence about the harness's own sandbox would be a
    cross-layer inference -- and on that branch the harness's sandbox is the only
    isolation the child had.
    """
    if layer == SANDBOX_LAYER_HARNESS:
        # Names NO switch, and *corroborated* cannot change that -- which is the
        # point of reading this branch before that flag. Corroboration re-runs
        # KIRO CREW'S OWN launcher, so a verdict from it is evidence about Crew's
        # layer and says nothing whatever about the harness's internal sandbox.
        # Letting it unlock this switch would be a cross-layer inference: a host
        # that cannot build Crew's namespace would hand the operator the key that
        # turns off the OTHER sandbox -- and on this branch that sandbox is the
        # only isolation the child had, so the one confirmed thing would be that
        # isolation is gone. The remaining evidence is the agent's own output, and
        # the agent is the unverified binary that sandbox exists to contain.
        return (
            "the agent reported its own sandbox refusing, and Kiro Crew did not wrap "
            "this spawn -- so that sandbox is the only isolation this child had. The "
            "report above is the agent's own output and Kiro Crew has NOT confirmed "
            "it; check the host's sandbox support"
        )
    if not corroborated:
        return (
            "Kiro Crew wrapped this spawn in its own OS sandbox, but the refusal above "
            "is the agent's own output and not a verdict on this host -- and Kiro Crew's "
            "own trusted sandbox run covers its Linux launcher only, so it has NOT "
            "confirmed the failure here. Check the host's sandbox support before turning "
            "either layer off"
        )
    return (
        "a trusted launcher run confirms this host refuses Kiro Crew's own OS sandbox: "
        "run `kirocrew config set agent.sandbox off`, which spawns agents unconfined "
        "WHERE GOVERNANCE PERMITS IT -- a governance floor clamps the mode back up and "
        "the request has no effect. Where it does take, it removes Kiro Crew's "
        "OS-level isolation for EVERY agent process (no credential-path masks, no "
        "data-home seal); each unconfined spawn is recorded in the security event log"
    )


def delegated_workspace_exposes_sealed_target(
    work_dir: "str | os.PathLike[str] | None",
) -> str | None:
    """Reason a kiro-cli spawn must be refused because its workspace would leave a
    SEALED target writable, or ``None`` when it may proceed.

    One guard over every sealed target. Each seal is a rule of Kiro Crew's OWN
    launcher: the kiro agents tree (:func:`_resolved_kiro_agents_targets`), whose fork
    and template specs decide what the next spawn may do, plus every data-home leaf in
    :data:`_DELEGATED_OVERLAP_LEAF_REASONS`, each with its own stated reason -- for
    example the strict no-alias config leaf ``cloud.json``, which names the container
    image a Fargate launch runs and therefore the image the task's execution role hands
    the model credential to. The gateway's packaged install tree
    (:func:`_resolved_install_root_targets`) is a target too: it is the code the
    gateway runs on its next start.

    A spawn delegated to kiro-cli's internal sandbox (macOS with that sandbox
    enabled, every first-party Windows spawn) never passes through that launcher,
    and the delegated sandbox treats the workspace as writable — so a workspace
    that IS, CONTAINS or sits INSIDE any target lets the child rewrite it.
    Refusing here, before the spawn, is the only enforcement point left on those
    paths. Where Kiro Crew's launcher does wrap the child every seal holds
    regardless of workspace, so this returns ``None`` and keeps ``$HOME``-rooted
    workspaces working there.

    Deliberately NOT conditioned on what the config currently CONTAINS. An agent
    does not need to swap a field it can create: given a writable path and no
    Fargate block, it writes a complete one and the owner's next launch runs the
    image it chose. A rule that reads the file's contents has that hole whatever
    the contents are, because the contents are what the attacker supplies.

    Same three-layer comparison as :func:`assert_voice_runtime_outside_agent_workspace`:
    the lexical spelling AND the canonical (``realpath``) spelling of both sides,
    then filesystem identity (``st_dev``/``st_ino``) walked along each side's
    ancestor chain — so a symlinked or junctioned workspace that resolves into
    the agents tree (or that the agents tree resolves into) is caught, not just
    the spelling the caller configured. Runs off the event loop (it stats). A
    workspace or agents dir that cannot be stat'ed fails CLOSED with a reason:
    "cannot verify" is not "does not overlap". Never raises.
    """
    if work_dir is None:
        return None
    delegated = (sys.platform == "darwin" and kiro_internal_sandbox_enabled()) or (
        sys.platform == "win32"
    )
    if not delegated:
        return None
    install_targets = _resolved_install_root_targets()
    targets = _resolved_kiro_agents_targets() + [
        os.path.join(str(config_dir()), leaf) for leaf in _DELEGATED_OVERLAP_LEAF_REASONS
    ]
    if not targets and not install_targets:
        return None

    def _reason(target: str, how: str) -> str:
        # Named per target: the consequences differ, and an operator reading this needs to
        # know which seal they are looking at. Looked up in the same mapping the target list
        # is built from, so a covered leaf cannot render another leaf's consequence.
        named = next(
            (
                reason
                for leaf, reason in _DELEGATED_OVERLAP_LEAF_REASONS.items()
                if _norm(target).endswith(os.sep + os.path.normcase(os.path.normpath(leaf)))
            ),
            None,
        )
        # The install directory refuses only a workspace at or inside it, so the advice
        # for that target is to move the workspace out of it; every other target also
        # refuses a workspace that contains it.
        remedy = f"Choose a workspace that does not contain '{target}'."
        if named is None and target in install_targets:
            named = (
                "Kiro Crew install directory",
                "the agent could change the code the gateway runs on its next start",
            )
            remedy = f"Choose a workspace outside '{target}'."
        if named is not None:
            what, consequence = named
        else:
            what = "kiro agents directory"
            consequence = (
                "the agent could rewrite template/fork specs and forge its next session's " "grants"
            )
        return (
            f"workspace '{os.fspath(work_dir)}' overlaps the {what} "
            f"'{target}' ({how}); on this platform the spawn is delegated to kiro-cli's "
            f"internal sandbox, which treats the workspace as writable, so {consequence}. "
            f"{remedy}"
        )

    def _norm(path: str) -> str:
        return os.path.normcase(os.path.normpath(os.path.abspath(path)))

    def _spellings(path: str) -> tuple[str, ...]:
        # Lexical first, canonical second; de-duplicated when they coincide.
        return tuple(dict.fromkeys((_norm(path), _norm(os.path.realpath(path)))))

    def _disk_spellings(path: str) -> tuple[str, ...]:
        # The same two spellings WITHOUT case folding, for the identity layer: a
        # case-sensitive share (\\wsl.localhost) reached from Windows does not
        # resolve the lowercased spelling that normcase produces.
        absolute = os.path.normpath(os.path.abspath(path))
        canonical = os.path.normpath(os.path.abspath(os.path.realpath(path)))
        return tuple(dict.fromkeys((absolute, canonical)))

    def _lexically_overlaps(a: str, b: str) -> bool:
        try:
            return os.path.commonpath([a, b]) in (a, b)
        except ValueError:
            # Different drives (Windows): cannot overlap.
            return False

    def _identity_in_ancestor_chain(identity: tuple[int, int], path: str) -> bool:
        current = os.path.abspath(path)
        while True:
            info = os.stat(current)
            if (info.st_dev, info.st_ino) == identity:
                return True
            parent = os.path.dirname(current)
            if parent == current:
                return False
            current = parent

    try:
        raw_work = os.fspath(work_dir)
        work_spellings = _spellings(raw_work)
        work_disk = _disk_spellings(raw_work)
    except Exception:
        return _reason((targets or install_targets)[0], "workspace path could not be resolved")
    # The install directory refuses only a workspace AT or INSIDE it. A workspace that
    # merely contains it (a project holding its own non-editable venv) is allowed: that
    # layout is common, and refusing it would stop every spawn in such a project. On
    # these delegated platforms the install directory inside such a workspace stays as
    # writable as the rest of the workspace.
    for target in install_targets:
        for work in work_spellings:
            for sealed in _spellings(target):
                try:
                    inside = os.path.commonpath([work, sealed]) == sealed
                except ValueError:
                    inside = False
                if inside:
                    return _reason(target, "path")
        # Filesystem identity, one direction only (the install directory at or above
        # the workspace): a case-insensitive volume or a link reaches the same
        # directory under a spelling the lexical check above does not match.
        try:
            for sealed in _spellings(target):
                if not os.path.exists(sealed):
                    continue
                sealed_stat = os.stat(sealed)
                sealed_id = (sealed_stat.st_dev, sealed_stat.st_ino)
                for work in work_spellings:
                    existing = os.path.abspath(work)
                    while not os.path.exists(existing) and os.path.dirname(existing) != existing:
                        existing = os.path.dirname(existing)
                    if _identity_in_ancestor_chain(sealed_id, existing):
                        return _reason(target, "alias")
        except OSError as exc:
            return _reason(
                target,
                "cannot verify: "
                f"{getattr(exc, 'filename', None) or raw_work}: "
                f"{getattr(exc, 'strerror', None) or exc}",
            )
    for target in targets:
        try:
            agents_spellings = _spellings(target)
            agents_disk = _disk_spellings(target)
        except Exception:
            return _reason(target, "sealed target path could not be resolved")
        # Layer 1+2: every spelling of one side against every spelling of the other.
        for work in work_spellings:
            for agents in agents_spellings:
                if _lexically_overlaps(work, agents):
                    return _reason(target, "path")
        # Layer 3: filesystem identity, both directions. Only an EXISTING node can
        # be an alias; a workspace the spawn is about to mkdir has no identity yet
        # and its lexical spellings above are the whole story.
        try:
            if not os.path.exists(raw_work):
                continue
            work_ids = {(s.st_dev, s.st_ino) for s in (os.stat(p) for p in work_disk)}
            for work_id in work_ids:
                for agents in agents_disk:
                    if os.path.exists(agents) and _identity_in_ancestor_chain(work_id, agents):
                        return _reason(target, "alias")
            for agents in agents_disk:
                if not os.path.exists(agents):
                    continue
                agents_stat = os.stat(agents)
                for work in work_disk:
                    if _identity_in_ancestor_chain((agents_stat.st_dev, agents_stat.st_ino), work):
                        return _reason(target, "alias")
        except OSError as exc:
            return _reason(
                target,
                "cannot verify: "
                f"{getattr(exc, 'filename', None) or raw_work}: "
                f"{getattr(exc, 'strerror', None) or exc}",
            )
    return None


def _spawns_kiro_cli(argv: list[str]) -> bool:
    """True when *argv* launches kiro-cli (by basename, the same convention
    as ``_resolve_kiro_bin``).

    Only the kiro-cli spawn may delegate isolation to kiro's internal
    sandbox — every other agent-influenced spawn (e.g. an MCP probe or a
    cron script) has no internal sandbox of its own and MUST keep KiroCrew's
    wrap regardless of the kiro settings file.
    """
    return bool(argv) and Path(argv[0]).name == "kiro-cli"


def _delegate_to_kiro_internal_sandbox(
    argv: list[str],
    sandbox_level: str,
    *,
    strip_python_env: bool = False,
    forward_ssh_auth_sock: bool = False,
) -> tuple[list[str], str | None] | None:
    """Delegate an explicitly trusted kiro-cli spawn to its internal sandbox.

    This is NOT the forbidden silent unsandboxed fallback: the child still
    runs under kiro-cli's own sandbox. On macOS the delegation is config-driven
    mutual exclusion with Kiro Crew's seatbelt; on Windows it is restricted to a
    positive first-party Kiro backend classification because Kiro Crew has no
    native OS wrapper there. The decision is deterministic (never a reaction to
    a wrap failure), logged loudly once per process, and every delegated spawn
    is SEL-audited on an audit-or-deny basis. If the audit event cannot be
    written, ``None`` tells the caller to continue through the normal sandbox
    policy, which fail-closes on Windows.

    The POSIX env scrub is applied inline. Windows has no ``env -u`` launcher,
    so every production caller must pass :func:`scrub_agent_subprocess_env`'s
    result as the child environment; regression tests pin those call sites.

    Deliberately does NOT resolve the real kiro binary: the launcher shim is
    part of kiro's own sandbox mechanism on this path, so bypassing it here
    would defeat the delegated layer.
    """

    global _kiro_delegation_warned
    try:
        # circular import (pre-emptive, layering): sandbox.py is a low-level
        # leaf imported at module level by many modules including subprocess
        # entry points. A top-level dep on sel would invert the low-level ->
        # high-level layering; deferring to this rarely-taken path keeps
        # sandbox leaf-pure.
        from kiro_crew.sel import sel

        sel().log_tool_invocation(
            session_key="sandbox",
            agent="system",
            source="sandbox.wrap_argv",
            tool_name=_command_log_label(argv),
            tool_kind="subprocess",
            outcome="delegated",
            resources=(
                "Windows Kiro backend delegation: kiro internal sandbox owns "
                "this spawn; Kiro Crew has no native Windows sandbox backend"
                if sys.platform == "win32"
                else "macOS sandbox mutual exclusion: kiro internal sandbox on -> "
                "KiroCrew seatbelt off for this kiro-cli spawn"
            ),
            # audit-or-deny: written synchronously; a filesystem failure
            # re-raises so an unaudited delegation can never proceed.
            critical=True,
        )
    except Exception:
        # Fail closed (security-controls): a security delegation that cannot
        # be audited does not happen. The caller continues through Kiro Crew's
        # normal policy: macOS gets its seatbelt; Windows, which has no native
        # backend, raises SandboxUnavailableError rather than run unaudited.
        logger.warning(
            "SEL audit failed for sandbox delegation — refusing unaudited "
            "delegation; falling back to Kiro Crew's sandbox policy",
            exc_info=True,
        )
        return None
    # SEL audit succeeded — delegation is actually proceeding. Only now
    # consume the warn-once flag (a SEL-failed attempt above fell back to
    # seatbelt and must not burn the warning for the first real delegation).
    if not _kiro_delegation_warned:
        _kiro_delegation_warned = True
        if sys.platform == "win32":
            logger.warning(
                "SECURITY: delegating this Windows kiro-cli spawn to kiro-cli's "
                "internal sandbox and skipping Kiro Crew's OS wrapper. Env scrubbing "
                "still applies."
            )
        else:
            # macOS delegation is decided by a settings file, so name it: the
            # operator who has to change this cannot find it from "delegating"
            # alone, and the symptom they arrive with is a denied read of a
            # path OUTSIDE the workspace, which looks like a macOS privacy
            # (TCC) problem and is not one.
            logger.warning(
                "SECURITY: delegating this macOS kiro-cli spawn to kiro-cli's "
                "internal sandbox and skipping Kiro Crew's OS wrapper (%s sets "
                '"%s": true). Env scrubbing still applies. kiro-cli owns file '
                "access for these spawns, so a path its own profile does not allow "
                'fails with "Operation not permitted" regardless of what macOS '
                "privacy settings grant; set that key to false to hand isolation "
                "back to Kiro Crew's profile.",
                _KIRO_INTERNAL_SETTINGS_PATH,
                _KIRO_INTERNAL_SANDBOX_KEY,
            )
    if sys.platform == "win32":
        return list(argv), None
    unset_args = _sandbox_env_unset_args(sandbox_level, strip_python_env, forward_ssh_auth_sock)
    if unset_args:
        return [_pinned_env_bin(), *unset_args, *argv], None
    return list(argv), None


def sandbox_exec_argv(
    argv: list[str],
    sandbox_level: str = "strict",
    *,
    strip_python_env: bool = False,
    forward_ssh_auth_sock: bool = False,
    extra_hidden_dirs: tuple[str, ...] = (),
    extra_hidden_dir_ids: tuple[tuple[str, int, int], ...] = (),
    extra_visible_dirs: tuple[str, ...] = (),
    extra_private_dirs: tuple[str, ...] = (),
    extra_private_dir_ids: tuple[tuple[str, int, int], ...] = (),
    extra_writable_dirs: tuple[str, ...] = (),
    extra_expose_files: tuple[str, ...] = (),
) -> tuple[list[str], str | None]:
    """Wrap *argv* with ``sandbox-exec -f <profile>``.

    Also scrubs sensitive env vars via ``env -u`` since Seatbelt only
    handles file-level deny rules, not environment variables.

    Returns (new_argv, tmp_profile_path).  Caller should delete the
    profile file after the child exits.
    """
    resolved_argv = list(argv)
    if resolved_argv:
        resolved_argv[0] = _resolve_agent_executable(resolved_argv[0])

    # A Seatbelt deny on a linked models name covers the link, not the tree it points at.
    _refuse_linked_models_leaf()
    # A pre-upgrade md-notebook staging temp holding the PAT sits at a name this profile
    # never denies (it names the state leaves and the staging directory, not an arbitrary
    # ``*.tmp`` sibling), so removing it is NOT Linux-only work. File materialisation
    # stays on the namespace path — a Seatbelt deny is a path rule that holds for a name
    # that does not exist yet — but an orphan already on disk needs sweeping here too.
    _sweep_legacy_md_notebook_temps()
    _sweep_legacy_auth_store_temps()
    # Reconciled here as well. The stale name is a second name for the signing key either
    # way, and leaving it to accumulate on a macOS host means the first Linux spawn on a
    # shared data home meets a backlog of them.
    _reconcile_auth_store_staging_links()
    # A Seatbelt deny is path-shaped -- ``deny file-read* (subpath ...)`` names the leaf, not
    # its inode -- so a second hard link on a credential leaf is read straight through the
    # profile. That is the same exposure the namespace path refuses on, so this pass belongs
    # on both launch paths and not only where a bind mask is what does the hiding. It runs
    # AFTER the cleanup above, which is the ordering the namespace path also establishes: a
    # link the cleanup would have removed must not be what refuses.
    alias_masks = _refuse_multilinked_credential_leaves(masks_are_path_only=True)

    # A caller that pins a window to an inode is asking for a guarantee no path-rule
    # profile can make in full: the allow names a MUTABLE pathname, and a same-UID peer
    # renaming an app onto it is read through that allow. The Linux child compares the
    # descriptor it pinned; this backend has no counterpart at exec time. What it DOES have
    # is the check the mask roots below already get -- read the name's own identity
    # immediately before the profile is written, refuse on a mismatch, and state the
    # residual instead of implying it. A window gets that same treatment, rather than being
    # dropped: dropping a pinned window would deny the caller the directory it pinned.
    for path_, dev, ino in extra_private_dir_ids:
        if path_ not in extra_private_dirs:
            continue
        try:
            win_st = os.lstat(path_)
        except OSError as exc:
            raise SandboxCeilingUnsealable(
                f"cannot confirm the data window {safe_terminal_line(path_)} is still the "
                f"one this spawn approved: {safe_terminal_line(str(exc))}. Refusing rather "
                "than opening a window at a name whose identity cannot be read."
            ) from exc
        # NO-FOLLOW and a real directory, the two rules the producer recorded the approval
        # under and the two the Linux child re-reads through
        # ``O_PATH|O_NOFOLLOW|O_DIRECTORY``. A rename preserves the inode, so a followed
        # lookup would compare the wrong object.
        if not stat.S_ISDIR(win_st.st_mode):
            raise SandboxCeilingUnsealable(
                f"cannot open a data window at {safe_terminal_line(path_)}: the name is no "
                "longer a directory, so the allow would carve out a link rather than the "
                "tree this spawn approved. Refusing the spawn."
            )
        if (win_st.st_dev, win_st.st_ino) != (dev, ino):
            raise SandboxCeilingUnsealable(
                f"the data window {safe_terminal_line(path_)} is not the one this spawn "
                "approved -- it was replaced after approval, so the allow would expose a "
                "substitute. Refusing the spawn."
            )
    # A pinned MASK ROOT is the opposite direction and cannot be answered the same way.
    # Withholding a WINDOW leaves the tree masked, so it costs a data view; withholding a
    # MASK would leave the tree open, which is the exposure the pin was taken against --
    # so the fail-closed answer here is to refuse the spawn, exactly as the Linux child
    # does when its own comparison fails. Dropping the argument was the real defect: the
    # approval was taken and then discarded, so the profile masked a pathname while the
    # caller believed an inode had been settled.
    #
    # Checked as late as this backend can check anything -- immediately before the profile
    # is written -- and honestly NOT race-free: a same-UID peer can still rename the tree
    # between this ``lstat`` and the ``exec``, and no path-rule profile can close that,
    # because the peer is outside the sandbox and Seatbelt has no inode predicate. What
    # this does close is the case where the rename already happened, and it ends the
    # silence in the case it cannot: the residual is stated here rather than implied by an
    # argument that went nowhere.
    for path_, dev, ino in extra_hidden_dir_ids:
        try:
            st = os.lstat(path_)
        except OSError as exc:
            raise SandboxCeilingUnsealable(
                f"cannot confirm the masked directory {safe_terminal_line(path_)} is still "
                f"the one this spawn approved: {safe_terminal_line(str(exc))}. Refusing "
                "rather than masking a name whose identity cannot be read."
            ) from exc
        # NO-FOLLOW, and a real directory or nothing -- the two rules a caller's approval
        # is recorded under, and the same pair the Linux child gets from ``O_PATH|O_NOFOLLOW|O_DIRECTORY``. A followed lookup
        # compares the wrong object: a rename PRESERVES the inode, so moving the tree
        # aside and leaving a symlink at the name resolves back to the approved
        # ``(dev, ino)`` and passes, while the profile rule covers only the link's name
        # and the tree stays readable where it was moved to. Checking the name's OWN
        # identity makes the link a mismatch, and the explicit directory test says so in
        # its own words rather than relying on the inodes differing.
        if not stat.S_ISDIR(st.st_mode):
            raise SandboxCeilingUnsealable(
                f"cannot mask {safe_terminal_line(path_)}: the name is no longer a "
                "directory, so masking it would cover a link while the directory this "
                "spawn approved stays readable elsewhere. Refusing the spawn."
            )
        if (st.st_dev, st.st_ino) != (dev, ino):
            raise SandboxCeilingUnsealable(
                f"the masked directory {safe_terminal_line(path_)} is not the one this "
                "spawn approved -- it was replaced after approval, so masking the name "
                "would cover a substitute while the original stays readable under its new "
                "one. Refusing the spawn."
            )
    profile = _build_seatbelt_profile(
        sandbox_level,
        # These entries become path RULES, not binds over an inode. That is weaker than it
        # first appears: the rule keeps naming a path, and nothing here denies a write to the
        # alias's parent or its ancestors while the data home root stays writable in-sandbox,
        # so the governed process can rename a parent and read the bytes under a name no rule
        # covers -- without needing to win any race. The seal for the voice runtime exists for
        # exactly this reason. So a located alias on a CREDENTIAL leaf in the live home now
        # refuses above rather than arriving here, and what still arrives is the case where a
        # refusal would be disproportionate: a home the install does not use.
        extra_hidden_dirs=extra_hidden_dirs + tuple(m.path for m in alias_masks),
        extra_visible_dirs=extra_visible_dirs,
        extra_private_dirs=extra_private_dirs,
        extra_writable_dirs=extra_writable_dirs,
        extra_expose_files=extra_expose_files,
    )
    run_dir = _ensure_run_dir()
    # Constants, not a second spelling: the run-dir sweep reclaims this family by prefix
    # and suffix, and a literal here that drifted from either would leak the profile.
    fd, path = tempfile.mkstemp(
        suffix=_SEATBELT_PROFILE_SUFFIX,
        prefix=f"{_SANDBOX_ARTIFACT_PREFIX}{os.getpid()}_",
        dir=run_dir,
    )
    os.write(fd, profile.encode())
    os.close(fd)
    # Build env -u flags for sensitive vars present in current env. cc/strict
    # additionally scrub agent-denied credential keys (Slack tokens, owner id)
    # since loader.py seeds them into os.environ for trusted children only.
    unset_args = _sandbox_env_unset_args(sandbox_level, strip_python_env, forward_ssh_auth_sock)
    # Mark the sandboxed tree, exactly as the Linux namespace launcher does after
    # its own env scrub (see ``scrub_env`` in ``sandbox_launcher_program``, the
    # program ``_build_launcher_script`` renders). Without
    # this, an in-sandbox ``wrap_argv`` call cannot tell that KiroCrew's own
    # sandbox already confines it, tries to nest, and gets EPERM — which then
    # fail-closes every app-backend and MCP spawn on the host. Set as an ``env``
    # assignment so it lands AFTER the ``-u`` flags and cannot be dropped by them.
    marker = f"{_IN_SANDBOX_MARKER}=1"
    # Record the tier beside the marker, in the same after-the-``-u``-flags
    # position so the scrub cannot drop it — an in-sandbox wrap_argv
    # passthrough compares it against the requested tier to detect downgrades.
    level_assign = f"{_IN_SANDBOX_LEVEL_VAR}={sandbox_level}"
    # SECURITY: BOTH wrappers this function prepends are pinned here, at the layer
    # that prepends them, so no spawn site has to remember to re-pin (the caller's
    # ``env`` may carry a config-declared PATH, and CPython resolves a slash-less
    # argv[0] through THAT PATH via os.get_exec_path):
    #   * the outer ``env`` (argv[0]), which runs first of all;
    #   * the inner ``sandbox-exec``, which ``env`` itself resolves through the
    #     PATH in the environment it is handed -- BEFORE the Seatbelt profile is
    #     applied, so a hostile PATH there is a pre-confinement escape.
    # ``trusted_system_bin`` ignores PATH entirely rather than reading os.environ:
    # a gateway's PATH can legitimately lead with agent-writable directories
    # (a worktree venv's bin, ~/.local/bin), so resolving through it would leave
    # the hole half-open. Both fall back to their canonical macOS locations,
    # matching the _probe_sandbox_exec probe, so a host with an unusual layout
    # still gets an absolute path rather than a redirectable bare name.
    outer_env = _pinned_env_bin()
    sandbox_exec = platform_compat.trusted_system_bin("sandbox-exec") or "/usr/bin/sandbox-exec"
    return (
        [
            outer_env,
            *unset_args,
            marker,
            level_assign,
            sandbox_exec,
            "-f",
            path,
            *resolved_argv,
        ],
        path,
    )


def _sandbox_env_scrub_keys(
    sandbox_level: str, strip_python_env: bool, forward_ssh_auth_sock: bool = False
) -> list[str]:
    """Names of the live environment keys to scrub for a given sandbox level.

    The single source of the per-level scrub set, shared by
    :func:`_sandbox_env_unset_args` (which renders it as ``env -u`` flags) and
    the first-party no-backend carve-out in :func:`wrap_argv` (which hands the
    keys to :func:`_unset_env_argv` for a trusted-absolute-path ``env`` prefix),
    so the two paths can never scrub different sets.
    """
    prefixes = list(_SENSITIVE_ENV_PREFIXES)
    if sandbox_level in ("cc", "strict"):
        prefixes.extend(_AGENT_DENIED_ENV_KEYS)
    if strip_python_env:
        prefixes.extend(_PYTHON_ENV_PREFIXES)
    # Honour the SSH_AUTH_SOCK forward opt-in on the seatbelt
    # ``env -u`` path (macOS) exactly as on the Linux launcher. The decision is
    # passed in (resolved off-loop on the agent path) and defaults False, so a
    # generic caller keeps the socket in the unset flags.
    prefixes = _agent_scrub_prefixes(prefixes, forward_ssh_auth_sock)
    return [key for key in os.environ if any(key.startswith(p) for p in prefixes)]


def _sandbox_env_unset_args(
    sandbox_level: str, strip_python_env: bool, forward_ssh_auth_sock: bool = False
) -> list[str]:
    """``env -u`` flags scrubbing sensitive vars for a sandboxed/delegated spawn.

    Shared by ``sandbox_exec_argv`` (seatbelt wrap) and
    ``_delegate_to_kiro_internal_sandbox`` (macOS mutual-exclusion path) so the
    env-scrub guarantee is identical whether or not KiroCrew's own seatbelt is
    the active isolation layer.
    """
    unset_args: list[str] = []
    for key in _sandbox_env_scrub_keys(sandbox_level, strip_python_env, forward_ssh_auth_sock):
        unset_args.extend(["-u", key])
    return unset_args


def cleanup_stale_sandbox_profiles(*, data_home: Path, legacy_dir: str | None = None) -> int:
    """Remove orphan sandbox files from runtime artifact directories and legacy /tmp.

    A file is removed when EITHER:
      - The tagged PID is dead (os.kill probe fails), OR
      - The file mtime is older than _LAUNCHER_MAX_AGE_SECONDS (the launcher
        is consumed exactly once at child exec, so old files are garbage
        regardless of PID liveness — this handles the spawner-PID design
        where the gateway PID is always alive for current-generation files).

    Also sweeps legacy /tmp/kirocrew_sandbox_*.py files that predate the
    migration to <config_dir>/run/ — these have no PID segment, so only the
    age threshold applies — plus the orphaned bind-mount sources the namespace
    launcher stages on tmpfs (see _cleanup_stale_sandbox_mount_sources).

    Called from the periodic cleanup sweep in session.py, offloaded to the
    maintenance executor (blocking I/O).  Safe to call from sync contexts too.

    *data_home* is the data home every path below is rooted at. The sweep runs on
    a pool thread, and a pool thread resolves ``config_dir()`` whenever it happens
    to be scheduled, which, under the test suite, is routinely AFTER the test
    that queued it has torn down its ``KIROCREW_HOME`` pin, so the sweep then
    walked (and could stamp or remove under) the operator's real ``~/.kiro/crew``.
    The caller that knows the home resolves it on ITS thread and passes it in;
    there is deliberately no default, so no future caller can reopen that path.

    Returns:
        Number of stale files removed.
    """
    now = time.time()
    if legacy_dir is None:
        legacy_dir = _LEGACY_LAUNCHER_DIR
    artifact_dirs = (
        (str(data_home / "run"), _RUN_DIR_ARTIFACTS),
        (str(data_home / "pi-gate"), _PI_GATE_DIR_ARTIFACTS),
    )
    removed = 0

    # ── Sweep runtime artifact directories (PID + age) ──
    for artifact_dir, families in artifact_dirs:
        artifact_fd: int | None = None
        try:
            if platform_compat.IS_WINDOWS:
                # Windows has no os-level pinned-directory primitive, so this
                # check-then-act arm retains an accepted replacement window.
                if platform_compat.is_link_or_junction(artifact_dir):
                    logger.warning(
                        "Refusing to sweep linked or non-directory artifact directory: %s",
                        artifact_dir,
                    )
                    continue
                if not os.path.isdir(artifact_dir):
                    continue
                entries = os.listdir(artifact_dir)
            else:
                try:
                    artifact_fd = os.open(
                        artifact_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                    )
                except OSError as exc:
                    if exc.errno == errno.ENOENT:
                        continue
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                        logger.warning(
                            "Refusing to sweep linked or non-directory artifact directory: %s",
                            artifact_dir,
                        )
                        continue
                    raise
                with os.scandir(artifact_fd) as iterator:
                    entries = [entry.name for entry in iterator]

            for entry in entries:
                prefix = next((p for p in families if entry.startswith(p)), None)
                if prefix is None:
                    continue
                suffix = next((x for x in families[prefix] if entry.endswith(x)), None)
                if suffix is None:
                    continue
                filepath = os.path.join(artifact_dir, entry)
                # Age check first — handles the spawner-PID design flaw. Not for the
                # gate artifacts (pi and dsh): those are written once per gateway
                # process and REUSED by every later spawn of that process, so their
                # age says nothing, and the PID in their name is the owner's own.
                try:
                    if artifact_fd is None:
                        mtime = os.stat(filepath).st_mtime
                    else:
                        mtime = os.stat(entry, dir_fd=artifact_fd, follow_symlinks=False).st_mtime
                except OSError:
                    continue
                if prefix == _SANDBOX_ARTIFACT_PREFIX and (now - mtime) > _LAUNCHER_MAX_AGE_SECONDS:
                    try:
                        if artifact_fd is None:
                            os.remove(filepath)
                        else:
                            os.unlink(entry, dir_fd=artifact_fd)
                        removed += 1
                    except OSError:
                        pass
                    continue
                # Fresh file — fall back to PID liveness check
                middle = entry[len(prefix) : -len(suffix)]
                pid = sandbox_mount_sweep._parse_pid_segment(middle.split("_", 1)[0])
                if pid is None:
                    continue
                # Liveness probe via the shim — NEVER raw os.kill(pid, 0), which
                # TERMINATES the target process on Windows (see platform_compat).
                try:
                    alive = platform_compat.pid_exists(pid)
                except OverflowError:
                    alive = False  # absurd pid digits from a corrupt filename — stale
                if not alive:
                    try:
                        if artifact_fd is None:
                            os.remove(filepath)
                        else:
                            os.unlink(entry, dir_fd=artifact_fd)
                        removed += 1
                    except OSError:
                        pass
        finally:
            if artifact_fd is not None:
                os.close(artifact_fd)

    # ── Sweep legacy /tmp/kirocrew_sandbox_*.py (age only, no PID segment) ──
    if os.path.isdir(legacy_dir):
        try:
            with os.scandir(legacy_dir) as it:
                for dentry in it:
                    if not dentry.name.startswith(_SANDBOX_ARTIFACT_PREFIX):
                        continue
                    if not dentry.name.endswith(_LAUNCHER_SCRIPT_SUFFIX):
                        continue
                    try:
                        mtime = dentry.stat().st_mtime
                    except OSError:
                        continue
                    if (now - mtime) > _LAUNCHER_MAX_AGE_SECONDS:
                        try:
                            os.remove(dentry.path)
                            removed += 1
                        except OSError:
                            pass
        except OSError:
            pass

    removed += sandbox_mount_sweep._cleanup_stale_sandbox_mount_sources()
    removed += sandbox_mount_sweep._cleanup_legacy_mount_source_residue(data_home)
    removed += sandbox_mount_sweep._cleanup_retired_acp_snapshot_dir(data_home)
    return removed


# ── Public API ──

_backend: str | None = None  # "namespace", "sandbox-exec", "none"


def _allow_no_isolation() -> bool:
    """Whether the operator has explicitly opted into running the agent
    subprocess without OS-level credential isolation.

    Read lazily from config to avoid an import cycle with the config loader
    (sandbox.py is a low-level dependency of much of the codebase).
    """
    try:
        from kiro_crew.config.loader import (
            KiroCrewConfig,  # circular import: sandbox is a low-level dep of config.loader
        )

        return bool(getattr(KiroCrewConfig.load().agent, "sandbox_allow_no_isolation", False))
    except Exception:
        return False


#: ``_unsandboxed_exec_grant()`` verdicts. Kept as constants because the warning
#: text, the SEL ``resources`` string and the tests all have to name the same
#: two cases, and a typo in any one of them would silently read as "not granted".
UNSANDBOXED_BY_OPERATOR = "operator"
UNSANDBOXED_BY_PLATFORM = "platform"


def _allow_unsandboxed_exec() -> bool:
    """Whether execution is permitted when NO sandbox backend is available.

    The one boolean gate ``wrap_argv`` consults, and the seam a test patches to
    pin a host as permitting or refusing. A plain read of
    ``agent.sandbox_allow_unsandboxed_exec``, because that field CARRIES the
    effective policy — :func:`~kiro_crew.config.loader
    .unsandboxed_exec_platform_default` is resolved into it at load time, so an
    undeclared key reads ``True`` on a platform with no installable
    backend and a declared ``false`` reads ``False`` everywhere.

    Deliberately not keyed on whether the operator DECLARED the key. That would
    make a full-document ``KiroCrewConfig.save()`` — which publishes
    ``asdict(self.agent)``, materializing every field — turn "never decided" into
    a declared lockdown and re-brick every spawn on such a platform. Meaning has
    to live in the value, so that writing the resolved value back changes nothing.

    An unreadable config yields ``False``: a broken config must never be a way to
    obtain a LOOSER sandbox than the operator configured.
    """
    try:
        from kiro_crew.config.loader import (
            KiroCrewConfig,  # circular import: sandbox is a low-level dep of config.loader
        )

        return bool(getattr(KiroCrewConfig.load().agent, "sandbox_allow_unsandboxed_exec", False))
    except Exception:
        return False


def _unsandboxed_grant_source(permitted: bool) -> str:
    """Which permission let a spawn through, for the warning and the SEL event.

    Derived FROM the gate's own verdict rather than resolved independently, so the
    two can never contradict each other about one spawn — including when a test
    patches :func:`_allow_unsandboxed_exec`, where an independent read would
    describe the real host while the gate described the pinned one.

    The split is not cosmetic: it is what lets the audit log say whether a host ran
    unconfined because an operator accepted the risk or because the platform
    default did, and only the operator case survives a later change to that
    default. A declaration is what distinguishes them, so an unreadable config
    reports the platform — the gate can only have said ``True`` there via the
    platform branch anyway.
    """
    if not permitted:
        return ""
    return UNSANDBOXED_BY_OPERATOR if _unsandboxed_exec_key_declared() else UNSANDBOXED_BY_PLATFORM


def _unsandboxed_exec_key_declared() -> bool:
    """Whether the operator declared the opt-in key at all, for message wording.

    Thin, failure-tolerant wrapper over
    :func:`~kiro_crew.config.loader.unsandboxed_exec_declared` so a refusal can
    tell "you set this to false" apart from "you never set it" without a broken
    config turning the refusal itself into an exception. Diagnostic only — it never
    decides whether a spawn runs.
    """
    try:
        from kiro_crew.config.loader import (  # circular import: sandbox is a low-level dep
            unsandboxed_exec_declared,
        )

        return unsandboxed_exec_declared()
    except Exception:
        return False


#: Tiers whose sandbox leaves ``~/.ssh`` readable to the agent child. On these
#: the forward is the default: a child that can already open the private key
#: files gains nothing new from the agent socket except reach to keys that never
#: sit in a readable file (a passphrase, a hardware token, an external agent).
#: Spelled as an ALLOW set so an unknown or newly added tier falls to the consent
#: path rather than forwarding by omission.
_SSH_VISIBLE_TIERS = frozenset({"off", "standard", "cc"})


def _ssh_dir_masked(hidden_dirs: tuple[str, ...] | list[str]) -> bool:
    """Whether any entry of *hidden_dirs* masks ``~/.ssh`` or anything inside it.

    An entry that is ``~/.ssh`` itself, one of its ancestors (a mask over the
    whole home), or a leaf inside it all count: each means the child cannot read
    every key file, so the socket would reach further than the files do.
    """
    ssh_dir = os.path.abspath(os.path.join(os.path.expanduser("~"), ".ssh"))
    for raw in hidden_dirs:
        hidden = os.path.abspath(os.path.expanduser(str(raw)))
        if hidden == ssh_dir:
            return True
        if ssh_dir.startswith(hidden.rstrip(os.sep) + os.sep):
            return True
        if hidden.startswith(ssh_dir + os.sep):
            return True
    return False


def _tier_lists_ssh(tier: str) -> bool:
    """Whether *tier*'s own hidden-dir list names ``.ssh`` or something inside it.

    Read from the same source the confinement plan reads: ``cc`` from the active
    PlatformContext (a companion may extend it), ``standard`` from the module list,
    ``off`` hides nothing.
    """
    if tier == "off":
        return False
    entries = _STANDARD_DIRS if tier == "standard" else _sandbox_policy().cc_dirs()
    for entry in entries:
        head = str(entry).replace("\\", "/").strip("/").split("/", 1)[0]
        if head in (".ssh", "", "~"):
            return True
    return False


def _forward_ssh_auth_sock(mode: str, hidden_dirs: tuple[str, ...] | list[str]) -> bool:
    """Whether this agent spawn keeps ``SSH_AUTH_SOCK`` in its environment.

    The socket grants USE of the operator's ssh-agent keys, not possession. Two
    routes keep it:

    * By default, when the spawn's sandbox leaves ``~/.ssh`` readable: the
      effective tier (after the governed ``sandbox.min_level`` clamp, the same
      one :func:`wrap_argv` applies) is in :data:`_SSH_VISIBLE_TIERS`, that
      tier's own hidden-dir list (``cc`` is a PlatformContext extension
      point) does not name ``.ssh``, AND no
      entry of *hidden_dirs* -- the per-harness credential mask an enforced
      host adds -- covers ``~/.ssh``. Scrubbing the socket there would block
      git-over-SSH while the key files themselves stay open to the child, and
      the ``standard`` tier keeps them visible precisely so git-over-SSH works.
    * Otherwise (``strict``, or a harness whose mask hides ``~/.ssh``), only
      with the operator's recorded consent on the KEYSTONE leaf
      ``ssh_auth_sock_consent.json``. There the socket would reach keys the
      sandbox hides, so it stays an authorization the agent cannot grant
      itself: the OS sandbox mounts the keystone read-only for the agent's
      shell and ``is_sensitive_path`` fences the file tools.

    Fail-closed: any failure to resolve the tier or read consent returns False,
    so the socket is scrubbed. Read lazily to avoid an import cycle with the
    config loader.

    Windows has no ``SSH_AUTH_SOCK`` (Win32 OpenSSH's agent is a named pipe, not
    a Unix-domain socket), so the forward is a no-op there on every route. This
    mirrors ``_resolve_ssh_auth_sock`` returning early on Windows.
    """
    if platform_compat.IS_WINDOWS:
        return False
    try:
        return _forward_for_effective_tier(effective_sandbox_mode(mode), hidden_dirs)
    except Exception:
        return False


def _forward_for_effective_tier(effective: str, hidden_dirs: tuple[str, ...] | list[str]) -> bool:
    """The forward verdict for an ALREADY-CLAMPED tier; reads no governance floor.

    :func:`wrap_argv` calls this with the tier it is about to apply, so a floor
    raised between the launch tail's resolution and the wrap's own floor read
    can only take the forward away, never leave it on a tier that hides
    ``~/.ssh``. Raises on a consent read error; callers fail closed.
    """
    tier = _SANDBOX_MODE_ALIASES.get(effective, effective)
    if (
        tier in _SSH_VISIBLE_TIERS
        and not _tier_lists_ssh(tier)
        and not _ssh_dir_masked(hidden_dirs)
    ):
        return True
    from kiro_crew import (
        ssh_auth_sock_consent,  # circular import: sandbox is a low-level dep of config.loader
    )

    return ssh_auth_sock_consent.is_granted()


def unsandboxed_exec_permitted_by() -> str:
    """Public read of the no-backend execution verdict, for diagnostics.

    The same resolution :func:`wrap_argv` gates on, exposed so ``doctor`` reports
    the policy the spawn path will actually apply instead of re-deriving it from
    the config field — which records only what the operator DECLARED, and reads
    ``False`` both for "locked down" and for "never decided".

    Returns ``UNSANDBOXED_BY_OPERATOR``, ``UNSANDBOXED_BY_PLATFORM`` or ``""``.

    Deliberately does NOT fold in the governance ``sandbox.min_level`` floor: the
    floor is resolved per spawn against the mode that spawn requested, so a single
    host-level answer would be wrong for some of them. A caller that reports this
    verdict must say that a floor overrides it.
    """
    return _unsandboxed_grant_source(_allow_unsandboxed_exec())


_agent_scrub_prefixes = sandbox_plan.scrub_prefixes


def _bundled_cli_invocation() -> str | None:
    """Absolute, shell-quoted path to the CLI this process was started from.

    The AppImage persona is defined by the install guide as needing "no Python,
    pip, npm, or Node" (docs/guides/install.md), so there is usually no
    ``kirocrew`` on their PATH at all — the CLI is bundled INSIDE the AppImage.
    Printing the bare command would hand exactly the affected user a
    ``command not found`` and leave them with only the opt-out, which is the
    opposite of the point.

    ``shutil.which("kirocrew")`` is deliberately NOT trusted as evidence here:
    this string is generated inside the gateway process, which inherits the
    AppImage's own PATH, but it is pasted into the user's shell, which does not.
    A hit would prove the bundle can find its own CLI, not that the user can.

    Returns None when the path cannot be established, so the caller can fall back
    to the bare name rather than print something invented.
    """
    argv0 = sys.argv[0] if sys.argv else ""
    if not argv0:
        return None
    try:
        resolved = os.path.realpath(argv0)
    except OSError:
        return None
    name = os.path.basename(resolved).lower()
    if not name.startswith("kirocrew") or not os.path.isfile(resolved):
        return None
    return shlex.quote(resolved)


def _apparmor_userns_restricted() -> bool:
    """True when this kernel is the Ubuntu AppArmor userns-restriction case.

    Read straight from /proc rather than importing
    :mod:`kiro_crew.service.apparmor`: ``sandbox`` is a low-level dependency of
    config loading, and pulling the service package in here would create an
    import cycle. One file read, no subprocess.
    """
    try:
        with open(
            "/proc/sys/kernel/apparmor_restrict_unprivileged_userns", encoding="utf-8"
        ) as handle:
            return handle.read().strip() == "1"
    except OSError:
        return False


def _no_backend_guidance() -> str:
    """Remedy text for a genuine no-backend host, specific to WHY it has none.

    The generic "install a sandbox backend, or opt out" advice is actively
    unhelpful on the single most common affected host: stock Ubuntu 23.10+, where
    a backend exists and is one AppArmor profile away from working. Worse, the
    only concrete thing that text suggests is the opt-out, which turns off the
    isolation the message exists to protect.

    The remedy differs by HOW Kiro Crew was launched, so it is named per shape:

    * AppImage / desktop app — nothing applies a profile to a directly launched
      binary, so attach one to it (``kirocrew sandbox install-profile``).
    * anything else on such a host — ``kirocrew service install`` installs a
      profile attached by path to the resolved kirocrew launcher script (never the
      shared interpreter, which would grant unprivileged userns to every Python
      process on the machine). Any launch through that launcher is confined,
      foreground included; other entry points stay unconfined.

    Deliberately does NOT tell the user to set the sysctl to 0: that trades a
    kernel-wide protection for one app's need, and the per-application profile
    exists so they do not have to.

    The ``sandbox_allow_unsandboxed_exec`` opt-out is still named in every case,
    because it is the documented escape hatch and withholding it would leave a
    stuck user with no way out. What changes is the ORDER: on a host where the
    sandbox is one profile away from working, the profile is the remedy and the
    opt-out is the last resort, where the previous text offered the opt-out as
    the only concrete suggestion.
    """
    optout = (
        "As a last resort, agent.sandbox_allow_unsandboxed_exec=true in "
        "~/.kiro/crew/config.json allows unsandboxed execution, but that removes "
        "the isolation this check exists to protect. "
    )
    if sys.platform.startswith("linux") and _apparmor_userns_restricted():
        base = (
            "This host restricts unprivileged user namespaces via AppArmor "
            "(kernel.apparmor_restrict_unprivileged_userns=1, the default on "
            "Ubuntu 23.10+ and derivatives). A sandbox backend DOES exist here — "
            "it needs a per-application AppArmor profile granting 'userns', "
            "exactly as stock Ubuntu already ships for chrome, brave, 1password "
            "and Discord. "
        )
        appimage = os.environ.get("APPIMAGE", "").strip()
        if appimage:
            # Name the CLI by ABSOLUTE PATH, not as `kirocrew`. An AppImage user
            # has no kirocrew on PATH (see _bundled_cli_invocation), so the bare
            # command would fail for exactly the person reading this. The bundled
            # path is valid while the app is running, which is when they will run
            # it, and it is the same binary the desktop app already spawns.
            cli = _bundled_cli_invocation() or "kirocrew"
            where = (
                " (that path is inside the running app, so run it while Kiro Crew " "is open)"
                if cli != "kirocrew"
                else ""
            )
            # shlex.quote, not bare interpolation: this string is printed for the
            # user to paste into a shell, and a filename is attacker-influenced in
            # the cases that matter (a downloaded or unpacked AppImage). An
            # AppImage named `Kiro-Crew-$(...).AppImage` would otherwise have its
            # substitution executed by the paste, turning a diagnostic into a
            # command-injection vector. Mirrors the quoting the desktop side
            # already does in website/electron/sandbox-profile.js.
            return (
                base
                + (
                    "This is an AppImage launch, which no profile is attached to yet. "
                    "Run this in a terminal (it needs sudo, so it cannot be done from "
                    f"the app): {cli} sandbox install-profile --path "
                    f"{shlex.quote(appimage)}{where} — then restart the app. Do NOT "
                    "set the sysctl to 0: that disables a kernel-wide protection for "
                    "every application on the machine. "
                )
                + optout
            )
        return (
            base
            + (
                "Run `kirocrew service install` to install the profile, attached by "
                "path to the kirocrew launcher; any launch through that launcher, "
                "foreground included, is then confined. Do NOT set the sysctl to 0: "
                "that disables a kernel-wide protection for every application on the "
                "machine. "
            )
            + optout
        )
    return (
        "If this host genuinely lacks a sandbox backend, set "
        "agent.sandbox_allow_unsandboxed_exec=true in "
        "~/.kiro/crew/config.json to explicitly allow unsandboxed "
        "execution, or install a supported sandbox backend "
        "(Linux user namespaces, or macOS sandbox-exec). "
    )


def _classify_unavailable(transient: bool) -> str:
    """Name why no backend is available, given an already-read transient flag.

    One implementation of the rule shared by ``wrap_argv``'s
    ``SandboxUnavailableError.kind`` and the public :func:`unavailable_kind`, so
    the two can never drift into disagreeing about the same host.
    """
    if transient:
        return "transient"
    return "foreign_sandbox" if _inside_macos_sandbox() else "no_backend"


def unavailable_kind() -> str:
    """Classify a backend-less host for callers that offer a PERSISTENT opt-in.

    Returns ``""`` when a backend IS available, otherwise the same value
    ``SandboxUnavailableError.kind`` would carry.

    A caller that writes ``sandbox_allow_unsandboxed_exec`` to disk must act
    ONLY on ``"no_backend"``. ``detect_backend()`` alone is not enough: it also
    reports ``"none"`` for a momentary fork/resource failure, which self-heals on
    the next spawn and must never buy a permanent bypass — and for a foreign
    outer sandbox, where the host's own sandbox is fine and the remedy is to hand
    isolation back to Kiro Crew rather than disable it.
    """
    if detect_backend() != "none":
        return ""
    transient, _reason, _remedy = _last_unshare_failure or (False, "none", "")
    return _classify_unavailable(transient)


def _inside_kirocrew_sandbox() -> bool:
    """True when this process already runs inside a KiroCrew OS sandbox.

    Nested sandboxing is impossible on both backends: the Linux launcher's
    seccomp filter denies ``unshare``/``setns`` precisely so the sandboxed tree
    cannot manipulate namespaces, and macOS Seatbelt refuses ``sandbox_apply``
    with EPERM from inside an existing sandbox — even under an ``(allow default)``
    outer profile. An in-sandbox wrap_argv call must therefore pass through rather
    than fail closed — the outer sandbox still confines every descendant, so this
    is NOT the fail-open path. Failing closed here would refuse every in-sandbox
    MCP spawn with unshare EPERM and, on macOS, every app-backend spawn and MCP
    probe the gateway starts.

    Detection is deny-by-default: gated solely on the explicit, launcher-only
    ``KIROCREW_SANDBOX_ACTIVE`` marker (see ``_IN_SANDBOX_MARKER``).
    """
    return bool(os.environ.get(_IN_SANDBOX_MARKER))


@functools.lru_cache(maxsize=1)
def _macos_sandbox_state() -> bool | None:
    """Kernel verdict on whether THIS macOS process is Seatbelt-confined.

    ``True``
        Confined — ``sandbox_check(pid, NULL, SANDBOX_FILTER_NONE)`` returned 1.
    ``False``
        Definitely not confined — the kernel answered 0.
    ``None``
        Unanswerable — non-darwin, or the symbol could not be loaded or called.

    Three states rather than a bool because the two negatives carry opposite
    security meanings. A definite ``False`` alongside a present
    ``KIROCREW_SANDBOX_ACTIVE`` marker proves the marker was forged or inherited
    into an unsandboxed process, and must NOT grant a passthrough. An
    unanswerable probe says nothing at all, and must not retroactively invalidate
    a marker the Linux path honours unconditionally.

    Cached for the process lifetime — a process cannot leave its sandbox.
    """
    if sys.platform != "darwin":
        return None
    try:
        libpath = ctypes.util.find_library("System") or "/usr/lib/libSystem.dylib"
        lib = ctypes.CDLL(libpath, use_errno=True)
        check = lib.sandbox_check
        # sandbox_check(pid_t, const char *operation, enum sandbox_filter_type, ...)
        # A NULL operation with SANDBOX_FILTER_NONE (0) asks the generic
        # "is this pid sandboxed at all?" question. The path-scoped form that
        # could identify WHICH paths the profile denies is variadic and returns
        # -1 through ctypes on arm64, so it is not usable here.
        check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        check.restype = ctypes.c_int
        rc = check(os.getpid(), None, 0)
        if rc < 0:
            return None
        return rc == 1
    except Exception as exc:  # missing symbol, ABI change, restricted dyld, ...
        logger.debug("sandbox_check unavailable (%s); sandbox state unknown", exc)
        return None


def _inside_macos_sandbox() -> bool:
    """True when the kernel confirms a Seatbelt sandbox confines this process.

    Used to tell a *nesting* EPERM apart from a genuinely missing backend, so the
    fail-closed error names the real cause. Without it,
    :func:`_probe_sandbox_exec`'s EPERM is reported as "this host has no sandbox
    backend" — on a host whose ``sandbox-exec`` works perfectly when not nested.

    Unlike :data:`_IN_SANDBOX_MARKER` this is OS-authoritative: it cannot be
    spoofed through an agent-influenced environment, and it sees sandboxes
    KiroCrew did NOT create — notably kiro-cli >= 2.13's own internal seatbelt
    (see ``_KIRO_INTERNAL_SETTINGS_PATH``), or an operator-wrapped gateway. It is
    therefore NOT sufficient on its own to grant a passthrough: it proves *some*
    sandbox is active, not that KiroCrew built it or scrubbed the environment.
    The marker supplies that half; see :func:`wrap_argv`.
    """
    return _macos_sandbox_state() is True


def agent_confinement_evidence() -> str | None:
    """Evidence that THIS process runs under agent-shell confinement, or ``None``.

    The runtime half of the operator-attestation check used by authorization
    gates whose input must come from a human at a host terminal and never from
    an agent-spawned process (the app dev-mode out-of-install confirmation).
    Returns a short human-readable reason when there is ANY evidence of
    confinement, ``None`` when there is none.

    Deny-direction only, and deliberately so: each signal is unforgeable *in
    the direction of refusal*. A present ``KIROCREW_SANDBOX_ACTIVE`` marker in
    a genuinely unsandboxed process only over-refuses (and proves the marker
    was forged or leaked — see :func:`_macos_sandbox_state`); a kernel verdict
    of "Seatbelt-confined" for a non-KiroCrew sandbox (kiro-cli's internal
    seatbelt, an operator-wrapped process) still means "not a bare operator
    terminal", which is the question being asked. The converse is NOT
    guaranteed: a ``None`` does not prove a human — an agent can strip the
    marker with ``env -u``, and Linux offers no cheap kernel verdict — which
    is why callers pair this with a structural probe of a sealed artifact
    (``_CREW_READONLY_LEAVES``) that the OS sandbox denies regardless of the
    environment. Never use this function to *grant* anything.
    """
    if os.environ.get(_IN_SANDBOX_MARKER):
        return f"the {_IN_SANDBOX_MARKER} marker is set (agent-sandboxed process)"
    if _macos_sandbox_state() is True:
        return "the kernel reports this process is Seatbelt-confined"
    return None


def _warn_no_isolation(mode: str, granted_by: str = "") -> None:
    """Loudly surface that the agent subprocess is running WITHOUT OS-level
    isolation, so the fallback is never silent.

    When no sandbox backend is available the credential paths (``~/.aws``,
    ``~/.ssh``, ...) are visible to the (untrusted) agent subprocess and only
    the bypassable app-level ``security.py`` checks remain. This is a real
    degradation of the security posture, so it is logged as a WARNING unless
    the operator has explicitly acknowledged it via
    ``agent.sandbox_allow_no_isolation``. Emitted once per process.

    ``granted_by`` names which permission let the spawn through
    (``UNSANDBOXED_BY_OPERATOR`` / ``UNSANDBOXED_BY_PLATFORM``) and changes the
    REMEDY, not the severity. "Install a supported sandbox" is unactionable on a
    platform that has none to install, and a warning whose only suggestion is
    impossible trains readers to ignore it — so a platform-default grant is told
    how to LOCK THE HOST DOWN instead. The default is empty so the existing
    ``mode="off"`` callers keep the operator-shaped text.
    """
    if getattr(wrap_argv, "_warned", False):
        return
    wrap_argv._warned = True  # type: ignore[attr-defined]
    if _allow_no_isolation():
        logger.info(
            "OS-level sandbox unavailable (mode=%s, permitted by %s); running "
            "WITHOUT credential isolation. Operator opted in via "
            "agent.sandbox_allow_no_isolation; app-level checks are the only "
            "remaining boundary.",
            mode,
            granted_by or "config",
        )
        return
    if granted_by == UNSANDBOXED_BY_PLATFORM:
        logger.warning(
            "SECURITY: this platform offers no OS-level sandbox backend for Kiro "
            "Crew to apply (mode=%s), so the agent subprocess runs WITHOUT "
            "credential isolation — ~/.aws, ~/.ssh and other secrets are readable "
            "by it and only the bypassable app-level security.py checks remain. "
            "This is the documented default for such a host, not a failure: no "
            "backend can be installed here. To refuse these spawns instead, set "
            "agent.sandbox_allow_unsandboxed_exec=false in ~/.kiro/crew/config.json "
            "(or pin a governance sandbox.min_level, which overrides it fleet-wide).",
            mode,
        )
        return
    logger.warning(
        "SECURITY: no OS-level sandbox backend is available on this host "
        "(mode=%s), so the agent subprocess runs WITHOUT credential isolation — "
        "~/.aws, ~/.ssh and other secrets are readable by it and only the "
        "bypassable app-level security.py checks remain. Install a supported "
        "sandbox (Linux user namespaces, or macOS sandbox-exec), or set "
        "agent.sandbox_allow_no_isolation=true in ~/.kiro/crew/config.json to "
        "acknowledge the risk and silence this warning.",
        mode,
    )


def _command_log_label(argv: list[str]) -> str:
    """Return a fixed, non-sensitive executable class for diagnostics.

    ``wrap_argv`` is a generic boundary: later argv elements routinely contain
    user-controlled paths, URLs, and occasionally transport capabilities. Static
    analysis also correctly treats a list element as able to reach any other
    element. Never send a value taken from that container to a log or SEL event,
    even when the runtime expression selects ``argv[0]``. The fixed labels retain
    enough operational signal without exposing executable paths or arguments.
    """

    if not argv:
        return "unknown"
    name = argv[0].replace("\\", "/").rsplit("/", 1)[-1].casefold()
    if name.endswith(".exe"):
        name = name[:-4]
    if name == "git":
        return "git"
    if name in {"python", "python3", "pythonw", "pythonw3"}:
        return "python"
    if name in {"node", "npm", "npx"}:
        return "node"
    if name in {"kiro", "kiro-cli", "kirocrew"}:
        return "kiro"
    if name in {"bash", "sh", "zsh", "cmd", "powershell", "pwsh"}:
        return "shell"
    if name in {"env", "bwrap", "sandbox-exec", "systemd-run"}:
        return "sandbox-helper"
    return "other"


def _warn_mode_off_unconfined(argv: list[str], is_kiro_spawn: bool) -> None:
    """Emit a once-per-process SECURITY warning when mode='off' results in
    no OS-level isolation and no verified delegation.

    This covers the gap where the documented mutual-exclusion invariant (above
    ``_KIRO_INTERNAL_SETTINGS_PATH``) is violated by an explicit mode='off'
    config without the kiro-cli delegation being active.
    """
    # Honour the same acknowledgment as _warn_no_isolation (SEC-009 opt-in).
    if _allow_no_isolation():
        if not getattr(_warn_mode_off_unconfined, "_info_logged", False):
            _warn_mode_off_unconfined._info_logged = True  # type: ignore[attr-defined]
            logger.info(
                "agent.sandbox='off' with no active delegation; operator opted "
                "in via sandbox_allow_no_isolation. Command: %s",
                _command_log_label(argv),
            )
        return

    # Per-branch latch so a non-kiro spawn doesn't suppress the kiro-spawn warning.
    _warned_set: set = getattr(_warn_mode_off_unconfined, "_warned_set", set())

    if is_kiro_spawn and sys.platform == "darwin":
        if "darwin_kiro" in _warned_set:
            return
        _warned_set.add("darwin_kiro")
        logger.warning(
            "SECURITY: agent.sandbox='off' but kiro-cli's internal sandbox is "
            "NOT enabled (~/.kiro/settings/amazon-internal.json). Both isolation "
            "layers are inactive — ~/.aws, ~/.ssh and other secrets are readable "
            "by the agent subprocess and only the bypassable app-level "
            "security.py checks remain. Set agent.sandbox='auto' or enable "
            "kiro-cli's internal sandbox to restore OS-level confinement. "
            "Command: %s",
            _command_log_label(argv),
        )
    elif sys.platform.startswith("linux"):
        if "linux" in _warned_set:
            return
        _warned_set.add("linux")
        logger.warning(
            "SECURITY: agent.sandbox='off' on Linux — there is no kiro-cli "
            "delegation mechanism on this platform, so the agent subprocess "
            "runs with NO OS-level confinement. ~/.aws, ~/.ssh and other "
            "secrets are readable by it and only the bypassable app-level "
            "security.py checks remain. Set agent.sandbox='auto' to engage "
            "namespace isolation. Command: %s",
            _command_log_label(argv),
        )
    elif sys.platform == "win32":
        if "win32" in _warned_set:
            return
        _warned_set.add("win32")
        logger.warning(
            "SECURITY: agent.sandbox='off' on Windows — no OS-level sandbox "
            "backend exists on this platform. The agent subprocess runs with "
            "full filesystem access. Command: %s",
            _command_log_label(argv),
        )
    else:
        if "other" in _warned_set:
            return
        _warned_set.add("other")
        logger.warning(
            "SECURITY: agent.sandbox='off' for a non-kiro-cli subprocess — "
            "running without OS-level confinement. Set agent.sandbox='auto' "
            "to engage seatbelt isolation. Command: %s",
            _command_log_label(argv),
        )

    _warn_mode_off_unconfined._warned_set = _warned_set  # type: ignore[attr-defined]


def _warn_first_party_unconfined_once(argv: list[str]) -> None:
    """One-shot loud SECURITY warning for the first-party no-backend carve-out.

    Per-process sentinel, mirroring :func:`_warn_mode_off_unconfined`'s latch
    style: the trigger is the HOST having no backend, so without the latch every
    managed MCP probe would repeat the same paragraph on every discovery cycle.
    """
    if getattr(_warn_first_party_unconfined_once, "_warned", False):
        return
    _warn_first_party_unconfined_once._warned = True  # type: ignore[attr-defined]
    logger.warning(
        "SECURITY: no OS-level sandbox backend on this host — spawning a "
        "first-party fixed-argv Kiro Crew helper UNCONFINED (its full command "
        "line is derived inside this package with no agent, repo, or "
        "user-config input; the credential environment is scrubbed). "
        "Hostile-input spawn paths are unaffected: they keep failing closed "
        "and still require agent.sandbox_allow_unsandboxed_exec=true. "
        "Command: %s",
        _command_log_label(argv),
    )


def _first_party_no_backend_passthrough(
    argv: list[str], sandbox_level: str, strip_python_env: bool
) -> tuple[list[str], str | None]:
    """Allowed path of the first-party carve-out in :func:`wrap_argv`.

    Reached only when the caller passed ``first_party_fixed_argv=True``, the
    backend unavailability class is ``no_backend``, and no governance
    ``sandbox.min_level`` floor is active (all checked by the caller). Applies
    the same env scrub as the other unconfined-but-deliberate paths, warns
    loudly once per process, and SEL-audits with a DISTINCT third outcome:
    ``unconfined`` — deliberately neither ``denied`` (nothing was refused) nor
    the nested-passthrough ``allowed`` (nothing confines this spawn).

    SEL failure here is log-and-proceed, matching the ``mode="off"`` delegation
    precedent: the spawn is first-party with a package-derived argv, and the
    alternative is bricking built-in tooling on audit hiccups.

    Deliberately NOT ``critical=True``: unlike the fail-closed ``denied`` and
    nested-passthrough ``allowed`` audits — rare, one-per-condition events —
    this fires for every managed-server probe on every discovery cycle of a
    backend-less host, and the critical path drains + flushes SYNCHRONOUSLY on
    the caller's thread, which here is the gateway event loop (async
    ``probe_server``). A best-effort async write keeps the loop responsive; the
    tamper-evident record still lands via the background writer.
    """
    _warn_first_party_unconfined_once(argv)
    try:
        from kiro_crew.sel import sel  # circular import: sandbox is low-level

        sel().log_tool_invocation(
            session_key="sandbox",
            agent="system",
            source="sandbox.wrap_argv",
            tool_name=_command_log_label(argv),
            tool_kind="subprocess",
            outcome="unconfined",
            resources="first-party fixed argv, no sandbox backend (issue #1563 carve-out)",
        )
    except Exception:
        logger.warning(
            "SEL audit failed for first-party unconfined spawn — proceeding "
            "unaudited: the argv is package-derived and denying the spawn "
            "would brick built-in tooling whenever SEL hiccups (matches the "
            "mode=off delegation posture). Command: %s",
            _command_log_label(argv),
            exc_info=True,
        )
    # Same env scrub as the seatbelt / delegation paths, via the trusted
    # absolute-path ``env`` binary (never a PATH-resolved shim). Where no such
    # binary exists — Windows, the main no-backend host — the argv-level scrub
    # cannot run; that is acceptable ONLY because every ratchet-allowlisted
    # caller routes through ``sandboxed_spawn_argv``, whose ``scrub_env`` drops
    # a superset of these keys from the child environment it returns.
    scrub_keys = _sandbox_env_scrub_keys(sandbox_level, strip_python_env)
    if scrub_keys:
        env_argv = _unset_env_argv(tuple(scrub_keys))
        if env_argv is not None:
            return [*env_argv, *argv], None
        logger.warning(
            "first-party unconfined spawn: no trusted `env` binary for the "
            "argv-level scrub; relying on the chokepoint's scrub_env for the "
            "child environment"
        )
    return list(argv), None


def detect_backend(config_mode: str = "auto") -> str:
    """Detect the best available sandbox backend.

    Cache policy (a single transient fork failure must not poison the cache and
    fail-close every spawn until restart):

    - A positive result (``"namespace"``/``"sandbox-exec"``) is cached for the
      process lifetime — kernel capability does not change while running.
    - ``"none"`` is cached ONLY when the userns probe failure looks permanent
      (kernel refuses user namespaces: EPERM/EINVAL/ENOSYS). A transient
      resource failure (fork EAGAIN, EMFILE, ...) is never cached — the next
      spawn re-probes and self-heals.
    - ``config_mode="off"`` short-circuits to ``"none"`` without probing and
      without touching the cache. All other modes share one cache entry:
      backend capability is mode-independent, so mode alternation does not
      force pointless re-probes.
    """
    global _backend
    if config_mode == "off":
        return "none"
    if _backend is not None:
        return _backend
    if userns_available():
        _backend = "namespace"
    elif _probe_sandbox_exec():
        _backend = "sandbox-exec"
    else:
        transient, reason, _remedy = _last_unshare_failure or (False, "none", "")
        if transient:
            logger.warning(
                "Sandbox backend probe failed transiently (%s); result NOT cached — "
                "the next spawn re-probes",
                reason,
            )
            return "none"
        _backend = "none"
    logger.info("Sandbox backend: %s (config_mode=%s)", _backend, config_mode)
    return _backend


#: Env marker the cron *script* launcher sets on its child -- the one way to tell
#: that child apart at a spawn site every caller shares.
CRON_SCRIPT_CHILD_ENV = "_KIROCREW_CRON_SCRIPT_CHILD"


class UnauditedSpawnRefused(BaseException):
    """A cron script child refused to proceed after an ENOSYS audit failure.

    ``BaseException`` because it is raised inside the user function's own stack
    (``ctx.call_tool`` re-enters ``wrap_argv``), and a script's own ``except
    Exception`` must not be able to swallow it and return a success envelope.
    """


def refuse_unaudited_on_dead_fs(exc: BaseException, what: str) -> None:
    """Turn a best-effort audit degrade into a refusal, for a cron script child.

    Log-and-proceed is right for the gateway: denying a spawn on an audit hiccup
    would brick built-in tooling and every in-sandbox MCP call. It is wrong for a
    detached cron child, whose ``ENOSYS`` write means its filesystem is gone, so it
    can neither audit nor persist what it does next. Both conditions are required.
    """
    if os.environ.get(CRON_SCRIPT_CHILD_ENV) != "1":
        return
    # SEL wraps its writes, so the errno can sit a link or two down the chain;
    # ``seen`` bounds a cyclic one.
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        if isinstance(cur, OSError) and cur.errno == errno.ENOSYS:
            raise UnauditedSpawnRefused(
                f"{what}: the security-event log write failed with ENOSYS (errno 38), "
                "so this cron child can neither audit nor persist -- refusing to "
                "continue unaudited."
            ) from exc
        seen.add(id(cur))
        cur = cur.__cause__ or cur.__context__


class SandboxUnavailableError(RuntimeError):
    """``wrap_argv`` fail-closed because this host could not build a sandbox.

    A typed error so a caller can tell "the sandbox refused this spawn" apart
    from any other spawn failure **structurally**, instead of inferring it from
    host capability or pattern-matching English prose. That distinction matters:
    verification is not sandboxed on every platform (``_run_process`` skips the
    wrap on Windows) and the ``sandbox_allow_unsandboxed_exec`` opt-in bypasses
    it entirely, so "this host has no backend" does NOT imply "the sandbox is
    why this particular spawn failed". Reporting it that way would recreate the
    misdiagnosis class of #613 on a different platform.

    Subclasses ``RuntimeError`` so existing ``except RuntimeError`` handlers keep
    working unchanged.

    ``kind`` is machine-readable so a presentation layer can select its own
    translated remedy copy: ``"transient"`` (momentary resource pressure — not
    cached, retrying works, and callers must NOT advise disabling the sandbox),
    ``"foreign_sandbox"`` (an outer Seatbelt sandbox KiroCrew did not create
    already confines this process and Seatbelt cannot nest — this host's sandbox
    is fine), or ``"no_backend"`` (the host genuinely offers no mechanism).

    ``detail`` is the technical probe reason, which names the failing step (e.g.
    ``"unshare(CLONE_NEWNS) failed with errno 1 (EPERM)"``).

    ``remedy`` is a machine-readable ``REMEDY_*`` token naming the host mechanism
    behind a Linux userns denial (``""`` when unknown), so a presentation layer
    can render the concrete fix for that mechanism rather than a bare errno.
    """

    def __init__(self, message: str, kind: str, detail: str, remedy: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.detail = detail
        self.remedy = remedy


#: Leading text of every line the Linux launcher writes when it ends WITHOUT
#: exec'ing its child: ``_mount_or_die`` and the seccomp/prctl installs say
#: ``sandbox: BLOCKED``, the two unshare steps say ``sandbox: unshare(``, a
#: broken parent/child handshake or an unreadable ``known_hosts`` says
#: ``sandbox: FATAL``, and a launcher invoked with no command says
#: ``sandbox_launcher:``. The launcher is the only Kiro Crew code that writes
#: a line shaped this way to a child's stderr, and it exits right after, so a
#: refused child's captured output carries exactly one of these. Public because
#: it is the ONE definition every consumer keys on — :func:`launcher_refusal`
#: here and the clone probe's exit classifier in
#: ``apps/builtins/auto_improvement/backend/clone_setup.py`` — and each prefix
#: is pinned against the generated launcher by a round-trip test so the tuple
#: cannot drift from the script that emits it. ``sandbox: WARNING`` is
#: deliberately absent: the launcher warns and then still runs the child.
LAUNCHER_EXIT_PREFIXES = (
    "sandbox: BLOCKED",
    "sandbox: FATAL",
    "sandbox: unshare(",
    "sandbox_launcher:",
)
_LAUNCHER_REFUSAL_RE = re.compile(
    r"^sandbox: (?:BLOCKED (?:--|—) )?(?P<what>.+?) failed: errno (?P<errno>\d+)"
)


def launcher_refusal(output: str) -> tuple[str, str, str] | None:
    """Classify the launcher's OWN refusal line in a refused child's output.

    ``wrap_argv`` raises :class:`SandboxUnavailableError` when the sandbox
    cannot be built BEFORE the spawn. The launcher can also refuse AFTER it — a
    host that passed the probe can still deny a control at spawn time, and a
    probe result cached under one policy can outlive a policy change. That
    refusal reaches the caller only as a non-zero exit with the launcher's line
    in the captured stderr, which every caller so far read as "the child is
    broken": a present, signed-in kiro-cli was reported as not installed because
    a container refused the launcher's first mount.

    Returns the same ``(kind, detail, remedy)`` triple the typed error carries,
    or ``None`` when *output* holds no launcher refusal. Keys ONLY on the
    launcher's fixed prefixes, never on host capability.

    **The line is untrusted, and this function is the CLASSIFIER, not the
    verdict.** The child the launcher would have exec'd is the unverified
    candidate itself, running inside the sandbox this module built, and a
    planted binary can print the same prefix to stderr and exit 1. A caller that
    reported this triple as a sandbox verdict would let that binary make the
    first-run gate announce "sandbox unavailable" and hand the operator the
    opt-out that disables the isolation it is running under. Callers deciding a
    verdict use :func:`corroborate_launcher_refusal`, which re-runs the real
    launcher around a trusted no-op with the refused spawn's own options and
    classifies THAT run's own stderr through this function; the candidate's line
    only decides whether to make that trusted run. This function is for
    messaging (a diagnostic that quotes the line) and for that corroboration's
    hint.

    * A refused ``mount(2)`` or ``unshare(2)`` is ``no_backend`` — the host
      cannot build the sandbox — and its errno is classified exactly as the
      probe classifies the same step, so the remedy names the same mechanism
      (a hiding mount refused with EACCES is the same policy that refuses the
      propagation mount, so it shares :data:`REMEDY_MOUNT_DENIED`).
    * The seccomp/prctl installs are ``no_backend`` with no mechanism token: the
      host offers no way to apply the filter.
    * A broken parent/child handshake (``FATAL ... did not publish``) is
      ``transient``: the pipe protocol failed, which says nothing about the
      host and self-heals on the next spawn.
    * A refusal about HOST STATE is NOT a sandbox failure at all — a hardlinked
      credential, an unreadable ``known_hosts``: the sandbox worked and found
      something it must not paper over. Reported as ``None`` so the launcher's
      line reaches the operator as the child's own complaint, which names the
      file to fix. A launcher invoked with no command (``sandbox_launcher:``)
      is the caller's defect, ``None`` for the same reason. Advisory
      ``sandbox: WARNING`` lines never match: the launcher continued past them.
    """
    for raw in output.splitlines():
        line = raw.strip()
        if not line.startswith(LAUNCHER_EXIT_PREFIXES):
            continue
        if line.startswith("sandbox_launcher:"):
            return None
        if line.startswith("sandbox: FATAL"):
            return ("transient", line, "") if "did not publish" in line else None
        if "hardlink" in line:
            return None
        match = _LAUNCHER_REFUSAL_RE.match(line)
        if match is None:
            return ("no_backend", line, "")
        what = match.group("what")
        err = int(match.group("errno"))
        if what.startswith("unshare(NEWUSER)"):
            step = _PROBE_STEP_NEWUSER
        elif what.startswith("unshare(NEWNS)"):
            step = _PROBE_STEP_NEWNS
        else:
            step = _PROBE_STEP_MOUNT_PRIVATE
        return ("no_backend", line, _remedy_for_step(step, err))
    return None


#: Wall-clock ceiling for the trusted corroboration run. The child is ``true``,
#: which returns at once, so the budget is for the launcher's own setup (the
#: namespace unshares, the propagation mount, the seccomp install) and a
#: momentarily loaded host, and it bounds the worker thread so a wedged launcher
#: cannot stall the caller.
_TRUSTED_LAUNCHER_TIMEOUT_SECS = 15.0


def corroborate_launcher_refusal(
    output: str,
    *,
    mode: str = "strict",
    extra_hidden_dirs: tuple[str, ...] = (),
    extra_visible_dirs: tuple[str, ...] = (),
) -> tuple[str, str, str] | None:
    """A post-spawn sandbox verdict for a refused child -- from a TRUSTED launcher run.

    The launcher's refusal line (:func:`launcher_refusal`) is the only signal a
    post-spawn refusal leaves, and it cannot be trusted on its own: the child
    was the unverified candidate, and a planted ``kiro-cli`` can print the same
    line and exit 1. So the candidate's line decides only WHETHER to look
    further. The verdict comes from re-running the REAL launcher around a
    trusted no-op (``true``), under the refused spawn's own ``mode`` and
    hidden/visible dirs, and classifying THAT run's own stderr -- text no child
    wrote.

    Running the whole launcher (not the boot probe) is what makes this cover
    EVERY launcher step. The probe mirrors only the first three -- the two
    unshares and the propagation mount -- so a policy that refused a LATER step
    (a hiding bind mount, ``NO_NEW_PRIVS``, the seccomp-BPF install) left the
    probe building the sandbox while every real spawn died, and the refusal was
    reported as ``installed=False``. The no-op runs every step, so its stderr
    carries whichever one this host refuses.

    * No launcher line in *output*: ``None``, nothing run.
    * Off Linux: ``None``, nothing run. The prefixes are the Linux launcher's,
      so elsewhere a matching line can only be the child's own text.
    * No trusted ``true`` on the host: ``None`` -- nothing safe to run.
    * The sandbox cannot even be built for the no-op: the pre-spawn
      :class:`SandboxUnavailableError`'s own ``(kind, detail, remedy)``.
    * The no-op run times out: ``None``.
    * The no-op run exits 0: ``None``. The candidate's line was forged, or the
      refusal was momentary; either way the text reaches the operator as the
      child's own complaint, never as a sandbox verdict.
    * The no-op run exits non-zero: :func:`launcher_refusal` of ITS stderr -- a
      real launcher refusal classified into the triple, or ``None`` when that
      trusted line is host state (a hardlinked credential) rather than a sandbox
      failure.

    The candidate's own text can never become the verdict on any branch. The
    real launcher blocks on a subprocess, so a caller on an event loop runs this
    in a thread. It touches the backend cache for nothing: a spawn's report must
    not move a process-wide verdict, even by way of a real launcher run.
    """
    if launcher_refusal(output) is None:
        return None
    if sys.platform != "linux":
        return None
    noop = platform_compat.trusted_system_bin("true")
    if noop is None:
        return None
    try:
        argv, env, cleanup_path = sandboxed_spawn_argv(
            [noop],
            mode=mode,
            strip_python_env=True,
            extra_hidden_dirs=extra_hidden_dirs,
            extra_visible_dirs=extra_visible_dirs,
        )
    except SandboxUnavailableError as exc:
        # The launcher could not be built at all for the trusted no-op, so the
        # host offers the same verdict a pre-spawn refusal would have carried.
        return (exc.kind, exc.detail, exc.remedy)
    try:
        # The spawn stays in this function, next to the ``sandboxed_spawn_argv``
        # call that built its argv, so the spawn audit can see the routing and
        # the resource ceiling together. ``stdin`` is closed so the child
        # cannot block on input, and stderr is decoded leniently because it is
        # the launcher's diagnostic, not structured data.
        completed = run_limited(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TRUSTED_LAUNCHER_TIMEOUT_SECS,
        )
    except subprocess.TimeoutExpired:
        # A launcher that will not return in the budget says nothing certain
        # about the host; the operator still has the candidate's own line.
        return None
    finally:
        if cleanup_path is not None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(cleanup_path)
    if completed.returncode == 0:
        return None
    return launcher_refusal(completed.stderr)


def reset_backend() -> None:
    """Reset cached backend (for testing or config change)."""
    global _backend, _last_unshare_failure
    _backend = None
    _last_unshare_failure = None


def credential_mask_applies(mode: str) -> bool:
    """Whether :func:`wrap_argv` would actually APPLY ``extra_hidden_dirs`` for *mode*.

    Exactly two outcomes hand back an UNWRAPPED child, dropping the mask: the ``off``
    tier, and a host with no backend where unsandboxed exec is opted in and no
    governance floor mandates a sandbox (the ``return argv, None`` after
    ``_warn_no_isolation``). Every other path either wraps the argv -- ``namespace``
    and ``sandbox-exec`` both thread ``extra_hidden_dirs`` through -- or REFUSES the
    spawn outright with :class:`SandboxUnavailableError`, and a refusal needs no guard
    because nothing starts.

    Note this predicate is STRICTER than that inventory on one path: with no
    backend it never consults the unsandboxed-exec opt-in, so the opted-in host
    answers False rather than tracking that mutable value. See the branch below.

    This lives here, beside those branches, so a caller whose security argument
    depends on the mask cannot drift from them: a future branch that skips the mask
    is a change to this function, not a silent hole in some other module's copy of
    the reasoning.
    """
    floor = _governance_sandbox_floor()
    effective = _clamp_sandbox_mode_to_floor(mode, floor)
    if effective == "off":
        return False
    # Already inside a Kiro Crew sandbox: a nested re-wrap is impossible by design,
    # wrap_argv passes the argv through (at most an env scrub) and never reaches a
    # backend that could apply the mask. The OUTER sandbox confines the child, but it
    # was built for the tier's own hidden dirs -- which deliberately leave ~/.aws,
    # ~/.ssh and ~/.kube readable for kiro-cli's sake -- so it is NOT a substitute for
    # an adapter-specific credential mask.
    if _inside_kirocrew_sandbox() and _macos_sandbox_state() is not False:
        return False
    if detect_backend(config_mode=effective) != "none":
        return True
    # backend == "none": FAIL CLOSED, reading NO policy value to decide it.
    #
    # Nothing on a host without a backend can carry ``extra_hidden_dirs``, so the
    # only question was whether the spawn would be refused instead -- and every
    # answer to THAT is mutable config read here at preflight and acted on at the
    # spawn. The opt-in was the first such value (an operator opting in between the
    # two let a session that had been told "the mask applies" hand back an unwrapped
    # child); the governance floor is the second, because a ceiling LOOSENED in the
    # same window drops the very refusal that made True safe to report. Both windows
    # close only by refusing to derive this from policy at all: no backend, no mask,
    # so no enforced adapter starts here regardless of how policy moves.
    #
    # The cost is that a no-backend host cannot run an enforced adapter even under a
    # governance floor that forbids unsandboxed execution. That host could not run
    # one anyway -- ``wrap_argv`` cannot satisfy the floor without a backend and
    # raises -- so this changes which layer reports it, not whether it works. Only an
    # ENFORCED adapter reaches here at all: ``enforce_sandbox_floor`` returns early
    # for every harness this core does not enforce, so no first-class path changes.
    return False


def spawn_delegates_masking() -> bool:
    """Whether the agent spawn is DELEGATED, so Crew's own hidden-dir mask never runs.

    :func:`credential_mask_applies` answers whether ``wrap_argv`` would thread
    ``extra_hidden_dirs`` through the backend it selects. That is the right
    question for a mode/backend hole and the wrong one for a DELEGATION hole: on
    macOS with kiro-cli's internal sandbox enabled, a backend is present, so that
    predicate answers True, and yet the spawn is handed to kiro-cli
    (``_delegate_to_kiro_internal_sandbox``) and Crew's mask is not applied at
    all. Native Windows delegates for the same reason with no Crew backend to
    apply.

    Kept here rather than in a caller, for the reason
    :func:`credential_mask_applies` states about itself: a control whose security
    argument depends on the mask must not carry its own copy of when the mask is
    skipped. It is a SEPARATE predicate rather than a widening of that one
    because the two answer different questions, and their existing callers depend
    on the narrower answer -- a caller that only needs "would the backend carry
    the mask" must not start refusing a delegated spawn it never cared about.

    Read-only, and never raises: an unreadable delegation setting reads as
    DELEGATED, which is the fail-closed direction -- a mask that may not run is
    not trusted.
    """
    try:
        if sys.platform == "win32":
            return True
        return bool(sys.platform == "darwin" and kiro_internal_sandbox_enabled())
    except Exception:  # noqa: BLE001 -- an unverifiable setting is delegated, not trusted
        return True


def unconfined_live_agent_pid(pids: "Iterable[int]") -> int | None:
    """The first pid among *pids* that is NOT actually confined, or ``None``.

    Confinement is decided at SPAWN by :func:`wrap_argv`, and ``agent.sandbox``
    is a live setting -- it carries no ``restart=True`` marker, so it reaches the
    running gateway the moment it is saved. A session spawned while the tier was
    ``off`` therefore stays unconfined after the config flips, and a control that
    reads only :func:`configured_sandbox_mode` is asking about the NEXT spawn
    while the hazard is a process already running. This asks about the processes.

    Reuses the platform predicates ``member_memory_auth`` uses for the same
    question rather than inventing a second answer: on Linux a confined child
    holds different user/mount namespaces than the gateway, so MATCHING
    namespaces mean unconfined; on macOS Seatbelt membership is read directly.
    Both return ``None`` when the answer cannot be read, and an unreadable
    process counts as UNCONFINED -- "cannot verify" is not "is confined".

    Native Windows has no Crew confinement to verify, so every pid there answers
    unconfined and a caller whose security argument needs the mask refuses on
    that platform, which is the same posture ``spawn_delegates_masking`` takes.
    """
    for pid in pids:
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
            return pid if isinstance(pid, int) and not isinstance(pid, bool) else -1
        try:
            if sys.platform == "linux":
                if platform_compat.process_namespaces_match(pid, os.getpid()) is not False:
                    return pid
            elif sys.platform == "darwin":
                if platform_compat.process_is_sandboxed(pid) is not True:
                    return pid
            else:
                return pid
        except Exception:  # noqa: BLE001 -- unreadable is unconfined, never confined
            return pid
    return None


def wrap_argv(
    argv: list[str],
    mode: str = "auto",
    *,
    strip_python_env: bool = False,
    forward_ssh_auth_sock: bool = False,
    extra_hidden_dirs: tuple[str, ...] = (),
    extra_hidden_dir_ids: tuple[tuple[str, int, int], ...] = (),
    extra_alias_credential_ids: tuple[tuple[int, int], ...] = (),
    extra_visible_dirs: tuple[str, ...] = (),
    extra_private_dirs: tuple[str, ...] = (),
    extra_private_dir_ids: tuple[tuple[str, int, int], ...] = (),
    extra_writable_dirs: tuple[str, ...] = (),
    extra_expose_files: tuple[str, ...] = (),
    is_kiro_cli: bool | None = None,
    first_party_fixed_argv: bool = False,
) -> tuple[list[str], str | None]:
    """Wrap a command argv with OS-level sandbox if available.

    Args:
        argv: Original command + args.
        mode: ``"auto"``/``"standard"`` (expose .aws/.ssh/.kube),
              ``"cc"`` (hide .aws but expose .aws/config for Bedrock auth),
              ``"strict"`` (hide everything), ``"off"`` (no sandbox).
        extra_hidden_dirs: Additional absolute directory trees to deny.
        extra_visible_dirs: Trusted paths that must remain visible when an
            otherwise-hidden parent contains them (the whole parent's mask is lifted).
        extra_private_dirs: The spawn's OWN directories inside a hidden tree
            (its ``agent_scratch`` dir under the masked scratch root). Re-exposed
            read-write as a window; the parent's mask and every sibling stay hidden.
        extra_expose_files: Absolute files to keep READABLE inside dirs that
            ``extra_hidden_dirs`` hides. Linux restores a read-only COPY via
            the launcher's ``expose_files`` primitive (cc mode's mechanism
            for ``.aws/config``); Seatbelt carves a ``require-not (literal)``
            exception out of the hidden dir's read deny (the shape it uses
            for ``.ssh/known_hosts``). Writes and hardlinks stay denied on
            both.
        extra_writable_dirs: Self-derived scratch directories INSIDE the sealed
            runtime parent (``<data home>/run``) that the child must be able to
            write — e.g. the MCP probe's private ``TMPDIR``. Validated
            inside the plan by :func:`kiro_crew.sandbox_plan.writable_carveouts`;
            a candidate that would re-open any other seal is refused with a
            security warning. Inert on
            backends with no path seal to carve (Windows, the no-backend
            fail-open path): there the directory is already writable.
        is_kiro_cli: Explicit executable classification for descriptor-backed
            Kiro snapshots whose launch path has no ``kiro-cli``
            basename. ``None`` retains basename detection for other callers.
            Windows internal-sandbox delegation requires this to be exactly
            ``True``; basename inference can never grant that exception.
        first_party_fixed_argv: True ONLY for spawns whose full argv is derived
            inside this package with zero agent/repo/user-config influence;
            every passing site must be allowlisted in
            ``test/test_spawn_audit.py::FIRST_PARTY_SPAWNS``. On a host with
            genuinely no sandbox backend (``no_backend`` — never a transient
            probe failure or a foreign outer sandbox) and no governance
            ``sandbox.min_level`` floor, such a spawn proceeds unconfined
            (env-scrubbed, loudly warned, SEL ``outcome="unconfined"``) instead
            of fail-closing. Inert whenever a backend exists or
            ``sandbox_allow_unsandboxed_exec`` is set.

    Returns:
        (wrapped_argv, cleanup_path_or_None).
        *cleanup_path* is a temp file to delete after the child exits
        (macOS seatbelt profile or Linux launcher script).
        ``None`` when no cleanup is needed.

    Raises:
        RuntimeError: When no sandbox backend is available, mode is not "off",
            ``agent.sandbox_allow_unsandboxed_exec`` is False (default), and
            neither the first-party carve-out nor the explicitly classified
            Windows Kiro internal-sandbox delegation applies.
            This is the fail-closed behavior — the agent subprocess is NOT
            allowed to run without OS-level isolation unless explicitly opted in.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "wrap_argv() performs blocking sandbox preparation and cannot run on "
            "an event loop; await wrap_argv_async() instead"
        )

    # Governance ordinal floor: a policy/profile may require a MINIMUM sandbox
    # tier (off < standard < cc < strict).  Clamp the requested mode up to that
    # floor before resolving the level — so an enterprise "min_level: cc" makes
    # even a mode="off" call run confined.  Cheap no-op when ungoverned.
    #
    # ONE read per wrap_argv call, reused by the first-party carve-out below:
    # the (potentially profile-walking) resolve runs once, and the clamp and
    # the carve-out condition can never disagree about the same host.
    governance_floor = _governance_sandbox_floor()
    mode = _clamp_sandbox_mode_to_floor(mode, governance_floor)
    # The caller resolved the SSH forward against its own floor read. Re-judge
    # it against the tier this wrap applies, so a floor raised in between can
    # only drop the socket. Never turns a False into True.
    if forward_ssh_auth_sock:
        try:
            forward_ssh_auth_sock = _forward_for_effective_tier(mode, extra_hidden_dirs)
        except Exception:
            forward_ssh_auth_sock = False

    if mode == "off":
        # Fix #2: verify kiro-cli delegation before honoring "off". The
        # documented invariant (sandbox.py:1680-1681) requires that when
        # Kiro Crew's seatbelt is off, kiro-cli's internal sandbox is ON —
        # but the old early return never checked. Now we verify the delegation
        # on macOS kiro-cli spawns; on Linux (where kiro's internal sandbox
        # doesn't apply) or non-kiro spawns, "off" means genuinely unconfined.
        kiro_spawn_off = _spawns_kiro_cli(argv) if is_kiro_cli is None else is_kiro_cli
        if sys.platform == "darwin" and kiro_spawn_off and kiro_internal_sandbox_enabled():
            # Delegation is valid: kiro-cli's sandbox IS active. Apply env scrub
            # (same as _delegate_to_kiro_internal_sandbox) but WITHOUT the
            # seatbelt fallback on SEL failure — mode="off" must never produce a
            # nested seatbelt wrap (the exact EPERM case the design prevents).
            # SEL audit-or-degrade: record the delegation with critical=True
            # (synchronous write for tamper-evident log), but on failure degrade
            # to unconfined passthrough rather than seatbelt wrap (which would
            # EPERM inside kiro-cli's already-active sandbox).
            try:
                from kiro_crew.sel import sel

                sel().log_tool_invocation(
                    session_key="sandbox",
                    agent="system",
                    source="sandbox.wrap_argv",
                    tool_name=_command_log_label(argv),
                    tool_kind="subprocess",
                    outcome="delegated",
                    resources=(
                        "mode=off: kiro internal sandbox on -> env scrub only "
                        "(no seatbelt, no seatbelt-fallback)"
                    ),
                    critical=True,  # synchronous write for audit integrity
                )
            except Exception as exc:
                refuse_unaudited_on_dead_fs(exc, "mode=off delegation audit")
                # Fail OPEN (not to seatbelt): an unaudited delegation with
                # mode=off still applies env scrub but returns without seatbelt.
                # This is deliberately different from _delegate_to_kiro_internal_sandbox
                # which falls back to seatbelt — here that fallback would EPERM.
                logger.warning(
                    "SECURITY: SEL audit failed for mode=off delegation; "
                    "proceeding with env scrub but no seatbelt. Command: %s",
                    _command_log_label(argv),
                    exc_info=True,
                )
            unset_args = _sandbox_env_unset_args(
                "standard", strip_python_env, forward_ssh_auth_sock
            )
            if unset_args:
                return [_pinned_env_bin(), *unset_args, *argv], None
            return list(argv), None
        # Fix #3: Make the degradation loud — both layers are inactive.
        _warn_mode_off_unconfined(argv, kiro_spawn_off)
        return argv, None

    # Already inside a KiroCrew sandbox (script cron, sandboxed agent child, app
    # backend, pooled MCP server): the outer sandbox confines every descendant,
    # and a nested wrap is impossible by design — Linux seccomp denies the
    # unshare, macOS Seatbelt refuses sandbox_apply with EPERM. Pass through
    # within the existing isolation boundary.
    #
    # On macOS the marker must agree with the kernel, and the two cover each
    # other's blind spot: the marker proves KiroCrew built the outer sandbox and
    # scrubbed the credential env on the way in, but an env var alone could be
    # forged; the kernel independently confirms a sandbox IS active, but cannot
    # say whose profile it is, so it can never grant this on its own. A definite
    # kernel "not sandboxed" therefore vetoes the marker. An *unanswerable* probe
    # does not: that says nothing, and must not invalidate a marker the Linux
    # path honours unconditionally.
    if _inside_kirocrew_sandbox() and _macos_sandbox_state() is not False:
        if not getattr(wrap_argv, "_nested_passthrough_logged", False):
            wrap_argv._nested_passthrough_logged = True  # type: ignore[attr-defined]
            logger.info(
                "wrap_argv: already inside a KiroCrew sandbox — nested OS "
                "sandboxing is impossible by design (Linux seccomp denies "
                "unshare; macOS Seatbelt refuses sandbox_apply with EPERM); "
                "spawning within the existing isolation boundary rather than "
                "fail-closing on a nesting artifact"
            )
        # Compare the tier the caller asked for against the tier the OUTER
        # sandbox was built at. The passthrough is unavoidable (a nested
        # re-wrap is denied by design on both platforms), but a tier
        # downgrade must be visible, not silent. ``unknown`` = launcher
        # predates the level export (or the value is unrecognized); it has no
        # ordinal, so no downgrade can be proven against it.
        requested_level = _mode_to_level(mode)
        active_level = os.environ.get(_IN_SANDBOX_LEVEL_VAR) or "unknown"
        if active_level not in _TIER_ORDINALS:
            active_level = "unknown"
        tier_downgrade = (
            active_level in _TIER_ORDINALS
            and _TIER_ORDINALS[requested_level] > _TIER_ORDINALS[active_level]
        )
        if tier_downgrade:
            # Loud and per-call (not once-only like the info log above): every
            # downgraded spawn is a distinct security-relevant event.
            logger.warning(
                "SECURITY: nested-sandbox passthrough tier downgrade — caller "
                "requested %r but the outer sandbox runs at %r, so %s executes "
                "at the weaker tier (a nested re-wrap is impossible by design). "
                "Applying the stricter tier's env scrub to the passthrough.",
                requested_level,
                active_level,
                _command_log_label(argv),
            )
        # Emit an SEL audit event for this security-relevant passthrough so the
        # decision to spawn without a *fresh* wrap is tamper-evidently recorded,
        # mirroring the ``denied`` event on the fail-closed path. Outcome is
        # ``allowed`` (a permission grant, not a denial). Fires on EVERY
        # passthrough so the audit trail is complete.
        #
        # critical=True gives this the same write reliability as the fail-closed
        # ``denied`` audit and the ``delegated`` audit: the event is written
        # SYNCHRONOUSLY after draining the async backlog (sel.log), so a
        # slow/wedged background writer can NOT silently drop passthrough
        # records. What it does NOT do is re-raise into a *deny*: unlike
        # _delegate_to_kiro_internal_sandbox — which on audit failure falls back
        # to KiroCrew's own seatbelt, an equally-safe audited layer — a nested
        # passthrough has no safe alternative (seccomp denies the re-wrap by
        # design). Failing the spawn on a SEL filesystem error would couple every
        # in-sandbox MCP call to SEL health and reintroduce a prior in-sandbox
        # spawn outage. The child is confined by the outer namespace + seccomp
        # whether or not the record lands, so on a hard write failure we log
        # loudly and proceed: availability of the confinement over a best-effort
        # audit gap during an already-degraded FS.
        try:
            # circular import (see the fail-closed branch below for the full
            # rationale): sandbox.py is a low-level leaf; defer the sel import.
            from kiro_crew.sel import sel

            sel().log_tool_invocation(
                session_key="sandbox",
                agent="system",
                source="sandbox.wrap_argv",
                tool_name=_command_log_label(argv),
                tool_kind="subprocess",
                outcome="allowed",
                metadata={
                    "reason": "nested_sandbox_passthrough",
                    "mode": mode,
                    "requested_tier": requested_level,
                    "active_tier": active_level,
                    # tier_known separates "proven no downgrade" from
                    # "unprovable": tier_downgrade=False alone cannot tell a
                    # consumer which of the two it is looking at.
                    "tier_known": active_level in _TIER_ORDINALS,
                    "tier_downgrade": tier_downgrade,
                },
                critical=True,
            )
        except Exception as exc:
            refuse_unaudited_on_dead_fs(exc, "nested-sandbox passthrough audit")
            logger.warning(
                "SEL audit failed for nested-sandbox passthrough — proceeding "
                "unaudited: the outer namespace + seccomp still confine this "
                "spawn, and denying it would brick in-sandbox MCP calls whenever "
                "SEL is down",
                exc_info=True,
            )
        if tier_downgrade:
            # The one slice of the stricter tier that IS enforceable without a
            # nested wrap: its env scrub. A standard outer sandbox scrubbed
            # only _SENSITIVE_ENV_PREFIXES, so agent-denied credential keys
            # (Slack tokens, owner id) are still in this environment; prefix
            # the child with the requested tier's ``env -u`` scrub so it does
            # not inherit them (a delta in practice — the outer launcher
            # already removed the shared prefixes). File-level hides still run
            # at the outer tier — that residual gap is exactly what the audit
            # above records. The ``env`` binary is resolved at a trusted
            # absolute path only (:func:`_unset_env_argv`): this environment
            # can carry a PATH that leads with user-writable directories, and
            # a planted ``env`` there would receive exactly the credentials
            # this scrub exists to withhold. No trusted binary → keep the
            # plain passthrough (never fail closed) and say so.
            unset_args = _sandbox_env_unset_args(
                requested_level, strip_python_env, forward_ssh_auth_sock
            )
            if unset_args:
                scrub_keys = tuple(unset_args[1::2])
                env_prefix = _unset_env_argv(scrub_keys)
                if env_prefix is not None:
                    return [*env_prefix, *argv], None
                logger.warning(
                    "SECURITY: no trusted env binary (%s) — the requested "
                    "tier's env scrub cannot be applied to this passthrough; "
                    "spawning without it",
                    ", ".join(_ENV_BINARY_CANDIDATES),
                )
        return argv, None
    if _inside_kirocrew_sandbox():
        # Marker present, kernel says NOT sandboxed: the marker can only have been
        # forged or inherited into an unconfined process. Refuse the passthrough
        # and fall through to a normal wrap.
        logger.warning(
            "SECURITY: %s is set but the kernel reports this process is NOT "
            "sandboxed — refusing the nested-sandbox passthrough and falling back "
            "to a normal wrap.",
            _IN_SANDBOX_MARKER,
        )

    # "auto"/"standard" allows git-over-SSH, AWS CLI, kubectl.
    # "cc" hides .aws (exposes only .aws/config for Bedrock credential_process).
    # "strict" hides everything.
    sandbox_level = _mode_to_level(mode)

    # The ONE place an aliased strict leaf is REPORTED, covering every spawn this function can
    # produce: the Linux namespace wrap, the macOS Seatbelt wrap, the delegated kiro-cli spawn,
    # and the Windows no-backend path. A rule stated once per platform branch is a rule each
    # branch can be edited out of independently, and this one belongs to none of them: it is a
    # fact about a NAME, settled before any mechanism is chosen. What stays platform-specific
    # below is the mechanism that enforces a seal.
    #
    # It WARNS and lets the spawn through. The refusal lives where the file is CONSUMED --
    # `provisioners.engine_for` for `cloud.json`, `LaunchState.load` for the launch record --
    # because refusing here refused every sandboxed spawn on the host, a chat turn, a cron job,
    # a subagent, whenever a leaf carried a second name, and an install laid down by stow,
    # chezmoi or `rsync --link-dest` has that shape for reasons that have nothing to do with
    # Fargate. The blast radius was the whole box; the exposure is one command. The alias harm
    # is a refused command either way, and nothing else stops working.
    #
    # The warning matters most on the path that applies no seal of ours. A delegated spawn is
    # confined by kiro-cli's own sandbox and Crew wraps nothing, so a write reaching
    # `cloud.json` through an alias whose target sits outside the data home chooses the
    # container image a Fargate launch runs, and the task's execution role hands the model
    # credential to it. The check reads no file contents, so it needs no knowledge of which
    # layer owns isolation and there is no encoding to bypass. Absent leaves cost nothing:
    # `_warn_if_alias_backed` returns when the lstat fails.
    #
    # The passthrough tiers above return before this point, which is deliberate: with no
    # sandbox at all Crew claims no seal, so there is nothing here to be bypassed.
    _warn_aliased_strict_leaves()

    # macOS sandbox mutual exclusion: kiro-cli >= 2.13's internal sandbox cannot
    # initialize nested inside KiroCrew's seatbelt (kernel EPERM even under an
    # allow-all outer profile), so exactly one layer can own isolation. When
    # kiro's internal sandbox is enabled, it is that layer for kiro-cli spawns;
    # KiroCrew's sandbox stays on for everything else and whenever kiro's is off.
    # Windows has no Kiro Crew OS sandbox backend. Official Kiro ACP spawns are
    # positively classified by their reviewed callers and delegate to Kiro's
    # built-in sandbox by default; basename inference is deliberately
    # insufficient to grant this exception. All other Windows spawns retain the
    # no-backend fail-closed path. Checked before backend detection so this is a
    # deterministic capability decision, never a fallback after a probe failure.
    # Linux namespace isolation is unaffected.
    kiro_spawn = _spawns_kiro_cli(argv) if is_kiro_cli is None else is_kiro_cli
    delegate_to_kiro = (
        sys.platform == "darwin" and kiro_spawn and kiro_internal_sandbox_enabled()
    ) or (sys.platform == "win32" and is_kiro_cli is True)
    if delegate_to_kiro:
        # ``extra_private_dirs`` is deliberately NOT in this test: a private
        # window only RELAXES a mask owned by Kiro Crew (the scratch root) for the
        # spawn's own directory. A delegated sandbox applies none of those
        # masks, so the window is moot there and must not cost the delegation
        # (on Windows that would send every session to the no-backend path).
        if (
            extra_hidden_dirs
            or extra_hidden_dir_ids
            or extra_visible_dirs
            or extra_writable_dirs
            or extra_expose_files
        ):
            # A delegated sandbox cannot enforce KiroCrew-specific path hides.
            # macOS keeps the outer seatbelt. Windows falls through to its
            # no-backend policy and fail-closes unless explicitly opted in.
            if sys.platform == "darwin":
                return sandbox_exec_argv(
                    argv,
                    sandbox_level,
                    strip_python_env=strip_python_env,
                    forward_ssh_auth_sock=forward_ssh_auth_sock,
                    extra_hidden_dirs=extra_hidden_dirs,
                    extra_hidden_dir_ids=extra_hidden_dir_ids,
                    extra_visible_dirs=extra_visible_dirs,
                    extra_private_dirs=extra_private_dirs,
                    extra_private_dir_ids=extra_private_dir_ids,
                    extra_writable_dirs=extra_writable_dirs,
                    extra_expose_files=extra_expose_files,
                )
        else:
            delegated = _delegate_to_kiro_internal_sandbox(
                argv,
                sandbox_level,
                strip_python_env=strip_python_env,
                forward_ssh_auth_sock=forward_ssh_auth_sock,
            )
            if delegated is not None:
                return delegated
            if sys.platform == "darwin":
                # Preserve macOS's audit-failure fallback: once delegation is
                # refused, Kiro Crew's own seatbelt remains the safe owner.
                return sandbox_exec_argv(
                    argv,
                    sandbox_level,
                    strip_python_env=strip_python_env,
                    forward_ssh_auth_sock=forward_ssh_auth_sock,
                )

    backend = detect_backend(config_mode=mode)

    if backend == "namespace":
        if (
            extra_hidden_dirs
            or extra_visible_dirs
            or extra_private_dirs
            or extra_writable_dirs
            or extra_expose_files
        ):
            wrapped = namespace_argv(
                argv,
                sandbox_level,
                strip_python_env=strip_python_env,
                forward_ssh_auth_sock=forward_ssh_auth_sock,
                extra_hidden_dirs=extra_hidden_dirs,
                extra_hidden_dir_ids=extra_hidden_dir_ids,
                extra_alias_credential_ids=extra_alias_credential_ids,
                extra_visible_dirs=extra_visible_dirs,
                extra_private_dirs=extra_private_dirs,
                extra_private_dir_ids=extra_private_dir_ids,
                extra_writable_dirs=extra_writable_dirs,
                extra_expose_files=extra_expose_files,
            )
        else:
            wrapped = namespace_argv(
                argv,
                sandbox_level,
                strip_python_env=strip_python_env,
                forward_ssh_auth_sock=forward_ssh_auth_sock,
            )
        # Caller deletes the generated launcher script. Its position is
        # ``1 + len(flags)``, NOT a hardcoded 1: the interpreter flags sit between
        # the executable and the script, so hardcoding leaks the tempfile (and
        # hands the caller a flag to unlink) the moment that list changes.
        return wrapped, _launcher_script_of(wrapped)
    if backend == "sandbox-exec":
        if (
            extra_hidden_dirs
            or extra_hidden_dir_ids
            or extra_visible_dirs
            or extra_private_dirs
            or extra_writable_dirs
            or extra_expose_files
        ):
            return sandbox_exec_argv(
                argv,
                sandbox_level,
                strip_python_env=strip_python_env,
                forward_ssh_auth_sock=forward_ssh_auth_sock,
                extra_hidden_dirs=extra_hidden_dirs,
                extra_hidden_dir_ids=extra_hidden_dir_ids,
                extra_visible_dirs=extra_visible_dirs,
                extra_private_dirs=extra_private_dirs,
                extra_private_dir_ids=extra_private_dir_ids,
                extra_writable_dirs=extra_writable_dirs,
                extra_expose_files=extra_expose_files,
            )
        return sandbox_exec_argv(
            argv,
            sandbox_level,
            strip_python_env=strip_python_env,
            forward_ssh_auth_sock=forward_ssh_auth_sock,
        )

    if backend == "none":
        # Reaching here while the kernel says this process IS sandboxed means the
        # outer sandbox is NOT one KiroCrew built: a KiroCrew-built one carries
        # KIROCREW_SANDBOX_ACTIVE and was already passed through above. The
        # remaining nested case is a foreign confiner — kiro-cli's own internal
        # seatbelt, or an operator-wrapped gateway — whose profile macOS gives us
        # no supported way to identify, and whose environment our scrub never
        # touched. We therefore do NOT pass through. What we DO fix is the
        # diagnosis: the probe's EPERM is a nesting artifact, not a host verdict,
        # so the error must not claim this host lacks a sandbox backend.
        #
        # FAIL-CLOSED: refuse to execute without sandbox unless explicitly opted in.
        # Returning unmodified argv would let the agent subprocess reach every
        # credential path with no OS-level isolation.
        #
        # ONE read of the gate: the branch below, the warning on the permitted path
        # and the message that explains a refusal must describe the same state, and
        # a concurrent config reload must not let them disagree about the same
        # spawn. ``granted_by`` is DERIVED from that one verdict rather than
        # resolved again, so it cannot contradict it.
        opted_in = _allow_unsandboxed_exec()
        granted_by = _unsandboxed_grant_source(opted_in)
        # A governance ``sandbox.min_level`` floor OVERRIDES the config opt-in.
        # The floor must not do the opposite of what pinning it implies:
        # disabling the audited first-party carve-out below while leaving this
        # broad opt-in untouched would leave a governed fleet without the
        # constrained path and with the unconstrained one.  ``config.json`` is not
        # policy — the floor is — so the flag cannot re-open this on a governed
        # host.  Derived from the ONE floor read taken at the top of this call,
        # and via ``_floor_mandates_sandbox`` rather than raw truthiness, because
        # a pinned floor of the loosest tier requires nothing and must not deny.
        floor_mandates_sandbox = _floor_mandates_sandbox(governance_floor)
        if floor_mandates_sandbox or not opted_in:
            # ONE read of the pair: a concurrent re-probe swaps the whole tuple,
            # so failure and remedy can never come from different probes.
            transient, probe_reason, probe_remedy = _last_unshare_failure or (
                False,
                "no probe detail recorded",
                "",
            )
            # First-party carve-out: a spawn whose full argv is
            # derived inside this package (never agent/repo/user-config text)
            # may proceed unconfined on a host that GENUINELY has no backend.
            # All three preconditions, structurally:
            #   * the caller vouched via ``first_party_fixed_argv`` — a reviewed
            #     property, ratcheted by test_spawn_audit.py::FIRST_PARTY_SPAWNS;
            #   * the unavailability class is ``no_backend``: a ``transient``
            #     failure still raises (it self-heals on the next spawn and must
            #     not buy a bypass) and ``foreign_sandbox`` still raises (the
            #     host's sandbox is fine; the remedy is config, not bypass);
            #   * no governance ``sandbox.min_level`` floor is active — reuses
            #     the ONE floor read taken at the top of this call (the same
            #     value the clamp used), so no second profile walk runs and the
            #     two checks cannot disagree; a governed host keeps fail-closing
            #     for first-party spawns too.
            if (
                first_party_fixed_argv
                and _classify_unavailable(transient) == "no_backend"
                and not governance_floor
            ):
                return _first_party_no_backend_passthrough(argv, sandbox_level, strip_python_env)
            if transient:
                # The mechanism follows the retry advice rather than leading it: the
                # cap case is permanently reported transient, so withholding it here
                # would leave doctor and the logs unable to name the one sysctl that
                # fixes the host, while leading with it would read as "reconfigure"
                # to someone whose host is merely busy.
                guidance = (
                    "This probe failure looks TRANSIENT (momentary resource "
                    "pressure) — it is not cached and the next spawn re-probes "
                    "automatically. Do NOT disable the sandbox for this; retry "
                    "instead. "
                ) + _linux_remedy_guidance(probe_remedy)
            elif _inside_macos_sandbox():
                # Nesting under a FOREIGN sandbox — say so, and point at the
                # config-level fix that hands isolation back to KiroCrew's own
                # profile, not at sandbox_allow_unsandboxed_exec (which would
                # disable isolation everywhere to fix a case where a sandbox
                # demonstrably exists).
                guidance = (
                    "This host's sandbox is NOT broken: the kernel reports this "
                    "process is already inside a macOS Seatbelt sandbox that "
                    "KiroCrew did not create, and Seatbelt cannot nest, so "
                    "sandbox-exec fails with EPERM. Spawns under KiroCrew's OWN "
                    "sandbox are unaffected — they carry an isolation marker and "
                    "pass through. The usual cause is kiro-cli's internal "
                    'sandbox: set {"sandbox": false} in '
                    "~/.kiro/settings/amazon-internal.json so KiroCrew's own "
                    "profile owns isolation (that profile is the one that hides "
                    "the credential directories, so this keeps isolation rather "
                    "than weakening it), then restart the gateway. Other outer "
                    "sandboxes (e.g. an operator-wrapped gateway) hit the same "
                    "nesting limit — see docs/system-specs/modules/security.md "
                    '("macOS marker site and the kernel cross-check"). '
                )
            elif is_docker_container():
                # Inside a Docker/OCI container the runtime's seccomp or
                # AppArmor policy blocked a step of the namespace handshake.
                # This is a container-policy restriction, NOT a kernel-level
                # limitation on the host — the correct fix is at the container
                # level, not disabling the sandbox everywhere.
                #
                # WHICH step decides the advice. A refused unshare is seccomp
                # (Docker's default profile gates it on CAP_SYS_ADMIN) and the
                # shipped seccomp profile fixes it. A refused propagation MOUNT
                # after both unshares succeeded is the runtime's default
                # AppArmor profile (`deny mount`) — the Kubernetes default on an
                # AppArmor node, where no seccomp profile is applied at all — and
                # a seccomp profile cannot fix that, so prescribing one would
                # send the operator through a change that leaves the probe
                # failing exactly as before.
                if probe_remedy == REMEDY_MOUNT_DENIED:
                    guidance = (
                        "Running inside a Docker/OCI container whose runtime "
                        "policy permits user namespaces but refuses mount(2) "
                        f"inside them (probe: {probe_reason}). This is the "
                        "runtime's default AppArmor profile (`deny mount`) or a "
                        "seccomp filter, not a host kernel limitation, and it "
                        "needs no root or CAP_SYS_ADMIN to fix. Choose one of:\n"
                        "  (a) Run the container with AppArmor unconfined, and a "
                        "seccomp profile that permits unshare and mount:\n"
                        "        docker run --security-opt apparmor=unconfined "
                        "--security-opt seccomp=kirocrew-seccomp.json ...\n"
                        "        # Kubernetes 1.30+: securityContext.appArmorProfile: "
                        "{type: Unconfined}\n"
                        "        # earlier Kubernetes: annotation "
                        "container.apparmor.security.beta.kubernetes.io/<container>: "
                        "unconfined\n"
                        "  (b) Restart with explicit unsandboxed consent "
                        "(the container is then the only isolation boundary):\n"
                        "        docker run -e KIROCREW_ALLOW_UNSANDBOXED=1 ...\n"
                        "  (c) Manually set agent.sandbox_allow_unsandboxed_exec=true "
                        "in ~/.kiro/crew/config.json inside the container.\n"
                        "See docs/guides/docker.md for the full sandbox troubleshooting guide."
                    )
                else:
                    guidance = (
                        "Running inside a Docker/OCI container where the runtime's "
                        "seccomp or AppArmor policy blocks user namespace creation "
                        f"(probe: {probe_reason}). "
                        "This is a container policy restriction, not a host kernel "
                        "limitation. To resolve, choose one of:\n"
                        "  (a) Use the Kiro Crew custom seccomp profile (adds "
                        "unconditional unshare/clone/mount allows to the Docker "
                        "default — less permissive than seccomp=unconfined):\n"
                        "        # With a repo checkout:\n"
                        "        docker run --security-opt "
                        "seccomp=docker/seccomp/kirocrew-seccomp.json ...\n"
                        "        # Without a checkout (image-only):\n"
                        "        curl -fsSL https://raw.githubusercontent.com/"
                        "kirodotdev/KiroCrew/main/docker/seccomp/kirocrew-seccomp.json"
                        " -o kirocrew-seccomp.json\n"
                        "        docker run --security-opt seccomp=kirocrew-seccomp.json ...\n"
                        "  (b) Restart with explicit unsandboxed consent "
                        "(the container is then the only isolation boundary):\n"
                        "        docker run -e KIROCREW_ALLOW_UNSANDBOXED=1 ...\n"
                        "  (c) Manually set agent.sandbox_allow_unsandboxed_exec=true "
                        "in ~/.kiro/crew/config.json inside the container.\n"
                        "See docs/guides/docker.md for the full sandbox troubleshooting guide."
                    )
            else:
                guidance = _no_backend_guidance()
            # When the policy floor is what refused, every guidance above points
            # at the wrong lever: the operator HAS set the opt-in and the flag is
            # deliberately powerless here, so naming it would send them down a
            # dead end.  Replace the remedy rather than appending to it.
            policy_overrode_opt_in = floor_mandates_sandbox and opted_in
            if policy_overrode_opt_in:
                guidance = (
                    "This host is GOVERNED: an enterprise policy pins "
                    f"sandbox.min_level={governance_floor!r}, which forbids "
                    "unsandboxed execution regardless of "
                    "agent.sandbox_allow_unsandboxed_exec — that flag is set on "
                    "this host and is deliberately powerless against the policy, "
                    "so editing config.json cannot resolve this. A governed host "
                    "also withholds the first-party carve-out, so Kiro Crew's own "
                    "built-in spawns are refused here too: this host runs no "
                    "agent subprocess until it has a working sandbox backend "
                    "(see docs/system-specs/modules/security.md) or the policy "
                    "owner relaxes sandbox.min_level."
                )
                sel_reason = (
                    "No sandbox backend available and a governance "
                    f"sandbox.min_level={governance_floor!r} floor forbids "
                    "unsandboxed exec (the config opt-in is set but overridden)"
                )
                refusal = (
                    "Sandbox backend unavailable and a governance policy forbids "
                    "unsandboxed execution. "
                )
            elif _unsandboxed_exec_key_declared():
                # The operator DID decide, and decided to keep this host
                # fail-closed. Telling them the flag "is not set" would send them
                # to set a key they already set, so name their own decision as
                # the thing to revisit. Diagnostic-only, so reading it here rather
                # than alongside the gate costs at worst a stale WORDING under a
                # concurrent config reload, never a wrong allow/deny.
                sel_reason = (
                    "No sandbox backend available and allow_unsandboxed_exec is declared false"
                )
                refusal = (
                    "Sandbox backend unavailable and agent.sandbox_allow_unsandboxed_exec "
                    "is set to false on this host, so unsandboxed execution stays "
                    "refused. Change that key to true (or remove it to accept this "
                    "platform's default) if you want these spawns to run unconfined. "
                )
            else:
                sel_reason = "No sandbox backend available and allow_unsandboxed_exec is not set"
                refusal = "Sandbox backend unavailable and allow_unsandboxed_exec is not set. "
            # Emit SEL audit event for this security-relevant denial so it
            # appears in the tamper-evident audit log (security-review requirement).
            try:
                from kiro_crew.sel import sel  # circular import: sandbox is low-level

                sel().log_tool_invocation(
                    session_key="sandbox",
                    agent="system",
                    source="sandbox.wrap_argv",
                    tool_name=_command_log_label(argv),
                    tool_kind="subprocess",
                    outcome="denied",
                    error=(f"{sel_reason} (probe: {probe_reason})"),
                )
            except Exception:
                logger.warning("Failed to emit SEL audit event for sandbox denial", exc_info=True)
            raise SandboxUnavailableError(
                refusal + "No OS-level sandbox backend is available on this host, and the "
                "agent subprocess cannot be safely isolated. "
                f"Probe detail: {probe_reason}. " + guidance,
                kind=_classify_unavailable(transient),
                detail=probe_reason,
                # A transient verdict is never cached, so the host is free to
                # recover on the next call — but it can still name a mechanism.
                # `user.max_user_namespaces` exhaustion surfaces as ENOSPC, which
                # is indistinguishable from momentary fd/disk pressure, so a
                # configured cap of 0 is permanently reported as transient. Withholding
                # the remedy there leaves the one host this token exists for with
                # no way out; the steps are framed as "if this keeps happening" so
                # they never read as advice to reconfigure a merely busy host.
                remedy=probe_remedy,
            )
        # Permitted: audit, warn (or info), and return unmodified argv.
        #
        # The audit is not optional now that a PLATFORM DEFAULT can reach here.
        # Before, everything on this path had an operator declaration behind it
        # and the config file was itself the record; a spawn permitted because of
        # the platform has no such record, so without this event an unconfined
        # spawn would leave no trace at all. ``outcome="unconfined"`` matches the
        # first-party carve-out's third outcome — neither ``denied`` (nothing was
        # refused) nor ``allowed`` (nothing confines this spawn) — and
        # ``resources`` names WHICH permission applied, so the log distinguishes
        # an accepted risk from a default.
        #
        # Best-effort, like the carve-out and unlike the fail-closed ``denied``
        # audit: this fires for every spawn on a backend-less host, and draining
        # SEL synchronously on each one would put a flush on the gateway event
        # loop. Denying the spawn on an audit hiccup would also brick every MCP
        # server and app backend on Windows, which is the platform this path
        # exists to serve.
        try:
            from kiro_crew.sel import sel  # circular import: sandbox is low-level

            sel().log_tool_invocation(
                session_key="sandbox",
                agent="system",
                source="sandbox.wrap_argv",
                tool_name=_command_log_label(argv),
                tool_kind="subprocess",
                outcome="unconfined",
                resources=(
                    "operator declared agent.sandbox_allow_unsandboxed_exec=true"
                    if granted_by == UNSANDBOXED_BY_OPERATOR
                    else "platform default for a host with no sandbox backend"
                ),
            )
        except Exception:
            logger.warning(
                "SEL audit failed for an unconfined spawn (%s permission) — "
                "proceeding unaudited rather than bricking every agent "
                "subprocess on a host that has no sandbox backend to fall back "
                "on. Command: %s",
                granted_by,
                _command_log_label(argv),
                exc_info=True,
            )
        _warn_no_isolation(mode, granted_by)
    return argv, None


async def wrap_argv_async(
    argv: list[str],
    mode: str = "auto",
    *,
    strip_python_env: bool = False,
    forward_ssh_auth_sock: bool = False,
    extra_hidden_dirs: tuple[str, ...] = (),
    extra_visible_dirs: tuple[str, ...] = (),
    extra_private_dirs: tuple[str, ...] = (),
    extra_writable_dirs: tuple[str, ...] = (),
    extra_expose_files: tuple[str, ...] = (),
    is_kiro_cli: bool | None = None,
    first_party_fixed_argv: bool = False,
    _prepare: Callable[..., tuple[list[str], str | None]] | None = None,
) -> tuple[list[str], str | None]:
    """Cancellation-safe, off-loop sandbox preparation for async spawn paths.

    Sandbox construction probes the host and creates a launcher/profile. It also
    resolves the protected voice-runtime paths on the first call. None of that
    filesystem work may run on a gateway event loop. If the caller is cancelled
    while the worker is finishing, settle it and remove any newly-created
    launcher/profile before propagating cancellation. ``_prepare`` preserves
    each caller's module-local test seam; production callers pass their imported
    :func:`wrap_argv`, and the default is this module's implementation.
    """
    options: dict[str, Any] = {"mode": mode}
    if strip_python_env:
        options["strip_python_env"] = True
    if forward_ssh_auth_sock:
        options["forward_ssh_auth_sock"] = True
    if extra_hidden_dirs:
        options["extra_hidden_dirs"] = extra_hidden_dirs
    if extra_visible_dirs:
        options["extra_visible_dirs"] = extra_visible_dirs
    if extra_private_dirs:
        options["extra_private_dirs"] = extra_private_dirs
    if extra_writable_dirs:
        options["extra_writable_dirs"] = extra_writable_dirs
    if extra_expose_files:
        options["extra_expose_files"] = extra_expose_files
    if is_kiro_cli is not None:
        options["is_kiro_cli"] = is_kiro_cli
    if first_party_fixed_argv:
        options["first_party_fixed_argv"] = True
    prepare = functools.partial(wrap_argv if _prepare is None else _prepare, argv, **options)

    def _prepare_wrapped() -> tuple[list[str], dict[str, str], str | None]:
        wrapped, cleanup = prepare()
        return wrapped, {}, cleanup

    wrapped, _unused_env, cleanup = await shielded_prepare_off_loop(_prepare_wrapped)
    return wrapped, cleanup


# Environment keys always scrubbed from an agent-influenced subprocess'
# environment, regardless of sandbox backend. These are the credential-bearing
# names that must never reach a spawn whose command, arguments, or working
# directory the agent (or a hostile MCP-config / repo) can influence. The OS
# sandbox launcher already drops these when a backend is present (see
# ``env_prefixes`` in ``namespace_argv`` / ``sandbox_exec_argv``), but scrubbing
# at the parent level too means the guarantee holds even on the opted-in
# ``sandbox_allow_unsandboxed_exec`` fail-open path where no launcher runs.
# Prefix match via ``startswith`` (mirrors the launcher's env_prefixes check).
_SPAWN_SCRUB_ENV_PREFIXES: list[str] = list(_SENSITIVE_ENV_PREFIXES) + list(_AGENT_DENIED_ENV_KEYS)


def scrub_env(
    env: dict[str, str] | None = None,
    *,
    extra_prefixes: list[str] | None = None,
) -> dict[str, str]:
    """Return a copy of *env* (default ``os.environ``) with credential-bearing
    keys removed.

    Drops every key whose name starts with one of ``_SPAWN_SCRUB_ENV_PREFIXES``
    (AWS secret/session vars, SSH_AUTH_SOCK, GNUPGHOME, GIT_ASKPASS, and the
    Slack/owner tokens seeded into ``os.environ`` for trusted children). Used to
    build the environment for agent-influenced spawns so a spawned process
    cannot read secrets straight out of the inherited environment.

    *extra_prefixes* adds more name prefixes to drop (e.g.
    ``_PYTHON_ENV_PREFIXES`` when the spawn is a foreign Python child).
    """
    prefixes = _SPAWN_SCRUB_ENV_PREFIXES + (extra_prefixes or [])
    src = os.environ if env is None else env
    return {k: v for k, v in src.items() if not any(k.startswith(p) for p in prefixes)}


def agent_env_scrub_prefixes() -> tuple[str, ...]:
    """Name prefixes :func:`scrub_agent_subprocess_env` drops from an agent child.

    DERIVED from the two lists that scrub actually composes, so a caller checking
    "would this variable survive the scrub?" cannot drift from the scrub itself.
    That question has one caller today: the ``agent.deepseek_env`` validator in
    ``acp/client.py`` refuses to inject a name the scrub would strip, because the
    injection happens BEFORE the scrub and the operator would otherwise get a
    harness with no provider key and no error naming why.

    Deliberately NOT the whole truth about a given spawn: ``forward_ssh_auth_sock``
    re-admits ``SSH_AUTH_SOCK`` for an opted-in agent child, so a name matching
    that prefix is reported as scrubbed here even though one spawn shape keeps it.
    The caller's use is a refusal, so answering conservatively refuses a name that
    might have worked rather than accepting one that silently would not.
    """
    return tuple(_SPAWN_SCRUB_ENV_PREFIXES) + tuple(_PYTHON_ENV_PREFIXES)


def scrub_agent_denied_env(env: dict[str, str]) -> dict[str, str]:
    """Return a copy of *env* with gateway-owned channel credentials removed.

    Drops every key matching ``_AGENT_DENIED_ENV_KEYS`` — the Slack/WeCom/
    Telegram tokens and owner id that ``config/loader.load_credentials()`` seeds
    into ``os.environ`` for trusted children only.

    This is the PARENT-level complement to the OS-sandbox launcher scrub. The
    launcher (``namespace_argv`` / ``sandbox_exec_argv``) only strips these keys
    for the ``cc``/``strict`` tiers; on the default ``auto``/``standard`` tier
    they are left in place. The production ACP spawn paths
    (:meth:`AcpRuntime._spawn` / :meth:`AcpClient._spawn`) copy a raw
    ``os.environ`` and call :func:`wrap_argv` directly (not
    :func:`sandboxed_spawn_argv`), so without this scrub the channel credentials
    would be inherited by the agent subprocess on the default tier — reachable
    via ``env`` / ``os.environ`` and usable to control those channel identities
    outside KiroCrew.

    Unlike :func:`scrub_env`, this deliberately does NOT strip
    ``_SENSITIVE_ENV_PREFIXES`` (AWS/SSH/GPG): the ``standard`` sandbox is
    designed to leave git-over-SSH, the AWS CLI and kubectl usable, so those
    vars must survive the parent scrub. Prefix match via ``startswith`` mirrors
    the launcher's env_prefixes check.
    """
    return {
        k: v for k, v in env.items() if not any(k.startswith(p) for p in _AGENT_DENIED_ENV_KEYS)
    }


def scrub_agent_subprocess_env(
    env: dict[str, str] | None = None, *, forward_ssh_auth_sock: bool = False
) -> dict[str, str]:
    """Return the full environment scrub required for a Kiro/ACP child.

    This is the parent-side equivalent of the OS launchers' sensitive-variable
    removal plus ``strip_python_env=True``. It is mandatory for Windows Kiro
    delegation because Windows cannot express the POSIX ``env -u`` prefix, and
    keeping it on every platform makes delegated and wrapped ACP spawns inherit
    the same environment policy.

    This is the AGENT enforcement point, so the SSH_AUTH_SOCK
    forward opt-in is applied HERE rather than in the generic :func:`scrub_env`,
    which also serves non-agent callers (tailscale host children,
    ``sandboxed_spawn_argv`` spawns) that must keep the socket scrubbed. The
    decision is passed in as an already-resolved boolean (the caller resolves
    :func:`_forward_ssh_auth_sock` once in its off-loop environment-prep hop, so
    no synchronous config read runs on the asyncio event loop here); it defaults
    False, so a caller that does not opt in scrubs the socket as before. When
    set, the socket value is preserved across the scrub for the agent child
    alone.
    """
    scrubbed = scrub_env(env, extra_prefixes=_PYTHON_ENV_PREFIXES)
    src = os.environ if env is None else env
    if forward_ssh_auth_sock and "SSH_AUTH_SOCK" in src:
        # scrub_env removed it unconditionally; re-add the socket for the agent
        # child only. Restricted to the exact key, so nothing else the generic
        # scrub dropped is reintroduced.
        scrubbed["SSH_AUTH_SOCK"] = src["SSH_AUTH_SOCK"]
    return scrubbed


def sandboxed_spawn_argv(
    argv: list[str],
    mode: str = "standard",
    *,
    env: dict[str, str] | None = None,
    strip_python_env: bool = False,
    extra_hidden_dirs: tuple[str, ...] = (),
    extra_visible_dirs: tuple[str, ...] = (),
    extra_private_dirs: tuple[str, ...] = (),
    extra_writable_dirs: tuple[str, ...] = (),
    first_party_fixed_argv: bool = False,
    is_kiro_cli: bool | None = None,
) -> tuple[list[str], dict[str, str], str | None]:
    """Single chokepoint for agent-influenced subprocess spawns.

    Wraps *argv* with the OS-level sandbox (:func:`wrap_argv`) AND returns a
    credential-scrubbed environment (:func:`scrub_env`), so every caller gets
    both the filesystem-isolation and the environment-hiding layer without
    having to remember to apply each separately. This is the wrapper the
    subprocess-spawn audit test (``test/test_spawn_audit.py``) requires every
    agent-influenced spawn in ``src/kiro_crew`` to route through.

    Args:
        argv: Original command + args.
        mode: Sandbox mode passed to :func:`wrap_argv` (default ``"standard"``:
            hides non-workflow credential dirs while leaving git-over-SSH and
            the AWS CLI usable).
        env: Base environment to scrub (default ``os.environ``). Pass a
            pre-augmented env (e.g. with a resolved ``PATH``) to have the scrub
            applied on top of it.
        strip_python_env: Strip ``PYTHONPATH``/``PYTHONHOME`` so a foreign
            Python child does not inherit KiroCrew's interpreter paths. Applied
            BOTH inside :func:`wrap_argv`'s launcher AND to the returned env, so
            the strip holds even on the fail-open path where no launcher runs.
        extra_hidden_dirs: Additional absolute directory trees the caller needs
            hidden in both the macOS Seatbelt and Linux namespace profiles.
        extra_visible_dirs: Trusted paths that must remain visible when an
            otherwise-hidden parent contains them (the whole parent's mask is lifted).
        extra_private_dirs: The spawn's OWN directories inside a hidden tree
            (its ``agent_scratch`` dir under the masked scratch root). Re-exposed
            read-write as a window; the parent's mask and every sibling stay hidden.
        extra_writable_dirs: Self-derived scratch directories inside the sealed
            runtime parent that the child must be able to write — see
            :func:`wrap_argv`. Validated; refused candidates degrade to the
            sealed behavior rather than blocking the spawn.
        is_kiro_cli: Threaded to :func:`wrap_argv`. Whether the child carries
            its own internal sandbox, which cannot nest inside Crew's. Exposed
            here so a DELEGATING spawn can route through this chokepoint instead
            of calling :func:`wrap_argv` directly: without it, such a caller had
            to choose between the chokepoint (and silently lose the delegation
            decision, asking for a tier the child cannot honour) and its own
            hand-rolled wrap+scrub+scope (and drift from the contract every other
            spawn gets). ``None`` keeps ``wrap_argv``'s own classification.
        first_party_fixed_argv: Threaded to :func:`wrap_argv`. True ONLY for
            spawns whose full argv is derived inside this package with zero
            agent/repo/user-config influence; every passing site must be
            allowlisted in ``test/test_spawn_audit.py::FIRST_PARTY_SPAWNS``.
            See :func:`wrap_argv` for the no-backend carve-out it gates.

    Returns:
        ``(wrapped_argv, scrubbed_env, cleanup_path_or_None)``. The caller MUST
        pass *scrubbed_env* as the subprocess ``env=`` and unlink *cleanup_path*
        (a temp launcher/profile) after the child exits.
    """
    if extra_hidden_dirs or extra_visible_dirs or extra_private_dirs or extra_writable_dirs:
        wrapped, cleanup = wrap_argv(
            argv,
            mode=mode,
            strip_python_env=strip_python_env,
            extra_hidden_dirs=extra_hidden_dirs,
            extra_visible_dirs=extra_visible_dirs,
            extra_private_dirs=extra_private_dirs,
            extra_writable_dirs=extra_writable_dirs,
            first_party_fixed_argv=first_party_fixed_argv,
            is_kiro_cli=is_kiro_cli,
        )
    else:
        wrapped, cleanup = wrap_argv(
            argv,
            mode=mode,
            strip_python_env=strip_python_env,
            first_party_fixed_argv=first_party_fixed_argv,
            is_kiro_cli=is_kiro_cli,
        )
    # ``wrap_argv`` only strips PYTHONPATH/PYTHONHOME inside the launcher script,
    # so on the fail-open path (no sandbox backend, opted-in unsandboxed exec) it
    # returns argv unmodified and the strip never happens. Apply the same strip
    # to the scrubbed env here so ``strip_python_env=True`` holds regardless of
    # whether a backend is available.
    extra = _PYTHON_ENV_PREFIXES if strip_python_env else None
    scrubbed = scrub_env(env, extra_prefixes=extra)
    # The cgroup wrapper prepended below needs the user session bus in the
    # environment it is spawned with, so restore its locator vars after the
    # scrub. Callers that pass a strict allowlist env (e.g. the source-provider
    # CLI) otherwise hand systemd-run an environment it cannot reach the bus
    # from and the spawn dies before exec'ing the real command.
    patched, injected = cgroup_scope_bus_env(scrubbed)
    if injected:
        # The locators are the WRAPPER's capability, never the child's: a bus
        # address in the sandboxed process is a sandbox escape (it can ask the
        # user systemd manager to start a unit that runs outside the namespace).
        # So they live only until systemd-run has used them — an ``env -u`` shim
        # inside the scope drops them again before the real command execs. If no
        # ``env`` binary is available we fail CLOSED: keep the scrubbed env, let
        # the wrapper fail loudly, and never hand the child a live bus.
        unset = _unset_env_argv(injected)
        if unset is None:
            logger.warning(
                "SECURITY: no `env` binary to drop %s inside the cgroup scope; "
                "not forwarding the bus locators (systemd-run will fail rather "
                "than leak a user-bus address into the sandboxed child).",
                ", ".join(injected),
            )
        else:
            scrubbed = patched
            wrapped = [*unset, *wrapped]
    # cgroup v2 scope (OUTERMOST layer): bound the spawned process tree with
    # pids.max + memory.max. Applied here so every sandboxed_spawn_argv caller
    # gets the fork-bomb / memory-DoS ceiling without threading it through each
    # site. No-op (with a one-time loud warning) where cgroup delegation is
    # unavailable. Safe re: the cleanup path — that is returned separately, not
    # re-derived from an argv index, so prepending systemd-run does not disturb
    # it. See docs/architecture/resource-protection.md.
    wrapped = cgroup_scope_argv(wrapped)
    # Positive-identity marker for the orphan sweep: every tree spawned through
    # this chokepoint (and its descendants, via env inheritance) is identifiable
    # as KiroCrew-spawned even when its cmdline carries no KiroCrew fingerprint
    # (e.g. ``npx @playwright/mcp``).
    scrubbed[KIROCREW_SPAWNED_ENV] = KIROCREW_SPAWNED_VALUE
    # Marks this tree as TOOL work -- a build, an ``npx`` install, a ``git``/``gh``
    # read, a provisioning run. The runtime reconciler reads it back from the kernel's
    # exec-time copy to leave such a tree out of its kill-candidate population: a tool
    # subprocess lands in the agent slice that reconciler compares against, carries the
    # inherited KIROCREW_SPAWNED marker, and is in no membership record, so once it
    # outlives the age floor the argv0 basename test is the only thing between it and a
    # signal. This marker is exec-time evidence instead: a same-uid process can set its
    # own argv or write any file, and cannot alter a running process's environment.
    #
    # It describes the TREE, not each process in it, and this function is not limited
    # to non-harness argv: ``is_kiro_cli`` exists precisely so a DELEGATING spawn can
    # route here, and callers do (a pod child probe, an unattended fix-authoring
    # agent). Since the marker is inherited, a harness can carry it without being tool
    # work, so the reconciler's exclusion requires this marker AND a non-harness argv0
    # -- see ``runtime_reconcile.RuntimeReconciler._unowned``. Stamping it here is
    # therefore safe for any argv: it never decides an exclusion on its own.
    scrubbed[KIROCREW_SANDBOX_TOOL_ENV] = KIROCREW_SANDBOX_TOOL_VALUE
    return wrapped, scrubbed, cleanup


async def shielded_prepare_off_loop(
    prepare: Callable[[], tuple[list[str], dict[str, str], str | None]],
    *,
    executor: ThreadPoolExecutor | None = None,
) -> tuple[list[str], dict[str, str], str | None]:
    """Run a spawn-preparation callable off the loop, shielded from cancellation.

    ``prepare`` must follow the :func:`sandboxed_spawn_argv` contract: it returns
    ``(wrapped_argv, scrubbed_env, cleanup_path_or_None)`` where the third element
    is a temp launcher/profile the CALLER must unlink after the child exits.

    Every async caller reaches the sync chokepoint through a worker hop.
    Cancelling that hop abandons the returned tuple while the worker still
    materializes the launcher/profile, leaking the temp file forever.  Shielding
    the hop keeps the worker's result recoverable: on cancellation we wait for
    the thread to settle, unlink the launcher it created, and re-raise.

    A REPEAT cancellation landing on a bare recovery ``await`` is a
    ``BaseException`` that would abandon the recovery before the unlink runs,
    leaking the materialized launcher.  The settle-then-unlink therefore
    runs as its own task, shielded from cancellations aimed at this caller; each
    absorbed repeat is ``uncancel()``-ed so an enclosing ``asyncio.timeout``
    still reports ``TimeoutError``, and the ORIGINAL cancellation is re-raised
    once the launcher is gone.

    ``executor`` keeps pool choice with the CALLER, because which pool absorbs a
    wedged preparation is per-site policy, not a shield concern: the chokepoint
    can cold-probe the sandbox backend with a synchronous subprocess, and
    :mod:`kiro_crew.executors` partitions blocking work into named pools so such
    a probe cannot occupy the workers another subsystem (the orphan-reaping
    sweep) needs.  Defaulting it to ``None`` — the loop's default pool, via
    ``asyncio.to_thread`` — would silently collapse that partition for callers
    that had chosen a pool, so every site that had one passes it explicitly.
    """

    Prepared = tuple[list[str], dict[str, str], str | None]
    task: asyncio.Future[Prepared]
    if executor is None:
        task = asyncio.ensure_future(asyncio.to_thread(prepare))
    else:
        task = asyncio.get_running_loop().run_in_executor(executor, prepare)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:

        async def _settle_then_unlink() -> None:
            cleanup: str | None = None
            with contextlib.suppress(Exception, asyncio.CancelledError):
                _, _, cleanup = await task
            if not cleanup:
                return
            target = cleanup

            def _unlink() -> None:
                with contextlib.suppress(OSError):
                    os.unlink(target)

            if executor is None:
                await asyncio.to_thread(_unlink)
            else:
                await asyncio.get_running_loop().run_in_executor(executor, _unlink)

        current = asyncio.current_task()
        recovery = asyncio.create_task(_settle_then_unlink())
        while not recovery.done():
            try:
                await asyncio.shield(recovery)
            except asyncio.CancelledError:
                uncancel = getattr(current, "uncancel", None)  # 3.11+
                if uncancel is not None:
                    uncancel()
            except Exception:
                logger.warning(
                    "sandbox launcher cleanup failed after cancellation",
                    exc_info=True,
                )
        raise


async def sandboxed_spawn_argv_async(
    argv: list[str],
    mode: str | None = None,
    *,
    env: dict[str, str] | None = None,
    strip_python_env: bool = False,
    extra_hidden_dirs: tuple[str, ...] = (),
    extra_visible_dirs: tuple[str, ...] = (),
    extra_private_dirs: tuple[str, ...] = (),
    extra_writable_dirs: tuple[str, ...] = (),
    first_party_fixed_argv: bool = False,
    executor: ThreadPoolExecutor | None = None,
    _prepare: Callable[..., tuple[list[str], dict[str, str], str | None]] | None = None,
) -> tuple[list[str], dict[str, str], str | None]:
    """Prepare a sandboxed spawn safely off-loop, retaining caller test seams."""
    # Preserve the long-standing injectable preparation seam: many focused
    # callers replace ``sandboxed_spawn_argv`` with a narrow ``(argv, *, env)``
    # test double. ``None`` means the caller omitted the argument, in which case
    # the synchronous function supplies its own ``standard`` default. An
    # explicitly supplied value -- including ``standard`` -- is forwarded.
    options: dict[str, Any] = {}
    if mode is not None:
        options["mode"] = mode
    if env is not None:
        options["env"] = env
    if strip_python_env:
        options["strip_python_env"] = True
    if extra_hidden_dirs:
        options["extra_hidden_dirs"] = extra_hidden_dirs
    if extra_visible_dirs:
        options["extra_visible_dirs"] = extra_visible_dirs
    if extra_private_dirs:
        options["extra_private_dirs"] = extra_private_dirs
    if extra_writable_dirs:
        options["extra_writable_dirs"] = extra_writable_dirs
    if first_party_fixed_argv:
        options["first_party_fixed_argv"] = True
    return await shielded_prepare_off_loop(
        functools.partial(
            sandboxed_spawn_argv if _prepare is None else _prepare,
            argv,
            **options,
        ),
        executor=executor,
    )


# ── cgroup v2 scope enforcement (fork bomb + memory DoS) ──
# The RLIMIT preexec (resource_limit_preexec) caps a SINGLE process's FDs, but
# RLIMIT is the wrong tool for the headline threats: RLIMIT_NPROC is
# per-real-UID (not per-spawn-subtree) and RLIMIT_AS caps virtual not resident
# memory. cgroup v2 pids.max / memory.max are the correct per-cgroup ceilings —
# they bound the agent + all its MCP-server/tool descendants as one unit, and
# the kernel enforces at fork()/alloc time (no reaper race). We place each
# agent-influenced spawn in a transient systemd --user --scope, which works
# UNPRIVILEGED when the user session has cgroup v2 delegation (pids + memory
# controllers). See docs/architecture/resource-protection.md.

# Default cgroup ceilings (per agent scope). Overridable via the same
# ``resource_limits`` config block used by apply_resource_limits.
_CGROUP_DEFAULT_MAX_PROCESSES = 8192  # pids.max counts TASKS (threads), not processes;
# 1024 starved legitimate JVM build trees (Gradle + parallel test workers need
# thousands of threads -> pthread_create EAGAIN / 'unable to create native thread'
# while the host is idle); 8192 still bounds fork bombs which spawn tens of
# thousands of tasks near-instantly. Override via resource_limits.max_processes.

# CPUWeight — proportional CPU share for agent scopes (systemd default is 100).
# Setting 50 makes agent scopes yield to interactive work under CPU contention
# while still using 100% of idle CPU — proportional share, never a hard throttle.
# Both grok-build and OpenClaw ship no default CPU quota; fair-share weight is
# the correct default for agent workloads that include legitimate builds.
_CGROUP_DEFAULT_CPU_WEIGHT = 50

# The memory.max default is HOST-PROPORTIONAL, not a flat cap: the agent
# subprocess tree may occupy up to this fraction of physical RAM before the
# kernel OOM-kills the scope. This is a PER-SCOPE ceiling (each spawn gets its
# own transient scope), so 65% bounds a single runaway tree to a share that
# leaves headroom for the OS + gateway — it is NOT an aggregate host guarantee
# across many concurrent scopes. It gives the agent real headroom on the 16–32
# GB machines this targets (16 GB → ~10.6 GB, 32 GB → ~21.3 GB) — where a flat
# 8 GB cap was both too tight on big boxes and too loose on small ones. There
# is deliberately NO floor: a floor could push a tiny box above 65%, and 65% is
# the ceiling on our take.
_CGROUP_MEMORY_FRACTION = 0.65
# Fallback memory.max (MB) used only when physical RAM can't be read (sysconf
# missing/unknown). The cgroup path is Linux-only, where SC_PHYS_PAGES exists,
# so this is a belt-and-suspenders default, not the normal path.
_CGROUP_FALLBACK_MAX_MEMORY_MB = 8192

# The slice every agent scope nests under (systemd dash-hierarchy places it at
# kirocrew.slice/kirocrew-agents.slice inside the user manager). It is also
# the aggregate enforcement boundary — see ensure_agents_slice_limits().
_CGROUP_AGENTS_SLICE = "kirocrew-agents.slice"


def _instance_slice_token() -> str:
    """A short, systemd-safe token identifying THIS instance's data home.

    Hex only, and specifically NO dashes: systemd reads a dash in a slice name
    as a hierarchy separator, so a token containing one would silently insert
    another slice level (and two tokens could collide on a shared prefix).

    Keyed on the resolved data home because that is already the ownership key
    the rest of the codebase uses — ``cleanup_orphaned_session_roots`` reaps
    from a per-data-home account book, which is exactly why it cannot reach a
    co-resident instance's children. Deriving the slice from the same key keeps
    one notion of "whose process is this" instead of adding a second.
    """
    return hashlib.sha256(str(config_dir()).encode("utf-8")).hexdigest()[:12]


def _agents_slice_name() -> str:
    """The slice new agent scopes are placed in: one child per instance.

    Every gateway on a host shared ONE slice, because the name was a module
    constant with no per-instance component. Nothing downstream could then tell
    one instance's scopes from another's, since a scope's identity is its slice
    plus a timestamp and neither names an owner. That is a correctness floor
    for anything operating on scopes as a population: a host routinely runs
    several gateways at once — ``kirocrew pod up`` creates one per pod by
    design — so "every scope in the agents slice" is not "my scopes", and a
    sweep that assumes it is reaches into a live co-resident session.

    Returns a dash-nested CHILD of :data:`_CGROUP_AGENTS_SLICE`, so the parent
    stays the aggregate enforcement boundary: cgroup v2 bounds a descendant by
    the minimum effective limit along its ancestor chain, so the MemoryHigh /
    MemoryMax on the parent keep applying to every instance's scopes exactly as
    before. Callers that match on the parent's name in a cgroup path (it is a
    path component of every scope either way) are likewise unaffected.

    Degrades to the bare parent slice if the data home cannot be resolved: a
    missing ownership component must not fail the spawn, matching how an
    unavailable cgroup backend is handled.
    """
    try:
        token = _instance_slice_token()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "could not derive a per-instance agent slice (%s); falling back to "
            "the shared %s — scopes from this instance will not be "
            "distinguishable from a co-resident gateway's",
            exc,
            _CGROUP_AGENTS_SLICE,
        )
        return _CGROUP_AGENTS_SLICE
    return f"{_CGROUP_AGENTS_SLICE[: -len('.slice')]}-{token}.slice"


def _default_max_memory_mb() -> int:
    """Return the default cgroup ``memory.max`` in MB: a fixed fraction
    (:data:`_CGROUP_MEMORY_FRACTION`) of physical RAM, so the ceiling scales
    with the machine instead of being a flat cap. Falls back to
    :data:`_CGROUP_FALLBACK_MAX_MEMORY_MB` if host RAM can't be determined.
    """
    try:
        total_bytes = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        mb = int(total_bytes * _CGROUP_MEMORY_FRACTION) // (1024 * 1024)
        if mb > 0:
            return mb
    except (ValueError, OSError, AttributeError):
        pass
    # Windows has no ``os.sysconf``, so the probe above raises AttributeError and
    # would leave a FLAT cap that ignores the machine entirely. That is not a
    # cosmetic gap now that ``apply_windows_resource_ceiling`` consumes this
    # value: on an 8 GB host the fallback EQUALS physical RAM and on a smaller
    # one it exceeds it, so the Job object's memory limit could never engage
    # before the host was exhausted — the ceiling would exist and enforce
    # nothing. Ask the kernel instead. ``system_memory()`` returns None off
    # Windows, so POSIX still reaches the fallback below unchanged.
    mem = platform_compat.system_memory()
    if mem is not None:
        mb = int(mem[0] * _CGROUP_MEMORY_FRACTION) // (1024 * 1024)
        if mb > 0:
            return mb
    return _CGROUP_FALLBACK_MAX_MEMORY_MB


# Cached (available, reason) probe result. Half of what it depends on is NOT
# process-stable: the systemd user manager, its bus and the delegated user slice
# belong to the user's LOGIN, and logind tears all three down at the last logout
# on a host without lingering while ``XDG_RUNTIME_DIR`` stays set in our
# environment. So the cache is keyed on a cheap fingerprint of that session state
# (:func:`_cgroup_scope_session_fingerprint`) and re-validated on a TTL; see
# :func:`_probe_cgroup_scope`.
_CGROUP_SCOPE_PROBE: tuple[bool, str] | None = None
# The fingerprint and monotonic timestamp the cached probe was computed against.
_CGROUP_SCOPE_PROBE_SESSION: tuple[object, ...] | None = None
_CGROUP_SCOPE_PROBE_AT = 0.0
# Upper bound on how long a cached probe is trusted without recomputing it. The
# fingerprint catches the normal logout (logind removes the runtime directory);
# this catches a manager that died and left its socket file behind, which no
# stat can tell apart from a live one. A recompute is a handful of small file
# reads and one non-blocking connect(), so once a minute costs nothing.
_CGROUP_SCOPE_PROBE_TTL_SECONDS = 60.0
_CGROUP_SCOPE_PROBE_LOCK = threading.Lock()
_CGROUP_WARNED = False
# Appended to every "no reachable user manager" reason, so both the one-time
# warning and the mid-run transition warning name the fix an operator can apply.
_CGROUP_LINGER_REMEDY = (
    "the systemd user manager is not running for this user, which logind does "
    "when the last login session ends on a host without lingering; "
    "`loginctl enable-linger $USER` keeps it running (it needs sudo on a managed "
    "host), and the ceiling returns on its own once the "
    "manager is back"
)


def _warn_cgroup_unavailable(reason: str) -> None:
    """Emit the one-time SECURITY warning for a host without cgroup enforcement.

    Shared by the per-spawn wrapper and the slice-limit application so a host
    where delegation is missing produces exactly ONE warning, no matter which
    site notices first — both react to the same host condition.
    """
    global _CGROUP_WARNED
    if _CGROUP_WARNED:
        return
    _CGROUP_WARNED = True
    logger.warning(
        "SECURITY: cgroup v2 scope enforcement unavailable (%s); agent "
        "subprocess fork-bomb / memory-DoS ceilings are NOT enforced on "
        "this host. RLIMIT_NOFILE still applies. See "
        "docs/architecture/resource-protection.md.",
        reason,
    )


def _user_slice_controllers_path() -> str:
    """``cgroup.controllers`` of this uid's logind user slice."""
    return f"/sys/fs/cgroup/user.slice/user-{os.getuid()}.slice/cgroup.controllers"


def _user_bus_socket_paths() -> tuple[str, ...]:
    """Filesystem sockets ``systemd-run --user`` may dial, most specific first.

    ``DBUS_SESSION_BUS_ADDRESS``'s ``unix:path=`` entries, then the two sockets
    systemd derives from ``XDG_RUNTIME_DIR``: the user manager's private socket
    (``systemd-run --scope`` tries it first) and the session bus. Abstract and
    non-unix transports are skipped -- they have no path to stat or dial here.

    A D-Bus address value may percent-escape any byte (the spec's escaping is
    ``%XX`` over the raw bytes), so it is decoded before use; probing the
    escaped spelling literally would miss a live bus and drop the ceiling. A
    decoded value carrying a NUL cannot name any filesystem socket and would
    make ``os.stat`` raise ``ValueError`` on the spawn path, so it is skipped.
    """
    paths: list[str] = []
    for entry in os.environ.get("DBUS_SESSION_BUS_ADDRESS", "").split(";"):
        transport, _, params = entry.partition(":")
        if transport != "unix":
            continue
        for param in params.split(","):
            key, _, value = param.partition("=")
            if key != "path" or not value:
                continue
            decoded = urllib.parse.unquote_to_bytes(value)
            if decoded and b"\0" not in decoded:
                paths.append(os.fsdecode(decoded))
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "")
    if runtime_dir:
        paths.append(os.path.join(runtime_dir, "systemd", "private"))
        paths.append(os.path.join(runtime_dir, "bus"))
    return tuple(dict.fromkeys(paths))


def _stat_identity(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _cgroup_scope_session_fingerprint() -> tuple[object, ...]:
    """Cheap per-spawn identity of the login-scoped state the probe depends on.

    Stats only -- no reads, no connects -- so it is safe on the event loop at
    every spawn. Changes when logind removes or recreates the runtime directory,
    when the user manager re-creates its sockets (new inode), when the user
    slice comes or goes, or when the gateway's bus locators change. Off Linux it
    is constant: nothing there is session-scoped.
    """
    if sys.platform != "linux":
        return ()
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "")
    sockets = _user_bus_socket_paths()
    return (
        runtime_dir,
        _stat_identity(runtime_dir) if runtime_dir else None,
        sockets,
        tuple(_stat_identity(path) for path in sockets),
        _stat_identity(_user_slice_controllers_path()),
    )


def _user_bus_reachable() -> tuple[bool, str]:
    """True when some :func:`_user_bus_socket_paths` socket accepts a connection.

    A socket FILE is not a bus: a manager that died leaves it behind (refused),
    and a seccomp or LSM policy can refuse ``connect()`` outright (EPERM) --
    either way ``systemd-run`` dies with ``Failed to connect to bus`` before it
    execs the command it wraps. The connect is NON-BLOCKING, so a listener with
    a full backlog answers EAGAIN instead of stalling the caller; that still
    proves something is listening and counts as reachable. The connection is
    closed at once, before any D-Bus authentication, which the bus treats as an
    ordinary client hang-up.
    """
    paths = _user_bus_socket_paths()
    if not paths:
        return (False, "no XDG_RUNTIME_DIR (no systemd user session)")
    failures: list[str] = []
    for path in paths:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.setblocking(False)
                err = sock.connect_ex(path)
        except OSError as exc:
            err = exc.errno or errno.EIO
        if err in (0, errno.EAGAIN, errno.EINPROGRESS):
            return (True, "ok")
        failures.append(f"{path}: {os.strerror(err)}")
    return (False, f"user bus unreachable ({'; '.join(failures)}): {_CGROUP_LINGER_REMEDY}")


def _probe_cgroup_scope() -> tuple[bool, str]:
    """Return (available, reason) for unprivileged cgroup-v2 scope enforcement.

    Requires, on Linux: a pure cgroup-v2 mount, the ``pids`` and ``memory``
    controllers delegated to our user slice, a ``systemd-run`` binary, and a
    REACHABLE user bus. Any missing piece → not available.

    The answer follows the user's login rather than the process: it is
    recomputed whenever :func:`_cgroup_scope_session_fingerprint` changes and at
    most every :data:`_CGROUP_SCOPE_PROBE_TTL_SECONDS`. A gateway started inside
    an SSH session therefore stops prepending ``systemd-run`` once logind has
    stopped the user manager -- which it would otherwise keep doing, failing
    every spawn with ``Failed to connect to bus`` -- and takes the same no-scope
    path a gateway started after the logout takes; and it bounds spawns again
    when the manager comes back. Either flip is logged (see
    :func:`_log_cgroup_scope_transition`), so the ceiling is never lost quietly.
    """
    global _CGROUP_SCOPE_PROBE, _CGROUP_SCOPE_PROBE_SESSION, _CGROUP_SCOPE_PROBE_AT
    global _CPU_DELEGATED
    session = _cgroup_scope_session_fingerprint()
    now = time.monotonic()
    cached = _CGROUP_SCOPE_PROBE
    if (
        cached is not None
        and session == _CGROUP_SCOPE_PROBE_SESSION
        and now - _CGROUP_SCOPE_PROBE_AT < _CGROUP_SCOPE_PROBE_TTL_SECONDS
    ):
        return cached
    with _CGROUP_SCOPE_PROBE_LOCK:
        cached = _CGROUP_SCOPE_PROBE
        if (
            cached is not None
            and session == _CGROUP_SCOPE_PROBE_SESSION
            and now - _CGROUP_SCOPE_PROBE_AT < _CGROUP_SCOPE_PROBE_TTL_SECONDS
        ):
            return cached
        result = _compute_cgroup_scope_probe()
        _CGROUP_SCOPE_PROBE = result
        _CGROUP_SCOPE_PROBE_SESSION = session
        _CGROUP_SCOPE_PROBE_AT = now
        # The cpu controller lives in the same user slice; re-read it with the rest.
        _CPU_DELEGATED = None
    if cached is not None and cached[0] != result[0]:
        _log_cgroup_scope_transition(result)
    return result


def _log_cgroup_scope_transition(result: tuple[bool, str]) -> None:
    """Report a mid-run flip of scope availability, once per flip.

    Losing the ceiling is a SECURITY warning even when the one-time startup
    warning already fired for an earlier outage: the operator must be able to
    see from the log WHEN new spawns stopped being bounded. It also marks the
    one-time warning as spent, so ``cgroup_scope_argv`` does not repeat it.
    """
    global _CGROUP_WARNED
    available, reason = result
    if available:
        logger.info(
            "cgroup v2 scope enforcement is available again; new agent subprocesses "
            "are bounded by the fork-bomb / memory-DoS ceilings once more."
        )
        return
    _CGROUP_WARNED = True
    logger.warning(
        "SECURITY: cgroup v2 scope enforcement became unavailable while the gateway "
        "was running (%s). New agent subprocesses start WITHOUT the fork-bomb / "
        "memory-DoS ceilings (RLIMIT_NOFILE still applies) instead of failing with "
        "'Failed to connect to bus'. See docs/architecture/resource-protection.md.",
        reason,
    )


def _compute_cgroup_scope_probe() -> tuple[bool, str]:
    """Uncached capability check backing :func:`_probe_cgroup_scope`."""
    if sys.platform != "linux":
        return (False, "not Linux")
    # The same fixed-directory lookup ``cgroup_scope_argv`` pins its wrapper
    # with, minus its PATH-walking miss diagnostic: this runs on the event loop
    # each time the probe refreshes, so it must stay a handful of stats and
    # never touch a PATH entry that may sit on a stalled mount. The miss is
    # reported through the reason below instead.
    if platform_compat.trusted_system_bin_quiet("systemd-run") is None:
        return (False, "systemd-run not found in a trusted system directory")
    # A user session bus is required for `systemd-run --user`. The variable
    # alone proves nothing: a login shell sets it from $UID by formula, so it
    # stays set after logind removed the directory it names.
    if not os.environ.get("XDG_RUNTIME_DIR"):
        return (False, "no XDG_RUNTIME_DIR (no systemd user session)")
    bus_ok, bus_reason = _user_bus_reachable()
    if not bus_ok:
        return (False, bus_reason)
    # Pure cgroup v2 unified hierarchy.
    try:
        with open("/proc/self/cgroup", encoding="utf-8") as fh:
            # v2 is a single line beginning "0::".
            if not any(line.startswith("0::") for line in fh):
                return (False, "not a cgroup v2 unified hierarchy")
    except OSError as exc:
        return (False, f"cannot read /proc/self/cgroup: {exc}")
    # The pids + memory controllers must be delegated to our user slice, else
    # systemd-run --scope can set the knobs but the kernel won't enforce them.
    try:
        with open(_user_slice_controllers_path(), encoding="utf-8") as fh:
            controllers = set(fh.read().split())
        missing = {"pids", "memory"} - controllers
        if missing:
            return (False, f"controllers not delegated: {sorted(missing)}")
    except OSError as exc:
        return (False, f"cannot read delegated controllers: {exc}")
    return (True, "ok")


_CPU_DELEGATED: bool | None = None


def _cpu_controller_delegated() -> bool:
    """Return True when the ``cpu`` controller is delegated to our user slice.

    CPUWeight / CPUQuota on a ``systemd-run --user`` scope are only enforced
    when the cpu controller is delegated; emitting them without delegation is
    a silent no-op at best and a warning at worst, so callers gate the CPU
    properties on this check. Cached alongside the main probe, and dropped
    whenever :func:`_probe_cgroup_scope` recomputes, because the user slice it
    reads belongs to the login session. Failure to read → False (skip CPU
    properties, keep pids/memory enforcement).
    """
    global _CPU_DELEGATED
    if _CPU_DELEGATED is None:
        try:
            with open(_user_slice_controllers_path(), encoding="utf-8") as fh:
                _CPU_DELEGATED = "cpu" in fh.read().split()
        except OSError:
            _CPU_DELEGATED = False
    return _CPU_DELEGATED


def _cgroup_limits_from_config() -> tuple[int, int, int, int]:
    """Return ``(max_processes, max_memory_mb, cpu_weight, max_cpu_percent)``
    for the cgroup scope.

    Reads the same ``resource_limits`` config block as apply_resource_limits;
    falls back to the module defaults. ``0`` (or junk) means "use default" for
    the cgroup ceiling — unlike the RLIMIT path, we never leave the cgroup DoS
    ceiling unset by default (that is the whole point of this control). The
    memory default is host-proportional (see :func:`_default_max_memory_mb`).

    ``max_cpu_percent`` is the OPT-IN hard CPU quota (``CPUQuota``): ``0``
    (the default) means "no quota property emitted at all" — hard CPU caps
    slow legitimate builds, so unlike the other ceilings this one is off
    unless an operator explicitly sets ``resource_limits.max_cpu_percent``.
    """
    max_procs = _CGROUP_DEFAULT_MAX_PROCESSES
    max_mem_mb = _default_max_memory_mb()
    cpu_weight = _CGROUP_DEFAULT_CPU_WEIGHT
    max_cpu_percent = 0  # opt-in: 0 = emit no CPUQuota
    try:
        # circular import: sandbox is a low-level module imported by
        # config/security consumers — importing kiro_crew.config.loader at
        # module load would create an import cycle, so it stays function-level
        # (same pattern as resource_limit_preexec below).
        from kiro_crew.config.loader import ResourceLimitsConfig, _raw_config

        # One validated read for the whole block. ResourceLimitsConfig.from_raw
        # is the only place these keys are coerced, and it is what refuses a
        # fraction, a NaN/Infinity from json.loads, and a non-number before
        # ``int()`` can raise on them and abort the remaining fields.
        rl = ResourceLimitsConfig.from_raw(_raw_config().get("resource_limits"))
        # ``>= 1``, so 0 lands on the default with everything else out of domain:
        # TasksMax=0 / MemoryMax=0M are rejected by systemd and the scope would
        # never start, so this ceiling is never left unset. The SAME two keys
        # mean "leave inherited" when 0 reaches the rlimit path in
        # security.apply_resource_limits — ResourceLimitsConfig carries both
        # domains so neither side can be tightened without seeing the other.
        if rl.max_processes is not None and rl.max_processes >= 1:
            max_procs = rl.max_processes
        if rl.max_memory_mb is not None and rl.max_memory_mb >= 1:
            max_mem_mb = rl.max_memory_mb
        # Range-checked at the parse site (1..10000), so any value that arrives
        # here is usable as-is.
        if rl.cpu_weight is not None:
            cpu_weight = rl.cpu_weight
        # Opt-in: 0 keeps the "emit no CPUQuota" default rather than capping.
        if rl.max_cpu_percent is not None and rl.max_cpu_percent > 0:
            max_cpu_percent = rl.max_cpu_percent
    except Exception:
        logger.debug("cgroup limits: config unavailable, using defaults")
    return max_procs, max_mem_mb, cpu_weight, max_cpu_percent


# ── aggregate agent-slice soft ceiling (memory.high on kirocrew-agents.slice) ──
# Per-scope MemoryMax bounds ONE runaway spawn tree, but scopes are created per
# spawn: several concurrent agent trees, each legitimately under its own 65%
# cap, can still sum past physical RAM and livelock a swapless host. memory.high
# on the slice all agent scopes share throttles-and-reclaims the whole subtree
# once the SUM of agent memory crosses it, keeping the kernel and the gateway
# (which lives outside the slice) responsive — a soft layer BELOW the slice's
# hard aggregate memory.max (ensure_agents_slice_limits), which OOM-kills a
# scope only when throttling was not enough, while each scope's own memory.max
# still hard-kills an individual runaway. Same trust model as the scope
# ceilings: unprivileged, enforced by the user manager, requires the memory
# controller delegated to the user slice (the existing _probe_cgroup_scope
# check).

# Fraction of physical RAM used for the default slice memory.high. Higher than
# the per-scope 65% because it bounds the SUM of all agent scopes, and below
# the slice's hard 80% memory.max so throttling engages before the kernel
# OOM-kills, with OS + gateway headroom preserved even while the whole fleet
# is being throttled.
_SLICE_MEMORY_HIGH_FRACTION = 0.75
# Fallback slice memory.high (MB) used only when physical RAM can't be read.
# The slice path is Linux-only, where SC_PHYS_PAGES exists, so this is a
# belt-and-suspenders default, not the normal path.
_SLICE_FALLBACK_MEMORY_HIGH_MB = 12288

# Last MemoryHigh value applied to the slice by THIS process ("24576M" /
# "infinity"), or None before the first reconcile. The desired value is
# host-derived and constant for the process's life (no config input), so
# after the first successful apply every later reconcile reduces to a
# string compare and no-ops. Kept as a reconcile (rather than a one-shot)
# so an apply that failed transiently is retried on the next spawn.
_SLICE_MEMHIGH_APPLIED: str | None = None
# Process-level kill switch: set after a failed apply so a broken systemctl is
# warned about ONCE and never hammered on every subsequent spawn.
_SLICE_MEMHIGH_DISABLED = False


def _default_slice_memory_high_mb() -> int:
    """Return the default slice ``memory.high`` in MB: a fixed fraction
    (:data:`_SLICE_MEMORY_HIGH_FRACTION`) of physical RAM, falling back to
    :data:`_SLICE_FALLBACK_MEMORY_HIGH_MB` if host RAM can't be determined.
    """
    try:
        total_bytes = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        mb = int(total_bytes * _SLICE_MEMORY_HIGH_FRACTION) // (1024 * 1024)
        if mb > 0:
            return mb
    except (ValueError, OSError, AttributeError):
        pass
    return _SLICE_FALLBACK_MEMORY_HIGH_MB


def _ensure_agent_slice_memory_high() -> None:
    """Reconcile ``MemoryHigh`` on :data:`_CGROUP_AGENTS_SLICE` with the host default.

    The slice is UID-GLOBAL: every gateway instance under this user (live,
    dev-backend, pods with delegation) parents scopes into the same slice, so
    the ceiling is deliberately NOT config-driven — a per-instance config key
    would let one instance (e.g. a dev gateway configured permissively) lift
    or lower the ceiling that protects the others. The value is always the
    host-derived default (:func:`_default_slice_memory_high_mb`).

    Runs ``systemctl --user set-property --runtime`` — unprivileged: the user
    manager owns the slice, and the memory controller is delegated wherever the
    caller's probe passed. ``--runtime`` is deliberate: the drop-in lives under
    ``$XDG_RUNTIME_DIR`` and vanishes with the login session, so no persistent
    unit files accumulate under ``~/.config`` and a stale ceiling can never
    outlive the login session that set it.

    Never raises: agent spawns must not fail because the ceiling could not be
    applied. On failure it logs one loud warning and disarms for the rest of
    the process.
    """
    global _SLICE_MEMHIGH_APPLIED, _SLICE_MEMHIGH_DISABLED
    if _SLICE_MEMHIGH_DISABLED:
        return
    desired = f"{_default_slice_memory_high_mb()}M"
    if desired == _SLICE_MEMHIGH_APPLIED:
        return
    try:
        systemctl = platform_compat.trusted_system_bin("systemctl")
        if systemctl is None:
            raise FileNotFoundError("systemctl not found in trusted system dirs")
        proc = subprocess.run(
            [
                systemctl,
                "--user",
                "set-property",
                "--runtime",
                _CGROUP_AGENTS_SLICE,
                f"MemoryHigh={desired}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "").strip() or "non-zero exit")
    except Exception as exc:
        _SLICE_MEMHIGH_DISABLED = True
        logger.warning(
            "SECURITY: could not apply MemoryHigh=%s to %s (%s); the AGGREGATE "
            "agent memory ceiling is NOT enforced on this host — per-scope "
            "MemoryMax still applies. See "
            "docs/architecture/resource-protection.md.",
            desired,
            _CGROUP_AGENTS_SLICE,
            exc,
        )
        return
    _SLICE_MEMHIGH_APPLIED = desired
    logger.info("agent slice %s: MemoryHigh=%s applied", _CGROUP_AGENTS_SLICE, desired)


# Last observed value of the slice's memory.events `high` counter, or None
# before the first successful read. The first read only baselines — the
# counter is monotonic for the slice cgroup's lifetime, so a nonzero value
# may predate this process — and climbs are judged against it.
_SLICE_MEMHIGH_EVENTS_SEEN: int | None = None
# True while inside a climbing episode that has already been warned about, so
# sustained throttling logs once per episode instead of on every spawn. Reset
# when an observation finds the counter stable (episode over) or lower (slice
# cgroup recreated).
_SLICE_MEMHIGH_CLIMB_WARNED = False


def _slice_memory_events_high() -> int | None:
    """Return the ``high`` counter from the slice cgroup's ``memory.events``.

    The slice directory comes from :func:`_agents_slice_cgroup_dir`, which
    understands systemd's dash-hierarchy (``kirocrew-agents.slice`` nests
    under ``kirocrew.slice`` in the user manager's subtree). ``None`` when it
    cannot be read: not Linux, no cgroup v2, the slice cgroup not currently
    materialized (systemd releases an empty slice), or unparseable content.
    """
    slice_dir = _agents_slice_cgroup_dir()
    if slice_dir is None:
        return None
    return _read_cgroup_counters(slice_dir / "memory.events").get("high")


def _check_slice_memory_pressure() -> None:
    """Warn when the slice's ``memory.events`` ``high`` counter climbs.

    Past ``memory.high`` the kernel throttles-and-reclaims the subtree
    SILENTLY — agents just slow down; nothing kills and nothing alerts, since
    per-scope ``MemoryMax`` never fired. The ``high`` counter climbing is the
    kernel's only signal that the aggregate ceiling is throttling, and
    surfacing it makes "agents mysteriously slow" diagnosable from the
    gateway log as ceiling throttling rather than a hang.

    Warned once per climbing episode: the first observed increase logs, later
    increases stay silent until an observation finds the counter stable,
    which closes the episode. A DECREASE means the slice cgroup was recreated
    (an empty slice is released and its counters reset) — re-baseline
    silently, never warn.
    """
    global _SLICE_MEMHIGH_EVENTS_SEEN, _SLICE_MEMHIGH_CLIMB_WARNED
    current = _slice_memory_events_high()
    if current is None:
        return
    previous = _SLICE_MEMHIGH_EVENTS_SEEN
    _SLICE_MEMHIGH_EVENTS_SEEN = current
    if previous is None or current <= previous:
        # First read, counter stable, or slice cgroup recreated: (re)baseline
        # and close any open episode.
        _SLICE_MEMHIGH_CLIMB_WARNED = False
        return
    if _SLICE_MEMHIGH_CLIMB_WARNED:
        return
    _SLICE_MEMHIGH_CLIMB_WARNED = True
    logger.warning(
        "agent slice %s: memory.events high counter climbed %d -> %d — "
        "aggregate agent memory crossed the slice MemoryHigh ceiling and the "
        "kernel is throttling the whole agent subtree; agents run slowly "
        "(not hung) until aggregate memory drops. See "
        "docs/architecture/resource-protection.md.",
        _CGROUP_AGENTS_SLICE,
        previous,
        current,
    )


# Serializes reconciliation workers. Deliberately a plain blocking mutex held
# for the whole reconcile body: every schedule spawns its own short-lived
# worker and workers queue on the mutex, so concurrent spawns never interleave
# systemctl calls and a failed apply is retried by the next spawn's worker.
# The desired value is host-derived and process-constant (no config input) —
# the mutex guards the apply/retry handoff, not value freshness. Redundant
# workers are near-free (the applied-value check reduces them to a string
# compare), and thread count is bounded by concurrent agent spawns.
_SLICE_MEMHIGH_MUTEX = threading.Lock()


def _reconcile_slice_memory_high_off_thread() -> None:
    """Reconcile ``MemoryHigh`` and check slice throttling in a daemon thread.

    The reconciliation reads config and shells out to ``systemctl`` (up to
    10s), and its caller sits on the agent-spawn path, which runs on the
    gateway event loop — so it must never execute inline. Fire-and-forget is
    semantically safe: ``MemoryHigh`` set on a slice applies to members that
    are already running, so a reconciliation that lands moments after the
    spawn still bounds it, and every later spawn re-reconciles. The worker
    also runs :func:`_check_slice_memory_pressure`, so throttle visibility
    shares the reconcile cadence (per spawn) and its kill switch.
    """
    global _SLICE_MEMHIGH_DISABLED
    if _SLICE_MEMHIGH_DISABLED:
        return

    def _worker() -> None:
        with _SLICE_MEMHIGH_MUTEX:
            _ensure_agent_slice_memory_high()
            _check_slice_memory_pressure()

    try:
        threading.Thread(target=_worker, name="agent-slice-memhigh", daemon=True).start()
    except RuntimeError as exc:
        # Thread exhaustion. This sits on the agent-spawn path, so it must
        # never abort the spawn. Disarm like any other reconciliation
        # failure: per-scope MemoryMax still applies.
        _SLICE_MEMHIGH_DISABLED = True
        logger.warning(
            "SECURITY: could not start the MemoryHigh reconciliation thread "
            "(%s); the AGGREGATE agent memory ceiling is NOT enforced on this "
            "host — per-scope MemoryMax still applies.",
            exc,
        )


# ``resource_limits`` keys whose only enforcement is a cgroup/systemd property
# or a POSIX rlimit. The Windows Job object carries per-spawn processes and
# memory only, so a value set for any of these is accepted but not enforced.
_WINDOWS_INERT_RESOURCE_LIMIT_KEYS = (
    "cpu_weight",
    "max_cpu_percent",
    "max_cpu_seconds",
    "max_open_files",
    "max_total_memory_mb",
    "max_total_processes",
)
_WINDOWS_INERT_LIMITS_WARNED = False


def _warn_windows_inert_resource_limits_once() -> None:
    """Log ONE warning naming each set ``resource_limits`` key Windows ignores.

    A key counts as set when it is non-null and non-zero. Nothing is logged
    when no such key is configured, and later calls are no-ops either way.
    """
    global _WINDOWS_INERT_LIMITS_WARNED
    if _WINDOWS_INERT_LIMITS_WARNED:
        return
    _WINDOWS_INERT_LIMITS_WARNED = True
    try:
        # circular import: same function-level pattern as _cgroup_limits_from_config.
        from kiro_crew.config.loader import ResourceLimitsConfig, _raw_config

        rl = ResourceLimitsConfig.from_raw(_raw_config().get("resource_limits"))
    except Exception:
        logger.debug("inert resource_limits check: config unavailable")
        return
    inert = [key for key in _WINDOWS_INERT_RESOURCE_LIMIT_KEYS if getattr(rl, key, None)]
    if inert:
        logger.warning(
            "resource_limits.%s %s set but not enforced on Windows: the Job "
            "object bounds only per-spawn max_processes / max_memory_mb, and "
            "these keys need a systemd user manager or POSIX rlimits.",
            ", resource_limits.".join(inert),
            "is" if len(inert) == 1 else "are",
        )


def apply_windows_resource_ceiling(pid: int) -> bool:
    """Windows counterpart to :func:`cgroup_scope_argv`, applied AFTER the spawn.

    ``cgroup_scope_argv`` bounds an agent subprocess and all its descendants by
    prepending ``systemd-run --user --scope`` with ``TasksMax`` / ``MemoryMax``.
    That has no Windows equivalent expressible as an argv prefix, so there it
    returns argv unchanged and logs a one-time loud SECURITY warning — the
    fork-bomb and memory-DoS ceilings were simply absent on that platform.

    A Job object is the native mechanism (limits cover every process in the job,
    and a member's descendants join automatically), but it must be applied to a
    live pid rather than baked into argv. Callers therefore invoke this right
    after the spawn returns, in ADDITION to the ``cgroup_scope_argv`` call they
    already make (a no-op on Windows), and while the child is still suspended —
    see :func:`platform_compat.apply_job_limits` for why that ordering is what
    makes the ceiling airtight.

    Reads the SAME ``resource_limits`` config as the cgroup path, so one operator
    setting governs both platforms.

    Returns ``True`` when a ceiling was installed; ``False`` on non-Windows
    (nothing to do — the cgroup wrapper owns it) or on any failure, which
    :func:`platform_compat.apply_job_limits` has already logged as a SECURITY
    warning. Never raises: a missing ceiling must not fail the spawn, matching
    how an unavailable cgroup scope is handled.
    """
    if not platform_compat.IS_WINDOWS:
        return False
    _warn_windows_inert_resource_limits_once()
    max_procs, max_mem_mb, _cpu_weight, _max_cpu_percent = _cgroup_limits_from_config()
    return platform_compat.apply_job_limits(
        pid,
        max_procs=max_procs,
        max_memory_bytes=max_mem_mb * 1024 * 1024,
    )


# Prefix of the scope unit name a caller gets from ``scope_unit_name``. Carries
# the owning gateway's product name so a human reading ``systemctl --user
# list-units`` can tell an agent scope from anything else in the slice, and is
# the token the reverse lookup matches on.
_SCOPE_UNIT_PREFIX = "kirocrew-rt-"
# systemd unit names accept alphanumerics and ``:-_.\`` plus escapes; anything
# else has to be escaped to be a legal name. Rather than escape, a token
# carrying something else is REFUSED (see ``scope_unit_name``), because every
# caller mints its own token and a token needing escapes is a caller bug.
_SCOPE_UNIT_TOKEN_SAFE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")


def scope_unit_name(token: str) -> str | None:
    """The scope unit name for a spawn identified by *token*, or None.

    ``cgroup_scope_argv`` otherwise lets systemd auto-name the scope
    ``run-u<N>.scope``, whose only content is "the Nth transient unit this user
    manager made". One scope holds one spawn, and one spawn is one agent
    RUNTIME, which may serve several sessions -- so an anonymous scope name is
    the reason a reader holding a scope (the slice OOM report at
    :func:`check_agents_slice_pressure`, ``systemd-cgls``, an operator reading
    ``systemctl --user list-units``) can name the victim's directory but not the
    runtime it held, nor the sessions leasing that runtime. Naming the scope
    after the runtime's own spawn token closes that: the SAME token travels in
    the child's environment as ``KIROCREW_SPAWN_INSTANCE``, so a scope name and
    a live process both resolve to one incarnation. For the case that matters
    most -- a scope the kernel already killed, where both the environment and the
    in-memory ``_process_instance`` are gone -- ``AcpRuntime`` logs this unit name
    beside its pid at initialization, and sessions are logged against that same
    pid as they are created, so the join survives the process.

    Returns ``None`` when *token* cannot form a legal unit name, and the caller
    then wraps the spawn exactly as before -- anonymously, but bounded. That
    direction is deliberate: the scope's JOB is the DoS ceiling, and a naming
    defect must never be able to cost a spawn its ceiling or fail the spawn
    outright. ``token`` is expected to be an opaque random identifier, so it is
    validated rather than escaped; a value needing escapes is a caller bug and
    is refused rather than mangled into a name a reverse lookup would miss.
    """
    if not _SCOPE_UNIT_TOKEN_SAFE.match(token or ""):
        return None
    return f"{_SCOPE_UNIT_PREFIX}{token}.scope"


def cgroup_scope_argv(argv: list[str]) -> list[str]:
    """Wrap *argv* in a transient systemd --user --scope with cgroup v2 limits.

    Prepends ``systemd-run --user --scope`` with ``TasksMax`` (pids.max, the
    fork-bomb ceiling), ``MemoryMax`` + ``MemorySwapMax=0`` (memory.max, the
    RSS balloon ceiling), and — when the cpu controller is delegated —
    ``CPUWeight`` (proportional fair-share: agents run full speed on an idle
    host but yield to interactive work under contention; never a hard
    throttle) plus an OPT-IN ``CPUQuota`` hard cap
    (``resource_limits.max_cpu_percent``, off by default because hard quotas
    slow legitimate builds), so the spawned agent AND all its MCP-server/tool
    descendants are bounded as one cgroup and the kernel kills the scope on
    breach. ``--scope`` execs into the target (it does NOT fork a wrapper), so
    the returned argv's eventual PID is the real child — parent PID tracking,
    ``killpg``, and descendant scans are unaffected.

    Every scope is parented under a per-instance child of
    :data:`_CGROUP_AGENTS_SLICE` (see :func:`_agents_slice_name`, which explains
    why the owner has to be expressible), and the slice-level soft ceiling
    (``MemoryHigh``, see :func:`_ensure_agent_slice_memory_high`) — a
    host-derived constant (75% of RAM) — is ensured before each wrap on the
    PARENT, throttling the SUM of all concurrent agent trees before the slice's
    hard ``MemoryMax`` (:func:`ensure_agents_slice_limits`) OOM-kills a scope.
    cgroup v2 bounds a descendant by the minimum effective limit along its
    ancestor chain, so nesting one level deeper does not loosen either ceiling.

    Layers OUTSIDE the OS-level sandbox: callers pass the already-``wrap_argv``-ed
    argv here so the child is filesystem-isolated AND cgroup-bounded.

    The ceiling is per SPAWN, and therefore per agent RUNTIME rather than per
    SESSION: one process can serve several sessions (a parent and the subagents
    whose sessions are created on its runtime), and cgroup v2 kills a breaching
    scope as ONE unit, so every session on that runtime goes together. The
    per-scope values come from :func:`_cgroup_limits_from_config`, which is where
    a ceiling that scales with the number of sessions a runtime serves would
    attach -- it is the single place both the value and its config source are
    resolved, so a scaling factor applied there reaches every caller without any
    spawn site being taught about sessions.

    The scope itself is left ANONYMOUS here -- systemd auto-names it
    ``run-u<N>.scope``. A caller that can name the runtime it is spawning passes
    the result through :func:`name_scope_unit`, which is a separate step so that
    this function's argv stays byte-identical for the many callers wrapping a
    one-off tool or app subprocess that no reader needs to resolve back to a
    runtime.

    On a host without cgroup v2 delegation (older Linux, no systemd user
    session, macOS), returns *argv* unchanged and logs a one-time loud SECURITY
    warning — the RLIMIT_NOFILE preexec still applies, but the fork-bomb/memory
    DoS ceiling is NOT enforced there. The same degradation applies when
    ``systemd-run`` resolves outside the trusted system directories: prepending
    an unpinned wrapper name would trade a DoS ceiling for an exec-hijack
    channel, which is the worse of the two.

    The returned wrapper is an ABSOLUTE path, so callers may hand the result to
    a spawn whose ``env`` carries a config-declared PATH without that PATH being
    able to redirect argv[0].
    """
    available, reason = _probe_cgroup_scope()
    if not available:
        _warn_cgroup_unavailable(reason)
        return argv
    # SECURITY: the wrapper this function prepends becomes argv[0], and a caller
    # that hands the result to a spawn with a config-influenced ``env`` has
    # CPython resolve a slash-less argv[0] through THAT env's PATH
    # (os.get_exec_path) -- so a bare name here is an exec-hijack channel that
    # runs BEFORE ``--scope`` establishes confinement. Pin it at the layer that
    # prepends it, so every caller inherits the protection rather than each
    # spawn site remembering to re-pin (the same reason ``sandbox_exec_argv``
    # pins its own wrappers). ``trusted_system_bin`` ignores PATH entirely: a
    # gateway's PATH can legitimately lead with agent-writable directories, so
    # resolving through it would leave the hole half-open. An unresolvable
    # wrapper degrades exactly like a missing cgroup backend -- no ceiling, loud
    # warning -- rather than emitting an unpinned name.
    systemd_run = platform_compat.trusted_system_bin("systemd-run")
    if not systemd_run:
        _warn_cgroup_unavailable("systemd-run is not in a trusted system directory")
        return argv
    # Reconcile the slice-level aggregate ceiling off-thread: this call site
    # runs on the gateway event loop during agent spawn, and reconciliation
    # does config reads + a systemctl subprocess. MemoryHigh on a slice
    # applies to already-running members, so the spawn need not wait for it.
    _reconcile_slice_memory_high_off_thread()
    max_procs, max_mem_mb, cpu_weight, max_cpu_percent = _cgroup_limits_from_config()
    props = [
        "-p",
        f"TasksMax={max_procs}",
        "-p",
        f"MemoryMax={max_mem_mb}M",
        "-p",
        "MemorySwapMax=0",
    ]
    # CPU properties only when the cpu controller is delegated — otherwise the
    # kernel won't enforce them and systemd may warn on every spawn.
    if _cpu_controller_delegated():
        props += ["-p", f"CPUWeight={cpu_weight}"]
        if max_cpu_percent > 0:
            props += ["-p", f"CPUQuota={max_cpu_percent}%"]
    return [
        systemd_run,
        "--user",
        "--scope",
        "-q",
        f"--slice={_agents_slice_name()}",
        *props,
        "--",
        *argv,
    ]


def name_scope_unit(argv: list[str], token: str) -> list[str]:
    """Name the scope in a :func:`cgroup_scope_argv` result after *token*.

    Returns *argv* UNCHANGED unless it really is a systemd-run scope wrapper AND
    *token* forms a legal unit name (:func:`scope_unit_name`). Both degradations
    are one decision: the scope's job is the DoS ceiling, the name is a
    diagnostic, and a diagnostic must never cost a spawn its ceiling nor fail the
    spawn outright. A host without cgroup delegation (where ``cgroup_scope_argv``
    hands back the bare command) and a token that cannot be spelled as a unit
    therefore both leave the spawn exactly as it would otherwise have been.

    A separate step rather than a keyword on ``cgroup_scope_argv`` because that
    function has dozens of callers and is widely replaced by one-argument stubs in
    tests: a keyword there would make every one of those stubs refuse the call,
    for callers that have no runtime to name anyway. The recognition check here
    makes a stubbed wrap a silent no-op instead.

    ``--unit`` goes BEFORE the ``--`` separator. After it, systemd-run reads it as
    an argument to the wrapped command instead of a property of the scope.
    """
    unit = scope_unit_name(token)
    if unit is None or "--scope" not in argv or "--" not in argv:
        return argv
    at = argv.index("--")
    return [*argv[:at], "--unit", unit, *argv[at:]]


# ── aggregate ceiling on the parent slice ──
# The per-scope MemoryMax above bounds ONE spawn tree, but scopes are siblings:
# N concurrent spawns may collectively request N x 65% of host RAM with no
# single cgroup ever breaching its own limit. cgroup v2 enforces limits down
# the tree — a descendant is bounded by the MINIMUM effective limit of itself
# and all ancestors — so the parent slice every scope already nests under is
# the natural aggregate boundary. ensure_agents_slice_limits() puts a ceiling
# on it, yielding a two-level model: slice = aggregate across all concurrent
# agent work, scope = per-tree (unchanged).

# Aggregate memory.max as a fraction of physical RAM. Must sit ABOVE the
# per-scope fraction (0.65) — otherwise the slice would shrink a single
# spawn's existing headroom — and meaningfully below 1.0 so the OS and the
# gateway keep breathing room even when agent work saturates the ceiling.
_CGROUP_TOTAL_MEMORY_FRACTION = 0.80
# Fallback aggregate memory.max (MB) when physical RAM can't be read. Above
# the per-scope fallback (8192) for the same "never clamp a single scope
# tighter than its own ceiling" reason as the fraction.
_CGROUP_FALLBACK_MAX_TOTAL_MEMORY_MB = 12288
# Aggregate pids.max across all concurrent scopes: four fully-loaded scopes'
# worth (4 x 8192). Bounds the composition blow-up (32 scopes x 8192 tasks =
# 262144 otherwise) while still allowing several concurrent JVM-scale builds,
# each of which legitimately needs thousands of threads.
_CGROUP_DEFAULT_MAX_TOTAL_TASKS = 32768


def _default_max_total_memory_mb() -> int:
    """Default aggregate ``memory.max`` (MB) for the agents slice: a fixed
    fraction (:data:`_CGROUP_TOTAL_MEMORY_FRACTION`) of physical RAM, falling
    back to :data:`_CGROUP_FALLBACK_MAX_TOTAL_MEMORY_MB` when RAM is unreadable.
    """
    try:
        total_bytes = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        mb = int(total_bytes * _CGROUP_TOTAL_MEMORY_FRACTION) // (1024 * 1024)
        if mb > 0:
            return mb
    except (ValueError, OSError, AttributeError):
        pass
    return _CGROUP_FALLBACK_MAX_TOTAL_MEMORY_MB


def _slice_limits_from_config() -> tuple[int, int]:
    """Return ``(max_total_memory_mb, max_total_tasks)`` for the agents slice.

    Reads ``resource_limits.max_total_memory_mb`` / ``max_total_processes``
    from the same config block as the per-scope knobs. ``0`` or junk means
    "use default" — the aggregate ceiling is never left unset, matching the
    per-scope rule in :func:`_cgroup_limits_from_config`. The two memory knobs
    are deliberately independent of one another: per-scope answers "how big may
    one tree get", aggregate answers "how much may all trees claim together".
    """
    total_mem_mb = _default_max_total_memory_mb()
    total_tasks = _CGROUP_DEFAULT_MAX_TOTAL_TASKS
    try:
        # circular import: same constraint as _cgroup_limits_from_config —
        # config.loader consumers import sandbox, so the import stays local.
        from kiro_crew.config.loader import ResourceLimitsConfig, _raw_config

        # Same single validated read as the per-scope knobs. This function used
        # to test ``int(m) >= 1`` directly, which raises on a NaN/Infinity that
        # json.loads happily produces — and the raise landed in the except below,
        # discarding a VALID max_total_processes set alongside a junk memory
        # value. from_raw refuses both before int() sees them, per key.
        rl = ResourceLimitsConfig.from_raw(_raw_config().get("resource_limits"))
        if rl.max_total_memory_mb is not None and rl.max_total_memory_mb >= 1:
            total_mem_mb = rl.max_total_memory_mb
        if rl.max_total_processes is not None and rl.max_total_processes >= 1:
            total_tasks = rl.max_total_processes
    except Exception:
        logger.debug("slice limits: config unavailable, using defaults")
    return total_mem_mb, total_tasks


_SLICE_LIMITS_APPLIED = False


def ensure_agents_slice_limits() -> bool:
    """Apply the aggregate cgroup ceiling to the agents slice. Idempotent.

    Runs ``systemctl --user set-property --runtime`` on
    :data:`_CGROUP_AGENTS_SLICE`, setting ``MemoryMax`` (aggregate across ALL
    concurrent agent scopes), ``MemorySwapMax=0`` (consistent with the
    per-scope property: a true RSS ceiling, no swap escape), and ``TasksMax``
    (aggregate fork-bomb ceiling). Called once at gateway startup.

    ``--runtime`` over a shipped unit drop-in, deliberately: the property is
    re-derived from config and re-applied on every gateway start, so a config
    change can never leave a stale on-disk artifact behind, and uninstalling
    leaves nothing to clean up. The property persists on the user manager
    until logout/reboot — long enough, since the gateway is the long-lived
    process that re-applies it.

    Gated on the same :func:`_probe_cgroup_scope` capability check as the
    per-scope wrapper: where delegation is unavailable this is skipped and the
    single shared SECURITY warning covers both layers — no second warning for
    the same host condition.

    Blocking (shells out): call off-loop (``asyncio.to_thread``).

    Returns True when the ceiling is in place (now or from an earlier call).
    """
    global _SLICE_LIMITS_APPLIED
    if _SLICE_LIMITS_APPLIED:
        return True
    available, reason = _probe_cgroup_scope()
    if not available:
        _warn_cgroup_unavailable(reason)
        return False
    total_mem_mb, total_tasks = _slice_limits_from_config()
    # PATH can legitimately lead with agent-writable directories (a worktree
    # venv's bin, ~/.local/bin), so a bare "systemctl" would let a planted
    # shim run with the gateway's environment. Resolve from fixed system
    # directories only; unavailable = ceiling not applied (per-scope ceilings
    # still hold).
    systemctl = platform_compat.trusted_system_bin("systemctl")
    if systemctl is None:
        logger.warning(
            "could not apply the aggregate cgroup ceiling to %s: no trusted "
            "systemctl binary — per-scope ceilings still apply.",
            _CGROUP_AGENTS_SLICE,
        )
        return False
    cmd = [
        systemctl,
        "--user",
        "set-property",
        "--runtime",
        _CGROUP_AGENTS_SLICE,
        f"MemoryMax={total_mem_mb}M",
        "MemorySwapMax=0",
        f"TasksMax={total_tasks}",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning(
            "could not apply the aggregate cgroup ceiling to %s: %s — "
            "per-scope ceilings still apply, but N concurrent spawns may "
            "collectively exceed host RAM.",
            _CGROUP_AGENTS_SLICE,
            exc,
        )
        return False
    if proc.returncode != 0:
        logger.warning(
            "could not apply the aggregate cgroup ceiling to %s (rc=%d): %s — "
            "per-scope ceilings still apply, but N concurrent spawns may "
            "collectively exceed host RAM.",
            _CGROUP_AGENTS_SLICE,
            proc.returncode,
            (proc.stderr or "").strip(),
        )
        return False
    _SLICE_LIMITS_APPLIED = True
    logger.info(
        "aggregate cgroup ceiling on %s: MemoryMax=%dM MemorySwapMax=0 TasksMax=%d "
        "(per-scope ceilings unchanged)",
        _CGROUP_AGENTS_SLICE,
        total_mem_mb,
        total_tasks,
    )
    return True


# cgroup v2 directory of the systemd user manager's subtree (transient
# --user units always live under user@<uid>.service on the unified
# hierarchy); ``{uid}`` is filled at resolve time. Module-level so tests can
# point the resolver at a fabricated tree.
_USER_MANAGER_CGROUP_BASE = "/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service"


def _agents_slice_cgroup_dir() -> Path | None:
    """Resolve the agents slice's cgroup directory, or None when absent.

    systemd's dash-hierarchy places ``kirocrew-agents.slice`` under
    ``kirocrew.slice`` inside the user manager's subtree; the direct
    construction covers that. The shallow scan tolerates a manager that laid
    the slice out differently (one extra level only — never a recursive walk).
    The directory exists only while the slice is active (a runtime property or
    a live scope holds it); None simply means "no agent work to observe".
    """
    if sys.platform != "linux":
        return None
    base = Path(_USER_MANAGER_CGROUP_BASE.format(uid=os.getuid()))
    direct = base / "kirocrew.slice" / _CGROUP_AGENTS_SLICE
    if direct.is_dir():
        return direct
    try:
        for child in base.iterdir():
            cand = child / _CGROUP_AGENTS_SLICE
            if cand.is_dir():
                return cand
    except OSError:
        pass
    return None


def _read_cgroup_counters(path: Path) -> dict[str, int]:
    """Parse a ``memory.events``-style key/value cgroup file into a dict."""
    counters: dict[str, int] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition(" ")
            if value.strip().isdigit():
                counters[key] = int(value)
    except OSError:
        pass
    return counters


def read_cgroup_int(path: str | Path) -> int | None:
    """Read a single-value cgroup file (``memory.high``, ``memory.max`` and kin).

    The one reader for every single-integer cgroup file the product consults,
    here and in ``subagent``'s memory probe. ``None`` when the file is absent,
    unparseable, or holds the ``max`` sentinel the kernel writes for "no
    limit" -- every caller treats all three the same way, as "this bound does
    not constrain".
    """
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.isdigit():
        return None
    return int(text)


# Last ``memory.events`` ``high`` counter seen by ``agents_slice_throttling``.
# Separate from ``_SLICE_MEMHIGH_EVENTS_SEEN``: that one paces a once-per-episode
# WARNING from the reconcile worker, this one answers a yes/no question for a
# caller that is about to commit a cold start. Sharing the baseline would let
# either reader consume the other's climb.
_SLICE_THROTTLE_PROBE_SEEN: int | None = None

# ``time.monotonic()`` of the most recent counter advance any probe observed.
# The advance itself is consumed by the probe that reads it (the baseline moves
# to the new value), so a second probe moments later would otherwise read a
# stable counter and answer ``False`` while the episode is still under way. The
# timestamp makes the edge a shared, time-bounded fact instead of a
# one-reader event: every probe inside the hold window agrees.
_SLICE_THROTTLE_EDGE_AT: float | None = None
_SLICE_THROTTLE_EDGE_HOLD_SECS = 60.0


def agents_slice_throttling() -> bool:
    """Whether the kernel is throttling the agents slice right now.

    Two signals, either suffices. ``memory.current >= memory.high`` is the
    kernel's own definition of "over the soft ceiling"; under sustained
    pressure reclaim holds usage AT the ceiling rather than above it, so the
    equality is the steady state, not an edge. The ``memory.events`` ``high``
    counter climbing since this function's previous read is the second signal:
    the kernel increments it every time it throttles, so during an episode it
    advances between any two probes even when reclaim has momentarily pushed
    usage under the line. The first read only baselines the counter. An
    observed advance is held for ``_SLICE_THROTTLE_EDGE_HOLD_SECS`` so that
    concurrent callers (two cold starts racing one counter tick) all read the
    same verdict instead of the first one consuming the edge.

    ``False`` whenever there is nothing to read (not Linux, no slice); an
    unmeasurable host is never reported as throttled.
    """
    global _SLICE_THROTTLE_PROBE_SEEN, _SLICE_THROTTLE_EDGE_AT
    slice_dir = _agents_slice_cgroup_dir()
    if slice_dir is None:
        return False
    counter = _read_cgroup_counters(slice_dir / "memory.events").get("high")
    previous = _SLICE_THROTTLE_PROBE_SEEN
    now = time.monotonic()
    if counter is not None:
        _SLICE_THROTTLE_PROBE_SEEN = counter
        if previous is not None and counter > previous:
            _SLICE_THROTTLE_EDGE_AT = now
    high = read_cgroup_int(slice_dir / "memory.high")
    current = read_cgroup_int(slice_dir / "memory.current")
    if high is not None and current is not None and current >= high:
        return True
    edge_at = _SLICE_THROTTLE_EDGE_AT
    return edge_at is not None and (now - edge_at) < _SLICE_THROTTLE_EDGE_HOLD_SECS


# Last-seen slice-level OOM counters, so only NEW kills are reported. Seeded
# lazily from the current values on first read: kills that predate this
# process must not fire a spurious warning at boot.
_SLICE_OOM_SEEN: dict[str, int] | None = None


def check_agents_slice_pressure() -> str | None:
    """Report (and log) new OOM kills inside the agents slice, else None.

    With an aggregate ceiling on the slice, a breach OOM-kills SOME scope in
    it — the kernel picks the victim, not necessarily the spawn that grew.
    Without attribution the operator-visible failure is "a random subagent
    died". This turns it into a diagnosable event: which scopes took kills
    (each scope's own ``memory.events.local oom_kill``), the slice's
    ``memory.current`` vs ``memory.max`` at observation time, and whether the
    SLICE ceiling itself engaged (``memory.events.local max`` on the slice —
    the discriminator between a slice-level breach and a single scope hitting
    its own per-tree limit).

    Reads a handful of cgroup files; never raises. Polled from the resource
    pressure sampler's worker thread, so it is already off-loop. The same
    poll also re-applies the slice ceiling if a user-manager restart dropped
    the --runtime property (see the self-heal block below).
    """
    global _SLICE_OOM_SEEN, _SLICE_LIMITS_APPLIED
    slice_dir = _agents_slice_cgroup_dir()
    if slice_dir is None:
        return None
    # Self-heal: the ceiling is a --runtime property, so a user-manager
    # restart (logout/reboot) silently drops it while the gateway keeps
    # running. This sampler already reads the slice each tick — if the
    # ceiling we applied has vanished (memory.max reads "max"), re-apply it
    # here instead of waiting for the next gateway start. Only when WE
    # applied it before: a host that never passed the delegation gate must
    # not start shelling out from the sampler.
    if _SLICE_LIMITS_APPLIED:
        try:
            if (slice_dir / "memory.max").read_text().strip() == "max":
                _SLICE_LIMITS_APPLIED = False
                ensure_agents_slice_limits()
        except OSError:
            pass
    events = _read_cgroup_counters(slice_dir / "memory.events")
    local = _read_cgroup_counters(slice_dir / "memory.events.local")
    current = {"oom_kill": events.get("oom_kill", 0), "max": local.get("max", 0)}
    if _SLICE_OOM_SEEN is None:
        _SLICE_OOM_SEEN = current
        return None
    new_kills = current["oom_kill"] - _SLICE_OOM_SEEN["oom_kill"]
    slice_max_hits = current["max"] - _SLICE_OOM_SEEN["max"]
    _SLICE_OOM_SEEN = current
    if new_kills <= 0:
        return None
    victims: list[str] = []

    def _record_if_killed(scope_dir: Path) -> None:
        scope_local = _read_cgroup_counters(scope_dir / "memory.events.local")
        if scope_local.get("oom_kill", 0) > 0:
            victims.append(scope_dir.name)

    try:
        # Scopes sit one level deep: each instance places
        # its own under a per-instance child slice (_agents_slice_name), so
        # scanning only direct children would report "(already reaped)" for
        # every real victim. Both shapes are walked rather than just the nested
        # one, because the aggregate slice is shared host-wide: a co-resident
        # gateway still on an older build keeps putting scopes directly here,
        # and its OOM kills are still worth naming.
        for child in slice_dir.iterdir():
            if not child.is_dir():
                continue
            if child.suffix == ".scope":
                _record_if_killed(child)
            elif child.suffix == ".slice":
                for grandchild in child.iterdir():
                    if grandchild.suffix == ".scope" and grandchild.is_dir():
                        _record_if_killed(grandchild)
    except OSError:
        pass
    mem_current = -1
    try:
        mem_current = int((slice_dir / "memory.current").read_text().strip())
    except (OSError, ValueError):
        pass
    try:
        mem_max = (slice_dir / "memory.max").read_text().strip()
    except OSError:
        mem_max = "?"
    message = (
        f"cgroup OOM kill inside {_CGROUP_AGENTS_SLICE}: {new_kills} new kill(s); "
        f"slice memory.current={mem_current} memory.max={mem_max}; "
        f"slice-level aggregate ceiling engaged: "
        f"{'yes' if slice_max_hits > 0 else 'no (a scope hit its own per-tree limit)'}; "
        f"scopes with recorded kills: {victims or '(already reaped)'}"
    )
    logger.warning("%s", message)
    return message


# ``systemd-run --user`` finds the caller's session bus through these two
# variables. They hold a socket path owned by the current user, not a
# credential, and they are a dependency of the WRAPPER rather than of the
# sandboxed child — which is why they are restored after the credential scrub
# and then dropped again inside the scope (see :func:`_unset_env_argv`) instead
# of being added to any caller's env allowlist.
_CGROUP_SCOPE_BUS_ENV_KEYS = ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
# Absolute paths only: the shim that drops the locators again must not be
# resolvable through a caller- or agent-influenced PATH.
_ENV_BINARY_CANDIDATES = ("/usr/bin/env", "/bin/env")


def _unset_env_argv(keys: tuple[str, ...]) -> list[str] | None:
    """Return an ``env -u KEY …`` prefix dropping *keys*, or None if impossible.

    ``env`` ``exec``s its target in place (it does not fork), so prepending this
    inside the scope leaves PID tracking, ``killpg`` and descendant scans intact
    — the eventual PID is still the real child.

    Returns None when no ``env`` binary exists at a trusted absolute path, which
    callers must treat as "do not forward the bus locators at all".
    """
    for candidate in _ENV_BINARY_CANDIDATES:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            argv = [candidate]
            for key in keys:
                argv += ["-u", key]
            return argv
    return None


def cgroup_scope_bus_env(env: dict[str, str]) -> tuple[dict[str, str], tuple[str, ...]]:
    """Return ``(env_with_bus_locators, keys_this_call_added)``.

    Only applied when :func:`cgroup_scope_argv` actually wraps the spawn (same
    :func:`_probe_cgroup_scope` gate), so hosts that never see a ``systemd-run``
    prefix keep the exact environment their caller asked for and the returned
    key tuple is empty.

    Values are taken from the gateway's own environment and only fill keys the
    caller left unset — an explicit value in *env* always wins and is NOT
    reported as injected, so a caller that deliberately passes a bus address
    keeps it end to end. The probe requires ``XDG_RUNTIME_DIR`` in
    ``os.environ``, so whenever wrapping is applied at least that locator is
    available to forward.

    The returned key tuple is what lets the caller drop exactly what it added
    again *inside* the scope: the locators must reach ``systemd-run``, but must
    not reach the sandboxed child — a live user-bus address there can be used to
    ask the user systemd manager to start a unit that runs outside the sandbox.

    Without this, a caller that builds its child environment from a strict
    allowlist (``dashboard/handlers/source_providers.py`` is the live example)
    hands ``systemd-run`` an environment with no reachable bus; it exits 1 with
    ``Failed to connect to bus: No medium found`` and the wrapped command never
    runs at all.
    """
    available, _ = _probe_cgroup_scope()
    if not available:
        return env, ()
    patched = dict(env)
    injected: list[str] = []
    for key in _CGROUP_SCOPE_BUS_ENV_KEYS:
        if patched.get(key):
            continue
        value = os.environ.get(key)
        if value:
            patched[key] = value
            injected.append(key)
    return patched, tuple(injected)


# Cached preexec_fn shared by every agent-influenced spawn. Built once from the
# loaded config (limits are process-global, not per-spawn) so the hot path adds
# nothing but a dict lookup. ``_UNSET`` distinguishes "not built yet" from the
# legitimate ``None`` result on non-POSIX platforms.
_UNSET = object()
_RESOURCE_PREEXEC: object = _UNSET


def resource_limit_preexec() -> "Callable[[], None] | None":
    """Return the shared ``preexec_fn`` that caps a spawned child's resources.

    This is the companion to :func:`sandboxed_spawn_argv`: the sandbox wrapper
    gives a child filesystem + credential isolation, and this gives it a
    kernel-enforced ceiling on processes / file descriptors / CPU / memory so a
    fork bomb or runaway allocation in a compromised tool or MCP server cannot
    exhaust the host out from under the gateway. Call sites do not use this
    directly: agent-influenced spawns go through
    :func:`create_subprocess_limited` / :func:`run_limited` /
    :func:`popen_limited`, which deliver the same limits AFTER ``exec`` via the
    spawn shim and fall back to this ``preexec_fn`` only on a host with no
    usable shim (see ``docs/architecture/resource-protection.md``).

    Returns the callable from :func:`kiro_crew.security.apply_resource_limits`,
    or ``None`` on non-POSIX platforms (where there is nothing to enforce and
    ``preexec_fn`` must be ``None``). The callable and the underlying config
    read are computed once and cached — the limits are a host-global policy, not
    a per-spawn decision.
    """
    global _RESOURCE_PREEXEC
    if _RESOURCE_PREEXEC is _UNSET:
        if os.name != "posix":
            # Non-POSIX (Windows): preexec_fn is unsupported by
            # create_subprocess_exec and MUST be None — passing any callable
            # (even a no-op) raises ValueError. Cache None to honor the return
            # contract. (apply_resource_limits also no-ops there, but it returns
            # a callable, so we must not forward it.)
            _RESOURCE_PREEXEC = None
            return None
        # Lazy imports: sandbox is a low-level module (see the SEL import note in
        # wrap_argv) and must not import config/security at module load.
        from kiro_crew.security import apply_resource_limits

        cfg: dict | None = None
        try:
            # Raw config.json (process-cached) — carries the unrecognized
            # ``resource_limits`` key an operator may add; the typed config
            # schema drops unknown keys, so read the raw dict here.
            from kiro_crew.config.loader import _raw_config

            cfg = _raw_config()
        except Exception:
            # Config unavailable (early boot, tests) — apply_resource_limits
            # falls back to its safe built-in defaults.
            logger.debug("resource_limit_preexec: config unavailable, using defaults")
        # POSIX: apply_resource_limits returns a callable (a no-op only when
        # every limit is disabled). Cache it; passing a no-op preexec_fn is fine.
        _RESOURCE_PREEXEC = apply_resource_limits(cfg)
    return _RESOURCE_PREEXEC  # type: ignore[return-value]


_EXTRACTOR_PREEXEC: object = _UNSET


def extractor_resource_limit_preexec() -> "Callable[[], None] | None":
    """Legacy ``preexec_fn`` for :data:`RLIMIT_PROFILE_EXTRACTOR`, shim-less hosts only.

    Same fixed ceiling ``_rlimit_spec`` emits for the profile, applied post-fork
    through :func:`kiro_crew.security.apply_resource_limits`. ``None`` off POSIX,
    where ``preexec_fn`` must not be passed.
    """
    global _EXTRACTOR_PREEXEC
    if _EXTRACTOR_PREEXEC is _UNSET:
        if os.name != "posix":
            _EXTRACTOR_PREEXEC = None
            return None
        from kiro_crew.security import apply_resource_limits

        _EXTRACTOR_PREEXEC = apply_resource_limits(
            {
                "resource_limits": {
                    "max_memory_mb": _EXTRACTOR_MAX_AS_BYTES // (1024 * 1024),
                    "max_cpu_seconds": _EXTRACTOR_MAX_CPU_SECS,
                    "max_open_files": _EXTRACTOR_MAX_NOFILE,
                }
            }
        )
    return _EXTRACTOR_PREEXEC  # type: ignore[return-value]


# Cached ``--rlimits=`` argv fragment for the process-group supervisor. Same
# policy as ``resource_limit_preexec``, delivered post-exec instead of post-fork.
_RESOURCE_SUPERVISOR_ARGV: object = _UNSET


def resource_limit_supervisor_argv() -> "tuple[str, ...]":
    """Return the supervisor's ``--rlimits=`` argv fragment (empty if none apply).

    The alternative to :func:`resource_limit_preexec` for the one spawn that
    already prepends ``_process_group_supervisor.py``. Passing ``preexec_fn=``
    forces CPython to ``fork()`` the multi-GB, ~118-thread gateway and run Python
    in the child before ``exec``; a lock another thread held at fork time is
    unreleasable there, and that is how a child deadlocked in a futex, never
    exec'd, and pinned every fd it had inherited. Handing the limits to the
    supervisor moves the same ``setrlimit`` calls after ``exec``, where the
    process is single-threaded, and the exec'd child inherits them either way.

    The values are policy numbers, not secrets, so argv (world-readable via
    ``ps``) is a fine channel.
    """
    global _RESOURCE_SUPERVISOR_ARGV
    if _RESOURCE_SUPERVISOR_ARGV is _UNSET:
        if os.name != "posix":
            _RESOURCE_SUPERVISOR_ARGV = ()
            return ()
        from kiro_crew.security import resource_limit_spec

        cfg: dict | None = None
        try:
            from kiro_crew.config.loader import _raw_config

            cfg = _raw_config()
        except Exception:
            logger.debug("resource_limit_supervisor_argv: config unavailable, using defaults")
        spec = ",".join(f"{name}:{value}" for name, value in resource_limit_spec(cfg))
        _RESOURCE_SUPERVISOR_ARGV = (f"--rlimits={spec}",) if spec else ()
    return _RESOURCE_SUPERVISOR_ARGV  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Session host preexec — the inverse of resource_limit_preexec.
# ---------------------------------------------------------------------------

_SESSION_HOST_PREEXEC: object = _UNSET


def session_host_preexec() -> "Callable[[], None] | None":
    """Return a ``preexec_fn`` that *raises* NOFILE for a session host process.

    Session hosts (kiro-cli-chat / claude-agent-acp) are **trusted** internal
    processes — they manage a tree of MCP server subprocesses, each consuming
    pipe fd pairs for stdin/stdout communication.  A single session host may
    hold 100-200 fds under normal operation (10+ MCP servers × pipe pairs +
    sockets + log files).

    The default ``resource_limit_preexec()`` caps NOFILE at 1024 to defend
    against compromised *tool* processes, but applying the same cap to the
    trusted session host causes "Too many open files" crashes when subagent
    concurrency or MCP server count is high.

    This preexec raises NOFILE soft+hard to the *gateway's* inherited hard
    limit (typically 10240 from the systemd unit, or 524288 kernel max) so
    the session host has headroom proportional to the gateway itself.  Other
    resource limits (NPROC, CPU, AS) are left inherited (this preexec touches NOFILE only) — a
    session host has no legitimate reason to fork-bomb or allocate unbounded
    memory.

    Returns ``None`` on non-POSIX platforms (preexec_fn must be None there).
    """
    global _SESSION_HOST_PREEXEC
    if _SESSION_HOST_PREEXEC is _UNSET:
        if os.name != "posix" or _resource_mod is None:
            _SESSION_HOST_PREEXEC = None
            return None

        res = _resource_mod

        def _raise_nofile() -> None:
            """Raise NOFILE to the hard limit in the child process."""
            try:
                _soft, hard = res.getrlimit(res.RLIMIT_NOFILE)
                if hard == res.RLIM_INFINITY:
                    # Kernel allows unlimited — cap at a sane maximum but never
                    # reduce below the inherited soft limit.
                    target = max(_soft, 65536)
                else:
                    target = hard
                res.setrlimit(res.RLIMIT_NOFILE, (target, hard))
            except (ValueError, OSError):
                pass  # Leave inherited — better than failing the spawn.

        _SESSION_HOST_PREEXEC = _raise_nofile
    return _SESSION_HOST_PREEXEC  # type: ignore[return-value]


# Build workloads (vite/npm/pip) legitimately hold thousands of descriptors —
# the default 1024 NOFILE ceiling EMFILEs them while still being the right cap
# for one-shot tools. Same policy, higher finite descriptor ceiling; every
# other limit still comes from the operator config. Cached like the default.
_BUILD_NOFILE_CEILING = 65536
_BUILD_RESOURCE_PREEXEC: object = _UNSET


def build_resource_limit_preexec() -> "Callable[[], None] | None":
    """``resource_limit_preexec`` variant for build-class children.

    Identical policy except ``max_open_files`` is raised to a still-finite
    65536 (matching the gateway service's own ``LimitNOFILE``); an operator
    ``resource_limits.max_open_files`` override higher than the default wins.
    """
    global _BUILD_RESOURCE_PREEXEC
    if _BUILD_RESOURCE_PREEXEC is _UNSET:
        if os.name != "posix":
            _BUILD_RESOURCE_PREEXEC = None
            return None
        from kiro_crew.security import apply_resource_limits

        cfg: dict | None = None
        try:
            from kiro_crew.config.loader import _raw_config

            cfg = dict(_raw_config() or {})
        except Exception:
            cfg = {}
        raw_limits = (cfg or {}).get("resource_limits")
        limits = dict(raw_limits) if isinstance(raw_limits, dict) else {}
        # Malformed operator values must not break the spawn — the shared parse
        # ignores anything out of domain and returns None, which floors to the
        # build ceiling here. Going through it keeps this path from being a
        # second rule for a key security.resource_limit_spec also reads.
        from kiro_crew.config.loader import ResourceLimitsConfig

        configured = ResourceLimitsConfig.from_raw(raw_limits).max_open_files or 0
        limits["max_open_files"] = max(configured, _BUILD_NOFILE_CEILING)
        _BUILD_RESOURCE_PREEXEC = apply_resource_limits({**(cfg or {}), "resource_limits": limits})
    return _BUILD_RESOURCE_PREEXEC  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Post-exec resource limits (the replacement for ``preexec_fn=``)
# ---------------------------------------------------------------------------

_SPAWN_SHIM_SOURCE = str(Path(__file__).with_name("_spawn_exec_shim.py"))
try:
    # Captured once, at gateway import, and passed to the interpreter as a ``-c``
    # source string. Loading it from the package path at SPAWN time would let a
    # same-UID agent rewrite the file between capture and use.
    _SPAWN_SHIM_CODE = Path(_SPAWN_SHIM_SOURCE).read_text(encoding="utf-8")
except OSError:  # pragma: no cover - only if the install is truncated
    _SPAWN_SHIM_CODE = ""

# Resource-limit profiles. ``tool`` is the default for every agent-influenced
# spawn; the others exist because a single policy cannot serve a one-shot tool, a
# build, a session host, and the user's own terminal at once. Each one is a
# faithful port of the ``preexec_fn`` variant it replaces -- including which ones
# bias the OOM killer, which is not uniform across them.
RLIMIT_PROFILE_TOOL = "tool"
RLIMIT_PROFILE_BUILD = "build"
RLIMIT_PROFILE_SESSION_HOST = "session_host"
# A first-party document parser fed untrusted bytes (``pdf_extract_child``). Its
# ceiling is FIXED, not read from ``resource_limits``: the ``tool`` profile only
# applies RLIMIT_AS when an operator sets ``max_memory_mb`` (default 0), and a
# parser whose allocation precedes any length check needs a memory bound that
# is on by default. RLIMIT_AS caps VIRTUAL address space, which is why ``tool``
# leaves it opt-in (Node/V8 reserves far more than it touches); this child is
# pure CPython plus ``pdfplumber``, measured at ~270 MB VmPeak on a one-page
# document, so 1 GiB is headroom for a large document and a hard stop for a
# Flate bomb. RLIMIT_CPU ends a parse that never finishes; NOFILE matches the
# ``tool`` default. Biases the OOM killer like ``tool``: this is the process to
# lose.
RLIMIT_PROFILE_EXTRACTOR = "extractor"
_EXTRACTOR_MAX_AS_BYTES = 1024 * 1024 * 1024
_EXTRACTOR_MAX_CPU_SECS = 60
_EXTRACTOR_MAX_NOFILE = 1024
# No limits and no OOM bias: the interactive terminal is the user's own shell,
# not agent-executed code, and never carried either.
RLIMIT_PROFILE_NONE = "none"

# profile -> (biases the OOM killer, legacy preexec accessor name)
_PROFILE_OOM_BIAS = {
    RLIMIT_PROFILE_TOOL: True,
    RLIMIT_PROFILE_BUILD: True,
    RLIMIT_PROFILE_EXTRACTOR: True,
    # session_host_preexec raises NOFILE and does nothing else -- notably it does
    # NOT bias the OOM score, and a trusted session host should not be the
    # preferred kill target.
    RLIMIT_PROFILE_SESSION_HOST: False,
    RLIMIT_PROFILE_NONE: False,
}

# The shim's own argv contract, mirrored here so a cached prefix can be extended
# for one spawn. Kept as literals rather than imported from the shim module: the
# shim is consumed as a source string captured at import time, never imported from
# the (agent-writable) package directory at spawn time.
_SHIM_ARGV_SEPARATOR = "--"
_SHIM_CHDIR_FD_FLAG = "--chdir-fd="
_SHIM_CTTY_FD_FLAG = "--ctty-fd="

_SHIM_ARGV_CACHE: dict[str, tuple[str, ...]] = {}
_SHIM_UNAVAILABLE_LOGGED = False


def _rlimit_spec(profile: str) -> str:
    """Build the shim's ``--rlimits=`` payload for *profile*.

    The values are policy numbers, not secrets, so argv (world-readable through
    ``ps``) is a fine channel for them.
    """
    from kiro_crew.security import resource_limit_spec

    if profile == RLIMIT_PROFILE_NONE:
        return ""
    if profile == RLIMIT_PROFILE_SESSION_HOST:
        # Faithful port of session_host_preexec: it touches NOFILE only, and
        # deliberately leaves NPROC/CPU/AS inherited. A session host multiplexes
        # pipe pairs for a whole tree of MCP servers, and the tool-grade 1024 cap
        # EMFILE-crashed it.
        return "RLIMIT_NOFILE:hard"
    if profile == RLIMIT_PROFILE_EXTRACTOR:
        # Fixed policy, independent of ``resource_limits``: see the constants.
        return (
            f"RLIMIT_AS:{_EXTRACTOR_MAX_AS_BYTES},"
            f"RLIMIT_CPU:{_EXTRACTOR_MAX_CPU_SECS},"
            f"RLIMIT_NOFILE:{_EXTRACTOR_MAX_NOFILE}"
        )

    cfg: dict | None = None
    try:
        from kiro_crew.config.loader import _raw_config

        cfg = _raw_config()
    except Exception:
        logger.debug("_rlimit_spec: config unavailable, using defaults")
    if profile == RLIMIT_PROFILE_BUILD:
        raw_limits = (cfg or {}).get("resource_limits")
        limits = dict(raw_limits) if isinstance(raw_limits, dict) else {}
        # Same shared parse as the post-fork build path above, so the two
        # spellings of "raise the build NOFILE floor" cannot drift apart.
        from kiro_crew.config.loader import ResourceLimitsConfig

        configured = ResourceLimitsConfig.from_raw(raw_limits).max_open_files or 0
        limits["max_open_files"] = max(configured, _BUILD_NOFILE_CEILING)
        cfg = {**(cfg or {}), "resource_limits": limits}
    return ",".join(f"{name}:{value}" for name, value in resource_limit_spec(cfg))


def spawn_shim_argv(
    profile: str = RLIMIT_PROFILE_TOOL, *, ctty_fd: int | None = None
) -> tuple[str, ...]:
    """Return the argv prefix that applies *profile*'s policy AFTER ``exec``.

    Prepend it to a command and pass ``preexec_fn=None``; the shim replaces
    itself with the command, so PID, process group, inherited fds, and exit
    status all stay the command's own. See ``_spawn_exec_shim.py`` for why this
    cannot ride on ``preexec_fn``: that forks this multi-threaded gateway and runs
    Python in the child, where a wedged child blocks the spawning thread inside
    ``Popen`` and pins every fd it inherited.

    *ctty_fd* asks the shim to make the terminal on that inherited descriptor the
    child's controlling terminal, which is what lets Ctrl+C reach an interactive
    shell. It is a spawn-scoped request rather than part of a profile: the
    descriptor belongs to one PTY, and the same profile serves spawns with no
    terminal at all. Passing it also redirects the child's stdin, stdout and
    stderr onto that descriptor.

    Returns an empty tuple when there is nothing for a shim to do -- on Windows
    (no POSIX rlimits), for a profile that asks for nothing and no *ctty_fd*, or
    if the shim source could not be captured. An empty result on a profile that
    DOES carry policy means the caller must fall back to ``preexec_fn`` rather
    than drop it. A caller that asked for *ctty_fd* must NOT fall back that way:
    reintroducing the fork is the defect the request exists to avoid, so it spawns
    without the shim and accepts a shell with no controlling terminal.
    """
    global _SHIM_UNAVAILABLE_LOGGED
    if os.name != "posix":
        return ()
    key = profile if ctty_fd is None else f"{profile}|ctty={ctty_fd}"
    cached = _SHIM_ARGV_CACHE.get(key)
    if cached is not None:
        return cached
    if not _SPAWN_SHIM_CODE or not sys.executable:
        if not _SHIM_UNAVAILABLE_LOGGED:
            _SHIM_UNAVAILABLE_LOGGED = True
            logger.warning(
                "post-exec spawn shim unavailable (source_captured=%s, executable=%r); "
                "falling back to preexec_fn",
                bool(_SPAWN_SHIM_CODE),
                sys.executable,
            )
        return ()
    spec = _rlimit_spec(profile)
    bias = _PROFILE_OOM_BIAS.get(profile, True)
    if not spec and not bias and ctty_fd is None:
        # Nothing to do post-exec: skip the interpreter hop entirely rather than
        # pay ~10ms to exec a shim that would only exec again.
        _SHIM_ARGV_CACHE[key] = ()
        return ()
    argv = [sys.executable, "-I", "-S", "-c", _SPAWN_SHIM_CODE]
    if spec:
        argv.append(f"--rlimits={spec}")
    if bias:
        argv.append("--oom-bias")
    if ctty_fd is not None:
        argv.append(f"{_SHIM_CTTY_FD_FLAG}{ctty_fd}")
    argv.append(_SHIM_ARGV_SEPARATOR)
    resolved = tuple(argv)
    _SHIM_ARGV_CACHE[key] = resolved
    return resolved


def _shim_prefix_entering_fd(prefix: "tuple[str, ...]", descriptor: int) -> "tuple[str, ...]":
    """Return *prefix* with ``--chdir-fd`` inserted ahead of its argv separator.

    Copied rather than mutated: the prefix is cached per profile, while the
    descriptor belongs to a single spawn.
    """
    if not prefix or prefix[-1] != _SHIM_ARGV_SEPARATOR:
        raise RuntimeError("spawn shim prefix is missing its argv separator")
    return prefix[:-1] + (f"{_SHIM_CHDIR_FD_FLAG}{descriptor}", _SHIM_ARGV_SEPARATOR)


def _pass_fds_including(passed: Any, descriptor: int) -> "tuple[int, ...]":
    """Return *passed* with *descriptor* inherited, leaving its order alone.

    The shim can only ``fchdir`` a descriptor the child actually holds, and
    ``pass_fds`` is what carries it there: it exempts the fd from
    ``_close_open_fds`` and clears the ``O_CLOEXEC`` the binder opens with. Owned
    here rather than left to each caller so the flag and the inheritance cannot
    drift apart.
    """
    existing = tuple(passed or ())
    if descriptor in existing:
        return existing
    return existing + (descriptor,)


def _preexec_for_profile(profile: str) -> "Callable[[], None] | None":
    """Legacy ``preexec_fn`` for *profile*, used only when the shim is missing."""
    if profile == RLIMIT_PROFILE_NONE:
        return None
    if profile == RLIMIT_PROFILE_SESSION_HOST:
        return session_host_preexec()
    if profile == RLIMIT_PROFILE_BUILD:
        return build_resource_limit_preexec()
    if profile == RLIMIT_PROFILE_EXTRACTOR:
        return extractor_resource_limit_preexec()
    return resource_limit_preexec()


def _resolve_spawn_target(
    argv: "Sequence[str]", env: "Mapping[str, str] | None", cwd: Any = None
) -> str:
    """Resolve a bare command NAME against the child's ``PATH``.

    The shim ``execv``s without a PATH search, so a command given as a bare name
    has to be resolved by someone. It is resolved HERE, in the gateway, on
    purpose: the call sites that vet an executable (provider allowlists, binary
    trust checks) do it in the parent, and letting the shim run its own search
    would add a second resolution path that nothing vetted.

    Resolving here also keeps the contract call sites already depend on -- a
    command that is not on ``PATH`` raises ``FileNotFoundError`` from the spawn
    itself, exactly as ``Popen`` raised it when the child's ``execvpe`` failed.

    This touches the filesystem (``shutil.which`` stats each ``PATH`` entry), so
    the caller runs it in a worker thread: a stalled NFS/autofs ``PATH`` entry
    would otherwise block the event loop, which the child-side search never did.

    ``PATH`` comes from the child's own environment, matching ``Popen``'s
    ``os.get_exec_path(env)``. A relative ``PATH`` entry is resolved against the
    child's *cwd* for the same reason: that is where ``execvpe`` would have looked
    from, not where the gateway happens to be running.
    """
    name = argv[0]
    if os.sep in name or (os.altsep and os.altsep in name):
        # An explicit path: exec resolves it (against cwd when relative), and
        # stat-ing it here would only pre-empt a failure exec reports anyway.
        return name
    search_path = (env or os.environ).get("PATH") or os.defpath
    if cwd:
        base = os.fspath(cwd)
        search_path = os.pathsep.join(
            entry if os.path.isabs(entry) else os.path.join(base, entry)
            for entry in search_path.split(os.pathsep)
        )
    found = shutil.which(name, path=search_path)
    if not found:
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), name)
    return found


def _pinned_spawn_path(
    env: "Mapping[str, str] | None", *, chdir_fd: int | None = None
) -> "dict[str, str]":
    """A copy of *env* whose ``PATH`` keeps only entries safe under a pinned cwd.

    For resolving a command when the child's working directory is pinned by
    descriptor. Two screens, cheapest first:

    * **Lexical** -- only absolute entries survive. A relative entry (``''``,
      ``.``, ``tools``) is resolved against the pinned directory, which is the
      one place the pin says not to trust by name.
    * **Identity** (when *chdir_fd* is given) -- an absolute entry that IS the
      pinned directory, or lives anywhere beneath it, is dropped too.
      ``PATH=/home/me/.kiro/crew/workspace/bin:/usr/bin`` passes the lexical
      screen unchanged, yet a binary planted behind such an entry wins the
      child's own later lookup the moment the shim has entered the pinned
      directory. Entries are compared by ``(st_dev, st_ino)`` ancestry walked
      over descriptors -- never by pathname -- so a symlink or other alias of
      the pinned directory cannot dodge the screen. A kept entry is emitted as
      the OPENED descriptor's own canonical path, never the caller's spelling:
      the child re-resolves its ``PATH`` strings later, so a spelling that
      traverses a retargetable symlink could be pointed somewhere else between
      this screen and that lookup. An entry that cannot be opened, walked, or
      re-spelled is dropped, fail-closed per entry: an unopenable entry cannot
      contribute a resolvable binary today, and dropping is the direction that
      cannot be gamed by making a directory un-``stat``-able.

    When the BOUND descriptor's own identity cannot be read there is nothing to
    compare entries against, so the lexical screen stands alone for that spawn.
    That is a deliberate degrade, not a silent fallback: in production
    ``chdir_fd`` always originates from a real opened directory descriptor, and
    one that cannot be ``fstat``-ed is one the shim's own ``fchdir`` rejects
    before any command runs.

    Dropping entries can leave ``PATH`` empty, and that is the intended outcome
    -- the resolve then raises ``FileNotFoundError`` exactly as an unresolvable
    command already did, rather than silently searching somewhere else.
    """
    source = dict(env if env is not None else os.environ)
    raw = source.get("PATH") or os.defpath
    entries = [entry for entry in raw.split(os.pathsep) if entry and os.path.isabs(entry)]
    bound_identity: "tuple[int, int] | None" = None
    if chdir_fd is not None:
        try:
            bound_info = os.fstat(chdir_fd)
        except OSError:
            bound_identity = None
        else:
            bound_identity = (bound_info.st_dev, bound_info.st_ino)
    if bound_identity is not None:
        # Local import: hooks imports sandbox at call time, so a module-level
        # dependency would be circular. `_fd_real_path` is private but already
        # borrowed this way by `bound_agent_workspace_target` above.
        from kiro_crew.hooks import _fd_real_path

        screened: list[str] = []
        for entry in entries:
            try:
                entry_fd = _open_directory_descriptor(entry)
            except OSError:
                continue
            try:
                ancestors = _directory_ancestor_identities(entry_fd)
                if bound_identity in ancestors:
                    # The walk yields the entry's OWN identity first, so one
                    # membership test covers both "the entry IS the pinned
                    # directory" and "the entry lives beneath it".
                    continue
                # Keep the OPENED descriptor's own canonical path, never the
                # caller's spelling. The child re-resolves whatever string ends
                # up in its PATH, so a kept spelling that traverses a symlink
                # could be retargeted between this screen and that lookup --
                # the identity verified here must be the identity the child
                # reaches. A canonical path has no symlink components, and one
                # inside the pinned directory cannot exist here (its target
                # would have failed the ancestry test above). Unresolvable ==
                # dropped: falling back to the mutable spelling would reopen
                # the window this screen exists to close.
                resolved_entry = _fd_real_path(entry_fd)
            except OSError:
                continue
            finally:
                os.close(entry_fd)
            if resolved_entry is None:
                continue
            screened.append(resolved_entry)
        entries = screened
    source["PATH"] = os.pathsep.join(entries)
    return source


def _needs_path_search(argv: "Sequence[str]") -> bool:
    """Whether ``argv[0]`` is a bare name, i.e. whether resolution touches disk."""
    name = argv[0]
    return not (os.sep in name or (os.altsep and os.altsep in name))


async def create_subprocess_limited(
    *argv: str,
    profile: str = RLIMIT_PROFILE_TOOL,
    chdir_fd: int | None = None,
    windows_cleanup_owner: platform_compat._PendingWindowsTreeCleanup | None = None,
    **kwargs: Any,
) -> asyncio.subprocess.Process:
    """``asyncio.create_subprocess_exec`` with resource limits applied post-exec.

    The drop-in replacement for ``create_subprocess_exec(..., preexec_fn=
    resource_limit_preexec())``. Every keyword argument is forwarded untouched
    except ``preexec_fn``, which this owns: passing one would reintroduce the fork
    hazard the shim exists to remove, so it is refused.

    The returned ``Process`` describes the command itself, not a wrapper -- the
    shim ``exec``s in place -- so ``pid``, ``returncode``, signal delivery, and
    ``platform_compat.kill_process_tree`` all behave as they did before.

    ``chdir_fd`` pins the child's working directory to a directory IDENTITY
    rather than to a name: the descriptor is inherited, the shim ``fchdir``s into
    it and closes it, and only then is the command exec'd. Callers pass it when a
    pathname re-resolved in the child could be retargeted between the check and
    the chdir. It is deliberately not spelled ``cwd="/dev/fd/<n>"`` -- that is a
    Linux-only trick, and macOS refuses ``chdir()`` on those entries (``EACCES`` or
    ``ENOTDIR`` depending on the OS version). It needs the shim, and is refused rather than quietly downgraded
    to ``cwd``'s pathname when the shim is missing: entering a name nobody
    re-verified would reopen the window the descriptor exists to close.

    Setting it also DROPS ``cwd`` from the spawn, since ``Popen`` would otherwise
    chdir that pathname in the fork child before the shim ever runs, and screens
    ``PATH`` by directory IDENTITY -- for the search that resolves a bare command
    name here AND for the child's own environment. Relative entries are dropped
    (``execvpe`` resolved them against the child's cwd, the directory this
    descriptor exists to distrust), and so is any absolute entry that is the
    pinned directory itself or lives beneath it, compared by ``(st_dev, st_ino)``
    ancestry rather than by pathname. Resolving ``argv[0]`` is not the last
    lookup that happens: the wrapper this spawns looks its own target up on
    ``PATH`` after the shim has entered that directory. ``PATH=.:/usr/bin`` --
    or the same directory spelled absolutely -- would otherwise exec a binary
    out of the agent's own workspace, ahead of the sandbox meant to contain it.

    A spawn landing while the launcher interpreter's install tree is being
    rebuilt is retried, on the same budget and by the same discriminator as
    :func:`popen_limited` -- this wrapper puts the same ``sys.executable`` at
    the head of the spawned argv, so it was exposed to the identical blip. The
    backoff uses ``asyncio.sleep``: a blocking sleep would freeze the event loop
    for up to ~3.75s, which is precisely the hazard this wrapper exists to
    avoid. Only the shim-prefixed spawn is retried -- the no-shim fallback below
    execs the caller's own ``argv[0]``, so an ENOENT there is the caller's own
    missing binary and must still surface on the first attempt.

    There is deliberately no ``abort_retry`` hook here, unlike
    :func:`popen_limited`: no caller mediates this wrapper's cancellation
    through a registry keyed on the live child, so the lost-cancel window that
    hook exists to close has no consumer. Add one when a caller needs it.
    """
    if "preexec_fn" in kwargs:
        raise TypeError(
            "create_subprocess_limited owns preexec_fn: limits are applied "
            "post-exec by the spawn shim, not post-fork"
        )
    if not argv:
        raise ValueError("create_subprocess_limited requires a command")
    prefix = spawn_shim_argv(profile)
    if not prefix:
        if chdir_fd is not None:
            raise RuntimeError(
                "a descriptor-pinned working directory requires the post-exec "
                "spawn shim; refusing to enter an unverified pathname instead"
            )
        # No shim (Windows, a no-op profile, or a truncated install): keep
        # whatever policy the profile carries on the legacy fork path. Dropping
        # the caps silently would be worse than the fork hazard.
        if platform_compat.IS_WINDOWS and windows_cleanup_owner is not None:
            return await platform_compat._create_windows_subprocess_owned(
                windows_cleanup_owner, *argv, preexec_fn=_preexec_for_profile(profile), **kwargs
            )
        return await asyncio.create_subprocess_exec(
            *argv, preexec_fn=_preexec_for_profile(profile), **kwargs
        )
    search_cwd = kwargs.get("cwd")
    search_env = kwargs.get("env")
    if chdir_fd is not None:
        prefix = _shim_prefix_entering_fd(prefix, chdir_fd)
        kwargs["pass_fds"] = _pass_fds_including(kwargs.get("pass_fds"), chdir_fd)
        # THE INVARIANT: while the cwd is pinned by descriptor, NO resolution of a
        # program name -- not the one below, and not one the child performs later --
        # may consult a relative PATH entry, the pinned directory, or anything
        # inside it. Three things enforce it together, and each was a hole on its
        # own:
        #
        # (a) `cwd` leaves the spawn. ``Popen`` chdirs it in the fork child BEFORE it
        #     execs the shim, so leaving it in place would resolve the very pathname
        #     the descriptor exists to bypass -- and fail the spawn outright
        #     (EACCES/ENOENT/ENOTDIR) if that name was removed or retargeted since the
        #     bind, with the pinned descriptor never reached.
        # (b) The search below gets no cwd and a PATH screened by directory IDENTITY:
        #     relative entries are dropped, and so is any absolute entry that IS the
        #     pinned directory or lives beneath it -- compared by (st_dev, st_ino)
        #     ancestry, so an alias cannot dodge it; kept entries are re-spelled from
        #     the verified descriptor, so a retargetable symlink in the caller's
        #     spelling cannot redirect the child's later lookup. A bare name IS the
        #     normal shape here -- the macOS sandbox wrapper hands back "env" as
        #     argv[0] and the Linux cgroup wrapper hands back "systemd-run" -- so the
        #     search cannot simply be refused, and `execvpe` resolved a relative entry
        #     against the child's cwd, i.e. the pinned workspace. An absolute entry
        #     pointing INTO that workspace reaches the same binary by a different
        #     spelling.
        # (c) The CHILD gets that same screened PATH. Resolving argv[0] here is
        #     not the last resolution that happens: `env` looks `sandbox-exec` up on
        #     PATH itself, inside the child, after the shim has already entered the
        #     workspace. Narrowing only (b) left `PATH=.:/usr/bin` exec'ing a
        #     `sandbox-exec` the agent dropped in its own workspace -- ahead of the
        #     sandbox that was supposed to contain it. One sanitized PATH, used for
        #     both, is what makes the invariant hold rather than move down a level.
        #
        # Those two wrapper names are spelled in prose on purpose: test_spawn_audit
        # matches its routed-through-the-sandbox tokens against this function's raw
        # source, comments included, so writing either identifier here would make the
        # spawn chokepoint read as if it routed on its own behalf.
        kwargs.pop("cwd", None)
        pinned_env = search_env

        def _screened_spawn_plan() -> "tuple[dict[str, str], str]":
            # One worker-thread hop covers the identity screen AND the resolve:
            # the screen opens and walks PATH entries and the resolve stats
            # them, so a stalled NFS/autofs entry would block either one, and
            # neither may freeze the event loop. Returning the screened env
            # alongside the resolved target keeps clauses (b) and (c) fed from
            # the SAME value by construction.
            screened = _pinned_spawn_path(pinned_env, chdir_fd=chdir_fd)
            if _needs_path_search(argv):
                return screened, _resolve_spawn_target(argv, screened, None)
            return screened, argv[0]

        search_env, resolved = await asyncio.to_thread(_screened_spawn_plan)
        kwargs["env"] = search_env
    elif not _needs_path_search(argv):
        # Explicit path: nothing to resolve, so no filesystem access and no
        # thread hop -- exec does the work.
        resolved = argv[0]
    else:
        # A PATH search stats every entry, so it runs off the loop. One stalled
        # NFS/autofs entry would otherwise freeze the gateway.
        resolved = await asyncio.to_thread(_resolve_spawn_target, argv, search_env, search_cwd)
    for delay in _INTERPRETER_ENOENT_DELAYS:
        try:
            return await asyncio.create_subprocess_exec(
                *prefix, resolved, *argv[1:], preexec_fn=None, **kwargs
            )
        except FileNotFoundError as exc:
            # ``prefix`` IS the head of the argv actually spawned, and prefix[0]
            # is this process's own sys.executable -- the only shape the
            # discriminator accepts.
            if not _retry_interpreter_enoent(exc, prefix, delay):
                raise
            # asyncio.sleep, NOT time.sleep: a blocking sleep here would freeze
            # the event loop for up to ~3.75s, which is the hazard this wrapper
            # exists to avoid.
            await asyncio.sleep(delay)
    # Budget spent. Deliberately unguarded, exactly as in popen_limited: a
    # genuinely broken install reports the error it reports today, ~4s later.
    return await asyncio.create_subprocess_exec(
        *prefix, resolved, *argv[1:], preexec_fn=None, **kwargs
    )


def _prepare_limited_spawn(
    argv: "Sequence[str]", profile: str, kwargs: "dict[str, Any]", caller: str
) -> "tuple[list[str], Callable[[], None] | None]":
    """Resolve *argv* into the command to spawn plus the ``preexec_fn`` to pass.

    Shared by :func:`run_limited` and :func:`popen_limited`, which differ only in
    which ``subprocess`` entry point they hand the result to.

    Two things make this the sync twin of :func:`create_subprocess_limited`
    rather than a copy of it:

    * The PATH search runs INLINE. The async wrapper hops to a worker thread
      because ``shutil.which`` stats every ``PATH`` entry and one stalled
      NFS/autofs mount would freeze the event loop. A synchronous caller is
      already off the loop, so the hop would buy nothing and cost a thread.
    * ``shell=True`` is refused. The shim ``exec``s an argv vector, so there is
      no correct place to put a prefix in front of a shell command STRING;
      wrapping it anyway would change what the shell parses.
    """
    if "preexec_fn" in kwargs:
        raise TypeError(
            f"{caller} owns preexec_fn: limits are applied post-exec by the "
            "spawn shim, not post-fork"
        )
    if kwargs.get("shell"):
        raise TypeError(
            f"{caller} cannot wrap shell=True: the shim prefixes an argv "
            "vector, and a shell command is a single string"
        )
    if not argv:
        raise ValueError(f"{caller} requires a command")
    prefix = spawn_shim_argv(profile)
    if not prefix:
        # No shim (Windows, a no-op profile, or a truncated install): keep
        # whatever policy the profile carries on the legacy fork path. Dropping
        # the caps silently would be worse than the fork hazard.
        return list(argv), _preexec_for_profile(profile)
    if not _needs_path_search(argv):
        # Explicit path: exec resolves it, so stat-ing it here would only
        # pre-empt a failure exec reports anyway.
        resolved = argv[0]
    else:
        resolved = _resolve_spawn_target(argv, kwargs.get("env"), kwargs.get("cwd"))
    return [*prefix, resolved, *argv[1:]], None


def run_limited(
    argv: "Sequence[str]",
    *,
    profile: str = RLIMIT_PROFILE_TOOL,
    **kwargs: "Any",
) -> "subprocess.CompletedProcess[Any]":
    """``subprocess.run`` with resource limits applied AFTER ``exec``.

    The synchronous counterpart of :func:`create_subprocess_limited`, and the
    drop-in replacement for ``subprocess.run(..., preexec_fn=
    resource_limit_preexec())``. Every keyword argument is forwarded untouched
    except ``preexec_fn``, which this owns.

    A synchronous spawn wedges the calling worker thread rather than the event
    loop, so it does not take the whole gateway down the way the async hazard
    did -- but it is the same ``fork()`` of the same multi-GB, ~118-thread
    process, and the child still inherits a duplicate of every open fd until it
    ``exec``s. Taking ``preexec_fn`` out of the picture removes both.

    ``CompletedProcess.args`` and the ``cmd`` of a ``CalledProcessError`` /
    ``TimeoutExpired`` are the command's own argv, not the shim's. That is
    maintained here rather than free: the shim source rides in argv as a ~8 KB
    ``-c`` string, and both exceptions render ``cmd`` into their message, so
    reporting the spawned argv would put the whole shim in every failure log
    line.

    A spawn landing while the launcher interpreter's install tree is being
    rebuilt is retried, on the same budget and by the same discriminator as
    :func:`popen_limited` -- this wrapper puts the same ``sys.executable`` at
    ``cmd[0]``, so it was exposed to the identical blip. The retry sits INSIDE
    the ``cmd``-rewriting handler so a ``CalledProcessError`` or
    ``TimeoutExpired`` still reports the caller's own argv.

    There is deliberately no ``abort_retry`` hook here, unlike
    :func:`popen_limited`: this wrapper never hands back a handle, so no
    cancellation registry can be keyed on the child and the lost-cancel hazard
    that hook exists to close cannot arise. Add one when a caller needs it.
    """
    cmd, preexec = _prepare_limited_spawn(argv, profile, kwargs, "run_limited")
    reported = list(argv)
    try:
        for delay in _INTERPRETER_ENOENT_DELAYS:
            try:
                result = subprocess.run(cmd, preexec_fn=preexec, **kwargs)
                break
            except FileNotFoundError as exc:
                if not _retry_interpreter_enoent(exc, cmd, delay):
                    raise
                time.sleep(delay)
        else:
            # Budget spent. Deliberately unguarded, exactly as in
            # popen_limited: whatever this raises reaches the caller unchanged,
            # so a genuinely broken install still reports the error it reports
            # today -- just ~4s later.
            result = subprocess.run(cmd, preexec_fn=preexec, **kwargs)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        exc.cmd = reported
        raise
    result.args = reported
    return result


#: Backoff between spawn attempts when the launcher interpreter is transiently
#: absent. Five attempts, ~3.75s of waiting in total.
#:
#: ``sys.executable`` is often a symlink into a managed install tree, and
#: rebuilding that tree DELETES and re-creates its entries -- including the
#: interpreter :func:`wrap_argv` prepends to EVERY sandboxed argv. The tree is
#: whole again in about a second, so a spawn landing inside that window dies with
#: ENOENT on an interpreter that both existed before it and exists after it. The
#: caller cannot tell that apart from a broken install: a cron records a hard
#: failure (and counts a strike toward auto-pause) for a condition that already
#: healed itself, and several crons sharing one tick fail together. Any packaging
#: that relinks an interpreter in place reaches this -- an environment rebuild, a
#: toolchain reinstall, a swapped container layer.
_INTERPRETER_ENOENT_DELAYS: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0)


def _is_transient_interpreter_enoent(exc: OSError, cmd: "Sequence[str]") -> bool:
    """True when ``exc`` is ENOENT for the interpreter WE prepended to ``cmd``.

    Deliberately narrow, because ENOENT from ``Popen`` is ambiguous: it is raised
    for a missing ``cwd`` and for a missing program alike, and a genuinely absent
    user binary MUST still fail on the first attempt rather than after a delay.
    So the only retryable shape is an ENOENT whose ``filename`` IS ``cmd[0]`` and
    whose ``cmd[0]`` is this process's own ``sys.executable``. A missing-``cwd``
    ENOENT names the directory and a missing user binary names that binary, so
    both fall through untouched.

    ``filename`` being populated is not guaranteed, so when it is absent fall
    back to a live check that the interpreter really is gone from disk -- an
    observation, rather than an assumption that this ENOENT must be ours.
    """
    if not cmd or cmd[0] != sys.executable:
        return False
    if exc.filename is not None:
        return exc.filename == cmd[0]
    return not os.path.exists(sys.executable)


def _retry_interpreter_enoent(exc: OSError, cmd: "Sequence[str]", delay: float) -> bool:
    """Decide whether *exc* is a retryable interpreter blip, and log it if so.

    The single shared implementation behind all three spawn wrappers
    (:func:`run_limited`, :func:`popen_limited`,
    :func:`create_subprocess_limited`), which all put ``sys.executable`` at
    ``cmd[0]`` via :func:`spawn_shim_argv` and are therefore exposed to the same
    transient ENOENT. Returning ``False`` means the caller must re-raise
    untouched.

    What is deliberately NOT shared is the spawn itself, and the WAIT. The spawn
    stays lexically inside each wrapper because both spawn audits key on
    ``<relpath>::<enclosing function>``: hoisting any of the three into a common
    helper would migrate its audit key, stranding the existing ``_SYNC_ALLOWED``
    and ``BENIGN_SPAWNS`` entries as stale while the relocated call read as a
    brand-new unrouted spawn. The wait is per-flavour because the async wrapper
    must ``await asyncio.sleep`` -- a ``time.sleep`` there would block the event
    loop for up to ~3.75s, which is the very hazard the async wrapper exists to
    avoid. So each caller keeps its own two lines (wait, then re-check abort) and
    shares the DECISION, which is where the subtlety actually lives.
    """
    if not _is_transient_interpreter_enoent(exc, cmd):
        return False
    # Log every retry: a silently-absorbed spawn failure would hide an install
    # tree that has genuinely stopped converging.
    logger.warning(
        "sandbox launcher interpreter %r is absent; retrying spawn in "
        "%.2fs (its install tree is probably mid-rebuild)",
        sys.executable,
        delay,
    )
    return True


def popen_limited(
    argv: "Sequence[str]",
    *,
    profile: str = RLIMIT_PROFILE_TOOL,
    abort_retry: "Callable[[], bool] | None" = None,
    **kwargs: "Any",
) -> "subprocess.Popen[Any]":
    """``subprocess.Popen`` with resource limits applied AFTER ``exec``.

    Same contract as :func:`run_limited`, for callers that need the handle
    rather than the result -- a long-running child they will ``communicate()``
    with, poll, or signal later.

    The returned ``Popen`` is the command's own process, not a wrapper's, so
    ``pid``, ``returncode``, signal delivery, and
    ``platform_compat.kill_process_tree`` behave as they did before.

    ``Popen.args`` is reset to the command's own argv for the same reason
    :func:`run_limited` rewrites ``cmd``: ``communicate(timeout=...)`` builds its
    ``TimeoutExpired`` from ``self.args``, so leaving the shim there would put
    ~8 KB of shim source into the timeout message. Nothing in CPython reads
    ``self.args`` functionally -- only ``__repr__`` and that exception.

    A spawn that lands while the launcher interpreter's install tree is being
    rebuilt is retried rather than surfaced -- see
    :data:`_INTERPRETER_ENOENT_DELAYS`. The retry loop is INLINE rather than
    extracted into a helper on purpose: both spawn audits key on
    ``<relpath>::<enclosing function>``, so moving this ``Popen`` into its own
    function would migrate its key, stranding the ``popen_limited`` entries in
    ``_SYNC_ALLOWED`` and ``BENIGN_SPAWNS`` as stale while the relocated call read
    as a brand-new unrouted spawn.

    ``abort_retry`` is consulted after each backoff, and matters only to a caller
    whose cancellation is mediated by a REGISTRY keyed on the live child -- for
    those, the backoff is a window in which a cancel is silently LOST rather than
    merely delayed, because the canceller finds no registered child and records
    nothing, and the retry then launches work the caller already cancelled.
    Returning ``True`` re-raises the ENOENT instead of spawning, so the caller's
    own cancellation path reports the run rather than running it. A caller that
    holds the ``Popen`` handle itself and polls a stop flag (such as
    ``auto_improvement.spine.agent_runner``, which calls ``_terminate_group`` on
    the handle) loses nothing by omitting it: the stop is observed after the
    spawn returns and the child is signalled then.
    """
    cmd, preexec = _prepare_limited_spawn(argv, profile, kwargs, "popen_limited")
    for delay in _INTERPRETER_ENOENT_DELAYS:
        try:
            proc = subprocess.Popen(cmd, preexec_fn=preexec, **kwargs)
            break
        except FileNotFoundError as exc:
            if not _retry_interpreter_enoent(exc, cmd, delay):
                raise
            time.sleep(delay)
            # Checked AFTER the sleep, because that is when a cancellation
            # racing the backoff will have landed. Spawning now would run work
            # the caller has already cancelled, and the exit status would not
            # say so.
            if abort_retry is not None and abort_retry():
                raise
    else:
        # Budget spent. Deliberately unguarded: whatever this raises reaches the
        # caller unchanged, so a genuinely broken install still reports exactly
        # the error it reports today -- just ~4s later.
        proc = subprocess.Popen(cmd, preexec_fn=preexec, **kwargs)
    proc.args = list(argv)
    return proc


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
        masked_credential_leaf_aliases,
    )
    from kiro_crew.sandbox_leaves import (  # noqa: F401
        _AUTH_STORE_STAGING_LEAF,
        _CREW_CHILD_READABLE_LEAVES,
        _CREW_CHILD_WITHHELD_LEAVES,
        _CREW_HOME_DEFAULT,
        _CREW_HOME_PREFIXES,
        _CREW_SANDBOX_VISIBLE_LEAVES,
        _LIVE_TARGET_LEAF,
        _LIVE_TARGET_STAGING_LEAF,
        _MD_NOTEBOOK_STAGING_LEAF,
        _MD_NOTEBOOK_STATE_LEAVES,
        _POLICY_CACHE_LEAF,
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
        _EMPTY_CEILING_DOCUMENT,
        _LIVE_TARGET_PRECREATE_CONTENT,
        _MD_NOTEBOOK_PRECREATE_CONTENT,
    )
    from kiro_crew.sandbox_seals import (  # noqa: F401
        _CREW_NO_ALIAS_LEAVES,
        _delete_file_command,
        _is_link_or_junction,
        _materialize_sealable_parent_dirs,
        _mint_gateway_id_before_seal,
        _note_established,
        _publish_empty_ceiling,
        _refuse_if_aliased_protected_leaf,
        _refuse_if_dangling_symlink,
        _refuse_if_symlink_leaf,
        _require_real_dir_nofollow,
        _require_real_file_nofollow,
        _sealable_absent_ceilings,
        _symlink_target_display,
        _warn_if_alias_backed,
        _warn_unsealed_ceiling,
        _warn_unsealed_ceiling_parent_missing,
        require_real_model_path,
        require_unaliased_cloud_config,
        require_unaliased_launch_state,
        require_unaliased_model_file,
    )
    from kiro_crew.sandbox_seatbelt import _SEATBELT_PROFILE  # noqa: F401
    from kiro_crew.sandbox_staging import (  # noqa: F401
        _AUTH_STORE_LEGACY_TEMP_PREFIX,
        _AUTH_STORE_PUBLISHED_KEY_LEAF,
        _MD_NOTEBOOK_STATE_COMPONENTS,
        LiveTargetUnfitness,
        _live_target_irregular_detail,
        _live_target_multilink_detail,
        _live_target_symlink_detail,
        _open_dir_anchored,
        _refuse_if_live_target_symlink,
        _refuse_unless_sole_regular_link,
        _sweep_one_md_notebook_state_dir,
        live_target_pointer_unfitness,
    )
    from kiro_crew.sandbox_tiers import (  # noqa: F401
        _APP_BACKEND_OWNED_LEAVES,
        _CC_DIRS,
        _PACKAGED_INSTALL_DIR_NAMES,
        _SANDBOX_MODE_FALLBACK,
        _STRICT_DIRS,
        _clamp_sandbox_mode,
        _gateway_package_dir,
        _install_root_targets_cached,
        _install_root_targets_with,
        _pod_os_home_targets,
        _resolves_to,
        _tier_mask_dirs,
        app_backend_visible_targets,
        carveout_chain_has_planted_link,
        carveout_shadowed_by_foreign_mask,
        configured_sandbox_mode,
        crew_home_visible_spellings,
        install_root_targets_for,
        path_within_tier_mask,
    )
    from kiro_crew.sandbox_workspace import (  # noqa: F401
        _VOICE_GUARD_REMEDY,
        CANONICAL_TEMP_KEYS,
        DECLARED_TEMP_REFUSAL_REASONS,
        BoundWorkspaceMismatch,
        DeclaredTempRefusal,
        _bound_agent_workspace_matches,
        _close_bound_agent_workspace,
        _ensure_voice_runtime_directory,
        _identity_within_sealed_parent,
        _lexical_runtime_overlap,
        _path_identity,
        _repr_declared_temp_log_value,
        _restore_missing_voice_runtime_roots,
        _voice_runtime_guard_message,
        _voice_runtime_paths_cache,
        _voice_runtime_paths_lock,
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
