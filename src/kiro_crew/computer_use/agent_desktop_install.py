"""Install and readiness for the agent-desktop computer-use driver.

The driver is the ``agent-desktop`` npm package, pinned to
:data:`~kiro_crew.computer_use.agent_desktop_locate.PINNED_VERSION` and installed
into the product-owned prefix :func:`INSTALL_PREFIX`. One npm command into a
prefix we own, every child run through :mod:`~kiro_crew.computer_use.install_runner`'s
``_run`` so a gateway shutdown can kill it through an :class:`InstallScope`, and
every byte of npm output
redacted before it reaches a log or the dashboard.

npm runs with the user's own registry configuration. No registry URL is named
here: a host that routes npm through a mirror or a proxy keeps doing so.

The tarball ships one native binary per platform. npm runs with
``--ignore-scripts``: the package's postinstall only copies the bundled binary
into place (and downloads from GitHub when it is missing), so :func:`install`
does the copy itself and nothing reaches the network but the registry. Trust is
decided by :func:`agent_desktop_locate.verify` against the pinned digests, the
one pin the driver also checks, never by the package's own ``.sha256`` files.

Nothing here runs on the event loop: every function blocks on the filesystem or
on a subprocess, and the dashboard handler calls them through a worker thread.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Any, Callable

from kiro_crew.computer_use import agent_desktop_locate
from kiro_crew.computer_use.install_runner import (
    INTERRUPTED_RC,
    TIMEOUT_RC,
    _run,
    redact_install_output,
)
from kiro_crew.computer_use.types import PLATFORM_LINUX, PLATFORM_MACOS, PLATFORM_WINDOWS
from kiro_crew.env import find_node_tool

logger = logging.getLogger(__name__)

PACKAGE_NAME = "agent-desktop"

#: npm talks to the registry and the package is ~30 MB of binaries.
NPM_INSTALL_TIMEOUT_S = 600.0
#: ``permissions`` answers from local TCC state; anything slower is wedged.
PROBE_TIMEOUT_S = 15.0
#: ``--guide`` opens System Settings and returns; it does not wait for a grant.
GUIDE_TIMEOUT_S = 30.0

LOG_TAIL_CAP = 4000
_PACKAGE_JSON_MAX_BYTES = 1024 * 1024

PERM_GRANTED = "granted"
PERM_DENIED = "denied"
PERM_UNKNOWN = "unknown"
_DENIED_STATES = frozenset({"denied", "not_granted", "missing", "restricted", "not_determined"})

LogCallback = Callable[[str], None]

#: Our platform id -> the OS word in the tarball's binary names.
_OS_KEYS = {PLATFORM_MACOS: "darwin", PLATFORM_LINUX: "linux", PLATFORM_WINDOWS: "win32"}


def package_dir() -> Path:
    """Where npm puts the package under the product prefix."""
    return agent_desktop_locate.INSTALL_PREFIX() / "node_modules" / PACKAGE_NAME


def platform_binary_name() -> str | None:
    """The bundled binary for THIS host, named like the tarball names it.

    The OS comes from ``backend.platform_id_for_current_os()``, the package's one
    platform answer, rather than ``sys.platform``, so a test can pick the Linux (or
    any) branch on whatever runner it is on.
    """
    from kiro_crew.computer_use.backend import platform_id_for_current_os

    os_key = _OS_KEYS.get(platform_id_for_current_os())
    if os_key is None:
        return None
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        arch = "arm64"
    elif machine in ("x86_64", "amd64", "x64"):
        arch = "x64"
    else:
        return None
    suffix = ".exe" if os_key == "win32" else ""
    return f"agent-desktop-{os_key}-{arch}{suffix}"


def installed_version() -> str | None:
    """``version`` from the installed package.json, or ``None``."""
    path = package_dir() / "package.json"
    try:
        if path.stat().st_size > _PACKAGE_JSON_MAX_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = data.get("version") if isinstance(data, dict) else None
    return version if isinstance(version, str) and version else None


def state_dir() -> Path:
    """agent-desktop's own state directory, inside the product prefix."""
    return agent_desktop_locate.INSTALL_PREFIX() / "state"


