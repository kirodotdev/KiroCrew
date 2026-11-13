"""Which kiro-cli the desktop app ships, and the sign-in command that runs it.

A stdlib-only leaf. :mod:`kiro_crew.agent_sdk.host_auth` names the kiro-cli
sign-in command in its remedy text, and that table is read while the credential
floor is being built, so it can import nothing heavier than this. Everything here
is pure string work on an environment mapping -- no ``which``, no stat -- so a
caller on the event loop may use any of it.

:mod:`kiro_crew.kiro_cli` and :mod:`kiro_crew.kiro_prerequisite_status` re-export
these names; the setup gate and the sign-in remedy therefore compose the command
from one source and cannot drift apart.
"""

from __future__ import annotations

import os
import shlex
import sys
from collections.abc import Mapping

#: The directory holding the kiro-cli copy the desktop app ships inside its own
#: resources. The Electron shell sets it at gateway spawn
#: (``website/electron/gateway-env.js``) only when the payload actually shipped;
#: ``packaging/build-desktop.sh`` stages it. Set by the process that starts the
#: gateway, like ``KIROCREW_KIRO_BIN``, never by a directory an agent can plant
#: a file in -- which is what lets ``kiro_cli.pin_kiro_cli`` trust it while it
#: still refuses the inherited ``PATH``. A directory rather than a binary because
#: the Electron side only stats the directory it staged; the name of the entry
#: binary inside it lives on this side, in :func:`bundled_kiro_cli_entry`.
BUNDLED_KIRO_DIR_ENV = "KIROCREW_BUNDLED_KIRO_DIR"

#: The operator's explicit kiro-cli binary. Ranked ahead of the bundled copy by
#: ``kiro_cli.find_kiro_cli_candidates``.
KIRO_BIN_OVERRIDE_ENV = "KIROCREW_KIRO_BIN"

#: The executable the bundled directory is entered through: the chat binary,
#: not the ``kiro-cli`` launcher. The launcher resolves ``kiro-cli-chat`` via
#: ``$HOME/.local/bin`` and ``PATH`` and never looks in its own directory, so a
#: bundle entered through it would silently run whatever copy the USER has
#: installed and, on a clean machine, fail with "failed to launch
#: kiro-cli-chat" -- while the launcher answers ``--version`` and ``whoami``
#: itself, so those probes pass. ``kiro-cli-chat`` is the process every session
#: is anyway, accepts every subcommand used here (``acp``, ``login``, ``whoami``,
#: ``mcp``, ``agent``, ``settings``, ``--version``) and finds its own helpers
#: through its own executable path, so it is self-contained.
BUNDLED_KIRO_CLI_ENTRY = "kiro-cli-chat"
BUNDLED_KIRO_CLI_WINDOWS_ENTRY = "kiro-cli.exe"

# The exact command the user runs to sign in. A CODE CONSTANT, never a catalog
# value: a translated command cannot be typed or executed (see
# website/docs/i18n-catalog.md — "A literal token the user must type must never be
# a catalog value"). Served in the status payload so the UI has one source of
# truth for it rather than hardcoding a second copy that can drift.
# The argument tails are split out so login_commands() can compose the
# bundled-copy form (absolute path + same tail) without duplicating flag
# literals that would drift. They carry the leading space DELIBERATELY: the
# read-only-probe tripwire in test_kiro_prerequisite_handlers.py forbids the standalone
# quoted token a spawn argv element would have, and these are display strings
# the user types, never argv.
_LOGIN_TAIL = " login"
_SSO_LOGIN_TAIL = " login --use-device-flow --license pro"
KIRO_CLI_LOGIN_COMMAND = f"kiro-cli{_LOGIN_TAIL}"
# The organization-SSO counterpart, served alongside the bare command so the gate
# can offer both instead of one ambiguous line. Both flags are load-bearing:
# ``--use-device-flow`` is what makes the others take effect at all (kiro-cli
# discards every login flag unless the environment is remote OR that flag is set,
# falling back to a browser portal), and ``--license pro`` then selects Identity
# Center directly. The pair cannot yield a Builder ID session, which is the
# failure this exists to prevent: on the portal, a free Builder ID sits as a
# visual peer of organization SSO, so a user on an SSO plan can sign in to the
# wrong tier and only discover it when models are missing.
KIRO_CLI_SSO_LOGIN_COMMAND = f"kiro-cli{_SSO_LOGIN_TAIL}"


