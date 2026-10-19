"""What the Linux service unit says, and which account it runs as.

The unit and overrides paths (:data:`UNIT_PATH`, :data:`ENV_FILE_PATH`), the
service account (its ``User=``, primary group, uid, home and linger state), and
:func:`render_unit`, which turns them into the unit file. Nothing here writes a
file or drives a manager: :mod:`kiro_crew.service.linux_install` writes what this
module renders. The lowest owner of the family; it imports no other.

Part of the :mod:`kiro_crew.service.linux` family: callers and tests reach every name
through that facade, which keeps the module's patch seams whole
(:mod:`kiro_crew._facade_mirror`). In the docstrings and comments below, "this module"
means that family and "the module docstring" means the facade's, which carries the
systemd 219 commitment and the sudo scope.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from kiro_crew.gateway_lock import LIVE_HOLDER_EXIT_CODE
from kiro_crew.gateway_shutdown_budget import TOTAL_SHUTDOWN_BUDGET_SECS
from kiro_crew.platform_compat import trusted_system_bin
from kiro_crew.service import selinux
from kiro_crew.service.common import SERVICE_NAME, kirocrew_bin, service_environment, systemd_quote

_sd_quote = systemd_quote

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

    ``id`` is resolved from fixed system directories, never ``PATH``: this runs
    as root under ``sudo kirocrew service install``, where a ``PATH`` leading
    with an agent-writable directory would otherwise let a planted ``id`` shim
    execute with euid 0.
    """
    id_bin = trusted_system_bin("id")
    if id_bin is None:
        return user
    try:
        res = subprocess.run(
            [id_bin, "-gn", user],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
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


def _linger_enabled(user: str) -> bool | None:
    """Whether ``user``'s per-user systemd manager lingers past logout.

    ``True`` / ``False`` from ``loginctl show-user <user> -p Linger``; ``None``
    when it cannot be determined — no ``loginctl``, an unknown user, or an
    unrecognised value — so the caller stays quiet rather than guessing. Linger
    is a system-level logind property, so a plain (non ``--user``) ``loginctl``
    read answers it regardless of whether that account currently has a session.

    The ``Linger=`` value is parsed out of the ``Key=value`` output rather than
    taken from ``--value``: ``--value`` arrived in systemd 230, and this module
    commits to systemd 219 (see the module docstring), where that option is
    rejected and the read would fail. The canonical probe for the whole package;
    :func:`kiro_crew.cli_doctor._linger_enabled` delegates here.

    ``loginctl`` is resolved from fixed system directories, never ``PATH``: the
    system-install path runs as root under ``sudo``, where a ``PATH`` leading
    with an agent-writable directory would let a planted ``loginctl`` shim
    execute with euid 0.
    """
    loginctl = trusted_system_bin("loginctl")
    if loginctl is None:
        return None
    try:
        res = subprocess.run(
            [loginctl, "show-user", user, "-p", "Linger"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    val = ""
    for line in (res.stdout or "").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "Linger":
            val = value.strip().lower()
            break
    if val in ("yes", "true", "1"):
        return True
    if val in ("no", "false", "0"):
        return False
    return None


def linger_warning_for_service_account() -> str | None:
    """Operator warning for a system install whose service account has linger off.

    The system unit bakes ``XDG_RUNTIME_DIR=/run/user/<uid>`` and
    ``DBUS_SESSION_BUS_ADDRESS`` (see :func:`render_unit`), so an agent runtime
    scope is created under that account's ``user@<uid>.service``. With
    ``Linger=no`` logind stops the account's manager when its last login session
    ends: an in-flight scoped runtime gets ``SIGTERM`` and dies with it. New
    spawns do not fail — ``sandbox`` detects the gone manager and starts them
    UNSCOPED, which drops the cgroup-v2 memory and fork-count ceilings those
    scopes enforce, and logs each loss as a SECURITY warning. Linger keeps the
    account's manager running without a login, which is what a persistent system
    service needs to retain both the runtimes and their ceilings.

    Returns the warning string when linger is off for the resolved account, or
    ``None`` when it is on or cannot be determined (so the install reports
    nothing it is not sure of). This only describes a condition and names the
    fix; it never changes the install outcome.
    """
    user = _current_user()
    if not user or user == "root":
        return None
    if _linger_enabled(user) is not False:
        return None
    return (
        f"linger is off for {user}: this system service parents each agent "
        f"runtime under that account's user manager, which systemd stops when "
        f"{user}'s last login session ends — killing in-flight runtimes, and "
        f"starting later spawns unscoped so they lose their memory and "
        f"fork-count ceilings. Enable it so the service keeps the manager, and "
        f"the ceilings, without a login:\n"
        f"     sudo loginctl enable-linger {shlex.quote(user)}"
    )


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
    # System unit only: a user unit already runs in its login's context. Without
    # it the gateway runs as system_u and labels every cache it writes under the
    # user's home system_u (see selinux.installer_context).
    selinux_context = None if user_scope else selinux.installer_context(bin_path)
    selinux_line = f"SELinuxContext={selinux_context}\n" if selinux_context else ""
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
        + ("" if user_scope else f"User={user}\nGroup={group}\n")
        + selinux_line
        + f"WorkingDirectory={home}\n"
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
        # systemd 219 (<= 228) reads StartLimit* only from [Service]; the
        # [Unit] copy above covers systemd >= 229, which places them there.
        # Emitting both ensures the crash-loop cap works on all supported distros.
        # StartLimitInterval is the pre-229 key name; >= 229 accepts it as an alias.
        "StartLimitBurst=3\n"
        "StartLimitInterval=300\n"
        # ...but never against a home another gateway already serves. The lock
        # refusal for a LIVE holder (kiro_crew.gateway_lock) is a standing
        # condition for this unit: the incumbent keeps serving, and every
        # relaunch boots the stack only to meet the identical refusal, until
        # StartLimit* parks the unit `failed` -- or forever, on a unit without
        # them. That refusal exits LIVE_HOLDER_EXIT_CODE, and this line makes it
        # terminal: the unit goes `failed` once, with the refusal line visible in
        # the journal. Every other exit -- 1 for a lock failure a retry may find
        # cleared, the watchdog's and listener guard's own relaunch requests --
        # is still restarted. Value comes from kiro_crew.gateway_lock, the
        # module that decides the refusal, so the two cannot drift.
        f"RestartPreventExitStatus={LIVE_HOLDER_EXIT_CODE}\n"
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
