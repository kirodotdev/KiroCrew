"""The whisper.cpp model catalog, and a sha256-pinned downloader for it.

Speech recognition needs weights, and shipping them in the wheel is not an
option: the smallest useful one is 148 MB. So the first use of voice input
fetches one model, once, and every later session loads it from disk.

**The sha256 pin is the trust anchor, and not only for the network fetch.** A
model is written to its final path only after the digest computed while streaming
matches the pinned one, so a tampered mirror, a truncated transfer or a
captive-portal HTML body can only fail verification.

A file ALREADY on disk is verified too, on every load (see
``ModelStore._verified_on_disk``), because provenance is not enough: a same-size
file dropped over the weights would otherwise be trusted forever. Not cached
against the file's metadata either -- ``os.utime`` is available to anything that can
write the file, so a size-and-mtime memo is forgeable by the same actor it is meant
to catch.

The digest is the second line, not the first. Verifying and then handing a PATH to a
native loader leaves a window in which the bytes can be swapped, and re-hashing
cannot close it because the loader re-opens by name. What closes it is that
``<data home>/models`` is WRITE-PROTECTED from the agent on both gates
(``security._WRITE_PROTECTED_HOME_PATHS`` for the file tools,
``_WRITE_PROTECTED_BASH_LEAVES`` for the shell), so the verified bytes are the
loaded bytes.

The shell half needs one thing more than a path entry, and it is worth stating here
because this module is what depends on it: the weight FILENAME is fenced with no
anchor at all (``security._WHISPER_WEIGHT_NAME``). A home-anchored directory pattern
falls to a single ``cd`` -- ``cd <data home>/models; cp evil.bin ggml-base.bin`` names
neither the home nor the directory -- and since the loader opens by name, that command
decides what a C++ GGML parser reads. Adding a row to :data:`CATALOG` therefore needs
no edit in ``security``: the pattern covers any ``ggml-*.bin``. Kiro Crew's own downloader writes here directly and does not route
through those gates, so a first-run fetch and a re-download after a failed check both
still work. That is a deliberate divergence from
``embeddings.ModelDownloadManager``, which documents the same size-only trade for
its own GGUF -- that reasoning weighed a corrupted download, not a writable
directory and an agent with a shell.

This deliberately does not reuse that manager. It is bound to one pinned
artifact through ``default_model_path()``, ``_GGUF_SHA256`` and the
``memory.embed_model_path`` custom-model escape hatch, so sharing it would mean
generalising the memory subsystem's download path to carry a catalog. The shape,
the doctrine and the ``KIROCREW_SKIP_MODEL_DOWNLOAD`` escape hatch are mirrored
instead, which keeps the two subsystems independent.

Digests here were obtained by downloading each file and hashing it, not by
trusting an upstream header: HuggingFace's ``X-Linked-Etag`` for these objects is
not the content digest. Two of them (``base``, ``small``) independently match the
digests kiro-cli pins for the same models, which is a second source on the same
bytes.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from kiro_crew.atomic_write import replace_with_retry
from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

#: Upstream for the ggml conversions of OpenAI's Whisper weights. This is the
#: canonical publisher that whisper.cpp itself points at. Unlike the embedding
#: GGUF, Kiro Crew serves no mirror of these, so the pin below is doing the whole
#: job of establishing what we accept.
MODELS_BASE_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main"

#: Operator override for the base URL, for an air-gapped or mirrored install. The
#: pin still applies, so a mirror can serve the bytes but cannot substitute them.
MODEL_URL_ENV = "KIROCREW_WHISPER_MODEL_BASE_URL"

#: Shared with the embedding downloader on purpose: one switch means "this
#: process must not pull model weights over the network", and a test run wants
#: that to hold for every subsystem, not one of them.
SKIP_DOWNLOAD_ENV = "KIROCREW_SKIP_MODEL_DOWNLOAD"

#: Read size while streaming a download. Large enough that the digest update and
#: the progress callback are not per-packet work, small enough that a cancelled
#: download stops promptly.
_CHUNK_BYTES = 1 << 20

#: Suffix for the staging file. Staged inside the TARGET directory so the final
#: step is a same-filesystem ``os.replace`` and therefore atomic; a staging area
#: under the system temp dir can land on a different device, where the rename
#: degrades into a copy that a reader can observe half-finished.
_STAGING_SUFFIX = ".part"

#: Per-socket-operation ceiling on the model download, NOT a ceiling on the whole
#: transfer: `urlopen`'s timeout bounds each individual read, so a 1.6 GB model on a
#: slow line still completes while a connection that stops delivering bytes fails
#: instead of hanging. Without it a stalled TCP connection held its worker for the
#: life of the process, and the only visible symptom was a download progress bar
#: frozen at some percentage -- indistinguishable from the slow-but-working case the
#: byte counter exists to prove apart.
_NETWORK_STALL_TIMEOUT_SECS = 60.0


@dataclass(frozen=True)
class WhisperModel:
    """One entry in the catalog.

    ``size_bytes`` is carried for two reasons: the UI states the download cost
    before asking for it, and a file whose size does not match cannot be the
    pinned artifact, which is a free pre-check before any expensive work.
    """

    name: str
    size_bytes: int
    sha256: str

    @property
    def filename(self) -> str:
        return f"ggml-{self.name}.bin"


#: The offered models, smallest first. Deliberately short. Every extra row is a
#: choice the user has to make before they can dictate a sentence, and the
#: accuracy ladder here already spans the useful range: ``tiny`` for a slow
#: machine, ``base`` for everyone, ``small`` when accents or jargon need it, and
#: ``large-v3-turbo`` for the accuracy ceiling. The English-only (``.en``)
#: variants are left out because they are a trap for a multilingual user and buy
#: little for an English one.
CATALOG: tuple[WhisperModel, ...] = (
    WhisperModel(
        "tiny",
        77_691_713,
        "be07e048e1e599ad46341c8d2a135645097a538221678b7acdd1b1919c6e1b21",
    ),
    WhisperModel(
        "base",
        147_951_465,
        "60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe",
    ),
    WhisperModel(
        "small",
        487_601_967,
        "1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b",
    ),
    WhisperModel(
        "large-v3-turbo",
        1_624_555_275,
        "1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69",
    ),
)

#: The default. Small enough that the one-time download is not a decision, and
#: accurate enough for dictation; measured real-time factor is ~0.01, so decoding
#: is never the thing a user waits on.
DEFAULT_MODEL = "base"

_BY_NAME: dict[str, WhisperModel] = {m.name: m for m in CATALOG}

#: Names accepted from superseded configuration, mapped to the entry that best
#: honours what the user actually asked for. Without this table every one of them
#: falls back to the default, which for someone who deliberately picked the
#: accuracy ceiling is a silent downgrade to the second-smallest model.
#:
#: Two mappings deserve their reasoning stated, because they look like
#: substitutions and are not:
#:
#: - The full-size ``large`` lineage resolves to ``large-v3-turbo``. Turbo is the
#:   same encoder with a distilled decoder, so it keeps the accuracy the user was
#:   asking for while decoding several times faster.
#: - ``medium`` also resolves to ``large-v3-turbo``, which is an upgrade rather
#:   than a compromise: turbo is both more accurate and faster than medium, so
#:   there is no reading of "medium" that turbo does not satisfy better.
#:
#: The English-only (``.en``) names drop to their multilingual sibling of the same
#: size. That loses a little English accuracy and gains every other language,
#: which is the right default for a config value nobody will revisit.
_ALIASES: dict[str, str] = {
    "turbo": "large-v3-turbo",
    "large": "large-v3-turbo",
    "large-v1": "large-v3-turbo",
    "large-v2": "large-v3-turbo",
    "large-v3": "large-v3-turbo",
    "large-v3-turbo-q5_0": "large-v3-turbo",
    "large-v3-turbo-q8_0": "large-v3-turbo",
    "medium": "large-v3-turbo",
    "medium.en": "large-v3-turbo",
    "small.en": "small",
    "base.en": "base",
    "tiny.en": "tiny",
}


def resolve(name: str) -> WhisperModel:
    """Return the catalog entry for *name*, falling back to the default.

    Falls back with a warning rather than raising: this value arrives from
    ``config.json``, and an unrecognised model must degrade to a working default
    the way an unusable backend does, not fail the voice session that read it.
    """
    canonical = _ALIASES.get(name, name)
    model = _BY_NAME.get(canonical)
    if model is not None:
        return model
    logger.warning(
        "Unknown whisper model %r; using %r. Known models: %s",
        name,
        DEFAULT_MODEL,
        ", ".join(_BY_NAME),
    )
    return _BY_NAME[DEFAULT_MODEL]


def models_dir() -> Path:
    """Where whisper weights live. Respects ``KIROCREW_HOME``.

    A subdirectory of the shared ``models/`` tree rather than a sibling of it, so
    the embedding GGUF and these cannot collide on a filename and an operator
    clearing one does not take the other with it.
    """
    return config_dir() / "models" / "whisper"


def model_path(model: WhisperModel) -> Path:
    return models_dir() / model.filename


def is_present(model: WhisperModel) -> bool:
    """Whether *model* is on disk and the right size.

    The size check is what makes an interrupted download visible. A staging file
    is never at this path, so a wrong size here means a truncated or replaced
    file, and treating it as absent lets the next download overwrite it.
    """
    try:
        return model_path(model).stat().st_size == model.size_bytes
    except OSError:
        return False


def _model_url(model: WhisperModel) -> str:
    base = os.environ.get(MODEL_URL_ENV, "").strip() or MODELS_BASE_URL
    return f"{base.rstrip('/')}/{model.filename}"


ProgressFn = Callable[[int, int], None]


class ModelDownloadError(RuntimeError):
    """A download did not produce a verified model file."""


def _sha256_file(path: Path) -> str:
    """Digest a file on disk. Blocking; callers run it off the loop.

    Read in `_CHUNK_BYTES` pieces rather than whole: the largest model is 1.6 GB, and
    reading that into memory to hash it would cost more than the hash.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def stream_pinned_payload(
    url: str,
    *,
    label: str,
    expected_size: int,
    expected_sha256: str,
    write: Callable[[bytes], object],
    on_progress: ProgressFn | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> None:
    """Stream *url* into *write*, refusing anything but the pinned payload.

    Shared by the whisper weights and by the ffmpeg decoder wheel
    (:mod:`kiro_crew.stt.decoder`), because every property below is a property of
    "fetching a sha256-pinned artifact over the network" rather than of either
    artifact: the https floor, the per-socket stall timeout, the digest computed
    while streaming, and the size enforced as a CEILING before each write. A
    second copy of this is a second place for one of them to be missing.

    *write* is a callable rather than a path, so the caller owns where the bytes
    land -- an exclusively-created staging descriptor in both cases -- and this
    function never opens or names a file.

    Raises :class:`ModelDownloadError` on a refusal; the transport's own errors
    (an unreachable host, an HTTP status, the stall timeout) propagate unchanged
    so a caller can tell a network failure from a rejected payload.
    """
    if not url.startswith("https://"):
        # The pin bounds what we accept, but plaintext would still leak which
        # artifact an operator uses and let a network attacker waste the transfer.
        raise ModelDownloadError(f"{label}: refusing a non-https URL: {url}")
    digest = hashlib.sha256()
    written = 0
    with (
        # Inside the parentheses, immediately above the call: `nosemgrep` only
        # covers its own line and the next one.
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- https enforced above and the payload is sha256-pinned
        urllib.request.urlopen(url, timeout=_NETWORK_STALL_TIMEOUT_SECS) as response,
    ):
        while True:
            if should_cancel is not None and should_cancel():
                raise ModelDownloadError("cancelled")
            chunk = response.read(_CHUNK_BYTES)
            if not chunk:
                break
            # Refused BEFORE the write, because the pinned size is a ceiling on
            # what we are willing to store and not merely something to check
            # afterwards. Streaming to EOF first and comparing the total lets a
            # hostile or misconfigured mirror fill the disk: nothing about an
            # HTTPS response bounds its length, and `Content-Length` is not
            # consulted (it is the server's claim, not the pin). Failing on the
            # first excess chunk caps the damage at one `_CHUNK_BYTES` over the
            # size we already agreed to.
            if written + len(chunk) > expected_size:
                raise ModelDownloadError(
                    f"{label}: response exceeds the pinned "
                    f"{expected_size} bytes; refusing to keep writing"
                )
            write(chunk)
            digest.update(chunk)
            written += len(chunk)
            if on_progress is not None:
                on_progress(written, expected_size)
    # Only a SHORT response can reach this now; the oversized case fails inside the
    # loop. Kept as a distinct check because a truncated transfer is the common
    # failure (a dropped connection) and deserves its own message.
    if written != expected_size:
        raise ModelDownloadError(f"{label}: expected {expected_size} bytes, received {written}")
    actual = digest.hexdigest()
    if actual != expected_sha256:
        raise ModelDownloadError(
            f"{label}: sha256 mismatch (got {actual[:16]}…, expected {expected_sha256[:16]}…)"
        )


def _download_blocking(
    model: WhisperModel,
    on_progress: ProgressFn | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> Path:
    """Fetch and verify *model*, returning its final path. Blocks; never on the loop.

    The digest is computed while streaming rather than by re-reading the finished
    file, so a tampered payload is rejected without ever having been written to
    the path a loader would pick up.
    """
    target = model_path(model)
    target.parent.mkdir(parents=True, exist_ok=True)
    url = _model_url(model)

    # `mkstemp`, not a name this function composes. Two properties matter and neither
    # is available from `open(path, "wb")`:
    #
    # * It opens with ``O_CREAT | O_EXCL``, so it CANNOT follow a symlink. This
    #   directory is agent-writable, and a predictable staging path (a fixed
    #   ``.bin.part``, or one derived from a PID that is trivially observable) let an
    #   agent pre-plant a symlink there and have the download truncate and overwrite
    #   whatever it pointed at.
    # * The name is unpredictable and unique, which also settles the collision the
    #   fixed name caused: nothing serialises across processes -- a gateway, an MCP
    #   server and a `kirocrew` CLI each run their own store over the same data home --
    #   so two transfers interleaved their writes into one file and the sha256 pin then
    #   failed BOTH of them.
    #
    # Created in the TARGET directory so the finishing rename stays same-filesystem
    # and therefore atomic; a system-temp staging area can land on another device,
    # where the rename degrades into a copy a reader can observe half-finished.
    staging_fd, staging_name = tempfile.mkstemp(
        dir=target.parent, prefix=f"{target.name}.", suffix=_STAGING_SUFFIX
    )
    staging = Path(staging_name)
    try:
        # The descriptor is adopted FIRST, and the order is load-bearing even
        # though opening the connection first reads more naturally. `mkstemp`
        # hands back an unowned fd and nothing else here closes one -- the
        # `finally` below unlinks the PATH -- so with the transfer in front, every
        # unreachable host, HTTP error and stall timeout leaked one descriptor per
        # attempt, because a context manager that never gets evaluated never gets
        # exited. On Windows it also stranded the partial file, since the unlink
        # cannot remove a file that still has a live handle.
        #
        # Written through the DESCRIPTOR `mkstemp` returned, never reopened by
        # name: reopening would reintroduce the very race the exclusive create
        # closed, since the path is public the moment it exists.
        with os.fdopen(staging_fd, "wb") as fh:
            stream_pinned_payload(
                url,
                label=model.name,
                expected_size=model.size_bytes,
                expected_sha256=model.sha256,
                write=fh.write,
                on_progress=on_progress,
                should_cancel=should_cancel,
            )
        # replace_with_retry, not os.replace: on Windows the rename fails with a
        # PermissionError while ANY other handle is open on either path, and a
        # just-written 148MB file is exactly what an AV scanner or the Search
        # indexer reaches for.
        replace_with_retry(staging, target)
        logger.info("Whisper model %s verified and installed at %s", model.name, target)
        return target
    finally:
        # A failed or cancelled attempt must not leave a partial file behind. On
        # the success path the rename already consumed it, so missing is normal.
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            logger.debug("Could not remove staging file %s", staging, exc_info=True)


class ModelStore:
    """Serialises model downloads and exposes their progress.

    One store per process. Concurrent callers (a voice session that needs the
    model and a settings panel that asked for it) share one in-flight transfer
    through ``_lock`` instead of both pulling the same gigabyte.
    """

    def __init__(self) -> None:
        self._lock: asyncio.Lock | None = None
        #: Strong references to detached decoder fetches. A task nobody references
        #: can be collected mid-await, and the set is per-store so a test with its
        #: own store cannot be handed another one's pending work.
        self._decoder_tasks: set[asyncio.Task[None]] = set()
        self.status: dict[str, object] = {
            "step": "idle",
            "model": "",
            "downloaded_bytes": 0,
            "total_bytes": 0,
            "error": "",
        }

    def _start_decoder_autofetch(self) -> None:
        """Fetch the ffmpeg decoder in the background, if this install needs one.

        Started here because a completed model download is the one moment a second
        transfer reads as part of the same setup: the user has just committed to
        local speech-to-text, and a source install with no system FFmpeg cannot
        decode a browser recording or a voice memo at all. The alternative is
        discovering that at the first upload, which is where the shell command the
        settings page used to print came from.

        Detached rather than awaited: the caller is a voice session or a settings
        poll waiting on the WEIGHTS, and live PCM never touches the decoder.
        Failures are the decoder store's to report through its own status, so this
        only has to keep the task alive and its exception retrieved.
        """
        # Imported here, not at module scope: decoder imports this module, so a
        # module-scope import back would be a cycle.
        from kiro_crew.stt import decoder as stt_decoder

        try:
            task = asyncio.ensure_future(stt_decoder.maybe_autofetch())
        except RuntimeError:
            # No running loop. `ensure` is always awaited, so this is not reachable
            # from production; it keeps a synchronous caller in a test from turning
            # a successful model download into a failure.
            logger.debug("No event loop for the decoder autofetch", exc_info=True)
            return
        self._decoder_tasks.add(task)
        task.add_done_callback(self._decoder_task_done)

    def _decoder_task_done(self, task: asyncio.Task[None]) -> None:
        """Drop the reference and retrieve the exception, so neither is a warning."""
        self._decoder_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning("Background ffmpeg decoder fetch failed: %s", exc)

    async def ensure(self, model: WhisperModel) -> Path | None:
        """Return the on-disk path of *model*, downloading it if absent.

        Returns ``None`` when the model is not available and cannot be fetched,
        so a caller degrades to reporting voice as unavailable rather than
        raising into a websocket handler.

        A file already on disk is verified against the pin (see
        :meth:`_verified_on_disk`) rather than trusted on its size alone. BOTH
        already-present branches go through :meth:`_accept_existing`, which is the
        point: an earlier revision gated only the pre-lock one and left the post-lock
        "a concurrent caller finished while we waited" branch returning the file
        unverified, which is the same hole through the other door.
        """
        accepted = await self._accept_existing(model)
        if accepted is not None:
            return accepted
        if os.environ.get(SKIP_DOWNLOAD_ENV) == "1":
            logger.info("%s=1, not downloading whisper model %s", SKIP_DOWNLOAD_ENV, model.name)
            self._set(step="skipped", model=model.name, error="model download disabled")
            return None
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            # A concurrent caller may have completed it while we waited, and its file
            # gets the same check ours would have.
            accepted = await self._accept_existing(model)
            if accepted is not None:
                return accepted
            self._set(step="downloading", model=model.name, total=model.size_bytes)
            try:
                path = await asyncio.to_thread(
                    _download_blocking,
                    model,
                    lambda done, total: self._set(
                        step="downloading", model=model.name, done=done, total=total
                    ),
                )
            except Exception as exc:
                logger.warning("Whisper model %s download failed: %s", model.name, exc)
                self._set(step="failed", model=model.name, error=str(exc))
                return None
            self._set(step="ready", model=model.name, total=model.size_bytes)
            self._start_decoder_autofetch()
            return path

    async def _accept_existing(self, model: WhisperModel) -> Path | None:
        """The on-disk path if *model* is present AND matches its pin, else ``None``.

        The single gate for "there is already a usable file here". Both of `ensure`'s
        already-present branches ask this rather than `is_present` directly, so a
        digest check cannot be added to one door and forgotten at the other.

        ``None`` means "carry on to the download": either nothing is there, or what was
        there failed the pin and has been deleted.
        """
        if not is_present(model):
            return None
        if not await self._verified_on_disk(model):
            logger.warning("Re-downloading whisper model %s after a failed check", model.name)
            return None
        self._set(step="ready", model=model.name, total=model.size_bytes)
        return model_path(model)

    async def _verified_on_disk(self, model: WhisperModel) -> bool:
        """Check an already-present model against its pin, once per process.

        `is_present` deliberately tests only the size: it answers "must this be
        downloaded", and it is on the path of a UI poll. But size alone is not a
        trust check, and the models directory is fenced by nothing -- neither
        `is_sensitive_path` nor `is_sensitive_write_path` covers it, and a plain
        ``cp`` over the weights is an allowed bash command. So an agent could replace
        them with a same-size file and every later session would transcribe the user's
        speech through weights of its choosing, persistently and with nothing to show
        it had happened.

        Verified on EVERY call, with no metadata cache. A first version memoised the
        result against the file's size and mtime, which does not hold: `os.utime` is
        available to anything that can write the file, so a same-size overwrite with a
        restored mtime satisfied the memo and the next load trusted it. Anything cheap
        enough to check is also cheap enough to forge.

        Affordable because `WhisperEngine.ensure_loaded` decides residency BEFORE
        asking the store, so this runs once per model LOAD rather than once per
        session. That is also precisely when an unverified file would take effect: a
        resident model is already loaded from bytes that passed, and the next thing
        that could load different ones is the next load.

        A file that fails is deleted, so the caller's download path replaces it rather
        than reporting voice as broken.

        Deliberately diverging from `embeddings`, which documents the same size-only
        trade for its own GGUF. That reasoning ("a sha256 over ~600MB on every boot
        buys almost nothing") weighed a corrupted download, not a writable directory
        and an agent with a shell.
        """
        path = model_path(model)
        if not path.is_file():
            return False
        actual = await asyncio.to_thread(_sha256_file, path)
        if actual == model.sha256:
            return True
        logger.error(
            "Whisper model %s at %s does not match its pinned digest "
            "(got %s…, expected %s…). Removing it: a model of the right SIZE but the "
            "wrong CONTENT would transcribe every later utterance through weights "
            "nobody verified.",
            model.name,
            path,
            actual[:16],
            model.sha256[:16],
        )
        try:
            path.unlink()
        except OSError:
            logger.warning("Could not remove the unverified model at %s", path, exc_info=True)
            return False
        return False

    def _set(
        self,
        *,
        step: str,
        model: str,
        done: int = 0,
        total: int = 0,
        error: str = "",
    ) -> None:
        self.status = {
            "step": step,
            "model": model,
            "downloaded_bytes": done,
            "total_bytes": total,
            "error": error,
        }


_store: ModelStore | None = None


def store() -> ModelStore:
    """The process-wide model store.

    A module global holding shared, caller-independent state (which model files
    exist and whether a transfer is running) rather than per-caller data, so it is safe
    in the MCP servers that serve many sessions from one process.
    """
    global _store
    if _store is None:
        _store = ModelStore()
    return _store
