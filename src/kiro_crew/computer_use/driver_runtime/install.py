"""Installing the driver: download the pinned archive, verify it, extract it safely.

Layout under the data home::

    <home>/computer-use/
        driver/<version>/        extracted archive, read-only after install
        driver/.staging-*/       a download in progress (removed on every exit path)
        home/                    the driver's private CUA_DRIVER_RS_HOME
        install.json             the install record, written last

Every check fails closed and runs before the next step can do harm:

1. the download is HTTPS, and every redirect it follows stays HTTPS;
2. the byte count may not exceed the pinned size while streaming, and must equal
   it at the end; the sha256 must equal the pinned digest. Nothing is extracted
   before both hold;
3. extraction refuses absolute paths, ``..`` components, drive letters, device
   and FIFO members, and any link whose target leaves the extraction root;
4. on macOS the extracted ``CuaDriver.app`` must pass ``codesign --verify
   --strict`` and carry team ``YCK386LBJ7`` and bundle id ``com.trycua.driver``;
   on Windows the executable's Authenticode signature must be valid and name
   ``Cua AI, Inc.``;
5. ``--version`` must print the pinned version (run with the fenced environment);
6. the tree is renamed into place atomically, write bits are removed, and the
   record (with the binary's own sha256) is written last.

At every later start :func:`verify_installed` re-hashes the binary against the
record BEFORE executing it, then re-checks ``--version``. A mismatch never runs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable
from contextlib import suppress as contextlib_suppress
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from kiro_crew import _ssl_compat, platform_compat
from kiro_crew.computer_use.driver_runtime import env as driver_env_mod
from kiro_crew.computer_use.driver_runtime.pin import (
    DRIVER_VERSION,
    MACOS_BUNDLE_ID,
    MACOS_TEAM_ID,
    OS_LINUX,
    OS_MACOS,
    OS_WINDOWS,
    WINDOWS_SIGNER,
    PinnedAsset,
    asset_for,
)

logger = logging.getLogger(__name__)

#: Crew-home leaf holding everything this module writes.
RUNTIME_LEAF = "computer-use"
DRIVER_DIR_NAME = "driver"
PRIVATE_HOME_NAME = "home"
RECORD_FILE = "install.json"
STAGING_PREFIX = ".staging-"

STAGE_PREPARING = "preparing"
STAGE_DOWNLOADING = "downloading"
STAGE_VERIFYING = "verifying"
STAGE_EXTRACTING = "extracting"
STAGE_SIGNATURE = "checking_signature"
STAGE_VERSION = "checking_version"
STAGE_FINISHING = "finishing"
STAGE_PLACING = "placing_app"
STAGE_DONE = "done"
#: The driver was killed or refused by the OS as it started (not a bad install).
STAGE_BLOCKED = "blocked"
BLOCKED_FILE = "blocked.json"

_CHUNK = 1 << 16
_READ_TIMEOUT_SECS = 60.0
_VERSION_TIMEOUT_SECS = 20.0
_SIGNATURE_TIMEOUT_SECS = 60.0


class InstallError(Exception):
    """A failed install step. ``stage`` names the step; the message is user-facing."""

    def __init__(self, stage: str, detail: str) -> None:
        super().__init__(detail)
        self.stage = stage
        self.detail = detail


class DriverBlocked(InstallError):
    """The driver binary was stopped by the operating system the moment it started.

    Seen on managed Macs where endpoint security software SIGKILLs an allowed,
    signed and notarized binary at exec. Not a version mismatch and not a broken
    install, so it is reported as itself. Kiro Crew never retries it in a loop and
    never tries to get around the block.
    """

    def __init__(self, how: str, os_id: str) -> None:
        super().__init__(STAGE_BLOCKED, blocked_message(how, os_id))
        self.how = how


#: Windows exit codes and spawn errors that mean "the system refused to run it".
_WINDOWS_BLOCKED_EXIT = {
    0xC0000022: "access denied",  # STATUS_ACCESS_DENIED
    0xC0000428: "invalid image hash",  # STATUS_INVALID_IMAGE_HASH (code integrity)
    0xC0000361: "blocked by policy",  # STATUS_ACCESS_DISABLED_BY_POLICY_DEFAULT
    0xC0000362: "blocked by policy",  # STATUS_ACCESS_DISABLED_BY_POLICY_PATH
    0xC0000363: "blocked by policy",  # STATUS_ACCESS_DISABLED_BY_POLICY_PUBLISHER
    0xC0000364: "blocked by policy",  # STATUS_ACCESS_DISABLED_BY_POLICY_OTHER
}
_WINDOWS_BLOCKED_SPAWN = {
    5: "access denied",  # ERROR_ACCESS_DENIED
    225: "flagged by security software",  # ERROR_VIRUS_INFECTED
    226: "flagged by security software",  # ERROR_VIRUS_DELETED
    1260: "blocked by policy",  # ERROR_ACCESS_DISABLED_BY_POLICY
    4551: "blocked by policy",  # ERROR_SYSTEM_INTEGRITY_POLICY_VIOLATION
}


def blocked_how(
    returncode: "int | None" = None, error: "OSError | None" = None, *, os_id: "str | None" = None
) -> "str | None":
    """How the OS stopped the driver at launch, or ``None`` when it did not.

    POSIX: a negative return code (killed by a signal) or a spawn refused with
    EPERM / EACCES. Windows: the access-denied and blocked-by-policy exit codes and
    spawn errors. Anything else (a non-zero exit, wrong output) is not a block.
    """
    host = os_id or current_os()
    if error is not None:
        if host == OS_WINDOWS:
            return _WINDOWS_BLOCKED_SPAWN.get(getattr(error, "winerror", None) or -1)
        import errno as errno_mod

        if error.errno in (errno_mod.EPERM, errno_mod.EACCES):
            return "the system refused to start it"
        return None
    if returncode is None:
        return None
    if host == OS_WINDOWS:
        return _WINDOWS_BLOCKED_EXIT.get(returncode & 0xFFFFFFFF)
    if returncode < 0:
        return f"signal {-returncode}"
    return None


def blocked_message(how: str, os_id: str) -> str:
    """The plain, model-safe reason shown in setup and in every tool refusal."""
    text = (
        f"The driver was stopped as soon as it started ({how}). Security software on "
        "this computer is probably blocking it. Ask your IT team to allow CuaDriver"
    )
    if os_id in (OS_MACOS, OS_WINDOWS):
        return text + ", or keep using the built-in driver."
    return text + "."


def blocked_path(home: "str | os.PathLike[str]") -> Path:
    return runtime_root(home) / BLOCKED_FILE


def record_blocked(home: "str | os.PathLike[str]", how: str) -> None:
    """Remember that the installed driver was blocked, for ``auto`` and for setup."""
    path = blocked_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"how": how, "at": int(time.time())}) + "\n"
    # A private temp file (``mkstemp``: O_EXCL, 0600) renamed into place, so two
    # writers never race on creating the same name.
    fd, tmp = tempfile.mkstemp(prefix=".blocked-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, path)
    except BaseException:
        with contextlib_suppress(OSError):
            os.unlink(tmp)
        raise


def clear_blocked(home: "str | os.PathLike[str]") -> None:
    try:
        blocked_path(home).unlink()
    except FileNotFoundError:
        pass


def read_blocked(home: "str | os.PathLike[str]") -> str:
    """``how`` the installed driver was last blocked, or ``""`` (display read only)."""
    try:
        data = json.loads(blocked_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    how = data.get("how") if isinstance(data, dict) else None
    return how if isinstance(how, str) else ""


class UnsafeArchiveMember(InstallError):
    """An archive member that would land outside the extraction root."""

    def __init__(self, member: str, why: str) -> None:
        super().__init__(STAGE_EXTRACTING, f"unsafe archive member refused: {member!r} ({why})")
        self.member = member


@dataclass(frozen=True)
class InstallState:
    """What is on disk. Read without executing anything."""

    installed: bool
    version: str = ""
    binary: "Path | None" = None
    binary_sha256: str = ""
    asset: str = ""
    detail: str = ""


@dataclass
class InstallProgress:
    """The in-flight install, for the setup state machine to report."""

    running: bool = False
    stage: str = ""
    error: str = ""
    error_stage: str = ""
    bytes_done: int = 0
    bytes_total: int = 0
    finished_at: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "stage": self.stage,
            "error": self.error,
            "error_stage": self.error_stage,
            "bytes_done": self.bytes_done,
            "bytes_total": self.bytes_total,
        }


#: One install at a time per process; the setup module reads this.
PROGRESS = InstallProgress()


# ── paths ──


def runtime_root(home: "str | os.PathLike[str]") -> Path:
    return Path(home) / RUNTIME_LEAF


def driver_root(home: "str | os.PathLike[str]") -> Path:
    return runtime_root(home) / DRIVER_DIR_NAME


def version_dir(home: "str | os.PathLike[str]", version: str = DRIVER_VERSION) -> Path:
    return driver_root(home) / version


def private_home(home: "str | os.PathLike[str]") -> Path:
    """The driver's own home, created owner-only."""
    path = runtime_root(home) / PRIVATE_HOME_NAME
    path.mkdir(parents=True, exist_ok=True)
    platform_compat.chmod_safe(path, 0o700)
    return path


