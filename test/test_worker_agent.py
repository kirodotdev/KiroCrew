"""The ``kirocrew-worker`` agent spec, and the two conductors' work-ledger mounts.

Pins the Phase 2 exit criteria about specs: the worker spec is the default agent's
SUPERSET plus ``@kirocrew-work``, and both conductor specs auto-approve the two
conductor tools while auto-approving neither worker tool.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from kiro_crew import agent
from kiro_crew.agent_files import (
    CONDUCTOR_AGENT_FILENAME,
    LEDGER_CONDUCTOR_AGENT_FILENAME,
    OWNED_KIRO_AGENT_FILES,
    PIPELINE_CONDUCTOR_AGENT_FILENAME,
    WORKER_AGENT_FILENAME,
)


@pytest.fixture()
def specs(tmp_path, monkeypatch) -> dict[str, dict[str, Any]]:
    """Install the four related specs into a throwaway agents dir and read them back."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    agent._install_worker_agent()
    agent._install_conductor_agent()
    agent._install_pipeline_conductor_agent()
    agent._install_ledger_conductor_agent()
    return {
        name: json.loads((tmp_path / name).read_text(encoding="utf-8"))
        for name in (
            WORKER_AGENT_FILENAME,
            CONDUCTOR_AGENT_FILENAME,
            PIPELINE_CONDUCTOR_AGENT_FILENAME,
            LEDGER_CONDUCTOR_AGENT_FILENAME,
        )
    }


# ── the worker is a superset, not a narrowing ─────────────────────────────


def test_the_worker_spec_carries_every_default_tool(specs):
    """The whole of v2's reversal. A worker writes files, runs builds and drives
    git, so anything a NARROWED spec withheld would be something some work item
    needs — the same defect an omitted ``agent`` produces by handing the child
    ``kirocrew-conductor``, which has no ``fs_write``."""
    worker = specs[WORKER_AGENT_FILENAME]
    default_tools = set(agent.build_agent_config().get("tools") or [])
    assert default_tools, "the default template resolved no tools at all"
    assert default_tools <= set(worker["tools"]), (
        "the worker spec withholds a default tool: "
        f"{sorted(default_tools - set(worker['tools']))}"
    )


def test_the_worker_spec_mounts_the_work_server(specs):
    worker = specs[WORKER_AGENT_FILENAME]
    assert "@kirocrew-work" in worker["tools"]
    entry = worker["mcpServers"]["kirocrew-work"]
    assert entry["args"][-1] == "mcp-work"
    assert "autoApprove" not in entry


def test_the_worker_spec_auto_approves_both_worker_tools_and_neither_conductor_one(specs):
    """A worker that must ask permission to say it is blocked will not say it. And a
    worker holding a conductor grant would be auto-approving a tool whose only
    answer to it is a refusal."""
    allowed = specs[WORKER_AGENT_FILENAME]["allowedTools"]
    assert "@kirocrew-work/work_brief" in allowed
    assert "@kirocrew-work/work_report" in allowed
    assert "@kirocrew-work/work_ledger_read" not in allowed
    assert "@kirocrew-work/work_ledger_record" not in allowed


def test_the_worker_spec_keeps_the_default_grants_it_inherited(specs):
    """Appended, not rewritten: a grant added to the default agent tomorrow reaches
    the worker for free, and a grant the ceiling withholds there stays withheld."""
    worker = specs[WORKER_AGENT_FILENAME]
    default_allowed = set(agent.build_agent_config().get("allowedTools") or [])
    assert default_allowed <= set(worker["allowedTools"])


def test_the_worker_prompt_states_the_reporting_contract(specs):
    prompt = specs[WORKER_AGENT_FILENAME]["prompt"]
    for token in (
        "work_brief",
        "work_report",
        "progress",
        "blocked",
        "question",
        "done",
        "artifacts",
        "acceptance",
    ):
        assert token in prompt, token


def test_the_worker_prompt_says_only_decision_is_an_instruction(specs):
    """The prompt's share of the threat model: a worker reads ONE instruction field
    from its conductor, and everything else it reads is state."""
    prompt = specs[WORKER_AGENT_FILENAME]["prompt"]
    assert "`decision`" in prompt
    lowered = prompt.lower()
    assert "instruction" in lowered
    assert "user message" in lowered


def test_the_worker_prompt_carries_the_verbosity_placeholder(specs):
    """Without the token the dashboard verbosity setting never reaches this agent."""
    assert "{{VERBOSITY_BLOCK}}" in specs[WORKER_AGENT_FILENAME]["prompt"]


def test_the_worker_prompt_says_done_is_a_claim(specs):
    """``work_report`` cannot write a verdict, and the prompt must not imply it can."""
    prompt = specs[WORKER_AGENT_FILENAME]["prompt"]
    assert "claim" in prompt.lower()
    assert "verdict" in prompt.lower()


