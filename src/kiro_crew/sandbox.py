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

Config: ``"sandbox": "auto" | "off"`` in ``~/.kirocrew/config.json``.
``"auto"`` (default) uses namespace sandbox on Linux, seatbelt on macOS.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import functools
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew import platform_compat
from kiro_crew.platform import current_context

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# Launcher scripts and seatbelt profiles are read exactly once at child exec.
# Any file older than this threshold is garbage regardless of PID liveness.
_LAUNCHER_MAX_AGE_SECONDS = 3600

# Legacy sandbox launcher directory (before migration to ~/.kirocrew/run/).
_LEGACY_LAUNCHER_DIR = "/tmp"

# Sensitive directories to hide from the agent subprocess tree.
# "strict" mode hides all; "standard" mode only hides non-workflow dirs.
_STRICT_DIRS: list[str] = [
    ".aws",
    ".gnupg",
    ".gpg",
    ".config/gcloud",
    ".azure",
    ".docker",
    ".kube",
]

_STANDARD_DIRS: list[str] = [
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
    ".kirocrew/.env",
]

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
    "KIROCREW_OWNER_ID",
]


# ── Platform context accessor ──


def _sandbox_policy():
    """Return the active context's SandboxPolicy adapter.

    The Default adapter delegates to ``_STRICT_DIRS`` / ``_CC_DIRS`` above, so a
    standalone process gets today's exact lists; the Amazon companion extends
    them.
    """
    return current_context().sandbox


# ── Availability probes ──


def _probe_unshare() -> bool:
    """Return True if user + mount namespaces work (Linux)."""
    if sys.platform != "linux":
        return False
    try:
        _clone_newuser = 0x10000000
        _clone_newns = 0x00020000
        _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        _libc.unshare.argtypes = [ctypes.c_int]
        _libc.unshare.restype = ctypes.c_int
        pid = os.fork()
        if pid == 0:
            ret = _libc.unshare(_clone_newuser | _clone_newns)
            os._exit(0 if ret == 0 else 1)
        _, status = os.waitpid(pid, 0)
        return os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    except Exception:
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


# Native-executable magic numbers, split by platform so we never accept an ELF
# binary on macOS or a Mach-O binary on Linux (which would select an unrunnable
# target). Mach-O set covers thin 32/64-bit AND fat/universal 32-bit *and*
# 64-bit (FAT_MAGIC_64), both byte orders — a universal kiro-cli must not be
# rejected. Used to confirm a resolved candidate is a real binary rather than a
# shim, stub, or partial/corrupt download.
_ELF_MAGICS: tuple[bytes, ...] = (b"\x7fELF",)
_MACHO_MAGICS: tuple[bytes, ...] = (
    b"\xfe\xed\xfa\xce",  # MH_MAGIC     Mach-O 32-bit (BE)
    b"\xce\xfa\xed\xfe",  # MH_CIGAM     Mach-O 32-bit (LE)
    b"\xfe\xed\xfa\xcf",  # MH_MAGIC_64  Mach-O 64-bit (BE)
    b"\xcf\xfa\xed\xfe",  # MH_CIGAM_64  Mach-O 64-bit (LE)
    b"\xca\xfe\xba\xbe",  # FAT_MAGIC    universal/fat 32-bit (BE)
    b"\xbe\xba\xfe\xca",  # FAT_CIGAM    universal/fat 32-bit (LE)
    b"\xca\xfe\xba\xbf",  # FAT_MAGIC_64 universal/fat 64-bit (BE)
    b"\xbf\xba\xfe\xca",  # FAT_CIGAM_64 universal/fat 64-bit (LE)
)


def _native_magics() -> tuple[bytes, ...]:
    """Executable magics valid for the *current* platform."""
    return _MACHO_MAGICS if sys.platform == "darwin" else _ELF_MAGICS


