# SPDX-License-Identifier: Apache-2.0
"""Crewmate 2.0 behind ``dashboard.crewmate_v2``, on by default.

A fresh install and an upgrade with no key set both run it. Off is the shape before the conductor/worker split: no startup migration, no
crewmate file on create, the chat on the bound template, no worker redirect or
worker backend route, and none of the conductor-only prompt pieces. A run that
migrated with the switch on is reversed at the next start with it off, and
turning it on again migrates afresh. The rest of the suite runs with it on
(``conftest._crewmate_v2_on``).
"""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

import pytest
import test_mate_config_migration as _mig
from test_mate_config_migration import JIRA, _cfg, _file, _ledger, _now, _template, _thread, _write

from kiro_crew import crewmate_v2, mate_worker
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.mate_config_migration import migrate_existing_crewmates
from kiro_crew.mate_config_reversal import migrate_or_reverse, reverse_migration

# The migration suite's fixtures: its layout, its config shim, its thread records.
layout = _mig.layout
persisted = _mig.persisted
threads = _mig.threads

CONDUCTOR = mate_worker.MATE_CONDUCTOR_AGENT
KEY = "dashboard:member-zxc.memory-member-zxc-0123"


@pytest.fixture
def off():
    crewmate_v2._latched = False


@pytest.fixture
def reversal_cfg(monkeypatch):
    """Serve the reversal the same config object the migration shim serves."""

    def install(cfg):
        monkeypatch.setattr(
            "kiro_crew.mate_config_reversal.KiroCrewConfig",
            type("Shim", (), {"load": staticmethod(lambda: cfg)}),
        )

    return install


# ── the switch itself ──


def test_the_switch_ships_on_and_is_a_restart_setting():
    from kiro_crew.config import loader
    from kiro_crew.config.schema import requires_restart
    from kiro_crew.dashboard.handlers import core

    # A fresh install (no config) and an upgrade whose dashboard section has no key.
    assert KiroCrewConfig().dashboard.crewmate_v2 is True
    assert loader._build_dashboard_config(set(), {}).crewmate_v2 is True
    assert loader._build_dashboard_config(set(), {"crewmate_v2": False}).crewmate_v2 is False
    assert requires_restart(crewmate_v2.CONFIG_KEY)
    assert core._EDITABLE_CONFIG[crewmate_v2.CONFIG_KEY] == {"type": "bool"}


def test_the_switch_is_read_once_per_process(monkeypatch):
    reads = iter([True, False])
    monkeypatch.setattr(crewmate_v2, "_read", lambda: next(reads))
    assert crewmate_v2.latch() is True
    # A config edit after the latch changes nothing until the next start.
    assert crewmate_v2.enabled() is True


def test_an_unreadable_config_reads_the_default_and_is_not_latched(monkeypatch):
    # On, not off: off would reverse the migration at a start whose read failed.
    crewmate_v2._latched = None
    monkeypatch.setattr(crewmate_v2, "_read", lambda: None)
    assert crewmate_v2.enabled() is True
    assert crewmate_v2._latched is None


def test_an_upgrade_with_no_key_set_latches_on(tmp_path, monkeypatch):
    """The real read: a config.json from before the switch existed runs 2.0."""
    from kiro_crew.config import loader

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"dashboard": {"crewmate_threads": True}}), encoding="utf-8")
    monkeypatch.setattr(loader, "config_path", lambda: path)
    loader._invalidate_config_cache()
    try:
        # The file itself was read (not the unreadable-config default).
        assert loader.KiroCrewConfig.load().dashboard.crewmate_threads is True
        assert crewmate_v2._read() is True
        assert crewmate_v2.latch() is True
    finally:
        loader._invalidate_config_cache()


# ── off: the chat, the hand-off, the worker ──


def test_off_the_chat_runs_on_the_bound_name(off, monkeypatch):
    from kiro_crew.config import loader

    monkeypatch.setattr(mate_worker, "is_mate_owned_agent", lambda name: True)
    monkeypatch.setattr(loader, "dispatch_kiro_agent", lambda name: f"dispatched:{name}")
    assert mate_worker.member_chat_agent("zxc") == "dispatched:zxc"
    assert mate_worker.member_chat_template("zxc") == "zxc"


