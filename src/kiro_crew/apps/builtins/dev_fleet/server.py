"""Dev Fleet — standalone aiohttp backend for KiroCrew feature worktrees.

Manages KiroCrew feature worktrees (git worktrees of the main repo) and their
isolated pod test instances. Runs as a subprocess spawned by the KiroCrew app
backend system (apps/backend.py). The gateway proxies /apps/dev-fleet/api/* to
this process with X-KiroCrew-Proxy HMAC signing; HMAC middleware validates
every request (except /health) fail-closed.

Routes (as seen by the backend after prefix stripping by gateway):
  GET  /api/fleet             -> lightweight worktree + pod list (polled)
  GET  /api/worktree?name=    -> lazy per-branch detail (pr/commits/disk)
  GET  /api/pod/logs?name=&n=
  GET  /api/run?id=           -> async run status + streamed output
  GET  /api/prune-candidates
  GET  /api/prune-status
  GET  /api/disk
  POST /api/sync              -> pull main + rebuild
  POST /api/worktree/remove {name, force?}
  POST /api/prune-run {names}
  POST /api/pod/up   {name}
  POST /api/pod/down {name}
  POST /api/pod/restart {name}
  POST /api/pod/token {name}
  POST /api/pod/provision {name}  -> start async build, returns {run_id}
  POST /api/rebase  {name}
  GET  /health                -> {"status": "ok"}
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import hmac as _hmac_mod
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew import hooks, platform_compat
from kiro_crew.executors import subprocess_executor
from kiro_crew.sandbox import (
    build_resource_limit_preexec,
    resource_limit_preexec,
    sandboxed_spawn_argv,
)
from kiro_crew.security import (
    redact_credentials,
    redact_exfiltration_urls,
)

logger = logging.getLogger(__name__)


# --- standalone backend config ---
PORT = int(os.environ.get("PORT", 9100))
APP_NAME = os.environ.get("KIROCREW_APP_NAME", "dev-fleet")
_PROXY_HMAC_MAX_AGE_S = 60
_APP_SECRET: str | None = None


def _load_app_secret() -> str:
    """Load the app secret for proxy HMAC verification (once)."""
    global _APP_SECRET
    if _APP_SECRET is not None:
        return _APP_SECRET
    from kiro_crew.config.loader import config_dir
    secret_path = config_dir() / "apps" / APP_NAME / ".app_secret"
    if secret_path.is_file():
        _APP_SECRET = secret_path.read_text().strip()
    else:
        # Fallback: try the apps dir from manager
        try:
            from kiro_crew.apps.manager import app_dir
            alt = app_dir(APP_NAME) / ".app_secret"
            if alt.is_file():
                _APP_SECRET = alt.read_text().strip()
        except Exception:
            pass
    # Do NOT cache emptiness: the secret may be provisioned after this
    # backend starts (install race) — retry on the next request, matching
    # the gateway-side _get_app_secret semantics.
    return _APP_SECRET or ""


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
        for k, v in pr.items() if not k.startswith("_")  # _repo etc. stay internal
    }


def _resolve_primary_checkout(path: str) -> str:
    """Given any checkout (primary or linked worktree), return the primary
    checkout path. A linked worktree's --git-common-dir points at the
    primary's .git directory."""
    git = _trusted_bin("git")
    if git is None:
        return path
    env = {k: v for k, v in os.environ.items() if k in _SAFE_ENV_KEYS}
    env["PATH"] = _TRUSTED_PATH
    try:
        out = subprocess.run(
            [git, "-C", path, "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, timeout=5, env=env,
        )
        common = out.stdout.strip()
        if out.returncode == 0 and Path(common).name == ".git":
            return str(Path(common).parent)
    except (OSError, subprocess.SubprocessError):
        pass
    return path


def _default_main_repo() -> str:
    """Resolve the main checkout hint from env (NO subprocess at import time —
    this module is imported from the async route-registration path and a git
    call here would block the event loop). The hint is normalized to the
    PRIMARY checkout in dev_fleet_startup() via the subprocess executor."""
    explicit = os.environ.get("KIROCREW_DEVFLEET_REPO")
    if explicit:
        return explicit
    proj = os.environ.get("KIROCREW_PROJECT_DIR")
    if proj and (Path(proj) / ".git").exists():
        return proj
    return str(Path.home() / "kirocrew")


# --- configuration ---
MAIN_REPO = _default_main_repo()
BASE_BRANCH = "main"

# --- upstream remote resolution (replaces hardcoded 'origin') ---
_UPSTREAM_REMOTE: str | None = None


async def _upstream_remote() -> str:
    """Resolve the configured remote for BASE_BRANCH, falling back to 'origin'.

    Uses `git config branch.<BASE_BRANCH>.remote` so renamed remotes (e.g.
    'kirocrew' instead of 'origin') are honoured automatically. Cached at
    startup via dev_fleet_startup().
    """
    global _UPSTREAM_REMOTE
    if _UPSTREAM_REMOTE is not None:
        return _UPSTREAM_REMOTE
    rc, out, _ = await _run_cmd(
        ["git", "-C", MAIN_REPO, "config", f"branch.{BASE_BRANCH}.remote"],
        timeout=5,
    )
    cand = out.strip() if rc == 0 else ""
    # Repo-writable config could smuggle an option-like value ("--exec=...")
    # that later argv interpolation (`git rebase {remote}/main`) would parse
    # as a flag. Accept only a plausible remote NAME that git itself lists.
    if cand and not cand.startswith("-") and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", cand):
        rc2, remotes, _ = await _run_cmd(["git", "-C", MAIN_REPO, "remote"], timeout=5)
        if rc2 == 0 and cand in remotes.split():
            _UPSTREAM_REMOTE = cand
            return _UPSTREAM_REMOTE
    _UPSTREAM_REMOTE = "origin"
    return _UPSTREAM_REMOTE

# --- stream watchdog deadline (module constant so tests can patch it) ---
_RUN_DEADLINE_S = 1800

# --- build-pending detection (server-side truth) ---
_START_EPOCH = time.time()


def _build_pending() -> bool:
    """True when the built SPA dist mtime is NEWER than this module's import time
    (gateway process start). Mirrors MeshClaw v0.6.4 _beta_build_pending() semantics:
    a completed Pull+Build has artifacts waiting to be applied on the next restart."""
    try:
        # parents[3] == the kiro_crew package root (dev_fleet -> builtins ->
        # apps -> kiro_crew). A relative parent-chain broke once already when
        # this module moved from dashboard/handlers/ — the test pins the shape.
        dist = Path(__file__).resolve().parents[3] / "static" / "dist"
        if not dist.exists():
            return False
        return dist.stat().st_mtime > _START_EPOCH
    except OSError:
        return False


# --- pod availability ---
_POD_AVAILABLE = False
_POD_ERROR = ""
try:
    from kiro_crew.pod import provision as prov
    from kiro_crew.pod import runtime as rt
    from kiro_crew.pod.config import PodConfig

    # Pods are systemd --user units — they can only exist on Linux with
    # systemctl present. On other platforms skip pod-state checks entirely
    # instead of failing closed on every removal.
    if sys.platform == "linux" and shutil.which("systemctl"):
        _POD_AVAILABLE = True
    else:
        _POD_ERROR = "pods require Linux systemd (systemctl not available)"
except ImportError as exc:
    _POD_ERROR = str(exc)


# --- async run tracking ---
_RUNS: dict[str, dict] = {}
_RUNS_LOCK = asyncio.Lock()
_SYNC_LOCK = asyncio.Lock()


def _find_cli() -> list[str]:
    """Invoke the kirocrew CLI as a module of OUR interpreter.

    Never resolved through the filesystem: a `kirocrew` shim planted in an
    agent-writable PATH entry (or venv bin) would become an absolute path
    that bypasses the trusted-binary gate. `sys.executable -m` pins the CLI
    to the exact code identity this backend is already running.
    """
    return [sys.executable, "-m", "kiro_crew.cli"]


# Git hardening injected as ENVIRONMENT (same precedence as `git -c`, which
# overrides every config file) so EVERY git invocation from this handler —
# foreground inspection, the unattended background fetch, rebase, sync pull,
# and any git a build step runs — is neutralized at one chokepoint instead of
# per-call-site flags. All four keys are attacker-configurable via an
# agent-writable ``.git/config`` and would otherwise execute code:
#   * protocol pin  — ``ext::``/custom remote helpers refused by git itself
#   * core.fsmonitor / core.hooksPath — repo-registered executables
#   * credential.helper (reset to empty list) — helper commands
#   * core.sshCommand (pinned to plain ``ssh``) — arbitrary command on fetch
# Harmless for non-git commands (pip/npm ignore GIT_*).
_GIT_ENV_NEUTRALIZERS: dict[str, str] = {
    "GIT_ALLOW_PROTOCOL": "https:ssh",
    "GIT_PROTOCOL_FROM_USER": "0",
    "GIT_CONFIG_COUNT": "4",
    "GIT_CONFIG_KEY_0": "core.fsmonitor", "GIT_CONFIG_VALUE_0": "false",
    "GIT_CONFIG_KEY_1": "core.hooksPath", "GIT_CONFIG_VALUE_1": "/dev/null",
    "GIT_CONFIG_KEY_2": "credential.helper", "GIT_CONFIG_VALUE_2": "",
    "GIT_CONFIG_KEY_3": "core.sshCommand", "GIT_CONFIG_VALUE_3": "ssh",
}

# The credential.helper reset above kills repo-injected helpers (the attack
# vector) but ALSO the operator's own GLOBAL helper (e.g. `gh auth
# git-credential`), breaking https pulls with "could not read Username".
# The global config file is operator-owned — outside the repo attack surface
# the neutralizer targets — so its helper entries are trusted and re-pinned
# AFTER the reset. Env precedence still guarantees a repo-level helper can
# never win. Loaded once at startup; None means "not loaded yet" (probe-safe).
_GIT_TRUSTED_HELPERS: dict[str, str] | None = None


# Legacy-remote fallback: a renamed project keeps old remotes (e.g. origin ->
# the pre-rename repo) whose PRs cover older worktrees. A fallback repo's
# merged verdict is trusted ONLY when that remote's BASE_BRANCH is an ANCESTOR
# of the upstream BASE_BRANCH — i.e. everything merged there is contained in
# the current main, so "merged" still means "content is shipped".
_FALLBACK_REPOS: list[str] | None = None


# Which checkout powers the live gateway (the MeshClaw reference showed this
# per-row as is_live; users need to see what occupies the main instance).
_LIVE_WORKTREE: str | None = None
_LIVE_CHECK_AT: float = 0.0
_LIVE_TTL = 30.0


def _same_path(a: str, b: str) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False


async def _live_worktree_path() -> str | None:
    """Resolve the checkout the live gateway unit runs from (or None)."""
    global _LIVE_WORKTREE, _LIVE_CHECK_AT
    now = time.monotonic()
    if _LIVE_CHECK_AT and (now - _LIVE_CHECK_AT) < _LIVE_TTL:
        return _LIVE_WORKTREE
    _LIVE_CHECK_AT = now
    if sys.platform != "linux" or not shutil.which("systemctl"):
        _LIVE_WORKTREE = None
        return None
    rc, out, _err = await _run_cmd(
        ["systemctl", "--user", "show", _LIVE_GATEWAY_UNIT, "-p", "ExecStart"],
        timeout=5,
    )
    path = None
    if rc == 0 and out:
        m = re.search(r"path=([^ ;]+)", out)
        if m:
            exe = Path(m.group(1))
            # <checkout>/.venv/bin/kirocrew -> <checkout>
            if ".venv" in exe.parts:
                path = str(exe.parents[2])
    try:
        _LIVE_WORKTREE = str(Path(path).resolve()) if path else None
    except OSError:
        _LIVE_WORKTREE = None
    return _LIVE_WORKTREE


async def _load_fallback_repos() -> None:
    global _FALLBACK_REPOS
    repos: list[str] = []
    upstream = await _upstream_remote()
    rc, out, _err = await _run_cmd(["git", "-C", MAIN_REPO, "remote"], timeout=5)
    if rc == 0:
        for remote in out.split():
            if remote == upstream:
                continue
            rc2, _, _ = await _run_cmd(
                ["git", "-C", MAIN_REPO, "merge-base", "--is-ancestor",
                 f"{remote}/{BASE_BRANCH}", f"{upstream}/{BASE_BRANCH}"],
                timeout=10,
            )
            if rc2 != 0:
                continue
            rc3, url, _ = await _run_cmd(
                ["git", "-C", MAIN_REPO, "remote", "get-url", remote], timeout=5,
            )
            if rc3 == 0:
                m = re.search(r"[:/]([^/]+/[^/]+?)(?:\.git)?$", url.strip())
                if m:
                    repos.append(m.group(1))
    _FALLBACK_REPOS = repos


async def _load_trusted_credential_helpers() -> None:
    global _GIT_TRUSTED_HELPERS
    rc, out, _err = await _run_cmd(
        ["git", "config", "--global", "--get-regexp", r"^credential(\..+)?\.helper$"],
        timeout=5,
    )
    extra: dict[str, str] = {}
    if rc == 0 and out:
        base = int(_GIT_ENV_NEUTRALIZERS["GIT_CONFIG_COUNT"])
        idx = base
        for line in out.splitlines():
            key, _, val = line.partition(" ")
            if not key.endswith(".helper"):
                continue
            trusted_val = _sanitize_helper_value(val.strip())
            if trusted_val is None:
                # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
                # No secret is logged: the helper VALUE is deliberately
                # withheld; only the config KEY name is recorded.
                logger.warning(
                    "dev-fleet: skipping helper with unverifiable provenance"
                    " for config key %s", key,
                )
                continue
            extra[f"GIT_CONFIG_KEY_{idx}"] = key
            extra[f"GIT_CONFIG_VALUE_{idx}"] = trusted_val
            idx += 1
            if idx - base >= 9:
                break
        if idx > base:
            extra["GIT_CONFIG_COUNT"] = str(idx)
    _GIT_TRUSTED_HELPERS = extra


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
    _TRUSTED_BIN_DIRS = ("/usr/local/bin", "/usr/bin", "/bin")
_TRUSTED_PATH = os.pathsep.join(_TRUSTED_BIN_DIRS)
_TRUSTED_BIN_CACHE: dict[str, str | None] = {}


def _trusted_bin(name: str) -> str | None:
    """Resolve *name* to a canonical executable in a root-owned system dir.

    The service PATH starts with agent-writable directories (worktree venv,
    ~/.local/bin) — resolving through it would let a planted `git`/`gh`
    shim run inside the credential-bearing standard tier. Only non-symlink
    executables physically inside the trusted dirs qualify; fail closed
    otherwise.
    """
    if name in _TRUSTED_BIN_CACHE:
        return _TRUSTED_BIN_CACHE[name]
    resolved: str | None = None
    # Operator escape hatch for hosts where the tool lives outside the
    # system dirs (e.g. gh in ~/.local/bin): an explicit absolute path set
    # in the SERVICE environment (operator-owned unit file), never derived
    # from the inherited PATH.
    override = os.environ.get(f"KIROCREW_DEVFLEET_BIN_{name.upper().replace('-', '_')}")
    if override and Path(override).is_absolute() and Path(override).is_file() \
            and os.access(override, os.X_OK):
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
                if platform_compat.IS_POSIX and (
                    os.access(real, os.W_OK) or st.st_mode & 0o022
                ):
                    continue
                resolved = str(cand)
                break
            except OSError:
                continue
        if resolved:
            break
    _TRUSTED_BIN_CACHE[name] = resolved
    return resolved


async def _run_cmd(
    cmd: list[str], *, cwd: str | None = None, env: dict | None = None,
    timeout: int = 30, mode: str = "standard"
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
            return -1, "", f"no trusted executable for {cmd[0]!r} in {_TRUSTED_PATH}"
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
        loop = asyncio.get_running_loop()
        cmd, env, cleanup = await loop.run_in_executor(
            subprocess_executor(),
            functools.partial(sandboxed_spawn_argv, cmd, mode, env=base_env),
        )
    except RuntimeError as exc:
        # Fail closed: no sandbox backend and unsandboxed exec not opted in.
        return -1, "", f"sandbox unavailable: {exc}"
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            # Kernel RLIMIT ceilings for the sandboxed child (fork bomb / FD /
            # mem / CPU) — required for every chokepoint-routed spawn.
            preexec_fn=resource_limit_preexec(),
            # Own process group so a timeout kill reaps descendants (e.g.
            # `pod up` spawning pip), matching _start_run.
            start_new_session=platform_compat.IS_POSIX,
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
                if platform_compat.IS_WINDOWS else 0
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
            proc.kill()
            await proc.wait()
            return -1, "", f"timeout ({timeout}s)"
        except asyncio.CancelledError:
            # Backend shutdown/restart cancels in-flight handlers: the child
            # runs in its own process group and would outlive us (a canceled
            # rebase never reaches its --abort path, wedging the worktree).
            await _kill_tree(proc.pid)
            proc.kill()
            await proc.wait()
            raise
        return proc.returncode or 0, (stdout or b"").decode(errors="replace"), (stderr or b"").decode(errors="replace")
    finally:
        if cleanup:
            try:
                os.unlink(cleanup)
            except OSError:
                pass


async def _kill_tree(pid: int) -> None:
    """Kill a process tree without blocking the event loop (taskkill/killpg
    are synchronous syscalls/subprocesses — run them on the executor)."""
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            subprocess_executor(), platform_compat.kill_process_tree, pid
        )
    except (ProcessLookupError, OSError):
        pass


# Active background runs: rid -> (worker task, subprocess). Tracked so
# gateway cleanup can kill process trees instead of orphaning pip/npm.
_ACTIVE_RUNS: dict[str, tuple[asyncio.Task, Any]] = {}


_RUNS_MAX_COMPLETED = 50


async def _start_run(
    label: str, cmd: list[str], *, cwd: str | None = None,
    env: dict | None = None, cleanup_paths: list[str] | None = None,
) -> str:
    """Start a background subprocess with output streaming and watchdog.

    ``cleanup_paths``: sandbox launcher/profile temp files from
    ``sandboxed_spawn_argv`` — deleted when the run finishes.
    """
    rid = uuid.uuid4().hex[:12]
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
            "status": "running", "exit_code": None, "label": label,
            "output": [], "started": time.time(),
        }

    async def worker() -> None:
        proc: Any = None
        try:
            try:
                proc = await asyncio.create_subprocess_exec(
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
                    preexec_fn=build_resource_limit_preexec(),
                    # Own process group so a timeout kill reaps descendants
                    # (pip/npm children), not just the immediate CLI process.
                    start_new_session=platform_compat.IS_POSIX,
                    creationflags=(
                        subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
                        if platform_compat.IS_WINDOWS else 0
                    ),
                )
            except OSError as exc:
                async with _RUNS_LOCK:
                    _RUNS[rid]["status"] = "done"
                    _RUNS[rid]["exit_code"] = -1
                    _RUNS[rid]["output"].append(f"[error] spawn failed: {exc}")
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
                        # Authoritative step index survives the output-window
                        # cap (a chatty build step floods markers out of the
                        # last-60-lines snapshot the API returns).
                        parts = text.split("::", 4)
                        if len(parts) >= 3 and parts[2].isdigit():
                            _RUNS[rid]["step"] = int(parts[2])
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
        except Exception as exc:  # noqa: BLE001
            # readline() raising (e.g. a single output line exceeding the
            # 64 KiB stream limit -> ValueError/LimitOverrunError) lands
            # here with the subprocess still running — reap the whole tree
            # so a worktree-controlled build can't outlive its run record.
            if proc is not None and proc.returncode is None:
                await _kill_tree(proc.pid)
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
            async with _RUNS_LOCK:
                _RUNS[rid]["status"] = "done"
                _RUNS[rid]["exit_code"] = -1
                _RUNS[rid]["output"].append("[error] " + str(exc))
        finally:
            for cp in (cleanup_paths or []):
                try:
                    os.unlink(cp)
                except OSError:
                    pass

    task = asyncio.create_task(worker())
    _ACTIVE_RUNS[rid] = (task, None)
    task.add_done_callback(lambda _t: _ACTIVE_RUNS.pop(rid, None))
    return rid