def test_the_worker_spec_derives_its_kas_permissions_from_the_filtered_grants(specs):
    """Derived rather than restated, so a ceiling that strips a grant strips its
    KAS rule with it."""
    worker = specs[WORKER_AGENT_FILENAME]
    assert worker.get("permissions"), "no KAS policy derived"
    rendered = json.dumps(worker["permissions"])
    for ref in worker["allowedTools"]:
        if ref.startswith("@kirocrew-work/"):
            assert ref.split("/", 1)[1] in rendered, ref


def test_the_worker_filename_is_owned_and_wired(specs, tmp_path):
    assert WORKER_AGENT_FILENAME == "kirocrew-worker.json"
    assert WORKER_AGENT_FILENAME in OWNED_KIRO_AGENT_FILES
    assert (tmp_path / WORKER_AGENT_FILENAME).is_file()
    assert specs[WORKER_AGENT_FILENAME]["name"] == "kirocrew-worker"


def test_the_worker_install_runs_on_the_rebuild_path():
    """Eager, like its six siblings — and that placement is FORCED, not chosen.

    ``session_create`` refuses an agent it cannot resolve, and resolution reads an
    in-memory snapshot refreshed at boot rather than the directory, so a spec written
    on the spawn path is invisible to the validation ahead of the spawn. The
    companion test below measures that. Same degrade-to-debug footing as the
    siblings: a failed install disables one feature rather than every turn, which is
    why it is not in ``REQUIRED_KIRO_AGENT_FILES``.
    """
    import inspect

    from kiro_crew.agent_files import REQUIRED_KIRO_AGENT_FILES

    src = inspect.getsource(agent.rebuild_agent_config)
    assert "_install_worker_agent()" in src
    assert WORKER_AGENT_FILENAME not in REQUIRED_KIRO_AGENT_FILES


def test_a_lazily_written_spec_would_not_resolve_for_session_create(tmp_path, monkeypatch):
    """The measurement behind the choice above, kept as a test so the reasoning
    cannot be quietly reverted.

    ``resolve_agent_bindings`` -> ``_materialized_kiro_agent`` answers from a snapshot
    that a spec write does NOT refresh, and ``session_create`` refuses an unresolved
    name with ``agent_unresolved`` BEFORE the spawn path runs. So writing the spec
    lazily leaves the feature unusable on a clean install: the conductor's very first
    dispatch is refused.
    """
    from kiro_crew.config import loader

    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "kirocrew.json").write_text('{"name": "kirocrew"}', encoding="utf-8")
    monkeypatch.setattr(loader, "kiro_agents_dir", lambda: agents)
    loader.refresh_materialized_agents()

    # The default is in the boot snapshot; a not-yet-written worker spec is not.
    assert bool(loader._materialized_kiro_agent("kirocrew", None)) is True
    assert bool(loader._materialized_kiro_agent("kirocrew-worker", None)) is False

    # Writing it later does not publish it either — the snapshot is not refreshed by
    # a write, which is why the boot install is the only moment that works.
    (agents / WORKER_AGENT_FILENAME).write_text('{"name": "kirocrew-worker"}', encoding="utf-8")
    assert bool(loader._materialized_kiro_agent("kirocrew-worker", None)) is False
    loader.refresh_materialized_agents()
    assert bool(loader._materialized_kiro_agent("kirocrew-worker", None)) is True


