"""Cross-platform Kiro CLI prerequisite detection, install, and login.

The public KiroCrew provider is KiroACP-only, so a healthy, authenticated
``kiro-cli`` is a hard runtime prerequisite.  This module owns the fixed,
operator-triggered setup operations used by the dashboard:

* discover and validate an installed Kiro CLI;
* download the official HTTPS installer and execute it without a shell string;
* start Kiro's device-flow login and surface its HTTPS sign-in URL.

No command, argument, URL, or filesystem target is accepted from an HTTP
request.  The dashboard handlers expose only ``status``, ``install``, and
``login`` verbs.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import hmac
import json
import logging
import ntpath
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir
from kiro_crew.env import augmented_path, resolve_krb5_ccname
from kiro_crew.kiro_cli import (
    find_kiro_cli_candidates,
    known_kiro_cli_dirs,
)
from kiro_crew.sandbox import (
    resource_limit_preexec,
    sandboxed_spawn_argv,
    scrub_agent_denied_env,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

OFFICIAL_INSTALL_URL = "https://cli.kiro.dev/install"
OFFICIAL_WINDOWS_INSTALL_URL = "https://cli.kiro.dev/install.ps1"
OFFICIAL_INSTALL_DOCS_URL = "https://kiro.dev/docs/cli/installation/"

_MAX_INSTALLER_BYTES = 5 * 1024 * 1024
_MAX_INSTALLER_REDIRECTS = 3
_INSTALLER_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_CAPTURED_OUTPUT = 64 * 1024
_MAX_VISIBLE_DETAIL = 4_000
_DOWNLOAD_TIMEOUT_SECS = 60
_INSTALL_TIMEOUT_SECS = 5 * 60
_LOGIN_TIMEOUT_SECS = 10 * 60
_PROBE_TIMEOUT_SECS = 10
_PROBE_CACHE_SECS = 2.0
_SESSION_GUARD_REPROBE_SECS = 30.0
_TERMINATION_GRACE_SECS = 2.0
_WINDOWS_DESCENDANT_POLL_SECS = 0.05
_HTTPS_URL_RE = re.compile(r"https://[^\s<>\"']+")
_UNSAFE_LOGIN_URL_RE = re.compile(r"[\\\x00-\x1f\x7f]")
_TRUSTED_LOGIN_HOSTS = frozenset({"app.kiro.dev", "view.awsapps.com"})
_TRUSTED_INSTALLER_HOSTS = frozenset({"cli.kiro.dev"})
_INSTALLER_SHA256 = {
    "posix": "91a21bfa05cd7b58601cb83e0f1f187a9d0084726e5b824d4a4cf60306250908",
    "win32": "2af3e4bb56f4fcce9244fe2f395805a5cf383ce683774664bcb444417eae1d3e",
}
_POSIX_INSTALLER_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
_KIRO_AUTH_SANDBOX_MODE = "standard"
_UNVERIFIED_SANDBOX_MODE = "strict"
_SETUP_COMPLETE_FILENAME = ".kiro_cli_setup_complete"
_BINARY_TRUST_FILENAME = ".kiro_cli_binary_trust.json"
_PROCESS_GROUP_SUPERVISOR = str(Path(__file__).with_name("_process_group_supervisor.py"))
_PROCESS_GROUP_SUPERVISOR_ERROR = "Kiro process-group supervisor is unavailable"
try:
    _PROCESS_GROUP_SUPERVISOR_CODE = Path(_PROCESS_GROUP_SUPERVISOR).read_text(encoding="utf-8")
except OSError:
    _PROCESS_GROUP_SUPERVISOR_CODE = ""
_AUTH_STAGING_RELATIVE = Path(".kiro") / "crew-auth-staging"
_AUTH_PUBLISH_LOCK_FILENAME = ".publish.lock"
_ACP_EXECUTABLE_SNAPSHOT_RELATIVE = Path("run") / "kiro-cli-snapshots"
_BINARY_TRUST_VERSION = 1
_MFD_EXEC = 0x0010
FAKE_ACP_TEST_MODE_ENV = "KIROCREW_FAKE_ACP_TEST_MODE"
_PACKAGED_FAKE_ACP_BACKEND = str(Path(__file__).with_name("testing") / "fake_acp_backend.py")
_MAX_AUTH_EXECUTABLE_BYTES = 512 * 1024 * 1024
_MAX_AUTH_STORE_FILE_BYTES = 64 * 1024 * 1024
_AUTH_STORE_READ_ERROR = "Kiro identity file could not be read safely"
_AUTH_SQLITE_FILES = (
    "data.sqlite3",
    "data.sqlite3-wal",
    "data.sqlite3-shm",
    "data.sqlite3-journal",
)
# Process-lifetime pins for explicit operator overrides. The prerequisite
# service records the canonical path + digest before any agent session starts;
# ACP spawn consumes the same pin so a later agent write cannot turn a stale
# readiness result into execution of replacement bytes.
_OPERATOR_OVERRIDE_ATTESTATIONS: dict[str, str] = {}

_SAFE_ENV_KEYS = frozenset(
    {
        "APPDATA",
        "DBUS_SESSION_BUS_ADDRESS",
        "DISPLAY",
        "HOME",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "LOGNAME",
        "NO_PROXY",
        "PATH",
        "ProgramFiles",
        "PROGRAMFILES",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SystemRoot",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USER",
        "USERPROFILE",
        "WAYLAND_DISPLAY",
        "WINDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)
_PROBE_ENV_KEYS = frozenset(
    {
        "APPDATA",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "LOGNAME",
        "PATH",
        "ProgramFiles",
        "PROGRAMFILES",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SystemRoot",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USER",
        "USERPROFILE",
        "WINDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
    }
)


@dataclass
class ProcessResult:
    """Bounded result from one fixed child process."""

    ok: bool
    output: str = ""
    returncode: int | None = None
    timed_out: bool = False
    error: str = ""


@dataclass
class PrerequisiteStatus:
    """Last known Kiro CLI readiness state."""

    platform: str
    installed: bool = False
    authenticated: bool = False
    ready: bool = False
    can_auto_install: bool = False
    can_login: bool = False
    repair_required: bool = False
    initial_setup_complete: bool = False
    docs_url: str = OFFICIAL_INSTALL_DOCS_URL


@dataclass
class OperationStatus:
    """Dashboard-visible state for the current/most-recent setup operation."""

    kind: str = ""
    status: str = "idle"
    message: str = ""
    detail: str = ""
    url: str = ""
    error: str = ""


@dataclass(frozen=True)
class _AuthStoreMapping:
    """One real Kiro identity store mapped into the temporary auth home."""

    source: Path
    staged_relative: Path
    filenames: tuple[str, ...]


@dataclass(frozen=True)
class _AuthWorkspace:
    """Temporary credential-minimal home for a trusted Kiro CLI auth call."""

    root: Path
    env: dict[str, str]
    mappings: tuple[_AuthStoreMapping, ...]
    source_digests: dict[str, str]


@dataclass(frozen=True)
class TrustedAcpExecutableSnapshot:
    """OS-bound immutable executable handle for one trusted ACP launch."""

    launch_path: str
    fd: int | None = None
    expected_sha256: str | None = None
    cleanup_path: str | None = None


class PrerequisiteBusyError(RuntimeError):
    """Raised when a second setup mutation is requested while one is active."""


ProcessRunner = Callable[..., Awaitable[ProcessResult]]
InstallerDownloader = Callable[[str], Awaitable[bytes]]
AuditWriter = Callable[..., Awaitable[None]]


def _platform_label(platform_name: str) -> str:
    if platform_name == "darwin":
        return "macOS"
    if platform_name == "win32":
        return "Windows"
    if platform_name.startswith("linux"):
        return "Linux"
    return platform_name or "Unknown"


def _append_capped(current: str, chunk: str) -> str:
    combined = current + str(chunk or "").replace("\r", "")
    if len(combined) <= _MAX_CAPTURED_OUTPUT:
        return combined
    return combined[-_MAX_CAPTURED_OUTPUT:]


def _sanitize_detail(text: str) -> str:
    safe, _ = redact_exfiltration_urls(str(text or ""))
    safe, _ = redact_credentials(safe)
    return safe[-_MAX_VISIBLE_DETAIL:]


def _canonical_candidate(path: str) -> str:
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def _is_runnable_executable(path: str, platform_name: str | None = None) -> bool:
    """True if *path* resolves to an executable regular file on this platform.

    The single trust primitive for the "runs + valid login" model: a Kiro CLI
    that can be executed is eligible for sign-in and ACP launch, regardless of
    install source, owner, or fixed path.
    """

    return platform_compat.is_executable_file(
        _canonical_candidate(path),
        platform_name=platform_name,
    )


def _bounded_file_sha256(path: Path) -> str | None:
    """Return a digest for one bounded regular file, or ``None`` when absent."""

    content = _read_bounded_regular_file(path)
    return hashlib.sha256(content).hexdigest() if content is not None else None


def _binary_sha256(path: str) -> str:
    """Hash one regular executable without following a final symlink."""

    canonical = _canonical_candidate(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(canonical, flags)
    digest = hashlib.sha256()
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > _MAX_AUTH_EXECUTABLE_BYTES
        ):
            raise OSError("Kiro CLI candidate is not a bounded regular executable")
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest()


def _copy_verified_executable_to_fd(
    source: str,
    destination_fd: int,
    expected_sha256: str | None,
) -> None:
    """Copy canonical executable bytes into an already-open private descriptor."""

    canonical = _canonical_candidate(source)
    source_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    source_flags |= getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(canonical, source_flags)
    digest = hashlib.sha256()
    try:
        metadata = os.fstat(source_fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > _MAX_AUTH_EXECUTABLE_BYTES
        ):
            raise ValueError("Kiro CLI is not a bounded regular executable")
        os.lseek(destination_fd, 0, os.SEEK_SET)
        os.ftruncate(destination_fd, 0)
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            pending = memoryview(chunk)
            while pending:
                written = os.write(destination_fd, pending)
                if written <= 0:
                    raise OSError("could not snapshot the Kiro CLI executable")
                pending = pending[written:]
        os.fsync(destination_fd)
        if expected_sha256 and not hmac.compare_digest(expected_sha256, digest.hexdigest()):
            raise ValueError("Kiro CLI provenance changed before credential access")
        platform_compat.fchmod_safe(destination_fd, 0o500)
        os.lseek(destination_fd, 0, os.SEEK_SET)
    finally:
        os.close(source_fd)


def _copy_verified_auth_executable(
    source: str,
    destination_dir: Path,
    expected_sha256: str | None,
    *,
    prefix: str = "kiro-cli-auth-",
) -> str:
    """Snapshot a verified executable into the agent-protected runtime dir.

    The snapshot keeps the SOURCE BASENAME (e.g. ``kiro-cli``) rather than a
    random ``mkstemp`` name: a multiplexer launcher such as
    ``~/.toolbox/bin/kiro-cli`` dispatches on its argv[0] basename, so a copy
    named ``kiro-cli-auth-XXXX`` would run as the wrong tool and fail with
    "Command doesn't appear to be associated with any tool". The copy lives in a
    unique owner-only subdir (``mkdtemp``) so preserving the fixed basename still
    can't collide across concurrent calls.
    """

    destination_dir.mkdir(parents=True, exist_ok=True)
    if platform_compat.IS_POSIX:
        platform_compat.chmod_safe(str(destination_dir), 0o700)
    else:
        platform_compat.restrict_to_owner(str(destination_dir))
    holder = tempfile.mkdtemp(prefix=prefix, dir=str(destination_dir))
    if not platform_compat.IS_POSIX:
        platform_compat.restrict_to_owner(holder)
    # Preserve the basename the CALLER resolved (e.g. the ``kiro-cli`` symlink
    # name), NOT the realpath — a multiplexer dispatches on argv[0], and its
    # realpath (``toolbox-exec``) is exactly the name that fails to dispatch.
    target = os.path.join(holder, os.path.basename(source) or "kiro-cli")
    destination_fd = -1
    try:
        open_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        destination_fd = os.open(target, open_flags, 0o700)
        _copy_verified_executable_to_fd(source, destination_fd, expected_sha256)
        os.close(destination_fd)
        destination_fd = -1
        if not platform_compat.IS_POSIX:
            platform_compat.restrict_to_owner(target)
        return target
    except Exception:
        if destination_fd >= 0:
            os.close(destination_fd)
        with contextlib.suppress(OSError):
            os.unlink(target)
        with contextlib.suppress(OSError):
            os.rmdir(holder)
        raise


def _register_operator_override_attestation(path: str, digest: str | None) -> None:
    """Remember the first gateway-start digest for one explicit override."""

    if not path or not digest:
        return
    key = os.path.normcase(_canonical_candidate(path))
    # First observation wins for the lifetime of this process. Reconstructing a
    # service after the file changes must not silently bless the new bytes.
    _OPERATOR_OVERRIDE_ATTESTATIONS.setdefault(key, digest)


def _recorded_trust_digest(trust_path: Path, canonical: str) -> str | None:
    """Return the pinned sha256 recorded for *canonical*, or ``None``.

    Single reader for the ``.kiro_cli_binary_trust.json`` attestation: parse the
    file, require the current schema version and an exact path match, and return
    the recorded 64-char digest. Callers that must prove the on-disk bytes still
    match re-hash the candidate and ``hmac.compare_digest`` against this value;
    callers that only need the recorded pin use it directly.
    """

    try:
        payload = json.loads(trust_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    expected = str(payload.get("sha256", ""))
    if (
        payload.get("version") != _BINARY_TRUST_VERSION
        or os.path.normcase(str(payload.get("path", ""))) != os.path.normcase(canonical)
        or len(expected) != 64
    ):
        return None
    return expected


def _trusted_acp_binary_digest(
    executable: str,
    *,
    data_home: Path,
    platform_name: str,
    environ: MutableMapping[str, str],
) -> str | None:
    """Return the digest ACP may execute for a runnable Kiro CLI, else ``None``.

    Trust is "it runs" — install source, owner, and fixed path do not gate ACP
    launch, so a toolbox / Homebrew / self-updated CLI is accepted like any
    other (KiroCrew is not the authority on where Kiro CLI is installed). The
    returned digest
    still pins the exact bytes for the exec-time integrity snapshot (sealed
    memfd on Linux, verified copy on macOS), so a swap between resolve and exec
    is still caught; ``None`` means the file is not a runnable executable.
    """

    del data_home, environ  # provenance no longer gates ACP launch
    canonical = _canonical_candidate(executable)
    if not _is_runnable_executable(canonical, platform_name):
        return None
    try:
        return _binary_sha256(canonical)
    except (OSError, ValueError):
        return None


def snapshot_trusted_acp_executable(
    executable: str,
    *,
    data_home: Path | None = None,
    platform_name: str | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> TrustedAcpExecutableSnapshot:
    """Return an integrity-checked, OS-bound executable snapshot for ACP.

    Trust is "the CLI runs" — install source, owner, and fixed path do not gate
    launch, so a toolbox / Homebrew / self-updated Kiro CLI launches like any
    other. The snapshot still pins the
    resolved bytes so a swap between resolve and exec is caught: Linux executes
    a write-sealed ``memfd`` of the exact bytes, falling back to a verified
    private copy when the interpreter lacks ``os.memfd_create`` (glibc < 2.27
    portable builds); because Mach-O cannot launch reliably through ``/dev/fd``,
    macOS executes a verified private copy; Windows launches the resolved path
    in place.
    """

    platform_name = platform_name or sys.platform
    active_environ = environ if environ is not None else os.environ
    if platform_name == "win32":
        return TrustedAcpExecutableSnapshot(launch_path=_canonical_candidate(executable))
    active_home = data_home if data_home is not None else config_dir()
    expected = _trusted_acp_binary_digest(
        executable,
        data_home=active_home,
        platform_name=platform_name,
        environ=active_environ,
    )
    if not expected:
        raise ValueError("Kiro CLI is not a runnable executable for ACP execution")

    canonical = _canonical_candidate(executable)
    if platform_name == "darwin":
        override = active_environ.get("KIROCREW_KIRO_BIN", "")
        packaged_fake_test_mode = bool(
            active_environ.get(FAKE_ACP_TEST_MODE_ENV) == "1"
            and override
            and os.path.normcase(canonical)
            == os.path.normcase(_canonical_candidate(_PACKAGED_FAKE_ACP_BACKEND))
        )
        if packaged_fake_test_mode:
            # The offline E2E harness opts into one exact, packaged executable
            # (a source-tree Python entry point) and launches it in place.
            return TrustedAcpExecutableSnapshot(
                launch_path=canonical,
                expected_sha256=expected,
            )
        snapshot_path = _copy_verified_auth_executable(
            # Pass the UNRESOLVED path so the copy keeps the caller's basename
            # (e.g. ``kiro-cli``): a multiplexer launcher dispatches on argv[0],
            # and the copier re-canonicalizes internally to read+pin the exact
            # resolved bytes, so byte integrity is unaffected. Passing
            # ``canonical`` here would name the copy ``toolbox-exec`` and break
            # ACP spawn for a toolbox CLI that just signed in.
            executable,
            active_home / _ACP_EXECUTABLE_SNAPSHOT_RELATIVE,
            expected,
            prefix="kiro-cli-acp-",
        )
        return TrustedAcpExecutableSnapshot(
            launch_path=snapshot_path,
            expected_sha256=expected,
            cleanup_path=snapshot_path,
        )
    if not platform_name.startswith("linux"):
        raise ValueError("ACP executable snapshots are unsupported on this POSIX platform")

    memfd_create = getattr(os, "memfd_create", None)
    if not callable(memfd_create):
        # Some Linux CPython builds — notably the portable python-build-standalone
        # interpreters shipped by mise/pyenv, compiled against a glibc that predates
        # the memfd_create(3) wrapper (glibc < 2.27) — omit os.memfd_create even
        # though the running kernel supports the syscall. Rather than fail every
        # ACP spawn, degrade to the same swap-safe mechanism macOS uses: a
        # sha256-verified private copy in the agent-protected 0700 snapshot dir,
        # launched in place. The sealed in-memory fd is lost, but the resolved
        # bytes are still pinned, so a swap between resolve and exec is caught.
        snapshot_path = _copy_verified_auth_executable(
            # Pass the UNRESOLVED path so the copy keeps the caller's basename
            # (e.g. ``kiro-cli``): a multiplexer launcher dispatches on argv[0],
            # and the copier re-canonicalizes internally to read+pin the exact
            # resolved bytes, so byte integrity is unaffected. Passing
            # ``canonical`` here would name the copy ``toolbox-exec`` and break
            # ACP spawn for a toolbox CLI that just signed in.
            executable,
            active_home / _ACP_EXECUTABLE_SNAPSHOT_RELATIVE,
            expected,
            prefix="kiro-cli-acp-",
        )
        return TrustedAcpExecutableSnapshot(
            launch_path=snapshot_path,
            expected_sha256=expected,
            cleanup_path=snapshot_path,
        )

    snapshot_fd = -1
    try:
        memfd_flags = (
            getattr(os, "MFD_CLOEXEC", 0x0001)
            | getattr(os, "MFD_ALLOW_SEALING", 0x0002)
            | _MFD_EXEC
        )
        try:
            snapshot_fd = memfd_create("kiro-cli-acp", memfd_flags)
        except OSError as exc:
            if exc.errno != errno.EINVAL:
                raise
            snapshot_fd = memfd_create("kiro-cli-acp", memfd_flags & ~_MFD_EXEC)
        _copy_verified_executable_to_fd(canonical, snapshot_fd, expected)
        platform_compat.seal_memfd(snapshot_fd)
        launch_path = f"/proc/self/fd/{snapshot_fd}"
        return TrustedAcpExecutableSnapshot(
            launch_path=launch_path,
            fd=snapshot_fd,
            expected_sha256=expected,
        )
    except Exception:
        if snapshot_fd >= 0:
            os.close(snapshot_fd)
        raise


def _read_bounded_regular_file(path: Path) -> bytes | None:
    """Read an allowlisted auth-store file with size and symlink defenses."""

    if path.is_symlink():
        return None
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(path), flags)
    except OSError:
        return None
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_AUTH_STORE_FILE_BYTES:
            return None
        output = bytearray()
        while len(output) <= _MAX_AUTH_STORE_FILE_BYTES:
            chunk = os.read(fd, min(1024 * 1024, _MAX_AUTH_STORE_FILE_BYTES + 1 - len(output)))
            if not chunk:
                return bytes(output)
            output.extend(chunk)
        return None
    finally:
        os.close(fd)


def _atomic_write_secret_bytes(path: Path, content: bytes) -> None:
    """Atomically restore one bounded Kiro identity file with owner-only mode."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            platform_compat.fchmod_safe(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        platform_compat.restrict_to_owner(str(path))
    except Exception:
        if fd >= 0:
            os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def _atomic_restore_sqlite(source: Path, destination: Path) -> None:
    """Checkpoint a staged SQLite store into one atomic destination file.

    The staged CLI may leave WAL/SHM/journal sidecars behind. Copying those
    files one at a time can publish a mixed generation after a failed or
    interrupted login. SQLite's backup API reads the complete staged
    generation (including its WAL) into one standalone database, which is
    then published with a single ``os.replace``.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(destination.parent), suffix=".sqlite.tmp")
    os.close(fd)
    try:
        source_uri = f"{source.resolve().as_uri()}?mode=ro"
        with contextlib.closing(sqlite3.connect(source_uri, uri=True)) as source_db:
            with contextlib.closing(sqlite3.connect(temporary)) as destination_db:
                source_db.backup(destination_db)
                # Publish a self-contained rollback-journal database. Carrying
                # WAL mode into the live path would immediately recreate a
                # sidecar and defeat the single-file atomic publication.
                destination_db.execute("PRAGMA journal_mode=DELETE")
                destination_db.commit()
        platform_compat.chmod_safe(temporary, 0o600)
        if destination.exists():
            # First checkpoint the live generation and move it out of WAL
            # mode. If publication stops here, the previous credentials remain
            # complete and readable; stale WAL bytes can no longer be replayed
            # over the replacement database.
            with contextlib.closing(sqlite3.connect(destination)) as live_db:
                live_db.execute("PRAGMA wal_checkpoint(FULL)")
                journal_mode = live_db.execute("PRAGMA journal_mode=DELETE").fetchone()
                if not journal_mode or str(journal_mode[0]).lower() != "delete":
                    raise sqlite3.OperationalError(
                        "could not prepare the live Kiro identity database for replacement"
                    )
                live_db.commit()
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(f"{destination}{suffix}")
            if sidecar.exists():
                sidecar.unlink()
        os.replace(temporary, destination)
        platform_compat.restrict_to_owner(str(destination))
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def _auth_store_mappings(
    platform_name: str,
    home: Path,
    environ: MutableMapping[str, str],
) -> tuple[_AuthStoreMapping, ...]:
    """Return only Kiro identity stores, never the surrounding credential dirs."""

    mappings = [
        _AuthStoreMapping(
            source=home / ".aws" / "sso" / "cache",
            staged_relative=Path(".aws") / "sso" / "cache",
            filenames=("kiro-auth-token*.json",),
        )
    ]
    app_names = ("kiro-cli", "amazon-q")
    if platform_name == "darwin":
        for app_name in app_names:
            mappings.append(
                _AuthStoreMapping(
                    source=home / "Library" / "Application Support" / app_name,
                    staged_relative=Path("Library") / "Application Support" / app_name,
                    filenames=_AUTH_SQLITE_FILES,
                )
            )
    elif platform_name == "win32":
        local_app_data = Path(environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        for app_name in app_names:
            mappings.append(
                _AuthStoreMapping(
                    source=local_app_data / app_name,
                    staged_relative=Path("AppData") / "Local" / app_name,
                    filenames=_AUTH_SQLITE_FILES,
                )
            )
    else:
        data_home = Path(environ.get("XDG_DATA_HOME") or home / ".local" / "share")
        for app_name in app_names:
            mappings.append(
                _AuthStoreMapping(
                    source=data_home / app_name,
                    staged_relative=Path(".local") / "share" / app_name,
                    filenames=_AUTH_SQLITE_FILES,
                )
            )
    return tuple(mappings)


def _ensure_auth_staging_parent(home: Path) -> Path:
    """Create the fixed sandbox-hidden parent before agent sessions can start."""

    staging_parent = home / _AUTH_STAGING_RELATIVE
    staging_parent.mkdir(parents=True, exist_ok=True)
    if staging_parent.is_symlink() or not staging_parent.is_dir():
        raise OSError("Kiro auth staging root is not a private directory")
    if platform_compat.IS_POSIX:
        platform_compat.chmod_safe(str(staging_parent), 0o700)
    else:
        platform_compat.restrict_to_owner(str(staging_parent))
    return staging_parent


def _prepare_auth_workspace(
    platform_name: str,
    home: Path,
    environ: MutableMapping[str, str],
    base_env: dict[str, str],
) -> _AuthWorkspace:
    """Build a sandbox-hidden HOME containing only Kiro identity artifacts."""

    staging_parent = _ensure_auth_staging_parent(home)
    root = Path(tempfile.mkdtemp(prefix="auth-", dir=str(staging_parent)))
    try:
        if platform_compat.IS_POSIX:
            platform_compat.chmod_safe(str(root), 0o700)
        else:
            platform_compat.restrict_to_owner(str(root))
        mappings = _auth_store_mappings(platform_name, home, environ)
        source_digests: dict[str, str] = {}
        for mapping in mappings:
            for pattern in mapping.filenames:
                for source in mapping.source.glob(pattern):
                    content = _read_bounded_regular_file(source)
                    if content is None:
                        raise OSError(_AUTH_STORE_READ_ERROR)
                    source_digests[str(source)] = hashlib.sha256(content).hexdigest()
                    _atomic_write_secret_bytes(
                        root / mapping.staged_relative / source.name,
                        content,
                    )

        env = dict(base_env)
        env.update(
            {
                "HOME": str(root),
                "USERPROFILE": str(root),
                "XDG_CACHE_HOME": str(root / ".cache"),
                "XDG_CONFIG_HOME": str(root / ".config"),
                "XDG_DATA_HOME": str(root / ".local" / "share"),
                "APPDATA": str(root / "AppData" / "Roaming"),
                "LOCALAPPDATA": str(root / "AppData" / "Local"),
            }
        )
        return _AuthWorkspace(
            root=root,
            env=env,
            mappings=mappings,
            source_digests=source_digests,
        )
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise


def _current_auth_source_digests(
    mappings: tuple[_AuthStoreMapping, ...],
) -> dict[str, str]:
    """Snapshot all allowlisted live identity files for conflict detection."""

    result: dict[str, str] = {}
    for mapping in mappings:
        for pattern in mapping.filenames:
            for source in mapping.source.glob(pattern):
                digest = _bounded_file_sha256(source)
                if digest is None:
                    raise OSError(_AUTH_STORE_READ_ERROR)
                result[str(source)] = digest
    return result


def _finish_auth_workspace(workspace: _AuthWorkspace, *, commit: bool) -> None:
    """Restore only allowlisted Kiro identity files, then delete the temp home."""

    try:
        if not commit:
            return
        lock_path = workspace.root.parent / _AUTH_PUBLISH_LOCK_FILENAME
        lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.fstat(lock_fd).st_size == 0:
                os.write(lock_fd, b"\0")
                os.fsync(lock_fd)
            platform_compat.restrict_to_owner(str(lock_path))
            # The generation check and every publication form one
            # cross-gateway critical section. Without this lock, two gateway
            # processes can both validate the same starting generation and
            # then overwrite one another's successful device login.
            with platform_compat.file_lock(lock_fd, exclusive=True, required=True):
                if _current_auth_source_digests(workspace.mappings) != workspace.source_digests:
                    raise RuntimeError(
                        "Kiro identity changed during sign-in; retry to preserve "
                        "the newer credentials"
                    )
                for mapping in workspace.mappings:
                    staged_dir = workspace.root / mapping.staged_relative
                    for pattern in mapping.filenames:
                        for staged in staged_dir.glob(pattern):
                            if staged.name != "data.sqlite3" and staged.name.startswith(
                                "data.sqlite3"
                            ):
                                # Sidecars are consumed by _atomic_restore_sqlite;
                                # they are never published independently.
                                continue
                            if staged.name == "data.sqlite3":
                                _atomic_restore_sqlite(staged, mapping.source / staged.name)
                                continue
                            content = _read_bounded_regular_file(staged)
                            if content is None:
                                continue
                            if (
                                staged.name.startswith("kiro-auth-token")
                                and staged.suffix == ".json"
                            ):
                                try:
                                    if not isinstance(json.loads(content), dict):
                                        continue
                                except (UnicodeDecodeError, json.JSONDecodeError):
                                    continue
                            _atomic_write_secret_bytes(mapping.source / staged.name, content)
        finally:
            os.close(lock_fd)
    finally:
        shutil.rmtree(workspace.root, ignore_errors=True)


def extract_secure_login_url(text: str) -> str:
    """Return the first official HTTPS URL in Kiro's device-flow output."""

    for match in _HTTPS_URL_RE.findall(str(text or "")):
        candidate = match.rstrip("),.;")
        if _UNSAFE_LOGIN_URL_RE.search(candidate):
            continue
        parsed = urlparse(candidate)
        hostname = (parsed.hostname or "").lower()
        try:
            port = parsed.port
        except ValueError:
            continue
        if (
            parsed.scheme == "https"
            and port in (None, 443)
            and hostname in _TRUSTED_LOGIN_HOSTS
            and parsed.username is None
            and parsed.password is None
            and (
                hostname == "app.kiro.dev"
                or parsed.path == "/start"
                or parsed.path.startswith("/start/")
            )
        ):
            return candidate
    return ""


