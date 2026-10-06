"""Detection, installation, and the capability gate for ``@playwright/cli``.

The CLI has no capability gating of its own — every command is available to
whoever can run the binary — so :func:`available` reports host capability only.
It is not an approval decision: dashboard shell turns still follow the ordinary
approval ladder unless the user has granted trust or enabled auto-approve.

Installation is global within a product-owned npm prefix rather than ``npx``.
``npx`` re-resolves the package through the registry on every invocation, so an
expired registry token would take browsing down at use time. The managed prefix
is ``<data-home>/playwright-cli``. Every agent sandbox exposes that leaf
read-only, and gateway execution resolves it by absolute path before considering
fixed, non-writable system locations. ``PATH`` is never an execution source.

Node is located through :func:`kiro_crew.env.find_node_tool` rather than bare
``shutil.which``: the gateway can run with a PATH that omits the version-manager
shim directory a managed npm install needs at execution time.

Every function here blocks (subprocess, filesystem), so a caller on the event
loop offloads it.
"""

from __future__ import annotations

import contextlib
import enum
import json
import logging
import os
import platform
import re
import shutil
import stat
import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any

from kiro_crew import github_runner, platform_compat
from kiro_crew.browser_cli import os_deps
from kiro_crew.config.paths import config_dir
from kiro_crew.env import augmented_path, find_node_tool, node_augmented_path
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

# The CLI's own floor. Node 19 and older lack APIs its bundle uses, so a lower
# version does not fail at install — it fails at first browse with an opaque
# stack, which is why detection rejects it up front rather than letting install
# "succeed" into a broken state.
MIN_NODE_MAJOR = 20

CLI_BIN = "playwright-cli"
# Verified on September 19, 2026, with ``@playwright/cli@0.1.21``. These launches:
# ``npx --yes @playwright/cli@0.1.21 show --port 45613 --host 127.0.0.1``
# ``npx --yes @playwright/cli@0.1.21 show --port 0 --host 127.0.0.1``
# printed ``Listening on http://127.0.0.1:45613`` and
# ``Listening on http://127.0.0.1:42963``, respectively. Each line was read only
# after its listener had bound. ``@latest`` may change that wording; the parser
# then fails closed and reports the installed version for diagnosis, and the
# daily ``playwright-cli-banner.yml`` workflow catches the drift first.
NPM_SPEC = "@playwright/cli@latest"

# ``install --skills`` writes the command reference where an agent can read it.
# ``agents`` is the agent-neutral target (the default, ``claude``, writes a
# Claude-specific layout) and ``--global`` puts it in the home directory so it
# is found from any working directory rather than only inside one workspace.
_SKILLS_TARGET = "agents"

# Browser binaries live in Playwright's own cache, keyed by platform, and
# ``PLAYWRIGHT_BROWSERS_PATH`` overrides it. Probing this directory keeps
# ``detect()`` free of a subprocess that would launch a browser to answer.
_BROWSERS_CACHE_ENV = "PLAYWRIGHT_BROWSERS_PATH"

# A version probe answers immediately or something is wrong; an install talks to
# the npm registry and then downloads a browser, so its budget is minutes.
_PROBE_TIMEOUT_S = 20.0
_NPM_INSTALL_TIMEOUT_S = 900.0
_BROWSER_INSTALL_TIMEOUT_S = 1800.0
_SKILLS_INSTALL_TIMEOUT_S = 180.0
_CLI_PACKAGE_JSON_MAX_BYTES = 1024 * 1024

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def cli_env() -> dict[str, str]:
    """Environment for a CLI/npm child, with the Node bin directories on PATH.

    The gateway's own PATH is not sufficient: a global ``npm install -g`` lands
    in a version-manager-owned bin directory that the gateway process may never
    have had on PATH, so a child that inherits it unchanged cannot find the
    binary that was just installed.

    Two layers, node bins outermost so they win:

    1. :func:`node_augmented_path` prepends the Node bin dirs, so ``npm``/``node``
       resolve to the managed toolchain.
    2. :func:`augmented_path` (the inner layer) contributes the broad non-login
       PATH -- ``~/.local/bin``, ``/opt/homebrew/bin``, the mise shims -- because
       the mise-managed per-version ``npm`` is itself a wrapper script that runs
       ``mise reshim`` after a global install. That hook needs the ``mise``
       binary itself on PATH, and mise installs to ``~/.local/bin`` (or
       Homebrew's bin), NOT to any Node bin dir. A GUI- or daemon-launched
       gateway inherits a minimal PATH lacking those dirs, so without this layer
       the wrapper dies ``mise: command not found`` and, under its
       ``set -euo pipefail``, fails the whole ``npm install -g`` with rc 127.

    This PATH is execution support only. :func:`cli_path` does not consume it:
    gateway execution resolves the sandbox-sealed managed entrypoint or a fixed,
    non-writable system candidate by absolute path.
    """
    env = dict(os.environ)
    env["PATH"] = node_augmented_path(augmented_path(env.get("PATH", "")))
    return env


#: Return code of a step refused or cut short because its :class:`InstallScope`
#: was terminated. 130 is the shell's "ended by interrupt" code, distinct from the
#: 124 timeout and 127 missing-executable codes :func:`_run` already reports.
INTERRUPTED_RC = 130
_INTERRUPTED_REASON = "interrupted: the gateway stopped this installer"
#: Return code :func:`_run` reports when the child outlives its timeout. Shared
#: with :func:`_step` so a timed-out step is not dressed as an ordinary failure.
TIMEOUT_RC = 124

#: How long a killed child gets to close its pipes before :func:`_run` stops
#: waiting for its output. The kill has already been sent; this bounds only the
#: collection of whatever it printed.
_REAP_TIMEOUT_S = 5.0


class InstallScope:
    """The installer children one install job owns, so the gateway can stop them.

    ``asyncio.to_thread`` cannot cancel its worker, and cancelling the awaiting
    task leaves the worker's subprocess running. The job's worker therefore runs
    inside a scope (:func:`run_in_scope`); every :func:`_run` call made on that
    thread registers its child here, and :meth:`terminate` kills each child's
    whole process group or tree. After termination the scope refuses to spawn,
    so the worker's remaining steps end at once instead of starting the next
    download.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._children: set[subprocess.Popen[str]] = set()
        self._terminated = False

    @property
    def terminated(self) -> bool:
        with self._lock:
            return self._terminated

    def _adopt(self, proc: subprocess.Popen[str]) -> bool:
        """Track *proc*; ``False`` when the scope was terminated first."""
        with self._lock:
            if self._terminated:
                return False
            self._children.add(proc)
            return True

    def _release(self, proc: subprocess.Popen[str]) -> None:
        with self._lock:
            self._children.discard(proc)

    def terminate(self) -> int:
        """Kill every live child and refuse new ones. Returns how many were signalled.

        Safe from any thread and idempotent. The kill is synchronous and does not
        wait for exit: the worker thread's own ``communicate`` reaps the child.
        """
        with self._lock:
            self._terminated = True
            children = list(self._children)
        for proc in children:
            platform_compat.kill_popen_tree(proc)
        return len(children)


_scope_local = threading.local()


def _current_scope() -> InstallScope | None:
    scope = getattr(_scope_local, "scope", None)
    return scope if isinstance(scope, InstallScope) else None


def run_in_scope(scope: InstallScope, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Call *fn* with every :func:`_run` child on this thread owned by *scope*.

    Meant as the target of ``asyncio.to_thread``: the scope is thread-local, so
    concurrent status probes on other executor threads are never adopted.
    """
    previous = getattr(_scope_local, "scope", None)
    _scope_local.scope = scope
    try:
        return fn(*args, **kwargs)
    finally:
        _scope_local.scope = previous


def kill_cli_process_tree(pid: int) -> None:
    """Signal *pid* and every descendant; never raises.

    The one pid-addressed tree kill ``browser_cli`` issues, for
    :mod:`kiro_crew.browser_cli.view`'s reaper, which holds a pid rather than a
    ``Popen``. This installer's timeout and cancel path holds the ``Popen`` and
    uses :func:`platform_compat.kill_popen_tree` instead. So the package adds a
    single site to the kill-attribution ratchet
    (``test_kill_chokepoint_ratchet.py``) rather than one per caller.
    """
    with contextlib.suppress(Exception):
        platform_compat.kill_process_tree(pid)


def _collect_after_kill(proc: subprocess.Popen[str]) -> tuple[str, str]:
    """Output a killed child printed, bounded by :data:`_REAP_TIMEOUT_S`."""
    try:
        out, err = proc.communicate(timeout=_REAP_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return "", ""
    return out or "", err or ""


def _run(argv: list[str], timeout: float, *, cwd: str | None = None) -> tuple[int, str, str]:
    """Run *argv*, returning ``(returncode, stdout, stderr)``.

    A timeout or a missing executable is reported as a non-zero return code with
    the reason on stderr, so callers branch on one shape instead of catching
    three exception types at every call site. *cwd* is the child's working
    directory; ``None`` inherits the gateway's, which is right for a PATH probe
    and wrong for the staged-copy smoke run (see :func:`_staged_node_runs`).

    The child gets its own process group, and a timeout kills that whole group
    rather than only the direct child, so an expired ``npm install`` cannot leave
    its download running: ``npm`` and the browser installer each start
    grandchildren. POSIX uses a new session (``setsid`` in the C fork path);
    Windows uses a new process group that
    :func:`platform_compat.kill_process_tree` walks. Inside an
    :class:`InstallScope` the child is also registered for gateway shutdown,
    and a terminated scope answers :data:`INTERRUPTED_RC` without spawning.
    """
    scope = _current_scope()
    if scope is not None and scope.terminated:
        return INTERRUPTED_RC, "", _INTERRUPTED_REASON
    try:
        # npm and playwright-cli write in the host's locale.
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,  # subprocess-encoding: locale
            env=cli_env(),
            cwd=cwd,
            start_new_session=platform_compat.IS_POSIX,
            creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
        )
    except OSError as exc:
        return 127, "", f"{exc}"
    if scope is not None and not scope._adopt(proc):
        platform_compat.kill_popen_tree(proc)
        _collect_after_kill(proc)
        return INTERRUPTED_RC, "", _INTERRUPTED_REASON
    try:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            platform_compat.kill_popen_tree(proc)
            _collect_after_kill(proc)
            return TIMEOUT_RC, "", f"timed out after {timeout:.0f}s: {' '.join(argv)}"
    finally:
        if scope is not None:
            scope._release(proc)
    if scope is not None and scope.terminated:
        return INTERRUPTED_RC, out or "", _INTERRUPTED_REASON
    return proc.returncode, out or "", err or ""