# --- GitHub PR status (TTL-cached, best-effort) ---
_PR_CACHE: dict[str, dict] = {}
_PR_TTL = 55

_OWNER_REPO: str | None = None
_OWNER_REPO_RETRY_AT: float = 0.0  # monotonic deadline before retrying a failed lookup


async def _repo_owner_name() -> str | None:
    """Derive owner/repo from the upstream remote URL."""
    remote = await _upstream_remote()
    rc, stdout, _ = await _run_cmd(
        ["git", "-C", MAIN_REPO, "remote", "get-url", remote], timeout=5
    )
    if rc != 0:
        return None
    url = stdout.strip()
    m = re.search(r"[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
    return m.group(1) if m else None


async def _get_owner_repo() -> str | None:
    """Resolve owner/repo once. Only SUCCESS is cached permanently; a failed
    lookup (transient network/gh error) is retried after a short TTL so PR
    status and merged-worktree pruning recover without a gateway restart."""
    global _OWNER_REPO, _OWNER_REPO_RETRY_AT
    if _OWNER_REPO:
        return _OWNER_REPO
    now = time.monotonic()
    if now < _OWNER_REPO_RETRY_AT:
        return None
    val = await _repo_owner_name()
    if val:
        _OWNER_REPO = val
        return val
    _OWNER_REPO_RETRY_AT = now + 60.0
    return None


async def _pr_query_one(owner_repo: str, branch: str) -> dict | None:
    rc, stdout, _ = await _run_cmd(
        ["gh", "pr", "list", "--repo", owner_repo, "--head", branch,
         "--json", "number,state,url,isDraft", "--state", "all", "--limit", "1"],
        timeout=15,
    )
    if rc != 0:
        return None
    try:
        prs = json.loads(stdout)
        pr = prs[0] if prs else None
    except (json.JSONDecodeError, IndexError):
        return None
    if pr is not None:
        pr["_repo"] = owner_repo
    return pr


async def _fetch_pr_status(branch: str) -> dict | None:
    """Query GitHub PR via gh CLI: upstream repo first, then ancestor-verified
    legacy-remote repos (pre-rename PRs stay visible and prunable)."""
    owner_repo = await _get_owner_repo()
    if not owner_repo or not branch:
        return None
    pr = await _pr_query_one(owner_repo, branch)
    if pr is not None:
        return pr
    for repo in (_FALLBACK_REPOS or []):
        pr = await _pr_query_one(repo, branch)
        if pr is not None:
            return pr
    return None


async def _head_contained_in_pr(path: str, branch_oid: str, pr_head_oid: str) -> bool:
    """True when the worktree HEAD is the PR head or an ANCESTOR of it.

    A merged PR whose head gained remote-side commits before merge leaves the
    local branch strictly BEHIND the PR head — all local content is contained
    in the merge, so removal is safe. Only commits the PR head does NOT
    contain (local HEAD not an ancestor) are unmerged work.
    """
    if branch_oid.strip() == pr_head_oid.strip():
        return True
    rc, _, _err = await _run_cmd(
        ["git", "-C", path, "merge-base", "--is-ancestor",
         branch_oid.strip(), pr_head_oid.strip()],
        timeout=10,
    )
    return rc == 0


async def _fetch_pr_head_oid(branch: str, repo: str | None = None) -> str | None:
    """Fetch the headRefOid of the PR for *branch* — FRESH and MERGED-gated.

    Destructive callers (prune/removal) rely on this as the authoritative
    check: the state and head OID come from the SAME live response, and a
    non-MERGED state returns None. A stale cached MERGED verdict for a
    reused branch name can therefore never authorize removing the new
    branch's worktree — the fresh state here is OPEN and we refuse.
    """
    owner_repo = repo or await _get_owner_repo()
    if not owner_repo or not branch:
        return None
    rc, stdout, _ = await _run_cmd(
        ["gh", "pr", "view", branch, "--repo", owner_repo,
         "--json", "headRefOid,state"],
        timeout=15,
    )
    if rc != 0:
        return None
    try:
        data = json.loads(stdout)
        if data.get("state") != "MERGED":
            return None
        return data.get("headRefOid")
    except (json.JSONDecodeError, ValueError):
        return None


async def _pr_status_cached(branch: str) -> dict | None:
    """Return cached PR status for a branch."""
    if not branch or branch == BASE_BRANCH:
        return None
    now = time.time()
    ent = _PR_CACHE.get(branch)
    if ent:
        # Only MERGED is permanently terminal — a CLOSED PR can be reopened,
        # so its cache entry must expire via the normal TTL.
        is_terminal = (ent.get("data") or {}).get("state") == "MERGED"
        if is_terminal or (now - ent["ts"]) < _PR_TTL:
            return ent.get("data")
    data = await _fetch_pr_status(branch)
    _PR_CACHE[branch] = {"data": data, "ts": time.time()}
    return data


def _is_pr_merged(pr: dict | None) -> bool:
    return (pr or {}).get("state") == "MERGED"


# --- worktree discovery via git worktree list --porcelain ---
def _parse_worktree_porcelain(raw: str) -> list[dict]:
    """Parse `git worktree list --porcelain` output into a list of dicts."""
    entries: list[dict] = []
    current: dict = {}
    for line in raw.splitlines():
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        if line.startswith("worktree "):
            current["path"] = line[9:]
        elif line.startswith("HEAD "):
            current["head"] = line[5:]
        elif line.startswith("branch "):
            ref = line[7:]
            current["branch"] = ref.split("refs/heads/", 1)[-1] if "refs/heads/" in ref else ref
        elif line == "detached":
            current["branch"] = None
    if current:
        entries.append(current)
    return entries


async def _discover_worktrees() -> list[dict]:
    """List git worktrees of MAIN_REPO."""
    rc, stdout, stderr = await _run_cmd(
        ["git", "-C", MAIN_REPO, "worktree", "list", "--porcelain"], timeout=10
    )
    if rc != 0:
        # Propagate sandbox/git failures as a RuntimeError so callers can
        # surface the real reason instead of returning silent empty lists.
        err_detail = (stderr or stdout or "").strip()[:200]
        if "sandbox unavailable" in err_detail:
            raise RuntimeError(err_detail)  # already prefixed by _run_cmd
        return []
    entries = _parse_worktree_porcelain(stdout)
    # `git worktree list --porcelain` always lists the primary checkout
    # first — that is the authoritative main, regardless of whether
    # MAIN_REPO itself points at a linked worktree (it is only the
    # repository discovery hint).
    for i, e in enumerate(entries):
        e["is_main"] = (i == 0)
    return entries


async def _git(
    git_dir: str, *args: str, timeout: int = 6, mode: str = "standard"
) -> str | None:
    # Repo-controlled execution vectors are neutralized centrally in
    # _run_cmd via _GIT_ENV_NEUTRALIZERS — no per-call-site flags needed.
    rc, stdout, _ = await _run_cmd(
        ["git", "-C", git_dir, *args], timeout=timeout, mode=mode
    )
    return stdout.strip() if rc == 0 else None


async def _git_info(path: str) -> dict:
    info: dict = {
        "branch": None, "head": None, "dirty": False,
        "ahead": 0, "behind": 0, "last_updated_at": None,
    }
    info["branch"] = await _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    info["head"] = await _git(path, "rev-parse", "--short=7", "HEAD")
    st = await _git(path, "status", "--porcelain")
    if st is not None:
        info["dirty"] = len(st) > 0
    remote = await _upstream_remote()
    behind = await _git(path, "rev-list", "--count", f"HEAD..{remote}/{BASE_BRANCH}")
    if behind and behind.isdigit():
        info["behind"] = int(behind)
    ct = await _git(path, "log", "-1", "--format=%ct")
    if ct and ct.isdigit():
        info["last_updated_at"] = int(ct)
    return info


async def _git_ahead(path: str) -> int | None:
    """Patch-unique local commits via git cherry."""
    remote = await _upstream_remote()
    ch = await _git(path, "cherry", f"{remote}/{BASE_BRANCH}", "HEAD", timeout=12)
    if ch is not None:
        return sum(1 for ln in ch.splitlines() if ln.startswith("+"))
    ar = await _git(path, "rev-list", "--count", f"{remote}/{BASE_BRANCH}..HEAD")
    return int(ar) if ar and ar.isdigit() else None


async def _own_commits_count(path: str) -> int | None:
    remote = await _upstream_remote()
    out = await _git(path, "rev-list", "--count", f"{remote}/{BASE_BRANCH}..HEAD")
    return int(out) if out and out.isdigit() else None


async def _real_dirty(path: str) -> bool | None:
    st = await _git(path, "status", "--porcelain")
    if st is None:
        return None
    return any(ln.strip() for ln in st.splitlines())


# --- fleet cache ---
_FLEET_TTL = 10.0
_FLEET_CACHE: dict[str, Any] = {"data": None, "ts": 0.0}
_FLEET_REFRESHING = False


async def _fleet_refresh() -> dict:
    data = await _build_fleet()
    _FLEET_CACHE["data"] = data
    _FLEET_CACHE["ts"] = time.monotonic()
    return data


async def _fleet_cached() -> dict:
    global _FLEET_REFRESHING
    data, ts = _FLEET_CACHE["data"], _FLEET_CACHE["ts"]
    if data is None:
        return await _fleet_refresh()
    if time.monotonic() - ts > _FLEET_TTL and not _FLEET_REFRESHING:
        _FLEET_REFRESHING = True

        async def _bg():
            global _FLEET_REFRESHING
            try:
                await _fleet_refresh()
            finally:
                _FLEET_REFRESHING = False

        asyncio.create_task(_bg())
    return data


async def _build_fleet() -> dict:
    live_path = await _live_worktree_path()
    worktrees = await _discover_worktrees()
    cfg = _load_cfg()
    legacy_prefixes = tuple(
        f"{r.split('/')[-1].lower()}-wt-" for r in (_FALLBACK_REPOS or [])
    )
    wts = []
    for wt in worktrees:
        path = wt.get("path", "")
        branch = wt.get("branch")
        is_main = wt.get("is_main", False)
        g = await _git_info(path)
        pr = (await _pr_status_cached(branch)) if branch else None
        name = Path(path).name if not is_main else BASE_BRANCH

        # Pod status (best-effort)
        running = False
        port = None
        health = None
        has_venv = False
        has_dist = False
        if _POD_AVAILABLE and cfg and not is_main:
            try:
                loop = asyncio.get_running_loop()
                active = await loop.run_in_executor(
                    subprocess_executor(), rt.active_names, cfg
                )
                running = name in active
                if running:
                    port = await loop.run_in_executor(
                        subprocess_executor(), rt.derive_port, cfg, name
                    )
                    health = await loop.run_in_executor(
                        subprocess_executor(), rt.health, port, 2
                    )
                has_venv = await loop.run_in_executor(
                    subprocess_executor(), prov.has_venv, Path(path)
                )
                has_dist = await loop.run_in_executor(
                    subprocess_executor(), prov.has_dist, Path(path)
                )
            except Exception:  # noqa: BLE001
                pass

        ahead = await _git_ahead(path)
        # "shipped" drives the UI's "safe to remove" affordance — require a
        # POSITIVELY clean worktree (dirty is False, not merely unknown), so
        # the confirm dialog never promises a removal the backend will refuse.
        shipped = (
            _is_pr_merged(pr)
            and (ahead is not None and ahead == 0)
            and g["dirty"] is False
            and not is_main
        )

        wts.append({
            # "name" doubles as the opaque identifier for follow-up actions
            # (validated against the discovered set on every call); display
            # fields sourced from git/gh output are redacted.
            "name": name, "path": _redact(path), "is_main": is_main,
            "running": running, "port": port, "health": health,
            "is_live": live_path is not None and _same_path(path, live_path),
            "has_venv": has_venv, "has_dist": has_dist,
            "branch": _redact(g["branch"] or branch or ""), "head": g["head"] or wt.get("head", "")[:7],
            "dirty": g["dirty"], "behind": g["behind"],
            "pr": _redact_pr(pr), "shipped": shipped,
            "legacy": bool(legacy_prefixes) and not is_main
            and name.lower().startswith(legacy_prefixes),
            "last_updated_at": g["last_updated_at"],
        })
    return {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "worktrees": wts,
        "main_repo": MAIN_REPO,
        "base_branch": BASE_BRANCH,
        "sync_run_id": _SYNC_RID,
        "build_pending": _build_pending(),
        "gateway_service_active": await _gateway_service_active(),
    }


def _find_worktree_sync(worktrees: list[dict], name: str) -> tuple[dict | None, str | None]:
    """Resolve a worktree by display name, rejecting ambiguous basenames."""
    matches = []
    for w in worktrees:
        wname = Path(w["path"]).name if not w.get("is_main") else BASE_BRANCH
        if wname == name:
            matches.append(w)
    if not matches:
        return None, f"worktree not found: {name}"
    if len(matches) > 1:
        paths = ", ".join(w["path"] for w in matches)
        return None, f"ambiguous worktree name {name!r} matches multiple checkouts: {paths}"
    return matches[0], None


async def _find_worktree(name: str) -> tuple[dict | None, str | None]:
    wts = await _discover_worktrees()
    return _find_worktree_sync(wts, name)


async def _valid_worktree_names() -> set[str]:
    return {
        Path(w["path"]).name if not w.get("is_main") else BASE_BRANCH
        for w in await _discover_worktrees()
    }


async def _worktree_detail(name: str) -> dict:
    """Lazy per-worktree detail."""
    wt, err = await _find_worktree(name)
    if wt is None:
        return {"error": err}
    path = wt["path"]
    branch = wt.get("branch")
    is_main = wt.get("is_main", False)
    g = await _git_info(path)
    pr = (await _pr_status_cached(branch)) if branch else None
    own_commits = await _own_commits_count(path)

    remote = await _upstream_remote()
    commits: list[dict] = []
    if not is_main:
        log = await _git(
            path, "log", f"{remote}/{BASE_BRANCH}..HEAD", "-12",
            "--format=%h\x1f%s\x1f%cr",
        )
        if log:
            for line in log.splitlines():
                parts = line.split("\x1f")
                if len(parts) == 3:
                    commits.append({"hash": parts[0], "subject": _redact(parts[1]), "when": parts[2]})

    design_docs: list[str] = []
    if not is_main:
        diff_out = await _git(
            path, "diff", "--name-only", f"{remote}/{BASE_BRANCH}...HEAD",
            timeout=15,
        )
        if diff_out:
            seen: set[str] = set()
            for line in diff_out.splitlines():
                line = line.strip()
                if not line or line in seen:
                    continue
                seen.add(line)
                low = line.lower()
                if low.startswith("docs/") or "/docs/" in low or "design" in low:
                    design_docs.append(_redact(line))
                if len(design_docs) >= 12:
                    break

    disk_mb = None
    try:
        rc, stdout, _ = await _run_cmd(["du", "-sm", path], timeout=15)
        if rc == 0:
            disk_mb = int(stdout.split()[0])
    except (ValueError, IndexError):
        pass

    pod_running = False
    pod_port = None
    cfg = _load_cfg()
    if _POD_AVAILABLE and cfg and not is_main:
        try:
            loop = asyncio.get_running_loop()
            active = await loop.run_in_executor(
                subprocess_executor(), rt.active_names, cfg
            )
            pod_running = name in active
            if pod_running:
                pod_port = await loop.run_in_executor(
                    subprocess_executor(), rt.derive_port, cfg, name
                )
        except Exception:
            pass

    return {
        "name": name, "path": _redact(path),
        "branch": _redact(g["branch"] or branch or ""), "head": g["head"],
        "dirty": g["dirty"], "own_commits": own_commits,
        "real_dirty": await _real_dirty(path),
        "pr": _redact_pr(pr), "pr_merged": _is_pr_merged(pr),
        "commits": commits, "design_docs": design_docs,
        "disk_mb": disk_mb,
        "behind": g["behind"],
        "is_main": is_main,
        "pod_running": pod_running, "pod_port": pod_port,
    }


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
_SAFE_ENV_KEYS = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TMPDIR",
    "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
)


