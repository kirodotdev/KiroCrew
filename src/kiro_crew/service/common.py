"""Platform detection and shared service constants."""

from __future__ import annotations

import enum
import os
import shlex
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from kiro_crew.config import loader
from kiro_crew.config.paths import CONFIG_DIR_NAME

SERVICE_NAME = "kirocrew"  # systemd unit name (without .service)
LAUNCHD_LABEL = "dev.kirocrew.gateway"  # launchd Label


# The NAME of the environment variable kiro-cli reads its model credential
# from — not the credential. This holds a variable name, is safe to print, and
# appears verbatim in operator-facing output. Keep key/secret/token/credential
# words OUT of the identifier: taint analysis classifies sources by identifier
# name, so a credential-sounding name here marks every string this flows into as
# a cleartext credential and flags the operator message as a disclosure.
_AUTH_ENV_VAR = "KIRO_API_KEY"


def systemd_quote(value: str) -> str:
    """Double-quote a value for a systemd unit token.

    systemd splits unquoted ``ExecStart`` / ``Environment=`` tokens on
    whitespace, so paths and environment values containing spaces must be
    quoted.  Percent signs are doubled because systemd performs specifier
    expansion even inside quotes, and backslashes / quotes use C-style escapes.

    Control characters are rejected rather than escaped: a newline would end
    the physical value and let the remainder be parsed as fresh unit directives.
    """
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError(
            "refusing to render a systemd unit value containing a control "
            "character (possible unit-file injection): " + repr(value)
        )
    escaped = value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def session_runtime_dir() -> str:
    """The per-user runtime directory ``systemctl --user`` resolves against.

    ``XDG_RUNTIME_DIR`` when the caller has one, else systemd's conventional
    ``/run/user/<uid>``. ``os.getuid`` is absent on Windows; every caller is
    Linux-only at runtime, but the ``getattr`` keeps this module importable there.
    """
    explicit = os.environ.get("XDG_RUNTIME_DIR")
    if explicit:
        return explicit
    uid = getattr(os, "getuid", lambda: -1)()
    return f"/run/user/{uid}"


def systemctl_user_env() -> dict[str, str]:
    """Environment for ``systemctl --user``, with the session-bus pointers
    backfilled when absent.

    ``systemctl --user`` finds the per-user instance through ``XDG_RUNTIME_DIR``
    + ``DBUS_SESSION_BUS_ADDRESS``. A gateway started by a systemd SYSTEM unit
    inherits neither, so a bare ``systemctl --user`` fails with "Failed to
    connect to bus" even while the bus and the unit are up. Every
    ``systemctl --user`` this codebase spawns reads a bus failure as a verdict
    about the host, so each one resolves its environment here.

    Only ever ADDS: an explicitly-set value wins. The bus is named only when its
    socket exists, so a stopped ``systemd --user`` gets systemctl's own diagnostic.
    """
    env = {**os.environ}
    runtime_dir = session_runtime_dir()
    if not env.get("DBUS_SESSION_BUS_ADDRESS"):
        sock = os.path.join(runtime_dir, "bus")
        if os.path.exists(sock):
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={sock}"
    env.setdefault("XDG_RUNTIME_DIR", runtime_dir)
    return env


def launchd_live_program() -> "os.PathLike[str]":
    """Stable path the launchd agent's ``ProgramArguments[0]`` points at.

    The indirection lets the running gateway be repointed at another checkout
    (Dev Fleet's "Make live") without rewriting the plist. launchd re-reads a
    plist only on ``bootout`` + ``bootstrap``, and ``bootout`` kills the process
    that would run the ``bootstrap``; rewriting THIS file plus a restart avoids that.

    It is a launcher SCRIPT, not a symlink, because a cutover must also move the
    working directory and put the target venv first on ``PATH`` (as the systemd
    drop-in does); a symlink would leave PATH-resolved subprocesses on the OLD install.
    """
    return (
        Path.home() / "Library" / "Application Support" / "KiroCrew"
        / "live-gateway"
    )