def _interactive_repair_required(
    platform_name: str,
    candidates: list[str],
    home: Path,
) -> bool:
    """Whether the official installer would block on an unreadable /dev/tty prompt."""

    if platform_name == "darwin":
        app = Path("/Applications/Kiro CLI.app")
        target = os.path.normcase("/Applications/Kiro CLI.app/Contents/MacOS/kiro-cli")
        try:
            app_exists = app.is_dir()
        except OSError:
            app_exists = False
        return app_exists or any(
            os.path.normcase(os.path.normpath(item)) == target for item in candidates
        )
    if platform_name.startswith("linux"):
        installed = home / ".local" / "bin" / "kiro-cli"
        target = os.path.normcase(str(installed))
        try:
            target_exists = installed.is_file()
        except OSError:
            target_exists = False
        return target_exists or any(
            os.path.normcase(os.path.normpath(item)) == target for item in candidates
        )
    return False


def _official_install_target(
    platform_name: str,
    home: Path,
    environ: MutableMapping[str, str],
) -> str:
    """Return the exact executable target produced by the official installer."""

    if platform_name == "win32":
        program_files = (
            environ.get("ProgramFiles") or environ.get("PROGRAMFILES") or r"C:\Program Files"
        )
        return str(Path(program_files) / "Kiro-Cli" / "kiro-cli.exe")
    if platform_name == "darwin":
        return "/Applications/Kiro CLI.app/Contents/MacOS/kiro-cli"
    return str(home / ".local" / "bin" / "kiro-cli")