def _build_env(*, with_credentials: bool = False) -> dict:
    """Allowlisted base environment for build/CLI subprocesses.

    ``_GIT_ENV_NEUTRALIZERS`` pins git transports to https/ssh and
    neutralizes repo-controlled execution config (fsmonitor/hooks/credential
    helper/sshCommand) for the sync ``git pull`` and any git a build step
    runs. Harmless for pip/npm.

    Operator credential helpers are injected ONLY when ``with_credentials``
    is set — reserved for the network fetch step. Build steps (pip/npm) run
    worktree-controlled code and must never see a configured helper: a
    malicious install script could otherwise mint the operator's token via
    ``git credential fill``.
    """
    out = {k: v for k, v in os.environ.items() if k in _SAFE_ENV_KEYS}
    out["PATH"] = _TRUSTED_PATH
    out.update(_GIT_ENV_NEUTRALIZERS)
    if with_credentials and _GIT_TRUSTED_HELPERS:
        out.update(_GIT_TRUSTED_HELPERS)
    return out


def _pod_env() -> dict:
    """Environment for pod CLI subprocesses (allowlisted base + pod repo)."""
    return {**_build_env(), "KIROCREW_POD_REPO": MAIN_REPO}


def _read_pin_strict(cfg: Any, name: str) -> tuple[bool, str | None]:
    """Read the pod's pinned CHECKOUT with failures PROPAGATED.

    Returns ``(env_file_exists, checkout_or_none)``. Unlike
    ``rt.read_env_file`` (which swallows OSError and returns ``{}``), a read
    failure raises — the caller must treat "file exists but cannot be
    positively read" as deny, never as "unpinned". The pin file must be a
    regular non-symlink file resolving inside the pods dir and must not be a
    sensitive path (the pods dir is agent-writable; a symlinked ``.env``
    must never pull a protected file into the gateway). Runs on the executor.
    """
    env_path = cfg.env_file(name)
    if not env_path.exists():
        return False, None
    # TOCTOU-safe: O_NOFOLLOW open + fstat validation of the DESCRIPTOR
    # (symlink/regular-file/containment/sensitivity checked atomically
    # against the opened inode, not a raceable path). Raises -> caller denies.
    data = hooks.safe_read_file_bytes_nolink(str(env_path), within_root=str(cfg.pods_dir))
    if data is None:
        # hooks gate refused (symlink/hardlink/containment/sensitive/IO):
        # "exists but cannot be positively read" is a DENY, never "unpinned".
        raise OSError(f"pin file refused by hooks read gate: {env_path}")
    text = data.decode("utf-8", errors="replace")
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        key, val = ln.split("=", 1)
        if key.strip() != "CHECKOUT":
            continue
        raw = val.strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            raw = raw[1:-1]
        return True, raw or None
    return True, None


