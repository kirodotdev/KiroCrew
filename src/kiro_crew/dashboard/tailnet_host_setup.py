"""Set Tailscale up on a headless Linux gateway host, from the dashboard.

The phone-access card's first steps (install Tailscale, start it, sign in) were
errands the operator ran on the machine itself, and the card only linked to
Tailscale's download page. On a remote host with no desktop, such as an EC2
Remote Crew reached over SSM, that link downloads Tailscale onto the browser's
own computer, which does nothing for the box the gateway runs on. Its users have
no terminal on the box, so the card had no path forward.

This module is that path, for one shape of host: Linux, no display, a distro
Tailscale publishes a package repository for, and a gateway user that may run
``sudo`` without a password (or is root). A launched EC2 Remote Crew is exactly
that: its gateway runs as ``ec2-user`` under systemd, and the image grants that
user passwordless ``sudo``. Every other host keeps the download link.

What runs is fixed. Every argv below is a literal, no value from a request
reaches it, and nothing goes through a shell. The repository file comes from
``pkgs.tailscale.com`` over HTTPS and the package manager checks the package's
signature with the key that repository names, the same steps Tailscale's own
install script takes. Sign-in uses no auth key: ``tailscale login`` makes the
daemon hold a login URL, which the card shows as a button the person opens in
their own browser. Its output is discarded, and the URL is never logged.

``tailscale set --operator=<gateway user>`` is the one lasting permission this
adds. It lets the gateway user run ``tailscale serve`` without ``sudo``, which is
what the card's later publish step and ``kirocrew tailnet up`` already need.
"""

from __future__ import annotations

import getpass
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from kiro_crew.bounded_read import bounded_read
from kiro_crew.dashboard import tailnet
from kiro_crew.platform.context import redact_log_via_context
from kiro_crew.sandbox import scrub_env

logger = logging.getLogger(__name__)

#: Tailscale's stable package repositories. A fixed origin: the distro id and
#: release that complete the URL are matched against :data:`_SUPPORTED` first.
_REPO_ROOT = "https://pkgs.tailscale.com/stable"

#: ``/etc/os-release`` ``ID`` -> (package manager, Tailscale's name for the OS).
#: Amazon Linux is only 2023: AL2 is end of life and ships yum, not dnf.
_SUPPORTED: dict[str, tuple[str, str]] = {
    "amzn": ("dnf", "amazon-linux"),
    "ubuntu": ("apt", "ubuntu"),
    "debian": ("apt", "debian"),
}

#: A distro release that may go into a repository URL: a codename or a number.
_RELEASE_RE = re.compile(r"^[a-z0-9][a-z0-9.]{0,31}\Z")

#: A gateway user name that may go into ``--operator=``.
_USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}\Z")

#: Environment variables that mean a desktop session is attached.
_DISPLAY_ENVS = ("DISPLAY", "WAYLAND_DISPLAY")

#: Absolute paths, so a root gateway never resolves a tool through ``PATH``.
_SUDO = "/usr/bin/sudo"
_TEE = "/usr/bin/tee"
_CHMOD = "/usr/bin/chmod"
_SYSTEMCTL = "/usr/bin/systemctl"

#: A repository file or keyring is a few KB; anything far larger is not one.
_MAX_DOWNLOAD_BYTES = 256 * 1024

_DOWNLOAD_TIMEOUT_SECS = 60
#: A package install on a fresh box (repo metadata plus the package) is slow.
_INSTALL_TIMEOUT_SECS = 600
_STEP_TIMEOUT_SECS = 120
#: How long the request waits for the daemon to hand out a login URL.
_AUTH_URL_WAIT_SECS = 20.0
#: How long a started ``tailscale login`` may wait for the person to approve.
_LOGIN_PROCESS_SECS = 30 * 60


@dataclass(frozen=True)
class HostPlan:
    """How to install Tailscale on this host."""

    manager: str  # "dnf" | "apt"
    #: ``https://pkgs.tailscale.com/stable/<os>/<release>``
    repo: str
    #: ``[]`` for a root gateway, else ``[_SUDO, "-n"]``.
    prefix: tuple[str, ...]


@dataclass(frozen=True)
class HostSetupResult:
    ok: bool
    detail: str
    #: The login URL the daemon holds, or ``""`` (already signed in, or none yet).
    auth_url: str = ""


def _read_os_release(path: str = "/etc/os-release") -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip()] = value.strip().strip("\"'")
    return out


def _repo_for(release: dict[str, str]) -> tuple[str, str] | None:
    """``(manager, repo URL)`` for an ``/etc/os-release`` mapping, or ``None``."""
    distro = release.get("ID", "")
    entry = _SUPPORTED.get(distro)
    if entry is None:
        return None
    manager, os_name = entry
    if distro == "amzn":
        version = release.get("VERSION_ID", "")
        if version != "2023":
            return None
    else:
        version = release.get("VERSION_CODENAME", "")
    if not _RELEASE_RE.match(version):
        return None
    return manager, f"{_REPO_ROOT}/{os_name}/{version}"


