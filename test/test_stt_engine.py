"""The model catalog, its pinned downloader, and the resident engine's lifecycle.

No test here loads a real recogniser or touches the network. The engine's one
seam onto the native library is ``WhisperEngine._build_model``, so a fake stands
in there; the downloader's one seam is ``urllib.request.urlopen``. Both are
patched per test, which is also what keeps a native context from outliving a test
on an xdist worker.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import threading
import time
import urllib.error

import numpy as np
import pytest

from kiro_crew.stt import engine as engine_mod
from kiro_crew.stt import models

# ── Test doubles ──


class _FakeSegment:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeModel:
    """Stands in for pywhispercpp's Model. Records what it was asked to decode."""

    def __init__(self, texts: tuple[str, ...] = ("hello world",)) -> None:
        self._texts = texts
        self.decoded_samples: list[int] = []
        self.aborted = 0

    def transcribe(self, pcm, abort_callback=None, **_kw):
        self.decoded_samples.append(len(pcm))
        if abort_callback is not None and abort_callback():
            self.aborted += 1
            return []
        return [_FakeSegment(t) for t in self._texts]


class _FakeBinding:
    """Stands in for the ``_pywhispercpp`` extension module.

    Only the four entry points ``_decode_segments`` uses. ``status`` is what
    ``whisper_full`` reports, which is the value pywhispercpp discards and the whole
    reason the engine calls the binding rather than ``Model.transcribe``. ``on_full``
    runs inside that call, which is where a live supersession lands.
    """

    def __init__(self, status: int = 0, on_full=None) -> None:
        self.status = status
        self._on_full = on_full
        self.aborts_assigned = 0
        self.abort_callback = None

    def assign_new_segment_callback(self, _params, _cb) -> None:
        pass

    def assign_abort_callback(self, _params, cb) -> None:
        self.aborts_assigned += 1
        self.abort_callback = cb

    def whisper_full(self, _ctx, _params, _pcm, _n) -> int:
        if self._on_full is not None:
            self._on_full()
        return self.status

    def whisper_full_n_segments(self, _ctx) -> int:
        return 0


def _make_native(monkeypatch, fake: _FakeModel, texts: tuple[str, ...] = ("hello world",)) -> None:
    """Give *fake* the private shape ``_decode_segments`` reads a status through.

    A plain ``_FakeModel`` deliberately does NOT have it, so every other test keeps
    exercising the ``Model.transcribe`` fallback; a test about the status opts in.
    Through monkeypatch because the real member is a staticmethod on the CLASS, and a
    bare assignment there would outlive the test on an xdist worker.
    """
    fake._ctx = object()
    fake._params = object()
    monkeypatch.setattr(
        type(fake),
        "_get_segments",
        staticmethod(lambda _ctx, _start, _end: [_FakeSegment(t) for t in texts]),
        raising=False,
    )


@pytest.fixture
def fake_engine(monkeypatch, tmp_path):
    """A WhisperEngine whose native model is a fake and whose weights are a stub file."""
    monkeypatch.setattr(engine_mod, "_engine", None)
    monkeypatch.setattr(engine_mod, "probe", lambda: engine_mod.Availability(True))
    stub = tmp_path / "ggml-base.bin"
    stub.write_bytes(b"not a real model")

    async def _ensure(_model):
        return stub

    monkeypatch.setattr(models, "store", lambda: type("S", (), {"ensure": staticmethod(_ensure)})())
    fake = _FakeModel()
    monkeypatch.setattr(engine_mod.WhisperEngine, "_build_model", staticmethod(lambda key: fake))
    return engine_mod.WhisperEngine(idle_evict_secs=600), fake


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


# ── Download ──


def _stub_urlopen(payload: bytes):
    """A urlopen replacement yielding *payload* in one chunk."""

    class _Response:
        def __init__(self) -> None:
            self._data = payload

        def read(self, _n: int) -> bytes:
            data, self._data = self._data, b""
            return data

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def _open(_url, timeout=None):
        # Asserted rather than merely tolerated: without a timeout a stalled
        # connection holds its worker for the life of the process, and the only
        # symptom is a progress bar frozen at some percentage — which is exactly
        # what a slow-but-working transfer looks like.
        assert timeout, "the model download must pass a socket timeout"
        return _Response()

    return _open