async def _pod_checkout_guard(name: str) -> str | None:
    """Pod identities are global basenames while Dev Fleet scopes worktrees to
    MAIN_REPO. Before ANY pod operation, verify the pod's pinned ``CHECKOUT``
    matches THIS repo's worktree of that name — otherwise the operation would
    land on an unrelated repository's pod (stop it, delete its isolated HOME,
    or provision the wrong checkout). Returns an error string to refuse, or
    None to proceed. Fail closed on any uncertainty."""
    target, ferr = await _find_worktree(name)
    if target is None:
        return ferr or f"unknown worktree: {name!r}"
    cfg = _load_cfg()
    if cfg is None:
        if not _POD_AVAILABLE:
            # Pod subsystem entirely absent -> nothing to collide with; the
            # pod op itself will fail with its own clear error.
            return None
        # Pods exist on this host but config cannot be loaded -> we cannot
        # verify pod identity; fail closed.
        return "cannot load pod configuration to verify pod identity"
    loop = asyncio.get_running_loop()
    try:
        env_exists, pinned = await loop.run_in_executor(
            subprocess_executor(), _read_pin_strict, cfg, name
        )
    except Exception as exc:  # noqa: BLE001
        # Pin state exists but cannot be positively read -> deny, never
        # treat as "unpinned" (that ambiguity is exactly the cross-repo hole).
        return f"cannot verify pod checkout pin: {_redact(str(exc))}"
    if not env_exists:
        # No pin file: only safe when no pod under this global name is
        # ACTIVE — an active unit with a missing pin is a foreign pod we
        # cannot attribute; acting on it would stop/expose another repo's
        # gateway. Fail closed on active or unverifiable.
        try:
            active = await loop.run_in_executor(
                subprocess_executor(), rt.active_names, cfg
            )
        except Exception as exc:  # noqa: BLE001
            return f"cannot verify active pods: {_redact(str(exc))}"
        if name in active:
            return (
                f"pod {name!r} is active but has no checkout pin — refusing "
                "pod operation (unattributable pod identity)"
            )
        return None
    if not pinned:
        # Pin file EXISTS but carries no verifiable CHECKOUT -> ambiguous
        # pod identity; refuse rather than risk acting on a foreign pod.
        return (
            f"pod {name!r} has a pin file without a verifiable CHECKOUT — "
            "refusing pod operation (ambiguous pod identity)"
        )
    try:
        if Path(pinned).resolve() != Path(target["path"]).resolve():
            return (
                f"pod {name!r} is pinned to a different checkout — refusing "
                "cross-repository pod operation (basename collision)"
            )
    except OSError as exc:
        return f"cannot resolve checkout paths for pod guard: {_redact(str(exc))}"
    return None


