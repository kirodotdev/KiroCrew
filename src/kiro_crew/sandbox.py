"""OS-level sandbox for agent child processes.

Hides sensitive credential paths (``~/.aws``, ``~/.gnupg``, etc.) from the
kiro-cli subprocess tree and exposes ``~/.ssh/known_hosts`` while hiding
other SSH files (keys, config, etc.), using platform-native isolation:

- **Linux**: fork → ``unshare(CLONE_NEWUSER)`` → parent writes identity
  UID/GID map → ``unshare(CLONE_NEWNS)`` → bind-mount empty dirs → exec.
  The child retains the real UID so all toolchains work normally.
- **macOS**: ``sandbox-exec`` with a Seatbelt profile that denies reads

The parent KiroCrew process is completely unaffected — isolation applies
only to the spawned child.  Falls back gracefully to no sandbox when the
OS mechanism is unavailable (logged as warning).

Config: ``"sandbox": "auto" | "off"`` in ``~/.kiro/crew/config.json``.
``"auto"`` (default) uses namespace sandbox on Linux, seatbelt on macOS.
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import errno
import functools
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew import platform_compat
from kiro_crew.config.paths import config_dir
from kiro_crew.constants import KIROCREW_SPAWNED_ENV, KIROCREW_SPAWNED_VALUE
from kiro_crew.platform import current_context

try:
    import resource as _resource_mod
except ImportError:  # non-POSIX (Windows)
    _resource_mod = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# Launcher scripts and seatbelt profiles are read exactly once at child exec.
# Any file older than this threshold is garbage regardless of PID liveness.
_LAUNCHER_MAX_AGE_SECONDS = 3600

# Legacy sandbox launcher directory (before migration to <config_dir>/run/).
_LEGACY_LAUNCHER_DIR = "/tmp"

# Sensitive directories to hide from the agent subprocess tree.
# "strict" mode hides all; "standard" mode only hides non-workflow dirs.
_STRICT_DIRS: list[str] = [
    ".kiro/crew-auth-staging",
    ".aws",
    ".gnupg",
    ".gpg",
    ".config/gcloud",
    ".config/gh",
    ".azure",
    ".docker",
    ".kube",
]

_STANDARD_DIRS: list[str] = [
    ".kiro/crew-auth-staging",
    ".gnupg",
    ".gpg",
    ".config/gcloud",
    ".azure",
    ".docker",
]

# CC mode: hides all credential dirs including .aws, but selectively exposes
# .aws/config (needed for credential_process → Bedrock auth). All other .aws
# files (credentials, sso cache, etc.) are filesystem-hidden via bind mount.
_CC_DIRS: list[str] = [
    ".kiro/crew-auth-staging",
    ".aws",
    ".gnupg",
    ".gpg",
    ".config/gcloud",
    ".azure",
    ".docker",
    ".kube",
]

# CC mode: files to expose read-only inside otherwise-hidden dirs.
# After hiding the parent dir, these are recreated with original content.
_CC_EXPOSE_FILES: list[str] = [
    ".aws/config",
]

# CC mode: individual sensitive files that aren't inside the hidden dirs above.
# These require file-level (not directory-level) sandbox enforcement.
_CC_FILES: list[str] = [
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".git-credentials",
    # KiroCrew's channel-credential file. The data home moved to ~/.kiro/crew,
    # so the live .env is now ~/.kiro/crew/.env; the legacy ~/.kirocrew/.env is
    # kept covered too (a not-yet-migrated box still holds real secret bytes).
    ".kiro/crew/.env",
    ".kirocrew/.env",
]


def _hidden_path_contains_visible_path(
    hidden_path: str,
    visible_paths: tuple[str, ...],
) -> bool:
    """Return whether hiding *hidden_path* would also hide a required path."""

    hidden = os.path.abspath(hidden_path)
    for item in visible_paths:
        visible = os.path.abspath(item)
        try:
            if os.path.commonpath((hidden, visible)) == hidden:
                return True
        except ValueError:
            continue
    return False


# Sensitive env var prefixes to scrub from the child environment.
# Scrubbed in ALL modes (standard + strict) — credential_process reads
# from ~/.aws/config, not env vars, so scrubbing is always safe.
_SENSITIVE_ENV_PREFIXES: list[str] = [
    "AWS_SECRET",
    "AWS_SESSION",
    "SSH_AUTH_SOCK",
    "GNUPGHOME",
    "GIT_ASKPASS",
]

# Python interpreter env that must NOT leak into a *foreign* Python subprocess
# launched under the sandbox (e.g. the MCP servers kiro-cli spawns, such as
# ord-mcp, which bundle their own interpreter + deps). KiroCrew's runtime may
# export PYTHONPATH pointing at its own site-packages; a foreign server that
# inherits it prepends KiroCrew's site-packages to sys.path and imports
# KiroCrew's fastmcp/cryptography instead of its own -> ABI collision + init
# hang. Stripped ONLY when the caller passes ``strip_python_env=True`` (the
# kiro-cli / agent spawn path). It is deliberately NOT part of
# ``_SENSITIVE_ENV_PREFIXES`` because KiroCrew's OWN sandboxed Python
# subprocesses (cron scripts, app backends, code-review workers) import
# ``kiro_crew`` via PYTHONPATH and would break if it were stripped.
_PYTHON_ENV_PREFIXES: list[str] = [
    "PYTHONPATH",
    "PYTHONHOME",
]

# Gateway-owned credentials must never reach agent-influenced subprocesses.
# This list feeds the cc/strict launcher scrub, the always-on ``scrub_env``
# parent scrub, and ``scrub_agent_denied_env`` — the parent-level scrub the ACP
# spawn paths apply on EVERY tier (incl. the default auto/standard tier, whose
# launcher does not strip these keys). Loader coverage is pinned by regression
# test.
_AGENT_DENIED_ENV_KEYS: list[str] = [
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_USER_TOKEN",
    "WECOM_BOT_ID",
    "WECOM_SECRET",
    "TELEGRAM_BOT_TOKEN",
    "DISCORD_BOT_TOKEN",
    "WEBEX_BOT_TOKEN",
    "MICROSOFT_APP_ID",
    "MICROSOFT_APP_PASSWORD",
    "MICROSOFT_APP_TENANT_ID",
    "WEIXIN_TOKEN",
    "KIROCREW_OWNER_ID",
]


# ── Platform context accessor ──


def _sandbox_policy():
    """Return the active context's SandboxPolicy adapter.

    The Default adapter delegates to ``_STRICT_DIRS`` / ``_CC_DIRS`` above, so a
    standalone process gets today's exact lists; the internal companion extends
    them.
    """
    return current_context().sandbox


# ── Availability probes ──


# unshare(2) flags for the userns probe.
_CLONE_NEWUSER = 0x10000000
_CLONE_NEWNS = 0x00020000

# Errnos that indicate a TRANSIENT resource failure (fork/CDLL under momentary
# pressure) — the kernel supports user namespaces, we just couldn't verify it
# right now. These must never be treated as "this host has no sandbox backend"
# (incident 2026-07-18: one EAGAIN during a cron spawn burst fail-closed every
# subsequent spawn for an hour because the failed probe result was cached).
_TRANSIENT_PROBE_ERRNOS = frozenset(
    {errno.EAGAIN, errno.ENOMEM, errno.EMFILE, errno.ENFILE, errno.ENOSPC}
)

# Delay before the single in-probe retry on a transient failure.
_PROBE_TRANSIENT_RETRY_DELAY_SECS = 0.05

# Detail of the most recent failed userns probe: (transient, reason).
# ``None`` means the last probe succeeded (or none has run yet). Consumed by
# detect_backend() for cache policy and by wrap_argv() for error reporting.
_last_unshare_failure: tuple[bool, str] | None = None


def _probe_unshare_once() -> tuple[bool, bool, str]:
    """One unshare(CLONE_NEWUSER|CLONE_NEWNS) attempt: ``(ok, transient, reason)``.

    The forked child exits with the unshare(2) errno so the parent can
    distinguish a kernel that refuses user namespaces (EPERM/EINVAL/ENOSYS —
    permanent) from momentary resource exhaustion (EAGAIN/ENOMEM/... —
    transient).
    """
    try:
        _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        _libc.unshare.argtypes = [ctypes.c_int]
        _libc.unshare.restype = ctypes.c_int
    except OSError as exc:
        return (False, exc.errno in _TRANSIENT_PROBE_ERRNOS, f"libc load failed: {exc}")
    except Exception as exc:  # find_library returning junk, ABI issues, ...
        return (False, False, f"libc load failed: {exc}")
    try:
        pid = os.fork()
    except OSError as exc:
        name = errno.errorcode.get(exc.errno or 0, "?")
        transient = exc.errno in _TRANSIENT_PROBE_ERRNOS
        return (False, transient, f"fork failed with errno {exc.errno} ({name})")
    if pid == 0:
        try:
            ret = _libc.unshare(_CLONE_NEWUSER | _CLONE_NEWNS)
            err = ctypes.get_errno() if ret != 0 else 0
            os._exit(0 if ret == 0 else (err if 0 < err < 256 else 1))
        except BaseException:
            os._exit(1)
    try:
        _, status = os.waitpid(pid, 0)
    except OSError as exc:
        return (False, True, f"waitpid failed: {exc}")
    if os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0:
        return (True, False, "ok")
    if not os.WIFEXITED(status):
        sig = os.WTERMSIG(status) if os.WIFSIGNALED(status) else 0
        return (
            False,
            True,  # child killed by signal is always transient
            f"probe child killed by signal {sig}",
        )
    child_errno = os.WEXITSTATUS(status)
    name = errno.errorcode.get(child_errno, "?")
    transient = child_errno in _TRANSIENT_PROBE_ERRNOS
    return (
        False,
        transient,
        f"unshare(CLONE_NEWUSER|CLONE_NEWNS) failed with errno {child_errno} ({name})",
    )


# ── Background warm thread (never-block-on-loop policy) ──
# The event loop NEVER executes fork/waitpid/sleep for the probe. On-loop
# callers with a cold cache get an immediate transient "none" (fail-closed,
# self-heals in ms) and fire a background daemon thread that populates the
# cache off-loop. Boot sites call prewarm_backend() to fill the cache before
# any on-loop caller ever reaches detect_backend(), so the transient path is
# typically never hit in production.

_warm_thread: threading.Thread | None = None


def _background_warm() -> None:
    """Run the probe off-loop and populate the cache. Thread target."""
    global _backend, _last_unshare_failure
    for attempt in (1, 2):
        ok, transient, reason = _probe_unshare_once()
        if ok:
            _last_unshare_failure = None
            _backend = "namespace"
            logger.info("Background warm: sandbox backend = namespace")
            return
        _last_unshare_failure = (transient, reason)
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
    """Start the background warm thread if not already running."""
    global _warm_thread
    if _warm_thread is not None and _warm_thread.is_alive():
        return  # dedupe: warm already in progress
    _warm_thread = threading.Thread(target=_background_warm, name="sandbox-probe-warm", daemon=True)
    _warm_thread.start()


def prewarm_backend() -> None:
    """Fire-and-forget boot hook: start background probe to fill the cache.

    Call early in gateway startup (slack/gateway.py, mcp_gateway/gatewayd.py)
    so the cache is warm before any on-loop spawn path reaches detect_backend().
    """
    if sys.platform != "linux":
        return  # probes are Linux-only
    _kick_background_warm()


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
        _last_unshare_failure = (False, "not Linux")
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
        _last_unshare_failure = (
            True,
            "probe deferred to background thread (cold cache on event loop); "
            "cache warms in ms — retry",
        )
        return False

    # Off-loop: direct probe with one retry on transient failure.
    for attempt in (1, 2):
        ok, transient, reason = _probe_unshare_once()
        if ok:
            _last_unshare_failure = None
            return True
        _last_unshare_failure = (transient, reason)
        if not transient:
            logger.warning("userns probe failed (permanent): %s", reason)
            return False
        logger.warning("userns probe failed (transient, attempt %d/2): %s", attempt, reason)
        if attempt == 1:
            time.sleep(_PROBE_TRANSIENT_RETRY_DELAY_SECS)
    return False


def userns_available() -> bool:
    """Public: True if unprivileged user + mount namespaces work on this host.

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
            timeout=5,
        )
        if r.returncode != 0:
            logger.warning(
                "sandbox-exec probe failed (exit %d): %s",
                r.returncode,
                r.stderr.decode(errors="replace").strip(),
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


def _build_launcher_script(
    sandbox_level: str = "strict",
    *,
    strip_python_env: bool = False,
    extra_hidden_dirs: tuple[str, ...] = (),
    extra_visible_dirs: tuple[str, ...] = (),
) -> str:
    """Build a Python launcher script for the Linux namespace sandbox.

    The launcher is executed as a subprocess.  It:

    1. Forks a child.
    2. Child calls ``unshare(CLONE_NEWUSER)`` and signals the parent.
    3. Parent writes identity UID/GID map (``uid uid 1``) to
       ``/proc/<child>/{setgroups,uid_map,gid_map}`` and signals back.
    4. Child calls ``unshare(CLONE_NEWNS)``, sets mount propagation private,
       bind-mounts empty dirs over credential paths, scrubs env vars,
       and ``exec``s the real command.

    The child retains the real UID/GID — no UID 0, no UID 65534.
    """
    home = str(Path.home())
    uid = os.getuid()
    gid = os.getgid()
    # Source the sensitive-dir lists from the active PlatformContext so the
    # The internal companion can extend them (+ .midway/.ada).  The Default adapter
    # returns ``list(_STRICT_DIRS)`` / ``list(_CC_DIRS)``, so standalone is
    # unchanged.  ``_STANDARD_DIRS`` is not an extension point (no interface
    # method) and stays on the module global.
    if sandbox_level == "standard":
        dirs = _STANDARD_DIRS
    elif sandbox_level == "cc":
        dirs = _sandbox_policy().cc_dirs()
    else:
        dirs = _sandbox_policy().strict_dirs()
    files = _CC_FILES if sandbox_level in ("cc", "strict") else []
    expose_files = _CC_EXPOSE_FILES if sandbox_level == "cc" else []
    env_prefixes = list(_SENSITIVE_ENV_PREFIXES)
    if sandbox_level in ("cc", "strict"):
        # Block agent subprocesses from reading credentials via os.environ
        # (the file-level bind-mount of ~/.kiro/crew/.env hides them on disk;
        # config/loader.py seeds them into os.environ for trusted children
        # only — sandboxed agents must not see them either way).
        env_prefixes = env_prefixes + list(_AGENT_DENIED_ENV_KEYS)
    if strip_python_env:
        # Foreign Python subprocess (kiro-cli's MCP servers) — do not let
        # KiroCrew's PYTHONPATH/PYTHONHOME leak in and shadow their own deps.
        env_prefixes = env_prefixes + list(_PYTHON_ENV_PREFIXES)
    hide_ssh = sandbox_level == "strict"
    hidden_dirs = [os.path.join(home, d) for d in dirs]
    hidden_dirs.extend(os.path.abspath(path) for path in extra_hidden_dirs)
    hidden_dirs = [
        path
        for path in hidden_dirs
        if not _hidden_path_contains_visible_path(path, extra_visible_dirs)
    ]
    dirs_json = json.dumps(list(dict.fromkeys(hidden_dirs)))
    files_json = json.dumps([os.path.join(home, f) for f in files])
    expose_json = json.dumps([(os.path.join(home, f), f.split("/")[-1]) for f in expose_files])
    env_prefixes_json = json.dumps(env_prefixes)
    ssh_dir = json.dumps(os.path.join(home, ".ssh"))
    ssh_known_hosts = json.dumps(os.path.join(home, ".ssh", "known_hosts"))
    strict_host_key_opt = (
        " -o StrictHostKeyChecking=accept-new" if _ssh_supports_accept_new() else ""
    )

    return f'''#!/usr/bin/env python3
"""Namespace sandbox launcher — spawned by KiroCrew."""
import sys
# Harden against stdlib shadowing. This launcher runs as
# ``python <config_dir>/run/kirocrew_sandbox_*.py``, so CPython prepends the
# script's own directory (sys.path[0], typically <config_dir>/run/) to sys.path.
# A stray sibling module left in that directory by another process — e.g.
# struct.py, os.py — then shadows the real stdlib and crashes the imports below
# (seen in the wild: "ImportError: cannot import name 'calcsize' from
# '/tmp/struct.py'", which kills the agent subprocess on spawn). ``sys`` is a
# builtin and cannot be shadowed, so importing it first is safe; drop the
# launcher dir (and any cwd "" entry) before importing anything that resolves
# from the filesystem.
sys.path[:] = [p for p in sys.path if p not in ("", sys.path[0])]
import ctypes
import ctypes.util
import os
import tempfile

_CLONE_NEWUSER = 0x10000000
_CLONE_NEWNS   = 0x00020000
_MS_BIND       = 4096
_MS_REC        = 16384
_MS_PRIVATE    = 1 << 18

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
_libc.mount.argtypes = [
    ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
    ctypes.c_ulong, ctypes.c_void_p,
]
_libc.mount.restype = ctypes.c_int
_libc.unshare.argtypes = [ctypes.c_int]
_libc.unshare.restype = ctypes.c_int
_libc.prctl = _libc.prctl if hasattr(_libc, "prctl") else None
if _libc.prctl:
    _libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    _libc.prctl.restype = ctypes.c_int

REAL_UID = {uid}
REAL_GID = {gid}
SENSITIVE_DIRS = {dirs_json}
SENSITIVE_FILES = {files_json}
EXPOSE_FILES = {expose_json}
ENV_PREFIXES = {env_prefixes_json}
SSH_DIR = {ssh_dir}
SSH_KNOWN_HOSTS = {ssh_known_hosts}
HIDE_SSH = {hide_ssh}

def main():
    argv = sys.argv[1:]
    if not argv:
        sys.exit("sandbox_launcher: no command given")

    # Export this launcher's HOST pid before any fork/namespace work. The
    # gateway records exactly this pid (its direct Popen child) when it
    # writes ``session_pid_<pid>.txt`` on session claim, so in-sandbox
    # identity resolvers can look the file up directly via this env var
    # instead of walking /proc — which breaks whenever the subtree's view
    # of pids diverges from the host's (PID-namespace sandboxing).
    os.environ["KIROCREW_HOST_PID"] = str(os.getpid())

    # Two pipes for parent↔child synchronization
    c2p_r, c2p_w = os.pipe()  # child signals "unshare done"
    p2c_r, p2c_w = os.pipe()  # parent signals "maps written"

    pid = os.fork()

    if pid > 0:
        # ── Parent: write identity UID/GID map ──
        os.close(c2p_w)
        os.close(p2c_r)
        os.read(c2p_r, 1)  # wait for child to unshare(NEWUSER)
        os.close(c2p_r)
        with open(f"/proc/{{pid}}/setgroups", "w") as f:
            f.write("deny")
        with open(f"/proc/{{pid}}/uid_map", "w") as f:
            f.write(f"{{REAL_UID}} {{REAL_UID}} 1\\n")
        with open(f"/proc/{{pid}}/gid_map", "w") as f:
            f.write(f"{{REAL_GID}} {{REAL_GID}} 1\\n")
        os.write(p2c_w, b"x")  # signal child to proceed
        os.close(p2c_w)
        _, status = os.waitpid(pid, 0)
        code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1
        sys.exit(code)
    else:
        # ── Child: unshare, wait for maps, mount, exec ──
        os.close(c2p_r)
        os.close(p2c_w)

        # Step 1: enter user namespace
        if _libc.unshare(_CLONE_NEWUSER) != 0:
            sys.exit(f"sandbox: unshare(NEWUSER) failed: errno {{ctypes.get_errno()}}")
        os.write(c2p_w, b"x")  # tell parent
        os.close(c2p_w)
        os.read(p2c_r, 1)  # wait for maps
        os.close(p2c_r)

        # Step 2: enter mount namespace (now we have a mapped UID)
        if _libc.unshare(_CLONE_NEWNS) != 0:
            sys.exit(f"sandbox: unshare(NEWNS) failed: errno {{ctypes.get_errno()}}")

        # Private mount propagation
        _libc.mount(None, b"/", None, _MS_REC | _MS_PRIVATE, None)

        # Pick a tmpfs-backed source dir for bind-mount empty files/dirs. Same-fs
        # binds (e.g. /tmp on ext4 over ~/.kiro/crew/.env on ext4) can corrupt the
        # target's host directory entry via a kernel propagation race when the
        # private NS is torn down — leaving the host file pointing at the empty
        # source inode permanently. Cross-fs binds use distinct inode spaces and
        # cannot leak that way. Fallback chain: /run/user/$UID → /dev/shm.
        # Verify each candidate is on a different filesystem from HOME by
        # comparing st_dev — same-fs candidates provide no isolation benefit.
        _tmpfs_src = None
        try:
            _home_dev = os.stat(os.path.expanduser("~")).st_dev
        except OSError:
            _home_dev = None
        for _candidate in (f"/run/user/{{REAL_UID}}", "/dev/shm"):
            try:
                if _home_dev is not None and os.stat(_candidate).st_dev == _home_dev:
                    continue  # same fs as HOME — no isolation, race still possible
                _probe = tempfile.mkdtemp(dir=_candidate, prefix="kirocrew_sb_")
                os.rmdir(_probe)
                _tmpfs_src = _candidate
                break
            except (OSError, ValueError):
                continue
        # _tmpfs_src=None falls through to system default tempdir (typically /tmp).
        # In that case we accept the kernel-race risk because no tmpfs is
        # available — better to function (with the original regression risk)
        # than to refuse to start.

        # Pre-read files that must survive dir hiding
        expose_data = {{}}
        for src_path, filename in EXPOSE_FILES:
            if os.path.isfile(src_path):
                with open(src_path, "rb") as fh:
                    expose_data[src_path] = fh.read()

        # Bind-mount empty dirs over credential paths (per-dir tmpdir to
        # prevent content leaking across mounts via shared backing dir).
        for d in SENSITIVE_DIRS:
            target = d.encode()
            if os.path.isdir(target):
                per_dir_empty = tempfile.mkdtemp(dir=_tmpfs_src).encode()
                _libc.mount(per_dir_empty, target, None, _MS_BIND, None)

        # Restore selectively exposed files into the now-empty mounts
        for src_path, filename in EXPOSE_FILES:
            if src_path in expose_data:
                parent = os.path.dirname(src_path)
                dest = os.path.join(parent, filename)
                with open(dest, "wb") as fh:
                    fh.write(expose_data[src_path])
                # NOTE: this runs inside the embedded Linux-only namespace
                # launcher script (a standalone /tmp file that imports only
                # stdlib — sys/ctypes/os/tempfile — and never kiro_crew), so it
                # must stay a raw os.chmod, NOT platform_compat.chmod_safe
                # (which is undefined in that process). The launcher never runs
                # on Windows, so there is no portability loss.
                os.chmod(dest, 0o444)

        # Bind-mount empty files over individual sensitive files. Source the
        # empty tempfile from a tmpfs (cross-fs) when available so the bind
        # cannot corrupt the target's host directory entry on namespace exit.
        for f in SENSITIVE_FILES:
            target = f.encode()
            if os.path.isfile(target):
                fd, empty_path = tempfile.mkstemp(dir=_tmpfs_src)
                os.close(fd)
                _libc.mount(empty_path.encode(), target, None, _MS_BIND, None)

        # .ssh: hide keys but expose known_hosts content (strict only)
        if HIDE_SSH and os.path.isdir(SSH_DIR):
            kh_data = b""
            if os.path.isfile(SSH_KNOWN_HOSTS):
                with open(SSH_KNOWN_HOSTS, "rb") as fh:
                    kh_data = fh.read()
            # Cross-fs source for the same kernel-race reason as SENSITIVE_DIRS
            # (line 371) and SENSITIVE_FILES (line 389).
            ssh_tmp = tempfile.mkdtemp(dir=_tmpfs_src).encode()
            _libc.mount(ssh_tmp, SSH_DIR.encode(), None, _MS_BIND, None)
            if kh_data:
                with open(os.path.join(SSH_DIR, "known_hosts"), "wb") as fh:
                    fh.write(kh_data)

        # Scrub sensitive env vars
        for key in list(os.environ):
            for prefix in ENV_PREFIXES:
                if key.startswith(prefix):
                    del os.environ[key]
                    break

        # Mark the sandboxed tree so in-sandbox wrap_argv calls know OS
        # isolation is already active (nested unshare is seccomp-denied).
        # Set AFTER the scrub loop so a scrubbed prefix cannot delete it.
        os.environ["KIROCREW_SANDBOX_ACTIVE"] = "1"

        # Fix /etc/ssh/ssh_config.d/ ownership issue: root-owned files
        # appear as nobody:nobody inside the user namespace because UID 0
        # is unmapped. SSH refuses to load them. Bypass with -F /dev/null.
        if not os.environ.get("GIT_SSH_COMMAND"):
            os.environ["GIT_SSH_COMMAND"] = (
                "ssh -F /dev/null -o IdentityFile=~/.ssh/id_rsa"
                " -o IdentityFile=~/.ssh/id_ecdsa"
                " -o IdentityFile=~/.ssh/id_ed25519"
                " -o UserKnownHostsFile=~/.ssh/known_hosts"
                "{strict_host_key_opt}"
            )

        # ── Step 5: Drop capabilities + set NO_NEW_PRIVS ──
        # Inside the user namespace, the child has CAP_SYS_ADMIN (owner of the
        # NS) which lets it umount the credential bind-mounts. Drop ALL
        # capabilities from the bounding set and set NO_NEW_PRIVS before exec.
        import struct as _struct

        _PR_SET_NO_NEW_PRIVS = 38
        _PR_CAPBSET_DROP = 24
        if _libc.prctl:
            # Linux CAP_LAST_CAP is currently 41 (kernel 6.x); iterate 0..63 for
            # forward-compatibility — dropping a non-existent cap just returns -1.
            for _cap in range(64):
                _libc.prctl(_PR_CAPBSET_DROP, _cap, 0, 0, 0)
            # NO_NEW_PRIVS: prevents regaining caps via exec of setuid/setcap bins
            _ret = _libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
            if _ret != 0:
                sys.exit("sandbox: BLOCKED — failed to set NO_NEW_PRIVS (prctl returned %d)" % _ret)

        # ── Step 6: Install seccomp-BPF filter ──
        # Deny mount/umount2/unshare/setns/pivot_root/link/linkat to prevent
        # the sandboxed process from undoing bind-mounts or creating hardlinks
        # to protected credential inodes.
        #
        # Additionally deny kill(-1, sig) — the signal BROADCAST that reaches
        # every same-uid process on the host (gateway, other sessions). This
        # is the accident-containment redo of the reverted PID-namespace
        # isolation (24c320f6): a static arg filter blocks the hand-slip /
        # runaway-script broadcast without changing the subtree's view of
        # pids, so session identity, claim-push, and systemd stay intact.
        # Only ``kill`` needs arg inspection: tkill/tgkill/pidfd_send_signal
        # are inherently targeted (no broadcast semantics). pid==0 and
        # negative process-group targets stay ALLOWED on purpose — the spawn
        # already setsid()s, so every reachable process group is inside the
        # sandbox session, and denying killpg breaks legitimate tooling
        # (timeout(1), shell job control, cleanup traps).
        if _libc.prctl:
            _PR_SET_SECCOMP = 22
            _SECCOMP_MODE_FILTER = 2
            _SECCOMP_RET_ALLOW = 0x7FFF0000
            _SECCOMP_RET_ERRNO = 0x00050000
            _EPERM = 1
            _BPF_LD = 0x00
            _BPF_W = 0x00
            _BPF_ABS = 0x20
            _BPF_JMP = 0x05
            _BPF_JEQ = 0x10
            _BPF_K = 0x00
            _BPF_RET = 0x06
            # Syscall numbers (x86_64): mount=165, umount2=166, unshare=272,
            # setns=308, pivot_root=155, link=86, linkat=265, kill=62
            # aarch64: mount=40, umount2=39, unshare=97, setns=268,
            # pivot_root=41, link=N/A(use linkat=37), linkat=37, kill=129
            import platform as _plat
            _machine = _plat.machine()
            if _machine == "x86_64":
                _DENY_SYSCALLS = (165, 166, 272, 308, 155, 86, 265)
                _KILL_NR = 62
            elif _machine == "aarch64":
                _DENY_SYSCALLS = (40, 39, 97, 268, 41, 37)
                _KILL_NR = 129
            else:
                _DENY_SYSCALLS = ()  # unknown arch — skip seccomp
                _KILL_NR = None

            if _DENY_SYSCALLS:
                # Architecture constants for seccomp arch validation
                _AUDIT_ARCH_X86_64 = 0xC000003E
                _AUDIT_ARCH_AARCH64 = 0xC00000B7
                _SECCOMP_RET_KILL = 0x00000000
                _expected_arch = _AUDIT_ARCH_X86_64 if _machine == "x86_64" else _AUDIT_ARCH_AARCH64

                # BPF program layout (indices relative to start):
                #   0: LD arch
                #   1: JEQ expected_arch ? skip 1 : fall through
                #   2: RET KILL                (unexpected arch)
                #   3: LD syscall nr
                #   4..4+n-1: JEQ deny_i -> DENY
                #   k   = 4+n: JEQ kill_nr ? fall into arg check : jump ALLOW
                #   k+1: LD args[0] low 32 bits    (seccomp_data offset 16)
                #   k+2: JEQ 0xFFFFFFFF ? jump DENY : fall through
                #   ALLOW = k+3: RET ALLOW
                #   DENY  = k+4: RET ERRNO|EPERM
                #
                # Only the LOW 32 bits of args[0] are inspected. pid_t is a
                # 32-bit int: the kernel truncates the register to 32 bits, so
                # low==0xFFFFFFFF is exactly "pid == -1" regardless of what the
                # upper half holds. The upper half MUST NOT be matched — the
                # x86-64 ABI leaves it undefined for int arguments, and glibc's
                # ``movl`` zero-extends, so kill(-1) typically arrives as
                # 0x00000000_FFFFFFFF (a high==0xFFFFFFFF check silently never
                # fires, which is a filter bypass, not a compat issue).
                _insns = []
                # Load arch: BPF_LD | BPF_W | BPF_ABS, offset=4 (seccomp_data.arch)
                _insns.append(_struct.pack("<HBBI", _BPF_LD | _BPF_W | _BPF_ABS, 0, 0, 4))
                # If arch == expected, skip next insn (jt=1); else fall through to kill
                _insns.append(_struct.pack("<HBBI", _BPF_JMP | _BPF_JEQ | _BPF_K, 1, 0, _expected_arch))
                # Kill on unexpected arch (blocks i386 int 0x80 bypass)
                _insns.append(_struct.pack("<HBBI", _BPF_RET | _BPF_K, 0, 0, _SECCOMP_RET_KILL))
                # Load syscall number: BPF_LD | BPF_W | BPF_ABS, offset=0
                _insns.append(_struct.pack("<HBBI", _BPF_LD | _BPF_W | _BPF_ABS, 0, 0, 0))
                # For each denied syscall: JEQ -> DENY (at index k+4)
                _n_deny = len(_DENY_SYSCALLS)
                for _i, _nr in enumerate(_DENY_SYSCALLS):
                    _jt = (_n_deny - _i - 1) + 4  # jumps to the DENY RET at k+4
                    _insns.append(_struct.pack("<HBBI",
                        _BPF_JMP | _BPF_JEQ | _BPF_K, _jt, 0, _nr))
                # k: nr == kill ? fall into arg check : jump to ALLOW (k+3)
                _insns.append(_struct.pack("<HBBI",
                    _BPF_JMP | _BPF_JEQ | _BPF_K, 0, 2, _KILL_NR))
                # k+1: load args[0] low word (offset 16, little-endian layout)
                _insns.append(_struct.pack("<HBBI", _BPF_LD | _BPF_W | _BPF_ABS, 0, 0, 16))
                # k+2: low == 0xFFFFFFFF (pid -1) ? DENY (skip 1) : fall to ALLOW
                _insns.append(_struct.pack("<HBBI",
                    _BPF_JMP | _BPF_JEQ | _BPF_K, 1, 0, 0xFFFFFFFF))
                # ALLOW: return SECCOMP_RET_ALLOW
                _insns.append(_struct.pack("<HBBI", _BPF_RET | _BPF_K, 0, 0, _SECCOMP_RET_ALLOW))
                # DENY: return SECCOMP_RET_ERRNO | EPERM
                _insns.append(_struct.pack("<HBBI", _BPF_RET | _BPF_K, 0, 0, _SECCOMP_RET_ERRNO | _EPERM))

                _prog_bytes = b"".join(_insns)
                _n_insns = len(_insns)

                # struct sock_fprog {{ unsigned short len; struct sock_filter *filter; }}
                class _SockFprog(ctypes.Structure):
                    _fields_ = [("len", ctypes.c_ushort),
                                ("filter", ctypes.c_char_p)]

                _fprog = _SockFprog()
                _fprog.len = _n_insns
                _fprog.filter = _prog_bytes
                _ret = _libc.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER,
                                   ctypes.addressof(_fprog), 0, 0)
                if _ret != 0:
                    sys.exit("sandbox: BLOCKED — failed to install seccomp-BPF filter (prctl returned %d)" % _ret)

        # ── Step 7: Pre-exec hardlink scan ──
        # Scan the agent workspace + /tmp for hardlinks (nlink > 1) whose
        # inode matches a protected credential file. If found, refuse to exec.
        _protected_inodes = set()
        for _pd in SENSITIVE_DIRS:
            if os.path.isdir(_pd):
                for _root, _dirs_scan, _files_scan in os.walk(_pd):
                    for _fname in _files_scan:
                        try:
                            _st = os.stat(os.path.join(_root, _fname))
                            _protected_inodes.add((_st.st_dev, _st.st_ino))
                        except OSError:
                            pass
                    break  # depth=1 for credential dirs
        for _pf in SENSITIVE_FILES:
            try:
                _st = os.stat(_pf)
                _protected_inodes.add((_st.st_dev, _st.st_ino))
            except OSError:
                pass

        if _protected_inodes:
            _scan_count = 0
            _MAX_SCAN = 10000
            _dangerous_links = []
            _cwd = os.getcwd()
            for _scan_root in (_cwd, "/tmp"):
                if not os.path.isdir(_scan_root):
                    continue
                for _root2, _dirs2, _files2 in os.walk(_scan_root):
                    # Depth limit: max 5 levels
                    _depth = _root2[len(_scan_root):].count(os.sep)
                    if _depth > 5:
                        _dirs2.clear()
                        continue
                    for _fn2 in _files2:
                        _scan_count += 1
                        if _scan_count > _MAX_SCAN:
                            break
                        _fp2 = os.path.join(_root2, _fn2)
                        try:
                            _st2 = os.lstat(_fp2)
                            if _st2.st_nlink > 1:
                                if (_st2.st_dev, _st2.st_ino) in _protected_inodes:
                                    _dangerous_links.append(_fp2)
                        except OSError:
                            pass
                    if _scan_count > _MAX_SCAN:
                        break
            if _dangerous_links:
                sys.exit(
                    f"sandbox: BLOCKED — found hardlink(s) to protected credential "
                    f"inodes: {{_dangerous_links[:5]}}. Remove them before running."
                )

        os.execvp(argv[0], argv)

if __name__ == "__main__":
    main()
'''


def _ensure_run_dir() -> str:
    """Create ``<config_dir>/run/`` with mode 0o700, falling back to tmpdir on failure."""
    run_dir = str(config_dir() / "run")
    try:
        os.makedirs(run_dir, mode=0o700, exist_ok=True)
        # exist_ok does not re-apply mode on existing dirs — enforce explicitly.
        # 0o700 (owner-only rwx) is deliberately restrictive: this dir holds
        # per-session sandbox launcher scripts and sockets that must NOT be
        # world-readable. Semgrep's 0o644 suggestion is wrong for a directory
        # (needs the execute/traverse bit) and would loosen, not tighten, access.
        os.chmod(run_dir, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501  # fmt: skip
    except OSError:
        logger.warning("Cannot create %s; falling back to system tmpdir", run_dir)
        run_dir = tempfile.gettempdir()
    return run_dir


def namespace_argv(
    argv: list[str],
    sandbox_level: str = "strict",
    *,
    strip_python_env: bool = False,
    extra_hidden_dirs: tuple[str, ...] = (),
    extra_visible_dirs: tuple[str, ...] = (),
) -> list[str]:
    """Wrap *argv* via the Python namespace launcher.

    The launcher forks, the parent writes identity UID/GID maps, and the
    child bind-mounts empty dirs over credential paths before exec.
    The child retains the real UID/GID.
    """
    resolved_argv = list(argv)
    if resolved_argv:
        resolved_argv[0] = _resolve_agent_executable(resolved_argv[0])

    script = _build_launcher_script(
        sandbox_level,
        strip_python_env=strip_python_env,
        extra_hidden_dirs=extra_hidden_dirs,
        extra_visible_dirs=extra_visible_dirs,
    )
    run_dir = _ensure_run_dir()
    fd, path = tempfile.mkstemp(
        suffix=".py", prefix=f"kirocrew_sandbox_{os.getpid()}_", dir=run_dir
    )
    os.write(fd, script.encode())
    os.close(fd)
    platform_compat.chmod_safe(path, 0o700)

    return [sys.executable, path, *resolved_argv]


# ── Backend: macOS sandbox-exec ──

_SEATBELT_PROFILE = """\
(version 1)
(allow default)
{deny_rules}
"""


def _build_seatbelt_profile(
    sandbox_level: str = "strict",
    *,
    extra_hidden_dirs: tuple[str, ...] = (),
    extra_visible_dirs: tuple[str, ...] = (),
) -> str:
    """Build a Seatbelt .sb profile denying reads of sensitive dirs."""
    home = str(Path.home())
    # Source the sensitive-dir lists from the active PlatformContext (Default
    # adapter == today's module globals; internal companion adds .midway/.ada).
    if sandbox_level == "standard":
        dirs = _STANDARD_DIRS
    elif sandbox_level == "cc":
        # On macOS, don't hide .aws — credential_process and SSO token
        # caches live under .aws/ and Seatbelt can't do partial exposure
        # as cleanly as Linux bind mounts. Deny patterns still block LLM
        # tool reads of credential files. The .aws-exclusion is applied to the
        # context-sourced list so a companion's extra cc dirs are still hidden.
        dirs = [d for d in _sandbox_policy().cc_dirs() if d != ".aws"]
    else:
        dirs = _sandbox_policy().strict_dirs()
    files = _CC_FILES if sandbox_level in ("cc", "strict") else []
    expose_files = _CC_EXPOSE_FILES if sandbox_level == "cc" else []
    expose_abs = {os.path.join(home, f) for f in expose_files}
    rules: list[str] = []
    for d in dirs:
        target = os.path.join(home, d)
        if _hidden_path_contains_visible_path(target, extra_visible_dirs):
            continue
        escaped = target.replace('"', '\\"')
        # Check if any exposed files live under this dir
        exposed_in_dir = [f for f in expose_abs if f.startswith(target + "/")]
        if exposed_in_dir:
            exceptions = " ".join(
                f'(require-not (literal "{f.replace(chr(34), chr(92) + chr(34))}"))'
                for f in exposed_in_dir
            )
            rules.append(f'(deny file-read* (require-all (subpath "{escaped}") {exceptions}))')
        else:
            rules.append(f'(deny file-read* (subpath "{escaped}"))')
        # Deny creating a HARDLINK whose target is under this dir.
        # Seatbelt's file-read* deny is path-based, so a hardlink at a
        # non-denied path (e.g. /tmp) reads the same inode past the deny rule.
        # ``file-link`` fires on the link TARGET, so this stops the sandboxed
        # agent from minting such a hardlink in the first place.  Blanket (no
        # exposed-file exception): the agent never needs to hardlink a
        # credential-dir file, and blocking it is harmless.
        rules.append(f'(deny file-link (subpath "{escaped}"))')
    for f in files:
        target = os.path.join(home, f)
        escaped = target.replace('"', '\\"')
        rules.append(f'(deny file-read* (literal "{escaped}"))')
        # Also deny hardlinking the protected file (see above).
        rules.append(f'(deny file-link (literal "{escaped}"))')
    for target in dict.fromkeys(os.path.abspath(path) for path in extra_hidden_dirs):
        if _hidden_path_contains_visible_path(target, extra_visible_dirs):
            continue
        escaped = target.replace('"', '\\"')
        rules.append(f'(deny file-read* (subpath "{escaped}"))')
        rules.append(f'(deny file-write* (subpath "{escaped}"))')
        rules.append(f'(deny file-link (subpath "{escaped}"))')

    # .ssh: deny all access except reading known_hosts (strict only)
    if sandbox_level == "strict":
        ssh_dir = os.path.join(home, ".ssh")
        ssh_escaped = ssh_dir.replace('"', '\\"')
        ssh_kh = os.path.join(ssh_dir, "known_hosts")
        ssh_kh_escaped = ssh_kh.replace('"', '\\"')
        rules.append(
            f'(deny file-read* (require-all (subpath "{ssh_escaped}")'
            f' (require-not (literal "{ssh_kh_escaped}"))))'
        )
        rules.append(f'(deny file-write* (subpath "{ssh_escaped}"))')
        # Block hardlinking any .ssh file (private keys) out of the
        # denied subtree.  Blanket over the whole subpath — no known_hosts
        # exception, since a hardlink to known_hosts has no legitimate use.
        rules.append(f'(deny file-link (subpath "{ssh_escaped}"))')

    return _SEATBELT_PROFILE.format(deny_rules="\n".join(rules))


# kiro-cli >= 2.13 ships its own internal agent sandbox, toggled by the
# "sandbox" key in this settings file. On macOS its in-process seatbelt init
# cannot nest inside KiroCrew's sandbox-exec wrap — the kernel returns EPERM
# even under an (allow default) outer profile (verified 2026-07-20 on
# macOS 26.5.2 / kiro-cli 2.13.0). Exactly one sandbox layer can be active per
# spawn, so on macOS the layers are mutually exclusive:
# kiro's internal sandbox ON  -> KiroCrew's seatbelt OFF for kiro-cli spawns
# kiro's internal sandbox OFF -> KiroCrew's seatbelt ON (unchanged default)
# (``~/.kiro/settings`` is the kiro-cli backend's own directory, distinct from
# KiroCrew's data home ``~/.kiro/crew``; the filename is the literal kiro-cli ships.)
_KIRO_INTERNAL_SETTINGS_PATH = "~/.kiro/settings/amazon-internal.json"
_KIRO_INTERNAL_SANDBOX_KEY = "sandbox"

# One loud warning per process for the delegation decision (per-spawn logs
# would spam warm-pool refills); every delegated spawn is still SEL-audited.
_kiro_delegation_warned = False


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
) -> tuple[list[str], str | None]:
    """macOS sandbox mutual exclusion: kiro-cli's internal sandbox owns
    isolation for this spawn; KiroCrew's seatbelt is skipped.

    This is NOT the forbidden silent unsandboxed fallback: the child still
    runs under an OS sandbox (kiro's own), the delegation is config-driven and
    deterministic (never a reaction to a wrap failure), it is logged loudly
    once per process, and every delegated spawn is SEL-audited on an
    audit-or-deny basis — if the audit event cannot be written, the delegation
    is refused and the spawn falls back to KiroCrew's own seatbelt. The env
    scrub is applied exactly as the seatbelt wrap would have applied it.

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
            tool_name=argv[0] if argv else "unknown",
            tool_kind="subprocess",
            outcome="delegated",
            resources=(
                "macOS sandbox mutual exclusion: kiro internal sandbox on -> "
                "KiroCrew seatbelt off for this kiro-cli spawn"
            ),
            # audit-or-deny: written synchronously; a filesystem failure
            # re-raises so an unaudited delegation can never proceed.
            critical=True,
        )
    except Exception:
        # Fail closed (security-controls): a security delegation that cannot
        # be audited does not happen. Fall back to KiroCrew's own seatbelt —
        # the always-safe, audited-by-default layer. If kiro's internal
        # sandbox is enabled this spawn will then fail with the nested-
        # sandbox EPERM rather than run unaudited: safety over availability
        # while SEL is broken.
        logger.warning(
            "SEL audit failed for sandbox delegation — refusing unaudited "
            "delegation; falling back to KiroCrew's seatbelt",
            exc_info=True,
        )
        return sandbox_exec_argv(argv, sandbox_level, strip_python_env=strip_python_env)
    # SEL audit succeeded — delegation is actually proceeding. Only now
    # consume the warn-once flag (a SEL-failed attempt above fell back to
    # seatbelt and must not burn the warning for the first real delegation).
    if not _kiro_delegation_warned:
        _kiro_delegation_warned = True
        logger.warning(
            "SECURITY: kiro-cli's internal sandbox is enabled (%s) — delegating "
            "agent isolation to it and skipping KiroCrew's seatbelt for kiro-cli "
            "spawns (nested seatbelt is impossible on macOS; exactly one layer "
            "can be active). To use KiroCrew's sandbox instead, set "
            '{"sandbox": false} in that file. Env scrubbing still applies.',
            _KIRO_INTERNAL_SETTINGS_PATH,
        )
    unset_args = _sandbox_env_unset_args(sandbox_level, strip_python_env)
    if unset_args:
        return ["env", *unset_args, *argv], None
    return list(argv), None


def sandbox_exec_argv(
    argv: list[str],
    sandbox_level: str = "strict",
    *,
    strip_python_env: bool = False,
    extra_hidden_dirs: tuple[str, ...] = (),
    extra_visible_dirs: tuple[str, ...] = (),
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

    profile = _build_seatbelt_profile(
        sandbox_level,
        extra_hidden_dirs=extra_hidden_dirs,
        extra_visible_dirs=extra_visible_dirs,
    )
    run_dir = _ensure_run_dir()
    fd, path = tempfile.mkstemp(
        suffix=".sb", prefix=f"kirocrew_sandbox_{os.getpid()}_", dir=run_dir
    )
    os.write(fd, profile.encode())
    os.close(fd)
    # Build env -u flags for sensitive vars present in current env. cc/strict
    # additionally scrub agent-denied credential keys (Slack tokens, owner id)
    # since loader.py seeds them into os.environ for trusted children only.
    unset_args = _sandbox_env_unset_args(sandbox_level, strip_python_env)
    return ["env", *unset_args, "sandbox-exec", "-f", path, *resolved_argv], path


def _sandbox_env_unset_args(sandbox_level: str, strip_python_env: bool) -> list[str]:
    """``env -u`` flags scrubbing sensitive vars for a sandboxed/delegated spawn.

    Shared by ``sandbox_exec_argv`` (seatbelt wrap) and
    ``_delegate_to_kiro_internal_sandbox`` (macOS mutual-exclusion path) so the
    env-scrub guarantee is identical whether or not KiroCrew's own seatbelt is
    the active isolation layer.
    """
    prefixes = list(_SENSITIVE_ENV_PREFIXES)
    if sandbox_level in ("cc", "strict"):
        prefixes.extend(_AGENT_DENIED_ENV_KEYS)
    if strip_python_env:
        prefixes.extend(_PYTHON_ENV_PREFIXES)
    unset_args: list[str] = []
    for key in os.environ:
        for prefix in prefixes:
            if key.startswith(prefix):
                unset_args.extend(["-u", key])
                break
    return unset_args


def cleanup_stale_sandbox_profiles(*, legacy_dir: str | None = None) -> int:
    """Remove orphan sandbox files from <config_dir>/run/ and legacy /tmp.

    A file is removed when EITHER:
      - The tagged PID is dead (os.kill probe fails), OR
      - The file mtime is older than _LAUNCHER_MAX_AGE_SECONDS (the launcher
        is consumed exactly once at child exec, so old files are garbage
        regardless of PID liveness — this handles the spawner-PID design
        where the gateway PID is always alive for current-generation files).

    Also sweeps legacy /tmp/kirocrew_sandbox_*.py files that predate the
    migration to <config_dir>/run/ — these have no PID segment, so only the
    age threshold applies.

    Called from the periodic cleanup sweep in session.py, offloaded to the
    maintenance executor (blocking I/O).  Safe to call from sync contexts too.

    Returns:
        Number of stale files removed.
    """
    now = time.time()
    if legacy_dir is None:
        legacy_dir = _LEGACY_LAUNCHER_DIR
    run_dir = str(config_dir() / "run")
    removed = 0

    # ── Sweep <config_dir>/run/ (PID + age) ──
    if os.path.isdir(run_dir):
        for entry in os.listdir(run_dir):
            if not entry.startswith("kirocrew_sandbox_"):
                continue
            if entry.endswith(".sb"):
                suffix = ".sb"
            elif entry.endswith(".py"):
                suffix = ".py"
            else:
                continue
            filepath = os.path.join(run_dir, entry)
            # Age check first — handles the spawner-PID design flaw
            try:
                mtime = os.stat(filepath).st_mtime
            except OSError:
                continue
            if (now - mtime) > _LAUNCHER_MAX_AGE_SECONDS:
                try:
                    os.remove(filepath)
                    removed += 1
                except OSError:
                    pass
                continue
            # Fresh file — fall back to PID liveness check
            middle = entry[len("kirocrew_sandbox_") : -len(suffix)]
            pid_str = middle.split("_", 1)[0]
            if not pid_str.isdigit():
                continue
            # Liveness probe via the shim — NEVER raw os.kill(pid, 0), which
            # TERMINATES the target process on Windows (see platform_compat).
            try:
                alive = platform_compat.pid_exists(int(pid_str))
            except OverflowError:
                alive = False  # absurd PID digits from a corrupt filename — stale
            if not alive:
                try:
                    os.remove(filepath)
                    removed += 1
                except OSError:
                    pass

    # ── Sweep legacy /tmp/kirocrew_sandbox_*.py (age only, no PID segment) ──
    if os.path.isdir(legacy_dir):
        try:
            with os.scandir(legacy_dir) as it:
                for dentry in it:
                    if not dentry.name.startswith("kirocrew_sandbox_"):
                        continue
                    if not dentry.name.endswith(".py"):
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

    removed += _cleanup_retired_acp_snapshot_dir()
    return removed


def _cleanup_retired_acp_snapshot_dir() -> int:
    """Reclaim `<config_dir>/run/kiro-cli-snapshots` from before the in-place launch.

    KiroCrew used to copy the whole kiro-cli binary here per ACP spawn generation
    and exec the copy. The CLI is now launched in place, so nothing writes this
    tree — but an install upgrading past that change keeps every orphaned copy
    forever (observed: 196 MB in two ~100 MB copies). Nothing else reclaims them:
    the sweep above only matches `kirocrew_sandbox_*` FILES in this same dir, and
    the tree sits on `security._SENSITIVE_HOME_DIRS`, so the agent cannot delete
    it either — on request or otherwise. Best-effort and idempotent; returns 1
    when a tree was removed so the periodic sweep logs it.
    """

    retired = config_dir() / "run" / "kiro-cli-snapshots"
    if not retired.is_dir():
        return 0
    shutil.rmtree(retired, ignore_errors=True)
    # ignore_errors swallows partial failures, so report only a real removal.
    return 0 if retired.exists() else 1


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


def _allow_unsandboxed_exec() -> bool:
    """Whether the operator has explicitly opted into allowing execution
    without ANY sandbox backend (fail-open behavior).

    When False (default), wrap_argv will RAISE instead of returning unmodified
    argv when no sandbox backend is available. This is the fail-closed behavior
    required by a penetration-test finding.

    Read lazily from config to avoid an import cycle with the config loader.
    """
    try:
        from kiro_crew.config.loader import (
            KiroCrewConfig,  # circular import: sandbox is a low-level dep of config.loader
        )

        return bool(getattr(KiroCrewConfig.load().agent, "sandbox_allow_unsandboxed_exec", False))
    except Exception:
        return False


# The single environment marker that proves this process is already INSIDE a
# KiroCrew namespace sandbox. Deny-by-default: the gate keys ONLY on the
# explicit, single-purpose ``KIROCREW_SANDBOX_ACTIVE``, which is exported at
# exactly one site — the namespace launcher main() (see the export beside
# ``KIROCREW_HOST_PID``). We deliberately do NOT key on ``KIROCREW_HOST_PID``:
# it is dual-purpose session-identity plumbing, and gating a security-relevant
# passthrough on a variable set for other reasons is a latent bypass. Since the
# launcher sets ``KIROCREW_SANDBOX_ACTIVE`` at the same site, no fallback marker
# is needed. No unsandboxed code path sets this marker.
_IN_SANDBOX_MARKER = "KIROCREW_SANDBOX_ACTIVE"


def _inside_kirocrew_sandbox() -> bool:
    """True when this process already runs inside a KiroCrew OS sandbox.

    Nested sandboxing is impossible by design: the launcher's seccomp filter
    denies ``unshare``/``setns`` precisely so the sandboxed tree cannot
    manipulate namespaces. An in-sandbox wrap_argv call must therefore pass
    through rather than fail closed — the outer namespace still confines every
    descendant, so this is NOT the fail-open path. Failing closed here bricked
    every in-sandbox MCP spawn with unshare EPERM: the probe error was raised on
    every ctx.call_tool and silently swallowed by the caller.

    Detection is deny-by-default: gated solely on the explicit, launcher-only
    ``KIROCREW_SANDBOX_ACTIVE`` marker (see ``_IN_SANDBOX_MARKER``).
    """
    return bool(os.environ.get(_IN_SANDBOX_MARKER))


def _warn_no_isolation(mode: str) -> None:
    """Loudly surface that the agent subprocess is running WITHOUT OS-level
    isolation, so the fallback is never silent (CSE SEC-009).

    When no sandbox backend is available the credential paths (``~/.aws``,
    ``~/.ssh``, ...) are visible to the (untrusted) agent subprocess and only
    the bypassable app-level ``security.py`` checks remain. This is a real
    degradation of the security posture, so it is logged as a WARNING unless
    the operator has explicitly acknowledged it via
    ``agent.sandbox_allow_no_isolation``. Emitted once per process.
    """
    if getattr(wrap_argv, "_warned", False):
        return
    wrap_argv._warned = True  # type: ignore[attr-defined]
    if _allow_no_isolation():
        logger.info(
            "OS-level sandbox unavailable (mode=%s); running WITHOUT credential "
            "isolation. Operator opted in via agent.sandbox_allow_no_isolation; "
            "app-level checks are the only remaining boundary.",
            mode,
        )
        return
    logger.warning(
        "SECURITY: no OS-level sandbox backend is available on this host "
        "(mode=%s), so the agent subprocess runs WITHOUT credential isolation — "
        "~/.aws, ~/.ssh and other secrets are readable by it and only the "
        "bypassable app-level security.py checks remain. Install a supported "
        "sandbox (Linux user namespaces, or macOS < 26 sandbox-exec), or set "
        "agent.sandbox_allow_no_isolation=true in ~/.kiro/crew/config.json to "
        "acknowledge the risk and silence this warning.",
        mode,
    )


def detect_backend(config_mode: str = "auto") -> str:
    """Detect the best available sandbox backend.

    Cache policy (incident 2026-07-18 — one transient fork failure poisoned
    the cache and fail-closed every spawn for an hour until restart):

    - A positive result (``"namespace"``/``"sandbox-exec"``) is cached for the
      process lifetime — kernel capability does not change while running.
    - ``"none"`` is cached ONLY when the userns probe failure looks permanent
      (kernel refuses user namespaces: EPERM/EINVAL/ENOSYS). A transient
      resource failure (fork EAGAIN, EMFILE, ...) is never cached — the next
      spawn re-probes and self-heals.
    - ``config_mode="off"`` short-circuits to ``"none"`` without probing and
      without touching the cache. All other modes share one cache entry:
      backend capability is mode-independent, so mode alternation no longer
      forces pointless re-probes.
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
        transient, reason = _last_unshare_failure or (False, "no probe detail recorded")
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


def reset_backend() -> None:
    """Reset cached backend (for testing or config change)."""
    global _backend, _last_unshare_failure
    _backend = None
    _last_unshare_failure = None


# wrap_argv's ``mode`` vocabulary is a superset of the governance ``sandbox``
# ordinal scale: ``auto`` is an alias that resolves to ``standard`` below.  Only
# this alias mapping lives here; the strictness ORDER is owned solely by
# governance._ORDINAL_SCALES["sandbox"] (the single source of truth) — we never
# re-encode the order, so a new tier added there is honoured here without edit.
_SANDBOX_MODE_ALIASES = {"auto": "standard"}


def _clamp_sandbox_mode(mode: str) -> str:
    """Clamp *mode* UP to the governed ``sandbox.min_level`` floor, if any.

    Derives strictness ranking from the enforcer-owned ordinal registry
    (``OrdinalControl`` over ``_ORDINAL_SCALES['sandbox']``) — NOT a private
    duplicate table — so the floor cannot silently no-op if a tier is added to
    the scale.  Returns *mode* unchanged when there is no governance opinion or
    the floor is already satisfied.

    Fail-closed: a ``PlatformCompositionError`` (a non-standalone host that could
    not compose) propagates — the sandbox floor must never silently downgrade
    from DENY to ALLOW on the very host that is supposed to be governed.  Any
    OTHER (transient) error leaves *mode* as-is (a missing tighten is backstopped
    by the always-on controls), and an unknown floor/mode value raises rather
    than ranking it as 0 (which would fail open).
    """
    from kiro_crew.platform.context import PlatformCompositionError
    from kiro_crew.platform.governance import _ORDINAL_SCALES, OrdinalControl

    try:
        from kiro_crew.platform.governance_profiles import governance_floor_ordinal

        floor = governance_floor_ordinal("sandbox.min_level")
    except PlatformCompositionError:
        raise
    except Exception:
        return mode
    if not floor:
        return mode
    scale = _ORDINAL_SCALES["sandbox"]
    # The floor already validated through OrdinalControl inside
    # governance_floor_ordinal, so it is in-scale; an unrecognised caller mode is
    # treated as the loosest tier so the floor still clamps it UP (fail-closed —
    # never let an unknown mode skip the tighten).
    cur_value = _SANDBOX_MODE_ALIASES.get(mode, mode)
    floor_rank = OrdinalControl("sandbox", floor).rank()
    cur_rank = scale.index(cur_value) if cur_value in scale else -1
    if floor_rank <= cur_rank:
        return mode
    # The floor's scale value IS a valid wrap_argv mode (off/standard/cc/strict).
    return floor


def wrap_argv(
    argv: list[str],
    mode: str = "auto",
    *,
    strip_python_env: bool = False,
    extra_hidden_dirs: tuple[str, ...] = (),
    extra_visible_dirs: tuple[str, ...] = (),
    is_kiro_cli: bool | None = None,
) -> tuple[list[str], str | None]:
    """Wrap a command argv with OS-level sandbox if available.

    Args:
        argv: Original command + args.
        mode: ``"auto"``/``"standard"`` (expose .aws/.ssh/.kube),
              ``"cc"`` (hide .aws but expose .aws/config for Bedrock auth),
              ``"strict"`` (hide everything), ``"off"`` (no sandbox).
        extra_hidden_dirs: Additional absolute directory trees to deny.
        extra_visible_dirs: Trusted paths that must remain visible when an
            otherwise-hidden parent contains them.
        is_kiro_cli: Explicit executable classification for descriptor-backed
            Kiro snapshots whose launch path no longer has a ``kiro-cli``
            basename. ``None`` retains basename detection for other callers.

    Returns:
        (wrapped_argv, cleanup_path_or_None).
        *cleanup_path* is a temp file to delete after the child exits
        (macOS seatbelt profile or Linux launcher script).
        ``None`` when no cleanup is needed.

    Raises:
        RuntimeError: When no sandbox backend is available, mode is not "off",
            and ``agent.sandbox_allow_unsandboxed_exec`` is False (default).
            This is the fail-closed behavior — the agent subprocess is NOT
            allowed to run without OS-level isolation unless explicitly opted in.
    """
    # Governance ordinal floor: a policy/profile may require a MINIMUM sandbox
    # tier (off < standard < cc < strict).  Clamp the requested mode up to that
    # floor before resolving the level — so an enterprise "min_level: cc" makes
    # even a mode="off" call run confined.  Cheap no-op when ungoverned.
    mode = _clamp_sandbox_mode(mode)

    if mode == "off":
        return argv, None

    # Already inside a KiroCrew sandbox (script cron, sandboxed agent child):
    # the outer namespace + seccomp confine every descendant, and that seccomp
    # filter denies the unshare a nested backend would need. Pass through
    # within the existing isolation boundary.
    if _inside_kirocrew_sandbox():
        if not getattr(wrap_argv, "_nested_passthrough_logged", False):
            wrap_argv._nested_passthrough_logged = True  # type: ignore[attr-defined]
            logger.info(
                "wrap_argv: already inside a KiroCrew sandbox — nested OS "
                "sandboxing is unavailable by design (seccomp denies unshare); "
                "spawning within the existing isolation boundary"
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
                tool_name=argv[0] if argv else "unknown",
                tool_kind="subprocess",
                outcome="allowed",
                metadata={"reason": "nested_sandbox_passthrough", "mode": mode},
                critical=True,
            )
        except Exception:
            logger.warning(
                "SEL audit failed for nested-sandbox passthrough — proceeding "
                "unaudited: the outer namespace + seccomp still confine this "
                "spawn, and denying it would brick in-sandbox MCP calls whenever "
                "SEL is down",
                exc_info=True,
            )
        return argv, None

    # "auto"/"standard" allows git-over-SSH, AWS CLI, kubectl.
    # "cc" hides .aws (exposes only .aws/config for Bedrock credential_process).
    # "strict" hides everything.
    if mode == "strict":
        sandbox_level = "strict"
    elif mode == "cc":
        sandbox_level = "cc"
    else:
        sandbox_level = "standard"

    # macOS sandbox mutual exclusion: kiro-cli >= 2.13's internal sandbox cannot
    # initialize nested inside KiroCrew's seatbelt (kernel EPERM even under an
    # allow-all outer profile), so exactly one layer can own isolation. When
    # kiro's internal sandbox is enabled, it is that layer for kiro-cli spawns;
    # KiroCrew's sandbox stays on for everything else and whenever kiro's is off.
    # Checked before backend detection so delegation also applies where our own
    # probe found no backend. macOS only — Linux namespace isolation is
    # unaffected.
    kiro_spawn = _spawns_kiro_cli(argv) if is_kiro_cli is None else is_kiro_cli
    if sys.platform == "darwin" and kiro_spawn and kiro_internal_sandbox_enabled():
        if extra_hidden_dirs or extra_visible_dirs:
            # A delegated sandbox cannot enforce KiroCrew-specific path hides.
            # Keep the outer seatbelt for callers that require extra isolation.
            return sandbox_exec_argv(
                argv,
                sandbox_level,
                strip_python_env=strip_python_env,
                extra_hidden_dirs=extra_hidden_dirs,
                extra_visible_dirs=extra_visible_dirs,
            )
        return _delegate_to_kiro_internal_sandbox(
            argv, sandbox_level, strip_python_env=strip_python_env
        )

    backend = detect_backend(config_mode=mode)

    if backend == "namespace":
        if extra_hidden_dirs or extra_visible_dirs:
            wrapped = namespace_argv(
                argv,
                sandbox_level,
                strip_python_env=strip_python_env,
                extra_hidden_dirs=extra_hidden_dirs,
                extra_visible_dirs=extra_visible_dirs,
            )
        else:
            wrapped = namespace_argv(
                argv,
                sandbox_level,
                strip_python_env=strip_python_env,
            )
        # The launcher script is argv[1] — caller should clean it up
        return wrapped, wrapped[1]
    if backend == "sandbox-exec":
        if extra_hidden_dirs or extra_visible_dirs:
            return sandbox_exec_argv(
                argv,
                sandbox_level,
                strip_python_env=strip_python_env,
                extra_hidden_dirs=extra_hidden_dirs,
                extra_visible_dirs=extra_visible_dirs,
            )
        return sandbox_exec_argv(
            argv,
            sandbox_level,
            strip_python_env=strip_python_env,
        )

    if backend == "none":
        # FAIL-CLOSED: refuse to execute without sandbox unless explicitly opted in.
        # This addresses a penetration-test finding — the previous behavior silently
        # returned unmodified argv, allowing the agent subprocess to access all
        # credential paths without any OS-level isolation.
        if not _allow_unsandboxed_exec():
            transient, probe_reason = _last_unshare_failure or (
                False,
                "no probe detail recorded",
            )
            if transient:
                guidance = (
                    "This probe failure looks TRANSIENT (momentary resource "
                    "pressure) — it is not cached and the next spawn re-probes "
                    "automatically. Do NOT disable the sandbox for this; retry "
                    "instead. "
                )
            else:
                guidance = (
                    "If this host genuinely lacks a sandbox backend, set "
                    "agent.sandbox_allow_unsandboxed_exec=true in "
                    "~/.kiro/crew/config.json to explicitly allow unsandboxed "
                    "execution, or install a supported sandbox backend "
                    "(Linux user namespaces, or macOS sandbox-exec). "
                )
            # Emit SEL audit event for this security-relevant denial so it
            # appears in the tamper-evident audit log (security-review requirement).
            try:
                from kiro_crew.sel import sel  # circular import: sandbox is low-level

                sel().log_tool_invocation(
                    session_key="sandbox",
                    agent="system",
                    source="sandbox.wrap_argv",
                    tool_name=argv[0] if argv else "unknown",
                    tool_kind="subprocess",
                    outcome="denied",
                    error=(
                        "No sandbox backend available and allow_unsandboxed_exec "
                        f"is not set (probe: {probe_reason})"
                    ),
                )
            except Exception:
                logger.warning("Failed to emit SEL audit event for sandbox denial", exc_info=True)
            raise RuntimeError(
                "Sandbox backend unavailable and allow_unsandboxed_exec is not set. "
                "No OS-level sandbox backend is available on this host, and the "
                "agent subprocess cannot be safely isolated. "
                f"Probe detail: {probe_reason}. " + guidance
            )
        # Opted in: warn (or info) and return unmodified argv
        _warn_no_isolation(mode)
    return argv, None


# Environment keys always scrubbed from an agent-influenced subprocess'
# environment, regardless of sandbox backend. These are the credential-bearing
# names that must never reach a spawn whose command, arguments, or working
# directory the agent (or a hostile MCP-config / repo) can influence. The OS
# sandbox launcher already drops these when a backend is present (see
# ``ENV_PREFIXES`` in ``namespace_argv`` / ``sandbox_exec_argv``), but scrubbing
# at the parent level too means the guarantee holds even on the opted-in
# ``sandbox_allow_unsandboxed_exec`` fail-open path where no launcher runs.
# Prefix match via ``startswith`` (mirrors the launcher's ENV_PREFIXES check).
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
    the launcher's ENV_PREFIXES check.
    """
    return {
        k: v for k, v in env.items() if not any(k.startswith(p) for p in _AGENT_DENIED_ENV_KEYS)
    }


def sandboxed_spawn_argv(
    argv: list[str],
    mode: str = "standard",
    *,
    env: dict[str, str] | None = None,
    strip_python_env: bool = False,
    extra_hidden_dirs: tuple[str, ...] = (),
    extra_visible_dirs: tuple[str, ...] = (),
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
            otherwise-hidden parent contains them.

    Returns:
        ``(wrapped_argv, scrubbed_env, cleanup_path_or_None)``. The caller MUST
        pass *scrubbed_env* as the subprocess ``env=`` and unlink *cleanup_path*
        (a temp launcher/profile) after the child exits.
    """
    if extra_hidden_dirs or extra_visible_dirs:
        wrapped, cleanup = wrap_argv(
            argv,
            mode=mode,
            strip_python_env=strip_python_env,
            extra_hidden_dirs=extra_hidden_dirs,
            extra_visible_dirs=extra_visible_dirs,
        )
    else:
        wrapped, cleanup = wrap_argv(
            argv,
            mode=mode,
            strip_python_env=strip_python_env,
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
    # it. See docs/resource-protection.md.
    wrapped = cgroup_scope_argv(wrapped)
    # Positive-identity marker for the orphan sweep: every tree spawned through
    # this chokepoint (and its descendants, via env inheritance) is identifiable
    # as KiroCrew-spawned even when its cmdline carries no KiroCrew fingerprint
    # (e.g. ``npx @playwright/mcp``).
    scrubbed[KIROCREW_SPAWNED_ENV] = KIROCREW_SPAWNED_VALUE
    return wrapped, scrubbed, cleanup


# ── cgroup v2 scope enforcement (fork bomb + memory DoS) ──
# The RLIMIT preexec (resource_limit_preexec) caps a SINGLE process's FDs, but
# RLIMIT is the wrong tool for the finding's headline threats: RLIMIT_NPROC is
# per-real-UID (not per-spawn-subtree) and RLIMIT_AS caps virtual not resident
# memory. cgroup v2 pids.max / memory.max are the correct per-cgroup ceilings —
# they bound the agent + all its MCP-server/tool descendants as one unit, and
# the kernel enforces at fork()/alloc time (no reaper race). We place each
# agent-influenced spawn in a transient systemd --user --scope, which works
# UNPRIVILEGED when the user session has cgroup v2 delegation (pids + memory
# controllers). See docs/resource-protection.md.

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
    return _CGROUP_FALLBACK_MAX_MEMORY_MB


# Cached (available, reason) probe result — the environment doesn't change
# within a process, and the probe shells out, so compute it once.
_CGROUP_SCOPE_PROBE: tuple[bool, str] | None = None
_CGROUP_WARNED = False


def _probe_cgroup_scope() -> tuple[bool, str]:
    """Return (available, reason) for unprivileged cgroup-v2 scope enforcement.

    Requires, on Linux: a pure cgroup-v2 mount, the ``pids`` and ``memory``
    controllers delegated to our user slice, a ``systemd-run`` binary, and a
    user session bus (XDG_RUNTIME_DIR). Any missing piece → not available.
    """
    global _CGROUP_SCOPE_PROBE
    if _CGROUP_SCOPE_PROBE is None:
        _CGROUP_SCOPE_PROBE = _compute_cgroup_scope_probe()
    return _CGROUP_SCOPE_PROBE


def _compute_cgroup_scope_probe() -> tuple[bool, str]:
    """Uncached capability check backing :func:`_probe_cgroup_scope`."""
    if sys.platform != "linux":
        return (False, "not Linux")
    if shutil.which("systemd-run") is None:
        return (False, "systemd-run not found")
    # A user session bus is required for `systemd-run --user`.
    if not os.environ.get("XDG_RUNTIME_DIR"):
        return (False, "no XDG_RUNTIME_DIR (no systemd user session)")
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
        uid = os.getuid()
        ctrl_path = f"/sys/fs/cgroup/user.slice/user-{uid}.slice/cgroup.controllers"
        with open(ctrl_path, encoding="utf-8") as fh:
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
    properties on this check. Cached alongside the main probe (the environment
    is process-stable). Failure to read → False (skip CPU properties, keep
    pids/memory enforcement).
    """
    global _CPU_DELEGATED
    if _CPU_DELEGATED is None:
        try:
            uid = os.getuid()
            ctrl_path = f"/sys/fs/cgroup/user.slice/user-{uid}.slice/cgroup.controllers"
            with open(ctrl_path, encoding="utf-8") as fh:
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
        from kiro_crew.config.loader import _raw_config

        rl = _raw_config().get("resource_limits")
        if isinstance(rl, dict):
            p = rl.get("max_processes")
            if isinstance(p, (int, float)) and not isinstance(p, bool) and p > 0:
                max_procs = int(p)
            m = rl.get("max_memory_mb")
            if isinstance(m, (int, float)) and not isinstance(m, bool) and m > 0:
                max_mem_mb = int(m)
            w = rl.get("cpu_weight")
            if isinstance(w, (int, float)) and not isinstance(w, bool) and 1 <= w <= 10000:
                cpu_weight = int(w)
            q = rl.get("max_cpu_percent")
            if isinstance(q, (int, float)) and not isinstance(q, bool) and q > 0:
                max_cpu_percent = int(q)
    except Exception:
        logger.debug("cgroup limits: config unavailable, using defaults")
    return max_procs, max_mem_mb, cpu_weight, max_cpu_percent


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

    Layers OUTSIDE the OS-level sandbox: callers pass the already-``wrap_argv``-ed
    argv here so the child is filesystem-isolated AND cgroup-bounded.

    On a host without cgroup v2 delegation (older Linux, no systemd user
    session, macOS), returns *argv* unchanged and logs a one-time loud SECURITY
    warning — the RLIMIT_NOFILE preexec still applies, but the fork-bomb/memory
    DoS ceiling is NOT enforced there.
    """
    global _CGROUP_WARNED
    available, reason = _probe_cgroup_scope()
    if not available:
        if not _CGROUP_WARNED:
            _CGROUP_WARNED = True
            logger.warning(
                "SECURITY: cgroup v2 scope enforcement unavailable (%s); agent "
                "subprocess fork-bomb / memory-DoS ceilings are NOT enforced on "
                "this host. RLIMIT_NOFILE still applies. See "
                "docs/resource-protection.md.",
                reason,
            )
        return argv
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
        "systemd-run",
        "--user",
        "--scope",
        "-q",
        "--slice=kirocrew-agents.slice",
        *props,
        "--",
        *argv,
    ]


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
    exhaust the host out from under the gateway. Every agent-influenced spawn
    passes the result as ``preexec_fn=`` (see ``docs/resource-protection.md``).

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
    resource limits (NPROC, CPU, AS) are left at their sandbox values — a
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
        # Malformed operator values must not break the spawn — resource-limit
        # handling elsewhere ignores bad values, so mirror that here and fall
        # back to the ceiling (bools are ints in Python; exclude them).
        raw = limits.get("max_open_files")
        configured = raw if isinstance(raw, int) and not isinstance(raw, bool) else 0
        limits["max_open_files"] = max(configured, _BUILD_NOFILE_CEILING)
        _BUILD_RESOURCE_PREEXEC = apply_resource_limits({**(cfg or {}), "resource_limits": limits})
    return _BUILD_RESOURCE_PREEXEC  # type: ignore[return-value]
