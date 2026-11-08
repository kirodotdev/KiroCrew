"""The pinned FFmpeg decoder, and a digest-verified store for source installs.

A desktop release carries the pinned executable inside its own bundle (see
:data:`PACKAGED_DIR`), so it never needs this -- except on macOS Intel, which carries none
(see :func:`bundle_carries_decoder`). A source or Toolbox install carries nothing,
and the only decoder it can otherwise reach is a system FFmpeg from a fixed
set of package-manager directories -- which on a distribution that ships no
FFmpeg package (Amazon Linux, RHEL without EPEL) does not exist, leaving batch
voice input permanently broken with a shell command as the only remedy.

**The trust anchor is the digest, not the path.** :mod:`kiro_crew.transcribe`
already pins one executable per platform by size and SHA-256 and
re-verifies it on every open, so fetching *those same bytes* into the data home
and running them through the same check adds a location, not a weaker rule. What
would weaken the model is trusting a directory: a user-writable path is never
enough on its own, which is why the candidate directory list this module sits
beside must not grow an entry for the store.

The executables are the minimal audio-only builds of ``scripts/build-ffmpeg.sh``,
published once per bump by ``.github/workflows/build-ffmpeg-decoder.yml`` as the
GitHub release :data:`DECODER_RELEASE`. Each one is fetched as itself, with no
archive around it, and its size and SHA-256 here are BOTH the transfer bound (a
hostile mirror or a captive-portal HTML body fails on the first byte past the
size, or on the digest) and the SAME pin the runtime authenticates against
(:data:`PACKAGED_FFMPEG_ARTIFACTS`), so the store cannot install bytes the
resolver would then refuse.

The same table drives the desktop build: ``python -m kiro_crew.stt.decoder
--install-into DIR`` is how ``packaging/build-desktop.sh`` puts the decoder into a
bundle, so the build and the runtime cannot pin different bytes.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import platform
import sys
import tempfile
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

#: The release the pins below are taken from. Stated once so the tag and the
#: digests cannot disagree; a new build means a new ``-rN`` tag and new pins, and
#: the publish job refuses to replace the assets of an existing tag.
DECODER_RELEASE = "ffmpeg-decoder-7.1.1-r1"

#: Where :data:`DECODER_RELEASE`'s assets are downloaded from.
_RELEASE_BASE_URL = f"https://github.com/kirodotdev/KiroCrew/releases/download/{DECODER_RELEASE}"

#: The bundle-relative directory a desktop release carries its decoder in: a
#: top-level directory of the bundled interpreter's site-packages, so the runtime
#: finds it under the same trusted roots it already resolves the interpreter's own
#: packages from.
PACKAGED_DIR = ("kirocrew_ffmpeg", "bin")

#: Directory inside the shared ``models/`` tree that holds a fetched decoder. A
#: sibling of the whisper weights rather than a child, for the reason
#: ``models.models_dir`` gives: two artifact families under one directory can
#: collide on a name, and an operator clearing one should not take the other.
_STORE_DIRNAME = "ffmpeg"

#: Suffix for the staging file. Staged inside the target directory so the
#: finishing rename is same-filesystem and therefore atomic.
_STAGING_SUFFIX = ".part"

#: Mode for the installed executable. Owner-writable is deliberate -- this file is
#: replaced by a later fetch -- because the digest, not the mode, is what decides
#: whether it runs.
_EXECUTABLE_MODE = 0o755


@dataclass(frozen=True)
class DecoderArtifact:
    """One platform's pinned decoder, exactly as the release publishes it.

    ``platform_key`` is the build script's ``--target`` spelling, shared with
    ``transcribe._SHIPPED_FFMPEG_PLATFORMS`` so the desktop matrix and this store
    describe platforms in one vocabulary rather than two.

    ``size_bytes`` is a ceiling on the transfer as well as a pre-check: the
    download refuses the first chunk that would exceed it, so a mirror that keeps
    sending cannot fill the disk before the digest is ever computed.
    """

    platform_key: str
    filename: str
    size_bytes: int
    sha256: str

    @property
    def url(self) -> str:
        """The release asset this artifact is fetched from."""
        return f"{_RELEASE_BASE_URL}/{self.filename}"


#: The pinned decoders, one per platform the desktop matrix ships (see
#: ``transcribe._SHIPPED_FFMPEG_PLATFORMS``, which test_transcribe_ffmpeg.py keeps
#: equal to the platform keys here).
#:
#: Size and SHA-256 are the bytes :data:`DECODER_RELEASE` publishes, cross-checked
#: against that release's own ``SHA256SUMS``: the filename selects the platform
#: artifact, size makes a truncated payload fail cheaply, and the digest is the
#: trust anchor. Desktop build staging is intentionally writable, so path
#: placement cannot establish provenance and does not try to.
#:
#: ``macos-x86_64`` is deliberately absent even though the release carries it: the
#: Intel backend ships no decoder and resolves a system FFmpeg instead. Keep it
#: absent -- a pin here is what would make this store fetch that executable and
#: the desktop resolver accept it.
ARTIFACTS: tuple[DecoderArtifact, ...] = (
    DecoderArtifact(
        platform_key="macos-aarch64",
        filename="ffmpeg-macos-aarch64",
        size_bytes=2_248_760,
        sha256="7cb3900ae27620526eebcdd49fcdc366afc8b1a3f08cdec0c205cadee1944300",
    ),
    DecoderArtifact(
        platform_key="linux-x86_64",
        filename="ffmpeg-linux-x86_64",
        size_bytes=3_889_216,
        sha256="688cc450b016f38d31e63bff6b2d0ec62ca0973fda6a0b5cf6242869f764f712",
    ),
    DecoderArtifact(
        platform_key="linux-aarch64",
        filename="ffmpeg-linux-aarch64",
        size_bytes=2_970_296,
        sha256="f10acc9ea1c51becc8ffc03433d39b4863f925ed2881f46d6923f9d02796821f",
    ),
    DecoderArtifact(
        platform_key="windows-x86_64",
        filename="ffmpeg-windows-x86_64.exe",
        size_bytes=2_662_400,
        sha256="ebf1982bec7cb0693c4c1547210244747a5e3d12535b8873fa4ae52215ad3f68",
    ),
)

#: Store filenames earlier releases pinned (the imageio-ffmpeg 0.6.0 executables).
#: Nothing resolves them any more; :func:`migrate_retired_decoder` fetches the
#: current pin for an install that still has one and then removes it, so an
#: upgrade neither breaks batch voice nor strands a 50-90 MB executable.
RETIRED_FILENAMES: tuple[str, ...] = (
    "ffmpeg-macos-aarch64-v7.1",
    "ffmpeg-linux-x86_64-v7.0.2",
    "ffmpeg-linux-aarch64-v7.0.2",
    "ffmpeg-win-x86_64-v7.1.exe",
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

#: ``platform.system()`` to the build target's platform prefix. A system absent here (BSD,
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
#: would leave the Windows and macOS pins unreachable on their own platforms.
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
CODE_PAYLOAD_UNVERIFIED = "decoder_payload_unverified"
CODE_INSTALL_FAILED = "decoder_install_failed"


def platform_key(system: str | None = None, machine: str | None = None) -> str:
    """The build target's platform spelling for *system*/*machine*, or ``""``.

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