async def _pod_up(name: str) -> dict:
    guard = await _pod_checkout_guard(name)
    if guard:
        return {"ok": False, "error": guard}
    cmd = _find_cli() + ["pod", "up", name, "--json"]
    rc, stdout, stderr = await _run_cmd(cmd, cwd=MAIN_REPO, env=_pod_env(), timeout=180)
    if rc == 0:
        try:
            return {"ok": True, **json.loads(stdout)}
        except (json.JSONDecodeError, ValueError):
            return {"ok": True, "output": stdout}
    return {"ok": False, "error": _redact(stderr or stdout)}


async def _pod_down(name: str) -> dict:
    guard = await _pod_checkout_guard(name)
    if guard:
        return {"ok": False, "error": guard}
    cmd = _find_cli() + ["pod", "down", name]
    rc, stdout, stderr = await _run_cmd(cmd, cwd=MAIN_REPO, env=_pod_env(), timeout=30)
    return {"ok": rc == 0, "error": _redact(stderr or stdout) if rc != 0 else None}


async def _pod_restart(name: str) -> dict:
    """Restart a pod: down, then up only after a successful shutdown."""
    r = await _pod_down(name)
    if not r.get("ok"):
        return {"ok": False, "error": f"pod shutdown failed: {r.get('error')}"}
    return await _pod_up(name)