#: The whole npm prefix and entry point live under one crew-home leaf. The OS
#: sandbox exposes this directory read-only to agent descendants, while the
#: unsandboxed gateway can install or update it.
_MANAGED_TOOLS_LEAF = Path(CLI_BIN)


def _managed_cli_root() -> Path:
    """Gateway-owned npm prefix sealed read-only inside every agent sandbox."""
    return config_dir() / _MANAGED_TOOLS_LEAF


@contextlib.contextmanager
def _pinned_managed_cli_root() -> Iterator[Path]:
    """Yield the real managed prefix pinned against links before npm writes.

    ``mkdir(exist_ok=True)`` is allowed to encounter an existing link, but no
    external write follows it: :func:`platform_compat.pin_directory` opens the
    leaf without following symlinks or Windows reparse points. On Windows the
    held handle also blocks rename/delete for the duration of npm's path-based
    write. The descriptor identity check makes a create/open race fail closed.
    """
    root = _managed_cli_root()
    try:
        root.mkdir(mode=stat.S_IRWXU)
    except FileExistsError:
        # Do not call is_dir() here: on Windows it follows a reparse point and
        # can authenticate to an attacker-named UNC target. pin_directory is
        # deliberately the first operation that judges the existing name.
        pass
    try:
        fd = platform_compat.pin_directory(root)
    except OSError as exc:
        raise OSError(f"managed CLI root is not a stable real directory: {root}") from exc
    try:
        named = os.stat(root, follow_symlinks=False)
        opened = os.fstat(fd)
        if (
            not stat.S_ISDIR(named.st_mode)
            or not stat.S_ISDIR(opened.st_mode)
            or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise OSError(f"managed CLI root is not a stable real directory: {root}")
        yield root
    finally:
        os.close(fd)


def _managed_node_path() -> Path:
    """Node executable the gateway owns inside the sealed managed leaf."""
    name = "node.exe" if platform_compat.IS_WINDOWS else "gateway-node"
    return _managed_cli_root() / name


def _staged_node_runs(candidate: Path) -> str | None:
    """Reason the staged Node copy cannot run standalone, or ``None`` if it can.

    Runs the copy exactly as the gateway will run it -- by absolute path, from
    the managed leaf, under :func:`cli_env` -- and asks for its version. One
    probe covers every way a copy can be dead: a launcher script whose relative
    targets are not in the leaf, a binary that only links under a wrapper's
    ``LD_LIBRARY_PATH``, a build for another architecture. The reason names the
    failure so ``stage-node`` reports it instead of a later call.

    "From the managed leaf" is literal: the leaf is the child's working
    directory. The probe must not inherit the gateway's, which is whatever the
    service manager or a test runner started it in.
    """
    code, out, err = _run(
        [str(candidate), "--version"], _PROBE_TIMEOUT_S, cwd=str(candidate.parent)
    )
    if code != 0:
        detail = (err or out).strip().splitlines()
        return f"exit {code}" + (f": {detail[-1]}" if detail else "")
    if _first_version(out) is None:
        return f"did not report a version (stdout: {out.strip()[:80]!r})"
    return None


def _stage_managed_node(source: str) -> Path:
    """Copy the installer-selected Node into the managed leaf atomically.

    Every gateway-owned CLI call invokes this copy with the package's JavaScript
    entrypoint directly. That keeps both executable choices inside the read-only
    leaf instead of letting ``#!/usr/bin/env node`` or a generated wrapper select
    a version-manager binary from the gateway's broad PATH.

    The copy is run before it becomes ``gateway-node``. Being a regular,
    executable file is not enough: a launcher script locates the binary it
    starts relative to its own path, and a binary from a wrapped distribution
    may link only under the wrapper's ``LD_LIBRARY_PATH``. Either copies
    cleanly, passes the mode checks, and then fails on the first CLI call with
    an error naming a path under the leaf, while the status probe, which checks
    that the staged file exists rather than that it runs, keeps reporting the
    CLI as installed. Running the staged copy once, from the leaf, under the
    gateway's own environment, fails at ``stage-node`` with the cause named.
    """
    source_path = Path(source).resolve(strict=True)
    try:
        mode = source_path.stat().st_mode
    except OSError as exc:
        raise OSError(f"Node source could not be inspected: {source_path}") from exc
    if not stat.S_ISREG(mode) or not os.access(source_path, os.X_OK):
        raise OSError(f"Node source is not an executable regular file: {source_path}")
    root = _managed_cli_root()
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        raise OSError(f"managed CLI root is a symlink: {root}")
    destination = _managed_node_path()
    fd, incoming = tempfile.mkstemp(
        dir=root,
        prefix=f".{destination.name}-",
        suffix=".incoming",
    )
    os.close(fd)
    try:
        shutil.copyfile(source_path, incoming)
        if not platform_compat.IS_WINDOWS:
            os.chmod(incoming, 0o500)
        reason = _staged_node_runs(Path(incoming))
        if reason is not None:
            raise OSError(
                f"Node copied from {source_path} does not run from the managed leaf "
                f"({reason}). Install Node as a self-contained binary."
            )
        os.replace(incoming, destination)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(incoming)
    return destination


def _managed_cli_candidates() -> tuple[Path, ...]:
    """Entrypoint spellings npm creates under the managed prefix."""
    root = _managed_cli_root()
    managed_bin = root / "managed-bin"
    if platform_compat.IS_WINDOWS:
        npm_bin = root / "bin"
        return (
            managed_bin / f"{CLI_BIN}.cmd",
            managed_bin / f"{CLI_BIN}.exe",
            managed_bin / CLI_BIN,
            npm_bin / f"{CLI_BIN}.cmd",
            npm_bin / f"{CLI_BIN}.exe",
            npm_bin / CLI_BIN,
            root / f"{CLI_BIN}.cmd",
            root / f"{CLI_BIN}.exe",
            root / CLI_BIN,
        )
    return (managed_bin / CLI_BIN, root / "bin" / CLI_BIN)


def _system_cli_candidates() -> tuple[Path, ...]:
    """Fixed machine-install locations, never entries discovered from ``PATH``."""
    candidates: list[Path] = []
    if platform_compat.IS_WINDOWS:
        trusted = platform_compat.trusted_system_bin(CLI_BIN)
        if trusted:
            candidates.append(Path(trusted))
        for variable in github_runner.WINDOWS_PROGRAM_ROOT_VARS:
            root = os.environ.get(variable)
            if not root:
                continue
            node_dir = Path(root) / "nodejs"
            candidates.extend(
                (node_dir / f"{CLI_BIN}.cmd", node_dir / f"{CLI_BIN}.exe", node_dir / CLI_BIN)
            )
    else:
        directories: list[str] = []
        trusted_path = platform_compat.trusted_system_path()
        if trusted_path:
            directories.extend(trusted_path.split(os.pathsep))
        directories.extend(github_runner.PROVIDER_EXECUTABLE_DIRS)
        candidates.extend(Path(directory) / CLI_BIN for directory in dict.fromkeys(directories))
    return tuple(dict.fromkeys(candidates))


def _agent_writable_roots() -> tuple[Path, ...]:
    """Trees where an agent may replace an executable by design."""
    return github_runner.agent_writable_roots()


def _under(path: Path, root: Path) -> bool:
    try:
        return path == root or root in path.parents
    except (OSError, ValueError):
        return False


def _resolve_executable(candidate: Path) -> tuple[Path | None, str | None]:
    """Canonical executable file, or the reason this spelling is unusable."""
    try:
        resolved = candidate.resolve(strict=True)
        mode = resolved.stat().st_mode
    except OSError as exc:
        return None, f"the candidate could not be resolved ({exc})"
    if not stat.S_ISREG(mode):
        return None, "the resolved candidate is not a regular file"
    if not os.access(resolved, os.X_OK):
        return None, "the resolved candidate is not executable"
    return resolved, None


def _common_candidate_rejection(resolved: Path) -> str | None:
    """Reject roots the agent can write independently of candidate provenance."""
    try:
        agent_roots = _agent_writable_roots()
    except Exception:
        return "the agent-writable roots could not be verified"
    for root in agent_roots:
        if _under(resolved, root):
            return f"the resolved executable is inside the agent-writable tree {root}"
    try:
        user_local = (Path.home() / ".local").resolve(strict=False)
    except (OSError, RuntimeError):
        return "the user-local boundary could not be verified"
    if _under(resolved, user_local):
        return f"the resolved executable is under the user-local tree {user_local}"
    return None


def _managed_candidate(candidate: Path) -> tuple[Path | None, str | None]:
    """Validate an entrypoint inside the sandbox-sealed managed prefix."""
    root = _managed_cli_root()
    if root.is_symlink():
        return None, f"the managed tools boundary is a symlink ({root})"
    resolved, reason = _resolve_executable(candidate)
    if resolved is None:
        return None, reason
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        return None, f"the managed tools boundary could not be resolved ({exc})"
    if not _under(resolved, resolved_root):
        return None, f"the entrypoint resolves outside the managed tools boundary {resolved_root}"
    return (None, common) if (common := _common_candidate_rejection(resolved)) else (resolved, None)


def _gateway_writable_component(candidate: Path, resolved: Path) -> Path | None:
    """First executable hierarchy component writable by this gateway process.

    The question ("can this process write it") is asked of every directory the
    walk from *candidate* to *resolved* actually reads plus the target itself,
    enumerated by :func:`kiro_crew.platform_compat.traversed_components`. A
    lexical chain over the already-collapsed *resolved* path cannot name a
    symlink hop in the middle of the chain, nor a symlinked directory
    component's own parent, and both are places where whoever can write chooses
    what executes. The whole *candidate* is the answer when the walk cannot be
    enumerated: unknown is not shown-to-be-unwritable.

    Windows keeps the lexical chain over *resolved*: the walker is POSIX-shaped
    and the mode bits carry no information there, so ``os.access`` over the
    resolved spelling is the check that exists.
    """
    if platform_compat.IS_WINDOWS:
        components: list[Path] | None = [resolved, *resolved.parents]
    else:
        components = platform_compat.traversed_components(candidate)
    if components is None:
        return candidate
    for component in components:
        try:
            mode = component.stat().st_mode
        except OSError:
            return component
        if mode & (stat.S_IWGRP | stat.S_IWOTH) or os.access(component, os.W_OK):
            return component
    return None


def _system_candidate(candidate: Path) -> tuple[Path | None, str | None]:
    """Validate a fixed system candidate with the tailnet planted-binary floor."""
    resolved, reason = _resolve_executable(candidate)
    if resolved is None:
        return None, reason
    if common := _common_candidate_rejection(resolved):
        return None, common
    if writable := _gateway_writable_component(candidate, resolved):
        return None, f"the executable hierarchy is writable by the gateway user at {writable}"
    return resolved, None


_warned_cli_refusals: set[tuple[str, str]] = set()


def _warn_cli_refusal(candidate: Path, reason: str) -> None:
    """Emit one credential-redacted, repr-escaped warning per refused candidate."""
    safe_candidate = redact_install_output(str(candidate))
    safe_reason = redact_install_output(reason)
    key = (safe_candidate, safe_reason)
    if key in _warned_cli_refusals:
        return
    _warned_cli_refusals.add(key)
    logger.warning(
        "SECURITY: refusing playwright-cli candidate %r: %s. "
        "No browser launcher will run from this path.",
        safe_candidate,
        safe_reason,
    )


def cli_path() -> str | None:
    """Canonical trusted ``playwright-cli`` path, or ``None``.

    Resolution is absolute and ordered: the sandbox-sealed crew-home tools leaf
    first, then fixed machine-install directories. ``PATH`` and the legacy
    ``~/.local/bin/playwright-cli`` location are never execution sources. A PATH
    hit is inspected only after every vetted location misses, so the refusal can
    name the planted shim without ever running it.
    """
    first_refusal: tuple[Path, str] | None = None
    for candidate in _managed_cli_candidates():
        if not os.path.lexists(candidate):
            continue
        resolved, reason = _managed_candidate(candidate)
        if resolved is not None:
            if first_refusal is not None:
                _warn_cli_refusal(*first_refusal)
            return str(resolved)
        if reason is not None and first_refusal is None:
            first_refusal = (candidate, reason)
    for candidate in _system_cli_candidates():
        if not os.path.lexists(candidate):
            continue
        resolved, reason = _system_candidate(candidate)
        if resolved is not None:
            if first_refusal is not None:
                _warn_cli_refusal(*first_refusal)
            return str(resolved)
        if reason is not None and first_refusal is None:
            first_refusal = (candidate, reason)

    if first_refusal is None:
        found = shutil.which(CLI_BIN, path=os.environ.get("PATH", ""))
        if found:
            candidate = Path(found)
            resolved, reason = _resolve_executable(candidate)
            if resolved is not None:
                reason = _common_candidate_rejection(resolved)
                candidate = resolved
            first_refusal = (
                candidate,
                reason or "the candidate came from PATH, which is not a trusted launcher source",
            )
    if first_refusal is not None:
        _warn_cli_refusal(*first_refusal)
    return None


def _first_version(text: str) -> str | None:
    """First semver-looking token in *text*, or ``None``.

    Both ``node --version`` (``v24.18.0``) and ``playwright-cli --version``
    (``0.1.18``) are matched by the same scan, and a version banner that carries
    extra output around the number still parses.
    """
    m = _VERSION_RE.search(text)
    return m.group(0) if m else None


def _node_version() -> str | None:
    """Version reported by the resolved ``node``, or ``None`` if absent/mute."""
    node = find_node_tool("node")
    if node is None:
        return None
    rc, out, err = _run([node, "--version"], _PROBE_TIMEOUT_S)
    if rc != 0:
        logger.debug("node --version failed (rc=%d): %s", rc, err.strip())
        return None
    return _first_version(out)


def _node_major(version: str | None) -> int | None:
    """Major component of *version*, or ``None`` when it is unparseable."""
    if not version:
        return None
    m = _VERSION_RE.search(version)
    return int(m.group(1)) if m else None


# Ask the kernel which file is executing, not Node. ``process.execPath`` is argv[0]
# made absolute, and a launcher script that starts the binary with ``exec -a
# <script>`` sets argv[0] to itself -- Node then reports the script as its own
# executable. ``/proc/self/exe`` is the link the kernel keeps to the running
# image; no argv trick reaches it. Linux only: macOS and Windows have no procfs,
# and there ``process.execPath`` is the best answer available.
_NODE_EXECUTABLE_PROBE = (
    "(function(){try{return require('fs').realpathSync('/proc/self/exe')}"
    "catch(e){return process.execPath}})()"
)


def _node_runtime_executable(node: str) -> str | None:
    """Native executable behind a version-manager shim, if the host can name it."""
    rc, out, err = _run([node, "-p", _NODE_EXECUTABLE_PROBE], _PROBE_TIMEOUT_S)
    if rc != 0:
        logger.debug("node executable probe failed (rc=%d): %s", rc, err.strip())
        return None
    raw = out.strip()
    if not raw or "\n" in raw or "\0" in raw or not os.path.isabs(raw):
        return None
    resolved, _reason = _resolve_executable(Path(raw))
    return str(resolved) if resolved is not None else None


def _playwright_env(name: str) -> str | None:
    """Read *name* the way playwright-core's ``getFromENV`` does.

    The process environment first, then npm's ``npm_config_<name>`` and
    ``npm_package_config_<name>`` projections. An installer child inherits the
    gateway's environment (:func:`cli_env`), so these are the values it sees.
    """
    lowered = name.lower()
    for key in (name, f"npm_config_{lowered}", f"npm_package_config_{lowered}"):
        value = os.environ.get(key)
        if value is not None:
            return value
    return None


def _default_cache_root() -> Path | None:
    """playwright-core's ``computeDefaultCacheDirectory`` for this platform."""
    if platform_compat.IS_LINUX:
        xdg = os.environ.get("XDG_CACHE_HOME")
        return Path(xdg) if xdg else Path.home() / ".cache"
    if platform_compat.IS_MACOS:
        return Path.home() / "Library" / "Caches"
    if platform_compat.IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA")
        return Path(local) if local else Path.home() / "AppData" / "Local"
    return None


def _browsers_cache_dir() -> Path | None:
    """The browser registry directory the installed CLI downloads into.

    Mirrors ``registryDirectory`` in the playwright-core the CLI pins
    (``lib/coreBundle.js`` of ``playwright-core@1.64.0-alpha-1789764292000``,
    served to ``@playwright/cli@0.1.21``; ``lib/server/registry/index.js`` in
    1.58 has the same logic):

    * ``PLAYWRIGHT_BROWSERS_PATH=0`` means ``<playwright-core>/.local-browsers``,
      the package-local registry, so it is resolved against the SERVING core
      package rather than read as a directory named ``0``;
    * any other non-empty value is the directory itself;
    * otherwise ``ms-playwright`` under the platform cache root, which honours
      ``XDG_CACHE_HOME`` on Linux and falls back to ``~/AppData/Local`` on
      Windows when ``LOCALAPPDATA`` is unset;
    * a relative result is resolved against ``INIT_CWD`` or the working
      directory, which for the installer child is the gateway's own.

    ``None`` when the location cannot be determined (an unknown platform, or
    ``0`` with no attributable core package), which reads back as "unknown",
    never as a missing browser.
    """
    override = _playwright_env(_BROWSERS_CACHE_ENV)
    result: Path | None
    if override == "0":
        manifest = _browsers_manifest_path()
        result = manifest.parent / ".local-browsers" if manifest is not None else None
    elif override:
        result = Path(override)
    else:
        root = _default_cache_root()
        result = root / "ms-playwright" if root is not None else None
    if result is None:
        return None
    if not result.is_absolute():
        base = _playwright_env("INIT_CWD") or os.getcwd()
        result = Path(os.path.abspath(Path(base) / result))
    return result


# The engines Playwright downloads, in the order the panel lists them. A fixed
# tuple, not free input: it is what validates the engine name before it reaches
# argv (see `install_browser`), which is what keeps that spawn benign.
BROWSER_ENGINES: tuple[str, ...] = ("chromium", "firefox", "webkit")
_DEFAULT_BROWSER_ENGINE = BROWSER_ENGINES[0]


#: Per-engine download state reported by :func:`browser_status`.
STATUS_DOWNLOADED = "downloaded"
STATUS_MISSING = "missing"
STATUS_UNKNOWN = "unknown"

#: The file playwright-core writes into a browser directory once extraction has
#: finished (``browserDirectoryToMarkerFilePath`` in the registry and in
#: ``oopDownloadBrowserMain``). The directory exists from the start of the
#: download, so its presence alone does not mean a browser is there.
_INSTALLATION_MARKER = "INSTALLATION_COMPLETE"


def _cached_browser_names(cache: Path) -> set[str] | None:
    """Directory names in Playwright's browser cache, or ``None`` if unreadable.

    An absent cache directory is an empty set: nothing was ever downloaded,
    which is a confirmed absence rather than an unknown.
    """
    try:
        return {child.name for child in cache.iterdir() if child.is_dir()}
    except (FileNotFoundError, NotADirectoryError):
        return set()
    except OSError:
        return None


def _download_complete(directory: Path) -> bool | None:
    """Whether *directory* carries the completion marker; ``None`` if unreadable."""
    try:
        return (directory / _INSTALLATION_MARKER).is_file()
    except OSError:
        return None


# playwright-core ships this manifest beside its entry point, listing the browser
# revision each engine needs. It is the file `install-browser` consults, so it is
# the authority on what "present" means for THIS installed CLI. Reading it is a
# plain file read, which keeps `detect()` subprocess-free.
_BROWSERS_MANIFEST = "browsers.json"
_PLAYWRIGHT_CORE_PKG = "playwright-core"

#: The CLI's own package coordinates. The manifest is only trusted when it is
#: served to THIS package, so the scope and name are what anchors the search.
_CLI_PKG_SCOPE = "@playwright"
_CLI_PKG_NAME = "cli"

#: Where the standalone installer puts the package tree. `npm --global --prefix`
#: writes under ``<prefix>/lib/node_modules`` on POSIX and ``<prefix>/node_modules``
#: on Windows, so both are probed.
_STANDALONE_PREFIX_ENV = "KIROCREW_PLAYWRIGHT_CLI_HOME"
_NODE_MODULES = "node_modules"


def _standalone_node_modules() -> list[Path]:
    """``node_modules`` roots of the managed, unprivileged CLI install.

    The product-owned default is the sandbox-sealed tools leaf. An explicit
    operator prefix remains useful to the standalone installer and is trusted as
    operator configuration, but it never adds that prefix to executable
    resolution: :func:`cli_path` still accepts only the managed leaf or fixed
    system locations.
    """
    prefix_override = os.environ.get(_STANDALONE_PREFIX_ENV, "").strip()
    prefix = Path(prefix_override) if prefix_override else _managed_cli_root()
    return [prefix / "lib" / _NODE_MODULES, prefix / _NODE_MODULES]


def _launcher_node_modules(anchor: Path) -> list[Path]:
    """``node_modules`` roots to probe relative to the resolved launcher.

    A launcher that is a SYMLINK resolves INTO the package tree, so an ancestor is
    already the package directory and none of these are needed. A launcher that is
    a real FILE resolves to itself, and then the tree has to be found beside it:
    ``npm install -g`` writes a ``.cmd`` batch wrapper on Windows, and a generated
    shell wrapper is what the standalone installer produces. Without this, those
    shapes read no revision at all and fall back to presence-only -- the exact
    false positive the revision gate exists to remove.

    Bounded to the launcher's own install prefix rather than walked toward the
    filesystem root, because an unbounded walk is what allowed a foreign tree to
    supply the revision. Every candidate still has to hold ``@playwright/cli``
    (see :func:`_cli_package_dirs`), so a stray ``playwright-core`` from an
    unrelated install is still unreachable.
    """
    return [
        # <prefix>/playwright-cli.cmd  ->  <prefix>/node_modules  (npm -g, Windows)
        anchor.parent / _NODE_MODULES,
        # <prefix>/bin/playwright-cli  ->  <prefix>/node_modules
        anchor.parent.parent / _NODE_MODULES,
        # <prefix>/bin/playwright-cli  ->  <prefix>/lib/node_modules  (npm -g, POSIX)
        anchor.parent.parent / "lib" / _NODE_MODULES,
    ]


def _cli_package_for_launcher(anchor: Path) -> Path | None:
    """The ``@playwright/cli`` package served by one resolved launcher."""
    try:
        resolved = anchor.resolve(strict=True)
    except OSError:
        return None
    for parent in resolved.parents:
        if parent.name == _CLI_PKG_NAME and parent.parent.name == _CLI_PKG_SCOPE:
            return parent
    for node_modules in _launcher_node_modules(resolved):
        package = node_modules / _CLI_PKG_SCOPE / _CLI_PKG_NAME
        if package.is_dir():
            return package
    return None


def _cli_package_dirs() -> list[Path]:
    """``@playwright/cli`` package directories on this host, most specific first.

    Three sources, in priority order: the package attributed to the resolved
    launcher, then the standalone installer's known prefix. Keeping the active
    launcher's package first prevents stale fallback metadata from describing a
    different executable.
    """
    dirs: list[Path] = []
    cli = cli_path()
    if cli is not None and (package := _cli_package_for_launcher(Path(cli))) is not None:
        dirs.append(package)
    for node_modules in _standalone_node_modules():
        package = node_modules / _CLI_PKG_SCOPE / _CLI_PKG_NAME
        if package.is_dir() and package not in dirs:
            dirs.append(package)
    return dirs


def _regular_file_within(candidate: Path, root: Path) -> tuple[Path | None, str | None]:
    """Resolve a regular file and require it to stay under *root*."""
    try:
        resolved = candidate.resolve(strict=True)
        resolved_root = root.resolve(strict=True)
        mode = resolved.stat().st_mode
    except OSError as exc:
        return None, f"the direct launcher file could not be resolved ({exc})"
    if not stat.S_ISREG(mode):
        return None, "the direct launcher target is not a regular file"
    if not _under(resolved, resolved_root):
        return None, f"the direct launcher target resolves outside {resolved_root}"
    if common := _common_candidate_rejection(resolved):
        return None, common
    return resolved, None


def _system_node_candidates(launcher: Path, package: Path) -> tuple[Path, ...]:
    """Fixed Node locations that may serve one system CLI package.

    No entry comes from ``PATH``. Package-prefix candidates keep ordinary global
    npm layouts working, and the remaining directories are the same fixed
    machine/provider roots used for launcher discovery. Every returned file still
    passes :func:`_system_candidate` before execution.
    """
    name = "node.exe" if platform_compat.IS_WINDOWS else "node"
    candidates: list[Path] = []
    for parent in package.parents:
        if parent.name != _NODE_MODULES:
            continue
        container = parent.parent
        candidates.append(container / name)
        if container.name in {"lib", "lib64"}:
            candidates.append(container.parent / "bin" / name)
        else:
            candidates.append(container / "bin" / name)
        break
    candidates.append(launcher.parent / name)
    if platform_compat.IS_WINDOWS:
        candidates.extend(candidate.parent / name for candidate in _system_cli_candidates())
    else:
        directories: list[str] = []
        trusted_path = platform_compat.trusted_system_path()
        if trusted_path:
            directories.extend(trusted_path.split(os.pathsep))
        directories.extend(github_runner.PROVIDER_EXECUTABLE_DIRS)
        candidates.extend(Path(directory) / name for directory in directories)
    return tuple(dict.fromkeys(candidates))


def _system_node_for_launcher(launcher: Path, package: Path) -> tuple[Path | None, str | None]:
    """First fixed, non-writable Node candidate for a system CLI install."""
    first_refusal: tuple[Path, str] | None = None
    for candidate in _system_node_candidates(launcher, package):
        if not os.path.lexists(candidate):
            continue
        resolved, reason = _system_candidate(candidate)
        if resolved is not None:
            return resolved, None
        if reason is not None and first_refusal is None:
            first_refusal = (candidate, reason)
    if first_refusal is not None:
        candidate, reason = first_refusal
        return None, f"the fixed Node candidate {candidate} was refused: {reason}"
    return None, "no fixed non-writable Node executable serves this system launcher"


def _direct_cli_command(cli: str) -> tuple[list[str] | None, str | None]:
    """Direct trusted Node+JavaScript argv for one vetted launcher identity."""
    launcher = Path(cli)
    package = _cli_package_for_launcher(launcher)
    if package is None:
        return None, "the serving @playwright/cli package could not be attributed"
    entry = package / "playwright-cli.js"
    try:
        launcher_resolved = launcher.resolve(strict=True)
    except OSError as exc:
        return None, f"the launcher could not be resolved ({exc})"

    managed_root: Path | None = None
    raw_managed_root = _managed_cli_root()
    if os.path.lexists(raw_managed_root) and not raw_managed_root.is_symlink():
        try:
            managed_root = raw_managed_root.resolve(strict=True)
        except OSError:
            managed_root = None
    if managed_root is not None and _under(launcher_resolved, managed_root):
        node, reason = _managed_candidate(_managed_node_path())
        entry_resolved, entry_reason = _regular_file_within(entry, managed_root)
    else:
        node, reason = _system_node_for_launcher(launcher_resolved, package)
        entry_resolved, entry_reason = _resolve_executable_file_for_system(entry)
    if node is None:
        return None, reason or "the direct Node executable is unavailable"
    if entry_resolved is None:
        return None, entry_reason or "the direct JavaScript entrypoint is unavailable"
    return [str(node), str(entry_resolved)], None


def _resolve_executable_file_for_system(candidate: Path) -> tuple[Path | None, str | None]:
    """Validate a non-executable package file under a fixed system hierarchy."""
    try:
        resolved = candidate.resolve(strict=True)
        mode = resolved.stat().st_mode
    except OSError as exc:
        return None, f"the direct launcher file could not be resolved ({exc})"
    if not stat.S_ISREG(mode):
        return None, "the direct launcher target is not a regular file"
    if common := _common_candidate_rejection(resolved):
        return None, common
    if writable := _gateway_writable_component(candidate, resolved):
        return None, f"the direct launcher hierarchy is writable by the gateway user at {writable}"
    return resolved, None


def cli_command(cli: str | None = None) -> list[str] | None:
    """Safe direct argv prefix for every gateway-owned CLI call.

    The resolved launcher is identity only on every OS. The gateway invokes a
    validated Node executable plus the attributed package entrypoint directly,
    so neither a POSIX ``env node`` shebang nor a Windows batch processor can
    choose another executable or reparse request data.
    """
    resolved = cli or cli_path()
    if resolved is None:
        return None
    command, reason = _direct_cli_command(resolved)
    if command is None:
        _warn_cli_refusal(Path(resolved), reason or "the launcher could not be unwrapped")
    return command


_LIFECYCLE_SOURCE_MAX_BYTES = 32 * 1024 * 1024


@lru_cache(maxsize=8)
def _source_contains(path_text: str, mtime_ns: int, size: int, needle: bytes) -> bool | None:
    """Whether one version-pinned installed source file contains *needle*.

    ``None`` when the file was never read: empty, past the size ceiling, or an
    I/O failure. That is a different answer from ``False``, which means the bytes
    were searched and the needle is not in them. Collapsing the two lets a
    caller report a capability the CLI was never measured for.
    """
    del mtime_ns  # cache-key only: invalidates the answer after an upgrade
    if size <= 0 or size > _LIFECYCLE_SOURCE_MAX_BYTES:
        return None
    try:
        return needle in Path(path_text).read_bytes()
    except OSError:
        return None


class SeamSupport(enum.StrEnum):
    """Verdict on one upstream seam the installed CLI is pinned against.

    ``UNVERIFIED`` is not a softer ``UNSUPPORTED``. It means no bundle was read,
    so the CLI's capability is unknown and the finding is about THIS host's path
    attribution. Collapsing the two makes every gate report a capability gap and
    sends the operator to upgrade a CLI that already carries the seam.
    """

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNVERIFIED = "unverified"


#: What an operator can do about an ``UNVERIFIED`` verdict. ``PATH`` is not a
#: launcher source (see :func:`cli_path`), so a version-manager shim there is
#: refused by design and re-admitting it is not the remedy.
ATTRIBUTION_REMEDY = (
    "install the CLI where Kiro Crew resolves it -- the Browser panel's install "
    "action, or the standalone installer -- so its @playwright/cli package "
    "directory is attributable"
)


def _serving_core_root() -> tuple[Path | None, str]:
    """The ``playwright-core`` tree that will serve the resolved launcher.

    Anchored on the launcher ALONE, not on :func:`_cli_package_dirs`. That list
    falls back to the standalone install prefix, which is right for a revision
    lookup (a fallback revision beats no revision) and wrong here: a seam verdict
    is a claim about the CLI that will actually run, so a package belonging to a
    different install must not answer for a launcher whose own package could not
    be found. It would put back the same misreport in a narrower case.

    ``(None, detail)`` when nothing could be attributed, *detail* naming what was
    resolved and what was missing beside it. Callers report that as attribution,
    never as a measured capability.
    """
    cli = cli_path()
    if cli is None:
        return None, "no trusted playwright-cli launcher resolved on this host"
    package = _cli_package_for_launcher(Path(cli))
    if package is None:
        return None, (
            "no @playwright/cli package directory is attributable to the resolved "
            f"launcher {redact_install_output(cli)}"
        )
    manifest = _manifest_for_cli_package(package)
    if manifest is None:
        return None, (
            "no playwright-core tree serves the CLI package "
            f"{redact_install_output(str(package))}"
        )
    return manifest.parent, ""


def _seams_support(seams: tuple[tuple[Path, bytes], ...]) -> tuple[SeamSupport, str]:
    """Read *seams* in order, stopping at the first that is absent or unreadable.

    An unreadable bundle is ``UNVERIFIED``, not ``UNSUPPORTED``: the needle was
    never searched for, so nothing was learned about the CLI.
    """
    for path, needle in seams:
        try:
            info = path.stat()
        except OSError as exc:
            return SeamSupport.UNVERIFIED, redact_install_output(
                f"the serving bundle {path} could not be read ({exc})"
            )
        found = _source_contains(str(path), info.st_mtime_ns, info.st_size, needle)
        if found is None:
            return SeamSupport.UNVERIFIED, redact_install_output(
                f"the serving bundle {path} could not be read"
            )
        if not found:
            return SeamSupport.UNSUPPORTED, ""
    return SeamSupport.SUPPORTED, ""


def cli_lifecycle_env_support() -> tuple[SeamSupport, str]:
    """Whether the installed CLI honors both stable lifecycle env variables.

    The variable names are upstream test-prefixed seams rather than a declared
    compatibility API. Checking the package that will actually launch the
    daemon turns a future rename/removal into a loud fail-back instead of
    silently putting sockets under scratch again.

    Three-valued on purpose. ``UNSUPPORTED`` means both bundles were read and a
    hook is gone -- a real capability gap upstream. ``UNVERIFIED`` means the
    serving tree could not be attributed to the resolved launcher, or a bundle
    could not be read, and comes with the detail a caller needs to say so.
    """
    core_root, detail = _serving_core_root()
    if core_root is None:
        return SeamSupport.UNVERIFIED, detail
    return _seams_support(
        (
            (
                core_root / "lib" / "tools" / "cli-client" / "registry.js",
                b"process.env.PWTEST_DAEMON_SESSION_DIR",
            ),
            (core_root / "lib" / "coreBundle.js", b"process.env.PWTEST_SOCKETS_DIR ||"),
        )
    )


def cli_dashboard_socket_support() -> tuple[SeamSupport, str]:
    """Whether the installed CLI's ``show`` dashboard listens where the panel expects.

    The dashboard app claims one singleton socket at
    ``makeSocketPath("dashboard", "app")`` under ``PWTEST_SOCKETS_DIR``, and the
    Browser panel's launcher sends its reveal request there. Both halves are
    upstream layout rather than a declared API, so they are pinned the same way
    :func:`cli_lifecycle_env_support` pins the socket-root hook: by reading the
    serving ``playwright-core`` bundle. A rename upstream turns the reveal into a
    logged skip instead of a connect to a path nothing listens on.

    Three-valued for the same reason as the lifecycle gate: an unattributable
    launcher is ``UNVERIFIED``, so the log line can name attribution instead of
    claiming the CLI lacks the layout.
    """
    core_root, detail = _serving_core_root()
    if core_root is None:
        return SeamSupport.UNVERIFIED, detail
    bundle = core_root / "lib" / "coreBundle.js"
    return _seams_support(
        (
            (bundle, b'makeSocketPath("dashboard", "app")'),
            (bundle, b"process.env.PWTEST_SOCKETS_DIR ||"),
        )
    )


def _manifest_for_cli_package(package: Path) -> Path | None:
    """The ``browsers.json`` of the ``playwright-core`` serving *package*.

    Two layouts, both anchored ON the package so the manifest can only come from
    the tree that will launch the browser: nested inside the package's own
    ``node_modules``, or hoisted as a sibling in the ``node_modules`` that holds
    ``@playwright/cli`` (``<pkg>/../..`` — up past ``@playwright``).
    """
    for candidate in (
        package / _NODE_MODULES / _PLAYWRIGHT_CORE_PKG / _BROWSERS_MANIFEST,
        package.parent.parent / _PLAYWRIGHT_CORE_PKG / _BROWSERS_MANIFEST,
    ):
        if candidate.is_file():
            return candidate
    return None


def _browsers_manifest_path() -> Path | None:
    """Locate the installed ``playwright-core/browsers.json``, or ``None``.

    Resolution is anchored on the ``@playwright/cli`` package
    (:func:`_cli_package_dirs`) rather than walked up from the launcher toward
    the filesystem root. The anchor is the correctness property, not a
    shortcut: an unbounded walk passes through ``$HOME`` on the standalone
    layout, where a single unrelated ``~/node_modules/playwright-core`` would
    supply a revision from a DIFFERENT install. That reports a **working**
    browser broken, and keeps reporting it after the download the panel offers,
    because the gate goes on reading the foreign manifest. Requiring the
    manifest to be served to the CLI package makes a foreign tree unreachable.

    ``None`` when no manifest can be attributed to a CLI package, which callers
    treat as "revision unknown" and answer with the documented presence-only
    fallback.
    """
    for package in _cli_package_dirs():
        manifest = _manifest_for_cli_package(package)
        if manifest is not None:
            return manifest
    return None


def _required_revisions() -> dict[str, str] | None:
    """Required revision per engine, read from ``browsers.json``, or ``None``.

    ``None`` means the required revision cannot be determined -- the manifest is
    absent, unreadable, or not the shape this expects. Callers treat that as
    "cannot confirm a revision" and fall back to the older presence-only check
    rather than turning a working browser into a reported-broken one.

    Keyed by the manifest's own engine names (``chromium``,
    ``chromium-headless-shell``, ``firefox``, ``webkit``, ...). Only entries with
    a string ``name`` and ``revision`` are kept, so a malformed row is skipped
    rather than crashing the read.
    """
    path = _browsers_manifest_path()
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    browsers = data.get("browsers") if isinstance(data, dict) else None
    if not isinstance(browsers, list):
        return None
    revisions: dict[str, str] = {}
    for entry in browsers:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        revision = entry.get("revision")
        if isinstance(name, str) and isinstance(revision, str):
            revisions[name] = revision
    return revisions or None


def _revision_overrides() -> dict[str, dict[str, str]]:
    """Per-engine ``revisionOverrides`` from ``browsers.json`` (host platform -> revision).

    playwright-core downloads an overridden revision into
    ``<engine>_<hostPlatform>_special-<revision>`` (``readDescriptors``) on the
    hosts an override names, such as WebKit on older Debian and Ubuntu. Empty
    when there is no manifest or no override.
    """
    path = _browsers_manifest_path()
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    browsers = data.get("browsers") if isinstance(data, dict) else None
    if not isinstance(browsers, list):
        return {}
    overrides: dict[str, dict[str, str]] = {}
    for entry in browsers:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            continue
        raw = entry.get("revisionOverrides")
        if isinstance(raw, dict):
            kept = {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}
            if kept:
                overrides[entry["name"]] = kept
    return overrides


def _playwright_arch() -> str | None:
    """Node's ``os.arch()`` name for this machine, for the architectures Playwright ships."""
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    return None


def _playwright_host_platform() -> str:
    """playwright-core's ``hostPlatform`` key for this host, or ``"<unknown>"``.

    A port of ``calculatePlatform`` in playwright-core's
    ``lib/server/utils/hostPlatform.js`` (1.58 and the 1.64 build the CLI pins),
    because the key decides which directory ``revisionOverrides`` sends the
    installer to: a host the override names gets
    ``<engine>_<key>_special-<rev>``, every other host the plain directory. One
    known divergence: on macOS Playwright appends ``-arm64`` when a CPU model
    names Apple, and this reads the machine type, which differs only for an
    x64 interpreter under Rosetta.
    """
    override = os.environ.get("PLAYWRIGHT_HOST_PLATFORM_OVERRIDE", "")
    if override:
        return override
    if platform_compat.IS_MACOS:
        try:
            major = int(platform.release().split(".")[0])
        except ValueError:
            return "<unknown>"
        if major < 18:
            return "mac10.13"
        if major == 18:
            return "mac10.14"
        if major == 19:
            return "mac10.15"
        key = f"mac{min(major - 9, 15)}"
        return f"{key}-arm64" if _playwright_arch() == "arm64" else key
    if platform_compat.IS_WINDOWS:
        return "win64"
    if not platform_compat.IS_LINUX:
        return "<unknown>"
    arch = _playwright_arch()
    if arch is None:
        return "<unknown>"
    suffix = f"-{arch}"
    try:
        release = platform.freedesktop_os_release()
    except OSError:
        release = {}
    distro = release.get("ID", "").lower()
    version = release.get("VERSION_ID", "")
    try:
        major_version = int(version.split(".")[0])
    except ValueError:
        major_version = 0
    if distro in ("ubuntu", "pop", "neon", "tuxedo"):
        if major_version < 20:
            return f"ubuntu18.04{suffix}"
        if major_version < 22:
            return f"ubuntu20.04{suffix}"
        if major_version < 24:
            return f"ubuntu22.04{suffix}"
        if major_version < 26:
            return f"ubuntu24.04{suffix}"
        return f"ubuntu{version}{suffix}"
    if distro == "linuxmint":
        if major_version <= 20:
            return f"ubuntu20.04{suffix}"
        if major_version == 21:
            return f"ubuntu22.04{suffix}"
        return f"ubuntu24.04{suffix}"
    if distro in ("debian", "raspbian"):
        if version in ("11", "12", "13"):
            return f"debian{version}{suffix}"
        if version == "":
            return f"debian13{suffix}"
    return f"ubuntu24.04{suffix}"


def _cache_dir_name_for(engine: str, revision: str) -> str:
    """The cache directory name that satisfies *engine* at *revision*.

    Playwright names the directory ``<engine>-<revision>`` (``chromium-1232``),
    with hyphens in the engine name turned into underscores. The engines in
    :data:`BROWSER_ENGINES` contain none, so the name is used as-is.
    """
    return f"{engine}-{revision}"


def _status_of(complete: list[bool | None]) -> str:
    """Fold the completion readings of candidate directories into one status."""
    if any(value is True for value in complete):
        return STATUS_DOWNLOADED
    if any(value is None for value in complete):
        return STATUS_UNKNOWN
    return STATUS_MISSING


def browser_status() -> dict[str, str]:
    """Per-engine download state for the revision the installed CLI needs.

    ``downloaded`` means a directory for the required revision holds
    playwright-core's completion marker (:data:`_INSTALLATION_MARKER`). It is
    passive filesystem evidence: nothing is launched, so it is not a claim that
    the browser starts. ``missing`` means the cache was read and no complete
    build is there, which includes a directory an interrupted download left
    behind. ``unknown`` means the answer could not be read -- an unknown cache
    location or an unreadable cache or marker.

    Revision matching is exact: playwright-core launches only the revision
    bound to its own version, so a stale ``chromium-1208`` left over from before
    a CLI upgrade is not the ``chromium-1232`` the upgraded CLI needs. When the
    required revision cannot be determined (no attributable manifest -- see
    :func:`_required_revisions`), any complete ``<engine>-*`` or
    ``<engine>_<host>_special-*`` build counts, the documented presence-only
    fallback that keeps missing metadata from turning a working browser into a
    reported-broken one.
    """
    cache = _browsers_cache_dir()
    names = _cached_browser_names(cache) if cache is not None else None
    if names is None or cache is None:
        return {engine: STATUS_UNKNOWN for engine in BROWSER_ENGINES}
    required = _required_revisions()
    overrides = _revision_overrides() if required is not None else {}
    host = _playwright_host_platform() if overrides else ""
    result: dict[str, str] = {}
    for engine in BROWSER_ENGINES:
        revision = required.get(engine) if required is not None else None
        if revision is None:
            # Presence-only: any complete build of this engine counts, including
            # the ``<engine>_<host>_special-<rev>`` directory playwright-core's
            # revisionOverrides writes on some hosts. Without a revision there is
            # nothing to match it against, so it is as good as the plain one; the
            # headless shell (``chromium_headless_shell-*``) still does not count.
            candidates = sorted(
                n for n in names if n.startswith(f"{engine}-") or _is_special_build_name(n, engine)
            )
            result[engine] = _status_of([_download_complete(cache / n) for n in candidates])
            continue
        # Exactly one directory satisfies the engine on this host, the one the
        # installer writes: the special override directory when the override
        # names this host's exact platform key, the plain one otherwise.
        host_revision = overrides.get(engine, {}).get(host)
        if host_revision is not None:
            wanted = f"{engine}_{host}_special-{host_revision}"
        else:
            wanted = _cache_dir_name_for(engine, revision)
        status = _status_of([_download_complete(cache / wanted)] if wanted in names else [])
        if status == STATUS_MISSING and _has_complete_special_build(cache, names, engine):
            # A special directory exists only because playwright-core's own
            # `calculatePlatform` chose it, so a complete one this host key did
            # not predict is evidence that the port in `_playwright_host_platform`
            # is stale relative to the installed playwright-core (the CLI pins
            # `@playwright/cli@latest`), not that the cache is empty. Read that as
            # `unknown` rather than a confident `missing`.
            status = STATUS_UNKNOWN
        elif status == STATUS_MISSING and host_revision is not None:
            # The converse: the port says this host takes the special build, but
            # the installer wrote (and completed) the plain required-revision
            # directory instead. Same rule: the port may be wrong about this host,
            # so the plain build is evidence for `unknown`, not `missing`.
            plain = _cache_dir_name_for(engine, revision)
            if plain in names and _download_complete(cache / plain) is True:
                status = STATUS_UNKNOWN
        result[engine] = status
    return result


def _is_special_build_name(name: str, engine: str) -> bool:
    """Whether *name* is an ``<engine>_<host>_special-<rev>`` cache directory."""
    return name.startswith(f"{engine}_") and "_special-" in name


def _has_complete_special_build(cache: Path, names: set[str], engine: str) -> bool:
    """Whether some complete ``<engine>_<host>_special-<rev>`` directory is in *cache*."""
    return any(
        _is_special_build_name(name, engine) and _download_complete(cache / name) is True
        for name in names
    )


def browsers_present(status: dict[str, str] | None = None) -> dict[str, bool]:
    """Which engines are ``downloaded`` per :func:`browser_status`.

    Reported per engine rather than as one boolean so the panel can offer each
    download separately: a user who wants to check a page in Firefox should not
    have to discover that "browser installed" only ever meant Chromium. An
    ``unknown`` engine is ``False`` here; callers that must tell unknown from
    missing read :func:`browser_status` instead. *status* reuses a reading the
    caller already took.
    """
    reading = status if status is not None else browser_status()
    return {engine: reading.get(engine) == STATUS_DOWNLOADED for engine in BROWSER_ENGINES}


def _browser_present(status: dict[str, str] | None = None) -> bool:
    """Whether a downloaded Chromium build exists in Playwright's cache.

    Chromium only, and that narrowness is the point: it is the engine
    ``attach``/``--extension`` supports, so a cache holding solely Firefox or
    WebKit does not make the capability work. This stays the single
    ``browser_ok`` capability gate even though `browsers_present` reports all
    three, because the other two are extras rather than prerequisites.
    """
    return browsers_present(status).get("chromium", False)


_INSTALLER_BASE = "https://raw.githubusercontent.com/kirodotdev/KiroCrew/main"


def _standalone_install_command() -> str:
    """The command that installs the CLI where `npm install -g` cannot.

    `playwright-cli.sh` / `.ps1` bootstrap their own Node into the user's home
    directory and classify the enterprise failures npm reports as one
    undifferentiated error, so they are the answer for the two states this
    module can detect but not fix: no usable Node, and a registry that refuses
    the request.

    Download-then-run rather than piping into a shell, because a machine locked
    down enough to need this is usually also one where piping a script from the
    network into `sh` is forbidden -- and because it is the form that lets the
    operator read what they are about to run.

    The PowerShell form wraps the download in try/catch and exits on failure.
    Downloaded into a FRESH TEMPORARY path, never the working directory under a
    fixed name. The operator pastes this into whatever shell they happen to have
    open, so the destination is a directory this command does not own: a file
    already named `playwright-cli.sh` there -- their own copy, mid-edit, or
    something unrelated -- would be truncated by the download. An unpredictable
    name also retires the planted-file hazard the chaining below guards against.

    `;` in PowerShell is a statement SEPARATOR, not `&&`: a failed download
    otherwise falls straight through to the `powershell -File` call and runs
    whatever is at that path. `-ErrorAction Stop` alone does NOT prevent this;
    measured on pwsh 7.6, the following statement still runs, because the
    terminating error ends the pipeline rather than the command. `&&` would be the
    obvious fix and is unavailable: this command targets Windows PowerShell 5.1,
    which has no `&&`.
    """
    if os.name == "nt":
        return (
            '$p = Join-Path $env:TEMP "playwright-cli-$([guid]::NewGuid()).ps1"; '
            f"try {{ irm {_INSTALLER_BASE}/playwright-cli.ps1 "
            "-OutFile $p -ErrorAction Stop } "
            "catch { Write-Error $_; exit 1 }; "
            "powershell -ExecutionPolicy Bypass -File $p"
        )
    # `&&` gives the shell form the same guarantee for free.
    # `_pwcli_dir`, not `d`: this runs at the top level of whatever interactive
    # shell the operator pasted it into, so the assignment persists in THEIR
    # session. `d` is a common scratch name and overwriting it silently is the
    # same discourtesy as writing playwright-cli.sh over their file. The directory
    # is left in place -- /tmp is reaped by the OS, and putting an `rm -rf` into a
    # string users are invited to edit is a worse trade than a stale temp dir.
    return (
        "_pwcli_dir=$(mktemp -d) && curl -fsSL "
        f'{_INSTALLER_BASE}/playwright-cli.sh -o "$_pwcli_dir/playwright-cli.sh" '
        '&& sh "$_pwcli_dir/playwright-cli.sh"'
    )


def _cli_package_json_for_command(command: list[str] | None) -> Path | None:
    """Package metadata served by *command*, or the vetted launcher fallback."""
    if command is not None and len(command) >= 2:
        try:
            entry = Path(command[1]).resolve(strict=True)
        except OSError:
            entry = None
        if (
            entry is not None
            and entry.name == "playwright-cli.js"
            and entry.parent.name == _CLI_PKG_NAME
            and entry.parent.parent.name == _CLI_PKG_SCOPE
        ):
            return entry.parent / "package.json"
    path = cli_path()
    if path is None:
        return None
    package = _cli_package_for_launcher(Path(path))
    return package / "package.json" if package is not None else None


def installed_cli_version(command: list[str] | None = None) -> str | None:
    """Return the attributed package version without spawning the CLI."""
    manifest = _cli_package_json_for_command(command)
    if manifest is None:
        return None
    try:
        info = manifest.stat()
    except OSError:
        return None
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_size <= 0
        or info.st_size > _CLI_PACKAGE_JSON_MAX_BYTES
    ):
        return None
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = payload.get("version") if isinstance(payload, dict) else None
    return _first_version(version) if isinstance(version, str) else None


