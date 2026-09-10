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
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
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


def _run(argv: list[str], timeout: float) -> tuple[int, str, str]:
    """Run *argv*, returning ``(returncode, stdout, stderr)``.

    A timeout or a missing executable is reported as a non-zero return code with
    the reason on stderr, so callers branch on one shape instead of catching
    three exception types at every call site.
    """
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=cli_env(),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout:.0f}s: {' '.join(argv)}"
    except OSError as exc:
        return 127, "", f"{exc}"
    return proc.returncode, proc.stdout or "", proc.stderr or ""


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


def _stage_managed_node(source: str) -> Path:
    """Copy the installer-selected Node into the managed leaf atomically.

    Every gateway-owned CLI call invokes this copy with the package's JavaScript
    entrypoint directly. That keeps both executable choices inside the read-only
    leaf instead of letting ``#!/usr/bin/env node`` or a generated wrapper select
    a version-manager binary from the gateway's broad PATH.
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


def _gateway_writable_component(path: Path) -> Path | None:
    """First executable hierarchy component writable by this gateway process."""
    for component in (path, *path.parents):
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
    if writable := _gateway_writable_component(resolved):
        return None, f"the executable hierarchy is writable by the gateway user at {writable}"
    return resolved, None


_warned_cli_refusals: set[tuple[str, str]] = set()


def _warn_cli_refusal(candidate: Path, reason: str) -> None:
    """Emit one credential-redacted, repr-escaped warning per refused candidate."""
    safe_candidate = _redact(str(candidate))
    safe_reason = _redact(reason)
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


def _node_runtime_executable(node: str) -> str | None:
    """Native executable behind a version-manager shim, if Node can name it."""
    rc, out, err = _run([node, "-p", "process.execPath"], _PROBE_TIMEOUT_S)
    if rc != 0:
        logger.debug("node process.execPath failed (rc=%d): %s", rc, err.strip())
        return None
    raw = out.strip()
    if not raw or "\n" in raw or "\0" in raw or not os.path.isabs(raw):
        return None
    resolved, _reason = _resolve_executable(Path(raw))
    return str(resolved) if resolved is not None else None


def _browsers_cache_dir() -> Path | None:
    """Playwright's browser cache directory for this platform.

    ``None`` on a platform whose cache location this does not know, which reads
    back as "cannot confirm a browser" rather than as a missing browser.
    """
    override = os.environ.get(_BROWSERS_CACHE_ENV, "").strip()
    if override:
        return Path(override)
    if platform_compat.IS_MACOS:
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    if platform_compat.IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA", "").strip()
        return Path(local) / "ms-playwright" if local else None
    if platform_compat.IS_LINUX:
        return Path.home() / ".cache" / "ms-playwright"
    return None


# The engines Playwright downloads, in the order the panel lists them. A fixed
# tuple, not free input: it is what validates the engine name before it reaches
# argv (see `install_browser`), which is what keeps that spawn benign.
BROWSER_ENGINES: tuple[str, ...] = ("chromium", "firefox", "webkit")
_DEFAULT_BROWSER_ENGINE = BROWSER_ENGINES[0]


def _cached_browser_names() -> set[str] | None:
    """Directory names in Playwright's browser cache, or ``None`` if unreadable."""
    cache = _browsers_cache_dir()
    if cache is None:
        return None
    try:
        return {child.name for child in cache.iterdir() if child.is_dir()}
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
    if writable := _gateway_writable_component(resolved):
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
def _source_contains(path_text: str, mtime_ns: int, size: int, needle: bytes) -> bool:
    """Whether one version-pinned installed source file contains *needle*."""
    del mtime_ns  # cache-key only: invalidates the answer after an upgrade
    if size <= 0 or size > _LIFECYCLE_SOURCE_MAX_BYTES:
        return False
    try:
        return needle in Path(path_text).read_bytes()
    except OSError:
        return False


