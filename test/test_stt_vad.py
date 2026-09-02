"""Endpointer behaviour: what counts as speech, and when an utterance is over.

Every case here builds its audio arithmetically rather than reading a fixture, so
the assertions are about the detector's rules and not about one recording. Arrays
are built inside the test bodies: a literal at module scope is paid by every xdist
worker at collection and held for the whole session.
"""

from __future__ import annotations

import numpy as np
import pytest

from kiro_crew.stt import vad


def _tone(seconds: float, amplitude: float = 0.3) -> np.ndarray:
    """A 220 Hz tone, which reads as speech to an energy detector."""
    n = int(seconds * vad.SAMPLE_RATE_HZ)
    t = np.arange(n, dtype=np.float32) / vad.SAMPLE_RATE_HZ
    return (amplitude * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)


def _silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * vad.SAMPLE_RATE_HZ), dtype=np.float32)


def _quiet_room(seconds: float, level: float = 0.0008) -> np.ndarray:
    """Low-level noise, the shape of a real microphone's floor."""
    n = int(seconds * vad.SAMPLE_RATE_HZ)
    rng = np.random.default_rng(1234)
    return (rng.standard_normal(n) * level).astype(np.float32)


def test_speech_then_silence_ends_the_utterance():
    ep = vad.Endpointer(silence_ms=400)
    assert not ep.push(_quiet_room(0.5)).ended
    update = ep.push(_tone(1.0))
    assert update.speech
    assert not update.ended
    assert ep.push(_silence(0.6)).ended
    assert ep.state is vad.VadState.ENDED


def test_silence_alone_never_ends_an_utterance():
    """Nothing was said, so there is no utterance to end before the ceiling."""
    ep = vad.Endpointer(silence_ms=200, max_utterance_ms=60_000)
    assert not ep.push(_silence(5.0)).ended
    assert ep.state is vad.VadState.IDLE
    assert not ep.speech_frames_seen


def test_a_single_loud_frame_is_not_speech():
    """A key click or a desk bump must not open an utterance."""
    ep = vad.Endpointer(silence_ms=300)
    ep.push(_quiet_room(0.4))
    click = np.concatenate((_tone(0.02, amplitude=0.9), _silence(0.5)))
    update = ep.push(click)
    assert not update.speech
    assert not update.ended
    assert ep.state is vad.VadState.IDLE


def test_a_pause_shorter_than_the_window_does_not_end_it():
    ep = vad.Endpointer(silence_ms=700)
    ep.push(_quiet_room(0.3))
    ep.push(_tone(0.8))
    mid = ep.push(_silence(0.3))
    assert not mid.ended
    assert mid.speech, "the utterance-level state must survive a short pause"
    assert mid.silent, "the frame-level reading must report the pause"
    assert not ep.push(_tone(0.8)).ended


def test_speech_and_silent_are_not_complements():
    """The two flags answer different questions, which is why both exist."""
    ep = vad.Endpointer(silence_ms=900)
    ep.push(_quiet_room(0.3))
    ep.push(_tone(0.6))
    update = ep.push(_silence(0.2))
    assert update.speech and update.silent


def test_the_utterance_ceiling_always_terminates():
    """A hot mic that never goes quiet must still finish on its own."""
    ep = vad.Endpointer(silence_ms=10_000, max_utterance_ms=200)
    assert ep.push(_tone(1.0)).ended
    assert ep.state is vad.VadState.ENDED


def test_ended_is_terminal_and_one_shot():
    ep = vad.Endpointer(silence_ms=300)
    ep.push(_quiet_room(0.3))
    ep.push(_tone(0.8))
    assert ep.push(_silence(0.5)).ended
    after = ep.push(_tone(1.0))
    assert not after.ended, "ended is reported once, on the transition"
    assert not after.speech, "a client that keeps sending cannot re-arm the utterance"
    assert after.state is vad.VadState.ENDED


