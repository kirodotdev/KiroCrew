"""The resident engine's lifecycle: load, decode, prewarm and eviction.

Covers loading once and reusing the context, ``ensure_loaded``'s idempotence and a
language change reloading it, the codes a missing, refused or unavailable model reports
instead of raising, what a decode returns (empty without a model, a raise on a failed
decode or a non-zero native status rather than silence, segments read through the
binding, an abort that is not a failure, a host without the low-level binding, a
superseded partial aborted but never a final, a key mismatch refused), segments joined
and stripped, idle eviction and ``close``, prewarm (the first decode paid up front, not
repeated, not counted as use for the boot gate), the usage stamp written off the event
loop without holding the transcript, and a session stop aborting its partial but never
its final. The model is a fake: ``fake_engine`` and the doubles come from
``stt_engine_helpers.py``.
"""

from __future__ import annotations

import asyncio
import threading

import numpy as np
import pytest
from stt_engine_helpers import (  # noqa: F401 -- the fixture is requested by name
    _FakeBinding,
    _FakeModel,
    _FakeSegment,
    _make_native,
    fake_engine_fixture,
)

from kiro_crew.stt import engine as engine_mod
from kiro_crew.stt import models


@pytest.mark.asyncio
async def test_the_model_is_loaded_once_and_reused(fake_engine):
    """This is the whole point of the module: no per-utterance load."""
    eng, fake = fake_engine
    builds = []
    assert (await eng.ensure_loaded("base", "en")).ok
    assert eng.loaded
    for _ in range(3):
        assert await eng.decode(np.zeros(16000, dtype=np.float32)) == "hello world"
    assert len(fake.decoded_samples) == 3
    assert builds == []


@pytest.mark.asyncio
async def test_ensure_loaded_is_idempotent(fake_engine, monkeypatch):
    eng, _ = fake_engine
    calls = {"n": 0}

    def _build(key):
        calls["n"] += 1
        return _FakeModel()

    monkeypatch.setattr(engine_mod.WhisperEngine, "_build_model", staticmethod(_build))
    await eng.ensure_loaded("base", "en")
    await eng.ensure_loaded("base", "en")
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_changing_the_language_reloads_the_context(fake_engine, monkeypatch):
    """Language is baked into the context, so it cannot be switched in place."""
    eng, _ = fake_engine
    keys: list[str] = []

    def _build(key):
        keys.append(key.language)
        return _FakeModel()

    monkeypatch.setattr(engine_mod.WhisperEngine, "_build_model", staticmethod(_build))
    await eng.ensure_loaded("base", "en")
    await eng.ensure_loaded("base", "fr")
    assert keys == ["en", "fr"]


@pytest.mark.asyncio
async def test_a_missing_model_reports_its_code_rather_than_raising(monkeypatch, tmp_path):
    monkeypatch.setattr(engine_mod, "probe", lambda: engine_mod.Availability(True))

    async def _ensure(_model):
        return None

    monkeypatch.setattr(models, "store", lambda: type("S", (), {"ensure": staticmethod(_ensure)})())
    eng = engine_mod.WhisperEngine()
    result = await eng.ensure_loaded("base", "en")
    assert not result.ok
    assert result.code == engine_mod.CODE_MODEL_MISSING
    assert not eng.loaded


@pytest.mark.asyncio
async def test_a_refused_model_reports_the_refusal_not_a_missing_download(monkeypatch):
    # A hard-linked weights file is refused at load. The bytes are present and pinned,
    # so "not downloaded" would send the operator to a download that cannot help.
    monkeypatch.setattr(engine_mod, "probe", lambda: engine_mod.Availability(True))
    refusal = "the verified model file /x/ggml-base.bin has 2 hardlinks"

    async def _ensure(_model):
        return None

    store = type(
        "S",
        (),
        {
            "ensure": staticmethod(_ensure),
            "status": {"step": "failed", "model": "base", "error": refusal, "refused": True},
        },
    )()
    monkeypatch.setattr(models, "store", lambda: store)
    eng = engine_mod.WhisperEngine()
    result = await eng.ensure_loaded("base", "en")
    assert not result.ok
    assert result.code != engine_mod.CODE_MODEL_MISSING
    assert result.detail == refusal
    assert not eng.loaded


@pytest.mark.asyncio
async def test_an_unavailable_recogniser_short_circuits_before_any_download(monkeypatch):
    """No point pulling 148MB for an engine that cannot load it."""
    monkeypatch.setattr(
        engine_mod, "probe", lambda: engine_mod.Availability(False, engine_mod.CODE_EXTRA_MISSING)
    )

    async def _explode(_model):
        raise AssertionError("must not touch the model store")

    monkeypatch.setattr(
        models, "store", lambda: type("S", (), {"ensure": staticmethod(_explode)})()
    )
    result = await engine_mod.WhisperEngine().ensure_loaded("base", "en")
    assert result.code == engine_mod.CODE_EXTRA_MISSING


