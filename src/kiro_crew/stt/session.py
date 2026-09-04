"""A live recognition session: PCM in, partial and final transcripts out.

whisper.cpp is not a streaming recogniser. It decodes a buffer, so "live" text
has to be produced by decoding repeatedly as audio arrives, and the only real
question is *what* to re-decode. Decoding the whole utterance every time is the
obvious answer and the wrong one: cost grows with the recording, so at a
real-time factor of ~0.01 a 60 s dictation would spend 660 ms per partial and
fall behind the speaker.

So the detector that decides when the utterance ended also decides where to cut
it. On a pause too short to end the utterance, the audio so far is decoded once,
its text is *committed*, and the live buffer resets. A partial is then
``committed text + a decode of the current phrase``, which costs what the current
phrase costs rather than what the whole recording costs, and committed text never
regresses under the speaker.

Those committed decodes are for display only. The **final** transcript is one
decode of the entire buffer, so the text that reaches the user's message box has
the full context the model would have had if it had never been streamed. Partials
are fast and approximate on purpose; the final is accurate.

This module owns no socket and no HTTP. It emits :class:`SttEvent` values and
lets the transport render them, which is what makes a session testable without
one.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import numpy as np

from kiro_crew.stt import engine as engine_mod
from kiro_crew.stt import models
from kiro_crew.stt.engine import LoadedKey
from kiro_crew.stt.hallucinations import filter_hallucinations
from kiro_crew.stt.limits import DEFAULT_PARTIAL_INTERVAL_MS, MIN_PARTIAL_INTERVAL_MS
from kiro_crew.stt.vad import (
    ABSOLUTE_SILENCE_DB,
    DEFAULT_SILENCE_MS,
    FRAME_SAMPLES,
    SAMPLE_RATE_HZ,
    Endpointer,
)

logger = logging.getLogger(__name__)

#: Shortest audio whisper.cpp will accept: it returns nothing at all for less.
#: A shorter buffer is padded up to this rather than withheld (see :func:`_padded`),
#: so a first partial and a one-word command both still transcribe.
MIN_DECODE_SECS = 1.0

#: A phrase is committed once it reaches this length even without a pause, so a
#: speaker who never breathes cannot grow the partial buffer without bound. At
#: RTF ~0.01 this caps a partial decode near 90 ms.
MAX_PHRASE_SECS = 8.0

#: Audio needed before a pause is worth committing. Without a floor, the gap
#: between two words would cut a phrase in half and the model would lose the
#: context that makes the second half decode correctly.
MIN_COMMIT_SECS = 1.0

#: Ceiling on a single session's buffered audio. At 16 kHz float32 this is
#: 4 bytes per sample, so ten minutes is ~38 MB. The point is to bound a client
#: that streams forever, and the transport's own duration cap normally fires
#: first.
MAX_SESSION_SECS = 600

#: Advisory English for a final decode that failed. Advisory because the machine
#: readable ``code`` beside it is the contract the dashboard renders from; this text
#: is what a client with no catalog entry for that code shows instead, and what the
#: log carries. Names retrying as the action because the fault is per-decode: the
#: model stays resident and the next utterance normally succeeds.
DECODE_FAILED_ADVISORY = "the speech recogniser could not finish this transcript"


def _padded(audio: np.ndarray) -> np.ndarray:
    """Extend *audio* with silence up to the recogniser's minimum length.

    whisper.cpp returns nothing for a buffer under a second, so a genuinely short
    utterance ("stop", "yes") would otherwise be dropped rather than transcribed.
    Padding costs a few milliseconds of decode and preserves the word.
    """
    minimum = int(MIN_DECODE_SECS * SAMPLE_RATE_HZ)
    if audio.size >= minimum:
        return audio
    return np.concatenate((audio, np.zeros(minimum - audio.size, dtype=np.float32)))


def _is_audible(audio: np.ndarray) -> bool:
    """Whether *audio* carries anything above the digital-silence floor.

    Measures the loudest 20 ms frame rather than the whole buffer's average, so a
    single spoken word inside a long quiet recording still counts. A muted device,
    an unplugged input and a stream of exact zeros all fall below the floor.
    """
    usable = audio.size - (audio.size % FRAME_SAMPLES)
    if usable <= 0:
        return bool(audio.size) and float(np.abs(audio).max()) > 0.0
    frames = audio[:usable].reshape(-1, FRAME_SAMPLES)
    rms = np.sqrt(np.mean(np.square(frames, dtype=np.float64), axis=1) + 1e-20)
    peak_db = 20.0 * np.log10(float(rms.max()) + 1e-20)
    return peak_db > ABSOLUTE_SILENCE_DB


@dataclass(frozen=True)
class SttEvent:
    """One thing that happened in a session, in transport-neutral form.

    ``kind`` is the discriminator and mirrors the websocket frame names the
    dashboard already speaks, so the transport is a rename rather than a
    translation. ``code`` carries a machine-readable reason on an error or a
    status, because the dashboard renders localised text and cannot key off an
    English sentence.
    """

    kind: str
    text: str = ""
    code: str = ""
    stage: str = ""
    downloaded_bytes: int = 0
    total_bytes: int = 0


KIND_STATUS = "status"
KIND_PARTIAL = "partial"
KIND_FINAL = "final"
KIND_ERROR = "error"

STAGE_DOWNLOADING = "downloading"
STAGE_READY = "ready"


@dataclass
class _Buffers:
    """Audio held by a session, split by what has already been decoded for display.

    The two lists and the two counts move together, which is why growing them is a
    method rather than four statements at the call site: an edit that appended to
    one list and forgot a count would leave the phrase length disagreeing with the
    phrase audio, and every cut decision reads that length.
    """

    #: Everything heard so far, including the current phrase. The final decodes this.
    full: list[np.ndarray] = field(default_factory=list)
    #: The current phrase, since the last commit. Partials decode this.
    phrase: list[np.ndarray] = field(default_factory=list)
    phrase_samples: int = 0
    total_samples: int = 0

    def append(self, pcm: np.ndarray) -> None:
        """Add *pcm* to both buffers and keep the counts in step."""
        self.full.append(pcm)
        self.phrase.append(pcm)
        self.phrase_samples += pcm.size
        self.total_samples += pcm.size

    def take_phrase(self) -> list[np.ndarray]:
        """Return the current phrase's audio and start a new one."""
        phrase, self.phrase, self.phrase_samples = self.phrase, [], 0
        return phrase


