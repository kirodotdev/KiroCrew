"""Codex media boundary tests with stand-in aiortc and av modules (no native media)."""

from __future__ import annotations

import fractions
import sys
from types import ModuleType, SimpleNamespace

import pytest

from kiro_crew.stt import codex_media


class _Plane:
    def __init__(self) -> None:
        self.data = b""

    def update(self, data: bytes) -> None:
        self.data = data


class _AudioFrame:
    def __init__(self, *, format: str, layout: str, samples: int) -> None:
        self.format, self.layout, self.samples = format, layout, samples
        self.planes = [_Plane()]
        self.sample_rate = 0
        self.pts = None
        self.time_base = None


class _AudioStreamTrack:
    kind = "audio"

    def __init__(self) -> None:
        self.started = True


class _PeerConnection:
    def __init__(self, configuration: object) -> None:
        self.configuration = configuration
        self.tracks: list[object] = []

    def addTrack(self, track: object) -> None:  # noqa: N802 - aiortc's spelling
        self.tracks.append(track)


@pytest.fixture
def fake_media(monkeypatch):
    av = ModuleType("av")
    av.AudioFrame = _AudioFrame
    aiortc = ModuleType("aiortc")
    aiortc.AudioStreamTrack = _AudioStreamTrack
    aiortc.RTCConfiguration = lambda iceServers: SimpleNamespace(iceServers=iceServers)
    aiortc.RTCPeerConnection = _PeerConnection
    aiortc.RTCSessionDescription = lambda sdp, type: SimpleNamespace(sdp=sdp, type=type)
    monkeypatch.setitem(sys.modules, "av", av)
    monkeypatch.setitem(sys.modules, "aiortc", aiortc)


def test_peer_has_no_ice_servers_and_carries_the_input_track(fake_media):
    pc, track = codex_media.create_peer()
    assert pc.configuration.iceServers == []
    assert pc.tracks == [track]
    assert isinstance(track.source, codex_media.PcmInput)


@pytest.mark.asyncio
async def test_track_frames_are_mono_pcm16_with_sample_clock(fake_media):
    _, track = codex_media.create_peer()
    pcm = b"\x01\x00" * codex_media.FRAME_SAMPLES
    track.append(pcm)
    frame = await track.recv()
    assert (frame.format, frame.layout, frame.samples) == ("s16", "mono", codex_media.FRAME_SAMPLES)
    assert frame.planes[0].data == pcm
    assert frame.sample_rate == codex_media.SAMPLE_RATE
    assert frame.pts == 0
    assert frame.time_base == fractions.Fraction(1, codex_media.SAMPLE_RATE)
    await track.drain()


def test_remote_answer_is_an_sdp_answer(fake_media):
    answer = codex_media.remote_answer("v=0 fixture")
    assert (answer.sdp, answer.type) == ("v=0 fixture", "answer")


@pytest.mark.asyncio
async def test_partial_backlog_stays_pending_until_drained():
    source = codex_media.PcmInput()
    source.append(b"")
    assert source.drained.is_set()
    frame_bytes = codex_media.FRAME_SAMPLES * 2
    source.append(b"\x02\x00" * (codex_media.FRAME_SAMPLES + 10))
    data, pts = await source.read()
    assert len(data) == frame_bytes and pts == 0
    assert not source.drained.is_set()
    data, pts = await source.read()
    assert data == (b"\x02\x00" * 10).ljust(frame_bytes, b"\0")
    assert pts == codex_media.FRAME_SAMPLES
    assert source.drained.is_set()
