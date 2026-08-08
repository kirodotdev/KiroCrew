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
  POST /api/make-live {path, dry_run?}  -> repoint the live gateway at a worktree
  GET  /api/health            -> {"status": "ok", "start_id": ...}  (restart handshake; proxied)
  GET  /health                -> same body, HMAC-exempt (gateway-internal liveness poll only)
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
from typing import Any, Callable

from aiohttp import web

from kiro_crew import frontend, hooks, platform_compat
from kiro_crew.apps.builtins.dev_fleet import gateway_service
from kiro_crew.env import find_node_tool, node_bin_dirs
from kiro_crew.executors import subprocess_executor
from kiro_crew.sandbox import (
    RLIMIT_PROFILE_BUILD,
    create_subprocess_limited,
    sandboxed_spawn_argv,
)
from kiro_crew.security import (
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.service import live_target

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
    env = {k: v for k, v in os.environ.items() if _is_safe_env_key(k)}
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
    """True when MAIN_REPO's built SPA dist is NEWER than this process's start.

    The dist inspected is the one Pull+Build actually writes — the MAIN CHECKOUT's
    — not the dist of the installation this backend happens to be running from.
    Those coincide only when the gateway runs from that same source tree. With the
    packaged desktop app the running installation's dist lives inside the .app
    bundle and its mtime never changes, so the previous ``parents[3]`` lookup
    could never fire: a completed Pull+Build reported nothing pending and the
    dashboard never told the user there was a build to apply.
    """
    try:
        dist = Path(MAIN_REPO) / "src" / "kiro_crew" / "static" / "dist"
        if not dist.exists():
            return False
        # stat() follows a symlink on purpose: a source-tree install points
        # static/dist at website/dist, and the rebuild time we care about is the
        # target's.
        return dist.stat().st_mtime > _START_EPOCH
    except OSError:
        return False


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
            "Pods are launchd user agents on macOS, but no `launchctl` was found "
            "on PATH."
        )
    elif sys.platform == "linux":
        _POD_ERROR = (
            "Pods require `systemctl --user`, but no `systemctl` was found on PATH."
        )
    else:
        _POD_ERROR = (
            f"Pods need systemd --user (Linux) or launchd (macOS); this host is "
            f"{sys.platform}. Preview a worktree with ./dev-backend.sh instead."
        )
except ImportError as exc:
    _POD_ERROR = f"the pod subsystem could not be imported: {exc}"


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


# Which checkout powers the live gateway (the upstream reference showed this
# per-row as is_live; users need to see what occupies the main instance).
_LIVE_WORKTREE: str | None = None
_LIVE_CHECK_AT: float = 0.0
_LIVE_TTL = 30.0


def _own_checkout_path() -> str | None:
    """Checkout root the RUNNING process's kiro_crew package resolves into.

    The systemd probe only sees service-managed gateways; a gateway launched
    directly from a feature worktree (and this backend, its subprocess) is
    invisible to it. Our own module path is ground truth for which checkout
    is live code right now -- editable installs resolve
    ``<checkout>/src/kiro_crew/__init__.py``.
    """
    try:
        import kiro_crew as _pkg

        p = Path(_pkg.__file__).resolve()
        for parent in p.parents:
            if (parent / ".git").exists() or (parent / "pyproject.toml").is_file():
                return str(parent)
    except Exception:  # noqa: BLE001 -- identity probe must never crash callers
        return None
    return None


def _same_path(a: str, b: str) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False


def _launchd_live_worktree() -> str | None:
    """Resolve the live checkout from the launchd agent's live-gateway launcher.

    The launcher is a generated script whose ``exec`` line names the target
    binary; the checkout is that binary's ``.venv`` grandparent. Returns ``None``
    when the launcher is absent (no agent installed) or names something that is
    not a worktree venv binary — e.g. a freshly installed agent still aimed at
    the system-wide ``kirocrew``. ``None`` correctly means "no row is live"
    rather than guessing.
    """
    try:
        script = gateway_service.LaunchdBackend.live_program().read_text()
    except OSError:
        return None
    m = re.search(r"^exec '((?:[^']|'\\'')+)'", script, re.MULTILINE)
    if not m:
        return None
    exe = Path(m.group(1).replace("'\\''", "'"))
    # <checkout>/.venv/bin/kirocrew -> <checkout>
    if ".venv" not in exe.parts:
        return None
    try:
        return str(exe.parents[2].resolve())
    except (OSError, IndexError):
        return None


def _running_checkout() -> Path | None:
    """The checkout this gateway process is EXECUTING from, or None.

    Authoritative where a service definition is not: it is derived from the
    location of the code that is actually loaded, so it needs no service query
    and cannot be fooled by a definition that was never updated. Returns None
    for a packaged/site-packages install, which is not a checkout at all — the
    caller must treat that as "cannot verify" rather than as a mismatch.
    """
    # .../<checkout>/src/kiro_crew/apps/builtins/dev_fleet/server.py
    here = Path(__file__).resolve()
    for parent in here.parents:
        if parent.name == "src" and (parent / "kiro_crew").is_dir():
            return parent.parent
    return None


def _staged_target() -> str | None:
    """The pointer target when a cutover is staged but NOT yet in effect.

    Non-None means the operator has committed a cutover that the gateway has not
    picked up: the next start lands on this checkout, and until then the running
    image is a different one. The UI renders this as its own persistent state so
    the pending restart survives a dismissed toast or a page reload.
    """
    pointed = live_target.read_target()
    if pointed is None:
        return None
    running = _running_checkout()
    if running is None or _same_path(str(pointed), str(running)):
        return None
    return str(pointed)


