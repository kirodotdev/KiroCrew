"""Shared environment helpers for subprocess spawning."""

from __future__ import annotations

import functools
import getpass
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import MutableMapping
from pathlib import Path

from kiro_crew import platform_compat

logger = logging.getLogger(__name__)

# Common directories where MCP server binaries may be installed.
# Order matters — earlier entries take precedence.
_EXTRA_PATH_DIRS = (
    "{home}/.local/bin",
    "{home}/.toolbox/bin",
    "{home}/.npm-packages/bin",
    "{home}/.local/share/mise/shims",
    "{home}/.volta/bin",
    "/opt/homebrew/bin",  # Apple Silicon Homebrew node / global npm bins
)


@functools.lru_cache(maxsize=1)
def _node_version_manager_bins(home: str) -> list[str]:
    """Return node bin dirs from version managers with dynamic version paths.

    nvm and fnm install each Node version under a versioned directory, so the
    bin path cannot be a static template in ``_EXTRA_PATH_DIRS``.  Glob the
    install roots and return every ``bin`` dir, newest version first.  A
    non-login gateway (launchd / systemd) does not inherit these on ``$PATH``,
    so adding them lets us find globally-installed MCP binaries such as
    ``claude-agent-acp`` that were installed via ``npm i -g`` under nvm/fnm.

    Cached for the process lifetime (``lru_cache(maxsize=1)``, ``home`` is
    constant per process): the filesystem glob is the surface a prior GIL-storm
    wedge got caught in, so it must run exactly once.  Trade-off: a node version
    installed via nvm/fnm *while the long-lived gateway is running* is not
    visible until the gateway restarts.  Acceptable — installing node mid-session
    is rare, and a restart picks it up.  Call ``cache_clear()`` if that ever
    needs to be re-discovered without a restart.
    """
    bins: list[str] = []
    roots = (
        Path(home) / ".nvm" / "versions" / "node",
        Path(home) / ".fnm" / "node-versions",
    )
    for root in roots:
        if not root.is_dir():
            continue
        for ver_dir in sorted(root.glob("*"), reverse=True):
            bin_dir = ver_dir / "bin"
            if bin_dir.is_dir():
                bins.append(str(bin_dir))
    return bins


@functools.lru_cache(maxsize=1)
def is_toolbox_install() -> bool:
    """Return True if the running kirocrew binary was installed via Toolbox."""
    exe = Path(sys.executable).resolve()
    toolbox_dir = (Path.home() / ".toolbox").resolve()
    try:
        exe.relative_to(toolbox_dir)
        return True
    except ValueError:
        return False


@functools.lru_cache(maxsize=1)
def git_build_info() -> tuple[str, str]:
    """Return ``(branch, short_commit)`` for the running source checkout.

    Reads ``KIROCREW_PROJECT_DIR`` (the git tree the gateway runs from) and
    shells out to ``git`` once. The result is cached for the process lifetime
    (``lru_cache(maxsize=1)``): the running build's branch and commit cannot
    change without a restart, and status snapshots are emitted on every SSE /
    WebSocket tick, so this must not spawn ``git`` on the hot path repeatedly.

    Returns ``("", "")`` when there is no source tree to inspect — toolbox /
    pip-wheel installs (no ``KIROCREW_PROJECT_DIR`` or no ``.git``) — so callers
    can omit the fields gracefully. Any git failure also fails open to empty
    strings.
    """
    proj = os.environ.get("KIROCREW_PROJECT_DIR", "")
    if not proj or not (Path(proj) / ".git").exists():
        return ("", "")

    def _run(*args: str) -> str:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=proj,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        if result.returncode != 0:
            return ""
        return result.stdout.strip()

    return (
        _run("rev-parse", "--abbrev-ref", "HEAD"),
        _run("rev-parse", "--short", "HEAD"),
    )


def augmented_path(base_path: str = "") -> str:
    """Return *base_path* prepended with well-known MCP binary directories.

    When KiroCrew runs under systemd or another non-login shell the
    inherited ``$PATH`` rarely includes directories like
    ``~/.local/bin``.  Both the MCP-probe code and the kiro-cli
    spawn code need the same augmentation — this helper keeps them in
    sync.

    On Windows a launched (non-shell) gateway inherits a ``PATH`` that does
    not include the venv's ``Scripts\\`` directory, so ``shutil.which`` fails
    to resolve the ``kirocrew`` / ``kirocrew-core`` console-script wrappers
    pip generated for MCP-server spawn. Append ``sys.executable``'s parent
    directory as the LAST entry so the running interpreter's own
    console-scripts (``Scripts\\`` on Windows, ``bin/`` on POSIX) are always
    discoverable. Last, not first: the interpreter dir also contains
    ``python``/``pip``, and placing it ahead of ``base_path`` would silently
    rebind a user MCP spec's bare ``"command": "python"`` (and the spawned
    agent's own ``python``/``pip`` shell calls) to the gateway's venv
    interpreter. As a pure fallback it resolves only names found nowhere
    else — exactly the console-script-wrapper case.
    """
    home = os.path.expanduser("~")
    extra = [d.format(home=home) for d in _EXTRA_PATH_DIRS]
    extra += _node_version_manager_bins(home)
    parts = extra + ([base_path] if base_path else [])
    parts.append(str(Path(sys.executable).parent))
    return os.pathsep.join(parts)


