"""The custom whisper model escape hatch: ``stt.model = "custom"`` plus a URL and digest.

The whole question these answer is whether letting the CALLER supply the sha256
weakens the pin. It does not, and each test below pins one half of that claim: a
matching digest installs the file, a wrong one is refused and removed on the
download path AND on the load path, an incomplete pair degrades to a catalog model
instead of running something unverified, and the missing published size is replaced
by an absolute ceiling rather than dropped.
"""

from __future__ import annotations

import hashlib
import json
import unittest.mock
from pathlib import Path

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.stt import models

_PAYLOAD = b"custom weights" * 64
_DIGEST = hashlib.sha256(_PAYLOAD).hexdigest()
_URL = "https://models.example/ggml-my-model.bin"


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
        assert timeout, "the model download must pass a socket timeout"
        return _Response()

    return _open


def _load_stt_config(tmp_path: Path, **stt) -> KiroCrewConfig:
    """Load a real config whose ``stt`` section is *stt*.

    Goes through ``KiroCrewConfig.load`` rather than constructing ``SttConfig``
    directly, because the degrade-to-default behaviour under test lives in the
    LOADER's validation, not in the dataclass.
    """
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    cfg_file = home / "config.json"
    cfg_file.write_text(json.dumps({"stt": stt}), encoding="utf-8")
    with (
        unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=home),
        unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
    ):
        return KiroCrewConfig.load()


def _with_config(monkeypatch, cfg: KiroCrewConfig) -> None:
    """Make ``models.resolve`` see *cfg*.

    ``_configured_custom_model`` imports the config lazily and calls ``load()``,
    so the substitute has to sit on the class the deferred import resolves to.
    """
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda _cls: cfg))


# ── resolve ──


def test_a_configured_custom_model_carries_its_url_and_its_digest(monkeypatch, tmp_path):
    cfg = _load_stt_config(
        tmp_path, model="custom", custom_model_url=_URL, custom_model_sha256=_DIGEST
    )
    assert cfg.stt.model == "custom"
    _with_config(monkeypatch, cfg)
    model = models.resolve("custom")
    assert model.name == models.CUSTOM_MODEL
    assert model.url == _URL
    assert model.sha256 == _DIGEST
    # No published size, which is what the download ceiling stands in for.
    assert model.size_bytes == 0
    # Still an ordinary ggml weight filename, so it stays inside the shell fence
    # `security._WHISPER_WEIGHT_NAME` puts around these files.
    assert model.filename == "ggml-custom.bin"


@pytest.mark.parametrize(
    "url, digest",
    [
        ("", _DIGEST),  # digest with nothing to fetch
        (_URL, ""),  # a URL with no pin is an unverified download
        ("http://models.example/m.bin", _DIGEST),  # plaintext
        (_URL, "not-a-digest"),  # cannot match anything
        (_URL, _DIGEST[:-1]),  # one character short
        (_URL, _DIGEST[:-1] + "z"),  # right length, not hex
    ],
)
def test_an_unusable_custom_pair_degrades_to_the_default_model(
    monkeypatch, tmp_path, caplog, url, digest
):
    """A half-configured custom model must never fail the session that read it.

    Same contract as an unknown catalog name: ``config.json`` is hand-editable, so
    a typo has to leave voice input working on a model that exists.
    """
    cfg = _load_stt_config(
        tmp_path, model="custom", custom_model_url=url, custom_model_sha256=digest
    )
    # The loader already degraded the SELECTION, so nothing downstream sees `custom`.
    assert cfg.stt.model == models.DEFAULT_MODEL
    _with_config(monkeypatch, cfg)
    with caplog.at_level("WARNING"):
        # Asked directly, because a stored `custom` predating a cleared pair reaches
        # `resolve` without passing through the loader again.
        assert models.resolve("custom").name == models.DEFAULT_MODEL
    assert "custom" in caplog.text


def test_an_unreadable_config_degrades_rather_than_raising(monkeypatch, caplog):
    """`resolve` is on a live voice session's path and may not raise out of it."""

    def _explode(_cls):
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_explode))
    with caplog.at_level("WARNING"):
        assert models.resolve("custom").name == models.DEFAULT_MODEL
    assert "custom whisper model" in caplog.text


def test_custom_is_not_reachable_through_the_catalog_or_an_alias():
    """It names no artifact, so it must not appear where a real model is expected."""
    assert models.CUSTOM_MODEL not in {m.name for m in models.CATALOG}
    assert models.CUSTOM_MODEL not in models._ALIASES.values()


# ── the download path ──


def _custom(monkeypatch, tmp_path, digest: str = _DIGEST) -> models.WhisperModel:
    monkeypatch.setattr(models, "models_dir", lambda: tmp_path)
    model = models.custom_model(_URL, digest)
    assert model is not None
    return model


def test_a_matching_digest_installs_the_custom_model(monkeypatch, tmp_path):
    model = _custom(monkeypatch, tmp_path)
    monkeypatch.setattr(models.urllib.request, "urlopen", _stub_urlopen(_PAYLOAD))
    path = models._download_blocking(model)
    assert path == tmp_path / "ggml-custom.bin"
    assert path.read_bytes() == _PAYLOAD
    assert not list(tmp_path.glob("*.part")), "staging file must not survive"


