"""Tests for set_orch_cfg voice_reply restore — covers auto_speak + peers."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kiro_crew.slack import handler as handler_mod
from kiro_crew.slack.handler import _vc, set_orch_cfg


@pytest.fixture(autouse=True)
def _reset_vc():
    """Reset _vc flags before/after each test."""
    _vc.auto_speak = False
    _vc.global_enabled = False
    _vc.auto_reply_to_voice = False
    _vc.provider = "polly"
    yield
    _vc.auto_speak = False
    _vc.global_enabled = False
    _vc.auto_reply_to_voice = False
    _vc.provider = "polly"


def _cfg_file(tmp_path, monkeypatch, voice_reply: dict) -> None:
    """Write a minimal config.json and point config_path() at it."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"voice_reply": voice_reply}))
    monkeypatch.setattr(handler_mod, "config_path", lambda: p)


def test_set_orch_cfg_restores_auto_speak_true(tmp_path, monkeypatch):
    _cfg_file(tmp_path, monkeypatch, {"enabled": True, "auto_speak": True})
    set_orch_cfg(SimpleNamespace())  # no .raw -> disk fallback
    assert _vc.auto_speak is True
    assert _vc.global_enabled is True


def test_set_orch_cfg_auto_speak_defaults_false_when_missing(tmp_path, monkeypatch):
    _cfg_file(tmp_path, monkeypatch, {"enabled": True})  # no auto_speak key
    set_orch_cfg(SimpleNamespace())
    assert _vc.auto_speak is False


def test_set_orch_cfg_reads_auto_speak_from_raw(tmp_path, monkeypatch):
    # Ensure disk fallback isn't used when cfg.raw is present.
    _cfg_file(tmp_path, monkeypatch, {"auto_speak": False})
    cfg = SimpleNamespace(raw={"voice_reply": {"auto_speak": True}})
    set_orch_cfg(cfg)
    assert _vc.auto_speak is True

# ── auto_reply_to_voice default follows enabled ─────────────────────────


def test_auto_reply_to_voice_defaults_false_when_enabled_false(tmp_path, monkeypatch):
    """Explicit ``enabled=false`` users keep zero-voice behavior."""
    _cfg_file(tmp_path, monkeypatch, {"enabled": False})
    set_orch_cfg(SimpleNamespace())
    assert _vc.auto_reply_to_voice is False
    assert _vc.global_enabled is False


def test_auto_reply_to_voice_defaults_true_when_enabled_true(tmp_path, monkeypatch):
    """Globally-enabled users automatically get symmetric voice-in/voice-out."""
    _cfg_file(tmp_path, monkeypatch, {"enabled": True})
    set_orch_cfg(SimpleNamespace())
    assert _vc.auto_reply_to_voice is True
    assert _vc.global_enabled is True


def test_auto_reply_to_voice_explicit_overrides_enabled_false(tmp_path, monkeypatch):
    """User can set ``auto_reply_to_voice=true`` while keeping ``enabled=false``."""
    _cfg_file(
        tmp_path, monkeypatch,
        {"enabled": False, "auto_reply_to_voice": True},
    )
    set_orch_cfg(SimpleNamespace())
    assert _vc.auto_reply_to_voice is True
    assert _vc.global_enabled is False


def test_auto_reply_to_voice_explicit_overrides_enabled_true(tmp_path, monkeypatch):
    """User can set ``auto_reply_to_voice=false`` while keeping ``enabled=true``."""
    _cfg_file(
        tmp_path, monkeypatch,
        {"enabled": True, "auto_reply_to_voice": False},
    )
    set_orch_cfg(SimpleNamespace())
    assert _vc.auto_reply_to_voice is False
    assert _vc.global_enabled is True


def test_auto_reply_to_voice_default_when_no_voice_reply_section(tmp_path, monkeypatch):
    """No voice_reply section at all -> both default to False (no surprise voice)."""
    _cfg_file(tmp_path, monkeypatch, {})  # empty voice_reply dict
    set_orch_cfg(SimpleNamespace())
    assert _vc.auto_reply_to_voice is False
    assert _vc.global_enabled is False


# ── provider validation on load ─────────────────────────────────────────


def test_provider_polly_accepted(tmp_path, monkeypatch):
    _cfg_file(tmp_path, monkeypatch, {"provider": "polly"})
    set_orch_cfg(SimpleNamespace())
    assert _vc.provider == "polly"


def test_provider_piper_accepted(tmp_path, monkeypatch):
    _cfg_file(tmp_path, monkeypatch, {"provider": "piper"})
    set_orch_cfg(SimpleNamespace())
    assert _vc.provider == "piper"


def test_provider_typo_falls_back_to_polly_with_warning(tmp_path, monkeypatch, caplog):
    """An invalid provider value must be rejected with a warning + fallback to polly."""
    import logging

    _cfg_file(tmp_path, monkeypatch, {"provider": "ploly"})
    with caplog.at_level(logging.WARNING, logger="kiro_crew.slack.handler"):
        set_orch_cfg(SimpleNamespace())
    assert _vc.provider == "polly"
    assert any(
        "voice_reply.provider" in rec.message and "ploly" in rec.message
        for rec in caplog.records
    ), "expected a warning log naming the bad provider value"


def test_provider_empty_string_falls_back_to_polly(tmp_path, monkeypatch):
    _cfg_file(tmp_path, monkeypatch, {"provider": ""})
    set_orch_cfg(SimpleNamespace())
    assert _vc.provider == "polly"


def test_provider_omitted_defaults_to_polly(tmp_path, monkeypatch):
    _cfg_file(tmp_path, monkeypatch, {})
    set_orch_cfg(SimpleNamespace())
    assert _vc.provider == "polly"
