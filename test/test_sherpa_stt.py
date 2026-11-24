"""Sherpa-ONNX provider contract tests: no sherpa-onnx install, model download or network.

The recogniser is replaced by a fake engine and the archive by one built in the test,
so these pin the provider's own behaviour: the wiring of the provider name, the pinned
extraction, the per-load check, the utterance cutting and the session's events.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import io
import tarfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from kiro_crew import transcribe
from kiro_crew.config import sections_stt
from kiro_crew.dashboard import stt_stream
from kiro_crew.metrics import voice as voice_metrics
from kiro_crew.stt import models as whisper_models
from kiro_crew.stt import sherpa, sherpa_custom, sherpa_engine, sherpa_models
from kiro_crew.stt.engine import Availability
from kiro_crew.stt.limits import (
    DEFAULT_IDLE_EVICT_SECS,
    DEFAULT_TIMEOUT_SECS,
    MIN_PARTIAL_INTERVAL_MS,
)
from kiro_crew.stt.vad import DEFAULT_SILENCE_MS, SAMPLE_RATE_HZ

#: Upper bound on draining a finished session's events; reached only on a hang.
_EVENTS_BOUND_SECS = 10.0

# ── wiring ──


def test_the_provider_name_is_one_value_everywhere():
    for spelled in (
        sections_stt.STT_PROVIDER_SHERPA,
        transcribe.STT_PROVIDER_SHERPA,
        stt_stream.PROVIDER_SHERPA,
    ):
        assert spelled == sherpa.PROVIDER
    assert sherpa.PROVIDER in sections_stt._VALID_STT_PROVIDERS
    assert sections_stt._VALID_STT_PROVIDERS[-1] == sections_stt.STT_PROVIDER_OFF
    assert sherpa.PROVIDER in stt_stream._STREAMING_PROVIDERS
    assert sherpa.PROVIDER in voice_metrics.STT_PROVIDER_VALUES


def test_every_session_error_code_is_counted_by_the_voice_metrics():
    assert sherpa.SESSION_ERROR_CODES <= set(voice_metrics.STT_ERROR_CODES)


def test_an_unknown_stored_model_reads_as_the_default():
    assert sections_stt._validated_stt_sherpa_model(None) == sherpa_models.DEFAULT_MODEL
    assert sections_stt._validated_stt_sherpa_model("zipformer-vi-30m") == "zipformer-vi-30m"
    assert sections_stt._validated_stt_sherpa_model("../../etc") == sherpa_models.DEFAULT_MODEL
    assert sections_stt._validated_stt_sherpa_model(["x"]) == sherpa_models.DEFAULT_MODEL
    assert sections_stt.SttConfig().sherpa_model == sherpa_models.DEFAULT_MODEL


def test_files_keep_the_local_ceiling_and_split():
    cfg = SimpleNamespace(provider=sherpa.PROVIDER)
    assert transcribe.batch_duration_cap_secs(cfg) == transcribe._MAX_AUDIO_SECS
    assert transcribe.provider_splits_oversized(cfg) is True


def test_a_missing_package_names_the_extra(monkeypatch):
    monkeypatch.setattr(sherpa_engine.importlib.util, "find_spec", lambda name: None)
    result = sherpa.availability()
    assert not result.ok
    assert result.code == sherpa.CODE_EXTRA_MISSING
    assert "sherpa-onnx" in result.detail


@pytest.mark.parametrize(
    "system, machine, libc, python, expected",
    [
        ("Darwin", "arm64", None, (3, 12), True),
        ("Darwin", "x86_64", None, (3, 12), True),
        ("Linux", "x86_64", ("glibc", "2.17"), (3, 12), True),
        ("Linux", "aarch64", ("glibc", "2.39"), (3, 12), True),
        ("Linux", "x86_64", ("glibc", "2.12"), (3, 12), False),
        ("Linux", "armv7l", ("glibc", "2.31"), (3, 12), False),
        ("Linux", "armv7l", ("glibc", "2.35"), (3, 12), True),
        # musl: `platform.libc_ver` names no glibc.
        ("Linux", "x86_64", ("", ""), (3, 12), False),
        ("Linux", "riscv64", ("glibc", "2.39"), (3, 12), False),
        ("Windows", "AMD64", None, (3, 12), True),
        ("Windows", "x86", None, (3, 10), True),
        ("Windows", "ARM64", None, (3, 11), True),
        ("Windows", "ARM64", None, (3, 10), False),
        ("FreeBSD", "amd64", None, (3, 12), False),
    ],
)
def test_the_wheel_table_matches_the_published_wheels(system, machine, libc, python, expected):
    assert sherpa_engine._has_prebuilt_wheel(system, machine, libc, python) is expected


def test_a_missing_package_with_no_wheel_says_so(monkeypatch):
    monkeypatch.setattr(sherpa_engine.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(sherpa_engine, "_has_prebuilt_wheel", lambda: False)
    result = sherpa.availability()
    assert result.code == sherpa.CODE_NO_WHEEL
    assert "C++" in result.detail


def test_a_python_without_bz2_is_refused_only_while_the_model_is_absent(monkeypatch):
    monkeypatch.setattr(sherpa_engine, "probe", lambda: Availability(True))
    monkeypatch.setattr(sherpa_models, "bz2_available", lambda: False)
    cfg = SimpleNamespace(sherpa_model="")
    monkeypatch.setattr(sherpa_models, "is_present", lambda model: False)
    result = sherpa.availability(cfg)
    assert result.code == sherpa.CODE_BZ2_MISSING
    assert "bz2" in result.detail
    monkeypatch.setattr(sherpa_models, "is_present", lambda model: True)
    assert sherpa.availability(cfg).ok


@pytest.mark.asyncio
async def test_the_store_refuses_a_download_it_could_not_unpack(home, tmp_path, monkeypatch):
    monkeypatch.delenv(sherpa_models.SKIP_DOWNLOAD_ENV, raising=False)
    monkeypatch.setattr(sherpa_models, "bz2_available", lambda: False)
    model, _ = _fake_model(tmp_path, _MEMBERS)
    store = sherpa_models.SherpaModelStore()
    assert await store.ensure(model) is None
    assert store.status["step"] == "failed"
    assert store.status["error"] == sherpa_models.BZ2_MISSING_ERROR


def test_this_python_has_bz2():
    assert sherpa_models.bz2_available() is True


def test_tidy_turns_upper_case_words_into_a_sentence():
    assert sherpa.tidy("  RỒI CŨNG   HỖ TRỢ ") == "Rồi cũng hỗ trợ"
    assert sherpa.tidy("ĐƯỢC") == "Được"
    assert sherpa.tidy("") == ""


def test_tidy_leaves_cased_and_caseless_text_alone():
    cased = "After early nightfall, the lamps lit up."
    assert sherpa.tidy(cased) == cased
    assert sherpa.tidy("气象庁は  雪や") == "气象庁は 雪や"
    # Mixed scripts with all-caps Latin, as the zh-en model writes them.
    assert sherpa.tidy("SPEND ON 在什么上面") == "Spend on 在什么上面"


# ── catalog, extraction and the per-load check ──


def _fake_model(
    tmp_path: Path, members: dict[str, bytes]
) -> tuple[sherpa_models.SherpaModel, Path]:
    """A catalog row pinned to *members*, and a tar.bz2 holding them plus a stray file."""
    prefix = "pkg"
    archive = tmp_path / "model.tar.bz2"
    with tarfile.open(archive, "w:bz2") as tar:
        for name, data in {**members, "test_wavs/0.wav": b"RIFF", "README.md": b"hi"}.items():
            info = tarfile.TarInfo(f"{prefix}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

    def pin(name: str) -> sherpa_models.SherpaFile:
        data = members[name]
        return sherpa_models.SherpaFile(
            f"{prefix}/{name}", len(data), hashlib.sha256(data).hexdigest()
        )

    blob = archive.read_bytes()
    model = sherpa_models.SherpaModel(
        name="fake",
        languages=("vi",),
        archive=archive.name,
        archive_size_bytes=len(blob),
        archive_sha256=hashlib.sha256(blob).hexdigest(),
        encoder=pin("encoder.onnx"),
        decoder=pin("decoder.onnx"),
        joiner=pin("joiner.onnx"),
        tokens=pin("tokens.txt"),
        license="CC-BY-NC-ND-4.0",
        source_url="https://example.invalid/model",
    )
    return model, archive


_MEMBERS = {
    "encoder.onnx": b"encoder-bytes" * 10,
    "decoder.onnx": b"decoder-bytes",
    "joiner.onnx": b"joiner-bytes",
    "tokens.txt": b"<blk> 0\n",
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


@pytest.fixture
def served_archive(monkeypatch):
    """Serve a local archive through the pinned-payload streamer, which keeps its checks."""

    def install(archive: Path) -> None:
        class _Response(io.BytesIO):
            def __enter__(self):
                return self

        def _open(url, **_kwargs):
            assert url.endswith("/" + archive.name)
            return _Response(archive.read_bytes())

        opener = SimpleNamespace(open=_open)
        monkeypatch.setattr(whisper_models.asset_downloader, "build_opener", lambda **_k: opener)

    return install


def test_only_the_four_pinned_members_are_installed(home, tmp_path, served_archive):
    model, archive = _fake_model(tmp_path, _MEMBERS)
    served_archive(archive)
    target = sherpa_models._download_blocking(model)
    assert sorted(p.name for p in target.iterdir()) == sorted(_MEMBERS)
    for name, data in _MEMBERS.items():
        assert (target / name).read_bytes() == data
    # Nothing staged is left behind, and the archive is gone.
    assert sorted(p.name for p in sherpa_models.models_dir().iterdir()) == ["fake"]
    assert sherpa_models.is_present(model)


def test_a_member_that_does_not_match_its_pin_installs_nothing(home, tmp_path, served_archive):
    model, _ = _fake_model(tmp_path, _MEMBERS)
    # Same sizes, one changed byte: the archive itself is pinned, so only the
    # member's own digest can refuse it.
    (tmp_path / "bad").mkdir()
    tampered = dict(_MEMBERS, **{"tokens.txt": b"<blk> 1\n"})
    bad, bad_archive = _fake_model(tmp_path / "bad", tampered)
    model = dataclasses.replace(
        model,
        archive_size_bytes=bad.archive_size_bytes,
        archive_sha256=bad.archive_sha256,
    )
    served_archive(bad_archive)
    with pytest.raises(whisper_models.ModelDownloadError, match="tokens.txt"):
        sherpa_models._download_blocking(model)
    assert list(sherpa_models.model_dir(model).iterdir()) == []
    assert not sherpa_models.is_present(model)


def test_an_archive_that_is_not_the_pinned_one_is_refused(home, tmp_path, served_archive):
    model, archive = _fake_model(tmp_path, _MEMBERS)
    model = dataclasses.replace(model, archive_sha256="0" * 64)
    served_archive(archive)
    with pytest.raises(whisper_models.ModelDownloadError, match="sha256"):
        sherpa_models._download_blocking(model)
    assert not sherpa_models.is_present(model)


def test_a_tampered_file_is_removed_at_load(home, tmp_path, served_archive):
    model, archive = _fake_model(tmp_path, _MEMBERS)
    served_archive(archive)
    sherpa_models._download_blocking(model)
    assert sherpa_models._verify_blocking(model) is True
    joiner = sherpa_models.file_path(model, model.joiner)
    joiner.write_bytes(b"JOINER-BYTES")
    assert sherpa_models._verify_blocking(model) is False
    assert not joiner.exists()


@pytest.mark.asyncio
async def test_the_store_downloads_once_and_reports_ready(
    home, tmp_path, served_archive, monkeypatch
):
    # The suite turns model downloads off; this one serves a local archive.
    monkeypatch.delenv(sherpa_models.SKIP_DOWNLOAD_ENV, raising=False)
    model, archive = _fake_model(tmp_path, _MEMBERS)
    served_archive(archive)
    store = sherpa_models.SherpaModelStore()
    path = await store.ensure(model)
    assert path == sherpa_models.model_dir(model)
    assert store.status["step"] == "ready"
    assert store.status["total_bytes"] == model.size_bytes


@pytest.mark.asyncio
async def test_the_store_honours_the_download_switch(home, tmp_path, monkeypatch):
    model, _ = _fake_model(tmp_path, _MEMBERS)
    monkeypatch.setenv(sherpa_models.SKIP_DOWNLOAD_ENV, "1")
    store = sherpa_models.SherpaModelStore()
    assert await store.ensure(model) is None
    assert store.status["step"] == "skipped"


def test_the_catalog_pins_four_files_per_row():
    for model in sherpa_models.CATALOG:
        assert len(model.files) == 4
        assert all(len(f.sha256) == 64 for f in (*model.files,))
        assert len(model.archive_sha256) == 64
        assert model.archive.startswith("sherpa-onnx-") and model.archive.endswith(".tar.bz2")
        assert model.languages and all(len(code) in (2, 3) for code in model.languages)
        assert model.source_url.startswith("https://")
    assert sherpa_models.DEFAULT_MODEL in sherpa_models.MODEL_NAMES
    assert sherpa_custom.CUSTOM_MODEL not in sherpa_models.MODEL_NAMES
    assert len(set(sherpa_models.MODEL_NAMES)) == len(sherpa_models.CATALOG)


def test_the_catalog_covers_more_than_vietnamese():
    languages = {code for model in sherpa_models.CATALOG for code in model.languages}
    assert {"vi", "en", "zh", "ja", "ko"} <= languages


# ── the owner's own archive ──


def _custom_archive(home: Path, members: dict[str, bytes], name: str = "mine.tar.bz2") -> str:
    """Write *members* as a k2-fsa-shaped archive into the custom folder; return its digest."""
    folder = sherpa_custom.custom_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    with tarfile.open(path, "w:bz2") as tar:
        for member, data in members.items():
            info = tarfile.TarInfo(f"pkg/{member}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return hashlib.sha256(path.read_bytes()).hexdigest()


_BOTH_PRECISIONS = {
    "encoder-epoch-99-avg-1.onnx": b"E32",
    "encoder-epoch-99-avg-1.int8.onnx": b"E8",
    "decoder-epoch-99-avg-1.onnx": b"D32",
    "decoder-epoch-99-avg-1.int8.onnx": b"D8",
    "joiner-epoch-99-avg-1.onnx": b"J32",
    "joiner-epoch-99-avg-1.int8.onnx": b"J8",
    "tokens.txt": b"<blk> 0\n",
    "test_wavs/0.wav": b"RIFF",
}


def test_a_custom_archive_name_must_be_a_plain_tar_bz2_file_name():
    assert sherpa_custom.valid_archive_name(" model.tar.bz2 ") == "model.tar.bz2"
    for bad in ("../x.tar.bz2", "a/b.tar.bz2", ".hidden.tar.bz2", "x.zip", "x..tar.bz2", 3, ""):
        assert sherpa_custom.valid_archive_name(bad) == ""


def test_the_custom_pick_takes_int8_encoder_and_joiner_and_fp32_decoder():
    picked = sherpa_custom.pick_members([f"pkg/{n}" for n in _BOTH_PRECISIONS])
    assert picked == {
        "encoder": "pkg/encoder-epoch-99-avg-1.int8.onnx",
        "decoder": "pkg/decoder-epoch-99-avg-1.onnx",
        "joiner": "pkg/joiner-epoch-99-avg-1.int8.onnx",
        "tokens": "pkg/tokens.txt",
    }


def test_a_ctc_archive_is_refused_as_unsupported():
    with pytest.raises(sherpa_custom.CustomModelError) as raised:
        sherpa_custom.pick_members(["pkg/model.int8.onnx", "pkg/tokens.txt"])
    assert raised.value.code == sherpa_custom.CODE_CUSTOM_UNSUPPORTED


def test_a_custom_archive_is_unpacked_once_and_checked_after(home):
    digest = _custom_archive(home, _BOTH_PRECISIONS)
    model = sherpa_custom.prepare_blocking("mine.tar.bz2", digest)
    folder = sherpa_models.model_dir(model)
    assert folder == sherpa_custom.custom_dir() / digest
    assert sorted(p.name for p in folder.iterdir()) == [
        "decoder.onnx",
        "encoder.onnx",
        "joiner.onnx",
        "manifest.json",
        "tokens.txt",
    ]
    assert (folder / "encoder.onnx").read_bytes() == b"E8"
    assert (folder / "decoder.onnx").read_bytes() == b"D32"
    assert sherpa_custom.is_unpacked(digest)
    # A file changed on disk is caught by the manifest and unpacked again.
    (folder / "joiner.onnx").write_bytes(b"J9")
    sherpa_custom.prepare_blocking("mine.tar.bz2", digest)
    assert (folder / "joiner.onnx").read_bytes() == b"J8"
    # The owner's archive is left where it is.
    assert (sherpa_custom.custom_dir() / "mine.tar.bz2").is_file()


def test_a_custom_archive_that_does_not_match_its_digest_is_refused(home):
    _custom_archive(home, _BOTH_PRECISIONS)
    with pytest.raises(sherpa_custom.CustomModelError) as raised:
        sherpa_custom.prepare_blocking("mine.tar.bz2", "0" * 64)
    assert raised.value.code == sherpa_custom.CODE_CUSTOM_MISMATCH
    assert not any(p.is_dir() for p in sherpa_custom.custom_dir().iterdir())


def test_a_missing_custom_archive_is_reported(home, monkeypatch):
    with pytest.raises(sherpa_custom.CustomModelError) as raised:
        sherpa_custom.prepare_blocking("absent.tar.bz2", "a" * 64)
    assert raised.value.code == sherpa_custom.CODE_CUSTOM_MISSING
    monkeypatch.setattr(sherpa_engine, "probe", lambda: Availability(True))
    cfg = SimpleNamespace(
        sherpa_model="custom", sherpa_custom_archive="absent.tar.bz2", sherpa_custom_sha256="a" * 64
    )
    assert sherpa.availability(cfg).code == sherpa.CODE_CUSTOM_MISSING


def test_custom_needs_both_fields_in_config():
    assert (
        sections_stt._validated_stt_sherpa_model(
            "custom", custom_archive="m.tar.bz2", custom_sha256="b" * 64
        )
        == "custom"
    )
    assert (
        sections_stt._validated_stt_sherpa_model("custom", custom_archive="m.tar.bz2")
        == sherpa_models.DEFAULT_MODEL
    )
    assert sections_stt._validated_stt_sherpa_custom_archive("../m.tar.bz2") == ""


# ── utterances and the live session ──


def _tone(secs: float) -> np.ndarray:
    n = int(secs * SAMPLE_RATE_HZ)
    t = np.arange(n, dtype=np.float32) / SAMPLE_RATE_HZ
    return (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def _room(secs: float) -> np.ndarray:
    """A quiet hum, about -63 dBFS: the floor a real microphone gives the endpointer.

    Every speech pattern below opens with it. The endpointer learns its noise floor
    from the first frames it hears, so with digital silence first the opening tone
    would become the floor instead of speech.
    """
    n = int(secs * SAMPLE_RATE_HZ)
    t = np.arange(n, dtype=np.float32) / SAMPLE_RATE_HZ
    return (0.001 * np.sin(2 * np.pi * 50 * t)).astype(np.float32)


def _quiet(secs: float) -> np.ndarray:
    """Digital silence, as a muted input delivers it."""
    return np.zeros(int(secs * SAMPLE_RATE_HZ), dtype=np.float32)


def _int16(pcm: np.ndarray) -> bytes:
    return (np.clip(pcm, -1, 1) * 32767).astype("<i2").tobytes()


def test_split_utterances_keeps_every_sample():
    pcm = np.concatenate([_room(0.5), _tone(1.0), _room(1.2), _tone(0.8), _room(1.2)])
    pieces = sherpa.split_utterances(pcm, DEFAULT_SILENCE_MS)
    # The two tones land in different pieces; the hum after the last end may be one more.
    assert [bool(np.abs(p).max() > 0.1) for p in pieces][:2] == [True, True]
    assert sum(p.size for p in pieces) == pcm.size


class _FakeEngine:
    """Hears the tone as "XIN CHÀO" and the hum as nothing, as a transducer does."""

    def __init__(self, *, fail_final: bool = False) -> None:
        self.decodes: list[int] = []
        self.fail_final = fail_final
        self.released = False

    async def ensure_loaded(self, model_name, threads=0, **custom):
        return Availability(True)

    async def decode(self, pcm):
        self.decodes.append(pcm.size)
        if self.fail_final:
            raise sherpa_engine.DecodeFailed("boom")
        return "XIN CHÀO" if float(np.abs(pcm).max()) > 0.1 else ""

    def release_soon(self):
        self.released = True


def _session(monkeypatch, engine, **overrides):
    monkeypatch.setattr(sherpa, "_engine_for", lambda cfg: engine)
    settings = {
        "sherpa_model": "",
        "threads": 0,
        "silence_ms": DEFAULT_SILENCE_MS,
        "partial_interval_ms": MIN_PARTIAL_INTERVAL_MS,
        "streaming": True,
        "idle_evict_secs": DEFAULT_IDLE_EVICT_SECS,
        "timeout_secs": DEFAULT_TIMEOUT_SECS,
    }
    settings.update(overrides)
    return sherpa.StreamingSession(SimpleNamespace(**settings))


async def _run(session, chunks: list[bytes]) -> list[dict]:
    events: list[dict] = []

    async def collect():
        async for event in session.events():
            events.append(event)

    collector = asyncio.create_task(collect())
    for chunk in chunks:
        assert await session.feed(chunk)
    await session.finish()
    # finish() ends the event stream, so this completes without waiting on a clock.
    await asyncio.wait_for(collector, _EVENTS_BOUND_SECS)
    await session.close()
    return events


def _chunks(pcm: np.ndarray, ms: int = 100) -> list[bytes]:
    raw = _int16(pcm)
    step = SAMPLE_RATE_HZ * ms // 1000 * 2
    return [raw[i : i + step] for i in range(0, len(raw), step)]


@pytest.mark.asyncio
async def test_each_utterance_gets_one_final_and_the_tail_is_flushed(monkeypatch):
    engine = _FakeEngine()
    session = _session(monkeypatch, engine)
    assert await session.start() is None
    pcm = np.concatenate([_room(0.5), _tone(1.0), _room(1.2), _tone(0.8)])
    events = await _run(session, _chunks(pcm))
    finals = [e for e in events if e["type"] == "final"]
    assert [e["text"] for e in finals] == ["Xin chào", "Xin chào"]
    assert all(e["type"] in ("partial", "final") for e in events)
    assert engine.released


@pytest.mark.asyncio
async def test_silence_alone_decodes_nothing(monkeypatch):
    engine = _FakeEngine()
    session = _session(monkeypatch, engine)
    assert await session.start() is None
    events = await _run(session, _chunks(_quiet(2.0)))
    assert events == []
    assert engine.decodes == []


@pytest.mark.asyncio
async def test_no_partials_when_streaming_is_off(monkeypatch):
    engine = _FakeEngine()
    session = _session(monkeypatch, engine, **{"streaming": False})
    assert await session.start() is None
    events = await _run(session, _chunks(np.concatenate([_room(0.5), _tone(1.0), _room(1.2)])))
    assert [e["type"] for e in events] == ["final"]


@pytest.mark.asyncio
async def test_a_failed_final_ends_the_session_with_its_code(monkeypatch):
    engine = _FakeEngine(fail_final=True)
    session = _session(monkeypatch, engine, **{"streaming": False})
    assert await session.start() is None
    events = await _run(session, _chunks(np.concatenate([_room(0.5), _tone(1.0), _room(1.2)])))
    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == sherpa.CODE_FAILED
    assert session.failure_code == sherpa.CODE_FAILED


@pytest.mark.asyncio
async def test_a_start_that_cannot_load_reports_the_reason(monkeypatch):
    engine = _FakeEngine()

    async def missing(model_name, threads=0, **custom):
        return Availability(False, sherpa.CODE_MODEL_MISSING, "model zipformer is not downloaded")

    engine.ensure_loaded = missing
    session = _session(monkeypatch, engine)
    assert await session.start() == "model zipformer is not downloaded"
    assert session.failure_code == sherpa.CODE_MODEL_MISSING


@pytest.mark.asyncio
async def test_a_recording_is_decoded_one_utterance_at_a_time(monkeypatch):
    engine = _FakeEngine()
    monkeypatch.setattr(sherpa, "_engine_for", lambda cfg: engine)
    cfg = SimpleNamespace(sherpa_model="", threads=0, silence_ms=DEFAULT_SILENCE_MS)
    pcm = np.concatenate([_room(0.5), _tone(1.0), _room(1.2), _tone(0.8), _room(0.5)])
    text = await sherpa.transcribe_pcm(pcm, cfg)
    assert text == "Xin chào xin chào"
    assert len(engine.decodes) == 2
    assert await sherpa.transcribe_pcm(_quiet(1.0), cfg) == ""


def test_short_audio_is_padded_before_the_encoder():
    padded = sherpa_engine._padded(np.ones(10, dtype=np.float32))
    assert padded.size == sherpa_engine.MIN_DECODE_SAMPLES
    assert padded[:10].tolist() == [1.0] * 10