@pytest.mark.asyncio
async def test_decoding_without_a_loaded_model_is_empty_not_an_error(fake_engine):
    eng, _ = fake_engine
    assert await eng.decode(np.zeros(16000, dtype=np.float32)) == ""


@pytest.mark.asyncio
async def test_a_failed_decode_raises_rather_than_looking_like_silence(fake_engine, monkeypatch):
    """A failure and a quiet room must not answer the same thing.

    Returning "" here is what made a lost utterance invisible: the session read it
    as "nothing heard", the transport dropped the empty final, and the user's
    dictation disappeared with no error anywhere.
    """
    eng, fake = fake_engine
    await eng.ensure_loaded("base", "en")

    def _boom(*_a, **_k):
        raise RuntimeError("native fault")

    monkeypatch.setattr(fake, "transcribe", _boom)
    with pytest.raises(engine_mod.DecodeFailed) as caught:
        await eng.decode(np.zeros(16000, dtype=np.float32))
    assert caught.value.code == engine_mod.CODE_DECODE_FAILED
    assert "native fault" in caught.value.detail


@pytest.mark.asyncio
async def test_a_non_zero_native_status_is_a_failure_not_an_empty_transcript(
    fake_engine, monkeypatch
):
    """The failure pywhispercpp throws away.

    ``Model._transcribe`` calls ``whisper_full`` as a bare statement and then reads
    ``whisper_full_n_segments``, which is 0 after a failed encode -- so its public
    answer for a failure is the same empty list a silent recording produces. The
    engine reads the status itself, which is the only place the difference exists.
    """
    eng, fake = fake_engine
    await eng.ensure_loaded("base", "en")
    binding = _FakeBinding(status=-6)
    monkeypatch.setattr(engine_mod, "_whisper_binding", lambda: binding)
    _make_native(monkeypatch, fake)

    with pytest.raises(engine_mod.DecodeFailed) as caught:
        await eng.decode(np.zeros(16000, dtype=np.float32))
    assert caught.value.code == engine_mod.CODE_DECODE_FAILED
    assert "-6" in caught.value.detail
    assert binding.aborts_assigned == 1, "the abort callback must still be wired"


@pytest.mark.asyncio
async def test_a_status_read_through_the_binding_still_returns_the_segments(
    fake_engine, monkeypatch
):
    """The checked path is the normal path, so it must transcribe like the other one."""
    eng, fake = fake_engine
    await eng.ensure_loaded("base", "en")
    monkeypatch.setattr(engine_mod, "_whisper_binding", lambda: _FakeBinding())
    _make_native(monkeypatch, fake, texts=(" hello ", "world"))
    assert await eng.decode(np.zeros(16000, dtype=np.float32)) == "hello world"


@pytest.mark.asyncio
async def test_an_aborted_decode_is_not_reported_as_a_failure(fake_engine, monkeypatch):
    """An abort unwinds the encoder, so whisper.cpp reports the SAME -6 as a fault.

    The predicate that would have asked for the abort is the only thing that tells
    them apart, and a superseded partial is a protocol non-event -- raising for it
    would turn every fast typist's second partial into an error frame.
    """
    eng, fake = fake_engine
    await eng.ensure_loaded("base", "en")

    def _newer_audio_arrives() -> None:
        # From INSIDE the native call, which is where a real supersession lands:
        # `decode` claims its own generation on entry, so bumping beforehand would
        # only make this request the newest one.
        eng._generation += 1

    monkeypatch.setattr(
        engine_mod,
        "_whisper_binding",
        lambda: _FakeBinding(status=-6, on_full=_newer_audio_arrives),
    )
    _make_native(monkeypatch, fake)
    assert await eng.decode(np.zeros(16000, dtype=np.float32), superseding=True) == ""


@pytest.mark.asyncio
async def test_a_host_without_the_low_level_binding_still_decodes(fake_engine, monkeypatch):
    """Reading the status is best-effort; transcribing is not.

    A renamed private member in a later pywhispercpp, or an extension module that
    cannot be reached, must cost the status and nothing else.
    """
    eng, fake = fake_engine
    await eng.ensure_loaded("base", "en")
    monkeypatch.setattr(engine_mod, "_whisper_binding", lambda: None)
    assert await eng.decode(np.zeros(16000, dtype=np.float32)) == "hello world"
    assert fake.decoded_samples, "the library's own transcribe was never called"


