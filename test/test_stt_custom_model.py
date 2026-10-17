"""The custom whisper model: ``stt.model = "custom"`` plus a local file and its digest.

Two checks stand where a catalog pin stands, and each test below pins one half of
one of them: the configured path must resolve -- symlinks followed -- to a regular
file inside the models directory, and that file must hash to the configured digest
on every load. A refusal names the rule and leaves the file where it is, and nothing
is ever downloaded for a custom model.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import unittest.mock
from pathlib import Path

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.sections import (
    _validated_stt_custom_path,
    _validated_stt_custom_sha256,
)
from kiro_crew.stt import models
from kiro_crew.stt import session as stt_session

_PAYLOAD = b"custom weights" * 64
_DIGEST = hashlib.sha256(_PAYLOAD).hexdigest()
_NAME = "ggml-my-model.bin"

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX links only")


@pytest.fixture
def models_dir(tmp_path, monkeypatch) -> Path:
    """An isolated models directory, so every path below is checked against it."""
    root = tmp_path / "home" / "models" / "whisper"
    root.mkdir(parents=True)
    monkeypatch.setattr(models, "models_dir", lambda: root)
    return root


@pytest.fixture
def placed(models_dir) -> Path:
    """The user's model file, placed in the models directory."""
    path = models_dir / _NAME
    path.write_bytes(_PAYLOAD)
    return path


def _load_stt_config(tmp_path: Path, **stt) -> KiroCrewConfig:
    """Load a real config whose ``stt`` section is *stt*, through the loader's own
    validation -- which is where the degrade-to-default under test lives."""
    home = tmp_path / "cfg-home"
    home.mkdir(exist_ok=True)
    cfg_file = home / "config.json"
    cfg_file.write_text(json.dumps({"stt": stt}), encoding="utf-8")
    with (
        unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=home),
        unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
    ):
        return KiroCrewConfig.load()


def _with_config(monkeypatch, cfg: KiroCrewConfig) -> None:
    """Make ``models.resolve`` see *cfg* through its deferred ``load()``."""
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda _cls: cfg))


def _custom(path: str = _NAME, digest: str = _DIGEST) -> models.WhisperModel:
    model = models.custom_model(path, digest)
    assert model is not None
    return model


# ── the path check ──


def test_a_file_name_in_the_models_dir_resolves_to_that_file(placed):
    assert models.custom_model_file(_NAME) == placed.resolve()
    # An absolute path INTO the folder is the same file.
    assert models.custom_model_file(str(placed)) == placed.resolve()


def test_a_symlink_that_stays_inside_the_models_dir_is_followed(models_dir, placed):
    (models_dir / "ggml-alias.bin").symlink_to(placed)
    assert models.custom_model_file("ggml-alias.bin") == placed.resolve()


def test_a_path_outside_the_models_dir_is_refused(models_dir, tmp_path):
    """Containment, through every door. Each file named here EXISTS, so the refusal
    is about where it is: ``..`` out of the folder, an absolute path elsewhere, a
    symlink inside the folder that points out, a nested ``..`` that climbs back
    out, and the folder itself."""
    outside = tmp_path / "elsewhere" / "ggml-evil.bin"
    outside.parent.mkdir()
    outside.write_bytes(_PAYLOAD)
    (models_dir.parent / "ggml-sibling.bin").write_bytes(_PAYLOAD)
    (models_dir / "ggml-link.bin").symlink_to(outside)
    (models_dir / "sub").mkdir()

    for value in (
        "../ggml-sibling.bin",
        str(outside),
        "ggml-link.bin",
        "sub/../../ggml-sibling.bin",
        str(models_dir),
        ".",
    ):
        with pytest.raises(models.CustomModelPathError) as excinfo:
            models.custom_model_file(value)
        assert excinfo.value.code == "custom_model_path_outside", value
        # The message names the rule, never the path.
        if len(value) > 1:
            assert value not in str(excinfo.value)


def test_a_dotdot_after_a_symlink_is_refused_not_collapsed(models_dir, placed):
    """``alias/../x`` with ``alias -> .`` names ``models/x`` to the OS but
    ``whisper/x`` as text. Refusing every ``..`` keeps the hashed file and the
    loaded file the same file."""
    (models_dir.parent / _NAME).write_bytes(b"other weights")
    (models_dir / "alias").symlink_to(".", target_is_directory=True)

    with pytest.raises(models.CustomModelPathError) as excinfo:
        models.custom_model_file(f"alias/../{_NAME}")
    assert excinfo.value.code == "custom_model_path_outside"