def _existing_binary_digest(path: str) -> str | None:
    try:
        return _binary_sha256(path)
    except (OSError, ValueError):
        return None


def register_process_start_override_attestation(
    *,
    platform_name: str | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> tuple[str, str | None]:
    """Pin the first observed POSIX override bytes for this process."""

    active_platform = platform_name or sys.platform
    active_environ = environ if environ is not None else os.environ
    override = active_environ.get("KIROCREW_KIRO_BIN", "")
    if not override or active_platform == "win32":
        return "", None

    canonical = _canonical_candidate(override)
    key = os.path.normcase(canonical)
    digest = _OPERATOR_OVERRIDE_ATTESTATIONS.get(key)
    if digest is None:
        digest = _existing_binary_digest(canonical)
        _register_operator_override_attestation(canonical, digest)
    return canonical, _OPERATOR_OVERRIDE_ATTESTATIONS.get(key)


def _powershell_path(environ: MutableMapping[str, str]) -> str:
    system_root = environ.get("SystemRoot") or environ.get("WINDIR") or r"C:\Windows"
    return str(Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")


def official_installer_command(
    platform_name: str,
    environ: MutableMapping[str, str],
) -> tuple[str, list[str]] | None:
    """Return the fixed interpreter command used for validated installer bytes."""

    if platform_name == "win32":
        powershell = _powershell_path(environ)
        if not platform_compat.is_executable_file(powershell):
            return None
        return (
            powershell,
            [
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                "-",
            ],
        )
    if platform_name == "darwin" or platform_name.startswith("linux"):
        bash = next(
            (
                item
                for item in ("/bin/bash", "/usr/bin/bash")
                if platform_compat.is_executable_file(item)
            ),
            "",
        )
        if not bash:
            return None
        return bash, ["-s"]
    return None


def validate_installer_script(platform_name: str, content: bytes) -> bool:
    """Require the release-pinned digest and expected script type."""

    if not content or len(content) > _MAX_INSTALLER_BYTES:
        return False
    digest_key = "win32" if platform_name == "win32" else "posix"
    digest = hashlib.sha256(content).hexdigest()
    if not hmac.compare_digest(digest, _INSTALLER_SHA256[digest_key]):
        return False
    prefix = content[:512].decode("utf-8", "replace")
    if platform_name == "win32":
        return (
            prefix.startswith("# Kiro CLI Installation Script for Windows")
            and '$ErrorActionPreference = "Stop"' in prefix
        )
    return prefix.startswith("#!/bin/bash") and "Kiro CLI Installation Script" in prefix


def _child_env(environ: MutableMapping[str, str], search_path: str) -> dict[str, str]:
    result = {key: value for key, value in environ.items() if key in _SAFE_ENV_KEYS}
    result["PATH"] = search_path
    result["NO_COLOR"] = "1"
    result["TERM"] = "dumb"
    return result


def _probe_env(environ: MutableMapping[str, str], search_path: str) -> dict[str, str]:
    """Build a non-interactive probe environment without proxy or desktop IPC."""

    result = {key: value for key, value in environ.items() if key in _PROBE_ENV_KEYS}
    result["PATH"] = search_path
    result["NO_COLOR"] = "1"
    result["TERM"] = "dumb"
    return result


def _trusted_installer_path(
    platform_name: str,
    environ: MutableMapping[str, str],
) -> str:
    """Return a system-only PATH for the unsandboxed official installer."""

    if platform_name != "win32":
        return _POSIX_INSTALLER_PATH
    system_root = environ.get("SystemRoot") or environ.get("WINDIR") or r"C:\Windows"
    return ";".join(
        [
            ntpath.join(system_root, "System32"),
            system_root,
            ntpath.join(system_root, "System32", "Wbem"),
            ntpath.join(system_root, "System32", "WindowsPowerShell", "v1.0"),
        ]
    )


def _proxy_bypassed(hostname: str, no_proxy: str) -> bool:
    """Return whether a simple NO_PROXY list excludes *hostname*."""

    host = hostname.lower().rstrip(".")
    for raw in no_proxy.split(","):
        item = raw.strip().lower()
        if not item:
            continue
        if item == "*":
            return True
        if item.startswith("[") and "]" in item:
            item = item[1 : item.index("]")]
        elif item.count(":") == 1:
            item = item.split(":", 1)[0]
        item = item.lstrip(".").rstrip(".")
        if item and (host == item or host.endswith(f".{item}")):
            return True
    return False


def _installer_proxy(
    url: str,
    environ: MutableMapping[str, str],
) -> str | None:
    """Return an explicitly configured HTTP(S) proxy without reading .netrc."""

    parsed_target = urlparse(url)
    hostname = parsed_target.hostname or ""
    no_proxy = environ.get("NO_PROXY") or environ.get("no_proxy") or ""
    if hostname and _proxy_bypassed(hostname, no_proxy):
        return None
    raw = (
        environ.get("HTTPS_PROXY")
        or environ.get("https_proxy")
        or environ.get("HTTP_PROXY")
        or environ.get("http_proxy")
        or ""
    ).strip()
    if not raw:
        return None
    parsed_proxy = urlparse(raw)
    if parsed_proxy.scheme not in {"http", "https"} or not parsed_proxy.hostname:
        return None
    return raw


def _trusted_installer_url(url: str) -> bool:
    parsed = urlparse(str(url))
    try:
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").lower().rstrip(".") in _TRUSTED_INSTALLER_HOSTS
        and port in (None, 443)
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"/install", "/install.ps1"}
        and not parsed.query
        and not parsed.fragment
    )