def _pin(monkeypatch, tmp_path, payload: bytes) -> models.WhisperModel:
    """Register a catalog entry pinned to *payload* and point storage at tmp_path."""
    model = models.WhisperModel("stub", len(payload), hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(models, "models_dir", lambda: tmp_path)
    return model


def test_a_verified_download_lands_at_the_final_path(monkeypatch, tmp_path):
    payload = b"weights" * 100
    model = _pin(monkeypatch, tmp_path, payload)
    monkeypatch.setattr(models.urllib.request, "urlopen", _stub_urlopen(payload))
    path = models._download_blocking(model)
    assert path.read_bytes() == payload
    assert not list(tmp_path.glob("*.part")), "staging file must not survive"


def test_a_tampered_payload_is_refused_and_leaves_no_file(monkeypatch, tmp_path):
    """The pin is the whole defence for a network fetch."""
    model = _pin(monkeypatch, tmp_path, b"the real weights")
    monkeypatch.setattr(models.urllib.request, "urlopen", _stub_urlopen(b"the fake weights"))
    with pytest.raises(models.ModelDownloadError, match="sha256 mismatch"):
        models._download_blocking(model)
    assert not (tmp_path / model.filename).exists()
    assert not list(tmp_path.glob("*.part"))


def test_a_truncated_payload_is_refused(monkeypatch, tmp_path):
    payload = b"weights" * 100
    model = _pin(monkeypatch, tmp_path, payload)
    monkeypatch.setattr(models.urllib.request, "urlopen", _stub_urlopen(payload[:-10]))
    with pytest.raises(models.ModelDownloadError, match="bytes"):
        models._download_blocking(model)
    assert not (tmp_path / model.filename).exists()


def test_a_non_https_url_is_refused_before_any_request(monkeypatch, tmp_path):
    payload = b"weights"
    model = _pin(monkeypatch, tmp_path, payload)
    monkeypatch.setenv(models.MODEL_URL_ENV, "http://mirror.example/whisper")

    def _explode(_url):
        raise AssertionError("must not open a plaintext connection")

    monkeypatch.setattr(models.urllib.request, "urlopen", _explode)
    with pytest.raises(models.ModelDownloadError, match="non-https"):
        models._download_blocking(model)


def test_an_oversized_response_is_refused_before_it_fills_the_disk(monkeypatch, tmp_path):
    """The pinned size is a CEILING, not just something to compare afterwards.

    Streaming to EOF and checking the total lets a hostile or misconfigured mirror write
    without bound: nothing about an HTTPS response limits its length, and the operator
    can point ``MODEL_URL_ENV`` anywhere. The digest cannot help, because it is only
    computed once the bytes are already on disk.

    The assertion is on what reached the disk, not merely that an error was raised: an
    exception after a 40 GB write is the same outage.
    """
    payload = b"w" * 64
    model = _pin(monkeypatch, tmp_path, payload)
    monkeypatch.setattr(models, "_CHUNK_BYTES", 16)

    class _Endless:
        """A server that keeps sending, which is the case the size check exists for."""

        def read(self, n: int) -> bytes:
            return b"x" * n

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(models.urllib.request, "urlopen", lambda _u, timeout=None: _Endless())

    with pytest.raises(models.ModelDownloadError, match="exceeds the pinned"):
        models._download_blocking(model)

    assert not list(tmp_path.glob("*")), "the staging file survived a refused transfer"


def test_a_transfer_stops_within_one_chunk_of_the_pinned_size(monkeypatch, tmp_path):
    """The bound has to hold at the moment of the write, so the overshoot is bounded by
    one read rather than by however much the server chose to send."""
    payload = b"w" * 64
    model = _pin(monkeypatch, tmp_path, payload)
    monkeypatch.setattr(models, "_CHUNK_BYTES", 16)
    high_water = []

    real_open = models.os.fdopen

    def _watch(fd, mode):
        handle = real_open(fd, mode)
        real_write = handle.write

        def _write(data):
            high_water.append(handle.tell() + len(data))
            return real_write(data)

        handle.write = _write  # type: ignore[method-assign]
        return handle

    class _Endless:
        def read(self, n: int) -> bytes:
            return b"x" * n

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(models.os, "fdopen", _watch)
    monkeypatch.setattr(models.urllib.request, "urlopen", lambda _u, timeout=None: _Endless())

    with pytest.raises(models.ModelDownloadError):
        models._download_blocking(model)

    assert high_water, "nothing was written, so the bound was not exercised"
    assert max(high_water) <= model.size_bytes, (
        f"wrote {max(high_water)} bytes against a pinned {model.size_bytes}: the check "
        "has to precede the write, not follow it"
    )


def test_an_unreachable_host_does_not_leak_the_staging_descriptor(monkeypatch, tmp_path):
    """A connection that never opens must still hand back the descriptor.

    `mkstemp` returns an UNOWNED fd, and the download's ``finally`` unlinks the path
    rather than closing a handle, so the adoption has to happen before anything that
    can raise. It is not an exotic path: an unreachable host, an HTTP error and the
    stall timeout all land here, and the dashboard offers a retry button — so the
    gateway would run out of descriptors and start failing unrelated file and socket
    work. The spy is what makes this observable: with the connection opened first,
    `os.fdopen` is never REACHED, so asserting on a handle that was never created is
    the only way to tell the two orders apart.
    """
    model = _pin(monkeypatch, tmp_path, b"weights")
    adopted: list = []
    real_fdopen = models.os.fdopen

    def _spy(fd, mode):
        handle = real_fdopen(fd, mode)
        adopted.append(handle)
        return handle

    def _unreachable(_url, timeout=None):
        raise urllib.error.URLError("host is down")

    monkeypatch.setattr(models.os, "fdopen", _spy)
    monkeypatch.setattr(models.urllib.request, "urlopen", _unreachable)

    with pytest.raises(urllib.error.URLError):
        models._download_blocking(model)

    assert adopted, "the descriptor was never adopted, so nothing will ever close it"
    assert all(h.closed for h in adopted), "the staging descriptor outlived the attempt"
    # Windows cannot unlink a file whose handle is still open, so a leaked descriptor
    # strands the partial too. Checked here because it is the operator-visible half.
    assert not list(tmp_path.glob("*")), "a failed attempt left the staging file behind"


def test_the_base_url_is_overridable_for_a_mirror(monkeypatch):
    monkeypatch.setenv(models.MODEL_URL_ENV, "https://mirror.example/whisper/")
    url = models._model_url(models.resolve("base"))
    assert url == "https://mirror.example/whisper/ggml-base.bin"


def test_progress_is_reported_against_the_pinned_total(monkeypatch, tmp_path):
    payload = b"w" * 4096
    model = _pin(monkeypatch, tmp_path, payload)
    monkeypatch.setattr(models.urllib.request, "urlopen", _stub_urlopen(payload))
    seen: list[tuple[int, int]] = []
    models._download_blocking(model, on_progress=lambda d, t: seen.append((d, t)))
    assert seen and seen[-1] == (len(payload), model.size_bytes)


def test_cancellation_stops_the_transfer(monkeypatch, tmp_path):
    payload = b"w" * 4096
    model = _pin(monkeypatch, tmp_path, payload)
    monkeypatch.setattr(models.urllib.request, "urlopen", _stub_urlopen(payload))
    with pytest.raises(models.ModelDownloadError, match="cancelled"):
        models._download_blocking(model, should_cancel=lambda: True)
    assert not list(tmp_path.glob("*"))


@pytest.mark.asyncio
async def test_the_store_refuses_to_download_when_the_escape_hatch_is_set(monkeypatch, tmp_path):
    """The suite sets this for every test; a whisper download must honour it too."""
    monkeypatch.setattr(models, "models_dir", lambda: tmp_path)
    monkeypatch.setenv(models.SKIP_DOWNLOAD_ENV, "1")
    store = models.ModelStore()
    assert await store.ensure(models.resolve("base")) is None
    assert store.status["step"] == "skipped"


def _present_model(monkeypatch, tmp_path, payload: bytes = b"weights") -> models.WhisperModel:
    """A catalog entry whose file is on disk AND matches its pin.

    The pin has to be real now: `ModelStore.ensure` verifies an already-present file
    against it, so a right-size stub is deleted rather than returned.
    """
    model = models.WhisperModel("stub", len(payload), hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(models, "models_dir", lambda: tmp_path)
    (tmp_path / model.filename).write_bytes(payload)
    return model


@pytest.mark.asyncio
async def test_the_store_returns_a_present_model_without_downloading(monkeypatch, tmp_path):
    model = _present_model(monkeypatch, tmp_path)

    def _explode(_url, timeout=None):
        raise AssertionError("must not download a model already on disk")

    monkeypatch.setattr(models.urllib.request, "urlopen", _explode)
    store = models.ModelStore()
    assert await store.ensure(model) == tmp_path / model.filename
    assert store.status["step"] == "ready"


@pytest.mark.asyncio
async def test_a_present_model_is_verified_against_its_pin_not_just_its_size(monkeypatch, tmp_path):
    """A same-size file is not the pinned artifact, and this directory is writable.

    Nothing fences the models directory: neither `is_sensitive_path` nor
    `is_sensitive_write_path` covers it, and a plain ``cp`` over the weights is an
    allowed bash command. So an agent could swap in a model of the right SIZE and
    every later session would transcribe the user's speech through weights of its
    choosing, persistently and with nothing to show it had happened.
    """
    model = _present_model(monkeypatch, tmp_path, b"the real weights")
    path = tmp_path / model.filename
    path.write_bytes(b"the FAKE weights")  # same length, different content
    assert models.is_present(model), "the size check alone still passes, which is the point"

    downloaded: list[str] = []

    def _record(_url, timeout=None):
        downloaded.append("fetch")
        raise OSError("network down")

    monkeypatch.setattr(models.urllib.request, "urlopen", _record)
    monkeypatch.delenv(models.SKIP_DOWNLOAD_ENV, raising=False)
    store = models.ModelStore()
    assert await store.ensure(model) is None
    assert not path.exists(), "the unverified file must be removed, not left to be loaded"
    assert downloaded == ["fetch"], "it must fall through to a re-download"


@pytest.mark.asyncio
async def test_the_pin_check_is_not_cached_against_forgeable_metadata(monkeypatch, tmp_path):
    """A size-and-mtime memo is forgeable by exactly the actor it must catch.

    `os.utime` is available to anything that can write the file, so a same-size
    overwrite with a RESTORED mtime satisfied an identity memo and the next load
    trusted attacker-selected weights. Anything cheap enough to check is also cheap
    enough to forge, so the digest is re-read every time the store is asked.
    """
    model = _present_model(monkeypatch, tmp_path, b"the real weights")
    path = tmp_path / model.filename
    store = models.ModelStore()
    assert await store.ensure(model) == path
    before = path.stat()

    # The overwrite a memo would have missed: same size, mtime put back.
    path.write_bytes(b"the FAKE weights")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.stat().st_size == before.st_size
    assert path.stat().st_mtime_ns == before.st_mtime_ns

    monkeypatch.setattr(models.urllib.request, "urlopen", _stub_urlopen(b"the real weights"))
    monkeypatch.delenv(models.SKIP_DOWNLOAD_ENV, raising=False)
    assert await store.ensure(model) == path
    assert path.read_bytes() == b"the real weights", "the swapped file must be replaced"


@pytest.mark.asyncio
async def test_the_digest_is_read_once_per_load_not_once_per_session(monkeypatch, tmp_path):
    """Where the cost is bounded instead: residency is decided before the store runs.

    `ensure_loaded` builds its key from the path the model WOULD have, so a resident
    model short-circuits without touching the file. The digest is therefore paid per
    LOAD, not per session, which is what makes verifying on every call affordable.
    """
    monkeypatch.setattr(engine_mod, "_engine", None)
    monkeypatch.setattr(engine_mod, "probe", lambda: engine_mod.Availability(True))
    payload = b"weights"
    model = models.WhisperModel("base", len(payload), hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(models, "models_dir", lambda: tmp_path)
    monkeypatch.setattr(models, "resolve", lambda name: model)
    (tmp_path / model.filename).write_bytes(payload)

    hashes = 0
    real = models._sha256_file

    def _counting(path):
        nonlocal hashes
        hashes += 1
        return real(path)

    monkeypatch.setattr(models, "_sha256_file", _counting)
    monkeypatch.setattr(models, "store", lambda: models.ModelStore())
    monkeypatch.setattr(
        engine_mod.WhisperEngine, "_build_model", staticmethod(lambda key: _FakeModel())
    )
    eng = engine_mod.WhisperEngine()
    for _ in range(5):
        assert (await eng.ensure_loaded("base", "en")).ok
    assert hashes == 1, f"a resident model must not re-hash; got {hashes} digests"

    # An eviction is a real reload, and it must verify again -- that is the moment a
    # swapped file would otherwise take effect.
    await eng.close()
    assert (await eng.ensure_loaded("base", "en")).ok
    assert hashes == 2, f"a reload must re-verify; got {hashes} digests"


@pytest.mark.asyncio
async def test_a_failed_download_is_reported_not_raised(monkeypatch, tmp_path):
    """A websocket handler must be able to turn this into a status frame."""
    monkeypatch.setattr(models, "models_dir", lambda: tmp_path)
    monkeypatch.delenv(models.SKIP_DOWNLOAD_ENV, raising=False)

    def _fail(_url, timeout=None):
        raise OSError("network down")

    monkeypatch.setattr(models.urllib.request, "urlopen", _fail)
    store = models.ModelStore()
    assert await store.ensure(models.resolve("base")) is None
    assert store.status["step"] == "failed"
    assert "network down" in str(store.status["error"])


# ── Availability probing ──


def _pretend_absent(monkeypatch):
    """Make the recogniser look NOT INSTALLED, which is what the finder answers.

    Absence has to be simulated at `find_spec` rather than by making the import
    raise. Those are different states and the probe now reports them differently:
    an installed-but-unloadable package (missing system library, too-old glibc,
    numpy ABI mismatch) used to be reported as "install the voice extra" to a user
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


def test_thread_count_is_bounded_and_positive(monkeypatch):
    monkeypatch.setattr(engine_mod, "available_cpus", lambda: 256)
    assert engine_mod.thread_count() == engine_mod.THREAD_CEILING
    monkeypatch.setattr(engine_mod, "available_cpus", lambda: 1)
    assert engine_mod.thread_count() == 1
    monkeypatch.setattr(engine_mod, "available_cpus", lambda: 16)
    assert engine_mod.thread_count() == 8


# ── Engine lifecycle ──


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


def test_shared_engine_is_a_process_singleton(monkeypatch):
    monkeypatch.setattr(engine_mod, "_engine", None)
    assert engine_mod.shared_engine() is engine_mod.shared_engine()


# ── Shared instance and its bounds ──


def test_a_zero_argument_shared_engine_does_not_reset_configured_bounds(monkeypatch):
    """`prewarm` and `close` ask for the engine without opinions about its config.

    The bounds used to default to the module constants, which made every
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
        for _ in range(200):
            await asyncio.sleep(0.01)
            if not eng.loaded:
                break
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


def test_a_planted_symlink_at_a_predictable_staging_path_is_not_followed(monkeypatch, tmp_path):
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
        (tmp_path / guess).symlink_to(victim)

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
    for _ in range(200):
        await asyncio.sleep(0.01)
        if entered.is_set():
            break
    assert entered.is_set(), "the native call never started"
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
    for _ in range(200):
        await asyncio.sleep(0.01)
        if in_decode.is_set():
            break
    assert in_decode.is_set(), "the decode never started"

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
    for _ in range(200):
        await asyncio.sleep(0.01)
        if in_decode.is_set():
            break
    assert in_decode.is_set(), "the decode never started"

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
    await asyncio.wait_for(fetching.wait(), timeout=10.0)

    _, decode_lock = eng._locks()
    assert not decode_lock.locked(), "the decode lock is held for the length of a download"
    # And the observable consequence, so this does not reduce to an assertion about an
    # internal: the session that was already streaming still gets its transcript.
    text = await asyncio.wait_for(
        eng.decode(np.zeros(engine_mod.SAMPLE_RATE_HZ, dtype=np.float32), expect=resident),
        timeout=10.0,
    )
    assert text == "hello world"

    finish_fetch.set()
    assert (await asyncio.wait_for(fetch, timeout=10.0)).ok
    await eng.close()