def test_a_tilde_is_not_expanded(models_dir):
    """No home directory is in play, so ``~user`` is just a name -- and an unknown
    user is a missing file, not an exception."""
    with pytest.raises(models.CustomModelPathError) as excinfo:
        models.custom_model_file("~nonexistent-user-14918/weights.bin")
    assert excinfo.value.code == "custom_model_path_missing"
    assert models.is_present(_custom("~nonexistent-user-14918/weights.bin")) is False
    assert models.model_path(_custom("~nonexistent-user-14918/weights.bin")).is_relative_to(
        models_dir
    )


def test_the_load_path_is_the_vetted_real_path(models_dir, placed):
    """Residency keys are built from ``model_path``, so for a custom model it must
    be the same real file ``ensure`` hashes and the loader opens."""
    (models_dir / "ggml-alias.bin").symlink_to(placed)
    assert models.model_path(_custom("ggml-alias.bin")) == placed.resolve()


def test_a_symlinked_folder_pointing_out_is_refused(models_dir, tmp_path):
    """A symlink can be a DIRECTORY on the way to the file, not only the leaf."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / _NAME).write_bytes(_PAYLOAD)
    (models_dir / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(models.CustomModelPathError) as excinfo:
        models.custom_model_file(f"linked/{_NAME}")
    assert excinfo.value.code == "custom_model_path_outside"


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("ggml-absent.bin", "custom_model_path_missing"),
        ("", "custom_model_path_empty"),
        ("   ", "custom_model_path_empty"),
        (None, "custom_model_path_empty"),
        ("bad\x00name.bin", "custom_model_path_empty"),
    ],
)
def test_an_unusable_path_is_refused_with_its_own_code(models_dir, value, code):
    with pytest.raises(models.CustomModelPathError) as excinfo:
        models.custom_model_file(value)
    assert excinfo.value.code == code


@pytest.mark.parametrize("name", [m.filename for m in models.CATALOG])
def test_a_catalog_file_name_is_refused(models_dir, name):
    """Preparing a built-in model deletes a mismatching file at its path and
    overwrites it with a download, so a custom file there would be destroyed."""
    (models_dir / name).write_bytes(_PAYLOAD)
    with pytest.raises(models.CustomModelPathError) as excinfo:
        models.custom_model_file(name)
    assert excinfo.value.code == "custom_model_path_reserved"


def test_a_symlink_to_a_catalog_file_is_refused(models_dir):
    """Checked on the resolved name, so an alias does not hide a catalog file."""
    (models_dir / "ggml-base.bin").write_bytes(_PAYLOAD)
    (models_dir / "ggml-alias.bin").symlink_to(models_dir / "ggml-base.bin")
    with pytest.raises(models.CustomModelPathError) as excinfo:
        models.custom_model_file("ggml-alias.bin")
    assert excinfo.value.code == "custom_model_path_reserved"


def test_a_case_variant_of_a_catalog_name_is_refused(models_dir):
    """On a case-insensitive volume (the macOS default) ``GGML-BASE.BIN`` is the
    catalog's own file, so the reservation is case-folded on every platform."""
    (models_dir / "GGML-Base.BIN").write_bytes(_PAYLOAD)
    with pytest.raises(models.CustomModelPathError) as excinfo:
        models.custom_model_file("GGML-Base.BIN")
    assert excinfo.value.code == "custom_model_path_reserved"


def test_a_hard_link_to_a_catalog_file_is_refused(models_dir):
    """Identity, not spelling: the same inode under a new name is still the
    catalog's file."""
    (models_dir / "ggml-base.bin").write_bytes(_PAYLOAD)
    (models_dir / "ggml-mine.bin").hardlink_to(models_dir / "ggml-base.bin")
    with pytest.raises(models.CustomModelPathError) as excinfo:
        models.custom_model_file("ggml-mine.bin")
    assert excinfo.value.code == "custom_model_path_reserved"


def test_a_staging_file_is_refused(models_dir):
    (models_dir / "ggml-mine.bin.abc.part").write_bytes(_PAYLOAD)
    with pytest.raises(models.CustomModelPathError) as excinfo:
        models.custom_model_file("ggml-mine.bin.abc.part")
    assert excinfo.value.code == "custom_model_path_reserved"


def test_a_folder_is_not_a_model_file(models_dir):
    (models_dir / "ggml-a-folder.bin").mkdir()
    with pytest.raises(models.CustomModelPathError) as excinfo:
        models.custom_model_file("ggml-a-folder.bin")
    assert excinfo.value.code == "custom_model_path_not_file"


