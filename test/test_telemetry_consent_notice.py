"""Usage-data consent: DO_NOT_TRACK, the notice version and the CLI.

These drive the real code paths against real config files under a temporary
data home: the loader, ``beacon.telemetry_permitted``, ``metrics.provider`` and
``kirocrew telemetry enable|disable``. Nothing here sends data anywhere.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from kiro_crew import beacon
from kiro_crew import cli_commands as cc
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.metrics import provider

_ENV_VARS = ("DO_NOT_TRACK", "KIROCREW_TELEMETRY", "KIROCREW_TELEMETRY_DISABLED")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private data home with no telemetry env vars set."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def _write_config(home: Path, doc: dict, name: str = "config.json") -> None:
    (home / name).write_text(json.dumps(doc), encoding="utf-8")


def _read_config(home: Path) -> dict:
    return json.loads((home / "config.json").read_text(encoding="utf-8"))


# -- DO_NOT_TRACK --


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", " TRUE "])
def test_do_not_track_turns_both_switches_off(
    home: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("DO_NOT_TRACK", value)
    assert beacon.do_not_track() is True
    assert provider.env_pin() is False
    assert provider.pin_env_var() == "DO_NOT_TRACK"
    assert beacon.is_env_opted_out() is True
    assert beacon.opt_out_env_var() == "DO_NOT_TRACK"
    verdict = beacon.telemetry_permitted(enabled=True, acked=True)
    assert (verdict.ok, verdict.code) == (False, "do_not_track")


def test_do_not_track_wins_over_an_on_value(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Off wins: an on value in the product's own variable cannot undo it."""
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    monkeypatch.setenv("KIROCREW_TELEMETRY", "1")
    _write_config(home, {"telemetry": {"enabled": True}})
    assert provider._consent_enabled(KiroCrewConfig.load().telemetry) is False


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
def test_falsy_do_not_track_changes_nothing(
    home: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("DO_NOT_TRACK", value)
    monkeypatch.setenv("KIROCREW_TELEMETRY", "1")
    assert beacon.do_not_track() is False
    assert provider.env_pin() is True
    assert provider.pin_env_var() == "KIROCREW_TELEMETRY"
    assert beacon.opt_out_env_var() == "KIROCREW_TELEMETRY_DISABLED"


def test_do_not_track_has_a_reason_code() -> None:
    """The dashboard owns one catalog string per code; the code must be listed."""
    assert "do_not_track" in beacon.REASONS


# -- the notice version --


def test_ack_version_defaults_to_zero_without_falling_back(home: Path) -> None:
    """An existing user (onboarded, acked) reads 0, so the new notice shows once."""
    _write_config(home, {"dashboard": {"onboarded": True, "privacy_acked": True}})
    dash = KiroCrewConfig.load().dashboard
    assert dash.privacy_acked is True
    assert dash.privacy_ack_version == 0


@pytest.mark.parametrize(("raw", "expected"), [(2, 2), ("2", 2), (True, 0), (-5, 0), (10**6, 1000)])
def test_ack_version_is_coerced_and_clamped(home: Path, raw: object, expected: int) -> None:
    _write_config(home, {"dashboard": {"privacy_ack_version": raw}})
    assert KiroCrewConfig.load().dashboard.privacy_ack_version == expected


# -- kirocrew telemetry enable|disable|status --


def _ns(action: str) -> argparse.Namespace:
    return argparse.Namespace(telemetry_action=action)


def test_cli_disable_turns_both_off_and_survives_a_reload(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config(home, {"agent": {"model": "auto"}, "telemetry": {"enabled": True}})
    cc._telemetry(_ns("disable"))
    stored = _read_config(home)
    assert stored["telemetry"] == {"enabled": False, "beacon_enabled": False}
    assert stored["dashboard"]["privacy_acked"] is True
    # Disable records that the user chose, not that they were told recording is on.
    assert "privacy_ack_version" not in stored["dashboard"]
    assert stored["agent"] == {"model": "auto"}
    loaded = KiroCrewConfig.load()
    assert (loaded.telemetry.enabled, loaded.telemetry.beacon_enabled) == (False, False)
    assert "DISABLED" in capsys.readouterr().out


def test_cli_enable_turns_both_on(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(beacon, "is_governance_pinned_off", lambda **kw: False)
    _write_config(home, {"telemetry": {"enabled": False, "beacon_enabled": False}})
    cc._telemetry(_ns("enable"))
    loaded = KiroCrewConfig.load()
    assert (loaded.telemetry.enabled, loaded.telemetry.beacon_enabled) == (True, True)
    assert loaded.dashboard.privacy_ack_version == beacon.PRIVACY_NOTICE_VERSION


def test_cli_reports_an_overlay_that_keeps_recording_on(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config(home, {})
    _write_config(home, {"telemetry": {"enabled": True}}, "config.local.json")
    with pytest.raises(SystemExit) as exc:
        cc._telemetry(_ns("disable"))
    assert exc.value.code == 1
    assert "local metric recording is still ON" in capsys.readouterr().err


def test_cli_status_shows_both_switches(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config(home, {"telemetry": {"enabled": True}})
    cc._telemetry(_ns("status"))
    out = capsys.readouterr().out
    assert "Local metric recording" in out
    assert "telemetry.enabled is true" in out
    assert "Anonymous usage beacon" in out
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    cc._telemetry(_ns("status"))
    out = capsys.readouterr().out
    assert "Pinned by:   DO_NOT_TRACK" in out


def test_cli_warns_when_an_env_var_overrides_the_write(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(beacon, "is_governance_pinned_off", lambda **kw: False)
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    cc._telemetry(_ns("enable"))
    err = capsys.readouterr().err
    assert "DO_NOT_TRACK is set" in err


# -- the two notice-version constants --


def test_dashboard_notice_version_matches_the_backend() -> None:
    """The chapter and the export gate must agree on which notice is current."""
    root = Path(__file__).resolve().parents[1]
    source = (root / "website" / "src" / "components" / "privacyNotice.ts").read_text(
        encoding="utf-8"
    )
    assert f"PRIVACY_NOTICE_VERSION = {beacon.PRIVACY_NOTICE_VERSION}" in source


def test_cli_enable_leaves_recording_alone_when_it_would_start_export(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Same rule as the dashboard's 409: enabling recording here would export."""
    monkeypatch.setattr(beacon, "is_governance_pinned_off", lambda **kw: False)
    monkeypatch.setattr(provider, "otlp_egress_active", lambda tel: True)
    _write_config(home, {"telemetry": {"enabled": False, "beacon_enabled": False}})
    cc._telemetry(_ns("enable"))
    stored = _read_config(home)
    assert stored["telemetry"] == {"enabled": False, "beacon_enabled": True}
    # Recording stayed off, so nobody was told it is on.
    assert "privacy_ack_version" not in stored["dashboard"]
    assert "NOT turned on" in capsys.readouterr().out


def test_cli_enable_on_an_exporting_host_with_recording_already_on_says_so(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Recording and export both run there; "NOT turned on" would say neither does."""
    monkeypatch.setattr(beacon, "is_governance_pinned_off", lambda **kw: False)
    monkeypatch.setattr(provider, "otlp_egress_active", lambda tel: True)
    _write_config(home, {"telemetry": {"enabled": True, "beacon_enabled": False}})
    cc._telemetry(_ns("enable"))
    assert _read_config(home)["telemetry"]["enabled"] is True
    out = capsys.readouterr().out
    assert "already on" in out and "export metrics off the machine" in out
    assert "NOT turned on" not in out


def test_cli_enable_fails_closed_when_the_export_posture_is_unknown(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(tel: object) -> bool:
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(beacon, "is_governance_pinned_off", lambda **kw: False)
    monkeypatch.setattr(provider, "otlp_egress_active", _boom)
    _write_config(home, {"telemetry": {"enabled": False}})
    cc._telemetry(_ns("enable"))
    assert _read_config(home)["telemetry"]["enabled"] is False
