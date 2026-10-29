# SPDX-License-Identifier: Apache-2.0
"""A crewmate's own chat runs the conductor, whatever its record or the snapshot says.

Two holes let the crewmate's own file (or an outdated template) reach its chat:

* a ``member`` execution record whose ``template_id`` predates the conductor
  (``kirocrew-conductor``) was dispatched verbatim, bypassing
  ``mate_worker.member_chat_agent``;
* on a running loop, a crewmate file the snapshot did not know yet (just
  created by another process) answered "not mate-owned", so the chat ran the
  crewmate file itself.

A crewmate bound to a shared agent keeps running that agent.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import kiro_crew.config.paths as paths_mod
from kiro_crew import mate_worker, session_agent_selection
from kiro_crew.config import loader
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, ResolvedBindings
from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef
from kiro_crew.mate_worker import MATE_CONDUCTOR_AGENT


@pytest.fixture
def home(monkeypatch, tmp_path):
    """A kiro home with a shared user-level agent and a warm snapshot that knows only it."""
    kiro = tmp_path / "kiro"
    (kiro / "agents").mkdir(parents=True)
    (kiro / "mates" / "agents").mkdir(parents=True)
    (kiro / "agents" / "auto-improvement.json").write_text(
        json.dumps({"name": "auto-improvement", "prompt": "improve"}), encoding="utf-8"
    )
    monkeypatch.setattr(paths_mod, "kiro_home", lambda: kiro)
    monkeypatch.setattr(paths_mod, "_agents_dir_override", lambda: kiro / "agents")
    monkeypatch.setattr(paths_mod, "_mate_agents_dir_override", lambda: kiro / "mates" / "agents")
    monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS", frozenset({"auto-improvement", "kirocrew"}))
    monkeypatch.setattr(loader, "_MATERIALIZED_MATE_ONLY", frozenset())
    monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS_READY", True)
    # No rescan may rescue the answer on the loop.
    monkeypatch.setattr(loader, "refresh_materialized_agents", lambda **_: None)
    monkeypatch.setattr(loader, "schedule_materialized_agents_refresh", lambda: None)
    return kiro


def _write_mate_file(home: Path, name: str) -> None:
    (home / "mates" / "agents" / f"{name}.json").write_text(
        json.dumps({"name": name, "prompt": "crewmate"}), encoding="utf-8"
    )


def _record(member_id: str, template_id: str, kind: str = "member") -> ExecutionContext:
    return ExecutionContext(
        member_id=member_id,
        store=MemoryStoreRef(f"member-{member_id}", member_id),
        selection_kind=kind,
        template_id=template_id,
    )


def _resolve(monkeypatch, cfg, record) -> ResolvedBindings:
    monkeypatch.setattr(session_agent_selection, "read_session_execution", lambda _k: record)

    def resolver(config, selected, *project_dir, **_kwargs):
        return ResolvedBindings(
            workspace_dir=Path("workspace"),
            memory_store_name="x",
            effective_memory_config={},
            kiro_agent="resolver-answer",
        )

    return session_agent_selection.resolve_session_agent_bindings(resolver, cfg, "chat-1", None)


def _config(**bindings: str) -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.agents = {
        name: KiroCrewAgentConfig(member_id=name, kiro_agent=kiro_agent)
        for name, kiro_agent in bindings.items()
    }
    return cfg


def test_a_member_record_with_a_stale_template_chats_on_the_conductor(home, monkeypatch):
    """A pre-conductor thread kept ``kirocrew-conductor`` as its template."""
    _write_mate_file(home, "atlas")
    monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS", frozenset({"atlas", "kirocrew"}))
    monkeypatch.setattr(loader, "_MATERIALIZED_MATE_ONLY", frozenset({"atlas"}))
    bindings = _resolve(monkeypatch, _config(atlas="atlas"), _record("atlas", "kirocrew-conductor"))
    assert bindings.kiro_agent == MATE_CONDUCTOR_AGENT


def test_a_member_record_naming_the_crewmate_file_chats_on_the_conductor(home, monkeypatch):
    _write_mate_file(home, "atlas")
    monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS", frozenset({"atlas", "kirocrew"}))
    monkeypatch.setattr(loader, "_MATERIALIZED_MATE_ONLY", frozenset({"atlas"}))
    bindings = _resolve(monkeypatch, _config(atlas="atlas"), _record("atlas", "atlas"))
    assert bindings.kiro_agent == MATE_CONDUCTOR_AGENT


def test_a_shared_agent_crewmate_keeps_running_its_agent(home, monkeypatch):
    cfg = _config(fixer="auto-improvement")
    bindings = _resolve(monkeypatch, cfg, _record("fixer", "auto-improvement"))
    assert bindings.kiro_agent == "auto-improvement"


def test_a_rebound_shared_agent_crewmate_keeps_the_agent_its_thread_recorded(home, monkeypatch):
    """Only a crewmate-owned binding overrides the record; a shared rebind does not."""
    cfg = _config(fixer="kirocrew")
    bindings = _resolve(monkeypatch, cfg, _record("fixer", "auto-improvement"))
    assert bindings.kiro_agent == "auto-improvement"


def test_a_stale_member_record_is_its_crewmates_conductor_given_config(home, monkeypatch):
    """The identity check follows the same rule the chat dispatches by."""
    _write_mate_file(home, "atlas")
    monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS", frozenset({"atlas", "kirocrew"}))
    monkeypatch.setattr(loader, "_MATERIALIZED_MATE_ONLY", frozenset({"atlas"}))
    stale = _record("atlas", "kirocrew-conductor")
    assert mate_worker.is_conductor_execution(stale, _config(atlas="atlas"))
    assert not mate_worker.is_conductor_execution(stale, _config(atlas="auto-improvement"))
    worker = _record("atlas", mate_worker.MATE_WORKER_AGENT, kind="template")
    assert not mate_worker.is_conductor_execution(worker, _config(atlas="atlas"))


def test_a_member_the_config_does_not_name_falls_back_to_its_template(home, monkeypatch):
    bindings = _resolve(monkeypatch, _config(), _record("gone", "auto-improvement"))
    assert bindings.kiro_agent == "auto-improvement"


def test_a_template_record_stays_exact(home, monkeypatch):
    """A worker (template selection on the worker) is not routed through the member rule."""
    _write_mate_file(home, "atlas")
    monkeypatch.setattr(loader, "_MATERIALIZED_MATE_ONLY", frozenset({"atlas"}))
    record = _record("atlas", mate_worker.MATE_WORKER_AGENT, kind="template")
    bindings = _resolve(monkeypatch, _config(atlas="atlas"), record)
    assert bindings.kiro_agent == mate_worker.MATE_WORKER_AGENT


@pytest.mark.asyncio
async def test_a_fresh_crewmate_unknown_to_the_snapshot_chats_on_the_conductor_on_the_loop(home):
    """Created by another process a moment ago: the snapshot has not seen its file."""
    _write_mate_file(home, "cove")
    assert mate_worker.member_chat_agent("cove") == MATE_CONDUCTOR_AGENT


@pytest.mark.asyncio
async def test_a_user_level_agent_unknown_to_the_snapshot_is_not_a_crewmate_on_the_loop(home):
    """A user file of the same name wins, the rule the full scan applies."""
    _write_mate_file(home, "shared")
    (home / "agents" / "shared.json").write_text(
        json.dumps({"name": "shared", "prompt": "mine"}), encoding="utf-8"
    )
    assert mate_worker.member_chat_agent("shared") == "shared"


@pytest.mark.asyncio
async def test_an_unknown_name_with_no_file_is_not_a_crewmate_on_the_loop(home):
    assert mate_worker.member_chat_agent("nobody") == "nobody"
    assert not mate_worker.is_mate_owned_agent("../agents/auto-improvement")
    assert not mate_worker.is_mate_owned_agent(MATE_CONDUCTOR_AGENT)


@pytest.mark.asyncio
async def test_the_on_loop_check_never_rescans(home, monkeypatch):
    """Only two stats: a full rescan on the loop would stall every other session."""
    _write_mate_file(home, "cove")

    def _fail(**_):
        raise AssertionError("rescanned on the loop")

    monkeypatch.setattr(loader, "refresh_materialized_agents", _fail)
    asyncio.get_running_loop()
    assert mate_worker.is_mate_owned_agent("cove")