def _is_native_kiro(p: Path) -> bool:
    """True if *p* is a runnable native kiro-cli binary.

    Requires (1) a regular file named ``kiro-cli``, (2) the platform execute
    bit (``platform_compat.is_executable_file``), and (3) a platform-correct
    native binary magic (ELF on Linux; Mach-O thin/fat incl. FAT_MAGIC_64 on
    macOS). The magic prefix is read through ``hooks.safe_read_prefix``, which
    enforces ``is_sensitive_path`` and opens with ``O_NOFOLLOW`` — so a
    ``kiro-cli``-named symlink pointing into a credential path is refused rather
    than read. Rejects shell shims, non-executable/partial installs, and stubs.
    """
    try:
        if p.name != "kiro-cli" or not p.is_file():
            return False
        if not platform_compat.is_executable_file(p):
            return False
        from kiro_crew.hooks import safe_read_prefix

        magic = safe_read_prefix(str(p), 4)
        if not magic:
            return False
        return any(magic.startswith(m) for m in _native_magics())
    except OSError:
        return False


def _is_toolbox_shim(path: str) -> bool:
    """True if *path* is the toolbox / aim-sandbox wrapper shim we must bypass.

    The shim is a small shell script that re-execs kiro-cli through
    ``aim sandbox``. We only redirect to the real binary when the supplied
    path is positively identified as this shim, so an explicit
    ``KIROCREW_KIRO_BIN`` override (even a custom ``kiro-cli`` script) is
    honored rather than silently replaced by a toolbox install.
    """
    from kiro_crew.hooks import safe_read_prefix

    head = safe_read_prefix(path, 4096)
    if not head or not head.startswith(b"#!"):
        return False
    return b"aim sandbox" in head or b"aim-sandbox" in head