async def _pod_token(name: str) -> dict:
    guard = await _pod_checkout_guard(name)
    if guard:
        return {"ok": False, "error": guard}
    cfg = _load_cfg()
    if cfg is None:
        return {"ok": False, "error": "PodConfig unavailable"}
    try:
        loop = asyncio.get_running_loop()
        token = await loop.run_in_executor(
            subprocess_executor(), rt.mint_token, cfg, name, "2h"
        )
        port = await loop.run_in_executor(
            subprocess_executor(), rt.derive_port, cfg, name
        )
        return {"ok": True, "token": token, "url": f"http://127.0.0.1:{port}/?token={token}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


async def _pod_logs(name: str, n: int = 120) -> dict:
    guard = await _pod_checkout_guard(name)
    if guard:
        return {"ok": False, "error": guard}
    cfg = _load_cfg()
    if cfg is None:
        return {"ok": False, "error": "PodConfig unavailable"}
    loop = asyncio.get_running_loop()
    raw = await loop.run_in_executor(
        subprocess_executor(), rt.recent_journal, cfg, name, n
    )
    return {"ok": True, "logs": _redact(raw)}


# Per-worktree provisioning single-flight: name -> run id. Repeated POSTs
# must not concurrently recreate .venv / dist for the same checkout.
_PROVISION_INFLIGHT: dict[str, str] = {}
_PROVISION_LOCK = asyncio.Lock()


async def _pod_provision(name: str) -> dict:
    guard = await _pod_checkout_guard(name)
    if guard:
        return {"ok": False, "error": guard}
    # Check, start, and record under ONE lock — releasing between the check
    # and the record lets two queued requests both observe "no active run".
    async with _PROVISION_LOCK:
        prev = _PROVISION_INFLIGHT.get(name)
        if prev:
            async with _RUNS_LOCK:
                running = _RUNS.get(prev, {}).get("status") == "running"
            if running:
                return {"ok": False, "error": "provision already running", "run_id": prev}
        loop = asyncio.get_running_loop()
        p_argv, p_env, p_cleanup = await loop.run_in_executor(
            subprocess_executor(),
            functools.partial(
                sandboxed_spawn_argv,
                _find_cli() + ["pod", "provision", name], "strict", env=_pod_env(),
            ),
        )
        rid = await _start_run(
            "provision " + name, p_argv, cwd=MAIN_REPO, env=p_env,
            cleanup_paths=[p_cleanup] if p_cleanup else None,
        )
        _PROVISION_INFLIGHT[name] = rid
    return {"ok": True, "run_id": rid}


# --- disk aggregation ---
_DISK: dict = {"status": "idle", "total_mb": None, "per": {}}
_DISK_COMPUTING = False


async def _disk() -> dict:
    global _DISK_COMPUTING
    if _DISK["status"] == "computing":
        return dict(_DISK)
    if _DISK["status"] == "done":
        snap = dict(_DISK)
        _DISK["status"] = "idle"
        return snap
    _DISK["status"] = "computing"
    _DISK_COMPUTING = True

    async def work() -> None:
        global _DISK_COMPUTING
        try:
            per: dict = {}
            total = 0
            for w in await _discover_worktrees():
                nm = Path(w["path"]).name
                try:
                    rc, stdout, _ = await _run_cmd(["du", "-sm", w["path"]], timeout=60)
                    if rc == 0:
                        mb = int(stdout.split()[0])
                        per[nm] = mb
                        total += mb
                except (ValueError, IndexError):
                    pass
            _DISK.update({"status": "done", "total_mb": total, "per": per})
        except Exception:  # noqa: BLE001
            _DISK.update({"status": "done", "total_mb": None, "per": {}})
        finally:
            _DISK_COMPUTING = False

    asyncio.create_task(work())
    return {"status": "computing", "total_mb": None, "per": {}}


# --- worktree remove ---
async def _worktree_remove(name: str, force: bool = False) -> dict:
    """Remove a feature worktree. All safety gates preserved.

    Non-forced removal of merged PRs uses a SQUASH-SAFE race guard: fetches
    the PR's headRefOid via `gh` and requires the worktree branch's current
    OID == the PR's merged headRefOid. Commits pushed after merge cause OID
    divergence and refuse the removal (unlike git cherry which never works
    for squash merges).
    """
    target, err = await _find_worktree(name)
    if target is None:
        return {"ok": False, "error": err}
    if target.get("is_main"):
        return {"ok": False, "error": "refusing: cannot remove the main checkout"}
    path = target["path"]
    branch = target.get("branch")

    if not force:
        dirty = await _real_dirty(path)
        if dirty is not False:
            return {"ok": False, "error": (
                "worktree has uncommitted changes (use force to override)"
                if dirty else "cannot verify worktree state (git status failed)"
            )}

    pr = (await _pr_status_cached(branch)) if branch else None
    own = await _own_commits_count(path)
    if not force and not _is_pr_merged(pr):
        if own is None or own > 0:
            return {
                "ok": False,
                "error": f"PR not merged (state: {(pr or {}).get('state', 'no PR')})",
                "pr": _redact_pr(pr),
            }

    # Pin the branch ref NOW — the same OID the safety verdict below evaluates
    # is the expected-old-OID for the atomic delete. A commit landing at any
    # point after this pin moves the ref, update-ref -d fails, branch retained.
    verdict_oid = (await _git(MAIN_REPO, "rev-parse", f"refs/heads/{branch}")) if branch else None
    if branch and branch != BASE_BRANCH and verdict_oid is None:
        return {"ok": False, "error": (
            "cannot pin branch OID (git rev-parse failed) — refusing removal"
        )}

    # Squash-safe race guard: for merged PRs, verify the branch tip matches
    # the PR's merged headRefOid. A commit pushed after merge moves the OID.
    if not force and _is_pr_merged(pr) and branch:
        branch_oid = verdict_oid
        if branch_oid is None:
            return {"ok": False, "error": (
                "cannot verify branch OID (git rev-parse failed) — "
                "refusing non-forced removal; retry or use force"
            )}
        pr_head_oid = await _fetch_pr_head_oid(branch, repo=(pr or {}).get("_repo"))
        if pr_head_oid is None:
            return {"ok": False, "error": (
                "cannot verify PR head OID (gh query failed) — "
                "refusing non-forced removal; retry or use force"
            )}
        if not await _head_contained_in_pr(path, branch_oid, pr_head_oid):
            return {
                "ok": False,
                "error": (
                    "branch has commits after merge (OID diverged from PR head) — "
                    "refusing non-forced removal; use force to override"
                ),
                "pr": _redact_pr(pr),
            }

    # stop pod if running
    cfg = _load_cfg()
    stopped_pod = False
    if _POD_AVAILABLE and cfg is None:
        return {"ok": False, "error": "cannot load pod configuration to verify pod state"}
    if _POD_AVAILABLE and cfg:
        try:
            loop = asyncio.get_running_loop()
            active = await loop.run_in_executor(
                subprocess_executor(), rt.active_names, cfg
            )
            if name in active:
                r = await _pod_down(name)
                if not r.get("ok"):
                    return {"ok": False, "error": f"pod shutdown failed: {r.get('error')}"}
                stopped_pod = True
                try:
                    active2 = await loop.run_in_executor(
                        subprocess_executor(), rt.active_names, cfg
                    )
                    if name in active2:
                        return {"ok": False, "error": "pod still active after shutdown"}
                except Exception as exc:
                    return {
                        "ok": False,
                        "error": f"cannot verify pod shutdown: {_redact(str(exc))}",
                    }
        except Exception as exc:
            return {
                "ok": False,
                "error": f"cannot verify pod state: {_redact(str(exc))}",
            }

    cmd = ["git", "-C", MAIN_REPO, "worktree", "remove", path]
    if force:
        cmd.append("--force")
    rc, stdout, stderr = await _run_cmd(cmd, timeout=60)
    if rc != 0:
        return {"ok": False, "error": _redact((stderr or stdout).strip()[:300])}

    # delete branch if shipped/empty — atomically against the pinned OID
    if branch and branch != BASE_BRANCH and verdict_oid:
        if _is_pr_merged(pr) or own == 0:
            await _git(
                MAIN_REPO, "update-ref", "-d",
                f"refs/heads/{branch}", verdict_oid.strip(), timeout=10,
            )

    return {"ok": True, "removed": True, "stopped_pod": stopped_pod, "pr": _redact_pr(pr)}


# --- sync (pull + build) ---
_SYNC_RID: str | None = None


async def _sync() -> dict:
    """Pull upstream main + rebuild. Single-flight via _SYNC_LOCK."""
    async with _SYNC_LOCK:
        if _SYNC_RID is not None:
            async with _RUNS_LOCK:
                run = _RUNS.get(_SYNC_RID)
            if run and run["status"] == "running":
                return {"ok": False, "error": "sync already running", "run_id": _SYNC_RID}
        return await _sync_start_locked()


def _venv_python(repo: str) -> Path | None:
    """Resolve the repo's own venv interpreter cross-platform (POSIX bin/,
    Windows Scripts/). Returns None when the venv is not provisioned."""
    for rel in ("bin/python", "Scripts/python.exe"):
        cand = Path(repo) / ".venv" / rel
        if cand.is_file():
            return cand
    return None


async def _sync_start_locked() -> dict:
    """Start the sync run. Caller holds _SYNC_LOCK."""
    global _SYNC_RID  # noqa: F824 (assigned below after await)

    head = await _git(MAIN_REPO, "symbolic-ref", "--short", "HEAD")
    if head is None:
        return {"ok": False, "error": "cannot determine checked-out branch (git failed)"}
    if head.strip() != BASE_BRANCH:
        return {"ok": False, "error": (
            f"refusing to sync: primary checkout is on {head.strip()!r}, not {BASE_BRANCH!r}"
        )}

    remote = await _upstream_remote()

    # CRITICAL: pip must run with the TARGET repo's own venv interpreter.
    # ``sys.executable`` here is the app backend's venv (a feature worktree's)
    # — `pip install -e .` with it would re-point that venv's editable install
    # at MAIN_REPO, hijacking the running gateway's code identity on its next
    # restart (observed live: gateway silently became the main repo's code).
    target_py = _venv_python(MAIN_REPO)
    if target_py is None:
        return {"ok": False, "error": (
            "main checkout has no .venv — provision it first "
            f"(expected under {Path(MAIN_REPO) / '.venv'})"
        )}
    git_bin = _trusted_bin("git")
    npm_bin = _trusted_bin("npm")
    if git_bin is None or npm_bin is None:
        missing = "git" if git_bin is None else "npm"
        return {"ok": False, "error": (
            f"no trusted executable for {missing!r} in {_TRUSTED_PATH}"
        )}
    raw_steps: list[tuple[list[str], str, dict, str]] = [
        ([git_bin, "fetch", remote, BASE_BRANCH], "standard",
         _build_env(with_credentials=True), "Pull"),
        ([git_bin, "merge", "--ff-only", f"{remote}/{BASE_BRANCH}"], "strict", _build_env(), "Pull"),
        ([str(target_py), "-m", "pip", "install", "-e", "."], "strict", _build_env(), "pip install"),
        ([npm_bin, "ci", "--prefix", "website"], "strict", _build_env(), "npm ci"),
        ([npm_bin, "run", "build", "--prefix", "website"], "strict", _build_env(), "npm build"),
    ]
    cleanups: list[str] = []
    wrapped_steps: list[dict] = []
    loop = asyncio.get_running_loop()
    for argv, mode, base_env, label in raw_steps:
        w_argv, w_env, cleanup = await loop.run_in_executor(
            subprocess_executor(),
            functools.partial(sandboxed_spawn_argv, argv, mode, env=base_env),
        )
        if cleanup:
            cleanups.append(cleanup)
        wrapped_steps.append({"argv": w_argv, "env": w_env, "label": label})
    script = (
        "import subprocess, sys, json\n"
        f"steps = json.loads({json.dumps(json.dumps(wrapped_steps))})\n"
        f"cwd = {json.dumps(MAIN_REPO)}\n"
        "for i, st in enumerate(steps):\n"
        "    print(f'::step::{i}::{st[\"label\"]}', flush=True)\n"
        "    r = subprocess.run(st['argv'], cwd=cwd, env=st['env'])\n"
        "    if r.returncode != 0:\n"
        "        sys.exit(r.returncode)\n"
    )
    cmd = [sys.executable, "-c", script]
    rid = await _start_run("sync", cmd, env=_build_env(), cleanup_paths=cleanups)
    _SYNC_RID = rid
    return {"ok": True, "run_id": rid}


# --- rebase ---
# Per-worktree mutation locks: two concurrent /rebase requests for the same
# checkout could both pass the clean-state check, then one's failure path
# would `rebase --abort` the OTHER's in-flight rebase.
_WT_LOCKS: dict[str, asyncio.Lock] = {}


def _wt_lock(name: str) -> asyncio.Lock:
    return _WT_LOCKS.setdefault(name, asyncio.Lock())


async def _rebase(name: str) -> dict:
    """Rebase worktree onto latest base branch. Aborts on conflict."""
    target, err = await _find_worktree(name)
    if target is None:
        return {"ok": False, "error": err}
    if target.get("is_main"):
        return {"ok": False, "error": "refusing to rebase the main checkout"}
    lock = _wt_lock(name)
    if lock.locked():
        return {"ok": False, "error": "rebase already running for this worktree"}
    async with lock:
        return await _rebase_locked(target)


async def _rebase_locked(target: dict) -> dict:
    path = target["path"]
    st = await _git(path, "status", "--porcelain")
    if st is None:
        return {"ok": False, "error": "cannot verify worktree state (git status failed)"}
    if st:
        return {"ok": False, "error": "worktree has uncommitted changes"}
    remote = await _upstream_remote()
    if await _git(path, "fetch", remote, BASE_BRANCH, timeout=90) is None:
        return {"ok": False, "error": f"git fetch {remote} {BASE_BRANCH} failed"}
    rc, stdout, stderr = await _run_cmd(
        ["git", "-C", path, "rebase", f"{remote}/{BASE_BRANCH}"],
        timeout=180, mode="strict",
    )
    if rc == 0:
        g = await _git_info(path)
        return {"ok": True, "rebased": True, "head": g["head"], "behind": g["behind"]}
    abort_res = await _git(path, "rebase", "--abort", timeout=30, mode="strict")
    tail = _redact((stdout + stderr).strip()[-200:])
    if abort_res is None:
        # Abort itself failed/timed out — the worktree is still mid-rebase.
        # Never report "aborted" when it is not; manual recovery required.
        return {
            "ok": False, "conflict": True,
            "error": (
                "rebase conflict AND `git rebase --abort` failed — worktree "
                f"is still mid-rebase; manual recovery required. {tail}"
            ),
        }
    return {"ok": False, "conflict": True, "error": f"rebase conflict (aborted). {tail}"}


# --- prune ---
_PRUNE_STATE: dict = {"running": False, "total": 0, "done": 0, "current": None, "results": []}
_PRUNE_LOCK = asyncio.Lock()


async def _prunable(path: str, branch: str | None) -> dict:
    """Structured prune verdict. Squash-merge safe: PR merged + clean -> ok.

    Does NOT require ahead==0 (git cherry never reports 0 for squash merges).
    The race guard in _worktree_remove handles the edge case of commits pushed
    after the PR was merged by comparing branch OID to the PR's headRefOid.
    """
    pr = (await _pr_status_cached(branch)) if branch else None
    own = await _own_commits_count(path)
    dirty = await _real_dirty(path)
    try:
        age_h = round((time.time() - Path(path).stat().st_ctime) / 3600, 1)
    except OSError:
        age_h = None
    base = {"pr": _redact_pr(pr), "own": own, "dirty": dirty, "age_h": age_h}
    if dirty is None:
        return {**base, "ok": False, "code": "dirty_check_failed"}
    if _is_pr_merged(pr):
        if dirty:
            return {**base, "ok": False, "code": "merged_dirty"}
        # Same squash-safe race guard removal enforces: commits pushed AFTER
        # the merge mean the branch OID diverged from the PR head — surface it
        # at preview time instead of letting the candidate fail every run.
        oid = await _git(path, "rev-parse", "HEAD")
        pr_oid = await _fetch_pr_head_oid(branch, repo=(pr or {}).get("_repo")) if branch else None
        if not oid or not pr_oid:
            # Cannot verify the squash-safe guard: removal would refuse this
            # anyway, so never present it as a candidate (fail-closed verdict
            # keeps preview and execution consistent).
            return {**base, "ok": False, "code": "merged_unverified"}
        if not await _head_contained_in_pr(path, oid, pr_oid):
            return {**base, "ok": False, "code": "merged_new_commits"}
        return {**base, "ok": True, "code": "merged"}
    if own == 0 and not dirty:
        if age_h and age_h > 48:
            return {**base, "ok": True, "code": "empty"}
        return {**base, "ok": False, "code": "fresh"}
    return {**base, "ok": False, "code": "active"}


async def _prune_candidates() -> dict:
    worktrees = await _discover_worktrees()
    candidates, kept = [], []
    for w in worktrees:
        if w.get("is_main"):
            continue
        name = Path(w["path"]).name
        v = await _prunable(w["path"], w.get("branch"))
        row = {"name": name, "code": v["code"], "branch": w.get("branch")}
        if v["ok"]:
            candidates.append(row)
        else:
            kept.append(row)
    return {"ok": True, "candidates": candidates, "kept": kept, "scanned": len(worktrees) - 1}


async def _prune_run(names: list[str]) -> dict:
    async with _PRUNE_LOCK:
        if _PRUNE_STATE["running"]:
            return {"ok": False, "error": "prune already running"}
        _PRUNE_STATE.update({"running": True, "total": len(names), "done": 0, "current": None, "results": []})

    async def _work() -> None:
        try:
            for nm in names:
                _PRUNE_STATE["current"] = nm
                # Re-resolve and require a fresh prunable verdict immediately
                # before removal — the API accepts any discovered name, so a
                # clean-but-recent worktree must be rejected here.
                target, err = await _find_worktree(nm)
                if target is None:
                    _PRUNE_STATE["results"].append({"name": nm, "ok": False, "error": err})
                    _PRUNE_STATE["done"] += 1
                    continue
                verdict = await _prunable(target["path"], target.get("branch"))
                if not verdict.get("ok"):
                    _PRUNE_STATE["results"].append(
                        {"name": nm, "ok": False,
                         "error": f"not prunable: {verdict.get('code', 'unknown')}"}
                    )
                    _PRUNE_STATE["done"] += 1
                    continue
                res = await _worktree_remove(nm, force=False)
                _PRUNE_STATE["results"].append({"name": nm, **res})
                _PRUNE_STATE["done"] += 1
        finally:
            _PRUNE_STATE["running"] = False
            _PRUNE_STATE["current"] = None

    asyncio.create_task(_work())
    return {"ok": True, "total": len(names)}


async def _prune_status() -> dict:
    return {k: (list(v) if isinstance(v, list) else v) for k, v in _PRUNE_STATE.items()}


# --- background fleet refresher (started on app startup) ---
_NET_REFRESH_S = 60
_refresher_task: asyncio.Task | None = None
_warm_task: asyncio.Task | None = None


async def _status_refresher() -> None:
    """Background task: periodically fetch upstream + refresh fleet cache."""
    while True:
        try:
            remote = await _upstream_remote()
            await _run_cmd(
                ["git", "-C", MAIN_REPO, "fetch", remote, BASE_BRANCH, "--quiet"],
                timeout=90,
            )
            await _fleet_refresh()
        except Exception:
            logger.exception("dev-fleet status refresher failed")
        await asyncio.sleep(_NET_REFRESH_S)


# =============================================================================
# aiohttp route handlers
# =============================================================================

async def api_dev_fleet_fleet(request: web.Request) -> web.Response:
    fresh = request.query.get("fresh") == "1"
    try:
        data = (await _fleet_refresh()) if fresh else (await _fleet_cached())
    except RuntimeError as exc:
        return web.json_response(
            {"worktrees": [], "error": str(exc)},  # _run_cmd already prefixes
        )
    return web.json_response(data)


async def api_dev_fleet_worktree(request: web.Request) -> web.Response:
    name = request.query.get("name")
    if not name:
        return web.json_response({"error": "missing 'name'"}, status=400)
    valid = await _valid_worktree_names()
    if name not in valid:
        return web.json_response({"error": f"unknown worktree: {name!r}"}, status=400)
    return web.json_response(await _worktree_detail(name))


async def api_dev_fleet_pod_logs(request: web.Request) -> web.Response:
    name = request.query.get("name")
    if not name:
        return web.json_response({"error": "missing 'name'"}, status=400)
    valid = await _valid_worktree_names()
    if name not in valid:
        return web.json_response({"error": f"unknown worktree: {name!r}"}, status=400)
    try:
        n = int(request.query.get("n", "120"))
    except ValueError:
        n = 120
    n = max(1, min(n, 1000))
    return web.json_response(await _pod_logs(name, n))


async def api_dev_fleet_run(request: web.Request) -> web.Response:
    rid = request.query.get("id")
    if not rid:
        return web.json_response({"error": "missing 'id'"}, status=400)
    async with _RUNS_LOCK:
        run = _RUNS.get(rid)
        snap = dict(run, output=[_redact(ln) for ln in list(run["output"])[-60:]]) if run else None
    if snap:
        return web.json_response(snap)
    return web.json_response({"error": "unknown run id"}, status=404)


async def api_dev_fleet_prune_candidates(request: web.Request) -> web.Response:
    return web.json_response(await _prune_candidates())


async def api_dev_fleet_prune_status(request: web.Request) -> web.Response:
    return web.json_response(await _prune_status())


async def api_dev_fleet_disk(request: web.Request) -> web.Response:
    return web.json_response(await _disk())


def _sel():
    """Structured audit-log sink. In standalone backend context, imports
    kiro_crew.sel directly (no _handlers_pkg indirection needed)."""
    from kiro_crew.sel import sel as _sel_singleton
    return _sel_singleton()


def _audited(tool_name: str):
    """Audit every Dev Fleet mutation via SEL, exactly once per request.

    The decision is made at the single response boundary of the handler:
    2xx -> success, 4xx -> denied, 5xx/exception -> failure.  Target
    worktree name is read from the JSON body without consuming the stream
    (handlers re-parse independently); values are redacted before logging.
    """
    def _decorate(handler):
        async def _wrapped(request: web.Request) -> web.Response:
            target = ""
            try:
                if request.content_length and request.can_read_body:
                    raw = await request.read()  # cached; handler .json() re-reads it
                    try:
                        parsed = json.loads(raw)
                        if isinstance(parsed, dict):
                            t = parsed.get("name") or parsed.get("names")
                            if isinstance(t, str):
                                target = t
                            elif isinstance(t, list):
                                target = ",".join(str(x) for x in t[:20])
                    except (ValueError, TypeError):
                        target = ""
            except Exception:
                target = ""
            try:
                resp = await handler(request)
            except Exception as exc:
                _sel().log_tool_invocation(
                    session_key="api", source="api", tool_name=tool_name,
                    tool_kind="dev_fleet", outcome="failure",
                    resources=_redact(target), error=type(exc).__name__)
                raise
            try:
                payload = json.loads(resp.text or "{}")
            except (ValueError, TypeError, AttributeError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            if resp.status >= 500:
                outcome = "failure"
            elif resp.status >= 400:
                outcome = "denied"
            elif payload.get("ok") is False:
                # Handlers report refused/failed operations as {"ok": false}
                # with HTTP 200 -- audit them as denied, never success.
                outcome = "denied"
            else:
                outcome = "success"
            err = ""
            if outcome != "success":
                err = _redact(str(payload.get("error", "")))[:200] or f"http_{resp.status}"
            _sel().log_tool_invocation(
                session_key="api", source="api", tool_name=tool_name,
                tool_kind="dev_fleet", outcome=outcome,
                resources=_redact(target), error=err)
            return resp
        _wrapped.__name__ = handler.__name__
        _wrapped.__doc__ = handler.__doc__
        return _wrapped
    return _decorate


@_audited("dev_fleet_sync")
async def api_dev_fleet_sync(request: web.Request) -> web.Response:
    result = await _sync()
    code = 409 if not result.get("ok") and "already running" in result.get("error", "") else 200
    return web.json_response(result, status=code)


async def _json_body(request: web.Request) -> tuple[dict | None, web.Response | None]:
    """Parse a JSON object body; (body, None) on success, (None, 400) otherwise."""
    try:
        body = await request.json() if request.content_length else {}
    except ValueError:
        return None, web.json_response({"error": "invalid JSON body"}, status=400)
    if not isinstance(body, dict):
        return None, web.json_response({"error": "body must be an object"}, status=400)
    return body, None


@_audited("dev_fleet_worktree_remove")
async def api_dev_fleet_worktree_remove(request: web.Request) -> web.Response:
    body, err = await _json_body(request)
    if err is not None:
        return err
    assert body is not None
    name = body.get("name")
    if not isinstance(name, str) or not name:
        return web.json_response({"error": "'name' must be a non-empty string"}, status=400)
    valid = await _valid_worktree_names()
    if name not in valid:
        return web.json_response({"error": f"unknown worktree: {name!r}"}, status=400)
    force = body.get("force")
    if force is not None and not isinstance(force, bool):
        return web.json_response({"error": "force must be a boolean"}, status=400)
    return web.json_response(await _worktree_remove(name, force is True))


@_audited("dev_fleet_prune_run")
async def api_dev_fleet_prune_run(request: web.Request) -> web.Response:
    try:
        body = await request.json() if request.content_length else {}
    except ValueError:
        return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"ok": False, "error": "body must be an object"}, status=400)
    raw_names = body.get("names") or []
    if not isinstance(raw_names, list) or not all(isinstance(n, str) for n in raw_names):
        return web.json_response(
            {"ok": False, "error": "'names' must be a list of strings"}, status=400
        )
    valid = await _valid_worktree_names()
    names = [n for n in raw_names if n in valid]
    if not names:
        return web.json_response({"ok": False, "error": "no valid names"}, status=400)
    return web.json_response(await _prune_run(names))


