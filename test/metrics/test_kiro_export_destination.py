"""The Kiro telemetry destination the public build ships (``metrics/kiro_export.py``).

In the public, non-LTS build it exports under the recording default, once the
notice that names it (version 3) was shown. What
already worked and must keep working, pinned here:

* An operator's own entries still need the written opt-in, and keep their place
  first (``TestWhoSends``, ``TestProvider``).
* Every off switch builds no reader (``TestWhoSends``).
* The Settings switch and ``kirocrew telemetry enable`` still turn recording on;
  only an operator entry makes them refuse (``TestSwitches``).
* An LTS build, CI runners and non-default homes do not list it; a policy
  ``otlp_hosts`` list can refuse its host (``TestProvider``).

The last test drives the real public provider and the real build against a
loopback receiver standing in for the endpoint.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import http.server
import io
import json
import socketserver
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kiro_crew import beacon
from kiro_crew.config.loader import KiroCrewConfig, TelemetryConfig
from kiro_crew.metrics import kiro_export
from kiro_crew.metrics import provider as pm
from kiro_crew.metrics.product_metrics import PRODUCT_METRICS
from kiro_crew.platform.defaults import DefaultTelemetryProvider

OPS = "https://collector.example.com/v1/metrics"

_ENV_VARS = (
    "KIROCREW_TELEMETRY",
    "KIROCREW_TELEMETRY_DISABLED",
    "DO_NOT_TRACK",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_EXPORTER_OTLP_METRICS_HEADERS",
    *beacon._CI_ENV_VARS,
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private data home, no telemetry or CI env vars, the destination on."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(kiro_export, "_suppressed_for_tests", False)
    monkeypatch.setattr(kiro_export, "_session", None)
    monkeypatch.setattr(pm, "_telemetry_provider", DefaultTelemetryProvider)
    monkeypatch.setattr(pm, "_policy_ceiling", lambda: None)
    _write_config(tmp_path, {})
    pm.reset_for_testing()
    yield tmp_path
    pm.reset_for_testing()


def _write_config(
    home: Path,
    telemetry: dict,
    ack: int = kiro_export.NOTICE_VERSION,
    kiro_ack: "int | None" = None,
) -> None:
    """Write *telemetry*, with the privacy notice acknowledged at version *ack*.

    *kiro_ack* is the Kiro-specific acknowledgement, the same as *ack* unless given.
    """
    dashboard = {
        "privacy_ack_version": ack,
        "kiro_telemetry_ack_version": ack if kiro_ack is None else kiro_ack,
    }
    doc = {"telemetry": telemetry, "dashboard": dashboard}
    (home / "config.json").write_text(json.dumps(doc), encoding="utf-8")


def _built(monkeypatch, home: Path) -> "tuple[bool, list[str]]":
    """Run the real build; return (recording, names of the readers it built)."""
    built: list = []
    monkeypatch.setattr(pm, "_build_otlp_reader", lambda d, c: built.append(d.name) or None)
    with patch.object(pm, "_default_metrics_dir", return_value=home / "metrics"):
        with patch("kiro_crew.sel.sel"):
            recording = pm.get_recorder().enabled
    return recording, built


class TestWhoSends:
    """One row per line of the table in docs/guides/telemetry-otlp-export.md."""

    def test_a_new_install_exports_once_the_notice_was_shown(self, home, monkeypatch):
        assert _built(monkeypatch, home) == (True, [kiro_export.DESTINATION_NAME])

    def test_a_notice_ack_that_did_not_name_it_does_not_count(self, home, monkeypatch):
        """Acknowledged under a non-default home, where the chapter never named it."""
        _write_config(home, {}, ack=kiro_export.NOTICE_VERSION, kiro_ack=0)
        assert _built(monkeypatch, home) == (True, [])

    def test_nothing_is_sent_before_the_notice(self, home, monkeypatch):
        _write_config(home, {}, ack=kiro_export.NOTICE_VERSION - 1)
        assert _built(monkeypatch, home) == (True, [])

    def test_the_printed_headless_kiro_notice_counts(self, home, monkeypatch):
        _write_config(home, {}, ack=0)
        (home / "kiro_telemetry_notice_shown").write_text(f"{kiro_export.NOTICE_VERSION}\n")
        assert _built(monkeypatch, home) == (True, [kiro_export.DESTINATION_NAME])

    @pytest.mark.parametrize("stamp", ["1\n", f"{kiro_export.NOTICE_VERSION}\n"])
    def test_the_generic_cli_stamp_does_not_count(self, home, monkeypatch, stamp):
        """It is also written by a dashboard start and by the local-only line."""
        _write_config(home, {}, ack=0)
        (home / "usage_notice_shown").write_text(stamp)
        assert _built(monkeypatch, home) == (True, [])

    def test_an_upgrade_with_the_heartbeat_on_exports(self, home, monkeypatch):
        _write_config(home, {"beacon_enabled": True})
        assert _built(monkeypatch, home) == (True, [kiro_export.DESTINATION_NAME])

    def test_an_upgrade_with_the_heartbeat_off_records_nothing(self, home, monkeypatch):
        _write_config(home, {"beacon_enabled": False})
        assert _built(monkeypatch, home) == (False, [])

    def test_a_written_true_under_an_older_notice_sends_nothing(self, home, monkeypatch):
        """Settings wrote it when the switch said metrics stay on this machine."""
        _write_config(home, {"enabled": True}, ack=kiro_export.NOTICE_VERSION - 1)
        assert _built(monkeypatch, home) == (True, [])

    def test_a_written_true_exports(self, home, monkeypatch):
        _write_config(home, {"enabled": True})
        assert _built(monkeypatch, home) == (True, [kiro_export.DESTINATION_NAME])

    def test_the_env_opt_in_exports(self, home, monkeypatch):
        monkeypatch.setenv("KIROCREW_TELEMETRY", "1")
        assert _built(monkeypatch, home) == (True, [kiro_export.DESTINATION_NAME])

    @pytest.mark.parametrize("value", [False, "true", 1, None])
    def test_a_written_false_or_non_bool_records_nothing(self, home, monkeypatch, value):
        _write_config(home, {"enabled": value})
        assert _built(monkeypatch, home) == (False, [])

    @pytest.mark.parametrize(
        ("name", "value"), [("DO_NOT_TRACK", "1"), ("KIROCREW_TELEMETRY", "0")]
    )
    def test_an_env_opt_out_wins(self, home, monkeypatch, name, value):
        _write_config(home, {"enabled": True})
        monkeypatch.setenv(name, value)
        assert _built(monkeypatch, home) == (False, [])

    def test_ci_records_but_does_not_list_or_export_it(self, home, monkeypatch):
        monkeypatch.setenv("CI", "true")
        assert _built(monkeypatch, home) == (True, [])

    def test_an_operator_entry_still_needs_the_written_opt_in(self, home, monkeypatch):
        _write_config(home, {"otlp_endpoints": [{"name": "ops", "endpoint": OPS}]})
        assert _built(monkeypatch, home) == (True, [kiro_export.DESTINATION_NAME])

    def test_with_the_written_opt_in_the_operator_entry_is_built_first(self, home, monkeypatch):
        _write_config(home, {"enabled": True, "otlp_endpoints": [{"name": "ops", "endpoint": OPS}]})
        assert _built(monkeypatch, home) == (True, ["ops", kiro_export.DESTINATION_NAME])

    def test_the_fingerprint_reads_the_snapshot_the_build_used(self, home):
        """An acknowledgement written mid-build must still move the next recheck."""
        _write_config(home, {}, ack=kiro_export.NOTICE_VERSION - 1)
        snapshot = KiroCrewConfig.load()
        before = pm._endpoints_key(snapshot.telemetry, full_cfg=snapshot)
        _write_config(home, {})
        assert pm._endpoints_key(snapshot.telemetry, full_cfg=snapshot) == before
        fresh = KiroCrewConfig.load()
        assert pm._endpoints_key(fresh.telemetry, full_cfg=fresh) != before

    def test_showing_the_notice_moves_the_rebuild_fingerprint(self, home):
        _write_config(home, {}, ack=kiro_export.NOTICE_VERSION - 1)
        before = pm._endpoints_key(TelemetryConfig())
        _write_config(home, {})
        assert pm._endpoints_key(TelemetryConfig()) != before


class TestSwitches:
    def test_the_built_in_destination_does_not_refuse_the_enable(self, home):
        assert pm.otlp_egress_active(TelemetryConfig()) is True
        assert kiro_export.egress_needs_written_opt_in(TelemetryConfig()) is False

    def test_an_operator_entry_still_refuses_it(self, home):
        cfg = TelemetryConfig(otlp_endpoints=[{"name": "ops", "endpoint": OPS}])
        assert kiro_export.egress_needs_written_opt_in(cfg) is True

    def test_the_collection_status_reports_it(self, home):
        from kiro_crew.dashboard.handlers.telemetry import _telemetry_cfg

        state = _telemetry_cfg()
        assert state.otlp_configured is True
        assert state.enable_refused is False
        # The operator opt-in; the built-in destination is named by its row.
        assert state.export_permitted is False
        assert state.otlp_destinations == (
            {"name": "kiro-telemetry", "source": "provider", "locked": True},
        )

    def test_a_kept_recorder_discloses_its_kiro_export(self, home, monkeypatch):
        """Consent unreadable keeps the live recorder; its export must still show."""
        monkeypatch.setattr(pm, "_built_consent", True)
        monkeypatch.setattr(pm, "_built_outbound", False)
        monkeypatch.setattr(kiro_export, "_last_build_kept", True)
        assert pm.live_recorder_state() == (True, True)
        monkeypatch.setattr(kiro_export, "_last_build_kept", False)
        assert pm.live_recorder_state() == (True, False)

    def test_a_build_records_whether_it_kept_it(self, home, monkeypatch):
        _built(monkeypatch, home)
        assert kiro_export.last_build_kept() is True
        pm.reset_for_testing()
        _write_config(home, {}, ack=0)
        _built(monkeypatch, home)
        assert kiro_export.last_build_kept() is False

    @pytest.mark.parametrize("terminal", [True, False])
    def test_cli_enable_turns_recording_on_and_names_it(self, home, monkeypatch, capsys, terminal):
        """The Kiro acknowledgement is written only after its line reached a terminal."""
        import argparse
        import sys

        from kiro_crew import cli_commands as cc

        monkeypatch.setattr(beacon, "is_governance_pinned_off", lambda **kw: False)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: terminal)
        _write_config(home, {"enabled": False}, ack=0)
        cc._telemetry(argparse.Namespace(telemetry_action="enable"))
        stored = json.loads((home / "config.json").read_text(encoding="utf-8"))
        assert stored["telemetry"]["enabled"] is True
        assert stored["dashboard"]["privacy_ack_version"] == beacon.PRIVACY_NOTICE_VERSION
        assert stored["dashboard"]["kiro_telemetry_ack_version"] == (
            beacon.PRIVACY_NOTICE_VERSION if terminal else 0
        )
        assert "sent to Kiro telemetry" in capsys.readouterr().out

    def test_cli_enable_beside_a_waiting_collector_names_the_kiro_export(
        self, home, monkeypatch, capsys
    ):
        import argparse

        from kiro_crew import cli_commands as cc

        monkeypatch.setattr(beacon, "is_governance_pinned_off", lambda **kw: False)
        _write_config(home, {"otlp_endpoints": [{"name": "ops", "endpoint": OPS}]})
        cc._telemetry(argparse.Namespace(telemetry_action="enable"))
        out = capsys.readouterr().out
        assert "nothing is exported until" in out
        assert "Product usage metrics are sent to Kiro telemetry" in out


class _Terminal(io.StringIO):
    """A stream that reports itself as a terminal, as an interactive stderr does."""

    def isatty(self) -> bool:
        return True


class TestNotice:
    def test_the_current_notice_names_this_destination(self):
        assert beacon.PRIVACY_NOTICE_VERSION >= kiro_export.NOTICE_VERSION

    def test_a_headless_start_names_it_and_stamps_it(self, home):
        from kiro_crew.metrics import notice

        _write_config(home, {}, ack=0)
        out = _Terminal()
        assert notice.maybe_print_notice(stream=out, headless=True) is True
        assert out.getvalue().strip() == notice.NOTICE_TEXT_KIRO
        assert notice.stamped_version() == beacon.PRIVACY_NOTICE_VERSION
        assert notice.stamped_version(notice.KIRO_NOTICE_STAMP_FILE) == kiro_export.NOTICE_VERSION
        assert kiro_export.notice_shown() is True
        assert notice.maybe_print_notice(stream=io.StringIO(), headless=True) is False

    def test_a_headless_line_into_a_log_does_not_count_as_told(self, home):
        """``docker logs``, a journal or a redirected file: nobody saw it."""
        from kiro_crew.metrics import notice

        _write_config(home, {}, ack=0)
        assert notice.maybe_print_notice(stream=io.StringIO(), headless=True) is True
        assert notice.stamped_version(notice.KIRO_NOTICE_STAMP_FILE) == 0
        assert kiro_export.notice_shown() is False

    def test_a_line_into_a_log_is_not_repeated_on_every_start(self, home):
        """A cron job or a service prints it once, not at every run."""
        from kiro_crew.metrics import notice

        _write_config(home, {}, ack=0)
        assert notice.maybe_print_notice(stream=io.StringIO(), headless=True) is True
        assert notice.maybe_print_notice(stream=io.StringIO(), headless=True) is False
        # A person at a terminal later still gets the Kiro line.
        out = _Terminal()
        assert notice.maybe_print_notice(stream=out, headless=True) is True
        assert out.getvalue().strip() == notice.NOTICE_TEXT_KIRO
        assert kiro_export.notice_shown() is True

    def test_a_generic_ack_still_owes_the_kiro_line(self, home):
        """A redirected `telemetry enable` wrote privacy_ack_version but named nothing."""
        from kiro_crew.metrics import notice

        _write_config(home, {}, ack=beacon.PRIVACY_NOTICE_VERSION, kiro_ack=0)
        out = _Terminal()
        assert notice.maybe_print_notice(stream=out, headless=True) is True
        assert out.getvalue().strip() == notice.NOTICE_TEXT_KIRO
        assert kiro_export.notice_shown() is True

    def test_the_chapter_shows_again_while_kiro_telemetry_is_owed(self, home):
        from kiro_crew.dashboard.handlers import core as core_handlers

        _write_config(home, {}, ack=beacon.PRIVACY_NOTICE_VERSION, kiro_ack=0)
        assert core_handlers._theme_payload(KiroCrewConfig.load())["privacy_ack_version"] == 0
        _write_config(home, {})
        assert (
            core_handlers._theme_payload(KiroCrewConfig.load())["privacy_ack_version"]
            == beacon.PRIVACY_NOTICE_VERSION
        )

    def test_a_provider_that_does_not_list_it_does_not_hold_the_chapter(self, home, monkeypatch):
        """Keep on records only privacy_ack_version there, so a hold would never clear."""
        from kiro_crew.dashboard.handlers import core as core_handlers
        from kiro_crew.platform.interfaces import OtlpDestination

        own = OtlpDestination(
            name="edition-collector", endpoint=OPS, signals=frozenset({"metrics"})
        )

        class _EditionProvider(DefaultTelemetryProvider):
            def otlp_destinations(self, cfg):
                return (own,)

        monkeypatch.setattr(pm, "_telemetry_provider", _EditionProvider)
        _write_config(home, {}, ack=beacon.PRIVACY_NOTICE_VERSION, kiro_ack=0)
        cfg = KiroCrewConfig.load()
        assert kiro_export.destinations() and not kiro_export.notice_shown(cfg)
        assert not kiro_export.listed(pm.otlp_egress_listing(cfg.telemetry))
        assert (
            core_handlers._theme_payload(cfg)["privacy_ack_version"]
            == beacon.PRIVACY_NOTICE_VERSION
        )

    def test_an_unreadable_listing_does_not_hold_the_chapter(self, home, monkeypatch):
        from kiro_crew.dashboard.handlers import core as core_handlers

        def boom(cfg):
            raise RuntimeError("provider failed")

        monkeypatch.setattr(pm, "otlp_egress_listing", boom)
        _write_config(home, {}, ack=beacon.PRIVACY_NOTICE_VERSION, kiro_ack=0)
        assert (
            core_handlers._theme_payload(KiroCrewConfig.load())["privacy_ack_version"]
            == beacon.PRIVACY_NOTICE_VERSION
        )

    def test_a_failed_print_does_not_count_as_told(self, home):
        from kiro_crew.metrics import notice

        class _Closed:
            def write(self, _text):
                raise BrokenPipeError

            def flush(self):
                raise BrokenPipeError

            def isatty(self):
                return True

        _write_config(home, {}, ack=0)
        notice.maybe_print_notice(stream=_Closed(), headless=True)
        assert notice.stamped_version(notice.KIRO_NOTICE_STAMP_FILE) == 0
        assert kiro_export.notice_shown() is False

    def test_a_headless_start_after_a_dashboard_start_still_tells(self, home):
        from kiro_crew.metrics import notice

        _write_config(home, {}, ack=0)
        assert notice.maybe_print_notice(stream=io.StringIO()) is True
        out = _Terminal()
        assert notice.maybe_print_notice(stream=out, headless=True) is True
        assert out.getvalue().strip() == notice.NOTICE_TEXT_KIRO
        assert kiro_export.notice_shown() is True

    def test_a_dashboard_start_does_not_count_as_told(self, home):
        """The stderr line of a desktop or dashboard gateway goes to a log."""
        from kiro_crew.metrics import notice

        _write_config(home, {}, ack=0)
        assert notice.maybe_print_notice(stream=io.StringIO()) is True
        assert notice.stamped_version(notice.KIRO_NOTICE_STAMP_FILE) == 0
        assert kiro_export.notice_shown() is False

    def test_the_local_only_line_does_not_count_as_told(self, home, monkeypatch):
        from kiro_crew.metrics import notice

        _write_config(home, {}, ack=0)
        monkeypatch.setenv("CI", "true")
        out = io.StringIO()
        assert notice.maybe_print_notice(stream=out, headless=True) is True
        assert out.getvalue().strip() == notice.NOTICE_TEXT
        assert notice.stamped_version(notice.KIRO_NOTICE_STAMP_FILE) == 0

    def test_an_older_stamp_prints_again(self, home):
        from kiro_crew.metrics import notice

        _write_config(home, {}, ack=0)
        (home / notice.NOTICE_STAMP_FILE).write_text("1\n")
        assert notice.maybe_print_notice(stream=io.StringIO()) is True

    def test_an_unreadable_config_is_not_shown(self, home, monkeypatch):
        def boom(cls):
            raise OSError("unreadable")

        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(boom))
        assert kiro_export.notice_shown() is False


class TestOffOffer:
    """Off only by a written false older builds put in every new config."""

    @pytest.fixture(autouse=True)
    def _default_home(self, _floor_monkeypatch):
        # The offer is asked only on the default data home, and the private test home
        # is not one, so these tests fake it. The shared ``home`` fixture does not:
        # ``destinations()`` lists the destination on any home.
        _floor_monkeypatch.setattr(beacon, "is_default_home", lambda: True)

    def _old_default(self, home, **telemetry):
        _write_config(home, {"enabled": False, **telemetry}, ack=2, kiro_ack=0)

    def test_it_is_due_and_changes_nothing(self, home, monkeypatch):
        from kiro_crew.dashboard.handlers import core as core_handlers

        self._old_default(home)
        cfg = KiroCrewConfig.load()
        assert kiro_export.off_offer_due(cfg) is True
        # The chapter shows: the boot payload reports the stored, older version.
        assert core_handlers._theme_payload(cfg)["privacy_ack_version"] < kiro_export.NOTICE_VERSION
        assert _built(monkeypatch, home) == (False, [])

    def test_the_collection_status_reports_it(self, home):
        from aiohttp.test_utils import make_mocked_request

        from kiro_crew.dashboard.handlers import telemetry as tel

        self._old_default(home)
        resp = asyncio.run(tel.api_collection_status(make_mocked_request("GET", "/x")))
        assert json.loads(resp.body)["off_offer"] is True

    @pytest.mark.parametrize(
        "case",
        [
            "dnt",
            "env",
            "heartbeat_env",
            "heartbeat_off",
            "acked",
            "unset",
            "lts",
            "ci",
            "home",
            "collector",
            "overlay",
            "policy",
        ],
    )
    def test_it_is_not_due(self, home, monkeypatch, case):
        from kiro_crew.dashboard.handlers import core as core_handlers

        self._old_default(home)
        if case == "dnt":
            monkeypatch.setenv("DO_NOT_TRACK", "1")
        elif case == "env":
            monkeypatch.setenv("KIROCREW_TELEMETRY", "0")
        elif case == "heartbeat_env":
            monkeypatch.setenv("KIROCREW_TELEMETRY_DISABLED", "1")
        elif case == "heartbeat_off":
            self._old_default(home, beacon_enabled=False)
        elif case == "acked":
            _write_config(home, {"enabled": False}, ack=kiro_export.NOTICE_VERSION, kiro_ack=0)
        elif case == "unset":
            _write_config(home, {}, ack=2, kiro_ack=0)
            monkeypatch.setattr(pm, "_consent_enabled", lambda tel: False)
        elif case == "lts":
            monkeypatch.setattr("kiro_crew.edition.EDITION", "lts")
        elif case == "ci":
            monkeypatch.setenv("CI", "true")
        elif case == "home":
            # Dev homes still export (destinations() stays listed); they are not asked.
            listed_rows = kiro_export.destinations()
            assert listed_rows
            monkeypatch.setattr(kiro_export, "destinations", lambda: listed_rows)
            monkeypatch.setattr(beacon, "is_default_home", lambda: False)
        elif case == "collector":
            self._old_default(home, otlp_endpoints=[{"name": "ops", "endpoint": OPS}])
        elif case == "overlay":
            (home / "config.local.json").write_text(json.dumps({"telemetry": {"enabled": False}}))
        else:
            monkeypatch.setattr(beacon, "is_governance_pinned_off", lambda **kw: True)
        cfg = KiroCrewConfig.load()
        assert kiro_export.off_offer_due(cfg) is False
        if case not in ("unset", "lts"):
            assert core_handlers._theme_payload(cfg)["privacy_ack_version"] >= 3

    def test_turn_on_records_and_exports(self, home, monkeypatch):
        _write_config(home, {"enabled": True}, ack=kiro_export.NOTICE_VERSION)
        assert kiro_export.off_offer_due() is False
        assert _built(monkeypatch, home) == (True, [kiro_export.DESTINATION_NAME])

    def test_keep_off_is_not_asked_again_and_sends_nothing(self, home, monkeypatch):
        _write_config(home, {"enabled": False}, ack=kiro_export.NOTICE_VERSION, kiro_ack=0)
        assert kiro_export.off_offer_due() is False
        assert _built(monkeypatch, home) == (False, [])

    def test_a_headless_terminal_start_prints_the_line_once(self, home):
        from kiro_crew.metrics import notice

        self._old_default(home)
        out = _Terminal()
        assert notice.maybe_print_notice(stream=out, headless=True) is True
        assert out.getvalue().strip() == notice.OFFER_TEXT
        assert "kirocrew telemetry enable" in notice.OFFER_TEXT
        assert notice.maybe_print_notice(stream=_Terminal(), headless=True) is False
        stored = json.loads((home / "config.json").read_text(encoding="utf-8"))
        assert stored["telemetry"]["enabled"] is False

    @pytest.mark.parametrize("headless", [True, False])
    def test_no_line_into_a_log_or_on_a_dashboard_start(self, home, headless):
        from kiro_crew.metrics import notice

        self._old_default(home)
        stream = io.StringIO() if headless else _Terminal()
        assert notice.maybe_print_notice(stream=stream, headless=headless) is False
        assert stream.getvalue() == ""


class TestProvider:
    def test_it_follows_the_operator_entries_and_is_narrowed(self, home):
        dests = DefaultTelemetryProvider().otlp_destinations(TelemetryConfig(otlp_endpoint=OPS))
        assert [d.name for d in dests] == ["telemetry.otlp_endpoint", "kiro-telemetry"]
        kiro = dests[1]
        assert kiro.endpoint == kiro_export.ENDPOINT
        assert kiro.allow == PRODUCT_METRICS
        # No install id in this fresh home yet, so no machineId (TestIdentity).
        assert dict(kiro.point_attributes) == {"ClientType": "kiro-crew"}
        assert "service.instance.id" not in kiro.resource_allow
        assert "os.type" not in kiro.resource_allow
        assert not any(key.startswith("host.") for key in kiro.resource_allow)
        assert kiro.process_env_credentials is False
        assert kiro.headers_env == {}

    def test_an_operator_entry_with_its_name_is_not_it(self, home):
        cfg = TelemetryConfig(otlp_endpoints=[{"name": "kiro-telemetry", "endpoint": OPS}])
        rows = pm.otlp_egress_listing(cfg)
        assert [(r.destination.name, kiro_export.is_builtin(r.destination)) for r in rows] == [
            ("kiro-telemetry", False),
            ("kiro-telemetry", True),
        ]

    def test_the_disclosure_names_it_as_included_by_the_build(self, home):
        rows = pm.otlp_egress_listing(TelemetryConfig())
        assert [(r.destination.name, r.source, r.locked) for r in rows] == [
            ("kiro-telemetry", "provider", True)
        ]

    def test_telemetry_status_names_it(self, home):
        from kiro_crew.cli_commands import _format_recording_status

        text = _format_recording_status(KiroCrewConfig.load())
        assert "Collectors:  kiro-telemetry (included by this build)" in text
        assert "also sent to the metrics destination" in text

    @pytest.mark.parametrize("hidden_by", ["ci", "lts", "tests"])
    def test_it_is_not_listed_where_it_must_not_send(self, home, monkeypatch, hidden_by):
        if hidden_by == "ci":
            monkeypatch.setenv("GITHUB_ACTIONS", "true")
        elif hidden_by == "lts":
            monkeypatch.setattr("kiro_crew.edition.EDITION", "lts")
        else:
            monkeypatch.setattr(kiro_export, "_suppressed_for_tests", True)
        assert kiro_export.destinations() == ()
        assert DefaultTelemetryProvider().otlp_destinations(TelemetryConfig()) == ()

    def test_a_non_default_home_gets_it(self, home):
        """A dev home, a pod or a worktree preview exports like any other home."""
        assert beacon.is_default_home() is False
        assert [d.name for d in kiro_export.destinations()] == ["kiro-telemetry"]

    def test_an_lts_policy_collector_is_the_only_destination(self, home, monkeypatch):
        from kiro_crew import edition_locks
        from kiro_crew.platform import context as pc
        from kiro_crew.platform.governance import parse_policy

        admin = "https://otel.corp.example/v1/metrics"
        monkeypatch.setattr("kiro_crew.edition.EDITION", "lts")
        edition_locks.reset_logged()
        ctx = SimpleNamespace(
            governance=parse_policy(
                {
                    "version": 1,
                    "boot": {"fail_closed": True},
                    "capabilities": {"telemetry": {"enabled": True, "otlp_endpoint": admin}},
                }
            )
        )
        monkeypatch.setattr(pc, "current_context", lambda: ctx)
        monkeypatch.setattr(pm, "current_context", lambda: ctx)
        monkeypatch.setattr(pm, "_policy_ceiling", lambda: ctx.governance)
        with patch("kiro_crew.sel.sel"):
            dests = pm._otlp_destinations(TelemetryConfig())
        assert [(d.name, d.endpoint) for d in dests] == [("policy", admin)]
        edition_locks.reset_logged()

    def test_a_policy_host_list_can_refuse_it(self, home, monkeypatch):
        from kiro_crew.platform.governance import parse_policy

        ceiling = parse_policy(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "capabilities": {
                    "telemetry": {
                        "scopes": {
                            "otlp_hosts": {"mode": "allow", "allow": ["collector.example.com"]}
                        }
                    }
                },
            }
        )
        monkeypatch.setattr(pm, "_policy_ceiling", lambda: ceiling)
        with patch("kiro_crew.sel.sel"):
            dests = pm._otlp_destinations(TelemetryConfig(otlp_endpoint=OPS), ceiling)
        assert [d.name for d in dests] == ["telemetry.otlp_endpoint"]

    def test_the_session_is_built_once(self, home):
        first = kiro_export.destinations()[0].session
        assert kiro_export.destinations()[0].session is first


class TestIdentity:
    @pytest.fixture
    def install_id(self, monkeypatch):
        monkeypatch.setattr(beacon, "install_id", lambda create=True: "a" * 32)
        return hashlib.sha256(
            ("kirocrew-metrics-machine-id-v1:" + "a" * 32).encode("utf-8")
        ).hexdigest()

    def test_only_the_machine_id_is_sent(self, home, install_id):
        assert kiro_export.identity_headers() == {"x-kiro-machineid": install_id}

    def test_the_points_carry_the_header_hash(self, home, install_id):
        """The endpoint does not copy the header onto stored points; a point with
        no identity is dropped downstream, so the same hash rides on each point."""
        (dest,) = kiro_export.destinations()
        assert dict(dest.point_attributes) == {"ClientType": "kiro-crew", "machineId": install_id}
        assert (
            dest.point_attributes["machineId"] == kiro_export.identity_headers()["x-kiro-machineid"]
        )

    def test_it_is_still_builtin_when_the_install_id_appears_later(self, home, monkeypatch):
        """A descriptor from before the id was minted and one from after are one
        destination; an operator entry of the same name still is not."""
        monkeypatch.setattr(beacon, "install_id", lambda create=True: "")
        (before,) = kiro_export.destinations()
        assert "machineId" not in before.point_attributes
        monkeypatch.setattr(beacon, "install_id", lambda create=True: "a" * 32)
        (after,) = kiro_export.destinations()
        assert "machineId" in after.point_attributes
        assert before != after
        assert kiro_export.is_builtin(before) and kiro_export.is_builtin(after)
        monkeypatch.setattr(beacon, "install_id", lambda create=True: "")
        assert kiro_export.is_builtin(after)
        cfg = TelemetryConfig(otlp_endpoints=[{"name": "kiro-telemetry", "endpoint": OPS}])
        operator = pm.otlp_egress_listing(cfg)[0].destination
        assert not kiro_export.is_builtin(operator)
        assert not kiro_export.is_builtin(None)

    def test_a_stored_sign_in_is_never_sent(self, home, monkeypatch, install_id):
        from kiro_crew.auth.store import KasToken, TokenStore

        token = KasToken(
            access_token="tok",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            provider="BuilderId",
            identity="builder_id",
        )
        monkeypatch.setattr(TokenStore, "resolve", lambda self: token)
        assert kiro_export.identity_headers() == {"x-kiro-machineid": install_id}

    def test_no_install_id_sends_nothing(self, home, monkeypatch):
        monkeypatch.setattr(beacon, "install_id", lambda create=True: "")
        assert kiro_export.identity_headers() == {}
        session = kiro_export.build_session()
        with patch("requests.Session.send") as send:
            response = session.post(kiro_export.ENDPOINT, data=b"x")
        assert response.status_code == 401
        send.assert_not_called()

    def test_another_url_is_never_sent(self, home, install_id):
        session = kiro_export.build_session()
        with patch("requests.Session.send") as send:
            response = session.post(OPS, data=b"x")
        assert response.status_code == 401
        send.assert_not_called()


# ── the real path, on the wire ──────────────────────────────────────────────


def _otlp_available() -> bool:
    try:
        import opentelemetry.exporter.otlp.proto.http.metric_exporter  # noqa: F401
        from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


class _LoopbackServer(http.server.HTTPServer):
    # Reached by port, never by name: skip the getfqdn lookup on bind.
    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        self.server.received.append((self.path, dict(self.headers), body))  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.end_headers()

    def log_message(self, *args):
        return


@pytest.mark.skipif(not _otlp_available(), reason="needs the otlp extra")
def test_the_real_provider_sends_product_metrics_to_the_endpoint(home, monkeypatch):
    """Written opt-in, the public provider, the real build and exporter. The
    loopback server stands in for the endpoint; nothing else is contacted."""
    from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2

    from kiro_crew.metrics import inventory_gauges as ig

    # Loopback only: this listens for our own export.
    server = _LoopbackServer(("127.0.0.1", 0), _Handler)
    server.received = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    monkeypatch.setattr(kiro_export, "ENDPOINT", f"http://{host}:{port}/v1/metrics")
    # Set for an operator's own collector; must never reach this one.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-env-auth=env-secret")
    _write_config(home, {"enabled": True, "export_interval_seconds": 3600})
    assert beacon.install_id(create=False) == ""
    try:
        ig.reset_for_testing()
        ig.mark_install_reporter()
        with patch.object(pm, "_default_metrics_dir", return_value=home / "metrics"):
            with patch("kiro_crew.sel.sel"):
                assert pm.get_recorder().enabled
                assert pm._provider is not None
                pm._provider.force_flush()
    finally:
        pm.reset_for_testing()
        ig.reset_for_testing()
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)

    assert server.received, "nothing reached the endpoint"
    expected_id = hashlib.sha256(
        ("kirocrew-metrics-machine-id-v1:" + beacon.install_id(create=False)).encode("utf-8")
    ).hexdigest()
    names, labels, resource_keys, point_ids = set(), set(), set(), set()
    points = 0
    for path, headers, body in server.received:
        assert path == "/v1/metrics"
        assert headers.get("x-kiro-machineid") == expected_id
        assert "Authorization" not in headers
        assert "x-env-auth" not in {k.lower() for k in headers}
        request = metrics_service_pb2.ExportMetricsServiceRequest()
        request.ParseFromString(body)
        for rm in request.resource_metrics:
            resource_keys.update(attr.key for attr in rm.resource.attributes)
            for sm in rm.scope_metrics:
                for metric in sm.metrics:
                    names.add(metric.name)
                    data = metric.WhichOneof("data")
                    for point in getattr(metric, data).data_points:
                        points += 1
                        attrs = {attr.key: attr.value.string_value for attr in point.attributes}
                        labels.add(attrs.get("ClientType"))
                        point_ids.add(attrs.get("machineId"))
    assert names and names <= PRODUCT_METRICS
    assert labels == {"kiro-crew"}
    # This home had no install id before the build: the build mints it before it
    # resolves the destination, so every point carries the header's hash.
    assert points and point_ids == {expected_id}
    assert resource_keys <= kiro_export.RESOURCE_ALLOW
    assert "service.instance.id" not in resource_keys


def test_the_suite_never_sees_it_by_default():
    """The conftest guard every other test relies on."""
    assert kiro_export._suppressed_for_tests is True
    assert kiro_export.destinations() == ()
