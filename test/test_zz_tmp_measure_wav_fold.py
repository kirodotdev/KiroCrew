"""The multichannel WAV fold's memory stays bounded by its slice budget.

``transcribe_local._pcm_from_wav`` folds a multichannel WAV to mono in slices of
``_WAV_SLICE_BYTES`` raw int16 bytes, so one fold's transient memory follows the
budget, not the file size or the channel count. Measured with ``tracemalloc``
(numpy reports its buffers to it) under a 64 KiB budget, so the files stay small.
"""

from __future__ import annotations

import tracemalloc

import numpy as np
import pytest
from transcribe_helpers import _write_wav

from kiro_crew import transcribe, transcribe_local

#: The slice budget the measurement runs under.
_BUDGET = 64 * 1024

#: Slices each file spans, so a whole-file read would hold several budgets.
_SLICES = 6


@pytest.mark.parametrize("channels", [2, 4, 256])
def test_one_fold_holds_a_few_slice_budgets_whatever_the_channel_count(
    tmp_path, monkeypatch, channels
):
    monkeypatch.setattr(transcribe_local, "_WAV_SLICE_BYTES", _BUDGET)
    frames = _BUDGET // (channels * 2) * _SLICES
    rng = np.random.default_rng(channels)
    interleaved = rng.integers(-30_000, 30_000, frames * channels, dtype=np.int16)
    audio = tmp_path / "multi.wav"
    _write_wav(audio, interleaved, channels=channels)
    # One untraced fold first resolves the lazy ``stt`` imports the fold reaches
    # (``stt.vad``, ``stt.engine``), so compiling them never counts as fold memory.
    assert transcribe._pcm_from_wav(str(audio)) is not None

    tracemalloc.start()
    try:
        pcm = transcribe._pcm_from_wav(str(audio))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert pcm is not None and pcm.size == frames
    # The mono output is held twice (the per-slice list and its concatenation);
    # beyond that only one slice's raw bytes and their conversion are live. A
    # whole-file read would add six budgets of raw bytes and at least twelve of
    # conversion, past this bound for every channel count here.
    output_bytes = 2 * frames * np.dtype(np.float32).itemsize
    assert peak < output_bytes + 8 * _BUDGET, f"{channels} channels peaked at {peak} bytes"
