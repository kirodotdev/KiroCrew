"""Coverage for ``kirocrew computer doctor``, ``help`` and ``main``."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.computer_use import cli as cu_cli
from kiro_crew.computer_use import enable_state as cu_enable_state
from kiro_crew.computer_use import index as cu_index
from kiro_crew.computer_use import service as cu_service
from kiro_crew.computer_use.types import PERMISSION_UNKNOWN, STATE_KEY_ENABLED


@pytest.fixture(autouse=True)
def fresh_singletons():
    cu_service.reset_shared_service()
    cu_index.reset_shared_index()
    yield
    cu_service.reset_shared_service()
    cu_index.reset_shared_index()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def enabled(home: Path) -> None:
    (home / "computer_use.json").write_text(
        json.dumps({"enabled": True, "allow_cli_diagnostics": True}), encoding="utf-8"
    )


def _report(**overrides) -> dict:
    report = {
        "platform": "linux",
        "supported": True,
        "reason": "",
        "enabled": True,
        "state_file": "/state/computer_use.json",
        "state_key": STATE_KEY_ENABLED,
        "permissions": {
            "accessibility": PERMISSION_UNKNOWN,
            "screen_recording": PERMISSION_UNKNOWN,
            "responsible_hint": "",
        },
        "blocked_apps": [],
        "errors": [],
    }
    report.update(overrides)
    return report


def _render(monkeypatch: pytest.MonkeyPatch, **overrides) -> None:
    monkeypatch.setattr(cu_cli, "build_doctor_report", lambda: _report(**overrides))


class TestBuildDoctorReport:
    def test_a_disabled_install_reports_the_documented_shape(self, home):
        report = cu_cli.build_doctor_report()
        assert report["enabled"] is False
        assert report["state_key"] == STATE_KEY_ENABLED
        assert report["state_file"] == str(cu_enable_state.computer_use_state_path())
        assert report["errors"] == []

    def test_the_primary_enable_is_reported_when_set(self, enabled):
        assert cu_cli.build_doctor_report()["enabled"] is True

    def test_a_state_read_failure_is_recorded_and_not_raised(self, home, monkeypatch):
        def broken() -> bool:
            raise OSError("disk gone")

        monkeypatch.setattr(cu_enable_state, "is_enabled", broken)
        report = cu_cli.build_doctor_report()
        assert report["enabled"] is False
        assert any("state file" in m and "disk gone" in m for m in report["errors"])

    def test_an_unresolvable_state_path_becomes_a_placeholder(self, home, monkeypatch):
        def unresolved():
            raise RuntimeError("no home")

        monkeypatch.setattr(cu_enable_state, "computer_use_state_path", unresolved)
        assert cu_cli.build_doctor_report()["state_file"] == "(unresolved)"

    def test_a_driver_that_will_not_construct_is_named_not_crashed_on(self, home, monkeypatch):
        def no_driver():
            raise RuntimeError("no framework")

        monkeypatch.setattr(cu_service, "get_shared_service", no_driver)
        report = cu_cli.build_doctor_report()
        assert report["supported"] is False
        assert report["reason"] == "no framework"
        assert report["permissions"]["accessibility"] == PERMISSION_UNKNOWN
        assert "could not be probed: no framework" in report["errors"][0]

    def test_the_driver_status_and_probe_fill_the_report(self, home, monkeypatch):
        fake = SimpleNamespace(
            status=lambda: SimpleNamespace(supported=True, reason="", platform_id="macos"),
            probe_permissions=lambda: SimpleNamespace(
                accessibility="granted",
                screen_recording="missing",
                responsible_hint="Kiro Crew.app",
            ),
        )
        monkeypatch.setattr(cu_service, "get_shared_service", lambda: fake)
        report = cu_cli.build_doctor_report()
        assert report["platform"] == "macos"
        assert report["supported"] is True
        assert report["permissions"]["screen_recording"] == "missing"
        assert report["permissions"]["responsible_hint"] == "Kiro Crew.app"

    def test_blocked_targets_are_category_and_reason_rows(self, home):
        blocked = cu_cli.build_doctor_report()["blocked_apps"]
        assert blocked
        assert all("category" in entry and "reason" in entry for entry in blocked)


class TestDoctorCommand:
    def test_json_prints_the_real_report_and_returns_without_exiting(self, home, capsys):
        cu_cli.run_computer(["doctor", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["state_key"] == STATE_KEY_ENABLED
        assert payload["enabled"] is False

    def test_a_supported_enabled_report_exits_zero_without_permission_rows(
        self, monkeypatch, capsys
    ):
        _render(monkeypatch)
        with pytest.raises(SystemExit) as excinfo:
            cu_cli.run_computer(["doctor"])
        out = capsys.readouterr().out
        assert excinfo.value.code == cu_cli._EXIT_OK
        assert "Supported:     yes" in out and "Enabled:       yes" in out
        assert "Accessibility" not in out

    def test_an_unsupported_disabled_report_gives_the_reason_and_exits_one(
        self, monkeypatch, capsys
    ):
        _render(monkeypatch, supported=False, reason="no display", enabled=False)
        with pytest.raises(SystemExit) as excinfo:
            cu_cli.run_computer(["doctor"])
        out = capsys.readouterr().out
        assert excinfo.value.code == cu_cli._EXIT_PROBLEM
        assert "Supported:     no — no display" in out
        assert "Enabled:       no (Settings -> Computer Use)" in out

    def test_macos_prints_the_advisory_permission_rows(self, monkeypatch, capsys):
        permissions = {
            "accessibility": "granted",
            "screen_recording": "missing",
            "responsible_hint": "Kiro Crew.app",
        }
        _render(monkeypatch, platform="macos", permissions=permissions)
        with pytest.raises(SystemExit):
            cu_cli.run_computer(["doctor"])
        out = capsys.readouterr().out
        assert "Accessibility: granted" in out
        assert "Screen record: missing" in out
        assert "Grant to:      Kiro Crew.app" in out
        assert "Note: these permission readings are advisory." in out

    def test_blocked_targets_are_printed_under_their_heading(self, monkeypatch, capsys):
        _render(
            monkeypatch, blocked_apps=[{"category": "password managers", "reason": "holds secrets"}]
        )
        with pytest.raises(SystemExit):
            cu_cli.run_computer(["doctor"])
        out = capsys.readouterr().out
        assert "Always-blocked targets (built in, not configurable):" in out
        assert "  - password managers: holds secrets" in out

    def test_report_errors_go_to_stderr_and_exit_one(self, monkeypatch, capsys):
        _render(monkeypatch, errors=["the probe broke"])
        with pytest.raises(SystemExit) as excinfo:
            cu_cli.run_computer(["doctor"])
        assert excinfo.value.code == cu_cli._EXIT_PROBLEM
        assert "Problem: the probe broke" in capsys.readouterr().err


class TestHelpAndEntryPoint:
    @pytest.mark.parametrize("flag", ["-h", "--help", "help"])
    def test_help_spellings_print_the_usage(self, flag, capsys):
        cu_cli.run_computer([flag])
        assert "Commands:" in capsys.readouterr().out

    def test_main_reads_the_process_arguments(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["kirocrew", "help"])
        cu_cli.main()
        assert "Commands:" in capsys.readouterr().out