def detect() -> dict[str, Any]:
    """Report what is installed, without changing anything.

    ``installed`` describes the CLI binary alone. It is intentionally
    independent of ``node_ok`` and ``browser_ok`` so a caller can tell "not
    installed" apart from "installed but unusable here", which are different
    problems with different fixes.
    """
    path = cli_path()
    node_version = _node_version()
    major = _node_major(node_version)
    command = cli_command(path) if path is not None else None
    cli_version = installed_cli_version(command)
    status = browser_status()
    return {
        "installed": command is not None,
        "cli_path": path if command is not None else None,
        "cli_version": cli_version,
        "node_ok": major is not None and major >= MIN_NODE_MAJOR,
        "node_version": node_version,
        "browser_ok": _browser_present(status),
        # Per-engine, so the panel can offer each download rather than
        # implying "browser" means only the one attach needs. ``browsers`` is
        # the boolean projection older dashboards read; ``browser_status``
        # keeps "could not read" apart from "not downloaded".
        "browsers": browsers_present(status),
        "browser_status": status,
        # What to run when THIS install cannot proceed. Composed here rather than
        # in the dashboard for three reasons: only the gateway knows which OS it
        # runs on, so the operator gets one correct command instead of two to
        # choose between; a shell command is not translatable copy and must not
        # enter the i18n catalogs, whose pseudolocale accents every Latin
        # character and would corrupt it; and the frontend's untranslated-literal
        # gate forbids holding it as a string there.
        "standalone_install": _standalone_install_command(),
    }