def cli_lifecycle_env_supported() -> bool:
    """Whether the installed CLI honors both stable lifecycle env variables.

    The variable names are upstream test-prefixed seams rather than a declared
    compatibility API. Checking the package that will actually launch the
    daemon turns a future rename/removal into a loud fail-back instead of
    silently putting sockets under scratch again. Answers false when the CLI is
    absent or its serving playwright-core tree cannot be attributed.
    """
    packages = _cli_package_dirs()
    if not packages:
        return False
    # _cli_package_dirs is most-specific-first: once an active launcher was
    # attributed, never let a stale standalone fallback satisfy its contract.
    package = packages[0]
    manifest = _manifest_for_cli_package(package)
    if manifest is None:
        return False
    core_root = manifest.parent
    registry = core_root / "lib" / "tools" / "cli-client" / "registry.js"
    bundle = core_root / "lib" / "coreBundle.js"
    try:
        registry_stat = registry.stat()
        bundle_stat = bundle.stat()
    except OSError:
        return False
    registry_ok = _source_contains(
        str(registry),
        registry_stat.st_mtime_ns,
        registry_stat.st_size,
        b"process.env.PWTEST_DAEMON_SESSION_DIR",
    )
    sockets_ok = _source_contains(
        str(bundle),
        bundle_stat.st_mtime_ns,
        bundle_stat.st_size,
        b"process.env.PWTEST_SOCKETS_DIR ||",
    )
    return registry_ok and sockets_ok


