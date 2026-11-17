"""The voice counters (``metrics.voice``): closed attributes, one end per session.

Every attribute value is a member of a closed tuple, a fixed bucket, or ``other``;
no transcript, reply text or message is ever emitted. A dictation session counts
``ended`` exactly once, whatever path ends it. The end-to-end readings through the
real ``/api/ws/stt`` and ``/api/voice/synthesize`` handlers are in
``test_speech_provider_dispatch.py`` (``TestVoiceMetrics``), beside the provider
fakes they need.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from kiro_crew.metrics import voice as m


@pytest.fixture
def emitted(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    rows: list[tuple[str, dict]] = []
    monkeypatch.setattr(m, "_emit", lambda name, attrs: rows.append((name, attrs)))
    return rows


class _Clock:
    """A clock the test advances by hand, so no bucket depends on runner speed."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    c = _Clock()
    monkeypatch.setattr(m, "_now", c)
    return c


def _in_fresh_context(fn):
    """Run *fn* in a task, so the session ContextVar starts empty and stays local."""

    async def run():
        return fn()

    return asyncio.run(run())


# ── buckets ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("seconds", "bucket"),
    [
        (0.0, "lt_10s"),
        (9.99, "lt_10s"),
        (10.0, "10_60s"),
        (299.0, "1_5m"),
        (300.0, "5_15m"),
        (899.0, "5_15m"),
        (900.0, "15_60m"),
        (3600.0, "gt_60m"),
        (None, m.NONE),
        (-1.0, m.OTHER),
        (True, m.OTHER),
    ],
)
def test_duration_bucket_is_closed(seconds, bucket) -> None:
    assert m.duration_bucket(seconds) == bucket
    assert bucket in m.DURATION_VALUES or bucket in (m.NONE, m.OTHER)


@pytest.mark.parametrize(
    ("seconds", "bucket"),
    [
        (0.1, "lt_500ms"),
        (0.5, "500ms_1s"),
        (1.5, "1_2s"),
        (4.0, "2_5s"),
        (9.0, "gt_5s"),
        (None, "none"),
    ],
)
def test_latency_bucket_is_closed(seconds, bucket) -> None:
    assert m.latency_bucket(seconds) == bucket
    assert bucket in m.LATENCY_VALUES


# ── provider labels ──────────────────────────────────────────────────────────


def test_builtin_provider_lists_match_the_config_sources() -> None:
    """A built-in added to config must be added here, or it would read as ``other``."""
    from kiro_crew.config.sections_stt import _VALID_STT_PROVIDERS
    from kiro_crew.voice_reply import VALID_PROVIDERS

    assert set(m.STT_PROVIDER_VALUES) == set(_VALID_STT_PROVIDERS)
    assert set(m.TTS_PROVIDER_VALUES) == set(VALID_PROVIDERS)


@pytest.mark.parametrize("name", ["local", "apple", "transcribe", "codex", "off"])
def test_a_builtin_stt_provider_is_itself(name) -> None:
    assert m.provider_label(m.KIND_STT, name) == name


def test_an_unregistered_name_is_other_and_never_echoed() -> None:
    assert m.provider_label(m.KIND_STT, "my-secret-host") == m.OTHER
    assert m.provider_label(m.KIND_TTS, None) == m.OTHER


def test_a_registered_provider_is_one_bucket(monkeypatch) -> None:
    from kiro_crew import speech_providers

    monkeypatch.setattr(speech_providers, "stt_provider", lambda name: object())
    monkeypatch.setattr(speech_providers, "tts_provider", lambda name: object())
    assert m.provider_label(m.KIND_STT, "acme") == m.PROVIDER_REGISTERED
    assert m.provider_label(m.KIND_TTS, "acme") == m.PROVIDER_REGISTERED


def test_a_raising_lookup_is_other(monkeypatch) -> None:
    from kiro_crew import speech_providers

    def boom(name):
        raise RuntimeError("seam broken")

    monkeypatch.setattr(speech_providers, "stt_provider", boom)
    assert m.provider_label(m.KIND_STT, "acme") == m.OTHER


# ── dictation lifecycle ──────────────────────────────────────────────────────


def test_a_session_counts_start_and_one_end(emitted, clock) -> None:
    def scenario():
        m.stt_session_begin("local")
        clock.now += 0.7
        m.observe_stt_frame({"type": "partial", "text": "hel"})
        clock.now += 1.0
        m.observe_stt_frame({"type": "final", "text": "hello"})
        clock.now += 400.0
        m.stt_session_end("ok")
        m.stt_session_end("error")  # a second end is ignored

    _in_fresh_context(scenario)
    names = [name for name, _ in emitted]
    assert names == [m.VOICE_STT_STARTED, m.VOICE_STT_ENDED]
    assert emitted[0][1] == {"provider": "local"}
    ended = emitted[1][1]
    assert ended["provider"] == "local"
    assert ended["outcome"] == "ok"
    assert ended["transcript"] == "nonempty"
    assert ended["duration"] == "5_15m"
    assert ended["first_partial"] == "500ms_1s"


def test_an_empty_session_has_no_first_partial(emitted, clock) -> None:
    def scenario():
        m.stt_session_begin("local")
        m.observe_stt_frame({"type": "final", "text": "   "})
        m.stt_session_end("ok")

    _in_fresh_context(scenario)
    _started, ended = (attrs for _, attrs in emitted)
    assert ended["transcript"] == "empty"
    assert ended["first_partial"] == m.NONE