def bundled_kiro_cli_entry(platform_name: str) -> str:
    """Return the executable staged for *platform_name*.

    Windows' MSI carries one self-contained ``kiro-cli.exe``. POSIX releases
    stage ``kiro-cli-chat`` directly because their launcher does not resolve a
    sibling chat binary.
    """
    if platform_name == "win32":
        return BUNDLED_KIRO_CLI_WINDOWS_ENTRY
    return BUNDLED_KIRO_CLI_ENTRY


def bundled_kiro_cli_dir(environ: Mapping[str, str]) -> str | None:
    """The desktop app's bundled kiro-cli directory, or ``None`` when not bundled.

    Whitespace-only and unset both read as "not bundled": a blank value is what
    an unbundled build leaves behind, never a directory to search.
    """
    bundled = environ.get(BUNDLED_KIRO_DIR_ENV, "").strip()
    return bundled or None


def is_bundled_kiro_cli(binary: str, environ: Mapping[str, str]) -> bool:
    """Whether *binary* is the desktop app's own bundled kiro-cli.

    True only when a bundled directory is declared AND the binary's directory
    is that directory, compared as normalised strings. The resolver builds the
    bundled candidate from the same directory string
    (``kiro_cli.find_kiro_cli_candidates``), so a textual compare is exact for
    anything it produced; a symlinked ``KIROCREW_KIRO_BIN`` override that
    points into the bundle reads as "not bundled", which keeps the operator's
    binary on its normal update path. Pure string work -- no ``resolve()``, no
    stat -- so it is safe to call on the event loop.

    Provenance callers key off this: the setup gate serves the bundled copy's
    absolute path as the sign-in command (it is not on the user's shell PATH),
    and the self-update paths skip it (it lives inside a signed, read-only app
    bundle and is replaced by the next app update, never in place).
    """
    bundled_dir = bundled_kiro_cli_dir(environ)
    if not bundled_dir or not binary:
        return False
    return os.path.normpath(os.path.dirname(binary)) == os.path.normpath(bundled_dir)


def _is_off_path_override(binary: str, environ: Mapping[str, str]) -> bool:
    """Whether *binary* came from ``KIROCREW_KIRO_BIN`` and sits in no ``PATH``
    directory, so a bare ``kiro-cli`` typed in the user's shell would not run it.

    Pure string work, like :func:`is_bundled_kiro_cli`: the callers run on the
    event loop, so no ``which``/stat may happen here. A directory match is the
    whole test -- an override named ``kiro-cli-chat`` inside a ``PATH`` dir is
    left bare, which is the pre-existing behaviour for every ``PATH`` install.
    """
    override = environ.get(KIRO_BIN_OVERRIDE_ENV, "")
    if not override or not binary:
        return False
    if os.path.normpath(override) != os.path.normpath(binary):
        return False
    binary_dir = os.path.normpath(os.path.dirname(binary))
    path_dirs = {
        os.path.normpath(entry) for entry in environ.get("PATH", "").split(os.pathsep) if entry
    }
    return binary_dir not in path_dirs