def cli_dashboard_socket_supported() -> bool:
    """Whether the installed CLI's ``show`` dashboard listens where the panel expects.

    The dashboard app claims one singleton socket at
    ``makeSocketPath("dashboard", "app")`` under ``PWTEST_SOCKETS_DIR``, and the
    Browser panel's launcher sends its reveal request there. Both halves are
    upstream layout rather than a declared API, so they are pinned the same way
    :func:`cli_lifecycle_env_supported` pins the socket-root hook: by reading the
    serving ``playwright-core`` bundle. A rename upstream turns the reveal into a
    logged skip instead of a connect to a path nothing listens on. Answers false
    when the CLI is absent or its serving tree cannot be attributed.
    """
    packages = _cli_package_dirs()
    if not packages:
        return False
    manifest = _manifest_for_cli_package(packages[0])
    if manifest is None:
        return False
    bundle = manifest.parent / "lib" / "coreBundle.js"
    try:
        bundle_stat = bundle.stat()
    except OSError:
        return False
    return _source_contains(
        str(bundle),
        bundle_stat.st_mtime_ns,
        bundle_stat.st_size,
        b'makeSocketPath("dashboard", "app")',
    ) and _source_contains(
        str(bundle),
        bundle_stat.st_mtime_ns,
        bundle_stat.st_size,
        b"process.env.PWTEST_SOCKETS_DIR ||",
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


def _cache_dir_name_for(engine: str, revision: str) -> str:
    """The cache directory name that satisfies *engine* at *revision*.

    Playwright names the directory ``<engine>-<revision>`` (``chromium-1232``).
    One name, not a set: the only caller passes engines from
    :data:`BROWSER_ENGINES`, none of which contains a hyphen, so an underscore
    variant of the same name could never match anything.
    """
    return f"{engine}-{revision}"


def browsers_present() -> dict[str, bool]:
    """Which engines have a build for the REVISION the installed CLI needs.

    Reported per engine rather than as one boolean so the panel can offer each
    download separately: a user who wants to check a page in Firefox should not
    have to discover that "browser installed" only ever meant Chromium.

    A cache dir carries the revision (``chromium-1232``), and playwright-core
    launches only the exact revision bound to its own version. A prefix match
    (``name.startswith(engine)``) ignores that revision, so a stale
    ``chromium-1208`` left over from before a CLI upgrade reads as present while
    the launch fails ``Browser "chromium" is not installed`` -- and because the
    gate reads ready, the panel never offers the download that would fix it. So
    ``browsers.json`` supplies the required revision and the match is exact.

    Degradation: when the required revision cannot be determined (manifest
    absent/unreadable -- see :func:`_required_revisions`), fall back to the older
    prefix match rather than reporting a browser broken on missing metadata. A
    missing manifest is an unknown, not evidence of a stale cache.
    """
    names = _cached_browser_names()
    if names is None:
        return {engine: False for engine in BROWSER_ENGINES}
    required = _required_revisions()
    if required is None:
        # Cannot confirm a revision: preserve the historical presence-only
        # behaviour rather than failing closed on absent metadata.
        return {
            engine: any(name.startswith(engine) for name in names) for engine in BROWSER_ENGINES
        }
    result: dict[str, bool] = {}
    for engine in BROWSER_ENGINES:
        revision = required.get(engine)
        if revision is None:
            # The engine is not in the manifest at all: we cannot say which
            # revision it needs, so degrade to presence-only for this one engine.
            result[engine] = any(name.startswith(engine) for name in names)
            continue
        wanted = _cache_dir_name_for(engine, revision)
        result[engine] = wanted in names
    return result


def _browser_present() -> bool:
    """Whether a downloaded Chromium build exists in Playwright's cache.

    Chromium only, and that narrowness is the point: it is the engine
    ``attach``/``--extension`` supports, so a cache holding solely Firefox or
    WebKit does not make the capability work. This stays the single
    ``browser_ok`` capability gate even though `browsers_present` reports all
    three, because the other two are extras rather than prerequisites.
    """
    return browsers_present().get("chromium", False)


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
    cli_version: str | None = None
    command = cli_command(path) if path is not None else None
    if command is not None:
        rc, out, err = _run([*command, "--version"], _PROBE_TIMEOUT_S)
        if rc == 0:
            cli_version = _first_version(out)
        else:
            logger.debug("%s --version failed (rc=%d): %s", CLI_BIN, rc, err.strip())
    return {
        "installed": command is not None,
        "cli_path": path if command is not None else None,
        "cli_version": cli_version,
        "node_ok": major is not None and major >= MIN_NODE_MAJOR,
        "node_version": node_version,
        "browser_ok": _browser_present(),
        # Per-engine, so the panel can offer each download rather than
        # implying "browser" means only the one attach needs.
        "browsers": browsers_present(),
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


# A failing npm run can emit a very large log; the operator needs the head of it,
# not megabytes in a log line and a dashboard card.
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


def _redact(text: str) -> str:
    """Redact credential-shaped content before it reaches a log or the dashboard.

    Runs the shared two-pass used on every external surface, then the npm shapes
    that pass leaves untouched (see :data:`_NPM_SECRET_RES`).
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
    detail = "" if ok else _redact((err.strip() or out.strip()))[:_STDERR_CAP]
    if not ok:
        logger.warning("playwright-cli install step %s failed (rc=%d): %s", name, rc, detail)
        if hint:
            detail = f"{detail}\n\n{hint}" if detail else hint
    return {
        "name": name,
        "ok": ok,
        "returncode": rc,
        "stderr": detail,
    }


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
    tried instead of only the last verdict.

    Every attempt is judged on its output as well as its exit code: a build whose
    libraries are missing downloads "successfully" and cannot launch.
    """
    selected_engine = engine or _DEFAULT_BROWSER_ENGINE
    base = [*command, "install-browser", selected_engine]
    # Keep baseline step names stable for the dashboard; optional engine
    # downloads name their engine so concurrent outcomes remain distinguishable.
    suffix = f"-{engine}" if engine else ""
    hint = os_deps.missing_deps_hint()

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


def install() -> dict[str, Any]:
    """Install the CLI, a browser, and the skills reference.

    Steps run in order and stop at the first failure, because each one depends
    on its predecessor: the browser download is driven by the binary the first
    step installs. The result carries every step attempted so an operator sees
    which one failed rather than only that something did.

    The browser step adapts to the host's package manager; see
    :func:`_download_browser`.
    """
    steps: list[dict[str, Any]] = []

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

    steps.extend(_download_browser(command))
    if not steps[-1]["ok"]:
        return {"ok": False, "steps": steps}

    steps.append(
        _step(
            "install-skills",
            [*command, "install", "--skills", _SKILLS_TARGET, "--global"],
            _SKILLS_INSTALL_TIMEOUT_S,
        )
    )
    # The LAST step decides, not every step: a recovered ``--with-deps`` refusal
    # leaves its failed attempt in the list for the operator to see, and that
    # entry must not veto an install the retry actually completed. Every earlier
    # gate has already returned on a real failure, so only this step is undecided.
    return {"ok": steps[-1]["ok"], "steps": steps}


def install_browser(engine: str) -> dict[str, Any]:
    """Download one engine's browser build.

    Separate from :func:`install` because the two answer different questions.
    ``install`` is "make browsing work at all" and downloads only the engine
    ``attach`` needs; this is "I also want to check this page in Firefox", which
    is a later, optional choice the old Browser Mode panel exposed as an engine
    selector and which would otherwise have no surface at all.

    *engine* is validated against :data:`BROWSER_ENGINES` before it can reach
    argv. That check is what keeps this spawn benign (fixed argv, no free input)
    rather than an agent-influenced one -- see ``test_spawn_audit``.
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
    steps = _download_browser(command, engine)
    return {"ok": steps[-1]["ok"], "steps": steps}
