"""What the Linux service unit says, and which account it runs as.

The unit and overrides paths (:data:`UNIT_PATH`, :data:`ENV_FILE_PATH`), the
directory that says systemd booted this host (:data:`_SYSTEMD_BOOTED_DIR`), the
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

# systemd as the init system leaves this directory behind for exactly this
# question (sd_booted(3) checks nothing else). A host without it runs no system
# manager, so no unit can start or be running there: `install` refuses it before
# writing anything, and it is the one case in which `uninstall` removes a unit
# file without the manager's word that it is stopped.
_SYSTEMD_BOOTED_DIR = Path("/run/systemd/system")

# Where a user-scope unit belongs, per systemd.unit(5) — RELATIVE to the service
# account's home, deliberately. Referenced only in the printed remedy; nothing in
# this module writes here. It is not spelled "~/.config/..." because "~" resolves
# against whoever pastes the command, and `service install` is documented to run
# under sudo — so a tilde would silently name root's home in the one shell the
# operator is most likely to be sitting in.
USER_UNIT_SUBDIR = Path(".config/systemd/user")

# The unit's crash-loop cap: at most this many starts within this many seconds,
# then the manager parks it ``failed``. The stale-unit check compares what the
# manager loaded against these (``linux_systemctl._start_limit_line``).
START_LIMIT_BURST = 3
START_LIMIT_INTERVAL_SECS = 300


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
    ``USER`` / ``LOGNAME`` for a non-sudo invocation, then to the passwd entry
    of the process's uid: a non-interactive ``ssh host kirocrew service install``
    sets neither variable, yet still runs as the account it logged in as. uid 0
    resolves to ``"root"`` there too. May return ``"root"``, or ``""`` when the
    uid has no passwd entry (or the platform has no uid); :func:`install`
    refuses to render a unit from either.
    """
    sudo_user = os.environ.get("SUDO_USER", "").strip()
    if sudo_user and sudo_user != "root":
        return sudo_user
    env_user = os.environ.get("USER") or os.environ.get("LOGNAME")
    if env_user:
        return env_user
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        return ""
    try:
        import pwd  # Unix-only; lazy so this module still imports on Windows.

        return pwd.getpwuid(getuid()).pw_name or ""
    except Exception:
        return ""


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

    From ``loginctl show-user <user> -p Linger`` (a system-level property, so it
    answers whether or not the account has a session); ``None`` when unknown, so
    the caller stays quiet. :func:`kiro_crew.cli_doctor._linger_enabled` delegates here.

    Parses ``Key=value`` output instead of ``--value``, which needs systemd 230;
    this module supports systemd 219. ``loginctl`` comes from fixed system
    directories, never ``PATH``: the system install runs as root under ``sudo``,
    where a planted shim on an agent-writable ``PATH`` would run with euid 0.
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

    Agent runtime scopes live under the account's ``user@<uid>.service`` (the
    unit bakes ``XDG_RUNTIME_DIR`` and ``DBUS_SESSION_BUS_ADDRESS``). With
    ``Linger=no`` logind stops that manager at the last logout: in-flight runtimes
    get ``SIGTERM``, and ``sandbox`` starts later spawns UNSCOPED, losing their
    cgroup-v2 memory and fork-count ceilings (logged as a SECURITY warning).

    ``None`` when linger is on or unknown. Advisory only: never changes the
    install outcome.
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

    Runs the gateway as the invoking user (``User=``, ``Group=``) so it reaches
    ``$HOME/.kiro/crew``, with an explicit ``PATH`` so git, node, etc. resolve as
    in an interactive shell.

    No ``AppArmorProfile=`` directive: the profile attaches by path to the
    launcher (:func:`install_apparmor_profile`), and systemd's ``change_onexec``
    transition would silently override that attachment.

    ``user_scope`` renders the ``systemctl --user`` variant that
    :func:`selinux_refusal` only prints, so the remedy cannot drift from the real
    unit. The per-user manager rejects ``User=``/``Group=``, and has no
    ``multi-user.target``, so it installs to ``default.target``.
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
    # Agent shells, MCP servers and crons drive `systemctl --user` (pods), and a
    # system unit inherits no login-session environment: without these every
    # pod command fails with "Failed to connect to bus: No medium found".
    # Kept out of service_environment(): /run/user/<uid> has no launchd
    # equivalent. A numeric uid, not `%U`: _sd_quote escapes `%` to `%%`.
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
        f"StartLimitBurst={START_LIMIT_BURST}\n"
        f"StartLimitIntervalSec={START_LIMIT_INTERVAL_SECS}\n"
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
        # `always`, not `on-failure`: the stale-asset watchdog exits 0 when an
        # update prunes the running install and relies on the supervisor to start
        # a fresh process; `on-failure` never restarts an exit 0, so the gateway
        # would stay down. `always` still honors an explicit `systemctl
        # stop`/`disable`, and StartLimit* above caps a tight loop.
        "Restart=always\n"
        "RestartSec=10\n"
        # systemd 219 (<= 228) reads StartLimit* only from [Service]; the
        # [Unit] copy above covers systemd >= 229, which places them there.
        # Emitting both ensures the crash-loop cap works on all supported distros.
        # StartLimitInterval is the pre-229 key name; >= 229 accepts it as an alias.
        f"StartLimitBurst={START_LIMIT_BURST}\n"
        f"StartLimitInterval={START_LIMIT_INTERVAL_SECS}\n"
        # ...but never against a home another gateway already serves. The
        # LIVE-holder lock refusal (kiro_crew.gateway_lock) does not clear on a
        # relaunch, so its exit code is terminal: the unit fails once with the
        # refusal in the journal. Every other exit (1 for a lock failure a retry
        # may clear, the watchdog's and listener guard's relaunches) restarts.
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
        # No core file: with core_pattern piped to systemd-coredump, a crashed
        # multi-GB gateway cannot exit, or restart, until its core has streamed,
        # which can take that service's whole 5-minute cap.
        "LimitCORE=0\n"
        f"{env_lines}"
        "\n"
        "[Install]\n"
        # multi-user.target is a SYSTEM target; the per-user manager has no such
        # unit, so a user-scope install must want default.target instead or
        # `systemctl --user enable` fails.
        + ("WantedBy=default.target\n" if user_scope else "WantedBy=multi-user.target\n")
    )