def kirocrew_bin() -> str:
    """Return the resolved kirocrew executable path, or fall back to sys.argv[0].

    Used by both the systemd unit and the launchd plist as ``ExecStart`` /
    ``ProgramArguments``. Resolution order:

    1. ``KIROCREW_SERVICE_BIN`` if set — an operator override for a launcher
       the resolver can't discover (a wrapper script, an off-PATH venv entry
       point). Made absolute: the service manager has no meaningful working
       directory, so a relative path would fail to start.
    2. The ``kirocrew`` console script beside the running interpreter.
       ``~/.local/bin/kirocrew`` is a shared name another install may repoint
       later, which would switch the service's install on its next restart.
       Skipped inside a linked git worktree or an AppImage runtime mount, which
       are reaped out from under a persisted path.
    3. ``shutil.which("kirocrew")`` — the installed console script, for an
       install whose console script does not sit beside its interpreter.
    4. ``sys.argv[0]`` — for development installs where ``kirocrew`` isn't on
       the global PATH.
    """
    override = os.environ.get("KIROCREW_SERVICE_BIN", "").strip()
    if override:
        return os.path.abspath(override)
    # No automatic source may name a versioned tree in a persisted ExecStart:
    # a later update's prune can remove it after this process exits.
    from kiro_crew.platform.tree_liveness import through_stable_link

    running = _running_install_bin()
    if running is not None:
        return through_stable_link(running)
    found = shutil.which("kirocrew")
    if found:
        return through_stable_link(found)
    return through_stable_link(os.path.realpath(sys.argv[0]))


def _running_install_bin() -> str | None:
    """The console script beside ``sys.executable``, or ``None``.

    The interpreter's ``bin/`` directory is used as-is, not resolved: a venv's
    ``bin/python3`` is a symlink to the base interpreter, and resolving it
    would leave the venv entirely.
    """
    from kiro_crew.config.paths import _in_ephemeral_tree, _in_linked_git_worktree

    sibling = Path(sys.executable).parent / (
        "kirocrew.exe" if os.name == "nt" else "kirocrew"
    )
    try:
        if not (sibling.is_file() and os.access(sibling, os.X_OK)):
            return None
        resolved = sibling.resolve()
    except OSError:
        return None
    if _in_linked_git_worktree(resolved) or _in_ephemeral_tree(resolved):
        return None
    return str(sibling)


def service_environment(home: str) -> "dict[str, str]":
    """Build the environment baked into the installed service.

    launchd and systemd start a service with a minimal, non-login environment,
    so everything the gateway relies on is captured at install time. One source
    for the plist ``EnvironmentVariables`` and the unit ``Environment=`` lines,
    so the two managers cannot drift.

    Keys:

    * ``HOME`` / ``PATH`` — always set; ``PATH`` per :func:`service_path`.
    * ``LANG`` / ``LC_ALL`` — a fixed UTF-8 locale, so a subprocess reading a
      non-ASCII file does not fail under ``US-ASCII`` (launchd sets no locale).
      Not the installer's own: a ``LANG=C`` install would bake a non-UTF-8
      ``LC_ALL`` that also blocks PEP 538 coercion, and a UTF-8-named installer
      locale may not exist on the host. ``C.UTF-8`` on Linux; macOS BSD libc
      has no ``C.UTF-8``, so Darwin uses ``en_US.UTF-8``.
    * ``KIROCREW_KIRO_BIN`` — only when the installer sets it, made absolute.
      The readiness ``whoami`` probe's real-home fallback keys off it; dropping
      it would leave the gateway not signed in.
    * ``KIROCREW_NODE_BIN_DIR``: only when the installer sets it to one
      absolute dir. It puts that Node ahead of nvm's on the MCP search path
      (:mod:`kiro_crew.env_node_pins`); a relative value or one carrying a
      ``:`` would add PATH entries nobody named, so it is dropped.
    """
    utf8_locale = "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"
    env = {
        "HOME": home,
        "PATH": service_path(home),
        "LANG": utf8_locale,
        "LC_ALL": utf8_locale,
        # Cross-platform marker for runtime policies that differ in a managed
        # background service. Older definitions without it are diagnosed by
        # ``kirocrew doctor`` and regenerated with ``kirocrew service install``.
        "KIROCREW_SERVICE_MANAGED": "1",
    }
    kiro_bin = os.environ.get("KIROCREW_KIRO_BIN", "").strip()
    if kiro_bin:
        env["KIROCREW_KIRO_BIN"] = os.path.abspath(kiro_bin)
    # Deferred for the reason _mise_shims_dir gives; the one check the MCP search
    # path applies to the same variable, so the two cannot drift apart.
    from kiro_crew.env import _validated_bin_dir

    node_bin = _validated_bin_dir(os.environ.get("KIROCREW_NODE_BIN_DIR", ""))
    if node_bin:
        env["KIROCREW_NODE_BIN_DIR"] = node_bin
    # KIROCREW_PORT is the only input DASHBOARD_PORT reads, so without it the
    # service can only bind the default 5476. Captured from the installer:
    # `KIROCREW_PORT=5477 kirocrew service install` bakes 5477 into the unit.
    # Not validated here: `cli.py`'s main() already rejects a bad value before
    # any subcommand runs, and a second check would drift from it. Rejecting
    # (not dropping) matters: a dropped value would install the default port.
    port = os.environ.get("KIROCREW_PORT", "").strip()
    if port:
        env["KIROCREW_PORT"] = port
    return env