_IS_LINUX = sys.platform.startswith("linux")


def _euid() -> int:
    # getattr: os.geteuid does not exist on Windows.
    return getattr(os, "geteuid", lambda: -1)()


_SUDO_LOCK = threading.Lock()
#: ``sudo -n true``'s answer, asked once per process. shortcut: a sudo grant
#: added later is seen after a gateway restart; probe again if that matters.
_sudo_ok: bool | None = None


def _sudo_without_password() -> bool:
    global _sudo_ok
    with _SUDO_LOCK:
        if _sudo_ok is None:
            try:
                proc = subprocess.run(  # noqa: S603 - fixed argv
                    [_SUDO, "-n", "true"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                    env=scrub_env(),
                )
                _sudo_ok = proc.returncode == 0
            except (OSError, subprocess.SubprocessError):
                # A probe that could not run is no answer: ask again next time.
                return False
        return _sudo_ok


def host_plan(environ: dict[str, str] | None = None) -> HostPlan | None:
    """The install plan when this host can set Tailscale up itself, else ``None``.

    ``None`` keeps the card on the download link: not Linux, a desktop session
    attached (the person at this machine can install it there), a distro with
    no Tailscale repository here, or a gateway user that needs a password for
    ``sudo``. Never raises.
    """
    env = os.environ if environ is None else environ
    if not _IS_LINUX:
        return None
    if any(env.get(name, "").strip() for name in _DISPLAY_ENVS):
        return None
    repo = _repo_for(_read_os_release())
    if repo is None:
        return None
    if _euid() == 0:
        prefix: tuple[str, ...] = ()
    elif os.path.isfile(_SUDO) and _sudo_without_password():
        prefix = (_SUDO, "-n")
    else:
        return None
    return HostPlan(manager=repo[0], repo=repo[1], prefix=prefix)


def _auth_url_from_status(status: object) -> str:
    """The daemon's pending login URL from ``tailscale status --json``, or ``""``."""
    if not isinstance(status, dict):
        return ""
    raw = status.get("AuthURL")
    if not isinstance(raw, str) or not raw or len(raw) > 2048 or any(c.isspace() for c in raw):
        return ""
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme != "https" or not parts.netloc:
        return ""
    return raw


def current_auth_url() -> str:
    """The login URL this host's daemon is waiting on, or ``""``. Never raises."""
    return _auth_url_from_status(tailnet._run_json(["status", "--json"]))


def _run(argv: list[str], timeout: float, stdin_bytes: bytes | None = None) -> tuple[bool, str]:
    """Run one fixed step. ``(ok, why)``; *why* is the redacted error tail.

    *stdin_bytes*, when given, is the child's whole stdin; its stdout is
    discarded (``tee`` would echo the file back).
    """
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv,
            input=stdin_bytes,
            stdin=None if stdin_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.DEVNULL if stdin_bytes is not None else subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            env=scrub_env(),
        )
    except subprocess.TimeoutExpired:
        return False, f"`{' '.join(argv)}` did not finish in {int(timeout)} seconds."
    except OSError as exc:
        return False, f"`{' '.join(argv)}` could not run: {exc}"
    if proc.returncode == 0:
        return True, ""
    raw = proc.stderr or proc.stdout or b""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    tail = redact_log_via_context(text.strip())[-300:]
    return False, f"`{' '.join(argv)}` failed: {tail or f'exit {proc.returncode}'}"


def _download(url: str) -> bytes:
    """Fetch one small repository file over HTTPS, kept in memory."""
    if not url.startswith(_REPO_ROOT + "/"):
        raise ValueError(f"refusing a download outside {_REPO_ROOT}: {url}")
    with urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT_SECS) as response:  # nosemgrep
        data = bounded_read(response, _MAX_DOWNLOAD_BYTES + 1)
    if len(data) > _MAX_DOWNLOAD_BYTES:
        raise ValueError(f"{url} is larger than a repository file")
    return bytes(data)


def _write_as_root(sudo: list[str], data: bytes, target: str) -> tuple[bool, str]:
    """Write *data* to the fixed system path *target* as root, mode 0644.

    The bytes reach root on stdin and never through a file: a path the gateway
    user can write is a path an agent running as that user can swap before root
    reads it, so root is never handed one.
    """
    ok, why = _run([*sudo, _TEE, target], _STEP_TIMEOUT_SECS, stdin_bytes=data)
    if not ok:
        return False, why
    return _run([*sudo, _CHMOD, "0644", target], _STEP_TIMEOUT_SECS)


