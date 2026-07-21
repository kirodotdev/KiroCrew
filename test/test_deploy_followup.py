"""Follow-up batch regression tests (FU-1/FU-3, NEW-1 pricing, NEW-3 alarm)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.deploy import handlers as h
from kiro_crew.deploy import pricing

_TPL_DIR = Path(h.__file__).parent / "skills" / "artifact-deploy" / "templates"


class TestFU1ReaperRemediation:
    def test_renders_exact_operator_command(self):
        cmd = h._reaper_remediation("personal", "us-west-2")
        assert cmd == "install-reaper.sh --profile personal --region us-west-2"

    def test_omits_empty_parts(self):
        assert h._reaper_remediation("", "") == "install-reaper.sh"

    def test_both_409_sites_attach_remediation(self):
        src = Path(h.__file__).read_text()
        assert src.count('"remediation": _reaper_remediation(profile, region)') == 2


class TestFU3EmptyCostHint:
    def test_save_response_warns_on_empty_estimates(self):
        from kiro_crew import mcp_core
        src = Path(mcp_core.__file__.replace(".pyc", ".py")).read_text()
        assert "webapp_metadata.cost.estimates is empty" in src

    def test_hint_condition_requires_webapp_kind(self):
        from kiro_crew import mcp_core
        src = Path(mcp_core.__file__.replace(".pyc", ".py")).read_text()
        gate = src.split("cost_hint = \"\"")[1].split("cost_hint = (")[0]
        assert 'kind == "webapp"' in gate


def _price_doc(usd: str) -> str:
    return json.dumps({
        "product": {"attributes": {}},
        "terms": {"OnDemand": {"t1": {"priceDimensions": {
            "d1": {"unit": "Requests", "pricePerUnit": {"USD": usd}},
        }}}},
    })


class TestNew1Pricing:
    def setup_method(self):
        pricing._CACHE.clear()

    def test_live_prices_extracted(self, monkeypatch):
        def fake_run_aws(args, profile, timeout):
            return 0, json.dumps({"PriceList": [_price_doc("0.0000012")]}), ""
        monkeypatch.setattr(pricing.engine, "run_aws", fake_run_aws)
        prices = pricing.get_unit_prices("p1", "us-west-2")
        assert prices.source == "live"
        assert prices.s3_gb_month == pytest.approx(0.0000012)
        assert prices.cf_per_10k_requests == pytest.approx(0.0000012 * 10000)

    def test_fallback_on_api_failure(self, monkeypatch):
        monkeypatch.setattr(
            pricing.engine, "run_aws", lambda a, p, t: (1, "", "denied"))
        prices = pricing.get_unit_prices("p1", "us-west-2")
        assert prices.source == "fallback"
        assert prices.s3_gb_month == pricing._FALLBACK.s3_gb_month

    def test_fallback_on_shape_drift(self, monkeypatch):
        monkeypatch.setattr(
            pricing.engine, "run_aws",
            lambda a, p, t: (0, '{"PriceList": ["not-json"]}', ""))
        prices = pricing.get_unit_prices("p1", "us-west-2")
        assert prices.source == "fallback"

    def test_zero_price_rejected(self, monkeypatch):
        monkeypatch.setattr(
            pricing.engine, "run_aws",
            lambda a, p, t: (0, json.dumps({"PriceList": [_price_doc("0")]}), ""))
        prices = pricing.get_unit_prices("p1", "us-west-2")
        assert prices.source == "fallback"

    def test_cache_prevents_repeat_lookups(self, monkeypatch):
        calls = []

        def fake_run_aws(args, profile, timeout):
            calls.append(args)
            return 0, json.dumps({"PriceList": [_price_doc("0.001")]}), ""
        monkeypatch.setattr(pricing.engine, "run_aws", fake_run_aws)
        pricing.get_unit_prices("p1", "us-west-2")
        first = len(calls)
        pricing.get_unit_prices("p1", "us-west-2")
        assert len(calls) == first

    def test_pricing_api_always_queried_in_us_east_1(self, monkeypatch):
        seen = []

        def fake_run_aws(args, profile, timeout):
            seen.append(args)
            return 1, "", ""
        monkeypatch.setattr(pricing.engine, "run_aws", fake_run_aws)
        pricing.get_unit_prices("p1", "ap-southeast-2")
        for args in seen:
            region = args[args.index("--region") + 1]
            assert region == "us-east-1"

    def test_route_registered(self):
        src = Path(h.__file__).read_text()
        assert 'r.add_get("/api/deploy/pricing", _handle_pricing)' in src


class TestNew3ReaperAlarm:
    def test_alarm_resources_are_conditional(self):
        tpl = (_TPL_DIR / "reaper.yaml").read_text()
        assert "AlarmEmail:" in tpl
        assert "HasAlarmEmail: !Not [!Equals [!Ref AlarmEmail, '']]" in tpl
        assert tpl.count("Condition: HasAlarmEmail") == 2

    def test_alarm_watches_reaper_errors_with_action(self):
        tpl = (_TPL_DIR / "reaper.yaml").read_text()
        assert "MetricName: Errors" in tpl
        assert "AlarmActions:" in tpl
        assert "TreatMissingData: notBreaching" in tpl

    def test_install_script_wires_alarm_email(self):
        script = (
            _TPL_DIR.parent / "scripts" / "install-reaper.sh").read_text()
        assert "--alarm-email" in script
        assert 'AlarmEmail="${ALARM_EMAIL:-}"' in script
