"""launchd LaunchAgent generation and control for macOS.

The plist lives at ``~/Library/LaunchAgents/dev.kirocrew.gateway.plist``
and is loaded via ``launchctl load -w``. The service starts on user login
and is restarted on crash by ``KeepAlive``.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path

from kiro_crew.service.common import (
    LAUNCHD_LABEL,
    kirocrew_bin,
    service_environment,
)

log = logging.getLogger(__name__)

PLIST_DIR = Path.home() / "Library" / "LaunchAgents"
PLIST_PATH = PLIST_DIR / f"{LAUNCHD_LABEL}.plist"
LOG_DIR = Path.home() / "Library" / "Logs" / "KiroCrew"
STDOUT_LOG = LOG_DIR / "gateway.log"
STDERR_LOG = LOG_DIR / "gateway.err"


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def render_plist() -> str:
    """Render the launchd LaunchAgent plist contents."""
    bin_path = _xml_escape(kirocrew_bin())
    home_str = str(Path.home())
    out_log = _xml_escape(str(STDOUT_LOG))
    err_log = _xml_escape(str(STDERR_LOG))
    env_entries = "".join(
        f"        <key>{_xml_escape(key)}</key>\n"
        f"        <string>{_xml_escape(value)}</string>\n"
        for key, value in service_environment(home_str).items()
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "    <key>Label</key>\n"
        f"    <string>{LAUNCHD_LABEL}</string>\n"
        "    <key>ProgramArguments</key>\n"
        "    <array>\n"
        f"        <string>{bin_path}</string>\n"
        "        <string>gateway</string>\n"
        "    </array>\n"
        "    <key>RunAtLoad</key>\n"
        "    <true/>\n"
        "    <key>KeepAlive</key>\n"
        "    <dict>\n"
        "        <key>SuccessfulExit</key>\n"
        "        <false/>\n"
        "    </dict>\n"
        "    <key>EnvironmentVariables</key>\n"
        "    <dict>\n"
        f"{env_entries}"
        "    </dict>\n"
        f"    <key>StandardOutPath</key>\n"
        f"    <string>{out_log}</string>\n"
        f"    <key>StandardErrorPath</key>\n"
        f"    <string>{err_log}</string>\n"
        "</dict>\n"
        "</plist>\n"
    )


class ServiceInstallError(RuntimeError):
    """Raised when LaunchAgent install can't proceed without manual user action."""


def _write_plist_atomic(contents: str) -> None:
    """Write the plist atomically.

    Writes to a sibling temp file in the same directory, then
    ``os.replace`` to swap into place. ``os.replace`` is atomic on POSIX
    when source and destination are on the same filesystem, so a SIGINT
    or crash mid-write leaves either the old plist or no plist at all —
    never a partial XML document that ``launchctl load`` would reject.
    """
    fd, tmp_path = tempfile.mkstemp(
        prefix=PLIST_PATH.name + ".", suffix=".tmp", dir=str(PLIST_DIR)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(contents)
        os.replace(tmp_path, PLIST_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def _launchctl(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", *args],
        capture_output=True,
        text=True,
        check=check,
    )


def install() -> None:
    """Write the plist and load+start the agent.

    Idempotent — unloads first if already loaded so the new plist takes
    effect without leaving the prior agent stale.

    Raises :class:`ServiceInstallError` with a human-readable message if
    ``launchctl load`` fails. The CLI catches this and prints the message
    instead of letting a CalledProcessError surface.
    """
    PLIST_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if PLIST_PATH.exists():
        _launchctl("unload", "-w", str(PLIST_PATH))
    _write_plist_atomic(render_plist())
    load_res = _launchctl("load", "-w", str(PLIST_PATH))
    if load_res.returncode != 0:
        raise ServiceInstallError(
            f"`launchctl load` failed: "
            f"{(load_res.stderr or load_res.stdout).strip()}\n"
            f"   Plist: {PLIST_PATH}\n"
            f"   Tail the agent logs at {STDOUT_LOG} / {STDERR_LOG} for details."
        )


def uninstall() -> None:
    """Unload and remove the plist. Idempotent."""
    if PLIST_PATH.exists():
        _launchctl("unload", "-w", str(PLIST_PATH))
        PLIST_PATH.unlink()


def is_active() -> bool:
    """Return True if launchd reports the agent loaded with a PID."""
    res = _launchctl("list", LAUNCHD_LABEL)
    if res.returncode != 0:
        return False
    # `launchctl list <label>` prints a plist-ish dict with PID = <int>;
    # an unloaded agent returns nonzero. A loaded-but-not-running agent
    # has PID = "-" instead of a number.
    for line in res.stdout.splitlines():
        line = line.strip()
        if line.startswith('"PID"'):
            return "=" in line and line.split("=")[-1].strip().rstrip(";").isdigit()
    return True  # `list <label>` succeeded; treat as active even if PID line absent


def stop() -> None:
    """Stop the running agent.

    ``launchctl stop`` only sends SIGTERM, which the plist's
    ``KeepAlive={SuccessfulExit: false}`` treats as an unsuccessful exit
    and immediately restarts — so a plain ``stop`` is effectively a
    no-op. Use ``unload`` (without ``-w``) so the agent stops and stays
    stopped for the current session, but reloads automatically on next
    login. This mirrors ``systemctl stop`` semantics on Linux.
    """
    if PLIST_PATH.exists():
        _launchctl("unload", str(PLIST_PATH))


def restart() -> bool:
    """Restart the running agent. Returns True iff the reload succeeded.

    ``launchctl restart`` was deprecated and behaves like ``stop`` —
    KeepAlive treats SIGTERM as an unsuccessful exit and respawns
    immediately, so the user-observed effect is a flicker, not a
    re-read of the plist. Use a transient ``unload`` (without ``-w``,
    so persistent enable state is unchanged) followed by ``load``
    (also without ``-w``) so the agent picks up any plist changes and
    starts a fresh process. This mirrors ``systemctl restart``
    semantics on Linux.

    Returns False when the plist is absent or the ``load`` step exits
    non-zero, so callers do not report a restart that never happened.
    """
    if not PLIST_PATH.exists():
        return False
    _launchctl("unload", str(PLIST_PATH))
    return _launchctl("load", str(PLIST_PATH)).returncode == 0


def status() -> str:
    """Return a human-readable status block from launchctl."""
    res = _launchctl("list", LAUNCHD_LABEL)
    if res.returncode != 0:
        return f"kirocrew service is not loaded ({res.stderr.strip() or 'no entry'})\n"
    return res.stdout