def service_dotenv(
    environ: "Mapping[str, str] | None" = None, plat: "Platform | None" = None
) -> "tuple[Path, str]":
    """The ``.env`` the installed service reads, and the account to edit it as.

    ``sudo kirocrew service install`` on systemd runs the unit as ``SUDO_USER``
    with that account's passwd home (``linux_unit._current_user`` and
    ``_home_for_user``), while this process's home is root's under ``sudo -H``,
    so :func:`loader.env_path` would name a file the service never reads. In
    every other case it is that file and the account is ``""``: no ``sudo``,
    launchd (whose plist bakes this process's home), or ``KIROCREW_HOME`` set
    (its own caveat applies).
    """
    source = os.environ if environ is None else environ
    account = source.get("SUDO_USER", "").strip()
    if account in ("", "root") or _home_override_is_set(environ):
        return loader.env_path(), ""
    if (plat or current_platform()) is not Platform.SYSTEMD:
        return loader.env_path(), ""
    from kiro_crew.service import linux  # circular import: linux imports this module at load

    return Path(linux._home_for_user(account), CONFIG_DIR_NAME, ".env"), account


def api_key_will_be_dropped(environ: "Mapping[str, str] | None" = None) -> bool:
    """Return whether an API-key credential is set but invisible to the service.

    Deliberately the ONLY function that touches the credential, and it returns a
    bool rather than any string built from it. The message is assembled
    separately in :func:`headless_auth_warning` from a constant name and a path,
    so no value read here can reach a print, a log, or a return value — the
    no-disclosure property is structural rather than something a reader has to
    verify by following the formatting.

    True when the installer's environment defines a non-blank ``KIRO_API_KEY``
    that ``.env`` does not already carry. Only the presence of the NAME is
    inspected in ``.env``; its value is never read.
    """
    source = os.environ if environ is None else environ
    if not source.get(_AUTH_ENV_VAR, "").strip():
        return False
    try:
        names = _names_defined_in_env_file(service_dotenv(environ)[0])
    except loader.EnvFileWideEncodingError:
        # The gateway reads a wide file as unset, so the key is dropped.
        return True
    return _AUTH_ENV_VAR not in names


