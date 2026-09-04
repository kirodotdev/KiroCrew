"""Policy-only update provider seam for enterprise self-update.

Abstracts *how* Kiro Crew checks for and applies updates behind a single
operator-supplied :class:`CommandProvider`.

**Trust placement (security-critical).** A command provider runs unsandboxed
shell code AS THE GATEWAY. Its commands therefore live in exactly ONE place: the
keystone-protected ``security_policy.json`` (surfaced as ``UpdatePins`` via
:func:`~kiro_crew.platform.governance.active_update_pins`), which a
prompt-injected agent shell can neither read nor write. ``config.json`` and
environment variables are agent-writable / process-inherited, so they cannot
reach this seam at all — there is no config or env path into it.

**Selection is by PRESENCE, not by a mechanism name.** There is no mechanism
enum any more. :func:`resolve_provider` returns a :class:`CommandProvider` when
the policy pins define a check or apply command, and ``None`` otherwise (the
ungoverned default, where the gateway keeps its built-in update behaviour).
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import sys
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Protocol, runtime_checkable

from kiro_crew import platform_compat
from kiro_crew.platform_compat import (
    IS_POSIX,
    trusted_system_bin,
    trusted_system_path,
)

logger = logging.getLogger(__name__)


#: Ceiling on how much of an update command's output is kept. A version string
#: is tens of bytes and an error summary is logged truncated anyway, while a
#: chatty package manager can emit megabytes per package. ``communicate()``
#: buffers everything in the gateway's own memory with no bound, so a verbose
#: command could exhaust it before the timeout fires.
_MAX_CAPTURED_OUTPUT = 64 * 1024


async def _read_bounded_output(
    proc: asyncio.subprocess.Process, *, timeout: float, want_stdout: bool
) -> tuple[bytes, bytes]:
    """Wait for *proc*, keeping at most :data:`_MAX_CAPTURED_OUTPUT` per stream.

    Drains both pipes concurrently so a full pipe buffer cannot deadlock the
    child, but stops accumulating past the cap and discards the rest. ``apply``
    passes ``want_stdout=False``: its stdout is installer chatter nobody reads,
    and only a bounded stderr is kept for the log.
    """

    async def _drain(stream: asyncio.StreamReader | None, keep: bool) -> bytes:
        if stream is None:
            return b""
        buf = bytearray()
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                return bytes(buf)
            if keep and len(buf) < _MAX_CAPTURED_OUTPUT:
                buf.extend(chunk[: _MAX_CAPTURED_OUTPUT - len(buf)])

    out, err = await asyncio.wait_for(
        asyncio.gather(_drain(proc.stdout, want_stdout), _drain(proc.stderr, True)),
        timeout=timeout,
    )
    await proc.wait()
    return out, err


async def _kill_and_reap(proc: asyncio.subprocess.Process) -> None:
    """Delegate to :func:`kiro_crew.platform_compat.kill_and_reap`.

    Kept as a module-level seam rather than a bare re-export so the
    module-local ceiling below keeps bounding the reap: existing callers
    (including function-local imports elsewhere) and tests resolve both
    names on THIS module.
    """
    # Module-object attribute lookup happens at call time, so tests patching
    # ``kiro_crew.platform_compat.kill_process_tree_async`` (inside the shared
    # helper) still intercept the tree kill.
    await platform_compat.kill_and_reap(proc, timeout=_REAP_TIMEOUT_SECS)


#: Ceiling on waiting for a killed updater tree. A descendant that ignores the
#: signal must not turn cleanup into a hang while the gateway is shutting down.
#: Mirrors the shared default so the updater's bound stays independently
#: patchable without touching every other reap site.
_REAP_TIMEOUT_SECS = 10


@dataclass(frozen=True)
class UpdateCheckResult:
    """Result of an update availability check."""

    available: bool = False
    remote_version: str = ""
    error: str = ""


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class UpdateProvider(Protocol):
    """Documents the contract an operator-supplied provider must satisfy.

    This Protocol has exactly one implementation in-tree
    (:class:`CommandProvider`); it is kept purely as living documentation of the
    check/apply contract an enterprise's command provider is expected to honour.
    Implementations must be safe for concurrent use (the gateway may call
    ``check()`` from multiple coroutines on boot).
    """

    async def check(self) -> UpdateCheckResult:
        """Check whether an update is available.

        Returns :class:`UpdateCheckResult` with ``available=True`` when a newer
        version exists. On transient errors, ``error`` is set and ``available``
        is False — callers must not treat an errored check as "up to date".
        """
        ...

    async def apply(self) -> bool:
        """Apply the update. Returns True on success.

        On failure returns False (the existing install is left intact).
        The caller is responsible for restarting the process after a
        successful apply.
        """
        ...


# ---------------------------------------------------------------------------
# Platform helpers
# ---------------------------------------------------------------------------

# Canonical machine name mapping — covers the values platform.machine() returns
# on each OS+arch combination we support.
_MACHINE_ALIASES: dict[str, str] = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "x64": "x86_64",
    "aarch64": "arm64",
    "arm64": "arm64",
}


def _current_platform_key() -> str:
    """Return the normalized platform key for the running host.

    Format: ``{sys.platform}-{machine}`` where machine is one of
    ``x86_64`` or ``arm64``.  Examples: ``linux-x86_64``, ``darwin-arm64``,
    ``win32-x86_64``.

    Falls back to the raw ``platform.machine()`` value (lowercased) when the
    architecture is unrecognized, so an operator can still target exotic
    hardware by using the raw key in ``platform_commands``.
    """
    raw_machine = platform.machine().lower()
    normalized = _MACHINE_ALIASES.get(raw_machine, raw_machine)
    return f"{sys.platform}-{normalized}"


def _shell_exec_args(command: str) -> list[str] | None:
    """Return the argv for running *command* in the platform's shell, or None.

    POSIX: ``[<trusted sh>, "-c", command]``. Windows: ``None`` — see below.

    The shell binary is resolved through :func:`trusted_system_bin` (fixed
    system directories) rather than as a bare ``"sh"`` argv name, because a
    gateway's ``PATH`` can lead with an agent-writable directory (a worktree venv
    ``bin``, ``~/.local/bin``): a bare name would let a planted ``~/.local/bin/sh``
    shim run with the gateway's credentials.

    Returns ``None`` (fail CLOSED) when no trusted shell is found — the caller
    must treat that as "cannot run" rather than falling back to a bare name,
    because the bare-name fallback is exactly the agent-writable-PATH hole this
    resolution exists to close.

    **Windows is refused outright**, and that is a deliberate restriction rather
    than a gap: pinning the shell alone does not make the CHILD's lookup safe,
    and the second hop cannot be closed on Windows today.
    :func:`platform_compat.trusted_system_path` returns ``None`` there (Windows
    helpers live beside their install rather than on a search path), so
    :func:`_trusted_path_env` has no trusted ``PATH`` to substitute, and
    ``cmd.exe`` additionally resolves a bare command word from the working
    directory first. A command provider that runs unsandboxed code as the gateway
    must not rely on a lookup an agent can influence, so the Windows lane stays
    closed until it has a trusted lookup and a trusted working directory.
    """
    if sys.platform == "win32":
        return None
    shell = trusted_system_bin("sh")
    return [shell, "-c", command] if shell else None


def _trusted_path_env() -> dict[str, str] | None:
    """The gateway environment with ``PATH`` narrowed to trusted system dirs.

    Pinning the shell binary is not sufficient on its own: the shell then
    resolves the operator's own command words (``my-updater check``) through
    ``PATH``, and a gateway's ``PATH`` can lead with agent-writable directories
    (a worktree venv ``bin``, ``~/.local/bin``). Narrowing ``PATH`` for the child
    closes that second hop.

    Consequence for operators, and why it is the right default: a command whose
    binary does NOT live in a system directory must be written as an ABSOLUTE
    path in the policy (``/opt/acme/bin/acme-pkg update kirocrew``). An
    execution-authorizing command naming its binary absolutely is the posture we
    want anyway, since it also removes any ambiguity about which binary runs.

    Only ``PATH`` is replaced; the rest of the environment is left alone, so a
    command can still read the proxy, locale and credential-helper variables its
    package manager needs.

    Returns ``None`` (fail CLOSED) when there is no trusted ``PATH`` to
    substitute. Passing the inherited ``PATH`` through instead would leave the
    child's lookup agent-influenceable, which is the whole hole this closes.
    """
    trusted = trusted_system_path()
    if not trusted:
        return None
    env = dict(os.environ)
    env["PATH"] = trusted
    # Narrowing PATH is not enough on its own: the loader variables below make an
    # interpreter execute agent-writable code no matter which binary was resolved.
    # PYTHONPATH plus a planted ``sitecustomize.py`` runs on EVERY Python start,
    # and the LD_*/DYLD_* family does the same for any dynamically linked binary,
    # so an update command that happens to be a Python or shell wrapper would
    # execute that code as the gateway. Dropped rather than sanitised: an update
    # command has no business inheriting an interpreter search path.
    for var in _INJECTABLE_LOADER_VARS:
        env.pop(var, None)
    # Exported shell FUNCTIONS are the same threat carrying an open-ended name:
    # bash imports ``BASH_FUNC_<name>%%`` (4.3+) or ``BASH_FUNC_<name>()`` (the
    # patched-4.2 form), and the resulting function shadows that command word
    # outright -- beating the narrowed PATH above rather than competing in it.
    # Matched by PREFIX because the suffix is version-specific, so neither form
    # can be missed and no future one has to be enumerated.
    for var in [k for k in env if k.startswith("BASH_FUNC_")]:
        env.pop(var, None)
    return env


#: Environment variables that make an interpreter, a dynamic linker, or the
#: shell itself load code from a caller-controlled path. Removed from every
#: update-command child. ``SHELLOPTS``/``PS4`` are the shell's own pair: the
#: command runs via ``[<sh>, "-c", ...]`` and where that ``sh`` is bash,
#: ``SHELLOPTS=xtrace`` enables tracing while ``PS4`` is expanded with command
#: substitution, so a payload there runs before the command does.
_INJECTABLE_LOADER_VARS: tuple[str, ...] = (
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "PYTHONEXECUTABLE",
    "PYTHONUSERBASE",
    "PYTHONWARNINGS",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "LD_AUDIT",
    "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH",
    "DYLD_FRAMEWORK_PATH",
    "BASH_ENV",
    "ENV",
    "SHELLOPTS",
    "PS4",
    "IFS",
)


# ---------------------------------------------------------------------------
# Command provider (the one operator-supplied implementation)
# ---------------------------------------------------------------------------


@dataclass
class CommandProvider:
    """Runs operator-configured shell commands for check and apply.

    Security: commands come ONLY from the keystone-protected
    ``security_policy.json`` (via ``UpdatePins``), never from environment
    variables, config.json, or feed data. The operator who controls that file
    already has full host access.

    ``check_command`` must exit 0 and print the available version to stdout
    when an update is available, or exit non-zero when up to date.

    ``apply_command`` must exit 0 on success; a non-zero exit means the apply
    failed and the existing install is intact.

    **Platform-aware commands.** The top-level ``check_command``/``apply_command``
    are the default for all platforms. ``platform_commands`` allows per-platform
    overrides keyed by ``{sys.platform}-{machine}`` (e.g. ``linux-x86_64``,
    ``darwin-arm64``, ``win32-x86_64``). When the current platform key matches
    an entry, its ``check_command``/``apply_command`` values override the
    top-level defaults for that field only.

    On POSIX systems commands run via a trusted ``sh -c`` with a trusted-only
    ``PATH``. Windows is not supported yet and both verbs refuse there: the
    child's command lookup cannot be made trustworthy on Windows today (see
    :func:`_shell_exec_args`), so ``win32-*`` keys in ``platform_commands`` are
    accepted by the schema but never reached.
    """

    check_command: str = ""
    apply_command: str = ""
    platform_commands: dict[str, dict[str, str]] = dataclass_field(default_factory=dict)

    def _resolve_command(self, field: str) -> str:
        """Resolve the effective command for *field* on this platform.

        Checks ``platform_commands[current_key][field]`` first, then falls back
        to the top-level attribute.
        """
        key = _current_platform_key()
        overrides = self.platform_commands.get(key, {})
        if overrides and overrides.get(field):
            return overrides[field]
        return getattr(self, field, "")

    def can_apply(self) -> bool:
        """True when an ``apply_command`` is configured AND can run here.

        The dashboard's check path uses this to decide whether to offer an
        Update button at all: a provider configured with only a
        ``check_command`` can report availability but cannot act, and a button
        that can only fail would contradict the honesty contract the check
        cache carries. The runnability half matters on Windows, where
        :func:`_shell_exec_args` refuses every command — a configured
        ``apply_command`` there must not render a button whose only possible
        outcome is ``policy_update_failed``.

        NOTE: True means the PROVIDER can apply. It does NOT imply a git
        checkout — callers that git-reset must gate on
        :func:`resolve_provider` first, as both existing callers do.
        """
        cmd = self._resolve_command("apply_command")
        return bool(cmd) and _shell_exec_args(cmd) is not None

    async def check(self) -> UpdateCheckResult:
        """Run check_command. Exit 0 + non-empty stdout version = available."""
        cmd = self._resolve_command("check_command")
        if not cmd:
            return UpdateCheckResult(error="no check_command configured")

        argv = _shell_exec_args(cmd)
        if argv is None:
            return UpdateCheckResult(error="no trusted shell found")
        env = _trusted_path_env()
        if env is None:
            return UpdateCheckResult(error="no trusted PATH for the update command")

        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                # Own session so the whole pipeline is one killable group and
                # cannot signal back into the gateway's group.
                start_new_session=IS_POSIX,
                # Root, not the gateway's cwd: a relative command word
                # (``./update.sh``) would otherwise resolve inside whatever
                # directory the gateway was launched from, which can be an
                # agent-writable checkout. Operator commands name absolute paths.
                cwd="/",
            )
            stdout, _stderr = await _read_bounded_output(proc, timeout=60, want_stdout=True)
        except asyncio.CancelledError:
            if proc is not None:
                await _kill_and_reap(proc)
            raise
        except asyncio.TimeoutError:
            if proc is not None:
                await _kill_and_reap(proc)
            return UpdateCheckResult(error="check_command timed out")
        except OSError:
            # OSError, not just FileNotFoundError: fd or process exhaustion
            # raises a different OSError, and a manual update must get an error
            # verdict back rather than an exception escaping the provider.
            return UpdateCheckResult(error="could not start check_command")

        if proc.returncode != 0:
            # Non-zero = no update available (not an error)
            return UpdateCheckResult(available=False)

        version = (stdout or b"").decode(errors="replace").strip()
        # An exit-0 check that prints NO version is a broken command, not an
        # available update. Returning available=True with remote_version=''
        # would make apply() run and the gateway restart to the SAME version,
        # forever — an infinite update-restart loop. Fail the check instead.
        if not version:
            return UpdateCheckResult(error="check_command produced no version")
        # Sanitize: version should be a short string, no shell metacharacters
        if len(version) > 128:
            version = version[:128]
        return UpdateCheckResult(available=True, remote_version=version)

    async def apply(self) -> bool:
        """Run apply_command. Exit 0 = success."""
        cmd = self._resolve_command("apply_command")
        if not cmd:
            logger.warning("CommandProvider.apply: no apply_command configured")
            return False

        argv = _shell_exec_args(cmd)
        if argv is None:
            logger.error("CommandProvider.apply: no trusted shell found — refusing to run")
            return False
        env = _trusted_path_env()
        if env is None:
            logger.error("CommandProvider.apply: no trusted PATH — refusing to run")
            return False

        logger.info("CommandProvider.apply: running apply command")
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                # Own session so the whole pipeline is one killable group and
                # cannot signal back into the gateway's group.
                start_new_session=IS_POSIX,
                # Root, not the gateway's cwd: a relative command word
                # (``./update.sh``) would otherwise resolve inside whatever
                # directory the gateway was launched from, which can be an
                # agent-writable checkout. Operator commands name absolute paths.
                cwd="/",
            )
            _stdout, stderr = await _read_bounded_output(proc, timeout=600, want_stdout=False)
        except asyncio.CancelledError:
            if proc is not None:
                await _kill_and_reap(proc)
            logger.warning("CommandProvider.apply: cancelled — update child killed")
            raise
        except asyncio.TimeoutError:
            if proc is not None:
                await _kill_and_reap(proc)
            logger.error("CommandProvider.apply: timed out (10 min)")
            return False
        except OSError:
            # OSError, not just FileNotFoundError (see check() above).
            logger.exception("CommandProvider.apply: could not start the command")
            return False

        if proc.returncode != 0:
            # Redact credentials AND token-bearing URLs before logging stderr,
            # so neither an inline token nor a presigned/token URL enters the
            # persistent ring buffer or the /api/logs dashboard stream.
            #
            # Through the CONTEXT, not `security.redact` directly: an installer
            # error is prime territory for a host-specific credential shape (an
            # internal registry cookie, an SSO token in a fetch URL), and those
            # live in a loaded companion's regexes rather than in the OSS
            # baseline. Reading the baseline here would scan a companion host's
            # stderr with the weaker pass and log what it missed. The `_log_`
            # spelling is the one that cannot raise -- see its docstring.
            from kiro_crew.platform.context import redact_log_via_context

            # Redact BEFORE truncating. Slicing first can cut a credential in
            # half, and half a token no longer matches the redactors' patterns
            # (an AWS key needs its full 20 chars to match), so the surviving
            # fragment would reach gateway.log and /api/logs verbatim. The
            # 500-char cap is for log volume, so it belongs last.
            err_text = redact_log_via_context((stderr or b"").decode(errors="replace"))
            err_text = err_text[:500]
            logger.error(
                "CommandProvider.apply: failed (rc=%d): %s",
                proc.returncode,
                err_text,
            )
            return False

        logger.info("CommandProvider.apply: succeeded")
        return True


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


async def apply_policy_update() -> bool | None:
    """Run the policy-defined update, or ``None`` when no provider is configured.

    The single seam every MANUAL update entry point shares with the unattended
    boot check. Without it, `POST /api/update` and `kirocrew update` would run
    the built-in git/CDN update on a host whose administrator selected a
    different package manager, which is exactly the bypass this policy exists to
    prevent -- and a bypass an authenticated operator could trigger by accident.

    ``None`` means "no policy provider, carry on with the built-in path".
    ``True``/``False`` is the provider's own apply verdict, and the caller must
    NOT fall back to the built-in path on ``False``: a configured provider owns
    the update, and its failure is a failure, not a reason to run the mechanism
    the operator excluded.
    """
    provider = resolve_provider()
    if provider is None:
        return None
    return await provider.apply()


def resolve_provider() -> CommandProvider | None:
    """Resolve the operator-supplied command provider, or ``None``.

    **Trust placement (security-critical).** A command provider executes
    unsandboxed shell code AS THE GATEWAY, so its commands are read ONLY from the
    keystone-protected ``security_policy.json`` (via ``UpdatePins``), which a
    prompt-injected agent shell cannot write. There is no ``config.json`` or
    environment path into this seam.

    **Presence is the selection.** When the active pins define a ``check_command``
    or an ``apply_command`` (top-level or per-platform), a
    :class:`CommandProvider` is returned; otherwise ``None`` — the ungoverned
    default, where the gateway keeps its built-in update behaviour.
    """
    try:
        from kiro_crew.platform.governance import active_update_pins

        pins = active_update_pins()
    except Exception:
        logger.debug("Reading update pins from policy failed", exc_info=True)
        return None

    check_command = getattr(pins, "check_command", "") or ""
    apply_command = getattr(pins, "apply_command", "") or ""
    platform_commands = {
        k: dict(v) for k, v in (getattr(pins, "platform_commands", {}) or {}).items()
    }
    # A policy may define commands ONLY per platform (an operator whose package
    # manager exists on some hosts and not others, with no sensible default).
    # Ignoring that shape here would silently fall through to the built-in
    # updater and bypass the administrator-selected package manager, so any
    # per-platform command counts as presence too. Whether the CURRENT platform
    # has one is CommandProvider's decision, and it refuses when it does not.
    has_platform_command = any(
        entry.get("check_command") or entry.get("apply_command")
        for entry in platform_commands.values()
    )
    if check_command or apply_command or has_platform_command:
        return CommandProvider(
            check_command=check_command,
            apply_command=apply_command,
            platform_commands=platform_commands,
        )
    return None


__all__ = [
    "UpdateCheckResult",
    "UpdateProvider",
    "CommandProvider",
    "apply_policy_update",
    "resolve_provider",
    "_current_platform_key",
    "_kill_and_reap",
    "_read_bounded_output",
    "_shell_exec_args",
    "_trusted_path_env",
]