# ── the digest ──


@pytest.mark.parametrize(
    "raw, expected",
    [
        (_DIGEST, _DIGEST),
        (_DIGEST.upper(), _DIGEST),
        (f"  {_DIGEST}  ", _DIGEST),
        (f"sha256:{_DIGEST}", _DIGEST),
        (f"SHA-256:{_DIGEST}", _DIGEST),
        (f"{_DIGEST}  {_NAME}", _DIGEST),
        (f"sha256:{_DIGEST[:-1]}", ""),
        ("sha256:", ""),
        (f"{_NAME}  {_DIGEST}", ""),
        (_DIGEST[:-1] + "z", ""),
        ("", ""),
        (None, ""),
        (7, ""),
    ],
)
def test_valid_custom_sha256_unwraps_carriers_but_accepts_only_64_hex(raw, expected):
    assert models.valid_custom_sha256(raw) == expected


# ── resolve ──


def test_a_configured_custom_model_carries_its_path_and_its_digest(monkeypatch, tmp_path):
    cfg = _load_stt_config(
        tmp_path, model="custom", custom_model_path=_NAME, custom_model_sha256=_DIGEST
    )
    assert cfg.stt.model == models.CUSTOM_MODEL
    _with_config(monkeypatch, cfg)

    model = models.resolve(models.CUSTOM_MODEL)

    assert model.name == models.CUSTOM_MODEL
    assert model.custom_path == _NAME
    assert model.sha256 == _DIGEST
    assert model.size_bytes == 0


@pytest.mark.parametrize("path, digest", [("", _DIGEST), (_NAME, ""), (_NAME, "abc")])
def test_a_half_configured_pair_degrades_to_the_default_model(
    monkeypatch, tmp_path, caplog, path, digest
):
    """A typo in a hand-edited ``config.json`` must leave voice input working."""
    cfg = _load_stt_config(
        tmp_path, model="custom", custom_model_path=path, custom_model_sha256=digest
    )
    assert cfg.stt.model == models.DEFAULT_MODEL
    _with_config(monkeypatch, cfg)
    with caplog.at_level(logging.WARNING):
        assert models.resolve(models.CUSTOM_MODEL).name == models.DEFAULT_MODEL
    assert "custom" in caplog.text


def test_a_complete_pair_with_a_missing_file_does_not_degrade(monkeypatch, tmp_path, models_dir):
    """Degrading here would quietly download and run a DIFFERENT model than the one
    chosen. It stays custom, and fails at load with the reason on screen."""
    cfg = _load_stt_config(
        tmp_path, model="custom", custom_model_path="ggml-gone.bin", custom_model_sha256=_DIGEST
    )
    _with_config(monkeypatch, cfg)
    model = models.resolve(models.CUSTOM_MODEL)
    assert model.name == models.CUSTOM_MODEL
    assert models.is_present(model) is False


def test_an_unreadable_config_degrades_rather_than_raising(monkeypatch, caplog):
    def _explode(_cls):
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_explode))
    with caplog.at_level(logging.WARNING):
        assert models.resolve(models.CUSTOM_MODEL).name == models.DEFAULT_MODEL


def test_the_loader_drops_a_malformed_path_without_quoting_it(caplog):
    with caplog.at_level(logging.WARNING):
        assert _validated_stt_custom_path("secret\x00token") == ""
        assert _validated_stt_custom_sha256("not-a-digest-secret") == ""
    assert "secret" not in caplog.text


# ── presence and sizing ──


def test_a_placed_file_is_present_and_sized_by_itself(placed):
    model = _custom()
    assert models.is_present(model) is True
    assert models.prewarm_size_bytes(model) == len(_PAYLOAD)


def test_a_file_outside_the_models_dir_is_never_present(models_dir, tmp_path):
    outside = tmp_path / _NAME
    outside.write_bytes(_PAYLOAD)
    model = _custom(str(outside))
    assert models.is_present(model) is False
    assert models.prewarm_size_bytes(model) == 0


def test_an_empty_file_is_not_present(models_dir):
    (models_dir / _NAME).write_bytes(b"")
    assert models.is_present(_custom()) is False


# ── loading ──


@pytest.fixture
def no_download(monkeypatch):
    """Fail the test if anything tries to download for a custom model."""

    def _refuse(*_a, **_kw):
        raise AssertionError("a custom model must never be downloaded")

    monkeypatch.setattr(models, "_download_blocking", _refuse)