@pytest.mark.asyncio
async def test_a_superseded_partial_aborts_instead_of_returning_text(fake_engine):
    """A stale partial must not overwrite a newer one in the user's textbox.

    Supersession is about a request that arrives WHILE a decode is in flight, so
    the generation has to move during the decode. The fake does that from inside
    ``transcribe``, which is where the real abort callback is polled.
    """
    eng, fake = fake_engine
    await eng.ensure_loaded("base", "en")

    def _newer_audio_arrives(pcm, abort_callback=None, **_kw):
        eng._generation += 1
        assert abort_callback is not None and abort_callback()
        return []

    fake.transcribe = _newer_audio_arrives
    assert await eng.decode(np.zeros(16000, dtype=np.float32), superseding=True) == ""


@pytest.mark.asyncio
async def test_a_final_decode_is_never_aborted_by_supersession(fake_engine):
    """A final is the text the user keeps, so nothing may cut it short."""
    eng, fake = fake_engine
    await eng.ensure_loaded("base", "en")
    seen: list[bool] = []

    def _newer_audio_arrives(pcm, abort_callback=None, **_kw):
        eng._generation += 1
        seen.append(abort_callback() if abort_callback is not None else False)
        return [_FakeSegment("hello world")]

    fake.transcribe = _newer_audio_arrives
    assert await eng.decode(np.zeros(16000, dtype=np.float32)) == "hello world"
    assert seen == [False], "a final must not report itself as superseded"


@pytest.mark.asyncio
async def test_a_key_mismatch_refuses_without_raising(fake_engine):
    """A refusal is a protocol non-event: the caller answers it by preparing again.

    Kept as "" rather than folded into DecodeFailed because the two need different
    handling -- the session RETRIES this one, and turning it into an error frame
    would surface an operator's mid-meeting model change as a failed dictation.
    """
    eng, _ = fake_engine
    await eng.ensure_loaded("base", "en")
    stale = engine_mod.LoadedKey("/somewhere/else.bin", "en", 4)
    assert await eng.decode(np.zeros(16000, dtype=np.float32), expect=stale) == ""


@pytest.mark.asyncio
async def test_a_prewarm_whose_throwaway_decode_fails_still_reports_the_load(
    fake_engine, monkeypatch
):
    """Prewarm reports on the LOAD, and the model is loaded.

    Failing it would refuse a session the user could still dictate into, and the real
    decode that follows raises on its own behalf if the fault persists.
    """
    eng, fake = fake_engine

    def _boom(*_a, **_k):
        raise RuntimeError("native fault")

    monkeypatch.setattr(fake, "transcribe", _boom)
    result = await eng.prewarm("base", "en")
    assert result.ok
    assert eng.loaded


@pytest.mark.asyncio
async def test_segments_are_joined_and_stripped(fake_engine, monkeypatch):
    eng, fake = fake_engine
    await eng.ensure_loaded("base", "en")
    monkeypatch.setattr(fake, "_texts", (" one ", "", "  two  "))
    assert await eng.decode(np.zeros(16000, dtype=np.float32)) == "one two"


@pytest.mark.asyncio
async def test_an_idle_model_is_released(fake_engine, monkeypatch):
    eng, _ = fake_engine
    await eng.ensure_loaded("base", "en")
    assert eng.loaded
    # Pretend the last use was longer ago than the window.
    monkeypatch.setattr(eng, "_last_used", eng._last_used - 10_000)
    assert await eng.maybe_evict()
    assert not eng.loaded


@pytest.mark.asyncio
async def test_a_recently_used_model_is_kept(fake_engine):
    eng, _ = fake_engine
    await eng.ensure_loaded("base", "en")
    assert not await eng.maybe_evict()
    assert eng.loaded


@pytest.mark.asyncio
async def test_a_zero_window_evicts_as_soon_as_the_model_is_idle(fake_engine, monkeypatch):
    """Zero is the tightest setting, not a disable.

    `limits.MIN_IDLE_EVICT_SECS` documents zero as "release the model as soon as it
    goes idle", for a memory-constrained host. Reading ``<= 0`` as "never" inverted
    that exactly: the one value an operator picks to bound memory hardest was the
    one that pinned 148 MB (1.6 GB at the largest model) for the life of the
    process. "Never release" is spelled with the maximum window, which
    `limits.MAX_IDLE_EVICT_SECS` documents as indistinguishable from it.
    """
    eng, _ = fake_engine
    monkeypatch.setattr(eng, "_idle_evict_secs", 0)
    await eng.ensure_loaded("base", "en")
    assert await eng.maybe_evict()
    assert not eng.loaded


@pytest.mark.asyncio
async def test_close_is_idempotent(fake_engine):
    eng, _ = fake_engine
    await eng.ensure_loaded("base", "en")
    await eng.close()
    await eng.close()
    assert not eng.loaded