def headless_auth_warning(environ: "Mapping[str, str] | None" = None) -> str:
    """Return a warning when API-key auth will not survive ``service install``.

    The readiness probe forwards ``KIRO_API_KEY`` to ``whoami`` only from the
    GATEWAY's environment, and the service starts with a minimal one, so a key
    exported in the installing shell is lost. Unless a ``kiro-cli login`` store
    under the baked ``HOME`` covers it, the dashboard then asks for a sign-in.

    The key is NOT added to :func:`service_environment`: the unit in
    ``/etc/systemd/system`` and the ``0644`` override file are readable by every
    local user. ``~/.kiro/crew/.env`` is the supported home:
    ``load_credentials()`` reads every key from it at boot and enforces ``0600``.

    The returned text is built only from the variable NAME and the ``.env``
    path, never the value (see :func:`api_key_will_be_dropped`). Empty when
    there is nothing to warn about.
    """
    if not api_key_will_be_dropped(environ):
        return ""
    dotenv, account = service_dotenv(environ)
    try:
        _names_defined_in_env_file(dotenv)
    except loader.EnvFileWideEncodingError as exc:
        # Appending UTF-8 after a UTF-16/UTF-32 mark leaves a file the gateway
        # still reads as unset, so the re-save is the only step to give.
        return _with_crew_home_caveat(
            [
                f"Note: {_AUTH_ENV_VAR} is set in this shell but the service will not",
                "   inherit it, and the .env it would read the key from cannot be read:",
                "",
                f"     {loader.undecodable_env_message(dotenv, exc)}",
                "",
                f"   Then add {_AUTH_ENV_VAR} to it (0600) and restart the service:",
                f"     {restart_command_hint()}",
            ],
            environ,
            account,
        )
    # The append must never CREATE the file: under a 022 umask a fresh .env is
    # 0644 until load_credentials() next tightens it. Pre-create and chmod first,
    # which also repairs an already-loose file.
    #
    # shlex.quote because the line is pasted verbatim: an unquoted path with a
    # space word-splits, and the redirect would land the key in a different
    # 0644 file that load_credentials() never tightens.
    #
    # One && chain, so nothing is appended when the chmod fails, and the echo ends
    # a last line written without a newline, which the key would otherwise join.
    target = shlex.quote(str(dotenv))
    remedy = (
        f"touch {target} && chmod 600 {target} && echo >> {target} &&\n"
        f"       printf '%s\\n' \"{_AUTH_ENV_VAR}=${_AUTH_ENV_VAR}\" >> {target}"
    )
    lines = [
        f"Note: {_AUTH_ENV_VAR} is set in this shell but the service will not",
        "   inherit it, so unless kiro-cli has a login credential store to fall",
        "   back on, the dashboard will report a signed-out state. Add it",
        f"   to {dotenv} (0600) and restart the service:",
        "",
        f"     {remedy}",
        f"     {restart_command_hint()}",
    ]
    return _with_crew_home_caveat(lines, environ, account)


def _with_crew_home_caveat(
    lines: "list[str]", environ: "Mapping[str, str] | None", account: str = ""
) -> str:
    """Join a warning's lines, adding the caveat on whose ``.env`` it is when one applies.

    *account* is :func:`service_dotenv`'s: the file is in that account's home,
    so its remedy runs as that account, or root would own a file the service
    cannot read.

    Not named ``*home_no*``: CodeQL's private-data name heuristic reads that as a
    home phone number and flags every printed warning built here as sensitive.
    """
    if account:
        lines += [
            "",
            f"   Run the lines that write the file as {account}, not under sudo, so",
            "   the file is owned by the account the service runs as.",
        ]
    if _home_override_is_set(environ):
        lines.append("")
        lines.append(
            "   KIROCREW_HOME is set here but is also not inherited, so confirm"
        )
        lines.append("   that path is the home the service actually starts with.")
    return "\n".join(lines)


def _home_override_is_set(environ: "Mapping[str, str] | None" = None) -> bool:
    """Whether the installer overrides the crew home (also not inherited)."""
    source = os.environ if environ is None else environ
    return bool(source.get("KIROCREW_HOME", "").strip())