async def _pod_name_action(request: web.Request, action) -> web.Response:
    """Helper: validate name from body, call action(name)."""
    body, err = await _json_body(request)
    if err is not None:
        return err
    assert body is not None
    name = body.get("name")
    if not isinstance(name, str) or not name:
        return web.json_response({"error": "'name' must be a non-empty string"}, status=400)
    # _find_worktree rejects ambiguous basenames (two checkouts sharing a
    # name) — a bare set-membership check would collapse them and let the
    # action land on whichever checkout git lists first.
    target, ferr = await _find_worktree(name)
    if target is None:
        return web.json_response({"error": ferr}, status=400)
    return web.json_response(await action(name))


@_audited("dev_fleet_pod_up")
async def api_dev_fleet_pod_up(request: web.Request) -> web.Response:
    return await _pod_name_action(request, _pod_up)


@_audited("dev_fleet_pod_down")
async def api_dev_fleet_pod_down(request: web.Request) -> web.Response:
    return await _pod_name_action(request, _pod_down)


@_audited("dev_fleet_pod_restart")
async def api_dev_fleet_pod_restart(request: web.Request) -> web.Response:
    return await _pod_name_action(request, _pod_restart)


@_audited("dev_fleet_pod_token")
async def api_dev_fleet_pod_token(request: web.Request) -> web.Response:
    return await _pod_name_action(request, _pod_token)


