"""The model catalog, its pinned downloader, and the resident engine's lifecycle.

No test here loads a real recogniser or touches the network. The engine's one
seam onto the native library is ``WhisperEngine._build_model``, so a fake stands
in there; the downloader's one seam is ``urllib.request.urlopen``. Both are
patched per test, which is also what keeps a native context from outliving a test
on an xdist worker.

The download section (the pinned transfer and the model store's verified load) is in
``test_stt_engine_download.py`` and the engine-lifecycle section (load, decode, prewarm,
eviction, the usage stamp) in ``test_stt_engine_lifecycle.py``. The encoder window, the
shared instance and its bounds, the staging paths and the race cases (cancellation,
timeouts, a late load, eviction or a model swap during a decode, a download kept off the
decode lock) stay here; the doubles the three files share live in
``stt_engine_helpers.py``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import gc
import hashlib
import os
import threading
import time
import weakref

import numpy as np
import pytest
from stt_engine_helpers import (  # noqa: F401 -- the fixture is requested by name
    _FakeBinding,
    _FakeModel,
    _make_native,
    _pin,
    _stub_urlopen,
    fake_engine_fixture,
)

from conftest import requires_symlinks
from kiro_crew.stt import engine as engine_mod
from kiro_crew.stt import limits as stt_limits
from kiro_crew.stt import models
from kiro_crew.testing.wait import async_wait_until, await_until

# ── Catalog ──


def test_every_catalog_entry_has_a_full_sha256_and_a_positive_size():
    """The pin is the trust anchor for the download, so a blank one is a hole."""
    assert models.CATALOG
    for model in models.CATALOG:
        assert len(model.sha256) == 64, model.name
        assert set(model.sha256) <= set("0123456789abcdef"), model.name
        assert model.size_bytes > 0, model.name
        assert model.filename == f"ggml-{model.name}.bin"


def test_catalog_names_are_unique():
    names = [m.name for m in models.CATALOG]
    assert len(names) == len(set(names))


def test_default_model_is_in_the_catalog():
    assert models.DEFAULT_MODEL in {m.name for m in models.CATALOG}


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("turbo", "large-v3-turbo"),
        ("large", "large-v3-turbo"),
        ("large-v1", "large-v3-turbo"),
        ("large-v2", "large-v3-turbo"),
        ("large-v3", "large-v3-turbo"),
        ("large-v3-turbo-q5_0", "large-v3-turbo"),
        ("medium", "large-v3-turbo"),
        ("medium.en", "large-v3-turbo"),
        ("small.en", "small"),
        ("base.en", "base"),
        ("tiny.en", "tiny"),
    ],
)
def test_a_superseded_model_name_keeps_what_the_user_asked_for(stored, expected):
    """Falling back to the default would silently downgrade a deliberate choice."""
    assert models.resolve(stored).name == expected


def test_no_superseded_name_falls_back_to_the_default_by_accident():
    """A name in the alias table must never land on the default unless it means it."""
    for stored, target in models._ALIASES.items():
        assert target in {m.name for m in models.CATALOG}, stored


def test_resolve_falls_back_to_the_default_with_a_warning(caplog):
    """The value comes from config.json, so it degrades instead of raising."""
    with caplog.at_level("WARNING"):
        assert models.resolve("no-such-model").name == models.DEFAULT_MODEL
    assert "no-such-model" in caplog.text


def test_resolve_is_exact_for_every_catalog_name():
    for model in models.CATALOG:
        assert models.resolve(model.name) is model


# ── Presence ──


def test_is_present_requires_the_exact_pinned_size(monkeypatch, tmp_path):
    """A truncated download must read as absent so the next attempt replaces it."""
    monkeypatch.setattr(models, "models_dir", lambda: tmp_path)
    model = models.resolve("base")
    assert not models.is_present(model)
    path = tmp_path / model.filename
    path.write_bytes(b"x" * 10)
    assert not models.is_present(model)
    path.write_bytes(b"x" * model.size_bytes)
    assert models.is_present(model)


def test_models_dir_is_under_the_data_home(monkeypatch, tmp_path):
    monkeypatch.setattr(models, "config_dir", lambda: tmp_path)
    assert models.models_dir() == tmp_path / "models" / "whisper"


# ── Availability probing ──


def _pretend_absent(monkeypatch):
    """Make the recogniser look NOT INSTALLED, which is what the finder answers.

    Absence has to be simulated at `find_spec` rather than by making the import
    raise. Those are different states and the probe now reports them differently:
    an installed-but-unloadable package (missing system library, too-old glibc,
    numpy ABI mismatch) must not be reported as "install the voice extra" to a user
    who already had it installed.
    """
    monkeypatch.setattr(engine_mod.importlib.util, "find_spec", lambda name: None, raising=True)


def test_probe_reports_a_missing_extra_with_its_own_code(monkeypatch):
    _pretend_absent(monkeypatch)
    monkeypatch.setattr(engine_mod, "_has_prebuilt_wheel", lambda: True)
    result = engine_mod.probe()
    assert not result.ok
    assert result.code == engine_mod.CODE_EXTRA_MISSING
    # The recogniser distribution, since `pip install kirocrew[voice]` resolves
    # nowhere: this project is on no index.
    assert "pywhispercpp" in result.detail
    assert "kirocrew[" not in result.detail


def test_probe_distinguishes_a_platform_with_no_wheel(monkeypatch):
    """A platform with no wheel needs a toolchain, not a pip install."""
    _pretend_absent(monkeypatch)
    monkeypatch.setattr(engine_mod, "_has_prebuilt_wheel", lambda: False)
    result = engine_mod.probe()
    assert result.code == engine_mod.CODE_NO_WHEEL
    # The platform is NAMED, because "reinstall the extra" is unactionable advice
    # for someone whose install is the thing that found no wheel.
    assert engine_mod.platform.system() in result.detail


def test_probe_reports_an_installed_but_unloadable_recogniser_separately(monkeypatch):
    """The state the old `except ImportError` mislabelled as a missing extra.

    An installed package whose import raises has a real loader message, and that
    message is the useful part: it names the library that is missing or the ABI
    that did not match. Reporting it as "install the voice extra" sent the reader
    back to an install that had already succeeded.
    """
    monkeypatch.setattr(engine_mod.importlib.util, "find_spec", lambda name: object(), raising=True)
    import builtins

    real_import = builtins.__import__

    def _boom(name, *args, **kwargs):
        if name.startswith("pywhispercpp"):
            raise ImportError("libgomp.so.1: cannot open shared object file")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _boom)
    result = engine_mod.probe()
    assert result.code == engine_mod.CODE_IMPORT_FAILED
    assert "libgomp" in result.detail


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        # Published: macOS arm64 only, Linux x86_64/aarch64, Windows x86/x64.
        ("Darwin", "arm64", True),
        ("Darwin", "x86_64", False),
        ("Darwin", "i386", False),
        ("Linux", "x86_64", True),
        ("Linux", "aarch64", True),
        ("Linux", "arm64", True),
        ("Windows", "AMD64", True),
        ("Windows", "x86", True),
        # Every row below returned True before, i.e. the probe told a user with no
        # possible wheel to reinstall the extra that had just failed to find one.
        ("Windows", "ARM64", False),
        ("Linux", "armv7l", False),
        ("Linux", "i686", False),
        ("Linux", "ppc64le", False),
        ("Linux", "s390x", False),
        ("FreeBSD", "amd64", False),
        ("SunOS", "sun4v", False),
    ],
)
def test_wheel_coverage_matches_what_is_published(monkeypatch, system, machine, expected):
    monkeypatch.setattr(engine_mod.platform, "system", lambda: system)
    monkeypatch.setattr(engine_mod.platform, "machine", lambda: machine)
    assert engine_mod._has_prebuilt_wheel() is expected


def test_the_wheel_table_is_case_insensitive_on_the_machine_name():
    """`platform.machine()` capitalises differently per OS: Windows says AMD64."""
    for arch_set in engine_mod._WHEEL_ARCHS.values():
        assert all(a == a.lower() for a in arch_set), "table entries must be lower-cased"


def test_availability_codes_are_distinct():
    """They are a wire contract the dashboard keys its messages off."""
    codes = [
        engine_mod.CODE_EXTRA_MISSING,
        engine_mod.CODE_NO_WHEEL,
        engine_mod.CODE_IMPORT_FAILED,
        engine_mod.CODE_MODEL_MISSING,
    ]
    assert len(set(codes)) == len(codes)
    assert engine_mod.CODE_OK == ""
    assert all(codes)


# ── PCM conversion ──


def test_int16_pcm_is_scaled_into_the_unit_range():
    raw = np.array([0, 16384, -16384, 32767, -32768], dtype="<i2").tobytes()
    out = engine_mod.pcm_from_int16(raw)
    assert out.dtype == np.float32
    assert out[0] == 0.0
    assert out[1] == pytest.approx(0.5)
    assert out[2] == pytest.approx(-0.5)
    assert -1.0 <= out.min() and out.max() < 1.0


def test_an_odd_trailing_byte_is_dropped_not_fatal():
    """A socket read can split a sample across two frames."""
    raw = np.array([1000, 2000], dtype="<i2").tobytes() + b"\x07"
    assert engine_mod.pcm_from_int16(raw).size == 2


def test_empty_pcm_is_an_empty_array():
    assert engine_mod.pcm_from_int16(b"").size == 0
    assert engine_mod.pcm_from_int16(b"\x01").size == 0


# ── Thread sizing ──


def test_available_cpus_reads_the_affinity_helper(monkeypatch):
    """The platform read lives in ``cpu_affinity``; an unknown count reads as one."""
    monkeypatch.setattr(engine_mod, "affinity_cpu_count", lambda: 6)
    assert engine_mod.available_cpus() == 6
    monkeypatch.setattr(engine_mod, "affinity_cpu_count", lambda: None)
    assert engine_mod.available_cpus() == 1


def test_thread_count_is_bounded_and_positive(monkeypatch):
    # An inherited override would outrank the auto sizing this test pins.
    monkeypatch.delenv(stt_limits.THREADS_ENV, raising=False)
    monkeypatch.setattr(engine_mod, "available_cpus", lambda: 256)
    assert engine_mod.thread_count() == stt_limits.THREAD_CEILING
    monkeypatch.setattr(engine_mod, "available_cpus", lambda: 1)
    assert engine_mod.thread_count() == 1
    monkeypatch.setattr(engine_mod, "available_cpus", lambda: 16)
    assert engine_mod.thread_count() == 8


# ── Encoder window ──


class _WindowBinding(_FakeBinding):
    """Records the encoder window each native decode ran with."""

    def __init__(self, raises: Exception | None = None, **kw) -> None:
        super().__init__(**kw)
        self.windows: list[int] = []
        self._raises = raises

    def whisper_full(self, _ctx, params, _pcm, _n) -> int:
        self.windows.append(params.audio_ctx)
        if self._raises is not None:
            raise self._raises
        return super().whisper_full(_ctx, params, _pcm, _n)


def _native_with_window(monkeypatch, fake: _FakeModel, binding: _FakeBinding) -> None:
    monkeypatch.setattr(engine_mod, "_whisper_binding", lambda: binding)
    _make_native(monkeypatch, fake)
    fake._params = type("Params", (), {"audio_ctx": 0})()


def _secs(n: float) -> int:
    return int(n * engine_mod.SAMPLE_RATE_HZ)


@pytest.mark.parametrize(
    "secs, frames",
    [
        (0.0, engine_mod.PREVIEW_AUDIO_CTX_FLOOR),
        (1.0, engine_mod.PREVIEW_AUDIO_CTX_FLOOR),
        # The longest phrase a partial decodes still fits the floor with its margin.
        (8.0, engine_mod.PREVIEW_AUDIO_CTX_FLOOR),
        (9.0, 576),
        (27.0, 1472),
        # Within a step of the full window: sizing would save nothing.
        (28.0, 0),
        (30.0, 0),
    ],
)
def test_a_preview_window_has_a_floor_a_margin_and_yields_to_the_full_one(secs, frames):
    assert engine_mod.preview_audio_ctx(_secs(secs)) == frames


@pytest.mark.parametrize("secs", [0.5, 1.0, 2.5, 8.0, 20.0, 27.9])
def test_a_preview_window_always_holds_the_whole_clip(secs):
    """A window shorter than the audio truncates it, which is how a decode loops."""
    frames = engine_mod.preview_audio_ctx(_secs(secs)) or engine_mod.FULL_AUDIO_CTX
    assert frames >= secs * 50


@pytest.mark.asyncio
async def test_a_sized_decode_narrows_the_window_for_that_call_only(fake_engine, monkeypatch):
    """The params object is shared, so the final after a partial must get it back."""
    eng, fake = fake_engine
    await eng.ensure_loaded("base", "en")
    binding = _WindowBinding()
    _native_with_window(monkeypatch, fake, binding)
    audio = np.zeros(_secs(2.0), dtype=np.float32)

    await eng.decode(audio, superseding=True, sized_window=True)
    await eng.decode(audio)

    assert binding.windows == [engine_mod.PREVIEW_AUDIO_CTX_FLOOR, 0]
    assert fake._params.audio_ctx == 0


@pytest.mark.asyncio
async def test_the_window_is_restored_when_the_native_call_raises(fake_engine, monkeypatch):
    eng, fake = fake_engine
    await eng.ensure_loaded("base", "en")
    _native_with_window(monkeypatch, fake, _WindowBinding(raises=RuntimeError("boom")))

    with pytest.raises(engine_mod.DecodeFailed):
        await eng.decode(np.zeros(_secs(1.0), dtype=np.float32), sized_window=True)
    assert fake._params.audio_ctx == 0


@pytest.mark.asyncio
async def test_the_fallback_path_decodes_at_the_full_window(fake_engine, monkeypatch):
    """``Model.transcribe`` keeps any parameter it is given, so none is passed."""
    eng, fake = fake_engine
    await eng.ensure_loaded("base", "en")
    seen: list[dict] = []
    original = fake.transcribe

    def transcribe(pcm, abort_callback=None, **kw):
        seen.append(kw)
        return original(pcm, abort_callback=abort_callback, **kw)

    monkeypatch.setattr(fake, "transcribe", transcribe)
    text = await eng.decode(np.zeros(_secs(1.0), dtype=np.float32), sized_window=True)
    assert text == "hello world"
    assert seen == [{}]


@pytest.mark.asyncio
async def test_a_binding_without_a_window_parameter_still_decodes_previews(
    fake_engine, monkeypatch
):
    """Params with no ``audio_ctx`` decode at the full window instead of failing."""
    eng, fake = fake_engine
    await eng.ensure_loaded("base", "en")
    monkeypatch.setattr(engine_mod, "_whisper_binding", lambda: _FakeBinding())
    _make_native(monkeypatch, fake)
    assert not hasattr(fake._params, "audio_ctx")

    text = await eng.decode(
        np.zeros(_secs(1.0), dtype=np.float32), superseding=True, sized_window=True
    )

    assert text == "hello world"


@pytest.mark.asyncio
async def test_slow_sized_decodes_in_a_row_restore_the_full_window(fake_engine, monkeypatch):
    eng, fake = fake_engine
    await eng.ensure_loaded("base", "en")
    binding = _WindowBinding()
    _native_with_window(monkeypatch, fake, binding)
    audio = np.zeros(_secs(1.0), dtype=np.float32)
    await eng.decode(audio)  # binds the window state to this model

    eng._note_window_cost(0, 400.0)
    for _ in range(engine_mod.PREVIEW_WINDOW_MAX_STRIKES):
        eng._note_window_cost(engine_mod.PREVIEW_AUDIO_CTX_FLOOR, 900.0)
    await eng.decode(audio, superseding=True, sized_window=True)

    assert binding.windows[-1] == 0


def test_one_fast_sized_decode_clears_the_strikes():
    """The first sized decode after a final re-reserves the graph and is slow once."""
    eng = engine_mod.WhisperEngine()
    eng._note_window_cost(0, 400.0)
    for _ in range(engine_mod.PREVIEW_WINDOW_MAX_STRIKES - 1):
        eng._note_window_cost(engine_mod.PREVIEW_AUDIO_CTX_FLOOR, 900.0)
    eng._note_window_cost(engine_mod.PREVIEW_AUDIO_CTX_FLOOR, 150.0)
    eng._note_window_cost(engine_mod.PREVIEW_AUDIO_CTX_FLOOR, 900.0)
    assert not eng._sized_window_off


def test_a_busy_host_does_not_turn_sizing_off():
    """Previews somewhat slower than a quick prewarm are load, not a looping decode.

    The prewarm decodes a second of silence, so it is a fast reference, and a host
    busy with other work slows every preview a little. A strict comparison against it
    turned sizing off for good in a live meeting.
    """
    eng = engine_mod.WhisperEngine()
    eng._note_window_cost(0, 387.0)
    for wall_ms in (300.0, 420.0, 450.0, 390.0, 440.0, 430.0, 410.0) * 3:
        eng._note_window_cost(engine_mod.PREVIEW_AUDIO_CTX_FLOOR, wall_ms)
    assert not eng._sized_window_off
    assert eng._sized_window_strikes == 0


def test_exactly_the_slowdown_threshold_is_not_a_strike():
    eng = engine_mod.WhisperEngine()
    eng._note_window_cost(0, 400.0)
    for _ in range(engine_mod.PREVIEW_WINDOW_MAX_STRIKES):
        eng._note_window_cost(
            engine_mod.PREVIEW_AUDIO_CTX_FLOOR, 400.0 * engine_mod.PREVIEW_WINDOW_SLOWDOWN
        )
    assert not eng._sized_window_off


def test_a_sized_decode_with_no_full_reference_counts_no_strike():
    eng = engine_mod.WhisperEngine()
    for _ in range(engine_mod.PREVIEW_WINDOW_MAX_STRIKES + 1):
        eng._note_window_cost(engine_mod.PREVIEW_AUDIO_CTX_FLOOR, 900.0)
    assert not eng._sized_window_off


@pytest.mark.asyncio
async def test_an_aborted_decode_sets_no_full_window_reference(fake_engine, monkeypatch):
    """An abort unwinds early; its short time would make every sized decode look slow."""
    eng, fake = fake_engine
    await eng.ensure_loaded("base", "en")
    stop = threading.Event()
    _native_with_window(monkeypatch, fake, _WindowBinding(status=-6, on_full=stop.set))

    assert (
        await eng.decode(
            np.zeros(_secs(1.0), dtype=np.float32), superseding=True, abort_if=stop.is_set
        )
        == ""
    )
    assert eng._full_window_ms is None


@pytest.mark.asyncio
async def test_a_reloaded_model_gets_a_fresh_window_verdict(fake_engine, monkeypatch):
    eng, _fake = fake_engine
    built: list[_FakeModel] = []
    binding = _WindowBinding()

    def build(_key):
        model = _FakeModel()
        _native_with_window(monkeypatch, model, binding)
        built.append(model)
        return model

    monkeypatch.setattr(engine_mod.WhisperEngine, "_build_model", staticmethod(build))
    audio = np.zeros(_secs(1.0), dtype=np.float32)
    await eng.ensure_loaded("base", "en")
    await eng.decode(audio)
    eng._note_window_cost(0, 400.0)
    for _ in range(engine_mod.PREVIEW_WINDOW_MAX_STRIKES):
        eng._note_window_cost(engine_mod.PREVIEW_AUDIO_CTX_FLOOR, 900.0)
    assert eng._sized_window_off

    await eng.close()
    await eng.ensure_loaded("base", "en")
    await eng.decode(audio, superseding=True, sized_window=True)

    assert len(built) == 2
    assert binding.windows[-1] == engine_mod.PREVIEW_AUDIO_CTX_FLOOR


class _LingeringPool(concurrent.futures.Executor):
    """A pool whose worker keeps each finished work item until the test lets go.

    ``ThreadPoolExecutor`` delivers a result inside ``_WorkItem.run`` and drops the
    item -- callable, arguments and the future holding the result -- only after
    ``run`` returns, so a worker descheduled between the two still references all
    three while the event loop is already running the caller's next line. On a
    loaded CI runner that gap is where ``close()`` found a model still alive. This
    pool makes the gap last for the whole test: the engine passes only when nothing
    it hands the pool keeps the model, so the race is a fail rather than a flake.
    """

    def __init__(self) -> None:
        self._let_go = threading.Event()
        self._threads: list[threading.Thread] = []

    def submit(self, fn, /, *args, **kwargs):
        future: concurrent.futures.Future = concurrent.futures.Future()

        def run() -> None:
            future.set_running_or_notify_cancel()
            try:
                future.set_result(fn(*args, **kwargs))
            except BaseException as exc:
                future.set_exception(exc)
            # `fn`, `args` and `future` stay referenced by this frame until the test
            # releases them: exactly what the real worker holds before `del work_item`.
            self._let_go.wait()

        thread = threading.Thread(target=run, name="mc-stt-lingering", daemon=True)
        self._threads.append(thread)
        thread.start()
        return future

    def release(self) -> None:
        self._let_go.set()
        for thread in self._threads:
            thread.join(timeout=5.0)
            assert not thread.is_alive(), "a lingering pool worker did not let go"


@pytest.fixture
def lingering_pool(monkeypatch):
    """The engine's STT pool, replaced by a :class:`_LingeringPool` for one test."""
    pool = _LingeringPool()
    monkeypatch.setattr(engine_mod, "stt_executor", lambda: pool)
    yield pool
    pool.release()


