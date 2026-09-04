"""What a live session emits, and when.

The session only ever talks to the engine's interface, so these tests substitute a
recorded fake for it rather than a fake native model. That makes the assertions
about the session's own rules: the partial cadence, where a phrase is cut, and
that the final is a decode of everything rather than the partials pasted together.
"""

from __future__ import annotations

import numpy as np
import pytest

from kiro_crew.stt import engine as engine_mod
from kiro_crew.stt import models
from kiro_crew.stt import session as session_mod
from kiro_crew.stt import vad

SR = vad.SAMPLE_RATE_HZ


def _int16(seconds: float, amplitude: float = 0.3, freq: float = 220.0) -> bytes:
    """Speech-shaped int16 PCM: a syllable envelope over a tone.

    The envelope matters. A flat tone gives the detector no dynamics, so the noise
    floor it seeds from the first frame sits at signal level and nothing ever
    clears the speech margin. Real speech is amplitude-modulated by syllables,
    which is what this reproduces.
    """
    n = int(seconds * SR)
    t = np.arange(n, dtype=np.float32) / SR
    envelope = 0.5 + 0.5 * np.sin(2 * np.pi * 4.0 * t).astype(np.float32)
    wave = (amplitude * envelope * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    return (wave * 32767).astype("<i2").tobytes()


def _room_tone(seconds: float = 0.3, level: float = 0.001) -> bytes:
    """The quiet lead-in every real capture has before the first word.

    Fed first so the detector seeds its noise floor on the room rather than on
    speech, which is what a browser's microphone stream actually delivers.
    """
    n = int(seconds * SR)
    rng = np.random.default_rng(99)
    return ((rng.standard_normal(n) * level).astype(np.float32) * 32767).astype("<i2").tobytes()


def _silence_int16(seconds: float) -> bytes:
    return b"\x00\x00" * int(seconds * SR)


class _FakeEngine:
    """Records every decode so a test can assert what the session asked for."""

    def __init__(self, text: str = "spoken words", available: bool = True) -> None:
        self._text = text
        self._available = available
        self.decodes: list[tuple[int, bool]] = []
        self.evictions = 0
        self.loaded_with: list[tuple[str, str]] = []
        #: What the real engine would report as resident. Sessions snapshot it and
        #: hand it back on every decode, which is how a decode proves it ran on the
        #: model prepared for it rather than one another session swapped in.
        self.loaded_key = engine_mod.LoadedKey("/stub/ggml-base.bin", "en", 4)
        self.expected: list[object] = []
        #: Set to a `DecodeFailed` to make every decode fail. Assigned per test rather
        #: than fixed at construction because the interesting cases are transitions:
        #: a session that keeps working after a failed partial, and one that recovers.
        self.fail_with: Exception | None = None

    async def ensure_loaded(self, model_name: str, language: str):
        self.loaded_with.append((model_name, language))
        if self._available:
            return engine_mod.Availability(True)
        return engine_mod.Availability(False, engine_mod.CODE_EXTRA_MISSING, "no recogniser")

    async def decode(self, pcm, *, superseding: bool = False, expect=None) -> str:
        self.decodes.append((len(pcm), superseding))
        self.expected.append(expect)
        if self.fail_with is not None:
            raise self.fail_with
        return self._text

    async def maybe_evict(self) -> bool:
        self.evictions += 1
        return False


@pytest.fixture
def fake(monkeypatch, tmp_path):
    """A session wired to a fake engine, with the configured model present on disk."""
    monkeypatch.setattr(models, "models_dir", lambda: tmp_path)
    model = models.resolve(models.DEFAULT_MODEL)
    (tmp_path / model.filename).write_bytes(b"x" * model.size_bytes)
    eng = _FakeEngine()
    monkeypatch.setattr(engine_mod, "shared_engine", lambda **_kw: eng)
    return eng


def _session(**kw) -> session_mod.LocalSession:
    kw.setdefault("language", "en")
    kw.setdefault("silence_ms", 400)
    kw.setdefault("partial_interval_ms", 0)
    return session_mod.LocalSession(**kw)


class _Clock:
    """A monotonic clock that advances only when audio is delivered.

    The partial cadence is a wall-clock rule, so a loop that feeds a whole second
    of audio in under a millisecond cannot observe it, and real sleeps would make
    the assertion depend on host load. Advancing the clock by exactly the duration
    of each chunk makes the cadence observable and the result identical every run.
    """

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def _feed(session, data: bytes, chunk_ms: int = 100, clock=None) -> list:
    """Stream *data* the way a socket delivers it, and collect every event.

    Chunk size matters. A browser sends short frames, so a commit and a partial
    interleave across calls; handing a whole second to one ``feed`` lets a phrase
    commit short-circuit the partial that would otherwise follow it.
    """
    step = int(SR * chunk_ms / 1000) * 2
    events: list = []
    for i in range(0, len(data), step):
        if clock is not None:
            clock.now += chunk_ms / 1000.0
        events.extend(await session.feed(data[i : i + step]))
        if session.ended:
            break
    return events


async def _started(**kw) -> session_mod.LocalSession:
    """A prepared session that has already heard its room-tone lead-in."""
    session = _session(**kw)
    await session.prepare()
    await session.feed(_room_tone())
    return session


# ── prepare ──


@pytest.mark.asyncio
async def test_prepare_is_silent_when_everything_is_ready(fake):
    assert await _session().prepare() == []


@pytest.mark.asyncio
async def test_a_first_run_download_is_reported_before_prepare_blocks_on_it(monkeypatch, tmp_path):
    """A silent multi-hundred-megabyte transfer is indistinguishable from a hang.

    prepare() cannot announce it, because prepare() returns only once the transfer
    is done. pending_download() is what a transport asks first.
    """
    monkeypatch.setattr(models, "models_dir", lambda: tmp_path)
    eng = _FakeEngine()
    monkeypatch.setattr(engine_mod, "shared_engine", lambda **_kw: eng)
    session = _session()
    pending = session.pending_download()
    assert pending is not None
    assert pending.size_bytes == models.resolve(models.DEFAULT_MODEL).size_bytes
    assert await session.prepare() == [], "prepare reports errors only"


@pytest.mark.asyncio
async def test_nothing_is_pending_once_the_model_is_on_disk(fake):
    assert _session().pending_download() is None


@pytest.mark.asyncio
async def test_prepare_surfaces_an_unavailable_recogniser_with_its_code(monkeypatch, tmp_path):
    monkeypatch.setattr(models, "models_dir", lambda: tmp_path)
    model = models.resolve(models.DEFAULT_MODEL)
    (tmp_path / model.filename).write_bytes(b"x" * model.size_bytes)
    monkeypatch.setattr(engine_mod, "shared_engine", lambda **_kw: _FakeEngine(available=False))
    events = await _session().prepare()
    assert [e.kind for e in events] == [session_mod.KIND_ERROR]
    assert events[0].code == engine_mod.CODE_EXTRA_MISSING


@pytest.mark.asyncio
async def test_the_configured_model_and_language_reach_the_engine(fake):
    await _session(model_name="small", language="fr").prepare()
    assert fake.loaded_with == [("small", "fr")]


# ── partials ──


@pytest.mark.asyncio
async def test_a_partial_is_emitted_once_there_is_audible_speech(fake):
    """A short phrase is padded to the recogniser's minimum, not withheld."""
    session = await _started()
    events = await _feed(session, _int16(0.6))
    partials = [e for e in events if e.kind == session_mod.KIND_PARTIAL]
    assert partials and partials[-1].text == "spoken words"


@pytest.mark.asyncio
async def test_room_tone_alone_produces_no_partial(fake):
    """Nothing was said, so there is nothing to show and nothing to decode."""
    session = await _started()
    assert await _feed(session, _room_tone(1.5)) == []
    assert fake.decodes == []


@pytest.mark.asyncio
async def test_partials_are_abortable_and_the_final_is_not(fake):
    session = await _started()
    await _feed(session, _int16(1.5))
    await session.finish()
    assert any(superseding for _, superseding in fake.decodes), "no abortable partial ran"
    assert fake.decodes[-1][1] is False, "the final must never be aborted"


@pytest.mark.asyncio
async def test_the_partial_cadence_bounds_how_often_a_decode_runs(fake, monkeypatch):
    """Every audio frame must not cost a decode."""
    clock = _Clock()
    monkeypatch.setattr(session_mod.time, "monotonic", clock)

    eager = await _started(partial_interval_ms=0)
    await _feed(eager, _int16(2.5), clock=clock)
    eager_interim = sum(1 for _, superseding in fake.decodes if superseding)

    fake.decodes.clear()
    throttled = await _started(partial_interval_ms=60_000)
    await _feed(throttled, _int16(2.5), clock=clock)
    throttled_interim = sum(1 for _, superseding in fake.decodes if superseding)

    # The cadence bounds how often an interim decode REPEATS, not whether the first
    # one runs: the first partial fires as soon as there is speech, because making
    # the user wait out the interval to see any text at all would defeat the point.
    # A phrase commit is an event rather than a tick, so it reports whatever it
    # settled regardless of how long ago the last partial was.
    assert eager_interim > throttled_interim
    assert throttled_interim == 1, "only the immediate first partial should get through"


@pytest.mark.asyncio
async def test_a_failed_final_decode_becomes_an_error_event_not_an_empty_final(fake):
    """The transport DROPS an empty final, so returning one loses the utterance.

    This is the whole failure: whisper.cpp reported a failed encode, the engine
    answered "", the session called that silence, the empty final was dropped, and the
    dictation the user had just finished speaking vanished with nothing on screen.
    """
    session = await _started()
    fake.fail_with = engine_mod.DecodeFailed("whisper.cpp decode returned -6")
    await session.feed(_int16(1.5))
    event = await session.finish()
    assert event.kind == session_mod.KIND_ERROR
    assert event.code == engine_mod.CODE_DECODE_FAILED
    assert event.text, "an error event with no advisory text is untranslatable and blank"


@pytest.mark.asyncio
async def test_a_failed_partial_decode_keeps_the_session_alive(fake):
    """One bad partial must not end a session the speaker is still talking into.

    A partial is cosmetic and the next one is moments away, so the trade runs the
    other way from the final's: skip it, log it, keep listening. Ending the session
    here would stop transcribing mid-sentence over a decode nobody would have kept.
    """
    session = await _started()
    fake.fail_with = engine_mod.DecodeFailed("whisper.cpp decode returned -6")
    events = await _feed(session, _int16(1.5))
    assert fake.decodes, "no partial decode was attempted, so nothing was skipped"
    assert events == [], "a failed partial was surfaced instead of skipped"
    assert not session.ended

    # And the session still delivers once decoding recovers.
    fake.fail_with = None
    final = await session.finish()
    assert final.kind == session_mod.KIND_FINAL
    assert final.text == "spoken words"


@pytest.mark.asyncio
async def test_a_failed_phrase_commit_loses_no_text_from_the_final(fake, monkeypatch):
    """Committed text is display-only, so a failed commit is skipped, not surfaced.

    The phrase audio stays in the full buffer either way, so the final still decodes
    everything that was said.
    """
    monkeypatch.setattr(session_mod, "MIN_COMMIT_SECS", 0.2)
    session = await _started()
    fake.fail_with = engine_mod.DecodeFailed("whisper.cpp decode returned -6")
    await _feed(session, _int16(1.0) + _silence_int16(0.2) + _int16(0.5))
    assert fake.decodes, "no commit decode was attempted, so nothing was skipped"
    assert not session.ended
    fake.fail_with = None
    final = await session.finish()
    assert final.kind == session_mod.KIND_FINAL
    assert final.text == "spoken words"
    # The whole utterance, not just the phrase that survived: the commit's audio was
    # never removed from the full buffer.
    assert fake.decodes[-1][0] >= int(1.5 * SR)


@pytest.mark.asyncio
async def test_the_batch_path_reports_a_failed_decode_as_unavailable(fake):
    """`transcribe_pcm` answers an Availability, and its caller already reads it.

    Raising out of it would take a Slack voice memo's whole message with it, while
    returning ("", ok) would report a memo the recogniser could not hear.
    """
    fake.fail_with = engine_mod.DecodeFailed("whisper.cpp decode returned -6")
    text, result = await session_mod.transcribe_pcm(np.zeros(SR, dtype=np.float32))
    assert text == ""
    assert not result.ok
    assert result.code == engine_mod.CODE_DECODE_FAILED


@pytest.mark.asyncio
async def test_an_empty_decode_does_not_blank_the_textbox(fake, monkeypatch):
    """An empty partial means superseded, not silence."""
    session = await _started()
    monkeypatch.setattr(fake, "_text", "")
    assert await _feed(session, _int16(1.5)) == []


@pytest.mark.asyncio
async def test_committed_text_prefixes_later_partials(fake, monkeypatch):
    """Text already recognised must not vanish when a phrase is cut."""
    clock = _Clock()
    monkeypatch.setattr(session_mod.time, "monotonic", clock)
    session = await _started(partial_interval_ms=0, silence_ms=900)
    # Speech, then a gap long enough to commit the phrase but short of the endpoint.
    await _feed(session, _int16(1.5), clock=clock)
    await _feed(session, _silence_int16(0.3), clock=clock)
    assert session._committed, "a phrase boundary must commit its text"
    committed = session._committed
    events = await _feed(session, _int16(1.5), clock=clock)
    partials = [e for e in events if e.kind == session_mod.KIND_PARTIAL]
    assert partials, "speech after a commit must still produce partials"
    assert partials[-1].text.startswith(committed), "committed text must never be lost"
    assert len(partials[-1].text) > len(committed)


# ── endpointing ──


@pytest.mark.asyncio
async def test_the_detector_ending_the_utterance_produces_the_final(fake):
    session = await _started(silence_ms=300)
    await _feed(session, _int16(1.5))
    events = await _feed(session, _silence_int16(0.8))
    assert events[-1].kind == session_mod.KIND_FINAL
    assert [e.kind for e in events].count(session_mod.KIND_FINAL) == 1


@pytest.mark.asyncio
async def test_the_final_decodes_everything_heard_not_the_last_phrase(fake):
    """Partials are approximate; the final gets the whole utterance's context."""
    session = _session(silence_ms=300, partial_interval_ms=60_000)
    await session.prepare()
    lead = _room_tone()
    total = len(lead) // 2
    await session.feed(lead)
    for _ in range(3):
        chunk = _int16(1.0)
        total += len(chunk) // 2
        await session.feed(chunk)
    await session.finish()
    final_samples = fake.decodes[-1][0]
    assert final_samples == total


@pytest.mark.asyncio
async def test_feeding_after_the_session_ends_is_ignored(fake):
    """Terminal means terminal -- but only `finish()` is terminal."""
    session = await _started(silence_ms=300)
    await _feed(session, _int16(1.5))
    await session.finish()
    assert session.ended
    assert await session.feed(_int16(1.0)) == []


@pytest.mark.asyncio
async def test_the_detector_finalising_an_utterance_does_not_end_the_session(fake):
    """A pause finalises one utterance; the session keeps listening.

    Ending it here broke the continuous consumer outright: the socket closed on the
    speaker's first pause, `useMeetingTranscription` reported "disconnected" and ran
    its cleanup, and because its session binding keys only on `status` nothing
    restarted it -- transcription stayed dead for the rest of the meeting while the
    UI still showed Live. Both clients accumulate finals rather than treating the
    first as the end.
    """
    session = await _started(silence_ms=300)
    await _feed(session, _int16(1.5))
    first = await _feed(session, _silence_int16(0.8))
    assert [e.kind for e in first].count(session_mod.KIND_FINAL) == 1
    assert not session.ended, "the detector must not end the session"

    # A second utterance on the SAME session must transcribe too.
    await _feed(session, _int16(1.5))
    second = await _feed(session, _silence_int16(0.8))
    assert [e.kind for e in second].count(session_mod.KIND_FINAL) == 1
    assert not session.ended


@pytest.mark.asyncio
async def test_the_chunk_that_ends_an_utterance_is_split_not_filed_whole(fake):
    """Speech resumed inside the ENDPOINT chunk belongs to the next utterance.

    A caller's chunk is much longer than a detector frame -- 100 ms here against 20 ms
    -- so the chunk that trips the endpoint can also carry the start of the next
    utterance. Filing it whole attributed that audio to the utterance that just closed,
    where it sits behind a hangover of silence and contributes nothing, and clipped the
    onset off the word it belongs to.

    Asserted as CONSERVATION rather than against a predicted split point: every sample
    fed is accounted for exactly once, and the tail is non-empty. A sample count that
    depends on which frame the hangover expires in would be pinning the detector's
    arithmetic instead of the session's rule.
    """
    session = await _started(silence_ms=300)
    speech = _int16(1.5)
    await _feed(session, speech)

    # One chunk: enough silence to expire the hangover, then speech again. Fed in a
    # single `feed` call, which is the case the split exists for.
    ending = _silence_int16(0.5) + _int16(0.4)
    fed_samples = (len(speech) + len(ending)) // 2 + len(_room_tone()) // 2
    events = await session.feed(ending)

    assert [e.kind for e in events].count(session_mod.KIND_FINAL) == 1, "no final was emitted"
    carried = session._buffers.total_samples
    assert carried > 0, "the resumed speech was filed under the utterance that just ended"

    # The final is the last decode: `_decode_utterance` runs on the whole buffer, and
    # nothing decodes the carried tail. Committed phrases also decode non-superseding,
    # so filtering on that flag would not isolate it.
    final_samples, superseding = fake.decodes[-1]
    assert not superseding, "the final must not be a superseding partial"
    assert final_samples + carried == fed_samples, (
        f"audio was lost or duplicated: decoded {final_samples} + carried {carried} "
        f"!= {fed_samples} fed"
    )


@pytest.mark.asyncio
async def test_a_later_utterance_does_not_carry_the_previous_one_forward(fake):
    """Re-arming has to reset the audio and the committed text, not just the state.

    Otherwise utterance two decodes with utterance one's audio in front of it and
    its text prefixed to the result.
    """
    session = await _started(silence_ms=300)
    await _feed(session, _int16(1.5))
    await _feed(session, _silence_int16(0.8))
    assert not session.has_pending_audio, "the finalised utterance's audio must be dropped"
    assert session._committed == ""
    await _feed(session, _int16(0.5))
    assert session.has_pending_audio


@pytest.mark.asyncio
async def test_pending_audio_is_what_teardown_asks_about(fake):
    """ "A final was sent" and "there is audio to finalise" are different questions.

    Over a multi-utterance session both are true at once, and a teardown that reads
    the first discards whatever was said after the last detected pause.
    """
    session = _session(silence_ms=300)
    await session.prepare()
    assert not session.has_pending_audio, "nothing has been heard yet"
    await _feed(session, _room_tone())
    await _feed(session, _int16(1.5))
    await _feed(session, _silence_int16(0.8))  # utterance 1 finalised
    await _feed(session, _int16(1.0))  # utterance 2 still open
    assert session.has_pending_audio, "the trailing utterance would have been dropped"
    tail = await session.finish()
    assert tail.kind == session_mod.KIND_FINAL
    assert session.ended


@pytest.mark.asyncio
async def test_the_audio_ceiling_finalises_the_session(fake, monkeypatch):
    """A client that streams forever must still terminate."""
    monkeypatch.setattr(session_mod, "MAX_SESSION_SECS", 1)
    session = await _started()
    events = await session.feed(_int16(1.5))
    assert [e.kind for e in events] == [session_mod.KIND_FINAL]
    assert session.ended


# ── silence and cancellation ──


@pytest.mark.asyncio
async def test_a_silent_recording_is_not_decoded_at_all(fake):
    """A muted or unplugged device delivers zeros, which must not be decoded."""
    session = _session()
    await session.prepare()
    await _feed(session, _silence_int16(2.0))
    event = await session.finish()
    assert event.kind == session_mod.KIND_FINAL
    assert event.text == ""
    assert fake.decodes == []


@pytest.mark.asyncio
async def test_cancel_yields_an_empty_final_and_no_decode(fake):
    session = await _started()
    await session.feed(_int16(1.2))
    before = len(fake.decodes)
    session.cancel()
    event = await session.finish()
    assert event.text == ""
    assert len(fake.decodes) == before, "an abandoned session must not decode"


@pytest.mark.asyncio
async def test_a_cancelled_session_stops_consuming_audio(fake):
    session = await _started()
    session.cancel()
    assert await session.feed(_int16(1.5)) == []


# ── short utterances ──


@pytest.mark.asyncio
async def test_a_one_word_utterance_is_padded_rather_than_dropped(fake):
    """The recogniser refuses a buffer under a second; a short command is still real."""
    session = await _started()
    await session.feed(_int16(0.4))
    event = await session.finish()
    assert event.text == "spoken words"
    assert fake.decodes[-1][0] == int(session_mod.MIN_DECODE_SECS * SR)


# ── output hygiene ──


@pytest.mark.asyncio
async def test_the_final_passes_through_the_hallucination_filter(fake, monkeypatch):
    monkeypatch.setattr(fake, "_text", "Subtitles by the Amara.org community")
    session = await _started()
    await session.feed(_int16(1.2))
    assert (await session.finish()).text == ""


@pytest.mark.asyncio
async def test_a_partial_is_not_filtered(fake, monkeypatch):
    """A partial is a prefix of speech in progress, so collapsing it would be wrong."""
    monkeypatch.setattr(fake, "_text", "No. No. No. No. No. No.")
    session = await _started()
    events = await _feed(session, _int16(1.5))
    partials = [e for e in events if e.kind == session_mod.KIND_PARTIAL]
    assert partials and partials[0].text.endswith("No. No. No. No. No. No.")


@pytest.mark.asyncio
async def test_every_decode_names_the_model_it_was_prepared_for(fake):
    """The engine is a process singleton, so a decode has to say what it expects.

    Without it, a second session whose configuration names a different model or
    language replaces the resident context and this session's next decode returns
    text from the wrong weights with nothing to show it happened.
    """
    session = await _started()
    await _feed(session, _int16(1.5))
    await session.finish()
    assert fake.expected, "nothing was decoded"
    assert all(e == fake.loaded_key for e in fake.expected)


@pytest.mark.asyncio
async def test_the_engine_is_offered_for_eviction_after_a_final(fake):
    """A 148MB resident model must not survive an idle gateway."""
    session = await _started()
    await session.feed(_int16(1.2))
    await session.finish()
    assert fake.evictions == 1


# ── batch path ──


@pytest.mark.asyncio
async def test_the_batch_path_shares_the_resident_model(fake):
    text, availability = await session_mod.transcribe_pcm(
        np.zeros(SR, dtype=np.float32), model_name="base", language="en"
    )
    assert availability.ok
    assert text == "spoken words"
    assert fake.decodes == [(SR, False)]


@pytest.mark.asyncio
async def test_the_batch_path_reports_an_unavailable_engine(monkeypatch, tmp_path):
    monkeypatch.setattr(models, "models_dir", lambda: tmp_path)
    monkeypatch.setattr(engine_mod, "shared_engine", lambda **_kw: _FakeEngine(available=False))
    text, availability = await session_mod.transcribe_pcm(np.zeros(SR, dtype=np.float32))
    assert text == ""
    assert availability.code == engine_mod.CODE_EXTRA_MISSING


@pytest.mark.asyncio
async def test_the_batch_path_filters_hallucinations(fake, monkeypatch):
    monkeypatch.setattr(fake, "_text", "Subtitles by the Amara.org community")
    text, _ = await session_mod.transcribe_pcm(np.zeros(SR, dtype=np.float32))
    assert text == ""


@pytest.mark.asyncio
async def test_a_refused_decode_is_reprepared_rather_than_reported_as_silence(fake):
    """A model swapped by a concurrent session must not silently eat an utterance.

    The engine refuses a decode whose resident model is not the one the session
    prepared for, and that refusal returns "" -- indistinguishable here from silence.
    The transport drops an empty final, so the utterance the user just spoke left no
    trace at all. Changing `stt.model` mid-meeting is enough to cause it.

    The counters are armed AFTER the audio has been fed, so they see only the
    final-decode path and not the commit decodes a live phrase also makes.
    """
    session = await _started(silence_ms=300)
    await _feed(session, _int16(1.5))

    calls: list[str] = []
    real_decode = session._engine.decode
    real_ensure = session._engine.ensure_loaded

    async def _refuse_once(pcm, **kw):
        calls.append("decode")
        if calls.count("decode") == 1:
            return ""  # the key check refused
        return await real_decode(pcm, **kw)

    async def _ensure(model_name, language):
        calls.append("ensure")
        return await real_ensure(model_name, language)

    session._engine.decode = _refuse_once  # type: ignore[method-assign]
    session._engine.ensure_loaded = _ensure  # type: ignore[method-assign]
    try:
        final = await session.finish()
    finally:
        session._engine.decode = real_decode  # type: ignore[method-assign]
        session._engine.ensure_loaded = real_ensure  # type: ignore[method-assign]
    assert calls == ["decode", "ensure", "decode"], calls
    assert final.text, "the utterance was lost instead of being decoded on retry"


@pytest.mark.asyncio
async def test_the_reprepare_retry_is_bounded_to_one_attempt(fake):
    """Bounded on purpose: two sessions with different settings must not starve.

    A genuinely empty transcript also has to be allowed to stay empty.
    """
    session = await _started(silence_ms=300)
    await _feed(session, _int16(1.5))

    decodes = 0
    real_decode = session._engine.decode

    async def _always_refuse(pcm, **kw):
        nonlocal decodes
        decodes += 1
        return ""

    session._engine.decode = _always_refuse  # type: ignore[method-assign]
    try:
        final = await session.finish()
    finally:
        session._engine.decode = real_decode  # type: ignore[method-assign]
    assert decodes == 2, f"expected exactly one retry, got {decodes} decodes"
    assert final.text == ""
