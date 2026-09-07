"""The pinned FFmpeg decoder, and a digest-verified store for source installs.

A desktop release carries the ``imageio-ffmpeg`` executable inside its own
interpreter, so it never needs this. A source or Toolbox install carries nothing,
and the only decoder it could previously reach was a system FFmpeg from a fixed
set of package-manager directories -- which on a distribution that ships no
FFmpeg package (Amazon Linux, RHEL without EPEL) does not exist, leaving batch
voice input permanently broken with a shell command as the only remedy.

**The trust anchor is the digest, not the path.** :mod:`kiro_crew.transcribe`
already pins one upstream executable per platform by size and SHA-256 and
re-verifies it on every open, so fetching *those same bytes* into the data home
and running them through the same check adds a location, not a weaker rule. What
would weaken the model is trusting a directory: a user-writable path is never
enough on its own, which is why the candidate directory list this module sits
beside must not grow an entry for the store.

Two pins per platform, and both are load-bearing:

- the WHEEL (filename, URL, size, SHA-256) is what bounds the network fetch, so a
  hostile mirror or a captive-portal HTML body fails before anything is extracted;
- the EXECUTABLE (:data:`PACKAGED_FFMPEG_ARTIFACTS`) is what bounds the member
  taken out of it, and it is the SAME table the runtime authenticates against, so
  the store cannot install bytes the resolver would then refuse.

Exactly one member is ever extracted, matched by its full archive name, and it is
written through a descriptor this process opened -- never ``ZipFile.extract``,
whose member name is attacker-controlled path data.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.atomic_write import replace_with_retry
from kiro_crew.stt.models import (
    SKIP_DOWNLOAD_ENV,
    ModelDownloadError,
    _sha256_file,
    models_dir,
    stream_pinned_payload,
)

logger = logging.getLogger(__name__)

#: Upstream release the pins below are taken from. Stated once so the version and
#: the digests cannot disagree; bumping it means re-deriving every pin.
IMAGEIO_FFMPEG_VERSION = "0.6.0"

#: Directory inside the shared ``models/`` tree that holds a fetched decoder. A
#: sibling of the whisper weights rather than a child, for the reason
#: ``models.models_dir`` gives: two artifact families under one directory can
#: collide on a name, and an operator clearing one should not take the other.
_STORE_DIRNAME = "ffmpeg"

#: Suffix for both staging files (the wheel, and the executable taken out of it).
#: Staged inside the store directory so the finishing rename is same-filesystem
#: and therefore atomic.
_STAGING_SUFFIX = ".part"

#: Read size while copying the archive member out. Same order as the download
#: chunk: large enough that the digest update is not per-page work, small enough
#: that the bound below is checked often.
_EXTRACT_CHUNK_BYTES = 1 << 20

#: Mode for the installed executable. Owner-writable is deliberate -- this file is
#: replaced by a later fetch -- because the digest, not the mode, is what decides
#: whether it runs.
_EXECUTABLE_MODE = 0o755

#: Archive directory holding the platform executable in every imageio-ffmpeg
#: wheel. Joined with the artifact filename to form the ONE member name accepted.
_WHEEL_BINARIES_PREFIX = "imageio_ffmpeg/binaries/"


@dataclass(frozen=True)
class DecoderArtifact:
    """One platform's pinned decoder, and the wheel it is extracted from.

    ``platform_key`` is upstream's own platform spelling, shared with
    ``transcribe._SHIPPED_FFMPEG_PLATFORMS`` so the desktop matrix and this store
    describe platforms in one vocabulary rather than two.

    ``wheel_size_bytes`` is a ceiling on the transfer as well as a pre-check: the
    download refuses the first chunk that would exceed it, so a mirror that keeps
    sending cannot fill the disk before the digest is ever computed.
    """

    platform_key: str
    filename: str
    size_bytes: int
    sha256: str
    wheel_filename: str
    wheel_url: str
    wheel_size_bytes: int
    wheel_sha256: str

    @property
    def member(self) -> str:
        """The single archive member this artifact may be extracted from."""
        return f"{_WHEEL_BINARIES_PREFIX}{self.filename}"


#: The pinned decoders, one per platform the desktop matrix ships (see
#: ``transcribe._SHIPPED_FFMPEG_PLATFORMS``, which the completeness test in
#: test_transcribe.py cross-checks against upstream's own filename table).
#:
#: Executable size and SHA-256 are the bytes the WHEEL publishes: the filename
#: selects the platform artifact, size makes a truncated payload fail cheaply, and
#: the digest is the trust anchor. Desktop build staging is intentionally
#: writable, so path placement cannot establish provenance and does not try to.
#:
#: Wheel URL, size and digest come from PyPI's own metadata for
#: ``imageio-ffmpeg==0.6.0`` and were verified by downloading each wheel and
#: hashing it, then hashing the member it carries against the executable pin
#: beside it. The URLs are content-addressed by upstream (the hash segment in the
#: path), so they are immutable rather than a channel that can be re-pointed.
#:
#: ``windows-i686`` is deliberately absent for the same reason it is absent from
#: the shipped set: no 32-bit Windows target exists in any build workflow.
ARTIFACTS: tuple[DecoderArtifact, ...] = (
    DecoderArtifact(
        platform_key="macos-aarch64",
        filename="ffmpeg-macos-aarch64-v7.1",
        size_bytes=49_368_728,
        sha256="6d175a4743ca50256e89a8cdd731100f9cee33bd79aeea46894d209410dc6617",
        wheel_filename="imageio_ffmpeg-0.6.0-py3-none-macosx_11_0_arm64.whl",
        wheel_url=(
            "https://files.pythonhosted.org/packages/40/5c/"
            "f3d8a657d362cc93b81aab8feda487317da5b5d31c0e1fdfd5e986e55d17/"
            "imageio_ffmpeg-0.6.0-py3-none-macosx_11_0_arm64.whl"
        ),
        wheel_size_bytes=21_113_891,
        wheel_sha256="b1ae3173414b5fc5f538a726c4e48ea97edc0d2cdc11f103afee655c463fa742",
    ),
    DecoderArtifact(
        platform_key="macos-x86_64",
        filename="ffmpeg-macos-x86_64-v7.1",
        size_bytes=75_991_688,
        sha256="4a4a968b98859588e98500ae25973d80a5ca5eed0724222b9f76360dcb72a001",
        wheel_filename=("imageio_ffmpeg-0.6.0-py3-none-macosx_10_9_intel.macosx_10_9_x86_64.whl"),
        wheel_url=(
            "https://files.pythonhosted.org/packages/da/58/"
            "87ef68ac83f4c7690961bce288fd8e382bc5f1513860fc7f90a9c1c1c6bf/"
            "imageio_ffmpeg-0.6.0-py3-none-macosx_10_9_intel.macosx_10_9_x86_64.whl"
        ),
        wheel_size_bytes=24_932_969,
        wheel_sha256="9d2baaf867088508d4a3458e61eeb30e945c4ad8016025545f66c4b5aaef0a61",
    ),
    DecoderArtifact(
        platform_key="linux-x86_64",
        filename="ffmpeg-linux-x86_64-v7.0.2",
        size_bytes=79_826_272,
        sha256="e7e7fb30477f717e6f55f9180a70386c62677ef8a4d4d1a5d948f4098aa3eb99",
        wheel_filename="imageio_ffmpeg-0.6.0-py3-none-manylinux2014_x86_64.whl",
        wheel_url=(
            "https://files.pythonhosted.org/packages/a0/2d/"
            "43c8522a2038e9d0e7dbdf3a61195ecc31ca576fb1527a528c877e87d973/"
            "imageio_ffmpeg-0.6.0-py3-none-manylinux2014_x86_64.whl"
        ),
        wheel_size_bytes=29_498_237,
        wheel_sha256="c7e46fcec401dd990405049d2e2f475e2b397779df2519b544b8aab515195282",
    ),
    DecoderArtifact(
        platform_key="linux-aarch64",
        filename="ffmpeg-linux-aarch64-v7.0.2",
        size_bytes=51_134_160,
        sha256="6bb182d0d75d23028db82e9e4f723ca69b853d055698486e6984ddb2c06fb8ce",
        wheel_filename="imageio_ffmpeg-0.6.0-py3-none-manylinux2014_aarch64.whl",
        wheel_url=(
            "https://files.pythonhosted.org/packages/33/e7/"
            "1925bfbc563c39c1d2e82501d8372734a5c725e53ac3b31b4c2d081e895b/"
            "imageio_ffmpeg-0.6.0-py3-none-manylinux2014_aarch64.whl"
        ),
        wheel_size_bytes=25_632_706,
        wheel_sha256="1d47bebd83d2c5fc770720d211855f208af8a596c82d17730aa51e815cdee6dc",
    ),
    DecoderArtifact(
        platform_key="windows-x86_64",
        filename="ffmpeg-win-x86_64-v7.1.exe",
        size_bytes=87_638_016,
        sha256="2ce797a0f88d7f067180338fb227f7b1928ea727bd9a4d7a1d022f7c52af71a3",
        wheel_filename="imageio_ffmpeg-0.6.0-py3-none-win_amd64.whl",
        wheel_url=(
            "https://files.pythonhosted.org/packages/2c/c6/"
            "fa760e12a2483469e2bf5058c5faff664acf66cadb4df2ad6205b016a73d/"
            "imageio_ffmpeg-0.6.0-py3-none-win_amd64.whl"
        ),
        wheel_size_bytes=31_246_824,
        wheel_sha256="02fa47c83703c37df6bfe4896aab339013f62bf02c5ebf2dce6da56af04ffc0a",
    ),
)

#: Filename to ``(size, sha256)``, the form the runtime authenticates against.
#: DERIVED from :data:`ARTIFACTS` rather than restated, so the store and
#: ``transcribe`` cannot pin different bytes for one filename -- the failure that
#: would produce is a decoder the store installs and the resolver then refuses,
#: with nothing to show which of the two copies is wrong.
PACKAGED_FFMPEG_ARTIFACTS: dict[str, tuple[int, str]] = {
    artifact.filename: (artifact.size_bytes, artifact.sha256) for artifact in ARTIFACTS
}

_BY_PLATFORM: dict[str, DecoderArtifact] = {a.platform_key: a for a in ARTIFACTS}

#: ``platform.system()`` to upstream's platform prefix. A system absent here (BSD,
#: SunOS, Android) has no pinned decoder and is reported unsupported rather than
#: guessed at.
_SYSTEM_PREFIX: dict[str, str] = {
    "Darwin": "macos",
    "Linux": "linux",
    "Windows": "windows",
}

#: ``platform.machine()`` spellings that mean the same ISA. The value differs by
#: OS for identical hardware -- Windows reports ``AMD64`` and macOS ``arm64``
#: where Linux reports ``x86_64`` and ``aarch64`` -- so matching on the raw string
#: would leave two of the five shipped platforms unable to find their own pin.
_MACHINE_SUFFIX: dict[str, str] = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "x64": "x86_64",
    "i686": "i686",
    "aarch64": "aarch64",
    "arm64": "aarch64",
}

#: Stages of :attr:`DecoderStore.status`. ``unsupported`` is terminal and distinct
#: from ``failed`` on purpose: nothing the user or an agent does will make a
#: pinned artifact exist for this platform, so the UI must offer the manual
#: system-decoder route there instead of a retry.
STAGE_IDLE = "idle"
STAGE_DOWNLOADING = "downloading"
STAGE_READY = "ready"
STAGE_FAILED = "failed"
STAGE_UNSUPPORTED = "unsupported"

#: Machine-readable failure reasons. The dashboard renders localised text and
#: cannot key off an English sentence, so these are the contract and
#: ``error_detail`` is advisory -- the same split the STT endpoints already use.
CODE_UNSUPPORTED = "decoder_unsupported_platform"
CODE_DOWNLOAD_DISABLED = "decoder_download_disabled"
CODE_WHEEL_UNVERIFIED = "decoder_wheel_unverified"
CODE_MEMBER_MISSING = "decoder_member_missing"
CODE_PAYLOAD_UNVERIFIED = "decoder_payload_unverified"
CODE_INSTALL_FAILED = "decoder_install_failed"


def platform_key(system: str | None = None, machine: str | None = None) -> str:
    """Upstream's platform spelling for *system*/*machine*, or ``""``.

    Empty rather than a guess: an unmapped system or ISA has no pinned artifact,
    and composing a plausible-looking key would turn "this platform is not
    covered" into a lookup miss the caller cannot tell from a typo.
    """
    resolved_system = platform.system() if system is None else system
    resolved_machine = platform.machine() if machine is None else machine
    prefix = _SYSTEM_PREFIX.get(resolved_system)
    suffix = _MACHINE_SUFFIX.get(resolved_machine.lower())
    if not prefix or not suffix:
        return ""
    return f"{prefix}-{suffix}"


def artifact_for(system: str | None = None, machine: str | None = None) -> DecoderArtifact | None:
    """The pinned decoder for *system*/*machine*, or ``None`` when unsupported."""
    key = platform_key(system, machine)
    return _BY_PLATFORM.get(key) if key else None


def store_dir() -> Path:
    """Where a fetched decoder lives. Respects ``KIROCREW_HOME``.

    Derived from ``models.models_dir()`` rather than composed from the data home
    again, so the two directories cannot drift apart if the shared ``models/``
    tree ever moves -- and so this stays under the path the agent's file and shell
    gates already write-protect.
    """
    return models_dir().parent / _STORE_DIRNAME


def installed_path(artifact: DecoderArtifact | None = None) -> Path | None:
    """Where this platform's decoder is installed, or ``None`` when unsupported."""
    resolved = artifact_for() if artifact is None else artifact
    if resolved is None:
        return None
    return store_dir() / resolved.filename


def is_present(artifact: DecoderArtifact) -> bool:
    """Whether *artifact* is on disk at the pinned SIZE.

    Size only, deliberately: this answers "must this be downloaded" and is on the
    path of a UI poll. It is NOT a trust check -- that is the resolver's digest
    verification on every open, and :meth:`DecoderStore._verified_on_disk` before
    a fetch is skipped.
    """
    path = installed_path(artifact)
    if path is None:
        return False
    try:
        return path.stat().st_size == artifact.size_bytes
    except OSError:
        return False


def _extract_pinned_member(archive: Path, artifact: DecoderArtifact, target_dir: Path) -> Path:
    """Copy *artifact*'s one member out of *archive*, verified, into *target_dir*.

    Returns the staging path of the extracted file; the caller renames it into
    place. Raises :class:`~kiro_crew.stt.models.ModelDownloadError` on anything
    that is not exactly the pinned payload.

    ``ZipFile.extract`` is deliberately not used. Its destination is derived from
    the member NAME, which is data inside the archive, so a crafted entry
    (``../``, an absolute path, a Windows drive letter) chooses where the write
    lands. Here the member is looked up by its full name, the name that came back
    is compared against the one asked for, and the bytes are written through a
    descriptor this process created under *target_dir*.
    """
    staging_fd, staging_name = tempfile.mkstemp(
        dir=target_dir, prefix=f"{artifact.filename}.", suffix=_STAGING_SUFFIX
    )
    staging = Path(staging_name)
    try:
        with zipfile.ZipFile(archive) as bundle:
            try:
                info = bundle.getinfo(artifact.member)
            except KeyError as exc:
                raise ModelDownloadError(
                    f"{artifact.filename}: {artifact.wheel_filename} carries no "
                    f"{artifact.member}"
                ) from exc
            # getinfo resolves through a name index, so a second entry with the
            # same name, or a name that only normalises to the one asked for,
            # would be served here. Compare what came back.
            if info.filename != artifact.member or info.is_dir():
                raise ModelDownloadError(
                    f"{artifact.filename}: refusing archive member {info.filename!r}"
                )
            # The declared size is the archive's claim, not the pin, so it is a
            # cheap pre-check rather than the check.
            if info.file_size != artifact.size_bytes:
                raise ModelDownloadError(
                    f"{artifact.filename}: {info.filename} declares {info.file_size} "
                    f"bytes against the pinned {artifact.size_bytes}"
                )
            digest_written = 0
            with bundle.open(info) as member, os.fdopen(staging_fd, "wb") as handle:
                staging_fd = -1  # ownership transferred to the context manager
                while True:
                    chunk = member.read(_EXTRACT_CHUNK_BYTES)
                    if not chunk:
                        break
                    # Bounded BEFORE the write, for the reason the download bounds
                    # its own: a decompressed member's real length is not fixed by
                    # anything in the header, so streaming to EOF first is how a
                    # zip bomb fills the disk.
                    digest_written += len(chunk)
                    if digest_written > artifact.size_bytes:
                        raise ModelDownloadError(
                            f"{artifact.filename}: member exceeds the pinned "
                            f"{artifact.size_bytes} bytes; refusing to keep writing"
                        )
                    handle.write(chunk)
        actual = _sha256_file(staging)
        if digest_written != artifact.size_bytes or actual != artifact.sha256:
            raise ModelDownloadError(
                f"{artifact.filename}: sha256 mismatch (got {actual[:16]}…, "
                f"expected {artifact.sha256[:16]}…)"
            )
        # Executable, because the resolver will spawn it. Through chmod_safe: this
        # runs on Windows too, where os.chmod cannot express the bit at all.
        platform_compat.chmod_safe(staging, _EXECUTABLE_MODE)
        return staging
    except BaseException:
        if staging_fd >= 0:
            os.close(staging_fd)
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            logger.debug("Could not remove staging file %s", staging, exc_info=True)
        raise


def _fetch_blocking(
    artifact: DecoderArtifact,
    on_progress: object = None,
) -> Path:
    """Download, extract and install *artifact*. Blocks; never on the loop.

    The wheel is streamed to a staging file under the store directory and its own
    digest is verified before the archive is opened at all, so a tampered or
    truncated download never reaches the zip parser.
    """
    target_dir = store_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / artifact.filename
    wheel_fd, wheel_name = tempfile.mkstemp(
        dir=target_dir, prefix=f"{artifact.wheel_filename}.", suffix=_STAGING_SUFFIX
    )
    wheel = Path(wheel_name)
    try:
        # The descriptor is adopted FIRST, for the reason models._download_blocking
        # gives: mkstemp hands back an unowned fd, and the cleanup below unlinks a
        # PATH, so an exception raised before the adoption leaks one descriptor per
        # attempt and (on Windows) strands the partial file with it.
        with os.fdopen(wheel_fd, "wb") as handle:
            stream_pinned_payload(
                artifact.wheel_url,
                label=artifact.wheel_filename,
                expected_size=artifact.wheel_size_bytes,
                expected_sha256=artifact.wheel_sha256,
                write=handle.write,
                on_progress=on_progress,  # type: ignore[arg-type]
            )
        staging = _extract_pinned_member(wheel, artifact, target_dir)
    finally:
        try:
            wheel.unlink(missing_ok=True)
        except OSError:
            logger.debug("Could not remove staging wheel %s", wheel, exc_info=True)
    try:
        # replace_with_retry, not os.replace: on Windows the rename fails while any
        # other handle is open on either path, and a freshly written 80 MB
        # executable is exactly what an AV scanner reaches for.
        replace_with_retry(staging, target)
    except BaseException:
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            logger.debug("Could not remove staging file %s", staging, exc_info=True)
        raise
    logger.info("FFmpeg decoder %s verified and installed at %s", artifact.filename, target)
    return target


class DecoderStore:
    """Serialises decoder fetches and exposes their progress.

    One store per process, and one in-flight fetch: a settings panel that pressed
    the button and a model download that finished at the same moment share the
    transfer through ``_lock`` rather than both pulling the same wheel.
    """

    def __init__(self) -> None:
        self._lock: asyncio.Lock | None = None
        self.status: dict[str, object] = {
            "stage": STAGE_IDLE,
            "artifact": "",
            "downloaded_bytes": 0,
            "total_bytes": 0,
            "error_code": "",
            "error_detail": "",
        }

    async def ensure(self) -> Path | None:
        """Return the installed decoder's path, fetching it if absent.

        ``None`` when this platform has no pinned artifact, when the fetch is
        disabled, or when it failed -- callers degrade to reporting the decoder as
        missing rather than raising into a request handler.
        """
        artifact = artifact_for()
        if artifact is None:
            self._set(
                stage=STAGE_UNSUPPORTED,
                error_code=CODE_UNSUPPORTED,
                error_detail=f"{platform.system()} {platform.machine()}",
            )
            return None
        accepted = await self._accept_existing(artifact)
        if accepted is not None:
            return accepted
        if os.environ.get(SKIP_DOWNLOAD_ENV) == "1":
            logger.info("%s=1, not downloading the ffmpeg decoder", SKIP_DOWNLOAD_ENV)
            self._set(
                stage=STAGE_FAILED,
                artifact=artifact.filename,
                error_code=CODE_DOWNLOAD_DISABLED,
                error_detail=f"{SKIP_DOWNLOAD_ENV}=1",
            )
            return None
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            # A concurrent caller may have finished while we waited, and its file
            # gets the same digest check ours would have.
            accepted = await self._accept_existing(artifact)
            if accepted is not None:
                return accepted
            self._set(
                stage=STAGE_DOWNLOADING,
                artifact=artifact.filename,
                total=artifact.wheel_size_bytes,
            )
            try:
                path = await asyncio.to_thread(
                    _fetch_blocking,
                    artifact,
                    lambda done, total: self._set(
                        stage=STAGE_DOWNLOADING,
                        artifact=artifact.filename,
                        done=done,
                        total=total,
                    ),
                )
            except Exception as exc:
                logger.warning("FFmpeg decoder %s fetch failed: %s", artifact.filename, exc)
                self._set(
                    stage=STAGE_FAILED,
                    artifact=artifact.filename,
                    error_code=_failure_code(exc),
                    error_detail=str(exc),
                )
                return None
            self._set(
                stage=STAGE_READY,
                artifact=artifact.filename,
                done=artifact.wheel_size_bytes,
                total=artifact.wheel_size_bytes,
            )
            return path

    async def _accept_existing(self, artifact: DecoderArtifact) -> Path | None:
        """The installed path when *artifact* is present AND matches its pin.

        ``None`` means "carry on to the fetch": either nothing is there, or what
        is there is not the pinned payload -- in which case the fetch replaces it
        atomically rather than this deleting it first, so a host that cannot reach
        PyPI keeps whatever it had instead of losing it to a failed repair.
        """
        if not is_present(artifact):
            return None
        if not await self._verified_on_disk(artifact):
            return None
        self._set(
            stage=STAGE_READY,
            artifact=artifact.filename,
            done=artifact.wheel_size_bytes,
            total=artifact.wheel_size_bytes,
        )
        return installed_path(artifact)

    async def _verified_on_disk(self, artifact: DecoderArtifact) -> bool:
        """Check an already-present decoder against its pin.

        Not cached, and not memoised against size or mtime: ``os.utime`` is
        available to anything that can write the file, so any metadata memo is
        forgeable by the same actor it would be meant to catch. Affordable because
        this runs when a fetch would otherwise be skipped, not per request.

        The resolver re-verifies independently on every open, so a file that
        passes here still cannot be executed on this check alone.
        """
        path = installed_path(artifact)
        if path is None or not path.is_file():
            return False
        actual = await asyncio.to_thread(_sha256_file, path)
        if actual == artifact.sha256:
            return True
        logger.error(
            "FFmpeg decoder at %s does not match its pinned digest (got %s…, "
            "expected %s…); it will be ignored and replaced by a fetch.",
            path,
            actual[:16],
            artifact.sha256[:16],
        )
        return False

    def _set(
        self,
        *,
        stage: str,
        artifact: str = "",
        done: int = 0,
        total: int = 0,
        error_code: str = "",
        error_detail: str = "",
    ) -> None:
        self.status = {
            "stage": stage,
            "artifact": artifact,
            "downloaded_bytes": done,
            "total_bytes": total,
            "error_code": error_code,
            "error_detail": error_detail,
        }


def _failure_code(exc: BaseException) -> str:
    """Classify a fetch failure into a code the dashboard can localise.

    Keyed off which pin the message names rather than off exception type, because
    every verification failure raises the one shared ``ModelDownloadError``: the
    caller needs "the download was tampered with or truncated" told apart from
    "the archive did not carry the member" and from "the write failed", since only
    the first two are worth another attempt.
    """
    text = str(exc)
    if isinstance(exc, ModelDownloadError):
        if "carries no " in text or "refusing archive member" in text:
            return CODE_MEMBER_MISSING
        if "declares " in text or "member exceeds the pinned" in text:
            return CODE_PAYLOAD_UNVERIFIED
        if ".whl" in text:
            return CODE_WHEEL_UNVERIFIED
        return CODE_PAYLOAD_UNVERIFIED
    return CODE_INSTALL_FAILED


_store: DecoderStore | None = None


def store() -> DecoderStore:
    """The process-wide decoder store.

    A module global holding shared, caller-independent state (whether a decoder is
    installed and whether a transfer is running) rather than per-caller data, so
    it is safe in the MCP servers that serve many sessions from one process.
    """
    global _store
    if _store is None:
        _store = DecoderStore()
    return _store


async def maybe_autofetch() -> None:
    """Fetch the decoder in the background when this install has none.

    Called after a whisper model download, which is the moment the user has
    committed to local speech-to-text and is the only point where the cost is
    already understood: a decoder fetch behind a model fetch reads as part of the
    same setup, while one behind a voice memo reads as a hang.

    Skipped on a bundled interpreter (a desktop release carries its own decoder
    and must repair itself by reinstalling, not by downloading one) and whenever
    the resolver already finds one, so a host with a packaged FFmpeg pays nothing.
    """
    if platform_compat.is_bundled_interpreter():
        return
    # Imported here rather than at module scope: transcribe imports THIS module for
    # the artifact table, so a module-scope import back would be a cycle.
    from kiro_crew import transcribe

    if await asyncio.to_thread(transcribe._find_ffmpeg) is not None:
        return
    if artifact_for() is None:
        return
    await store().ensure()