def _helper_binary_name() -> str | None:
    name = platform_binary_name()
    if name is None or name.startswith("agent-desktop-win32"):
        return None
    os_arch = name[len("agent-desktop-") :]
    helper = "macos-helper" if os_arch.startswith("darwin") else "linux-clipboard-owner"
    return f"agent-desktop-{helper}-{os_arch}"


def _copy_executable(source: Path, destination: Path) -> None:
    """Copy *source* over *destination* atomically, mode 0755."""
    temporary = destination.with_name(destination.name + ".tmp")
    shutil.copyfile(source, temporary)
    os.chmod(temporary, 0o755)
    os.replace(temporary, destination)


def place_platform_binary() -> tuple[bool, str]:
    """Do the postinstall's local step: put this host's bundled CLI at ``bin/agent-desktop``.

    The package's ``bin/agent-desktop`` is a Node wrapper until then; npm's
    ``.bin`` link points at it, so after the copy that link runs the native CLI.
    The helper binary the CLI execs is copied beside it under its plain name.
    """
    name = platform_binary_name()
    if name is None:
        return False, f"no agent-desktop build for {sys.platform}/{platform.machine()}"
    bin_dir = package_dir() / "bin"
    source = bin_dir / name
    if not source.is_file():
        return False, f"bundled binary missing: {name}"
    target = bin_dir / ("agent-desktop.exe" if name.endswith(".exe") else "agent-desktop")
    try:
        _copy_executable(source, target)
        helper = _helper_binary_name()
        if helper is not None and (bin_dir / helper).is_file():
            plain = helper.rsplit("-", 2)[0]
            _copy_executable(bin_dir / helper, bin_dir / plain)
    except OSError as exc:
        return False, f"cannot place {name}: {exc}"
    return True, f"placed {name}"


def verify_installed() -> tuple[str | None, str | None]:
    """``(path, None)`` when the located binary is the pinned build, else ``(None, reason)``."""
    path = agent_desktop_locate.locate()
    if path is None:
        return None, "agent-desktop is not installed"
    reason = agent_desktop_locate.verify(path)
    if reason is not None:
        return None, reason
    return path, None


def _normalize_permission(raw: Any) -> str:
    state = raw.get("state") if isinstance(raw, dict) else raw
    if not isinstance(state, str):
        return PERM_UNKNOWN
    state = state.strip().lower()
    if state == PERM_GRANTED:
        return PERM_GRANTED
    if state in _DENIED_STATES:
        return PERM_DENIED
    return PERM_UNKNOWN


def _unknown_permissions() -> dict[str, str]:
    return {"accessibility": PERM_UNKNOWN, "screen_recording": PERM_UNKNOWN}


def parse_permissions(stdout: str) -> dict[str, str]:
    """Map ``agent-desktop permissions`` JSON to the two rows the status reports.

    The CLI prints ``{"ok": true, "data": {"accessibility": {"state": ...}, ...}}``.
    Anything else (not JSON, ``ok`` false, a missing row) reads as ``unknown``:
    the probe is advisory, and a parse failure must not claim a denial.
    """
    try:
        payload = json.loads(stdout.strip() or "null")
    except ValueError:
        return _unknown_permissions()
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return _unknown_permissions()
    data = payload.get("data")
    if not isinstance(data, dict):
        return _unknown_permissions()
    return {
        "accessibility": _normalize_permission(data.get("accessibility")),
        "screen_recording": _normalize_permission(data.get("screen_recording")),
    }


def probe_permissions(binary: str) -> dict[str, str]:
    """Run ``agent-desktop permissions`` and read its answer; never raises."""
    try:
        rc, out, _err = _run([binary, "permissions"], PROBE_TIMEOUT_S)
    except Exception:  # noqa: BLE001 - advisory probe
        logger.debug("agent-desktop permissions probe failed", exc_info=True)
        return _unknown_permissions()
    if rc != 0:
        return _unknown_permissions()
    return parse_permissions(out)


