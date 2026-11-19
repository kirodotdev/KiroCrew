"""``kirocrew compliance report`` reads the security facts into one evidence file."""

from __future__ import annotations

import argparse
import json
import stat
import types

import pytest

from kiro_crew import cli_compliance, platform_compat
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform import policy_distribution

_FACT_NAMES = {"policy", "retention", "audit_chain", "denied_rules", "app_trust"}


def _args(out: object = None, action: str = "report") -> argparse.Namespace:
    return argparse.Namespace(command="compliance", compliance_action=action, out=out)


@pytest.fixture(autouse=True)
def _isolated(_floor_monkeypatch, tmp_path):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()


def test_report_holds_every_fact_and_a_manifest_that_matches_it():
    report = cli_compliance.build_report()
    assert set(report["facts"]) == _FACT_NAMES
    assert all(fact["status"] == "ok" for fact in report["facts"].values())
    parts = report["manifest"]["parts"]
    for name, fact in report["facts"].items():
        assert parts[name] == cli_compliance._digest(fact)
    assert parts["summary"] == cli_compliance._digest(report["summary"])
    body = {key: value for key, value in report.items() if key != "manifest"}
    assert report["manifest"]["whole"] == cli_compliance._digest(body)
    assert len(report["summary"]) == len(_FACT_NAMES)
    report["facts"]["retention"]["telemetry_days"] = 1  # an edited fact changes its digest
    assert parts["retention"] != cli_compliance._digest(report["facts"]["retention"])


@pytest.mark.parametrize(("tier", "source"), [("home", "local"), ("central", "central")])
def test_the_source_is_the_installed_tier_not_the_distribution_settings(monkeypatch, tier, source):
    monkeypatch.setattr(policy_distribution, "distribution_posture", lambda: {"configured": True})
    ceiling = types.SimpleNamespace(
        tier=tier,
        version=4,
        signature_state="verified",
        controls={"commands": {}},
        boot=types.SimpleNamespace(require_sandbox=True, fail_closed=True),
        signature_summary=lambda: "signed and verified (issuer: acme)",
    )
    context = types.SimpleNamespace(governance=ceiling)
    monkeypatch.setattr(ctx_mod, "current_context", lambda: context)
    policy = cli_compliance.build_report()["facts"]["policy"]
    assert [policy[k] for k in ("version", "signature_state", "source")] == [4, "verified", source]
    assert policy["distribution"]["configured"] is True
    assert policy["provenance"] == "signed and verified (issuer: acme)"
    assert policy["require_sandbox"] is True


def test_an_unreadable_fact_is_unavailable_and_the_rest_still_read(monkeypatch):
    def unreadable():
        raise OSError("/private/place/events.log is locked")

    facts = [(n, unreadable if n == "audit_chain" else read) for n, read in cli_compliance._FACTS]
    monkeypatch.setattr(cli_compliance, "_FACTS", tuple(facts))
    report = cli_compliance.build_report()
    assert report["facts"]["audit_chain"] == {"status": "unavailable", "reason": "OSError"}
    assert "/private/place" not in json.dumps(report)
    assert report["facts"]["retention"]["status"] == "ok"
    assert any(line.startswith("audit chain: unavailable (OSError)") for line in report["summary"])


def test_switched_off_cleanup_reads_as_no_limit_not_a_day_count(tmp_path):
    off = {"session": {"archive_retention_days": -1}, "telemetry": {"retention_days": 0}}
    (tmp_path / "home" / "config.json").write_text(json.dumps(off), encoding="utf-8")
    report = cli_compliance.build_report()
    assert report["facts"]["retention"]["session_archive_days"] is None
    assert "sessions no limit, telemetry no limit (size cap MB: 100)" in report["summary"][1]


@pytest.mark.parametrize("text", ['{"memory": [], "agent": []}', "{not json"])
def test_defaults_standing_in_for_the_config_read_as_unavailable(tmp_path, text):
    (tmp_path / "home" / "config.json").write_text(text, encoding="utf-8")
    facts = cli_compliance.build_report()["facts"]
    for name in ("retention", "app_trust"):
        assert facts[name] == {"status": "unavailable", "reason": "config-defaults"}


def test_out_writes_one_owner_only_file_and_prints_the_summary(tmp_path, capsys):
    target = tmp_path / "evidence.json"
    assert cli_compliance.compliance_cmd(_args(out=str(target))) == 0
    written = json.loads(target.read_text(encoding="utf-8"))
    assert set(written["facts"]) == _FACT_NAMES
    if platform_compat.IS_POSIX:
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    shown = capsys.readouterr().out
    assert "Wrote the compliance report to" in shown
    assert written["summary"][0] in shown


def test_out_never_overwrites_and_removes_a_file_it_cannot_lock(tmp_path, capsys, monkeypatch):
    target = tmp_path / "evidence.json"
    target.write_text("keep me", encoding="utf-8")
    assert cli_compliance.compliance_cmd(_args(out=str(target))) == 1
    assert target.read_text(encoding="utf-8") == "keep me"
    assert "Refusing to overwrite evidence.json" in capsys.readouterr().err
    target.unlink()

    def refuse(path):
        assert target.read_text(encoding="utf-8") == ""  # locked before any text is written
        raise PermissionError("/private/place")

    monkeypatch.setattr(platform_compat, "restrict_to_owner", refuse)
    assert cli_compliance.compliance_cmd(_args(out=str(target))) == 1
    assert not target.exists()
    assert "Could not write evidence.json: PermissionError." in capsys.readouterr().err


def test_it_prints_without_out_and_shows_usage_without_an_action(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli_compliance.compliance_cmd(_args()) == 0
    assert set(json.loads(capsys.readouterr().out)["facts"]) == _FACT_NAMES
    assert not list(tmp_path.glob("*.json"))
    assert cli_compliance.compliance_cmd(_args(action=None)) == 2
    assert "Usage: kirocrew compliance report" in capsys.readouterr().err
