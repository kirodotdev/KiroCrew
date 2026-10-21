"""``stt.threads`` and ``KIROCREW_STT_THREADS``: the recogniser's decode thread count.

Covers the one parser every writer shares (``stt.limits.parse_threads``), the
engine's resolver and its precedence, that a changed count reaches the loaded-context
key so it takes effect without a restart, and each write path: a hand-edited
config.json, ``kirocrew config set``. The dashboard endpoint is covered beside its
siblings in ``test_dashboard_handlers_core_coverage.py``.

Every test writes under ``tmp_path``; none touches ``~/.kiro/crew``.
"""

from __future__ import annotations

import argparse
import json
import logging
import unittest.mock
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.validation import validate_config_data
from kiro_crew.stt import engine as engine_mod
from kiro_crew.stt import limits as stt_limits
from kiro_crew.stt import models

# ── parse_threads ──


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0, 0),
        (1, 1),
        (64, 64),
        (stt_limits.MAX_THREADS, stt_limits.MAX_THREADS),
        ("auto", 0),
        (" AUTO ", 0),
        ("", 0),
        ("48", 48),
        (" 8 ", 8),
        ("0048", 48),
        ("0" * 5000 + "64", 64),
        ("0" * 5000, 0),
    ],
)
def test_parse_threads_accepts_auto_and_counts(raw, expected):
    assert stt_limits.parse_threads(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        -1,
        stt_limits.MAX_THREADS + 1,
        1.5,
        8.0,
        True,
        False,
        None,
        "lots",
        "-4",
        "8.0",
        "١٦",
        [8],
        "9" * 5000,
        "0" * 5000 + "9" * 5000,
    ],
)
def test_parse_threads_refuses_junk(raw):
    assert stt_limits.parse_threads(raw) is None


# ── thread_count resolver ──


@pytest.fixture
def cpus(monkeypatch):
    """Pin the host's visible cores, and start with no env override."""

    def _set(n: int) -> None:
        monkeypatch.setattr(engine_mod, "available_cpus", lambda: n)

    monkeypatch.delenv(stt_limits.THREADS_ENV, raising=False)
    monkeypatch.setattr(engine_mod, "_warned_thread_env", set())
    _set(128)
    return _set


def test_auto_keeps_half_the_cores_under_the_ceiling(cpus):
    assert engine_mod.thread_count() == stt_limits.THREAD_CEILING
    assert engine_mod.thread_count(stt_limits.THREADS_AUTO) == stt_limits.THREAD_CEILING
    cpus(12)
    assert engine_mod.thread_count() == 6
    cpus(1)
    assert engine_mod.thread_count() == 1


def test_an_explicit_count_is_used_as_given_past_the_ceiling(cpus):
    assert engine_mod.thread_count(64) == 64
    assert engine_mod.thread_count(1) == 1


def test_an_explicit_count_is_clamped_to_the_visible_cores(cpus):
    cpus(20)
    assert engine_mod.thread_count(64) == 20
    assert engine_mod.thread_count(20) == 20


def test_the_env_var_outranks_the_setting(cpus):
    assert engine_mod.thread_count(8, {stt_limits.THREADS_ENV: "48"}) == 48
    # `auto` in the environment is an override too, back to the derived count.
    assert (
        engine_mod.thread_count(64, {stt_limits.THREADS_ENV: "auto"}) == stt_limits.THREAD_CEILING
    )
    # The env value is clamped like any other explicit count.
    cpus(20)
    assert engine_mod.thread_count(8, {stt_limits.THREADS_ENV: "96"}) == 20


def test_an_empty_env_var_leaves_the_setting_in_force(cpus):
    assert engine_mod.thread_count(32, {stt_limits.THREADS_ENV: "  "}) == 32


def test_the_process_environment_is_read_by_default(cpus, monkeypatch):
    monkeypatch.setenv(stt_limits.THREADS_ENV, "40")
    assert engine_mod.thread_count(8) == 40


def test_an_unusable_env_var_is_ignored_and_logged_once(cpus, caplog):
    env = {stt_limits.THREADS_ENV: "lots"}
    with caplog.at_level(logging.WARNING, logger=engine_mod.logger.name):
        assert engine_mod.thread_count(24, env) == 24
        assert engine_mod.thread_count(24, env) == 24
    warnings = [r for r in caplog.records if stt_limits.THREADS_ENV in r.getMessage()]
    assert len(warnings) == 1


# ── The engine carries the setting into the loaded-context key ──


class _FakeModel:
    def transcribe(self, *_args, **_kwargs):
        return []


@pytest.fixture
def loading_engine(monkeypatch, tmp_path, cpus):
    monkeypatch.setattr(engine_mod, "_engine", None)
    monkeypatch.setattr(engine_mod, "probe", lambda: engine_mod.Availability(True))
    monkeypatch.setattr(
        engine_mod.WhisperEngine,
        "capabilities",
        staticmethod(lambda: unittest.mock.MagicMock(backend="cpu")),
    )
    stub = tmp_path / "ggml-base.bin"
    stub.write_bytes(b"not a real model")

    async def _ensure(_model):
        return stub

    monkeypatch.setattr(models, "store", lambda: type("S", (), {"ensure": staticmethod(_ensure)})())
    built: list[int] = []

    def _build(key):
        built.append(key.n_threads)
        return _FakeModel()

    monkeypatch.setattr(engine_mod.WhisperEngine, "_build_model", staticmethod(_build))
    return built