def test_frames_are_carried_across_chunk_boundaries():
    """Sub-frame remainders accumulate instead of being dropped."""
    ep = vad.Endpointer()
    stub = np.zeros(vad.FRAME_SAMPLES // 2, dtype=np.float32)
    assert ep.push(stub).level == 0.0
    # Two half-frames make one whole frame, which must now be analysed.
    ep.push(stub)
    assert ep._frames_seen == 1


def test_the_noise_floor_adapts_to_a_loud_room():
    """A noisy room must not read as continuous speech."""
    ep = vad.Endpointer(silence_ms=300)
    loud_room = _quiet_room(2.0, level=0.02)
    update = ep.push(loud_room)
    assert not update.speech
    assert ep.noise_floor_db > -60.0, "the floor must sit at the room level, not below it"


def test_digital_silence_pulls_the_floor_down_immediately():
    """A muted device is exact zeros, which must not leave a stale floor."""
    ep = vad.Endpointer()
    ep.push(_tone(0.5))
    ep.push(_silence(0.2))
    assert ep.noise_floor_db <= vad.ABSOLUTE_SILENCE_DB


@pytest.mark.parametrize("lead_silent_frames", [1, 5, 25, 100])
@pytest.mark.parametrize("room_level", [0.0018, 0.01])
def test_leading_digital_silence_does_not_seed_the_floor(lead_silent_frames, room_level):
    """A muted first frame must not make the whole room read as speech.

    A microphone routinely delivers digital silence before the first real sample
    (device warm-up, a muted input), and a frame of exact zeros measures about
    -200 dBFS -- the epsilon in `_frame_db`, not a level. Seeding the floor from it
    put every later frame 130 dB above the floor, so room tone cleared the speech
    margin and the utterance could never end: measured at 1282 of 1500 room-tone
    frames before the fix. Clamping the seed to `ABSOLUTE_SILENCE_DB` is not enough
    either -- that still seeds BELOW a real room, and the slow rise coefficient took
    ~170 frames to converge -- so an unset floor stays unset until a frame carries a
    real level.

    Parametrised over both a quiet and a noisy room because the two failure modes
    are opposite: a floor seeded too low reads tone as speech, and one seeded too
    high reads speech as tone.
    """
    ep = vad.Endpointer(silence_ms=700)
    ep.push(_silence(lead_silent_frames * vad.FRAME_MS / 1000.0))
    speech_frames = 0
    for _ in range(75):  # 1.5 s of room tone, in 20 ms pushes
        chunk = _quiet_room(vad.FRAME_MS / 1000.0, level=room_level)
        if ep.push(chunk).speech:
            speech_frames += 1
    assert speech_frames == 0, f"room tone read as speech in {speech_frames} frames"


@pytest.mark.parametrize("lead_silent_frames", [0, 1, 25, 100])
def test_speech_after_leading_silence_is_still_detected_and_ended(lead_silent_frames):
    """The counterpart: refusing to seed from silence must not deafen the detector."""
    ep = vad.Endpointer(silence_ms=300)
    ep.push(_silence(lead_silent_frames * vad.FRAME_MS / 1000.0))
    ep.push(_quiet_room(0.4))
    assert ep.push(_tone(0.5)).speech, "real speech went undetected"
    ended = any(ep.push(_quiet_room(0.1)).ended for _ in range(20))
    assert ended, "the utterance never ended on trailing room tone"


def test_silence_window_is_clamped_not_rejected(caplog):
    """The value arrives from user config, so it degrades rather than failing."""
    with caplog.at_level("WARNING"):
        ep = vad.Endpointer(silence_ms=1)
    assert ep._silence_frames_needed == vad.MIN_SILENCE_MS // vad.FRAME_MS
    assert "below the" in caplog.text


@pytest.mark.parametrize(
    ("db", "expected"),
    [
        (vad.LEVEL_FLOOR_DB - 20, 0.0),
        (vad.LEVEL_FLOOR_DB, 0.0),
        (vad.LEVEL_CEILING_DB, 1.0),
        (vad.LEVEL_CEILING_DB + 20, 1.0),
    ],
)
def test_level_is_clamped_to_the_meter_range(db, expected):
    assert vad.level_from_db(db) == expected


def test_level_rises_with_loudness():
    quiet = vad.Endpointer().push(_tone(0.2, amplitude=0.02)).level
    loud = vad.Endpointer().push(_tone(0.2, amplitude=0.5)).level
    assert 0.0 <= quiet < loud <= 1.0


def test_frame_geometry_divides_the_sample_rate_exactly():
    """A frame that straddled a sample would drift over a long session."""
    assert vad.SAMPLE_RATE_HZ * vad.FRAME_MS % 1000 == 0
    assert vad.FRAME_SAMPLES == 320


def test_the_ending_update_hands_back_the_audio_it_did_not_consume():
    """The caller needs the tail, because its chunk is longer than a frame.

    ``push`` stops at the frame that closed the utterance, so anything after it in the
    same chunk is unexamined. Returning it is what lets the session start the NEXT
    utterance from resumed speech rather than filing it under the one that just ended.
    """
    ep = vad.Endpointer(silence_ms=300)
    ep.push(_quiet_room(0.5))
    ep.push(_tone(1.0))

    # Silence long enough to expire the hangover, then speech again, in ONE chunk.
    resumed = _tone(0.4)
    update = ep.push(np.concatenate((_silence(0.5), resumed)))

    assert update.ended
    assert update.pending.size > 0, "the unconsumed tail was dropped"
    # It comes from the end of the chunk, so the resumed speech is in it: the tail is
    # longer than the silence that followed the endpoint could account for on its own.
    assert update.pending.size <= 0.5 * vad.SAMPLE_RATE_HZ + resumed.size
    assert float(np.abs(update.pending[-resumed.size // 2 :]).max()) > 0.1


def test_pending_is_empty_when_the_utterance_did_not_end():
    """Otherwise it would be the sub-frame carry, which `push` retains internally and
    a caller re-feeding would duplicate."""
    ep = vad.Endpointer(silence_ms=400)
    # A length that does not divide into whole frames, so there IS a carry to leak.
    odd = vad.FRAME_SAMPLES * 3 + 71
    update = ep.push(_tone(0.0) if odd == 0 else _quiet_room(odd / vad.SAMPLE_RATE_HZ))
    assert not update.ended
    assert update.pending.size == 0