async def _terminate_process(
    proc: asyncio.subprocess.Process,
    windows_descendants: dict[int, int] | None = None,
) -> None:
    group_signalled = False
    if platform_compat.IS_POSIX:
        if proc.returncode is None:
            try:
                # Resolve the group through the still-live leader instead of
                # signalling a retained numeric PGID that the OS could reuse.
                await platform_compat.kill_process_tree_async(
                    proc.pid,
                    platform_compat.SIGTERM,
                )
                group_signalled = True
            except (ProcessLookupError, OSError, ValueError):
                if proc.returncode is None:
                    try:
                        proc.terminate()
                    except ProcessLookupError:
                        pass
    else:
        if proc.returncode is None:
            try:
                # asyncio retains the real Windows process handle, so this
                # targets the original process even if its PID is later reused.
                proc.terminate()
            except ProcessLookupError:
                pass

    leader_exited = proc.returncode is not None
    if not leader_exited:
        try:
            await asyncio.wait_for(proc.wait(), timeout=_TERMINATION_GRACE_SECS)
            leader_exited = True
        except asyncio.TimeoutError:
            pass

    if platform_compat.IS_POSIX:
        if group_signalled and not leader_exited:
            # The live group leader anchors the PGID identity. Once it exits,
            # never signal that retained integer because POSIX may reuse it.
            with contextlib.suppress(ProcessLookupError, OSError, ValueError):
                await platform_compat.kill_process_tree_async(
                    proc.pid,
                    platform_compat.SIGKILL,
                )
        elif not leader_exited:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
    else:
        # Retained Windows process handles refer to the original kernel process
        # objects, unlike PIDs, which may be recycled during a long operation.
        for handle in tuple((windows_descendants or {}).values()):
            with contextlib.suppress(ProcessLookupError, OSError, ValueError):
                platform_compat.terminate_process_handle(handle)
        if not leader_exited:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    if not leader_exited:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=_TERMINATION_GRACE_SECS)