class LocalSession:
    """Drives the resident recogniser over a live audio stream.

    One session per client connection. Not thread-safe and not re-entrant: it is
    driven from the single task that owns the socket.

    A session spans MANY utterances. The detector finalising one does not end the
    session: it emits that utterance's final, resets, and listens for the next. This
    matches what the other two providers do behind the same socket, and both clients
    are built for it -- each accumulates finals (`useStreamingStt`,
    `useMeetingTranscription`) rather than treating the first as the end.

    Ending the session on the detector instead was the anomaly, and it broke the
    continuous consumer outright: the server closed the socket on the speaker's
    first pause, `useMeetingTranscription`'s `onclose` reported "disconnected" and
    ran its cleanup (mic tracks stopped, AudioContext closed), and because its
    session binding keys only on `status` -- which had not changed -- nothing
    restarted it. Transcription stayed dead for the rest of the meeting while the UI
    still showed Live. Only the client (a `stop` frame, a close) or a resource
    ceiling ends a session.
    """

    def __init__(
        self,
        *,
        model_name: str = models.DEFAULT_MODEL,
        language: str = "",
        silence_ms: int = DEFAULT_SILENCE_MS,
        partial_interval_ms: int = DEFAULT_PARTIAL_INTERVAL_MS,
        idle_evict_secs: int = engine_mod.DEFAULT_IDLE_EVICT_SECS,
        timeout_secs: int = engine_mod.DEFAULT_TIMEOUT_SECS,
    ) -> None:
        self._model_name = model_name
        self._language = language
        self._partial_interval = max(MIN_PARTIAL_INTERVAL_MS / 1000.0, partial_interval_ms / 1000.0)
        self._engine = engine_mod.shared_engine(
            idle_evict_secs=idle_evict_secs, timeout_secs=timeout_secs
        )
        # Kept so each utterance gets a FRESH detector: `Endpointer`'s ENDED state is
        # terminal by design (it must not re-arm under a client that keeps sending
        # after a finalise), so continuing means replacing it, not resetting it.
        self._silence_ms = silence_ms
        self._endpointer = Endpointer(silence_ms=silence_ms)
        self._buffers = _Buffers()
        self._committed = ""
        self._last_partial = 0.0
        self._cancelled = False
        self._ended = False
        # Set by prepare(); every decode carries it so a concurrent session
        # cannot retarget this one's model or language unnoticed.
        self._key: LoadedKey | None = None

    @property
    def ended(self) -> bool:
        """Whether the SESSION is over, i.e. no further audio will be accepted.

        True only after :meth:`finish` or :meth:`cancel`. Deliberately not set by the
        detector finalising an utterance: see the class docstring.
        """
        return self._ended

    @property
    def has_pending_audio(self) -> bool:
        """Whether anything worth decoding has arrived since the last final.

        What a transport's teardown needs to know, and NOT the same question as "has
        a final been sent". Over a multi-utterance session both are true at once: the
        detector finalises utterance one, the speaker starts utterance two, and the
        client then stops. Deciding on "a final already went out" discards that
        second utterance; deciding on this decodes it.

        Measures audibility rather than sample count, because the frames that arrive
        immediately after a finalise are the trailing silence that CAUSED it. Counting
        those would make this true almost always and spend a full-buffer decode on
        room tone at the end of every session.
        """
        if self._buffers.total_samples == 0:
            return False
        return bool(_is_audible(self._joined(self._buffers.full)))

    def pending_download(self) -> models.WhisperModel | None:
        """The model this session must fetch before it can run, or ``None``.

        Asked BEFORE :meth:`prepare`, because a transport has to say "downloading,
        148 MB" while the transfer is happening rather than after it: a silent
        multi-hundred-megabyte fetch is indistinguishable from a hang, which is the
        worst moment in a first-run voice experience. ``prepare`` cannot report it
        itself, since it only returns once the transfer it is waiting on is done.
        """
        model = models.resolve(self._model_name)
        return None if models.is_present(model) else model

    async def prepare(self) -> list[SttEvent]:
        """Make the recogniser ready, fetching the model if this is a first run.

        Returns the events a transport must relay, which is an error and nothing
        else: an empty list means the session is ready. Progress is deliberately
        not reported through the return value (see :meth:`pending_download`); live
        byte counts come from the model store's status instead.
        """
        events: list[SttEvent] = []
        result = await self._engine.ensure_loaded(self._model_name, self._language)
        if not result.ok:
            events.append(SttEvent(KIND_ERROR, text=result.detail, code=result.code))
            return events
        self._key = self._engine.loaded_key
        return events

    async def feed(self, raw_int16: bytes) -> list[SttEvent]:
        """Consume one chunk of little-endian int16 PCM and return what to emit.

        Returns at most one partial per call, or the utterance's final when the
        detector reports the speaker stopped. A final does NOT end the session: the
        caller keeps reading audio, and the next utterance starts a new one.
        """
        if self._cancelled or self._ended:
            return []
        pcm = engine_mod.pcm_from_int16(raw_int16)
        if pcm.size == 0:
            return []

        if self._buffers.total_samples + pcm.size > MAX_SESSION_SECS * SAMPLE_RATE_HZ:
            logger.info("Voice session hit its %ds audio ceiling; finalising", MAX_SESSION_SECS)
            return [await self.finish()]

        # Pushed BEFORE the buffer append, because a chunk that ends the utterance has
        # to be SPLIT rather than filed whole. The caller's chunk is much longer than a
        # detector frame (a browser sends ~100 ms against 20 ms), so the chunk carrying
        # the endpoint routinely carries the start of the next utterance too. Appending
        # it first put a resumed word into the utterance that just closed -- where it
        # lands after a hangover of silence and contributes nothing -- and clipped that
        # word's onset off the utterance it actually belongs to.
        update = self._endpointer.push(pcm)
        if update.ended:
            tail = update.pending
            self._buffers.append(pcm[: pcm.size - tail.size])
            event = await self._finalise_utterance()
            if tail.size:
                # Into the RE-ARMED buffers and detector `_finalise_utterance` just
                # built. The push's verdict is deliberately discarded: a tail is under
                # one chunk, and ending an utterance needs MIN_SPEECH_FRAMES of speech
                # followed by a silence run, so it cannot close one on its own.
                self._buffers.append(tail)
                self._endpointer.push(tail)
            return [event]

        self._buffers.append(pcm)

        if not self._endpointer.speech_frames_seen:
            # Nothing has been recognised as speech yet, so there is nothing to
            # show and nothing worth decoding. Gated on the DETECTOR here, not on
            # the audibility test finish() uses, and the asymmetry is deliberate:
            # a partial is cosmetic and repeatable, so a conservative gate costs at
            # most a late first partial, while room tone that slipped past it would
            # put an invented sentence on screen. finish() takes the opposite
            # trade, because the text it returns is the text the user keeps.
            return []

        phrase_secs = self._buffers.phrase_samples / SAMPLE_RATE_HZ
        # A gap in the audio right now is the best place to cut, because the model
        # decodes a phrase far better than it decodes half of one. Reads the
        # frame-level ``silent``, not the utterance-level ``speech``, which stays
        # true across exactly these pauses. The length ceiling is the fallback for
        # a speaker who never pauses, so the partial buffer is bounded either way.
        if phrase_secs >= MAX_PHRASE_SECS or (update.silent and phrase_secs >= MIN_COMMIT_SECS):
            if await self._commit_phrase():
                # Report the text the commit just confirmed. A commit is the moment
                # a phrase became settled, so staying silent here would leave the
                # user watching a stalled box at exactly the point most was learned.
                self._last_partial = time.monotonic()
                return [SttEvent(KIND_PARTIAL, text=self._committed)]
            return []

        now = time.monotonic()
        if now - self._last_partial < self._partial_interval:
            return []
        phrase = self._joined(self._buffers.phrase)
        if phrase.size == 0:
            return []
        self._last_partial = now
        try:
            text = await self._engine.decode(_padded(phrase), superseding=True, expect=self._key)
        except engine_mod.DecodeFailed as exc:
            # A partial is cosmetic and the next one is moments away, so one failed
            # decode must not end a session the speaker is still talking into. Logged
            # rather than surfaced: only the FINAL is text the user keeps, so that is
            # the one whose failure they have to be told about.
            logger.warning("Partial decode failed: %s", exc.detail)
            return []
        if not text:
            # An empty result here is a superseded decode, not silence: newer
            # audio arrived and aborted it. Emitting an empty partial would blank
            # the user's textbox mid-sentence.
            return []
        return [SttEvent(KIND_PARTIAL, text=self._with_committed(text))]

    async def finish(self) -> SttEvent:
        """End the SESSION: decode what is buffered and accept no more audio.

        Called when the client stops (a ``stop`` frame, a close) or a resource
        ceiling fires, NOT when the detector finalises an utterance -- that is
        :meth:`_finalise_utterance`, which leaves the session running.
        """
        self._ended = True
        event = await self._decode_utterance()
        # Only on the terminal path. Per utterance this would release the model in
        # the middle of a meeting whenever the idle window is short.
        await self._engine.maybe_evict()
        return event

    async def _finalise_utterance(self) -> SttEvent:
        """Emit the current utterance's final and re-arm for the next one.

        The session stays open. Everything utterance-scoped is reset -- the audio,
        the committed display text and the detector -- because the next utterance
        must not decode with this one's audio in front of it or its text prefixed to
        the result.
        """
        event = await self._decode_utterance()
        self._buffers = _Buffers()
        self._committed = ""
        self._last_partial = 0.0
        self._endpointer = Endpointer(silence_ms=self._silence_ms)
        return event

    async def _decode_utterance(self) -> SttEvent:
        """Decode everything buffered for the current utterance.

        This is a full-buffer decode rather than the committed partials joined
        together, so the model sees the whole utterance and the text the user
        keeps is not a concatenation of context-free fragments.

        Answers a ``final`` -- possibly an empty one, which means nothing was heard --
        or an ``error`` when the decode itself failed. Those are different outcomes
        with different remedies, and reporting the second as the first is what made a
        failed decode look like a silent room.
        """
        if self._cancelled:
            return SttEvent(KIND_FINAL, text="")
        audio = self._joined(self._buffers.full)
        if not _is_audible(audio):
            # A muted or unplugged device delivers digital silence. Decoding it
            # would invite the model to invent a sentence out of nothing.
            #
            # Deliberately a measurement of the audio, NOT a read of the
            # detector's state. The detector can legitimately fail to latch onto
            # speech (a session whose very first frame is already mid-word seeds
            # its noise floor at speech level and needs a gap to converge), and
            # treating that as "nothing was said" would discard a real utterance.
            # Losing genuine speech is the worse failure: a hallucination on a
            # silent recording is what filter_hallucinations below exists for,
            # whereas a dropped sentence leaves no trace at all.
            return SttEvent(KIND_FINAL, text="")
        padded = _padded(audio)
        try:
            text = await self._engine.decode(padded, expect=self._key)
            if not text:
                # The engine refuses a decode whose model was replaced by a concurrent
                # session (an operator changing `stt.model` mid-meeting is enough), and
                # that refusal is indistinguishable here from silence: it returns "",
                # the transport drops an empty final, and the utterance the user just
                # spoke leaves no trace at all.
                #
                # So re-prepare and try once, which is the same bounded retry
                # `transcribe_pcm` already applies for the same reason. Bounded
                # deliberately: looping would let two sessions with different settings
                # starve each other indefinitely, and a genuinely empty transcript must
                # still be allowed to be empty.
                result = await self._engine.ensure_loaded(self._model_name, self._language)
                if result.ok:
                    self._key = self._engine.loaded_key
                    text = await self._engine.decode(padded, expect=self._key)
        except engine_mod.DecodeFailed as exc:
            # The one decode whose failure the user has to be told about: this is the
            # text they keep. Reported as an `error` event rather than an empty final
            # because the transport drops an empty final, so returning one is exactly
            # how a failed decode became a dictation that vanished with no feedback.
            logger.warning("Final transcript decode failed: %s", exc.detail)
            return SttEvent(KIND_ERROR, text=DECODE_FAILED_ADVISORY, code=exc.code)
        # Only the final is filtered. A partial is a prefix of speech still in
        # progress, so collapsing a repetition there could delete one the speaker
        # had only begun.
        return SttEvent(KIND_FINAL, text=filter_hallucinations(text))

    def cancel(self) -> None:
        """Abandon the session. Any in-flight decode is left to abort on its own."""
        self._cancelled = True
        self._ended = True

    async def _commit_phrase(self) -> bool:
        """Fold the current phrase into the committed display text.

        Returns whether the committed text grew, so the caller knows whether there
        is anything new to show. A phrase carrying no audible content is discarded
        without a decode: transcribing room tone spends a decode to learn nothing
        and invites a hallucination into text the user cannot see being formed.
        """
        phrase = self._joined(self._buffers.take_phrase())
        if not _is_audible(phrase):
            return False
        try:
            text = await self._engine.decode(_padded(phrase), expect=self._key)
        except engine_mod.DecodeFailed as exc:
            # Display-only text, so the same trade as a partial: skip this phrase and
            # keep the session alive. The phrase audio is still in the full buffer, so
            # the final decodes it with everything else and nothing is lost from the
            # text the user keeps.
            logger.warning("Phrase commit decode failed: %s", exc.detail)
            return False
        if not text:
            return False
        self._committed = f"{self._committed} {text}".strip()
        return True

    def _with_committed(self, text: str) -> str:
        return f"{self._committed} {text}".strip() if self._committed else text

    @staticmethod
    def _joined(chunks: list[np.ndarray]) -> np.ndarray:
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        if len(chunks) == 1:
            return chunks[0]
        return np.concatenate(chunks)


