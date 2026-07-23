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

from kiro_crew.service.common import (
    SERVICE_NAME,
    kirocrew_bin,
    service_path,
)

log = logging.getLogger(__name__)

UNIT_PATH = Path(f"/etc/systemd/system/{SERVICE_NAME}.service")


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


def render_unit() -> str:
    """Render the systemd system-unit file contents.

    Runs the gateway as the invoking user (``User=``, ``Group=``) so it
    has access to ``$HOME/.kirocrew``, the user's config, etc. The PATH
    is set explicitly so subprocess invocations of git, node, etc.
    resolve the same way they would from an interactive shell.
    """
    bin_path = kirocrew_bin()
    user = _current_user()
    group = _current_group(user) if user else ""
    home = str(Path.home())
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
        f"User={user}\n"
        f"Group={group}\n"
        f"WorkingDirectory={home}\n"
        f"ExecStart={bin_path} gateway\n"
        "Restart=on-failure\n"
        "RestartSec=10\n"
        "TimeoutStopSec=20\n"
        # Pin a high open-file limit rather than inheriting the host's
        # ambient DefaultLimitNOFILE. Stock systemd defaults to 1024 — and
        # the frontend production build (vite/rollup) opens ~1000
        # lucide-react icon files concurrently, which exhausts a 1024 cap and
        # fails with `EMFILE: too many open files`. Pinning it here makes
        # agent-launched builds and other FD-hungry work survive regardless
        # of the host default.
        "LimitNOFILE=65536\n"
        f"Environment=HOME={home}\n"
        f"Environment=USER={user}\n"
        f"Environment=PATH={service_path(home)}\n"
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


def install() -> None:
    """Write the unit file and enable+start the service. Idempotent.

    Calls ``sudo`` to write the unit and to invoke ``systemctl``. Sudo
    will prompt for a password the first time (or when the cached
    ticket has expired) — that prompt appears on the user's terminal.
    No kirocrew / LLM / agent code runs under sudo: only ``tee`` and
    ``systemctl`` are invoked.

    Raises :class:`ServiceInstallError` with a human-readable message if
    a step fails. The CLI catches this and prints the message instead
    of letting a CalledProcessError surface.
    """
    user = _current_user()
    if not user:
        raise ServiceInstallError(
            "Could not determine current user (USER and LOGNAME both unset). "
            "Set $USER and re-run."
        )

    write_res = _write_unit_via_sudo(render_unit())
    if write_res.returncode != 0:
        raise ServiceInstallError(
            "Failed to write the unit file. The sudo step is required because "
            f"{UNIT_PATH} is owned by root.\n"
            f"   sudo install said: {(write_res.stderr or write_res.stdout).strip()}"
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
