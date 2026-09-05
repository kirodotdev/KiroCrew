"""Secure command execution and background-process lifecycle for Dev Fleet."""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.dev_fleet import npm_preflight, sync_runner
from kiro_crew.env import find_node_tool, node_bin_dirs
from kiro_crew.executors import subprocess_executor
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.sandbox import (
    RLIMIT_PROFILE_BUILD,
    create_subprocess_limited,
    sandboxed_spawn_argv,
    shielded_prepare_off_loop,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger("kiro_crew.apps.builtins.dev_fleet.server")


def _redact(text: str) -> str:
    """Apply both credential and exfiltration-URL redaction to output text."""
    text, _ = redact_credentials(text)
    text, _ = redact_exfiltration_urls(text)
    return text


def _redact_pr(pr: dict | None) -> dict | None:
    """Redact string display fields of a PR status dict (url, state, etc.)."""
    if not pr:
        return pr
    return {
        k: (_redact(v) if isinstance(v, str) else v)
        for k, v in pr.items()
        if not k.startswith("_")  # _repo etc. stay internal
    }


# --- stream watchdog deadline (module constant so tests can patch it) ---
_RUN_DEADLINE_S = 1800


# --- pod availability ---
# Two distinct flags, because they gate different things:
#   _POD_IMPORTED  — the ``kiro_crew.pod`` modules are importable, so the
#                    PLATFORM-NEUTRAL helpers (``prov.has_venv`` /
#                    ``prov.has_dist``, both plain filesystem checks) may be
#                    called. True on every platform unless the import failed.
#   _POD_AVAILABLE — pods can actually RUN here, i.e. Linux with ``systemctl``.
# Conflating the two used to report every worktree as "not built" off Linux,
# even though the build state is knowable everywhere.
_POD_IMPORTED = False
_POD_AVAILABLE = False
_POD_ERROR = ""
try:
    from kiro_crew.pod import provision as prov
    from kiro_crew.pod import runtime as rt
    from kiro_crew.pod.config import PodConfig

    _POD_IMPORTED = True
    # Pods are per-user service-manager units: systemd --user on Linux, launchd
    # user agents on macOS. On a platform with neither, skip pod-state checks
    # entirely instead of failing closed on every removal.
    #
    # NOTE this gate is about PODS only. Make-live (repointing the LIVE gateway's
    # unit) is a separate feature with its own Linux-only gates further down —
    # macOS support for pods deliberately does not imply macOS make-live.
    if sys.platform == "linux" and shutil.which("systemctl"):
        _POD_AVAILABLE = True
    elif sys.platform == "darwin" and shutil.which("launchctl"):
        _POD_AVAILABLE = True
    elif sys.platform == "darwin":
        _POD_ERROR = (
            "Pods are launchd user agents on macOS, but no `launchctl` was found " "on PATH."
        )
    elif sys.platform == "linux":
        _POD_ERROR = "Pods require `systemctl --user`, but no `systemctl` was found on PATH."
    else:
        _POD_ERROR = (
            f"Pods need systemd --user (Linux) or launchd (macOS); this host is "
            f"{sys.platform}. Preview a worktree with ./dev-backend.sh instead."
        )
except ImportError as exc:
    _POD_ERROR = f"the pod subsystem could not be imported: {exc}"


# --- async run tracking ---
_RUNS: dict[str, dict] = {}
_RUNS_LOCK = LoopBoundLock()
_SYNC_LOCK = LoopBoundLock()


def _find_cli() -> list[str]:
    """Invoke the kirocrew CLI as a module of OUR interpreter.

    Never resolved through the filesystem: a `kirocrew` shim planted in an
    agent-writable PATH entry (or venv bin) would become an absolute path
    that bypasses the trusted-binary gate. `sys.executable -m` pins the CLI
    to the exact code identity this backend is already running.

    Targets the ``kiro_crew`` PACKAGE (its ``__main__``), NOT ``kiro_crew.cli``:
    ``cli.py`` has no ``if __name__ == "__main__"`` guard, so
    ``python -m kiro_crew.cli <cmd>`` imports the module, runs no ``main()`` and
    exits 0 with NO output — turning every pod op (up/down/restart/provision)
    into a SILENT no-op the backend then reports as success (a stopped pod that
    keeps running, the confirmed "Stopped but still up" bug). The package
    ``__main__`` also performs the SSL-cert / UTF-8-console setup that must run
    before ``kiro_crew.cli`` is imported, so it is the only correct ``-m`` entry.
    """
    return [sys.executable, "-m", "kiro_crew"]


# Git hardening injected as ENVIRONMENT (same precedence as `git -c`, which
# overrides every config file) so EVERY git invocation from this handler —
# foreground inspection, the unattended background fetch, rebase, sync pull,
# and any git a build step runs — is neutralized at one chokepoint instead of
# per-call-site flags. The config keys are attacker-configurable via an
# agent-writable ``.git/config`` and would otherwise execute code:
#   * protocol pin  — ``ext::``/custom remote helpers refused by git itself
#   * core.fsmonitor / core.hooksPath — repo-registered executables
#   * credential.helper (reset to empty list) — helper commands
#   * core.sshCommand (pinned to plain ``ssh``) — arbitrary command on fetch
#
# GIT_NO_REPLACE_OBJECTS is the odd one out: not a config key and not about code
# execution, but about WHICH OBJECT GRAPH git answers from. A
# ``refs/replace/<oid>`` ref substitutes one object for another in every read, so
# ``log``, ``rev-list --count``, ``merge-base`` and ``merge --ff-only`` all answer
# about the SUBSTITUTE graph — a history no checked-out commit names. Every git
# answer this handler acts on is a statement about the checkout on disk, so the
# real graph is the only one that answers the question asked. Grafting is a
# legitimate local operation (``git replace``), so this is a correctness pin
# first and a tamper pin second, and it is an env var rather than a config pair
# so no config precedence applies to it at all. ``update_governance`` and
# ``auto_improvement``'s clone setup already pin it for the same reason.
# Harmless for non-git commands (pip/npm ignore GIT_*).
_GIT_ENV_NEUTRALIZERS: dict[str, str] = {
    "GIT_ALLOW_PROTOCOL": "https:ssh",
    "GIT_PROTOCOL_FROM_USER": "0",
    "GIT_NO_REPLACE_OBJECTS": "1",
    "GIT_CONFIG_COUNT": "4",
    "GIT_CONFIG_KEY_0": "core.fsmonitor",
    "GIT_CONFIG_VALUE_0": "false",
    "GIT_CONFIG_KEY_1": "core.hooksPath",
    "GIT_CONFIG_VALUE_1": "/dev/null",
    "GIT_CONFIG_KEY_2": "credential.helper",
    "GIT_CONFIG_VALUE_2": "",
    "GIT_CONFIG_KEY_3": "core.sshCommand",
    "GIT_CONFIG_VALUE_3": "ssh",
}

# The credential.helper reset above kills repo-injected helpers (the attack
# vector) but ALSO the operator's own GLOBAL helper (e.g. `gh auth
# git-credential`), breaking https pulls with "could not read Username".
# The global config file is operator-owned — outside the repo attack surface
# the neutralizer targets — so its helper entries are trusted and re-pinned
# AFTER the reset. Env precedence still guarantees a repo-level helper can
# never win. Loaded once at startup; None means "not loaded yet" (probe-safe).
_GIT_TRUSTED_HELPERS: dict[str, str] | None = None


# Non-persistent OS-keychain helpers: credentials go to the system keychain,
# never to an attacker-readable file. `store` and `cache` are deliberately
# EXCLUDED (they persist/relay secrets and accept file-path arguments).
_KEYCHAIN_HELPER_NAMES = frozenset(
    {"osxkeychain", "manager", "manager-core", "libsecret", "wincred"}
)


def _sanitize_helper_value(val: str) -> str | None:
    """Map a configured credential helper to a SYNTHESIZED trusted command.

    ``~/.gitconfig`` is same-user writable — strict-tier build code can edit
    it, and any helper loaded at the NEXT startup runs in the
    credential-bearing standard tier AND receives the acquired secret on
    stdin via git's ``store`` action. Provenance of the first executable is
    NOT sufficient: ``!/usr/bin/sh -c '...'`` has a trusted argv[0] but
    exfiltrates the token through its arguments. So the configured value is
    never executed as-is; it only SELECTS from a fixed allowlist:

    - a ``!<anything ending in gh> auth git-credential`` shape (exactly
      three argv tokens) selects the gh helper, re-synthesized from
      ``_trusted_bin("gh")`` (system dirs or the operator unit-file
      override) — the configured path itself is discarded;
    - a bare single-token OS-keychain helper name (osxkeychain, manager,
      manager-core, libsecret, wincred) passes through and resolves as
      ``git-credential-<name>`` via git's exec path under OUR pinned PATH;
    - persistent helpers (``store``, ``cache``), arbitrary ``!`` commands,
      absolute paths, and any helper carrying arguments are rejected.

    Returns the trusted helper value, or ``None`` to reject.
    """
    if not val:
        return None
    if val.startswith("!"):
        try:
            argv = shlex.split(val[1:])
        except ValueError:
            return None
        if len(argv) != 3 or argv[1:] != ["auth", "git-credential"]:
            return None
        gh_names = ("gh", "gh.exe") if platform_compat.IS_WINDOWS else ("gh",)
        if Path(argv[0]).name not in gh_names:
            return None
        trusted_gh = _trusted_bin("gh")
        if trusted_gh is None:
            return None
        return f"!{trusted_gh} auth git-credential"
    if len(val.split()) != 1:
        return None
    return val if val in _KEYCHAIN_HELPER_NAMES else None


if platform_compat.IS_WINDOWS:  # pragma: no cover - exercised on Windows hosts
    _TRUSTED_BIN_DIRS = tuple(
        str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / sub)
        for sub in (r"Git\cmd", r"Git\bin", "GitHub CLI", "nodejs")
    ) + (str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"),)
else:
    # Homebrew/Linuxbrew prefixes are included: they are where a `gh` (and often
    # `git`) the user installed themselves actually lives, and the resolved-target
    # checks below still reject anything writable by us or under $HOME. Without
    # them a stock `brew install gh` was invisible to Dev Fleet.
    _TRUSTED_BIN_DIRS = (
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/opt/homebrew/bin",
        "/home/linuxbrew/.linuxbrew/bin",
    )
_TRUSTED_PATH = os.pathsep.join(_TRUSTED_BIN_DIRS)
_TRUSTED_BIN_CACHE: dict[str, str | None] = {}
_BUILD_PATH_CACHE: str | None = None


def _build_path() -> str:
    """``_TRUSTED_PATH`` with the node toolchain dirs prepended.

    BLOCKING: ``node_bin_dirs()`` walks the filesystem (globs + stats + one
    small read). On an NFS-backed ``$HOME`` those are not microseconds, so this
    must never be first-called on the event loop — it would stall every backend
    request and health check behind one directory scan.

    Callers on an async path therefore await :func:`_warm_build_path` first,
    which resolves it on ``subprocess_executor()``. After that the underlying
    resolver is ``lru_cache``d and this is a pure in-memory read, which is why
    :func:`_build_env` can stay synchronous.
    """
    global _BUILD_PATH_CACHE
    if _BUILD_PATH_CACHE is None:
        _BUILD_PATH_CACHE = os.pathsep.join([*node_bin_dirs(), _TRUSTED_PATH])
    return _BUILD_PATH_CACHE


async def _warm_build_path() -> None:
    """Resolve the node toolchain off the event loop, once per process.

    Idempotent and cheap after the first call (a ``None`` check). Called from
    ``dev_fleet_startup`` so the common case is warm before any request, and
    again at the top of every async handler that constructs a build env — a
    handler must not depend on startup having run (tests, and any future entry
    point that skips it).
    """
    if _BUILD_PATH_CACHE is not None:
        return
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(subprocess_executor(), _build_path)


def _invalidate_toolchain_cache() -> None:
    """Forget the memoized node-toolchain resolution.

    Both layers are memoized for the process lifetime: ``node_bin_dirs()`` is
    ``lru_cache``d and ``_BUILD_PATH_CACHE`` is filled once. That is right for a
    hot path but wrong for a REMEDY: the "npm not found" banner tells the user to
    run ``ensure-node.sh``, which writes the marker file this resolver reads — so
    without dropping both caches a long-lived gateway would keep serving the same
    error after the user had already fixed the host. Called only from the
    not-found path, so a working resolution is never discarded.
    """
    global _BUILD_PATH_CACHE
    _BUILD_PATH_CACHE = None
    node_bin_dirs.cache_clear()


# Upper bound on a propagated "sandbox unavailable" message. Wide enough to
# carry the sandbox layer's remedy sentence (the actionable half, appended after
# a ~180-char preamble) into the Discovery Error banner, while still bounding an
# arbitrarily long stderr.
_SANDBOX_ERR_MAX = 900

# Upper bound on a propagated generic git-discovery error. Git's own failure
# messages are short ("fatal: not a git repository", "cannot change to ..."),
# so a tight cap keeps the Discovery Error banner readable while still bounding
# an arbitrarily long stderr from a broken repo.
_GIT_ERR_MAX = 300

# Identity for the "no trusted executable" failure, so callers can branch on
# the CLASS of failure instead of re-matching prose: `_run_cmd` puts this
# prefix on the stderr it synthesizes when `_trusted_bin` resolves nothing,
# and error paths that want to name the remedy (set the per-tool override in
# the service environment) test `startswith` on it. Deliberately a constant,
# not an exception type: `_run_cmd` reports every failure through its
# (rc, stdout, stderr) tuple and callers already handle it that way.
_UNRESOLVED_TOOL_PREFIX = "no trusted executable for "


def _bin_override_var(name: str) -> str:
    """Env var that overrides trusted-bin resolution for *name*.

    Single source of truth shared by `_trusted_bin` (which reads it) and the
    user-facing remedy messages (which name it) — deriving it twice is how the
    advertised remedy drifts from the one that works.
    """
    return f"KIROCREW_DEVFLEET_BIN_{name.upper().replace('-', '_')}"


def _unresolved_tool_message(name: str) -> str:
    """User-facing message for an unresolved trusted tool.

    Blames the HOST toolchain, not the checkout (issue #2530: the previous
    wording folded this failure into "git worktree discovery failed in
    <repo>", sending users to debug a healthy repository), and names the
    operator remedy in the same voice as the missing-checkout branch. The
    trusted-PATH detail stays in the log line, not here: it is unactionable
    noise in a UI banner.
    """
    return (
        f"no trusted {name!r} executable found on this host — the checkout "
        f"itself is not the problem. Set {_bin_override_var(name)} to an "
        f"absolute path in the gateway's service environment (it needs a "
        "restart to be seen)."
    )


def _trusted_bin(name: str) -> str | None:
    """Resolve *name* to a canonical executable in a system or Homebrew bin dir.

    The service PATH starts with agent-writable directories (worktree venv,
    ~/.local/bin) — resolving through it would let a planted `git`/`gh`
    shim run inside the credential-bearing standard tier. Only executables
    physically inside the trusted dirs whose resolved target is unwritable by
    us and outside $HOME qualify; fail closed otherwise.
    """
    if name in _TRUSTED_BIN_CACHE:
        return _TRUSTED_BIN_CACHE[name]
    resolved: str | None = None
    # Operator escape hatch for hosts where the tool lives outside the
    # system dirs (e.g. gh in ~/.local/bin): an explicit absolute path set
    # in the SERVICE environment (operator-owned unit file), never derived
    # from the inherited PATH.
    override = os.environ.get(_bin_override_var(name))
    if (
        override
        and Path(override).is_absolute()
        and Path(override).is_file()
        and os.access(override, os.X_OK)
    ):
        _TRUSTED_BIN_CACHE[name] = override
        return override
    suffixes = ("", ".exe", ".cmd") if platform_compat.IS_WINDOWS else ("",)
    for d in _TRUSTED_BIN_DIRS:
        for suffix in suffixes:
            cand = Path(d) / (name + suffix)
            try:
                if not (cand.is_file() and os.access(cand, os.X_OK)):
                    continue
                # System binaries legitimately symlink outside the bin dirs
                # (e.g. /usr/bin/npm -> /usr/lib/node_modules/...). Require
                # the RESOLVED target to be system-owned: root uid, not
                # writable by others, and never under the user's HOME.
                real = cand.resolve()
                st = real.stat()
                if str(real).startswith(str(Path.home().resolve()) + os.sep):
                    continue
                # System-owned invariant that survives userns uid mapping:
                # the resolved target must not be writable by US and must
                # carry no group/other write bits. A user-planted shim is
                # writable by its planter; real system binaries are not.
                if platform_compat.IS_POSIX and (os.access(real, os.W_OK) or st.st_mode & 0o022):
                    continue
                # Pin the RESOLVED target, not the entry we searched: a bin-dir
                # entry can itself be a user-writable symlink (Homebrew's
                # `bin/gh -> ../Cellar/...`), so caching the link path would let
                # it be repointed between validation and execution. The real
                # path we just vetted is what gets spawned.
                resolved = str(real)
                break
            except OSError:
                continue
        if resolved:
            break
    _TRUSTED_BIN_CACHE[name] = resolved
    return resolved


def _toolchain_bin(name: str) -> str | None:
    """Resolve a NODE-TOOLCHAIN executable (``npm``/``node``/``npx``).

    Deliberately NOT ``_trusted_bin``. That function fails closed on anything
    under ``$HOME`` or writable by us, because it resolves ``git``/``gh`` -- the
    binaries that run in the CREDENTIAL-BEARING standard tier, where a planted
    shim would exfiltrate the operator's token. npm is a different case in both
    directions:

    * It is only ever spawned in the ``strict`` tier under
      :func:`_build_env` (no credential helpers), and it already executes
      worktree-controlled ``package.json`` scripts -- arbitrary code, by design.
      Requiring a system-owned npm buys nothing there.
    * Kiro Crew's own supported installer (``install.sh --mise`` /
      ``ensure-node.sh``) puts node under ``$HOME``, so ``_trusted_bin`` returned
      ``None`` for npm on exactly the hosts Kiro Crew set up itself, and Pull+Build
      failed with "no trusted executable for 'npm'".

    Managed toolchain first, system npm second: a distribution's node can be
    older than ``website/package.json``'s ``engines`` (Amazon Linux 2023 ships
    node 18 against ``>=22``), while ``ensure-node.sh`` installs a version
    chosen to satisfy the build.
    """
    return find_node_tool(name, _TRUSTED_PATH) or _trusted_bin(name)


async def _run_cmd(
    cmd: list[str],
    *,
    cwd: str | None = None,
    env: dict | None = None,
    timeout: int = 30,
    mode: str = "standard",
) -> tuple[int, str, str]:
    """Run a subprocess asynchronously, return (returncode, stdout, stderr).

    Every spawn routes through ``sandboxed_spawn_argv`` (OS isolation +
    credential-scrubbed env): these commands run against agent-influenced
    repositories whose config can execute code, so the gateway's
    credential-bearing environment must never reach them.

    ``_GIT_ENV_NEUTRALIZERS`` pins transports AND neutralizes every
    repo-controlled execution vector (fsmonitor/hooks/credential
    helper/sshCommand) for every git this handler ever runs.
    """
    base_env = dict(env) if env is not None else dict(os.environ)
    # Pin executable + PATH to trusted system dirs: the inherited service
    # PATH begins with agent-writable dirs, where a planted git/gh shim
    # would otherwise run with workflow credentials on every auto-refresh.
    if cmd and "/" not in cmd[0]:
        trusted = _trusted_bin(cmd[0])
        if trusted is None:
            return -1, "", (f"{_UNRESOLVED_TOOL_PREFIX}{cmd[0]!r} in {_TRUSTED_PATH}")
        cmd = [trusted, *cmd[1:]]
    base_env["PATH"] = _TRUSTED_PATH
    base_env.update(_GIT_ENV_NEUTRALIZERS)
    # Credential helpers only for gateway-controlled commands at "standard"
    # (background fetch, PR queries). "strict" invocations run in the
    # repo-controlled tier (rebase applying worktree commits) and get none.
    if mode == "standard" and _GIT_TRUSTED_HELPERS:
        base_env.update(_GIT_TRUSTED_HELPERS)
    cleanup: str | None = None
    try:
        # sandboxed_spawn_argv can cold-probe the sandbox backend with a
        # synchronous subprocess (blocking base rule) — run it on the executor.
        cmd, env, cleanup = await shielded_prepare_off_loop(
            functools.partial(sandboxed_spawn_argv, cmd, mode, env=base_env),
            executor=subprocess_executor(),
        )
    except RuntimeError as exc:
        # Fail closed: no sandbox backend and unsandboxed exec not opted in.
        return -1, "", f"sandbox unavailable: {exc}"
    try:
        proc = await create_subprocess_limited(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            # Kernel RLIMIT ceilings for the sandboxed child (fork bomb / FD /
            # mem / CPU) — required for every chokepoint-routed spawn.
            # Own process group so a timeout kill reaps descendants (e.g.
            # `pod up` spawning pip), matching _start_run.
            start_new_session=platform_compat.IS_POSIX,
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
                if platform_compat.IS_WINDOWS
                else 0
            ),
        )
    except OSError as exc:
        if cleanup:
            try:
                os.unlink(cleanup)
            except OSError:
                pass
        return -1, "", f"spawn failed: {exc}"
    try:
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            await _kill_tree(proc.pid)
            await platform_compat.kill_and_reap(proc)
            return -1, "", f"timeout ({timeout}s)"
        except asyncio.CancelledError:
            # Backend shutdown/restart cancels in-flight handlers: the child
            # runs in its own process group and would outlive us (a canceled
            # rebase never reaches its --abort path, wedging the worktree).
            # kill_and_reap is best-effort throughout, so an already-reaped
            # child cannot REPLACE the in-flight CancelledError with
            # ProcessLookupError and swallow the cancellation.
            await _kill_tree(proc.pid)
            await platform_compat.kill_and_reap(proc)
            raise
        return (
            proc.returncode or 0,
            (stdout or b"").decode(errors="replace"),
            (stderr or b"").decode(errors="replace"),
        )
    finally:
        if cleanup:
            try:
                os.unlink(cleanup)
            except OSError:
                pass


