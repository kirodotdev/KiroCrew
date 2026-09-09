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
source installs use a system FFmpeg from fixed platform paths, or the
digest-verified store :mod:`kiro_crew.stt.decoder` fetches the same pinned upstream
bytes into. A 16 kHz mono WAV and live PCM skip the executable entirely.

Two guards here are deliberately provider-independent, because a per-branch copy
is a copy that will be missing from the next branch someone adds:
:func:`_is_sensitive_audio_path` refuses before any provider is dispatched, and
:func:`_redact_transcript` runs on every provider's output.
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
import wave
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterator

from kiro_crew import aws_consent, pinned_fs, platform_compat, stt

# The pinned-artifact table and the digest-verified decoder store live here. It
# imports no numpy and no recogniser binding, so this stays cheap on the gateway
# boot path; `stt.decoder` in turn imports THIS module only inside a function,
# which is what keeps the pair acyclic.
from kiro_crew.stt import decoder

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

from kiro_crew.extras import install_hint

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


# imageio-ffmpeg==0.6.0 executables, taken from the published wheels that the
# desktop matrix installs -- one per shipped platform, and which platforms those
# are is _SHIPPED_FFMPEG_PLATFORMS below rather than a count restated here. The
# filename selects the platform artifact; size makes a truncated payload fail
# cheaply; SHA-256 is the trust anchor. Desktop build staging is intentionally
# writable, so path placement or a removable `.git` marker cannot establish
# provenance. These are the bytes the WHEEL publishes.
#
# The table is OWNED by `stt.decoder`, which also pins the wheel each artifact
# comes out of and installs it into the digest-verified store this module resolves
# from. One table, because the store must never be able to install bytes the
# resolver would refuse: two copies would fail as a decoder that downloads
# successfully and then cannot be executed, with nothing to say which copy is
# wrong. The name stays here because this is where every reader of it looks.
_PACKAGED_FFMPEG_ARTIFACTS: dict[str, tuple[int, str]] = decoder.PACKAGED_FFMPEG_ARTIFACTS

# The upstream imageio_ffmpeg platform KEYS the desktop matrix actually ships a
# bundled decoder for. This is the maintainer-owned source of truth for WHICH
# platforms are covered; the completeness test in test_transcribe.py maps each key
# through imageio_ffmpeg._definitions.FNAME_PER_PLATFORM to derive the authoritative
# filename set and asserts _PACKAGED_FFMPEG_ARTIFACTS covers exactly it. Keys, not
# filenames, so this module imports no imageio_ffmpeg at all (not a core dependency).
#
# Each key ties to the build leg that ships it:
#   macos-aarch64 + macos-x86_64 = the ONE universal DMG from the macos-15 leg of
#       .github/workflows/build-desktop.yml; the Makefile's `desktop` target builds
#       that single DMG covering arm64 AND x86_64, so both slices ship together.
#   linux-x86_64  = ubuntu-22.04 leg of build-desktop.yml.
#   linux-aarch64 = ubuntu-22.04-arm leg of build-desktop.yml.
#   windows-x86_64 = .github/workflows/build-windows.yml.
#
# windows-i686 (upstream ffmpeg-win32-v4.2.2.exe) is DELIBERATELY excluded: no
# 32-bit Windows target exists in any build workflow or the Electron config.
# Adding a win32 lane must add its key here, which then forces a pin via the
# completeness test.
_SHIPPED_FFMPEG_PLATFORMS: frozenset[str] = frozenset(
    {
        "macos-aarch64",
        "macos-x86_64",
        "linux-x86_64",
        "linux-aarch64",
        "windows-x86_64",
    }
)

# Artifacts the macOS app signer REWRITES on its way into a release, so the
# upstream digest above cannot be the only anchor. Signing replaces the wheel's
# ad-hoc LC_CODE_SIGNATURE with a Developer ID one (plus hardened runtime and a
# secure timestamp), which changes both size and SHA-256, and it is not optional:
# Apple notarization rejects the whole submission over an unsigned nested
# executable -- including one hidden inside a compressed member, which the notary
# service decompresses and scans (submission 3dbd3c7d). A digest that could
# survive signing does not exist, because it is not known until after the signing
# service has run.
#
# So these artifacts are authenticated by EITHER anchor, both cryptographic:
#   - the pinned upstream digest -- a local/unsigned build, and the desktop build
#     gate, which executes the decoder BEFORE the bundle is signed; or
#   - a valid Developer ID signature from our own team on the exact bytes staged
#     for execution, which is what a released app carries.
# Neither anchor is a path or a filesystem-permission claim.
# BOTH macOS slices are here: build-desktop.sh ships the arm64 AND x86_64
# imageio-ffmpeg executables as plain Mach-O under Contents/Resources, and the app
# signer signs every nested binary with Developer ID + hardened runtime + secure
# timestamp (generate-manifest.py enumerates them). So the released Intel-Mac slice
# authenticates via its signature anchor exactly like the arm64 slice, its bytes
# having been rewritten by signing away from the pinned upstream digest.
#
# The set is therefore exactly the macos-* members of _SHIPPED_FFMPEG_PLATFORMS, and
# test_transcribe.py asserts that equality rather than a subset: a macOS slice added
# above but forgotten here has no anchor left once signing has rewritten its bytes,
# so a SIGNED release would refuse its own decoder.
_SIGNER_REWRITTEN_FFMPEG_ARTIFACTS: frozenset[str] = frozenset(
    {"ffmpeg-macos-aarch64-v7.1", "ffmpeg-macos-x86_64-v7.1"}
)

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
    ``_SIGNER_REWRITTEN_FFMPEG_ARTIFACTS``): the upstream digest still authenticates
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
        # Bytes that are not the pinned upstream payload are only executed when
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
    bundled interpreter's ``imageio_ffmpeg/binaries`` and the digest-verified store
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
    """Open the one authenticated imageio-ffmpeg executable in this runtime."""
    candidates: list[_AuthenticatedFfmpeg] = []
    for root in _trusted_site_package_roots():
        root = os.path.realpath(root)
        package_root = os.path.realpath(os.path.join(root, "imageio_ffmpeg"))
        binaries_root = os.path.realpath(os.path.join(package_root, "binaries"))
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
    distribution that ships no FFmpeg package previously had no decoder it could
    ever reach, and the store is how ``stt.decoder`` puts the SAME upstream bytes
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
    upstream digest or an accepted signature, and it must hold on every host.
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
                "no packaged decoder authenticated against the pinned upstream "
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