async def _track_windows_descendants(
    proc: asyncio.subprocess.Process,
    tracked: dict[int, int],
    primary_root_handle: int | None = None,
    initial_snapshot: asyncio.Future[None] | None = None,
    primary_terminal_snapshot: asyncio.Future[None] | None = None,
) -> None:
    """Retain the complete Windows tree until every observed anchor exits."""

    primary_snapshot_pending = primary_root_handle is not None
    primary_terminally_scanned = primary_root_handle is None and proc.returncode is not None
    terminally_scanned: set[int] = set()
    try:
        while True:
            descendant_roots: list[tuple[int, int, bool]] = []
            for pid, handle in tuple(tracked.items()):
                active_before = platform_compat.process_handle_active(handle)
                if active_before or pid not in terminally_scanned:
                    descendant_roots.append((pid, handle, active_before))
            primary_active = (
                platform_compat.process_handle_active(primary_root_handle)
                if primary_root_handle is not None
                else proc.returncode is None
            )
            primary_needs_scan = (
                not primary_terminally_scanned
                if primary_root_handle is not None
                else primary_active
            )
            roots = (
                [(proc.pid, primary_root_handle, True, primary_active)]
                if primary_needs_scan
                else []
            ) + [
                (root_pid, retained_handle, False, active_before)
                for root_pid, retained_handle, active_before in descendant_roots
            ]
            if not roots:
                return
            for (
                root_pid,
                retained_root_handle,
                is_primary_root,
                root_active_before,
            ) in roots:
                initial_primary_snapshot = is_primary_root and primary_snapshot_pending
                try:
                    discovered = await platform_compat.descendant_termination_handles_async(
                        root_pid,
                        tracked,
                        retained_root_handle,
                    )
                    # The platform snapshot validates each numeric Toolhelp
                    # parent edge against exact-handle creation/exit times. A
                    # root that exits during discovery can therefore contribute
                    # genuine children without admitting a recycled PID's tree.
                    tracked.update(discovered)
                except (OSError, ValueError) as exc:
                    if initial_primary_snapshot and initial_snapshot is not None:
                        if not initial_snapshot.done():
                            initial_snapshot.set_exception(exc)
                        return
                    if (
                        is_primary_root
                        and not root_active_before
                        and primary_terminal_snapshot is not None
                    ):
                        if not primary_terminal_snapshot.done():
                            primary_terminal_snapshot.set_exception(exc)
                        return
                    raise
                root_active = (
                    platform_compat.process_handle_active(retained_root_handle)
                    if retained_root_handle is not None
                    else proc.returncode is None
                )
                if is_primary_root:
                    primary_terminally_scanned = not root_active_before and not root_active
                    if (
                        primary_terminally_scanned
                        and primary_terminal_snapshot is not None
                        and not primary_terminal_snapshot.done()
                    ):
                        primary_terminal_snapshot.set_result(None)
                elif root_active or root_active_before:
                    terminally_scanned.discard(root_pid)
                else:
                    terminally_scanned.add(root_pid)
                if initial_primary_snapshot:
                    primary_snapshot_pending = False
                    if initial_snapshot is not None and not initial_snapshot.done():
                        initial_snapshot.set_result(None)
            await asyncio.sleep(_WINDOWS_DESCENDANT_POLL_SECS)
    finally:
        if initial_snapshot is not None and not initial_snapshot.done():
            initial_snapshot.cancel()
        if primary_terminal_snapshot is not None and not primary_terminal_snapshot.done():
            primary_terminal_snapshot.cancel()


async def _unlink_off_loop(path: str | None) -> None:
    """Remove a sandbox launcher/profile without blocking the event loop."""

    if not path:
        return

    def _unlink() -> None:
        with contextlib.suppress(OSError):
            os.unlink(path)

    await asyncio.to_thread(_unlink)


async def _prepare_sandboxed_spawn(
    argv: list[str],
    *,
    mode: str,
    env: dict[str, str],
    extra_hidden_dirs: tuple[str, ...],
    extra_visible_dirs: tuple[str, ...],
) -> tuple[list[str], dict[str, str], str | None]:
    """Prepare filesystem-heavy sandbox state on a worker thread.

    Cancellation waits for preparation to settle so a launcher/profile created
    by the worker is still removed instead of becoming an untracked temp file.
    """

    task = asyncio.create_task(
        asyncio.to_thread(
            sandboxed_spawn_argv,
            argv,
            mode=mode,
            env=env,
            strip_python_env=True,
            extra_hidden_dirs=extra_hidden_dirs,
            extra_visible_dirs=extra_visible_dirs,
        )
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cleanup_path: str | None = None
        with contextlib.suppress(Exception):
            _, _, cleanup_path = await task
        await _unlink_off_loop(cleanup_path)
        raise


async def _run_process(
    command: str,
    args: list[str],
    *,
    env: dict[str, str],
    timeout_secs: float,
    on_output: Callable[[str], None] | None = None,
    sandboxed: bool = True,
    sandbox_mode: str = _UNVERIFIED_SANDBOX_MODE,
    extra_hidden_dirs: tuple[str, ...] = (),
    extra_visible_dirs: tuple[str, ...] = (),
    stdin_data: bytes | None = None,
) -> ProcessResult:
    """Run one fixed argv with bounded output and optional OS isolation."""

    if platform_compat.IS_POSIX and not _PROCESS_GROUP_SUPERVISOR_CODE:
        return ProcessResult(ok=False, error=_PROCESS_GROUP_SUPERVISOR_ERROR)

    output = ""
    cleanup_path: str | None = None
    creationflags = platform_compat.CREATE_NEW_PROCESS_GROUP
    if platform_compat.IS_WINDOWS:
        creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        spawn_argv = [command, *args]
        spawn_env = env
        if sandboxed and not platform_compat.IS_WINDOWS:
            # An absolute system `env` entrypoint prevents the generic sandbox
            # layer from mistaking an unverified candidate named `kiro-cli` for
            # the trusted provider spawn that may delegate to Kiro's internal
            # macOS sandbox. The candidate still executes inside the requested
            # outer sandbox with its original absolute path and argv.
            spawn_argv = ["/usr/bin/env", *spawn_argv]
            spawn_argv, spawn_env, cleanup_path = await _prepare_sandboxed_spawn(
                spawn_argv,
                mode=sandbox_mode,
                env=env,
                extra_hidden_dirs=extra_hidden_dirs,
                extra_visible_dirs=extra_visible_dirs,
            )
            if spawn_argv and not os.path.isabs(spawn_argv[0]):
                resolved_wrapper = shutil.which(spawn_argv[0], path=os.defpath)
                if not resolved_wrapper:
                    raise OSError(f"sandbox wrapper is unavailable: {spawn_argv[0]}")
                spawn_argv[0] = resolved_wrapper
        if platform_compat.IS_POSIX:
            # The immutable, gateway-captured supervisor is the outermost
            # process. Putting it inside the Linux namespace launcher makes the
            # two parent wait loops depend on each other; loading it from a
            # mutable package path would also let a same-UID agent replace code
            # immediately before an owner-triggered install.
            spawn_argv = [
                sys.executable,
                "-I",
                "-c",
                _PROCESS_GROUP_SUPERVISOR_CODE,
                *spawn_argv,
            ]
        proc = await asyncio.create_subprocess_exec(
            *spawn_argv,
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=spawn_env,
            preexec_fn=resource_limit_preexec(),
            start_new_session=platform_compat.IS_POSIX,
            creationflags=creationflags,
        )
    except (OSError, RuntimeError) as exc:
        await _unlink_off_loop(cleanup_path)
        return ProcessResult(ok=False, error=str(exc))

    windows_descendants: dict[int, int] = {}
    windows_root_handle: int | None = None
    descendant_task: asyncio.Task[None] | None = None
    initial_snapshot: asyncio.Future[None] | None = None
    primary_terminal_snapshot: asyncio.Future[None] | None = None
    if platform_compat.IS_WINDOWS:
        # Anchor the primary kernel object before yielding after spawn. Without
        # this handle, a launcher that exits immediately could leave helpers
        # behind while its numeric PID is recycled before the first snapshot.
        windows_root_handle = platform_compat.duplicate_asyncio_process_handle(proc)
        if windows_root_handle is None:
            await _terminate_process(proc)
            await _unlink_off_loop(cleanup_path)
            return ProcessResult(
                ok=False,
                returncode=proc.returncode,
                error="could not retain the Windows process tree",
            )
        initial_snapshot = asyncio.get_running_loop().create_future()
        primary_terminal_snapshot = asyncio.get_running_loop().create_future()
        descendant_task = asyncio.create_task(
            _track_windows_descendants(
                proc,
                windows_descendants,
                windows_root_handle,
                initial_snapshot,
                primary_terminal_snapshot,
            )
        )

    async def _capture(stream: asyncio.StreamReader | None) -> None:
        nonlocal output
        if stream is None:
            return
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            text = chunk.decode("utf-8", "replace")
            output = _append_capped(output, text)
            if on_output is not None:
                on_output(text)

    stdout_task = asyncio.create_task(_capture(proc.stdout))
    stderr_task = asyncio.create_task(_capture(proc.stderr))

    async def _feed_stdin() -> None:
        if stdin_data is None or proc.stdin is None:
            return
        proc.stdin.write(stdin_data)
        await proc.stdin.drain()
        proc.stdin.close()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            await proc.stdin.wait_closed()

    stdin_task = asyncio.create_task(_feed_stdin())
    operation_tasks = (stdout_task, stderr_task, stdin_task)
    wait_task = asyncio.create_task(proc.wait())
    cleanup_tasks = (*operation_tasks, wait_task)

    async def _wait_for_completion() -> None:
        completion_tasks: list[Awaitable[Any]] = list(cleanup_tasks)
        if descendant_task is not None:
            # A Windows bootstrap executable may exit after launching the real
            # installer as a child.  Keep the retained-tree tracker in the
            # success condition so a zero exit cannot release live descendant
            # handles and report setup complete while installation is ongoing.
            # Shield the tracker from the operation timeout: the timeout path
            # still needs its latest retained handles while terminating the tree.
            completion_tasks.append(asyncio.shield(descendant_task))
        if initial_snapshot is not None:
            # A fast launcher and its readers cannot finish the operation until
            # the exact-object primary snapshot has settled.
            await initial_snapshot
        if primary_terminal_snapshot is not None:
            await asyncio.gather(primary_terminal_snapshot, *completion_tasks)
        else:
            await asyncio.gather(*completion_tasks)

    try:
        await asyncio.wait_for(
            _wait_for_completion(),
            timeout=timeout_secs,
        )
    except asyncio.TimeoutError:
        await _terminate_process(proc, windows_descendants)
        for task in cleanup_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*cleanup_tasks, return_exceptions=True)
        return ProcessResult(
            ok=False,
            output=output,
            returncode=proc.returncode,
            timed_out=True,
            error="process timed out",
        )
    except asyncio.CancelledError:
        await _terminate_process(proc, windows_descendants)
        for task in cleanup_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*cleanup_tasks, return_exceptions=True)
        raise
    except Exception as exc:
        await _terminate_process(proc, windows_descendants)
        for task in cleanup_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*cleanup_tasks, return_exceptions=True)
        return ProcessResult(
            ok=False,
            output=output,
            returncode=proc.returncode,
            error=str(exc),
        )
    finally:
        if descendant_task is not None:
            descendant_task.cancel()
            await asyncio.gather(descendant_task, return_exceptions=True)
        for handle in windows_descendants.values():
            platform_compat.close_process_handle(handle)
        if windows_root_handle is not None:
            platform_compat.close_process_handle(windows_root_handle)
        await _unlink_off_loop(cleanup_path)

    return ProcessResult(
        ok=proc.returncode == 0,
        output=output,
        returncode=proc.returncode,
        error="" if proc.returncode == 0 else f"process exited with code {proc.returncode}",
    )


