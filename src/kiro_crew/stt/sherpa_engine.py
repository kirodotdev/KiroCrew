"""The resident sherpa-onnx recogniser, and the lock discipline around it.

One ``OfflineRecognizer`` per process, shared by every live session and every file
transcription that selects the ``sherpa_onnx`` provider, the same arrangement the
whisper engine (:mod:`kiro_crew.stt.engine`) uses for its model. The state held is
caller-independent (which model is loaded), so a module-level singleton is safe.

``sherpa_onnx`` is an optional extra and is imported only inside the functions that
need it, so a gateway without it starts normally and :func:`probe` says why the
provider cannot run.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import platform
import sys
import time
from dataclasses import dataclass
from typing import Any

import numpy as np

from kiro_crew import extras
from kiro_crew.executors import stt_executor
from kiro_crew.stt import sherpa_custom, sherpa_models
from kiro_crew.stt.engine import Availability, thread_count
from kiro_crew.stt.limits import DEFAULT_IDLE_EVICT_SECS, DEFAULT_TIMEOUT_SECS, THREADS_AUTO
from kiro_crew.stt.vad import SAMPLE_RATE_HZ

logger = logging.getLogger(__name__)

#: The extra that installs ``sherpa_onnx``.
EXTRA = "voice-sherpa"

CODE_EXTRA_MISSING = "stt_sherpa_extra_missing"
#: Not installed, and this platform has no prebuilt wheel, so installing the extra
#: would start a source build. Its own code because the remedy differs.
CODE_NO_WHEEL = "stt_sherpa_no_wheel"
CODE_IMPORT_FAILED = "stt_sherpa_import_failed"
#: The model has to be downloaded and this Python cannot open a ``.tar.bz2``.
CODE_BZ2_MISSING = "stt_sherpa_bz2_missing"
CODE_MODEL_MISSING = "stt_sherpa_model_missing"
CODE_FAILED = "stt_sherpa_failed"

#: Advisory English for :data:`CODE_BZ2_MISSING`, shared by the probe and the load.
BZ2_MISSING_DETAIL = (
    "this Python was built without the bz2 module, so it cannot unpack the speech "
    "model; rebuild or reinstall Python with bz2 support (libbz2), or use the "
    "desktop app"
)

#: A 30M-parameter model gains nothing from more intra-op threads than this.
MAX_THREADS = 4

#: Shortest buffer handed to the recogniser. The encoder raises on an empty feature
#: matrix, so a shorter buffer is padded with silence up to this.
MIN_DECODE_SAMPLES = int(0.3 * SAMPLE_RATE_HZ)

_FEATURE_DIM = 80


class DecodeFailed(RuntimeError):
    """A load or decode raised or timed out, so an empty result means nothing."""


#: Lower-cased ``platform.machine()`` spellings sherpa-onnx publishes a wheel for, per
#: ``platform.system()``. Transcribed from the wheels of the release the extra pins.
_WHEEL_ARCHS: dict[str, frozenset[str]] = {
    "Darwin": frozenset({"arm64", "aarch64", "x86_64"}),
    "Linux": frozenset({"x86_64", "amd64", "aarch64", "arm64", "armv7l"}),
    "Windows": frozenset({"amd64", "x86_64", "x86", "i386", "i686", "arm64", "aarch64"}),
}
#: The oldest glibc each Linux wheel accepts (manylinux2014 is 2.17).
_GLIBC_FLOOR: dict[str, tuple[int, int]] = {"armv7l": (2, 35)}
_GLIBC_DEFAULT_FLOOR = (2, 17)
_WINDOWS_ARM = frozenset({"arm64", "aarch64"})
#: Windows ARM64 wheels start at CPython 3.11.
_WINDOWS_ARM_MIN_PYTHON = (3, 11)


def _glibc_version(raw: str) -> tuple[int, int] | None:
    parts = raw.split(".")
    try:
        return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        return None


def _has_prebuilt_wheel(
    system: str | None = None,
    machine: str | None = None,
    libc: tuple[str, str] | None = None,
    python: tuple[int, int] | None = None,
) -> bool:
    """Whether sherpa-onnx publishes a wheel for this host. Fails closed on anything unlisted.

    Every argument defaults to the running host; they are parameters so the table
    can be tested for hosts this one is not. On Linux the C library decides:
    ``platform.libc_ver`` answers ``('', '')`` on musl, which has no wheel.
    """
    system = platform.system() if system is None else system
    machine = (platform.machine() if machine is None else machine).lower()
    allowed = _WHEEL_ARCHS.get(system)
    if allowed is None or machine not in allowed:
        return False
    if system == "Windows" and machine in _WINDOWS_ARM:
        version = sys.version_info[:2] if python is None else python
        return tuple(version) >= _WINDOWS_ARM_MIN_PYTHON
    if system == "Linux":
        name, raw = platform.libc_ver() if libc is None else libc
        found = _glibc_version(raw) if name == "glibc" else None
        return found is not None and found >= _GLIBC_FLOOR.get(machine, _GLIBC_DEFAULT_FLOOR)
    return True


def probe() -> Availability:
    """Whether ``sherpa_onnx`` is installed and imports. BLOCKING (it may dlopen)."""
    try:
        installed = importlib.util.find_spec("sherpa_onnx") is not None
    except Exception as exc:
        return Availability(False, CODE_IMPORT_FAILED, f"sherpa-onnx failed to load: {exc}")
    if not installed:
        if not _has_prebuilt_wheel():
            return Availability(
                False,
                CODE_NO_WHEEL,
                f"sherpa-onnx has no prebuilt package for {platform.system()} "
                f"{platform.machine()}; installing it needs CMake and a C++ compiler, "
                f"then {extras.install_hint(EXTRA)}",
            )
        return Availability(
            False,
            CODE_EXTRA_MISSING,
            f"the Sherpa-ONNX provider needs its package: {extras.install_hint(EXTRA)}",
        )
    try:
        import sherpa_onnx  # noqa: F401
    except Exception as exc:  # pragma: no cover (host-specific loader failures)
        return Availability(False, CODE_IMPORT_FAILED, f"sherpa-onnx failed to load: {exc}")
    return Availability(True)


@dataclass(frozen=True)
class LoadedKey:
    """What a resident recogniser is specific to. A change forces a reload."""

    model: str
    threads: int


def _build_recognizer(model: sherpa_models.SherpaModel, threads: int) -> Any:
    """Construct the recogniser from the verified files. BLOCKING; on the STT pool."""
    import sherpa_onnx

    def path(entry: sherpa_models.SherpaFile) -> str:
        return str(sherpa_models.file_path(model, entry))

    return sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=path(model.encoder),
        decoder=path(model.decoder),
        joiner=path(model.joiner),
        tokens=path(model.tokens),
        num_threads=threads,
        sample_rate=SAMPLE_RATE_HZ,
        feature_dim=_FEATURE_DIM,
        decoding_method="greedy_search",
    )


def _decode_blocking(recognizer: Any, pcm: np.ndarray) -> str:
    """One offline decode of mono float32 16 kHz audio. BLOCKING; on the STT pool."""
    stream = recognizer.create_stream()
    stream.accept_waveform(SAMPLE_RATE_HZ, pcm)
    recognizer.decode_stream(stream)
    return str(stream.result.text)


def _padded(pcm: np.ndarray) -> np.ndarray:
    if pcm.size >= MIN_DECODE_SAMPLES:
        return pcm
    return np.concatenate((pcm, np.zeros(MIN_DECODE_SAMPLES - pcm.size, dtype=np.float32)))


class SherpaEngine:
    """Holds one loaded recogniser and serialises decodes onto it."""

    def __init__(self) -> None:
        self._recognizer: Any | None = None
        self._key: LoadedKey | None = None
        self._load_lock: asyncio.Lock | None = None
        self._decode_lock: asyncio.Lock | None = None
        self._last_used = 0.0
        self._busy = 0
        self._idle_evict_secs = DEFAULT_IDLE_EVICT_SECS
        self._timeout_secs = DEFAULT_TIMEOUT_SECS
        self._evict_handle: asyncio.TimerHandle | None = None

    @property
    def loaded_key(self) -> LoadedKey | None:
        return self._key if self._recognizer is not None else None

    def _locks(self) -> tuple[asyncio.Lock, asyncio.Lock]:
        # Created inside the running loop: a lock built at import time belongs to
        # whichever loop first awaits it.
        if self._load_lock is None:
            self._load_lock = asyncio.Lock()
        if self._decode_lock is None:
            self._decode_lock = asyncio.Lock()
        return self._load_lock, self._decode_lock

    def set_bounds(self, *, idle_evict_secs: int, timeout_secs: int) -> None:
        """Apply ``stt.idle_evict_secs`` and ``stt.timeout_secs`` from current config."""
        self._idle_evict_secs = max(0, int(idle_evict_secs))
        self._timeout_secs = max(1, int(timeout_secs))

    async def ensure_loaded(
        self,
        model_name: str,
        threads: int = THREADS_AUTO,
        *,
        custom_archive: str = "",
        custom_sha256: str = "",
    ) -> Availability:
        """Make *model_name* resident, downloading it first when absent.

        ``custom`` loads the owner's archive (*custom_archive*, *custom_sha256*)
        instead, which is never downloaded. Returns an :class:`Availability` rather
        than raising, so a session can turn a missing package or model into an
        error event.
        """
        probed = await asyncio.to_thread(probe)
        if not probed.ok:
            return probed
        threads = min(MAX_THREADS, thread_count(threads))
        custom = model_name == sherpa_custom.CUSTOM_MODEL
        if custom:
            digest = sherpa_custom.valid_sha256(custom_sha256)
            key = LoadedKey(f"{sherpa_custom.CUSTOM_MODEL}:{digest}", threads)
        else:
            catalog = sherpa_models.resolve(model_name)
            key = LoadedKey(catalog.name, threads)
        load_lock, decode_lock = self._locks()
        async with load_lock:
            # Decided before any hash, so a resident model costs a session nothing.
            if self._recognizer is not None and self._key == key:
                self._last_used = time.monotonic()
                return Availability(True)
            if custom:
                prepared = await self._prepare_custom(custom_archive, custom_sha256)
                if isinstance(prepared, Availability):
                    return prepared
                model = prepared
            else:
                model = catalog
                # Before the transfer: a Python without bz2 would download the
                # archive and then fail to open it.
                if not await asyncio.to_thread(
                    sherpa_models.is_present, model
                ) and not await asyncio.to_thread(sherpa_models.bz2_available):
                    return Availability(False, CODE_BZ2_MISSING, BZ2_MISSING_DETAIL)
                path = await sherpa_models.store().ensure(model)
                if path is None:
                    status = sherpa_models.store().status
                    detail = (
                        str(status.get("error") or "") or f"model {model.name} is not downloaded"
                    )
                    return Availability(False, CODE_MODEL_MISSING, detail)
            loop = asyncio.get_running_loop()
            started = time.monotonic()
            try:
                recognizer = await asyncio.wait_for(
                    loop.run_in_executor(stt_executor(), _build_recognizer, model, key.threads),
                    self._timeout_secs,
                )
            except Exception as exc:
                logger.warning("Sherpa-ONNX model %s did not load: %s", model.name, exc)
                return Availability(False, CODE_FAILED, "the speech model could not be loaded")
            async with decode_lock:
                self._recognizer = recognizer
                self._key = key
                self._last_used = time.monotonic()
            logger.info(
                "Sherpa-ONNX model %s loaded in %.2fs (%d threads)",
                model.name,
                time.monotonic() - started,
                key.threads,
            )
            return Availability(True)

    async def _prepare_custom(
        self, archive: str, sha256: str
    ) -> sherpa_models.SherpaModel | Availability:
        """The verified custom model, or the :class:`Availability` that says why not."""
        from kiro_crew.sandbox import SandboxCeilingUnsealable

        try:
            return await asyncio.wait_for(
                asyncio.to_thread(sherpa_custom.prepare_blocking, archive, sha256),
                self._timeout_secs,
            )
        except sherpa_custom.CustomModelError as exc:
            logger.warning("Custom Sherpa-ONNX model refused: %s", exc)
            return Availability(False, exc.code, str(exc))
        except SandboxCeilingUnsealable as exc:
            logger.error("Refusing to load the custom Sherpa-ONNX model: %s", exc)
            return Availability(False, sherpa_custom.CODE_CUSTOM_MISMATCH, str(exc))
        except (asyncio.TimeoutError, OSError) as exc:
            logger.warning("Custom Sherpa-ONNX model could not be prepared: %s", exc)
            return Availability(False, CODE_FAILED, "the custom speech model could not be read")

    async def decode(self, pcm: np.ndarray) -> str:
        """Transcribe mono float32 16 kHz audio. Raises :class:`DecodeFailed`.

        ``""`` means nothing was heard, or no recogniser is resident (a caller
        prepares with :meth:`ensure_loaded` first).
        """
        _, decode_lock = self._locks()
        self._busy += 1
        try:
            async with decode_lock:
                recognizer = self._recognizer
                if recognizer is None:
                    return ""
                loop = asyncio.get_running_loop()
                try:
                    return await asyncio.wait_for(
                        loop.run_in_executor(
                            stt_executor(), _decode_blocking, recognizer, _padded(pcm)
                        ),
                        self._timeout_secs,
                    )
                except asyncio.TimeoutError:
                    raise DecodeFailed("the speech recogniser timed out") from None
                except Exception as exc:
                    raise DecodeFailed(f"the speech recogniser failed: {exc}") from exc
        finally:
            self._busy -= 1
            self._last_used = time.monotonic()

    def release_soon(self) -> None:
        """Schedule the release of the recogniser once it has been idle long enough.

        Called when a session or a file is done. A later use resets the clock, and
        a decode in progress keeps it resident.
        """
        if self._evict_handle is not None:
            self._evict_handle.cancel()
            self._evict_handle = None
        if self._recognizer is None:
            return
        if self._idle_evict_secs == 0:
            self._release_if_idle()
            return
        loop = asyncio.get_running_loop()
        self._evict_handle = loop.call_later(self._idle_evict_secs, self._release_if_idle)

    def _release_if_idle(self) -> None:
        self._evict_handle = None
        if self._recognizer is None or self._busy:
            return
        idle = time.monotonic() - self._last_used
        if idle + 0.5 < self._idle_evict_secs:
            # Used again since this was scheduled; check again at the new deadline.
            loop = asyncio.get_running_loop()
            self._evict_handle = loop.call_later(
                self._idle_evict_secs - idle, self._release_if_idle
            )
            return
        logger.info("Releasing the idle Sherpa-ONNX model %s", self._key.model if self._key else "")
        self._recognizer = None
        self._key = None


_engine: SherpaEngine | None = None


def shared_engine(
    *, idle_evict_secs: int | None = None, timeout_secs: int | None = None
) -> SherpaEngine:
    """The process-wide recogniser. Bounds given here are applied to it."""
    global _engine
    if _engine is None:
        _engine = SherpaEngine()
    if idle_evict_secs is not None and timeout_secs is not None:
        _engine.set_bounds(idle_evict_secs=idle_evict_secs, timeout_secs=timeout_secs)
    return _engine
