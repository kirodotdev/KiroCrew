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
import subprocess
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
    home = str(Path.home())
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


def _sudo_run(
    *args: str,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command under sudo, capturing output.

    Sudo prompts for a password on first use; subsequent calls within
    the cached ticket window run silently. All three call sites
    (``install``, ``uninstall``, ``stop``) are interactive user
    commands invoked from a TTY, so we always allow the prompt.
    """
    return subprocess.run(
        ["sudo", *args],
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
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
                "sudo",
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
    user = _current_user()
    if not user:
        raise ServiceInstallError(
            "Could not determine current user (USER and LOGNAME both unset). "
            "Set $USER and re-run."
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


def uninstall() -> None:
    """Stop, disable, and remove the unit. Idempotent."""
    # Use a non-sudo `test -e` so we don't prompt for a password
    # when the unit isn't even present.
    if not UNIT_PATH.exists():
        return
    _systemctl("stop", f"{SERVICE_NAME}.service")
    _systemctl("disable", f"{SERVICE_NAME}.service")
    _sudo_run("rm", "-f", str(UNIT_PATH))
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