def _resolve_real_kiro_bin(shim_path: str) -> str:
    """Resolve the real kiro-cli binary, bypassing any wrapper shim.

    On some installs ``kiro-cli`` is a bash shim that re-execs the real
    binary through a launcher (e.g. the Amazon toolbox shim routes through
    ``aim sandbox`` which creates its own seatbelt sandbox — nesting that
    inside KiroCrew's sandbox-exec fails on macOS 26+).

    Resolution order:
    1. If the supplied path resolves (through symlinks) to a native binary,
       use it — this honors an explicit ``KIROCREW_KIRO_BIN`` / PATH selection
       and the macOS ``~/.local/bin/kiro-cli`` -> ``.app`` symlink layout.
    2. If the supplied path is an explicit *non-toolbox* script override,
       return it unchanged (do not substitute a toolbox install).
    3. Otherwise (the path is the toolbox aim-sandbox shim, or missing) resolve
       via the toolbox-managed ``~/.local/bin/kiro-cli`` symlink, then the Linux
       ``$BUNDLE_ROOT/kiro-cli`` sibling of that symlink's target.

    Security: we deliberately do NOT enumerate ``~/.toolbox/tools/kiro-cli/<ver>``
    and pick the highest version. That directory is user-writable, so a
    compromised/sandboxed process could plant a higher-version binary and have
    it exec'd — including under the ``(allow default)`` capability probe. The
    installer's symlink is the single trusted pointer to the active version, so
    resolution follows it and nothing else.

    Every candidate must pass ``_is_native_kiro`` (executable + binary magic).
    Falls back to ``shim_path`` unchanged if nothing is found. Only attempts
    resolution when the basename is ``kiro-cli``.

    Intentionally uncached: it is a few ``stat``/``readlink`` calls (no
    subprocess, no directory scan), and caching risked returning a stale
    version across a toolbox upgrade that retains the old install.
    """
    if Path(shim_path).name != "kiro-cli":
        return shim_path
    home = Path.home()

    # 1. Supplied path already a native binary (after following symlinks). On
    #    macOS this resolves ~/.local/bin/kiro-cli -> the active version's
    #    .app binary directly, honoring an explicit KIROCREW_KIRO_BIN / PATH
    #    selection too.
    try:
        resolved = Path(shim_path).resolve(strict=True)
        if _is_native_kiro(resolved):
            return str(resolved)
    except (OSError, ValueError):
        pass

    # 2. Explicit non-toolbox script override → honor it unchanged. Only the
    #    toolbox aim-sandbox shim (or a missing/unusable path) triggers the
    #    trusted-symlink fallback below.
    if os.path.isfile(shim_path) and not _is_toolbox_shim(shim_path):
        return shim_path

    # 3a. Follow the toolbox-managed ~/.local/bin/kiro-cli symlink. This is the
    #     installer's trusted pointer to the ACTIVE version — NOT a scan of the
    #     user-writable version directory (see the security note above).
    try:
        local = Path(home / ".local" / "bin" / "kiro-cli").resolve(strict=True)
        if _is_native_kiro(local):
            return str(local)
    except (OSError, ValueError):
        pass

    # 3b. Linux $BUNDLE_ROOT/kiro-cli sibling of the resolved symlink target
    #     (pure realpath, non-blocking — no directory enumeration).
    for entry in [shim_path, str(home / ".local" / "bin" / "kiro-cli")]:
        try:
            self_path = os.path.realpath(entry)
        except OSError:
            continue
        candidate = Path(self_path).parent.parent / "kiro-cli"
        if str(candidate) != self_path and _is_native_kiro(candidate):
            return str(candidate)

    return shim_path


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
    # Amazon companion can extend them (+ .midway/.ada).  The Default adapter
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
        # (the file-level bind-mount of ~/.kirocrew/.env hides them on disk;
        # config/loader.py seeds them into os.environ for trusted children
        # only — sandboxed agents must not see them either way).
        env_prefixes = env_prefixes + list(_AGENT_DENIED_ENV_KEYS)
    if strip_python_env:
        # Foreign Python subprocess (kiro-cli's MCP servers) — do not let
        # KiroCrew's PYTHONPATH/PYTHONHOME leak in and shadow their own deps.
        env_prefixes = env_prefixes + list(_PYTHON_ENV_PREFIXES)
    hide_ssh = sandbox_level == "strict"
    dirs_json = json.dumps([os.path.join(home, d) for d in dirs])
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
# ``python ~/.kirocrew/run/kirocrew_sandbox_*.py``, so CPython prepends the
# script's own directory (sys.path[0], typically ~/.kirocrew/run/) to sys.path.
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
        # binds (e.g. /tmp on ext4 over ~/.kirocrew/.env on ext4) can corrupt the
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

        # ── Step 5: Drop capabilities + set NO_NEW_PRIVS (P472042955) ──
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

        # ── Step 6: Install seccomp-BPF filter (P472042955) ──
        # Deny mount/umount2/unshare/setns/pivot_root/link/linkat to prevent
        # the sandboxed process from undoing bind-mounts or creating hardlinks
        # to protected credential inodes (P472042777).
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
            # setns=308, pivot_root=155, link=86, linkat=265
            # aarch64: mount=40, umount2=39, unshare=97, setns=268,
            # pivot_root=41, link=N/A(use linkat=37), linkat=37
            import platform as _plat
            _machine = _plat.machine()
            if _machine == "x86_64":
                _DENY_SYSCALLS = (165, 166, 272, 308, 155, 86, 265)
            elif _machine == "aarch64":
                _DENY_SYSCALLS = (40, 39, 97, 268, 41, 37)
            else:
                _DENY_SYSCALLS = ()  # unknown arch — skip seccomp

            if _DENY_SYSCALLS:
                # Architecture constants for seccomp arch validation
                _AUDIT_ARCH_X86_64 = 0xC000003E
                _AUDIT_ARCH_AARCH64 = 0xC00000B7
                _SECCOMP_RET_KILL = 0x00000000
                _expected_arch = _AUDIT_ARCH_X86_64 if _machine == "x86_64" else _AUDIT_ARCH_AARCH64

                # BPF program: validate arch, load syscall number, compare
                # against deny list, return ERRNO(EPERM) on match, ALLOW otherwise.
                _insns = []
                # Load arch: BPF_LD | BPF_W | BPF_ABS, offset=4 (seccomp_data.arch)
                _insns.append(_struct.pack("<HBBI", _BPF_LD | _BPF_W | _BPF_ABS, 0, 0, 4))
                # If arch == expected, skip next insn (jt=1); else fall through to kill
                _insns.append(_struct.pack("<HBBI", _BPF_JMP | _BPF_JEQ | _BPF_K, 1, 0, _expected_arch))
                # Kill on unexpected arch (blocks i386 int 0x80 bypass)
                _insns.append(_struct.pack("<HBBI", _BPF_RET | _BPF_K, 0, 0, _SECCOMP_RET_KILL))
                # Load syscall number: BPF_LD | BPF_W | BPF_ABS, offset=0
                _insns.append(_struct.pack("<HBBI", _BPF_LD | _BPF_W | _BPF_ABS, 0, 0, 0))
                # For each denied syscall: JEQ -> deny
                _n_deny = len(_DENY_SYSCALLS)
                for _i, _nr in enumerate(_DENY_SYSCALLS):
                    _jt = _n_deny - _i  # jumps to the DENY RET
                    _insns.append(_struct.pack("<HBBI",
                        _BPF_JMP | _BPF_JEQ | _BPF_K, _jt, 0, _nr))
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

        # ── Step 7: Pre-exec hardlink scan (P472042777) ──
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
    """Create ~/.kirocrew/run/ with mode 0o700, falling back to system tmpdir on failure."""
    run_dir = os.path.join(os.path.expanduser("~"), ".kirocrew", "run")
    try:
        os.makedirs(run_dir, mode=0o700, exist_ok=True)
        # exist_ok does not re-apply mode on existing dirs — enforce explicitly.
        # 0o700 (owner-only rwx) is deliberately restrictive: this dir holds
        # per-session sandbox launcher scripts and sockets that must NOT be
        # world-readable. Semgrep's 0o644 suggestion is wrong for a directory
        # (needs the execute/traverse bit) and would loosen, not tighten, access.
        os.chmod(run_dir, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
    except OSError:
        logger.warning("Cannot create %s; falling back to system tmpdir", run_dir)
        run_dir = tempfile.gettempdir()
    return run_dir


def namespace_argv(
    argv: list[str],
    sandbox_level: str = "strict",
    *,
    strip_python_env: bool = False,
) -> list[str]:
    """Wrap *argv* via the Python namespace launcher.

    The launcher forks, the parent writes identity UID/GID maps, and the
    child bind-mounts empty dirs over credential paths before exec.
    The child retains the real UID/GID.
    """
    real_argv = list(argv)
    if real_argv:
        real_argv[0] = _resolve_real_kiro_bin(real_argv[0])

    script = _build_launcher_script(sandbox_level, strip_python_env=strip_python_env)
    run_dir = _ensure_run_dir()
    fd, path = tempfile.mkstemp(suffix=".py", prefix=f"kirocrew_sandbox_{os.getpid()}_", dir=run_dir)
    os.write(fd, script.encode())
    os.close(fd)
    platform_compat.chmod_safe(path, 0o700)

    return [sys.executable, path, *real_argv]


# ── Backend: macOS sandbox-exec ──

_SEATBELT_PROFILE = """\
(version 1)
(allow default)
{deny_rules}
"""


def _build_seatbelt_profile(sandbox_level: str = "strict") -> str:
    """Build a Seatbelt .sb profile denying reads of sensitive dirs."""
    home = str(Path.home())
    # Source the sensitive-dir lists from the active PlatformContext (Default
    # adapter == today's module globals; Amazon companion adds .midway/.ada).
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
        # AVP-23427: deny creating a HARDLINK whose target is under this dir.
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
        # AVP-23427: also deny hardlinking the protected file (see above).
        rules.append(f'(deny file-link (literal "{escaped}"))')

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
        # AVP-23427: block hardlinking any .ssh file (private keys) out of the
        # denied subtree.  Blanket over the whole subpath — no known_hosts
        # exception, since a hardlink to known_hosts has no legitimate use.
        rules.append(f'(deny file-link (subpath "{ssh_escaped}"))')

    return _SEATBELT_PROFILE.format(deny_rules="\n".join(rules))


def sandbox_exec_argv(
    argv: list[str],
    sandbox_level: str = "strict",
    *,
    strip_python_env: bool = False,
) -> tuple[list[str], str | None]:
    """Wrap *argv* with ``sandbox-exec -f <profile>``.

    Also scrubs sensitive env vars via ``env -u`` since Seatbelt only
    handles file-level deny rules, not environment variables.

    Returns (new_argv, tmp_profile_path).  Caller should delete the
    profile file after the child exits.
    """
    # Resolve the real kiro-cli binary to bypass wrapper shims (e.g. the
    # toolbox shim that calls ``aim sandbox``, which would nest a second
    # seatbelt inside ours and fail on macOS 26+).
    real_argv = list(argv)
    if real_argv:
        real_argv[0] = _resolve_real_kiro_bin(real_argv[0])

    profile = _build_seatbelt_profile(sandbox_level)
    run_dir = _ensure_run_dir()
    fd, path = tempfile.mkstemp(suffix=".sb", prefix=f"kirocrew_sandbox_{os.getpid()}_", dir=run_dir)
    os.write(fd, profile.encode())
    os.close(fd)
    # Build env -u flags for sensitive vars present in current env. cc/strict
    # additionally scrub agent-denied credential keys (Slack tokens, owner id)
    # since loader.py seeds them into os.environ for trusted children only.
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
    return ["env", *unset_args, "sandbox-exec", "-f", path, *real_argv], path


def cleanup_stale_sandbox_profiles(*, legacy_dir: str | None = None) -> int:
    """Remove orphan sandbox files from ~/.kirocrew/run/ and legacy /tmp.

    A file is removed when EITHER:
      - The tagged PID is dead (os.kill probe fails), OR
      - The file mtime is older than _LAUNCHER_MAX_AGE_SECONDS (the launcher
        is consumed exactly once at child exec, so old files are garbage
        regardless of PID liveness — this handles the spawner-PID design
        where the gateway PID is always alive for current-generation files).

    Also sweeps legacy /tmp/kirocrew_sandbox_*.py files that predate the
    migration to ~/.kirocrew/run/ — these have no PID segment, so only the
    age threshold applies.

    Called from the periodic cleanup sweep in session.py, offloaded to the
    maintenance executor (blocking I/O).  Safe to call from sync contexts too.

    Returns:
        Number of stale files removed.
    """
    now = time.time()
    if legacy_dir is None:
        legacy_dir = _LEGACY_LAUNCHER_DIR
    run_dir = os.path.join(os.path.expanduser("~"), ".kirocrew", "run")
    removed = 0

    # ── Sweep ~/.kirocrew/run/ (PID + age) ──
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
            middle = entry[len("kirocrew_sandbox_"):-len(suffix)]
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

    return removed


# ── Public API ──

_backend: str | None = None  # "namespace", "sandbox-exec", "none"
_backend_config_mode: str | None = None  # config mode when backend was cached


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
    required by pentest finding P472042906.

    Read lazily from config to avoid an import cycle with the config loader.
    """
    try:
        from kiro_crew.config.loader import (
            KiroCrewConfig,  # circular import: sandbox is a low-level dep of config.loader
        )

        return bool(getattr(KiroCrewConfig.load().agent, "sandbox_allow_unsandboxed_exec", False))
    except Exception:
        return False


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
        "agent.sandbox_allow_no_isolation=true in ~/.kirocrew/config.json to "
        "acknowledge the risk and silence this warning.",
        mode,
    )


def detect_backend(config_mode: str = "auto") -> str:
    """Detect the best available sandbox backend.

    Cached after first call; cache is invalidated if *config_mode* changes
    (e.g. user toggles agent.sandbox between "auto" and "off").
    """
    global _backend, _backend_config_mode
    if _backend is not None and _backend_config_mode == config_mode:
        return _backend
    # Invalidate on config change
    if _backend_config_mode != config_mode:
        _backend = None
        _backend_config_mode = config_mode
    if config_mode == "off":
        _backend = "none"
    elif userns_available():
        _backend = "namespace"
    elif _probe_sandbox_exec():
        _backend = "sandbox-exec"
    else:
        _backend = "none"
    logger.info("Sandbox backend: %s (config_mode=%s)", _backend, config_mode)
    return _backend


def reset_backend() -> None:
    """Reset cached backend (for testing or config change)."""
    global _backend, _backend_config_mode
    _backend = None
    _backend_config_mode = None


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
) -> tuple[list[str], str | None]:
    """Wrap a command argv with OS-level sandbox if available.

    Args:
        argv: Original command + args.
        mode: ``"auto"``/``"standard"`` (expose .aws/.ssh/.kube),
              ``"cc"`` (hide .aws but expose .aws/config for Bedrock auth),
              ``"strict"`` (hide everything), ``"off"`` (no sandbox).

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

    # "auto"/"standard" allows git-over-SSH, AWS CLI, kubectl.
    # "cc" hides .aws (exposes only .aws/config for Bedrock credential_process).
    # "strict" hides everything.
    if mode == "strict":
        sandbox_level = "strict"
    elif mode == "cc":
        sandbox_level = "cc"
    else:
        sandbox_level = "standard"

    backend = detect_backend(config_mode=mode)

    if backend == "namespace":
        wrapped = namespace_argv(argv, sandbox_level, strip_python_env=strip_python_env)
        # The launcher script is argv[1] — caller should clean it up
        return wrapped, wrapped[1]
    if backend == "sandbox-exec":
        return sandbox_exec_argv(argv, sandbox_level, strip_python_env=strip_python_env)

    if backend == "none":
        # FAIL-CLOSED: refuse to execute without sandbox unless explicitly opted in.
        # This addresses pentest finding P472042906 — the previous behavior silently
        # returned unmodified argv, allowing the agent subprocess to access all
        # credential paths without any OS-level isolation.
        if not _allow_unsandboxed_exec():
            # Emit SEL audit event for this security-relevant denial so it
            # appears in the tamper-evident audit log (AutoSDE requirement).
            try:
                from kiro_crew.sel import sel  # circular import: sandbox is low-level

                sel().log_tool_invocation(
                    session_key="sandbox",
                    agent="system",
                    source="sandbox.wrap_argv",
                    tool_name=argv[0] if argv else "unknown",
                    tool_kind="subprocess",
                    outcome="denied",
                    error="No sandbox backend available and allow_unsandboxed_exec is not set",
                )
            except Exception:
                logger.warning("Failed to emit SEL audit event for sandbox denial", exc_info=True)
            raise RuntimeError(
                "Sandbox backend unavailable and allow_unsandboxed_exec is not set. "
                "No OS-level sandbox backend is available on this host, and the "
                "agent subprocess cannot be safely isolated. Set "
                "agent.sandbox_allow_unsandboxed_exec=true in ~/.kirocrew/config.json "
                "to explicitly allow unsandboxed execution, or install a supported "
                "sandbox backend (Linux user namespaces, or macOS sandbox-exec)."
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
        k: v
        for k, v in env.items()
        if not any(k.startswith(p) for p in _AGENT_DENIED_ENV_KEYS)
    }


def sandboxed_spawn_argv(
    argv: list[str],
    mode: str = "standard",
    *,
    env: dict[str, str] | None = None,
    strip_python_env: bool = False,
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

    Returns:
        ``(wrapped_argv, scrubbed_env, cleanup_path_or_None)``. The caller MUST
        pass *scrubbed_env* as the subprocess ``env=`` and unlink *cleanup_path*
        (a temp launcher/profile) after the child exits.
    """
    wrapped, cleanup = wrap_argv(argv, mode=mode, strip_python_env=strip_python_env)
    # cgroup v2 scope (OUTERMOST layer): bound the spawned process tree with
    # pids.max + memory.max. Applied here so every sandboxed_spawn_argv caller
    # gets the fork-bomb / memory-DoS ceiling without threading it through each
    # site. No-op (with a one-time loud warning) where cgroup delegation is
    # unavailable. Safe re: the cleanup path — that is returned separately, not
    # re-derived from an argv index, so prepending systemd-run does not disturb
    # it. See docs/resource-protection.md (Talos bdf0d7e5).
    wrapped = cgroup_scope_argv(wrapped)
    # ``wrap_argv`` only strips PYTHONPATH/PYTHONHOME inside the launcher script,
    # so on the fail-open path (no sandbox backend, opted-in unsandboxed exec) it
    # returns argv unmodified and the strip never happens. Apply the same strip
    # to the scrubbed env here so ``strip_python_env=True`` holds regardless of
    # whether a backend is available.
    extra = _PYTHON_ENV_PREFIXES if strip_python_env else None
    return wrapped, scrub_env(env, extra_prefixes=extra), cleanup


# ── cgroup v2 scope enforcement (fork bomb + memory DoS) ──
# The RLIMIT preexec (resource_limit_preexec) caps a SINGLE process's FDs, but
# RLIMIT is the wrong tool for the finding's headline threats: RLIMIT_NPROC is
# per-real-UID (not per-spawn-subtree) and RLIMIT_AS caps virtual not resident
# memory. cgroup v2 pids.max / memory.max are the correct per-cgroup ceilings —
# they bound the agent + all its MCP-server/tool descendants as one unit, and
# the kernel enforces at fork()/alloc time (no reaper race). We place each
# agent-influenced spawn in a transient systemd --user --scope, which works
# UNPRIVILEGED when the user session has cgroup v2 delegation (pids + memory
# controllers). See docs/resource-protection.md (Talos bdf0d7e5).

# Default cgroup ceilings (per agent scope). Overridable via the same
# ``resource_limits`` config block used by apply_resource_limits.
_CGROUP_DEFAULT_MAX_PROCESSES = 1024  # pids.max — bounds fork bombs

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


def _cgroup_limits_from_config() -> tuple[int, int]:
    """Return ``(max_processes, max_memory_mb)`` for the cgroup scope.

    Reads the same ``resource_limits`` config block as apply_resource_limits;
    falls back to the module defaults. ``0`` (or junk) means "use default" for
    the cgroup ceiling — unlike the RLIMIT path, we never leave the cgroup DoS
    ceiling unset by default (that is the whole point of this control). The
    memory default is host-proportional (see :func:`_default_max_memory_mb`).
    """
    max_procs = _CGROUP_DEFAULT_MAX_PROCESSES
    max_mem_mb = _default_max_memory_mb()
    try:
        from kiro_crew.config.loader import _raw_config

        rl = _raw_config().get("resource_limits")
        if isinstance(rl, dict):
            p = rl.get("max_processes")
            if isinstance(p, (int, float)) and not isinstance(p, bool) and p > 0:
                max_procs = int(p)
            m = rl.get("max_memory_mb")
            if isinstance(m, (int, float)) and not isinstance(m, bool) and m > 0:
                max_mem_mb = int(m)
    except Exception:
        logger.debug("cgroup limits: config unavailable, using defaults")
    return max_procs, max_mem_mb


def cgroup_scope_argv(argv: list[str]) -> list[str]:
    """Wrap *argv* in a transient systemd --user --scope with cgroup v2 limits.

    Prepends ``systemd-run --user --scope`` with ``TasksMax`` (pids.max, the
    fork-bomb ceiling) and ``MemoryMax`` + ``MemorySwapMax=0`` (memory.max, the
    RSS balloon ceiling), so the spawned agent AND all its MCP-server/tool
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
    max_procs, max_mem_mb = _cgroup_limits_from_config()
    return [
        "systemd-run",
        "--user",
        "--scope",
        "-q",
        "--slice=kirocrew-agents.slice",
        "-p",
        f"TasksMax={max_procs}",
        "-p",
        f"MemoryMax={max_mem_mb}M",
        "-p",
        "MemorySwapMax=0",
        "--",
        *argv,
    ]


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