@_audited("dev_fleet_pod_provision")
async def api_dev_fleet_pod_provision(request: web.Request) -> web.Response:
    return await _pod_name_action(request, _pod_provision)


@_audited("dev_fleet_rebase")
async def api_dev_fleet_rebase(request: web.Request) -> web.Response:
    return await _pod_name_action(request, _rebase)


# --- startup hook ---
async def dev_fleet_startup(app: web.Application) -> None:
    """Start the background fleet refresher on app startup."""
    global _refresher_task, _warm_task, MAIN_REPO
    loop = asyncio.get_running_loop()
    MAIN_REPO = await loop.run_in_executor(
        subprocess_executor(), _resolve_primary_checkout, MAIN_REPO
    )
    await _load_trusted_credential_helpers()
    await _load_fallback_repos()
    await _upstream_remote()
    if _refresher_task is None or _refresher_task.done():
        _refresher_task = asyncio.create_task(_status_refresher())
    _warm_task = asyncio.create_task(_fleet_refresh())


async def dev_fleet_cleanup(app: web.Application) -> None:
    """Cancel and await background tasks so a stopped runner leaves nothing behind."""
    global _refresher_task, _warm_task
    # Kill active sync/provision subprocess trees first, then cancel workers —
    # otherwise a gateway restart leaves pip/npm mutating shared checkouts.
    for rid, (task, proc) in list(_ACTIVE_RUNS.items()):
        if proc is not None and proc.returncode is None:
            await _kill_tree(proc.pid)
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        _ACTIVE_RUNS.pop(rid, None)
    for bg_task in (_refresher_task, _warm_task):
        if bg_task is not None and not bg_task.done():
            bg_task.cancel()
            try:
                await bg_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
    _refresher_task = None
    _warm_task = None


# =============================================================================
# HMAC Proxy Middleware (fail-closed)
# =============================================================================

@web.middleware
async def hmac_proxy_middleware(request: web.Request, handler) -> web.Response:
    """Verify X-KiroCrew-Proxy HMAC on every request except /health.

    Message format matches routes.py signing:
      msg = "<timestamp>:<METHOD>:<path>[?query]:<sha256(body)>"
    Fail-closed: missing/invalid/expired signature -> 401.
    """
    if request.path == "/health":
        return await handler(request)

    def _deny(reason: str) -> web.Response:
        # Every auth decision lands in the tamper-evident SEL trail — an
        # HMAC denial is a permission decision like any handler outcome.
        try:
            _sel().log_tool_invocation(
                session_key="api", source="api",
                tool_name="dev-fleet:proxy-hmac", tool_kind="dev_fleet",
                outcome="denied", resources=f"{request.method} {request.path}",
                error=reason,
            )
        except Exception:  # noqa: BLE001 — auditing must never mask the 401
            logger.warning("dev-fleet: SEL emit failed for HMAC denial")
        return web.json_response({"error": reason}, status=401)

    secret = _load_app_secret()
    if not secret:
        # Fail closed, no exceptions: an unauthenticated backend must never
        # serve mutation routes (a local-user bypass here reaches worktree
        # removal / rebase / gateway restart).
        return _deny("no app secret configured — HMAC verification impossible")

    header = request.headers.get("X-KiroCrew-Proxy")
    if not header:
        return _deny("missing X-KiroCrew-Proxy header")

    parts = header.split(":", 1)
    if len(parts) != 2:
        return _deny("malformed X-KiroCrew-Proxy header")

    ts_str, sig_received = parts
    try:
        ts = int(ts_str)
    except ValueError:
        return _deny("invalid timestamp in proxy header")

    now = int(time.time())
    if abs(now - ts) > _PROXY_HMAC_MAX_AGE_S:
        return _deny("proxy signature expired")

    # Reconstruct the signed message exactly as routes.py builds it
    body = await request.read() if request.can_read_body else b""
    body_hash = hashlib.sha256(body).hexdigest()
    # The gateway signs "/api/<path>[?query]" — the path as received by the backend
    msg = f"{ts_str}:{request.method}:{request.path}"
    if request.query_string:
        msg += f"?{request.query_string}"
    msg += f":{body_hash}"

    expected_sig = _hmac_mod.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    if not _hmac_mod.compare_digest(sig_received, expected_sig):
        return _deny("invalid proxy signature")

    return await handler(request)


# =============================================================================
# Health endpoint
# =============================================================================

async def api_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


# --- gateway service detection + restart ---
_GATEWAY_SERVICE_ACTIVE: bool | None = None
_GATEWAY_SERVICE_CHECK_AT: float = 0.0
_GATEWAY_SERVICE_TTL = 30.0
_LIVE_GATEWAY_UNIT = "kirocrew-gateway.service"


def _gateway_unit_name() -> str:
    """Resolve the systemd unit of the gateway THIS backend belongs to.

    Inside a pod (config home under ``.kirocrew-pods/<name>``) the owning unit
    is the pod template instance — restarting the hardcoded live unit from a
    pod would bounce the user's LIVE gateway across planes.
    """
    try:
        from kiro_crew.config.loader import config_dir

        home = config_dir()
        if home.parent.name == ".kirocrew-pods":
            return f"kirocrew-pod@{home.name}.service"
    except Exception:  # noqa: BLE001 — fall through to the live unit
        pass
    return _LIVE_GATEWAY_UNIT


async def _gateway_service_active() -> bool:
    """Cached check: is the gateway running as a user service?

    Async and routed through the sandboxed ``_run_cmd`` chokepoint: a sync
    ``subprocess.run`` here would block the event loop on cache miss AND
    bypass the spawn-audit sandbox invariant.
    """
    global _GATEWAY_SERVICE_ACTIVE, _GATEWAY_SERVICE_CHECK_AT
    now = time.monotonic()
    if _GATEWAY_SERVICE_ACTIVE is not None and (now - _GATEWAY_SERVICE_CHECK_AT) < _GATEWAY_SERVICE_TTL:
        return _GATEWAY_SERVICE_ACTIVE
    if sys.platform != "linux" or not shutil.which("systemctl"):
        _GATEWAY_SERVICE_ACTIVE = False
        _GATEWAY_SERVICE_CHECK_AT = now
        return False
    rc, _, _err = await _run_cmd(
        ["systemctl", "--user", "is-active", _gateway_unit_name()], timeout=5,
    )
    _GATEWAY_SERVICE_ACTIVE = rc == 0
    _GATEWAY_SERVICE_CHECK_AT = now
    return _GATEWAY_SERVICE_ACTIVE


async def _restart_gateway() -> dict:
    """Restart the gateway service via a detached systemd-run.

    The restart kills the current process, so we use systemd-run --collect
    to schedule a restart that survives our own death.
    """
    if sys.platform != "linux" or not shutil.which("systemctl"):
        return {"ok": False, "error": "gateway is not running as a user service"}
    rc, _, stderr = await _run_cmd(
        ["systemctl", "--user", "is-active", _gateway_unit_name()],
        timeout=5,
    )
    if rc != 0:
        return {"ok": False, "error": "gateway is not running as a user service"}
    rc, _, stderr = await _run_cmd(
        ["systemd-run", "--user", "--collect",
         "systemctl", "--user", "restart", _gateway_unit_name()],
        timeout=10,
    )
    if rc != 0:
        return {"ok": False, "error": _redact(stderr.strip()[:200]) or "systemd-run failed"}
    return {"ok": True}


@_audited("dev_fleet_restart_gateway")
async def api_dev_fleet_restart_gateway(request: web.Request) -> web.Response:
    result = await _restart_gateway()
    return web.json_response(result)


# =============================================================================
# Application factory and main
# =============================================================================

def create_app() -> web.Application:
    """Build the aiohttp Application with all routes and lifecycle hooks."""
    app = web.Application(middlewares=[hmac_proxy_middleware])
    app.router.add_get("/health", api_health)
    app.router.add_get("/api/fleet", api_dev_fleet_fleet)
    app.router.add_get("/api/worktree", api_dev_fleet_worktree)
    app.router.add_get("/api/pod/logs", api_dev_fleet_pod_logs)
    app.router.add_get("/api/run", api_dev_fleet_run)
    app.router.add_get("/api/prune-candidates", api_dev_fleet_prune_candidates)
    app.router.add_get("/api/prune-status", api_dev_fleet_prune_status)
    app.router.add_get("/api/disk", api_dev_fleet_disk)
    app.router.add_post("/api/sync", api_dev_fleet_sync)
    app.router.add_post("/api/worktree/remove", api_dev_fleet_worktree_remove)
    app.router.add_post("/api/prune-run", api_dev_fleet_prune_run)
    app.router.add_post("/api/pod/up", api_dev_fleet_pod_up)
    app.router.add_post("/api/pod/down", api_dev_fleet_pod_down)
    app.router.add_post("/api/pod/restart", api_dev_fleet_pod_restart)
    app.router.add_post("/api/pod/token", api_dev_fleet_pod_token)
    app.router.add_post("/api/pod/provision", api_dev_fleet_pod_provision)
    app.router.add_post("/api/rebase", api_dev_fleet_rebase)
    app.router.add_post("/api/restart-gateway", api_dev_fleet_restart_gateway)
    app.on_startup.append(dev_fleet_startup)
    app.on_cleanup.append(dev_fleet_cleanup)
    return app


def main() -> int:
    """Entry point when run as a module by the app backend system."""
    app = create_app()
    logger.info("Dev Fleet backend starting on 127.0.0.1:%d", PORT)
    web.run_app(app, host="127.0.0.1", port=PORT, print=None)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    raise SystemExit(main())
