"""``_kiro.dev/metadata`` unconsumed-field reporting.

``parse_metadata`` reads two keys and drops everything else. These tests pin the
diagnostic that names what was dropped, so a field kiro-cli starts sending (a
prompt-cache counter, a new billing unit) becomes visible instead of silent.
"""

import logging

import pytest

from kiro_crew.acp import _dispatch
from kiro_crew.acp._dispatch import parse_metadata


@pytest.fixture(autouse=True)
def _clear_reported_fields():
    """Reset the once-per-process report set so each test starts cold."""
    _dispatch._reported_metadata_fields.clear()
    yield
    _dispatch._reported_metadata_fields.clear()


def _debug_lines(caplog):
    # getMessage() interpolates args; LogRecord.message only exists once a
    # Formatter has run, which caplog does not do.
    return [r.getMessage() for r in caplog.records]


class TestConsumedFieldsStaySilent:
    def test_known_params_report_nothing(self, caplog):
        with caplog.at_level(logging.DEBUG, logger=_dispatch.logger.name):
            pct, credits = parse_metadata(
                {
                    "contextUsagePercentage": 41.0,
                    "meteringUsage": [{"unit": "credit", "unitPlural": "credits", "value": 0.5}],
                }
            )
        assert (pct, credits) == (41.0, 0.5)
        assert not [ln for ln in _debug_lines(caplog) if "unconsumed field" in ln]

    def test_empty_params_report_nothing(self, caplog):
        with caplog.at_level(logging.DEBUG, logger=_dispatch.logger.name):
            assert parse_metadata({}) == (None, 0.0)
        assert not [ln for ln in _debug_lines(caplog) if "unconsumed field" in ln]

    def test_session_id_is_not_reported(self, caplog):
        # AcpRuntime routes every notification by params["sessionId"], so a
        # shared-runtime frame always carries it. Reporting it would mislabel a
        # consumed routing field on the first frame of every session.
        with caplog.at_level(logging.DEBUG, logger=_dispatch.logger.name):
            parse_metadata({"sessionId": "sess-abc", "contextUsagePercentage": 12})
        assert not [ln for ln in _debug_lines(caplog) if "unconsumed field" in ln]


class TestUnconsumedFieldsAreNamed:
    def test_unknown_top_level_key_is_named_with_its_type(self, caplog):
        with caplog.at_level(logging.DEBUG, logger=_dispatch.logger.name):
            parse_metadata({"contextUsagePercentage": 10, "cacheReadTokens": 2048})
        line = next(ln for ln in _debug_lines(caplog) if "unconsumed field" in ln)
        assert "cacheReadTokens:int" in line

    def test_value_is_never_logged(self, caplog):
        with caplog.at_level(logging.DEBUG, logger=_dispatch.logger.name):
            parse_metadata({"accountId": "super-secret-value"})
        line = next(ln for ln in _debug_lines(caplog) if "unconsumed field" in ln)
        assert "accountId:str" in line
        assert "super-secret-value" not in line

    def test_non_credit_metering_unit_is_named(self, caplog):
        with caplog.at_level(logging.DEBUG, logger=_dispatch.logger.name):
            _, credits = parse_metadata(
                {"meteringUsage": [{"unit": "cacheRead", "value": 4096}]}
            )
        # The unit is not "credit", so it still contributes nothing to the sum.
        assert credits == 0.0
        line = next(ln for ln in _debug_lines(caplog) if "unconsumed field" in ln)
        assert "meteringUsage[].unit=cacheRead" in line

    def test_unknown_metering_entry_key_is_named(self, caplog):
        with caplog.at_level(logging.DEBUG, logger=_dispatch.logger.name):
            parse_metadata(
                {"meteringUsage": [{"unit": "credit", "value": 1.0, "cachedTokens": 900}]}
            )
        line = next(ln for ln in _debug_lines(caplog) if "unconsumed field" in ln)
        assert "meteringUsage[].cachedTokens:int" in line


class TestReportedOncePerProcess:
    def test_repeat_notification_does_not_re_report(self, caplog):
        params = {"cacheReadTokens": 2048}
        with caplog.at_level(logging.DEBUG, logger=_dispatch.logger.name):
            parse_metadata(params)
            first = len([ln for ln in _debug_lines(caplog) if "unconsumed field" in ln])
            parse_metadata(params)
            second = len([ln for ln in _debug_lines(caplog) if "unconsumed field" in ln])
        assert (first, second) == (1, 1)

    def test_a_newly_appearing_field_still_reports(self, caplog):
        with caplog.at_level(logging.DEBUG, logger=_dispatch.logger.name):
            parse_metadata({"cacheReadTokens": 1})
            parse_metadata({"cacheReadTokens": 1, "cacheWriteTokens": 2})
        lines = [ln for ln in _debug_lines(caplog) if "unconsumed field" in ln]
        assert len(lines) == 2
        assert "cacheWriteTokens:int" in lines[1]
        assert "cacheReadTokens" not in lines[1]


class TestDiagnosticNeverBreaksParsing:
    def test_malformed_metering_entries_are_tolerated(self):
        pct, credits = parse_metadata(
            {
                "contextUsagePercentage": "77",
                "meteringUsage": ["not-a-dict", None, {"unit": "credit", "value": 2}],
            }
        )
        assert (pct, credits) == (77.0, 2.0)

    def test_a_scan_failure_leaves_parsing_intact(self, monkeypatch):
        def _boom(_params):
            raise RuntimeError("scan exploded")

        monkeypatch.setattr(_dispatch, "_log_unrecognized_metadata_fields", _boom)
        pct, credits = parse_metadata(
            {"contextUsagePercentage": 12.5, "meteringUsage": [{"unit": "credit", "value": 3}]}
        )
        assert (pct, credits) == (12.5, 3.0)