async def _live_worktree_path(*, fresh: bool = False) -> str | None:
    """Resolve the checkout the live gateway is RUNNING from (or None).

    ``fresh=True`` bypasses the 30s display cache -- destructive callers
    (worktree removal) must never authorize against a stale answer: the
    gateway can switch checkouts within the TTL window.
    """
    global _LIVE_WORKTREE, _LIVE_CHECK_AT
    now = time.monotonic()
    if not fresh and _LIVE_CHECK_AT and (now - _LIVE_CHECK_AT) < _LIVE_TTL:
        return _LIVE_WORKTREE
    _LIVE_CHECK_AT = now
    # The live-target pointer outranks every service-definition probe below —
    # but ONLY once the gateway is actually running it. A cutover always writes
    # the pointer, and on a host whose service cannot be driven it writes ONLY
    # the pointer, so the unit's WorkingDirectory still names the checkout the
    # gateway was installed from: reading the definition first would report that
    # stale checkout as live and leave `already_live` and `is_live` wrong.
    #
    # Honouring the pointer unconditionally is the opposite error, and the worse
    # one: between staging and the manual restart the pointer names a checkout
    # the gateway is NOT executing, so the fleet would mark it live while the old
    # image serves real data — the exact wrong conclusion this feature exists to
    # prevent. ``_running_checkout()`` is authoritative for what is executing, so
    # the pointer is only "live" when the two agree; otherwise it is staged
    # (see ``_staged_target``) and resolution falls through to the definition.
    pointed = live_target.read_target()
    if pointed is not None:
        running = _running_checkout()
        if running is None or _same_path(str(pointed), str(running)):
            _LIVE_WORKTREE = str(pointed)
            return _LIVE_WORKTREE
        _LIVE_WORKTREE = str(running)
        return _LIVE_WORKTREE
    if sys.platform == "darwin" and shutil.which("launchctl"):
        # launchd has no WorkingDirectory to query: the live target IS whatever
        # the agent's ProgramArguments symlink currently points at. Reading the
        # link is authoritative, needs no service query, and reflects a make-live
        # swap immediately.
        _LIVE_WORKTREE = _launchd_live_worktree()
        return _LIVE_WORKTREE
    if sys.platform != "linux" or not shutil.which("systemctl"):
        _LIVE_WORKTREE = None
        return None
    # Prefer WorkingDirectory: make-live always writes it alongside ExecStart,
    # and the baseline unit sets it too. ``--value`` prints the bare path with
    # no ``WorkingDirectory=`` prefix and, crucially, NO truncation at spaces —
    # so a checkout path containing a space resolves correctly. The old
    # ExecStart ``path=([^ ;]+)`` regex truncates at the first space, which
    # (now that make-live escapes space paths into the drop-in) would leave
    # is_live / already_live perpetually unmatched for such a worktree and
    # drive pointless repeat restarts.
    path = None
    rc, out, _err = await _run_cmd(
        ["systemctl", "--user", "show", _LIVE_GATEWAY_UNIT,
         "--property=WorkingDirectory", "--value"],
        timeout=5,
    )
    if rc == 0 and out.strip():
        path = out.strip()
    else:
        # Fallback: parse ExecStart's ``path=`` when WorkingDirectory is empty
        # (an older unit that predates the WorkingDirectory= directive).
        rc, out, _err = await _run_cmd(
            ["systemctl", "--user", "show", _LIVE_GATEWAY_UNIT, "-p", "ExecStart"],
            timeout=5,
        )
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
    extra: dict[str, str] = {}
    base = int(_GIT_ENV_NEUTRALIZERS["GIT_CONFIG_COUNT"])
    idx = base
    # SYSTEM scope first, then GLOBAL, mirroring git's own precedence: for a
    # multi-valued key like credential.helper the later entry wins, so the
    # operator's own global setting still overrides a machine-wide default.
    #
    # System scope is read at all because that is where macOS puts the operator's
    # helper: Xcode's Command Line Tools ship
    # `credential.helper = osxkeychain` in
    # /Library/Developer/CommandLineTools/usr/share/git-core/gitconfig, and a
    # stock install has NOTHING in global. Scanning only --global therefore left
    # the neutralizer's reset unrepaired on every stock macOS host, and `git
    # fetch` died with "could not read Username" — no tty to prompt on.
    #
    # Repo-LOCAL scope stays excluded. That is the attack surface the reset
    # exists for: a checkout Dev Fleet builds can write .git/config, and a helper
    # from there would run in the credential-bearing standard tier.
    for scope in ("--system", "--global"):
        rc, out, _err = await _run_cmd(
            ["git", "config", scope, "--get-regexp", r"^credential(\..+)?\.helper$"],
            timeout=5,
        )
        # A missing system gitconfig is rc != 0 with no output — normal, not an
        # error worth surfacing.
        if rc != 0 or not out:
            continue
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
                    " for config key %s (%s scope)", key, scope.lstrip("-"),
                )
                continue
            extra[f"GIT_CONFIG_KEY_{idx}"] = key
            extra[f"GIT_CONFIG_VALUE_{idx}"] = trusted_val
            idx += 1
            if idx - base >= 9:
                break
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
    node 18 against ``20 || >=22``), while ``ensure-node.sh`` installs a version
    chosen to satisfy the build.
    """
    return find_node_tool(name, _TRUSTED_PATH) or _trusted_bin(name)


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


_RUNS_MAX_COMPLETED = 50


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
                proc = await create_subprocess_limited(
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
    # `title` is a display field carried into the payload; `body` is fetched in
    # the SAME query (no extra gh call, so no added rate cost per refresh) and
    # kept INTERNAL (moved to `_body`) — it feeds issue-ref parsing but is
    # dropped from the payload by _redact_pr (which skips `_`-prefixed keys).
    rc, stdout, _ = await _run_cmd(
        ["gh", "pr", "list", "--repo", owner_repo, "--head", branch,
         "--json", "number,state,url,isDraft,title,body", "--state", "all", "--limit", "1"],
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
        if "body" in pr:
            pr["_body"] = pr.pop("body") or ""
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


# --- per-worktree context: issue/ticket links + purpose one-liner ---
#
# Best-effort and TTL-cached per branch exactly like the PR-state cache. Every
# field degrades to empty/None on any git/gh failure so context resolution can
# never break the fleet payload.

# GitHub issue references: keyworded (Fixes/Closes/Resolves #N) OR bare #N. The
# bare form is a superset, so a single "#<digits>" match — guarded by a
# trailing non-alphanumeric lookahead that rejects colour hexes (#1a2b, #fff)
# and version-ish tokens — covers both; dedup collapses the keyworded overlap.
_ISSUE_REF_RE = re.compile(r"#(\d{1,7})(?![0-9A-Za-z])")
# Ticket IDs (JIRA / Taskei style): PROJECT-1234.
_TICKET_ID_RE = re.compile(r"\b[A-Z][A-Za-z]{1,15}-\d{1,6}\b")
# Subjects that are pure version bumps — skipped when picking the one-liner.
_VERSION_BUMP_RE = re.compile(
    r"^\s*(?:chore(?:\([^)]*\))?:\s*)?"
    r"(?:bump\b|release\b|bump version\b|version bump\b|v?\d+\.\d+\.\d+\s*$)",
    re.IGNORECASE,
)
# Payload growth caps (keep fleet rows modest).
_CTX_MAX_ISSUES = 8
_CTX_MAX_TICKETS = 5


def _extract_issue_refs(text: str) -> list[int]:
    """Ordered-unique GitHub issue numbers referenced in *text*."""
    seen: list[int] = []
    for m in _ISSUE_REF_RE.finditer(text or ""):
        n = int(m.group(1))
        if n not in seen:
            seen.append(n)
    return seen


def _extract_ticket_ids(text: str) -> list[str]:
    """Ordered-unique ticket IDs (PROJECT-1234) referenced in *text*."""
    seen: list[str] = []
    for m in _TICKET_ID_RE.finditer(text or ""):
        t = m.group(0)
        if t not in seen:
            seen.append(t)
    return seen


def _render_ticket_url(template: str, tid: str) -> str | None:
    """Render a ticket URL from *template* ({id} placeholder). Returns None
    when the template is empty or has no {id} — chips then render unlinked."""
    if not template or "{id}" not in template:
        return None
    return template.replace("{id}", tid)


def _is_version_bump(subject: str) -> bool:
    return bool(_VERSION_BUMP_RE.match(subject or ""))


def _pick_summary(subjects: list[str]) -> str | None:
    """Latest non-merge commit subject, skipping trivial version bumps; falls
    back to the latest subject when every candidate is a bump."""
    for s in subjects:
        if s and not _is_version_bump(s):
            return s
    return subjects[0] if subjects else None


def _issue_url(base: str | None, n: int) -> str | None:
    return f"{base}/issues/{n}" if base else None


def _parse_html_repo_base(remote_url: str) -> str | None:
    """Derive the repo's browser (html) base URL from a git remote URL,
    normalising scp-style and scheme URLs to https. None when unparseable."""
    url = (remote_url or "").strip()
    if not url:
        return None
    # scp-like: [user@]host:owner/repo(.git)  — (?!/) rejects a scheme "://".
    m = re.match(r"^(?:[^@/]+@)?([\w.\-]+):(?!/)(.+?)(?:\.git)?/?$", url)
    if m:
        return f"https://{m.group(1)}/{m.group(2)}"
    # scheme://[user@]host[:port]/owner/repo(.git)
    m = re.match(
        r"^[A-Za-z][\w+.\-]*://(?:[^@/]+@)?([\w.\-]+)(?::\d+)?/(.+?)(?:\.git)?/?$",
        url,
    )
    if m:
        return f"https://{m.group(1)}/{m.group(2)}"
    return None


_HTML_BASE: str | None = None


async def _html_repo_base() -> str | None:
    """Cached browser base URL of the upstream repo (for issue links). Derived
    from the remote URL; falls back to github.com/<owner/repo> (PR resolution
    is gh/github-only anyway)."""
    global _HTML_BASE
    if _HTML_BASE:
        return _HTML_BASE
    remote = await _upstream_remote()
    rc, out, _ = await _run_cmd(
        ["git", "-C", MAIN_REPO, "remote", "get-url", remote], timeout=5
    )
    if rc == 0:
        base = _parse_html_repo_base(out.strip())
        if base:
            _HTML_BASE = base
            return base
    owner_repo = await _get_owner_repo()
    if owner_repo:
        _HTML_BASE = f"https://github.com/{owner_repo}"
    return _HTML_BASE


def _load_dev_fleet_cfg() -> dict:
    """Read the ``dev_fleet`` config section (config.json + local overlay),
    lazily and best-effort. Never raises; a missing file/section -> {}. Read
    directly rather than through KiroCrewConfig (a separate process owns the
    validated loader) so a purely cosmetic template needs no schema dependency
    and can never break the fleet payload."""
    section: dict = {}
    try:
        from kiro_crew.config.loader import config_dir
        base = config_dir()
    except Exception:  # noqa: BLE001
        return section
    for fname in ("config.json", "config.local.json"):
        p = base / fname
        try:
            if not p.is_file():
                continue
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if isinstance(raw, dict) and isinstance(raw.get("dev_fleet"), dict):
            section.update(raw["dev_fleet"])
    return section


# per-branch context cache (mirrors _PR_CACHE / _PR_TTL)
_CTX_CACHE: dict[str, dict] = {}
_CTX_TTL = 55


async def _resolve_context(
    branch: str | None, subjects: list[str], bodies: list[str], pr_body: str | None
) -> dict:
    """Assemble {issues, tickets, summary} from pre-extracted text. The html
    base is resolved ONLY when issue refs exist and the ticket template ONLY
    when ticket IDs exist — so a refless worktree triggers no extra lookup (and
    no owner/repo cache write), keeping callers side-effect-free."""
    issue_nums = _extract_issue_refs("\n".join([pr_body or "", *subjects, *bodies]))
    ticket_ids = _extract_ticket_ids("\n".join([branch or "", *subjects]))
    html_base = await _html_repo_base() if issue_nums else None
    tpl = str(_load_dev_fleet_cfg().get("ticket_url_template") or "") if ticket_ids else ""
    summary = _pick_summary(subjects)
    return {
        "issues": [
            {"number": n, "url": _issue_url(html_base, n)}
            for n in issue_nums[:_CTX_MAX_ISSUES]
        ],
        "tickets": [
            {"id": t, "url": _render_ticket_url(tpl, t)}
            for t in ticket_ids[:_CTX_MAX_TICKETS]
        ],
        "summary": _redact(summary) if summary else None,
    }


async def _build_context(branch: str, path: str, pr: dict | None) -> dict:
    """Resolve {issues, tickets, summary} for a worktree branch. Best-effort:
    the PR body comes from the already-cached PR dict (no NEW gh call) and the
    commit subjects/bodies from a local `git log`; any failure yields empties."""
    remote = await _upstream_remote()
    subjects: list[str] = []
    bodies: list[str] = []
    # Subject + body of the last ~10 non-merge commits, record-separated by
    # 0x1e (bodies contain newlines, so newline can't delimit records).
    log = await _git(
        path, "log", f"{remote}/{BASE_BRANCH}..HEAD", "--no-merges", "-10",
        "--format=%s%x1f%b%x1e", timeout=12,
    )
    if log:
        for rec in log.split("\x1e"):
            rec = rec.strip("\n")
            if not rec.strip():
                continue
            subj, _sep, body = rec.partition("\x1f")
            subj = subj.strip()
            if subj:
                subjects.append(subj)
            if body.strip():
                bodies.append(body)
    return await _resolve_context(branch, subjects, bodies, (pr or {}).get("_body"))


async def _context_cached(branch: str | None, path: str, pr: dict | None) -> dict:
    """TTL-cached per-branch context (same approach as _pr_status_cached)."""
    empty: dict = {"issues": [], "tickets": [], "summary": None}
    if not branch or branch == BASE_BRANCH:
        return empty
    now = time.time()
    ent = _CTX_CACHE.get(branch)
    if ent and (now - ent["ts"]) < _CTX_TTL:
        return ent["data"]
    try:
        data = await _build_context(branch, path, pr)
    except Exception:  # noqa: BLE001 — context is best-effort, never break fleet
        data = empty
    _CTX_CACHE[branch] = {"data": data, "ts": time.time()}
    return data


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
        elif line == "prunable" or line.startswith("prunable "):
            # git flags an entry `prunable` when its checkout directory is gone
            # but the admin record survives (a `rm -rf` with no
            # `git worktree prune`). The reason text is optional.
            current["prunable"] = line[len("prunable"):].strip() or "unknown"
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
        raw = (stderr or stdout or "").strip()
        if "sandbox unavailable" in raw:
            # Do NOT clip to the generic git-error length here. The sandbox layer
            # puts the *remedy* (which opt-in to set, or that an EPERM is a
            # Seatbelt nesting artifact rather than a missing backend) AFTER a
            # ~180-char preamble, so a tight cap would surface the diagnosis and
            # swallow the fix. Keep a generous bound purely to stop an unbounded
            # stderr reaching the UI.
            raise RuntimeError(raw[:_SANDBOX_ERR_MAX])  # already prefixed by _run_cmd
        # Every other git failure was previously swallowed into a silent [] —
        # which the UI renders as the "No worktrees found / Nothing under the
        # worktrees root yet" empty state. When MAIN_REPO is wrong that empty
        # state is a lie: the fleet is not empty, it is unreadable. This is the
        # default condition on packaged installs, where KIROCREW_PROJECT_DIR
        # points at the app bundle (no .git) and discovery falls through to the
        # hardcoded ~/kirocrew — raise instead, so api_dev_fleet_fleet's
        # existing error path renders the Discovery Error banner with the path
        # it tried and the remedy.
        # The .git probe is a filesystem stat — on a wedged network mount it
        # can block indefinitely, and this branch is reachable precisely when
        # the checkout is unhealthy (git already failed or timed out against
        # it). Same "Blocking — executor only" convention as _is_checkout().
        loop = asyncio.get_running_loop()
        repo_is_git = await loop.run_in_executor(
            subprocess_executor(), (Path(MAIN_REPO) / ".git").exists
        )
        if not repo_is_git:
            raise RuntimeError(
                f"main checkout not found: {MAIN_REPO} is missing or not a git "
                "checkout. Set KIROCREW_DEVFLEET_REPO to your Kiro Crew checkout, "
                "or clone it to ~/kirocrew."
            )
        # The repo exists but git failed for some other reason (corrupt repo,
        # permissions): surface git's own message, redacted and bounded.
        raise RuntimeError(
            f"git worktree discovery failed in {MAIN_REPO}: "
            f"{_redact(raw)[:_GIT_ERR_MAX] or 'unknown git error'}"
        )
    entries = _parse_worktree_porcelain(stdout)
    # `git worktree list --porcelain` always lists the primary checkout
    # first — that is the authoritative main, regardless of whether
    # MAIN_REPO itself points at a linked worktree (it is only the
    # repository discovery hint).
    for i, e in enumerate(entries):
        e["is_main"] = (i == 0)
    # A `prunable` entry has no checkout on disk, so every git call against its
    # path fails and it renders as a ghost row with no branch, behind count or
    # timestamp — and no refresh ever clears it, because git keeps reporting the
    # record until `git worktree prune` runs. Drop those. The primary checkout
    # is never filtered: it anchors `is_main`, and losing it would promote a
    # linked worktree to main.
    return [e for e in entries if e.get("is_main") or not e.get("prunable")]


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
# The single in-flight rebuild. A rebuild costs one `gh pr` round-trip per
# branch, so the background revalidate and every `fresh=1` request coalesce onto
# the SAME task instead of each starting their own. Holding the reference here
# also keeps a fire-and-forget rebuild from being garbage-collected mid-flight.
_FLEET_INFLIGHT: asyncio.Task[dict] | None = None
# Worktrees evicted by `_fleet_forget`, keyed to an eviction counter. A rebuild
# that started BEFORE an eviction still reads the worktree from git, so storing
# its result would resurrect the row the eviction just dropped. Entries are
# reaped by the first build that started after them.
_FLEET_EPOCH = 0
_FLEET_TOMBSTONES: dict[str, int] = {}


def _drop_worktrees(data: dict, names: set[str]) -> dict:
    """A copy of ``data`` without the named worktrees.

    Copies rather than mutates: a concurrent response may still hold the old
    dict while aiohttp serializes it.
    """
    wts = data.get("worktrees")
    if not isinstance(wts, list):
        return data
    kept = [w for w in wts if w.get("name") not in names]
    if len(kept) == len(wts):
        return data
    return {**data, "worktrees": kept}


async def _fleet_build() -> dict:
    global _FLEET_TOMBSTONES
    started = _FLEET_EPOCH
    data = await _build_fleet()
    # Removals that landed DURING this build are invisible to it — the git state
    # it read predates them. Re-apply them so a slow build cannot put back a row
    # an eviction already removed.
    data = _drop_worktrees(
        data, {n for n, e in _FLEET_TOMBSTONES.items() if e > started}
    )
    # Evictions that predate this build's start need no tombstone: `_fleet_forget`
    # runs only after git has removed the worktree, so no later build can see it.
    _FLEET_TOMBSTONES = {n: e for n, e in _FLEET_TOMBSTONES.items() if e > started}
    _FLEET_CACHE["data"] = data
    _FLEET_CACHE["ts"] = time.monotonic()
    return data


def _fleet_rebuild_task() -> asyncio.Task[dict]:
    """The in-flight rebuild, starting one if none is running."""
    global _FLEET_INFLIGHT
    task = _FLEET_INFLIGHT
    if task is None or task.done():
        task = asyncio.ensure_future(_fleet_build())
        _FLEET_INFLIGHT = task
    return task


async def _fleet_refresh() -> dict:
    # shield: a client disconnecting mid-request must not cancel a rebuild that
    # other waiters (and the cache) depend on. Coalescing onto a build already in
    # flight is safe even for a `fresh=1` request that raced a removal:
    # `_fleet_build` re-applies any eviction that landed mid-build.
    return await asyncio.shield(_fleet_rebuild_task())


def _fleet_forget(name: str) -> None:
    """Evict one worktree from the cached snapshot and mark it for rebuild.

    ``_fleet_cached`` is stale-while-revalidate: once past the TTL it serves the
    PREVIOUS snapshot and only schedules a rebuild behind it. Without this hook a
    just-removed worktree keeps rendering for the length of a full rebuild, so
    the UI shows rows that no longer exist and refreshing does not help. Evicting
    the row makes the very next response truthful at zero rebuild latency, and
    zeroing the timestamp schedules the rebuild that refreshes the rest.
    """
    global _FLEET_EPOCH
    _FLEET_EPOCH += 1
    _FLEET_TOMBSTONES[name] = _FLEET_EPOCH
    data = _FLEET_CACHE["data"]
    if isinstance(data, dict):
        _FLEET_CACHE["data"] = _drop_worktrees(data, {name})
    _FLEET_CACHE["ts"] = 0.0


def _log_fleet_rebuild_failure(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("dev-fleet: background fleet rebuild failed: %s", exc)


async def _fleet_cached() -> dict:
    data, ts = _FLEET_CACHE["data"], _FLEET_CACHE["ts"]
    if data is None:
        return await _fleet_refresh()
    if time.monotonic() - ts > _FLEET_TTL:
        task = _fleet_rebuild_task()
        # This caller does not await the rebuild, so consume its exception here
        # or asyncio reports it as never-retrieved.
        if not task.done():
            task.add_done_callback(_log_fleet_rebuild_failure)
    return data


#: Memoized ``(key, reason)``. The comparison needs real path resolution —
#: a symlinked checkout otherwise reads as a mismatch — and that is filesystem
#: IO, which on a network-backed checkout can stall. So it runs on the executor,
#: once per distinct key, rather than on the event loop for every ``/fleet`` poll.
_SERVING_REASON: "tuple[tuple[str, tuple[str, ...]], str | None] | None" = None


def _is_checkout(path: str) -> bool:
    """Whether *path* is a real git checkout. Blocking — executor only.

    ``.git`` is tested rather than mere existence, and as a path rather than a
    directory, because a linked worktree's ``.git`` is a FILE. An empty or absent
    directory is not something Dev Fleet can be said to manage.
    """
    if not path:
        return False
    try:
        return (Path(path) / ".git").exists()
    except (OSError, RuntimeError, ValueError):
        return False


def _serving_install_reason_sync(
    main_repo: str, managed: "tuple[str, ...]"
) -> str | None:
    """Why the install serving this dashboard is not one Dev Fleet manages.

    Blocking — resolves paths. Call it through an executor, never on the loop.

    Dev Fleet drives a set of checkouts, but the backend answering these routes
    can be a different install altogether — a published desktop bundle, say,
    whose own Pull+Build predates the dist-staging step. Every control then keeps
    reporting success while doing an incomplete job: the checkout fast-forwards,
    the bundle it serves does not, and a Restart button whose eligibility that
    older backend computes never appears at all. Saying nothing is what turns a
    one-line diagnosis into a long session chasing the downstream symptoms.

    Every discovered worktree counts as managed, not just the primary checkout:
    Make live deliberately points the gateway at a linked worktree that lives
    outside it, and warning about a state this app just created would train the
    user to dismiss the one signal built for the takeover case.
    """
    # __file__ is the strongest available evidence of which install is RUNNING:
    # it is this very module, so it cannot disagree with the process the way a
    # PATH-resolved binary can.
    pkg = Path(__file__).resolve().parents[3]
    for candidate in (main_repo, *managed):
        if not candidate:
            continue
        try:
            root = Path(candidate).resolve()
        except (OSError, RuntimeError, ValueError):
            # An unusable entry is skipped, not raised: /fleet is a read and every
            # other field still describes the fleet correctly. RuntimeError is in
            # the tuple because a symlink cycle surfaces as one rather than as an
            # OSError — the same reason beacon.is_default_home() catches it.
            continue
        if pkg == root or root in pkg.parents:
            return None
    if not _is_checkout(main_repo):
        # Nothing is actually being managed. MAIN_REPO defaults to ~/kirocrew
        # whether or not it exists, so a desktop-bundle or pip install with no
        # source checkout — the out-of-the-box case — would otherwise get a
        # permanent warning whose remedy ("start the gateway from <path>") names
        # a directory that is not there. A dead-end instruction on every visit is
        # how a signal gets trained away.
        return None
    return (
        "This dashboard is served by a different install than the checkout you are "
        "managing, so Pull+Build here does not change the code that runs. "
        f"Start the gateway from {_redact(main_repo)}, or Make live onto it. "
        f"Serving now: {_redact(str(pkg))} — an install older than the pulled "
        "revision may not refresh the dashboard bundle."
    )


async def _serving_install_reason(worktrees: "list[dict]") -> str | None:
    global _SERVING_REASON
    managed = tuple(sorted(
        str(wt["path"]) for wt in worktrees if wt.get("path")
    ))
    key = (MAIN_REPO, managed)
    if _SERVING_REASON is not None and _SERVING_REASON[0] == key:
        return _SERVING_REASON[1]
    loop = asyncio.get_running_loop()
    reason = await loop.run_in_executor(
        subprocess_executor(),
        functools.partial(_serving_install_reason_sync, MAIN_REPO, managed),
    )
    _SERVING_REASON = (key, reason)
    return reason


async def _build_fleet() -> dict:
    live_path = await _live_worktree_path()
    staged_path = _staged_target()
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
        loop = asyncio.get_running_loop()
        # Build state is a plain filesystem check (``.venv`` binary present /
        # ``static/dist`` directory present) and is therefore knowable on EVERY
        # platform — report it even where pods cannot run, so the Fleet view
        # still tells the truth about which worktrees are built.
        if _POD_IMPORTED and not is_main:
            try:
                has_venv = await loop.run_in_executor(
                    subprocess_executor(), prov.has_venv, Path(path)
                )
                has_dist = await loop.run_in_executor(
                    subprocess_executor(), prov.has_dist, Path(path)
                )
            except Exception:  # noqa: BLE001
                pass
        # Pod state, by contrast, only exists where pods can run.
        if _POD_AVAILABLE and cfg and not is_main:
            try:
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

        # Per-worktree context (issue/ticket links + purpose one-liner). Skipped
        # for the main checkout (no feature context). Best-effort: never raises.
        ctx = (
            await _context_cached(branch, path, pr)
            if branch and not is_main
            else {"issues": [], "tickets": [], "summary": None}
        )

        wts.append({
            # "name" doubles as the opaque identifier for follow-up actions
            # (validated against the discovered set on every call); display
            # fields sourced from git/gh output are redacted.
            "name": name, "path": _redact(path), "is_main": is_main,
            "running": running, "port": port, "health": health,
            "is_live": live_path is not None and _same_path(path, live_path),
            "is_staged": staged_path is not None and _same_path(path, staged_path),
            "has_venv": has_venv, "has_dist": has_dist,
            "branch": _redact(g["branch"] or branch or ""), "head": g["head"] or wt.get("head", "")[:7],
            "dirty": g["dirty"], "behind": g["behind"],
            "pr": _redact_pr(pr), "shipped": shipped,
            "issues": ctx["issues"], "tickets": ctx["tickets"],
            "summary": ctx["summary"],
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
        # Non-null while a cutover is staged but not yet running: the UI renders a
        # persistent pending-restart state from this, so the instruction outlives
        # the toast that announced it.
        "staged_target": _redact(staged_path) if staged_path else None,
        "manual_restart": _manual_restart_command(),
        # WHY the gateway cannot be restarted/repointed from here, when it
        # cannot. Same lesson as pods_unavailable_reason below: the previous
        # behaviour was to hide Restart and Make live with no explanation, so a
        # macOS user saw a Pull+Build succeed with no way to apply it and
        # nothing on screen saying why. ``None`` when the service is drivable.
        "gateway_service_reason": await _gateway_service_reason(),
        # Non-null when the backend answering this request is NOT the checkout
        # below. Reported for the same reason as gateway_service_reason: the
        # controls stay clickable and keep succeeding, so nothing else on screen
        # would ever reveal that the managed code is not the running code.
        "serving_install_reason": await _serving_install_reason(worktrees),
        # Whether pods can run on THIS host, and if not, why. Previously
        # _POD_ERROR was computed and then never read by anything, so a
        # non-Linux user saw pod controls that silently failed with no
        # explanation. The UI uses these to disable those controls and say why.
        "pods_available": _POD_AVAILABLE,
        "pods_unavailable_reason": _POD_ERROR or None,
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

    # Context (issue/ticket links + purpose one-liner) assembled from the
    # commits already fetched above (their subjects) + the PR body — no extra
    # git log, so the detail endpoint keeps to a single own-commits log call.
    ctx = (
        await _resolve_context(branch, [c["subject"] for c in commits], [],
                               (pr or {}).get("_body"))
        if branch and not is_main
        else {"issues": [], "tickets": [], "summary": None}
    )
    return {
        "name": name, "path": _redact(path),
        "branch": _redact(g["branch"] or branch or ""), "head": g["head"],
        "dirty": g["dirty"], "own_commits": own_commits,
        "real_dirty": await _real_dirty(path),
        "pr": _redact_pr(pr), "pr_merged": _is_pr_merged(pr),
        "issues": ctx["issues"], "tickets": ctx["tickets"],
        "summary": ctx["summary"],
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
_POSIX_SAFE_ENV_KEYS = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TMPDIR",
    "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
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
    "SystemRoot", "SystemDrive", "windir", "ComSpec", "PATHEXT",
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "APPDATA", "LOCALAPPDATA", "ProgramData",
    "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432",
    "TEMP", "TMP", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
)

_SAFE_ENV_KEYS = _POSIX_SAFE_ENV_KEYS + (
    _WINDOWS_SAFE_ENV_KEYS if platform_compat.IS_WINDOWS else ()
)
_SAFE_ENV_KEYS_FOLDED = frozenset(name.upper() for name in _SAFE_ENV_KEYS)


def _is_safe_env_key(key: str) -> bool:
    """Whether *key* is allowlisted, honoring Windows' case-insensitive env.

    On Windows, environment names are case-INSENSITIVE and CPython's
    ``os.environ`` upper-cases every key, so ``os.environ.items()`` yields
    ``SYSTEMROOT`` — never the ``SystemRoot`` spelling Microsoft documents and
    that these allowlists write. A literal membership test therefore drops
    exactly the variables the allowlist was extended to carry, and the failure
    is silent at the boundary and only surfaces in the child as an unrelated
    error: without ``SystemRoot`` a spawned ``git`` cannot initialize Winsock,
    so a fetch dies with ``getaddrinfo() thread failed to start``.

    Folding on Windows only, rather than upper-casing the lists, keeps POSIX
    exact: ``PATH`` and ``Path`` are genuinely different variables there, and a
    case-insensitive match would let a lookalike through. Mirrors
    ``apps.registry._is_safe_env_key`` and
    ``kiro_prerequisite._allowlisted_env``.
    """
    if platform_compat.IS_WINDOWS:
        return key.upper() in _SAFE_ENV_KEYS_FOLDED
    return key in _SAFE_ENV_KEYS


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
    # Resolve the node toolchain off the loop before building the pod env:
    # `pod up` runs the provision chain (npm ci + vite) when asked to.
    await _warm_build_path()
    cmd = _find_cli() + ["pod", "up", name, "--json"]
    rc, stdout, stderr = await _run_cmd(cmd, cwd=MAIN_REPO, env=_pod_env(), timeout=180)
    if rc != 0:
        return {"ok": False, "error": _redact(stderr or stdout)}
    # Post-start verification (symmetry with _pod_down): rc==0 is not proof the
    # pod is up. Confirm the unit is actually active, else fail closed rather
    # than flash a false "started" — the same false-success class as a false
    # "stopped", in the opposite direction.
    cfg = _load_cfg()
    if _POD_AVAILABLE and cfg:
        try:
            loop = asyncio.get_running_loop()
            active = await loop.run_in_executor(
                subprocess_executor(), rt.active_names, cfg
            )
            if name not in active:
                return {"ok": False, "error": "pod not active after start"}
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": f"cannot verify pod start: {_redact(str(exc))}",
            }
    try:
        return {"ok": True, **json.loads(stdout)}
    except (json.JSONDecodeError, ValueError):
        return {"ok": True, "output": stdout}


async def _pod_down(name: str) -> dict:
    guard = await _pod_checkout_guard(name)
    if guard:
        return {"ok": False, "error": guard}
    await _warm_build_path()
    cmd = _find_cli() + ["pod", "down", name]
    rc, stdout, stderr = await _run_cmd(cmd, cwd=MAIN_REPO, env=_pod_env(), timeout=30)
    if rc != 0:
        return {"ok": False, "error": _redact(stderr or stdout)}
    # Post-stop verification: a CLI exit 0 is NOT proof the unit stopped (a
    # broken `-m` entry point can no-op with rc 0, and a real stop
    # can still fail or time out). Re-check the live unit state and fail CLOSED
    # if the pod is still active — mirrors the post-shutdown recheck in
    # _worktree_remove so "Stopped" is never reported for a pod still running.
    cfg = _load_cfg()
    if _POD_AVAILABLE and cfg:
        try:
            loop = asyncio.get_running_loop()
            active = await loop.run_in_executor(
                subprocess_executor(), rt.active_names, cfg
            )
            if name in active:
                return {"ok": False, "error": "pod still active after shutdown"}
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": f"cannot verify pod shutdown: {_redact(str(exc))}",
            }
    return {"ok": True, "error": None}


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
        await _warm_build_path()
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
async def _worktree_remove(
    name: str,
    force: bool = False,
    progress: Callable[[str], None] | None = None,
) -> dict:
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

    live_path = await _live_worktree_path(fresh=True)
    if live_path is not None and _same_path(path, live_path):
        return {"ok": False, "error": (
            "refusing: this worktree is running the live gateway -- "
            "switch the gateway to another checkout first"
        )}
    # The systemd unit only covers service-managed gateways. A gateway (and
    # this backend, its subprocess) launched directly from a feature worktree
    # is invisible to it -- so also refuse when the target IS the checkout our
    # own running code was imported from.
    own_checkout = _own_checkout_path()
    if own_checkout is not None and _same_path(path, own_checkout):
        return {"ok": False, "error": (
            "refusing: this worktree is the checkout the current gateway "
            "process is running from -- switch checkouts first"
        )}

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
    # Verification (dirty/PR/OID guards above) is the "verifying" phase; from
    # here we enter pod shutdown, then the serialized git mutation. These phase
    # signals drive the per-item prune checklist (no-op for other callers).
    if progress is not None:
        progress("stopping_pod")
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

    if progress is not None:
        progress("removing")
    # Serialize the destructive git mutations. Concurrent `git worktree remove`
    # / `update-ref -d` against the shared MAIN_REPO would race on the worktree
    # admin dir and packed-refs locks, so only one worker mutates at a time.
    async with _GIT_MUTATION_LOCK:
        # TOCTOU recheck: the pod-inactive verification above happened BEFORE
        # this lock was acquired. Under parallel prune a worker can queue here
        # behind other removals — long enough for another session to restart
        # the pod. Removing the checkout under a live pod would leave its
        # gateway running from deleted files, so re-verify inactivity now.
        if _POD_AVAILABLE and cfg:
            try:
                loop = asyncio.get_running_loop()
                active3 = await loop.run_in_executor(
                    subprocess_executor(), rt.active_names, cfg
                )
                if name in active3:
                    return {"ok": False, "error": (
                        "pod became active again before removal — refusing"
                    )}
            except Exception as exc:
                return {
                    "ok": False,
                    "error": f"cannot re-verify pod state before removal: {_redact(str(exc))}",
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

    # Every removal path lands here — the single-worktree handler, each parallel
    # prune worker, and the auto-prune reaper — so this is the one place the
    # cached snapshot has to be told the row is gone.
    _fleet_forget(name)
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

    await _warm_build_path()
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
    # Both binary lookups stat the filesystem (`_trusted_bin` walks the trusted
    # dirs; `_toolchain_bin` adds a `shutil.which` over the node bin dirs, which
    # may be NFS-backed). Resolve them together on the executor so /api/sync
    # cannot stall the gateway's requests and liveness behind a directory scan.
    loop = asyncio.get_running_loop()
    git_bin, npm_bin = await loop.run_in_executor(
        subprocess_executor(),
        lambda: (_trusted_bin("git"), _toolchain_bin("npm")),
    )
    if git_bin is None:
        return {"ok": False, "error": (
            f"no trusted executable for 'git' in {_TRUSTED_PATH}"
        )}
    if npm_bin is None:
        # Drop the memoized resolution so the remedy this message advertises
        # actually works. `node_bin_dirs()` is lru_cached and `_BUILD_PATH_CACHE`
        # is set once per process, so in a long-lived gateway a user who ran
        # ensure-node.sh and hit Pull+build again would get this SAME error --
        # the marker file is never re-read. Invalidating on the failure path
        # makes the retry fresh. Only on failure: a successful resolution is
        # worth keeping cached, and this path is user-initiated, not a loop.
        _invalidate_toolchain_cache()
        return {"ok": False, "error": (
            "npm not found. Kiro Crew looks for a Node toolchain in "
            "<data-home>/node-bin-dir (written by ensure-node.sh), then in "
            "mise / asdf / nvm / fnm / volta install dirs, then in "
            f"{_TRUSTED_PATH}. Fix: run `bash ensure-node.sh` in the main "
            "checkout and press Pull + build again — no restart needed. To point "
            "at a toolchain by hand instead, set "
            "KIROCREW_NODE_BIN_DIR=/abs/path/to/node/bin in the gateway's "
            "service environment; that one does need a restart, because a "
            "running process cannot see a new environment variable."
        )}
    raw_steps: list[tuple[list[str], str, dict, str]] = [
        ([git_bin, "fetch", remote, BASE_BRANCH], "standard",
         _build_env(with_credentials=True), "Pull"),
        ([git_bin, "merge", "--ff-only", f"{remote}/{BASE_BRANCH}"], "strict", _build_env(), "Pull"),
        ([str(target_py), "-m", "pip", "install", "-e", "."], "strict", _build_env(), "pip install"),
    ]
    # The whole FRONTEND half of the sync is skipped on an edition checkout.
    #
    # The build runs under _build_env(), whose allowlist (_SAFE_ENV_KEYS) drops
    # KIROCREW_EDITION_DIR and KIROCREW_ALLOW_EDITION, so on an edition
    # composition root `npm run build` can only compile the STOCK SPA -- and vite
    # builds with emptyOutDir, so it OVERWRITES website/dist. On a source-tree
    # install frontend.ensure_dev_dist_symlink() has pointed static/dist at
    # website/dist, which means the build alone replaces the served edition
    # dashboard with upstream's, with or without a staging step. Skipping the
    # build is therefore the only way to make this safe, and it costs an edition
    # nothing: the only artifact this path could produce for it is a stock SPA it
    # must never serve. It is the same call frontend's own
    # edition_sources_missing() guard already makes -- leave the shipped bundle
    # alone rather than degrade it.
    #
    # This backend is a SEPARATE process started with apps.registry.minimal_env(),
    # which strips KIROCREW_EDITION_DIR, so the guard would read "stock" on every
    # install and never fire. apps/backend.py therefore propagates that one var
    # explicitly, the same way it already propagates KIROCREW_PROJECT_DIR.
    if frontend.edition_configured():
        logger.info(
            "dev-fleet: skipping the frontend build and dist staging -- this is "
            "an edition checkout and the sync build cannot recompose the "
            "edition; the shipped bundle is left in place"
        )
    else:
        raw_steps += [
            ([npm_bin, "ci", "--prefix", "website"], "strict", _build_env(), "npm ci"),
            # Build and stage as ONE step, holding the staging lock across both.
            # `npm run build` empties website/dist, so a peer flow (the
            # dashboard's own update, pod provisioning) staging concurrently
            # would copy a partially written tree — and a bundle's lazy chunks
            # are not reachable from index.html, so no post-hoc inspection of
            # the copy detects that reliably. Covering only the copy is not
            # enough; the holder has to span the build.
            #
            # Run with THIS backend's interpreter, not the target checkout's, for
            # the same reason the staging step does: the logic is
            # revision-independent, while resolving it from the target would make
            # the step's very EXISTENCE contingent on the pulled revision
            # carrying build_and_stage, turning an older target into an
            # ImportError that fails the whole Pull+Build. The repo to build and
            # npm's resolved trusted path are passed in rather than re-resolved.
            ([sys.executable, "-c",
              "import sys;from kiro_crew.frontend import build_and_stage;"
              "sys.exit(0 if build_and_stage(sys.argv[1], npm=sys.argv[2]) else 1)",
              MAIN_REPO, npm_bin],
             "strict", _build_env(), "npm build + stage"),
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
# Per-item state machine (``items``) drives the frontend checklist, while the
# top-level ``running``/``total``/``done``/``current``/``results`` fields are
# kept for backward compatibility (auto-prune reaper + any existing consumers).
_PRUNE_STATE: dict = {
    "running": False, "total": 0, "done": 0, "current": None,
    "results": [], "items": {},
}
_PRUNE_LOCK = asyncio.Lock()
# Cap on concurrent per-item prune phases (fresh gh verdict + pod shutdown).
_PRUNE_CONCURRENCY = 4
# Serializes the destructive git mutations (`git worktree remove` +
# `update-ref -d`) across ALL removal paths — concurrent prune workers, the
# single-worktree remove handler, and the auto-prune reaper — because they all
# mutate the shared MAIN_REPO ``.git`` state (worktree admin dir + packed-refs).
# Uncontended in the sequential paths; only the parallel prune workers ever
# queue on it.
_GIT_MUTATION_LOCK = asyncio.Lock()


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
    # Deduplicate while preserving order: the API accepts any list of names,
    # and a duplicate would spawn two workers racing to remove the SAME
    # worktree — the second one then reports a spurious failure over the
    # first one's success.
    names = list(dict.fromkeys(names))
    async with _PRUNE_LOCK:
        if _PRUNE_STATE["running"]:
            return {"ok": False, "error": "prune already running"}
        _PRUNE_STATE.update({
            "running": True, "total": len(names), "done": 0, "current": None,
            "results": [],
            "items": {nm: {"status": "pending", "error": None} for nm in names},
        })

    items = _PRUNE_STATE["items"]
    sem = asyncio.Semaphore(_PRUNE_CONCURRENCY)
    # ``current`` is kept for API-shape compatibility but under parallel
    # execution it is BEST-EFFORT: one of the currently in-flight items (or
    # None when idle). ``active`` tracks in-flight names so a finished worker
    # can hand ``current`` to a still-running one instead of leaving a
    # completed name dangling.
    active: set[str] = set()

    async def _prune_one(nm: str) -> None:
        # The expensive phases (fresh gh verdict + pod shutdown) run
        # concurrently, capped by the semaphore. The destructive git mutation
        # inside _worktree_remove is serialized on _GIT_MUTATION_LOCK. Every
        # item finalizes EXACTLY once (in ``finally``) to a terminal status and
        # bumps ``done`` — so one item failing (or raising) never wedges the
        # batch or stops the others.
        async with sem:
            active.add(nm)
            _PRUNE_STATE["current"] = nm
            status = "failed"
            error: str | None = None
            result: dict = {"name": nm, "ok": False}
            try:
                items[nm]["status"] = "verifying"
                # Re-resolve and require a fresh prunable verdict immediately
                # before removal — the API accepts any discovered name, so a
                # clean-but-recent worktree must be rejected here.
                target, err = await _find_worktree(nm)
                if target is None:
                    error = err
                    result = {"name": nm, "ok": False, "error": err}
                else:
                    verdict = await _prunable(target["path"], target.get("branch"))
                    if not verdict.get("ok"):
                        error = f"not prunable: {verdict.get('code', 'unknown')}"
                        result = {"name": nm, "ok": False, "error": error}
                    else:
                        def _progress(phase: str, _nm: str = nm) -> None:
                            # phase in {"stopping_pod", "removing"}
                            items[_nm]["status"] = phase

                        res = await _worktree_remove(nm, force=False, progress=_progress)
                        result = {"name": nm, **res}
                        if res.get("ok"):
                            status, error = "done", None
                        else:
                            status, error = "failed", res.get("error")
            except Exception as exc:  # noqa: BLE001
                error = _redact(str(exc))
                result = {"name": nm, "ok": False, "error": error}
                logger.exception("dev-fleet prune: item %r failed", nm)
            finally:
                items[nm].update(status=status, error=error)
                _PRUNE_STATE["results"].append(result)
                _PRUNE_STATE["done"] += 1
                active.discard(nm)
                # Never leave a COMPLETED name in ``current``: hand it to any
                # still-in-flight item, or None when this was the last one.
                if _PRUNE_STATE["current"] == nm:
                    _PRUNE_STATE["current"] = next(iter(active), None)

    async def _work() -> None:
        try:
            await asyncio.gather(
                *(_prune_one(nm) for nm in names), return_exceptions=True
            )
        finally:
            _PRUNE_STATE["running"] = False
            _PRUNE_STATE["current"] = None

    asyncio.create_task(_work())
    return {"ok": True, "total": len(names)}


async def _prune_status() -> dict:
    # Snapshot both the backward-compatible top-level fields and the per-item
    # state machine. Copies are made so the JSON encoder never observes a dict
    # being mutated by an in-flight worker.
    return {
        "running": _PRUNE_STATE["running"],
        "total": _PRUNE_STATE["total"],
        "done": _PRUNE_STATE["done"],
        "current": _PRUNE_STATE["current"],
        "results": list(_PRUNE_STATE["results"]),
        "items": {
            nm: {"status": st.get("status"), "error": st.get("error")}
            for nm, st in _PRUNE_STATE.get("items", {}).items()
        },
    }


# --- background fleet refresher (started on app startup) ---
_NET_REFRESH_S = 60
_refresher_task: asyncio.Task | None = None
_warm_task: asyncio.Task | None = None
_reaper_task: asyncio.Task | None = None

# Auto-prune reaper (opt-in via dev_fleet.auto_prune.enabled). The poll interval
# is floored so a misconfigured tiny value can't hammer gh/git every cycle.
_AUTO_PRUNE_MIN_INTERVAL_S = 300
_AUTO_PRUNE_DEFAULT_INTERVAL_S = 3600


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


# --- auto-prune reaper (opt-in) ---------------------------------------------
def _auto_prune_cfg() -> tuple[bool, int]:
    """(enabled, interval_secs) from the ``dev_fleet.auto_prune`` config section.

    Disabled by default — auto-prune REMOVES merged worktrees (and stops their
    pods), so it must be an explicit opt-in. The interval is floored at
    ``_AUTO_PRUNE_MIN_INTERVAL_S`` to protect gh/git from a misconfigured tiny
    value, and read fresh each cycle so toggling the flag takes effect without a
    gateway restart.
    """
    section = _load_dev_fleet_cfg().get("auto_prune")
    if not isinstance(section, dict):
        return False, _AUTO_PRUNE_DEFAULT_INTERVAL_S
    # Strict literal-True opt-in: a truthy string like "false" (or any non-empty
    # string / nonzero int) must NEVER arm destructive auto-prune — only a real
    # JSON boolean true does. `bool("false")` is True, so `bool(...)` is unsafe here.
    enabled = section.get("enabled") is True
    raw = section.get("interval_secs", _AUTO_PRUNE_DEFAULT_INTERVAL_S)
    try:
        interval = int(raw)
    except (TypeError, ValueError):
        interval = _AUTO_PRUNE_DEFAULT_INTERVAL_S
    return enabled, max(_AUTO_PRUNE_MIN_INTERVAL_S, interval)


async def _auto_prune_once() -> dict:
    """Remove every MERGED+clean worktree once, reusing the manual-prune path.

    Candidates come from ``_prune_candidates`` but are filtered to
    ``code == "merged"`` (PR MERGED + clean + OID-verified). ``_prune_candidates``
    also surfaces an "empty + stale >48h" class; silently auto-deleting an
    unmerged empty branch (e.g. one created but not yet pushed) on a timer is
    surprising, so that riskier class stays MANUAL-only ("Prune merged"). Each
    kept candidate is removed via ``_worktree_remove(force=False)`` — which stops
    a running pod first (then re-verifies) and applies the squash-safe OID race
    guard. Nothing is force-removed. Best-effort: never raises; returns
    ``{removed, failed}``.
    """
    removed: list[str] = []
    failed: list[dict] = []
    try:
        cand = await _prune_candidates()
    except Exception as exc:  # noqa: BLE001
        logger.exception("dev-fleet auto-prune: candidate scan failed")
        # Surface the scan failure so the reaper still emits a SEL FAILURE event
        # — a failed destructive-op cycle must never be absent from the audit trail.
        return {"removed": removed, "failed": failed, "error": _redact(str(exc))}
    for row in cand.get("candidates", []):
        name = row.get("name")
        # Restrict unattended auto-prune to MERGED worktrees only; the
        # stale-empty class stays manual (see docstring).
        if not name or row.get("code") != "merged":
            continue
        try:
            res = await _worktree_remove(name, force=False)
        except Exception as exc:  # noqa: BLE001
            res = {"ok": False, "error": _redact(str(exc))}
        if res.get("ok"):
            removed.append(name)
        else:
            failed.append({"name": name, "error": res.get("error")})
    return {"removed": removed, "failed": failed, "error": None}


async def _auto_prune_reaper() -> None:
    """Background loop that auto-prunes merged worktrees when enabled.

    Always running but a strict opt-in: each cycle re-reads
    ``dev_fleet.auto_prune`` and does nothing unless ``enabled`` is true, so the
    feature toggles live. A cycle that removes or fails anything is recorded in
    the SEL audit trail (same tamper-evident sink as the manual mutations).
    """
    while True:
        enabled, interval = _auto_prune_cfg()
        if enabled:
            try:
                res = await _auto_prune_once()
                had_error = bool(res["failed"] or res.get("error"))
                if res["removed"] or had_error:
                    _sel().log_tool_invocation(
                        session_key="api", source="api",
                        tool_name="dev_fleet_auto_prune", tool_kind="dev_fleet",
                        outcome="failure" if had_error else "success",
                        resources=_redact(",".join(res["removed"])),
                        error="" if not had_error
                        else _redact(res.get("error") or str(res["failed"]))[:200],
                    )
            except Exception:  # noqa: BLE001
                logger.exception("dev-fleet auto-prune reaper cycle failed")
        await asyncio.sleep(interval)


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
                            t = parsed.get("name") or parsed.get("names") or parsed.get("path")
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
    global _refresher_task, _warm_task, _reaper_task, MAIN_REPO
    loop = asyncio.get_running_loop()
    MAIN_REPO = await loop.run_in_executor(
        subprocess_executor(), _resolve_primary_checkout, MAIN_REPO
    )
    await _load_trusted_credential_helpers()
    await _load_fallback_repos()
    await _upstream_remote()
    # Resolve the node build toolchain here, on the executor, so no request
    # handler ever pays for the filesystem scan (NFS homes make it slow).
    await _warm_build_path()
    if _refresher_task is None or _refresher_task.done():
        _refresher_task = asyncio.create_task(_status_refresher())
    if _reaper_task is None or _reaper_task.done():
        _reaper_task = asyncio.create_task(_auto_prune_reaper())
    _warm_task = asyncio.create_task(_fleet_refresh())


async def dev_fleet_cleanup(app: web.Application) -> None:
    """Cancel and await background tasks so a stopped runner leaves nothing behind."""
    global _refresher_task, _warm_task, _reaper_task
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
    for bg_task in (_refresher_task, _warm_task, _reaper_task):
        if bg_task is not None and not bg_task.done():
            bg_task.cancel()
            try:
                await bg_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
    _refresher_task = None
    _warm_task = None
    _reaper_task = None


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
    # Served at BOTH /health (HMAC-exempt, gateway-internal liveness poll) and
    # /api/health (proxied, reached by the browser at /apps/dev-fleet/api/health).
    # ``start_id`` lets the dashboard's restart handshake wait for the NEW
    # gateway process rather than "a 200 came back" (see _gateway_start_id).
    # None-safe: a platform that cannot report identity returns None here and
    # the frontend degrades to reload-on-first-response instead of hanging.
    return web.json_response({"status": "ok", "start_id": await _gateway_start_id()})


# --- gateway service detection + restart ---
_GATEWAY_SERVICE_ACTIVE: bool | None = None
_GATEWAY_SERVICE_CHECK_AT: float = 0.0
_GATEWAY_SERVICE_TTL = 30.0
_LIVE_GATEWAY_UNIT = "kirocrew-gateway.service"
# The launchd counterpart of the live systemd unit. Same agent
# `kirocrew service install` writes (kiro_crew.service.common.LAUNCHD_LABEL);
# duplicated as a literal rather than imported so this module keeps importing
# cleanly on hosts where the service package's optional deps are unavailable.
_LIVE_GATEWAY_LABEL = "dev.kirocrew.gateway"

# Single-flights the make-live cutover. Two concurrent cutovers would race on
# the shared drop-in (snapshot -> atomic-write -> daemon-reload -> systemd-run
# -> rollback): one request's failure rollback could restore/delete the OTHER
# request's successful override, restarting the gateway into the wrong
# worktree. The mutation sequence in ``_make_live`` runs under this lock; a
# second concurrent request fails fast with ``busy`` rather than queueing (a
# queued cutover could apply a stale target after the winner already restarted
# the gateway out from under us).
_MAKE_LIVE_LOCK = asyncio.Lock()

# Process-local "cutover committed" latch. ``systemd-run --collect ... restart``
# only SCHEDULES the restart and returns immediately, so ``_MAKE_LIVE_LOCK`` is
# released while the restart is still pending. Without this latch a second
# cutover could then acquire the lock and mutate the drop-in for target B while
# target A's already-scheduled restart tears this backend down mid-write —
# leaving the loaded unit and the persisted drop-in disagreeing. Once a cutover
# is successfully scheduled we set this True (BEFORE returning) and refuse every
# further request for the rest of THIS process's life with ``restart_pending``.
# It is deliberately process-local and never persisted: the fresh gateway the
# restart spawns starts with it clear. Failure paths BEFORE successful
# scheduling never set it, and ``dry_run`` never sets it.
_MAKE_LIVE_COMMITTED = False


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


def _gateway_label() -> str:
    """Resolve the launchd label of the gateway THIS backend belongs to.

    The launchd counterpart of :func:`_gateway_unit_name`, with the same pod
    rule for the same reason: inside a pod the owning agent is that pod's own,
    and kickstarting the live agent from a pod plane would bounce the user's
    LIVE gateway. The label shape mirrors ``pod.launchd.pod_label`` — every
    plane carries its ``unit_prefix`` segment, including the default one.
    """
    try:
        from kiro_crew.config.loader import config_dir
        from kiro_crew.pod.config import DEFAULT_UNIT_PREFIX
        from kiro_crew.pod.launchd import LABEL_PREFIX

        home = config_dir()
        if home.parent.name == ".kirocrew-pods":
            prefix = os.environ.get("KIROCREW_POD_UNIT_PREFIX", DEFAULT_UNIT_PREFIX)
            return f"{LABEL_PREFIX}.{prefix}.{home.name}"
    except Exception:  # noqa: BLE001 — fall through to the live agent
        pass
    return _LIVE_GATEWAY_LABEL


def _gateway_backend() -> "gateway_service.GatewayServiceBackend | None":
    """Build the service backend for this host.

    Constructed per call, never cached: ``platform`` and ``which`` are resolved
    HERE, through this module's globals, so the existing tests that drive
    platform detection by patching ``server.sys`` / ``server.shutil`` keep
    controlling it. Caching the instance would freeze the first verdict and
    silently escape those patches.
    """
    return gateway_service.backend(
        _run_cmd,
        unit=_gateway_unit_name,
        label=_gateway_label,
        platform=sys.platform,
        which=shutil.which,
        # Resolved at call time so tests patching these module attributes still
        # control the systemd rendering (see SystemdBackend's docstring).
        dropin_path=_dropin_path,
        dropin_content=_dropin_content,
    )


async def _gateway_service_reason() -> str | None:
    """Human-readable reason the gateway service cannot be driven, or ``None``.

    Reuses the make-live eligibility codes so one probe explains both controls.
    The live-checkout hint is appended for the case that motivated this field:
    on a packaged desktop app the gateway runs from inside the bundle, so even a
    successful restart would not pick up a Pull+Build of the main checkout — and
    the previous UI said nothing at all.
    """
    if await _gateway_service_active():
        return None
    status = await _live_user_unit_status()
    reason = _make_live_status_error(status)
    if status in {"no_agent", "no_user_unit"} and await _live_worktree_path() is None:
        reason += (
            ". The running gateway does not belong to any known worktree, so "
            "restarting it would not apply a Pull+Build of the main checkout"
        )
    return reason


async def _gateway_service_active() -> bool:
    """Cached check: is the gateway running as a service we can drive?

    Async and routed through the sandboxed ``_run_cmd`` chokepoint: a sync
    ``subprocess.run`` here would block the event loop on cache miss AND
    bypass the spawn-audit sandbox invariant.
    """
    global _GATEWAY_SERVICE_ACTIVE, _GATEWAY_SERVICE_CHECK_AT
    now = time.monotonic()
    if _GATEWAY_SERVICE_ACTIVE is not None and (now - _GATEWAY_SERVICE_CHECK_AT) < _GATEWAY_SERVICE_TTL:
        return _GATEWAY_SERVICE_ACTIVE
    svc = _gateway_backend()
    _GATEWAY_SERVICE_ACTIVE = False if svc is None else await svc.active()
    _GATEWAY_SERVICE_CHECK_AT = now
    return _GATEWAY_SERVICE_ACTIVE


async def _gateway_start_id() -> str | None:
    """Start identity of the live gateway, or ``None``. Delegated per platform.

    On systemd this is ``ExecMainStartTimestampMonotonic``; on launchd it is the
    agent's PID (launchd exposes no monotonic start stamp). See
    ``gateway_service`` for each backend's rationale and caveats.

    Reads ``ExecMainStartTimestampMonotonic`` -- the CLOCK_MONOTONIC microsecond
    stamp of the unit's ExecStart *main* PID. Chosen over a wall-clock stamp or
    ``ActiveEnterTimestampMonotonic`` because it is (a) monotonic, so it can
    only increase and never repeats or goes backwards across a restart even if
    the wall clock is stepped by NTP, and (b) tied to the actual main-process
    spawn, so it changes the instant the NEW gateway process starts -- precisely
    the "the new process is up" signal the restart handshake needs (a unit can
    enter ``active`` before its replacement main PID exists).

    Returns ``None`` when no service manager applies, the probe fails, or the
    manager reports no usable identity (systemd prints ``0`` when no main-start
    stamp is recorded; launchd omits the pid line for a loaded-but-not-running
    agent). Callers
    MUST treat ``None`` as "identity unavailable" and degrade to the legacy
    reload-on-first-response behaviour rather than waiting forever in
    "restarting". Uses ``_gateway_unit_name()`` so it matches whichever unit
    ``_restart_gateway`` / ``_make_live`` actually bounce (pod or live).
    """
    svc = _gateway_backend()
    return None if svc is None else await svc.start_id()


async def _restart_gateway() -> dict:
    """Restart the gateway service via a detached systemd-run.

    The restart kills the current process, so we use systemd-run --collect
    to schedule a restart that survives our own death.

    Returns the pre-restart ``start_id`` (the live unit's start identity
    captured BEFORE scheduling) so the caller can poll until a DIFFERENT
    identity appears -- a 200 from this same process still winding down must
    not read as "recovered". ``start_id`` is None-safe (see _gateway_start_id).
    """
    svc = _gateway_backend()
    if svc is None or not await svc.active():
        return {"ok": False, "error": "gateway is not running as a user service"}
    # Capture identity BEFORE scheduling the restart: afterwards the detached
    # bounce can tear this process down at any moment, and the whole point is to
    # hand the frontend the OLD identity to wait past.
    start_id = await _gateway_start_id()
    ok, err = await svc.restart_detached()
    if not ok:
        return {"ok": False, "error": _redact(err)}
    return {"ok": True, "start_id": start_id}


@_audited("dev_fleet_restart_gateway")
async def api_dev_fleet_restart_gateway(request: web.Request) -> web.Response:
    result = await _restart_gateway()
    return web.json_response(result)


# --- make-live: switch the live gateway to another worktree ---
#
# `_restart_gateway` only bounces the live unit in place — the shipped unit
# file hardcodes WorkingDirectory/ExecStart/PATH, so there is no way to point
# the live gateway at a DIFFERENT worktree. Make-live closes that gap with a
# systemd drop-in that OVERRIDES those three fields (the main unit file is
# never edited), then a detached restart applies it.


def _in_pod() -> bool | None:
    """Whether THIS backend runs inside a pod (config home under
    ``.kirocrew-pods/<name>``) — same detection as _gateway_unit_name.

    Returns ``True`` (definitely a pod), ``False`` (definitely not), or
    ``None`` when pod status cannot be resolved (config home unresolvable).

    Cutting the real live gateway from a pod plane is refused: a pod is a
    throwaway test instance and must never repoint the operator's live
    gateway. The ambiguous ``None`` case is fail-CLOSED by the caller
    (``_make_live`` refuses with ``pod_indeterminate``) — an unresolvable
    home must NEVER be treated as "not a pod", which would let a pod cut the
    operator's live gateway."""
    try:
        from kiro_crew.config.loader import config_dir

        return config_dir().parent.name == ".kirocrew-pods"
    except Exception:  # noqa: BLE001
        return None