def available() -> bool:
    """Whether the browse capability exists on this host.

    Presence is an availability signal, not consent to skip shell approval.
    Node is not consulted: a host with the CLI installed and Node broken has a
    repairable environment, and reporting that as absent would send the operator
    to the wrong fix.
    """
    return cli_path() is not None


# A failing npm run can emit a very large log. The step keeps the redacted TAIL
# of stderr, capped at this many characters: Playwright prints the list of
# missing OS libraries last, so the tail is what the operator needs, not
# megabytes in a log line and a dashboard card. The remedy hint is carried
# separately and appended after the capped text.
_STDERR_CAP = 2000


# npm-specific credential shapes. The shared `redact_credentials` matches
# header-style secrets and (since the fetch-scheme widening) inline-credential
# http(s)/ftp(s) URLs, but still leaves the npm-specific forms intact -- the
# registry query (`?_authToken=`), the .npmrc line (`//host/:_authToken=`), and
# `*_TOKEN=` env echo. The URL pattern below stays as a backstop for schemes
# the shared alternation does not name.
# Scoped here rather than added to the shared helper: this is the one surface that
# emits npm output, and widening a security primitive every caller depends on is a
# change that deserves its own review.
_NPM_SECRET_RES = (
    re.compile(r"(_authToken\s*=\s*)[^\s&]+", re.I),
    re.compile(r"(_password\s*=\s*)[^\s&]+", re.I),
    # Bounded prefix ({0,40}), not `*`: an unbounded run before a required
    # keyword backtracks catastrophically on a large log -- MEASURED as a 120s
    # timeout on 50 KB of stderr, which would have hung the install task on a
    # real npm failure, not merely slowed a test.
    re.compile(r"([A-Z0-9_]{0,40}(?:TOKEN|SECRET|PASSWORD|APIKEY|API_KEY)\s*=\s*)[^\s&]+", re.I),
    # scheme://user:secret@host -- keep the user, drop the secret.
    re.compile(r"(://[^/\s:@]+:)[^@\s/]+(@)"),
)


