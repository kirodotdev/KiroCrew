"""The upgrade move of a Bot whose old agent carries restrictions.

Each restriction the worker keeps exactly is written into the Bot's file; the
rest are held for a person (``mate_held_rules``), and the Bot still moves and
its worker still starts. Split from ``test_mate_config_migration``, whose
fixtures and helpers these tests share.
"""

from __future__ import annotations

import json

import pytest
import test_mate_config_migration as _migration
from test_mate_config_migration import BASE, _cfg, _file, _template, _write

from kiro_crew import mate_worker
from kiro_crew.config.loader import KiroCrewAgentConfig
from kiro_crew.mate_config_migration import migrate_existing_crewmates

#: The fixtures this file shares with ``test_mate_config_migration``.
layout = _migration.layout
persisted = _migration.persisted


def test_a_shell_deny_rule_with_an_auto_approved_cron_stays_on_the_worker(layout, persisted):
    """Raphael's report: deny-only rules (shell, web, an mcp deny except kept tools)
    were dropped, so every action asked and auto approval took it."""
    user, mates = layout
    rules = [
        {"capability": "shell", "effect": "deny"},
        {"capability": "web_fetch", "effect": "deny"},
        {"capability": "mcp", "effect": "deny", "match": ["*"], "exclude": ["kirocrew-core/*"]},
        {"capability": "web_search", "effect": "allow"},
    ]
    _write(user, "crew-boxed", _template("crew-boxed", permissions={"rules": rules}))
    cfg = _cfg(box=KiroCrewAgentConfig(member_id="box", kiro_agent="crew-boxed"))
    persisted(cfg)

    report = migrate_existing_crewmates(cfg)

    assert report.migrated == ["box"]
    written = _file(mates, "box")
    merged = mate_worker.merge_worker_spec(BASE, mate_worker.worker_additions(BASE, written))
    # Every restriction lands; the allow-only rule is a grant and is not written.
    assert merged["permissions"]["rules"] == rules[:3]


def test_a_restriction_the_worker_cannot_carry_moves_held_and_its_worker_starts(layout, persisted):
    """Never a startup failure: the limit is held for a person and an ``ask`` stands in."""
    from kiro_crew.mate_held_rules import HELD_KEY

    user, mates = layout
    unknown = {"execute_bash": {"fallbackAction": "deny"}}
    _write(user, "odd", _template("odd", toolsSettings=unknown))
    cfg = _cfg(qwe=KiroCrewAgentConfig(member_id="qwe", kiro_agent="odd"))
    persisted(cfg)

    report = migrate_existing_crewmates(cfg)

    assert report.migrated == ["qwe"] and report.dry_run_failed == {}
    written = _file(mates, "qwe")
    assert "toolsSettings" not in written
    merged = mate_worker.merge_worker_spec(BASE, mate_worker.worker_additions(BASE, written))
    assert {"capability": "shell", "effect": "ask"} in merged["permissions"]["rules"]
    ledger = json.loads((mates.parent / "migrated.json").read_text(encoding="utf-8"))
    assert [r["capability"] for r in ledger[HELD_KEY]["qwe"]["rules"]] == ["shell"]


def test_a_restriction_on_a_backend_that_cannot_apply_it_is_held_not_written(
    layout, persisted, monkeypatch
):
    """Only KAS applies a Bot file's restrictions; elsewhere nothing is written and all are held."""
    from kiro_crew.mate_held_rules import HELD_KEY

    user, mates = layout
    deny = {"rules": [{"capability": "shell", "effect": "deny"}]}
    _write(user, "careful", _template("careful", permissions=deny))
    cfg = _cfg(ijk=KiroCrewAgentConfig(member_id="ijk", kiro_agent="careful"))
    persisted(cfg)
    monkeypatch.setattr(mate_worker, "crewmate_worker_backend", lambda c, n=None: "kiro-cli")

    report = migrate_existing_crewmates(cfg)

    assert report.migrated == ["ijk"]
    assert "permissions" not in _file(mates, "ijk")
    ledger = json.loads((mates.parent / "migrated.json").read_text(encoding="utf-8"))
    assert ledger[HELD_KEY]["ijk"]["rules"][0]["capability"] == "shell"


def _fail_write(monkeypatch):
    from kiro_crew.mate_config import MateConfigError

    def refuse(name, spec):
        raise MateConfigError("disk full")

    monkeypatch.setattr("kiro_crew.mate_config_migration._write_file", refuse)


def _fail_read_back_check(monkeypatch):
    import kiro_crew.mate_config_migration as migration

    real, calls = migration._loosening_besides_restrictions, []

    def second_says_looser(diff):
        calls.append(diff)
        return "it would gain a tool" if len(calls) == 2 else real(diff)

    monkeypatch.setattr(migration, "_loosening_besides_restrictions", second_says_looser)