def test_a_wrong_digest_is_refused_and_leaves_no_file(monkeypatch, tmp_path):
    """The caller supplies the pin; it is enforced exactly as a catalog pin is."""
    model = _custom(monkeypatch, tmp_path, hashlib.sha256(b"other weights").hexdigest())
    monkeypatch.setattr(models.urllib.request, "urlopen", _stub_urlopen(_PAYLOAD))
    with pytest.raises(models.ModelDownloadError, match="sha256 mismatch"):
        models._download_blocking(model)
    assert not (tmp_path / "ggml-custom.bin").exists()
    assert not list(tmp_path.glob("*.part"))


def test_the_custom_url_is_fetched_verbatim(monkeypatch, tmp_path):
    """The catalog base URL must not be prepended to an address the user gave.

    ``MODEL_URL_ENV`` mirrors the catalog publisher's layout; a user-supplied
    artifact has no reason to sit under it, and composing one would fetch a path
    that does not exist while looking like a network failure.
    """
    model = _custom(monkeypatch, tmp_path)
    monkeypatch.setenv(models.MODEL_URL_ENV, "https://mirror.example/whisper")
    seen: list[str] = []

    def _open(url, timeout=None):
        seen.append(url)
        return _stub_urlopen(_PAYLOAD)(url, timeout=timeout)

    monkeypatch.setattr(models.urllib.request, "urlopen", _open)
    models._download_blocking(model)
    assert seen == [_URL]


def test_a_response_past_the_custom_ceiling_is_refused_mid_stream(monkeypatch, tmp_path):
    """A digest cannot bound a transfer, so an absolute ceiling replaces the pinned size.

    The digest is only known to be wrong once the last byte has arrived, which is
    too late: a hostile URL could stream until the disk filled. Asserted on what
    reached the disk, because an error raised after a 40 GB write is the same
    outage.
    """
    monkeypatch.setattr(models, "_CUSTOM_MAX_BYTES", 32)
    model = _custom(monkeypatch, tmp_path)
    monkeypatch.setattr(models, "_CHUNK_BYTES", 8)
    monkeypatch.setattr(models.urllib.request, "urlopen", _stub_urlopen(b"w" * 4096))
    with pytest.raises(models.ModelDownloadError, match="ceiling"):
        models._download_blocking(model)
    assert not (tmp_path / "ggml-custom.bin").exists()
    for staged in tmp_path.glob("*.part"):
        raise AssertionError(f"staging file survived: {staged}")


def test_an_empty_response_is_refused(monkeypatch, tmp_path):
    """The one truncation a sizeless model can catch before hashing."""
    model = _custom(monkeypatch, tmp_path)
    monkeypatch.setattr(models.urllib.request, "urlopen", _stub_urlopen(b""))
    with pytest.raises(models.ModelDownloadError, match="no bytes"):
        models._download_blocking(model)


# ── the load path ──


@pytest.mark.asyncio
async def test_a_present_custom_model_is_returned_when_its_digest_matches(monkeypatch, tmp_path):
    model = _custom(monkeypatch, tmp_path)
    (tmp_path / "ggml-custom.bin").write_bytes(_PAYLOAD)
    assert models.is_present(model)

    def _explode(_url, timeout=None):
        raise AssertionError("must not download a model already on disk")

    monkeypatch.setattr(models.urllib.request, "urlopen", _explode)
    monkeypatch.delenv(models.SKIP_DOWNLOAD_ENV, raising=False)
    store = models.ModelStore()
    assert await store.ensure(model) == tmp_path / "ggml-custom.bin"
    assert store.status["step"] == "ready"


@pytest.mark.asyncio
async def test_a_present_custom_model_is_deleted_when_its_digest_does_not_match(
    monkeypatch, tmp_path
):
    """Re-verified on every load, exactly like a catalog model.

    This is the case a caller-supplied pin has to cover to be worth anything: the
    models directory is agent-writable, and the loader re-opens the file by name
    after the check, so a same-name overwrite would otherwise transcribe every
    later utterance through weights nobody verified.
    """
    model = _custom(monkeypatch, tmp_path)
    path = tmp_path / "ggml-custom.bin"
    path.write_bytes(b"weights nobody pinned")

    def _refuse(_url, timeout=None):
        raise OSError("network down")

    monkeypatch.setattr(models.urllib.request, "urlopen", _refuse)
    monkeypatch.delenv(models.SKIP_DOWNLOAD_ENV, raising=False)
    store = models.ModelStore()
    # None because the re-download then failed -- the point is that the unverified
    # file was not returned, and is gone.
    assert await store.ensure(model) is None
    assert not path.exists()


# ── the validators the config surface and the dashboard share ──


@pytest.mark.parametrize(
    "raw, expected",
    [
        (_URL, _URL),
        (f"  {_URL}  ", _URL),
        ("http://models.example/m.bin", ""),
        ("https://", ""),
        ("", ""),
        (None, ""),
        (["https://models.example/m.bin"], ""),
    ],
)
def test_valid_custom_url_accepts_only_a_real_https_address(raw, expected):
    assert models.valid_custom_url(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        (_DIGEST, _DIGEST),
        (_DIGEST.upper(), _DIGEST),
        (f" {_DIGEST}\n", _DIGEST),
        (_DIGEST[:-1], ""),
        (_DIGEST + "a", ""),
        (_DIGEST[:-1] + "g", ""),
        ("", ""),
        (None, ""),
        (0, ""),
    ],
)
def test_valid_custom_sha256_accepts_only_64_hex_characters(raw, expected):
    assert models.valid_custom_sha256(raw) == expected


def test_custom_model_needs_both_halves():
    assert models.custom_model(_URL, "") is None
    assert models.custom_model("", _DIGEST) is None
    assert models.custom_model(_URL, _DIGEST) is not None