@pytest.mark.asyncio
async def test_a_changed_setting_reloads_with_the_new_count(loading_engine):
    """The count is part of ``LoadedKey``, so a new value is a different context and
    the next ``ensure_loaded`` rebuilds it. No restart, and no stale context."""
    eng = engine_mod.shared_engine(idle_evict_secs=600, timeout_secs=300)
    assert (await eng.ensure_loaded("base", "en")).ok
    assert eng.loaded_key is not None
    assert eng.loaded_key.n_threads == stt_limits.THREAD_CEILING

    same = engine_mod.shared_engine(idle_evict_secs=600, timeout_secs=300, threads=64)
    assert same is eng
    assert eng.thread_count() == 64
    assert (await eng.ensure_loaded("base", "en")).ok
    assert eng.loaded_key.n_threads == 64
    assert loading_engine == [stt_limits.THREAD_CEILING, 64]

    # Unchanged setting: the resident context is reused, nothing is rebuilt.
    assert (await eng.ensure_loaded("base", "en")).ok
    assert loading_engine == [stt_limits.THREAD_CEILING, 64]


def test_a_caller_without_a_thread_opinion_keeps_the_setting(loading_engine):
    """`prewarm` and `close` call `shared_engine()` bare; they must not reset it."""
    eng = engine_mod.shared_engine(threads=48)
    assert engine_mod.shared_engine() is eng
    assert eng.thread_count() == 48
    engine_mod.shared_engine(idle_evict_secs=30, timeout_secs=45)
    assert eng.thread_count() == 48
    assert eng._idle_evict_secs == 30


def test_a_session_built_without_threads_keeps_the_setting(loading_engine):
    """`LocalSession` defaults `threads` to no opinion, like `shared_engine`."""
    from kiro_crew.stt import session as session_mod

    eng = engine_mod.shared_engine(threads=48)
    session_mod.LocalSession()
    assert eng.thread_count() == 48


def test_a_threads_only_update_leaves_the_bounds_alone(loading_engine):
    eng = engine_mod.shared_engine(idle_evict_secs=30, timeout_secs=45)
    engine_mod.shared_engine(threads=8)
    assert (eng._idle_evict_secs, eng._timeout_secs) == (30, 45)
    assert eng.thread_count() == 8


def test_a_session_asks_its_engine_for_the_count(loading_engine, monkeypatch):
    """``pending_load`` compares against the key ``prepare`` would build, which must
    use the configured count, or every session would announce a load it never runs."""
    from kiro_crew.stt.session import LocalSession

    monkeypatch.setattr(models, "is_present", lambda _m: True)
    session = LocalSession(model_name="base", language="en", threads=64)
    assert session._engine.thread_count() == 64
    assert session.pending_load() is True


# ── Config: load, validation, CLI ──


def _loaded_threads(tmp_path: Path, value: object) -> int:
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"stt": {"threads": value}}))
    with unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path):
        return KiroCrewConfig.load().stt.threads


def test_absent_threads_load_as_auto(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"stt": {}}))
    with unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path):
        assert KiroCrewConfig.load().stt.threads == stt_limits.THREADS_AUTO


@pytest.mark.parametrize(
    ("stored", "loaded"), [(64, 64), ("auto", 0), ("32", 32), (0, 0), ("0" * 5000 + "64", 64)]
)
def test_stored_threads_load_as_the_int(tmp_path, stored, loaded):
    assert _loaded_threads(tmp_path, stored) == loaded


@pytest.mark.parametrize(
    "stored",
    [-3, stt_limits.MAX_THREADS + 1, "lots", 2.5, True, "9" * 5000, "0" * 5000 + "9" * 5000],
)
def test_unusable_stored_threads_load_as_auto_with_a_warning(tmp_path, stored, caplog):
    with caplog.at_level(logging.WARNING):
        assert _loaded_threads(tmp_path, stored) == stt_limits.THREADS_AUTO
    assert any("stt.threads" in r.getMessage() for r in caplog.records)


def test_validation_turns_auto_into_the_int_without_a_type_warning(caplog):
    pytest.importorskip("jsonschema")
    data = {"stt": {"threads": "auto"}}
    with caplog.at_level(logging.WARNING):
        validated = validate_config_data(data)
    assert validated["stt"]["threads"] == 0
    assert not [r for r in caplog.records if "threads" in r.getMessage()]


def _cli_set(home: Path, value: str) -> None:
    from kiro_crew.cli_config import _config_cmd

    args = argparse.Namespace(
        config_action="set", key="stt.threads", value=value, file=None, local=False
    )
    with (
        patch("kiro_crew.cli_config.config_path", return_value=home / "config.json"),
        patch("kiro_crew.cli_config.config_local_path", return_value=home / "config.local.json"),
        patch("kiro_crew.config.loader.config_path", return_value=home / "config.json"),
        patch("kiro_crew.config.loader.config_dir", return_value=home),
        patch("kiro_crew.cli_config.sel"),
    ):
        _config_cmd(args)


def _stored(home: Path) -> object:
    return json.loads((home / "config.json").read_text(encoding="utf-8"))["stt"]["threads"]


def test_cli_set_stores_a_count_and_auto(tmp_path):
    _cli_set(tmp_path, "64")
    assert _stored(tmp_path) == 64
    _cli_set(tmp_path, "auto")
    assert _stored(tmp_path) == 0


@pytest.mark.parametrize(
    "value", ["lots", "-1", "2.5", str(stt_limits.MAX_THREADS + 1), "9" * 5000]
)
def test_cli_set_refuses_junk_and_writes_nothing(tmp_path, value, capsys):
    _cli_set(tmp_path, "24")
    before = (tmp_path / "config.json").read_text(encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        _cli_set(tmp_path, value)
    assert exc.value.code == 1
    assert "stt.threads" in capsys.readouterr().err
    assert (tmp_path / "config.json").read_text(encoding="utf-8") == before