def record_path(home: "str | os.PathLike[str]") -> Path:
    return runtime_root(home) / RECORD_FILE


#: Where the macOS driver app lives. Upstream's ``cua-driver mcp`` on macOS starts
#: the app daemon from this exact location and nowhere else, so the owner's install
#: places the verified bundle here. A module attribute so tests point it at a
#: temporary directory; nothing else changes it.
APPLICATIONS_DIR = Path("/Applications")
MACOS_APP_NAME = "CuaDriver.app"
MACOS_APP_BINARY = "Contents/MacOS/cua-driver"
_LSREGISTER = (
    "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework"
    "/Support/lsregister"
)

ERR_FOREIGN_APP = (
    "another version of CuaDriver is installed at {path}; remove it or update it to "
    "{version}, then install the driver again ({why})"
)
ERR_APPS_NOT_WRITABLE = (
    "{dir} is not writable for this user, so the driver app cannot be placed there; "
    "an administrator can make it writable or install CuaDriver {version} there, then "
    "install the driver again"
)
ERR_APP_GONE = "CuaDriver.app is no longer in {dir}; install the driver again"


def applications_app() -> Path:
    """``/Applications/CuaDriver.app`` (or the test root's equivalent)."""
    return Path(APPLICATIONS_DIR) / MACOS_APP_NAME


