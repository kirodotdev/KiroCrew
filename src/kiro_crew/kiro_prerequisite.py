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
import hashlib
import hmac
import json
import logging
import ntpath
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp

from kiro_crew import platform_compat
from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir
from kiro_crew.kiro_cli import (
    find_kiro_cli_candidates,
    known_kiro_cli_dirs,
)
from kiro_crew.sandbox import (
    SandboxUnavailableError,
    resource_limit_supervisor_argv,
    sandboxed_spawn_argv,
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
# Yield before the boot-time readiness probe so its kiro-cli spawn does not
# contend with the concurrent app-backend spawns on the boot-critical path.
_WARM_UP_DELAY_SECS = 3.0
_TERMINATION_GRACE_SECS = 2.0
_WINDOWS_DESCENDANT_POLL_SECS = 0.05
_HTTPS_URL_RE = re.compile(r"https://[^\s<>\"']+")
_UNSAFE_LOGIN_URL_RE = re.compile(r"[\\\x00-\x1f\x7f]")
# kiro-cli renders an animated TTY progress bar ("▰▰▱▱ Logging in… | Press ^C to
# cancel") whose repaints arrive as hundreds of carriage-return-separated frames.
# It is noise in a dashboard <pre>: stored verbatim, the sign-in card fills with a
# wall of block glyphs. Strip ANSI control sequences, turn each progress-glyph run
# into a line break (a frame boundary — some builds repaint without a CR), treat
# CR as a line break too, and collapse consecutive identical lines.
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_PROGRESS_GLYPH_RE = re.compile(r"[▰▱]+")
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
_BINARY_TRUST_VERSION = 1
# Marker the offline E2E harness sets on the gateway it spawns. It grants NO
# privilege: the packaged fake ACP backend is launched by the ordinary in-place
# path like any other runnable executable. It is kept purely as a "this gateway
# is a test rig" signal for the harness contract (test/test_harness.py).
FAKE_ACP_TEST_MODE_ENV = "KIROCREW_FAKE_ACP_TEST_MODE"
_MAX_AUTH_EXECUTABLE_BYTES = 512 * 1024 * 1024
_MAX_AUTH_STORE_FILE_BYTES = 64 * 1024 * 1024
_AUTH_STORE_READ_ERROR = "Kiro identity file could not be read safely"
# The Kiro CLI identity database is PROJECTED, never byte-copied. It is the CLI's
# main store: identity lives in two small tables, while `history` /
# `conversations*` hold chat transcripts and grow without bound (a real user
# report had it at ~429 MB). A byte copy therefore (a) blew past
# _MAX_AUTH_STORE_FILE_BYTES and aborted sign-in with a message that names
# neither size nor the real cause, and (b) read the whole file into memory to
# write it straight back out. Projection copies the full SCHEMA plus rows from
# _AUTH_IDENTITY_TABLES only, so the staged database is small and bounded by the
# identity data alone, no matter how large the source grows.
#
# Full schema, not just the identity tables: `migrations` is projected WITH its
# rows, so the CLI sees its schema version as already-applied and runs no
# migration. A database holding only the identity tables would then fail with
# "no such table: history" on first use. Copying every table's DDL (and indexes)
# while withholding non-identity ROWS keeps the CLI's queries valid and still
# hands the sandboxed process no transcript content.
_AUTH_SQLITE_DB = "data.sqlite3"
_AUTH_IDENTITY_TABLES = ("auth_kv", "migrations")
# `state` is a mixed key/value table: a few rows describe WHICH identity is signed
# in (Identity Center region + start URL, CodeWhisperer profile) and the rest is
# unrelated local state — telemetry ids, onboarding flags, prompt counters. Copy
# the table's rows SELECTIVELY by key prefix so the staged store can render a
# complete `whoami` (profile/ARN block) without also handing the sandboxed CLI
# the user's telemetry identifiers. Prefix-matched, not an exact list, so a new
# `auth.idc.*` key is carried automatically rather than silently dropped.
_AUTH_STATE_TABLE = "state"
_AUTH_STATE_KEY_PREFIXES = ("auth.", "api.codewhisperer.")
_AUTH_SQLITE_FILES = (_AUTH_SQLITE_DB,)
# Short: the projection is a handful of small reads against a local file, and a
# stuck open must not hold up sign-in.
_AUTH_SQLITE_TIMEOUT_SECS = 5.0
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
        "XDG_RUNTIME_DIR",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)