def test_on_the_chat_runs_on_the_conductor(monkeypatch):
    monkeypatch.setattr(mate_worker, "is_mate_owned_agent", lambda name: True)
    assert mate_worker.member_chat_agent("zxc") == CONDUCTOR
    assert mate_worker.member_chat_template("zxc") == CONDUCTOR


def _worker_record():
    return SimpleNamespace(
        selection_kind="template", template_id=mate_worker.MATE_WORKER_AGENT, member_id="m-1"
    )


def test_off_no_session_is_a_crewmates_worker(off):
    from kiro_crew import members

    members.note_crewmate_worker_session("dashboard:chat-w1")
    assert mate_worker.is_own_worker_execution(_worker_record()) is False
    assert members.is_crewmate_worker_session("dashboard:chat-w1") is False
    # So the worker backend route is not taken: the configured default decides.
    assert members.select_provider_backend("dashboard:chat-w1", "kas", "claude") == "claude"


def test_on_a_crewmates_worker_takes_the_member_route():
    from kiro_crew import members

    members.note_crewmate_worker_session("dashboard:chat-w2")
    assert mate_worker.is_own_worker_execution(_worker_record()) is True
    assert members.is_crewmate_worker_session("dashboard:chat-w2") is True


def test_off_session_create_does_not_redirect_to_the_worker(off, monkeypatch):
    from kiro_crew.dashboard.session_control_parts import session_create

    monkeypatch.setattr(session_create, "_conducts_for_a_worker", lambda *a: True)
    assert session_create._hands_off_to_own_worker(None, None, "", "template") is False


def test_on_session_create_redirects_to_the_worker(monkeypatch):
    from kiro_crew.dashboard.session_control_parts import session_create

    monkeypatch.setattr(session_create, "_conducts_for_a_worker", lambda *a: True)
    assert session_create._hands_off_to_own_worker(None, None, "", "template") is True


# ── off: the create path ──


def test_off_create_writes_no_crewmate_file(off, layout, persisted):
    from kiro_crew.mate_config import adopt_own_file, has_mate_config

    _user, mates = layout
    cfg = _cfg(zxc=KiroCrewAgentConfig(member_id="zxc", kiro_agent="kirocrew"))
    persisted(cfg)
    assert adopt_own_file("zxc") == "switched_off"
    assert not has_mate_config("zxc")
    # Only the generated worker base the layout put there; no crewmate file.
    assert [p.name for p in mates.glob("*.json")] == ["kirocrew-mate-worker.json"]
    assert cfg.agents["zxc"].kiro_agent == "kirocrew"


def test_off_the_first_crewmate_gets_the_default_spec(off):
    from kiro_crew.agent_materialization import first_crewmate

    assert first_crewmate._default_template({}) == "kirocrew"
    crewmate_v2._latched = True
    assert first_crewmate._default_template({}) == CONDUCTOR


def test_off_the_mate_tools_grant_takes_the_enrollment_path(off, monkeypatch):
    from kiro_crew import member_mate_tools

    seen: list[str] = []
    monkeypatch.setattr(
        member_mate_tools, "_grant_by_enrollment", lambda m, c: seen.append(m) or "granted"
    )
    assert member_mate_tools.grant_mate_tools_to_new_member("zxc") == "granted"
    assert seen == ["zxc"]


# ── off: no conductor-only prompt pieces ──


def test_off_the_member_section_has_no_crew_log_block(off, monkeypatch, tmp_path):
    from kiro_crew import context as ctx
    from kiro_crew.context_assembly import member

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    cfg = ctx.KiroCrewConfig()
    cfg.agents["Ada"] = SimpleNamespace(
        description="", triggers="", kiro_agent="", workspace="", member_id=""
    )
    monkeypatch.setattr(ctx.KiroCrewConfig, "load", staticmethod(lambda *a, **k: cfg))
    marker = "[YOUR CREW LOG, DASHBOARD AND TOOLS]"
    assert marker not in member.build_member_section("Ada")
    crewmate_v2._latched = True
    assert marker in member.build_member_section("Ada")


