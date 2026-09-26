"""systemd system-service generation and control for Linux.

The unit lives at ``/etc/systemd/system/kirocrew.service`` and is
enabled+started via ``sudo systemctl enable --now``. The service runs
as the invoking user (via ``User=`` in the unit) — only the install,
uninstall, and start/stop actions need sudo.

Why system-level instead of user-level (``systemctl --user``):
some older distros (notably systemd 219) do not have a working
per-user systemd manager — ``systemctl --user`` fails with
``Failed to get D-Bus connection``. System-level units work
uniformly across any distro shipping systemd >= 219, which is
everything since 2015.

One host class this choice does NOT work on, and cannot be made to work by
anything the installer writes: an SELinux-enforcing host whose kirocrew lives
under ``$HOME`` (the default on Bazzite, Fedora Silverblue/Kinoite and other
atomic desktops). PID 1's domain is denied ``execute`` on a home-labelled file,
so the unit fails every start with ``203/EXEC``. :mod:`kiro_crew.service
.selinux` detects exactly that case by querying the loaded policy, and
:func:`install` refuses up front with a rendered user-scope unit as the remedy
rather than writing a unit that provably cannot start. A per-user INSTALL mode
is deliberately NOT implemented here — it is an install-model change (where the
AppArmor profile and the root-owned overrides file live) rather than a
mechanical one. The unit that remedy stands up is nevertheless first-class to
every other verb: :func:`status`, :func:`is_active`, :func:`stop`,
:func:`restart` and :func:`uninstall` look at BOTH scopes and name the one they
report on, because a health command that sees only the system unit reports a
running user-scope gateway as ``inactive (dead)``.

Sudo scope: this file escalates ``systemctl``, ``install``, ``mkdir``,
``rm``, ``rmdir`` and ``test`` directly, and lends its privileged helpers
to ``service/apparmor.py``, which adds ``apparmor_parser``, ``aa-exec``,
and — inside ``aa-exec`` — ``setpriv`` plus a trusted system ``python3``.

What each mechanism here buys is narrower than it reads, and for ``setpriv`` it
depends on which install path is running — that gap is where an audit of this
module goes wrong.

Trusted resolution buys one thing, on both paths: the interpreter is root-owned,
resolved from a fixed list of trusted system directories, and never
``sys.executable`` — which rules out escalating the venv python, the one that is
user-writable. It says nothing about what that interpreter then loads. Invoked
without ``-I``/``-S``, CPython prepends the caller's working directory to
``sys.path`` and imports ``site``, so code from that working directory,
``PYTHONPATH``, a user-site ``.pth`` line, ``sitecustomize`` or ``usercustomize``
runs before or during the payload's own first import — ``ctypes``, which a planted
module on any of those paths shadows.

WHOSE privileges that loaded code gets is what ``setpriv`` decides, and both
install paths are live. It reuids to the account the INSTALLER was invoked as
(``os.getuid()``): started as an ordinary user — the default, where this module
escalates individual commands through ``sudo`` — it reuids from sudo's root back
to that user, so the probe and anything it loads stay unprivileged; started as
``sudo kirocrew service install`` it reuids to 0, which is a no-op, and only on
that path does the loaded code run as root.

What IS bounded on both paths is the PAYLOAD: a constant stdlib snippet importing
no ``kiro_crew``, so no MCP / LLM / agent code is reached deliberately.

``docs/system-specs/modules/security.md`` carries the reasoning behind the
AppArmor step's four tools. The actual gateway runs as ``User=$USER`` once
started.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from kiro_crew.gateway_shutdown_budget import TOTAL_SHUTDOWN_BUDGET_SECS
from kiro_crew.service import apparmor, selinux
from kiro_crew.service.common import (
    RESTART_NOT_UP,
    RESTART_REFUSED,
    RESTART_UNCONFIRMED,
    SERVICE_NAME,
    RestartReport,
    ScopeRestart,
    kirocrew_bin,
    service_environment,
    system_restart_command_hint,
    systemctl_user_env,
    systemd_quote,
    user_restart_command_hint,
)
from kiro_crew.subprocess_utf8 import UTF8_TEXT

_sd_quote = systemd_quote

log = logging.getLogger(__name__)

UNIT_PATH = Path(f"/etc/systemd/system/{SERVICE_NAME}.service")

# Where a user-scope unit belongs, per systemd.unit(5) — RELATIVE to the service
# account's home, deliberately. Referenced only in the printed remedy; nothing in
# this module writes here. It is not spelled "~/.config/..." because "~" resolves
# against whoever pastes the command, and `service install` is documented to run
# under sudo — so a tilde would silently name root's home in the one shell the
# operator is most likely to be sitting in.
USER_UNIT_SUBDIR = Path(".config/systemd/user")


def user_unit_file_path() -> Path:
    """The per-user unit file at the remedy's location, for THIS account.

    A stat target, not an authority: the manager's own answer (``systemctl
    --user show``, :func:`user_unit_path`) is what the verbs trust, and a unit an
    operator placed in another user-unit directory is missed here on purpose —
    the caller is :func:`kiro_crew.service.common.restart_command_hint`, a string
    built inside the gateway's own update path and at install time, where
    spawning ``systemctl`` is not on. Resolves against the calling process's home
    (under ``sudo -H`` that is root's, and the file is simply not found).
    """
    return Path.home() / USER_UNIT_SUBDIR / f"{SERVICE_NAME}.service"


# Operator-editable environment overrides, read by the unit via
# ``EnvironmentFile=``. Placed AFTER the baked ``Environment=`` lines in the
# unit so an edit here overrides the install-time snapshot (systemd.exec(5):
# later assignments win). This is what makes a port change a one-liner
# (`edit + systemctl restart`) instead of a full re-install: the baked
# ``Environment=KIROCREW_PORT`` was frozen at `service install` time, so before
# this file there was no supported way to change it on a running unit.
ENV_DIR = Path("/etc/kirocrew")
ENV_FILE_PATH = ENV_DIR / "kirocrew.env"

# Seed contents written only when the file is absent (a re-install never
# clobbers operator edits). Everything is commented out so the file changes
# nothing until an operator opts in.
_ENV_FILE_TEMPLATE = """\
# Kiro Crew service environment overrides.
#
# This file is read by the systemd unit (EnvironmentFile=) AFTER the values
# baked in at `kirocrew service install` time, so anything set here WINS. Edit
# it, then apply without reinstalling:
#
#     sudo systemctl restart kirocrew
#
# Bind a non-default dashboard port (e.g. to run a second crew beside the
# default 5476, or when 5476 is already taken):
#KIROCREW_PORT=5477
"""


def _current_user() -> str:
    """Resolve the user the gateway should run AS (the ``User=`` in the unit).

    Prefer ``SUDO_USER`` when it names a non-root account: the module invariant
    is that the gateway — which imports MCP / LLM / agent code — runs as the
    invoking human, never root, so ``sudo kirocrew service install`` must target
    that human, not the root that sudo elevated us to. Falls back to
    ``USER`` / ``LOGNAME`` for a non-sudo invocation. May return ``"root"`` or
    ``""``; :func:`install` refuses to render a root-run agent from either.
    """
    sudo_user = os.environ.get("SUDO_USER", "").strip()
    if sudo_user and sudo_user != "root":
        return sudo_user
    return os.environ.get("USER") or os.environ.get("LOGNAME") or ""


def _current_group(user: str) -> str:
    """Return the primary group name for ``user``.

    On some distros the primary group differs from the username (e.g. a
    shared ``users`` group), so ``Group=<username>`` would fail with
    systemd's status 216/GROUP. Resolve the actual primary group via
    ``id -gn``. Falls back to the username only if id can't resolve it.
    """
    try:
        res = subprocess.run(
            ["id", "-gn", user], capture_output=True, text=True, check=False
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except FileNotFoundError:
        pass
    return user


def _current_uid(user: str) -> int | None:
    """Numeric uid for ``user``, or ``None`` when it cannot be resolved.

    Needed to point the unit at the per-user systemd runtime directory
    (``/run/user/<uid>``). ``pwd`` is Unix-only and this module is imported on
    Windows too, so the lookup is lazy and failure is non-fatal: the caller
    omits the session-bus variables rather than baking in a guessed path.
    """
    try:
        import pwd  # Unix-only; lazy so this module still imports on Windows.

        return int(pwd.getpwnam(user).pw_uid)
    except Exception:
        return None


def _home_for_user(user: str) -> str:
    """Home directory of ``user`` from its passwd entry, else ``Path.home()``.

    Must NOT use ``Path.home()`` for a sudo-selected user: under
    ``sudo -H kirocrew service install`` the process's ``HOME`` is ``/root``
    while ``User=`` is the human (``SUDO_USER``). Baking ``/root`` into the
    unit's ``HOME`` / ``WorkingDirectory`` would then point the service at a
    directory the non-root ``User=`` cannot enter, and it fails to start. Keep
    the home tied to the SAME account the unit runs as. Falls back to
    ``Path.home()`` when the lookup is unavailable (non-Unix, unknown user).
    """
    try:
        import pwd  # Unix-only; lazy so this module still imports on Windows.

        return pwd.getpwnam(user).pw_dir
    except Exception:
        return str(Path.home())


def render_unit(*, user_scope: bool = False) -> str:
    """Render the systemd unit file contents.

    Runs the gateway as the invoking user (``User=``, ``Group=``) so it
    has access to ``$HOME/.kiro/crew``, the user's config, etc. The PATH
    is set explicitly so subprocess invocations of git, node, etc.
    resolve the same way they would from an interactive shell.

    A system unit inherits no login-session environment, so the per-user
    systemd instance is also wired up explicitly — see the ``XDG_RUNTIME_DIR`` /
    ``DBUS_SESSION_BUS_ADDRESS`` lines below.

    The unit deliberately carries no ``AppArmorProfile=`` directive: the
    profile is attached by PATH to the resolved launcher script instead
    (:func:`install_apparmor_profile`), and when both mechanisms are present
    systemd's ``change_onexec`` transition silently wins over the kernel's
    automatic path attachment, defeating it.

    ``user_scope`` renders the ``systemctl --user`` variant this module only ever
    PRINTS (see :func:`selinux_refusal`) — rendered here rather than hand-written
    so the copy-pasteable remedy cannot drift from the unit we actually install.
    Two directives differ, and both are hard requirements of the per-user manager
    rather than style choices: ``User=``/``Group=`` are rejected outright in a
    user unit (the manager already runs as that account), and the install target
    is ``default.target`` because ``multi-user.target`` is a system target the
    user manager does not have.
    """
    bin_path = kirocrew_bin()
    user = _current_user()
    # Only the system unit carries Group=, and resolving it costs an `id -gn`
    # subprocess — skipped for the user scope both because the value is unused
    # and because this render happens on the refusal path, which must not shell
    # out on a host it is declining to touch.
    group = _current_group(user) if user and not user_scope else ""
    # Tie HOME / WorkingDirectory to the SAME account as User= (see
    # _home_for_user): under `sudo -H` the process HOME is /root while User= is
    # the sudo-selected human, and baking /root in would break service start.
    home = _home_for_user(user) if user else str(Path.home())
    # `--no-open` for the same reason as the launchd plist: a service starts on
    # boot and on every restart, and auto-opening a browser there is wrong. It is
    # simply less visible on a headless Linux box than on a desktop.
    exec_start = f"{_sd_quote(bin_path)} gateway --no-open"
    env_lines = f"Environment={_sd_quote(f'USER={user}')}\n" + "".join(
        f"Environment={_sd_quote(f'{key}={value}')}\n"
        for key, value in service_environment(home).items()
    )
    # The gateway spawns agent shells, MCP servers and crons that drive
    # `systemctl --user` (pods). A system unit inherits no login-session
    # environment, so without these the per-user systemd instance is
    # unreachable and every pod command fails with "Failed to connect to bus:
    # No medium found".
    #
    # Deliberately NOT in the shared service_environment(): `/run/user/<uid>` is
    # a Linux/systemd path with no launchd equivalent, so baking it into the
    # macOS plist would be meaningless. Same reason USER= is systemd-only above.
    #
    # A numeric uid is used rather than systemd's `%U` specifier: it has no
    # specifier-expansion semantics to get wrong (and _sd_quote escapes `%` to
    # `%%` anyway, which would defeat a specifier), and it matches how this
    # generator already resolves user/group/home in Python.
    uid = _current_uid(user) if user else None
    if uid is not None:
        env_lines += "".join(
            f"Environment={_sd_quote(f'{key}={value}')}\n"
            for key, value in (
                ("XDG_RUNTIME_DIR", f"/run/user/{uid}"),
                ("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{uid}/bus"),
            )
        )
    return (
        "[Unit]\n"
        "Description=Kiro Crew gateway (dashboard + Slack + cron)\n"
        "Documentation=https://github.com/kirodotdev/KiroCrew\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        # If the gateway crashes hard 3 times within 5 minutes, give up.
        # Without this systemd would loop the restart forever and a bad
        # startup would melt the user's terminal with journal output.
        "StartLimitBurst=3\n"
        "StartLimitIntervalSec=300\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        # Omitted for the user scope: the per-user manager already runs as this
        # account, and it REJECTS User=/Group= outright ("Unknown key name"),
        # which would make the whole unit unloadable rather than merely noisy.
        + ("" if user_scope else f"User={user}\nGroup={group}\n") + f"WorkingDirectory={home}\n"
        f"ExecStart={exec_start}\n"
        # `always`, not `on-failure`: the gateway deliberately exits on its own
        # to be relaunched — the stale-asset watchdog shuts down cleanly when a
        # Toolbox/package update prunes the running install, expecting the
        # supervisor to start a fresh process. `on-failure` never restarts an
        # exit 0, so that path left the gateway down for hours. `always` still
        # honors an explicit `systemctl stop`/`disable` (operator actions are
        # exempt from Restart=), and StartLimit* above caps a tight loop.
        "Restart=always\n"
        "RestartSec=10\n"
        f"TimeoutStopSec={TOTAL_SHUTDOWN_BUDGET_SECS}\n"
        # Operator-editable overrides. systemd applies EnvironmentFile= AFTER —
        # and overriding — the baked Environment= lines below (systemd.exec(5)),
        # so editing this file and restarting changes a value (e.g.
        # KIROCREW_PORT) without a re-install. The leading "-" makes a missing
        # file non-fatal, so the unit still starts where install could not write
        # /etc/kirocrew (the baked Environment= values then apply).
        f"EnvironmentFile=-{ENV_FILE_PATH}\n"
        # Pin a high open-file limit rather than inheriting the host's
        # ambient DefaultLimitNOFILE. Stock systemd defaults to 1024 — and
        # the frontend production build (vite/rollup) opens ~1000
        # lucide-react icon files concurrently, which exhausts a 1024 cap and
        # fails with `EMFILE: too many open files`. Pinning it here makes
        # agent-launched builds and other FD-hungry work survive regardless
        # of the host default.
        "LimitNOFILE=65536\n"
        f"{env_lines}"
        "\n"
        "[Install]\n"
        # multi-user.target is a SYSTEM target; the per-user manager has no such
        # unit, so a user-scope install must want default.target instead or
        # `systemctl --user enable` fails.
        + ("WantedBy=default.target\n" if user_scope else "WantedBy=multi-user.target\n")
    )


class ServiceInstallError(RuntimeError):
    """Raised when service install can't proceed without manual user action."""


def _privilege_prefix() -> list[str]:
    """Return the argv prefix that runs a command with root privilege.

    Empty when the caller is already root (``euid == 0``) — a minimal
    container or a ``root`` login often has no ``sudo`` binary at all, so
    invoking ``sudo`` there would raise ``FileNotFoundError`` for a privilege
    the process already holds. When not root, ``sudo`` is required to write the
    root-owned unit and drive ``systemctl``; if it is missing we cannot proceed,
    and the caller must surface a clear :class:`ServiceInstallError` rather than
    let a raw ``FileNotFoundError`` escape ``controller.install_service`` (which
    only catches ``ServiceInstallError``). ``_require_privilege`` enforces that.
    """
    # getattr: os.geteuid does not exist on Windows, where this module is
    # imported (via controller) even though these functions never run there.
    # Default 1000 (a non-root euid) keeps the "needs sudo" branch on any
    # platform lacking geteuid, which is the safe assumption.
    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None and geteuid() == 0:
        return []
    return ["sudo"]


def _require_privilege() -> None:
    """Raise a clean error if privilege escalation is needed but unavailable.

    Called by every write/control action before it shells out, so a Linux host
    without ``sudo`` (and not already root) fails on the friendly
    ``ServiceInstallError`` path the CLI prints, instead of an uncaught
    ``FileNotFoundError`` traceback.

    Scoped to Linux: this is the systemd module, and in production the
    controller only dispatches here on Linux (macOS uses launchd). The check
    exists specifically for the Linux-host-without-sudo case, so on any other
    platform it is a no-op — which also keeps these functions callable in
    cross-platform unit tests that mock the subprocess layer on a host with no
    ``sudo`` binary.
    """
    if not sys.platform.startswith("linux"):
        return
    # Ask :func:`_privilege_prefix` rather than re-reading ``os.geteuid``: a
    # non-empty prefix IS "this call will shell out through sudo", so the two
    # functions cannot drift into disagreeing about whether escalation is needed.
    if _privilege_prefix() and shutil.which("sudo") is None:
        raise ServiceInstallError(
            "This action needs root to manage the system service at "
            f"{UNIT_PATH}, but 'sudo' was not found. Re-run as root, or install "
            "sudo (e.g. 'yum install sudo' / 'apt-get install sudo')."
        )


def _sudo_run(
    *args: str,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command with root privilege, capturing output.

    Prepends ``sudo`` only when the caller is not already root (see
    :func:`_privilege_prefix`). Sudo prompts for a password on first use;
    subsequent calls within the cached ticket window run silently. All call
    sites (``install``, ``uninstall``, ``stop``) are interactive user commands
    invoked from a TTY, so we always allow the prompt.

    A missing ``sudo`` is turned into a synthetic non-zero result rather than a
    raised ``FileNotFoundError``, so best-effort callers (``restart``, ``stop``)
    degrade to "did not run" instead of crashing. The raising entry points
    (``install`` / ``uninstall``) call :func:`_require_privilege` first, so they
    surface the precise reason before reaching here.
    """
    try:
        return subprocess.run(
            [*_privilege_prefix(), *args],
            input=input_text,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(
            args=list(args), returncode=127, stdout="", stderr="sudo: command not found"
        )


def _systemctl(
    *args: str, sudo: bool = True, user: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run ``systemctl`` against one scope: the system manager, or with
    ``user=True`` the calling account's own manager (``systemctl --user``).

    A user-scope call never goes through sudo, whatever ``sudo`` says: under
    ``sudo`` the process is root, and ``systemctl --user`` there addresses ROOT's
    manager, not the account whose unit this module cares about. Callers gate on
    :func:`_user_scope_unreachable_reason` before spawning for exactly that case.

    It runs with :func:`systemctl_user_env` — the one resolver every
    ``systemctl --user`` in this codebase spawns with — so a shell that inherited
    no login-session variables (a gateway started from a system unit, and every
    shell it spawns) still finds the account's bus when its socket exists, and a
    "not reachable" reading names a host condition, not a missing variable.
    """
    if user:
        return subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True,
            check=False,
            env=systemctl_user_env(),
            **UTF8_TEXT,
        )
    if sudo:
        return _sudo_run("systemctl", *args)
    return subprocess.run(
        ["systemctl", *args], capture_output=True, text=True, check=False
    )


def _user_scope_unreachable_reason() -> str | None:
    """Why this process cannot see the service account's user manager, or ``None``.

    Decided from the process's own identity, never from a spawned command's
    output: a root process whose ``SUDO_USER`` names a human is a ``sudo`` shell,
    and ``systemctl --user`` from it would answer for root's (usually absent)
    manager while the human's unit runs on unseen — the one answer worse than
    "unreachable". ``service install`` is documented to run under sudo, so this is
    the shell an operator is most likely to type ``service status`` into.

    Every other failure to reach the user bus — no session, a stripped
    environment, a sandbox — is left to ``systemctl`` itself, whose exit status
    and diagnostic :func:`_unit_state` reports verbatim as "not reachable".
    """
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None or geteuid() != 0:
        return None
    account = _current_user()
    if not account or account == "root":
        return None
    return (
        f"this is a root shell, and the user scope belongs to {account}'s own "
        f"session; run `systemctl --user status {SERVICE_NAME}.service` as {account}"
    )


# ``ActiveState`` values under which the unit has no process. Everything else
# (``active``, ``activating``, ``deactivating``, ``reloading``, and any value a
# newer systemd adds, or an empty one) is treated as "may still be running".
_STOPPED_STATES = frozenset({"inactive", "failed"})

# ``ActiveState`` values under which the unit is UP — the process is running and
# the manager is not between a crash and the next attempt. Exactly the states
# ``systemctl is-active`` exits 0 for, so the `service status` exit code keeps
# the meaning the base gave it while reading both scopes.
_UP_STATES = frozenset({"active", "reloading"})


@dataclass(frozen=True)
class _UnitState:
    """What one systemd scope says about ``kirocrew.service``.

    ``load`` is systemd's ``LoadState`` (``loaded``, ``not-found``, ``masked``,
    ...) or ``None`` when the scope could not be queried at all, in which case
    ``error`` carries the reason. ``unit_id`` is the canonical ``Id`` the manager
    resolved the name to — another unit's name when ours is an alias — and
    ``fragment`` the unit file it loaded for THAT unit, which is why
    :func:`uninstall` removes it only when :attr:`removable` holds. ``result`` is
    the unit's ``Result`` — ``success``, or why its last run ended (``exit-code``,
    ``signal``, ``start-limit-hit``, …) — which is what tells a crash loop's
    ``activating (auto-restart)`` apart from a unit that is merely slow to start.

    Two questions are asked of this state and they are deliberately not the
    same predicate: :attr:`running` — is there a process a ``stop`` or
    ``restart`` must reach, which includes a unit the manager is still trying
    to run — and :attr:`up` — is the gateway actually running right now, which
    excludes it. A ``203/EXEC`` crash loop is the state the two disagree on:
    ``kirocrew stop`` must reach it, ``kirocrew service status`` must not exit
    0 for it.
    """

    scope: str
    load: str | None
    active: str = ""
    sub: str = ""
    fragment: str = ""
    unit_id: str = ""
    error: str = ""
    result: str = ""

    @property
    def reachable(self) -> bool:
        return self.load is not None

    @property
    def installed(self) -> bool:
        """The manager knows a unit by this name (any load state but not-found)."""
        return self.load is not None and self.load != "not-found"

    @property
    def running(self) -> bool:
        """The unit has, or may still have, a process: any ``ActiveState`` but
        ``inactive`` / ``failed``. ``activating`` covers a crash-looping unit
        sitting in its auto-restart backoff — the state the reporter's ``203/EXEC``
        loop spends nearly all its time in, which ``is-active`` answers non-zero
        for — and ``deactivating`` / ``reloading`` a unit mid-transition. Load
        state says nothing here: a unit masked or made unparseable at runtime
        keeps running until it is stopped. This is the REACH predicate; see
        :attr:`up` for health.
        """
        return self.load is not None and self.active not in _STOPPED_STATES

    @property
    def up(self) -> bool:
        """The gateway is running right now: ``ActiveState`` is ``active`` (or
        ``reloading``), the states ``systemctl is-active`` exits 0 for. A unit in
        ``activating`` — crash-looping through ``auto-restart``, or still
        starting — is not up, and neither is ``failed``. This is the HEALTH
        predicate the `service status` exit code follows; :attr:`running` is
        the wider one ``stop`` / ``restart`` select scopes by.
        """
        return self.load is not None and self.active in _UP_STATES

    @property
    def is_alias(self) -> bool:
        """The name resolves to a different canonical unit."""
        return bool(self.unit_id) and self.unit_id != f"{SERVICE_NAME}.service"

    @property
    def ours(self) -> bool:
        """The unit the name resolves to IS ``kirocrew.service`` — the one guard
        every verb that acts on the name shares, and it fails CLOSED: the manager
        must have reported that exact canonical ``Id``. ``systemctl show <name>``
        on an alias answers for the unit the alias points at, and so would
        ``stop``, ``restart``, ``disable`` and the unlink: an operator who declared
        ``Alias=kirocrew.service`` on their own unit has not made that unit this
        gateway; and an answer with no ``Id`` at all has verified nothing, so it
        is not acted on either. :func:`uninstall` refuses such a unit whole;
        :func:`stop`, :func:`restart`, :func:`is_active` and :func:`is_up` do not
        select or count it, so nothing this module runs ever lands on another
        unit.
        """
        return self.load is not None and self.unit_id == f"{SERVICE_NAME}.service"

    @property
    def acts_on(self) -> bool:
        """Selected by ``stop`` / ``restart`` and counted by ``is_active``: the
        unit is running (:attr:`running`) and it is ours (:attr:`ours`)."""
        return self.running and self.ours

    @property
    def removable(self) -> bool:
        """The unit file is ours to unlink: a LOADED unit whose canonical name is
        ours (:attr:`ours`, fail-closed) and whose fragment is named after it. An
        alias resolves to another unit's file, a mask to ``/dev/null``, and a
        unit file that failed to parse is left for the operator — none of them
        is deleted on this name's account.
        """
        return (
            self.load == "loaded"
            and self.ours
            and os.path.basename(self.fragment) == f"{SERVICE_NAME}.service"
        )

    def headline(self) -> str:
        """One line naming the scope and its state — never a bare ``inactive``
        standing in for "no unit here"."""
        if not self.reachable:
            return f"{self.scope} scope: not reachable from this shell ({self.error})"
        if not self.installed:
            return f"{self.scope} scope: not installed"
        alias = f", an alias of {self.unit_id}" if self.is_alias else ""
        return f"{self.scope} scope: {self.active} ({self.sub}){alias}"


_SHOW_PROPERTIES = ("Id", "LoadState", "ActiveState", "SubState", "FragmentPath", "Result")


def _unit_state(*, user: bool) -> _UnitState:
    """Query one scope for the unit's load / active state and unit file.

    ``systemctl show`` rather than ``is-active`` or ``status`` because it is the
    one query that separates "no unit in this scope" (``LoadState=not-found``)
    from "a unit that is stopped" (``loaded`` + ``inactive``): ``is-active``
    answers ``inactive`` to both, which is what turns a running user-scope
    gateway into a dead system unit in a report that asks only one scope.
    Properties are parsed as ``Key=value`` lines rather than read with
    ``--value``, which the systemd 219 the module docstring commits to does not
    have.

    A scope is unreachable when ``systemctl`` exits non-zero without printing a
    ``LoadState`` — no bus, no session, a sandbox — and the diagnostic is carried
    as the reason; nothing here classifies stderr text.
    """
    scope = "user" if user else "system"
    if user:
        reason = _user_scope_unreachable_reason()
        if reason is not None:
            return _UnitState(scope, None, error=reason)
    argv: list[str] = ["show"]
    for prop in _SHOW_PROPERTIES:
        argv += ["-p", prop]
    res = _systemctl(*argv, f"{SERVICE_NAME}.service", sudo=False, user=user)
    props: dict[str, str] = {}
    for line in (res.stdout or "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            props[key.strip()] = value.strip()
    load = props.get("LoadState")
    if load is None:
        detail = (res.stderr or res.stdout or "").strip().splitlines()
        return _UnitState(
            scope,
            None,
            error=detail[0] if detail else f"systemctl exited {res.returncode} without output",
        )
    return _UnitState(
        scope,
        load,
        active=props.get("ActiveState", ""),
        sub=props.get("SubState", ""),
        fragment=props.get("FragmentPath", ""),
        unit_id=props.get("Id", ""),
        result=props.get("Result", ""),
    )


def _write_unit_via_sudo(contents: str) -> subprocess.CompletedProcess[str]:
    """Write the unit file at ``UNIT_PATH`` atomically via ``sudo install``.

    Writes contents to a user-owned temp file first, then uses
    ``sudo install -m 0644 -o root -g root`` to atomically place it at
    ``UNIT_PATH`` with the correct ownership and mode in a single step.
    The atomic rename inside ``install`` means a SIGINT or crash mid-write
    leaves either the old unit file (if any) or no file at all — never a
    partially-written file that systemd would fail to parse on
    ``daemon-reload``.
    """
    fd, tmp_path = tempfile.mkstemp(prefix="kirocrew-unit-", suffix=".service")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(contents)
        return subprocess.run(
            [
                *_privilege_prefix(),
                "install",
                "-m",
                "0644",
                "-o",
                "root",
                "-g",
                "root",
                tmp_path,
                str(UNIT_PATH),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass


def _install_file_via_sudo(contents: str, dest: Path, mode: str = "0644") -> None:
    """Atomically place ``contents`` at ``dest`` as root, like the unit write.

    Same escalation path as the unit file — no second mechanism, and no kirocrew
    or LLM-influenced code runs under sudo; only ``install`` is invoked.
    """
    fd, tmp_path = tempfile.mkstemp(prefix="kirocrew-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(contents)
        res = _sudo_run("install", "-m", mode, "-o", "root", "-g", "root", tmp_path, str(dest))
        if res.returncode != 0:
            raise ServiceInstallError((res.stderr or res.stdout).strip())
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _sudo_capture(*argv: str) -> tuple[int, str]:
    """Run one privileged command, returning ``(rc, combined output)``.

    Needed for the AppArmor enforcement check: it must run under sudo (an
    unconfined user cannot aa_change_onexec into a named profile, and aa-exec
    does not fail loudly when it cannot transition) AND its exit code is the
    answer rather than an error, so it cannot use the raising helper.
    """
    res = _sudo_run(*argv)
    return (res.returncode, (res.stderr or "") + (res.stdout or ""))


def _sudo_run_checked(*argv: str) -> None:
    """Run one privileged command, raising on a non-zero exit."""
    res = _sudo_run(*argv)
    if res.returncode != 0:
        raise ServiceInstallError((res.stderr or res.stdout).strip())


def _seed_env_file() -> None:
    """Create the operator-editable overrides file if it does not already exist.

    Create-if-absent is the whole contract: a re-install must never clobber an
    operator's edits (the value they set here is precisely what survives a
    re-install, unlike the baked ``Environment=`` snapshot). Best-effort — the
    unit references it with ``EnvironmentFile=-`` so a seeding failure is
    non-fatal and simply leaves the baked defaults in force, and install() must
    proceed to daemon-reload/restart regardless.

    Existence is probed through a PRIVILEGED ``test -e``, never
    ``Path.exists()``: a pre-existing root-owned ``/etc/kirocrew`` with a
    restrictive mode makes ``Path.exists()`` raise ``PermissionError`` under the
    invoking (non-root) user on Python 3.12, which would abort the install. The
    whole body is wrapped so any unexpected error degrades to a warning.
    """
    try:
        # rc 0 => the file already exists (privileged stat sees through a
        # root-only directory); leave whatever is there untouched.
        if _sudo_run("test", "-e", str(ENV_FILE_PATH)).returncode == 0:
            return
        _sudo_run("mkdir", "-p", str(ENV_DIR))
        # 0644: readable so an operator can inspect it, root-owned so an
        # unprivileged process cannot rewrite the service's environment.
        _install_file_via_sudo(_ENV_FILE_TEMPLATE, ENV_FILE_PATH, mode="0644")
    except (ServiceInstallError, OSError):
        log.warning("Could not seed %s; the service uses baked defaults", ENV_FILE_PATH)


def _env_file_is_untouched_seed() -> bool:
    """True only when the overrides file still holds our exact seed template.

    Uninstall uses this to decide whether the file is ours to delete. Any
    difference — an operator edit, a file they pre-provisioned before install,
    or an unreadable file — returns False so their configuration is left intact.
    """
    try:
        return ENV_FILE_PATH.read_text(encoding="utf-8") == _ENV_FILE_TEMPLATE
    except OSError:
        return False


def _user_scope_remedy() -> str:
    """The commands that stand up a working per-user unit on this host.

    Shared by :func:`selinux_refusal` (pre-flight proved the system unit cannot
    start) and :func:`selinux_start_failure_hint` (it started nothing and SELinux
    is enforcing), so the operator is handed the same verified sequence either
    way and the two cannot drift.

    **Every path and account is spelled out, and none is taken from the pasting
    shell.** A user unit has no ``User=`` — the account it runs as is whichever
    manager loads it — so ``~`` and ``$USER`` would decide who runs the agent.
    ``service install`` is documented to run under ``sudo``, so the shell reading
    this is usually root's: a tilde would name ``/root``, ``$USER`` would expand to
    ``root``, and the remedy would hand an operator a unit that runs untrusted
    agent tools as root — defeating the same invariant :func:`install` enforces by
    refusing a ``User=root`` unit. Hence the absolute home, the explicit account
    name, and the warning.

    Both generated paths go through :func:`shlex.quote`, like the ``.env`` remedy
    in :mod:`kiro_crew.service.common`: these lines are copy-pasted verbatim, and
    an account home containing a space would word-split, so ``mkdir`` would create
    the wrong directories and the redirect would put the unit somewhere systemd
    never reads. Ordinary paths come back unquoted, so the common case is
    unchanged.
    """
    unit_body = render_unit(user_scope=True)
    user = _current_user()
    home = _home_for_user(user) if user else str(Path.home())
    account = user or "<the service account>"
    unit_dir = shlex.quote(str(Path(home) / USER_UNIT_SUBDIR))
    unit_file = shlex.quote(str(Path(home) / USER_UNIT_SUBDIR / UNIT_PATH.name))
    return (
        f"   Run the next four commands AS {account} — a user unit carries no\n"
        f"   User=, so it runs as whichever account's manager loads it. Loading it\n"
        f"   from a root shell (the shell you are probably in, since `service\n"
        f"   install` needs sudo) would run the agent as ROOT, which this installer\n"
        f"   otherwise refuses outright. `sudo -u {account}` is NOT enough — it\n"
        f"   creates no session, so `systemctl --user` cannot reach that account's\n"
        f"   manager. Get a real session first, e.g. `machinectl shell {account}@`,\n"
        f"   or just log in as {account}.\n"
        f"\n"
        f"     mkdir -p {unit_dir}\n"
        f"     cat > {unit_file} <<'KIROCREW_UNIT'\n"
        f"{unit_body}"
        f"KIROCREW_UNIT\n"
        f"     systemctl --user daemon-reload\n"
        f"     systemctl --user enable --now {SERVICE_NAME}.service\n"
        f"\n"
        f"   Then, back in a root shell — this one step needs privilege, and takes\n"
        f"   the account name explicitly so it cannot land on the wrong user:\n"
        f"\n"
        f"     loginctl enable-linger {shlex.quote(account)}\n"
        f"\n"
        f"   Manage it with `systemctl --user status|restart {SERVICE_NAME}` and\n"
        f"   `journalctl --user -u {SERVICE_NAME} -f`. `kirocrew service "
        f"status|uninstall`\n"
        f"   report and remove it as the user scope — run them as {account}, not\n"
        f"   under sudo, since a root shell cannot reach that account's manager."
    )


def selinux_refusal(reason: str) -> str:
    """Operator-facing refusal for a system unit SELinux proves cannot start.

    A refusal rather than a warning because everything after this point is
    destructive to no purpose: install would write the unit, ``enable`` it, fail
    at the first ``systemctl restart``, and leave a unit enabled that crash-loops
    at every boot until it exhausts ``StartLimitBurst``. Stopping before the
    first write leaves the host exactly as it was found.

    The remedy embeds a ready-to-paste user unit rendered by :func:`render_unit`,
    not prose describing one: the operator's working unit then carries the same
    ``ExecStart`` and the same baked environment as the unit we would have
    installed, and cannot drift from it as this module changes.
    """
    return (
        f"Refusing to install a system service that cannot start on this host.\n"
        f"   {reason}.\n"
        f"\n"
        f"   This is SELinux type enforcement, not a broken file. The binary is\n"
        f"   perfectly ordinary — it exists, it is executable, and `test -x` on\n"
        f"   it succeeds; the policy's execute check is the only thing that\n"
        f"   fails, and nothing short of asking the policy reveals it. A unit at\n"
        f"   {UNIT_PATH} would fail every start with\n"
        f"   status=203/EXEC until it hit its restart limit.\n"
        f"\n"
        f"   A per-user unit is not subject to this: the per-user systemd manager\n"
        f"   does not run in PID 1's domain, so it is allowed to execute a binary\n"
        f"   under $HOME.\n"
        f"\n"
        f"{_user_scope_remedy()}\n"
        f"\n"
        f"   Installing kirocrew outside $HOME (onto a system-labelled path such\n"
        f"   as /usr/local/bin) also resolves it. Relocating only the LAUNCHER\n"
        f"   does not: whatever systemd execs still runs in PID 1's domain, so\n"
        f"   the next execve of the binary under $HOME is denied identically."
    )


def selinux_start_failure_hint() -> str:
    """SELinux context to append when the unit was written but would not start.

    Covers the residue the pre-flight cannot prove. That check asks only whether
    PID 1's domain may execute the file systemd itself ``execve``s; it cannot
    follow what that file execs at runtime, so a ``KIROCREW_SERVICE_BIN`` override
    naming a system-labelled wrapper that later runs a binary under ``$HOME``
    passes the gate and still fails — as the shell's exit 126 rather than
    ``203/EXEC``, since the wrapper itself execs fine. Rather than guess at a
    wrapper's contents (see the boundary discussion in
    :mod:`kiro_crew.service.selinux`), name SELinux here, where the unit has
    actually failed, so the operator is never left with only "run journalctl".

    Deliberately the HYPOTHESIS and the command that settles it — NOT the
    user-scope remedy :func:`selinux_refusal` prints. This fires on every failed
    restart on every enforcing host, which is all of RHEL/Fedora, including the
    ones the pre-flight positively proved ALLOW for; a port conflict on a stock
    RHEL box would otherwise be answered with a wall of SELinux text and a
    pasteable unit for a denial nobody has observed. A remedy belongs behind a
    proven denial, so this points at the documented one and stops.

    Empty on any host that is not enforcing, so nothing changes on the
    overwhelming majority of installs.
    """
    if not selinux.is_enforcing():
        return ""
    return (
        f"\n\nSELinux is enforcing here, which is one common cause of a unit that\n"
        f"   installs and then will not start. This is a hypothesis, not a finding:\n"
        f"   the pre-flight found no proven denial for {kirocrew_bin()}, but it only\n"
        f"   checks the file systemd execs and the interpreter its shebang names, so\n"
        f"   if that file is a wrapper, whatever IT runs is not covered. Settle it\n"
        f"   with:\n"
        f"\n"
        f"     sudo ausearch -m avc -ts recent\n"
        f"\n"
        f"   An `avc: denied {{ execute }}` naming the gateway binary means no system\n"
        f"   unit can work on this host. The per-user remedy is in\n"
        f"   docs/guides/install.md, \"SELinux-enforcing hosts with kirocrew under\n"
        f"   $HOME\". No such denial means this failure is something else."
    )


def install() -> apparmor.ProfileOutcome:
    """Write the unit file and enable+start the service. Idempotent.

    Calls ``sudo`` to write the unit and to invoke ``systemctl``. Sudo
    will prompt for a password the first time (or when the cached
    ticket has expired) — that prompt appears on the user's terminal.
    No kirocrew / LLM / agent code runs under sudo deliberately — see the module
    docstring's sudo scope for the full set of escalated programs, including
    the ones the AppArmor step adds, and for what the escalated interpreter can
    still load on its own.

    Raises :class:`ServiceInstallError` with a human-readable message if
    a step fails. The CLI catches this and prints the message instead
    of letting a CalledProcessError surface.

    Returns the AppArmor profile outcome for the caller to report. The profile is
    installed BEFORE systemd starts the unit: the directive only takes effect at
    service start, so loading it afterwards would leave the first gateway process
    unprofiled and every agent spawn failing closed until the next restart.
    """
    # Fail early and cleanly if we cannot escalate: without this the first
    # `sudo` call raises FileNotFoundError, which controller.install_service
    # does not catch, so the CLI prints a traceback instead of the reason.
    _require_privilege()

    user = _current_user()
    if not user:
        raise ServiceInstallError(
            "Could not determine current user (USER and LOGNAME both unset). "
            "Set $USER and re-run."
        )
    # Never render a root-run agent. The gateway imports MCP / LLM / agent code,
    # and the module invariant is that it runs as the invoking human, never
    # root. When invoked as bare root (a root login, or `sudo` with no
    # SUDO_USER) `user` resolves to "root"; refuse rather than write a
    # `User=root` unit that would run untrusted tools with host-wide root. The
    # operator picks a real account via `sudo -u <user>` / `SUDO_USER` / `$USER`.
    if user == "root":
        raise ServiceInstallError(
            "Refusing to install a service that runs the agent as root. The "
            "gateway runs untrusted tools and must run as a normal user. Re-run "
            "as that user (e.g. via their login, or `sudo -u <user> kirocrew "
            "service install`), or set $USER to a non-root account."
        )

    # Last gate before anything is written: on an SELinux-enforcing host whose
    # kirocrew lives under $HOME, PID 1's domain is denied execute on the binary
    # this unit would name, so the unit can never start. Everything below
    # would still "succeed" up to the first `systemctl restart`, leaving an
    # enabled unit crash-looping at 203/EXEC on every boot. Fires only on a
    # proven policy denial and fails open on every indeterminate answer, so a
    # host without SELinux, or in permissive mode, is unaffected.
    blocked, selinux_reason = selinux.blocks_system_unit(kirocrew_bin())
    if blocked:
        raise ServiceInstallError(selinux_refusal(selinux_reason))

    needs_profile, profile_reason = apparmor.should_install()
    write_res = _write_unit_via_sudo(render_unit())
    if write_res.returncode != 0:
        raise ServiceInstallError(
            "Failed to write the unit file. The sudo step is required because "
            f"{UNIT_PATH} is owned by root.\n"
            f"   sudo install said: {(write_res.stderr or write_res.stdout).strip()}"
        )

    # Seed the operator-editable overrides file (create-if-absent), so a later
    # `KIROCREW_PORT=...` edit + restart works without re-installing.
    _seed_env_file()

    # Before daemon-reload/enable/restart: a path-attached profile applies at the
    # kernel's own execve() time, so it must already be loaded or the first
    # gateway process (and everything it forks) comes up unprofiled.
    profile_outcome = (
        install_apparmor_profile(_current_uid(user))
        if needs_profile
        else apparmor.ProfileOutcome(False, f"AppArmor profile not needed: {profile_reason}")
    )

    reload_res = _systemctl("daemon-reload")
    if reload_res.returncode != 0:
        raise ServiceInstallError(
            f"`sudo systemctl daemon-reload` failed: "
            f"{(reload_res.stderr or reload_res.stdout).strip()}"
        )

    enable_res = _systemctl("enable", f"{SERVICE_NAME}.service")
    if enable_res.returncode != 0:
        raise ServiceInstallError(
            f"`sudo systemctl enable` failed: "
            f"{(enable_res.stderr or enable_res.stdout).strip()}"
        )

    # Use restart (not start) so re-running install picks up a unit-file
    # change without manual intervention.
    restart_res = _systemctl("restart", f"{SERVICE_NAME}.service")
    if restart_res.returncode != 0:
        raise ServiceInstallError(
            f"`sudo systemctl restart` failed: "
            f"{(restart_res.stderr or restart_res.stdout).strip()}\n"
            f"Run `sudo journalctl -u {SERVICE_NAME}.service -n 50` for details."
            # The pre-flight only proves denials for the file systemd itself
            # execs, so a wrapper's delegated binary can still be denied and land
            # here. Name SELinux where the unit has actually failed rather than
            # leave the operator with only a journalctl command.
            + selinux_start_failure_hint()
        )

    return profile_outcome


def install_apparmor_profile(expected_uid: int | None) -> apparmor.ProfileOutcome:
    """Install the userns AppArmor profile when this host needs one.

    Deliberately NOT fatal: a gateway running without the profile is the status
    quo, whereas aborting a service install because a hardening step failed is a
    regression. The caller prints the outcome and continues either way.

    Attaches the profile to ``kirocrew_bin()`` — the same resolved path
    ``render_unit()`` uses for ``ExecStart`` — instead of relying on
    ``AppArmorProfile=`` (see the module docstring in ``apparmor.py``).

    ``expected_uid`` is the numeric uid of the account the SERVICE runs as
    (``_current_uid(_current_user())``, resolved once by the caller): the
    installer process itself may be running as root (bare root, or under
    ``sudo``), but the launcher script being attached is expected to be owned by
    the human the gateway's ``User=`` names, not by whichever account happens to
    be executing this installer. When that account cannot be resolved
    (``expected_uid is None``) the install is SKIPPED rather than attempted:
    :func:`apparmor._substitutable_by_others` reads ``None`` as "check against
    the calling process's own uid" — the AppImage semantics — and under ``sudo``
    that calling uid is root, so a root-owned, host-shared launcher would pass
    the ownership check and the path-keyed userns grant would extend to every
    account that runs it. Skipping mirrors how an unresolvable ``exec_path`` is
    already a named non-fatal skip, and re-running the install once the account
    resolves is the documented recovery.
    """
    if expected_uid is None:
        return apparmor.ProfileOutcome(
            False,
            "AppArmor profile not installed: the service account's uid could not "
            "be resolved, and without it the ownership check that keeps this "
            "path-keyed userns grant scoped to that account cannot run — the "
            "fallback would be the installer's own uid (root, under `sudo "
            "kirocrew service install`), which would accept a root-owned shared "
            "launcher and hand the grant to every account on this host. The "
            "service was installed without the profile; re-run `kirocrew "
            "service install` once the account resolves.",
            ok=False,
        )
    # uid/gid, not sys.executable: the verification drops privilege back to the
    # invoking user inside the profile and runs a TRUSTED system python, because
    # the venv interpreter is user-writable and must never execute under sudo.
    return apparmor.install(
        _install_file_via_sudo,
        _sudo_run_checked,
        _sudo_capture,
        os.getuid(),
        os.getgid(),
        exec_path=kirocrew_bin(),
        expected_uid=expected_uid,
    )


def remove_apparmor_profile() -> apparmor.ProfileOutcome:
    """Unload and delete the profile so uninstall leaves the host as it was."""
    return apparmor.uninstall(_sudo_run_checked)


def install_launcher_profile(exec_path: str | None = None) -> apparmor.ProfileOutcome:
    """Attach the userns profile to a directly launched app (AppImage/desktop).

    Same three privileged helpers as the service path — one escalation mechanism
    for both profiles, and still nothing but ``install`` / ``apparmor_parser`` /
    ``aa-exec`` running under sudo. No kirocrew or LLM-influenced code does.

    Unlike the service profile this is NOT reached from ``service install``: a
    direct launch has no unit to hang it off, so the user (or the desktop app,
    which surfaces the exact command) invokes it explicitly.
    """
    return apparmor.install_launcher(
        _install_file_via_sudo,
        _sudo_run_checked,
        _sudo_capture,
        os.getuid(),
        os.getgid(),
        exec_path,
    )


def remove_launcher_profile() -> apparmor.ProfileOutcome:
    """Unload and delete the launcher profile, leaving the host as it was found."""
    return apparmor.uninstall_launcher(_sudo_run_checked)


@dataclass(frozen=True)
class UninstallReport:
    """What :func:`uninstall` did, per scope, for the controller to print.

    Each field is one of ``"removed (<unit file>)"``, ``"removed (the link to …;
    the linked unit file … itself was kept)"``, ``"stopped (its unit file … was
    already gone …)"``, ``"not installed"``, ``"left in place (<why>)"``,
    ``"stopped and disabled, but its unit file … could not be removed (…)"`` or,
    for the user scope, ``"not reachable from this shell (<reason>)"`` — so the
    operator always reads which scope was touched and which was not, instead of a
    blanket "stopped and removed" that is true of one scope at most. The two
    sets are the report's structure, and the controller reads THEM, never a
    line's wording: ``removed`` names every scope whose unit is gone from its
    manager on this call's account — its file unlinked, its link entry removed,
    or a fileless unit stopped and ``daemon-reload``ed away — and ``unfinished``
    names every scope whose teardown was ATTEMPTED and did not finish. Unfinished
    are: a ``stop`` or ``disable`` the manager refused (the unit stays loaded,
    possibly running, and its file is left in place); a unit still running
    after ``stop`` returned 0, or whose state could not be re-read; a system
    unit this process lacks the privilege to touch (no ``sudo``), or whose
    manager cannot be reached from this shell while ``/run/systemd/system``
    says one runs; a refusal — an alias, an unverified ``Id``, a running unit
    under a load state other than ``loaded`` — that leaves a RUNNING unit or the
    system unit file at ``UNIT_PATH`` behind; and a unit file or link that could
    not be removed after a successful stop. The report still carries every
    scope, the controller marks those lines and exits non-zero after printing
    it. A refusal that leaves nothing behind is a report and finishes the
    scope: in the user scope a unit that runs nothing and is not ours to remove
    (an inactive alias, mask or unparseable unit), and in the system scope a unit
    that runs nothing when there is no file at ``UNIT_PATH`` either (a runtime
    mask, an alias provided from another directory) — nothing is left running
    and nothing this module owns is left on disk. It is a report rather than an
    exception because by then the other scope may already be torn down, and an
    exception would drop that fact and skip the AppArmor profile removal that
    follows.
    """

    system: str
    user: str
    unfinished: frozenset[str] = frozenset()
    removed: frozenset[str] = frozenset()

    @property
    def incomplete(self) -> bool:
        """At least one attempted teardown did not finish; the exit code says so."""
        return bool(self.unfinished)

    @property
    def removed_any(self) -> bool:
        """A unit is gone from at least one scope's manager — the structured
        outcome, not a line prefix: the fileless system unit that was stopped and
        ``daemon-reload``ed reads ``stopped (…)`` and counts, a ``left in place``
        line never does."""
        return bool(self.removed)


@dataclass(frozen=True)
class _ScopeTeardown:
    """One scope's outcome inside :func:`uninstall`: the report line, whether the
    teardown finished (see :class:`UninstallReport`), and whether the unit is
    gone from that scope's manager."""

    line: str
    finished: bool = True
    removed: bool = False


def _first_line(res: subprocess.CompletedProcess[str]) -> str:
    """The first line of a failed command's diagnostic, for a report line."""
    detail = (res.stderr or res.stdout or "").strip().splitlines()
    return detail[0] if detail else f"exited {res.returncode} without output"


def _stop_and_disable(*, user: bool, file_present: bool = True) -> str | None:
    """``stop`` then ``disable`` the unit in one scope, then confirm it stopped;
    the ``left in place (…)`` report line when any step did not take, ``None``
    when the unit is verified stopped.

    Checked, unlike the best-effort ``stop()`` / ``restart()`` verbs: the step
    after this one deletes the unit file, and a unit whose stop the manager
    refused — ``RefuseManualStop=yes`` in the loaded unit, a bus that went away
    mid-run, a sudo the operator declined — is still loaded and possibly still
    running. Unlinking its file then would leave a running gateway with no unit
    to find it by, while the report read ``removed``. So a failed step ends this
    scope's teardown before anything is unlinked: the file stays, the line says
    which verb the manager refused and why, and the controller exits non-zero.
    The final re-read is the proof the unlink rests on: ``stop`` exiting 0 is the
    manager's word that the stop job ran, the ``ActiveState`` it reports
    afterwards is the fact.

    ``file_present=False`` is the system unit still LOADED after its file was
    deleted under it (no ``daemon-reload`` yet): the process is exactly what
    must be stopped, but there is no unit file for ``disable`` to read an
    ``[Install]`` section from and systemd refuses the verb (``Unit file …
    does not exist``), so ``stop`` runs alone, checked the same way.
    """
    unit = f"{SERVICE_NAME}.service"
    spelled = "systemctl --user" if user else "sudo systemctl"
    kept = "the unit file was not removed" if file_present else "the unit is still loaded"
    for verb in ("stop", "disable") if file_present else ("stop",):
        res = _systemctl(verb, unit, user=user)
        if res.returncode != 0:
            return f"left in place (`{spelled} {verb} {unit}` failed: {_first_line(res)}; {kept})"
    after = _unit_state(user=user)
    if not after.reachable:
        # No answer is not "not running": the unlink rests on the manager's
        # word that the unit is stopped, and a manager that went away between
        # `stop` and this read (a session ending, a bus that closed) has given
        # none. The file stays; the operator re-runs from a shell that reaches it.
        return (
            f"left in place (the {after.scope} manager stopped answering after `{spelled} "
            f"stop {unit}`: {after.error}; whether the unit stopped could not be confirmed, "
            f"so {kept})"
        )
    if after.running:
        return (
            f"left in place (still {after.active} ({after.sub}) after `{spelled} stop "
            f"{unit}` returned 0; stop it by hand, then run `kirocrew service uninstall` "
            f"again)"
        )
    return None


def _teardown_refusal(state: _UnitState) -> str | None:
    """Why a scope's unit must not be touched at all — not stopped, not disabled,
    not unlinked — or ``None`` when the teardown may proceed.

    Three shapes. An ALIAS: ``systemctl show <name>`` answered for the unit the
    name resolves to, so every verb issued on our name would act on that other
    unit — stopping it, disabling its install links, unlinking its file. An
    answer with NO canonical ``Id`` (:attr:`_UnitState.ours` fails closed):
    nothing was verified, so nothing is acted on. A unit that is RUNNING under
    any load state but ``loaded`` — masked at runtime (``systemctl mask`` leaves
    a running unit running), edited into an unparseable state and reloaded, or
    left ``not-found`` by a file removed under it: systemd reports its
    ``FragmentPath`` as the mask or nothing at all, so a teardown that followed
    the file would delete the wrong thing or nothing while the process kept
    running, and ``disable`` on such a unit fails anyway. All are handed to the
    operator whole, with the step that makes the unit removable.
    """
    unit = f"{SERVICE_NAME}.service"
    if state.is_alias:
        return f"left in place ({unit} is an alias of {state.unit_id}; manage that unit)"
    if not state.ours:
        # Reachable, yet no canonical `Id` in the answer: the identity every
        # verb below would act on is unverified, and an unverified name is not
        # ours to stop, disable or unlink.
        return (
            f"left in place (the manager did not report {unit}'s canonical Id, so the "
            f"unit's identity could not be verified; inspect it with `systemctl show {unit}`)"
        )
    if state.running and state.load != "loaded":
        how = "unmask and stop it first" if state.load == "masked" else "stop it first"
        return (
            f"left in place (an {state.active} unit whose load state is {state.load}: "
            f"{how}, then run `kirocrew service uninstall` again)"
        )
    return None


# systemd as the init system leaves this directory behind for exactly this
# question (sd_booted(3) checks nothing else). A host without it runs no system
# manager, so no unit can be running there — the one case in which a unit file
# is removed without the manager's word that it is stopped.
_SYSTEMD_BOOTED_DIR = Path("/run/systemd/system")


def _teardown_system_scope(*, file_present: bool) -> _ScopeTeardown:
    """Stop, disable and remove the system unit; the scope's :class:`_ScopeTeardown`.

    Reached when the unit file exists (``file_present``) or when the file is
    gone but the manager still answers for the unit — loaded and possibly
    running after its file was deleted under it — which :func:`uninstall`
    establishes with an unprivileged ``show`` before calling here. Order per
    scope is stop → disable → verify inactive → unlink → daemon-reload, and
    nothing is unlinked without the manager's word that the unit is stopped —
    except where no manager can exist: a host not booted with systemd (a
    container where systemd is not PID 1, where ``install()`` leaves the file
    behind when its ``daemon-reload`` fails) has nothing running under any
    unit, so the stale file is removed outright. A manager that exists but
    cannot be reached from this shell is the opposite case: the unit may well
    be running, so the file stays and the line says why. With no file there is
    nothing to ``disable`` or unlink: a loaded unit is stopped (checked) and the
    ``daemon-reload`` makes it ``not-found`` — the unit is gone, so that counts
    as removed; one the manager has in any other load state and not running (a
    runtime mask) is reported, never claimed removed. A refusal
    (:func:`_teardown_refusal`) is unfinished when it leaves a running unit or
    the module's own file at ``UNIT_PATH`` behind; with no file and nothing
    running it is a report, the rule the user scope applies.

    The privilege check is reported, not raised: a stale system unit beside the
    account's own user unit, on a host with no ``sudo``, is exactly the layout the
    SELinux remedy leaves behind, and an exception here would abort
    :func:`uninstall` before the user scope — the one this account CAN tear down —
    is reached. The system line names the missing privilege and the scope is
    marked unfinished, so the operator still reads that the system unit needs
    root while the user unit is gone.
    """
    try:
        _require_privilege()
    except ServiceInstallError as exc:
        return _ScopeTeardown(f"left in place (privilege unavailable: {exc})", finished=False)
    state = _unit_state(user=False)
    if not state.reachable:
        if _SYSTEMD_BOOTED_DIR.is_dir():
            return _ScopeTeardown(
                f"left in place (the system manager is not reachable from this shell: "
                f"{state.error}; whether the unit is running cannot be confirmed from "
                f"here, so its file was not removed — run this from a host shell)",
                finished=False,
            )
    else:
        refusal = _teardown_refusal(state)
        if refusal is not None:
            # Unfinished when something is left behind for the operator: a unit
            # that keeps running, or the file at UNIT_PATH this module owns. A
            # unit that runs nothing and has no file there (an alias provided
            # from another unit directory) leaves nothing, so it is a report.
            return _ScopeTeardown(refusal, finished=not file_present and not state.running)
        # A LOADED unit is stopped and disabled first, whatever its active state
        # (a stopped one costs two no-op verbs). Anything else the manager answers
        # for is not running (see `_teardown_refusal`), has nothing to disable and
        # would only refuse the verbs: a file dropped without a `daemon-reload`, a
        # mask, an unparseable unit. Its file is removed as the base removed it.
        if state.load == "loaded":
            refused = _stop_and_disable(user=False, file_present=file_present)
            if refused is not None:
                return _ScopeTeardown(refused, finished=False)
        elif not file_present:
            # Not running, not loaded, and no file at the path this module owns
            # (a runtime mask under /run, a definition placed elsewhere): nothing
            # here to stop or unlink, so the line reports what the manager holds.
            what = f"load state {state.load}, unit file {state.fragment or 'unknown'}"
            return _ScopeTeardown(f"left in place ({what})")
    if file_present:
        rm_res = _sudo_run("rm", "-f", str(UNIT_PATH))
        if rm_res.returncode != 0:
            return _ScopeTeardown(
                f"stopped and disabled, but its unit file {UNIT_PATH} could not be "
                f"removed ({_first_line(rm_res)}); remove it by hand, then run "
                f"`sudo systemctl daemon-reload`",
                finished=False,
            )
    # Remove the overrides file ONLY when it still holds our untouched seed —
    # proving both that we wrote it and that the operator never edited it. An
    # operator-authored or -edited /etc/kirocrew/kirocrew.env (including one
    # pre-provisioned before install, which _seed_env_file preserves) is their
    # config, not ours to delete. rmdir is best-effort and only clears an empty
    # dir, so it never removes anything else parked under /etc/kirocrew.
    if _env_file_is_untouched_seed():
        _sudo_run("rm", "-f", str(ENV_FILE_PATH))
        _sudo_run("rmdir", str(ENV_DIR))
    _systemctl("daemon-reload")
    if not file_present:
        return _ScopeTeardown(
            f"stopped (its unit file {UNIT_PATH} was already gone, so nothing was disabled "
            f"or unlinked; daemon-reload run)",
            removed=True,
        )
    return _ScopeTeardown(f"removed ({UNIT_PATH})", removed=True)


def _user_unit_search_path() -> list[str] | None:
    """The user manager's unit search directories, in its own words — the
    Manager object's ``UnitPath`` property (``systemctl --user show -p
    UnitPath``, no unit named; present since systemd 219, printed as one
    space-separated line) — or ``None`` when the manager did not answer it.

    Asked, not derived from ``$XDG_CONFIG_HOME`` and friends: the directories a
    unit file may be OURS to unlink are exactly the ones this manager loads units
    from, and the manager's list already carries every XDG override, the
    ``.control`` and generator directories, and a distribution's own layout.
    """
    res = _systemctl("show", "-p", "UnitPath", sudo=False, user=True)
    for line in (res.stdout or "").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "UnitPath":
            dirs = [d for d in value.split() if d.startswith("/")]
            return dirs or None
    return None


def _same_dir(a: str, b: str) -> bool:
    return os.path.normpath(a) == os.path.normpath(b) or os.path.realpath(a) == os.path.realpath(b)


def _user_unit_removal(fragment: str, search_path: list[str]) -> tuple[str | None, list[str]]:
    """What the user-scope teardown may delete once the unit is stopped and
    disabled: ``(the unit file to unlink, or None; the link entries to remove)``.

    Only a DIRECT file — not a symlink — that lives inside one of the manager's
    unit search directories is ours to unlink. Anything else the manager
    reported as ``FragmentPath`` is a LINKED unit: ``systemctl --user link
    /path/kirocrew.service`` puts a symlink named after the unit into the
    account's unit directory and leaves the operator's file where it was, and
    which of the two the manager reports depends on its loader — the name-map
    loader (``unit_file_resolve_symlink``, systemd ≥ 245) reports the link
    itself, the older ``open_follow()`` loader reported the resolved target.
    Either way the file that holds the definition is the operator's, and
    following ``FragmentPath`` to an ``unlink`` would delete it. So for a linked
    unit nothing at the source is touched: ``systemctl --user disable`` (run
    before this) removes the link from the configuration directory, and any
    link entry that survived it — the reported link itself, or an entry in a
    search directory that resolves to the source — is returned to be unlinked
    as a LINK, which removes the entry and never what it points at.
    """
    unit = f"{SERVICE_NAME}.service"
    parent = os.path.dirname(fragment)
    if any(_same_dir(parent, d) for d in search_path) and not os.path.islink(fragment):
        return fragment, []
    source = os.path.realpath(fragment)
    links: list[str] = []
    for d in search_path:
        entry = os.path.join(d, unit)
        if not os.path.islink(entry):
            continue
        if os.path.normpath(entry) == os.path.normpath(fragment) or os.path.realpath(entry) == source:
            links.append(entry)
    return None, links


def uninstall() -> UninstallReport:
    """Stop, disable, and remove the unit from every scope that has it. Idempotent.

    The system scope is torn down under sudo as before, and decided by the
    MANAGER rather than by the unit file: with no file at ``UNIT_PATH`` the
    manager is still asked (an unprivileged ``show``), so a unit still loaded
    and running after its file was deleted under it is stopped, one a
    ``daemon-reload`` left ``not-found`` but running is refused whole like the
    user scope's, and a manager this shell cannot reach on a systemd host reads
    ``not reachable from this shell (…)`` instead of ``not installed``. On a host
    with no ``sudo`` the whole call still runs: the system line reads ``left in
    place (privilege unavailable: …)`` and the user scope is torn down
    regardless. The user scope — the
    unit the SELinux remedy stands up — is torn down through the account's own
    manager (``systemctl --user``, never sudo) and its unit file is unlinked as
    the calling user — only when that file is a DIRECT file inside the manager's
    own unit search path (``UnitPath``): a linked unit (``systemctl --user link
    /path/kirocrew.service``) keeps the operator's file and loses only its link
    entries (:func:`_user_unit_removal`). In either scope the order is stop → disable → verify
    inactive → unlink → daemon-reload (:func:`_stop_and_disable`): a step the
    manager refused leaves the file where it is and is reported on that scope's
    line, and a unit that is running under any load state but ``loaded``, or an
    alias of another unit, is refused whole before any file operation
    (:func:`_teardown_refusal`). A user scope this shell cannot reach is left alone
    and reported as such: nothing is deleted on the strength of a query that did
    not run. Nothing installed in either scope is a plain report, not an error.
    """
    # Probe unprivileged so we don't prompt for a password when the unit isn't
    # even present: a stock `/etc/systemd/system` is traversable by every user,
    # so a plain stat answers whether the FILE is there, and `systemctl show`
    # needs no privilege either. Unlike `_seed_env_file`'s probe, this one does
    # not need the privileged `test -e` — that path targets a directory an
    # operator may have locked down, where an unprivileged stat cannot answer
    # trustworthily (see that function's own docstring for the failure it takes).
    system = "not installed"
    unfinished: set[str] = set()
    removed: set[str] = set()

    def _system(outcome: _ScopeTeardown) -> None:
        nonlocal system
        system = outcome.line
        if not outcome.finished:
            unfinished.add("system")
        if outcome.removed:
            removed.add("system")

    def _report(user: str, *, user_unfinished: bool = False, user_removed: bool = False) -> UninstallReport:
        return UninstallReport(
            system,
            user,
            unfinished=frozenset(unfinished | ({"user"} if user_unfinished else set())),
            removed=frozenset(removed | ({"user"} if user_removed else set())),
        )

    if UNIT_PATH.exists():
        _system(_teardown_system_scope(file_present=True))
    else:
        # The file's absence is not the manager's word. A unit whose file was
        # deleted under it stays loaded and running until it is stopped (and
        # reads `not-found` after a `daemon-reload` while still running), so
        # "no file" must not become "not installed" while a gateway runs: the
        # manager is asked, and a unit it still answers for takes the same
        # teardown as one whose file exists.
        state = _unit_state(user=False)
        if not state.reachable:
            # No manager can exist on a host not booted with systemd, so the
            # scope IS empty there; on a systemd host an unreachable manager
            # may well run the unit, and that is reported as what it is —
            # nothing is left behind, so the scope is not unfinished.
            if _SYSTEMD_BOOTED_DIR.is_dir():
                system = f"not reachable from this shell ({state.error})"
        elif state.installed or state.running:
            _system(_teardown_system_scope(file_present=False))

    state = _unit_state(user=True)
    if not state.reachable:
        return _report(f"not reachable from this shell ({state.error})")
    # Before the not-installed answer: a unit whose file was removed under it
    # reads `not-found` while it keeps running, and that is a refusal, not
    # "nothing here".
    refusal = _teardown_refusal(state)
    if refusal is not None:
        # A refused unit that is still running needs the operator's hand (exit 1);
        # a refused unit that runs nothing is a report (exit 0).
        return _report(refusal, user_unfinished=state.running)
    if not state.installed:
        return _report("not installed")
    # Only a loaded unit that is canonically ours, in a file named after it, is
    # torn down: a mask points at /dev/null and a unit that failed to parse is
    # the operator's to inspect (an alias was refused above). Neither runs
    # anything by here, so neither is stopped, disabled or unlinked.
    if not state.removable:
        what = f"load state {state.load}, unit file {state.fragment or 'unknown'}"
        return _report(f"left in place ({what})")
    # Which file is ours to delete is settled BEFORE the verbs, from the
    # manager's own unit search path: `disable` removes a linked unit's link
    # entry, and the shape of what `FragmentPath` names must be read while it
    # is still there. With no answer nothing is unlinked — the unit is not even
    # stopped, the way every other unverified shape is handed back whole.
    search_path = _user_unit_search_path()
    if search_path is None:
        return _report(
            f"left in place (the user manager did not report its unit search path, so "
            f"{state.fragment} could not be told from an operator's linked unit file; "
            f"inspect it with `systemctl --user show -p UnitPath` and `systemctl --user "
            f"show {SERVICE_NAME}.service`)",
            user_unfinished=state.running,
        )
    unit_file, links = _user_unit_removal(state.fragment, search_path)
    refused = _stop_and_disable(user=True)
    if refused is not None:
        return _report(refused, user_unfinished=True)
    # A direct file inside the manager's search path is the user-scope unit
    # itself — the remedy's ~/.config/systemd/user/kirocrew.service, or the same
    # file in any other directory the manager loads from — and is unlinked. A
    # linked unit's source is never followed (see `_user_unit_removal`): only
    # its link entries go, and unlinking a symlink removes the entry alone.
    # Either way the failure is reported, not raised: the system scope above may
    # already be gone, and the controller still has the AppArmor profile to
    # remove and both scope lines to print. The unit is stopped and disabled,
    # so nothing runs.
    if unit_file is not None:
        try:
            os.unlink(unit_file)
        except FileNotFoundError:
            pass
        except OSError as exc:
            return _report(
                f"stopped and disabled, but its unit file {unit_file} could not be "
                f"removed ({exc}); remove it by hand, then run "
                f"`systemctl --user daemon-reload`",
                user_unfinished=True,
            )
        line = f"removed ({unit_file})"
    else:
        source = os.path.realpath(state.fragment)
        for link in links:
            try:
                os.unlink(link)
            except FileNotFoundError:
                pass
            except OSError as exc:
                return _report(
                    f"stopped and disabled, but the link {link} to its unit file {source} "
                    f"could not be removed ({exc}); remove the link by hand — not the file "
                    f"it points at — then run `systemctl --user daemon-reload`",
                    user_unfinished=True,
                )
        line = f"removed (the link to {source}; the linked unit file {source} itself was kept)"
    _systemctl("daemon-reload", user=True)
    return _report(line, user_removed=True)


def user_unit_installed() -> bool:
    """Whether the calling account's own manager has a ``kirocrew.service`` loaded.

    The one scope question a caller outside this module needs: ``kirocrew logs``
    reads the USER journal when the gateway is the per-user unit, and that
    journal exists only if this answers True. An unreachable user scope (root
    shell, no session bus) is False — there is nothing to read from here.
    """
    state = _unit_state(user=True)
    return state.installed and state.ours


def user_unit_active() -> bool:
    """Whether the per-user unit is running right now — active, or crash-looping
    through ``activating (auto-restart)``, which is the unit whose journal holds
    the failure worth reading.

    ``kirocrew logs`` asks this when BOTH scopes hold a unit — a stopped system
    unit left by an earlier install beside the per-user one — so the journal it
    shows is the running gateway's, not the dead unit's.
    """
    return _unit_state(user=True).acts_on


def user_unit_path() -> Path | None:
    """The per-user unit's file, when the calling account's own manager has one
    loaded and the file exists — the user-scope answer to
    :func:`controller.installed_unit_path`'s "is a service definition installed".

    The manager is asked, not a directory guessed: the SELinux remedy names
    ``~/.config/systemd/user``, but a unit an operator placed in any other
    user-unit directory is just as much the user-scope unit, and the file
    systemd reports as ``FragmentPath`` is the one it loaded. Only a LOADED unit
    whose canonical ``Id`` is ours (:attr:`_UnitState.ours`, fail-closed) answers:
    a unit the manager does not have loaded (a file dropped without
    ``daemon-reload``), a mask (``FragmentPath=/dev/null`` — no definition to
    read), a unit that failed to parse (``bad-setting`` / ``error`` — a file the
    doctor would read as the definition while the manager runs nothing from
    it), a name that is an alias of another unit, an answer with no ``Id`` or a
    blank ``LoadState`` (nothing verified), and an unreachable user scope all
    answer ``None``: nothing this account can point ``kirocrew doctor`` at.
    """
    state = _unit_state(user=True)
    if state.load != "loaded" or not state.ours or not state.fragment:
        return None
    path = Path(state.fragment)
    return path if path.is_file() else None


def _running_scopes() -> list[bool]:
    """The ``user`` flags of every scope whose unit is running or mid-transition
    AND is canonically ours (:attr:`_UnitState.acts_on`).

    The one scope question ``is_active()``, ``stop()`` and ``restart()`` share,
    asked with the same ``systemctl show`` the status verbs use rather than a
    second ``is-active`` probe: ``is-active`` answers non-zero for ``activating``,
    so a crash-looping unit in its auto-restart backoff would select no scope and
    ``kirocrew stop`` would issue nothing while the loop kept flapping. An alias
    is never selected: ``show kirocrew.service`` answered for the unit the alias
    points at, and every verb issued on our name would act on that unit — the
    same guard :func:`uninstall` applies before it touches a file.
    """
    return [user for user in (False, True) if _unit_state(user=user).acts_on]


def is_active() -> bool:
    """Return True if OUR unit is running in EITHER systemd scope — the REACH
    predicate.

    "Running" is any ``ActiveState`` but ``inactive`` / ``failed`` — a unit
    crash-looping through ``activating (auto-restart)`` counts, because a caller
    asking this is about to stop or restart it, and a unit the manager is still
    trying to run is one it must be able to reach. It is NOT the answer to "is
    the gateway up": that is :func:`is_up`, which the `service status` exit code
    follows. A name that is an alias of another unit is not counted in either:
    that unit is not this gateway, and the verbs gated on this answer would act
    on it. ``show`` needs no sudo, so both scopes use the unprivileged path.
    The user scope counts because the SELinux remedy runs the gateway there; a
    caller must not be told no while a gateway runs.
    """
    return bool(_running_scopes())


def is_up() -> bool:
    """Return True if OUR gateway is UP in either scope — the HEALTH predicate.

    ``ActiveState=active`` (or ``reloading``), which is what ``systemctl
    is-active`` exits 0 for. A crash-looping unit in ``activating
    (auto-restart)`` and a ``failed`` one are not up, however reachable they
    are: the `service status` exit code follows this predicate so that a script
    or monitor gating on it reads a gateway that never started as down — the
    ``203/EXEC`` loop is the failure `status` exists to surface, and its
    headline says ``activating (auto-restart)`` while this says 1. An alias of
    another unit is not up either: whatever runs under that name is not this
    gateway (the headline names the alias).
    """
    return any((s := _unit_state(user=user)).up and s.ours for user in (False, True))


def stop() -> None:
    """Stop the service in every scope where it runs, without disabling it."""
    for user in _running_scopes():
        _systemctl("stop", f"{SERVICE_NAME}.service", user=user)


# How long a unit the manager just restarted must stay up before the restart is
# reported as having taken, and how often its state is re-read meanwhile. The
# unit this module renders is ``Type=simple``, whose start job completes the
# moment the process is forked: ``systemctl restart`` exits 0 for a gateway that
# dies on its first instruction — a ``203/EXEC`` crash loop included — and only
# the state read AFTER the fork tells the two apart. A failed one is in
# ``activating (auto-restart)`` (or ``failed``, once the start limit is hit)
# within milliseconds of the fork and stays there for ``RestartSec=10``; a live
# one is still ``active (running)`` at the end of the window. Two seconds cover
# an exec failure and a Python import error with margin; a gateway that dies
# later than that is the supervisor's to report, and `kirocrew service status`
# / `kirocrew logs` are where it shows.
_RESTART_SETTLE_SECS = 2.0
_RESTART_POLL_SECS = 0.25


def _journal_hint(*, user: bool) -> str:
    """The journal read that shows why THIS scope's unit exits — the remedy for a
    unit that does not stay up. Scope-specific on purpose: with both scopes
    running, `kirocrew logs` picks the user unit's journal whenever that unit is
    running, which is the healthy sibling's when the system unit is the one
    flapping."""
    unit = f"{SERVICE_NAME}.service"
    if user:
        return f"journalctl --user -u {unit} -n 50 --no-pager"
    return f"sudo journalctl -u {unit} -n 50 --no-pager"


def _confirm_up(*, user: bool) -> tuple[str, str] | None:
    """After a ``restart`` the manager ran: ``None`` once the unit is up at the
    end of the settle window, else ``(kind, reason)`` — the unit's real
    ``ActiveState (SubState)`` and ``Result`` — the moment that is known.

    A unit seen in ``auto-restart``, ``failed``, ``inactive`` or ``deactivating``
    inside the window has already died: there is nothing to wait for
    (:data:`RESTART_NOT_UP`). Any other ``activating`` sub-state (``start``,
    ``start-pre``, …) is a unit still coming up, so the loop keeps reading until
    the deadline and reports what it finds there. A manager that stops answering
    mid-window leaves the unit's health UNKNOWN — not "exiting", not "restarted"
    — and is reported as :data:`RESTART_UNCONFIRMED`.
    """
    unit = f"{SERVICE_NAME}.service"
    deadline = time.monotonic() + _RESTART_SETTLE_SECS
    while True:
        state = _unit_state(user=user)
        if not state.reachable:
            return (
                RESTART_UNCONFIRMED,
                f"the {state.scope} manager stopped answering after the restart "
                f"({state.error}); whether {unit} is up could not be read",
            )
        last = f" (last result: {state.result})" if state.result else ""
        if not state.running or state.sub == "auto-restart" or state.active == "deactivating":
            return (
                RESTART_NOT_UP,
                f"{unit} is {state.active} ({state.sub}){last} after the restart — "
                f"the gateway exits as soon as it starts",
            )
        if time.monotonic() >= deadline:
            if state.up:
                return None
            return (
                RESTART_NOT_UP,
                f"{unit} is still {state.active} ({state.sub}){last} "
                f"{_RESTART_SETTLE_SECS:g}s after the restart",
            )
        time.sleep(_RESTART_POLL_SECS)


def _restart_scope(*, user: bool) -> ScopeRestart:
    """``systemctl restart`` the unit in one scope and confirm it came back up.

    A non-zero exit is classified by the unit's state right after it, never by
    the exit code alone: ``systemctl restart`` exits non-zero both when the
    manager did not run the job (an unprivileged caller and a system unit —
    "Interactive authentication required" — or a bus this shell cannot reach)
    and when it ran the job and the job FAILED (a start limit already hit, an
    ``ExecStartPre=`` that exits non-zero, a ``Type=notify`` unit that never
    signalled). The first leaves the unit as it was — still up, or unreadable —
    and is a REFUSAL, remedied by the same restart with the right privilege from
    the right shell: ``sudo systemctl restart kirocrew`` for the system unit,
    ``systemctl --user restart kirocrew`` for the user unit, never the other
    one (under ``sudo`` the user command addresses root's manager and fails
    with ``Unit kirocrew.service not found``). The second leaves the unit
    ``failed``, ``inactive`` or in ``activating (auto-restart)``: the gateway is
    NOT UP, a hand-run restart fails the same way, and the remedy is its
    journal. A zero exit is only the manager's word that the restart job ran;
    :func:`_confirm_up` decides whether the gateway is up.
    """
    unit = f"{SERVICE_NAME}.service"
    scope = "user" if user else "system"
    spelled = "systemctl --user" if user else "sudo systemctl"
    restart_hint = user_restart_command_hint() if user else system_restart_command_hint()
    res = _systemctl("restart", unit, user=user)
    if res.returncode != 0:
        diag = _first_line(res)
        after = _unit_state(user=user)
        if not after.reachable or after.up:
            return ScopeRestart(
                scope,
                False,
                reason=f"the {scope} manager refused the restart: {diag}",
                kind=RESTART_REFUSED,
                hint=restart_hint,
            )
        last = f" (last result: {after.result})" if after.result else ""
        return ScopeRestart(
            scope,
            False,
            reason=(
                f"`{spelled} restart {unit}` exited {res.returncode} ({diag}); "
                f"{unit} is {after.active} ({after.sub}){last}"
            ),
            kind=RESTART_NOT_UP,
            hint=_journal_hint(user=user),
        )
    outcome = _confirm_up(user=user)
    if outcome is None:
        return ScopeRestart(scope, True)
    kind, reason = outcome
    hint = _journal_hint(user=user) if kind == RESTART_NOT_UP else "kirocrew service status"
    return ScopeRestart(scope, False, reason=reason, kind=kind, hint=hint)


def restart() -> RestartReport:
    """Restart the service in every scope where it runs and confirm each came up.

    Single ``systemctl restart`` call per scope rather than ``stop`` + ``start``
    — smaller down-window, and the supervisor stays in charge of the lifecycle
    the whole time. ``Restart=always`` semantics in the unit are unaffected:
    ``systemctl restart`` is an explicit operator action, so the manager honors
    it regardless of restart policy.

    The report is per scope (:class:`RestartReport`), and ``ok`` only when every
    restarted unit is UP at the end of the settle window — not when ``systemctl``
    exited 0: for the ``Type=simple`` unit this module renders that exit says
    the process was forked, not that it lived, so a crash-looping gateway
    restarted into ``activating (auto-restart)`` would otherwise read as
    restarted. Each failure carries its kind: REFUSED (the manager did not run
    the job — unit unchanged; remedy: that scope's own restart command), NOT UP
    (the job ran, the gateway is not up; remedy: that scope's journal), or
    UNCONFIRMED (the manager stopped answering; remedy: `service status`). A
    unit running in no scope at all is an empty report (``attempted`` False):
    nothing was restarted — and a stopped, still-installed unit in the other
    scope is never started on the side, which selecting on "installed" instead
    of "running" would do.
    """
    scopes = _running_scopes()
    # Every scope gets its restart before the results are combined: stopping at
    # the first refusal would leave the other scope's gateway untouched while
    # reporting a failed restart.
    return RestartReport(tuple(_restart_scope(user=user) for user in scopes))


def status() -> str:
    """Return a human-readable status report covering BOTH systemd scopes.

    One headline per scope — ``system scope: …`` then ``user scope: …`` — stating
    ``not installed``, ``not reachable from this shell (…)``, or the unit's
    ``ActiveState (SubState)``, followed by the ``systemctl status`` block for
    each scope that actually has a unit. A scope with no unit never shows
    systemd's ``inactive (dead)`` for it: that line, printed for the system
    scope alone, is what makes a running user-scope gateway read as dead.

    Status is queryable without sudo. We avoid sudo here so
    ``kirocrew service status`` doesn't prompt for a password just to
    show whether the service is up.
    """
    sections: list[str] = []
    for user in (False, True):
        state = _unit_state(user=user)
        lines = [state.headline()]
        if state.installed:
            res = _systemctl(
                "status", f"{SERVICE_NAME}.service", "--no-pager", sudo=False, user=user
            )
            block = (res.stdout or res.stderr).rstrip("\n")
            if block:
                lines.append(block)
        sections.append("\n".join(lines))
    return "\n\n".join(sections)