def _dropin_path() -> Path:
    """Absolute path of the make-live systemd drop-in for the live unit.

    Honours ``$XDG_CONFIG_HOME`` (systemd --user reads units there when set)
    and falls back to ``~/.config`` — a literal ``~/.config`` would be the
    WRONG directory on a host that sets XDG_CONFIG_HOME, and the override
    would silently never take effect."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else (Path.home() / ".config")
    return base / "systemd" / "user" / f"{_LIVE_GATEWAY_UNIT}.d" / "make-live.conf"


#: A path/value cannot be safely serialised into a service definition.
#:
#: Aliased to the adapter's exception so the systemd renderer below and the
#: launchd backend raise ONE type: ``_make_live`` catches a single class, and the
#: existing ``pytest.raises(mod._UnsafeUnitValue)`` assertions keep working.
#:
#: Raised for a value containing a newline, NUL, or any other control character.
#: Such a value would split or truncate the drop-in, and because the broken
#: override is PERSISTED, the failed cutover would then poison every subsequent
#: restart of the live unit (the restart stops the gateway but the malformed unit
#: refuses to start, and recovery restarts hit the same wall).
_UnsafeUnitValue = gateway_service._UnsafeTargetValue


# Control chars (C0 range + DEL) are unrepresentable in a single directive
# value: NUL/newline split or truncate the unit; a tab is ambiguous whitespace.
_SD_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
# A value needs double-quoting only when it carries whitespace or a systemd
# command-line / assignment metacharacter. A plain path is emitted verbatim so
# ordinary worktrees render byte-for-byte identically to before this guard.
_SD_NEEDS_QUOTE_RE = re.compile(r"""[\s"'\\$;`]""")


