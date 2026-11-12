"""A cancelled Apple batch transcription stops its helper.

A caller that gives up (an uploader that disconnected from ``/api/stt/transcribe``)
cancels ``apple_speech.transcribe``; the Swift helper must not keep decoding.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from kiro_crew import apple_speech


@pytest.mark.asyncio
async def test_a_cancelled_transcription_kills_the_helper() -> None:
    proc = AsyncMock()
    proc.communicate = AsyncMock(side_effect=[asyncio.CancelledError(), (b"", b"")])
    proc.kill = Mock()
    with (
        patch.object(apple_speech, "availability", return_value=apple_speech.Availability(True)),
        patch.object(apple_speech, "helper_path", return_value="/fake/helper"),
        patch("asyncio.create_subprocess_exec", return_value=proc),
        # The sandbox is not this test's subject; on a host without a backend
        # the fail-closed return would fire before the helper ever runs.
        patch.object(apple_speech, "_sandboxed", side_effect=lambda argv: (argv, {}, None)),
    ):
        with pytest.raises(asyncio.CancelledError):
            await apple_speech.transcribe("/tmp/x.wav")
    proc.kill.assert_called_once()
    assert proc.communicate.await_count == 2