@pytest.mark.asyncio
async def test_prewarm_pays_the_first_decode_up_front(fake_engine):
    """A prewarm that only loaded would leave graph allocation on the user's click."""
    eng, fake = fake_engine
    assert (await eng.prewarm("base", "en")).ok
    assert eng.loaded
    assert fake.decoded_samples, "prewarm must run a decode, not just a load"


@pytest.mark.asyncio
async def test_prewarm_does_not_repeat_inference_until_the_model_is_reloaded(fake_engine):
    eng, fake = fake_engine
    assert (await eng.prewarm("base", "en")).ok
    assert (await eng.prewarm("base", "en")).ok
    assert len(fake.decoded_samples) == 1
    await eng.close()
    assert (await eng.prewarm("base", "en")).ok
    assert len(fake.decoded_samples) == 2


@pytest.mark.asyncio
async def test_a_prewarm_does_not_count_as_use_for_the_boot_gate(
    fake_engine, monkeypatch, tmp_path
):
    """The boot prewarm is gated on recent use; its own decode must not feed that gate."""
    monkeypatch.setattr(models, "models_dir", lambda: tmp_path)
    monkeypatch.setattr(models, "_last_usage_stamp", None)
    eng, fake = fake_engine
    assert (await eng.prewarm("base", "en")).ok
    assert fake.decoded_samples
    assert not models.usage_marker_path().exists()


@pytest.mark.asyncio
async def test_a_real_decode_stamps_use_for_the_boot_gate(fake_engine, monkeypatch, tmp_path):
    monkeypatch.setattr(models, "models_dir", lambda: tmp_path)
    monkeypatch.setattr(models, "_last_usage_stamp", None)
    eng, _fake = fake_engine
    await eng.ensure_loaded("base", "en")
    audio = np.zeros(engine_mod.SAMPLE_RATE_HZ, dtype=np.float32)
    assert await eng.decode(audio) == "hello world"
    await asyncio.gather(*eng._stamp_tasks)
    assert models.used_within(60)


@pytest.mark.asyncio
async def test_the_usage_stamp_write_runs_off_the_event_loop(fake_engine, monkeypatch, tmp_path):
    """A stalled filesystem must not freeze the gateway's loop on the first dictation."""
    monkeypatch.setattr(models, "models_dir", lambda: tmp_path)
    monkeypatch.setattr(models, "_last_usage_stamp", None)
    writers: list[int] = []
    monkeypatch.setattr(models, "write_usage_marker", lambda: writers.append(threading.get_ident()))
    eng, _fake = fake_engine
    await eng.ensure_loaded("base", "en")
    audio = np.zeros(engine_mod.SAMPLE_RATE_HZ, dtype=np.float32)
    assert await eng.decode(audio) == "hello world"
    assert await eng.decode(audio) == "hello world"  # inside the interval: no second write
    await asyncio.gather(*eng._stamp_tasks)
    assert len(writers) == 1
    assert writers[0] != threading.get_ident()


@pytest.mark.asyncio
async def test_a_stalled_usage_stamp_does_not_hold_the_transcript(
    fake_engine, monkeypatch, tmp_path
):
    """The final's deadline would cancel a decode still waiting on the marker write."""
    monkeypatch.setattr(models, "models_dir", lambda: tmp_path)
    monkeypatch.setattr(models, "_last_usage_stamp", None)
    release = threading.Event()
    written = threading.Event()

    def _stalled_write() -> None:
        release.wait(60)
        written.set()

    monkeypatch.setattr(models, "write_usage_marker", _stalled_write)
    eng, _fake = fake_engine
    await eng.ensure_loaded("base", "en")
    audio = np.zeros(engine_mod.SAMPLE_RATE_HZ, dtype=np.float32)
    try:
        # The ceiling only names a hang; the verdict is the line after it.
        assert await asyncio.wait_for(eng.decode(audio), timeout=30) == "hello world"
        assert not written.is_set(), "the transcript must not wait for the marker write"
        assert eng._stamp_tasks, "the write is still in flight, held by the engine"
    finally:
        release.set()
        await asyncio.gather(*eng._stamp_tasks)


@pytest.mark.asyncio
async def test_a_session_stop_aborts_its_partial_but_never_its_final(fake_engine):
    eng, fake = fake_engine
    await eng.ensure_loaded("base", "en")
    audio = np.zeros(engine_mod.SAMPLE_RATE_HZ, dtype=np.float32)
    assert await eng.decode(audio, superseding=True, abort_if=lambda: True) == ""
    assert fake.decoded_samples == []
    assert await eng.decode(audio, abort_if=lambda: True) == "hello world"