#: The one desktop backend built without a decoder: the x86_64 tree of the
#: universal macOS app, for which ``install_desktop_decoder`` in
#: packaging/build-desktop.sh installs no decoder.
DECODERLESS_BUNDLE_PLATFORM = "macos-x86_64"


def bundle_carries_decoder() -> bool:
    """Whether this install's own payload is where its decoder comes from.

    True for every desktop release except the macOS Intel backend. Such a release
    authenticates its own executable, never looks anywhere else, and is repaired
    by reinstalling. The Intel backend carries no decoder at all, so it resolves a
    system FFmpeg the way a source install does, rather than reporting a payload
    that was never shipped as damaged. Every caller that branches on "this is a
    desktop release" for DECODER behaviour asks this, not
    ``is_bundled_interpreter`` alone.

    The exception is named, not derived from the store's pins. On Windows,
    ``platform.machine()`` reports the physical CPU, so the x64 release running on
    an ARM64 PC keys to the unpinned ``windows-aarch64``; a pin lookup would skip
    the decoder that release does carry. On macOS it reports the running
    process's architecture, which is the backend's own: the launcher starts the
    x86_64 tree only as an x86_64 process.
    """
    return (
        platform_compat.is_bundled_interpreter() and platform_key() != DECODERLESS_BUNDLE_PLATFORM
    )


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