def _track_built_models(monkeypatch) -> list[weakref.ReferenceType]:
    """Build a fresh fake per load and keep only WEAK references to them.

    The real binding frees its native context in ``Model.__del__``, so a model is
    released exactly when its last strong reference goes. Holding these weakly is
    what lets a test see that, rather than ``loaded`` merely reading False.
    """
    refs: list[weakref.ReferenceType] = []

    def build(_key):
        model = _FakeModel()
        refs.append(weakref.ref(model))
        return model

    monkeypatch.setattr(engine_mod.WhisperEngine, "_build_model", staticmethod(build))
    return refs


@pytest.mark.asyncio
async def test_close_frees_a_model_that_ran_a_sized_decode(
    fake_engine, monkeypatch, lingering_pool
):
    """Under a pool that never lets a finished work item go: the load's result and
    the decode's argument must both have been handed over, not kept."""
    eng, _fake = fake_engine
    refs = _track_built_models(monkeypatch)
    await eng.ensure_loaded("base", "en")
    await eng.decode(np.zeros(_secs(1.0), dtype=np.float32), superseding=True, sized_window=True)

    await eng.close()
    gc.collect()

    assert not eng.loaded
    assert refs[0]() is None, "the window verdict must not keep unloaded weights alive"


@pytest.mark.asyncio
async def test_idle_eviction_frees_a_model_that_ran_a_sized_decode(
    monkeypatch, tmp_path, lingering_pool
):
    monkeypatch.setattr(engine_mod, "_engine", None)
    monkeypatch.setattr(engine_mod, "probe", lambda: engine_mod.Availability(True))
    stub = tmp_path / "ggml-base.bin"
    stub.write_bytes(b"not a real model")

    async def _ensure(_model):
        return stub

    monkeypatch.setattr(models, "store", lambda: type("S", (), {"ensure": staticmethod(_ensure)})())
    refs = _track_built_models(monkeypatch)
    eng = engine_mod.WhisperEngine(idle_evict_secs=0)
    await eng.ensure_loaded("base", "en")
    await eng.decode(np.zeros(_secs(1.0), dtype=np.float32), superseding=True, sized_window=True)

    assert await eng.maybe_evict()
    gc.collect()

    assert refs[0]() is None


