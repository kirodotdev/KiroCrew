"""Speech-to-text for whole audio files: one local recogniser, two adapted ones.

The default provider is ``local``: the whisper.cpp recogniser that
:mod:`kiro_crew.stt` holds loaded in this process. Keeping the model resident is
what makes it usable, because the cost that made local speech-to-text feel broken
was never the decode. A warm decode of 4.2 s of audio measures 30-48 ms (real-time
factor 0.007-0.011) and a 0.9 s push-to-talk utterance 27 ms, against seconds per
utterance for anything that loads a model per recording. It needs no external
binary and it works on every OS Kiro Crew supports.

Two further providers are *adapted* onto the same seam rather than being
first-class, and neither may add a step to the local path:

- ``apple``: Apple's on-device SpeechAnalyzer (macOS 26+), which downloads no
  model because the OS ships the assets. Owned by :mod:`kiro_crew.apple_speech`.
- ``transcribe``: AWS Transcribe Streaming, a paid service, gated on the recorded
  operator consent in :mod:`kiro_crew.aws_consent`.

Compressed input still needs ffmpeg: a Slack voice memo arrives as ogg/Opus and
the dashboard records webm. Desktop releases carry a pinned imageio-ffmpeg wheel
with that executable, so desktop users never install a system binary separately;
source installs use a system FFmpeg from fixed platform paths. A 16 kHz mono WAV
and live PCM skip the executable entirely.

Two guards here are deliberately provider-independent, because a per-branch copy
is a copy that will be missing from the next branch someone adds:
:func:`_is_sensitive_audio_path` refuses before any provider is dispatched, and
:func:`_redact_transcript` runs on every provider's output.
"""

from __future__ import annotations

import asyncio
import errno
import gzip
import hashlib
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import wave
import zlib
from typing import TYPE_CHECKING, Any, Iterator

from kiro_crew import aws_consent, platform_compat, stt

# Re-exported: the hallucination filter lives in kiro_crew.stt.hallucinations so
# the live session's final transcript and this batch path apply the SAME rules.
# The name stays importable from here because that is where callers have always
# found it, and one filter with two import paths cannot drift.
from kiro_crew.stt.hallucinations import filter_hallucinations  # noqa: F401

# Transcribe-path deps are the OPTIONAL 'voice' extra (amazon-transcribe + boto3).
# The module MUST stay importable when they're absent (default install, partial
# install, pip mid-install) so that `cli_doctor` — which imports this module —
# can surface the missing-deps diagnostic. Methods that actually use boto3 or
# the Credentials class are only invoked when stt.provider == "transcribe" and
# a profile is configured, so absence here is harmless for non-STT use. The
# local recogniser (default STT provider) needs neither.
try:
    import boto3
    from amazon_transcribe.auth import CredentialResolver, Credentials
except ImportError:  # pragma: no cover — covered by cli_doctor tests
    boto3 = None  # type: ignore[assignment,misc]
    CredentialResolver = object  # type: ignore[assignment,misc]
    Credentials = None  # type: ignore[assignment,misc]

if TYPE_CHECKING:  # annotations only; see the deferred-import note below
    import numpy as np

logger = logging.getLogger(__name__)


def _ffmpeg_candidate_dirs() -> list[str]:
    """Build the ordered directory list to probe for an ffmpeg install.

    Every entry is a PACKAGE MANAGER's directory. Whatever this resolves is exec'd by
    the gateway, so a generic user-writable directory must not appear at all: an
    earlier version carried ``~/ffmpeg`` and ``~/.local/bin`` for a user who had
    unzipped a static build by hand, and searching them LAST was not enough -- on a
    host with no packaged ffmpeg they were still trusted, and ``~/.local/bin`` is a
    generic dumping ground on nearly every PATH. Speculative support for a manual
    unzip is not worth a path that executes agent-written code as the gateway; a host
    without ffmpeg gets the "install ffmpeg or send 16 kHz mono WAV" log and a
    supported ``brew``/``apt`` install instead.

    Package prefixes are kept even where they are user-OWNED: Homebrew makes
    ``/opt/homebrew`` user-owned on Apple Silicon, and winget's user-scope target sits
    under ``%LOCALAPPDATA%``. The distinction is not the mode bits but whether the
    directory is a managed install root -- planting there means overwriting a package
    manager's own file, which is a different proposition from dropping a new name into
    a directory that exists to hold loose binaries. Dropping these would leave the
    feature unusable for most macOS and Windows users.

    Ordered most-trusted first regardless, and `_find_ffmpeg` consults
    `platform_compat.trusted_system_path` ahead of this list entirely.

    On Windows the two idiomatic install locations are the winget/Chocolatey
    machine-wide ``%ProgramFiles%\\ffmpeg\\bin`` and the winget/scoop user-scope
    ``%LOCALAPPDATA%\\Programs\\ffmpeg\\bin``. Expanded once at import time.
    """
    dirs = ["/opt/homebrew/bin", "/usr/local/bin"]
    if platform_compat.IS_WINDOWS:
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        local_appdata = os.environ.get(
            "LOCALAPPDATA",
            os.path.join(os.path.expanduser("~"), "AppData", "Local"),
        )
        dirs.extend(
            [
                os.path.join(program_files, "ffmpeg", "bin"),
                os.path.join(local_appdata, "Programs", "ffmpeg", "bin"),
            ]
        )
    return dirs


_FFMPEG_CANDIDATE_DIRS = _ffmpeg_candidate_dirs()


# imageio-ffmpeg==0.6.0 executables, taken from the four wheels that the desktop
# matrix installs. The filename selects the platform artifact; size makes a
# truncated payload fail cheaply; SHA-256 is the trust anchor. Desktop build
# staging is intentionally writable, so path placement or a removable `.git`
# marker cannot establish provenance. Only these exact upstream bytes may run.
_PACKAGED_FFMPEG_ARTIFACTS: dict[str, tuple[int, str]] = {
    "ffmpeg-macos-aarch64-v7.1.gz": (
        49_368_728,
        "6d175a4743ca50256e89a8cdd731100f9cee33bd79aeea46894d209410dc6617",
    ),
    "ffmpeg-linux-aarch64-v7.0.2": (
        51_134_160,
        "6bb182d0d75d23028db82e9e4f723ca69b853d055698486e6984ddb2c06fb8ce",
    ),
    "ffmpeg-linux-x86_64-v7.0.2": (
        79_826_272,
        "e7e7fb30477f717e6f55f9180a70386c62677ef8a4d4d1a5d948f4098aa3eb99",
    ),
    "ffmpeg-win-x86_64-v7.1.exe": (
        87_638_016,
        "2ce797a0f88d7f067180338fb227f7b1928ea727bd9a4d7a1d022f7c52af71a3",
    ),
}


def _trusted_site_package_roots() -> tuple[str, ...]:
    """Return interpreter-owned package roots, never the ambient import path."""
    roots: list[str] = []
    for prefix in (sys.prefix, sys.exec_prefix):
        if platform_compat.IS_WINDOWS:
            value = os.path.join(prefix, "Lib", "site-packages")
        else:
            version = f"python{sys.version_info.major}.{sys.version_info.minor}"
            value = os.path.join(prefix, "lib", version, "site-packages")
        root = os.path.realpath(value)
        if root not in roots:
            roots.append(root)
    return tuple(roots)


