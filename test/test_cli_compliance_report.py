"""``kirocrew compliance report`` reads the security facts into one evidence file."""

from __future__ import annotations

import argparse
import json
import stat
from types import SimpleNamespace

import pytest

from kiro_crew import cli_compliance, platform_compat
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform import governance_profiles, policy_distribution
from kiro_crew.security import builtin_denied_rules
from kiro_crew.testing.links import make_dir_link

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
    assert all(parts[n] == cli_compliance._digest(f) for n, f in report["facts"].items())
    assert parts["summary"] == cli_compliance._digest(report["summary"])
    body = {key: value for key, value in report.items() if key != "manifest"}
    assert report["manifest"]["whole"] == cli_compliance._digest(body)
    assert len(report["summary"]) == len(_FACT_NAMES)
    report["facts"]["retention"]["telemetry_days"] = 1  # an edited fact changes its digest
    assert parts["retention"] != cli_compliance._digest(report["facts"]["retention"])


@pytest.mark.parametrize(("tier", "source"), [("home", "local"), ("central", "central")])
def test_the_source_is_the_installed_tier_not_the_distribution_settings(monkeypatch, tier, source):
    monkeypatch.setattr(policy_distribution, "distribution_posture", lambda: {"configured": True})
    ceiling = SimpleNamespace(tier=tier, version=4, signature_state="verified", controls=[1])
    ceiling.boot = SimpleNamespace(require_sandbox=True, fail_closed=True)
    ceiling.signature_summary = lambda: "signed and verified (issuer: acme)"
    monkeypatch.setattr(ctx_mod, "current_context", lambda: SimpleNamespace(governance=ceiling))
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


@pytest.mark.parametrize("folder", ["real", "link"])  # "link": --out into a linked folder
def test_out_writes_one_owner_only_file_and_prints_the_summary(tmp_path, capsys, folder):
    (tmp_path / "real").mkdir()
    make_dir_link(tmp_path / "link", tmp_path / "real")
    target = tmp_path / folder / "evidence.json"
    assert cli_compliance.compliance_cmd(_args(out=str(target))) == 0
    written = json.loads((tmp_path / "real" / "evidence.json").read_text(encoding="utf-8"))
    assert set(written["facts"]) == _FACT_NAMES
    if platform_compat.IS_POSIX:
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    shown = capsys.readouterr().out
    assert "Wrote the compliance report to" in shown and written["summary"][0] in shown


def test_out_never_overwrites_and_removes_a_file_it_cannot_lock(tmp_path, capsys, monkeypatch):
    target = tmp_path / "evidence.json"
    target.write_text("keep me", encoding="utf-8")
    assert cli_compliance.compliance_cmd(_args(out=str(target))) == 1
    assert target.read_text(encoding="utf-8") == "keep me"
    assert "Refusing to overwrite evidence.json" in capsys.readouterr().err
    target.unlink()

    def refuse(path):
        assert target.read_text(encoding="utf-8") == ""  # locked before any text is written
        raise PermissionError(13, "Permission denied", "/private/place")

    monkeypatch.setattr(platform_compat, "restrict_to_owner", refuse)
    assert cli_compliance.compliance_cmd(_args(out=str(target))) == 1
    assert not target.exists()
    err = capsys.readouterr().err
    assert "evidence.json: PermissionError (Permission denied)." in err and "/private" not in err


def test_it_prints_without_out_and_shows_usage_without_an_action(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli_compliance.compliance_cmd(_args()) == 0
    assert set(json.loads(capsys.readouterr().out)["facts"]) == _FACT_NAMES
    assert not list(tmp_path.glob("*.json"))
    assert cli_compliance.compliance_cmd(_args(action=None)) == 2
    assert "Usage: kirocrew compliance report" in capsys.readouterr().err


def test_a_rule_pinned_only_by_an_unused_profile_is_not_counted_as_on(tmp_path, monkeypatch):
    rule = next(r for r in builtin_denied_rules() if r["id"] == "credential-exfil-s3-cp")
    off = json.dumps({"disabled_ids": [rule["id"]]})
    (tmp_path / "home" / "denied_commands.json").write_text(off, encoding="utf-8")
    pin = rule["pattern"]
    monkeypatch.setattr(governance_profiles, "all_profile_pinned_commands", lambda: (pin,))
    fact = cli_compliance.build_report()["facts"]["denied_rules"]
    assert fact["enabled"] == fact["builtin_total"] + fact["edition_total"] - 1  # off, as saved
    assert fact["profile_pinned"] == 1 and fact["locked_by_policy"] == 1