@pytest.mark.asyncio
async def test_a_model_switch_frees_the_old_weights_before_building_the_new(
    fake_engine, monkeypatch, lingering_pool
):
    """One model's worth of memory, not two, across a language or model change."""
    eng, _fake = fake_engine
    refs: list[weakref.ReferenceType] = []
    alive_at_build: list[bool] = []

    def build(_key):
        gc.collect()
        alive_at_build.append(any(ref() is not None for ref in refs))
        model = _FakeModel()
        refs.append(weakref.ref(model))
        return model

    monkeypatch.setattr(engine_mod.WhisperEngine, "_build_model", staticmethod(build))
    await eng.ensure_loaded("base", "en")
    await eng.decode(np.zeros(_secs(1.0), dtype=np.float32), superseding=True, sized_window=True)
    await eng.ensure_loaded("base", "fr")

    assert alive_at_build == [False, False]


# ── Engine lifecycle ──


@pytest.mark.asyncio
async def test_real_audio_supersedes_an_inflight_cold_prewarm(fake_engine, monkeypatch):
    eng, fake = fake_engine
    await eng.ensure_loaded("base", "en")
    loop = asyncio.get_running_loop()
    warming = asyncio.Event()
    release = threading.Event()
    order: list[str] = []
    original_transcribe = fake.transcribe

    def transcribe(pcm, abort_callback=None, **kwargs):
        if not order:
            order.append("warm")
            loop.call_soon_threadsafe(warming.set)
            # A real request must invalidate this throwaway decode without
            # waiting for its normal completion or cancelling its asyncio task.
            while not release.wait(timeout=0.01):
                if abort_callback():
                    order.append("warm aborted")
                    return []
            return []
        order.append("real audio")
        return original_transcribe(pcm, abort_callback=abort_callback, **kwargs)

    monkeypatch.setattr(fake, "transcribe", transcribe)
    prewarm = asyncio.create_task(eng.prewarm("base", "en"))
    try:
        await await_until(warming, what="the prewarm decode starting")
        transcript = await await_until(
            asyncio.ensure_future(eng.decode(np.ones(engine_mod.SAMPLE_RATE_HZ, dtype=np.float32))),
            what="the real decode",
        )
        assert transcript == "hello world"
        assert (await await_until(prewarm, what="the prewarm")).ok
        assert order == ["warm", "warm aborted", "real audio"]
    finally:
        release.set()
        prewarm.cancel()
        await asyncio.gather(prewarm, return_exceptions=True)