def _sd_value(raw: str) -> str:
    """Serialise *raw* for a systemd unit directive value.

    All three directives make-live emits — ``WorkingDirectory``, ``ExecStart``
    and ``Environment`` — undergo specifier expansion, so a literal ``%`` is
    doubled to ``%%``. Control characters are rejected outright
    (``_UnsafeUnitValue`` → ``unsafe_path``). Only when *raw* contains
    whitespace or a systemd metacharacter is it wrapped in double quotes (with
    ``\\`` and ``"`` backslash-escaped, per systemd's command-line C-style
    quoting); a clean path is returned unquoted so existing units are
    unchanged."""
    if _SD_CTRL_RE.search(raw):
        raise _UnsafeUnitValue(repr(raw))
    escaped = raw.replace("%", "%%")
    if _SD_NEEDS_QUOTE_RE.search(raw):
        inner = escaped.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{inner}"'
    return escaped


def _dropin_content(worktree: Path, kcbin: Path) -> str:
    """Render the drop-in that repoints the live unit at *worktree*.

    The lone empty ``ExecStart=`` line RESETS the unit's ExecStart before the
    replacement — systemd otherwise APPENDS, and a Type=simple service with
    two ExecStart values is a fatal unit error. ``~`` is NOT expanded inside
    ``Environment=``, so the operator bin dir is materialised to an absolute
    path here (a literal ``~/.local/bin`` would corrupt PATH).

    Every interpolated value passes through ``_sd_value`` so a worktree path
    with spaces, ``%`` specifiers, or quotes is escaped (and a control-char
    path rejected) rather than silently splitting/expanding the directive."""
    venv_bin = worktree / ".venv" / "bin"
    local_bin = Path.home() / ".local" / "bin"
    path_env = ":".join(
        [str(venv_bin), str(local_bin), "/usr/local/bin", "/usr/bin", "/bin"]
    )
    return (
        "[Service]\n"
        f"WorkingDirectory={_sd_value(str(worktree))}\n"
        "ExecStart=\n"
        f"ExecStart={_sd_value(str(kcbin))} gateway --no-open\n"
        f"Environment={_sd_value('PATH=' + path_env)}\n"
    )


