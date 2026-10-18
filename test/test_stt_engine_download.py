"""The model downloader and the model store's verified load.

Covers ``models``' pinned download (a verified payload landing at its final path, a
tampered or truncated payload refused with no file left, a non-HTTPS URL refused before
any request, an oversized response stopped within one chunk of the pinned size, an
unreachable host leaving no staging descriptor, a mirror base URL, progress against the
pinned total, cancellation), the store's escape hatch, a present model returned without
a download but verified against its pin rather than its size or forgeable metadata, the
digest read once per load, and the load path's digest stage kept apart from a first-run
download. No test touches the network: each one that reaches the downloader replaces
``urllib.request.urlopen``, with ``_stub_urlopen`` from ``stt_engine_helpers.py`` or a
local double for a failure shape.
"""

from __future__ import annotations

import hashlib
import os
import urllib.error

import pytest
from stt_engine_helpers import (
    _FakeModel,
    _pin,
    _stub_urlopen,
)

from kiro_crew.stt import engine as engine_mod
from kiro_crew.stt import models


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
    real = models._sha256_verified_file

    def _counting(path):
        nonlocal hashes
        hashes += 1
        return real(path)

    monkeypatch.setattr(models, "_sha256_verified_file", _counting)
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
async def test_the_load_path_records_the_digest_stage(monkeypatch, tmp_path):
    """The stage has to be recorded from PRODUCTION, not only from a test.

    `record_hash` existing is not the same as it being called: with no caller the
    payload reports `hash_ms: 0` forever while the surface claims a load is split
    into hash / load / first-decode. That is the shape a reviewer caught, and this
    asserts the wiring rather than the recorder.
    """
    from kiro_crew.stt import telemetry as telemetry_mod

    monkeypatch.setattr(engine_mod, "_engine", None)
    monkeypatch.setattr(engine_mod, "probe", lambda: engine_mod.Availability(True))
    payload = b"weights"
    model = models.WhisperModel("base", len(payload), hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(models, "models_dir", lambda: tmp_path)
    monkeypatch.setattr(models, "resolve", lambda name: model)
    (tmp_path / model.filename).write_bytes(payload)
    monkeypatch.setattr(models, "store", lambda: models.ModelStore())
    monkeypatch.setattr(
        engine_mod.WhisperEngine, "_build_model", staticmethod(lambda key: _FakeModel())
    )
    monkeypatch.setattr(telemetry_mod, "_recorder", telemetry_mod.Recorder())

    eng = engine_mod.WhisperEngine()
    assert (await eng.ensure_loaded("base", "en")).ok
    snapshot = telemetry_mod.recorder().snapshot()
    # The COUNTER is the wiring: it moves only when the load path calls the recorder,
    # which is the thing that was missing. Deliberately NOT asserting `hash_ms > 0`:
    # this fixture hashes a 7-byte payload, and on Windows `time.monotonic()` advances
    # in ~15.6 ms steps, so a correctly-recorded stage legitimately measures 0.0 there.
    # A duration assertion would be testing the clock's resolution, not the wiring.
    assert snapshot["hashes"] == 1, snapshot
    # And folded onto the load it belongs to rather than left pending or misattributed,
    # which is what keying the pending slot by model buys.
    assert snapshot["last_load"]["model"] == "base"
    assert "hash_ms" in snapshot["last_load"], snapshot["last_load"]


@pytest.mark.asyncio
async def test_a_first_run_download_is_not_filed_as_digest_time(monkeypatch, tmp_path):
    """`ensure` both downloads and verifies, and only one of those recurs.

    On a first run the interval around it is 1.6 GB of network time, so recording it
    as `hash_ms` would put a number in the load's digest stage that says nothing about
    hashing -- and the stage exists to name the cost paid on EVERY load. A download is
    its own stage with its own progress surface.
    """
    from kiro_crew.stt import telemetry as telemetry_mod

    monkeypatch.setattr(engine_mod, "_engine", None)
    monkeypatch.setattr(engine_mod, "probe", lambda: engine_mod.Availability(True))
    payload = b"weights"
    model = models.WhisperModel("base", len(payload), hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(models, "models_dir", lambda: tmp_path)
    monkeypatch.setattr(models, "resolve", lambda name: model)
    # ABSENT when the load starts, and written by the "download" the store performs.
    monkeypatch.setattr(models, "is_present", lambda m: False)

    async def _ensure(m):
        (tmp_path / m.filename).write_bytes(payload)
        return tmp_path / m.filename

    store = models.ModelStore()
    monkeypatch.setattr(store, "ensure", _ensure)
    monkeypatch.setattr(models, "store", lambda: store)
    monkeypatch.setattr(
        engine_mod.WhisperEngine, "_build_model", staticmethod(lambda key: _FakeModel())
    )
    monkeypatch.setattr(telemetry_mod, "_recorder", telemetry_mod.Recorder())

    eng = engine_mod.WhisperEngine()
    assert (await eng.ensure_loaded("base", "en")).ok
    snapshot = telemetry_mod.recorder().snapshot()
    assert snapshot["hashes"] == 0, snapshot


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