async def _run_uninterruptible(coro: Any) -> Any:
    """Await *coro* to completion even if THIS caller is cancelled.

    ``asyncio.shield`` alone is not enough for a destructive, lock-guarded git
    mutation: it stops the cancellation from reaching the inner command, but
    the outer ``await`` still raises ``CancelledError`` immediately, so the
    caller unwinds -- releasing _GIT_MUTATION_LOCK / _MAKE_LIVE_LOCK -- while
    the detached git child is still writing, and a new mutation could race it.

    Run the coroutine as a task and, if a cancellation lands on our await,
    keep re-awaiting (shielded) until the task is actually done before
    re-raising. Repeat cancellations (e.g. a shutdown hard-timeout after the
    first cancel) are absorbed only for the drain -- the same pattern the run
    worker uses to reap a mid-spawn subprocess. The inner git command is
    ``_run_cmd``, which is timeout-bounded, so the drain terminates.
    """
    task = asyncio.ensure_future(coro)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            if cancelled:
                raise asyncio.CancelledError
            return result
        except asyncio.CancelledError:
            cancelled = True
            if not task.done():
                continue
            # Task finished; propagate the cancellation the caller requested,
            # but only after the mutation is complete.
            raise


def _kill_tree_sync(pid: int) -> None:
    """Kill *pid*'s group, then any descendant that escaped it.

    The group kill alone is not sufficient: a descendant spawned with its own
    session (``start_new_session`` / ``CREATE_NEW_PROCESS_GROUP``) sits in a
    different process group, so POSIX ``killpg`` never reaches it. Sync/provision
    run worktree-controlled build tooling that does exactly this, and an escaped
    npm/vite keeps rewriting ``website/dist`` after the run is declared dead —
    a later sync then stages a bundle a live writer is still mutating.

    Descendants are enumerated FIRST: killing reparents survivors to init and
    erases the PPID links that identify them. Each survivor is killed via its
    own tree kill so a nested group (npm -> vite) goes down with it.
    """

    descendants = platform_compat.process_descendants(pid)
    try:
        platform_compat.kill_process_tree(pid)
    except (ProcessLookupError, OSError, ValueError):
        pass
    for child in descendants:
        try:
            platform_compat.kill_process_tree(child)
        except (ProcessLookupError, OSError, ValueError):
            # Already reaped by the group kill, or a pid we may no longer
            # signal — the primary kill has happened either way.
            continue