def _restore_dropin(dropin: Path, prior: str | None) -> bool:
    """Restore the drop-in to its pre-cutover state after a failed cutover:
    rewrite *prior* content, or delete the file when there was none. Returns
    ``True`` when the on-disk state was restored; best-effort, returning
    ``False`` on any OSError so the caller can report ``rolled_back: false``."""
    try:
        if prior is None:
            dropin.unlink(missing_ok=True)
        else:
            gateway_service.atomic_write_text(dropin, prior)
        return True
    except OSError:
        return False


async def _find_worktree_by_path(path: str) -> tuple[dict | None, str | None]:
    """Resolve a discovered worktree by filesystem path.

    Reuses the same ``git worktree list`` enumeration the fleet listing uses,
    so the caller-supplied path is only ever a SELECTOR validated against the
    server's authoritative set — an arbitrary path can never be made live."""
    if not path:
        return None, "'path' must be a non-empty string"
    try:
        want = Path(path).resolve()
    except (OSError, ValueError, RuntimeError):
        return None, f"invalid path: {path!r}"
    for w in await _discover_worktrees():
        try:
            if Path(w["path"]).resolve() == want:
                return w, None
        except OSError:
            continue
    return None, f"path is not a known worktree: {path!r}"


async def _live_user_unit_status() -> str:
    """Classify the live gateway unit for make-live eligibility.

    make-live writes a ``systemctl --user`` drop-in and restarts the --user
    unit. A ``kirocrew service install`` SYSTEM unit
    (``/etc/systemd/system/kirocrew.service``) is NOT controllable that way:
    the drop-in would be written and the cutover would "succeed" while the
    detached ``--user restart`` bounces nothing (a silent false success).
    Gate on the unit actually being known to the --user manager
    (``systemctl --user cat`` rc==0, same plane ``_restart_gateway`` acts on).

    Returns:
      ``"no_systemd"``   — not Linux / systemctl absent (make-live needs --user systemd);
      ``"no_user_unit"`` — systemctl present but the live unit is not a loaded
                           --user unit (a system-unit install, or not installed);
      ``"user_unit_inactive"`` — the unit is loaded but NOT running, so it is not
                           the process serving this request: restarting it would
                           bounce an idle unit while the real gateway (foreground,
                           or a system unit) keeps serving the old code;
      ``"no_launchd"`` / ``"no_agent"`` / ``"agent_not_indirected"`` /
      ``"agent_restart_contract_outdated"`` — the launchd counterparts (see
                           ``gateway_service``);
      ``"ok"``           — the live service is known to the manager AND running,
                           so a restart actually replaces the gateway we are in.
    """
    svc = _gateway_backend()
    # No manager at all on this host: report the systemd code, which the
    # dashboard already maps, rather than inventing a third "no platform" state.
    if svc is None:
        return "no_systemd"
    status = await svc.status()
    # Loadedness alone is not drivability: a cutover that bounces a unit which is
    # not the running gateway "succeeds" while the old code keeps serving, and the
    # UI would run its restart handshake to a false completion.
    if status == "ok" and not await svc.active():
        return "user_unit_inactive"
    return status


