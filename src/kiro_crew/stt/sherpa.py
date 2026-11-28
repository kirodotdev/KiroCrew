"""The ``sherpa_onnx`` speech provider: live sessions and files on a resident Zipformer.

The model is an OFFLINE transducer: it decodes a whole buffer. Live text therefore
comes from the energy endpointer (:class:`kiro_crew.stt.vad.Endpointer`) cutting the
stream into utterances, each decoded once for its ``final``, with the open utterance
re-decoded for a ``partial`` while the speaker talks. At about a hundredth of real
time a re-decode is cheap, so there is no phrase-commit machinery like
:mod:`kiro_crew.stt.session` needs for whisper.

:class:`StreamingSession` has the ``SttStreamSession`` shape and is driven by the
dashboard's helper-session loop; :func:`transcribe_pcm` serves the batch path, which
``transcribe_local._transcribe_sherpa`` feeds with decoded audio.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator

import numpy as np

from kiro_crew.stt import sherpa_custom, sherpa_engine, sherpa_models
from kiro_crew.stt.engine import Availability, pcm_from_int16
from kiro_crew.stt.limits import DEFAULT_PARTIAL_INTERVAL_MS, MIN_PARTIAL_INTERVAL_MS
from kiro_crew.stt.session import _is_audible
from kiro_crew.stt.vad import DEFAULT_SILENCE_MS, Endpointer

logger = logging.getLogger(__name__)

#: The ``stt.provider`` value that selects this provider.
PROVIDER = "sherpa_onnx"

CODE_EXTRA_MISSING = sherpa_engine.CODE_EXTRA_MISSING
CODE_NO_WHEEL = sherpa_engine.CODE_NO_WHEEL
CODE_IMPORT_FAILED = sherpa_engine.CODE_IMPORT_FAILED
CODE_BZ2_MISSING = sherpa_engine.CODE_BZ2_MISSING
CODE_MODEL_MISSING = sherpa_engine.CODE_MODEL_MISSING
CODE_FAILED = sherpa_engine.CODE_FAILED
CODE_CUSTOM_MISSING = sherpa_custom.CODE_CUSTOM_MISSING
CODE_CUSTOM_MISMATCH = sherpa_custom.CODE_CUSTOM_MISMATCH
CODE_CUSTOM_UNSUPPORTED = sherpa_custom.CODE_CUSTOM_UNSUPPORTED
#: Every code a session's ``error`` event can carry; the dashboard has text for each.
SESSION_ERROR_CODES = frozenset(
    {
        CODE_EXTRA_MISSING,
        CODE_NO_WHEEL,
        CODE_IMPORT_FAILED,
        CODE_BZ2_MISSING,
        CODE_MODEL_MISSING,
        CODE_FAILED,
        CODE_CUSTOM_MISSING,
        CODE_CUSTOM_MISMATCH,
        CODE_CUSTOM_UNSUPPORTED,
    }
)

#: An utterance is finalised at this length even while the speaker keeps talking,
#: which bounds both the final's decode and every partial's.
MAX_UTTERANCE_MS = 30_000

_FINISH = object()


def availability(stt_config: Any = None) -> Availability:
    """Whether the provider can run: the package imports. BLOCKING.

    A model that is not on disk is not "unavailable": it downloads on first use,
    the same answer the ``local`` provider gives. That download needs bz2, so with
    *stt_config* given and its model absent, a Python without bz2 is refused here.
    """
    probed = sherpa_engine.probe()
    if not probed.ok or stt_config is None:
        return probed
    name = getattr(stt_config, "sherpa_model", "")
    if name == sherpa_custom.CUSTOM_MODEL:
        # A custom archive does not appear on its own, so its absence is reported;
        # whether it matches its digest is answered by the load, which hashes it.
        archive = getattr(stt_config, "sherpa_custom_archive", "")
        if not sherpa_custom.archive_present(archive):
            return Availability(
                False,
                CODE_CUSTOM_MISSING,
                f"put {archive or 'the archive'} in {sherpa_custom.custom_dir()}",
            )
        present = sherpa_custom.is_unpacked(getattr(stt_config, "sherpa_custom_sha256", ""))
    else:
        present = sherpa_models.is_present(sherpa_models.resolve(name))
    if not present and not sherpa_models.bz2_available():
        return Availability(False, sherpa_engine.CODE_BZ2_MISSING, sherpa_engine.BZ2_MISSING_DETAIL)
    return probed


def tidy(text: str) -> str:
    """The model's words with whitespace folded; all-caps output made a sentence.

    Most of these models emit upper-case words with no punctuation, which are
    lower-cased with the first letter capitalised. Text that already has case, or
    has none (Chinese, Japanese, Thai), is left as the model wrote it.
    """
    words = " ".join(str(text).split())
    if words == words.upper() and words != words.lower():
        words = words.lower()
        return words[:1].upper() + words[1:]
    return words


def _custom(stt_config: Any) -> dict[str, str]:
    """The custom-archive fields the engine needs when ``sherpa_model`` is ``custom``."""
    return {
        "custom_archive": str(getattr(stt_config, "sherpa_custom_archive", "") or ""),
        "custom_sha256": str(getattr(stt_config, "sherpa_custom_sha256", "") or ""),
    }


def _engine_for(stt_config: Any) -> sherpa_engine.SherpaEngine:
    return sherpa_engine.shared_engine(
        idle_evict_secs=int(getattr(stt_config, "idle_evict_secs", 600)),
        timeout_secs=int(getattr(stt_config, "timeout_secs", 300)),
    )


def _new_endpointer(silence_ms: int) -> Endpointer:
    return Endpointer(silence_ms=silence_ms, max_utterance_ms=MAX_UTTERANCE_MS)


def split_utterances(pcm: np.ndarray, silence_ms: int = DEFAULT_SILENCE_MS) -> list[np.ndarray]:
    """Cut a whole recording into utterances with the live path's endpointer.

    Every sample lands in exactly one piece, so nothing is dropped between them.
    """
    pieces: list[np.ndarray] = []
    endpointer = _new_endpointer(silence_ms)
    rest = pcm
    while rest.size:
        update = endpointer.push(rest)
        if not update.ended:
            pieces.append(rest)
            break
        tail = update.pending
        pieces.append(rest[: rest.size - tail.size])
        rest = tail
        endpointer = _new_endpointer(silence_ms)
    return [p for p in pieces if p.size]


class StreamingSession:
    """The ordinary STT session contract over the resident recogniser."""

    def __init__(self, stt_config: Any) -> None:
        self._stt_config = stt_config
        self._model = str(getattr(stt_config, "sherpa_model", ""))
        self._threads = int(getattr(stt_config, "threads", 0))
        self._silence_ms = int(getattr(stt_config, "silence_ms", DEFAULT_SILENCE_MS))
        interval_ms = int(getattr(stt_config, "partial_interval_ms", DEFAULT_PARTIAL_INTERVAL_MS))
        self._partial_interval = max(MIN_PARTIAL_INTERVAL_MS, interval_ms) / 1000.0
        self._partials = bool(getattr(stt_config, "streaming", True))
        self._engine = _engine_for(stt_config)
        self._audio: asyncio.Queue[object] = asyncio.Queue()
        self._events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._failure: tuple[str, str] | None = None
        self._closed = False
        self._finishing = False
        self._endpointer = _new_endpointer(self._silence_ms)
        self._utterance: list[np.ndarray] = []
        self._last_partial_at = 0.0
        self._last_partial = ""

    @property
    def failure_code(self) -> str | None:
        """The code of the failure that ended or refused this session, if any."""
        return self._failure[0] if self._failure else None

    def _fail(self, code: str, message: str) -> None:
        if self._failure is None:
            self._failure = (code, message)
            self._events.put_nowait({"type": "error", "message": message, "code": code})

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        while (event := await self._events.get()) is not None:
            yield event
            if event.get("type") == "error":
                return

    async def start(self) -> str | None:
        result = await self._engine.ensure_loaded(
            self._model, self._threads, **_custom(self._stt_config)
        )
        if not result.ok:
            self._failure = (result.code or CODE_FAILED, result.detail)
            return result.detail or "the speech model is not available"
        self._worker = asyncio.create_task(self._run())
        return None

    async def feed(self, pcm: bytes) -> bool:
        if self._closed or self._finishing:
            return False
        if self._failure is not None:
            # The error event already carries the cause and the relay ends the
            # session on it; answering False here would add a second, vaguer frame.
            return True
        if pcm:
            self._audio.put_nowait(pcm)
        return True

    async def finish(self) -> None:
        if self._closed or self._finishing:
            return
        self._finishing = True
        self._audio.put_nowait(_FINISH)
        if self._worker is not None:
            try:
                await self._worker
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Sherpa-ONNX session worker failed", exc_info=True)
        self._events.put_nowait(None)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        worker = self._worker
        if worker is not None and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        self._events.put_nowait(None)
        self._engine.release_soon()

    # worker

    async def _run(self) -> None:
        while True:
            item = await self._audio.get()
            if item is _FINISH:
                await self._finalise()
                return
            if not isinstance(item, bytes):
                continue
            if not await self._consume(pcm_from_int16(item)):
                return

    async def _consume(self, pcm: np.ndarray) -> bool:
        """Push *pcm* through the endpointer; finalise each utterance it closes."""
        if pcm.size == 0:
            return True
        update = self._endpointer.push(pcm)
        while update.ended:
            tail = update.pending
            self._utterance.append(pcm[: pcm.size - tail.size])
            if not await self._finalise():
                return False
            self._endpointer = _new_endpointer(self._silence_ms)
            pcm = tail
            if not pcm.size:
                return True
            update = self._endpointer.push(pcm)
        self._utterance.append(pcm)
        await self._maybe_partial()
        return True

    async def _maybe_partial(self) -> None:
        if not self._partials or not self._endpointer.speech_frames_seen:
            return
        if not self._audio.empty():
            # Behind the speaker: spend the time on audio, not on display text.
            return
        now = time.monotonic()
        if now - self._last_partial_at < self._partial_interval:
            return
        self._last_partial_at = now
        try:
            text = tidy(await self._engine.decode(np.concatenate(self._utterance)))
        except sherpa_engine.DecodeFailed as exc:
            logger.warning("Sherpa-ONNX partial decode failed: %s", exc)
            return
        if text and text != self._last_partial:
            self._last_partial = text
            self._events.put_nowait({"type": "partial", "text": text})

    async def _finalise(self) -> bool:
        """Decode the open utterance and emit its final. False once the session failed."""
        audio = np.concatenate(self._utterance) if self._utterance else np.zeros(0, np.float32)
        self._utterance = []
        self._last_partial = ""
        self._last_partial_at = 0.0
        if not audio.size or not _is_audible(audio):
            return True
        try:
            text = tidy(await self._engine.decode(audio))
        except sherpa_engine.DecodeFailed as exc:
            logger.warning("Sherpa-ONNX final decode failed: %s", exc)
            self._fail(CODE_FAILED, "the speech recogniser could not finish this transcript")
            return False
        if text:
            self._events.put_nowait({"type": "final", "text": text})
        return True


async def transcribe_pcm(pcm: np.ndarray, stt_config: Any) -> str | None:
    """Transcribe a whole recording: text, ``""`` for silence, ``None`` on failure."""
    if not _is_audible(pcm):
        return ""
    engine = _engine_for(stt_config)
    result = await engine.ensure_loaded(
        str(getattr(stt_config, "sherpa_model", "")),
        int(getattr(stt_config, "threads", 0)),
        **_custom(stt_config),
    )
    if not result.ok:
        logger.error("Sherpa-ONNX speech recognition unavailable: %s", result.detail)
        return None
    silence_ms = int(getattr(stt_config, "silence_ms", DEFAULT_SILENCE_MS))
    texts: list[str] = []
    try:
        for piece in split_utterances(pcm, silence_ms):
            if not _is_audible(piece):
                continue
            text = " ".join((await engine.decode(piece)).split())
            if text:
                texts.append(text)
    except sherpa_engine.DecodeFailed as exc:
        logger.error("Sherpa-ONNX file decode failed: %s", exc)
        return None
    finally:
        engine.release_soon()
    return tidy(" ".join(texts))
