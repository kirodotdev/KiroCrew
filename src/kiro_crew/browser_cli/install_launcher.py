"""Which ``playwright-cli`` the gateway runs, and the Node it runs it with.

``PATH`` is never an execution source. :func:`cli_path` resolves the sandbox-sealed managed
tools leaf first, then fixed machine-install locations, and refuses a candidate inside an
agent-writable tree, under ``~/.local``, or, for a system location, in a hierarchy the
gateway user can write. :func:`legacy_launcher_refused` tells a launcher the user installed
themselves, which the one-time managed reinstall fixes, apart from a planted shim. The
installer stages its Node into the leaf (:func:`_stage_managed_node`), and every
gateway-owned CLI call runs the argv :func:`cli_command` binds: a vetted Node plus the
JavaScript entrypoint of the ``@playwright/cli`` package attributed to the launcher.
Package attribution lives here too: :func:`_cli_package_for_launcher`, which
:func:`cli_command` needs, and :func:`_manifest_for_cli_package`, which the seam verdicts and
the browser revision check read.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Iterator
from pathlib import Path

from kiro_crew import github_runner, platform_compat
from kiro_crew.browser_cli.install_runner import _PROBE_TIMEOUT_S, _run, redact_install_output
from kiro_crew.config.paths import config_dir
from kiro_crew.env import find_node_tool

logger = logging.getLogger("kiro_crew.browser_cli.install")


CLI_BIN = "playwright-cli"


_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


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


def agent_writable_root_over_data_home(data_home: Path | None = None) -> Path | None:
    """The agent-writable root that contains the data home, or ``None``.

    The managed launcher lives under the data home, so a workspace root or
    project directory at the data home or above it (``$HOME`` is the usual
    case) puts every launcher the installer writes inside the fence.
    """
    try:
        data_home = (data_home or config_dir()).resolve(strict=False)
        roots = _agent_writable_roots()
    except (OSError, RuntimeError):
        return None
    for root in roots:
        if _under(data_home, root):
            return root
    return None


def _data_home_overlap_remedy(root: Path) -> str:
    return (
        f"the workspace root or project directory {root} contains the Kiro Crew data "
        f"home ({config_dir()}), so a launcher installed there is inside the "
        "agent-writable tree and would be refused. Point it at a directory outside "
        "the data home and restart the gateway"
    )


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


def _common_candidate_rejection(resolved: Path) -> tuple[str, str] | None:
    """Reject roots the agent can write independently of candidate provenance.

    Returns ``(reason, kind)`` for a rejected location or ``None`` when the
    location clears. ``kind`` is :data:`_REFUSAL_LEGACY` only for the user's own
    ``~/.local`` install (a one-time managed reinstall is the remedy); an
    agent-writable tree is a planted shim and a boundary that cannot be verified
    is an anomaly, so both are :data:`_REFUSAL_UNTRUSTED` -- a reinstall does not
    address either.
    """
    try:
        agent_roots = _agent_writable_roots()
    except Exception:
        return "the agent-writable roots could not be verified", _REFUSAL_UNTRUSTED
    for root in agent_roots:
        if _under(resolved, root):
            return (
                f"the resolved executable is inside the agent-writable tree {root}",
                _REFUSAL_UNTRUSTED,
            )
    try:
        user_local = (Path.home() / ".local").resolve(strict=False)
    except (OSError, RuntimeError):
        return "the user-local boundary could not be verified", _REFUSAL_UNTRUSTED
    if _under(resolved, user_local):
        return (
            f"the resolved executable is under the user-local tree {user_local}",
            _REFUSAL_LEGACY,
        )
    return None


def _managed_candidate(candidate: Path) -> tuple[Path | None, str | None, str | None]:
    """Validate an entrypoint inside the sandbox-sealed managed prefix.

    Returns ``(resolved, reason, kind)``. Every managed-prefix refusal is an
    anomaly -- a symlinked boundary, an entrypoint outside the sealed prefix, or
    an agent-writable component -- never a plain user install, so the kind is
    always :data:`_REFUSAL_UNTRUSTED`.
    """
    root = _managed_cli_root()
    if root.is_symlink():
        return None, f"the managed tools boundary is a symlink ({root})", _REFUSAL_UNTRUSTED
    resolved, reason = _resolve_executable(candidate)
    if resolved is None:
        return None, reason, _REFUSAL_UNTRUSTED
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        return None, f"the managed tools boundary could not be resolved ({exc})", _REFUSAL_UNTRUSTED
    if not _under(resolved, resolved_root):
        return (
            None,
            f"the entrypoint resolves outside the managed tools boundary {resolved_root}",
            _REFUSAL_UNTRUSTED,
        )
    rejection = _common_candidate_rejection(resolved)
    if rejection is not None:
        return None, rejection[0], _REFUSAL_UNTRUSTED
    return resolved, None, None


def _managed_leaf_unresolved_reason() -> str:
    """Why the managed leaf yields no launcher, worded for the install step.

    An entrypoint that is on disk but refused is a different failure from one
    that is absent: the refusal names the boundary, and an agent-writable root
    that contains the data home is a configuration the user can change.
    """
    resolved, refusal = _resolve_managed_cli()
    if resolved is not None or refusal is None:
        return f"{CLI_BIN} was not found in the managed tools leaf after a successful install"
    candidate, reason, _kind = refusal
    return redact_install_output(f"{CLI_BIN} was installed at {candidate} but refused: {reason}")


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


def bootstrap_tool_provenance(path: str) -> dict[str, object]:
    """Describe a resolved bootstrap ``npm``/``node``: where it lands, who can replace it.

    Returns ``{"path", "real_path", "writable_at", "own_toolchain",
    "shared_write"}``. ``writable_at`` is the first component of the
    executable's hierarchy the gateway user can write, from the SAME predicate
    the CLI resolver refuses on (:func:`_gateway_writable_component`), or ``""``
    when there is none.

    Who else can write that component is what separates the expected case from
    the alarming one. ``shared_write`` means its group or other write bit is set,
    so another account can replace the binary. ``own_toolchain`` means it is
    owned by the gateway's own account and carries no such bit -- which is how
    nvm, fnm, volta and mise lay out ``$HOME``, and how Homebrew's installer
    creates ``/opt/homebrew`` (``install -m 0755 -o $USER``; the ``g+rwx`` it
    applies to ``bin``/``Cellar`` sits below the prefix, which the walk reaches
    first): only this account, which already runs the gateway, can replace it.
    A legacy root-owned ``/usr/local`` over a ``g+rwx`` ``bin`` is
    ``shared_write``: every other ``admin`` account can write it.

    Reporting only: the bootstrap never refuses on this answer. ``writable_at``
    is ``None`` -- not checked -- on Windows, where ``os.access`` reads only the
    read-only attribute and not the ACL. A gateway running as root gets the
    resolver's own answer: every component is writable by it.
    """
    real = os.path.realpath(path)
    writable_at: str | None
    own_toolchain = False
    shared_write = False
    if platform_compat.IS_WINDOWS:
        writable_at = None
    else:
        component = _gateway_writable_component(Path(path), Path(real))
        writable_at = "" if component is None else str(component)
        if component is not None:
            try:
                st = component.stat()
            except OSError:
                st = None
            if st is not None:
                shared_write = bool(st.st_mode & (stat.S_IWGRP | stat.S_IWOTH))
                me = platform_compat.process_owner_uid(os.getpid())
                own_toolchain = not shared_write and me is not None and st.st_uid == me
    return {
        "path": path,
        "real_path": real,
        "writable_at": writable_at,
        "own_toolchain": own_toolchain,
        "shared_write": shared_write,
    }


def _system_candidate(candidate: Path) -> tuple[Path | None, str | None, str | None]:
    """Validate a fixed system candidate with the tailnet planted-binary floor.

    Returns ``(resolved, reason, kind)``. A system candidate refused as writable
    by the gateway user is the common user-owned machine install -- a Homebrew
    ``/opt/homebrew/bin`` or an npm-global ``/usr/local/bin`` launcher the user
    put there themselves -- so it is :data:`_REFUSAL_LEGACY` and earns the
    one-time managed reinstall copy. A ``~/.local`` hit inherits its
    :data:`_REFUSAL_LEGACY` from :func:`_common_candidate_rejection`; an
    agent-writable tree, an unverifiable boundary, and a candidate that will not
    resolve are :data:`_REFUSAL_UNTRUSTED`.
    """
    resolved, reason = _resolve_executable(candidate)
    if resolved is None:
        return None, reason, _REFUSAL_UNTRUSTED
    rejection = _common_candidate_rejection(resolved)
    if rejection is not None:
        return None, rejection[0], rejection[1]
    if writable := _gateway_writable_component(candidate, resolved):
        return (
            None,
            f"the executable hierarchy is writable by the gateway user at {writable}",
            _REFUSAL_LEGACY,
        )
    return resolved, None, None


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


def _resolve_managed_cli() -> tuple[Path | None, tuple[Path, str, str] | None]:
    """The first vetted managed entrypoint, and the first managed refusal seen.

    The refusal is ``(candidate, reason, kind)``; every managed-prefix refusal is
    an anomaly, so its ``kind`` is always :data:`_REFUSAL_UNTRUSTED`.
    """
    first_refusal: tuple[Path, str, str] | None = None
    for candidate in _managed_cli_candidates():
        if not os.path.lexists(candidate):
            continue
        resolved, reason, kind = _managed_candidate(candidate)
        if resolved is not None:
            return resolved, first_refusal
        if reason is not None and first_refusal is None:
            first_refusal = (candidate, reason, kind or _REFUSAL_UNTRUSTED)
    return None, first_refusal


def _resolve_system_cli() -> tuple[Path | None, tuple[Path, str, str] | None]:
    """The first vetted fixed-system entrypoint, and the first system refusal seen.

    The refusal is ``(candidate, reason, kind)``; a system candidate refused as
    writable by the gateway user is the common user-owned machine install, so its
    ``kind`` is :data:`_REFUSAL_LEGACY`.
    """
    first_refusal: tuple[Path, str, str] | None = None
    for candidate in _system_cli_candidates():
        if not os.path.lexists(candidate):
            continue
        resolved, reason, kind = _system_candidate(candidate)
        if resolved is not None:
            return resolved, first_refusal
        if reason is not None and first_refusal is None:
            first_refusal = (candidate, reason, kind or _REFUSAL_UNTRUSTED)
    return None, first_refusal


#: A launcher the user installed themselves outside the managed prefix -- a
#: ``~/.local/bin`` or version-manager binary on ``PATH``, or a machine install
#: such as Homebrew ``/opt/homebrew/bin`` or npm-global ``/usr/local/bin`` refused
#: as writable by the gateway user. The one-time managed reinstall is the right
#: remedy, so this is the refusal that earns the reinstall copy.
_REFUSAL_LEGACY = "legacy"

#: A candidate refused as an untrusted source a reinstall would not change -- a
#: managed-prefix anomaly, a candidate that will not resolve, or a planted shim in
#: an agent-writable tree or an unverifiable boundary. The plain refusal message
#: stands for this case.
_REFUSAL_UNTRUSTED = "untrusted"

#: Reason surfaced when no ``playwright-cli`` launcher is present at all. Shared by
#: every surface that reports the browser tools as unavailable, so they stay
#: identical.
CLI_NOT_INSTALLED_REASON = "playwright-cli is not installed"

#: Reason surfaced when a launcher IS present but was declined as a source the
#: user installed themselves outside the managed prefix -- a ``~/.local`` or
#: version-manager launcher on ``PATH``, or a machine install such as Homebrew or
#: npm-global. It explains that the one-time managed reinstall is needed, so the
#: user is not told "not installed" when they do have an install. Surfaced
#: verbatim, so it must stay plain and complete. Shown only when
#: :func:`legacy_launcher_refused` is true.
CLI_LEGACY_UNTRUSTED_REASON = (
    "playwright-cli needs a one-time reinstall. An existing install outside the "
    "managed location (for example on your PATH under ~/.local/bin, a version "
    "manager such as nvm, mise, or Volta, or a machine install under a Homebrew "
    "prefix or an npm-global prefix) is no longer run for safety; reinstall the "
    "browser tools from the dashboard to use the managed copy."
)


def _resolve_cli_candidate() -> tuple[Path | None, tuple[Path, str, str] | None]:
    """Resolve the trusted launcher and the first refused candidate, if any.

    Returns ``(resolved, first_refusal)``. ``resolved`` is the canonical trusted
    path or ``None``; ``first_refusal`` is ``(candidate, reason, kind)`` for the
    first candidate that existed on disk or ``PATH`` but was declined as an
    untrusted source, or ``None`` when no launcher spelling was present at all.
    The managed leaf is searched first, then the fixed system directories, then
    ``PATH`` -- the same order and the same :func:`_resolve_managed_cli` /
    :func:`_resolve_system_cli` scans :func:`cli_path` uses, so the two can never
    disagree about what resolves. ``kind`` is :data:`_REFUSAL_LEGACY` for a
    launcher the user installed themselves -- a ``PATH`` / ``~/.local`` hit, or a
    system candidate (Homebrew ``/opt/homebrew/bin``, npm-global
    ``/usr/local/bin``) refused as writable by the gateway user -- and
    :data:`_REFUSAL_UNTRUSTED` for every other refusal (a managed/system anomaly,
    or a planted shim in an agent-writable tree or an unverifiable boundary). Each
    helper returns its own typed kind, so the classification never depends on
    parsing a reason string. Detection is identity-only -- the same
    ``os.path.lexists`` / ``shutil.which`` probing and reason computation
    :func:`cli_path` performs -- so no launcher is executed, copied, or trusted
    here.
    """
    managed, first_refusal = _resolve_managed_cli()
    if managed is not None:
        return managed, first_refusal
    system, system_refusal = _resolve_system_cli()
    if first_refusal is None:
        first_refusal = system_refusal
    if system is not None:
        return system, first_refusal

    if first_refusal is None:
        found = shutil.which(CLI_BIN, path=os.environ.get("PATH", ""))
        if found:
            candidate = Path(found)
            resolved, reason = _resolve_executable(candidate)
            kind = _REFUSAL_LEGACY
            if resolved is not None:
                rejection = _common_candidate_rejection(resolved)
                candidate = resolved
                # A plain PATH hit outside every vetted tree is the user's own
                # install (legacy); the kind for a rejected one comes straight
                # from the typed classification -- ``~/.local`` legacy, an
                # agent-writable tree or unverifiable boundary untrusted.
                if rejection is not None:
                    reason, kind = rejection
            first_refusal = (
                candidate,
                reason or "the candidate came from PATH, which is not a trusted launcher source",
                kind,
            )
    return None, first_refusal


def cli_path() -> str | None:
    """Canonical trusted ``playwright-cli`` path, or ``None``.

    Resolution is absolute and ordered: the sandbox-sealed crew-home tools leaf
    first, then fixed machine-install directories. ``PATH`` and the legacy
    ``~/.local/bin/playwright-cli`` location are never execution sources. A PATH
    hit is inspected only after every vetted location misses, so the refusal can
    name the planted shim without ever running it.
    """
    resolved, first_refusal = _resolve_cli_candidate()
    if first_refusal is not None:
        candidate, reason, _kind = first_refusal
        _warn_cli_refusal(candidate, reason)
    return str(resolved) if resolved is not None else None


def legacy_launcher_refused() -> bool:
    """Whether a user-installed ``playwright-cli`` launcher is present but declined.

    ``True`` only when a launcher the user installed themselves outside the
    managed prefix -- a ``~/.local/bin`` or version-manager binary on ``PATH``, or
    a machine install (Homebrew ``/opt/homebrew/bin``, npm-global
    ``/usr/local/bin``) refused as writable by the gateway user -- exists yet is
    intentionally not run. That is the state a host is in after the managed-install
    migration, and the one a one-time managed reinstall fixes. The check is
    identity-only (:func:`_resolve_cli_candidate`): it never executes, copies, or
    trusts the detected launcher. ``False`` when a trusted launcher resolves, when
    nothing is installed at all, and when the only refused candidate is a planted
    shim in an agent-writable tree or an unverifiable boundary (which a reinstall
    does not address), so that case keeps the plain refusal message.
    """
    resolved, first_refusal = _resolve_cli_candidate()
    return resolved is None and first_refusal is not None and first_refusal[2] == _REFUSAL_LEGACY


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
    if rejection := _common_candidate_rejection(resolved):
        return None, rejection[0]
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
        resolved, reason, _kind = _system_candidate(candidate)
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
        node, reason, _node_kind = _managed_candidate(_managed_node_path())
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
    if rejection := _common_candidate_rejection(resolved):
        return None, rejection[0]
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