def current_os() -> str:
    if platform_compat.IS_MACOS:
        return OS_MACOS
    if platform_compat.IS_WINDOWS:
        return OS_WINDOWS
    if platform_compat.IS_LINUX:
        return OS_LINUX
    return ""


# ── state ──


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_record(home: "str | os.PathLike[str]") -> dict[str, Any]:
    try:
        data = json.loads(record_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _pinned_for_host(asset: "PinnedAsset | None") -> "PinnedAsset | None":
    return asset if asset is not None else asset_for(current_os())


def install_state(
    home: "str | os.PathLike[str]", *, asset: "PinnedAsset | None" = None
) -> InstallState:
    """The install on disk. Never executes anything, never raises.

    The binary's location and expected digest come from the PIN for this host, not
    from the record: the record says an install finished, nothing more.
    """
    record = read_record(home)
    pinned = _pinned_for_host(asset)

    def _text(key: str) -> str:
        value = record.get(key)
        return value if isinstance(value, str) else ""

    version = _text("version")
    if not record:
        return InstallState(installed=False, detail="not installed")
    if version != DRIVER_VERSION:
        return InstallState(
            installed=False,
            version=version,
            detail=f"installed version {version or '?'} is not the pinned {DRIVER_VERSION}",
        )
    if pinned is None or _text("asset") != pinned.name:
        return InstallState(
            installed=False, version=version, detail="the install is for another OS or CPU"
        )
    binary = version_dir(home, version) / PurePosixPath(pinned.binary)
    if pinned.os == OS_MACOS:
        app_record = record.get("applications_app")
        app = applications_app()
        if not isinstance(app_record, dict) or app_record.get("path") != str(app):
            return InstallState(
                installed=False,
                version=version,
                detail=ERR_APP_GONE.format(dir=APPLICATIONS_DIR),
            )
        binary = app / MACOS_APP_BINARY
        if not binary.is_file():
            return InstallState(
                installed=False,
                version=version,
                detail=ERR_APP_GONE.format(dir=APPLICATIONS_DIR),
            )
    try:
        present = binary.is_file()
    except OSError:
        present = False
    if not present:
        return InstallState(installed=False, version=version, detail="the driver binary is missing")
    return InstallState(
        installed=True,
        version=version,
        binary=binary,
        binary_sha256=pinned.binary_sha256 or _text("binary_sha256"),
        asset=pinned.name,
    )


def exec_ready_binary(
    home: "str | os.PathLike[str]", *, asset: "PinnedAsset | None" = None
) -> Path:
    """The driver binary Kiro Crew may execute directly, re-verified right now.

    The ONE source of a driver executable for a direct exec (``mcp``, ``stop``,
    ``--version``) on every platform: the copy in the sealed
    ``<data-home>/computer-use/driver/<version>`` tree (read-only to the agent's
    sandbox), hashed against the PIN immediately before it is returned. On macOS
    this is never ``/Applications/CuaDriver.app``: that copy is writable by the
    user, so a sandboxed agent could replace it between a check and an exec. It is
    only ever started through LaunchServices, after :func:`check_app_copy`.

    Raises :class:`InstallError` when the copy is missing, a link, or not the pin.
    """
    pinned = _pinned_for_host(asset)
    if pinned is None or not pinned.binary_sha256:
        raise InstallError(STAGE_VERIFYING, "there is no pinned driver for this computer")
    binary = version_dir(home) / PurePosixPath(pinned.binary)
    try:
        if binary.is_symlink() or not binary.is_file():
            raise InstallError(STAGE_VERIFYING, "the installed driver binary is missing")
        digest = sha256_file(binary)
    except OSError as exc:
        raise InstallError(
            STAGE_VERIFYING, f"the installed driver cannot be read ({exc.strerror or exc})"
        ) from exc
    if digest != pinned.binary_sha256:
        raise InstallError(
            STAGE_VERIFYING,
            "the installed driver does not match its pinned hash; it was not run (reinstall it)",
        )
    return binary


def verify_installed(
    home: "str | os.PathLike[str]",
    *,
    asset: "PinnedAsset | None" = None,
    run: "Callable[..., subprocess.CompletedProcess[str]] | None" = None,
) -> "tuple[Path | None, str]":
    """``(binary, "")`` when the installed driver may run, else ``(None, reason)``.

    Hashes the binary against the PINNED digest first, so a replaced file never
    executes even when the record beside it was rewritten to match; on macOS the
    bundle's code signature is re-verified; only then is ``--version`` run.
    """
    pinned = _pinned_for_host(asset)
    state = install_state(home, asset=pinned)
    if not state.installed or state.binary is None or pinned is None:
        return None, state.detail or "not installed"
    try:
        actual = sha256_file(state.binary)
    except OSError as exc:
        return None, f"the driver binary cannot be read ({exc.strerror or exc})"
    expected = pinned.binary_sha256 or str(read_record(home).get("binary_sha256") or "")
    if not expected or actual != expected:
        return None, "the driver binary does not match its pinned hash; reinstall it"
    try:
        sealed = exec_ready_binary(home, asset=pinned)
    except InstallError as exc:
        return None, exc.detail
    if pinned.os == OS_MACOS:
        # The copy LaunchServices starts: the one in /Applications. Signature, team,
        # bundle id and pinned hash every time; ``--version`` runs on the sealed copy,
        # so nothing in /Applications is ever executed directly.
        why = check_app_copy(applications_app(), pinned, home, run=run, version_binary=sealed)
        if why:
            return None, f"{applications_app()} failed its checks ({why}); it was not started"
        return sealed, ""
    reason = check_version(sealed, home, run=run)
    if reason:
        return None, reason
    return sealed, ""


def check_version(
    binary: Path,
    home: "str | os.PathLike[str]",
    *,
    run: "Callable[..., subprocess.CompletedProcess[str]] | None" = None,
) -> str:
    """``""`` when ``binary --version`` prints the pinned version, else the reason.

    Raises :class:`DriverBlocked` when the system stops the binary as it starts.
    """
    runner = run or subprocess.run
    try:
        done = runner(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_VERSION_TIMEOUT_SECS,
            env=driver_env_mod.driver_env(os.environ, private_home(home)),
            stdin=subprocess.DEVNULL,
            cwd=str(private_home(home)),
        )
    except OSError as exc:
        how = blocked_how(error=exc)
        if how:
            raise DriverBlocked(how, current_os()) from exc
        return f"the driver did not report its version ({exc})"
    except subprocess.TimeoutExpired as exc:
        return f"the driver did not report its version ({exc})"
    how = blocked_how(done.returncode)
    if how:
        # Killed at launch (endpoint security software on a managed computer): say
        # so, rather than "version '?'". One attempt, no retry, no workaround.
        raise DriverBlocked(how, current_os())
    words = [word.removeprefix("v") for word in (done.stdout or "").split()]
    if done.returncode != 0 or DRIVER_VERSION not in words:
        printed = (done.stdout or done.stderr or "").strip().splitlines()[:1]
        return f"the driver reports version {printed[0] if printed else '?'!r}, not the pinned {DRIVER_VERSION}"
    return ""


# ── download ──


def download_verified(
    asset: PinnedAsset,
    dest: Path,
    *,
    progress: "InstallProgress | None" = None,
) -> None:
    """Fetch *asset* to *dest* through :func:`asset_downloader.download_to`.

    The shared downloader enforces HTTPS, the pinned size as a ceiling and the
    pinned SHA-256, stages through a no-follow file and swaps the verified file
    in atomically. The pinned GitHub release redirects to its asset host, which
    is why cross-host redirects (HTTPS only) are allowed for this constant URL.
    """
    from kiro_crew import asset_downloader

    def on_progress(done: int, _total: int) -> None:
        if progress is not None:
            progress.bytes_done = done

    ok, why = asset_downloader.download_to(
        dest,
        asset.url,
        sha256=asset.sha256,
        size=asset.size,
        min_bytes=asset.size,
        on_progress=on_progress,
        allow_cross_host_redirects=True,
        label=f"the desktop driver {asset.name}",
    )
    if not ok:
        stage = STAGE_VERIFYING if ("sha256" in why or "byte" in why) else STAGE_DOWNLOADING
        note = _ssl_compat.system_trust_note() if stage == STAGE_DOWNLOADING else ""
        raise InstallError(stage, f"{why}; refused" + (f"; {note}" if note else ""))
    if progress is not None:
        progress.bytes_done = asset.size


# ── safe extraction ──


def _member_parts(name: str) -> PurePosixPath:
    """Validate one archive member name; return it as a relative POSIX path."""
    normalized = name.replace("\\", "/")
    if not normalized or normalized.startswith("/"):
        raise UnsafeArchiveMember(name, "absolute path")
    if len(normalized) >= 2 and normalized[1] == ":":
        raise UnsafeArchiveMember(name, "drive-qualified path")
    parts = PurePosixPath(normalized)
    if any(part == ".." for part in parts.parts):
        raise UnsafeArchiveMember(name, "parent-directory component")
    return parts


def _link_stays_inside(member_path: PurePosixPath, target: str) -> bool:
    if not target or target.startswith("/") or (len(target) >= 2 and target[1] == ":"):
        return False
    depth = 0
    for part in (member_path.parent / PurePosixPath(target.replace("\\", "/"))).parts:
        if part in ("", "."):
            continue
        if part == "..":
            depth -= 1
            if depth < 0:
                return False
        else:
            depth += 1
    return True


def check_tar_members(members: "list[tarfile.TarInfo]") -> None:
    for member in members:
        path = _member_parts(member.name)
        if member.isdev() or member.isfifo():
            raise UnsafeArchiveMember(member.name, "device or FIFO member")
        if member.islnk() or member.issym():
            if member.islnk():
                _member_parts(member.linkname)
                continue
            if not _link_stays_inside(path, member.linkname):
                raise UnsafeArchiveMember(
                    member.name, f"link target {member.linkname!r} leaves the archive"
                )


def extract_tar(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:*") as tf:
        members = tf.getmembers()
        check_tar_members(members)
        # ``data`` filter as a second fence: it also strips set-id bits and
        # refuses anything the explicit check above missed.
        tf.extractall(dest, members=members, filter="data")


_ZIP_TYPE_MASK = 0o170000
_ZIP_SYMLINK = 0o120000


def extract_zip(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with zipfile.ZipFile(archive) as zf:
        infos = zf.infolist()
        for info in infos:
            _member_parts(info.filename)
            mode = info.external_attr >> 16
            if (mode & _ZIP_TYPE_MASK) == _ZIP_SYMLINK:
                raise UnsafeArchiveMember(info.filename, "symlink member")
        for info in infos:
            out = (dest / _member_parts(info.filename)).resolve()
            if out != root and root not in out.parents:
                raise UnsafeArchiveMember(info.filename, "resolves outside the extraction root")
            if info.is_dir():
                out.mkdir(parents=True, exist_ok=True)
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, out.open("wb") as fh:
                shutil.copyfileobj(src, fh, _CHUNK)
            perm = (info.external_attr >> 16) & 0o777
            if perm and os.name != "nt":
                os.chmod(out, (perm & 0o755) | stat.S_IRUSR | stat.S_IWUSR)


# ── signatures ──


Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def _run_tool(run: "Runner | None", argv: list[str]) -> "subprocess.CompletedProcess[str]":
    runner = run or subprocess.run
    return runner(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_SIGNATURE_TIMEOUT_SECS,
        stdin=subprocess.DEVNULL,
    )


def verify_macos_bundle(app: Path, *, run: "Runner | None" = None) -> None:
    """Refuse unless *app* is the notarized ``CuaDriver.app`` from the pinned team."""
    try:
        strict = _run_tool(run, ["/usr/bin/codesign", "--verify", "--strict", "--deep", str(app)])
        info = _run_tool(run, ["/usr/bin/codesign", "-dv", "--verbose=4", str(app)])
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallError(STAGE_SIGNATURE, f"codesign could not run ({exc})") from exc
    if strict.returncode != 0:
        raise InstallError(STAGE_SIGNATURE, "the driver bundle's code signature does not verify")
    text = f"{info.stdout}\n{info.stderr}"
    fields = dict(
        line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith(" ")
    )
    if fields.get("TeamIdentifier", "").strip() != MACOS_TEAM_ID:
        raise InstallError(STAGE_SIGNATURE, "the driver bundle is not signed by the pinned team")
    if fields.get("Identifier", "").strip() != MACOS_BUNDLE_ID:
        raise InstallError(STAGE_SIGNATURE, "the driver bundle's identifier is not the pinned one")


def _authenticode_script(exe: Path) -> str:
    """The PowerShell check for *exe*, with the path as a quoted literal.

    ``powershell -Command <script> <arg>`` does NOT bind ``<arg>`` to ``$args``: it
    appends it to the script text, so the path must be inside the script itself.
    """
    literal = "'" + str(exe).replace("'", "''") + "'"
    return (
        f"$s = Get-AuthenticodeSignature -LiteralPath {literal}; "
        "Write-Output $s.Status; Write-Output $s.SignerCertificate.Subject"
    )


def verify_windows_signature(exe: Path, *, run: "Runner | None" = None) -> None:
    """Refuse unless *exe* carries a valid Authenticode signature from the pinned signer."""
    try:
        done = _run_tool(
            run,
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _authenticode_script(exe),
            ],
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallError(STAGE_SIGNATURE, f"the signature check could not run ({exc})") from exc
    lines = [line.strip() for line in (done.stdout or "").splitlines() if line.strip()]
    status = lines[0] if lines else ""
    subject = lines[1] if len(lines) > 1 else ""
    if done.returncode != 0 or status != "Valid":
        raise InstallError(
            STAGE_SIGNATURE,
            f"the driver's Authenticode signature is not valid ({status or 'unknown'})",
        )
    if subject_common_name(subject) != WINDOWS_SIGNER:
        raise InstallError(STAGE_SIGNATURE, "the driver is not signed by the pinned publisher")


def subject_common_name(subject: str) -> str:
    """The ``CN`` value of an X.500 subject string, quotes removed (``""`` if absent).

    Windows prints a CN that contains a comma quoted: ``CN="Cua AI, Inc.", O=...``.
    """
    text = subject.strip()
    i = 0
    while i < len(text):
        while i < len(text) and text[i] in " ,":
            i += 1
        eq = text.find("=", i)
        if eq < 0:
            return ""
        key = text[i:eq].strip().upper()
        j = eq + 1
        if j < len(text) and text[j] == '"':
            end = j + 1
            chars: list[str] = []
            while end < len(text):
                if text[end] == '"':
                    if end + 1 < len(text) and text[end + 1] == '"':
                        chars.append('"')
                        end += 2
                        continue
                    break
                chars.append(text[end])
                end += 1
            value = "".join(chars)
            nxt = text.find(",", end)
            i = len(text) if nxt < 0 else nxt + 1
        else:
            end = text.find(",", j)
            end = len(text) if end < 0 else end
            value = text[j:end].strip()
            i = end + 1
        if key == "CN":
            return value
    return ""


# ── macOS: the app in /Applications ──


def check_app_copy(
    app: Path,
    pinned: PinnedAsset,
    home: "str | os.PathLike[str]",
    *,
    run: "Runner | None" = None,
    version_binary: "Path | None" = None,
    check_running_version: bool = True,
) -> str:
    """``""`` when *app* is exactly the pinned, signed driver app, else why not.

    Ordered so nothing is executed before it is proven: link and file checks, the
    code signature (team and bundle id), the binary's pinned hash, and only then
    ``--version``. *version_binary* runs ``--version`` on that (already verified)
    copy instead of *app*'s own binary; ``check_running_version=False`` skips it.
    """
    if app.is_symlink():
        return "it is a link"
    binary = app / MACOS_APP_BINARY
    if not binary.is_file() or binary.is_symlink():
        return "it has no driver binary"
    try:
        verify_macos_bundle(app, run=run)
    except InstallError as exc:
        return exc.detail
    try:
        digest = sha256_file(binary)
    except OSError as exc:
        return f"its driver binary cannot be read ({exc.strerror or exc})"
    if not pinned.binary_sha256 or digest != pinned.binary_sha256:
        return f"its driver binary is not the pinned {DRIVER_VERSION}"
    if not check_running_version:
        return ""
    return check_version(version_binary or binary, home, run=run)


def _copy_bundle(src: Path, dst: Path) -> None:
    """Copy an app bundle preserving its links and metadata (``ditto`` when present)."""
    if Path("/usr/bin/ditto").is_file():
        done = subprocess.run(  # noqa: S603 - fixed system tool, paths we chose
            ["/usr/bin/ditto", str(src), str(dst)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_SIGNATURE_TIMEOUT_SECS,
            stdin=subprocess.DEVNULL,
        )
        if done.returncode != 0:
            raise OSError(done.stderr.strip() or f"ditto exited {done.returncode}")
        return
    shutil.copytree(src, dst, symlinks=True)


def place_macos_app(
    staged_app: Path,
    pinned: PinnedAsset,
    home: "str | os.PathLike[str]",
    *,
    previous: "dict[str, Any] | None" = None,
    run: "Runner | None" = None,
    copy: "Callable[[Path, Path], None] | None" = None,
) -> dict[str, Any]:
    """Put the verified app at ``/Applications/CuaDriver.app``; return its record.

    * An existing copy is used ONLY when it passes every check our own copy passes;
      otherwise the install fails and the existing copy is left untouched. A copy
      Kiro Crew did not place is never replaced, moved or deleted.
    * A non-writable ``/Applications`` is reported, never escalated.
    * Our copy is assembled under a private temporary name in the same directory,
      verified there, and renamed into place, so a half-copied bundle is never at
      the path upstream launches.
    """
    target = applications_app()
    # ``--version`` runs on the staged copy (in the sealed runtime tree, already
    # hashed and signature-checked), never on a binary in /Applications.
    staged_binary = staged_app / MACOS_APP_BINARY
    if target.exists() or target.is_symlink():
        why = check_app_copy(target, pinned, home, run=run, version_binary=staged_binary)
        if why:
            raise InstallError(
                STAGE_PLACING,
                ERR_FOREIGN_APP.format(path=target, version=DRIVER_VERSION, why=why),
            )
        owned = (
            isinstance(previous, dict)
            and previous.get("path") == str(target)
            and previous.get("owned") is True
        )
        return {"path": str(target), "owned": owned, "binary_sha256": pinned.binary_sha256}
    parent = target.parent
    if not parent.is_dir() or not os.access(parent, os.W_OK):
        raise InstallError(
            STAGE_PLACING, ERR_APPS_NOT_WRITABLE.format(dir=parent, version=DRIVER_VERSION)
        )
    tmp = parent / f".{MACOS_APP_NAME}.kirocrew-{os.getpid()}"
    try:
        (copy or _copy_bundle)(staged_app, tmp)
        why = check_app_copy(tmp, pinned, home, run=run, version_binary=staged_binary)
        if why:
            raise InstallError(STAGE_PLACING, f"the copied driver app failed its checks ({why})")
        if target.exists() or target.is_symlink():
            raise InstallError(
                STAGE_PLACING,
                ERR_FOREIGN_APP.format(
                    path=target, version=DRIVER_VERSION, why="it appeared during the install"
                ),
            )
        os.rename(tmp, target)
    except OSError as exc:
        raise InstallError(
            STAGE_PLACING, f"the driver app could not be placed in {parent} ({exc})"
        ) from exc
    finally:
        if tmp.exists() or tmp.is_symlink():
            _remove_tree(tmp)
    return {"path": str(target), "owned": True, "binary_sha256": pinned.binary_sha256}


def _remove_owned_app(record: dict[str, Any]) -> str:
    """Remove ``/Applications/CuaDriver.app`` only when the install marker says
    Kiro Crew placed the app at exactly that path (``owned`` true) AND the app's
    driver binary still has the SHA-256 recorded in that marker. Other files in
    the bundle are not compared: a copy of ours whose ``Info.plist`` was edited is
    still ours and is removed; a copy whose binary changed is left in place.
    Returns what happened, for the log."""
    app_record = record.get("applications_app")
    target = applications_app()
    if not isinstance(app_record, dict) or app_record.get("owned") is not True:
        return "no driver app of ours to remove"
    if app_record.get("path") != str(target) or not target.is_dir() or target.is_symlink():
        return "the driver app we placed is gone"
    try:
        digest = sha256_file(target / MACOS_APP_BINARY)
    except OSError:
        return "the driver app we placed cannot be read; left in place"
    if digest != app_record.get("binary_sha256"):
        return "the driver binary in the app we placed has changed since; left in place"
    _remove_tree(target)
    if Path(_LSREGISTER).is_file():
        try:
            subprocess.run(  # noqa: S603 - fixed system tool, a path we removed
                [_LSREGISTER, "-u", str(target)],
                capture_output=True,
                timeout=_SIGNATURE_TIMEOUT_SECS,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired):
            logger.debug("lsregister -u failed", exc_info=True)
    return "removed the driver app we placed"


# ── install ──


def _make_read_only(root: Path) -> None:
    if os.name == "nt":
        return
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            mode = path.stat().st_mode
            # Narrows only: clears every write bit, adds nothing.
            os.chmod(path, mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501  # fmt: skip
        os.chmod(dirpath, 0o555)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501  # fmt: skip


def _make_writable(root: Path) -> None:
    if os.name == "nt" or not root.exists():
        return
    for dirpath, _dirnames, _filenames in os.walk(root):
        try:
            # Owner-only: the tree lives in the private data home.
            os.chmod(dirpath, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501  # fmt: skip
        except OSError:
            pass


def _remove_tree(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    _make_writable(path)
    shutil.rmtree(path, ignore_errors=True)


def install(
    home: "str | os.PathLike[str]",
    *,
    asset: "PinnedAsset | None" = None,
    os_id: "str | None" = None,
    run: "Runner | None" = None,
    progress: "InstallProgress | None" = None,
    copy_app: "Callable[[Path, Path], None] | None" = None,
) -> InstallState:
    """Download, verify and install the pinned driver. Raises :class:`InstallError`."""
    prog = progress if progress is not None else PROGRESS
    if not prog.lock.acquire(blocking=False):
        raise InstallError(STAGE_PREPARING, "an install is already running")
    try:
        prog.running, prog.error, prog.error_stage = True, "", ""
        prog.stage, prog.bytes_done = STAGE_PREPARING, 0
        result = _install_locked(
            home,
            asset=asset,
            os_id=os_id,
            run=run,
            prog=prog,
            copy_app=copy_app,
        )
        prog.stage = STAGE_DONE
        return result
    except InstallError as exc:
        prog.error, prog.error_stage = exc.detail, exc.stage
        raise
    except Exception as exc:
        prog.error, prog.error_stage = f"install failed: {exc}", prog.stage
        raise InstallError(prog.stage or STAGE_PREPARING, f"install failed: {exc}") from exc
    finally:
        prog.running = False
        prog.finished_at = time.monotonic()
        prog.lock.release()


def _install_locked(
    home: "str | os.PathLike[str]",
    *,
    asset: "PinnedAsset | None",
    os_id: "str | None",
    run: "Runner | None",
    prog: InstallProgress,
    copy_app: "Callable[[Path, Path], None] | None" = None,
) -> InstallState:
    host_os = os_id or current_os()
    chosen = asset or asset_for(host_os)
    if chosen is None:
        raise InstallError(STAGE_PREPARING, "no pinned driver archive exists for this OS and CPU")
    root = driver_root(home)
    root.mkdir(parents=True, exist_ok=True)
    private_home(home)
    for leftover in root.glob(f"{STAGING_PREFIX}*"):
        _remove_tree(leftover)
    staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=root))
    try:
        archive = staging / chosen.name
        prog.stage, prog.bytes_total = STAGE_DOWNLOADING, chosen.size
        download_verified(chosen, archive, progress=prog)
        prog.stage = STAGE_EXTRACTING
        tree = staging / "tree"
        if chosen.is_zip:
            extract_zip(archive, tree)
        else:
            extract_tar(archive, tree)
        archive.unlink()
        binary = tree / PurePosixPath(chosen.binary)
        if not binary.is_file():
            raise InstallError(STAGE_EXTRACTING, f"the archive has no {chosen.binary}")
        if os.name != "nt":
            os.chmod(binary, binary.stat().st_mode | stat.S_IXUSR)
        if chosen.binary_sha256 and sha256_file(binary) != chosen.binary_sha256:
            raise InstallError(
                STAGE_VERIFYING, "the extracted driver does not match its pinned hash; refused"
            )
        prog.stage = STAGE_SIGNATURE
        if host_os == OS_MACOS:
            verify_macos_bundle(tree / PurePosixPath(chosen.bundle or "CuaDriver.app"), run=run)
        elif host_os == OS_WINDOWS:
            verify_windows_signature(binary, run=run)
        prog.stage = STAGE_VERSION
        reason = check_version(binary, home, run=run)
        if reason:
            raise InstallError(STAGE_VERSION, reason)
        app_record: "dict[str, Any] | None" = None
        if host_os == OS_MACOS:
            prog.stage = STAGE_PLACING
            previous = read_record(home).get("applications_app")
            app_record = place_macos_app(
                tree / PurePosixPath(chosen.bundle or MACOS_APP_NAME),
                chosen,
                home,
                previous=previous if isinstance(previous, dict) else None,
                run=run,
                copy=copy_app,
            )
        prog.stage = STAGE_FINISHING
        digest = sha256_file(binary)
        target = version_dir(home)
        aside = root / f"{STAGING_PREFIX}old-{os.getpid()}"
        if target.exists():
            _make_writable(target)
            os.replace(target, aside)
        os.replace(tree, target)
        _remove_tree(aside)
        _make_read_only(target)
        record = {
            "version": DRIVER_VERSION,
            "asset": chosen.name,
            "archive_sha256": chosen.sha256,
            "binary": chosen.binary,
            "binary_sha256": digest,
            "installed_at": int(time.time()),
        }
        if app_record is not None:
            record["applications_app"] = app_record
        tmp = record_path(home).with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, record_path(home))
        clear_blocked(home)
    finally:
        _remove_tree(staging)
    return install_state(home, asset=chosen)


def uninstall(home: "str | os.PathLike[str]") -> str:
    """Remove what Kiro Crew installed: the record, every installed version and, on
    macOS, ``/Applications/CuaDriver.app`` if (and only if) Kiro Crew placed it and it
    is unchanged. The private home stays. Returns a one-line note on the app."""
    note = _remove_owned_app(read_record(home))
    try:
        record_path(home).unlink()
    except FileNotFoundError:
        pass
    _remove_tree(driver_root(home))
    return note


__all__ = [
    "InstallError",
    "InstallProgress",
    "InstallState",
    "PROGRESS",
    "UnsafeArchiveMember",
    "applications_app",
    "DriverBlocked",
    "blocked_how",
    "blocked_message",
    "check_app_copy",
    "clear_blocked",
    "read_blocked",
    "record_blocked",
    "exec_ready_binary",
    "check_tar_members",
    "check_version",
    "current_os",
    "download_verified",
    "driver_root",
    "extract_tar",
    "extract_zip",
    "install",
    "install_state",
    "place_macos_app",
    "private_home",
    "read_record",
    "record_path",
    "runtime_root",
    "sha256_file",
    "uninstall",
    "verify_installed",
    "verify_macos_bundle",
    "verify_windows_signature",
    "version_dir",
]