class _AuthenticatedFfmpeg:
    """One-shot executable whose verified bytes stay bound through spawn."""

    __slots__ = ("cleanup_path", "descriptor", "execution_path", "source_path")

    def __init__(
        self,
        source_path: str,
        descriptor: int,
        execution_path: str,
        *,
        cleanup_path: str | None = None,
    ) -> None:
        self.source_path = source_path
        self.descriptor = descriptor
        self.execution_path = execution_path
        self.cleanup_path = cleanup_path

    def close(self) -> None:
        descriptor, self.descriptor = self.descriptor, -1
        cleanup_path, self.cleanup_path = self.cleanup_path, None
        if descriptor < 0 and cleanup_path is None:
            return
        try:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        finally:
            if cleanup_path is not None:
                _remove_named_snapshot(cleanup_path)

    def __str__(self) -> str:
        return self.source_path

    def __del__(self) -> None:
        self.close()


def _open_windows_read_locked(candidate: str) -> int:
    """Open *candidate* while denying write/delete sharing on Windows."""
    import msvcrt

    win_dll = getattr(platform_compat.ctypes, "WinDLL")
    kernel32 = win_dll("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        platform_compat.wintypes.LPCWSTR,
        platform_compat.wintypes.DWORD,
        platform_compat.wintypes.DWORD,
        platform_compat.wintypes.LPVOID,
        platform_compat.wintypes.DWORD,
        platform_compat.wintypes.DWORD,
        platform_compat.wintypes.HANDLE,
    )
    create_file.restype = platform_compat.wintypes.HANDLE
    handle = create_file(
        candidate,
        0x80000000,  # GENERIC_READ
        0x00000001,  # FILE_SHARE_READ: deny writes, replacement and deletion
        None,
        3,  # OPEN_EXISTING
        0x00000080,  # FILE_ATTRIBUTE_NORMAL
        None,
    )
    invalid_handle = platform_compat.wintypes.HANDLE(-1).value
    if handle == invalid_handle:
        error = getattr(platform_compat.ctypes, "get_last_error")()
        raise OSError(error, "CreateFileW failed", candidate)
    try:
        open_osfhandle = getattr(msvcrt, "open_osfhandle")
        return open_osfhandle(int(handle), os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except BaseException:
        kernel32.CloseHandle(handle)
        raise


def _write_all(descriptor: int, chunk: bytes) -> None:
    view = memoryview(chunk)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write creating authenticated ffmpeg snapshot")
        view = view[written:]


def _ffmpeg_payload_chunks(descriptor: int, *, compressed: bool) -> Iterator[bytes]:
    """Yield original executable bytes from a raw or gzip package resource."""
    if not compressed:
        while True:
            chunk = os.read(descriptor, 1 << 20)
            if not chunk:
                return
            yield chunk

    # Apple Silicon stores the already ad-hoc-signed upstream executable as
    # inert gzip data so app signing cannot replace its Mach-O signature and
    # invalidate the pinned digest. Duplicate the descriptor because GzipFile
    # owns its file object.
    with os.fdopen(os.dup(descriptor), "rb") as encoded:
        with gzip.GzipFile(fileobj=encoded, mode="rb") as payload:
            while True:
                chunk = payload.read(1 << 20)
                if not chunk:
                    return
                yield chunk


def _remove_named_snapshot(path: str) -> None:
    """Best-effort cleanup for a private macOS executable snapshot."""
    parent = os.path.dirname(path)
    try:
        os.chmod(parent, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- 0o700 is owner-only and deliberately restores the private executable-snapshot directory before cleanup; 0o644 would expose names and make the directory untraversable.  # noqa: E501  # fmt: skip
    except OSError:
        pass
    try:
        os.unlink(path)
    except OSError:
        pass
    try:
        os.rmdir(parent)
    except OSError:
        pass


_FFMPEG_SNAPSHOT_PREFIX = ".kirocrew-ffmpeg-"
_FFMPEG_SNAPSHOT_NAME_RE = re.compile(r"^\.kirocrew-ffmpeg-(\d+)-[A-Za-z0-9_-]+$")
_ffmpeg_snapshot_roots_cleaned: set[str] = set()
_ffmpeg_snapshot_cleanup_lock = threading.Lock()


def _cleanup_stale_ffmpeg_snapshots(root: str) -> None:
    """Remove dead-process decoder snapshots without following links."""
    owner = getattr(os, "getuid", lambda: os.lstat(root).st_uid)()
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        match = _FFMPEG_SNAPSHOT_NAME_RE.fullmatch(name)
        if match is None:
            continue
        pid = int(match.group(1))
        if pid == os.getpid() or platform_compat.pid_liveness(pid) != platform_compat.PID_DEAD:
            continue
        parent = os.path.join(root, name)
        payload = os.path.join(parent, "ffmpeg")
        try:
            parent_info = os.lstat(parent)
            if (
                not stat.S_ISDIR(parent_info.st_mode)
                or stat.S_ISLNK(parent_info.st_mode)
                or parent_info.st_uid != owner
            ):
                continue
            entries = os.listdir(parent)
            if entries not in ([], ["ffmpeg"]):
                continue
            if entries:
                payload_info = os.lstat(payload)
                if (
                    not stat.S_ISREG(payload_info.st_mode)
                    or stat.S_ISLNK(payload_info.st_mode)
                    or payload_info.st_uid != owner
                ):
                    continue
            os.chmod(parent, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- 0o700 is owner-only and the tightest traversable mode for this gateway-private snapshot directory; the rule's 0o644 suggestion is both broader and unusable for a directory.  # noqa: E501  # fmt: skip
            if entries:
                os.unlink(payload)
            os.rmdir(parent)
        except OSError:
            logger.debug("could not prune stale voice decoder snapshot %s", parent, exc_info=True)


def _ffmpeg_snapshot_root() -> str:
    """Return the gateway-only runtime root used for macOS decoder images."""
    from kiro_crew.sandbox import prime_voice_runtime_sandbox_paths

    root = prime_voice_runtime_sandbox_paths()
    root_stat = os.lstat(root)
    if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
        raise OSError("voice runtime root is not a real directory")
    os.chmod(root, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- 0o700 intentionally keeps verified decoder images gateway-only while retaining directory traversal; Semgrep's suggested 0o644 would grant world-read and remove traversal.  # noqa: E501  # fmt: skip
    if root not in _ffmpeg_snapshot_roots_cleaned:
        with _ffmpeg_snapshot_cleanup_lock:
            if root not in _ffmpeg_snapshot_roots_cleaned:
                _cleanup_stale_ffmpeg_snapshots(root)
                _ffmpeg_snapshot_roots_cleaned.add(root)
    return root


def _new_executable_snapshot() -> tuple[int, int, bool, str | None]:
    """Return writer, reader, seal flag and any required execution pathname."""
    if platform_compat.IS_LINUX and hasattr(os, "memfd_create"):
        flags = getattr(os, "MFD_CLOEXEC", 0x0001) | getattr(os, "MFD_ALLOW_SEALING", 0x0002)
        # Linux 6.3 can default memfd_create() to non-executable through
        # vm.memfd_noexec. Request the kernel's explicit executable mode so the
        # authenticated snapshot still runs in hardened namespaces; retry only
        # on EINVAL for older kernels that predate MFD_EXEC.
        try:
            descriptor = os.memfd_create("kirocrew-ffmpeg", flags | getattr(os, "MFD_EXEC", 0x0010))
        except OSError as exc:
            if exc.errno != errno.EINVAL:
                raise
            descriptor = os.memfd_create("kirocrew-ffmpeg", flags)
        return descriptor, -1, True, None

    # macOS has no memfd or fexecve, and its Mach-O loader does not reliably
    # execute an unlinked file through /dev/fd. Stage the authenticated bytes in
    # a fresh 0700 directory beneath the gateway-only runtime root instead. All
    # agent sandbox modes deny read, write and hardlink access to that fixed
    # root, closing the same-UID watcher race that a generic $TMPDIR would leave
    # open while bytes are copied. The private directory becomes non-writable
    # before verification and close() removes it only after the child has opened
    # the image.
    parent = tempfile.mkdtemp(
        prefix=f"{_FFMPEG_SNAPSHOT_PREFIX}{os.getpid()}-", dir=_ffmpeg_snapshot_root()
    )
    path = os.path.join(parent, "ffmpeg")
    writer = -1
    reader = -1
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        writer = os.open(path, flags, 0o600)
        reader = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        return writer, reader, False, path
    except BaseException:
        if writer >= 0:
            os.close(writer)
        if reader >= 0:
            os.close(reader)
        _remove_named_snapshot(path)
        raise


def _seal_linux_memfd(descriptor: int) -> int:
    """Seal *descriptor* and reopen the immutable memfd read-only."""
    import fcntl

    # Python exposes these only when its build headers define them. The Linux
    # ABI values have been stable since memfd sealing was introduced, so a PBS
    # interpreter built against older headers can still use a newer kernel.
    add_seals = getattr(fcntl, "F_ADD_SEALS", 1033)
    seals = (
        getattr(fcntl, "F_SEAL_SEAL", 0x0001)
        | getattr(fcntl, "F_SEAL_SHRINK", 0x0002)
        | getattr(fcntl, "F_SEAL_GROW", 0x0004)
        | getattr(fcntl, "F_SEAL_WRITE", 0x0008)
    )
    fcntl.fcntl(descriptor, add_seals, seals)
    return os.open(f"/proc/self/fd/{descriptor}", os.O_RDONLY)


def _authenticated_ffmpeg(
    candidate: str,
    expected_size: int,
    expected_sha256: str,
    *,
    compressed: bool = False,
) -> _AuthenticatedFfmpeg | None:
    """Copy/hash exact bytes and keep an immutable execution identity open.

    Linux executes a sealed memfd by inherited descriptor. macOS executes an
    gateway-owned named snapshot because its Mach-O loader cannot reliably
    execute an unlinked ``/dev/fd`` image; agent sandboxes cannot reach its root,
    and the file and its parent stay non-writable from verification through
    spawn. Windows instead holds a ``CreateFileW`` handle
    that denies both write and delete sharing until ``CreateProcess`` has opened
    the image. In every case the bytes hashed are the bytes staged for execution.
    """
    source = -1
    snapshot_writer = -1
    snapshot = -1
    snapshot_path: str | None = None
    try:
        if platform_compat.IS_WINDOWS:
            source = _open_windows_read_locked(candidate)
        else:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            source = os.open(candidate, flags)
        opened = os.fstat(source)
        if not stat.S_ISREG(opened.st_mode):
            return None
        if compressed:
            if opened.st_size <= 0:
                return None
        elif opened.st_size != expected_size:
            return None

        seal_snapshot = False
        if not platform_compat.IS_WINDOWS:
            snapshot_writer, snapshot, seal_snapshot, snapshot_path = _new_executable_snapshot()

        digest = hashlib.sha256()
        total = 0
        for chunk in _ffmpeg_payload_chunks(source, compressed=compressed):
            total += len(chunk)
            if total > expected_size:
                return None
            digest.update(chunk)
            if snapshot_writer >= 0:
                _write_all(snapshot_writer, chunk)
        if total != expected_size or digest.hexdigest() != expected_sha256:
            return None

        if platform_compat.IS_WINDOWS:
            result = _AuthenticatedFfmpeg(candidate, source, candidate)
            source = -1  # ownership transferred to result
            return result

        os.fchmod(snapshot_writer, 0o500)
        if seal_snapshot:
            snapshot = _seal_linux_memfd(snapshot_writer)
        os.close(snapshot_writer)
        snapshot_writer = -1
        if snapshot_path is not None:
            os.chmod(os.path.dirname(snapshot_path), 0o500)

        # Authenticate the descriptor that will actually be inherited, after
        # the last writer owned by this process has closed.
        snapshot_digest = hashlib.sha256()
        snapshot_total = 0
        while True:
            chunk = os.read(snapshot, 1 << 20)
            if not chunk:
                break
            snapshot_total += len(chunk)
            snapshot_digest.update(chunk)
        if snapshot_total != expected_size or snapshot_digest.hexdigest() != expected_sha256:
            return None
        os.lseek(snapshot, 0, os.SEEK_SET)
        if snapshot_path is None:
            execution_path = f"/proc/self/fd/{snapshot}"
        else:
            execution_path = snapshot_path
        result = _AuthenticatedFfmpeg(
            candidate,
            snapshot,
            execution_path,
            cleanup_path=snapshot_path,
        )
        snapshot = -1  # ownership transferred to result
        snapshot_path = None  # ownership transferred to result
        return result
    except (EOFError, OSError, zlib.error):
        return None
    finally:
        if source >= 0:
            os.close(source)
        if snapshot_writer >= 0:
            os.close(snapshot_writer)
        if snapshot >= 0:
            os.close(snapshot)
        if snapshot_path is not None:
            _remove_named_snapshot(snapshot_path)


def _open_packaged_ffmpeg_resource() -> _AuthenticatedFfmpeg | None:
    """Open the one authenticated imageio-ffmpeg executable in this runtime."""
    candidates: list[_AuthenticatedFfmpeg] = []
    for root in _trusted_site_package_roots():
        root = os.path.realpath(root)
        package_root = os.path.realpath(os.path.join(root, "imageio_ffmpeg"))
        binaries_root = os.path.realpath(os.path.join(package_root, "binaries"))
        try:
            if os.path.commonpath((root, package_root)) != root:
                continue
            if os.path.commonpath((package_root, binaries_root)) != package_root:
                continue
            filenames = os.listdir(binaries_root)
        except (OSError, ValueError):
            continue
        for filename in filenames:
            artifact = _PACKAGED_FFMPEG_ARTIFACTS.get(filename)
            if artifact is None:
                continue
            unresolved = os.path.join(binaries_root, filename)
            candidate = os.path.realpath(unresolved)
            if (
                os.path.dirname(candidate) != binaries_root
                or candidate != os.path.abspath(unresolved)
                or not os.path.isfile(candidate)
            ):
                continue
            compressed = filename.endswith(".gz")
            if (
                not compressed
                and not platform_compat.IS_WINDOWS
                and not os.access(candidate, os.X_OK)
            ):
                continue
            authenticated = _authenticated_ffmpeg(candidate, *artifact, compressed=compressed)
            if authenticated is not None:
                candidates.append(authenticated)
    if len(candidates) == 1:
        return candidates[0]
    for opened_candidate in candidates:
        opened_candidate.close()
    return None


def _packaged_ffmpeg_resource() -> str | None:
    """Resolve the ffmpeg executable inside this interpreter's package tree.

    The pinned imageio-ffmpeg wheel stores one platform-native executable beside
    its Python package. Resolve that exact package resource instead of calling
    ``get_ffmpeg_exe()``: the public helper deliberately falls back to an ambient
    PATH, which this gateway must never execute, and honours an environment
    override that would let outside state replace release payload.
    """
    authenticated = _open_packaged_ffmpeg_resource()
    if authenticated is None:
        return None
    try:
        return authenticated.source_path
    finally:
        authenticated.close()


def _packaged_ffmpeg_version_probe() -> bool:
    """Run ``-version`` from authenticated bytes for the desktop build gate."""
    authenticated = _open_packaged_ffmpeg_resource()
    if authenticated is None:
        return False
    try:
        kwargs: dict[str, Any] = {}
        if not platform_compat.IS_WINDOWS:
            kwargs["pass_fds"] = (authenticated.descriptor,)
        result = subprocess.run(
            [authenticated.execution_path, "-version"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **kwargs,
        )
        return result.returncode == 0
    except OSError:
        return False
    finally:
        authenticated.close()


def _bundled_ffmpeg() -> str | None:
    """Return the authenticated decoder carried by a bundled interpreter."""
    if not platform_compat.is_bundled_interpreter():
        return None
    return _packaged_ffmpeg_resource()


def _open_ffmpeg_for_execution() -> str | _AuthenticatedFfmpeg | None:
    """Resolve FFmpeg, retaining authenticated bundled bytes until spawn."""
    if platform_compat.is_bundled_interpreter():
        # A desktop release is self-contained. If its authenticated decoder is
        # missing or damaged, fail closed instead of executing a fixed-path
        # binary that was never authenticated as part of this installation.
        return _open_packaged_ffmpeg_resource()
    return _find_system_ffmpeg()


def _close_abandoned_ffmpeg_resolution(
    resolution: asyncio.Future[str | _AuthenticatedFfmpeg | None],
) -> None:
    """Close a cancelled resolver's eventual handle outside the event loop."""
    try:
        executable = resolution.result()
    except BaseException:
        return
    if isinstance(executable, _AuthenticatedFfmpeg):
        # The executor retains the bound method (and therefore the descriptor)
        # until close finishes; dropping the Future cannot invoke __del__ first.
        asyncio.get_running_loop().run_in_executor(None, executable.close)


async def _resolve_ffmpeg_for_execution() -> str | _AuthenticatedFfmpeg | None:
    """Resolve off-loop and retain cleanup ownership if this task is cancelled."""
    resolution = asyncio.ensure_future(asyncio.to_thread(_open_ffmpeg_for_execution))
    try:
        return await asyncio.shield(resolution)
    except BaseException:
        # ``to_thread`` cannot be stopped once running. If it later returns an
        # authenticated descriptor, transfer that descriptor directly to an
        # executor worker instead of letting Future destruction run __del__ on
        # the event-loop thread.
        resolution.add_done_callback(_close_abandoned_ffmpeg_resolution)
        raise


async def _close_ffmpeg_for_execution(
    executable: str | _AuthenticatedFfmpeg,
    *,
    preserve_active_exception: bool = False,
) -> None:
    """Close an authenticated handle off-loop, optionally preserving a caller error."""
    if not isinstance(executable, _AuthenticatedFfmpeg):
        return
    close_task = asyncio.ensure_future(asyncio.to_thread(executable.close))
    try:
        await asyncio.shield(close_task)
    except BaseException:
        # The worker retains ownership and will still finish. On a pre-spawn
        # failure, cleanup must not replace the exception already in flight.
        if not preserve_active_exception:
            raise


async def _create_ffmpeg_subprocess(
    executable: str | _AuthenticatedFfmpeg, *args: str, **kwargs: Any
) -> asyncio.subprocess.Process:
    """Spawn FFmpeg while its authenticated image remains immutable/open."""
    if isinstance(executable, str):
        return await asyncio.create_subprocess_exec(executable, *args, **kwargs)
    try:
        if not platform_compat.IS_WINDOWS:
            kwargs["pass_fds"] = (executable.descriptor,)
        return await asyncio.create_subprocess_exec(executable.execution_path, *args, **kwargs)
    finally:
        await _close_ffmpeg_for_execution(executable)


def ensure_ffmpeg_in_path() -> None:
    """Add known ffmpeg directories to PATH if they contain an ffmpeg binary.

    Probes each candidate dir with ``shutil.which("ffmpeg", path=d)`` — that
    honours ``PATHEXT`` on Windows (so ``ffmpeg.exe`` resolves) while still
    matching a plain ``ffmpeg`` on POSIX.
    """
    path_parts = os.environ.get("PATH", "").split(os.pathsep)
    for d in reversed(_FFMPEG_CANDIDATE_DIRS):
        if d in path_parts:
            continue
        if shutil.which("ffmpeg", path=d):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            path_parts.insert(0, d)


def _find_system_ffmpeg() -> str | None:
    """Return a system FFmpeg from fixed directories rather than ambient PATH."""
    trusted_path = platform_compat.trusted_system_path()
    if trusted_path:
        found = shutil.which("ffmpeg", path=trusted_path)
        if found:
            return found
    for directory in _FFMPEG_CANDIDATE_DIRS:
        found = shutil.which("ffmpeg", path=directory)
        if found:
            return found
    return None


def _find_ffmpeg() -> str | None:
    """Report the authenticated bundle path or a fixed-path system FFmpeg.

    Deliberately NOT ``shutil.which("ffmpeg")``. A gateway's PATH can legitimately lead
    with agent-writable directories (a worktree venv's ``bin``, ``~/.local/bin``), which
    is exactly the threat `platform_compat.trusted_system_bin` documents: the result
    here describes what the execution resolver would use. Execution itself calls
    :func:`_open_ffmpeg_for_execution`, which keeps bundled bytes bound through spawn.

    The trusted system directories are tried first, then the fixed candidate list,
    which is itself ordered most-trusted first. ffmpeg is not an OS tool -- on macOS it
    lives under a Homebrew prefix and on Windows under a package-manager directory --
    so the system set alone would find it almost nowhere.

    Reached through `trusted_system_path` rather than `trusted_system_bin` because that
    helper warns once per name when a tool is on PATH but outside the system set, and
    that message states the caller "degrades instead of running a PATH-chosen binary".
    Here it does not: the candidate list below finds the packaged ffmpeg and uses it, so
    borrowing the helper would log a degradation that never happens on every macOS host
    with Homebrew. ``None`` from it means Windows, where the search must NOT fall back
    to `which`'s default (the ambient PATH) and the candidate list already carries the
    package-manager directories.
    """
    if platform_compat.is_bundled_interpreter():
        return _bundled_ffmpeg()
    return _find_system_ffmpeg()


# Homebrew installs its ``brew`` shim at a fixed prefix per platform, and none of
# those prefixes are on the PATH a GUI-launched gateway inherits: the desktop app
# (Dock / Finder / launchd) starts with ``/usr/bin:/bin:/usr/sbin:/sbin``, so
# ``shutil.which("brew")`` reports Homebrew MISSING on a machine that has it.
# Probing the prefixes directly is what keeps the STT prereq list and the install
# script from telling a Homebrew user to install Homebrew.
_BREW_CANDIDATE_PATHS = [
    "/opt/homebrew/bin/brew",  # Apple Silicon macOS
    "/usr/local/bin/brew",  # Intel macOS
    "/home/linuxbrew/.linuxbrew/bin/brew",  # Linuxbrew, system install
    os.path.expanduser("~/.linuxbrew/bin/brew"),  # Linuxbrew, per-user install
]


def find_brew() -> str | None:
    """Return the ``brew`` binary path, or None when Homebrew is not installed.

    Falls back to the well-known install prefixes when ``brew`` is not on PATH
    (see ``_BREW_CANDIDATE_PATHS``) so a GUI-launched gateway agrees with what
    the user sees in their terminal.
    """
    found = shutil.which("brew")
    if found:
        return found
    for p in _BREW_CANDIDATE_PATHS:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------

# The adapted providers answer in :class:`kiro_crew.stt.Availability`, the same
# shape and the same machine-readable vocabulary the local recogniser uses, so a
# caller renders one set of reasons whichever provider is configured. The codes
# below are the ones only this module can report; the rest come from
# :mod:`kiro_crew.stt.engine`. They travel to the browser in JSON, so renaming
# one silently drops the UI back to a generic string.

#: Speech-to-text is switched off in configuration. Not a fault: it is the
#: distinction between "you turned this off" and "this cannot run here".
CODE_DISABLED = "stt_disabled"

#: This host cannot run Apple's on-device speech at all (not macOS, or too old a
#: macOS for the SpeechAnalyzer API). No install fixes it.
CODE_APPLE_UNSUPPORTED = "stt_apple_unsupported"

#: Apple's on-device speech could run here once the Swift toolchain is present.
#: Separated from :data:`CODE_APPLE_UNSUPPORTED` because this one has a one-line
#: fix and that one does not.
CODE_APPLE_NEEDS_TOOLCHAIN = "stt_apple_needs_toolchain"

#: What to do about a missing AWS Transcribe client. Named once because doctor,
#: the settings panel and the failure log all report it, and a divergent copy
#: sends a user to install the wrong thing.
_VOICE_EXTRA_HINT = "AWS Transcribe needs the voice extra: pip install 'kirocrew[voice]'"


def _aws_availability() -> stt.Availability:
    """Whether the AWS Transcribe client libraries are importable.

    Consent is deliberately NOT consulted here. ``aws_consent.refuse_and_log``
    records an audit entry, and this predicate is polled (once per inbound Slack
    message, on every settings read), so asking it here would fill the audit log
    with refusals nobody requested. The paid-service gate stays at the point
    where audio would actually leave the host.
    """
    if boto3 is None:
        return stt.Availability(False, stt.CODE_EXTRA_MISSING, _VOICE_EXTRA_HINT)
    try:
        import amazon_transcribe  # noqa: F401
    except ImportError:
        return stt.Availability(False, stt.CODE_EXTRA_MISSING, _VOICE_EXTRA_HINT)
    return stt.Availability(True)


def _apple_availability() -> stt.Availability:
    """Whether Apple's on-device speech can run, translated into one shape."""
    from kiro_crew import apple_speech

    # Stats only, never a build: this runs on the event loop (the settings read,
    # the transcribe endpoint, the Slack voice path), and compiling the Swift
    # helper there would freeze the gateway for as long as swiftc takes. The
    # build happens inside the offloaded transcribe path.
    result = apple_speech.availability()
    if result.ok:
        return stt.Availability(True)
    code = CODE_APPLE_NEEDS_TOOLCHAIN if result.needs_toolchain else CODE_APPLE_UNSUPPORTED
    return stt.Availability(False, code, result.reason)


def availability_detail(stt_config=None) -> stt.Availability:  # type: ignore[no-untyped-def]
    """Whether speech-to-text can run, and when it cannot, precisely why.

    One shape for all three providers so a caller renders one set of reasons.
    Distinguishing them is the point: "install an extra", "your platform has no
    prebuilt wheel" and "this needs a newer macOS" lead to completely different
    actions, and collapsing them into a boolean is what makes a feature feel
    broken rather than unconfigured.

    Whether the configured MODEL is on disk is deliberately not part of the
    answer. A missing model resolves itself on first use, so reporting it as
    unavailable would hide a working install behind a condition that fixes itself.
    """
    if stt_config is None:
        from kiro_crew.config.loader import KiroCrewConfig

        stt_config = KiroCrewConfig.load().stt
    if not stt_config.enabled:
        return stt.Availability(False, CODE_DISABLED, "speech-to-text is turned off")
    provider = stt_config.provider
    if provider == "transcribe":
        return _aws_availability()
    if provider == "apple":
        return _apple_availability()
    # ``local`` is the floor every other value degrades to; see
    # :func:`transcribe_audio` for why that is answered here rather than raised.
    # The first call links the recogniser's native extension, then ``sys.modules``
    # makes it a dictionary lookup. A FAILED import is not cached, so a gateway
    # that booted without the extra picks up a later install with no restart.
    return stt.availability()


def is_available(stt_config=None) -> bool:  # type: ignore[no-untyped-def]
    """Whether speech-to-text is enabled and the configured provider can run.

    The boolean view of :func:`availability_detail`, derived from it rather than
    implemented beside it: two implementations of one question drift, and the pair
    that disagrees hands a caller a 503 for a provider the settings panel is
    showing as ready.
    """
    return availability_detail(stt_config).ok


def _load_stt_config() -> Any:
    """Load STT configuration without importing or reading config on the loop."""
    from kiro_crew.config.loader import KiroCrewConfig

    return KiroCrewConfig.load().stt


def _is_sensitive_audio_path(audio_path: str) -> bool:
    """Run the filesystem-resolving sensitive-path guard off the event loop."""
    from kiro_crew.security import is_sensitive_path

    return is_sensitive_path(audio_path)


def _redact_transcript(transcript: str) -> str:
    """Apply transcript redaction without consuming event-loop time."""
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    transcript, _ = redact_exfiltration_urls(transcript)
    transcript, _ = redact_credentials(transcript)
    return transcript


async def transcribe_audio(audio_path: str, stt_config=None) -> str | None:  # type: ignore[no-untyped-def]
    """Transcribe an audio file. Returns the text, or None.

    None on every failure, and never an exception: eight channel adapters call
    this and turn None into a visible "transcription failed" note for the user,
    whereas an exception becomes a log line nobody reads and a turn that never
    starts.
    """
    if stt_config is None:
        stt_config = await asyncio.to_thread(_load_stt_config)

    if not stt_config.enabled:
        logger.debug("STT disabled in config")
        return None

    # Before dispatch, for every provider. Refusing here rather than inside each
    # branch is what makes it impossible to add a provider that skips the check.
    if await asyncio.to_thread(_is_sensitive_audio_path, audio_path):
        logger.error("Refusing to read sensitive path: %s", audio_path)
        return None

    provider = stt_config.provider
    if provider == "transcribe":
        result = await _transcribe_aws(audio_path, stt_config)
    elif provider == "apple":
        result = await _transcribe_apple(audio_path, stt_config)
    else:
        # ``local`` is the floor. The config loader already degrades a retired or
        # unrecognised provider onto it with a logged reason, and landing here
        # for anything else transcribes rather than raising, so a hand-edited
        # config costs the user a different engine and not a dead voice path.
        result = await _transcribe_local(audio_path, stt_config)

    if result:
        # Unconditional, on every provider's output, in one off-loop hop.
        result = await asyncio.to_thread(_redact_transcript, result)
    return result


class _ProfileCredentialResolver(CredentialResolver):
    """Async credential resolver that delegates to a boto3 Session profile."""

    def __init__(self, profile: str) -> None:
        if boto3 is None:  # pragma: no cover (the optional 'voice' extra is absent)
            raise RuntimeError(
                "AWS Transcribe support is not available: install the optional "
                "dependencies (pip install 'kirocrew[voice]')."
            )
        self._session = boto3.Session(profile_name=profile)

    async def get_credentials(self) -> Credentials | None:
        loop = asyncio.get_running_loop()
        creds = await loop.run_in_executor(None, lambda: self._session.get_credentials())
        if creds is None:
            # Profile name in error is safe — only logged server-side via
            # logger.exception in _transcribe_aws, never exposed in HTTP responses.
            raise RuntimeError(
                f"No AWS credentials found for profile '{self._session.profile_name}'"
            )
        frozen = await loop.run_in_executor(None, creds.get_frozen_credentials)
        return Credentials(frozen.access_key, frozen.secret_key, frozen.token)


#: Sample rate declared to AWS Transcribe for the ogg-opus stream. Chrome's
#: MediaRecorder with the opus codec defaults to 48 kHz; a different rate here
#: makes Transcribe reject or garble the stream. Unrelated to the recogniser's
#: 16 kHz (``stt.SAMPLE_RATE_HZ``): this one describes bytes already encoded by a
#: browser, that one describes samples we hand to a decoder.
_TRANSCRIBE_SAMPLE_RATE_HZ = 48000

_TRANSCRIBE_MAX_BYTES = 25 * 1024 * 1024  # 25 MB Transcribe API limit


def _load_aws_transcribe_components() -> tuple[Any, Any]:
    """Import optional AWS Transcribe components outside the event loop."""
    from amazon_transcribe.client import TranscribeStreamingClient
    from amazon_transcribe.handlers import TranscriptResultStreamHandler
    from amazon_transcribe.model import TranscriptEvent

    class TranscriptCollector(TranscriptResultStreamHandler):
        def __init__(self, output_stream: Any, transcript_parts: list[str]) -> None:
            super().__init__(output_stream)
            self._transcript_parts = transcript_parts

        async def handle_transcript_event(self, transcript_event: TranscriptEvent) -> None:
            for result in transcript_event.transcript.results:
                if not result.is_partial and result.alternatives:
                    self._transcript_parts.append(result.alternatives[0].transcript)

    return TranscribeStreamingClient, TranscriptCollector


def _make_temp_ogg() -> str:
    """Create and close a temporary OGG file without leaking its descriptor."""
    fd, path = tempfile.mkstemp(suffix=".ogg")
    os.close(fd)
    return path


def _unlink_if_exists(path: str) -> None:
    """Remove *path*, tolerating another cleanup path winning the race."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _read_audio_bytes(audio_path: str) -> bytes:
    """Read at most one byte beyond AWS Transcribe's upload limit."""
    with open(audio_path, "rb") as audio_file:
        return audio_file.read(_TRANSCRIBE_MAX_BYTES + 1)


async def _transcribe_aws(audio_path: str, stt_config) -> str | None:  # type: ignore[no-untyped-def]
    """Transcribe using AWS Transcribe Streaming API (ogg-opus)."""
    ext = os.path.splitext(audio_path)[1].lower()
    if ext not in (".ogg", ".webm"):
        logger.error("Unsupported format '%s' for Transcribe (expected .ogg or .webm)", ext)
        return None

    # Transcribe is a PAID AWS service, so no audio leaves the host without a
    # recorded operator consent for this exact profile+region. Checked before
    # the optional-dependency probe and before any remux work, so a refusal
    # costs nothing and no temp file is created. Returning None is this
    # function's established failure contract.
    if not await aws_consent.refuse_and_log(
        aws_consent.SERVICE_TRANSCRIBE,
        profile=stt_config.transcribe_profile,
        region=stt_config.transcribe_region,
    ):
        return None

    # amazon-transcribe + boto3 are the optional 'voice' extra. Absent on a
    # vanilla install → report not available rather than raising ImportError.
    if boto3 is None:
        logger.error("AWS Transcribe not available: install 'kirocrew[voice]'")
        return None
    try:
        TranscribeStreamingClient, TranscriptCollector = await asyncio.to_thread(
            _load_aws_transcribe_components
        )
    except ImportError:
        logger.error("AWS Transcribe not available: install 'kirocrew[voice]'")
        return None

    region = stt_config.transcribe_region
    tmp_ogg = None
    actual_path = audio_path
    if ext in (".webm",):
        ffmpeg_bin = await _resolve_ffmpeg_for_execution()
        if not ffmpeg_bin:
            logger.error("ffmpeg required to remux webm to ogg for Transcribe")
            return None
        try:
            tmp_ogg = await asyncio.to_thread(_make_temp_ogg)
        except BaseException:
            await _close_ffmpeg_for_execution(ffmpeg_bin, preserve_active_exception=True)
            raise
        proc = None
        try:
            proc = await _create_ffmpeg_subprocess(
                ffmpeg_bin,
                "-y",
                "-i",
                audio_path,
                "-c:a",
                "copy",
                tmp_ogg,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                await asyncio.wait_for(proc.communicate(), timeout=10)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                raise
            if proc.returncode != 0:
                raise RuntimeError(f"ffmpeg exited with {proc.returncode}")
        except Exception:
            logger.exception("ffmpeg remux failed for %s", audio_path)
            if tmp_ogg:
                await asyncio.to_thread(_unlink_if_exists, tmp_ogg)
            return None
        except BaseException:
            # ``CancelledError`` derives from ``BaseException``, so the
            # ``Exception`` guard above never sees it: a cancellation landing
            # mid-``communicate`` used to leave the ffmpeg child running and the
            # owned temp on disk (#5780). Mirror ``_to_native_audio``'s cleanup
            # (#5777): stop AND reap the child BEFORE the unlink — Windows keeps
            # the output file locked until the child fully exits, and on POSIX a
            # live child can race the removal. Every step is best-effort, and
            # the unlink stays synchronous (one-file unlink, matching #5777): a
            # repeat cancellation could eat an off-loop hop before it runs. The
            # exception in flight is the one that must surface.
            if proc is not None:
                try:
                    proc.kill()
                except (OSError, ProcessLookupError):
                    logger.debug(
                        "ffmpeg kill during cancellation cleanup failed",
                        exc_info=True,
                    )
                else:
                    try:
                        await proc.communicate()
                    except BaseException:
                        # A repeat cancellation can land on this await; swallow
                        # it so the unlink below still runs and the ORIGINAL
                        # exception is the one that propagates.
                        pass
            if tmp_ogg:
                try:
                    _unlink_if_exists(tmp_ogg)
                except OSError:
                    # A not-yet-exited child can still hold the file (Windows
                    # lock); letting that escape would REPLACE the in-flight
                    # cancellation with a PermissionError.
                    pass
            raise
        actual_path = tmp_ogg

    transcript_parts: list[str] = []
    stream = None
    try:
        audio_bytes = await asyncio.to_thread(_read_audio_bytes, actual_path)
        if len(audio_bytes) > _TRANSCRIBE_MAX_BYTES:
            logger.error(
                "Audio file too large for Transcribe: >%d bytes",
                _TRANSCRIBE_MAX_BYTES,
            )
            return None

        profile = stt_config.transcribe_profile or None
        credential_resolver = (
            await asyncio.to_thread(_ProfileCredentialResolver, profile) if profile else None
        )

        client = await asyncio.to_thread(
            TranscribeStreamingClient,
            region=region,
            credential_resolver=credential_resolver,
        )
        stream = await client.start_stream_transcription(
            language_code=stt_config.language_code,
            media_sample_rate_hz=_TRANSCRIBE_SAMPLE_RATE_HZ,
            media_encoding="ogg-opus",
        )

        async def write_chunks():
            chunk_size = 8192
            for i in range(0, len(audio_bytes), chunk_size):
                await stream.input_stream.send_audio_event(
                    audio_chunk=audio_bytes[i : i + chunk_size]
                )
            await stream.input_stream.end_stream()

        handler = TranscriptCollector(stream.output_stream, transcript_parts)
        await asyncio.wait_for(
            asyncio.gather(write_chunks(), handler.handle_events()),
            timeout=stt_config.timeout_secs,
        )

        transcript = " ".join(transcript_parts).strip() or None
        return transcript
    except Exception:
        logger.exception("AWS Transcribe streaming STT failed")
        return None
    finally:
        # Nested ``finally`` so the unlink is unconditional: the ``end_stream``
        # await can itself raise on a REPEAT cancellation (``CancelledError`` is
        # a ``BaseException``, so its ``Exception`` guard misses it), and that
        # escape used to skip the temp removal below (#5780).
        try:
            if stream is not None:
                try:
                    await stream.input_stream.end_stream()
                except Exception:
                    pass
        finally:
            if tmp_ogg:
                try:
                    await asyncio.to_thread(_unlink_if_exists, tmp_ogg)
                except BaseException:
                    # A repeat cancellation can land on this await before the
                    # off-loop hop runs; unlink synchronously (one file,
                    # matching #5777) and let the cancellation propagate. The
                    # OSError guard keeps a locked/contended file from
                    # REPLACING the exception already in flight.
                    try:
                        _unlink_if_exists(tmp_ogg)
                    except OSError:
                        pass
                    raise


# ---------------------------------------------------------------------------
# The local recogniser
# ---------------------------------------------------------------------------

#: Shape of a language code whisper understands: two or three ASCII letters
#: (ISO 639-1 / 639-3), never a region. Anything outside it is treated as unset.
_LANGUAGE_RE = re.compile(r"^[a-z]{2,3}$")

#: Suffixes read with the stdlib WAV reader before ffmpeg is considered. Only the
#: suffix is trusted to decide whether to *try*; the reader itself decides whether
#: the bytes are usable, so a mislabelled file falls through to the transcode.
_WAV_SUFFIXES = (".wav", ".wave")

#: Longest audio a batch transcription reads into memory. At 16 kHz float32 this
#: is 4 bytes per sample, so an hour is ~230 MB. The point is to bound a
#: pathological input (a multi-hour recording, a corrupt container ffmpeg decodes
#: forever), not to limit a real voice memo, which is seconds to minutes long.
_MAX_AUDIO_SECS = 3600


def _whisper_language(language_code: str) -> str:
    """Reduce a BCP-47 tag to the bare language whisper wants (``en-US`` -> ``en``).

    Whisper names its languages by ISO 639 code with no region, so a configured
    locale has to be cut down to its primary subtag. An empty, unrecognisably
    shaped, or ``auto`` value returns ``""``, which the recogniser reads as
    auto-detect: a mistyped setting must cost the user a detection pass, never a
    failed transcription. The ``str()`` covers a hand-edited ``config.json``
    holding a non-string, which ``or ""`` would let through because it only
    substitutes on a falsy value.
    """
    primary = str(language_code or "").strip().split("-")[0].split("_")[0].lower()
    return primary if _LANGUAGE_RE.match(primary) else ""


def _make_temp_wav() -> str:
    """Create and close a temporary WAV file without leaking its descriptor."""
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    return path


def _pcm_from_wav(audio_path: str) -> np.ndarray | None:
    """Read a 16 kHz WAV as mono float32, or None when it needs transcoding.

    The dashboard's audio worklet and the recogniser already agree on 16 kHz mono
    int16, so audio that arrives in that form needs no external tool at all. Any
    other rate or sample width returns None so the caller hands it to ffmpeg,
    because resampling correctly is ffmpeg's job and a naive stride would change
    the pitch the model hears.
    """
    try:
        with wave.open(audio_path, "rb") as wav:
            channels = wav.getnchannels()
            if wav.getframerate() != stt.SAMPLE_RATE_HZ or wav.getsampwidth() != 2 or channels < 1:
                return None
            raw = wav.readframes(min(wav.getnframes(), _MAX_AUDIO_SECS * stt.SAMPLE_RATE_HZ))
    except (OSError, EOFError, wave.Error):
        # Not a readable PCM WAV (a compressed payload, a truncated header, a
        # mislabelled suffix). ffmpeg reads far more than the stdlib does, so this
        # is a "try the other route", not a failure.
        return None
    pcm = stt.pcm_from_int16(raw)
    if channels == 1:
        return pcm
    # Drop a final frame the file cut in half before folding channels, so the
    # reshape cannot fail on a truncated recording.
    usable = pcm.size - (pcm.size % channels)
    if usable <= 0:
        return None
    return pcm[:usable].reshape(-1, channels).mean(axis=1, dtype=pcm.dtype)


async def _kill_and_reap(proc: Any) -> None:
    """Stop a child process and collect it. Best effort throughout.

    Reaped with ``communicate()`` rather than ``wait()``: it drains the pipes, so
    a child that died with a full stderr buffer cannot deadlock the reap. Nothing
    here may raise, because the caller already has a failure or an in-flight
    cancellation to report and this cleanup must not replace it.
    """
    try:
        proc.kill()
    except OSError:
        logger.debug("ffmpeg kill during cleanup failed", exc_info=True)
        return
    try:
        await proc.communicate()
    except BaseException:
        # A repeat cancellation can land on this await; swallow it so the
        # caller's own exception is the one that propagates.
        pass


async def _pcm_via_ffmpeg(audio_path: str, timeout_secs: int) -> np.ndarray | None:
    """Transcode *audio_path* to 16 kHz mono and return it as float32 samples.

    A Slack voice memo arrives as ogg/Opus and the dashboard records webm,
    neither of which the stdlib reads. Desktop releases supply the decoder;
    source installs use a system FFmpeg from fixed platform paths. The recogniser
    accepts exactly one format, so the transcode targets it directly rather than
    leaving a rate conversion for later.
    """
    ffmpeg_bin = await _resolve_ffmpeg_for_execution()
    if not ffmpeg_bin:
        logger.error(
            "the audio decoder is unavailable for %s; reinstall the Kiro Crew "
            "desktop app or install system FFmpeg for a source install",
            audio_path,
        )
        return None
    try:
        tmp_wav = await asyncio.to_thread(_make_temp_wav)
    except BaseException:
        await _close_ffmpeg_for_execution(ffmpeg_bin, preserve_active_exception=True)
        raise
    try:
        try:
            proc = await _create_ffmpeg_subprocess(
                ffmpeg_bin,
                "-y",
                "-i",
                audio_path,
                "-ar",
                str(stt.SAMPLE_RATE_HZ),
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                # Bounds the temp file as well as the later read, so a container
                # that decodes forever cannot fill the disk while it does.
                "-t",
                str(_MAX_AUDIO_SECS),
                tmp_wav,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError:
            logger.exception("Could not run ffmpeg (%s) to decode %s", ffmpeg_bin, audio_path)
            return None
        try:
            _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_secs)
        except asyncio.TimeoutError:
            await _kill_and_reap(proc)
            logger.error("ffmpeg decode of %s timed out after %ds", audio_path, timeout_secs)
            return None
        except BaseException:
            # ``CancelledError`` is a ``BaseException``, so the ``TimeoutError``
            # arm never sees it and an abandoned request would leave the child
            # running. Stop AND reap it before the ``finally`` removes the temp:
            # Windows keeps the output file locked until the child fully exits,
            # and on POSIX a live child can race the removal.
            await _kill_and_reap(proc)
            raise
        if proc.returncode != 0:
            tail = stderr.decode(errors="replace").strip()[-500:] if stderr else ""
            logger.error(
                "ffmpeg exited %s decoding %s: %s",
                proc.returncode,
                audio_path,
                tail or "(no stderr)",
            )
            return None
        return await asyncio.to_thread(_pcm_from_wav, tmp_wav)
    finally:
        # Off the loop, and scheduled as its own task BEFORE it is awaited, so a
        # repeat cancellation landing on the await abandons only the wait while
        # the removal still runs to completion in its worker thread. ``shield``
        # keeps that cancellation out of the removal task; the exception itself
        # still reaches the awaiter.
        rm = asyncio.ensure_future(asyncio.to_thread(_unlink_if_exists, tmp_wav))
        await asyncio.shield(rm)


async def _transcribe_local(audio_path: str, stt_config) -> str | None:  # type: ignore[no-untyped-def]
    """Transcribe with the resident whisper.cpp recogniser.

    Everything expensive is shared with every other voice surface: one loaded
    model per process, so a Slack voice memo decodes on the weights a dashboard
    dictation just warmed rather than loading its own copy.
    """
    # Off the loop: the first probe links the recogniser's native extension, and
    # this coroutine is awaited from the Slack path and the transcribe endpoint.
    available = await asyncio.to_thread(stt.availability)
    if not available.ok:
        logger.error("Local speech recognition unavailable: %s", available.detail)
        return None

    pcm: np.ndarray | None = None
    if os.path.splitext(audio_path)[1].lower() in _WAV_SUFFIXES:
        pcm = await asyncio.to_thread(_pcm_from_wav, audio_path)
    if pcm is None:
        pcm = await _pcm_via_ffmpeg(audio_path, stt_config.timeout_secs)
    if pcm is None or pcm.size == 0:
        logger.error("No audio could be decoded from %s", audio_path)
        return None

    # ``timeout_secs`` bounds the transcode above AND, inside the engine, each
    # decode and each model load separately. What it deliberately does NOT bound is
    # the first-run model download: that happens before the engine takes its lock,
    # so a slow transfer cannot be mistaken for a wedged decode and abandoned
    # mid-flight. The decode measures a real-time factor of 0.007-0.011, so the
    # ceiling only ever fires on a genuinely stuck native call.
    #
    # Both bounds are passed on every call because the recogniser is a singleton:
    # they are re-applied to the live instance rather than fixed by whichever
    # surface reached it first, which is what stops a Slack voice memo from pinning
    # the operator's settings to the package defaults.
    text, result = await stt.transcribe_pcm(
        pcm,
        model_name=stt_config.model,
        language=_whisper_language(stt_config.language_code),
        idle_evict_secs=stt_config.idle_evict_secs,
        timeout_secs=stt_config.timeout_secs,
    )
    if not result.ok:
        logger.error("Local speech recognition unavailable: %s", result.detail)
        return None
    # ``transcribe_pcm`` has already applied the hallucination filter, which can
    # empty a transcript that was entirely caption boilerplate. Empty means no
    # transcript, so the caller reports a memo it could not hear instead of
    # writing boilerplate into an agent's notes.
    return text or None


async def _transcribe_apple(audio_path: str, stt_config) -> str | None:  # type: ignore[no-untyped-def]
    """Transcribe with Apple's on-device SpeechAnalyzer (macOS 26+).

    Delegates to :mod:`kiro_crew.apple_speech`, which owns the Swift-helper seam.
    The framework needs a language *locale* rather than whisper's bare language
    code, so ``stt_config.language_code`` (already BCP-47, e.g. ``en-US``) is passed
    straight through; the helper falls back to another installed dialect of the same
    language before it refuses.

    A supported host needs no model download because the OS ships the assets, so
    a failure here is a real error rather than the missing-model state the local
    recogniser can be in on a first run.
    """
    from kiro_crew import apple_speech

    text, metrics = await apple_speech.transcribe(
        audio_path,
        locale=stt_config.language_code or "en-US",
        timeout_secs=stt_config.timeout_secs or apple_speech.DEFAULT_TIMEOUT_SECS,
    )
    if text is None:
        logger.error("Apple speech transcription failed: %s", metrics.get("error", "unknown"))
        return None
    logger.debug(
        "Apple speech: %.2fs for %.1fs of audio (locale=%s)",
        metrics.get("transcribe_secs", 0.0),
        metrics.get("audio_secs", 0.0),
        metrics.get("locale", "?"),
    )
    return text