async def _download_installer(
    url: str,
    environ: MutableMapping[str, str],
) -> bytes:
    """Download official installer bytes without a user-writable staging path."""

    if not _trusted_installer_url(url):
        raise RuntimeError("installer URL left the trusted Kiro host")

    timeout = aiohttp.ClientTimeout(total=_DOWNLOAD_TIMEOUT_SECS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        current_url = url
        for redirects_followed in range(_MAX_INSTALLER_REDIRECTS + 1):
            async with session.get(
                current_url,
                allow_redirects=False,
                proxy=_installer_proxy(current_url, environ),
            ) as response:
                if response.status in _INSTALLER_REDIRECT_STATUSES:
                    if redirects_followed >= _MAX_INSTALLER_REDIRECTS:
                        raise RuntimeError("installer download exceeded the redirect limit")
                    location = response.headers.get("Location", "")
                    redirect_url = urljoin(str(response.url), location)
                    if not location or not _trusted_installer_url(redirect_url):
                        raise RuntimeError("installer redirect left the trusted Kiro host")
                    current_url = redirect_url
                    continue
                if not _trusted_installer_url(str(response.url)):
                    raise RuntimeError("installer redirect left the trusted Kiro host")
                if response.status != 200:
                    raise RuntimeError(f"installer download returned HTTP {response.status}")
                length = response.content_length
                if length is not None and (length <= 0 or length > _MAX_INSTALLER_BYTES):
                    raise RuntimeError("installer response has an invalid size")
                content = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    content.extend(chunk)
                    if len(content) > _MAX_INSTALLER_BYTES:
                        raise RuntimeError("installer response exceeded the size limit")
                return bytes(content)
    raise RuntimeError("installer download exceeded the redirect limit")


async def _write_audit(
    *,
    action: str,
    outcome: str,
    caller: str,
    error: str = "",
    critical: bool = False,
) -> None:
    from kiro_crew.sel import sel

    def _write() -> None:
        sel().log_tool_invocation(
            session_key="dashboard:kiro-prerequisite",
            source="dashboard",
            tool_name=f"kiro_prerequisite_{action}",
            tool_kind="system_setup",
            outcome=outcome,
            error=error,
            metadata={"caller": caller[:100]},
            critical=critical,
        )

    await asyncio.to_thread(_write)


def _probe_filesystem_state(
    platform_name: str,
    home: Path,
    environ: MutableMapping[str, str],
) -> tuple[dict[str, str], dict[str, str], list[str], tuple[str, list[str]] | None, bool]:
    """Collect path/candidate state on a worker thread."""

    separator = ";" if platform_name == "win32" else os.pathsep
    # Setup discovery matches ACP resolution on every OS: a runnable Kiro CLI is
    # recognized wherever it lives (PATH, Scripts, override, package-manager
    # dir), since trust is "the CLI runs". Windows is no longer restricted to
    # the Program Files tree — a winget/scoop/user install that ACP would launch
    # must also be recognized by setup, or the two disagree and the user is sent
    # to a redundant reinstall.
    search_path = separator.join(
        known_kiro_cli_dirs(
            platform_name,
            home,
            environ,
            include_inherited_path=True,
        )
    )
    child_environment = _child_env(environ, search_path)
    probe_environment = _probe_env(environ, search_path)
    candidates = find_kiro_cli_candidates(
        platform_name,
        home,
        environ,
        include_inherited_path=True,
    )
    installer_plan = official_installer_command(platform_name, environ)
    repair_hint = _interactive_repair_required(platform_name, candidates, home)
    return child_environment, probe_environment, candidates, installer_plan, repair_hint


def _established_installation(data_home: Path) -> bool:
    """Recognize an existing Kiro Crew home when migrating onto the setup marker."""

    marker = data_home / _SETUP_COMPLETE_FILENAME
    if marker.is_file():
        return True
    for path in (data_home / "sessions", data_home / "history"):
        try:
            if path.is_file() and path.stat().st_size > 0:
                return True
            if path.is_dir():
                for child in path.rglob("*"):
                    if child.is_file() and child.stat().st_size > 0:
                        return True
        except OSError:
            continue
    return False


class KiroPrerequisiteService:
    """Single-gateway coordinator for prerequisite probes and setup operations."""

    def __init__(
        self,
        *,
        platform_name: str | None = None,
        environ: MutableMapping[str, str] | None = None,
        home: Path | None = None,
        data_home: Path | None = None,
        process_runner: ProcessRunner | None = None,
        downloader: InstallerDownloader | None = None,
        audit_writer: AuditWriter | None = None,
        clock: Callable[[], float] | None = None,
        assume_ready: bool = False,
    ) -> None:
        self._platform = platform_name or sys.platform
        self._environ = environ if environ is not None else os.environ
        self._home = home or Path.home()
        if data_home is not None:
            self._data_home = data_home
        elif home is not None:
            configured_home = self._environ.get("KIROCREW_HOME", "")
            self._data_home = (
                Path(configured_home).expanduser()
                if configured_home
                else self._home / ".kiro" / "crew"
            )
        else:
            self._data_home = config_dir()
        self._auth_staging_parent = _ensure_auth_staging_parent(self._home)
        self._setup_marker = self._data_home / _SETUP_COMPLETE_FILENAME
        self._binary_trust_path = self._data_home / _BINARY_TRUST_FILENAME
        (
            self._initial_override_path,
            self._initial_override_sha256,
        ) = register_process_start_override_attestation(
            platform_name=self._platform,
            environ=self._environ,
        )
        self._initial_setup_complete = _established_installation(self._data_home)
        auth_store_dirs = [
            mapping.source
            for mapping in _auth_store_mappings(self._platform, self._home, self._environ)
        ]
        # Kiro Crew's own secret home is always hidden from a probed CLI. The
        # credential-minimal probe additionally hides the identity stores; the
        # real-home fallback probe must leave those visible so a CLI whose valid
        # session lives outside the staged files (an external auth helper
        # resolved from the real home) can read its own credentials.
        self._crew_hidden_dirs = tuple(
            dict.fromkeys(
                str(path)
                for path in (
                    self._data_home,
                    self._home / ".kiro" / "crew",
                    self._home / ".kirocrew",
                )
            )
        )
        self._hidden_probe_dirs = tuple(
            dict.fromkeys((*self._crew_hidden_dirs, *(str(path) for path in auth_store_dirs)))
        )
        self._child_environment: dict[str, str] = {}
        self._probe_environment: dict[str, str] = {}
        self._installer_environment = _child_env(
            self._environ,
            _trusted_installer_path(self._platform, self._environ),
        )
        self._run = process_runner or _run_process
        self._download: InstallerDownloader
        if downloader is None:

            async def download(url: str) -> bytes:
                return await _download_installer(url, self._environ)

            self._download = download
        else:
            self._download = downloader
        self._audit = audit_writer or _write_audit
        self._clock = clock or time.monotonic
        self._assume_ready = assume_ready
        self._status = PrerequisiteStatus(
            platform=_platform_label(self._platform),
            initial_setup_complete=self._initial_setup_complete,
        )
        self._operation = OperationStatus()
        self._task: asyncio.Task[None] | None = None
        self._session_probe_task: asyncio.Task[PrerequisiteStatus] | None = None
        self._probe_lock = asyncio.Lock()
        self._last_probe_at = 0.0
        self._has_probed = False
        self._viable_binary = ""
        self._installer_plan: tuple[str, list[str]] | None = None

    @property
    def operation_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _snapshot_dict(self) -> dict[str, Any]:
        result = asdict(self._status)
        result["operation"] = asdict(self._operation)
        return result

    async def snapshot(self, *, force: bool = False) -> dict[str, Any]:
        if not self.operation_running:
            await self._probe(force=force)
        return self._snapshot_dict()

    async def session_ready(self) -> bool:
        """Return readiness without putting CLI probes on the chat hot path."""

        if self.operation_running:
            return bool(self._status.ready)
        if not self._has_probed:
            await self._probe()
        elif self._clock() - self._last_probe_at >= _SESSION_GUARD_REPROBE_SECS and (
            self._session_probe_task is None or self._session_probe_task.done()
        ):
            # Chat/session guards consume the last known state immediately and
            # refresh it in the background. The dashboard status endpoint still
            # awaits `_probe()` directly, so setup progress and sign-out repair
            # remain prompt without making an ordinary send wait up to two
            # subprocess timeouts.
            self._session_probe_task = asyncio.create_task(self._probe())
            self._session_probe_task.add_done_callback(self._session_probe_done)
        return bool(self._status.ready)

    def _session_probe_done(self, task: asyncio.Task[PrerequisiteStatus]) -> None:
        if task is not self._session_probe_task:
            return
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            logger.warning("Background Kiro readiness probe failed", exc_info=True)

    async def _probe(self, *, force: bool = False) -> PrerequisiteStatus:
        async with self._probe_lock:
            if self._assume_ready:
                self._status = PrerequisiteStatus(
                    platform=_platform_label(self._platform),
                    installed=True,
                    authenticated=True,
                    ready=True,
                    can_login=True,
                    initial_setup_complete=True,
                )
                self._last_probe_at = self._clock()
                self._has_probed = True
                return self._status
            now = self._clock()
            if self._has_probed and not force and now - self._last_probe_at < _PROBE_CACHE_SECS:
                return self._status
            self._viable_binary = ""
            (
                self._child_environment,
                self._probe_environment,
                candidates,
                self._installer_plan,
                repair_hint,
            ) = await asyncio.to_thread(
                _probe_filesystem_state,
                self._platform,
                self._home,
                self._environ,
            )
            # ACP resolves the first executable candidate. Probe that exact
            # candidate instead of skipping a broken entry and approving a
            # different binary than the session launcher will use.
            for executable in candidates[:1]:
                result = await self._audited_probe(
                    "probe_version",
                    executable,
                    ["--version"],
                )
                if result.ok:
                    # Keep the discovered path AS RESOLVED (not realpath'd): a
                    # multiplexer launcher like ``~/.toolbox/bin/kiro-cli``
                    # dispatches on its argv[0] basename, so resolving the
                    # symlink to ``toolbox-exec`` would make whoami/login fail
                    # with "Command doesn't appear to be associated with any
                    # tool". This is the exact path ``--version`` just succeeded
                    # with and the one ACP launches.
                    self._viable_binary = executable
                    break

            repair_required = not self._viable_binary and repair_hint
            if not self._viable_binary:
                self._status = PrerequisiteStatus(
                    platform=_platform_label(self._platform),
                    can_auto_install=self._installer_plan is not None and not repair_required,
                    repair_required=repair_required,
                    initial_setup_complete=self._initial_setup_complete,
                )
                self._last_probe_at = self._clock()
                self._has_probed = True
                return self._status

            # A viable binary answered ``--version``, so it can be signed into
            # (trust is "it runs"); ``whoami`` decides whether it is already
            # authenticated. No provenance gate: source/owner/path do not block
            # sign-in, so a runnable CLI never needs an unreachable "repair".
            # ``whoami`` decides whether the CLI is already signed in. Run it the
            # same way a real ACP session runs the CLI (see acp/runtime.py):
            # against the real environment/home, NOT a credential-minimal
            # rewritten HOME. A rewritten HOME breaks any CLI whose session or
            # tool registry lives in the real home — e.g. a multiplexer launcher
            # cannot even resolve itself without its real-home registry — so the
            # isolated probe reported such CLIs signed-out even though a real
            # session authenticates fine.
            whoami = await self._audited_identity_probe(
                self._viable_binary, isolate_home=False
            )
            if whoami.ok:
                await asyncio.to_thread(self._mark_setup_complete)
            self._status = PrerequisiteStatus(
                platform=_platform_label(self._platform),
                installed=True,
                authenticated=whoami.ok,
                ready=whoami.ok,
                can_auto_install=self._installer_plan is not None,
                can_login=True,
                repair_required=False,
                initial_setup_complete=self._initial_setup_complete,
            )
            self._last_probe_at = self._clock()
            self._has_probed = True
            return self._status

    async def _audited_probe(
        self,
        action: str,
        executable: str,
        args: list[str],
    ) -> ProcessResult:
        """Run one credential-free status probe with paired SEL lifecycle events."""

        await self._audit(
            action=action,
            outcome="invoked",
            caller="gateway-status",
            critical=True,
        )
        try:
            result = await self._run(
                executable,
                args,
                env=self._probe_environment,
                timeout_secs=_PROBE_TIMEOUT_SECS,
                sandboxed=True,
                sandbox_mode=_UNVERIFIED_SANDBOX_MODE,
                extra_hidden_dirs=self._hidden_probe_dirs,
            )
        except asyncio.CancelledError:
            await self._set_terminal_audit(action, "failed", "gateway-status", "cancelled")
            raise
        except Exception:
            # A probe that cannot even run means the candidate is not viable,
            # not that the gateway is broken. Degrade to a not-ok result so the
            # status endpoint stays a retryable "not ready" instead of a 500.
            logger.warning("Kiro %s probe failed to run", action, exc_info=True)
            await self._set_terminal_audit(
                action,
                "failed",
                "gateway-status",
                "probe execution failed",
            )
            return ProcessResult(ok=False, error="Kiro CLI probe could not run")
        await self._set_terminal_audit(
            action,
            "completed" if result.ok else "failed",
            "gateway-status",
            "" if result.ok else ("timeout" if result.timed_out else "nonzero exit"),
        )
        return result

    def _attest_candidate(self, executable: str) -> None:
        """Pin the exact binary produced by the validated official installer."""

        canonical = _canonical_candidate(executable)
        payload = {
            "version": _BINARY_TRUST_VERSION,
            "path": canonical,
            "sha256": _binary_sha256(canonical),
        }
        atomic_write(
            self._binary_trust_path,
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            fsync=True,
            mode=0o600,
        )
        platform_compat.restrict_to_owner(str(self._binary_trust_path))

    def _real_home_probe_env(self) -> dict[str, str]:
        """Build the real-home readiness ``whoami`` env like an ACP session.

        The readiness login check runs against the real home, so it must see the
        same session environment a real ``kiro-cli acp`` session gets — the
        D-Bus session bus / secret-service keyring, XDG runtime dir, Kerberos
        ccache, proxy, SSH agent, locale, etc. A curated allowlist drops
        whatever a given host's keyring backend needs (e.g. AL2023 validates the
        login via the D-Bus secret service, which needs
        ``DBUS_SESSION_BUS_ADDRESS``), so mirror ``acp/runtime.py`` exactly: the
        full real environment minus gateway-owned channel credentials, with PATH
        augmented and the Kerberos ccache repaired. The OS sandbox still scrubs
        sensitive env and Kiro Crew's own home stays hidden.
        """

        env = {str(key): str(value) for key, value in self._environ.items()}
        env = scrub_agent_denied_env(env)
        env["PATH"] = augmented_path(env.get("PATH", ""))
        resolve_krb5_ccname(env)
        env["NO_COLOR"] = "1"
        env["TERM"] = "dumb"
        return env

    async def _run_auth_command(
        self,
        executable: str,
        args: list[str],
        *,
        base_env: dict[str, str],
        timeout_secs: float,
        on_output: Callable[[str], None] | None = None,
        commit: bool,
        isolate_home: bool = True,
    ) -> ProcessResult:
        """Run Kiro auth with only Kiro identity files in its HOME.

        The CLI is trusted because it runs (its install source, owner, and path
        do not gate sign-in); KiroCrew is not the authority on where Kiro CLI is
        installed. The POSIX copy still snapshots the exact resolved bytes into
        the sandbox so the process that receives the staged credentials is the
        one just resolved, but it pins no stored digest — a Kiro self-update
        that legitimately rewrites the binary must not break sign-in.

        ``isolate_home=False`` runs the read-only readiness login check against
        the user's real home (like an ACP session) instead of the
        credential-minimal one, so a CLI whose session/registry lives in the
        real home is detected. ``commit`` (device login) always isolates.
        """

        if not isolate_home:
            # Read-only readiness login check, run the way a real ACP session
            # runs the CLI (acp/runtime.py): against the real environment/home
            # under the standard OS sandbox. A rewritten HOME breaks CLIs whose
            # session or tool registry lives in the real home (e.g. a toolbox
            # multiplexer that resolves itself via its real-home registry), so
            # this is what actually detects them.
            #
            # SECURITY: this matches the accepted ACP launch posture, not a new
            # surface — ACP already runs the resolved kiro-cli with the full real
            # environment on every session (the standard sandbox intentionally
            # exposes AWS/SSH to it). This path is a read-only subset: `commit`
            # is rejected so it never stages or publishes, only Kiro Crew's own
            # secret home is hidden, and the bytes are copied into a
            # sandbox-visible private snapshot (keeping the resolved basename so a
            # multiplexer still dispatches) and THAT is executed — binding
            # resolve-to-exec exactly like the isolated probe and the ACP snapshot.
            if commit:
                raise ValueError("real-home auth commands cannot commit credentials")
            fallback_executable = executable
            cleanup_dir: str | None = None
            extra_visible: tuple[str, ...] = ()
            try:
                if platform_compat.IS_POSIX and self._run is _run_process:
                    snapshot_root = Path(
                        tempfile.mkdtemp(prefix="probe-", dir=str(self._auth_staging_parent))
                    )
                    cleanup_dir = str(snapshot_root)
                    # Pass the UNRESOLVED path so the copy keeps the caller's
                    # basename (e.g. the ``kiro-cli`` symlink), which a
                    # multiplexer launcher dispatches on.
                    fallback_executable = await asyncio.to_thread(
                        _copy_verified_auth_executable,
                        executable,
                        snapshot_root,
                        None,
                        prefix="kiro-cli-probe-",
                    )
                    extra_visible = (str(snapshot_root),)
                return await self._run(
                    fallback_executable,
                    args,
                    env=self._real_home_probe_env(),
                    timeout_secs=timeout_secs,
                    on_output=on_output,
                    sandboxed=True,
                    sandbox_mode=_KIRO_AUTH_SANDBOX_MODE,
                    extra_hidden_dirs=self._crew_hidden_dirs,
                    extra_visible_dirs=extra_visible,
                )
            finally:
                if cleanup_dir:
                    await asyncio.to_thread(shutil.rmtree, cleanup_dir, ignore_errors=True)

        workspace = await asyncio.to_thread(
            _prepare_auth_workspace,
            self._platform,
            self._home,
            self._environ,
            base_env,
        )
        auth_executable = executable
        commit_changes = False
        try:
            if platform_compat.IS_POSIX and self._run is _run_process:
                auth_executable = await asyncio.to_thread(
                    _copy_verified_auth_executable,
                    executable,
                    workspace.root / ".bin",
                    None,
                )
            result = await self._run(
                auth_executable,
                args,
                env=workspace.env,
                timeout_secs=timeout_secs,
                on_output=on_output,
                sandboxed=True,
                sandbox_mode=_KIRO_AUTH_SANDBOX_MODE,
                extra_hidden_dirs=self._hidden_probe_dirs,
                extra_visible_dirs=(str(workspace.root),),
            )
            commit_changes = commit and result.ok
            return result
        finally:
            await asyncio.to_thread(
                _finish_auth_workspace,
                workspace,
                commit=commit_changes,
            )

    async def _audited_identity_probe(
        self, executable: str, *, isolate_home: bool = True
    ) -> ProcessResult:
        """Run an identity probe with paired SEL events.

        The readiness check calls this with ``isolate_home=False`` so ``whoami``
        runs against the real home (like an ACP session) and detects CLIs whose
        session or tool registry lives there. ``isolate_home=True`` keeps the
        credential-minimal temporary home for callers that need it.
        """

        action = "probe_identity"
        await self._audit(
            action=action,
            outcome="invoked",
            caller="gateway-status",
            critical=True,
        )
        try:
            result = await self._run_auth_command(
                executable,
                ["whoami"],
                base_env=self._probe_environment,
                timeout_secs=_PROBE_TIMEOUT_SECS,
                commit=False,
                isolate_home=isolate_home,
            )
        except asyncio.CancelledError:
            await self._set_terminal_audit(action, "failed", "gateway-status", "cancelled")
            raise
        except Exception:
            # A whoami that cannot even run means "not signed in", not a broken
            # gateway. Degrade to a not-ok result so the status endpoint reports
            # authenticated=False (retryable) instead of surfacing a 500 that
            # flashes the full-screen "could not check Kiro CLI" error.
            logger.warning("Kiro identity probe failed to run", exc_info=True)
            await self._set_terminal_audit(
                action,
                "failed",
                "gateway-status",
                "probe execution failed",
            )
            return ProcessResult(ok=False, error="Kiro identity probe could not run")
        await self._set_terminal_audit(
            action,
            "completed" if result.ok else "failed",
            "gateway-status",
            "" if result.ok else ("timeout" if result.timed_out else "nonzero exit"),
        )
        return result

    def _mark_setup_complete(self) -> None:
        if self._initial_setup_complete:
            return
        atomic_write(
            self._setup_marker,
            "complete\n",
            fsync=True,
            mode=0o600,
        )
        platform_compat.restrict_to_owner(str(self._setup_marker))
        self._initial_setup_complete = True

    def _start(self, kind: str, caller: str) -> dict[str, Any]:
        if self.operation_running:
            raise PrerequisiteBusyError("Another Kiro setup step is already running.")
        self._operation = OperationStatus(
            kind=kind,
            status="running",
            message="Preparing Kiro CLI setup…",
        )
        target = self._install(caller) if kind == "install" else self._login(caller)
        self._task = asyncio.create_task(target)
        self._task.add_done_callback(self._operation_done)
        return self._snapshot_dict()

    def start_install(self, caller: str = "") -> dict[str, Any]:
        return self._start("install", caller)

    def start_login(self, caller: str = "") -> dict[str, Any]:
        return self._start("login", caller)

    def _operation_done(self, task: asyncio.Task[None]) -> None:
        if task is not self._task:
            return
        if task.cancelled():
            self._operation.status = "failed"
            self._operation.error = "Kiro setup was cancelled."
            return
        try:
            task.result()
        except Exception:
            logger.exception("Kiro prerequisite operation failed unexpectedly")
            self._operation.status = "failed"
            self._operation.error = "Kiro setup failed unexpectedly."

    async def _set_terminal_audit(
        self,
        action: str,
        outcome: str,
        caller: str,
        error: str = "",
    ) -> None:
        try:
            await self._audit(
                action=action,
                outcome=outcome,
                caller=caller,
                error=error,
                critical=False,
            )
        except Exception:
            logger.warning("Could not write terminal Kiro setup audit event", exc_info=True)

    async def _install(self, caller: str) -> None:
        try:
            await self._audit(
                action="install",
                outcome="invoked",
                caller=caller,
                critical=True,
            )
            current = await self._probe(force=True)
            if current.installed and current.can_login:
                self._operation = OperationStatus(
                    kind="install",
                    status="succeeded",
                    message="Kiro CLI is already installed.",
                )
                await self._set_terminal_audit("install", "completed", caller)
                return
            if not current.can_auto_install:
                error = (
                    "Replace the existing Kiro CLI using the official installation guide."
                    if current.repair_required
                    else f"Automatic Kiro CLI installation is unavailable on {current.platform}."
                )
                self._operation = OperationStatus(
                    kind="install",
                    status="failed",
                    message="Kiro CLI needs manual installation.",
                    error=error,
                )
                await self._set_terminal_audit("install", "denied", caller, error)
                return
            expected_target = _canonical_candidate(
                _official_install_target(self._platform, self._home, self._environ)
            )
            previous_target_digest = await asyncio.to_thread(
                _existing_binary_digest,
                expected_target,
            )

            install_url = (
                OFFICIAL_WINDOWS_INSTALL_URL if self._platform == "win32" else OFFICIAL_INSTALL_URL
            )
            self._operation.message = "Downloading the official Kiro CLI installer…"
            content = await self._download(install_url)
            if not validate_installer_script(self._platform, content):
                raise RuntimeError("the official installer response failed validation")

            plan = self._installer_plan
            if plan is None:
                raise RuntimeError("the platform installer command is unavailable")
            self._operation.message = "Installing Kiro CLI…"
            result = await self._run(
                plan[0],
                plan[1],
                env=self._installer_environment,
                timeout_secs=_INSTALL_TIMEOUT_SECS,
                on_output=self._capture_operation_output,
                sandboxed=False,
                stdin_data=content,
            )
            if not result.ok:
                reason = (
                    "Kiro CLI installation timed out."
                    if result.timed_out
                    else ("Kiro CLI installation did not complete.")
                )
                raise RuntimeError(reason)
            next_status = await self._probe(force=True)
            if not next_status.installed:
                raise RuntimeError("installation finished, but Kiro CLI was not found")
            if not self._viable_binary:
                raise RuntimeError("installation finished without a usable Kiro CLI")
            installed_target = _canonical_candidate(self._viable_binary)
            if os.path.normcase(installed_target) != os.path.normcase(expected_target):
                raise RuntimeError(
                    "the installed Kiro CLI is shadowed by another executable; "
                    "remove the shadowing executable and retry"
                )
            installed_target_digest = await asyncio.to_thread(
                _existing_binary_digest,
                expected_target,
            )
            if installed_target_digest is None:
                raise RuntimeError("the official Kiro CLI install target is not a regular file")
            if previous_target_digest is not None and hmac.compare_digest(
                previous_target_digest,
                installed_target_digest,
            ):
                raise RuntimeError(
                    "the installer did not replace the existing Kiro CLI; "
                    "remove it and retry from Kiro Crew"
                )
            await asyncio.to_thread(self._attest_candidate, expected_target)
            next_status = await self._probe(force=True)
            if not next_status.can_login:
                raise RuntimeError("installed Kiro CLI could not be verified for sign-in")
            self._operation = OperationStatus(
                kind="install",
                status="succeeded",
                message="Kiro CLI is installed.",
            )
            await self._set_terminal_audit("install", "completed", caller)
        except asyncio.CancelledError:
            await self._set_terminal_audit("install", "failed", caller, "cancelled")
            raise
        except Exception as exc:
            safe_error = _sanitize_detail(str(exc))
            self._operation.status = "failed"
            self._operation.message = "Kiro CLI installation failed."
            self._operation.error = safe_error
            await self._set_terminal_audit("install", "failed", caller, safe_error)

    def _capture_operation_output(self, chunk: str) -> None:
        detail = _append_capped(self._operation.detail, chunk)
        self._operation.detail = _sanitize_detail(detail)
        if self._operation.kind == "login" and (url := extract_secure_login_url(detail)):
            self._operation.url = url
            self._operation.message = "Open the sign-in page and enter the code shown below."

    async def _login(self, caller: str) -> None:
        try:
            await self._audit(
                action="login",
                outcome="invoked",
                caller=caller,
                critical=True,
            )
            current = await self._probe(force=True)
            if not current.installed or not self._viable_binary:
                error = (
                    "Kiro CLI could not start. Reinstall it before signing in."
                    if current.repair_required
                    else "Install Kiro CLI before signing in."
                )
                self._operation = OperationStatus(
                    kind="login",
                    status="failed",
                    message="Kiro sign-in cannot start.",
                    error=error,
                )
                await self._set_terminal_audit("login", "denied", caller, error)
                return
            if current.authenticated:
                self._operation = OperationStatus(
                    kind="login",
                    status="succeeded",
                    message="Kiro sign-in is already complete.",
                )
                await self._set_terminal_audit("login", "completed", caller)
                return

            self._operation.message = "Starting secure browser sign-in…"
            result = await self._run_auth_command(
                self._viable_binary,
                ["login", "--use-device-flow"],
                base_env=self._child_environment,
                timeout_secs=_LOGIN_TIMEOUT_SECS,
                on_output=self._capture_operation_output,
                commit=True,
            )
            next_status = await self._probe(force=True)
            if next_status.authenticated:
                self._operation = OperationStatus(
                    kind="login",
                    status="succeeded",
                    message="Kiro sign-in is complete.",
                )
                await self._set_terminal_audit("login", "completed", caller)
                return
            error = (
                "Kiro sign-in timed out. Start it again to receive a new code."
                if result.timed_out
                else "Kiro sign-in did not complete. Try again."
            )
            self._operation.status = "failed"
            self._operation.message = "Kiro sign-in is incomplete."
            self._operation.error = error
            await self._set_terminal_audit("login", "failed", caller, error)
        except asyncio.CancelledError:
            await self._set_terminal_audit("login", "failed", caller, "cancelled")
            raise
        except Exception as exc:
            safe_error = _sanitize_detail(str(exc))
            self._operation.status = "failed"
            self._operation.message = "Kiro sign-in failed."
            self._operation.error = safe_error
            await self._set_terminal_audit("login", "failed", caller, safe_error)

    async def close(self) -> None:
        tasks: list[asyncio.Task[Any]] = []
        for task in (self._task, self._session_probe_task):
            if task is not None and not task.done() and task not in tasks:
                task.cancel()
                tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