async def transcribe_pcm(
    pcm: np.ndarray,
    *,
    model_name: str = models.DEFAULT_MODEL,
    language: str = "",
    idle_evict_secs: int = engine_mod.DEFAULT_IDLE_EVICT_SECS,
    timeout_secs: int = engine_mod.DEFAULT_TIMEOUT_SECS,
) -> tuple[str, engine_mod.Availability]:
    """One-shot decode of complete audio, for the batch path.

    Shares the resident model with live sessions, which is the whole point: a
    Slack voice memo and a dashboard dictation both land on a model that is
    already loaded.
    """
    eng = engine_mod.shared_engine(idle_evict_secs=idle_evict_secs, timeout_secs=timeout_secs)
    result = await eng.ensure_loaded(model_name, language)
    if not result.ok:
        return "", result
    # Padded like the live paths: whisper.cpp returns nothing for a buffer under a
    # second, so a one-word voice memo ("stop", "yes") would otherwise transcribe
    # to nothing at all rather than to the word that was said.
    #
    # Retried ONCE, because the engine refuses a decode whose model was replaced by
    # a concurrent session between the prepare above and this call. A single retry
    # covers that; looping would let two sessions with different settings starve
    # each other indefinitely.
    padded = _padded(pcm)
    try:
        text = await eng.decode(padded, expect=eng.loaded_key)
        if not text:
            result = await eng.ensure_loaded(model_name, language)
            if not result.ok:
                return "", result
            text = await eng.decode(padded, expect=eng.loaded_key)
    except engine_mod.DecodeFailed as exc:
        # Reported through the Availability this function already returns, so the batch
        # caller's existing "unavailable" branch names the real reason instead of
        # reporting a memo it could not hear. Returned rather than raised for the same
        # reason every other failure here is: the caller is a channel handler, and an
        # exception out of a voice memo would take the message with it.
        logger.warning("Batch transcript decode failed: %s", exc.detail)
        return "", engine_mod.Availability(False, exc.code, exc.detail)
    await eng.maybe_evict()
    return filter_hallucinations(text), result


async def prewarm(
    *,
    model_name: str = models.DEFAULT_MODEL,
    language: str = "",
) -> engine_mod.Availability:
    """Load and warm the recogniser ahead of a user actually speaking."""
    return await engine_mod.shared_engine().prewarm(model_name, language)


async def close() -> None:
    """Release the resident model. Called from the gateway's shutdown path."""
    await engine_mod.shared_engine().close()


# Kept so a caller can await a download without opening a session (the settings
# panel's "get it ready now" action).
async def ensure_model(model_name: str) -> bool:
    model = models.resolve(model_name)
    return (await models.store().ensure(model)) is not None


__all__ = [
    "DECODE_FAILED_ADVISORY",
    "KIND_ERROR",
    "KIND_FINAL",
    "KIND_PARTIAL",
    "KIND_STATUS",
    "LocalSession",
    "STAGE_DOWNLOADING",
    "STAGE_READY",
    "SttEvent",
    "close",
    "ensure_model",
    "prewarm",
    "transcribe_pcm",
]