_PROBE_ENV_KEYS = frozenset(
    {
        "APPDATA",
        # Session bus + runtime dir: some Kiro CLI builds connect to the D-Bus
        # secret-service keyring at startup — even for ``--version`` — so the
        # probe must pass these through when the host sets them (e.g. AL2023,
        # where without them the CLI exits "Failed to connect to bus"). They are
        # only forwarded when present; hosts without a session bus (macOS, AL2,
        # headless) simply don't set them, so this is a no-op there.
        "DBUS_SESSION_BUS_ADDRESS",
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
        "XDG_RUNTIME_DIR",
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
    # ``(kind, detail)`` when the spawn was refused because the sandbox could not
    # be built — set ONLY from the typed SandboxUnavailableError, never inferred
    # from host capability. A probe that failed for any other reason leaves this
    # None, so an unrelated failure can never be misreported as a sandbox problem.
    sandbox_failure: tuple[str, str] | None = None


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
    # A Kiro CLI binary that is present and executable but could not be VERIFIED
    # (verification runs the binary inside the sandbox) is a categorically
    # different condition from a missing binary, and a failed sandbox build
    # carries zero information about whether the CLI is installed. Without these
    # fields the two collapse into ``installed=False`` and the dashboard tells
    # the user to install a CLI that is already there and authenticated.
    sandbox_unavailable: bool = False
    # Machine-readable: "transient" | "foreign_sandbox" | "no_backend". The
    # presentation layer maps this to its own translated remedy copy instead of
    # parsing English prose out of ``sandbox_detail``.
    sandbox_failure_kind: str = ""
    # Technical probe reason, e.g. "unshare(CLONE_NEWNS) failed with errno 1
    # (EPERM)". Names the failing step, so it is shown verbatim, untranslated.
    sandbox_detail: str = ""


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


@dataclass(frozen=True)
class TrustedAcpExecutableSnapshot:
    """The resolved Kiro CLI path for one ACP launch.

    ``launch_path`` is ALWAYS the user's installed binary, launched in place —
    KiroCrew never copies the CLI and runs the copy, because a multi-call Kiro
    CLI resolves its sibling subcommand executable relative to its own path.
    """

    launch_path: str


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


def _strip_progress_noise(text: str) -> str:
    """Drop ANSI control sequences and animated progress glyphs."""

    cleaned = _ANSI_ESCAPE_RE.sub("", str(text or ""))
    # Each progress-glyph run starts a repaint, so it doubles as a frame
    # boundary; a repaint is also usually delimited by a carriage return. Treat
    # both as line breaks so consecutive identical repaints collapse below --
    # and do it BEFORE _append_capped, which strips carriage returns and would
    # otherwise fuse every frame into one unsplittable line.
    cleaned = _PROGRESS_GLYPH_RE.sub("\n", cleaned)
    return cleaned.replace("\r", "\n")


def _dedupe_repeated_lines(text: str) -> str:
    """Collapse consecutive identical lines to one occurrence."""

    output: list[str] = []
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if output and output[-1] == stripped:
            continue
        output.append(stripped)
    result = "\n".join(output)
    # ``splitlines`` drops the final terminator, but this runs over the whole
    # accumulated detail on every stream read -- without it the next chunk fuses
    # onto the last stored line (device code, installer progress, URL fallback).
    if result and str(text or "").endswith(("\n", "\r")):
        result += "\n"
    return result


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


def _acp_executable_is_runnable(
    executable: str,
    *,
    platform_name: str,
) -> bool:
    """Return whether ACP may launch *executable*.

    Trust is "it runs" — install source, owner, and path do not gate ACP launch,
    so a toolbox / Homebrew / self-updated CLI is accepted like any other
    (KiroCrew is not the authority on where Kiro CLI is installed).

    A zero-byte candidate is still refused. An interrupted install or
    self-update can leave a truncated but executable ``kiro-cli``; exec'ing it
    dies without an ACP frame, surfacing as the same opaque
    "process exited (rc=None)" this module works to avoid. Rejecting it here
    turns that into a readable prerequisite error instead. (The retired copy
    path got this for free from the snapshot's ``fstat`` size guard.)
    """

    canonical = _canonical_candidate(executable)
    if not _is_runnable_executable(canonical, platform_name):
        return False
    try:
        return os.path.getsize(canonical) > 0
    except OSError:
        return False


def snapshot_trusted_acp_executable(
    executable: str,
    *,
    data_home: Path | None = None,
    platform_name: str | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> TrustedAcpExecutableSnapshot:
    """Return the user's installed Kiro CLI, to be launched IN PLACE.

    KiroCrew always executes the Kiro CLI the user installed, at the path it was
    resolved from. It never copies the binary elsewhere and runs the copy.

    Kiro CLI 2.15+ is a **multi-call binary**: it dispatches subcommands by
    exec'ing a SIBLING executable (e.g. ``kiro-cli-chat``) that it locates
    relative to its own path — on macOS by finding ``.app/Contents/MacOS/`` in
    that path. Copying the binary into a flat private directory destroys the
    sibling layout, so every dispatch fails with ``No such file or directory (os
    error 2)`` and ACP dies at handshake with ``process exited (rc=None)``. The
    same breaks any launcher that resolves adjacent resources: a multiplexer
    dispatching on ``argv[0]``, a wrapper reading a sibling registry, or a
    self-updating install whose payload lives beside it.

    An earlier design copied the bytes into a private snapshot (sealed memfd on
    Linux, verified copy on macOS) to close the resolve-to-exec window in which
    the file could be swapped. That is deliberately **not** done anymore: it
    defends against an attacker who already has write access to the user's own
    machine — a threat the rest of the product does not defend against either —
    and the cost was breaking every multi-call and multiplexer install outright.

    Trust is "the CLI runs": install source, owner, and path do not gate launch,
    so a toolbox / Homebrew / self-updated Kiro CLI launches like any other.
    Raises ``ValueError`` when the candidate is not a runnable executable.
    """

    platform_name = platform_name or sys.platform
    del data_home, environ  # neither provenance nor a data home gates ACP launch
    if platform_name == "win32":
        return TrustedAcpExecutableSnapshot(launch_path=_canonical_candidate(executable))
    if not _acp_executable_is_runnable(executable, platform_name=platform_name):
        raise ValueError("Kiro CLI is not a runnable executable for ACP execution")

    # Launch the path the caller resolved, NOT its realpath: a multiplexer
    # launcher (e.g. ``~/.toolbox/bin/kiro-cli`` → ``toolbox-exec``) dispatches
    # on its own argv[0] basename, and a multi-call binary resolves its sibling
    # subcommand relative to the invoked path. Both break under the realpath.
    #
    # ``abspath``, NOT ``realpath``: it anchors a relative candidate (an operator
    # ``KIROCREW_KIRO_BIN=./kiro-cli`` resolves against the GATEWAY's cwd, but ACP
    # spawns with ``cwd=<session work_dir>``, so a relative launch path would die
    # with ENOENT there) while leaving symlinks intact, so the multiplexer and
    # sibling-layout properties above still hold.
    return TrustedAcpExecutableSnapshot(launch_path=os.path.abspath(executable))


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


def _open_identity_db_readonly(path: Path) -> Any | None:
    """Open a Kiro identity database read-only, with the same path defenses.

    Mirrors :func:`_read_bounded_regular_file`'s gates — reject a symlink, and
    require a regular file — then hands SQLite a read-only URI. Deliberately NOT
    size-gated: only the identity tables are read out, so the source file's size
    does not bound what is staged. Returns ``None`` when the path fails a gate or
    SQLite cannot read it (missing, corrupt, or not a database).
    """

    if path.is_symlink():
        return None
    try:
        if not stat.S_ISREG(os.lstat(str(path)).st_mode):
            return None
    except OSError:
        return None
    # mode=ro WITHOUT immutable=1. `immutable=1` would promise never to touch a
    # sidecar, but it also tells SQLite the file cannot change, so the WAL is
    # IGNORED: against a store in WAL mode whose newest commits are still in
    # `data.sqlite3-wal`, the token row reads as missing and the CLI is handed a
    # store it reports as signed-out — a worse failure than the size abort this
    # projection replaces. Plain `mode=ro` applies the WAL, so the staged
    # identity always matches what the CLI itself would read. The cost is that
    # SQLite may create/refresh the `-shm` index beside the live database exactly
    # as any other reader does; `-shm` is a shared-memory index holding no
    # identity data, and no identity bytes are ever written back.
    uri = f"file:{urllib.request.pathname2url(str(path))}?mode=ro"
    try:
        return sqlite3.connect(uri, uri=True, timeout=_AUTH_SQLITE_TIMEOUT_SECS)
    except sqlite3.Error:
        return None


def _project_identity_database(source: Path, destination: Path) -> bool:
    """Stage ONLY Kiro's identity tables into a fresh owner-only database.

    Copies every table/index DDL (so the CLI never hits "no such table" on a
    store whose ``migrations`` rows say the schema is current) but only the ROWS
    of :data:`_AUTH_IDENTITY_TABLES`. Transcript tables arrive empty, so the
    staged file stays small however large the source grows.

    Returns ``False`` when the source cannot be read or lacks an identity table,
    so the caller aborts staging instead of running against a store that looks
    signed-out.
    """

    connection = _open_identity_db_readonly(source)
    if connection is None:
        return False
    try:
        with contextlib.closing(connection):
            schema = connection.execute(
                "SELECT sql FROM sqlite_master "  # wokeignore:rule=master
                "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            present = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"  # wokeignore:rule=master
                ).fetchall()
            }
            # EVERY identity table must exist. `all`, not `any`: a future kiro-cli
            # that renames one of them (say `auth_kv`) while keeping the other
            # would pass an `any` gate and stage a store with the schema present
            # but ZERO identity rows — silently producing exactly the "signed out"
            # outcome this gate exists to prevent. Requiring all of them turns a
            # schema change into the loud _AUTH_STORE_READ_ERROR abort instead.
            if not all(table in present for table in _AUTH_IDENTITY_TABLES):
                return False
            rows = {
                table: connection.execute(f'SELECT * FROM "{table}"').fetchall()
                for table in _AUTH_IDENTITY_TABLES
            }
            # `state`: carry only the identity-describing keys (see
            # _AUTH_STATE_KEY_PREFIXES). Absent on an older schema — optional by
            # design, so a store without it still stages.
            if _AUTH_STATE_TABLE in present:
                predicate = " OR ".join(["key LIKE ?"] * len(_AUTH_STATE_KEY_PREFIXES))
                rows[_AUTH_STATE_TABLE] = connection.execute(
                    f'SELECT * FROM "{_AUTH_STATE_TABLE}" WHERE {predicate}',
                    tuple(f"{prefix}%" for prefix in _AUTH_STATE_KEY_PREFIXES),
                ).fetchall()
    except sqlite3.Error:
        return False

    destination.parent.mkdir(parents=True, exist_ok=True)
    # Create the file before writing, so the identity rows are never briefly
    # world-readable between SQLite's create and the chmod.
    fd = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    platform_compat.restrict_to_owner(str(destination))
    try:
        staged = sqlite3.connect(str(destination), timeout=_AUTH_SQLITE_TIMEOUT_SECS)
        with contextlib.closing(staged):
            with staged:
                for (statement,) in schema:
                    staged.execute(str(statement))
                for table, table_rows in rows.items():
                    if not table_rows:
                        continue
                    placeholders = ",".join("?" * len(table_rows[0]))
                    staged.executemany(
                        f'INSERT INTO "{table}" VALUES ({placeholders})', table_rows
                    )
    except sqlite3.Error:
        with contextlib.suppress(OSError):
            os.unlink(str(destination))
        return False
    return True


def _atomic_write_secret_bytes(path: Path, content: bytes) -> None:
    """Atomically stage one bounded Kiro identity file with owner-only mode."""

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
    # A stray file or dangling/loop symlink at this path makes a plain
    # mkdir(exist_ok=True) raise FileExistsError and crash gateway boot. Remove
    # it before mkdir so boot self-heals. UNLINK rather than rename-aside: the
    # staging root holds credential material, and moving an unexpected file to a
    # sibling name would leave its (possibly sensitive) contents readable outside
    # the sandbox-hidden staging prefix. unlink() acts on the symlink itself,
    # never its target. (#561)
    if staging_parent.is_symlink() or (staging_parent.exists() and not staging_parent.is_dir()):
        try:
            staging_parent.unlink()
        except OSError as exc:
            # A concurrent gateway boot may have won the race and already cleared
            # the stray path (FileNotFoundError) or replaced it with the real
            # private directory. Only abort if a non-directory we cannot clear is
            # STILL sitting here; otherwise fall through to the idempotent mkdir.
            # (#561, concurrent-boot race)
            if staging_parent.is_symlink() or (staging_parent.exists() and not staging_parent.is_dir()):
                raise OSError(
                    f"Kiro auth staging root {staging_parent} is not a private "
                    "directory and could not be reset"
                ) from exc
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
        for mapping in _auth_store_mappings(platform_name, home, environ):
            for pattern in mapping.filenames:
                for source in mapping.source.glob(pattern):
                    staged_path = root / mapping.staged_relative / source.name
                    # The identity DATABASE is projected (identity tables only);
                    # every other identity file is a small JSON token copied
                    # under the bounded byte rules. Both abort staging on
                    # failure — never omit a matched store as though absent.
                    if source.name == _AUTH_SQLITE_DB:
                        if not _project_identity_database(source, staged_path):
                            raise OSError(_AUTH_STORE_READ_ERROR)
                        continue
                    content = _read_bounded_regular_file(source)
                    if content is None:
                        raise OSError(_AUTH_STORE_READ_ERROR)
                    _atomic_write_secret_bytes(staged_path, content)

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
        return _AuthWorkspace(root=root, env=env)
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise


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
            #
            # It also carries the resource limits (``--rlimits=``) that used to
            # ride on ``preexec_fn``. See resource_limit_supervisor_argv: a
            # preexec_fn forces a plain fork() of this multi-threaded gateway and
            # runs Python in the child before exec, which is how a child wedged
            # in a futex and pinned the fds it had inherited. The supervisor
            # applies the same setrlimits after exec, single-threaded, and the
            # exec'd child inherits them.
            spawn_argv = [
                sys.executable,
                "-I",
                "-c",
                _PROCESS_GROUP_SUPERVISOR_CODE,
                *resource_limit_supervisor_argv(),
                *spawn_argv,
            ]
        proc = await asyncio.create_subprocess_exec(
            *spawn_argv,
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=spawn_env,
            start_new_session=platform_compat.IS_POSIX,
            creationflags=creationflags,
        )
    except SandboxUnavailableError as exc:
        # The sandbox itself refused this spawn. Record it structurally so the
        # caller can report "present but unverifiable" without guessing from
        # host state — on Windows this wrap is skipped entirely, and the
        # allow_unsandboxed_exec opt-in bypasses it, so host capability is not
        # evidence about why THIS spawn failed.
        await _unlink_off_loop(cleanup_path)
        return ProcessResult(
            ok=False, error=str(exc), sandbox_failure=(exc.kind, exc.detail)
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
        warm_up_delay: float = _WARM_UP_DELAY_SECS,
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
        # real-home callers must leave those visible — the readiness probe so a
        # CLI whose valid session lives outside the staged files (an external
        # auth helper resolved from the real home) can read its own credentials,
        # and device login so kiro-cli can WRITE its own credential store there.
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
        # `assume_ready` must hold from CONSTRUCTION, not from the first probe:
        # readiness is now a latch that `session_ready()` reads without probing,
        # so an offline/test gateway that never probes would otherwise report
        # not-ready and its blocking gates (e.g. /api/models) would 503.
        self._status = (
            PrerequisiteStatus(
                platform=_platform_label(self._platform),
                installed=True,
                authenticated=True,
                ready=True,
                can_login=True,
                initial_setup_complete=True,
            )
            if assume_ready
            else PrerequisiteStatus(
                platform=_platform_label(self._platform),
                initial_setup_complete=self._initial_setup_complete,
            )
        )
        self._operation = OperationStatus()
        self._task: asyncio.Task[None] | None = None
        self._warm_up_task: asyncio.Task[None] | None = None
        # Injectable so tests need not sleep out the real boot-contention delay.
        self._warm_up_delay = warm_up_delay
        self._probe_lock = asyncio.Lock()
        self._last_probe_at = 0.0
        self._has_probed = False
        self._viable_binary = ""
        self._installer_plan: tuple[str, list[str]] | None = None

    @property
    def operation_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def initial_setup_complete(self) -> bool:
        """Whether this data home has already completed first-run setup.

        Derived from the data home at construction, so it is available BEFORE
        any CLI probe runs. Callers use it to classify a returning user without
        waiting on the probe, whose cold path spawns two sandboxed subprocesses
        (``--version``, then ``whoami``) and takes long enough for the dashboard
        to flash first-run setup chrome at someone who has used the app for
        months.
        """

        return self._initial_setup_complete

    def _snapshot_dict(self) -> dict[str, Any]:
        result = asdict(self._status)
        result["operation"] = asdict(self._operation)
        return result

    def warm_up(self) -> asyncio.Task[None] | None:
        """Resolve readiness in the background shortly after gateway start.

        The probe spawns two ``kiro-cli`` subprocesses, so it yields
        ``_WARM_UP_DELAY_SECS`` first rather than running inline: racing those
        spawns against the concurrent app-backend spawns measurably lengthened and
        destabilized boot (~2.7s → 2.8-5.6s on one real home). Running it at all
        means the answer is usually already there when a session is first started.

        Returns the task so callers (and tests) can await it; startup itself does
        NOT await it, and failures are contained. A warm-up is strictly an
        optimization: the dashboard renders without waiting for it (readiness
        gates nothing in the UI), so it must never delay boot.
        """

        if self._warm_up_task is not None:
            return self._warm_up_task

        async def _warm() -> None:
            try:
                if self._warm_up_delay > 0:
                    await asyncio.sleep(self._warm_up_delay)
                await self._probe()
            except asyncio.CancelledError:
                raise
            except Exception:
                # The lazy probe path stays authoritative and will retry, so a
                # failed warm-up must not fail gateway startup.
                logger.warning("Kiro readiness warm-up failed", exc_info=True)

        self._warm_up_task = asyncio.create_task(_warm())
        return self._warm_up_task

    async def snapshot(self, *, force: bool = False) -> dict[str, Any]:
        """Return the latched status, probing only on an explicit ``force``.

        Probing is boot-and-explicit-action only (see :meth:`session_ready`), so
        the polled status endpoint reads latched state and spawns nothing. The
        dashboard's Refresh button and the install/login operations pass
        ``force=True``; that is the supported way to re-probe on demand.
        """

        if force and not self.operation_running:
            await self._probe(force=True)
        elif not self._has_probed and not self.operation_running:
            # Nothing has resolved yet (warm-up still pending or it failed).
            # One probe here is the boot probe, just arriving late.
            await self._probe()
        return self._snapshot_dict()

    def mark_signed_out(self) -> None:
        """Latch readiness to signed-out after an observed ACP auth failure.

        The ACP attempt — not a probe — is what discovers a mid-session logout
        now that probing is boot-and-explicit-action only. Recording it here is
        what keeps the fail-closed gates (poll-driven spawn sites + destructive
        reruns) honest with no timer re-probe and no subprocess.

        Only ever *narrows* readiness (never grants it), and never touches
        ``initial_setup_complete``, so a returning user is not demoted to
        first-run setup. A running install/login operation owns the status, so
        this defers to it rather than racing its outcome.
        """

        if self._assume_ready or self.operation_running:
            return
        if not self._status.ready and not self._status.authenticated:
            return
        self._status = replace(self._status, authenticated=False, ready=False)
        # Force the next explicit probe (Refresh / login) to actually run rather
        # than being served by the short cache off this synthetic transition.
        self._last_probe_at = 0.0
        logger.info("Kiro readiness latched to signed-out after an ACP auth failure")

    async def verified_ready(self, *, max_age_secs: float) -> bool:
        """Return readiness backed by a probe no older than *max_age_secs*.

        For callers that act IRREVERSIBLY on the answer — the destructive reruns
        (which rewrite persisted history) and the poll-driven ``kiro-cli`` spawn
        sites (which would open an interactive browser login) — the latch is not
        a sufficient authority in EITHER direction. It is written at boot and
        narrowed only when a chat turn observes an auth failure, so an external
        logout with no chat turn in between would leave it ``ready=True``
        indefinitely.

        Re-probing here is bounded: only these paths call it, never the message
        hot path, and ``_PROBE_CACHE_SECS`` collapses a burst of callers onto one
        probe. A running install/login operation owns the status, so defer to its
        current value rather than racing it.
        """

        if self._assume_ready or self.operation_running:
            return bool(self._status.ready)
        if not self._has_probed or self._clock() - self._last_probe_at >= max_age_secs:
            try:
                await self._probe(force=True)
            except Exception:
                # A probe that cannot run is not evidence of readiness. Fail
                # closed: these callers must never act on a guess.
                logger.warning("Kiro verification probe failed; denying", exc_info=True)
                return False
        return bool(self._status.ready)

    async def session_ready(self) -> bool:
        """Return the latched readiness. Never probes; never blocks a turn.

        Readiness is resolved ONCE at gateway start (:meth:`warm_up`) and then
        refreshed only by an explicit user action — the dashboard's Refresh, or
        an install/login operation. There is deliberately no timer re-probe and
        no probe on the send path: a signed-out CLI is discovered by the ACP
        attempt itself, which raises ``AcpAuthRequired`` and surfaces an
        actionable sign-in error in the chat transcript.

        Because this value can therefore be arbitrarily stale, turn-starting
        callers treat it as ADVISORY and let the real ACP attempt be the
        authority (see ``dashboard/kiro_readiness.py``). Poll-driven
        ``kiro-cli`` spawn sites still gate on it — they have no turn to carry
        the failure, and an unauthenticated spawn opens a browser window on
        every poll.
        """

        return bool(self._status.ready)

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
            version_probe: ProcessResult | None = None
            for executable in candidates[:1]:
                result = await self._audited_probe(
                    "probe_version",
                    executable,
                    ["--version"],
                )
                version_probe = result
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
                # Was the probe refused BY THE SANDBOX, as opposed to failing for
                # any other reason? Verification runs the candidate inside the
                # sandbox (see _UNVERIFIED_SANDBOX_MODE), so on a host that
                # cannot build one a perfectly good, already-authenticated CLI
                # fails verification and must not be reported as missing.
                #
                # This keys on the typed failure the spawn actually raised, NOT
                # on whether the host has a backend. Host capability is not
                # evidence: _run_process skips the wrap on Windows, and the
                # allow_unsandboxed_exec opt-in bypasses it, so a broken CLI on
                # either would otherwise be blamed on the sandbox and lose the
                # repair actions that would genuinely help.
                sandbox_failure = version_probe.sandbox_failure if version_probe else None
                first_candidate = candidates[0] if candidates else ""
                # Off-loop: _is_runnable_executable realpath()s and stat()s the
                # candidate, and a stalled NFS/autofs mount would block those
                # syscalls indefinitely — on the event loop that freezes the whole
                # gateway, not just this probe.
                candidate_runnable = bool(first_candidate) and await asyncio.to_thread(
                    _is_runnable_executable, first_candidate, self._platform
                )
                if sandbox_failure is not None and candidate_runnable:
                    kind, detail = sandbox_failure
                    logger.warning(
                        "Kiro CLI at %s is present and executable but could not be "
                        "verified: the sandbox refused the probe (%s: %s)",
                        first_candidate,
                        kind,
                        detail,
                    )
                    self._status = PrerequisiteStatus(
                        platform=_platform_label(self._platform),
                        # Present and executable on disk. Verification is what
                        # failed, and it failed for an unrelated reason.
                        installed=True,
                        # Unknown, not false: whoami also runs through the probe
                        # path, so we cannot claim either way.
                        authenticated=False,
                        ready=False,
                        # Reinstalling and signing in both fix nothing here, so
                        # offer neither — a button that cannot help is the
                        # dead-end this change exists to remove.
                        can_auto_install=False,
                        can_login=False,
                        repair_required=False,
                        initial_setup_complete=self._initial_setup_complete,
                        sandbox_unavailable=True,
                        sandbox_failure_kind=kind,
                        sandbox_detail=detail,
                    )
                    self._last_probe_at = self._clock()
                    self._has_probed = True
                    return self._status
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

    async def _run_auth_command(
        self,
        executable: str,
        args: list[str],
        *,
        base_env: dict[str, str],
        timeout_secs: float,
        on_output: Callable[[str], None] | None = None,
        isolate_home: bool = True,
    ) -> ProcessResult:
        """Run one Kiro auth command against a sandboxed, trusted executable.

        The CLI is trusted because it runs (its install source, owner, and path
        do not gate sign-in); KiroCrew is not the authority on where Kiro CLI is
        installed. The user's installed binary is executed IN PLACE — never a
        private copy: Kiro CLI 2.15+ is a multi-call binary that exec's a sibling
        ``kiro-cli-chat`` resolved relative to its own path, so a copy into a
        flat staging dir fails with ENOENT.

        ``isolate_home=False`` runs against the user's real home (like an ACP
        session), so a CLI whose session/registry lives in the real home is
        detected and device login writes its own credential store where the CLI
        normally keeps it. ``isolate_home=True`` keeps a credential-minimal
        temporary HOME holding only Kiro identity files, for read-only probes
        that must never see the real ``~/.aws`` / ``~/.ssh``.
        """

        if not isolate_home:
            # Run the way a real ACP session runs the CLI (acp/runtime.py):
            # against the real environment/home under the standard OS sandbox. A
            # rewritten HOME breaks CLIs whose session or tool registry lives in
            # the real home (e.g. a toolbox multiplexer that resolves itself via
            # its real-home registry), so this is what actually detects them, and
            # it is where the CLI's own credential store belongs.
            #
            # SECURITY: this matches the accepted ACP launch posture, not a new
            # surface — ACP already runs the resolved kiro-cli with the full real
            # environment on every session (the standard sandbox intentionally
            # exposes AWS/SSH to it). Device sign-in writes kiro-cli's OWN
            # credential store in its own home; that write is the entire point of
            # delegating sign-in to the CLI, and it targets the same store an ACP
            # session already reads. KiroCrew stages nothing and publishes
            # nothing. Only Kiro Crew's own secret home is hidden, and the user's
            # binary runs IN PLACE, exactly as ACP runs it, so a multi-call CLI
            # can still reach its sibling subcommand executable.
            return await self._run(
                executable,
                args,
                env=base_env,
                timeout_secs=timeout_secs,
                on_output=on_output,
                sandboxed=True,
                sandbox_mode=_KIRO_AUTH_SANDBOX_MODE,
                extra_hidden_dirs=self._crew_hidden_dirs,
            )

        workspace = await asyncio.to_thread(
            _prepare_auth_workspace,
            self._platform,
            self._home,
            self._environ,
            base_env,
        )
        try:
            # The user's binary runs IN PLACE — never a copy staged under the
            # workspace. Kiro CLI 2.15+ dispatches subcommands by exec'ing a
            # sibling executable resolved relative to its own path, which a flat
            # copy destroys (ENOENT). Only the credential workspace is staged.
            return await self._run(
                executable,
                args,
                env=workspace.env,
                timeout_secs=timeout_secs,
                on_output=on_output,
                sandboxed=True,
                sandbox_mode=_KIRO_AUTH_SANDBOX_MODE,
                extra_hidden_dirs=self._hidden_probe_dirs,
                extra_visible_dirs=(str(workspace.root),),
            )
        finally:
            # Read-only probe home: nothing to publish, just remove it.
            await asyncio.to_thread(shutil.rmtree, str(workspace.root), ignore_errors=True)

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
        # Read the login URL out of the RAW chunk first: the detector is strict
        # about host and path, and noise filtering must never be able to hide a
        # legitimate sign-in URL from the user.
        url = extract_secure_login_url(chunk)
        detail = _dedupe_repeated_lines(
            _append_capped(self._operation.detail, _strip_progress_noise(chunk))
        )
        self._operation.detail = _sanitize_detail(detail)
        if self._operation.kind == "login":
            # One stream read can split a URL across two chunks, so keep the
            # accumulated-text fallback: the filter only inserts line breaks at
            # frame boundaries, which a sign-in URL never contains.
            url = url or extract_secure_login_url(detail)
            if url:
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

            self._operation.message = "Starting Kiro sign-in…"
            # kiro-cli owns the whole device flow: it runs against the real home
            # and writes its own credential store, exactly as it does from a
            # terminal. KiroCrew stages no credentials and copies none back.
            result = await self._run_auth_command(
                self._viable_binary,
                ["login", "--use-device-flow"],
                base_env=self._child_environment,
                timeout_secs=_LOGIN_TIMEOUT_SECS,
                on_output=self._capture_operation_output,
                isolate_home=False,
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
        for task in (self._task, self._warm_up_task):
            if task is not None and not task.done() and task not in tasks:
                task.cancel()
                tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
