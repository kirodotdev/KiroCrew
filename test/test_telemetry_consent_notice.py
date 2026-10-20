"""Usage-data consent: DO_NOT_TRACK, the notice version, the export gate, the CLI.

These drive the real code paths against real config files under a temporary
data home: the loader, ``beacon.telemetry_permitted``, the recorder build in
``metrics.provider`` and ``kirocrew telemetry enable|disable``. Nothing here
sends data anywhere; the export tests stop at whether a reader would be built.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from kiro_crew import beacon
from kiro_crew import cli_commands as cc
from kiro_crew.config.loader import KiroCrewConfig, TelemetryConfig, written_value
from kiro_crew.config.sections import DashboardConfig
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


# -- load rules --


def test_heartbeat_off_and_enabled_unset_loads_as_off(home: Path) -> None:
    _write_config(home, {"telemetry": {"beacon_enabled": False}})
    assert KiroCrewConfig.load().telemetry.enabled is False


def test_written_enabled_wins_over_the_heartbeat_rule(home: Path) -> None:
    _write_config(home, {"telemetry": {"beacon_enabled": False, "enabled": True}})
    tel = KiroCrewConfig.load().telemetry
    assert (tel.enabled, tel.beacon_enabled) == (True, False)


def test_heartbeat_rule_sees_the_overlay(home: Path) -> None:
    """The overlay merges first, so a heartbeat-off overlay counts too."""
    _write_config(home, {"telemetry": {}})
    _write_config(home, {"telemetry": {"beacon_enabled": False}}, "config.local.json")
    assert KiroCrewConfig.load().telemetry.enabled is False


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


def test_written_value_reads_the_overlay_first(home: Path) -> None:
    _write_config(home, {"telemetry": {"enabled": False}})
    assert written_value("telemetry", "enabled") is False
    _write_config(home, {"telemetry": {"enabled": True}}, "config.local.json")
    assert written_value("telemetry", "enabled") is True
    assert written_value("telemetry", "missing") is None


def test_written_value_ignores_a_malformed_file(home: Path) -> None:
    (home / "config.json").write_text("{not json", encoding="utf-8")
    assert written_value("telemetry", "enabled") is None


# -- the export gate --


def _cfg(*, enabled: bool = True, ack: int = 0) -> KiroCrewConfig:
    return KiroCrewConfig(
        telemetry=TelemetryConfig(enabled=enabled),
        dashboard=DashboardConfig(privacy_ack_version=ack),
    )


def test_the_notice_ack_alone_does_not_permit_export(home: Path) -> None:
    """The notice says recorded data stays here; agreeing to it is not agreeing to export."""
    assert provider.outbound_consented(_cfg(ack=beacon.PRIVACY_NOTICE_VERSION)) is False


def test_export_allowed_for_an_explicit_env_opt_in(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIROCREW_TELEMETRY", "1")
    assert provider.outbound_consented(_cfg(enabled=False)) is True


def test_export_allowed_for_a_written_opt_in(home: Path) -> None:
    _write_config(home, {"telemetry": {"enabled": True}})
    assert provider.outbound_consented(_cfg(enabled=True)) is True


def test_export_refused_when_recording_is_not_chosen(home: Path) -> None:
    assert provider.outbound_consented(_cfg(enabled=False)) is False


def test_a_recording_default_alone_never_permits_export(home: Path) -> None:
    """Recording is on by default here, and nothing was written: no export."""
    assert (
        provider.outbound_consented(_cfg(enabled=True, ack=beacon.PRIVACY_NOTICE_VERSION)) is False
    )


def test_export_refused_when_the_config_cannot_be_read(home: Path) -> None:
    class Broken:
        @property
        def telemetry(self) -> object:
            raise RuntimeError("unreadable")

    assert provider.outbound_consented(Broken()) is False  # type: ignore[arg-type]


class _Dest:
    name = "test-collector"
    endpoint = "http://127.0.0.1:9/v1/metrics"
    signals = frozenset({"metrics"})
    session = None


def _build_with_destination(monkeypatch: pytest.MonkeyPatch, cfg: KiroCrewConfig) -> list:
    """Run the real recorder build with one destination; return the readers it asked for."""
    built: list = []
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(provider, "_otlp_destinations", lambda tel: (_Dest(),))
    monkeypatch.setattr(provider, "_build_otlp_reader", lambda dest, tel: built.append(dest.name))
    provider.reset_for_testing()
    try:
        result = provider._build_recorder()
        if result.provider is not None:
            result.provider.shutdown()
    finally:
        provider.reset_for_testing()
    return built


def test_build_attaches_no_export_reader_on_the_default_even_after_the_ack(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep on over a screen that says data stays local must not start an export."""
    cfg = _cfg(enabled=True, ack=beacon.PRIVACY_NOTICE_VERSION)
    assert _build_with_destination(monkeypatch, cfg) == []