def login_commands(
    binary: str,
    environ: Mapping[str, str],
    *,
    windows: bool,
) -> tuple[str, str, bool]:
    """Sign-in commands for *binary*, plus whether it is the bundled copy.

    The desktop app's bundled kiro-cli (``KIROCREW_BUNDLED_KIRO_DIR``) is not
    on the user's shell PATH, so when it is the copy that resolved, the commands
    must carry the binary's absolute path — otherwise the instruction fails with
    command-not-found on a fresh machine. POSIX paths use shell quoting. Windows
    commands name PowerShell explicitly, which also lets the same copied command
    run from cmd.exe. An operator's ``KIROCREW_KIRO_BIN`` override gets the same
    treatment when its resolved file is not what the user's shell would run as
    ``kiro-cli``: the bare command would then fail or sign in a different
    install. Any other resolution keeps the bare constants: the binary is on
    PATH by construction there.
    """
    bundled = is_bundled_kiro_cli(binary, environ)
    if bundled or _is_off_path_override(binary, environ):
        if windows:
            # A quoted executable path alone is data in PowerShell; its call
            # operator is required. Wrapping the command in powershell.exe keeps
            # this one display string runnable from both PowerShell and cmd.exe.
            # The user's terminal is outside the gateway's process tree, so the
            # KIRO_NO_AUTO_UPDATE the Electron shell sets does not reach this
            # run; upstream compiles the startup self-update only for Windows,
            # and without the switch a fresh sign-in would rewrite the sealed
            # bundled exe. ``Set-Item`` rather than ``$env:`` because an outer
            # interactive PowerShell interpolates ``$env:`` inside the double
            # quotes before powershell.exe ever sees it.
            quoted = binary.replace("'", "''")
            prefix = (
                'powershell.exe -NoProfile -Command "Set-Item Env:KIRO_NO_AUTO_UPDATE 1; '
                f"& '{quoted}'"
            )
            suffix = '"'
        else:
            prefix = shlex.quote(binary)
            suffix = ""
        return f"{prefix}{_LOGIN_TAIL}{suffix}", f"{prefix}{_SSO_LOGIN_TAIL}{suffix}", bundled
    return KIRO_CLI_LOGIN_COMMAND, KIRO_CLI_SSO_LOGIN_COMMAND, False


def spawn_kiro_cli_binary(environ: Mapping[str, str], platform_name: str) -> str:
    """The kiro-cli a spawn picks before any install directory, or ``""``.

    The two candidates ``kiro_cli.find_kiro_cli_candidates`` ranks first, both
    set by the process that started the gateway: the operator's
    ``KIROCREW_KIRO_BIN``, then the desktop app's bundled entry. ``""`` means the
    spawn falls through to an install the user's shell finds as ``kiro-cli``.
    Read from the environment alone, with no stat, so it can name the binary in
    a message raised on the event loop. The Electron shell sets the bundled
    directory only when it staged the payload, which is why naming the entry
    without checking it exists matches what the spawn resolved.
    """
    override = environ.get(KIRO_BIN_OVERRIDE_ENV, "")
    if override:
        return override
    bundled_dir = bundled_kiro_cli_dir(environ)
    if bundled_dir:
        return os.path.join(bundled_dir, bundled_kiro_cli_entry(platform_name))
    return ""


def spawn_login_command(
    environ: Mapping[str, str] | None = None,
    platform_name: str | None = None,
) -> str:
    """The sign-in command for the kiro-cli a spawn here runs.

    :data:`KIRO_CLI_LOGIN_COMMAND` unless :func:`spawn_kiro_cli_binary` names the
    desktop app's bundled copy, in which case it is that copy's absolute-path
    form from :func:`login_commands`.

    An operator's ``KIROCREW_KIRO_BIN`` keeps the bare command. The spawn keeps
    an override only when it is an executable file and otherwise falls through
    to an install directory or the bundled copy; a stat-free read cannot tell
    which of them ran, so naming the override could point at a removed binary.
    """
    resolved_environ = os.environ if environ is None else environ
    resolved_platform = sys.platform if platform_name is None else platform_name
    if resolved_environ.get(KIRO_BIN_OVERRIDE_ENV, ""):
        return KIRO_CLI_LOGIN_COMMAND
    binary = spawn_kiro_cli_binary(resolved_environ, resolved_platform)
    if not binary:
        return KIRO_CLI_LOGIN_COMMAND
    login, _sso, _bundled = login_commands(
        binary, resolved_environ, windows=resolved_platform == "win32"
    )
    return login


__all__ = [
    "BUNDLED_KIRO_CLI_ENTRY",
    "BUNDLED_KIRO_CLI_WINDOWS_ENTRY",
    "BUNDLED_KIRO_DIR_ENV",
    "KIRO_BIN_OVERRIDE_ENV",
    "KIRO_CLI_LOGIN_COMMAND",
    "KIRO_CLI_SSO_LOGIN_COMMAND",
    "bundled_kiro_cli_dir",
    "bundled_kiro_cli_entry",
    "is_bundled_kiro_cli",
    "login_commands",
    "spawn_kiro_cli_binary",
    "spawn_login_command",
]