async def _kill_tree(pid: int) -> None:
    """Kill a process tree without blocking the event loop (taskkill/killpg/ps
    are synchronous syscalls/subprocesses — run them on the executor)."""
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(subprocess_executor(), _kill_tree_sync, pid)
    except (ProcessLookupError, OSError):
        pass


# Active background runs: rid -> (worker task, subprocess). Tracked so
# gateway cleanup can kill process trees instead of orphaning pip/npm.
_ACTIVE_RUNS: dict[str, tuple[asyncio.Task, Any]] = {}

# Shutdown admission control: once dev_fleet_cleanup starts, no new run may
# register in _ACTIVE_RUNS.  The lock is held only for the two fast dict
# operations that constitute the critical section (read flag + register, or
# set flag + snapshot) — it is never held across slow kill/await calls, so
# there is no risk of asyncio lock contention or done-callback deadlocks.
# LoopBoundLock (not a bare asyncio.Lock) because a module-global primitive
# binds to the import-time loop and raises RuntimeError from any other loop
# (Python 3.10+, see #4800) — this module is imported once but serves
# whichever loop the gateway runs.
_SHUTDOWN_ADMISSION_LOCK = LoopBoundLock()
_SHUTDOWN_IN_PROGRESS = False


_RUNS_MAX_COMPLETED = 50