def test_every_boot_re_filters_the_worker_grants_through_the_ceiling(tmp_path, monkeypatch):
    """The property the lazy path lost: a spec cannot outlive a tightened ceiling,
    because the installer re-runs on every ``rebuild_agent_config`` — inherited
    grants included, not just this server's."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    agent._install_worker_agent()
    granted = json.loads((tmp_path / WORKER_AGENT_FILENAME).read_text(encoding="utf-8"))
    assert "@kirocrew-work/work_report" in granted["allowedTools"]

    monkeypatch.setattr(agent, "_may_auto_approve", lambda ref: False)
    agent._install_worker_agent()
    regranted = json.loads((tmp_path / WORKER_AGENT_FILENAME).read_text(encoding="utf-8"))
    assert regranted["allowedTools"] == []
    assert regranted["permissions"] == {"rules": []}
    # Still MOUNTED — the ceiling removes auto-approve, not the tool.
    assert "@kirocrew-work" in regranted["tools"]


# ── exactly ONE conductor mounts it, per tool ─────────────────────────────


def test_the_ledger_conductor_mounts_the_server_and_grants_only_its_own_half(specs):
    """Per tool rather than whole-server, because the worker half is mounted on the
    same server. Missing these is not an error but a silent approval prompt on every
    patrol cycle, which is why they are asserted."""
    spec = specs[LEDGER_CONDUCTOR_AGENT_FILENAME]
    assert "@kirocrew-work" in spec["tools"]
    entry = spec["mcpServers"]["kirocrew-work"]
    assert entry["args"][-1] == "mcp-work"
    assert "autoApprove" not in entry
    allowed = spec["allowedTools"]
    assert "@kirocrew-work/work_ledger_read" in allowed
    assert "@kirocrew-work/work_ledger_record" in allowed
    # The one worker verb a conductor may hold: a read of its OWN bound item, and a
    # nested conductor's mandated first call. The write stays gated.
    assert "@kirocrew-work/work_brief" in allowed
    assert "@kirocrew-work/work_report" not in allowed
    # Whole-server auto-approve would grant the write by the back door.
    assert "@kirocrew-work" not in allowed


@pytest.mark.parametrize("filename", [CONDUCTOR_AGENT_FILENAME, PIPELINE_CONDUCTOR_AGENT_FILENAME])
def test_a_shipped_conductor_does_not_mount_the_server(specs, filename):
    """The two shipped conductors mounted this server briefly, and the mount is
    retracted.

    The tools alone do not describe the change they came with: the ledger flow
    inverts the dispatch order (bind before seed) and replaces the patrol cycle (a
    ledger read instead of a transcript read), so mounting them on an agent that
    ships a different procedure hands its users a procedure they did not choose.
    Asserted negatively, on every surface a mount can survive on, so it cannot
    return unnoticed — the KAS rule especially, since nothing reads
    ``allowedTools`` on that backend.
    """
    spec = specs[filename]
    assert "@kirocrew-work" not in spec["tools"]
    assert "kirocrew-work" not in spec["mcpServers"]
    assert not [ref for ref in spec["allowedTools"] if "kirocrew-work" in ref]
    assert not [m for m in spec["permissions"]["rules"][0]["match"] if "kirocrew-work" in m]
    for token in ("work_ledger", "work_brief", "work_report", "kirocrew-work"):
        assert token not in spec["prompt"], token


@pytest.mark.parametrize(
    "filename",
    [CONDUCTOR_AGENT_FILENAME, PIPELINE_CONDUCTOR_AGENT_FILENAME, LEDGER_CONDUCTOR_AGENT_FILENAME],
)
def test_a_conductor_still_has_no_file_writing_tool(specs, filename):
    """The property the conductor installers' docstrings argue for, re-asserted here
    because this change edits their ``tools`` lists: neither mounting the work server
    nor copying an installer may smuggle a write tool in beside it."""
    tools = specs[filename]["tools"]
    assert "fs_write" not in tools
    assert "code" not in tools


def test_the_grant_tuples_cover_the_server_and_share_only_the_read():
    assert agent._LEDGER_CONDUCTOR_WORK_GRANTS == (
        "@kirocrew-work/work_ledger_read",
        "@kirocrew-work/work_ledger_record",
        "@kirocrew-work/work_brief",
    )
    assert agent._WORKER_WORK_GRANTS == (
        "@kirocrew-work/work_brief",
        "@kirocrew-work/work_report",
    )
    # Together they cover the server's whole surface. The one overlap is the
    # read-only ``work_brief``: a nested conductor is also a worker, and its first
    # mandated call must not be an approval stall. The write is never shared.
    from kiro_crew import mcp_work

    granted = {
        ref.split("/", 1)[1]
        for ref in agent._LEDGER_CONDUCTOR_WORK_GRANTS + agent._WORKER_WORK_GRANTS
    }
    assert granted == set(mcp_work.WORK_TOOLS)
    assert set(agent._LEDGER_CONDUCTOR_WORK_GRANTS) & set(agent._WORKER_WORK_GRANTS) == {
        "@kirocrew-work/work_brief"
    }


def test_the_hand_built_entry_carries_the_registry_and_home_pins(monkeypatch):
    """Without ``type: registry`` a registry-mode client silently DROPS the entry, so
    the granted tools never launch and the grant is dead with no local error."""
    monkeypatch.setattr(agent, "_mcp_registry_mode", lambda: True)
    monkeypatch.setattr(agent, "_managed_mcp_env", lambda: {"KIROCREW_HOME": "/tmp/home"})
    entry = agent._managed_opt_in_entry("mcp-work")
    assert entry["type"] == agent._MCP_REGISTRY_TYPE
    assert entry["env"] == {"KIROCREW_HOME": "/tmp/home"}
    assert entry["args"][-1] == "mcp-work"


def test_the_hand_built_entry_is_bare_on_a_default_install(monkeypatch):
    monkeypatch.setattr(agent, "_mcp_registry_mode", lambda: False)
    monkeypatch.setattr(agent, "_managed_mcp_env", lambda: {})
    entry = agent._managed_opt_in_entry("mcp-work")
    assert set(entry) == {"command", "args"}