@functools.lru_cache(maxsize=1)
def _mise_bin() -> str | None:
    """Locate the ``mise`` binary in a non-login (daemon) context.

    A systemd / launchd gateway does not source the user's shell rc, so
    ``~/.local/bin`` (mise's default install dir) is often absent from the
    inherited ``$PATH``.  Try ``$PATH`` first, then fall back to the canonical
    install location before giving up.
    """
    found = shutil.which("mise")
    if found:
        return found
    candidate = Path.home() / ".local" / "bin" / "mise"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def activate_mise(env: MutableMapping[str, str] | None = None) -> list[str]:
    """Merge mise's resolved environment into *env* (defaults to ``os.environ``).

    Run once at gateway start so every subprocess the gateway later spawns —
    MCP servers, script crons, kiro-cli — inherits the user's mise-managed
    toolchain (Node, Python, kubectl, …) exactly as an interactive shell
    would.  This prevents the most common MCP failure mode: a Node-based MCP
    server spawned against the system ``/usr/bin/node`` (v18 on AL2) instead of
    the user's mise ``node@20+``, which exits during ``initialize`` with a
    stderr-only "Node version 18 detected, but version 20 or higher is
    required" error and surfaces only as "MCP server disconnected during
    'initialize' call".

    Best-effort and non-fatal: a no-op (returns ``[]``) when mise is not
    installed, when disabled via ``KIROCREW_NO_MISE``, or when invoking /
    parsing mise fails — the gateway always starts regardless.  Returns the
    sorted list of env var names that were added or changed, for logging.

    ``mise env --json`` returns only the variables mise manages (PATH plus any
    ``[env]`` / tool-provided vars), not the whole environment, so the merge is
    bounded.  We pass the current env in and resolve from ``$HOME`` so the
    user's *global* mise config is used (not whatever ``.mise.toml`` happens to
    sit in the daemon's cwd), and ``--json`` avoids fragile ``export NAME=VALUE``
    shell-quoting parsing.
    """
    target = os.environ if env is None else env
    if target.get("KIROCREW_NO_MISE"):
        logger.debug("mise activation skipped: KIROCREW_NO_MISE set")
        return []
    mise = _mise_bin()
    if not mise:
        return []
    try:
        proc = subprocess.run(
            [mise, "env", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
            env=dict(target),
            cwd=str(Path.home()),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("mise activation skipped: %s", type(exc).__name__)
        return []
    if proc.returncode != 0:
        logger.debug(
            "mise env --json exited %s: %s",
            proc.returncode,
            proc.stderr.strip()[:200],
        )
        return []
    try:
        resolved = json.loads(proc.stdout)
    except ValueError as exc:
        logger.debug("mise env --json unparsable: %s", type(exc).__name__)
        return []
    if not isinstance(resolved, dict):
        return []
    changed: list[str] = []
    for key, value in resolved.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        if target.get(key) != value:
            target[key] = value
            changed.append(key)
    return sorted(changed)


def resolve_krb5_ccname(env: dict[str, str]) -> None:
    """Point *env* at a FILE: Kerberos ccache, mutating it in place.

    The gateway is a long-lived, non-login process.  On AL2023 the default
    ``krb5.conf`` uses ``KEYRING:persistent:<uid>`` for the ccache, and kernel
    keyrings are session-scoped — they are NOT visible to subprocesses spawned
    by a background daemon.  So a child (kiro-cli / claude / a pooled MCP
    backend) inheriting ``os.environ`` sees no usable ticket, and Kerberos-gated
    MCP servers (e.g. an SSO-backed MCP server) fail with
    "no Kerberos ticket" even though ``kinit`` succeeded in the user's shell.

    This mirrors :func:`_resolve_ssh_auth_sock` in ``acp.client``: repair the
    credential pointer at spawn time rather than trusting the daemon's stale
    env.  Resolution rules:

    * If ``KRB5CCNAME`` already names a non-default scheme (``FILE:`` operator
      override, or a platform-native ``KCM:`` / ``DIR:`` / ``API:`` cache),
      leave it — the caller already has a working, non-keyring ccache.
    * Only act on Linux: the ``/tmp/krb5cc_<uid>`` workaround targets the
      AL2023 ``KEYRING:persistent`` default.  On macOS the default is the
      ``KCM:`` daemon, so blindly pointing at a stale ``/tmp`` file (e.g. left
      by a prior Linux session or container mount) would hijack a working
      ccache — gate the whole thing on ``sys.platform == "linux"``.
    * Else, if ``/tmp/krb5cc_<uid>`` resolves to a regular file we own, point
      at it.
    * Else, do nothing — no ticket to find; let the MCP surface its own
      auth error rather than masking it.

    The candidate lives in ``/tmp`` (world-writable, sticky-bit), so we ``lstat``
    it first and require ownership by the current uid.  We do NOT reject a
    uid-owned symlink: sssd-krb5 / systemd-pam-krb5 legitimately ship
    ``/tmp/krb5cc_<uid>`` as a symlink into ``/run/user/<uid>/krb5cc/...`` — the
    exact keyring-default distros this fix targets.  For a uid-owned symlink we
    follow it (``os.stat``) and require the *resolved* target to be a regular
    file owned by the current uid.  A symlink or file owned by anyone else is
    rejected, which preserves the co-tenant defense (a foreign user cannot plant
    ``/tmp/krb5cc_<victim_uid>`` and have us trust it).

    ``KRB5CCNAME`` is intentionally absent from the MCP-gateway scrub list
    (``mcp_gateway.manager._SENSITIVE_ENV_PREFIXES``), so a value set here
    propagates to pooled backends as well.
    """
    current = env.get("KRB5CCNAME", "")
    # FILE: = explicit operator override; KCM:/DIR:/API: = platform-native
    # schemes (KCM: is the macOS default). Any of these is already a working,
    # subprocess-visible ccache — never override it.
    if current.startswith(("FILE:", "KCM:", "DIR:", "API:")):
        return
    # The /tmp/krb5cc_<uid> workaround only applies to the Linux kernel-keyring
    # default. On macOS/other platforms the keyring-isolation problem does not
    # exist and a stray /tmp file must not hijack the native ccache. Routing
    # through ``platform_compat`` (rather than a raw ``sys.platform`` compare)
    # keeps this consistent with the rest of the codebase's POSIX/Linux gates
    # and gives Windows the same no-op behaviour it needs (no ``os.getuid``).
    if not platform_compat.IS_LINUX:
        return
    # The kernel's default FILE ccache is named by numeric UID
    # (``/tmp/krb5cc_<uid>``) — this is also what the documented workaround
    # ``kinit -c /tmp/krb5cc_$(id -u)`` produces.  Some setups instead use the
    # login name, so check that as a fallback.  ``getpass.getuser()`` is only
    # evaluated for the fallback path.
    candidates = [f"/tmp/krb5cc_{os.getuid()}"]
    try:
        candidates.append(f"/tmp/krb5cc_{getpass.getuser()}")
    except Exception as exc:  # getuser() can raise without a passwd entry / env
        logger.debug("krb5 ccache username fallback skipped: %s", type(exc).__name__)
    rejected: list[str] = []
    for cache in candidates:
        reason = _reject_reason(cache)
        if reason is None:
            env["KRB5CCNAME"] = f"FILE:{cache}"
            logger.debug("resolved KRB5CCNAME to FILE:%s", cache)
            return
        if reason != "absent":
            # A candidate physically exists but failed the ownership/type gate.
            # Log it so this is distinguishable from the plain "no ccache" case —
            # otherwise it reproduces the silent-failure gap this resolver fixes.
            rejected.append(f"{cache} ({reason})")
    if rejected:
        logger.debug("KRB5CCNAME left unset; rejected ccache candidate(s): %s", ", ".join(rejected))


def _reject_reason(cache: str) -> str | None:
    """Return ``None`` if *cache* is a usable FILE ccache, else a rejection reason.

    Accepts a regular file owned by us, or a uid-owned symlink whose resolved
    target is a regular file owned by us (sssd/systemd ship the ccache as a
    symlink into ``/run/user/<uid>/krb5cc/...``).  Rejects anything owned by
    another uid — a co-tenant on a shared ``/tmp`` cannot make us trust a
    planted file or symlink.

    Reasons are coarse, log-only labels (``absent`` means the path does not
    exist, i.e. the ordinary no-op case — callers skip logging it).
    """
    uid = os.getuid()
    try:
        st = os.lstat(cache)  # lstat: inspect the link itself, do not follow yet
    except OSError:
        return "absent"
    if stat.S_ISLNK(st.st_mode):
        # A foreign-owned symlink is an attack vector; a uid-owned one may
        # legitimately point at /run/user/<uid>/krb5cc/... — follow and validate.
        if st.st_uid != uid:
            return "foreign-owned-symlink"
        try:
            st = os.stat(cache)  # resolves the symlink to its target
        except OSError:
            return "dangling-symlink"
        if not stat.S_ISREG(st.st_mode):
            return "symlink-target-not-regular"
        if st.st_uid != uid:
            return "symlink-target-foreign-owned"
        return None
    if not stat.S_ISREG(st.st_mode):
        return "not-regular"
    if st.st_uid != uid:
        return "foreign-owned"
    return None