def _staged_notice(name: str, unit_status: str) -> str:
    """Operator-facing message for a cutover that staged but could not restart.

    Leads with the remedy: the actionable command is what the operator needs
    first, and the reason is diagnostic context after it. The reason string
    already carries the "Dev Fleet cannot restart it" clause, so this must not
    restate it.
    """
    return (
        f"{name} is staged as the live target. Run "
        f"`{_manual_restart_command()}` to finish the cutover — the gateway "
        f"will come up on it. It was not automatic because "
        f"{_make_live_status_error(unit_status)}."
    )


def _make_live_status_error(code: str) -> str:
    """Operator-facing message for a non-``ok`` service status.

    Every message names the concrete remedy: an unmanageable service is the one
    make-live failure a user cannot diagnose from the UI alone, and the macOS
    variants are only reachable on a host where the previous behaviour was to
    hide the control entirely.
    """
    return {
        "no_systemd": (
            "the gateway does not run as a systemd --user service, so Dev Fleet "
            "cannot restart it for you"
        ),
        "no_user_unit": (
            f"the live gateway is not running as the user service "
            f"{_LIVE_GATEWAY_UNIT} — Dev Fleet cannot restart it for you (a "
            "`kirocrew service install` system unit needs root to bounce)"
        ),
        "user_unit_inactive": (
            f"the user service {_LIVE_GATEWAY_UNIT} exists but is not running, so "
            "this gateway is not it — restarting that unit would leave the "
            "gateway you are talking to untouched"
        ),
        "no_launchd": (
            "the gateway does not run as a launchd user agent, so Dev Fleet "
            "cannot restart it for you"
        ),
        "no_agent": (
            f"the live gateway is not running as the launchd agent "
            f"{_LIVE_GATEWAY_LABEL} — it was most likely started by the "
            "packaged app or from a terminal, so Dev Fleet cannot restart it "
            "for you"
        ),
        "agent_not_indirected": (
            f"the launchd agent {_LIVE_GATEWAY_LABEL} does not run through the "
            "live-gateway launcher, so Dev Fleet does not treat it as one it "
            "can safely bounce. Re-run `kirocrew service install` to refresh "
            "the agent definition"
        ),
        "agent_restart_contract_outdated": (
            f"the launchd agent {_LIVE_GATEWAY_LABEL} lacks the bounded graceful "
            "restart contract required by Dev Fleet. Re-run "
            "`kirocrew service install` to refresh the agent definition"
        ),
        "live_program_missing": (
            f"the launchd agent {_LIVE_GATEWAY_LABEL} is loaded but its "
            "live-gateway launcher is missing (deleted application-support "
            "directory?), so it has nothing to execute. Make live onto a "
            "worktree to rewrite it, or start a gateway from your source "
            "checkout — either restores the launcher without touching the "
            "agent definition, whereas kirocrew service install would rewrite "
            "the whole plist and discard any environment you added to it"
        ),
    }.get(code, f"the live gateway cannot be repointed ({code})")