def test_shared_engine_is_a_process_singleton(monkeypatch):
    monkeypatch.setattr(engine_mod, "_engine", None)
    assert engine_mod.shared_engine() is engine_mod.shared_engine()


# ── Shared instance and its bounds ──


def test_a_zero_argument_shared_engine_does_not_reset_configured_bounds(monkeypatch):
    """`prewarm` and `close` ask for the engine without opinions about its config.

    The bounds must not default to the module constants, which would make every
    zero-argument caller a silent WRITER: one `stt.close()` or `stt.prewarm()` put
    an operator's configured idle window back to 600 s, so the setting appeared to
    work and then quietly reverted.
    """
    monkeypatch.setattr(engine_mod, "_engine", None)
    configured = engine_mod.shared_engine(idle_evict_secs=30, timeout_secs=45)
    assert configured._idle_evict_secs == 30
    assert configured._timeout_secs == 45

    same = engine_mod.shared_engine()
    assert same is configured
    assert same._idle_evict_secs == 30, "a bare shared_engine() overwrote the window"
    assert same._timeout_secs == 45


def test_a_configured_shared_engine_still_picks_up_a_live_config_change(monkeypatch):
    """The reason the write path exists at all: config is read live."""
    monkeypatch.setattr(engine_mod, "_engine", None)
    engine_mod.shared_engine(idle_evict_secs=30, timeout_secs=45)
    updated = engine_mod.shared_engine(idle_evict_secs=90, timeout_secs=120)
    assert updated._idle_evict_secs == 90
    assert updated._timeout_secs == 120


