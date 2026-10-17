"""Stage timings for the local speech path.

The recorder is what made this path's cost measurable at all: before it, a slow
dictation was one undifferentiated wait, and the hash, the load and the first decode
were indistinguishable from each other.

Its tests use fabricated samples rather than real inference, so the assertions do not
depend on the speed of the machine running the suite.
"""

from __future__ import annotations

import pytest

from kiro_crew.stt import telemetry


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> telemetry.Recorder:
    """A fresh recorder in place of the process-wide one.

    Replaced through ``monkeypatch`` rather than by calling ``reset()`` on the real
    singleton: the recorder is module state, and a test that mutates it in place
    leaves its samples visible to whatever runs next in the same process.
    """
    fresh = telemetry.Recorder()
    monkeypatch.setattr(telemetry, "_recorder", fresh)
    return fresh


def _sample(
    kind: str, *, audio_ms: float, wall_ms: float, aborted: bool = False
) -> telemetry.DecodeSample:
    return telemetry.DecodeSample(
        kind=kind,
        audio_ms=audio_ms,
        wall_ms=wall_ms,
        rtf=wall_ms / audio_ms if audio_ms else 0.0,
        aborted=aborted,
    )


class TestRecorderHoldsNoContent:
    def test_a_sample_has_no_field_that_could_hold_a_transcript(self):
        """The privacy rule, pinned as a shape rather than as a promise.

        The recording API takes a duration and a kind, so a function that never
        receives the text cannot leak it. This asserts the field set, because the way
        content arrives in telemetry is by someone adding a field for it.
        """
        fields = set(telemetry.DecodeSample.__dataclass_fields__)
        assert fields == {"kind", "audio_ms", "wall_ms", "rtf", "queue_wait_ms", "aborted", "at"}
        load_fields = set(telemetry.LoadSample.__dataclass_fields__)
        assert load_fields == {"model", "size_bytes", "hash_ms", "load_ms", "first_decode_ms", "at"}

    def test_the_snapshot_is_json_safe_numbers_and_enums(self, recorder: telemetry.Recorder):
        recorder.record_hash("base", 110.0)
        recorder.record_load("base", 147_951_465, 90.0)
        recorder.record_decode(_sample(telemetry.KIND_FINAL, audio_ms=11_000, wall_ms=650))
        snapshot = recorder.snapshot()
        # The model name is a value the user chose in settings and already appears in
        # the status payload; everything else is a number.
        assert snapshot["last_load"]["model"] == "base"
        assert isinstance(snapshot["last_final"]["rtf"], float)
        assert snapshot["decodes"][0]["kind"] == telemetry.KIND_FINAL


