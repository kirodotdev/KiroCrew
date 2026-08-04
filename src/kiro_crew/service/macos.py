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
    launchd_live_program,
    service_environment,
)

log = logging.getLogger(__name__)

PLIST_DIR = Path.home() / "Library" / "LaunchAgents"
PLIST_PATH = PLIST_DIR / f"{LAUNCHD_LABEL}.plist"
LOG_DIR = Path.home() / "Library" / "Logs" / "KiroCrew"
STDOUT_LOG = LOG_DIR / "gateway.log"
STDERR_LOG = LOG_DIR / "gateway.err"
#: See :func:`kiro_crew.service.common.launchd_live_program` for why the agent
#: runs a generated launcher rather than the resolved binary.
LIVE_PROGRAM = Path(launchd_live_program())


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def render_plist() -> str:
    """Render the launchd LaunchAgent plist contents.

    ``ProgramArguments[0]`` is the :data:`LIVE_PROGRAM` launcher, not the
    resolved binary, so the agent can be repointed at another checkout (with its
    own working directory and PATH) without rewriting — and re-bootstrapping —
    the plist. :func:`install` renders a pass-through launcher aimed at
    :func:`kirocrew_bin` by default.
    """
    bin_path = _xml_escape(str(LIVE_PROGRAM))
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


def _sh_quote(raw: str) -> str:
    """POSIX single-quote *raw* for the generated launcher script."""
    return "'" + raw.replace("'", "'\\''") + "'"


def render_live_program(target_bin: str, *, working_dir: str | None = None,
                        path_prefix: "list[str] | None" = None) -> str:
    """Render the launcher the agent executes (see LIVE_PROGRAM).

    With no *working_dir* this is a pass-through to *target_bin* — what a plain
    ``service install`` needs. Dev Fleet's cutover supplies the target checkout's
    directory and venv-first PATH so the whole process tree, including anything
    that re-invokes ``kirocrew`` by name, resolves to the checkout that is now
    live. Without that, PATH-resolved subprocesses would keep hitting the old
    install while the gateway ran the new one.

    ``exec "$@"`` forwarding matters: the plist passes ``gateway`` as argv[1], so
    the launcher must hand its own arguments through unchanged.
    """
    lines = [
        "#!/bin/sh",
        "# Generated by KiroCrew (service install / Dev Fleet make-live).",
        "# Rewritten atomically on every change - do not edit by hand.",
    ]
    if working_dir:
        lines.append(f"cd {_sh_quote(working_dir)} || exit 1")
    if path_prefix:
        lines.append(f"PATH={_sh_quote(':'.join(path_prefix))}")
        lines.append("export PATH")
    lines.append(f"exec {_sh_quote(target_bin)} \"$@\"")
    return "\n".join(lines) + "\n"


def write_live_program(contents: str, path: Path | None = None) -> None:
    """Write the live-gateway launcher atomically and make it executable.

    *path* defaults to :data:`LIVE_PROGRAM`. Dev Fleet's backend passes the path
    it reports as the live program, so the writer and the reader can never
    disagree about which file is authoritative.

    Atomic because the agent may be kickstarted at any moment: a partially
    written launcher would exec a truncated script. The temp file is chmod'd
    BEFORE the rename so the file is never visible at the final path without its
    exec bit.

    ``0o700``, not ``0o755``: launchd runs the agent as the owning user, so
    nobody else needs to read or execute it, and it lives in that user's own
    application-support directory.
    """
    dest = path or LIVE_PROGRAM
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=dest.name + ".", suffix=".tmp", dir=str(dest.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(contents)
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- launchd EXECUTES this file as the ProgramArguments entry, so the exec bit is required and the rule's suggested 0o644 would stop the agent from spawning at all. 0o700 is the tightest mode that still works: owner-only, in the owner's own application-support directory, and the agent runs as that same user.  # noqa: E501
        os.chmod(tmp_path, 0o700)  # fmt: skip
        os.replace(tmp_path, dest)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def install() -> None:
    """Write the plist and load+start the agent.

    Idempotent — unloads first if already loaded so the new plist takes
    effect without leaving the prior agent stale. Also (re)writes
    :data:`LIVE_PROGRAM`: the plist executes that launcher, so an absent or
    non-executable one would leave the agent with nothing to run. Re-running
    ``service install`` is therefore also the documented repair for a deleted
    application-support directory.

    Raises :class:`ServiceInstallError` with a human-readable message if
    ``launchctl load`` fails. The CLI catches this and prints the message
    instead of letting a CalledProcessError surface.
    """
    PLIST_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # Write the launcher BEFORE loading: a loaded agent whose Program does not
    # exist fails to spawn and KeepAlive would retry it in a tight loop.
    write_live_program(render_live_program(kirocrew_bin()))
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
    """Unload and remove the plist and the live-gateway launcher. Idempotent."""
    if PLIST_PATH.exists():
        _launchctl("unload", "-w", str(PLIST_PATH))
        PLIST_PATH.unlink()
    # Drop the launcher too: leaving it behind would make a later `status` look
    # like a partially installed service.
    try:
        LIVE_PROGRAM.unlink()
    except OSError:
        pass


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
    """Restart the running agent. Returns True iff the restart was accepted.

    Uses ``launchctl kickstart -k gui/<uid>/<label>``: launchd performs the kill
    and the respawn ITSELF, so this is safe to call from inside the process being
    restarted.

    The previous implementation (``unload`` then ``load``) could not do that. Run
    from the gateway, the ``unload`` half SIGTERMs the caller, so the ``load``
    half never executes and the agent stays down — the exact failure mode that
    made Dev Fleet's Restart unimplementable on macOS. ``launchctl restart`` is
    deprecated and behaves like ``stop``, which ``KeepAlive`` treats as an
    unsuccessful exit and respawns immediately, so it re-runs the OLD job
    definition rather than re-reading anything.

    Returns False when the plist is absent or launchd rejects the kickstart, so
    callers never report a restart that never happened.
    """
    if not PLIST_PATH.exists():
        return False
    uid = getattr(os, "getuid", lambda: -1)()
    target = f"gui/{uid}/{LAUNCHD_LABEL}"
    return _launchctl("kickstart", "-k", target).returncode == 0


def status() -> str:
    """Return a human-readable status block from launchctl."""
    res = _launchctl("list", LAUNCHD_LABEL)
    if res.returncode != 0:
        return f"kirocrew service is not loaded ({res.stderr.strip() or 'no entry'})\n"
    return res.stdout