def _manual_restart_command() -> str:
    """The command an operator runs to finish a staged cutover themselves.

    Always the service-aware ``kirocrew restart``: it resolves whatever manager
    owns the gateway (or a foreground process) at run time. Naming a specific
    ``systemctl`` invocation here would guess, and guessing wrong hands the
    operator a command that fails while the staged pointer stays unapplied — a
    Linux host with ``systemctl`` present may still be running the gateway from a
    terminal with no unit to bounce.
    """
    return "kirocrew restart"


def _make_live_plan(worktree: Path, kcbin: Path, *,
                    svc: "gateway_service.GatewayServiceBackend | None") -> dict:
    """Describe — without mutating anything — what making *worktree* live does.

    Validates the target the same way the real cutover does, so a dry run
    reports an unusable worktree instead of promising a cutover that would then
    be refused. When the service is drivable the backend's own plan is folded in,
    because the cutover restages that definition too.
    """
    live_target.validate(str(worktree))
    plan: dict = {
        "mechanism": "live-target pointer",
        "pointer_path": str(live_target.pointer_path()),
        "exec": str(kcbin),
        "restart": "automatic" if svc is not None else "manual",
    }
    if svc is None:
        plan["manual_restart"] = _manual_restart_command()
    else:
        plan.update(svc.plan(worktree, kcbin))
    return plan