def test_an_end_without_a_begin_counts_nothing(emitted) -> None:
    _in_fresh_context(lambda: m.stt_session_end("ok"))
    assert emitted == []


@pytest.mark.parametrize(("outcome", "label"), [("timeout", "timeout"), ("forbidden", m.OTHER)])
def test_the_end_outcome_is_closed(emitted, clock, outcome, label) -> None:
    def scenario():
        m.stt_session_begin("apple")
        m.stt_session_end(outcome)

    _in_fresh_context(scenario)
    assert emitted[-1][1]["outcome"] == label


def test_an_error_frame_counts_its_code_and_never_its_message(emitted, clock) -> None:
    def scenario():
        m.stt_session_begin("transcribe")
        m.observe_stt_frame({"type": "error", "code": "stt_consent_required", "message": "x"})
        m.observe_stt_frame({"type": "error", "code": "made_up", "message": "y"})

    _in_fresh_context(scenario)
    errors = [attrs for name, attrs in emitted if name == m.VOICE_STT_ERRORS]
    assert errors == [
        {"provider": "transcribe", "code": "stt_consent_required"},
        {"provider": "transcribe", "code": m.OTHER},
    ]


def test_an_error_before_a_session_has_provider_none(emitted) -> None:
    _in_fresh_context(
        lambda: m.observe_stt_frame({"type": "error", "code": "stt_streaming_disabled"})
    )
    assert emitted == [(m.VOICE_STT_ERRORS, {"provider": m.NONE, "code": "stt_streaming_disabled"})]


def test_every_socket_and_recogniser_error_code_is_in_the_closed_list() -> None:
    """A new code must be added here, or the counter would fold it into ``other``."""
    src = Path(__file__).resolve().parents[2] / "src" / "kiro_crew"
    declared: set[str] = set()
    for rel in (
        "dashboard/stt_stream.py",
        "stt/engine.py",
        "stt/preflight.py",
        "stt/decoder.py",
        "stt/codex.py",
        "stt/codex_rpc.py",
    ):
        text = (src / rel).read_text(encoding="utf-8")
        declared.update(re.findall(r'^_?CODE_\w+ = "([a-z_]+)"', text, flags=re.MULTILINE))
    assert declared, "no codes were collected; the pattern is broken"
    assert declared <= set(m.STT_ERROR_CODES), sorted(declared - set(m.STT_ERROR_CODES))


def test_unavailable_reasons_are_closed(emitted) -> None:
    m.record_stt_unavailable("busy")
    m.record_stt_unavailable("because I said so")
    assert [attrs for _, attrs in emitted] == [{"reason": "busy"}, {"reason": m.OTHER}]


@pytest.mark.parametrize(
    ("enabled", "provider", "streaming", "reason"),
    [
        (False, "local", True, "stt_disabled"),
        (True, "off", True, "provider_off"),
        (True, "local", False, "streaming_off"),
        (True, "acme", True, "provider_not_streaming"),
    ],
)
def test_the_guard_reason_names_the_refusing_switch(enabled, provider, streaming, reason) -> None:
    from types import SimpleNamespace

    cfg = SimpleNamespace(enabled=enabled, provider=provider, streaming=streaming)
    assert m.guard_reason(cfg) == reason
    assert m.guard_reason(object()) == m.OTHER


# ── replies ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "code", "outcome"),
    [
        (200, None, "ok"),
        (409, "voice_cancelled", "cancelled"),
        (429, "voice_busy", "busy"),
        (400, "voice_invalid_request", "invalid"),
        (502, "voice_unavailable", "unavailable"),
        (502, "sandbox_network", "sandbox_refused"),
        (502, "voice_synthesis_failed", "error"),
        (None, None, "error"),
    ],
)
def test_tts_outcome_is_closed(status, code, outcome) -> None:
    assert m.tts_outcome(status, code) == outcome
    assert outcome in m.TTS_OUTCOME_VALUES


def test_a_reply_counts_its_first_audio(emitted, clock) -> None:
    def scenario():
        timing = m.tts_request_begin()
        clock.now += 1.5
        m.note_tts_chunk()
        clock.now += 3.0
        m.note_tts_chunk()
        m.tts_request_end("system", 200, None, timing)

    _in_fresh_context(scenario)
    assert emitted == [
        (m.VOICE_TTS_REPLIES, {"provider": "system", "outcome": "ok", "first_audio": "1_2s"})
    ]


def test_a_reply_with_no_audio_has_first_audio_none(emitted, clock) -> None:
    def scenario():
        timing = m.tts_request_begin()
        m.tts_request_end("polly", 502, "voice_unavailable", timing)

    _in_fresh_context(scenario)
    assert emitted[0][1]["first_audio"] == m.NONE
    assert emitted[0][1]["outcome"] == "unavailable"


def test_recording_never_raises(monkeypatch) -> None:
    def boom(*a, **k):
        raise RuntimeError("recorder down")

    monkeypatch.setattr("kiro_crew.metrics.events.emit_counter", boom)
    _in_fresh_context(
        lambda: (
            m.stt_session_begin("local"),
            m.observe_stt_frame({"type": "error", "code": "stt_session_failed"}),
            m.stt_session_end("ok"),
            m.record_stt_unavailable("busy"),
            m.tts_request_end("system", 200, None, m.tts_request_begin()),
        )
    )