@pytest.mark.asyncio
async def test_the_idle_sweep_releases_a_model_the_decode_paths_never_could(
    monkeypatch, fake_engine
):
    """Eviction needs something that runs while nothing else is running.

    `maybe_evict` is also called when a decode finishes, and that call can never
    fire: it runs microseconds after `_last_used` was stamped, so the elapsed time
    is always inside the window. Without this sweep the documented saving never
    happened and the weights stayed resident for the life of the gateway.
    """
    eng, _ = fake_engine
    monkeypatch.setattr(engine_mod, "_engine", eng)
    monkeypatch.setattr(engine_mod, "_IDLE_SWEEP_INTERVAL_SECS", 0.01)
    await eng.ensure_loaded("base", "en")
    assert eng.loaded
    eng._idle_evict_secs = 0  # "release as soon as it is idle"

    task = asyncio.create_task(engine_mod.idle_sweep_loop())
    try:
        await async_wait_until(
            lambda: not eng.loaded, describe=lambda: "the idle sweep never released the model"
        )
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert not eng.loaded, "the idle sweep never released the model"


@pytest.mark.asyncio
async def test_the_idle_sweep_does_not_create_an_engine_on_a_voiceless_host(monkeypatch):
    """A host that never dictates must not get an instance out of a janitor."""
    monkeypatch.setattr(engine_mod, "_engine", None)
    monkeypatch.setattr(engine_mod, "_IDLE_SWEEP_INTERVAL_SECS", 0.01)
    task = asyncio.create_task(engine_mod.idle_sweep_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert engine_mod._engine is None


@pytest.mark.parametrize(
    "link_type", [pytest.param("symlink", marks=requires_symlinks), "hardlink"]
)
def test_a_planted_link_at_a_predictable_staging_path_is_not_followed(
    monkeypatch, tmp_path, link_type
):
    """The models directory is agent-writable, so the staging path must be unguessable.

    A fixed ``.bin.part`` -- or one derived from a PID, which is trivially observable --
    let an agent pre-plant a symlink there and have the download truncate and overwrite
    whatever it pointed at. `mkstemp` opens with ``O_CREAT | O_EXCL``, so it cannot
    follow a symlink, and the name it picks is unpredictable.
    """
    payload = b"weights" * 100
    model = _pin(monkeypatch, tmp_path, payload)
    victim = tmp_path / "precious.txt"
    victim.write_text("do not overwrite me", encoding="utf-8")
    target = tmp_path / model.filename
    # Every staging path a reader of the old code could have predicted.
    for guess in (
        f"{model.filename}{models._STAGING_SUFFIX}",
        f"{model.filename}.{os.getpid()}{models._STAGING_SUFFIX}",
    ):
        planted = tmp_path / guess
        if link_type == "symlink":
            planted.symlink_to(victim)
        else:
            planted.hardlink_to(victim)

    monkeypatch.setattr(models.urllib.request, "urlopen", _stub_urlopen(payload))
    assert models._download_blocking(model) == target
    assert victim.read_text(encoding="utf-8") == "do not overwrite me"
    assert target.read_bytes() == payload


def test_each_staging_file_gets_its_own_name(monkeypatch, tmp_path):
    """Nothing serialises across processes, so two transfers must not share a file.

    A gateway, an MCP server and a `kirocrew` CLI each run their own store over one
    data home. With a shared staging name their writes interleaved and the sha256 pin
    then failed BOTH, so a model that downloaded fine twice completed zero times.
    """
    payload = b"weights" * 100
    model = _pin(monkeypatch, tmp_path, payload)
    seen: set[str] = set()

    class _Recorder:
        def read(self, _n):
            seen.update(p.name for p in tmp_path.glob(f"*{models._STAGING_SUFFIX}"))
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(models.urllib.request, "urlopen", lambda _u, timeout=None: _Recorder())
    for _ in range(3):
        with pytest.raises(models.ModelDownloadError):
            models._download_blocking(model)
    assert len(seen) == 3, f"staging names repeated across transfers: {seen}"
    assert not list(tmp_path.glob(f"*{models._STAGING_SUFFIX}")), "staging files must not survive"


@pytest.mark.asyncio
async def test_cancelling_a_decode_aborts_native_work_before_releasing_the_context(
    fake_engine, monkeypatch
):
    eng, fake = fake_engine
    await eng.ensure_loaded("base", "en")
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    cancellation_seen = asyncio.Event()
    release = threading.Event()
    done = threading.Event()

    def transcribe(pcm, abort_callback=None, **_kwargs):
        loop.call_soon_threadsafe(entered.set)
        try:
            while not release.wait(timeout=0.01):
                if abort_callback():
                    loop.call_soon_threadsafe(cancellation_seen.set)
                    release.wait(timeout=5)
                    return []
            return []
        finally:
            done.set()

    monkeypatch.setattr(fake, "transcribe", transcribe)
    task = asyncio.create_task(eng.decode(np.zeros(engine_mod.SAMPLE_RATE_HZ, dtype=np.float32)))
    try:
        await await_until(entered, what="the native call starting")
        task.cancel()
        await await_until(cancellation_seen, what="the worker seeing the abort")
        assert not task.done(), "the native context was released before its worker finished"
        assert eng._decode_lock.locked()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await await_until(task, what="the cancelled decode ending")
        assert done.is_set()
        assert eng.loaded
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.to_thread(done.wait, 5)


@pytest.mark.asyncio
async def test_a_cancelled_worker_cannot_trigger_a_second_model_allocation(
    fake_engine, monkeypatch
):
    eng, fake = fake_engine
    await eng.ensure_loaded("base", "en")
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    done = threading.Event()
    monkeypatch.setattr(engine_mod, "_ABORT_GRACE_SECS", 0)

    def transcribe(pcm, **_kwargs):
        loop.call_soon_threadsafe(entered.set)
        try:
            release.wait(timeout=5)
            return []
        finally:
            done.set()

    monkeypatch.setattr(fake, "transcribe", transcribe)
    task = asyncio.create_task(eng.decode(np.zeros(engine_mod.SAMPLE_RATE_HZ, dtype=np.float32)))
    try:
        await await_until(entered, what="the native call starting")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await await_until(task, what="the cancelled decode ending")
        assert not done.is_set(), "the fake must still be holding its native context"
        assert not eng.loaded
        refused = await await_until(
            asyncio.ensure_future(eng.ensure_loaded("base", "en")), what="the refused reload"
        )
        assert refused.code == engine_mod.CODE_DECODE_FAILED
        release.set()
        await await_until(asyncio.shield(eng._retired_decode), what="the retired decode finishing")
        assert (await eng.ensure_loaded("base", "en")).ok
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.to_thread(done.wait, 5)


@pytest.mark.asyncio
async def test_a_timed_out_decode_waits_for_the_native_call_before_releasing(monkeypatch, tmp_path):
    """A timeout must not return while the decode is still inside the context.

    `wait_for` cancels its wrapper and leaves the executor thread inside
    `whisper_full`, so returning there released the decode lock under a running
    native call and let the NEXT decode enter the same single-entry context — the
    exact corruption the lock exists to prevent.

    Asserted as "decode() has not returned yet", which is deterministic, rather than
    by racing two decodes and counting overlaps: the counter version passed against
    the buggy code, because whether the second call's thread had started by the time
    the assertion ran was a matter of scheduling.
    """
    monkeypatch.setattr(engine_mod, "_engine", None)
    monkeypatch.setattr(engine_mod, "probe", lambda: engine_mod.Availability(True))
    stub = tmp_path / "ggml-base.bin"
    stub.write_bytes(b"not a real model")

    async def _ensure(_model):
        return stub

    monkeypatch.setattr(models, "store", lambda: type("S", (), {"ensure": staticmethod(_ensure)})())

    entered = threading.Event()
    release = threading.Event()

    class _SlowModel:
        def transcribe(self, pcm, abort_callback=None, **_kw):
            entered.set()
            # Stands in for a decode that is still running when the caller's wait
            # expires, and honours the abort only afterwards.
            release.wait(timeout=10.0)
            return []

    monkeypatch.setattr(
        engine_mod.WhisperEngine, "_build_model", staticmethod(lambda key: _SlowModel())
    )
    eng = engine_mod.WhisperEngine()
    await eng.ensure_loaded("base", "en")
    eng._timeout_secs = 0  # the caller's wait expires immediately

    task = asyncio.create_task(eng.decode(np.zeros(engine_mod.SAMPLE_RATE_HZ, dtype=np.float32)))
    await async_wait_until(entered.is_set, describe=lambda: "the native call never started")
    await asyncio.sleep(0.2)
    assert not task.done(), "decode() returned while the native call was still running"

    release.set()
    with pytest.raises(engine_mod.DecodeFailed):
        # A timeout is a failure even once the abort lands cleanly: the audio was
        # heard and no transcript exists for it, so answering "" is what let a whole
        # utterance disappear behind an empty final.
        await task


@pytest.mark.asyncio
async def test_a_decode_that_ignores_its_abort_does_not_wedge_the_engine(monkeypatch, tmp_path):
    """The grace window is bounded: holding the lock forever is worse than the race.

    A native call that never honours the abort must not block every later decode, so
    the lock is released after `_ABORT_GRACE_SECS` with a log saying so.
    """
    monkeypatch.setattr(engine_mod, "_engine", None)
    monkeypatch.setattr(engine_mod, "probe", lambda: engine_mod.Availability(True))
    monkeypatch.setattr(engine_mod, "_ABORT_GRACE_SECS", 0.05)
    stub = tmp_path / "ggml-base.bin"
    stub.write_bytes(b"not a real model")

    async def _ensure(_model):
        return stub

    monkeypatch.setattr(models, "store", lambda: type("S", (), {"ensure": staticmethod(_ensure)})())
    release = threading.Event()

    class _StuckModel:
        def transcribe(self, pcm, abort_callback=None, **_kw):
            release.wait(timeout=10.0)
            return []

    monkeypatch.setattr(
        engine_mod.WhisperEngine, "_build_model", staticmethod(lambda key: _StuckModel())
    )
    eng = engine_mod.WhisperEngine()
    await eng.ensure_loaded("base", "en")
    eng._timeout_secs = 0
    try:
        with pytest.raises(engine_mod.DecodeFailed) as caught:
            await eng.decode(np.zeros(engine_mod.SAMPLE_RATE_HZ, dtype=np.float32))
        assert caught.value.code == engine_mod.CODE_DECODE_FAILED
        # The context is RETIRED rather than left resident: the lock had to be
        # released, so the only way to stop the next decode entering a context this
        # call is still executing on is to make sure there is no context to enter.
        assert not eng.loaded, "a context still in use was left available to the next decode"
        assert eng.loaded_key is None
        # With nothing resident this is the documented non-event, not a failure: the
        # caller's re-prepare is what loads a fresh context.
        assert await eng.decode(np.zeros(engine_mod.SAMPLE_RATE_HZ, dtype=np.float32)) == ""
    finally:
        release.set()


@pytest.mark.asyncio
async def test_the_availability_probe_does_not_block_the_event_loop(monkeypatch, tmp_path):
    """`probe` imports the binding, which dlopens a native library.

    Measured at 209 ms cold, and `ensure_loaded` runs on the websocket handler's
    task, so probing inline stalled the WHOLE gateway on the first voice session of a
    boot: every other request, every other session's partials, the heartbeat.

    The probe itself reports the verdict, which is what makes this deterministic
    rather than a timing threshold: it returns successfully only if the event loop
    made progress WHILE it was running. A timed version passed against the buggy
    code, because a blocked loop also blocks the test's own assertions until the
    block clears, at which point everything looks fine in hindsight.
    """
    monkeypatch.setattr(engine_mod, "_engine", None)
    stub = tmp_path / "ggml-base.bin"
    stub.write_bytes(b"not a real model")

    async def _ensure(_model):
        return stub

    monkeypatch.setattr(models, "store", lambda: type("S", (), {"ensure": staticmethod(_ensure)})())
    monkeypatch.setattr(
        engine_mod.WhisperEngine, "_build_model", staticmethod(lambda key: _FakeModel())
    )

    loop_made_progress = threading.Event()
    CODE_BLOCKED = "loop-was-blocked"

    def _probe_watching_the_loop():
        if loop_made_progress.wait(timeout=5.0):
            return engine_mod.Availability(True)
        return engine_mod.Availability(False, CODE_BLOCKED, "the loop never ran")

    monkeypatch.setattr(engine_mod, "probe", _probe_watching_the_loop)

    eng = engine_mod.WhisperEngine()
    load = asyncio.create_task(eng.ensure_loaded("base", "en"))
    # This only runs if the loop is free while the probe is in flight.
    await asyncio.sleep(0.05)
    loop_made_progress.set()
    result = await load
    assert result.code != CODE_BLOCKED, "the event loop was blocked inside probe()"
    assert result.ok
    assert eng.loaded


@pytest.mark.asyncio
async def test_a_timed_out_load_does_not_let_a_retry_start_a_second_context(monkeypatch, tmp_path):
    """Two native contexts at once is what exhausts a host sized for one model.

    A load has no abort hook, so a timeout cannot stop the native allocation. Releasing
    the load lock there let the caller's retry start a SECOND `_build_model` while the
    first was still allocating: 148 MB at the default, 1.6 GB at the largest, twice.
    """
    monkeypatch.setattr(engine_mod, "_engine", None)
    monkeypatch.setattr(engine_mod, "probe", lambda: engine_mod.Availability(True))
    monkeypatch.setattr(engine_mod, "_LOAD_GRACE_SECS", 0.05)
    payload = b"weights"
    model = models.WhisperModel("base", len(payload), hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(models, "models_dir", lambda: tmp_path)
    monkeypatch.setattr(models, "resolve", lambda name: model)
    (tmp_path / model.filename).write_bytes(payload)
    monkeypatch.setattr(models, "store", lambda: models.ModelStore())

    builds = 0
    release = threading.Event()

    def _slow_build(key):
        nonlocal builds
        builds += 1
        release.wait(timeout=10.0)
        return _FakeModel()

    monkeypatch.setattr(engine_mod.WhisperEngine, "_build_model", staticmethod(_slow_build))
    eng = engine_mod.WhisperEngine()
    eng._timeout_secs = 0  # the caller's wait expires at once
    try:
        first = await eng.ensure_loaded("base", "en")
        assert not first.ok, "the caller must still see the timeout"
        # The retry a caller makes next. It must NOT start another context.
        second = await eng.ensure_loaded("base", "en")
        assert not second.ok
        assert builds == 1, f"a second native context was started ({builds} builds)"
    finally:
        release.set()


@pytest.mark.asyncio
async def test_a_load_that_lands_late_is_adopted_rather_than_leaked(monkeypatch, tmp_path):
    """The memory is already allocated, so discarding it just invites a second one."""
    monkeypatch.setattr(engine_mod, "_engine", None)
    monkeypatch.setattr(engine_mod, "probe", lambda: engine_mod.Availability(True))
    monkeypatch.setattr(engine_mod, "_LOAD_GRACE_SECS", 5.0)
    payload = b"weights"
    model = models.WhisperModel("base", len(payload), hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(models, "models_dir", lambda: tmp_path)
    monkeypatch.setattr(models, "resolve", lambda name: model)
    (tmp_path / model.filename).write_bytes(payload)
    monkeypatch.setattr(models, "store", lambda: models.ModelStore())

    def _slightly_slow_build(key):
        time.sleep(0.1)
        return _FakeModel()

    monkeypatch.setattr(
        engine_mod.WhisperEngine, "_build_model", staticmethod(_slightly_slow_build)
    )
    eng = engine_mod.WhisperEngine()
    eng._timeout_secs = 0
    result = await eng.ensure_loaded("base", "en")
    assert not result.ok, "the caller that gave up still gets the timeout"
    assert eng.loaded, "the context that landed late must be adopted, not dropped"


@pytest.mark.asyncio
async def test_idle_eviction_cannot_retire_a_context_a_decode_is_using(monkeypatch, fake_engine):
    """Evicting mid-decode leaves the engine looking unloaded while a context lives.

    The decode survives on its own reference, but a concurrent request then builds a
    SECOND context. Eviction therefore takes the decode lock, which makes the two
    mutually exclusive.
    """
    eng, _ = fake_engine
    monkeypatch.setattr(engine_mod, "_engine", eng)
    await eng.ensure_loaded("base", "en")
    eng._idle_evict_secs = 0  # always due

    in_decode = threading.Event()
    release = threading.Event()

    class _SlowModel:
        def transcribe(self, pcm, abort_callback=None, **_kw):
            in_decode.set()
            release.wait(timeout=10.0)
            return []

    eng._model = _SlowModel()
    decode = asyncio.create_task(eng.decode(np.zeros(engine_mod.SAMPLE_RATE_HZ, dtype=np.float32)))
    await async_wait_until(in_decode.is_set, describe=lambda: "the decode never started")

    evict = asyncio.create_task(eng.maybe_evict())
    await asyncio.sleep(0.15)
    assert not evict.done(), "eviction retired the context while a decode was running"
    assert eng.loaded

    release.set()
    await decode
    await evict


@pytest.mark.asyncio
async def test_replacing_the_model_waits_for_an_active_decode(monkeypatch, fake_engine):
    """Swapping models mid-decode would have two native contexts alive at once.

    The decode survives on its own reference through the bound `model.transcribe`, so
    unloading under the load lock alone leaves the old context allocated while the
    replacement allocates too: 148 MB at the default, 1.6 GB at the largest, doubled on
    a host sized for one. `ensure_loaded` therefore holds the decode lock as well, the
    same pairing `maybe_evict` uses.
    """
    eng, _ = fake_engine
    monkeypatch.setattr(engine_mod, "_engine", eng)
    await eng.ensure_loaded("base", "en")

    in_decode = threading.Event()
    release = threading.Event()

    class _SlowModel:
        def transcribe(self, pcm, abort_callback=None, **_kw):
            in_decode.set()
            release.wait(timeout=10.0)
            return []

    eng._model = _SlowModel()
    decode = asyncio.create_task(eng.decode(np.zeros(engine_mod.SAMPLE_RATE_HZ, dtype=np.float32)))
    await async_wait_until(in_decode.is_set, describe=lambda: "the decode never started")

    # A different LANGUAGE is enough to change the key, so this is a real replacement.
    swap = asyncio.create_task(eng.ensure_loaded("base", "fr"))
    await asyncio.sleep(0.15)
    assert not swap.done(), "the model was replaced while a decode was running on it"

    release.set()
    await decode
    assert (await swap).ok


@pytest.mark.asyncio
async def test_a_download_does_not_hold_the_decode_lock(monkeypatch, tmp_path):
    """The counterpart to the test above: the SWAP waits for a decode, the FETCH must not.

    `store().ensure` transfers up to 1.6 GB and re-hashes what is already on disk, and
    neither touches the resident context — so it has no business holding the lock that
    serialises decodes. Held across it, one session changing model stalls every other
    session for the length of a download, and a stalled stream does not merely lag: it
    keeps buffering until it trips the utterance duration cap, and the speech is
    discarded rather than deferred.

    The assertion is on the lock itself rather than on how long anything took. A
    duration says "slow", which is what a large download looks like when it is working;
    only the lock state says "held".
    """
    monkeypatch.setattr(engine_mod, "_engine", None)
    monkeypatch.setattr(engine_mod, "probe", lambda: engine_mod.Availability(True))
    monkeypatch.setattr(models, "models_dir", lambda: tmp_path)

    fetching = asyncio.Event()
    finish_fetch = asyncio.Event()

    async def _ensure(model):
        fetching.set()
        await finish_fetch.wait()
        return models.model_path(model)

    monkeypatch.setattr(models, "store", lambda: type("S", (), {"ensure": staticmethod(_ensure)})())
    monkeypatch.setattr(
        engine_mod.WhisperEngine, "_build_model", staticmethod(lambda key: _FakeModel())
    )
    eng = engine_mod.WhisperEngine(idle_evict_secs=600)

    finish_fetch.set()
    assert (await eng.ensure_loaded("base", "en")).ok
    resident = engine_mod.LoadedKey(
        str(models.model_path(models.resolve("base"))), "en", engine_mod.thread_count()
    )
    finish_fetch.clear()
    fetching.clear()

    # A second session naming a model that is not on disk yet: the only path that
    # reaches a real transfer.
    fetch = asyncio.create_task(eng.ensure_loaded("small", "en"))
    await await_until(fetching, what="the transfer starting")

    _, decode_lock = eng._locks()
    assert not decode_lock.locked(), "the decode lock is held for the length of a download"
    # And the observable consequence, so this does not reduce to an assertion about an
    # internal: the session that was already streaming still gets its transcript.
    text = await await_until(
        asyncio.ensure_future(
            eng.decode(np.zeros(engine_mod.SAMPLE_RATE_HZ, dtype=np.float32), expect=resident)
        ),
        what="the resident session's transcript",
    )
    assert text == "hello world"

    finish_fetch.set()
    assert (await await_until(fetch, what="the fetch")).ok
    await eng.close()
