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
4. on Windows the executable's Authenticode signature must be valid and name
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
        "this computer is probably blocking it. Ask your IT team to allow the open-source driver"
    )
    if os_id == OS_WINDOWS:
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

    The ONE source of a driver executable for a direct exec (``mcp``,
    ``--version``): the copy in the sealed ``<data-home>/computer-use/driver/<version>``
    tree (read-only to the agent's sandbox), hashed against the PIN immediately
    before it is returned.

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
    executes even when the record beside it was rewritten to match; only then is
    ``--version`` run.
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
        label=f"the open-source driver {asset.name}",
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
        if host_os == OS_WINDOWS:
            verify_windows_signature(binary, run=run)
        prog.stage = STAGE_VERSION
        reason = check_version(binary, home, run=run)
        if reason:
            raise InstallError(STAGE_VERSION, reason)
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
        tmp = record_path(home).with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, record_path(home))
        clear_blocked(home)
    finally:
        _remove_tree(staging)
    return install_state(home, asset=chosen)


def uninstall(home: "str | os.PathLike[str]") -> str:
    """Remove what Kiro Crew installed: the record and every installed version. The
    private home stays. Returns a one-line note for the log."""
    try:
        record_path(home).unlink()
    except FileNotFoundError:
        pass
    _remove_tree(driver_root(home))
    return "removed the installed driver"


__all__ = [
    "InstallError",
    "InstallProgress",
    "InstallState",
    "PROGRESS",
    "UnsafeArchiveMember",
    "DriverBlocked",
    "blocked_how",
    "blocked_message",
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
    "private_home",
    "read_record",
    "record_path",
    "runtime_root",
    "sha256_file",
    "uninstall",
    "verify_installed",
    "verify_windows_signature",
    "version_dir",
]