#: ``_start_run`` label of the sync. The diagnosis stamp is gated on it because
#: the sync runner is the only script that enforces the reserved-code
#: reservation; a `provision` run reaches the same stamp while executing an
#: agent-authored branch, and must not be able to assert a cause.
_SYNC_RUN_LABEL = "sync"


#: The preflight probe's source, captured ONCE at import.
#:
#: The snapshot has to be of the code THIS gateway is running, not of whatever is
#: on disk when a sync happens. Copying the file at sync time left a window from
#: gateway start until the button press in which the module could be rewritten,
#: and the copy is then executed as the one step trusted to assert a failure
#: cause. Reading at import closes that: these bytes are the same ones the
#: running process imported.
#:
#: ``None`` when the source cannot be read (a frozen or zipimported install has
#: no readable ``__file__``). The sync REFUSES in that case rather than falling
#: back to reading the file later -- the fallback is exactly the window this
#: exists to remove, and refusing is the safe direction.
try:
    _PREFLIGHT_SOURCE: bytes | None = Path(npm_preflight.__file__).read_bytes()
except OSError:  # pragma: no cover - frozen/zipimported install
    _PREFLIGHT_SOURCE = None

#: The sync runner's source, captured ONCE at import — same contract, same
#: reasons as :data:`_PREFLIGHT_SOURCE` above: the snapshot must be of the code
#: THIS gateway is running, and ``None`` (unreadable ``__file__``) makes the
#: sync REFUSE rather than fall back to reading the file later.
try:
    _SYNC_RUNNER_SOURCE: bytes | None = Path(sync_runner.__file__).read_bytes()