class TestLoadEpisodesAreSplitByPhase:
    def test_hash_load_and_first_decode_are_reported_separately(self, recorder: telemetry.Recorder):
        """One total made a 5.5 s digest check look like a slow model.

        Measured on a 32-core aarch64 host, ``large-v3-turbo``'s cold start is
        ~19.5 s of which 5.48 s is the SHA-256 over 1.6 GB. Those have completely
        different remedies (a faster disk, versus a smaller model), so a single
        number is not an answer.
        """
        recorder.record_hash("large-v3-turbo", 5_480.0)
        recorder.record_load("large-v3-turbo", 1_624_555_275, 540.0)
        recorder.record_decode(_sample(telemetry.KIND_PREWARM, audio_ms=1_000, wall_ms=13_510))
        episode = recorder.snapshot()["last_load"]
        assert episode["hash_ms"] == 5_480.0
        assert episode["load_ms"] == 540.0
        # The graph allocation the first decode pays belongs to the cold start, not
        # to the utterance that happened to trigger it.
        assert episode["first_decode_ms"] == 13_510

    def test_a_later_decode_does_not_overwrite_the_first(self, recorder: telemetry.Recorder):
        recorder.record_hash("base", 110.0)
        recorder.record_load("base", 147_951_465, 90.0)
        recorder.record_decode(_sample(telemetry.KIND_PREWARM, audio_ms=1_000, wall_ms=570))
        recorder.record_decode(_sample(telemetry.KIND_FINAL, audio_ms=11_000, wall_ms=650))
        assert recorder.snapshot()["last_load"]["first_decode_ms"] == 570

    def test_a_hash_is_never_folded_into_another_model_s_load(self, recorder: telemetry.Recorder):
        """The 50x misattribution: verify one model, load a different one.

        ``_verified_on_disk`` records the hash BEFORE comparing the digest, so a
        failed verification leaves a pending hash with nothing to consume it, and
        nothing clears it when no load follows. The first version discarded the model
        argument entirely, so the next load of ANY model inherited that time --
        ``large-v3-turbo``'s 5.48 s reported against ``base``'s real ~110 ms.
        """
        recorder.record_hash("large-v3-turbo", 5_480.0)
        recorder.record_load("base", 147_951_465, 90.0)
        episode = recorder.snapshot()["last_load"]
        assert episode["model"] == "base"
        assert episode["hash_ms"] == 0.0, "another model's digest time was folded in"
        # The verification still counted: it happened, and the count is what says a
        # digest check is being paid at all.
        assert recorder.snapshot()["hashes"] == 1

    def test_only_the_prewarm_decode_is_read_as_the_graph_build(self, recorder: telemetry.Recorder):
        """An utterance's own decode must never be published as a load phase.

        A prewarm runs with ``superseding=True``, so a boot prewarm racing a
        pointer-down prewarm aborts; the aborted sample is skipped, and without this
        restriction the next non-aborted decode fills the field. If that is a 60 s
        utterance, a field documented as a 30-40 ms graph build reports a minute.
        """
        recorder.record_hash("base", 110.0)
        recorder.record_load("base", 147_951_465, 90.0)
        recorder.record_decode(
            _sample(telemetry.KIND_PREWARM, audio_ms=1_000, wall_ms=12, aborted=True)
        )
        recorder.record_decode(_sample(telemetry.KIND_FINAL, audio_ms=60_000, wall_ms=74_000))
        assert recorder.snapshot()["last_load"]["first_decode_ms"] == 0.0

    def test_an_aborted_decode_is_not_taken_as_the_first(self, recorder: telemetry.Recorder):
        """An aborted decode's wall time is a fraction of the graph cost.

        Attributing it to the load would under-report the cold start, which is the
        number a user is trying to explain.
        """
        recorder.record_hash("base", 110.0)
        recorder.record_load("base", 147_951_465, 90.0)
        recorder.record_decode(
            _sample(telemetry.KIND_PREWARM, audio_ms=1_000, wall_ms=12, aborted=True)
        )
        assert recorder.snapshot()["last_load"]["first_decode_ms"] == 0.0


class TestThePayloadCarriesNoWallClock:
    def test_a_stamp_of_when_someone_spoke_never_reaches_the_payload(
        self, recorder: telemetry.Recorder
    ):
        """``at`` orders the recorder's own samples; it is not for the wire.

        The module's stated contract is durations and counts, and an absolute
        wall-clock time is neither -- it says when a person was dictating, on a body
        served to the browser and pasted into bug reports. The list already expresses
        ordering by being in order, so the field buys the payload nothing.
        """
        recorder.record_hash("base", 110.0)
        recorder.record_load("base", 147_951_465, 90.0)
        recorder.record_decode(_sample(telemetry.KIND_FINAL, audio_ms=11_000, wall_ms=650))
        snapshot = recorder.snapshot()
        for body in (snapshot["last_load"], snapshot["last_final"], snapshot["decodes"][0]):
            assert "at" not in body
        # The sample itself still carries it, so this is an omission at the boundary
        # rather than a measurement nobody takes.
        assert recorder._decodes[-1].at > 0

    def test_every_other_measured_field_does_reach_the_payload(self, recorder: telemetry.Recorder):
        """The projection is a denylist, and this is why it has to stay one.

        A field added to a sample is a measurement, and a measurement that silently
        failed to reach the status endpoint is the exact failure this module exists to
        prevent -- so anything not named private must appear.
        """
        recorder.record_decode(_sample(telemetry.KIND_FINAL, audio_ms=11_000, wall_ms=650))
        body = recorder.snapshot()["last_final"]
        expected = set(telemetry.DecodeSample.__dataclass_fields__) - telemetry._PRIVATE_FIELDS
        assert set(body) == expected

    def test_a_missing_sample_is_none_rather_than_an_empty_body(self, recorder: telemetry.Recorder):
        snapshot = recorder.snapshot()
        assert snapshot["last_load"] is None
        assert snapshot["last_final"] is None
        assert snapshot["last_decode"] is None

    def test_history_is_bounded(self, recorder: telemetry.Recorder):
        for _ in range(telemetry._HISTORY * 3):
            recorder.record_decode(_sample(telemetry.KIND_PARTIAL, audio_ms=1_000, wall_ms=10))
        assert len(recorder.snapshot()["decodes"]) == telemetry._HISTORY
