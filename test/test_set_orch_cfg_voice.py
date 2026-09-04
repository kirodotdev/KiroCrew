"""Tests for the voice_reply restore — set_orch_cfg and the dashboard path."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kiro_crew.slack import handler as handler_mod
from kiro_crew.slack.handler import _vc, load_voice_reply_config, set_orch_cfg
from kiro_crew.voice_reply import DEFAULT_PROVIDER, PROVIDER_POLLY


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
        tmp_path,
        monkeypatch,
        {"enabled": False, "auto_reply_to_voice": True},
    )
    set_orch_cfg(SimpleNamespace())
    assert _vc.auto_reply_to_voice is True
    assert _vc.global_enabled is False


def test_auto_reply_to_voice_explicit_overrides_enabled_true(tmp_path, monkeypatch):
    """User can set ``auto_reply_to_voice=false`` while keeping ``enabled=true``."""
    _cfg_file(
        tmp_path,
        monkeypatch,
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


def test_provider_typo_falls_back_to_local_with_warning(tmp_path, monkeypatch, caplog):
    """An invalid provider value must warn and fall back to the LOCAL provider.

    Flipped from asserting a "polly" fallback: falling back to a PAID cloud
    service on a typo is the wrong direction. A wrong local provider costs
    nothing and degrades to a "TTS isn't configured" notice, whereas a wrong
    cloud fallback bills an AWS account the operator never chose.
    """
    import logging

    _cfg_file(tmp_path, monkeypatch, {"provider": "ploly"})
    with caplog.at_level(logging.WARNING, logger="kiro_crew.slack.handler"):
        set_orch_cfg(SimpleNamespace())
    assert _vc.provider == DEFAULT_PROVIDER
    # The direction is the point: a bad or absent value must never resolve
    # to the paid cloud provider.
    assert _vc.provider != PROVIDER_POLLY
    assert any(
        "voice_reply.provider" in rec.message and "ploly" in rec.message for rec in caplog.records
    ), "expected a warning log naming the bad provider value"


def test_provider_empty_string_falls_back_to_local(tmp_path, monkeypatch):
    _cfg_file(tmp_path, monkeypatch, {"provider": ""})
    set_orch_cfg(SimpleNamespace())
    assert _vc.provider == DEFAULT_PROVIDER
    # The direction is the point: a bad or absent value must never resolve
    # to the paid cloud provider.
    assert _vc.provider != PROVIDER_POLLY


def test_provider_omitted_defaults_to_local(tmp_path, monkeypatch):
    """The regression this flip closes: voice ON, provider unnamed.

    This previously resolved to Amazon Polly, so a config that only said
    ``enabled: true`` reached a paid AWS service under whatever the ambient
    credential chain resolved to -- with no operator decision behind it.
    """
    _cfg_file(tmp_path, monkeypatch, {})
    set_orch_cfg(SimpleNamespace())
    assert _vc.provider == DEFAULT_PROVIDER
    # The direction is the point: a bad or absent value must never resolve
    # to the paid cloud provider.
    assert _vc.provider != PROVIDER_POLLY


def test_non_string_path_and_voice_values_normalise_to_unset(tmp_path, monkeypatch):
    """A wrong TYPE in config.json must not reach the dashboard's config GET.

    `config.json` is hand-editable and JSON permits any shape, so `system_voice:
    {}` used to be stored verbatim, served by the config endpoint, and crash the
    React panel that renders it. Normalising to `""` (unset) rather than
    `str(value)` matters: stringifying would persist `"{}"` as a voice name and
    move the failure into synthesis instead of removing it.

    All TEN string reads in that block are covered, not just the four this
    feature touched: they are the same shape and fail identically, and stopping
    at a subset would leave the next reader guessing which ones are safe.
    ``rate``/``pitch`` are coerced here as well as in their synthesis-time
    validators, because the validators protect synthesis and this protects the
    config GET.
    """
    _cfg_file(
        tmp_path,
        monkeypatch,
        {
            "enabled": True,
            "system_voice": {},
            "piper_binary": ["/usr/bin/piper"],
            "piper_model": 42,
            "piper_model_config": None,
            "voice_id": {},
            "engine": [],
            "rate": {"pct": 100},
            "pitch": 0,
            "aws_profile": {},
            "region": [],
        },
    )
    load_voice_reply_config()
    # Fields whose "unset" is empty.
    assert _vc.system_voice == ""
    assert _vc.piper_binary == ""
    assert _vc.piper_model == ""
    assert _vc.piper_model_config == ""
    assert _vc.aws_profile == ""
    assert _vc.region == ""
    # Fields with a real documented default fall back to it, not to empty: an
    # empty engine or rate would be a different defect from the one being fixed.
    assert _vc.default_voice == "Ruth"
    assert _vc.default_engine == "generative"
    assert _vc.default_rate == "100%"
    assert _vc.default_pitch == "+0%"


def test_string_path_and_voice_values_are_preserved(tmp_path, monkeypatch):
    """The normalisation must not eat legitimate values."""
    _cfg_file(
        tmp_path,
        monkeypatch,
        {"enabled": True, "system_voice": "Alex", "piper_binary": "/usr/bin/piper"},
    )
    load_voice_reply_config()
    assert _vc.system_voice == "Alex"
    assert _vc.piper_binary == "/usr/bin/piper"


# ── restore without a Slack orchestrator (dashboard-only gateway) ────────


def test_load_voice_reply_config_restores_from_disk_without_cfg(tmp_path, monkeypatch):
    """A gateway with no Slack tokens never calls set_orch_cfg, so the
    loader must be callable with no config object and fall back to disk."""
    _cfg_file(tmp_path, monkeypatch, {"enabled": True, "auto_speak": True})
    load_voice_reply_config()
    assert _vc.auto_speak is True
    assert _vc.global_enabled is True
    assert _vc.auto_reply_to_voice is True


def test_load_voice_reply_config_keeps_defaults_when_config_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(handler_mod, "config_path", lambda: tmp_path / "missing.json")
    load_voice_reply_config()
    assert _vc.auto_speak is False
    assert _vc.global_enabled is False


def test_load_voice_reply_config_prefers_cfg_raw_over_disk(tmp_path, monkeypatch):
    _cfg_file(tmp_path, monkeypatch, {"auto_speak": False})
    load_voice_reply_config(SimpleNamespace(raw={"voice_reply": {"auto_speak": True}}))
    assert _vc.auto_speak is True


def test_dashboard_entrypoints_restore_voice_settings():
    """Wiring pin: BOTH dashboard app builders must load persisted voice
    settings at boot. On a dashboard-only gateway (no Slack tokens) nothing
    else does — without this call every restart silently resets TTS to
    disabled while the dashboard's settings PUT keeps reporting success."""
    import inspect

    from kiro_crew.dashboard import server as server_mod

    for func, name in (
        (server_mod.start_dashboard, "start_dashboard"),
        (server_mod.start_api_server, "start_api_server"),
    ):
        assert "await asyncio.to_thread(load_voice_reply_config)" in inspect.getsource(
            func
        ), f"{name} must restore persisted voice settings without blocking the event loop"