except OSError:  # pragma: no cover - frozen/zipimported install
    _SYNC_RUNNER_SOURCE = None

#: Label of the ONE sync step whose binary is ours, so its exit code can be
#: trusted to mean what :mod:`npm_preflight` says it means. Every other step runs
#: worktree-controlled code and can exit any number it likes, so a reserved code
#: coming from one of those is remapped rather than believed.
#:
#: Named for the OUTCOME rather than the mechanism: this step runs a real install
#: against the incoming lockfile, which on a cold cache is minutes of apparent
#: silence under a 900s timeout. "Preflight" is a term the product does not use
#: anywhere else, and an invented word next to a spinner reads as a hang; "Verify
#: dependencies" reads as work. The runner's trust gate compares against this
#: constant, so the display name and the gate cannot drift apart.
_PREFLIGHT_LABEL = "Verify dependencies"


def _parse_step_marker(text: str) -> tuple[int | None, str | None]:
    """Parse a ``::step::<idx>::<label>`` progress marker into (index, label).

    The sync/build script emits one marker per step (see _sync_start_locked).
    The run worker records the parsed index AND label into the run entry so the
    dashboard can name the CURRENT step ("npm ci") instead of showing a bare
    percentage -- both survive the 60-line output tail window a chatty build
    step would otherwise flush the marker out of. Either element is ``None``
    when absent/malformed; a non-``::step::`` line yields ``(None, None)``.
    """
    if not text.startswith("::step::"):
        return None, None
    parts = text.split("::", 4)  # ['', 'step', '<idx>', '<label>', <rest>]
    idx = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else None
    label = parts[3] if len(parts) >= 4 and parts[3] else None
    return idx, label


