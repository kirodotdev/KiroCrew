"""Shared doubles for the ``test_stt_engine*.py`` modules.

The engine's one seam onto the native library is ``WhisperEngine._build_model``:
``_FakeModel`` (with ``_FakeSegment``) stands in there, ``_FakeBinding`` and
``_make_native`` stand in for the low-level binding, and ``fake_engine`` is a
``WhisperEngine`` over a fake model and a stub weights file. The downloader's one seam is
``urllib.request.urlopen``: ``_stub_urlopen`` answers it in one chunk, and ``_pin``
registers a catalog entry pinned to a payload with storage under the test's tmp dir.
"""

from __future__ import annotations

import hashlib

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


@pytest.fixture(name="fake_engine")
def fake_engine_fixture(monkeypatch, tmp_path):
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
