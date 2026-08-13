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

Sudo scope: only the systemctl/tee invocations in this file run under
sudo. The Python interpreter that imports MCP / LLM / agent code never
runs as root. The actual gateway runs as ``User=$USER`` once started.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from kiro_crew.gateway_shutdown_budget import TOTAL_SHUTDOWN_BUDGET_SECS
from kiro_crew.service import apparmor
from kiro_crew.service.common import (
    SERVICE_NAME,
    kirocrew_bin,
    service_environment,
)

log = logging.getLogger(__name__)

UNIT_PATH = Path(f"/etc/systemd/system/{SERVICE_NAME}.service")

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


def _sd_quote(value: str) -> str:
    """Double-quote a value for a systemd unit token.

    systemd splits unquoted ``ExecStart`` / ``Environment=`` tokens on
    whitespace, so any path or locale containing a space (a spaced
    ``KIROCREW_SERVICE_BIN`` / ``KIROCREW_KIRO_BIN`` override, ``PATH`` entries
    with spaces, etc.) must be wrapped in double quotes. Inside a double-quoted
    token systemd honours C-style escapes, so ``\\`` and ``"`` are
    backslash-escaped.

    ``%`` is escaped to ``%%`` *first* (before quoting): systemd performs
    specifier expansion on ``ExecStart`` / ``Environment=`` values regardless of
    quoting — an unescaped ``%h`` / ``%i`` in a path (e.g. a directory literally
    named ``100%``) would be replaced with the home dir / instance name and the
    exec would target the wrong path. See systemd.unit(5) "Specifiers",
    systemd.service(5) "Command lines", and systemd.exec(5) ``Environment=``.

    A control character — most dangerously a newline — is rejected outright
    (``ValueError``) rather than escaped. A double-quoted systemd token does not
    span physical lines, so a newline in an operator override
    (``KIROCREW_SERVICE_BIN`` / ``KIROCREW_KIRO_BIN``) would terminate the value
    and let the remainder be parsed as fresh unit directives — e.g. injecting
    ``User=root`` + a replacement ``ExecStart`` into the root-owned unit that
    ``sudo … service install`` writes (a privilege-escalation vector). No
    legitimate executable path or locale contains a C0/DEL control character, so
    refusing them is strictly safer than trying to escape them.
    """
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError(
            "refusing to render a systemd unit value containing a control "
            "character (possible unit-file injection): " + repr(value)
        )
    escaped = (
        value.replace("%", "%%")
        .replace("\\", "\\\\")
        .replace('"', '\\"')
    )
    return f'"{escaped}"'


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


def render_unit(apparmor_profile: str = "") -> str:
    """Render the systemd system-unit file contents.

    Runs the gateway as the invoking user (``User=``, ``Group=``) so it
    has access to ``$HOME/.kiro/crew``, the user's config, etc. The PATH
    is set explicitly so subprocess invocations of git, node, etc.
    resolve the same way they would from an interactive shell.

    A system unit inherits no login-session environment, so the per-user
    systemd instance is also wired up explicitly — see the ``XDG_RUNTIME_DIR`` /
    ``DBUS_SESSION_BUS_ADDRESS`` lines below.
    """
    bin_path = kirocrew_bin()
    user = _current_user()
    group = _current_group(user) if user else ""
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
        # "-" = best-effort: a missing or unloaded profile must never stop the
        # gateway from starting. Without it the sandbox simply cannot be built,
        # which fails closed per-spawn rather than bricking the service.
        f"{('AppArmorProfile=-' + apparmor_profile + chr(10)) if apparmor_profile else ''}"
        f"User={user}\n"
        f"Group={group}\n"
        f"WorkingDirectory={home}\n"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
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
        "WantedBy=multi-user.target\n"
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
    geteuid = getattr(os, "geteuid", None)
    is_root = geteuid is not None and geteuid() == 0
    if not is_root and shutil.which("sudo") is None:
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