async def _start_run(
    label: str,
    cmd: list[str],
    *,
    cwd: str | None = None,
    env: dict | None = None,
    cleanup_paths: list[str] | None = None,
) -> str:
    """Start a background subprocess with output streaming and watchdog.

    ``cleanup_paths``: sandbox launcher/profile temp files from
    ``sandboxed_spawn_argv`` — deleted when the run finishes.
    """
    rid = uuid.uuid4().hex[:12]
    # The run KIND, captured before the output loop can touch it. `label` is
    # rebound inside that loop by the `::step::` handler, so by completion it
    # holds the last STEP's label on a run that emits markers and is UNBOUND on
    # one that does not -- reading it at the diagnosis stamp would suppress
    # every real cause on the sync path and raise NameError on a provision.
    run_kind = label
    async with _RUNS_LOCK:
        # Bound memory: evict the oldest COMPLETED runs beyond the cap
        # (running entries are never evicted — reattach depends on them).
        done = sorted(
            (k for k, v in _RUNS.items() if v.get("status") != "running"),
            key=lambda k: _RUNS[k].get("started", 0.0),
        )
        for k in done[: max(0, len(done) - _RUNS_MAX_COMPLETED + 1)]:
            _RUNS.pop(k, None)
        _RUNS[rid] = {
            "status": "running",
            "exit_code": None,
            "label": label,
            "output": [],
            "started": time.time(),
        }

    async def worker() -> None:
        proc: Any = None
        spawn_task: asyncio.Task | None = None
        try:
            try:
                # Spawn on a child task and shield the await. A CancelledError
                # (gateway shutdown cancels in-flight run tasks) that arrives
                # WHILE asyncio is mid-exec would otherwise abandon the child:
                # the OS process is already forked+exec'd but the Process handle
                # is never returned, so nothing can reap it and it outlives the
                # gateway, still mutating the shared checkout. The spawn runs to
                # completion on its own task regardless of our cancellation; the
                # handler below retrieves the handle from ``spawn_task`` and
                # reaps it even when the shielded await itself raised.
                spawn_task = asyncio.ensure_future(
                    create_subprocess_limited(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                        cwd=cwd,
                        env=env,
                        # Kernel RLIMIT ceilings: sync/provision execute
                        # worktree-controlled pip/npm code; on hosts without
                        # delegated cgroup v2 the scope limiter is a no-op, so
                        # the per-process rlimit backstop must be present. Build
                        # variant: vite/npm need thousands of descriptors — the
                        # default 1024 NOFILE hard cap EMFILEs the SPA build.
                        profile=RLIMIT_PROFILE_BUILD,
                        # Own process group so a timeout kill reaps descendants
                        # (pip/npm children), not just the immediate CLI process.
                        start_new_session=platform_compat.IS_POSIX,
                        creationflags=(
                            subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
                            if platform_compat.IS_WINDOWS
                            else 0
                        ),
                    )
                )
                proc = await asyncio.shield(spawn_task)
            except OSError as exc:
                async with _RUNS_LOCK:
                    _RUNS[rid]["status"] = "done"
                    _RUNS[rid]["exit_code"] = -1
                    _RUNS[rid]["output"].append(f"[error] spawn failed: {exc}")
                return
            # Stamp the live process handle under the admission lock and
            # re-check the shutdown flag in the SAME critical section. The
            # parent registered this run as ``(task, None)`` before the child
            # existed; cleanup snapshots ``(task, proc)`` tuples and only kills
            # a proc it can SEE. Without this guard a child spawned in the
            # window between registration and this stamp is invisible to a
            # cleanup that already snapshotted -- it skips _kill_tree (proc was
            # None) and only cancels the task, orphaning the child to keep
            # mutating the shared checkout after the gateway exits. If shutdown
            # already snapshotted, reap the just-spawned child ourselves and
            # abort, since our cancellation may not have arrived yet.
            async with _SHUTDOWN_ADMISSION_LOCK:
                if _SHUTDOWN_IN_PROGRESS:
                    await _kill_tree(proc.pid)
                    await platform_compat.kill_and_reap(proc)
                    async with _RUNS_LOCK:
                        _RUNS[rid]["status"] = "done"
                        _RUNS[rid]["exit_code"] = -1
                        _RUNS[rid]["output"].append("[shutdown] run aborted: gateway stopping")
                    return
                if rid in _ACTIVE_RUNS:
                    _ACTIVE_RUNS[rid] = (_ACTIVE_RUNS[rid][0], proc)
            assert proc.stdout is not None
            timed_out = False
            deadline = asyncio.get_event_loop().time() + _RUN_DEADLINE_S

            while True:
                if asyncio.get_event_loop().time() > deadline:
                    timed_out = True
                    await _kill_tree(proc.pid)
                    proc.kill()
                    break
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue
                if not line:
                    break
                async with _RUNS_LOCK:
                    out = _RUNS[rid]["output"]
                    text = line.decode(errors="replace").rstrip("\n")
                    if text.startswith("::step::"):
                        # Authoritative step index AND label survive the
                        # output-window cap (a chatty build step floods markers
                        # out of the last-60-lines snapshot the API returns).
                        idx, label = _parse_step_marker(text)
                        if idx is not None:
                            _RUNS[rid]["step"] = idx
                        if label is not None:
                            _RUNS[rid]["step_label"] = label
                    out.append(text)
                    if len(out) > 500:
                        del out[: len(out) - 500]

            rc = await proc.wait()
            async with _RUNS_LOCK:
                if timed_out:
                    _RUNS[rid]["status"] = "timeout"
                    _RUNS[rid]["exit_code"] = -1
                    _RUNS[rid]["output"].append(
                        f"[timeout] process killed after {_RUN_DEADLINE_S}s deadline"
                    )
                else:
                    _RUNS[rid]["status"] = "done"
                    _RUNS[rid]["exit_code"] = rc
                    # The failure DIAGNOSIS is derived here, from the exit code,
                    # and never read out of the child's stdout. That stream also
                    # carries worktree-controlled build output, so a marker in it
                    # could be forged by an install script printing the marker
                    # and then failing -- and the dashboard would present the
                    # forgery as authoritative, remedy included.
                    #
                    # An exit code is not self-authenticating either: worktree
                    # code can exit 41 as easily as it can print a marker. What
                    # makes the code trustworthy is the SCRIPT that produced it,
                    # and only the sync runner enforces the reservation (a
                    # reserved code from any step but the probe is demoted to a
                    # plain failure). So only that kind may be stamped. A
                    # `provision` run reaches this same line while executing an
                    # agent-authored branch with no such remapping, and stamping
                    # it would hand back exactly the forged-diagnosis-plus-remedy
                    # this boundary exists to refuse -- latent only for as long as
                    # no consumer reads `cause` off a non-sync run.
                    if run_kind == _SYNC_RUN_LABEL:
                        cause = npm_preflight.explain_exit(rc)
                        if cause:
                            _RUNS[rid]["cause"] = cause
        except asyncio.CancelledError:
            # Gateway shutdown cancels in-flight run tasks. The child runs in
            # its own process group and would outlive us, continuing to mutate
            # the shared checkout after the gateway exits. ``asyncio.shield``
            # re-raises the cancellation immediately while the inner spawn keeps
            # running detached, so ``proc`` may still be None here with the
            # child forked-or-forking. Drain ``spawn_task`` to COMPLETION before
            # reaping: a single ``await asyncio.shield(spawn_task)`` is not
            # enough because a SECOND cancellation (e.g. a shutdown hard-timeout
            # following the first cancel) lands on that await too, and swallowing
            # it into ``proc = None`` would abandon the very child this handler
            # exists to reap. So re-await the shield until the spawn task is
            # actually done, absorbing repeat cancellations only for the drain,
            # then recover the handle, kill/reap the tree, and re-raise so the
            # task still reports cancelled. An OSError result means the spawn
            # itself failed and there is no child to reap.
            if proc is None and spawn_task is not None:
                while not spawn_task.done():
                    try:
                        await asyncio.shield(spawn_task)
                    except asyncio.CancelledError:
                        continue
                    except OSError:
                        break
                if spawn_task.done():
                    try:
                        proc = spawn_task.result()
                    except (OSError, asyncio.CancelledError):
                        proc = None
            if proc is not None and proc.returncode is None:
                await _kill_tree(proc.pid)
                await platform_compat.kill_and_reap(proc)
            raise
        except Exception as exc:  # noqa: BLE001
            # readline() raising (e.g. a single output line exceeding the
            # 64 KiB stream limit -> ValueError/LimitOverrunError) lands
            # here with the subprocess still running — reap the whole tree
            # so a worktree-controlled build can't outlive its run record.
            if proc is not None and proc.returncode is None:
                await _kill_tree(proc.pid)
                await platform_compat.kill_and_reap(proc)
            async with _RUNS_LOCK:
                _RUNS[rid]["status"] = "done"
                _RUNS[rid]["exit_code"] = -1
                _RUNS[rid]["output"].append("[error] " + str(exc))
        finally:
            for cp in cleanup_paths or []:
                # A caller may register a temp FILE, or a temp directory it
                # created for one (the dependency-only sync stages a snapshot
                # that way). unlink refuses a directory, so fall back to rmdir
                # rather than leaking one temp dir per run.
                try:
                    os.unlink(cp)
                except IsADirectoryError:
                    try:
                        os.rmdir(cp)
                    except OSError:
                        pass
                except PermissionError:
                    # Windows raises PermissionError, not IsADirectoryError,
                    # when unlink is handed a directory.
                    try:
                        os.rmdir(cp)
                    except OSError:
                        pass
                except OSError:
                    pass

    task = asyncio.create_task(worker())
    # Register under the admission lock so this insertion is atomic with
    # respect to dev_fleet_cleanup's flag-set + snapshot.  The lock is held
    # only for these two dict writes (< 1 µs) — never across slow I/O — so
    # it cannot stall cleanup or introduce done-callback deadlocks.
    async with _SHUTDOWN_ADMISSION_LOCK:
        if _SHUTDOWN_IN_PROGRESS:
            # Cleanup has already snapshotted _ACTIVE_RUNS; cancelling the
            # task here keeps the worker from running to completion after the
            # gateway exits and mutating shared checkout state.
            task.cancel()
            raise RuntimeError("dev-fleet shutdown in progress: run refused")
        _ACTIVE_RUNS[rid] = (task, None)
    task.add_done_callback(lambda _t: _ACTIVE_RUNS.pop(rid, None))
    return rid