@pytest.mark.asyncio
async def test_a_matching_file_is_loaded_from_its_real_path(placed, no_download):
    store = models.ModelStore()
    assert await store.ensure(_custom()) == placed.resolve()
    assert store.status["step"] == "ready"


@pytest.mark.asyncio
async def test_a_file_that_does_not_match_its_digest_is_refused_and_kept(placed, no_download):
    """Refused, with the reason in status -- and NOT deleted, because it is the
    user's own file rather than one this store fetched."""
    store = models.ModelStore()
    assert await store.ensure(_custom(digest="0" * 64)) is None
    assert store.status["step"] == "failed"
    assert "SHA-256" in str(store.status["error"])
    assert placed.read_bytes() == _PAYLOAD


@pytest.mark.asyncio
async def test_a_file_outside_the_models_dir_is_never_loaded(models_dir, tmp_path, no_download):
    """The load path runs the containment check itself, so a value that reached
    ``config.json`` without the dashboard's check is still refused."""
    outside = tmp_path / _NAME
    outside.write_bytes(_PAYLOAD)
    store = models.ModelStore()

    assert await store.ensure(_custom(str(outside))) is None
    assert store.status["step"] == "failed"
    assert str(outside) not in str(store.status["error"])


@pytest.mark.asyncio
async def test_a_file_swapped_after_a_good_load_is_refused_on_the_next(placed, no_download):
    """Hashed on every load, not once: a file that passed once proves nothing later."""
    store = models.ModelStore()
    assert await store.ensure(_custom()) is not None
    placed.write_bytes(b"other weights")
    assert await store.ensure(_custom()) is None


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_a_hard_linked_file_is_refused_and_both_names_kept(placed, tmp_path, no_download):
    """A second name is a writable handle onto the bytes the hash just passed, so the
    catalog's descriptor check refuses the custom file too, and touches neither name."""
    alias = tmp_path / "alias.bin"
    os.link(placed, alias)
    store = models.ModelStore()

    assert await store.ensure(_custom()) is None
    assert store.status["step"] == "failed"
    assert store.status.get("refused") is True
    assert placed.read_bytes() == alias.read_bytes() == _PAYLOAD


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_a_linked_whisper_folder_is_refused(models_dir, tmp_path, no_download):
    """The folder must be a real directory, as it must for a catalog file: through a
    link, a file that resolves inside it can sit where the read-only seal does not."""
    elsewhere = tmp_path / "elsewhere"
    models_dir.rename(elsewhere)
    models_dir.symlink_to(elsewhere, target_is_directory=True)
    (elsewhere / _NAME).write_bytes(_PAYLOAD)
    store = models.ModelStore()

    assert await store.ensure(_custom()) is None
    assert store.status["step"] == "failed"
    assert store.status.get("refused") is True
    assert (elsewhere / _NAME).read_bytes() == _PAYLOAD


def test_a_custom_model_announces_no_download(monkeypatch, models_dir):
    """Nothing is downloaded for a custom model, so the voice socket must not say
    "downloading" while a missing file fails in ``prepare`` instead."""
    monkeypatch.setattr(models, "resolve", lambda _name: _custom("ggml-gone.bin"))
    session = stt_session.LocalSession.__new__(stt_session.LocalSession)
    session._model_name = models.CUSTOM_MODEL
    assert session.pending_download() is None


@pytest.mark.asyncio
async def test_the_engine_loads_the_file_ensure_vetted(monkeypatch, models_dir, placed, tmp_path):
    """If the folder changes between building the residency key and the hash, the
    loader still opens the file that PASSED the hash, never the key's earlier guess."""
    from kiro_crew.stt import engine as engine_mod

    stale = tmp_path / "stale-guess.bin"
    monkeypatch.setattr(engine_mod, "_engine", None)
    monkeypatch.setattr(engine_mod, "probe", lambda: engine_mod.Availability(True))
    monkeypatch.setattr(models, "resolve", lambda _name: _custom())
    monkeypatch.setattr(models, "model_path", lambda _m: stale)
    built: list[str] = []

    def _build(key):
        built.append(key.model_path)
        return object()

    monkeypatch.setattr(engine_mod.WhisperEngine, "_build_model", staticmethod(_build))
    monkeypatch.setattr(models, "store", lambda: models.ModelStore())

    eng = engine_mod.WhisperEngine(idle_evict_secs=600)
    result = await eng.ensure_loaded(models.CUSTOM_MODEL, "en")

    assert result.ok, result
    assert built == [str(placed.resolve())]