def _names_defined_in_env_file(path: Path) -> "set[str]":
    """Return the variable names a ``.env`` file assigns a NON-BLANK value.

    Reads the file through ``load_credentials()``'s own parser
    (``loader.parse_env_lines``: last assignment wins), including its skip of a
    blank value: a bare ``NAME=`` never reaches ``os.environ``, so it counts as
    absent, also after an earlier non-blank assignment of the same name.

    Values are checked for emptiness and otherwise discarded. An unreadable or
    absent file yields an empty set, so the caller warns (the safe direction).
    A UTF-16 or UTF-32 file raises
    :class:`~kiro_crew.config.loader.EnvFileWideEncodingError` instead, because
    its remedy is a re-save, not an append.
    """
    try:
        raw = loader.read_env_text(path, encoding="utf-8")
    except loader.EnvFileWideEncodingError:
        raise
    except (OSError, UnicodeDecodeError):
        return set()
    # The final map, as load_credentials() reads it: a later ``NAME=`` overrides
    # an earlier value, and that blank value never reaches ``os.environ``.
    return {name for name, value in loader.parse_env_lines(raw).items() if value}


def service_path(home: str) -> str:
    """Build the PATH for the gateway's service environment.

    Snapshots the installer's ``$PATH`` so the gateway's subprocesses resolve
    as they did in the installing shell, with ``~/.local/bin`` and the POSIX
    defaults prepended and duplicates removed in order.

    On Linux the mise shims dir (:func:`_mise_shims_dir`) follows
    ``~/.local/bin``, ahead of the system dirs: the unit keeps this PATH until
    the next install, so a Node mise installs later is found only through the
    shims; without them ``#!/usr/bin/env node`` runs the system Node. That is
    also the order ``mise activate`` gives a shell. launchd's PATH is unchanged.

    nvm bin dirs on the installer's ``$PATH`` (:func:`_nvm_bin_dirs`) follow the
    shims for the same reason: nvm has no shims, so the versioned bin dir the
    installing shell resolved ``node`` from is the only way the service finds
    that Node instead of an older system one in ``/usr/bin``.

    No entry names a versioned install tree, the rule ``ExecStart`` follows
    (:func:`_persistable_path_entry`): the next update replaces that tree.
    """
    env_path = [
        _persistable_path_entry(p, home) for p in os.environ.get("PATH", "").split(":") if p
    ]
    required = [f"{home}/.local/bin"]
    if sys.platform.startswith("linux"):
        if shims := _mise_shims_dir(home):
            required.append(shims)
        required += _nvm_bin_dirs(env_path)
    required += [
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
    # dict.fromkeys dedupes on first occurrence and preserves insertion order,
    # so the required prefixes keep their precedence over the installer's $PATH.
    return ":".join(dict.fromkeys(required + env_path))


def _persistable_path_entry(entry: str, home: str) -> str:
    """*entry* as the service PATH may persist it.

    A managed-venv dir (``crew-venv-<version>/bin``) is named through
    ``crew-venv-current``. A Toolbox tool-version dir under
    ``~/.toolbox/tools/`` is replaced by ``~/.toolbox/bin``, which reaches
    every tool across updates; the next update removes that version.
    """
    from kiro_crew.platform.tree_liveness import through_stable_link

    toolbox_tools = PurePosixPath(home, ".toolbox", "tools")
    if PurePosixPath(entry).is_relative_to(toolbox_tools):
        return str(PurePosixPath(home, ".toolbox", "bin"))
    return through_stable_link(entry)


def _mise_shims_dir(home: str) -> str | None:
    """mise's shims dir for *home*, or ``None`` when it is not one absolute dir.

    The data dir comes from :func:`kiro_crew.env.mise_data_dir` (``MISE_DATA_DIR``,
    then ``XDG_DATA_HOME``, then ``~/.local/share/mise``), read from the
    installer's environment like the rest of this PATH. A relative value, or one
    carrying a PATH separator, is refused: the service has no meaningful cwd, and
    a ``:`` would split into extra PATH entries.
    """
    # Deferred: kiro_crew.env pulls in heavier optional imports than this module.
    from kiro_crew.env import mise_data_dir

    shims = os.path.join(mise_data_dir(home), "shims")
    if ":" in shims or "\0" in shims or not os.path.isabs(shims):
        return None
    return shims


def _nvm_bin_dirs(env_path: list[str]) -> list[str]:
    """nvm's versioned Node bin dirs among *env_path*, in their PATH order.

    Read from the installer's PATH rather than ``$NVM_BIN``: nvm exports that
    variable only in shells that source ``nvm.sh``, and ``sudo`` typically drops
    it, while the PATH entry is what actually resolved ``node`` for the
    installer. An entry matches only as an absolute
    ``<root>/versions/node/<version>/bin``, where ``<root>`` is one nvm itself
    installs to: a dir named ``.nvm`` (the default) or ``nvm`` (the
    ``$XDG_CONFIG_HOME/nvm`` default), or the installer's ``$NVM_DIR``. So an
    unrelated dir is never promoted ahead of the system dirs.
    """
    nvm_dir = os.environ.get("NVM_DIR", "")
    nvm_root = PurePosixPath(nvm_dir) if nvm_dir.startswith("/") else None
    dirs = []
    for p in env_path:
        path = PurePosixPath(p)
        parts = path.parts
        if not (p.startswith("/") and len(parts) >= 6
                and parts[-4:-2] == ("versions", "node") and parts[-1] == "bin"):
            continue
        root = path.parents[3]
        if root.name in (".nvm", "nvm") or root == nvm_root:
            dirs.append(p)
    return dirs


class Platform(enum.Enum):
    """Supported service-management platforms."""

    # System-level systemd. Unit lives at /etc/systemd/system/, write
    # and control commands require sudo. The name reflects the privilege
    # model: a user-level (~/.config/systemd/user/) variant doesn't work
    # on older systemd (e.g. 219), so we don't ship one.
    SYSTEMD = "systemd"
    LAUNCHD = "launchd"
    UNSUPPORTED = "unsupported"


def current_platform() -> Platform:
    """Return the platform whose service manager we should target.

    Linux with systemctl on PATH → SYSTEMD.
    macOS with launchctl on PATH → LAUNCHD.
    Anything else → UNSUPPORTED.
    """
    if sys.platform.startswith("linux") and shutil.which("systemctl"):
        return Platform.SYSTEMD
    if sys.platform == "darwin" and shutil.which("launchctl"):
        return Platform.LAUNCHD
    return Platform.UNSUPPORTED


def restart_command_hint() -> str:
    """Return the shell command that actually restarts the installed gateway.

    The scopes are not interchangeable, so the wrong one is a dead end:

    * ``SYSTEMD`` with ONLY the **system** unit file
      (:data:`kiro_crew.service.linux.UNIT_PATH`) — ``systemctl --user`` fails
      on AL2 and addresses the wrong manager elsewhere: ``sudo systemctl
      restart kirocrew``.
    * ``SYSTEMD`` with ONLY the **per-user** unit file
      (:func:`kiro_crew.service.linux.user_unit_file_path`, the SELinux
      remedy's) — the system command answers ``Unit kirocrew.service not
      found``: ``systemctl --user restart kirocrew``.
    * Anything else — launchd, unsupported, neither file (a foreground
      gateway), or BOTH files (which says nothing about which scope runs) —
      ``kirocrew restart``, which reads both managers.

    Decided by two stats, never by spawning ``systemctl``, because it runs in
    the gateway's update path and at install time. Under ``sudo -H`` the user
    file resolves against root's home and is not found, so the answer is the
    CLI. A stat can still block on a disconnected network home, so the async
    update-failure handler calls this through ``asyncio.to_thread``, never on
    the event loop. The paths live in the Linux module (its tests patch
    ``UNIT_PATH``), imported at call time because it imports this module.
    """
    if current_platform() is Platform.SYSTEMD:
        from kiro_crew.service import (  # circular import: linux imports this module at load
            linux,
        )

        system_present = linux.UNIT_PATH.is_file()
        user_present = linux.user_unit_file_path().is_file()
        if system_present and not user_present:
            return system_restart_command_hint()
        if user_present and not system_present:
            return user_restart_command_hint()
    return "kirocrew restart"


def system_restart_command_hint() -> str:
    """The command that restarts the SYSTEM systemd unit by hand — the string
    :func:`restart_command_hint` answers on a systemd host, spelled once.

    Unconditional on purpose: :mod:`kiro_crew.service.linux` only ever drives
    systemd, so its report must not become ``kirocrew restart`` just because the
    process building it (a test on another platform) is not on a systemd host.
    """
    return f"sudo systemctl restart {SERVICE_NAME}"


def user_restart_command_hint() -> str:
    """The command that restarts the PER-USER systemd unit by hand.

    The SELinux remedy's unit lives in the calling account's own manager; under
    ``sudo`` the command would address root's and fail with ``Unit
    kirocrew.service not found``. Spelled once so the restart report and the
    remedy text cannot drift.
    """
    return f"systemctl --user restart {SERVICE_NAME}"


# The three ways one scope's restart fails (:attr:`ScopeRestart.kind`), kept
# apart because each has its own remedy. REFUSED: the manager did not run it
# (no privilege, unreachable bus); rerun with the right privilege or shell.
# NOT_UP: it ran and the gateway is not up; read the journal. UNCONFIRMED: the
# manager stopped answering mid-restart, so the unit's health is unknown.
RESTART_REFUSED = "refused"
RESTART_NOT_UP = "not-up"
RESTART_UNCONFIRMED = "unconfirmed"


@dataclass(frozen=True)
class ScopeRestart:
    """One scope's outcome from a service restart, for the CLI to print.

    ``scope`` names the manager (``system`` / ``user`` on Linux, ``launchd`` on
    macOS). ``ok`` is the whole verdict: the manager ran the restart AND the
    unit was up once its start had had time to fail. When False, ``reason``
    says what happened in the manager's own words and the unit's real state,
    ``kind`` is one of :data:`RESTART_REFUSED` / :data:`RESTART_NOT_UP` /
    :data:`RESTART_UNCONFIRMED`, and ``hint`` is the command for THAT kind and
    THAT scope: the restart to run by hand, the journal to read, or the status
    to check.
    """

    scope: str
    ok: bool
    reason: str = ""
    kind: str = ""
    hint: str = ""


@dataclass(frozen=True)
class RestartReport:
    """What a service restart did, one :class:`ScopeRestart` per scope acted on.

    Empty ``outcomes`` means no scope had a running unit, so nothing was
    restarted and the caller falls back to its foreground-gateway path — read
    :attr:`attempted`, because an attempted restart that failed must NOT take
    that path (it would spawn an unmanaged gateway beside an installed unit).
    The report is truthy exactly when :attr:`ok` — a report is the answer to
    "did the restart take?" first, so the callers that ask only that
    (``if restart_service():``) read it as the bool it replaces — and the
    failure path reads :attr:`restarted` and :attr:`failures` for what to tell
    the operator, per scope: with a unit running in BOTH scopes (a stale
    crash-looping system unit beside the working per-user one) one scope
    restarts and the other does not, and "the gateway was not restarted" would
    be false for the gateway the operator uses.
    """

    outcomes: tuple[ScopeRestart, ...] = ()

    @property
    def attempted(self) -> bool:
        return bool(self.outcomes)

    @property
    def ok(self) -> bool:
        """At least one scope was restarted and every one of them came back up."""
        return bool(self.outcomes) and all(o.ok for o in self.outcomes)

    @property
    def restarted(self) -> tuple[ScopeRestart, ...]:
        """The scopes whose unit the manager restarted and that stayed up."""
        return tuple(o for o in self.outcomes if o.ok)

    @property
    def failures(self) -> tuple[ScopeRestart, ...]:
        return tuple(o for o in self.outcomes if not o.ok)

    def __bool__(self) -> bool:
        return self.ok