# --- pod helpers ---
def _load_cfg():
    if not _POD_AVAILABLE:
        return None
    try:
        return PodConfig.load()
    except Exception:  # noqa: BLE001
        return None


# Minimal allowlisted environment for subprocesses that execute
# worktree-controlled code (pip/npm builds, pod CLI). The gateway's full
# environment carries credentials (Slack/cloud tokens) that build scripts
# must never be able to read.
_POSIX_SAFE_ENV_KEYS = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
)

# Windows counterparts of the POSIX set above, written in the spelling Microsoft
# documents. Matching is case-folded on Windows (see :func:`_is_safe_env_key`),
# so these do not have to be upper-cased to survive ``os.environ``.
#
# SystemRoot is load-bearing, not cosmetic: Winsock locates its socket catalog
# through it, so a child without it cannot resolve names at all. libcurl's
# threaded resolver reports that as ``getaddrinfo() thread failed to start``,
# which is what a credential-bearing ``git fetch`` fails with here. The rest
# keep git and the node/pip toolchains functional: git reads its global config
# through USERPROFILE, npm and pip need APPDATA/LOCALAPPDATA plus a writable
# TEMP, PATHEXT is required to resolve ``.exe``/``.cmd`` at all, and
# NUMBER_OF_PROCESSORS sizes build parallelism.
#
# This is platform parity, not a wider boundary: USERPROFILE/APPDATA are the
# Windows equivalents of the POSIX HOME already allowlisted above, and every
# name here is a platform path rather than a secret. No credential-bearing
# variable is added, so build steps still cannot read Slack/cloud tokens.
_WINDOWS_SAFE_ENV_KEYS = (
    "SystemRoot",
    "SystemDrive",
    "windir",
    "ComSpec",
    "PATHEXT",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "APPDATA",
    "LOCALAPPDATA",
    "ProgramData",
    "ProgramFiles",
    "ProgramFiles(x86)",
    "ProgramW6432",
    "TEMP",
    "TMP",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
)

