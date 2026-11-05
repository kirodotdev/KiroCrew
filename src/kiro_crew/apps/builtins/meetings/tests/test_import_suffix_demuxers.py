"""Every audio suffix the Meetings import admits has a forced FFmpeg demuxer.

The core's protocol pin (``transcribe._create_ffmpeg_subprocess``) stops NETWORK
fetches, but an auto-probed playlist demuxer could still open other LOCAL files
its text names -- reads that never went through ``validate_file_path``. Forcing
the validated suffix's own demuxer turns playlist text into a decode error, so
every suffix the import route admits must map to one and no attacker-influenced
input ever falls back to content sniffing. The core's own demuxer cases are in
``test/test_transcribe_ffmpeg.py``.
"""

from __future__ import annotations

import pytest

from kiro_crew import transcribe
from kiro_crew.apps.builtins.meetings.backend import constants as k


@pytest.mark.parametrize("suffix", sorted(k.IMPORT_AUDIO_EXTENSIONS))
def test_every_import_suffix_has_a_forced_demuxer(suffix: str) -> None:
    forced = transcribe._forced_demuxer_args(f"/tmp/talk{suffix}")
    assert forced and forced[0] == "-f", f"no forced demuxer for {suffix}"