def test_off_no_build_your_page_line(off, monkeypatch):
    from kiro_crew import dashboard_agentic
    from kiro_crew.dashboard import goal_reminder

    monkeypatch.setattr(dashboard_agentic, "bound_package", lambda slug: None)
    assert goal_reminder.dashboard_line("ada") == ""
    crewmate_v2._latched = True
    assert goal_reminder.dashboard_line("ada") != ""


def test_off_the_mate_server_lists_no_catalog_tool(off):
    from kiro_crew import mcp_mate

    names = {t["name"] for t in mcp_mate._list_tools()}
    assert "find_capabilities" not in names and "request_capabilities" in names
    assert "unknown tool" in mcp_mate._call_tool_inner("find_capabilities", {})
    crewmate_v2._latched = True
    assert "find_capabilities" in {t["name"] for t in mcp_mate._list_tools()}


def test_off_a_worker_turn_end_wakes_no_crewmate(off, monkeypatch):
    from kiro_crew.dashboard import mate_thread_wakes

    called: list[object] = []
    monkeypatch.setattr(mate_thread_wakes, "reporting_creator", lambda *a: called.append(a))
    mate_thread_wakes.note_turn_end(SimpleNamespace(), SimpleNamespace(key="k"), "done")
    assert called == []


def test_off_no_dispatch_summary_and_no_worktree(off, monkeypatch):
    import asyncio

    from kiro_crew.dashboard import (
        chat_utils,
        crewmate_dispatch_capabilities,
        crewmate_worktree_scope,
    )
    from kiro_crew.dashboard.session_control_parts import caller_access

    monkeypatch.setattr(crewmate_dispatch_capabilities, "_note", lambda *a: "worker has: jira")
    monkeypatch.setattr(caller_access, "caller_slot_key", lambda state, key: key)
    monkeypatch.setattr(chat_utils, "slot_history_key", lambda slot: slot)
    state = SimpleNamespace(get_slot=lambda key: key)
    note = crewmate_dispatch_capabilities.own_worker_note
    assert asyncio.run(note(None, state, "a", "b")) == ""
    crewmate_v2._latched = True
    assert asyncio.run(note(None, state, "a", "b")) == "worker has: jira"
    crewmate_v2._latched = False
    assert asyncio.run(crewmate_worktree_scope.scope_worker_turn(None, None, None, None, "")) == ""


def test_off_the_rail_names_no_crewmate_worktree(off, monkeypatch):
    from kiro_crew import crewmate_worktree
    from kiro_crew.context_assembly import turn

    monkeypatch.setattr(crewmate_worktree, "describe", lambda p: (p, "/repo", "crew/ada/main"))
    kwargs = dict(
        project="/w",
        context_groups=None,
        board_tags=None,
        minimal_context=False,
        folder_path=None,
        session_key=None,
        agent=None,
        request_prefix_context=None,
    )
    assert "own git worktree" not in "".join(turn.rail_parts(**kwargs))
    crewmate_v2._latched = True
    monkeypatch.setattr(crewmate_worktree, "take_base_note", lambda p: "")
    assert "own git worktree" in "".join(turn.rail_parts(**kwargs))


# ── startup: off does not migrate; on then off reverses; off then on migrates ──


def _crewmates(user):
    _write(user, "builder", _template("builder", mcpServers={"jira": JIRA}, tools=["@jira"]))
    _write(user, "kirocrew", _template("kirocrew"))
    return _cfg(
        zxc=KiroCrewAgentConfig(member_id="zxc", kiro_agent="builder"),
        ada=KiroCrewAgentConfig(member_id="ada", kiro_agent="kirocrew"),
        mate=KiroCrewAgentConfig(member_id="mate", kiro_agent=CONDUCTOR),
    )