_SAFE_ENV_KEYS = _POSIX_SAFE_ENV_KEYS + (
    _WINDOWS_SAFE_ENV_KEYS if platform_compat.IS_WINDOWS else ()
)


def _is_safe_env_key(key: str) -> bool:
    """Whether *key* is allowlisted, honoring Windows' case-insensitive env.

    Thin wrapper binding this module's allowlist to the shared matching
    convention — exact on POSIX, case-folded on Windows. The rationale (why a
    literal membership test silently drops ``SystemRoot`` on Windows, so a
    spawned ``git`` cannot initialize Winsock and a fetch dies with
    ``getaddrinfo() thread failed to start``, and why POSIX must stay exact)
    lives on :func:`platform_compat.env_key_allowed`.
    """
    return platform_compat.env_key_allowed(key, _SAFE_ENV_KEYS)


def _build_env(*, with_credentials: bool = False) -> dict:
    """Allowlisted base environment for build/CLI subprocesses.

    ``_GIT_ENV_NEUTRALIZERS`` pins git transports to https/ssh and
    neutralizes repo-controlled execution config (fsmonitor/hooks/credential
    helper/sshCommand) for the sync ``git pull`` and any git a build step
    runs. Harmless for pip/npm.

    Operator credential helpers are injected ONLY when ``with_credentials`` is
    set — reserved for the network fetch step. Build steps (pip/npm) run
    worktree-controlled code and must never see a configured helper: a
    malicious install script could otherwise mint the operator's token via
    ``git credential fill``.

    ``with_credentials`` ALSO selects the PATH, and that is a security boundary,
    not a convenience:

    * credential-free (default) — the node toolchain dirs are PREPENDED to the
      trusted path. npm's own run-scripts (``tsc``, ``vite``) are
      ``#!/usr/bin/env node``, so ``node`` has to resolve by NAME inside the
      child; resolving only the ``npm`` argv would still fail at the first
      script. Those dirs live under ``$HOME`` because that is where
      ``ensure-node.sh`` installs node.
    * with credentials — the pinned ``_TRUSTED_PATH`` only. The fetch step's
      argv is an already-vetted absolute ``git``, but git looks its OWN helpers
      up (``git-remote-https``, credential helpers) on PATH, so a
      same-user-writable directory there would be a path to intercepting a
      credential-bearing fetch. Never widen this side.

    Scope of that guarantee, precisely: this ternary is what enforces it for the
    callers that spawn DIRECTLY (the ``raw_steps`` list, ``_pod_provision``,
    ``_start_run``). Callers that route through :func:`_run_cmd` are covered by
    a second, independent mechanism — ``_run_cmd`` overwrites PATH with
    ``_TRUSTED_PATH`` unconditionally, BEFORE it injects any credential helper —
    so on that path a node-augmented PATH from :func:`_pod_env` is discarded and
    never coexists with credentials. Both mechanisms must keep holding; do not
    remove one on the assumption that the other covers it.
    """
    out = {k: v for k, v in os.environ.items() if _is_safe_env_key(k)}
    out["PATH"] = _TRUSTED_PATH if with_credentials else _build_path()
    out.update(_GIT_ENV_NEUTRALIZERS)
    if with_credentials and _GIT_TRUSTED_HELPERS:
        out.update(_GIT_TRUSTED_HELPERS)
    return out


def _sel():
    """Structured audit-log sink. In standalone backend context, imports
    kiro_crew.sel directly (no _handlers_pkg indirection needed)."""
    from kiro_crew.sel import sel as _sel_singleton

    return _sel_singleton()


__all__ = (
    "PodConfig",
    "_ACTIVE_RUNS",
    "_BUILD_PATH_CACHE",
    "_GIT_ENV_NEUTRALIZERS",
    "_GIT_ERR_MAX",
    "_GIT_TRUSTED_HELPERS",
    "_KEYCHAIN_HELPER_NAMES",
    "_POD_AVAILABLE",
    "_POD_ERROR",
    "_POD_IMPORTED",
    "_POSIX_SAFE_ENV_KEYS",
    "_PREFLIGHT_LABEL",
    "_PREFLIGHT_SOURCE",
    "_RUNS",
    "_RUNS_LOCK",
    "_RUNS_MAX_COMPLETED",
    "_RUN_DEADLINE_S",
    "_SAFE_ENV_KEYS",
    "_SANDBOX_ERR_MAX",
    "_SHUTDOWN_ADMISSION_LOCK",
    "_SHUTDOWN_IN_PROGRESS",
    "_SYNC_LOCK",
    "_SYNC_RUNNER_SOURCE",
    "_SYNC_RUN_LABEL",
    "_TRUSTED_BIN_CACHE",
    "_TRUSTED_BIN_DIRS",
    "_TRUSTED_PATH",
    "_UNRESOLVED_TOOL_PREFIX",
    "_WINDOWS_SAFE_ENV_KEYS",
    "_bin_override_var",
    "_build_env",
    "_build_path",
    "_find_cli",
    "_invalidate_toolchain_cache",
    "_is_safe_env_key",
    "_kill_tree",
    "_kill_tree_sync",
    "_load_cfg",
    "_parse_step_marker",
    "_redact",
    "_redact_pr",
    "_run_cmd",
    "_run_uninterruptible",
    "_sanitize_helper_value",
    "_sel",
    "_start_run",
    "_toolchain_bin",
    "_trusted_bin",
    "_unresolved_tool_message",
    "_warm_build_path",
    "logger",
    "prov",
    "rt",
)
