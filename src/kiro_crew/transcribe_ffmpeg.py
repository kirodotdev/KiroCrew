"""The ffmpeg decoder behind :mod:`kiro_crew.transcribe`.

Where the executable comes from (the pinned bundled image, the fixed system
directories, the digest-verified store), how a bundled or stored image is
authenticated into a private snapshot before it runs, and how one conversion is
spawned, described when it fails, reaped, and its temporary output removed.

:mod:`kiro_crew.transcribe` stays the import path and the patch surface: every name
here is read and patched as ``kiro_crew.transcribe.<name>``, and the facade's mirror
(:mod:`kiro_crew._facade_mirror`) carries a patch to every module that binds it.
"""

from __future__ import annotations

import asyncio
import errno
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
from dataclasses import dataclass
from typing import Any, Iterator

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.stt import decoder

# Logs under the facade's name, which operators and tests filter on.
logger = logging.getLogger("kiro_crew.transcribe")


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


# The minimal audio-only decoder executables of stt.decoder.DECODER_RELEASE, the
# ones the desktop matrix installs -- one per shipped platform, and which
# platforms those are is _SHIPPED_FFMPEG_PLATFORMS below rather than a count
# restated here. The filename selects the platform artifact; size makes a
# truncated payload fail cheaply; SHA-256 is the trust anchor. Desktop build
# staging is intentionally writable, so path placement or a removable `.git`
# marker cannot establish provenance. These are the bytes the RELEASE publishes.
#
# The table is OWNED by `stt.decoder`, which also installs each artifact into the
# digest-verified store this module resolves from, and into the desktop bundle.
# One table, because the store must never be able to install bytes the
# resolver would refuse: two copies would fail as a decoder that downloads
# successfully and then cannot be executed, with nothing to say which copy is
# wrong. The name stays here because this is where every reader of it looks.
_PACKAGED_FFMPEG_ARTIFACTS: dict[str, tuple[int, str]] = decoder.PACKAGED_FFMPEG_ARTIFACTS


# The build-ffmpeg.sh --target KEYS the desktop matrix actually ships a bundled
# decoder for. This is the maintainer-owned source of truth for WHICH platforms are
# covered; the completeness test in test_transcribe_ffmpeg.py asserts the pinned
# artifact table covers exactly these keys, so adding a lane forces a pin.
#
# Each key ties to the build leg that ships it:
#   macos-aarch64 = the arm64 half of the ONE universal DMG from the macos-15 leg
#       of .github/workflows/build-desktop.yml (the Makefile's `desktop` target).
#   linux-x86_64  = ubuntu-22.04 leg of build-desktop.yml.
#   linux-aarch64 = ubuntu-22.04-arm leg of build-desktop.yml.
#   windows-x86_64 = .github/workflows/build-windows.yml.
#
# macos-x86_64 (the x86_64 half of that same DMG) is DELIBERATELY excluded: the
# Intel backend ships no decoder and resolves a system FFmpeg instead
# (stt.decoder.bundle_carries_decoder).
#
# windows-i686 is DELIBERATELY excluded: no 32-bit Windows target exists in any
# build workflow or the Electron config.
# Adding a win32 lane must add its key here, which then forces a pin via the
# completeness test.
_SHIPPED_FFMPEG_PLATFORMS: frozenset[str] = frozenset(
    {
        "macos-aarch64",
        "linux-x86_64",
        "linux-aarch64",
        "windows-x86_64",
    }
)


