"""Shared WAV builders for the ``test_transcribe*.py`` family.

``_write_wav`` writes samples as a RIFF WAV with the stdlib writer, and
``_ramp_int16`` makes a deterministic signal that is not silence; the WAV-input
and guard tests in ``test_transcribe.py`` and the provider tests in
``test_transcribe_providers.py`` build their audio with them.
"""

from __future__ import annotations

import wave

import numpy as np


def _write_wav(
    path,
    samples: np.ndarray,
    *,
    rate: int = 16_000,
    channels: int = 1,
    sampwidth: int = 2,
) -> None:
    """Write *samples* as a RIFF WAV with the stdlib writer.

    Built in the test body rather than committed as a fixture: the whole question
    these tests ask is which (rate, width, channel) combinations the stdlib reader
    accepts, and a binary blob in the repo hides its own header from review.
    """
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sampwidth)
        wav.setframerate(rate)
        wav.writeframes(samples.astype("<i2").tobytes())


def _ramp_int16(count: int) -> np.ndarray:
    """A deterministic int16 signal that is not silence, so a fold is observable."""
    return (np.arange(count, dtype=np.int32) * 37 % 20_000 - 10_000).astype(np.int16)