def test_build_attaches_the_export_reader_after_a_written_opt_in(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(home, {"telemetry": {"enabled": True}})
    assert _build_with_destination(monkeypatch, _cfg(enabled=True)) == ["test-collector"]


def test_clearing_the_written_opt_in_drops_a_live_export_reader(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting ``enabled: true`` leaves every loaded value the same, so the config
    watcher cannot see it; the consent recheck has to notice export was withdrawn,
    and notice it again when the opt-in comes back."""
    attached: list = []
    cfg = _cfg(enabled=True)
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(provider, "_otlp_destinations", lambda tel: (_Dest(),))
    monkeypatch.setattr(
        provider, "_build_otlp_reader", lambda dest, tel: attached.append(dest.name)
    )
    _write_config(home, {"telemetry": {"enabled": True}})
    provider.reset_for_testing()
    try:
        provider.get_recorder()
        assert attached == ["test-collector"]
        assert provider._built_outbound is True

        _write_config(home, {"telemetry": {}})
        provider._consent_worker(provider._build_generation)
        assert provider._built_consent is True  # recording stays on by default
        assert provider._built_outbound is False
        assert attached == ["test-collector"]  # the rebuild attached no export reader

        _write_config(home, {"telemetry": {"enabled": True}})
        provider._consent_worker(provider._build_generation)
        assert provider._built_outbound is True
        assert attached == ["test-collector", "test-collector"]
    finally:
        provider.reset_for_testing()


def test_a_local_only_recheck_reads_no_export_state(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no destination the recheck must not pay for the config-file reads."""
    cfg = _cfg(enabled=True)
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(provider, "_otlp_destinations", lambda tel: ())
    asked: list = []
    monkeypatch.setattr(provider, "outbound_consented", lambda c: asked.append(c) or False)
    provider.reset_for_testing()
    try:
        provider.get_recorder()
        provider._consent_worker(provider._build_generation)
        assert asked == []
        assert provider._built_outbound is None
    finally:
        provider.reset_for_testing()


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


def test_cli_enable_on_an_exporting_host_with_only_the_default_on_says_nothing_is_exported(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Recording is on by default but export needs a written opt-in, so the
    message must not say metrics leave the machine."""
    monkeypatch.setattr(beacon, "is_governance_pinned_off", lambda **kw: False)
    monkeypatch.setattr(provider, "otlp_egress_active", lambda tel: True)
    _write_config(home, {"telemetry": {}})
    cc._telemetry(_ns("enable"))
    assert "enabled" not in _read_config(home)["telemetry"]
    out = capsys.readouterr().out
    assert "on by default" in out and "nothing is exported until" in out
    assert "export metrics off the machine" not in out


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


# -- a whole-document save must not invent an opt-in --


def test_save_on_a_fresh_install_does_not_write_the_recording_default(home: Path) -> None:
    """The boot default-config write would otherwise read back as a written opt-in."""
    KiroCrewConfig().save()
    stored = _read_config(home)
    assert "enabled" not in stored.get("telemetry", {})
    assert written_value("telemetry", "enabled") is None
    assert provider.outbound_consented(KiroCrewConfig.load()) is False


@pytest.mark.parametrize("choice", [True, False])
def test_save_keeps_a_recording_choice_already_on_disk(home: Path, choice: bool) -> None:
    _write_config(home, {"telemetry": {"enabled": choice}})
    KiroCrewConfig.load().save()
    assert _read_config(home)["telemetry"]["enabled"] is choice


# -- an install whose first boot saved the old defaults --


def _old_saved_telemetry() -> dict:
    """The section a pre-default-on build's whole-document save wrote."""
    from kiro_crew.config.sections_telemetry import _DEFAULT_BEACON_ENDPOINT

    return {
        "enabled": False,
        "local_dir": "",
        "export_interval_seconds": 60,
        "retention_days": 0,
        "max_total_mb": 0,
        "otlp_endpoint": "",
        "beacon_enabled": True,
        "beacon_endpoint": _DEFAULT_BEACON_ENDPOINT,
    }


def test_an_old_full_save_keeps_recording_off_and_its_caps_as_written(home: Path) -> None:
    """Recording stays off (it may be the user's off). The stored 0 / 0 caps are
    read as written: 0 is the documented "no limit", so they may be a choice, and
    reading them as 30 / 100 would delete metric shards the user kept."""
    _write_config(home, {"telemetry": _old_saved_telemetry()})
    tel = KiroCrewConfig.load().telemetry
    assert tel.enabled is False
    assert (tel.retention_days, tel.max_total_mb) == (0, 0)


@pytest.mark.parametrize(
    "doc",
    [
        {"enabled": True, "retention_days": 0, "max_total_mb": 0},
        {**_old_saved_telemetry(), "enabled": True},
    ],
)
def test_explicit_zero_caps_stay_authoritative(home: Path, doc: dict) -> None:
    _write_config(home, {"telemetry": doc})
    tel = KiroCrewConfig.load().telemetry
    assert (tel.retention_days, tel.max_total_mb) == (0, 0)


def test_stored_zero_caps_are_reported_not_adopted(home: Path) -> None:
    """The superseded-default report names both caps, and nothing rewrites them."""
    from kiro_crew.config import superseded_defaults as sd

    doc = {"telemetry": _old_saved_telemetry()}
    _write_config(home, doc)
    KiroCrewConfig.load()
    drifted = {e.dotted_key: e for e in sd.superseded_default_drift(doc, acked={})}
    for key in ("telemetry.retention_days", "telemetry.max_total_mb"):
        assert key in drifted and not drifted[key].auto_adopt
        assert "deletes" in (drifted[key].note or "")
    assert _read_config(home)["telemetry"]["retention_days"] == 0
    assert _read_config(home)["telemetry"]["max_total_mb"] == 0


def test_unset_caps_read_the_new_defaults(home: Path) -> None:
    _write_config(home, {"telemetry": {"enabled": True}})
    tel = KiroCrewConfig.load().telemetry
    assert (tel.retention_days, tel.max_total_mb) == (30, 100)


# -- the old heartbeat env opt-out also holds the default recording off --


def test_heartbeat_env_opt_out_keeps_default_recording_off(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIROCREW_TELEMETRY_DISABLED", "1")
    _write_config(home, {})
    assert provider.env_pin() is False
    assert provider.pin_env_var() == "KIROCREW_TELEMETRY_DISABLED"
    assert provider._consent_enabled(KiroCrewConfig.load().telemetry) is False


def test_the_heartbeat_env_hold_is_a_soft_pin_the_switch_can_lift(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Settings must not grey out a switch whose write is exactly what lifts the
    hold, and ``status`` must not say the config value has no effect."""
    from kiro_crew.dashboard.handlers import telemetry as telemetry_handlers

    monkeypatch.setenv("KIROCREW_TELEMETRY_DISABLED", "1")
    _write_config(home, {})
    assert provider.env_pin() is False
    assert provider.env_pin_is_soft() is True
    state = telemetry_handlers._telemetry_cfg()
    assert (state.enabled, state.env_pinned) == (False, False)
    cc._telemetry(_ns("status"))
    out = capsys.readouterr().out
    assert "Held off by: KIROCREW_TELEMETRY_DISABLED" in out
    assert "has no effect" not in out


@pytest.mark.parametrize(("name", "value"), [("DO_NOT_TRACK", "1"), ("KIROCREW_TELEMETRY", "0")])
def test_the_other_env_vars_stay_hard_pins(
    home: Path, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    from kiro_crew.dashboard.handlers import telemetry as telemetry_handlers

    monkeypatch.setenv("KIROCREW_TELEMETRY_DISABLED", "1")
    monkeypatch.setenv(name, value)
    _write_config(home, {})
    assert provider.env_pin_is_soft() is False
    assert telemetry_handlers._telemetry_cfg().env_pinned is True


def test_a_written_recording_choice_still_wins_over_the_heartbeat_env_opt_out(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIROCREW_TELEMETRY_DISABLED", "1")
    _write_config(home, {"telemetry": {"enabled": True}})
    assert provider.env_pin() is None
    assert provider._consent_enabled(KiroCrewConfig.load().telemetry) is True