# Artifacts the macOS app signer REWRITES on its way into a release, so the
# pinned digest above cannot be the only anchor. Signing replaces the release's
# ad-hoc LC_CODE_SIGNATURE with a Developer ID one (plus hardened runtime and a
# secure timestamp), which changes both size and SHA-256, and it is not optional:
# Apple notarization rejects the whole submission over an unsigned nested
# executable -- including one hidden inside a compressed member, which the notary
# service decompresses and scans (submission 3dbd3c7d). A digest that could
# survive signing does not exist, because it is not known until after the signing
# service has run.
#
# So these artifacts are authenticated by EITHER anchor, both cryptographic:
#   - the pinned release digest -- a local/unsigned build, and the desktop build
#     gate, which executes the decoder BEFORE the bundle is signed; or
#   - a valid Developer ID signature from our own team on the exact bytes staged
#     for execution, which is what a released app carries.
# Neither anchor is a path or a filesystem-permission claim.
# The arm64 slice is here because build-desktop.sh ships its decoder
# executable as a plain Mach-O under Contents/Resources, and the app signer signs
# every nested binary with Developer ID + hardened runtime + secure timestamp
# (generate-manifest.py enumerates them), so the released bytes authenticate via
# the signature anchor, having been rewritten by signing away from the pin.
#
# The set is therefore exactly the macos-* members of _SHIPPED_FFMPEG_PLATFORMS, and
# test_transcribe_ffmpeg.py asserts that equality rather than a subset: a macOS slice added
# above but forgotten here has no anchor left once signing has rewritten its bytes,
# so a SIGNED release would refuse its own decoder.
_SIGNER_REWRITTEN_FFMPEG_ARTIFACTS: frozenset[str] = frozenset({"ffmpeg-macos-aarch64"})


# Upper bound on a signer-rewritten payload, whose exact size is unknowable in
# source. Signing appends a code-signature superblob to a ~50 MB executable, so
# this is a safety ceiling that keeps the copy below bounded, not a pin.
_MAX_SIGNED_FFMPEG_BYTES = 192 * 1024 * 1024


# Apple team identifier of the Developer ID certificate that signs Kiro Crew
# releases (see packaging/signing/manifest-template.json). `anchor apple generic`
# ties the chain to Apple's root, so only a certificate Apple issued to THIS team
# satisfies the requirement -- a self-signed or ad-hoc replacement does not.
_MACOS_SIGNING_TEAM_ID = "94KV3E626L"


# The team identifier MUST stay quoted. In the requirement language a bare token
# beginning with a digit is not a valid identifier, so an unquoted 94KV3E626L is
# a SYNTAX ERROR rather than a comparison: codesign exits non-zero without ever
# evaluating the signature, which this function cannot tell apart from a genuine
# authenticity failure. That made every signed macOS release refuse its own
# decoder -- the digest anchor cannot cover a signed artifact by construction,
# so with the signature anchor unparseable both anchors were unreachable.
_MACOS_FFMPEG_REQUIREMENT = (
    f'anchor apple generic and certificate leaf[subject.OU] = "{_MACOS_SIGNING_TEAM_ID}"'
)