def _download_into(
    artifact: DecoderArtifact,
    target_dir: Path,
    on_progress: object = None,
) -> Path:
    """Download, verify and install *artifact* as ``target_dir/filename``. Blocks.

    The bytes are streamed to a staging file in *target_dir* under the pin's size
    bound and digest check (``stream_pinned_payload``), made executable, and only
    then renamed over the target, so a tampered or truncated transfer never
    replaces a good decoder and never lands under the name the resolver opens.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / artifact.filename
    staging_fd, staging_name = tempfile.mkstemp(
        dir=target_dir, prefix=f"{artifact.filename}.", suffix=_STAGING_SUFFIX
    )
    staging = Path(staging_name)
    try:
        # The descriptor is adopted FIRST, for the reason models._download_blocking
        # gives: mkstemp hands back an unowned fd, and the cleanup below unlinks a
        # PATH, so an exception raised before the adoption leaks one descriptor per
        # attempt and (on Windows) strands the partial file with it.
        with os.fdopen(staging_fd, "wb") as handle:
            stream_pinned_payload(
                artifact.url,
                label=artifact.filename,
                expected_size=artifact.size_bytes,
                expected_sha256=artifact.sha256,
                write=handle.write,
                on_progress=on_progress,  # type: ignore[arg-type]
            )
        # Executable, because the resolver will spawn it. Through chmod_safe: this
        # runs on Windows too, where os.chmod cannot express the bit at all.
        platform_compat.chmod_safe(staging, _EXECUTABLE_MODE)
        # replace_with_retry, not os.replace: on Windows the rename fails while any
        # other handle is open on either path, and a freshly written executable is
        # exactly what an AV scanner reaches for.
        replace_with_retry(staging, target)
    except BaseException:
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            logger.debug("Could not remove staging file %s", staging, exc_info=True)
        raise
    logger.info("FFmpeg decoder %s verified and installed at %s", artifact.filename, target)
    return target


def _fetch_blocking(
    artifact: DecoderArtifact,
    on_progress: object = None,
) -> Path:
    """Fetch *artifact* into the data-home store. Blocks; never on the loop."""
    return _download_into(artifact, store_dir(), on_progress)


def _digest_if_file(path: Path) -> str | None:
    """The sha256 of *path*, or ``None`` when it is not a regular file. Blocks.

    The ``is_file`` probe and the hash are both filesystem calls, so the caller
    runs them together in ONE ``asyncio.to_thread`` hop rather than probing on
    the event loop. Read errors propagate to the caller.
    """
    if not path.is_file():
        return None
    return _sha256_file(path)


class DecoderStore:
    """Serialises decoder fetches and exposes their progress.

    One store per process, and one in-flight fetch: a settings panel that pressed
    the button and a model download that finished at the same moment share the
    transfer through ``_lock`` rather than both pulling the same executable.
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
                total=artifact.size_bytes,
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
                done=artifact.size_bytes,
                total=artifact.size_bytes,
            )
            await asyncio.to_thread(_remove_retired)
            return path

    async def _accept_existing(self, artifact: DecoderArtifact) -> Path | None:
        """The installed path when *artifact* is present AND matches its pin.

        ``None`` means "carry on to the fetch": either nothing is there, or what
        is there is not the pinned payload -- in which case the fetch replaces it
        atomically rather than this deleting it first, so a host that cannot reach
        the release keeps whatever it had instead of losing it to a failed repair.
        """
        if not await asyncio.to_thread(is_present, artifact):
            return None
        if not await self._verified_on_disk(artifact):
            return None
        self._set(
            stage=STAGE_READY,
            artifact=artifact.filename,
            done=artifact.size_bytes,
            total=artifact.size_bytes,
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
        if path is None:
            return False
        actual = await asyncio.to_thread(_digest_if_file, path)
        if actual is None:
            return False
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

    Every verification failure raises the one shared ``ModelDownloadError``, and
    the caller only needs "the download did not match its pin" (worth another
    attempt) told apart from "the write failed".
    """
    if isinstance(exc, ModelDownloadError):
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


#: Process-wide, caller-independent: the one retired-name migration, once started.
#: Kept after it finishes so it never runs twice, and awaited while in flight so a
#: concurrent transcode waits for the same transfer instead of failing.
_migration_task: asyncio.Task[Path | None] | None = None


def has_retired_decoder() -> bool:
    """Whether the store still holds a decoder under a retired pinned name."""
    root = store_dir()
    return any((root / name).is_file() for name in RETIRED_FILENAMES)


def _remove_retired() -> None:
    """Delete retired-name decoders from the store. Best effort, never raises."""
    root = store_dir()
    for name in RETIRED_FILENAMES:
        try:
            (root / name).unlink(missing_ok=True)
        except OSError:
            logger.debug("Could not remove retired decoder %s", name, exc_info=True)


async def _join_migration(task: asyncio.Task[Path | None]) -> Path | None:
    """Wait for the migration *task* another caller started; ``None`` when it
    runs on a different event loop, which this caller cannot await."""
    try:
        same_loop = task.get_loop() is asyncio.get_running_loop()
    except RuntimeError:
        same_loop = False
    if not same_loop:
        return None
    # Shielded: one caller giving up must not cancel the shared transfer.
    return await asyncio.shield(task)


async def migrate_retired_decoder() -> Path | None:
    """Fetch the current pin for an install whose store has only a retired one.

    Called by the transcode resolver when nothing resolves. ``None`` -- and no
    network -- unless this is a store-using install (no decoder in the bundle, a
    pinned platform) that still has a retired file. The retired file is removed
    by :meth:`DecoderStore.ensure` once the current pin is installed.
    """
    global _migration_task
    if _migration_task is not None:
        return await _join_migration(_migration_task)
    if bundle_carries_decoder() or artifact_for() is None:
        return None
    has_retired = await asyncio.to_thread(has_retired_decoder)
    # Another caller can start the migration while this one awaits the probe, and
    # its transfer removes the retired file, so a probe that ran after it finds
    # none: join that transfer rather than report the decoder missing.
    if _migration_task is not None:
        return await _join_migration(_migration_task)
    if not has_retired:
        return None
    # Started at most once per process: on a host that cannot reach the release,
    # every later transcode would otherwise wait out another failed fetch. The
    # settings page's Download button still retries on demand.
    if _migration_task is None:
        logger.info(
            "Replacing a retired FFmpeg decoder in %s with %s", store_dir(), DECODER_RELEASE
        )
        _migration_task = asyncio.ensure_future(store().ensure())
    return await asyncio.shield(_migration_task)


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


def main(argv: list[str] | None = None) -> int:
    """``python -m kiro_crew.stt.decoder --install-into DIR [--platform KEY]``.

    The desktop build's only way to put a decoder into a bundle, so the bytes it
    ships are the bytes this module pins. ``--platform`` defaults to this host;
    a platform with no pin exits 3 without writing anything, which is how the
    macOS Intel backend stays decoderless.
    """
    parser = argparse.ArgumentParser(prog="python -m kiro_crew.stt.decoder")
    parser.add_argument("--install-into", required=True, type=Path, metavar="DIR")
    parser.add_argument("--platform", default="", metavar="KEY")
    args = parser.parse_args(argv)
    artifact = _BY_PLATFORM.get(args.platform) if args.platform else artifact_for()
    if artifact is None:
        print(f"no pinned decoder for {args.platform or platform_key() or 'this host'}")
        return 3
    try:
        path = _download_into(artifact, args.install_into)
    except (ModelDownloadError, OSError) as exc:
        # OSError covers the transport too (urllib's errors subclass it), so an
        # unreachable host fails the build with this line, not a traceback.
        print(f"decoder download failed: {exc}", file=sys.stderr)
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