def _fail_repoint(monkeypatch):
    from kiro_crew.memory_stores import MemberFieldConflict

    def switched(c, name, **kw):
        raise MemberFieldConflict("kiro_agent")

    monkeypatch.setattr("kiro_crew.memory_stores.persist_member_config", switched)


@pytest.mark.parametrize(
    "prompt, fail",
    [
        ("", _fail_write),
        ("", _fail_read_back_check),
        ("", _fail_repoint),
        ("file://missing-prompt.md", _fail_write),
        ("file://missing-prompt.md", _fail_repoint),
    ],
    ids=["write", "read-back", "repoint", "default-template-write", "default-template-repoint"],
)
def test_a_bot_that_stays_on_its_old_agent_keeps_no_held_limit(
    layout, persisted, monkeypatch, prompt, fail
):
    """Its old agent still enforces its own limits, so a hold recorded for the move
    it did not make would only make its calls ask a person, with no notice saying why."""
    from kiro_crew.mate_held_rules import HELD_KEY

    user, mates = layout
    _write(user, "kirocrew", _template("kirocrew"))
    deny = {"rules": [{"capability": "shell", "effect": "deny"}]}
    extra = {"prompt": prompt} if prompt else {}
    _write(user, "careful", _template("careful", permissions=deny, **extra))
    cfg = _cfg(ijk=KiroCrewAgentConfig(member_id="ijk", kiro_agent="careful"))
    persisted(cfg)
    monkeypatch.setattr(mate_worker, "crewmate_worker_backend", lambda c, n=None: "kiro-cli")
    fail(monkeypatch)

    report = migrate_existing_crewmates(cfg)

    assert report.migrated == []
    ledger_path = mates.parent / "migrated.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else {}
    assert "ijk" not in ledger.get(HELD_KEY, {})


def test_a_bot_another_gateway_keeps_under_the_same_name_keeps_its_hold(
    layout, persisted, monkeypatch
):
    """Two gateways with their own data homes share this Kiro home. The other one
    moved its Bot ``ijk`` and holds its limits; this one's ``ijk`` cannot take that
    file, so it stays and the other Bot's hold is left exactly as it was."""
    from kiro_crew.mate_held_rules import HELD_KEY

    user, mates = layout
    deny = {"rules": [{"capability": "shell", "effect": "deny"}]}
    _write(user, "careful", _template("careful", permissions=deny))
    _write(mates, "ijk", {"name": "ijk", "mcpServers": {}, "tools": [], "allowedTools": []})
    (mates.parent / "owners.json").write_text(json.dumps({"ijk": "/other/data/home"}))
    theirs = {"member_id": "theirs", "rules": [{"text": "deny shell", "capability": "shell"}]}
    (mates.parent / "migrated.json").write_text(json.dumps({HELD_KEY: {"ijk": theirs}}))
    cfg = _cfg(ijk=KiroCrewAgentConfig(member_id="ours", kiro_agent="careful"))
    persisted(cfg)
    monkeypatch.setattr(mate_worker, "crewmate_worker_backend", lambda c, n=None: "kiro-cli")

    report = migrate_existing_crewmates(cfg)

    assert report.migrated == [] and "ijk" in report.copy_failed
    ledger = json.loads((mates.parent / "migrated.json").read_text(encoding="utf-8"))
    assert ledger[HELD_KEY]["ijk"] == theirs


@pytest.mark.parametrize(("backend", "says_reads_run"), [("", True), ("claude", False)])
def test_a_held_read_limit_on_kiro_cli_says_its_folder_reads_still_run(
    layout, persisted, monkeypatch, backend, says_reads_run
):
    """Kiro CLI reads inside the worker's own folder with no permission request, as
    for any Kiro CLI agent, so the notice must not claim those reads ask first."""
    from kiro_crew.mate_upgrade_notice import claim_policy_notice
    from kiro_crew.members import member_slug

    user, _mates = layout
    deny = {"rules": [{"capability": "fs_read", "effect": "deny"}]}
    _write(user, "careful", _template("careful", permissions=deny))
    cfg = _cfg(ijk=KiroCrewAgentConfig(member_id="ijk", kiro_agent="careful"))
    persisted(cfg)
    monkeypatch.setattr(mate_worker, "crewmate_worker_backend", lambda c, n=None: backend)

    assert migrate_existing_crewmates(cfg).migrated == ["ijk"]

    prompt, held = claim_policy_notice(member_slug("ijk"), "ijk")
    assert held is True and "asks you" in prompt
    assert ("inside its own folder without asking" in prompt) is says_reads_run