async def _make_live(path: str, dry_run: bool = False) -> dict:
    """Repoint the live gateway at *path* by staging the live-target pointer.

    Validation order (all enforced for ``dry_run`` too): the path is a known,
    existing worktree (``unknown_path`` / ``missing_path``); NOT inside a pod,
    fail-CLOSED on indeterminate pod status (``pod`` / ``pod_indeterminate``);
    not already live (``already_live``); the worktree has its own executable
    ``.venv/bin/kirocrew`` (else Provision -> ``missing_venv`` when absent,
    ``venv_not_executable`` when present but not +x) and a built SPA
    ``dist/index.html`` (else Pull+Build -> ``missing_dist``).

    A real cutover writes the pointer, then bounces the gateway through the
    service manager when there is one we can drive (DETACHED, so it survives our
    own death — mirroring ``_restart_gateway``). When there is not, the pointer
    still stands and the response carries ``staged_only`` plus the one command
    that finishes it: the gateway reads the pointer on ITS next start, whoever
    performs it. Staging deliberately does NOT require a drivable service, which
    is what keeps a ``kirocrew service install`` host (a SYSTEM unit, needing
    root to bounce) able to cut over at all.
    """
    global _MAKE_LIVE_COMMITTED, _LIVE_WORKTREE, _LIVE_CHECK_AT
    # A cutover already scheduled in THIS process. systemd-run has returned but
    # the restart is still pending, so refuse up-front (before any validation or
    # dry_run plan) — any further mutation would race the pending restart.
    if _MAKE_LIVE_COMMITTED:
        return {"ok": False, "code": "restart_pending", "error": (
            "a cutover has been scheduled; the gateway is restarting — "
            "retry after it comes back"
        )}
    target, err = await _find_worktree_by_path(path)
    if target is None:
        return {"ok": False, "code": "unknown_path", "error": err}
    real = Path(target["path"])
    if not real.exists():
        return {"ok": False, "code": "missing_path",
                "error": f"worktree path no longer exists: {real}"}

    pod = _in_pod()
    if pod is None:
        return {"ok": False, "code": "pod_indeterminate", "error": (
            "cannot determine whether this backend runs inside a pod (config "
            "home unresolvable) — refusing make-live to avoid repointing the "
            "live gateway from an unattributable plane"
        )}
    if pod:
        return {"ok": False, "code": "pod", "error": (
            "refusing make-live from inside a pod — a pod is a throwaway test "
            "instance and must never repoint the real live gateway "
            "(run this from the live dashboard)"
        )}

    # The live target is a POINTER the gateway resolves at startup, not an edit
    # to this host's service definition — so staging never needs the service
    # manager. Restarting still does, and that is the one thing a `kirocrew
    # service install` SYSTEM unit cannot give us without root: the cutover is
    # staged either way, and when we cannot bounce the gateway ourselves we hand
    # the operator the one command that finishes it. Refusing here instead would
    # make the whole feature unreachable on the most common Linux install.
    svc = _gateway_backend()
    unit_status = await _live_user_unit_status()
    can_restart = svc is not None and unit_status == "ok"

    live = await _live_worktree_path()
    same_as_running = live is not None and _same_path(str(real), live)
    if same_as_running and _staged_target() is None:
        # Nothing staged: pointing at the checkout already running is a no-op on
        # EVERY host. This guard sits before the cancel below so that a drivable
        # host cannot turn a harmless repeat click into a real gateway restart by
        # falling through to the cutover path.
        return {"ok": False, "code": "already_live",
                "error": f"{real.name} is already the live gateway"}
    if same_as_running and not can_restart:
        # Pointing at the checkout already running is normally a no-op — EXCEPT
        # while a cutover is staged, where it is the operator cancelling it. The
        # pointer names a different checkout than the running image, so re-pinning
        # the running one is exactly "stay on what is running", and it is the only
        # cancel a non-drivable host can offer: without this the operator's only
        # routes are to complete the cutover into the wrong code and reverse it
        # (two manual restarts) or to hand-delete a keystone-fenced file the
        # product never names.
        #
        # Deliberately limited to hosts this app cannot drive. A drivable host
        # also stages a service DEFINITION naming the staged checkout, and this
        # shortcut only touches the pointer — so the definition would keep naming
        # a checkout nobody intends to run. Once that checkout is pruned the unit
        # fails to start before it ever reads the pointer, turning a recoverable
        # mis-stage into a gateway that will not boot. A drivable host therefore
        # falls through to the full cutover below, which restages the definition
        # and the pointer together and restarts.
        pending_target = _staged_target()
        if pending_target is None:
            # Defensive re-read: the check above and this one straddle no await,
            # but keeping it means the cancel never builds a plan around a stage
            # that has since disappeared.
            return {"ok": False, "code": "already_live",
                    "error": f"{real.name} is already the live gateway"}
        cancel_plan = {
            "action": "cancel_staged_cutover",
            "staged_target": pending_target,
            "keeps_live_target": str(real),
            "pointer_path": str(live_target.pointer_path()),
            "restart": "not needed",
        }
        # Deleting the pointer IS a mutation, so it owes the same two duties as
        # the cutover below: never act under ``dry_run``, and never touch the
        # pointer outside the single-flight lock.
        if dry_run:
            return {"ok": True, "dry_run": True, "plan": cancel_plan}
        if _MAKE_LIVE_LOCK.locked():
            return {"ok": False, "code": "busy", "error": (
                "another make-live cutover is in progress"
            )}
        async with _MAKE_LIVE_LOCK:
            if _MAKE_LIVE_COMMITTED:
                return {"ok": False, "code": "restart_pending", "error": (
                    "a cutover has been scheduled; the gateway is restarting — "
                    "retry after it comes back"
                )}
            # Re-read under the lock: the awaits above mean the stage may have
            # been completed or re-pointed since the entry check, and cancelling
            # a stage that no longer exists would delete a pointer someone else
            # just wrote.
            if _staged_target() is None:
                return {"ok": False, "code": "already_live",
                        "error": f"{real.name} is already the live gateway"}
            # Re-pin the RUNNING checkout rather than deleting the pointer.
            # Deleting only means "stay here" when the running image is the
            # installed build; if this checkout was itself selected by an earlier
            # cutover, the pointer is the only record of that choice, so removing
            # it would silently demote the operator back to the installed build
            # on the next restart — the opposite of the cancel they asked for.
            # Writing is idempotent when the pointer already named it.
            loop = asyncio.get_running_loop()
            try:
                prior_pointer = await loop.run_in_executor(
                    subprocess_executor(), live_target.snapshot
                )
            except (OSError, ValueError) as exc:
                return {"ok": False, "code": "write_failed", "error": (
                    "refusing to cancel the staged cutover: the staged pointer "
                    "exists but could not be read, so a failed cancel could not "
                    f"be rolled back: {_redact(str(exc))}"
                )}
            try:
                await loop.run_in_executor(
                    subprocess_executor(), live_target.write_target, real
                )
            except (live_target.InvalidTarget, OSError) as exc:
                # InvalidTarget refuses before anything is written. OSError can
                # arrive AFTER the pointer has been replaced, because
                # write_target re-applies the owner-only mode as its last step —
                # so a failure there would otherwise leave a code-execution input
                # in place with inherited permissions while this call reported
                # failure. Roll the pointer back so the cancel is all-or-nothing,
                # and only when there was one: restore(None) DELETES, which is
                # the demotion this branch exists to avoid.
                rolled_back = True
                if prior_pointer is not None:
                    rolled_back = await loop.run_in_executor(
                        subprocess_executor(), live_target.restore, prior_pointer
                    )
                detail = "" if rolled_back else (
                    " The rollback also failed, so the pointer may name the "
                    "running checkout without owner-only permissions — check it "
                    "before the next restart."
                )
                return {"ok": False, "code": "write_failed", "error": (
                    "refusing to cancel the staged cutover: the running "
                    "checkout could not be re-pinned as the live target: "
                    f"{_redact(str(exc))}.{detail}"
                )}
            _LIVE_WORKTREE = None
            _LIVE_CHECK_AT = 0.0
            return {"ok": True, "cancelled": True, "target": str(real),
                    "plan": cancel_plan,
                    "notice": (
                        f"Staged cutover cancelled. {real.name} stays the live "
                        f"target and no restart is needed."
                    )}
    if same_as_running:
        # Drivable host with a stage pending. The pointer-only cancel above is
        # unsafe here (it would leave the service definition naming the staged
        # checkout), but falling through to the full cutover would bounce a live
        # gateway carrying real sessions in response to a request that reads as
        # "keep running what is already running". Refuse and name both real
        # exits instead: surprising an operator in the destructive direction is
        # worse than doing nothing.
        pending = _staged_target()
        pending_name = Path(pending).name if pending else "another checkout"
        return {"ok": False, "code": "staged_cutover_pending", "error": (
            f"a cutover to {pending_name} is already staged. Dev Fleet can "
            f"restart this host, so cancelling by re-pointing here would leave "
            f"the service definition naming {pending_name}. Make {pending_name} "
            f"live to complete the cutover, or restart the gateway to apply it."
        )}

    kcbin = real / ".venv" / "bin" / "kirocrew"
    if not kcbin.is_file():
        return {"ok": False, "code": "missing_venv", "error": (
            f"{real.name} has no .venv/bin/kirocrew — Provision it first "
            "(row menu \u2192 Provision) before making it live"
        )}
    # A present-but-non-executable binary is worse than a missing one: the
    # drop-in gets written and the old gateway is stopped, but the replacement
    # can never start (systemd ExecStart requires +x) — leaving NO gateway
    # running. Gate on the exec bit with a DISTINCT, actionable code.
    if not os.access(kcbin, os.X_OK):
        return {"ok": False, "code": "venv_not_executable", "error": (
            f"{real.name} has a non-executable .venv/bin/kirocrew — run "
            "`chmod +x` on it or re-Provision the worktree before making it "
            "live (a non-executable binary stops the live gateway but cannot "
            "start the replacement, leaving no gateway running)"
        )}
    dist_index = real / "src" / "kiro_crew" / "static" / "dist" / "index.html"
    if not dist_index.is_file():
        return {"ok": False, "code": "missing_dist", "error": (
            f"{real.name} has no built dashboard "
            "(src/kiro_crew/static/dist/index.html) — run Pull+Build first; "
            "cutover without a built dist serves a broken dashboard"
        )}

    try:
        plan = _make_live_plan(real, kcbin, svc=svc if can_restart else None)
    except live_target.InvalidTarget as exc:
        return {"ok": False, "code": "unsafe_path", "error": (
            "refusing make-live: the worktree path cannot be used as a live "
            f"target: {_redact(str(exc))}"
        )}
    except gateway_service._UnsafeTargetValue as exc:
        return {"ok": False, "code": "unsafe_path", "error": (
            "refusing make-live: the worktree path is not safely representable "
            "in a service definition (contains control characters): "
            f"{_redact(str(exc))}"
        )}
    plan["target"] = str(real)
    if dry_run:
        return {"ok": True, "dry_run": True, "plan": plan}

    # Serialize the mutation sequence: two concurrent cutovers racing on the
    # shared drop-in (snapshot -> write -> reload -> restart -> rollback) could
    # have one request's rollback restore/delete the OTHER's successful
    # override, restarting into the wrong worktree. Fail fast with ``busy`` on
    # contention rather than queueing — a queued cutover would apply a stale
    # target after the winner already restarted the gateway. The check and the
    # acquire are atomic here (no ``await`` between them on the single-threaded
    # event loop), so the busy response cannot itself race the lock.
    if _MAKE_LIVE_LOCK.locked():
        return {"ok": False, "code": "busy", "error": (
            "another make-live cutover is in progress"
        )}
    async with _MAKE_LIVE_LOCK:
        # Re-check the committed latch now that we hold the lock. A request
        # that passed the entry check just before the WINNING cutover latched
        # (the entry check and the lock acquire are separated by awaits) would
        # otherwise fall through here and mutate the drop-in a second time while
        # the winner's restart is already tearing us down.
        if _MAKE_LIVE_COMMITTED:
            return {"ok": False, "code": "restart_pending", "error": (
                "a cutover has been scheduled; the gateway is restarting — "
                "retry after it comes back"
            )}
        # Snapshot the prior live target BEFORE staging so a failed cutover can
        # be rolled back — a persisted pointer would otherwise silently activate
        # on the NEXT unrelated restart. Staging itself is atomic (temp file +
        # os.replace), so a partial write can never leave a truncated pointer
        # either.
        #
        # An UNREADABLE (as opposed to absent) prior pointer aborts here, before
        # anything is staged: restore interprets None as "there was nothing
        # here" and DELETES the pointer, so continuing would let a failed
        # restart destroy a live target we merely could not read.
        try:
            prior_content = live_target.snapshot()
        except (OSError, ValueError) as exc:
            # ValueError covers an undecodable pointer: it exists, so rollback
            # cannot treat it as absent (that DELETES it), and the cutover is
            # refused rather than made unreversible.
            return {"ok": False, "code": "write_failed", "error": (
                "refusing make-live: the current live target exists but "
                f"could not be read, so a failed cutover could not be rolled "
                f"back: {_redact(str(exc))}"
            )}
        # A drivable service may ALSO carry staging from an earlier cutover whose
        # definition names a worktree directly. Leaving that definition pinned to
        # a stale checkout is a live landmine: once that worktree is pruned the
        # unit's ExecStart binary is gone, the service fails EXEC on its next
        # start, and the pointer is never even read — no gateway comes up. So the
        # definition is restaged alongside the pointer whenever we can drive it,
        # keeping the two in agreement and the ExecStart binary always present.
        prior_definition: str | None = None
        if can_restart:
            assert svc is not None
            try:
                prior_definition = svc.snapshot()
            except OSError as exc:
                return {"ok": False, "code": "write_failed", "error": (
                    "refusing make-live: the current service definition exists "
                    "but could not be read, so a failed cutover could not be "
                    f"rolled back: {_redact(str(exc))}"
                )}

        def _unwind_sync() -> bool:
            """Restore both staged surfaces. False when either did not land."""
            ok = live_target.restore(prior_content)
            if can_restart and svc is not None:
                ok = svc.rollback(prior_definition) and ok
            return ok

        async def _unwind() -> bool:
            # Both halves block: restore() ends in restrict_to_owner, which shells
            # out to icacls on Windows, and svc.rollback() rewrites the service
            # definition. Offload them for the same reason the write below is
            # offloaded — an unwind must not stall every other gateway request for
            # the duration of a subprocess.
            return await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(), _unwind_sync
            )

        try:
            # write_target ends in restrict_to_owner, which shells out to icacls
            # on Windows. Run it off the loop so a cutover cannot stall every
            # other gateway request for the duration of that subprocess.
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                subprocess_executor(), live_target.write_target, real
            )
        except live_target.InvalidTarget as exc:
            return {"ok": False, "code": "unsafe_path", "error": _redact(str(exc))}
        except OSError as exc:
            return {"ok": False, "code": "write_failed",
                    "rolled_back": await _unwind(),
                    "error": _redact(str(exc))}

        # Nothing bounces the gateway on this host, so the cutover is STAGED and
        # the operator finishes it. Reported as a success with the exact command,
        # not a failure: the pointer is written and correct, and the next start
        # of the gateway — however it happens — comes up on the new target.
        # Deliberately NOT latched as committed: no restart is pending, so a
        # subsequent cutover to a different worktree must stay allowed.
        if not can_restart:
            _LIVE_WORKTREE = None
            _LIVE_CHECK_AT = 0.0
            return {"ok": True, "cutover": True, "staged_only": True,
                    "target": str(real), "plan": plan,
                    "manual_restart": _manual_restart_command(),
                    "notice": _staged_notice(real.name, unit_status)}
        assert svc is not None  # can_restart implies a backend

        staged, code, err = await svc.stage(real, kcbin)
        if not staged:
            rolled_back = await _unwind()
            # Re-read definitions so the loaded config matches the restored disk
            # state rather than the rejected override.
            await svc.reload()
            return {"ok": False, "code": code, "rolled_back": rolled_back,
                    "error": _redact(err)}

        # The restart tears down THIS backend with the gateway, so it is handed
        # to the service manager to perform (systemd-run on Linux, launchd's
        # stop/relaunch transaction on macOS) so it survives our own death. Capture the
        # pre-restart identity FIRST so the dashboard reuses the same handshake
        # it uses for restart-gateway (wait for a DIFFERENT start id, not "a 200
        # came back") -- a cutover is just a restart into different code, so it
        # has the identical early-200 hazard. None-safe (see _gateway_start_id).
        start_id = await _gateway_start_id()
        restarted, err = await svc.restart_detached()
        if not restarted:
            rolled_back = await _unwind()
            await svc.reload()
            return {"ok": False, "code": "restart_failed", "rolled_back": rolled_back,
                    "error": _redact(err)}

        # COMMITTED: the restart is scheduled (the call returns before it
        # lands). Latch process-locally BEFORE returning so no further cutover
        # can mutate the live target while the restart is pending — the fresh
        # process the restart spawns starts with this clear.
        _MAKE_LIVE_COMMITTED = True

        # Invalidate the live-worktree cache so the next fleet poll re-resolves
        # the live checkout.
        _LIVE_WORKTREE = None
        _LIVE_CHECK_AT = 0.0

        return {"ok": True, "cutover": True, "target": str(real),
                "plan": plan, "start_id": start_id}


@_audited("dev_fleet_make_live")
async def api_dev_fleet_make_live(request: web.Request) -> web.Response:
    body, err = await _json_body(request)
    if err is not None:
        return err
    assert body is not None
    path = body.get("path")
    if not isinstance(path, str) or not path:
        return web.json_response(
            {"error": "'path' must be a non-empty string"}, status=400
        )
    dry_run = body.get("dry_run")
    if dry_run is not None and not isinstance(dry_run, bool):
        return web.json_response({"error": "dry_run must be a boolean"}, status=400)
    return web.json_response(await _make_live(path, dry_run is True))


# =============================================================================
# Application factory and main
# =============================================================================

def create_app() -> web.Application:
    """Build the aiohttp Application with all routes and lifecycle hooks."""
    app = web.Application(middlewares=[hmac_proxy_middleware])
    app.router.add_get("/health", api_health)
    # The dashboard reaches this backend ONLY through the gateway proxy, which
    # matches /apps/dev-fleet/api/{path} and forwards to /api/{path}
    # (handle_app_api_proxy). The bare /health above is reachable only by the
    # gateway's own in-process liveness poll (127.0.0.1:<port>/health, and it is
    # the one path the HMAC middleware exempts). So the restart-identity
    # handshake MUST poll a PROXIED path -- expose the same handler
    # under /api/health, which the browser reaches at /apps/dev-fleet/api/health.
    app.router.add_get("/api/health", api_health)
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
    app.router.add_post("/api/make-live", api_dev_fleet_make_live)
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