def test_off_the_startup_does_not_migrate(off, layout, persisted, reversal_cfg):
    user, mates = layout
    cfg = _crewmates(user)
    persisted(cfg)
    reversal_cfg(cfg)
    called: list[object] = []
    migrate_or_reverse(cfg, crewmate_v2.enabled(), lambda c: called.append(c))
    assert called == []
    assert cfg.agents["zxc"].kiro_agent == "builder"
    assert not (mates / "zxc.json").exists()


def test_on_then_off_restores_every_crewmate_and_its_threads(
    layout, persisted, threads, reversal_cfg
):
    user, mates = layout
    cfg = _crewmates(user)
    persisted(cfg)
    reversal_cfg(cfg)
    before = _thread(threads, KEY, "zxc", "builder")
    report = migrate_existing_crewmates(cfg)
    assert {"zxc", "ada", "mate"} <= set(report.migrated)
    assert _now(threads, KEY).template_id == CONDUCTOR
    added_on_a_card = _file(mates, "zxc")

    back = reverse_migration(cfg)

    assert back.restored == {"ada": "kirocrew", "mate": "kirocrew", "zxc": "builder"}
    assert {n: cfg.agents[n].kiro_agent for n in ("zxc", "ada", "mate")} == {
        "zxc": "builder",
        "ada": "kirocrew",
        "mate": "kirocrew",
    }
    assert _now(threads, KEY) == before
    # The files stay on disk, untouched, and the ledger no longer names anyone.
    assert _file(mates, "zxc") == added_on_a_card
    ledger = _ledger(mates)
    assert ledger["migrated"] == [] and ledger["threads"] == {}
    assert ledger["reversed"]["zxc"]["threads"] == [KEY]
    # Idempotent: a second boot with the switch off changes nothing.
    again = reverse_migration(cfg)
    assert again.restored == {} and _now(threads, KEY) == before


def test_off_then_on_migrates_again(layout, persisted, threads, reversal_cfg):
    user, mates = layout
    cfg = _crewmates(user)
    persisted(cfg)
    reversal_cfg(cfg)
    _thread(threads, KEY, "zxc", "builder")
    migrate_existing_crewmates(cfg)
    reverse_migration(cfg)

    report = migrate_existing_crewmates(cfg)

    assert "zxc" in report.migrated
    assert cfg.agents["zxc"].kiro_agent == "zxc"
    assert _now(threads, KEY).template_id == CONDUCTOR
    assert "zxc" in _ledger(mates)["migrated"]


def test_a_thread_that_moved_meanwhile_is_left_alone(layout, persisted, threads, reversal_cfg):
    user, _mates = layout
    cfg = _crewmates(user)
    persisted(cfg)
    reversal_cfg(cfg)
    _thread(threads, KEY, "zxc", "builder")
    migrate_existing_crewmates(cfg)
    moved = dataclasses.replace(_now(threads, KEY), template_id="someone-else")
    threads.bind_session_execution(KEY, moved, replace_existing=True, vouch=False)

    reverse_migration(cfg)

    assert _now(threads, KEY).template_id == "someone-else"
    assert cfg.agents["zxc"].kiro_agent == "builder"


def test_a_crewmate_created_while_on_goes_back_to_the_agent_it_was_made_on(
    layout, persisted, reversal_cfg, monkeypatch
):
    from kiro_crew.mate_config import adopt_own_file

    user, mates = layout
    _write(user, "builder", _template("builder"))
    cfg = _cfg(neo=KiroCrewAgentConfig(member_id="neo", kiro_agent="builder"))
    persisted(cfg)
    reversal_cfg(cfg)
    import kiro_crew.config.loader as loader

    monkeypatch.setattr(loader.KiroCrewConfig, "load", staticmethod(lambda *a, **k: cfg))
    monkeypatch.setattr(loader, "_invalidate_config_cache", lambda: None)
    assert adopt_own_file("neo") == "copied"
    assert json.loads((mates.parent / "migrated.json").read_text())["threads"]["neo"]["from"] == (
        "builder"
    )

    reverse_migration(cfg)

    assert cfg.agents["neo"].kiro_agent == "builder"
    assert (mates / "neo.json").exists()