def _describe_ffmpeg_exit(returncode: int | None, stderr_tail: str) -> str:
    """Render an FFmpeg exit status for a log line, naming a signal death.

    A negative return code is an external signal, not an FFmpeg error, and a
    signalled child usually wrote no stderr, so a bare "exited -9 ...
    (no stderr)" reads as corrupt audio. On macOS the likeliest sender for a
    just-spawned staged binary is the asynchronous system policy assessment
    denying the exec (#8918), so name that path in the line.
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
    closing the handle here (which removes the staged directory) had the
    kernel deny the exec with SIGKILL (#8918). The caller therefore owns the
    close and must run it once the child has exited. A failed spawn never
    produced a child, so nothing depends on the path surviving and the handle
    is closed here before the error propagates.

    Every invocation is pinned to LOCAL protocols: FFmpeg's protocol allowlist
    flag is prepended ahead of the caller's args (set to ``file,pipe``) so it
    precedes every ``-i``. The import suffix allowlist upstream is a "did the
    user mean this" filter, not a content check, so a file whose bytes are an
    HLS/ffconcat playlist reaches the demuxer — without the protocol pin the
    demuxer would then FETCH the playlist's segment URLs (SSRF from a crafted
    recording, GPT review r19). Every caller in this module reads one
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
#: ``validate_file_path`` (GPT review r20). With the suffix's own demuxer
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
#: (GPT review r21: a same-uid racer could otherwise swap the snapshot between
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
            # the suffix to re-enable content sniffing (GPT review r25).
            # Refuse: a rename may only ever cause a loud refusal, never a
            # sniffed playlist.
            raise OSError(f"descriptor input {audio_path} resolved to unmapped suffix {suffix!r}")
        return ()
    return ("-f", demuxer)


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
    """Report the authenticated bundle path, a fixed-path system FFmpeg, or the store.

    Deliberately NOT ``shutil.which("ffmpeg")``. A gateway's PATH can legitimately lead
    with agent-writable directories (a worktree venv's ``bin``, ``~/.local/bin``), which
    is exactly the threat `platform_compat.trusted_system_bin` documents: the result
    here describes what the execution resolver would use. Execution itself calls
    :func:`_open_ffmpeg_for_execution`, which keeps bundled bytes bound through spawn.

    The trusted system directories are tried first, then the fixed candidate list,
    which is itself ordered most-trusted first. ffmpeg is not an OS tool -- on macOS it
    lives under a Homebrew prefix and on Windows under a package-manager directory --
    so the system set alone would find it almost nowhere.

    Last comes the digest-verified store (:func:`_store_ffmpeg`), which is a
    filename-and-digest match rather than a directory the search trusts; see
    :func:`_open_store_ffmpeg_resource` for why that is not the same widening.

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
    system = _find_system_ffmpeg()
    if system is not None:
        return system
    return _store_ffmpeg()


#: Where the decoder the transcode path would run comes from, as reported by
#: :func:`ffmpeg_source` and served on ``GET /api/stt/status``. Codes rather than
#: prose because the dashboard renders localised text, and each one leads
#: somewhere different: a bundled payload is repaired by reinstalling the app, a
#: system one by the host's package manager, and the store one by
#: ``stt.decoder``'s own fetch.
FFMPEG_SOURCE_BUNDLED = "bundled"
FFMPEG_SOURCE_SYSTEM = "system"
FFMPEG_SOURCE_STORE = "store"


def ffmpeg_source() -> str | None:
    """Which of the three decoder sources answers on this host, or ``None``.

    Resolved in the same order :func:`_open_ffmpeg_for_execution` uses, so the
    settings panel names the decoder that would actually run rather than the first
    one that happens to exist.
    """
    if platform_compat.is_bundled_interpreter():
        return FFMPEG_SOURCE_BUNDLED if _bundled_ffmpeg() is not None else None
    if _find_system_ffmpeg() is not None:
        return FFMPEG_SOURCE_SYSTEM
    if _store_ffmpeg() is not None:
        return FFMPEG_SOURCE_STORE
    return None


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
_VOICE_EXTRA_HINT = f"AWS Transcribe needs its cloud dependencies: {install_hint('voice-aws')}"


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


def load_stt_config() -> Any:
    """One STT configuration snapshot, for callers that must not re-read it.

    Every function here that takes an ``stt_config`` parameter re-loads the
    configuration when handed None. That is right for a single call, and wrong
    for a SEQUENCE whose answers must agree: a readiness check, a duration-cap
    answer, and the transcription itself each re-reading the file can straddle an
    operator changing the provider in Settings, so the gate evaluates one
    provider's rules and the decode runs under another's. A caller doing several
    of those calls loads ONE snapshot here and passes it to each. BLOCKING
    (reads config); call off the event loop.
    """
    return _load_stt_config()


def _under_voice_runtime_root(real: str) -> bool:
    """Whether *real* lies under the gateway's own voice-runtime staging root.

    The crew ``run/`` directory is a read+write-sensitive leaf (it holds spawn
    trust roots), so ``is_sensitive_path`` refuses everything beneath it —
    including the import snapshots this gateway itself stages under
    ``run/voice-runtime`` precisely BECAUSE agents cannot reach that root
    (GPT review r29 relocation). Judged against the kernel-resolved name of an
    already-pinned descriptor, membership here means "a file this process
    staged", not "a caller-supplied path": the route's own vet gate has
    already refused sensitive ORIGINAL paths before any snapshot exists.
    """
    from kiro_crew.sandbox import prime_voice_runtime_sandbox_paths

    root = os.path.realpath(prime_voice_runtime_sandbox_paths())
    try:
        return os.path.commonpath((os.path.realpath(real), root)) == root
    except ValueError:  # different drives on Windows
        return False


def _is_sensitive_audio_path(audio_path: str) -> bool:
    """Run the filesystem-resolving sensitive-path guard off the event loop.

    A descriptor path is judged by the kernel's name for the open descriptor,
    and an unresolvable one is refused outright — the guard must never answer
    "not sensitive" for a file it cannot identify. The one exemption is the
    gateway's own voice-runtime snapshot staging (see
    :func:`_under_voice_runtime_root`), on BOTH branches: POSIX consumers hold
    a ``/dev/fd`` path, while Windows consumers hold the snapshot NAME (the
    open handle blocks rename/delete there). The membership test resolves the
    real path first, so a link planted under the root resolves outside it and
    is judged as whatever it points at.
    """
    from kiro_crew.security import is_sensitive_path

    fd = _dev_fd_number(audio_path)
    if fd is not None:
        real = pinned_fs.fd_real_path(fd)
        if real is None:
            return True
    else:
        real = audio_path
    if _under_voice_runtime_root(real):
        return False
    return is_sensitive_path(real)


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
                f"dependencies ({install_hint('voice-aws')})."
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
    ext = _input_suffix(audio_path)
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
        logger.error("AWS Transcribe not available: %s", install_hint("voice-aws"))
        return None
    try:
        TranscribeStreamingClient, TranscriptCollector = await asyncio.to_thread(
            _load_aws_transcribe_components
        )
    except ImportError:
        logger.error("AWS Transcribe not available: %s", install_hint("voice-aws"))
        return None

    region = stt_config.transcribe_region
    tmp_ogg = None
    actual_path = audio_path
    if ext in (".webm",):
        try:
            # BEFORE the decoder handle is resolved: a refusal here must not
            # leak the authenticated FFmpeg descriptor the seam would own.
            demux_args = _forced_demuxer_args(audio_path)
        except OSError:
            logger.exception("Could not resolve a demuxer to remux %s", audio_path)
            return None
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
            try:
                proc = await _create_ffmpeg_subprocess(
                    ffmpeg_bin,
                    "-y",
                    *demux_args,
                    "-i",
                    audio_path,
                    "-c:a",
                    "copy",
                    tmp_ogg,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.communicate()
                    raise
                if proc.returncode != 0:
                    tail = stderr.decode(errors="replace").strip()[-500:] if stderr else ""
                    raise RuntimeError(f"ffmpeg {_describe_ffmpeg_exit(proc.returncode, tail)}")
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
        finally:
            # The authenticated handle must outlive the spawn (#8918): every
            # branch above has already reaped the child (``communicate`` on
            # success and on a nonzero exit, kill-and-reap on timeout and on
            # cancellation), so the staged image can be released now. The
            # ``finally`` makes the close unconditional — the staged 0700
            # directory must never leak. ``preserve_active_exception`` is set
            # only while an exception is genuinely in flight, so a cleanup
            # failure never masks the original error and a cancellation landing
            # on the close await of a success path still propagates.
            try:
                await _close_ffmpeg_for_execution(
                    ffmpeg_bin,
                    preserve_active_exception=sys.exc_info()[1] is not None,
                )
            except BaseException:
                # This await is the only suspension point between the remux
                # child exiting and ``actual_path`` taking ownership of the
                # temp. A cancellation landing exactly here (it can only raise
                # on the no-exception-in-flight path) would otherwise propagate
                # with ``tmp_ogg`` still on disk; the failure branches already
                # unlinked, and ``_unlink_if_exists`` tolerates that.
                if tmp_ogg:
                    try:
                        _unlink_if_exists(tmp_ogg)
                    except OSError:
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
            language_code=stt_config.effective_language_code,
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


def batch_duration_cap_secs(stt_config=None) -> int | None:  # type: ignore[no-untyped-def]
    """The longest recording the ACTIVE provider transcribes whole, or None.

    The local recogniser truncates: both of its decode paths stop at
    ``_MAX_AUDIO_SECS`` (the WAV reader caps ``readframes``, the ffmpeg transcode
    passes ``-t``), and neither reports that it did. The Apple lane's
    to-native conversion is bounded the same way (``-t`` on the remux — an
    unbounded conversion of a large low-bitrate input could exhaust the temp
    volume, GPT review r23), so it shares the ceiling. AWS Transcribe refuses
    an oversized payload outright (a loud ``None``), so for it there is no
    silent ceiling to guard. Callers that must not dispatch a truncated
    transcript — the meetings import route — ask here which ceiling applies
    and refuse longer input BEFORE transcribing. BLOCKING when *stt_config*
    is None (reads config).
    """
    if stt_config is None:
        stt_config = _load_stt_config()
    if stt_config.provider == "transcribe":
        return None
    return _MAX_AUDIO_SECS


def provider_splits_oversized(stt_config) -> bool:  # type: ignore[no-untyped-def]
    """Whether an over-cap recording should be SPLIT rather than refused.

    Only the local recogniser both HAS a ceiling and TRUNCATES SILENTLY past it
    (both decode paths stop at ``_MAX_AUDIO_SECS`` and say nothing), so it is the
    one provider where auto-splitting turns silent data loss into a transparent
    success. The Apple lane also has the ceiling but FAILS LOUDLY at it
    (``_to_native_audio`` raises ``RecordingTooLongError``), so it needs no split
    and must not get one: its Swift helper runs in the ``mode="strict"`` sandbox,
    which masks the ``run/voice-runtime`` leaf the import stages its segments
    under (``sandbox.py``), so a segment WAV handed to the helper by name is
    unreadable and every over-cap Apple import would 502. AWS Transcribe has no
    silent ceiling at all (``batch_duration_cap_secs`` is None for it). So the
    split is local-only, by design: segmentation
    only ever triggers where the ceiling exists (AWS/Apple providers fail loudly
    and need no split). Takes the caller's config snapshot as a REQUIRED
    argument (the one production caller always has it in hand, from the same
    snapshot the readiness/cap/transcribe calls share); it never reads config
    itself, so there is no window for the three answers to describe different
    providers.
    """
    return stt_config.provider not in ("transcribe", "apple")


def _wav_duration_secs(audio_path: str) -> float | None:
    """Exact duration from a WAV header, or None when it is not a readable WAV.

    Header math only — no sample data is read — so this works for any rate or
    width, including files :func:`_pcm_from_wav` would hand to ffmpeg. BLOCKING.
    """
    try:
        with wave.open(audio_path, "rb") as wav:
            rate = wav.getframerate()
            if rate <= 0:
                return None
            return wav.getnframes() / rate
    except (OSError, EOFError, wave.Error):
        return None


_PROGRESS_OUT_TIME_RE = re.compile(rb"^out_time_us=(\d+)", re.MULTILINE)


async def audio_exceeds_secs(
    audio_path: str, max_secs: int, *, timeout_secs: int = 300
) -> bool | None:
    """Whether the recording at *audio_path* is longer than *max_secs*.

    True/False when the answer is known, None when it cannot be determined.

    WAV files are answered exactly from the header. Everything else is answered
    by the same decoder that will transcribe it: a null decode bounded at
    ``max_secs`` plus one second (``-t``), reading the decoded timestamp from
    ffmpeg's ``-progress`` stream. Decoding — not metadata — is deliberate: the
    dashboard's own recordings are MediaRecorder webm, whose header carries no
    duration at all, so a metadata probe would answer None for exactly the files
    users are most likely to import. The ``-t`` bound keeps the probe's cost
    proportional to the cap, not to the file.

    A None is honest, not fail-open in disguise, ONLY while the caller gives the
    probe at least the timeout the transcode itself will get (callers with an
    ``stt_config`` in hand pass ``stt_config.timeout_secs``; the default matches
    the config default). The probe decodes at most ``max_secs + 1`` seconds — a
    strict subset of the transcode's work — so under an aligned budget every
    None cause leads to a loud downstream failure: an undecodable file fails the
    transcode the same way, and a host slow enough to time the probe out times
    the strictly-larger transcode out too. A SHORTER probe budget would reopen
    the gap where the probe gives up but the transcode "succeeds" truncated —
    silent data loss on exactly the over-cap files this guard exists to catch.
    """
    duration = await asyncio.to_thread(_wav_duration_secs, audio_path)
    if duration is not None:
        return duration > max_secs
    try:
        # BEFORE the decoder handle is resolved: a refusal here must not leak
        # the authenticated FFmpeg descriptor the seam would otherwise own.
        demux_args = _forced_demuxer_args(audio_path)
    except OSError:
        logger.exception("Could not resolve a demuxer to probe %s", audio_path)
        return None
    ffmpeg_bin = await _resolve_ffmpeg_for_execution()
    if not ffmpeg_bin:
        return None
    try:
        try:
            proc = await _create_ffmpeg_subprocess(
                ffmpeg_bin,
                "-v",
                "error",
                "-nostdin",
                "-progress",
                "pipe:1",
                *demux_args,
                "-i",
                audio_path,
                "-vn",
                # One second PAST the cap: the probe only needs to know whether the
                # recording crosses it, so decoding further would be pure waste.
                "-t",
                str(max_secs + 1),
                "-f",
                "null",
                "-",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            logger.exception("Could not run ffmpeg (%s) to probe %s", ffmpeg_bin, audio_path)
            return None
        try:
            stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_secs)
        except asyncio.TimeoutError:
            await _kill_and_reap(proc)
            logger.error(
                "ffmpeg duration probe of %s timed out after %ds", audio_path, timeout_secs
            )
            return None
        except BaseException:
            # CancelledError is a BaseException; stop AND reap the child so an
            # abandoned request does not leak a decoder process.
            await _kill_and_reap(proc)
            raise
        if proc.returncode != 0:
            return None
        matches = _PROGRESS_OUT_TIME_RE.findall(stdout or b"")
        if not matches:
            return None
        return int(matches[-1]) / 1_000_000 > max_secs
    finally:
        # The authenticated handle must outlive the spawn (#8918): every path
        # reaching this ``finally`` has already reaped the child (``communicate``
        # on success and nonzero exit, kill-and-reap on timeout and
        # cancellation) or never spawned one, so the staged image can be
        # released now — off the loop, like every sibling spawn site, instead
        # of by ``__del__`` running the blocking close on the gateway loop.
        await _close_ffmpeg_for_execution(
            ffmpeg_bin,
            preserve_active_exception=sys.exc_info()[1] is not None,
        )


# ---------------------------------------------------------------------------
# Splitting an over-cap recording into transcribable segments
# ---------------------------------------------------------------------------

#: How far back from a target cut a silence is allowed to sit and still be
#: used. A recording is cut at the last silence BEFORE each cap multiple, but
#: only if that silence is within this window — otherwise the segment would be
#: far shorter than the cap and the split would need many more passes than the
#: recording's length warrants. 30s at a 3600s cap means a segment is at worst
#: 0.8% shorter than the cap; a recording whose only silence sits further back
#: than this is cut hard at the cap instead (see ``_choose_segment_cuts``).
_SILENCE_SEARCH_WINDOW_SECS = 30

#: ``silencedetect`` threshold. -30 dBFS for at least 0.3s is quiet enough to be
#: a real pause between utterances rather than a breath, and short enough to
#: catch the gaps a meeting recording actually has. These are the boundary rule,
#: not a tuning knob a caller passes, so they live here as constants.
_SILENCE_NOISE_DBFS = "-30dB"
_SILENCE_MIN_SECS = 0.3

_SILENCE_END_RE = re.compile(rb"silence_end:\s*([0-9]+(?:\.[0-9]+)?)")

#: Hard ceiling on how many silence points the scan retains. A cut is chosen per
#: cap-sized window, so even a many-hour recording needs only a handful; a few
#: thousand covers any recording a human would import with margin to spare. The
#: ceiling exists to bound MEMORY, not to shape the split: ffmpeg's stderr is read
#: incrementally (not buffered whole by ``communicate``) and only the parsed
#: ``silence_end`` floats are kept, so a pathological low-bitrate, pause-dense file
#: -- ~149h at ~8 kbps would emit ~1.5M events -- cannot buffer hundreds of MiB and
#: OOM/hard-exit the gateway (GPT review, security-class). Crossing the ceiling
#: means the input is pathological, so the scan refuses (returns None) and the
#: import is refused rather than risking the crash.
_MAX_SILENCE_POINTS = 100_000

#: Hard ceiling on how many segments one recording may be split into. A real
#: recording under the import size cap cannot exceed a knowable duration, so a
#: decoded duration implying more than this many cap-sized segments is not a real
#: recording -- it is a crafted container whose terminal PTS (ffmpeg
#: ``out_time_us``) was authored far into the future. ``_choose_segment_cuts``
#: appends one cut per cap-sized window in a single synchronous loop, so an
#: unbounded duration would allocate billions of cuts and freeze/OOM the gateway
#: before any caller check runs (GPT review, security-class). The caller refuses
#: (returns None) rather than build the cuts when the duration crosses this
#: ceiling. 512 covers ~21 days at the 1-hour cap -- far past any real meeting,
#: and far past what the 512 MiB import size cap can hold at any real bitrate.
_MAX_SEGMENTS = 512


async def _drain_progress_and_silence(
    proc: Any, *, timeout_secs: int
) -> "tuple[list[float], float] | None":
    """Read ffmpeg's stdout (-progress) and stderr (silencedetect) INCREMENTALLY.

    ``communicate()`` buffers both whole streams in memory; a pathological
    pause-dense recording could make stderr hundreds of MiB and OOM the gateway
    (GPT review, security-class). This reads both streams line by line and keeps
    only the parsed data -- the latest ``out_time_us`` and up to
    :data:`_MAX_SILENCE_POINTS` ``silence_end`` floats -- so memory is bounded by
    the ceiling, not by the input. Returns ``(silence_ends, duration)``, or None
    when the duration is unreadable or the silence ceiling is crossed (a
    pathological input the caller must refuse rather than risk crashing on).
    Reaps nothing itself: the caller owns kill/close on timeout and cancellation.
    """
    silence_ends: list[float] = []
    last_out_time: int | None = None
    overflowed = False

    #: Bytes kept across chunk reads so a token split by a chunk boundary is not
    #: missed. Longer than any ``out_time_us=<digits>`` or ``silence_end: <float>``
    #: token ffmpeg emits. Reading with ``read(n)`` instead of ``readline()`` is the
    #: point: ``StreamReader.readline`` raises ``ValueError`` on a line past its
    #: 64 KiB limit (GPT review, security-class -- a >64 KiB no-newline stderr line
    #: would otherwise escape as an unhandled 500), while ``read`` has no
    #: line-length limit. A partial trailing fragment longer than this carry cannot
    #: contain a whole token that also straddles the boundary, so trimming to it is
    #: safe and keeps memory bounded against a pathological no-newline line.
    _CARRY = 256

    async def _read_stream(reader: Any, on_match: Any, pattern: Any) -> None:
        if reader is None:
            return
        pending = b""  # the trailing partial line carried to the next chunk
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            data = pending + chunk
            # Scan complete lines; keep only the trailing partial for next time so a
            # token split across a chunk boundary is still matched whole. Each line
            # is scanned exactly once, so an appending consumer never double-counts.
            newline = data.rfind(b"\n")
            if newline == -1:
                # No line terminator yet: keep only a bounded tail. A fragment
                # longer than the carry cannot hold a token that will still be
                # completed by later bytes, so discarding its head is lossless and
                # caps memory against a giant no-newline line.
                pending = data[-_CARRY:] if len(data) > _CARRY else data
                continue
            for m in pattern.finditer(data[: newline + 1]):
                on_match(m)
            pending = data[newline + 1 :]
            if len(pending) > _CARRY:
                pending = pending[-_CARRY:]
        if pending:
            for m in pattern.finditer(pending):
                on_match(m)

    def _on_silence(m: Any) -> None:
        nonlocal overflowed
        if len(silence_ends) >= _MAX_SILENCE_POINTS:
            overflowed = True
            return
        silence_ends.append(float(m.group(1)))

    def _on_progress(m: Any) -> None:
        nonlocal last_out_time
        last_out_time = int(m.group(1))

    await asyncio.wait_for(
        asyncio.gather(
            _read_stream(proc.stdout, _on_progress, _PROGRESS_OUT_TIME_RE),
            _read_stream(proc.stderr, _on_silence, _SILENCE_END_RE),
        ),
        timeout=timeout_secs,
    )
    await proc.wait()
    if proc.returncode != 0:
        return None
    if last_out_time is None:
        return None
    if overflowed:
        logger.error(
            "ffmpeg silence scan produced more than %d points; refusing", _MAX_SILENCE_POINTS
        )
        return None
    return silence_ends, last_out_time / 1_000_000


#: Bytes of ffmpeg stderr retained for an error message. Only a short tail is
#: logged, so the reader keeps at most this much and discards the rest -- ffmpeg
#: can emit a per-frame warning line, so reading the whole stream with
#: ``communicate()`` would buffer unbounded memory on a long/pathological input
#: (GPT review, security-class: same class as the silence scan).
_STDERR_TAIL_BYTES = 8192


async def _drain_stderr_tail(proc: Any, *, timeout_secs: int) -> bytes:
    """Drain ffmpeg's stderr INCREMENTALLY, retaining only the last
    :data:`_STDERR_TAIL_BYTES`, then reap the child.

    ``communicate()`` buffers all of stderr; a segment extract of a long input
    could make that hundreds of MiB and OOM the gateway. This keeps a bounded
    tail (all the error log needs) and discards the rest as it arrives, so the
    pipe never blocks the child and memory stays bounded. Returns the tail;
    the caller reads ``proc.returncode`` after this returns.
    """
    tail = bytearray()
    if proc.stderr is not None:

        async def _read() -> None:
            while True:
                chunk = await proc.stderr.read(65536)
                if not chunk:
                    break
                tail.extend(chunk)
                if len(tail) > _STDERR_TAIL_BYTES:
                    del tail[: len(tail) - _STDERR_TAIL_BYTES]

        await asyncio.wait_for(_read(), timeout=timeout_secs)
    await proc.wait()
    return bytes(tail)


def _choose_segment_cuts(
    duration_secs: float, cap_secs: int, silence_ends: "list[float]"
) -> "list[float]":
    """Where to cut a *duration_secs* recording into segments each <= *cap_secs*.

    Returns the INTERIOR cut points in seconds, ascending — the segment
    boundaries between 0 and *duration_secs*, excluding both ends. A recording
    already within the cap returns ``[]`` (one segment, no split).

    Each cut is placed at the last ``silence_end`` at or before the running
    target (``previous_cut + cap_secs``) and no earlier than the target minus
    :data:`_SILENCE_SEARCH_WINDOW_SECS`, so a word straddling the target is not
    split — the cut lands in the pause after the previous utterance. When the
    window holds no silence (continuous speech across the whole window), the cut
    falls HARD on the target: a rare, documented seam that may split one word,
    which is still strictly better than refusing the whole import. The target
    always advances by a real amount, so the loop always terminates and every
    segment is <= *cap_secs*.

    Pure function, no IO: the boundary rules are the interesting part and are
    unit-tested directly, the way ``audio.split_transcript`` is.
    """
    cuts: list[float] = []
    ordered = sorted(silence_ends)
    start = 0.0
    while duration_secs - start > cap_secs:
        target = start + cap_secs
        floor = target - _SILENCE_SEARCH_WINDOW_SECS
        # The last silence in ``(floor, target]``. A silence exactly at the
        # target counts; one at or before ``start`` never does (it would make a
        # zero-length or backwards segment).
        candidates = [s for s in ordered if floor < s <= target and s > start]
        cut = candidates[-1] if candidates else target
        cuts.append(cut)
        start = cut
    return cuts


async def _detect_silence_ends(
    audio_path: str, *, timeout_secs: int
) -> "tuple[list[float], float] | None":
    """Silence-end timestamps and the decoded duration, or None when unknown.

    One decode pass through ffmpeg's ``silencedetect`` filter, reading
    ``silence_end`` events from stderr and the final decoded timestamp from the
    ``-progress`` stream. Both streams are drained INCREMENTALLY
    (:func:`_drain_progress_and_silence`), never buffered whole, so a
    pause-dense recording cannot make stderr hundreds of MiB and OOM the gateway
    (GPT review, security-class); only the parsed floats are kept, capped at
    :data:`_MAX_SILENCE_POINTS`. None on any failure the caller must treat as
    "cannot split safely" -- an undecodable file, a decoder that is unavailable,
    a timeout, a nonzero exit, or a pathological input past the silence ceiling
    -- so the caller refuses the import rather than proceeding to a truncating
    decode. Mirrors the spawn shape of :func:`audio_exceeds_secs`: forced
    demuxer, local-protocol pin (inherited from :func:`_create_ffmpeg_subprocess`),
    authenticated-handle lifetime, kill-and-reap on timeout and cancellation.
    """
    try:
        demux_args = _forced_demuxer_args(audio_path)
    except OSError:
        logger.exception("Could not resolve a demuxer to scan %s for silence", audio_path)
        return None
    ffmpeg_bin = await _resolve_ffmpeg_for_execution()
    if not ffmpeg_bin:
        return None
    try:
        try:
            proc = await _create_ffmpeg_subprocess(
                ffmpeg_bin,
                "-nostdin",
                # -hide_banner drops ffmpeg's input-metadata dump from stderr, so a
                # crafted container's oversized tag value cannot appear there;
                # silencedetect logs at info level, which is kept. Defence in depth
                # for the bounded reader below, not the primary guard.
                "-hide_banner",
                "-progress",
                "pipe:1",
                *demux_args,
                "-i",
                audio_path,
                "-vn",
                "-af",
                f"silencedetect=noise={_SILENCE_NOISE_DBFS}:d={_SILENCE_MIN_SECS}",
                "-f",
                "null",
                "-",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError:
            logger.exception("Could not run ffmpeg (%s) to scan %s", ffmpeg_bin, audio_path)
            return None
        try:
            result = await _drain_progress_and_silence(proc, timeout_secs=timeout_secs)
        except asyncio.TimeoutError:
            await _kill_and_reap(proc)
            logger.error("ffmpeg silence scan of %s timed out after %ds", audio_path, timeout_secs)
            return None
        except BaseException:
            await _kill_and_reap(proc)
            raise
        return result
    finally:
        await _close_ffmpeg_for_execution(
            ffmpeg_bin,
            preserve_active_exception=sys.exc_info()[1] is not None,
        )


async def _extract_segment(
    audio_path: str,
    start_secs: float,
    end_secs: "float | None",
    out_path: str,
    *,
    timeout_secs: int,
) -> bool:
    """Decode ``[start_secs, end_secs)`` of *audio_path* to a 16 kHz mono WAV.

    ``end_secs`` of None means "to the end of the recording" (the last segment).
    Returns True on a clean extraction, False on any failure — the caller turns
    a False into a whole-import refusal, never a partial transcript.

    ``-ss`` before ``-i`` is an INPUT seek (ffmpeg skips to the start without
    decoding the skipped span), so extracting the k-th segment costs one
    segment's worth of decode, not k. The output targets the recogniser's own
    format (16 kHz mono ``pcm_s16le``) exactly as :func:`_pcm_via_ffmpeg` does,
    so each segment file is a plain WAV the transcriber's fast path reads with no
    second transcode.
    """
    try:
        demux_args = _forced_demuxer_args(audio_path)
    except OSError:
        logger.exception("Could not resolve a demuxer to extract a segment of %s", audio_path)
        return False
    ffmpeg_bin = await _resolve_ffmpeg_for_execution()
    if not ffmpeg_bin:
        return False
    # ``-ss`` is an INPUT option (before ``-i``) so the skip is not decoded;
    # ``-t`` bounds the segment length. A None end means the final segment,
    # which runs to end-of-file with no ``-t``.
    seek_args = ("-ss", f"{start_secs:.3f}")
    length_args = () if end_secs is None else ("-t", f"{end_secs - start_secs:.3f}")
    try:
        try:
            proc = await _create_ffmpeg_subprocess(
                ffmpeg_bin,
                "-y",
                "-nostdin",
                *seek_args,
                *demux_args,
                "-i",
                audio_path,
                *length_args,
                "-ar",
                str(stt.SAMPLE_RATE_HZ),
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                out_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError:
            logger.exception(
                "Could not run ffmpeg (%s) to extract a segment of %s", ffmpeg_bin, audio_path
            )
            return False
        try:
            stderr_tail = await _drain_stderr_tail(proc, timeout_secs=timeout_secs)
        except asyncio.TimeoutError:
            await _kill_and_reap(proc)
            logger.error(
                "ffmpeg segment extract of %s timed out after %ds", audio_path, timeout_secs
            )
            return False
        except BaseException:
            await _kill_and_reap(proc)
            raise
        if proc.returncode != 0:
            tail = stderr_tail.decode(errors="replace").strip()[-500:] if stderr_tail else ""
            logger.error(
                "ffmpeg %s extracting a segment of %s",
                _describe_ffmpeg_exit(proc.returncode, tail),
                audio_path,
            )
            return False
        return True
    finally:
        await _close_ffmpeg_for_execution(
            ffmpeg_bin,
            preserve_active_exception=sys.exc_info()[1] is not None,
        )


async def transcribe_oversized_in_segments(
    audio_path: str,
    cap_secs: int,
    segment_dir: str,
    stt_config,  # type: ignore[no-untyped-def]
) -> "str | None":
    """Transcribe a recording longer than *cap_secs* by splitting it first.

    Returns the stitched transcript, or None when the recording cannot be split
    or any segment fails to decode or transcribe — the same None-means-failure
    contract as :func:`transcribe_audio`, so the caller reports one clean failure
    rather than a partial import.

    The split is on the AUDIO, not the transcript: one ffmpeg pass locates the
    pauses (:func:`_detect_silence_ends`), a pure function picks a cut at the
    last pause before each cap multiple (:func:`_choose_segment_cuts`), and each
    segment is decoded to its own WAV (:func:`_extract_segment`) and transcribed
    through the ordinary :func:`transcribe_audio` — so every segment gets the same
    provider dispatch, sensitive-path guard and redaction a whole recording does.
    Segments are NON-OVERLAPPING (cut in the silence between utterances), so the
    stitch is a plain space join with nothing to de-duplicate; the joined text is
    one paragraph, so the meetings import route's ``split_transcript`` sentence-
    splits it into utterances exactly as it does for a whole recording, and the
    seams are invisible.

    *segment_dir* is a caller-owned directory the segment WAVs are written into;
    the caller removes it. It must be a trusted directory the transcriber is
    allowed to read (the meetings route stages it under the voice-runtime root,
    which ``transcribe_audio``'s sensitive-path guard exempts).

    This does NOT re-check the stitched result against any total-size ceiling:
    that stays the caller's job (the import route's ``split_transcript`` still
    enforces ``MAX_IMPORT_LINES``/``MAX_TRANSCRIPT_CHARS``), so splitting is never
    a way around the total-size refusal.
    """
    scanned = await _detect_silence_ends(audio_path, timeout_secs=stt_config.timeout_secs)
    if scanned is None:
        logger.error("Could not scan %s for split boundaries", audio_path)
        return None
    silence_ends, duration = scanned
    # Bound the segment count BEFORE building cuts. ``duration`` is ffmpeg's
    # decoded terminal PTS, which a crafted container can author far into the
    # future; ``_choose_segment_cuts`` appends one cut per cap-sized window in a
    # single synchronous loop, so an unbounded duration would allocate billions of
    # cuts and freeze/OOM the gateway before any later check runs (GPT review,
    # security-class). A real recording under the import size cap cannot span more
    # than ``_MAX_SEGMENTS`` cap-sized windows, so a duration past that ceiling is
    # not a real recording -- refuse rather than build the cuts.
    if duration <= 0 or duration > _MAX_SEGMENTS * cap_secs:
        logger.error(
            "%s reports an implausible duration (%.1fs > %d segments x %ds); refusing",
            audio_path,
            duration,
            _MAX_SEGMENTS,
            cap_secs,
        )
        return None
    cuts = _choose_segment_cuts(duration, cap_secs, silence_ends)
    # Boundaries as [start, end) pairs, end=None on the last (runs to EOF).
    bounds: list[tuple[float, float | None]] = []
    prev = 0.0
    for cut in cuts:
        bounds.append((prev, cut))
        prev = cut
    bounds.append((prev, None))

    transcripts: list[str] = []
    for index, (seg_start, seg_end) in enumerate(bounds):
        seg_path = os.path.join(segment_dir, f"segment-{index:03d}.wav")
        ok = await _extract_segment(
            audio_path, seg_start, seg_end, seg_path, timeout_secs=stt_config.timeout_secs
        )
        if not ok:
            logger.error(
                "Could not extract segment %d of %s; refusing the whole import", index, audio_path
            )
            return None
        try:
            text = await transcribe_audio(seg_path, stt_config)
        finally:
            await asyncio.to_thread(_unlink_if_exists, seg_path)
        if not text:
            # A falsy result refuses the WHOLE import (GPT + Opus review, both
            # BLOCKING). transcribe_audio returns None for a genuine recogniser
            # failure -- a per-segment decode error, a timeout, the shared
            # recogniser singleton being swapped mid-import -- as well as for a
            # legitimately silent segment, and the two are INDISTINGUISHABLE here
            # (``return text or None``). _extract_segment returning True only
            # proves ffmpeg carved the WAV, not that the recogniser succeeded on
            # it. So skipping a falsy segment would silently drop a real spoken
            # span and still answer 200 -- the exact silent-partial data loss this
            # feature exists to prevent, one level down. Refusing the whole import
            # is the safe answer: distinguishing decoded-empty from
            # recogniser-failure would require a new failure flag on
            # transcribe_audio's contract (8 callers), which is out of scope for
            # this route-level change. A recording with a genuinely silent
            # cap-sized stretch is refused loudly rather than imported with a gap.
            logger.error(
                "Segment %d of %s produced no transcript; refusing the whole import",
                index,
                audio_path,
            )
            return None
        transcripts.append(text)
    # Join with a SPACE, not a newline (GPT review): a segment transcript from the
    # local/Apple recognisers is one whitespace-joined paragraph with no internal
    # newlines (``stt/engine.py`` joins whisper segments with " "). A newline join
    # would hand ``split_transcript`` N>1 lines, which makes it treat each whole
    # segment as ONE line (tier 1) and only hard-wrap it at ``max_chars`` -- so a
    # segment would dispatch as 4000-char chunks instead of utterances, worse line
    # structure than the un-split path produces. A space join keeps the stitched
    # text as one paragraph, so ``split_transcript`` sentence-splits it into
    # utterances (tier 2) exactly as it does for a whole recording; any internal
    # newline a segment DOES carry (a future recogniser with its own line
    # structure) is preserved across the join and still read as tier-1 structure.
    return " ".join(transcripts)


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
            frames_total = min(wav.getnframes(), _MAX_AUDIO_SECS * stt.SAMPLE_RATE_HZ)
            if channels == 1:
                return stt.pcm_from_int16(wav.readframes(frames_total))
            # Fold to mono in BYTE-bounded slices. A whole-file read would hold
            # the interleaved int16 buffer AND its float32 conversion at once —
            # around 1.5 GiB for a four-channel hour, enough to OOM the
            # gateway on an input the 512 MiB import cap admits (GPT review
            # r23). And the bound must be BYTES, not seconds: a frame is
            # ``channels * 2`` bytes, so a fixed frame count lets the channel
            # count scale the transient without limit — a valid 256-channel
            # minute under the same cap would make a "60-second" slice
            # allocate ~0.5 GiB raw plus its float32 conversion (GPT review
            # r34). 8 MiB of raw int16 per slice keeps the transient under a
            # few tens of MiB for ANY channel count, while the result stays
            # the same: the per-frame mean is local to each frame, and
            # ``readframes`` counts whole frames, so no frame is ever split
            # across slices.
            chunk_frames = max(1, (8 * 1024 * 1024) // (channels * 2))
            folded = []
            remaining = frames_total
            while remaining > 0:
                take = min(chunk_frames, remaining)
                raw = wav.readframes(take)
                if not raw:
                    break
                remaining -= take
                pcm = stt.pcm_from_int16(raw)
                # Drop a final frame the file cut in half before folding
                # channels, so the reshape cannot fail on a truncated
                # recording.
                usable = pcm.size - (pcm.size % channels)
                if usable <= 0:
                    continue
                folded.append(pcm[:usable].reshape(-1, channels).mean(axis=1, dtype=pcm.dtype))
    except (OSError, EOFError, wave.Error):
        # Not a readable PCM WAV (a compressed payload, a truncated header, a
        # mislabelled suffix). ffmpeg reads far more than the stdlib does, so this
        # is a "try the other route", not a failure.
        return None
    if not folded:
        return None
    if len(folded) == 1:
        return folded[0]
    import numpy as np  # runtime import: module-level numpy is typing-only here

    return np.concatenate(folded)


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
    try:
        # BEFORE the decoder handle is resolved: a refusal here must not leak
        # the authenticated FFmpeg descriptor the seam would otherwise own.
        demux_args = _forced_demuxer_args(audio_path)
    except OSError:
        logger.exception("Could not resolve a demuxer to decode %s", audio_path)
        return None
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
                *demux_args,
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
                "ffmpeg %s decoding %s",
                _describe_ffmpeg_exit(proc.returncode, tail),
                audio_path,
            )
            return None
        return await asyncio.to_thread(_pcm_from_wav, tmp_wav)
    finally:
        # Off the loop, and scheduled as its own task BEFORE anything is
        # awaited, so a repeat cancellation landing on an await abandons only
        # the wait while the removal still runs to completion in its worker
        # thread. ``shield`` keeps that cancellation out of the removal task;
        # the exception itself still reaches the awaiter.
        rm = asyncio.ensure_future(asyncio.to_thread(_unlink_if_exists, tmp_wav))
        try:
            # The authenticated handle must outlive the spawn (#8918): every
            # path reaching this ``finally`` has already reaped the child
            # (``communicate`` on success and on a nonzero exit, kill-and-reap
            # on timeout and on cancellation) or never spawned one, so the
            # staged image can be released now. ``preserve_active_exception``
            # is set only while an exception is genuinely in flight, so a
            # cleanup failure never masks the original error and a cancellation
            # landing on the close await of a success path still propagates.
            await _close_ffmpeg_for_execution(
                ffmpeg_bin,
                preserve_active_exception=sys.exc_info()[1] is not None,
            )
        finally:
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
    if _input_suffix(audio_path) in _WAV_SUFFIXES:
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
    code, so ``stt_config.effective_language_code`` supplies BCP-47 (e.g. ``en-US``)
    even when the stored preference is automatic. The helper falls back to another
    installed dialect of the same language before it refuses.

    A supported host needs no model download because the OS ships the assets, so
    a failure here is a real error rather than the missing-model state the local
    recogniser can be in on a first run.
    """
    from kiro_crew import apple_speech

    text, metrics = await apple_speech.transcribe(
        audio_path,
        locale=stt_config.effective_language_code,
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