def _macos_developer_id_authentic(path: str) -> bool:
    """True when *path* carries an intact Developer ID signature from our team.

    /usr/bin/codesign is the only supported authenticity oracle for a signed
    Mach-O: it validates the code-directory hashes over the file's own bytes AND
    evaluates the certificate chain, so a tampered or foreign-signed payload
    fails. The absolute path is deliberate -- an ambient `codesign` on PATH must
    never be able to answer this question.
    """
    if not platform_compat.IS_MACOS:
        return False
    try:
        result = subprocess.run(
            [
                "/usr/bin/codesign",
                "--verify",
                "--strict",
                # A leading "=" marks the argument as requirement SOURCE TEXT;
                # without it codesign reads it as a path to a requirement file.
                "-R",
                f"={_MACOS_FFMPEG_REQUIREMENT}",
                "--",
                path,
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


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


def _ffmpeg_payload_chunks(descriptor: int) -> Iterator[bytes]:
    """Yield the executable bytes of a package resource."""
    while True:
        chunk = os.read(descriptor, 1 << 20)
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
        if pid == os.getpid():
            continue
        # A live or unsignalable owner keeps its snapshot -- unless it is the
        # supervisor an in-app restart through a supervising launcher left
        # behind: this gateway's own previous image, exec-ed into the launcher
        # with its pid kept, running no decoder. A snapshot it left
        # mid-transcription is stale, and the supervisor never reads dead while
        # this gateway runs. A live gateway ancestor still runs Python and keeps
        # its snapshot.
        owner_gone = platform_compat.pid_liveness(pid) == platform_compat.PID_DEAD
        if not owner_gone and not platform_compat.is_exec_supervisor_of_this_process(pid):
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
    signature_anchored: bool = False,
) -> _AuthenticatedFfmpeg | None:
    """Copy/hash exact bytes and keep an immutable execution identity open.

    Linux executes a sealed memfd by inherited descriptor. macOS executes an
    gateway-owned named snapshot because its Mach-O loader cannot reliably
    execute an unlinked ``/dev/fd`` image; agent sandboxes cannot reach its root,
    and the file and its parent stay non-writable from verification through
    spawn. Windows instead holds a ``CreateFileW`` handle
    that denies both write and delete sharing until ``CreateProcess`` has opened
    the image. In every case the bytes hashed are the bytes staged for execution.

    ``signature_anchored`` marks an artifact the macOS app signer rewrites (see
    ``_SIGNER_REWRITTEN_FFMPEG_ARTIFACTS``): the pinned digest still authenticates
    an unsigned build, and a Developer ID signature from our own team authenticates
    the released one. One of the two must hold; a payload that satisfies neither is
    refused exactly as before.
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
        if signature_anchored:
            # The signed size is unknowable in source, so bound the copy by the
            # file's own length under a safety ceiling instead of by the pin.
            if opened.st_size <= 0 or opened.st_size > _MAX_SIGNED_FFMPEG_BYTES:
                return None
            size_limit = opened.st_size
        elif opened.st_size != expected_size:
            return None
        else:
            size_limit = expected_size

        seal_snapshot = False
        if not platform_compat.IS_WINDOWS:
            snapshot_writer, snapshot, seal_snapshot, snapshot_path = _new_executable_snapshot()

        digest = hashlib.sha256()
        total = 0
        for chunk in _ffmpeg_payload_chunks(source):
            total += len(chunk)
            if total > size_limit:
                return None
            digest.update(chunk)
            if snapshot_writer >= 0:
                _write_all(snapshot_writer, chunk)
        source_digest = digest.hexdigest()
        upstream_bytes = total == expected_size and source_digest == expected_sha256
        if not upstream_bytes and not signature_anchored:
            return None

        if platform_compat.IS_WINDOWS:
            # Windows artifacts are never signer-rewritten, so reaching here means
            # the pinned digest matched.
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
        # the last writer owned by this process has closed. Compared against the
        # digest of what was READ, so the snapshot is proven identical to the
        # verified source whether the anchor was the pin or the signature.
        snapshot_digest = hashlib.sha256()
        snapshot_total = 0
        while True:
            chunk = os.read(snapshot, 1 << 20)
            if not chunk:
                break
            snapshot_total += len(chunk)
            snapshot_digest.update(chunk)
        if snapshot_total != total or snapshot_digest.hexdigest() != source_digest:
            return None
        os.lseek(snapshot, 0, os.SEEK_SET)
        if snapshot_path is None:
            execution_path = f"/proc/self/fd/{snapshot}"
        else:
            execution_path = snapshot_path
        # Bytes that are not the pinned payload are only executed when
        # macOS itself vouches for their signature, on the snapshot about to run.
        if not upstream_bytes and not _macos_developer_id_authentic(execution_path):
            return None
        result = _AuthenticatedFfmpeg(
            candidate,
            snapshot,
            execution_path,
            cleanup_path=snapshot_path,
        )
        snapshot = -1  # ownership transferred to result
        snapshot_path = None  # ownership transferred to result
        return result
    except OSError:
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


def _open_authenticated_in(
    binaries_root: str, *, containing_root: str | None = None, allow_signature_anchor: bool
) -> list[_AuthenticatedFfmpeg]:
    """Open every pinned artifact directly inside *binaries_root* that authenticates.

    One implementation for both places a pinned executable can be found -- a
    bundled interpreter's ``kirocrew_ffmpeg/bin`` and the digest-verified store
    under the data home -- so the path-safety checks around the digest cannot be
    present at one and missing at the other. Callers differ only in whether the
    macOS signature anchor applies (it does not in the store: nothing signs those
    bytes, so the pin is the only anchor there).

    *containing_root* is the directory *binaries_root* must resolve inside, for a
    caller whose root is itself composed from an outside value.
    """
    opened: list[_AuthenticatedFfmpeg] = []
    try:
        if containing_root is not None and (
            os.path.commonpath((containing_root, binaries_root)) != containing_root
        ):
            return opened
    except ValueError:
        return opened
    # Driven by the pin table rather than by a directory listing: the table is
    # already the sole authority on which filenames may be opened here, so probing
    # its keys says that directly instead of enumerating the directory and then
    # discarding everything absent from the table. It also keeps the name that
    # reaches the log below a module constant rather than a value composed from the
    # interpreter's own install prefix.
    for filename, artifact in _PACKAGED_FFMPEG_ARTIFACTS.items():
        unresolved = os.path.join(binaries_root, filename)
        candidate = os.path.realpath(unresolved)
        # A symlink out of the directory, or a name that only resolves to this
        # directory after following one, is refused rather than hashed: the digest
        # would then describe bytes at a path nobody vouched for.
        if (
            os.path.dirname(candidate) != binaries_root
            or candidate != os.path.abspath(unresolved)
            or not os.path.isfile(candidate)
        ):
            continue
        if not platform_compat.IS_WINDOWS and not os.access(candidate, os.X_OK):
            continue
        authenticated = _authenticated_ffmpeg(
            candidate,
            *artifact,
            signature_anchored=(
                allow_signature_anchor and filename in _SIGNER_REWRITTEN_FFMPEG_ARTIFACTS
            ),
        )
        if authenticated is None:
            logger.warning(
                "Ignoring %s in a pinned decoder location: its bytes do not match the "
                "digest pinned for that filename.",
                filename,
            )
            continue
        opened.append(authenticated)
    return opened


def _open_packaged_ffmpeg_resource() -> _AuthenticatedFfmpeg | None:
    """Open the one authenticated bundled decoder executable in this runtime."""
    candidates: list[_AuthenticatedFfmpeg] = []
    for root in _trusted_site_package_roots():
        root = os.path.realpath(root)
        package_root = os.path.realpath(os.path.join(root, decoder.PACKAGED_DIR[0]))
        binaries_root = os.path.realpath(os.path.join(package_root, *decoder.PACKAGED_DIR[1:]))
        try:
            if os.path.commonpath((root, package_root)) != root:
                continue
        except ValueError:
            continue
        candidates.extend(
            _open_authenticated_in(
                binaries_root,
                containing_root=package_root,
                allow_signature_anchor=True,
            )
        )
    if len(candidates) == 1:
        return candidates[0]
    for opened_candidate in candidates:
        opened_candidate.close()
    return None


def _open_store_ffmpeg_resource() -> _AuthenticatedFfmpeg | None:
    """Open the decoder in the data home's store, if its bytes match the pin.

    The third and last source, after a bundled interpreter's own payload and a
    package manager's system FFmpeg. It exists because a source install on a
    distribution that ships no FFmpeg package has no other decoder it can
    reach, and the store is how ``stt.decoder`` puts the SAME pinned bytes
    the desktop release carries onto such a host.

    This does not widen the trust model, and the distinction is worth being exact
    about: the store directory is user-writable, so its PATH vouches for nothing
    and is not treated as if it did. What is accepted is a filename in
    ``_PACKAGED_FFMPEG_ARTIFACTS`` whose bytes match that pin, re-verified here on
    every open exactly as a bundled payload is, with the bytes staying bound to the
    descriptor that gets spawned. A file that fails is ignored and logged rather
    than executed, and no store directory is added to ``_ffmpeg_candidate_dirs`` --
    that list is for a package manager's own directories, where the search is by
    NAME and a match would be executed on the strength of where it sits.

    The macOS signature anchor deliberately does not apply: nothing signs these
    bytes, so accepting a signature here would accept a payload the pin refused.
    """
    # Resolved before the scan, exactly as the packaged roots are. The per-file
    # guard compares the realpath of a candidate against this directory, so a data
    # home reached through a symlinked ancestor -- /home -> /var/home on an
    # rpm-ostree distribution, which is also one that ships no ffmpeg package --
    # would otherwise fail that comparison for every file and report a decoder this
    # store had just installed and verified as absent, forever.
    store_dir = os.path.realpath(str(decoder.store_dir()))
    candidates = _open_authenticated_in(store_dir, allow_signature_anchor=False)
    if len(candidates) == 1:
        return candidates[0]
    # More than one pinned filename in the store means two platforms' decoders are
    # present; refusing is the same ambiguity guard the packaged lookup applies.
    for opened_candidate in candidates:
        opened_candidate.close()
    return None


def _store_ffmpeg() -> str | None:
    """Report the store decoder's path when it authenticates, else ``None``."""
    authenticated = _open_store_ffmpeg_resource()
    if authenticated is None:
        return None
    try:
        return authenticated.source_path
    finally:
        authenticated.close()


def _packaged_ffmpeg_resource() -> str | None:
    """Resolve the ffmpeg executable inside this interpreter's package tree.

    The desktop build stores one platform-native executable in
    ``site-packages/kirocrew_ffmpeg/bin`` (``stt.decoder.PACKAGED_DIR``). Resolve
    that exact resource rather than anything on an ambient PATH, which this
    gateway must never execute.
    """
    authenticated = _open_packaged_ffmpeg_resource()
    if authenticated is None:
        return None
    try:
        return authenticated.source_path
    finally:
        authenticated.close()


#: Outcome codes for :func:`_packaged_ffmpeg_version_probe`.
DECODER_OK = "decoder_ok"


DECODER_UNAUTHENTIC = "decoder_unauthentic"


DECODER_NOT_EXECUTABLE = "decoder_not_executable"


#: Windows loader refusals worth naming in the build gate's report. Both mean the
#: image was rejected BEFORE its entry point ran -- a missing or wrong-version
#: import -- which is a property of the host, never of the bytes. Spelled as the
#: signed values ``subprocess`` reports, because CPython surfaces the Windows exit
#: DWORD through a signed C int.
_WINDOWS_LOADER_STATUS: dict[int, str] = {
    -1073741515: "STATUS_DLL_NOT_FOUND",
    -1073741511: "STATUS_ENTRYPOINT_NOT_FOUND",
}


#: Cap on child output quoted into a probe's ``detail``. Enough for a loader or
#: dynamic-linker complaint, short enough to stay one readable log line.
_DECODER_DETAIL_MAX_CHARS = 400


@dataclass(frozen=True)
class PackagedDecoderProbe:
    """Whether the packaged decoder authenticated, and whether it then ran.

    Two INDEPENDENT questions, and collapsing them is what made this unreadable.
    ``authentic`` is a property of the ARTIFACT: the bytes matched the pinned
    pinned digest or an accepted signature, and it must hold on every host.
    ``ok`` additionally requires that they EXECUTED, which is a property of the
    BUILD HOST -- a container image can lack an OS library the executable
    load-time imports, and the loader then refuses it before its entry point runs
    even though the identical bytes run correctly for a user.

    A caller that treats a host limitation as a corrupt payload sends the reader
    to the wrong half of the problem, so the release gate reads these separately:
    ``authentic`` false fails the build, ``ok`` false alone only warns.
    """

    ok: bool
    authentic: bool
    code: str = DECODER_OK
    detail: str = ""


def _decoder_exit_detail(source_path: str, result: subprocess.CompletedProcess[bytes]) -> str:
    """Describe a decoder that authenticated but would not run."""
    code = result.returncode
    named = _WINDOWS_LOADER_STATUS.get(code)
    # The unsigned spelling is what a reader can look up; the signed one is what
    # the log of a failing build will actually have shown them.
    status = f"exit {code} (0x{code & 0xFFFFFFFF:08X}{f', {named}' if named else ''})"
    # Decoded here rather than by asking subprocess for text mode: a loader or
    # dynamic-linker complaint arrives in the host's console encoding, not
    # necessarily UTF-8, and a probe must not raise while explaining a failure.
    streams = b"\n".join(part for part in (result.stderr, result.stdout) if part)
    noise = streams.decode("utf-8", "replace").strip()
    if len(noise) > _DECODER_DETAIL_MAX_CHARS:
        noise = f"{noise[:_DECODER_DETAIL_MAX_CHARS]}…"
    return f"{source_path} authenticated but did not run: {status}{f'; {noise}' if noise else ''}"


def _packaged_ffmpeg_version_probe() -> PackagedDecoderProbe:
    """Authenticate the packaged decoder, then try to run it, for the build gate.

    Both halves are reported because they fail for unrelated reasons and demand
    unrelated fixes; see :class:`PackagedDecoderProbe`. Streams are captured
    rather than discarded so that a refusal explains itself in the build log
    instead of arriving as one unattributable line.
    """
    authenticated = _open_packaged_ffmpeg_resource()
    if authenticated is None:
        return PackagedDecoderProbe(
            ok=False,
            authentic=False,
            code=DECODER_UNAUTHENTIC,
            detail=(
                "no packaged decoder authenticated against the pinned release "
                "digest or an accepted signature"
            ),
        )
    source_path = authenticated.source_path
    try:
        kwargs: dict[str, Any] = {}
        if not platform_compat.IS_WINDOWS:
            kwargs["pass_fds"] = (authenticated.descriptor,)
        result = subprocess.run(
            [authenticated.execution_path, "-version"],
            check=False,
            capture_output=True,
            **kwargs,
        )
    except OSError as exc:
        return PackagedDecoderProbe(
            ok=False,
            authentic=True,
            code=DECODER_NOT_EXECUTABLE,
            detail=f"{source_path} authenticated but could not be spawned: {exc}",
        )
    finally:
        authenticated.close()
    if result.returncode == 0:
        return PackagedDecoderProbe(ok=True, authentic=True)
    return PackagedDecoderProbe(
        ok=False,
        authentic=True,
        code=DECODER_NOT_EXECUTABLE,
        detail=_decoder_exit_detail(source_path, result),
    )


def _bundled_ffmpeg() -> str | None:
    """Return the authenticated decoder carried by a bundled interpreter."""
    if not decoder.bundle_carries_decoder():
        return None
    return _packaged_ffmpeg_resource()


def _open_ffmpeg_for_execution() -> str | _AuthenticatedFfmpeg | None:
    """Resolve FFmpeg, retaining authenticated bundled bytes until spawn."""
    if decoder.bundle_carries_decoder():
        # A desktop release that ships a decoder is self-contained. If its
        # authenticated decoder is missing or damaged, fail closed instead of
        # executing a fixed-path binary that was never authenticated as part of
        # this installation. A release that ships none (macOS Intel) has nothing
        # to be damaged, so it falls through to the source-install order below.
        return _open_packaged_ffmpeg_resource()
    system = _find_system_ffmpeg()
    if system is not None:
        return system
    # Last: the digest-verified store. Ordered after a package manager's copy
    # because a system FFmpeg is the one a host's own updates keep current, and
    # because a host that has one never needed the store to be populated.
    return _open_store_ffmpeg_resource()


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
    """Resolve off-loop and retain cleanup ownership if this task is cancelled.

    When nothing resolves but the store still holds a decoder under a RETIRED
    pinned name (an install that fetched one before the pins were renamed), the
    current pin is fetched once and resolution retried, so an upgrade does not
    silently break batch voice until someone presses Download again.
    """
    resolved = await _resolve_once()
    if resolved is not None:
        return resolved
    if await decoder.migrate_retired_decoder() is None:
        return None
    return await _resolve_once()


async def _resolve_once() -> str | _AuthenticatedFfmpeg | None:
    """One off-loop resolution, keeping cleanup ownership across cancellation."""
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


def _describe_ffmpeg_exit(returncode: int | None, stderr_tail: str) -> str:
    """Render an FFmpeg exit status for a log line, naming a signal death.

    A negative return code is an external signal, not an FFmpeg error, and a
    signalled child usually wrote no stderr, so a bare "exited -9 ...
    (no stderr)" reads as corrupt audio. On macOS the likeliest sender for a
    just-spawned staged binary is the asynchronous system policy assessment
    denying the exec, so name that path in the line.
    """
    detail = stderr_tail or "(no stderr)"
    if returncode is None or returncode >= 0:
        return f"exited {returncode}: {detail}"
    message = f"was killed by signal {-returncode}: {detail}"
    if platform_compat.IS_MACOS:
        message += (
            "; on macOS a SIGKILL immediately after spawn usually means the"
            " system policy assessment (Gatekeeper) denied the exec"
        )
    return message


async def _create_ffmpeg_subprocess(
    executable: str | _AuthenticatedFfmpeg, *args: str, **kwargs: Any
) -> asyncio.subprocess.Process:
    """Spawn FFmpeg while its authenticated image remains immutable/open.

    A returning ``create_subprocess_exec`` means only that the fork/exec was
    issued, not that the platform authorized it: on macOS the syspolicy
    assessment resolves the staged *path* asynchronously after the spawn, so
    closing the handle here (which removes the staged directory) makes the
    kernel deny the exec with SIGKILL. The caller therefore owns the
    close and must run it once the child has exited. A failed spawn never
    produced a child, so nothing depends on the path surviving and the handle
    is closed here before the error propagates.

    Every invocation is pinned to LOCAL protocols: FFmpeg's protocol allowlist
    flag is prepended ahead of the caller's args (set to ``file,pipe``) so it
    precedes every ``-i``. The import suffix allowlist upstream is a "did the
    user mean this" filter, not a content check, so a file whose bytes are an
    HLS/ffconcat playlist reaches the demuxer — without the protocol pin the
    demuxer would then FETCH the playlist's segment URLs (SSRF from a crafted
    recording). Every caller in ``transcribe`` and its owners reads one
    validated local file and writes a local temp file, a null sink, or a pipe,
    so nothing legitimate needs a network protocol; the pin applies to nested
    opens (playlist segments) as well as the top-level input.
    """
    guarded = ("-protocol_whitelist", "file,pipe", *args)  # wokeignore:rule=whitelist
    if not platform_compat.IS_WINDOWS:
        # A descriptor-path input (``/dev/fd/N``) is only readable by the child
        # if N survives the exec: collect every one in the argv and inherit it.
        # ``pass_fds`` keeps the same numbers open in the child, so the argv
        # needs no rewriting. Windows never receives descriptor paths (the
        # import route refuses platforms without pinned traversal), so this is
        # POSIX-only by construction.
        dev_fds = tuple(
            fd for fd in (_dev_fd_number(a) for a in args if isinstance(a, str)) if fd is not None
        )
        if dev_fds:
            kwargs["pass_fds"] = tuple(kwargs.get("pass_fds", ())) + dev_fds
    if isinstance(executable, str):
        return await asyncio.create_subprocess_exec(executable, *guarded, **kwargs)
    try:
        if not platform_compat.IS_WINDOWS:
            kwargs["pass_fds"] = tuple(kwargs.get("pass_fds", ())) + (executable.descriptor,)
        return await asyncio.create_subprocess_exec(executable.execution_path, *guarded, **kwargs)
    except BaseException:
        await _close_ffmpeg_for_execution(executable, preserve_active_exception=True)
        raise


#: The FFmpeg demuxer each supported audio suffix promises to be. Forcing the
#: demuxer (``-f <name>`` before ``-i``) is the second half of the r19 protocol
#: pin: the protocol allowlist stops NETWORK fetches, but a crafted
#: allowed-suffix HLS/ffconcat playlist could still make an auto-probed demuxer
#: open OTHER LOCAL FILES its text names — reads that never went through
#: ``validate_file_path``. With the suffix's own demuxer
#: forced, playlist text is a decode error rather than a set of paths to open.
#: A mislabeled-but-genuine recording is refused the same way, which matches
#: the import vet gate's "did the user mean this" contract.
_DEMUXER_BY_SUFFIX = {
    ".wav": "wav",
    ".mp3": "mp3",
    ".m4a": "mov,mp4,m4a,3gp,3g2,mj2",
    ".mp4": "mov,mp4,m4a,3gp,3g2,mj2",
    ".ogg": "ogg",
    ".oga": "ogg",
    ".opus": "ogg",
    ".flac": "flac",
    ".webm": "matroska,webm",
    ".mkv": "matroska,webm",
    ".aac": "aac",
    ".wma": "asf",
}


#: Descriptor-path inputs (``/dev/fd/N``, and Linux's ``/proc/self/fd/N``): an
#: import hands its consumers one of these instead of the snapshot's mutable
#: name, so every open — ours and FFmpeg's — pins the inode the route opened
#: (a same-uid racer could otherwise swap the snapshot between
#: the duration probe and the transcode, defeating the truncation guard).
_DEV_FD_RE = re.compile(r"^(?:/dev/fd|/proc/self/fd)/(\d+)$")


def _dev_fd_number(audio_path: str) -> int | None:
    """The descriptor a ``/dev/fd``-style input names, or None for a plain path."""
    match = _DEV_FD_RE.match(audio_path)
    return int(match.group(1)) if match else None


def _input_suffix(audio_path: str) -> str | None:
    """The validated suffix behind *audio_path*, resolving descriptor paths.

    Format decisions (demuxer pin, WAV fast paths, remux branches) must follow
    the suffix the caller VALIDATED. For a descriptor path that suffix is read
    through the kernel's own name for the open descriptor
    (:func:`kiro_crew.pinned_fs.fd_real_path`) — never by trusting the mutable
    original name. ``None`` means the suffix cannot be known (an unresolvable
    descriptor path): callers take no fast path, and the demuxer pin refuses
    rather than falling back to content sniffing.
    """
    fd = _dev_fd_number(audio_path)
    if fd is None:
        return os.path.splitext(audio_path)[1].lower()
    real = pinned_fs.fd_real_path(fd)
    if real is None:
        return None
    return os.path.splitext(real)[1].lower()


def _forced_demuxer_args(audio_path: str) -> tuple[str, ...]:
    """``("-f", <demuxer>)`` for a recognized audio suffix, else ``()``.

    Every import-admissible suffix (``k.IMPORT_AUDIO_EXTENSIONS``) is covered,
    so an attacker-influenced import input is ALWAYS decoded by the demuxer its
    validated name promises — never by content sniffing. The empty fallback is
    reachable only for the gateway's own internal temp files, whose names this
    process chose itself.
    """
    suffix = _input_suffix(audio_path)
    if suffix is None:
        # A descriptor-pinned input whose real suffix cannot be read: refuse.
        # Falling back to content sniffing here is exactly the playlist hole
        # the demuxer pin closes. OSError, so every spawn site's existing
        # failure arm turns it into that caller's normal "could not decode"
        # answer (a retryable refusal for the import route).
        raise OSError(f"cannot resolve the suffix behind {audio_path}")
    demuxer = _DEMUXER_BY_SUFFIX.get(suffix)
    if demuxer is None:
        if _dev_fd_number(audio_path) is not None:
            # Descriptor inputs are the attacker-influenced imports, and their
            # VALIDATED suffix always maps (the coverage test pins every
            # IMPORT_AUDIO_EXTENSIONS entry). The resolution follows the
            # descriptor's CURRENT name, so an unmapped answer here means the
            # snapshot was renamed after pinning — a same-uid racer stripping
            # the suffix to re-enable content sniffing.
            # Refuse: a rename may only ever cause a loud refusal, never a
            # sniffed playlist.
            raise OSError(f"descriptor input {audio_path} resolved to unmapped suffix {suffix!r}")
        return ()
    return ("-f", demuxer)


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


def _unlink_if_exists(path: str) -> None:
    """Remove *path*, tolerating another cleanup path winning the race."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


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