def redact_install_output(text: str) -> str:
    """Redact credential-shaped content before it reaches a log or the dashboard.

    Runs the shared two-pass used on every external surface, then the npm shapes
    that pass leaves untouched (see :data:`_NPM_SECRET_RES`). Public: any surface
    that renders installer output (step ``stderr``, the ``error`` fallback, or an
    exception message quoting an npm line) must use THIS redactor rather than the
    shared pair alone, or a bare ``_authToken=<value>`` assignment survives.
    """
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    for pattern in _NPM_SECRET_RES:
        # The last pattern has a trailing group (the `@`); the rest have one.
        text = pattern.sub(
            lambda m: m.group(1) + "[REDACTED]" + (m.group(m.re.groups) if m.re.groups > 1 else ""),
            text,
        )
    return text


def _step(
    name: str,
    argv: list[str],
    timeout: float,
    hint: str = "",
    failure_signal: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """Run one install step and describe its outcome.

    stderr is carried only on failure: a successful ``npm install`` writes
    progress and deprecation notices there, and surfacing those to an operator
    reads as a broken install.

    It is redacted HERE, at the source, rather than only where the dashboard
    renders it. npm quotes the command's own environment back on failure -- a
    registry line carrying ``_authToken=``, a proxy URL with inline credentials --
    and the log is the longer-lived of the two surfaces: `kirocrew logs` output
    gets pasted into bug reports. Redacting at the boundary would have left the
    secret in the log file, which is the copy that outlives the session.

    *failure_signal* inspects the child's output for a failure the exit code does
    NOT report. A zero exit is otherwise taken at face value, which is wrong for
    exactly one case -- see :func:`os_deps.host_deps_unsatisfied`.

    *hint* is our own trusted remediation line, appended AFTER the cap so a long
    stderr cannot push the actionable part out of the operator's view. It is not
    redacted because it is a constant composed here, never external output.
    """
    rc, out, err = _run(argv, timeout)
    ok = rc == 0
    # Both streams: the diagnostic is on stderr today, and a step that starts
    # printing it to stdout must not silently reopen the bug this guards.
    if ok and failure_signal is not None and failure_signal(f"{err}\n{out}"):
        ok = False
    # Redact BEFORE truncating: a credential straddling the truncation
    # boundary does not match its regex (e.g. the trailing ``@`` in a
    # ``://user:pass@host`` URL is past the cap), so truncating first can
    # leak partial secrets. The npm-specific patterns use bounded
    # repetition (``{0,40}``) and the shared credential regex is a fixed
    # alternation with no nested quantifiers, so redacting the full
    # stderr is linear in input length — measured at <200 ms on 50 KB of
    # adversarial input, well below the subprocess timeout.
    detail = "" if ok else redact_install_output((err.strip() or out.strip()))[-_STDERR_CAP:]
    # The hint names missing OS libraries. A step the gateway interrupted or that
    # ran out of time did not fail for that reason, so it carries no hint; the
    # job layer reads the same return codes to report interrupted / timeout.
    ordinary_failure = not ok and rc not in (INTERRUPTED_RC, TIMEOUT_RC)
    step_hint = hint if ordinary_failure else ""
    if not ok:
        logger.warning("playwright-cli install step %s failed (rc=%d): %s", name, rc, detail)
        if step_hint:
            detail = f"{detail}\n\n{step_hint}" if detail else step_hint
    return {
        "name": name,
        "ok": ok,
        "returncode": rc,
        "stderr": detail,
        "hint": step_hint,
    }


#: Stage names reported through ``on_stage``, in the order :func:`install` runs
#: them. The dashboard's install job publishes ``preparing`` itself before the
#: worker starts, so the installer never reports it.
STAGE_INSTALLING_CLI = "installing_cli"
STAGE_DOWNLOADING_BROWSER = "downloading_browser"
STAGE_INSTALLING_SKILLS = "installing_skills"
STAGE_FINISHING = "finishing"

StageCallback = Callable[[str], None]


def _emit_stage(on_stage: StageCallback | None, stage: str) -> None:
    """Report *stage*; a failing callback is logged, never allowed to stop an install."""
    if on_stage is None:
        return
    try:
        on_stage(stage)
    except Exception:  # noqa: BLE001 - progress reporting must not fail the install
        logger.debug("browser install stage callback failed for %s", stage, exc_info=True)


def _download_browser(command: list[str], engine: str | None = None) -> list[dict[str, Any]]:
    """Download a browser build, adapting to what this host's OS allows.

    Shared by :func:`install` and :func:`install_browser` so the two cannot
    disagree about a host: they answer different product questions but face the
    same package manager.

    ``--with-deps`` is passed only where Playwright can honour it (see
    :mod:`kiro_crew.browser_cli.os_deps`), and even there a refusal is not fatal.
    Installing OS packages needs root, a managed workstation often withholds it,
    and the download itself needs no privilege at all -- so the flag is dropped
    and the download retried rather than losing the browser over a permission the
    operator may never have. Returns every attempt, so the panel shows what was
    tried instead of only the last verdict. The engine-aware remedy rides only on
    the attempt without the flag: it is the one a human has to act on.

    Every attempt is judged on its output as well as its exit code: a build whose
    libraries are missing downloads "successfully" and cannot launch.
    """
    selected_engine = engine or _DEFAULT_BROWSER_ENGINE
    base = [*command, "install-browser", selected_engine]
    # Keep baseline step names stable for the dashboard; optional engine
    # downloads name their engine so outcomes remain distinguishable.
    suffix = f"-{engine}" if engine else ""
    hint = os_deps.missing_deps_hint(selected_engine)

    def attempt(step_name: str, argv: list[str], with_hint: bool) -> dict[str, Any]:
        return _step(
            step_name,
            argv,
            _BROWSER_INSTALL_TIMEOUT_S,
            hint=hint if with_hint else "",
            failure_signal=os_deps.host_deps_unsatisfied,
        )

    if not os_deps.with_deps_supported():
        return [attempt(f"install-browser{suffix}", base, True)]

    first = attempt(f"install-browser{suffix}", base + ["--with-deps"], False)
    if first["ok"]:
        return [first]
    return [first, attempt(f"install-browser{suffix}-no-deps", base, True)]


def install(on_stage: StageCallback | None = None) -> dict[str, Any]:
    """Install the CLI, a browser, and the skills reference.

    Steps run in order and stop at the first failure, because each one depends
    on its predecessor: the browser download is driven by the binary the first
    step installs. The result carries every step attempted so an operator sees
    which one failed rather than only that something did.

    *on_stage* is called with each :data:`STAGE_INSTALLING_CLI` ..
    :data:`STAGE_FINISHING` transition as it happens, on the calling thread.
    """
    steps: list[dict[str, Any]] = []
    _emit_stage(on_stage, STAGE_INSTALLING_CLI)

    npm = find_node_tool("npm")
    if npm is None:
        steps.append(
            {
                "name": "npm-install-global",
                "ok": False,
                "returncode": 127,
                "stderr": "npm not found; install Node.js 20 or newer first",
            }
        )
        return {"ok": False, "steps": steps}

    try:
        with _pinned_managed_cli_root() as managed_root:
            steps.append(
                _step(
                    "npm-install-global",
                    [
                        npm,
                        "install",
                        "-g",
                        "--prefix",
                        str(managed_root),
                        NPM_SPEC,
                    ],
                    _NPM_INSTALL_TIMEOUT_S,
                )
            )
    except OSError as exc:
        steps.append(
            {
                "name": "npm-install-global",
                "ok": False,
                "returncode": 127,
                "stderr": f"refusing unsafe managed CLI prefix: {exc}",
            }
        )
        return {"ok": False, "steps": steps}
    if not steps[-1]["ok"]:
        return {"ok": False, "steps": steps}

    node = find_node_tool("node")
    try:
        if node is None:
            raise OSError("node not found after npm install")
        runtime_node = _node_runtime_executable(node)
        if runtime_node is None:
            raise OSError("Node did not report an executable process.execPath")
        staged_node = _stage_managed_node(runtime_node)
    except OSError as exc:
        steps.append(
            {
                "name": "stage-node",
                "ok": False,
                "returncode": 127,
                "stderr": f"could not stage Node for direct gateway execution: {exc}",
            }
        )
        return {"ok": False, "steps": steps}
    steps.append(
        {
            "name": "stage-node",
            "ok": True,
            "returncode": 0,
            "stderr": "",
            "path": str(staged_node),
        }
    )

    # Resolved after the global install, not before: the binary does not exist
    # until that step succeeds.
    path = cli_path()
    if path is None:
        steps.append(
            {
                "name": "resolve-binary",
                "ok": False,
                "returncode": 127,
                "stderr": (
                    f"{CLI_BIN} was not found in the managed tools leaf after "
                    "a successful install"
                ),
            }
        )
        return {"ok": False, "steps": steps}

    command = cli_command(path)
    if command is None:
        steps.append(
            {
                "name": "resolve-binary",
                "ok": False,
                "returncode": 127,
                "stderr": (
                    f"{CLI_BIN} could not be bound to the staged Node and package entrypoint"
                ),
            }
        )
        return {"ok": False, "steps": steps}

    _emit_stage(on_stage, STAGE_DOWNLOADING_BROWSER)
    steps.extend(_download_browser(command))
    if not steps[-1]["ok"]:
        return {"ok": False, "steps": steps}

    _emit_stage(on_stage, STAGE_INSTALLING_SKILLS)
    steps.append(
        _step(
            "install-skills",
            [*command, "install", "--skills", _SKILLS_TARGET, "--global"],
            _SKILLS_INSTALL_TIMEOUT_S,
        )
    )
    _emit_stage(on_stage, STAGE_FINISHING)
    # The LAST step decides. Every earlier gate has already returned on a real
    # failure, so only this step is undecided.
    return {"ok": steps[-1]["ok"], "steps": steps}


def install_browser(engine: str, on_stage: StageCallback | None = None) -> dict[str, Any]:
    """Download one engine's browser build.

    Separate from :func:`install` because the two answer different questions.
    ``install`` is "make browsing work at all" and downloads only the engine
    ``attach`` needs; this is "I also want to check this page in Firefox", which
    is a later, optional choice the old Browser Mode panel exposed as an engine
    selector and which would otherwise have no surface at all.

    *engine* is validated against :data:`BROWSER_ENGINES` before it can reach
    argv. That check is what keeps this spawn benign (fixed argv, no free input)
    rather than an agent-influenced one -- see ``test_spawn_audit``.

    *on_stage* receives :data:`STAGE_DOWNLOADING_BROWSER` before the download
    and :data:`STAGE_FINISHING` after it, on the calling thread.
    """
    if engine not in BROWSER_ENGINES:
        return {
            "ok": False,
            "steps": [
                {
                    "name": "install-browser",
                    "ok": False,
                    "returncode": 2,
                    "stderr": f"unknown engine {engine!r}; expected one of {BROWSER_ENGINES}",
                }
            ],
        }
    path = cli_path()
    if path is None:
        return {
            "ok": False,
            "steps": [
                {
                    "name": "resolve-binary",
                    "ok": False,
                    "returncode": 127,
                    "stderr": f"no vetted {CLI_BIN} launcher is installed; install the CLI first",
                }
            ],
        }
    command = cli_command(path)
    if command is None:
        return {
            "ok": False,
            "steps": [
                {
                    "name": "resolve-binary",
                    "ok": False,
                    "returncode": 127,
                    "stderr": f"no safe direct {CLI_BIN} command is available; reinstall the CLI",
                }
            ],
        }
    _emit_stage(on_stage, STAGE_DOWNLOADING_BROWSER)
    steps = _download_browser(command, engine)
    _emit_stage(on_stage, STAGE_FINISHING)
    return {"ok": steps[-1]["ok"], "steps": steps}