def _install(plan: HostPlan) -> tuple[bool, str]:
    """Add Tailscale's repository and install the package."""
    sudo = list(plan.prefix)
    if plan.manager == "dnf":
        files = [(f"{plan.repo}/tailscale.repo", "/etc/yum.repos.d/tailscale.repo")]
        steps = [[*sudo, "/usr/bin/dnf", "install", "-y", "tailscale"]]
    else:
        files = [
            (
                f"{plan.repo}.noarmor.gpg",
                "/usr/share/keyrings/tailscale-archive-keyring.gpg",
            ),
            (
                f"{plan.repo}.tailscale-keyring.list",
                "/etc/apt/sources.list.d/tailscale.list",
            ),
        ]
        steps = [
            [*sudo, "/usr/bin/apt-get", "update"],
            [*sudo, "/usr/bin/apt-get", "install", "-y", "tailscale"],
        ]
    for url, target in files:
        try:
            data = _download(url)
        except (OSError, ValueError) as exc:
            return False, f"Could not download {url}: {exc}"
        ok, why = _write_as_root(sudo, data, target)
        if not ok:
            return False, why
    for argv in steps:
        ok, why = _run(argv, _INSTALL_TIMEOUT_SECS)
        if not ok:
            return False, why
    return True, ""


_LOGIN_LOCK = threading.Lock()
_login_proc: subprocess.Popen[bytes] | None = None


def _start_login(cli: str) -> tuple[bool, str]:
    """Start ``tailscale login`` once, in the background, output discarded.

    It waits for the person to approve the login URL, so it outlives the request.
    A reaper thread ends it after :data:`_LOGIN_PROCESS_SECS`; the daemon keeps
    the pending login either way.
    """
    global _login_proc
    with _LOGIN_LOCK:
        if _login_proc is not None and _login_proc.poll() is None:
            return True, ""
        try:
            proc = subprocess.Popen(  # noqa: S603 - vetted absolute binary, fixed argv
                [cli, "login"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=tailnet._cli_env(),
                start_new_session=True,
            )
        except OSError as exc:
            return False, f"`tailscale login` could not run: {exc}"
        _login_proc = proc

    def _reap() -> None:
        try:
            proc.wait(timeout=_LOGIN_PROCESS_SECS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    threading.Thread(target=_reap, name="tailscale-login-reaper", daemon=True).start()
    return True, ""


def _wait_for_auth_url(
    wait_secs: float = _AUTH_URL_WAIT_SECS,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    deadline = clock() + wait_secs
    while True:
        url = current_auth_url()
        if url or clock() >= deadline:
            return url
        sleep(0.5)


_SETUP_LOCK = threading.Lock()


def run_host_setup(plan: HostPlan) -> HostSetupResult:
    """Install, start and sign this host in to Tailscale, as far as it needs.

    Each step runs only when the live state says it is missing, so a click on
    ``start_daemon`` or ``sign_in`` skips the install. One run at a time.
    """
    if not _SETUP_LOCK.acquire(blocking=False):
        return HostSetupResult(False, "Tailscale setup is already running on this machine.")
    try:
        return _run_host_setup(plan)
    finally:
        _SETUP_LOCK.release()


def _run_host_setup(plan: HostPlan) -> HostSetupResult:
    user = getpass.getuser()
    if not _USERNAME_RE.match(user):
        return HostSetupResult(False, f"The gateway user name {user!r} is not supported.")
    sudo = list(plan.prefix)

    if tailnet._cli_path() is None:
        ok, why = _install(plan)
        if not ok:
            return HostSetupResult(False, why)

    # The same vetted binary every other tailscale call on this host runs.
    cli = tailnet._cli_path()
    if cli is None:
        return HostSetupResult(False, tailnet.probe_daemon().detail)
    for argv in (
        [*sudo, _SYSTEMCTL, "enable", "--now", "tailscaled"],
        # Lets this user run `tailscale serve` and `tailscale login` without sudo.
        [*sudo, cli, "set", f"--operator={user}"],
    ):
        ok, why = _run(argv, _STEP_TIMEOUT_SECS)
        if not ok:
            return HostSetupResult(False, why)
    status = tailnet._run_json(["status", "--json"])
    state = status.get("BackendState") if isinstance(status, dict) else None
    if state == "Stopped":
        ok, why = _run([cli, "up"], _STEP_TIMEOUT_SECS)
        return HostSetupResult(ok, why)
    if state in tailnet._BACKEND_STATES_NEEDING_LOGIN:
        url = _auth_url_from_status(status)
        if not url:
            ok, why = _start_login(cli)
            if not ok:
                return HostSetupResult(False, why)
            url = _wait_for_auth_url()
        if not url:
            return HostSetupResult(
                False, "Tailscale did not offer a sign-in link yet. Try again in a moment."
            )
        return HostSetupResult(True, "", auth_url=url)
    return HostSetupResult(True, "")
