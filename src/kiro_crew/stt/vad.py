"""Streaming voice-activity detection and utterance endpointing.

This decides *when the speaker stopped*, which is a different job from the
recogniser's own silence trimming: it runs on the incoming stream, ahead of any
decode, so a live session can finalise the moment speech ends instead of waiting
for the client to say it is done.

The detector is an adaptive-RMS one rather than a neural VAD, and that is a
deliberate trade. A fixed dBFS threshold cannot work across a laptop's built-in
microphone, a headset and a noisy room; a neural VAD would work but costs a
second model download and another native dependency for a decision that is
already unambiguous in practice. On real 16 kHz speech the separation measured
here is ~36 dB (speech at the 90th percentile of frame energy sits near
-15 dBFS, the noise floor near -51 dBFS), so tracking the floor and requiring a
margin above it is both sufficient and cheap: the whole detector is a handful of
numpy operations per 20 ms frame.

Everything here is pure numpy and synchronous. It holds no I/O and no lock, so a
caller may drive it straight from a socket read.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

import numpy as np

from kiro_crew.stt.limits import DEFAULT_SILENCE_MS, MIN_SILENCE_MS

logger = logging.getLogger(__name__)

#: Analysis frame, in milliseconds. 20 ms is the shortest window that still
#: gives a stable RMS for speech (a full pitch period even for a low voice) and
#: it divides 16 kHz exactly, so frames never straddle a sample.
FRAME_MS = 20

#: Sample rate every caller must feed. whisper.cpp is trained at this rate and
#: the dashboard's audio worklet already downsamples to it, so resampling never
#: enters the path. A caller handing over any other rate is a programming error,
#: not a runtime condition to recover from.
SAMPLE_RATE_HZ = 16000

#: Samples per analysis frame.
FRAME_SAMPLES = SAMPLE_RATE_HZ * FRAME_MS // 1000

#: How far above the tracked noise floor a frame must sit to count as speech.
#: Measured separation between speech and floor on real input is ~36 dB, so 12 dB
#: leaves a wide margin on both sides: high enough that room tone, fan noise and
#: the microphone's own hiss stay below it, low enough that a quiet talker or a
#: trailing unvoiced consonant still registers.
SPEECH_MARGIN_DB = 12.0

#: Floor tracking. The floor must fall quickly onto a newly quiet room but rise
#: only very slowly, otherwise sustained speech drags it up until the speaker's
#: own voice stops clearing the margin and the utterance ends mid-sentence.
#: These are per-frame smoothing coefficients on the dB estimate.
FLOOR_FALL_COEFF = 0.5
FLOOR_RISE_COEFF = 0.002

#: Sentinel meaning "no floor estimate yet". The floor is seeded from the FIRST
#: frame of real audio rather than from a constant, because no constant works: a
#: fixed low value leaves ordinary room tone sitting more than the speech margin
#: above it, so a session opening in a noisy room reads as continuous speech and
#: the utterance never ends. Seeding from the first frame makes the floor correct
#: by construction for a quiet start, and the fast-fall coefficient converges it
#: within a few frames when the session happens to open mid-word.
_FLOOR_UNSET = float("inf")

#: A frame quieter than this is treated as digital silence and always updates the
#: floor, whatever the smoothing says. Without it a stream of exact zeros (a muted
#: or unplugged device) leaves the floor wherever it was and every subsequent
#: frame reads as speech.
ABSOLUTE_SILENCE_DB = -70.0

#: Consecutive speech frames required to enter the speaking state. A single loud
#: frame is a key click, a desk bump or a plosive on the mic housing, and
#: admitting one would start an utterance the speaker never began.
MIN_SPEECH_FRAMES = 6

#: dBFS window the UI level meter is mapped from. Below the floor of this range a
#: meter reads empty; above the top it saturates. Chosen to put ordinary speech in
#: the upper half of the bar rather than pinned at either end.
LEVEL_FLOOR_DB = -60.0
LEVEL_CEILING_DB = -10.0

#: Guard against a stream that never goes quiet: a hot mic in a noisy room, or a
#: client that opened a session and walked away. At this point the utterance is
#: finalised regardless of energy, so a session always terminates on its own.
DEFAULT_MAX_UTTERANCE_MS = 120_000


class VadState(Enum):
    """Where an utterance is in its lifecycle."""

    #: No qualifying speech seen yet.
    IDLE = "idle"
    #: Speech is in progress.
    SPEECH = "speech"
    #: Speech was seen and has since stopped, or the utterance hit its ceiling.
    ENDED = "ended"


#: Shared empty buffer for :attr:`VadUpdate.pending`. One instance, since it is only
#: ever read and a frozen dataclass hands it out by reference.
_NO_SAMPLES: np.ndarray = np.empty(0, dtype=np.float32)


@dataclass(frozen=True)
class VadUpdate:
    """The detector's verdict after consuming a chunk of audio.

    ``level`` is a 0.0-1.0 meter value for the UI, taken from the loudest frame
    in the chunk so a meter driven from it tracks peaks rather than averaging
    them away. ``ended`` is a one-shot: it is true on the update that closed the
    utterance and false afterwards, so a caller can act on the transition
    without tracking previous state itself.

    ``speech`` and ``silent`` are NOT complements, and the difference is the
    useful part. ``speech`` is the utterance-level state, which stays true across
    the pauses between clauses, which is what stops a breath from ending a
    sentence. ``silent`` is the frame-level reading for the end of this chunk, so
    it goes true during those same pauses. A caller wanting "is the speaker
    mid-utterance" reads ``speech``; one wanting "is there a gap right now, a good
    place to cut" reads ``silent``.
    """

    level: float
    speech: bool
    ended: bool
    state: VadState
    silent: bool = False
    #: Audio from this chunk the detector had NOT consumed when the utterance closed:
    #: everything after the frame that ended it. Empty unless ``ended``.
    #:
    #: A caller's chunk is far longer than one frame -- a browser sends ~100 ms against
    #: a 20 ms frame -- so the chunk carrying the endpoint routinely carries the start
    #: of the NEXT utterance as well. Handing that audio back is what lets it begin the
    #: next utterance instead of being filed under the one that just closed, where it
    #: sits after a hangover of silence and is clipped off the word it belongs to.
    pending: np.ndarray = field(default_factory=lambda: _NO_SAMPLES)


def _frame_db(frames: np.ndarray) -> np.ndarray:
    """Return per-frame energy in dBFS for a ``(n_frames, FRAME_SAMPLES)`` block.

    The epsilon keeps a frame of exact zeros finite; it lands well below
    ``ABSOLUTE_SILENCE_DB`` so such a frame is still classified as silence.
    """
    rms = np.sqrt(np.mean(np.square(frames, dtype=np.float64), axis=1) + 1e-20)
    return 20.0 * np.log10(rms + 1e-20)


def level_from_db(db: float) -> float:
    """Map a dBFS reading onto the 0.0-1.0 range a level meter renders."""
    span = LEVEL_CEILING_DB - LEVEL_FLOOR_DB
    return float(min(1.0, max(0.0, (db - LEVEL_FLOOR_DB) / span)))


class Endpointer:
    """Tracks the noise floor and reports when an utterance has ended.

    Feed 16 kHz mono float32 in ``[-1.0, 1.0]``. Chunks may be any length: the
    remainder shorter than one frame is carried into the next call, so frame
    boundaries stay aligned across an entire session and a caller never has to
    align its reads.

    Not thread-safe by design. One session owns one endpointer, and a session is
    driven from a single task.
    """

    def __init__(
        self,
        silence_ms: int = DEFAULT_SILENCE_MS,
        max_utterance_ms: int = DEFAULT_MAX_UTTERANCE_MS,
    ) -> None:
        # Clamped rather than rejected: the value reaches here from user config,
        # and a too-eager silence window should degrade to the shortest usable
        # one instead of failing a voice session the user just started.
        effective_silence = max(MIN_SILENCE_MS, silence_ms)
        if effective_silence != silence_ms:
            logger.warning(
                "stt.silence_ms=%d is below the %dms floor; using %d",
                silence_ms,
                MIN_SILENCE_MS,
                effective_silence,
            )
        self._silence_frames_needed = max(1, effective_silence // FRAME_MS)
        self._max_frames = max(1, max_utterance_ms // FRAME_MS)
        self._floor_db = _FLOOR_UNSET
        self._state = VadState.IDLE
        self._speech_run = 0
        self._silence_run = 0
        self._frames_seen = 0
        self._last_silent = True
        # Annotated rather than inferred. `np.empty(0, ...)` infers a RANK-1 shape,
        # while the slice assigned to this in `push` is a slice of a concatenation and
        # carries an unknown rank, so the two are incompatible under numpy's
        # shape-typed stubs. Which stub version CI resolves differs by Python
        # version, so this passed locally and failed only on the 3.10 lane.
        self._carry: np.ndarray = np.empty(0, dtype=np.float32)

    @property
    def state(self) -> VadState:
        return self._state

    @property
    def noise_floor_db(self) -> float:
        """The current floor estimate, or ``-inf`` before any audio was seen."""
        return float("-inf") if self._floor_db is _FLOOR_UNSET else self._floor_db

    @property
    def speech_frames_seen(self) -> bool:
        """Whether any qualifying speech was ever detected in this utterance."""
        return self._state is not VadState.IDLE

    def push(self, pcm: np.ndarray) -> VadUpdate:
        """Consume mono float32 audio and return the updated verdict."""
        if self._state is VadState.ENDED:
            # Terminal: keep reporting the end state rather than re-arming, so a
            # client that keeps sending after the endpoint cannot restart an
            # utterance the caller has already finalised.
            return VadUpdate(0.0, False, False, VadState.ENDED, silent=True)

        buf = np.concatenate((self._carry, np.asarray(pcm, dtype=np.float32)))
        n_full = len(buf) // FRAME_SAMPLES
        self._carry = buf[n_full * FRAME_SAMPLES :]
        if n_full == 0:
            return VadUpdate(
                0.0, self._state is VadState.SPEECH, False, self._state, silent=self._last_silent
            )

        frames = buf[: n_full * FRAME_SAMPLES].reshape(n_full, FRAME_SAMPLES)
        db = _frame_db(frames)

        ended = False
        consumed = 0
        loudest = float(db.max())
        for frame_db in db:
            value = float(frame_db)
            self._update_floor(value)
            consumed += 1
            if self._consume_frame(value):
                ended = True
                break

        # Only when the loop broke early. Without ``ended`` the slice would be the
        # sub-frame carry, which is retained internally and must not be handed out
        # twice. The unconsumed region is always wholly inside the CALLER's chunk:
        # the carry is shorter than one frame and at least one frame was consumed,
        # so the offset is past it.
        return VadUpdate(
            level=level_from_db(loudest),
            speech=self._state is VadState.SPEECH,
            ended=ended,
            state=self._state,
            silent=self._last_silent,
            pending=buf[consumed * FRAME_SAMPLES :] if ended else _NO_SAMPLES,
        )

    def _update_floor(self, frame_db: float) -> None:
        """Pull the floor estimate toward *frame_db*, fast down and slow up.

        Asymmetric on purpose. Falling fast means a room that goes quiet is
        tracked at once; rising slowly means sustained speech cannot drag the
        floor up under itself until the speaker stops clearing the margin, which
        would end the utterance mid-sentence.
        """
        if frame_db <= ABSOLUTE_SILENCE_DB:
            # Digital silence, which is NOT a measurement of the room. A frame of
            # exact zeros reads about -200 dBFS -- the epsilon in `_frame_db`, not a
            # level -- and a microphone delivers one routinely: device warm-up, a
            # muted input, the gap before the first sample lands.
            #
            # Seeding the floor from it is the trap. Stored raw it becomes a floor
            # every later frame clears by the 12 dB margin, so ordinary room tone
            # reads as speech and the utterance never ends (measured: 1282 of 1500
            # room-tone frames, ~26 s of a 30 s recording). Clamping to
            # `ABSOLUTE_SILENCE_DB` instead is better but still wrong, because it
            # seeds BELOW a real room: -55 dB room tone clears a -70 dB floor, and
            # the deliberately slow rise then takes ~170 frames to converge.
            #
            # So an unset floor stays unset until a frame carries a real level;
            # `_consume_frame` reads an unset floor as "not speech", which is the
            # right answer for silence anyway. Once the floor IS set, snapping it
            # down to the silence bound is correct and is the fast-fall path: the
            # room genuinely went quiet.
            if self._floor_db is not _FLOOR_UNSET:
                self._floor_db = ABSOLUTE_SILENCE_DB
            return
        if self._floor_db is _FLOOR_UNSET:
            self._floor_db = frame_db
            return
        coeff = FLOOR_FALL_COEFF if frame_db < self._floor_db else FLOOR_RISE_COEFF
        self._floor_db += (frame_db - self._floor_db) * coeff

    def _consume_frame(self, frame_db: float) -> bool:
        """Advance the state machine by one frame. Returns True if it just ended."""
        self._frames_seen += 1
        is_speech = (
            self._floor_db is not _FLOOR_UNSET and frame_db > self._floor_db + SPEECH_MARGIN_DB
        )
        self._last_silent = not is_speech

        if is_speech:
            self._speech_run += 1
            self._silence_run = 0
            if self._state is VadState.IDLE and self._speech_run >= MIN_SPEECH_FRAMES:
                self._state = VadState.SPEECH
        else:
            self._speech_run = 0
            if self._state is VadState.SPEECH:
                self._silence_run += 1
                if self._silence_run >= self._silence_frames_needed:
                    self._state = VadState.ENDED
                    return True

        if self._frames_seen >= self._max_frames:
            # The ceiling ends the utterance whether or not speech was ever
            # detected: a session that heard nothing for two minutes has no
            # better outcome available than closing.
            self._state = VadState.ENDED
            return True
        return False