def readiness() -> dict[str, Any]:
    """Installed? which version? where? and what the OS has granted.

    ``installed`` means :func:`locate` finds a binary AND :func:`verify` accepts
    it. A present binary that fails verification is reported as not installed
    and is never run, so the dashboard offers a reinstall instead.
    """
    path, _reason = verify_installed()
    installed = path is not None
    return {
        "installed": installed,
        "version": installed_version() if installed else None,
        "pinned": agent_desktop_locate.PINNED_VERSION,
        "path": path,
        "permissions": probe_permissions(path) if path else _unknown_permissions(),
    }


def _emit(on_log: LogCallback | None, text: str) -> None:
    if on_log is None or not text:
        return
    try:
        on_log(text)
    except Exception:  # noqa: BLE001 - progress must never fail the install
        logger.debug("agent-desktop install log callback failed", exc_info=True)


def _fail(name: str, rc: int, detail: str) -> dict[str, Any]:
    return {
        "ok": False,
        "step": name,
        "returncode": rc,
        "error": redact_install_output(detail)[-LOG_TAIL_CAP:],
        "interrupted": rc == INTERRUPTED_RC,
        "timed_out": rc == TIMEOUT_RC,
    }


def install(on_log: LogCallback | None = None) -> dict[str, Any]:
    """``npm install --prefix <INSTALL_PREFIX> agent-desktop@<PINNED>``, then verify.

    Returns ``{"ok": True, "version", "path"}`` or a failure carrying the step
    name and the redacted tail of what failed. *on_log* receives redacted
    output lines for the job's log tail.
    """
    npm = find_node_tool("npm")
    if npm is None:
        return _fail("npm", 127, "npm not found; install Node.js 18 or newer first")
    prefix = agent_desktop_locate.INSTALL_PREFIX()
    try:
        prefix.mkdir(parents=True, exist_ok=True, mode=0o700)
        state_dir().mkdir(exist_ok=True, mode=0o700)
    except OSError as exc:
        return _fail("prefix", 1, f"cannot create {prefix}: {exc}")
    spec = f"{PACKAGE_NAME}@{agent_desktop_locate.PINNED_VERSION}"
    argv = [
        npm,
        "install",
        "--prefix",
        str(prefix),
        "--ignore-scripts",
        "--no-fund",
        "--no-audit",
        spec,
    ]
    _emit(on_log, f"$ npm install --prefix {prefix} --ignore-scripts {spec}")
    rc, out, err = _run(argv, NPM_INSTALL_TIMEOUT_S)
    _emit(on_log, redact_install_output(f"{out}\n{err}".strip()))
    if rc != 0:
        return _fail("npm-install", rc, err.strip() or out.strip() or "npm install failed")
    version = installed_version()
    if version != agent_desktop_locate.PINNED_VERSION:
        return _fail(
            "version",
            1,
            f"installed {version!r}, expected {agent_desktop_locate.PINNED_VERSION!r}",
        )
    placed, detail = place_platform_binary()
    _emit(on_log, detail)
    if not placed:
        return _fail("place", 1, detail)
    path, reason = verify_installed()
    if path is None:
        return _fail("verify", 1, reason or "verification failed")
    _emit(
        on_log, f"verified {path} against the pinned {agent_desktop_locate.PINNED_VERSION} digests"
    )
    _reset_backend()
    return {"ok": True, "version": version, "path": path}


def _reset_backend() -> None:
    """Drop the shared backend so the next build finds the installed agent-desktop."""
    try:
        from kiro_crew.computer_use.backend import reset_shared_backend

        reset_shared_backend()
    except Exception:  # noqa: BLE001 - the install itself succeeded
        logger.warning("could not reset the computer-use backend after install", exc_info=True)


def open_permissions_guide() -> dict[str, Any]:
    """Run ``agent-desktop permissions --guide`` (macOS System Settings flow)."""
    if sys.platform != "darwin":
        return {"ok": False, "error": "the permissions guide is macOS only"}
    path, reason = verify_installed()
    if path is None:
        return {"ok": False, "error": reason}
    rc, out, err = _run([path, "permissions", "--guide"], GUIDE_TIMEOUT_S)
    if rc != 0:
        text = err.strip() or out.strip() or f"exit {rc}"
        return {"ok": False, "error": redact_install_output(text)[-LOG_TAIL_CAP:]}
    return {"ok": True, "error": None}