def _systemctl(*args: str, sudo: bool = True) -> subprocess.CompletedProcess[str]:
    if sudo:
        return _sudo_run("systemctl", *args)
    return subprocess.run(
        ["systemctl", *args], capture_output=True, text=True, check=False
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
        with os.fdopen(fd, "w") as fh:
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
        with os.fdopen(fd, "w") as fh:
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


def install() -> apparmor.ProfileOutcome:
    """Write the unit file and enable+start the service. Idempotent.

    Calls ``sudo`` to write the unit and to invoke ``systemctl``. Sudo
    will prompt for a password the first time (or when the cached
    ticket has expired) — that prompt appears on the user's terminal.
    No kirocrew / LLM / agent code runs under sudo: only ``tee`` and
    ``systemctl`` are invoked.

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

    # Decide before writing the unit: the directive has to be in the unit that
    # systemd reloads, and the profile must be loaded before the restart.
    needs_profile, profile_reason = apparmor.should_install()
    write_res = _write_unit_via_sudo(
        render_unit(apparmor.PROFILE_NAME if needs_profile else "")
    )
    if write_res.returncode != 0:
        raise ServiceInstallError(
            "Failed to write the unit file. The sudo step is required because "
            f"{UNIT_PATH} is owned by root.\n"
            f"   sudo install said: {(write_res.stderr or write_res.stdout).strip()}"
        )

    # Seed the operator-editable overrides file (create-if-absent), so a later
    # `KIROCREW_PORT=...` edit + restart works without re-installing.
    _seed_env_file()

    # Before daemon-reload/enable/restart: the AppArmorProfile= directive is
    # applied by systemd at unit START, so the profile must already be loaded or
    # the first gateway process comes up unprofiled.
    profile_outcome = install_apparmor_profile() if needs_profile else apparmor.ProfileOutcome(
        False, f"AppArmor profile not needed: {profile_reason}"
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
        )

    return profile_outcome


def install_apparmor_profile() -> apparmor.ProfileOutcome:
    """Install the userns AppArmor profile when this host needs one.

    Deliberately NOT fatal: a gateway running without the profile is the status
    quo, whereas aborting a service install because a hardening step failed is a
    regression. The caller prints the outcome and continues either way.
    """
    # uid/gid, not sys.executable: the verification drops privilege back to the
    # invoking user inside the profile and runs a TRUSTED system python, because
    # the venv interpreter is user-writable and must never execute under sudo.
    return apparmor.install(
        _install_file_via_sudo, _sudo_run_checked, _sudo_capture, os.getuid(), os.getgid()
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


def uninstall() -> None:
    """Stop, disable, and remove the unit. Idempotent."""
    # Use a non-sudo `test -e` so we don't prompt for a password
    # when the unit isn't even present.
    if not UNIT_PATH.exists():
        return
    _require_privilege()
    _systemctl("stop", f"{SERVICE_NAME}.service")
    _systemctl("disable", f"{SERVICE_NAME}.service")
    _sudo_run("rm", "-f", str(UNIT_PATH))
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


def is_active() -> bool:
    """Return True if the systemd service is currently active.

    ``is-active`` does not require sudo to query state, so we use the
    non-sudo path.
    """
    res = _systemctl("is-active", f"{SERVICE_NAME}.service", sudo=False)
    return res.returncode == 0 and res.stdout.strip() == "active"


def stop() -> None:
    """Stop the running service without disabling it."""
    _systemctl("stop", f"{SERVICE_NAME}.service")


def restart() -> bool:
    """Atomically restart the service. Returns True iff systemctl succeeded.

    Single ``systemctl restart`` call rather than ``stop`` + ``start`` —
    smaller down-window, and the supervisor stays in charge of the
    lifecycle the whole time. ``Restart=on-failure`` semantics in the
    unit are unaffected: ``systemctl restart`` is an explicit operator
    action, so the manager honors it regardless of restart policy.

    A system-scope restart requires root/polkit; an unprivileged caller
    gets a non-zero exit ("Interactive authentication required"). We
    return that outcome so callers do not report a restart that never
    happened.
    """
    return _systemctl("restart", f"{SERVICE_NAME}.service").returncode == 0


def status() -> str:
    """Return a human-readable status block from systemctl.

    Status is queryable without sudo. We avoid sudo here so
    ``kirocrew service status`` doesn't prompt for a password just to
    show whether the service is up.
    """
    res = _systemctl(
        "status", f"{SERVICE_NAME}.service", "--no-pager", sudo=False
    )
    return res.stdout or res.stderr
